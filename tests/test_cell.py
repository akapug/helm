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
from tests.test_no_private_names import hits_in  # noqa: E402

CELL_HEX = "ab" * 32
TURN = "cd" * 32

ENV_KEYS = ("HELM_HOME", "HELM_CELL_BIN", "MELD_CELL_BIN", "HELM_NODE_URL",
            "HELM_CELL_PROFILE", "HELM_NODE_TOKEN", "HELM_NODE_PASSPHRASE",
            "HELM_ROSTER", "HELM_NODE_ANCHOR_FEE", "HELM_NODE_ANCHOR_TIMEOUT",
            "MELD_NODE_URL", "MELD_AGENT_PROFILE",
            "MELD_NODE_TOKEN", "MELD_NODE_PASSPHRASE", "MELD_ROSTER",
            "DREGG_NODE_URL", "DREGG_PROFILE",
            "DREGG_API_TOKEN", "DREGG_NODE_PASSPHRASE",
            # OWNED HERE BECAUSE THE DECLARATION ARMS MUTATE IT. A key a test
            # sets or pops without the fixture owning it survives the test:
            # an arm that sets this to "1" left it set for every later arm in
            # the process, defeating a neighbour's absent-key assertion, and
            # an arm that popped it erased whatever the process started with.
            "DREGG_ALLOW_UNAUDITED_PQ")

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
        # measured 2026-08-04: test_seat_env_allowlist's grounding arm crashed
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
        # A2: a valid JSON list/string/number is NOT acceptance — guard with
        # isinstance(dict) before .get(), fail open, never raise AttributeError.
        for resp in ([1, 2, 3], "ok", 42, [{"turn_hash": TURN}]):
            with mock.patch.object(cell, "post_json", return_value=resp):
                turn, err = cell.anchor_submit("ab" * 32)
            self.assertIsNone(turn, resp)
            self.assertIn("non-object", err)

    def test_anchor_declares_coordination_fee_zero_env_overridable(self):
        # B3 (Stage B): the attest anchor is a COORDINATION turn (EmitEvent-only,
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
        # A1: a present proof means the turn EXISTS, not that it commits this
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

    def test_timeout_preserves_tuple_contract_but_marks_launched_unknown(self):
        with mock.patch.object(cell, "bin_path", return_value="/synthetic/signer"), \
             mock.patch.object(cell, "bin_status", return_value={"usable": True}), \
             mock.patch.object(cell, "build_env", return_value={}), \
             mock.patch.object(cell.subprocess, "run", side_effect=cell.subprocess.TimeoutExpired(
                 ["signer", "send"], 30, output=b"private stdout", stderr=b"private stderr")):
            rc, out, err = cell.run_bin(["send"], timeout=30)
        self.assertIsNone(rc)
        self.assertEqual(out, "")
        self.assertIsInstance(err, str)
        self.assertIsInstance(err, cell.BinTimeout)
        self.assertIn("outcome unknown", err)
        self.assertNotIn("private", err)

    def test_exec_failure_is_not_a_timeout(self):
        with mock.patch.object(cell, "bin_path", return_value="/synthetic/signer"), \
             mock.patch.object(cell, "bin_status", return_value={"usable": True}), \
             mock.patch.object(cell, "build_env", return_value={}), \
             mock.patch.object(cell.subprocess, "run", side_effect=FileNotFoundError()):
            rc, out, err = cell.run_bin(["send"], timeout=30)
        self.assertIsNone(rc)
        self.assertEqual(out, "")
        self.assertNotIsInstance(err, cell.BinTimeout)
        self.assertIn("could not execute", err)

    def test_run_bin_degrades_without_binary(self):
        rc, out, err = cell.run_bin(["roster"])
        self.assertIsNone(rc)
        self.assertIn("a2a transport unavailable", err)


