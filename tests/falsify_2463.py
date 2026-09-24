"""task/2463 falsification -- RUN IT, do not read it.

    fab test --repo . -- python3 tests/falsify_2463.py

Every cure is mutated back and its arm is run in a fresh interpreter through
the unittest API. The verdict is STRUCTURED, never read off an exit code or a
substring of the output:

  RED      the named test was reached, no test errored, and exactly one test
           failed: the named one, with an AssertionError whose own message
           carries the arm's target text.
  GREEN    the named test was reached and passed: the mutant survived.
  INVALID  anything else -- the module did not import, a setUp raised, the
           test was never reached, another test failed, or the failure was not
           the intended assertion. An import error is INVALID even when the
           target text appears somewhere in its output.

Each arm's CONTROL (the unmutated tree) must PASS, every mutant must read RED,
and the runner's own self-test -- a mutant that cannot import, whose error
text is exactly the target text -- must read INVALID. Every mutated file is
restored and compared against its pre-mutation sha256. The last line of
output is one JSON summary; the exit status is 0 only when all of it holds.
"""
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
MARK = "FALSIFY-2463-RESULT "

B, R, H = "helm/beacons.py", "helm/resumeturn.py", "helm/harness.py"
O, S = "helm/orcaadopt.py", "helm/seats_stop_fp.py"
P, D = "helm/proxywatch.py", "docs/ENVIRONMENT.md"
TB = "tests.test_beacons.TheNudgeRidesTheRevalidatedEdgeTest."
TR = "tests.test_resumeturn."
TS = "tests.test_seats.APreparedKeystrokeIsValidatedByItsInputsTest."
TF = "tests.test_seats_final_capture.TheFinalCaptureIsTheLastWordTest."
TE = "tests.test_resumeturn.TheActDoorsOwnEdgesTest."

