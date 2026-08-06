#!/usr/bin/env python3
"""cell tests — hermetic. The dregg node leg is OPTIONAL and fail-open: the
node reader/writer is mocked or aimed at a dead port. The OPTIONAL a2a
transport degrades with no HELM_CELL_BIN. No meld binary is needed for
attestation — this module never attests (that is premise.py, native)."""
import contextlib
import io
import os
import shutil
import stat
import tempfile
import time
import unittest
from unittest import mock

import os as _os, sys as _sys  # noqa: E402
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
from tests._tmphome import home as _tmp_home  # noqa: E402
_tmp_home(prefix="helm-test-home-", var="HELM_HOME")

from helm import cell  # noqa: E402

CELL_HEX = "ab" * 32
TURN = "cd" * 32

ENV_KEYS = ("HELM_HOME", "HELM_CELL_BIN", "MELD_CELL_BIN", "HELM_NODE_URL",
            "HELM_CELL_PROFILE", "HELM_NODE_TOKEN", "HELM_NODE_PASSPHRASE",
            "HELM_ROSTER", "HELM_NODE_ANCHOR_FEE", "HELM_NODE_ANCHOR_TIMEOUT",
            "MELD_NODE_URL", "MELD_AGENT_PROFILE",
            "MELD_NODE_TOKEN", "MELD_NODE_PASSPHRASE", "MELD_ROSTER",
            "DREGG_NODE_URL", "DREGG_PROFILE",
            "DREGG_API_TOKEN", "DREGG_NODE_PASSPHRASE")

DEAD = "http://127.0.0.1:1"   # nothing listens — fail-open, fast


class CellBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-cell-")
        self.env_prior = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_NODE_URL"] = DEAD
        # PATH is snapshotted but NOT popped: the bare-name resolution tests
        # overwrite it (one leaves a bare "/usr/bin:/bin"), and without a
        # restore that clobber leaked into every later test in the process —
        # measured: test_seat_env_allowlist's grounding arm crashed
        # on shutil.which("claude")->None because its skipIf had already
        # answered at IMPORT time, before this file ran. The polluter owns it.
        self.path_prior = os.environ.get("PATH")

    def tearDown(self):
        for k, v in self.env_prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        if self.path_prior is None:
            os.environ.pop("PATH", None)
        else:
            os.environ["PATH"] = self.path_prior
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_cli(self, args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cell.cmd_cell(args)
        return rc, out.getvalue(), err.getvalue()


class NodeUrlTest(CellBase):
    def test_helm_wins_over_legacy_and_strips(self):
        os.environ["HELM_NODE_URL"] = "http://helm:1/"
        os.environ["MELD_NODE_URL"] = "http://old:2"
        self.assertEqual(cell.node_url(), "http://helm:1")

    def test_legacy_fallback_when_no_helm(self):
        os.environ.pop("HELM_NODE_URL", None)
        os.environ["MELD_NODE_URL"] = "http://old:2/"
        self.assertEqual(cell.node_url(), "http://old:2")

    def test_default(self):
        os.environ.pop("HELM_NODE_URL", None)
        self.assertEqual(cell.node_url(), cell.DEFAULT_NODE_URL)

    def test_profile_name_precedence(self):
        self.assertEqual(cell.profile_name(), "helm-agent")
        os.environ["MELD_AGENT_PROFILE"] = "legacy"
        self.assertEqual(cell.profile_name(), "legacy")
        os.environ["HELM_CELL_PROFILE"] = "new"
        self.assertEqual(cell.profile_name(), "new")
        self.assertEqual(cell.profile_name(default="helm-test"), "new")


class AnchorSubmitTest(CellBase):
    def test_accepted_returns_turn_hash(self):
        with mock.patch.object(cell, "post_json",
                               return_value={"accepted": True, "turn_hash": TURN}):
            turn, err = cell.anchor_submit("ab" * 32)
        self.assertIsNone(err)
        self.assertEqual(turn, TURN)

    def test_refusal_is_failopen_reason(self):
        with mock.patch.object(cell, "post_json",
                               return_value={"accepted": False, "error": "locked"}):
            turn, err = cell.anchor_submit("ab" * 32)
        self.assertIsNone(turn)
        self.assertIn("locked", err)

    def test_non_dict_json_fails_open_never_raises(self):
        # a valid JSON list/string/number is NOT acceptance — guard with
        # isinstance(dict) before .get(), fail open, never raise AttributeError.
        for resp in ([1, 2, 3], "ok", 42, [{"turn_hash": TURN}]):
            with mock.patch.object(cell, "post_json", return_value=resp):
                turn, err = cell.anchor_submit("ab" * 32)
            self.assertIsNone(turn, resp)
            self.assertIn("non-object", err)

    def test_anchor_declares_coordination_fee_zero_env_overridable(self):
        # (Stage B): the attest anchor is a COORDINATION turn (EmitEvent-only,
        # no balance_change), so it declares fee=0 and rides dregg's
        # coordination-exempt admission free — no cell drain, no faucet grant, no
        # [unsigned] throttle. Env-overridable via HELM_NODE_COORD_FEE for a node
        # that has NOT opted into the exempt class.
        seen = {}

        def fake_post(url, payload, timeout=8, headers=None, **kw):
            seen["fee"] = payload.get("fee")
            return {"accepted": True, "turn_hash": TURN}

        self.assertEqual(cell.DEFAULT_COORD_FEE, 0)
        with mock.patch.object(cell, "post_json", fake_post):
            cell.anchor_submit("ab" * 32)
        self.assertEqual(seen["fee"], 0)
        os.environ["HELM_NODE_COORD_FEE"] = "1000"
        try:
            with mock.patch.object(cell, "post_json", fake_post):
                cell.anchor_submit("ab" * 32)
            self.assertEqual(seen["fee"], 1000)
        finally:
            os.environ.pop("HELM_NODE_COORD_FEE", None)

    def test_turn_fee_zeroes_coordination_only_not_economic(self):
        # The client half of the leash: fee=0 ONLY for the coordination class
        # (all effects emit_event, no balance_change); an economic effect or any
        # balance_change keeps the full anchor_fee() so economic turns are never
        # zeroed — mirrors dregg Turn::is_coordination.
        coord = [{"method": "attest",
                  "effects": [{"kind": "emit_event", "topic": "t"}]}]
        econ_bc = [{"method": "pay",
                    "balance_change": {"cell": "x", "delta": -5},
                    "effects": [{"kind": "emit_event", "topic": "t"}]}]
        econ_eff = [{"method": "x",
                     "effects": [{"kind": "transfer"},
                                 {"kind": "emit_event"}]}]
        self.assertTrue(cell.is_coordination_actions(coord))
        self.assertEqual(cell.turn_fee(coord), 0)
        self.assertFalse(cell.is_coordination_actions(econ_bc))
        self.assertEqual(cell.turn_fee(econ_bc), cell.anchor_fee())
        self.assertFalse(cell.is_coordination_actions(econ_eff))
        self.assertEqual(cell.turn_fee(econ_eff), cell.anchor_fee())
        # empty forest and effect-less actions are NOT coordination
        self.assertFalse(cell.is_coordination_actions([]))
        self.assertFalse(cell.is_coordination_actions([{"method": "x",
                                                        "effects": []}]))

    def test_a_dead_port_is_failopen_AND_says_it_was_the_transport(self):
        """Renamed and sharpened. This asserted the literal word "unreachable",
        which was the VAGUE word being fixed: the old message said
        "unreachable/locked" for every failure, including an HTTP 401 from a node
        that was demonstrably up. Pinning that word would have blocked the fix
        while proving nothing — a dead port and a locked node produced identical
        text, so the assertion passed in both cases.

        What actually matters here is the two things the caller needs: fail open
        (None, never raise) and a reason naming the TRANSPORT, so a reader is not
        sent to check auth for a socket problem."""
        turn, err = cell.anchor_submit("ab" * 32)
        self.assertIsNone(turn)
        self.assertIn("refused", err.lower())
        self.assertNotIn("auth problem", err)

    def test_bearer_and_digest_word_wire(self):
        seen = {}

        def fake_post(url, payload, timeout=8, headers=None, **kw):
            seen["url"] = url
            seen["payload"] = payload
            seen["headers"] = headers or {}
            return {"accepted": True, "turn_hash": TURN}

        os.environ["HELM_NODE_TOKEN"] = "tok"
        with mock.patch.object(cell, "post_json", fake_post):
            cell.anchor_submit("ab" * 32, memo="helm-attest:v2:law-x")
        self.assertTrue(seen["url"].endswith(cell.ANCHOR_ENDPOINT))
        self.assertEqual(seen["headers"].get("Authorization"), "Bearer tok")
        eff = seen["payload"]["actions"][0]["effects"][0]
        self.assertEqual(eff["kind"], "emit_event")
        self.assertEqual(eff["data"], ["ab" * 32])   # the 64-hex record word
        self.assertEqual(seen["payload"]["memo"], "helm-attest:v2:law-x")

    def test_anchor_label_is_honest_node_anchored(self):
        os.environ["HELM_NODE_URL"] = "http://node:9"
        label = cell.anchor_label(TURN)
        self.assertIn("dregg node http://node:9 anchored", label)
        self.assertIn(TURN, label)
        self.assertNotIn("signed", label)   # never a signer claim


class VerifyAnchorTest(CellBase):
    def test_proof_present_is_turn_observed_not_payload_bound(self):
        # a present proof means the turn EXISTS, not that it commits this
        # record's hash — the detail says so honestly.
        with mock.patch.object(cell, "get_json",
                               return_value={"turn_hash": TURN, "proof_len": 12}):
            ok, detail = cell.verify_anchor(TURN)
        self.assertTrue(ok)
        self.assertIn("turn present", detail)
        self.assertIn("payload binding unavailable", detail)

    def test_receipt_present_is_confirmed(self):
        def by_endpoint(url, timeout=4):
            return [{"turn_hash": TURN}] if "starbridge" in url else None
        with mock.patch.object(cell, "get_json", by_endpoint):
            ok, detail = cell.verify_anchor(TURN)
        self.assertTrue(ok)
        self.assertIn("receipt present", detail)

    def test_unreachable_is_unverified(self):
        with mock.patch.object(cell, "get_json", return_value=None):
            ok, detail = cell.verify_anchor(TURN)
        self.assertFalse(ok)
        self.assertIn("unreachable", detail)


class BinPathTest(CellBase):
    def test_explicit_env_wins(self):
        os.environ["HELM_CELL_BIN"] = "/some/where/cellbin"
        self.assertEqual(cell.bin_path(), "/some/where/cellbin")

    def test_legacy_bin_env_fallback(self):
        os.environ["MELD_CELL_BIN"] = "/legacy/cellbin"
        self.assertEqual(cell.bin_path(), "/legacy/cellbin")

    def test_no_auto_resolution_returns_none(self):
        # never a PATH probe, never a sibling-build guess — unset => None
        self.assertIsNone(cell.bin_path())
        self.assertEqual(cell.bin_status()["state"], "unset")

    def test_bin_status_distinguishes_missing_and_non_executable(self):
        missing = os.path.join(self.tmp, "deleted-signer\x1b[2J")
        os.environ["HELM_CELL_BIN"] = missing
        status = cell.bin_status()
        self.assertEqual((status["configured"], status["usable"], status["state"]),
                         (True, False, "missing"))
        self.assertIn("path does not exist", status["reason"])
        self.assertNotIn(missing, status["reason"])

        directory = os.path.join(self.tmp, "signer-dir")
        os.mkdir(directory)
        os.environ["HELM_CELL_BIN"] = directory
        self.assertEqual(cell.bin_status()["state"], "not_file")

        os.environ["HELM_CELL_BIN"] = missing
        with open(missing, "w") as f:
            f.write("#!/bin/sh\nexit 0\n")
        os.chmod(missing, 0o600)
        status = cell.bin_status()
        self.assertEqual(status["state"], "not_executable")
        self.assertIn("not executable", status["reason"])
        os.chmod(missing, 0o700)
        self.assertEqual(cell.bin_status()["state"], "ready")

    def test_run_bin_degrades_without_binary(self):
        rc, out, err = cell.run_bin(["roster"])
        self.assertIsNone(rc)
        self.assertIn("a2a transport unavailable", err)


class BareNameIsPathDependentTest(CellBase):
    """seat.py's module doc taught `HELM_CELL_BIN=dregg-client-sign` — a BARE
    name — while the launcher exports DREGG_SIGNER_DEFAULT, an ABSOLUTE path.
    An operator copying the doc got usable:False (the incident cell._resolve's
    docstring records). The runtime half was cured by resolving slash-free
    names through PATH; these pin WHY the launcher still exports an absolute,
    so the doc line cannot drift back to describing a bare export."""

    def signer(self, name="dregg-client-sign"):
        """A real executable in an otherwise-empty dir — returns (dir, path)."""
        d = os.path.join(self.tmp, "bin")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, name)
        with open(path, "w") as f:
            f.write("#!/bin/sh\nexit 0\n")
        os.chmod(path, 0o700)
        return d, path

    def test_a_bare_name_resolves_through_PATH(self):
        d, path = self.signer()
        os.environ["PATH"] = d + os.pathsep + "/usr/bin:/bin"
        os.environ["HELM_CELL_BIN"] = "dregg-client-sign"
        self.assertEqual(cell._resolve("dregg-client-sign"), path)
        self.assertTrue(cell._usable("dregg-client-sign"))
        self.assertEqual(cell.bin_status()["state"], "ready")

    def test_the_bare_name_is_PATH_dependent_and_the_absolute_is_not(self):
        """THE MEASUREMENT THE DOC NOW STATES, and the reason the launcher
        exports a resolved path: the launched process's PATH is not the one
        that verified the binary. Both forms name the SAME file here — only
        the resolution differs — so this isolates PATH-dependence and nothing
        else."""
        d, path = self.signer()
        # POSITIVE CONTROL, unconditional: with the dir on PATH the bare name
        # IS usable, so the failure below is PATH and not a broken fixture.
        os.environ["PATH"] = d + os.pathsep + "/usr/bin:/bin"
        self.assertTrue(cell._usable("dregg-client-sign"))

        os.environ["PATH"] = "/usr/bin:/bin"
        self.assertIsNone(cell._resolve("dregg-client-sign"))
        self.assertFalse(cell._usable("dregg-client-sign"))
        # ...and the absolute form is untouched by the same PATH change
        self.assertTrue(cell._usable(path))

    def test_the_launcher_exports_an_absolute_path_not_a_bare_name(self):
        """THE DOC-VS-CODE PIN. The divergence this lane closed was a doc line
        promising a bare export while the code exported an absolute; without a
        test the prose can drift back and nothing fails. Asserts the CODE's
        commitment, so the doc has something to be true ABOUT."""
        from helm import seat
        self.assertTrue(os.path.isabs(seat.DREGG_SIGNER_DEFAULT),
                        "the launcher must export a RESOLVED path — a bare "
                        "name is PATH-dependent in a process whose PATH the "
                        "launcher does not control")
        # UNCONDITIONAL, and it was not on the first cut: an earlier draft guarded this on
        # `hasattr(seat, "proxy_seat_env")` — a function that does not exist —
        # so the assertion carrying this test's whole purpose was skipped and
        # the test passed for not running it. A conditional around the pin is
        # a pin that reports success for being absent.
        env = seat._seat_env("kimi", os.path.join(self.tmp, "cfg"))
        self.assertEqual(env["HELM_CELL_BIN"], seat.DREGG_SIGNER_DEFAULT)
        self.assertTrue(os.path.isabs(env["HELM_CELL_BIN"]),
                        "the exported value must be an absolute path, not the "
                        "bare name the doc used to promise")


