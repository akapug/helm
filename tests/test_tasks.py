#!/usr/bin/env python3
"""helm tasks — the FLEET TASK LEDGER (helm/tasks.py).

Hermetic twice over: every call names its own tmp ledger through the `path=`
seam every function in the module accepts, AND HELM_HOME is redirected at
setUp, so a call that forgets `path=` lands in the temp tree instead of the
live fleet's ~/.helm/_global/tasks.jsonl. A test that writes the real ledger
would file phantom work into the room the fleet reads — it is a bug, not a
mess, and `test_the_default_path_never_leaves_the_temp_home` pins the seam.

Each test below carries one contract from the module docstring: id crosswalk,
continuing numbering, the owner rule and its deliberate asymmetry, append-only
snapshots, the close reason, comments, the offer-rung producer, UNAVAILABLE vs
EMPTY, and numeric (never lexical) ordering.
"""
import contextlib
import io
import json
import os
import shlex
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import home, tasks  # noqa: E402

# A SYNTHETIC 40-hex value — it is not a commit id, and nothing in this repo's
# history resolves it. It is CONSTRUCTED for the two properties that make it
# the right bait for the anchoring arm: it CONTAINS "281" (the substring an
# unanchored number regex answers with) and it STARTS with a digit (what a
# `.match` with the anchors stripped grabs first). Synthetic on purpose —
# tests/ is public-bound and takes fabricated fixtures, and a real prefix
# extended with invented hex reads as verified provenance to the next person,
# who then goes looking for a commit that does not exist. The two assertions
# in the arm below check both properties rather than trusting this comment.
SHA = "9c59742b0f281e4a3d5c6b7a8e9f0a1b2c3d4e5f"

# Scrubbed at every setUp, so an AMBIENT seat identity from the pane that
# launched the run can never decide a verdict here. The CLI arms plant
# HELM_CHAT_NAME themselves and assert what own_name() returns before they
# depend on it — a seat name that arrived by accident is how a test starts
# passing for a reason the test does not state.
ENV_KEYS = ("HELM_HOME", "MELD_HOME", "HELM_CHAT_DIR", "MELD_CHAT_DIR",
            "HELM_CHAT_NAME", "MELD_CHAT_NAME", "HELM_ADOPTED_DIR",
            "MELD_ADOPTED_DIR", "HELM_ACTOR",
            # The SESSION half of identity, which this fixture did not own
            # (the catchup fixture already states the law: a fixture that
            # does not own an input cannot isolate it). It matters now that
            # BOTH the owner and the `source` stamp resolve through
            # dispatches.acting_author, which reads the session id and the
            # roster: unmanaged, the gate RUNNER's real session id leaks into
            # every arm here, and an arm that plants one would leak it into
            # every later test in the file. Both directions are one bug.
            #
            # SPELLED OUT rather than `*home._SESSION_ENV`, which is what I
            # wrote first and what test_env_hygiene reddened the gate over:
            # that scanner is an AST pass that credits a restoration only
            # from a LITERAL in a tuple/list/set, so a splat left the arm
            # below looking like an unrestored leak while the scrub was in
            # fact correct. test_task_posture spells them out for the same
            # reason. A transcribed constant is pinned to nothing, so
            # ENV_KEYS_COVER_THE_SESSION_SEAM below asserts this list is a
            # superset of home._SESSION_ENV — add a var there and the arm
            # goes red instead of the scrub going quietly incomplete.
            "CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID", "CODEX_SESSION_ID")


class TasksBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-tasks-")
        self.path = os.path.join(self.tmp, "tasks.jsonl")
        self.env_prior = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "chat")
        os.environ["HELM_ADOPTED_DIR"] = os.path.join(self.tmp, "adopted")

    def tearDown(self):
        for k, v in self.env_prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def admit(self, seat="seat-a"):
        """An ADMITTED actor for `seat` -> actors.AdmittedActor.

        THE FIXTURE BUILDS THE PRODUCTION PRECONDITION RATHER THAN BYPASSING
        IT, and it cannot do otherwise: `AdmittedActor`'s constructor demands
        a module-private mint token, so the only way a test can hold one is to
        make the real admission pass succeed. That pass wants a DECLARED name
        (HELM_CHAT_NAME) corroborated by a harness session THE ROSTER KNOWS —
        a declared name alone is a value any process can export — so both
        halves are planted here and the resolve is ASSERTED, never assumed. A
        fixture that silently failed to admit would make every arm below
        measure the refusal instead of the write.

        EVERY SESSION KEY, because `home.session_id` reads three and takes the
        first non-empty: planting one and leaving a real runner's value in
        another would resolve the wrong session and read as a DISPUTE."""
        from helm import actors as _actors, pk as _pk, seats as _seats
        session = "sess-for-%s" % seat
        for key in home._SESSION_ENV:
            os.environ[key] = session
        os.environ["HELM_CHAT_NAME"] = seat
        rpath = _seats.roster_path()
        rows = _pk.read_json(rpath, {}) or {}
        rows[seat] = dict(rows.get(seat) or {}, session=session)
        os.makedirs(os.path.dirname(rpath), exist_ok=True)
        _pk.atomic_write(rpath, json.dumps(rows))
        actor, err = _actors.resolve_actor(session, os.getcwd(),
                                           act="rank in a test")
        self.assertIsNone(err, "the fixture did not admit an actor: %s" % err)
        self.assertEqual(seat, actor.canonical_name)
        return actor

    def unidentify(self):
        """Withdraw the WHOLE identity this process could be resolved by.

        DELETING HELM_CHAT_NAME IS HALF AN ANSWER, AND THE HALF THAT SURVIVES
        IS THE ONE THAT RESOLVES. A seat is found by a DECLARED name OR by a
        harness session the roster maps to a seat, so an arm that removes only
        the declaration still has an identity whenever a session is present —
        which is exactly how a fixture seeding one silently contradicted the
        two arms whose subject is having NONE. Both halves go, and the
        precondition below is asserted rather than assumed so a future arm
        cannot inherit a resolvable process while claiming an empty one."""
        os.environ.pop("HELM_CHAT_NAME", None)
        for key in home._SESSION_ENV:
            os.environ.pop(key, None)
        self.assertIsNone(home.session_id(),
                          "a session survived the withdrawal, so this arm is "
                          "not about an identity-less process")

    def scope(self, name="proj-a"):
        """Register `name` at this process's cwd so `current_project()`
        RESOLVES, and return the name.

        THROUGH THE REAL LENS, NEVER A PATCH. `current_project` deliberately
        does not own the cwd→project derivation — `inject._ledger.
        project_for_cwd` does, over the registry — so a fixture that mocked
        the answer would leave the two axes free to disagree about one
        directory, which is the bug the project axis exists to end. Planting a
        registry entry makes the real longest-prefix match succeed, and the
        assertion below is what says the plant worked rather than the arm
        silently measuring the unresolved branch."""
        from helm import registry
        reg = registry.load()
        reg.setdefault("projects", {})[name] = {"path": os.getcwd()}
        registry.save(reg)
        self.assertEqual(name, tasks.current_project(),
                         "the fixture did not register a scope, so this arm "
                         "would measure the unresolved-scope refusal instead")
        return name

    def lines(self, path=None):
        """The RAW ledger file, one parsed object per line — the append-only
        shape, not the projection. rows() would hide exactly what we assert."""
        p = path or self.path
        if not os.path.exists(p):
            return []
        with open(p, encoding="utf-8") as f:
            return [json.loads(l) for l in f if l.strip()]

    def file(self, title, owner=None, **kw):
        """add() that FAILS THE TEST on an error instead of returning None and
        letting the next line blame something unrelated."""
        row, err = tasks.add(title, owner, path=self.path, **kw)
        self.assertIsNone(err, "add(%r) refused: %s" % (title, err))
        return row


class EnvKeysCoverTheSessionSeamTest(unittest.TestCase):
    """The pin on the transcription above. ENV_KEYS must spell the session
    vars as literals (test_env_hygiene's AST scanner credits a restoration
    only from a literal), and a spelled list drifts silently the moment
    home gains a fourth harness. This is the derivation the literals owe:
    it MOVES when its input moves, which is the only thing that makes a
    copied constant safe."""

    def test_ENV_KEYS_scrubs_every_session_var_home_reads(self):
        self.assertTrue(home._SESSION_ENV, "the seam under test is empty")
        self.assertEqual(set(home._SESSION_ENV) - set(ENV_KEYS), set(),
                         "home reads a session var this fixture never "
                         "scrubs, so the gate runner's own session id "
                         "decides verdicts in this file")


class HermeticityTest(TasksBase):
    def test_the_default_path_never_leaves_the_temp_home(self):
        p = tasks.ledger_path()
        self.assertEqual(p, os.path.join(home.global_dir(), "tasks.jsonl"))
        self.assertTrue(p.startswith(self.tmp + os.sep), p)


class IdCrosswalkTest(TasksBase):
    def test_normalize_id_takes_every_spelling_and_refuses_the_rest(self):
        """The crosswalk IS the feature: a week of chat rows saying "#263" has
        to resolve without a lookup table."""
        for token in ("263", "#263", "task/263", "task-263", "TASK/263",
                      "Task-263", " 263 "):
            self.assertEqual(tasks.normalize_id(token), "task/263", token)
        self.assertEqual(tasks.normalize_id("task/work-tab-backlog"),
                         "task/work-tab-backlog")
        for bad in ("", "   ", "abc", "task/", "task-"):
            self.assertIsNone(tasks.normalize_id(bad), bad)

    def test_normalize_id_is_anchored_so_a_sha_is_never_a_task(self):
        """THE anchoring arm, asserted directly. An unanchored pattern reads
        the digits inside a commit id and answers CONFIDENTLY — which is worse
        than answering None, because the caller then resolves a real row for a
        citation that was never about a task."""
        # THE MUST-HIT, on the observable itself and not on the fixture. Every
        # other assertion here is an absence, so a normalize_id that returned
        # None for EVERY input would satisfy all of them and this arm would
        # pass while the feature was entirely dead. The fixture controls below
        # prove SHA is the right bait; only this line proves the trap is armed.
        self.assertEqual(tasks.normalize_id("263"), "task/263")
        self.assertEqual(len(SHA), 40)
        self.assertIn("281", SHA)               # the digits it must not take
        self.assertTrue(SHA[0].isdigit())       # nor the ones it starts with
        self.assertIsNone(tasks.normalize_id(SHA))
        # the same rule from the other side: a number EMBEDDED in prose is not
        # an id either, however much it looks like one.
        self.assertIsNone(tasks.normalize_id("fix 263 before the land"))
        self.assertIsNone(tasks.normalize_id("263abc"))
        self.assertIsNone(tasks.normalize_id("#263 and #45"))


class MintingTest(TasksBase):
    def test_add_continues_the_numbering_and_keeps_a_given_id(self):
        """The sequence the fleet has in its head keeps running: the next
        integer after the HIGHEST, not after the last-written and not len+1."""
        migrated = self.file("the migrated item", "seat-a", tid="263")
        self.assertEqual(migrated["id"], "task/263")
        older = self.file("an older migrated item", "seat-a", tid="#45")
        self.assertEqual(older["id"], "task/45")     # any spelling, same row id
        fresh = self.file("filed today", "seat-a")
        self.assertEqual(fresh["id"], "task/264")    # highest + 1, not 46, not 3
        self.assertEqual(self.file("filed a second later", "seat-a")["id"],
                         "task/265")
        self.assertEqual([l["id"] for l in self.lines()],
                         ["task/263", "task/45", "task/264", "task/265"])

    def test_add_refuses_a_duplicate_id_and_an_empty_title(self):
        self.file("the first one", "seat-a", tid="263")
        dup, err = tasks.add("a second thing behind one citation", "seat-a",
                             tid="#263", path=self.path)
        self.assertIsNone(dup)
        self.assertIn("already exists", err)
        blank, err = tasks.add("   ", "seat-a", path=self.path)
        self.assertIsNone(blank)
        self.assertIn("title", err)
        junk, err = tasks.add("unparseable", "seat-a", tid=SHA, path=self.path)
        self.assertIsNone(junk)
        self.assertIn("unparseable", err)
        # a refusal that still WROTE would be the worst of both
        self.assertEqual([l["id"] for l in self.lines()], ["task/263"])

    def test_in_progress_needs_an_owner_but_open_deliberately_does_not(self):  # noqa: VACUOUS_ASSERTION — the flagged absence is `self.lines() == []` proving the REFUSED add wrote nothing, and no same-observable positive is constructible there: at that point the ledger is legitimately empty, and the rung mints a fresh producer identity per self.lines() call so the later non-empty read is a different observable by its own rule. The arm's positive half is the err channel (assertIn "in_progress with no owner") plus the open-unowned row that follows.
        """Both directions, because the asymmetry is the design (add():167-179):
        work IN PROGRESS by nobody is incoherent, but an OPEN unowned row is
        precisely the POOL that offer_rows() hands an idle seat to take
        without coordinating — refusing it would empty that pool by
        construction."""
        held, err = tasks.add("somebody is on it", "", status="in_progress",
                              path=self.path)
        self.assertIsNone(held)
        self.assertIn("in_progress with no owner", err)
        self.assertEqual(self.lines(), [])           # nothing landed
        free = self.file("nobody has taken this yet", None, status="open")
        self.assertEqual(free["status"], "open")
        self.assertIsNone(free["owner"])
        self.assertEqual([l["id"] for l in self.lines()], [free["id"]])


class SnapshotTest(TasksBase):
    def test_update_appends_a_snapshot_and_rows_returns_only_the_latest(self):
        row = self.file("draft title", "seat-a")
        tid = row["id"]
        first, err = tasks.update(tid, path=self.path, note="reproduced here")
        self.assertIsNone(err)
        second, err = tasks.update(tid, path=self.path, title="the real title")
        self.assertIsNone(err)

        raw = self.lines()
        self.assertEqual(len(raw), 3)                     # never rewritten
        self.assertEqual([l["id"] for l in raw], [tid] * 3)
        self.assertEqual([l["title"] for l in raw],
                         ["draft title", "draft title", "the real title"])
        self.assertEqual([l["note"] for l in raw],
                         [None, "reproduced here", "reproduced here"])

        snap = tasks.rows(path=self.path)
        self.assertEqual(list(snap), [tid])               # one row, not three
        self.assertEqual(snap[tid]["title"], "the real title")
        self.assertEqual(snap[tid]["note"], "reproduced here")  # carries history
        self.assertEqual(snap[tid], second)

    def test_close_refuses_a_silent_close(self):
        row = self.file("the thing", "seat-a")
        for reason in (None, "", "   "):
            silent, err = tasks.close(row["id"], reason, path=self.path)
            self.assertIsNone(silent, reason)
            self.assertIn("reason", err)
        self.assertEqual(len(self.lines()), 1)            # no tombstone written
        closed, err = tasks.close(row["id"], "landed in d6588d3",
                                  path=self.path)
        self.assertIsNone(err)
        self.assertEqual(closed["status"], "closed")
        self.assertEqual(closed["closed_reason"], "landed in d6588d3")
        self.assertEqual(tasks.rows(path=self.path)[row["id"]]["status"],
                         "closed")

    def test_comment_appends_to_comments_and_never_moves_the_status(self):
        row = self.file("needs a second pair of eyes", "seat-a")
        self.assertEqual(row["comments"], [])     # present and empty FROM BIRTH

        one, err = tasks.comment(row["id"], "reproduced on this box", by="kimi",
                                 path=self.path)
        self.assertIsNone(err)
        self.assertEqual([c["text"] for c in one["comments"]],
                         ["reproduced on this box"])
        self.assertEqual(one["comments"][0]["by"], "kimi")
        self.assertEqual(one["status"], "open")   # a comment is not a decision

        two, err = tasks.comment(row["id"], "and on the other one",
                                 path=self.path)
        self.assertIsNone(err)
        self.assertEqual([c["text"] for c in two["comments"]],
                         ["reproduced on this box", "and on the other one"])
        self.assertIsNone(two["comments"][1]["by"])
        latest = tasks.rows(path=self.path)[row["id"]]
        self.assertEqual(latest["status"], "open")
        self.assertEqual(len(latest["comments"]), 2)

        empty, err = tasks.comment(row["id"], "  ", path=self.path)
        self.assertIsNone(empty)
        self.assertIn("empty comment", err)
        self.assertEqual(len(self.lines()), 3)    # add + two comments, no more


class OfferRungTest(TasksBase):
    def legacy_placeholder_row(self, title):
        """A row holding the DISPLAY WORD as its owner, written straight to the
        ledger — because `add()` now refuses to make one, and 18 of these are
        already on disk. Faking it through the door would test the door twice
        and the legacy shape never."""
        row = self.file(title, None)
        raw = dict(row)
        raw["owner"] = tasks.UNOWNED_DISPLAY
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(raw) + "\n")
        # PRECONDITION, asserted not assumed: the ledger really does read back
        # the placeholder, so every assertion below is about the state this
        # test claims to be in.
        stored = {r["id"]: r for r in tasks.open_rows(self.path)}[row["id"]]
        self.assertEqual(stored.get("owner"), tasks.UNOWNED_DISPLAY)
        return row

    def test_the_display_word_is_REFUSED_as_an_owner_at_BOTH_doors(self):
        """`UNOWNED` is what the owner column PRINTS for nobody, and it is also
        a legal seat-name shape — so a caller who reads the column, or this
        file's own prose ("So: UNOWNED unless someone says otherwise"), and
        passes it back as `--owner` is answering the question the vocabulary
        asked. 18 rows on disk did exactly that.

        BOTH doors, per update()'s own docstring: a rule enforced at add() and
        open at update() is the rule deleted."""
        row, err = tasks.add("filed with the display word", "UNOWNED",
                             path=self.path)
        self.assertIsNone(row)
        self.assertIn("PRINTS for an absent owner", err)
        self.assertIn("--owner ''", err)         # the refusal names the path

        live = self.file("a real row", None)
        for spelling in ("UNOWNED", "unowned", "  Unowned  ", "-"):
            with self.subTest(spelling=spelling):
                got, err = tasks.update(live["id"], path=self.path,
                                        owner=spelling)
                self.assertIsNone(got, "update stored %r as an owner"
                                  % spelling)
                self.assertIn("PRINTS for an absent owner", err)
        # UNCONDITIONAL POSITIVE CONTROL on both doors: a real seat name still
        # goes in, so these refusals discriminate rather than rejecting every
        # owner.
        ok = self.file("owned properly", "seat-a")
        self.assertEqual(ok["owner"], "seat-a")
        got, err = tasks.update(live["id"], path=self.path, owner="kimi")
        self.assertIsNone(err, err)
        self.assertEqual(got["owner"], "kimi")

    def test_a_legacy_placeholder_row_is_CLAIMABLE_and_offered_as_FREE(self):
        """The two consequences the 18 rows actually had, both measured.

        (1) tasks.py's incumbent guard read the field raw, so `UNOWNED` was an
        incumbent and every `helm task claim` refused as if somebody held it,
        which taught an unsafe bypass as the normal way to pick up backlog.
        (2) offer_rows() read it raw too, so `others` went True, no live seat
        matched that name, and the row took the STRANDED-RESCUE branch — 18
        unowned rows offered as somebody's abandoned work instead of as the
        free pool the rung exists to fill."""
        stale = self.legacy_placeholder_row("nobody really holds this")

        # (1) CLAIMABLE, with no --force.
        got, err = tasks.update(stale["id"], path=self.path, owner="seat-a")
        self.assertIsNone(err, "the placeholder was treated as an incumbent")
        self.assertEqual(got["owner"], "seat-a")

        # (2) OFFERED AS FREE. A second row, since the first is now owned.
        other = self.legacy_placeholder_row("also nobody")
        free = self.file("a genuinely unowned row", None)
        offers = tasks.offer_rows(path=self.path, seat="seat-a", live=("seat-a",))
        by_id = {o[5]["id"]: o for o in offers}
        # MUST-HIT before any absence assertion: both rows are in the offers
        # at all, or the comparison below is between two things that are not
        # there.
        self.assertIn(other["id"], by_id)
        self.assertIn(free["id"], by_id)
        self.assertNotIn("[assigned:", by_id[other["id"]][1],
                         "a placeholder row was offered as somebody's "
                         "stranded work")
        # and it reads the SAME as a genuinely unowned row, which is the
        # property — not merely "no marker", but indistinguishable from the
        # state it meant.
        self.assertEqual("[assigned:" in by_id[other["id"]][1],
                         "[assigned:" in by_id[free["id"]][1])
        # UNCONDITIONAL POSITIVE CONTROL: a REAL absent owner still gets the
        # marker, so the assertions above are not passing because the marker
        # never appears for anyone.
        held = self.file("stranded on another seat", "kimi")
        marked = {o[5]["id"]: o for o in tasks.offer_rows(
            path=self.path, seat="seat-a", live=("seat-a",))}
        self.assertIn("[assigned:", marked[held["id"]][1])

    def test_the_renderer_still_prints_the_word_for_an_absent_owner(self):
        """The constants must be WIRED, not merely defined. If `_fmt` stopped
        printing UNOWNED the vocabulary collision would be gone by accident
        and this whole cure would be guarding a word nothing says."""
        free = self.file("nobody holds this", None)
        self.assertIn(tasks.UNOWNED_DISPLAY, tasks._fmt(free))
        # and a row that STORES the placeholder renders identically to one
        # that stores nothing — same word, arrived at honestly.
        stale = self.legacy_placeholder_row("legacy")
        stored = {r["id"]: r for r in tasks.open_rows(self.path)}[stale["id"]]
        self.assertIn(tasks.UNOWNED_DISPLAY, tasks._fmt(stored))
        # NEGATIVE CONTROL: an owned row prints its seat, so the assertions
        # above are not true of every row.
        owned = self.file("held", "seat-a")
        self.assertNotIn(tasks.UNOWNED_DISPLAY, tasks._fmt(owned))

    def test_offer_rows_keeps_assigned_rows_offerable_and_says_whose(self):
        """The half that makes it a backlog rather than a list — and the
        contract belongs to the CONSUMER, so it is quoted here rather than
        invented. seats.py:4185-4195: "a named recipient who is not live is the
        STRANDED-WORK case this rung exists to rescue, so excluding it would
        starve the rung. What was wrong is the FRAMING."

        The first version of offer_rows dropped every owned row, which would
        have made a task assigned to a dead seat permanently invisible to the
        one surface that rescues it. Found by reading the consumer, not by any
        test on this side — which is why the assertions below are about the
        rung's shape and not about what felt right here.

        THE SECOND VERSION KEPT THE CONCLUSION AND DROPPED THE ANTECEDENT: the
        quoted sentence says "a named recipient WHO IS NOT LIVE", and the
        consumer spends a whole clause on that (`recip in live: continue`)
        before it ever reaches the marker. Every arm below now names the live
        set, because "is this row offerable" is not answerable without it."""
        free = self.file("nobody holds this one", None)
        held = self.file("stranded on another seat", "kimi")
        mine_row = self.file("assigned to me", "seat-a")
        done = self.file("history", None)
        _closed, err = tasks.close(done["id"], "shipped", path=self.path)
        self.assertIsNone(err)

        # kimi is MEASURED ABSENT from the roster, so its row is stranded.
        offers = tasks.offer_rows(path=self.path, seat="seat-a", live=("seat-a",))
        offered = [o[5]["id"] for o in offers]
        self.assertEqual(len(offers), 3, offers)   # MUST-HIT before any absence
        self.assertIn(held["id"], offered)         # THE RESCUE: still offered…
        self.assertIn(free["id"], offered)
        self.assertIn(mine_row["id"], offered)
        self.assertNotIn(done["id"], offered)      # closed: not work at all

        by_id = {o[5]["id"]: o for o in offers}
        self.assertEqual(len(by_id[free["id"]]), 6)   # seats._offer_rows' shape
        id8, line, claim_cmd, mine, kind, raw = by_id[free["id"]]
        self.assertEqual(id8, free["id"].split("/", 1)[-1])
        self.assertIn("nobody holds this one", line)
        self.assertEqual(claim_cmd, "helm task claim %s" % id8)
        self.assertEqual(kind, "task")
        self.assertEqual(raw["id"], free["id"])

        # …AND SAYS WHOSE, which is the whole difference between a glance and a
        # wasted turn. Three seats in one afternoon each spent a turn
        # discovering a row belonged to someone else.
        self.assertIn("[assigned: kimi]", by_id[held["id"]][1])
        self.assertNotIn("[assigned:", by_id[free["id"]][1])
        self.assertNotIn("[assigned:", by_id[mine_row["id"]][1])  # not from me

        # `mine` is the AUTO-CLAIM signal: true only for my own assigned row.
        self.assertTrue(by_id[mine_row["id"]][3])
        self.assertFalse(by_id[free["id"]][3])     # the pool is never auto-claimed
        self.assertFalse(by_id[held["id"]][3])

        # A resource another idle seat already holds is skipped, in OUR
        # namespace — sharing dispatch's would let a task and a dispatch with
        # the same short id silence each other.
        short = free["id"].split("/", 1)[-1]
        thinned = tasks.offer_rows(path=self.path, seat="seat-a", live=("seat-a",),
                                   claimed={"task:" + short})
        self.assertEqual(len(thinned), 2)
        self.assertNotIn(free["id"], [o[5]["id"] for o in thinned])

    def test_a_live_owners_row_is_not_offered_and_a_dead_owners_row_is(self):
        """THE FOUNDING MEASUREMENT. Against the real ledger this function
        offered 110 rows, 69 of them owned by a seat that was live at that
        instant — 63% of an idle seat's list was work other seats had in hand,
        and task/290 was about to wire it into the fleet's offer rung.

        The two rows below differ in ONE fact — whether their owner is on the
        roster — so an implementation that ignores liveness cannot pass this,
        and one that reverts to dropping every owned row cannot either."""
        pool = self.file("nobody holds this one", None)
        busy = self.file("kimi is building this right now", "kimi")
        dead = self.file("ds4pro went away holding this", "ds4pro")

        offers = tasks.offer_rows(path=self.path, seat="seat-a",
                                  live=("seat-a", "kimi", "codex"))
        offered = [o[5]["id"] for o in offers]
        self.assertIn(pool["id"], offered)      # MUST-HIT before any absence
        self.assertIn(dead["id"], offered)      # the rescue still fires…
        self.assertNotIn(busy["id"], offered)   # …and the poach does not
        self.assertEqual(len(offers), 2, offered)

        by_id = {o[5]["id"]: o for o in offers}
        self.assertIn("[assigned: ds4pro]", by_id[dead["id"]][1])

    def test_an_unreadable_roster_is_not_an_empty_one(self):
        """`live=()` is MEASURED-EMPTY: nobody is live, so every owned row is
        genuinely stranded and all of them are offerable. `live=None` is
        UNREADABLE, and an unreadable roster must never be the reason a seat
        takes another's work.

        These two arms are what make `if live is None` load-bearing: collapse
        it to `if not live` and the measured-empty case silences the rescue at
        exactly the moment the fleet is emptiest and the rescue matters most.
        The pool row is in BOTH expectations because liveness is a fact about
        an OWNER — an unowned row has nobody to be wrong about, which is why
        None fails closed to the owned rows rather than to the whole list."""
        pool = self.file("nobody holds this one", None)
        held = self.file("owned by somebody", "kimi")

        empty = tasks.offer_rows(path=self.path, seat="seat-a", live=())
        self.assertEqual(sorted(o[5]["id"] for o in empty),
                         sorted([pool["id"], held["id"]]))

        unknown = tasks.offer_rows(path=self.path, seat="seat-a", live=None)
        self.assertEqual([o[5]["id"] for o in unknown], [pool["id"]])

        # The default is the unknown one: a caller that never learned to pass a
        # roster gets the pool, never somebody else's row.
        self.assertEqual([o[5]["id"] for o in
                          tasks.offer_rows(path=self.path, seat="seat-a")],
                         [pool["id"]])

    def test_identity_compares_casefolded_and_still_displays_verbatim(self):
        """The exact-tip probe, as a regression arm. The measurement:
        owner="Alpha", asker="beta", live={"alpha"} STILL returned the row
        marked [assigned: Alpha] — because seats._live_seats() returns
        casefolded names by contract while this function compared raw. The
        liveness cure was real and this hole survived inside it, which is why
        every identity below differs from its roster spelling by case alone.

        The display half is asserted too: casefolding the comparison must not
        casefold the NAME, or the rescue marker stops matching what the owner
        typed and a reader has to guess whose row it is."""
        busy = self.file("Alpha is building this", "Alpha")
        own = self.file("mine, spelled the other way", "Beta")
        gone = self.file("Gamma went away holding this", "Gamma")
        pool = self.file("nobody holds this one", None)

        offers = tasks.offer_rows(path=self.path, seat="beta",
                                  live=("alpha", "beta"))
        offered = [o[5]["id"] for o in offers]
        self.assertIn(pool["id"], offered)      # MUST-HIT before any absence
        self.assertIn(own["id"], offered)       # my row, my own liveness
        self.assertIn(gone["id"], offered)      # stranded: not on the roster
        self.assertNotIn(busy["id"], offered)   # THE PROBE: live under a
        self.assertEqual(len(offers), 3, offered)    # different spelling

        by_id = {o[5]["id"]: o for o in offers}
        self.assertTrue(by_id[own["id"]][3])         # mine survives the case
        self.assertNotIn("[assigned:", by_id[own["id"]][1])
        # DISPLAYED VERBATIM: the owner's own spelling, not the fold.
        self.assertIn("[assigned: Gamma]", by_id[gone["id"]][1])

    def test_my_own_row_survives_my_own_liveness(self):
        """The exclusion is `owner != me` FIRST. A seat asking for work while
        it is itself live must still be offered its own assigned row — that is
        the auto-claim signal, and excluding it would make `mine` unreachable
        for every seat that is actually running."""
        mine_row = self.file("assigned to me", "seat-a")
        offers = tasks.offer_rows(path=self.path, seat="seat-a", live=("seat-a",))
        self.assertEqual([o[5]["id"] for o in offers], [mine_row["id"]])
        self.assertTrue(offers[0][3])                     # mine -> auto-claim
        self.assertNotIn("[assigned:", offers[0][1])      # never from me


class UnavailableTest(TasksBase):
    def test_snapshot_reports_UNAVAILABLE_distinctly_from_EMPTY(self):
        """eventledger.checked_events returns (rows, reason): a MISSING file is
        a known-empty ledger (reason None), while an unreadable path is an
        OSError turned into a reason string. Both hand back {} — the second
        element is the only thing that tells a quiet backlog from a blind one,
        so it has to be readable by callers."""
        empty, unavailable = tasks.snapshot(path=self.path)
        # UNCONDITIONAL POSITIVE ON THE SAME PROBE: the tmp dir DOES exist,
        # so the absence asserted next is os.path.exists answering, not a
        # probe that cannot answer anything at all.
        self.assertTrue(os.path.exists(self.tmp))
        self.assertFalse(os.path.exists(self.path))
        self.assertEqual(empty, {})
        self.assertIsNone(unavailable)             # absent = KNOWN empty

        # eventledger._prepare refuses a ledger path that is not a regular
        # file; checked_events turns that OSError into the reason string.
        blocked = os.path.join(self.tmp, "blocked.jsonl")
        os.mkdir(blocked)
        snap, unavailable = tasks.snapshot(path=blocked)
        self.assertEqual(snap, {})                 # same rows…
        self.assertTrue(unavailable)               # …opposite meaning
        self.assertIn("not a regular file", unavailable)

        by_status, why = tasks.counts(path=blocked)
        self.assertEqual(by_status, {})
        self.assertTrue(why)                       # the badge cannot print 0
        self.assertEqual(tasks.counts(path=self.path), ({}, None))

        row, err = tasks.add("would vanish", "seat-a", path=blocked)
        self.assertIsNone(row)                     # never mint into the dark
        self.assertIn("UNKNOWN", err)


class OrderingTest(TasksBase):
    def test_sort_key_orders_numerically_and_puts_slugs_last(self):
        """THE TRAP: as strings "263" < "45". A list sorted lexically reads
        nothing like the numbering the fleet already has in its head."""
        # THE TRAP, stated in the docstring rather than asserted: a literal
        # comparison of two constants proves nothing about production and is
        # the decoration the vacuity rung is built to notice.
        for tid, title in (("263", "two sixty three"), ("45", "forty five"),
                           ("7", "seven"), ("1000", "a thousand")):
            self.file(title, None, tid=tid)
        self.file("the slug one", None, tid="task/work-tab-backlog")
        self.file("another slug", None, tid="task/aaa-first-alphabetically")

        self.assertEqual([r["id"] for r in tasks.open_rows(path=self.path)],
                         ["task/7", "task/45", "task/263", "task/1000",
                          "task/aaa-first-alphabetically",
                          "task/work-tab-backlog"])
        # and on the key itself, so a failure names the comparison
        self.assertLess(tasks.sort_key({"id": "task/45"}),
                        tasks.sort_key({"id": "task/263"}))
        self.assertLess(tasks.sort_key({"id": "task/263"}),
                        tasks.sort_key({"id": "task/aaa-first-alphabetically"}))


# ---------------------------------------------------------------------------
# The six fixes in 72b50ad7 ("fix(tasks): six defects an adversarial reviewer
# found, one of them fatal"). Each arm below pins ONE of them, and each one is
# a rule that a later "simplification" would delete without any other test
# noticing — which is how all six got in.
# ---------------------------------------------------------------------------


class CliBase(TasksBase):
    """The CLI half. cmd_task takes NO path= — it resolves ledger_path() — so
    these arms depend on the redirected HELM_HOME that HermeticityTest pins,
    and they say the seat name out loud instead of inheriting one."""

    def ledger_lines(self):
        """How many events the ledger FILE holds right now.

        A helper rather than an inline read because review measured the
        inline form leaking: `len(open(path).read().splitlines())` never
        closes the handle, and a test that emits ResourceWarning teaches
        every reader who copies it. One door, closed by a context manager,
        and the next arm that needs a line count inherits the fix instead of
        the leak."""
        with open(tasks.ledger_path()) as fh:
            return len(fh.read().splitlines())

    # The seat these arms act as. Assertions below spell "seat-a" as a LITERAL
    # rather than reading it back off this attribute — partly so the expected
    # value is readable where it is asserted, and partly because the
    # vacuous-assertion rung only credits a positive control that pins an
    # observable to a non-empty LITERAL; `assertEqual(row["source"], SEAT)`
    # compares two unknowns as far as the analyzer can see, and left the
    # neighbouring assertIsNone uncovered.
    SEAT = "seat-a"

    def setUp(self):
        super().setUp()
        # NAMED, NOT ADMITTED — and the difference cost a red gate. Admitting
        # here plants a roster row and a session for EVERY arm in this class,
        # which silently gave an identity to the two arms whose whole subject
        # is NOT HAVING ONE: `del os.environ["HELM_CHAT_NAME"]` stopped
        # unresolving a seat, because the session I planted still mapped to it
        # through the roster. A fixture that seeds state contradicts every arm
        # about that state's absence. So the arms that WRITE A RANK call
        # `self.admit()` themselves, and everyone else keeps the world they
        # were written against.
        os.environ["HELM_CHAT_NAME"] = self.SEAT

    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = tasks.cmd_task(list(args))
        return rc, out.getvalue(), err.getvalue()

    def filed(self, title):
        """The row the CLI wrote, found by title in the DEFAULT ledger."""
        got = [r for r in tasks.rows(path=tasks.ledger_path()).values()
               if r.get("title") == title]
        self.assertEqual(len(got), 1, "%r -> %d rows" % (title, len(got)))
        return got[0]


