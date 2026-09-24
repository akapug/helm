#!/usr/bin/env python3
"""The burn-down console renderers, run for real — three sections, zero arms.

task/986. obdRender, obdAge, ocbRender and oubRender draw the owner's burn-down
console and NOTHING in the suite referenced them. Not hypothetical: building the
cured bucket I called ocbRender AFTER obdRender's unavailable early return, so an
owed source that could not answer would have silently blanked a cured bucket that
could. The Python independence arm asserts the ENDPOINT's payload keeps both
states separate, which it did the whole time — the defect lived downstream of
everything Python can see.

REVISED AFTER A REVIEW. Its four findings were one defect: that
version spliced the renderers and hand-wrote their whole environment, so it
tested my escaper against my selector over my auto-created elements. Now the
harness uses PRODUCTION's esc (spliced, not reimplemented), PRODUCTION's
selector semantics (null for an unknown id, so a renamed mount FAILS), the mount
ids the VIEW declares, and it drives obdInit rather than calling obdRender by
hand — so the fetch/generation/render wiring is what runs.

Requires node for the runtime half; SKIPPED (never failed) where node is absent.
"""
import json
import os
import re
import shutil
import subprocess
import unittest

from helm import web_ui_loader
from tests.test_web_chat_client_runtime import _extract_fn

HERE = os.path.dirname(os.path.abspath(__file__))
HARNESS = os.path.join(HERE, "burndown_runtime_harness.js")
# the burn-down sections moved onto the Work page with the rest of the work
# tab, so its mounts are declared in the Work page's part
VIEW = os.path.join(os.path.dirname(HERE), "helm", "web_ui", "views",
                    "00-home.html.part")

RENDERERS = ("obdAge", "ocbRender", "oubRender", "obdWarming", "obdRender",
             "obdFail", "obdInit")

# HELPERS THE RENDERERS CALL AND DO NOT OWN, spliced from the SAME assembled
# UI rather than reimplemented — the esc lesson, applied to the next helper
# that crossed a file boundary. `lrAgo` is the page's one server-age
# formatter; a copy here could print "0s ago" for an unknown age while
# production printed "unknown", and the arm would be about the copy.
# `lrDur` rides with it: `lrAgo` is the DIRECTION ("ago"), `lrDur` the
# MAGNITUDE, split so the building band's "left" cannot round differently
# from this card's "ago". Lifting one without the other is a ReferenceError
# the moment an age is actually formatted.
HELPERS = ("lrDur", "lrAgo")


def _declared_ids():
    """The mount ids the VIEW declares — the harness's whole DOM.

    Read from the markup rather than listed here, so the fake cannot drift into
    containing an element production does not have."""
    with open(VIEW, encoding="utf-8") as fh:
        # CASE-SENSITIVE AND CAMEL-CASE AWARE. The first version matched
        # `[a-z][a-z0-9]*` only, so every camelCase mount — obdReload among
        # them — was silently absent from the fake DOM. The claim "the DOM
        # holds exactly the ids the view declares" was FALSE for that whole
        # class, and no arm asked for one until the top-level wiring was
        # spliced and production's own $("#obdReload") came back null.
        return sorted(set(re.findall(r'id="([A-Za-z][A-Za-z0-9_-]*)"',
                                     fh.read())))


def _wiring_lines(src):
    """The top-level lines that are the ONLY callers of obdInit.

    Nothing else invokes it: the reload button and the 45s interval are the
    entire entry path. A harness that splices only FUNCTIONS can never fail on
    their removal, which is exactly the mutation a probe measured surviving."""
    want = [ln for ln in src.splitlines()
            if ln.startswith("let OBD_GEN")
            or ln.startswith("$(\"#obdReload\")")
            or ln.startswith("setInterval(obdInit")]
    # OBD_GEN's DECLARATION is production's too, and splicing it rather than
    # declaring one here is the same rule as splicing esc: a counter the
    # harness owns is a counter the harness can keep working after production
    # stops having one.
    if len(want) != 3:
        raise AssertionError(
            "expected OBD_GEN + reload + interval top-level, found %d: %r"
            % (len(want), want))
    return "\n".join(want)


def _core_line(src, name):
    """One verbatim `const NAME = ...;` line out of the assembled UI.

    esc is an arrow const, not a function declaration, so _extract_fn cannot
    lift it — and REIMPLEMENTING it is exactly the finding this cures: the old
    harness's copy dropped production's apostrophe escaping."""
    for line in src.splitlines():
        if line.strip().startswith("const %s =" % name):
            return line
    raise AssertionError("core helper not found in assembled web UI: " + name)