class BuildEnvTest(CellBase):
    def test_helm_maps_onto_meld_and_dregg_names(self):
        # the dregg-native signer (dregg-client-sign) reads DREGG_*; the
        # legacy meld-style bin reads MELD_* — one HELM_* feeds both
        os.environ.update(HELM_NODE_URL="http://helm:1", HELM_CELL_PROFILE="p1",
                          HELM_NODE_TOKEN="tok", HELM_NODE_PASSPHRASE="pw")
        env = cell.build_env()
        self.assertEqual(env["MELD_NODE_URL"], "http://helm:1")
        self.assertEqual(env["DREGG_NODE_URL"], "http://helm:1")
        self.assertEqual(env["MELD_AGENT_PROFILE"], "p1")
        self.assertEqual(env["DREGG_PROFILE"], "p1")
        self.assertEqual(env["MELD_NODE_TOKEN"], "tok")
        self.assertEqual(env["DREGG_API_TOKEN"], "tok")
        self.assertEqual(env["DREGG_NODE_PASSPHRASE"], "pw")

    def test_absent_helm_leaves_direct_dregg_env_untouched(self):
        # env2: no HELM_NODE_TOKEN set — a directly-exported DREGG_API_TOKEN
        # survives; unset HELM vars never mint empty DREGG ones
        os.environ["DREGG_API_TOKEN"] = "direct"
        env = cell.build_env()
        self.assertEqual(env["DREGG_API_TOKEN"], "direct")
        self.assertNotIn("DREGG_NODE_PASSPHRASE", env)

    def test_signer_env_file_fills_gaps_but_os_environ_wins(self):
        # An operator's signer.env configures the signer's runtime posture (e.g.
        # a marshal-only devnet's DREGG_ALLOW_UNAUDITED_PQ) ONCE, read fresh on
        # every signed turn so a running seat picks it up with no relaunch.
        # Absent file => no-op; the file fills GAPS only, real env WINS.
        self.assertNotIn("DREGG_ALLOW_UNAUDITED_PQ", cell.build_env())  # absent file
        sp = os.path.join(cell.home.helm_home(), "signer.env")
        os.makedirs(os.path.dirname(sp), exist_ok=True)
        with open(sp, "w", encoding="utf-8") as f:
            f.write("# devnet marshal-only posture\n"
                    "DREGG_ALLOW_UNAUDITED_PQ=1\n"
                    "\n"
                    "DREGG_API_TOKEN=fromfile\n")
        os.environ["DREGG_API_TOKEN"] = "fromenv"   # explicit env must WIN
        env = cell.build_env()
        self.assertEqual(env["DREGG_ALLOW_UNAUDITED_PQ"], "1")  # gap filled
        self.assertEqual(env["DREGG_API_TOKEN"], "fromenv")     # env beats file

    def test_signer_env_file_explicit_path_and_malformed_lines(self):
        sp = os.path.join(self.tmp, "custom-signer.env")
        with open(sp, "w", encoding="utf-8") as f:
            f.write("# comment\nNOEQUALS\n  DREGG_ALLOW_UNAUDITED_PQ = 1 \n")
        os.environ["HELM_CELL_ENV_FILE"] = sp
        try:
            env = cell.build_env()
            self.assertEqual(env["DREGG_ALLOW_UNAUDITED_PQ"], "1")  # trimmed
            self.assertNotIn("NOEQUALS", env)                       # skipped
        finally:
            os.environ.pop("HELM_CELL_ENV_FILE", None)


