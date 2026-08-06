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
import shutil
import sys
import tempfile
import unittest

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
            "MELD_ADOPTED_DIR", "HELM_ACTOR")


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
        migrated = self.file("the migrated item", "cj", tid="263")
        self.assertEqual(migrated["id"], "task/263")
        older = self.file("an older migrated item", "cj", tid="#45")
        self.assertEqual(older["id"], "task/45")     # any spelling, same row id
        fresh = self.file("filed today", "cj")
        self.assertEqual(fresh["id"], "task/264")    # highest + 1, not 46, not 3
        self.assertEqual(self.file("filed a second later", "cj")["id"],
                         "task/265")
        self.assertEqual([l["id"] for l in self.lines()],
                         ["task/263", "task/45", "task/264", "task/265"])

    def test_add_refuses_a_duplicate_id_and_an_empty_title(self):
        self.file("the first one", "cj", tid="263")
        dup, err = tasks.add("a second thing behind one citation", "cj",
                             tid="#263", path=self.path)
        self.assertIsNone(dup)
        self.assertIn("already exists", err)
        blank, err = tasks.add("   ", "cj", path=self.path)
        self.assertIsNone(blank)
        self.assertIn("title", err)
        junk, err = tasks.add("unparseable", "cj", tid=SHA, path=self.path)
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
        row = self.file("draft title", "cj")
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
        row = self.file("the thing", "cj")
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
        row = self.file("needs a second pair of eyes", "cj")
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
        mine_row = self.file("assigned to me", "cj")
        done = self.file("history", None)
        _closed, err = tasks.close(done["id"], "shipped", path=self.path)
        self.assertIsNone(err)

        # kimi is MEASURED ABSENT from the roster, so its row is stranded.
        offers = tasks.offer_rows(path=self.path, seat="cj", live=("cj",))
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
        thinned = tasks.offer_rows(path=self.path, seat="cj", live=("cj",),
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

        offers = tasks.offer_rows(path=self.path, seat="cj",
                                  live=("cj", "kimi", "codex"))
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

        empty = tasks.offer_rows(path=self.path, seat="cj", live=())
        self.assertEqual(sorted(o[5]["id"] for o in empty),
                         sorted([pool["id"], held["id"]]))

        unknown = tasks.offer_rows(path=self.path, seat="cj", live=None)
        self.assertEqual([o[5]["id"] for o in unknown], [pool["id"]])

        # The default is the unknown one: a caller that never learned to pass a
        # roster gets the pool, never somebody else's row.
        self.assertEqual([o[5]["id"] for o in
                          tasks.offer_rows(path=self.path, seat="cj")],
                         [pool["id"]])

    def test_identity_compares_casefolded_and_still_displays_verbatim(self):
        """codex-3's exact-tip probe, as a regression arm. Their measurement:
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
        mine_row = self.file("assigned to me", "cj")
        offers = tasks.offer_rows(path=self.path, seat="cj", live=("cj",))
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

        row, err = tasks.add("would vanish", "cj", path=blocked)
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

    # The seat these arms act as. Assertions below spell "cj" as a LITERAL
    # rather than reading it back off this attribute — partly so the expected
    # value is readable where it is asserted, and partly because the
    # vacuous-assertion rung only credits a positive control that pins an
    # observable to a non-empty LITERAL; `assertEqual(row["source"], SEAT)`
    # compares two unknowns as far as the analyzer can see, and left the
    # neighbouring assertIsNone uncovered.
    SEAT = "cj"

    def setUp(self):
        super().setUp()
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
        self.assertEqual(seats.own_name(), "cj")

        rc, out, err = self.cli("add", "a", "row", "nobody", "took")
        self.assertEqual(rc, 0, err)
        self.assertIn("filed", out)
        free = self.filed("a row nobody took")
        self.assertEqual(free["source"], "cj")        # provenance is KEPT…
        self.assertIsNone(free["owner"])              # …but filing is not owning
        self.assertEqual(free["status"], "open")

        rc, _o, err = self.cli("add", "one", "I", "am", "taking", "--mine")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.filed("one I am taking")["owner"], "cj")

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
        offers = tasks.offer_rows(tasks.ledger_path(), seat="cj", live=("cj",))
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
        row = self.file("migrated by script", "cj", tid=45)
        self.assertEqual(row["id"], "task/45")
        self.assertEqual(tasks.get(45, path=self.path)["title"],
                         "migrated by script")


class UpdateGuardTest(TasksBase):
    """FIX 3: update() was a second door into the same row that skipped the
    first door's rules — update(title="") landed a titleless row that add()
    refuses, and update(status="closed") landed the silent close that close()
    itself calls "a drop"."""

    def test_update_enforces_the_title_and_reason_rules_of_its_siblings(self):
        row = self.file("the original title", "cj")
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

        row, err = tasks.add("a ghost id", "cj", tid=ghost, path=self.path)
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
        self.assertEqual(taken["owner"], "cj")
        self.assertEqual(taken["status"], "in_progress")

        rc, _o, err = self.cli("add", "kimi's work", "--owner", "kimi")
        self.assertEqual(rc, 0, err)
        held = self.filed("kimi's work")
        rc, _o, err = self.cli("claim", held["id"])
        self.assertEqual(rc, 2)
        self.assertIn("kimi", err)            # NAMES the incumbent…
        self.assertIn("--force", err)         # …and says how to mean it
        # the row is UNTOUCHED, which the exit code alone would not show
        self.assertEqual(self.filed("kimi's work")["owner"], "kimi")
        self.assertEqual(self.filed("kimi's work")["status"], "open")

        # re-claiming what I ALREADY hold is not a steal and needs no --force
        rc, _o, err = self.cli("claim", taken["id"])
        self.assertEqual(rc, 0, err)

        rc, _o, err = self.cli("claim", held["id"], "--force")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.filed("kimi's work")["owner"], "cj")


if __name__ == "__main__":
    unittest.main()


class ClosedRowsStayClosedTest(TasksBase):
    """task/345 — codex-3's live repro, and it is a two-field contradiction
    rather than a wrong value: `helm task claim 172` on a CLOSED row returned
    status=in_progress owner=codex-3 WHILE RETAINING closed_reason. One row
    claiming to be live work and carrying the tombstone explaining why it is
    not. Every consumer picks one of those fields and is wrong whenever it
    picks the other."""

    def closed_row(self, title="already finished"):
        row, err = tasks.add(title, "cj", path=self.path)
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
        """LEDGER LINES, not projected rows — @codex-3's finding, and it is
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
        self.assertEqual(still[done["id"]]["owner"], "cj")
        self.assertEqual(still[done["id"]]["closed_reason"], "landed at abc1234")

    def test_force_does_NOT_buy_a_resurrection(self):
        """--force overrides the INCUMBENT-OWNER refusal. A closed row has no
        incumbent to take it from, so force is answering a question nobody
        asked — and if it worked here, the only way to say "I really mean it"
        would also silently mean "and reopen tombstones"."""
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
        fresh, err = tasks.add("genuinely open", "cj", path=self.path)
        self.assertIsNone(err, err)
        got, err = tasks.update(fresh["id"], path=self.path,
                                status="in_progress", owner="codex-3",
                                force=True)
        self.assertIsNone(err, err)
        self.assertEqual(got["status"], "in_progress")
        self.assertEqual(got["owner"], "codex-3")


class BornClosedNeedsAReasonTest(TasksBase):
    """@offbox-claude, found in a MELD after four async rounds missed it —
    and it is my own law one invariant over. update() refuses a close with no
    reason ("a silent close is a drop"); add() accepted a row BORN closed with
    closed_reason None, so the identical end state was reachable through the
    other door. The comment I wrote in update() says it exactly: "a closed set
    enforced at add() and open at update() is not a closed set."

    Invisible to the five task/345 arms BY CONSTRUCTION — no single-line
    deletion survives them, so the suite was not weak, it was aimed at the
    other door."""

    def test_a_row_BORN_closed_still_needs_a_reason(self):
        row, err = tasks.add("born a tombstone", "cj", status="closed",
                             path=self.path)
        self.assertIsNone(row)
        self.assertIn("still needs a reason", err)

    def test_a_tombstone_WITH_a_reason_files_normally(self):
        """The other polarity, and the control: the guard must not make
        tombstones unfileable — they are how a pre-ledger citation resolves
        to a sentence instead of nothing."""
        row, err = tasks.add("a real tombstone", "cj", status="closed",
                             closed_reason="retired before the ledger existed",
                             path=self.path)
        self.assertIsNone(err, err)
        self.assertEqual(row["status"], "closed")
        self.assertEqual(row["closed_reason"],
                         "retired before the ledger existed")

    def test_BOTH_DOORS_refuse_the_same_end_state(self):
        """The invariant stated as one fact rather than two: whichever door
        you come through, a closed row without a reason is refused."""
        born, err_add = tasks.add("via add", "cj", status="closed",
                                  path=self.path)
        live, err = tasks.add("via update", "cj", path=self.path)
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
        ok_add, err = tasks.add("via add, with a reason", "cj",
                                status="closed", closed_reason="retired",
                                path=self.path)
        self.assertIsNone(err, err)
        self.assertEqual(ok_add["status"], "closed")
        live2, err = tasks.add("via update, with a reason", "cj",
                               path=self.path)
        self.assertIsNone(err, err)
        ok_upd, err = tasks.update(live2["id"], path=self.path,
                                   status="closed", closed_reason="done")
        self.assertIsNone(err, err)
        self.assertEqual(ok_upd["status"], "closed")

    def test_STATUSES_is_pinned_EXACT_as_a_tripwire(self):
        """@offbox-claude's answer, and it beats both options I was weighing.
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
    """The repro exactly as codex-3 typed it. `claim` passes
    status=in_progress into update() and has no terminal check of its own —
    which is CORRECT: a guard on one door is a guard the next door walks
    around, and this codebase has the scar (`update --owner thief`)."""

    def test_claiming_a_CLOSED_row_is_refused_and_appends_NOTHING(self):
        rc, out, _err = self.cli("add", "already finished", "--owner", "cj")
        self.assertEqual(rc, 0, out)
        row = self.filed("already finished")
        rc, out, _err = self.cli("close", row["id"], "landed at abc1234")
        self.assertEqual(rc, 0, out)
        before, unavailable = tasks.snapshot()
        self.assertIsNone(unavailable, unavailable)
        self.assertEqual(before[row["id"]]["status"], "closed")   # MUST-HIT

        # RAW LEDGER LINES, not projected ids (@codex-3, and it is the SAME
        # vacuity the library arms had): this store is event-sourced, so an
        # appended duplicate snapshot changes neither the projected values nor
        # the id count. I fixed the library arms and left this one counting
        # ids, which is the identical hole one layer up.
        events = self.eventcount()
        self.assertGreater(events, 0)
        rc, out, err = self.cli("claim", row["id"], "--owner", "codex-3")
        self.assertEqual(rc, 2, out)
        self.assertIn("CLOSED", err)
        # THE CLI REMEDY BRANCH IS PINNED (@codex-3: deleting it left all five
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
    """@codex round 2: the read normalizer cured replay while THREE write and
    display surfaces still diverged from it. A closed set that only one door
    respects is the same defect this lane was opened for, one layer over."""

    LEGACY = "corpus-2026-08-05"

    def test_show_says_UNKNOWN_and_names_what_is_RECORDED(self):
        """`show` printed the raw field, so 251 rows displayed
        `corpus-2026-08-05` as a VALUE on a surface whose whole claim is a
        closed set. It cannot be written through the doors any more, so the
        fixture appends the event directly — which is exactly how those rows
        got there."""
        rc, out, _err = self.cli("add", "a migrated row", "--owner", "cj")
        self.assertEqual(rc, 0, out)
        row = dict(self.filed("a migrated row"))
        row["origin"] = self.LEGACY
        with open(tasks.ledger_path(), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
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
        rc, out, _err = self.cli("add", "he asked", "--owner", "cj",
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
        rc, out, _err = self.cli("add", "a row", "--owner", "cj")
        self.assertEqual(rc, 0, out)
        row = self.filed("a row")
        rc, out, err = self.cli("update", row["id"], "--origin", "owner")
        self.assertEqual(rc, 0, err)
        again = tasks.get(row["id"])
        self.assertEqual(again["origin"], "owner")

    def test_the_CLI_refuses_an_origin_outside_the_closed_set(self):
        rc, out, _err = self.cli("add", "a row", "--owner", "cj")
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
        rc, out, _err = self.cli("add", "he asked", "--owner", "cj",
                                 "--owner-asked")
        self.assertEqual(rc, 0, out)
        row = self.filed("he asked")
        self.assertEqual(row["origin"], "owner")     # positive control
        _r, err = tasks.update(row["id"], origin=None)
        self.assertIsNotNone(err)
        self.assertIn("cannot be cleared", err)
        self.assertEqual(tasks.get(row["id"])["origin"], "owner")

    def test_the_PUBLIC_help_names_the_flag_that_writes_it(self):
        """FINDABLE, in tonight's ladder terms: a flag the authoritative help
        omits is a flag nobody can discover. `--owner-asked` was reachable
        only by reading the source."""
        from helm import cli
        self.assertIn("--owner-asked", cli._VERB_HELP["task"])
        self.assertIn("--origin", cli._VERB_HELP["task"])


class OneSerializationDoorTest(CliBase):
    """ROUND 4 on the same defect, which is the signal to change the SHAPE
    rather than patch again. Rounds 1-3 cured the wire, the glyph and `show`
    by routing each reader through origin_of; round 4 found the JSON paths
    still emitting raw rows. Every round I fixed the readers I could find and
    a new one appeared. `public_row` is now the ONE serialization door, so a
    new consumer is correct by DEFAULT rather than correct by remembering."""

    LEGACY = "corpus-2026-08-05"

    def legacy_row(self, title):
        rc, out, _err = self.cli("add", title, "--owner", "cj")
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
        rc, out2, _e = self.cli("add", "he asked", "--owner", "cj",
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
        rc, out, _err = self.cli("add", "he asked", "--owner", "cj",
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
        rc, out, _err = self.cli("add", "a row", "--owner", "cj")
        self.assertEqual(rc, 0, out)
        row = self.filed("a row")
        # POSITIVE CONTROL FIRST, on the same field: update CAN write a note,
        # so "the note is unchanged" below is a refusal and not a no-op verb.
        _r, err = tasks.update(row["id"], note="a real change")
        self.assertIsNone(err, err)
        self.assertEqual(tasks.get(row["id"])["note"], "a real change")

        rc, _out, err = self.cli("update", row["id"], "--note", "changed",
                                 "--nosuchflag", "x")
        self.assertEqual(rc, 2)
        self.assertIn("--nosuchflag", err)
        self.assertEqual(tasks.get(row["id"])["note"], "a real change")

    def test_a_VALUELESS_origin_at_the_end_is_refused_too(self):
        rc, out, _err = self.cli("add", "a row", "--owner", "cj")
        self.assertEqual(rc, 0, out)
        row = self.filed("a row")
        rc, _out, err = self.cli("update", row["id"], "--note", "n", "--origin")
        self.assertEqual(rc, 2)
        self.assertIn("--origin", err)


class LegacyOriginIsNormalizedOnReadTest(TasksBase):
    """@codex round 1, and it refuted the premise the whole design rested on.

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
        row, err = tasks.add("a migrated row", "cj", path=self.path)
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
        row, err = tasks.add("a migrated row", "cj", path=self.path)
        self.assertIsNone(err, err)
        row["origin"] = self.LEGACY
        self.assertNotIn("@", tasks._fmt(row))
        # unconditional positive control on the same observable
        row["origin"] = "owner"
        self.assertIn("@", tasks._fmt(row))


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
        row, err = tasks.add("filed by an agent", "cj", path=self.path,
                             origin="agent")
        self.assertIsNone(err)                       # MUST-HIT: valid passes
        self.assertEqual(row["origin"], "agent")
        _r, err = tasks.add("bogus provenance", "cj", path=self.path,
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
        declared, err = tasks.add("declared row", "cj", path=self.path,
                                  origin="owner")
        self.assertIsNone(err)
        self.assertEqual(declared["origin"], "owner")

        row, err = tasks.add("legacy-shaped row", "cj", path=self.path)
        self.assertIsNone(err)
        self.assertIsNone(row["origin"])
        self.assertNotIn(row["origin"], tasks.ORIGINS)

    def test_the_glyph_marks_owner_asked_rows_only(self):
        """A provenance nothing renders is a provenance nobody sorts by —
        which is how 16 rows ended up smuggling it into their titles."""
        asked, _e = tasks.add("owner asked for this", "cj", path=self.path,
                              origin="owner")
        agent, _e = tasks.add("agent filed this", "cj", path=self.path,
                              origin="agent")
        legacy, _e = tasks.add("nobody said", "cj", path=self.path)
        self.assertIn("@", tasks._fmt(asked))
        self.assertNotIn("@", tasks._fmt(agent))
        self.assertNotIn("@", tasks._fmt(legacy))