class CommentBodyDoorTest(CliBase):
    """The comment verb's body door: a flag it does not implement must refuse
    rather than silently become the comment."""

    def test_a_flag_this_verb_does_not_implement_refuses(self):
        """AN UNRECOGNISED LEADING FLAG WAS STORED AS THE COMMENT, and the
        verb printed its ordinary success line while the real body — piped,
        heredoc'd, or typed after it — was dropped.

        MEASURED ACROSS THE LEDGER: 24 comments on 15 rows lost their body
        this way, behind three different flags over two months, and every
        author read "noted on task/NNNN". The sibling verb already refuses:
        `task add` names the token it could not read and points at the `--`
        escape. This is that door on the verb that needed it.

        THE BODY IS THE OBSERVABLE, NOT THE EXIT CODE. An arm checking only
        rc would pass against a build that refused for any other reason, so
        this pins that the row gained NO comment."""
        self.cli("add", "carrier", "--mine")
        tid = self.filed("carrier")["id"]
        # THE INSTRUMENT IS PROVEN TO FILL BEFORE ITS SILENCE IS READ. A
        # comment that DOES land gives the absence below something to be an
        # absence OF; without it, "no new comment" is equally satisfied by a
        # verb that can never write one.
        rc, _out, err = self.cli("comment", tid, "a real comment lands")
        self.assertEqual(rc, 0, err)
        seeded = tasks.rows(path=tasks.ledger_path())[tid]["comments"]
        self.assertIn("a real comment lands", seeded[-1]["text"],
                      "the comment door does not write at all, so the "
                      "silence below measures nothing")
        rc, _out, err = self.cli("comment", tid, "--stdin")
        self.assertEqual(rc, 2, "an unimplemented flag was accepted")
        self.assertIn("is not comment text", err)
        after = tasks.rows(path=tasks.ledger_path())[tid]["comments"]
        self.assertEqual(len(after), len(seeded),
                         "the refusal still wrote a comment, so the flag "
                         "reached the body after all: %r" % (after[-1:],))

    def test_the_dash_dash_escape_takes_a_dashed_body_literally(self):
        """THE POSITIVE CONTROL FOR THE ARM ABOVE, and the half that makes the
        refusal usable: a comment whose text genuinely starts with a dash must
        remain writable, or the guard has traded a silent loss for a wall."""
        self.cli("add", "escapee", "--mine")
        tid = self.filed("escapee")["id"]
        rc, _out, err = self.cli("comment", tid, "--",
                                 "--stdin is literal here")
        self.assertEqual(rc, 0, err)
        got = tasks.rows(path=tasks.ledger_path())[tid]["comments"]
        self.assertIn("--stdin is literal here", got[-1]["text"],
                      "the escaped body did not survive as text")


class CliOwnershipTest(CliBase):
    """FIX 1, the fatal one: `add` did `owner or seats.own_name()`, so every
    row a real seat filed came out OWNED — and an owned row is never POOL, so
    the idle-seat rung would have been handed nothing it could take without
    coordinating, forever. The library was always right; the defect lived only
    in its one caller, which is why the twelve library arms were all green
    over it."""

    def test_the_cli_files_UNOWNED_unless_asked_and_mine_takes_it(self):
        from helm import seats
        # THE CONTROL THAT MAKES THE REST MEAN ANYTHING: own_name() has to
        # RETURN something. The old expression could only misfire when it had
        # a name to fall back on, so with no seat name planted an unowned row
        # is what the BROKEN code produced too, and this arm would pass on the
        # defect it exists to catch.
        self.assertEqual(seats.own_name(), "seat-a")

        rc, out, err = self.cli("add", "a", "row", "nobody", "took")
        self.assertEqual(rc, 0, err)
        self.assertIn("filed", out)
        free = self.filed("a row nobody took")
        self.assertEqual(free["source"], "seat-a")        # provenance is KEPT…
        self.assertIsNone(free["owner"])              # …but filing is not owning
        self.assertEqual(free["status"], "open")

        rc, _o, err = self.cli("add", "one", "I", "am", "taking", "--mine")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.filed("one I am taking")["owner"], "seat-a")

        rc, _o, err = self.cli("add", "work", "for", "kimi", "--owner", "kimi")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.filed("work for kimi")["owner"], "kimi")

        # THE PRODUCTION CONSEQUENCE, which is the whole reason this is fatal
        # rather than cosmetic: the idle-seat rung gets a POOL row it can take
        # without coordinating. Under the old CLI default every row came out
        # owned, so `mine=False` never appeared and the pool was empty every
        # time — the rung would have been handed nothing a seat could claim.
        #
        # The assertion is about the POOL, not about the whole offer list.
        # Offering assigned rows is the rung's stranded-work rescue and is
        # pinned in OfferRungTest; asserting an exact whole-list here would
        # re-pin the exclusion that test exists to forbid, which is exactly
        # what this arm did before the contract was corrected.
        # kimi is measured ABSENT from the roster here, so its row reaches the
        # stranded-work rescue. This arm is about POOL vs OWNED and says the
        # live set out loud rather than leaning on a default, because the
        # default is now the fail-closed one and would drop kimi's row for a
        # reason that has nothing to do with what this arm is testing.
        offers = tasks.offer_rows(tasks.ledger_path(), seat="seat-a", live=("seat-a",))
        pool = [o[5]["title"] for o in offers if not (o[5].get("owner") or "")]
        self.assertEqual(pool, ["a row nobody took"])
        # and the two owned rows are still OFFERED, just never as pool
        self.assertEqual(sorted(o[5]["title"] for o in offers),
                         ["a row nobody took", "one I am taking",
                          "work for kimi"])
        # `mine` is the auto-claim signal: true for my row, false for kimi's
        by_title = {o[5]["title"]: o for o in offers}
        self.assertTrue(by_title["one I am taking"][3])
        self.assertFalse(by_title["work for kimi"][3])
        self.assertFalse(by_title["a row nobody took"][3])


class IntIdTest(TasksBase):
    """FIX 2: normalize_id refused the int a migration script obviously holds,
    with an error that told the caller to pass what they had just passed."""

    def test_normalize_id_takes_the_int_a_migration_script_passes(self):
        # ONE observable carrying the hits AND the misses side by side: a
        # normalize_id that answered None to everything fails this line, which
        # a column of assertIsNone could not say.
        self.assertEqual(
            [tasks.normalize_id(t)
             for t in (263, "263", 45, True, False, 263.0, None, b"263")],
            ["task/263", "task/263", "task/45", None, None, None, None, None])
        # bool is an int in Python and is NOT an id. (Honest note: this pair
        # cannot currently FAIL — str(True) is "True", which no pattern
        # matches — so it states the intent of the isinstance(bool) exclusion
        # rather than guarding it.)

        # and end to end, which is the path that actually mattered: filing a
        # historical number from a script that holds ints, not argv strings.
        row = self.file("migrated by script", "seat-a", tid=45)
        self.assertEqual(row["id"], "task/45")
        self.assertEqual(tasks.get(45, path=self.path)["title"],
                         "migrated by script")


class UpdateGuardTest(TasksBase):
    """FIX 3: update() was a second door into the same row that skipped the
    first door's rules — update(title="") landed a titleless row that add()
    refuses, and update(status="closed") landed the silent close that close()
    itself calls "a drop"."""

    def test_update_enforces_the_title_and_reason_rules_of_its_siblings(self):
        row = self.file("the original title", "seat-a")
        tid = row["id"]
        # MUST-HIT: update still WORKS. Every refusal below is free if the
        # verb is simply broken.
        ok, err = tasks.update(tid, path=self.path, title="a better title")
        self.assertIsNone(err)
        self.assertEqual(ok["title"], "a better title")

        empty, err = tasks.update(tid, path=self.path, title="   ")
        self.assertIsNone(empty)
        self.assertIn("title", err)
        gone, err = tasks.update(tid, path=self.path, title=None)
        self.assertIsNone(gone)
        self.assertIn("title", err)
        silent, err = tasks.update(tid, path=self.path, status="closed")
        self.assertIsNone(silent)
        self.assertIn("reason", err)

        # the escape the refusal itself names — a reason passed THROUGH update
        closed, err = tasks.update(tid, path=self.path, status="closed",
                                   closed_reason="landed in 72b50ad7")
        self.assertIsNone(err)
        self.assertEqual(closed["status"], "closed")
        self.assertEqual(closed["closed_reason"], "landed in 72b50ad7")

        # add + the good update + the good close. THREE refusals wrote
        # nothing, which is the half an error string alone would not show.
        self.assertEqual(len(self.lines()), 3)
        self.assertEqual(tasks.rows(path=self.path)[tid]["title"],
                         "a better title")


class CitationScanTest(TasksBase):
    """FIX 4: cited_in matched bare #NNN, so every "closes #130" in this
    repo's own commit subjects resolved as a task citation. Prose scanning is
    now strictly NARROWER than resolve: a human typing `resolve '#130'` has
    declared what they mean and a chat row has not."""

    def test_cited_in_reads_the_explicit_form_and_leaves_github_alone(self):
        # task/130 IS IN THE LEDGER for every assertion below. With no row
        # filed, a scanner that matched everything and a scanner that matched
        # nothing both return [] and the arm proves neither.
        self.file("the row every citation points at", None, tid="130")
        self.file("a second filed row", None, tid="45")

        # Each case is ONE call returning a NON-EMPTY list holding the hit and
        # excluding the miss, so no assertion here is satisfiable by silence.
        # The numbers differ across the two forms on purpose: with the same
        # number, dedup would hide a scanner that matched both.
        self.assertEqual(
            [r["id"] for r in
             tasks.cited_in("closes #130 by landing task/45", path=self.path)],
            ["task/45"])
        self.assertEqual(
            [r["id"] for r in
             tasks.cited_in("PR #45 on github, tracked as task-130",
                            path=self.path)],
            ["task/130"])
        # case-insensitive, deduped, in encounter order
        self.assertEqual(
            [r["id"] for r in
             tasks.cited_in("TASK/130 again task/130, plus task-45",
                            path=self.path)],
            ["task/130", "task/45"])
        # an id nobody filed is never invented into a row
        self.assertEqual(
            [r["id"] for r in
             tasks.cited_in("task/999 and task/45", path=self.path)],
            ["task/45"])
        # …while `resolve` stays WIDER on purpose. The two are allowed to
        # disagree about "#130"; that disagreement IS the fix.
        self.assertEqual(tasks.normalize_id("#130"), "task/130")


class SlugIdTest(TasksBase):
    """FIX 5: an all-digit slug longer than six digits filed through the slug
    pattern and was then unresolvable by its own number forever — a ghost id,
    which is the one thing this id scheme exists to prevent."""

    def test_an_all_digit_slug_is_refused_so_no_id_outlives_its_number(self):
        ghost = "task/" + "1" * 7        # seven digits: past _NUM_ID's six
        # One observable again: the accepted spellings and the refused one in
        # a single list, so a normalize_id that refused EVERYTHING fails here.
        self.assertEqual(
            [tasks.normalize_id(t) for t in
             ("task/work-tab-backlog", "task/2fa-rollout", "task/" + "1" * 6,
              "1" * 6, ghost, "1" * 7)],
            ["task/work-tab-backlog", "task/2fa-rollout", "task/111111",
             "task/111111", None, None])

        # a digit-LEADING slug is still a slug — the refusal is for all-digit
        # ids, not for digits, and a lookahead written `(?!\d)` would take
        # task/2fa-rollout down with the ghost.
        kept = self.file("two factor rollout", None, tid="task/2fa-rollout")
        self.assertEqual(kept["id"], "task/2fa-rollout")

        row, err = tasks.add("a ghost id", "seat-a", tid=ghost, path=self.path)
        self.assertIsNone(row)
        self.assertIn("unparseable", err)
        self.assertEqual([l["id"] for l in self.lines()], ["task/2fa-rollout"])


class ClaimIncumbentTest(CliBase):
    """FIX 6: claim silently STOLE a live holder's row. Two seats on one row
    is the failure the incumbent guard exists to prevent — and it lives in
    update(), not here, because claim was only ONE door into it and `update
    --owner` walked straight past the version that sat in the CLI."""

    def test_claim_refuses_a_row_another_seat_holds_unless_forced(self):
        # MUST-HIT: claiming an UNOWNED row WORKS. Without this the refusal
        # below is indistinguishable from a claim verb that refuses always.
        rc, _o, err = self.cli("add", "free", "work")
        self.assertEqual(rc, 0, err)
        rc, out, err = self.cli("claim", self.filed("free work")["id"])
        self.assertEqual(rc, 0, err)
        taken = self.filed("free work")
        self.assertEqual(taken["owner"], "seat-a")
        self.assertEqual(taken["status"], "in_progress")

        rc, _o, err = self.cli("add", "kimi's work", "--owner", "kimi")
        self.assertEqual(rc, 0, err)
        held = self.filed("kimi's work")
        rc, _o, err = self.cli("claim", held["id"])
        self.assertEqual(rc, 2)
        self.assertIn("kimi", err)            # NAMES the incumbent…
        self.assertIn("takeover", err)         # …and names the evidence door
        # the row is UNTOUCHED, which the exit code alone would not show
        self.assertEqual(self.filed("kimi's work")["owner"], "kimi")
        self.assertEqual(self.filed("kimi's work")["status"], "open")

        # re-claiming what I ALREADY hold is not a steal and needs no bypass
        rc, _o, err = self.cli("claim", taken["id"])
        self.assertEqual(rc, 0, err)

        rc, _o, err = self.cli("claim", held["id"], "--force")
        self.assertEqual(rc, 2)
        self.assertIn("no incumbent-owner authority", err)
        self.assertEqual(self.filed("kimi's work")["owner"], "kimi")


if __name__ == "__main__":
    unittest.main()


class ClosedRowsStayClosedTest(TasksBase):
    """task/345 — a live repro, and it is a two-field contradiction
    rather than a wrong value: `helm task claim 172` on a CLOSED row returned
    status=in_progress owner=codex-3 WHILE RETAINING closed_reason. One row
    claiming to be live work and carrying the tombstone explaining why it is
    not. Every consumer picks one of those fields and is wrong whenever it
    picks the other."""

    def closed_row(self, title="already finished"):
        row, err = tasks.add(title, "seat-a", path=self.path)
        self.assertIsNone(err, err)
        done, err = tasks.update(row["id"], path=self.path, status="closed",
                                 closed_reason="landed at abc1234")
        self.assertIsNone(err, err)
        self.assertEqual(done["status"], "closed")     # MUST-HIT
        return done

    def rowcount(self):
        rows, unavailable = tasks.snapshot(path=self.path)
        self.assertIsNone(unavailable, unavailable)
        return len(rows)

    def events(self):
        """LEDGER LINES, not projected rows — the finding, and it is
        the difference between asserting the refusal and asserting nothing.
        This store is EVENT-SOURCED: appending another snapshot of the same
        row changes neither the projected values nor the row COUNT, so a
        `refusal` that wrote a duplicate event would satisfy every arm I had
        written. The file's line count is the only observable that moves."""
        try:
            with open(self.path, encoding="utf-8") as fh:
                return sum(1 for line in fh if line.strip())
        except FileNotFoundError:
            return 0

    def test_a_CLOSED_row_cannot_be_claimed_back_to_life(self):
        done = self.closed_row()
        before = self.events()
        self.assertGreater(before, 0)      # the ledger really has events
        row, err = tasks.update(done["id"], path=self.path,
                                status="in_progress", owner="codex-3")
        self.assertIsNone(row)
        # NOT ONE EVENT WRITTEN. An event-sourced store that appended a
        # rejected snapshot would leave the contradiction on disk for the
        # next projection, and every value-based assertion below would still
        # pass because latest-wins hides it.
        self.assertEqual(self.events(), before)
        self.assertIn("CLOSED", err)
        self.assertIn("landed at abc1234", err)   # the refusal SAYS why
        # THE RECORD IS UNTOUCHED, not merely the return value: an
        # event-sourced ledger that appended a rejected snapshot would leave
        # the contradiction on disk for the next reader to project.
        still, _u = tasks.snapshot(path=self.path)
        self.assertEqual(still[done["id"]]["status"], "closed")
        self.assertEqual(still[done["id"]]["owner"], "seat-a")
        self.assertEqual(still[done["id"]]["closed_reason"], "landed at abc1234")

    def test_force_does_NOT_buy_a_resurrection(self):
        """Raw force authorizes no owner mutation and cannot reopen history.

        A closed row also has no incumbent takeover can transfer, so the
        resurrection invariant remains independent of the new BUILD gate.
        """
        done = self.closed_row()
        before = self.events()
        self.assertGreater(before, 0)
        row, err = tasks.update(done["id"], path=self.path, force=True,
                                status="open", owner="codex-3")
        self.assertIsNone(row)
        # THE LIBRARY SPEAKS LAYER-NEUTRALLY. An error naming `--force` tells
        # an API caller to pass a flag it has no way to pass; the CLI adds
        # its own spellings in _cli_error. Pinned in both directions.
        self.assertIn("forcing does not override", err)
        self.assertNotIn("--force", err)
        self.assertEqual(self.events(), before)

    def test_metadata_edits_that_STAY_closed_are_still_legal(self):
        """THE CONTROL THAT KEEPS THE GUARD FROM BEING TOO BROAD: retitling a
        tombstone is task/294's entire job, and 172 reasonless tombstones
        exist to be upgraded in place. The invariant is about the TRANSITION
        OUT of closed, never the resting state."""
        done = self.closed_row(title="vague old title")
        row, err = tasks.update(done["id"], path=self.path,
                                title="a title that says what it was")
        self.assertIsNone(err, err)
        self.assertEqual(row["title"], "a title that says what it was")
        self.assertEqual(row["status"], "closed")

    def test_an_OPEN_unowned_row_still_claims_normally(self):
        """The positive control on the whole path: a sweep that refused
        everything would pass every arm above."""
        # an UNOWNED tombstone is legal (that is add()'s stated exception),
        # and as of the born-closed guard it still needs a reason like any
        # other close. My own new invariant broke my own older fixture here,
        # which is the guard doing its job on the first row that tried it.
        row, err = tasks.add("live work", "", path=self.path, status="closed",
                             closed_reason="retired before the ledger")
        self.assertIsNone(err, err)
        fresh, err = tasks.add("genuinely open", "", path=self.path)
        self.assertIsNone(err, err)
        got, err = tasks.update(fresh["id"], path=self.path,
                                status="in_progress", owner="seat-b")
        self.assertIsNone(err, err)
        self.assertEqual(got["status"], "in_progress")
        self.assertEqual(got["owner"], "seat-b")


class BornClosedNeedsAReasonTest(TasksBase):
    """Found in a MELD after four async rounds missed it —
    and it is my own law one invariant over. update() refuses a close with no
    reason ("a silent close is a drop"); add() accepted a row BORN closed with
    closed_reason None, so the identical end state was reachable through the
    other door. The comment I wrote in update() says it exactly: "a closed set
    enforced at add() and open at update() is not a closed set."

    Invisible to the five task/345 arms BY CONSTRUCTION — no single-line
    deletion survives them, so the suite was not weak, it was aimed at the
    other door."""

    def test_a_row_BORN_closed_still_needs_a_reason(self):
        row, err = tasks.add("born a tombstone", "seat-a", status="closed",
                             path=self.path)
        self.assertIsNone(row)
        self.assertIn("still needs a reason", err)

    def test_a_tombstone_WITH_a_reason_files_normally(self):
        """The other polarity, and the control: the guard must not make
        tombstones unfileable — they are how a pre-ledger citation resolves
        to a sentence instead of nothing."""
        row, err = tasks.add("a real tombstone", "seat-a", status="closed",
                             closed_reason="retired before the ledger existed",
                             path=self.path)
        self.assertIsNone(err, err)
        self.assertEqual(row["status"], "closed")
        self.assertEqual(row["closed_reason"],
                         "retired before the ledger existed")

    def test_BOTH_DOORS_refuse_the_same_end_state(self):
        """The invariant stated as one fact rather than two: whichever door
        you come through, a closed row without a reason is refused."""
        born, err_add = tasks.add("via add", "seat-a", status="closed",
                                  path=self.path)
        live, err = tasks.add("via update", "seat-a", path=self.path)
        self.assertIsNone(err, err)
        closed, err_update = tasks.update(live["id"], path=self.path,
                                          status="closed")
        self.assertIsNone(born)
        self.assertIsNone(closed)
        self.assertIsNotNone(err_add)
        self.assertIsNotNone(err_update)
        # UNCONDITIONAL POSITIVE CONTROL ON BOTH DOORS: each ACCEPTS the same
        # end state when the reason is supplied, so the refusals above are
        # about the missing reason and not about closed rows being unreachable.
        ok_add, err = tasks.add("via add, with a reason", "seat-a",
                                status="closed", closed_reason="retired",
                                path=self.path)
        self.assertIsNone(err, err)
        self.assertEqual(ok_add["status"], "closed")
        live2, err = tasks.add("via update, with a reason", "seat-a",
                               path=self.path)
        self.assertIsNone(err, err)
        ok_upd, err = tasks.update(live2["id"], path=self.path,
                                   status="closed", closed_reason="done")
        self.assertIsNone(err, err)
        self.assertEqual(ok_upd["status"], "closed")

    def test_STATUSES_is_pinned_EXACT_as_a_tripwire(self):
        """The meld's answer, and it beats both options I was weighing.
        The resurrection guard reads `prev.status == "closed"` LITERALLY.
        Closed is the only terminal today, so a one-member TERMINAL set would
        be ceremony — but if a fourth status ever lands, that guard silently
        stops covering it and nothing says so. This pin REDDENS on any change
        to the vocabulary and forces its author through exactly that question.
        If you are here because this test failed: decide whether your new
        status is TERMINAL, and if it is, teach helm/tasks.py update()'s
        resurrection guard about it before changing this line."""
        self.assertEqual(tasks.STATUSES, ("open", "in_progress", "closed"))


class ClosedRowClaimCliTest(CliBase):
    """The repro exactly as typed. `claim` passes
    status=in_progress into update() and has no terminal check of its own —
    which is CORRECT: a guard on one door is a guard the next door walks
    around, and this codebase has the scar (`update --owner thief`)."""

    def test_claiming_a_CLOSED_row_is_refused_and_appends_NOTHING(self):
        rc, out, _err = self.cli("add", "already finished", "--owner", "seat-a")
        self.assertEqual(rc, 0, out)
        row = self.filed("already finished")
        rc, out, _err = self.cli("close", row["id"], "landed at abc1234")
        self.assertEqual(rc, 0, out)
        before, unavailable = tasks.snapshot()
        self.assertIsNone(unavailable, unavailable)
        self.assertEqual(before[row["id"]]["status"], "closed")   # MUST-HIT

        # RAW LEDGER LINES, not projected ids (and it is the SAME
        # vacuity the library arms had): this store is event-sourced, so an
        # appended duplicate snapshot changes neither the projected values nor
        # the id count. I fixed the library arms and left this one counting
        # ids, which is the identical hole one layer up.
        events = self.eventcount()
        self.assertGreater(events, 0)
        rc, out, err = self.cli("claim", row["id"], "--owner", "codex-3")
        self.assertEqual(rc, 2, out)
        self.assertIn("CLOSED", err)
        # THE CLI REMEDY BRANCH IS PINNED (deleting it left all five
        # arms green, so it was decoration). Two halves, and the second is the
        # layer rule: the CLI names the command a person types, and the
        # LIBRARY error must not — update() is called by the API too, and an
        # error naming --force tells a caller to pass a flag it cannot pass.
        self.assertIn("--ref", err)
        self.assertNotIn("--force", err)
        after, _u = tasks.snapshot()
        self.assertEqual(after[row["id"]]["status"], "closed")
        self.assertEqual(after[row["id"]]["closed_reason"], "landed at abc1234")
        self.assertEqual(len(after), len(before))
        self.assertEqual(self.eventcount(), events)

    def eventcount(self):
        try:
            with open(tasks.ledger_path(), encoding="utf-8") as fh:
                return sum(1 for line in fh if line.strip())
        except FileNotFoundError:
            return 0


