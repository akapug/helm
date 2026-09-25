#!/usr/bin/env python3
"""doctor tests — hermetic: a synthetic HELM_HOME in a tempdir with a broken
symlink, a missing repo path, a stale memory_dir, an adoption conflict, and a
dup-prefix adopted store. Never touches the real ~/.helm or ~/.claude."""
import contextlib
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

# `seat` IS IMPORTED EXPLICITLY at module scope, which is an ancestor of
# every function below: a method here imports helm.seat_credentials, and the
# co-occurrence guard in tests/test_seat_facade_injection.py requires an
# accepted facade import in the same or an enclosing scope. It asserts
# nothing about import order.
from helm import (chat, dispatches, doctor, home, pk, projscope, seat,  # noqa: F401
                  whoami, wiring)


def levels(results, level):
    return [msg for l, msg in results if l == level]


class DoctorBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.helm_home = os.path.join(self.tmp.name, "helm-home")
        scan = os.path.join(self.tmp.name, "scan-root")
        os.makedirs(scan)  # a live source for the registry projection row
        self.envp = mock.patch.dict(os.environ, {
            "HELM_HOME": self.helm_home,
            "HELM_PROFILE_HOME": os.path.join(self.tmp.name, "profile-home"),
            "HELM_CACHE_DIR": os.path.join(self.tmp.name, "cache"),
            "HELM_SCAN_ROOTS": scan,
        })
        self.envp.start()
        # THE FIRST CLEANUP RUNS LAST. Restored in tearDown, the environment
        # was put back BEFORE every addCleanup a test registered, and a
        # cleanup that restores its own env snapshot then re-planted this
        # fixture's paths into every module that ran after this one.
        self.addCleanup(self.envp.stop)
        os.environ.pop("MELD_HOME", None)
        self.assertTrue(home.helm_home().startswith(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def seed_home(self):
        """The synthetic estate: one healthy project + one issue per check."""
        home.scaffold_global()
        repo = os.path.join(self.tmp.name, "repos", "good")
        os.makedirs(repo)
        for name in ("good", "gone-repo", "brokelink", "memstale", "adopted-proj"):
            if name != "brokelink":
                home.scaffold_project(name)
        os.symlink(os.path.join(self.tmp.name, "no-such-target"),
                   home.project_dir("brokelink"))
        rec = lambda name, path, mem=None: {
            "name": name, "path": path, "kind": "git", "status": "active",
            "sessions": {}, "memory_dir": mem}
        pk.write_json(home.registry_path(), {"version": 1, "projects": {
            "good": rec("good", repo),
            "gone-repo": rec("gone-repo", os.path.join(self.tmp.name, "repos", "gone")),
            "brokelink": rec("brokelink", repo),
            "memstale": rec("memstale", repo, mem=os.path.join(self.tmp.name, "no-such-mem")),
            "adopted-proj": rec("adopted-proj", repo),
        }})
        # adopted-proj declares an adopt home in the authored layer but its helm
        # dir was scaffolded as a REAL dir, not the adoption symlink — the
        # conflict check_adoption() surfaces (adopted_homes reads authored `adopt`).
        pk.write_json(home.authored_path(), {"version": 1, "projects": {
            "adopted-proj": {"adopt": os.path.join(self.tmp.name, "external-home")}}})

    def seed_adopted(self):
        d = os.path.join(self.tmp.name, "adopted")
        os.makedirs(d)
        for f in ("prem-x.md", "prior-x.md", "prior-y.md", "lex-z.md",
                  "heuristic-h.md", "random.md"):
            pk.atomic_write(os.path.join(d, f), "stub\n")
        return d


class TestChecks(DoctorBase):
    def test_home_missing_is_warn_not_fail(self):
        results = doctor.check_home()
        self.assertTrue(any("helm home missing" in m for m in levels(results, doctor.WARN)))
        self.assertEqual(levels(results, doctor.FAIL), [])

    def test_registry_garbled_is_fail(self):
        home.scaffold_global()
        pk.atomic_write(home.registry_path(), "not json{")
        results = doctor.check_home()
        self.assertTrue(any("does not parse" in m for m in levels(results, doctor.FAIL)))

    def test_registry_parses_reports_count(self):
        self.seed_home()
        results = doctor.check_home()
        self.assertTrue(any("5 projects" in m for m in levels(results, doctor.OK)))

    def test_authored_absent_is_silent(self):
        home.scaffold_global()
        self.assertEqual(doctor.check_authored(), [])

    def test_authored_garbled_is_fail(self):
        home.scaffold_global()
        pk.atomic_write(home.authored_path(), "not json{")
        results = doctor.check_authored()
        self.assertTrue(any("registry-authored" in m and "does not parse" in m
                            for m in levels(results, doctor.FAIL)))

    def test_authored_merge_counts(self):
        self.seed_home()
        good = os.path.join(self.tmp.name, "repos", "good")
        pk.write_json(home.authored_path(), {"version": 1, "projects": {
            "good": {"path": good, "notes": "n"},           # path-matched: live
            "ghost": {"path": "/gone/elsewhere", "notes": "x"},  # orphan: not live
            "vend": {"path": "/v", "external": True},       # external anchor: live
        }})
        ok = levels(doctor.check_authored(), doctor.OK)[0]
        self.assertIn("3 entries", ok)
        self.assertIn("2 live in merge", ok)

    def test_projects_broken_symlink_missing_path_stale_memory(self):
        self.seed_home()
        results = doctor.check_projects()
        fails, warns = levels(results, doctor.FAIL), levels(results, doctor.WARN)
        self.assertTrue(any("brokelink" in m and "broken symlink" in m for m in fails))
        self.assertTrue(any("gone-repo" in m and "repo moved or deleted" in m for m in warns))
        self.assertTrue(any("memstale" in m and "memory_dir" in m for m in warns))
        self.assertFalse(any("good:" in m for m in fails + warns))

    def test_a_repeated_class_folds_to_ONE_line_with_a_count(self):
        """MEASURED 2026-07-31: `helm doctor` printed 208 warnings, ~193 of them
        the SAME finding — home dir missing, once per registry project. Buried at
        line 209 of 229 was the pre-push leak guard, built and never installed,
        leaving 131 pushes unscanned. A correct detector fired correctly for two
        days into noise nobody could read.

        A report nobody can read is a report that does not exist, so this is a
        correctness property, not cosmetics."""
        home.scaffold_global()
        repo = os.path.join(self.tmp.name, "repos", "good")
        os.makedirs(repo)
        rec = lambda name: {"name": name, "path": repo, "kind": "git",
                            "status": "active", "sessions": {}, "memory_dir": None}
        names = ["nohome-%02d" % i for i in range(20)]      # none scaffolded
        pk.write_json(home.registry_path(), {"version": 1,
                                             "projects": {n: rec(n) for n in names}})
        warns = levels(doctor.check_projects(), doctor.WARN)
        homeless = [m for m in warns if "no home dir" in m]
        self.assertEqual(len(homeless), 1,
                         "a repeated class emitted %d lines, not 1" % len(homeless))
        self.assertIn("20 projects", homeless[0])
        self.assertIn("+14 more", homeless[0], "the tail was not truncated")

    def test_FAIL_is_NEVER_folded(self):
        """THE CARVE-OUT, and the control that stops the fix becoming a worse
        bug. A broken symlink is rare, individually actionable, and the line you
        most need named. Fold only where the REMEDY is shared — all 193 homeless
        projects share `helm sync`; a broken symlink shares nothing."""
        self.seed_home()
        fails = levels(doctor.check_projects(), doctor.FAIL)
        self.assertTrue(any("brokelink" in m and "broken symlink" in m
                            for m in fails),
                        "the individually-actionable FAIL was folded away")

    def test_a_folded_line_still_NAMES_the_projects(self):
        """A count alone is unactionable. The names are what an operator acts
        on; only the per-project path detail is dropped, because it is
        reconstructible from the name."""
        self.seed_home()
        warns = levels(doctor.check_projects(), doctor.WARN)
        self.assertTrue(any("gone-repo" in m for m in warns))
        self.assertTrue(any("memstale" in m for m in warns))
        self.assertFalse(any("good" in m for m in warns),
                         "a healthy project was named in a warning")

    def test_adoption_conflict_real_dir_warns(self):
        self.seed_home()
        results = doctor.check_adoption()
        self.assertTrue(any("adopted-proj" in m and "adoption conflict" in m
                            for m in levels(results, doctor.WARN)))

    def test_adopted_store_counts_and_dup_warn(self):
        d = self.seed_adopted()
        results = doctor.check_adopted_store(adopted_dir=d)
        ok = levels(results, doctor.OK)[0]
        self.assertIn("prior=2", ok)
        self.assertIn("prem=1", ok)
        self.assertIn("lex=1", ok)
        self.assertIn("heuristic=1", ok)
        self.assertIn("other=1", ok)
        warn = levels(results, doctor.WARN)[0]
        self.assertIn("1 prem-/prior- same-slug duplicate", warn)
        self.assertIn("drain --sweep-dups", warn)
        self.assertIn("owner-gated", warn)

    def test_adopted_store_missing_warns(self):
        results = doctor.check_adopted_store(
            adopted_dir=os.path.join(self.tmp.name, "nope"))
        self.assertTrue(any("adopted store missing" in m for m in levels(results, doctor.WARN)))

    def test_lexicon_dead_vocabulary_flags_spaced_kind_only(self):
        # a space-separated multi-word kind with no keywords is DEAD symptom
        # vocabulary (the comma gate cannot rescue it); a comma CSV kind is
        # rescued by the legacy fallback and a clean entry is silent
        d = os.path.join(home.global_dir(), "lexicon")
        os.makedirs(d)
        lex = lambda term, kind: (
            "---\nname: lex-%s\ndescription: \"lexicon: %s = def\"\n"
            "metadata:\n  node_type: memory\n  type: lexicon\n"
            "  term: %s\n  scope: global\n  kind: %s\n"
            "  definition: def of %s\n---\n" % (term, term, term, kind, term))
        for term, kind in (("cli-proxy", "cli-proxy proxy codex kimi seat"),
                           ("rescued", "snowflake, avalanche"),
                           ("clean", "phrase")):
            pk.atomic_write(os.path.join(d, "lex-%s.md" % term), lex(term, kind))
        empty = os.path.join(self.tmp.name, "adopted-empty")
        os.makedirs(empty)
        with mock.patch.dict(os.environ, {"HELM_ADOPTED_DIR": empty}):
            res = doctor.check_lexicon_dead_vocabulary()
        self.assertEqual([lvl for lvl, _ in res], [doctor.WARN])
        self.assertIn("lexicon 'cli-proxy'", res[0][1])
        self.assertIn("helm store add lexicon \"cli-proxy | def of cli-proxy | "
                      "phrase | cli-proxy, proxy, codex, kimi, seat\"", res[0][1])

    def test_know_your_user_empty_warns_then_ok(self):
        results = doctor.check_know_your_user()
        self.assertTrue(any("know-your-user leg is empty" in m and "helm interview" in m
                            for m in levels(results, doctor.WARN)))
        whoami.add_note("short replies", topic="voice")
        results = doctor.check_know_your_user()
        self.assertTrue(any("1 active note" in m for m in levels(results, doctor.OK)))

    def test_cv_present_and_missing(self):
        d = os.path.join(self.tmp.name, "cv")
        os.makedirs(d)
        self.assertEqual(doctor.check_cv(cv_dir=d)[0][0], doctor.OK)
        gone = doctor.check_cv(cv_dir=os.path.join(self.tmp.name, "no-cv"))
        self.assertEqual(gone[0][0], doctor.WARN)
        self.assertIn("recall plane offline", gone[0][1])

    def test_env_overrides_reported(self):
        msgs = levels(doctor.check_env(), doctor.OK)
        self.assertTrue(any("HELM_HOME=" + self.helm_home in m for m in msgs))

    def test_chat_sign_failure_is_a_loud_operator_warning(self):
        st = {"mode": "degraded", "profile": "seat-a",
              "reason": "send failed", "first_failure": "first",
              "last_failure": "last", "last_age_s": 9,
              "failure_count": 2,
              "remediation": "repair balance, then retry"}
        from helm import cell
        with mock.patch.object(chat, "node_url", return_value="http://node"), \
             mock.patch.object(chat, "node_head", return_value={"chain_index": 7}), \
             mock.patch.object(chat, "transport_status", return_value=st), \
             mock.patch.object(chat, "cells_path",
                               return_value=os.path.join(self.tmp.name, "none")), \
             mock.patch.object(cell, "bin_status", return_value={
                 "configured": True, "usable": True, "state": "ready",
                 "reason": "signer ready"}):
            results = doctor.check_chat_node()
        warning = "\n".join(levels(results, doctor.WARN))
        self.assertIn("DEGRADED profile 'seat-a': send failed", warning)
        self.assertIn("first first, last last (9s ago)", warning)
        self.assertIn("remediation: repair balance, then retry", warning)
        # The persistent incident must not disappear behind a CURRENT node-down
        # early return; doctor reports both facts until signed recovery clears it.
        with mock.patch.object(chat, "node_url", return_value="http://node"), \
             mock.patch.object(chat, "node_head", return_value=None), \
             mock.patch.object(chat, "transport_status", return_value=st):
            down = "\n".join(levels(doctor.check_chat_node(), doctor.WARN))
        self.assertIn("DEGRADED profile 'seat-a'", down)
        self.assertIn("UNREACHABLE", down)
        with mock.patch.object(chat, "node_url", return_value=None), \
             mock.patch.object(chat, "transport_status", return_value=st):
            disabled = doctor.check_chat_node()
        self.assertTrue(any(lvl == doctor.WARN and "DEGRADED" in msg
                            for lvl, msg in disabled))
        self.assertTrue(any(lvl == doctor.OK and "disabled" in msg
                            for lvl, msg in disabled))

    def test_an_UNDECLARED_unaudited_signer_FAILS_and_a_DECLARED_one_WARNS(self):
        """THE DIFFERENCE BETWEEN A DECISION AND AN ACCIDENT.

        dregg refuses to serve unverified unless an operator opens the hatch,
        and helm's contract is to MIRROR that declaration and never mint one.
        So the same measured condition means opposite things: DECLARED is a
        posture somebody chose and wrote a reversal for, UNDECLARED is a signer
        nobody chose. A diagnostic that shouts at both stops being read, which
        is the failure mode this split exists to avoid.
        """
        from helm import cell
        ok_signer = {"configured": True, "usable": True, "state": "ready",
                     "reason": "signer ready"}
        bad_cores = {"state": "degraded", "sign": "ExportAbsent",
                     "verify": "ExportAbsent",
                     "reason": "linked NO Lean-verified core, so dregg-pq "
                               "answers with its unaudited fallback"}
        def run(cores, declared):
            with mock.patch.object(chat, "node_url", return_value="http://node"), \
                 mock.patch.object(chat, "node_head",
                                   return_value={"chain_index": 1}), \
                 mock.patch.object(chat, "transport_status",
                                   return_value={"mode": "off"}), \
                 mock.patch.object(chat, "cells_path",
                                   return_value=os.path.join(self.tmp.name, "none")), \
                 mock.patch.object(cell, "bin_status", return_value=ok_signer), \
                 mock.patch.object(cell, "bin_path", return_value="/bin/true"), \
                 mock.patch.object(cell, "signer_reach",
                                   return_value={"naming": 3, "seats": 7,
                                                 "opaque": 0, "unreadable": 0,
                                                 "err": None}), \
                 mock.patch.object(cell, "unaudited_declared",
                                   return_value=declared), \
                 mock.patch.object(cell, "verified_cores", return_value=cores):
                return doctor.check_chat_node()

        # POSITIVE CONTROL, UNCONDITIONAL AND FIRST: verified cores add NO row
        # at all, so every level below is a statement about the condition and
        # not about a check that always speaks.
        clean = run({"state": "ready", "sign": "Installed",
                     "verify": "Installed", "reason": "both installed"},
                    (True, "/x/signer.env"))
        self.assertEqual([], [m for _l, m in clean if "unaudited PQ" in m])

        declared_rows = run(bad_cores, (True, "/x/signer.env"))
        hit = [(l, m) for l, m in declared_rows if "unaudited PQ" in m]
        self.assertEqual(1, len(hit), "the fleet fact must render exactly once")
        self.assertEqual(doctor.WARN, hit[0][0],
                         "a DECLARED posture is not an alarm")
        self.assertIn("DECLARED in /x/signer.env", hit[0][1])
        self.assertIn("3 of 7 live seats", hit[0][1])
        # THE SOURCE IS NAMED, ITS CONTENTS ARE NOT CLAIMED. This check
        # establishes THAT a declaration exists and WHERE; the row must not
        # tell a reader what the file says, which it has not read and which a
        # bare env var does not have at all.
        self.assertIn("read it for scope and reversal", hit[0][1])
        # MTIME IS SAID AS MTIME. A copy, a restore or a touch moves it
        # without rebuilding anything, so "built" would attribute an origin
        # this row never established.
        self.assertNotIn("built ", hit[0][1])
        self.assertIn("file mtime", hit[0][1])
        self.assertIn("helm's pointer", hit[0][1])
        self.assertNotIn("which also states", hit[0][1])

        undeclared = run(bad_cores, (False, None))
        hit = [(l, m) for l, m in undeclared if "unaudited PQ" in m]
        self.assertEqual(doctor.FAIL, hit[0][0],
                         "a signer nobody declared is the case that alarms")
        self.assertIn("NOTHING DECLARES", hit[0][1])

        # UNREADABLE IS NEITHER. Answering FAIL on a failed read would promote
        # a declared posture to an alarm on the strength of a broken probe.
        murky = run(bad_cores, (None, None))
        hit = [(l, m) for l, m in murky if "unaudited PQ" in m]
        self.assertEqual(doctor.WARN, hit[0][0])
        self.assertIn("could not be read", hit[0][1])

        unknown = run({"state": "unknown", "sign": None, "verify": None,
                       "reason": "no verified-core line"}, (True, "/x"))
        self.assertTrue(any(l == doctor.WARN and "verification UNKNOWN" in m
                            for l, m in unknown))

    def _cells(self, **balances):
        """A cells file plus a node that answers each cell's balance.

        A balance of None stands for a node that did not answer — the case the
        old code rendered at OK level reading "balance ?"."""
        from helm import cell
        path = os.path.join(self.tmp.name, "cells.json")
        pk.write_json(path, {name: "addr-" + name for name in balances})
        def get_json(url, timeout=None):
            name = url.rsplit("addr-", 1)[-1]
            bal = balances.get(name)
            return None if bal is None else {"balance": bal}
        return path, mock.patch.object(cell, "get_json", side_effect=get_json)

    def _chat_node(self, cells_path):
        """A FEE-CHARGING node (a declared coordination fee), where a low cell
        is a finding; where chat turns are fee-free a zero balance is healthy
        (test_chat_faucet pins that pole)."""
        from helm import cell
        return [
            mock.patch.object(cell, "coord_fee", return_value=1000),
            mock.patch.object(chat, "node_url", return_value="http://node"),
            mock.patch.object(chat, "node_head", return_value={"chain_index": 1}),
            mock.patch.object(chat, "transport_status",
                              return_value={"mode": "off"}),
            mock.patch.object(chat, "cells_path", return_value=cells_path),
            mock.patch.object(cell, "bin_status", return_value={
                "configured": True, "usable": True, "state": "ready",
                "reason": "signer ready"}),
        ]

    def test_an_UNREADABLE_cell_balance_is_a_WARNING_not_an_OK(self):
        """A FAILED LOOK AND A FUNDED CELL SHARED A LEVEL. The node not
        answering returned {}, so `balance` was None, so the isinstance test
        was False and the row printed at OK reading "balance ?" — the only
        tell a question mark in prose nobody greps for. Whether that cell can
        post is UNKNOWN, which is not the same as fine."""
        path, cells = self._cells(alive=5000, silent=None)
        with contextlib.ExitStack() as st:
            for m in self._chat_node(path) + [cells]:
                st.enter_context(m)
            results = doctor.check_chat_node()
        warns = "\n".join(levels(results, doctor.WARN))
        self.assertIn("UNREADABLE", warns)
        self.assertIn("silent", warns)
        self.assertIn("UNKNOWN, not fine", warns)
        self.assertNotIn("alive", warns,
                         "a funded cell must not be swept into the warning")
        oks = "\n".join(levels(results, doctor.OK))
        self.assertIn("chat cells: 1 funded of 2", oks)

    def test_MANY_low_cells_fold_into_ONE_row_naming_them(self):
        """MEASURED 2026-08-25: 16 of this report's 30 WARN lines were the
        identical low-balance row, whose own text says the auto-faucet fixes it
        on the next post. More than half the warnings on a surface agents read
        to find the ones that are NOT self-healing — so the operator learns to
        skim exactly the section carrying real findings."""
        path, cells = self._cells(**{("cell%d" % i): 0 for i in range(9)})
        with contextlib.ExitStack() as st:
            for m in self._chat_node(path) + [cells]:
                st.enter_context(m)
            results = doctor.check_chat_node()
        low = [m for l, m in results if l == doctor.WARN and "balance low" in m]
        self.assertEqual(len(low), 1, "nine low cells must be ONE row: %r" % low)
        self.assertIn("9 cells", low[0])
        # A TRUNCATED NAME IS DEFERRED, NEVER LOST — and this arm used to
        # demand all nine, which was the RIGHT bar for the design it was
        # written against and the WRONG one for the design that replaced it.
        # The concern was real: a STABLE sort means a silently dropped cell is
        # dropped on every run, permanently invisible, while the row tells the
        # reader to act on names that persist. The cure was not an unbounded
        # join (one population would bury every other finding); it was a cap
        # that carries an EXECUTABLE address for the remainder.
        #
        # So the contract is now: the first _FOLD_SHOW names inline, an honest
        # remainder count, and a drilldown that reaches what was omitted.
        shown = [i for i in range(9) if "cell%d" % i in low[0]]
        self.assertEqual(len(shown), doctor._FOLD_SHOW,
                         "exactly the cap is named inline: %r" % low[0])
        self.assertIn("+%d more" % (9 - doctor._FOLD_SHOW), low[0],
                      "the remainder is COUNTED, not hidden: %r" % low[0])
        # THE LOAD-BEARING HALF: a cap without a way to reach the rest is the
        # dishonest third form the renderer's own docstring names. Truncation
        # is only acceptable BECAUSE this address is here.
        self.assertIn(doctor._CELL_DRILLDOWN, low[0],
                      "a truncating row must name an executable surface that "
                      "renders what it omitted: %r" % low[0])

    def test_a_MALFORMED_registry_is_UNKNOWN_never_a_clean_zero(self):  # noqa: VACUOUS_ASSERTION — the flagged root is the LOOP-SCOPED `msgs`, whose positive (assertIn needle) and absence (assertNotIn funded-of-0) are necessarily in the same loop, so no unconditional control can exist on it by construction; the unconditional control at the top runs the same code path outside the loop and pins both directions on a well-formed registry
        """An empty LIST reported "0 funded of 0" — a dead registry rendering
        as a healthy one, which is the exact conflation a doctor exists to
        catch. The old test refused only None, so `[]` fell through `or {}`,
        and any TRUTHY non-mapping reached the iteration and crashed the whole
        check — one malformed file taking out every other finding in the
        report. Absence and all-clear must never share an observable.
        """
        needle = "cells registry UNREADABLE"

        # UNCONDITIONAL POSITIVE CONTROL, FIRST, on the same observable the
        # loop and the must-miss both read. Without it every assertion below
        # sits behind a loop or is an absence, so an empty payload list or a
        # check that returned nothing would satisfy the whole arm silently.
        path = os.path.join(self.tmp.name, "cells.json")
        with open(path, "w") as fh:
            json.dump({"cellA": "addr1"}, fh)
        with contextlib.ExitStack() as st:
            for m in self._chat_node(path):
                st.enter_context(m)
            control = " | ".join(m for _, m in doctor.check_chat_node())
        self.assertIn("cellA", control)          # the registry WAS iterated
        self.assertNotIn(needle, control)        # and a good one does not trip

        payloads = ([], "oops", [1], {"ok": 1})
        self.assertEqual(len(payloads), 4, "all four measured shapes must run")
        for payload in payloads:
            path = os.path.join(self.tmp.name, "cells.json")
            with open(path, "w") as fh:
                json.dump(payload, fh)
            with contextlib.ExitStack() as st:
                for m in self._chat_node(path):
                    st.enter_context(m)
                results = doctor.check_chat_node()
            msgs = " | ".join(m for _, m in results)
            self.assertIn(needle, msgs,
                          "%r must read UNKNOWN, not fine: %r" % (payload, msgs))
            self.assertNotIn("funded of 0", msgs,
                             "%r must not claim a clean zero: %r" % (payload, msgs))

        # (The well-formed MUST-MISS is the control at the top: a guard that
        # refuses everything would hide every real reading, and the needle is
        # the registry sentence specifically — "UNREADABLE" alone also appears
        # in the per-cell blind row, which made all five cases look identical
        # when this arm was first probed by hand.)

    def test_a_TRUNCATED_summary_carries_an_EXECUTABLE_way_to_reach_the_rest(self):
        """A TRUNCATING SUMMARY HAS EXACTLY TWO HONEST FORMS: render every
        identity, or name an EXECUTABLE surface that renders them. Nothing else
        is honest — a stable sort makes any silent tail permanently invisible,
        the same rows hidden on every run, while an unbounded join lets one
        population bury every other finding in the report.

        Both wrong forms were tried on this row before this one. The drill-down
        is an EXISTING verb, not a second implementation: `helm chat node
        status` already enumerates every cell and its balance."""
        n = 60
        path, cells = self._cells(**{("c%03d" % i): 0 for i in range(n)})
        with contextlib.ExitStack() as st:
            for m in self._chat_node(path) + [cells]:
                st.enter_context(m)
            results = doctor.check_chat_node()
        low = [m for l, m in results if l == doctor.WARN and "balance low" in m]
        self.assertEqual(len(low), 1, "still ONE row: %r" % low)
        self.assertIn("+%d more" % (n - doctor._FOLD_SHOW), low[0])
        self.assertIn("helm chat node status", low[0],
                      "a truncated row MUST name the surface that lists the "
                      "rest: %r" % low[0])
        self.assertNotIn("c059", low[0], "the tail is omitted — that is why "
                                         "the pointer has to be there")

    def test_a_SMALL_population_is_rendered_whole_with_no_pointer(self):
        """The other honest form. Nothing was omitted, so nothing may claim a
        remainder or send the reader somewhere else."""
        path, cells = self._cells(**{("c%d" % i): 0 for i in range(3)})
        with contextlib.ExitStack() as st:
            for m in self._chat_node(path) + [cells]:
                st.enter_context(m)
            results = doctor.check_chat_node()
        low = [m for l, m in results if l == doctor.WARN and "balance low" in m]
        # UNCONDITIONAL FIRST: the loop below is the interesting assertion but a
        # loop cannot be a positive control — an empty range would satisfy it
        # silently, which is the vacuity this file keeps finding elsewhere.
        self.assertEqual(len(low), 1, "exactly one row: %r" % low)
        self.assertIn("3 cells", low[0])
        for i in range(3):
            self.assertIn("c%d" % i, low[0])
        self.assertNotIn("more", low[0])
        self.assertNotIn("helm chat node status", low[0])

    def test_an_UNREADABLE_cells_registry_is_UNKNOWN_not_an_empty_estate(self):
        """`read_json` answers {} for a MISSING file and for a MALFORMED one,
        and `or {}` folded a null in with them — so a corrupted .cells.json
        reported "0 funded of 0", a clean bill over a file nothing could read.
        The absence-versus-unreadable split this check already made per CELL
        was missing one level up, at the FILE."""
        bad = os.path.join(self.tmp.name, "broken-cells.json")
        with open(bad, "w") as f:
            f.write("{not json")
        with contextlib.ExitStack() as st:
            for m in self._chat_node(bad):
                st.enter_context(m)
            results = doctor.check_chat_node()
        self.assertTrue(any(l == doctor.WARN and "UNREADABLE" in m
                            for l, m in results), results)
        self.assertFalse([m for l, m in results
                          if l == doctor.OK and "chat cells" in m],
                         "an unreadable registry may not report a funded count")
        # THE OTHER POLE, unconditional: a genuinely ABSENT registry is not the
        # same fact and must not borrow the same sentence.
        gone = os.path.join(self.tmp.name, "no-such-cells.json")
        with contextlib.ExitStack() as st:
            for m in self._chat_node(gone):
                st.enter_context(m)
            absent = doctor.check_chat_node()
        self.assertFalse([m for l, m in absent if "UNREADABLE" in m],
                         "a missing file is absence, not unreadability")
        self.assertTrue(any(l == doctor.OK and "chat cells: 0 funded of 0" in m
                            for l, m in absent), absent)

    def test_a_PROFILE_NAME_cannot_move_the_operator_cursor(self):
        """NAMES IN THIS REPORT ARE NOT HELM'S TEXT. A chat profile comes from
        HELM_CELL_PROFILE and is persisted verbatim, so it reaches a terminal
        through this row — control characters, bidi overrides and unbounded
        length are an operator-output problem before a cosmetic one."""
        self.assertNotIn("\x1b", doctor._safe("bad\x1b[2Jname"))
        self.assertNotIn("\u202e", doctor._safe("bad\u202ename"))
        self.assertEqual(len(doctor._safe("x" * 500)), 64)
        self.assertEqual(doctor._safe("ordinary-seat"), "ordinary-seat")

    def test_EVERY_folded_caller_supplies_a_drilldown(self):  # noqa: VACUOUS_ASSERTION — a source-contract walk; its unconditional positive is assertGreaterEqual(sites, 3), which fails rather than reporting a clean sweep if the enumeration finds nothing
        """The invariant is enforced by the SIGNATURE, not by remembering. A
        caller cannot pick the dishonest third option by omission, because
        `drilldown` is required — and a walk of the call sites keeps that true
        if the parameter ever gains a default."""
        import inspect
        src = inspect.getsource(doctor)
        sig = inspect.signature(doctor._folded)
        self.assertIs(sig.parameters["drilldown"].default,
                      inspect.Parameter.empty,
                      "a default would let a caller omit the drill-down")
        sites = src.count("_folded(WARN")
        self.assertGreaterEqual(sites, 3, "MUST-HIT: expected the known "
                                          "_folded call sites, found %d" % sites)

    def test_ready_unproven_signing_is_explicit(self):
        st = {"mode": "ready", "state": "READY",
              "label": "ready (unproven)",
              "detail": "no committed signing receipt observed for this profile"}
        from helm import cell
        with mock.patch.object(chat, "node_url", return_value="http://node"), \
             mock.patch.object(chat, "node_head", return_value={"chain_index": 7}), \
             mock.patch.object(chat, "transport_status", return_value=st), \
             mock.patch.object(chat, "cells_path",
                               return_value=os.path.join(self.tmp.name, "none")), \
             mock.patch.object(cell, "bin_status", return_value={
                 "configured": True, "usable": True, "state": "ready",
                 "reason": "signer ready"}):
            warning = "\n".join(levels(doctor.check_chat_node(), doctor.WARN))
        self.assertIn("ready (unproven)", warning)
        self.assertIn("no committed signing receipt", warning)

    def test_configured_missing_signer_is_unavailable_not_unset(self):
        missing = os.path.join(self.tmp.name, "deleted-signer")
        with mock.patch.dict(os.environ, {
                "HELM_CELL_BIN": missing,
                "HELM_CHAT_NODE_URL": "http://node"}), \
             mock.patch.object(chat, "node_head",
                               return_value={"chain_index": 7}), \
             mock.patch.object(chat, "cells_path",
                               return_value=os.path.join(self.tmp.name, "none")):
            results = doctor.check_chat_node()
        warning = "\n".join(levels(results, doctor.WARN))
        self.assertIn("DEGRADED profile", warning)
        self.assertIn("signer path does not exist", warning)
        self.assertNotIn("HELM_CELL_BIN unset", warning)
        self.assertNotIn(missing, warning)


class DocTaskConditionalCorpusTest(DoctorBase):
    """THE RUNG IS MEASURED AGAINST REAL MARKDOWN SHAPES, not against prose
    written to suit it.

    The unit arms beside this class all passed while a planted 30-document
    corpus misclassified 18 of them, and they could not have caught it: each
    one feeds the classifier a bare sentence, which is the one input shape
    this tree's documents never take. What actually sits in docs/ and
    agents/ is fenced code, tables, hard-wrapped paragraphs, bullet lists and
    headings -- and a sentence splitter reads every one of those as grammar.

    So each case here is a DOCUMENT, its expected findings are pinned as
    (line, task) pairs, and the whole corpus runs in ONE pass that reports
    every mismatch together. A per-case assert would stop at the first and
    hide the shape of the rest, which is how a classifier gets cured one
    symptom at a time."""

    OPEN, CLOSED = "7001", "7002"
    CONTROL = "4242"
    CONTROL_DOC = ("the control door is task/4242\n"
                   "and it works once it lands; until then do it by hand.\n")
    STATUSES = {"7001": "open", "7002": "closed", "4242": "closed"}

    #: (name, part, lines, expected) -- expected is the set of (line, task)
    #: findings the rung owes on that document, and EMPTY means it must be
    #: silent about everything except the control.
    CORPUS = (
        # ---- structure that is not prose: a promise nobody made -----------
        ("fenced code carries no promise", "agents", [
            "The runner is documented below.",              # 1
            "```sh",                                        # 2
            "# until task/7002 lands, run this by hand",    # 3
            "helm foo",                                     # 4
            "```",                                          # 5
            "Nothing here is waiting on anything.",         # 6
        ], ()),
        ("a tilde fence is a fence too", "agents", [
            "Sample:",                                      # 1
            "~~~",                                          # 2
            "until task/7002 lands, do it by hand",         # 3
            "~~~",                                          # 4
        ], ()),
        ("a table row is cells, not a sentence", "agents", [
            "| verb | note |",                                          # 1
            "|------|------|",                                          # 2
            "| foo | door is task/7002 and until it lands, by hand |",   # 3
        ], ()),
        # ---- reach: adjacency is a referent only inside one block --------
        ("a paragraph break ends the reach", "agents", [
            "The door is task/7002.",                       # 1
            "",                                             # 2
            "Until then, do something else by hand.",       # 3
        ], ()),
        ("the next BULLET is a different thought", "agents", [
            "- The door is task/7002.",                     # 1
            "- Until then, do it by hand.",                 # 2
        ], ()),
        ("a fence between them breaks the reach", "agents", [
            "The door is task/7002.",                       # 1
            "```sh",                                        # 2
            "helm foo",                                     # 3
            "```",                                          # 4
            "Until then, do it by hand.",                   # 5
        ], ()),
        ("a heading between them breaks the reach", "agents", [
            "The door is task/7002.",                       # 1
            "## Heading",                                   # 2
            "Until then, do it by hand.",                   # 3
        ], ()),
        # ---- the real construction, which must still be caught -----------
        ("a WRAPPED sentence keeps its citation", "agents", [
            "The door is task/7002",                                # 1
            "and it works once it lands; until then do by hand.",   # 2
        ], ((2, "7002"),)),
        ("the anaphor reaches the previous SENTENCE", "agents", [
            "The door is task/7002.",                       # 1
            "Until then, do it by hand.",                   # 2
        ], ((2, "7002"),)),
        ("one bullet holding both halves still counts", "agents", [
            "- The door is task/7002 and until it lands, by hand.",  # 1
        ], ((1, "7002"),)),
        # ---- provenance, and the boundary that scopes it -----------------
        ("provenance does not license the NEXT sentence", "agents", [
            "This was filed against task/7002.",            # 1
            "Until then, do the other thing by hand.",      # 2
        ], ()),
        ("a pointer INSIDE the promise is still a promise", "agents", [
            "See task/7002 once it lands.",                 # 1
        ], ((1, "7002"),)),
        # ---- the row is open, so the sentence is true --------------------
        ("an OPEN row is not rot", "agents", [
            "The door is task/7001.",                       # 1
            "Until then, do it by hand.",                   # 2
        ], ()),
        # ---- repetition: the location must be THIS copy ------------------
        ("a REPEATED sentence reports each copy", "agents", [
            "The door is task/7002.",                       # 1
            "Until then, do it by hand.",                   # 2
            "",                                             # 3
            "An unrelated paragraph sits between them.",    # 4
            "",                                             # 5
            "The door is task/7002.",                       # 6
            "Until then, do it by hand.",                   # 7
        ], ((2, "7002"), (7, "7002"))),
        # ---- every remaining BLOCK START, enumerated rather than met one
        # ---- round at a time ---------------------------------------------
        ("indented code is code", "agents", [
            "Example:",                                     # 1
            "",                                             # 2
            "    until task/7002 lands, do it by hand",     # 3
            "",                                             # 4
        ], ()),
        ("a fence may be longer than three", "agents", [
            "Sample:",                                      # 1
            "````",                                         # 2
            "until task/7002 lands, by hand",               # 3
            "````",                                         # 4
        ], ()),
        ("a fence may carry an info string", "agents", [
            "Sample:",                                      # 1
            "```sh title=x",                                # 2
            "until task/7002 lands",                        # 3
            "```",                                          # 4
        ], ()),
        ("a SETEXT underline ends its paragraph", "agents", [
            "The door is task/7002.",                       # 1
            "Heading",                                      # 2
            "=======",                                      # 3
            "Until then, do it by hand.",                   # 4
        ], ()),
        ("a table needs no leading pipe", "agents", [
            "verb | note",                                          # 1
            "-----|-----",                                          # 2
            "foo | door is task/7002 and until it lands, by hand",  # 3
        ], ()),
        ("a blockquote is its own block", "agents", [
            "The door is task/7002.",                       # 1
            "> quoted material",                            # 2
            "Until then, do it by hand.",                   # 3
        ], ()),
        ("a thematic break is a boundary", "agents", [
            "The door is task/7002.",                       # 1
            "---",                                          # 2
            "Until then, do it by hand.",                   # 3
        ], ()),
        # ---- citations survive the punctuation docs actually use ----------
        ("backticked provenance is still provenance", "agents", [
            "This was filed against `task/7002`.",          # 1
            "Until then, do the other thing by hand.",      # 2
        ], ()),
        ("a backticked pointer is still a pointer", "agents", [
            "See `task/7002`.",                             # 1
            "Until then, do the other thing by hand.",      # 2
        ], ()),
        ("a backticked citation is still caught", "agents", [
            "The door is `task/7002`.",                     # 1
            "Until then, do it by hand.",                   # 2
        ], ((2, "7002"),)),
        ("a backticked row INSIDE the clause", "agents", [
            "Do it by hand once `task/7002` lands.",        # 1
        ], ((1, "7002"),)),
        # ---- which row the promise is about -------------------------------
        ("two rows and an 'it' names NOBODY", "agents", [
            "task/7001 supersedes task/7002, and once it lands the shim goes.",
        ], ()),
        ("a clause that NAMES its row settles it", "agents", [
            "task/7001 supersedes task/7002; until task/7002 lands, by hand.",
        ], ((1, "7002"),)),
        # ---- phrasings measured in this tree ------------------------------
        ("'For now' points back too", "agents", [
            "The door is task/7002.",                       # 1
            "For now, do it by hand.",                      # 2
        ], ((2, "7002"),)),
        ("'Meanwhile' points back too", "agents", [
            "The door is task/7002.",                       # 1
            "Meanwhile, do it by hand.",                    # 2
        ], ((2, "7002"),)),
        # ---- the other scan root is really walked ------------------------
        ("docs/ is traversed, not only agents/", "docs", [
            "The door is task/7002.",                       # 1
            "Until then, do it by hand.",                   # 2
        ], ((2, "7002"),)),
    )

    def _scan(self, part, text, name="SKILL.md"):
        root = tempfile.mkdtemp(prefix="helm-test-doccorpus-")
        self.addCleanup(shutil.rmtree, root, True)
        agents = os.path.join(root, "agents", "claudecode", "skills", "x")
        docs = os.path.join(root, "docs")
        os.makedirs(agents)
        os.makedirs(docs)
        with open(os.path.join(docs if part == "docs" else agents, name),
                  "w") as fh:
            fh.write(text)
        # THE CONTROL RIDES EVERY CASE, because most of this corpus asserts
        # SILENCE and a walker that never arrived is silent too. Its warning
        # is the receipt that this tree was read and this classifier fired.
        with open(os.path.join(agents, "CONTROL.md"), "w") as fh:
            fh.write(self.CONTROL_DOC)
        return doctor.check_doc_task_conditionals(
            root=root, status_of=lambda tid: self.STATUSES.get(tid))

    @staticmethod
    def _findings(rows):
        """{(line, task)} for the subject, with the control filtered out."""
        got = set()
        for lvl, msg in rows:
            if lvl != doctor.WARN:
                continue
            where = re.search(r":(\d+) ", msg)
            tid = re.search(r"about task/(\d+)", msg) \
                or re.search(r"cites task/(\d+)", msg)
            if not where or not tid:
                continue
            if tid.group(1) == DocTaskConditionalCorpusTest.CONTROL:
                continue
            got.add((int(where.group(1)), tid.group(1)))
        return got

    @staticmethod
    def _disagreement(name, got, expected):
        """The comparison itself, as ONE function -- or None when they agree.

        THE LOOP AND ITS OWN CONTROL BOTH CALL THIS, and that is the entire
        reason it exists. With the comparison written inline, disabling the
        branch that fills the disagreement list left the list empty and every
        assertion below still passed: a corpus of thirty documents graded by a
        comparator that cannot fail. Measured -- `if got != set(expected)`
        replaced by `if False` survived. Now the same function decides both,
        so a mutation that stops it reporting kills the control too."""
        if got == set(expected):
            return None
        return "%s\n    expected %s\n    got      %s" % (
            name, sorted(set(expected)), sorted(got))

    def test_every_document_shape_in_the_corpus_is_classified(self):
        """One pass over every shape, reporting ALL disagreements together."""
        # THE SENTINEL RIDES THE LOOP, and it is the only way to prove the
        # accumulator works on a green run. With every case agreeing, the
        # append is NEVER EXECUTED -- so `wrong` stays empty whether the
        # recording step works or has been deleted, and an empty list proves
        # nothing about a corpus of thirty documents. Measured: replacing the
        # append's condition with `if False` survived every other assertion
        # here. This case's expectation is deliberately FALSE (the promise is
        # on line 2, not line 1), so a working loop MUST record exactly it,
        # and the assertion below is an equality rather than an emptiness.
        sentinel = ("CONTROL: a deliberately false expectation", "agents",
                    ["The door is task/7002.",
                     "Until then, do it by hand."], ((1, "7002"),))
        wrong, compared = [], 0
        for name, part, lines, expected in tuple(self.CORPUS) + (sentinel,):
            rows = self._scan(part, "\n".join(lines) + "\n")
            self.assertTrue(
                any(lvl == doctor.WARN and "task/" + self.CONTROL in msg
                    for lvl, msg in rows),
                "THE CONTROL DID NOT WARN for %r, so this corpus was never "
                "read and every silence in it means nothing" % name)
            got = self._findings(rows)
            compared += 1
            disagreement = self._disagreement(name, got, expected)
            if disagreement:
                wrong.append(disagreement)
        # AN UNCONDITIONAL POSITIVE CONTROL ON `wrong` ITSELF. Everything
        # above compares two sets and appends on a difference, so an empty
        # `wrong` is equally consistent with three worlds: the corpus ran and
        # agreed, the corpus never ran, or the comparison cannot fail at all.
        # This drives the SAME reader and the SAME comparison over a document
        # whose answer is known, and then requires a false expectation to
        # disagree with it.
        truth = self._findings(self._scan(
            "agents", "The door is task/7002.\nUntil then, do by hand.\n"))
        self.assertEqual({(2, "7002")}, truth,
                         "the corpus comparator cannot read a finding it is "
                         "about to judge this corpus by")
        # AND THE COMPARATOR MUST ACTUALLY REPORT ONE. This drives the SAME
        # function the loop drives, with an expectation known to be false, and
        # requires a disagreement to come back -- so a comparator that cannot
        # fail is caught here rather than passing thirty documents silently.
        self.assertIsNone(self._disagreement("control", truth, {(2, "7002")}),
                          "the comparator disagreed with a TRUE expectation")
        self.assertIsNotNone(
            self._disagreement("control", truth, {(1, "7002")}),
            "the comparator accepted a FALSE expectation, so every agreement "
            "recorded above means nothing")
        # EVERY CASE WAS REACHED, which an empty or short-circuited loop is
        # not: a corpus nobody walked also produces zero disagreements.
        self.assertEqual(len(self.CORPUS) + 1, compared,
                         "the loop did not reach every document shape")
        self.assertEqual([sentinel[0]], [w.split("\n")[0] for w in wrong],
                         "%d document shape(s) misclassified (the CONTROL "
                         "entry is expected and proves the recording step "
                         "runs; anything else beside it is a real "
                         "disagreement):\n\n%s"
                         % (max(0, len(wrong) - 1), "\n".join(wrong)))


class DocTaskConditionalOriginalCorpusTest(unittest.TestCase):
    """THE REVIEWER'S CORPUS, RUN AS THE REVIEWER RAN IT (task/2612).

    An author's corpus holds the shapes the author can see; the reviewer's
    holds the ones that were missed. That corpus is a fixture here, with the
    source file's sha256 recorded, and this arm reproduces the reviewer's
    harness line for line: both roots, a CONTROL.txt beside every subject,
    2598 open and everything else closed, one expected id per WARN in order
    with duplicates retained. It compares against the reviewer's expectation
    column, never against what the code does today.

    A deliberately wrong sentinel rides the loop, so the arm proves the
    comparator can disagree: the disagreement list must equal exactly that
    entry, which fails on a recorder that stopped recording as surely as on a
    real miss."""

    FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures",
                           "doctor_task2612_original_corpus.json")
    #: PINNED HERE, NOT READ FROM THE FIXTURE — see the hostile corpus arm.
    SOURCE_SHA256 = ("b830945049ab7601abbd9e21eb94d1fd"
                     "bd4101e4b6644f35bcd9693c23732c3f")
    CASES_SHA256 = ("e9e60ac5a43d093f17b9629ea37eca83"
                    "fde1c8cd32fb24621af9cfe28f2fde65")

    def test_the_shipped_corpus_is_the_one_the_reviewer_ran(self):  # noqa: VACUOUS_ASSERTION — three equalities against digests PINNED IN THIS FILE; nothing here asserts an absence and a fixture edit reddens it in two places
        import hashlib
        import json
        with open(self.FIXTURE, encoding="utf-8") as fh:
            fx = json.load(fh)
        self.assertEqual(self.SOURCE_SHA256, fx["original_file_sha256"])
        self.assertEqual(
            self.CASES_SHA256,
            hashlib.sha256("\n".join(c[1] for c in fx["original_cases"])
                           .encode()).hexdigest(),
            "the shipped case texts are not the reviewer's")
        self.assertEqual(self.CASES_SHA256, fx["cases_joined_sha256"])

    def test_every_original_case_answers_as_the_reviewer_expected(self):
        import json
        import tempfile
        with open(self.FIXTURE, encoding="utf-8") as fh:
            fx = json.load(fh)
        cases = [tuple(c) for c in fx["original_cases"]]
        self.assertEqual(len(cases), 30)
        sentinel = ("SENTINEL-must-disagree",
                    "Until task/2573 lands, exit by hand.", [])
        cases.append(sentinel)
        wrong, compared = [], 0
        for label, text, expect in cases:
            with tempfile.TemporaryDirectory(prefix="doc-review-") as d:
                for part, control in fx["roots"]:
                    os.mkdir(os.path.join(d, part))
                    with open(os.path.join(d, part, "subject.md"), "w",
                              encoding="utf-8") as fh:
                        fh.write(text)          # no trailing newline: as shipped
                    with open(os.path.join(d, part, "CONTROL.txt"), "w",
                              encoding="utf-8") as fh:
                        fh.write(fx["control_text"].replace("{id}", control))
                got = doctor.check_doc_task_conditionals(
                    root=d,
                    status_of=lambda tid: "open" if tid == "2598" else "closed")
            warnings = [m for lvl, m in got if lvl == doctor.WARN]
            subjects = [m for m in warnings if "subject.md:" in m]
            for part, control in fx["roots"]:
                # EACH ROOT'S CONTROL IS COUNTED ON ITS OWN, and its SUBJECT
                # asserted: a total lets a deleted must-hit in one root be
                # paid for by a stray warning anywhere else.
                mine = [m for m in warnings
                        if m.startswith(part + "/CONTROL.txt:")]
                self.assertEqual(
                    [control], [_subject_of(m) for m in mine],
                    "%s: %s's control did not fire exactly once about "
                    "task/%s — this case measured nothing: %r"
                    % (label, part, control, got))
            for part, _control in fx["roots"]:
                actual = [_subject_of(m)
                          for m in subjects if m.startswith(part + "/")]
                compared += 1
                if actual != list(expect):
                    wrong.append("%s [%s]: expected %r got %r"
                                 % (label, part, list(expect), actual))
        self.assertEqual(compared, 62)
        self.assertEqual(
            ["%s [%s]" % (sentinel[0], part) for part, _c in fx["roots"]],
            [w.split(": expected")[0] for w in wrong],
            "the reviewer's corpus disagrees with this tree:\n  "
            + "\n  ".join(wrong))


class DocTaskConditionalClauseShapesTest(unittest.TestCase):
    """Shapes the reviewer's hostile replay named after the original corpus
    passed: a citation matched inside a longer token, only the first
    condition of a clause reported, and the body of a multi-line HTML block
    read as prose. Each arm carries its paired pole so the cure is measured
    against the shape it was written for and not against silence."""

    def _ids(self, text):
        import tempfile
        with tempfile.TemporaryDirectory(prefix="doc-shapes-") as d:
            for part in ("docs", "agents"):
                os.makedirs(os.path.join(d, part))
                with open(os.path.join(d, part, "s.md"), "w",
                          encoding="utf-8") as fh:
                    fh.write(text)
                with open(os.path.join(d, part, "CONTROL.txt"), "w",
                          encoding="utf-8") as fh:
                    fh.write("Until task/4242 lands, copy by hand.")
            got = doctor.check_doc_task_conditionals(
                root=d, status_of=lambda tid: "closed")
        subj = [m for lvl, m in got if lvl == doctor.WARN
                and m.startswith("docs/s.md:")]
        self.assertEqual(
            2, sum(1 for lvl, m in got if lvl == doctor.WARN
                   and "CONTROL.txt:" in m),
            "MUST-HIT: a control was missed, so this case measured nothing")
        return [(doctor._TASK_CITE.search(m).group(1),
                 m.split(" says ")[1].split(" about ")[0].strip("'"))
                for m in subj]

    def test_a_citation_is_a_whole_token(self):
        self.assertEqual([("2573", "is not yet available")],
                         self._ids("task/2573 is not yet available."))
        self.assertEqual([], self._ids("subtask/2573 is not yet available."))
        self.assertEqual([("25730", "is not yet available")],
                         self._ids("task/25730 is not yet available."),
                         "a longer number is a different row, not row 2573")

    def test_every_condition_in_a_clause_is_reported_and_a_named_one_settles_its_row_first(self):
        self.assertEqual(
            [("4244", "until task/4244 lands"), ("2573", "is not yet available")],
            self._ids("task/2573 is not yet available and until task/4244 "
                      "lands, wait."))
        self.assertEqual([("2573", "until task/2573 lands")],
                         self._ids("the door is task/2573 and until task/2573 "
                                   "lands, exit by hand."),
                         "one row named twice in one clause is one warning")

    def test_the_body_of_a_multi_line_html_comment_is_not_prose(self):
        prose = "task/2573 is not yet available."
        self.assertEqual([("2573", "is not yet available")], self._ids(prose),
                         "MUST-HIT: the same sentence as prose warns")
        self.assertEqual([], self._ids("<!-- task/2573 is\nnot yet available\n"
                                      "-->\nfine."))
        self.assertEqual([("2573", "is not yet available")],
                         self._ids("<!-- task/2573 is\nnot yet available\n-->\n"
                                   + prose),
                         "prose after the comment closes is prose again")

    def test_a_tag_block_runs_to_the_blank_line_and_no_further(self):
        self.assertEqual(
            [("2573", "is not yet available")],
            self._ids("<div>\ntask/2573 is not yet available\n</div>\n\n"
                      "task/2573 is not yet available."),
            "the block body is skipped; the paragraph after the blank line is "
            "prose and is the only warning")


class DocTaskConditionalTypedBlockTest(unittest.TestCase):
    """A raw-text HTML element and a provenance verb are both whole tokens.

    `<script>`, `<style>`, `<pre>` and `<textarea>` run to their CLOSING TAG
    across blank lines -- a blank line inside a script does not put its body
    back into prose -- while any other tag block ends at the blank line. And
    a provenance verb matched inside a longer word: "unfiled against task/N"
    is the opposite claim and "oversee task/N" is a different word, so a
    promise about that row was read as a pointer at it and silenced."""

    def _ids(self, text):
        import tempfile
        with tempfile.TemporaryDirectory(prefix="doc-typed-") as d:
            for part in ("docs", "agents"):
                os.makedirs(os.path.join(d, part))
                with open(os.path.join(d, part, "s.md"), "w",
                          encoding="utf-8") as fh:
                    fh.write(text)
                with open(os.path.join(d, part, "CONTROL.txt"), "w",
                          encoding="utf-8") as fh:
                    fh.write("Until task/4242 lands, copy by hand.")
            got = doctor.check_doc_task_conditionals(
                root=d, status_of=lambda tid: "closed")
        self.assertEqual(
            2, sum(1 for lvl, m in got if lvl == doctor.WARN
                   and "CONTROL.txt:" in m),
            "MUST-HIT: a control was missed, so this case measured nothing")
        return [doctor._TASK_CITE.search(m).group(1) for lvl, m in got
                if lvl == doctor.WARN and m.startswith("docs/s.md:")]

    PROSE = "task/2573 is not yet available."

    def test_a_raw_text_element_runs_to_its_closing_tag_across_blank_lines(self):
        self.assertEqual(["2573"], self._ids(self.PROSE),
                         "MUST-HIT: the same sentence as prose warns")
        self.assertEqual([], self._ids(
            "<script>\n" + self.PROSE + "\n\nstill script\n</script>\nfine."))
        self.assertEqual(["2573"], self._ids(
            "<style>\n.x{}\n</style>\n\n" + self.PROSE),
            "prose after the closing tag is prose again")

    def test_a_raw_text_element_closed_on_its_own_line_opens_no_block(self):
        self.assertEqual([], self._ids("<pre>" + self.PROSE + "</pre>\nfine."))
        self.assertEqual(["2573"], self._ids(
            "<pre>opaque</pre>\n" + self.PROSE),
            "the element closed on its opening line consumes nothing after it")

    def test_a_provenance_verb_is_a_whole_token(self):
        pointer = ("The change was filed against task/2573.\n"
                   "Until then, exit by hand.\n")
        self.assertEqual([], self._ids(pointer),
                         "a pointer does not anchor the next clause")
        self.assertEqual(["2573"], self._ids(
            pointer.replace("filed", "unfiled")),
            "'unfiled' is the opposite claim, so the citation is a subject")
        self.assertEqual([], self._ids(pointer.replace("filed", "refiled")),
                         "a re- prefix is the same act repeated")


#: THE ID A WARNING IS ABOUT, which is not the first `task/N` in its text: the
#: message quotes the phrase first, so a reader keyed on the first citation
#: reads the QUOTED row and agrees with a renderer that names a different
#: subject. Both shapes the rung emits are parsed here, and an unparseable
#: message is an error rather than a silent skip.
_ABOUT = re.compile(r" about task/([0-9]+),|^\S+:\d+ cites task/([0-9]+) as ")


def _subject_of(message):
    m = _ABOUT.search(message)
    if not m:
        raise AssertionError("no subject in warning: %r" % (message,))
    return m.group(1) or m.group(2)


class DocTaskConditionalHostileCorpusTest(unittest.TestCase):
    """THE REVIEWER'S HOSTILE CORPUS, RUN AS THE REVIEWER RAN IT (task/2612).

    Fifty-four cases from the outsider read of this lane, shipped with the
    source file's sha256 and each case's own text hash, replayed under the
    reviewer's contract: both roots, all three controls present throughout
    (a must-hit closed citation, an open row that must not warn, a
    provenance pointer that must not warn), 2598 open and everything else
    closed, and the expected (line, id) pairs compared in order. The
    multiline-comment witness the reviewer measured against main rides the
    same replay.

    A deliberately wrong sentinel rides the loop, so the comparator is proven
    able to disagree."""

    FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures",
                           "doctor_task2612_hostile_54.json")
    _ROW = re.compile(r"^(\S+):(\d+) ")
    #: PINNED HERE, NOT READ FROM THE FIXTURE. A digest a test reads out of
    #: the file it is checking asserts nothing: edit both and the arm agrees.
    SOURCE_SHA256 = ("2c4fc26f416dd8696876b426c3e88713"
                     "cb5a5ee56e458aa3f001115e5d438727")
    CASES_SHA256 = ("9ca7f76e31fde44efa01d263bb10b407"
                    "c7f221035778041300f55fe1bcdff2fd")

    @classmethod
    def setUpClass(cls):
        import json
        with open(cls.FIXTURE, encoding="utf-8") as fh:
            cls.fx = json.load(fh)

    def _replay(self, text):
        """({root: [[line, id], ...]}, {control path: warning count})."""
        import tempfile
        with tempfile.TemporaryDirectory(prefix="doc-hostile-") as d:
            for root in self.fx["roots"]:
                os.makedirs(os.path.join(d, root))
            for c in self.fx["controls"]:
                with open(os.path.join(d, c["path"]), "w",
                          encoding="utf-8") as fh:
                    fh.write(c["text"])
            for root in self.fx["roots"]:
                with open(os.path.join(d, root, "subject.md"), "w",
                          encoding="utf-8") as fh:
                    fh.write(text)          # no trailing newline: as shipped
            got = doctor.check_doc_task_conditionals(
                root=d,
                status_of=lambda tid: ("open" if tid in
                                       self.fx["status_rule"]["open_task_ids"]
                                       else "closed"))
        out = {r: [] for r in self.fx["roots"]}
        controls = {c["path"]: 0 for c in self.fx["controls"]}
        for lvl, msg in got:
            if lvl != doctor.WARN:
                continue
            m = self._ROW.match(msg)
            if not m:
                continue
            path, line = m.group(1), int(m.group(2))
            if path.endswith("subject.md"):
                out[path.split("/")[0]].append([line, _subject_of(msg)])
            else:
                # EVERY CONTROL IS COUNTED BY ITS OWN PATH. A total lets a
                # deleted must-hit in one root be paid for by a false warning
                # on that root's OPEN or PROVENANCE control, which is exactly
                # the substitution this corpus exists to catch.
                self.assertIn(path, controls,
                              "a warning from an unexpected file: %r" % msg)
                controls[path] += 1
        return out, controls

    def _assertControls(self, controls, label):
        for c in self.fx["controls"]:
            self.assertEqual(
                c["expected_warning_count"], controls[c["path"]],
                "%s: control %s (%s) fired %d time(s), expected %d — this "
                "case measured nothing, or a substitution paid for a deleted "
                "must-hit"
                % (label, c["path"], c["kind"], controls[c["path"]],
                   c["expected_warning_count"]))

    def test_the_shipped_corpus_is_the_one_the_reviewer_ran(self):  # noqa: VACUOUS_ASSERTION — three equalities against digests PINNED IN THIS FILE; nothing here asserts an absence and a fixture edit reddens it in two places
        """The recorded source digest is PINNED IN THIS FILE, and the cases
        carry their own joined digest, so editing the fixture's own recorded
        sha to match an edited corpus fails here."""
        import hashlib
        self.assertEqual(self.SOURCE_SHA256, self.fx["original_file_sha256"])
        self.assertEqual(
            self.CASES_SHA256,
            hashlib.sha256("\n".join(c["text"] for c in self.fx["cases"])
                           .encode()).hexdigest(),
            "the shipped case texts are not the reviewer's")
        self.assertEqual(self.CASES_SHA256, self.fx["cases_joined_sha256"])

    def test_every_hostile_case_answers_as_the_reviewer_measured(self):  # noqa: VACUOUS_ASSERTION — every case asserts each control separately per root (unconditional positives on the same scan) and the equality is against the SENTINEL's disagreement, which fails on a comparator that stopped comparing
        import hashlib
        cases = [(c["label"], c["text"],
                  [list(p) for p in c["expected_line_task_pairs_per_root"]])
                 for c in self.fx["cases"]]
        self.assertEqual(len(cases), 54)
        for c in self.fx["cases"]:
            self.assertEqual(
                hashlib.sha256(c["text"].encode()).hexdigest(),
                c["text_utf8_sha256"],
                "%s: the shipped text is not the text the reviewer ran"
                % c["label"])
        sentinel = ("SENTINEL-must-disagree",
                    "Until task/2573 lands, exit by hand.", [])
        cases.append(sentinel)
        wrong, compared = [], 0
        for label, text, expected in cases:
            got, controls = self._replay(text)
            self._assertControls(controls, label)
            for root in self.fx["roots"]:
                compared += 1
                if got[root] != expected:
                    wrong.append("%s [%s]: expected %r got %r"
                                 % (label, root, expected, got[root]))
        self.assertEqual(compared, 110)
        self.assertEqual(
            ["%s [%s]" % (sentinel[0], r) for r in self.fx["roots"]],
            [w.split(": expected")[0] for w in wrong],
            "the reviewer's hostile corpus disagrees with this tree:\n  "
            + "\n  ".join(wrong))

    def test_the_multiline_comment_witness_stays_quiet(self):  # noqa: VACUOUS_ASSERTION — the controls assertion above it is the unconditional positive control on the same scan, and it fires whether or not the witness is silent
        """The one case the reviewer measured against main: a promise inside
        a multi-line HTML comment. Main warns on it; the body of a comment is
        not prose, so this tree must not."""
        witness = self.fx["multiline_comment_witness"]
        got, controls = self._replay(witness["text"])
        self._assertControls(controls, "multiline-comment-witness")
        for root in self.fx["roots"]:
            self.assertEqual(
                [list(p) for p in witness["expected_line_task_pairs_per_root"]],
                got[root])