class VerifiedCoresTest(CellBase):
    """A USABLE SIGNER IS NOT A VERIFIED ONE.

    A dregg build whose libdregg_lean.a exports no verified ML-DSA cores does
    not fail: dregg-pq answers security-critical operations with its unaudited
    fallback crates, the build is green, and the only trace is one stderr line
    the signer prints at startup. Every helm surface called that transport
    healthy until this reader existed.
    """

    #: THE REAL BINARY'S LINE, COPIED VERBATIM from a deployed
    #: dregg-client-sign. The format is an EXTERNAL producer's:
    #: dregg-sdk-net/src/bin/dregg-client-sign.rs prints
    #: `verified ML-DSA cores: sign {x:?}, verify {y:?}` to STDERR with Rust's
    #: Debug, so the words are dregg-pq's enum variant spellings. If upstream
    #: rewords it, this fixture is what fails, which is the point.
    REAL_LINE = ("[client-sign] verified ML-DSA cores: "
                 "sign ExportAbsent, verify ExportAbsent")

    def signer(self, stderr_line, rc=0):
        """A stub printing ONE line on stderr, where the real signer prints."""
        import shlex
        d = os.path.join(self.tmp, "bin")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, "dregg-client-sign")
        body = "#!/bin/sh\n"
        if stderr_line is not None:
            body += "echo %s 1>&2\n" % shlex.quote(stderr_line)
        body += "exit %d\n" % rc
        with open(path, "w") as f:
            f.write(body)
        os.chmod(path, 0o700)
        os.environ["HELM_CELL_BIN"] = path
        return path

    def test_ExportAbsent_is_DEGRADED_and_Installed_is_READY(self):
        """THE DISCRIMINATION, both directions in one arm. An absence arm
        alone would pass against a reader that answered `degraded` for
        everything, so the installed case is asserted first and
        unconditionally."""
        self.signer("[client-sign] verified ML-DSA cores: "
                    "sign Installed, verify AlreadyInstalled")
        good = cell.verified_cores()
        self.assertEqual("ready", good["state"], good["reason"])

        self.signer(self.REAL_LINE)
        bad = cell.verified_cores()
        self.assertEqual("degraded", bad["state"], bad["reason"])
        self.assertEqual("ExportAbsent", bad["sign"])
        self.assertEqual("ExportAbsent", bad["verify"])
        self.assertIn("unaudited", bad["reason"])

    def test_ONE_absent_core_is_still_degraded(self):
        """The gate is on EITHER core. A signer that verifies but cannot sign
        with a verified core is not a verified signer, and an `and` here would
        have called it one."""
        self.signer("[client-sign] verified ML-DSA cores: "
                    "sign ExportAbsent, verify Installed")
        r = cell.verified_cores()
        self.assertEqual("degraded", r["state"], r["reason"])
        self.assertIn("sign", r["reason"])

    def test_a_SILENT_signer_is_UNKNOWN_never_ready(self):
        """NOT BEING ABLE TO ASK IS NOT A GOOD ANSWER. A signer that predates
        the line, or is not dregg-client-sign at all, must never read as
        verified — the one direction this reader exists to prevent."""
        self.signer(None)
        r = cell.verified_cores()
        self.assertEqual("unknown", r["state"], r["reason"])
        self.assertIsNone(r["sign"])

    def test_a_SUFFIXED_variant_stays_UNKNOWN_and_is_not_truncated(self):
        """THE WHOLE TOKEN OR NOTHING. A character class of [A-Za-z] stops at
        the underscore, so `Installed_v2` matched as `Installed` and reported
        READY — a variant this helm has never seen, read as the single outcome
        that withholds every warning. A new word must fail membership, not
        shorten into a known one."""
        # POSITIVE CONTROL FIRST: the unsuffixed word DOES read ready through
        # this same reader, so the arm below is about the suffix.
        self.signer("[client-sign] verified ML-DSA cores: "
                    "sign Installed, verify Installed")
        self.assertEqual("ready", cell.verified_cores()["state"])

        self.signer("[client-sign] verified ML-DSA cores: "
                    "sign Installed_v2, verify Installed_v2")
        r = cell.verified_cores()
        self.assertEqual("unknown", r["state"],
                         "a suffixed variant must not truncate into Installed")
        self.assertIsNone(r["sign"])

    def test_a_VERIFY_ONLY_suffix_is_the_input_that_read_READY(self):
        """THE EXACT INPUT THE OLD PARSER GOT WRONG, and the reason the
        both-tokens arm above cannot stand alone.

        With only the SECOND token suffixed, the old class matched `sign
        Installed` cleanly, the comma matched, and `verify Installed_v2`
        truncated to `Installed` — two known words, reported READY. Suffixing
        BOTH tokens instead makes the comma fail to match at all, so the old
        parser answered UNKNOWN for a different reason and an arm built on it
        passes against the defect it was written to catch.

        MEASURED against both parsers:
          sign Installed_v2, verify Installed_v2  old: no match  new: unknown
          sign Installed,    verify Installed_v2  old: READY     new: unknown
        """
        # POSITIVE CONTROL, unconditional and first: the unsuffixed pair still
        # reads ready, so the assertion below is about the suffix and not
        # about a reader that never says ready.
        self.signer("[client-sign] verified ML-DSA cores: "
                    "sign Installed, verify Installed")
        self.assertEqual("ready", cell.verified_cores()["state"])

        self.signer("[client-sign] verified ML-DSA cores: "
                    "sign Installed, verify Installed_v2")
        r = cell.verified_cores()
        self.assertEqual("unknown", r["state"],
                         "a suffixed VERIFY token truncated into Installed "
                         "and the pair reported ready")
        self.assertIsNone(r["verify"])

    def test_unrecognised_words_are_UNKNOWN_and_never_echoed(self):
        """The value comes from a binary an operator was TOLD about, not one
        helm built, so this module names the FORM and never the VALUE on a
        diagnostic surface. An unknown word must not ride the reason onto an
        operator's screen."""
        self.signer("[client-sign] verified ML-DSA cores: "
                    "sign Sploit, verify Sploit")
        r = cell.verified_cores()
        self.assertEqual("unknown", r["state"])
        self.assertNotIn("Sploit", r["reason"])
        self.assertIsNone(r["sign"])

    def test_an_unconfigured_signer_is_UNUSABLE_not_verified(self):
        os.environ.pop("HELM_CELL_BIN", None)
        r = cell.verified_cores()
        self.assertEqual("unusable", r["state"])
        self.assertIsNone(r["sign"])