class TestBurnDownSource(unittest.TestCase):
    """Node-free contracts, so they hold where the runtime harness skips."""

    def test_the_OTHER_BUCKETS_RENDER_ABOVE_the_owed_guard(self):
        """THE DEFECT THIS ROW EXISTS FOR, asserted on ORDERING. Both calls
        below the early return would still grep as present while never running
        whenever the owed half cannot see."""
        body = _extract_fn(web_ui_loader.read_text(), "obdRender")
        guard = body.index("if (!d || d.unavailable)")
        for name in ("ocbRender(", "oubRender("):
            self.assertIn(name, body)
            self.assertLess(
                body.index(name), guard,
                "%s is called BELOW obdRender's unavailable guard, so an owed "
                "source that cannot answer silently blanks a section that can"
                % name)

    def test_each_bucket_renderer_gets_ITS_OWN_bucket(self):
        """With the undeclared bucket seeded but never asserted,
        oubRender(d && d.cured) — the WRONG bucket — passed every arm. This
        pins the ARGUMENT at the call site; the runtime arm below pins the
        rendered effect."""
        body = _extract_fn(web_ui_loader.read_text(), "obdRender")
        self.assertIn("ocbRender(d && d.cured, ageS)", body)
        self.assertIn("oubRender(d && d.undeclared, ageS)", body)

    def test_CANNOT_SEE_never_renders_as_a_COUNT(self):
        """'0 owed' for a scan that never ran is the false all-clear the
        three-state split exists to refuse."""
        body = _extract_fn(web_ui_loader.read_text(), "obdRender")
        guard = body.index("if (!d || d.unavailable)")
        ret = body.index("return;", guard)
        self.assertNotIn(" owed", body[guard:ret],
                         "the cannot-see branch prints a count")

    def test_obdInit_SPLITS_THE_FETCH_FROM_THE_RENDER(self):
        """task/1033, asserted on STRUCTURE, because the discrimination is a
        property of the block layout and not of the strings.

        One try around both calls cannot tell the kinds apart AFTER the fact:
        by the time the catch runs, a TypeError from a renamed mount and a
        timeout from a dead node are both just an Error carrying a message.
        Two blocks make the kind a fact about WHICH CATCH RAN."""
        body = _extract_fn(web_ui_loader.read_text(), "obdInit")
        self.assertEqual(
            body.count("try {"), 2,
            "obdInit wraps its fetch and its render in ONE try block again, "
            "so a broken console reports itself as a broken fleet")
        self.assertEqual(body.count("catch ("), 2)
        self.assertIn('obdFail("transport"', body)
        self.assertIn('obdFail("defect"', body)
        fetch, render = body.index("try {"), body.rindex("try {")
        self.assertLess(body.index("/api/owed"), render,
                        "the fetch is inside the RENDER's try block, so a "
                        "transport failure is reported as a page defect")
        self.assertGreater(body.index("obdRender("), render,
                           "the render is not inside the second try block")
        self.assertLess(fetch, render)

    def test_the_FAILURE_REPORTER_NEVER_WRITES_THROUGH_AN_UNCHECKED_MOUNT(self):
        """The reporter's own mount is a candidate for the missing one. The
        old catch wrote `$("#obdmeta").textContent` blind, so the case it
        existed to report threw a second TypeError from inside itself and
        destroyed the original cause."""
        body = _extract_fn(web_ui_loader.read_text(), "obdFail")
        for blind in ('$("#obdmeta").', '$("#obdlist").'):
            self.assertNotIn(
                blind, body,
                "obdFail writes through %s without checking it, so a missing "
                "reporter mount throws from inside the failure handler" % blind)
        self.assertIn("if (meta)", body)
        self.assertIn("if (list)", body)
        self.assertIn("console.error", body,
                      "the defect is not reported to the developer at all")

    def test_WARMING_IS_CHECKED_BEFORE_THE_CANNOT_SEE_GUARD(self):
        """ORDERING IS THE PROPERTY, exactly as it is for the sibling buckets
        two arms up. A warming body carries no `unavailable` key, so a check
        placed BELOW the guard is reached only after `!d || d.unavailable` has
        already drawn "✗ CANNOT SEE the burn-down" — the false alarm this
        branch exists to stop, still printed, with the correct branch sitting
        unreachable underneath it."""
        body = _extract_fn(web_ui_loader.read_text(), "obdRender")
        # UNCONDITIONAL POSITIVE CONTROLS on the three observables the
        # orderings below compare, written OUT rather than looped: an ordering
        # assertion over text that is not there cannot discriminate, and
        # `.index` raising is a crash rather than a stated claim.
        self.assertIn("d.warming", body)
        self.assertIn("if (!d || d.unavailable)", body)
        self.assertIn("ocbRender(", body)
        warming = body.index("d.warming")
        self.assertLess(
            warming, body.index("if (!d || d.unavailable)"),
            "the warming check sits BELOW the cannot-see guard, so a cold "
            "burn-down is reported as an unreadable ledger")
        self.assertLess(warming, body.index("ocbRender("),
                        "the sibling buckets render before the warming check, "
                        "so a warming body draws their cannot-see lines")

    def test_every_renderer_and_mount_this_module_covers_still_EXISTS(self):
        """THE MUST-HIT. A rename would otherwise leave this module passing on
        whatever it still found."""
        src = web_ui_loader.read_text()
        # THE LOOPS BELOW ARE ONLY AS GOOD AS THEIR POPULATIONS. An empty
        # RENDERERS or an empty id list asserts nothing at all, so both are
        # pinned non-empty out here where no iteration count can excuse it.
        self.assertTrue(src, "the assembled UI is empty")
        self.assertTrue(RENDERERS + HELPERS)
        self.assertIn("function obdRender", src)
        for name in RENDERERS + HELPERS:
            self.assertIn("function " + name, src, "renderer %s gone" % name)
        ids = _declared_ids()
        self.assertTrue(ids, "the view declares no mounts at all")
        self.assertIn("obdlist", ids)
        for need in ("obdlist", "obdmeta", "ocblist", "ocbmeta",
                     "oublist", "oubmeta"):
            self.assertIn(need, ids, "the view no longer declares #" + need)


