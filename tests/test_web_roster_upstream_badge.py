#!/usr/bin/env python3
"""Render-leg contract for the roster's walled/rate-limited badge (#230).

The DETECTION side of a provider wall was already true twice over — proxywatch
records the family sweep, `helm seat where gemini` speaks it, seat_liveness
reports WALLED — and the owner still learned about gemini's 429 daily-quota
wall from pane errors, because the ROSTER row rendered the wall exactly like
idle. Server-side tests (test_web_ledger_native.py) pin the JOIN; these pin
the last leg: what the owner's eyes actually receive.

They run the REAL upstreamBadge / esc / lago / lagoIso / fmtIn source lifted
verbatim from the assembled web UI under node with a frozen clock, so a regression
in the composition (state without since, a dark class on UNKNOWN, a horizon
invented where none is recorded) fails here and not on the owner's phone.
Requires node; skipped (not failed) where node is unavailable, like any
optional toolchain — the server-side join tests do not depend on it."""
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest

from helm import proxywatch, seat, web_ui_loader

HERE = os.path.dirname(os.path.abspath(__file__))

# the real client code the harness drives — `function NAME(){}` and
# `const NAME = …;` forms both appear in assembled web UI
LIFT = ["esc", "lago", "lagoIso", "fmtIn", "upstreamBadge", "renderRoster"]


def _lift(src, name):
    """The verbatim `function NAME(…) {…}` or `const NAME = …;` source.

    Brace/paren-matched with string, template, comment AND regex-literal
    awareness — `esc` holds `/[&<>"']/g`, whose quotes would derail a scanner
    that only tracks strings (the chat-runtime extractor predates that need).
    A `/` opens a regex only where an expression may START (after an operator,
    an opener, or a keyword boundary); after a value it is division."""
    m = re.search(r"(?:function\s+" + re.escape(name) + r"\s*\(|const\s+"
                  + re.escape(name) + r"\s*=)", src)
    if not m:
        raise AssertionError("not found in assembled web UI: " + name)
    is_fn = src[m.start()] == "f"
    i, n = m.end(), len(src)
    # the function head's regex consumed its opening paren: count it
    depth, quote, esc_next, last = (1 if is_fn else 0), None, False, "="
    while i < n:
        c = src[i]
        if quote:
            if esc_next:
                esc_next = False
            elif c == "\\":
                esc_next = True
            elif c == quote:
                quote = None
                last = c              # a closed string is a VALUE: `"x" / 2`
            i += 1                    # must read as division, never regex
            continue
        if c in "\"'`":
            quote = c
        elif c == "/" and i + 1 < n and src[i + 1] == "/":
            i = src.index("\n", i) if "\n" in src[i:] else n
            continue
        elif c == "/" and i + 1 < n and src[i + 1] == "*":
            i = src.index("*/", i) + 2
            continue
        elif c == "/" and (last in "=(,:[!&|?{};+-*%<>~^" or last == ""):
            # regex literal: scan to its unescaped closing /, [] class aware
            i += 1
            in_class, esc_next = False, False
            while i < n:
                r = src[i]
                if esc_next:
                    esc_next = False
                elif r == "\\":
                    esc_next = True
                elif r == "[":
                    in_class = True
                elif r == "]":
                    in_class = False
                elif r == "/" and not in_class:
                    break
                i += 1
        elif c in "({[":
            depth += 1
        elif c in ")}]":
            depth -= 1
            if is_fn and c == "}" and depth == 0:
                return src[m.start():i + 1]
        elif c == ";" and depth == 0 and not is_fn:
            return src[m.start():i + 1]
        if not c.isspace():
            last = c
        i += 1
    raise AssertionError("unterminated source lifting " + name)