class SignerReachTest(CellBase):
    """HOW FAR ONE BINARY REACHES, and every way the count is a FLOOR."""

    def _scan(self, procs, unreadable=(), noperm=()):
        """Drive the real signer_reach over a fabricated /proc.

        `unreadable` pids raise a real OSError and `noperm` pids raise
        PermissionError, so the function under test does its own status
        handling rather than being handed a classification.
        """
        import builtins
        real_open = builtins.open

        def fake_open(path, *a, **k):
            for pid in unreadable:
                if path == "/proc/%d/environ" % pid:
                    raise OSError("boom")
            for pid in noperm:
                if path == "/proc/%d/environ" % pid:
                    raise PermissionError("nope")
            for pid, env in procs.items():
                if path == "/proc/%d/environ" % pid:
                    blob = b"".join(
                        ("%s=%s" % (k2, v2)).encode() + b"\0"
                        for k2, v2 in env.items())
                    return io.BytesIO(blob)
            return real_open(path, *a, **k)

        listing = [str(p) for p in
                   list(procs) + list(unreadable) + list(noperm)]
        return mock.patch.object(os, "listdir", return_value=listing), \
            mock.patch.object(builtins, "open", fake_open)

    def _abs_signer(self):
        path = os.path.join(self.tmp, "signer")
        with open(path, "w") as f:
            f.write("#!/bin/sh\nexit 0\n")
        os.chmod(path, 0o700)
        return path

    def test_a_seats_CHILDREN_do_not_inflate_the_count(self):
        """A seat's children inherit its environment, so counting PROCESSES
        answers several times over. Measured before this was a set: 34
        processes for 10 seats."""
        path = self._abs_signer()
        env = {"HELM_CHAT_NAME": "seat-a", "HELM_CELL_BIN": path}
        procs = {1: dict(env), 2: dict(env), 3: dict(env),
                 4: {"HELM_CHAT_NAME": "seat-b", "HELM_CELL_BIN": path},
                 5: {"HELM_CHAT_NAME": "seat-c"},
                 6: {}}
        a, b = self._scan(procs)
        with a, b:
            r = cell.signer_reach(path)
        self.assertIsNone(r["err"])
        self.assertEqual(2, r["naming"])
        self.assertEqual(3, r["seats"])

    def test_ANOTHER_seats_bare_name_is_opaque_not_a_match(self):
        """A BARE NAME BELONGS TO THE SEAT THAT SET IT. Resolving it here uses
        THIS process's PATH and cwd, so a seat whose bare `dregg-client-sign`
        is /b/signer in its own context would be counted against our
        /a/signer. Unresolvable identities are recorded as opaque and stated
        as a limit rather than guessed at."""
        path = self._abs_signer()
        procs = {1: {"HELM_CHAT_NAME": "seat-abs", "HELM_CELL_BIN": path},
                 2: {"HELM_CHAT_NAME": "seat-bare",
                     "HELM_CELL_BIN": "dregg-client-sign"},
                 3: {"HELM_CHAT_NAME": "seat-rel",
                     "HELM_CELL_BIN": "./dregg-client-sign"}}
        a, b = self._scan(procs)
        with a, b:
            r = cell.signer_reach(path)
        self.assertEqual(1, r["naming"], "only the absolute path is comparable")
        self.assertEqual(2, r["opaque"])
        self.assertEqual(3, r["seats"])

    def test_an_UNREADABLE_environ_is_a_partial_census_never_a_zero(self):
        """A failed read is not an empty environment. roguescan's reader fails
        open to {}, which would turn unreadable processes into 'no seat there'
        and let the census report a confident 0 of 0."""
        path = self._abs_signer()
        a, b = self._scan({}, unreadable=(11, 12))
        with a, b:
            r = cell.signer_reach(path)
        self.assertEqual(2, r["unreadable"])
        self.assertEqual(0, r["naming"])
        self.assertEqual(0, r["seats"])

    def test_a_denied_read_is_excluded_ONLY_when_the_owner_is_FOREIGN(self):
        """A DENIED READ DOES NOT NAME AN OWNER.

        EPERM can mean another user's process, and it can equally mean a
        SAME-UID process that is non-dumpable or fenced by ptrace policy. So
        exclusion requires POSITIVELY ESTABLISHED foreign ownership, read from
        the /proc entry's own owner — which is readable when its environ is
        not. Excluding on the denial alone silently drops processes that could
        be helm seats, which is the false-zero this census exists to prevent
        wearing a narrower costume.

        Both directions in one arm, because either alone passes against a
        reader that answers the same way for everything.
        """
        path = self._abs_signer()
        procs = {1: {"HELM_CHAT_NAME": "seat-a", "HELM_CELL_BIN": path}}
        mine, theirs = os.getuid(), os.getuid() + 1

        def owner_of(p):
            st = mock.Mock()
            st.st_uid = theirs if p == "/proc/21" else mine
            return st

        a, b = self._scan(procs, noperm=(21, 22))
        with a, b, mock.patch.object(os, "stat", side_effect=owner_of):
            r = cell.signer_reach(path)
        # 21 is FOREIGN by its owner, so excluding it is a fact, not a guess.
        # 22 is OURS and unreadable, so it stays a gap.
        self.assertEqual(1, r["unreadable"],
                         "a same-uid unreadable process is still a census gap")
        self.assertEqual(1, r["naming"])

    def test_an_unreadable_OWNER_keeps_the_process_as_a_gap(self):
        """If the owner itself cannot be read, nothing was established — and
        an unestablished exclusion is the same false-zero by another route."""
        path = self._abs_signer()
        a, b = self._scan({}, noperm=(31,))
        with a, b, mock.patch.object(os, "stat", side_effect=OSError("no")):
            r = cell.signer_reach(path)
        self.assertEqual(1, r["unreadable"])

    def test_an_UNREADABLE_scan_is_None_never_zero(self):
        """A count that could not be taken must not render as zero: zero reads
        as 'nothing is affected', the opposite of an unreadable census."""
        with mock.patch.object(os, "listdir", side_effect=OSError("nope")):
            r = cell.signer_reach("/nowhere")
        self.assertIsNone(r["naming"])
        self.assertEqual("OSError", r["err"])