class BurnDownRuntimeBase(unittest.TestCase):
    """setUpClass lifts the burn-down renderers, their helpers and the
    top-level wiring out of the web UI verbatim, runs them under node once
    per class, and keeps the result in `cls.out`. Requires node; skipped
    (not failed) where node is unavailable.

    THIS CLASS HOLDS NO `test_*` METHOD, and that is its contract. unittest
    collects every inherited `test_*` again under each subclass's own id, so
    an arm written here runs once per subclass. A class that needs this
    fixture subclasses THIS class; its arms go in the subclass."""

    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        if not cls.node:
            raise unittest.SkipTest("node not available")
        src = web_ui_loader.read_text()
        with open(HARNESS, encoding="utf-8") as fh:
            template = fh.read()
        fns = "\n\n".join(_extract_fn(src, n)
                          for n in RENDERERS + HELPERS)
        # The marker carries a valid empty-array default so the harness parses
        # standalone; the substitution REPLACES that default rather than
        # prefixing it, which would leave `[...][]` and a syntax error.
        script = template.replace("/*__IDS__*/[]",
                                  json.dumps(_declared_ids()))
        script = script.replace("/*__CORE__*/", _core_line(src, "esc"))
        script = script.replace("/*__INJECT__*/", fns)
        # THE TOP-LEVEL WIRING, SPLICED VERBATIM. Asserting
        # these lines EXIST is a string check a no-op survives. Splicing them
        # means the harness RUNS what production installs, so a dropped
        # interval or a neutered onclick fails on behaviour instead.
        script = script.replace("/*__WIRING__*/", _wiring_lines(src))
        cls.script = script
        p = subprocess.run([cls.node, "-e", script],
                           capture_output=True, text=True, timeout=60)
        if p.returncode != 0:
            raise AssertionError("harness failed rc=%s\n%s"
                                 % (p.returncode, p.stderr[-2000:]))
        cls.out = json.loads(p.stdout)