#: (label, file, target test id, expected assertion text, anchor, replacement)
MUTANTS = [
    ("M1 install reopens a settled spell", B,
     TB + "test_a_stale_snapshot_cannot_reopen_a_settled_spell",
     "a stale snapshot opened an episode on a settled spell",
     '''            if isinstance(prior, dict) and prior.get("since") == spell \\
                    and prior.get("outcome") in _REPAIR_SETTLED:
                return None                  # already settled; nothing to open
''', ""),
    ("M2 a refusal writes the episode", B,
     TB + "test_a_refused_wake_does_not_displace_the_running_child",
     "a refused wake rewrote the running child's episode",
     '''                if mine is not None:
                    if have != mine:
                        continue             # not the episode this result ran
                elif got.get("action") != "drained" \\
                        or (isinstance(ep, dict)
                            and ep.get("since") == att.get("since")
                            and ep.get("outcome") in _REPAIR_ATTEMPTED):
                    continue                 # a refusal opened nothing to write
''', ""),
    ("M3a a nameless report closes a named episode", B,
     TB + "test_a_child_with_no_name_cannot_close_a_named_episode",
     "a nameless report was allowed to close a named episode",
     '''            if attempt is None and ep.get("attempt"):
                return False
''', ""),
    ("M3b a failed install launches", R,
     TR + "TheEpisodeIsOneTransactionTest."
          "test_a_failed_required_install_launches_nothing",
     "an unbound child was launched",
     '''        if attempt is None:
            return {"action": "alert", "detail": (
''', '''        if False:
            return {"action": "alert", "detail": (
'''),
    ("M3c the install precedes the refusals", R,
     TR + "TheEpisodeIsOneTransactionTest."
          "test_the_install_is_the_last_act_before_the_fork",
     "to not have been called",
     '''    key = _repair_key(seat_name, session)
    action, detail = _decide(_peek(key), time.time())
''', '''    key = _repair_key(seat_name, session)
    if att is not None:
        from . import beacons as _mutant_beacons
        _mutant_beacons.install_repair(seat_name, att, time.time())
    action, detail = _decide(_peek(key), time.time())
'''),
    ("M4a the ENTER door ignores its admission", H,
     TR + "OneActDoorTest.test_an_enter_refused_at_its_door_is_withheld_and_named",
     "Enter was pressed after the reason expired",
     '''            grant = admit(door)
            if not grant.ok:
''', '''            grant = admit(door)
            if not grant.ok and door != "enter":
'''),
    ("M4b the adopted branch drops the authorization", O,
     TR + "TheEpisodeIsOneTransactionTest."
          "test_every_deliver_branch_hands_the_admission_to_the_adapter",
     "the adopted branch",
     '''        state, proof = adapter.submit(handle, text, on_typed=typed,
                                      **({"admit": admit} if admit is not None
                                         else {}))
''', '''        state, proof = adapter.submit(handle, text, on_typed=typed)
'''),
    ("M4c the whole-delivery retry drops the authorization", R,
     TR + "TheEpisodeIsOneTransactionTest."
          "test_the_whole_delivery_retry_carries_the_admission",
     "the RETRY delivery carried no admission",
     '''            on_submit=capture, admit=admit, **carried)
        refusals.append(detail)
''', '''            on_submit=capture, **carried)
        refusals.append(detail)
'''),
    ("M5 an unreadable census is spelled as a drain", R,
     TS + "test_an_unreadable_census_withholds_and_is_never_a_drain",
     "'drained' != 'unknown'",
     '''    if waited is None:
        return harness.Grant(False, (
''', '''    if waited is None or waited is _NO_CACHED_WAIT:
        return harness.Grant(False, (
'''),
    ("M6 a charge is judged by the spell's age", R,
     TR + "AnAttemptIsBornOnceAndChargedOnceTest."
          "test_a_fresh_retry_of_an_old_spell_is_charged_and_rate_limited",
     "a fresh retry of an old spell was not charged",
     '''        elif standing in ("current", "legacy"):
''', '''        elif standing in ("current", "legacy") and \\
                now - float(str(attempt).split("#")[0]) < _window_s():
'''),
    ("M7 a stale mint launches", R,
     TR + "AnAttemptIsBornOnceAndChargedOnceTest."
          "test_a_stale_mint_is_refused_and_re_minted",
     "!= 'stale'",
     '''        if age >= _debounce_s():
''', '''        if False:
'''),
    ("M8 a retired attempt is accounted", R,
     TR + "AnAttemptIsBornOnceAndChargedOnceTest."
          "test_a_delayed_second_writer_never_recounts",
     "a delayed writer of a retired attempt recounted",
     '''        else:
            paid, refused = True, standing
''', '''        else:
            paid, refused = False, standing
'''),
    ("M9 the validator ignores the owed bytes", S,
     TS + "test_owed_bytes_that_vanish_without_a_cursor_advance_refuse",
     "a refilled byte range validated as the owed row",
     '''        if got != occ.get("bytes"):
''', '''        if False:
'''),
    ("M10 an interrupted observation is kept", S,
     TS + "test_a_cursor_commit_during_the_preparation_discards_it",
     "a preparation kept an observation a cursor commit landed inside",
     '''        report["coherent"] = (before == after and not before[1]
                              and not after[1])
''', '''        report["coherent"] = True
'''),
    ("M11 the placement proof precedes its preparation", H,
     TR + "OneActDoorTest."
          "test_a_human_draft_during_the_placement_preparation_places_nothing",
     "'not-delivered' != 'unknown'",
     '''        placed, got = self._through_door(
            "placement", admit, clean, self._final_clean(handle), type_it)
''', '''        early = clean()
        placed, got = self._through_door(
            "placement", admit, lambda: early, self._final_clean(handle),
            type_it)
'''),
    ("M12 a recovery Enter skips its own door", H,
     TR + "APlacedDirectiveKeepsItsOwnerTest."
          "test_every_recovery_attempt_reauthorizes",
     "the recovery Enter was never prepared at its own door",
     '''                admitted, got = self._through_door(
                    "recovery-attempt", admit, exact_read,
                    self._final_exact(handle, text), enter)
''', '''                admitted, got = True, (enter(), None)
'''),
    ("M13 an unreadable composer is reported as an edit", H,
     TR + "OneActDoorTest.test_chrome_only_after_holds_is_unreadable_not_an_edit",
     "an unreadable composer was reported as a human edit",
     '''                         "because the composer could not be read, NOT because "
                         "anyone edited it"
''', '''                         "because the composer could not be read and "
                         "a human may have edited it"
'''),
    ("M14 the placement validation is ignored", H,
     TR + "OneActDoorTest.test_a_pause_set_during_the_pre_read_places_nothing",
     "helm typed after the pause landed",
     '''        if valid is not True:
            return "moved", ran(why or "an input of the authorization")
''', '''        if valid is not True and door != "placement":
            return "moved", ran(why or "an input of the authorization")
'''),
    ("M15 the parent's outcome write drops the birth", B,
     TR + "AnAttemptIsBornOnceAndChargedOnceTest."
          "test_the_parents_outcome_write_keeps_the_birth",
     "the parent's outcome write erased the attempt's birth",
     '''                    cur["repair"]["born"] = ep["born"]
''', '''                    pass
'''),
    ("M16 a child delivers after its charge was refused", R,
     TR + "AnAttemptIsBornOnceAndChargedOnceTest."
          "test_a_charge_refused_at_the_launch_launches_nothing",
     "a child whose charge was refused (retired) still delivered",
     '''    stale = _launch_charge(key, session, text, attempt, seat_name)
    if stale:
''', '''    stale = _launch_charge(key, session, text, attempt, seat_name)
    if False:
'''),
    ("M17 the launch horizon is the rate window", R,
     TR + "AnAttemptIsBornOnceAndChargedOnceTest."
          "test_a_mint_past_its_debounce_is_stale",
     "a mint past its debounce launched",
     '''        if age >= _debounce_s():
''', '''        if age >= _window_s():
'''),
    ("M18 an unreadable standing charges nothing", R,
     TR + "AnAttemptIsBornOnceAndChargedOnceTest."
          "test_an_unreadable_standing_still_debounces_the_next_pass",
     "an unreadable standing disabled the limiter (malformed row)",
     '''            paid, refused = str(attempt) in receipts, standing
''', '''            paid, refused = True, standing
'''),
    ("M19 a launch keeps the mint's stamp", R,
     TR + "AnAttemptIsBornOnceAndChargedOnceTest."
          "test_a_launch_re_dates_its_one_stamp",
     "a launch kept the mint's stamp",
     '''            redate = launching and paid
''', '''            redate = False
'''),
    ("M20 the act door drops its final dependency capture", H,
     TF + "test_a_retired_attempt_is_refused_at_the_act",
     "a superseded attempt typed",
     '''        done, got = _bounded(grant.capture, deadline)
''', '''        done, got = True, (True, "", {})
'''),
    ("M21 the act door drops its final composer capture", H,
     TR + "OneActDoorTest."
          "test_a_human_edit_during_the_authorization_reads_is_seen_last",
     "was not seen by the final composer capture",
     '''        if final is not None:
            left = deadline - (composer_start - start)
''', '''        if False:
            left = deadline - (composer_start - start)
'''),
    ("M22 an act is not accounted", R,
     TF + "test_an_authorization_change_after_the_capture_is_accounted",
     "the obsolete act was not accounted",
     '''    return harness.Grant(True, still=capture,
                         account=account if key else None)
''', '''    return harness.Grant(True, still=capture, account=None)
'''),
    ("M23 the act door has no deadline", H,
     TR + "OneActDoorTest."
          "test_a_stalled_final_capture_refuses_inside_its_deadline",
     "acted on a capture that stalled past its deadline",
     '''    return value if value > 0 else ACT_DEADLINE_S
''', '''    return 1e6
'''),
    ("M24 a repeat of an obsolete act is not refused", R,
     TF + "test_an_authorization_change_after_the_capture_is_accounted",
     "a repeat of an obsolete act was not refused",
     '''    if key and door in REPEAT_DOORS:
''', '''    if False:
'''),
    ("M25 the seqlock bracket closes before the room bytes", S,
     TF + "test_a_consumption_during_the_byte_read_is_inside_the_bracket",
     "still validated",
     '''            seen = _occurrence_bytes(room, occ)
            after = _room_epoch(room, seat, session)
''', '''            after = _room_epoch(room, seat, session)
            seen = _occurrence_bytes(room, occ)
'''),
    ("M26 a failed write discards a determined launch refusal", R,
     TR + "AnAttemptIsBornOnceAndChargedOnceTest."
          "test_a_determined_refusal_survives_a_failed_state_write",
     "a refusal the charge determined was discarded",
     '''        return verdict.get("refused") or ""
''', '''        return ""
'''),
    ("M27 the final capture does not judge the attempt", R,
     TF + "test_a_retired_attempt_is_refused_at_the_act",
     "a retired attempt validated at the act",
     '''        stale, facts["horizon"] = _attempt_refusal(attempt, row, time.time())
        if stale:
            return False, stale, facts
''', '''        stale, facts["horizon"] = _attempt_refusal(attempt, row, time.time())
'''),
    ("M28 the act closure drops the attempt", R,
     TR + "AnAttemptIsBornOnceAndChargedOnceTest."
          "test_the_act_closure_carries_the_attempt",
     "an act door was prepared without the attempt",
     '''                                        door=door, attempt=attempt, key=key))
''', '''                                        door=door))
'''),
    ("M29 a missing family record permits", R,
     TF + "test_a_missing_selected_family_record_is_unknown",
     "authorized a keystroke",
     '''    if why or not recognised:
''', '''    if False:
'''),
    ("M30 the accounting ignores a changed-and-restored version", H,
     TF + "test_an_authorization_change_after_the_capture_is_accounted",
     "a changed-and-restored pause was not accounted",
     '''        if before != after:
''', '''        if False:
'''),
    ("M31 a final composer read that raises escapes the door", H,
     TE + "test_a_final_read_that_raises_is_unknown_and_the_child_survives",
     "a final read that raised escaped the act door",
     '''            except (HarnessError, OSError) as e:
                return None, ("pane %s could not be read at the final "
                              "capture: %s" % (handle, e))
            state = observe_composer(tail, text)
''', '''            except ZeroDivisionError as e:
                return None, ("pane %s could not be read at the final "
                              "capture: %s" % (handle, e))
            state = observe_composer(tail, text)
'''),
    ("M32 an unreadable final composer is spelled as moved", H,
     TE + "test_a_final_read_that_raises_is_unknown_and_the_child_survives",
     "'moved' != 'unknown'",
     '''            if seen[0] is None:
''', '''            if False:
'''),
    ("M33 a failed accounting write forgets the obsolete act", R,
     TF + "test_a_failed_accounting_write_still_refuses_the_repeat",
     "whose accounting write failed was admitted",
     '''    if obsolete and named:
        _OBSOLETE_HERE[(key, mine)] = {"door": receipt.get("door"),
''', '''    if False:
        _OBSOLETE_HERE[(key, mine)] = {"door": receipt.get("door"),
'''),
    ("M34 composers --submit repeats an obsolete act", R,
     TF + "test_submit_refuses_the_stranded_text_of_an_obsolete_act",
     "composers --submit took an obsolete act past the recovery entry",
     '''    spent = _obsolete_act(injection.get("account_key"),
                          injection.get("attempt"))
    if spent:
''', '''    spent = _obsolete_act(injection.get("account_key"),
                          injection.get("attempt"))
    if False:
'''),
    ("M35 the act's send is unbounded", H,
     TE + "test_the_send_that_carries_the_keystroke_is_bounded",
     "the act's send was not bounded",
     '''    value = _env_float("HELM_ACT_SEND_S", ACT_SEND_S)
    return value if value > 0 else ACT_SEND_S
''', '''    return 1e6
'''),
    ("M36a the roster is compared by file identity", R,
     TF + "test_an_unrelated_writer_inside_the_window_leaves_the_act_clean",
     "stable authority was labeled obsolete",
     '''    paths = [("rename journal", rename_journal_path()),
''', '''    from .seats_common import roster_path
    paths = [("roster", roster_path()),
             ("rename journal", rename_journal_path()),
'''),
    ("M36b the pause is compared by file identity", R,
     TF + "test_an_unrelated_writer_inside_the_window_leaves_the_act_clean",
     "stable authority was labeled obsolete",
     '''    paths = [("rename journal", rename_journal_path()),
''', '''    from . import proxywatch
    paths = [("pause file", proxywatch._state_path()),
             ("rename journal", rename_journal_path()),
'''),
    ("M37 an act records no intent", R,
     TF + "test_a_crash_after_the_keystroke_leaves_an_intent_that_refuses",
     "an act a crash left unaccounted was repeated",
     '''        if grant.ok and nonce:
''', '''        if False:
'''),
    ("M38 an unnamed attempt is bound as 'None'", R,
     TF + "test_an_unnamed_attempt_leaves_no_record_for_the_next_child",
     "an unnamed child's obsolete act refused the next unnamed child",
     '''    return attempt is not None
''', '''    return True
'''),
    ("M39 the recovery capture drops the directive horizon", R,
     TE + "test_a_recovery_enter_past_its_directive_horizon_is_refused",
     "an Enter was pressed after the directive it points at expired",
     '''            if expires is not None:
''', '''            if False:
'''),
    ("M40 the spawn register is not a dependency", R,
     TE + "test_a_project_seat_s_spawn_register_is_a_dependency",
     "is not a dependency",
     '''    if register:
''', '''    if False:
'''),
    ("M41 a stalled real dependency read has no deadline", H,
     TF + "test_a_stalled_dependency_read_refuses_inside_its_deadline",
     "acted on a dependency read that stalled past its deadline",
     '''    return value if value > 0 else ACT_DEADLINE_S
''', '''    return 1e6
'''),
    ("M42 an unchanged-looking pause keeps its transition identity", P,
     TF + "test_a_pause_changed_and_restored_inside_one_second_is_accounted",
     "a placement that ran under a pause changed and restored inside one "
     "second was accounted",
     '''    if isinstance(prior, str) and prior and before.get("state") == state \\
            and (before.get("dark") is True) == bool(dark):
''', '''    if isinstance(prior, str) and prior:
'''),
    ("M43 the obsolete check reads the resume state leniently", R,
     TF + "test_an_unreadable_obsolete_check_refuses_the_recovery_enter",
     "a recovery went past an obsolete check that could not read the resume "
     "state",
     '''        entry = _read_store().get(key) or {}
''', '''        from . import pk
        entry = (pk.read_json(state_path(), {}) or {}).get(key) or {}
'''),
    ("M44 a payload refusal keeps the original grant's intent", R,
     TF + "test_a_payload_refused_after_the_intent_withdraws_the_intent",
     "a recovery refused on its payload before any keystroke left its act "
     "intent",
     '''            grant.abandon()
            return harness.Grant(False, refusal, "unknown")
''', '''            return harness.Grant(False, refusal, "unknown")
'''),
    ("M45 a returned unreadable final composer is spelled as moved", H,
     TE + "test_a_returned_unreadable_final_composer_reads_unknown",
     "the enter door's final helper answered False",
     '''            if state is UNREADABLE:
                return None, ("pane %s composer read UNREADABLE at the final "
''', '''            if False:
                return None, ("pane %s composer read UNREADABLE at the final "
'''),
    ("M46 the send bound row publishes an end-to-end bound", D,
     TE + "test_the_send_bound_row_publishes_no_end_to_end_bound",
     "The enforced end-to-end bound of an act is",
     "It is separate from `HELM_ACT_DEADLINE_S`, the capture admission "
     "window that ends when the act callback starts.",
     "The enforced end-to-end bound of an act is `HELM_ACT_DEADLINE_S` plus "
     "this, to the send's return."),
    ("M47 a bookkeeping write reads the resume state leniently", R,
     TF + "test_a_bookkeeping_write_that_cannot_read_the_state_keeps_the_"
          "obsolete_record",
     "a bookkeeping write that could not read the resume state erased the "
     "obsolete record",
     '''        st = _read_store()
        now = time.time()
''', '''        st = pk.read_json(p, {}) or {}
        now = time.time()
'''),
    ("M48a the acknowledgement write composes against the original prior", P,
     "tests.test_proxywatch.UpstreamTransitionTest."
     "test_a_posting_pass_keeps_one_transition_identity_through_its_"
     "acknowledgement",
     "the acknowledgement write of the pass that lifted the pause minted a "
     "second transition identity",
     '''                      pending_ntfy=pending_ntfy, prior_state=written):
''', '''                      pending_ntfy=pending_ntfy, prior_state=prior):
'''),
    ("M48b the acknowledgement write composes against the original prior", P,
     TF + "test_the_watchers_acknowledgement_write_inside_the_act_leaves_it_"
          "clean",
     "the watcher's acknowledgement write of the pass that lifted the pause "
     "was accounted as a change",
     '''                      pending_ntfy=pending_ntfy, prior_state=written):
''', '''                      pending_ntfy=pending_ntfy, prior_state=prior):
'''),
]