class DocTaskConditionalCommentAndClauseTest(unittest.TestCase):
    """A comment is not prose wherever it sits, a comma needs no space to
    start a clause, and one row promised twice in one sentence is one
    warning. This rung has no counterpart on trunk, so every false warning is
    worse than what a reader has today: each arm below pairs the silence it
    asserts with the same sentence in a shape that MUST warn."""

    def _ids(self, text):
        import tempfile
        with tempfile.TemporaryDirectory(prefix="doc-clause-") as d:
            os.makedirs(os.path.join(d, "docs"))
            with open(os.path.join(d, "docs", "s.md"), "w",
                      encoding="utf-8") as fh:
                fh.write(text)
            with open(os.path.join(d, "docs", "CONTROL.txt"), "w",
                      encoding="utf-8") as fh:
                fh.write("Until task/4242 lands, copy by hand.")
            got = doctor.check_doc_task_conditionals(
                root=d, status_of=lambda tid: "open" if tid == "2598"
                else "closed")
        self.assertEqual(
            1, sum(1 for lvl, m in got
                   if lvl == doctor.WARN and "CONTROL.txt:" in m),
            "MUST-HIT: the control was missed, so this case measured nothing")
        return [doctor._TASK_CITE.search(m).group(1) for lvl, m in got
                if lvl == doctor.WARN and m.startswith("docs/s.md:")]

    def test_a_comma_needs_no_space_to_start_a_clause(self):
        # THE POLE PAIR IS THE SAME SPACELESS COMMA, one clause apart. In the
        # first the row and the promise share a clause, so it is a subject; in
        # the second the clause before the comma only POINTS at it.
        self.assertEqual(
            ["2573"],
            self._ids("task/2573 is not yet available,while the signer waits."),
            "MUST-HIT: a row promised in its own clause is still a subject")
        self.assertEqual([], self._ids(
            "The fix was filed against task/2573,while the signer is not yet "
            "available."))
        self.assertEqual([], self._ids(
            "The signer is not yet available, so see task/2573."),
            "a pointer in the clause after the promise is not its subject")
        self.assertEqual([], self._ids(
            "task/2598 is not yet available,per task/2573."),
            "the open row warns about nothing and the pointer is not a subject")

    def test_a_comment_is_not_prose_wherever_it_opens(self):
        self.assertEqual(["2573"], self._ids(
            "Until task/2573 lands <!-- see also -->, exit by hand."),
            "MUST-HIT: prose around a comment is still prose")
        self.assertEqual([], self._ids(
            "The exit <!-- task/2573 --> is not yet available."))
        self.assertEqual([], self._ids(
            "<!-- task/2573 is\nnot yet available\n-->\nfine."))
        self.assertEqual([], self._ids(
            "<!-- task/2573 is not yet available\nstill inside"),
            "a comment nobody closed masks to the end of the body")

    def test_a_raw_text_block_ends_only_on_the_LITERAL_closing_tag(self):
        """`</script >` with an interior space closes nothing: CommonMark's
        end condition for a raw-text block is the literal string. Admitting
        the spaced form ended the block early and scanned the lines after it
        as prose, which on this rung is a warning about a sentence nobody
        can read."""
        self.assertEqual([], self._ids(
            "<script>\n</script >\nUntil task/2573 lands, exit by hand.\n"
            "</script>"))
        self.assertEqual(["2573"], self._ids(
            "<script>\nx\n</script>\nUntil task/2573 lands, exit by hand."),
            "MUST-HIT: the literal close really does end the block, so the "
            "silence above is the block and not a dead scanner")
        self.assertEqual([], self._ids(
            "<script>\nUntil task/2573 lands, exit by hand.\n</SCRIPT>"),
            "the end condition is case-insensitive")

    def test_one_row_promised_across_clauses_is_one_warning(self):
        self.assertEqual(["2573"], self._ids(
            "task/2573 is not yet available, and until task/2573 lands, and "
            "once task/2573 ships, wait."))
        self.assertEqual(["2573", "4244"], self._ids(
            "task/2573 is not yet available, and until task/4244 lands, wait."),
            "two rows in two clauses are still two warnings")
        self.assertEqual(["2573", "2573"], self._ids(
            "task/2573 is not yet available. Until then, wait."),
            "two SENTENCES about one row are still two warnings")