class OriginWriteSurfacesDoNotDivergeTest(CliBase):
    """The read normalizer cured replay while THREE write and
    display surfaces still diverged from it. A closed set that only one door
    respects is the same defect this lane was opened for, one layer over."""

    LEGACY = "corpus-2026-08-05"

    def test_show_says_UNKNOWN_and_names_what_is_RECORDED(self):
        """`show` printed the raw field, so 251 rows displayed
        `corpus-2026-08-05` as a VALUE on a surface whose whole claim is a
        closed set. It cannot be written through the doors any more, so the
        fixture appends the event directly — which is exactly how those rows
        got there."""
        rc, out, _err = self.cli("add", "a migrated row", "--owner", "seat-a")
        self.assertEqual(rc, 0, out)
        row = dict(self.filed("a migrated row"))
        row["origin"] = self.LEGACY
        with open(tasks.ledger_path(), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        # POSITIVE ON THE SAME READ: `tasks.get(row["id"])` must hand back
        # the row we just appended, proven by a field with a literal value.
        # Without it the origin comparison below is a name against an
        # ATTRIBUTE — satisfiable by two Nones — and a get() that returned
        # the wrong row, or a stub, would satisfy it just as well.
        self.assertEqual(tasks.get(row["id"])["title"], "a migrated row")
        self.assertEqual(tasks.get(row["id"])["origin"], self.LEGACY)

        rc, out, _err = self.cli("show", row["id"])
        self.assertEqual(rc, 0)
        # what it MEANS, and what is RECORDED — both, and the raw value is
        # never presented as the field's value
        self.assertIn("UNKNOWN (recorded: %s)" % self.LEGACY, out)
        self.assertNotIn("origin         %s" % self.LEGACY, out)

    def test_show_prints_a_DECLARED_origin_plainly(self):
        """The unconditional positive control on the same observable: a real
        value renders as itself, with no UNKNOWN wrapper."""
        rc, out, _err = self.cli("add", "he asked", "--owner", "seat-a",
                                 "--owner-asked")
        self.assertEqual(rc, 0, out)
        row = self.filed("he asked")
        rc, out, _err = self.cli("show", row["id"])
        self.assertEqual(rc, 0)
        self.assertIn("owner", out)
        self.assertNotIn("UNKNOWN", out)

    def test_the_CLI_can_WRITE_origin_instead_of_silently_ignoring_it(self):
        """`helm task update 1 --note changed --origin owner` reported success
        while silently retaining the old value, because --origin was never in
        the flag table and the leftover was dropped."""
        rc, out, _err = self.cli("add", "a row", "--owner", "seat-a")
        self.assertEqual(rc, 0, out)
        row = self.filed("a row")
        rc, out, err = self.cli("update", row["id"], "--origin", "owner")
        self.assertEqual(rc, 0, err)
        again = tasks.get(row["id"])
        self.assertEqual(again["origin"], "owner")

    def test_the_CLI_refuses_an_origin_outside_the_closed_set(self):
        rc, out, _err = self.cli("add", "a row", "--owner", "seat-a")
        self.assertEqual(rc, 0, out)
        row = self.filed("a row")
        # a seat-filed row is legitimately stamped `agent` — that IS the
        # witnessed provenance — so the claim is that the refusal left it
        # UNCHANGED, not that it is empty.
        self.assertEqual(row["origin"], "agent")
        rc, _out, err = self.cli("update", row["id"], "--origin", self.LEGACY)
        self.assertEqual(rc, 2)
        self.assertIn("unknown origin", err)
        self.assertEqual(tasks.get(row["id"])["origin"], "agent")

    def test_origin_CANNOT_be_cleared_once_witnessed(self):
        """The tri-state is NOT symmetric. UNKNOWN is what an UNWITNESSED row
        reads; un-witnessing one somebody did witness is the erasure this
        field exists to end, performed through the correction door."""
        rc, out, _err = self.cli("add", "he asked", "--owner", "seat-a",
                                 "--owner-asked")
        self.assertEqual(rc, 0, out)
        row = self.filed("he asked")
        self.assertEqual(row["origin"], "owner")     # positive control
        _r, err = tasks.update(row["id"], origin=None)
        self.assertIsNotNone(err)
        self.assertIn("cannot be cleared", err)
        self.assertEqual(tasks.get(row["id"])["origin"], "owner")

    def test_the_PUBLIC_help_names_the_flag_that_writes_it(self):
        """FINDABLE, in the ladder's terms: a flag the authoritative help
        omits is a flag nobody can discover. `--owner-asked` was reachable
        only by reading the source."""
        from helm import cli
        self.assertIn("--owner-asked", cli._VERB_HELP["task"])
        self.assertIn("--origin", cli._VERB_HELP["task"])


class OneSerializationDoorTest(CliBase):
    """ROUND 4 on the same defect, which is the signal to change the SHAPE
    rather than patch again. Rounds 1-3 cured the wire, the glyph and `show`
    by routing each reader through origin_of; review then found the JSON paths
    still emitting raw rows. Every round I fixed the readers I could find and
    a new one appeared. `public_row` is now the ONE serialization door, so a
    new consumer is correct by DEFAULT rather than correct by remembering."""

    LEGACY = "corpus-2026-08-05"

    def legacy_row(self, title):
        rc, out, _err = self.cli("add", title, "--owner", "seat-a")
        self.assertEqual(rc, 0, out)
        row = dict(self.filed(title))
        row["origin"] = self.LEGACY
        with open(tasks.ledger_path(), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        self.assertEqual(tasks.get(row["id"])["origin"], self.LEGACY)
        return row

    def test_show_json_normalizes_and_KEEPS_the_record(self):
        row = self.legacy_row("a migrated row")
        rc, out, _err = self.cli("show", row["id"], "--json")
        self.assertEqual(rc, 0)
        got = json.loads(out)
        self.assertIsNone(got["origin"])
        self.assertEqual(got["origin_recorded"], self.LEGACY)
        # UNCONDITIONAL POSITIVE CONTROL ON THE SAME KEY, same command: a
        # declared row's `origin` DOES cross this door, so the None above
        # means normalized and not "this serializer drops the field".
        rc, out2, _e = self.cli("add", "he asked", "--owner", "seat-a",
                                "--owner-asked")
        self.assertEqual(rc, 0, out2)
        other = self.filed("he asked")
        _rc, out3, _e = self.cli("show", other["id"], "--json")
        self.assertEqual(json.loads(out3)["origin"], "owner")

    def test_list_json_goes_through_the_SAME_door(self):
        """This path built its own json.dumps and so emitted raw rows while
        `show` next door was correct — the exact divergence a single door
        removes."""
        self.legacy_row("a migrated row")
        rc, out, _err = self.cli("list", "--json")
        self.assertEqual(rc, 0)
        rows = json.loads(out)
        self.assertTrue(rows)                      # positive control
        for r in rows:
            with self.subTest(row=r.get("id")):
                self.assertIn(r.get("origin"), (None, "owner", "agent"))
        # the legacy string appears ONLY as the audit key's value, never as
        # the field's — and it must still appear, or normalizing would have
        # become deletion.
        migrated = [r for r in rows if r.get("origin_recorded")]
        self.assertEqual(len(migrated), 1)
        self.assertEqual(migrated[0]["origin_recorded"], self.LEGACY)
        self.assertIsNone(migrated[0]["origin"])

    def test_a_DECLARED_origin_serializes_plainly_with_no_extra_key(self):
        """The control: the audit key appears ONLY when the two differ, so it
        is a signal rather than noise on every row."""
        rc, out, _err = self.cli("add", "he asked", "--owner", "seat-a",
                                 "--owner-asked")
        self.assertEqual(rc, 0, out)
        row = self.filed("he asked")
        rc, out, _err = self.cli("show", row["id"], "--json")
        got = json.loads(out)
        self.assertEqual(got["origin"], "owner")
        self.assertNotIn("origin_recorded", got)

    def test_update_REFUSES_an_unconsumed_flag_instead_of_reporting_success(self):
        """`--origin owner` returned SUCCESS while retaining the old value,
        because the leftover was dropped. This verb's contract is that what
        you asked for happened."""
        rc, out, _err = self.cli("add", "a row", "--owner", "seat-a")
        self.assertEqual(rc, 0, out)
        row = self.filed("a row")
        # POSITIVE CONTROL FIRST, on the same field: update CAN write a note,
        # so "the note is unchanged" below is a refusal and not a no-op verb.
        _r, err = tasks.update(row["id"], note="a real change")
        self.assertIsNone(err, err)
        self.assertEqual(tasks.get(row["id"])["note"], "a real change")

        # COUNT THE LEDGER, NOT THE VALUE. This asserted only that the note
        # still READ "a real change" — and in an append-only store that
        # cannot tell A REFUSED WRITE from AN APPENDED EVENT THAT HAPPENS TO
        # SET THE SAME VALUE. Both leave the projection identical; only one
        # leaves the file identical. The contract this arm defends is that
        # NOTHING WAS WRITTEN, so the file is what has to be measured.
        #
        # LOAD-BEARING MUTATION (the standing default — name the one
        # mutation that must red this arm, and the command that proves it):
        #   in helm/tasks.py, at the leftover-flag refusal that precedes
        #   `row, err = update(token, ...)`, append the SAME value before
        #   returning 2:
        #       update(token, note="a real change")
        #       return 2
        #   python3 -m unittest tests.test_tasks.OneSerializationDoorTest \
        #       .test_update_REFUSES_an_unconsumed_flag_instead_of_reporting_success
        #   -> AssertionError: 3 != 2 : a refused update appended 1 event(s)
        # rc STAYS 2 and the projection STAYS correct under that mutation, so
        # the ledger count is the only assertion here that can see it. A
        # mutation writing a DIFFERENT value is NOT the right one: it reds the
        # pre-existing value assertion instead and proves nothing about this
        # clause.
        before = self.ledger_lines()
        self.assertGreater(before, 0)          # control: the ledger has rows
        rc, _out, err = self.cli("update", row["id"], "--note", "changed",
                                 "--nosuchflag", "x")
        self.assertEqual(rc, 2)
        self.assertIn("--nosuchflag", err)
        self.assertEqual(tasks.get(row["id"])["note"], "a real change")
        after = self.ledger_lines()
        self.assertEqual(after, before,
                         "a refused update appended %d event(s) — rc 2 and an "
                         "unchanged projection are both satisfied by a write "
                         "that set the same value" % (after - before))

    def test_a_VALUELESS_origin_at_the_end_is_refused_too(self):
        rc, out, _err = self.cli("add", "a row", "--owner", "seat-a")
        self.assertEqual(rc, 0, out)
        row = self.filed("a row")
        rc, _out, err = self.cli("update", row["id"], "--note", "n", "--origin")
        self.assertEqual(rc, 2)
        self.assertIn("--origin", err)


class LegacyOriginIsNormalizedOnReadTest(TasksBase):
    """It refuted the premise the whole design rested on.

    I claimed the field was a tri-state and that legacy rows "read UNKNOWN".
    MEASURED ON THE REAL LEDGER: 251 rows carry `corpus-2026-08-05`, 73 of
    them LIVE, and ZERO carry None-as-legacy the way my fixtures did. So the
    field had FOUR states in production while every surface assumed three,
    and my tests could not see it because they invented the legacy value
    instead of using the one that exists. A validation that fires only on
    WRITES says nothing about rows that were already there.

    THE LITERAL STRING IS USED ON PURPOSE in these arms. A fixture that says
    "some unknown value" would pass against a normalizer keyed to anything;
    this pins the one the ledger actually holds."""

    LEGACY = "corpus-2026-08-05"

    def test_the_REAL_migration_tag_reads_UNKNOWN_not_itself(self):
        row, err = tasks.add("a migrated row", "seat-a", path=self.path)
        self.assertIsNone(err, err)
        row["origin"] = self.LEGACY          # as the migration left it
        self.assertEqual(tasks.origin_of(row), None)
        # and the RECORD is untouched — normalizing is a read, not a rewrite
        self.assertEqual(row["origin"], self.LEGACY)
        # UNCONDITIONAL POSITIVE CONTROL ON THE SAME OBSERVABLE: origin_of
        # CAN return a value, so the None above means "normalized" and not
        # "this function returns None for everything".
        row["origin"] = "owner"
        self.assertEqual(tasks.origin_of(row), "owner")

    def test_the_two_declared_values_pass_through_unchanged(self):  # noqa: VACUOUS_ASSERTION — every arm is positive: each declared value is asserted to come back AS ITSELF, a non-empty literal; there is no absence assertion in it
        """The positive control. A normalizer that returned None for
        everything would satisfy the arm above."""
        for declared in tasks.ORIGINS:
            with self.subTest(origin=declared):
                self.assertEqual(tasks.origin_of({"origin": declared}),
                                 declared)

    def test_an_ABSENT_field_and_a_LEGACY_tag_answer_the_SAME(self):
        """They are the same situation — a provenance nobody witnessed as
        owner-or-agent — so a surface that distinguished them would be
        inventing a difference the record does not carry."""
        # BOTH SIDES ARE None, which is the classic vacuous shape — so the
        # control comes first and is unconditional: this comparison is only
        # meaningful because origin_of DISTINGUISHES a declared value from
        # both of them.
        self.assertEqual(tasks.origin_of({"origin": "agent"}), "agent")
        self.assertIsNone(tasks.origin_of({}))
        self.assertEqual(tasks.origin_of({}),
                         tasks.origin_of({"origin": self.LEGACY}))

    def test_the_glyph_does_not_mark_a_migrated_row(self):
        row, err = tasks.add("a migrated row", "seat-a", path=self.path)
        self.assertIsNone(err, err)
        row["origin"] = self.LEGACY
        self.assertNotIn("@", tasks._fmt(row))
        # unconditional positive control on the same observable
        row["origin"] = "owner"
        self.assertIn("@", tasks._fmt(row))


class LeadingFlagIsNotATitleTest(CliBase):
    """task/335 is a real ledger row TITLED `--help`, filed by an agent that
    wanted the usage line and then disclosed and closed by hand. Every flag in
    the add leg is consumed BY NAME, so an unrecognised one falls through into
    the title join and becomes the row's permanent name."""

    def rowcount(self):
        rows, unavailable = tasks.snapshot()
        self.assertIsNone(unavailable, unavailable)
        return len(rows)

    def files_one(self, *argv):
        """UNCONDITIONAL POSITIVE CONTROL ON THE COUNTER ITSELF. "the count did
        not move" is worthless unless the count CAN move — a broken add leg
        would satisfy every refusal arm below by filing nothing, ever."""
        was = self.rowcount()
        rc, out, _err = self.cli("add", *argv)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.rowcount(), was + 1)

    def test_help_prints_usage_and_files_NOTHING(self):  # noqa: VACUOUS_ASSERTION — files_one() is the unconditional positive control ON THE SAME OBSERVABLE — it files a real row and asserts the counter moved BEFORE this arm asserts it did not
        self.files_one("a real row, so the counter is proven live",
                       "--owner", "seat-a")
        before = self.rowcount()
        rc, _out, err = self.cli("add", "--help")
        self.assertEqual(rc, 2)
        self.assertIn("not a title", err)
        self.assertEqual(self.rowcount(), before)

    def test_any_unrecognised_leading_flag_is_refused_and_NAMED(self):  # noqa: VACUOUS_ASSERTION — same: files_one() proves the counter can move, and the error string is asserted to CONTAIN the offending token, not merely to be non-empty
        self.files_one("another real row", "--owner", "seat-a")
        before = self.rowcount()
        rc, _out, err = self.cli("add", "--piority", "p0", "fix the thing")
        self.assertEqual(rc, 2)
        self.assertIn("--piority", err)      # it names the typo back
        self.assertEqual(self.rowcount(), before)

    def test_a_RECOGNISED_flag_may_lead_and_still_files(self):  # noqa: VACUOUS_ASSERTION — every arm here is positive — rc 0 plus a counter that INCREMENTS for each of five flags; there is no absence assertion in it
        """The exact probe, and the defect that made this its own lane.
        My first cut ran the guard BEFORE --owner-asked was consumed, so this
        valid command returned rc2 while the SAME WORDS title-first returned
        rc0. A guard that runs before its inputs are complete does not protect
        the door, it breaks it. Every recognised leading flag is covered, not
        just the one that caught me."""
        for argv in (["--owner-asked", "he asked for this"],
                     ["--owner", "seat-a", "an owned row"],
                     ["--mine", "a row I take"],
                     ["--note", "context here", "a noted row"],
                     ["--ref", "#218", "a row with a ref"]):
            with self.subTest(argv=argv[0]):
                was = self.rowcount()
                rc, out, err = self.cli("add", *argv)
                self.assertEqual(rc, 0, (argv, out, err))
                self.assertEqual(self.rowcount(), was + 1)

    def test_the_flag_never_reaches_the_TITLE(self):
        """The other half of the same ordering bug: --owner-asked used to be
        stripped AFTER the title was built, and the title was then re-joined a
        second time to compensate. One join now, after every flag."""
        rc, out, _err = self.cli("add", "--owner-asked", "he asked for this")
        self.assertEqual(rc, 0, out)
        row = self.filed("he asked for this")
        self.assertEqual(row["title"], "he asked for this")
        self.assertEqual(row["origin"], "owner")

    def test_a_valued_flag_NEVER_swallows_the_next_flag(self):
        """`--note --owner cj` set note to "--owner" AND ate the
        owner — one typo, two wrong fields, rc 0, and a row claiming what
        nobody asked for. Filing a lie beats no row only if you never notice."""
        self.files_one("a real row, so the counter is proven live",
                       "--owner", "seat-a")
        before = self.rowcount()
        # THE COUNTER IS PROVEN LIVE, not merely described as such: a real
        # row was just filed, so a zero here would mean rowcount() cannot see
        # anything and the unchanged-count assertion below proves nothing.
        self.assertGreater(before, 0)
        # `--owner` is consumed FIRST, so here it is the one staring at a
        # flag-shaped token. It takes nothing and the valueless check names it.
        rc, _out, err = self.cli("add", "--owner", "--note", "x", "a title")
        self.assertEqual(rc, 2)
        self.assertIn("--owner", err)
        self.assertIn("not another flag", err)
        self.assertEqual(self.rowcount(), before)

    def test_a_flag_that_eats_the_TITLE_files_nothing_either(self):
        """The same typo with the flags the other way round takes a different
        path — `--note` legitimately consumes "a title" as its value, leaving
        no title at all — and the property that matters is identical: rc 2 and
        NOTHING FILED. Pinning the message here would pin the path; pinning
        the outcome pins the contract."""
        self.files_one("another real row", "--owner", "seat-a")
        before = self.rowcount()
        # THE COUNTER IS PROVEN LIVE, not merely described as such: a real
        # row was just filed, so a zero here would mean rowcount() cannot see
        # anything and the unchanged-count assertion below proves nothing.
        self.assertGreater(before, 0)
        rc, _out, err = self.cli("add", "--note", "--owner", "seat-a", "a title")
        self.assertEqual(rc, 2)
        self.assertTrue(err.strip())
        self.assertEqual(self.rowcount(), before)

    def test_a_TRAILING_valued_flag_is_refused_not_dropped(self):
        self.files_one("a third real row", "--owner", "seat-a")
        before = self.rowcount()
        # THE COUNTER IS PROVEN LIVE, not merely described as such: a real
        # row was just filed, so a zero here would mean rowcount() cannot see
        # anything and the unchanged-count assertion below proves nothing.
        self.assertGreater(before, 0)
        rc, _out, err = self.cli("add", "a title", "--owner", "seat-a", "--note")
        self.assertEqual(rc, 2)
        self.assertIn("--note", err)
        self.assertEqual(self.rowcount(), before)

    def test_id_is_covered_and_still_works(self):
        """--id was flagged as uncovered. It is a valued flag like the
        others, so it now carries the same refusal AND the same positive."""
        rc, out, _err = self.cli("add", "a numbered row", "--owner", "seat-a",
                                 "--id", "9101")
        self.assertEqual(rc, 0, out)
        self.assertEqual(tasks.get("task/9101")["title"], "a numbered row")
        before = self.rowcount()
        # THE COUNTER IS PROVEN LIVE, not merely described as such: a real
        # row was just filed, so a zero here would mean rowcount() cannot see
        # anything and the unchanged-count assertion below proves nothing.
        self.assertGreater(before, 0)
        rc, _out, err = self.cli("add", "another", "--owner", "seat-a", "--id")
        self.assertEqual(rc, 2)
        self.assertIn("--id", err)
        self.assertEqual(self.rowcount(), before)

    def test_double_dash_makes_a_hyphen_title_EXPRESSIBLE(self):
        """The guard needs an escape hatch or it is a guard someone deletes
        the day they legitimately need a title starting with a dash."""
        rc, out, _err = self.cli("add", "--owner", "seat-a", "--",
                                 "--force is the flag we mean")
        self.assertEqual(rc, 0, out)
        row = self.filed("--force is the flag we mean")
        self.assertEqual(row["title"], "--force is the flag we mean")
        self.assertEqual(row["owner"], "seat-a")

    def test_a_REAL_title_containing_a_flag_shaped_word_still_files(self):
        """THE CONTROL FOR FIRST-TOKEN-ONLY: titles legitimately contain
        dashes and flag-shaped words further in. A guard that refused those
        would break real filing to catch a typo."""
        rc, out, _err = self.cli("add", "fix the --force bypass in update",
                                 "--owner", "seat-a")
        self.assertEqual(rc, 0, out)
        row = self.filed("fix the --force bypass in update")
        self.assertEqual(row["title"], "fix the --force bypass in update")



class DashValuedFlagIsExpressibleTest(CliBase):
    """The escape hatch this verb's own comment claimed was REFUTED.
    `--` was documented as the way to mean a dash on purpose; measured, it
    cuts the TITLE and never reaches a flag VALUE, so `--note -- --force`,
    `--note --force` and `--note -5` all returned rc 2 and NO spelling could
    file a note reading `--force` — or `-5`, which is an ordinary note.

    And the GNU form everyone reaches for next was not implemented at all,
    which was worse than the refusal: `task add T --owner=cj --ref=#218
    --note=--force` returned rc 0 having filed title `T --owner=cj --ref=#218
    --note=--force` with owner None, note None, refs []. Four fields lost
    silently, and the operator told the row was filed.

    So the `=` form becomes authoritative and literal, the space form keeps
    refusing (it is genuinely ambiguous), and the refusal now hands back the
    spelling that means it."""

    def rowcount(self):
        rows, unavailable = tasks.snapshot()
        self.assertIsNone(unavailable, unavailable)
        return len(rows)

    def test_a_VALUELESS_repeated_flag_refuses_instead_of_vanishing(self):  # noqa: VACUOUS_ASSERTION — the rung sees assertEqual(rowcount(), before) as an absence with no positive on the same observable. Three unconditional controls stand outside every branch: the refusal itself is asserted positively (rc == 2 AND the message names --ref), and the arm then FILES three rows through the same door and asserts their exact refs lists ([#218], [#1,#2], []) — so an unchanged count during the refusal cannot be explained by a ledger that accepts nothing. Mutation-proven: dropping --ref from the valueless list fails this arm.
        """On row e2acaae0: `task add REF-PROBE --ref` returned rc 0
        with refs=[]. `_take` CONSUMES a bare trailing flag while returning
        None — that is how the space form refuses without leaving a half-parse
        behind — and the three singleton flags were caught by add()'s
        `valueless` check while the REPEATED one had no equivalent, so its
        valueless form vanished silently.

        MY FIRST FIX WAS WRONG AND A PROBE CAUGHT IT: I added a break to
        `_take_all` believing the flag was LEFT in `rest`. It is not. The
        branch never fired and the defect survived a green edit."""
        before = self.rowcount()
        rc, out, err = self.cli("add", "REF-PROBE", "--ref")
        self.assertEqual(rc, 2, (out, err))
        self.assertIn("--ref needs a value", err, err)
        self.assertEqual(self.rowcount(), before, "a refused add filed a row")

        for argv, why in ((("add", "R1", "--ref=#218"), "one equals ref"),
                          (("add", "R2", "--ref", "#1", "--ref", "#2"), "two"),
                          (("add", "R3", "--note=x"), "no --ref at all")):
            with self.subTest(control=why):
                rc, out, err = self.cli(*argv)
                self.assertEqual(rc, 0, (why, out, err))
        self.assertEqual(self.filed("R1")["refs"], ["#218"])
        self.assertEqual(self.filed("R2")["refs"], ["#1", "#2"])
        self.assertEqual(self.filed("R3")["refs"], [])

    def test_an_EMPTY_owner_filter_refuses_rather_than_printing_everything(self):
        """On the same row: `task list --owner= --json` returned rc 0
        with EVERY row, because `if only:` reads "" as falsy and drops the
        filter. An operator who asked to NARROW got the whole ledger, which is
        the one direction a filter must never fail in.

        BOTH SPELLINGS, and the bare one needed a second fix: `_take` consumes
        `--owner` even when the next token is `--json`, so a check that reads
        `rest` AFTER the take finds nothing. The presence question is asked
        BEFORE."""
        self.cli("add", "the signer export rung", "--owner=seat-a")
        self.cli("add", "the proxy pool rung")
        for argv, why in ((("list", "--owner=", "--json"), "equals-empty"),
                          (("list", "--owner", "--json"), "bare before --json")):
            with self.subTest(spelling=why):
                rc, out, err = self.cli(*argv)
                self.assertEqual(rc, 2, (why, out, err))
                self.assertIn("needs a seat name", err, err)

        rc, out, err = self.cli("list", "--owner", "seat-a", "--json")
        self.assertEqual(rc, 0, (out, err))
        self.assertEqual(len(json.loads(out)), 1, "the real filter stopped working")
        rc, out, err = self.cli("list", "--json")
        self.assertEqual(rc, 0, (out, err))
        self.assertEqual(len(json.loads(out)), 2,
                         "dropping the flag must still list everything")

    def test_a_DUPLICATE_valued_flag_after_a_title_token_cannot_corrupt_it(self):  # noqa: VACUOUS_ASSERTION — the rung sees assertEqual(rowcount(), before) as an absence with no positive on the same observable. Three unconditional controls stand outside every branch: assertIsInstance(before, int) proves the ledger was readable at all; each subTest asserts rc == 2 AND that the stderr names the duplicate reason, which are positives on the refusal itself; and the arm ENDS by filing a real row and asserting rowcount() == before + 1, so an unchanged count during the refusals cannot be explained by a ledger that accepts nothing. Mutation-proven: restoring the rest[0]-only check fails 4 subtests.
        """On row e37b25f9, exact probe: `task add PROBE --note=one
        --note=two` returned rc 0 with note="one" and title="PROBE
        --note=two". `_take` consumes only the FIRST occurrence of a singleton
        flag and leaves the rest in argv, while add() checked only rest[0] —
        so a second known flag AFTER a title token was joined into the name.
        That contradicted this lane's own "silent title corruption ended"
        contract, and it made add() and update() disagree about identical
        argv, since update already rejected leftovers anywhere.

        A DUPLICATE IS JUST AN UNCONSUMED KNOWN FLAG, which is why the cure is
        one full-argv scan rather than a special case: the same check catches
        a stray unknown flag in the same position.

        BOTH SPELLINGS AND THE MIX, because they take different branches of
        `_take` and only one of them was ever exercised."""
        before = self.rowcount()
        for argv, why in (
                (("add", "PROBE", "--note=one", "--note=two"), "equals form"),
                (("add", "PROBE", "--note", "one", "--note", "two"), "space form"),
                (("add", "PROBE", "--note=one", "--note", "two"), "mixed")):
            with self.subTest(spelling=why):
                rc, out, err = self.cli(*argv)
                self.assertEqual(rc, 2, (why, out, err))
                self.assertIn("already given once", err,
                              "%s refused for the wrong reason, which sends "
                              "the operator hunting a typo in a flag they "
                              "spelled right twice: %r" % (why, err))
        # THE UNCONDITIONAL POSITIVE, on the same observable the absence is
        # about: the ledger must still be REACHABLE and countable, because
        # `rowcount()` raising or an unreadable snapshot would make the
        # equality below pass for the wrong reason.
        self.assertIsInstance(before, int)
        self.assertEqual(self.rowcount(), before,
                         "a refused duplicate filed a row anyway")
        rc, out, err = self.cli("add", "PROOF-THE-LEDGER-ACCEPTS", "--note=x")
        self.assertEqual(rc, 0, (out, err))
        self.assertEqual(self.rowcount(), before + 1,
                         "the ledger accepted nothing at all, so the equality "
                         "above proved nothing about the refusals")

    def test_an_unknown_flag_after_a_title_is_still_named_as_unknown(self):
        """THE OTHER SIDE OF THE SAME SCAN — a stray that is NOT a duplicate
        must keep its own reason. Collapsing both into one message would trade
        a corruption for a misdiagnosis."""
        rc, out, err = self.cli("add", "PROBE", "--nosuchflag")
        self.assertEqual(rc, 2, (out, err))
        self.assertIn("not recognised", err, err)
        self.assertNotIn("already given once", err, err)

    def test_a_SINGLE_occurrence_and_a_literal_title_are_untouched(self):
        """THE FAILURE-DIRECTION CONTROL. The scan must only ever ADD
        refusals — one occurrence still files, and `--` still delivers a title
        that really starts with a dash."""
        rc, out, err = self.cli("add", "CONTROL", "--note=one")
        self.assertEqual(rc, 0, (out, err))
        self.assertEqual(self.filed("CONTROL")["note"], "one")

        rc, out, err = self.cli("add", "--note=x", "--", "--dashy")
        self.assertEqual(rc, 0, (out, err))
        row = self.filed("--dashy")
        self.assertEqual(row["title"], "--dashy")
        self.assertEqual(row["note"], "x")

    def test_a_FLAG_SHAPED_note_value_files_literally(self):
        """The exact token from the review verdict. It must reach the note
        field intact AND leave the title alone — the pre-fix failure was not
        a refusal here, it was the value landing in the row's NAME."""
        rc, out, err = self.cli("add", "T", "--owner", "seat-a", "--note=--force")
        self.assertEqual(rc, 0, (out, err))
        row = self.filed("T")
        self.assertEqual(row["note"], "--force")
        self.assertEqual(row["title"], "T")      # not "T --note=--force"
        self.assertEqual(row["owner"], "seat-a")

    def test_a_NEGATIVE_NUMBER_is_an_ordinary_note(self):
        """`-5` was refused as "not another flag". It is a note."""
        rc, out, err = self.cli("add", "minus five", "--owner", "seat-a",
                                "--note=-5")
        self.assertEqual(rc, 0, (out, err))
        row = self.filed("minus five")
        self.assertEqual(row["note"], "-5")
        self.assertEqual(row["title"], "minus five")
        self.assertEqual(row["owner"], "seat-a")

    def test_every_valued_flag_gained_the_form_through_ONE_door(self):
        """_take is the single door, so --owner/--id/--ref get the literal
        form in the same edit. --ref needed its LOOP condition taught too:
        `while flag in rest` is an exact-token test, so `--ref=#218` was
        never consumed and fell into the title — the fix reintroduced one
        line from itself."""
        rc, out, err = self.cli("add", "--owner=seat-a", "--id=904",
                                "--ref=#218", "--ref=#219", "--note=-3",
                                "--", "--a dash title")
        self.assertEqual(rc, 0, (out, err))
        row = self.filed("--a dash title")
        self.assertEqual(row["owner"], "seat-a")
        self.assertEqual(row["id"], "task/904")
        self.assertEqual(row["refs"], ["#218", "#219"])
        self.assertEqual(row["note"], "-3")

    def test_the_GENUINE_TYPO_is_still_refused_and_still_files_NOTHING(self):  # noqa: VACUOUS_ASSERTION — the rowcount is pinned by an unconditional positive control: the arm above files a real row and asserts a non-empty literal note, and the first statement here files one and asserts the counter MOVED before this asserts it did not
        """The guard keeps its real value. `--owner --note x` is one flag
        staring at another and only the operator knows which they meant, so
        the space form must go on refusing — otherwise note becomes "--owner"
        and the owner is eaten, the two-wrong-fields row caught in review."""
        was = self.rowcount()
        rc, out, _err = self.cli("add", "a real row", "--owner", "seat-a")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.rowcount(), was + 1)   # the counter CAN move
        before = self.rowcount()
        rc, _out, err = self.cli("add", "--owner", "--note", "x", "a title")
        self.assertEqual(rc, 2)
        self.assertIn("--owner", err)
        self.assertEqual(self.rowcount(), before)

    def test_the_refusal_HANDS_BACK_the_spelling_that_means_it(self):
        """A guard that forbids something unsayable gets deleted by the first
        person who needed it — which is precisely what the review recommended
        doing to this one. The message is the difference."""
        rc, _out, err = self.cli("add", "T", "--owner", "seat-a", "--note", "-5")
        self.assertEqual(rc, 2)
        self.assertIn("--note=VALUE", err)

    def test_the_dash_TITLE_escape_still_works_and_is_still_separate(self):
        """`--` was never wrong, only wrongly described. It keeps covering
        the title, which is a different escape from the one for values."""
        rc, out, err = self.cli("add", "--owner", "seat-a", "--", "--not a flag")
        self.assertEqual(rc, 0, (out, err))
        row = self.filed("--not a flag")
        self.assertEqual(row["title"], "--not a flag")
        self.assertEqual(row["owner"], "seat-a")

    def test_UPDATE_gained_the_same_form_and_the_same_sentence(self):
        """`update` shares _take, so it had the identical hole and gains the
        identical cure. Curing one door and leaving its twin dead-ended
        teaches the operator the CLI is inconsistent, not the spelling."""
        rc, out, err = self.cli("add", "a row to edit", "--owner", "seat-a")
        self.assertEqual(rc, 0, (out, err))
        tid = self.filed("a row to edit")["id"]
        rc, out, err = self.cli("update", tid, "--note=-5")
        self.assertEqual(rc, 0, (out, err))
        self.assertEqual(self.filed("a row to edit")["note"], "-5")
        rc, _out, err = self.cli("update", tid, "--note", "-5")
        self.assertEqual(rc, 2)
        self.assertIn("--note=VALUE", err)
        # the refused command changed NOTHING — and the value it would have
        # written is the one already there, so this pins the row is intact
        # rather than pinning an absence
        self.assertEqual(self.filed("a row to edit")["note"], "-5")


class OriginProvenanceTest(TasksBase):
    """`origin` — WHO ASKED, a one-bit fact known free at filing time.

    The field EXISTED, populated on 251 of 291 rows by the migration alone,
    and no verb could write it: add() never passed it and update()'s allowed
    tuple omitted it. So provenance went into titles instead, in four
    incompatible spellings across 16 rows. Wiring it costs zero new fields.
    """

    def test_the_closed_set_is_enforced_at_BOTH_doors(self):
        """A closed set enforced at add() and open at update() is not a closed
        set — this file's own update() docstring records the measured case
        where `update --owner thief` walked around a CLI-only guard."""
        row, err = tasks.add("filed by an agent", "seat-a", path=self.path,
                             origin="agent")
        self.assertIsNone(err)                       # MUST-HIT: valid passes
        self.assertEqual(row["origin"], "agent")
        _r, err = tasks.add("bogus provenance", "seat-a", path=self.path,
                            origin="the-tooth-fairy")
        self.assertIsNotNone(err)
        self.assertIn("unknown origin", err)
        _r, err = tasks.update(row["id"], path=self.path,
                               origin="the-tooth-fairy")
        self.assertIsNotNone(err, "update must enforce the SAME closed set")
        self.assertIn("unknown origin", err)
        # and the legitimate correction still lands
        fixed, err = tasks.update(row["id"], path=self.path, origin="owner")
        self.assertIsNone(err)
        self.assertEqual(fixed["origin"], "owner")

    def test_an_undeclared_origin_stays_UNKNOWN_and_is_never_defaulted(self):
        """The 251 legacy rows carry provenance nobody witnessed. Inventing
        one for them would be the erasure this field exists to end, performed
        in the other direction."""
        # POSITIVE CONTROL ON THE SAME OBSERVABLE, unconditional: a declared
        # row DOES carry the field, so the None below means "undeclared" and
        # not "this field is dead" — an absence assertion against a field
        # nothing ever populates is the vacuous shape the rung exists to catch.
        declared, err = tasks.add("declared row", "seat-a", path=self.path,
                                  origin="owner")
        self.assertIsNone(err)
        self.assertEqual(declared["origin"], "owner")

        row, err = tasks.add("legacy-shaped row", "seat-a", path=self.path)
        self.assertIsNone(err)
        self.assertIsNone(row["origin"])
        self.assertNotIn(row["origin"], tasks.ORIGINS)

    def test_the_glyph_marks_owner_asked_rows_only(self):
        """A provenance nothing renders is a provenance nobody sorts by —
        which is how 16 rows ended up smuggling it into their titles."""
        asked, _e = tasks.add("owner asked for this", "seat-a", path=self.path,
                              origin="owner")
        agent, _e = tasks.add("agent filed this", "seat-a", path=self.path,
                              origin="agent")
        legacy, _e = tasks.add("nobody said", "seat-a", path=self.path)
        self.assertIn("@", tasks._fmt(asked))
        self.assertNotIn("@", tasks._fmt(agent))
        self.assertNotIn("@", tasks._fmt(legacy))


class ProjectAxisBase(CliBase):
    """The project-axis fixture: PROJECT is registered in the TEMP registry
    and the cwd is its path, so the cwd->project lens resolves hermetically;
    OTHER is a second project. `seed` writes two rows of ours, two foreign
    and one pre-field legacy line, and `ledger_records` reads the raw ledger
    file.

    THIS CLASS HOLDS NO `test_*` METHOD, and that is its contract. unittest
    collects every inherited `test_*` again under each subclass's own id, so
    an arm written here runs once per subclass. A class that needs this
    fixture subclasses THIS class; its arms go in the subclass."""

    PROJECT = "fixproj"
    OTHER = "otherproj"

    def setUp(self):
        super().setUp()
        self.prior_cwd = os.getcwd()
        self.proj_dir = os.path.realpath(
            os.path.join(self.tmp, "fixproj-repo"))
        os.makedirs(self.proj_dir, exist_ok=True)
        # THE FOREIGN PROJECT IS REGISTERED TOO, at its own path. The scope
        # arms below need a SECOND real cwd that resolves to a DIFFERENT
        # registered project — that is the whole shape of task/2446 (a task
        # about helm filed from another checkout) — and a name the flag can
        # address. The existing arms seed foreign rows through the API, so
        # registering the name changes nothing they assert.
        self.other_dir = os.path.realpath(
            os.path.join(self.tmp, "otherproj-repo"))
        os.makedirs(self.other_dir, exist_ok=True)
        gdir = home.global_dir()
        os.makedirs(gdir, exist_ok=True)
        with open(os.path.join(gdir, "registry.json"), "w",
                  encoding="utf-8") as fh:
            json.dump({"version": 1, "projects": {
                self.PROJECT: {"name": self.PROJECT,
                               "path": self.proj_dir},
                self.OTHER: {"name": self.OTHER,
                             "path": self.other_dir}}}, fh)

    def tearDown(self):
        os.chdir(self.prior_cwd)
        super().tearDown()

    def ledger_records(self):
        """The raw ledger FILE, parsed line by line — the event-sourced law:
        a projection can hide exactly what these arms assert about."""
        with open(tasks.ledger_path(), encoding="utf-8") as fh:
            return [json.loads(l) for l in fh if l.strip()]

    def seed(self):
        """Two of ours, two foreign, one true pre-field legacy line."""
        for title in ("ours alpha", "ours beta"):
            _r, err = tasks.add(title, "", source="seat-a",
                                project=self.PROJECT)
            self.assertEqual(err, None)
        for title in ("foreign gamma", "foreign delta"):
            _r, err = tasks.add(title, "", source="seat-a",
                                project="otherproj")
            self.assertEqual(err, None)
        # A GENUINE legacy line — no `project` key AT ALL, the shape every
        # row written before the field carries. Appended raw because add()
        # can no longer produce it, and simulating legacy through the new
        # door would test the wrong bytes.
        legacy = {"id": "task/9001", "ts": 1.0, "last_updated": 1.0,
                  "title": "legacy epsilon", "status": "open", "owner": None,
                  "note": None, "refs": [], "source": None, "origin": None,
                  "closed_reason": None, "comments": []}
        with open(tasks.ledger_path(), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(legacy) + "\n")