# frozen clock: 2026-08-05T03:00:00Z — walls and horizons render as exact ages
HARNESS = """"use strict";
const FIXED = Date.parse("2026-08-05T03:00:00Z");
Date.now = () => FIXED;
/*__INJECT__*/
const iso2h = "2026-08-05T01:00:00Z";           // walled for 2h at FIXED
const out = {};
out.walled = upstreamBadge({upstream: "RATE-LIMITED", upstream_since: iso2h,
                            upstream_dark: true, upstream_family: "gemini",
                            upstream_remediation_text: __UNKNOWN_REMEDIATION__});
out.proxy_cooldown = upstreamBadge({upstream: __PROXY_COOLDOWN__,
                            upstream_since: iso2h, upstream_dark: true,
                            upstream_remediation_text: __HELPFUL_REMEDIATION__});
out.walled_horizon = upstreamBadge({upstream: "AUTH-UNAVAILABLE",
                            upstream_since: iso2h, upstream_dark: true,
                            upstream_restart: "UNKNOWN",
                            upstream_restart_target: null,
                            upstream_remediation_evidence: "provider auth wall",
                            upstream_resets_at_ms: FIXED + 3 * 3600 * 1000});
out.passed_horizon = upstreamBadge({upstream: "AUTH-UNAVAILABLE",
                            upstream_since: iso2h, upstream_dark: true,
                            upstream_restart: "UNKNOWN",
                            upstream_resets_at_ms: FIXED - 1000});
out.healthy = upstreamBadge({upstream: "HEALTHY"});
out.no_upstream = upstreamBadge({seat: "helm-claude-2"});
out.unknown_stale = upstreamBadge({upstream: "UNKNOWN",
  upstream_why: "proxywatch last wrote 50m ago (>40m): this is the last " +
                "thing it saw, not the state now",
  upstream_stale_state: "RATE-LIMITED", upstream_since: iso2h});
out.legacy_no_dark = upstreamBadge({upstream: "RATE-LIMITED",
                                    upstream_since: iso2h});
out.bad_iso = upstreamBadge({upstream: "RATE-LIMITED",
                             upstream_since: "not-a-date",
                             upstream_dark: true});
out.hostile = upstreamBadge({upstream: '<img src=x onerror=1>',
                             upstream_dark: true});
out.explicit_false = upstreamBadge({upstream: "RATE-LIMITED",
                                    upstream_since: iso2h,
                                    upstream_dark: false});
out.unreadable_dark = upstreamBadge({upstream: "RATE-LIMITED",
                                     upstream_since: iso2h,
                                     upstream_dark: null});
out.nonfinite_horizon = upstreamBadge({upstream: "RATE-LIMITED",
                                       upstream_since: iso2h,
                                       upstream_dark: true,
                                       upstream_resets_at_ms: NaN});
console.log(JSON.stringify(out));
"""


class RosterWalledBadgeRenderTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        if not cls.node:
            raise unittest.SkipTest("node not available")
        cls.src = web_ui_loader.read_text()
        lifted = {name: _lift(cls.src, name) for name in LIFT}
        # renderRoster is lifted for the WIRED check only, never executed
        cls.render_roster = lifted.pop("renderRoster")
        helpful = seat.remediation_text(seat.upstream_remediation({
            "upstream": {"gemini": {
                "state": proxywatch._PROXY_COOLDOWN, "dark": True,
                "falsification_bar_s": 1800, "seats": {"gemini": {
                    "state": proxywatch._PROXY_COOLDOWN, "dark": True,
                    "falsification_due": True, "falsification_age_s": 3600,
                    "falsification_seat": "gemini"}}}}}, "gemini", "gemini"))
        unknown = seat.remediation_text(seat.upstream_remediation(
            {"upstream": {"gemini": {"state": "RATE-LIMITED",
                                       "dark": True, "seats": {"gemini": {
                                           "state": "RATE-LIMITED",
                                           "dark": True}}}}},
            "gemini", "gemini"))
        assembled = HARNESS.replace(
            "__PROXY_COOLDOWN__", json.dumps(proxywatch._PROXY_COOLDOWN)).replace(
            "__HELPFUL_REMEDIATION__", json.dumps(helpful)).replace(
            "__UNKNOWN_REMEDIATION__", json.dumps(unknown)).replace(
            "/*__INJECT__*/", "\n\n".join(lifted[n] for n in lifted))
        cls.tmp = tempfile.mkdtemp(prefix="helm-roster-badge-")
        path = os.path.join(cls.tmp, "run.js")
        with open(path, "w", encoding="utf-8") as f:
            f.write(assembled)
        chk = subprocess.run([cls.node, "--check", path],
                             capture_output=True, text=True)
        assert chk.returncode == 0, "node --check failed:\n" + chk.stderr
        proc = subprocess.run([cls.node, path], capture_output=True,
                              text=True, timeout=60)
        assert proc.returncode == 0, "harness failed:\n" + proc.stderr
        cls.out = json.loads(proc.stdout)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(getattr(cls, "tmp", ""), ignore_errors=True)

    def test_a_walled_seat_renders_the_cause_and_the_since(self):
        """THE OWNER'S GAP: a wall must not read like idle. The badge names
        the cause loud (RATE-LIMITED), carries how long in the roster's
        relative idiom (2h ago), and the hover holds the full ISO since plus
        the #163 sentence — a wall is a fact, not unreadiness."""
        b = self.out["walled"]
        self.assertIn('class="rup dark"', b)
        self.assertIn(">RATE-LIMITED<", b)
        self.assertIn("2h ago", b)
        self.assertIn("WALLED — upstream RATE-LIMITED since 2026-08-05T01:00:00Z", b)
        self.assertIn("a wall is a fact, not unreadiness", b)

    def test_stale_local_cooldown_prescribes_the_derived_proxy_restart(self):
        b = self.out["proxy_cooldown"]
        self.assertIn("PRESCRIBES: restart this exact proxy", b)
        self.assertIn("rerun helm proxywatch", b)
        self.assertNotIn("local restart does not repair", b)

    def test_provider_rate_limit_never_gets_the_proxy_restart_advice(self):
        b = self.out["walled"]
        self.assertIn("remediation UNKNOWN", b)
        self.assertNotIn("PRESCRIBES:", b)
        self.assertNotIn("restart/reprobe can help", b)

    def test_no_reset_horizon_is_recorded_and_the_badge_says_so(self):
        """Nothing feeds resets_at_ms yet (#45 will). The honest render is a
        stated absence in the hover and NO horizon glyph — not a guessed
        countdown and not silence a reader could mistake for 'imminent'."""
        b = self.out["walled"]
        self.assertIn("no recorded reset horizon", b)
        self.assertNotIn("↻", b)

    def test_a_recorded_reset_horizon_rides_the_badge(self):
        """The #45 seam, render side: an epoch-ms horizon 3h out shows as
        '↻ 3h' beside the since-age, with the prose form in the hover."""
        b = self.out["walled_horizon"]
        self.assertIn("↻ 3h", b)
        self.assertIn("recorded reset in 3h", b)
        self.assertNotIn("no recorded reset horizon", b)

    def test_a_passed_horizon_never_asserts_a_future_reset(self):
        b = self.out["passed_horizon"]
        self.assertIn("recorded reset horizon has passed", b)
        self.assertIn("current verdict still governs", b)
        self.assertNotIn("reset in", b)
        self.assertNotIn("↻", b)

    def test_a_seat_without_a_wall_stays_unbadged(self):
        """HEALTHY and no-upstream rows render NOTHING — the badge means
        exactly one thing wherever it appears, so its absence must too.
        Positive control on the SAME observable first: the walled case DOES
        render, so an upstreamBadge stubbed to always-empty cannot pass."""
        out = self.out
        self.assertIn('class="rup dark"', out["walled"])
        self.assertEqual(out["healthy"], "")
        self.assertEqual(out["no_upstream"], "")

    def test_a_stale_record_renders_UNKNOWN_honest_never_healthy(self):
        """A watcher that stopped writing yields an UNKNOWN badge (its own
        muted class, never the wall's red) that still surfaces the recorder's
        reason and the last thing it saw — more useful than either half, and
        never a blank a reader would file as health."""
        b = self.out["unknown_stale"]
        self.assertIn('class="rup unknown"', b)
        self.assertIn(">UNKNOWN<", b)
        self.assertIn("not the state now", b)
        self.assertIn("last saw RATE-LIMITED", b)
        self.assertNotIn('class="rup dark"', b)
        self.assertNotIn("a wall is a fact", b)

    def test_an_old_server_payload_still_reads_as_a_wall(self):
        """HALF-LIVE TOLERANCE (code_drift): assembled web UI is read per request,
        web.py is import-once — so this HTML will meet payloads with no
        upstream_dark until the console restarts. Every non-healthy state the
        old server forwards IS a named dark cause, so the wall language must
        survive the missing field."""
        b = self.out["legacy_no_dark"]
        self.assertIn('class="rup dark"', b)
        self.assertIn("WALLED", b)
        self.assertIn("2h ago", b)

    def test_an_unparseable_since_drops_the_age_not_the_fact(self):
        """A since the client cannot parse still names the wall and quotes the
        raw string in the hover; only the relative age disappears, because a
        wrong '—' beside the cause would be rendered ignorance."""
        b = self.out["bad_iso"]
        self.assertIn(">RATE-LIMITED<", b)
        self.assertIn("since not-a-date", b)
        self.assertNotIn("rupd", b)

    def test_a_hostile_state_string_is_escaped(self):
        """The badge is innerHTML like the rest of the row; the record's state
        travels through esc() so a poisoned proxywatch file cannot script the
        owner's console."""
        b = self.out["hostile"]
        self.assertNotIn("<img", b)
        self.assertIn("&lt;img", b)

    def test_an_authoritative_false_renders_calm_never_the_walls_red(self):
        """The FIX round, blocker 2, render half. upstream_dark === false
        is the RECORD saying "not a wall" — the fact (state, since) still
        renders, but in the calm class: no red, no WALLED, no #163 alarm
        sentence, and the hover states the verdict in words."""
        b = self.out["explicit_false"]
        self.assertIn('class="rup calm"', b)
        self.assertIn(">RATE-LIMITED<", b)
        self.assertIn("2h ago", b)
        self.assertIn("the recorded verdict: not a wall", b)
        self.assertNotIn('class="rup dark"', b)
        self.assertNotIn("WALLED", b)
        self.assertNotIn("a wall is a fact", b)

    def test_an_unreadable_dark_verdict_withholds_the_wall_styling(self):
        """The server maps a corrupt persisted dark (string "false", 1) to
        null: a verdict it could not read. The badge still names the state
        and since, but asserts NEITHER verdict — calm class, and the hover
        says the styling was withheld and why."""
        b = self.out["unreadable_dark"]
        self.assertIn('class="rup calm"', b)
        self.assertIn(">RATE-LIMITED<", b)
        self.assertIn("wall verdict is unreadable", b)
        self.assertNotIn('class="rup dark"', b)
        self.assertNotIn("WALLED", b)

    def test_a_non_finite_horizon_never_renders_a_countdown(self):
        """Belt to the server's finite gate: typeof NaN is "number", so the
        old check would have rendered "resets in NaN". Number.isFinite drops
        it — the badge keeps the wall and states the horizon's absence."""
        b = self.out["nonfinite_horizon"]
        self.assertIn(">RATE-LIMITED<", b)
        self.assertIn("no recorded reset horizon", b)
        self.assertNotIn("NaN", b)
        self.assertNotIn("↻", b)

    def test_the_badge_is_WIRED_into_renderRoster_not_merely_defined(self):
        """The server-side twin of this test guards the join's call site; this
        guards the render's: renderRoster's seat cell must call upstreamBadge,
        or every case above passes green over a console that shows nothing.
        The lift is proven REAL first — the function that writes the roster
        body must be the one holding the call, not a stub the scanner cut
        short."""
        rr = self.render_roster
        self.assertIn('$("#rtbody").innerHTML', rr)
        self.assertIn("upstreamBadge(s)", rr)