class SigningIdentityTest(CellBase):
    """THE FOUR POPULATIONS, because this fix is only correct if it separates
    them — and the owner's own sessions are the one it must NOT touch.

    `launch.py` already states the law ("a child speaking as `seat` must sign
    as that same seat, not its launcher") and enforces it at SPAWN time only.
    A seat adopted or started by another door never passes that gate and then
    inherits whatever the ambient environment claims — on the owner's box a
    shell rc exporting his own profile, which is how agent rows came to render
    as signed by him. This moves the law to SIGNING time.
    """

    def _as(self, seat, roster):
        """Patch this process's derived identity and the roster it is checked
        against. Returns the mock context."""
        return (mock.patch("helm.meld._self_seat", return_value=seat),
                mock.patch("helm.seats.roster", return_value=roster))

    ACTOR = {"helm-claude-2": {"home_room": "helm"},
             "kimi": {"home_room": "helm"}}
    OBSERVED = {"observer-session": {"cwd": "/tmp/x"}}     # sid, but no home_room

    def test_a_LAUNCHED_seat_whose_env_AGREES_signs_normally(self):
        """Population 1 — the correct case, and the control for every refusal
        below: agreement is not a conflict and must stay silent."""
        os.environ["HELM_CELL_PROFILE"] = "kimi"
        a, b = self._as("kimi", self.ACTOR)
        with a, b:
            profile, refusal = cell.signing_identity()
        self.assertEqual(profile, "kimi")
        self.assertIsNone(refusal)

    def test_an_ADOPTED_seat_with_an_AMBIENT_OWNER_profile_REFUSES(self):
        """Population 2 — the critical case. The process can prove it is helm-claude-2 and
        the environment names the OWNER. Signing as him attributes the row to a
        person who did not write it; signing as helm-claude-2 is impossible
        without that seat's key. So it refuses, and the reason names BOTH."""
        os.environ["HELM_CELL_PROFILE"] = "owner-profile"
        a, b = self._as("helm-claude-2", self.ACTOR)
        with a, b:
            profile, refusal = cell.signing_identity()
        self.assertIsNone(profile, "signed anyway under a borrowed identity")
        self.assertIn("helm-claude-2", refusal)
        self.assertIn("owner-profile", refusal)

    def test_the_OWNERS_OWN_SESSION_is_not_a_signing_identity_and_still_signs(self):
        """Population 3 — the case a naive fix BREAKS, which is why the actor
        gate exists. The owner's ad-hoc sessions ARE rostered with a session id,
        so a rule keyed on "is the identity derivable" would resolve
        that session name, disagree with his ambient profile, and strand his own
        rows UNSIGNED. Fleet actors carry `home_room` (minted when a session
        JOINS a room); observed sessions do not. A home_room-less row is not a
        signing identity, so no conflict fires and he keeps signing as
        himself."""
        os.environ["HELM_CELL_PROFILE"] = "owner-profile"
        a, b = self._as("observer-session", self.OBSERVED)
        with a, b:
            profile, refusal = cell.signing_identity()
        self.assertEqual(profile, "owner-profile",
                         "the owner's own session stopped signing as himself")
        self.assertIsNone(refusal)

    def test_an_EXPLICIT_profile_is_STATED_INTENT_and_wins(self):
        """Population 4 — `--seat kimi` signing as kimi is a caller
        deliberately speaking for someone, not an inherited accident. Intent
        outranks the conflict rule; that distinction is the whole reason this
        is not a precedence inversion."""
        os.environ["HELM_CELL_PROFILE"] = "owner-profile"
        a, b = self._as("helm-claude-2", self.ACTOR)
        with a, b:
            profile, refusal = cell.signing_identity("kimi")
        self.assertEqual(profile, "kimi")
        self.assertIsNone(refusal)

    def test_an_UNROSTERED_name_is_not_an_identity(self):
        """The `nobody-yet` edge: a row with neither runtime nor session is
        unreachable through seat_for_session, and a name absent from the roster
        entirely can never be an actor. Absence must read as "no identity",
        never as "no home_room therefore fine to trust the name"."""
        os.environ["HELM_CELL_PROFILE"] = "owner-profile"
        a, b = self._as("nobody-yet", {})
        with a, b:
            self.assertEqual(cell.derived_seat(), "")
            profile, refusal = cell.signing_identity()
        self.assertEqual(profile, "owner-profile")
        self.assertIsNone(refusal)

    def test_an_UNREADABLE_roster_is_not_permission(self):
        """Fail-CLOSED on the identity question. If the roster cannot be read
        we cannot prove actor-ness, so the name is not a signing identity —
        the same direction every other refusal here takes."""
        os.environ["HELM_CELL_PROFILE"] = "owner-profile"
        # POSITIVE CONTROL on the SAME observable, first: a READABLE roster
        # yields the name, so the "" below is a verdict about the failure and
        # not a function that only knows one answer.
        with mock.patch("helm.meld._self_seat", return_value="helm-claude-2"), \
                mock.patch("helm.seats.roster", return_value=self.ACTOR):
            self.assertEqual(cell.derived_seat(), "helm-claude-2")
        with mock.patch("helm.meld._self_seat", return_value="helm-claude-2"), \
                mock.patch("helm.seats.roster", side_effect=OSError("boom")):
            self.assertEqual(cell.derived_seat(), "")


