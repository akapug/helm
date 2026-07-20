#!/usr/bin/env python3
"""mentor tests — observe / teach / review / log, the inception actuator.

Pins the laws: observe is READ-ONLY (no drift snapshot consumed, no registry
sync, zero filesystem mutation) and fail-open (a dead catalog degrades to a
note, never a crash); miss-patterns seed ONLY from kind: bug-class lexicon
terms (literal needles — the plausible-noise rail); teach writes exactly ONE
provenanced reflex file into the target project's reflexes/ (same-id = hard
refuse, unknown project = refuse, receipt on the events journal) and delivery
rides the EXISTING inject lane; --attest signs ONE self-write turn and
annotates in place, substrate-down lands the reflex anyway and backfills
later; review joins the fire-ledger (fires since stated_ts) with before/after
transcript recurrence and says fires-are-not-heeds; log reads the taught
record, untaught reflexes invisible. Hermetic: tmp HELM_HOME, planted
counters/ledger/transcripts, the session catalog mocked at sessions.rows_for."""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("HELM_HOME", tempfile.mkdtemp(prefix="helm-test-home-"))

from helm import home, inject, mentor, pk, premise, record, reflex, sessions, store  # noqa: E402

ENV_KEYS = ("HELM_HOME", "MELD_HOME", "HELM_ADOPTED_DIR", "HELM_CACHE_DIR",
            "HELM_ACTOR", "CLAUDE_SESSION_ID", "HELM_CELL_PROFILE",
            "MELD_AGENT_PROFILE", "HELM_NODE_URL", "MELD_NODE_URL")

TS_OLD = "2026-01-01T00:00:00Z"


def snapshot(root):
    """{relpath: (size, mtime_ns)} for every file under root."""
    out = {}
    for dirpath, _dirs, files in os.walk(root):
        for f in files:
            p = os.path.join(dirpath, f)
            st = os.stat(p)
            out[os.path.relpath(p, root)] = (st.st_size, st.st_mtime_ns)
    return out


class MentorBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-mentor-")
        self.env_prior = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_ADOPTED_DIR"] = os.path.join(self.tmp, "adopted")
        os.environ["HELM_CACHE_DIR"] = os.path.join(self.tmp, "cache")
        os.environ["HELM_NODE_URL"] = "http://127.0.0.1:1"  # dead: anchor fails open
        os.makedirs(os.environ["HELM_ADOPTED_DIR"])

    def tearDown(self):
        for k, v in self.env_prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_cmd(self, args, rows=(), raising=False):
        """cmd_mentor with the session catalog mocked -> (rc, out, err)."""
        out, err = io.StringIO(), io.StringIO()
        kw = {"side_effect": RuntimeError("catalog down")} if raising \
            else {"return_value": list(rows)}
        with mock.patch.object(sessions, "rows_for", **kw), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = mentor.cmd_mentor(list(args))
        return rc, out.getvalue(), err.getvalue()

    def reg(self, *names):
        pk.write_json(home.registry_path(), {"version": 1, "projects": {
            n: {"name": n, "path": "/p/" + n} for n in names}})

    def row(self, sid, path="", mt=None, harness="claude", title="t"):
        return {"i": sid, "h": harness, "t": title, "p": path,
                "mt": int(time.time() if mt is None else mt)}

    def transcript(self, name, text):
        p = os.path.join(self.tmp, name + ".jsonl")
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
        return p

    def counters(self, sid, **kv):
        pk.write_json(os.path.join(record.session_dir(sid), "counters.json"), kv)

    def seed_lex(self, term, kind="bug-class"):
        store.write_lexicon({"term": term, "definition": "def of " + term,
                             "kind": kind},
                            root_dir=os.path.join(home.global_dir(), "lexicon"))

    def seed_prior(self, pid, conf):
        store.write_prior({"id": pid, "statement": "s-" + pid, "confidence": conf,
                           "stated_ts": TS_OLD, "source": "human", "keywords": pid},
                          root_dir=os.path.join(home.global_dir(), "premises"))

    def plant_ledger(self, rows):
        path = inject._ledger_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")

    @staticmethod
    def turn(jit=(), reflexes=(), ts=TS_OLD):
        if not (jit or reflexes):
            return {"v": 1, "ts": ts, "project": None, "silent": True}
        return {"v": 1, "ts": ts, "project": None,
                "fired": {"pinned": [], "jit": list(jit), "reflex": list(reflexes)}}

    def teach(self, project="proj", rid="parked-human-resolvable-gate",
              steer="surface human-resolvable gates, never park them", extra=()):
        self.reg(project, "other")
        rc, out, err = self.run_cmd(
            ["teach", project, "%s | %s" % (rid, steer)] + list(extra))
        return rc, out, err


