#!/usr/bin/env python3
"""The chat room node's FAUCET IS A CELL, NOT A MINT — and it ran dry.

MEASURED ON THE LIVE FLEET NODE. A `helm chat post` came back DEGRADED
(send_outcome_unknown) because helm's proactive top-up was refused with
"insufficient balance on cell 4a8882bb17c23b3e: need 697, have 260". The live
node held 22 cells whose whole supply was 10260 computrons: one cell at 10000
(the SDK profile `helm-agent`), the faucet at 260, and twenty seat cells at 0.
Nothing was leaking. The faucet had simply paid out everything it had, and
`POST /api/faucet` CANNOT MINT — node/src/api.rs says value enters only by
genesis issuer-moves, and this cave is genesis-less (no genesis.json in the
data dir), so the faucet is a plain cell that pays out of its own balance.

Two surfaces lied about that, and both are pinned here:

  * `helm doctor` printed one WARN per low cell saying "the auto-faucet refunds
    on next post". The auto-faucet is exactly the thing that could no longer
    refund anything, so the report's advice was to wait for a cure that had
    already failed. A dry faucet is its OWN state with its own repair, not a
    footnote on the cells it could not fund.
  * chat's proactive top-up folded the faucet's refusal into the generic
    `send_outcome_unknown` diagnostic, so the row an operator reads named the
    symptom (a send with no receipt) and never the cause (the faucet's source
    cell is empty).

THE THIRD FACT, and the reason the door below refuses rather than repairs: no
installed signer can sign a Transfer. `dregg-client-sign` exposes `join` and
`send` and nothing else — measured against the binary's own usage text and
against dregg-sdk-net/src/bin/dregg-client-sign.rs, which dispatches exactly
those two verbs. helm HOLDS the key for the funded cell (the SDK profile
`helm-agent`), so the missing half is a verb, not a credential; until dregg
grows one, the honest door states what it would move and refuses. An UNKNOWN
balance is never DRY and never healthy, which is the third pole every arm here
carries.
"""
import io
import json
import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

import os as _os, sys as _sys  # noqa: E402
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

from helm import cell, chat, chatnode, doctor, pk  # noqa: E402

FAUCET = "4a" * 32
SOURCE = "c2" * 32
NODE = "http://127.0.0.1:8898"


