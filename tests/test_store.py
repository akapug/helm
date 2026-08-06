#!/usr/bin/env python3
"""store tests — hermetic: every root (adopted + helm-global + project) points
at a tempdir via HELM_HOME + HELM_ADOPTED_DIR. The real ~/.claude, ~/.helm and
legacy stores are never read or written."""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from unittest import mock

import os as _os, sys as _sys  # noqa: E402
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
from tests._tmphome import home as _tmp_home  # noqa: E402
_tmp_home(prefix="helm-test-home-", var="HELM_HOME")

from helm import home, pk, store  # noqa: E402

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

    def one(self, es, eid):
        hits = [e for e in es if e["id"] == eid]
        self.assertEqual(len(hits), 1, "expected exactly one '%s' in %r"
                         % (eid, [x["id"] for x in es]))
        return hits[0]


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
                   "2026-08-03", "1781658848", "2026-08-03 09:44:23Z",
                   "2026-08-03T09:44:23", pk.now_ts()):
            self.assertIsNone(store_write._valid_write_ts(ok), ok)
        for bad in ("NOW", "", None, "yesterday", "2026-13-45T99:99:99Z"):
            self.assertTrue(store_write._valid_write_ts(bad), repr(bad))


class GlossLifecycleMatrixTest(StoreBase):
    """A GLOSS MUST SURVIVE EVERY LIFECYCLE PATH, and be DROPPED by exactly one.

    Requested by codex on review 2026-07-30, and the review found why it was
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
        """codex named these two by name: heuristic gloss lost on retire,
        reference gloss lost on demote."""
        self._plant("heuristic", "lc-retire")
        store.retire("lc-retire", self.TS, why="done")
        self.assertEqual(self._gloss_of("lc-retire"), "a short firing line")
        # A REFERENCE, not a prior: codex reproduced the demote loss on a
        # REFERENCE, and my first fixture planted a prior — a regression test
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
    """The four contrary verdicts of lane gloss-fires-full-entry-stays (codex
    2026-07-30: b7ac0070517d, 79553cbbbfe5, d59c552c3ff6, ea3b086bc687), each
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
        """codex round 3: '114 chars/414 UTF-8 bytes passes'. A four-byte code
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
        """codex rounds 2+3: an untyped probe measured the generic branch, and
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
        """codex rounds 1-3, three spellings of one loss: a re-mint under a
        retired slug carries NEW, often contradictory text — a kept gloss
        fires a line the entry no longer says. STALE_ON_REMINT owns the scrub
        (one spelling, shared with premise/_capture — the second hard-coded
        copy is exactly how round 3 happened)."""
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
        """codex round 3 named BOTH directions: a keyword-only sharpen (same
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
        """codex's r2 blocker on this lane: write_lexicon whitespace-normalizes
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
        """codex round 4's repro, replayed deterministically: writer B lands a
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
        """codex round 4's second leg: a rejected oversized write once left
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

        Live 2026-07-28: a seat wrote an incident runbook with
        `--project sibling-inc`, could not resolve it from that project's cwd, concluded
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
    (docs/CANON_CONTROLLED_LANGUAGE.md §2, docs/CANON_CONTROLLED_LANGUAGE_LANES.md
    Lane 1): the lexicon synonym-map + probe-fold that removes CD's measured
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
        # CD's DF-split reproduced and fixed. The concept lives in ONE canonical
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
        # memory (name+description, type: project, no id/statement) — the resolver drops
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
        # 4 pins, each line truncating to exactly LINE_CAP (400B) -> 3 fit the
        # 1200B budget, pin-3 (same conf, same ts, id-last) starves
        for i in range(4):
            self.seed_prior("pin-%d" % i, "x" * 400, conf=1.0, pin="true")

    def ledger(self, rows):
        from helm import inject
        path = inject._ledger_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write("\n".join(rows) + "\n")
        return path

    def test_the_walk_is_seeded_by_the_who_digest_like_gather(self):
        """THE MODEL MUST BE THE WALK, NOT A LIKENESS OF IT.

        gather (inject/_whisper.py) emits the WHO digest FIRST and only then
        walks the pinned entries, so an entry competes for PINNED_BUDGET minus
        the digest. This started at used=0 and was therefore optimistic: on the
        live store 2026-07-30 the digest was 350 bytes (its whole WHO_CAP),
        leaving 850 of 1200, and the model reported 3 entries fitting where the
        live walk fits 2. A starvation predicate that silently clears an entry
        is the same failure as having no predicate.

        The first cut of the seed ALSO failed silently: `from ..inject import
        _whisper` yields the FUNCTION of that name (the package re-exports a
        `_whisper` symbol that shadows the submodule), the call raised
        AttributeError, and the fail-open except returned the optimistic answer
        with nothing to show for it. Hence a test on the OBSERVABLE, not on the
        import."""
        from helm import inject, store as st
        base = st.pinned_stats()
        self.assertEqual(len(base["fits"]), 3, "no digest -> 3 x 400 fills 1200")
        with mock.patch.object(inject, "_who_lines", return_value=["W" * 400]):
            with_who = st.pinned_stats()
        self.assertEqual(len(with_who["fits"]), 2,
                         "a 400B digest leaves 800B -> only 2 entries can fit; "
                         "a walk that ignores it clears one entry that cannot "
                         "actually fire")
        self.assertGreaterEqual(with_who["used"], 400,
                                "used must account for the digest gather emits")
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
        self.assertIn("4 always, budget 1200B", out)
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
        self.assertIn("1200/1200 bytes used", out)   # names WHY it cannot fire
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

    def test_under_budget_noop(self):
        self.seed_prior("a", "s", conf=0.8, keywords="akw", root_dir=self.adopted)
        self._index(["# idx", "- [a](prior-a.md) a"])
        r = store.index_cap(budget_lines=60, apply=True)
        self.assertEqual((r["over"], r["demoted"]), (0, 0))

    def test_over_budget_but_nothing_safe(self):
        # over budget, but every line is untyped -> nothing lossless-demotable
        self._index(["# idx", "- [x](random-x.md) x", "- [y](random-y.md) y", "- z"])
        r = store.index_cap(budget_lines=1, apply=True)
        self.assertEqual((r["over"], r["demotable"], r["demoted"]), (3, 0, 0))
        rc, out, _ = self.run_cli(["cap", "--budget-lines", "1", "--apply"])
        self.assertIn("NOTHING safely demotable", out)

    def test_concurrent_append_preserved_on_apply(self):
        self.seed_prior("old-one", "old", conf=0.8, keywords="oldkw",
                        root_dir=self.adopted, last_updated="2026-01-01T00:00:00Z")
        self._index(["# idx", "- [old](prior-old-one.md) old", "- tail1", "- tail2"])
        # the re-read + line-set rewrite path drops ONLY the demoted line; every
        # other line (incl. a concurrent native append) survives
        r = store.index_cap(budget_lines=2, apply=True)
        self.assertEqual(r["demoted"], 1)
        with open(os.path.join(self.adopted, "MEMORY.md")) as f:
            kept = f.read()
        self.assertIn("tail1", kept)   # non-demoted lines survive
        self.assertIn("tail2", kept)
        self.assertNotIn("prior-old-one.md", kept)

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

    def test_confirm_prior_receipt_and_confidence_untouched(self):
        self.add("prior", "x-law | glorp before zork | 0.7 | glorpwork", "--candidate")
        e, err = store.confirm("x-law", TS)
        self.assertIsNone(err)
        self.assertEqual((e["status"], e["source"]), ("live", "explicit"))
        # confirm ratifies the capture, never inflates the belief
        e = self.one(store.load_all(), "x-law")
        self.assertAlmostEqual(e["confidence"], 0.7)
        # who/when receipt lives in the prior's own evidence_log
        r = e["evidence_log"][-1]
        self.assertEqual((r["type"], r["by"]), ("confirmed", "human"))
        self.assertEqual([x["id"] for x in store.resolve_prompt("glorpwork now")],
                         ["x-law"])

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
        """@codex-2's finding, and the one that made this cache unsafe in
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
        """@codex-2's second finding. `list(hit)` built a new LIST around the
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
        """@codex-2's R2 finding, and it killed a bound I had ARGUED for.

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

    def test_each_return_site_hands_out_its_OWN_copy(self):  # noqa: VACUOUS_ASSERTION — the arms sit inside try/finally only to restore the patched loader; mutation-proven non-vacuous (hit-site neutered -> 1 red, miss-site -> 3 red, restored -> green, 2026-08-01)
        """@codex-2's reissue finding, closed as TWO NAMED ARMS on one
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
    """#188 — `/learn` mandates a resolve-widen-RETEST loop and the store had no
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