class DocTaskConditionalRound6ReproTest(DocTaskConditionalHostileCorpusTest):
    """THE REVIEWER'S ROUND-SIX WITNESSES, run exactly as their hostile corpus
    is: same replay, same per-root controls, same pinned digests, same wrong
    sentinel. Twelve cases across five families -- comma-no-space, inline
    comments, malformed raw-text closers, and a duplicate-sentence pair whose
    control keeps distinct sentences distinct.

    It inherits the hostile arm's machinery ON PURPOSE: a second replay written
    by hand is a second chance to write a weaker one, and the mutants that
    survived round five all lived in replay code."""

    FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures",
                           "doctor_task2612_round6_repro.json")
    SOURCE_SHA256 = ("aa0b78ffb8d7d8d25d7ecdd55cce8de3"
                     "1e4351721e2325966651e9731d5e1af9")
    CASES_SHA256 = ("5d2f3d06a36d2e3e31b7c12d02f82cf6"
                    "7169d35d3906529118eba5d01c6eb80d")

    def test_every_hostile_case_answers_as_the_reviewer_measured(self):  # noqa: VACUOUS_ASSERTION — inherited replay; every case asserts each control separately per root and the equality is against the SENTINEL's disagreement
        cases = [(c["label"], c["text"],
                  [list(p) for p in c["expected_line_task_pairs_per_root"]])
                 for c in self.fx["cases"]]
        self.assertEqual(len(cases), 12)
        self.assertEqual(
            5, len({c["family"] for c in self.fx["cases"]}),
            "the five families the round-six read named must all be present")
        sentinel = ("SENTINEL-must-disagree",
                    "Until task/2573 lands, exit by hand.", [])
        cases.append(sentinel)
        wrong, compared = [], 0
        for label, text, expected in cases:
            got, controls = self._replay(text)
            self._assertControls(controls, label)
            for root in self.fx["roots"]:
                compared += 1
                if got[root] != expected:
                    wrong.append("%s [%s]: expected %r got %r"
                                 % (label, root, expected, got[root]))
        self.assertEqual(compared, 26)
        self.assertEqual(
            ["%s [%s]" % (sentinel[0], r) for r in self.fx["roots"]],
            [w.split(": expected")[0] for w in wrong],
            "the reviewer's round-six witnesses disagree with this tree:\n  "
            + "\n  ".join(wrong))

    def test_the_multiline_comment_witness_stays_quiet(self):  # noqa: VACUOUS_ASSERTION — a skip, not an assertion: this fixture carries no witness field and the hostile corpus asserts the real one
        self.assertNotIn("multiline_comment_witness", self.fx,
                         "this fixture now carries a witness — assert it "
                         "rather than skipping")
        self.skipTest("the multiline-comment witness rides the hostile corpus")


class DocTaskConditionalCheckTest(DoctorBase):
    """A doc sentence that says "until task/N lands, do X by hand" is true when
    written and becomes false the day N lands, with no edit and no reviewer
    able to see it — the file reads identically before and after.

    The rung joins the prose against the task store. These arms drive it with
    a PLANTED store so they assert the classifier, not tonight's ledger."""

    CONTROL_TASK = "4242"
    CONTROL_DOC = ("the control door is task/4242\n"
                   "and it works once it lands; until then do it by hand.\n")

    def plant(self, files, statuses):
        root = tempfile.mkdtemp(prefix="helm-test-doccond-")
        self.addCleanup(shutil.rmtree, root, True)
        sub = os.path.join(root, "agents", "claudecode", "skills", "x")
        os.makedirs(sub)
        # BOTH SCAN ROOTS EXIST, because the production tree has both and the
        # rung now WARNS on an absent one — a fixture that plants only half
        # the corpus is telling the rung it is aimed somewhere wrong, and the
        # arm would then be reading that complaint instead of its subject.
        os.makedirs(os.path.join(root, "docs"))
        for name, text in files.items():
            with open(os.path.join(sub, name), "w") as fh:
                fh.write(text)
        return doctor.check_doc_task_conditionals(
            root=root, status_of=lambda tid: statuses.get(tid))

    def scan(self, text, statuses, name="SKILL.md"):
        return self.plant({name: text}, statuses)

    def warnings(self, *a, **kw):
        return [m for lvl, m in self.scan(*a, **kw) if lvl == doctor.WARN]

    def silent_about(self, text, statuses, tid, name="SKILL.md"):
        """Assert the rung says nothing about `tid` WHILE PROVING IT RAN.

        AN ABSENCE ARM OVER A WALKER IS SATISFIED BY A WALKER THAT NEVER
        ARRIVED. If the plant landed outside the scanned prefix, the suffix
        filter skipped it, or the check returned [] off an exception, then
        "no warning about task/N" is true for a reason that has nothing to
        do with task/N — and the two outcomes render identically.

        So every no-warning arm here plants a SECOND file carrying a
        known-CLOSED promise in the same tree. That control warning is the
        receipt that this corpus was read and this classifier fired; the
        subject's silence means something only underneath it."""
        statuses = dict(statuses)
        statuses[self.CONTROL_TASK] = "closed"
        got = [m for lvl, m in
               self.plant({name: text, "CONTROL.md": self.CONTROL_DOC},
                          statuses)
               if lvl == doctor.WARN]
        self.assertTrue(
            any("task/" + self.CONTROL_TASK in m for m in got),
            "THE CONTROL DID NOT WARN, so this corpus was never read and "
            "the subject's silence proves nothing about the subject: %r"
            % (got,))
        return [m for m in got if "task/" + tid in m]

    def test_a_CLOSED_row_cited_as_a_future_condition_warns(self):
        """The shape that shipped: the verb existed and the sentence said it
        did not, routing a reader to the manual path it called temporary."""
        got = self.warnings(
            "`helm seat rehome <seat> --home H --apply` (task/2573) does\n"
            "the exit in one verb once it lands; until then the pane is\n"
            "exited by hand only with the owner's word.\n",
            {"2573": "closed"})
        self.assertEqual(len(got), 1, got)
        self.assertIn("task/2573", got[0])
        self.assertIn("CLOSED", got[0])

    def test_the_construction_on_the_NEXT_line_is_still_caught(self):
        """THE DISCRIMINATOR. The citation and the condition live on
        DIFFERENT lines in the real instance, so a line-anchored scan finds
        ZERO of it. This arm fails against any implementation that greps a
        line rather than a window."""
        got = self.warnings(
            "the one-verb door is task/2573\n"
            "and it works once it lands; until then do it by hand.\n",
            {"2573": "closed"})
        self.assertEqual(len(got), 1,
                         "the condition sits one line below the citation and "
                         "was missed: %r" % (got,))

    def test_an_OPEN_row_cited_the_same_way_is_silent(self):  # noqa: VACUOUS_ASSERTION — the unconditional
        # positive control is `silent_about`'s CONTROL.md plant, which this
        # walker cannot see because it does not follow test helper methods.
        # Proven load-bearing: relocating the plant outside the scanned
        # prefix turns this arm RED on the control, not green on absence.
        """The honest form of the shape — a promise about work that really is
        outstanding. Without this pole the arm above is satisfied by a rung
        that warns on every citation."""
        self.assertEqual(
            self.silent_about("task/2598 makes it one flag; until then, "
                              "re-pin\nafter any relaunch.\n",
                              {"2598": "open"}, "2598"), [])

    def test_a_PROVENANCE_citation_never_warns_even_when_closed(self):  # noqa: VACUOUS_ASSERTION — the unconditional
        # positive control is `silent_about`'s CONTROL.md plant, which this
        # walker cannot see because it does not follow test helper methods.
        # Proven load-bearing: relocating the plant outside the scanned
        # prefix turns this arm RED on the control, not green on absence.
        """"filed as task/2573" records WHERE a decision lives and stays true
        forever. A rung that cannot tell provenance from a promise would fire
        on every historical reference in the tree and be turned off."""
        self.assertEqual(
            self.silent_about("The hand copy remains only as a named fallback "
                              "with its risks, filed against task/2573.\n",
                              {"2573": "closed"}, "2573"), [])

    def test_THREE_CANONICAL_PROMISE_SPELLINGS_all_warn(self):
        """A PROMISE VOCABULARY IS A SET, AND A SET IS WHERE COVERAGE ROTS.
        The same promise is written a dozen ways, and a pattern built from the
        one instance in front of you reads clean over every other spelling —
        silently, because a rung that finds nothing and a rung that cannot see
        anything render identically. These three are canonical forms that a
        pattern built from a single observed instance does not reach."""
        for text in (
                "the one-verb door is task/2573 and until task/2573 lands the "
                "pane is exited by hand.\n",
                "while task/2573 is pending, the hand copy remains the only "
                "route.\n",
                "task/2573 is not yet available, so do it by hand.\n"):
            got = self.warnings(text, {"2573": "closed"})
            self.assertEqual(len(got), 1,
                             "this spelling of the promise reads clean: %r -> "
                             "%r" % (text, got))
            self.assertIn("CLOSED", got[0])

    def test_an_ANAPHORIC_promise_in_the_NEXT_sentence_still_couples(self):
        """THE SHAPE THE ONE REAL INSTANCE IN THIS TREE TAKES, and the coverage
        that a same-sentence rule silently drops. "Until then" names no row —
        the "then" points BACK at the landing the previous sentence described —
        so the citation and the promise live in different sentences by ordinary
        English, not by sloppiness.

        A rule requiring both halves in one sentence takes the live run to
        ZERO findings here, and a rung that finds nothing is indistinguishable
        from a rung that cannot see anything — so this shape is pinned."""
        got = self.warnings(
            "`helm seat launch` regenerates config.yaml and undoes the pin; "
            "task/2573 makes it one flag and shows the account per seat.\n"
            "Until then, re-pin after any relaunch and say so in the room.\n",
            {"2573": "closed"})
        self.assertEqual(len(got), 1,
                         "the promise is anaphoric and sits in the sentence "
                         "AFTER its citation — the real construction: %r"
                         % (got,))
        self.assertIn("task/2573", got[0])
        self.assertIn("CLOSED", got[0])

    def test_an_UNRELATED_nearby_condition_is_NOT_married_to_a_citation(self):
        """THE OTHER DIRECTION OF THE WINDOW, and the one a line-distance rule
        cannot get right at any width. A provenance citation followed a couple
        of lines later by a promise about something ELSE is two separate true
        sentences; joining them invents a stale promise and teaches a reader
        to distrust the rung. Coupling by SENTENCE is what makes wrapping
        invisible while keeping a neighbour out of reach."""
        got = self.warnings(
            "The hand copy remains a named fallback, filed against "
            "task/2573.\n"
            "Separately, the signer rollout is blocked on hardware and until "
            "then we stage by hand.\n",
            {"2573": "closed"})
        self.assertEqual(
            got, [],
            "an unrelated promise two lines below a PROVENANCE citation was "
            "married to it: %r" % (got,))

    def test_a_MISSING_scan_root_is_never_reported_clean(self):
        """A MISAIMED INSTRUMENT REPORTING A CLEAN ESTATE IS WORSE THAN ONE
        THAT REPORTS NOTHING. An absent scan root means this rung measured
        nothing there — most often because it is pointed somewhere wrong — and
        a reader takes a clean OK as "I looked and it is fine"."""
        root = tempfile.mkdtemp(prefix="helm-test-doccond-empty-")
        self.addCleanup(shutil.rmtree, root, True)
        got = [m for lvl, m in doctor.check_doc_task_conditionals(
            root=root, status_of=lambda tid: None) if lvl == doctor.WARN]
        self.assertTrue(got, "an empty tree reported clean")
        self.assertTrue(any("measured nothing" in m or "no .md" in m
                            for m in got),
                        "the warning does not say that nothing was measured: "
                        "%r" % (got,))
        # PAIRED POLE: a tree with both roots and no citations is genuinely
        # clean, so the warning above is about ABSENCE and not about emptiness.
        ok = self.plant({"SKILL.md": "nothing to see here.\n"}, {})
        self.assertEqual([m for lvl, m in ok if lvl == doctor.WARN], [],
                         "a real corpus with no citations must be clean: %r"
                         % (ok,))

    def test_an_UNRESOLVABLE_row_says_UNKNOWN_not_fine(self):
        """A row this reader cannot look up is not a row that is fine, and the
        two must not share a rendering."""
        got = self.warnings("see task/9999 once it lands; until then hand-roll\n",
                            {})
        self.assertEqual(len(got), 1, got)
        self.assertIn("UNKNOWN", got[0])
        self.assertNotIn("CLOSED", got[0])


class SkillsHubCheckTest(DoctorBase):
    """check_skills_hub — one FAIL row per config dir whose skills entry is
    missing or does not resolve to the hub, naming the path and the repair;
    a fully-linked estate is one OK row; no hub configured is no row."""

    def _estate(self):
        hub = os.path.join(self.tmp.name, "skills-hub")
        os.makedirs(os.path.join(hub, "learn"))
        linked = os.path.join(self.tmp.name, "claude-homes", "linked-com")
        unlinked = os.path.join(self.tmp.name, "claude-homes", "unlinked-com")
        os.makedirs(linked)
        os.makedirs(unlinked)
        os.symlink(hub, os.path.join(linked, "skills"))
        return hub, linked, unlinked

    def test_one_fail_names_exactly_the_unlinked_home(self):
        hub, linked, unlinked = self._estate()
        dirs = [("linked-com", linked), ("unlinked-com", unlinked)]
        res = doctor.check_skills_hub(dirs=dirs, canon=hub)
        fails = levels(res, doctor.FAIL)
        self.assertEqual(len(fails), 1, res)
        row = fails[0]
        self.assertIn("unlinked-com", row)
        self.assertIn(os.path.join(unlinked, "skills"), row)
        self.assertIn("MISSING", row)
        self.assertIn("helm skills sync --apply", row)
        self.assertNotIn("linked-com " + linked, row)
        self.assertEqual(levels(res, doctor.OK), [])
        # the same estate with the miss cured is one OK row and no FAIL —
        # the must-hit that proves the FAIL above is about THAT home
        os.symlink(hub, os.path.join(unlinked, "skills"))
        res = doctor.check_skills_hub(dirs=dirs, canon=hub)
        self.assertEqual(levels(res, doctor.FAIL), [])
        self.assertEqual(levels(res, doctor.OK),
                         ["skills hub: 2 config dir(s) link %s" % hub])

    def test_a_link_elsewhere_and_a_real_dir_each_fail_with_their_state(self):
        hub, linked, unlinked = self._estate()
        elsewhere = os.path.join(self.tmp.name, "elsewhere")
        os.makedirs(elsewhere)
        os.symlink(elsewhere, os.path.join(unlinked, "skills"))
        real = os.path.join(self.tmp.name, "seats", "kimi", "claude")
        os.makedirs(os.path.join(real, "skills"))
        dirs = [("linked-com", linked), ("unlinked-com", unlinked), ("seat:kimi", real)]
        fails = levels(doctor.check_skills_hub(dirs=dirs, canon=hub), doctor.FAIL)
        self.assertEqual(len(fails), 2, fails)
        self.assertIn("-> %s, NOT the hub" % elsewhere, fails[0])
        self.assertIn("seat:kimi", fails[1])
        self.assertIn("REAL dir", fails[1])

    @unittest.skipIf(os.geteuid() == 0, "root lists a mode-000 dir; the witness needs EACCES")
    def test_an_unlistable_seat_subtree_is_never_read_as_ok(self):
        """The review witness, driven through the REAL discovery (dirs=None,
        seats under this test's HELM_HOME, credhome roots patched): hub
        healthy, every visible config dir linked, an UNLINKED instance dir
        hidden under an instances dir the doctor cannot list. The row must
        not read OK. The must-hit: make the subtree listable and the SAME
        estate FAILs naming the hidden dir — so the WARN was about a real
        omission, not a dir that was never there — then link it and the
        estate is OK."""
        from helm import homes
        hub = os.path.join(self.tmp.name, "skills-hub")
        os.makedirs(os.path.join(hub, "learn"))
        croot = os.path.join(self.tmp.name, "claude-homes")
        os.makedirs(os.path.join(croot, "a-com"))
        os.symlink(hub, os.path.join(croot, "a-com", "skills"))
        roots = mock.patch.dict(homes.ROOTS, {"claude": croot})
        defaults = mock.patch.dict(
            homes.DEFAULTS, {"claude": os.path.join(self.tmp.name, "default-claude")})
        for patch in (roots, defaults):
            patch.start()
            self.addCleanup(patch.stop)
        seats = os.path.join(home.global_dir(), "seats")
        fam = os.path.join(seats, "kimi", "claude")
        os.makedirs(fam)
        os.symlink(hub, os.path.join(fam, "skills"))
        inst = os.path.join(seats, "kimi", "instances")
        hidden = os.path.join(inst, "kimi-2", "claude")
        os.makedirs(hidden)                                  # unlinked, hidden
        os.chmod(inst, 0)
        # tearDown's TemporaryDirectory.cleanup runs BEFORE addCleanup and
        # already removes a mode-000 subtree, so the restore only applies
        # when a failure left the dir behind
        self.addCleanup(lambda: os.path.isdir(inst) and os.chmod(inst, 0o755))
        res = doctor.check_skills_hub(canon=hub)
        self.assertEqual(levels(res, doctor.OK), [], res)
        warns = levels(res, doctor.WARN)
        self.assertEqual(len(warns), 1, res)
        self.assertIn("census INCOMPLETE", warns[0])
        self.assertIn("%s (EACCES)" % inst, warns[0])
        self.assertIn("2 dir(s) it could see", warns[0])
        os.chmod(inst, 0o755)
        res = doctor.check_skills_hub(canon=hub)
        fails = levels(res, doctor.FAIL)
        self.assertEqual(len(fails), 1, res)
        self.assertIn(os.path.join(hidden, "skills"), fails[0])
        self.assertIn("MISSING", fails[0])
        self.assertEqual(levels(res, doctor.WARN), [])
        os.symlink(hub, os.path.join(hidden, "skills"))
        self.assertEqual(doctor.check_skills_hub(canon=hub),
                         [(doctor.OK, "skills hub: 3 config dir(s) link %s" % hub)])

    def test_no_hub_configured_is_no_row_and_the_check_is_shipped(self):
        # HELM_HOME is the tmp estate: no authored host block, no env knob
        with mock.patch.dict(os.environ):
            os.environ.pop("HELM_SKILLS_CANONICAL", None)
            os.environ.pop("MELD_SKILLS_CANONICAL", None)
            self.assertEqual(doctor.check_skills_hub(dirs=[]), [])
        self.assertIn("check_skills_hub", doctor.CHECKS)
        # a configured hub that is MISSING is its own FAIL, never a scan that
        # reads every linked dir as fine because realpath cannot resolve
        gone = os.path.join(self.tmp.name, "no-such-hub")
        fails = levels(doctor.check_skills_hub(dirs=[], canon=gone), doctor.FAIL)
        self.assertEqual(len(fails), 1, fails)
        self.assertIn(gone, fails[0])
        self.assertIn("MISSING", fails[0])