class A2ATransportTest(CellBase):
    STUB = ("#!/bin/sh\n"
            'echo "argv:$@" >> "$STUB_LOG"\n'
            'echo "MELD_NODE_URL=$MELD_NODE_URL" >> "$STUB_LOG"\n'
            'echo "DREGG_NODE_URL=$DREGG_NODE_URL" >> "$STUB_LOG"\n'
            "exit 0\n")

    def write_stub(self):
        path = os.path.join(self.tmp, "cellbin-stub")
        with open(path, "w") as f:
            f.write(self.STUB)
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
        os.environ["HELM_CELL_BIN"] = path
        os.environ["STUB_LOG"] = os.path.join(self.tmp, "stub.log")
        return path

    def tearDown(self):
        os.environ.pop("STUB_LOG", None)
        super().tearDown()

    def test_passthrough_runs_when_binary_present(self):
        self.write_stub()
        os.environ["HELM_NODE_URL"] = "http://helm:1"
        rc, _, _ = self.run_cli(["roster", "--json"])
        self.assertEqual(rc, 0)
        with open(os.environ["STUB_LOG"]) as f:
            log = f.read()
        self.assertIn("argv:roster --json", log)
        self.assertIn("MELD_NODE_URL=http://helm:1", log)   # env2 mapping
        self.assertIn("DREGG_NODE_URL=http://helm:1", log)  # dregg-native name

    def test_passthrough_degrades_without_binary(self):
        rc, _, err = self.run_cli(["send", "--to", CELL_HEX, "x"])
        self.assertEqual(rc, 1)
        self.assertIn("a2a transport unavailable", err)

    def test_unknown_verb_and_usage(self):
        rc, _, _ = self.run_cli([])
        self.assertEqual(rc, 2)
        rc, _, err = self.run_cli(["frobnicate"])
        self.assertEqual(rc, 2)
        self.assertIn("unknown verb", err)


class StatusTest(CellBase):
    def test_unreachable_node_is_graceful(self):
        os.environ["HELM_ROSTER"] = os.path.join(self.tmp, "roster.toml")
        with open(os.environ["HELM_ROSTER"], "w") as f:
            f.write('interval_secs = 60\n[[cell]]\nid = "%s"\nlabel = "t1"\n' % CELL_HEX)
        rc, out, _ = self.run_cli(["status"])
        self.assertEqual(rc, 1)
        self.assertIn("UNREACHABLE", out)
        self.assertIn("1 cell (t1)", out)
        self.assertIn("a2a transport OFF", out)

    def test_live_node_reports_head(self):
        with mock.patch.object(cell, "get_json", return_value=[
                {"chain_index": 5, "finality": "tentative"}]):
            rc, out, _ = self.run_cli(["status"])
        self.assertEqual(rc, 0)
        self.assertIn("node LIVE", out)
        self.assertIn("chain head 5", out)

    def test_live_node_with_configured_missing_signer_is_unavailable(self):
        os.environ["HELM_CELL_BIN"] = os.path.join(self.tmp, "deleted-signer")
        with mock.patch.object(cell, "get_json", return_value=[
                {"chain_index": 5, "finality": "tentative"}]):
            rc, out, _ = self.run_cli(["status"])
        self.assertEqual(rc, 1)
        self.assertIn("signer UNAVAILABLE", out)
        self.assertIn("path does not exist", out)
        self.assertNotIn("transport OFF", out)


