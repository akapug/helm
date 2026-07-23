#!/usr/bin/env python3
"""Cross-MODULE display-launder tripwire — END the ESC/bidi class for good.

This test enumerates OUTPUT SINKS across MODULES, not fields of one report.
That distinction is the whole point. The r1..r5 rounds each hand-patched the
seats.py surfaces they already knew about, and the class kept reopening
because a roster KEY — seats.roster(), a roster dict key, or a pane's
HELM_CHAT_NAME — reaches sinks (stdout/stderr, JSON response bodies, HTML
renders) in OTHER modules (todos.py, codexhomes.py, web.py, hooks.py) that no
seats.py-scoped guard (tests/test_presence.py) could ever see. A hostile
HELM_CHAT_NAME is UNVALIDATED at the join seam, so the raw key may still drive
internal matching — only the EMITTED value must be laundered.

Two tripwires, deliberately BOTH — one proves today, one guards tomorrow:

  A. SOURCE-DRIVEN grep tripwire (test_every_roster_consumer_is_allowlisted):
     greps EVERY helm/*.py caller of seats.roster() and asserts each caller
     module is in an allowlist carrying a REASON (laundered-before-emit, or
     internal-matching-only with no sink). A NEW module that consumes the
     roster FAILS this test until someone registers it with a justification —
     that is the forcing function that stops the 6th surface from being born
     in a module nobody thought to guard.

  B. RUNTIME sweep (test_no_roster_sink_leaks_the_planted_payload): plants a
     hostile HELM_CHAT_NAME (lane\x1b[2J‮pwn, plus a codex-… twin so the
     codex-only readout is exercised) on the roster and DRIVES every
     roster-consuming verb/endpoint across all four modules, asserting NO
     ESC/bidi reaches any stdout/stderr or JSON body. Proves the surfaces are
     clean at HEAD; the grep tripwire keeps them that way.
"""
import contextlib
import io
import os
import re
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import codexhomes, hooks, seats, todos, web  # noqa: E402

# the two payload markers every sink must strip: a screen-clear CSI (Cc) and a
# right-to-left override (Cf, reorders the whole rendered line).
ESC, BIDI = "\x1b", "‮"

HELM_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(HELM_DIR, "helm")


# ── A. the source-driven grep tripwire ──────────────────────────────────────
#
# The allowlist IS the law: every module that CALLS seats.roster() must appear
# here with a reason that is EITHER "launders every emitted key" OR
# "internal-matching-only, no sink". A module absent from this map fails the
# test — so a new roster consumer cannot ship in an unguarded module without a
# human writing down HOW it stays inert. Keyed by module basename; the reason
# is the reviewer's contract, verified for real by the runtime sweep below.
_ROSTER_CONSUMERS = {
    "seats.py": (
        "PUBLISH OWNER: every roster-borne string emits through "
        "_pub_row / _seat_label (roster_report, presence_report, the CLI "
        "glance verbs, the mutation-helper echoes); field-complete guard is "
        "tests/test_presence.py::RosterLaunderCompletenessTest."),
    "todos.py": (
        "LAUNDERED: fleet()/_row run the seat KEY + project through _lbl "
        "(_scrub + _clip) before the fleet table AND the /api/todos JSON; "
        "the todo TEXT is scrubbed in digest()."),
    "codexhomes.py": (
        "LAUNDERED: _print_capacity emits the live codex seats through "
        "seats._seat_label."),
    "web.py": (
        "LAUNDERED: _api_chat routes the roster @mention list through "
        "_seat_label and the sidebar rows through _room_seats/_rooms_summary; "
        "_api_todos rides todos.fleet(); _api_chat_roster rides "
        "roster_report — every emitted key is laundered."),
    "hooks.py": (
        "LAUNDERED: surface_uncovered launders BOTH the printed pane-name "
        "column AND the interpolated reason via seats._seat_label."),
    "seat.py": (
        "INTERNAL-MATCHING-ONLY: counts live codex instances via last_seen "
        "and repr(%r)-echoes the OPERATOR-SUPPLIED launch arg — it never "
        "emits a stored roster KEY to a raw sink."),
}

# matches seats.roster() / _s.roster() / _seats.roster() / bare roster(), but
# NOT the `def roster()` definition, nor write_roster()/gc_roster()/
# roster_report()/roster_path() (\b keeps the underscore-joined names out, and
# roster() requires the empty-parens call).
_ROSTER_CALL = re.compile(r"\broster\(\)")


