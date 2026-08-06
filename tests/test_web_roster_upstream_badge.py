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

from helm import web_ui_loader

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
                            upstream_dark: true, upstream_family: "gemini"});
out.walled_horizon = upstreamBadge({upstream: "AUTH-UNAVAILABLE",
                            upstream_since: iso2h, upstream_dark: true,
                            upstream_resets_at_ms: FIXED + 3 * 3600 * 1000});
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
        assembled = HARNESS.replace(
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
        self.assertIn("resets in 3h", b)
        self.assertNotIn("no recorded reset horizon", b)

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
        """The codex-2 FIX round, blocker 2, render half. upstream_dark === false
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


if __name__ == "__main__":
    unittest.main()