# ── THE PREDICATE'S SENTENCE ON EVERY BADGE BRANCH, AND THE WALL COUNT ───────
# Two client surfaces render the one availability predicate, and both had a
# branch the server-side arms cannot see:
#
#   * upstreamBadge returns EARLY for an UNKNOWN upstream, and the sentence was
#     appended after that return. The two fields answer DIFFERENT questions and
#     legitimately differ — a record judged stale reads upstream UNKNOWN while a
#     latched record still says UNAVAILABLE — so the one surface with a hover
#     omitted the exact fact every terminal was printing.
#   * the home strip counts UNAVAILABLE seats REGARDLESS of presence, which is
#     right (the wall holds their mail either way) and made its hover's "these
#     seats are alive" a second claim from a signal the filter never read, while
#     "Nothing needs relaunching" read as advice about the pane.
#
# Lifted verbatim and EXECUTED, because both defects are in a branch and a
# string: a source scan for the new field would pass over the early return.
# `lrAgo` and `cardSource` are lifted because dashFleet CALLS cardSource — the
# home strip names its read like every other card. They are the shipped
# functions, not stubs: a stub would let the lift keep passing if the row's
# source line were ever removed, and the point of executing dashFleet is that
# the whole expression it builds is real.
AVAIL_LIFT = ["esc", "lago", "lagoIso", "fmtIn", "chatShort", "lrDur", "lrAgo",
              "cardSource", "upstreamBadge", "dashFleet"]