class ObserveTest(MentorBase):
    def test_quiet_estate_one_line_plus_seed_hint(self):
        rc, out, _ = self.run_cmd(["observe", "proj"])
        self.assertEqual(rc, 0)
        self.assertIn("0 sessions scanned: quiet", out)
        self.assertIn('helm store add lexicon "<term> | <definition> | bug-class"', out)

    def test_recorder_flags_floored_and_ranked(self):
        self.counters("s1", **{"stuck-streak": 4, "loop-streak": 3})
        self.counters("s2", **{"passive-streak": 20})
        self.counters("s3", **{"stuck-streak": 2, "dirty-streak": 9})  # under floors
        rows = [self.row(s) for s in ("s1", "s2", "s3")]
        rc, out, _ = self.run_cmd(["observe", "proj"], rows=rows)
        self.assertEqual(rc, 0)
        self.assertIn("RECORDER", out)
        self.assertIn("stuck x4, loop-thrash x3", out)
        self.assertIn("passive x20", out)
        self.assertNotIn("s3", out)
        self.assertLess(out.index("s1"), out.index("s2"), "strength ranks s1 first")

    def test_bug_class_terms_scanned_literally_and_minted(self):
        self.seed_lex("identified-fix-filed-not-fixed")
        self.seed_lex("drain", kind="phrase")  # non-bug-class: never a needle
        t1 = self.transcript("a", "x identified-fix-filed-not-fixed y drain z "
                                  "identified-fix-filed-not-fixed")
        t2 = self.transcript("b", "one identified-fix-filed-not-fixed, drain")
        t3 = self.transcript("c", "clean session")
        rows = [self.row("s1", t1), self.row("s2", t2), self.row("s3", t3)]
        rc, out, _ = self.run_cmd(["observe", "proj"], rows=rows)
        self.assertEqual(rc, 0)
        self.assertIn("MISS-PATTERNS", out)
        self.assertIn("identified-fix-filed-not-fixed", out)
        self.assertIn("2 sessions, 3 mentions", out)
        self.assertIn('teach: helm mentor teach proj '
                      '"identified-fix-filed-not-fixed | <steer>"', out)
        self.assertNotIn("drain ", out.split("teach:")[0].split("MISS")[1])
        self.assertIn("the judging is yours", out)

    def test_since_window_excludes_old_sessions(self):
        self.seed_lex("loop-thrash-tell")
        told = self.transcript("old", "loop-thrash-tell")
        tnew = self.transcript("new", "loop-thrash-tell")
        rows = [self.row("s-old", told, mt=time.time() - 7200),
                self.row("s-new", tnew, mt=time.time() - 60)]
        rc, out, _ = self.run_cmd(["observe", "proj", "--since", "1h"], rows=rows)
        self.assertEqual(rc, 0)
        self.assertIn("1 session scanned", out)
        self.assertIn("1 session, 1 mention", out)

    def test_estate_composes_drift_and_fire_observers(self):
        self.seed_prior("wallp", 0.6)
        self.seed_prior("dorm", 0.3)
        self.plant_ledger([self.turn(jit=["wallp"])] * 18
                          + [self.turn(jit=["other"])] * 6)
        rc, out, _ = self.run_cmd(["observe", "proj"])
        self.assertEqual(rc, 0)
        self.assertIn("ESTATE", out)
        self.assertIn("[fire] 'wallp' fired in 18/24 non-silent turns", out)
        self.assertIn("[drift] 'dorm' dormant at 0.30", out)
        self.assertLess(out.index("[fire]"), out.index("[drift]"),
                        "evidence strength ranks wallpaper first")

    def test_read_only_no_mutation_no_drift_snapshot(self):
        self.seed_lex("stuck-tell")
        self.seed_prior("dorm", 0.3)
        self.counters("s1", **{"stuck-streak": 5})
        p = self.transcript("a", "stuck-tell")
        self.plant_ledger([self.turn(jit=["x"])] * 3)
        before = snapshot(self.tmp)
        rc, _, _ = self.run_cmd(["observe", "proj"], rows=[self.row("s1", p)])
        self.assertEqual(rc, 0)
        self.assertEqual(snapshot(self.tmp), before,
                         "observe mutated the estate — read-only broken")

    def test_dead_catalog_fails_open_with_a_note(self):
        rc, out, _ = self.run_cmd(["observe", "proj"], raising=True)
        self.assertEqual(rc, 0)
        self.assertIn("session catalog unavailable", out)

    def test_bad_args(self):
        self.assertEqual(self.run_cmd(["observe"])[0], 2)
        rc, _, err = self.run_cmd(["observe", "proj", "--since", "soon"])
        self.assertEqual(rc, 2)
        self.assertIn("bad --since", err)