class ProjectionRegistryBase(DoctorBase):
    """The check_projection_registry fixture: `_row` builds one registry
    row, and `_check` runs the doctor check over a registry that holds only
    it.

    THIS CLASS HOLDS NO `test_*` METHOD, and that is its contract. unittest
    collects every inherited `test_*` again under each subclass's own id, so
    an arm written here runs once per subclass. A class that needs this
    fixture subclasses THIS class; its arms go in the subclass."""

    def _row(self, **kw):
        base = {"name": "probe", "kind": "projection", "root": "home",
                "globs": ("probe.json",), "source": "the probe source",
                "sources": (os.path.join(self.tmp.name, "scan-root"),),
                "rebuild": "helm probe", "fresh_days": None, "mutable": False}
        base.update(kw)
        return base

    def _check(self, row):
        from helm import registry
        with mock.patch.object(registry, "projections", lambda: (row,)):
            return doctor.check_projection_registry()


class TestProjectionRegistry(ProjectionRegistryBase):
    """check_projection_registry: laws 2+3 enforced read-only — undeclared
    rebuild/source FAILs, an orphaned projection FAILs, declared staleness
    WARNs, squatters WARN; a clean scaffolded estate is one OK row.

    A subclass of THIS class runs every arm below again under its own id,
    which is right only for one that changes the fixture those arms read. A
    class that only needs the fixture subclasses ProjectionRegistryBase."""

    def test_green_scaffold_is_single_ok(self):
        home.scaffold_global()
        res = doctor.check_projection_registry()
        self.assertEqual([lvl for lvl, _ in res], [doctor.OK])
        self.assertIn("0 squatters", res[0][1])

    def test_undeclared_rebuild_and_source_fail(self):
        for kw in ({"rebuild": None}, {"sources": ()}):
            res = self._check(self._row(**kw))
            fails = levels(res, doctor.FAIL)
            self.assertTrue(any("undeclared" in m and "gitignored" in m
                                for m in fails), kw)

    def test_exact_genesis_fails_when_seats_exist(self):
        """A registered seat ends cold genesis even while projection bytes
        remain canonically empty: missing sources are now real orphans."""
        home.scaffold_global()
        pk.write_json(os.path.join(self.helm_home, "probe.json"), {
            "version": 1, "projects": {}, "generated_ts": "2026-07-31T00:00:00Z",
        })
        row = self._row(
            sources=(os.path.join(self.tmp.name, "no-such-source"),),
            genesis={
                "json": {"version": 1, "projects": {}},
                "volatile_strings": ("generated_ts",),
            },
        )
        with mock.patch("helm.seat.registered_seats",
                        return_value=(["codex"], False)):
            res = self._check(row)
        self.assertTrue(any("ORPHANED" in m and "only truth" in m
                            for m in levels(res, doctor.FAIL)))

    def test_declared_exact_genesis_is_not_orphaned(self):
        home.scaffold_global()
        pk.write_json(os.path.join(self.helm_home, "probe.json"), {
            "version": 1, "projects": {}, "generated_ts": "2026-07-31T00:00:00Z",
        })
        res = self._check(self._row(
            sources=(os.path.join(self.tmp.name, "no-such-source"),),
            genesis={
                "json": {"version": 1, "projects": {}},
                "volatile_strings": ("generated_ts",),
            },
        ))
        self.assertEqual(levels(res, doctor.FAIL), [])

    def test_declared_genesis_with_data_is_orphaned(self):
        home.scaffold_global()
        pk.write_json(os.path.join(self.helm_home, "probe.json"), {
            "version": 1,
            "projects": {"lost": {"path": "/gone"}},
            "generated_ts": "2026-07-31T00:00:00Z",
        })
        res = self._check(self._row(
            sources=(os.path.join(self.tmp.name, "no-such-source"),),
            genesis={
                "json": {"version": 1, "projects": {}},
                "volatile_strings": ("generated_ts",),
            },
        ))
        self.assertTrue(any("ORPHANED" in m
                            for m in levels(res, doctor.FAIL)))

    def test_genesis_contract_is_exact_and_fails_closed(self):
        home.scaffold_global()
        path = os.path.join(self.helm_home, "probe.json")
        row = self._row(
            sources=(os.path.join(self.tmp.name, "no-such-source"),),
            genesis={
                "json": {"version": 1, "projects": {}},
                "volatile_strings": ("generated_ts",),
            },
        )
        cases = (
            {"version": 1, "projects": {}},
            {"version": 1, "projects": {}, "generated_ts": 7},
            {"version": 1, "projects": {},
             "generated_ts": "2026-07-31T00:00:00Z", "extra": True},
        )
        for body in cases:
            with self.subTest(body=body):
                pk.write_json(path, body)
                res = self._check(row)
                self.assertTrue(any("ORPHANED" in m
                                    for m in levels(res, doctor.FAIL)))
        pk.atomic_write(path, "{")
        self.assertTrue(any("ORPHANED" in m for m in
                            levels(self._check(row), doctor.FAIL)))
        from helm import registry
        self.assertFalse(registry.projection_is_genesis(
            dict(row, files=("probe.json", "another.json"))))

    def test_present_source_is_ok_and_absent_projection_skips_orphan_check(self):
        home.scaffold_global()
        # no probe.json on disk: a gone source is NOT an orphan (nothing to lose)
        res = self._check(self._row(
            sources=(os.path.join(self.tmp.name, "no-such-source"),)))
        self.assertEqual(levels(res, doctor.FAIL), [])
        pk.atomic_write(os.path.join(self.helm_home, "probe.json"), "{}")
        res = self._check(self._row())  # source exists -> healthy
        self.assertEqual(levels(res, doctor.FAIL), [])

    def test_stale_projection_warns(self):
        home.scaffold_global()
        p = os.path.join(self.helm_home, "probe.json")
        pk.atomic_write(p, "{}")
        os.utime(p, (0, 0))  # epoch: decades past any horizon
        res = self._check(self._row(fresh_days=30))
        self.assertTrue(any("stale" in m and "helm probe" in m
                            for m in levels(res, doctor.WARN)))

    def test_mutable_row_fails(self):
        res = self._check(self._row(mutable=True))
        self.assertTrue(any("read-only-as-truth" in m
                            for m in levels(res, doctor.FAIL)))

    def test_squatters_warn_with_paths(self):
        home.scaffold_global()
        state = os.path.join(home.global_dir(), ".state")
        pk.atomic_write(os.path.join(state, "mystery.bin"), "?")
        res = doctor.check_projection_registry()
        warns = levels(res, doctor.WARN)
        self.assertTrue(any("SQUATTER" in m and "mystery.bin" in m
                            and "helm projections" in m for m in warns))
        self.assertIn("1 squatter", levels(res, doctor.OK)[0])

    def test_survey_trouble_is_warn_not_crash(self):
        from helm import registry
        with mock.patch.object(registry, "projection_survey",
                               side_effect=OSError("boom")):
            res = doctor.check_projection_registry()
        self.assertEqual(res[0][0], doctor.WARN)
        self.assertIn("unreadable", res[0][1])


class CleanHomeColdStartTest(unittest.TestCase):
    def test_sync_then_doctor_accepts_source_free_genesis(self):
        root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        helm = os.path.join(root, "bin", "helm")
        with tempfile.TemporaryDirectory(prefix="helm-clean-home-") as tmp:
            env = {
                "HOME": tmp,
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "PYTHONIOENCODING": "utf-8",
                "HELM_METAHARNESS": "none",
            }
            for rel in ("dev", ".claude", ".codex",
                        ".local/share/opencode", ".pi"):
                self.assertFalse(os.path.exists(os.path.join(tmp, rel)), rel)

            sync = subprocess.run(
                [sys.executable, helm, "sync"], cwd=tmp, env=env,
                text=True, capture_output=True, timeout=30,
            )
            self.assertEqual(sync.returncode, 0, sync.stdout + sync.stderr)

            with open(os.path.join(tmp, ".helm", "_global", "registry.json"),
                      encoding="utf-8") as f:
                reg = json.load(f)
            self.assertEqual(reg["version"], 1)
            self.assertEqual(reg["projects"], {})
            self.assertIsInstance(reg["generated_ts"], str)
            with open(os.path.join(tmp, ".cache", "helm",
                                   "codex-cwd-cache.json"), encoding="utf-8") as f:
                self.assertEqual(json.load(f), {})

            for run in range(3):
                check = subprocess.run(
                    [sys.executable, helm, "doctor"], cwd=tmp, env=env,
                    text=True, capture_output=True, timeout=30,
                )
                output = check.stdout + check.stderr
                self.assertEqual(check.returncode, 0, "run %d:\n%s" % (run + 1, output))
                self.assertIn("0 fail", output)
                self.assertNotIn("ORPHANED", output)


class ActuatorWiringDoctorTest(unittest.TestCase):
    def test_registered_check_folds_the_whole_class_into_one_warning(self):
        data = {"consumers": 2, "wired": {},
                "missing": ["hostpath-pre-push", "worktree-gc"],
                "unknown": ["dispatch-mix"], "invalid_allowed": []}
        with mock.patch.object(wiring, "actuator_census", return_value=data):
            rows = doctor.check_actuator_wiring()
        self.assertIn("check_actuator_wiring", doctor.CHECKS)
        self.assertLessEqual(doctor.CHECKS.index("check_actuator_wiring"), 1,
                             "the warning must not be buried below the flood")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], doctor.WARN)
        self.assertIn("hostpath-pre-push", rows[0][1])
        self.assertIn("dispatch-mix", rows[0][1])


class TestCmdDoctor(DoctorBase):
    def run_doctor(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = doctor.cmd_doctor([])
        return rc, buf.getvalue()

    def snapshot(self, root):
        state = {}
        for dirpath, dirnames, filenames in os.walk(root):
            for f in filenames:
                p = os.path.join(dirpath, f)
                if os.path.islink(p):
                    state[p] = os.readlink(p)
                    continue
                with open(p, "rb") as fh:
                    state[p] = fh.read()
        return state

    def test_full_report_exit_1_on_fail_and_read_only(self):
        self.seed_home()
        adopted = self.seed_adopted()
        before = self.snapshot(self.tmp.name)
        with mock.patch.object(home, "adopted_memory_dir", lambda: adopted), \
                mock.patch.object(doctor, "check_cv",
                                  lambda: [(doctor.OK, "cv stub")]), \
                mock.patch.object(doctor, "check_cred_families",
                                  lambda: [(doctor.OK, "families stub")]), \
                mock.patch.object(doctor, "check_seat_memory_ceilings",
                                  lambda: [(doctor.OK, "throttle stub")]):
            rc, out = self.run_doctor()
        self.assertEqual(rc, 1)  # the broken symlink FAIL
        self.assertIn("FAIL", out)
        self.assertIn("broken symlink", out)
        self.assertIn("repo moved or deleted", out)
        self.assertIn("adoption conflict", out)
        self.assertIn("drain --sweep-dups", out)
        self.assertIn("know-your-user leg is empty", out)
        self.assertIn("helm doctor:", out)
        self.assertIn("1 fail", out)
        self.assertEqual(self.snapshot(self.tmp.name), before)  # READ-ONLY always

    def test_green_estate_exits_0(self):
        home.scaffold_global()
        repo = os.path.join(self.tmp.name, "repos", "solo")
        os.makedirs(repo)
        home.scaffold_project("solo")
        pk.write_json(home.registry_path(), {"version": 1, "projects": {
            "solo": {"name": "solo", "path": repo, "kind": "git",
                     "status": "active", "sessions": {}, "memory_dir": None}}})
        adopted = os.path.join(self.tmp.name, "adopted-clean")
        os.makedirs(adopted)
        pk.atomic_write(os.path.join(adopted, "prior-only.md"), "stub\n")
        whoami.save_profile({"schema_version": 2, "technical_level": "expert",
                             "guidance": ["short replies"], "interview_status": "done",
                             "updated_at": "", "source": "fresh"})
        with mock.patch.object(home, "adopted_memory_dir", lambda: adopted), \
                mock.patch.object(doctor, "check_cv",
                                  lambda: [(doctor.OK, "cv stub")]), \
                mock.patch.object(doctor, "check_cred_families",
                                  lambda: [(doctor.OK, "families stub")]), \
                mock.patch.object(doctor, "check_seat_memory_ceilings",
                                  lambda: [(doctor.OK, "throttle stub")]):
            rc, out = self.run_doctor()
        self.assertEqual(rc, 0)
        self.assertIn("0 fail", out)
        self.assertNotIn("FAIL", out)


class PhysicsCurrencyTest(unittest.TestCase):
    def _run(self, version_out):
        import shutil as sh
        import subprocess as sp
        done = mock.Mock(returncode=0, stdout=version_out, stderr="")
        with mock.patch.object(sh, "which", lambda t: "/usr/bin/" + t), \
                mock.patch.object(sp, "run", return_value=done), \
                mock.patch.object(doctor, "PHYSICS_PROBED", {"claude": "2.1.207"}):
            return doctor.check_physics_currency()

    def test_matching_version_is_ok(self):
        res = self._run("2.1.207 (Claude Code)")
        self.assertEqual([lvl for lvl, _ in res], [doctor.OK])

    def test_drifted_version_warns_with_both_versions(self):
        res = self._run("2.1.215 (Claude Code)")
        self.assertEqual(res[0][0], doctor.WARN)
        self.assertIn("2.1.215", res[0][1])
        self.assertIn("2.1.207", res[0][1])

    def test_unparseable_version_warns(self):
        res = self._run("not a version at all")
        self.assertEqual(res[0][0], doctor.WARN)
        self.assertIn("unparseable", res[0][1])

    def test_missing_binary_is_silent(self):
        import shutil as sh
        with mock.patch.object(sh, "which", lambda t: None), \
                mock.patch.object(doctor, "PHYSICS_PROBED", {"claude": "2.1.207"}):
            self.assertEqual(doctor.check_physics_currency(), [])

    def test_version_probe_failure_warns_not_silent(self):
        # the sentinel must not fail QUIET exactly when currency is unknowable
        import shutil as sh
        import subprocess as sp
        with mock.patch.object(sh, "which", lambda t: "/usr/bin/" + t), \
                mock.patch.object(sp, "run", side_effect=OSError("boom")), \
                mock.patch.object(doctor, "PHYSICS_PROBED", {"claude": "2.1.207"}):
            res = doctor.check_physics_currency()
        self.assertEqual(res[0][0], doctor.WARN)
        self.assertIn("UNKNOWN", res[0][1])

    def test_git_present_is_ok(self):
        import shutil as sh
        with mock.patch.object(sh, "which", lambda t: "/usr/bin/git"):
            res = doctor.check_git()
        self.assertEqual(res, [(doctor.OK, "git on PATH (/usr/bin/git)")])

    def test_git_absent_warns_with_distro_command_and_jj_note(self):
        import shutil as sh
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix="os-release") as f:
            f.write('NAME="Debian GNU/Linux"\nID=debian\n')
            f.flush()
            hint = doctor._git_install_hint(os_release=f.name, platform="linux")
        self.assertEqual(hint, "sudo apt install git")
        with mock.patch.object(sh, "which", lambda t: None), \
                mock.patch.object(doctor, "_git_install_hint",
                                  lambda: "sudo apt install git"):
            res = doctor.check_git()
        self.assertEqual(res[0][0], doctor.WARN)
        self.assertIn("sudo apt install git", res[0][1])
        self.assertIn("jj/jujutsu", res[0][1])
        self.assertIn("degrade", res[0][1])

    def test_git_install_hint_distro_table(self):
        import tempfile
        cases = (("ID=ubuntu\nID_LIKE=debian\n", "sudo apt install git"),
                 ("ID=fedora\n", "sudo dnf install git"),
                 ("ID=arch\n", "sudo pacman -S git"),
                 ('ID="opensuse-tumbleweed"\nID_LIKE="suse"\n', "sudo zypper install git"),
                 ("ID=alpine\n", "sudo apk add git"),
                 ("ID=plan9\n", "install git via your distro's package manager"))
        for text, want in cases:
            with tempfile.NamedTemporaryFile("w") as f:
                f.write(text)
                f.flush()
                self.assertEqual(doctor._git_install_hint(os_release=f.name,
                                                          platform="linux"), want, text)
        self.assertEqual(doctor._git_install_hint(platform="darwin"),
                         "xcode-select --install")
        self.assertEqual(doctor._git_install_hint(os_release="/no/such",
                                                  platform="linux"),
                         "install git via your distro's package manager")

    def test_authored_non_object_entry_fails_not_crashes(self):
        # a parseable file with a null entry must FAIL, never raise —
        # hermetic: never touch the live helm home
        import tempfile
        with tempfile.TemporaryDirectory(prefix="helm-doctor-auth-") as tmp, \
                mock.patch.dict(os.environ, {"HELM_HOME": tmp}):
            os.makedirs(os.path.dirname(doctor.home.authored_path()), exist_ok=True)
            pk.write_json(doctor.home.authored_path(),
                          {"version": 1, "projects": {"proj": None}})
            res = doctor.check_authored()
        self.assertEqual(res[0][0], doctor.FAIL)
        self.assertIn("proj", res[0][1])


class StartupDoorsCheckTest(unittest.TestCase):
    """The rung that measures a guard helm does not ship.

    Half of what makes a per-tool-call hook answer inside its budget is not in
    this repository at all: a machine-local per-interpreter site file installs
    the local-suite refusal doors through a `sys.meta_path` finder instead of
    importing the test engines at every interpreter start. Reading that file
    would be a proxy that goes stale, so the rung measures the EFFECT — what a
    fresh interpreter carries, and whether that same interpreter still refuses
    a suite — and these arms drive its three states through `probe`, the seam
    production fills with `_startup_probe`. One arm runs the real probe so a
    dict can never be the only input this file has ever seen.
    """

    LAZY = {"ok": True, "eager": [], "site_ms": 14.0, "refused": True,
            "user_site": "/probe/user-site", "pid": 4242}

    def probe(self, **over):
        row = dict(self.LAZY)
        row.update(over)
        return lambda: row

    def test_lazy_and_armed_is_good_and_says_what_it_did_not_verify(self):
        res = doctor.check_startup_doors(probe=self.probe())
        self.assertEqual([lvl for lvl, _ in res], [doctor.OK])
        msg = res[0][1]
        self.assertIn("14 ms", msg)
        for engine in ("unittest", "doctest", "pdb"):
            self.assertIn(engine, msg)
        self.assertIn("REFUSED", msg)
        self.assertIn("NOT probed", msg)

    # WHAT A HOOK LAUNCHED NOW WOULD PAY, the second input (task/3040).
    HOOK_PAYS = {"ok": True, "no_site": False, "pid": 4343,
                 "executable": "/probe/bin/python3"}
    HOOK_SKIPS = dict(HOOK_PAYS, no_site=True)
    EAGER = {"eager": ["unittest", "doctest", "pdb"], "site_ms": 576.0}

    def test_eager_warns_with_the_measured_cost_the_artifact_and_the_cure(self):
        res = doctor.check_startup_doors(
            probe=self.probe(**self.EAGER),
            hook_probe=lambda: dict(self.HOOK_PAYS))
        self.assertEqual([lvl for lvl, _ in res], [doctor.WARN])
        msg = res[0][1]
        self.assertIn("576 ms", msg)
        self.assertIn("unittest, doctest, pdb", msg)
        self.assertIn("usercustomize.py", msg)
        self.assertIn("/probe/user-site", msg)
        self.assertIn("sys.meta_path", msg)

    def test_eager_but_skipped_by_every_hook_is_OK_and_says_who_still_pays(self):
        """THE DESIGN'S ROW: the doctor startup door is OK on an idle box and
        on a loaded one once hooks start with -S, because the eager site stage
        is then every OTHER python start's cost and not the hook budget's."""
        res = doctor.check_startup_doors(
            probe=self.probe(**self.EAGER),
            hook_probe=lambda: dict(self.HOOK_SKIPS))
        self.assertEqual([lvl for lvl, _ in res], [doctor.OK])
        msg = res[0][1]
        self.assertIn("NO HOOK PAYS IT", msg)
        self.assertIn("/probe/bin/python3", msg)
        self.assertIn("-S", msg)
        self.assertIn("576 ms", msg)
        self.assertIn("REFUSED", msg)

    def test_eager_and_skipped_but_toothless_still_warns(self):
        res = doctor.check_startup_doors(
            probe=self.probe(refused=False, **self.EAGER),
            hook_probe=lambda: dict(self.HOOK_SKIPS))
        self.assertEqual([lvl for lvl, _ in res], [doctor.WARN])
        self.assertIn("GONE TOO", res[0][1])
        self.assertIn("Hooks skip it", res[0][1])

    def test_eager_and_paid_by_hooks_says_so_and_an_unread_hook_is_unknown(self):
        paid = doctor.check_startup_doors(
            probe=self.probe(**self.EAGER),
            hook_probe=lambda: dict(self.HOOK_PAYS))
        self.assertEqual([lvl for lvl, _ in paid], [doctor.WARN])
        self.assertIn("every hook process on this box pays", paid[0][1])
        self.assertIn("no interpreter is recorded yet", paid[0][1])

        def boom():
            raise OSError("no sh")
        unread = doctor.check_startup_doors(probe=self.probe(**self.EAGER),
                                            hook_probe=boom)
        self.assertEqual([lvl for lvl, _ in unread], [doctor.WARN])
        self.assertIn("UNKNOWN", unread[0][1])
        self.assertIn("no sh", unread[0][1])

    def test_the_hook_start_probe_launches_through_the_real_wrapper(self):  # noqa: VACUOUS_ASSERTION — the absent record after the COLD probe is read against the planted record the WARM probe then uses, on the same path, unconditionally
        """MUST-HIT for the second input: a copy of the SHIPPED wrapper starts
        a child the way a hook is started, cold (nothing recorded: the site
        stage runs) and warm (recorded: it does not). Hermetic: its own
        HELM_HOME and a PATH python3 of its own."""
        import shlex
        import shutil
        from helm import hooks
        with tempfile.TemporaryDirectory() as tmp:
            pathbin = os.path.join(tmp, "pathbin")
            os.makedirs(pathbin)
            shim = os.path.join(pathbin, "python3")
            with open(shim, "w") as fh:
                fh.write("#!/bin/sh\nexec %s \"$@\"\n" % shlex.quote(sys.executable))
            os.chmod(shim, 0o755)
            env = {"HELM_HOME": os.path.join(tmp, "home"),
                   "PATH": pathbin + os.pathsep + os.environ.get("PATH", "")}
            wrapper = os.path.join(os.path.dirname(os.path.dirname(
                os.path.abspath(doctor.__file__))), "bin", hooks.HOOK_WRAPPER)
            with mock.patch.dict(os.environ, env):
                cold = doctor._hook_start_probe(wrapper=wrapper)
                self.assertTrue(cold.get("ok"), cold)
                self.assertIs(cold["no_site"], False)
                self.assertNotEqual(cold["pid"], os.getpid())
                record = os.path.join(tmp, "home", "_global", ".state",
                                      "hook-interp")
                self.assertFalse(os.path.exists(record),
                                 "the probe repaired what it only reports")
                os.makedirs(os.path.dirname(record))
                with open(record, "w") as fh:
                    fh.write("%s\t%s\n" % (shim, sys.executable))
                warm = doctor._hook_start_probe(wrapper=wrapper)
            self.assertTrue(warm.get("ok"), warm)
            self.assertIs(warm["no_site"], True)
            self.assertEqual(os.path.realpath(warm["executable"]),
                             os.path.realpath(sys.executable))
            shutil.rmtree(os.path.join(tmp, "home"))

    def test_lazy_but_disarmed_never_reads_as_good(self):
        # AN ABSENT GUARD MEASURES EXACTLY LIKE A LAZY ONE on the import axis,
        # so the refusal is the half that separates them.
        res = doctor.check_startup_doors(probe=self.probe(refused=False))
        self.assertEqual([lvl for lvl, _ in res], [doctor.WARN])
        msg = res[0][1]
        self.assertIn("ABSENT or DISARMED", msg)
        self.assertIn("usercustomize.py", msg)
        self.assertIn("/probe/user-site", msg)

    def test_unmeasurable_is_unknown_not_ok(self):
        res = doctor.check_startup_doors(
            probe=lambda: {"ok": False, "why": "FileNotFoundError: no interpreter"})
        self.assertEqual([lvl for lvl, _ in res], [doctor.WARN])
        self.assertIn("UNKNOWN", res[0][1])
        self.assertIn("no interpreter", res[0][1])

    def test_a_raising_probe_is_unknown_not_a_crash(self):
        def boom():
            raise OSError("spawn refused")
        res = doctor.check_startup_doors(probe=boom)
        self.assertEqual([lvl for lvl, _ in res], [doctor.WARN])
        self.assertIn("UNKNOWN", res[0][1])
        self.assertIn("spawn refused", res[0][1])

    def test_probe_really_starts_a_child_interpreter(self):
        """MUST-HIT. Every arm above is satisfied by a dict, so exactly one arm
        runs the production probe and proves a second process answered it. The
        box's own guard state is NOT asserted: a build node and the hub give
        different true answers, and a rung that only passes where the artifact
        exists would be an arm about this machine."""
        row = doctor._startup_probe()
        self.assertTrue(row.get("ok"), row)
        self.assertIsInstance(row["pid"], int)
        self.assertNotEqual(row["pid"], os.getpid())
        self.assertGreater(row["pid"], 0)
        self.assertIsInstance(row["site_ms"], float)
        self.assertTrue(row["user_site"])
        self.assertIsInstance(row["refused"], bool)
        self.assertIsInstance(row["eager"], list)

    def test_probe_sees_an_eager_site_stage_that_this_arm_seeds(self):
        """CONTROL FOR THE PROBE ITSELF. The arm above proves a second process
        answered; this one proves the answer TRACKS THE WORLD. A site file
        planted on the child's path imports the engines at startup, and the
        probe must come back naming them — a probe reporting on nothing would
        still say `eager: []` here, and every other arm in this class would
        stay green while it did."""
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "usercustomize.py"), "w") as fh:
                fh.write("import unittest, doctest\n")
            with mock.patch.dict(os.environ, {"PYTHONPATH": tmp}):
                seeded = doctor._startup_probe(repeats=1)
        self.assertTrue(seeded.get("ok"), seeded)
        self.assertIn("unittest", seeded["eager"])
        self.assertIn("doctest", seeded["eager"])
        res = doctor.check_startup_doors(
            probe=lambda: seeded, hook_probe=lambda: dict(self.HOOK_PAYS))
        self.assertEqual([lvl for lvl, _ in res], [doctor.WARN])
        self.assertIn("usercustomize.py", res[0][1])

    def test_probe_scrubs_the_exemptions_the_guard_itself_honours(self):
        """The guard installs NO door when a routed-run variable is present, so
        a probe that inherited one would report an armed guard as absent — the
        exact state this rung exists to report."""
        seen = {}

        def fake_run(argv, **kw):
            seen.update(kw.get("env") or {})
            raise OSError("one spawn is enough")
        with mock.patch.dict(os.environ, {"FAB_ID": "x",
                                          "HELM_GATE_SUITE_CAP": "9",
                                          "FAB_ALLOW_LOCAL_SUITE": "1"}):
            row = doctor._startup_probe(run=fake_run)
        # the refusal is REPORTED, not merely falsy: an unmeasured world that
        # cannot say why is the shape this rung must never emit
        self.assertFalse(row["ok"])
        self.assertIn("one spawn is enough", row["why"])
        self.assertNotIn("FAB_ID", seen)
        self.assertNotIn("HELM_GATE_SUITE_CAP", seen)
        self.assertNotIn("FAB_ALLOW_LOCAL_SUITE", seen)
        self.assertIn("PATH", seen)      # control: a real env, not an empty one

    def test_registered_in_the_report_loop(self):
        # POSITIVE CONTROL: delete the rung and `helm doctor` resolves a name
        # that is no longer there, so this arm fails rather than passing quietly.
        self.assertIn("check_startup_doors", doctor.CHECKS)
        self.assertTrue(callable(getattr(doctor, "check_startup_doors", None)))