class FaucetBase(unittest.TestCase):
    """A hermetic HELM_HOME plus a STUBBED node HTTP surface: the only reads
    are `cell.get_json`, the seam every helm caller already goes through."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-faucet-")
        self.prior = {k: os.environ.get(k)
                      for k in ("HOME", "HELM_HOME", "HELM_CHAT_FAUCET_CELL",
                                "HELM_CHAT_NODE_URL", "DREGG_HOME",
                                "DREGG_PROFILES_DIR")}
        os.environ["HOME"] = self.tmp
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_CHAT_FAUCET_CELL"] = FAUCET
        os.environ["HELM_CHAT_NODE_URL"] = NODE
        # the SDK's own rule: $DREGG_HOME/profiles (DREGG_PROFILES_DIR is
        # honoured by no dregg build, so it is cleared, never used)
        os.environ.pop("DREGG_PROFILES_DIR", None)
        os.environ["DREGG_HOME"] = os.path.join(self.tmp, "dregg-home")
        self.profiles = os.path.join(self.tmp, "dregg-home", "profiles")
        os.makedirs(self.profiles)

    def tearDown(self):
        for k, v in self.prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def node(self, balances, cells=None):
        """Patch the node read seam. `balances` maps a 64-hex cell id to a
        balance, or to None for a cell the node did not answer for."""
        pubkeys = cells or {}

        def get_json(url, timeout=None):
            if url.endswith("/api/cells") or "/api/cells?" in url:
                return [{"id": c, "balance": balances.get(c) or 0}
                        for c in balances]
            if "/api/cell/" in url:
                cid = url.rsplit("/api/cell/", 1)[1]
                if cid not in balances:
                    return {"found": False}
                bal = balances[cid]
                if bal is None:
                    return None
                out = {"id": cid, "found": True, "balance": bal}
                if cid in pubkeys:
                    out["public_key"] = pubkeys[cid]
                return out
            return None

        return mock.patch.object(cell, "get_json", side_effect=get_json)

    def profile(self, name, public_key_hex, seed_hex="9f" * 32):
        """One SDK profile on disk. The seed is PRESENT on purpose: the door
        must be provable to never carry it to a surface."""
        with open(os.path.join(self.profiles, name + ".json"), "w") as f:
            json.dump({"version": 1, "name": name,
                       "public_key_hex": public_key_hex,
                       "seed_hex": seed_hex, "created_at": "(stamp)"}, f)
        return seed_hex

    def shortfall(self, cell_prefix="4a8882bb17c23b3e", need=697, have=260):
        chatnode.record_faucet_shortfall(cell_prefix, need, have)

    def doctor_rows(self, balances, cells=None, registry=None):
        """`doctor.check_chat_node` over the stubbed node."""
        path = os.path.join(self.tmp, "cells.json")
        pk.write_json(path, registry if registry is not None else {})
        with self.node(balances, cells), \
                mock.patch.object(chat, "node_url", return_value=NODE), \
                mock.patch.object(chat, "node_head",
                                  return_value={"chain_index": 1}), \
                mock.patch.object(chat, "transport_status",
                                  return_value={"mode": "off"}), \
                mock.patch.object(chat, "cells_path", return_value=path), \
                mock.patch.object(cell, "bin_status", return_value={
                    "configured": True, "usable": True, "state": "ready",
                    "reason": "signer ready"}):
            return doctor.check_chat_node()


class DoctorSaysDry(FaucetBase):
    def test_a_dry_faucet_is_its_own_FAIL_naming_cell_balance_need_and_verb(self):
        """THE ROW THE INCIDENT NEEDED. Twenty low cells produced twenty rows
        all promising an auto-refund; none named the one cell whose
        emptiness caused every one of them."""
        self.shortfall()
        rows = self.doctor_rows({FAUCET: 260})
        dry = [m for l, m in rows if l == doctor.FAIL and "FAUCET DRY" in m]
        self.assertEqual(1, len(dry), "exactly one dry row: %r" % (rows,))
        self.assertIn(FAUCET[:12], dry[0], "the row must NAME the faucet cell")
        self.assertIn("260", dry[0], "the row must carry the balance")
        self.assertIn("697", dry[0],
                      "the row must carry the largest recent requirement")
        self.assertIn("helm chat node refuel", dry[0],
                      "the row must carry the repair verb")
        # THE CONTROL: this same run reached the rest of the check, so a
        # missing row below is absence, not a check that never ran.
        self.assertTrue([m for l, m in rows if l == doctor.OK
                         and "chat room node LIVE" in m], rows)

    def test_a_funded_faucet_says_nothing_about_DRY(self):
        rows = self.doctor_rows({FAUCET: 9000})
        # UNCONDITIONAL FIRST: the absences below are only meaningful over
        # a report that was actually produced.
        live = [m for l, m in rows if l == doctor.OK
                and "chat room node LIVE" in m]
        self.assertEqual(1, len(live), rows)
        self.assertFalse([m for _l, m in rows if "FAUCET DRY" in m], rows)
        self.assertFalse([m for _l, m in rows if "faucet balance UNKNOWN" in m],
                         rows)

    def test_an_unreadable_faucet_balance_is_UNKNOWN_never_dry_never_healthy(self):
        """A BALANCE HELM COULD NOT READ IS THE THIRD POLE. The old low-cell
        row already learned this one level down; the faucet row is born with
        it."""
        rows = self.doctor_rows({FAUCET: None})
        unknown = [m for l, m in rows if l == doctor.WARN
                   and "faucet balance UNKNOWN" in m]
        self.assertEqual(1, len(unknown), rows)
        self.assertFalse([m for _l, m in rows if "FAUCET DRY" in m],
                         "an unreadable balance is not a dry one")
        self.assertFalse([m for l, m in rows if l == doctor.OK
                          and "faucet" in m.lower()],
                         "and it is not a clean bill either")

    def test_the_low_cell_row_stops_promising_a_refund_the_faucet_cannot_make(self):
        """The promise was true only while the faucet was funded. Measured on
        the live node it was the opposite of the truth for twenty cells at
        once. On a FEE-CHARGING node (a declared coordination fee) what tops a
        cell up is the signer, before its next send, out of the faucet."""
        self.shortfall()
        with mock.patch.object(cell, "coord_fee", return_value=1000):
            rows = self.doctor_rows({FAUCET: 260, SOURCE: 0},
                                    registry={"seat-a": SOURCE})
            ok = self.doctor_rows({FAUCET: 9000, SOURCE: 0},
                                  registry={"seat-a": SOURCE})
        low = [m for l, m in rows if l == doctor.WARN and "balance low" in m]
        self.assertEqual(1, len(low), rows)
        self.assertNotIn("tops it up before its next send", low[0],
                         "the signer cannot top up out of an empty faucet")
        self.assertIn("faucet", low[0].lower(),
                      "the low row must point at the cause row")
        # THE OTHER POLE, unconditional: a FUNDED faucet keeps the promise,
        # because then it is true — and it names who keeps it.
        low_ok = [m for l, m in ok if l == doctor.WARN and "balance low" in m]
        self.assertEqual(1, len(low_ok), ok)
        self.assertIn("the signer tops it up before its next send", low_ok[0])
        self.assertNotIn("auto-faucet", low_ok[0])

    def test_a_zero_balance_is_HEALTHY_where_chat_turns_are_fee_free(self):
        """helm declares fee 0 for its coordination turns by default, and the
        node's exempt class admits them free, so every seat cell there sits at
        zero for good. A WARN about that was permanent noise."""
        with mock.patch.object(cell, "coord_fee", return_value=0):
            rows = self.doctor_rows({FAUCET: 9000, SOURCE: 0},
                                    registry={"seat-a": SOURCE})
        self.assertFalse([m for _l, m in rows if "balance low" in m], rows)
        ok = [m for l, m in rows if l == doctor.OK and "chat cells:" in m]
        self.assertEqual(1, len(ok), rows)
        self.assertIn("1 readable of 1", ok[0])
        self.assertIn("a zero balance is healthy", ok[0])
        # THE POSITIVE CONTROL: the same cell on a fee-charging node WARNs.
        with mock.patch.object(cell, "coord_fee", return_value=1000):
            charged = self.doctor_rows({FAUCET: 9000, SOURCE: 0},
                                       registry={"seat-a": SOURCE})
        self.assertEqual(1, len([m for l, m in charged if l == doctor.WARN
                                 and "balance low" in m]), charged)


class FeeWellLine(FaucetBase):
    """The node names its faucet cell ONCE, at boot, and not in a form any
    plain filter can find."""

    # As `tracing` emits it: the field name, the `=` and the value are three
    # spans with SGR escapes between them, so the raw MESSAGE has no substring
    # `fee_well=` at all. A journal filter on that string matched nothing while
    # the line sat in the journal three times over, and the reader reported no
    # fee well with no error anywhere.
    ANSI = ("INFO dregg_node: fee loop: genesis-less devnet fee well pointed "
            "at the faucet cell \x1b[3mfee_well\x1b[0m\x1b[2m=\x1b[0m"
            "4a8882bb17c23b3e")

    def test_the_escaped_form_the_node_actually_writes_is_read(self):
        self.assertEqual("4a8882bb17c23b3e",
                         chatnode.read_fee_well(self.ANSI.encode().decode(
                             "unicode_escape")))

    def test_the_stripped_form_journalctl_renders_is_read_too(self):
        """journalctl's default output sanitises the escapes; `-o cat` does
        not. Both forms reach this reader depending on the caller."""
        self.assertEqual("4a8882bb17c23b3e", chatnode.read_fee_well(
            "fee loop: ... fee_well=4a8882bb17c23b3e"))

    def test_a_line_without_a_fee_well_yields_nothing(self):
        # UNCONDITIONAL POSITIVE FIRST: the same reader on the same shape DOES
        # return a value, so None below is a miss and not a dead reader.
        self.assertEqual("4a8882bb17c23b3e", chatnode.read_fee_well(
            "fee loop: fee_well=4a8882bb17c23b3e"))
        self.assertIsNone(chatnode.read_fee_well(
            "coordination class: genesis-less devnet fee-exempts EmitEvent"))


class RefuelDoor(FaucetBase):
    def run_refuel(self, argv, balances, cells=None, supports=True,
                   run_bin=None):
        """`supports` may be a CALLABLE, and then it is the signer-verb probe
        itself. That probe is the last thing the plan does before the move, so
        a callable is how an arm changes the world inside the exact window
        between the plan's reads and the submit."""
        buf, err = io.StringIO(), io.StringIO()
        runner = run_bin or mock.Mock(return_value=(
            0, json.dumps({"committed": True, "turn_hash": "ab" * 32}), ""))
        probe = (mock.Mock(side_effect=supports) if callable(supports)
                 else mock.Mock(return_value=supports))
        # The settle re-reads wait between reads; the arms record the waits
        # instead of sleeping them, so an unsettled answer costs no time.
        self.slept = []
        with self.node(balances, cells), \
                mock.patch.object(cell, "signer_supports", probe), \
                mock.patch.object(cell, "run_bin", runner), \
                mock.patch.object(cell, "bin_path", return_value="/x/signer"), \
                mock.patch.object(chatnode, "_sleep", self.slept.append), \
                mock.patch("sys.stdout", buf), mock.patch("sys.stderr", err):
            rc = chatnode.cmd_node(["refuel"] + argv)
        return rc, buf.getvalue() + err.getvalue(), runner

    def test_the_door_moves_a_named_amount_from_a_funded_cell_into_the_faucet(self):
        self.shortfall()
        self.profile("helm-agent", "ca75")
        rc, out, runner = self.run_refuel(
            ["--apply"], {FAUCET: 260, SOURCE: 10000}, cells={SOURCE: "ca75"})
        self.assertEqual(0, rc, out)
        self.assertIn(FAUCET[:12], out)
        self.assertIn(SOURCE[:12], out)
        self.assertIn("helm-agent", out)
        argv = runner.call_args[0][0]
        self.assertEqual("transfer", argv[0],
                         "the door signs a Transfer, never a faucet POST")
        self.assertIn(FAUCET, argv)

    def test_a_second_run_against_a_FUNDED_faucet_is_a_no_op(self):
        """IDEMPOTENCE IS MEASURED AT THE SIGNER, not in the prose: a door that
        printed 'already funded' and still moved value would pass a text
        assertion."""
        self.profile("helm-agent", "ca75")
        rc, out, runner = self.run_refuel(
            ["--apply"], {FAUCET: 9000, SOURCE: 10000}, cells={SOURCE: "ca75"})
        self.assertEqual(1, len([l for l in out.splitlines()
                                 if "already funded" in l]), out)
        self.assertEqual(0, rc, out)
        runner.assert_not_called()  # noqa: VACUOUS_ASSERTION — not-called IS the contract; the spy's positive control is the sibling arm test_the_door_moves_a_named_amount_from_a_funded_cell_into_the_faucet, which drives the SAME mock through the same door and reads its argv

    def test_the_door_REFUSES_when_no_installed_signer_can_sign_a_transfer(self):
        """THE MEASURED STATE OF THIS HOST. helm holds the funded cell's key
        (the SDK profile) and the signer has no verb that spends it."""
        self.shortfall()
        self.profile("helm-agent", "ca75")
        rc, out, runner = self.run_refuel(
            ["--apply"], {FAUCET: 260, SOURCE: 10000}, cells={SOURCE: "ca75"},
            supports=False)
        self.assertEqual(1, len([l for l in out.splitlines()
                                 if "cannot sign" in l.lower()]), out)
        self.assertEqual(1, rc, out)
        self.assertIn("transfer", out)
        runner.assert_not_called()  # noqa: VACUOUS_ASSERTION — not-called IS the contract; the spy's positive control is the sibling arm test_the_door_moves_a_named_amount_from_a_funded_cell_into_the_faucet, which drives the SAME mock through the same door and reads its argv
        # It still says what it WOULD have moved: a refusal that hides the
        # plan cannot be handed to whoever fixes the signer.
        self.assertIn(SOURCE[:12], out)
        self.assertIn(FAUCET[:12], out)

    def test_a_signer_helm_COULD_NOT_ASK_is_UNKNOWN_not_a_missing_verb(self):
        """The third pole again, one layer down. `signer_supports` answers
        None when the signer is absent, unusable or silent, and a refusal that
        renders that as "exposes no `transfer` verb" states a measurement
        nothing took."""
        self.shortfall()
        self.profile("helm-agent", "ca75")
        rc, out, runner = self.run_refuel(
            ["--apply"], {FAUCET: 260, SOURCE: 10000}, cells={SOURCE: "ca75"},
            supports=None)
        self.assertEqual(1, len([l for l in out.splitlines()
                                 if "UNKNOWN" in l]), out)
        self.assertEqual(1, rc, out)
        self.assertNotIn("exposes no", out,
                         "an unaskable signer is not a signer without the verb")
        runner.assert_not_called()  # noqa: VACUOUS_ASSERTION — not-called IS the contract; the spy's positive control is the sibling arm test_the_door_moves_a_named_amount_from_a_funded_cell_into_the_faucet, which drives the SAME mock through the same door and reads its argv

    def test_an_unreadable_faucet_refuses_instead_of_guessing(self):
        self.profile("helm-agent", "ca75")
        rc, out, runner = self.run_refuel(
            ["--apply"], {FAUCET: None, SOURCE: 10000}, cells={SOURCE: "ca75"})
        self.assertEqual(1, len([l for l in out.splitlines()
                                 if "UNKNOWN" in l]), out)
        self.assertEqual(1, rc, out)
        runner.assert_not_called()  # noqa: VACUOUS_ASSERTION — not-called IS the contract; the spy's positive control is the sibling arm test_the_door_moves_a_named_amount_from_a_funded_cell_into_the_faucet, which drives the SAME mock through the same door and reads its argv

    def test_without_apply_the_door_only_states_the_move(self):
        self.shortfall()
        self.profile("helm-agent", "ca75")
        rc, out, runner = self.run_refuel(
            [], {FAUCET: 260, SOURCE: 10000}, cells={SOURCE: "ca75"})
        self.assertEqual(1, len([l for l in out.splitlines()
                                 if "--apply" in l]), out)
        self.assertEqual(0, rc, out)
        runner.assert_not_called()  # noqa: VACUOUS_ASSERTION — not-called IS the contract; the spy's positive control is the sibling arm test_the_door_moves_a_named_amount_from_a_funded_cell_into_the_faucet, which drives the SAME mock through the same door and reads its argv

    def test_a_nonzero_exit_whose_balances_cannot_be_reread_stays_UNKNOWN(self):
        """A signer can submit a turn, have it COMMIT, and still exit nonzero
        resolving its receipt. Reading that exit as "refused" tells the
        operator to retry, and the retry moves the value a second time. When
        the balances that could settle it cannot be read either, the answer
        stays UNKNOWN and says why a retry is dangerous."""
        self.shortfall()
        self.profile("helm-agent", "ca75")
        balances = {FAUCET: 260, SOURCE: 10000}

        def commit_then_darken(*_a, **_k):
            balances[FAUCET] = None       # the node stops answering for it
            return (7, json.dumps({"committed": True, "turn_hash": "ab" * 32}),
                    "receipt lookup timed out")

        rc, out, runner = self.run_refuel(
            ["--apply"], balances, cells={SOURCE: "ca75"},
            run_bin=mock.Mock(side_effect=commit_then_darken))
        self.assertEqual(1, rc, out)
        self.assertEqual(1, len([l for l in out.splitlines()
                                 if "outcome still UNKNOWN" in l]), out)
        self.assertIn("could not be re-read", out)
        self.assertNotIn("refused", out.lower(),
                         "a nonzero exit cannot establish that nothing moved")
        self.assertNotIn("COMMITTED", out)
        self.assertIn("MAY move the value twice", out)
        self.assertEqual(1, runner.call_count, out)

    def test_exit_zero_with_no_receipt_is_UNKNOWN_not_moved(self):  # noqa: VACUOUS_ASSERTION — each subTest asserts what an unproven result must NOT claim; the unconditional positive control for the same observable is the sibling arm test_a_COMMITTED_transfer_is_reported_WITH_the_receipt_that_proves_it, which drives the same door with a real receipt and requires rc 0 and the receipt on the surface
        """THE OTHER POLE. A JSON object is not a receipt. An empty answer, a
        queued envelope and an explicit failure all leave the transfer
        unwitnessed, and only a committed receipt can say the value moved."""
        self.shortfall()
        self.profile("helm-agent", "ca75")
        for answer in ({}, {"status": "queued"},
                       {"success": False, "turn_hash": "ab" * 32},
                       # A HASH IS AN IDENTITY, NOT A COMMITMENT: a queued turn
                       # already has one, and helm holds no transfer protocol
                       # that would let it read this answer as applied.
                       {"status": "queued", "turn_hash": "ab" * 32},
                       {"turn_hash": "ab" * 32},
                       {"committed": False, "receipt_hash": "cd" * 32},
                       # A CONTRADICTING ANSWER IS NOT A RECEIPT EITHER. These
                       # two are what tell the commitment test apart from a
                       # bare "is any positive flag set": a stated failure and
                       # a not-yet-applied status each refuse on their own,
                       # beside a positive flag that would otherwise pass.
                       {"success": False, "committed": True,
                        "turn_hash": "ab" * 32},
                       {"status": "queued", "committed": True,
                        "turn_hash": "ab" * 32},
                       # ADMISSION IS NOT COMMITMENT. In bound dregg's send
                       # source `accepted: true` beside a turn hash says the
                       # node TOOK the turn, never that it applied one, and
                       # `ok` and `success` make the same shape of claim.
                       {"accepted": True, "turn_hash": "ab" * 32},
                       {"ok": True, "turn_hash": "ab" * 32},
                       {"success": True, "receipt_hash": "cd" * 32},
                       # A CONTRADICTION READ BY ITS FIRST TRUTHY WORD passes
                       # as committed; both words have to be read.
                       {"status": "committed", "state": "queued",
                        "turn_hash": "ab" * 32},
                       # PRESENT-BUT-MALFORMED IS NOT ABSENT: a truthiness
                       # read collapses these to "", so a committed status
                       # beside a malformed state read as a clean commitment.
                       # A field the producer SENT must satisfy the contract.
                       {"status": "committed", "state": False,
                        "turn_hash": "ab" * 32},
                       {"status": "committed", "state": 0,
                        "turn_hash": "ab" * 32},
                       {"status": "committed", "state": [],
                        "turn_hash": "ab" * 32},
                       {"status": 1, "turn_hash": "ab" * 32}):
            with self.subTest(answer=answer):
                rc, out, runner = self.run_refuel(
                    ["--apply"], {FAUCET: 260, SOURCE: 10000},
                    cells={SOURCE: "ca75"},
                    run_bin=mock.Mock(return_value=(0, json.dumps(answer), "")))
                self.assertEqual(1, rc, out)
                self.assertEqual(1, len([l for l in out.splitlines()
                                         if "UNKNOWN" in l]), out)
                self.assertNotIn("moved", out)
                # The fixture node never changes, so the balances settle the
                # signer's UNKNOWN as NOT COMMITTED — and only after waiting.
                self.assertEqual(1, len([l for l in out.splitlines()
                                         if "NOT COMMITTED" in l]), out)
                self.assertEqual(list(chatnode.REREAD_DELAYS_S), self.slept)
                self.assertEqual(1, runner.call_count, out)

    def test_a_COMMITTED_transfer_is_reported_WITH_the_receipt_that_proves_it(self):
        """THE POSITIVE CONTROL for the two arms above: the same door, a real
        receipt, and the success sentence names it. Without this arm a door
        that called everything UNKNOWN would pass both of them."""
        self.shortfall()
        self.profile("helm-agent", "ca75")
        rc, out, runner = self.run_refuel(
            ["--apply"], {FAUCET: 260, SOURCE: 10000}, cells={SOURCE: "ca75"},
            run_bin=mock.Mock(return_value=(
                0, json.dumps({"status": "committed",
                               "receipt_hash": "cd" * 32}), "")))
        self.assertEqual(0, rc, out)
        self.assertNotIn("UNKNOWN", out)
        self.assertIn("moved", out)
        self.assertIn("cd" * 8, out, "the receipt itself is on the surface")
        self.assertEqual(1, runner.call_count, out)

    def test_an_ABSENT_state_field_is_still_absent_and_a_committed_flag_moves(self):
        """THE POSITIVE POLE for the malformed-field arms above, and the one
        the reviewer named as missing: rc 0 with `committed: true` and no
        status or state field at all is a clean move. Absent means the key is
        missing or null; it must not be collapsed together with malformed."""
        self.shortfall()
        self.profile("helm-agent", "ca75")
        for answer in ({"committed": True, "turn_hash": "ab" * 32},
                       {"committed": True, "state": None,
                        "turn_hash": "ab" * 32}):
            with self.subTest(answer=answer):
                rc, out, runner = self.run_refuel(
                    ["--apply"], {FAUCET: 260, SOURCE: 10000},
                    cells={SOURCE: "ca75"},
                    run_bin=mock.Mock(return_value=(0, json.dumps(answer), "")))
                self.assertEqual(0, rc, out)
                self.assertIn("moved", out)
                self.assertNotIn("UNKNOWN", out)
                self.assertEqual(1, runner.call_count, out)

    def test_a_faucet_FUNDED_between_the_plan_and_the_move_is_not_refunded(self):
        """THE INTERLEAVE a review named. The balances behind a plan are read
        before the signer is even asked what verbs it has; if a second refuel
        (or a grant) funds the faucet inside that window, submitting the stale
        plan overfunds the faucet and drains the source twice."""
        self.shortfall()
        self.profile("helm-agent", "ca75")
        balances = {FAUCET: 260, SOURCE: 10000}

        def fund_it(_verb):
            balances[FAUCET] = 9000   # the other refuel lands, right here
            return True

        rc, out, runner = self.run_refuel(
            ["--apply"], balances, cells={SOURCE: "ca75"}, supports=fund_it)
        self.assertEqual(0, rc, out)
        self.assertIn("nothing to do", out)
        self.assertIn("9000", out)
        runner.assert_not_called()  # noqa: VACUOUS_ASSERTION — the spy's positive control is test_the_door_moves_a_named_amount_from_a_funded_cell_into_the_faucet, which drives the SAME mock through the same door and reads its argv

    def test_a_faucet_that_goes_UNREADABLE_inside_that_window_submits_nothing(self):
        """The third pole of the same re-read: UNKNOWN at the moment of the
        move is not "still dry"."""
        self.shortfall()
        self.profile("helm-agent", "ca75")
        balances = {FAUCET: 260, SOURCE: 10000}

        def darken(_verb):
            balances[FAUCET] = None   # the node stops answering for the cell
            return True

        rc, out, runner = self.run_refuel(
            ["--apply"], balances, cells={SOURCE: "ca75"}, supports=darken)
        self.assertEqual(1, rc, out)
        self.assertEqual(1, len([l for l in out.splitlines()
                                 if "nothing was submitted" in l]), out)
        runner.assert_not_called()  # noqa: VACUOUS_ASSERTION — the spy's positive control is test_the_door_moves_a_named_amount_from_a_funded_cell_into_the_faucet, which drives the SAME mock through the same door and reads its argv

    def test_no_surface_of_this_door_can_carry_key_material(self):
        """The profile store holds a seed beside the public key. The door reads
        that directory, so the arm that matters is the one that proves what
        comes back out."""
        self.shortfall()
        seed = self.profile("helm-agent", "ca75", seed_hex="7d" * 32)
        _rc, out, _r = self.run_refuel(
            ["--apply"], {FAUCET: 260, SOURCE: 10000}, cells={SOURCE: "ca75"},
            supports=False)
        # UNCONDITIONAL FIRST: prove the door SPOKE before proving what it
        # did not say — a silent door passes every assertNotIn there is.
        self.assertEqual(1, len([l for l in out.splitlines()
                                 if "helm-agent" in l]), out)
        self.assertNotIn(seed, out)
        self.assertNotIn(seed[:16], out)
        self.assertNotIn("seed", out.lower())
        keys = cell.profile_public_keys()
        self.assertEqual({"ca75": "helm-agent"}, keys,
                         "the reader returns the PUBLIC half only")