AVAIL_HARNESS = """"use strict";
const FIXED = Date.parse("2026-08-05T03:00:00Z");
Date.now = () => FIXED;
let DASH_FLEET_HTML = "", DASH_FLEET_TS = 0;
let CAPTURED = null;
const $ = () => ({set innerHTML(v) { CAPTURED = v; }});
/*__INJECT__*/
const SENTENCE = __SENTENCE__;
const out = {};
// a LATCHED wall whose record has gone stale: upstream UNKNOWN, availability
// UNAVAILABLE — the shape the early return dropped
out.unknown_latched = upstreamBadge({upstream: "UNKNOWN",
  upstream_why: "proxywatch last wrote 71m ago (bar 40m)",
  upstream_stale_state: "AUTH-401", availability: "UNAVAILABLE",
  availability_text: SENTENCE});
// the same branch with NOTHING to quote: the control that the sentence is
// carried from the payload rather than minted by this branch
out.unknown_bare = upstreamBadge({upstream: "UNKNOWN",
  upstream_why: "proxywatch last wrote 71m ago (bar 40m)"});
// the walled branch, which already carried it: the positive control that the
// harness can see the sentence at all
out.walled = upstreamBadge({upstream: "AUTH-401", upstream_dark: true,
  upstream_since: "2026-08-05T01:00:00Z", availability: "UNAVAILABLE",
  availability_text: SENTENCE});
dashFleet([{seat: "kimi", presence: "absent", availability: "UNAVAILABLE",
            availability_text: SENTENCE},
           {seat: "codex", presence: "fresh"}]);
out.fleet_walled_absent = CAPTURED;
dashFleet([{seat: "codex", presence: "fresh"}]);
out.fleet_no_wall = CAPTURED;
console.log(JSON.stringify(out));
"""

SENTENCE = "UNAVAILABLE kimi AUTH-401 since 2026-09-13T00:55:03Z"


class AvailabilityReachesEveryClientBranchTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        if not cls.node:
            raise unittest.SkipTest("node not available")
        src = web_ui_loader.read_text()
        lifted = {name: _lift(src, name) for name in AVAIL_LIFT}
        assembled = AVAIL_HARNESS.replace(
            "__SENTENCE__", json.dumps(SENTENCE)).replace(
            "/*__INJECT__*/", "\n\n".join(lifted[n] for n in lifted))
        cls.tmp = tempfile.mkdtemp(prefix="helm-avail-client-")
        path = os.path.join(cls.tmp, "run.js")
        with open(path, "w", encoding="utf-8") as f:
            f.write(assembled)
        chk = subprocess.run([cls.node, "--check", path],
                             capture_output=True, text=True)
        assert chk.returncode == 0, "node --check failed:\n" + chk.stderr
        proc = subprocess.run([cls.node, path], capture_output=True,
                              text=True, timeout=60)
        assert proc.returncode == 0, "harness failed:\n" + proc.stderr
        cls.out = json.loads(proc.stdout)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(getattr(cls, "tmp", ""), ignore_errors=True)

    def test_a_stale_record_over_a_latched_wall_still_quotes_the_sentence(self):
        """The owner hovering a seat whose watcher stopped must still read the
        predicate's word, because the latch is the fact he asked for. The
        positive control is the walled branch, which proves the harness can see
        this string; the bare UNKNOWN is the negative, which proves the branch
        does not mint it."""
        self.assertIn(SENTENCE, self.out["walled"])          # the control
        self.assertIn(SENTENCE, self.out["unknown_latched"])
        self.assertNotIn(SENTENCE, self.out["unknown_bare"])
        # the branch keeps its own styling and its own reason: the sentence is
        # ADDITIVE, and an UNKNOWN upstream must not acquire the wall's red
        self.assertIn("last wrote 71m ago", self.out["unknown_latched"])
        self.assertIn('class="rup unknown"', self.out["unknown_latched"])

    def test_the_wall_count_advises_no_relaunch_and_asserts_no_liveness(self):
        """The count deliberately includes ABSENT seats, so its hover may not
        call them alive — and the one thing true of every row in it is that a
        relaunch is not the cure. The control is the same renderer with no
        walled seat, which must produce no wall term at all."""
        walled = self.out["fleet_walled_absent"]
        self.assertIn("1 UNAVAILABLE", walled)
        self.assertIn(SENTENCE, walled)
        self.assertIn("RELAUNCHING DOES NOT CLEAR A WALL", walled)
        self.assertNotIn("these seats are alive", walled)
        self.assertNotIn("Nothing needs relaunching", walled)
        self.assertIn("pane", walled)      # it says what it does NOT know
        # THE UNCONDITIONAL POSITIVE CONTROL ON THE SAME OBSERVABLE: the
        # wall-free render is a real render of a real seat, so its missing wall
        # term is an absence rather than a renderer that produced nothing.
        bare = self.out["fleet_no_wall"]
        self.assertIn("active", bare)
        self.assertIn("codex", bare)
        self.assertNotIn("UNAVAILABLE", bare)



# ── THE SEAT'S OWN MEMORY CGROUP, ON THE OWNER'S ROSTER ─────────────────────
# A seat throttled by its own slice keeps its pane, beacon and presence dot,
# so every other cell on its roster row reads healthy — the owner found such a
# seat by its frozen pane. The server stamps seatceiling's cells on the row;
# these arms run the shipped memBadge over them and check renderRoster calls it.
MEM_HARNESS = """"use strict";
/*__INJECT__*/
const out = {};
out.throttled = memBadge({mem_pressure: "THROTTLED", mem_pressure_text: __LINE__});
out.near = memBadge({mem_pressure: "NEAR", mem_pressure_text: "NEAR now: x"});
out.calm = memBadge({seat: "seat-c"});
out.quiet = memBadge({mem_pressure: "HIGH",
                      mem_pressure_text: "HIGH, not pressing: x (cache)"});
out.unread = memBadge({mem_pressure: "HIGH-UNREAD", mem_shmem: null,
                       mem_pressure_text: "HIGH-UNREAD now: x; shmem unreadable"});
out.hostile = memBadge({mem_pressure: "THROTTLED",
                        mem_pressure_text: '"><img src=x onerror=1>'});
out.unknown = memBadge({mem_pressure: "UNKNOWN", mem_shmem: null,
                        mem_pressure_text: "UNKNOWN now: the seat-memory read raised RuntimeError"});
out.novel = memBadge({mem_pressure: '<b>NEW-WORD', mem_pressure_text: "x"});
console.log(JSON.stringify(out));
"""

