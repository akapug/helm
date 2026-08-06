"""The CLI presence verbs must resolve THIS seat by the identity law.

`helm chat status|pending|mute` each resolved their own seat with

    chat._seat_flag(args) or derive_seat(_env_session())

which skips `acting_seat` — the documented "identity law for every
delivery/presence path" — and lands on derive_seat's rung 2 with NO CWD.

The failure is counter-intuitive enough to be worth pinning: derive_seat with a
session and no cwd calls auto_name(session, None), which cannot resolve a
project and yields the bare family floor ("claude"). With NO session at all it
would fall through to chat.whoname() and get the RIGHT answer. So supplying
the session id is what breaks it — more information, worse answer.

Measured live 2026-07-29 on a resumed orca-launched seat (no HELM_CHAT_NAME in
environ, which is the normal state for a bare `claude --resume`):

    helm chat status   -> "no roster row for 'claude' yet"   (its own row exists)
    helm chat pending  -> reported for 'claude'              (26 rows waited)

A presence verb that cannot find its own row is not cosmetic: `pending` is the
seat's inbox-truth surface, so it read empty while real addressed rows sat
unconsumed.

COLLECTION NOTE (2026-07-30). These five guards were written pytest-style, as
bare module-level functions, so `unittest discover` — which is what the gate
runs — never collected them and they contributed ZERO to every receipt that has
gated a land. The most recent commit to touch this file ("the seat-identity
fixture cwd goes synthetic — never-track was red on main") edited a test that
had never executed. Converted to TestCase; tests/test_suite_collection.py now
fails if it recurs.
"""

import inspect
import os
import tempfile
import unittest
from unittest import mock

from helm import seats

SID = "cf8ce076-9fd5-47b7-a0fd-aba113bc8592"


def _source(_name="seats"):
    """THE WHOLE SEATS PACKAGE, not the facade module.

    This read `inspect.getsource(seats)` — one module's text — and the
    seats.py split moved the CLI verbs into helm/seats_cli.py and the
    identity resolvers into helm/seats_identity.py. The facade is now 580
    lines of re-exports, so every name this file searches for left it. The
    invariant was never about a FILE; it is about what the seats surface
    does."""
    if hasattr(seats, "_chat_cli"):
        return inspect.getsource(seats._chat_cli)
    d = os.path.dirname(os.path.abspath(seats.__file__))
    names = sorted(f for f in os.listdir(d)
                   if f == "seats.py" or f.startswith("seats_"))
    assert len(names) >= 3, "seats package scan found almost nothing: %s" % names
    return "".join(open(os.path.join(d, f), encoding="utf-8").read()
                   for f in names)


class _NoChatName(unittest.TestCase):
    """HELM_CHAT_NAME must be absent: its presence is exactly the condition
    under which the defect cannot reproduce."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._env = mock.patch.dict(
            os.environ, {"HELM_CHAT_DIR": self._tmp.name})
        self._env.start()
        self.addCleanup(self._env.stop)
        os.environ.pop("HELM_CHAT_NAME", None)


class DeriveSeatTest(_NoChatName):
    def test_derive_seat_with_session_and_no_cwd_yields_the_bare_floor(self):
        """The defect's mechanism, pinned directly. If this ever stops being
        true, the call sites below are no longer load-bearing and the fix can be
        re-argued rather than silently kept."""
        bare = seats.derive_seat(SID)
        # Synthetic public fixture: cwd SHAPE matters here, not a real path.
        withcwd = seats.derive_seat(SID, "/u/someone/example-platform")
        self.assertNotEqual(
            bare, withcwd,
            "derive_seat must be cwd-sensitive for this bug to exist; got %r "
            "both ways" % bare)

    def test_acting_seat_beats_a_cwd_less_derive(self):
        """acting_seat covers all three rungs, so it is right even when the
        caller has no cwd to give — which is why the fix uses it rather than
        threading cwd into derive_seat."""
        self.assertNotEqual(seats.acting_seat(SID, seats.safe_cwd()),
                            seats.derive_seat(SID))


class PresenceVerbShapeTest(unittest.TestCase):
    def test_no_presence_verb_resolves_its_own_seat_by_bare_derive_seat(self):
        """The CLASS, not the three instances. A new presence verb copying the
        old shape fails here rather than shipping a seat that cannot see its own
        row.

        The banned shape is specifically `_seat_flag(args) or derive_seat(...)`:
        an explicit --seat is fine (it is an assertion, guarded elsewhere), and
        derive_seat itself stays legitimate for naming OTHER rows.
        """
        bad = [ln.strip() for ln in _source().splitlines()
               if "_seat_flag(args)" in ln and "derive_seat" in ln]
        self.assertEqual(
            bad, [],
            "these resolve their own seat below the identity law — use "
            "acting_seat(_env_session(), safe_cwd()):\n  " + "\n  ".join(bad))

    def test_the_guard_can_actually_fail(self):
        """Vacuity guard: the assertion above passes trivially if the source
        read comes back empty or the pattern can never match. Prove both halves
        are real — the source is non-trivial AND the pattern matches when
        present."""
        src = _source()
        self.assertGreater(len(src), 10000,
                           "source read is too small to be the real module")
        self.assertIn("_seat_flag", src,
                      "the pattern's anchor is absent from the source")
        probe = ("        seat = chat._seat_flag(args) or "
                 "derive_seat(_env_session())")
        self.assertTrue("_seat_flag(args)" in probe and "derive_seat" in probe,
                        "the matcher would not catch the original defect")

    def test_acting_seat_is_used_by_the_three_repaired_verbs(self):
        """Assert the EFFECT (the fix is present), not merely the absence of the
        old shape — a refactor that deleted the verbs would satisfy the negative
        test alone."""
        fixed = [ln.strip() for ln in _source().splitlines()
                 if "_seat_flag(args)" in ln and "acting_seat" in ln]
        self.assertGreaterEqual(
            len(fixed), 3,
            "expected status/pending/mute to resolve via acting_seat; found %d"
            % len(fixed))


if __name__ == "__main__":
    unittest.main()