#: The runner's own falsifier: a mutant that cannot import, whose error text
#: IS the target text of a real arm. A runner that trusted a nonzero exit or a
#: substring would call it RED; this one must call it INVALID.
SELF_TEST = (
    "S0 a mutant that cannot import", H,
    TR + "OneActDoorTest.test_an_enter_refused_at_its_door_is_withheld_and_named",
    "Enter was pressed after the reason expired",
    '''class Grant(object):
''', '''raise RuntimeError("Enter was pressed after the reason expired")


class Grant(object):
''')


def probe(test_id):
    """Run ONE test id in this interpreter and print its structured result."""
    import unittest
    sys.path.insert(0, ROOT)

    def detail(test, err):
        return {"id": test.id(), "type": err[0].__name__,
                "message": str(err[1])[:4000]}

    class Result(unittest.TestResult):
        def __init__(self):
            super().__init__()
            self.seen, self.failed, self.errored = [], [], []

        def startTest(self, test):
            super().startTest(test)
            self.seen.append(test.id())

        def addFailure(self, test, err):
            super().addFailure(test, err)
            self.failed.append(detail(test, err))

        def addError(self, test, err):
            super().addError(test, err)
            self.errored.append(detail(test, err))

        def addSubTest(self, test, subtest, err):
            # A subTest's failure reaches the result HERE and never through
            # addFailure, so without this a mutant that reddens one subTest
            # reads GREEN. It is recorded under the test's own id.
            super().addSubTest(test, subtest, err)
            if err is not None:
                (self.failed if issubclass(err[0], test.failureException)
                 else self.errored).append(detail(test, err))

    out = {"seen": [], "failures": [], "errors": [], "skipped": []}
    loader = unittest.TestLoader()
    try:
        suite = loader.loadTestsFromName(test_id)
    except Exception as exc:                 # noqa: BLE001 — reported, INVALID
        out["errors"].append({"id": test_id, "type": type(exc).__name__,
                              "message": str(exc)[:4000]})
    else:
        result = Result()
        suite.run(result)
        out.update(seen=result.seen, failures=result.failed,
                   errors=result.errored,
                   skipped=[t.id() for t, _why in result.skipped])
        out["errors"] += [{"id": test_id, "type": "LoaderError",
                           "message": str(m)[:4000]} for m in loader.errors]
    sys.stdout.write("\n" + MARK + json.dumps(out) + "\n")
    sys.stdout.flush()


