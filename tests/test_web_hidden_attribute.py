#!/usr/bin/env python3
"""THE `hidden` ATTRIBUTE MEANS HIDDEN, MEASURED IN A REAL ENGINE.

The UA stylesheet hides `[hidden]` with an ATTRIBUTE selector, so any author
rule carrying an id outranks it on specificity. When that happens the element
stays painted while every script that reads it -- `el.hidden`,
`hasAttribute("hidden")` -- correctly reports it hidden. Nothing in the JS is
wrong and nothing in the CSS is wrong on its own; only the pair is, so no
assertion over either source alone can hold it.

IT HAS HAPPENED FOUR TIMES HERE and was cured three times only where it was
found -- `#lrsec [hidden]`, `#declform[hidden]`, `#declfill[hidden]`. The
fourth reached the owner: the stale-build offer announced "helm has changed
since you opened this page" on a page that was current, survived every reload,
and its "not now" button set `bar.hidden = true` to no visible effect, because
`#stalebuild{display:flex}` had already won. Measured in the browser at the
time: `hidden === true`, computed `display: "flex"`, 52px tall.

So the property is asserted where it lives -- over the shipped markup under the
shipped stylesheet, in Chrome. The source arm below is a cheap companion that
runs everywhere; it is NOT the guarantee, because a string cannot resolve a
cascade. Where Chrome is absent the browser arm SKIPS, and a skipped arm proves
nothing -- which is why it carries its own MUST-HIT: the same page is measured
a second time with the cure deleted, and the arm fails unless that second
measurement shows the defect. An instrument that cannot see the defect cannot
witness its absence.
"""
import json
import os
import re
import shutil
import subprocess
import unittest

from helm import web_ui_loader

CHROME = ("google-chrome", "google-chrome-stable", "chromium",
          "chromium-browser")
# The one line that carries the guarantee. Deleting it is what the MUST-HIT
# arm does on purpose, so it is spelled once, here, and used by both arms.
RULE = "[hidden]{display:none !important}"


def _chrome():
    for name in CHROME:
        path = shutil.which(name)
        if path:
            return path
    return None


class TheGlobalRuleIsShippedTest(unittest.TestCase):
    """The rule is IN the assembled page. Cheap, total, and not the guarantee."""

    def test_rule_is_present(self):
        self.assertIn(
            RULE, web_ui_loader.read_text(),
            "the global `hidden` rule is not in the assembled page: every "
            "element shipping the attribute is now one id selector away from "
            "being painted while it reports itself hidden")

    def _outranking_rules(self, src):
        """Author rules that are `!important` AND set a non-none display.

        Lifted out of the arm so the arm can point it at a source it KNOWS is
        dirty — the only way an empty result over the real source is evidence
        rather than an instrument that stopped matching.
        """
        offenders = []
        for match in re.finditer(r"([^{}]+)\{([^{}]*)\}", src):
            selector, body = match.group(1).strip(), match.group(2)
            found = re.search(r"(?:^|;)\s*display\s*:\s*([^;]+)", body)
            if not found or "!important" not in found.group(1):
                continue
            value = found.group(1).split("!")[0].strip()
            if value != "none" and "[hidden]" not in selector:
                offenders.append("%s { display: %s !important }"
                                 % (selector.splitlines()[-1].strip(), value))
        return offenders

    def test_no_author_rule_outranks_it(self):  # noqa: VACUOUS_ASSERTION — the control is unconditional and on this observable, but reaches it through `_outranking_rules`, and the rung credits a control only from channels of the SAME call
        """`!important` beats specificity but NOT another `!important`.

        The global rule makes the attribute win against an author rule of any
        specificity — except one that is itself `!important` and sets a
        display. That would be a silent, total defeat of the guarantee, so it
        is refused here rather than discovered in a screenshot.

        POSITIVE CONTROL FIRST, unconditionally and on the SAME observable:
        the identical scan is run over the real page with one such rule spliced
        in, and must report exactly that rule. Only then does an empty result
        over the untouched page mean the page is clean.
        """
        src = web_ui_loader.read_text()
        poison = "#helmpoison{display:flex !important}"
        caught = self._outranking_rules(src + "\n" + poison)
        self.assertIn(
            "#helmpoison { display: flex !important }", caught,
            "the scan did not find a rule deliberately spliced into the page, "
            "so it cannot witness the absence of any other; it reported %r"
            % (caught,))
        self.assertEqual(
            [], self._outranking_rules(src),
            "these author rules are `!important` and set a non-none display, "
            "so they outrank the global `hidden` rule for anything they "
            "match: %s" % self._outranking_rules(src))


