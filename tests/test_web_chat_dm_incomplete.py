#!/usr/bin/env python3
"""The DM-namespace warning must reach the RAIL, not just the envelope.

`web_chat` has carried `dm_incomplete` since the reader was fixed: it is True
when the DM lane namespace could not be enumerated, so the sidebar's DM list
may be missing channels. Nothing rendered it. A reviewer found it by reading
the client, which is the only place it could be found — every server-side test
passed, because the server was right.

THE ARM THAT MATTERS IS THE EMPTY ONE. `dmHead` is suppressed when zero DM
lanes came back, so an unreadable namespace that recovered NO lanes drew
nothing at all: the rail looked exactly like an account that has never had a
DM. That is the failure web_chat's own comment calls the worst place to fail
open, and it survived the fix that was written for it.

Like test_web_sidebar_fold.py this runs the ACTUAL assembled source under
node, so it asserts the rendered HTML rather than the spelling of a constant —
a census pinned to spelling breaks on a refactor while claiming the behaviour
still holds. Requires node; skipped (not failed) where unavailable.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from helm import web_ui_loader  # noqa: E402
from tests.test_web_chat_client_runtime import _extract_fn  # noqa: E402

EXTRACT = ["roomType", "chatQuietCut", "chatRoomQuiet", "chatSplitQuiet",
           "chatByRecency", "chatDmState", "chatRooms"]

# TWO PIECES, NOT ONE STRING SPLIT AT A MARKER. The prelude must precede the
# lifted source and the arms must follow it, and slicing one blob on a literal
# would rearrange itself the first time that literal moved.
PRELUDE = r"""
// The DOM and the neighbours chatRooms leans on, stubbed to the smallest
// shape that lets the REAL function run. Anything that would paint returns a
// constant, so what the arms read back is the rail's own markup decision.
let RAIL = "";
const $ = () => ({set innerHTML(v) { RAIL = v; }, get innerHTML() { return RAIL; }});
const esc = s => String(s == null ? "" : s);
const chatShort = s => String(s);
// chatIso MIRRORS ITS SOURCE LINE VERBATIM (60-chat.js.part) rather than
// being lifted, because _extract_fn understands `function name(...)` and this
// one is a const arrow. It is display-only here — chatRoomQuiet does its OWN
// Date.parse and never calls it — so the mirror cannot decide an arm.
//
// TWO WRONG DRAFTS ARE WHY THIS PARAGRAPH EXISTS. The rooms originally carried
// no `last` at all; a room with no parseable stamp is QUIET, quiet rooms fold
// into a collapsed group, and that group is closed by default, so every DM
// lane was folded out of the rail while two arms claimed to measure a rendered
// one. Draft one returned null from this helper and draft two returned a
// constant 60 — both left the arms red, because the helper was never the thing
// deciding. The fixture's missing timestamp was. Real stamps are the cure.
const chatIso = t => { const e = Date.parse(t || ""); return isNaN(e) ? null : (Date.now() - e) / 1e3; };
const chatAgo = () => "";
const seatRuntime = () => "";
let CHAT_ROOMS_LAST = [], CHAT_PRESENCE = {};
let CHAT_ROOM = "main", HELM_DEFAULT_ROOM = "helm";
let CHAT_ROOMS_OPEN = new Set(), CHAT_QUIET_OPEN = new Set();
let CHAT_DMS_FOLD = false, CHAT_MELDS_FOLD = false;
let CHAT_DM_STATE = "ok";
"""

ARMS = r"""
// A REAL, RECENT ISO STAMP — the rooms must read ACTIVE or the rail folds
// them into the collapsed quiet group and the arms measure nothing.
const iso = agoS => new Date(Date.now() - agoS * 1e3).toISOString();
const out = [];
const t = (name, fn) => {
  try { out.push({name, pass: true, detail: fn() || {}}); }
  catch (e) { out.push({name, pass: false, detail: String(e && e.stack || e)}); }
};
// RENDER FROM AN ENVELOPE, THE WAY THE POLL DOES. The previous version took a
// boolean and assigned the state by hand, so no arm could feed a real server
// payload — which is precisely how the missing-field case went uncaught.
const renderEnvelope = (rooms, envelope) => {
  CHAT_DM_STATE = chatDmState(envelope); RAIL = "";
  chatRooms(rooms);
  return RAIL;
};
const render = (rooms, incomplete) =>
  renderEnvelope(rooms, {dm_incomplete: incomplete});

// THE HEADLINE: no DM lanes recovered AND the namespace unreadable. This is
// the state that used to render an empty, confident, wrong sidebar.
t("unreadable_namespace_with_ZERO_lanes_still_warns", () => {
  const html = render([{room: "main", last: iso(60)}], true);
  return {warned: html.includes("dms incomplete"),
          // the positive control on the SAME observable and the same input:
          // flip only the verdict and the warning must vanish, which proves
          // the arm reads the flag and not merely that the rail renders text
          control_clean: render([{room: "main", last: iso(60)}], false).includes("dms incomplete"),
          rail_nonempty: html.length > 0};
});

// A warning that REPLACED the section would trade one silence for another.
t("warning_accompanies_the_dm_section_it_does_not_replace_it", () => {
  const rooms = [{room: "main", last: iso(60)},
                 {room: "dm-alice-0123abcd", seat: "alice", last: iso(60)}];
  const html = render(rooms, true);
  return {warned: html.includes("dms incomplete"),
          header_survives: html.includes("crdmhead"),
          lane_survives: html.includes("alice")};
});