class DeclarationEvidenceTest(CellBase):
    """ABSENT, DECLARED AND UNREADABLE ARE THREE ANSWERS, NOT TWO."""

    def test_an_UNREADABLE_declaration_is_None_not_undeclared(self):
        """`_signer_env_file` fails OPEN to {} — correct for its callers,
        fatal here, because 'no declaration' would then be indistinguishable
        from 'the declaration could not be read', and the row turns a failed
        read into a FAIL against a posture somebody may well have chosen."""
        os.environ.pop("DREGG_ALLOW_UNAUDITED_PQ", None)
        with mock.patch.object(cell, "_signer_env_read",
                               return_value=({}, "PermissionError")):
            declared, source = cell.unaudited_declared()
        self.assertIsNone(declared)
        self.assertIsNone(source)

    def test_an_ABSENT_file_really_is_undeclared(self):
        """THE POSITIVE CONTROL FOR THE ARM ABOVE, and it is not symmetric
        with it: signer.env's own text says an absent file means audited is
        required, so absent is a real answer and must stay False."""
        os.environ.pop("DREGG_ALLOW_UNAUDITED_PQ", None)
        with mock.patch.object(cell, "_signer_env_read", return_value=({}, None)):
            declared, source = cell.unaudited_declared()
        self.assertIs(False, declared)
        self.assertIsNone(source)

    def test_the_file_declares_and_names_itself_as_the_source(self):
        os.environ.pop("DREGG_ALLOW_UNAUDITED_PQ", None)
        with mock.patch.object(cell, "_signer_env_read",
                               return_value=({"DREGG_ALLOW_UNAUDITED_PQ": "1"},
                                             None)):
            declared, source = cell.unaudited_declared()
        self.assertIs(True, declared)
        self.assertIn("signer.env", source)

    def test_a_live_env_var_answers_without_the_file(self):
        """A real env var is readable whatever the file is doing, so a
        declaration made there never inherits the file's read status."""
        os.environ["DREGG_ALLOW_UNAUDITED_PQ"] = "1"
        with mock.patch.object(cell, "_signer_env_read",
                               return_value=({}, "PermissionError")):
            declared, source = cell.unaudited_declared()
        self.assertIs(True, declared)
        self.assertEqual("the environment", source)