class SignerFreshnessTest(unittest.TestCase):
    """Is the RUNNING signer the one we think we fixed?

    WHY (owner mandate): "is dregg signing just always broken after the next
    change we make... every time i look back on it after a few hours, it's
    broken again". It was not breaking repeatedly. The faucet fix — an exempt
    join must zero BOTH funding bounds, without which the first join still asks
    the faucet for a funded grant and dies on `rate limited: 1 request per cell
    per minute` — was committed at 17:44 while the deployed binary was built at
    16:52. Fifty-two minutes too old, unbuilt for twenty hours, and every helm
    surface reported the signer READY throughout, because presence (set, exists,
    is a file, is executable) was the only thing anything checked.

    Bug class: capability-asserted-from-declaration."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-signerfresh-")
        self.repo = os.path.join(self.tmp, "dregg")
        os.makedirs(os.path.join(self.repo, cell.SIGNER_CRATE))
        self._env = {k: os.environ.pop(k, None)
                     for k in ("HELM_CELL_BIN", "HELM_DREGG_REPO")}
        os.environ["HELM_DREGG_REPO"] = self.repo
        self._git("init", "-q", "-b", "main")
        self._git("config", "user.email", "t@t")
        self._git("config", "user.name", "t")

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _git(self, *a):
        import subprocess
        return subprocess.run(["git", "-C", self.repo] + list(a),
                              capture_output=True, text=True)

    def _commit(self, subject, body="x", ago=0):
        """`ago` seconds before now. EXPLICIT dates because git orders by
        commit time and a test makes several commits inside one second — the
        tie-break is then arbitrary and the test flaps rather than measures."""
        import subprocess as sp
        p = os.path.join(self.repo, cell.SIGNER_CRATE, "sign.rs")
        with open(p, "w") as f:
            # UNIQUE content per commit: identical bytes make `git commit` a
            # no-op, and an unchecked no-op turned this test GREEN against a
            # repo that only ever had one commit. Assert the effect.
            f.write("%s\n%s\n" % (body, subject))
        self._git("add", "-A")
        stamp = "%d +0000" % int(time.time() - ago)
        env = dict(os.environ, GIT_AUTHOR_DATE=stamp, GIT_COMMITTER_DATE=stamp)
        r = sp.run(["git", "-C", self.repo, "commit", "-q", "-m", subject],
                   capture_output=True, text=True, env=env)
        assert r.returncode == 0, "fixture commit failed: %s%s" % (r.stdout,
                                                                   r.stderr)

    def _signer(self, mtime_offset):
        """A fake signer binary whose mtime we control."""
        b = os.path.join(self.tmp, "dregg-client-sign")
        with open(b, "w") as f:
            f.write("#!/bin/sh\nexit 0\n")
        os.chmod(b, 0o755)
        t = time.time() + mtime_offset
        os.utime(b, (t, t))
        os.environ["HELM_CELL_BIN"] = b
        return b

    def test_a_signer_older_than_its_source_is_STALE_and_names_the_commit(self):
        """The whole point: not "something is wrong" but WHICH commit is
        missing, so the reader knows what capability they do not have."""
        self._commit("fix(signer): zero BOTH funding bounds")
        self._signer(-7200)                      # built two hours ago
        row = cell.signer_staleness()
        self.assertEqual(row["state"], "stale")
        self.assertIn("zero BOTH funding bounds", row["reason"])
        self.assertIn("NOT in the running binary", row["reason"])

    def test_stale_signer_reason_does_not_contain_private_tool_names(self):
        """World boundary: user-facing staleness reason must not leak private tool names
        (such as 'fab', 'fab build', or 'fabric') to public helm callers."""
        self._commit("fix(signer): test commit for world boundary check")
        self._signer(-7200)
        row = cell.signer_staleness()
        self.assertEqual(row["state"], "stale")
        reason = row["reason"]
        for forbidden in ("fab ", "fab build", "fabric"):
            self.assertNotIn(
                forbidden,
                reason,
                "user-facing reason string leaks private term %r: %r"
                % (forbidden, reason),
            )

    def test_a_signer_newer_than_its_source_is_current(self):
        self._commit("some old commit")
        self._signer(+60)
        self.assertEqual(cell.signer_staleness()["state"], "current")

    def test_it_finds_a_fix_that_never_LANDED_on_main(self):
        """THE LOAD-BEARING CASE, and the reason for `--all`.

        The commit that repairs a signer routinely lives on a LANE nobody
        merged — in the prior incident the cure sat on an unmerged lane
        while main knew nothing about it. A freshness check against main would
        have reported everything current with the fix one branch away, which is
        the same silence with extra steps."""
        self._commit("old thing on main", ago=7200)
        self._signer(-3600)                      # built AFTER main's tip
        self._git("checkout", "-q", "-b", "lane/unmerged")
        self._commit("fix(signer): the cure nobody merged", ago=0)
        self._git("checkout", "-q", "main")      # main does NOT have it
        row = cell.signer_staleness()
        self.assertEqual(row["state"], "stale",
                         "a fix on an unmerged lane still means the deployed "
                         "binary is missing it")
        self.assertIn("the cure nobody merged", row["reason"])

    def test_an_UNREADABLE_source_is_unknown_NEVER_current(self):
        """Unproven is not safe. A freshness check that failed open would
        recreate the exact silence it exists to break."""
        os.environ["HELM_DREGG_REPO"] = os.path.join(self.tmp, "nope")
        self._signer(-3600)
        row = cell.signer_staleness()
        self.assertEqual(row["state"], "unknown")
        self.assertNotEqual(row["state"], "current")

    def test_no_signer_configured_is_unknown_not_a_verdict(self):
        self._commit("anything")
        self.assertEqual(cell.signer_staleness()["state"], "unknown")


class SignerCapabilityTest(unittest.TestCase):
    """CAPABILITY, not presence: helm reported "signer ready" about a binary
    that was printing `sign ExportAbsent, verify ExportAbsent` on stderr of
    every single run. The answer was volunteered, unprompted, and nothing read
    it."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-signercap-")
        self._env = {k: os.environ.pop(k, None) for k in ("HELM_CELL_BIN",)}

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _fake(self, line):
        b = os.path.join(self.tmp, "sign")
        with open(b, "w") as f:
            f.write("#!/bin/sh\n")
            if line:
                f.write("echo '%s' >&2\n" % line)
            f.write("exit 1\n")
        os.chmod(b, 0o755)
        os.environ["HELM_CELL_BIN"] = b
        return b

    def test_absent_cores_read_as_marshal_only(self):
        self._fake("[client-sign] verified ML-DSA cores: "
                   "sign ExportAbsent, verify ExportAbsent")
        row = cell.signer_cores()
        self.assertEqual(row["state"], "marshal-only")
        self.assertEqual(row["absent"], ["sign", "verify"])

    def test_installed_cores_read_as_verified(self):
        self._fake("[client-sign] verified ML-DSA cores: "
                   "keygen Installed, sign Installed, verify Installed")
        row = cell.signer_cores()
        self.assertEqual(row["state"], "verified")
        self.assertEqual(row["absent"], [])

    def test_a_PARTIAL_core_set_is_not_verified(self):
        """The discriminating case. Reporting `verified` because SOME core is
        installed is how a probe stops being a measurement."""
        self._fake("[client-sign] verified ML-DSA cores: "
                   "keygen Installed, sign ExportAbsent, verify Installed")
        row = cell.signer_cores()
        self.assertEqual(row["state"], "marshal-only")
        self.assertEqual(row["absent"], ["sign"])

    def test_a_signer_that_says_nothing_is_unknown_not_verified(self):
        self._fake("")
        self.assertEqual(cell.signer_cores()["state"], "unknown")

    def test_no_signer_is_unknown(self):
        self.assertEqual(cell.signer_cores()["state"], "unknown")