class HiddenElementsAreNotPaintedTest(unittest.TestCase):
    """EVERY element shipping `hidden` computes to `display:none` in Chrome."""

    @classmethod
    def setUpClass(cls):
        cls.chrome = _chrome()
        if not cls.chrome:
            raise unittest.SkipTest("no chrome/chromium available")
        cls.src = web_ui_loader.read_text()
        cls.tmp = os.environ.get("TMPDIR") or "/tmp"

    def leaking(self, page, name):
        """Ids that report themselves hidden and are painted anyway.

        The probe writes into a <pre> the page did not have, which is what
        `--dump-dom` carries back out. A probe that throws writes nothing and
        this fails on the missing element -- an absent measurement must never
        read as a measurement of zero.
        """
        probe = """<script>
const all = document.querySelectorAll('[hidden]');
const out = [];
for (const el of all) {
  if (getComputedStyle(el).display !== 'none') out.push(el.id || el.tagName);
}
const pre = document.createElement('pre');
pre.id = 'helmhidden';
pre.textContent = JSON.stringify({seen: all.length, leaking: out});
document.body.appendChild(pre);
</script>"""
        path = os.path.join(self.tmp, "helm-hidden-%s.html" % name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(page + probe)
        try:
            proc = subprocess.run(
                [self.chrome, "--headless=new", "--disable-gpu", "--no-sandbox",
                 "--window-size=1400,900", "--virtual-time-budget=4000",
                 "--dump-dom", "file://" + path],
                capture_output=True, text=True, timeout=180)
        finally:
            os.unlink(path)
        found = re.search(r'(?s)<pre id="helmhidden">(.*?)</pre>', proc.stdout)
        self.assertIsNotNone(
            found, "the probe wrote no measurement — chrome said %r"
                   % (proc.stderr or "")[-600:])
        measured = json.loads(found.group(1))
        # POSITIVE CONTROL, unconditional: "nothing leaked" is only news if the
        # probe found elements to examine. A page that stopped shipping the
        # attribute, or a selector that stopped matching, would otherwise read
        # as a clean result forever.
        self.assertGreater(
            measured["seen"], 5,
            "the probe found only %d elements carrying `hidden` — it is not "
            "looking at the shipped markup, so an empty leak list means "
            "nothing" % measured["seen"])
        return measured["leaking"]

    def test_nothing_hidden_is_painted(self):
        self.assertEqual(
            [], self.leaking(self.src, "cured"),
            "these elements carry the `hidden` attribute and are painted "
            "anyway: an author `display` is outranking the global rule")

    def test_the_instrument_can_see_the_defect(self):
        """MUST-HIT. Without the cure, the real page must leak.

        This is what separates "the property holds" from "the probe never
        looked". If deleting the one line that carries the guarantee changes
        nothing, this arm is measuring something else and the arm above is
        worthless.
        """
        self.assertIn(RULE, self.src, "the cure is not in the page to delete")
        leaks = self.leaking(self.src.replace(RULE, ""), "uncured")
        self.assertIn(
            "stalebuild", leaks,
            "with the global rule deleted the stale-build offer must be "
            "painted while hidden — that is the defect this suite exists to "
            "hold, and not seeing it means the instrument is blind; saw %r"
            % (leaks,))