def run(test_id):
    """The structured result of one test id, in a fresh interpreter that
    compiles every module from source."""
    with tempfile.TemporaryDirectory(prefix="falsify-2463-pyc-") as cache:
        env = dict(os.environ, PYTHONPYCACHEPREFIX=cache,
                   PYTHONDONTWRITEBYTECODE="1")
        p = subprocess.run([sys.executable, os.path.abspath(__file__),
                            "--probe", test_id], cwd=ROOT, env=env,
                           capture_output=True, text=True)
    lines = [ln for ln in p.stdout.splitlines() if ln.startswith(MARK)]
    if not lines:
        return None, (p.stdout + p.stderr)[-2000:]
    return json.loads(lines[-1][len(MARK):]), ""


def control_verdict(res, test_id):
    if res is None:
        return "INVALID", "no structured result"
    if res["errors"]:
        return "INVALID", "errors: %s" % res["errors"][0]["type"]
    if test_id not in res["seen"]:
        return "INVALID", "the named test was not reached"
    if res["skipped"]:
        return "INVALID", "skipped"
    if res["failures"]:
        return "FAIL", res["failures"][0]["message"][:300]
    return "PASS", ""


def mutant_verdict(res, test_id, expect):
    if res is None:
        return "INVALID", "no structured result"
    if res["errors"]:
        return "INVALID", "%d error(s), first %s: %s" % (
            len(res["errors"]), res["errors"][0]["type"],
            res["errors"][0]["message"][:200])
    if test_id not in res["seen"]:
        return "INVALID", "the named test was not reached"
    if res["skipped"]:
        return "INVALID", "skipped"
    failures = res["failures"]
    if not failures:
        return "GREEN", "the mutant survived"
    first = failures[0]
    if len(failures) != 1 or first["id"] != test_id:
        return "INVALID", "a different or additional test failed"
    if first["type"] != "AssertionError":
        return "INVALID", "the failure was %s, not an AssertionError" % (
            first["type"])
    if expect not in first["message"]:
        return "INVALID", "the AssertionError was not the intended one: %s" % (
            first["message"][:200])
    return "RED", ""