class TestBurnDownRuntime(BurnDownRuntimeBase):
    """The renderers RUN, in production's environment.

    A subclass of THIS class runs every arm below again under its own id,
    which is right only for one that changes the fixture those arms read. A
    class that only needs the fixture subclasses BurnDownRuntimeBase."""

    def test_PRODUCTION_esc_is_used_not_a_copy_of_it(self):
        """The old harness reimplemented esc and dropped the
        apostrophe. Lane names carry apostrophes; that is the input class the
        ledger actually supplies, so the arm asserts the production entity."""
        e = self.out["esc"]
        self.assertEqual(e["apostrophe"], "lane/o&#39;brien")
        self.assertEqual(e["angle"], "&lt;script&gt;")
        self.assertEqual(e["amp"], "a&amp;b")
        self.assertEqual(e["quote"], "say &quot;hi&quot;")
        self.assertEqual(e["nullish"], "")

    def test_an_UNDATED_row_is_UNKNOWN_never_fresh(self):
        age = self.out["age"]
        for case in ("missing", "empty", "unparseable", "future"):
            self.assertEqual(age[case]["label"], "?", case)
            self.assertEqual(age[case]["days"], -1, case)
        self.assertEqual(age["today"]["label"], "today")
        self.assertEqual(age["three_days"]["label"], "3d")

    def test_A_BLIND_OWED_HALF_DOES_NOT_BLANK_THE_OTHERS(self):
        """THE COUPLING, driven through obdInit so the production wiring runs.

        COUNTERFACTUAL MEASURED: moving the ocbRender call below the early
        return leaves ocbmeta at its initial empty string, so the presence of
        the rendered text is what discriminates."""
        dom = self.out["owed_blind"]
        self.assertEqual(dom["obdmeta"]["text"], "UNKNOWN")
        self.assertIn("CANNOT SEE", dom["obdlist"]["html"])
        self.assertIn("2 cured, nobody waiting", dom["ocbmeta"]["text"])
        self.assertIn("lane/one", dom["ocblist"]["html"])

    def test_THE_UNDECLARED_BUCKET_RENDERS_ITS_OWN_DATA(self):
        """The arm the finding demanded. The fixtures differ in total
        AND in lane text, so handing oubRender the cured bucket shows up as a
        wrong number and a wrong lane rather than as nothing at all."""
        dom = self.out["owed_blind"]
        self.assertIn("oubmeta", dom, "the undeclared renderer never ran")
        self.assertIn("4", dom["oubmeta"]["text"])
        self.assertIn("lane/undeclared-a", dom["oublist"]["html"])
        self.assertNotIn("lane/one", dom["oublist"]["html"],
                         "the undeclared section rendered the CURED bucket")

    def test_the_cannot_see_meta_prints_no_count(self):
        self.assertNotIn(" owed", self.out["owed_blind"]["obdmeta"]["text"])

    def test_THE_SEAT_RENDERS_rather_than_falling_back_to_unassigned(self):
        html = self.out["owed_blind"]["ocblist"]["html"]
        self.assertIn("seat-one", html)
        self.assertNotIn("unassigned", html)

    def test_the_burn_down_is_sorted_OLDEST_FIRST_with_undated_LAST(self):
        html = self.out["populated"]["obdlist"]["html"]
        old, recent, undated = (html.index("lane/old"), html.index("lane/recent"),
                                html.index("lane/undated"))
        self.assertLess(old, recent, "not oldest-first")
        self.assertLess(recent, undated, "the undated row did not sort LAST")

    def test_INDEPENDENCE_HOLDS_THE_OTHER_WAY_TOO(self):
        dom = self.out["cured_blind"]
        self.assertEqual(dom["ocbmeta"]["text"], "UNKNOWN")
        self.assertIn("git could not answer", dom["ocblist"]["html"])
        self.assertIn("1 owed", dom["obdmeta"]["text"])

    def test_A_FAILED_FETCH_IS_ITS_OWN_UNKNOWN(self):
        """obdInit's catch arm, which NO direct obdRender call can reach —
        The third finding: without executing obdInit, removing its
        render call or breaking its error path leaves every other arm green."""
        dom = self.out["fetch_failed"]
        self.assertEqual(dom["obdmeta"]["text"], "UNKNOWN")
        self.assertIn("unreadable", dom["obdlist"]["html"])
        self.assertIn("network is down", dom["obdlist"]["html"])