class ProjectAxisTest(ProjectAxisBase):
    """task/974 — THE PROJECT AXIS. One derivation (the store's own
    project_for_cwd, reused never re-implemented), stamped at the CLI door,
    scoped at list with every withheld population DISCLOSED, and ids staying
    fleet-wide: `show`/`resolve` never scope.

    The fixture registers a project in the TEMP registry (HELM_HOME is
    already redirected by TasksBase) and chdirs into its path, so the cwd→
    project lens resolves hermetically; every arm asserts the lens's answer
    FIRST — the positive control without which a green scope test only
    proves the filter never ran (absence-of-a-match law: seed the scan with
    a MUST-HIT).

    A subclass of THIS class runs every arm below again under its own id,
    which is right only for one that changes the fixture those arms read. A
    class that only needs the fixture subclasses ProjectAxisBase."""

    def test_add_stamps_the_cwd_project_and_says_so(self):  # noqa: VACUOUS_ASSERTION — unconditional positive controls in-arm: the scoping note literal is asserted PRESENT in err and the stamped value equals the non-empty literal 'fixproj' on both the projection and the raw file line
        os.chdir(self.proj_dir)
        # the MUST-HIT control: the reused lens resolves this fixture
        self.assertEqual(tasks.current_project(), self.PROJECT)
        rc, out, err = self.cli("add", "stamped", "row")
        self.assertEqual(rc, 0, err)
        self.assertTrue("scoping to project 'fixproj'" in err, err)
        self.assertEqual(self.filed("stamped row").get("project"), "fixproj")
        # the stamp is on the appended LINE, not only in the projection
        recs = [r for r in self.ledger_records()
                if r.get("title") == "stamped row"]
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0].get("project"), "fixproj")

    def test_add_outside_any_project_files_unscoped(self):  # noqa: VACUOUS_ASSERTION — the positive polarity is the SIBLING arm test_add_stamps_the_cwd_project_and_says_so on the same observables (err note + project value); here filed() asserts the row EXISTS before its absent scope is read, so the arm cannot pass on a row that was never written
        os.chdir(self.tmp)   # a real directory NO registry project claims
        self.assertEqual(tasks.current_project(), None)
        rc, _out, err = self.cli("add", "free", "row")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.filed("free row").get("project"), None)
        self.assertFalse("scoping to project" in err, err)

    def test_default_list_withholds_and_discloses_the_file_counts(self):  # noqa: VACUOUS_ASSERTION — unconditional positive controls in-arm: 'ours alpha'/'ours beta' asserted PRESENT in the same stdout the absences are asserted on, and the disclosure counts are pinned to non-zero literals (1, 2) before the formatted line is matched
        self.seed()
        os.chdir(self.proj_dir)
        self.assertEqual(tasks.current_project(), self.PROJECT)
        rc, out, err = self.cli("list")
        self.assertEqual(rc, 0, err)
        self.assertTrue("ours alpha" in out, out)
        self.assertTrue("ours beta" in out, out)
        self.assertFalse("foreign gamma" in out, out)
        self.assertFalse("foreign delta" in out, out)
        self.assertFalse("legacy epsilon" in out, out)
        self.assertTrue("scoping to project 'fixproj'" in err, err)
        # THE DISCLOSURE COUNT, measured from the FILE'S LINES independently
        # of the code under test (event-sourced law: the projection under
        # test must not be the instrument that checks it).
        openrecs = [r for r in self.ledger_records()
                    if r.get("status") in ("open", "in_progress")]
        unscoped = sum(1 for r in openrecs
                       if not str(r.get("project") or "").strip())
        foreign = sum(1 for r in openrecs
                      if str(r.get("project") or "").strip()
                      not in ("", self.PROJECT))
        # literal pins first — a derived-vs-derived comparison can go green
        # over an empty fixture
        self.assertEqual(unscoped, 1)
        self.assertEqual(foreign, 2)
        self.assertTrue(
            ("%d unscoped legacy row(s), %d row(s) from other projects — "
             "--all-projects shows them" % (unscoped, foreign)) in out, out)

    def test_all_projects_shows_everything_labeled(self):
        self.seed()
        os.chdir(self.proj_dir)
        rc, out, err = self.cli("list", "--all-projects")
        self.assertEqual(rc, 0, err)
        for needle in ("ours alpha", "foreign gamma", "legacy epsilon",
                       "[fixproj]", "[otherproj]", "[UNSCOPED]"):
            self.assertTrue(needle in out, "%r missing from:\n%s"
                            % (needle, out))
        # the json leg carries the same axis through the ONE serializer
        rc, out, err = self.cli("list", "--all-projects", "--json")
        self.assertEqual(rc, 0, err)
        got = {r["title"]: r.get("project") for r in json.loads(out)}
        self.assertEqual(got.get("ours alpha"), "fixproj")
        self.assertEqual(got.get("foreign gamma"), "otherproj")
        self.assertEqual(got.get("legacy epsilon"), None)
        self.assertEqual("legacy epsilon" in got, True)

    def test_legacy_renders_UNSCOPED_never_inside_a_project_scope(self):  # noqa: VACUOUS_ASSERTION — unconditional positive control in-arm: exactly ONE line containing 'legacy epsilon' is asserted to exist in the --all-projects output and '[UNSCOPED]' asserted PRESENT on it before the scope-label absence is read
        self.seed()
        os.chdir(self.proj_dir)
        rc, out, _err = self.cli("list")
        self.assertEqual(rc, 0)
        self.assertFalse("legacy epsilon" in out, out)
        rc, out, _err = self.cli("list", "--all-projects")
        self.assertEqual(rc, 0)
        line = [l for l in out.splitlines() if "legacy epsilon" in l]
        self.assertEqual(len(line), 1, out)
        self.assertTrue("[UNSCOPED]" in line[0], line[0])
        self.assertFalse("[fixproj]" in line[0], line[0])

    def test_show_and_resolve_answer_across_scope(self):  # noqa: VACUOUS_ASSERTION — unconditional positive controls in-arm: rc 0 plus 'foreign gamma'/'otherproj'/'legacy epsilon' asserted PRESENT in stdout; the lens control (current_project == fixproj) proves the scope the answers cross is real
        """A cited id must never answer ABSENT because of a filter — ids are
        fleet-wide even when listings are not."""
        self.seed()
        os.chdir(self.proj_dir)
        self.assertEqual(tasks.current_project(), self.PROJECT)
        foreign_id = [r["id"] for r in self.ledger_records()
                      if r.get("title") == "foreign gamma"][0]
        rc, out, err = self.cli("show", foreign_id)
        self.assertEqual(rc, 0, err)
        self.assertTrue("foreign gamma" in out, out)
        self.assertTrue("otherproj" in out, out)   # WHOSE row answered
        rc, out, err = self.cli("resolve", "9001")
        self.assertEqual(rc, 0, err)
        self.assertTrue("legacy epsilon" in out, out)

    def test_the_footer_counts_the_scoped_population_not_the_fleet(self):  # noqa: VACUOUS_ASSERTION — every assertion is a whole-row equality against a non-empty literal footer ("2 shown — closed 1, open 2" / "5 shown — closed 1, open 5"); the exactly-one-footer count is the guard that the row compared is the row rendered
        """Dispatch ff7024b5: the scoped list's footer called
        fleet-wide counts() — "2 shown — open 5" — silently totalizing the
        exact rows the scope had just withheld and disclosed. The pin is the
        footer ROW compared WHOLE: an assertion scoped to the full screen is
        satisfied by any legend, including the defective one."""
        self.seed()
        # a CLOSED in-scope row, so the pinned legend proves the population
        # choice twice over: closed 1 can only come from counting the scoped
        # ledger beyond the rendered subset, and open 2 (never open 5) can
        # only come from excluding the withheld foreign + legacy rows
        _r, err = tasks.add("ours done", "", source="seat-a",
                            project=self.PROJECT, status="closed",
                            closed_reason="finished inside the fixture")
        self.assertEqual(err, None)
        os.chdir(self.proj_dir)
        self.assertEqual(tasks.current_project(), self.PROJECT)
        rc, out, err = self.cli("list")
        self.assertEqual(rc, 0, err)
        footer = [l for l in out.splitlines() if " shown — " in l]
        self.assertEqual(len(footer), 1, out)
        self.assertEqual(footer[0], "2 shown — closed 1, open 2")
        # and the unscoped view keeps its fleet-wide legend: same footer,
        # every status total drawn from the whole fixture file
        rc, out, err = self.cli("list", "--all-projects")
        self.assertEqual(rc, 0, err)
        footer = [l for l in out.splitlines() if " shown — " in l]
        self.assertEqual(len(footer), 1, out)
        self.assertEqual(footer[0], "5 shown — closed 1, open 5")


class ScopeFlagBeforeCwdTest(ProjectAxisBase):
    """task/2446 — A TASK IS SCOPED BY `--project` BEFORE cwd.

    MEASURED from a project checkout: `helm task add`
    printed "scoping to project '<that project>' (from cwd)" for a task filed
    ABOUT HELM (task/2436), so the row landed in that scope where the making
    team's `helm task list` does not look without --all-projects. The owner
    sentence this lane serves: a team USING helm must never need to know how
    helm is made, and wherever a verb assumes the helm checkout that is the
    bug.

    RED BEFORE, GREEN AFTER: against pre-fix trunk `task add --project helm`
    exited 2 with "'--project' is not a title — that flag was not recognised"
    (never swallowed into the title, which is the one thing worse), `task
    list --project X` exited 2 with "unknown option --project", and `update`
    and `close` refused it as unrecognised.

    Every arm carries the fixture's MUST-HIT control: the two registered
    project directories each resolve through the REAL lens to their own name,
    so an arm cannot pass on a scope that was never derived.
    """

    def other_cwd(self):
        """Stand in the OTHER project — the other-checkout position, whose
        cwd derives to a DIFFERENT registered project. Asserted, not assumed:
        without this control a green flag arm proves only that cwd scoping
        never ran."""
        os.chdir(self.other_dir)
        self.assertEqual(tasks.current_project(), self.OTHER)

    def test_the_flag_wins_over_cwd_and_the_door_says_how_it_decided(self):
        self.other_cwd()
        rc, _out, err = self.cli("add", "--project", self.PROJECT,
                                 "filed", "from", "elsewhere")
        self.assertEqual(rc, 0, err)
        # WHICH SCOPE, AND HOW IT WAS DECIDED — one line, both facts
        self.assertTrue(
            "scoping to project 'fixproj' (from --project, which wins over "
            "cwd)" in err, err)
        self.assertFalse("(from cwd" in err, err)
        # THE STAMP IS ON THE APPENDED LINE, not only in the projection
        recs = [r for r in self.ledger_records()
                if r.get("title") == "filed from elsewhere"]
        self.assertEqual(len(recs), 1, recs)
        self.assertEqual(recs[0].get("project"), "fixproj")
        # and the title is the title: the flag's value never joined it
        self.assertEqual(self.filed("filed from elsewhere").get("project"),
                         "fixproj")

    def test_without_the_flag_cwd_still_decides(self):
        """THE MUST-HIT for the arm above. If cwd scoping had broken, the
        flag arm would pass for the wrong reason."""
        self.other_cwd()
        rc, _out, err = self.cli("add", "filed", "by", "cwd")
        self.assertEqual(rc, 0, err)
        self.assertTrue(
            "scoping to project 'otherproj' (from cwd; pass "
            "--project=NAME to override)" in err, err)
        self.assertEqual(self.filed("filed by cwd").get("project"),
                         "otherproj")

    def test_the_scoped_row_lists_in_its_project_and_not_in_the_other(self):  # noqa: VACUOUS_ASSERTION — unconditional positive controls in-arm and in the SAME stdout each absence is read from: "other side row" asserted PRESENT where "helm side row" is asserted absent, then the exact reverse from the other cwd, then "helm side row" PRESENT under --project
        """BOTH DIRECTIONS, each with its own must-hit. A listing arm that
        only asserts presence passes over a filter that never ran; one that
        only asserts absence passes over an empty ledger."""
        self.other_cwd()
        rc, _out, err = self.cli("add", "--project", self.PROJECT,
                                 "helm side row")
        self.assertEqual(rc, 0, err)
        rc, _out, err = self.cli("add", "other side row")
        self.assertEqual(rc, 0, err)
        # from the OTHER project's cwd: its own row shows, the flagged one
        # does not — the control and the subject in ONE output
        rc, out, err = self.cli("list")
        self.assertEqual(rc, 0, err)
        self.assertTrue("other side row" in out, out)
        self.assertFalse("helm side row" in out, out)
        # from the flagged project's cwd: exactly the reverse
        os.chdir(self.proj_dir)
        self.assertEqual(tasks.current_project(), self.PROJECT)
        rc, out, err = self.cli("list")
        self.assertEqual(rc, 0, err)
        self.assertTrue("helm side row" in out, out)
        self.assertFalse("other side row" in out, out)
        # AND THE READ DOOR TAKES THE SAME FLAG — the filer who reported the
        # bug has to be able to SEE the row afterwards without moving
        self.other_cwd()
        rc, out, err = self.cli("list", "--project", self.PROJECT)
        self.assertEqual(rc, 0, err)
        self.assertTrue("helm side row" in out, out)
        self.assertFalse("other side row" in out, out)
        self.assertTrue(
            "scoping to project 'fixproj' (from --project, which wins over "
            "cwd" in err, err)

    def test_an_unregistered_project_is_refused_naming_the_registry(self):
        self.other_cwd()
        # A REAL ROW FIRST, so the line count below is a measurement on an
        # existing ledger rather than on a missing file — and so "nothing was
        # written" is asserted about a door that demonstrably CAN write.
        rc, _out, err = self.cli("add", "a row that does file")
        self.assertEqual(rc, 0, err)
        before = self.ledger_lines()
        self.assertEqual(before, 1)
        rc, _out, err = self.cli("add", "--project", "nosuchproj",
                                 "should not be filed")
        self.assertEqual(rc, 2, err)
        self.assertTrue("not a registered project" in err, err)
        # the registry is NAMED, and so is what it knows
        self.assertTrue(home.registry_path() in err, err)
        self.assertTrue("fixproj" in err and "otherproj" in err, err)
        # NOTHING WAS WRITTEN — counted on the FILE, not on a projection
        self.assertEqual(self.ledger_lines(), before)
        rc, _out, err = self.cli("list", "--project", "nosuchproj")
        self.assertEqual(rc, 2, err)
        self.assertTrue("not a registered project" in err, err)

    def test_a_valueless_scope_flag_is_refused_never_defaulted(self):  # noqa: VACUOUS_ASSERTION — unconditional positive controls in-arm, outside every loop: the opening `add` files at rc 0 and pins ledger_lines() to the literal 1, and the closing must-hit asserts the same trailing shape WITH a value files and stamps the non-empty literal 'fixproj'
        """A valued flag that produced nothing is a typo, not a default —
        the rule this door already keeps for --owner and --note. Silently
        falling back to cwd here would file the row in exactly the scope the
        operator was overriding.

        THE TOKENS ARE THE WHOLE ARM. A trailing ordinary token IS the flag's
        value, so `add --project 'trailing flag row'` exercises the
        unregistered-project refusal and says nothing about a valueless flag —
        it passes over a build with the valueless guard deleted. So the flag
        comes LAST in one case here and sits BEFORE A KNOWN OPTION in the
        other, and the refusal is pinned to the sentence only the valueless
        guard prints: an unregistered-project or missing-title refusal cannot
        satisfy it. WHAT FAILS: drop PROJECT_FLAG from `add`'s `valueless`
        tuple and both cases file a row into the cwd scope at rc 0.

        NOT RED ON TRUNK — measured: pre-fix the whole flag was refused as
        unrecognised, so this arm passed for a different reason. It is a
        REGRESSION guard on the new acceptance path, and it is named as one
        rather than counted as evidence."""
        self.other_cwd()
        rc, _out, err = self.cli("add", "a row that does file")
        self.assertEqual(rc, 0, err)
        before = self.ledger_lines()
        self.assertEqual(before, 1)
        for argv in (("add", "trailing flag row", "--project"),
                     ("add", "--project", "--owner-asked", "before a bool")):
            rc, _out, err = self.cli(*argv)
            self.assertEqual(rc, 2, "%s -> rc %s / %s" % (argv, rc, err))
            self.assertTrue(
                "--project needs a value that is not another flag" in err,
                err)
            self.assertFalse("not a registered project" in err, err)
            self.assertEqual(self.ledger_lines(), before)
        # THE MUST-HIT: the same trailing shape WITH a value files, so the
        # refusals above are about the missing value and not about position
        rc, _out, err = self.cli("add", "valued row", "--project",
                                 self.PROJECT)
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.filed("valued row").get("project"), "fixproj")

    def test_the_flag_and_all_projects_cannot_both_be_honoured(self):
        self.other_cwd()
        rc, _out, err = self.cli("list", "--project", self.PROJECT,
                                 "--all-projects")
        self.assertEqual(rc, 2, err)
        self.assertTrue("cannot be combined" in err, err)
        # the control: each flag ALONE is accepted on this same verb
        rc, _out, err = self.cli("list", "--all-projects")
        self.assertEqual(rc, 0, err)
        rc, _out, err = self.cli("list", "--project", self.PROJECT)
        self.assertEqual(rc, 0, err)

    def test_an_id_addressed_verb_refuses_the_flag_rather_than_no_op(self):
        """`update`, `close` and `show` resolve a FLEET-WIDE id and read no
        scope, so there is nothing for a flag to win over. Accepting it would
        be a silent no-op on a flag whose whole promise is choosing a scope.
        The refusal SAYS which of the two it is."""
        self.other_cwd()
        rc, _out, err = self.cli("add", "--project", self.PROJECT,
                                 "row to hit")
        self.assertEqual(rc, 0, err)
        tid = self.filed("row to hit")["id"]
        for argv in (("update", tid, "--project", self.PROJECT,
                      "--note", "n"),
                     ("close", tid, "--project", self.PROJECT, "done"),
                     ("show", tid, "--project", self.PROJECT)):
            rc, _out, err = self.cli(*argv)
            self.assertEqual(rc, 2, "%s -> %s" % (argv[0], err))
            self.assertTrue("takes a FLEET-WIDE id" in err, err)
        # NOTHING HAPPENED to the row — the note was not written and the row
        # is still open (the refusal is before the write, not after it)
        row = self.filed("row to hit")
        self.assertEqual(row.get("note"), None)
        self.assertEqual(row.get("status"), "open")
        # THE CONTROL: the same verbs work on the same id without the flag,
        # so the refusal above is about the flag and not about the id
        rc, _out, err = self.cli("update", tid, "--note", "n")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.filed("row to hit").get("note"), "n")
        # A LITERAL `--` KEEPS THE TEXT A TEXT, AND THE TOKENS AFTER IT ARE
        # SPELLINGS THE SCANNER ACTUALLY MATCHES. A single token such as
        # `--project was the text` is neither the exact flag nor the `=` form,
        # so it cannot tell a scanner that honours `--` from one that ignores
        # it. Both known spellings appear after the delimiter here and the
        # stored reason is compared WHOLE. WHAT FAILS: make
        # project_flag_not_applicable read the whole `rest` instead of the
        # head and both closes go rc 2 with nothing written.
        for title, spelling in (("row one", "--project fixproj"),
                                ("row two", "--project=fixproj")):
            rc, _out, err = self.cli("add", title)
            self.assertEqual(rc, 0, err)
            rid = self.filed(title)["id"]
            rc, _out, err = self.cli("close", rid, "--", *spelling.split(" "))
            self.assertEqual(rc, 0, err)
            self.assertEqual(self.filed(title).get("closed_reason"), spelling)
            self.assertEqual(self.filed(title).get("status"), "closed")
        # AND THE SAME TOKENS BEFORE THE DELIMITER REFUSE, nothing written —
        # the boundary is what separates them, not the tokens themselves
        rc, _out, err = self.cli("add", "row that stays open")
        self.assertEqual(rc, 0, err)
        open_id = self.filed("row that stays open")["id"]
        for argv in ((open_id, "--project", self.PROJECT, "done"),
                     (open_id, "--project=%s" % self.PROJECT, "done")):
            rc, _out, err = self.cli("close", *argv)
            self.assertEqual(rc, 2, "%s -> rc %s / %s" % (argv, rc, err))
            self.assertTrue("takes a FLEET-WIDE id" in err, err)
        row = self.filed("row that stays open")
        self.assertEqual(row.get("status"), "open")
        self.assertEqual(row.get("closed_reason"), None)

    def test_triage_takes_the_scope_it_cannot_derive_from_cwd(self):  # noqa: VACUOUS_ASSERTION — unconditional positive controls in-arm: rc 0, the literal scope line "scoping to project 'fixproj' (from --project, which wins over cwd)" in err, and "ours alpha" asserted PRESENT in the same stdout "foreign gamma" is asserted absent from
        """The third cwd-scoped verb. Run from a directory NO project claims,
        triage refuses for want of a scope — and the flag is what supplies
        one, which is the same shape as `add` filing from the wrong tree."""
        self.seed()
        os.chdir(self.tmp)          # a real directory no project claims
        self.assertEqual(tasks.current_project(), None)
        rc, _out, err = self.cli("triage")
        self.assertEqual(rc, 2, err)
        self.assertTrue("NO PROJECT" in err, err)
        rc, out, err = self.cli("triage", "--project", self.PROJECT)
        self.assertEqual(rc, 0, err)
        self.assertTrue(
            "scoping to project 'fixproj' (from --project, which wins over "
            "cwd)" in err, err)
        # the dry pass names the in-scope rows it would rank, and only those
        self.assertTrue("ours alpha" in out, out)
        self.assertFalse("foreign gamma" in out, out)


    def advertised_apply(self, out):
        """The COPYABLE apply command lifted OUT OF the preview's own text and
        split the way a shell would split it.

        Extracted rather than reconstructed: an arm that rebuilds the argv it
        expects measures its own hypothesis, never the sentence on screen."""
        line = [l for l in out.splitlines() if "DRY. `helm task triage" in l]
        self.assertEqual(len(line), 1, out)
        cmd = line[0].split("`")[1]
        self.assertTrue(cmd.startswith("helm task triage --apply"), cmd)
        argv = shlex.split(cmd)
        self.assertEqual(argv[:2], ["helm", "task"])
        return argv[2:]

    def test_a_valueless_scope_flag_at_the_read_doors_is_refused(self):  # noqa: VACUOUS_ASSERTION — unconditional positive controls in-arm, outside the loop: current_project() equals the non-empty literal 'fixproj', ledger_lines() is pinned to the literal 5, and the closing must-hit pair asserts `list --project NAME` and `triage --apply --project NAME` reach rc 0 and that the apply GREW the ledger file
        """PRESENCE IS NOT VALUE, AND THE TEST FOR IT HAS TO RUN FIRST.
        `_take` DELETES a valueless flag and answers None, which is
        byte-identical to never passing it, so a presence test placed AFTER
        the take can never fire: `list --project` and `triage --apply
        --project` both fell through to CWD — the exact scope the flag exists
        to override — and reported success. Asked before taken, the order this
        same `list` leg already keeps for `--owner`.

        RED BEFORE: from the fixproj checkout `triage --apply --project` exited
        0 and RANKED fixproj's rows, and `list --project --json` exited 0 with
        fixproj's rows."""
        self.seed()
        self.admit()
        os.chdir(self.proj_dir)
        # the MUST-HIT: cwd really does derive a scope here, so a refusal
        # below is the flag being checked and not scoping being broken
        self.assertEqual(tasks.current_project(), self.PROJECT)
        before = self.ledger_lines()
        self.assertEqual(before, 5)
        for argv in (("triage", "--apply", "--project"),
                     ("triage", "--project", "--limit", "1"),
                     ("list", "--project"),
                     ("list", "--project", "--json"),
                     ("list", "--project", "--all-projects")):
            rc, out, err = self.cli(*argv)
            self.assertEqual(rc, 2, "%s -> rc %s / %s" % (argv, rc, err))
            self.assertTrue("needs a registered project name" in err, err)
            self.assertFalse("scoping to project" in err, err)
            self.assertEqual(out, "")
        # NOTHING WAS RANKED — read off the ledger FILE, and the priorities
        # are read too, because triage's write is a field and not a line
        self.assertEqual(self.ledger_lines(), before)
        self.assertEqual([r.get("priority") for r in self.ledger_records()],
                         [None] * 5)
        # THE MUST-HIT PAIR: the same two doors with a real value are
        # accepted, and the apply demonstrably CAN write
        rc, _out, err = self.cli("list", "--project", self.PROJECT)
        self.assertEqual(rc, 0, err)
        rc, _out, err = self.cli("triage", "--apply", "--project",
                                 self.PROJECT)
        self.assertEqual(rc, 0, err)
        self.assertGreater(self.ledger_lines(), before)

    def test_the_triage_preview_advertises_the_scope_it_previewed(self):
        """THE ADVERTISED COMMAND IS RUN, VERBATIM, and it has to write the
        rows that were on screen. The remedy was built from `--limit` and
        `--legacy` and never learned the project axis, so a preview scoped by
        the flag offered an apply with no scope at all.

        RED BEFORE: from the otherproj checkout the preview listed fixproj's
        two rows and advertised `helm task triage --apply`, which then ranked
        OTHERPROJ's two rows — the rows the preview had just excluded — and
        left the previewed ones unranked.

        The name is SHELL-QUOTED, proven on a registered name that contains a
        space: unquoted it splits into two tokens and the copied command
        refuses."""
        self.seed()
        self.admit()
        self.other_cwd()
        rc, out, err = self.cli("triage", "--project", self.PROJECT)
        self.assertEqual(rc, 0, err)
        self.assertTrue("ours alpha" in out, out)
        self.assertFalse("foreign gamma" in out, out)
        argv = self.advertised_apply(out)
        # THE `=` FORM, EXACTLY ONE TOKEN — the spelling the parser admits for
        # every registered name (task/2446 round three). This arm was written
        # for the space form; the advertised spelling changed, its intent did
        # not, so the assertion follows the emitted token. The bare flag must
        # be ABSENT, which is what tells the space form apart from this one.
        self.assertEqual(argv.count("%s=%s" % (tasks.PROJECT_FLAG,
                                               self.PROJECT)), 1, argv)
        self.assertFalse(tasks.PROJECT_FLAG in argv, argv)
        rc, _out, err = self.cli(*argv)
        self.assertEqual(rc, 0, err)
        ranked = {r.get("title"): r.get("priority")
                  for r in tasks.rows(path=tasks.ledger_path()).values()}
        # THE PREVIEWED POPULATION, and the NON-TARGETS preserved
        self.assertEqual(ranked.get("ours alpha"), "P2")
        self.assertEqual(ranked.get("ours beta"), "P2")
        self.assertEqual(ranked.get("foreign gamma"), None)
        self.assertEqual(ranked.get("foreign delta"), None)
        # A SPACED REGISTERED NAME, written to the same real registry file the
        # loader reads — a third project, its own real directory, its own row
        spaced = "two words"
        spaced_dir = os.path.realpath(os.path.join(self.tmp, "spaced-repo"))
        os.makedirs(spaced_dir, exist_ok=True)
        with open(os.path.join(home.global_dir(), "registry.json"), "w",
                  encoding="utf-8") as fh:
            json.dump({"version": 1, "projects": {
                self.PROJECT: {"name": self.PROJECT, "path": self.proj_dir},
                self.OTHER: {"name": self.OTHER, "path": self.other_dir},
                spaced: {"name": spaced, "path": spaced_dir}}}, fh)
        _r, err = tasks.add("spaced row", "", source="seat-a", project=spaced)
        self.assertEqual(err, None)
        rc, out, err = self.cli("triage", "--project", spaced)
        self.assertEqual(rc, 0, err)
        self.assertTrue("spaced row" in out, out)
        argv = self.advertised_apply(out)
        # ONE token, not two — this is the QUOTING assertion and only that.
        # The spelling of the scope token is pinned by
        # test_the_advertised_apply_replays_through_the_parser; asserted here
        # it would be a second copy of the same pin, so this arm asks only
        # that the name arrives whole, which is true of either spelling.
        self.assertEqual(len([t for t in argv if spaced in t]), 1, argv)
        self.assertFalse("two" in argv, argv)
        rc, _out, err = self.cli(*argv)
        self.assertEqual(rc, 0, err)
        ranked = {r.get("title"): r.get("priority")
                  for r in tasks.rows(path=tasks.ledger_path()).values()}
        self.assertEqual(ranked.get("spaced row"), "P2")
        self.assertEqual(ranked.get("foreign gamma"), None)

    def test_a_valued_option_takes_only_the_token_beside_it(self):  # noqa: VACUOUS_ASSERTION — unconditional positive controls in-arm, outside the loop: current_project() equals the non-empty literal 'fixproj', ledger_lines() is pinned to the literal 5, and the closing must-hit pair asserts the SAME tokens adjoined reach rc 0 and that the apply GREW the ledger file
        """ADJACENCY IS DECIDED ON THE ORIGINAL ARGV (task/2446). Every
        leg here removed options in its own order, and each
        removal CLOSED A GAP that had separated a valued flag from a later
        positional — so a flag followed by another flag silently acquired a
        value nobody had written beside it.

        RED BEFORE, measured on this fixture: `triage --project --apply
        fixproj` removed `--apply` first, resolved the scope to fixproj and
        WROTE its ranks at rc 0; `triage --project --limit 1 fixproj --apply`
        did the same across a whole valued option; `list --owner --project
        fixproj seat-a` returned rc 0 having invented `seat-a` as the owner
        filter the scope take had moved next to `--owner`.

        WHY THE PRESENCE TEST CANNOT COVER THIS: `take_project_flag` asks what
        was typed BEFORE it takes, which is what stops a VALUELESS flag
        defaulting to cwd — but by then the argv already reads as if the
        operator had typed the adjacency, so the flag has a value and there is
        nothing left to refuse.

        THE CONTROL AND ITS BLAST RADIUS: delete the
        `option_adjacency_error` call from either leg and the three cases
        below go rc 0, the triage pair WRITES, and the raw-bytes assertion
        fails. Nothing else in this file reads those two call sites, so the
        control reddens this arm alone.
        """
        self.seed()
        self.admit()
        os.chdir(self.proj_dir)
        # the MUST-HIT: cwd really does derive a scope here, so a refusal
        # below is the adjacency being checked and not scoping being broken
        self.assertEqual(tasks.current_project(), self.PROJECT)
        before = self.ledger_lines()
        self.assertEqual(before, 5)
        with open(tasks.ledger_path(), "rb") as fh:
            raw = fh.read()
        for argv, flag in (
                (("triage", "--project", "--apply", self.PROJECT),
                 "--project"),
                (("triage", "--project", "--limit", "1", self.PROJECT,
                  "--apply"), "--project"),
                (("list", "--owner", "--project", self.PROJECT, "seat-a"),
                 "--owner")):
            rc, out, err = self.cli(*argv)
            self.assertEqual(rc, 2, "%s -> rc %s / %s" % (argv, rc, err))
            # the refusal names THE FLAG WHOSE VALUE WAS MISSING and says the
            # rule, so an unregistered-name or usage refusal cannot satisfy it
            self.assertTrue("%s needs" % flag in err, err)
            self.assertTrue("the ONE token beside it" in err, err)
            self.assertFalse("scoping to project" in err, err)
            self.assertFalse("not a registered project" in err, err)
            # NO SELECTION EITHER: a refusal that had already printed the
            # preview would have chosen the very rows it claims not to have
            self.assertEqual(out, "")
        # THE RAW LEDGER FILE IS BYTE-UNCHANGED, so these arms cannot be
        # passing at an earlier gate: an actor is admitted, the registry
        # resolves and the scope name is real, so the only door left is this
        # one. Bytes rather than a line count, because triage's write is a
        # FIELD on an appended snapshot.
        with open(tasks.ledger_path(), "rb") as fh:
            self.assertEqual(fh.read(), raw)
        self.assertEqual([r.get("priority") for r in self.ledger_records()],
                         [None] * 5)
        # THE MUST-HIT PAIR: the SAME tokens with the value ADJOINED are
        # accepted, and the apply demonstrably CAN write
        rc, _out, err = self.cli("triage", "--apply", "--project",
                                 self.PROJECT)
        self.assertEqual(rc, 0, err)
        self.assertGreater(self.ledger_lines(), before)
        rc, _out, err = self.cli("list", "--project", self.PROJECT,
                                 "--owner", "seat-a")
        self.assertEqual(rc, 0, err)

    def test_the_advertised_apply_replays_through_the_parser(self):  # noqa: VACUOUS_ASSERTION — the per-name positives sit inside the two-name loop; the unconditional control below it asserts registered_projects() returns the non-empty literals '-ops' and 'two words', and the closing control asserts rc 2 AND the refusal sentence PRESENT for the space form, which is a positive on the refusal itself
        """THE PREVIEW ADVERTISES A COMMAND, SO THE COMMAND HAS TO PARSE
        (task/2446).

        RED BEFORE: the registry admits a name beginning with a dash, and the
        preview serialized the SPACE form — `--project -ops` — which this
        module's own parser refuses as a missing value. So a scope the flag
        had just ACCEPTED could not be replayed: the preview said "copy this"
        and the copy exited 2.

        SHELL QUOTING IS NOT VALUE ADMISSION. Quoting decides how the shell
        splits the line; the `=` form decides whether the CLI accepts what
        arrives. The spaced name needs the first and the dash-leading name
        needs the second, so both are asserted on the same emitted token.

        NOT RECONSTRUCTED: the argv is lifted out of the preview's own text
        and replayed verbatim, then the ranks are read off the ledger.

        THE CONTROL AND ITS BLAST RADIUS: restore the space form in
        `selection` and the `--project=<name>` assertion fails for both names
        and the dash-leading replay exits 2. Only this arm and
        `test_the_triage_preview_advertises_the_scope_it_previewed` read that
        expression, and that arm's name has no dash and no space, so it stays
        green — which is exactly why this arm had to exist.
        """
        self.admit()
        dashy, spaced = "-ops", "two words"
        awkward = {}
        for i, name in enumerate((dashy, spaced)):
            d = os.path.realpath(os.path.join(self.tmp, "awkward-%d" % i))
            os.makedirs(d, exist_ok=True)
            awkward[name] = d
        # ONE registry file, the real path the loader reads, four real
        # projects — the two the fixture needs for cwd and the two awkward
        # names the flag has to carry
        projects = {self.PROJECT: {"name": self.PROJECT,
                                   "path": self.proj_dir},
                    self.OTHER: {"name": self.OTHER, "path": self.other_dir}}
        for name, d in awkward.items():
            projects[name] = {"name": name, "path": d}
        with open(os.path.join(home.global_dir(), "registry.json"), "w",
                  encoding="utf-8") as fh:
            json.dump({"version": 1, "projects": projects}, fh)
        for name in (dashy, spaced):
            _r, err = tasks.add("row for %s" % name, "", source="seat-a",
                                project=name)
            self.assertEqual(err, None)
        # A NON-TARGET in a third scope, so "it ranked the right rows" is a
        # measurement and not a tautology over a one-project ledger
        _r, err = tasks.add("untouched fixproj row", "", source="seat-a",
                            project=self.PROJECT)
        self.assertEqual(err, None)
        # THE UNCONDITIONAL CONTROL, outside the loop: the registry file
        # written above really is the authority this door reads, and it really
        # does admit both awkward names — so a refusal inside the loop would
        # be the parser and never an unregistered name.
        known, kerr = tasks.registered_projects()
        self.assertEqual(kerr, None)
        self.assertTrue("-ops" in known and "two words" in known, known)
        self.other_cwd()
        for name in (dashy, spaced):
            rc, out, err = self.cli("triage",
                                    "%s=%s" % (tasks.PROJECT_FLAG, name))
            self.assertEqual(rc, 0, err)
            self.assertTrue("row for %s" % name in out, out)
            argv = self.advertised_apply(out)
            # THE `=` FORM, AS ONE TOKEN after a shell split — the quoting and
            # the spelling asserted on the same emitted value
            self.assertTrue("%s=%s" % (tasks.PROJECT_FLAG, name) in argv,
                            argv)
            self.assertFalse(tasks.PROJECT_FLAG in argv, argv)
            # REPLAYED VERBATIM through the real parser
            rc, _out, err = self.cli(*argv)
            self.assertEqual(rc, 0, "%s -> %s" % (argv, err))
            ranked = {r.get("title"): r.get("priority")
                      for r in tasks.rows(path=tasks.ledger_path()).values()}
            self.assertEqual(ranked.get("row for %s" % name), "P2")
            self.assertEqual(ranked.get("untouched fixproj row"), None)
        # THE CONTROL THAT MAKES THE SERIALIZATION LOAD-BEARING: the SPACE
        # form of the dash-leading name is refused by this same parser, so a
        # preview advertising it hands the operator a dead command
        rc, out, err = self.cli("triage", tasks.PROJECT_FLAG, dashy)
        self.assertEqual(rc, 2, err)
        self.assertTrue("needs a registered project name" in err, err)
        self.assertEqual(out, "")

    def test_an_unreadable_registry_refuses_in_its_own_sentence(self):  # noqa: VACUOUS_ASSERTION — unconditional positive controls in-arm, outside the loop: the readable-populated leg files at rc 0 and its stamped project equals the non-empty literal 'fixproj' with ledger_lines() pinned to 1, and the readable-empty control asserts the NOT REGISTERED sentence PRESENT so the UNREADABLE absences are read against a door that produces both sentences
        """THE STORE CANON: an unreadable authority is a REFUSAL carrying its
        own sentence, never an empty set that admits everything or nothing
        silently. The membership check called the ORDINARY loader, which turns
        malformed primary JSON into the empty default — so `known` came back
        as [] with NO error and every name was reported NOT REGISTERED, with
        the remedy "drop the flag to scope by cwd" over a registry that cannot
        resolve a cwd either. The strict authority snapshot already exists for
        exactly this question, and it does not take a write lock or migrate.

        RED BEFORE: rc 2 saying "--project 'fixproj' is not a registered
        project — the registry ... knows: (none)".

        THE TWO CONTROLS separate the cure from "refuse always": a
        READABLE-EMPTY registry keeps the NOT REGISTERED sentence, and the
        READABLE-POPULATED fixture keeps filing."""
        reg = os.path.join(home.global_dir(), "registry.json")
        self.assertEqual(os.path.realpath(reg),
                         os.path.realpath(home.registry_path()))
        # READABLE-POPULATED FIRST: this door demonstrably accepts this very
        # name, so the refusals below are about the registry's readability
        self.other_cwd()
        rc, _out, err = self.cli("add", "--project", self.PROJECT,
                                 "a row that does file")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.filed("a row that does file").get("project"),
                         "fixproj")
        before = self.ledger_lines()
        self.assertEqual(before, 1)
        # MALFORMED PRIMARY JSON — real bytes on the real path, no patching
        with open(reg, "w", encoding="utf-8") as fh:
            fh.write("{ this is not registry json\n")
        with open(reg, "rb") as fh:
            raw = fh.read()
        for argv in (("add", "--project", self.PROJECT, "must not be filed"),
                     ("list", "--project", self.PROJECT),
                     ("triage", "--project", self.PROJECT)):
            rc, _out, err = self.cli(*argv)
            self.assertEqual(rc, 2, "%s -> rc %s / %s" % (argv, rc, err))
            self.assertTrue("UNREADABLE" in err, err)
            self.assertTrue(home.registry_path() in err, err)
            self.assertFalse("not a registered project" in err, err)
            self.assertFalse("drop the flag" in err, err)
        self.assertEqual(self.ledger_lines(), before)
        # THE AUTHORITY SNAPSHOT DOES NOT MUTATE WHAT IT COULD NOT READ
        with open(reg, "rb") as fh:
            self.assertEqual(fh.read(), raw)
        # READABLE-EMPTY CONTROL: still NOT REGISTERED, never UNREADABLE
        with open(reg, "w", encoding="utf-8") as fh:
            json.dump({"version": 1, "projects": {}}, fh)
        rc, _out, err = self.cli("add", "--project", self.PROJECT, "nor this")
        self.assertEqual(rc, 2, err)
        self.assertTrue("not a registered project" in err, err)
        self.assertFalse("UNREADABLE" in err, err)
        self.assertEqual(self.ledger_lines(), before)