#: What the fee-loop node answers a send its cell cannot pay for, as the
#: signer relays it: /turns/submit's `rejected: <TurnError>` around
#: TurnError::InsufficientBalance, naming the SENDING cell by 16 hex.
FEE_REFUSAL = ("[client-sign] error: node refused the turn: rejected: "
               "insufficient balance on cell %s: need 1254, have 0" % SOURCE[:16])


class DegradedNamesTheDrySource(FaucetBase):
    """The faucet is the cause only when the node refused the send for its fee
    and the top-up that would pay it was refused for a dry source."""

    REFUSAL = ("faucet refused: insufficient balance on cell "
               "4a8882bb17c23b3e: need 697, have 260")

    def sign_send(self, faucet_err):
        with mock.patch.object(cell, "bin_status", return_value={
                    "configured": True, "usable": True, "state": "ready",
                    "reason": "ready"}), \
                mock.patch.object(chat, "_node_token", return_value="t"), \
                mock.patch.object(chat, "_room_cell",
                                  return_value=(SOURCE, None, True)), \
                mock.patch.object(chatnode, "faucet",
                                  return_value=(None, faucet_err)), \
                mock.patch.object(cell, "run_bin",
                                  return_value=(1, "", FEE_REFUSAL)):
            return chat._sign_send("hi", "seat-a")

    def test_a_dry_SOURCE_becomes_the_codes_own_diagnostic(self):
        info, diag = self.sign_send(self.REFUSAL)
        self.assertIsNone(info)
        self.assertEqual("faucet_source_dry", diag["code"],
                         "send_outcome_unknown names the symptom, not the cause")
        self.assertIn("4a8882bb", diag["reason"])
        self.assertIn("260", diag["reason"])
        self.assertIn("697", diag["reason"])
        tag = chat._transport_tag({"transport": {"state": "DEGRADED",
                                                 "profile": "seat-a",
                                                 "code": diag["code"],
                                                 "reason": diag["reason"]}})
        self.assertIn("faucet_source_dry", tag)
        self.assertIn("helm chat node refuel", diag["remediation"])

    def test_the_refusal_is_recorded_so_doctor_can_name_the_requirement(self):
        """The refusal is the ONLY place the node states what a funded turn
        cost. Dropping it is why doctor had no number to print."""
        self.sign_send(self.REFUSAL)
        rows = self.doctor_rows({FAUCET: 260})
        dry = [m for l, m in rows if l == doctor.FAIL and "FAUCET DRY" in m]
        self.assertEqual(1, len(dry), rows)
        self.assertIn("697", dry[0])

    def test_any_OTHER_faucet_refusal_keeps_the_generic_code(self):
        """THE POLE. A rate limit is not a dry source, and collapsing them
        would put the wrong repair in front of the next operator."""
        _info, diag = self.sign_send(
            "faucet refused: rate limited: 1 request per cell per minute")
        self.assertEqual("send_failed", diag["code"])
        self.assertTrue(diag["reason"].startswith(
            "cell send rc 1: the node refused the turn"),
            "the node's own refusal leads: %s" % diag["reason"])
        self.assertIn("rate limited", diag["reason"])
        self.assertNotIn("refuel", diag["reason"])


