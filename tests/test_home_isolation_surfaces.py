#!/usr/bin/env python3
"""#117: a redirected HELM_HOME must redirect the BOOT-SCOPED surfaces too.

THE HOLE, measured before this lane: HELM_HOME appeared nowhere in
helm/seats.py, and roster_path() derived from chat_dir(), which read only
HELM_CHAT_DIR. So a probe that set HELM_HOME to a temp dir — the exact move
every isolated-looking harness makes — still resolved the LIVE fleet bus at
/dev/shm/helm-chat: the roster (the identity index), the claims file, every
room and DM. Read-side that poisons measurements; write-side it corrupts who
the fleet believes each seat IS. The near-miss is already in the tree:
tests/test_gc.py setUp records scan() returning 24,001 live-fleet victims
that `gc --apply` would have deleted from under running seats, purely because
HELM_CHAT_DIR was the one env its fixture had not planted.

THE LAW UNDER TEST (home.surface_dir, consumed by chat.chat_dir and
multiplayer.multiplayer_dir): explicit HELM_<X>_DIR wins; a REDIRECTED
HELM_HOME (realpath differs from the default ~/.helm) pulls the surface under
itself; the default root keeps the shared tmpfs bus.

FIXTURE SHAPE: the "live bus" is a tempdir stand-in patched over
chat.DEFAULT_DIR — no arm here ever touches the real /dev/shm/helm-chat, and
the positive control redirects home.default_home instead of un-setting
HELM_HOME so that no write can ever land in the real ~/.helm either.
"""
import json
import os
import shutil
import tempfile
import unittest
import unittest.mock as mock

from helm import chat, home, multiplayer, seats


def _bytes(path):
    with open(path, "rb") as f:
        return f.read()


class SurfaceIsolationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-iso-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.live = os.path.join(self.tmp, "live-bus")     # /dev/shm stand-in
        self.root = os.path.join(self.tmp, "probe-home")   # redirected HELM_HOME
        os.makedirs(self.live)
        self.seeded = json.dumps({"live-seat": {"session": "s-live"}},
                                 indent=1).encode()
        with open(os.path.join(self.live, ".roster.json"), "wb") as f:
            f.write(self.seeded)

    def _env(self, **extra):
        """Empty string == unset for every consumer (tests/__init__._plant),
        so blanking the harness plants exercises the real fallthrough."""
        base = {"HELM_CHAT_DIR": "", "MELD_CHAT_DIR": "",
                "HELM_MULTIPLAYER_DIR": "", "MELD_MULTIPLAYER_DIR": "",
                "HELM_HOME": "", "MELD_HOME": ""}
        base.update(extra)
        return mock.patch.dict(os.environ, base)

    def test_a_redirected_root_pulls_the_roster_write_off_the_live_bus(self):
        """THE WRITE ARM (#117's brief): temp HELM_HOME, one seat-row write —
        the live bus stays byte-identical while the isolated roster gains the
        row. A read-only arm would pass today by accident; this one cannot."""
        with self._env(HELM_HOME=self.root), \
             mock.patch.object(chat, "DEFAULT_DIR", self.live):
            self.assertEqual(chat.chat_dir(),
                             os.path.join(self.root, "helm-chat"))
            seats.write_roster("probe-seat", presence_beat=False)
            iso = os.path.join(self.root, "helm-chat", ".roster.json")
            self.assertTrue(os.path.exists(iso),
                            "isolated roster was never written")
            self.assertIn(b"probe-seat", _bytes(iso))
        self.assertEqual(_bytes(os.path.join(self.live, ".roster.json")),
                         self.seeded,
                         "the LIVE bus stand-in was mutated by an isolated write")

    def test_positive_control_the_default_root_write_hits_the_bus_standin(self):  # noqa: VACUOUS_ASSERTION — the NotEqual is paired with assertIn(probe-seat) on the same bytes; mutation M3 (comparison inverted) reddens this arm
        """THE NAMED CONTROL (#117's brief): prove the byte-unchanged
        instrument CAN fire. default_home is redirected to match HELM_HOME —
        the un-redirected arm — so the same write lands on the bus stand-in
        and the seeded bytes change. Nothing here touches the real ~/.helm:
        both sides of the comparison live in this test's tempdir."""
        control_home = os.path.join(self.tmp, "control-home")
        with self._env(HELM_HOME=control_home), \
             mock.patch.object(home, "default_home",
                               return_value=control_home), \
             mock.patch.object(chat, "DEFAULT_DIR", self.live):
            self.assertEqual(chat.chat_dir(), self.live)
            seats.write_roster("probe-seat", presence_beat=False)
        changed = _bytes(os.path.join(self.live, ".roster.json"))
        self.assertNotEqual(changed, self.seeded,
                            "control write left the bus untouched — the "
                            "unchanged-assertion above proves nothing")
        self.assertIn(b"probe-seat", changed)

    def test_an_explicit_CHAT_DIR_outranks_the_redirected_root(self):  # noqa: VACUOUS_ASSERTION — mutation M2 (explicit arm dead) reddens exactly this test; the path equality IS the observable
        explicit = os.path.join(self.tmp, "explicit-chat")
        with self._env(HELM_HOME=self.root, HELM_CHAT_DIR=explicit):
            self.assertEqual(chat.chat_dir(), explicit)

    def test_the_default_root_spelled_explicitly_keeps_the_shared_bus(self):  # noqa: VACUOUS_ASSERTION — mutation M3 (comparison inverted) reddens this arm; deliberately read-only so no write can touch the real bus
        """HELM_HOME set to the literal default is NOT an isolation request —
        a live seat exporting the default spelling must stay on the fleet
        bus, or setting the 'same' value would split the fleet. Read-only."""
        with self._env(HELM_HOME=os.path.join(os.path.expanduser("~"),
                                              ".helm")):
            self.assertEqual(chat.chat_dir(), chat.DEFAULT_DIR)

    def test_multiplayer_follows_the_same_law(self):  # noqa: VACUOUS_ASSERTION — mutations M1 and M3 both redden this arm; second consumer of the one derivation
        with self._env(HELM_HOME=self.root):
            self.assertEqual(multiplayer.multiplayer_dir(),
                             os.path.join(self.root, "helm-multiplayer"))
        with self._env():
            self.assertEqual(multiplayer.multiplayer_dir(),
                             multiplayer.DEFAULT_DIR)


if __name__ == "__main__":
    unittest.main()