class StoryAndPriorityTest(TasksBase):
    """`continues` and `priority` — the two fields 40% and 19% of open rows
    were already smuggling in free text. Every arm asserts its own premise
    before its subject."""

    def test_a_declared_parent_is_stored_and_walks_to_its_root(self):
        parent, err = tasks.add("the parent story", owner="seat-a")
        self.assertIsNone(err, err)
        child, err = tasks.add("the continuation", owner="seat-a",
                               continues=parent["id"])
        self.assertIsNone(err, err)
        # THE EFFECT, not the absence of a complaint: the row carries it.
        self.assertEqual(parent["id"], child["continues"])
        known = {parent["id"]: parent, child["id"]: child}
        self.assertEqual(parent["id"], tasks._story_root(known, child["id"]))
        # and a row that continues NOTHING is its own root — the control that
        # stops _story_root passing by always returning the argument's parent
        self.assertEqual(parent["id"], tasks._story_root(known, parent["id"]))

    def test_an_unverifiable_parent_is_refused_by_its_own_name(self):
        """THE MUST-MISS SET. Each refusal is a DIFFERENT fact and says so,
        because a caller who cannot tell them apart cannot fix the right one.
        A dangling parent is the prose-reference this field exists to replace,
        so storing one hopefully would reintroduce the defect in a typed
        costume."""
        real, err = tasks.add("real row", owner="seat-a")
        self.assertIsNone(err, err)
        _, unknown = tasks.add("child", owner="seat-a",
                               continues="task/999999")
        self.assertIsNotNone(unknown)
        self.assertIn("not in the ledger", unknown)
        known = {real["id"]: real}
        self.assertIn("ITSELF", tasks._story_error(known, real["id"],
                                                   real["id"]) or "")
        # a ring: b continues a, so a may not continue b
        b, err = tasks.add("b", owner="seat-a", continues=real["id"])
        self.assertIsNone(err, err)
        ring = {real["id"]: real, b["id"]: b}
        self.assertIn("CYCLE", tasks._story_error(ring, real["id"],
                                                  b["id"]) or "")
        # POSITIVE CONTROL on the same observable: a well-formed parent is
        # accepted by the very predicate that just refused three others, so
        # the refusals are discrimination rather than a blanket no.
        self.assertIsNone(tasks._story_error(ring, "task/new", real["id"]))

    def test_unranked_is_a_value_and_an_eighth_spelling_is_refused(self):
        """Seven priority spellings were already loose in free text — P0, P1,
        P2, BLOCKER, URGENT, CRITICAL, LOW PRIORITY. A field that accepts an
        eighth has bought nothing, and UNRANKED must never render as P3."""
        ranked, err = tasks.add("ranked", owner="seat-a", priority="P0")
        self.assertIsNone(err, err)
        self.assertEqual("P0", ranked["priority"])
        plain, err = tasks.add("unranked", owner="seat-a")
        self.assertIsNone(err, err)
        # ABSENT, not defaulted: nobody has judged this, which is not "low".
        self.assertIsNone(plain["priority"])
        for spelling in ("URGENT", "BLOCKER", "P9", "high"):
            with self.subTest(spelling):
                _, bad = tasks.add("x", owner="seat-a", priority=spelling)
                self.assertIsNotNone(bad, spelling)
                self.assertIn("unknown priority", bad)

    def test_both_fields_are_MUTABLE_because_triage_is_re_parenting(self):
        """The owner's correction, and the whole point: a parent settable only
        at filing time is decoration, since the shape is not known when a row
        is filed. `update` must be able to pull a row under a parent, promote
        it back out, and re-rank it."""
        a, _ = tasks.add("story a", owner="seat-a")
        b, _ = tasks.add("independent", owner="seat-a")
        self.assertIsNone(b["continues"])          # the arm's own premise
        moved, err = tasks.update(b["id"], continues=a["id"], priority="P1",
                                  rank_actor=self.admit())
        self.assertIsNone(err, err)
        self.assertEqual(a["id"], moved["continues"])
        self.assertEqual("P1", moved["priority"])
        # PROMOTED BACK OUT — the reverse direction, which a one-way field
        # would fail while still passing the test above.
        out, err = tasks.update(b["id"], continues=None)
        self.assertIsNone(err, err)
        self.assertIsNone(out["continues"])


class BothDoorsEnforceTest(TasksBase):
    """`update` must refuse everything `add` refuses. This file already states
    the law — "a closed set enforced at add() and open at update() is not a
    closed set" — and the first draft of these fields violated it: update
    accepted a dangling parent, a CYCLE, and the priority "URGENT".

    IT MATTERS MORE FOR THESE TWO FIELDS THAN FOR ANY OTHER, because they are
    mutable on purpose. Triage re-parents and re-ranks, so update() is the
    PRIMARY door — validation at add() alone would guard the path nobody
    takes."""

    def test_update_refuses_what_add_refuses(self):  # noqa: VACUOUS_ASSERTION — the loop asserts refusals; the unconditional control on the same door is the assertEqual(a["id"], b["continues"]) premise above it, which proves add() ACCEPTS a real re-parent
        # TITLES A LEDGER COULD HOLD, not placeholder letters. `add()`
        # now resolves a new title against the open backlog and refuses
        # a title with no comparable words at all — measured on the
        # live ledger, that refuses exactly ONE real row of 2644, whose
        # title is three dots — so a fixture spelling its rows "a" and
        # "b" was testing a world this ledger does not have.
        a, _ = tasks.add("parent story about the signer", owner="s")
        b, _ = tasks.add("child rung under the signer story", owner="s",
                         continues=a["id"])
        self.assertEqual(a["id"], b["continues"])       # the arm's premise
        for label, kw, needle in (
                ("dangling", {"continues": "task/999999"}, "not in the ledger"),
                ("cycle", {"continues": b["id"]}, "CYCLE"),
                ("priority", {"priority": "URGENT"}, "unknown priority")):
            with self.subTest(label):
                row, err = tasks.update(a["id"], **kw)
                self.assertIsNotNone(err, label)
                self.assertIn(needle, err)
                self.assertIsNone(row)

    def test_the_cycle_case_is_reachable_ONLY_through_update(self):
        """At add() a brand-new id cannot yet be anyone's ancestor, so a ring
        is only constructible by RE-PARENTING. An implementation that guarded
        add() alone would cover the impossible case and miss the real one."""
        a, _ = tasks.add("parent story about the signer", owner="s")
        b, err = tasks.add("child rung under the signer story", owner="s",
                           continues=a["id"])
        self.assertIsNone(err, err)                     # add cannot make a ring
        _, ring = tasks.update(a["id"], continues=b["id"])
        self.assertIsNotNone(ring)
        self.assertIn("CYCLE", ring)

    def test_the_legitimate_triage_moves_still_work(self):
        """MUST-HIT beside the must-misses: the cure must not be a blanket no.
        Re-parent, re-rank, and promote back out are the operations the field
        exists for."""
        a, _ = tasks.add("parent story about the signer", owner="s")
        c, _ = tasks.add("a separate rung about the proxy pool", owner="s")
        moved, err = tasks.update(c["id"], continues=a["id"], priority="P1",
                                  rank_actor=self.admit())
        self.assertIsNone(err, err)
        self.assertEqual((a["id"], "P1"),
                         (moved["continues"], moved["priority"]))
        out, err = tasks.update(c["id"], continues=None)
        self.assertIsNone(err, err)
        self.assertIsNone(out["continues"])


class TypedFieldDoorTest(TasksBase):
    """Every must-miss an earlier cut shipped fed these validators a
    WELL-FORMED STRING, so the arms and the implementation shared a blind spot
    BY CONSTRUCTION: nothing proved what happens when a caller hands these
    fields something that is not a string at all.

    The CLI cannot produce that input — argv is strings — which is exactly why
    argv-driven arms are silent here. helm calls `add`/`update` directly from
    more places than it calls the CLI, and the two failures behind the gap are
    opposite in shape, which is why one arm each."""

    def test_a_truthy_non_string_is_REFUSED_not_crashed(self):  # noqa: VACUOUS_ASSERTION — the control is unconditional at the `ok, err = tasks.add(...continues=parent["id"])` three lines below, on the same door; the rung cannot see that `ok` and the loop's `row` are one observable because they are different names
        """The crash case. `_story_error` asks `parent not in known` against a
        dict, so an unhashable argument raises TypeError out of the function
        whose whole job is returning refusals."""
        parent, err = tasks.add("real parent", owner="s")
        self.assertIsNone(err, err)
        # POSITIVE CONTROL FIRST AND UNCONDITIONALLY, before any subTest loop.
        # Behind a loop it proves nothing if the loop never runs, and the
        # vacuous-assertion rung is right to say so: a refusal arm whose
        # control cannot execute is a blanket-no wearing discrimination.
        ok, err = tasks.add("good child", owner="s", continues=parent["id"])
        self.assertIsNone(err, err)
        self.assertEqual(parent["id"], ok["continues"])
        for bad in (["task/1"], {"id": "task/1"}):
            with self.subTest(repr(bad)):
                row, why = tasks.add("child", owner="s", continues=bad)
                self.assertIsNone(row)
                self.assertIn("must be a string", why)
                self.assertIn(type(bad).__name__, why)

    def test_a_FALSY_non_string_is_refused_rather_than_filed_as_absent(self):
        """The silent case, and the worse one. `continues or None` maps 0,
        False and [] onto None, so the row stores "no parent" — an answer the
        caller never gave and which no later reader can distinguish from a row
        that genuinely continues nothing. That is absence and cannot-look
        sharing one representation, inside the field built to end it."""
        for bad in (0, False, [], {}):
            with self.subTest(repr(bad)):
                row, why = tasks.add("child", owner="s", continues=bad)
                self.assertIsNone(row, "filed silently: %r" % (row,))
                self.assertIn("must be a string", why)
        # and the SAME input at the same door for priority
        row, why = tasks.add("child", owner="s", priority=0)
        self.assertIsNone(row)
        self.assertIn("must be a string", why)

    def test_update_refuses_the_same_types_add_refuses(self):
        """update() is the PRIMARY door for these fields — triage re-parents —
        so a type guard living only at add() guards the path nobody takes."""
        a, _ = tasks.add("parent story about the signer", owner="s")
        b, _ = tasks.add("a separate rung about the proxy pool", owner="s")
        # POSITIVE CONTROL FIRST, UNCONDITIONALLY: this door accepts a real
        # re-parent and a real rank. Everything after it is discrimination
        # rather than a door that refuses whatever it is handed.
        good, err = tasks.update(b["id"], continues=a["id"], priority="P2",
                                 rank_actor=self.admit())
        self.assertIsNone(err, err)
        self.assertEqual((a["id"], "P2"),
                         (good["continues"], good["priority"]))
        # MUST-HIT: None is a DELIBERATE promote-to-root and must survive a
        # guard that refuses every other non-string.
        out, err = tasks.update(b["id"], continues=None)
        self.assertIsNone(err, err)
        self.assertIsNone(out["continues"])
        for field, bad in (("continues", ["task/1"]), ("continues", 0),
                           ("priority", False), ("priority", ["P0"])):
            with self.subTest("%s=%r" % (field, bad)):
                row, why = tasks.update(a["id"], **{field: bad})
                self.assertIsNone(row)
                self.assertIn("must be a string", why)


class PreExistingRingTest(TasksBase):
    """The fourth refusal, which _story_error's docstring promised to
    enumerate and did not. No arm in the original set could have caught it,
    because every one of them built its ring THROUGH the row under test.

    A ledger written by an older helm can already contain a ring. Walking
    into one while validating an INNOCENT write is a different fact from the
    write closing a ring itself, and answering both with "would form a CYCLE"
    sends the reader to repair a row that is fine."""

    def _ringed_ledger(self):
        """b -> c -> b, the ring an older helm could have written, with `a`
        outside it entirely."""
        return {"task/a": {"id": "task/a", "continues": None},
                "task/b": {"id": "task/b", "continues": "task/c"},
                "task/c": {"id": "task/c", "continues": "task/b"}}

    def test_a_ring_that_predates_the_write_is_NOT_blamed_on_the_write(self):
        known = self._ringed_ledger()
        why = tasks._story_error(known, "task/a", "task/b")
        self.assertIsNotNone(why, "walking into a ring must still refuse")
        # THE POINT OF THE ARM: it must not accuse this write of forming one.
        self.assertNotIn("would form a CYCLE", why)
        self.assertIn("ALREADY contains a ring", why)
        # and it must name a row INSIDE the ring, not the innocent writer
        self.assertNotIn("task/a", why)

    def test_a_write_that_really_closes_a_ring_still_says_CYCLE(self):
        """MUST-MISS for the cure: a fix that simply stopped saying CYCLE
        would pass the arm above. This is the input that must still get the
        original answer."""
        known = {"task/a": {"id": "task/a", "continues": None},
                 "task/b": {"id": "task/b", "continues": "task/a"}}
        why = tasks._story_error(known, "task/a", "task/b")
        self.assertIsNotNone(why)
        self.assertIn("would form a CYCLE", why)

    def test_the_cycle_path_is_DETERMINISTIC(self):
        """It rendered a SET, so the same defect printed a different
        arrow-chain on different runs and no two reports could be compared."""
        known = {"task/a": {"id": "task/a", "continues": None},
                 "task/b": {"id": "task/b", "continues": "task/a"},
                 "task/c": {"id": "task/c", "continues": "task/b"}}
        answers = {tasks._story_error(known, "task/a", "task/c")
                   for _ in range(8)}
        self.assertEqual(1, len(answers), answers)
        self.assertIn("task/a -> task/c -> task/b -> task/a", answers.pop())

    def test_story_root_survives_the_same_ringed_ledger(self):
        """Its docstring claims defence against a ring written by an older
        helm; nothing exercised that claim."""
        known = self._ringed_ledger()
        self.assertEqual("task/a", tasks._story_root(known, "task/a"))
        # inside the ring it must TERMINATE rather than hang
        self.assertIn(tasks._story_root(known, "task/b"),
                      ("task/b", "task/c"))


class PreExistingDanglingTest(TasksBase):
    """The FIFTH refusal, found by review: the walk already returned
    dangling_at and _story_error DISCARDED it, so a write into a chain whose
    ancestor names a missing row was ACCEPTED — filing a story rooted at a
    row nobody can read, which is exactly what the UNKNOWN clause refuses one
    hop up. Symmetric with PreExistingRingTest: the write is innocent, the
    refusal must say so and name the broken row."""

    def _dangling_ledger(self):
        """b -> gone, with `a` outside the damage."""
        return {"task/a": {"id": "task/a", "continues": None},
                "task/b": {"id": "task/b", "continues": "task/gone"}}

    def test_a_write_into_a_dangling_chain_is_refused_and_names_the_hole(self):
        known = self._dangling_ledger()
        why = tasks._story_error(known, "task/a", "task/b")
        self.assertIsNotNone(why, "walking into a dangle must refuse")
        self.assertIn("DANGLES", why)
        self.assertIn("task/gone", why)
        # the innocent writer is not the row to repair
        self.assertNotIn("repair task/a", why)

    def test_a_clean_chain_still_accepts_the_same_write(self):
        """MUST-MISS: a validator that refused every deep chain would pass
        the arm above while killing the feature."""
        # POSITIVE CONTROL, unconditional, same observable: this validator
        # DOES refuse things — without it, a _story_error returning None for
        # every input satisfies the assertion below while entirely dead.
        self.assertIsNotNone(
            tasks._story_error(self._dangling_ledger(), "task/x", "task/b"))
        known = {"task/root": {"id": "task/root", "continues": None},
                 "task/b": {"id": "task/b", "continues": "task/root"}}
        self.assertIsNone(tasks._story_error(known, "task/a", "task/b"))

    def test_the_DOOR_refuses_it_too_not_just_the_helper(self):
        """A probe-proven defect is closed by a probe at the same door the
        defect used: update() re-parenting onto the damaged chain. The damage
        is written RAW, the way an older helm left it — both validating doors
        now refuse to create it, so no door can build this fixture."""
        # the DEFAULT ledger throughout — self.file() writes self.path, a
        # DIFFERENT file than the no-path doors below read, and the first
        # cut split the fixture across the two. add() first so the default
        # ledger and its directory exist.
        d, err = tasks.add("d", owner="s")
        self.assertIsNone(err, err)
        legacy = {"id": "task/legacy", "title": "written by an older helm",
                  "status": "open", "continues": "task/ghost"}
        with open(tasks.ledger_path(), "a", encoding="utf-8") as f:
            f.write(json.dumps(legacy) + "\n")
        # PREMISE CONTROL: the raw line is a live row and its parent is not.
        known = tasks.rows()
        self.assertIn("task/legacy", known)
        self.assertNotIn("task/ghost", known)
        row, err = tasks.update(d["id"], continues="task/legacy")
        self.assertIsNone(row)
        self.assertIn("DANGLES", err)
        self.assertIn("task/ghost", err)


class TriageReachesTheCliTest(CliBase):
    """The fields were stored and the PRODUCTION door refused them. `--continues`/`--priority` reached _VALUED_FLAGS and
    update()'s `allowed` tuple and never the CLI `pairs` tuple, so the API
    accepted a re-parent that `helm task update` rejected.

    The whole promise of the field is that triage can re-parent LATER, and
    `update` is the only surface an operator has for "later" — so this is the
    difference between a feature and a stored column."""

    def test_update_re_parents_and_re_ranks_through_the_CLI(self):
        rc, _, err = self.cli("add", "the parent story")
        self.assertEqual(0, rc, err)
        rc, _, err = self.cli("add", "the child")
        self.assertEqual(0, rc, err)
        parent, child = self.filed("the parent story"), self.filed("the child")
        self.assertIsNone(child["continues"])        # the arm's own premise
        self.admit()                    # a rank is an act with an actor
        rc, _, err = self.cli("update", child["id"],
                              "--continues", parent["id"], "--priority", "P1")
        self.assertEqual(0, rc, err)
        moved = self.filed("the child")
        self.assertEqual(parent["id"], moved["continues"])
        self.assertEqual("P1", moved["priority"])

    def test_an_empty_value_promotes_a_row_back_out(self):  # noqa: VACUOUS_ASSERTION — assertIsNone(continues) is the empty observable; the unconditional control on the SAME field is the assertEqual two lines above, which proves the field was populated before it was cleared
        """The reverse direction needs a spelling too. A bare `--continues` is
        refused (below), so `--continues=` is the only way an operator can say
        "this row is its own story again" — and a one-way field would pass
        every arm above while making triage irreversible."""
        parent, child = "the signer export rung", "the proxy pool rung"
        rc, _, err = self.cli("add", parent)
        self.assertEqual(0, rc, err)
        rc, _, err = self.cli("add", child, "--continues",
                              self.filed(parent)["id"])
        self.assertEqual(0, rc, err)
        self.assertEqual(self.filed(parent)["id"],
                         self.filed(child)["continues"])
        rc, _, err = self.cli("update", self.filed(child)["id"], "--continues=")
        self.assertEqual(0, rc, err)
        self.assertIsNone(self.filed(child)["continues"])

    def test_a_bare_flag_is_REFUSED_at_both_doors_not_filed_as_null(self):  # noqa: VACUOUS_ASSERTION — `assertGreater(before, empty)` is the unconditional control on the SAME observable: it proves ledger_lines() moves on a real file before any assertion that it stayed still
        """`_take` returns None for a valueless flag and the coercion mapped
        that onto None — byte-identical to never passing it — so a bare
        `--continues` filed a parentless row and reported SUCCESS."""
        # THE COUNTER MUST BE PROVEN TO MOVE BEFORE ITS STILLNESS MEANS
        # ANYTHING. Asserting before == after is satisfied by a ledger that
        # never receives a write at all, so this arm would pass against a
        # totally broken `add` — the vacuous-assertion rung caught exactly
        # that and it was right.
        # THE COUNTER IS READ AFTER A PROVEN WRITE, never before one: the
        # ledger FILE does not exist until the first row lands, so reading it
        # for a zero baseline raises FileNotFoundError instead of measuring
        # emptiness. `> 0` is the same control — it proves this door writes,
        # so the stillness asserted below is a real refusal and not a ledger
        # that never receives anything.
        rc, _, err = self.cli("add", "a row that really files")
        self.assertEqual(0, rc, err)
        before = self.ledger_lines()
        self.assertGreater(before, 0, "the ledger never received the row")
        rc, _, err = self.cli("add", "orphan", "--continues")
        self.assertEqual(2, rc)
        self.assertIn("--continues needs a value", err)
        # NOT MERELY A COMPLAINT — assert nothing was written.
        self.assertEqual(before, self.ledger_lines())
        rc, _, err = self.cli("add", "unranked", "--priority")
        self.assertEqual(2, rc)
        self.assertIn("--priority needs a value", err)
        self.assertEqual(before, self.ledger_lines())
        # the update door inherits the same guard from the same `pairs` tuple
        rc, _, err = self.cli("add", "real")
        self.assertEqual(0, rc, err)
        rc, _, err = self.cli("update", self.filed("real")["id"], "--priority")
        self.assertEqual(2, rc)
        self.assertIn("--priority needs a value", err)

    def test_the_cli_refuses_an_eighth_spelling_through_the_real_door(self):  # noqa: VACUOUS_ASSERTION — `--priority P0` is filed and read back, and `assertGreater(before, empty)` proves the counter moves, both unconditional and both on the same observables the refusal then asserts unchanged
        """add()'s arm proved the API refuses URGENT. This proves the operator
        who types it gets that refusal rather than a stored surprise."""
        # POSITIVE CONTROL ON THE SAME OBSERVABLE: a rank this door DOES take,
        # proving the counter moves and that P-spellings are not refused
        # wholesale. Without it, "nothing was written" is true of a door that
        # writes nothing ever.
        # SAME ORDER, SAME REASON as the arm above: a proven write first,
        # then the baseline. A rank this door DOES take also proves the
        # refusal below is discrimination and not a blanket no.
        rc, _, err = self.cli("add", "ranked", "--priority", "P0")
        self.assertEqual(0, rc, err)
        self.assertEqual("P0", self.filed("ranked")["priority"])
        before = self.ledger_lines()
        self.assertGreater(before, 0, "the ledger never received the row")
        rc, _, err = self.cli("add", "x", "--priority", "URGENT")
        # 2, not 1: tasks.py:1554 returns 2 for EVERY add() refusal, which is
        # the same code the valueless guard uses. Matching the door rather
        # than my expectation of it.
        self.assertEqual(2, rc)
        self.assertIn("unknown priority", err)
        self.assertEqual(before, self.ledger_lines())


class SchemaDrivesEveryDoorTest(TasksBase):
    """THE ARM THAT KILLS THE CLASS RATHER THAN AN INSTANCE.

    These fields were registered by hand in seven places and reached four;
    the correction reached six. Each miss was a real shipped defect — a flag
    the production door refused, a help text that denied the flag existed.
    Hand-maintained door lists do not converge, so these arms fail the moment
    a door stops deriving from STORY_FIELDS.

    THE CLAIM, STATED NARROWLY BECAUSE ONE DOOR CANNOT DERIVE: the parser,
    the help text, update()'s allowed set and typed loop, and _row's storage
    all DERIVE from the schema. add()'s keyword signature structurally
    cannot — a Python signature is not a runtime table — so it and _row
    COUPLE via zip-dicts whose [] lookup raises KeyError the moment the
    schema outruns the threading. A third field is therefore one schema
    line plus one keyword threaded through add/_row, and forgetting the
    thread is a LOUD crash at the door, never a silent drop. The alarm is
    pinned below as a contract, not tolerated as a bug."""

    def test_the_default_ledger_is_NOT_the_one_these_arms_write(self):
        """THE ARM THAT SHOULD HAVE EXISTED BEFORE THE OTHERS. This class
        inherited plain TestCase, so it had no redirected HELM_HOME and its
        add/update calls resolved ledger_path() to the OPERATOR'S REAL LEDGER.
        A remote runner happened to isolate it; the source did not. A test
        that writes to production when run the ordinary way is a defect
        whether or not it has fired yet, and 'it did not happen to fire'
        is not a property anyone can rely on twice."""
        self.assertIn(self.tmp, tasks.ledger_path())
        before = tasks.rows(path=tasks.ledger_path())
        row, err = tasks.add("isolation probe", owner="s")
        self.assertIsNone(err, err)
        after = tasks.rows(path=tasks.ledger_path())
        self.assertEqual(len(before) + 1, len(after))
        self.assertNotIn("/.helm/_global/", tasks.ledger_path())

    def test_every_schema_flag_reaches_the_parser_and_the_help(self):
        self.assertTrue(tasks.STORY_FLAGS, "the schema is empty")
        for flag in tasks.STORY_FLAGS:
            with self.subTest(flag):
                self.assertIn(flag, tasks._VALUED_FLAGS)
                self.assertIn(flag, tasks.USAGE)

    def test_every_schema_key_is_mutable_through_update(self):  # noqa: VACUOUS_ASSERTION — assertIsNotNone(refused) above the loop is the unconditional control on the SAME observable: it proves this door returns errors before any assertion that it returned none
        """A field parseable at add and rejected by update is the exact defect
        that shipped: stored, and unreachable by the verb triage uses."""
        seed, err = tasks.add("seed", owner="s")
        self.assertIsNone(err, err)
        # POSITIVE CONTROL, unconditional: this door DOES return errors, so a
        # later assertIsNone(err) is a measurement and not a door that never
        # speaks.
        _r, refused = tasks.update(seed["id"], status="not-a-status")
        self.assertIsNotNone(refused, "update never returns an error at all")
        # ONE TITLE PER ITERATION, because the second `add("x")` is now an
        # exact duplicate of the first and `add()` refuses it. The loop was
        # never about filing the same row twice; the repeated literal was an
        # accident of a fixture that did not have to care.
        for _flag, key, _coerce in tasks.STORY_FIELDS:
            with self.subTest(key):
                row, err = tasks.add("a mutable row for %s" % key, owner="s")
                self.assertIsNone(err, err)
                _r, err = tasks.update(row["id"], **{key: None})
                self.assertIsNone(err, "update refuses %s" % key)

    def test_the_API_doors_read_STORY_KEYS_not_a_hand_list(self):
        """THE ARM THAT KILLS THE CLASS AT THE API DOORS TOO. Review measured
        STORY_KEYS defined and referenced NOWHERE: add() and update() carried
        their own ("continues", "priority") tuples, so this class's rationale
        — hand lists do not converge — was true of the exact doors it claimed
        to cover. Discriminates by extending the schema at runtime: a door
        deriving from STORY_KEYS admits the new key into its allowed set and
        TYPED-refuses its bad value; a hand-listed door answers unknown-field.
        add() couples instead of deriving — see the class docstring and the
        drift-alarm arm below."""
        seed, err = tasks.add("seed", owner="s")
        self.assertIsNone(err, err)
        prior = tasks.STORY_KEYS
        tasks.STORY_KEYS = prior + ("flavor",)
        try:
            row, why = tasks.update(seed["id"], flavor=["not", "a", "str"])
        finally:
            tasks.STORY_KEYS = prior
        self.assertIsNone(row)
        self.assertNotIn("unknown field", why or "",
                         "update() is not reading STORY_KEYS")
        self.assertIn("flavor must be a string", why)
        # MUST-MISS: with the schema back to normal the closed set holds —
        # the arm must not have proven the door open to anything.
        row, why = tasks.update(seed["id"], flavor="x")
        self.assertIsNone(row)
        self.assertIn("unknown field", why)

    def test_a_schema_key_add_cannot_thread_is_a_LOUD_crash_not_a_drop(self):
        """The drift alarm, pinned as the contract it is. add()'s signature
        cannot derive from a runtime table, so its coupling MUST fail the
        moment STORY_FIELDS outruns it — a KeyError at the door. The silent
        alternative (.get) would file the row while dropping the new field:
        absence and cannot-store sharing one representation, in the module
        built to end exactly that."""
        prior = tasks.STORY_KEYS
        tasks.STORY_KEYS = prior + ("flavor",)
        try:
            with self.assertRaises(KeyError):
                tasks.add("drift probe", owner="s")
        finally:
            tasks.STORY_KEYS = prior
        # MUST-MISS: the normal schema files cleanly through the same door.
        row, err = tasks.add("drift control", owner="s")
        self.assertIsNone(err, err)

    def test_storage_derives_from_the_schema_and_is_explicit_from_birth(self):
        """_row was the seventh hand-list: parser, help, and update derived
        while storage still spelled the keys by hand, so a third field would
        have parsed, validated, and then not been STORED. Every schema key
        is present on a fresh row even when omitted — a reader must never
        guess whether a field predates the feature."""
        self.assertTrue(tasks.STORY_KEYS, "the schema is empty")
        row, err = tasks.add("bare", owner="s")
        self.assertIsNone(err, err)
        for key in tasks.STORY_KEYS:
            with self.subTest(key):
                self.assertIn(key, row)
                self.assertIsNone(row[key])
        # and a given value survives storage through the same derivation
        child, err = tasks.add("child", owner="s",
                               continues=row["id"], priority="P1")
        self.assertIsNone(err, err)
        self.assertEqual(row["id"], child["continues"])
        self.assertEqual("P1", child["priority"])


class ListReadsTheLedgerOnceTest(CliBase):
    """THE LEGEND'S OWN PROMISE, armed. The footer comment says "same
    SNAPSHOT the listing rendered (no second read to drift between list and
    legend)" — and the default arm called open_rows(), a second full ledger
    read, after snapshot(). A row filed between the two reads appeared in one
    surface and not the other. Review named it; this counts the reads."""

    def test_list_performs_exactly_one_ledger_read(self):
        rc, _, err = self.cli("add", "a visible row")
        self.assertEqual(0, rc, err)
        from helm import eventledger
        calls, real = [], eventledger.latest_checked
        eventledger.latest_checked = (
            lambda *a, **k: calls.append(a) or real(*a, **k))
        try:
            rc, out, err = self.cli("list")
        finally:
            eventledger.latest_checked = real
        self.assertEqual(0, rc, err)
        self.assertIn("a visible row", out)   # the read actually happened
        # scoped to THIS ledger: the project-scope lens may read other stores
        # through the same seam, and those reads are not the drift window.
        mine = [a for a in calls if a and a[0] == tasks.ledger_path()]
        self.assertTrue(mine, "the counter never saw the tasks ledger at all")
        self.assertEqual(1, len(mine),
                         "list read the tasks ledger %d times" % len(mine))


class UnparseableIsNotAClearTest(CliBase):
    """A TYPO MUST NOT DESTROY A TRIAGE DECISION AND REPORT SUCCESS.

    normalize_id maps an unparseable token to empty, and the CLI spells
    promote-this-row-out as an empty value. Collapsed, `--continues=bogus`
    SUCCEEDED and CLEARED an existing parent. That is absence and cannot-parse
    sharing one representation, inside the field built to end it, and the
    promote-out spelling is what created it."""

    def test_garbage_refuses_and_leaves_the_parent_intact(self):  # noqa: VACUOUS_ASSERTION — the parent is read back and asserted EQUAL to a live id both before and after the refusal, so the observable is proven non-empty by the same assertion that later proves it unchanged
        rc, _, err = self.cli("add", "parent")
        self.assertEqual(0, rc, err)
        pid = self.filed("parent")["id"]
        rc, _, err = self.cli("add", "child", "--continues", pid)
        self.assertEqual(0, rc, err)
        self.assertEqual(pid, self.filed("child")["continues"])   # premise
        cid = self.filed("child")["id"]
        rc, _, err = self.cli("update", cid, "--continues=not-an-id")
        self.assertEqual(2, rc, "garbage was accepted")
        self.assertIn("not a task id", err)
        # THE EFFECT, not the absence of a complaint: the parent SURVIVED.
        self.assertEqual(pid, self.filed("child")["continues"])

    def test_an_EMPTY_value_still_promotes_out(self):  # noqa: VACUOUS_ASSERTION — assertIsNotNone on the same field immediately before the promote-out is the unconditional control; the None afterwards is the effect being measured
        """MUST-MISS for the cure above: a fix that simply refused every
        unparseable-looking value would take the promote-out spelling with it,
        and triage would become one-way."""
        parent, child = "the signer export rung", "the proxy pool rung"
        rc, _, err = self.cli("add", parent)
        self.assertEqual(0, rc, err)
        rc, _, err = self.cli("add", child, "--continues",
                              self.filed(parent)["id"])
        self.assertEqual(0, rc, err)
        self.assertIsNotNone(self.filed(child)["continues"])       # premise
        rc, _, err = self.cli("update", self.filed(child)["id"], "--continues=")
        self.assertEqual(0, rc, err)
        self.assertIsNone(self.filed(child)["continues"])

    def test_garbage_is_refused_at_the_add_door_too(self):
        before = None
        rc, _, err = self.cli("add", "real")
        self.assertEqual(0, rc, err)
        before = self.ledger_lines()
        self.assertGreater(before, 0)
        rc, _, err = self.cli("add", "bad", "--continues", "not-an-id")
        self.assertEqual(2, rc)
        self.assertIn("not a task id", err)
        self.assertEqual(before, self.ledger_lines())