class TheRecordedCellIsBoundToItsNodeToo(FaucetBase):
    """WHICH CELL a record names is the same identity question as HOW MUCH it
    needed, and answering it in only one of the two consumers is the shape
    defect this class exists to pin.

    EVERY OTHER ARM IN THIS FILE SETS HELM_CHAT_FAUCET_CELL, and being told
    wins over every discovered source — so the override MASKS this seam
    completely. These arms pop it and give the node a real, currently
    discoverable fee well, which is the only arrangement where a recorded
    cell and a discovered one can disagree."""

    OTHER_NODE = "http://127.0.0.1:9999"
    STALE_CELL = "9e" * 32
    LONG_AGO = 400 * 24 * 3600

    def setUp(self):
        super().setUp()
        os.environ.pop("HELM_CHAT_FAUCET_CELL", None)   # the mask comes off

    def plant_unstamped(self):
        """The pre-stamp record shape the earlier writer emitted: fresh, valid,
        and silent about which node refused."""
        path = chatnode.faucet_state_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        pk.write_json(path, {"cell": self.STALE_CELL, "need": 9000,
                             "have": 0, "at": pk.now_ts()})

    def plant(self, **over):
        rec = {"cell": self.STALE_CELL, "need": 9000, "have": 0,
               "node": NODE, "at": pk.now_ts()}
        rec.update(over)
        path = chatnode.faucet_state_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        pk.write_json(path, rec)

    def prefix(self, url=None):
        """The shipped reader over a node whose fee well IS discoverable.
        Returns just the cell; the disqualification flag has its own arms."""
        with mock.patch.object(chatnode, "_fee_well_from_journal",
                               return_value=FAUCET):
            return chatnode.faucet_cell_prefix(url)[0]

    def qualified(self, url=None):
        """(cell, disqualified) — the whole answer, for the arms that are
        about WHY there is no cell rather than which one."""
        with mock.patch.object(chatnode, "_fee_well_from_journal",
                               return_value=FAUCET):
            return chatnode.faucet_cell_prefix(url)

    def test_a_record_from_THIS_node_still_names_the_cell(self):
        """UNCONDITIONAL POSITIVE FIRST: a current record outranks the fee
        well, so the two arms below are a filter working and not a reader
        that ignores every record."""
        self.plant()
        self.assertEqual(self.STALE_CELL, self.prefix())

    def test_a_record_from_ANOTHER_node_does_not_name_this_faucet(self):
        self.plant(node=self.OTHER_NODE)
        self.assertEqual(FAUCET, self.prefix(),
                         "the fee well helm can read NOW outranks a cell "
                         "recorded at a different node")

    def test_a_record_older_than_its_season_does_not_name_this_faucet(self):
        self.plant(at=pk.epoch_ts(time.time() - self.LONG_AGO))
        self.assertEqual(FAUCET, self.prefix())

    def test_a_stale_record_no_longer_forces_the_faucet_UNKNOWN(self):
        """THE CONSEQUENCE, at the surface an operator reads: with the stale
        cell winning, the node answered for no such cell and the whole faucet
        read UNKNOWN even though its real cell was discoverable and funded."""
        self.plant(node=self.OTHER_NODE)
        with mock.patch.object(chatnode, "_fee_well_from_journal",
                               return_value=FAUCET), \
                self.node({FAUCET: 9000}):
            st = chatnode.faucet_state(NODE)
        self.assertEqual("funded", st["state"], st.get("reason"))
        self.assertEqual(FAUCET, st["cell"])


    def test_an_explicit_url_decides_BOTH_the_cell_and_the_threshold(self):
        """ONE TARGET IDENTITY THROUGH THE WHOLE READ. faucet_state(B) took
        its threshold from a record for B while the cell reader filtered
        against the CONFIGURED node A, so the call asked B for A's cell. The
        two halves have to answer about the same node."""
        other = "http://127.0.0.1:9999"
        self.plant(node=other)                      # a record for the OTHER node
        with mock.patch.object(chatnode, "_fee_well_from_journal",
                               return_value=FAUCET):
            # Read AS the other node: its own record names the cell and sets
            # the floor, and neither half falls back to the configured one.
            self.assertEqual(self.STALE_CELL,
                             chatnode.faucet_cell_prefix(other)[0])
            # Read as the CONFIGURED node: that record is foreign to it, so
            # the discovered fee well wins in BOTH halves.
            self.assertEqual(FAUCET, chatnode.faucet_cell_prefix(NODE)[0])
            self.assertEqual(FAUCET, chatnode.faucet_cell_prefix()[0])

    def test_an_UNSTAMPED_record_qualifies_for_no_node_at_all(self):
        """ABSENCE IS NOT PROVENANCE. A record written before the stamp
        existed carries no claim about which node refused, so after a url
        change it qualified for a node it had never described. The hostile
        composition: a FRESH unstamped record from A, node B requested, and
        helm must neither name A's cell nor read it at B."""
        for stamp in ({}, {"node": None}):
            with self.subTest(stamp=stamp):
                self.plant(**stamp) if stamp else self.plant_unstamped()
                other = "http://127.0.0.1:9999"
                reads = []

                def spy(u, timeout=None):
                    reads.append(u)
                    return None

                with mock.patch.object(chatnode, "_fee_well_from_journal",
                                       return_value=FAUCET), \
                        mock.patch.object(cell, "get_json", side_effect=spy):
                    st = chatnode.faucet_state(other)
                self.assertEqual("unknown", st["state"])
                self.assertIsNone(st["cell"])
                self.assertEqual([], reads,
                                 "no cell of A's was resolved or read at B")

    def test_a_QUALIFIED_cell_that_cannot_RESOLVE_says_so_and_not_the_other(self):
        """THE THIRD UNKNOWN, and the one I owed. A source DID describe this
        node and its cell simply did not resolve there — a different repair
        from having no qualified source at all, so it keeps its own sentence
        and must not borrow the disqualification one."""
        other = "http://127.0.0.1:9999"
        self.plant(node=other, cell="beefbeef")  # qualified, abbreviated
        with mock.patch.object(chatnode, "_fee_well_from_journal",
                               return_value=FAUCET), \
                self.node({FAUCET: 9000}):       # nothing starts with it
            st = chatnode.faucet_state(other)
        self.assertEqual("unknown", st["state"])
        self.assertIn("did not resolve", st["reason"])
        self.assertNotIn("will not resolve another node", st["reason"])

    def test_a_MATCHING_stamp_is_still_the_qualified_positive(self):
        """The pole that keeps the rule from being 'refuse every record'."""
        other = "http://127.0.0.1:9999"
        self.plant(node=other)
        with mock.patch.object(chatnode, "_fee_well_from_journal",
                               return_value=FAUCET):
            self.assertEqual(self.STALE_CELL,
                             chatnode.faucet_cell_prefix(other)[0])

    def test_the_unknown_reason_names_no_override_that_is_not_set(self):
        """A diagnostic that asserts an unset variable sends the reader to
        unset something that does not exist."""
        other = "http://127.0.0.1:9999"
        with mock.patch.object(chatnode, "_fee_well_from_journal",
                               return_value=FAUCET), \
                self.node({FAUCET: 9000}):
            quiet = chatnode.faucet_state(other)
        self.assertNotIn("HELM_CHAT_FAUCET_CELL", quiet["reason"])
        # UNCONDITIONAL POSITIVE: when it IS set, the sentence names it.
        os.environ["HELM_CHAT_FAUCET_CELL"] = FAUCET
        with mock.patch.object(chatnode, "_fee_well_from_journal",
                               return_value=FAUCET), \
                self.node({FAUCET: 9000}):
            loud = chatnode.faucet_state(other)
        self.assertIn("HELM_CHAT_FAUCET_CELL", loud["reason"])

    def test_a_CONFIGURED_REMOTE_node_does_not_consume_the_local_journal(self):
        """THE FALSIFIER, and it is the reason "configured" could not be the
        journal's binding. HELM_CHAT_NODE_URL may name a REMOTE node, and the
        journal still describes the unit on THIS host — so admitting it for
        "the configured target" re-opens the wrong-target lookup one layer in.
        No record, no override: the answer is UNKNOWN and helm must never
        resolve or read the local cell AT the remote node."""
        remote = "http://10.0.0.7:8898"
        os.environ["HELM_CHAT_NODE_URL"] = remote
        cell_reads = []

        def spy(u, timeout=None):
            cell_reads.append(u)
            return None

        with mock.patch.object(chatnode, "_fee_well_from_journal",
                               return_value=FAUCET), \
                mock.patch.object(cell, "get_json", side_effect=spy):
            st = chatnode.faucet_state()
        self.assertEqual("unknown", st["state"])
        self.assertIsNone(st["cell"])
        self.assertIn("will not resolve another node", st["reason"])
        self.assertEqual([], cell_reads,
                         "helm asked the remote node nothing at all")

    def test_a_TARGET_STAMPED_record_still_answers_for_a_configured_remote(self):
        """THE POSITIVE POLE for the arm above: refusing the journal must not
        refuse the node. A record stamped with the remote's own identity is
        still a qualified source and still names its cell."""
        remote = "http://10.0.0.7:8898"
        os.environ["HELM_CHAT_NODE_URL"] = remote
        self.plant(node=remote)
        with mock.patch.object(chatnode, "_fee_well_from_journal",
                               return_value=FAUCET):
            cellid, disqualified = chatnode.faucet_cell_prefix()
        self.assertEqual(self.STALE_CELL, cellid)
        self.assertFalse(disqualified)

    def test_an_override_answers_for_the_CONFIGURED_node_and_no_other(self):
        """The operator assertion is about the context this host is
        configured for. It answers there, and contributes nothing to a URL a
        caller names explicitly."""
        os.environ["HELM_CHAT_FAUCET_CELL"] = FAUCET
        self.assertEqual(FAUCET, self.prefix())          # configured: yes
        cellid, disqualified = self.qualified("http://10.0.0.7:8898")
        self.assertEqual("", cellid)                     # foreign: no
        self.assertTrue(disqualified)

    def test_a_qualified_record_is_never_vetoed_by_a_refused_source(self):
        """DISQUALIFICATION IS TERMINAL ONLY AFTER EVERY ELIGIBLE SOURCE IS
        EXHAUSTED. An override refused for target reasons must not suppress a
        record that does describe the node being asked about."""
        other = "http://127.0.0.1:9999"
        os.environ["HELM_CHAT_FAUCET_CELL"] = FAUCET
        self.plant(node=other)
        cellid, disqualified = self.qualified(other)
        self.assertEqual(self.STALE_CELL, cellid)
        self.assertFalse(disqualified)

    def test_the_two_UNKNOWNS_are_told_apart(self):
        """A source refused for target reasons and no source having ever
        existed are different repairs, so they must not share a sentence."""
        other = "http://127.0.0.1:9999"
        with mock.patch.object(chatnode, "_fee_well_from_journal",
                               return_value=FAUCET), \
                self.node({FAUCET: 9000}):
            disq = chatnode.faucet_state(other)
        self.assertIn("will not resolve another node", disq["reason"])
        # NOTHING observed anywhere: the other sentence, same state.
        with mock.patch.object(chatnode, "_fee_well_from_journal",
                               return_value=""), \
                self.node({FAUCET: 9000}):
            never = chatnode.faucet_state(NODE)
        self.assertIn("never observed", never["reason"])
        self.assertNotIn("will not resolve another node", never["reason"])

    def test_faucet_state_for_another_node_reads_THAT_node_in_both_halves(self):
        """THE COMPOSITION, which is where the disagreement actually bites and
        which the two halves passing separately cannot show. faucet_state(B)
        took its THRESHOLD from B's record while its CELL half filtered against
        the configured node, so the call asked B for a cell B's own record did
        not name — and a mutation that stops threading the url survives every
        arm that drives the halves one at a time."""
        other = "http://127.0.0.1:9999"
        self.plant(node=other, need=9000)
        with mock.patch.object(chatnode, "_fee_well_from_journal",
                               return_value=FAUCET), \
                self.node({self.STALE_CELL: 5000, FAUCET: 5000}):
            st = chatnode.faucet_state(other)
        # BOTH halves answer about B: its own record names the cell AND sets
        # the floor, so 5000 is dry against a 9000 requirement.
        self.assertEqual(self.STALE_CELL, st["cell"])
        self.assertEqual(9000, st["threshold"])
        self.assertEqual(9000, st["observed_need"])
        self.assertEqual("dry", st["state"])