class TeachTest(MentorBase):
    def test_teach_writes_one_provenanced_reflex_plus_receipt(self):
        os.environ["HELM_ACTOR"] = "teacher-x"
        rc, out, _ = self.teach()
        self.assertEqual(rc, 0)
        self.assertIn("TAUGHT 'parked-human-resolvable-gate' -> proj (prompt) "
                      "by teacher-x", out)
        self.assertIn("delivery is free", out)
        es = mentor.taught("proj")
        self.assertEqual(len(es), 1)
        e = es[0]
        self.assertEqual((e["teacher"], e["target"], e["source"]),
                         ("teacher-x", "proj", "mentor-teach"))
        self.assertEqual(e["pattern"], r"\bparked\-human\-resolvable\-gate\b")
        self.assertTrue(e["path"].startswith(
            os.path.join(home.project_dir("proj"), "reflexes")))
        ev = pk.read_events(5)
        self.assertEqual((ev[-1]["verb"], ev[-1]["target"], ev[-1]["actor"]),
                         ("mentor.teach", "parked-human-resolvable-gate", "teacher-x"))

    def test_delivery_rides_the_existing_inject_lane(self):
        self.teach(steer="never park a human-resolvable gate")
        fired = reflex.fire("we parked-human-resolvable-gate again", project="proj")
        self.assertEqual([e["id"] for e in fired], ["parked-human-resolvable-gate"])
        sections = inject.gather("we parked-human-resolvable-gate again",
                                 project="proj")
        self.assertIn("REFLEX: never park a human-resolvable gate",
                      sections["reflex"])
        self.assertEqual(reflex.fire("an unrelated turn", project="proj"), [])

    def test_same_id_hard_refuse_never_overwrites(self):
        self.teach(steer="first steer")
        rc, _, err = self.run_cmd(
            ["teach", "proj", "parked-human-resolvable-gate | second steer"])
        self.assertEqual(rc, 1)
        self.assertIn("supersede-not-duplicate", err)
        self.assertEqual(mentor.taught("proj")[0]["steer"], "first steer")

    def test_unknown_project_refused(self):
        rc, _, err = self.run_cmd(["teach", "ghost", "id | steer"])
        self.assertEqual(rc, 1)
        self.assertIn("unknown project 'ghost'", err)
        self.assertFalse(os.path.exists(home.project_dir("ghost")))

    def test_teacher_flag_and_global_shadow_note(self):
        reflex.write({"id": "gate-x", "steer": "global steer", "signal": "prompt",
                      "pattern": "x", "stated_ts": TS_OLD})
        rc, out, _ = self.teach(rid="gate-x", steer="project steer",
                                extra=["--teacher", "capt"])
        self.assertEqual(rc, 0)
        self.assertIn("by capt", out)
        self.assertIn("shadows the global reflex 'gate-x'", out)

    def test_usage_shapes(self):
        self.reg("proj")
        for args in (["teach"], ["teach", "proj"], ["teach", "proj", "id |"]):
            rc, _, err = self.run_cmd(args)
            self.assertEqual(rc, 2, args)
            self.assertIn("WRITER GATE", err)


class AttestTest(MentorBase):
    def test_payload_is_deterministic_and_tagged(self):
        a = mentor.incept_payload("proj", "id", "steer  text", "t")
        self.assertEqual(a, mentor.incept_payload("proj", "id", "steer text", "t"))
        self.assertRegex(a, r"^ment:b2b:[0-9a-f]{64}$")
        self.assertNotEqual(a, mentor.incept_payload("proj", "id", "other", "t"))

    def test_attest_success_annotates_in_place(self):
        # the native record always lands; the OPTIONAL anchor is down (dead port)
        rc, out, _ = self.teach(extra=["--attest"])
        self.assertEqual(rc, 0)
        self.assertIn("attested (native): record ", out)
        self.assertIn("recorded by 'helm-test'", out)
        e = mentor.taught("proj")[0]
        self.assertTrue(e["attest_record"])
        self.assertEqual(e["attest_chain_index"], "0")
        self.assertEqual(e["attest_by"], "helm-test")
        self.assertNotIn("attest_turn", [k for k in e if e.get(k)])   # never node-signed
        self.assertEqual(e["attest_payload"], mentor.incept_payload(
            "proj", e["id"], e["steer"], e["teacher"]))
        self.assertTrue(premise.verify_chain()[0])
        fired = reflex.fire("parked-human-resolvable-gate", project="proj")
        self.assertEqual(len(fired), 1, "annotation must not break the reflex")

    def test_primitive_raising_degrades_to_not_recorded(self):
        # belt-and-suspenders: if the native primitive raises, the reflex is
        # still live and the payload is named for a backfill
        with mock.patch.object(premise, "record_attestation",
                               side_effect=RuntimeError("boom")):
            rc, out, _ = self.teach(extra=["--attest"])
        self.assertEqual(rc, 0)
        self.assertIn("attestation NOT recorded (RuntimeError: boom)", out)
        self.assertIn("--attest  [payload ment:b2b:", out)
        self.assertEqual(mentor.taught("proj")[0]["attest_payload"], "")
        # a later --attest with the primitive working backfills the record
        with mock.patch.object(sessions, "rows_for", return_value=[]):
            out2 = io.StringIO()
            with contextlib.redirect_stdout(out2):
                rc2 = mentor.cmd_mentor(["teach", "proj",
                                         "parked-human-resolvable-gate", "--attest"])
        self.assertEqual(rc2, 0)
        self.assertIn("attested (native): record ", out2.getvalue())
        self.assertTrue(mentor.taught("proj")[0]["attest_record"])

    def test_backfill_is_idempotent_and_bounded(self):
        self.teach(extra=["--attest"])
        rc, out, _ = self.run_cmd(
            ["teach", "proj", "parked-human-resolvable-gate", "--attest"])
        self.assertEqual(rc, 0)
        self.assertIn("already attested (ment:b2b:", out)
        rc, _, err = self.run_cmd(["teach", "proj", "ghost-id", "--attest"])
        self.assertEqual(rc, 1)
        self.assertIn("nothing taught as 'ghost-id'", err)