class ColdGenesisTest(ProjectionRegistryBase):
    """Council item 2: a valid ZERO-STATE machine must pass the advertised
    health gate right after the documented first command.

    THE COMMIT THAT INTRODUCED THE FIX SAID "the test below exercises that
    exact path" AND SHIPPED NO TEST. It also cut the tail off
    check_projection_registry — the summary row and its `return out` ended up
    inside `_record_genesis` — so the function returned None and `helm doctor`
    died with `TypeError: NoneType is not iterable` before reaching any of
    this. The subprocess control below is what makes that undetectable-by-
    reading class detectable: it runs the REAL verb and reads its REAL exit.
    """

    def test_check_projection_registry_RETURNS_ITS_FINDINGS(self):
        """The floor. A check that returns None is not a check that passed —
        every caller iterates it, and `helm doctor` composes them all."""
        got = doctor.check_projection_registry()
        self.assertIsInstance(got, list)
        self.assertTrue(all(isinstance(r, tuple) and len(r) == 2 for r in got),
                        got)

    def _orphan(self):
        """One exact producer-declared genesis whose source never existed."""
        home.scaffold_global()
        pk.atomic_write(os.path.join(self.helm_home, "probe.json"), "{}")
        return self._row(
            sources=(os.path.join(self.tmp.name, "no-such"),),
            genesis={"json": {}, "volatile_strings": ()},
        )

    def test_the_genesis_exemption_and_its_CONTROL_on_one_fixture(self):
        """Both directions on ONE planted orphan, in one test, because they are
        the same claim read twice.

        The first draft of this called check_projection_registry() against the
        BASE estate, which plants no orphan at all — so the control could never
        have gone red for the reason it names. Measuring the wrong fixture is
        how a control becomes decoration, and it is the third time tonight."""
        row = self._orphan()
        with mock.patch.object(doctor, "_is_genesis", return_value=False):
            seen = [m for m in levels(self._check(row), doctor.FAIL)
                    if "ORPHANED" in m]
        with mock.patch.object(doctor, "_is_genesis", return_value=True):
            silenced = [m for m in levels(self._check(row), doctor.FAIL)
                        if "ORPHANED" in m]
        self.assertTrue(seen, "the fixture never reaches ORPHANED, so the "
                              "genesis half below is vacuous")
        self.assertEqual(silenced, [])

    def test_an_UNREADABLE_register_is_not_genesis(self):
        """Genesis SILENCES a FAIL. When the register cannot be read,
        genesis is FALSE — an estate whose state is unknown must surface
        orphaned projections, never hide them behind a guess."""
        with mock.patch("helm.seat.registered_seats",
                        side_effect=OSError("EIO")):
            self.assertFalse(doctor._is_genesis())

    def test_genesis_holds_while_no_seat_has_ever_run(self):
        """Genesis determined by register: an estate with zero registered
        seats has never had the chance to create harness stores, so absent
        sources are unrealized projections, not orphans. Once seats exist,
        genesis lifts permanently — no stamp to race with."""
        with mock.patch("helm.seat.registered_seats",
                        return_value=([], False)):
            self.assertTrue(doctor._is_genesis())
        with mock.patch("helm.seat.registered_seats",
                        return_value=(["codex"], False)):
            self.assertFalse(doctor._is_genesis())


class TrunkAuthorityCheckTest(DoctorBase):
    """The rung that names a repository whose trunk authority is UNDECLARED.

    THE FEATURE IT WATCHES DEGRADES IN TWO OPPOSITE DIRECTIONS AND SAYS THE
    WORD UNDECLARED IN NEITHER — a review retip stamps `unverified` and
    PROCEEDS, a non-FF build retip refuses with the LEGACY sentence about rows
    written before the binding. One reads as a shrug and the other as ordinary
    history, which is how the helm repository itself ran a day without the
    declaration its own landed predicate requires (task/1510)."""

    def _repo(self, name, declare=None):
        path = os.path.join(self.tmp.name, name)
        os.makedirs(path)
        run = lambda *a: subprocess.run(["git", "-C", path] + list(a),
                                        capture_output=True, text=True)
        subprocess.run(["git", "init", "-q", path], capture_output=True)
        run("config", "user.email", "t@example.com")
        run("config", "user.name", "t")
        with open(os.path.join(path, "f"), "w") as f:
            f.write("x\n")
        run("add", "f"); run("commit", "-qm", "c")
        if declare:
            run("config", "helm.trunkRef", declare)
        return os.path.join(path, ".git")

    def _repo_ids(self, *gitdirs):
        """The accessor's shape: (repositories, unavailable reason).

        IT MOVED OFF `rows()` AND EVERY ARM HERE HAD DOUBLED THAT NAME. The
        rung harvested ten strings through the full carriage/landing
        projection — one git spawn per ledger ROW to decide a status it never
        reads. `dispatches.repo_ids()` answers the identity question alone, so
        these arms patch the door the rung now opens; patching `rows` would
        leave them green about a call the rung no longer makes."""
        return list(gitdirs), None

    def test_an_UNDECLARED_repo_is_NAMED_and_a_DECLARED_one_is_NOT(self):
        """BOTH DIRECTIONS THROUGH ONE CALL. Either assertion alone passes if
        the rung answers the same way for every repository — which is exactly
        what a rung built on a predicate that cannot say no would do."""
        branch = "main"
        declared = self._repo("declared", declare="refs/heads/" + branch)
        subprocess.run(["git", "-C", os.path.dirname(declared),
                        "branch", "-M", branch], capture_output=True)
        bare = self._repo("undeclared")
        with mock.patch.object(dispatches, "repo_ids",
                               return_value=self._repo_ids(declared, bare)):
            out = doctor.check_trunk_authority()
        msgs = [m for _l, m in out]
        named = [m for m in msgs if "UNDECLARED" in m and bare in m]
        self.assertEqual(len(named), 1, msgs)
        self.assertIn("helm.trunkRef", named[0],
                      "the warning must carry the key to set")
        self.assertFalse([m for m in msgs if "UNDECLARED" in m and declared in m],
                         "a DECLARED repository must not be named: %r" % msgs)

    def test_an_EMPTY_population_is_UNKNOWN_never_a_CLEAN_BILL(self):  # noqa: VACUOUS_ASSERTION — the flagged absence is the no-OK assertion; the unconditional positive is the UNKNOWN warning asserted present on the same call
        """A RUNG THAT REPORTS ALL-CLEAR OVER A SET IT FAILED TO BUILD IS THE
        VACUITY THIS WHOLE CLASS IS ABOUT. An empty ledger, or one naming only
        repositories that no longer exist, means the rung could not look —
        never that it looked and found nothing wrong."""
        gone = os.path.join(self.tmp.name, "never-existed", ".git")
        for population in (self._repo_ids(), self._repo_ids(gone)):
            with self.subTest(named=len(population[0])):
                with mock.patch.object(dispatches, "repo_ids",
                                       return_value=population):
                    out = doctor.check_trunk_authority()
                self.assertTrue(any("UNKNOWN" in m for _l, m in out), out)
                self.assertFalse([l for l, _m in out if l == doctor.OK],
                                 "an unbuildable population may not report OK")

    def test_an_UNREADABLE_ledger_is_UNKNOWN_not_an_empty_population(self):  # noqa: VACUOUS_ASSERTION — a refusal arm; its unconditional positive is the two-repo arm above, which returns real rows through the same accessor
        """The ledger failing to read and the ledger being empty are different
        facts, and only one of them is about the repositories."""
        with mock.patch.object(dispatches, "repo_ids",
                               side_effect=OSError("ledger gone")):
            out = doctor.check_trunk_authority()
        self.assertTrue(any("UNKNOWN" in m for _l, m in out), out)
        self.assertTrue(any("OSError" in m for _l, m in out),
                        "the warning must name WHY it could not look: %r" % out)

    def test_a_ledger_that_REPORTED_itself_unreadable_is_UNKNOWN_too(self):  # noqa: VACUOUS_ASSERTION — the flagged absence is the no-OK assertion; the unconditional positive is the two-repo control taken first, in this method, through the same patched accessor
        """TWO SHAPES OF UNREADABLE AND THE SECOND ONE IS NEW HERE. `rows()`
        is `snapshot()[0]`: it THREW AWAY the unavailable reason, so the only
        way this rung could learn the ledger was unreadable was an exception.
        `repo_ids()` returns the reason `snapshot` already computed, and a
        rung that ignored it would read a blind ledger as an empty world and
        go on to say something about a population it never built.

        THE POSITIVE CONTROL IS TAKEN FIRST AND THROUGH THE SAME ACCESSOR: a
        real repository, really named, really reported — so the refusal below
        is about the reason field and not about a rung that answers UNKNOWN to
        everything."""
        live = self._repo("reported-live")          # undeclared: always NAMED
        with mock.patch.object(dispatches, "repo_ids",
                               return_value=self._repo_ids(live)):
            seen = doctor.check_trunk_authority()
        self.assertTrue([m for _l, m in seen if live in m],
                        "control: a readable population names its repository")
        with mock.patch.object(dispatches, "repo_ids",
                               return_value=(None, "PermissionError: ledger")):
            out = doctor.check_trunk_authority()
        self.assertTrue(any("UNKNOWN" in m for _l, m in out), out)
        self.assertTrue(any("PermissionError" in m for _l, m in out),
                        "the warning must carry the reason the ledger read "
                        "already knew: %r" % out)
        self.assertFalse([l for l, _m in out if l == doctor.OK],
                         "a ledger that could not be read may not report OK")


class DoctorReadsShareOneMemoScopeTest(DoctorBase):
    """`helm doctor` asked the same question of the same repository thousands
    of times in one pass, because the memo it already carried was inert.

    `projscope.memo` CACHES NOTHING OUTSIDE A SCOPE — that is its safety
    contract, not an accident — and `cmd_doctor` opened none, so every
    memoised read in every check computed. Measured on the live fleet before
    this class existed: 11,097 subprocess spawns in one pass, of which 1,793
    were a distinct (cwd, argv); 84% of the work was a question the SAME
    process had already answered. One scope over the CHECKS loop took it to
    2,396 with the distinct count UNCHANGED and the 86-row report
    byte-identical.

    AND IT IS A COHERENCE FIX BEFORE IT IS A SPEED ONE, which is why the
    scope goes here rather than inside the slowest check. A doctor pass
    publishes ONE tally; two identical reads forty seconds apart could
    answer differently, and the report would then carry two instants under
    one line."""

    def _run(self, argv, checks, ensure=None):
        """Run cmd_doctor with `checks` as the whole CHECKS loop."""
        names = tuple("probe_%d" % i for i in range(len(checks)))
        patches = dict(zip(names, checks))
        with mock.patch.object(doctor, "CHECKS", names), \
                mock.patch.dict(doctor.__dict__, patches, clear=False), \
                mock.patch.object(doctor, "ensure_credentials",
                                  ensure or (lambda: [])):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = doctor.cmd_doctor(argv)
        return rc, buf.getvalue()

    def test_the_CHECKS_run_inside_a_scope_and_the_WRITE_mode_runs_outside(self):
        """ONE METHOD, BOTH POLARITIES, ONE OBSERVABLE. `projscope.active()`
        read from inside the CHECKS loop and from inside `--ensure`'s
        credential backup-and-heal, on the same pass.

        THE WRITE HALF IS THE CONSTRAINT THIS WHOLE CHANGE LIVES UNDER, not a
        nicety: projscope's law is that no write path may ever be answered
        from an older question, and `--ensure` is the only thing `helm
        doctor` mutates. A scope hoisted over the whole verb body — the
        obvious edit — would have put cred backup and heal inside it, and an
        arm that only checked the read half would have called that correct."""
        seen = {}

        def _check():
            seen["check"] = projscope.active()
            return [(doctor.OK, "probe")]

        def _ensure():
            seen["ensure"] = projscope.active()
            return [(doctor.OK, "ensure probe")]

        rc, out = self._run(["--ensure"], (_check,), ensure=_ensure)
        self.assertEqual(rc, 0, out)
        self.assertEqual(seen, {"check": True, "ensure": False},
                         "reads memoised, writes never: %r" % (seen,))
        # THE UNCONDITIONAL POSITIVE CONTROL ON THE EXACT OBSERVABLE, inline
        # rather than behind the helper: `active()` DOES read True when a
        # scope is genuinely open, so the False below is about this verb's
        # boundary and not about a predicate that has stopped answering.
        with projscope.scope():
            self.assertTrue(projscope.active())
        self.assertFalse(projscope.active(),
                         "the scope must not outlive the pass that opened it")

    def test_two_checks_asking_ONE_question_compute_it_ONCE(self):
        """A SCOPE BEING OPEN IS NOT THE SAME CLAIM AS THE CACHE SPANNING THE
        LOOP, and only the second one is the cure. `projscope.active()` would
        read True for a scope opened and closed around each individual check —
        which caches nothing across them and would leave the 84% exactly where
        it was. So this counts COMPUTES of one key asked by two different
        checks.

        THE UNCONDITIONAL POSITIVE CONTROL IS THE SAME TWO CHECKS CALLED
        DIRECTLY, first, with no scope: they compute TWICE. Without it, a
        `memo` that had stopped calling `compute` at all would satisfy the
        assertion below."""
        computed = []

        def _ask():
            return projscope.memo(("doctor-probe-key",),
                                  lambda: computed.append(1) or len(computed))

        def _one():
            return [(doctor.OK, "one=%d" % _ask())]

        def _two():
            return [(doctor.OK, "two=%d" % _ask())]

        _one(), _two()
        self.assertEqual(len(computed), 2,
                         "control: outside a scope the memo is inert by "
                         "contract, so two asks are two computes")
        del computed[:]
        rc, out = self._run([], (_one, _two))
        self.assertEqual(rc, 0, out)
        self.assertEqual(len(computed), 1,
                         "one pass, one instant: the second check must be "
                         "answered from the first check's read")
        self.assertIn("one=1", out)
        self.assertIn("two=1", out)

    def test_a_FAILING_check_still_drops_the_scope_and_still_reports(self):
        """THE EXIT PATH IS WHERE A HOISTED SCOPE LEAKS. `scope.__exit__`
        clears on the way out however the body ends, but a `with` written
        around a generator expression that is consumed lazily elsewhere would
        not — and the symptom would be a cache surviving into the NEXT pass,
        where it is exactly the stale-answer defect projscope exists to
        prevent. A check that RAISES is the cheapest way to prove the block is
        a real one."""
        def _boom():
            raise RuntimeError("check exploded")

        with self.assertRaises(RuntimeError):
            self._run([], (_boom,))
        # THE UNCONDITIONAL POSITIVE CONTROL ON THE EXACT OBSERVABLE, inline
        # rather than behind the helper: `active()` DOES read True when a
        # scope is genuinely open, so the False below is about this verb's
        # boundary and not about a predicate that has stopped answering.
        with projscope.scope():
            self.assertTrue(projscope.active())
        self.assertFalse(projscope.active(),
                         "a raising check must not leave a scope open")
        # AND THE ORDINARY PASS STILL PRINTS ITS ROWS through the same block,
        # so the assertion above is about the exit and not about a verb that
        # has stopped running its checks.
        rc, out = self._run([], (lambda: [(doctor.FAIL, "probe fail")],))
        self.assertEqual(rc, 1, out)
        self.assertIn("probe fail", out)


