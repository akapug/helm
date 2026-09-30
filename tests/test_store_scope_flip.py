#!/usr/bin/env python3
"""`helm store confirm` must not silently change which project an entry is ABOUT.

THE DEFECT (task/3520, the owner's own incident). An entry with no recorded
project gets its project DERIVED (`helm/store/load.py entry_scope`): a row in
the helm-global root derives FLEET unless its statement line alone names a helm
ARTIFACT (`task/3363`, a lane, a row id, a `helm/` path), in which case it
derives helm. Fleet entries fire for every seat; helm entries fire only for
helm seats. The owner revised a fleet entry whose new wording named
"task/3363"; `confirm` landed the statement, the derivation flipped
fleet -> helm with nothing said, and every seat that is not helm stopped
receiving it.

THE LAW EACH ARM TESTS. A confirm that changes an entry's statement (a staged
`revise`, or `--edit`) must not change `entry_project(entry)` unless the caller
passes `--rescope`:

1. such a confirm must not change the entry's project without `--rescope`;
2. an UNRECORDED entry whose new statement would derive another project, with
   `--rescope` absent: the OLD derived project is RECORDED through the existing
   rescope door, the statement lands, and ONE line says so;
3. with `--rescope`: it lands, derives, and ONE line says so;
4. an entry with a RECORDED project: unchanged, no line (the record wins);
5. no flip: nothing recorded, nothing printed;
6. `revise` prints ONE heads-up line when the staged statement would flip the
   derivation, naming both projects and that confirm keeps the old one unless
   `--rescope`.

One arm per rule (rules 1 and 2 are one measured confirm, so one arm). The
statement half of each fixture is plain prose (derives fleet); the flip half
names a task row (derives helm). RED on the current tip: the flag is not yet
parsed, the note is not yet printed, and the old project is not yet pinned.
"""
import contextlib
import io
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests._tmphome import home as _tmp_home  # noqa: E402
_tmp_home(prefix="helm-test-scopeflip-", var="HELM_HOME")

from helm import pk, store  # noqa: E402
from helm.store import cli as store_cli  # noqa: E402
from helm.store import FLEET, HOME_PROJECT  # noqa: E402

ENV_KEYS = ("HELM_HOME", "MELD_HOME", "HELM_ADOPTED_DIR", "MELD_ADOPTED_DIR",
            "HELM_CACHE_DIR", "MELD_CACHE_DIR", "HELM_CHAT_DIR",
            "MELD_CHAT_DIR", "HELM_CHAT_NAME", "MELD_CHAT_NAME",
            "CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID", "CODEX_SESSION_ID",
            "HELM_ACTOR")

TS = "2026-09-11T12:00:00Z"
# Plain prose: the statement line names NO helm artifact, so an unrecorded
# row in the helm-global root derives FLEET.
FLEET_STMT = "A fleet-wide belief about how seats stay awake"
# Names a task row: the ONE artifact shape that flips the derivation to helm.
HELM_STMT = "A belief recorded for task/3363 and nothing else"
LIVE = "scope-flip-unrecorded-belief"
RECORDED = "scope-flip-recorded-belief"
EDIT = "scope-flip-edit-belief"


def run_store(args):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = store.cmd_store(list(args))
    return rc, out.getvalue(), err.getvalue()


class ScopeFlipBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-scopeflip-")
        self.env_prior = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        for var, leaf in (("HELM_HOME", "helm"),
                          ("HELM_ADOPTED_DIR", "adopted"),
                          ("HELM_CACHE_DIR", "cache"),
                          ("HELM_CHAT_DIR", "chat")):
            os.environ[var] = os.path.join(self.tmp, leaf)
        os.makedirs(os.environ["HELM_ADOPTED_DIR"])
        # MINTED UNDER THE GLOBAL ROOT WITH NO RECORDED PROJECT, so the
        # statement line decides the scope and the flip is real. Both rows
        # start from plain prose, so both derive fleet before the flip.
        store.write_prior({"id": LIVE, "statement": FLEET_STMT,
                           "confidence": "0.90", "keywords": "awake,seat",
                           "stated_ts": TS, "source": "explicit",
                           "status": "live"})
        store.write_prior({"id": RECORDED, "statement": FLEET_STMT,
                           "confidence": "0.90", "keywords": "recorded,awake",
                           "stated_ts": TS, "source": "explicit",
                           "status": "live",
                           "project": "acme"})
        # A CANDIDATE prior, for the --edit arm: a live entry's edit rides a
        # staged revise (the status gate refuses --edit on a live entry), so
        # the --edit flip is confirmed on a candidate, whose confirm crosses
        # the mint guard — one unique keyword, so the probe passes.
        store.write_prior({"id": EDIT, "statement": FLEET_STMT,
                           "confidence": "0.90", "keywords": "candedit",
                           "stated_ts": TS, "source": "inferred",
                           "status": "candidate"})

    def tearDown(self):
        for k, v in self.env_prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def entry(self, eid):
        hits = [e for e in store.load_all() if e["id"] == eid]
        self.assertEqual(len(hits), 1, eid)
        return hits[0]

    # A fixture sanity arm, so the arms below are about the flip and not
    # about a statement that never derived fleet in the first place.
    def test_the_fixture_is_unrecorded_and_derives_fleet(self):
        e = self.entry(LIVE)
        self.assertEqual(str(e.get("project") or "").strip(), "")
        self.assertEqual(store.entry_project(e), store.FLEET)
        self.assertEqual(store.entry_scope(e)[1], "statement")
        # the flip half, on its own, does derive helm
        self.assertEqual(store.entry_project(
            {"statement": HELM_STMT, "move": ""}), store.HOME_PROJECT)

    def stage_flip(self, eid=LIVE):
        with mock.patch.object(store_cli, "_acting_actor", return_value="seat-a"):
            return run_store(["revise", eid, HELM_STMT, "--project", "p"])

    def confirm(self, eid=LIVE, extra=()):
        with mock.patch.object(store_cli, "_acting_actor", return_value="seat-c"):
            return run_store(["confirm", eid, "--project", "p"] + list(extra))


class AConfirmThatFlipsKeepsTheOldProject(ScopeFlipBase):
    # rules 1 + 2
    def test_a_staged_flip_with_no_rescope_keeps_the_old_project(self):
        rc, out, err = self.stage_flip()
        self.assertEqual(rc, 0, err)
        rc, out, err = self.confirm()
        self.assertEqual(rc, 0, err)
        self.assertIn("CONFIRMED '%s' -> live" % LIVE, out)
        e = self.entry(LIVE)
        # the statement DID land — the flip was refused, not the edit
        self.assertEqual(e["statement"], HELM_STMT)
        # the OLD derived project is what the entry is about, now RECORDED
        # through the rescope door, so a later re-read cannot re-derive helm
        self.assertEqual(store.entry_project(e), store.FLEET)
        self.assertEqual(str(e.get("project") or "").strip(), store.FLEET)
        self.assertEqual(store.entry_scope(e)[1], "recorded")
        # ONE line says the scope was kept, names both projects, and names
        # the door that takes the new one
        self.assertIn("scope kept at %s" % store.FLEET, out)
        self.assertIn("would derive %s" % store.HOME_PROJECT, out)
        self.assertIn("--rescope", out)

    def test_a_confirm_edit_that_flips_keeps_the_old_project_too(self):
        # the --edit path swaps the statement in the same turn, so it owes
        # the same refusal. --edit is a rest flag: every token after it is
        # statement prose, so --rescope/--type must stand before it.
        # (--project is extracted position-independently and may stand
        # anywhere, before or after --edit.)
        rc, out, err = run_store(
            ["confirm", EDIT, "--project", "p", "--edit", HELM_STMT])
        self.assertEqual(rc, 0, err)
        self.assertIn("CONFIRMED '%s' -> live" % EDIT, out)
        e = self.entry(EDIT)
        self.assertEqual(e["statement"], HELM_STMT)
        self.assertEqual(store.entry_project(e), store.FLEET)
        self.assertEqual(str(e.get("project") or "").strip(), store.FLEET)
        self.assertEqual(store.entry_scope(e)[1], "recorded")
        self.assertIn("scope kept at %s" % store.FLEET, out)
        self.assertIn("would derive %s" % store.HOME_PROJECT, out)
        self.assertIn("--rescope", out)