class TestBurnDownServedStale(BurnDownRuntimeBase):
    """THE CONSOLE HALF of the serve-stale cure, sharing the parent's run.

    /api/owed costs a full fold of the dispatch ledger — about a minute on the
    owner's box — against a card that fetches with an eight second deadline
    every forty-five seconds, so the burn-down ALWAYS printed a timeout. It is
    served through the same serve-stale cache /api/lr uses, which introduces
    two states this console had never drawn: a COLD body that says `warming`,
    and a WARM body that may be minutes old and says how old.
    """

    def test_A_WARMING_BODY_IS_NOT_DRAWN_AS_AN_UNREADABLE_LEDGER(self):
        """WARMING IS A FACT ABOUT A CACHE; CANNOT-SEE IS A CLAIM ABOUT THE
        RECORD. Before this branch the warming body fell into `!d ||
        d.unavailable` and the owner met "✗ CANNOT SEE the burn-down" on every
        view after a restart, about a ledger that was perfectly readable.

        COUNTERFACTUAL: deleting the warming guard from obdRender turns this
        arm RED on the meta ("UNKNOWN") and on both list assertions.
        """
        dom = self.out["warming"]
        self.assertNotEqual(dom["obdmeta"]["text"], "UNKNOWN",
                            "a burn-down nobody has computed yet is reported "
                            "as one nobody can READ")
        self.assertIn("reading", dom["obdmeta"]["text"])
        self.assertNotIn("CANNOT SEE", dom["obdlist"]["html"])
        self.assertIn("READ", dom["obdlist"]["html"],
                      "the warming line does not say a read is under way")

    def test_WARMING_DRAWS_EVERY_SECTION_not_only_the_burn_down(self):
        """ONE FETCH FEEDS THREE SECTIONS, so a warming body leaves the other
        two with no bucket at all — and their own renderers say "✗ CANNOT SEE"
        about that absence. Two false alarms beside the cured one."""
        dom = self.out["warming"]
        # UNCONDITIONAL POSITIVE CONTROL on the same observables: both sections
        # were DRAWN. A renderer that wrote nothing at all satisfies every
        # absence claim in the loop below.
        self.assertTrue(dom["ocblist"]["html"], "the cured section drew nothing")
        self.assertTrue(dom["oublist"]["html"],
                        "the undeclared section drew nothing")
        for meta, lst, what in (("ocbmeta", "ocblist", "cured"),
                                ("oubmeta", "oublist", "undeclared")):
            self.assertNotEqual(dom[meta]["text"], "UNKNOWN", what)
            self.assertNotIn("CANNOT SEE", dom[lst]["html"], what)
            self.assertIn("READ", dom[lst]["html"], what)

    def test_A_STALE_BODY_SAYS_HOW_OLD_IT_IS_on_every_section(self):
        """THE CACHE SERVES A PREVIOUS BODY WHILE IT REBUILDS, so a list on
        screen can be minutes old. A count printed with no age passes as
        current, which is the one thing a serve-stale surface may never do.

        372s renders through production's lrAgo as "6m ago" — deliberately not
        a round number and not seconds, so a stamp that fabricated a zero or
        echoed the raw field cannot imitate it.
        """
        dom = self.out["aged"]
        for meta in ("obdmeta", "ocbmeta", "oubmeta"):
            self.assertIn("read 6m ago", dom[meta]["text"],
                          "%s prints no age, so a stale burn-down passes as "
                          "current: %r" % (meta, dom[meta]["text"]))
        # THE POSITIVE CONTROL ON THE SAME OBSERVABLE: the counts are still
        # there, so the age did not replace the card's content.
        self.assertIn("1 owed", dom["obdmeta"]["text"])
        self.assertIn("2 cured", dom["ocbmeta"]["text"])

    def test_A_BODY_WITH_NO_AGE_SAYS_UNKNOWN_never_zero_seconds(self):
        """An older server, or any path that could not date what it served.
        "0s ago" about a body nothing can date is the worst available lie on a
        freshness line — lrAgo's own law, reached through this card."""
        dom = self.out["ageless"]
        aged = self.out["aged"]
        # THE CONTROL IS THE OTHER FIXTURE ON THE SAME CELLS. A card that
        # printed no age at all would satisfy "not 0s ago" everywhere; these
        # three say the same cells DO print a real age when the body carries
        # one, so "unknown" below is discrimination and not silence.
        self.assertIn("read 6m ago", aged["obdmeta"]["text"])
        self.assertIn("read 6m ago", aged["ocbmeta"]["text"])
        self.assertIn("read 6m ago", aged["oubmeta"]["text"])
        self.assertIn("read unknown", dom["obdmeta"]["text"])
        self.assertIn("read unknown", dom["ocbmeta"]["text"])
        self.assertIn("read unknown", dom["oubmeta"]["text"])
        self.assertNotIn("0s ago", dom["obdmeta"]["text"])
        self.assertNotIn("0s ago", dom["ocbmeta"]["text"])
        self.assertNotIn("0s ago", dom["oubmeta"]["text"])