class IntentActualCheckTest(DoctorBase):
    """The intended-vs-actual census rung (the half-configured-is-invisible
    class): nothing compares declared intent to observed wiring, so a fleet
    at less than its paid capacity looks healthy until it fails.

    The rung reads DECLARED intent from an operator-authored intent.json and
    OBSERVED wiring from the pool's own machinery. THREE STATES, never two:
    undeclared is one quiet OK line (the must-miss — a rung that screams
    about undeclared intent is muted within a week), matched is OK, under is
    WARN carrying the observed side so a reader can disagree with the label."""

    def _pool(self, records):
        """Fabricate the codex pool dir with EXACTLY the given pooled records.
        Clears first: a prior test's (or subTest's) files must never leak into
        this pool, or a later assertion reads a fleet that was never planted."""
        from helm import codexhomes
        auth = codexhomes.pool_dir()
        os.makedirs(auth, exist_ok=True)
        for stale in os.listdir(auth):
            if stale.endswith(".json"):
                os.unlink(os.path.join(auth, stale))
        for i, rec in enumerate(records):
            path = os.path.join(auth, "codex-%d.json" % i)
            if rec is None:                      # an unparseable pool file
                with open(path, "w") as fh:
                    fh.write("not json{")
                continue
            with open(path, "w") as fh:
                json.dump(rec, fh)
        return auth

    def _intent(self, accounts):
        from helm import codexhomes
        path = os.path.join(os.path.dirname(codexhomes.pool_dir()),
                            "intent.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            json.dump({"accounts": accounts}, fh)
        return path

    @staticmethod
    def _cred(email, plan, disabled=False):
        # Shape mirrors the pooled flat record codex_pooled reads. The plan
        # tier rides a REAL (unsigned) JWT in access_token, because _identity
        # decodes claims from the token's middle segment — a literal nested
        # dict is not a token and the tier would silently read None. No
        # `expired` key: a healthy record's expiry is a FUTURE timestamp and
        # the rung reads only that field, so the fixtures set it explicitly
        # where it matters.
        import base64 as _b64
        claims = _b64.urlsafe_b64encode(json.dumps(
            {"email": email,
             "https://api.openai.com/auth": {"chatgpt_plan_type": plan}}
        ).encode()).decode().rstrip("=")
        return {"type": "codex", "email": email, "account_id": "acct-" + email,
                "disabled": disabled,
                "access_token": "hdr.%s.sig" % claims}

    def test_UNDER_intent_FIRES_and_names_the_observed_side(self):
        """THE MUST-HIT. Declared 2 ultra + 1 team, wired 1 ultra + 1 team:
        the rung must WARN and name the tier that is short, the observed
        counts, and never the tier that is satisfied."""
        self._pool([self._cred("a@x.example", "pro"),
                    self._cred("b@y.example", "team")])
        self._intent({"ultra": 2, "team": 1})
        out = doctor.check_intent_actual()
        warns = levels(out, doctor.WARN)
        self.assertEqual(len(warns), 1, out)
        self.assertIn("ultra: 1 wired of 2 declared", warns[0], warns[0])
        self.assertNotIn("team: 1 wired of 1", warns[0],
                         "a satisfied tier must not read as short: %r" % warns[0])

    def test_matched_intent_is_OK_and_UNDECLARED_is_the_must_miss(self):  # noqa: VACUOUS_ASSERTION — the must-miss is the no-WARN assertion on the undeclared call; its unconditional positive is the same call's OK line, and the matched arm beside it proves the rung can produce a different verdict at all
        """THE MUST-MISS, both halves. Wired exactly what is declared: OK and
        silent. No intent file at all: ONE QUIET OK LINE, never a WARN — a
        rung that cries wolf about an unarmed family is muted within a week,
        which is the same harm as the silence it exists to cure."""
        self._pool([self._cred("a@x.example", "pro")])
        self._intent({"ultra": 1})
        out = doctor.check_intent_actual()
        self.assertFalse(levels(out, doctor.WARN),
                         "matched intent must not warn: %r" % out)
        self.assertTrue(any("meet declared intent" in m for _l, m in out), out)
        # UNDECLARED: delete the intent file, keep the pool. One OK, no WARN.
        from helm import codexhomes
        os.unlink(os.path.join(os.path.dirname(codexhomes.pool_dir()),
                               "intent.json"))
        out = doctor.check_intent_actual()
        self.assertFalse(levels(out, doctor.WARN),
                         "UNDECLARED intent must never warn: %r" % out)
        oks = levels(out, doctor.OK)
        self.assertEqual(len(oks), 1, out)
        self.assertIn("no codex intent declared", oks[0], oks[0])

    def test_an_unparseable_pool_file_is_named_harm_not_silent(self):
        """A wired-but-dead slot occupies the pool and the proxy cannot draw
        on it; even with intent met it is harm and must be named, not passed."""
        self._pool([self._cred("a@x.example", "pro"), None])
        self._intent({"ultra": 1})
        out = doctor.check_intent_actual()
        warns = levels(out, doctor.WARN)
        self.assertEqual(len(warns), 1, out)
        self.assertIn("unparseable", warns[0], warns[0])

    def test_one_account_in_two_files_counts_ONCE_not_twice(self):
        """THE DOUBLE-COUNT. codex_pool re-pools the same account across
        refreshes, and a stale copy can sit beside the live one; counting
        FILES would read one credential as two and report capacity the proxy
        does not have. The count is by account_id."""
        cred = self._cred("a@x.example", "pro")
        dup = dict(cred)
        self._pool([cred, dup, self._cred("b@y.example", "team")])
        # control: the pool really does hold two files for one account
        from helm import codexhomes
        files = [r for r in codexhomes.codex_pooled()
                 if r.get("account_id") == "acct-a@x.example"]
        self.assertEqual(len(files), 2, "the fixture must actually double-pool")
        self._intent({"ultra": 1, "team": 1})
        out = doctor.check_intent_actual()
        self.assertFalse(levels(out, doctor.WARN),
                         "one account in two files must not warn: %r" % out)
        self.assertTrue(any("ultra=1" in m for _l, m in out), out)

    def test_a_surplus_is_named_not_hidden_in_the_OK(self):
        """MORE wired than declared is a finding too — a stale intent file or
        an account pooled that should not be — and it must not fall silently
        into the OK line."""
        self._pool([self._cred("a@x.example", "pro"),
                    self._cred("b@y.example", "team")])
        self._intent({"ultra": 1})           # team wired, no team declared
        out = doctor.check_intent_actual()
        warns = levels(out, doctor.WARN)
        self.assertEqual(len(warns), 1, out)
        self.assertIn("EXCEED", warns[0], warns[0])
        self.assertIn("team", warns[0], warns[0])

    def test_an_EXPIRED_credential_counts_OFF_not_live(self):
        """An expired access token is not live wiring — the proxy 401s on it
        until refreshed — so it must not satisfy the intent count, and it is
        named as re-enable, not re-login."""
        cred = self._cred("a@x.example", "pro")
        cred["expired"] = "2020-01-01T00:00:00+00:00"   # a PAST expiry
        self._pool([cred])
        self._intent({"ultra": 1})
        out = doctor.check_intent_actual()
        warns = levels(out, doctor.WARN)
        self.assertEqual(len(warns), 1, out)
        self.assertIn("expired", warns[0], warns[0])

    def test_a_FUTURE_expiry_counts_LIVE_not_off(self):  # noqa: VACUOUS_ASSERTION — the must-not-warn absence's unconditional positive is the fixture's healthy credential asserted through the same path, and the "meet declared intent" OK on the same call
        """EXPIRY IS A TIME, NOT A FLAG — the P0 the first cure shipped. A
        HEALTHY credential carries its access token's RFC3339 expiry, which is
        in the future and truthy; treating it as a boolean marks every normal
        account OFF and the fleet reads zero live. This arm drives exactly the
        real pooled shape."""
        cred = self._cred("a@x.example", "pro")
        cred["expired"] = "2099-01-01T00:00:00+00:00"  # a FUTURE expiry = healthy
        self._pool([cred])
        self._intent({"ultra": 1})
        out = doctor.check_intent_actual()
        self.assertFalse(levels(out, doctor.WARN),
                         "a future expiry must read healthy, not off: %r" % out)
        self.assertTrue(any("meet declared intent" in m for _l, m in out), out)

    def test_one_account_satisfies_ONLY_ONE_tier_declaration(self):
        """The dedupe is GLOBAL, not per-tier: the same account_id wired under
        two tier spellings must not fill two tier declarations with one
        credential."""
        cred = self._cred("a@x.example", "pro")
        self._pool([cred])
        self._intent({"ultra": 1, "team": 1})   # only one account, two tiers wanted
        out = doctor.check_intent_actual()
        warns = levels(out, doctor.WARN)
        self.assertEqual(len(warns), 1, out)
        self.assertIn("team: 0 wired of 1 declared", warns[0], warns[0])

    def test_a_declared_ZERO_tier_shows_its_surplus(self):
        """Declared-zero is a declaration, not an omission: the operator who
        says a tier should hold NOTHING means it, and anything wired there is
        a surplus the want>0 guard would hide."""
        self._pool([self._cred("b@y.example", "team")])
        self._intent({"ultra": 0, "team": 0})
        out = doctor.check_intent_actual()
        warns = levels(out, doctor.WARN)
        self.assertEqual(len(warns), 1, out)
        self.assertIn("team: 1 wired against 0 declared", warns[0], warns[0])

    def test_a_surplus_still_names_the_dead_files(self):
        """The surplus branch returns before the dead-file check, so a
        wired-but-dead slot could hide behind the excess. It is harm
        everywhere and must surface on every WARN path."""
        self._pool([self._cred("a@x.example", "pro"),
                    self._cred("b@y.example", "team"), None])
        self._intent({"ultra": 1})           # team surplus + one dead file
        out = doctor.check_intent_actual()
        warns = levels(out, doctor.WARN)
        self.assertEqual(len(warns), 1, out)
        self.assertIn("EXCEED", warns[0], warns[0])
        self.assertIn("unparseable", warns[0], warns[0])

    def test_a_disabled_account_is_NAMED_not_misprescribed_login(self):
        """A wired account that is OFF exists; the remedy is refresh or
        re-pool, and prescribing a fresh login is how a pooled account gets a
        duplicate credential."""
        cred = self._cred("a@x.example", "pro", disabled=True)
        self._pool([cred])
        self._intent({"ultra": 1})
        out = doctor.check_intent_actual()
        warns = levels(out, doctor.WARN)
        self.assertEqual(len(warns), 1, out)
        self.assertIn("refresh or re-pool", warns[0], warns[0])

    def test_a_ZULU_expiry_counts_LIVE_not_unparseable(self):  # noqa: VACUOUS_ASSERTION — the must-not-warn absence's unconditional positive is the fixture's Z-suffixed healthy credential asserted live through the same path, and the "meet declared intent" OK on the same call
        """THE PY3.9/3.10 TRAP. datetime.fromisoformat REJECTS the canonical
        `Z` suffix before 3.11, and helm supports 3.9 — so a bare call reads
        a real Zulu expiry as unparseable, and an unparseable-as-not-evidence
        rule then counts a PAST credential live. The cure normalises the Z."""
        cred = self._cred("a@x.example", "pro")
        cred["expired"] = "2099-01-01T00:00:00Z"       # canonical Zulu, healthy
        self._pool([cred])
        self._intent({"ultra": 1})
        out = doctor.check_intent_actual()
        self.assertFalse(levels(out, doctor.WARN),
                         "a Z-suffixed future expiry must read healthy: %r" % out)
        self.assertTrue(any("meet declared intent" in m for _l, m in out), out)
        # and the same Z at a PAST time must still count OFF, not vanish
        cred2 = self._cred("b@y.example", "pro")
        cred2["expired"] = "2020-01-01T00:00:00Z"
        self._pool([cred2])
        out = doctor.check_intent_actual()
        self.assertTrue(any("expired" in m for _l, m in out), out)

    def test_a_malformed_expiry_counts_off_but_an_absent_one_stays_live(self):
        """PRESENT-BUT-UNREADABLE is off; ABSENT is not evidence. A value that
        is THERE but cannot be parsed is the proxy recording something it
        could not vouch for — counting it live is the overstated-capacity
        failure this rung exists to catch. A record with NO expired key at
        all (what the layer emits when it has no claim) is not evidence of
        expiry and stays live."""
        bad = self._cred("a@x.example", "pro")
        bad["expired"] = "not a timestamp"          # present, unreadable
        self._pool([bad])
        self._intent({"ultra": 1})
        out = doctor.check_intent_actual()
        self.assertTrue(any("expired" in m for _l, m in out),
                        "a present-but-unparseable expiry must count off: %r" % out)

    def test_a_non_string_expiry_counts_off_not_live(self):
        """A truthy NON-STRING expiry (True, a number) is not a timestamp the
        layer mints, and must not count the account live. The isinstance(str)
        gate alone lets it fall through to expired=False — the exact hole
        where a malformed value reads as healthy."""
        for badval in (True, 12345):
            with self.subTest(badval=badval):
                cred = self._cred("a@x.example", "pro")
                cred["expired"] = badval
                self._pool([cred])
                self._intent({"ultra": 1})
                out = doctor.check_intent_actual()
                self.assertTrue(
                    any("expired" in m for _l, m in out),
                    "a non-string expiry %r must count off, not live: %r"
                    % (badval, out))

    def test_every_account_off_is_a_MEASURED_state_not_UNKNOWN(self):
        """ALL-DISABLED IS A FACT, NOT AN UNKNOWN. The files parse, so the
        rung knows exactly what is wired (everything, off) — calling that
        UNKNOWN hides the one thing the operator most needs named. Only an
        unparseable pool is unknown."""
        creds = [self._cred("a@x.example", "pro", disabled=True),
                 self._cred("b@y.example", "team", disabled=True)]
        self._pool(creds)
        self._intent({"ultra": 1})
        out = doctor.check_intent_actual()
        warns = levels(out, doctor.WARN)
        self.assertEqual(len(warns), 1, out)
        self.assertIn("EVERY codex account is OFF", warns[0], warns[0])
        self.assertNotIn("UNKNOWN", warns[0],
                         "an all-off pool is a measured state, not UNKNOWN: %r"
                         % warns[0])
        good = self._cred("b@y.example", "pro")          # no expired key at all
        self._pool([good])
        out = doctor.check_intent_actual()
        self.assertFalse(levels(out, doctor.WARN),
                         "an absent expiry must stay live: %r" % out)

    def test_two_records_with_the_same_account_id_count_once(self):
        """The dedupe still works within one namespace: two records spelling
        the SAME account_id are one credential, refreshed or not."""
        one = self._cred("a@x.example", "pro")
        dup = dict(one)
        self._pool([one, dup])
        from helm import codexhomes
        rows = [r for r in codexhomes.codex_pooled()
                if r.get("account_id") == "acct-a@x.example"]
        self.assertEqual(len(rows), 2, "the fixture must double-pool one id")
        self._intent({"ultra": 1})
        out = doctor.check_intent_actual()
        self.assertFalse(levels(out, doctor.WARN),
                         "two records with one account_id must count once: %r" % out)

    def test_casefolded_duplicates_count_once(self):
        """An account is not case-sensitive: the same email in two cases is
        one credential, not two."""
        self._pool([self._cred("a@x.example", "pro"),
                    self._cred("A@X.EXAMPLE", "pro")])
        self._intent({"ultra": 1})
        out = doctor.check_intent_actual()
        self.assertFalse(levels(out, doctor.WARN),
                         "case variants of one account must not warn: %r" % out)
        self.assertTrue(any("ultra=1" in m for _l, m in out), out)

    def test_an_all_dead_pool_is_UNKNOWN_never_a_measured_zero(self):
        """EVERY file unparseable reads identically to an empty pool through
        the count — and an unreadable pool collapsing to a measured zero is
        the exact failure this rung refuses. UNKNOWN, with the reason."""
        self._pool([None, None])                      # two unparseable files
        self._intent({"ultra": 1})
        out = doctor.check_intent_actual()
        warns = levels(out, doctor.WARN)
        self.assertEqual(len(warns), 1, out)
        self.assertIn("UNKNOWN", warns[0], warns[0])
        self.assertNotIn("wired of", warns[0],
                         "an all-dead pool must not invent a count: %r" % warns[0])

    def test_an_exact_match_still_names_off_accounts(self):
        """EXACT MATCH IS NOT ALL-CLEAR: an account needing refresh is harm
        even at the declared count, and the OK line must not swallow it."""
        cred = self._cred("a@x.example", "pro", disabled=True)
        self._pool([cred, self._cred("b@y.example", "pro")])
        self._intent({"ultra": 1})                  # one live, one off, want one
        out = doctor.check_intent_actual()
        warns = levels(out, doctor.WARN)
        self.assertEqual(len(warns), 1, out)
        self.assertIn("off account", warns[0], warns[0])

    def test_an_exact_match_also_names_the_dead_files(self):
        """The exact-match off branch dropped dead_note, so a wired-but-dead
        slot could hide behind a met count. Dead files are harm on every
        WARN path, exact match included."""
        cred = self._cred("a@x.example", "pro", disabled=True)
        self._pool([cred, self._cred("b@y.example", "pro"), None])
        self._intent({"ultra": 1})
        out = doctor.check_intent_actual()
        warns = levels(out, doctor.WARN)
        self.assertEqual(len(warns), 1, out)
        self.assertIn("off account", warns[0], warns[0])
        self.assertIn("unparseable", warns[0], warns[0])

    def test_one_account_counts_once_across_id_and_email_aliases(self):
        """A refreshed record can carry account_id while an older copy of the
        SAME account carries only the legacy email; the dedupe must treat both
        spellings as one credential or it counts the account twice."""
        by_id = self._cred("a@x.example", "pro")
        by_email = {k: v for k, v in by_id.items() if k != "account_id"}
        self._pool([by_id, by_email])
        # control: the fixture really does present the same account twice
        from helm import codexhomes
        rows = [r for r in codexhomes.codex_pooled()
                if r.get("email") == "a@x.example"]
        self.assertEqual(len(rows), 2, "the fixture must actually double-present")
        self._intent({"ultra": 1})
        out = doctor.check_intent_actual()
        self.assertFalse(levels(out, doctor.WARN),
                         "one account under two aliases must not warn: %r" % out)
        self.assertTrue(any("ultra=1" in m for _l, m in out), out)

    def test_a_transitive_alias_bridge_counts_once_in_BOTH_orderings(self):
        """THE TRANSITIVE BRIDGE. A legacy record spelling only the old
        email, a record spelling {account_id, old-email}, and a refreshed
        record spelling {account_id, new-email} are ONE credential in three
        files — and pairwise intersection counts it as TWO, because the
        refreshed record shares no spelling with the legacy one; the bridge
        merges their components without touching both. The dedupe must be
        the transitive closure, in either visit order. Controls: the pool
        really does hold three files for one account, and a DISTINCT second
        account beside the bridge still counts as two, not one."""
        legacy = {"type": "codex", "email": "old@x.example",
                  "access_token": self._cred("old@x.example", "pro")["access_token"]}
        bridge = self._cred("old@x.example", "pro")
        refreshed = self._cred("new@x.example", "pro")
        refreshed["account_id"] = bridge["account_id"]
        for order in ((legacy, bridge, refreshed),
                      (refreshed, bridge, legacy)):
            with self.subTest(first=list(order)[0]["email"]):
                self._pool(list(order))
                from helm import codexhomes
                rows = codexhomes.codex_pooled()
                self.assertEqual(len(rows), 3,
                                 "the fixture must actually triple-pool")
                self._intent({"ultra": 1})
                out = doctor.check_intent_actual()
                self.assertFalse(
                    levels(out, doctor.WARN),
                    "one account across a transitive bridge must count once: "
                    "%r" % out)
                self.assertTrue(any("ultra=1" in m for _l, m in out), out)
        # control: a genuinely distinct second account still counts as two
        self._pool([legacy, bridge, refreshed, self._cred("c@z.example", "pro")])
        self._intent({"ultra": 2})
        out = doctor.check_intent_actual()
        self.assertFalse(levels(out, doctor.WARN),
                         "bridge + one distinct account must be two: %r" % out)
        self.assertTrue(any("ultra=2" in m for _l, m in out), out)

    def test_an_OFF_bridge_still_unifies_the_LIVE_account(self):
        """LIVENESS FILTERS THE COUNT, NEVER THE IDENTITY GRAPH.

        THE HOSTILE INPUT is one account in three records: a live legacy record
        carrying only the old email, a DISABLED bridge carrying that email plus
        the account id, and a live refreshed record carrying the same account id
        plus a new email. If disabled records are discarded before identity
        edges are built, the bridge disappears and one live account counts as
        TWO. The disabled record must not itself count or vote on tier; it only
        preserves the identity edge that says the two live spellings are one.

        The control removes the bridge: with no evidence joining the spellings,
        the two live records honestly remain two units."""
        legacy = {"type": "codex", "email": "old@x.example",
                  "access_token": self._cred("old@x.example", "pro")["access_token"]}
        bridge = self._cred("old@x.example", "pro", disabled=True)
        refreshed = self._cred("new@x.example", "pro")
        refreshed["account_id"] = bridge["account_id"]
        import itertools
        for order in itertools.permutations((legacy, bridge, refreshed)):
            with self.subTest(order=[
                    ("off" if r.get("disabled") else "live", r.get("email"))
                    for r in order]):
                self._pool(list(order))
                self._intent({"ultra": 1})
                out = doctor.check_intent_actual()
                self.assertTrue(
                    any("meets intent (ultra=1)" in m for _l, m in out), out)
                self.assertFalse(
                    any("wired of 1 declared" in m for _l, m in out),
                    "the OFF bridge must keep one live account from "
                    "double-counting in every read order: %r" % out)
                self.assertTrue(
                    any("off account" in m for _l, m in out),
                    "identity evidence must not make the disabled row live: "
                    "%r" % out)
        self._pool([legacy, refreshed])
        self._intent({"ultra": 2})
        out = doctor.check_intent_actual()
        self.assertTrue(any("observed live: ultra=2" in m for _l, m in out), out)

    @staticmethod
    def _jwt(claims):
        """An unsigned JWT carrying these claims — what the pool's own readers
        decode. `_identity` and `translate_codex_auth` both read the MIDDLE
        segment, so a literal nested dict is not a token and every claim on it
        would silently read None."""
        import base64 as _b64
        return "hdr.%s.sig" % _b64.urlsafe_b64encode(
            json.dumps(claims).encode()).decode().rstrip("=")

    def test_the_bridge_counts_once_in_EVERY_read_ORDER_not_two(self):
        """ORDER-INDEPENDENCE, not two lucky orderings.

        THE HOSTILE INPUT IS THE BRIDGE READ LAST. The transitive arm beside
        this one reverses the two ENDPOINTS and keeps the bridging record in
        the MIDDLE, which is exactly where an incremental seen-so-far test
        happens to fire: the bridge is the second record read, so one endpoint
        is counted and the bridge marks itself the duplicate. Put the bridge
        LAST and BOTH endpoints are counted before anything links them, and
        one credential in three files reads as two.

        A dedupe whose answer depends on the order the pool dir happened to
        sort in is not a dedupe, so the property is stated over the whole
        symmetric group: EVERY permutation of one edge set yields the same
        component count. The control is the same sweep with a genuinely
        distinct fourth account, which must count TWO in all 24 orders — an
        arm that only ever asserts "one" passes on a rung that always says
        one."""
        import itertools
        legacy = {"type": "codex", "email": "old@x.example",
                  "access_token": self._cred("old@x.example", "pro")["access_token"]}
        bridge = self._cred("old@x.example", "pro")
        refreshed = self._cred("new@x.example", "pro")
        refreshed["account_id"] = bridge["account_id"]
        distinct = self._cred("c@z.example", "pro")
        # THE UNCONDITIONAL POSITIVE, on the same observable the loops read:
        # the fixture really does hold three files for one account, and the
        # rung really does emit an ultra count for them.
        from helm import codexhomes
        self._pool([legacy, refreshed, bridge])
        self._intent({"ultra": 1})
        self.assertEqual(len(codexhomes.codex_pooled()), 3,
                         "the fixture must actually triple-pool one account")
        self.assertTrue(any("ultra=1" in m for _l, m
                            in doctor.check_intent_actual()),
                        "one account in three files is ONE unit")
        for order in itertools.permutations((legacy, refreshed, bridge)):
            with self.subTest(order=[r.get("account_id", "-") for r in order]):
                self._pool(list(order))
                self._intent({"ultra": 1})
                out = doctor.check_intent_actual()
                self.assertFalse(
                    levels(out, doctor.WARN),
                    "one account across a bridge must count once in EVERY "
                    "order: %r" % out)
                self.assertTrue(any("ultra=1" in m for _l, m in out), out)
        for order in itertools.permutations((legacy, refreshed, bridge, distinct)):
            with self.subTest(control=[r.get("account_id", "-") for r in order]):
                self._pool(list(order))
                self._intent({"ultra": 2})
                out = doctor.check_intent_actual()
                self.assertFalse(
                    levels(out, doctor.WARN),
                    "a bridge plus a DISTINCT account is two in EVERY order: "
                    "%r" % out)
                self.assertTrue(any("ultra=2" in m for _l, m in out), out)

    def test_a_row_whose_identity_cannot_be_READ_is_COUNTED_not_SKIPPED(self):
        """CANNOT-DEDUPE-THEN-COUNT-IT, in the direction the contract names.

        THE HOSTILE INPUT is a parseable, enabled, unexpired codex record that
        carries NEITHER account_id NOR email — the pool's flat record is
        whatever the writer put in it, and a half-written one has no identity
        at all. `dup = not aliases` reads as "no identity means duplicate" and
        SKIPS the row, which is the one direction the contract forbids: the
        rung then reports capacity BELOW what is wired and prescribes a login
        for an account that is already there.

        Two such rows are TWO units, never merged: nothing in either row says
        they are the same credential. And the rung must NAME them, because a
        count that may be high owes its reader the rows that made it so."""
        faceless = {"type": "codex",
                    "access_token": self._cred("a@x.example", "pro")["access_token"]}
        self._pool([faceless, dict(faceless)])
        from helm import codexhomes
        rows = codexhomes.codex_pooled()
        self.assertEqual([r.get("account_id") for r in rows], [None, None],
                         "the fixture must actually present NO identity")
        self.assertEqual([r.get("email") for r in rows], [None, None], rows)
        self._intent({"ultra": 2})
        out = doctor.check_intent_actual()
        self.assertTrue(any("ultra=2" in m for _l, m in out),
                        "two identity-less rows are two counted units: %r" % out)
        self.assertFalse(any("wired of 2" in m for _l, m in out),
                         "an unidentifiable row must never be SKIPPED: %r" % out)
        self.assertTrue(any("identity" in m for _l, m in out),
                        "a count that may be high must name the rows: %r" % out)

    def test_DISTINCT_accounts_that_share_one_email_STAY_DISTINCT(self):
        """IDENTITY IS THE ACCOUNT ID, FIRST AND ALWAYS.

        THE HOSTILE INPUT IS THE ONE THE POOL WRITER MINTS. A credential whose
        id_token carries no email claim is written into the pool with a fixed
        PLACEHOLDER where the address goes, so every such account in the pool
        spells its email the same — and a union over email spellings collapses
        all of them into ONE credential, reporting a shortfall against
        accounts that are wired and live.

        The placeholder is DERIVED from the writer here, never transcribed: a
        constant copied into an arm is pinned to nothing, and the writer is
        free to respell it tomorrow. The second half proves the rule is
        ACCOUNT-ID-FIRST and not a blacklist of one string — a REAL address
        shared by two account_ids must not merge them either. The must-miss
        is the same two files under ONE account id, which must still count
        once."""
        from helm import codexhomes, seat_credentials
        import time as _time
        src = os.path.join(self.tmp.name, "auth.json")
        with open(src, "w") as fh:
            json.dump({"tokens": {
                "id_token": self._jwt({"https://api.openai.com/auth":
                                       {"chatgpt_plan_type": "pro"}}),
                "access_token": self._jwt({"exp": _time.time() + 10 ** 6}),
                "account_id": "acct-one"}}, fh)
        first, _fname, err = seat_credentials.translate_codex_auth(src)
        self.assertIsNone(err, "the fixture must come from the REAL writer")
        placeholder = first["email"]
        self.assertTrue(isinstance(placeholder, str) and placeholder,
                        "the writer must mint SOME email spelling: %r"
                        % (placeholder,))
        second = dict(first, account_id="acct-two")
        self._pool([first, second])
        rows = codexhomes.codex_pooled()          # control: one email, two ids
        self.assertEqual({r["email"] for r in rows}, {placeholder}, rows)
        self.assertEqual(len({r["account_id"] for r in rows}), 2, rows)
        self.assertEqual({r["tier"] for r in rows}, {"ultra"}, rows)
        self._intent({"ultra": 2})
        out = doctor.check_intent_actual()
        self.assertFalse(levels(out, doctor.WARN),
                         "two account_ids under one email spelling are TWO "
                         "credentials: %r" % out)
        self.assertTrue(any("ultra=2" in m for _l, m in out), out)
        self._pool([dict(first, email="shared@x.example"),
                    dict(second, email="shared@x.example")])
        out = doctor.check_intent_actual()
        self.assertTrue(any("ultra=2" in m for _l, m in out),
                        "a REAL shared address must not merge two account "
                        "ids either: %r" % out)
        self._pool([first, dict(first)])          # must-miss: ONE account
        self._intent({"ultra": 1})
        out = doctor.check_intent_actual()
        self.assertFalse(levels(out, doctor.WARN),
                         "one account in two files still counts once: %r" % out)
        self.assertTrue(any("ultra=1" in m for _l, m in out), out)

    def test_ONE_account_at_TWO_tiers_is_a_CONFLICT_not_first_file_wins(self):
        """A TIER IS A FACT ABOUT AN ACCOUNT; TWO RECORDS THAT DISAGREE HAVE
        NO ANSWER.

        THE HOSTILE INPUT is one account_id pooled twice under DIFFERENT
        plans — a stale copy from before a plan change beside the refreshed
        one. The pool is read in filename order, so crediting the first
        record's tier is FIRST-FILENAME-WINS: the census prints a tier
        breakdown nothing in the pool actually says, and the disagreement —
        the thing the operator needs to see — is thrown away silently. The
        conflict must be NAMED with both spellings and the account credited
        to NEITHER tier until it is resolved.

        The must-miss is the ABSENT tier: a record carrying no plan claim
        beside one that does is missing evidence, not contradicting evidence,
        and must not read as a conflict."""
        self._pool([self._cred("a@x.example", "pro"), self._cred("a@x.example", "team")])
        from helm import codexhomes
        rows = codexhomes.codex_pooled()
        self.assertEqual(len({r["account_id"] for r in rows}), 1,
                         "the fixture must pool ONE account twice")
        self.assertEqual({r["tier"] for r in rows}, {"ultra", "team"}, rows)
        self._intent({"ultra": 1})
        out = doctor.check_intent_actual()
        warns = levels(out, doctor.WARN)
        self.assertEqual(len(warns), 1, out)
        self.assertIn("acct-a@x.example", warns[0], warns[0])
        self.assertIn("ultra", warns[0], warns[0])
        self.assertIn("team", warns[0], warns[0])
        self.assertFalse(any("meet declared intent" in m for _l, m in out),
                         "a tier conflict must never read as all-clear: %r" % out)
        noplan = dict(self._cred("a@x.example", "pro"),
                      access_token=self._jwt({"sub": "no plan claim"}))
        self._pool([self._cred("a@x.example", "pro"), noplan])
        rows = codexhomes.codex_pooled()
        self.assertEqual({r["tier"] for r in rows}, {"ultra", None},
                         "the fixture must present one KNOWN and one ABSENT tier")
        out = doctor.check_intent_actual()
        self.assertFalse(levels(out, doctor.WARN),
                         "an ABSENT tier is not a disagreement: %r" % out)
        self.assertTrue(any("ultra=1" in m for _l, m in out), out)

    def test_OFF_accounts_beside_CORRUPT_files_are_not_ALL_UNPARSEABLE(self):
        """A MIXED POOL HAS NO SINGLE-CAUSE SENTENCE.

        THE HOSTILE INPUT is a pool holding a record that PARSED and is OFF
        beside a file that did not parse at all. Nothing is live, so the
        not-live branch fires — and the all-unparseable sentence is a lie
        about the account the rung read perfectly well, whose remedy (refresh
        or re-pool) is the one thing the operator most needs. The two harms
        must be named TOGETHER, and the unread files must leave the total
        UNKNOWN rather than claiming EVERY account is off.

        The control is the pool that really is all-unparseable, which must
        still say so — otherwise the cure is just the other single-cause
        sentence."""
        self._pool([self._cred("a@x.example", "pro", disabled=True), None])
        self._intent({"ultra": 1})
        out = doctor.check_intent_actual()
        warns = levels(out, doctor.WARN)
        self.assertEqual(len(warns), 1, out)
        self.assertNotIn("all files unparseable", warns[0],
                         "a file that PARSED refutes that sentence: %r" % warns[0])
        self.assertIn("a@x.example", warns[0], warns[0])
        self.assertIn("refresh or re-pool", warns[0], warns[0])
        self.assertIn("unparseable", warns[0], warns[0])
        self._pool([None, None])
        out = doctor.check_intent_actual()
        warns = levels(out, doctor.WARN)
        self.assertEqual(len(warns), 1, out)
        self.assertIn("all files unparseable", warns[0], warns[0])
        self.assertIn("UNKNOWN", warns[0], warns[0])

    def test_a_NON_STRING_identity_is_COUNTED_not_a_CRASH(self):
        """A POOL FILE IS JSON ON DISK AND ITS FIELDS ARE WHATEVER IS IN IT.

        THE HOSTILE INPUT is a parseable record whose `email` is a number, a
        dict, a list or a bool — `codex_pooled` passes the field straight
        through, so `email.casefold()` raises AttributeError and takes the
        WHOLE `helm doctor` run down: every rung after this one stops
        reporting because one pool file was hand-edited. The same value on the
        OFF path takes `", ".join` down with a TypeError.

        Unreadable is not absent: the row is counted (on its account id where
        it has one, on its own where it does not) and never merged with
        anything, because a spelling nobody can read matches nothing."""
        from helm import codexhomes
        # THE UNCONDITIONAL POSITIVE, on the same observable each subTest
        # reads: a READABLE identity counts through this same path.
        self._pool([self._cred("a@x.example", "pro")])
        self._intent({"ultra": 1})
        self.assertTrue(any("ultra=1" in m for _l, m
                            in doctor.check_intent_actual()),
                        "a readable identity must count through this path")
        for bad in (123, {"a": 1}, ["x"], True):
            with self.subTest(bad=bad):
                self._pool([dict(self._cred("a@x.example", "pro"), email=bad)])
                self.assertEqual([r["email"] for r in codexhomes.codex_pooled()],
                                 [bad], "the fixture must carry the raw value")
                self._intent({"ultra": 1})
                out = doctor.check_intent_actual()
                self.assertTrue(any("ultra=1" in m for _l, m in out),
                                "a non-string email must not lose the account: "
                                "%r" % out)
                self._pool([dict(self._cred("a@x.example", "pro", disabled=True),
                                 email=bad)])
                out = doctor.check_intent_actual()
                self.assertTrue(any("refresh or re-pool" in m for _l, m in out),
                                "the OFF path must survive it too: %r" % out)
                self._pool([{"type": "codex", "email": bad, "account_id": bad,
                             "access_token":
                                 self._cred("a@x.example", "pro")["access_token"]}])
                out = doctor.check_intent_actual()
                self.assertTrue(any("ultra=1" in m for _l, m in out),
                                "an all-unreadable identity is still ONE "
                                "counted row: %r" % out)

    def test_a_pool_that_cannot_be_ENUMERATED_is_UNKNOWN_never_a_ZERO(self):  # noqa: VACUOUS_ASSERTION — the flagged absence is the no-count assertion, and it must sit inside the chmod try/finally because the mode has to be restored or tearDown cannot remove the tmpdir; the unconditional positive control runs FIRST and OUTSIDE that try — the same pool READABLE, asserted to produce '0 wired of 2 declared' through the same call
        """NEGATIVE AND UNREADABLE MUST NOT SHARE A VALUE.

        THE HOSTILE INPUT is a pool dir that EXISTS and cannot be listed. A
        pool reader that swallowed that OSError would answer exactly what an
        empty pool answers, and the arithmetic downstream renders that silence
        as `0 wired of 2 declared` — a measured shortfall against a pool that
        was never read. The isdir guard cannot catch it: the dir is there. The
        pool reader enumerates once by name and RAISES, so the
        try-codex_pooled guard catches it; this arm pins the rung's ANSWER
        rather than the mechanism, which is the point: UNKNOWN, never a
        count.

        The control comes FIRST and is the same pool readable: an empty pool
        under declared intent IS a real shortfall and must keep saying so, or
        the cure has just moved the vacuity to the other side."""
        auth = self._pool([])
        self._intent({"ultra": 2})
        out = doctor.check_intent_actual()
        self.assertTrue(any("0 wired of 2 declared" in m for _l, m in out),
                        "a READABLE empty pool is a measured shortfall: %r" % out)
        os.chmod(auth, 0)
        try:
            try:
                os.listdir(auth)
            except OSError:
                pass
            else:
                self.skipTest("this user can enumerate a mode-000 dir (root?) "
                              "— the hostile fixture cannot be built here")
            out = doctor.check_intent_actual()
            warns = levels(out, doctor.WARN)
            self.assertEqual(len(warns), 1, out)
            self.assertIn("UNKNOWN", warns[0], warns[0])
            self.assertNotIn("wired of", warns[0],
                             "a pool nobody could read must not invent a "
                             "count: %r" % warns[0])
        finally:
            os.chmod(auth, 0o700)

    def test_an_UNREADABLE_pool_is_UNKNOWN_never_a_measured_shortfall(self):  # noqa: VACUOUS_ASSERTION — the flagged absence is the no-count assertion; the unconditional positives are the fixture's assertFalse(isdir) premise and the UNKNOWN-named WARN on the same call
        """A pool dir that cannot be read is not a fleet with zero wired —
        the rung must say UNKNOWN, because 'empty' and 'could not look' are
        opposite facts and only one licenses the shortfall sentence."""
        self._intent({"ultra": 2})
        # no pool dir at all: pool_dir() points at a path that was never made
        from helm import codexhomes
        self.assertFalse(os.path.isdir(codexhomes.pool_dir()),
                         "the fixture must have NO pool dir")
        out = doctor.check_intent_actual()
        warns = levels(out, doctor.WARN)
        self.assertEqual(len(warns), 1, out)
        self.assertIn("UNKNOWN", warns[0], warns[0])
        self.assertNotIn("wired of", warns[0],
                         "an unreadable pool must not invent a count: %r" % warns[0])

    def test_a_pool_that_DISAPPEARS_after_the_stat_is_UNKNOWN_not_0_of_N(self):  # noqa: VACUOUS_ASSERTION — the single absence (no `wired of` count) is surrounded by unconditional positives: the control BEFORE the injection asserts the same populated pool MEETS declared intent, the injected FileNotFoundError is asserted to have FIRED, and the answer itself is asserted to be exactly one WARN naming UNKNOWN
        """task/2480 R5 F2 — the discriminator round 3 deleted.

        The rung stats the pool dir (`os.path.isdir`) and then asks the reader
        to enumerate it. Between those two syscalls the directory can go away:
        a `helm codex unpool`, a rename, a tmpfs remount. The reader maps a
        missing directory to NO RECORDS, which is byte-identical to what an
        EMPTY directory produces — so the rung sailed past its own isdir guard
        (the dir WAS there), read zero records, and rendered them through the
        arithmetic as `0 wired of 2 declared`: UNDER, with a login
        recommendation, about a pool it never read one byte of. Its own
        contract says an input it cannot read is UNKNOWN, never zero.

        The fault is injected at THE ONE ENUMERATION the reader makes, once,
        AFTER the isdir has already passed — the shape round 3's own witness
        used. Everything else is the shipped rung over real files."""
        auth = self._pool([self._cred("a@x.example", "pro"),
                           self._cred("b@y.example", "pro")])
        self._intent({"ultra": 2})
        # THE CONTROL COMES FIRST and is the answer that must survive: the
        # same populated pool, read normally, MEETS the declared intent.
        # Blast radius: one rung call over two real pool files — it goes red
        # only if this fixture can never satisfy intent at all.
        out = doctor.check_intent_actual()
        self.assertTrue(any("meet declared intent" in m for _l, m in out), out)

        real_listdir = os.listdir
        armed = {"n": 1}

        def gone_once(path=".", *a, **kw):
            if armed["n"] and os.fspath(path) == auth:
                armed["n"] -= 1
                raise FileNotFoundError(2, "No such file or directory", path)
            return real_listdir(path, *a, **kw)

        with mock.patch("os.listdir", gone_once):
            out = doctor.check_intent_actual()
        # ASSERT THE INJECTED INPUT ARRIVED: a fixture that never fired would
        # make everything below a reading of an ordinary pass.
        self.assertEqual(armed["n"], 0, "the FileNotFoundError never fired")
        warns = levels(out, doctor.WARN)
        self.assertEqual(len(warns), 1, out)
        self.assertIn("UNKNOWN", warns[0], warns[0])
        self.assertNotIn("wired of", warns[0],
                         "a pool that vanished before the read must not "
                         "invent a count: %r" % warns[0])

    def test_the_three_pool_absences_stay_three_different_answers(self):  # noqa: VACUOUS_ASSERTION — the one absence (no `wired of` count on the absent-from-start leg) rides beside that leg's own unconditional UNKNOWN assertion, and the two legs after it assert PRESENT counts — `0 wired of 2 declared` over a genuinely empty pool and `1 wired of 2 declared` over a populated one — which is the whole point of the arm
        """The controls for the arm above, and the ones a heavy-handed cure
        breaks: declaring every empty pool UNKNOWN would take the rung from
        reporting a real shortfall to reporting nothing at all.

        Blast radius: three rung calls over the same intent file. Each leg
        asserts a PRESENT answer, so none of them can pass by absence."""
        self._intent({"ultra": 2})
        from helm import codexhomes
        auth = codexhomes.pool_dir()
        # 1. absent from the start — the isdir guard's own UNKNOWN
        if os.path.isdir(auth):
            import shutil as _sh
            _sh.rmtree(auth)
        self.assertFalse(os.path.isdir(auth))
        warns = levels(doctor.check_intent_actual(), doctor.WARN)
        self.assertEqual(len(warns), 1)
        self.assertIn("UNKNOWN", warns[0])
        self.assertNotIn("wired of", warns[0])
        # 2. genuinely empty — a MEASURED shortfall, and it must keep saying so
        self._pool([])
        self.assertTrue(any("0 wired of 2 declared" in m
                            for _l, m in doctor.check_intent_actual()))
        # 3. populated — the count is the count
        self._pool([self._cred("a@x.example", "pro")])
        self.assertTrue(any("1 wired of 2 declared" in m
                            for _l, m in doctor.check_intent_actual()))

    def test_the_rung_is_wired_into_doctor_not_just_defined(self):
        """DOGFOOD ON THE SHIPPED SURFACE, per the acceptance bar: a direct
        call proves the rung WORKS and says nothing about whether it is WIRED.
        The operator runs `helm doctor`; the rung must appear in its output."""
        self.assertIn("check_intent_actual", doctor.CHECKS,
                      "the rung is defined but doctor will never call it")
        self._pool([self._cred("a@x.example", "pro"),
                    self._cred("b@y.example", "team")])
        self._intent({"ultra": 2, "team": 1})
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            try:
                doctor.cmd_doctor([])
            except SystemExit:
                pass
        self.assertIn("intent-vs-actual", buf.getvalue(),
                      "helm doctor's output does not carry the rung")

    def _member(self, email, plan, user_id=None, account_id="ws-fake-0004"):
        """A pooled Team-workspace record: every member carries ONE account
        id, and the user id claim (when present) names the member."""
        auth = {"chatgpt_plan_type": plan}
        if user_id:
            auth["chatgpt_user_id"] = user_id
        return {"type": "codex", "email": email, "account_id": account_id,
                "disabled": False,
                "access_token": self._jwt({"email": email,
                                           "https://api.openai.com/auth": auth})}

    def test_three_TEAM_members_of_one_workspace_are_three_units(self):
        """task/2981. On a Team plan the account id names the WORKSPACE, and
        every member carries it. Keyed on that id alone, three members wired
        as three credentials counted as ONE, and the rung reported a
        shortfall against capacity that was wired and live. A member is the
        account id plus the user id (`codexresets.member_id`)."""
        members = [self._member("%s@team.example" % n, "team", "user-fake-" + n)
                   for n in ("admin", "d", "hey")]
        self._pool(members + [dict(members[1])])  # d@ in two files is ONE
        self._intent({"team": 3})
        out = doctor.check_intent_actual()
        self.assertFalse(levels(out, doctor.WARN),
                         "three members are three units: %r" % out)
        self.assertTrue(any("team=3" in m for _l, m in out), out)
        # a member with no user-id claim is its own unit by its address, and
        # one with neither is counted on its own and NAMED, never folded
        self._pool(members + [self._member("new@team.example", "team"),
                              dict(self._member("x@team.example", "team"),
                                   email="unknown")])
        self._intent({"team": 5})
        out = doctor.check_intent_actual()
        self.assertTrue(any("team=5" in m for _l, m in out), out)
        named = [m for _l, m in out if "counted on their own" in m]
        self.assertEqual(len(named), 1, out)
        self.assertIn("codex-4.json", named[0])
        self.assertNotIn("codex-3.json", named[0])
        # an unrecognised workspace plan splits by member the same way
        self._pool([self._member("%s@team.example" % n, "business",
                                 "user-fake-" + n) for n in ("admin", "d")])
        self._intent({"business": 2})
        out = doctor.check_intent_actual()
        self.assertTrue(any("business=2" in m for _l, m in out), out)
        # THE CONTROL: a personal account is ONE unit on its id, whatever its
        # files say about the user id
        self._pool([self._member("solo@personal.example", "pro",
                                 "user-fake-solo", "acct-fake-solo"),
                    self._member("solo@personal.example", "pro", None,
                                 "acct-fake-solo")])
        self._intent({"ultra": 1})
        out = doctor.check_intent_actual()
        self.assertFalse(levels(out, doctor.WARN), out)
        self.assertTrue(any("ultra=1" in m for _l, m in out), out)


if __name__ == "__main__":
    unittest.main()



class ResumeStateCheckTest(DoctorBase):
    """task/2530: an unreadable resume state refuses every compaction resume
    and every deaf-in-effect nudge on the host, so doctor names it. A missing
    store and a readable one are quiet."""

    def test_an_unreadable_resume_state_is_one_named_FAIL(self):
        from helm import resumeturn
        path = resumeturn.state_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("{not json\n")
        self.assertIn("check_resume_state", doctor.CHECKS)
        rows = doctor.check_resume_state()
        fails = levels(rows, doctor.FAIL)
        self.assertEqual(len(fails), 1, rows)
        self.assertIn("UNREADABLE", fails[0])
        self.assertIn(path, fails[0])

    def test_a_resume_state_that_is_a_fifo_is_a_FAIL_and_not_a_hang(self):
        """task/2530 r1 P2: `open()` on a FIFO with no writer blocks,
        so this rung hung `helm doctor` instead of naming the store. The call
        runs on a thread with a deadline, and a blocked reader is released by
        opening the FIFO's write end, so a RED arm cannot wedge the suite."""
        import stat
        import threading
        from helm import resumeturn
        path = resumeturn.state_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        os.mkfifo(path)
        self.assertTrue(stat.S_ISFIFO(os.stat(path).st_mode))
        box = {}
        t = threading.Thread(
            target=lambda: box.update(rows=doctor.check_resume_state()),
            daemon=True)
        t.start()
        t.join(5)
        if t.is_alive():
            try:
                os.close(os.open(path, os.O_WRONLY | os.O_NONBLOCK))
            except OSError:
                pass
            t.join(5)
            self.fail("check_resume_state blocked for 5s on the FIFO at %s"
                      % path)
        fails = levels(box["rows"], doctor.FAIL)
        self.assertEqual(len(fails), 1, box["rows"])
        self.assertIn(path, fails[0])
        self.assertIn("not a regular file", fails[0])

    def test_an_absent_or_readable_resume_state_is_quiet(self):
        from helm import resumeturn
        absent = doctor.check_resume_state()
        pk.write_json(resumeturn.state_path(),
                      {"codex": {"at": [], "mode": "spawned"}})
        readable = doctor.check_resume_state()
        for rows in (absent, readable):
            self.assertEqual(levels(rows, doctor.FAIL)
                             + levels(rows, doctor.WARN), [], rows)
            self.assertEqual(len(levels(rows, doctor.OK)), 1, rows)
        self.assertIn("1 key", levels(readable, doctor.OK)[0])


class GuardCensusTest(DoctorBase):
    """The git leak guard is fleet-wide or it is nothing.

    check_work_guard judges the repo doctor runs from and stays silent for a
    repo that shows no sign of the rail — which is precisely the repo that
    never had a guard. A registry project is helm-run by declaration, so
    there the silence is the finding: a project repo with no pre-commit leg
    is one `git add` away from carrying a customer export it cannot
    un-carry. check_projects folds the census to one WARN line per class,
    naming the projects and the install command.
    """

    def setUp(self):
        super().setUp()
        import subprocess
        self.subprocess = subprocess
        self.gitenv = dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null",
                           GIT_CONFIG_SYSTEM="/dev/null")
        home.scaffold_global()
        self.repo = os.path.join(self.tmp.name, "repos", "proj")
        os.makedirs(self.repo)
        for cmd in (("git", "init", "-q", "-b", "main"),
                    ("git", "config", "user.email", "t@example.com"),
                    ("git", "config", "user.name", "t")):
            self.sh(*cmd)
        with open(os.path.join(self.repo, "README"), "w") as f:
            f.write("seed\n")
        self.sh("git", "add", "-A")
        self.sh("git", "commit", "-q", "-m", "seed")
        home.scaffold_project("proj")
        pk.write_json(home.registry_path(), {"version": 1, "projects": {
            "proj": {"name": "proj", "path": self.repo, "kind": "git",
                     "status": "active", "sessions": {}, "memory_dir": None}}})

    def sh(self, *args):
        p = self.subprocess.run(list(args), cwd=self.repo, capture_output=True,
                                text=True, timeout=60, env=self.gitenv)
        self.assertEqual(p.returncode, 0, p.stderr)
        return p.stdout

    def test_a_registry_repo_with_no_guard_is_one_named_WARN(self):
        warns = levels(doctor.check_projects(), doctor.WARN)
        hits = [m for m in warns if "NO git leak guard" in m]
        self.assertEqual(len(hits), 1, warns)
        self.assertIn("proj", hits[0])
        self.assertIn("--profile leak", hits[0])

    def test_installing_the_leak_profile_clears_the_census(self):
        from helm.work import _guard
        rc, lines = _guard.install_guard(self.repo, apply=True, profile="leak")
        self.assertEqual(rc, 0, lines)
        warns = levels(doctor.check_projects(), doctor.WARN)
        self.assertFalse(any("git leak guard" in m or "git guard" in m
                             for m in warns), warns)
        self.assertTrue(any("1 of 1 healthy" in m
                            for m in levels(doctor.check_projects(), doctor.OK)))

    def test_a_stale_installed_hook_is_drift_not_unguarded(self):
        from helm.work import _guard
        _guard.install_guard(self.repo, apply=True, profile="leak")
        target = _guard.hook_path(self.repo, "pre-commit")
        with open(target, "a") as f:
            f.write("# older rules\n")
        warns = levels(doctor.check_projects(), doctor.WARN)
        self.assertTrue(any("stale, partial or unreadable git guard" in m
                            and "proj" in m for m in warns), warns)
        self.assertFalse(any("NO git leak guard" in m for m in warns), warns)

    def test_one_missing_scanner_in_a_current_rail_is_drift_not_unguarded(self):
        """stale_guard_hooks lists only NON-FRESH entries, so an all-MISSING
        read over that list is not 'nothing armed'. A rail repo with one
        scanner snapshot gone must read as drift — telling it to install the
        leak profile would strip its pre-commit rungs."""
        from helm.work import _guard
        rc, lines = _guard.install_guard(self.repo, apply=True, profile="rail")
        self.assertEqual(rc, 0, lines)
        snap = next(p for p in _guard._scanner_assets(self.repo, "rail")
                    if p.endswith("/hardcode.py"))
        os.remove(snap)
        self.assertEqual(doctor._repo_guard_state(self.repo), "drift")
        warns = levels(doctor.check_projects(), doctor.WARN)
        self.assertFalse(any("--profile leak" in m for m in warns), warns)
        self.assertTrue(any("stale, partial or unreadable" in m for m in warns),
                        warns)

    def test_a_repo_whose_hooks_dir_lacks_pre_merge_commit_is_counted(self):
        """A guard with no pre-merge-commit leg never sees a merge commit, so
        the census names that class on its own line. CONTROL first, on the
        same repo: the full install is not counted."""
        from helm.work import _guard
        rc, lines = _guard.install_guard(self.repo, apply=True, profile="leak")
        self.assertEqual(rc, 0, lines)
        self.assertEqual(doctor._repo_guard_state(self.repo), "guarded")
        warns = levels(doctor.check_projects(), doctor.WARN)
        self.assertFalse(any("NEVER SEES A MERGE COMMIT" in m for m in warns),
                         warns)
        os.remove(_guard.hook_path(self.repo, "pre-merge-commit"))
        self.assertEqual(doctor._repo_guard_state(self.repo), "merge-blind")
        warns = levels(doctor.check_projects(), doctor.WARN)
        hits = [m for m in warns if "NEVER SEES A MERGE COMMIT" in m]
        self.assertEqual(len(hits), 1, warns)
        self.assertIn("proj", hits[0])
        self.assertIn("install-guard --apply", hits[0])
        self.assertFalse(any("NO git leak guard" in m for m in warns), warns)

    def test_a_non_executable_pre_merge_commit_is_counted_merge_blind(self):
        """Git runs no hook that is not executable, so byte-identical
        pre-merge-commit at 0644 is as blind as an absent one. CONTROL first,
        on the same repo: mode 0755 reads guarded."""
        from helm.work import _guard
        rc, lines = _guard.install_guard(self.repo, apply=True, profile="leak")
        self.assertEqual(rc, 0, lines)
        hook = _guard.hook_path(self.repo, "pre-merge-commit")
        self.assertEqual(os.stat(hook).st_mode & 0o777, 0o755)
        self.assertEqual(doctor._repo_guard_state(self.repo), "guarded")
        os.chmod(hook, 0o644)
        self.assertEqual(doctor._repo_guard_state(self.repo), "merge-blind")
        warns = levels(doctor.check_projects(), doctor.WARN)
        hits = [m for m in warns if "NEVER SEES A MERGE COMMIT" in m]
        self.assertEqual(len(hits), 1, warns)
        self.assertIn("proj", hits[0])

    def test_a_plain_directory_project_is_not_a_repo_to_guard(self):
        plain = os.path.join(self.tmp.name, "repos", "plain")
        os.makedirs(plain)
        home.scaffold_project("plain")
        pk.write_json(home.registry_path(), {"version": 1, "projects": {
            "plain": {"name": "plain", "path": plain, "kind": "dir",
                      "status": "active", "sessions": {}, "memory_dir": None}}})
        warns = levels(doctor.check_projects(), doctor.WARN)
        self.assertFalse(any("guard" in m for m in warns), warns)
        self.assertIsNone(doctor._repo_guard_state(plain))
        self.assertEqual(doctor._repo_guard_state(self.repo), "unguarded")  # positive control