MEM_LINE = ("THROTTLED now: agents-seat_under_test.slice at 13.35G of 12.88G "
            "memory.high (104%); 1 process stalled in the over-high throttle")


class MemBadgeRenderTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        if not cls.node:
            raise unittest.SkipTest("node not available")
        src = web_ui_loader.read_text()
        cls.render_roster = _lift(src, "renderRoster")
        lifted = [_lift(src, name) for name in ("esc", "memBadge")]
        assembled = MEM_HARNESS.replace("__LINE__", json.dumps(MEM_LINE)) \
            .replace("/*__INJECT__*/", "\n\n".join(lifted))
        cls.tmp = tempfile.mkdtemp(prefix="helm-mem-badge-")
        path = os.path.join(cls.tmp, "run.js")
        with open(path, "w", encoding="utf-8") as f:
            f.write(assembled)
        proc = subprocess.run([cls.node, path], capture_output=True,
                              text=True, timeout=60)
        assert proc.returncode == 0, "harness failed:\n" + proc.stderr
        cls.out = json.loads(proc.stdout)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(getattr(cls, "tmp", ""), ignore_errors=True)

    def test_a_throttled_seat_wears_the_red_badge_with_the_servers_line(self):
        b = self.out["throttled"]
        self.assertIn('class="rup dark"', b)
        self.assertIn(">MEM THROTTLED<", b)
        self.assertIn("13.35G of 12.88G memory.high (104%)", b)

    def test_near_is_calm_and_a_calm_slice_renders_nothing(self):
        self.assertIn('class="rup calm"', self.out["near"])
        self.assertIn(">MEM NEAR<", self.out["near"])
        self.assertEqual(self.out["calm"], "",
                         "a seat with no reading grew a badge")

    def test_a_page_cache_slice_wears_only_the_quiet_badge(self):
        b = self.out["quiet"]
        self.assertIn('class="rup calm"', b)
        self.assertIn(">MEM HIGH \u00b7 cache<", b)
        self.assertNotIn("THROTTLED", b.split(">", 1)[1])
        self.assertNotIn('class="rup dark"', b)

    def test_an_unreadable_shmem_says_so_and_never_claims_cache(self):
        b = self.out["unread"]
        self.assertIn('class="rup calm"', b)
        self.assertIn(">MEM HIGH \u00b7 shmem ?<", b)
        self.assertNotIn("cache<", b)
        self.assertIn(">MEM HIGH \u00b7 cache<", self.out["quiet"],
                      "control: the measured HIGH still says cache")

    def test_the_servers_line_is_escaped(self):
        b = self.out["hostile"]
        self.assertIn(">MEM THROTTLED<", b)
        self.assertNotIn("<img", b)

    def test_an_unread_seat_wears_UNKNOWN_never_the_empty_badge(self):
        """The empty badge is a calm seat's. A read that did not answer
        publishes UNKNOWN, and it must render as its own badge with the
        server's reason in the hover. The calm seat's empty badge is pinned
        by test_near_is_calm_and_a_calm_slice_renders_nothing."""
        b = self.out["unknown"]
        self.assertIn('class="rup unknown"', b)
        self.assertIn(">MEM UNKNOWN<", b)
        self.assertIn("read raised RuntimeError", b)
        self.assertNotIn('class="rup calm"', b)

    def test_a_word_the_badge_does_not_know_is_shown_not_dropped(self):
        b = self.out["novel"]
        self.assertIn('class="rup unknown"', b)
        self.assertIn(">MEM &lt;b&gt;NEW-WORD<", b)
        self.assertNotIn("<b>", b)

    def test_the_badge_is_wired_into_renderRoster(self):
        rr = self.render_roster
        self.assertIn('$("#rtbody").innerHTML', rr)
        self.assertIn("memBadge(s)", rr)


if __name__ == "__main__":
    unittest.main()