def _roster_call_sites():
    """[(module, lineno, text)] for every helm/*.py line that CALLS roster().
    Source-driven: this walks the tree, so a new caller shows up here with no
    edit to this test — and then fails unless its module is allowlisted."""
    sites = []
    for fn in sorted(os.listdir(PKG)):
        if not fn.endswith(".py"):
            continue
        with open(os.path.join(PKG, fn), encoding="utf-8") as fh:
            for i, line in enumerate(fh, 1):
                if line.lstrip().startswith("def roster("):
                    continue                     # the definition, not a call
                if _ROSTER_CALL.search(line):
                    sites.append((fn, i, line.strip()))
    return sites


class RosterConsumerAllowlistTest(unittest.TestCase):
    def test_every_roster_consumer_is_allowlisted(self):
        """Enumerates SINKS across MODULES: every helm/*.py caller of
        seats.roster() must be registered in _ROSTER_CONSUMERS with a reason.
        A new module that reads the roster and reaches a sink cannot pass
        until a human writes down how it launders — this is the tripwire that
        makes the class un-reopenable across the tree, not just in seats.py."""
        sites = _roster_call_sites()
        self.assertTrue(sites, "found no roster() call sites — regex rotted")
        offenders = sorted({m for m, _, _ in sites} - set(_ROSTER_CONSUMERS))
        self.assertFalse(
            offenders,
            "un-allowlisted roster consumer(s) %r — a roster KEY can reach a "
            "sink from a module no display-launder guard covers. Add each to "
            "_ROSTER_CONSUMERS with a reason (LAUNDERED via _pub_row/"
            "_seat_label, or INTERNAL-MATCHING-ONLY) AND, if it emits, wire it "
            "into the runtime sweep below.\n  sites: %s"
            % (offenders, [s for s in sites if s[0] in offenders]))

    def test_allowlist_has_no_stale_entries(self):
        """The allowlist cannot rot: every allowlisted module must still call
        roster(). A module that stopped consuming the roster (or was renamed)
        must be pruned so the reasons stay trustworthy."""
        live = {m for m, _, _ in _roster_call_sites()}
        stale = sorted(set(_ROSTER_CONSUMERS) - live)
        self.assertFalse(stale, "allowlist entries no longer call roster(): %r"
                         % stale)


# ── B. the runtime sweep across every roster-consuming SINK ──────────────────
def _walk_strings(v):
    """Every string reachable in a value — dict values, list items, nested."""
    if isinstance(v, str):
        yield v
    elif isinstance(v, dict):
        for x in v.values():
            yield from _walk_strings(x)
    elif isinstance(v, (list, tuple)):
        for x in v:
            yield from _walk_strings(x)


ENV_KEYS = ("HELM_HOME", "MELD_HOME", "HELM_CHAT_DIR", "MELD_CHAT_DIR",
            "HELM_CHAT_NODE_URL", "MELD_CHAT_NODE_URL",
            "HELM_CELL_BIN", "MELD_CELL_BIN",
            "HELM_CHAT_NAME", "MELD_CHAT_NAME")