class KeepaliveCadenceRungTest(DoctorBase):
    """The rung that makes a stopped credential loop AUDIBLE.

    HELM_CACHE_DIR already points at a temp dir here, so keepalive's log is the
    fixture's; HOME is redirected per-arm so the unit path is one this suite
    owns. Nothing installs a real timer and nothing grants a credential."""

    def setUp(self):
        super().setUp()
        from helm import keepalive
        self.keepalive = keepalive
        self.fakehome = os.path.join(self.tmp.name, "fake-home")
        os.makedirs(self.fakehome, exist_ok=True)
        self.homep = mock.patch.dict(os.environ, {"HOME": self.fakehome})
        self.homep.start()
        self.addCleanup(self.homep.stop)
        self.crontab = mock.patch.object(keepalive, "hand_crontab", lambda: [])
        self.crontab.start()
        self.addCleanup(self.crontab.stop)

    def _log(self, *rows):
        path = self.keepalive._log_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")

    def _install(self):
        spath, service, tpath, timer = self.keepalive._timer_units()
        os.makedirs(os.path.dirname(tpath), exist_ok=True)
        with open(tpath, "w") as f:
            f.write(timer)
        return tpath

    def _stamp(self, ago_s):
        return time.strftime("%Y-%m-%dT%H:%M:%S%z",
                             time.localtime(time.time() - ago_s))

    def test_a_bare_box_reports_the_missing_timer_and_the_ensure_command(self):
        warns = levels(doctor.check_keepalive_cadence(), doctor.WARN)
        self.assertTrue(any("NOT installed" in m for m in warns), warns)
        self.assertTrue(any("helm keepalive --ensure-timer" in m for m in warns), warns)
        # and, on the same bare box, the log has nothing to report either
        self.assertTrue(any("no refresh is recorded" in m for m in warns), warns)

    def test_an_installed_timer_with_a_recent_grant_is_the_ok_line(self):  # noqa: VACUOUS_ASSERTION — the empty WARN list is the claim (a healthy loop must not nag), and it has two unconditional positive assertions beside it on the same rows plus a must-hit control in the sibling arms, which drive the SAME rung to emit WARNs for a missing timer, an old grant and a crontab line
        self._install()
        self._log({"ts": self._stamp(1800), "by": "keepalive-cron",
                   "home": "a", "action": "refreshed"})
        rows = doctor.check_keepalive_cadence()
        self.assertEqual(levels(rows, doctor.WARN), [])
        ok = levels(rows, doctor.OK)
        self.assertTrue(any("keepalive-cron" in m for m in ok), ok)
        self.assertTrue(any("last grant 0h ago" in m for m in ok), ok)

    def test_a_grant_older_than_the_token_lifetime_is_a_finding(self):
        """The bar is the TOKEN, not the configured period: a loop that last
        granted longer ago than an access token lives has already let a home
        go dead whatever its unit says."""
        self._install()
        self._log({"ts": self._stamp(30 * 3600), "by": "keepalive-cron",
                   "home": "a", "action": "refreshed"})
        warns = levels(doctor.check_keepalive_cadence(), doctor.WARN)
        self.assertTrue(any("PAST THE TOKEN LIFETIME" in m for m in warns), warns)
        self.assertTrue(any("helm-keepalive.timer" in m for m in warns), warns)

    def test_a_hand_crontab_line_is_reported_and_named_superseded(self):
        line = "23 * * * * HELM_CHAT_NAME=keepalive-cron helm keepalive --apply"
        self._install()
        self._log({"ts": self._stamp(600), "by": "hand", "home": "a",
                   "action": "refreshed"})
        with mock.patch.object(self.keepalive, "hand_crontab", lambda: [line]):
            warns = levels(doctor.check_keepalive_cadence(), doctor.WARN)
        self.assertTrue(any(line in m and "superseded" in m for m in warns), warns)
        # the control on the same rung: with no crontab line there is no such
        # row, so the finding above is the reader and not a constant
        self.assertEqual(
            [m for m in levels(doctor.check_keepalive_cadence(), doctor.WARN)
             if "crontab" in m], [])

    def test_an_unparseable_stamp_is_reported_as_unknown_not_as_an_age(self):
        self._install()
        self._log({"ts": "whenever", "by": "hand", "home": "a",
                   "action": "refreshed"})
        rows = doctor.check_keepalive_cadence()
        self.assertTrue(any("age is unknown" in m for _l, m in rows), rows)
        self.assertIsNone(doctor._keepalive_age_s("whenever"))
        # positive control on the same reader
        self.assertEqual(
            doctor._keepalive_age_s("1970-01-01T00:00:00+0000", now=3600), 3600)

    def test_the_rung_is_registered(self):
        self.assertIn("check_keepalive_cadence", doctor.CHECKS)
        self.assertIn("check_stale_bot", doctor.CHECKS)   # a populated table