class StoryChainTest(TasksBase):
    """ONE WALK. Two divergent walks over one graph is how a writer and a
    reader came to disagree about a ring, and neither could be made defensive
    without re-deriving the other's edge cases."""

    def test_a_rooted_chain_reports_its_path_and_root(self):
        known = {"task/a": {"id": "task/a", "continues": None},
                 "task/b": {"id": "task/b", "continues": "task/a"},
                 "task/c": {"id": "task/c", "continues": "task/b"}}
        path, root, ring, dangling = tasks._story_chain(known, "task/c")
        self.assertEqual(["task/c", "task/b", "task/a"], path)
        self.assertEqual("task/a", root)
        self.assertIsNone(ring)
        self.assertIsNone(dangling)

    def test_a_ring_reports_ring_at_and_NO_root(self):
        """root and ring_at are alternatives — a ring has no root, and a
        walker that returned both would let a caller believe it had one."""
        known = {"task/b": {"id": "task/b", "continues": "task/c"},
                 "task/c": {"id": "task/c", "continues": "task/b"}}
        path, root, ring, dangling = tasks._story_chain(known, "task/b")
        self.assertIsNone(root)
        self.assertEqual("task/b", ring)
        self.assertIn("task/b", path)
        self.assertIsNone(dangling)

    def test_a_non_string_link_ENDS_the_walk_instead_of_raising(self):
        """`cur in seen` and `known.get(cur)` both raise TypeError on an
        unhashable argument, and a walker that raises cannot report. Absence,
        an unreadable row and a ring are three answers, none an exception."""
        known = {"task/a": {"id": "task/a", "continues": ["task/b"]},
                 "task/z": {"id": "task/z", "continues": 0},
                 "task/ok": {"id": "task/ok", "continues": "task/a"}}
        # POSITIVE CONTROL, unconditional and on the same observable: a STRING
        # link is followed. Without it, "the walk stopped" is satisfied by a
        # walk that never moves.
        path, _r, _g, _d = tasks._story_chain(known, "task/ok")
        self.assertEqual(["task/ok", "task/a"], path)
        for start in ("task/a", "task/z"):
            with self.subTest(start):
                path, root, ring, dangling = tasks._story_chain(known, start)
                self.assertEqual([start], path)
                self.assertEqual(start, root)
                self.assertIsNone(ring)
                self.assertIsNone(dangling)

    def test_DANGLING_is_a_third_answer_and_not_a_root(self):
        """The failure that produced the fourth return value. Naming an
        unreadable id as the ROOT would group live rows under something no
        reader can display; dropping the fact entirely would make a dangling
        chain indistinguishable from a rooted one — the same collapse this
        field exists to end, one level down."""
        known = {"task/a": {"id": "task/a", "continues": "task/gone"}}
        path, root, ring, dangling = tasks._story_chain(known, "task/a")
        self.assertEqual(["task/a"], path, "path must hold readable ids only")
        self.assertEqual("task/a", root, "the unreadable id is not the root")
        self.assertEqual("task/gone", dangling)
        self.assertIsNone(ring)

    def test_a_ROOTED_chain_reports_no_dangling(self):
        """MUST-MISS for the arm above: a walk that reported dangling on every
        terminus would pass it while destroying the distinction."""
        known = {"task/a": {"id": "task/a", "continues": None}}
        _p, root, ring, dangling = tasks._story_chain(known, "task/a")
        self.assertEqual("task/a", root)
        self.assertIsNone(dangling)
        self.assertIsNone(ring)


class StoryOrderTest(TasksBase):
    """The reader that makes the fields mean something. A parent link nothing
    renders is a parent link nobody sorts by."""

    def _rows(self, *specs):
        return [{"id": i, "continues": c, "priority": p, "title": i}
                for i, c, p in specs]

    def test_children_follow_their_parent_and_are_indented(self):
        rows = self._rows(("task/1", None, None), ("task/2", "task/1", None),
                          ("task/3", None, None))
        got = tasks._story_order(rows)
        ids = [r["id"] for r, _i in got]
        self.assertEqual(ids.index("task/2"), ids.index("task/1") + 1)
        indents = {r["id"]: i for r, i in got}
        self.assertEqual("", indents["task/1"])
        self.assertEqual("  ", indents["task/2"])
        self.assertEqual("", indents["task/3"])

    def test_a_ranked_story_sorts_above_an_unranked_one(self):
        rows = self._rows(("task/1", None, None), ("task/2", None, "P0"))
        ids = [r["id"] for r, _i in tasks._story_order(rows)]
        self.assertEqual(["task/2", "task/1"], ids)

    def test_a_P0_SUBISSUE_lifts_its_whole_story(self):
        """A story ranks by its BEST member, or a P0 subissue sorts below
        unranked work because its parent happens to carry no rank."""
        rows = self._rows(("task/1", None, None), ("task/2", "task/1", "P0"),
                          ("task/9", None, "P2"))
        ids = [r["id"] for r, _i in tasks._story_order(rows)]
        self.assertEqual(["task/1", "task/2", "task/9"], ids)

    def test_UNRANKED_is_not_treated_as_P3(self):
        rows = self._rows(("task/1", None, None), ("task/2", None, "P3"))
        ids = [r["id"] for r, _i in tasks._story_order(rows)]
        self.assertEqual(["task/2", "task/1"], ids)

    def test_a_RING_costs_indentation_and_NEVER_visibility(self):
        """A ring makes every member somebody's child, so a roots-first walk
        emits NONE of them — a silent drop, in the renderer for the field
        whose validator refuses rings. The count is the assertion."""
        rows = self._rows(("task/1", "task/2", None), ("task/2", "task/1", None),
                          ("task/3", None, None))
        got = tasks._story_order(rows)
        self.assertEqual(3, len(got), "a ring dropped rows from the listing")
        self.assertEqual({"task/1", "task/2", "task/3"},
                         {r["id"] for r, _i in got})

    def test_a_parent_outside_the_shown_set_renders_flat(self):
        """Depth is computed over the SHOWN rows only: indenting under
        something the reader cannot see reads as a bug, not as scope."""
        rows = self._rows(("task/2", "task/999", None))
        got = tasks._story_order(rows)
        self.assertEqual([("task/2", "")], [(r["id"], i) for r, i in got])

    def test_every_input_row_is_emitted_exactly_once(self):
        rows = self._rows(("task/1", None, None), ("task/2", "task/1", "P1"),
                          ("task/3", "task/2", None), ("task/4", None, "P0"))
        self.assertEqual(4, len(rows), "the fixture itself is the control")
        got = tasks._story_order(rows)
        self.assertEqual(len(rows), len(got))
        self.assertEqual(len(rows), len({r["id"] for r, _i in got}))

    def test_an_unhashable_id_costs_indentation_and_NEVER_the_listing(self):
        """The renderer inherits the walk's obligation. _story_chain ends on
        a non-string link so no reader crashes on rows older helms wrote —
        and this reader then asked `cid in seen` with whatever the row
        carried, so ONE list-valued id raised TypeError out of `helm task
        list` for the whole ledger. Review reproduced it with an exact probe;
        this is that probe as an arm."""
        rows = self._rows(("task/1", None, None), ("task/2", "task/1", "P0"))
        rows.append({"id": ["task/9"], "continues": None, "priority": None,
                     "title": "list id"})
        rows.append({"id": {"k": 1}, "continues": "task/1", "priority": None,
                     "title": "dict id"})
        got = tasks._story_order(rows)   # the defect raised HERE
        self.assertEqual(4, len(got), "a bad id dropped rows from the listing")
        # POSITIVE CONTROL on the same call: the well-formed story still
        # renders as a story — order and indentation survive the bad rows.
        ids = [r["id"] for r, _i in got if type(r["id"]) is str]
        self.assertEqual(ids.index("task/2"), ids.index("task/1") + 1)
        titles = {r["title"] for r, _i in got}
        self.assertIn("list id", titles)
        self.assertIn("dict id", titles)
class RankIsVisibleAndSweepableTest(CliBase):
    """THE OWNER ASKED WHETHER TASKS ARE PRIORITIZED AND THE ANSWER WAS NO.
    The field has existed since the free-text census; what never existed was
    a glyph on the listing, a rank on the wire, and any verb that sweeps the
    backlog. Measured on the live ledger when these arms were written: 403 of
    this project's 440 open rows carried no rank at all."""

    def test_rank_key_puts_P0_first_and_UNRANKED_last(self):
        """The published order, and UNRANKED is not P3. A key that sorted the
        absence as lowest-rank would bury a P3 someone actually judged under
        sixteen hundred rows nobody has looked at."""
        rows = [{"id": "task/1", "priority": None},
                {"id": "task/2", "priority": "P3"},
                {"id": "task/3", "priority": "P0"},
                {"id": "task/4", "priority": "P2"}]
        got = [r["id"] for r in sorted(rows, key=tasks.rank_key)]
        self.assertEqual(["task/3", "task/4", "task/2", "task/1"], got)
        # THE NUMBERING STILL DECIDES WITHIN A RANK, which is the half that
        # composes rather than replaces: same rank, lower id first.
        same = [{"id": "task/30", "priority": "P1"},
                {"id": "task/4", "priority": "P1"}]
        self.assertEqual(["task/4", "task/30"],
                         [r["id"] for r in sorted(same, key=tasks.rank_key)])

    def test_the_listing_SHOWS_a_rank_and_leaves_UNRANKED_blank(self):
        """A FIELD WITH NO GLYPH IS A FIELD NOBODY SORTS BY — this module's
        own words about `origin`, and the listing had exactly that shape for
        rank. The blank is the point: a filler that looked like a value would
        be the eighth spelling PRIORITIES refuses."""
        ranked = tasks._fmt({"id": "task/9", "status": "open",
                             "priority": "P0", "title": "ranked row"})
        bare = tasks._fmt({"id": "task/9", "status": "open",
                           "title": "unranked row"})
        self.assertIn("P0", ranked)
        self.assertIn("ranked row", ranked)
        # THE CONTROL IS THE SAME RENDERER ON THE SAME OBSERVABLE: the
        # unranked row still renders its id and title, so the missing "P0" is
        # about the rank and not about a renderer that went dark.
        self.assertIn("task/9", bare)
        self.assertIn("unranked row", bare)
        for rank in tasks.PRIORITIES:
            self.assertNotIn(rank, bare,
                             "an unranked row rendered as a rank")

    def test_triage_counts_the_DEBT_and_never_writes_a_P0(self):
        """TWO PROPERTIES THAT LOOK LIKE ONE. The header must describe the
        whole debt rather than the slice `--limit` writes — a true "6
        unranked" about a backlog of hundreds is the most convincing way to
        be wrong — and the rule's P0 clause is a judgement about what a row
        BLOCKS, which no field records, so this verb may never write one."""
        self.scope()
        self.cli("add", "first unranked row", "--owner", self.SEAT)
        self.cli("add", "second unranked row", "--owner", self.SEAT)
        self.cli("add", "third unranked row", "--owner", self.SEAT)
        rc, out, err = self.cli("triage", "--limit", "1")
        self.assertEqual(rc, 0, err)
        self.assertIn("3 UNRANKED", out)
        self.assertIn("this pass covers 1 of them", out)
        self.assertIn("DRY", out)
        self.assertNotIn("P0", out.split("NO P0 IS EVER")[0],
                         "the dry listing proposed a P0")

    def test_triage_apply_RANKS_and_the_owner_rule_splits_them(self):  # noqa: VACUOUS_ASSERTION — the assertIsNone is a PRECONDITION on the same observable the arm then pins to the literals "P2" and "P1" unconditionally, so an absence that was never filled reddens those two lines rather than passing quietly.
        """THE WRITE, AND THE SPLIT IT MAKES. Owner-asked rows are P1 and the
        rest are P2 — the two clauses of the owner's rule that a stamped
        field can actually decide. Each row goes through `update()` so the
        rank lands as its own audited event."""
        self.scope()
        self.admit()                    # a rank is an act with an actor
        self.cli("add", "an agent filed this", "--owner", self.SEAT)
        self.cli("add", "the owner asked for this", "--owner", self.SEAT,
                 "--owner-asked")
        before = self.filed("an agent filed this")
        self.assertIsNone(before.get("priority"),
                          "the fixture started ranked, so the write below "
                          "would prove nothing")
        rc, out, err = self.cli("triage", "--apply")
        self.assertEqual(rc, 0, err)
        self.assertIn("RANKED", out)
        self.assertEqual("P2", self.filed("an agent filed this")["priority"])
        self.assertEqual("P1",
                         self.filed("the owner asked for this")["priority"])
        # AND IT IS IDEMPOTENT BY CONSTRUCTION: a second pass finds nothing,
        # because the debt is defined by the absence it just filled.
        _rc2, out2, _e2 = self.cli("triage")
        self.assertIn("nothing to triage", out2)

    def test_the_add_door_SAYS_the_row_is_unranked(self):
        """THE DEBT GREW SILENTLY BECAUSE NOTHING EVER MENTIONED IT. This
        does not DEFAULT a rank: stamping P2 on a row nobody judged renders
        nobody-has-ranked-this as ranked-in-the-middle, which is the
        absence-versus-value law PRIORITIES states in its own note."""
        rc, out, _err = self.cli("add", "a row with no rank",
                                 "--owner", self.SEAT)
        self.assertEqual(rc, 0)
        self.assertIn("UNRANKED", out)
        self.assertIn("helm task triage", out)
        self.assertIsNone(self.filed("a row with no rank").get("priority"),
                          "the door defaulted a rank instead of naming the "
                          "absence")
        # THE CONTROL: a row filed WITH a rank says nothing about UNRANKED,
        # so the line above is about the absence and not about every add.
        _rc, out2, _e = self.cli("add", "a row with a rank",
                                 "--owner", self.SEAT, "--priority", "P1")
        self.assertIn("filed", out2)
        self.assertNotIn("UNRANKED", out2)
        self.assertEqual("P1", self.filed("a row with a rank")["priority"])

    # ----------------------------------------------------------------- #
    # SIX MEASURED DEFECTS ON THIS VERB, WHICH WERE ONE SHAPE: every one
    # was a DECISION made from a read that was not a decision read.
    # The sweep chose outside the writer's lock, stamped no actor,
    # resolved its scope to None and let one bucket be two, took a bound
    # it never validated, advertised a command its own preview did not
    # describe, and turned a tolerant projection's skipped bytes into a
    # completeness claim. The arms below are ordered as the findings were.
    # ----------------------------------------------------------------- #

    def test_the_RULE_reads_the_row_UNDER_THE_LOCK_not_the_callers_snapshot(
            self):
        """FINDING 1, AT THE ONLY PLACE IT CAN BE MEASURED.

        A CLI pass that ranks a row by hand and THEN sweeps proves nothing
        about this: the second sweep's own read already excludes the ranked
        row, so the rule never runs and the survival is the SELECTION's doing.
        The defect lives in the window between a caller's read and its write,
        so the arm has to open that window.

        IT OPENS IT INSIDE THE WRITER'S LOCK. The strict read `update()` makes
        under its own lock is wrapped for ONE call, and the wrapper appends a
        deliberate P0 to the ledger before reading — the exact interleaving a
        second seat produces, from the one position where the state can change
        after the caller decided and before the append lands. A rule resolved
        from the CALLER's snapshot writes P2 here; a rule resolved from the
        row under the lock sees P0 and declines.

        THE POSITIVE CONTROL IS THE SAME CALL WITHOUT THE WRAPPER, on a row
        nobody touched: it WRITES. Without it a SKIPPED below could be a rule
        that declines everything."""
        scope = self.scope()
        actor = self.admit(self.SEAT)
        self.cli("add", "raced by another seat", "--owner", self.SEAT)
        self.cli("add", "nobody touches this one", "--owner", self.SEAT)
        raced = self.filed("raced by another seat")["id"]
        quiet = self.filed("nobody touches this one")["id"]
        rule = tasks.RankRule(scope)
        # THE POSITIVE CONTROL FIRST, so a decline below is about the race.
        row, err = tasks.update(quiet, rank_rule=rule, rank_actor=actor)
        self.assertIsNone(err, err)
        self.assertIsNot(row, tasks.SKIPPED, "the rule declined a clean row")
        self.assertEqual("P2", row["priority"])

        real = tasks.eventledger.latest_checked
        state = {"raced": False}

        def racing_read(path, accept=None, strict=False):
            """Append a deliberate P0 the first time the writer reads.

            THE WHOLE SIGNATURE, because a double that accepts less than the
            real function turns an unrelated caller into a TypeError and
            reddens this arm for a reason it is not about."""
            if not state["raced"]:
                state["raced"] = True
                before = real(path, accept, strict)[0].get(raced) or {}
                tasks.eventledger.append_unlocked(
                    path, dict(before, priority="P0"))
            return real(path, accept, strict)

        with mock.patch.object(tasks.eventledger, "latest_checked",
                               racing_read):
            row, why = tasks.update(raced, rank_rule=rule, rank_actor=actor)
        self.assertTrue(state["raced"], "the race never fired, so this arm "
                                        "measured an ordinary write")
        self.assertIs(row, tasks.SKIPPED,
                      "the sweep wrote over a rank that landed while it held "
                      "a decision made before the lock: %s" % (why,))
        self.assertIn("already ranked P0", why)
        self.assertEqual("P0", tasks.get(raced)["priority"],
                         "the deliberate rank did not survive the sweep")

    def test_a_row_that_changed_under_the_PREVIEW_is_skipped_BY_NAME(self):  # noqa: VACUOUS_ASSERTION — the ledger-line equality is an EXACT count, not an absence: it pins before+2 against a literal, and the same pass unconditionally pins the untouched row to the literal "P2" and the skipped row to "P0". A sweep that wrote nothing fails the P2 line; a sweep that wrote twice fails the count.
        """THE OPERATOR'S HALF OF FINDING 1. The arm above pins the ORDERING
        at the writer; this one pins what the SWEEP TELLS THE OPERATOR when a
        row it selected is no longer eligible — a count of "skipped" and a
        count of "failed" ask for opposite things, so they are reported
        separately and each skip names its reason.

        THE RANK LANDS AFTER THE SWEEP'S OWN SELECTION READ, which is the only
        placement that exercises the contract. A pass that ranks the row and
        then starts a NEW sweep proves nothing: the second sweep's selection
        already excludes it, so the rule never runs and nothing is skipped.
        Wrapping `snapshot` for one call puts the P0 in the window the sweep
        actually has — selected from a tolerant-of-nothing read, written
        against a row that has since moved.

        THE POSITIVE CONTROL IS THE SAME PASS: the untouched row IS ranked, so
        the skip is the rule declining rather than a sweep that wrote nothing,
        and the SKIPPED count is 1 rather than "at least one"."""
        self.scope()
        self.admit()                    # a rank is an act with an actor
        self.cli("add", "somebody ranks this by hand", "--owner", self.SEAT)
        self.cli("add", "nobody touches this one", "--owner", self.SEAT)
        guarded = self.filed("somebody ranks this by hand")["id"]
        before = self.ledger_lines()

        real = tasks.snapshot
        state = {"raced": False}

        def racing_snapshot(path=None, strict=False):
            """Let the sweep SELECT, then rank the row behind its back."""
            got = real(path, strict=strict)
            if not state["raced"]:
                state["raced"] = True
                row = dict(got[0].get(guarded) or {}, priority="P0")
                tasks.eventledger.append_unlocked(tasks.ledger_path(), row)
            return got

        with mock.patch.object(tasks, "snapshot", racing_snapshot):
            rc, out, err = self.cli("triage", "--apply")
        self.assertEqual(rc, 0, err)
        self.assertTrue(state["raced"], "the race never fired, so this arm "
                                        "measured an ordinary sweep")
        self.assertIn("1 SKIPPED", out,
                      "the sweep did not tell the operator a selected row was "
                      "declined: %r" % out)
        self.assertIn("already ranked P0", out,
                      "the skip did not name its reason")
        self.assertIn(guarded, out, "the skip did not name the row")
        self.assertEqual("P0", self.filed("somebody ranks this by hand")["priority"],
                         "the sweep overwrote a rank that landed after its "
                         "own selection")
        self.assertEqual("P2", self.filed("nobody touches this one")["priority"],
                         "the pass ranked nothing at all, so the skip above "
                         "proves nothing about the rule")
        # THE LEDGER COUNTS THE WRITES, because "P0 survived" is also what a
        # sweep that wrote P0 over P0 would leave behind. Two adds, the raced
        # P0, and exactly ONE rank write.
        self.assertEqual(before + 2, self.ledger_lines(),
                         "the skipped row still appended an event")

    def test_a_rank_event_names_WHO_ranked_it_and_keeps_the_FILER(self):
        """FINDING 2. The writer copied the old row, changed priority and
        last_updated, and appended — so the ledger recorded a history of
        VALUES and could answer nothing about AGENCY: a sweep's derived P2 and
        an operator's deliberate P2 were the same event. `source` is the
        FILER's and must not be relabelled as the ranker's, so the provenance
        is added beside it rather than written over it.

        THE RAW JSONL, NEVER THE PROJECTION, because a snapshot shows the
        final row and this arm is about which fields the EVENT carried."""
        self.scope()
        self.cli("add", "filed by one seat", "--owner", self.SEAT)
        rid = self.filed("filed by one seat")["id"]
        filer = self.filed("filed by one seat").get("source")
        self.admit("seat-b")
        rc, _o, err = self.cli("update", rid, "--priority", "P1")
        self.assertEqual(rc, 0, err)
        events = [e for e in self.lines(tasks.ledger_path())
                  if e.get("id") == rid]
        self.assertGreaterEqual(len(events), 2, "the update appended nothing")
        latest = events[-1]
        self.assertEqual("P1", latest.get("priority"))
        self.assertEqual("seat-b", latest.get("ranked_by"),
                         "the rank event cannot say who ranked it")
        self.assertEqual("manual", latest.get("rank_action"))
        self.assertIsNone(latest.get("ranked_from"),
                          "an unranked row reported a previous rank")
        self.assertEqual(filer, latest.get("source"),
                         "the ranker was written over the filer, which is the "
                         "erasure this field exists to refuse")
        # THE RULE DOOR CARRIES THE SAME PROVENANCE AND SAYS IT WAS A RULE.
        self.cli("add", "swept by the rule", "--owner", self.SEAT)
        rc, _o, err = self.cli("triage", "--apply")
        self.assertEqual(rc, 0, err)
        swept = self.filed("swept by the rule")["id"]
        rule_event = [e for e in self.lines(tasks.ledger_path())
                      if e.get("id") == swept][-1]
        self.assertEqual("rule", rule_event.get("rank_action"))
        self.assertEqual("seat-b", rule_event.get("ranked_by"))

    def test_a_rank_with_NO_ADMITTED_ACTOR_is_refused_at_BOTH_doors(self):
        """FINDING 2's OTHER HALF. An unresolved actor must REFUSE where a
        witnessed actor is required, never silently borrow the filer's
        identity — so the requirement is proven by removing the corroboration
        this fixture plants and watching both doors close.

        THE CONTROL IS THE ADMITTED CALL IN THE SAME ARM: the same row, the
        same flag, admitted, writes. Without it a red here could be any
        refusal at all."""
        self.scope()
        self.cli("add", "a row to rank", "--owner", self.SEAT)
        rid = self.filed("a row to rank")["id"]
        # THE CORROBORATION IS WHAT GOES AWAY: the name is still declared, so
        # this is the UNCORROBORATED refusal and not a missing seat name.
        for key in home._SESSION_ENV:
            os.environ.pop(key, None)
        rc, _o, err = self.cli("update", rid, "--priority", "P2")
        self.assertEqual(rc, 2, "an unadmitted seat ranked a row")
        self.assertIsNone(self.filed("a row to rank").get("priority"))
        rc, _o, err2 = self.cli("triage", "--apply")
        self.assertEqual(rc, 2, "an unadmitted seat swept the backlog")
        self.assertIsNone(self.filed("a row to rank").get("priority"),
                          "the sweep wrote before resolving its actor")
        # THE POSITIVE CONTROL, SAME ROW, SAME FLAG, ADMITTED.
        self.admit(self.SEAT)
        rc, _o, err3 = self.cli("update", rid, "--priority", "P2")
        self.assertEqual(rc, 0, err3)
        self.assertEqual("P2", self.filed("a row to rank")["priority"])

    def test_an_UNRESOLVED_project_refuses_instead_of_being_a_scope(self):
        """FINDING 3. With no project, `project_of_row(r) == scope` and
        `project_of_row(r) is None` select the SAME rows: the default pass
        proposed a rank for a row it simultaneously reported as NOT covered,
        and `--legacy` listed that row twice and would have written it twice.
        An unknown scope is the absence of the answer, not a value that
        matches the unscoped bucket.

        THE CONTROL IS THE REGISTERED SCOPE IN THE SAME ARM, which is also
        what proves the refusal is about the scope and not about the ledger."""
        self.cli("add", "a row with no project", "--owner", self.SEAT)
        rc, out, err = self.cli("triage")
        self.assertEqual(rc, 2, "an unknown scope was accepted as a scope")
        self.assertIn("NO PROJECT", err)
        self.assertNotIn("UNRANKED", out,
                         "the verb read the ledger before knowing its scope")
        rc, out, err = self.cli("triage", "--legacy")
        self.assertEqual(rc, 2, "--legacy bought an unknown scope a meaning")
        # THE CONTROL: with a scope resolved, the same ledger triages, and the
        # row appears EXACTLY ONCE under --legacy rather than in both buckets.
        self.scope()
        rc, out, err = self.cli("triage", "--legacy")
        self.assertEqual(rc, 0, err)
        rid = self.filed("a row with no project")["id"]
        self.assertEqual(1, out.count(rid),
                         "one row was listed in two buckets")

    def test_the_LIMIT_fails_closed_on_absence_and_on_a_non_positive_value(
            self):
        """FINDING 4, THREE SPELLINGS OF ONE HOLE. `_take` DELETES a trailing
        bare `--limit` and returns None, which is byte-identical to "never
        passed" — so the flag silently selected every row. A negative value
        reached Python's negative slice and selected everything BUT the last
        two. Zero selected nothing and then reported the debt as PAID.

        THE POSITIVE CONTROL IS `--limit 1` ON THE SAME LEDGER: the bound
        works when it is given, so each refusal below is about the value."""
        self.scope()
        for n in ("one", "two", "three"):
            self.cli("add", "row %s" % n, "--owner", self.SEAT)
        rc, out, err = self.cli("triage", "--limit")
        self.assertEqual(rc, 2, "a valueless --limit selected the whole debt")
        self.assertIn("WHOLE NUMBER", err)
        rc, out, err = self.cli("triage", "--limit=-2")
        self.assertEqual(rc, 2, "a negative bound selected a set by slicing")
        self.assertIn("1 or more", err)
        rc, out, err = self.cli("triage", "--limit=0")
        self.assertEqual(rc, 2, "zero reported the debt as paid")
        self.assertNotIn("every open row carries a rank", out)
        rc, out, err = self.cli("triage", "--limit", "1")
        self.assertEqual(rc, 0, err)
        self.assertIn("3 UNRANKED", out)
        self.assertIn("this pass covers 1 of them", out)

    def test_the_DRY_hint_advertises_the_command_it_just_PREVIEWED(self):
        """FINDING 5. A bounded preview said "this pass covers 1 of 3" and
        then offered the UNRESTRICTED write as writing "these" — so an
        operator following the text ranks the other two. The advertised
        command is built from the validated options now, which is the only
        way the sentence and the screen describe one selection.

        BOTH OPTION DIMENSIONS, because the legacy choice was discarded by
        the same line and for the same reason."""
        self.scope()
        for n in ("one", "two", "three"):
            self.cli("add", "row %s" % n, "--owner", self.SEAT)
        rc, out, err = self.cli("triage", "--limit", "1")
        self.assertEqual(rc, 0, err)
        self.assertIn("triage --apply --limit 1", out,
                      "the hint offered a wider write than it previewed")
        self.assertIn("writes THESE 1 row(s)", out)
        rc, out, err = self.cli("triage", "--limit", "2", "--legacy")
        self.assertEqual(rc, 0, err)
        self.assertIn("triage --apply --limit 2 --legacy", out)
        # THE CONTROL: an UNBOUNDED preview advertises the bare command, so
        # the options above are carried rather than always printed.
        rc, out, err = self.cli("triage")
        self.assertEqual(rc, 0, err)
        self.assertIn("triage --apply` writes THESE 3 row(s)", out)

    def test_a_CORRUPT_ledger_reads_as_UNKNOWN_and_never_as_debt_cleared(self):
        """FINDING 6. The tolerant projection SKIPS a malformed row to keep
        listings alive over one bad byte, which is right for a projection and
        a lie for a decision: measured on a ledger with one corrupt record,
        triage returned rc 0, zero UNRANKED and "every open row carries a
        rank" while the strict reader called the same file corrupt. Skipped
        evidence must not become a completeness claim.

        BOTH POPULATIONS, because a cure that only refuses an ALL-corrupt
        ledger leaves the mixed case — the one that actually happens —
        reporting a partial debt as the whole one."""
        self.scope()
        self.cli("add", "a readable row", "--owner", self.SEAT)
        with open(tasks.ledger_path(), "a", encoding="utf-8") as fh:
            fh.write("{not json at all\n")
        rc, out, err = self.cli("triage")
        self.assertEqual(rc, 2, "a corrupt ledger reported a clean backlog")
        self.assertIn("UNKNOWN, not zero", err)
        self.assertNotIn("every open row carries a rank", out)
        # THE CONTROL ON THE SAME LEDGER: with the corrupt line removed, the
        # same call reads the same rows and reports the debt.
        with open(tasks.ledger_path(), encoding="utf-8") as fh:
            keep = [ln for ln in fh if ln.strip() and "not json" not in ln]
        with open(tasks.ledger_path(), "w", encoding="utf-8") as fh:
            fh.writelines(keep)
        rc, out, err = self.cli("triage")
        self.assertEqual(rc, 0, err)
        self.assertIn("1 UNRANKED", out)