class ShortfallIsBoundToItsNodeAndItsSeason(FaucetBase):
    """A recorded requirement is an OBSERVATION AT A NODE AT A TIME, and the
    threshold it raises has to expire with it. Keeping the largest need forever
    made a rebuilt room read DRY at a balance its own turns never needed: the
    faucet cell id is derived deterministically, so a genesis-less cave rebuilt
    from scratch resolves the SAME cell and inherits the old season's number.
    """
    OTHER_NODE = "http://127.0.0.1:9999"
    LONG_AGO = 400 * 24 * 3600      # older than any season this record has

    def plant(self, **over):
        rec = {"cell": FAUCET, "need": 9000, "have": 0,
               "node": NODE, "at": pk.now_ts()}
        rec.update(over)
        path = chatnode.faucet_state_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        pk.write_json(path, rec)
        return rec

    def state(self, balance):
        with self.node({FAUCET: balance}):
            return chatnode.faucet_state(NODE)

    def test_a_requirement_observed_at_THIS_node_and_recently_still_stands(self):
        """UNCONDITIONAL POSITIVE FIRST. Without this arm a reader that ignored
        every record would pass the three below."""
        self.plant()
        st = self.state(5000)
        self.assertEqual(9000, st["threshold"])
        self.assertEqual(9000, st["observed_need"])
        self.assertEqual("dry", st["state"])

    def test_a_requirement_observed_at_ANOTHER_node_does_not_redefine_dry(self):
        self.plant(node=self.OTHER_NODE)
        st = self.state(5000)
        self.assertEqual(chatnode.FAUCET_DRY_BELOW, st["threshold"])
        self.assertIsNone(st["observed_need"])
        self.assertEqual("funded", st["state"])

    def test_a_requirement_older_than_its_season_does_not_redefine_dry(self):
        self.plant(at=pk.epoch_ts(time.time() - self.LONG_AGO))
        st = self.state(5000)
        self.assertEqual(chatnode.FAUCET_DRY_BELOW, st["threshold"])
        self.assertIsNone(st["observed_need"])
        self.assertEqual("funded", st["state"])

    def test_a_new_observation_does_not_inherit_a_foreign_high_water_mark(self):
        """The retention is the other half of the same property: the max()
        that keeps a high-water mark takes its prior from THIS node's own
        record, never from a foreign one."""
        self.plant(node=self.OTHER_NODE)
        chatnode.record_faucet_shortfall(FAUCET, 1600, 0)
        rec = chatnode.faucet_shortfall()
        self.assertEqual(1600, rec["need"])
        self.assertEqual(NODE, rec["node"],
                         "the record names the node it was observed at")
        self.assertEqual(1600, self.state(5000)["threshold"])

    def test_a_new_observation_DOES_keep_this_nodes_own_recent_high_water(self):
        """THE POLE. Bounding the record must not turn it into a last-writer
        record: a smaller second refusal at the same node in the same season
        still cannot lower what a funded turn was measured to cost."""
        self.plant(need=9000)
        chatnode.record_faucet_shortfall(FAUCET, 1600, 0)
        self.assertEqual(9000, chatnode.faucet_shortfall()["need"])



def scrub_env(test, *names):
    """Clear each variable for one test and put the runner's value back after,
    so an operator's knob cannot decide an arm."""
    for name in names:
        prior = os.environ.pop(name, None)
        if prior is not None:
            test.addCleanup(os.environ.__setitem__, name, prior)
        else:
            test.addCleanup(os.environ.pop, name, None)


class LowFaucetRung(FaucetBase):
    """LOW BEFORE DRY. The DRY row is written by the refusal that has already
    degraded every seat's signed turn; measured on the live node, a refuel to
    8260 was refusing again within ten minutes. The LOW row lands while the faucet
    can still pay, with the grants left and the refuel that restores them.
    A grant is 697 here: no refusal is recorded, so the default stands."""

    def setUp(self):
        super().setUp()
        scrub_env(self, chatnode.LOW_GRANTS_ENV)

    def state(self, balance):
        with self.node({FAUCET: balance}):
            return chatnode.faucet_state(NODE)

    def live(self, rows):
        """THE CONTROL: the report was produced, so an absence below is an
        absence and not a check that never ran."""
        self.assertEqual(1, len([m for l, m in rows if l == doctor.OK
                                 and "chat room node LIVE" in m]), rows)

    def test_a_balance_ABOVE_k_grants_is_not_low_and_counts_what_is_left(self):  # noqa: VACUOUS_ASSERTION — the unconditional positive control is self.live(rows) on the same report, and the sibling BELOW arm drives the same doctor to a LOW row
        rows = self.doctor_rows({FAUCET: 9000})
        self.live(rows)
        self.assertFalse([m for _l, m in rows if "faucet LOW" in m], rows)
        st = self.state(9000)
        self.assertIs(False, st["low"])
        self.assertEqual(12, st["grants_left"])
        self.assertEqual(5 * 697, st["low_below"])

    def test_a_balance_BELOW_k_grants_WARNs_with_the_count_and_the_remedy(self):
        # 2000 is above the 1500 DRY floor and below the 3485 LOW line.
        rows = self.doctor_rows({FAUCET: 2000})
        self.live(rows)
        low = [m for l, m in rows if l == doctor.WARN and "faucet LOW" in m]
        self.assertEqual(1, len(low), rows)
        self.assertIn(FAUCET[:12], low[0])
        self.assertIn("about 2 grants of 697 left", low[0])
        self.assertIn("helm chat node refuel --amount %d" % (2 * 3485 - 2000),
                      low[0], "the remedy is a command with its number")
        self.assertIn(chatnode.LOW_GRANTS_ENV, low[0],
                      "the row names the knob that sets its line")
        self.assertFalse([m for _l, m in rows if "FAUCET DRY" in m],
                         "LOW is not DRY: the faucet can still pay")

    def test_an_unreadable_balance_is_UNKNOWN_never_low_never_fine(self):
        rows = self.doctor_rows({FAUCET: None})
        self.assertEqual(1, len([m for l, m in rows if l == doctor.WARN
                                 and "faucet balance UNKNOWN" in m]), rows)
        self.assertFalse([m for _l, m in rows if "faucet LOW" in m], rows)
        st = self.state(None)
        self.assertEqual("unknown", st["state"])
        self.assertIsNone(st["low"], "UNKNOWN is a third value, not False")
        self.assertIsNone(st["grants_left"])

    def test_the_line_follows_the_knob_and_a_bad_knob_is_the_default(self):  # noqa: VACUOUS_ASSERTION — the first assertIs(False) is unconditional; every subTest asserts True on the same field, so the loop is the positive pole, not a guard
        os.environ[chatnode.LOW_GRANTS_ENV] = "2"
        self.assertIs(False, self.state(2000)["low"],
                      "two grants (1394) sit under a 2000 balance")
        for bad in ("0", "-3", "many", ""):
            with self.subTest(knob=bad):
                os.environ[chatnode.LOW_GRANTS_ENV] = bad
                st = self.state(2000)
                self.assertEqual(chatnode.FAUCET_LOW_GRANTS, st["low_grants"])
                self.assertIs(True, st["low"])

    def test_DRY_is_LOW_even_when_the_line_sits_under_the_DRY_floor(self):
        """With K at one the LOW line (697) is under the DRY floor (1500): a
        faucet that cannot pay must still read LOW, or its wake never goes."""
        os.environ[chatnode.LOW_GRANTS_ENV] = "1"
        st = self.state(1000)
        self.assertEqual("dry", st["state"])
        self.assertIs(True, st["low"])

    def test_a_larger_recorded_grant_moves_the_line(self):
        chatnode.record_faucet_shortfall(FAUCET[:16], 1000, 400)
        st = self.state(4000)
        self.assertEqual(1000, st["grant"])
        self.assertEqual(5000, st["low_below"])
        self.assertIs(True, st["low"])
        self.assertEqual(4, st["grants_left"])

    def status(self, balances):
        """`helm chat node status` over the stubbed node, stdout+stderr."""
        path = os.path.join(self.tmp, "cells.json")
        pk.write_json(path, {})
        fake = self.node(balances).kwargs["side_effect"]

        def get_json(url, timeout=None):
            if url.endswith("/api/receipts"):
                return [{"chain_index": 7}]
            return fake(url, timeout)

        buf, err = io.StringIO(), io.StringIO()
        with mock.patch.object(cell, "get_json", side_effect=get_json), \
                mock.patch.object(chatnode, "_systemctl",
                                  return_value=(0, "active")), \
                mock.patch.object(chat, "node_url", return_value=NODE), \
                mock.patch.object(chat, "transport_status",
                                  return_value={"mode": "off"}), \
                mock.patch.object(chat, "cells_path", return_value=path), \
                mock.patch.object(cell, "signer_staleness",
                                  return_value={"state": "current"}), \
                mock.patch.object(cell, "node_staleness", create=True,
                                  return_value={"state": "current"}), \
                mock.patch.object(cell, "signer_cores",
                                  return_value={"state": "verified"}), \
                mock.patch.object(chatnode, "identity_state", return_value={
                    "saved": ["node.key"], "matched": True}), \
                mock.patch("sys.stdout", buf), mock.patch("sys.stderr", err):
            chatnode.cmd_node(["status"])
        return buf.getvalue() + err.getvalue()

    def test_status_says_FAUCET_LOW_with_the_count_and_the_remedy(self):
        out = self.status({FAUCET: 2000})
        low = [l for l in out.splitlines() if "FAUCET LOW" in l]
        self.assertEqual(1, len(low), out)
        self.assertIn("about 2 grants of 697 left", low[0])
        self.assertIn("helm chat node refuel --amount 4970", low[0])

    def test_status_counts_grants_on_a_healthy_faucet_and_is_silent_on_LOW(self):
        out = self.status({FAUCET: 9000})
        self.assertIn("funded — balance 9000, about 12 grants of 697 left", out)
        self.assertNotIn("FAUCET LOW", out)

    def test_status_on_an_unreadable_faucet_is_UNKNOWN_and_never_LOW(self):
        out = self.status({FAUCET: None})
        self.assertIn("faucet balance UNKNOWN", out)
        self.assertNotIn("FAUCET LOW", out)
        self.assertNotIn("funded", out)

    def test_status_closes_a_recovered_spell_and_never_opens_one(self):
        """A READER CLOSES A SPELL, NEVER OPENS ONE. On a fee-free node no
        grant ever re-reads the faucet, so a faucet refilled by hand or from
        another host left the latch standing and the NEXT low spell woke
        nobody. `status` is a reading, and a reading that is not LOW closes
        the spell it finds open."""
        with self.node({FAUCET: 2000}):
            woke = chatnode.faucet_watch(
                NODE, post=lambda text, room: {"id": "w"},
                roster={"night-integrator": {"home_room": "helm"}})
        self.assertIsNotNone(woke, "control: the spell opened")
        self.assertTrue(chatnode.low_spell(NODE, FAUCET).get("since"))
        self.status({FAUCET: 9000})
        self.assertEqual({}, chatnode.low_spell(NODE, FAUCET),
                         "a funded reading through status closes the spell")
        # THE POLE: the same reader over a LOW faucet opens nothing.
        self.assertIn("FAUCET LOW", self.status({FAUCET: 2000}))
        self.assertEqual({}, chatnode.low_spell(NODE, FAUCET),
                         "status may close a spell, never open one")