// A healthy fleet must never see it — the whole point of a fault signal is
// that its absence is also information.
t("healthy_namespace_never_warns", () => {
  const rooms = [{room: "main", last: iso(60)},
                 {room: "dm-bob-4567beef", seat: "bob", last: iso(60)}];
  const html = render(rooms, false);
  return {warned: html.includes("dms incomplete"),
          lane_rendered: html.includes("bob")};
});

// THE COMPATIBILITY ARM: a valid envelope from the PREVIOUS server generation,
// which carries rooms and lines and simply has no dm_incomplete key at all.
t("an_OLD_SERVER_envelope_is_UNREPORTED_not_complete", () => {
  const old = {rooms: [], lines: [], total: 0, roster: []};   // no dm_incomplete
  const html = renderEnvelope([{room: "main", last: iso(60)}], old);
  return {state: chatDmState(old),
          warned: html.includes("dms not reported"),
          // and it must NOT claim the measured-incomplete wording, which would
          // assert a measurement this server never made
          not_miscalled: !html.includes("dms incomplete"),
          // the positive pole on the same path: a server that DID measure and
          // found it complete renders nothing at all
          modern_ok_silent: !renderEnvelope([{room: "main", last: iso(60)}],
                                            {dm_incomplete: false})
                              .includes("dms")};
});

console.log(JSON.stringify(out));
"""


class TestDmIncompleteRail(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        if not cls.node:
            raise unittest.SkipTest("node not available")
        src = web_ui_loader.read_text()
        fns = "\n\n".join(_extract_fn(src, name) for name in EXTRACT)
        cls.tmp = tempfile.mkdtemp(prefix="helm-dm-incomplete-")
        cls.path = os.path.join(cls.tmp, "run.js")
        with open(cls.path, "w", encoding="utf-8") as f:
            f.write(PRELUDE + "\n" + fns + "\n" + ARMS)
        chk = subprocess.run([cls.node, "--check", cls.path],
                             capture_output=True, text=True)
        assert chk.returncode == 0, "node --check failed:\n" + chk.stderr
        proc = subprocess.run([cls.node, cls.path], capture_output=True,
                              text=True, timeout=60)
        cls.proc = proc
        try:
            cls.results = {r["name"]: r for r in json.loads(proc.stdout or "[]")}
        except json.JSONDecodeError:
            cls.results = {}

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(getattr(cls, "tmp", ""), ignore_errors=True)

    def _detail(self, name):
        """The measured values from the node run.

        A MISSING RESULT IS A FAILURE, NEVER A PASS. If the driver threw before
        reaching an arm, `results` is short — and a test that merely looked up
        a key it never found would report success for a run that produced
        nothing. The stderr goes into the message because a node-side stack is
        the only thing that explains an empty result set.
        """
        self.assertIn(name, self.results,
                      "arm %r did not run — node stdout=%r stderr=%r"
                      % (name, self.proc.stdout[:400], self.proc.stderr[:400]))
        r = self.results[name]
        self.assertTrue(r["pass"], "%s threw: %s" % (name, r["detail"]))
        return r["detail"]

    def test_unreadable_namespace_with_zero_lanes_still_warns(self):
        """The regression that motivated the whole arm: zero DM lanes plus an
        unreadable namespace drew a clean, empty, confident sidebar."""
        d = self._detail("unreadable_namespace_with_ZERO_lanes_still_warns")
        self.assertTrue(d["rail_nonempty"], "the rail rendered nothing at all")
        self.assertTrue(d["warned"],
                        "an unreadable DM namespace with no recovered lanes "
                        "rendered NO warning — the owner sees an account with "
                        "no DMs and no reason to doubt it")
        self.assertFalse(d["control_clean"],
                         "the warning rendered with dm_incomplete FALSE, so "
                         "the arm above proves nothing about the flag")

    def test_warning_does_not_replace_the_dm_section(self):
        d = self._detail(
            "warning_accompanies_the_dm_section_it_does_not_replace_it")
        self.assertTrue(d["warned"])
        self.assertTrue(d["header_survives"],
                        "the warning swallowed the DM header")
        self.assertTrue(d["lane_survives"],
                        "the warning swallowed the DM lanes that WERE read — "
                        "an incomplete list must still show what it has")

    def test_a_healthy_namespace_is_silent(self):
        d = self._detail("healthy_namespace_never_warns")
        self.assertFalse(d["warned"],
                         "a healthy namespace warned; a signal that always "
                         "fires carries no information")
        self.assertTrue(d["lane_rendered"])


    def test_an_OLD_SERVERS_ENVELOPE_is_UNREPORTED_not_complete(self):
        """THE COMPATIBILITY ARM A REVIEWER SAID WAS MISSING, and he was right
        about why: the other arms assign the state by hand, so none of them
        could feed a real payload.

        The previous server generation returns a VALID chat envelope — rooms,
        lines, roster — that simply has no `dm_incomplete` key. `=== true` read
        that absence as a measured false and the rail drew no warning about a
        completeness nobody had measured. Absence is not evidence."""
        d = self._detail("an_OLD_SERVER_envelope_is_UNREPORTED_not_complete")
        self.assertEqual(d["state"], "unreported",
                         "an envelope with no dm_incomplete resolved to a "
                         "MEASURED state; absence became evidence")
        self.assertTrue(d["warned"],
                        "an old server's payload rendered a silent, confident "
                        "DM rail about a namespace it never measured")
        self.assertTrue(d["not_miscalled"],
                        "it claimed the namespace was measured INCOMPLETE, "
                        "asserting a measurement the server never made")
        self.assertTrue(d["modern_ok_silent"],
                        "a server that DID measure and found it complete still "
                        "warned — the pole is gone and the badge means nothing")


if __name__ == "__main__":
    unittest.main()