class MineFiltersNothingTest(CliBase):
    """task/1074. `list --mine` was ACCEPTED AND IGNORED: the verb consumed
    --all, --json, --all-projects and --owner and never looked for --mine, so
    it fell through as an unrecognised token and the caller got the WHOLE
    ledger under a flag whose only promise is narrowing.

    THE CONTROL IS THE FINDING, and every arm here keeps it: `--owner SEAT`
    filters correctly on the same rows in the same call, so a red arm cannot
    be blamed on an unresolvable identity or a broken predicate. What was
    broken was only that --mine never applied one, and never said so."""

    def _two_seats(self):
        """One row mine, one another seat's, in the same ledger."""
        rc, _o, err = self.cli("add", "mine to do", "--mine")
        self.assertEqual(rc, 0, err)
        rc, _o, err = self.cli("add", "not mine", "--owner", "seat-b")
        self.assertEqual(rc, 0, err)

    def _titles(self, out):
        return sorted(t for t in ("mine to do", "not mine") if t in out)

    def test_mine_returns_ONLY_my_rows_and_owner_proves_the_filter_works(self):
        self._two_seats()
        # THE DEFECT: this returned BOTH titles before the cure.
        rc, out, err = self.cli("list", "--mine")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self._titles(out), ["mine to do"],
                         "--mine printed a row this seat does not own")
        # CONTROL ON THE SAME LEDGER, UNCONDITIONAL: the machinery narrows
        # correctly when asked by name, in both directions. Without these two
        # the arm above could pass on a ledger that simply had one row.
        rc, out, err = self.cli("list", "--owner", self.SEAT)
        self.assertEqual(rc, 0, err)
        self.assertEqual(self._titles(out), ["mine to do"])
        rc, out, err = self.cli("list", "--owner", "seat-b")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self._titles(out), ["not mine"])
        # AND THE UNFILTERED CALL STILL SHOWS BOTH — so --mine narrowed rather
        # than the fixture only ever holding one visible row.
        rc, out, err = self.cli("list")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self._titles(out), ["mine to do", "not mine"])

    def test_mine_without_an_identity_prints_NOTHING_rather_than_everything(
            self):
        """The fall-back IS the defect. A backlog the reader believes is
        'mine' while it names every seat is exactly what this flag closes, so
        an unresolved identity prints zero rows and says why."""
        self._two_seats()
        # ROSTER-ONLY POSITIVE CONTROL, UNCONDITIONAL AND FIRST: an admitted
        # seat with NO declared name still resolves through its rostered
        # session and --mine narrows correctly. Without it the refusal below
        # could be a door that refuses everything.
        self.admit()
        os.environ.pop("HELM_CHAT_NAME", None)
        rc, out, err = self.cli("list", "--mine")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self._titles(out), ["mine to do"],
                         "a rostered session stopped resolving, so the "
                         "refusal below is not about the identity")
        self.unidentify()
        rc, out, err = self.cli("list", "--mine")
        self.assertEqual(rc, 1)
        self.assertEqual(self._titles(out), [],
                         "an identity-less --mine fell back to the full list")
        # THE CANONICAL RESOLVER'S OWN WORDS, not my earlier hand-written
        # sentence — this arm pinned text I later replaced, and the whole
        # suite caught it. "refusing to <action>" is the stable half: the
        # resolver interpolates the action this door passed it.
        self.assertIn("refusing to list your own rows", err)
        self.assertIn("NO ROWS PRINTED", err)
        # POSITIVE CONTROL, UNCONDITIONAL: the SAME identity-less process can
        # still list by name, so the refusal measured the missing IDENTITY and
        # not a ledger this process cannot read at all.
        rc, out, err = self.cli("list", "--owner", "seat-b")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self._titles(out), ["not mine"])

    def test_mine_and_owner_name_two_owners_and_are_refused(self):
        self._two_seats()
        rc, out, err = self.cli("list", "--mine", "--owner", "seat-b")
        self.assertEqual(rc, 2)
        self.assertEqual(self._titles(out), [],
                         "a refused combination still printed rows")
        self.assertIn("cannot be combined", err)
        self.assertIn("seat-b", err)          # names the one it did not use
        # UNCONDITIONAL POSITIVE CONTROL ON THE SAME OBSERVABLE, which the
        # rung was right to demand: the emptiness above means nothing unless
        # THIS helper, on THIS ledger, is shown returning a non-empty list.
        # An rc-only control cannot say that — `_titles` could return [] for
        # every input and every assertion here would still hold.
        rc, out, err = self.cli("list", "--mine")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self._titles(out), ["mine to do"])
        rc, out, err = self.cli("list", "--owner", "seat-b")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self._titles(out), ["not mine"])

    def test_mine_and_owner_are_refused_at_the_ADD_door_too(self):  # noqa: VACUOUS_ASSERTION — the absence `titled("conflict") == []` is controlled unconditionally by `len(titled("mine only")) == 1` and `len(titled("given")) == 1` on the SAME local reader over the SAME ledger, so it cannot pass by `titled` returning [] for every input. The rung mints a fresh producer identity per call and cannot relate calls through a local lambda; the ledger reads are real and the owners are pinned to the literals seat-a and seat-b.
        """This refusal shipped on `list` and
        not on `add`, one door over, by my own hand: `if mine and not owner`
        skipped own-resolution entirely when --owner was present, so
        `add --mine --owner seat-b` silently took the --owner value and
        discarded --mine without a word."""
        rc, _out, err = self.cli("add", "conflict", "--mine",
                                 "--owner", "seat-b")
        self.assertEqual(rc, 2)
        self.assertIn("cannot be combined", err)
        self.assertIn("seat-b", err)
        titled = lambda t: [r for r in tasks.rows(
            path=tasks.ledger_path()).values() if r.get("title") == t]
        self.assertEqual(titled("conflict"), [],
                         "a refused combination still filed a row")
        # UNCONDITIONAL POSITIVE CONTROLS ON THE SAME OBSERVABLE: each flag
        # ALONE still files, so the refusal is about the COMBINATION and not
        # about either flag having become inert.
        self.assertEqual(self.cli("add", "mine only", "--mine")[0], 0)
        self.assertEqual(self.cli("add", "given", "--owner", "seat-b")[0], 0)
        # THE SAME COMPREHENSION, SHOWN NON-EMPTY. An rc-only or filed()-based
        # control cannot say that `titled` can ever return a row, so the
        # emptiness above would hold even if it returned [] for every input.
        self.assertEqual(len(titled("mine only")), 1)
        self.assertEqual(titled("mine only")[0]["owner"], "seat-a")
        self.assertEqual(len(titled("given")), 1)
        self.assertEqual(titled("given")[0]["owner"], "seat-b")

    def test_a_MISSPELLED_filter_is_refused_not_silently_ignored(self):  # noqa: VACUOUS_ASSERTION — the in-loop absence `_titles(out) == []` is controlled AFTER the loop, unconditionally, by `_titles(out) == ["mine to do"]` on the same helper and the same ledger — so it cannot pass by _titles always returning []. The rung flags it because the control sits outside the `for bad in (...)` body and it cannot relate the two across that boundary; the tuple is a literal, so the body always runs.
        """The sharper half of the same class: curing
        `--mine` while leaving `--mnie` cures the INSTANCE and leaves the
        CLASS. An unrecognised option fell through exactly as --mine used to
        and rc0-printed the WHOLE ledger under a flag the caller believed had
        narrowed it — the identical consequence, one typo away."""
        self._two_seats()
        for bad in ("--mnie", "--mine=true", "--owner-ish"):
            with self.subTest(flag=bad):
                rc, out, err = self.cli("list", bad)
                self.assertEqual(rc, 2)
                self.assertEqual(self._titles(out), [],
                                 "a refused option still printed rows")
                self.assertIn("unknown option", err)
        # UNCONDITIONAL POSITIVE CONTROL ON THE SAME OBSERVABLE: the correctly
        # spelled flags still work on this ledger, so the guard rejects the
        # UNKNOWN and not everything.
        rc, out, err = self.cli("list", "--mine")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self._titles(out), ["mine to do"])
        rc, out, err = self.cli("list", "--all-projects")
        self.assertEqual(rc, 0, err)

    def test_add_mine_with_no_identity_files_NOTHING(self):  # noqa: VACUOUS_ASSERTION — the absence assertion is `ledger_lines() == before`, and its unconditional positive control is the SAME OBSERVABLE SHOWN MOVING later in the same method: `ledger_lines() == before + 1` after the permitted --owner add. If ledger_lines() were inert, absent, or always equal, that control fails — so the absence cannot pass vacuously. The rung cannot credit it because both sides are variables rather than literals (the same limitation this file documents at CliBase.SEAT), so the row is pinned to a literal too: filed("owned openly")["owner"] == "seat-b".
        """The sibling door, failing the same direction. `owner = own_name()`
        yielding None used to fall through and file the row UNOWNED — back
        into the pool any idle seat may take — while the caller who typed
        --mine believed they held it. An unowned row is a legitimate state,
        which is why it must be ASKED for rather than arrived at."""
        # `lines()`, NOT `ledger_lines()`, and NOT a seeded row. This arm
        # files NOTHING on purpose, so on a fresh fixture the ledger FILE does
        # not exist and ledger_lines — which opens it directly by design —
        # raised FileNotFoundError before any assertion ran (a red gate).
        # I cured that by seeding an unrelated row; but the
        # fixture ALREADY has a missing-file-safe reader one class up, which
        # asserts the same absence without inventing state the test is not
        # about. AND IT TAKES THE PATH EXPLICITLY: lines() defaults to the
        # FIXTURE ledger while these CLI arms drive cmd_task, which resolves
        # tasks.ledger_path() under the redirected HELM_HOME — two different
        # files. Reading the default would have measured a file the CLI never
        # writes, trading a crash for a silent mismeasurement.
        # ROSTER-ONLY POSITIVE CONTROL, UNCONDITIONAL AND FIRST, on the same
        # observable the refusal then asserts unchanged: an admitted seat with
        # no declared name files through --mine and the ledger GROWS.
        self.admit()
        os.environ.pop("HELM_CHAT_NAME", None)
        seeded = len(self.lines(tasks.ledger_path()))
        rc, _o, err = self.cli("add", "filed by a rostered session", "--mine")
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(self.lines(tasks.ledger_path())), seeded + 1,
                         "a rostered session could not file, so the refusal "
                         "below is not about the identity")
        self.unidentify()
        before = len(self.lines(tasks.ledger_path()))
        rc, _out, err = self.cli("add", "who owns this", "--mine")
        # rc 2, like every sibling refusal at this class of door (catchup's
        # REFUSING, the task/1450 guard): 1 was this lane's own invention and
        # consistency belongs to the estate, not the lane.
        self.assertEqual(rc, 2)
        self.assertIn("NOTHING WAS FILED", err)
        # THE LEDGER FILE, not a row count off a projection: an appended row
        # that a later filter hides would satisfy any value assertion.
        self.assertEqual(len(self.lines(tasks.ledger_path())), before,
                         "a refused --mine still appended to the ledger")
        # POSITIVE CONTROL, UNCONDITIONAL: the same identity-less process CAN
        # file when it names an owner, so the refusal measured the missing
        # identity and not a ledger that had become unwritable.
        rc, _out, err = self.cli("add", "owned openly", "--owner", "seat-b")
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(self.lines(tasks.ledger_path())), before + 1)
        # AND PINNED TO A LITERAL, because `before + 1` compares two unknowns
        # as far as any analyzer can see — the same reason this file spells
        # "seat-a" out rather than reading it off SEAT. A row that really
        # exists, with the owner really written, is what makes the unchanged
        # count above mean "nothing was filed" instead of "nothing files".
        self.assertEqual(self.filed("owned openly")["owner"], "seat-b")


class FilerStampAndOwnerShareOneResolverTest(CliBase):
    """A FIX on this lane: `add --mine` resolved the OWNER through
    dispatches.acting_author and then stamped `source` from seats.own_name()
    four lines later. Two resolvers answering one question — "which seat is
    this process" — and they disagree on exactly the two identities the switch
    to acting_author was made for:

      * a ROSTER-BOUND session with no HELM_CHAT_NAME resolves under
        acting_author (the roster binding is single-valued) and returns None
        from own_name, which reads only the declared env. The row came out
        correctly OWNED and with NO PROVENANCE — filed by nobody.
      * a STALE INHERITED HELM_CHAT_NAME that contradicts the roster is a
        DISPUTED identity acting_author refuses outright, while own_name hands
        it over unexamined. The stamp then named a filer the same call had
        just declined to call the owner.

    The cure is not "source = owner" (filing is not owning: `--owner kimi`
    keeps ME as the filer). It is ONE resolution of THIS PROCESS's identity,
    spent on both fields.

    The expected value is DERIVED from `dispatches.acting_author` — the
    accessor the implementation itself calls — rather than transcribed, so a
    change to the identity law moves the arm with it. The roster name is still
    pinned to a literal, but a literal THIS ARM PLANTED with write_roster, so
    the pin binds to state the test owns rather than to a copied constant."""

    SESSION = "s-roster-only"

    def _session(self, sid):
        """Plant the harness session id these arms depend on. TasksBase scrubs
        the session env at setUp and restores it at tearDown, so this cannot
        leak into a later test in the file — and, in the other direction, the
        gate runner's REAL session id cannot decide a verdict here."""
        os.environ["CLAUDE_CODE_SESSION_ID"] = sid

    def test_a_ROSTER_ONLY_caller_stamps_the_resolved_owner_not_None(self):  # noqa: VACUOUS_ASSERTION — the absence here is `own_name() is None`, and it is NOT the observable under test: it is the FIXTURE CONTROL that proves the two resolvers disagree, and the very next line shows the same identity question answering NON-empty through the other resolver (`acting_author(...) == "seat-roster"`, a literal this arm planted with write_roster). The observable under test is `row["source"]`, asserted NON-empty twice (`== resolved` and `assertIsNotNone`). The rung cannot relate the two because `resolved` is a variable — the same limitation this file documents at CliBase.SEAT — which is why the resolver's answer is ALSO pinned to a literal.
        from helm import seats
        from helm import dispatches
        # THE FIXTURE: identity lives ONLY in the roster. This is the shape
        # acting_author was widened to accept and own_name structurally cannot
        # see — a hand-launched-but-rostered seat.
        del os.environ["HELM_CHAT_NAME"]
        self._session(self.SESSION)
        seats.write_roster("seat-roster", session=self.SESSION, cwd=self.tmp)

        # THE TWO CONTROLS THAT MAKE THE REST MEAN ANYTHING, and they must
        # DISAGREE — that disagreement IS the defect. If own_name still
        # answered here the old code would have stamped correctly by accident
        # and this arm would pass on the bug it exists to catch.
        self.assertIsNone(seats.own_name(),
                          "fixture leaked a declared name; the arm would "
                          "measure the agreeing case instead of the split")
        resolved, ident_err = dispatches.acting_author("file this row")
        self.assertIsNone(ident_err, ident_err)
        self.assertEqual(resolved, "seat-roster")

        rc, _out, err = self.cli("add", "taken", "by", "a", "rostered", "seat",
                                 "--mine")
        self.assertEqual(rc, 0, err)
        row = self.filed("taken by a rostered seat")
        # The half that already worked…
        self.assertEqual(row["owner"], resolved)
        # …and the half a review named: the row was filed by NOBODY while it
        # was successfully owned. Asserted against the resolver's own answer,
        # not against a transcribed name.
        self.assertEqual(row["source"], resolved)
        self.assertIsNotNone(row["source"])

        # THE SAME SPLIT ONE DOOR OVER, where filing is NOT owning: an
        # explicit --owner leaves the row someone else's and the FILER is
        # still this process. Pins that the cure fed the resolver into
        # `source` rather than copying `owner` into it.
        rc, _out, err = self.cli("add", "work", "for", "kimi",
                                 "--owner", "kimi")
        self.assertEqual(rc, 0, err)
        handed = self.filed("work for kimi")
        self.assertEqual(handed["owner"], "kimi")
        self.assertEqual(handed["source"], resolved)

    def test_a_STALE_env_cannot_stamp_a_filer_the_resolver_REFUSED(self):  # noqa: VACUOUS_ASSERTION — `row["source"] is None` HAS an unconditional positive control on the same observable, in the same method, reached on every run: after the dispute is removed the very same env stamps, and that row's `source` is asserted equal to a value ALSO pinned to the literal "seat-roster". If `source` were inert or always None that control fails. The rung cannot credit it because the two assertions name different rows and compare through a variable; the absence is additionally pinned in the discriminating direction by `assertNotEqual(row["source"], "ghost-of-a-dead-pane")` — the literal the OLD code wrote — and by `assertEqual(seats.own_name(), "ghost-of-a-dead-pane")` above it, which proves the stale stamp was AVAILABLE and declined rather than absent.
        from helm import seats
        from helm import dispatches
        # THE INPUT THIS ARM MUST REJECT: an exported HELM_CHAT_NAME that
        # OUTLIVED the pane it named, on a session the roster binds to someone
        # else. own_name() hands the ghost over; acting_author refuses the
        # pair as DISPUTED. Under the old code the ghost reached the ledger.
        #
        # ORDER IS LOAD-BEARING, and my first cut got it backwards:
        # write_roster REFUSES a cross-seat binding ("a seat may only ever
        # stamp its own row"), so planting the roster while the ghost was
        # already exported wrote NOTHING, left bound=None, and acting_author
        # answered with the ghost — there was no dispute to refuse and the arm
        # failed on its own fixture. The seat rosters itself FIRST, exactly as
        # a live seat does; the stale ancestor env arrives afterwards.
        self._session(self.SESSION)
        os.environ["HELM_CHAT_NAME"] = "seat-roster"
        seats.write_roster("seat-roster", session=self.SESSION, cwd=self.tmp)
        # THE CONTROL ON THE FIXTURE ITSELF: the binding is REALLY in the
        # roster. Without it the arm's premise ("the roster names someone
        # else") is an assumption, which is how the first cut passed its own
        # setup and failed three assertions later.
        self.assertEqual(seats.seat_for_session(self.SESSION), "seat-roster")
        os.environ["HELM_CHAT_NAME"] = "ghost-of-a-dead-pane"

        self.assertEqual(seats.own_name(), "ghost-of-a-dead-pane",
                         "the stale stamp must be AVAILABLE for the cure to "
                         "be shown declining it")
        resolved, ident_err = dispatches.acting_author("file this row")
        self.assertIsNone(resolved)
        self.assertIn("DISPUTED", ident_err)

        rc, _out, err = self.cli("add", "filed", "under", "a", "dispute")
        # NOT a refusal: a disputed identity is a reason to withhold the
        # STAMP, not to refuse the filing — `--mine` refuses because the row
        # would otherwise be silently unowned, and widening that refusal to
        # every add would refuse more than the defect. The row is filed with
        # its filer UNKNOWN, which is what is true.
        self.assertEqual(rc, 0, err)
        row = self.filed("filed under a dispute")
        self.assertNotEqual(row["source"], "ghost-of-a-dead-pane")
        self.assertIsNone(row["source"])

        # POSITIVE CONTROL, UNCONDITIONAL, ON THE SAME OBSERVABLE: with the
        # dispute removed the very same env DOES stamp. Without this the arm
        # above would pass against a `source` that is always None.
        os.environ["HELM_CHAT_NAME"] = "seat-roster"
        agreed, ident_err = dispatches.acting_author("file this row")
        self.assertIsNone(ident_err, ident_err)
        rc, _out, err = self.cli("add", "filed", "in", "agreement")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.filed("filed in agreement")["source"], agreed)
        self.assertEqual(agreed, "seat-roster")


class TaskRowsRecordTheirFilingSessionAsAUDITONLYTest(TasksBase):
    """task/1898, after three review rounds that ended the feature it began as.

    IT STARTED AS A RECLAIM. A fleet reboot can re-roster agent sessions BY
    SLOT, so rows filed under one seat name end up held by a name that now
    belongs to a DIFFERENT LIVE SEAT. Those rows strand. The intended cure was
    a narrow door: the session a row REPORTED may take it back under the name
    it answers to today.

    A review killed three successive versions of that door, and the third
    killed the idea:

      1. stamping the FILER let a seat reclaim work it had ASSIGNED AWAY;
      2. restricting to self-filed work still let the author take a row back
         after an AUTHORIZED transfer, because an immutable field cannot
         express a fact that changes;
      3. and fatally — `session_id()` reads raw environment and `public_row()`
         copies the whole row, so the matched value is BOTH PUBLISHED AND
         SETTABLE. Read it off any row, export it, name any seat, take the row.

    THE THIRD ONE ALSO FALSIFIED THE LIMITATION I HAD DOCUMENTED. I had written
    that this was "not a regression" because seat names are equally settable.
    Spoofing a session could already IMPERSONATE a seat; it could NOT change
    custody to a DIFFERENT NAME. The door would have created that capability.

    SO WHAT SHIPS IS THE REPORTED CLAIM AND NOTHING ELSE: every row records
    the session its filer DECLARED — a value read from the environment and
    republished unchanged, which identifies nobody and is pinned as forgeable
    by an arm below — the field is immutable, and NO authorization path
    consults it. Most of the arms below exist to keep it that way — a value a
    reader can copy must never become a credential, and the cheapest way for
    that to happen is for someone to re-open the door this class closed.
    """

    def _as(self, session):
        """Run inside a given filing-session identity."""
        return mock.patch.object(tasks, "_reported_session", lambda: session)

    def test_the_reported_session_is_stamped_at_birth(self):
        with self._as("sess-A"):
            row = self.file("a row with a knowable filer", "seat-a")
        self.assertEqual(row["reported_session"], "sess-A",
                         "the reported session was not recorded, so the "
                         "fact this lane reduced to does not exist")

    def test_a_row_filed_FOR_SOMEONE_ELSE_still_records_its_REAL_filer(self):
        """AUDIT WANTS THE OPPOSITE OF WHAT AUTHORIZATION WANTED.

        While this field was spent as custody, stamping a delegated row was the
        hole. Now that it authorizes nothing, refusing to stamp one would just
        discard the fact — and a delegated row is exactly where knowing who
        filed it is worth most.
        """
        with self._as("sess-A"):
            row = self.file("filed BY sess-A FOR seat-b", "seat-b")
        self.assertEqual(row["reported_session"], "sess-A")
        self.assertEqual(row["owner"], "seat-b",
                         "fixture is wrong: this is not a delegated row")

    def test_a_MATCHING_session_still_cannot_take_a_row_THE_DOOR_IS_SHUT(self):
        """THE ARM THAT KEEPS THIS FIELD FROM BECOMING A CREDENTIAL.

        This is the exact move a probe measured: a caller presents the very
        session the row records — which anyone can read off `public_row` — and
        asks for the row under a different name. It must be refused by the
        ordinary incumbent contract, with the recorded session making NO
        difference whatsoever.

        If someone re-opens the reclaim on this value, this arm goes red. That
        is its whole purpose.
        """
        with self._as("sess-A"):
            row = self.file("a row whose filer is public", "seat-a")
        self.assertEqual(row["reported_session"], "sess-A",
                         "fixture is wrong: with nothing recorded this arm "
                         "would pass without exercising the risk at all")

        with self._as("sess-A"):
            got, err = tasks.update(row["id"], path=self.path,
                                    owner="seat-attacker")
        self.assertIsNone(got, "a caller presenting the recorded session took "
                               "the row — the field has become a bearer token")
        self.assertIn("held by", err)

        # POSITIVE CONTROL, unconditional: update() is not simply refusing
        # everything. A field the incumbent contract does not guard still edits.
        with self._as("sess-A"):
            ok, oerr = tasks.update(row["id"], path=self.path, note="still live")
        self.assertIsNone(oerr, "update refuses every field, so the refusal "
                                "above says nothing: %s" % oerr)
        self.assertEqual(ok["note"], "still live")

    def test_a_DIFFERENT_session_cannot_take_it_either(self):
        """The ordinary contract, unchanged and asserted so the pair above is a
        comparison rather than a single reading."""
        with self._as("sess-A"):
            row = self.file("filed by sess-A", "seat-a")
        with self._as("sess-B"):
            got, err = tasks.update(row["id"], path=self.path, owner="seat-b")
        self.assertIsNone(got)
        self.assertIn("held by", err)

        # POSITIVE CONTROL, unconditional: the SAME foreign session can still
        # edit a field the incumbent contract does not guard. Without it this
        # arm passes against an update() that refuses sess-B everything, and
        # the comparison it exists to make would be a single reading.
        with self._as("sess-B"):
            ok, oerr = tasks.update(row["id"], path=self.path,
                                    note="a foreign but unguarded edit")
        self.assertIsNone(oerr, "the foreign session is refused outright, so "
                                "the owner refusal above is not about owner "
                                "at all: %s" % oerr)
        self.assertEqual(ok["note"], "a foreign but unguarded edit")

    def test_a_FORGED_session_is_stored_and_published_verbatim(self):  # noqa: VACUOUS_ASSERTION — every assertion here compares against a LOCAL NAME (forged/other), and the classifier counts assertEqual as a positive control only against a LITERAL; measured, see the commit. Transcribing the constant would satisfy the rung and weaken the test.
        """THE HONEST BEHAVIOUR, PINNED SO NOBODY CURES IT INTO A LIE.

        This arm asserts a WEAKNESS on purpose. A reviewer measured it by hand
        — export any session id, and it is what the row records and what the
        public surface republishes — and the lane's prose used to claim the
        opposite ("never fabricates", "who authored this"). Pinning the
        forgery closes the gap between what the field DOES and what the file
        SAYS it does, and it makes the next reader who reaches for an
        authorization keyed on this value watch a test tell them it is
        attacker-controlled.

        IT DRIVES THE REAL RESOLVER. Every other arm here patches
        `_reported_session` to control the value, which cannot observe the
        environment read that is the whole finding — a mocked resolver would
        pass this arm while the real one did anything at all.
        """
        forged = "forged-public-value"
        env = dict(os.environ)
        for key in home._SESSION_ENV:
            env.pop(key, None)
        env[home._SESSION_ENV[0]] = forged
        with mock.patch.dict(os.environ, env, clear=True):
            # POSITIVE CONTROL, unconditional: the REAL resolver is what read
            # this, so the assertions below are about the environment read and
            # not about a fixture handing back its own argument.
            self.assertEqual(tasks._reported_session(), forged)
            row, err = tasks.add("a row filed under a forged session",
                                 "seat-a", path=self.path)
        # ASSERT THE EFFECT, NOT THE ABSENCE OF A COMPLAINT. `assertIsNone
        # (err)` says only that nothing objected, which a writer that stored
        # nothing also satisfies; requiring the ROW proves the add happened.
        self.assertIsNotNone(row, "the row was not filed: %s" % (err,))
        self.assertEqual(row["reported_session"], forged,
                         "the forged value was not stored verbatim, so this "
                         "arm is no longer measuring the finding it pins")
        # AND THE PUBLIC SURFACE DOES NOT CARRY IT AT ALL (task/2262). The
        # value is worthless as evidence about its own filer AND it is a
        # working BEARER for somebody else's check: `resolve_actor_reason`
        # corroborates a declared seat name by asking the roster whether the
        # acting session resolves, and it reads that session from the same
        # environment this arm forges. So a published copy is a bearer on a
        # queryable surface. Exposure and prose are one fact: the failure
        # message here, `_reported_session`'s docstring and the `_row`
        # call-site comment all state the same boundary.
        published = tasks.public_row(row)
        self.assertNotIn("reported_session", published,
                         "the public projection still carries the reported "
                         "session, so the corroboration check's bearer is "
                         "back on the wire")
        # AND THE LEDGER STILL HAS IT. Dropping it from the wire must not be
        # implemented by never recording it: the row read above already
        # asserted the stored value, and this re-reads the SAME row object to
        # say the projection copied rather than mutated it.
        self.assertEqual(row["reported_session"], forged,
                         "public_row mutated the row it was given, so the "
                         "audit record is gone rather than merely unpublished")

        # THE CONTROL THAT MAKES THE ABOVE MEAN SOMETHING, unconditional: a
        # SECOND row filed under a DIFFERENT exported id must record THAT one.
        # Without it every assertion above is satisfied by a field that always
        # answers "forged-public-value" for any reason at all — including a
        # constant — and the arm would pin a coincidence instead of the
        # environment read that is the finding.
        other = "a-different-declared-session"
        env2 = dict(env)
        env2[home._SESSION_ENV[0]] = other
        with mock.patch.dict(os.environ, env2, clear=True):
            row2, err2 = tasks.add("a second row, different declared session",
                                   "seat-a", path=self.path)
        self.assertIsNotNone(row2, "the control row was not filed: %s" % (err2,))
        self.assertEqual(row2["reported_session"], other,
                         "the field did not track the environment, so the "
                         "forged value above proved nothing")
        self.assertNotEqual(row2["reported_session"],
                            row["reported_session"])

    def test_NO_field_of_a_published_row_carries_the_acting_session(self):
        """THE ACCEPTANCE, AND IT ASKS ABOUT THE VALUE RATHER THAN THE KEY.

        Asserting that `reported_session` is absent pins one spelling. The
        property is that the corroboration bearer is not on the wire AT ALL,
        and a field added tomorrow that happens to carry it — a denormalized
        audit blob, a `filed_by` convenience, a comment the CLI stamps — would
        satisfy a key check and defeat the purpose. So this asks the RENDERED
        DOCUMENT, which is the thing a reader of `helm task show --json`
        actually receives: keys, nested values and all.

        The seeded id is alphanumeric and dashes, so JSON encodes it
        unchanged and a substring question is the whole question.

        TWO CONTROLS, because a check that finds nothing reads the same as a
        check that cannot see. The SEED control proves the value reached the
        store, so an implementation that never recorded it cannot pass here.
        The INSTRUMENT control puts the same id somewhere the render must
        carry it — a row TITLE — and requires it found there, so "absent"
        means absent rather than unlooked-for.
        """
        seeded = "sess-acceptance-2262"
        env = dict(os.environ)
        for key in home._SESSION_ENV:
            env.pop(key, None)
        env[home._SESSION_ENV[0]] = seeded
        with mock.patch.dict(os.environ, env, clear=True):
            row, err = tasks.add("a row filed under a seeded session",
                                 "seat-a", path=self.path)
            decoy, derr = tasks.add("a title mentioning %s" % seeded,
                                    "seat-a", path=self.path)
        self.assertIsNotNone(row, "the seeded row was not filed: %s" % (err,))
        self.assertIsNotNone(decoy,
                             "the instrument-control row was not filed: %s"
                             % (derr,))
        # SEED CONTROL, unconditional: the value is in the ledger, so the
        # absence below is about the projection and not about a writer that
        # stored nothing.
        self.assertEqual(row["reported_session"], seeded,
                         "the seeded session never reached the store, so the "
                         "assertion below is vacuous")
        # INSTRUMENT CONTROL, unconditional and on the SAME observable: the
        # rendered document of a row that DOES carry the id must contain it.
        self.assertIn(seeded, tasks.as_json(decoy),
                      "the rendered document does not contain an id that is "
                      "in the row's own TITLE, so this check cannot see "
                      "published values and its silence below proves nothing")
        self.assertNotIn(seeded, tasks.as_json(row),
                         "the rendered document carries the acting session "
                         "id, so the roster corroboration check's bearer is "
                         "readable by anyone who can read a task row")

    def test_the_reported_session_is_not_editable(self):
        """IF THIS FIELD WERE WRITABLE even its weak claim would be worthless: any
        row could claim any filer, and a later reader could not tell a record
        from an assertion."""
        with self._as("sess-A"):
            row = self.file("the session is a claim, not a record", "seat-a")
        with self._as("sess-B"):
            got, err = tasks.update(row["id"], path=self.path,
                                    reported_session="sess-B")
        self.assertIsNone(got, "reported_session was accepted as an editable field")
        self.assertIn("reported_session", err)

    def test_an_unknown_reported_session_is_recorded_as_UNKNOWN_not_invented(self):  # noqa: VACUOUS_ASSERTION — the control IS unconditional and on the same field, but on a SECOND row (known), and the classifier requires the positive control to share the absence's BINDING; measured, task/1955
        """NEVER FABRICATE, WHICH IS A NARROWER PROMISE THAN IT SOUNDS.

        The value is already unverifiable — a sibling arm pins that a forged
        export is stored and republished verbatim — so this arm does NOT claim
        the recorded session can be trusted. It claims only that helm does not
        ADD a session of its own invention when the environment offers none.
        An absent value is honest about knowing nothing; a guessed one would
        put helm's name behind a claim no one made.
        """
        with self._as(None):
            row = self.file("no session seam available", "seat-a")
        self.assertIsNone(row["reported_session"],
                          "a reported session was invented where none could be "
                          "established")

        # POSITIVE CONTROL ON THE SAME OBSERVABLE, unconditional: the stamp is
        # not simply broken. A row filed with a READABLE session must carry it,
        # or the None above proves nothing about the never-fabricate rule.
        with self._as("sess-A"):
            known = self.file("the session seam works here", "seat-a")
        self.assertEqual(known["reported_session"], "sess-A",
                         "no row records a session at all, so the absence "
                         "asserted above is the stamp being dead")

    def test_a_malformed_recorded_session_does_not_break_the_verb(self):
        """The ledger is append-only and HAND-EDITABLE, so every reader of this
        field must survive a value of the wrong type. `['x'].strip()` raises
        AttributeError while `[]` merely reads falsy, which is exactly how a
        type defect hides from arms that only try the empty case."""
        with self._as("sess-A"):
            row = self.file("the reported session will be corrupted", "seat-a")
        tid = row["id"]
        for bad in (["x"], [], 17, {"s": 1}):
            with self.subTest(bad=bad):
                self._recorded_session_becomes(tid, bad)
                with self._as("sess-A"):
                    got, err = tasks.update(tid, path=self.path,
                                            note="a routine edit")
                self.assertIsNone(err, "a malformed reported_session broke an "
                                       "ordinary update: %s" % err)
                self.assertEqual(got["note"], "a routine edit")

        # POSITIVE CONTROL, UNCONDITIONAL AND OUTSIDE THE LOOP. Every assertion
        # above lives inside `for bad in (...)`, so an empty iterable — or a
        # subTest that never ran — would pass this arm having checked nothing.
        # A WELL-FORMED session must still permit the same ordinary edit.
        self._recorded_session_becomes(tid, "sess-A")
        with self._as("sess-A"):
            ok, oerr = tasks.update(tid, path=self.path, note="well-formed too")
        self.assertIsNone(oerr, "the ordinary edit fails even on a well-formed "
                                "row, so the loop above proves nothing: %s" % oerr)
        self.assertEqual(ok["note"], "well-formed too")

    def _recorded_session_becomes(self, tid, value):
        """Rewrite one row's reported_session THROUGH THE STORE FILE, the way a
        hand-edit or a foreign writer would — never through update(), which
        refuses the field by design and is the thing under test."""
        out = []
        for ln in open(self.path, encoding="utf-8").read().splitlines():
            try:
                obj = json.loads(ln)
            except ValueError:
                out.append(ln)
                continue
            if obj.get("id") == tid:
                obj["reported_session"] = value
            out.append(json.dumps(obj))
        open(self.path, "w", encoding="utf-8").write("\n".join(out) + "\n")


class CloseReasonUsesTheSameDoorTest(CliBase):
    """task/2266. `close` joined its reason from the tail with no guard, so an
    unimplemented flag became the DURABLE closed_reason and the verb printed
    its ordinary success line.

    TWO LIVE INSTANCES ON THE LEDGER, both a reviewer's, both on this verb:
    `close <id> --reason <text>` stored "--reason <text>", and `close <id> --
    <text>` stored "-- <text>" — so the escape an operator learns from the
    CURED verb silently corrupted the record here. That ordering is why both
    halves ship together: a refusal without an escape turns a corrupted record
    into a command with no way through.

    THE ROW SAID FOUR SIBLING VERBS AND THE MEASURED ANSWER IS ONE. Driven
    through the real CLI against an isolated home: `standdown` REFUSES an
    unrecognised option, `takeover` refuses a leftover flag, `comment` refuses
    through this same door, and `close` is the only tail that took one. `claim`
    takes no free text at all — it silently IGNORES an unknown flag, which is
    a different defect and not this one.

    THE STORED REASON IS THE OBSERVABLE, never the exit code: an arm reading
    only rc would pass against a build that refused for any other reason.
    """

    def _row(self, tid):
        return tasks.rows(path=tasks.ledger_path())[tid]

    def test_an_unimplemented_flag_does_not_become_the_close_reason(self):
        self.cli("add", "carrier", "--mine")
        tid = self.filed("carrier")["id"]
        # THE INSTRUMENT IS PROVEN TO WRITE BEFORE ITS SILENCE IS READ.
        rc, _out, err = self.cli("close", tid, "an ordinary reason lands")
        self.assertEqual(rc, 0, err)
        self.assertIn("an ordinary reason lands",
                      str(self._row(tid).get("closed_reason")),
                      "the close door does not record a reason at all, so the "
                      "refusal below measures nothing")

        self.cli("add", "victim", "--mine")
        vid = self.filed("victim")["id"]
        rc, _out, err = self.cli("close", vid, "--reason", "duplicate of x")
        self.assertEqual(rc, 2, "an unimplemented flag was accepted as a "
                                "close reason")
        self.assertIn("is not a close reason", err)
        self.assertIsNone(self._row(vid).get("closed_reason"),
                          "the refusal still recorded a reason, so the flag "
                          "reached the record after all: %r"
                          % (self._row(vid).get("closed_reason"),))

    def test_the_escape_the_cured_verb_teaches_now_works_here_too(self):
        """THE SECOND LIVE INSTANCE. `close <id> -- <text>` stored the literal
        `-- ` prefix, so an operator who had learned the idiom from `comment`
        corrupted the record by following the documented advice."""
        self.cli("add", "escapee", "--mine")
        tid = self.filed("escapee")["id"]
        rc, _out, err = self.cli("close", tid, "--",
                                 "--reason is genuinely my text")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self._row(tid).get("closed_reason"),
                         "--reason is genuinely my text",
                         "the escape did not escape — the record still "
                         "carries a token the operator did not mean, or lost "
                         "the text that followed it")

    def test_a_bare_escape_refuses_rather_than_recording_nothing(self):
        """`close <id> --` asks for a reason and supplies none. Recording an
        empty reason would be a terminal state with no account of itself."""
        self.cli("add", "bare", "--mine")
        tid = self.filed("bare")["id"]
        rc, _out, err = self.cli("close", tid, "--")
        self.assertEqual(rc, 2, "a bare escape closed the row with no reason")
        self.assertIn("needs a close reason after", err)
        # THE ROW ITSELF IS THE OBSERVABLE, bound once and asked twice. The
        # POSITIVE half — it is still OPEN — is the stronger claim and is what
        # makes the absence below mean something: without it, "no reason
        # recorded" is equally satisfied by a row that was closed and simply
        # never carried one.
        row = self._row(tid)
        self.assertEqual(row["status"], "open",
                         "the bare escape closed the row anyway, so refusing "
                         "the reason bought nothing: %r" % (row["status"],))
        self.assertIsNone(row.get("closed_reason"),
                          "a reason was recorded for a refused close")

    def test_one_door_serves_both_verbs_and_says_each_ones_noun(self):
        """THE POINT OF THE SHARED HELPER, and the thing a copied guard loses:
        the refusal speaks the VERB'S OWN vocabulary while the escape it
        teaches is spelled once. The nouns below differ per verb; the sentence
        that tells an operator how to get through is byte-identical, because it
        now comes from `helm.freetext` rather than from each verb's own copy.
        A third tail added tomorrow inherits the second half for free, which is
        the whole reason the door is shared."""
        self.cli("add", "nouns", "--mine")
        tid = self.filed("nouns")["id"]
        _rc, _out, close_err = self.cli("close", tid, "--reason", "x")
        _rc, _out, comment_err = self.cli("comment", tid, "--stdin")
        self.assertIn("is not a close reason", close_err)
        self.assertIn("is not comment text", comment_err)
        escape = "put `--` before it"
        for err in (close_err, comment_err):
            self.assertIn(escape, err,
                          "a refusal does not teach the escape, so an "
                          "operator is walled with no way through: %r" % err)