class RosterSinkSweepTest(unittest.TestCase):
    """Plant a hostile HELM_CHAT_NAME and drive EVERY roster-consuming verb and
    endpoint across todos.py, codexhomes.py, web.py, and hooks.py. Hermetic:
    tmp HELM_HOME + HELM_CHAT_DIR; transport off; the real ~/.helm is never
    touched."""

    SEAT = "lane" + ESC + "[2J" + BIDI + "pwn"
    CODEX_SEAT = "codex-" + ESC + "[2J" + BIDI + "pwn"
    # a running pane whose HELM_CHAT_NAME is hostile and NOT on the roster —
    # exercises hooks' "never joined" reason interpolation.
    PANE = "pane" + ESC + "[2J" + BIDI + "x"

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="helm-test-tripwire-")
        cls.env_prior = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["HELM_HOME"] = os.path.join(cls.tmp, "helm")
        os.environ["HELM_CHAT_DIR"] = os.path.join(cls.tmp, "chat")
        os.environ["HELM_CHAT_NODE_URL"] = ""   # transport off — hermetic
        cls._plant()

    @classmethod
    def tearDownClass(cls):
        for k, v in cls.env_prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @classmethod
    def _plant(cls):
        from helm import pk
        for seat, sid in ((cls.SEAT, "z" * 32), (cls.CODEX_SEAT, "y" * 32)):
            seats.write_roster(seat, session=sid, cwd=cls.tmp)
            with seats._flocked(seats.roster_path() + ".lock"):
                r = seats.roster()
                r[seat]["project"] = "proj" + ESC + "[31m" + BIDI + "X"
                r[seat]["status"] = "busy" + ESC + "[2J" + BIDI + "wiping"
                r[seat]["status_ts"] = time.time()      # fresh: the status tier wins
                pk.write_json(seats.roster_path(), r)
            os.makedirs(os.path.dirname(todos.state_path(sid)), exist_ok=True)
            pk.write_json(todos.state_path(sid), {
                "v": 1, "ts": time.time(),
                "items": [{"id": "1",
                           "text": "evil" + ESC + "[31m" + BIDI + "task",
                           "status": "in_progress"}]})

    def _assert_inert(self, label, text):
        self.assertNotIn(ESC, text, "%s leaked ESC" % label)
        self.assertNotIn(BIDI, text, "%s leaked bidi" % label)

    @staticmethod
    def _cap(fn):
        o, e = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(o), contextlib.redirect_stderr(e):
            rv = fn()
        return o.getvalue() + e.getvalue(), rv

    def _assert_json_inert(self, label, obj):
        import json
        for s in _walk_strings(obj):
            self._assert_inert("%s (walk %r)" % (label, s), s)
        # the actual wire: the server serializes ensure_ascii=False, so a raw
        # bidi codepoint would ride the response bytes uncescaped.
        self._assert_inert("%s (serialized)" % label,
                           json.dumps(obj, ensure_ascii=False))

    # -- todos.py: the fleet table AND /api/todos JSON --------------------------
    def test_todos_fleet_table_is_inert(self):
        text, _ = self._cap(lambda: todos.cmd_todos(["--all"]))
        self._assert_inert("helm todos --all", text)
        # the seat label must have PRINTED (laundered), not vanished
        self.assertIn("lane", text)
        self.assertIn("pwn", text)

    def test_todos_json_is_inert(self):
        text, _ = self._cap(lambda: todos.cmd_todos(["--all", "--json"]))
        self._assert_inert("helm todos --all --json", text)

    def test_todos_fleet_structure_is_inert(self):
        self._assert_json_inert("todos.fleet()", todos.fleet(items=True))

    # -- codexhomes.py: `helm codex capacity` ----------------------------------
    def test_codex_capacity_readout_is_inert(self):
        text, _ = self._cap(codexhomes._print_capacity)
        self._assert_inert("helm codex capacity", text)
        self.assertIn("codex-", text)            # the laundered label survives

    # -- web.py: the three roster-consuming JSON endpoints ----------------------
    def test_api_chat_is_inert(self):
        body, _ = web._api_chat({})
        self._assert_json_inert("/api/chat", body)

    def test_api_todos_is_inert(self):
        body, _ = web._api_todos({})
        self._assert_json_inert("/api/todos", body)

    def test_api_chat_roster_is_inert(self):
        body, _ = web._api_chat_roster({})
        self._assert_json_inert("/api/chat/roster", body)

    # -- hooks.py: surface_uncovered (printed name + interpolated reason) -------
    def test_hooks_surface_uncovered_is_inert(self):
        pane = {"pid": 4242, "seat": self.PANE, "family": "",
                "config_dir": None, "signer_bin": None, "signer_profile": None}
        orig = hooks.running_panes
        hooks.running_panes = lambda proc=None: [pane]
        try:
            text, _ = self._cap(hooks.surface_uncovered)
        finally:
            hooks.running_panes = orig
        self._assert_inert("helm hooks surface_uncovered", text)
        self.assertIn("pane", text)              # the laundered name survives

    # -- proof the sweep BITES: without the launder the payload would leak -----
    def test_planted_payload_is_actually_hostile(self):
        """Guards the guard: if the plant ever stopped carrying ESC/bidi, every
        assertion above would pass vacuously. Prove the raw key is hostile."""
        raw = seats.roster()[self.SEAT]
        self.assertIn(ESC, self.SEAT)
        self.assertIn(BIDI, self.SEAT)
        self.assertIn(ESC, raw["project"])       # the stored key stays raw…
        # …and _seat_label is what makes the EMITTED copy inert.
        self._assert_inert("_seat_label", seats._seat_label(self.SEAT))


if __name__ == "__main__":
    unittest.main()
