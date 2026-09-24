#!/usr/bin/env python3
"""store tests — hermetic: every root (adopted + helm-global + project) points
at a tempdir via HELM_HOME + HELM_ADOPTED_DIR. The real ~/.claude and ~/.helm,
and the predecessor tool's home, are never read or written."""
import contextlib
import io
import json
import os
import shlex
import shutil
import sys
import tempfile
import threading
import re
import unittest
from unittest import mock

import os as _os, sys as _sys  # noqa: E402
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
from tests._tmphome import home as _tmp_home  # noqa: E402
_tmp_home(prefix="helm-test-home-", var="HELM_HOME")

from helm import home, pk, premise, projscope, store  # noqa: E402

TS = "2026-07-18T00:00:00Z"


def prem_text(pid, statement, status="live"):
    """A legacy prem-*.md in the live-store shape (no confidence field)."""
    return ("---\nname: prem-%s\ndescription: \"premise: %s\"\nmetadata:\n"
            "  node_type: memory\n  type: premise\n  id: %s\n  statement: %s\n"
            "  status: %s\n  stated_ts: 2026-06-01\n---\n\nPREMISE: %s\n"
            % (pid, pid, pid, statement, status, statement))


class StoreBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-store-")
        self.adopted = os.path.join(self.tmp, "adopted")
        os.makedirs(self.adopted)
        self.env_prior = {k: os.environ.get(k)
                          for k in ("HELM_HOME", "HELM_ADOPTED_DIR",
                                    "HELM_NTFY_TOPIC", "MELD_NTFY_TOPIC")}
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_ADOPTED_DIR"] = self.adopted
        # a down/ambient notifier must NEVER be dialed by the suite — only the
        # opt-in mocked NotifyOnGraduationTest exercises _notify_graduation. Pop
        # both HELM_ and the legacy MELD_ fallback (home.env resolves both).
        for k in ("HELM_NTFY_TOPIC", "MELD_NTFY_TOPIC"):
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self.env_prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- seed helpers -------------------------------------------------------
    def global_dir(self, sub):
        return os.path.join(home.global_dir(), sub)

    def project_dir(self, name, sub):
        return os.path.join(home.project_dir(name), sub)

    def seed_prior(self, pid, statement, conf=0.6, keywords="", root_dir=None, **kw):
        e = {"id": pid, "statement": statement, "confidence": conf,
             "keywords": keywords, "stated_ts": TS, "source": "human"}
        e.update(kw)
        return store.write_prior(e, root_dir=root_dir or self.global_dir("premises"))

    def _attested(self, pid, statement, keywords):
        """A REAL capture through `helm premise` (native chain under this
        test's HELM_HOME, node dead) -> the entry's path, asserted attested."""
        prior = {k: os.environ.get(k) for k in ("HELM_NODE_URL", "HELM_CELL_PROFILE")}

        def restore():
            for k, v in prior.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        self.addCleanup(restore)
        os.environ["HELM_NODE_URL"] = "http://127.0.0.1:1"
        os.environ["HELM_CELL_PROFILE"] = "owner-cell"
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            rc = premise.cmd_premise(["%s | %s | %s | dev" % (pid, statement, keywords)])
        self.assertEqual(rc, 0, out.getvalue())
        self.assertIn("attested (native): record", out.getvalue())
        path = os.path.join(self.global_dir("premises"), "prior-%s.md" % pid)
        with open(path, encoding="utf-8") as f:
            self.assertIn("  attest_record: ", f.read())
        return path

    def one(self, es, eid):
        hits = [e for e in es if e["id"] == eid]
        self.assertEqual(len(hits), 1, "expected exactly one '%s' in %r"
                         % (eid, [x["id"] for x in es]))
        return hits[0]


class ReviseLiveEntryTest(StoreBase):
    """The refinement path the store did not have. Ruled the store defect that
    matters most by the fable panel the owner commissioned, and it produced its
    first measured cost the same night: a certainty-1.00 SAFETY premise
    (seat-liveness-is-environ-not-cwd) recommends `pgrep -x claude`, which is
    blind to panes that exec .../claude/versions/<semver> — and no verb could
    correct the clause. `evidence` moves confidence, which would be a lie when
    the core claim is right; `supersede` tombstones the id and splits retrieval
    weight for a statement that is mostly correct."""

    def live(self, pid="probe-rule", stmt="ORIGINAL: use pgrep -x claude"):
        self.seed_prior(pid, stmt, conf=1.0, keywords="seat up",
                        status="live", **{"class": "certain"})
        return pid

    def entry(self, pid):
        return store._pick_candidate(pid)[0]

    def test_the_entry_STAYS_LIVE_and_keeps_serving_while_staged(self):
        """THE DESIGN CONSTRAINT, and the reason this is not 'demote it to
        provisional': a candidate is EXCLUDED FROM INJECT, so demoting to stage
        a correction takes canon DARK exactly while it is being corrected —
        worst for the safety entries most worth correcting."""
        pid = self.live()
        e, err = store.revise(pid, TS, "CORRECTED: enumerate /proc")
        self.assertIsNone(err, err)
        after = self.entry(pid)
        self.assertEqual(after["status"], "live")          # never dark
        self.assertEqual(after["statement"], "ORIGINAL: use pgrep -x claude")
        self.assertEqual(after["pending_revision"]["statement"],
                         "CORRECTED: enumerate /proc")

    def test_confirm_SWAPS_it_in_and_the_id_survives(self):
        """Same id, so ratification and retrieval weight are preserved — that
        is the whole difference from supersede."""
        pid = self.live()
        _e, err = store.revise(pid, TS, "CORRECTED: enumerate /proc")
        self.assertIsNone(err, err)
        _c, err = store.confirm(pid, TS)
        self.assertIsNone(err, err)
        after = self.entry(pid)
        self.assertEqual(after["statement"], "CORRECTED: enumerate /proc")
        self.assertEqual(after["id"], pid)                 # identity preserved
        self.assertEqual(after["status"], "live")
        self.assertEqual(float(after["confidence"]), 1.0)  # belief untouched
        self.assertIsNone(after.get("pending_revision"))   # consumed, not left

    def test_the_revision_SURVIVES_THE_WRITE(self):
        """THE BUG THIS ARM EXISTS FOR: the store has THREE independent
        allowlists for one field — the writers' explicit field lists, the
        parsers' DEFAULTS dicts, and the JSON decoder — and each drops an
        unknown key SILENTLY. My first cut set the field on the entry dict,
        got a clean return, and persisted nothing. Assert the round trip
        through disk, never the return value."""
        pid = self.live()
        _e, err = store.revise(pid, TS, "CORRECTED: enumerate /proc")
        self.assertIsNone(err, err)
        path = self.entry(pid)["path"]
        with open(path, encoding="utf-8") as fh:
            raw = fh.read()
        self.assertIn("pending_revision", raw)             # it reached disk
        self.assertIn("CORRECTED: enumerate /proc", raw)
        # and it reads BACK as a dict, not as the string the file holds
        self.assertIsInstance(self.entry(pid)["pending_revision"], dict)

    def test_revise_REFUSES_an_empty_or_identical_correction(self):
        pid = self.live()
        _e, err = store.revise(pid, TS, "   ")
        self.assertIn("needs the corrected statement", err)
        _e, err = store.revise(pid, TS, "ORIGINAL: use pgrep -x claude")
        self.assertIn("identical", err)

    def test_revise_REFUSES_a_non_live_entry(self):
        """confirm carries the edit for a candidate or provisional; revise is
        the LIVE-entry door. Two verbs, two states, no overlap."""
        self.seed_prior("cand-rule", "a candidate", conf=0.6,
                        keywords="cand", status="candidate")
        _e, err = store.revise("cand-rule", TS, "something else")
        self.assertIsNotNone(err)
        self.assertIn("not live", err)

    def test_confirm_still_refuses_a_LIVE_entry_with_NO_revision(self):
        """The control that keeps the new clause from widening confirm: a live
        entry with nothing staged is still not confirmable, and the refusal now
        NAMES the verb that would stage one."""
        pid = self.live()
        _c, err = store.confirm(pid, TS)
        self.assertIsNotNone(err)
        self.assertIn("no staged revision", err)
        self.assertIn("revise", err)

    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = store.cmd_store(list(args))
        return rc, out.getvalue() + err.getvalue()

    def test_a_LEGAL_redefine_does_not_CANCEL_a_staged_revision(self):
        """T1 cross-family, measured at the exact tip: a lexicon
        redefine silently cancelled a staged correction.

        The reconstruction there names every field it means to carry, and had
        already dropped one before — `gloss`, recorded in its
        own comment. `pending_revision` made it the third. That block's stated
        contract is 'redefine MERGES, never strips', so the cure is to merge
        BY CONSTRUCTION (start from the previous entry, overwrite only what
        this verb deliberately changes) rather than add a fourth name to a
        list that has now forgotten three.

        AND THE SECOND HALF, which the first cure exposed: merging carried the
        field through as its raw JSON STRING, because a DIRECT `_parse_lexicon`
        call is a different door from `_parse_entry` and only the latter
        decodes — so `_json1` encoded it a SECOND time and the field came back
        unreadable, which `confirm` reads as nothing staged. Corruption by the
        opposite route from dropping it, and invisible without reading the
        FILE."""
        rc, _out = self.cli("add", "lexicon",
                            "l-merge|A definition here.|phrase|widget zeta")
        self.assertEqual(rc, 0)
        _r, err = store.revise("l-merge", TS, "A CORRECTED definition.")
        self.assertIsNone(err)
        # a LEGAL redefine that changes only the keywords
        rc, _out = self.cli("add", "lexicon",
                            "l-merge|A definition here.|phrase|widget zeta,widget extra")
        self.assertEqual(rc, 0)
        got = store._pick_candidate("l-merge", ctype="lexicon")[0]
        staged = got.get("pending_revision")
        self.assertIsInstance(staged, dict,
                              "the redefine cancelled or corrupted the stage")
        self.assertEqual(staged.get("statement"), "A CORRECTED definition.")
        # the redefine's OWN change landed — the positive control, or this
        # would pass on a redefine that did nothing at all
        self.assertIn("widget extra", got.get("keywords") or "")
        # and confirm can still install it
        _c, err = store.confirm("l-merge", TS)
        self.assertIsNone(err, err)

    def test_a_STAGED_revision_is_VISIBLE_before_it_installs(self):
        """The primary finding: the grep census found pending_revision in the
        parser and the writer and in NO read surface, so `revise` could stage
        a correction that nobody — including the owner about to run `confirm`
        — could read before it installed. A verb whose whole point is that the
        entry KEEPS SERVING while a correction waits owes a way to see what is
        waiting."""
        pid = self.live()
        _rc, before = self.cli("get", pid)
        # UNCONDITIONAL POSITIVE CONTROL ON THE SAME OBSERVABLE: `get` really
        # rendered this entry. Without it, a `get` that printed NOTHING would
        # satisfy the absence below and the whole arm would pass on a broken
        # read surface — which is the failure mode a surface test exists for.
        self.assertIn("ORIGINAL: use pgrep -x claude", before)
        self.assertNotIn("STAGED REVISION", before)  # noqa: VACUOUS_ASSERTION — the unconditional positive control on this exact needle is the SIBLING assertion nine lines down, assertIn("STAGED REVISION", after), on output from the same call with a revision staged. It proves this render CAN emit the string, so its absence here means "nothing is staged yet" rather than "this surface never says that". A positive on the same needle in the SAME breath is impossible: the string being absent is the fact under test, and the line above already proves `before` is a real non-empty render of this entry.
        _r, err = store.revise(pid, TS, "CORRECTED: enumerate /proc instead.")
        self.assertIsNone(err)
        rc, after = self.cli("get", pid)
        self.assertEqual(rc, 0)
        self.assertIn("STAGED REVISION", after)
        self.assertIn("CORRECTED: enumerate /proc instead.", after)
        # it must name the verb that installs it, and say the id survives
        self.assertIn("confirm " + pid, after)
        self.assertIn("SAME id", after)
        # the entry is STILL SERVING its current statement beside the stage
        self.assertIn("ORIGINAL: use pgrep -x claude", after)

    def test_EVERY_type_round_trips_revise_then_confirm(self):
        """The arm the other six were missing.

        EVERY arm above uses a PRIOR id, so 9821 tests passed over a `confirm`
        that raised AttributeError on three of the four types: _decode_lists
        ran in _parse_prior alone — one of six parsers — leaving
        pending_revision a str for heuristic, reference and lexicon, which
        write.py then called .get("statement") on. The control was real and
        aimed at the one type that worked, which is the failure mode a control
        is supposed to prevent.

        Measured before the cure with this exact matrix: prior rc=0, the other
        three RAISED. It is a POSITIVE control in both directions — every type
        must stage AND swap, so a cure that quietly made revise a no-op fails
        here rather than passing quietly."""
        cases = [
            ("prior", "p-rt", store.write_prior,
             {"id": "p-rt", "statement": "ORIGINAL prior statement.",
              "confidence": 1.0, "keywords": "widget alpha", "stated_ts": TS},
             "premises"),
            ("heuristic", "h-rt", store.write_heuristic,
             {"id": "h-rt", "move": "ORIGINAL heuristic move.",
              "statement": "ORIGINAL heuristic move.", "trigger": "widget beta",
              "keywords": "widget beta", "stated_ts": TS}, "heuristics"),
            ("reference", "r-rt", store.write_reference,
             {"id": "r-rt", "statement": "ORIGINAL reference summary.",
              "summary": "ORIGINAL reference summary.", "keywords": "widget gamma",
              "stated_ts": TS}, "references"),
            ("lexicon", "l-rt", store.write_lexicon,
             {"term": "l-rt", "definition": "ORIGINAL definition.",
              "kind": "phrase", "keywords": "widget delta"}, "lexicon"),
        ]
        # EVERY assertion below lives in this loop, so a zero-length `cases`
        # would make all of them vanish together and the test would pass
        # having checked nothing. Size the input first — the one assertion
        # here that cannot be skipped by an empty sequence.
        self.assertEqual(len(cases), 4)
        for etype, eid, writer, entry, sub in cases:
            with self.subTest(type=etype):
                entry = dict(entry, status="live", source="human")
                writer(entry, root_dir=self.global_dir(sub))
                corrected = "CORRECTED %s statement entirely." % etype
                _r, err = store.revise(eid, TS, corrected)
                self.assertIsNone(err, "%s: revise refused: %s" % (etype, err))
                # THE UNCONDITIONAL POSITIVE CONTROL ON THE SAME OBSERVABLE,
                # and the vacuity rung was right to demand it: without this,
                # the assertFalse below passes on an entry where staging never
                # happened at all, so "nothing staged after confirm" would
                # prove the swap on a verb that had done nothing.
                staged = store._pick_candidate(eid)[0].get("pending_revision")
                self.assertIsInstance(staged, dict,
                                      "%s: nothing staged, so the post-confirm "
                                      "absence proves nothing" % etype)
                self.assertEqual(staged.get("statement"), corrected)
                _c, err = store.confirm(eid, TS)
                self.assertIsNone(err, "%s: confirm refused: %s" % (etype, err))
                # the SWAP actually happened, under the SAME id
                got = store._pick_candidate(eid)[0]
                self.assertIsNotNone(got, "%s: entry vanished" % etype)
                text = got.get("statement") or got.get("move") \
                    or got.get("summary") or got.get("definition") or ""
                self.assertEqual(text, corrected,
                                 "%s: statement did not swap" % etype)
                self.assertFalse(got.get("pending_revision"),
                                 "%s: revision still staged after confirm" % etype)


class RootsTest(StoreBase):
    def test_roots_order_and_injection(self):
        r = store.roots()
        self.assertEqual([t[0] for t in r], ["adopted", "helm-global"])
        self.assertEqual(r[0][2], self.adopted)
        r = store.roots(project="p1")
        self.assertEqual([t[0] for t in r], ["adopted", "helm-global", "project"])
        self.assertEqual(r[2][1], "project:p1")

    def test_new_writes_never_hit_adopted_by_default(self):
        p = self.seed_prior("x-law", "Always do X.")
        self.assertTrue(p.startswith(home.global_dir()))
        self.assertEqual(os.listdir(self.adopted), [])


class PriorTest(StoreBase):
    def test_round_trip_and_byte_shape(self):
        p = self.seed_prior("x-law", "Always do X.", conf=0.7,
                            keywords="xlaw,zeta", domain="dev")
        expected = """---
name: prior-x-law
description: "prior: x-law - Always do X."
metadata:
  node_type: memory
  type: prior
  id: x-law
  statement: Always do X.
  confidence: 0.70
  class: prior
  load_class: jit
  status: live
  domain: dev
  keywords: xlaw,zeta
  stated_ts: %s
  last_updated: %s
  source: human
  evidence_log: []
  confidence_history: []
---

PRIOR: Always do X.

**Why a prior:** human-stated standing belief; agent flags, never silently overrides; confidence updates on logged evidence.
""" % (TS, TS)
        with open(p) as f:
            self.assertEqual(f.read(), expected)
        e = self.one(store.load_all(), "x-law")
        self.assertEqual((e["type"], e["class"], e["load_class"]), ("prior", "prior", "jit"))
        self.assertAlmostEqual(e["confidence"], 0.7)
        self.assertEqual((e["root"], e["scope"], e["status"]), ("helm-global", "global", "live"))
        self.assertEqual(e["path"], p)
        self.assertFalse(e["pinned"])
        self.assertEqual(e["evidence_log"], [])

    def test_legacy_prem_dual_read_prior_wins(self):
        pk.atomic_write(os.path.join(self.adopted, "prem-old-truth.md"),
                        prem_text("old-truth", "the legacy statement"))
        es = store.load_all()
        e = self.one(es, "old-truth")
        # a legacy premise missing confidence IS a certain-prior, jit-not-always
        self.assertEqual((e["confidence"], e["class"], e["load_class"]),
                         (1.0, "certain", "jit"))
        store.write_prior({"id": "old-truth", "statement": "the migrated statement",
                          "confidence": 0.8, "stated_ts": TS}, root_dir=self.adopted)
        e = self.one(store.load_all(), "old-truth")
        self.assertEqual(e["statement"], "the migrated statement")
        self.assertTrue(e["path"].endswith("prior-old-truth.md"))

    def test_confidence_coercion_and_clamp(self):
        self.assertEqual(store._coerce_conf(""), 1.0)
        self.assertEqual(store._coerce_conf(None), 1.0)
        self.assertEqual(store._coerce_conf("garbled"), 1.0)
        self.assertEqual(store._coerce_conf("1.7"), 1.0)
        self.assertEqual(store._coerce_conf(-2), 0.0)
        self.assertAlmostEqual(store._coerce_conf("0.35"), 0.35)

    def test_belief_never_rounds_up_to_certainty(self):
        # audit MEDIUM: %.2f serialized 0.995..0.999 as "confidence: 1.00"
        # beside "class: prior" — the next read minted a certain premise from
        # an agent-suppliable belief. A belief clamps to 0.99 BEFORE writing.
        p = self.seed_prior("almostsure", "strong belief", conf=0.999)
        with open(p) as f:
            raw = f.read()
        self.assertIn("  confidence: 0.99", raw)
        self.assertIn("  class: prior", raw)
        self.assertNotIn("1.00", raw)
        e = self.one(store.load_all(), "almostsure")
        self.assertEqual(e["class"], "prior")
        self.assertAlmostEqual(e["confidence"], 0.99)
        # an explicit human certainty (exactly 1.0) still writes as a premise
        p = self.seed_prior("truth", "human truth", conf=1.0)
        with open(p) as f:
            raw = f.read()
        self.assertIn("  confidence: 1.00", raw)
        self.assertIn("  class: certain", raw)

    def test_dormant_derivation(self):
        self.seed_prior("weak-belief", "barely held", conf=0.3)
        e = self.one(store.load_all(), "weak-belief")
        self.assertEqual(e["load_class"], "dormant")
        self.assertEqual(store.load_all(include_dormant=False), [])

    def test_retired_excluded_unless_asked(self):
        self.seed_prior("gone", "was true once", conf=0.9)
        e, err = store.retire("gone", TS, "no longer holds")
        self.assertIsNone(err)
        self.assertEqual(e["status"], "retired")
        self.assertEqual(store.load_all(), [])
        e = self.one(store.load_all(include_retired=True), "gone")
        self.assertEqual((e["status"], e["retired_why"]), ("retired", "no longer holds"))


class TimestampArgumentGuardTest(StoreBase):
    """`retire|evidence|supersede` take the timestamp POSITIONALLY and wrote it
    into the durable record unchecked. REPRODUCED before the fix: an agent
    passed the literal string "NOW" and helm wrote `retired_ts: NOW` AND
    `last_updated: NOW` onto a live entry; the live store also holds six
    records whose retired_ts is a bare epoch int or a date with no time.
    A record whose timestamp cannot be ordered cannot be replayed, and no
    reader downstream can tell it from a real one."""

    def test_retire_refuses_a_non_iso_timestamp_and_writes_nothing(self):
        self.seed_prior("guarded", "still true", conf=0.9)
        e, err = store.retire("guarded", "NOW", "probe")
        self.assertIsNone(e)
        self.assertIn("refusing the write", err)
        # the entry is UNTOUCHED — a refused verb must not half-write
        live = self.one(store.load_all(), "guarded")
        self.assertEqual(live["status"], "live")
        self.assertEqual(live["retired_ts"], "")

    def test_retire_still_accepts_a_real_timestamp(self):
        self.seed_prior("retirable", "was true once", conf=0.9)
        e, err = store.retire("retirable", TS, "done")
        self.assertIsNone(err)
        self.assertEqual(e["retired_ts"], TS)
        self.assertEqual(e["status"], "retired")

    def test_evidence_and_supersede_share_the_guard(self):
        self.seed_prior("moving", "a belief", conf=0.6)
        self.seed_prior("replacement", "the new one", conf=0.9)
        e, err = store.apply_evidence("moving", "NOW", 0.1, "probe")
        self.assertIsNone(e)
        self.assertIn("refusing the write", err)
        e, err = store.mark_superseded("moving", "replacement", "yesterday")
        self.assertIsNone(e)
        self.assertIn("refusing the write", err)
        # and both still work on a real instant
        e, err = store.apply_evidence("moving", TS, 0.1, "probe")
        self.assertIsNone(err)
        self.assertGreater(e["confidence"], 0.6)

    def test_valid_ts_shapes(self):
        # The boundary is #145's single parser: whatever the sort key can
        # ORDER may be written (epoch, date-only, tz-less all order), and only
        # an unorderable word is refused. A second stricter validator here
        # would reintroduce the two-parser drift #145 removed.
        from helm.store import write as store_write
        for ok in ("2026-08-03T09:44:23Z", "2026-08-03T09:44:23.501Z",
                   "2026-08-03T09:44:23+00:00", "2026-08-03T09:44:23-0700",
                   "2026-08-03T09:44:23.1-0700", "2026-08-03T09:44:23.1234-0700",
                   "2026-08-03T09:44:23.1234567+00:00", "2026-08-03T09:44:23.1",
                   "2026-08-03", "1781658848", "2026-08-03 09:44:23Z",
                   "2026-08-03T09:44:23", pk.now_ts()):
            self.assertIsNone(store_write._valid_write_ts(ok), ok)
        for bad in ("NOW", "", None, "yesterday", "2026-13-45T99:99:99Z",
                    "9999-12-31T23:59:59.1-0700", "0001-01-01T00:00:00.1+0700"):
            self.assertTrue(store_write._valid_write_ts(bad), repr(bad))


class GlossLifecycleMatrixTest(StoreBase):
    """A GLOSS MUST SURVIVE EVERY LIFECYCLE PATH, and be DROPPED by exactly one.

    Requested in review, and the review found why it was
    needed: the load schemas accepted `gloss` for prior/heuristic/reference and
    only ONE writer emitted it. So a hand-authored heuristic gloss vanished on
    retire, a reference gloss vanished on demote, and every _WRITERS lifecycle
    path (supersede, confirm, reject, xrev-clear) shared the loss — silently
    re-truncating the owner's rules at the next rewrite.

    Four writers each carrying their own optional-key loop is what allowed it,
    which is the per-case spiral the store's own premise names. The cure is one
    shared emit plus an honest refusal, and this matrix is what holds it: a new
    type or a new verb that forgets the gloss fails HERE."""

    TS = "2026-07-30T00:00:00Z"

    def _plant(self, kind, eid, gloss="a short firing line", status="live"):
        # STATUS MATTERS TO THE FIXTURE: load_all() excludes candidates by
        # design, so a candidate planted here is invisible to _gloss_of. Only
        # the confirm cases need one; everything else plants live.
        base = {"id": eid, "gloss": gloss, "status": status}
        if status == "candidate":
            # #204 activates candidates through the mint guard; this fixture is
            # testing gloss lifecycle, so give its candidate a valid probe.
            base["keywords"] = "glossprobe"
        if kind == "prior":
            store.write_prior(dict(base, statement="S" * 500, confidence="0.8"))
        elif kind == "heuristic":
            store.write_heuristic(dict(base, statement="S" * 500, move="do the thing"))
        elif kind == "reference":
            store.write_reference(dict(base, statement="S" * 500, url="http://x"))
        else:
            store.write_lexicon(dict(base, term=eid, definition="D" * 500))
        if status != "live":
            return None          # a candidate is not in load_all by design
        return self.one(store.load_all(), eid)

    def _gloss_of(self, eid):
        """Read the gloss OFF THE FILE, every status.

        load_all() excludes candidates AND retired entries by design, so the
        obvious reader cannot see the very rows this matrix is about — a
        retired heuristic is exactly where the gloss used to disappear. Reading
        the artifact keeps the test's oracle independent of the loader's
        filtering, which is the same reason the byte-shape test reads bytes."""
        import glob as _g, re as _re
        for f in _g.glob(os.path.join(self.tmp, "**", "*.md"), recursive=True):
            with open(f, encoding="utf-8") as fh:
                txt = fh.read()
            if _re.search(r"^\s+id: %s\s*$" % _re.escape(eid), txt, _re.M) or \
               _re.search(r"^\s+term: %s\s*$" % _re.escape(eid), txt, _re.M):
                m = _re.search(r"^\s+gloss: (.*)$", txt, _re.M)
                return m.group(1).strip() if m else ""
        return None

    def test_every_type_keeps_its_gloss_through_a_plain_rewrite(self):
        """The rewrite every lifecycle verb performs. Before the shared emit,
        three of these four lost it."""
        for kind in ("prior", "heuristic", "reference", "lexicon"):
            eid = "rw-" + kind
            e = self._plant(kind, eid)
            self.assertEqual(e.get("gloss"), "a short firing line", kind)
            store._WRITERS[e["type"]](e, path=e["path"])      # the round-trip
            self.assertEqual(self._gloss_of(eid), "a short firing line",
                             "%s lost its gloss on a lifecycle rewrite" % kind)

    def test_retire_and_demote_keep_the_gloss(self):
        """Named by name in review: heuristic gloss lost on retire,
        reference gloss lost on demote."""
        self._plant("heuristic", "lc-retire")
        store.retire("lc-retire", self.TS, why="done")
        self.assertEqual(self._gloss_of("lc-retire"), "a short firing line")
        # A REFERENCE, not a prior: the demote loss was reproduced on a
        # REFERENCE, and the first fixture planted a prior — a regression test
        # for a bug on a type the bug was never reported on. It would have
        # passed against the broken code for three of the four writers.
        self._plant("reference", "lc-demote")
        store.demote("lc-demote", self.TS, "too noisy")
        self.assertEqual(self._gloss_of("lc-demote"), "a short firing line")

    def test_confirm_without_an_edit_keeps_it_and_an_edit_drops_it(self):
        """THE ONE PATH THAT MUST DROP IT, and the worst of the four findings.

        A gloss is DERIVED from the statement. `confirm --edit` replaces the
        statement with new, possibly contradictory text; a preserved gloss then
        FIRES A LINE THE ENTRY NO LONGER SAYS. That is worse than the truncation
        the gloss exists to prevent — a severed sentence is visibly incomplete,
        a stale gloss is confidently wrong. Dropped, never regenerated: only the
        author knows what the new statement means."""
        self._plant("prior", "cf-keep", status="candidate")
        store.confirm("cf-keep", self.TS)
        self.assertEqual(self._gloss_of("cf-keep"), "a short firing line",
                         "an unedited confirm must not disturb the gloss")
        self._plant("prior", "cf-edit", status="candidate")
        store.confirm("cf-edit", self.TS, new_statement="a CONTRADICTORY claim")
        self.assertEqual(self._gloss_of("cf-edit"), "",
                         "an edited statement invalidates its gloss")

    def test_an_oversized_gloss_is_refused_with_the_real_number(self):
        """REFUSED, not silently cut. Accepting a gloss the injector will then
        truncate mid-clause recreates the exact failure the field exists to
        prevent, one remove away and quietly. The limit is measured on the REAL
        rendered line, so the number in the error is the number that matters."""
        with self.assertRaises(ValueError) as cm:
            store.write_prior({"id": "too-big", "statement": "s",
                               "confidence": "1.0", "gloss": "G" * 900})
        msg = str(cm.exception)
        self.assertIn("gloss too long", msg)
        self.assertIn("limit", msg)
        self.assertRegex(msg, r"shorten the gloss by \d+")


class GlossVerdictDebtTest(StoreBase):
    """The four contrary verdicts of lane gloss-fires-full-entry-stays
    (b7ac0070517d, 79553cbbbfe5, d59c552c3ff6, ea3b086bc687), each
    pinned to the EFFECT it demanded. The cures landed across the lane's own
    rounds and survived the store-package split; these pins are what keeps
    trunk evolution from quietly re-opening any of them."""

    def prior_path(self, pid):
        return os.path.join(self.global_dir("premises"), "prior-%s.md" % pid)

    def raw(self, path):
        with open(path, encoding="utf-8") as f:
            return f.read()

    def add(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = store.cmd_store(["add", *args])
        return rc, out.getvalue(), err.getvalue()

    def test_the_budget_counts_utf8_bytes_never_chars(self):  # noqa: VACUOUS_ASSERTION — the not-exists has its positive control in-test: a fitting gloss lands at the same path
        """The finding: '114 chars/414 UTF-8 bytes passes'. A four-byte code
        point is one char to len() and four to the injector's byte budget, so
        a char-counting check waves the line through and the budget then
        severs it mid-clause. The refusal must measure and SPEAK bytes."""
        from helm import inject
        gloss = "\U0001f4a5" * (inject.LINE_CAP // 4)     # bytes == LINE_CAP
        rendered = "PREMISE byte-law: " + gloss           # the real render shape
        self.assertLess(len(rendered), inject.LINE_CAP,
                        "chars fit — that is the trap this test exists for")
        n = len(rendered.encode("utf-8"))
        self.assertGreater(n, inject.LINE_CAP)
        target = self.prior_path("byte-law")
        with self.assertRaises(ValueError) as cm:
            store.write_prior({"id": "byte-law", "statement": "s",
                               "confidence": "1.0", "gloss": gloss},
                              path=target)
        msg = str(cm.exception)
        self.assertIn("%d UTF-8 bytes" % n, msg)
        self.assertIn("shorten the gloss by %d" % (n - inject.LINE_CAP), msg)
        self.assertFalse(os.path.exists(target), "a refused row must not land")
        # positive control on the SAME observable: only the byte overage was
        # refused — a fitting gloss lands at this exact path
        store.write_prior({"id": "byte-law", "statement": "s",
                           "confidence": "1.0", "gloss": "fits"}, path=target)
        self.assertTrue(os.path.exists(target))

    def test_caller_type_cannot_divert_the_oracle_from_the_real_render(self):
        """Two findings: an untyped probe measured the generic branch, and
        a caller-supplied `type` overrode write_prior's authority — either way
        the wrong line was measured and the real PREMISE line truncated. The
        gloss below is sized so the generic render (id: gloss) is 5 bytes
        UNDER cap and the caller-claimed TERM render is AT cap — only the true
        PREMISE render is over. The oracle parses the artifact, so the
        contaminating keys change nothing."""
        from helm import inject
        gloss = "G" * (inject.LINE_CAP - 20)   # "TERM authority-law: " == 20
        target = self.prior_path("authority-law")
        with self.assertRaises(ValueError) as cm:
            store.write_prior({"id": "authority-law", "statement": "s",
                               "confidence": "1.0", "type": "lexicon",
                               "term": "authority-law", "gloss": gloss},
                              path=target)
        self.assertIn("%d UTF-8 bytes" % (inject.LINE_CAP + 3), str(cm.exception))
        self.assertFalse(os.path.exists(target))
        # positive control: three bytes shorter clears the true PREMISE render
        store.write_prior({"id": "authority-law", "statement": "s",
                           "confidence": "1.0", "type": "lexicon",
                           "gloss": "G" * (inject.LINE_CAP - 23)}, path=target)
        self.assertTrue(os.path.exists(target))

    def test_store_add_remint_over_a_retired_id_drops_the_stale_gloss(self):
        """Three spellings of one loss: a re-mint under a
        retired slug carries NEW, often contradictory text — a kept gloss
        fires a line the entry no longer says. STALE_ON_REMINT owns the scrub
        (one spelling, shared with premise/_capture — the second hard-coded
        copy is exactly how the loss happened)."""
        rc, _, _ = self.add("prior", "remint-law | the OLD belief | 0.6 | remintkw")
        self.assertEqual(rc, 0)
        e = self.one(store.load_all(), "remint-law")
        e["gloss"] = "the old firing line"
        store.write_prior(e, path=e["path"])
        store.retire("remint-law", TS, "superseded by events")
        rc, _, _ = self.add("prior", "remint-law | a NEW contradictory belief | 0.6")
        self.assertEqual(rc, 0)
        raw = self.raw(e["path"])
        self.assertIn("a NEW contradictory belief", raw)
        self.assertNotIn("gloss:", raw,
                         "a re-mint must not keep firing the old line")

    def test_lexicon_same_definition_sharpen_keeps_the_gloss_a_redefine_drops_it(self):
        """Both directions were named: a keyword-only sharpen (same
        definition) must MERGE-KEEP the gloss — dropping it re-truncates the
        term's firing line — while a redefine that changes the meaning
        invalidates it exactly as `confirm --edit` does."""
        store.write_lexicon({"id": "gterm", "term": "gterm",
                             "definition": "the settled meaning",
                             "gloss": "the short firing line"})
        rc, _, err = self.add("lexicon",
                              "gterm | the settled meaning | coinage | fleetword")
        self.assertEqual((rc, err), (0, ""))
        path = store._lexicon_path("gterm", "global")
        self.assertIn("  gloss: the short firing line", self.raw(path))
        rc, _, _ = self.add("lexicon", "gterm | a changed meaning")
        self.assertEqual(rc, 0)
        raw = self.raw(path)
        self.assertIn("a changed meaning", raw)
        self.assertNotIn("gloss:", raw)

    def test_a_whitespace_equivalent_redefine_still_keeps_the_gloss(self):
        """The r2 blocker on this lane: write_lexicon whitespace-normalizes
        definitions before disk, and the retention check compared the STORED
        (normalized) form to the RAW candidate — so retyping the identical
        definition with different spacing read as a semantic change and
        silently dropped the gloss. The property pinned here is retention
        under NORMALIZED equivalence, not any particular comparison code:
        same meaning keeps the firing line, changed meaning drops it (the
        sibling above pins that direction)."""
        store.write_lexicon({"id": "wsterm", "term": "wsterm",
                             "definition": "the settled meaning",
                             "gloss": "the short firing line"})
        rc, _, err = self.add(
            "lexicon", "wsterm |  the   settled\tmeaning  | coinage | fleetword")
        self.assertEqual((rc, err), (0, ""))
        path = store._lexicon_path("wsterm", "global")
        raw = self.raw(path)
        self.assertIn("  definition: the settled meaning", raw)
        self.assertIn("  gloss: the short firing line", raw)

    def test_a_concurrent_same_id_writer_cannot_feed_the_oracle_its_row(self):
        """The repro, replayed deterministically: writer B lands a
        small VALID body on the old shared check-temp name inside the window
        between A's temp-write and A's parse. With the mkstemp temp the oracle
        still reads A's own bytes and refuses A's oversized gloss; with the
        shared deterministic name it validated B's row and landed A's 900-byte
        gloss — a validator handed someone else's artifact is not measuring
        the artifact."""
        import helm.store.load as store_load
        target = self.prior_path("same-law")
        scratch = os.path.join(self.tmp, "scratch", "prior-same-law.md")
        store.write_prior({"id": "same-law", "statement": "small truth",
                           "confidence": "1.0", "gloss": "tiny line"},
                          path=scratch)
        small = self.raw(scratch)
        real = store_load._parse_entry

        def writer_b_then_parse(tmp, name):
            with open(target + ".commit-check.tmp", "w", encoding="utf-8") as f:
                f.write(small)                 # B stomps the old shared name
            return real(tmp, name)

        with mock.patch.object(store_load, "_parse_entry", writer_b_then_parse):
            with self.assertRaises(ValueError) as cm:
                store.write_prior({"id": "same-law", "statement": "s",
                                   "confidence": "1.0", "gloss": "G" * 900},
                                  path=target)
        self.assertIn("gloss too long", str(cm.exception))
        self.assertFalse(os.path.exists(target),
                         "the contaminated row must never land")
        # positive control on the same observable: an honest small write —
        # writer B still stomping — lands at this exact path
        with mock.patch.object(store_load, "_parse_entry", writer_b_then_parse):
            store.write_prior({"id": "same-law", "statement": "small truth",
                               "confidence": "1.0", "gloss": "tiny line"},
                              path=target)
        self.assertTrue(os.path.exists(target))

    def test_a_refused_write_leaves_no_body_behind(self):  # noqa: VACUOUS_ASSERTION — both absences are controlled in-test: the glob first hits a planted probe, and a valid write lands at the same path
        """The second leg: a rejected oversized write once left
        its temp body on disk beside a MISSING entry. Refusal must leave the
        directory exactly as it found it."""
        import glob as g
        target = self.prior_path("clean-law")
        pattern = os.path.join(os.path.dirname(target), "*commit-check*")
        # positive control for BOTH absence observables below: the glob CAN
        # hit in this directory, and a valid write DOES land at this path
        os.makedirs(os.path.dirname(target), exist_ok=True)
        probe = target + ".probe.commit-check.tmp"
        open(probe, "w").close()
        self.assertEqual(g.glob(pattern), [probe])
        os.remove(probe)
        with self.assertRaises(ValueError):
            store.write_prior({"id": "clean-law", "statement": "s",
                               "confidence": "1.0", "gloss": "G" * 900},
                              path=target)
        self.assertFalse(os.path.exists(target))
        self.assertEqual(g.glob(pattern), [])
        store.write_prior({"id": "clean-law", "statement": "s",
                           "confidence": "1.0", "gloss": "fits"}, path=target)
        self.assertTrue(os.path.exists(target))
        self.assertEqual(g.glob(pattern), [], "a clean commit leaves no temp")

    def test_a_failed_temp_cleanup_is_surfaced_and_the_write_still_lands(self):
        """The cleanup can fail (EPERM, EIO) — what it may never do is pass
        SILENTLY. The transaction still commits (a stray temp must not veto a
        valid write), and the operator hears about the leaked file by name."""
        import glob as g
        target = self.prior_path("noisy-law")
        real_remove = os.remove

        def deaf_remove(p, *a, **kw):
            if "commit-check" in str(p):
                raise OSError("simulated EPERM")
            return real_remove(p, *a, **kw)

        err = io.StringIO()
        with mock.patch("os.remove", side_effect=deaf_remove), \
                contextlib.redirect_stderr(err):
            store.write_prior({"id": "noisy-law", "statement": "s",
                               "confidence": "1.0", "gloss": "a small line"},
                              path=target)
        self.assertIn("  gloss: a small line", self.raw(target))
        msg = err.getvalue()
        self.assertIn("could not remove commit-check temp", msg)
        leaked = g.glob(os.path.join(os.path.dirname(target), "*commit-check*"))
        self.assertEqual(len(leaked), 1, "the warned-about file really exists")
        self.assertIn(os.path.basename(leaked[0]), msg)


class PolicyPriorTest(StoreBase):
    def policy(self, pid="approval-canon", **kw):
        conf = kw.pop("conf", 1.0)
        fields = {
            "policy_kind": "approval-tier",
            "policy_members": ["seat:lead", "family:codex"],
            "policy_reason": "final approval requires an independent tier",
        }
        fields.update(kw)
        return self.seed_prior(pid, "The final approval tier is explicit.",
                               conf=conf, **fields)

    def test_policy_fields_round_trip_and_survive_lifecycle_rewrite(self):
        path = self.policy(evidence_log=[{"type": "support", "detail": "owner"}],
                           confidence_history=[{"value": 1.0}],
                           xrev_by="kimi", xrev_ts=TS)
        with open(path, encoding="utf-8") as f:
            raw = f.read()
        self.assertIn("  policy_kind: approval-tier", raw)
        self.assertIn("  policy_members: seat:lead || family:codex", raw)
        self.assertIn("  policy_reason: final approval requires an independent tier", raw)
        policy, err = store.load_certain_policy("approval-tier")
        self.assertIsNone(err)
        self.assertEqual(policy["policy_members"], ["seat:lead", "family:codex"])
        self.assertEqual(policy["policy_reason"],
                         "final approval requires an independent tier")
        retired, err = store.retire(policy["id"], TS, "superseded policy")
        self.assertIsNone(err)
        self.assertEqual(retired["policy_kind"], "approval-tier")
        reread = self.one(store.load_all(include_retired=True), policy["id"])
        self.assertEqual(reread["policy_members"], ["seat:lead", "family:codex"])
        self.assertEqual(reread["policy_reason"], policy["policy_reason"])
        self.assertEqual(reread["evidence_log"], policy["evidence_log"])
        self.assertEqual(reread["confidence_history"], policy["confidence_history"])
        self.assertEqual((reread["xrev_by"], reread["xrev_ts"]), ("kimi", TS))

    def test_policy_declared_never_collapses_expiry_to_true(self):  # noqa: VACUOUS_ASSERTION — the parser double raises Expired; propagation proves the policy answer was never fabricated
        with mock.patch("helm.store.load._policy_hits",
                        side_effect=projscope.Expired("test expiry")):
            with self.assertRaises(projscope.Expired):
                store.policy_declared("approval-tier")

    def test_policy_lookup_refuses_zero_ambiguous_and_incomplete(self):
        policy, err = store.load_certain_policy("approval-tier")
        self.assertIsNone(policy)
        self.assertIn("no live policy", err)
        path = self.policy()
        with open(path, encoding="utf-8") as f:
            raw = f.read()
        pk.atomic_write(path, raw.replace(
            "  policy_members: seat:lead || family:codex",
            "  policy_members: "))
        policy, err = store.load_certain_policy("approval-tier")
        self.assertIsNone(policy)
        self.assertIn("no policy_members", err)
        self.policy(pid="approval-canon-2", policy_members=["family:kimi"])
        policy, err = store.load_certain_policy("approval-tier")
        self.assertIsNone(policy)
        self.assertIn("ambiguous policy kind", err)

    def test_policy_lookup_requires_explicit_human_certainty(self):
        path = self.policy()
        with open(path, encoding="utf-8") as f:
            raw = f.read()
        pk.atomic_write(path, raw.replace("  confidence: 1.00",
                                          "  confidence: garbage"))
        policy, err = store.load_certain_policy("approval-tier")
        self.assertIsNone(policy)
        self.assertIn("explicitly certain", err)
        self.policy()
        with open(path, encoding="utf-8") as f:
            raw = f.read()
        pk.atomic_write(path, raw.replace("  source: human",
                                          "  source: inferred"))
        policy, err = store.load_certain_policy("approval-tier")
        self.assertIsNone(policy)
        self.assertIn("human source", err)

    def test_policy_writer_rejects_laundering_and_terminal_controls(self):
        for kw in ({"policy_members": ["seat:lead||family:codex"]},
                   {"policy_reason": "safe\x1b]2;spoof\x07"},
                   {"conf": 0.8}, {"source": "agent-inferred"}):
            with self.assertRaises(ValueError):
                self.policy(**kw)

    def test_project_policy_uses_the_existing_shadowing_law(self):
        self.policy(policy_members=["family:codex"])
        self.seed_prior(
            "approval-canon", "Project-specific final approval tier.", conf=1.0,
            source="human", policy_kind="approval-tier",
            policy_members=["family:kimi"],
            policy_reason="project tier",
            root_dir=self.project_dir("p1", "premises"))
        global_policy, err = store.load_certain_policy("approval-tier")
        self.assertIsNone(err)
        project_policy, err = store.load_certain_policy("approval-tier", project="p1")
        self.assertIsNone(err)
        self.assertEqual(global_policy["policy_members"], ["family:codex"])
        self.assertEqual(project_policy["policy_members"], ["family:kimi"])

    def test_re_mint_does_not_reactivate_retired_policy_metadata(self):
        self.policy()
        _retired, err = store.retire("approval-canon", TS, "tier ended")
        self.assertIsNone(err)
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            rc = store.cmd_store([
                "add", "premise", "approval-canon | unrelated new premise | canonkw"])
        self.assertEqual(rc, 0)
        entry = self.one(store.load_all(), "approval-canon")
        self.assertEqual(entry["policy_kind"], "")
        self.assertEqual(entry["policy_members"], [])
        self.assertEqual(entry["policy_reason"], "")
        policy, err = store.load_certain_policy("approval-tier")
        self.assertIsNone(policy)
        self.assertIn("no live policy", err)


class EvidenceTest(StoreBase):
    def test_belief_move_appends_receipts(self):
        self.seed_prior("belief", "probably true", conf=0.6, keywords="zork")
        e, msg = store.apply_evidence("belief", TS, "0.2", "confirmed in the field")
        self.assertIsNone(msg)
        self.assertAlmostEqual(e["confidence"], 0.8)
        # persisted: reread from disk carries the receipt + the snapshot
        e = self.one(store.load_all(), "belief")
        self.assertAlmostEqual(e["confidence"], 0.8)
        self.assertEqual(len(e["evidence_log"]), 1)
        self.assertEqual(e["evidence_log"][0]["type"], "support")
        self.assertEqual(e["evidence_log"][0]["by"], "agent")
        self.assertEqual(len(e["confidence_history"]), 1)
        self.assertAlmostEqual(e["confidence_history"][0]["value"], 0.8)

    def test_belief_clamp_never_reaches_certainty(self):
        self.seed_prior("belief", "probably true", conf=0.6)
        e, _ = store.apply_evidence("belief", TS, 10, "overwhelming support")
        self.assertEqual(e["confidence"], 0.99)
        e, _ = store.apply_evidence("belief", TS, -10, "overwhelming contradiction")
        self.assertEqual(e["confidence"], 0.05)

    def test_unreasoned_move_refused(self):
        self.seed_prior("belief", "probably true", conf=0.6)
        e, err = store.apply_evidence("belief", TS, 0.1, "   ")
        self.assertIsNone(e)
        self.assertIn("reason", err)
        e, err = store.apply_evidence("belief", TS, "not-a-number", "why")
        self.assertIsNone(e)
        self.assertEqual(err, "delta not numeric")
        e, err = store.apply_evidence("missing", TS, 0.1, "why")
        self.assertIsNone(e)
        self.assertEqual(err, "not found")

    def test_certain_prior_logs_but_does_not_move(self):
        self.seed_prior("truth", "human-stated truth", conf=1.0)
        e, msg = store.apply_evidence("truth", TS, -0.3, "agent saw a counterexample")
        self.assertIsNotNone(msg)
        self.assertIn("certain-prior", msg)
        self.assertEqual(e["confidence"], 1.0)
        e = self.one(store.load_all(), "truth")
        self.assertEqual(e["confidence"], 1.0)      # pinned at certainty
        self.assertEqual(len(e["evidence_log"]), 1)  # but the contradiction is RECORDED
        self.assertEqual(e["evidence_log"][0]["type"], "contradict")

    def test_human_evidence_moves_a_certainty(self):
        self.seed_prior("truth", "human-stated truth", conf=1.0)
        e, msg = store.apply_evidence("truth", TS, -0.5, "the human recanted", by="human")
        self.assertIsNone(msg)
        self.assertAlmostEqual(e["confidence"], 0.5)

    def test_evidence_writes_in_place_on_adopted(self):
        store.write_prior({"id": "adopted-belief", "statement": "lives in adopted",
                          "confidence": 0.6, "stated_ts": TS}, root_dir=self.adopted)
        e, _ = store.apply_evidence("adopted-belief", TS, 0.1, "still true")
        self.assertEqual(os.path.dirname(e["path"]), self.adopted)
        self.assertAlmostEqual(self.one(store.load_all(), "adopted-belief")["confidence"], 0.7)


class SupersedeTest(StoreBase):
    def test_tombstone_backpointer_and_stops_injecting(self):
        self.seed_prior("old-way", "the old belief", conf=0.8, keywords="zorkway")
        self.seed_prior("new-way", "the new belief", conf=0.9, keywords="zorkway")
        old, err = store.mark_superseded("old-way", "new-way", TS, "replaced by new-way")
        self.assertIsNone(err)
        self.assertEqual((old["status"], old["replaced_by"]),
                         ("delete_eligible", "new-way"))
        # the tombstone stops injecting but the FILE stays
        self.assertEqual([e["id"] for e in store.load_all()], ["new-way"])
        self.assertTrue(os.path.exists(old["path"]))
        self.assertEqual([e["id"] for e in store.resolve_prompt("zorkway plan")],
                         ["new-way"])
        all_es = store.load_all(include_retired=True)
        self.assertEqual(self.one(all_es, "new-way")["supersedes"], "old-way")
        self.assertEqual(self.one(all_es, "old-way")["retired_why"], "replaced by new-way")

    def test_refusals(self):
        self.seed_prior("only", "the only one", conf=0.8)
        _, err = store.mark_superseded("only", "only", TS)
        self.assertIn("supersede itself", err)
        _, err = store.mark_superseded("only", "ghost", TS)
        self.assertIn("must exist first", err)
        _, err = store.mark_superseded("ghost", "only", TS)
        self.assertIn("not found", err)
        _, err = store.mark_superseded("", "only", TS)
        self.assertIn("both", err)


class ResolveTest(StoreBase):
    def test_specificity_guard(self):
        self.seed_prior("zeta-law", "specific belief", conf=0.9, keywords="build,test")
        # generic-only match (build/test are wallpaper words) -> rejected
        self.assertEqual(store.resolve_prompt("let's build a test"), [])
        # the id itself is a specific probe
        self.assertEqual([e["id"] for e in store.resolve_prompt("apply zeta-law here")],
                         ["zeta-law"])
        # a specific keyword fires
        self.seed_prior("kw-belief", "kw belief", conf=0.9, keywords="flimflam,build")
        self.assertEqual([e["id"] for e in store.resolve_prompt("pure flimflam")],
                         ["kw-belief"])

    def test_salience_law_empty(self):
        self.seed_prior("zeta-law", "specific belief", conf=0.9, keywords="zetaness")
        self.assertEqual(store.resolve_prompt("nothing relevant here"), [])
        self.assertEqual(store.resolve_prompt(""), [])
        self.assertEqual(store.resolve_prompt(None), [])

    def test_confidence_weighted_ranking_and_cap(self):
        for i, conf in enumerate((0.5, 0.9, 0.7)):
            self.seed_prior("belief-%d" % i, "b%d" % i, conf=conf, keywords="glorp")
        got = [e["id"] for e in store.resolve_prompt("glorp time")]
        self.assertEqual(got, ["belief-1", "belief-2", "belief-0"])
        self.assertEqual(len(store.resolve_prompt("glorp time", cap=2)), 2)

    def test_word_boundary(self):
        self.seed_prior("zeta-law", "specific", conf=0.9, keywords="ram")
        self.assertEqual(store.resolve_prompt("the program crashed"), [])
        self.assertEqual([e["id"] for e in store.resolve_prompt("out of ram again")],
                         ["zeta-law"])

    def test_the_jit_cap_moves_the_selected_set_by_parsed_recency(self):
        """#145: five candidates for four slots, so the control CAN evict.

        The old raw-string walk selected `garbage-now` and dropped `real-d`;
        the parsed walk must change the selected SET, not merely move one row to
        a different position inside an unchanged cap."""
        rows = (("real-a", "2026-08-04T00:00:00Z"),
                ("real-b", "2026-08-03T00:00:00Z"),
                ("real-c", "2026-08-02T00:00:00Z"),
                ("real-d", "2026-08-01T00:00:00Z"),
                ("garbage-now", "NOW"))
        for eid, stamp in rows:
            path = self.seed_prior(
                eid, "specific", conf=0.9, keywords="glorp",
                last_updated=TS if stamp == "NOW" else stamp)
            if stamp == "NOW":
                with open(path) as f:
                    body = f.read()
                pk.atomic_write(path, body.replace(
                    "  last_updated: %s\n" % TS,
                    "  last_updated: NOW\n"))
        loaded = [e for e in store.load_all()
                  if e["id"] in {eid for eid, _stamp in rows}]
        raw_winners = [e["id"] for e in sorted(
            loaded, key=lambda e: e.get("last_updated") or "", reverse=True)[:4]]
        winners = [e["id"] for e in store.resolve_prompt("glorp", cap=4)]
        self.assertEqual(raw_winners,
                         ["garbage-now", "real-a", "real-b", "real-c"])
        self.assertEqual(winners, ["real-a", "real-b", "real-c", "real-d"])
        self.assertNotEqual(set(raw_winners), set(winners))

    def test_write_validation_and_recency_share_one_timestamp_grammar(self):  # noqa: VACUOUS_ASSERTION — each data arm proves a positive scalar or explicit refusal before checking the paired absence
        from helm.store import _common as store_common
        from helm.store import write as store_write
        for bad in ("NOW", "9999-99-99", "2026-08-03T99:99:99Z",
                    "ram-demon", "not a date", "178313434393609190", ""):
            self.assertIsNotNone(store_write._valid_write_ts(bad),
                                 "an invalid timestamp was accepted: %r" % bad)
            self.assertEqual(store_common._recency({"last_updated": bad}), 0.0)
        for good in ("2026-08-03T23:00:00Z", "2026-08-03",
                     "2026-08-03T23:59Z", "2026-08-03T23:00:00+05:00",
                     "1783884443", "1783884443000", "1783884443000000",
                     "1783884443000000000"):
            value, err = store_common._timestamp_scalar(good)
            self.assertGreater(value, 0.0,
                               "a valid timestamp produced no recency: %r" % good)
            self.assertIsNone(err,
                              "a parseable timestamp was refused: %r" % good)
        recency = lambda stamp: store_common._recency({"last_updated": stamp})
        self.assertEqual(recency("2026-08-03T23:59Z"),
                         recency("2026-08-03T23:59:00Z"))
        self.assertEqual(recency("2026-08-03T23:00:00+05:00"),
                         recency("2026-08-03T18:00:00Z"))
        for epoch in ("1783884443000", "1783884443000000",
                      "1783884443000000000"):
            self.assertEqual(recency(epoch), recency("1783884443"))

    def test_every_typed_writer_refuses_bad_recency_without_changing_the_file(self):  # noqa: VACUOUS_ASSERTION — every arm first writes and reads a nonempty artifact, then proves refusal leaves those exact bytes intact
        cases = (
            (store.write_prior,
             {"id": "ts-prior", "statement": "p", "confidence": 0.9,
              "stated_ts": TS, "last_updated": TS}, "last_updated"),
            (store.write_lexicon,
             {"term": "ts-lex", "definition": "l", "updated_ts": TS},
             "updated_ts"),
            (store.write_heuristic,
             {"id": "ts-heur", "move": "h", "trigger": "h",
              "stated_ts": TS, "last_updated": TS}, "last_updated"),
            (store.write_reference,
             {"id": "ts-ref", "statement": "r", "stated_ts": TS,
              "last_updated": TS}, "last_updated"),
        )
        for writer, row, field in cases:
            with self.subTest(writer=writer.__name__):
                root = os.path.join(self.tmp, writer.__name__)
                path = writer(row, root_dir=root)
                with open(path, "rb") as f:
                    before = f.read()
                self.assertGreater(len(before), 0,
                                   "the positive-control artifact was empty")
                bad = dict(row)
                bad[field] = "NOW"
                with self.assertRaisesRegex(ValueError, "refusing the write"):
                    writer(bad, path=path)
                with open(path, "rb") as f:
                    self.assertEqual(f.read(), before)

    def test_every_timestamp_lifecycle_refuses_without_changing_an_artifact(self):
        from helm.store import write as store_write
        paths = []
        for eid, status in (("ts-evidence", "live"), ("ts-old", "live"),
                            ("ts-new", "live"), ("ts-retire", "live"),
                            ("ts-confirm", "candidate"),
                            ("ts-reject", "candidate"),
                            ("ts-xrev", "candidate")):
            paths.append(self.seed_prior(
                eid, "specific", conf=0.9, keywords="glorp", status=status,
                source="inferred" if status == "candidate" else "human"))
        paths.append(self.seed_prior(
            "ts-demote", "specific", conf=1.0, keywords="glorp", pin=True))
        operations = (
            ("evidence", lambda: store_write.apply_evidence(
                "ts-evidence", "NOW", 0.1, "reason", "test")),
            ("supersede", lambda: store_write.mark_superseded(
                "ts-old", "ts-new", "NOW", "reason")),
            ("retire", lambda: store_write.retire("ts-retire", "NOW")),
            ("confirm", lambda: store_write.confirm("ts-confirm", "NOW")),
            ("reject", lambda: store_write.reject("ts-reject", "NOW")),
            ("xrev", lambda: store_write.xrev_clear(
                "ts-xrev", "NOW", "reviewer")),
            ("demote", lambda: store_write.demote(
                "ts-demote", "NOW", "reason")),
        )
        before = {}
        for path in paths:
            with open(path, "rb") as f:
                before[path] = f.read()
        self.assertEqual(len(before), 8)
        self.assertTrue(all(before.values()),
                        "one positive-control artifact was empty")
        for name, operation in operations:
            with self.subTest(operation=name):
                try:
                    _row, err = operation()
                except ValueError as exc:
                    err = str(exc)
                self.assertIn("refusing the write", err)
                for path in paths:
                    with open(path, "rb") as f:
                        self.assertEqual(f.read(), before[path])

    def test_an_inflected_form_resolves_the_stem(self):
        """A plural or -ing form is the SAME concept, and the exact matcher
        silently missed it — the entry just never resolved and nothing reported
        a near-miss. Evidence it was already hurting: the live lexicon is
        hand-padded with variants (`account` AND `accounts`, `adapt` AND
        `adapted`) to work around this, which only ever covers the inflections
        whoever wrote that entry thought of."""
        self.seed_prior("infl-law", "specific", conf=0.9, keywords="glorpwidget")
        for text in ("one glorpwidget", "two glorpwidgets", "glorpwidgeting along",
                     "glorpwidgeted yesterday"):
            self.assertEqual([e["id"] for e in store.resolve_prompt(text)],
                             ["infl-law"], text)

    def test_inflection_never_reaches_a_different_word(self):
        """The safety is the >= 4-char floor plus a suffix WHITELIST, not a
        stemmer. Short stems form unrelated words under suffixing (`ban` + `d`
        is `band`), so `d` is excluded outright and 3-char probes stay exact —
        which is also why test_word_boundary's `ram`/`program` case is
        untouched. And the allowance is a suffix set, never a prefix match:
        `cap` must not reach `capability`, nor `card` reach `cardiac`."""
        self.seed_prior("short-stem", "specific", conf=0.9, keywords="cap")
        self.assertEqual(store.resolve_prompt("the capability lane"), [])
        self.assertEqual(store.resolve_prompt("caps on injection"), [])
        self.seed_prior("long-stem", "specific", conf=0.9, keywords="glorp")
        self.assertEqual(store.resolve_prompt("glorpless and glorpiform"), [])

    def test_a_bare_d_suffix_is_excluded_even_above_the_floor(self):
        """CAUGHT BY MUTATION, NOT BY REVIEW. Adding `d` to the suffix set left
        all 126 tests green — the exclusion was documented and unenforced, which
        is a claim laundered as a guarantee.

        The >= 4 floor does NOT make `d` safe, because 4-char stems form
        unrelated words under it too: `boar` + `d` is `board`. Bare `d` is
        excluded for every probe length, and only `-ed` (which carries a vowel
        of its own) is accepted."""
        self.seed_prior("boar-law", "specific", conf=0.9, keywords="boar")
        self.assertEqual(store.resolve_prompt("post it to the board"), [])
        self.assertEqual([e["id"] for e in store.resolve_prompt("a wild boar")],
                         ["boar-law"])

    def test_an_id_is_matched_exactly_not_inflected(self):
        """Ids are kebab-case slugs, not English words. `_probe_re` only relaxes
        purely alphabetic probes, so an id keeps the exact law."""
        self.seed_prior("glorp-law", "specific", conf=0.9, keywords="zzunused")
        self.assertEqual([e["id"] for e in store.resolve_prompt("apply glorp-law")],
                         ["glorp-law"])
        self.assertEqual(store.resolve_prompt("apply glorp-laws here"), [])

    def test_inflected_hit_reports_the_probe_not_the_surface_form(self):
        """`matched` feeds the DF map, so it must carry the PROBE. Returning
        `glorpwidgets` here would KeyError the 1/df lookup in resolve_prompt —
        this asserts the contract that keeps that from happening."""
        self.seed_prior("infl-law", "specific", conf=0.9, keywords="glorpwidget")
        e = self.one(store.load_all(), "infl-law")
        from helm.store import resolve as _resolve
        hits, specific, matched = _resolve._probe_hits(e, "two glorpwidgets")
        self.assertEqual(hits, 1)
        self.assertTrue(specific)
        self.assertEqual(matched, ["glorpwidget"])

    def test_a_read_infers_the_project_from_cwd_and_says_so(self):
        """THE SEAM THAT COST A PRODUCTION INCIDENT AN HOUR. The hook path
        (`helm inject --hook-json`) derives the project from the hook JSON's cwd,
        so a seat working in a project DOES get that project's entries. The
        interactive CLI never looked at os.getcwd(), so the same query typed to
        CHECK that behaviour returned nothing.

        Live: a seat wrote an incident runbook for a client project with
        `--project <name>`, could not resolve it from that project's cwd, concluded
        "project-scoped resolve is unfinished", and moved the entry back to
        _global. The entry was fine and project resolve was fine — the read path
        it debugged with could not see it. An inconsistency between the path that
        RUNS and the path you DEBUG WITH manufactures false architectural
        conclusions, which is worse than either path being broken."""
        from helm.store import cli as _cli
        self.assertIn("resolve", _cli._CWD_SCOPED_READS)
        self.assertIn("list", _cli._CWD_SCOPED_READS)
        # A WRITE must never be cwd-scoped: where knowledge LIVES is a decision,
        # and silently homing an entry by the directory you stood in is the
        # surprise this fix removes rather than adds.
        for w in ("add", "confirm", "reject", "supersede", "retire", "demote"):
            self.assertNotIn(w, _cli._CWD_SCOPED_READS, w)

    def test_dormant_and_pinned_not_in_jit(self):
        self.seed_prior("weak", "weak", conf=0.3, keywords="glorp")
        self.seed_prior("pinned-one", "always on", conf=0.9, keywords="glorp", pin="true")
        self.assertEqual(store.resolve_prompt("glorp time"), [])

    def test_a_rare_function_word_never_outranks_the_topical_term(self):
        """THE LIVE 2026-07-25 REGRESSION, reduced.

        DF weighting makes a RARE probe strong. A modal like "should" is rare
        across the corpus yet meaningless, so it scored 0.250 while the topical
        "dispatch" scored 0.062 — and all four jit slots went to entries that
        matched nothing but the modal, pushing the genuinely relevant entry (and
        helm's own dispatch capability index) over cap. Function words are
        exactly the population that is rare-yet-meaningless, which is why the
        generic set has to cover the whole closed class rather than a handful of
        words that felt generic."""
        self.seed_prior("modal-noise", "irrelevant", conf=0.9,
                        keywords="should,whenever")
        for i in range(6):                       # make the topical term COMMON
            self.seed_prior("topical-%d" % i, "t%d" % i, conf=0.9,
                            keywords="dispatchx")
        got = [e["id"] for e in
               store.resolve_prompt("should I cancel the dispatchx")]
        self.assertNotIn("modal-noise", got)
        self.assertTrue(got, "the topical entries must still resolve")
        self.assertTrue(all(g.startswith("topical-") for g in got), got)

    def test_the_whole_closed_class_is_generic_not_a_sampling(self):
        """The boundary is grammatical, so it is checkable: English mints no new
        modals or pronouns. A case-by-case list is always one token short — that
        is the per-case-handler shape this repo has a premise about."""
        from helm.store._common import GENERIC_KEYWORDS
        closed_class = (
            "should could would might must can will shall may "      # modals
            "be been am are was were has have had does did "          # auxiliaries
            "not no never none "                                      # negation
            "i me my you your we us our they them their he she it "   # pronouns
            "what why how when where which who "                      # question words
            "if then else but so because while until than "           # subordinators
            "at by from into over under about after before through "  # prepositions
            "all any some each every both more most less "            # quantifiers
        ).split()
        missing = [w for w in closed_class if w not in GENERIC_KEYWORDS]
        self.assertEqual(missing, [], "closed-class words left scoreable: %s"
                         % missing)

    def test_content_words_are_never_swept_into_the_generic_set(self):
        """The other half of the boundary, and the one that keeps this fix from
        becoming a retrieval outage: an over-broad stopword set silently deletes
        recall, and a corpus that returns nothing looks exactly like a corpus
        with nothing relevant in it."""
        from helm.store._common import GENERIC_KEYWORDS
        for word in ("dispatch", "worktree", "verdict", "polarity", "seat",
                     "proxy", "premise", "ledger", "stall", "reviewer",
                     "codex", "family", "beacon", "signing"):
            self.assertNotIn(word, GENERIC_KEYWORDS, word)

    def test_a_function_word_only_entry_still_resolves_by_its_id(self):
        """Making a keyword generic must not orphan the entry: the id remains a
        specific probe, so an entry whose keywords are all function words is
        reachable rather than lost."""
        self.seed_prior("zorkish-law", "reachable", conf=0.9,
                        keywords="should,would")
        self.assertEqual(store.resolve_prompt("should we"), [])
        self.assertEqual([e["id"] for e in
                          store.resolve_prompt("apply zorkish-law now")],
                         ["zorkish-law"])


class DFRankTest(StoreBase):
    """DF-weighted JIT scoring: a matched probe contributes 1/df (df = entries
    carrying it), confidence-weighted — rare keywords are strong signals, and
    the old (hits, id-desc) ranking noise is gone."""

    def test_rare_keyword_outranks_generic_with_more_hits(self):
        # six entries share alpha/beta/gamma (df=6 each); one entry alone
        # carries glorpx (df=1). Old ranking: 3 hits beat 1 -> a shared-keyword
        # entry won. DF: 3/6 = 0.5 < 1/1 = 1.0 -> the rare match wins the cap.
        for i in range(5):
            self.seed_prior("filler-%d" % i, "s", conf=0.8, keywords="alpha,beta,gamma")
        self.seed_prior("many-hits", "s", conf=0.8, keywords="alpha,beta,gamma")
        self.seed_prior("rare-one", "s", conf=0.8, keywords="glorpx")
        got = [e["id"] for e in store.resolve_prompt("alpha beta gamma glorpx report")]
        self.assertEqual(got[0], "rare-one")

    def test_alphabetical_order_no_longer_decides(self):
        # same confidence, same ts: the old tiebreak ranked ids DESCENDING, so
        # zzz-* won the cap arbitrarily. DF puts the rare match first instead.
        self.seed_prior("zzz-shared", "s", conf=0.8, keywords="commonkw")
        self.seed_prior("yyy-shared", "s", conf=0.8, keywords="commonkw")
        self.seed_prior("aaa-rare", "s", conf=0.8, keywords="rarekw")
        got = [e["id"] for e in store.resolve_prompt("commonkw rarekw both")]
        self.assertEqual(got[0], "aaa-rare")
        # and a FULL tie (score AND ts) falls to stable load order, never the
        # old id-descending rank (which would put zzz-shared first)
        self.assertEqual(got[1:], ["yyy-shared", "zzz-shared"])

    def test_tie_breaks_most_recently_updated(self):
        self.seed_prior("aaa-stale", "s", conf=0.8, keywords="glorp",
                        last_updated="2026-01-01T00:00:00Z")
        self.seed_prior("zzz-fresh", "s", conf=0.8, keywords="glorp",
                        last_updated="2026-06-01T00:00:00Z")
        self.assertEqual([e["id"] for e in store.resolve_prompt("glorp time")],
                         ["zzz-fresh", "aaa-stale"])

    def test_df_computed_over_candidates_only(self):
        # dormant/pinned entries are outside the JIT candidate set, so they
        # must not dilute df either
        self.seed_prior("live-one", "s", conf=0.8, keywords="sharedkw")
        self.seed_prior("dorm-one", "s", conf=0.3, keywords="sharedkw")
        self.seed_prior("pin-one", "s", conf=1.0, keywords="sharedkw", pin="true")
        df = store._df_map(store._jit_candidates(store.load_all()))
        self.assertEqual(df["sharedkw"], 1)


class LexiconTest(StoreBase):
    def test_write_and_term_match(self):
        p = store.write_lexicon({"term": "youable", "definition": "able to be you",
                                 "updated_ts": TS})
        self.assertTrue(p.endswith("lex-youable.md"))
        with open(p) as f:
            raw = f.read()
        self.assertIn("name: lex-youable", raw)
        self.assertIn("  type: lexicon", raw)
        self.assertIn("  scope: global", raw)
        self.assertIn("  definition: able to be you", raw)
        e = self.one(store.load_all(types=("lexicon",)), "youable")
        self.assertEqual((e["term"], e["definition"], e["load_class"]),
                         ("youable", "able to be you", "jit"))
        got = store.resolve_prompt("is this youable at all")
        self.assertEqual([x["id"] for x in got], ["youable"])
        self.assertEqual(store.resolve_prompt("unyouable is not the term"), [])

    def test_keywords_resolve_symptom_phrasing(self):
        # the invariant: symptom vocabulary must fire WITHOUT the term
        store.write_lexicon({"term": "fleet-truth",
                             "definition": "census-derived ground truth",
                             "keywords": "fleet state, stale, still up, seats",
                             "updated_ts": TS})
        for text in ("fleet state seems stale", "is codex-2 still up",
                     "which seats are actually alive",
                     "what does fleet-truth mean"):
            self.assertEqual([x["id"] for x in store.resolve_prompt(text)],
                             ["fleet-truth"], text)
        self.assertEqual(store.resolve_prompt("nothing relevant here"), [])

    def test_legacy_kind_csv_reads_as_keywords(self):
        # pre-fix files carry the keywords CSV under kind: (the add verb once
        # filed it there) — they must keep resolving, unrewritten
        d = self.global_dir("lexicon")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "lex-fleet-truth.md"), "w") as f:
            f.write("---\nname: lex-fleet-truth\n"
                    'description: "lexicon: fleet-truth = census ground truth"\n'
                    "metadata:\n  node_type: memory\n  type: lexicon\n"
                    "  term: fleet-truth\n  scope: global\n"
                    "  kind: fleet state, stale, still up\n"
                    "  definition: census ground truth\n---\n")
        e = self.one(store.load_all(types=("lexicon",)), "fleet-truth")
        self.assertEqual(e["keywords"], "fleet state, stale, still up")
        self.assertEqual([x["id"] for x in
                          store.resolve_prompt("fleet state seems stale")],
                         ["fleet-truth"])

    def test_taxonomy_kind_is_not_a_probe(self):
        # a single-slug kind (the default "phrase" above all) never enters the
        # probe vocabulary — only a legacy CSV does
        store.write_lexicon({"term": "youable", "definition": "able to be you",
                             "kind": "phrase", "updated_ts": TS})
        self.assertEqual(store.resolve_prompt("turn a phrase for me"), [])

    def test_scoped_filename_never_clobbers_global(self):
        store.write_lexicon({"term": "youable", "definition": "global sense"})
        p = store.write_lexicon({"term": "youable", "definition": "project sense",
                                 "term_scope": "project:p1"},
                                root_dir=self.project_dir("p1", "lexicon"))
        self.assertTrue(p.endswith("lex-project-p1--youable.md"))
        e = self.one(store.load_all(project="p1", types=("lexicon",)), "youable")
        self.assertEqual(e["definition"], "project sense")


class CanonSynonymMapTest(StoreBase):
    """Lane 1 of the canon-as-controlled-language design
    (docs/CANON_CONTROLLED_LANGUAGE.md §2): the lexicon synonym-map +
    probe-fold that removes the measured
    DF-split — a concept split across synonym entries halved its own 1/df
    retrieval weight, costing ~5 keyword re-tunes in one session."""

    def test_new_fields_round_trip_preserved(self):
        # The field-default-class bug: parse_simple_frontmatter keeps ONLY keys
        # present in _LEX_DEFAULTS, so a field lacking a default is silently
        # dropped on rewrite (the proven meme:true loss). Every new field ships
        # with a default -> all three survive write -> read -> write.
        p = store.write_lexicon({
            "term": "board-key-drift",
            "definition": "console renders an ad-hoc board key nobody sees",
            "keywords": "boardkey",
            "aliases": "stale-console, unrendered-key",
            "alias_triggers": "staleconsole, unrendered",
            "updated_ts": TS})
        with open(p) as f:
            raw = f.read()
        self.assertIn("  aliases: stale-console, unrendered-key", raw)
        self.assertIn("  alias_triggers: staleconsole, unrendered", raw)
        e = self.one(store.load_all(types=("lexicon",)), "board-key-drift")
        self.assertEqual(e["aliases"], "stale-console, unrendered-key")
        self.assertEqual(e["alias_triggers"], "staleconsole, unrendered")
        # rewrite FROM the parsed entry — the dropped-field failure mode — and
        # confirm nothing is lost the second time either
        store.write_lexicon(e, path=e["path"])
        e2 = self.one(store.load_all(types=("lexicon",)), "board-key-drift")
        self.assertEqual(e2["aliases"], "stale-console, unrendered-key")
        self.assertEqual(e2["alias_triggers"], "staleconsole, unrendered")
        # the stub back-pointer (canonical:) round-trips too
        store.write_lexicon({"term": "stale-console", "definition": "see canonical",
                             "canonical": "board-key-drift", "updated_ts": TS})
        stub = store._find("stale-console", types=("lexicon",))
        self.assertEqual(stub["canonical"], "board-key-drift")

    def test_alias_trigger_folds_into_canonical_resolve(self):
        # The fold (resolve._probes): an alias's probe form, carried in
        # alias_triggers on the CANONICAL entry, fires the canonical — the
        # "index on read" direction. The alias words are NOT in the canonical's
        # keywords, so ONLY the fold can resolve them.
        store.write_lexicon({
            "term": "board-key-drift",
            "definition": "the console renders an ad-hoc board key nobody sees",
            "keywords": "boardkey",
            "aliases": "stale-console, unrendered-key",
            "alias_triggers": "staleconsole, unrendered",
            "updated_ts": TS})
        # the alias_triggers land in the canonical entry's probe set
        e = self.one(store.load_all(types=("lexicon",)), "board-key-drift")
        probes = {p for p, _g in store._probes(e)}
        self.assertIn("staleconsole", probes)
        self.assertIn("unrendered", probes)
        # an alias word in the turn resolves to the canonical entry
        for text in ("the staleconsole is back", "an unrendered value here"):
            self.assertEqual(
                [x["id"] for x in store.resolve_prompt(text)],
                ["board-key-drift"], text)
        # and its own keyword + term still resolve
        self.assertEqual([x["id"] for x in store.resolve_prompt("a boardkey issue")],
                         ["board-key-drift"])

    def test_canonical_keeps_full_df_weight_no_split(self):
        # The DF-split reproduced and fixed. The concept lives in ONE canonical
        # entry; the synonym persists as a get-redirect STUB (canonical:
        # back-pointer) sharing the probe. Before Lane 1 both entries carried
        # `consolekey` so df[consolekey]=2 and each scored conf x 1/2 -> the
        # synonyms crowded the cap-4 and one was dropped. Now the stub leaves the
        # denominator: df drops to 1 and the canonical scores at full 1/1 weight.
        store.write_lexicon({
            "term": "board-key-drift",
            "definition": "console renders an ad-hoc board key",
            "keywords": "consolekey",
            "aliases": "stale-console",
            "updated_ts": TS})
        store.write_lexicon({
            "term": "stale-console",
            "definition": "synonym; see board-key-drift",
            "keywords": "consolekey",
            "canonical": "board-key-drift",
            "updated_ts": TS})
        cand = store._jit_candidates(store.load_all())
        df = store._df_map(cand)
        # the shared probe's df is 1, not 2 — the stub does not dilute the weight
        self.assertEqual(df["consolekey"], 1)
        # the stub stays loadable (get-redirect) but is NOT a resolve candidate
        self.assertIn("stale-console",
                      [e["id"] for e in store.load_all(types=("lexicon",))])
        self.assertNotIn("stale-console", [e["id"] for e in cand])
        # a "consolekey" turn resolves to the canonical at full weight, no stub
        got = [e["id"] for e in store.resolve_prompt("a consolekey went stale")]
        self.assertEqual(got, ["board-key-drift"])

    def test_canonical_outranks_a_generic_rival_only_via_full_weight(self):
        # The effect, asserted as a rank (not the absence of a complaint): a
        # canonical entry that shares probe `consolekey` with a legit-DISTINCT
        # rival must still beat it on a turn carrying the shared probe + one rare
        # canonical probe, BECAUSE the alias stub no longer inflates df. With the
        # stub competing (df 3) the arithmetic that seats the canonical erodes.
        store.write_lexicon({
            "term": "board-key-drift", "definition": "canonical concept",
            "keywords": "consolekey, driftrare", "aliases": "stale-console",
            "updated_ts": TS})
        store.write_lexicon({
            "term": "stale-console", "definition": "synonym stub",
            "keywords": "consolekey", "canonical": "board-key-drift",
            "updated_ts": TS})
        store.write_lexicon({
            "term": "render-budget", "definition": "a genuinely distinct concept",
            "keywords": "consolekey", "updated_ts": TS})
        # df[consolekey] = 2 (canonical + the distinct rival); the stub is out
        cand = store._jit_candidates(store.load_all())
        self.assertEqual(store._df_map(cand)["consolekey"], 2)
        got = [e["id"] for e in store.resolve_prompt("the consolekey driftrare case")]
        self.assertEqual(got[0], "board-key-drift")
        self.assertNotIn("stale-console", got)


class HeuristicTest(StoreBase):
    def test_always_jit_conf_one(self):
        p = store.write_heuristic({"id": "swarmify", "move": "split into waves",
                                   "trigger": "swarm,waves", "stated_ts": TS,
                                   "load_class": "always"})  # authored always is IGNORED
        with open(p) as f:
            self.assertIn("  load_class: jit", f.read())
        e = self.one(store.load_all(types=("heuristic",)), "swarmify")
        self.assertEqual((e["confidence"], e["load_class"], e["statement"]),
                         (1.0, "jit", "split into waves"))
        got = store.resolve_prompt("the swarm is restless")
        self.assertEqual([x["id"] for x in got], ["swarmify"])

    def test_heuristic_generic_trigger_guard(self):
        store.write_heuristic({"id": "vague-move", "move": "do the thing",
                               "trigger": "strategy,move", "stated_ts": TS})
        self.assertEqual(store.resolve_prompt("what strategy should we move on"), [])

    def test_description_fallback_for_live_store_shape(self):
        # the real adopted store carries heuristics whose move lives ONLY in the
        # description line — they must still resolve, not silently vanish
        pk.atomic_write(os.path.join(self.adopted, "heuristic-hand-authored.md"),
                        '---\nname: heuristic-hand-authored\n'
                        'description: "compose primitives, always"\nmetadata:\n'
                        '  node_type: memory\n  type: heuristic\n  id: hand-authored\n'
                        '  trigger: composeall\n  status: live\n---\nbody\n')
        e = self.one(store.load_all(types=("heuristic",)), "hand-authored")
        self.assertEqual(e["statement"], "compose primitives, always")
        self.assertEqual([x["id"] for x in store.resolve_prompt("composeall now")],
                         ["hand-authored"])


class ReferenceTest(StoreBase):
    def test_round_trip_and_resolve(self):
        p = store.write_reference({"id": "dregg-mandates", "statement":
                                   "Lean-proven mandate crown", "url": "https://x.test/d",
                                   "keywords": "dregg,mandate", "stated_ts": TS})
        with open(p) as f:
            raw = f.read()
        self.assertIn("name: ref-dregg-mandates", raw)
        self.assertIn("  type: reference", raw)
        self.assertIn("  url: https://x.test/d", raw)
        self.assertIn("  load_class: jit", raw)
        e = self.one(store.load_all(types=("reference",)), "dregg-mandates")
        self.assertEqual((e["type"], e["confidence"], e["url"]),
                         ("reference", 1.0, "https://x.test/d"))
        got = store.resolve_prompt("the dregg weld")
        self.assertEqual([x["id"] for x in got], ["dregg-mandates"])

    def test_live_store_shape_name_description_only(self):
        # the one real ref-* file today has NO id/summary fields — name +
        # description must carry it
        pk.atomic_write(os.path.join(self.adopted, "ref-bare.md"),
                        '---\nname: ref-bare\ndescription: "a harvested nugget"\n'
                        'metadata: \n  node_type: memory\n  type: reference\n---\nbody\n')
        e = self.one(store.load_all(types=("reference",)), "bare")
        self.assertEqual(e["statement"], "a harvested nugget")
        self.assertEqual(e["load_class"], "jit")


class EpisodicTest(StoreBase):
    def test_episodic_dormant_never_resolves(self):
        pk.atomic_write(os.path.join(self.adopted, "war-story.md"),
                        '---\nname: war-story\ndescription: "the glorp incident"\n'
                        'metadata:\n  node_type: memory\n  type: project\n---\nbody\n')
        pk.atomic_write(os.path.join(self.adopted, "MEMORY.md"), "# index\n- stuff\n")
        pk.atomic_write(os.path.join(self.adopted, "reflex-someone-elses.md"),
                        '---\nname: reflex-someone-elses\ndescription: "not ours"\n---\n')
        es = store.load_all()
        e = self.one(es, "war-story")
        self.assertEqual((e["type"], e["load_class"], e["memory_type"]),
                         ("episodic", "dormant", "project"))
        self.assertNotIn("MEMORY", [x["id"] for x in es])
        self.assertEqual([x for x in es if x["id"].startswith("reflex")], [])
        self.assertEqual(store.resolve_prompt("the glorp incident war-story"), [])
        self.assertEqual(store.load_all(include_dormant=False), [])


class TypedFallbackTest(StoreBase):
    def test_typed_prefix_without_typed_fields_reads_as_episodic(self):
        # the live store carries prem-/lex- named files that are really bulk
        # memory (name+description, type: project, no id/statement) — the predecessor drops
        # them; helm keeps them visible as episodic, never injected
        pk.atomic_write(os.path.join(self.adopted, "prem-bulk-note.md"),
                        '---\nname: prem-bulk-note\ndescription: "canon paragraph"\n'
                        'metadata:\n  node_type: memory\n  type: project\n---\nbody\n')
        e = self.one(store.load_all(), "prem-bulk-note")
        self.assertEqual((e["type"], e["load_class"]), ("episodic", "dormant"))
        self.assertEqual(store.resolve_prompt("prem-bulk-note canon paragraph"), [])


class ScopeTest(StoreBase):
    def test_project_shadows_global_shadows_adopted(self):
        store.write_prior({"id": "foo", "statement": "adopted sense",
                          "confidence": 0.9}, root_dir=self.adopted)
        e = self.one(store.load_all(), "foo")
        self.assertEqual((e["statement"], e["root"]), ("adopted sense", "adopted"))
        self.seed_prior("foo", "global sense", conf=0.9)
        e = self.one(store.load_all(), "foo")
        self.assertEqual((e["statement"], e["root"]), ("global sense", "helm-global"))
        self.seed_prior("foo", "project sense", conf=0.9,
                        root_dir=self.project_dir("p1", "premises"))
        e = self.one(store.load_all(project="p1"), "foo")
        self.assertEqual((e["statement"], e["root"], e["scope"]),
                         ("project sense", "project", "project:p1"))
        # without the project lens the project root is not in play
        e = self.one(store.load_all(), "foo")
        self.assertEqual(e["root"], "helm-global")

    def test_retiring_shadow_winner_never_resurrects_the_shadowed(self):
        # audit HIGH: a stale wide-scope belief shadowed by a narrow-scope
        # override must STAY buried when the override is retired/superseded —
        # per-root status filtering resurrected it as live.
        store.write_prior({"id": "shipfast", "statement": "old stale belief",
                          "confidence": 0.6, "keywords": "shipfast"},
                          root_dir=self.adopted)
        self.seed_prior("shipfast", "NEW corrected belief", conf=0.9,
                        keywords="shipfast",
                        root_dir=self.project_dir("myproj", "premises"))
        e = self.one(store.load_all(project="myproj"), "shipfast")
        self.assertEqual(e["root"], "project")
        _, err = store.retire("shipfast", TS, "no longer holds", project="myproj")
        self.assertIsNone(err)
        # the retired project override does NOT un-bury the adopted copy
        self.assertEqual(store.load_all(project="myproj"), [])
        self.assertEqual(store.resolve_prompt("shipfast plan", project="myproj"), [])
        # asked-for retired view shows the shadow winner, tombstoned
        e = self.one(store.load_all(project="myproj", include_retired=True), "shipfast")
        self.assertEqual((e["root"], e["status"]), ("project", "retired"))
        # the GLOBAL lens (no project root in play) still sees the adopted copy
        e = self.one(store.load_all(), "shipfast")
        self.assertEqual((e["root"], e["status"]), ("adopted", "live"))

    def test_superseding_shadow_winner_never_resurrects_the_shadowed(self):
        store.write_prior({"id": "shipfast", "statement": "old stale belief",
                          "confidence": 0.6}, root_dir=self.adopted)
        self.seed_prior("shipfast", "project override", conf=0.9,
                        root_dir=self.project_dir("myproj", "premises"))
        self.seed_prior("shipslow", "the replacement", conf=0.9,
                        root_dir=self.project_dir("myproj", "premises"))
        _, err = store.mark_superseded("shipfast", "shipslow", TS,
                                       "replaced", project="myproj")
        self.assertIsNone(err)
        self.assertEqual([e["id"] for e in store.load_all(project="myproj")],
                         ["shipslow"])

    def test_no_cross_type_shadowing(self):
        self.seed_prior("shared-slug", "the belief", conf=0.9)
        store.write_lexicon({"term": "shared-slug", "definition": "the term"})
        self.assertEqual(len(store.load_all()), 2)


class PinnedTest(StoreBase):
    def test_ordering_and_flag_survival(self):
        self.seed_prior("pin-b", "belief pin", conf=0.9, pin="true")
        self.seed_prior("pin-a", "certain pin", conf=1.0, pin="true")
        self.seed_prior("unpinned", "not pinned", conf=1.0)
        got = store.pinned()
        self.assertEqual([e["id"] for e in got], ["pin-a", "pin-b"])
        self.assertTrue(all(e["load_class"] == "always" for e in got))
        # the flag survives the file round-trip
        with open(got[1]["path"]) as f:
            self.assertIn("  pin: true", f.read())

    def test_walk_order_confidence_recency_id(self):
        # the deterministic budget-walk law: confidence desc, then recency desc
        # (mixed ISO/epoch/blank timestamps all comparable), then id asc — the
        # old (-conf, id) key made an all-1.0 lane alphabetical (starvation)
        self.seed_prior("b-old", "iso ts", conf=1.0, pin="true",
                        last_updated="2026-06-01")
        self.seed_prior("a-new", "epoch ts newer", conf=1.0, pin="true",
                        last_updated="1782000000")  # ~2026-06-21 > 2026-06-01
        self.seed_prior("t-x", "iso tie", conf=1.0, pin="true",
                        last_updated="2026-06-01")
        self.seed_prior("z-blank", "no ts ranks last", conf=1.0, pin="true",
                        stated_ts="", last_updated="")
        self.seed_prior("c-low", "freshest but lower conf", conf=0.9, pin="true",
                        last_updated="2026-07-01")
        want = ["a-new", "b-old", "t-x", "z-blank", "c-low"]
        self.assertEqual([e["id"] for e in store.pinned()], want)
        self.assertEqual([e["id"] for e in store.pinned()], want)  # deterministic

    def test_explicit_unpin_beats_pinned_slugs_tuple(self):
        self.seed_prior("drift-is-the-enemy", "founding pin", conf=1.0)
        e = self.one(store.load_all(), "drift-is-the-enemy")
        self.assertTrue(e["pinned"])  # the tuple pins it with no flag at all
        store.write_prior({"id": "drift-is-the-enemy", "statement": "founding pin",
                           "confidence": 1.0, "pin": "false", "load_class": "jit",
                           "stated_ts": TS}, path=e["path"])
        e = self.one(store.load_all(), "drift-is-the-enemy")
        self.assertFalse(e["pinned"])
        self.assertEqual(e["load_class"], "jit")
        with open(e["path"]) as f:
            self.assertIn("  pin: false", f.read())  # the un-pin survives rewrites


class DemoteTest(StoreBase):
    def test_demote_flips_with_receipt_never_silent(self):
        self.seed_prior("pin-a", "always on", conf=0.9, pin="true")
        e, err = store.demote("pin-a", TS, "starved under the byte budget")
        self.assertIsNone(err)
        e = self.one(store.load_all(), "pin-a")  # reload: the flip is on disk
        self.assertEqual((e["load_class"], e["pinned"], e["status"]),
                         ("jit", False, "live"))
        self.assertEqual(store.pinned(), [])
        last = e["evidence_log"][-1]
        self.assertEqual((last["type"], last["by"], last["reason"]),
                         ("demoted", "human", "starved under the byte budget"))
        self.assertEqual(last["was"], {"load_class": "always", "pin": True})

    def test_demote_founding_pin_sticks(self):
        self.seed_prior("drift-is-the-enemy", "founding", conf=1.0)
        e, err = store.demote("drift-is-the-enemy", TS, "measured starved")
        self.assertIsNone(err)
        e = self.one(store.load_all(), "drift-is-the-enemy")
        self.assertEqual((e["load_class"], e["pinned"]), ("jit", False))

    def test_undo_restores_via_the_receipt(self):
        self.seed_prior("pin-a", "always on", conf=0.9, pin="true")
        store.demote("pin-a", TS, "starved")
        e, err = store.demote("pin-a", TS, "load-bearing after all", undo=True)
        self.assertIsNone(err)
        e = self.one(store.load_all(), "pin-a")
        self.assertEqual((e["load_class"], e["pinned"]), ("always", True))
        self.assertEqual([r["type"] for r in e["evidence_log"]],
                         ["demoted", "undemoted"])

    def test_demote_guards(self):
        self.seed_prior("pin-a", "always on", conf=0.9, pin="true")
        self.seed_prior("plain", "jit entry", conf=0.8)
        for args, msg in ((("pin-a", TS, " "), "reason is required"),
                          (("plain", TS, "why"), "not in the always lane"),
                          (("ghost", TS, "why"), "not found")):
            e, err = store.demote(*args)
            self.assertIsNone(e)
            self.assertIn(msg, err)
        e, err = store.demote("plain", TS, "why", undo=True)
        self.assertIn("no demote receipt", err)
        e, err = store.demote("pin-a", TS, "why", undo=True)
        self.assertIn("already in the always lane", err)

    def test_demote_reference_flip_and_undo(self):
        store.write_reference({"id": "ref-a", "statement": "harvested",
                               "load_class": "always", "stated_ts": TS})
        e, err = store.demote("ref-a", TS, "starved")
        self.assertIsNone(err)
        self.assertEqual(self.one(store.load_all(), "ref-a")["load_class"], "jit")
        e, err = store.demote("ref-a", TS, "restore", undo=True)
        self.assertIsNone(err)
        self.assertEqual(self.one(store.load_all(), "ref-a")["load_class"], "always")


class PinnedStatsTest(StoreBase):
    """pinned --stats: the budget walk as inject renders it now + made-it
    counts over the fire-ledger window — read-only against a planted ledger."""

    def setUp(self):
        super().setUp()
        # 4 pins, each line rendering at exactly LINE_CAP (400B) -> 3 fit the
        # budget, pin-3 (same conf, same ts, id-last) starves. Whole lines, not
        # cut ones: a cut line stops short of the cap by the width of the
        # route the lane footer carries (inject._entries._cut)
        for i in range(4):
            self.seed_prior("pin-%d" % i, "x" * (400 - len("PREMISE pin-0: ")),
                            conf=1.0, pin="true")

    def ledger(self, rows):
        from helm import inject
        path = inject._ledger_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write("\n".join(rows) + "\n")
        return path

    def test_a_capability_gate_resolves_through_pinned_stats_itself(self):
        """THE CORPUS PARITY ARM, DRIVEN THROUGH THE BROKEN SURFACE.

        My first version of this arm called inject.pinned_lane() and
        pinned_admission() directly and never touched pinned_stats — so
        reverting pinned_stats to store.load_all() left it GREEN. An arm for a
        defect must exercise the surface that HAD the defect; testing the
        composer proves the composer, which was never wrong.

        gather walks store entries PLUS the live capability self-index.
        pinned_stats used to walk load_all(), which holds no capabilities at
        all, so a pinned rule whose GATE names a capability rendered its rider
        to the seat and read as a MISSING gate here.
        """
        from unittest import mock
        from helm import inject, store as st
        cap = {"id": "cap-probe", "type": "capability", "class": "capability",
               "statement": "you have probe: a synthetic capability",
               "confidence": 1.0, "load_class": "jit", "status": "live",
               "stated_ts": TS}
        # THE SAME-SLUG DECOY. gate_key is (type, slug) and
        # NEVER the bare slug, so `cap-probe` names TWO entries here: the live
        # capability and this stale prior. A BARE gate — which is what this arm
        # used to declare — cannot say which one it meant, so the arm passed
        # whether the walk resolved the capability or merely something spelled
        # the same. With the decoy present a bare gate is genuinely ambiguous,
        # which is the condition that makes the typed form load-bearing rather
        # than decorative.
        decoy = {"id": "cap-probe", "type": "prior", "load_class": "always",
                 "statement": "a STALE prior sharing the capability's slug "
                              + "y" * 40,
                 "confidence": 1.0, "stated_ts": TS}
        rule = {"id": "gated-rule", "type": "prior", "load_class": "always",
                "statement": "a rule gated on a capability " + "z" * 40,
                "confidence": 1.0, "stated_ts": TS,
                # TYPED, not bare: `capability:cap-probe`. split_gate reads the
                # prefix as the store type, so this names the capability and
                # cannot be satisfied by the decoy above.
                "gates": "capability:cap-probe"}
        real = inject._entries._lane_entries

        def corpus(extra):
            # patched at the DEFINITION module: pinned_lane calls
            # _lane_entries through its own global, so patching a re-export
            # would leave the real one running and this arm green over an
            # unpatched world.
            return lambda project=None: list(real(project)) + extra

        with mock.patch.object(inject._entries, "_lane_entries",
                               corpus([rule, cap, decoy])):
            stats = st.pinned_stats()
            always, entries = inject.pinned_lane()
            walk = inject.pinned_admission(always, entries)
        # STATS CONSUMES THE WALK, on the corpus that holds the capability.
        self.assertEqual(stats["used"], walk["used"])
        self.assertIn("gated-rule", stats["fits"])

        # MUST-FAIL CONTROL: on a corpus WITHOUT the capability — which is
        # exactly what reverting pinned_stats to load_all() produces — the SAME
        # rule yields a different charged total, because the gate degrades from
        # a rendered rider to a missing-gate marker. Without this the assertion
        # above holds under the reverted code too and proves nothing.
        # The revert corpus keeps the DECOY and drops only the capability, so
        # the difference below is attributable to the capability's absence and
        # not to a smaller corpus: a bare-gate implementation would resolve the
        # decoy here and report no change at all.
        with mock.patch.object(inject._entries, "_lane_entries",
                               corpus([rule, decoy])):
            reverted = st.pinned_stats()
        self.assertNotEqual(
            stats["used"], reverted["used"],
            "a capability-gated rule must charge differently when the "
            "capability is absent from the corpus — if these agree, this arm "
            "cannot see the defect it was written for")

    def test_stats_consumes_the_walk_it_does_not_model_it(self):
        """THE MODEL MUST BE THE WALK, NOT A LIKENESS OF IT.

        This surface has been wrong THREE times, each time by re-deriving an
        answer inject already computed, and each time it kept returning a
        plausible number: used=0 (optimistic, 3 fitting where 2 did),
        seeded-with-WHO (correct until the 2026-08-28 ruling took WHO out of
        PINNED_BUDGET), and a line-text match against the rendered lane.

        THIS ARM WAS ITSELF ROUND TWO. It asserted that a 400B digest leaves
        800B so only 2 entries fit -- the shared-budget accounting the ruling
        (chat 2044 clauses 1-2, amended by row 64f555da38e6) REMOVED. WHO now
        walks last against WHO_CAP as its whole budget and charges the rules
        nothing, so that specimen no longer discriminates anything. It is
        replaced below by one that does."""
        from helm import inject, store as st
        base = st.pinned_stats()
        self.assertEqual(len(base["fits"]), 3, "3 x 400 fit; the 4th starves")

        # THE RULED LAW: the digest costs the rules NOTHING. A 400B WHO must
        # not move the rule count, and `used` counts RULE bytes only.
        with mock.patch.object(inject, "_who_lines", return_value=["W" * 400]):
            with_who = st.pinned_stats()
        self.assertEqual(len(with_who["fits"]), 3,
                         "WHO walks last on WHO_CAP; it cannot starve a rule")
        self.assertEqual(with_who["used"], base["used"],
                         "used is the RULE total; the digest is not in it")

        # THE DISCRIMINATOR, and the reason this arm still earns its place: a
        # GATE-EARNED entry is DELIVERED -- it keeps the slot it earned -- but
        # renders through _gate_line, not _entry_line. A stats that recovers
        # `fits` by matching rendered line TEXT (the round-three defect) files
        # it as starved while the seat is reading it. Only a stats that reads
        # the PLAN's own identities gets this right.
        st.regate("pin-0", "2026-08-28T00:00:00Z", add="pin-1")
        st.regate("pin-1", "2026-08-28T00:00:00Z", add="pin-0")
        entries = st.load_all()
        adm = inject.pinned_admission(st.pinned(entries=entries), entries)
        kinds = [it[0] for it in adm["plan"]]
        self.assertIn("gate-earned", kinds,
                      "must-hit: the specimen really produced a gate-earned "
                      "item, else the assertion below proves nothing")
        earned = set(str(it[1]["id"]) for it in adm["plan"]
                     if it[6] and it[1] is not None and it[0] == "gate-earned")
        self.assertTrue(earned <= set(st.pinned_stats()["fits"]),
                        "a delivered gate-earned entry counts as FITTING")

        # fail-open contract: a digest that cannot be built costs nothing
        with mock.patch.object(inject, "_who_lines", side_effect=RuntimeError):
            self.assertEqual(len(st.pinned_stats()["fits"]), 3)

    def run_cli(self, args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = store.cmd_store(list(args))
        return rc, out.getvalue()

    def test_stats_walk_and_ledger_window_read_only(self):
        row = lambda ids: json.dumps(
            {"v": 1, "ts": "2026-07-19T00:00:00Z", "fired": {"pinned": ids}})
        path = self.ledger([row(["pin-0"]), row(["pin-0"]), row(["pin-0"]),
                            '{"v":1,"ts":"x","silent":true}', "not json",
                            row(["pin-1", "ghost-id"])])
        with open(path, "rb") as f:
            before = f.read()
        rc, out = self.run_cli(["pinned", "--stats"])
        self.assertEqual(rc, 0)
        # DERIVED from the constant, not transcribed: this hardcoded 1200
        # and broke when the budget moved for reasons unrelated to what it
        # asserts (that the stats line names the count and the budget).
        from helm import inject as _inj
        self.assertIn("4 always, budget %dB" % _inj.PINNED_BUDGET, out)
        self.assertIn("+ pin-0  made 3/4", out)
        self.assertIn("+ pin-1  made 1/4", out)  # ghost-id ignored, row counted
        self.assertIn("+ pin-2  made 0/4", out)
        self.assertIn("- pin-3  made 0/4", out)  # over budget NOW and starved
        # THE TWO CASES ARE REPORTED APART, because the owner's action differs.
        # This used to read "2 of 4 NEVER made the budget: pin-2, pin-3", which
        # lumped a structurally-dead entry together with a merely-new one and
        # offered `demote` for both. pin-3 cannot fit and needs something ahead
        # of it shortened; pin-2 fits today and has simply not landed yet.
        self.assertIn("1 of 4 always-entries CANNOT FIRE", out)
        # THE FORM, not the arithmetic: this asserted "1200/1200 bytes used",
        # which is true only when the fixture happens to fill the budget
        # exactly. The arm's subject is the stats LINE and the read-only walk,
        # so it pins the denominator (which must be the live budget) and that
        # a used-figure is reported at all.
        used_line = re.search(r"(\d+)/(\d+) bytes used", out)
        self.assertIsNotNone(used_line, out)
        self.assertEqual(int(used_line.group(2)), _inj.PINNED_BUDGET)
        self.assertLessEqual(int(used_line.group(1)), _inj.PINNED_BUDGET)   # names WHY it cannot fire
        self.assertIn("shorten an entry ahead of them", out)
        self.assertIn("1 of 4 fit TODAY but never made the budget", out)
        for line in out.splitlines():
            if "CANNOT FIRE" in line or "shorten an entry" in line:
                self.assertNotIn("pin-2", line,
                                 "an entry that FITS must never be reported as "
                                 "unable to fire")
        self.assertIn("helm store demote <id>", out)
        with open(path, "rb") as f:
            self.assertEqual(f.read(), before)  # stats never writes the ledger

    def test_stats_without_ledger(self):
        rc, out = self.run_cli(["pinned", "--stats"])
        self.assertEqual(rc, 0)
        self.assertIn("no fire-ledger rows with a pinned lane yet", out)
        self.assertIn("- pin-3", out)  # the walk still names the over-budget entry


class EntriesSeamTest(StoreBase):
    """pinned/resolve_prompt entries= — inject's parsed-entry cache feeds both
    lanes through this seam with ZERO load_all() calls (the durable one-parse
    fix), and the supplied list gets exactly the load_all-path post-filters."""

    def synth(self, eid, load_class="jit", etype="prior", keywords="", conf=0.8):
        return {"id": eid, "type": etype, "statement": "s", "confidence": conf,
                "load_class": load_class, "keywords": keywords, "last_updated": ""}

    def test_supplied_entries_never_touch_disk(self):
        entries = [self.synth("pin-a", load_class="always", conf=1.0),
                   self.synth("jit-a", keywords="fluxcap"),
                   self.synth("dorm-a", load_class="dormant", keywords="fluxcap"),
                   self.synth("epi-a", etype="episodic", keywords="fluxcap")]
        with mock.patch.object(store, "load_all",
                               side_effect=AssertionError("entries= must skip load_all")):
            got_pinned = store.pinned(entries=entries)
            got_jit = store.resolve_prompt("tune the fluxcap", entries=entries)
        self.assertEqual([e["id"] for e in got_pinned], ["pin-a"])
        # dormant/episodic/always filtered exactly as the load_all path would
        self.assertEqual([e["id"] for e in got_jit], ["jit-a"])

    def test_df_scoring_stable_through_seam(self):
        # the seam path scores identically to the disk path: DF over the
        # supplied candidate list, rare probe outranks the shared one
        entries = [self.synth("shared-a", keywords="commonkw"),
                   self.synth("shared-b", keywords="commonkw"),
                   self.synth("rare-c", keywords="rarekw")]
        with mock.patch.object(store, "load_all",
                               side_effect=AssertionError("entries= must skip load_all")):
            got = store.resolve_prompt("commonkw rarekw mix", entries=entries)
        self.assertEqual([e["id"] for e in got], ["rare-c", "shared-a", "shared-b"])


class CountsTest(StoreBase):
    def test_per_root_type_counts(self):
        store.write_prior({"id": "a", "statement": "s", "confidence": 0.9},
                          root_dir=self.adopted)
        pk.atomic_write(os.path.join(self.adopted, "war-story.md"),
                        '---\nname: war-story\ndescription: "d"\n---\n')
        self.seed_prior("b", "s", conf=0.9)
        store.write_lexicon({"term": "youable", "definition": "d"})
        store.write_heuristic({"id": "h", "move": "m", "trigger": "t"})
        store.write_reference({"id": "r", "statement": "s"})
        self.seed_prior("c", "s", conf=0.9,
                        root_dir=self.project_dir("p1", "premises"))
        c = store.counts(project="p1")
        self.assertEqual(c["adopted"], {"prior": 1, "episodic": 1})
        self.assertEqual(c["helm-global"],
                         {"prior": 1, "lexicon": 1, "heuristic": 1, "reference": 1})
        self.assertEqual(c["project"], {"prior": 1})


class ScopeIsRenderedAsWhatGovernsTest(StoreBase):
    """THE READ-BACK THE RESCOPE DOOR OWED (task/2447, reported through the
    helm-user umbrella).

    `entry_scope` reads the entry's own `project` field FIRST and the storage
    root only as a fallback — that is what makes `rescope` the door. But `list`
    and `get` rendered the STORAGE scope, which answers a different question:
    where the file sits. Every entry in the global and adopted roots therefore
    read `global` no matter what had been recorded about it, so an operator
    recording a scope and checking their work through the obvious surface saw
    nothing change and concluded the write had not landed.

    Measured on the live store before the cure: 255 of 255 entries carrying a
    recorded project rendered `global` — rows recorded `helm` and rows recorded
    `fleet` rendering identically, and oppositely wrong.
    """

    def run_cli(self, args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = store.cmd_store(list(args))
        return rc, out.getvalue(), err.getvalue()

    def test_list_renders_the_recorded_scope_not_the_storage_root(self):
        """Both rows live in the SAME global root, so the root cannot tell them
        apart and only the recorded field can. The derived row is the positive
        control: it is asserted unconditionally in the same output, so a
        listing that simply stopped printing scopes cannot pass this."""
        self.seed_prior("recorded-row", "a truth about one project",
                        project="helm")
        self.seed_prior("derived-row", "a truth with no record")
        rc, out, err = self.run_cli(["list"])
        self.assertEqual(rc, 0, err)
        rec = [l for l in out.splitlines() if "recorded-row" in l][0]
        der = [l for l in out.splitlines() if "derived-row" in l][0]
        self.assertIn("helm]", rec)
        self.assertNotIn("global", rec)
        self.assertIn("(derived)", der)       # control: the other answer shows

    def test_derived_is_marked_because_it_is_the_backlog(self):
        """`(derived)` is the difference between an entry somebody classified
        and one merely FALLING toward fleet — which is exactly the set the
        scope census counts as work remaining."""
        self.seed_prior("fleet-recorded", "owner policy", project="fleet")
        self.seed_prior("fleet-derived", "unrecorded truth")
        rc, out, err = self.run_cli(["list"])
        self.assertEqual(rc, 0, err)
        rec = [l for l in out.splitlines() if "fleet-recorded" in l][0]
        der = [l for l in out.splitlines() if "fleet-derived" in l][0]
        self.assertIn("fleet]", rec)
        self.assertNotIn("(derived)", rec)
        self.assertIn("fleet (derived)]", der)

    def test_get_answers_both_questions_and_does_not_conflate_them(self):
        """`root` (where it sits) and `scope` (what governs it) are different
        facts and a rescoped entry is precisely where they differ, so `get`
        prints both under their own names rather than one under the other's."""
        self.seed_prior("both-facts", "a truth about one project",
                        project="helm")
        rc, out, err = self.run_cli(["get", "both-facts"])
        self.assertEqual(rc, 0, err)
        self.assertIn("root=helm-global", out)
        self.assertIn("scope=helm", out)

    def test_the_listing_agrees_with_the_fence_on_every_entry(self):
        """The one property that matters: whatever the listing prints is what
        actually decides delivery. Asserted over the WHOLE seeded set rather
        than a chosen row, because the defect was a whole-column one."""
        self.seed_prior("a", "x", project="helm")
        self.seed_prior("b", "y", project="fleet")
        self.seed_prior("c", "z")
        es = store.load_all()
        self.assertEqual(len(es), 3)          # control: the probe sees them
        for e in es:
            self.assertTrue(
                store.scope_label(e).startswith(store.entry_project(e)),
                "the rendered scope must name the project that governs %s"
                % e["id"])


class RescopeCliTest(StoreBase):
    """THE RECORD-WRITING DOOR (task/2435): `helm store rescope` is what turns a
    DERIVED scope into a RECORDED one, so its operand contract is the registry's
    contract and its success line is what the operator reads instead of running
    the derivation in their head.

    TWO DEFECTS THESE ARMS PIN, both found by review of the door rather than of
    the fence it feeds:

    1. THE OPERAND WAS NORMALIZED, NOT VALIDATED. Internal whitespace was
       stripped before the name was recorded, so a registry identity carrying a
       space was recorded under a DIFFERENT name — and every downstream reader
       compares the recorded project EXACTLY, so the rewritten name matched no
       project at all while the CLI reported the operand back unchanged. The
       cure is to ask the registry's own name contract and to print what was
       recorded.
    2. THE CLEAR-OWNER SUCCESS LINE PROMISED FLEET. Clearing a record returns
       an entry to its ROOT's default, and for an entry written under a project
       root that default is THAT PROJECT. A line that says fleet is wrong for
       exactly the rows an operator is most likely to clear by mistake, so the
       line now reports the effective owner the derivation yields, after the
       clear, with how it got there.
    """

    def run_cli(self, args, stdin=None):
        out, err = io.StringIO(), io.StringIO()
        old_stdin = sys.stdin
        if stdin is not None:
            sys.stdin = io.StringIO(stdin)
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = store.cmd_store(args)
        finally:
            sys.stdin = old_stdin
        return rc, out.getvalue(), err.getvalue()

    def reread(self, eid, project=None):
        return self.one(store.load_all(project=project), eid)

    # -- (d) the operand is the registry's name, exactly -------------------
    def test_a_registry_name_carrying_a_space_round_trips_exactly(self):
        """A registry project name may carry internal spaces — the registry's own
        population contract refuses only an empty name, a dot name and a path
        separator — so the door must RECORD the name it was handed and the fence
        must then admit that entry for that project."""
        # MUST-HIT THROUGH THE CANONICAL VALIDATOR ITSELF, not a restatement
        # of it: the registry's own strict population reader admits this name,
        # so the arm below is about a real registry identity.
        from helm import registry
        registry._checked_value({"version": 1, "projects": {
            "client site": {"name": "client site", "path": "/x"}}})
        self.seed_prior("spaced-row", "a truth about one project")
        rc, out, err = self.run_cli(["rescope", "spaced-row", "client site"])
        self.assertEqual(rc, 0, err)
        e = self.reread("spaced-row")
        self.assertEqual(e.get("project"), "client site",
                         "the recorded name is the operand, byte for byte")
        self.assertEqual(store.entry_project(e), "client site")
        self.assertIn("client site", out)
        self.assertNotIn("clientsite", out,
                         "the CLI reports the operand AS RECORDED, so a "
                         "rewritten name can never be reported as the name")

    def test_an_invalid_name_is_refused_and_nothing_is_written(self):  # noqa: VACUOUS_ASSERTION — the same row goes through the same door with a VALID name afterwards and writes bytes, mints a receipt and prints a success line
        """The refusal is the registry's contract, asked through its ONE
        spelling, and it lands BEFORE any writer runs: the entry's bytes are
        unchanged and no receipt is minted."""
        from helm import registry
        with self.assertRaises(ValueError):
            registry._checked_value({"version": 1, "projects": {
                "bad/name": {"name": "bad/name", "path": "/x"}}})
        self.seed_prior("kept-row", "a truth that must not move")
        before_path = self.reread("kept-row")["path"]
        with open(before_path, "rb") as f:
            before = f.read()
        events_before = len(pk.read_events(500))
        rc, out, err = self.run_cli(["rescope", "kept-row", "bad/name"])
        self.assertEqual(rc, 1)
        self.assertIn("bad/name", err)
        self.assertEqual(out, "", "a refusal prints no success line")
        with open(before_path, "rb") as f:
            self.assertEqual(f.read(), before,
                             "refusing an invalid name must touch no bytes")
        self.assertEqual(store.entry_scope(self.reread("kept-row"))[1],
                         "statement",
                         "the row is still deriving its scope, so nothing was "
                         "recorded on it")
        self.assertEqual(len(pk.read_events(500)), events_before,
                         "a refusal mints no receipt")
        # THE POSITIVE CONTROL ON EVERY ONE OF THOSE OBSERVABLES, through the
        # SAME door and the SAME row: a valid name writes bytes, mints a
        # receipt and prints a success line. So the refusal above was the name
        # and not a door that cannot write, a row it cannot find, or an events
        # trail nothing ever appends to.
        rc, out, err = self.run_cli(["rescope", "kept-row", "client site"])
        self.assertEqual(rc, 0, err)
        self.assertIn("client site", out)
        with open(before_path, "rb") as f:
            self.assertNotEqual(f.read(), before)
        self.assertEqual(store.entry_project(self.reread("kept-row")),
                         "client site")
        self.assertEqual(len(pk.read_events(500)), events_before + 1)

    def test_a_registry_valid_name_the_frontmatter_cannot_carry_is_refused(self):  # noqa: VACUOUS_ASSERTION — every refused candidate is followed through the SAME door on the SAME row by a representable name that writes bytes, mints a receipt and prints a success line
        """TWO CONTRACTS, NOT ONE. The registry's name contract forbids only an
        empty name, a dot name and a path separator — so it admits a name
        carrying a newline, a carriage return, a vertical tab, a form feed, a
        Unicode line break, or a leading or trailing quotation mark. The store's
        flat frontmatter carries a recorded project as ONE FIELD VALUE ON ONE
        LINE, and the parser strips surrounding quotes, so NONE of those names
        survives a write and a read: the line-breaking ones end the field early
        and hand the remainder to the reader AS A FORGED FIELD, and the quoted
        ones come back short. Reporting success on any of them records a name
        no reader ever compares equal to, so the door refuses it instead.

        So representability is asked SEPARATELY from validity, and the refusal
        names BOTH contracts. The set of refused bytes is DERIVED by rendering
        the field line and reading it back through the parser's own grammar, so
        a name the frontmatter CAN carry — internal spaces, tabs and colons all
        round-trip, measured — is not swept up by it."""
        from helm import registry
        unrepresentable = (
            ("a newline, which ends the field and forges the next one",
             "site\n  status: retired"),
            ("a carriage return", "site\rrow"),
            ("a vertical tab", "site\x0brow"),
            ("a form feed", "site\x0crow"),
            ("a Unicode line separator", "site row"),
            ("a trailing quotation mark the parser strips", 'site"'),
            ("a leading quotation mark the parser strips", '"site'),
        )
        for why, name in unrepresentable:
            with self.subTest(why=why):
                # MUST-HIT THROUGH THE CANONICAL VALIDATOR ITSELF: the registry's
                # own strict population reader ADMITS this name, so the refusal
                # below is the frontmatter's contract and not the registry's.
                registry._checked_value({"version": 1, "projects": {
                    name: {"name": name, "path": "/x"}}})
                self.assertFalse(registry.name_is_malformed(name))
                rid = "unrep-" + str(abs(hash(name)))
                self.seed_prior(rid, "a truth that must not move")
                before_path = self.reread(rid)["path"]
                with open(before_path, "rb") as f:
                    before = f.read()
                events_before = len(pk.read_events(500))
                rc, out, err = self.run_cli(["rescope", rid, name])
                self.assertEqual(rc, 1, "a name the frontmatter cannot carry "
                                        "must be refused, not recorded")
                self.assertEqual(out, "", "a refusal prints no success line")
                low = err.casefold()
                self.assertIn("registry", low,
                              "the refusal names the contract that ADMITS the "
                              "name, so the reader knows which one refused")
                self.assertIn("frontmatter", low,
                              "the refusal names the contract that REFUSES it")
                with open(before_path, "rb") as f:
                    self.assertEqual(f.read(), before,
                                     "the refusal lands before any writer, so "
                                     "no bytes change")
                self.assertEqual(len(pk.read_events(500)), events_before,
                                 "a refusal mints no receipt")
                self.assertEqual(store.entry_scope(self.reread(rid))[1],
                                 "statement",
                                 "nothing was recorded on the row")
                # POSITIVE CONTROL ON EVERY ONE OF THOSE OBSERVABLES, same door
                # and same row: a representable name writes bytes, mints a
                # receipt and prints a success line. So the refusal was the
                # NAME, and not a door that cannot write or a row it cannot find.
                rc, out, err = self.run_cli(["rescope", rid, "client site"])
                self.assertEqual(rc, 0, err)
                self.assertIn("client site", out)
                with open(before_path, "rb") as f:
                    self.assertNotEqual(f.read(), before)
                self.assertEqual(store.entry_project(self.reread(rid)),
                                 "client site")
                self.assertEqual(len(pk.read_events(500)), events_before + 1)

    def test_a_representable_name_round_trips_through_the_parser_exactly(self):
        """The other side of the representability fence, asked at the grammar
        rather than through the CLI: a value the flat frontmatter CAN carry is
        handed back byte for byte. Internal spaces, internal tabs and an
        internal colon all survive — which is why the refusal above is a
        narrow representability test and not a whitelist that would have
        re-broken the registry's own admitted names."""
        # UNCONDITIONAL, AHEAD OF THE LOOP, on the observable the loop reads:
        # this grammar does carry an ordinary value, so a loop that never ran
        # could not read as a pass and the predicate is not answering False to
        # everything it is handed.
        self.assertTrue(pk.field_value_is_representable("plain"))
        self.assertFalse(pk.field_value_is_representable("broken\nrow"))
        for name in ("client site", "a\tb", "foo: bar", "plain"):
            with self.subTest(name=name):
                from helm import registry
                self.assertFalse(registry.name_is_malformed(name))
                self.assertTrue(pk.field_value_is_representable(name))
                self.seed_prior("rt-" + str(abs(hash(name))),
                                "a truth about one project")
                rid = "rt-" + str(abs(hash(name)))
                rc, out, err = self.run_cli(["rescope", rid, name])
                self.assertEqual(rc, 0, err)
                self.assertEqual(self.reread(rid).get("project"), name,
                                 "the recorded name comes back byte for byte "
                                 "through the real writer and the real parser")

    # -- (e) the clear-owner line reports the EFFECTIVE owner ---------------
    def test_clearing_the_record_reports_the_owner_the_root_derives(self):  # noqa: VACUOUS_ASSERTION — the cleared row is asserted to DERIVE a named owner through a named route, which is a positive reading and not an absence
        """Three roots, three different answers, and only one of them is fleet.
        The physical project root is the case the old line got wrong."""
        self.seed_prior("homed-row", "a project-homed truth", project="fleet",
                        root_dir=self.project_dir("p1", "premises"))
        store.write_prior({"id": "adopted-row", "statement": "owner canon",
                           "confidence": 0.9, "project": "helm"},
                          root_dir=self.adopted)
        self.seed_prior("general-row", "derive a check, never transcribe it",
                        project="helm")
        self.seed_prior("artifact-row", "the fence lives in helm/store/load.py",
                        project="fleet")
        # must-hit ahead of the loop: the four rows are all loadable and all
        # carry a RECORDED project, so a reported owner below is the DERIVATION
        # after the clear and not a record that was never there.
        for eid, rec in (("homed-row", "fleet"), ("adopted-row", "helm"),
                         ("general-row", "helm"), ("artifact-row", "fleet")):
            self.assertEqual(self.reread(eid, project="p1").get("project"),
                             rec, eid)
        for eid, owner in (("homed-row", "p1"), ("adopted-row", "fleet"),
                           ("general-row", "fleet"), ("artifact-row", "helm")):
            with self.subTest(eid=eid):
                rc, out, err = self.run_cli(
                    ["rescope", eid, "-", "--project", "p1"])
                self.assertEqual(rc, 0, err)
                e = self.reread(eid, project="p1")
                owner_now, how = store.entry_scope(e)
                self.assertIn(how, ("root", "adopted", "statement"),
                              "the row now DERIVES its scope, which is what a "
                              "cleared record means")
                self.assertEqual(owner_now, owner,
                                 "must-hit: the derivation itself yields this")
                self.assertIn(owner, out,
                              "the success line must report the owner the "
                              "derivation yields, never a promise of fleet")

class CliTest(StoreBase):
    def run_cli(self, args, stdin=None):
        out, err = io.StringIO(), io.StringIO()
        old_stdin = sys.stdin
        if stdin is not None:
            sys.stdin = io.StringIO(stdin)
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = store.cmd_store(args)
        finally:
            sys.stdin = old_stdin
        return rc, out.getvalue()

    def test_add_list_get(self):
        rc, out = self.run_cli(["add", "prior",
                                "cli-x | the statement | 0.7 | clikw | dev",
                                "--source", "human", "--rationale", "seen twice"])
        self.assertEqual(rc, 0)
        self.assertIn("helm store: LIVE 'cli-x' [prior 0.70]", out)
        # default target is helm-global, never adopted
        self.assertEqual(os.listdir(self.adopted), [])
        e = self.one(store.load_all(), "cli-x")
        self.assertEqual(e["root"], "helm-global")
        self.assertEqual(len(e["evidence_log"]), 1)  # rationale seeded the receipt
        rc, out = self.run_cli(["list"])
        self.assertEqual(rc, 0)
        self.assertIn("cli-x", out)
        rc, out = self.run_cli(["get", "cli-x"])
        self.assertEqual(rc, 0)
        self.assertIn("the statement", out)
        rc, _ = self.run_cli(["get", "ghost"])
        self.assertEqual(rc, 1)

    def test_add_prior_clamps_to_belief_rail(self):
        # the prior verb mints beliefs — 0.999 (or an outright 1.0) must never
        # become a certain premise through the agent-facing lane
        for i, raw_conf in enumerate(("0.999", "1.0")):
            rc, out = self.run_cli(["add", "prior",
                                    "clamp-%d | nearly sure | %s | clampkw%d"
                                    % (i, raw_conf, i)])
            self.assertEqual(rc, 0)
            self.assertIn("[prior 0.99]", out)
            e = self.one(store.load_all(), "clamp-%d" % i)
            self.assertEqual(e["class"], "prior")
            self.assertAlmostEqual(e["confidence"], 0.99)

    def test_add_premise_and_typed_adds(self):
        rc, out = self.run_cli(["add", "premise", "cli-truth | always true | truthkw"])
        self.assertEqual(rc, 0)
        self.assertIn("[certain 1.00]", out)
        rc, _ = self.run_cli(["add", "lexicon", "youable | able to be you"])
        self.assertEqual(rc, 0)
        rc, _ = self.run_cli(["add", "heuristic", "swarmify | split it | swarm"])
        self.assertEqual(rc, 0)
        rc, _ = self.run_cli(["add", "reference",
                              "dregg | mandate crown | https://x.test | dregg"])
        self.assertEqual(rc, 0)
        c = store.counts()["helm-global"]
        self.assertEqual(c, {"prior": 1, "lexicon": 1, "heuristic": 1, "reference": 1})

    def test_add_project_targets_project_root(self):
        rc, _ = self.run_cli(["add", "prior", "proj-x | scoped | 0.6 | projkw",
                              "--project", "p1"])
        self.assertEqual(rc, 0)
        e = self.one(store.load_all(project="p1"), "proj-x")
        self.assertEqual((e["root"], e["scope"]), ("project", "project:p1"))

    def test_resolve_pinned_counts(self):
        self.run_cli(["add", "prior", "cli-x | the statement | 0.7 | clikw"])
        rc, out = self.run_cli(["resolve"], stdin="clikw ahoy")
        self.assertEqual(rc, 0)
        self.assertIn("PRIOR cli-x [0.70]: the statement", out)
        rc, out = self.run_cli(["resolve"], stdin="nothing relevant")
        self.assertEqual(out, "")  # salience law: silent on no match
        self.seed_prior("pin-a", "always on", conf=1.0, pin="true")
        rc, out = self.run_cli(["pinned"])
        self.assertIn("PREMISE pin-a", out)
        rc, out = self.run_cli(["counts"])
        self.assertIn("helm-global", out)

    def test_resolve_keeps_query_words_equal_to_project_name(self):
        # audit MEDIUM: resolve --project P stripped EVERY query word equal to
        # P (the flag pair is already deleted from args) — false "no JIT match"
        # exactly when the query names the project
        self.run_cli(["add", "premise", "myproj-rule | follow the myproj law | myproj",
                      "--project", "myproj"])
        rc, out = self.run_cli(["resolve", "--project", "myproj",
                                "deploy", "myproj", "now"])
        self.assertEqual(rc, 0)
        self.assertIn("myproj-rule", out)
        self.assertNotIn("no JIT match", out)

    def test_evidence_supersede_retire(self):
        self.run_cli(["add", "prior", "cli-x | the statement | 0.7 | clikw"])
        rc, out = self.run_cli(["evidence", TS, "cli-x", "0.1", "held up in practice"])
        self.assertEqual(rc, 0)
        self.assertIn("confidence -> 0.80", out)
        rc, _ = self.run_cli(["evidence", TS, "ghost", "0.1", "why"])
        self.assertEqual(rc, 1)
        self.run_cli(["add", "prior", "cli-y | the replacement | 0.8 | clikw"])
        rc, out = self.run_cli(["supersede", TS, "cli-x", "cli-y", "sharper form"])
        self.assertEqual(rc, 0)
        self.assertIn("TOMBSTONED 'cli-x'", out)
        rc, out = self.run_cli(["retire", TS, "cli-y", "done with it"])
        self.assertEqual(rc, 0)
        self.assertIn("RETIRED 'cli-y'", out)
        self.assertEqual(store.load_all(), [])

    def test_usage_paths(self):
        rc, _ = self.run_cli([])
        self.assertEqual(rc, 2)
        rc, _ = self.run_cli(["add", "nonsense", "x | y"])
        self.assertEqual(rc, 2)
        rc, _ = self.run_cli(["frobnicate"])
        self.assertEqual(rc, 2)


class EvidenceResolvesWhatGetResolvesTest(StoreBase):
    """#951 (task/951, measured 2026-08-11T00:46Z): same cwd, same id, two
    doors, two answers — `get` resolved a helm-global heuristic and printed its
    statement; `evidence` answered exactly "not found". The write door admitted
    only priors, so the entries with the widest reach (global canon, mostly
    heuristics) were exactly the ones no seat could attach a correction receipt
    to. The cure: every id-resolving write verb resolves through the SAME door
    `get` reads (_resolve_write), certain-by-construction types take the
    receipt IN the entry file with confidence pinned, and a type outside a
    verb's writable set is refused BY NAME, never called absent."""

    run_cli = CliTest.run_cli

    def _seed_global_heuristic(self, hid):
        return store.write_heuristic(
            {"id": hid, "move": "a passing control proves the probe ran",
             "trigger": "probecontrol", "stated_ts": TS})

    def test_the_measured_flip_global_heuristic_both_doors_agree(self):
        p = self._seed_global_heuristic("probe-control-law")
        # the measured conditions: cwd-scoped to a project, entry in helm-global
        with mock.patch("helm.inject._ledger.project_for_cwd",
                        return_value="helm"):
            rc, out = self.run_cli(["get", "probe-control-law"])
            self.assertEqual(rc, 0)
            self.assertIn("helm-global", out)     # the read door: resolves, global
            rc, out = self.run_cli(["evidence", TS, "probe-control-law",
                                    "+0.02", "held in practice"])
        self.assertEqual(rc, 0)                    # was rc 1, "not found"
        self.assertIn("certain-by-construction", out)
        self.assertIn("confidence -> 1.00", out)   # pinned, never moved
        # the EFFECT, in the store records: re-read from DISK, the receipt landed
        e = self.one(store.load_all(types=("heuristic",)), "probe-control-law")
        self.assertEqual(e["confidence"], 1.0)
        self.assertEqual(len(e["evidence_log"]), 1)
        self.assertEqual(e["evidence_log"][0]["reason"], "held in practice")
        self.assertEqual(e["evidence_log"][0]["by"], "agent")
        self.assertEqual(e["evidence_log"][0]["type"], "support")
        with open(p) as f:
            raw = f.read()
        self.assertIn("evidence_log", raw)         # durable: IN the artifact,
        self.assertIn("held in practice", raw)     # not only the lossy journal

    def test_reference_and_lexicon_receipts_land_and_survive_rewrite(self):
        store.write_reference({"id": "ref-x", "statement": "harvested nugget",
                               "url": "https://x.test", "keywords": "refkw",
                               "stated_ts": TS})
        _, msg = store.apply_evidence("ref-x", TS, "-0.1", "aged badly")
        self.assertIn("certain-by-construction", msg)
        e = self.one(store.load_all(types=("reference",)), "ref-x")
        self.assertEqual(len(e["evidence_log"]), 1)
        self.assertEqual(e["evidence_log"][0]["type"], "contradict")
        store.write_lexicon({"term": "glorpword", "definition": "a word"})
        _, msg = store.apply_evidence("glorpword", TS, "0.1", "used correctly")
        self.assertIn("certain-by-construction", msg)
        e = self.one(store.load_all(types=("lexicon",)), "glorpword")
        self.assertEqual(len(e["evidence_log"]), 1)
        self.assertEqual(e["updated_ts"], TS)  # the freshness field lexicon serializes
        # the receipt SURVIVES an unrelated in-place rewrite — writer emission
        # + parser default + decoder, the three-allowlist law all present
        _, err = store.retag("glorpword", TS, add="glorpish")
        self.assertEqual(err, None)
        e = self.one(store.load_all(types=("lexicon",)), "glorpword")
        self.assertEqual(len(e["evidence_log"]), 1)

    def test_project_prior_control_and_the_cli_write_lens(self):
        # must-hit control: a project-scope PRIOR behaves exactly as before
        self.seed_prior("scoped-belief", "a belief", conf=0.6,
                        root_dir=self.project_dir("cwdproj", "premises"))
        e, msg = store.apply_evidence("scoped-belief", TS, "0.2", "held",
                                      project="cwdproj")
        self.assertEqual(msg, None)
        # unconditional positive control on the SAME observable (msg): the
        # surfaced message DOES fire where the law applies, so the None above
        # is a belief-lane answer, not a dead return value
        self.seed_prior("certain-here", "a truth", conf=1.0)
        _, msg2 = store.apply_evidence("certain-here", TS, "-0.1", "contradicted")
        self.assertIn("certain-prior", msg2)
        self.assertAlmostEqual(e["confidence"], 0.8)
        e = self.one(store.load_all(project="cwdproj"), "scoped-belief")
        self.assertAlmostEqual(e["confidence"], 0.8)
        self.assertEqual(len(e["evidence_log"]), 1)
        # the CLI write door shares get's cwd lens now: the same command shape
        # that answered "not found" about a project entry pre-fix reaches it
        with mock.patch("helm.inject._ledger.project_for_cwd",
                        return_value="cwdproj"):
            rc, out = self.run_cli(["evidence", TS, "scoped-belief",
                                    "0.05", "still holding"])
        self.assertEqual(rc, 0)
        self.assertIn("confidence -> 0.85", out)

    def test_neither_scope_still_not_found_rc_unchanged(self):
        e, err = store.apply_evidence("ghost-nowhere", TS, "0.1", "why")
        self.assertEqual(e, None)
        self.assertEqual(err, "not found")
        rc, out = self.run_cli(["evidence", TS, "ghost-nowhere", "0.1", "why"])
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")   # stdout silent; the refusal rode stderr
        # positive control on the SAME observable (rc + stdout): the same call
        # against a seeded id succeeds, so the empty answer above is earned
        self.seed_prior("real-belief", "a belief", conf=0.6, keywords="realkw")
        rc, out = self.run_cli(["evidence", TS, "real-belief", "0.1", "why"])
        self.assertEqual(rc, 0)
        self.assertIn("confidence -> 0.70", out)

    def test_both_scopes_write_lands_on_the_shadow_winner_only(self):  # noqa: VACUOUS_ASSERTION — the final assertEqual is an intentional absence claim (the shadowed twin's bytes must NOT move); its unconditional positive control is p_after assertNotEqual/assertIn on the SAME instrument (file bytes through the same open/read), so a do-nothing apply_evidence fails loudly before this line
        # the shadowing law IS the write law (canon: ScopeTest retires and
        # supersedes shadow winners) — the write lands EXACTLY where `get`
        # resolves, and the shadowed global twin's bytes never move
        g_path = self._seed_global_heuristic("twin-law")
        p_path = store.write_heuristic(
            {"id": "twin-law", "move": "the project sense",
             "trigger": "twinlaw", "stated_ts": TS},
            root_dir=os.path.join(home.project_dir("p1"), "heuristics"))
        with open(g_path) as f:
            g_before = f.read()
        winner = self.one(store.load_all(project="p1", types=("heuristic",)),
                          "twin-law")
        self.assertEqual(winner["root"], "project")   # where get resolves
        with open(p_path) as f:
            p_before = f.read()
        e, _ = store.apply_evidence("twin-law", TS, "-0.1",
                                    "contradicted in p1", project="p1")
        self.assertEqual(e["path"], p_path)           # landed there, and
        e = self.one(store.load_all(project="p1", types=("heuristic",)),
                     "twin-law")
        self.assertEqual(len(e["evidence_log"]), 1)   # durably
        # unconditional positive control on the SAME observable (file bytes):
        # the winner's file DID move, so the equality below can fail
        with open(p_path) as f:
            p_after = f.read()
        self.assertIn("contradicted in p1", p_after)
        self.assertNotEqual(p_after, p_before)
        with open(g_path) as f:
            self.assertEqual(f.read(), g_before)      # the twin: untouched

    def test_type_refusals_name_the_type_never_absent(self):
        store.write_lexicon({"term": "wordish", "definition": "a word"})
        self._seed_global_heuristic("move-law")
        # retire: lexicon outside its writable trio -> named, not "not found"
        e, err = store.retire("wordish", TS, "why")
        self.assertEqual(e, None)
        self.assertIn("resolves as type lexicon", err)
        self.assertNotIn("not found", err)
        # demote: heuristics can never hold the always lane -> named
        e, err = store.demote("move-law", TS, "tidy the lane")
        self.assertEqual(e, None)
        self.assertIn("resolves as type heuristic", err)
        # supersede: lexicon old refused by name (write_lexicon cannot
        # round-trip replaced_by — admitting it would drop the tombstone)
        e, err = store.mark_superseded("wordish", "move-law", TS)
        self.assertEqual(e, None)
        self.assertIn("resolves as type lexicon", err)
        # evidence: episodic bulk memory is resolvable by get, not writable
        pk.atomic_write(os.path.join(self.adopted, "war-story.md"),
                        '---\nname: war-story\ndescription: "the incident"\n'
                        'metadata:\n  node_type: memory\n  type: project\n---\nbody\n')
        e, err = store.apply_evidence("war-story", TS, "0.1", "why")
        self.assertEqual(e, None)
        self.assertIn("resolves as type episodic", err)
        # the genuinely-absent control keeps its exact historical answer
        e, err = store.retire("ghost-nowhere", TS, "why")
        self.assertEqual(err, "'ghost-nowhere' not found")

    def test_shared_slug_write_follows_gets_winner_never_a_lower_type(self):
        # (dispatch a75aecaea36a, verdict at tip 9647cd88): heuristic
        # + always-reference share one slug; get's untyped winner is the
        # heuristic (_TYPE_ORDER), but the first cut filtered to demote's
        # (prior, reference) BEFORE the winner choice and silently demoted the
        # REFERENCE — a write on a record get does not show, err=None. The
        # door now resolves untyped ONCE and admits/refuses THE WINNER.
        self._seed_global_heuristic("shared-face")
        store.write_reference({"id": "shared-face", "statement": "the ref sense",
                               "url": "https://x.test", "keywords": "sharedface",
                               "stated_ts": TS, "load_class": "always"})
        winner = store._find("shared-face")
        self.assertEqual(winner["type"], "heuristic")   # get's answer
        e, err = store.demote("shared-face", TS, "tidy the lane")
        self.assertEqual(e, None)                       # REFUSED, not diverted
        self.assertIn("resolves as type heuristic", err)
        # the reference was NOT silently demoted
        ref = self.one(store.load_all(types=("reference",)), "shared-face")
        self.assertEqual(ref["load_class"], "always")
        # positive control on the same observable (demote's return + the
        # persisted tier): a reference-only slug still demotes
        store.write_reference({"id": "lone-ref", "statement": "ref sense",
                               "url": "https://x.test", "keywords": "loneref",
                               "stated_ts": TS, "load_class": "always"})
        e, err = store.demote("lone-ref", TS, "tidy the lane")
        self.assertEqual(err, None)
        self.assertEqual(e["load_class"], "jit")
        self.assertEqual(
            self.one(store.load_all(types=("reference",)), "lone-ref")["load_class"],
            "jit")
        # a bare-id retag follows the same winner as get...
        e, err = store.retag("shared-face", TS, add="heurish")
        self.assertEqual(err, None)
        self.assertEqual(e["type"], "heuristic")
        # ...while an EXPLICIT --type stays the house disambiguator (the
        # _pick_candidate law) and SELECTS the named record under the winner
        e, err = store.retag("shared-face", TS, add="refonly", ctype="reference")
        self.assertEqual(err, None)
        self.assertEqual(e["type"], "reference")
        # and a --type nobody carries names the winner, never "not found"
        e, err = store.retag("lone-ref", TS, add="x", ctype="prior")
        self.assertEqual(e, None)
        self.assertIn("resolves as type reference", err)
        self.assertIn("no prior entry", err)


class AddGuardTest(StoreBase):
    """supersede-not-duplicate at add time: a live same-id is a hard refuse
    with the exact follow-up commands (never a silent overwrite — the drain
    conflict law); a near-identical statement under another id warns loudly
    and proceeds (never blocked on similarity alone)."""

    def add(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = store.cmd_store(["add", *args])
        return rc, out.getvalue(), err.getvalue()

    def test_same_id_live_refuses_with_commands(self):
        rc, _, _ = self.add("prior", "x-law | the original | 0.7 | xkw")
        self.assertEqual(rc, 0)
        rc, out, err = self.add("prior", "x-law | a different statement")
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")
        self.assertIn("'x-law' is already LIVE [prior helm-global]", err)
        self.assertIn("helm store evidence", err)
        self.assertIn("helm store supersede", err)
        # never a silent overwrite: the original statement survives
        self.assertEqual(self.one(store.load_all(), "x-law")["statement"],
                         "the original")
        # cross-type same slug stays legal (no cross-type shadowing)
        rc, _, _ = self.add("heuristic", "x-law | do the move | movekw")
        self.assertEqual(rc, 0)
        # premise re-add of a live prior hits the same rail (premise IS prior)
        rc, _, err = self.add("premise", "x-law | now certain")
        self.assertEqual(rc, 1)
        self.assertIn("helm store evidence", err)

    def test_retired_or_superseded_id_may_be_re_minted(self):
        self.add("prior", "gone | old sense | 0.7 | gonekw")
        store.retire("gone", TS, "over")
        # the keyword-less re-mint passes the lint through the re-mint
        # fallback: the retired file's own keywords are the effective field
        rc, out, _ = self.add("prior", "gone | new sense | 0.7")
        self.assertEqual(rc, 0)
        self.assertEqual(self.one(store.load_all(), "gone")["statement"], "new sense")

    def test_near_duplicate_warns_and_proceeds(self):
        self.add("premise", "small-prs | small reviewable prs land faster and cleaner | prkw")
        rc, out, err = self.add(
            "prior", "tiny-prs | small reviewable prs land faster and cleaner today "
            "| 0.7 | tiny reviews")
        self.assertEqual(rc, 0)
        self.assertEqual(err, "")
        self.assertIn("possible duplicate of 'small-prs'", out)
        self.assertIn("88% statement overlap", out)  # 7/8 tokens shared
        self.assertIn("helm store supersede", out)
        self.assertIn("LIVE 'tiny-prs'", out)  # the add went through
        self.assertEqual(len(store.load_all(types=("prior",))), 2)

    def test_distinct_adds_stay_silent(self):
        self.add("prior", "one-law | ship small slices deliberately | 0.7 | small slices")
        rc, out, _ = self.add(
            "prior", "other-law | measure quota before dispatching agents "
            "| 0.7 | quota headroom")
        self.assertEqual(rc, 0)
        self.assertNotIn("WARNING", out)

    def test_refuse_sees_live_entry_across_roots(self):
        # live in ADOPTED; the add targets helm-global — the lens still
        # refuses (a global add would silently shadow the adopted truth)
        store.write_prior({"id": "adopted-law", "statement": "adopted sense",
                           "confidence": 0.9}, root_dir=self.adopted)
        rc, _, err = self.add("prior", "adopted-law | shadowing attempt")
        self.assertEqual(rc, 1)
        self.assertIn("[prior adopted]", err)
        # a PROJECT-scoped entry is outside the global lens: global add proceeds
        self.add("prior", "proj-law | project sense | 0.6 | projlawkw",
                 "--project", "p1")
        rc, _, err = self.add("prior", "proj-law | global sense | 0.6 | globlawkw")
        self.assertEqual(rc, 0)
        # but through the project lens the (narrower) live entry refuses it
        rc, _, err = self.add("prior", "proj-law | another try | 0.6",
                              "--project", "p1")
        self.assertEqual(rc, 1)
        self.assertIn("[prior project]", err)

    def test_lexicon_redefine_stays_legal(self):
        self.add("lexicon", "youable | able to be you")
        rc, out, err = self.add("lexicon", "youable | able to be you, sharpened")
        self.assertEqual(rc, 0)
        self.assertEqual(err, "")
        self.assertEqual(self.one(store.load_all(types=("lexicon",)), "youable")
                         ["definition"], "able to be you, sharpened")


class LexiconPipeContractTest(StoreBase):
    """The lexicon pipe contract is CLOSED: field 3 is kind (ONE taxonomy
    slug), field 4 the keywords CSV, field 5 domain. A CSV in kind or a sixth
    field is refused loudly, never silently filed under the wrong key (the
    live lex-fleet-truth incident: keywords died as kind:, domain as
    examples:)."""

    def add(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = store.cmd_store(["add", *args])
        return rc, out.getvalue(), err.getvalue()

    def test_keywords_and_domain_store_and_resolve(self):
        rc, _, err = self.add(
            "lexicon", "fleet-truth | census ground truth | coinage | "
            "fleet state, stale, still up | helm-ops")
        self.assertEqual(rc, 0)
        self.assertEqual(err, "")
        e = self.one(store.load_all(types=("lexicon",)), "fleet-truth")
        self.assertEqual((e["kind"], e["keywords"], e["domain"]),
                         ("coinage", "fleet state, stale, still up", "helm-ops"))
        with open(e["path"]) as f:
            raw = f.read()
        self.assertIn("  keywords: fleet state, stale, still up", raw)
        self.assertIn("  domain: helm-ops", raw)
        for text in ("fleet state seems stale", "what does fleet-truth mean"):
            self.assertEqual([x["id"] for x in store.resolve_prompt(text)],
                             ["fleet-truth"], text)

    def test_csv_in_kind_is_refused_not_swallowed(self):
        rc, out, err = self.add(
            "lexicon", "fleet-truth | census ground truth | fleet state, stale")
        self.assertEqual(rc, 2)
        self.assertEqual(out, "")
        self.assertIn("keywords,csv", err)
        self.assertEqual(store.load_all(types=("lexicon",)), [])

    def test_sixth_field_is_refused(self):
        """Still refused, still nothing stored — but by the SHARED arity guard
        (helm/delim.py) rather than this type's own check. The lexicon
        incident that motivated this class was fixed for lexicon only; the
        same cascade was live in prior/premise/heuristic/reference and in the
        premise, reflex and mentor verbs until 2026-07-25. The message now
        shows the parse, which is what the operator actually needs."""
        rc, _, err = self.add("lexicon", "t | d | phrase | kw | dom | extra")
        self.assertEqual(rc, 2)
        self.assertIn("6 fields", err)
        self.assertIn("at most 5", err)
        self.assertIn("[5] extra", err)          # names the field that did not fit
        self.assertEqual(store.load_all(types=("lexicon",)), [])

    def test_redefine_preserves_keywords_domain_kind(self):
        # a bare definition sharpen (the coach landing verb's 2-field form)
        # must MERGE, not rebuild — the redefine regression of the very
        # incident the closed contract fixed (review: symptom vocabulary died)
        self.add("lexicon", "fleet-truth | census ground truth | coinage | "
                 "fleet state, stale, still up | helm-ops",
                 "--source", "coinage-capture")
        rc, _, err = self.add("lexicon",
                              "fleet-truth | census ground truth, sharpened")
        self.assertEqual((rc, err), (0, ""))
        e = self.one(store.load_all(types=("lexicon",)), "fleet-truth")
        self.assertEqual(e["definition"], "census ground truth, sharpened")
        self.assertEqual((e["kind"], e["keywords"], e["domain"]),
                         ("coinage", "fleet state, stale, still up", "helm-ops"))
        with open(e["path"]) as f:
            self.assertIn("  source: coinage-capture", f.read())  # source survives
        self.assertEqual([x["id"] for x in
                          store.resolve_prompt("fleet state seems stale")],
                         ["fleet-truth"])

    def test_redefine_empty_field_keeps_prior_value(self):
        # an EMPTY-but-PRESENT optional field (e.g. `... | kind |  | domain`)
        # must MERGE-KEEP the prior value, never STRIP it — the pipe position
        # exists only to reach a LATER field, not to null an earlier one. The
        # bug: `parts[N] if len(parts) > N` used the empty string and wiped the
        # symptom vocabulary; the fix guards `and parts[N]`.
        self.add("lexicon", "fleet-truth | census ground truth | coinage | "
                 "fleet state, stale, still up | helm-ops")
        # redefine reaching domain past an EMPTY keywords field
        rc, _, err = self.add(
            "lexicon", "fleet-truth | sharpened def | coinage |  | ops-2")
        self.assertEqual((rc, err), (0, ""))
        e = self.one(store.load_all(types=("lexicon",)), "fleet-truth")
        self.assertEqual(e["definition"], "sharpened def")
        # empty keywords field kept the prior; explicit domain overrode
        self.assertEqual((e["keywords"], e["domain"]),
                         ("fleet state, stale, still up", "ops-2"))
        self.assertEqual([x["id"] for x in
                          store.resolve_prompt("fleet state seems stale")],
                         ["fleet-truth"])

    def test_redefine_explicit_fields_still_override(self):
        self.add("lexicon", "fleet-truth | truth | coinage | oldword | ops")
        self.add("lexicon", "fleet-truth | truth | bug-class | newword | dev")
        e = self.one(store.load_all(types=("lexicon",)), "fleet-truth")
        self.assertEqual((e["kind"], e["keywords"], e["domain"]),
                         ("bug-class", "newword", "dev"))
        self.assertEqual([x["id"] for x in store.resolve_prompt("newword here")],
                         ["fleet-truth"])
        self.assertEqual(store.resolve_prompt("oldword here"), [])

    def test_project_redefine_preserves_keywords(self):
        # the merge follows the scoped-filename law: a project redefine reads
        # the PROJECT file, never global's
        self.add("lexicon", "pterm | pdef | phrase | projword", "--project", "p1")
        self.add("lexicon", "pterm | pdef sharpened", "--project", "p1")
        e = self.one(store.load_all(project="p1", types=("lexicon",)), "pterm")
        self.assertEqual((e["definition"], e["keywords"]),
                         ("pdef sharpened", "projword"))

    def test_legacy_csv_kind_migrates_on_redefine(self):
        # a legacy mis-file (keywords CSV under kind:) redefined via the verb:
        # the rescued vocabulary lands in the real keywords field and kind
        # normalizes to phrase — the rewrite is the migration moment
        d = self.global_dir("lexicon")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "lex-fleet-truth.md"), "w") as f:
            f.write("---\nname: lex-fleet-truth\n"
                    'description: "lexicon: fleet-truth = census ground truth"\n'
                    "metadata:\n  node_type: memory\n  type: lexicon\n"
                    "  term: fleet-truth\n  scope: global\n"
                    "  kind: fleet state, stale, still up\n"
                    "  definition: census ground truth\n---\n")
        rc, _, err = self.add("lexicon",
                              "fleet-truth | census ground truth, sharpened")
        self.assertEqual((rc, err), (0, ""))
        e = self.one(store.load_all(types=("lexicon",)), "fleet-truth")
        self.assertEqual((e["kind"], e["keywords"]),
                         ("phrase", "fleet state, stale, still up"))
        with open(e["path"]) as f:
            raw = f.read()
        self.assertIn("  keywords: fleet state, stale, still up", raw)
        self.assertIn("  kind: phrase", raw)
        self.assertEqual([x["id"] for x in
                          store.resolve_prompt("fleet state seems stale")],
                         ["fleet-truth"])


class AtomicWriteRaceTest(unittest.TestCase):
    """pk.atomic_write must not lose a concurrent same-path writer: with a
    SHARED tmp name (path + '.tmp'), the first os.replace steals the second
    writer's tmp and the second's replace dies FileNotFoundError — a silent
    lost write (adversarial review, pre-existing pk seam)."""

    def test_interleaved_same_path_writers_both_land(self):
        tmp = tempfile.mkdtemp(prefix="helm-test-aw-")
        self.addCleanup(shutil.rmtree, tmp, True)
        target = os.path.join(tmp, "entry.md")
        real, fired = os.replace, []

        def interleave(src, dst):
            if not fired:  # a second full writer (its own thread, as live)
                fired.append(1)  # runs between the first's write and replace
                t = threading.Thread(target=pk.atomic_write,
                                     args=(target, "second\n"))
                t.start()
                t.join()
            return real(src, dst)

        with mock.patch.object(pk.os, "replace", side_effect=interleave):
            pk.atomic_write(target, "first\n")  # old code: FileNotFoundError
        with open(target) as f:
            self.assertEqual(f.read(), "first\n")  # last replace wins, no loss
        self.assertEqual(os.listdir(tmp), ["entry.md"])  # no orphaned tmp


class AdoptProjectMemdirsTest(StoreBase):
    """Per-project claude memory dirs adopted as project-scoped store roots:
    roots(project) gains ('adopted-project', ...) triples, shadow order is
    project > adopted-project > helm-global > adopted. Hermetic: the claude
    memdir resolver is patched to a tmp dir (never the real ~/.claude)."""

    def setUp(self):
        super().setUp()
        store._ADOPTED_PROJECT_CACHE.clear()
        self.projmem = os.path.join(self.tmp, "projmem")
        os.makedirs(self.projmem)
        pk.write_json(home.registry_path(), {"version": 1, "projects": {
            "demo-project": {"name": "demo-project", "path": "/dev/demo-project", "kind": "git",
                        "sessions": {}, "cwds": []}}})

    def tearDown(self):
        store._ADOPTED_PROJECT_CACHE.clear()
        super().tearDown()

    def _patch(self):
        return mock.patch.object(
            home, "claude_memory_dir_for",
            side_effect=lambda p: self.projmem if p == "/dev/demo-project" else "/nonexistent-xyz")

    def test_project_adopted_root_fires_its_own_priors(self):
        store.write_prior({"id": "demo-law", "statement": "demo-project's own prior",
                           "confidence": 0.9, "keywords": "demoword"},
                          root_dir=self.projmem)
        with self._patch():
            store._ADOPTED_PROJECT_CACHE.clear()
            self.assertIn("adopted-project", [t[0] for t in store.roots(project="demo-project")])
            e = self.one(store.load_all(project="demo-project"), "demo-law")
            self.assertEqual((e["root"], e["scope"]), ("adopted-project", "project:demo-project"))
            self.assertEqual([x["id"] for x in
                              store.resolve_prompt("demoword now", project="demo-project")],
                             ["demo-law"])
        # without the project lens the adopted-project root is NOT in play
        self.assertEqual(store.load_all(), [])

    def test_shadow_order_project_over_adopted_project_over_global(self):
        self.seed_prior("foo", "global sense", conf=0.9)
        store.write_prior({"id": "foo", "statement": "adopted-project sense",
                           "confidence": 0.9}, root_dir=self.projmem)
        with self._patch():
            store._ADOPTED_PROJECT_CACHE.clear()
            e = self.one(store.load_all(project="demo-project"), "foo")
            self.assertEqual((e["statement"], e["root"]),
                             ("adopted-project sense", "adopted-project"))
        self.seed_prior("foo", "authored project sense", conf=0.9,
                        root_dir=self.project_dir("demo-project", "premises"))
        with self._patch():
            store._ADOPTED_PROJECT_CACHE.clear()
            e = self.one(store.load_all(project="demo-project"), "foo")
            self.assertEqual((e["statement"], e["root"]),
                             ("authored project sense", "project"))

    def test_no_registry_falls_back_to_single_store(self):
        # a project lens with no registry file adds no adopted-project root
        os.remove(home.registry_path())
        store._ADOPTED_PROJECT_CACHE.clear()
        self.assertEqual([t[0] for t in store.roots(project="demo-project")],
                         ["adopted", "helm-global", "project"])


class IndexCapTest(StoreBase):
    """helm index cap: demote MEMORY.md link lines whose backing entry stays
    jit-resolvable (lossless), oldest first, until under budget; never
    always/untyped; archive-first net + receipt; re-read + atomic-write."""

    def run_cli(self, args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = store.cmd_index(list(args))
        return rc, out.getvalue(), err.getvalue()

    def _index(self, lines):
        pk.atomic_write(os.path.join(self.adopted, "MEMORY.md"), "\n".join(lines) + "\n")

    def test_demotes_only_lossless_typed_lines_oldest_first(self):
        # three typed jit priors backing three index links; one always-pin; one
        # untyped link with no backing entry
        self.seed_prior("old-one", "old belief", conf=0.8, keywords="oldkw",
                        root_dir=self.adopted, last_updated="2026-01-01T00:00:00Z")
        self.seed_prior("mid-one", "mid belief", conf=0.8, keywords="midkw",
                        root_dir=self.adopted, last_updated="2026-03-01T00:00:00Z")
        self.seed_prior("new-one", "new belief", conf=0.8, keywords="newkw",
                        root_dir=self.adopted, last_updated="2026-06-01T00:00:00Z")
        self.seed_prior("pinned-one", "pinned", conf=1.0, keywords="pinkw",
                        pin="true", root_dir=self.adopted)
        self._index([
            "# MEMORY index",
            "- [old](prior-old-one.md) old",
            "- [mid](prior-mid-one.md) mid",
            "- [new](prior-new-one.md) new",
            "- [pinned](prior-pinned-one.md) pinned — always, never demote",
            "- [untyped](random-note.md) no typed backing — never demote",
            "- freeform line, not a link",
        ])
        # budget 4: 7 lines -> 3 over; only 3 are lossless-demotable (old/mid/new);
        # oldest first
        r = store.index_cap(budget_lines=4, apply=False)
        self.assertEqual((r["lines"], r["over"], r["demotable"]), (7, 3, 3))
        self.assertEqual([b for _l, b in r["plan"]],
                         ["prior-old-one.md", "prior-mid-one.md", "prior-new-one.md"])
        # apply demotes them, keeps the pin + untyped + freeform
        r = store.index_cap(budget_lines=4, apply=True)
        self.assertEqual(r["demoted"], 3)
        with open(os.path.join(self.adopted, "MEMORY.md")) as f:
            kept = f.read()
        self.assertNotIn("prior-old-one.md", kept)
        self.assertIn("prior-pinned-one.md", kept)   # always: never demoted
        self.assertIn("random-note.md", kept)        # untyped: never demoted
        self.assertIn("freeform line", kept)
        # the demoted content stays reachable via inject (lossless)
        self.assertTrue(store.resolve_prompt("oldkw here"))
        # net + receipt exist
        self.assertTrue(os.path.isfile(os.path.join(r["net"], "MEMORY-demoted.md")))
        self.assertTrue(os.path.isfile(os.path.join(r["net"], "RECEIPT.json")))

    def test_store_directory_size_never_returns_a_partial_load(self):
        self.seed_prior("a", "a", conf=0.8, root_dir=self.adopted)
        self.seed_prior("b", "b", conf=0.8, root_dir=self.adopted)
        self.assertEqual({row["id"] for row in store.load_all()}, {"a", "b"})

    def test_link_gates_expiry_precedes_later_projection(self):  # noqa: VACUOUS_ASSERTION — both input rows are present; Expired plus absent gate_probes proves no later row was projected
        rows = [
            {"id": "rule", "type": "prior", "status": "live",
             "keywords": "rule", "gates": "gate"},
            {"id": "gate", "type": "prior", "status": "live",
             "keywords": "gate", "gates": ""},
        ]

        def spend(what):
            if what == "resolving memory store gate":
                raise projscope.Expired("test expiry")

        with mock.patch.object(projscope, "spend_or_raise", side_effect=spend):
            with self.assertRaises(projscope.Expired):
                store.link_gates(rows)
        self.assertTrue(all("gate_probes" not in e for e in rows))

    def test_under_budget_noop(self):  # noqa: VACUOUS_ASSERTION — over=0 and demoted=0 are positive results; zero load calls pins the cheap no-op path
        self.seed_prior("a", "s", conf=0.8, keywords="akw", root_dir=self.adopted)
        self._index(["# idx", "- [a](prior-a.md) a"])
        with mock.patch("helm.store.index.load_all") as load:
            r = store.index_cap(budget_lines=60, apply=True)
        self.assertEqual((r["over"], r["demoted"]), (0, 0))
        self.assertFalse(load.called)

    def test_expiry_before_archive_starts_no_write(self):  # noqa: VACUOUS_ASSERTION — the over-budget index is the positive fixture; zero writes and absent archive pin pre-mutation refusal
        self.seed_prior("old-one", "old", conf=0.8, keywords="oldkw",
                        root_dir=self.adopted,
                        last_updated="2026-01-01T00:00:00Z")
        self._index(["# idx", "- [old](prior-old-one.md) old", "- tail"])

        def spend(what):
            if what == "applying memory index cap":
                raise projscope.Expired("test expiry")

        with mock.patch.object(projscope, "spend_or_raise", side_effect=spend), \
             mock.patch.object(pk, "atomic_write") as write, \
             mock.patch.object(pk, "write_json") as write_json:
            with self.assertRaises(projscope.Expired):
                store.index_cap(budget_lines=2, apply=True)
        self.assertFalse(write.called)
        self.assertFalse(write_json.called)
        self.assertFalse(os.path.exists(os.path.join(self.adopted, "archive")))

    def test_expiry_during_index_sort_never_returns_a_plan(self):  # noqa: VACUOUS_ASSERTION — two observed sort checks prove work began; Expired prevents a partial plan from returning
        self.seed_prior("old-one", "old", conf=0.8, keywords="oldkw",
                        root_dir=self.adopted,
                        last_updated="2026-01-01T00:00:00Z")
        self._index(["# idx", "- [old](prior-old-one.md) old", "- tail"])
        sort_checks = 0

        def spend(what):
            nonlocal sort_checks
            if what == "sorting memory index demotions":
                sort_checks += 1
                if sort_checks == 2:
                    raise projscope.Expired("test expiry")

        with mock.patch.object(projscope, "spend_or_raise", side_effect=spend):
            with self.assertRaises(projscope.Expired):
                store.index_cap(budget_lines=2, apply=False)
        self.assertEqual(sort_checks, 2)

    def test_admitted_index_transaction_completes_after_budget_spends(self):
        self.seed_prior("old-one", "old", conf=0.8, keywords="oldkw",
                        root_dir=self.adopted,
                        last_updated="2026-01-01T00:00:00Z")
        self._index(["# idx", "- [old](prior-old-one.md) old", "- tail"])
        mutated = False
        atomic_write = pk.atomic_write

        def write(path, content):
            nonlocal mutated
            atomic_write(path, content)
            mutated = True

        def spend(_what):
            if mutated:
                raise projscope.Expired("test expiry")

        with mock.patch.object(projscope, "spend_or_raise", side_effect=spend), \
             mock.patch.object(pk, "atomic_write", side_effect=write), \
             mock.patch.object(pk, "event") as event:
            r = store.index_cap(budget_lines=2, apply=True)
        self.assertEqual(r["demoted"], 1)
        self.assertTrue(os.path.isfile(os.path.join(r["net"],
                                                    "MEMORY-demoted.md")))
        self.assertTrue(os.path.isfile(os.path.join(r["net"], "RECEIPT.json")))
        with open(os.path.join(self.adopted, "MEMORY.md")) as f:
            self.assertNotIn("prior-old-one.md", f.read())
        self.assertEqual(event.call_count, 1)

    def test_over_budget_but_nothing_safe(self):
        # over budget, but every line is untyped -> nothing lossless-demotable
        self._index(["# idx", "- [x](random-x.md) x", "- [y](random-y.md) y", "- z"])
        r = store.index_cap(budget_lines=1, apply=True)
        self.assertEqual((r["over"], r["demotable"], r["demoted"]), (3, 0, 0))
        rc, out, _ = self.run_cli(["cap", "--budget-lines", "1", "--apply"])
        self.assertIn("NOTHING safely demotable", out)

    def test_concurrent_append_preserved_on_apply(self):
        from helm import eventledger
        self.seed_prior("old-one", "old", conf=0.8, keywords="oldkw",
                        root_dir=self.adopted, last_updated="2026-01-01T00:00:00Z")
        self._index(["# idx", "- [old](prior-old-one.md) old", "- tail1"])
        real_locked = eventledger.locked

        @contextlib.contextmanager
        def append_before_refresh(path, **kw):
            with real_locked(path, **kw) as held:
                with open(path, "a", encoding="utf-8") as f:
                    f.write("- tail2\n")
                yield held

        with mock.patch("helm.store.index.eventledger.locked",
                        side_effect=append_before_refresh):
            r = store.index_cap(budget_lines=2, apply=True)
        self.assertEqual(r["demoted"], 1)
        with open(os.path.join(self.adopted, "MEMORY.md")) as f:
            kept = f.read()
        self.assertIn("tail1", kept)   # non-demoted lines survive
        self.assertIn("tail2", kept)
        self.assertNotIn("prior-old-one.md", kept)

    def test_failed_memory_commit_leaves_only_a_prepared_receipt(self):  # noqa: VACUOUS_ASSERTION — prepared receipt and unchanged MEMORY are positive controls for the planted final-write failure
        self.seed_prior("old-one", "old", conf=0.8, keywords="oldkw",
                        root_dir=self.adopted,
                        last_updated="2026-01-01T00:00:00Z")
        self._index(["# idx", "- [old](prior-old-one.md) old", "- tail"])
        path = os.path.join(self.adopted, "MEMORY.md")
        real_write = pk.atomic_write

        def write(target, content):
            if target == path:
                raise OSError("planted final write failure")
            return real_write(target, content)

        with mock.patch.object(pk, "atomic_write", side_effect=write):
            with self.assertRaises(OSError):
                store.index_cap(budget_lines=2, apply=True)
        archive = os.path.join(self.adopted, "archive")
        nets = [os.path.join(archive, name) for name in os.listdir(archive)]
        self.assertEqual(len(nets), 1)
        with open(os.path.join(nets[0], "RECEIPT.json")) as f:
            self.assertEqual(json.load(f)["status"], "prepared")
        with open(path) as f:
            self.assertIn("prior-old-one.md", f.read())

    def test_same_instant_transactions_get_distinct_archive_paths(self):
        self.seed_prior("old-one", "old", conf=0.8, keywords="oldkw",
                        root_dir=self.adopted,
                        last_updated="2026-01-01T00:00:00Z")
        paths = []
        with mock.patch("helm.store.index.time.time_ns",
                        side_effect=(111, 222)):
            for _ in range(2):
                self._index(["# idx", "- [old](prior-old-one.md) old", "- tail"])
                paths.append(store.index_cap(budget_lines=2, apply=True)["net"])
        self.assertNotEqual(paths[0], paths[1])
        self.assertTrue(all(os.path.isfile(os.path.join(path, "RECEIPT.json"))
                            for path in paths))

    def test_no_memory_file(self):
        r = store.index_cap(apply=True)
        self.assertFalse(r["exists"])
        rc, out, _ = self.run_cli(["cap"])
        self.assertIn("no MEMORY.md", out)

    def test_cli_usage(self):
        rc, _, err = self.run_cli(["frob"])
        self.assertEqual(rc, 2)


class CandidateTierTest(StoreBase):
    """Candidate tier: safe inferred capture — a candidate is a non-live
    status EXCLUDED from every injecting lane (the hard law), surfaced only
    in list --candidates, promoted by confirm, rejected in place by reject.
    v2 (autolearn): every capturable type may be born a candidate — capture
    everything, canonize nothing automatically; premise stays human-only."""

    def add(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = store.cmd_store(["add", *args])
        return rc, out.getvalue(), err.getvalue()

    def run_cli(self, args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = store.cmd_store(list(args))
        return rc, out.getvalue(), err.getvalue()

    def test_candidate_write_shape_and_excluded_from_inject(self):
        rc, out, _ = self.add("lexicon", "glorpterm | a coined word", "--candidate")
        self.assertEqual(rc, 0)
        self.assertIn("CANDIDATE 'glorpterm'", out)
        self.assertIn("src=inferred", out)
        # the file carries the candidate status line + inferred source
        e = self.one(store.candidates(), "glorpterm")
        with open(e["path"]) as f:
            raw = f.read()
        self.assertIn("  status: candidate", raw)
        self.assertIn("  source: inferred", raw)
        # the hard law: excluded from load_all default, resolve, and pinned
        self.assertEqual(store.load_all(), [])
        self.assertEqual(store.resolve_prompt("is this glorpterm at all"), [])
        self.assertEqual(store.resolve_prompt("glorpterm here"), [])
        # but visible in the include_retired view and candidates()
        self.assertEqual([e["id"] for e in store.candidates()], ["glorpterm"])
        self.assertEqual(self.one(store.load_all(include_retired=True),
                                  "glorpterm")["status"], "candidate")

    def test_candidate_all_capturable_types(self):
        # capture-everything: prior/heuristic/reference candidates land as
        # non-live files with inferred source, invisible to every inject lane
        for args in (("prior", "x-law | inferred belief | 0.7 | glorpwork"),
                     ("heuristic", "x-move | try the glorp first | glorpwork"),
                     ("reference", "x-ref | the glorp paper | https://x.example "
                                   "| glorppaper")):
            rc, out, _ = self.add(*args, "--candidate")
            self.assertEqual(rc, 0, args[0])
            self.assertIn("CANDIDATE", out)
            self.assertIn("src=inferred", out)
        self.assertEqual(sorted(e["id"] for e in store.candidates()),
                         ["x-law", "x-move", "x-ref"])
        for e in store.candidates():
            with open(e["path"]) as f:
                raw = f.read()
            self.assertIn("  status: candidate", raw)
            self.assertIn("  source: inferred", raw)
        # the hard law holds across types: nothing loads, resolves, or pins
        self.assertEqual(store.load_all(), [])
        self.assertEqual(store.resolve_prompt("glorpwork x-law x-move x-ref"), [])
        self.assertEqual(store.pinned(), [])

    def test_premise_candidate_refused(self):
        # the certainty rail is human-only — an inference cannot claim 1.0
        # even in escrow; the refusal routes to the prior-candidate lane
        rc, _, err = self.add("premise", "x-truth | inferred certainty", "--candidate")
        self.assertEqual(rc, 2)
        self.assertIn("human-only", err)
        self.assertIn("add prior", err)
        self.assertEqual(store.candidates(), [])

    def test_confirm_prior_receipt_and_confidence_untouched(self):  # noqa: VACUOUS_ASSERTION — the confidence and the resolve-prompt hit are unconditional positives on the same confirmed entry, and the sibling arm asserts a SUPPLIED actor is recorded verbatim through the same call
        self.add("prior", "x-law | glorp before zork | 0.7 | glorpwork", "--candidate")
        e, err = store.confirm("x-law", TS)
        self.assertIsNone(err)
        self.assertEqual((e["status"], e["source"]), ("live", "explicit"))
        # confirm ratifies the capture, never inflates the belief
        e = self.one(store.load_all(), "x-law")
        self.assertAlmostEqual(e["confidence"], 0.7)
        # who/when receipt lives in the prior's own evidence_log. The `by`
        # value was the hardcoded literal "human" — asserted here as a
        # transcribed constant rather than as a requirement — while confirm()
        # has no actor check at all. A direct API call supplies no actor, so
        # the honest record is UNIDENTIFIED; the CLI resolves a real seat
        # through the identity law and passes it.
        r = e["evidence_log"][-1]
        self.assertEqual((r["type"], r["by"]), ("confirmed", "UNIDENTIFIED"))
        self.assertNotEqual(r["by"], "human")
        self.assertEqual([x["id"] for x in store.resolve_prompt("glorpwork now")],
                         ["x-law"])

    def test_confirm_records_a_SUPPLIED_actor_verbatim(self):
        # The other half: UNIDENTIFIED is what an API call with no actor
        # earns, and a caller that CAN name one has it recorded. Its own
        # keywords, so it cannot disturb a sibling's resolve arm.
        self.add("prior", "x-actor | zork beside glorp | 0.7 | zorkbeside",
                 "--candidate")
        e, err = store.confirm("x-actor", TS, by="seat-under-test")
        self.assertIsNone(err)
        self.assertEqual(e["evidence_log"][-1]["by"], "seat-under-test")

    def test_confirm_edit_swaps_move(self):
        self.add("heuristic", "x-move | first guess | glorpwork", "--candidate")
        e, err = store.confirm("x-move", TS, new_statement="the sharpened move")
        self.assertIsNone(err)
        e = self.one(store.load_all(), "x-move")
        self.assertEqual((e["move"], e["statement"]),
                         ("the sharpened move", "the sharpened move"))

    def test_reject_retires_in_place(self):
        self.add("lexicon", "glorpterm | a wrong guess", "--candidate")
        path = self.one(store.candidates(), "glorpterm")["path"]
        e, err = store.reject("glorpterm", TS, why="not a real coinage")
        self.assertIsNone(err)
        self.assertEqual(e["status"], "retired")
        # the record law: the file STAYS, carrying the receipt
        self.assertTrue(os.path.isfile(path))
        with open(path) as f:
            raw = f.read()
        self.assertIn("  status: retired", raw)
        self.assertIn("  retired_why: not a real coinage", raw)
        # gone from every surface: candidates, live load, resolve
        self.assertEqual(store.candidates(), [])
        self.assertEqual(store.load_all(), [])
        e = self.one(store.load_all(include_retired=True), "glorpterm")
        self.assertEqual((e["status"], e["retired_why"]),
                         ("retired", "not a real coinage"))
        self.assertTrue(any(r.get("verb") == "store.reject"
                            and r.get("target") == "glorpterm"
                            for r in pk.read_events(50)))

    def test_reject_guards(self):
        e, err = store.reject("ghost", TS)
        self.assertIsNone(e)
        self.assertIn("not found", err)
        self.add("lexicon", "liveterm | a live one")
        e, err = store.reject("liveterm", TS)
        self.assertIsNone(e)
        self.assertIn("not a candidate", err)

    def test_reject_cli(self):
        self.add("prior", "x-law | wrong inference | 0.6 | xkw", "--candidate")
        rc, out, _ = self.run_cli(["reject", "x-law", "misread", "the", "log"])
        self.assertEqual(rc, 0)
        self.assertIn("REJECTED 'x-law'", out)
        self.assertEqual(self.one(store.load_all(include_retired=True),
                                  "x-law")["retired_why"], "misread the log")
        rc, _, err = self.run_cli(["reject"])
        self.assertEqual(rc, 2)
        self.assertIn("usage", err)

    def test_list_candidates_surface(self):
        self.add("lexicon", "glorpterm | a coined word", "--candidate")
        self.add("lexicon", "realterm | a live one")   # live, not a candidate
        rc, out, _ = self.run_cli(["list", "--candidates"])
        self.assertEqual(rc, 0)
        self.assertIn("glorpterm", out)
        self.assertNotIn("realterm", out)
        self.assertIn("helm store confirm glorpterm", out)
        # a store with no candidates says so
        store.confirm("glorpterm", TS)
        rc, out, _ = self.run_cli(["list", "--candidates"])
        self.assertIn("no candidates", out)

    def test_confirm_promotes_and_fires(self):
        self.add("lexicon", "glorpterm | a coined word", "--candidate")
        e, err = store.confirm("glorpterm", TS)
        self.assertIsNone(err)
        self.assertEqual((e["status"], e["source"]), ("live", "explicit"))
        # reload from disk: live, the candidate status line is gone, fires JIT
        e = self.one(store.load_all(), "glorpterm")
        self.assertEqual(e["status"], "live")
        with open(e["path"]) as f:
            self.assertNotIn("status: candidate", f.read())
        self.assertEqual([x["id"] for x in store.resolve_prompt("glorpterm now")],
                         ["glorpterm"])
        # the promotion left an events receipt
        self.assertTrue(any(r.get("verb") == "store.confirm"
                            and r.get("target") == "glorpterm"
                            for r in pk.read_events(50)))

    def test_confirm_edit_swaps_definition(self):
        self.add("lexicon", "glorpterm | first guess", "--candidate")
        e, err = store.confirm("glorpterm", TS, new_statement="the sharpened sense")
        self.assertIsNone(err)
        self.assertEqual(self.one(store.load_all(), "glorpterm")["definition"],
                         "the sharpened sense")

    def test_confirm_guards(self):
        e, err = store.confirm("ghost", TS)
        self.assertIsNone(e)
        self.assertIn("not found", err)
        self.add("lexicon", "liveterm | a live one")
        e, err = store.confirm("liveterm", TS)
        self.assertIsNone(e)
        self.assertIn("not a candidate", err)

    def test_confirm_cli_edit(self):
        self.add("lexicon", "glorpterm | first", "--candidate")
        rc, out, _ = self.run_cli(["confirm", "glorpterm", "--edit", "the real sense"])
        self.assertEqual(rc, 0)
        self.assertIn("CONFIRMED 'glorpterm'", out)
        self.assertIn("edited", out)
        self.assertEqual(self.one(store.load_all(), "glorpterm")["definition"],
                         "the real sense")

    def test_candidate_over_live_lexicon_refused(self):
        # lexicon's redefine-freely exemption must not let a CANDIDATE add
        # de-canonize a LIVE term (writing status:candidate in place destroys
        # the human-confirmed definition and drops it out of inject)
        self.add("lexicon", "glorpterm | the confirmed sense")
        rc, _, err = self.add("lexicon", "glorpterm | an agent guess", "--candidate")
        self.assertEqual(rc, 1)
        self.assertIn("already LIVE", err)
        self.assertIn("de-canonize", err)
        # the confirmed definition is untouched and still fires
        e = self.one(store.load_all(), "glorpterm")
        self.assertEqual((e["status"], e["definition"]),
                         ("live", "the confirmed sense"))
        self.assertEqual(store.candidates(), [])
        self.assertEqual([x["id"] for x in store.resolve_prompt("glorpterm now")],
                         ["glorpterm"])
        # candidate-over-candidate stays a legal guess update...
        self.add("lexicon", "newterm | first guess", "--candidate")
        rc, _, _ = self.add("lexicon", "newterm | better guess", "--candidate")
        self.assertEqual(rc, 0)
        self.assertEqual(self.one(store.candidates(), "newterm")["definition"],
                         "better guess")
        # ...and a rejected term re-captures cleanly (retired != live)
        store.reject("newterm", TS, why="off")
        rc, _, _ = self.add("lexicon", "newterm | third guess", "--candidate")
        self.assertEqual(rc, 0)
        self.assertEqual(self.one(store.candidates(), "newterm")["status"],
                         "candidate")

    def test_confirm_reject_ambiguous_cross_type_refused(self):
        # candidates mint in all four types now — a bare id shared across
        # types must never silently ratify/retire _find's typed-first winner
        self.add("prior", "dupx | a belief guess | 0.6 | dupxkw", "--candidate")
        self.add("lexicon", "dupx | a term guess", "--candidate")
        for verb in (store.confirm, store.reject):
            e, err = verb("dupx", TS)
            self.assertIsNone(e)
            self.assertIn("ambiguous", err)
            self.assertIn("lexicon", err)
            self.assertIn("prior", err)
        self.assertEqual(len(store.candidates()), 2)  # nothing moved
        # the type qualifier resolves it — and confirms the RIGHT entry
        e, err = store.confirm("dupx", TS, ctype="lexicon")
        self.assertIsNone(err)
        self.assertEqual(e["type"], "lexicon")
        self.assertEqual(self.one(store.load_all(), "dupx")["type"], "lexicon")
        # one candidate left -> the bare id is unambiguous again, and the
        # candidate-first pick beats _find's typed-first live-lexicon shadow
        e, err = store.reject("dupx", TS, why="wrong lane")
        self.assertIsNone(err)
        self.assertEqual((e["type"], e["status"]), ("prior", "retired"))
        # bad qualifier is refused before anything resolves
        e, err = store.confirm("dupx", TS, ctype="episodic")
        self.assertIsNone(e)
        self.assertIn("unknown --type", err)

    def test_ambiguous_candidates_cli_type_flag_and_hints(self):
        self.add("prior", "dupx | a belief guess | 0.6 | dupxkw", "--candidate")
        self.add("lexicon", "dupx | a term guess", "--candidate")
        self.add("heuristic", "solo | lone move | glorpwork", "--candidate")
        # list hints carry the qualifier ONLY where the slug is shared
        rc, out, _ = self.run_cli(["list", "--candidates"])
        self.assertIn("helm store confirm dupx --type prior", out)
        self.assertIn("helm store reject dupx --type lexicon", out)
        self.assertNotIn("solo --type", out)
        # bare CLI confirm refuses with the disambiguation
        rc, _, err = self.run_cli(["confirm", "dupx"])
        self.assertEqual(rc, 1)
        self.assertIn("ambiguous", err)
        rc, _, _ = self.run_cli(["confirm", "dupx", "--type", "lexicon"])
        self.assertEqual(rc, 0)
        self.assertEqual(self.one(store.load_all(), "dupx")["type"], "lexicon")
        rc, _, _ = self.run_cli(["reject", "dupx", "--type", "prior", "not", "real"])
        self.assertEqual(rc, 0)
        self.assertEqual([x["id"] for x in store.candidates()], ["solo"])
        st = {e["type"]: e["status"]
              for e in store.load_all(include_retired=True) if e["id"] == "dupx"}
        self.assertEqual(st, {"lexicon": "live", "prior": "retired"})
        # a dangling --type is a usage error, not a silent bare-id fall-through
        rc, _, err = self.run_cli(["confirm", "solo", "--type"])
        self.assertEqual(rc, 2)
        self.assertIn("--type needs", err)

    def test_readd_after_reject_scrubs_tombstone_receipt(self):
        # rejected -> re-add mints a FRESH lifecycle: the heuristic/reference
        # branches must scrub the retire receipt exactly like the prior branch
        # ("live but retired_ts X" corrupts provenance)
        for typ, first, again in (
                ("heuristic", "h1 | bad move | glorpwork",
                 "h1 | good move | glorpwork"),
                ("reference", "r1 | wrong paper | https://x.example | glorppaper",
                 "r1 | right paper | https://x.example | glorppaper")):
            eid = typ[0] + "1"
            rc, _, _ = self.add(typ, first, "--candidate")
            self.assertEqual(rc, 0, typ)
            _, err = store.reject(eid, TS, why="bad " + typ)
            self.assertIsNone(err, typ)
            rc, _, _ = self.add(typ, again)
            self.assertEqual(rc, 0, typ)
            e = self.one(store.load_all(), eid)
            self.assertEqual(e["status"], "live", typ)
            with open(e["path"]) as f:
                raw = f.read()
            self.assertNotIn("retired", raw, typ)


class ProvisionalTierTest(StoreBase):
    """Provisional tier (owner canon 2026-07-22): a candidate a cross-family /x
    review has cleared goes PROVISIONALLY LIVE — it FIRES through the resolver
    like live but stays visibly [provisional]-tagged until the owner ratifies
    (confirm) or rejects it. xrev-clear is the graduation gate; an un-cleared
    candidate still fires NOTHING (the hard law never weakens)."""

    def add(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = store.cmd_store(["add", *args])
        return rc, out.getvalue(), err.getvalue()

    def run_cli(self, args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = store.cmd_store(list(args))
        return rc, out.getvalue(), err.getvalue()

    def test_three_state_resolver_law(self):
        # the whole point pinned in one place: live fires, provisional fires
        # (usable knowledge), candidate fires NOTHING.
        self.seed_prior("live-law", "the confirmed truth", keywords="glorpwork")
        self.add("prior", "prov-law | the cleared belief | 0.7 | glorpwork", "--candidate")
        self.add("prior", "cand-law | the raw guess | 0.7 | glorpwork", "--candidate")
        store.xrev_clear("prov-law", TS, by="codex-seat")
        got = [e["id"] for e in store.resolve_prompt("glorpwork now")]
        self.assertIn("live-law", got)
        self.assertIn("prov-law", got, "provisional must fire like live")
        self.assertNotIn("cand-law", got, "candidate must stay fully excluded")
        # load_all default surfaces live + provisional, never the candidate
        ids = {e["id"]: e["status"] for e in store.load_all()}
        self.assertEqual(ids, {"live-law": "live", "prov-law": "provisional"})

    def test_xrev_clear_records_reviewer_and_persists(self):
        self.add("prior", "x-law | inferred belief | 0.7 | glorpwork", "--candidate")
        e, err = store.xrev_clear("x-law", TS, by="codex-seat")
        self.assertIsNone(err)
        self.assertEqual((e["status"], e["xrev_by"], e["xrev_ts"]),
                         ("provisional", "codex-seat", TS))
        # reload from disk: status + who/when receipt persisted in the file
        e = self.one(store.load_all(), "x-law")
        self.assertEqual((e["status"], e["xrev_by"]), ("provisional", "codex-seat"))
        with open(e["path"]) as f:
            raw = f.read()
        self.assertIn("  status: provisional", raw)
        self.assertIn("  xrev_by: codex-seat", raw)
        # a prior logs the clearance to its own evidence_log (who/when)
        r = e["evidence_log"][-1]
        self.assertEqual((r["type"], r["by"]), ("xrev-cleared", "codex-seat"))
        # the events journal carries the mutation receipt
        self.assertTrue(any(row.get("verb") == "store.xrev_clear"
                            and row.get("target") == "x-law"
                            for row in pk.read_events(50)))

    def test_xrev_clear_all_types_carry_the_file_receipt(self):
        # the non-prior types have no evidence_log, so the durable receipt is the
        # xrev_by/xrev_ts file fields (what the web panel/CLI display reads)
        for args in (("lexicon", "glorpterm | a cleared coinage"),
                     ("heuristic", "x-move | try glorp first | glorpwork"),
                     ("reference", "x-ref | the glorp paper | https://x.example "
                                   "| glorppaper")):
            self.add(*args, "--candidate")
        for eid in ("glorpterm", "x-move", "x-ref"):
            e, err = store.xrev_clear(eid, TS, by="opus-seat")
            self.assertIsNone(err, eid)
            self.assertEqual(e["status"], "provisional", eid)
            e = self.one(store.load_all(), eid)
            self.assertEqual(e["xrev_by"], "opus-seat", eid)
            with open(e["path"]) as f:
                self.assertIn("xrev_by: opus-seat", f.read(), eid)

    def test_xrev_clear_guards(self):
        self.add("prior", "x-law | guess | 0.6 | glorpwork", "--candidate")
        # a reviewer is mandatory — the verb attests a review happened
        e, err = store.xrev_clear("x-law", TS, by="")
        self.assertIsNone(e)
        self.assertIn("--by", err)
        # not found
        e, err = store.xrev_clear("ghost", TS, by="r")
        self.assertIsNone(e)
        self.assertIn("not found", err)
        # a live entry is not a candidate
        self.add("lexicon", "liveterm | a live one")
        e, err = store.xrev_clear("liveterm", TS, by="r")
        self.assertIsNone(e)
        self.assertIn("not a candidate", err)
        # already provisional -> refused (graduate candidates only, once)
        store.xrev_clear("x-law", TS, by="r1")
        e, err = store.xrev_clear("x-law", TS, by="r2")
        self.assertIsNone(e)
        self.assertIn("not a candidate", err)
        self.assertIn("provisional", err)

    def test_xrev_clear_ambiguous_cross_type_refused(self):
        self.add("prior", "dupx | belief guess | 0.6 | dupxkw", "--candidate")
        self.add("lexicon", "dupx | a term guess", "--candidate")
        e, err = store.xrev_clear("dupx", TS, by="r")
        self.assertIsNone(e)
        self.assertIn("ambiguous", err)
        e, err = store.xrev_clear("dupx", TS, by="r", ctype="lexicon")
        self.assertIsNone(err)
        self.assertEqual((e["type"], e["status"]), ("lexicon", "provisional"))
        # the prior sibling is untouched — still a candidate
        self.assertEqual(self.one(store.candidates(), "dupx")["type"], "prior")

    def test_confirm_ratifies_a_provisional(self):
        self.add("prior", "x-law | cleared belief | 0.7 | glorpwork", "--candidate")
        store.xrev_clear("x-law", TS, by="codex-seat")
        self.assertEqual([e["id"] for e in store.resolve_prompt("glorpwork")], ["x-law"])
        e, err = store.confirm("x-law", TS)
        self.assertIsNone(err)
        self.assertEqual((e["status"], e["source"]), ("live", "explicit"))
        e = self.one(store.load_all(), "x-law")
        self.assertEqual(e["status"], "live")
        # the promotion receipt names the prior state; xrev provenance survives
        r = e["evidence_log"][-1]
        self.assertEqual(r["type"], "confirmed")
        self.assertIn("provisional -> live", r["reason"])
        self.assertEqual(e["xrev_by"], "codex-seat")
        # and it now fires UNtagged (the [provisional] mark is gone)
        rc, out, _ = self.run_cli(["resolve", "glorpwork"])
        self.assertIn("x-law", out)
        self.assertNotIn("[provisional]", out)

    def test_reject_retires_a_provisional_in_place(self):
        self.add("lexicon", "glorpterm | a cleared coinage", "--candidate")
        store.xrev_clear("glorpterm", TS, by="r")
        path = self.one(store.load_all(), "glorpterm")["path"]
        e, err = store.reject("glorpterm", TS, why="wrong after all")
        self.assertIsNone(err)
        self.assertEqual(e["status"], "retired")
        self.assertTrue(os.path.isfile(path), "the record law: the file stays")
        # gone from every injecting surface
        self.assertEqual(store.load_all(), [])
        self.assertEqual(store.resolve_prompt("is glorpterm here"), [])
        e = self.one(store.load_all(include_retired=True), "glorpterm")
        self.assertEqual((e["status"], e["retired_why"]), ("retired", "wrong after all"))

    def test_provisional_marked_in_cli_list_and_resolve(self):
        self.add("prior", "x-law | cleared | 0.7 | glorpwork", "--candidate")
        store.xrev_clear("x-law", TS, by="r")
        rc, out, _ = self.run_cli(["list"])
        self.assertIn("x-law", out)
        self.assertIn("[provisional]", out)
        rc, out, _ = self.run_cli(["resolve", "glorpwork here"])
        self.assertIn("[provisional]", out)
        self.assertIn("x-law", out)

    def test_xrev_clear_cli(self):
        self.add("prior", "x-law | guess | 0.7 | glorpwork", "--candidate")
        rc, out, _ = self.run_cli(["xrev-clear", "x-law", "--by", "codex-seat"])
        self.assertEqual(rc, 0)
        self.assertIn("XREV-CLEARED 'x-law'", out)
        self.assertIn("provisional", out)
        self.assertEqual(self.one(store.load_all(), "x-law")["status"], "provisional")
        # missing --by is a usage error (rc 2), not a silent clear
        self.add("prior", "y-law | guess | 0.7 | ylawkw", "--candidate")
        rc, _, err = self.run_cli(["xrev-clear", "y-law"])
        self.assertEqual(rc, 2)
        self.assertIn("--by", err)
        self.assertEqual(self.one(store.candidates(), "y-law")["status"], "candidate")

    def test_readd_scrubs_stale_xrev_receipt(self):
        # cleared -> rejected -> re-added: the fresh candidate must NOT carry the
        # prior xrev clearance ("provisional receipt on a raw candidate" corrupts
        # provenance — same law as the retire-receipt scrub)
        self.add("prior", "x-law | first | 0.7 | glorpwork", "--candidate")
        store.xrev_clear("x-law", TS, by="r")
        store.reject("x-law", TS, why="wrong")
        self.add("prior", "x-law | second guess | 0.7 | glorpwork", "--candidate")
        e = self.one(store.candidates(), "x-law")
        self.assertEqual((e["status"], e["xrev_by"]), ("candidate", ""))
        with open(e["path"]) as f:
            self.assertNotIn("xrev_by", f.read())


class NotifyOnGraduationTest(StoreBase):
    """Push-on-graduation (owner steer 2026-07-23: the provisional queue must
    ROUTINELY reach the owner). xrev_clear fires ONE optional ntfy push when
    HELM_NTFY_TOPIC is set. Hermetic: urllib.request.urlopen is mocked — no test
    ever touches the network. Laws under test: bare-topic -> ntfy.sh URL, full
    URL as-is, unset -> zero network calls, a down notifier is fail-open (the
    graduation still lands + a one-line journal note), and plain candidate
    CAPTURE never notifies (only graduation does)."""

    def _candidate(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            store.cmd_store(["add", *args, "--candidate"])

    def test_graduation_pushes_ntfy_on_a_bare_topic(self):
        self._candidate("prior", "x-law | cleared belief | 0.7 | glorpwork")
        with mock.patch.dict(os.environ, {"HELM_NTFY_TOPIC": "helmqueue"}), \
                mock.patch("urllib.request.urlopen") as uo:
            e, err = store.xrev_clear("x-law", TS, by="codex-seat")
        self.assertIsNone(err)
        self.assertEqual(e["status"], "provisional")       # graduation succeeded
        self.assertEqual(uo.call_count, 1)
        req = uo.call_args[0][0]
        self.assertEqual(uo.call_args[1]["timeout"], 3)     # 3s timeout
        self.assertEqual(req.full_url, "https://ntfy.sh/helmqueue")
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(req.data, b"helm: prior x-law now provisionally live - "
                                   b"review when convenient")
        self.assertTrue(req.get_header("Title"))            # a Title header rides

    def test_graduation_uses_a_full_url_as_is(self):
        self._candidate("lexicon", "glorpterm | a cleared coinage")
        with mock.patch.dict(os.environ,
                             {"HELM_NTFY_TOPIC": "https://ntfy.example/team-helm"}), \
                mock.patch("urllib.request.urlopen") as uo:
            store.xrev_clear("glorpterm", TS, by="opus-seat")
        req = uo.call_args[0][0]
        self.assertEqual(req.full_url, "https://ntfy.example/team-helm")
        self.assertEqual(req.data, b"helm: lexicon glorpterm now provisionally live "
                                   b"- review when convenient")

    def test_unset_topic_makes_no_network_call(self):
        self._candidate("prior", "x-law | cleared belief | 0.7 | glorpwork")
        with mock.patch.dict(os.environ), \
                mock.patch("urllib.request.urlopen",
                           side_effect=AssertionError("no network call when unset")) as uo:
            os.environ.pop("HELM_NTFY_TOPIC", None)
            os.environ.pop("MELD_NTFY_TOPIC", None)  # the home.env legacy fallback too
            e, err = store.xrev_clear("x-law", TS, by="codex-seat")
        self.assertIsNone(err)
        self.assertEqual(e["status"], "provisional")
        self.assertEqual(uo.call_count, 0)

    def test_down_notifier_is_fail_open_with_a_journal_note(self):
        self._candidate("prior", "x-law | cleared belief | 0.7 | glorpwork")
        with mock.patch.dict(os.environ, {"HELM_NTFY_TOPIC": "helmqueue"}), \
                mock.patch("urllib.request.urlopen",
                           side_effect=OSError("connection refused")):
            e, err = store.xrev_clear("x-law", TS, by="codex-seat")
        # fail-open: the graduation still lands, on disk and firing
        self.assertIsNone(err)
        self.assertEqual(e["status"], "provisional")
        e = self.one(store.load_all(), "x-law")
        self.assertEqual(e["status"], "provisional")
        self.assertEqual([x["id"] for x in store.resolve_prompt("glorpwork now")],
                         ["x-law"])
        # and the miss is journaled as one line (never silent)
        self.assertTrue(any(r.get("verb") == "store.notify_failed"
                            and r.get("target") == "x-law"
                            for r in pk.read_events(50)))

    def test_plain_candidate_capture_never_notifies(self):
        # candidates are agent-noise — only graduation to provisional pushes
        with mock.patch.dict(os.environ, {"HELM_NTFY_TOPIC": "helmqueue"}), \
                mock.patch("urllib.request.urlopen") as uo:
            self._candidate("prior", "x-law | just captured | 0.7 | glorpwork")
        self.assertEqual(uo.call_count, 0)
        self.assertEqual(self.one(store.candidates(), "x-law")["status"], "candidate")


if __name__ == "__main__":
    unittest.main()


class ReadScopeTest(unittest.TestCase):
    """`store.load.read_scope` — one store read per OPERATION, never per row.

    MEASURED 2026-07-31. `helm lr list` / `/api/lr` took 25-41s to project 312
    land loops while the owner's console card budgets 12s, so the land pipeline
    rendered "DISPATCH LEDGER UNREADABLE" on the one surface built to show it —
    and the ledger was fine. The profile put 30.7 of 41 seconds in a single
    chain: `_lr` -> `approval_tier` -> `load_certain_policy` -> `load_all`,
    re-walking the ENTIRE typed store PER ROW. 179 rows produced 716 root loads
    and 244,335 frontmatter parses of the same 1,308 files. After the fix: 11.5s
    for the same 312 rows.
    """

    def test_inside_a_scope_the_store_is_read_ONCE(self):
        from helm.store import load as L
        calls = []
        real = L._load_all_uncached
        try:
            L._load_all_uncached = lambda *a, **k: (calls.append(1), real(*a, **k))[1]
            with L.read_scope():
                for _ in range(5):
                    L.load_all()
        finally:
            L._load_all_uncached = real
        self.assertEqual(len(calls), 1,
                         "the store was read %d times inside one scope" % len(calls))

    def test_OUTSIDE_a_scope_nothing_is_cached(self):
        """THE CONTROL THAT BOUNDS THE RISK. A cache that outlives its operation
        would serve stale policy — the exact failure class this fix exists
        inside (a web server served 40h-old code today; the owner console served
        an 8-day-old render). Outside a scope behaviour must be byte-identical
        to before: every call re-reads."""
        from helm.store import load as L
        calls = []
        real = L._load_all_uncached
        try:
            L._load_all_uncached = lambda *a, **k: (calls.append(1), real(*a, **k))[1]
            for _ in range(3):
                L.load_all()
        finally:
            L._load_all_uncached = real
        self.assertEqual(len(calls), 3, "a read was cached outside any scope")

    def test_the_cache_DROPS_when_the_scope_exits(self):
        """A scope must not leak into the next operation."""
        from helm.store import load as L
        with L.read_scope():
            L.load_all()
            self.assertTrue(L._scope_state().cache,
                            "nothing was cached inside a scope")
        self.assertFalse(L._scope_state().cache, "the cache survived its scope")

    def test_nested_scopes_share_and_only_the_OUTERMOST_clears(self):
        """Re-entrant on purpose: a caller must not need to know whether its
        callee also scopes. If an inner exit cleared, the outer operation would
        silently go back to per-row reads and the fix would evaporate under
        composition."""
        from helm.store import load as L
        with L.read_scope():
            L.load_all()
            with L.read_scope():
                L.load_all()
            self.assertTrue(L._scope_state().cache,
                            "an inner scope exit cleared the outer cache")
        self.assertFalse(L._scope_state().cache)

    def test_two_THREADS_never_share_a_scope(self):
        """The finding, and the one that made this cache unsafe in
        production: helm web is a ThreadingHTTPServer, so two requests overlap
        inside ONE interpreter. With a module-level dict and a class-attribute
        depth, thread A's snapshot answered thread B's read and A's exit cleared
        the cache while B was still inside its scope.

        The probe is deterministic, not timing-hopeful: B is released only once
        A is provably INSIDE its scope with a cached read, which is exactly the
        interleaving that used to fail. A must see its OWN project and B must
        see B's."""
        from helm.store import load as L
        a_inside, b_done = threading.Event(), threading.Event()
        seen = {}

        def watcher(name, gate_set, gate_wait):
            def run():
                with L.read_scope():
                    L.load_all(project=name)
                    seen[name] = dict(L._scope_state().cache)
                    if gate_set:
                        gate_set.set()
                    if gate_wait:
                        gate_wait.wait(timeout=5)
            return run

        ta = threading.Thread(target=watcher("proj-a", a_inside, b_done))
        ta.start()
        self.assertTrue(a_inside.wait(timeout=5), "thread A never entered")
        tb = threading.Thread(target=watcher("proj-b", None, None))
        tb.start()
        tb.join(timeout=10)
        b_done.set()
        ta.join(timeout=10)

        a_keys = [k[0] for k in seen.get("proj-a", {})]
        b_keys = [k[0] for k in seen.get("proj-b", {})]
        self.assertEqual(a_keys, ["proj-a"],
                         "thread A's cache saw another thread's read: %r" % a_keys)
        self.assertEqual(b_keys, ["proj-b"],
                         "thread B's cache saw another thread's read: %r" % b_keys)

    def test_a_caller_MUTATING_a_result_cannot_poison_the_next_read(self):
        """The second finding. `list(hit)` built a new LIST around the
        SAME entry dicts, so mutating one entry changed what every later read in
        the scope returned — and the miss path was worse still, handing the
        caller the very list now sitting in the cache.

        Both reads below are inside ONE scope, so the second is served from the
        cache. It must not carry the first caller's edit."""
        from helm.store import load as L
        with L.read_scope():
            first = L.load_all()
            if not first:
                self.skipTest("no store entries to mutate")
            first[0]["statement"] = "POISONED"
            first[0]["nested"] = [{"deep": "clean"}]
            second = L.load_all()
            self.assertNotEqual(second[0].get("statement"), "POISONED",
                                "a caller's edit reached the next read")
            self.assertNotIn("nested", second[0],
                             "a caller's added key reached the next read")

    def test_a_NESTED_dict_inside_a_list_is_also_caller_owned(self):
        """The R2 finding, and it killed a bound argued for earlier.

        My first fix copied one level down and its docstring claimed list values
        were the only mutable ones a store entry carries. On the real 1,308-row
        corpus that is false: 892 entries have confidence_history / evidence_log
        lists whose ELEMENTS ARE DICTS, and mutating one of those nested dicts
        poisoned the next cached read.

        It also caught the arm that was supposed to test this being VACUOUS —
        my probe searched the first entry for any list field and skipped when it
        found none, which on this corpus is exactly what happened. So the
        fixture here is SYNTHETIC and guaranteed nested: no search, no guard, no
        way for the assertion to pass by not running."""
        from helm.store import load as L
        planted = [{"id": "x", "type": "premise",
                    "confidence_history": [{"conf": 1.0, "by": "clean"}]}]
        real = L._load_all_uncached
        try:
            L._load_all_uncached = lambda *a, **k: [dict(e) for e in planted]
            with L.read_scope():
                first = L.load_all()
                first[0]["confidence_history"][0]["by"] = "POISONED"
                second = L.load_all()
                self.assertEqual(second[0]["confidence_history"][0]["by"],
                                 "clean",
                                 "a mutation two levels down reached the "
                                 "next cached read")
        finally:
            L._load_all_uncached = real

    def test_each_return_site_hands_out_its_OWN_copy(self):  # noqa: VACUOUS_ASSERTION — the arms sit inside try/finally only to restore the patched loader; mutation-proven non-vacuous (hit-site neutered -> 1 red, miss-site -> 3 red, restored -> green)
        """The reissue finding, closed as TWO NAMED ARMS on one
        guaranteed fixture. The miss path (`_detached(out)`) and the hit path
        (`_detached(hit)`) are SEPARATE return sites, and a verifier that only
        ever mutates the miss result stays green while the hit site degrades
        to `list(hit)` — measured: all 7 ReadScopeTest green, warm cache ->
        mutate hit's nested dict -> third read POISONED. Mutation contract,
        each site separately: neuter the MISS site (`list(out)`) and the MISS
        arm goes red; neuter the HIT site (`list(hit)`) and the HIT arm goes
        red. The corpus-backed sibling above stays as supplementary coverage
        only — this fixture is planted, so neither arm can pass by not
        running."""
        from helm.store import load as L
        planted = [{"id": "x", "type": "premise",
                    "confidence_history": [{"conf": 1.0, "by": "clean"}]}]
        real = L._load_all_uncached
        try:
            L._load_all_uncached = lambda *a, **k: [dict(e) for e in planted]
            with L.read_scope():
                # MISS arm: the first read is the miss-site copy; its
                # mutation must not reach the first cache hit.
                miss = L.load_all()
                miss[0]["confidence_history"][0]["by"] = "POISONED-MISS"
                hit1 = L.load_all()
                self.assertEqual(hit1[0]["confidence_history"][0]["by"],
                                 "clean", "the MISS-site copy shares dicts "
                                 "with the cache")
                # HIT arm: hit1 is a hit-site copy; mutating it must not
                # reach a LATER hit.
                hit1[0]["confidence_history"][0]["by"] = "POISONED-HIT"
                hit2 = L.load_all()
                self.assertEqual(hit2[0]["confidence_history"][0]["by"],
                                 "clean", "the HIT-site copy shares dicts "
                                 "with the cache")
        finally:
            L._load_all_uncached = real


class RetagWidensTheRetrievalKeywords(StoreBase):
    """#188 — `/learn` mandates a resolve-sharpen-RETEST loop and the store had no
    verb for the widen step.

    MEASURED 2026-08-04, two seats in one night, both following the skill
    correctly and both landing wrong: one hand-edited frontmatter (silent — the
    mutation-receipt trail bypassed), the other minted `-r2` ids and superseded
    twice (loud but FALSE — a supersession that did not happen, and the DF
    weight of shared keywords split so BOTH files rank lower than either alone).
    """

    def _heuristic(self, hid="hmove", trigger="alpha,beta"):
        return store.write_heuristic(
            {"id": hid, "move": "do the thing", "trigger": trigger},
            root_dir=self.global_dir("heuristics"))

    def _ondisk(self, path, key):
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("  %s:" % key):
                    return line.split(":", 1)[1].strip()
        return None

    def test_a_heuristic_edit_actually_REACHES_DISK(self):
        """THE TRAP, and the reason this verb could have shipped as a no-op.
        `write_heuristic` reads `trigger` BEFORE `keywords`, so setting only
        `keywords` writes the OLD trigger back and reports success — the author
        then resolve-tests, still misses, and concludes the store cannot be
        widened."""
        path = self._heuristic()
        self.assertEqual(self._ondisk(path, "trigger"), "alpha,beta")  # control
        e, err = store.retag("hmove", TS, add="gamma")
        self.assertIsNone(err)
        # POSITIVE CONTROL ON THE SAME CALL'S OTHER CHANNEL — `e` and `err`
        # are two channels of one result. A second retag() would NOT do:
        # every call mints a fresh producer, so it could not vouch for this
        # one. Asserting the returned entry also states the stronger thing:
        # the call gave back the right row, not merely that it kept quiet.
        self.assertEqual(e["keywords"], "alpha,beta,gamma")
        self.assertEqual(self._ondisk(path, "trigger"), "alpha,beta,gamma")

    def test_a_prior_edit_reaches_disk_too(self):
        path = self.seed_prior("plaw", "Always do X.", keywords="one,two")
        self.assertEqual(self._ondisk(path, "keywords"), "one,two")   # control
        e, err = store.retag("plaw", TS, add="three")
        self.assertIsNone(err)
        self.assertEqual(e["keywords"], "one,two,three")   # same call, other channel
        self.assertEqual(self._ondisk(path, "keywords"), "one,two,three")

    def test_duplicates_are_dropped_case_insensitively(self):
        """The resolver lowercases, so `Alpha` and `alpha` are ONE probe
        wearing two spellings."""
        self.seed_prior("plaw", "S", keywords="alpha,beta")
        e, err = store.retag("plaw", TS, add="ALPHA,Beta,gamma")
        self.assertIsNone(err)
        self.assertEqual(store._kw_list(e["keywords"]),
                         ["alpha", "beta", "gamma"])

    def test_set_dedups_case_variants_within_its_own_argument(self):
        """A MUTATION FOUND THIS HOLE. Making `_kw_list`'s dedup
        case-SENSITIVE left the suite green, because the add path dedups again
        in `retag` against a lowercased set — so the test above proved retag's
        logic, never `_kw_list`'s. The REPLACE path has no second dedup, so
        this is where that function is load-bearing and where a case pair would
        otherwise land two spellings of one probe in the file."""
        self.seed_prior("plaw", "S", keywords="old")
        e, err = store.retag("plaw", TS, replace="Alpha,alpha,ALPHA,beta")
        self.assertIsNone(err)
        self.assertEqual(store._kw_list(e["keywords"]), ["Alpha", "beta"])

    def test_author_order_is_preserved(self):
        self.seed_prior("plaw", "S", keywords="zeta,alpha")
        e, _ = store.retag("plaw", TS, add="mid")
        self.assertEqual(store._kw_list(e["keywords"]), ["zeta", "alpha", "mid"])

    def test_remove_narrows_a_spammer(self):
        self.seed_prior("plaw", "S", keywords="alpha,beta,gamma")
        e, err = store.retag("plaw", TS, remove="BETA")
        self.assertIsNone(err)
        self.assertEqual(store._kw_list(e["keywords"]), ["alpha", "gamma"])

    def test_set_replaces_the_whole_list(self):
        self.seed_prior("plaw", "S", keywords="alpha,beta")
        e, err = store.retag("plaw", TS, replace="only")
        self.assertIsNone(err)
        self.assertEqual(store._kw_list(e["keywords"]), ["only"])

    def test_set_combined_with_add_is_refused_not_guessed(self):
        self.seed_prior("plaw", "S", keywords="alpha")
        e, err = store.retag("plaw", TS, add="beta", replace="only")
        self.assertIsNone(e)
        self.assertIn("do not combine", err)
        # CONTROL, same call shape: either flag ALONE works, so the refusal is
        # about the combination and not a verb that refuses everything.
        self.assertIsNone(store.retag("plaw", TS, add="beta")[1])
        self.assertIsNone(store.retag("plaw", TS, replace="only")[1])

    def test_emptying_an_entry_is_refused_and_points_at_retire(self):
        """An entry no probe reaches is retired WITHOUT a retirement receipt,
        and it looks live on every listing."""
        self.seed_prior("plaw", "S", keywords="alpha,beta")
        e, err = store.retag("plaw", TS, remove="alpha,beta")
        self.assertIsNone(e)
        self.assertIn("NO keywords", err)
        self.assertIn("store retire", err)
        # CONTROL: removing all-but-one is fine, so the refusal is the EMPTY
        # result and not a verb that cannot remove.
        e2, err2 = store.retag("plaw", TS, remove="alpha")
        self.assertIsNone(err2)
        self.assertEqual(store._kw_list(e2["keywords"]), ["beta"])

    def test_set_to_nothing_is_refused_too(self):
        self.seed_prior("plaw", "S", keywords="alpha")
        self.assertIn("NO keywords", store.retag("plaw", TS, replace=" , ")[1])
        self.assertIsNone(store.retag("plaw", TS, replace="x")[1])   # control

    def test_a_change_leaves_a_receipt_and_a_no_op_does_not(self):
        """The whole point: the widen step stops bypassing the mutation trail
        this store keeps."""
        self.seed_prior("plaw", "S", keywords="alpha")
        with mock.patch.object(pk, "event") as ev:
            store.retag("plaw", TS, add="beta")
            self.assertEqual(ev.call_count, 1)                # the receipt
            self.assertEqual(ev.call_args[0][0], "store.retag")
            ev.reset_mock()
            e, err = store.retag("plaw", TS, add="ALPHA,beta")  # already there
            self.assertIsNone(err)
            self.assertEqual(e["keywords"], "alpha,beta")   # same call, other channel
            self.assertEqual(ev.call_count, 0)                # idempotent

    def test_an_unknown_id_is_a_refusal_not_a_new_entry(self):
        e, err = store.retag("no-such-entry", TS, add="alpha")
        self.assertIsNone(e)
        self.assertIn("not found", err)
        self.seed_prior("plaw", "S", keywords="alpha")        # control
        self.assertIsNone(store.retag("plaw", TS, add="beta")[1])

    def test_an_unknown_type_is_refused_with_the_valid_set(self):
        self.seed_prior("plaw", "S", keywords="alpha")
        _e, err = store.retag("plaw", TS, add="beta", ctype="nonsense")
        self.assertIn("unknown --type", err)
        self.assertIn("heuristic", err)
        # premise is an ALIAS for prior — the CLI's own word must work.
        self.assertIsNone(store.retag("plaw", TS, add="beta",
                                      ctype="premise")[1])


class TheKeywordsVerbClosesTheLoopItOpens(StoreBase):
    """CLI arms. The verb exists so the widen step stops bypassing the store's
    own mutation trail — so its SURFACE has to make the remaining step
    (retest) unmissable, or it just relocates the place people stop early."""

    def _run(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = store.cmd_store(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def test_no_flags_prints_the_current_set_and_changes_nothing(self):
        """A READ, deliberately: the loop is resolve -> LOOK -> widen -> retest,
        and charging a separate verb for the look is how people skip it."""
        path = self.seed_prior("plaw", "S", keywords="alpha,beta")
        rc, out, _ = self._run("keywords", "plaw")
        self.assertEqual(rc, 0)
        # NO SPY HERE. A spy list is its own observable, so asserting only
        # "the recorder never fired" proves nothing about what the verb
        # WROTE. The file itself is the observable that matters, and
        # test_a_change_leaves_a_receipt_and_a_no_op_does_not owns the
        # receipt contract.
        with open(path, encoding="utf-8") as fh:
            self.assertIn("keywords: alpha,beta", fh.read())
        # A MUTATION FOUND THIS HOLE. Deleting the read branch entirely left
        # this test green: with no flags the call fell through to `retag`,
        # which is a NO-OP for an unchanged list and printed a widen summary
        # carrying the same words. So asserting "alpha appears" proved nothing
        # about read mode at all. These two assertions separate them —
        # ONE KEYWORD PER LINE, and NO retest instruction, because a read is
        # not a widen and telling someone to retest a change they did not make
        # is how a surface teaches people to ignore it.
        self.assertIn("\n  alpha\n", out)
        self.assertIn("\n  beta\n", out)
        self.assertNotIn("RETEST", out)
        self.assertIn("2 keywords", out)
        # ...and the SAME surface DOES print it for a real widen, so its
        # absence above is read mode and not a string this verb never emits.
        self.assertIn("RETEST", self._run("keywords", "plaw", "--add", "g")[1])

    def test_a_widen_tells_the_author_the_capture_is_not_done_yet(self):
        """`/learn`: a capture is done at FIRES, never at `stored:`. The verb
        that widens is the right place to say so."""
        self.seed_prior("plaw", "S", keywords="alpha")
        rc, out, _ = self._run("keywords", "plaw", "--add", "beta")
        self.assertEqual(rc, 0)
        self.assertIn("now carries 2 keywords", out)
        self.assertIn("RETEST", out)
        self.assertIn("store resolve", out)
        self.assertIn("must-MISS control", out)   # widening can make a spammer

    def test_an_unknown_id_exits_nonzero_and_names_itself(self):
        rc, _out, err = self._run("keywords", "nope", "--add", "x")
        self.assertEqual(rc, 1)
        self.assertIn("not found", err)
        self.seed_prior("plaw", "S", keywords="alpha")      # control
        ok_rc, ok_out, ok_err = self._run("keywords", "plaw", "--add", "b")
        self.assertEqual(ok_rc, 0)
        self.assertIn("now carries", ok_out)   # same call, other channel
        self.assertEqual(ok_err, "")           # ...so an empty stderr means worked

    def test_bare_verb_prints_usage_and_does_not_guess_an_id(self):
        rc, _out, err = self._run("keywords")
        self.assertEqual(rc, 2)
        self.assertIn("usage: helm store keywords", err)
        self.assertIn("--add", err)


# The duplicate guard weighs shared WORD MASS, not a count of shared probes
# (#271). A genuine sibling therefore has to share real symptom PHRASES, which
# is what a genuine duplicate looks like anyway — the pre-#271 fixture shared
# two bare 2-word cells (mass 4), and that strength of evidence is exactly the
# false-positive population the guard now releases. These two constants are the
# refusing pair every override/scanner test leans on; keeping them as named
# constants stops a future edit from silently weakening the collision and
# turning those tests vacuously green.
DUP_SIBLING_KEYWORDS = ("pane freeze, seat stall on plan prompt, "
                        "plan approval prompt")
DUP_COLLIDING_KEYWORDS = "seat stall on plan prompt, plan approval prompt, tmux"


class AddGateBase(StoreBase):
    """Shared harness for the add-time findability gate tests."""

    def add(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = store.cmd_store(["add", *args])
        return rc, out.getvalue(), err.getvalue()


class AddArgumentScannerTest(AddGateBase):
    """#214: add flags are one validated grammar, not terminal special cases."""

    def _seed_sibling(self):
        self.seed_prior("seat-freeze-law", "seats freeze on plan prompts",
                        keywords=DUP_SIBLING_KEYWORDS)

    def test_rationale_stops_at_trailing_force_new_and_scanning_resumes(self):
        """The reported regression: the override must act, not become prose."""
        self._seed_sibling()
        rc, out, err = self.add(
            "prior", "seat-freeze-two | a second law | 0.7 | "
            + DUP_COLLIDING_KEYWORDS,
            "--rationale", "observed", "in", "two", "incidents", "--force-new")
        self.assertEqual((rc, err), (0, ""))
        self.assertIn("DUP OVERRIDE recorded", out)
        e = self.one(store.load_all(), "seat-freeze-two")
        self.assertEqual(e["evidence_log"][0]["reason"],
                         "observed in two incidents")
        self.assertEqual(e["confidence_history"][0]["reason"],
                         "observed in two incidents")

    def test_reverse_order_keeps_the_exact_rationale(self):
        self._seed_sibling()
        rc, out, err = self.add(
            "prior", "seat-freeze-two | a second law | 0.7 | "
            + DUP_COLLIDING_KEYWORDS,
            "--force-new", "--rationale", "observed", "in", "two", "incidents")
        self.assertEqual((rc, err), (0, ""))
        self.assertIn("DUP OVERRIDE recorded", out)
        e = self.one(store.load_all(), "seat-freeze-two")
        self.assertEqual(e["evidence_log"][0]["reason"],
                         "observed in two incidents")

    def test_source_candidate_and_rationale_work_in_both_orders(self):  # noqa: VACUOUS_ASSERTION — fixed nonempty cases each assert the stored candidate
        cases = (
            ("capture-a", ("--source", "trace-a", "--candidate", "--rationale",
                           "seen", "during", "capture"), "trace-a"),
            ("capture-b", ("--rationale", "seen", "during", "capture",
                           "--candidate", "--source", "trace-b"), "trace-b"),
        )
        for eid, flags, source in cases:
            with self.subTest(eid=eid):
                rc, _, err = self.add(
                    "prior", "%s | an inferred belief | 0.6 | %skw" % (eid, eid),
                    *flags)
                self.assertEqual((rc, err), (0, ""))
                e = self.one(store.candidates(), eid)
                self.assertEqual((e["status"], e["source"]),
                                 ("candidate", source))
                self.assertEqual(e["evidence_log"][0]["reason"],
                                 "seen during capture")

    def test_duplicate_add_flags_refuse_without_store_work(self):  # noqa: VACUOUS_ASSERTION — fixed nonempty cases prove parser refusal before the final absence check
        cases = (
            ("--source", ("--source", "one", "--source", "two")),
            ("--rationale", ("--rationale", "one", "--rationale", "two")),
            ("--candidate", ("--candidate", "--candidate")),
            ("--force-new", ("--force-new", "--force-new")),
        )
        from helm.store import cli as store_cli
        for n, (flag, flags) in enumerate(cases):
            with self.subTest(flag=flag), mock.patch.object(
                    store_cli.pk, "now_ts",
                    side_effect=AssertionError("invalid argv reached store work")):
                rc, out, err = self.add(
                    "prior", "dup-%d | a belief | 0.6 | dupkw%d" % (n, n), *flags)
                self.assertEqual((rc, out), (2, ""))
                self.assertIn("%s may appear only once" % flag, err)
        self.assertEqual(store.load_all(), [])

    def test_missing_or_flag_shaped_values_refuse_without_store_work(self):  # noqa: VACUOUS_ASSERTION — fixed nonempty cases prove parser refusal before the final absence check
        cases = (
            ("missing-source", ("--source",), "--source needs a value"),
            ("empty-source", ("--source", ""), "--source needs a value"),
            ("flag-source", ("--source", "--candidate"),
             "--source needs a value before --candidate"),
            ("missing-rationale", ("--rationale",), "--rationale needs text"),
            ("empty-rationale", ("--rationale", ""), "--rationale needs text"),
            ("flag-rationale", ("--rationale", "--force-new"),
             "--rationale needs text before --force-new"),
        )
        from helm.store import cli as store_cli
        for n, (name, flags, message) in enumerate(cases):
            with self.subTest(name=name), mock.patch.object(
                    store_cli.pk, "now_ts",
                    side_effect=AssertionError("invalid argv reached store work")):
                rc, out, err = self.add(
                    "prior", "missing-%d | a belief | 0.6 | missingkw%d" % (n, n),
                    *flags)
                self.assertEqual((rc, out), (2, ""))
                self.assertIn(message, err)
        self.assertEqual(store.load_all(), [])

    def test_unknown_flag_shaped_args_refuse_in_payload_or_rationale(self):  # noqa: VACUOUS_ASSERTION — fixed nonempty cases prove parser refusal before the final absence check
        cases = (("--wat",), ("--rationale", "because", "--wat"))
        from helm.store import cli as store_cli
        for n, flags in enumerate(cases):
            with self.subTest(flags=flags), mock.patch.object(
                    store_cli.pk, "now_ts",
                    side_effect=AssertionError("invalid argv reached store work")):
                rc, out, err = self.add(
                    "prior", "unknown-%d | a belief | 0.6 | unknownkw%d" % (n, n),
                    *flags)
                self.assertEqual((rc, out), (2, ""))
                self.assertIn("unknown store add option --wat", err)
        self.assertEqual(store.load_all(), [])


class EntryMintGuardTest(AddGateBase):
    """#204 CORE: activation mints pay add's guard; raw lifecycle stays raw."""

    def raw(self, path):
        with open(path, "rb") as f:
            return f.read()

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = store.cmd_store(list(args))
        return rc, out.getvalue(), err.getvalue()

    def test_guard_is_pure_and_accepts_caller_corpus_exclusions(self):  # noqa: VACUOUS_ASSERTION — deferred event data is the positive control for the empty journal
        self.seed_prior("seat-freeze-law", "seats freeze on plan prompts",
                        keywords=DUP_SIBLING_KEYWORDS)
        corpus = store.load_all()
        kw, err, notes, events = store.guard_entry_keywords(
            "prior", "seat-freeze-two", DUP_COLLIDING_KEYWORDS,
            force=True, corpus=corpus)
        self.assertIsNone(err)
        # the author's EXACT string survives at the head; the tail is the stem
        # decomposition of its >= 3-word cells, which the guard reports in notes
        self.assertTrue(kw.startswith(DUP_COLLIDING_KEYWORDS), kw)
        # and the byte-identity contract itself, on an input with nothing to
        # stem: a guard that merely INSPECTED must not rewrite
        untouched, err2, _n, _e = store.guard_entry_keywords(
            "prior", "quiet-law", "alpha, beta", corpus=corpus)
        self.assertIsNone(err2)
        self.assertEqual(untouched, "alpha, beta")
        self.assertTrue(notes)
        self.assertEqual(events[0][0], "store.dup_override")
        self.assertEqual(pk.read_events(20), [],
                         "the pure guard must defer its journal receipt")
        _kw, err, notes, events = store.guard_entry_keywords(
            "prior", "seat-freeze-two", DUP_COLLIDING_KEYWORDS,
            force=True, corpus=corpus, exclusions=(corpus[0],))
        self.assertIsNone(err)
        # excluding the sibling removes the OVERRIDE specifically — asserted by
        # naming it, not by an empty-notes check that any unrelated note breaks
        self.assertEqual(events, [])
        self.assertEqual([n for n in notes if "DUP OVERRIDE" in n], [])
        # The pre-generalization spelling keeps its three-value unpack contract.
        _kw, err, notes = store.guard_add_keywords(
            "prior", "distinct-law", "quota headroom, cred rotation")
        self.assertEqual((err, notes), (None, []))

    def test_writer_failure_emits_no_duplicate_override(self):  # noqa: VACUOUS_ASSERTION — forced duplicate setup and raised writers positively exercise both absence checks
        self.seed_prior("seat-freeze-law", "seats freeze on plan prompts",
                        keywords=DUP_SIBLING_KEYWORDS)
        from helm.store import cli as store_cli
        with mock.patch.object(store_cli, "write_prior",
                               side_effect=ValueError("writer refused")):
            with self.assertRaisesRegex(ValueError, "writer refused"):
                self.add("prior", "seat-freeze-two | a second law | 0.7 | "
                         + DUP_COLLIDING_KEYWORDS, "--force-new")
        self.assertFalse(any(r.get("verb") == "store.dup_override"
                             for r in pk.read_events(20)))
        self.assertIsNone(store._find("seat-freeze-two"))

        path = store.write_prior({
            "id": "seat-freeze-three", "statement": "a third law",
            "confidence": 0.7, "keywords": DUP_COLLIDING_KEYWORDS,
            "status": "candidate", "source": "inferred", "stated_ts": TS,
            "last_updated": TS})
        before = self.raw(path)
        fail = mock.Mock(side_effect=ValueError("activation writer refused"))
        with mock.patch.dict(store._WRITERS, {"prior": fail}):
            with self.assertRaisesRegex(ValueError, "activation writer refused"):
                store.confirm("seat-freeze-three", TS, force=True)
        self.assertEqual(self.raw(path), before)
        self.assertFalse(any(r.get("verb") == "store.dup_override"
                             and r.get("target") == "seat-freeze-three"
                             for r in pk.read_events(20)))

    def test_raw_empty_and_salad_candidates_refuse_activation_byte_identically(self):  # noqa: VACUOUS_ASSERTION — fixed nonempty cases each assert the candidate bytes remain unchanged
        cases = (
            ("empty-candidate", "", lambda eid: store.confirm(eid, TS),
             "NO keywords"),
            ("salad-candidate", "one giant comma missing keyword cell",
             lambda eid: store.xrev_clear(eid, TS, by="codex-seat"),
             "comma-less cell"),
        )
        for eid, keywords, activate, message in cases:
            with self.subTest(eid=eid):
                path = store.write_prior({
                    "id": eid, "statement": "raw candidate", "confidence": 0.7,
                    "keywords": keywords, "status": "candidate",
                    "source": "inferred", "stated_ts": TS,
                    "last_updated": TS})
                before = self.raw(path)
                e, err = activate(eid)
                self.assertIsNone(e)
                self.assertIn(message, err)
                self.assertEqual(self.raw(path), before,
                                 "a refused activation must not rewrite one byte")

    def test_confirm_rechecks_the_current_corpus_and_force_new_is_deferred(self):
        path = store.write_prior({
            "id": "seat-freeze-two", "statement": "a second law",
            "confidence": 0.7, "keywords": DUP_COLLIDING_KEYWORDS,
            "status": "candidate", "source": "inferred", "stated_ts": TS,
            "last_updated": TS})
        self.seed_prior("seat-freeze-law", "seats freeze on plan prompts",
                        keywords=DUP_SIBLING_KEYWORDS)
        before = self.raw(path)
        e, err = store.confirm("seat-freeze-two", TS)
        self.assertIsNone(e)
        self.assertIn("seat-freeze-law", err)
        self.assertEqual(self.raw(path), before)
        rc, out, err = self.run_cli("confirm", "seat-freeze-two", "--force-new")
        self.assertEqual((rc, err), (0, ""))
        self.assertIn("DUP OVERRIDE recorded", out)
        self.assertEqual(self.one(store.load_all(), "seat-freeze-two")["status"],
                         "live")
        self.assertTrue(any(r.get("verb") == "store.dup_override"
                            and r.get("target") == "seat-freeze-two"
                            for r in pk.read_events(20)))

    def test_candidate_to_provisional_stems_the_heuristic_trigger(self):  # noqa: VACUOUS_ASSERTION — stored trigger, derived probes, and live resolve are unconditional positive controls
        store.write_heuristic({
            "id": "stem-move", "move": "inspect the frozen seat",
            "trigger": "seat freezes on plan prompt, pane tail",
            "status": "candidate", "source": "inferred", "stated_ts": TS,
            "last_updated": TS})
        notes = []
        e, err = store.xrev_clear("stem-move", TS, by="codex-seat",
                                  guard_notes=notes)
        self.assertIsNone(err)
        self.assertEqual(e["status"], "provisional")
        self.assertTrue(any("stem probes auto-added" in n for n in notes))
        e = self.one(store.load_all(), "stem-move")
        probes = store._kw_list(e["trigger"])
        self.assertEqual(e["keywords"], e["trigger"])
        for stem in ("seat", "freezes", "prompt", "seat freezes", "plan prompt"):
            self.assertIn(stem, probes)
        self.assertEqual([x["id"] for x in store.resolve_prompt("the seat freezes")],
                         ["stem-move"])

    def test_provisional_to_live_is_lifecycle_only_and_not_relinted(self):  # noqa: VACUOUS_ASSERTION — live rewrite and preserved empty keyword line are both asserted positively
        path = store.write_prior({
            "id": "legacy-provisional", "statement": "already firing",
            "confidence": 0.7, "keywords": "", "status": "provisional",
            "source": "inferred", "stated_ts": TS, "last_updated": TS,
            "xrev_by": "codex-seat", "xrev_ts": TS})
        before = self.raw(path)
        e, err = store.confirm("legacy-provisional", TS)
        self.assertIsNone(err)
        self.assertEqual((e["status"], e["keywords"]), ("live", ""))
        after = self.raw(path)
        self.assertNotEqual(after, before)
        self.assertIn(b"  status: live", after)
        self.assertIn(b"  keywords: \n", after)

    def test_raw_writers_and_nonactivation_lifecycle_remain_unguarded(self):
        paths = (
            store.write_prior({
                "id": "raw-prior", "statement": "raw", "confidence": 0.7,
                "keywords": "", "status": "candidate", "stated_ts": TS,
                "last_updated": TS}),
            store.write_lexicon({"term": "raw-lex", "definition": "raw",
                                 "keywords": ""}),
            store.write_heuristic({
                "id": "raw-heur", "move": "raw", "trigger": "",
                "status": "live", "stated_ts": TS, "last_updated": TS}),
            store.write_reference({
                "id": "raw-ref", "statement": "raw",
                "keywords": "one giant comma missing keyword cell",
                "status": "live", "stated_ts": TS, "last_updated": TS}),
        )
        self.assertTrue(all(os.path.isfile(path) for path in paths))
        rejected, err = store.reject("raw-prior", TS, why="raw lifecycle")
        self.assertIsNone(err)
        self.assertEqual(rejected["status"], "retired")
        retired, err = store.retire("raw-heur", TS, "raw lifecycle")
        self.assertIsNone(err)
        self.assertEqual(retired["status"], "retired")


class AddKeywordLintTest(AddGateBase):
    """GUARD 1 — the three MEASURED degenerate keyword shapes (SA audit
    2026-08-03: 9% EMPTY + 12% comma-missing salad + 4% lone word = 26% of
    the store structurally near-unfindable) refuse at add time, each with the
    teaching cure; healthy fields pass untouched (the controls)."""

    def test_empty_keywords_refuse_and_write_nothing(self):
        rc, out, err = self.add("prior", "no-kw-law | a belief nobody can find | 0.7")
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")
        self.assertIn("NO keywords", err)
        self.assertIn("comma-separated SYMPTOM phrases, 1-3 words", err)
        self.assertEqual(store.load_all(), [])   # a refusal writes NOTHING
        # positive control on the SAME observable: the same add WITH keywords
        # lands, so the empty list above is the refusal and not a dead store
        rc, _, err = self.add("prior",
                              "no-kw-law | a belief nobody can find | 0.7 | pane freeze")
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual([e["id"] for e in store.load_all()], ["no-kw-law"])

    def test_every_add_branch_is_gated(self):
        # one refusal per WRITER BRANCH — a guard wired into three of four
        # branches passes every single-type test above and still leaks
        for args in (("prior", "b1 | s | 0.7"),
                     ("premise", "b2 | s"),
                     ("heuristic", "b3 | s"),
                     ("reference", "b4 | s | https://x.example")):
            rc, _, err = self.add(*args)
            self.assertEqual(rc, 1, args[0])
            self.assertIn("NO keywords", err, args[0])
        self.assertEqual(store.load_all(), [])
        # positive control: one keyworded add lands, so the empty store above
        # is four refusals and not a broken harness
        rc, _, _ = self.add("prior", "b1 | s | 0.7 | pane freeze")
        self.assertEqual(rc, 0)
        self.assertEqual([e["id"] for e in store.load_all()], ["b1"])

    def test_the_measured_salad_shape_refuses(self):
        # the exact measured shape: one comma-less cell that indexes as ONE
        # giant probe only a verbatim repeat of the whole phrase can match
        rc, _, err = self.add(
            "prior", "salad-law | a statement | 0.7 | "
            "workforce team fable opus codex kimi subagent orchestrator role")
        self.assertEqual(rc, 1)
        self.assertIn("comma-less cell of 9 words", err)
        self.assertIn("VERBATIM", err)
        self.assertIn("comma-separated SYMPTOM phrases", err)
        self.assertEqual(store.load_all(), [])
        # positive control: the SAME vocabulary with its commas restored lands
        # — the refusal is about the missing commas and nothing else
        rc, _, err = self.add(
            "prior", "salad-law | a statement | 0.7 | "
            "workforce team, fable opus, codex kimi, subagent orchestrator role")
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual([e["id"] for e in store.load_all()], ["salad-law"])

    def test_the_measured_lone_word_shape_refuses_when_df_common(self):
        # 'infrastructure' is generic IN THIS CORPUS: carried by >= 3 live
        # entries, so as the SOLE probe it discriminates nothing — genericity
        # is measured (df), never a hand-kept word list (house law).
        for i in range(3):
            self.seed_prior("infra-%d" % i, "S%d" % i,
                            keywords="infrastructure, seed-%d" % i)
        rc, _, err = self.add("prior",
                              "lone-law | a statement | 0.7 | infrastructure")
        self.assertEqual(rc, 1)
        self.assertIn("'infrastructure'", err)
        self.assertIn("3 live entries", err)
        self.assertIn("comma-separated SYMPTOM phrases", err)

    def test_a_lone_function_word_refuses_even_in_an_empty_store(self):
        # a GENERIC_KEYWORDS member can never satisfy the specificity guard,
        # so the entry could literally never fire — no df evidence needed
        rc, _, err = self.add("prior", "modal-law | a statement | 0.7 | should")
        self.assertEqual(rc, 1)
        self.assertIn("generic", err)

    def test_controls_healthy_field_and_rare_lone_word_pass(self):
        # the refusals above are about the SHAPES, not a gate that refuses all
        rc, _, err = self.add("prior", "good-law | a findable belief | 0.7 | "
                              + DUP_SIBLING_KEYWORDS)
        self.assertEqual((rc, err), (0, ""))
        # a single RARE coined word is a legitimate probe (df 0 — one seat's
        # coinage is exactly how lexicon symptom vocabulary is born)
        rc, _, err = self.add("prior", "coin-law | another belief | 0.7 | glorpnax")
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(sorted(e["id"] for e in store.load_all()),
                         ["coin-law", "good-law"])   # both actually ON DISK

    def test_lexicon_is_exempt_from_the_empty_case_only(self):
        # the term IS a probe by construction, so empty keywords stay legal...
        rc, _, err = self.add("lexicon", "youable | able to be you")
        self.assertEqual((rc, err), (0, ""))
        # ...but PROVIDED keywords are linted like everyone else's
        rc, _, err = self.add("lexicon", "saladterm | a definition | phrase | "
                              "one giant comma missing keyword cell")
        self.assertEqual(rc, 1)
        self.assertIn("comma-less cell", err)


class AddStemDecompositionTest(AddGateBase):
    """GUARD 2 — a fused phrase is a probe only its VERBATIM repeat can match
    (every measured near-miss). Cells of >= 3 words get their 1-2-word
    content stems AUTO-ADDED (never replacing the original), stopwords
    dropped, total probes capped."""

    def test_a_long_cell_gains_its_stems_and_keeps_the_original(self):
        rc, out, err = self.add("prior", "stem-law | a belief | 0.7 | "
                                "seat freezes on plan prompt, pane tail")
        self.assertEqual((rc, err), (0, ""))
        kws = store._kw_list(self.one(store.load_all(), "stem-law")["keywords"])
        self.assertIn("seat freezes on plan prompt", kws)  # original KEPT
        self.assertIn("pane tail", kws)                    # short cell untouched
        for stem in ("seat", "freezes", "prompt",          # 1-word content stems
                     "seat freezes", "plan prompt"):       # 2-word adjacencies
            self.assertIn(stem, kws, stem)
        self.assertIn("stem probes auto-added", out)       # the visible receipt
        # and the entry now RESOLVES by a sub-phrase the fused original missed
        self.assertEqual([x["id"] for x in
                          store.resolve_prompt("my seat freezes every time")],
                         ["stem-law"])

    def test_short_cells_pass_byte_identical(self):
        # the guard never rewrites what it merely inspected
        self.add("prior", "short-law | a belief | 0.7 | pane freeze, seat stall")
        self.assertEqual(self.one(store.load_all(), "short-law")["keywords"],
                         "pane freeze, seat stall")

    def test_stopwords_are_dropped_from_stems(self):
        self.add("prior", "stop-law | a belief | 0.7 | "
                 "recovery of the loom, pane tail")
        kws = store._kw_list(self.one(store.load_all(), "stop-law")["keywords"])
        self.assertIn("recovery", kws)
        self.assertIn("loom", kws)
        self.assertIn("recovery loom", kws)  # the bigram BRIDGES the stopwords
        self.assertNotIn("the", kws)
        self.assertNotIn("of", kws)

    def test_the_probe_cap_bounds_decomposition(self):
        cells = ", ".join("alpha%d beta%d gamma%d delta%d" % (i, i, i, i)
                          for i in range(6))     # 6 originals, 42 potential stems
        rc, _, err = self.add("prior", "cap-law | a belief | 0.7 | " + cells)
        self.assertEqual((rc, err), (0, ""))
        kws = store._kw_list(self.one(store.load_all(), "cap-law")["keywords"])
        from helm.store import write as store_write
        self.assertEqual(len(kws), store_write._PROBE_CAP)   # bounded, exactly
        self.assertIn("alpha0 beta0 gamma0 delta0", kws)     # first original kept
        self.assertIn("alpha5 beta5 gamma5 delta5", kws)     # ...and the last
        for i in range(6):                                    # originals all kept
            self.assertIn("alpha%d beta%d gamma%d delta%d" % (i, i, i, i), kws)


class AddDuplicateProbeTest(AddGateBase):
    """GUARD 3 — keywords that ALREADY resolve to a live sibling refuse
    toward update-beats-add (a live duplicate pair was measured splitting
    retrieval weight); --force-new / HELM_STORE_FORCE_NEW override with the
    override recorded in the events journal."""

    def _seed_sibling(self):
        self.seed_prior("seat-freeze-law", "seats freeze on plan prompts",
                        keywords=DUP_SIBLING_KEYWORDS)

    def test_probe_overlap_refuses_toward_update(self):
        self._seed_sibling()
        rc, out, err = self.add("prior", "seat-freeze-two | a second law "
                                "| 0.7 | " + DUP_COLLIDING_KEYWORDS)
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")
        self.assertIn("'seat-freeze-law'", err)               # names the sibling
        self.assertIn("2 shared probes", err)
        self.assertIn("8 words", err)                         # the mass, not a tally
        # #271: the refusal NAMES the shared evidence. Reading it must settle
        # "are these the same thing?" without opening the other entry — the
        # surfacing incident cost a five-minute detour to learn the two shared
        # tokens were `remember` and `process`, which answered it instantly.
        self.assertIn("seat stall on plan prompt", err)
        self.assertIn("plan approval prompt", err)
        self.assertIn("helm store keywords seat-freeze-law --add", err)
        self.assertIn("--force-new", err)
        self.assertEqual(len(store.load_all(types=("prior",))), 1)  # not written

    def test_top_rank_on_two_cells_refuses_without_two_shared_probes(self):
        # leg (b): ONE shared probe winning TWO different cells, below leg (a)'s
        # two-probe floor. Post-#271 that probe must carry _DUP_MIN_WORDS of
        # mass — a whole shared symptom phrase, not a shared word.
        self.seed_prior("pane-law", "panes die",
                        keywords="pane freeze after the tmux reboot, "
                                 "quota headroom")
        rc, _, err = self.add(
            "prior", "pane-two | more pane trouble | 0.7 | "
            "why did pane freeze after the tmux reboot, "
            "pane freeze after the tmux reboot on a fresh seat")
        self.assertEqual(rc, 1)
        self.assertIn("'pane-law'", err)
        self.assertIn("top-ranked for 2", err)
        # names BOTH the cells that lost and the probe that won them
        self.assertIn("pane freeze after the tmux reboot", err)

    def test_one_shared_word_across_two_cells_is_not_a_duplicate(self):
        """#271, leg (b): the exact live false positive, in miniature.

        `pane` here stands in for `remember` — one bare English word that a
        reference doc on generational garbage collection and a premise on
        agent dependence both happened to carry. It top-ranked two cells and
        the guard called them the same entry. The author's only exit was to
        DELETE the colliding keyword, and deleting it measurably broke
        retrieval: of four symptom phrasings a person would really type, two
        stopped firing, including the owner's own wording. A guard that exists
        to protect retrieval was destroying it, so this is the direction that
        MUST pass."""
        self.seed_prior("pane-law", "panes die", keywords="pane")
        rc, out, err = self.add("prior", "pane-two | more pane trouble | 0.7 | "
                                "pane freeze, pane crash")
        self.assertEqual((rc, err), (0, ""))
        self.assertIn("stored:", out)
        self.assertIsNotNone(store._find("pane-two"))    # actually landed
        # ...and the keyword the author would have been forced to drop SURVIVED
        self.assertIn("pane", self.one(store.load_all(types=("prior",)),
                                       "pane-two")["keywords"])

    def test_shared_word_mass_is_what_separates_the_two(self):
        """The mechanism, asserted directly rather than through the CLI: the
        SAME two-probe overlap refuses or passes purely on word mass, so the
        discriminator is phrase mass and not the count of shared probes."""
        from helm.store import write as store_write
        self.seed_prior("thin-law", "a thin sibling", keywords="pane, tmux")
        self.seed_prior("fat-law", "a fat sibling",
                        keywords="pane froze on the plan prompt, "
                                 "tmux pane died mid reboot")
        corpus = store.load_all()
        thin = [e for e in corpus if e["id"] == "thin-law"]
        fat = [e for e in corpus if e["id"] == "fat-law"]
        self.assertEqual((len(thin), len(fat)), (1, 1))   # MUST-HIT control
        sib, why = store_write._dup_sibling(
            "new-law", ["pane", "tmux"], None, corpus)
        self.assertIsNone(sib, "two shared bare words are vocabulary, not kinship")
        sib, why = store_write._dup_sibling(
            "new-law", ["pane froze on the plan prompt",
                        "tmux pane died mid reboot"], None, corpus)
        self.assertIsNotNone(sib, "two shared symptom PHRASES are a duplicate")
        self.assertEqual(sib["id"], "fat-law")
        self.assertIn("11 words", why)
        self.assertIn("pane froze on the plan prompt", why)

    def test_dup_word_mass_counts_words_not_probes(self):
        from helm.store import write as store_write
        self.assertEqual(store_write._dup_word_mass(["alpha", "beta"]), 2)
        self.assertEqual(
            store_write._dup_word_mass(["seat stall on plan prompt"]), 5)
        self.assertEqual(store_write._dup_word_mass([]), 0)

    def test_force_new_overrides_and_records(self):
        self._seed_sibling()
        rc, out, err = self.add("prior", "seat-freeze-two | a second law "
                                "| 0.7 | " + DUP_COLLIDING_KEYWORDS,
                                "--force-new")
        self.assertEqual((rc, err), (0, ""))
        self.assertIn("DUP OVERRIDE recorded", out)
        self.assertIn("seat-freeze-law", out)             # who it collided with
        self.assertTrue(any(r.get("verb") == "store.dup_override"
                            and r.get("target") == "seat-freeze-two"
                            for r in pk.read_events(20)),
                        "the override must leave a journal receipt")

    def test_env_override_works_too(self):  # noqa: VACUOUS_ASSERTION — the empty stderr has its refusing control one test up: the SAME add without the env var refuses (test_probe_overlap_refuses_toward_update), and the landing is asserted positively via _find
        self._seed_sibling()
        with mock.patch.dict(os.environ, {"HELM_STORE_FORCE_NEW": "1"}):
            rc, _, err = self.add("prior", "seat-freeze-two | a second law "
                                  "| 0.7 | " + DUP_COLLIDING_KEYWORDS)
        self.assertEqual((rc, err), (0, ""))
        self.assertIsNotNone(store._find("seat-freeze-two"))   # actually landed

    def test_a_distinct_entry_passes(self):
        # the CONTROL: the guard accuses shared retrieval surface, not mere
        # coexistence
        self._seed_sibling()
        rc, _, err = self.add("prior", "quota-law | a different law | 0.7 | "
                              "quota headroom, cred rotation")
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(len(store.load_all(types=("prior",))), 2)


class AddRetestNudgeTest(AddGateBase):
    """GUARD 4 — a capture is done at FIRES, never at stored:. Every live add
    ends with THE one retest line — the same _RETEST constant `keywords
    --add` prints (one source of truth, two emitters)."""

    def test_add_prints_the_one_retest_constant(self):
        from helm.store import cli as store_cli
        rc, out, err = self.add("prior", "nudge-law | a belief | 0.7 | "
                                "pane freeze, seat stall")
        self.assertEqual((rc, err), (0, ""))
        self.assertIn(store_cli._RETEST, out)     # THE constant, verbatim
        self.assertIn("now RETEST", out)

    def test_keywords_add_prints_the_same_constant(self):
        from helm.store import cli as store_cli
        self.seed_prior("plaw", "S", keywords="alpha")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = store.cmd_store(["keywords", "plaw", "--add", "beta"])
        self.assertEqual(rc, 0)
        self.assertIn(store_cli._RETEST, out.getvalue())

    def test_a_candidate_add_does_not_nudge(self):  # noqa: VACUOUS_ASSERTION — the not-nudge has its positive control in-test: the live twin add asserts RETEST IS printed on the same surface
        # a candidate cannot fire until confirmed — resolve would MISS by
        # design, and nudging a retest that must fail teaches authors to
        # ignore the nudge
        rc, out, _ = self.add("prior", "cand-law | a guess | 0.6 | glorpwork",
                              "--candidate")
        self.assertEqual(rc, 0)
        self.assertNotIn("RETEST", out)
        self.assertEqual([e["id"] for e in store.candidates()], ["cand-law"])
        # positive control on the SAME surface: the LIVE twin of this add DOES
        # nudge, so the silence above is the candidate rule, not a dead print
        rc, out, _ = self.add("prior", "live-law | a truth | 0.6 | glorpother")
        self.assertEqual(rc, 0)
        self.assertIn("RETEST", out)


class StemCorpusGateTest(StoreBase):
    """task/1077 defect 1 — auto-stems must DISCRIMINATE. The measured incident:
    discipline-following phrase adds ("built it" / "now works" / "it is wired")
    minted bare `built`/`works`/`wired`/`back`/`came`, and half-arc-delivery
    fired on "lunch break, back in twenty minutes". Probe-df cannot gate this
    (the spammers sat at df 0-3 — RARITY is the damage, 1/df ~ 1); the store's
    own statement corpus is the measurement that separates them (spammers at
    statement-df 14..72 on the live store, symptom vocabulary under 11).

    Every arm here carries its must-miss twin, and the fixture-vs-live control
    (`worktree`) proves the double is in effect — a gate reading the LIVE store
    through this fixture would refuse `worktree` (live statement-df 91) and
    pass `built` only by coincidence of both corpora.

    AND EVERY MUST-MISS CARRIES A VACUITY PROBE (a review's round 2): an arm that
    asserts a query does NOT retrieve something passes for two reasons — the
    gate worked, or the query could never have matched. Each absence below is
    therefore paired with the SAME query run with the gate DISABLED (empty
    stop-list) or its discriminating condition removed, which must produce the
    hit. An absence that cannot be made to fire is not a control."""

    # four DISTINCT VERIFIED statements carry built/works/wired ->
    # corpus-common at the _STEM_COMMON_FLOOR (4); `worktree`/`freezes`/
    # `flange` appear nowhere. Distinct, because a pasted copy folds into its
    # sibling; VERIFIED (real `helm premise` captures), because the floor
    # counts only statements whose seal verifies — an unsealed row casts no
    # vote (task/2544).
    _COMMONS = (
        "seat one built the panel and it works when wired to the relay",
        "the relay panel was built last night and now works once wired",
        "what the crew built works because it is wired through the bus",
        "nothing built here works until the harness is wired and checked",
    )

    def _seed_commons(self):
        for i, stmt in enumerate(self._COMMONS):
            self._attested("noise-%d" % i, stmt, "relay-panel-%d" % i)

    def test_corpus_common_measures_this_corpus(self):  # noqa: VACUOUS_ASSERTION — the absence assert has its unconditional positive control two lines up, on the SAME set: built/works/wired must be IN `common`
        self._seed_commons()
        common = store.corpus_common(store._jit_candidates(store.load_all()))
        self.assertLessEqual({"built", "works", "wired"}, common)
        # the ISOLATION control: `worktree` is corpus-common in the LIVE store
        # (statement-df 91 of 1430 on 2026-08-11) and absent here — a gate
        # secretly reading the live corpus would include it
        self.assertNotIn("worktree", common)
        # VACUITY PROBE for that absence: same word, same call, discriminating
        # condition removed. Four statements now carry `worktree` and it enters
        # the SAME set — so the absence above is this fixture's corpus, not a
        # set that could never hold the word whatever the store said.
        for i in range(4):
            self._attested("wt-row-%d" % i, "the worktree %d was left dirty" % i,
                           "dirty-worktree-%d" % i)
        grown = store.corpus_common(store._jit_candidates(store.load_all()))
        self.assertIn("worktree", grown)

    def test_discipline_following_capture_needs_no_pruning(self):
        # THE ACCEPTANCE TEST NAMED IN task/1077: follow /learn (phrases),
        # get keywords back BYTE-IDENTICAL — nothing to prune afterwards.
        self._seed_commons()
        kw_in = "the fix is built, now works, it is wired"
        r = store.guard_entry_keywords("prior", "incident-replay", kw_in)
        self.assertIsNone(r.refusal)
        self.assertEqual(r.keywords, kw_in)
        # unconditional positive control on the SAME observable (the guard's
        # stem output): a rare-word phrase through the same door DOES stem,
        # so the byte-identical result above is the gate, not dead stemming
        r2 = store.guard_entry_keywords("prior", "control-capture",
                                        "flange stuck, freezes on flange")
        self.assertNotEqual(r2.keywords, "flange stuck, freezes on flange")
        self.assertIn("freezes", store._kw_list(r2.keywords))
        # VACUITY PROBE on THIS input: the same cells, same generic set, the
        # corpus gate DISABLED (default empty stop-list). Byte-identical above
        # is the gate refusing, not a phrase that could never have stemmed.
        # It also MEASURES how much of this acceptance case the gate carries:
        # exactly `built`, `fix built` and `wired`. `works` never reaches
        # decomposition at all — its cell "now works" is two words, under
        # _STEM_MIN_WORDS — so an arm claiming the gate refused `works` here
        # would be claiming a stem that was never on offer.
        ungated = store.stem_probes(store._kw_list(kw_in),
                                    store.GENERIC_KEYWORDS)
        self.assertEqual(ungated, ["built", "fix built", "wired"])

    def test_rare_stems_still_added_must_hit(self):
        # the must-hit that keeps the must-miss honest: the gate can SEE,
        # not merely refuse — killing stemming outright would pass the test
        # above and trade the precision bug for a recall bug
        self._seed_commons()
        kw_in = "seat stuck, freezes on worktree"
        r = store.guard_entry_keywords("prior", "genuine-capture", kw_in)
        self.assertIsNone(r.refusal)
        added = [c for c in store._kw_list(r.keywords)
                 if c not in store._kw_list(kw_in)]
        self.assertIn("freezes", added)
        self.assertIn("worktree", added)     # live-common, fixture-rare: kept

    def test_bigram_both_common_refused_mixed_kept(self):
        self._seed_commons()
        kw_in = "flange coil, flange built works"
        r = store.guard_entry_keywords("prior", "bigram-law", kw_in)
        self.assertIsNone(r.refusal)
        added = [c for c in store._kw_list(r.keywords)
                 if c not in store._kw_list(kw_in)]
        self.assertIn("flange", added)             # rare unigram survives
        self.assertIn("flange built", added)       # mixed pair survives
        self.assertNotIn("built works", added)     # both-common pair refused
        self.assertNotIn("built", added)           # common unigrams refused
        self.assertNotIn("works", added)
        # VACUITY PROBE for all three absences: the SAME cells through the SAME
        # decomposition with the stop-list emptied mint every one of them.
        ungated = store.stem_probes(store._kw_list(kw_in),
                                    store.GENERIC_KEYWORDS)
        for refusable in ("built", "works", "built works"):
            self.assertIn(refusable, ungated)
        # and the guard SAYS SO rather than dropping them silently — the
        # refusal names each word with the statement-df that refused it
        note = " ".join(r.notes)
        self.assertIn("REFUSED", note)
        self.assertIn("built (statement-df 4 of 4)", note)

    def test_stemmed_entry_does_not_fire_on_chatter(self):
        # END-TO-END must-miss: mint through the guard, then throw the
        # incident's own chatter at resolve — the entry must stay silent on
        # "lunch break, back in twenty minutes"-class text and fire on its
        # symptom. (The 8-false-positive audit fired its must-hit throughout;
        # only this direction proves discrimination.) The fixture must make
        # came/back/later corpus-common the way the live store does
        # (statement-df 14/69/... of 1430 measured 2026-08-11) — commonness
        # is store-relative, which is the whole point of the gate.
        # four DISTINCT sentences (a pasted one with a counter is the
        # near-duplicate shape corpus_profile folds to one contributor)
        for i, stmt in enumerate((
                "crew one came back later and built what works when wired",
                "the crew came back from lunch later than planned and what "
                "they built works once wired",
                "he came back later, built the bench rig, and it works now "
                "that it is wired",
                "she came back later to check that the thing built yesterday "
                "works when wired in")):
            self._attested("crew-log-%d" % i, stmt, "crew-log-%d" % i)
        r = store.guard_entry_keywords(
            "prior", "half-arc-law", "delivered half the arc, came back later")
        self.assertIsNone(r.refusal)
        self.seed_prior("half-arc-law", "finish the arc before leaving",
                        conf=1.0, keywords=r.keywords)
        silent = store.resolve_prompt("lunch break, back in twenty minutes")
        self.assertNotIn("half-arc-law", [e["id"] for e in silent])
        loud = store.resolve_prompt("I delivered half the arc tonight")
        self.assertIn("half-arc-law", [e["id"] for e in loud])
        # VACUITY PROBE — the one that decides whether this arm is a control at
        # all: mint the SAME capture with the gate DISABLED, seed it, and throw
        # the SAME chatter at resolve. The ungated twin ANSWERS "lunch break,
        # back in twenty minutes", which is the incident verbatim. So the
        # silence above is the gate refusing `came`/`back`, not a query that
        # was never going to reach a store entry.
        cells = store._kw_list("delivered half the arc, came back later")
        ungated = cells + store.stem_probes(cells, store.GENERIC_KEYWORDS)
        self.assertIn("back", ungated)
        self.seed_prior("half-arc-ungated", "the same law, minted ungated",
                        conf=1.0, keywords=",".join(ungated))
        noisy = store.resolve_prompt("lunch break, back in twenty minutes")
        self.assertIn("half-arc-ungated", [e["id"] for e in noisy])

    # -- the corpus DRIFTS: the documented behaviour, held to the code -------
    #
    # corpus_common is derived from the store's own statements, so a capture's
    # verdict is TIME-DEPENDENT. The lane's choice (stated in corpus_common's
    # docstring): the stop-list is MEASURED, never pinned, because a snapshot
    # freezes the measure at one day's corpus AND cannot see the hazard that
    # lives in the store rather than in the write — a stem admitted while its
    # word was rare becoming a spammer once siblings arrive. The drift is
    # accepted and made visible on both legs; these two arms are those legs.

    def test_the_threshold_itself_moves_with_the_corpus(self):
        # THE MOVING TERM — which every other arm in this lane ran blind to.
        #
        # `cut = max(_STEM_COMMON_FLOOR, n // 100)` has TWO branches, and every
        # seeded fixture in this class reaches only the FLOOR one: at n = 4..11,
        # n//100 is 0, so the floor decides and the divisor could be deleted
        # without reddening a single arm. MEASURED, not supposed — the 2026-08-12
        # sabotage matrix neutered the term (`n // 100000`) and all 18 lane arms
        # stayed GREEN, the one dead spot in 13 stages.
        #
        # It is not a spare branch. Censused against the live store the same day:
        # n = 1439 candidate statements, n//100 = 14 > floor 4 — so the divisor
        # decides EVERY production write today, and the fixtures above exercise
        # the branch that production never takes. This arm is the regime the
        # store is actually in.
        #
        # _floor_profile is the arithmetic half of corpus_profile — it takes
        # the word sets of the DISTINCT VERIFIED statements directly (the
        # eligibility and folding halves are pinned by the sealed-store arms
        # below) — so both regimes are measurable exactly at any n, with no
        # 1500-capture seeding.
        import helm.store.write as store_write

        def _floor(rows):
            return store_write._floor_profile(
                store_write._statement_words(e) for e in rows)

        def corpus(n, carriers):
            """`flangeword` in exactly `carriers` of `n` statements."""
            return [{"statement": "row %d carries flangeword" % i if i < carriers
                     else "row %d carries nothing of note" % i}
                    for i in range(n)]

        small = _floor(corpus(200, 14))
        big = _floor(corpus(1500, 14))
        # both branches of the max(), each actually taken
        self.assertEqual(small.cut, store_write._STEM_COMMON_FLOOR)  # 200//100=2
        self.assertEqual(big.cut, 15)                                # 1500//100
        self.assertEqual(small.df["flangeword"], 14)
        self.assertEqual(big.df["flangeword"], 14)
        # THE SAME WORD at THE SAME statement-df, opposite verdict — the
        # threshold moved under it. This is the drift the lane accepts rather
        # than pins, in the direction no seeded fixture here can reach: the
        # word gained no carriers at all, the STORE grew around it.
        self.assertIn("flangeword", small.common)
        self.assertNotIn("flangeword", big.common)          # must-miss
        # VACUITY PROBE 1 for that absence is the assertIn two lines up: same
        # word, same df, same call, only `n` differs. VACUITY PROBE 2 pins the
        # boundary so the absence is not merely "a big corpus commons nothing":
        # one more carrier clears the very same threshold at the very same n.
        self.assertIn("flangeword",
                      _floor(corpus(1500, 15)).common)
        # and the gate the AUTHOR meets moves with it: the same cells through
        # the same decomposition are refused under the small corpus and
        # ADMITTED under the big one, because 14 carriers is 7% of 200 and
        # 0.93% of 1500. Growth re-admits as well as refuses.
        cells = store._kw_list("flangeword coil jams")
        self.assertNotIn("flangeword",
                         store.stem_probes(cells, store.GENERIC_KEYWORDS,
                                           small.common))
        self.assertIn("flangeword",
                      store.stem_probes(cells, store.GENERIC_KEYWORDS,
                                        big.common))

    def test_a_corpus_under_the_floor_measures_no_stop_list_at_all(self):  # noqa: VACUOUS_ASSERTION — the `common == frozenset()` asserts carry their unconditional positive control in-test: `at_floor.common` holds the SAME word one statement further through the SAME helper
        # THE THIRD REGIME — the coverage gap a review of this chain named:
        # "cut=max(4, n//100) so n<4 gives common=EMPTY and the stem gate
        # reverts to generic-only. Measured: n=1,3 gated=False; n>=5 True.
        # 0 of 4 corpus arms use n<4."  Every corpus arm above stands at
        # n >= 4, so the branch that decides a BRAND-NEW store's first writes
        # was unpinned in both directions.
        #
        # It is not a bug to fix, it is a law to hold: statement-df is bounded
        # by n, so under the floor NO word can reach the threshold however
        # unanimously it is carried, and `common` is EMPTY by construction.
        # That is the floor doing its stated job (write.py: "keeps a
        # near-empty store from calling every word common") — on a 3-statement
        # store there is no evidence about the fleet's working language, and
        # inventing one from three rows would stop the store's first captures
        # dead on their own vocabulary. Lowering the cut to gate there is the
        # cure NOT taken, and this arm is what would redden if someone took it.
        #
        # WHAT THE ARM REJECTS (derived, never transcribed — `floor` is read
        # from the module, and the two corpora that straddle it are computed
        # from it, so moving the constant MOVES the arm):
        #   floor lowered  -> the sub-floor corpora start commoning their own
        #                     words, the `common == frozenset()` asserts go RED
        #   floor raised   -> the at-floor positive control loses `flangeword`,
        #                     that assertIn goes RED
        # so the boundary is pinned from both sides rather than sampled on one.
        import helm.store.write as store_write
        floor = store_write._STEM_COMMON_FLOOR

        def _floor(rows):
            return store_write._floor_profile(
                store_write._statement_words(e) for e in rows)

        def unanimous(n):
            """`flangeword` in EVERY one of n statements: df == n, the most a
            corpus that size can possibly say about a word."""
            return [{"statement": "row %d carries flangeword" % i}
                    for i in range(n)]

        # -- leg 1: under the floor, the measurement is empty -----------------
        self.assertGreater(floor, 1)          # the regime has to exist at all
        # Everything below is written in terms of `floor`, which is what makes
        # it a LAW rather than a sample — but a derivation that follows its
        # input everywhere also hides a change to that input. The floor is the
        # ONE term in this gate that is not supposed to move (the divisor is
        # the moving half, pinned by the arm above), and the reported cases
        # are n = 1 and 3 only while it is 4. So it is pinned by VALUE, once,
        # here: move the floor and this line is where you say so, out loud.
        self.assertEqual(floor, 4)
        for n in range(1, floor):             # n = 1..3 at floor 4, the reported cases
            prof = _floor(unanimous(n))
            self.assertEqual(prof.n, n)
            self.assertEqual(prof.cut, floor)        # the max() takes the floor
            self.assertEqual(prof.df["flangeword"], n)   # carried by ALL of them
            # ...and still short of the threshold: the emptiness below is the
            # arithmetic n < cut, not a corpus that failed to be measured.
            self.assertLess(prof.df["flangeword"], prof.cut)
            self.assertEqual(prof.common, frozenset())   # noqa: VACUOUS_ASSERTION — the unconditional positive control on the SAME observable is `at_floor.common` seven lines down: the SAME word, the SAME unanimity, the SAME call, one more statement, and it IS common. An empty set here is a threshold verdict, not a dead measurement — df is asserted == n first

        # -- the unconditional positive control, one statement further -------
        at_floor = _floor(unanimous(floor))
        self.assertEqual(at_floor.cut, floor)
        self.assertIn("flangeword", at_floor.common)     # must-hit

        # -- leg 2: what the AUTHOR meets — generic-ONLY, not gate-open ------
        # The fallback has to be a NARROWER gate, not an absent one. `gen` is
        # taken from the live curated set (moving the set moves the arm), and
        # the same cell goes through the same door under both corpora.
        gen = sorted(w for w in store.GENERIC_KEYWORDS
                     if w.isalpha() and w not in store_write._STEM_STOPWORDS)[0]
        self.assertIn(gen, store.GENERIC_KEYWORDS)
        cells = store._kw_list("flangeword %s coil" % gen)
        tiny = store.stem_probes(cells, store.GENERIC_KEYWORDS,
                                 _floor(unanimous(floor - 1)).common)
        full = store.stem_probes(cells, store.GENERIC_KEYWORDS, at_floor.common)
        # the degradation is REAL and this is it: the identical unigram the
        # at-floor corpus refuses is minted under the sub-floor one.
        self.assertIn("flangeword", tiny)
        self.assertNotIn("flangeword", full)
        # ...and it is bounded: the curated leg still bites with an EMPTY
        # measured stop-list, so `generic-only` is exactly what it says. This
        # is the must-miss — an implementation that skipped stemming's generic
        # filter whenever `common` was empty would pass every assert above.
        self.assertNotIn(gen, tiny)
        self.assertNotIn(gen, full)
        # and the cell's non-generic neighbour proves `tiny` is a live list
        # rather than a short-circuit that returned nothing.
        self.assertIn("coil", tiny)

    def _grow_flange_corpus(self):
        """Six VERIFIED statements carry `flange` -> corpus-common (6 of the
        10 verified: four commons plus these; the unsealed drifter casts no
        vote)."""
        for i in range(6):
            self._attested("flangelog-%d" % i,
                           "the flange log %d records a flange event" % i,
                           "flangelog-%d" % i)

    def test_the_same_capture_is_refused_later_and_shows_its_arithmetic(self):
        self._seed_commons()
        # VACUITY PROBE for the refusal below, taken FIRST on the SAME phrase
        # through the SAME door: on the corpus of the day, `flange` is ADMITTED
        early = store.guard_entry_keywords("prior", "drifter",
                                           "flange coil jams")
        self.assertIsNone(early.refusal)
        self.assertIn("flange", store._kw_list(early.keywords))
        self.seed_prior("drifter", "a rule about the coil", conf=1.0,
                        keywords=early.keywords)
        self._grow_flange_corpus()
        # force=True skips the duplicate-sibling gate only: this is the SAME
        # capture arriving a month later, and the corpus has moved under it
        late = store.guard_entry_keywords("prior", "later-capture",
                                          "flange coil jams", force=True)
        self.assertIsNone(late.refusal)
        self.assertNotIn("flange", store._kw_list(late.keywords))  # REFUSED
        # and the author is TOLD, with numbers he can reproduce — a bare
        # "refused" is what makes a moving verdict unarguable
        note = " ".join(late.notes)
        self.assertIn("REFUSED", note)
        self.assertIn("flange (statement-df 6 of 10)", note)
        self.assertIn("threshold >=4", note)
        self.assertIn("nothing is pinned", note)

    def test_doctor_re_checks_stored_stems_against_todays_corpus(self):
        # THE HAZARD NO SNAPSHOT CAN SEE, and the reason this lane accepts the
        # drift instead of pinning: `drifter` was minted correctly and is
        # rotting in place. Nothing retro-edits it (its bytes are its author's)
        # — the re-check is what surfaces it, with a runnable cure.
        self._seed_commons()
        early = store.guard_entry_keywords("prior", "drifter",
                                           "flange coil jams")
        self.seed_prior("drifter", "a rule about the coil", conf=1.0,
                        keywords=early.keywords)
        # VACUITY PROBE, taken FIRST: on the corpus that admitted it, the same
        # instrument reports nothing about this row. The report below is the
        # corpus moving, not a class that names every row it sees.
        quiet, _ = store.doctor()
        self.assertEqual([str(e["id"]) for e in quiet["stem_drift"]], [])
        self._grow_flange_corpus()
        report, actions = store.doctor()
        drift = {str(e["id"]): e["stem_drift"] for e in report["stem_drift"]}
        self.assertIn("drifter", drift)
        self.assertIn("flange", drift["drifter"])
        self.assertEqual(actions, [])            # report only, never rewritten
        self.assertIn("flange", store._find("drifter")["keywords"])
        # the CURE THE REPORT PRINTS must be a command that works — a probe I
        # prescribe owes the same scrutiny as the claim (store premise)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = store.cmd_store(["doctor"])
        self.assertEqual(rc, 0)
        self.assertIn('helm store keywords "drifter" --type prior --remove "flange"',
                      out.getvalue())
        e, err = store.retag("drifter", TS, remove="flange")
        self.assertIsNone(err, err)
        self.assertNotIn("flange", store._kw_list(e["keywords"]))
        healed, _ = store.doctor()
        self.assertEqual([str(x["id"]) for x in healed["stem_drift"]], [])


class StatementShapedIdTest(StoreBase):
    """task/1077 defect 2 — 43 live rows whose id IS a whole statement were
    dark to `get`/`keywords` under the token everyone actually types (the
    leading kebab word), so no CLI path could re-key them. The cure is an
    ADDRESS, not a rename: identity (supersedes chains, evidence citations,
    attestation payload hashes) keeps its bytes."""

    STMT_ID = ("verify-branch-base-before-merge CANON (near-miss 2026-07-02): "
               "BEFORE merging")

    def test_leading_token_resolves_the_dark_row(self):
        self.seed_prior(self.STMT_ID, "verify the branch base first",
                        conf=1.0, keywords="branch base check")
        e = store._find("verify-branch-base-before-merge")
        self.assertIsNotNone(e)
        self.assertEqual(e["statement"], "verify the branch base first")
        # identity untouched: the stored id keeps every byte
        self.assertEqual(e["id"], self.STMT_ID)

    def test_rekey_through_the_prefix(self):
        # the defect's own acceptance test: the row can be RE-KEYED again
        self.seed_prior(self.STMT_ID, "verify the branch base first",
                        conf=1.0, keywords="branch base check")
        e, err = store.retag("verify-branch-base-before-merge", TS,
                             add="merging the branch")
        self.assertIsNone(err)
        with open(e["path"], encoding="utf-8") as f:
            self.assertIn("merging the branch", f.read())

    def test_ambiguous_prefix_refuses_must_miss(self):
        # two dark rows share the token -> _find must NOT guess (its callers
        # include write verbs); the CLI lists the near-hits instead
        self.seed_prior("empty-200 CLOSED, MECHANISM PINNED", "closure",
                        conf=1.0, keywords="closed empty response")
        # VACUITY PROBE FIRST: with the discriminating condition (a SECOND
        # row sharing the token) not yet present, the SAME query resolves.
        # The None below is the ambiguity refusal, not a token that could
        # never have reached the prefix fallback.
        self.assertEqual(store._find("empty-200")["statement"], "closure")
        self.seed_prior("empty-200 CONSOLIDATED, SUPERSEDING BOTH", "merge",
                        conf=1.0, keywords="consolidated empty response")
        self.assertIsNone(store._find("empty-200"))
        self.assertIsNotNone(store._find("empty-200-closed"))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = store.cmd_store(["get", "empty-200"])
        self.assertEqual(rc, 1)
        self.assertIn("ambiguous", out.getvalue())
        self.assertIn("CONSOLIDATED", out.getvalue())

    def test_exact_id_beats_prefix_shadow_must_miss(self):
        # a healthy kebab id is NEVER hijacked by a statement-shaped sibling.
        #
        # THIS ARM WAS VACUOUS UNTIL a review's round 2 and the probe that
        # answered it: the id under test was `gate`, four characters, and
        # _statement_id_matches refuses any typed token under _PREFIX_MIN (8).
        # Measured with only the sibling seeded, _find("gate") was None — the
        # sibling could NEVER have won that query, so the shadowing law was
        # never on trial and the arm could not fail. The id is now past the
        # floor, and the vacuity probe is the first half of the test.
        self.seed_prior("gate-keeps-quorum CANON (statement shaped)",
                        "the sibling", conf=1.0, keywords="shaped sibling row")
        # VACUITY PROBE: the sibling DOES win this exact query when the
        # discriminating condition — a healthy exact row — is absent
        self.assertEqual(store._find("gate-keeps-quorum")["statement"],
                         "the sibling")
        self.seed_prior("gate-keeps-quorum", "the healthy exact id",
                        conf=1.0, keywords="healthy quorum probe")
        self.assertEqual(store._find("gate-keeps-quorum")["statement"],
                         "the healthy exact id")

    def test_short_prefix_stays_not_found_must_miss(self):
        import helm.store.load as store_load
        self.seed_prior(self.STMT_ID, "verify the branch base first",
                        conf=1.0, keywords="branch base check")
        self.assertIsNone(store._find("verify"))   # < _PREFIX_MIN chars
        # positive control on the SAME observable: past the length floor the
        # very same row resolves, so the None above is the floor, not a miss
        self.assertIsNotNone(store._find("verify-branch-base"))
        # VACUITY PROBE on the SAME query: with the floor itself removed,
        # `verify` DOES reach the row. The None above is _PREFIX_MIN doing its
        # job — not a token whose slug never matched this id's prefix.
        with mock.patch.object(store_load, "_PREFIX_MIN", 1):
            self.assertEqual(store._find("verify")["statement"],
                             "verify the branch base first")


class StoreDoctorTest(StoreBase):
    """task/1077 defects 3+4+5 — the mechanical retrieval-field repairs.
    Measured live 2026-08-11: 68 space-separated keyword CSVs (one fused probe
    that can never match), 3 delimiter-cascade rows (keywords CSV stranded in
    `domain`, statement fragment in `keywords` — never-stash, orca-pane-close,
    work-offer-fastfollow), 42 live zero-keyword rows (report-only: keying is
    judgment; their MINT door is already closed by the add-time lint).

    Every must-miss here carries a VACUITY PROBE (a review's round 2): the same
    observable is shown FIRING once its discriminating condition is removed —
    the healthy row re-seeded sick appears in the report and its bytes move,
    the chatter query retrieves a row keyed for it, the prose field minus its
    punctuation IS split. An absence nothing can make fail is not a control."""

    def _seed_ward(self):
        self.seed_prior("space-csv-victim", "keyed by a space separated list",
                        conf=1.0, keywords="audit parity migration runtime")
        self.seed_prior("cascade-victim", "statement got cut here", conf=1.0,
                        keywords="tail of the statement + fragment.,git stash,"
                                 "stash my changes",
                        domain="stash, swarm, worktree, git, jj")
        self.seed_prior("prose-flag-victim", "a fused sentence in keywords",
                        conf=1.0,
                        keywords="grep -q pattern && echo HOLDER; done.")
        self.seed_prior("zero-key-victim", "a live row nothing can reach",
                        conf=1.0)
        self.seed_prior("healthy-control", "the row the doctor must not touch",
                        conf=1.0, keywords="healthy probe,control row",
                        domain="git")

    def test_classify_finds_each_class_and_only_them(self):  # noqa: VACUOUS_ASSERTION — the healthy-row absence loop and empty-actions assert have four unconditional positive controls above them on the SAME report object (each seeded victim asserted IN its class)
        self._seed_ward()
        report, actions = store.doctor()
        ids = {k: [str(e["id"]) for e in v] for k, v in report.items()}
        self.assertIn("space-csv-victim", ids["space_csv"])
        self.assertIn("cascade-victim", ids["cascade"])
        self.assertIn("prose-flag-victim", ids["flagged"])
        self.assertIn("zero-key-victim", ids["zero_keys"])
        # must-miss: the healthy row appears in NO class, and a read-only
        # doctor takes NO actions
        for klass, rows in ids.items():
            self.assertNotIn("healthy-control", rows, klass)
        self.assertEqual(actions, [])
        # VACUITY PROBE for both absences, on the SAME id and the SAME call.
        # Re-seed healthy-control with ONE sick field and it appears; ask the
        # same doctor to fix and it acts. Neither absence is structural.
        self.seed_prior("healthy-control", "the row the doctor must not touch",
                        conf=1.0, keywords="audit parity migration runtime")
        sick, acted = store.doctor(fix=True, ts=TS)
        self.assertIn("healthy-control",
                      [str(e["id"]) for e in sick["space_csv"]])
        self.assertNotEqual(acted, [])

    def test_fix_revives_the_salad_row(self):
        self._seed_ward()
        # must-miss FIRST: the class is real — the fused probe cannot fire
        before = store.resolve_prompt("the migration parity broke again")
        self.assertNotIn("space-csv-victim", [e["id"] for e in before])
        report, actions = store.doctor(fix=True, ts=TS)
        self.assertIn(("space-csv-victim", "space_csv"),
                      [(a[0], a[1]) for a in actions])
        after = store.resolve_prompt("the migration parity broke again")
        self.assertIn("space-csv-victim", [e["id"] for e in after])
        # must-miss twin: revived, not wallpapered — unrelated chatter silent
        silent = store.resolve_prompt("lunch break, back in twenty minutes")
        self.assertNotIn("space-csv-victim", [e["id"] for e in silent])
        # VACUITY PROBE for that silence: the chatter query is a query that
        # CAN retrieve — a row keyed on its words answers it. So the silence
        # is the repaired row's keys, not a prompt resolve ignores.
        self.seed_prior("chatter-bait", "the row that wants the chatter",
                        conf=1.0, keywords="lunch break,twenty minutes")
        heard = store.resolve_prompt("lunch break, back in twenty minutes")
        self.assertIn("chatter-bait", [e["id"] for e in heard])

    def test_fix_brings_cascade_keywords_home_with_receipt(self):
        self._seed_ward()
        # VACUITY PROBE for the two absences below, on the SAME fields before
        # the repair: the prose fragment IS in keywords and domain IS full.
        # Without this the arm would pass against a row that never had them.
        pre = store._find("cascade-victim")
        self.assertIn("fragment.", pre["keywords"])
        self.assertNotEqual(pre["domain"], "")
        store.doctor(fix=True, ts=TS)
        e = store._find("cascade-victim")
        for kept in ("swarm", "worktree", "git stash", "stash my changes"):
            self.assertIn(kept, e["keywords"])
        self.assertEqual(e["domain"], "")
        self.assertNotIn("fragment.", e["keywords"])
        # the dropped prose is receipted IN THE ARTIFACT (evidence_log),
        # never only in the lossy events journal — enumerate the file itself
        with open(e["path"], encoding="utf-8") as f:
            raw = f.read()
        self.assertIn("evidence_log", raw)
        self.assertIn("fragment", raw)

    def test_fix_never_touches_a_healthy_row_must_miss(self):  # noqa: VACUOUS_ASSERTION — the byte-equality has its unconditional positive control in-test: the sick sibling's bytes are asserted CHANGED by the same doctor run
        self._seed_ward()
        path = store._find("healthy-control")["path"]
        sick = store._find("space-csv-victim")["path"]
        with open(path, encoding="utf-8") as f:
            before = f.read()
        with open(sick, encoding="utf-8") as f:
            sick_before = f.read()
        store.doctor(fix=True, ts=TS)
        with open(path, encoding="utf-8") as f:
            self.assertEqual(f.read(), before)
        # positive control on the SAME observable (on-disk bytes after the
        # SAME doctor run): the sick sibling DID change, so the equality
        # above is selectivity, not a doctor that wrote nothing
        with open(sick, encoding="utf-8") as f:
            self.assertNotEqual(f.read(), sick_before)
        # VACUITY PROBE on the SAME FILE: give that row one sick field and the
        # same doctor rewrites those very bytes. Its stillness above is the
        # classifier passing over a healthy row, not an unwritable path.
        # The baseline is re-read AFTER the re-seed on purpose — the re-seed
        # writes the file itself, so comparing against `before` would credit
        # the doctor with the fixture's own write.
        self.seed_prior("healthy-control", "the row the doctor must not touch",
                        conf=1.0, keywords="audit parity migration runtime")
        with open(path, encoding="utf-8") as f:
            reseeded = f.read()
        store.doctor(fix=True, ts=TS)
        with open(path, encoding="utf-8") as f:
            self.assertNotEqual(f.read(), reseeded)

    def test_prose_field_is_flagged_never_split(self):
        # splitting a sentence would mint exactly the common-word spam the
        # stem gate exists to refuse — prose stays a human call
        self._seed_ward()
        store.doctor(fix=True, ts=TS)
        e = store._find("prose-flag-victim")
        self.assertEqual(e["keywords"],
                         "grep -q pattern && echo HOLDER; done.")
        # VACUITY PROBE: the discriminating condition is the PROSE, nothing
        # else. The same words, same length, same comma-less shape, with the
        # shell punctuation removed, ARE comma-ized by the same doctor — so
        # the untouched field above is the prose guard, not a splitter that
        # never runs on this fixture.
        self.seed_prior("prose-flag-victim", "a fused sentence in keywords",
                        conf=1.0, keywords="grep q pattern echo HOLDER done")
        store.doctor(fix=True, ts=TS)
        self.assertIn(",", store._find("prose-flag-victim")["keywords"])

    def test_zero_keyword_mint_door_is_closed(self):
        # defect 5's fail-open half: the WRITER API still permits a keyless
        # entry (lifecycle rewrites of legacy rows depend on it — retiring a
        # salad must never be vetoed by the salad), but every MINT door
        # refuses. Assert the refusal names the cure.
        r = store.guard_entry_keywords("prior", "keyless-mint", "")
        self.assertIsNotNone(r.refusal)
        self.assertIn("NO keywords", r.refusal)
        # and the doctor reports the legacy rows so they get keyed on purpose
        self.seed_prior("legacy-keyless", "born before the guard", conf=1.0)
        report, _ = store.doctor()
        self.assertIn("legacy-keyless",
                      [str(e["id"]) for e in report["zero_keys"]])

    # -- task/1077 revival (F1 + F2): the printed cure names the typed row
    # the doctor measured; a repair never drops authored keys ---------------

    def _seed_shared_id_drift_pair(self):
        """A prior and a reference share the id `flange-rule`; ONLY the
        reference drifted. The reference's `flange` is a GENERATED stem of
        its long cell (the shipped mint guard wrote it, admitted on the
        corpus of the day); the prior's `flange` is AUTHORED, a short cell
        with no long phrase, so stem_drift_cells can never name it. Six
        flange statements then arrive and the stem becomes chatter."""
        for i in range(4):
            self.seed_prior(
                "noise-%d" % i,
                "seat %d built the panel and works when wired to the relay" % i,
                conf=1.0, keywords="relay-panel-%d" % i)
        early = store.guard_entry_keywords("reference", "flange-rule",
                                           "flange coil jams")
        self.assertIsNone(early.refusal)
        self.assertIn("flange", store._kw_list(early.keywords))
        store.write_reference({"id": "flange-rule", "statement": "the coil doc",
                               "url": "https://x.test",
                               "keywords": early.keywords,
                               "stated_ts": TS, "load_class": "jit"})
        self.seed_prior("flange-rule", "a rule about the coil", conf=1.0,
                        keywords="flange,stable probe")
        for i in range(6):
            self._attested("flangelog-%d" % i,
                           "the flange log %d records a flange event" % i,
                           "flangelog-%d" % i)

    def test_printed_drift_cure_names_the_typed_row_it_measured(self):
        """F1. The doctor classifies each TYPED row; a bare `keywords ID`
        resolves the untyped winner (prior > reference). Two rows share one
        id and only the reference drifted, so the printed cure must carry
        `--type reference` and, run as printed through the shipped cmd_store,
        strip the stale cell from the REFERENCE while the prior's authored
        `flange` stays byte-identical on disk.

        CONTROL, run FIRST on the same fixture: the printed argv with its
        `--type reference` stripped is the untyped form. Through the shipped bare
        path it removes the PRIOR's authored `flange` and leaves the
        reference stale — the defect the typed form exists to close. The
        prior is then re-seeded by the same producer before the typed run."""
        self._seed_shared_id_drift_pair()
        report, actions = store.doctor()
        drift = [(e["type"], e["stem_drift"]) for e in report["stem_drift"]
                 if str(e["id"]) == "flange-rule"]
        self.assertEqual(drift, [("reference", ["flange"])])
        self.assertEqual(actions, [])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = store.cmd_store(["doctor"])
        self.assertEqual(rc, 0)
        lines = [ln.strip() for ln in out.getvalue().splitlines()
                 if 'keywords "flange-rule"' in ln]
        self.assertEqual(lines, ['helm store keywords "flange-rule" '
                                 '--type reference --remove "flange"'])
        argv = shlex.split(lines[0])[2:]
        # CONTROL: the same command with the type stripped, via the shipped
        # bare path — the prior wins the resolve and loses ITS authored cell
        bare = [t for t in argv if t not in ("--type", "reference")]
        self.assertEqual(bare, ["keywords", "flange-rule", "--remove", "flange"])
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(store.cmd_store(bare), 0)
        self.assertNotIn("flange", store._kw_list(
            store._find("flange-rule", types=("prior",))["keywords"]))
        self.assertIn("flange", store._kw_list(
            store._find("flange-rule", types=("reference",))["keywords"]))
        # restore the prior through the same producer, then bind its bytes
        self.seed_prior("flange-rule", "a rule about the coil", conf=1.0,
                        keywords="flange,stable probe")
        ppath = store._find("flange-rule", types=("prior",))["path"]
        with open(ppath, encoding="utf-8") as f:
            before = f.read()
        self.assertIn("flange,stable probe", before)
        # THE CURE AS PRINTED, through the shipped verb
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(store.cmd_store(argv), 0)
        ref = store._kw_list(
            store._find("flange-rule", types=("reference",))["keywords"])
        self.assertNotIn("flange", ref)
        self.assertIn("flange coil", ref)        # only the drifted cell moved
        with open(ppath, encoding="utf-8") as f:
            self.assertEqual(f.read(), before)   # the prior: untouched bytes
        healed, _ = store.doctor()
        self.assertEqual([str(x["id"]) for x in healed["stem_drift"]], [])

    def test_printed_zero_key_cure_carries_type_and_project(self):
        """F1, the zero-key advice: the same omission. A project-scoped
        keyless prior is reported under the project lens, and the printed
        command names both the type and the project — run with a real CSV in
        the placeholder's slot it keys THAT row (a bare `keywords ID` under
        no lens would answer not-found about a project row)."""
        self.seed_prior("keyless-px", "a project row nothing reaches",
                        conf=1.0, root_dir=self.project_dir("px", "premises"))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = store.cmd_store(["doctor", "--project", "px"])
        self.assertEqual(rc, 0)
        lines = [ln.strip() for ln in out.getvalue().splitlines()
                 if 'keywords "keyless-px"' in ln]
        self.assertEqual(lines, ['helm store keywords "keyless-px" --type prior '
                                 '--add <symptom,csv> --project px'])
        argv = [("coil,jams" if t == "<symptom,csv>" else t)
                for t in shlex.split(lines[0])[2:]]
        # CONTROL: without the project lens the row is not found — the lens
        # the cure carries is what reaches it
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(store.cmd_store(argv[:-2]), 1)
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(store.cmd_store(argv), 0)
        e = store._find("keyless-px", project="px", types=("prior",))
        self.assertEqual(store._kw_list(e["keywords"]), ["coil", "jams"])

    def test_printed_cure_quotes_a_project_name_with_a_space(self):
        """F1 residual (task/1077 r4): the registry admits a project name with
        an internal space, and the printed cure concatenated it bare, so
        `--project px blue` re-parsed as `--project px` plus an ignored
        positional and the cure reached a same-id sibling in project px.
        The printed line is shell-quoted: split through shlex it carries the
        whole name as ONE token, and running it keys the row in the spaced
        project while the same-id sibling in px stays byte-identical.
        CONTROL: the bare rendering of the same line splits the name into
        two tokens, the shape the pre-cure printer produced."""
        import glob
        sib_root = self.project_dir("px", "premises")
        self.seed_prior("keyless-px", "a sibling row in px",
                        conf=1.0, root_dir=sib_root)
        self.seed_prior("keyless-px", "a project row with a spaced name",
                        conf=1.0,
                        root_dir=self.project_dir("px blue", "premises"))
        sib_files = sorted(glob.glob(os.path.join(sib_root, "**", "*keyless-px*"),
                                     recursive=True))
        self.assertTrue(sib_files, "sibling row file not found (must-hit)")
        before = [open(f, "rb").read() for f in sib_files]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = store.cmd_store(["doctor", "--project", "px blue"])
        self.assertEqual(rc, 0)
        lines = [ln.strip() for ln in out.getvalue().splitlines()
                 if 'keywords "keyless-px"' in ln]
        self.assertEqual(lines, ['helm store keywords "keyless-px" --type prior '
                                 "--add <symptom,csv> --project 'px blue'"])
        toks = shlex.split(lines[0])
        self.assertEqual(toks[-1], "px blue")     # one token, not two
        argv = [("coil,jams" if t == "<symptom,csv>" else t) for t in toks[2:]]
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(store.cmd_store(argv), 0)
        e = store._find("keyless-px", project="px blue", types=("prior",))
        self.assertEqual(store._kw_list(e["keywords"]), ["coil", "jams"])
        self.assertEqual([open(f, "rb").read() for f in sib_files], before)
        # CONTROL: the bare rendering splits the name; its last token is a
        # stray positional and its project is `px`, the sibling's
        bare = shlex.split(lines[0].replace("'px blue'", "px blue"))
        self.assertEqual(bare[-2:], ["px", "blue"])

    def test_fix_keeps_every_authored_token_of_a_long_space_csv(self):  # noqa: VACUOUS_ASSERTION — the receipt's empty `domain` half is the fixture's own before-state; its `keywords` half is the unconditional positive control on the same receipt object, asserted equal to the raw 25-token field
        """F2. _PROBE_CAP (24) is the mint gate's budget for GENERATED stems;
        the first cut sliced the AUTHORED tokens of a space-separated CSV at
        it, so a legal 25-token field lost its 25th with only a count in the
        receipt. The fixture is asserted ABOVE the cap so the arm cannot pass
        over a field the old slice would have left alone; the repair keeps
        all 25 in the author's order, the reason's count equals the stored
        count, and the receipt carries the exact before-bytes in the
        artifact. The four-token _seed_ward arms are the short controls."""
        from helm.store.write import _PROBE_CAP
        toks = ["needle%02d" % i for i in range(1, 26)]
        self.assertGreater(len(toks), _PROBE_CAP)
        for t in toks:
            self.assertNotIn(t, store.GENERIC_KEYWORDS)
        raw = " ".join(toks)
        self.seed_prior("long-space-csv", "twenty five authored probes",
                        conf=1.0, keywords=raw)
        pre = store._find("long-space-csv")
        self.assertEqual(pre["keywords"], raw)
        report, actions = store.doctor(fix=True, ts=TS)
        mine = [a for a in actions if a[0] == "long-space-csv"]
        self.assertEqual([a[1] for a in mine], ["space_csv"])
        self.assertIn("-> 25 cells", mine[0][2])
        e = store._find("long-space-csv")
        self.assertEqual(store._kw_list(e["keywords"]), toks)
        rcpt = [r for r in e["evidence_log"] if r.get("type") == "doctor"]
        self.assertEqual(rcpt[-1]["was"], {"keywords": raw, "domain": ""})
        with open(e["path"], encoding="utf-8") as f:
            self.assertIn(raw, f.read())

    def test_fix_keeps_the_cascade_survivor_beside_a_full_domain(self):  # noqa: VACUOUS_ASSERTION — the cleared `domain` has its unconditional positive control on the same field before the repair: pre["domain"] is asserted to hold the 24 cells, and the receipt's `was` is asserted to carry them
        """F2, the cascade leg: 24 domain cells (exactly _PROBE_CAP) plus one
        surviving authored keyword beside the prose fragment. The old slice
        dropped the survivor — the 25th merged cell — with no record. Now
        every cell comes home, the survivor included, and the receipt holds
        both fields' before-bytes as the loader read them. The
        five-cell _seed_ward cascade arm is the short control."""
        from helm.store.write import _PROBE_CAP
        dom = ["dom%02d" % i for i in range(1, 25)]
        self.assertEqual(len(dom), _PROBE_CAP)
        raw_kw = "tail of the statement + fragment.,safety needle"
        self.seed_prior("wide-cascade", "the statement got cut here",
                        conf=1.0, keywords=raw_kw, domain=", ".join(dom))
        pre = store._find("wide-cascade")
        self.assertIn("fragment.", pre["keywords"])
        self.assertEqual(store._kw_list(pre["domain"]), dom)
        report, actions = store.doctor(fix=True, ts=TS)
        self.assertIn(("wide-cascade", "cascade"),
                      [(a[0], a[1]) for a in actions])
        e = store._find("wide-cascade")
        self.assertEqual(store._kw_list(e["keywords"]), dom + ["safety needle"])
        self.assertEqual(e["domain"], "")
        rcpt = [r for r in e["evidence_log"] if r.get("type") == "doctor"]
        self.assertEqual(rcpt[-1]["was"],
                         {"keywords": pre["keywords"], "domain": pre["domain"]})
        with open(e["path"], encoding="utf-8") as f:
            raw = f.read()
        self.assertIn("fragment.", raw)          # the dropped prose, recoverable
        self.assertIn("dom24", raw)

    # -- task/2544: the three findings of row e43ec4309e89 that outlived the
    # >24-token cure. Each arm drives the SHIPPED doctor over a real fixture
    # under the hermetic HELM_HOME and names the one-line change to the cure
    # that turns it red. -------------------------------------------------

    def _bytes(self, path):
        with open(path, "rb") as f:
            return f.read()

    def _edit(self, path, pattern, repl):
        """One in-place frontmatter edit, asserted to have matched — the
        way a legacy row or a hand edit reaches the store, past the writer."""
        with open(path, encoding="utf-8") as f:
            text = f.read()
        new, n = re.subn(pattern, repl, text, flags=re.M)
        self.assertGreaterEqual(n, 1, pattern)
        with open(path, "w", encoding="utf-8") as f:
            f.write(new)

    def _premises_dir_strays(self):
        d = self.global_dir("premises")
        return sorted(n for n in os.listdir(d)
                      if not n.endswith(".md") or os.path.isdir(os.path.join(d, n)))

    def _premise_check_rc(self, pid):
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            return premise.cmd_premise_check([pid])

    def test_fix_lands_whole_or_not_at_all(self):  # noqa: VACUOUS_ASSERTION — the empty strays list has its unconditional positive control in the refusal that precedes it: the ValueError text is _commit's own gloss refusal, which fires only while serializing a STAGED body, so a stage was written and the empty list is cleanup, not absence; the shrunken-gloss run then lands both repairs through the same stages and the list is empty again because they were renamed
        """Finding (a), partial commit. Reproduced on trunk cb37970489f1: a
        valid cascade repair followed by a space_csv row whose EXISTING gloss
        overruns LINE_CAP raised out of the loop with the cascade row already
        rewritten and the CLI silent (it prints actions after the return).
        Now every repair is staged and validated first; one refusal removes
        every stage and raises naming the row, and both files are the bytes
        they were. MUTATION: write each row at `row["path"]` inside the
        staging loop (the old in-loop write) — the cascade bytes move under
        the refusal and the byte-equality below goes red; drop the stage
        cleanup in the except — the strays assert goes red."""
        from helm.inject._common import LINE_CAP
        casc = self.seed_prior(
            "cascade-victim", "statement got cut here", conf=1.0,
            keywords="tail of the statement + fragment.,git stash,stash my changes",
            domain="stash, swarm, worktree, git, jj")
        salad = self.seed_prior("space-csv-victim", "keyed by a space separated list",
                                conf=1.0, keywords="audit parity migration runtime")
        # the oversized gloss reaches the artifact PAST the writer, as a legacy
        # row would: the writer itself refuses it (that refusal is the arm)
        self._edit(salad, r"^  keywords: .*$",
                   "  keywords: audit parity migration runtime\n  gloss: "
                   + "g" * (LINE_CAP + 40))
        before = (self._bytes(casc), self._bytes(salad))
        with self.assertRaises(ValueError) as cm:
            store.doctor(fix=True, ts=TS)
        msg = str(cm.exception)
        self.assertIn("wrote NOTHING", msg)
        self.assertIn("'space-csv-victim' [space_csv]", msg)
        self.assertIn("gloss too long", msg)
        self.assertIn("1 other repair withheld", msg)
        self.assertEqual((self._bytes(casc), self._bytes(salad)), before)  # noqa: VACUOUS_ASSERTION — the unconditional positive control on the SAME files is the shrunken-gloss run below: the same doctor call then CHANGES both, so equality here is the whole-batch refusal, not a doctor that never writes
        self.assertEqual(self._premises_dir_strays(), [])
        # the CLI says so on stderr with rc 1, and still writes nothing
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = store.cmd_store(["doctor", "--fix"])
        self.assertEqual(rc, 1)
        self.assertIn("helm store doctor: doctor --fix wrote NOTHING", err.getvalue())
        self.assertEqual((self._bytes(casc), self._bytes(salad)), before)
        # CONTROL (the vacuity probe for every equality above): the ONLY
        # discriminating condition is the overrun gloss. Shrink it and the
        # SAME call lands BOTH repairs — so the stillness above was the batch
        # refusing, not a doctor with nothing to write.
        self._edit(salad, r"^  gloss: .*$", "  gloss: fits")
        report, actions = store.doctor(fix=True, ts=TS)
        self.assertEqual(sorted((a[0], a[1]) for a in actions),
                         [("cascade-victim", "cascade"), ("space-csv-victim", "space_csv")])
        self.assertNotEqual(self._bytes(casc), before[0])
        self.assertEqual(store._kw_list(store._find("space-csv-victim")["keywords"]),
                         ["audit", "parity", "migration", "runtime"])
        self.assertEqual(self._premises_dir_strays(), [])       # stages gone

    def test_fix_rewrites_an_attested_row_only_when_its_seal_verifies(self):
        """Finding (b), unverified attestations rewritten. Reproduced on trunk
        cb37970489f1 with real captures: a sealed row whose statement had
        been edited under the seal (premise-check: digest MISMATCH) and one
        whose attest_record the chain never minted (native chain BROKEN) were
        both comma-ized by --fix — a fresh last_updated and a doctor receipt
        laid over a broken seal. Now the doctor holds premise-check's own
        exit-0 contract before it rewrites: the verified row is repaired and
        its seal still verifies afterwards; the two unverified rows are left
        byte-identical, stay in their defect class, and are named with the
        reason in both read-only and --fix reports. CONTROLS: an UNATTESTED
        salad row in the same run is repaired (the doctor ran and repairs),
        and restoring the mismatched statement makes the same call repair
        that row (the refusal was the seal, not the row). MUTATION: drop the
        payload_digest comparison in _attest_verdict — `law-mismatch` is
        rewritten, red; drop the verify_record call — `law-forged` is
        rewritten, red."""
        ok = self._attested("law-ok", "The OK truth about relays", "okkw,ok-relay")
        mis = self._attested("law-mismatch", "The M truth about coils", "miskw,mis-coil")
        forged = self._attested("law-forged", "The F truth about buses", "forgekw,forge-bus")
        plain = self.seed_prior("plain-salad", "an unattested salad row", conf=1.0,
                                keywords="audit parity migration runtime")
        for path in (ok, mis, forged):
            self._edit(path, r"^  keywords: .*$",
                       "  keywords: audit parity migration runtime")
        self._edit(mis, r"^  statement: .*$", "  statement: The M truth about coils EDITED")
        self._edit(mis, r"^PREMISE: .*$", "PREMISE: The M truth about coils EDITED")
        self._edit(forged, r"^  attest_record: .*$", "  attest_record: " + "ab" * 32)
        # the seal's own instrument agrees about the three BEFORE the doctor
        self.assertEqual(self._premise_check_rc("law-ok"), 0)
        self.assertEqual(self._premise_check_rc("law-mismatch"), 1)
        self.assertEqual(self._premise_check_rc("law-forged"), 1)
        before = {p: self._bytes(p) for p in (mis, forged)}
        # read-only: the two are NAMED, with premise-check's reason, and
        # nothing moves
        quiet, quiet_actions = store.doctor()
        self.assertEqual(quiet_actions, [])
        named = {str(e["id"]): e["attest_why"] for e in quiet["attest_unverified"]}
        self.assertEqual(sorted(named), ["law-forged", "law-mismatch"])
        self.assertIn("digest MISMATCH", named["law-mismatch"])
        self.assertIn("native chain BROKEN", named["law-forged"])
        self.assertLessEqual({"law-ok", "law-mismatch", "law-forged", "plain-salad"},
                             {str(e["id"]) for e in quiet["space_csv"]})  # still in class
        self.assertEqual({p: self._bytes(p) for p in (mis, forged)}, before)
        # --fix: the verified seal and the unattested control are repaired;
        # the unverified two are byte-identical
        report, actions = store.doctor(fix=True, ts=TS)
        self.assertEqual(sorted(a[0] for a in actions), ["law-ok", "plain-salad"])
        self.assertEqual({p: self._bytes(p) for p in (mis, forged)}, before)  # noqa: VACUOUS_ASSERTION — the unconditional positive control on the SAME observable is `law-ok` in the same run: its bytes were rewritten (keywords asserted comma-ized below) by the same call that left these two
        self.assertEqual(sorted(str(e["id"]) for e in report["attest_unverified"]),
                         ["law-forged", "law-mismatch"])
        self.assertEqual(store._kw_list(store._find("law-ok")["keywords"]),
                         ["audit", "parity", "migration", "runtime"])
        self.assertIn("  attest_record: ", self._bytes(ok).decode("utf-8"))
        self.assertEqual(self._premise_check_rc("law-ok"), 0)   # seal survives the rewrite
        # the CLI names them with the instrument to run
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(store.cmd_store(["doctor"]), 0)
        self.assertIn("ATTESTED ROWS LEFT UNTOUCHED (2)", out.getvalue())
        self.assertIn('helm premise-check "law-mismatch"', out.getvalue())
        # VACUITY for the mismatch row: put the sealed statement back and the
        # SAME call repairs it — the refusal was the broken seal
        self._edit(mis, r"^  statement: .*$", "  statement: The M truth about coils")
        self._edit(mis, r"^PREMISE: .*$", "PREMISE: The M truth about coils")
        self.assertEqual(self._premise_check_rc("law-mismatch"), 0)
        report, actions = store.doctor(fix=True, ts=TS)
        self.assertEqual([a[0] for a in actions], ["law-mismatch"])
        self.assertEqual([str(e["id"]) for e in report["attest_unverified"]], ["law-forged"])
        self.assertEqual(self._bytes(forged), before[forged])

    def test_near_duplicate_statements_count_once_in_the_stem_corpus(self):
        """Finding (c), planted contributors. Reproduced on trunk
        cb37970489f1: four rows pasted from ONE sentence with a counter
        reached the floor (cut 4) and refused the rare stem `flange` of a
        genuine capture (statement-df 4 of 4); three did not. corpus_profile
        now folds a near-duplicate cluster (token-set Jaccard >= DUP_OVERLAP,
        the overlap `add` already warns at) into ONE contributor, so the
        four copies admit `flange` exactly as three did, the doctor names the
        copies with the supersede cure, and the refusal note says how many
        were folded. NEGATIVE (the pinned floor law, untouched): four
        DISTINCT statements carrying `flange` still refuse it at 4 of 4.
        Every carrier here is a VERIFIED capture: the floor counts only
        those, so the fold under test is the fold among verified rows.
        MUTATION: delete `rep[i] = rep[j]` in _dup_folds (no fold) — the
        admitted assert goes red; compare with `>` instead of `>=` — these
        rows sit at exactly 0.8 and go red, which pins the fold to the add
        door's inclusive law; shorten the prefix by one — a missed pair, red."""
        import helm.store.write as store_write
        capture = ("prior", "real-capture", "flange coupling seizes, coil jams")

        def stems():
            r = store.guard_entry_keywords(*capture)
            self.assertIsNone(r.refusal)
            return store._kw_list(r.keywords), " ".join(r.notes)
        # -- NEGATIVE / control first: four DISTINCT carriers common the word
        distinct = [self._attested("distinct-%d" % i, stmt, "distinct-probe-%d" % i)
                    for i, stmt in enumerate((
                        "the flange on the pump cracked at dawn",
                        "a stuck flange stalled the whole rig",
                        "we torque every flange to spec now",
                        "the spare flange sits in the north bin"))]
        cells, note = stems()
        self.assertNotIn("flange", cells)                    # REFUSED
        self.assertIn("flange (statement-df 4 of 4)", note)
        self.assertNotIn("near-duplicate", note)             # nothing folded
        for p in distinct:
            os.remove(p)
        # -- POSITIVE: four copies of one sentence are ONE contributor
        for i in range(4):
            self._attested("planted-%d" % i,
                           "planted row %d: the flange coupling seizes under load" % i,
                           "planted-probe-%d" % i)
        prof = store.corpus_profile(store._jit_candidates(store.load_all()))
        self.assertEqual(prof.n, 1)
        self.assertEqual(len(prof.folded), 3)
        self.assertEqual(prof.df["flange"], 1)
        self.assertNotIn("flange", prof.common)  # noqa: VACUOUS_ASSERTION — the unconditional positive control on the SAME word through the SAME measure is the distinct-carrier leg above (flange refused at 4 of 4) and the mixed leg below (5 of 5)
        cells, note = stems()
        self.assertIn("flange", cells)                       # ADMITTED
        # the boundary is the add door's own inclusive overlap, exactly
        toks = [store._tokens("planted row %d: the flange coupling seizes under load" % i)
                for i in range(2)]
        self.assertEqual(len(toks[0] & toks[1]) / len(toks[0] | toks[1]),
                         store.DUP_OVERLAP)
        # the doctor names the copies, with the cure `add` prescribes
        report, actions = store.doctor()
        self.assertEqual(actions, [])
        dups = {str(e["id"]): e["dup_of"] for e in report["dup_statements"]}
        self.assertEqual(dups, {"planted-%d" % i: ("prior", "planted-0")
                                for i in (1, 2, 3)})
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(store.cmd_store(["doctor"]), 0)
        self.assertIn("NEAR-DUPLICATE STATEMENTS (3)", out.getvalue())
        # the printed operands are followed through the real door in the
        # typed-operands arm (round 2); no text-only control here
        # -- MIXED: distinct carriers plus the folded cluster: refused at
        # 5 of 5 (four distinct + ONE cluster), and the note says what folded
        for i, stmt in enumerate((
                "the flange on the pump cracked at dawn",
                "a stuck flange stalled the whole rig",
                "we torque every flange to spec now",
                "the spare flange sits in the north bin")):
            self._attested("distinct-%d" % i, stmt, "distinct-probe-%d" % i)
        cells, note = stems()
        self.assertNotIn("flange", cells)
        self.assertIn("flange (statement-df 5 of 5)", note)
        self.assertIn("3 near-duplicate statements counted once", note)
        self.assertEqual(store_write._STEM_COMMON_FLOOR, 4)   # the law this leans on

    # -- task/2544 round 2: the five findings of the cross-family FIX on the
    # round-1 cure. --------------------------------------------------------

    def _reference(self, rid, statement, keywords):
        return store.write_reference({"id": rid, "statement": statement,
                                      "keywords": keywords, "stated_ts": TS})

    def test_the_duplicate_cure_names_typed_operands_the_supersede_door_resolves(self):
        """F1. The report kept (type, id) but the printed supersede dropped
        the type; the door resolves a bare slug to the untyped WINNER and a
        prior outranks a reference, so following the suggestion about a
        measured REFERENCE pair tombstoned the unrelated PRIOR sharing the
        slug. Now the suggestion carries typed_id operands and the write
        door (_resolve_write -> find_typed) honours them. The arm follows
        the ACTUAL printed operands through the real door on a three-row
        fixture. CONTROL: the same bare-slug command, run by hand, is what
        the door resolves to the prior — so the typing is load-bearing.
        A lexicon copy gets a 'no supersede door' line, not a command.
        MUTATION: print e['id'] instead of typed_id(e) — the prior's bytes
        move, red; resolve with _find instead of find_typed in
        _resolve_write — same, red."""
        ra = self._reference("dup-a", "planted row 1: the flange coupling seizes under load", "refkw-a")
        rb = self._reference("dup-b", "planted row 2: the flange coupling seizes under load", "refkw-b")
        pa = self.seed_prior("dup-a", "an unrelated prior sharing the slug", conf=1.0,
                             keywords="priorkw-a")
        report, _ = store.doctor()
        self.assertEqual([(e["type"], str(e["id"]), e["dup_of"])
                          for e in report["dup_statements"]],
                         [("reference", "dup-b", ("reference", "dup-a"))])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(store.cmd_store(["doctor"]), 0)
        lines = [l.strip() for l in out.getvalue().splitlines()
                 if "helm store supersede" in l]
        self.assertEqual(len(lines), 1, out.getvalue())
        argv = [a for a in shlex.split(lines[0])[2:] if a != "<reason...>"]
        self.assertEqual(argv[2:], ["reference:dup-b", "reference:dup-a"])
        before_pa = self._bytes(pa)
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            rc = store.cmd_store(argv + ["doctor-measured copy"])
        self.assertEqual(rc, 0, err.getvalue())
        self.assertEqual(self._bytes(pa), before_pa)   # noqa: VACUOUS_ASSERTION — the unconditional positive control on the SAME file is the bare-slug run below: the very same verb with the type dropped DOES rewrite the prior
        rb_raw = self._bytes(rb).decode("utf-8")
        self.assertIn("  replaced_by: dup-a", rb_raw)
        self.assertIn("delete_eligible", rb_raw)
        self.assertNotIn("delete_eligible", self._bytes(ra).decode("utf-8"))
        # CONTROL: the bare spelling resolves the PRIOR — measured, so the
        # typing above is not cosmetic
        rb2 = self._reference("dup-c", "planted row 3: the flange coupling seizes under load", "refkw-c")
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            rc = store.cmd_store(["supersede", TS, "dup-a", "dup-c", "bare slug"])
        self.assertEqual(rc, 0)
        self.assertNotEqual(self._bytes(pa), before_pa)          # the prior moved
        self.assertIn("delete_eligible", self._bytes(pa).decode("utf-8"))
        self.assertNotIn("delete_eligible", self._bytes(ra).decode("utf-8"))
        del rb2
        # a lexicon copy: the report says there is no door, prints no command
        store.write_lexicon({"id": "seam-x", "term": "seam-x", "definition":
                             "planted row 4: the flange coupling seizes under load",
                             "keywords": "lexkw-x", "stated_ts": TS, "term_scope": "global"})
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(store.cmd_store(["doctor"]), 0)
        text = out.getvalue()
        self.assertIn("no supersede door for a lexicon", text)
        self.assertNotIn("supersede %s lexicon:" % TS[:4], text)

    def test_a_malformed_native_record_is_named_not_escaped(self):  # noqa: VACUOUS_ASSERTION — the empty actions list and the untouched bytes have their unconditional positive control in the same arm: with the null line removed the SAME call repairs the SAME row (actions == ["law-ok"]), so the emptiness is the contained verifier refusing, not a doctor that never writes
        """F2. chain_records admits any JSON value, so a `null` line before
        the sought record made verify_record raise AttributeError out of
        _attest_verdict and abort even a READ-ONLY doctor (the CLI catches
        ValueError only). Now the verifier call is contained: the row reads
        unverified naming the exception, the inventory returns, --fix
        leaves the bytes. CONTROL: the null line removed, the same row
        verifies and is repaired by the same call. MUTATION: drop the
        try/except around verify_record — the read-only doctor raises
        AttributeError, red."""
        ok = self._attested("law-ok", "The OK truth about relays", "okkw,ok-relay")
        self._edit(ok, r"^  keywords: .*$", "  keywords: audit parity migration runtime")
        chain = os.path.join(home.global_dir(), ".state", "attest-chain.jsonl")
        good = self._bytes(chain)
        with open(chain, "wb") as f:
            f.write(b"null\n" + good)
        before = self._bytes(ok)
        report, actions = store.doctor()                      # read-only returns
        named = {str(e["id"]): e["attest_why"] for e in report["attest_unverified"]}
        self.assertIn("law-ok", named)
        self.assertIn("native chain UNREADABLE", named["law-ok"])
        self.assertIn("AttributeError", named["law-ok"])
        report, actions = store.doctor(fix=True, ts=TS)
        self.assertEqual(actions, [])
        self.assertEqual(self._bytes(ok), before)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(store.cmd_store(["doctor"]), 0)  # the CLI survives too
        self.assertIn("ATTESTED ROWS LEFT UNTOUCHED (1)", out.getvalue())
        # CONTROL: null removed -> verified -> repaired
        with open(chain, "wb") as f:
            f.write(good)
        report, actions = store.doctor(fix=True, ts=TS)
        self.assertEqual([a[0] for a in actions], ["law-ok"])
        self.assertEqual(report["attest_unverified"], [])

    def test_an_unreadable_seal_reread_is_unverified_never_absent(self):  # noqa: VACUOUS_ASSERTION — the empty actions list under the failing seam has its unconditional positive control in the same arm: with the seam restored the SAME call repairs the unattested sibling (actions == ["plain-salad"]) while the broken seal stays held for its digest
        """F3. The verification reread was lenient: a failed read answered
        None -> {} -> 'unattested', so a broken-seal prior the loader had
        already parsed was rewritable exactly when the verifier's own read
        failed. Now _attest_meta reads strictly and any failure is
        unverified with the OS reason. The arm fails ONLY that seam (the
        stage writer's reads keep working). CONTROL: the seam restored, the
        same broken seal is still unverified (digest MISMATCH) — the failure
        did not invent the refusal. MUTATION: `or {}` on a lenient read
        instead of the strict seam — the row is rewritten, red."""
        import helm.store.write as store_write
        mis = self._attested("law-mismatch", "The M truth about coils", "miskw,mis-coil")
        self._edit(mis, r"^  keywords: .*$", "  keywords: audit parity migration runtime")
        self._edit(mis, r"^  statement: .*$", "  statement: The M truth about coils EDITED")
        self._edit(mis, r"^PREMISE: .*$", "PREMISE: The M truth about coils EDITED")
        plain = self.seed_prior("plain-salad", "an unattested salad row", conf=1.0,
                                keywords="audit parity migration runtime")
        before = self._bytes(mis)
        with mock.patch.object(store_write, "_attest_meta",
                               side_effect=OSError(5, "EIO")) as seam:
            report, actions = store.doctor(fix=True, ts=TS)
        self.assertGreaterEqual(seam.call_count, 1)
        self.assertEqual(actions, [])            # nothing rewritten while blind
        self.assertEqual(self._bytes(mis), before)  # noqa: VACUOUS_ASSERTION — the unconditional positive control on the SAME row is the seam-restored run below, where the same doctor names the SAME row for its digest rather than for the read; and the plain row IS repaired once the seam works
        named = {str(e["id"]): e["attest_why"] for e in report["attest_unverified"]}
        self.assertIn("artifact unreadable for verification", named["law-mismatch"])
        self.assertIn("EIO", named["law-mismatch"])
        # blind means blind: the doctor will not guess that the unattested
        # row is unattested either
        self.assertIn("plain-salad", named)
        # CONTROL: seam restored — the broken seal holds on its own merits,
        # the unattested row is repaired
        report, actions = store.doctor(fix=True, ts=TS)
        self.assertEqual([a[0] for a in actions], ["plain-salad"])
        self.assertEqual(self._bytes(mis), before)
        named = {str(e["id"]): e["attest_why"] for e in report["attest_unverified"]}
        self.assertEqual(sorted(named), ["law-mismatch"])
        self.assertIn("digest MISMATCH", named["law-mismatch"])
        del plain

    _DISTINCT_FLANGE = ("the flange on the pump cracked at dawn",
                        "a stuck flange stalled the whole rig",
                        "we torque every flange to spec now",
                        "the spare flange sits in the north bin")

    def _flange_stem(self):
        """Whether the generated `flange` stem of a genuine capture is
        admitted right now, plus the notes. Short, distinct single-cell
        keywords on every carrier keep the duplicate-sibling guard out of
        the measurement."""
        r = store.guard_entry_keywords("prior", "real-capture",
                                       "flange coupling seizes, coil jams")
        self.assertIsNone(r.refusal)
        return "flange" in store._kw_list(r.keywords), " ".join(r.notes)

    def test_the_floor_counts_only_distinct_verified_statements(self):  # noqa: VACUOUS_ASSERTION — every empty observable (common == frozenset(), flange df 0, folded == []) has its unconditional positive control in leg 4 of the same arm: the SAME word through the SAME door is refused at 4 of 8 once four VERIFIED carriers exist, so the emptiness is eligibility, not a dead measurement
        """THE CONTRACT (third round on the planted-contributor concern, so
        a contract, not another case): the stem-common floor's votes come
        ONLY from the statement text of rows whose seal VERIFIES, per row;
        near-duplicates fold AMONG VERIFIED ROWS only; an unsealed row
        contributes zero tokens and cannot inherit a sealed sibling's
        eligibility by resembling it; a store with no verifying carrier has
        an UNMEASURED floor and demotes nothing; four verified statements
        sharing a stem make it common by design (verified rows are the
        store's authority — the accepted narrower reading). Legs: (1) an
        unsealed store — four distinct carriers admit `flange`,
        floor_measured False, attested 0, the guard's note and the doctor's
        printed line say so; (2) the counterexample — four verified distinct
        statements each shadowed by an unsealed copy appending `flange`:
        `flange` stays discriminating with ZERO votes; (3) removing the
        copies changes nothing; (4) three verified flange carriers admit,
        four make it common — the positive control and the accepted case.
        MUTATION: fold over all rows before eligibility (the union
        inherits) — leg 2 refuses, red; count unsealed rows as
        contributors — leg 1 refuses and floor_measured reads True, red;
        return the `common` of an empty word-set list from the df instead of
        frozenset() — nothing changes, which is why leg 1 asserts attested
        == 0 and the printed line too."""
        import helm.store.write as store_write
        self.assertEqual(store_write._STEM_COMMON_FLOOR, 4)

        def prof():
            return store.corpus_profile(store._jit_candidates(store.load_all()))
        # -- leg 1: an unsealed store cannot measure its floor ---------------
        paths = [self.seed_prior("d%d" % i, s, conf=1.0, keywords="dkw%d" % i)
                 for i, s in enumerate(self._DISTINCT_FLANGE)]
        admitted, note = self._flange_stem()
        self.assertTrue(admitted)                       # noqa: VACUOUS_ASSERTION — the unconditional positive control on the SAME word through the SAME door is leg 4 below, where four VERIFIED carriers refuse it
        self.assertIn("stem floor unmeasured: 0 attested carriers", note)
        p = prof()
        self.assertEqual((p.floor_measured, p.attested, p.unsealed, p.n), (False, 0, 4, 0))
        self.assertEqual(p.common, frozenset())
        self.assertEqual(sum(1 for e in store.load_all() if e.get("attest_record")), 0)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(store.cmd_store(["doctor"]), 0)
        self.assertIn("stem floor unmeasured: 0 attested carriers", out.getvalue())
        for path in paths:
            os.remove(path)
        # -- leg 2: the counterexample — unsealed copies inherit nothing -----
        plain = ("the pump housing cracked at dawn today",
                 "a stuck bearing stalled the whole rig",
                 "we torque every bolt to spec now",
                 "the spare gasket sits in the north bin")
        for i, s in enumerate(plain):
            self._attested("v%d" % i, s, "vkw%d" % i)
        copies = [self.seed_prior("c%d" % i, s + " flange", conf=1.0, keywords="ckw%d" % i)
                  for i, s in enumerate(plain)]
        # each copy resembles its verified sibling past the fold threshold
        t0, t1 = store._tokens(plain[0]), store._tokens(plain[0] + " flange")
        self.assertGreaterEqual(len(t0 & t1) / len(t0 | t1), store.DUP_OVERLAP)
        admitted, note = self._flange_stem()
        self.assertTrue(admitted)
        p = prof()
        self.assertEqual((p.floor_measured, p.attested, p.unsealed, p.n), (True, 4, 4, 4))
        self.assertEqual(p.df.get("flange", 0), 0)      # zero votes, not four
        self.assertEqual(p.folded, [])                  # nothing folded among the verified
        self.assertEqual(len(p.dups), 4)                # the copies ARE reported as duplicates
        # -- leg 3: removing the copies changes nothing ----------------------
        for path in copies:
            os.remove(path)
        admitted, _ = self._flange_stem()
        self.assertTrue(admitted)
        p = prof()
        self.assertEqual((p.attested, p.unsealed, p.n, p.df.get("flange", 0)), (4, 0, 4, 0))
        # -- leg 4: verified carriers ARE the authority — the accepted case --
        for i, s in enumerate(self._DISTINCT_FLANGE[:3]):
            self._attested("a%d" % i, s, "akw%d" % i)
        self.assertTrue(self._flange_stem()[0])         # 3 of 7 verified: admitted
        self._attested("a3", self._DISTINCT_FLANGE[3], "akw3")
        admitted, note = self._flange_stem()
        self.assertFalse(admitted)                      # 4 of 8 verified: common
        self.assertIn("flange (statement-df 4 of 8)", note)
        self.assertIn("measured NOW against 8 distinct verified statements", note)
        self.assertNotIn("unsealed", note)              # nothing unsealed here to mention
        # and an unsealed planted quartet beside them still casts no vote
        for i in range(4):
            self.seed_prior("p%d" % i,
                            "planted row %d: the flange coupling seizes under load" % i,
                            conf=1.0, keywords="pkw%d" % i)
        admitted, note = self._flange_stem()
        self.assertFalse(admitted)
        self.assertIn("flange (statement-df 4 of 8)", note)
        self.assertIn("4 unsealed statements cast no vote", note)

    def test_the_rename_leg_names_what_landed_and_the_cleanup_names_its_residue(self):  # noqa: VACUOUS_ASSERTION — the empty strays list and the absent RESIDUE clause have their unconditional positive control in the residue leg of the same arm: the SAME list is asserted to hold the stage dir and the SAME message to carry RESIDUE when a stage cannot be unlinked
        """F5. The public text promised WHOLE OR NOT AT ALL while the code
        only preflighted writers: a failure on the SECOND final os.replace
        left the first row committed and said nothing, and a stage the
        cleanup could not unlink was swallowed. Now the guarantee is
        qualified (help + VERBS.md) and the code reports: a rename failure
        after the first names LANDED and NOT landed rows; a cleanup that
        cannot remove a stage names the residue path. The mocks select
        exactly the final rename (source in the stage dir, destination
        outside it) and exactly the stage .md, so the writers' own renames
        and temp cleanup are untouched. CONTROL: the healthy two-row run
        lands both and leaves no stage. MUTATION: swallow the OSError in
        the rename loop — no diagnostic, the NOT-landed assert goes red;
        `except OSError: pass` in _doctor_unstage — the residue assert
        goes red."""
        import helm.store.write as store_write
        from helm.inject._common import LINE_CAP
        c1 = self.seed_prior("row-1", "first row", conf=1.0,
                             keywords="audit parity migration runtime")
        c2 = self.seed_prior("row-2", "second row", conf=1.0,
                             keywords="alpha beta gamma delta")
        real_replace = os.replace
        finals = []

        def flaky(src, dst):
            if ".doctor-stage." in src and ".doctor-stage." not in dst:
                finals.append(src)
                if len(finals) == 2:
                    raise OSError(28, "ENOSPC")
            return real_replace(src, dst)
        b1, b2 = self._bytes(c1), self._bytes(c2)
        with mock.patch.object(store_write.os, "replace", side_effect=flaky):
            with self.assertRaises(ValueError) as cm:
                store.doctor(fix=True, ts=TS)
        msg = str(cm.exception)
        self.assertEqual(len(finals), 2)                 # the mock saw the final leg
        self.assertIn("landed 1 of 2 repairs", msg)
        self.assertIn("LANDED (validated, receipted): row-1", msg)
        self.assertIn("NOT landed (originals untouched): row-2", msg)
        self.assertNotIn("RESIDUE", msg)
        self.assertNotEqual(self._bytes(c1), b1)         # the prefix landed
        self.assertEqual(self._bytes(c2), b2)
        self.assertEqual(self._premises_dir_strays(), [])
        # the landed row got its receipt
        e = store._find("row-1")
        self.assertEqual(store._kw_list(e["keywords"]), ["audit", "parity", "migration", "runtime"])
        self.assertTrue([r for r in e["evidence_log"] if r.get("type") == "doctor"])
        # CONTROL: the healthy run lands the remaining row and clears stages
        report, actions = store.doctor(fix=True, ts=TS)
        self.assertEqual([a[0] for a in actions], ["row-2"])
        self.assertEqual(self._premises_dir_strays(), [])
        # -- residue: a later gloss refusal plus an earlier stage that will
        # not unlink names the path it left behind
        c3 = self.seed_prior("row-3", "third row", conf=1.0,
                             keywords="epsilon zeta eta theta")
        c4 = self.seed_prior("row-4", "fourth row", conf=1.0,
                             keywords="iota kappa lambda mu")
        self._edit(c4, r"^  keywords: .*$",
                   "  keywords: iota kappa lambda mu\n  gloss: " + "g" * (LINE_CAP + 40))
        real_remove = os.remove

        def sticky(p):
            if ".doctor-stage." in p and p.endswith(".md"):
                raise OSError(1, "EPERM")
            return real_remove(p)
        b3 = self._bytes(c3)
        with mock.patch.object(store_write.os, "remove", side_effect=sticky):
            with self.assertRaises(ValueError) as cm:
                store.doctor(fix=True, ts=TS)
        msg = str(cm.exception)
        self.assertIn("wrote NOTHING", msg)
        self.assertIn("'row-4' [space_csv]", msg)
        self.assertIn("RESIDUE left on disk", msg)
        self.assertIn(os.path.join(".doctor-stage.%d" % os.getpid(), "prior-row-3.md"), msg)
        self.assertIn("EPERM", msg)
        self.assertEqual(self._bytes(c3), b3)
        self.assertEqual(self._premises_dir_strays(), [".doctor-stage.%d" % os.getpid()])
        # and once the stage CAN be removed, the same refusal leaves nothing
        with self.assertRaises(ValueError) as cm:
            store.doctor(fix=True, ts=TS)
        self.assertNotIn("RESIDUE", str(cm.exception))
        self.assertEqual(self._premises_dir_strays(), [])


    def test_the_supersede_fence_compares_resolved_identity(self):  # noqa: VACUOUS_ASSERTION — the byte-equality inside the alias loop has its unconditional positive control after the loop on the SAME verb and door: the long typed pair is asserted TOMBSTONED (delete_eligible, replaced_by, supersedes), so the refusals are the fence, not a dead verb
        """A self-supersede fence that compares the SLUGGED RAW operands
        before resolution lets `a` versus `prior:a` (and `prior:a` versus
        `premise:a`) through — both resolve to one artifact, the old copy
        writes a tombstone and the separately loaded new copy writes live
        status back with a self-link — and the slug's 60-character cap on a
        TYPED address makes two distinct 60-character reference ids sharing
        a prefix read as one. The fence compares the RESOLVED identity
        (type, slug) of both loaded records, before either write. Arms
        through the real CLI verb: both alias pairs refuse with bytes
        unchanged; two distinct long typed ids supersede; the typed-operands
        arm and its unrelated-prior fixture remain the positive controls.
        MUTATION: restore the raw-slug compare — the alias pairs write a
        live self-link (bytes change), red; compare paths only — the same
        artifact still refuses, but a moved file would not, the identity
        assert stays the honest one."""
        pa = self.seed_prior("alias-a", "one artifact", conf=1.0, keywords="aliaskw")
        before = self._bytes(pa)
        for old, new in (("alias-a", "prior:alias-a"), ("prior:alias-a", "premise:alias-a"),
                         ("prior:alias-a", "alias-a")):
            err = io.StringIO()
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                rc = store.cmd_store(["supersede", TS, old, new, "alias"])
            self.assertEqual(rc, 1, (old, new))
            self.assertIn("an entry cannot supersede itself", err.getvalue())
            self.assertEqual(self._bytes(pa), before, (old, new))  # noqa: VACUOUS_ASSERTION — the unconditional positive control on the SAME door is the long-id pair below, which the same verb DOES tombstone
        raw = self._bytes(pa).decode("utf-8")
        self.assertNotIn("supersedes:", raw)
        self.assertNotIn("delete_eligible", raw)
        # two distinct legal 60-character ids sharing their first 50
        i1 = "abcdefghij" * 5 + "0000000001"
        i2 = "abcdefghij" * 5 + "0000000002"
        self.assertEqual((len(i1), len(i2)), (60, 60))
        r1 = self._reference(i1, "the first long reference", "longkw1")
        r2 = self._reference(i2, "the second long reference", "longkw2")
        # the trap the raw-slug fence fell into: the typed addresses slug alike
        self.assertEqual(store._slug("reference:" + i1), store._slug("reference:" + i2))
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            rc = store.cmd_store(["supersede", TS, "reference:" + i1, "reference:" + i2, "long"])
        self.assertEqual(rc, 0, err.getvalue())
        self.assertIn("delete_eligible", self._bytes(r1).decode("utf-8"))
        self.assertIn("  replaced_by: " + i2, self._bytes(r1).decode("utf-8"))
        self.assertIn("  supersedes: " + i1, self._bytes(r2).decode("utf-8"))
        self.assertNotIn("delete_eligible", self._bytes(r2).decode("utf-8"))


# ---------------------------------------------------------------------------
# task/2978 — store and inject precision
# ---------------------------------------------------------------------------

def _plant_census(fracs, turns=None):
    """A prompt census in this test's HELM_HOME: `fracs` maps a word to the
    fraction of turns it reached. `turns` defaults to the measured minimum,
    so the census counts as measured unless a test asks otherwise."""
    from helm import promptcensus
    turns = promptcensus.MIN_TURNS if turns is None else turns
    df = {w: int(round(f * turns)) for w, f in fracs.items()}
    path = promptcensus.path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"v": 1, "turns": turns, "df": df}, f)
    return path


class StemShapeTest(StoreBase):
    """task/2978: a generated unigram stem is never numeric and never two
    characters or fewer. MEASURED on the live store (E2 audit): 31 entries
    carried a numeric probe and 120 a probe of <= 2 characters; the stem `1`
    fired silent-death-must-never-render-as-in-progress on 18 turns."""

    def test_no_numeric_or_short_unigram_stem(self):
        got = store.stem_probes(["retry 1 ok pane", "v2 fluxcap 30m drift"])
        self.assertIn("fluxcap", got)                  # unconditional control
        for weak in ("1", "ok", "v2"):
            with self.subTest(stem=weak):
                self.assertNotIn(weak, got)
        for kept in ("retry", "pane", "fluxcap", "30m", "drift"):   # must-hit
            with self.subTest(stem=kept):
                self.assertIn(kept, got)

    def test_a_pair_of_two_weak_halves_is_not_minted(self):
        got = store.stem_probes(["retry 1 ok pane"])
        self.assertNotIn("1 ok", got)
        self.assertIn("ok pane", got)                 # one strong half keeps it

    def test_an_authored_short_cell_is_the_authors(self):
        kw, err, _notes, _ev = store.guard_entry_keywords(
            "prior", "short-authored", "ok, retry 1 pane")
        self.assertIsNone(err)
        cells = store._kw_list(kw)
        self.assertEqual(cells[:2], ["ok", "retry 1 pane"])   # authored, kept
        self.assertNotIn("1", cells)                          # never generated
        self.assertIn("retry", cells)


class PromptCommonStemTest(StoreBase):
    """task/2978: the mint gate measured commonness on STATEMENTS while the
    probes fire on PROMPTS. `summary` sat in 1 verified statement and in 16.5%
    of turns; `ok` in 1 and 16.7%. The prompt census is the second leg."""

    def test_a_prompt_common_word_is_not_stemmed_and_the_arithmetic_is_shown(self):
        _plant_census({"summary": 0.6, "drift": 0.01})
        kw, err, notes, _ = store.guard_entry_keywords(
            "prior", "census-law", "cache summary drift")
        self.assertIsNone(err)
        cells = store._kw_list(kw)
        self.assertNotIn("summary", cells)
        self.assertIn("drift", cells)                  # a rare word still stems
        self.assertIn("cache", cells)
        note = " ".join(notes)
        self.assertIn("summary (prompt 60.0% of", note)
        self.assertIn("REFUSED", note)

    def test_an_unmeasured_census_refuses_nothing_and_says_so(self):
        from helm import promptcensus
        _plant_census({"summary": 0.6}, turns=promptcensus.MIN_TURNS - 1)
        kw, err, notes, _ = store.guard_entry_keywords(
            "prior", "census-law", "cache summary drift")
        self.assertIsNone(err)
        self.assertIn("summary", store._kw_list(kw))
        self.assertIn("prompt census unmeasured", " ".join(notes))


def _minted(author, stems):
    """A keyword field in the exact shape guard_entry_keywords writes: the
    author's string, then the stems appended after ', '."""
    return author + ", " + ", ".join(stems)


class DoctorStemUnfitTest(StoreBase):
    """task/2978: `helm store doctor` names GENERATED stems today's mint gate
    would not mint (numeric, <= 2 characters, or prompt-common), and --fix
    removes exactly those. Generated is read the way write.py records it: the
    guard appends its stems AFTER the author's string, in decomposition order,
    so the trailing run of cells the entry's own long phrases decompose into
    is generated, and the first cell that is not ends it. Authored cells are
    never edited."""

    AUTHOR = "summary, retry 1 fluxcap"
    STEMS = ["retry", "1", "fluxcap", "retry 1", "1 fluxcap"]

    def seed(self, pid="unfit-row", author=AUTHOR, stems=STEMS):
        return self.seed_prior(pid, "a rule about the fluxcap", conf=1.0,
                               keywords=_minted(author, stems))

    def test_the_report_names_generated_unfit_stems_only(self):  # noqa: VACUOUS_ASSERTION — the report rows are asserted EQUAL to a non-empty mapping first; the empty actions and unchanged bytes ARE the read-only claim
        _plant_census({"retry": 0.5, "summary": 0.5, "fluxcap": 0.001})
        path = self.seed()
        with open(path, "rb") as f:
            before = f.read()
        report, actions = store.doctor()
        rows = {str(e["id"]): e["stem_unfit"] for e in report["stem_unfit"]}
        self.assertEqual(rows, {"unfit-row": ["retry", "1", "retry 1"]})
        self.assertEqual(actions, [])
        with open(path, "rb") as f:
            self.assertEqual(f.read(), before)       # a report writes nothing

    def test_fix_removes_them_and_keeps_every_authored_cell(self):
        _plant_census({"retry": 0.5, "summary": 0.5, "fluxcap": 0.001})
        self.seed()
        report, actions = store.doctor(fix=True, ts=TS)
        self.assertIn(("unfit-row", "stem_unfit"), [(a[0], a[1]) for a in actions])
        e = self.one(store.load_all(), "unfit-row")
        self.assertEqual(store._kw_list(e["keywords"]),
                         ["summary", "retry 1 fluxcap", "fluxcap", "1 fluxcap"])
        receipt = e["evidence_log"][-1]
        self.assertEqual(receipt["was"]["keywords"],
                         _minted(self.AUTHOR, self.STEMS))
        healed, _ = store.doctor()
        self.assertEqual([str(x["id"]) for x in healed["stem_unfit"]], [])

    def test_a_stem_shaped_cell_before_an_authored_cell_is_authored(self):
        _plant_census({"retry": 0.5})
        self.seed_prior("authored-row", "a rule", conf=1.0,
                        keywords="1, retry, retry 1 fluxcap, pane tail")
        self.seed("unfit-row")                         # control: same census
        report, _ = store.doctor()
        rows = {str(e["id"]) for e in report["stem_unfit"]}
        self.assertIn("unfit-row", rows)
        self.assertNotIn("authored-row", rows)

    def test_an_unmeasured_census_removes_the_shape_leg_only(self):
        from helm import promptcensus
        _plant_census({"retry": 0.5}, turns=promptcensus.MIN_TURNS - 1)
        self.seed()
        report, _ = store.doctor()
        rows = {str(e["id"]): e["stem_unfit"] for e in report["stem_unfit"]}
        self.assertEqual(rows, {"unfit-row": ["1"]})
        self.assertEqual([r["id"] for r in report["census_unmeasured"]],
                         ["prompt-census"])

    def test_the_cli_prints_the_class_and_its_fix(self):
        _plant_census({"retry": 0.5})
        self.seed()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = store.cmd_store(["doctor"])
        self.assertEqual(rc, 0)
        self.assertIn("GENERATED STEMS THE MINT GATE WOULD NOT MINT (1)",
                      out.getvalue())
        self.assertIn("unfit-row", out.getvalue())
        self.assertIn("--fix removes", out.getvalue())


class LoneWordResolveTest(StoreBase):
    """task/2978: a lone single-word match must clear a rarity bar. MEASURED
    on the E2 gold: 61% of JIT fires came from ONE single-word
    probe and 7.8% of those were relevant. The relevant lone words sat at a
    prompt fraction of at most 3.5% of turns; most irrelevant ones above it
    ("please", "fine", "know", "ready", "report")."""

    def seed(self):
        self.seed_prior("common-lone", "a rule", conf=1.0,
                        keywords="please, rotate the flux keys")
        self.seed_prior("rare-lone", "a rule", conf=1.0, keywords="fluxcap")
        self.seed_prior("phrase-row", "a rule", conf=1.0, keywords="please hold")
        self.seed_prior("two-words", "a rule", conf=1.0, keywords="please, check")

    def ids(self, text, **kw):
        return sorted(str(e["id"]) for e in store.resolve_prompt(text, cap=9, **kw))

    def test_a_common_lone_word_is_held_and_a_rare_one_fires(self):
        _plant_census({"please": 0.5, "check": 0.5, "hold": 0.5})
        self.seed()
        got = self.ids("please look at the fluxcap")
        self.assertIn("rare-lone", got)
        self.assertNotIn("common-lone", got)

    def test_a_phrase_or_two_probes_still_fire(self):
        _plant_census({"please": 0.5, "check": 0.5, "hold": 0.5})
        self.seed()
        self.assertIn("phrase-row", self.ids("please hold the lane"))
        self.assertIn("two-words", self.ids("please check the lane"))

    def test_an_unmeasured_census_holds_nothing(self):
        from helm import promptcensus
        _plant_census({"please": 0.5}, turns=promptcensus.MIN_TURNS - 1)
        self.seed()
        self.assertIn("common-lone", self.ids("please look"))

    def test_the_held_entry_is_named_with_its_fraction(self):
        _plant_census({"please": 0.5})
        self.seed()
        held = []
        got = store.resolve_prompt("please look at the fluxcap", cap=9,
                                   held=held)
        self.assertIn("rare-lone", [str(e["id"]) for e in got])     # control
        named = {str(e["id"]): (p, f) for e, p, f in held}
        self.assertEqual(named["common-lone"][0], "please")
        self.assertAlmostEqual(named["common-lone"][1], 0.5)
        self.assertNotIn("rare-lone", named)


class ScopedResolveTest(StoreBase):
    """task/2978: `helm store resolve` from the helm checkout returned
    a lexicon recorded to another project. The seat door
    (inject) was fenced; the resolve door was not. A project-scoped entry
    answers only in its project; fleet entries answer everywhere."""

    def seed(self):
        self.seed_prior("other-only", "another project's rule", conf=1.0,
                        keywords="flumpet", project="other-project")
        self.seed_prior("fleet-row", "a fleet rule", conf=1.0,
                        keywords="flumpet", project="fleet")

    def ids(self, text, **kw):
        return sorted(str(e["id"]) for e in store.resolve_prompt(text, cap=9, **kw))

    def test_another_projects_entry_does_not_resolve_here(self):
        self.seed()
        self.assertEqual(self.ids("the flumpet broke", project="helm"),
                         ["fleet-row"])
        self.assertEqual(self.ids("the flumpet broke",
                                  project="other-project"),
                         ["fleet-row", "other-only"])

    def test_the_cli_answers_for_its_project(self):
        self.seed()
        out = io.StringIO()
        with contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(io.StringIO()):
            rc = store.cmd_store(["resolve", "--project", "helm",
                                  "the", "flumpet", "broke"])
        self.assertEqual(rc, 0)
        self.assertIn("fleet-row", out.getvalue())
        self.assertNotIn("other-only", out.getvalue())

    def test_a_seat_in_no_project_receives_fleet_entries_only(self):
        self.seed()
        seat = sorted(str(e["id"]) for e in
                      store.load_all(scope_fence=True))
        self.assertIn("fleet-row", seat)
        self.assertNotIn("other-only", seat)
        inventory = sorted(str(e["id"]) for e in store.load_all())
        self.assertIn("other-only", inventory)            # the inventory door


def _store_cli(*args):
    """-> (rc, stdout + stderr) of one `helm store` call."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = store.cmd_store(list(args))
    return rc, out.getvalue() + err.getvalue()


class SpacedIdMintTest(StoreBase):
    """AN ID IS ONE TOKEN (task/2980). It is the name every pointer, gate,
    supersede link and `helm store get` spells. MEASURED on the live store: 8 fired
    entries had ids with spaces and fired 97 times in 48 h; the worst,
    `beacon-also-freezes-seat-config beacon-also-freezes-seat-config`, spent
    its line on the id twice and delivered one word of rule, "EXTENDS"."""

    def test_canonical_id_reads_the_four_measured_shapes(self):  # noqa: VACUOUS_ASSERTION — the read set above asserts five exact ids from the same function
        # kills: a guessed id for a row no rule can read (the must-miss set)
        # and a lost reading of any measured shape
        read = {
            "beacon-also-freezes-seat-config beacon-also-freezes-seat-config":
                "beacon-also-freezes-seat-config",
            "add operator-absence-is-not-a-gate":
                "operator-absence-is-not-a-gate",
            "hardcode-is-eventual-failure CANON (Daria 2026-07-02): A HARDCODE":
                "hardcode-is-eventual-failure",
            "count the distance before you spend a gate":
                "count-the-distance-before-you-spend-a-gate",
            "re-run the proving measurement against the cure":
                "re-run-the-proving-measurement-against-the-cure",
        }
        for raw, want in read.items():
            self.assertEqual(store.canonical_id(raw), want, raw)
        for raw in ("as-public repo STORY law (Daria 2026-07-24): the repo",
                    "A COUNCIL MEASURES TARGETED REFUTATION, NOT DISCOVERY",
                    "probe: does the anchor failure now name the status",
                    "0.3 flagship eval (Daria 2026-07-24): cc-codex vs pi"):
            self.assertIsNone(store.canonical_id(raw), raw)

    def test_add_refuses_an_id_with_whitespace_for_every_id_type(self):  # noqa: VACUOUS_ASSERTION — each refusal asserts rc 1 and its message, and the kebab control asserts rc 0 on the same door
        # kills: the refusal missing at any door type, and a refusal that
        # writes anyway
        for argv in (("prior", "add some-rule | a statement | 0.7 | kwone,kwtwo"),
                     ("premise", "some-rule some-rule | a statement | kwone,kwtwo"),
                     ("heuristic", "a phrase id | a move | trig one,trig two"),
                     ("reference", "x y | a summary | https://e.x | kwone,kwtwo")):
            rc, out = _store_cli("add", *argv)
            self.assertEqual(rc, 1, (argv, out))
            self.assertIn("is not an id", out)
        self.assertEqual(store.load_all(include_retired=True), [])
        # the control: the kebab spelling the refusal names is accepted
        rc, out = _store_cli("add", "prior",
                             "some-rule | a statement | 0.7 | kwone,kwtwo")
        self.assertEqual(rc, 0, out)
        # and a lexicon TERM keeps its spaces: it is the phrase prompts carry
        rc, out = _store_cli("add", "lexicon",
                             "stripe test mode | a sandbox that takes fake cards")
        self.assertEqual(rc, 0, out)

    def test_premise_capture_refuses_the_leaked_add_and_names_the_id(self):  # noqa: VACUOUS_ASSERTION — the refusal asserts rc 1 and names the kebab id; writing nothing is the contract
        # THE SOURCE of `add operator-absence-is-not-a-gate`: `premise` has no
        # `add` subcommand, so the word became part of the id
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(err):
            rc = premise.cmd_premise(["add leaked-verb-rule | the statement "
                                      "| kwone,kwtwo"])
        self.assertEqual(rc, 1, err.getvalue())
        self.assertIn("'leaked-verb-rule'", err.getvalue())
        self.assertIn("drop the `add`", err.getvalue())
        self.assertEqual(store.load_all(include_retired=True), [])


class SpacedIdDoctorTest(StoreBase):
    """`helm store doctor` refuses an id with whitespace (task/2980): it
    reports every such row with what --fix makes of it or why it holds, and
    --fix re-keys the movable ones, leaving a tombstone at the old id."""

    def seed(self, pid, statement="a statement", keywords="probeone,probetwo",
             **kw):
        return self.seed_prior(pid, statement, conf=1.0, keywords=keywords, **kw)

    def disk(self):
        d = self.global_dir("premises")
        out = {}
        for name in sorted(os.listdir(d)):
            with open(os.path.join(d, name), "rb") as f:
                out[name] = f.read()
        return out

    def test_the_dry_run_names_what_each_row_becomes_and_writes_nothing(self):  # noqa: VACUOUS_ASSERTION — the verdicts are asserted per row; an unchanged disk is the dry-run contract, and the fix arms change the same files
        # kills: a row guessed at, a held row reported movable, and a dry run
        # that writes
        self.seed("dup-rule dup-rule")
        self.seed("add leaked-rule")
        self.seed("Capital Mess (x): the rule")
        self.seed("sealed-rule CANON (x): the rule",
                  attest_payload="prem:b2b:" + "0" * 64,
                  attest_ts=TS, attest_by="daria")
        self.seed("taken-rule", "the row that already has the id")
        self.seed("taken-rule taken-rule")
        store.write_lexicon({"term": "stripe test mode",
                             "definition": "a sandbox", "updated_ts": TS})
        before = self.disk()
        report, actions = store.doctor()
        self.assertEqual(actions, [])
        self.assertEqual(self.disk(), before)
        got = {str(e["id"]): (e.get("rekey"), e.get("rekey_held") or "")
               for e in report["space_ids"]}
        self.assertEqual(got["dup-rule dup-rule"][0], "dup-rule")
        self.assertEqual(got["add leaked-rule"][0], "leaked-rule")
        self.assertIn("no mechanical kebab id",
                      got["Capital Mess (x): the rule"][1])
        self.assertIn("attested", got["sealed-rule CANON (x): the rule"][1])
        self.assertIn("taken by prior:taken-rule",
                      got["taken-rule taken-rule"][1])
        self.assertEqual(len(got), 5)
        # a lexicon TERM is not an id: it stays in the report-only class
        self.assertEqual([str(e["id"]) for e in report["statement_ids"]],
                         ["stripe test mode"])
        rc, out = _store_cli("doctor")
        self.assertEqual(rc, 0)
        self.assertIn("IDS WITH WHITESPACE (5)", out)
        self.assertIn("dup-rule dup-rule -> dup-rule", out)
        self.assertIn("HELD: attested", out)

    def test_fix_rekeys_with_a_tombstone_and_repoints_every_link(self):
        # kills: a rename that orphans the gate or the supersede link naming
        # it, one that deletes the old row, and one that leaves both live
        old_path = self.seed("dup-rule dup-rule", "the dup rule",
                             keywords="dupword,dupother")
        self.seed("linker", "a rule gated on it", keywords="linkword,linkother",
                  gates="dup-rule dup-rule")
        self.seed("typed-linker", "a typed gate on it",
                  keywords="typedword,typedother", gates="prior:dup-rule dup-rule")
        self.seed("old-one", "a row it superseded", keywords="oldword,oldother",
                  status="delete_eligible", replaced_by="dup-rule dup-rule")
        with open(self.seed("healthy-control", "never touched",
                            keywords="healthy,control"), "rb") as f:
            control = f.read()
        report, actions = store.doctor(fix=True, ts=TS)
        self.assertIn(("dup-rule", "space_ids"), [a[:2] for a in actions])
        new = store._find("dup-rule")
        self.assertEqual(new["status"], "live")
        self.assertEqual(os.path.basename(new["path"]), "prior-dup-rule.md")
        self.assertEqual(new["supersedes"], "dup-rule dup-rule")
        self.assertEqual(new["statement"], "the dup rule")
        self.assertIn({"id": "dup-rule dup-rule", "path": old_path},
                      [r.get("was") for r in new["evidence_log"]])
        tomb = store._find("dup-rule dup-rule")
        self.assertEqual(tomb["path"], old_path)
        self.assertEqual(tomb["status"], "delete_eligible")
        self.assertEqual(tomb["replaced_by"], "dup-rule")
        self.assertEqual(store._find("linker")["gates"], "dup-rule")
        self.assertEqual(store._find("typed-linker")["gates"], "prior:dup-rule")
        self.assertEqual(store._find("old-one")["replaced_by"], "dup-rule")
        with open(store._find("healthy-control")["path"], "rb") as f:
            self.assertEqual(f.read(), control)
        # the rule answers under its kebab id, and the tombstone never does
        ids = [str(e["id"]) for e in store.resolve_prompt("dupword")]
        self.assertIn("dup-rule", ids)
        self.assertNotIn("dup-rule dup-rule", ids)
        live = [z for z in store.load_all()
                if z.get("status") in store.INJECTABLE_STATUSES]
        self.assertEqual(str(store.resolve_gate("dup-rule", live)["id"]),
                         "dup-rule")
        # the old spelling still answers, and names where the rule went
        rc, out = _store_cli("get", "dup-rule dup-rule")
        self.assertEqual(rc, 0, out)
        self.assertIn("replaced_by: dup-rule", out)
        # idempotent: nothing left to re-key
        report, actions = store.doctor(fix=True, ts=TS)
        self.assertEqual(report["space_ids"], [])
        self.assertEqual(actions, [])

    def test_a_phrase_id_is_respelled_in_place(self):
        # the kebab id slugs to the row's OWN key, so its file and every link
        # already resolve: no second file and no tombstone
        path = store.write_heuristic({
            "id": "count the distance first", "move": "count it first",
            "trigger": "distance,behind trunk", "stated_ts": TS},
            root_dir=self.global_dir("heuristics"))
        before = sorted(os.listdir(os.path.dirname(path)))
        report, actions = store.doctor(fix=True, ts=TS)
        self.assertIn(("count-the-distance-first", "space_ids"),
                      [a[:2] for a in actions])
        self.assertEqual(sorted(os.listdir(os.path.dirname(path))), before)
        e = store._find("count-the-distance-first")
        self.assertEqual((e["id"], e["path"], e["status"]),
                         ("count-the-distance-first", path, "live"))

    def test_a_rows_other_repair_lands_in_the_same_new_file(self):  # noqa: VACUOUS_ASSERTION — both receipts and the new keywords are asserted present; no stage residue is the contract
        # kills: two plan items for one path — the second stage overwrote the
        # first and its rename found nothing, landing half the batch
        old_path = self.seed("dup-rule dup-rule", "the dup rule",
                             keywords="audit parity migration runtime")
        report, actions = store.doctor(fix=True, ts=TS)
        new = store._find("dup-rule")
        self.assertEqual(new["keywords"], "audit,parity,migration,runtime")
        kinds = [r.get("reason", "") for r in new["evidence_log"]]
        self.assertTrue(any("comma-ized" in k for k in kinds), kinds)
        self.assertTrue(any("re-keyed from" in k for k in kinds), kinds)
        self.assertEqual(store._find("dup-rule dup-rule")["status"],
                         "delete_eligible")
        self.assertEqual(store._find("dup-rule dup-rule")["path"], old_path)
        self.assertFalse([n for n in os.listdir(os.path.dirname(old_path))
                          if n.startswith(".doctor-stage")])

    def test_a_held_row_is_left_byte_identical_by_fix(self):  # noqa: VACUOUS_ASSERTION — the movable row in the same run is asserted CHANGED, so the held rows' stillness is the hold
        # the must-miss, with its control in the same run: the movable row's
        # file DOES change, so the stillness below is the hold
        held = [self.seed("Capital Mess (x): the rule"),
                self.seed("sealed-rule CANON (x): the rule",
                          attest_payload="prem:b2b:" + "0" * 64,
                          attest_ts=TS, attest_by="daria")]
        moved = self.seed("dup-rule dup-rule")
        def read(path):
            with open(path, "rb") as f:
                return f.read()
        before = [read(p) for p in held + [moved]]
        store.doctor(fix=True, ts=TS)
        after = [read(p) for p in held + [moved]]
        self.assertEqual(after[:2], before[:2])
        self.assertNotEqual(after[2], before[2])


class WideKeysDoctorTest(StoreBase):
    """Keys are candidate generators, 3-6 short symptom probes (heuristic
    store-keywords-few-short-rare; trigger design R3). Past 6
    authored probes the doctor warns: the entry is two entries or a route."""

    def test_more_than_six_authored_probes_warns_and_never_writes(self):
        # kills: counting the guard's own stems or a route declaration as
        # authored, and a warning that writes
        self.seed_prior("wide-row", "s", conf=1.0,
                        keywords="aa1,bb2,cc3,dd4,ee5,ff6,gg7")
        self.seed_prior("six-row", "s", conf=1.0,
                        keywords="aa1,bb2,cc3,dd4,ee5,ff6")
        self.seed_prior("routed-six", "s", conf=1.0,
                        keywords="aa1,bb2,cc3,dd4,ee5,ff6,notice:x")
        report, actions = store.doctor()
        wide = {str(e["id"]): e["wide_keys"] for e in report["wide_keys"]}
        self.assertEqual(wide, {"wide-row": 7})
        self.assertEqual(actions, [])
        # a field the guard grew past six with its own stems is not wide:
        # the stems are its, not the author's
        kw, err, _n, _ev = store.guard_entry_keywords(
            "prior", "stemmed-six", "vault token rotation broke,hh2,ii3,jj4,"
            "kk5,ll6")
        self.assertIsNone(err, err)
        self.assertGreater(len(store._kw_list(kw)), 6,
                           "control: the guard must have appended stems")
        self.seed_prior("stemmed-six", "s", conf=1.0, keywords=kw)
        report, _ = store.doctor()
        self.assertNotIn("stemmed-six",
                         [str(e["id"]) for e in report["wide_keys"]])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(store.cmd_store(["doctor"]), 0)
        self.assertIn("WIDE KEYS (1)", out.getvalue())
        self.assertIn("wide-row", out.getvalue())