class LowFaucetWake(FaucetBase):
    """ONE wake per LOW spell, to the integrator (resolved from the roster,
    never spelled) and the node's owner seat, latched under an flock."""

    # The integrator is named by the roster alone: no literal in the code can
    # produce this name, so a mention of it proves it was RESOLVED.
    ROSTER = {"night-integrator": {"home_room": "helm"},
              "node-keeper": {"home_room": "helm"}}

    def setUp(self):
        super().setUp()
        scrub_env(self, chatnode.LOW_GRANTS_ENV, chatnode.OWNER_ENV,
                  "HELM_INTEGRATOR_SEAT", "MELD_INTEGRATOR_SEAT")
        os.environ[chatnode.OWNER_ENV] = "node-keeper"
        self.posts = []

    def post(self, text, room):
        self.posts.append((room, text))
        return {"id": len(self.posts)}

    def watch(self, balance, post=None, may_open=True):
        with self.node({FAUCET: balance}):
            return chatnode.faucet_watch(NODE, may_open=may_open,
                                         post=post or self.post,
                                         roster=self.ROSTER)

    def test_a_LOW_reading_wakes_once_naming_count_remedy_and_both_seats(self):  # noqa: VACUOUS_ASSERTION — the unconditional assertEqual on the first wake and the post count of 1 are the positive control for the None of the second read
        woke = self.watch(2000)
        self.assertEqual(("main", ["night-integrator", "node-keeper"]), woke)
        self.assertEqual(1, len(self.posts))
        room, text = self.posts[0]
        self.assertEqual("main", room)
        self.assertIn("about 2 grants of 697 left", text)
        self.assertIn("helm chat node refuel --amount 4970", text)
        self.assertIn("@night-integrator @node-keeper", text)
        # THE SAME SPELL, read again: nobody is woken twice.
        self.assertIsNone(self.watch(1800))
        self.assertEqual(1, len(self.posts), self.posts)

    def test_the_wake_re_arms_after_the_faucet_recovers(self):  # noqa: VACUOUS_ASSERTION — the post count of 2 after the second LOW read is the unconditional positive control for the recovery's None
        self.watch(2000)
        self.assertIsNone(self.watch(9000), "a recovery wakes nobody")
        self.assertEqual({}, chatnode.low_spell(NODE, FAUCET),
                         "a recovered reading closes the spell")
        self.watch(2000)
        self.assertEqual(2, len(self.posts), "a NEW spell wakes again")

    def test_an_UNKNOWN_reading_neither_opens_nor_closes_a_spell(self):  # noqa: VACUOUS_ASSERTION — the unconditional spell-since assertTrue and the final post count of 1 are the positive controls for the UNKNOWN reads' None
        self.assertIsNone(self.watch(None))
        self.assertEqual([], self.posts, "unknown is never low")
        self.watch(2000)
        self.assertIsNone(self.watch(None))
        self.assertTrue(chatnode.low_spell(NODE, FAUCET).get("since"),
                        "unknown is never fine either: the spell stays open")
        self.watch(2000)
        self.assertEqual(1, len(self.posts), self.posts)

    def test_an_UNDELIVERED_wake_does_not_latch(self):  # noqa: VACUOUS_ASSERTION — the unconditional assertIsNotNone on the retried wake and its post count are the positive control
        self.assertIsNone(self.watch(2000, post=lambda text, room: None))
        self.assertEqual({}, chatnode.low_spell(NODE, FAUCET))
        self.assertIsNotNone(self.watch(2000))
        self.assertEqual(1, len(self.posts))

    def test_a_reader_that_may_not_open_never_posts(self):
        self.assertIsNone(self.watch(2000, may_open=False))
        self.assertEqual([], self.posts)
        # THE POSITIVE CONTROL: the same reading, allowed to open, does.
        self.assertIsNotNone(self.watch(2000))
        self.assertEqual(1, len(self.posts))

    def test_no_owner_set_wakes_the_integrator_and_says_so(self):
        os.environ.pop(chatnode.OWNER_ENV)
        woke = self.watch(2000)
        self.assertEqual(["night-integrator"], woke[1])
        self.assertIn(chatnode.OWNER_ENV, self.posts[0][1])

    def test_an_owner_the_roster_does_not_carry_is_not_mentioned(self):
        os.environ[chatnode.OWNER_ENV] = "ghost-seat"
        woke = self.watch(2000)
        self.assertEqual(["night-integrator"], woke[1])
        self.assertIn("does not resolve in the roster", self.posts[0][1])
        self.assertNotIn("@ghost-seat", self.posts[0][1])

    def grant(self, response, balances):
        """One grant through chatnode.faucet, the path every seat's top-up
        takes, with the wake's post and roster seams stubbed."""
        with self.node(balances), \
                mock.patch.object(cell, "post_json", return_value=response), \
                mock.patch.object(chatnode, "_wake_post", self.post), \
                mock.patch("helm.seats_common.roster",
                           return_value=self.ROSTER):
            return chatnode.faucet(NODE, SOURCE, 10000)

    def test_a_granted_top_up_reads_the_faucet_and_wakes_on_LOW(self):  # noqa: VACUOUS_ASSERTION — the first grant's post count of 1 is the unconditional positive control for the funded grant's empty posts
        r, err = self.grant({"success": True}, {FAUCET: 2000})
        self.assertIsNone(err)
        self.assertEqual(1, len(self.posts), "the grant path drives the wake")
        # THE POLE: a grant from a faucet that is not low wakes nobody.
        self.posts.clear()
        os.remove(chatnode.faucet_low_path())
        self.grant({"success": True}, {FAUCET: 9000})
        self.assertEqual([], self.posts)

    def test_a_refusal_on_the_faucets_OWN_cell_opens_the_spell_from_its_text(self):
        """After the grant that crossed the line every later grant is refused,
        so a watch that ran only on success would never run again. The node's
        refusal states the balance; no read is needed, and none is made."""
        refusal = {"success": False, "error": "insufficient balance on cell "
                   "4a8882bb17c23b3e: need 697, have 593"}
        _r, err = self.grant(refusal, {})
        self.assertIn("insufficient balance", err)
        self.assertEqual(1, len(self.posts), self.posts)
        self.assertIn("holds 593", self.posts[0][1])
        self.assertIn("DRY already", self.posts[0][1])

    def test_other_refusals_open_nothing(self):  # noqa: VACUOUS_ASSERTION — the positive control is the sibling arm test_a_refusal_on_the_faucets_OWN_cell_opens_the_spell_from_its_text, which drives the same grant() seam to one post
        for error in ("rate limited: 1 request per cell per minute",
                      # the RECIPIENT's own shortfall is not the faucet's
                      "insufficient balance on cell %s: need 697, have 0"
                      % SOURCE[:16]):
            with self.subTest(error=error):
                _r, err = self.grant({"success": False, "error": error}, {})
                self.assertIsNotNone(err)
        self.assertEqual([], self.posts)