class ReviewLogTest(MentorBase):
    def test_review_joins_ledger_fires_with_recurrence(self):
        rid = "parked-human-resolvable-gate"
        self.teach(rid=rid)
        ts = mentor.taught("proj")[0]["stated_ts"]
        self.plant_ledger([self.turn(reflexes=[rid], ts=TS_OLD),
                           self.turn(reflexes=[rid], ts=ts),
                           self.turn(reflexes=[rid], ts="2036-01-01T00:00:00Z"),
                           self.turn(reflexes=["other"], ts="2036-01-01T00:00:00Z")])
        t0 = mentor._epoch(ts)
        rows = [self.row("s1", self.transcript("a", "hit " + rid), mt=t0 - 3600),
                self.row("s2", self.transcript("b", rid + " again"), mt=t0 + 3600),
                self.row("s3", self.transcript("c", "clean"), mt=t0 + 3600)]
        rc, out, _ = self.run_cmd(["review", "proj"], rows=rows)
        self.assertEqual(rc, 0)
        self.assertIn("review proj — 1 taught:", out)
        self.assertIn("fired since taught: 2 turns (inject ledger)", out)
        self.assertIn("1 session before -> 1 after (of 3 scanned", out)
        self.assertIn("fires, not heeds", out)

    def test_review_nothing_taught_points_at_observe(self):
        rc, out, _ = self.run_cmd(["review", "proj"])
        self.assertEqual(rc, 0)
        self.assertIn("nothing taught yet", out)
        self.assertIn("helm mentor observe proj", out)

    def test_log_reads_the_record_untaught_invisible(self):
        self.teach(rid="gate-a", steer="steer a")
        self.reg("proj", "other")
        self.run_cmd(["teach", "other", "gate-b | steer b", "--teacher", "capt"])
        reflex.write({"id": "hand-authored", "steer": "s", "signal": "prompt",
                      "pattern": "x", "stated_ts": TS_OLD}, project="proj")
        self.plant_ledger([self.turn(reflexes=["gate-a"],
                                     ts="2036-01-01T00:00:00Z")])
        rc, out, _ = self.run_cmd(["log"])
        self.assertEqual(rc, 0)
        self.assertIn("log — 2 taught:", out)
        self.assertIn("gate-a", out)
        self.assertIn("by capt", out)
        self.assertIn("fires-since: 1", out)
        self.assertNotIn("hand-authored", out)
        rc, out, _ = self.run_cmd(["log", "--project", "other"])
        self.assertIn("log — 1 taught:", out)
        self.assertNotIn("gate-a", out)

    def test_log_empty(self):
        rc, out, _ = self.run_cmd(["log"])
        self.assertEqual(rc, 0)
        self.assertIn("nothing taught yet", out)


class DispatchTest(MentorBase):
    def test_bare_prints_the_writer_gate(self):
        rc, out, _ = self.run_cmd([])
        self.assertEqual(rc, 0)
        self.assertIn("WRITER GATE", out)
        self.assertIn("v1 same-home", out)

    def test_unknown_subcommand(self):
        rc, _, err = self.run_cmd(["coach"])
        self.assertEqual(rc, 2)
        self.assertIn("unknown subcommand 'coach'", err)


if __name__ == "__main__":
    unittest.main()