class BoardOrderIsRankThenAgeTest(TasksBase):
    """THE ORDER THE OWNER CAN CROSS-CHECK (task/2622).

    He asked: "how am I supposed to help or cross-check the priority ordering
    of such a massive outstanding list". `rank_key` answers "most important
    first" and then falls through to THE NUMBERING, which is filing order only
    for rows this ledger minted — a migrated row keeps the number it was born
    with — so inside one rank the board was ordered by which private list a
    row came out of. AGE is the half he can check without reading prose."""

    NOW = 1_700_000_000.0

    def row(self, tid, priority=None, ago_days=0, ts=None):
        return {"id": tid, "priority": priority, "status": "open",
                "ts": self.NOW - ago_days * 86400.0 if ts is None else ts}

    def test_board_key_sorts_P0_first_then_OLDEST_first_inside_the_rank(self):
        rows = [self.row("task/10", "P1", ago_days=1),
                self.row("task/11", "P0", ago_days=2),
                self.row("task/12", "P1", ago_days=40),
                self.row("task/13", "P0", ago_days=90),
                self.row("task/14", "P2", ago_days=300),
                self.row("task/15", None, ago_days=900)]
        got = [r["id"] for r in sorted(rows, key=tasks.board_key)]
        self.assertEqual(
            ["task/13", "task/11", "task/12", "task/10", "task/14",
             "task/15"], got,
            "the board did not put P0 first, oldest first inside each rank, "
            "and UNRANKED last")

    def test_the_AGE_is_what_decides_and_the_NUMBERING_does_not_override_it(self):
        """POSITIVE CONTROL ON THE SAME CALL: swap only the two stamps and the
        two rows swap places. Without this the arm above would pass on a key
        that merely sorted by id, since the fixture's ids and ages agree."""
        young, old = self.row("task/4", "P1", ago_days=1), \
            self.row("task/40", "P1", ago_days=60)
        self.assertEqual(["task/40", "task/4"],
                         [r["id"] for r in sorted([young, old],
                                                  key=tasks.board_key)],
                         "the OLDER row did not sort first")
        young["ts"], old["ts"] = old["ts"], young["ts"]
        self.assertEqual(["task/4", "task/40"],
                         [r["id"] for r in sorted([young, old],
                                                  key=tasks.board_key)],
                         "moving the stamps did not move the order, so this "
                         "key is reading the id and not the age")

    def test_an_UNDATED_row_sorts_after_the_dated_ones_in_its_rank(self):
        """A stamp nobody could read may not claim the top of the board. This
        is `stamp_epoch`'s None-never-0 law reaching the order: an unreadable
        ts rendered as epoch 0 would make every legacy row the oldest P0."""
        dated = self.row("task/2", "P1", ago_days=500)
        undated = self.row("task/1", "P1", ts="not a timestamp")
        self.assertEqual(["task/2", "task/1"],
                         [r["id"] for r in sorted([undated, dated],
                                                  key=tasks.board_key)])
        # AND THE RANK STILL WINS OVER IT — an undated P0 outranks a dated P1,
        # so the age tier is a tie-break inside a rank and never a rank of its
        # own.
        undated["priority"] = "P0"
        self.assertEqual(["task/1", "task/2"],
                         [r["id"] for r in sorted([undated, dated],
                                                  key=tasks.board_key)])

    def test_the_two_stamp_spellings_this_ledger_actually_holds_both_read(self):
        """Rows minted here carry a `time.time()` float; rows migrated out of
        the private list carry ISO. A reader that knows one of them ages half
        the backlog wrong."""
        self.assertEqual(1_700_000_000.0,
                         tasks.stamp_epoch(1_700_000_000.0))
        self.assertEqual(1_754_352_000.0,
                         tasks.stamp_epoch("2025-08-05T00:00:00Z"))
        for junk in (None, "", "soon", 0, -1, [], {}):
            self.assertIsNone(tasks.stamp_epoch(junk),
                              "%r was read as a moment in time" % (junk,))

    def test_the_ONE_parser_refuses_every_value_that_is_not_an_INSTANT(self):
        """FINDING 5. Every age on every surface is now this function's
        answer, so a value it mis-reads becomes a confident number in the
        owner's browser with nothing left to contradict it.

        EACH POLE IS ITS OWN FAILURE MODE, not a list of synonyms for junk:
          bool     `float(True)` is 1.0 and Python calls bools ints, so the
                   numeric branch dated every True to one second after the
                   epoch and rendered a confident 56-year age.
          NaN      passes no comparison at all, so it slips through any
                   `> 0` guard's opposite and through any ordering silently.
          inf      `float("inf") > 0` is True, so an infinite stamp passed
                   and produced an age of minus infinity one subtraction on.
          0/-1     "we do not know when" and "just now" are the two answers
                   an age question must never confuse."""
        for bad, why in ((True, "a bool"), (False, "a bool"),
                         (float("nan"), "NaN"),
                         (float("inf"), "infinity"),
                         (float("-inf"), "negative infinity"),
                         (0, "zero"), (0.0, "zero"), (-1, "a negative"),
                         (-1_700_000_000.0, "a negative epoch"),
                         ("", "the empty string"), (None, "absent"),
                         ("not a timestamp", "prose"),
                         ("2026-13-45T99:99:99Z", "an impossible date"),
                         ([], "a list"), ({}, "a dict")):
            with self.subTest(why):
                self.assertIsNone(tasks.stamp_epoch(bad),
                                  "%s was read as a moment in time" % why)
        # THE UNCONDITIONAL POSITIVE CONTROL on the same function: it DOES
        # answer for real stamps, so the Nones above are discrimination and
        # not a parser that has stopped parsing.
        self.assertEqual(1_700_000_000.0,
                         tasks.stamp_epoch(1_700_000_000.0),
                         "MUST-HIT: the parser reads no stamp at all")

    def test_every_OFFSET_spelling_of_one_instant_reads_as_that_instant(self):  # noqa: VACUOUS_ASSERTION — the closing assertNotEqual is the "absence" the rung sees; its unconditional positive control on the SAME function is the assertEqual ahead of the loop, which pins the plain spelling to a non-empty literal instant before any spelling is varied
        """Four spellings of one moment are real in this ledger's history, and
        refusing three of them would call a dated row undated — which sorts it
        to the bottom of its rank and renders its age as UNKNOWN. The offset is
        APPLIED rather than dropped: dropping it misdates a row by up to half
        a day and calls the answer certain."""
        want = 1_789_554_602.0
        # UNCONDITIONAL, AHEAD OF THE LOOP: this parser answers for the plain
        # spelling at all. A control that only runs inside an iteration cannot
        # prove the parser fired if the fixture tuple is ever emptied.
        self.assertEqual(want, tasks.stamp_epoch("2026-09-16T10:30:02Z"),
                         "MUST-HIT: the parser reads no ISO stamp at all, so "
                         "every spelling below is vacuous")
        for spelling in ("2026-09-16T10:30:02Z",
                         "2026-09-16T10:30:02",
                         "2026-09-16T10:30:02+00:00",
                         "2026-09-16T10:30:02.500Z",
                         "2026-09-16 10:30:02Z",
                         "2026-09-16T12:30:02+02:00",
                         "2026-09-16T05:30:02-0500"):
            with self.subTest(spelling):
                self.assertEqual(want, tasks.stamp_epoch(spelling),
                                 "%r did not read as the instant it names"
                                 % spelling)
        # THE CONTROL: a DIFFERENT instant must not read as this one, or the
        # arm above would pass on a parser returning a constant.
        self.assertNotEqual(want, tasks.stamp_epoch("2026-09-16T11:30:02Z"))

    def test_row_ages_answers_NONE_and_never_ZERO_for_a_stamp_it_cannot_read(self):  # noqa: VACUOUS_ASSERTION — the assertIsNone block is the absence; its unconditional positive control on the SAME function and the SAME four fields is the `dated` block immediately above it, which pins age_s, noted_age_s and ts_epoch to non-empty literals
        """The wire carries numbers or null and the browser can render only
        the word for a null. A 0 here would arrive as "just now"."""
        # THE UNCONDITIONAL CONTROL FIRST, ON THE SAME FUNCTION AND THE SAME
        # FIELDS: a dated row gets numbers. Without it, "None" below is
        # equally satisfied by a function that answers None for everything.
        dated = tasks.row_ages({"id": "task/2", "ts": self.NOW - 3 * 86400},
                               now=self.NOW)
        self.assertEqual(3 * 86400, dated["age_s"])
        self.assertEqual(3 * 86400, dated["noted_age_s"])
        self.assertEqual(self.NOW - 3 * 86400, dated["ts_epoch"])
        self.assertIs(False, dated["stale"])
        blind = tasks.row_ages({"id": "task/1", "ts": "not a timestamp"},
                               now=self.NOW)
        self.assertIsNone(blind["ts_epoch"])
        self.assertIsNone(blind["age_s"])
        self.assertIsNone(blind["noted_age_s"])
        self.assertIs(False, blind["stale"],
                      "an undated row was MARKED stale, which claims a "
                      "measurement nobody made")
        stale = tasks.row_ages({"id": "task/3", "ts": self.NOW - 30 * 86400},
                               now=self.NOW)
        self.assertIs(True, stale["stale"],
                      "a row nobody has written on in 30 days is not stale, "
                      "so the seven-day line is not being applied")

    def test_queue_totals_counts_live_work_and_never_claims_an_UNKNOWN_oldest(self):
        """The headline the owner reads, counted once on the server so the
        board home and the work card cannot hold two versions of it."""
        rows = [self.row("task/1", "P0", ago_days=9),
                self.row("task/2", "P0", ago_days=2),
                self.row("task/3", "P1", ago_days=40),
                self.row("task/4", "P2"), self.row("task/5", "P3"),
                self.row("task/6"),
                dict(self.row("task/7", "P2"), status="in_progress"),
                dict(self.row("task/8", "P2"), status="closed")]
        t = tasks.queue_totals(rows, now=self.NOW)
        self.assertEqual(2, t["P0"])
        self.assertEqual(1, t["P1"])
        self.assertEqual(2, t["P2"], "a CLOSED row was counted as live work")
        self.assertEqual(1, t["P3"])
        self.assertEqual(1, t["unranked"],
                         "UNRANKED was folded into a rank — nobody-has-judged "
                         "and judged-lowest are different answers")
        self.assertEqual(1, t["in_progress"])
        self.assertEqual(7, t["live"])
        self.assertEqual(9 * 86400, t["oldest"]["P0"])
        self.assertEqual(40 * 86400, t["oldest"]["P1"])
        # AN UNDATED P0 COUNTS IN ITS RANK AND NEVER CLAIMS THE OLDEST.
        blind = tasks.queue_totals(
            [self.row("task/9", "P0", ts="not a timestamp")], now=self.NOW)
        self.assertEqual(1, blind["P0"], "the row vanished from its rank")
        self.assertIsNone(blind["oldest"]["P0"],
                          "a stamp nobody could read claimed the top of the "
                          "headline")

    def test_ONE_ordering_serves_the_key_and_the_order(self):
        """`board_order` is what both doors call. A published KEY is an
        invitation each caller answers its own way, which is exactly how the
        route and `helm task list` came to sort one snapshot two ways."""
        rows = {r["id"]: r for r in
                [self.row("task/10", "P1", ago_days=1),
                 self.row("task/11", "P0", ago_days=2),
                 self.row("task/12", "P1", ago_days=40)]}
        # MUST-HIT, UNCONDITIONAL: the ordering returns every row it was
        # given. Two functions that both drop everything would otherwise
        # "agree" perfectly.
        self.assertEqual(["task/11", "task/12", "task/10"],
                         [r["id"] for r in tasks.board_order(rows)],
                         "the ordering did not return the rows in board order")
        self.assertEqual([r["id"] for r in sorted(rows.values(),
                                                  key=tasks.board_key)],
                         [r["id"] for r in tasks.board_order(rows)],
                         "the published ordering disagrees with the published "
                         "key it is supposed to BE")
        # IT TAKES EITHER SHAPE, because one caller holds the snapshot dict
        # and the other holds a filtered list.
        self.assertEqual([r["id"] for r in tasks.board_order(rows)],
                         [r["id"] for r in
                          tasks.board_order(list(rows.values()))])

    def test_noted_epoch_reads_the_newest_comment_and_falls_back_to_filing(self):
        """"No note in seven days" has to be answerable for a row carrying no
        notes at all, which is most of a backlog nobody is reading."""
        bare = {"id": "task/1", "ts": self.NOW - 100}
        self.assertIsNone(tasks.last_note(bare))
        self.assertEqual(self.NOW - 100, tasks.noted_epoch(bare))
        noted = dict(bare, comments=[{"ts": self.NOW - 90, "text": "older"},
                                     {"ts": self.NOW - 5, "text": "newest"}])
        self.assertEqual("newest", tasks.last_note(noted)["text"],
                         "the APPEND order is the order — the last element is "
                         "the newest comment")
        self.assertEqual(self.NOW - 5, tasks.noted_epoch(noted))


class AskReflexRefusesANearDuplicateTest(CliBase):
    """THE OWNER'S SECOND QUESTION (task/2622), in his words: "how do we
    prevent me continually asking for things that have been placed on the list
    and ignored". 477 open rows cannot be scanned by the person filing row
    478, so the door resolves the title against the backlog itself."""

    FIRST = "the board shows the task queue by priority with age and owner"

    def test_a_near_duplicate_title_is_REFUSED_and_NOTHING_is_filed(self):  # noqa: VACUOUS_ASSERTION — the absence assertion is "the ledger did not grow", which cannot pin a non-empty literal; its unconditional control is the assertGreater on the SAME ledger_lines() reader after a distinct title gets through, so a broken counter fails this arm rather than passing it
        rc, _out, _err = self.cli("add", self.FIRST, "--owner", "seat-a")
        self.assertEqual(0, rc, "the fixture row did not file")
        before = self.ledger_lines()

        rc, _out, err = self.cli(
            "add", "the board shows the task queue by priority with owner and "
            "age", "--owner", "seat-b")
        # MUST-HIT: the refusal fired at all. Without this the two assertions
        # below would pass over a door that simply errored for some other
        # reason, or that filed happily while the ledger count moved for a
        # reason this arm cannot see.
        self.assertEqual(2, rc, "the near-duplicate was not refused: %r" % err)
        self.assertIn("REFUSED", err)
        self.assertIn(self.filed(self.FIRST)["id"], err,
                      "the refusal did not name the row that already exists, "
                      "so it tells the operator to stop without telling them "
                      "where to go")
        self.assertIn("--force-new", err, "a refusal with no escape hatch")
        self.assertEqual(before, self.ledger_lines(),
                         "a REFUSED add still wrote to the ledger")
        # THE UNCONDITIONAL POSITIVE CONTROL ON THE SAME OBSERVABLE. An
        # unchanged line count proves a refusal only if this counter moves
        # when an add DOES get through — otherwise a broken reader and a
        # working guard are the same measurement.
        self.cli("add", "rotate the moonshot credential pool",
                 "--owner", "seat-b")
        self.assertGreater(self.ledger_lines(), before,
                           "the ledger line counter never moves, so the "
                           "assertion above measures nothing")

    def test_a_DISTINCT_title_still_files(self):  # noqa: VACUOUS_ASSERTION — rc 0 is the "empty" observable here and it cannot carry a non-empty literal; the unconditional control on the same act is the assertGreater on ledger_lines plus the assertIn("filed") on stdout
        """THE CONTROL THAT MAKES THE REFUSAL MEAN SOMETHING. A guard that
        refused everything would satisfy the arm above."""
        self.cli("add", self.FIRST, "--owner", "seat-a")
        before = self.ledger_lines()
        rc, out, err = self.cli(
            "add", "rotate the moonshot credential pool before it walls",
            "--owner", "seat-b")
        self.assertEqual(0, rc, "an unrelated title was refused: %r" % err)
        self.assertIn("filed", out)
        self.assertGreater(self.ledger_lines(), before)

    def test_force_new_files_over_the_refusal_and_SAYS_it_did(self):  # noqa: VACUOUS_ASSERTION — same shape: rc 0 is the empty observable, and the unconditional controls on the same act are the assertIn("--force-new") on stderr and the assertGreater on ledger_lines
        """The operator may be right that the overlap is a coincidence. What
        the door may not do is let that override happen silently."""
        self.cli("add", self.FIRST, "--owner", "seat-a")
        before = self.ledger_lines()
        rc, _out, err = self.cli(
            "add", "the board shows the task queue by priority with owner and "
            "age", "--owner", "seat-b", "--force-new")
        self.assertEqual(0, rc, "--force-new did not get through: %r" % err)
        self.assertIn("--force-new", err)
        self.assertGreater(self.ledger_lines(), before)

    def test_the_REFUSAL_names_the_closest_rows_with_their_owners_and_scores(self):
        """"Stop" is half the cure; "go here instead" is the other half, and
        it has to travel with the refusal because the refusal is the only
        thing the filer reads.

        THE NEIGHBOURS ARE NAMED BY THE REFUSAL AND NOWHERE ELSE, because
        the resolve happens once — inside `add()`, on the snapshot the write
        itself extends. A listing printed by the CLI off its own ledger read
        would be a SECOND version of the backlog taken outside the write lock,
        on the door whose whole subject is whether the backlog already holds
        this row."""
        self.cli("add", self.FIRST, "--owner", "seat-a")
        self.cli("add", "rotate the moonshot credential pool",
                 "--owner", "seat-c")
        first = self.filed(self.FIRST)["id"]
        rc, _out, err = self.cli(
            "add", "the board shows the task queue by priority with owner and "
            "age", "--owner", "seat-b")
        self.assertEqual(2, rc, "the near-duplicate was not refused: %r" % err)
        self.assertIn("The closest OPEN rows to this title:", err,
                      "the refusal named no neighbours at all")
        self.assertIn(first, err)
        self.assertIn("seat-a", err,
                      "the listing does not say who holds the row it is "
                      "sending the filer to")
        self.assertIn("100%", err,
                      "the listing does not say how close the match was, so "
                      "the operator cannot judge the refusal")

    # ---- finding 3: one tokenizer, one threshold, every producer -----------

    def test_the_threshold_and_the_tokenizer_ARE_the_stores_and_not_a_copy(self):
        """A second literal that merely agrees today is not one rule. This
        binds the two objects by identity, so retuning the store moves this
        door and a copy re-appearing here goes red."""
        from helm.store import index as storeindex
        self.assertIs(storeindex.DUP_OVERLAP, tasks.DUP_OVERLAP,
                      "the task door holds its own copy of the overlap "
                      "threshold, so retuning the store's leaves this one "
                      "behind silently")
        # AND THE SPLIT IS THE STORE'S TOO — asserted by behaviour, since a
        # stoplist legitimately differs per field: the same sentence minus the
        # grammar must come out of both. MUST-HIT FIRST, unconditional: both
        # sides produce WORDS, or two empty sets would compare equal and prove
        # nothing at all.
        self.assertEqual({"rotate", "moonshot", "pool"},
                         tasks.title_tokens("Rotate the moonshot pool"),
                         "MUST-HIT: the title tokenizer produced no words")
        self.assertEqual(
            storeindex.dup_tokens("Rotate the moonshot pool",
                                  tasks.TITLE_STOPWORDS),
            tasks.title_tokens("Rotate the moonshot pool"),
            "the title tokenizer is no longer the store's splitter")

    def test_a_title_and_its_NEGATION_are_not_the_same_row(self):  # noqa: VACUOUS_ASSERTION — rc 0 is the "empty" observable and cannot pin a non-empty literal; the unconditional controls on the same observables are the assertIn("filed") on stdout, the assertGreater on ledger_lines, and the rc-2 re-file of the UNNEGATED title which proves the guard is still firing
        """THE WORST THING A DUPLICATE GUARD CAN DO is refuse the correction
        to a row already filed. "not" was in the stoplist, so a statement and
        its opposite tokenized identically and scored 100%."""
        self.cli("add", "the wall verdict anchors to the recorded tip",
                 "--owner", "seat-a")
        before = self.ledger_lines()
        rc, out, err = self.cli(
            "add", "the wall verdict does not anchor to the recorded tip",
            "--owner", "seat-b")
        self.assertEqual(0, rc,
                         "a title stating the OPPOSITE of a filed row was "
                         "refused as a copy of it: %r" % err)
        self.assertIn("filed", out)
        self.assertGreater(self.ledger_lines(), before,
                           "the correction never reached the ledger")
        # THE CONTROL ON THE SAME PAIR: drop the polarity word and the two
        # titles ARE the same row, so this arm is measuring the word rather
        # than a guard that stopped working.
        rc, _out, err = self.cli(
            "add", "the wall verdict anchors to the recorded tip",
            "--owner", "seat-b")
        self.assertEqual(2, rc, "the duplicate guard itself stopped firing, "
                                "so the arm above proves nothing: %r" % err)

    def test_an_EXACT_CJK_duplicate_is_seen_as_a_duplicate(self):  # noqa: VACUOUS_ASSERTION — the absence is "the ledger did not grow"; its unconditional controls on the same observables are the rc-0 first file and the assertTrue(title_tokens(title)) must-hit, which proves the tokenizer saw words rather than the refusal being an UNKNOWN
        """The ASCII `[^a-z0-9]+` split dropped every non-Latin character, so
        two byte-identical CJK titles both tokenized to the EMPTY SET, scored
        0.0 against each other and were called distinct — a duplicate detector
        that cannot see an exact copy."""
        title = "信号器導出缺失"
        rc, _out, err = self.cli("add", title, "--owner", "seat-a")
        self.assertEqual(0, rc, "the fixture row did not file: %r" % err)
        self.assertTrue(tasks.title_tokens(title),
                        "MUST-HIT: the tokenizer produced nothing for a CJK "
                        "title, so the refusal below could only be UNKNOWN")
        before = self.ledger_lines()
        rc, _out, err = self.cli("add", title, "--owner", "seat-b")
        self.assertEqual(2, rc, "an EXACT CJK duplicate was filed again")
        self.assertIn("REFUSED", err)
        self.assertEqual(before, self.ledger_lines())

    def test_a_STOPWORD_ONLY_title_is_UNKNOWN_and_refuses_rather_than_filing(self):  # noqa: VACUOUS_ASSERTION — the absence is "the ledger did not grow"; the unconditional control on the SAME counter is the assertGreater after the --force-new re-file, so a broken line counter fails this arm rather than passing it
        """A guard that opens whenever it is confused guards nothing. A title
        made only of grammar has no comparable words, so whether it is already
        on the list is UNKNOWN — and the first cut filed it as PROVEN
        distinct."""
        # A ROW FIRST, so the ledger FILE exists and the counter below has
        # something to be an unchanged count OF.
        rc, _out, err = self.cli("add", "rotate the moonshot credential pool",
                                 "--owner", "seat-a")
        self.assertEqual(0, rc, err)
        before = self.ledger_lines()
        rc, _out, err = self.cli("add", "the and of it", "--owner", "seat-a")
        self.assertEqual(2, rc, "a title with no comparable words was filed "
                                "as proven distinct: %r" % err)
        self.assertIn("UNKNOWN", err)
        self.assertEqual(before, self.ledger_lines(),
                         "a REFUSED add still wrote to the ledger")
        # AND THE EXPLICIT OVERRIDE IS THE ONLY WAY THROUGH.
        rc, _out, err = self.cli("add", "the and of it", "--owner", "seat-a",
                                 "--force-new")
        self.assertEqual(0, rc, "--force-new did not get through: %r" % err)
        self.assertGreater(self.ledger_lines(), before)

    def test_a_MALFORMED_title_refuses_with_UNKNOWN_rather_than_RAISING(self):  # noqa: VACUOUS_ASSERTION — assertIsNone(row) is the empty observable; the unconditional control on the same door is the well-formed add after the loop, which must return a row and no error
        """The direct producer takes whatever a caller hands it. The first cut
        called `.lower()` on it, so a non-string title put an AttributeError
        traceback on the filer instead of a refusal."""
        for bad in (123, ["a", "list"], {"a": "dict"}, object()):
            with self.subTest(repr(bad)[:30]):
                # NO RAISE is the first half and the refusal is the second;
                # an arm asserting only "no exception" would pass over a door
                # that silently filed the garbage.
                row, err = tasks.add(bad, owner="seat-a")
                self.assertIsNone(row, "a malformed title was FILED")
                self.assertIsNotNone(err, "a malformed title was accepted")
        # THE CONTROL: the same door files a well-formed title, so the
        # refusals above are discrimination and not a door that refuses all.
        row, err = tasks.add("rotate the moonshot credential pool",
                             owner="seat-a")
        self.assertIsNone(err, err)
        self.assertIsNotNone(row)

    def test_the_DIRECT_producer_refuses_the_duplicate_the_CLI_refuses(self):
        """FINDING 3'S CORE. The guard lived at the CLI door only, so the same
        title filed through `tasks.add()` — the todo bridge, the resume-turn
        recovery, any script — was never resolved at all and the duplicate
        walked in through the door the guard was not on."""
        first, err = tasks.add(self.FIRST, owner="seat-a")
        self.assertIsNone(err, err)
        row, err = tasks.add(self.FIRST, owner="seat-b")
        self.assertIsNone(row, "the direct producer filed a duplicate")
        self.assertIsNotNone(err)
        self.assertIn("REFUSED", err)
        self.assertIn(first["id"], err)
        # AND ONLY THE EXPLICIT FLAG BYPASSES IT — the same bit the CLI's
        # --force-new sets, so mirror and the recovery path opt out by NAME
        # rather than by using a door the guard is not on.
        forced, err = tasks.add(self.FIRST, owner="seat-b", force_new=True)
        self.assertIsNone(err, err)
        self.assertIsNotNone(forced)

    def test_a_CONCURRENT_add_through_the_direct_producer_cannot_file_twice(self):
        """THE RACE THE CLI-DOOR VERSION COULD NOT CLOSE. That guard read the
        ledger OUTSIDE the write lock, so two adds of one title could each
        resolve against a backlog that did not yet hold the other and both be
        filed. The guard now runs on `existing` — the strict snapshot the
        append itself extends — so the second writer sees the first's row.

        RUN, NOT REASONED ABOUT: real threads through the real producer."""
        import threading
        title = "rotate the moonshot credential pool before it walls"
        results, lock = [], threading.Lock()

        def file_it():
            row, err = tasks.add(title, owner="seat-a")
            with lock:
                results.append((row, err))

        threads = [threading.Thread(target=file_it) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        filed = [row for row, err in results if row is not None]
        self.assertEqual(6, len(results), "a racing thread never answered")
        # MUST-HIT: at least one got through, so "only one" is a measurement
        # and not a producer that refused everything under contention.
        self.assertEqual(1, len(filed),
                         "%d concurrent adds of ONE title reached the ledger"
                         % len(filed))
        rows = [r for r in tasks.rows(path=tasks.ledger_path()).values()
                if r.get("title") == title]
        self.assertEqual(1, len(rows),
                         "the ledger holds %d rows for one title" % len(rows))
        for row, err in results:
            if row is None:
                self.assertIn("REFUSED", err,
                              "a losing writer failed for some other reason: "
                              "%r" % err)

    def test_near_duplicates_ranks_OPEN_rows_only_and_scores_them(self):
        """A closed row with the same title is history. Refusing new work
        because the same thing was finished months ago is the opposite of
        useful, so the detector never returns one."""
        rows = [
            {"id": "task/1", "status": "open",
             "title": "rotate the moonshot credential pool"},
            {"id": "task/2", "status": "closed",
             "title": "rotate the moonshot credential pool"},
            {"id": "task/3", "status": "open",
             "title": "teach the board to render the oldest P0 age"},
        ]
        got = tasks.near_duplicates("rotate the moonshot credential pool",
                                    rows)
        self.assertEqual(["task/1"], [r["id"] for r, _ov in got],
                         "the detector reached a closed row, or missed the "
                         "open one")
        self.assertEqual(1.0, got[0][1],
                         "an identical title did not score as identical")
        # AND THE THRESHOLD IS THE DOOR'S, NOT THE DETECTOR'S: a weak match is
        # RETURNED with its score so the listing and the refusal read the same
        # numbers.
        weak = tasks.near_duplicates("rotate the board", rows)
        self.assertTrue(weak, "a partial word overlap scored nothing at all")
        self.assertLess(weak[0][1], tasks.DUP_OVERLAP)

    def test_the_threshold_lets_through_the_pairs_the_LIVE_ledger_proved_distinct(self):
        """THE STRICTNESS CONTROL, written from a measurement rather than from
        taste. Scored over the live backlog at 1837 open rows, a 0.6 threshold
        refused 116 rows and a 0.8 threshold refused 28 — and the difference is
        made of pairs that are NOT duplicates, because task titles are short
        enough that three content words reach 0.67 while differing by the only
        word that matters. These two pairs are taken verbatim from that run."""
        pairs = [("Bind task 872 round-two verdict",
                  "Bind task 872 round-six verdict"),
                 ("Re-review binding provenance successor",
                  "Re-review empty binding successor")]
        # UNCONDITIONAL FIRST, OUTSIDE THE LOOP. A control that only runs
        # inside an iteration cannot prove the detector fired at all if the
        # fixture list is ever emptied or the loop is ever short-circuited.
        head = tasks.near_duplicates(
            pairs[0][1], [{"id": "task/1", "status": "open",
                           "title": pairs[0][0]}])
        self.assertEqual("task/1", head[0][0]["id"],
                         "the detector found nothing at all, so every "
                         "threshold assertion below is vacuous")
        self.assertGreater(head[0][1], 0.5)
        for first, second in pairs:
            rows = [{"id": "task/1", "status": "open", "title": first}]
            got = tasks.near_duplicates(second, rows)
            # MUST-HIT: the pair really does score, so this arm is measuring
            # the THRESHOLD and not a detector that found nothing.
            self.assertTrue(got, "%r scored nothing against %r"
                            % (second, first))
            self.assertGreater(got[0][1], 0.5,
                               "the pair is not even a near miss, so it "
                               "cannot demonstrate where the line sits")
            self.assertLess(got[0][1], tasks.DUP_OVERLAP,
                            "%r would be REFUSED as a duplicate of %r, and "
                            "the live ledger says they are different work"
                            % (second, first))
        # AND THE OTHER SIDE OF THE LINE, from the same run: a word-order
        # variant of one title IS refused.
        rows = [{"id": "task/1", "status": "open",
                 "title": "Run focused verification and review diff"}]
        got = tasks.near_duplicates(
            "Run focused verification and diff review", rows)
        self.assertGreaterEqual(got[0][1], tasks.DUP_OVERLAP)


class ExpectedRowTest(TasksBase):
    """task/1738: a sweep that decides from a snapshot hands the writer the row
    it judged, and the writer refuses under its lock when the row moved. The
    same reason RankRule exists: a decision made outside the lock is about a
    row that may no longer exist."""

    def lines(self):
        try:
            with open(self.path, encoding="utf-8") as fh:
                return sum(1 for line in fh if line.strip())
        except FileNotFoundError:
            return 0

    def judged_then_touched(self):
        row, err = tasks.add("a mirrored scratchpad item", "", path=self.path)
        self.assertIsNone(err, err)
        judged = tasks.rows(self.path)[row["id"]]
        _c, err = tasks.comment(row["id"], "a seat was here", by="seat-a",
                                path=self.path)
        self.assertIsNone(err, err)
        return row["id"], judged

    def test_a_STALE_expect_closes_nothing_and_says_skipped(self):
        rid, judged = self.judged_then_touched()
        before = self.lines()
        self.assertGreater(before, 0)
        got, why = tasks.close(rid, "source finished", path=self.path,
                               expect=judged)
        self.assertIs(got, tasks.SKIPPED)
        self.assertIn("changed since", why)
        self.assertEqual(self.lines(), before)
        self.assertEqual(tasks.rows(self.path)[rid]["status"], "open")
        # UNCONDITIONAL POSITIVE CONTROL: the row as it stands now closes.
        current = tasks.rows(self.path)[rid]
        got, err = tasks.close(rid, "source finished", path=self.path,
                               expect=current)
        self.assertIsNone(err, err)
        self.assertEqual(got["status"], "closed")

    def test_a_STALE_expect_comments_nothing_either(self):
        rid, judged = self.judged_then_touched()
        before = self.lines()
        self.assertGreater(before, 0)
        got, why = tasks.comment(rid, "note", by="task-mirror",
                                 path=self.path, expect=judged)
        self.assertIs(got, tasks.SKIPPED)
        self.assertIn("changed since", why)
        self.assertEqual(self.lines(), before)
        current = tasks.rows(self.path)[rid]
        got, err = tasks.comment(rid, "note", by="task-mirror",
                                 path=self.path, expect=current)
        self.assertIsNone(err, err)
        self.assertEqual(len(got["comments"]), 2)