class AConfirmWithRescopeTakesTheNewProject(ScopeFlipBase):       # rule 3
    def test_a_staged_flip_with_rescope_takes_the_new_project(self):
        rc, out, err = self.stage_flip()
        self.assertEqual(rc, 0, err)
        rc, out, err = self.confirm(extra=["--rescope"])
        self.assertEqual(rc, 0, err)
        e = self.entry(LIVE)
        self.assertEqual(e["statement"], HELM_STMT)
        # it lands and derives; nothing was pinned against it
        self.assertEqual(store.entry_project(e), store.HOME_PROJECT)
        self.assertEqual(str(e.get("project") or "").strip(), "")  # noqa: VACUOUS_ASSERTION — the absent field IS rule 3 (nothing pinned); the statement is asserted above
        # ONE line says the scope moved, old -> new, and that --rescope did it
        self.assertIn("scope %s -> %s" % (store.FLEET, store.HOME_PROJECT), out)
        self.assertIn("--rescope", out)


class NoFlipRecordsAndPrintsNothing(ScopeFlipBase):               # rule 5
    def test_a_statement_change_that_derives_the_same_project_is_silent(self):
        other = "Another plain-prose belief about how seats stay awake"
        with mock.patch.object(store_cli, "_acting_actor", return_value="seat-a"):
            rc, out, err = run_store(["revise", LIVE, other, "--project", "p"])
        self.assertEqual(rc, 0, err)
        rc, out, err = self.confirm()
        self.assertEqual(rc, 0, err)
        self.assertIn("CONFIRMED '%s' -> live" % LIVE, out)
        e = self.entry(LIVE)
        self.assertEqual(e["statement"], other)
        # fleet -> fleet: nothing recorded (no pin on every entry), no line
        self.assertEqual(str(e.get("project") or "").strip(), "")
        self.assertNotIn("scope kept at", out)
        self.assertNotIn("scope fleet ->", out)


class ARecordedProjectIsUntouched(ScopeFlipBase):                 # rule 4
    def test_a_confirm_that_flips_on_a_recorded_entry_prints_no_scope_line(self):
        rc, out, err = self.stage_flip(eid=RECORDED)
        self.assertEqual(rc, 0, err)
        rc, out, err = self.confirm(eid=RECORDED)
        self.assertEqual(rc, 0, err)
        self.assertIn("CONFIRMED '%s' -> live" % RECORDED, out)
        e = self.entry(RECORDED)
        self.assertEqual(e["statement"], HELM_STMT)
        # the recorded field WINS regardless of what the statement now says
        self.assertEqual(store.entry_project(e), "acme")
        self.assertEqual(str(e.get("project") or "").strip(), "acme")
        # behavior unchanged: no scope line for an already-recorded entry
        self.assertNotIn("scope kept at", out)
        self.assertNotIn("scope %s ->" % store.FLEET, out)


class ReviseWarnsWhenItsStagedStatementWouldFlip(ScopeFlipBase):  # rule 6
    def test_staging_a_statement_that_flips_prints_a_heads_up(self):
        rc, out, err = self.stage_flip()
        self.assertEqual(rc, 0, err)
        # names BOTH projects and says confirm keeps the old one unless
        # --rescope — the operator sees the flip at the step where it is made
        self.assertIn("fleet", out)
        self.assertIn("helm", out)
        self.assertIn("--rescope", out)
        # and the entry is UNCHANGED by the warning: still live, still
        # serving the old statement, the revision merely staged
        e = self.entry(LIVE)
        self.assertEqual(e["statement"], FLEET_STMT)
        self.assertEqual(e["status"], "live")
        self.assertEqual(store.entry_project(e), store.FLEET)


if __name__ == "__main__":
    unittest.main()