class TestBurnDownFailureIsTyped(BurnDownRuntimeBase):
    """task/1033 — A BROKEN CONSOLE MUST NOT READ AS A DEAD NETWORK.

    obdInit used to wrap its fetch AND its render in one try/catch, and every
    path out of that catch printed "✗ burn-down unreadable: <message>". So a
    renamed mount id — production's $() returns null, the next textContent
    throws TypeError — put the SAME sentence on the owner's console as an
    unreachable server. What he concluded from it was "the fleet cannot answer
    right now; wait and reload", and that conclusion was unreachable: the data
    had arrived intact and this page's own code could not draw it. Waiting
    fixes a network. Nothing fixes a rename except an edit.

    These arms share the parent's single spliced run.
    """

    def test_A_RENDER_DEFECT_DOES_NOT_READ_AS_A_DEAD_NETWORK(self):
        """THE DISCRIMINATION ITSELF, stated as the reader's conclusion.

        COUNTERFACTUAL MEASURED: restoring the single try/catch (both calls in
        one block, one "burn-down unreadable" line) turns this arm RED on the
        meta equality AND on both kind words — the collapsed version prints
        "UNKNOWN" for a page defect and never says which layer failed.
        """
        defect = self.out["render_defect"]["dom"]
        transport = self.out["fetch_failed"]

        self.assertTrue(defect["obdlist"]["html"],
                        "the render defect drew NOTHING — the console kept "
                        "its stale text and said nothing about why")
        self.assertTrue(transport["obdlist"]["html"])

        self.assertNotEqual(
            defect["obdmeta"]["text"], transport["obdmeta"]["text"],
            "a renamed mount and a dead network print the SAME meta, so the "
            "owner's glance cannot tell a broken page from a silent fleet")

        self.assertIn("THIS PAGE", defect["obdlist"]["html"],
                      "the defect line does not say the failure is OURS")
        self.assertNotIn("FETCH FAILED", defect["obdlist"]["html"],
                         "a page defect is reported as a failed fetch, "
                         "sending the reader at the network layer")
        self.assertIn("FETCH FAILED", transport["obdlist"]["html"],
                      "the transport line does not say the fetch failed")
        self.assertNotIn("THIS PAGE", transport["obdlist"]["html"],
                         "a dead network is reported as a page defect, "
                         "sending the reader at the markup layer")

    def test_NEITHER_KIND_LOSES_ITS_UNDERLYING_MESSAGE(self):
        """Naming the layer is worth nothing if the cause is dropped to do
        it. Both surfaces still carry the throw that produced them."""
        transport = self.out["fetch_failed"]["obdlist"]["html"]
        defect = self.out["render_defect"]["dom"]["obdlist"]["html"]
        self.assertIn("network is down", transport,
                      "the transport line dropped the throw that caused it")
        self.assertIn("Cannot set properties of null", defect,
                      "the defect line dropped the throw that caused it")

    def test_THE_DEFECT_LINE_NAMES_THE_MOUNT_THAT_IS_MISSING(self):
        """"Cannot set properties of null" identifies nothing. The ids this
        console needs and cannot find identify the edit that broke it — and
        the throwing renderer is a SIBLING section's, above the owed guard by
        design, so the burn-down half reports a break it did not cause."""
        html = self.out["render_defect"]["dom"]["obdlist"]["html"]
        self.assertIn("ocbmeta", html,
                      "the defect line does not name the missing mount, so "
                      "the reader learns only that something was null")
        self.assertNotIn("obdlist", html,
                         "a mount that is PRESENT is named as missing")

    def test_THE_REPORTERS_OWN_MISSING_MOUNT_DOES_NOT_DESTROY_THE_CAUSE(self):
        """THE WORST CASE, and the one the old code handled worst. When
        #obdmeta is itself the renamed id, the old catch wrote through
        $("#obdmeta") unguarded: a SECOND TypeError, thrown from inside the
        catch, replacing the original cause and escaping obdInit entirely.
        The owner saw no message at all — a silently stale panel — and the
        console carried a null-property error naming nothing.

        COUNTERFACTUAL MEASURED: with the unguarded writes restored, `escaped`
        is a TypeError and obdlist is empty, so this arm goes RED twice.
        """
        case = self.out["defect_at_the_reporter"]
        self.assertNotIn("obdmeta", case["dom"],
                         "the fixture did not actually remove the mount")
        self.assertIsNone(
            case["escaped"],
            "the failure reporter threw its own TypeError out of obdInit; the "
            "browser logs an unhandled rejection and the panel stays STALE")
        html = case["dom"]["obdlist"]["html"]
        self.assertTrue(html, "nothing was reported ANYWHERE on the surface")
        self.assertIn("Cannot set properties of null", html,
                      "the original cause was lost")
        self.assertIn("obdmeta", html, "the missing mount is not named")

    def test_NEITHER_FAILURE_IS_SWALLOWED_FROM_THE_DEVELOPER(self):
        """The owner's line is one sentence; a page defect's actionable part
        is its STACK. Both kinds reach console.error, and they reach it with
        the Error OBJECT — flattening it to text drops the stack, and for the
        reporter's-own-mount case the console is the ONLY report there is."""
        transport = self.out["fetch_failed_errors"]
        defect = self.out["render_defect"]["errors"]
        blind = self.out["defect_at_the_reporter"]["errors"]
        # THE UNCONDITIONAL CONTROLS. Every discriminating assertion below
        # lives inside the loop, and a loop over an empty sequence asserts
        # nothing at all — so each observable is pinned as non-empty out here,
        # where no iteration count can excuse it.
        self.assertTrue(transport, "the failed fetch told the console nothing")
        self.assertTrue(defect, "the render defect told the console nothing")
        self.assertTrue(blind, "the reporter's own broken mount told the "
                               "console nothing, and it is the ONLY report "
                               "that case can produce")
        cases = (("fetch_failed", transport, "transport"),
                 ("render_defect", defect, "defect"),
                 ("defect_at_the_reporter", blind, "defect"))
        for name, errs, kind in cases:
            self.assertTrue(errs, "%s reported nothing to the console" % name)
            self.assertTrue(
                any(e["error"] for e in errs),
                "%s logged only text, so the stack is gone" % name)
            joined = " ".join(e["text"] for e in errs)
            self.assertIn("burn-down", joined, name)
            self.assertIn(kind, joined,
                          "%s does not name its KIND in the console either, so "
                          "the developer reads the same ambiguity" % name)