def sha(path):
    with io.open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def mutate_and_run(mutant):
    label, path, test_id, expect, old, new = mutant
    path = os.path.join(ROOT, path)
    with io.open(path, encoding="utf-8") as f:
        src = f.read()
    before = sha(path)
    count = src.count(old)
    if count != 1:
        return {"label": label, "verdict": "INVALID",
                "why": "anchor count %d, want 1" % count, "restored": True}
    try:
        with io.open(path, "w", encoding="utf-8") as f:
            f.write(src.replace(old, new))
        res, raw = run(test_id)
    finally:
        with io.open(path, "w", encoding="utf-8") as f:
            f.write(src)
    verdict, why = mutant_verdict(res, test_id, expect)
    return {"label": label, "test": test_id, "verdict": verdict,
            "why": why or raw[-300:], "restored": sha(path) == before}


def main():
    summary = {"controls": [], "mutants": [], "self_test": None}
    ok = True
    for test_id in sorted({m[2] for m in MUTANTS}):
        res, raw = run(test_id)
        verdict, why = control_verdict(res, test_id)
        summary["controls"].append({"test": test_id, "verdict": verdict,
                                    "why": why or raw[-300:]})
        print("CONTROL %-7s %s %s" % (verdict, test_id.rsplit(".", 1)[-1], why))
        ok = ok and verdict == "PASS"
    for mutant in MUTANTS:
        row = mutate_and_run(mutant)
        summary["mutants"].append(row)
        print("MUTANT  %-7s %-52s %s%s" % (
            row["verdict"], row["label"],
            "" if row["restored"] else "sha256-DRIFT ", row["why"]))
        ok = ok and row["verdict"] == "RED" and row["restored"]
    row = mutate_and_run(SELF_TEST)
    summary["self_test"] = row
    print("SELF    %-7s %-52s %s" % (row["verdict"], row["label"], row["why"]))
    ok = ok and row["verdict"] == "INVALID" and row["restored"]
    counts = {}
    for row in summary["mutants"]:
        counts[row["verdict"]] = counts.get(row["verdict"], 0) + 1
    summary["counts"] = counts
    summary["ok"] = ok
    print(json.dumps(summary, sort_keys=True))
    return 0 if ok else 1


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--probe":
        probe(sys.argv[2])
        sys.exit(0)
    sys.exit(main())