class DeclarationKeyIsFixtureOwnedTest(CellBase):
    """THE ARMS ABOVE MUTATE A REAL ENV KEY, so the fixture must own it.

    These two are ORDER controls, not behaviour controls: they assert that
    running the declaration arms leaves the process environment exactly as
    they found it, which is the property a neighbour's absent-key assertion
    depends on and which no amount of testing the declaration logic can show.
    """

    KEY = "DREGG_ALLOW_UNAUDITED_PQ"

    def test_the_key_is_owned_by_the_fixture(self):
        """A key the fixture does not list is a key no tearDown restores."""
        self.assertIn(self.KEY, ENV_KEYS)

    def _ordered(self, *tests):
        """Run REAL test methods in an EXPLICIT order and return the result.

        The order a leak needs is not the order the runner happens to choose:
        alphabetically BuildEnvTest precedes DeclarationEvidenceTest, so the
        collision that actually bit could never be reproduced by relying on
        default ordering. These drive the real fixtures through unittest, in
        the sequence under test, so the control does not depend on where the
        classes sort.
        """
        res = unittest.TestResult()
        unittest.TestSuite(tests).run(res)
        return res

    def _pair(self):
        return (DeclarationEvidenceTest(
                    "test_a_live_env_var_answers_without_the_file"),
                BuildEnvTest(
                    "test_signer_env_file_fills_gaps_but_os_environ_wins"))

    def _both_orders(self, first, second, label):
        start = "sentinel-preexisting"
        os.environ[self.KEY] = start
        res = self._ordered(first, second)
        # NAMES AND TYPES ONLY, NEVER THE RENDERED FAILURE. A nested unittest
        # traceback carries whatever the inner assertion rendered, and an
        # assertion against an environment mapping renders the WHOLE ambient
        # environment — which on a build node includes real service
        # credentials. The failing test's id is what a reader needs; the dump
        # is what leaks.
        self.assertTrue(
            res.wasSuccessful(),
            "%s: %s" % (label, sorted(t.id() for t, _ in
                                      res.failures + res.errors)))
        self.assertEqual(start, os.environ.get(self.KEY),
                         "%s: the starting value was not restored" % label)

    def test_the_declaration_arm_THEN_the_absent_key_arm_both_pass(self):
        """THE EXACT COLLISION, driven rather than reasoned about.

        The declaration arm SETS this key; the neighbour asserts it is ABSENT
        from build_env(). Before the fixture owned the key, running them in
        this order left it set and defeated the neighbour's assertion.
        """
        first, second = self._pair()
        self._both_orders(first, second, "declaration then absent-key")

    def test_the_REVERSED_order_also_passes_and_restores(self):
        """The reverse, because a leak is directional and one order can pass
        while the other does not — and because the neighbour also touches the
        key, so it can be the polluter as easily as the victim."""
        first, second = self._pair()
        self._both_orders(second, first, "absent-key then declaration")

    def test_an_INITIAL_value_is_restored_not_erased(self):
        """The reverse direction, and the one a pop-only fixture gets wrong:
        a value the PROCESS already had must come back. This drives the real
        setUp/tearDown pair rather than asserting about them."""
        os.environ[self.KEY] = "preexisting"
        base = CellBase("run")
        base.setUp()
        # assertNotIn RENDERS THE CONTAINER on failure, and this container is
        # the process environment. assertFalse prints only the message.
        self.assertFalse(self.KEY in os.environ,
                         "setUp must clear the key for the test that follows")
        base.tearDown()
        self.assertEqual("preexisting", os.environ.get(self.KEY),
                         "tearDown erased a value the process arrived with")
        os.environ.pop(self.KEY, None)


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
        # UNCONDITIONAL, and it was not on the first cut: I guarded this on
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
        # A KEY VIEW, NEVER THE MAPPING. build_env() is documented as a copy
        # of os.environ, and assertNotIn renders its container on failure —
        # so the mapping form prints every ambient value into the report and
        # from there into the fab artifact. tuple() renders key names alone.
        self.assertNotIn("DREGG_NODE_PASSPHRASE", tuple(env))

    def test_signer_env_file_fills_gaps_but_os_environ_wins(self):
        # An operator's signer.env configures the signer's runtime posture (e.g.
        # a marshal-only devnet's DREGG_ALLOW_UNAUDITED_PQ) ONCE, read fresh on
        # every signed turn so a running seat picks it up with no relaunch.
        # Absent file => no-op; the file fills GAPS only, real env WINS.
        self.assertNotIn("DREGG_ALLOW_UNAUDITED_PQ",
                         tuple(cell.build_env()))          # absent file
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
            self.assertNotIn("NOEQUALS", tuple(env))                # skipped
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
        against. Returns the mock context. The gate reads the roster STRICTLY
        (`seats.roster_checked`), so that is the seam patched here."""
        return (mock.patch("helm.meld._self_seat", return_value=seat),
                mock.patch("helm.seats.roster_checked",
                           return_value=(roster, False)))

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
        """Population 2 — THE P0. The process can prove it is helm-claude-2 and
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
        the gate cannot prove whether the named seat is a fleet actor, so an
        ambient profile that is not that seat is REFUSED, never let stand.
        `derived_seat` still answers "" (it never guesses); the refusal comes
        from `signing_identity`, which alone can tell "no seat" from "could
        not read"."""
        os.environ["HELM_CELL_PROFILE"] = "owner-profile"
        # POSITIVE CONTROL on the SAME observable, first: a READABLE roster
        # yields the name, so the "" below is a verdict about the failure and
        # not a function that only knows one answer.
        with mock.patch("helm.meld._self_seat", return_value="seat-a"), \
                mock.patch("helm.seats.roster_checked",
                           return_value=({"seat-a": {"home_room": "helm"}},
                                         False)):
            self.assertEqual(cell.derived_seat(), "seat-a")
        # BOTH FAILURE SHAPES, because the reader has two: a read it judged
        # failed (corrupt, unreadable, wrong-shaped) and a read that RAISED.
        for failure in (mock.patch("helm.seats.roster_checked",
                                   return_value=({}, True)),
                        mock.patch("helm.seats.roster_checked",
                                   side_effect=OSError("boom"))):
            with mock.patch("helm.meld._self_seat", return_value="seat-a"), \
                    failure:
                self.assertEqual(cell.derived_seat(), "")
                profile, refusal = cell.signing_identity()
            self.assertIsNone(profile, "an unreadable roster let the ambient "
                              "profile stand")
            self.assertIn("identity unreadable", refusal)
            self.assertIn("seat-a", refusal)
            self.assertIn("owner-profile", refusal)

    def test_a_CONTRACT_VIOLATING_roster_answer_is_unreadable_not_a_raise(self):
        """seat_reading never raises: an answer that is not a mapping is read
        as an unreadable roster, and the gate refuses rather than crashing."""
        os.environ["HELM_CELL_PROFILE"] = "owner-profile"
        with mock.patch("helm.meld._self_seat", return_value="seat-a"), \
                mock.patch("helm.seats.roster_checked",
                           return_value=(None, False)):
            reading = cell.seat_reading()
            profile, refusal = cell.signing_identity()
        self.assertEqual(reading[0], "seat-a")
        self.assertIn("not a mapping", reading[1])
        self.assertIsNone(profile)
        self.assertIn("identity unreadable", refusal)

    def test_an_unreadable_roster_still_lets_a_seat_sign_as_ITSELF(self):
        """Control for the refusal above: an ambient profile that IS the named
        seat claims nobody else, so it needs no roster to be safe."""
        os.environ["HELM_CELL_PROFILE"] = "seat-a"
        with mock.patch("helm.meld._self_seat", return_value="seat-a"), \
                mock.patch("helm.seats.roster_checked",
                           side_effect=OSError("boom")):
            profile, refusal = cell.signing_identity()
        self.assertEqual(profile, "seat-a")
        self.assertIsNone(refusal)

    def test_a_MISSING_roster_is_a_proven_empty_one(self):
        """A fresh box has no roster file and no seats, so the owner's own
        session keeps signing as himself. Missing is not unreadable."""
        os.environ["HELM_CELL_PROFILE"] = "owner-profile"
        with mock.patch("helm.meld._self_seat", return_value="seat-a"), \
                mock.patch("helm.seats.roster_checked",
                           return_value=({}, False)):
            profile, refusal = cell.signing_identity()
        self.assertEqual(profile, "owner-profile")
        self.assertIsNone(refusal)


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
        rc, _, _ = self.run_cli(["send", "--to", CELL_HEX, "x"])
        self.assertEqual(rc, 0)
        with open(os.environ["STUB_LOG"]) as f:
            log = f.read()
        self.assertIn("argv:send --to %s x" % CELL_HEX, log)
        self.assertIn("MELD_NODE_URL=http://helm:1", log)   # env2 mapping
        self.assertIn("DREGG_NODE_URL=http://helm:1", log)  # dregg-native name

    def test_the_retired_meld_verbs_never_reach_the_signer(self):
        """dregg-client-sign dispatches join and send; `accept`, `recv`,
        `heartbeat` and `roster` belonged to the retired meld-style cell
        binary. Passed through, each one launched the signer only to print
        its `unknown verb` error. helm refuses them itself, and the stub
        proves the signer was never started."""
        self.write_stub()
        for verb in ("accept", "recv", "heartbeat", "roster"):
            rc, _, err = self.run_cli([verb, "--once"])
            self.assertEqual(rc, 2, verb)
            self.assertIn("unknown verb '%s'" % verb, err)
        self.assertFalse(os.path.exists(os.environ["STUB_LOG"]),
                         "a retired verb still launched the signer")
        rc, _, _ = self.run_cli(["join", "--profile", "p"])
        self.assertEqual(rc, 0)
        with open(os.environ["STUB_LOG"]) as f:
            self.assertIn("argv:join --profile p", f.read())

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
        rc, out, _ = self.run_cli(["status"])
        self.assertEqual(rc, 1)
        self.assertIn("UNREACHABLE", out)
        self.assertIn("a2a transport OFF", out)

    def test_status_reads_no_roster_file(self):
        """No dregg build reads ~/.dregg/roster.toml, so status no longer
        reports on it — not even when one is present."""
        os.environ["HELM_ROSTER"] = os.path.join(self.tmp, "roster.toml")
        with open(os.environ["HELM_ROSTER"], "w") as f:
            f.write('[[cell]]\nid = "%s"\nlabel = "t1"\n' % CELL_HEX)
        rc, out, _ = self.run_cli(["status"])
        self.assertEqual(rc, 1)
        self.assertIn("UNREACHABLE", out)
        self.assertNotIn("roster", out)

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