if __name__ == "__main__":
    unittest.main()


class SignerEnvFillsTheBinGapTest(unittest.TestCase):
    """`signer.env` may name the signer binary, so a claude-direct seat signs
    without a relaunch and without per-command ritual.

    THE GAP THIS CLOSES, measured over 30 room rows: proxy-family
    seats carry HELM_CELL_BIN in their process env and signed 12/12; the two
    claude-direct seats must prefix it onto EVERY command, because their shell
    state does not persist between tool calls, and signed 7 of 16. The prior
    guidance said RELAUNCH was the sole path — it never was, but the alternative was
    a habit both seats forgot about half the time, and a capability that
    degrades silently unless a habit holds every time is not wired.

    THE LAW IS UNBROKEN. `bin_path` still refuses to GUESS: no PATH probe, no
    sibling-build search. An operator writing an absolute path into signer.env
    has told helm as deliberately as an export does, and `build_env` has always
    read that same file with these same gaps-only semantics."""

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="helm-cellhome-")
        self._env = dict(os.environ)
        os.environ["HELM_HOME"] = self.home
        for k in ("HELM_CELL_BIN", "MELD_CELL_BIN", "HELM_CELL_ENV_FILE"):
            os.environ.pop(k, None)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        shutil.rmtree(self.home, ignore_errors=True)

    def _write(self, text):
        with open(os.path.join(self.home, "signer.env"), "w",
                  encoding="utf-8") as f:
            f.write(text)

    def _tighten(self):
        """0700 dir + 0600 file — the only mode the file route may speak in."""
        os.chmod(self.home, 0o700)
        f = os.path.join(self.home, "signer.env")
        if os.path.exists(f):
            os.chmod(f, 0o600)

    def test_the_file_names_the_signer_when_the_env_does_not(self):
        self._write("# operator-written\nHELM_CELL_BIN=/usr/bin/true\n")
        self._tighten()
        self.assertEqual(cell.bin_path(), "/usr/bin/true")

    def test_a_group_writable_file_never_chooses_the_binary(self):
        """A blocking review finding. A file ANY same-uid seat can rewrite must
        not choose the binary EVERY seat execs — that is a cross-seat redirect
        the env route cannot produce, because one seat's environ is private
        while a shared file is a channel into every other seat's exec."""
        self._write("HELM_CELL_BIN=/usr/bin/true\n")
        self._tighten()
        self.assertEqual(cell.bin_path(), "/usr/bin/true")   # control: tight = honoured
        os.chmod(os.path.join(self.home, "signer.env"), 0o664)
        self.assertIsNone(cell.bin_path())

    def test_a_group_writable_DIRECTORY_is_refused_too(self):
        """A 0600 file inside a group-writable directory can be REPLACED
        wholesale — unlink then create — so checking the file alone would be a
        guard reading the wrong object. OpenSSH's StrictModes rule, same
        reason."""
        self._write("HELM_CELL_BIN=/usr/bin/true\n")
        self._tighten()
        self.assertEqual(cell.bin_path(), "/usr/bin/true")   # control
        os.chmod(self.home, 0o770)                            # file still 0600
        self.assertIsNone(cell.bin_path())

    def test_a_real_env_var_wins_even_when_the_file_is_wide_open(self):
        """The env route is untouched: a seat that sets HELM_CELL_BIN is
        choosing its OWN signer and no shared file is involved, so a hostile
        signer.env must not be able to disarm a seat that never consulted it."""
        self._write("HELM_CELL_BIN=/from/the/file\n")
        os.chmod(self.home, 0o777)
        os.environ["HELM_CELL_BIN"] = "/from/the/env"
        self.assertEqual(cell.bin_path(), "/from/the/env")

    def test_an_absent_file_is_not_a_risk_and_says_so(self):
        """An absent file answers False: there is no route to abuse, bin_path
        returns None a line later anyway, and calling it "writable by others"
        would be a guard inventing a threat. The file check short-circuits, so
        the directory's mode is only ever consulted for a file that EXISTS —
        which is the only case where the directory can be used to replace
        it."""
        # ABSENT FILE -> False, and it short-circuits before the directory is
        # ever examined, which is why an absent file under an unstattable
        # directory also answers False: there is no route either way.
        self._tighten()
        self.assertFalse(cell._signer_env_writable_by_others(
            os.path.join(self.home, "absent.env")))
        self.assertFalse(cell._signer_env_writable_by_others(
            "/nonexistent-dir-xyz/deeper/signer.env"))
        # CONTROL on the same observable: a PRESENT file under a loose
        # directory does answer True, so the Falses above are the absence
        # rule and not a predicate that never fires.
        self._write("HELM_CELL_BIN=/usr/bin/true\n")
        os.chmod(self.home, 0o770)
        self.assertTrue(cell._signer_env_writable_by_others(
            os.path.join(self.home, "signer.env")))
        # control on the same observable: a TIGHT real path answers False
        self._write("HELM_CELL_BIN=/usr/bin/true\n")
        self._tighten()
        self.assertFalse(cell._signer_env_writable_by_others(
            os.path.join(self.home, "signer.env")))

    def test_the_legacy_spelling_is_read_too(self):
        """The operator writes this file by hand and MELD_ is still accepted
        wherever else helm reads config; refusing it here would be a trap."""
        self._write("MELD_CELL_BIN=/usr/bin/true\n")
        self._tighten()
        self.assertEqual(cell.bin_path(), "/usr/bin/true")

    def test_a_real_env_var_still_wins(self):
        """Gaps ONLY — a seat that sets the var keeps exactly what it set, so
        the file can never silently redirect a seat to a different signer."""
        self._write("HELM_CELL_BIN=/from/the/file\n")
        os.environ["HELM_CELL_BIN"] = "/from/the/env"
        self.assertEqual(cell.bin_path(), "/from/the/env")

    def test_no_env_and_no_file_still_degrades_to_none(self):
        """The unconfigured deployment is unchanged: None, and the transport
        degrades exactly as before. Positive control first so this absence is
        a verdict rather than a function that can only return None."""
        self._write("HELM_CELL_BIN=/usr/bin/true\n")
        self._tighten()
        self.assertEqual(cell.bin_path(), "/usr/bin/true")   # control
        os.remove(os.path.join(self.home, "signer.env"))
        self.assertIsNone(cell.bin_path())

    def test_a_file_naming_nothing_usable_is_still_refused(self):
        """`_usable` remains the arbiter of whether what we were TOLD is real:
        a path that is not a regular executable file does not become one by
        being written down."""
        # UNCONDITIONAL POSITIVE CONTROL on the same observable: _usable says
        # YES to a real executable. Without it, a _usable that only ever
        # answered False would satisfy the assertion below.
        self._write("HELM_CELL_BIN=/usr/bin/true\n")
        self._tighten()
        self.assertTrue(cell._usable(cell.bin_path()))
        self._write("HELM_CELL_BIN=%s\n" % self.home)     # a directory
        self.assertEqual(cell.bin_path(), self.home)      # told...
        self.assertFalse(cell._usable(cell.bin_path()))   # ...but not usable


