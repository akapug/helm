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

from helm import chat, codexhomes, home, hooks, human, seats, todos, web  # noqa: E402,E501

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

# COUNT-PIN per module: module-granularity alone let a NEW roster() call site
# slip into an ALREADY-allowlisted module unreviewed (r6 was module-only). Pin
# the exact number of call sites per module so a new one — even in an
# allowlisted module — trips the wire until a human re-counts AND confirms the
# new site launders. Regenerate deliberately with _roster_call_sites() below.
_ROSTER_CALL_COUNTS = {
    "codexhomes.py": 1,
    "hooks.py": 1,
    "seat.py": 2,
    "seats.py": 15,
    "todos.py": 1,
    "web.py": 2,
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

    def test_roster_call_site_counts_are_pinned(self):
        """COUNT-PIN, not just module-membership: a NEW roster() call site in an
        ALREADY-allowlisted module (the exact gap r6 left — module-granular
        guards can't see a fresh call site in a module already trusted) trips
        this until a human re-counts and confirms the new site launders."""
        from collections import Counter
        counts = Counter(m for m, _, _ in _roster_call_sites())
        actual = dict(counts)
        self.assertEqual(
            actual, _ROSTER_CALL_COUNTS,
            "roster() call-site counts drifted from the pin. A new call site "
            "(even in an allowlisted module) can reach a sink unreviewed — "
            "verify each site launders its emitted key, then update "
            "_ROSTER_CALL_COUNTS.\n  actual: %s\n  pinned: %s"
            % (actual, _ROSTER_CALL_COUNTS))

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


# ── C. the SOURCE seam: every HELM_CHAT_NAME env read routes the accessor ────
#
# The r1..r6 rounds laundered SINKS; this closes the SOURCE. HELM_CHAT_NAME is
# validated once, in home.chat_name (home.py) — a legit name is [A-Za-z0-9._-],
# a control/bidi name is REJECTED at the seam. This grep enforces that NO other
# module reads the var raw: a new `os.environ["HELM_CHAT_NAME"]` /
# getenv / home.env("CHAT_NAME") anywhere but the accessor FAILS here, so a new
# sink can never be fed an unvalidated name again.
_RAW_ENV_READ = re.compile(
    r"""(?:os\.)?(?:environ(?:\.get)?\s*[\[(]|getenv\s*\()\s*"""
    r"""['"](?:HELM|MELD)_CHAT_NAME""")
_HOME_ENV_READ = re.compile(r"""\benv\(\s*['"]CHAT_NAME['"]""")
# ONLY this module may read the var — it is the accessor's home.
_ACCESSOR_MODULE = "home.py"


def _chat_name_read_sites():
    """[(module, lineno, text)] for every helm/*.py line that reads the
    HELM_CHAT_NAME env var — directly (os.environ/getenv) or via home.env's
    'CHAT_NAME' indirection. Source-driven: a new reader shows up here with no
    edit to this test, then fails unless it is the accessor module."""
    sites = []
    for fn in sorted(os.listdir(PKG)):
        if not fn.endswith(".py"):
            continue
        with open(os.path.join(PKG, fn), encoding="utf-8") as fh:
            for i, line in enumerate(fh, 1):
                if _RAW_ENV_READ.search(line) or _HOME_ENV_READ.search(line):
                    sites.append((fn, i, line.strip()))
    return sites


class SeatNameSourceSeamTest(unittest.TestCase):
    def test_only_the_accessor_reads_helm_chat_name(self):
        """Every HELM_CHAT_NAME ingestion routes home.chat_name — the ONE
        validating seam. A raw read in any other module (a new sink fed an
        unvalidated name) fails until it is routed through the accessor."""
        sites = _chat_name_read_sites()
        self.assertTrue(sites, "found no HELM_CHAT_NAME read — regex rotted")
        offenders = sorted({(m, ln, t) for m, ln, t in sites
                            if m != _ACCESSOR_MODULE})
        self.assertFalse(
            offenders,
            "HELM_CHAT_NAME is read RAW outside the accessor (%s) — route it "
            "through home.chat_name so the name is validated at the source.\n"
            "  offenders: %s" % (_ACCESSOR_MODULE, offenders))

    def test_the_accessor_actually_reads_it(self):
        """The seam cannot rot to a no-op: home.py must still read the var, so
        the allowlist-of-one names a real reader, not a stale entry."""
        live = {m for m, _, _ in _chat_name_read_sites()}
        self.assertIn(_ACCESSOR_MODULE, live,
                      "the accessor module no longer reads HELM_CHAT_NAME — the "
                      "seam moved; update _ACCESSOR_MODULE")


# ── D. the chat.py runtime sweep: hostile name REJECTED at the seam ──────────
#
# r6 found chat.py's from-field (reaching `helm chat read`/`rooms`/--follow +
# /api/chat raw) — the surface the roster()-scoped tripwire is structurally
# blind to. The SOURCE fix closes it: a seat can NEVER post under a hostile
# HELM_CHAT_NAME because home.chat_name rejects it before whoname/derive_seat
# return. This class proves (1) the reject fires across every name reader, (2)
# a legit name (codex-2/opus-integrator/ds4pro) still joins+posts+reads clean,
# and (3) a name planted OUTSIDE the seam (a foreign jsonl row) is still
# display-laundered on read AND /api/chat — belt (source) and suspenders (sink).
class ChatHostileNameSweepTest(unittest.TestCase):
    HOSTILE = "lane" + ESC + "[2J" + BIDI + "pwn"
    LEGIT = ("codex-2", "opus-integrator", "ds4pro")

    _ENV = ("HELM_HOME", "MELD_HOME", "HELM_CHAT_DIR", "MELD_CHAT_DIR",
            "HELM_CHAT_NODE_URL", "MELD_CHAT_NODE_URL",
            "HELM_CHAT_NAME", "MELD_CHAT_NAME", "HELM_CHAT_ROOM",
            "MELD_CHAT_ROOM", "HELM_CELL_BIN", "MELD_CELL_BIN")

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-chatseam-")
        self.prior = {k: os.environ.get(k) for k in self._ENV}
        for k in self._ENV:
            os.environ.pop(k, None)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "chat")
        os.environ["HELM_CHAT_NODE_URL"] = ""   # transport off — hermetic
        self.cwd_prior = os.getcwd()
        os.chdir(self.tmp)                       # default room stays 'main'

    def tearDown(self):
        os.chdir(self.cwd_prior)
        for k, v in self.prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _assert_inert(self, label, text):
        self.assertNotIn(ESC, text, "%s leaked ESC" % label)
        self.assertNotIn(BIDI, text, "%s leaked bidi" % label)

    # (1) the reject fires at the source, across EVERY name reader ------------
    def test_hostile_helm_chat_name_is_rejected_at_the_seam(self):
        os.environ["HELM_CHAT_NAME"] = self.HOSTILE
        for label, fn in (("home.chat_name", home.chat_name),
                          ("chat.whoname", chat.whoname),
                          ("seats.derive_seat", seats.derive_seat),
                          ("human.operator_name", human.operator_name)):
            with self.assertRaises(home.SeatNameError, msg=label):
                fn()

    def test_hostile_name_cannot_post_and_the_error_is_safe(self):
        os.environ["HELM_CHAT_NAME"] = self.HOSTILE
        with self.assertRaises(home.SeatNameError) as cm:
            chat.post("payload")
        # the room never received the hostile row — the post was refused
        _rows, total = chat.read("main")
        self.assertEqual(total, 0, "a hostile name entered the room")
        # the error names the offender SAFELY — no raw ESC/bidi in the message
        self._assert_inert("SeatNameError message", str(cm.exception))
        self.assertIn("\\x1b", str(cm.exception))   # hex-escaped, not raw

    # (2) a LEGIT name still joins + posts + reads cleanly, pinned ------------
    def test_legit_names_join_post_and_read_clean(self):
        for name in self.LEGIT:
            os.environ["HELM_CHAT_NAME"] = name
            self.assertEqual(home.chat_name(), name)
            self.assertEqual(chat.whoname(), name)
            row = chat.post("hi from %s" % name, room=name)
            self.assertEqual(row["from"], name)
            rows, total = chat.read(name)
            self.assertEqual(total, 1)
            self.assertEqual(rows[0]["from"], name)
            self._assert_inert(name, chat._fmt(rows[0]))
            self.assertIn(name, chat._fmt(rows[0]))

    # (3) a name planted OUTSIDE the seam is still laundered on the sinks -----
    def test_planted_hostile_from_field_is_laundered_on_read_and_api(self):
        # bypass the seam: write a raw jsonl row with a hostile `from`. The text
        # carries an @owner mention + a turn id so the owner_mention_* AND ledger
        # sinks (the reviewer's blind spots) are exercised, not only `lines`.
        os.makedirs(chat.chat_dir(), mode=0o700, exist_ok=True)
        import json
        with open(chat.room_path("main"), "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": "2026-07-22T00:00:00", "from": self.HOSTILE,
                                "text": "@david look here", "turn": "t1"},
                               ensure_ascii=False) + "\n")
        rows, total = chat.read("main")
        self.assertEqual(total, 1)
        # CLI/journal render sink: laundered, but the visible name survives
        line = chat._fmt(rows[0])
        self._assert_inert("chat._fmt", line)
        self.assertIn("lane", line)
        self.assertIn("pwn", line)
        import json as _json
        # every browser-polled JSON sink that carries a from-field: /api/chat
        # (incl. owner_mention_last/preview) + the ledger endpoints.
        for label, body in (("/api/chat", web._api_chat({})[0]),
                            ("/api/ledger/native", web._api_ledger_native({})[0]),
                            ("/api/ledger", web._api_ledger({})[0])):
            for s in _walk_strings(body):
                self._assert_inert(label + " (walk)", s)
            self._assert_inert(label + " (serialized)",
                               _json.dumps(body, ensure_ascii=False))
        # the laundered name still rode the wire (not vanished)
        self.assertIn("lane", _json.dumps(web._api_chat({})[0], ensure_ascii=False))

    def test_seat_arg_rejects_hostile_name_second_ingestion(self):
        # the --seat CLI arg is the SECOND seat-name ingestion beside the env
        # seam; home.validate_seat_arg rejects a hostile name so `helm launch
        # --seat <hostile>` can never export it as HELM_CHAT_NAME or key a roster
        # row (the source-grep tripwire covers env reads only).
        from helm import home
        with self.assertRaises(home.SeatNameError) as cm:
            home.validate_seat_arg(self.HOSTILE)
        self._assert_inert("SeatNameError(--seat)", str(cm.exception))
        self.assertEqual(home.validate_seat_arg("codex-2"), "codex-2")
        self.assertIsNone(home.validate_seat_arg(""))


if __name__ == "__main__":
    unittest.main()