if __name__ == "__main__":
    unittest.main()


class TestBurnDownMutationsCodex3Named(unittest.TestCase):
    """The five mutations a probe measured as SURVIVING at 85da4dc57.

    Their FIX was exact: "still green if served fragment is absent, top-level
    init/reload is removed, fetch URL is wrong, OBD_GEN guards are removed, or
    the undated cured row vanishes." Each arm below is named for the mutation
    it kills, because an arm whose target is not stated drifts off it.
    """

    @classmethod
    def setUpClass(cls):
        cls.src = web_ui_loader.read_text()

    def test_the_WORK_VIEW_FRAGMENT_IS_ACTUALLY_SERVED(self):
        """Every other arm reaches the renderers through the assembled UI, so
        all of them pass on a build that assembles the SCRIPT and drops the
        VIEW. The console would render nothing into elements that do not
        exist."""
        for mount in ('id="obdlist"', 'id="ocblist"', 'id="oublist"'):
            self.assertIn(mount, self.src,
                          "the work view fragment is not in the served UI: "
                          "%s is missing, so the renderers write into nothing"
                          % mount)

    def test_the_TOP_LEVEL_INIT_AND_RELOAD_WIRING_SURVIVES(self):
        """obdInit is only ever CALLED by top-level wiring the harness cannot
        splice: the reload button and the interval. Delete both and every
        runtime arm still passes while the console never populates."""
        self.assertIn("obdInit()", self.src)
        self.assertRegex(self.src, r'\$\("#obdReload"\)\.onclick',
                         "the reload control is unwired")
        self.assertRegex(self.src, r"setInterval\(\s*obdInit",
                         "the refresh interval is gone, so the console only "
                         "ever shows its first read")

    def test_the_OBD_GEN_GUARDS_BOTH_SURVIVE(self):
        """Two generation checks, and they are why a slow first response cannot
        overwrite a fast second one. Removing either leaves every arm green
        because the harness resolves its stub immediately — the race the guard
        exists for cannot happen here."""
        body = _extract_fn(self.src, "obdInit")
        self.assertEqual(body.count("gen !== OBD_GEN"), 2,
                         "obdInit no longer has BOTH generation guards, so a "
                         "stale in-flight response can overwrite a newer one")
        self.assertIn("++OBD_GEN", body)