class DeployedArtifactCanonTest(unittest.TestCase):
    """The rung for an artifact that RUNS from outside any checkout.

    THE DEFECT IT WATCHES IS NOT "A FILE CHANGED" — it is that the fleet gates
    through scripts living in a directory with no git in it, so the artifact
    that dispatches a gate cannot be diffed, reviewed or rolled back, and
    nothing noticed when it stopped matching its source. The cost is forward,
    not retroactive: a receipt binds the run rather than the binary, so what
    is lost is the ability to review, reproduce or roll back, and any way to
    say when the difference began.

    EVERY ARM HERE DRIVES A REAL GIT REPOSITORY AND A REAL DEPLOY DIRECTORY
    through the real vcs seam. A stubbed backend would test the arithmetic of
    a diff this rung does not own, and would stay green if `ls-tree` or `show`
    ever answered a shape it cannot parse — which is the only interesting way
    for it to break.
    """

    TREE = "fab/bin"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = os.path.join(self.tmp.name, "repo")
        self.dest = os.path.join(self.tmp.name, "deploy")
        os.makedirs(os.path.join(self.repo, self.TREE))
        os.makedirs(self.dest)
        subprocess.run(["git", "init", "-q", self.repo], capture_output=True)
        self._git("config", "user.email", "t@example.com")
        self._git("config", "user.name", "t")

    def _git(self, *args):
        return subprocess.run(["git", "-C", self.repo] + list(args),
                              capture_output=True, text=True)

    def _commit(self, name, body, mode=0o755):
        path = os.path.join(self.repo, self.TREE, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(body)
        os.chmod(path, mode)
        self._git("add", "-A")
        self._git("commit", "-qm", "c")

    def _deploy(self, name, body, mode=0o755):
        path = os.path.join(self.dest, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(body)
        os.chmod(path, mode)

    def _rows(self):
        return doctor.check_deployed_artifact_canon(
            deploy_dir=self.dest, project="proj", repo=self.repo)

    def _said(self, rows, level):
        return " ".join(m for lvl, m in rows if lvl == level)

    def test_each_DIRECTION_is_NAMED_and_they_are_NOT_the_same_word(self):
        """THE WHOLE POINT OF THE RUNG IN ONE CALL. A check that says only
        "drift" sends every reader to the same wrong place: AHEAD wants a
        land, BEHIND wants a deploy, DIVERGENT wants a human. Asserting one
        direction alone would stay green on a rung that hard-codes that word,
        so all three are driven here, and the OK arm below proves the same
        rung can also say nothing at all."""
        self._commit("ahead", "a\nb\n")
        self._commit("behind", "a\nb\nc\nd\n")
        self._commit("both", "a\nb\nc\n")
        self._deploy("ahead", "a\nb\nc\n")          # the deploy gained a line
        self._deploy("behind", "a\nb\n")            # the tree gained two
        self._deploy("both", "a\nZ\nc\n")           # one line swapped
        said = self._said(self._rows(), doctor.FAIL)
        self.assertIn("ahead is AHEAD", said)
        self.assertIn("behind is BEHIND", said)
        self.assertIn("both is DIVERGENT", said)
        self.assertIn("+1/-0", said)
        self.assertIn("+0/-2", said)
        self.assertIn("+1/-1", said)

    def test_the_drifted_ARTIFACT_and_BOTH_digests_are_named(self):
        """A direction with no artifact and no digests is still unactionable:
        the reader cannot tell WHICH file to open or WHICH two objects to
        compare. Both digests are computed here rather than written as
        literals, so the arm cannot pass on a rung that prints the canon
        digest twice."""
        self._commit("fab-gate", "one\n")
        self._deploy("fab-gate", "one\ntwo\n")
        rows = self._rows()
        self.assertEqual([lvl for lvl, _ in rows], [doctor.FAIL])
        said = self._said(rows, doctor.FAIL)
        self.assertTrue(said)
        live = hashlib.sha256(b"one\ntwo\n").hexdigest()[:16]
        canon = hashlib.sha256(b"one\n").hexdigest()[:16]
        # Both digests are computed here rather than written as literals, and
        # both are asserted PRESENT: a rung that printed the canon digest in
        # both slots would satisfy neither, so distinctness needs no separate
        # assertion of its own.
        self.assertIn("fab-gate", said)
        self.assertIn("live %s" % live, said)
        self.assertIn("canon %s" % canon, said)

    def test_a_MATCHING_deploy_reports_OK_and_spends_no_FAIL(self):
        """THE UNCONDITIONAL POSITIVE CONTROL for every absence asserted in
        this class. The arms around it claim the rung says AHEAD, MODE or
        EXTRA only when those hold; that claim is vacuous unless this same
        rung, on this same observable, can be driven to a clean answer."""
        self._commit("fab-gate", "same\n")
        self._deploy("fab-gate", "same\n")
        rows = self._rows()
        self.assertEqual([lvl for lvl, _ in rows], [doctor.OK])
        self.assertIn("match", rows[0][1])

    def test_MODE_only_drift_WARNS_and_does_NOT_spend_the_FAIL(self):
        """The FAIL sentence claims a receipt cannot be reproduced. Equal bytes
        ARE reproducible, so a mode difference must not borrow that word — but
        it is still the deploy editing what it installs, so it must not vanish
        either."""
        self._commit("tool", "x\n", mode=0o644)
        self._deploy("tool", "x\n", mode=0o755)
        rows = self._rows()
        self.assertEqual([lvl for lvl, _ in rows], [doctor.WARN])
        self.assertIn("MODE", rows[0][1])
        self.assertIn("tool", rows[0][1])

    def test_an_EXTRA_deployed_file_cannot_hide_behind_a_clean_run(self):
        """A rung that enumerates only the COMMITTED names is blind to a file
        existing solely in the deploy directory — the one shape where
        everything reviewed matches and something unreviewed still runs."""
        self._commit("fab-gate", "same\n")
        self._deploy("fab-gate", "same\n")
        self._deploy("fab-gate.prev", "whatever\n")
        said = self._said(self._rows(), doctor.WARN)
        self.assertIn("fab-gate.prev", said)
        self.assertIn("no committed source", said)

    def test_a_COMMITTED_file_that_was_never_deployed_is_named(self):
        """The mirror of the extra: reviewed, and not running."""
        self._commit("fab-gate", "same\n")
        self._commit("never-shipped", "y\n")
        self._deploy("fab-gate", "same\n")
        said = self._said(self._rows(), doctor.WARN)
        self.assertIn("never-shipped", said)
        self.assertIn("not deployed", said)

    def test_NO_deploy_dir_says_NOTHING_but_an_UNREACHABLE_canon_is_LOUD(self):
        """THE TWO SILENCES ARE NOT THE SAME SILENCE, and collapsing them is
        how a rung like this dies quietly. A host with no deploy directory
        runs no deployed gate and owes no row. A host that HAS one whose canon
        cannot be resolved is gating RIGHT NOW against a source nobody can
        name, which is the finding, not the absence of one."""
        absent = os.path.join(self.tmp.name, "not-there")
        self.assertEqual(
            doctor.check_deployed_artifact_canon(deploy_dir=absent,
                                                 project="proj",
                                                 repo=self.repo), [])
        self._deploy("fab-gate", "running\n")
        rows = doctor.check_deployed_artifact_canon(
            deploy_dir=self.dest, project="proj",
            repo=os.path.join(self.tmp.name, "no-such-checkout"))
        self.assertEqual([lvl for lvl, _ in rows], [doctor.WARN])
        self.assertIn("compared against nothing", rows[0][1])

    def test_an_EMPTY_canon_tree_compares_against_NOTHING_not_PASSES(self):
        """`ls-tree` on a tree that is not there answers with no names. Read as
        "no files to check", that is a permanent silent pass over a live
        deploy directory — the exact failure this rung replaces."""
        self._deploy("fab-gate", "running\n")
        self._git("commit", "-qm", "empty", "--allow-empty")
        rows = self._rows()
        self.assertEqual([lvl for lvl, _ in rows], [doctor.WARN])
        self.assertIn("compared against nothing", rows[0][1])

    def test_the_rung_is_REGISTERED_so_the_report_actually_runs_it(self):
        """A check absent from CHECKS is a check nobody runs. This rung exists
        because drift ran unnoticed; leaving it unregistered reproduces that
        exactly, and every arm above would still be green."""
        self.assertIn("check_deployed_artifact_canon", doctor.CHECKS)


class DriftDirectionTest(unittest.TestCase):
    """The direction verdict alone: the three real directions, the one shape
    where the honest answer is that the instrument cannot say, and the one
    that looks like a fourth and is not."""

    def test_a_dropped_trailing_newline_is_a_LINE_change_not_a_blind_spot(self):
        """WHY THERE IS NO "BYTES DIFFER BUT NO LINE DOES" ARM, pinned so the
        next reader does not add one back. It looks like a real case and
        cannot occur: `splitlines(keepends=True)` is lossless and UTF-8
        decoding is injective, so differing bytes always differ in some line.
        This arm FAILED when the rung carried that branch, which is how the
        branch was found to be unreachable rather than merely unused."""
        where, detail = doctor._drift_direction(b"a\n", b"a")
        self.assertEqual(where, "DIVERGENT")
        self.assertIn("+1/-1", detail)

    def test_a_NON_TEXT_artifact_is_refused_a_direction_and_reports_sizes(self):
        """Not every deployed artifact is a script. Guessing a direction from
        bytes that do not decode is worse than naming what could be compared,
        and this is the ONLY input for which the rung declines to answer."""
        where, detail = doctor._drift_direction(b"\xff\xfe\x00", b"\xff\x01")
        self.assertEqual(where, "UNDIFFABLE")
        self.assertIn("3 bytes", detail)
        self.assertIn("2 bytes", detail)

    def test_the_three_real_directions_are_distinct(self):
        """POSITIVE CONTROL for the two refusals above: the same function, on
        the same kind of input, does answer when it honestly can."""
        self.assertEqual(doctor._drift_direction(b"a\n", b"a\nb\n")[0], "AHEAD")
        self.assertEqual(doctor._drift_direction(b"a\nb\n", b"a\n")[0], "BEHIND")
        self.assertEqual(doctor._drift_direction(b"a\n", b"b\n")[0], "DIVERGENT")


class CredCopyStalenessRungTest(DoctorBase):
    """The rung that makes a DEAD CREDENTIAL COPY audible.

    `check_keepalive_cadence` above measures THE WRITER and reads green while a
    home whose refresh chain is spent rots, because keepalive skips that home
    forever and every other one keeps rolling forward. This rung measures the
    OUTCOME. HELM_CACHE_DIR already points at a temp dir here, so the
    observation log is the fixture's and no credential is read or written."""

    def setUp(self):
        super().setUp()
        from helm import brief
        self.path = brief.usage_history_path()
        self.assertTrue(self.path.startswith(self.tmp.name), self.path)
        os.makedirs(os.path.dirname(self.path), exist_ok=True)

    def _write(self, *rows):
        with open(self.path, "w") as fh:
            for r in rows:
                fh.write(json.dumps(r) + "\n")

    def _row(self, account, ago_s, status):
        return {"provider": "anthropic", "account": account,
                "status": status, "gauges": [],
                "probed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                           time.gmtime(time.time() - ago_s))}

    def _dead(self, account="dead@example.com", probes=4):
        rows = [self._row(account, 40 * 86400, "allowed")]
        for i in range(probes):
            rows.append(self._row(account, 3600 * (probes - i),
                                  "reauth-needed (helm's token expired 63d "
                                  "ago and home-refresh-expired; orca "
                                  "refreshes its own store, not this one)"))
        return rows

    def test_a_copy_dead_across_many_probes_is_a_finding(self):
        self._write(*self._dead())
        warns = levels(doctor.check_cred_copy_staleness(), doctor.WARN)
        self.assertTrue(any("answered NO READING" in m for m in warns), warns)
        # THE REPAIR IS THE PROBE'S OWN SENTENCE, carried whole: its
        # parenthesis is the difference between a sync and an owner re-login
        self.assertTrue(any("orca refreshes its own store" in m
                            for m in warns), warns)
        # CONTROL on the same rung: a log whose newest row reads fine emits no
        # such finding, so the WARN above is the reader and not a constant
        self._write(self._row("dead@example.com", 40 * 86400, "allowed"),
                    self._row("dead@example.com", 60, "allowed"))
        rows = doctor.check_cred_copy_staleness()
        self.assertEqual(levels(rows, doctor.WARN), [])
        self.assertTrue(any("no credential's copy has been unreadable" in m
                            for m in levels(rows, doctor.OK)), rows)

    def test_both_conjuncts_are_required(self):
        """A duration alone accuses an account nothing has probed lately,
        which is a fact about the PROBE. A single probe is one bad night."""
        self._write(self._row("a@example.com", 40 * 86400, "allowed"),
                    self._row("a@example.com", 30 * 86400, "network-error"))
        self.assertEqual(levels(doctor.check_cred_copy_staleness(),
                                doctor.WARN), [])
        # CONTROL: add the second probe and the same rung fires — so the
        # silence above is the probe-count conjunct and not an unread log
        self._write(self._row("a@example.com", 40 * 86400, "allowed"),
                    self._row("a@example.com", 30 * 86400, "network-error"),
                    self._row("a@example.com", 60, "network-error"))
        self.assertTrue(levels(doctor.check_cred_copy_staleness(),
                               doctor.WARN))

    def test_a_streak_shorter_than_the_token_lifetime_is_not_a_finding(self):
        """The bar is the TOKEN's own lifetime, and the streak is measured
        from the last READABLE reading — so a recent good read keeps two
        failures behind it below the bar."""
        self._write(self._row("a@example.com", 7200, "allowed"),
                    self._row("a@example.com", 1800, "network-error"),
                    self._row("a@example.com", 60, "network-error"))
        self.assertEqual(levels(doctor.check_cred_copy_staleness(),
                                doctor.WARN), [])
        # CONTROL: move ONLY the last good read back past the token lifetime —
        # the same two failures now sit on a streak over the bar and the rung
        # fires, so the silence above is the duration and not the row count
        self._write(self._row("a@example.com", 3 * doctor.TOKEN_LIFETIME_S,
                              "allowed"),
                    self._row("a@example.com", 1800, "network-error"),
                    self._row("a@example.com", 60, "network-error"))
        self.assertTrue(levels(doctor.check_cred_copy_staleness(),
                               doctor.WARN))

    def test_the_seats_own_home_is_named_as_proof_the_account_is_fine(self):
        """A seat serving turns on a credential its own reader calls
        unreadable is the proof that the ACCOUNT is healthy and our COPY is
        not — the exact reading that was inverted into an owner re-login."""
        from helm import accounts, burst
        self._write(*self._dead())
        masked = accounts.mask_identity("dead@example.com")
        with mock.patch.object(burst, "homed_account",
                               lambda *a, **k: ("dead@example.com", masked,
                                                None)):
            warns = levels(doctor.check_cred_copy_staleness(), doctor.WARN)
        self.assertTrue(any("THIS SEAT IS HOMED ON IT" in m for m in warns),
                        warns)
        # CONTROL: homed on a DIFFERENT credential and the same rung still
        # reports the streak, without the claim
        with mock.patch.object(burst, "homed_account",
                               lambda *a, **k: ("other@example.com",
                                                accounts.mask_identity(
                                                    "other@example.com"),
                                                None)):
            other = levels(doctor.check_cred_copy_staleness(), doctor.WARN)
        self.assertTrue(any("answered NO READING" in m for m in other), other)
        self.assertFalse(any("THIS SEAT IS HOMED ON IT" in m for m in other),
                         other)

    def test_an_unreadable_reader_reports_rather_than_raising(self):
        from helm import burnflags
        with mock.patch.object(burnflags, "unread_streaks",
                               mock.Mock(side_effect=OSError("boom"))):
            rows = doctor.check_cred_copy_staleness()
        self.assertTrue(any("cannot tell" in m for _l, m in rows), rows)
        # CONTROL: without the raise the same call answers about the log
        self._write(*self._dead())
        self.assertTrue(levels(doctor.check_cred_copy_staleness(),
                               doctor.WARN))

    def test_the_rung_is_registered(self):
        self.assertIn("check_cred_copy_staleness", doctor.CHECKS)
        self.assertIn("check_keepalive_cadence", doctor.CHECKS)



class MemoryBaseProbeTest(unittest.TestCase):
    """The memory-base variable is undocumented, so doctor proves Claude Code
    still honours it: a static name scan on every pass, and an opt-in live
    write whose result is recorded per Claude Code version."""

    NAME = b"CLAUDE_CODE_REMOTE_MEMORY_DIR"
    PROG = "claude"  # noqa: SEAT_NAME — the Claude Code program name on PATH, not a seat

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        env = mock.patch.dict(os.environ, {"HELM_HOME": os.path.join(self.root, "hh")})
        env.start()
        self.addCleanup(env.stop)
        # the caller's own home is chosen first when it qualifies; the fleet
        # shell running the suite must not decide which fixture home is used
        os.environ.pop("CLAUDE_CONFIG_DIR", None)
        self.linked = self._home("home-a", expires_in=3600)
        homes = mock.patch.object(doctor, "_memory_homes",
                                  lambda: [("home-a", self.linked)])
        homes.start()
        self.addCleanup(homes.stop)

    def _home(self, name, expires_in=None):
        """A credential home whose access token expires `expires_in` seconds
        from now (negative: already expired). None writes no credentials
        file at all. The token strings are fixture words, not tokens."""
        path = os.path.join(self.root, name)
        os.makedirs(path)
        if expires_in is not None:
            now = time.time() * 1000
            with open(os.path.join(path, ".credentials.json"), "w") as f:
                json.dump({"claudeAiOauth": {
                    "expiresAt": int(now + expires_in * 1000),
                    "refreshToken": "fixture-refresh-" + name,
                    "refreshTokenExpiresAt": int(now + 86400000)}}, f)
        return path

    def _program(self, body, name="2.1.999"):
        path = os.path.join(self.root, self.PROG, "versions", name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(body)
        return path

    def _static(self, prog, ver="2.1.999", why=None):
        with mock.patch.object(doctor, "claude_program",
                               lambda **k: (prog, ver, why)):
            return doctor.check_memory_base_honoured()

    # -- static --------------------------------------------------------------

    def test_a_program_that_contains_the_name_is_ok(self):
        rows = self._static(self._program(b"\x7fELF..." + self.NAME + b"..."))
        self.assertEqual([l for l, _m in rows], [doctor.OK], rows)
        self.assertIn("2.1.999", rows[0][1])
        self.assertIn("has not run on 2.1.999", rows[0][1])

    def test_a_program_without_the_name_fails_naming_it_and_the_version(self):
        rows = self._static(self._program(b"\x7fELF...CLAUDE_CODE_REMOTE_MEMORY..."))
        self.assertEqual([l for l, _m in rows], [doctor.FAIL], rows)
        self.assertIn("CLAUDE_CODE_REMOTE_MEMORY_DIR", rows[0][1])
        self.assertIn("2.1.999", rows[0][1])

    def test_an_unreadable_program_is_unknown_never_green(self):
        rows = self._static(os.path.join(self.root, self.PROG, "versions", "gone"))
        self.assertEqual([l for l, _m in rows], [doctor.WARN], rows)
        self.assertIn("UNKNOWN", rows[0][1])
        self.assertIn("cannot be read", rows[0][1])

    def test_claude_not_found_is_unknown_with_the_reason(self):
        rows = self._static(None, None, "`claude` is not on PATH")
        self.assertEqual([l for l, _m in rows], [doctor.WARN], rows)
        self.assertIn("UNKNOWN", rows[0][1])
        self.assertIn("not on PATH", rows[0][1])

    def test_no_linked_home_makes_no_row(self):
        prog = self._program(b"no name here")
        self.assertEqual([l for l, _m in self._static(prog)], [doctor.FAIL])
        with mock.patch.object(doctor, "_memory_homes", lambda: []):
            self.assertEqual(self._static(prog), [])

    def test_the_recorded_live_result_for_this_version_is_reported(self):
        prog = self._program(self.NAME)
        doctor.record_memory_probe(doctor.OK, "fine", "2.1.999")
        ok = self._static(prog)
        self.assertEqual([l for l, _m in ok], [doctor.OK], ok)
        self.assertIn("passed on 2.1.999", ok[0][1])
        # a newer version has no record, whatever an older one holds
        newer = self._static(prog, ver="2.2.0")
        self.assertIn("has not run on 2.2.0", newer[0][1])
        doctor.record_memory_probe(doctor.FAIL, "refused", "2.1.999")
        bad = self._static(prog)
        self.assertEqual([l for l, _m in bad], [doctor.FAIL], bad)
        self.assertIn("refused", bad[0][1])

    def test_unknown_is_not_recorded(self):
        doctor.record_memory_probe(doctor.OK, "fine", "2.1.1")
        doctor.record_memory_probe(doctor.WARN, "UNKNOWN", "2.1.2")
        recs, why = doctor._memory_probe_records()
        self.assertIsNone(why)
        self.assertEqual(sorted(recs), ["2.1.1"])

    def test_the_rung_is_registered(self):
        self.assertIn("check_memory_base_honoured", doctor.CHECKS)

    # -- locating the program ------------------------------------------------

    def test_a_versioned_install_names_its_own_version(self):
        prog = self._program(self.NAME, "2.1.280")
        link = os.path.join(self.root, "bin", self.PROG)
        os.makedirs(os.path.dirname(link))
        os.symlink(prog, link)
        got = doctor.claude_program(which=lambda _t: link,
                                    run=mock.Mock(side_effect=AssertionError))
        self.assertEqual(got, (os.path.realpath(prog), "2.1.280", None))

    def test_a_launcher_script_resolves_through_the_versions_dir(self):
        prog = self._program(self.NAME, "2.1.280")
        shim = os.path.join(self.root, "bin", self.PROG)
        os.makedirs(os.path.dirname(shim))
        with open(shim, "w") as f:
            f.write("#!/usr/bin/env bash\nexec real \"$@\"\n")
        run = mock.Mock(return_value=mock.Mock(stdout="2.1.280 (Claude Code)\n"))
        got = doctor.claude_program(which=lambda _t: shim, run=run,
                                    versions_dir=os.path.dirname(prog))
        self.assertEqual(got, (prog, "2.1.280", None))
        # the version it names is not installed -> no path, and why
        run.return_value = mock.Mock(stdout="2.1.281 (Claude Code)\n")
        none, ver, why = doctor.claude_program(
            which=lambda _t: shim, run=run, versions_dir=os.path.dirname(prog))
        self.assertIsNone(none)
        self.assertEqual(ver, "2.1.281")
        self.assertIn("launcher script", why)

    # -- live ----------------------------------------------------------------

    def _live(self, reply, write=None):
        seen = {}

        def run(argv, cwd=None, env=None, **_k):
            seen.update(argv=argv, cwd=cwd, env=env)
            name, nonce = re.search(r"named (\S+\.md) .*line: (\w+)\.",
                                    argv[2]).groups()
            seen.update(name=name, nonce=nonce)
            if write:
                write(env, name, nonce)
            return mock.Mock(returncode=0, stdout=json.dumps(reply(seen)),
                             stderr="")
        prog = (self._program(self.NAME), "2.1.999", None)
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "sid-x",
                                          "ANTHROPIC_BASE_URL": "http://proxy"}):
            got = doctor.probe_memory_live(run=run, program=prog)
        return got, seen

    def test_live_write_under_the_base_is_ok(self):
        def write(env, name, nonce):
            d = os.path.join(env["CLAUDE_CODE_REMOTE_MEMORY_DIR"], "projects",
                             "-slug", "memory")
            os.makedirs(d)
            with open(os.path.join(d, name), "w") as f:
                f.write(nonce + "\n")
        (level, msg, ver), seen = self._live(
            lambda s: {"total_cost_usd": 0.0257, "permission_denials": []}, write)
        self.assertEqual(level, doctor.OK, msg)
        self.assertEqual(ver, "2.1.999")
        self.assertIn("$0.0257", msg)
        env = seen["env"]
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], self.linked)
        self.assertTrue(env["CLAUDE_CODE_REMOTE_MEMORY_DIR"].endswith(
            os.path.join("base", ".claude")))
        self.assertNotIn("CLAUDE_CODE_SESSION_ID", env)
        self.assertNotIn("ANTHROPIC_BASE_URL", env)
        self.assertIn("haiku", seen["argv"])
        self.assertNotIn("autoMemoryDirectory", " ".join(seen["argv"]))
        # the scratch tree is gone after the run
        self.assertTrue(env["CLAUDE_CODE_REMOTE_MEMORY_DIR"])
        self.assertFalse(os.path.exists(env["CLAUDE_CODE_REMOTE_MEMORY_DIR"]))

    def test_a_refused_live_write_fails(self):
        def reply(s):
            return {"total_cost_usd": 0.02, "permission_denials": [
                {"tool_name": "Write",
                 "tool_input": {"file_path": "/home/x/projects/s/memory/" + s["name"]}}]}
        (level, msg, _v), _s = self._live(reply)
        self.assertEqual(level, doctor.FAIL, msg)
        self.assertIn("was refused", msg)
        self.assertIn("CLAUDE_CODE_REMOTE_MEMORY_DIR", msg)

    def test_no_write_at_all_is_unknown_not_fail(self):
        (level, msg, _v), _s = self._live(
            lambda s: {"permission_denials": [], "result": "I cannot"})
        self.assertEqual(level, doctor.WARN, msg)
        self.assertIn("UNKNOWN", msg)

    def test_live_with_claude_missing_is_unknown(self):  # noqa: VACUOUS_ASSERTION — the level and message assertions prove the missing-claude branch ran; the not-called assertion is the contract that it spends no model call
        run = mock.Mock(side_effect=AssertionError("must not spawn"))
        level, msg, _v = doctor.probe_memory_live(
            run=run, program=(None, None, "`claude` is not on PATH"))
        self.assertEqual(level, doctor.WARN, msg)
        self.assertIn("UNKNOWN", msg)
        self.assertIn("not on PATH", msg)
        run.assert_not_called()

    def test_live_does_not_fire_without_a_linked_home(self):  # noqa: VACUOUS_ASSERTION — the level and message assertions prove the no-home branch ran; the not-called assertion is the contract that it spends no model call
        run = mock.Mock(side_effect=AssertionError("must not spawn"))
        with mock.patch.object(doctor, "_memory_homes", lambda: []):
            level, msg, _v = doctor.probe_memory_live(
                run=run, program=(self._program(self.NAME), "2.1.999", None))
        self.assertEqual(level, doctor.OK, msg)
        self.assertIn("nothing to probe", msg)
        run.assert_not_called()

    # -- which home the live probe borrows -----------------------------------

    def _choose(self, homes, config_dir=None):
        """Run the live probe over `homes` with a subprocess that records the
        home it was handed and writes nothing. -> (level, msg, home or None)."""
        seen = {}

        def run(argv, cwd=None, env=None, **_k):
            seen["home"] = env["CLAUDE_CONFIG_DIR"]
            return mock.Mock(returncode=0, stderr="", stdout=json.dumps(
                {"permission_denials": [], "result": "no"}))
        extra = {"CLAUDE_CONFIG_DIR": config_dir} if config_dir else {}
        with mock.patch.object(doctor, "_memory_homes", lambda: homes), \
                mock.patch.dict(os.environ, extra):
            level, msg, _v = doctor.probe_memory_live(
                run=run, program=(self._program(self.NAME), "2.1.999", None))
        return level, msg, seen.get("home")

    def test_an_expired_first_home_is_skipped_for_a_valid_one(self):
        stale = self._home("a-stale", expires_in=-3600)
        fresh = self._home("b-fresh", expires_in=3600)
        level, msg, used = self._choose([("a-stale", stale), ("b-fresh", fresh)])
        self.assertEqual(used, fresh, msg)
        self.assertIn("home b-fresh", msg)

    # THE RESERVED HOMES ARE CONFIGURED under the helm home, never named in
    # the code: a home's label is its owner's login folded, so a label in
    # the source is an address in the source.
    RESERVED = "b-reserved"
    WHY = "reserved for a lead seat"

    def _reserve(self, table):
        """Write the helm home's reserved-homes file: a dict as JSON, a str
        as raw bytes (to plant an unreadable one)."""
        path = os.path.join(os.environ["HELM_HOME"], "_global",
                            "probe-reserved-homes.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(table if isinstance(table, str) else json.dumps(table))
        return path

    def _probe_only(self, homes):
        """The live probe over `homes` with a run that must never spawn."""
        run = mock.Mock(side_effect=AssertionError("must not spawn"))
        with mock.patch.object(doctor, "_memory_homes", lambda: homes):
            level, msg, _v = doctor.probe_memory_live(
                run=run, program=(self._program(self.NAME), "2.1.999", None))
        return level, msg, run

    def test_the_reserved_home_is_never_a_fallback_even_when_it_is_the_only_valid_one(self):  # noqa: VACUOUS_ASSERTION — the level and message assertions prove the no-qualifying-home branch ran; the not-called assertion is the contract that it spends no model call
        stale = self._home("a-stale", expires_in=-3600)
        kept = self._home(self.RESERVED, expires_in=3600)
        self._reserve({self.RESERVED: self.WHY})
        level, msg, run = self._probe_only(
            [("a-stale", stale), (self.RESERVED, kept)])
        self.assertEqual(level, doctor.WARN, msg)
        self.assertIn("UNKNOWN", msg)
        self.assertIn("%s: %s" % (self.RESERVED, self.WHY), msg)
        self.assertIn("a-stale", msg)
        self.assertIn("expired", msg)
        run.assert_not_called()

    def test_with_no_reserved_homes_configured_every_valid_home_qualifies(self):
        """The control for the arm above: the same home, with nothing
        configured, is the one the probe runs on."""
        stale = self._home("a-stale", expires_in=-3600)
        kept = self._home(self.RESERVED, expires_in=3600)
        _l, msg, used = self._choose([("a-stale", stale),
                                      (self.RESERVED, kept)])
        self.assertEqual(used, kept, msg)
        self.assertIn("home %s" % self.RESERVED, msg)

    def test_an_unreadable_reserved_homes_file_borrows_no_home(self):  # noqa: VACUOUS_ASSERTION — the level and message assertions prove the refusal branch ran; the not-called assertion is the contract that it spends no model call
        """A file that does not read cannot say which homes are reserved, so
        every home that is not the caller's is refused, and the refusal names
        the file."""
        kept = self._home(self.RESERVED, expires_in=3600)
        path = self._reserve("{not json")
        level, msg, run = self._probe_only([(self.RESERVED, kept)])
        self.assertEqual(level, doctor.WARN, msg)
        self.assertIn("UNKNOWN", msg)
        self.assertIn(path, msg)
        run.assert_not_called()

    def test_the_callers_own_home_is_used_even_when_it_is_reserved(self):
        kept = self._home(self.RESERVED, expires_in=3600)
        other = self._home("a-fresh", expires_in=3600)
        self._reserve({self.RESERVED: self.WHY})
        _l, msg, used = self._choose(
            [("a-fresh", other), (self.RESERVED, kept)], config_dir=kept)
        self.assertEqual(used, kept, msg)
        self.assertIn("home %s" % self.RESERVED, msg)

    def test_every_outcome_names_the_home_it_used(self):  # noqa: VACUOUS_ASSERTION — the level-set assertion is the positive control: OK, FAIL and UNKNOWN each ran before every message is checked for the home
        def write(env, name, nonce):
            d = os.path.join(env["CLAUDE_CODE_REMOTE_MEMORY_DIR"], "projects",
                             "-slug", "memory")
            os.makedirs(d)
            with open(os.path.join(d, name), "w") as f:
                f.write(nonce + "\n")

        def refused(s):
            return {"permission_denials": [{"tool_input": {
                "file_path": "/x/memory/" + s["name"]}}]}
        outcomes = [
            self._live(lambda s: {"permission_denials": []}, write)[0],
            self._live(refused)[0],
            self._live(lambda s: {"permission_denials": [], "result": "no"})[0],
            doctor.probe_memory_live(
                run=mock.Mock(return_value=mock.Mock(
                    returncode=1, stdout="not json", stderr="Failed to authenticate")),
                program=(self._program(self.NAME), "2.1.999", None)),
            doctor.probe_memory_live(
                run=mock.Mock(side_effect=subprocess.TimeoutExpired(self.PROG, 1)),
                program=(self._program(self.NAME), "2.1.999", None)),
            doctor.probe_memory_live(
                run=mock.Mock(side_effect=AssertionError("must not spawn")),
                program=(None, None, "`claude` is not on PATH")),
        ]
        levels = sorted({l for l, _m, _v in outcomes})
        self.assertEqual(levels, sorted({doctor.OK, doctor.FAIL, doctor.WARN}))
        for level, msg, _v in outcomes:
            self.assertIn("home home-a", msg, (level, msg))

    def test_no_qualifying_home_spawns_nothing_and_names_each_skip(self):  # noqa: VACUOUS_ASSERTION — the level and message assertions prove the no-qualifying-home branch ran; the not-called assertion is the contract that it spends no model call
        stale = self._home("a-stale", expires_in=-60)
        bare = self._home("b-bare")
        run = mock.Mock(side_effect=AssertionError("must not spawn"))
        with mock.patch.object(doctor, "_memory_homes",
                               lambda: [("a-stale", stale), ("b-bare", bare)]):
            level, msg, _v = doctor.probe_memory_live(
                run=run, program=(self._program(self.NAME), "2.1.999", None))
        self.assertEqual(level, doctor.WARN, msg)
        self.assertIn("UNKNOWN", msg)
        self.assertIn("a-stale", msg)
        self.assertIn("expired", msg)
        self.assertIn("b-bare", msg)
        self.assertIn("no .credentials.json", msg)
        run.assert_not_called()

    def test_the_flag_runs_the_live_probe_and_records_it(self):  # noqa: VACUOUS_ASSERTION — the same mock is asserted called once with the flag before the control asserts it is not called without it
        probe = mock.Mock(return_value=(doctor.OK, "live memory probe: fine", "2.1.999"))
        with mock.patch.object(doctor, "probe_memory_live", probe), \
                mock.patch.object(doctor, "CHECKS", ()), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            rc = doctor.cmd_doctor(["--probe-memory"])
        self.assertEqual(rc, 0)
        self.assertIn("live memory probe: fine", out.getvalue())
        self.assertIn("2.1.999", doctor._memory_probe_records()[0])
        probe.assert_called_once_with()
        # CONTROL: without the flag nothing spends a model call
        probe.reset_mock()
        with mock.patch.object(doctor, "probe_memory_live", probe), \
                mock.patch.object(doctor, "CHECKS", ()), \
                contextlib.redirect_stdout(io.StringIO()):
            doctor.cmd_doctor([])
        probe.assert_not_called()