class ProfilesDirTest(unittest.TestCase):
    """helm must read keys from the directory the SIGNER reads them from.

    The SDK's rule (`dregg_sdk::profiles::profiles_dir`, the same in the
    fee-loop build and the rebased one): `$DREGG_HOME/profiles` when
    DREGG_HOME is set, else `$HOME/.dregg/profiles`. helm read
    DREGG_PROFILES_DIR, which no dregg build honours."""

    KEYS = ("DREGG_HOME", "DREGG_PROFILES_DIR", "HOME")

    def setUp(self):
        self.prior = {k: os.environ.get(k) for k in self.KEYS}
        for k in self.KEYS:
            os.environ.pop(k, None)
        self.tmp = tempfile.mkdtemp(prefix="helm-test-profiles-")
        os.environ["HOME"] = os.path.join(self.tmp, "home")

    def tearDown(self):
        for k, v in self.prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_DREGG_HOME_wins_the_way_the_sdk_resolves_it(self):
        os.environ["DREGG_HOME"] = os.path.join(self.tmp, "dh")
        self.assertEqual(cell.profiles_dir(),
                         os.path.join(self.tmp, "dh", "profiles"))

    def test_DREGG_PROFILES_DIR_is_ignored_because_no_dregg_build_reads_it(self):
        os.environ["DREGG_PROFILES_DIR"] = os.path.join(self.tmp, "elsewhere")
        self.assertEqual(cell.profiles_dir(),
                         os.path.join(self.tmp, "home", ".dregg", "profiles"))

    def test_the_keys_helm_reads_are_the_ones_the_signer_reads(self):
        """End to end through profile_public_keys: a profile in the SDK's
        directory is found, and one planted only under DREGG_PROFILES_DIR is
        not — the positive control and the refusal on the same observable."""
        import json as _json

        def plant(d, name, pk):
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, name + ".json"), "w") as f:
                _json.dump({"name": name, "public_key_hex": pk}, f)
        os.environ["DREGG_HOME"] = os.path.join(self.tmp, "dh")
        plant(os.path.join(self.tmp, "dh", "profiles"), "sdk", "aa" * 32)
        os.environ["DREGG_PROFILES_DIR"] = os.path.join(self.tmp, "stray")
        plant(os.path.join(self.tmp, "stray"), "stray", "bb" * 32)
        self.assertEqual(cell.profile_public_keys(), {"aa" * 32: "sdk"})