class RefuelKeepsAFeeMargin(FaucetBase):
    """THE SOURCE PAYS A FEE TOO. The signer tops a short SENDING cell up from
    the faucet for its turn fee, so a refuel of the source's whole balance
    asked the dry faucet to pay for its own refill — and failed live."""

    # The door's harness, borrowed rather than inherited: inheriting
    # RefuelDoor would run every one of its arms a second time.
    run_refuel = RefuelDoor.run_refuel

    def setUp(self):
        super().setUp()
        scrub_env(self, chatnode.LOW_GRANTS_ENV)

    def plan_out(self, argv, source):
        self.profile("helm-agent", "ca75")
        return self.run_refuel(argv, {FAUCET: 260, SOURCE: source},
                               cells={SOURCE: "ca75"})

    def test_the_default_moves_the_balance_less_two_fees(self):
        rc, out, runner = self.plan_out([], 10000)
        self.assertEqual(0, rc, out)
        self.assertIn("would move 8606 computrons", out)
        self.assertIn("keeping 1394 for its own fee", out)
        runner.assert_not_called()  # noqa: VACUOUS_ASSERTION — a plan without --apply moves nothing; the spy's positive control is test_the_door_moves_a_named_amount_from_a_funded_cell_into_the_faucet

    def test_the_default_at_the_margin_edge(self):
        rc, out, _r = self.plan_out([], 2 * 697)
        self.assertEqual(1, rc, out)
        self.assertIn("REFUSED", out)
        self.assertIn("margin (1394)", out)
        rc, out, _r = self.plan_out([], 2 * 697 + 1)
        self.assertEqual(0, rc, out)
        self.assertIn("would move 1 computrons", out)

    def test_an_explicit_amount_may_leave_exactly_one_fee_and_no_less(self):
        rc, out, _r = self.plan_out(["--amount", "9303"], 10000)
        self.assertEqual(0, rc, out)
        self.assertIn("keeping 697 for its own fee", out)
        rc, out, runner = self.plan_out(["--amount", "9304", "--apply"], 10000)
        self.assertEqual(1, rc, out)
        self.assertIn("would leave 696 in the source, less than one fee (697)",
                      out)
        self.assertIn("Move at most 9303", out)
        runner.assert_not_called()  # noqa: VACUOUS_ASSERTION — refused before the signer; the spy's positive control is the 9303 arm above and test_the_door_moves_a_named_amount_from_a_funded_cell_into_the_faucet

    def test_a_LOW_faucet_is_refuelled_not_called_funded(self):
        """The LOW warning names this verb; answering it "already funded"
        would be a remedy that contradicts the warning that prescribed it."""
        self.profile("helm-agent", "ca75")
        rc, out, _r = self.run_refuel([], {FAUCET: 2000, SOURCE: 10000},
                                      cells={SOURCE: "ca75"})
        self.assertEqual(0, rc, out)
        self.assertNotIn("already funded", out)
        self.assertIn("would move 8606", out)

    def test_a_dry_run_against_a_recovered_faucet_closes_the_spell(self):
        """The funded answer is a reading that is not LOW, and a reader
        closes the spell it finds open — no grant, no move, no post."""
        with self.node({FAUCET: 2000}):
            woke = chatnode.faucet_watch(
                NODE, post=lambda text, room: {"id": "w"},
                roster={"night-integrator": {"home_room": "helm"}})
        self.assertIsNotNone(woke, "control: the spell opened")
        rc, out, runner = self.run_refuel([], {FAUCET: 9000, SOURCE: 10000})
        self.assertEqual(0, rc, out)
        self.assertIn("already funded", out)
        self.assertEqual({}, chatnode.low_spell(NODE, FAUCET),
                         "a funded dry run closes the spell")
        runner.assert_not_called()  # noqa: VACUOUS_ASSERTION — a funded dry run moves nothing; the spy's positive control is test_the_door_moves_a_named_amount_from_a_funded_cell_into_the_faucet

    def test_the_margin_is_rechecked_against_the_source_at_the_move(self):
        self.profile("helm-agent", "ca75")
        balances = {FAUCET: 260, SOURCE: 10000}

        def drain(_verb):
            balances[SOURCE] = 9000   # the source spends between plan and move
            return True

        rc, out, runner = self.run_refuel(
            ["--apply", "--amount", "9303"], balances, cells={SOURCE: "ca75"},
            supports=drain)
        self.assertEqual(1, rc, out)
        self.assertIn("REFUSED at the moment of the move", out)
        runner.assert_not_called()  # noqa: VACUOUS_ASSERTION — refused before the signer; the spy's positive control is test_the_door_moves_a_named_amount_from_a_funded_cell_into_the_faucet


class RefuelSettlesUnknownFromBalances(FaucetBase):
    """THE OLD NODE CANNOT ANSWER BY HASH: its /api/receipts ignores the
    turn_hash filter and returns the chain head, so the signer exits rc 1 with
    "outcome UNKNOWN" over transfers that committed — measured live. The two
    balances settle it; the door never retries."""

    # The door's harness, borrowed rather than inherited: inheriting
    # RefuelDoor would run every one of its arms a second time.
    run_refuel = RefuelDoor.run_refuel

    def setUp(self):
        super().setUp()
        scrub_env(self, chatnode.LOW_GRANTS_ENV)

    AMOUNT, FEE = 8606, 697

    def move(self, effect):
        """A fake signer that exits rc 1 with no receipt, after `effect`
        changes the fake node's balances the way the node would have."""
        self.profile("helm-agent", "ca75")
        balances = {FAUCET: 260, SOURCE: 10000}

        def signer(*_a, **_k):
            effect(balances)
            return (1, "", "outcome UNKNOWN: the receipt for this turn is not "
                            "the chain head")

        return self.run_refuel(["--apply"], balances, cells={SOURCE: "ca75"},
                               run_bin=mock.Mock(side_effect=signer))

    def test_both_balances_moved_as_one_transfer_is_COMMITTED(self):  # noqa: VACUOUS_ASSERTION — the empty slept list is the claim; the sibling NOT COMMITTED arm drives the same harness and asserts the waits were taken
        def commit(b):
            b[SOURCE] -= self.AMOUNT + self.FEE
            b[FAUCET] += self.AMOUNT

        rc, out, runner = self.move(commit)
        self.assertEqual(0, rc, out)
        self.assertEqual(1, len([l for l in out.splitlines()
                                 if "COMMITTED, confirmed from balances" in l]),
                         out)
        self.assertIn("fell by 9303 (8606 + a 697 fee)", out)
        self.assertIn("Do NOT retry", out)
        self.assertEqual([], self.slept, "a settled answer does not wait")
        self.assertEqual(1, runner.call_count, "never retried")

    def test_neither_balance_moved_is_NOT_COMMITTED_after_waiting(self):  # noqa: VACUOUS_ASSERTION — rc 1, the NOT COMMITTED line and the recorded waits are unconditional positive observations on the same run
        rc, out, runner = self.move(lambda b: None)
        self.assertEqual(1, rc, out)
        self.assertEqual(1, len([l for l in out.splitlines()
                                 if "NOT COMMITTED" in l]), out)
        self.assertIn("safe", out)
        self.assertEqual(list(chatnode.REREAD_DELAYS_S), self.slept,
                         "an in-flight move is given time to show")
        self.assertEqual(1, runner.call_count, "never retried")

    def test_a_move_that_shows_on_a_LATER_read_is_still_COMMITTED(self):
        """THE WAIT IS LOAD-BEARING: a turn still in flight reads as nothing
        on the first look."""
        self.profile("helm-agent", "ca75")
        balances = {FAUCET: 260, SOURCE: 10000}

        def land_later(_seconds):
            balances[SOURCE] = 10000 - self.AMOUNT - self.FEE
            balances[FAUCET] = 260 + self.AMOUNT

        with mock.patch.object(chatnode, "_sleep", land_later):
            reads = {"source": SOURCE, "faucet": FAUCET, "url": NODE,
                     "amount": self.AMOUNT, "fee": self.FEE}
            with self.node(balances):
                verdict, said = chatnode.classify_by_balances(reads, 10000, 260)
        self.assertEqual("committed", verdict, said)

    def test_anything_else_stays_UNKNOWN_with_both_deltas(self):
        def half(b):
            b[SOURCE] -= self.AMOUNT + self.FEE   # source paid, faucet did not
                                                  # rise: a grant went out too
        rc, out, runner = self.move(half)
        self.assertEqual(1, rc, out)
        self.assertEqual(1, len([l for l in out.splitlines()
                                 if "outcome still UNKNOWN" in l]), out)
        self.assertIn("fell by 9303", out)
        self.assertIn("rose by 0", out)
        self.assertNotIn("COMMITTED", out)
        self.assertIn("MAY move the value twice", out)
        self.assertEqual(1, runner.call_count, "never retried")

    def test_a_TIMED_OUT_signer_never_settles_as_NOT_COMMITTED(self):  # noqa: VACUOUS_ASSERTION — rc 1, the one 'outcome still UNKNOWN' line, the 'no change seen within 4.5 s' text and the recorded waits are unconditional positives on the same output
        """A signer killed at its budget may have left a submit queued at the
        node; "safe to re-run" there is how the value moves twice."""
        rc, out, runner = self.move_timed_out(lambda b: None)
        self.assertEqual(1, rc, out)
        self.assertEqual(1, len([l for l in out.splitlines()
                                 if "outcome still UNKNOWN" in l]), out)
        self.assertIn("no change seen within 4.5 s", out)
        self.assertNotIn("NOT COMMITTED", out)
        self.assertNotIn("safe", out)
        self.assertEqual(list(chatnode.REREAD_DELAYS_S), self.slept)
        self.assertEqual(1, runner.call_count, "never retried")

    def test_a_TIMED_OUT_signer_whose_move_shows_is_still_COMMITTED(self):
        def commit(b):
            b[SOURCE] -= self.AMOUNT + self.FEE
            b[FAUCET] += self.AMOUNT

        rc, out, _r = self.move_timed_out(commit)
        self.assertEqual(0, rc, out)
        self.assertIn("COMMITTED, confirmed from balances", out)

    def move_timed_out(self, effect):
        """The fake signer runs past its budget: run_bin's (None, "",
        BinTimeout) after `effect` changes the fake node's balances."""
        self.profile("helm-agent", "ca75")
        balances = {FAUCET: 260, SOURCE: 10000}

        def signer(*_a, **_k):
            effect(balances)
            return (None, "", cell.BinTimeout(
                "cell transfer timed out after 60s; outcome unknown"))

        return self.run_refuel(["--apply"], balances, cells={SOURCE: "ca75"},
                               run_bin=mock.Mock(side_effect=signer))

    def test_a_faucet_rise_with_no_source_fall_is_not_a_commit_either(self):
        def someone_else(b):
            b[FAUCET] += self.AMOUNT       # another refuel landed instead

        rc, out, _r = self.move(someone_else)
        self.assertEqual(1, rc, out)
        self.assertNotIn("COMMITTED", out)
        self.assertIn("outcome still UNKNOWN", out)



