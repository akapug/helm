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

from helm import chat, codexhomes, home, hooks, human, meld, pk, seats, todos, web  # noqa: E402,E501

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
        "tests/test_presence.py::RosterLaunderCompletenessTest. The consume-"
        "ladder readers (_recipients resolves @mention/rfrom tokens to roster "
        "KEYS, _recipient_cursor matches a cursor row internally, pending "
        "resolves recipients) emit only through the `pending` CLI's "
        "_seat_label / the `ack` refusal's _seat_label — same publish owner."),
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
    "seats.py": 18,        # +3: _recipients, _recipient_cursor, pending (ack ladder)
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


# ── E. the chat-ROW from-field sinks: deliver / stop_guard / meld.recv ───────
#
# Rounds A–D covered roster() consumers, the four web endpoints, and the
# HELM_CHAT_NAME env seam. But three terminal-facing sinks read a from-field
# off a CHAT ROW — not from roster() and not from the env — so BOTH the
# roster-grep tripwire (A) and the env-seam grep (C) are structurally BLIND to
# them, and the round-D chat sweep only drives chat._fmt / the JSON endpoints:
#
#   1. seats.deliver()   — the tool-boundary nudge "[helm chat → seat] <FROM>:
#                          <text>" (the most-rendered agent-facing line, every
#                          PostToolUse + beacon).
#   2. seats.stop_guard() — the undelivered-message block line.
#   3. meld.recv()       — the YIELD/HOLD/DONE/ABORT/READY line to the peer.
#
# A hostile from-field reaches these via a PLANTED/FOREIGN jsonl row (written
# outside the validating join seam — a pre-fix row, a foreign node's row). The
# TEXT legitimately carries unicode and is left as-is (the class rule); only
# the identity field is laundered through chat._dsan. THIS SWEEP now covers the
# chat-ROW from-field sinks, not just roster()/env readers.
#
# r10 broadened the sweep to the FULL meld identity surface + verify: the
# convener/peer from-field also PROMOTES into posted message TEXT (join's READY,
# invite's @peer, say's DONE/ABORT mention) which chat._fmt renders raw
# fleet-wide — the identity-into-text bypass — plus meld.status' peer column and
# chat.verify's MISMATCH stderr print. Every meld identity EMIT and verify's
# emitted dict now launder through chat._dsan; the RAW convener/peer lives only
# in state for recv's matching. The Section-F grep tripwire below keeps it that
# way: a NEW raw from-field emit in ANY module trips the source-driven grep.
class ChatRowFromFieldSinkSweepTest(unittest.TestCase):
    HOSTILE = "lane" + ESC + "[2J" + BIDI + "pwn"

    _ENV = ("HELM_HOME", "MELD_HOME", "HELM_CHAT_DIR", "MELD_CHAT_DIR",
            "HELM_CHAT_NODE_URL", "MELD_CHAT_NODE_URL",
            "HELM_CHAT_NAME", "MELD_CHAT_NAME", "HELM_CHAT_ROOM",
            "MELD_CHAT_ROOM", "HELM_CELL_BIN", "MELD_CELL_BIN")

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-rowsink-")
        self.prior = {k: os.environ.get(k) for k in self._ENV}
        for k in self._ENV:
            os.environ.pop(k, None)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "chat")
        os.environ["HELM_CHAT_NODE_URL"] = ""    # transport off — hermetic
        self.cwd_prior = os.getcwd()
        os.chdir(self.tmp)                        # default room stays 'main'
        os.makedirs(chat.chat_dir(), mode=0o700, exist_ok=True)

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

    def _plant(self, room, rid, text):
        """Append ONE raw jsonl row with a hostile from-field — bypassing the
        join seam, as a foreign/pre-fix node row does."""
        import json
        with open(chat.room_path(room), "a", encoding="utf-8") as f:
            f.write(json.dumps(
                {"ts": "2026-07-22T00:00:00", "id": rid, "from": self.HOSTILE,
                 "text": text}, ensure_ascii=False) + "\n")

    def _plant_obj(self, room, obj):
        """Append ONE raw jsonl row of an ARBITRARY shape — the meld/verify
        sinks need custom from + text + protocol markers (READY/ABORT/DONE,
        an epoch fence, a signed-MISMATCH payload)."""
        import json
        row = {"ts": "2026-07-22T00:00:00"}
        row.update(obj)
        with open(chat.room_path(room), "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    @staticmethod
    def _cap(fn):
        o, e = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(o), contextlib.redirect_stderr(e):
            rv = fn()
        return o.getvalue() + e.getvalue(), rv

    # -- 1. seats.deliver(): the tool-boundary nudge ---------------------------
    def test_deliver_boundary_nudge_is_inert(self):
        self._plant("main", "aa" * 6, "@recvr ping here")
        collected = []
        seats.deliver(session="s" * 32, room="main", seat="recvr",
                      emit=collected.append, backfill=True)
        line = "\n".join(collected)
        self._assert_inert("seats.deliver", line)
        # the from-field actually EMITTED (laundered, not vanished): the sweep
        # would pass vacuously if deliver never rendered the row's from.
        self.assertIn("lane", line)
        self.assertIn("pwn", line)

    # -- 2. seats.stop_guard(): the undelivered-message block ------------------
    def test_stop_guard_inbox_block_is_inert(self):
        sid = "t" * 32
        seats.write_roster("stopr", session=sid, cwd=self.tmp)
        seats.deliver(session=sid, room="main", seat="stopr",
                      emit=lambda s: None)         # establish the EOF cursor
        self._plant("main", "bb" * 6, "@stopr ping here")
        blocks, warns = seats.stop_guard(session=sid, room="main", seat="stopr")
        text = "\n".join(blocks + warns)
        self._assert_inert("seats.stop_guard", text)
        self.assertIn("undelivered", text)         # the block fired…
        self.assertIn("lane", text)                # …and rendered the from
        self.assertIn("pwn", text)

    # -- 3. meld.recv(): the peer-facing chunk line ---------------------------
    def test_meld_recv_chunk_line_is_inert(self):
        room = "meld-sink-test"
        meld._write_state(room, "recvr2", {
            "room": room, "epoch": 123, "role": "joiner", "self": "recvr2",
            "peer": self.HOSTILE, "idx": 0, "exchanges": 0, "cap": 8,
            "status": "active", "created": pk.now_ts()})
        self._plant(room, "cc" * 6, "my chunk [YIELD]")
        code, lines = meld.recv(room, timeout=0.2, seat="recvr2")
        text = "\n".join(lines)
        self._assert_inert("meld.recv", text)
        self.assertEqual(code, 0)                  # a real chunk was returned…
        self.assertIn("lane", text)                # …with the from laundered
        self.assertIn("pwn", text)

    # -- 3b. meld.recv(): the OTHER three marker lines (r9 laundered all four,
    #        the r9 sweep drove only YIELD — READY/ABORT/DONE were revert-blind).
    def test_meld_recv_ready_abort_done_lines_are_inert(self):
        # READY (convener/invited): the joiner's control row, hostile from.
        r1 = "meld-recv-ready"
        meld._write_state(r1, "conv2", {
            "room": r1, "epoch": 123, "role": "convener", "self": "conv2",
            "peer": "joiner", "idx": 0, "exchanges": 0, "cap": 8,
            "status": "invited", "created": pk.now_ts()})
        self._plant_obj(r1, {"from": self.HOSTILE,
                             "text": "[MELD e:123] READY:123 (joined)"})
        code, lines = meld.recv(r1, timeout=0.2, seat="conv2")
        t = "\n".join(lines)
        self._assert_inert("meld.recv READY", t)
        self.assertEqual(code, 0)
        self.assertIn("lane", t)
        self.assertIn("pwn", t)
        # ABORT (active joiner): fail-loud line renders the peer's from.
        r2 = "meld-recv-abort"
        meld._write_state(r2, "recvr3", {
            "room": r2, "epoch": 9, "role": "joiner", "self": "recvr3",
            "peer": self.HOSTILE, "idx": 0, "exchanges": 0, "cap": 8,
            "status": "active", "created": pk.now_ts()})
        self._plant_obj(r2, {"from": self.HOSTILE, "text": "boom [ABORT]"})
        code, lines = meld.recv(r2, timeout=0.2, seat="recvr3")
        t = "\n".join(lines)
        self._assert_inert("meld.recv ABORT", t)
        self.assertEqual(code, meld.EXIT_ABORT)
        self.assertIn("lane", t)
        # DONE (active joiner): peer-left line renders the from.
        r3 = "meld-recv-done"
        meld._write_state(r3, "recvr4", {
            "room": r3, "epoch": 9, "role": "joiner", "self": "recvr4",
            "peer": self.HOSTILE, "idx": 0, "exchanges": 0, "cap": 8,
            "status": "active", "created": pk.now_ts()})
        self._plant_obj(r3, {"from": self.HOSTILE, "text": "bye [DONE]"})
        code, lines = meld.recv(r3, timeout=0.2, seat="recvr4")
        t = "\n".join(lines)
        self._assert_inert("meld.recv DONE", t)
        self.assertEqual(code, 0)
        self.assertIn("lane", t)

    # -- 4. meld.join(): the MELD-JOINED display AND the promoted READY TEXT ----
    #    convener is a SEED ROW's from — HIGH: it entered the POSTED text (@%s
    #    [MELD…] READY), which chat._fmt then renders raw fleet-wide.
    def test_meld_join_display_and_promoted_text_are_inert(self):
        room = "meld-join-test"
        self._plant_obj(room, {"from": self.HOSTILE,
                               "text": "[MELD e:123] PROBLEM: x [HOLD]"})
        lines = meld.join(room, seat="joiner")
        disp = "\n".join(lines)
        self._assert_inert("meld.join display", disp)
        self.assertIn("lane", disp)
        self.assertIn("pwn", disp)
        rows, _ = chat.read(room)
        ready = [r for r in rows if "READY" in (r.get("text") or "")][0]
        # the convener was laundered BEFORE entering the posted text — the
        # identity-into-text bypass is closed at the source of the promotion.
        self._assert_inert("meld.join promoted READY text", ready["text"])
        self.assertIn("lane", ready["text"])
        self._assert_inert("meld.join READY _fmt", chat._fmt(ready))

    # -- 5. meld.invite(): the @peer mention posted into TEXT + display lines ---
    def test_meld_invite_mention_and_display_are_inert(self):
        room, lines = meld.invite(self.HOSTILE, "topic here", seat="conv")
        disp = "\n".join(lines)
        self._assert_inert("meld.invite display", disp)
        self.assertIn("lane", disp)
        self.assertIn("pwn", disp)
        rows, _ = chat.read(room)
        inv = [r for r in rows if "MELD-INVITE" in (r.get("text") or "")][0]
        self._assert_inert("meld.invite posted @mention text", inv["text"])
        self.assertIn("lane", inv["text"])

    # -- 6. meld.say(): the DONE/ABORT @peer mention posted into TEXT ----------
    def test_meld_say_done_mention_is_inert(self):
        room = "meld-say-test"
        meld._write_state(room, "sayer", {
            "room": room, "epoch": 55, "role": "convener", "self": "sayer",
            "peer": self.HOSTILE, "idx": 0, "exchanges": 0, "cap": 8,
            "status": "active", "created": pk.now_ts()})
        lines = meld.say(room, "DONE", "closing state", seat="sayer")
        self._assert_inert("meld.say return", "\n".join(lines))
        rows, _ = chat.read(room)
        done = [r for r in rows if "[DONE]" in (r.get("text") or "")][0]
        self._assert_inert("meld.say posted DONE text", done["text"])
        self.assertIn("lane", done["text"])
        self.assertIn("pwn", done["text"])

    # -- 7. meld.status(): the peer=%s display column --------------------------
    def test_meld_status_display_is_inert(self):
        room = "meld-status-test"
        meld._write_state(room, "statr", {
            "room": room, "epoch": 7, "role": "convener", "self": "statr",
            "peer": self.HOSTILE, "idx": 0, "exchanges": 0, "cap": 8,
            "status": "active", "created": pk.now_ts()})
        text = "\n".join(meld.status(seat="statr"))
        self._assert_inert("meld.status", text)
        self.assertIn("lane", text)
        self.assertIn("pwn", text)

    # -- 8. chat.verify(): the emitted dict AND the CLI MISMATCH stderr print ---
    def test_chat_verify_mismatch_row_is_inert(self):
        room = "verify-test"
        # a SIGNED row whose stored payload no longer recomputes = MISMATCH, the
        # one verify state that reaches the stderr print (r["from"], raw pre-fix).
        self._plant_obj(room, {"from": self.HOSTILE, "text": "x",
                               "chain": "sig-abc", "payload": "WRONG-PAYLOAD"})
        rep = chat.verify(room)
        for r in rep:                              # every emitted from laundered
            self._assert_inert("verify() dict from", r["from"])
        bad = [r for r in rep if r["state"] == "MISMATCH"]
        self.assertTrue(bad, "the planted signed row must verify MISMATCH")
        self.assertIn("lane", bad[0]["from"])      # laundered, not vanished
        self.assertIn("pwn", bad[0]["from"])
        # the actual CLI stderr sink: the print reads r["from"] off the dict.
        text, _ = self._cap(lambda: chat.cmd_chat(["verify", "--room", room]))
        self._assert_inert("helm chat verify (stderr)", text)
        self.assertIn("lane", text)

    # -- 9. chat.log_flush(): the journal a `cat` renders (identity columns) ----
    def test_log_flush_journal_is_inert(self):
        room = "main"
        self._plant_obj(room, {"from": self.HOSTILE, "id": "ee" * 6,
                               "text": "@x hi there"})
        n = chat.log_flush(rooms=[room])
        self.assertGreaterEqual(n, 1)
        path = os.path.join(chat.journal_dir(),
                            "chat-%s.log" % time.strftime("%Y-%m-%d"))
        with open(path, encoding="utf-8") as f:
            body = f.read()
        # the from column is _dsan-laundered; the message TEXT stays full-fidelity
        self._assert_inert("chat log-flush journal", body)
        self.assertIn("lane", body)                # laundered name survives
        self.assertIn("pwn", body)
        self.assertIn("hi there", body)            # the text rode through intact

    # -- proof the sweep BITES: the planted from-field is genuinely hostile ----
    def test_planted_from_field_is_actually_hostile(self):
        """Guards the guard: if the plant stopped carrying ESC/bidi the three
        assertions above would pass vacuously. The stored from stays RAW; only
        the EMITTED copy (chat._dsan) is inert."""
        self.assertIn(ESC, self.HOSTILE)
        self.assertIn(BIDI, self.HOSTILE)
        self._plant("main", "dd" * 6, "@x hi")
        rows, _total = chat.read("main")
        self.assertIn(ESC, rows[0]["from"])        # stored key stays raw…
        self._assert_inert("chat._dsan", chat._dsan(self.HOSTILE))  # …emit inert


# ── F. the SOURCE-DRIVEN grep tripwire for chat-ROW from-field emits ─────────
#
# THE FROM-FIELD ANALOG OF THE roster() TRIPWIRE (Section A,
# RosterConsumerAllowlistTest). Section A greps every seats.roster() caller and
# forces each into an allowlist with a reason; a new consumer in an unguarded
# module fails by construction. This does the IDENTICAL thing for the chat-row
# IDENTITY fields — the second unvalidated identity source that kept the ESC/
# bidi class reopening (r7..r10): a foreign/planted row's from/tfrom/rfrom/dm,
# and its meld relocations convener (join) + peer (meld state). Unlike
# HELM_CHAT_NAME (rejected at the home.chat_name seam, Section C), a foreign row
# CANNOT be rejected — so its only defense is PER-SINK laundering (chat._dsan),
# and each round found one more sink. This grep ENDS that: it enumerates EVERY
# helm/*.py site that reads a chat/meld row's identity field, and every module
# reaching a sink must be allowlisted with a reason — LAUNDERED (the emitted
# copy routes chat._dsan / public_rows / _fmt), INTERNAL-MATCHING-ONLY (the read
# never reaches a sink — react/reply/deliverable keys), or NOT-A-CHAT-ROW. A NEW
# raw from-field emit in ANY module then FAILS this grep by construction: it is
# a new read site (count-pin trips) or lands in an unlisted module (allowlist
# trips) — sink #14 can never ship unlaundered. The reason is the reviewer's
# contract; the Section-E runtime sweep verifies the emits are actually inert.

# IDENTITY-field dict accessors ( .get/.pop/.setdefault("X") / ["X"] ) —
# from/tfrom/rfrom/dm are the chat-row NAME columns (chat._ID_FIELDS); peer is
# the meld state relocation of a convener from-field (say/status emit it). The
# keys are ALWAYS quoted, so this is code-only by nature (no prose match),
# exactly like _ROSTER_CALL. get/pop/setdefault all READ the value into a sink
# (r10 adversarial: a raw .pop("from") emit would otherwise evade the sweep);
# %-dict ("%(from)s") and itemgetter remain a documented LOW residual — exotic
# enough that no current sink uses them and a future one is caught at review.
_FROM_FIELD_READ = re.compile(
    r"""(?:\.(?:get|pop|setdefault)\(\s*|\[\s*)['"](?:from|tfrom|rfrom|dm|peer)['"]""")
# the meld `convener` LOCAL, assigned from a seed row's `from` — matched as a
# bare identifier in CODE only (docstrings + comments + the "convener" role
# string literal are stripped before the match, see _from_field_read_sites).
_CONVENER_LOCAL = re.compile(r"\bconvener\b")
_STR_LITERAL = re.compile(r"""(['"]).*?\1""")


def _code_visible(line, st):
    """The part of `line` OUTSIDE any triple-quoted docstring, tracking the
    open/close across lines via `st` — so a docstring that merely MENTIONS
    'convener' (meld.join's) is never counted as a read site."""
    out, i, n = [], 0, len(line)
    while i < n:
        if st["in"]:
            idx = line.find(st["q"], i)
            if idx == -1:
                i = n
            else:
                i = idx + 3
                st["in"] = False
                st["q"] = None
        else:
            cands = [x for x in (line.find('"""', i), line.find("'''", i))
                     if x != -1]
            if not cands:
                out.append(line[i:])
                i = n
            else:
                nxt = min(cands)
                out.append(line[i:nxt])
                st["q"] = line[nxt:nxt + 3]
                st["in"] = True
                i = nxt + 3
    return "".join(out)


def _strip_comment(code):
    """Drop the trailing comment QUOTE-AWARE — a naive split("#") truncates at
    a '#' INSIDE a string literal, hiding any accessor after it (a planted
    `print("#issue %s" % row.get("from"))` evaded the r10 tripwire that way).
    Worst-case mis-tracking keeps comment text and over-counts — which fails
    the count-pin LOUDLY, never silently hides a read site."""
    q = None
    for i, ch in enumerate(code):
        if q:
            if ch == q and (i == 0 or code[i - 1] != "\\"):
                q = None
        elif ch in "'\"":
            q = ch
        elif ch == "#":
            return code[:i]
    return code


def _from_field_read_sites():
    """[(module, lineno, text)] for every helm/*.py line that reads a chat/meld
    row's IDENTITY field — a from/tfrom/rfrom/dm/peer dict accessor, or the
    meld `convener` local. Source-driven, like _roster_call_sites: a new reader
    shows up here with no edit to this test, then fails unless its module is
    allowlisted (and its module count re-pinned)."""
    sites = []
    for fn in sorted(os.listdir(PKG)):
        if not fn.endswith(".py"):
            continue
        st = {"in": False, "q": None}
        with open(os.path.join(PKG, fn), encoding="utf-8") as fh:
            for i, line in enumerate(fh, 1):
                code = _strip_comment(_code_visible(line, st))
                hit = bool(_FROM_FIELD_READ.search(code))
                if not hit and _CONVENER_LOCAL.search(_STR_LITERAL.sub("", code)):
                    hit = True                  # bare convener local, no literal
                if hit:
                    sites.append((fn, i, line.strip()))
    return sites


# The allowlist IS the law (mirrors _ROSTER_CONSUMERS): every module that reads
# a chat-row identity field must appear here with a reason. Keyed by basename.
_FROM_FIELD_CONSUMERS = {
    "chat.py": (
        "LAUNDERED (publish owner): every identity column that reaches a sink "
        "routes chat._dsan — _fmt (CLI/journal render), _fmt_body (log-flush "
        "journal), verify() (the emitted dict, one-owner for the MISMATCH "
        "stderr print), public_rows (the JSON wire). The remaining reads are "
        "INTERNAL-MATCHING-ONLY: react-digest/react-state keys, reply/quote "
        "resolution, rkey, _touch_poster_presence, the dm-room derivation — "
        "none emit a raw name. Verified by ChatHostileNameSweep + "
        "ChatRowFromFieldSinkSweep."),
    "meld.py": (
        "LAUNDERED: every meld identity EMIT routes chat._dsan — invite (@peer "
        "posted text + MELD-INVITED display, d_peer), join (READY posted text + "
        "MELD-JOINED display, d_convener), recv (READY/ABORT/DONE lines, "
        "chat._dsan(frm)), say (DONE/ABORT @peer mention), status (peer column). "
        "The RAW convener/peer is kept ONLY in state[peer] for recv's frm "
        "matching (never emitted raw). Verified by ChatRowFromFieldSinkSweep's "
        "join/invite/say/status/recv-all-markers tests."),
    "seats.py": (
        "LAUNDERED+INTERNAL: the EMIT sites — deliver()'s boundary nudge, "
        "stop_guard()'s undelivered block, the `ack` refusal string and the "
        "`pending`/`ack` CLI success lines — launder via chat._dsan / "
        "_seat_label; the deliverable-matching reads (frm/dm/rfrom in "
        "deliverable()) and the consume-ladder matching reads (from/dm/rfrom "
        "casefolded into dict keys + branch tests in _recipients / "
        "consume_state / pending / ack) are INTERNAL-MATCHING-ONLY, never "
        "emitted raw. Verified by ChatRowFromFieldSinkSweep's deliver/"
        "stop_guard tests + test_ackladder's hostile-name sink test."),
    "web.py": (
        "LAUNDERED+INTERNAL: the owner-mention preview + ledger + channel-row "
        "emits launder via chat._dsan (and the /api/chat body via "
        "chat.public_rows); the mention-scan (str(from).lower() in names), the "
        "recent-activity roster lookup (frm in roster), and the react-target "
        "payload.get('tfrom') are INTERNAL-MATCHING-ONLY. Verified by "
        "RosterSinkSweep's /api/chat + /api/ledger tests."),
    "homes.py": (
        "NOT-A-CHAT-ROW: meta.get('from') is a provider-migration SOURCE PATH "
        "(the home's origin dir), never a chat/meld identity — it reaches no "
        "chat sink. Listed so a future `.get(\"from\")` here is re-justified."),
}

# COUNT-PIN per module (mirrors _ROSTER_CALL_COUNTS): module-membership alone
# lets a NEW read site slip into an already-allowlisted module unreviewed. Pin
# the exact count so a new identity read — even in an allowlisted module —
# trips until a human re-counts AND confirms the new site launders (or is
# internal). Regenerate deliberately from _from_field_read_sites().
_FROM_FIELD_READ_COUNTS = {
    "chat.py": 30,         # +1: _fmt's ack-marker render (laundered via _dsan)
    "homes.py": 1,
    "meld.py": 9,
    "seats.py": 17,        # +11: the ack/consume-ladder reads (matching + laundered emits)
    "web.py": 7,
}


class ChatRowFromFieldConsumerAllowlistTest(unittest.TestCase):
    """The from-field analog of RosterConsumerAllowlistTest (Section A). Same
    three teeth: every consuming module allowlisted with a reason, the per-
    module read-count pinned, and no stale allowlist entry."""

    def test_every_from_field_consumer_is_allowlisted(self):
        sites = _from_field_read_sites()
        self.assertTrue(sites, "found no from-field read sites — regex rotted")
        offenders = sorted({m for m, _, _ in sites} - set(_FROM_FIELD_CONSUMERS))
        self.assertFalse(
            offenders,
            "un-allowlisted chat-row identity consumer(s) %r — a from/tfrom/"
            "rfrom/dm/convener/peer can reach a sink from a module no display-"
            "launder guard covers (the exact way sinks #12..#14 were born). Add "
            "each to _FROM_FIELD_CONSUMERS with a reason (LAUNDERED via "
            "chat._dsan/public_rows/_fmt, INTERNAL-MATCHING-ONLY, or "
            "NOT-A-CHAT-ROW) AND, if it emits, wire it into the Section-E "
            "runtime sweep.\n  sites: %s"
            % (offenders, [s for s in sites if s[0] in offenders]))

    def test_from_field_read_counts_are_pinned(self):
        from collections import Counter
        actual = dict(Counter(m for m, _, _ in _from_field_read_sites()))
        self.assertEqual(
            actual, _FROM_FIELD_READ_COUNTS,
            "chat-row identity read-site counts drifted from the pin. A new read "
            "site (even in an allowlisted module) can reach a sink unreviewed — "
            "verify each new site launders its emitted identity (or is internal/"
            "not-a-chat-row), then update _FROM_FIELD_READ_COUNTS.\n"
            "  actual: %s\n  pinned: %s" % (actual, _FROM_FIELD_READ_COUNTS))

    def test_allowlist_has_no_stale_entries(self):
        live = {m for m, _, _ in _from_field_read_sites()}
        stale = sorted(set(_FROM_FIELD_CONSUMERS) - live)
        self.assertFalse(stale, "from-field allowlist entries no longer read an "
                         "identity field: %r" % stale)


if __name__ == "__main__":
    unittest.main()