class TestBurnDownRuntimeMutations(BurnDownRuntimeBase):
    """Runtime halves of the same five, sharing the parent's spliced run."""

    def test_obdInit_FETCHES_THE_OWED_ENDPOINT(self):
        """The stub answered any URL, so a wrong endpoint passed everything."""
        fetched = self.out.get("fetched") or []
        self.assertTrue(fetched, "obdInit never called the fetch helper")
        self.assertEqual(fetched[0]["url"], "/api/owed",
                         "obdInit fetches the wrong endpoint: %r"
                         % fetched[0]["url"])

    def test_the_UNDATED_CURED_ROW_STILL_RENDERS(self):
        """The cured fixture's second row carries since=null on purpose. If it
        vanishes, the only remaining cured card is dated and obdAge's unknown
        branch stops being exercised through the cured path at all."""
        html = self.out["owed_blind"]["ocblist"]["html"]
        self.assertIn("lane/two", html, "the undated cured row is gone")
        self.assertIn("no timestamp on this row", html,
                      "the undated cured row lost its unknown-age title")
        self.assertIn("2 cured", self.out["owed_blind"]["ocbmeta"]["text"])


class TestBurnDownWiringExecutes(BurnDownRuntimeBase):
    """The last three mutations that kept their SOURCE TEXT.

    Their FIX: "still green if initial/reload obdInit wiring breaks, timeout
    changes from 8000, or both generation guards become no-ops retaining their
    source strings." All three survive a STRING assertion by construction — a
    no-op keeps the text. So these arms run the behaviour instead.

    That is the shape lesson from the rounds on this lane: this harness can
    only fail on what it SPLICES, and I had been asserting strings about
    everything it did not. The wiring is now spliced and fired.
    """

    def test_THE_RELOAD_BUTTON_ACTUALLY_CALLS_obdInit(self):
        """A neutered onclick keeps its source line and stops the console from
        ever refreshing on demand."""
        self.assertEqual(self.out.get("reload_fetched"), 1,
                         "clicking reload did not reach the endpoint — the "
                         "wiring is present in source and dead in behaviour")

    def test_THE_INTERVAL_ACTUALLY_CALLS_obdInit(self):
        """setInterval(obdInit, 45000) is the console's ONLY self-refresh. A
        dropped or rewired interval leaves a page that is correct once."""
        timers = self.out.get("timers") or []
        self.assertTrue(timers, "no interval was installed at all")
        self.assertEqual(timers[0]["ms"], 45000,
                         "the refresh cadence changed: %r" % timers[0])
        self.assertEqual(self.out.get("interval_fetched"), 1,
                         "the installed interval does not call obdInit")

    def test_THE_FETCH_TIMEOUT_IS_PINNED(self):
        """The URL was asserted and the timeout was not, so 8000 ->
        anything passed. A short timeout turns a slow-but-healthy ledger into
        'burn-down unreadable'; a long one hangs the panel.

        IT WAS 8000 AND THAT WAS TOO SHORT TO CARRY A COLD READ. Measured:
        the first `_owed_build` in a fresh process costs 8.9-10.2s, and the
        server waits `_OWED_COLD_WAIT_S` for it — so an 8s deadline aborted
        that wait before either the body OR the `warming` placeholder could
        land, and the first view after every restart printed the timeout over
        a build that was about to succeed. 20000 clears the wait with six
        seconds of response budget. The two numbers are ONE decision living in
        two files: `test_THE_COLD_WAIT_FITS_INSIDE_THE_CARDS_FETCH_DEADLINE`
        in tests/test_web_owed.py keeps them in step, and this arm holds the
        literal so neither can move by accident.
        """
        fetched = self.out.get("fetched") or []
        self.assertEqual(fetched[0]["ms"], 20000,
                         "the fetch timeout changed: %r" % fetched[0])

    def test_A_STALE_IN_FLIGHT_READ_NEVER_OVERWRITES_A_NEWER_ONE(self):
        """THE GENERATION RACE, RUN RATHER THAN COUNTED.

        Counting `gen !== OBD_GEN` in the source passes on a no-op that keeps
        the string — which is precisely the mutation a review named. This
        starts a SLOW read, starts a FAST one, lets the fast one land, then
        releases the slow one and asserts the stale answer never appears.
        Without both guards the console would show 111 owed after 222 arrived.
        """
        meta = self.out.get("race_meta") or ""
        self.assertIn("222", meta,
                      "the newer read did not land: %r" % meta)
        self.assertNotIn("111", meta,
                         "a STALE in-flight response overwrote a newer one — "
                         "the generation guards are no longer effective: %r"
                         % meta)