class ReactiveTopUp(FaucetBase):
    """THE TOP-UP IS REACTIVE. Measured on the live node: chat turns are
    fee-free (a post left helm-agent's balance untouched), so every cell sits
    at zero, and a top-up that fired before every send under 3000 asked for a
    grant nothing needed — each one a faucet Transfer whose own fee burns. The
    send goes first; only the node's refusal of THIS cell's fee asks the
    faucet, once, and the send is retried once."""

    SENT = {"sent": True, "turn_hash": "ab" * 32, "receipt_hash": "cd" * 32,
            "chain_index": 7}
    DRY = ("faucet refused: transfer rejected: insufficient balance on cell "
           "4a8882bb17c23b3e: need 697, have 593")

    def send(self, results, grant=({"success": True}, None)):
        """`_sign_send` over a fake signer (`results`, one per send) and a
        fake node that reports this cell at a balance of ZERO — the balance
        the old proactive rule topped up before every send."""
        signer = mock.Mock(side_effect=list(results))
        faucet = mock.Mock(return_value=grant)
        with self.node({SOURCE: 0, FAUCET: 593}), \
                mock.patch.object(cell, "bin_status", return_value={
                    "configured": True, "usable": True, "state": "ready",
                    "reason": "ready"}), \
                mock.patch.object(chat, "_node_token", return_value="t"), \
                mock.patch.object(chat, "_env_extra", return_value={}), \
                mock.patch.object(chat, "_room_cell",
                                  return_value=(SOURCE, None, True)), \
                mock.patch.object(chatnode, "faucet", faucet), \
                mock.patch.object(cell, "run_bin", signer):
            info, diag = chat._sign_send("hi", "seat-a")
        return info, diag, signer, faucet

    def sends(self, signer):
        return [c.args[0][0] for c in signer.call_args_list]

    def test_a_fee_free_send_from_a_zero_balance_cell_asks_the_faucet_nothing(self):
        info, diag, signer, faucet = self.send([(0, json.dumps(self.SENT), "")])
        self.assertIsNone(diag)
        self.assertEqual(7, info["chain_index"])
        self.assertEqual(["send"], self.sends(signer))
        faucet.assert_not_called()  # noqa: VACUOUS_ASSERTION — not-called IS the cure; the spy's positive control is test_a_fee_refusal_tops_up_once_retries_once_and_commits, which drives the same faucet spy through the same door to exactly one call

    def test_a_fee_refusal_tops_up_once_retries_once_and_commits(self):
        info, diag, signer, faucet = self.send(
            [(1, "", FEE_REFUSAL), (0, json.dumps(self.SENT), "")])
        self.assertIsNone(diag)
        self.assertEqual(7, info["chain_index"])
        self.assertEqual(["send", "send"], self.sends(signer),
                         "one send, one grant, one retry")
        faucet.assert_called_once_with(NODE, SOURCE, 10000)

    def test_a_fee_refusal_on_a_dry_faucet_reads_faucet_source_dry(self):
        info, diag, signer, faucet = self.send([(1, "", FEE_REFUSAL)],
                                               grant=(None, self.DRY))
        self.assertIsNone(info)
        self.assertEqual("faucet_source_dry", diag["code"])
        self.assertIn("insufficient balance on cell %s" % SOURCE[:16],
                      diag["reason"], "the node's refusal is kept")
        self.assertEqual(["send"], self.sends(signer),
                         "a failed top-up is not followed by a retry")
        faucet.assert_called_once_with(NODE, SOURCE, 10000)

    def test_a_non_fee_refusal_never_touches_the_faucet_and_stays_send_failed(self):  # noqa: VACUOUS_ASSERTION — every subTest asserts the send_failed code and the node's leading text positively; the faucet spy's positive control is test_a_fee_refusal_tops_up_once_retries_once_and_commits
        """The mislabel, measured on live rows: chain-race refusals were
        labelled faucet_source_dry because the faucet happened to be dry, and
        that sent the fleet to refuel. The faucet here IS dry and records a
        shortfall, so a dry-always-wins rule would fire on every one."""
        chatnode.record_faucet_shortfall("4a8882bb17c23b3e", 697, 593)
        for text in (
                "receipt chain mismatch: receipt chain mismatch: cipherclerk "
                "head = Some([7, 1]), receipt's prev = Some([9, 2])",
                "rejected: nonce replay: expected 5094, got 5093",
                "rejected: computron budget exceeded: limit=0, used=1254",
                # a shortfall on ANOTHER cell is not this cell's fee
                "rejected: insufficient balance on cell 4a8882bb17c23b3e: "
                "need 697, have 593"):
            with self.subTest(refusal=text):
                info, diag, signer, faucet = self.send(
                    [(1, "", "[client-sign] error: node refused the turn: "
                      + text)], grant=(None, self.DRY))
                self.assertIsNone(info)
                self.assertEqual("send_failed", diag["code"])
                self.assertTrue(diag["reason"].startswith(
                    "cell send rc 1: the node refused the turn, so it did "
                    "not commit: " + text), diag["reason"])
                self.assertNotIn(chatnode.FAUCET_DRY_MARK, diag["reason"])
                self.assertEqual(["send"], self.sends(signer))
                faucet.assert_not_called()

    def test_an_UNKNOWN_send_never_touches_the_faucet(self):
        info, diag, signer, faucet = self.send(
            [(1, "", "[client-sign] error: POST /turns/submit: disconnected")])
        self.assertEqual("send_outcome_unknown", diag["code"])
        self.assertEqual(["send"], self.sends(signer), "never replayed")
        faucet.assert_not_called()  # noqa: VACUOUS_ASSERTION — the faucet spy's positive control is test_a_fee_refusal_tops_up_once_retries_once_and_commits

    def test_a_retried_send_refused_again_is_never_retried_a_second_time(self):
        info, diag, signer, faucet = self.send(
            [(1, "", FEE_REFUSAL), (1, "", FEE_REFUSAL),
             (0, json.dumps(self.SENT), "")])
        self.assertIsNone(info)
        self.assertEqual("send_failed", diag["code"])
        self.assertEqual(["send", "send"], self.sends(signer))
        faucet.assert_called_once_with(NODE, SOURCE, 10000)
        self.assertIn("this is the one retry", diag["reason"])

    FUNDING = "[client-sign] error: faucet refused funding: "

    def funding_refusal(self, error):
        """The bound signer's last line when ensure_cell — which runs BEFORE
        POST /turns/submit — gets success:false: the faucet's JSON, compact,
        as serde_json renders it."""
        return self.FUNDING + json.dumps(
            {"success": False, "tx_hash": None, "turn_hash": None, "amount": 0,
             "error": error}, separators=(",", ":"))

    def test_a_pre_submit_funding_refusal_on_a_dry_faucet_reads_dry_never_unknown(self):
        info, diag, signer, faucet = self.send([(1, "", self.funding_refusal(
            "transfer rejected: insufficient balance on cell 4a8882bb17c23b3e: "
            "need 697, have 593"))])
        self.assertIsNone(info)
        self.assertEqual("faucet_source_dry", diag["code"])
        self.assertIn("never submitted", diag["reason"])
        self.assertNotIn("outcome unknown", diag["reason"])
        self.assertEqual(["send"], self.sends(signer), "never replayed")
        self.assertEqual(697, chatnode.faucet_shortfall()["need"],
                         "the faucet's refusal is recorded like any other")
        faucet.assert_not_called()  # noqa: VACUOUS_ASSERTION — helm asks nothing of a faucet that just refused; the spy's positive control is test_a_fee_refusal_tops_up_once_retries_once_and_commits

    def test_any_other_pre_submit_funding_refusal_is_send_failed_never_unknown(self):  # noqa: VACUOUS_ASSERTION — every subTest asserts the send_failed code and the kept text positively
        for stderr, kept in (
                (self.funding_refusal("public_key does not derive the recipient "
                                      "cell"),
                 "public_key does not derive the recipient cell"),
                (self.FUNDING + "not json at all", "not json at all")):
            with self.subTest(kept=kept):
                info, diag, signer, _f = self.send([(1, "", stderr)])
                self.assertEqual("send_failed", diag["code"])
                self.assertIn("never submitted", diag["reason"])
                self.assertIn(kept, diag["reason"])
                self.assertNotIn("outcome unknown", diag["reason"])
                self.assertEqual(["send"], self.sends(signer))

    def test_a_funding_line_that_is_not_the_signers_last_word_stays_UNKNOWN(self):
        info, diag, signer, _f = self.send([(1, "", self.funding_refusal(
            "rate limited") + "\n[client-sign] error: GET receipts: reset")])
        self.assertEqual("send_outcome_unknown", diag["code"])
        self.assertNotIn("never submitted", diag["reason"])

    def test_a_rate_limited_top_up_keeps_the_nodes_refusal_first(self):
        info, diag, signer, faucet = self.send(
            [(1, "", FEE_REFUSAL)],
            grant=(None, "faucet refused: rate limited: 1 request per cell "
                         "per minute"))
        self.assertEqual("send_failed", diag["code"])
        self.assertTrue(diag["reason"].startswith(
            "cell send rc 1: the node refused the turn"), diag["reason"])
        self.assertIn("the top-up failed: faucet refused: rate limited",
                      diag["reason"])
        self.assertEqual(["send"], self.sends(signer))


if __name__ == "__main__":
    unittest.main()