class BareNameResolutionTest(CellBase):
    """The doc-vs-code defect, measured on the original row: seat.py's module
    doc teaches HELM_CELL_BIN=dregg-client-sign (a BARE NAME) and the first
    consumer exec'd the value as a literal path — an operator following the
    doc exactly got usable:False. A slash-free name now resolves through
    PATH, the same law the seat's own proxy binary already lived under; an
    absolute or relative path is still taken as-is; the TOLD-ABOUT rule is
    untouched (unset still means None, never a PATH probe for a signer
    nobody configured)."""

    def _fake_signer(self, name="dregg-client-sign"):
        d = os.path.join(self.tmp, "bindir")
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, name)
        with open(p, "w") as f:
            f.write("#!/bin/sh\nexit 0\n")
        os.chmod(p, 0o700)
        return d, p

    def test_a_bare_name_resolves_through_PATH_and_reads_ready(self):
        d, p = self._fake_signer()
        os.environ["HELM_CELL_BIN"] = "dregg-client-sign"
        os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
        # POSITIVE CONTROL on the same observable: the file really is the
        # resolved target, so "ready" below is the resolution acting.
        self.assertEqual(cell._resolve("dregg-client-sign"), p)
        self.assertTrue(cell._usable("dregg-client-sign"))
        self.assertEqual(cell.bin_status()["state"], "ready")

    def test_a_bare_name_not_on_PATH_refuses_with_the_tried_form_named(self):
        os.environ["HELM_CELL_BIN"] = "no-such-signer-anywhere"
        self.assertIsNone(cell._resolve("no-such-signer-anywhere"))
        self.assertFalse(cell._usable("no-such-signer-anywhere"))
        status = cell.bin_status()
        self.assertEqual(status["state"], "missing")
        self.assertIn("PATH", status["reason"],
                      "a bare-name refusal must say WHERE it looked")
        self.assertNotIn("no-such-signer-anywhere", status["reason"],
                         "the reason names the form, never the value")

    def test_an_absolute_path_still_taken_as_is(self):
        d, p = self._fake_signer()
        os.environ["HELM_CELL_BIN"] = p
        self.assertEqual(cell._resolve(p), p)
        self.assertEqual(cell.bin_status()["state"], "ready")

    def test_unset_still_means_no_probe_at_all(self):  # noqa: VACUOUS_ASSERTION — the positive is assertIsNone on bin_path: resolution never invents a signer nobody set
        self.assertIsNone(cell.bin_path())
        self.assertIsNone(cell._resolve(None))
        self.assertFalse(cell._usable(None))