class SignerFreshnessTest(unittest.TestCase):
    """Is the RUNNING signer the one we think we fixed?

    WHY (owner, 2026-07-29): "is dregg signing just always broken after the next
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
                     for k in ("HELM_CELL_BIN", "HELM_DREGG_REPO",
                               "HELM_CHAT_NODE_BIN", "MELD_CHAT_NODE_BIN")}
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

    def _commit(self, subject, body="x", ago=0, crate=cell.SIGNER_CRATE):
        """`ago` seconds before now. EXPLICIT dates because git orders by
        commit time and a test makes several commits inside one second — the
        tie-break is then arbitrary and the test flaps rather than measures."""
        import subprocess as sp
        os.makedirs(os.path.join(self.repo, crate), exist_ok=True)
        p = os.path.join(self.repo, crate, "sign.rs")
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
        # the operator's private names, held as hex by the arm that owns them
        self.assertEqual(hits_in(reason), [],
                         "user-facing reason string names a private project: "
                         "%r" % reason)

    def test_a_signer_newer_than_its_source_is_current(self):
        self._commit("some old commit")
        self._signer(+60)
        self.assertEqual(cell.signer_staleness()["state"], "current")

    def test_it_finds_a_fix_that_never_LANDED_on_main(self):
        """THE LOAD-BEARING CASE, and the reason for `--all`.

        The commit that repairs a signer routinely lives on a LANE nobody
        merged — on 2026-07-29 the cure sat on lane/fee-loop-plus-coordination
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

    def test_a_fix_in_a_signer_DEPENDENCY_makes_the_signer_stale(self):
        """The signer is its crate AND what it links: sdk and turn directly,
        dregg-lean-ffi through sdk, and the Lean cores dregg-lean-ffi's
        build.rs compiles from metatheory/. A cure in any of them is missing
        from a binary built before it, exactly as one in dregg-sdk-net is."""
        for crate, ago in (("turn", 120), ("metatheory", 0)):
            self._commit("fix(%s): the cure lives here" % crate, crate=crate,
                         body=crate, ago=ago)
            self._signer(-7200)
            row = cell.signer_staleness()
            self.assertEqual(row["state"], "stale", crate)
            self.assertIn("fix(%s): the cure lives here" % crate, row["reason"])

    def test_the_signer_set_is_BOUNDED_a_node_only_commit_leaves_it_current(self):
        self._commit("signer source", ago=7200)
        self._signer(-3600)
        self._commit("node: a storage change", crate="node", ago=0)
        self.assertEqual(cell.signer_staleness()["state"], "current")

    def _node(self, mtime_offset):
        b = os.path.join(self.tmp, "dregg-cave-node")
        with open(b, "w") as f:
            f.write("#!/bin/sh\nexit 0\n")
        os.chmod(b, 0o755)
        t = time.time() + mtime_offset
        os.utime(b, (t, t))
        os.environ["HELM_CHAT_NODE_BIN"] = b
        return b

    def test_a_node_older_than_its_store_source_is_STALE_and_names_it(self):
        """The node had no freshness check at all and sat thousands of
        commits behind its source with no alarm."""
        self._commit("persist: refuse a store with no schema epoch",
                     crate="persist")
        self._node(-7200)
        row = cell.node_staleness()
        self.assertEqual(row["state"], "stale")
        self.assertIn("refuse a store with no schema epoch", row["reason"])
        self.assertIn("DEPLOYED node", row["reason"])

    def test_a_node_newer_than_its_source_is_current_and_signer_commits_do_not_count(self):
        self._commit("node: old", crate="node", ago=7200)
        self._node(-3600)
        self._commit("sdk-net: a signer-only change", ago=0)
        self.assertEqual(cell.node_staleness()["state"], "current")

    def test_no_node_binary_is_unknown_never_current(self):
        self._commit("node: anything", crate="node")
        os.environ["HELM_CHAT_NODE_BIN"] = os.path.join(self.tmp, "absent")
        row = cell.node_staleness()
        self.assertEqual(row["state"], "unknown")
        self.assertIn("no chat node binary", row["reason"])


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

    THE GAP THIS CLOSES, measured 2026-08-01 over 30 room rows: proxy-family
    seats carry HELM_CELL_BIN in their process env and signed 12/12; the two
    claude-direct seats must prefix it onto EVERY command, because their shell
    state does not persist between tool calls, and signed 7 of 16. The board
    row said RELAUNCH was the sole path — it never was, but the alternative was
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
        """A blocking finding. A file ANY same-uid seat can rewrite must
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
