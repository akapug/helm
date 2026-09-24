"""`helm compose` — the fold that produces its own evidence, and its rung.

Every arm here asserts an OBSERVABLE: a word git returned, a file's contents,
a tree read back out of a real repository. None of them asserts that a call
did not raise.

THE INVARIANT THE WHOLE FILE EXISTS FOR, stated once: a fold-time refusal
cannot be built on patch identity, because `git patch-id` hashes context and a
car picked underneath other cars re-keys — so a MISS proves only that no commit
carries that EXACT diff. The manifest records what a REPLAY needs instead, and
what cannot be replayed says so rather than getting a weaker automatic answer.
"""
import ast
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import foldcompose                                 # noqa: E402

ENV_KEYS = ("HELM_HOME", "HELM_ADOPTED_DIR", "HELM_WORK_INTEGRATOR")


class ComposeBase(unittest.TestCase):
    """A real repo, a real room worktree, and an estate of this test's own.

    The room is a genuine `git worktree` at the path `_lanes.lane_path`
    computes, because the verb resolves it by that pure function and a fixture
    that faked the path would be testing a different resolver.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-foldcompose-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.prior = {k: os.environ.get(k) for k in ENV_KEYS}
        self.addCleanup(self._restore_env)
        for key in ENV_KEYS:
            os.environ.pop(key, None)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_ADOPTED_DIR"] = os.path.join(self.tmp, "adopted")
        os.makedirs(os.environ["HELM_ADOPTED_DIR"])
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "t@example.com")
        self.git("config", "user.name", "T")
        self.base = self.commit("seed.txt", "seed")
        # `project_for` reads the real registry, which this test has no
        # business writing into. The NAME is the only thing the module wants
        # from it, so the name is what the fixture supplies.
        patch = mock.patch.object(foldcompose, "project_for",
                                  lambda _root: "testproj")
        patch.start()
        self.addCleanup(patch.stop)

    def _restore_env(self):
        for key, value in self.prior.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def git(self, *args, where=None):
        done = subprocess.run(("git",) + args, cwd=where or self.repo,
                              capture_output=True, text=True)
        self.assertEqual(done.returncode, 0,
                         "git %s failed: %s" % (" ".join(args), done.stderr))
        return done.stdout.strip()

    def commit(self, name, body="x", where=None):
        path = os.path.join(where or self.repo, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(body)
        self.git("add", name, where=where)
        self.git("commit", "-q", "-m", name, where=where)
        return self.git("rev-parse", "HEAD", where=where)

    def lane(self, name, files):
        """A lane branch off the base carrying one commit per file."""
        self.git("checkout", "-q", "-b", "lane/" + name, self.base)
        for fname, body in files:
            self.commit(fname, body)
        self.git("checkout", "-q", "main")
        return "lane/" + name

    def room(self, name="room-1"):
        """A real worktree where `_lanes.lane_path` says the room lives."""
        path = self.repo.rstrip(os.sep) + "-wt" + os.sep + name
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.git("worktree", "add", "-q", "-b", "lane/" + name, path,
                 self.base)
        return name, path

    def manifest_file(self, tip):
        return foldcompose.manifest_path("testproj", tip)


class ManifestRecordTest(ComposeBase):
    def _car(self, **over):
        car = {"row": "abc123", "lane": "l", "source_commit": "a" * 40,
               "result_commit": "c" * 40}
        car.update(over)
        return car

    def test_a_whole_car_is_usable_and_each_hole_is_not(self):  # noqa: VACUOUS_ASSERTION — the first line is an unconditional positive on the same observable: a WHOLE car is usable, before any hole is tried
        self.assertIsNotNone(foldcompose._usable_car(self._car()))
        holes = [
            ("a missing row", {"row": ""}),
            ("a missing source commit", {"source_commit": ""}),
            ("a missing result commit", {"result_commit": ""}),
            ("an abbreviated source id", {"source_commit": "abcdef12"}),
            ("an abbreviated result id", {"result_commit": "abcdef12"}),
            ("an uppercase id", {"result_commit": "D" * 40}),
            ("a numeric row", {"row": 7}),
        ]
        for why, over in holes:
            with self.subTest(why=why):
                self.assertIsNone(foldcompose._usable_car(self._car(**over)),
                                  "%s was accepted as a whole car" % why)
        self.assertIsNone(foldcompose._usable_car("not a dict"))

    def test_a_record_for_ANOTHER_tip_is_not_this_tips_evidence(self):  # noqa: VACUOUS_ASSERTION — the same file re-keyed truthfully reads back, unconditionally, on the same observable
        tip, other = "e" * 40, "f" * 40
        rec = {"result_tip": other, "cars": [self._car()]}
        path = self.manifest_file(tip)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(rec, fh)
        self.assertIsNone(foldcompose.read_manifest("testproj", tip),
                          "a record naming a different tip was read as this "
                          "tip's evidence")
        # THE CONTROL on the same file: the same record, keyed truthfully,
        # reads back — so the refusal above is about the key and not about a
        # reader that never works.
        rec["result_tip"] = tip
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(rec, fh)
        got = foldcompose.read_manifest("testproj", tip)
        self.assertEqual((got or {}).get("result_tip"), tip)

    def test_an_unreadable_record_is_NO_EVIDENCE_not_partial_evidence(self):
        tip = "e" * 40
        path = self.manifest_file(tip)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        for why, body in (("not json", "{{{"),
                          ("a list", "[]"),
                          ("no cars", json.dumps({"result_tip": tip})),
                          ("empty cars", json.dumps({"result_tip": tip,
                                                     "cars": []}))):
            with self.subTest(why=why):
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(body)
                self.assertIsNone(foldcompose.read_manifest("testproj", tip))
        # ONE HOLED CAR DROPS THE WHOLE RECORD. A verifier that skipped it
        # while calling the manifest complete would license exactly the
        # unreviewed land this module exists to refuse.
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"result_tip": tip,
                       "cars": [self._car(),
                                self._car(result_commit="")]}, fh)
        self.assertIsNone(foldcompose.read_manifest("testproj", tip))
        # THE POSITIVE CONTROL, same path and same reader: drop the holed car
        # and the record reads back whole. Without it every assertion above is
        # about a reader that might refuse everything.
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"result_tip": tip, "cars": [self._car()]}, fh)
        self.assertEqual(foldcompose.read_manifest("testproj", tip)["cars"],
                         [self._car()])

    def test_an_ABBREVIATED_tip_names_no_manifest_path(self):
        self.assertIsNone(foldcompose.manifest_path("testproj", "abcdef12"))
        self.assertIsNone(foldcompose.manifest_path("testproj", ""))
        self.assertTrue(foldcompose.manifest_path("testproj", "a" * 40))

    def test_the_writer_refuses_what_the_reader_could_not_read(self):
        tip = "e" * 40
        self.assertIsNone(foldcompose.write_manifest(
            "testproj", {"result_tip": tip, "cars": [self._car(row="")]}))
        self.assertFalse(os.path.exists(self.manifest_file(tip)),
                         "a record the reader would drop was still stored")
        path = foldcompose.write_manifest(
            "testproj", {"result_tip": tip, "cars": [self._car()]})
        self.assertTrue(path and os.path.exists(path))
        self.assertEqual(
            (foldcompose.read_manifest("testproj", tip) or {}).get("cars"),
            [self._car()])


class ComposeLoopTest(ComposeBase):
    def commit_dates(self, commit):
        """Read stored seconds and UTC offsets, not a pretty date rendering."""
        headers = self.git("cat-file", "-p", commit).split("\n\n", 1)[0]
        dates = {}
        for line in headers.splitlines():
            role = line.split(" ", 1)[0]
            if role in ("author", "committer"):
                _identity, seconds, offset = line.rsplit(" ", 2)
                dates[role] = (int(seconds), offset)
        self.assertEqual(set(dates), {"author", "committer"})
        return dates

    @mock.patch.dict(os.environ, {"GIT_AUTHOR_DATE": "2001-02-03T04:05:06+00:00",
                                  "GIT_COMMITTER_DATE": "2001-02-03T04:05:06+00:00"})
    def test_it_composes_two_cars_and_records_what_a_REPLAY_needs(self):  # noqa: VACUOUS_ASSERTION — every id here is compared to a value read back out of git; the one assertNotEqual is bracketed by an assertEqual on the sibling car
        lane_a = self.lane("car-a", [("a.txt", "a")])
        lane_b = self.lane("car-b", [("b.txt", "b")])
        room, path = self.room()
        record, err = foldcompose.compose(
            self.repo, room, self.base,
            [{"row": "row-a", "lane": "car-a"},
             {"row": "row-b", "lane": "car-b"}])
        self.assertIsNone(err)
        self.assertEqual([c["row"] for c in record["cars"]],
                         ["row-a", "row-b"])
        # EVERY RECORDED FIELD IS READ BACK OUT OF GIT, never taken from the
        # record: a compose that silently produced something else must not be
        # able to describe itself as what was intended.
        head = self.git("rev-parse", "HEAD", where=path)
        self.assertEqual(record["result_tip"], head)
        self.assertEqual(record["cars"][-1]["result_commit"], head)
        self.assertEqual(record["cars"][0]["result_commit"],
                         self.git("rev-parse", "HEAD~1", where=path))
        # THE SOURCE SIDE IS AN ADDRESS AND NOTHING ELSE. What that commit
        # holds is git's to say at read time, not the record's to assert.
        pinned = (int(datetime.fromisoformat(os.environ["GIT_AUTHOR_DATE"]).timestamp()),
                  "+0000")
        for car, lane_ref in ((record["cars"][0], lane_a),
                              (record["cars"][1], lane_b)):
            self.assertEqual(car["source_commit"],
                             self.git("rev-parse", lane_ref))
            self.assertNotIn("result_tree", car)
            self.assertNotIn("status", car)
            for commit in (car["source_commit"], car["result_commit"]):
                self.assertEqual(self.commit_dates(commit),
                                 {"author": pinned, "committer": pinned})
        # WITH METADATA PINNED, the second car is the one that must change.
        # Git preserves the author but stamps a cherry-pick's committer date
        # anew. The decorator makes equal metadata an actual fixture fact,
        # rather than hoping source creation and composition share a second.
        # Car A then has the same tree, parent, message AND metadata; car B
        # has a different parent and must have a different object id.
        self.assertEqual(record["cars"][0]["source_commit"],
                         record["cars"][0]["result_commit"],
                         "a pick onto the source's own parent should "
                         "reproduce the source object")
        self.assertNotEqual(record["cars"][1]["source_commit"],
                            record["cars"][1]["result_commit"],
                            "a car picked onto a DIFFERENT parent kept its "
                            "object id, so nothing was actually re-created "
                            "and the recorded result is the lane's own commit")

    def test_same_parent_pick_changes_identity_when_only_committer_time_moves(self):
        """A deterministic discriminator for timestamp drift, not a sleep."""
        first = datetime.fromisoformat("2001-02-03T04:05:06+00:00")
        later = first + timedelta(seconds=1)
        with mock.patch.dict(os.environ, {"GIT_AUTHOR_DATE": first.isoformat(),
                                          "GIT_COMMITTER_DATE": first.isoformat()}):
            lane = self.lane("dated", [("dated.txt", "content")])
            room, _path = self.room("dated-room")
            with mock.patch.dict(os.environ, {"GIT_COMMITTER_DATE": later.isoformat()}):
                record, err = foldcompose.compose(
                    self.repo, room, self.base, [{"row": "dated-row", "lane": "dated"}])
        self.assertIsNone(err, err)
        car = record["cars"][0]
        source, result = car["source_commit"], car["result_commit"]
        self.assertEqual(source, self.git("rev-parse", lane))
        self.assertEqual(self.git("rev-parse", source + "^"), self.base)
        self.assertEqual(self.git("rev-parse", result + "^"), self.base)
        self.assertEqual(self.git("rev-parse", source + "^{tree}"),
                         self.git("rev-parse", result + "^{tree}"))
        for fmt in ("%an <%ae>", "%cn <%ce>", "%B"):
            self.assertEqual(self.git("show", "-s", "--format=" + fmt, source),
                             self.git("show", "-s", "--format=" + fmt, result))
        before, after = self.commit_dates(source), self.commit_dates(result)
        pinned = (int(first.timestamp()), "+0000")
        self.assertEqual(before, {"author": pinned, "committer": pinned})
        self.assertEqual(after, {"author": pinned,
                                 "committer": (int(later.timestamp()), "+0000")})
        self.assertEqual(after["committer"][0] - before["committer"][0], 1)
        self.assertNotEqual(source, result)
        ok, why = foldcompose.replay_car(self.repo, car)
        self.assertIs(ok, True, why)

    def test_the_controls_pass_on_a_TWO_CAR_compose(self):  # noqa: VACUOUS_ASSERTION — every rung is asserted True by name and the written manifest is read back by tip
        """THE CASE THE FIRST CUT COULD NOT PASS, and it is why this arm
        composes TWO cars rather than one.

        The old control compared the FINAL room tree against
        `merge-tree(base, lane)` once per lane. That is only meaningful for a
        one-car room: with A and B the room holds base+A+B and neither base+A
        nor base+B can equal it, so BOTH rungs refused every real two-car
        compose. Measured on a live two-car probe. A one-car arm cannot find
        it and neither can a two-car arm that never runs the controls, so
        this one does both.
        """
        self.lane("car-a", [("a.txt", "a")])
        self.lane("car-b", [("b.txt", "b")])
        room, path = self.room()
        cars = [{"row": "row-a", "lane": "car-a"},
                {"row": "row-b", "lane": "car-b"}]
        record, err = foldcompose.compose(self.repo, room, self.base, cars)
        self.assertIsNone(err)
        self.assertEqual(len(record["cars"]), 2)
        rows, tip = foldcompose.controls(self.repo, room, self.base, cars,
                                         record=record)
        for name, ok, detail in rows:
            self.assertIs(ok, True, "%s did not pass: %s" % (name, detail))
        # THE REPLAY REALLY RAN, by name, so a control list that silently
        # dropped it could not pass this arm.
        names = [name for name, _ok, _d in rows]
        self.assertIn("replay-ends-at-tip", names)
        self.assertEqual(len([n for n in names if n.startswith("replay:")]), 2)
        self.assertEqual(tip, record["result_tip"])
        written = foldcompose.write_manifest("testproj", record)
        self.assertTrue(written and os.path.exists(written))
        self.assertEqual(
            foldcompose.read_manifest("testproj", tip)["result_tip"], tip)

    def test_controls_WITHOUT_a_record_do_not_report_a_passed_replay(self):  # noqa: VACUOUS_ASSERTION — the same call WITH a record returns the replay rungs as True in the arm above
        """A shorter list is not a greener one."""
        self.lane("car-a", [("a.txt", "a")])
        room, path = self.room()
        cars = [{"row": "row-a", "lane": "car-a"}]
        foldcompose.compose(self.repo, room, self.base, cars)
        rows, _tip = foldcompose.controls(self.repo, room, self.base, cars)
        replay = [(n, ok, d) for n, ok, d in rows if n == "replay-chain"]
        self.assertEqual(len(replay), 1)
        self.assertIsNone(replay[0][1])
        self.assertIn("not a passed control", replay[0][2])

    def test_a_BROKEN_CHAIN_is_refused_even_when_each_car_replays(self):  # noqa: VACUOUS_ASSERTION — the untouched record's chain passes in the two-car arm above
        """Each pick can be individually honest and the SEQUENCE still a lie."""
        self.lane("car-a", [("a.txt", "a")])
        self.lane("car-b", [("b.txt", "b")])
        room, path = self.room()
        cars = [{"row": "row-a", "lane": "car-a"},
                {"row": "row-b", "lane": "car-b"}]
        record, err = foldcompose.compose(self.repo, room, self.base, cars)
        self.assertIsNone(err)
        # Drop the FIRST car: car B's recorded parent is now no longer the
        # previous result, so the sequence does not hold even though B's own
        # replay would.
        rows = foldcompose.replay_chain(path, record["cars"][1:], self.base,
                                        record["result_tip"])
        self.assertIs(rows[0][1], False)
        self.assertIn("do not form one sequence", rows[0][2])

    def test_a_WRONG_ADDRESS_fails_the_replay_and_the_right_one_does_not(self):
        """THE ADDRESS IS ALL A TAMPERER CONTROLS once the record holds no
        trees, so this points a car at a real commit that is not what its
        pick produced. The replay reads that commit's tree from GIT and
        disagrees."""
        self.lane("car-a", [("a.txt", "a")])
        self.lane("car-b", [("b.txt", "b")])
        room, path = self.room()
        cars = [{"row": "row-a", "lane": "car-a"},
                {"row": "row-b", "lane": "car-b"}]
        record, err = foldcompose.compose(self.repo, room, self.base, cars)
        self.assertIsNone(err)
        for car in record["cars"]:
            ok, detail = foldcompose.replay_car(path, car)
            self.assertIs(ok, True, detail)
        poisoned = dict(record["cars"][0],
                        result_commit=record["cars"][1]["result_commit"])
        ok, detail = foldcompose.replay_car(path, poisoned)
        self.assertIs(ok, False, detail)
        self.assertIn("replay produced", detail)

    def test_a_CONFLICT_records_exact_review_required_and_writes_nothing(self):
        # Two lanes that touch the SAME path differently: the second pick
        # cannot apply, which is precisely the car with no deterministic
        # replay.
        self.lane("car-a", [("clash.txt", "from a")])
        self.lane("car-b", [("clash.txt", "from b")])
        room, path = self.room()
        record, err = foldcompose.compose(
            self.repo, room, self.base,
            [{"row": "row-a", "lane": "car-a"},
             {"row": "row-b", "lane": "car-b"}])
        self.assertIsNone(err)
        self.assertTrue(record["conflict_at"])
        self.assertEqual(len(record["cars"]), 1,
                         "a car with no result commit was recorded anyway")
        # NOTHING IS STORED FOR A COMPOSE THAT DID NOT FINISH: the record has
        # no result tip, so the writer has no key and refuses.
        self.assertIsNone(foldcompose.write_manifest("testproj", record))

    def test_a_MESSAGE_ONLY_car_is_carried_rather_than_stopping_the_train(self):
        """MEASURED, and it stopped the whole compose before this cure.

        `git cherry` reports a message-only commit as PLUS, so it reaches the
        pick loop, and a plain cherry-pick fails it with "The previous
        cherry-pick is now empty" — which was recorded as
        exact-review-required and ended the compose. A message-only commit is
        an ordinary thing to have on a lane, and the same shape already cost
        this project a frozen patch-id backfill.

        CARRYING IT STAYS VISIBLE rather than hiding it: the car keeps its
        row and its place, its result tree EQUALS its parent's, and the
        replay verifies that.
        """
        self.git("checkout", "-q", "-b", "lane/car-a", self.base)
        self.git("commit", "-q", "--allow-empty", "-m", "message only")
        empty = self.git("rev-parse", "HEAD")
        self.commit("a.txt", "a")
        self.git("checkout", "-q", "main")
        room, path = self.room()
        cars = [{"row": "row-a", "lane": "car-a"}]
        record, err = foldcompose.compose(self.repo, room, self.base, cars)
        self.assertIsNone(err)
        self.assertFalse(record.get("conflict_at"),
                         "a message-only commit stopped the compose")
        self.assertEqual([c["source_commit"] for c in record["cars"]][0],
                         empty, "the empty car was dropped from the sequence")
        self.assertEqual(len(record["cars"]), 2)
        # ITS TREE DID NOT MOVE, which is the whole content of an empty car —
        # read from GIT at the recorded ADDRESS, never from the record.
        self.assertEqual(
            self.git("rev-parse",
                     record["cars"][0]["result_commit"] + "^{tree}"),
            self.git("rev-parse", self.base + "^{tree}"))
        ok, detail = foldcompose.replay_car(path, record["cars"][0])
        self.assertIs(ok, True, detail)
        rows = foldcompose.replay_chain(path, record["cars"], self.base,
                                        record["result_tip"])
        for name, ok, detail in rows:
            self.assertIs(ok, True, "%s: %s" % (name, detail))

    def test_a_nonempty_car_made_empty_by_an_earlier_car_is_kept(self):
        """`--allow-empty` alone covers only commits born empty. This car has a
        real diff on its lane, but the first car already introduced the same
        blob, so the second pick becomes empty only inside this composition.
        It must retain its row, message and sequence address rather than stop or
        disappear."""
        self.lane("car-a", [("same.txt", "same")])
        self.lane("car-b", [("same.txt", "same")])
        room, path = self.room()
        record, err = foldcompose.compose(
            self.repo, room, self.base,
            [{"row": "row-a", "lane": "car-a"},
             {"row": "row-b", "lane": "car-b"}])
        self.assertIsNone(err, err)
        self.assertFalse(record.get("conflict_at"))
        self.assertEqual([car["row"] for car in record["cars"]],
                         ["row-a", "row-b"])
        second = record["cars"][1]["result_commit"]
        self.assertEqual(self.git("show", "-s", "--format=%s", second,
                                  where=path), "same.txt")
        self.assertEqual(self.git("rev-parse", second + "^{tree}", where=path),
                         self.git("rev-parse", second + "^^{tree}", where=path))
        rows = foldcompose.replay_chain(
            path, record["cars"], self.base, record["result_tip"])
        for name, ok, detail in rows:
            self.assertIs(ok, True, "%s: %s" % (name, detail))

    def test_a_car_with_NO_ROW_is_refused_before_anything_is_touched(self):
        cars, err = foldcompose._cars_from_args(["justalane"])
        self.assertIsNone(cars)
        self.assertIn("no reviewer", err)
        cars, err = foldcompose._cars_from_args(["row-a:car-a"])
        self.assertIsNone(err)
        self.assertEqual(cars, [{"row": "row-a", "lane": "car-a"}])

    def test_cherry_reports_only_the_commits_NOT_already_carried(self):
        lane_ref = self.lane("car-a", [("a.txt", "a"), ("a2.txt", "a2")])
        room, path = self.room()
        picks, err = foldcompose.cars_to_pick(path, self.base, lane_ref)
        self.assertIsNone(err)
        self.assertEqual(picks, [self.git("rev-parse", lane_ref + "^"),
                                 self.git("rev-parse", lane_ref)],
                         "cherry did not report both cars oldest-first")
        # AFTER THE PICK THE SAME QUESTION ANSWERS EMPTY, which is the
        # observable that the pick actually carried them — a plus that stayed
        # plus would mean the compose did nothing.
        foldcompose.compose(self.repo, room, self.base,
                            [{"row": "r", "lane": "car-a"}])
        picks, err = foldcompose.cars_to_pick(path, "HEAD", lane_ref)
        self.assertIsNone(err)
        self.assertEqual(picks, [])

    def test_a_deletion_NO_CAR_asked_for_is_refused_and_a_recreate_is_not(self):
        self.commit("doomed.txt", "here")
        base = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", "-b", "lane/car-a", base)
        self.git("rm", "-q", "doomed.txt")
        self.git("commit", "-q", "-m", "drop it")
        self.git("checkout", "-q", "main")
        room, path = self.room()
        cars = [{"row": "r", "lane": "car-a"}]
        record, err = foldcompose.compose(self.repo, room, base, cars)
        self.assertIsNone(err)
        sources = [c["source_commit"] for c in record["cars"]]
        ok, detail = foldcompose.deletions_are_accounted_for(path, base,
                                                             sources)
        self.assertIs(ok, True, detail)
        # THE REFUSAL: the room loses a path no car asked to lose. Deleting it
        # in the room directly is the state a hand-edit produces.
        self.git("rm", "-q", "seed.txt", where=path)
        self.git("commit", "-q", "-m", "hand edit", where=path)
        ok, detail = foldcompose.deletions_are_accounted_for(path, base,
                                                             sources)
        self.assertIs(ok, False)
        self.assertIn("seed.txt", detail)


class CompositionProofTest(ComposeBase):
    """The SEPARATE stage, and every arm is about something it RE-DERIVES.

    It is not one of `foldcheck.check`'s rungs. That five-rung shape is a
    public contract eleven arms assert on, and quietly making it six turned
    every one of them red in a single gate.
    """

    def _activate(self, trunk):
        path, err = foldcompose.activate("testproj", self.repo, trunk)
        self.assertIsNone(err, err)
        self.assertEqual(path, foldcompose.activation_path("testproj"))
        return path

    def _approvals(self, rows, gate_verdict="PASS"):
        from helm import dispatches, foldcheck, landreq
        projected = {}
        verdicts = {}
        for index, (row, reviewed_tip, polarity) in enumerate(rows, 1):
            projected[row] = {
                "id": row, "status": "verdict", "kind": "review",
                "reviewed_tip": reviewed_tip, "polarity": polarity,
                "gate_caps": ["receipt-v1"], "gate": "b" * 16,
            }
            verdicts[row] = (index, ("%032x" % index)[-32:])
        snapshot = mock.patch.multiple(
            dispatches,
            snapshot_with_verdicts=lambda: (projected, verdicts, None),
            gate_epoch=lambda _rows, _verdicts: 0,
            approval_tier_for_verdict=lambda _row: ("ok", None))
        receipt = mock.patch.object(
            landreq.foldcheck, "gate_authority",
            lambda _repo, _tip, _gate: foldcheck.Rung(
                "tree-vs-gate", gate_verdict, "fixture receipt authority"))
        return snapshot, receipt

    def _approve(self, row, reviewed_tip, polarity="approve",
                 gate_verdict="PASS"):
        return self._approvals([(row, reviewed_tip, polarity)], gate_verdict)

    def _proof(self, tip):
        return foldcompose.composition_proof(self.repo, "testproj", tip)

    def _compose_one(self, row="row-a", activate=True):
        lane_ref = self.lane("car-a", [("a.txt", "a")])
        room, path = self.room()
        cars = [{"row": row, "lane": "car-a"}]
        record, err = foldcompose.compose(self.repo, room, self.base, cars)
        self.assertIsNone(err)
        self.assertTrue(foldcompose.write_manifest("testproj", record))
        if activate:
            self._activate(self.base)      # the tip is NOT reachable: in scope
        return record, lane_ref

    def test_NEVER_ACTIVATED_is_the_ABSENCE_of_a_stage(self):
        record, _lane_ref = self._compose_one(activate=False)
        self.assertIsNone(self._proof(record["result_tip"]))

    def test_HISTORICAL_at_activation_is_the_ABSENCE_of_a_stage(self):
        record, _lane_ref = self._compose_one(activate=False)
        tip = record["result_tip"]
        self._activate(tip)
        self.assertIsNone(self._proof(tip), "a reachable tip ran a stage")

    def test_ACTIVE_newer_tip_runs_the_separate_stage(self):
        record, lane_ref = self._compose_one()
        tip = record["result_tip"]
        a, b = self._approve("row-a", self.git("rev-parse", lane_ref))
        with a, b:
            rows = self._proof(tip)
        self.assertTrue(rows)
        for name, ok, detail in rows:
            self.assertIs(ok, True, "%s: %s" % (name, detail))

    def test_a_car_pointed_at_ANOTHER_commit_is_refused_by_the_replay(self):  # noqa: VACUOUS_ASSERTION — the untouched record passes unconditionally in the scope arm above
        record, lane_ref = self._compose_one()
        tip = record["result_tip"]
        # THE ADDRESS IS ALL A TAMPERER CONTROLS NOW: point the car at a real
        # commit that is not what that pick produced.
        record["cars"][0]["result_commit"] = self.base
        self.assertTrue(foldcompose.write_manifest("testproj", record))
        a, b = self._approve("row-a", self.git("rev-parse", lane_ref))
        with a, b:
            rows = self._proof(tip)
        self.assertTrue(any(ok is False for _n, ok, _d in rows), rows)

    def test_a_chain_that_does_not_END_at_the_queried_tip_is_refused(self):  # noqa: VACUOUS_ASSERTION — the whole chain passes unconditionally on the last lines
        """A manifest correctly keyed to this tip can still describe a
        DIFFERENT compose, and the ends-at-tip clause is the only thing that
        notices."""
        lane_a = self.lane("car-a", [("a.txt", "a")])
        lane_b = self.lane("car-b", [("b.txt", "b")])
        room, path = self.room()
        cars = [{"row": "row-a", "lane": "car-a"},
                {"row": "row-b", "lane": "car-b"}]
        record, err = foldcompose.compose(self.repo, room, self.base, cars)
        self.assertIsNone(err)
        tip = record["result_tip"]
        self._activate(self.base)
        whole = list(record["cars"])
        record["cars"] = whole[:1]              # drops the car that reaches tip
        self.assertTrue(foldcompose.write_manifest("testproj", record))
        a, b = self._approve("row-a", self.git("rev-parse", lane_a))
        with a, b:
            rows = self._proof(tip)
        bad = [d for _n, ok, d in rows if ok is False]
        self.assertTrue(bad, rows)
        self.assertIn("ends at", bad[0])
        # THE CONTROL: the WHOLE chain, same tip, same reader, passes.
        record["cars"] = whole
        self.assertTrue(foldcompose.write_manifest("testproj", record))
        a, b = self._approvals([
            ("row-a", self.git("rev-parse", lane_a), "approve"),
            ("row-b", self.git("rev-parse", lane_b), "approve"),
        ])
        with a, b:
            rows = self._proof(tip)
        for name, ok, detail in rows:
            self.assertIs(ok, True, "%s: %s" % (name, detail))

    def test_only_APPROVE_authorises_a_car(self):
        record, lane_ref = self._compose_one()
        tip, reviewed = record["result_tip"], self.git("rev-parse", lane_ref)
        for polarity in ("fix", "supersede", "concur", None):
            with self.subTest(polarity=polarity):
                a, b = self._approve("row-a", reviewed, polarity=polarity)
                with a, b:
                    rows = self._proof(tip)
                bad = [d for _n, ok, d in rows if ok is False]
                self.assertTrue(bad, rows)
                self.assertIn("authorises nothing", bad[0])
        a, b = self._approve("row-a", reviewed)
        with a, b:
            rows = self._proof(tip)
        for name, ok, detail in rows:
            self.assertIs(ok, True, "%s: %s" % (name, detail))

    def test_a_row_whose_REVIEWER_NEVER_SAW_the_car_is_refused(self):  # noqa: VACUOUS_ASSERTION — the containing reviewed tip passes unconditionally on the last lines
        record, lane_ref = self._compose_one()
        tip = record["result_tip"]
        a, b = self._approve("row-a", self.base)
        with a, b:
            rows = self._proof(tip)
        bad = [d for n, ok, d in rows if ok is False]
        self.assertTrue(bad, rows)
        self.assertIn("does NOT contain", bad[0])
        a, b = self._approve("row-a", self.git("rev-parse", lane_ref))
        with a, b:
            rows = self._proof(tip)
        for name, ok, detail in rows:
            self.assertIs(ok, True, "%s: %s" % (name, detail))

    def test_a_row_ABSENT_from_the_ledger_is_refused(self):  # noqa: VACUOUS_ASSERTION — the present-row control passes unconditionally on the last lines
        from helm import dispatches
        record, lane_ref = self._compose_one()
        tip = record["result_tip"]
        with mock.patch.object(
                dispatches, "snapshot_with_verdicts",
                lambda: ({}, {}, None)):
            rows = self._proof(tip)
        bad = [d for n, ok, d in rows if ok is False]
        self.assertTrue(bad, rows)
        self.assertIn("not in the dispatch ledger", bad[0])
        a, b = self._approve("row-a", self.git("rev-parse", lane_ref))
        with a, b:
            rows = self._proof(tip)
        for name, ok, detail in rows:
            self.assertIs(ok, True, "%s: %s" % (name, detail))

    def test_an_IN_SCOPE_tip_with_NO_manifest_is_refused(self):  # noqa: VACUOUS_ASSERTION — the same tip WITH its manifest passes unconditionally on the last lines
        record, lane_ref = self._compose_one()
        tip = record["result_tip"]
        os.remove(foldcompose.manifest_path("testproj", tip))
        rows = self._proof(tip)
        self.assertTrue(any(ok is False for _n, ok, _d in rows), rows)
        self.assertIn("green gate is not a review",
                      " ".join(d for _n, _ok, d in rows))
        self.assertTrue(foldcompose.write_manifest("testproj", record))
        a, b = self._approve("row-a", self.git("rev-parse", lane_ref))
        with a, b:
            rows = self._proof(tip)
        for name, ok, detail in rows:
            self.assertIs(ok, True, "%s: %s" % (name, detail))

    def test_unreadable_ledger_and_gate_authority_are_UNKNOWN(self):
        from helm import dispatches
        record, lane_ref = self._compose_one()
        tip, reviewed = record["result_tip"], self.git("rev-parse", lane_ref)
        with mock.patch.object(
                dispatches, "snapshot_with_verdicts",
                lambda: ({}, {}, "fixture ledger outage")):
            rows = self._proof(tip)
        self.assertTrue(any(ok is None for _n, ok, _d in rows), rows)
        self.assertIn("ledger", " ".join(d for _n, _ok, d in rows))
        a, b = self._approve("row-a", reviewed, gate_verdict="UNKNOWN")
        with a, b:
            rows = self._proof(tip)
        self.assertTrue(any(ok is None for _n, ok, _d in rows), rows)
        self.assertIn("receipt authority", " ".join(
            d for _n, ok, d in rows if ok is None))

    def test_git_ancestry_error_is_UNKNOWN_not_a_measured_no(self):
        record, lane_ref = self._compose_one()
        reviewed = self.git("rev-parse", lane_ref)
        a, b = self._approve("row-a", reviewed)
        with a, b, \
                mock.patch.object(foldcompose, "_resolve", lambda _w, rev: rev), \
                mock.patch.object(
                    foldcompose, "_git",
                    lambda *_a, **_kw: (128, "", "fixture ancestry error")):
            ok, detail = foldcompose._reviewed_covers(
                self.repo, "row-a", record["cars"][0]["source_commit"])
        self.assertIsNone(ok)
        self.assertIn("could not decide", detail)

    def test_a_dirty_tree_snapshot_reviewed_tip_is_a_measured_NO(self):
        """The second door, and it exists because the first one is too late.

        `dispatches.send` refuses a snapshot at the moment a row is WRITTEN,
        which does nothing for rows already on the ledger — and both instances
        that reached trunk were already written when the class was found. This
        one fires at fold time on whatever the manifest names.

        It is a hard False rather than the UNKNOWN its neighbours return: an
        unreadable tip is a shrug, but a tip we CAN read whose subject says it
        is fab's snapshot of a dirty tree is a decided no.
        """
        record, lane_ref = self._compose_one()
        reviewed = self.git("rev-parse", lane_ref)
        source = record["cars"][0]["source_commit"]

        # POSITIVE CONTROL FIRST, through the same call: the ordinary reviewed
        # tip passes, so the refusal below is about the snapshot and not about
        # the fixture's approval plumbing.
        a, b = self._approve("row-a", reviewed)
        with a, b:
            ok, detail = foldcompose._reviewed_covers(self.repo, "row-a", source)
        self.assertIs(ok, True, detail)

        # A REAL dirty-tree snapshot over the same content, minted the way fab
        # mints one, so the object under test is the one production produces.
        index = os.path.join(self.tmp, "composnapindex")
        env = dict(os.environ, GIT_INDEX_FILE=index)
        subprocess.run(["git", "-C", self.repo, "add", "-A"], check=True, env=env)
        tree = subprocess.run(["git", "-C", self.repo, "write-tree"],
                              check=True, capture_output=True, text=True,
                              env=env).stdout.strip()
        msg = os.path.join(self.tmp, "composnapmsg")
        with open(msg, "w") as fh:
            fh.write("fab snapshot (tracked+untracked) of %s+dirty\n"
                     % reviewed[:9])
        snapshot = subprocess.run(
            ["git", "-C", self.repo, "commit-tree", tree, "-p", reviewed,
             "-F", msg], check=True, capture_output=True,
            text=True).stdout.strip()
        self.assertEqual(len(snapshot), 40)

        a, b = self._approve("row-a", snapshot)
        with a, b:
            ok, detail = foldcompose._reviewed_covers(self.repo, "row-a", source)
        self.assertIs(ok, False, detail)
        self.assertIn("COMMIT BEFORE GATING", detail)


class ActivationTest(ComposeBase):
    def test_cli_activation_uses_the_integrator_door_and_real_writer(self):
        rc = foldcompose.cmd_compose(
            ["--activate", self.base, "--repo", self.repo])
        self.assertEqual(rc, 2)
        self.assertEqual(foldcompose.activation_state("testproj"),
                         (foldcompose.ACTIVATION_INACTIVE, None))
        os.environ["HELM_WORK_INTEGRATOR"] = "1"
        rc = foldcompose.cmd_compose(
            ["--activate", self.base, "--repo", self.repo])
        self.assertEqual(rc, 0)
        self.assertEqual(foldcompose.activation_state("testproj"),
                         (foldcompose.ACTIVATION_ACTIVE, self.base))

    def test_writer_is_atomic_idempotent_and_write_once(self):
        path, err = foldcompose.activate("testproj", self.repo, self.base)
        self.assertIsNone(err, err)
        self.assertEqual(path, foldcompose.activation_path("testproj"))
        with open(path, encoding="utf-8") as fh:
            record = json.load(fh)
        with open(foldcompose.activation_latch_path("testproj"),
                  encoding="utf-8") as fh:
            latch = json.load(fh)
        self.assertEqual(record, {"v": 1, "trunk": self.base})
        self.assertEqual(latch, record)
        again, err = foldcompose.activate("testproj", self.repo, self.base)
        self.assertIsNone(err, err)
        self.assertEqual(again, path)
        later = self.commit("later.txt", "later")
        refused, err = foldcompose.activate("testproj", self.repo, later)
        self.assertIsNone(refused)
        self.assertIn("already bound", err)
        self.assertEqual(foldcompose.activation_state("testproj"),
                         (foldcompose.ACTIVATION_ACTIVE, self.base))

    def test_invalid_trunk_is_refused_without_writing(self):
        path, err = foldcompose.activate("testproj", self.repo, "not-a-commit")
        self.assertIsNone(path)
        self.assertIn("names no commit", err)
        self.assertEqual(foldcompose.activation_state("testproj"),
                         (foldcompose.ACTIVATION_INACTIVE, None))

    def test_activation_ancestry_error_is_UNKNOWN_not_in_scope(self):
        _path, err = foldcompose.activate("testproj", self.repo, self.base)
        self.assertIsNone(err, err)
        with mock.patch.object(
                foldcompose, "_git",
                return_value=(128, "", "fixture ancestry error")):
            rows = foldcompose.composition_proof(
                self.repo, "testproj", self.base)
        self.assertEqual(rows[0][0], "activation")
        self.assertIsNone(rows[0][1])
        self.assertIn("could not decide", rows[0][2])

    def test_deleted_or_corrupt_active_record_is_UNKNOWN(self):
        path, err = foldcompose.activate("testproj", self.repo, self.base)
        self.assertIsNone(err, err)
        os.remove(path)
        self.assertEqual(foldcompose.activation_state("testproj"),
                         (foldcompose.ACTIVATION_UNKNOWN, None))
        rows = foldcompose.composition_proof(self.repo, "testproj", self.base)
        self.assertEqual(rows[0][0], "activation")
        self.assertIsNone(rows[0][1])
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{{{")
        self.assertEqual(foldcompose.activation_state("testproj"),
                         (foldcompose.ACTIVATION_UNKNOWN, None))


class ApplyDoorTest(ComposeBase):
    def test_apply_is_refused_without_the_integrator_declaration(self):  # noqa: VACUOUS_ASSERTION — the same argv WITHOUT --apply returns 0 unconditionally on the same observable
        self.lane("car-a", [("a.txt", "a")])
        room, path = self.room()
        before = self.git("rev-parse", "HEAD", where=path)
        rc = foldcompose.cmd_compose(
            ["--room", room, "--base", self.base, "--car", "row-a:car-a",
             "--repo", self.repo, "--apply"])
        self.assertEqual(rc, 2)
        self.assertEqual(self.git("rev-parse", "HEAD", where=path), before,
                         "a refused --apply still reset the room")
        # THE CONTROL: the same argv WITHOUT --apply is always safe and says
        # what it would do, so the refusal above is about the door and not
        # about a verb that cannot run.
        rc = foldcompose.cmd_compose(
            ["--room", room, "--base", self.base, "--car", "row-a:car-a",
             "--repo", self.repo])
        self.assertEqual(rc, 0)
        self.assertEqual(self.git("rev-parse", "HEAD", where=path), before)

    def test_a_JUNK_TAIL_refuses_before_the_room_is_touched(self):  # noqa: VACUOUS_ASSERTION — the same argv WITHOUT the junk token returns 0 unconditionally on the last lines
        """THE PROBE THE APPLY-READER CENSUS ASKS FOR when a verb declares an
        alternative guard. `--apply` resets a room hard, so a junk tail must
        refuse before any work — `helm seat down codex --bogus` once stopped
        the seat and exited 0, which is the incident that census exists for.
        """
        self.lane("car-a", [("a.txt", "a")])
        room, path = self.room()
        before = self.git("rev-parse", "HEAD", where=path)
        os.environ["HELM_WORK_INTEGRATOR"] = "1"
        for junk in (["--bogus"], ["stray"], ["--room"], ["help"],
                     ["-h"], ["--help"], ["--room", room],
                     ["--base", self.base], ["--repo", self.repo],
                     ["--verify", self.base], ["--activate", self.base],
                     ["--apply"]):
            with self.subTest(junk=junk):
                rc = foldcompose.cmd_compose(
                    ["--room", room, "--base", self.base, "--car",
                     "row-a:car-a", "--repo", self.repo, "--apply"] + junk)
                self.assertEqual(rc, 2)
                self.assertEqual(self.git("rev-parse", "HEAD", where=path),
                                 before, "a junk tail still reset the room")
        rc = foldcompose.cmd_compose(
            ["--room", room, "--base", self.base, "--car", "row-a:car-a",
             "--repo", self.repo, "--apply"])
        self.assertEqual(rc, 0)
        self.assertNotEqual(self.git("rev-parse", "HEAD", where=path), before)

    def test_duplicate_verify_and_activate_values_refuse(self):
        for mode in ("--verify", "--activate"):
            with self.subTest(mode=mode):
                rc = foldcompose.cmd_compose(
                    [mode, self.base, mode, self.base, "--repo", self.repo])
                self.assertEqual(rc, 2)
        self.assertEqual(foldcompose.activation_state("testproj"),
                         (foldcompose.ACTIVATION_INACTIVE, None))

    def test_repeated_car_is_the_only_repeatable_valued_option(self):
        self.lane("car-a", [("a.txt", "a")])
        self.lane("car-b", [("b.txt", "b")])
        room, path = self.room()
        before = self.git("rev-parse", "HEAD", where=path)
        rc = foldcompose.cmd_compose(
            ["--room", room, "--base", self.base,
             "--car", "row-a:car-a", "--car", "row-b:car-b",
             "--repo", self.repo])
        self.assertEqual(rc, 0)
        self.assertEqual(self.git("rev-parse", "HEAD", where=path), before)

    def test_apply_with_the_declaration_composes_and_writes(self):  # noqa: VACUOUS_ASSERTION — rc 0, a moved tip and a manifest read back by that tip are all unconditional positives
        self.lane("car-a", [("a.txt", "a")])
        room, path = self.room()
        os.environ["HELM_WORK_INTEGRATOR"] = "1"
        rc = foldcompose.cmd_compose(
            ["--room", room, "--base", self.base, "--car", "row-a:car-a",
             "--repo", self.repo, "--apply"])
        self.assertEqual(rc, 0)
        tip = self.git("rev-parse", "HEAD", where=path)
        self.assertNotEqual(tip, self.base)
        got = foldcompose.read_manifest("testproj", tip)
        self.assertEqual((got or {}).get("result_tip"), tip)
        self.assertEqual([c["row"] for c in got["cars"]], ["row-a"])


if __name__ == "__main__":
    unittest.main()


class UnreadableIsNotAbsentTest(unittest.TestCase):
    """AN UNREADABLE AUTHORITY MUST NEVER RENDER AS A NEGATIVE THAT EXEMPTS.

    Two readers in this module answered a FAILED READ with a POSITIVE claim of
    absence, and both feed a door that treats absence as "nothing to enforce":

      `project_state` called `registry.load()` without strict, so an
      unreadable registry arrived as an empty projects map and the function
      answered "unregistered" -- which the fold reads as "no project can have
      been activated" and PASSES.

      `_activation_file` asked `os.path.lexists`, which swallows the OSError
      and answers False for a path it could not examine as readily as for one
      that is gone.

    These arms hold each reader's input unreadable and assert the answer is
    UNKNOWN. Each carries the positive control beside it -- a genuinely absent
    input must still read as absent -- because a reader that answered UNKNOWN
    to everything would pass the first half and be useless.

    Not every conflation is this defect: `read_manifest` merges absence and
    malformation deliberately and says so, because there both mean NO EVIDENCE
    and the fold treats both as unproven. The test is the DIRECTION of the
    merged answer, not the merging.
    """

    def _unreadable(self):
        """A real path inside a directory this process cannot examine."""
        tmp = tempfile.mkdtemp(prefix="helm-test-unreadable-")
        shut = os.path.join(tmp, "shut")
        os.mkdir(shut)
        target = os.path.join(shut, "activation.json")
        with open(target, "w", encoding="utf-8") as fh:
            fh.write('{"v": 1, "trunk": "%s"}' % ("a" * 40))
        os.chmod(shut, 0o000)

        def restore():
            os.chmod(shut, 0o700)
            shutil.rmtree(tmp, ignore_errors=True)

        self.addCleanup(restore)
        return target

    def test_a_path_it_cannot_examine_is_unknown_and_a_missing_one_is_absent(self):
        hidden = self._unreadable()
        self.assertFalse(os.path.lexists(hidden),
                         "the fixture is not exercising the swallow: lexists "
                         "can still see this path")
        self.assertEqual(foldcompose._presence(hidden), "unknown",
                         "a file that EXISTS but could not be examined was "
                         "reported as measured absence")
        # THE CONTROL MUST NOT ROUTE THROUGH THE LOCKED DIRECTORY. A path
        # under it resolves through a component this process cannot examine,
        # so it answers "unknown" correctly and proves nothing about absence.
        open_dir = tempfile.mkdtemp(prefix="helm-test-open-")
        self.addCleanup(shutil.rmtree, open_dir, ignore_errors=True)
        self.assertEqual(
            foldcompose._presence(os.path.join(open_dir, "gone.json")),
            "absent", "a genuinely missing path must still read as absent")

    def test_a_BROKEN_CONTAINER_is_unknown_even_though_the_child_cannot_exist(self):
        """A BROKEN CONTAINER IS NOT AN ANSWER ABOUT THE FILE. When a parent
        is a regular file, or exists and refuses to be read, the child's
        non-existence is a FACT -- so "absent" is factually true and still the
        wrong DIRECTION, because it is the negative that grants the exemption.
        A project home replaced by a regular file reads INACTIVE on both
        activation paths at once: a broken estate reported as
        nothing-to-enforce.

        Both controls sit beside it, and the first is the one that bounds this
        cure: a container that was NEVER CREATED is ordinary absence, since a
        project that has never been activated has no manifest directory at all.
        """
        tmp = tempfile.mkdtemp(prefix="helm-test-broken-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        not_a_dir = os.path.join(tmp, "home-replaced-by-a-file")
        with open(not_a_dir, "w", encoding="utf-8") as fh:
            fh.write("this is where a project home should be")
        self.assertEqual(
            foldcompose._presence(os.path.join(not_a_dir, "activation.json")),
            "unknown", "ENOTDIR was read as a measured absence")
        shut = os.path.join(tmp, "unreadable-home")
        os.mkdir(shut)
        os.chmod(shut, 0o000)
        self.addCleanup(os.chmod, shut, 0o700)
        self.assertEqual(
            foldcompose._presence(os.path.join(shut, "activation.json")),
            "unknown",
            "a container that EXISTS and refuses to be read was reported as "
            "a measured absence")
        # TWO CONTROLS, AND THE FIRST IS THE ONE THAT BOUNDS THIS CURE. A
        # project that has never been activated has NO manifest directory at
        # all, so a MISSING container is the commonest honest "nothing here"
        # in this module. Answering unknown there makes every never-activated
        # project unreadable, and six arms in this file fail when it does.
        self.assertEqual(
            foldcompose._presence(os.path.join(tmp, "never-made", "a.json")),
            "absent",
            "a container that was never created is ordinary absence, not a "
            "broken estate")
        self.assertEqual(foldcompose._presence(os.path.join(tmp, "gone.json")),
                         "absent",
                         "a genuinely missing file in a REAL directory must "
                         "still read absent")

    def test_an_unreadable_activation_file_is_unknown_not_absent(self):
        hidden = self._unreadable()
        state, trunk = foldcompose._activation_file(hidden)
        self.assertEqual(state, "unknown")
        self.assertIsNone(trunk)

    def test_a_container_that_is_a_DANGLING_LINK_is_unknown_not_absent(self):
        """`os.stat` FOLLOWS links, so its ENOENT merges two worlds: nothing is
        there at all, and something IS there that does not resolve. A project
        home that is a dangling symlink is the second -- a broken estate, not a
        project that was never activated -- and absence is the direction that
        grants the exemption.

        `lstat` separates them because it does not follow. It cannot do the job
        ALONE: on a dangling link lstat SUCCEEDS, so a reader that asks only
        lstat falls through to absence unchanged. The discriminator is the
        PAIR, and the controls below pin both of its answers.
        """
        tmp = tempfile.mkdtemp(prefix="helm-test-dangling-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        dangling = os.path.join(tmp, "home-is-a-dangling-link")
        os.symlink(os.path.join(tmp, "nothing-here"), dangling)
        self.assertTrue(os.path.lexists(dangling),
                        "the fixture is not a dangling link")
        self.assertFalse(os.path.exists(dangling),
                         "the fixture resolves, so it is not dangling")
        self.assertEqual(
            foldcompose._presence(os.path.join(dangling, "activation.json")),
            "unknown", "a dangling project home was read as measured absence")
        # CONTROL ONE: a real directory with no file in it is ordinary absence.
        real = os.path.join(tmp, "real-home")
        os.mkdir(real)
        self.assertEqual(
            foldcompose._presence(os.path.join(real, "activation.json")),
            "absent")
        # CONTROL TWO: a container that was never created at all is ordinary
        # absence -- the case that bounds this whole cure.
        self.assertEqual(
            foldcompose._presence(os.path.join(tmp, "never", "a.json")),
            "absent")

    def test_an_alias_whose_parent_is_unreadable_is_unknown_not_unregistered(self):
        """`os.path.realpath` SWALLOWS a resolution failure and returns the
        path UNRESOLVED, so a registered ALIAS whose own parent loses search
        permission stops matching the physical repo and this function answers
        "unregistered" -- a positive claim produced by a failed read, while the
        repo itself is still perfectly reachable.

        The negative is withheld PER-ENTRY, never wholesale: one unreadable
        registration must not make every other project's answer unknown, which
        the third assertion pins.
        """
        tmp = tempfile.mkdtemp(prefix="helm-test-alias-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        repo = os.path.join(tmp, "physical-repo")
        os.makedirs(repo)
        aliasdir = os.path.join(tmp, "aliasdir")
        os.mkdir(aliasdir)
        alias = os.path.join(aliasdir, "alias")
        os.symlink(repo, alias)
        reg = {"version": 1, "projects": {"named": {"path": alias}}}

        with mock.patch.object(foldcompose.registry, "load",
                               lambda *_a, **_kw: reg):
            self.assertEqual(foldcompose.project_state(repo),
                             ("registered", "named"),
                             "the control failed: the alias does not resolve "
                             "to this repo even while readable")
            os.chmod(aliasdir, 0o000)
            self.addCleanup(os.chmod, aliasdir, 0o700)
            state, project = foldcompose.project_state(repo)
            self.assertEqual(state, "unknown",
                             "an alias that could not be resolved produced a "
                             "positive claim that this repo is unregistered")
            self.assertIsNone(project)
            self.assertTrue(os.path.isdir(repo),
                            "the physical repo must still be reachable, or "
                            "this arm is about something else")
            os.chmod(aliasdir, 0o700)
            other = os.path.join(tmp, "elsewhere")
            os.makedirs(other)
            self.assertEqual(foldcompose.project_state(other),
                             ("unregistered", None),
                             "a readable registry must still answer the "
                             "honest negative")

    def test_an_unreadable_registry_is_unknown_not_unregistered(self):
        """The dangerous direction: "unregistered" tells the fold that no
        project can have been activated, so it PASSES on a failed read."""
        tmp = tempfile.mkdtemp(prefix="helm-test-registry-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        root = os.path.join(tmp, "repo")
        os.makedirs(root)

        def unreadable(*_a, **_kw):
            raise OSError(13, "Permission denied")

        with mock.patch.object(foldcompose.registry, "load", unreadable):
            state, project = foldcompose.project_state(root)
        self.assertEqual(state, "unknown",
                         "an unreadable registry was reported as a positive "
                         "claim that this repo is not a registered project")
        self.assertIsNone(project)

    def test_the_strict_read_is_what_makes_that_handler_reachable(self):
        """The cure is the ARGUMENT, not the try. Without strict, the default
        swallows the failure one level down and the except never runs, so this
        arm asserts the call that was actually made.
        """
        seen = {}

        def spy(*_a, **kw):
            seen["strict"] = kw.get("strict")
            return {"version": 1, "projects": {}}

        tmp = tempfile.mkdtemp(prefix="helm-test-registry-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        with mock.patch.object(foldcompose.registry, "load", spy):
            foldcompose.project_state(tmp)
        self.assertIs(seen.get("strict"), True,
                      "project_state read the registry non-strictly, so a "
                      "failed read returns the default and its own except "
                      "handler is unreachable")

    def test_a_readable_registry_still_answers_registered_and_unregistered(self):
        """The positive control for the strict change: ordinary reads are
        untouched, so the cure cannot be credited to refusing everything."""
        tmp = tempfile.mkdtemp(prefix="helm-test-registry-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        root = os.path.join(tmp, "repo")
        os.makedirs(root)
        reg = {"version": 1, "projects": {"named": {"path": root}}}
        with mock.patch.object(foldcompose.registry, "load",
                               lambda *_a, **_kw: reg):
            self.assertEqual(foldcompose.project_state(root),
                             ("registered", "named"))
            other = os.path.join(tmp, "elsewhere")
            os.makedirs(other)
            self.assertEqual(foldcompose.project_state(other),
                             ("unregistered", None))


class FloorReachableResolverTest(unittest.TestCase):
    """A CHECKED RESOLVER MUST EXIST AT THE INTERPRETER THIS REPO PROMISES.

    THE GATE CANNOT SEE THIS CLASS AT ALL, which is why it is an arm rather
    than a convention. `helm gate run` binds landing authority and runs one
    interpreter -- 3.14 on the fab today -- so a call that is a TypeError on
    the 3.9 floor is invisible to the only instrument that runs before a land.
    CI's 3.9 leg would catch it AFTER the fact.

    `os.path.realpath`'s `strict` keyword is documented as 3.10. Whether a
    given 3.9 build accepts it is a PATCH-LEVEL question -- the 3.9.25 on this
    box does, and what `actions/setup-python` resolves for "3.9" is not
    knowable from here -- so the spelling is refused outright rather than
    reasoned about. `pathlib.Path.resolve` has taken `strict` since 3.6 and is
    MEASURED to give identical answers on 3.9.25 and 3.14.4 for a real
    directory, a symlink to one, a dangling link, a never-created path, and an
    entry whose parent is 0o000.

    SCOPED TO THE ONE SPELLING, deliberately: this is not a general 3.9 syntax
    sweep (CI's matrix is that), it is a pin on the single construct this
    module reached for and the floor may not carry.
    """

    @staticmethod
    def _strict_realpath_calls(source, label):
        """Lines in one module that call realpath with a `strict` keyword."""
        import ast
        hits = []
        for node in ast.walk(ast.parse(source, filename=label)):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute) or func.attr != "realpath":
                continue
            if any(kw.arg == "strict" for kw in node.keywords):
                hits.append("%s:%d" % (label, node.lineno))
        return hits

    def test_the_sweep_FINDS_the_spelling_it_is_written_to_refuse(self):  # noqa: VACUOUS_ASSERTION — the first assertEqual is the unconditional positive control; the empty one beside it is the negative half of the same must-hit
        """THE MUST-HIT, and it runs the same detector the sweep does. An
        empty result below means "no module spells it that way" only if this
        passes; otherwise it means the detector cannot see the construct at
        all, which looks identical."""
        self.assertEqual(
            self._strict_realpath_calls(
                "import os.path\np = os.path.realpath(x, strict=True)\n",
                "synthetic"),
            ["synthetic:2"])
        self.assertEqual(
            self._strict_realpath_calls(
                "import os.path\np = os.path.realpath(x)\n", "synthetic"),
            [], "the detector fires on a call that has no strict keyword")

    def test_no_module_calls_realpath_with_the_3_10_strict_keyword(self):  # noqa: VACUOUS_ASSERTION — the must-hit at the top of this method is the unconditional positive control on this exact detector, so the empty sweep below cannot pass because the detector is blind
        # THE MUST-HIT RIDES INSIDE THIS METHOD, not only beside it. An empty
        # `found` is the finding here, and a detector that cannot see the
        # construct produces exactly the same empty list as a clean tree; a
        # control in a sibling method proves that only until someone deletes
        # the sibling.
        self.assertEqual(
            self._strict_realpath_calls(
                "import os.path\np = os.path.realpath(x, strict=True)\n",
                "must-hit"),
            ["must-hit:2"], "the detector cannot see the construct it sweeps "
                            "for, so an empty sweep below means nothing")
        root = os.path.dirname(os.path.dirname(os.path.abspath(
            foldcompose.__file__)))
        found, scanned = [], 0
        for base, dirs, names in os.walk(os.path.join(root, "helm")):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for name in sorted(names):
                if not name.endswith(".py"):
                    continue
                path = os.path.join(base, name)
                with io.open(path, encoding="utf-8") as fh:
                    source = fh.read()
                scanned += 1
                found.extend(self._strict_realpath_calls(
                    source, os.path.relpath(path, root)))
        self.assertGreater(scanned, 1, "the sweep read nothing; it proves "
                                       "nothing about the floor")
        self.assertEqual(found, [],
                         "realpath(..., strict=...) is 3.10+ and this repo "
                         "declares a 3.9 floor; use "
                         "pathlib.Path(p).resolve(strict=True): %s" % found)

    def test_the_checked_resolver_in_use_answers_the_five_poles(self):  # noqa: VACUOUS_ASSERTION — the two assertEquals are the unconditional positive control on this exact resolver: it must RESOLVE a real directory and a symlink to one in the same method that pins what it refuses
        """The positive control for the pin above: the replacement is not
        merely floor-safe, it makes the distinctions the cure needs."""
        import pathlib
        tmp = tempfile.mkdtemp(prefix="helm-test-resolver-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        real = os.path.join(tmp, "real")
        os.makedirs(real)
        good = os.path.join(tmp, "good")
        os.symlink(real, good)
        dangling = os.path.join(tmp, "dangling")
        os.symlink(os.path.join(tmp, "nope"), dangling)
        locked = os.path.join(tmp, "locked")
        os.mkdir(locked)
        alias = os.path.join(locked, "alias")
        os.symlink(real, alias)

        resolve = lambda q: str(pathlib.Path(q).resolve(strict=True))
        self.assertEqual(resolve(real), os.path.realpath(real))
        self.assertEqual(resolve(good), os.path.realpath(real))
        with self.assertRaises(FileNotFoundError):
            resolve(dangling)
        with self.assertRaises(FileNotFoundError):
            resolve(os.path.join(tmp, "never-created"))
        os.chmod(locked, 0o000)
        self.addCleanup(os.chmod, locked, 0o700)
        with self.assertRaises(PermissionError):
            resolve(alias)


class SkippedEntryDoesNotChooseTheNameTest(unittest.TestCase):
    """AN ENTRY WE COULD NOT READ MUST NOT DECIDE WHICH REGISTRATION WINS.

Withholding the final NEGATIVE when an entry cannot be resolved is not
    enough, because the hole opens one step earlier. The loop walks
    `sorted(projects.items())` and answers with the FIRST entry that matches,
    the registry permits two entries to hold the same physical path, and the
    caller uses the returned NAME to find the activation record. An entry that
    fails to resolve must therefore not promote a later one: "the registration
    is `z`" asserted on the strength of not having been able to read `a` is a
    positive claim from a failed read, and a wrong name is a wrong activation
    answer rather than a cosmetic one.

    The two controls matter as much as the arm: withholding must not become
    wholesale, and a failure AFTER the match must not withhold anything,
    because the answer was already decided by then.
    """

    def _registry(self, mapping):
        return {"version": 1, "projects": mapping}

    def _state(self, reg, root):
        with mock.patch.object(foldcompose.registry, "load",
                               lambda *_a, **_kw: reg):
            return foldcompose.project_state(root)

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-mixed-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = os.path.join(self.tmp, "physical-repo")
        os.makedirs(self.repo)
        self.locked = os.path.join(self.tmp, "locked")
        os.mkdir(self.locked)
        self.hidden = os.path.join(self.locked, "hidden-alias")
        os.symlink(self.repo, self.hidden)

    def test_an_unreadable_EARLIER_entry_does_not_pick_the_name(self):
        reg = self._registry({"a-hidden": {"path": self.hidden},
                              "z-direct": {"path": self.repo}})
        self.assertEqual(
            self._state(reg, self.repo), ("registered", "a-hidden"),
            "the control failed: while readable the EARLIER entry must win, "
            "or this arm is not about promotion at all")
        os.chmod(self.locked, 0o000)
        self.addCleanup(os.chmod, self.locked, 0o700)
        state, project = self._state(reg, self.repo)
        self.assertEqual(state, "unknown",
                         "an entry that could not be read chose the winner: "
                         "the name came from a failed read, and the caller "
                         "resolves activation by that name")
        self.assertIsNone(project)

    def test_a_cyclic_registry_alias_is_unknown_without_promoting_a_name(self):
        """Exercise the real resolver through project_state, not in isolation.

        A real loop raises RuntimeError on older Python versions and OSError
        on newer ones. This fixture pins classification on either interpreter;
        the injected-exception arm below separately pins the older catch on
        a modern gate, where this real loop alone cannot distinguish it.
        """
        reg = self._registry({"a-hidden": {"path": self.hidden},
                              "z-direct": {"path": self.repo}})
        self.assertEqual(self._state(reg, self.repo),
                         ("registered", "a-hidden"))
        os.unlink(self.hidden)
        os.symlink(os.path.basename(self.hidden), self.hidden)
        self.assertEqual(os.readlink(self.hidden),
                         os.path.basename(self.hidden),
                         "the fixture must be a self-referential symlink")
        self.assertEqual(self._state(reg, self.repo), ("unknown", None))
        self.assertEqual(
            self._state(self._registry({"a-loop": {"path": self.hidden}}),
                        self.repo),
            ("unknown", None), "a loop cannot establish an honest negative")
        self.assertEqual(
            self._state(self._registry({"a-direct": {"path": self.repo},
                                       "z-loop": {"path": self.hidden}}),
                        self.repo),
            ("registered", "a-direct"))

    def test_resolver_RuntimeError_is_unknown_on_every_gate_interpreter(self):
        """Inject the older resolver's exception, not a simulated real loop.

        Modern Path.resolve raises OSError for the real loop above, so that
        fixture alone stays green if the RuntimeError catch is removed. This
        arm supplies the older exception at the same resolver seam and proves
        the seam was reached, while other paths still use the real resolver.
        """
        reg = self._registry({"a-hidden": {"path": self.hidden},
                              "z-direct": {"path": self.repo}})
        self.assertEqual(self._state(reg, self.repo),
                         ("registered", "a-hidden"))
        resolve = foldcompose.pathlib.Path.resolve
        raised = []

        def legacy_resolve(path, *, strict=False):
            if str(path) == self.hidden:
                raised.append(strict)
                raise RuntimeError("Symlink loop from %r" % self.hidden)
            return resolve(path, strict=strict)

        with mock.patch.object(foldcompose.pathlib.Path, "resolve",
                               legacy_resolve):
            self.assertEqual(self._state(reg, self.repo), ("unknown", None))
            self.assertEqual(
                self._state(self._registry({"a-direct": {"path": self.repo},
                                           "z-hidden": {"path": self.hidden}}),
                            self.repo),
                ("registered", "a-direct"))
        self.assertEqual(raised, [True],
                         "the strict resolver exception was not exercised")

    def test_a_failure_AFTER_the_match_withholds_nothing(self):
        """The answer was already decided in sorted order, so the later
        unreadable entry is not evidence about it. Without this control the
        cure could be 'answer unknown whenever anything is unreadable'."""
        reg = self._registry({"a-direct": {"path": self.repo},
                              "z-hidden": {"path": self.hidden}})
        os.chmod(self.locked, 0o000)
        self.addCleanup(os.chmod, self.locked, 0o700)
        self.assertEqual(self._state(reg, self.repo),
                         ("registered", "a-direct"))

    def test_another_project_still_gets_its_honest_negative(self):
        """The withholding stays PER-ENTRY: one unreadable registration must
        not make every other repo's answer unknown."""
        elsewhere = os.path.join(self.tmp, "elsewhere")
        os.makedirs(elsewhere)
        reg = self._registry({"a-direct": {"path": self.repo}})
        self.assertEqual(self._state(reg, elsewhere), ("unregistered", None))


class OnlyENOENTEstablishesAbsenceTest(unittest.TestCase):
    """INSIDE THE CURE THAT SAYS SO: the container's second read had `except
    OSError: return "absent"`.

    `_presence` reaches that read only when `os.stat(parent)` answered ENOENT,
    and the `os.lstat(parent)` beside it separates "nothing is there" from "an
    entry that does not resolve". But the second read can fail for its OWN
    reasons -- ELOOP in a grandparent, ENAMETOOLONG, or EACCES if a mode
    changed between the two calls -- and NONE of those establish absence. The
    module's whole law is that a negative must not come from a failed read,
    and this was that law broken in the code that states it.

    THE STATE IS CONSTRUCTED RATHER THAN STAGED ON DISK, and that is the
    honest shape: stat answering ENOENT while lstat answers EACCES on the SAME
    path is a race between two syscalls, not a filesystem a test can lay out.
    Staging some other fixture that happens to be red would be testing a
    different thing.
    """

    def _presence_with_parent_errors(self, stat_error, lstat_error):
        target = os.path.join(os.sep, "definitely", "not", "here", "a.json")
        real_stat, real_lstat = os.stat, os.lstat

        def fake_lstat(path, *a, **kw):
            if path == target:
                raise FileNotFoundError(2, "no such file")
            if lstat_error is not None:
                raise lstat_error
            return real_lstat(os.sep)

        def fake_stat(path, *a, **kw):
            if stat_error is not None:
                raise stat_error
            return real_stat(os.sep)

        with mock.patch.object(os, "lstat", fake_lstat), \
                mock.patch.object(os, "stat", fake_stat):
            return foldcompose._presence(target)

    def test_a_parent_lstat_that_fails_for_ITS_OWN_reason_is_unknown(self):
        self.assertEqual(
            self._presence_with_parent_errors(
                FileNotFoundError(2, "no such file"),
                PermissionError(13, "permission denied")),
            "unknown",
            "EACCES on the second read was read as proof that nothing is "
            "there, which is a negative granted by a failed read")

    def test_an_ELOOP_on_the_parent_is_unknown_too(self):
        self.assertEqual(
            self._presence_with_parent_errors(
                FileNotFoundError(2, "no such file"),
                OSError(40, "too many levels of symbolic links")),
            "unknown")

    def test_the_control_ENOENT_on_both_reads_is_still_ordinary_absence(self):
        """Without this the cure could be "never answer absent", which would
        make every never-activated project unreadable -- the way this cure
        first went wrong."""
        self.assertEqual(
            self._presence_with_parent_errors(
                FileNotFoundError(2, "no such file"),
                FileNotFoundError(2, "no such file")),
            "absent")

    def test_the_control_a_parent_that_reads_fine_is_still_unknown(self):
        """stat ENOENT with lstat SUCCEEDING is the dangling-link shape, and
        it must stay unknown -- so the arms above are not just the mock
        answering unknown to everything."""
        self.assertEqual(
            self._presence_with_parent_errors(
                FileNotFoundError(2, "no such file"), None),
            "unknown")


class EmptyValueIsNotAModeTest(unittest.TestCase):
    """AN EMPTY FLAG VALUE MUST NOT SELECT A DESTRUCTIVE MODE BY FALLING
    THROUGH.

    `--verify` and `--activate` are complete modes, and the guards that keep
    them from being mixed with composition options read the flag's VALUE.
    An empty string satisfies a "wants a value" check and is FALSY everywhere
    after it, so `--verify ""` is a value that passes both mixing guards, reads
    as absent to the mode dispatch, and falls through to the COMPOSITION path
    -- the one where `--apply` resets a room hard.

    TWO CURES, AND THEY ARE NOT INDEPENDENT -- a mutation matrix said so and
    the docstring says what the matrix said. The parser refuses an empty value
    outright, and the mode dispatch asks whether the FLAG WAS NAMED rather
    than whether its value is truthy. The parser cure is the one that closes
    the hole: with it in place, no accepted argv can make a mode value falsy,
    because every non-empty string is truthy. Reverting the dispatch cure
    alone therefore kills NO arm here, and none can be written that would --
    which is the honest status of that half.

    It stays anyway, as defence in depth and as a statement of the rule:
    "was this mode requested" and "what did the caller pass" are different
    questions, and a later change that relaxes the parser must not silently
    reopen the fall-through.
    """

    def _run(self, argv):
        """(rc, combined output) from the real entry point, no work done."""
        import io as _io
        import contextlib
        out, err = _io.StringIO(), _io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = foldcompose.cmd_compose(argv)
        return rc, out.getvalue() + err.getvalue()

    def test_an_empty_valued_mode_flag_is_refused_before_any_work(self):
        rc, text = self._run(["--verify", "", "--apply"])
        self.assertEqual(rc, 2, "an empty --verify reached past the parser")
        self.assertIn("empty value", text)
        self.assertNotIn("resets a room", text,
                         "the refusal came from the unknown-token branch, not "
                         "from the empty-value check this arm is about")

    def test_the_same_holds_for_activate(self):
        rc, text = self._run(["--activate", "", "--apply"])
        self.assertEqual(rc, 2)
        self.assertIn("empty value", text)

    def test_a_NON_empty_mode_flag_still_refuses_to_mix_with_apply(self):
        """The positive control for the mixing guard itself: with a real value
        the complete-mode refusal must still fire, so the cure cannot be
        credited to refusing everything that carries --apply."""
        rc, text = self._run(["--verify", "a" * 40, "--apply"])
        self.assertEqual(rc, 2)
        self.assertIn("complete mode", text)

    def test_the_mode_dispatch_reads_the_FLAG_not_its_value(self):
        """Naming --verify selects verify mode rather than falling through to
        compose.

        THIS ARM DOES NOT PROVE THE TWO CURES INDEPENDENT, and nothing here
        can: with the parser refusing empty values, no accepted argv makes a
        mode value falsy, so a mutation selecting on the value again passes
        this arm unchanged. It pins that verify mode is REACHED, nothing more.

        AND THE DEFENCE-IN-DEPTH HALF IS THE FLAG-MEMBERSHIP DISPATCH, NOT A
        VALUE-BASED ONE -- an earlier wording here had it backwards. Selecting
        on the VALUE is the defective form this arm is named for; the shipped
        dispatch asks `"--verify" in args`, and it is that question, not the
        value's truthiness, that stays as depth behind the parser.
        """
        rc, text = self._run(["--verify", "a" * 40])
        # The rc is NOT the discriminator here: verify mode legitimately exits
        # 2 when the cwd is not a registered project, which is the same code a
        # fall-through would produce. The BRANCH is the discriminator, and only
        # verify mode can print any of these.
        self.assertTrue(
            "not a registered project" in text or "composition proof" in text
            or "NOT IN SCOPE" in text,
            "naming --verify did not select verify mode; it fell through "
            "to another branch: rc=%s %r" % (rc, text[:200]))
        self.assertNotIn("unknown token", text)
        self.assertNotIn("complete mode", text,
                         "verify alone must not trip the mixing guard")


class BaseGapTest(ComposeBase):
    """What a manifest cannot say about itself: what its base is MISSING.

    A compose record proves what the tree CONTAINS — the cars, their source
    commits, the result trees — and every one of those fields is equally true
    of a base that is weeks behind. A reviewer who checks the tree against the
    manifest gets a clean confirmation either way, which is the kind of answer
    that stops them looking.
    """

    def advance_main(self, n):
        """`n` more commits on main, newest first, returned as (sha, subject)."""
        made = []
        for i in range(n):
            sha = self.commit("ahead-%d.txt" % i, "a")
            made.append((sha, "ahead-%d.txt" % i))
        made.reverse()
        return made

    def test_a_base_that_carries_trunk_is_not_behind(self):
        gap = foldcompose.base_gap(self.repo, self.base)
        self.assertEqual(gap["behind"], 0)
        self.assertIsNone(gap["unknown"])
        self.assertIn("nothing was missing",
                      "\n".join(foldcompose.render_base_gap(gap)))

    def test_a_behind_base_names_the_commits_it_is_missing(self):
        """A COUNT AND THE COMMITS, NEVER A BOOLEAN. "base: behind" is a word
        a reader shrugs at; the subjects are what stop a compose."""
        self.advance_main(2)
        gap = foldcompose.base_gap(self.repo, self.base)
        self.assertEqual(gap["behind"], 2)
        said = "\n".join(foldcompose.render_base_gap(gap))
        self.assertIn("BASE IS BEHIND: 2 commit(s)", said)
        self.assertIn("ahead-0.txt", said)
        self.assertIn("ahead-1.txt", said)

    def test_THE_STALE_REF_CANNOT_HIDE_THE_GAP(self):
        """THE ARM THIS WHOLE MEASUREMENT EXISTS FOR, and it is the production
        failure exactly. In the cli-proxy fork the compose that lost three
        shipped features reads ZERO behind against `origin/main` and EIGHT
        against local `main`, because the remote-tracking ref had not been
        fetched. Asking only the conventional ref prints "nothing is missing"
        over the exact commits that went missing, so every candidate is
        counted and the LARGEST is reported."""
        self.advance_main(3)
        # origin/main left pointing at the base: the un-fetched snapshot.
        self.git("update-ref", "refs/remotes/origin/main", self.base)
        stale_only = foldcompose.base_gap(self.repo, self.base,
                                          trunk="origin/main")
        self.assertEqual(stale_only["behind"], 0,
                         "the fixture does not reproduce the stale ref, so "
                         "the arm below proves nothing")
        gap = foldcompose.base_gap(self.repo, self.base)
        self.assertEqual(gap["behind"], 3)
        self.assertEqual(gap["trunk"], "main")
        self.assertEqual(dict(gap["measured_refs"]),
                         {"origin/main": 0, "main": 3})
        self.assertIn("trunk refs DISAGREE",
                      "\n".join(foldcompose.render_base_gap(gap)))

    def test_an_unmeasurable_trunk_is_UNKNOWN_and_never_zero(self):
        """A compose in a clone with no trunk ref must not read as "nothing is
        missing" — that is a measurement nobody took."""
        gap = foldcompose.base_gap(self.repo, self.base, trunk="no/such/ref")
        self.assertIsNone(gap["behind"])
        self.assertIn("not the same as missing nothing", gap["unknown"])
        self.assertIn("BASE GAP UNKNOWN",
                      "\n".join(foldcompose.render_base_gap(gap)))

    def test_a_capped_list_keeps_the_total_and_says_it_is_capped(self):
        """A capped rendering read as the whole set is how a twenty-item cap
        turns a hundred missing commits into twenty."""
        self.advance_main(foldcompose.GAP_LISTED + 3)
        gap = foldcompose.base_gap(self.repo, self.base)
        self.assertEqual(gap["behind"], foldcompose.GAP_LISTED + 3)
        self.assertEqual(len(gap["commits"]), foldcompose.GAP_LISTED)
        self.assertTrue(gap["truncated"])
        self.assertIn("... 3 more not listed",
                      "\n".join(foldcompose.render_base_gap(gap)))

    def test_the_count_is_stamped_with_the_moment_it_was_taken(self):
        """The number answers "what was already on trunk when this base was
        chosen". Re-asked later it answers "how far has trunk moved since",
        which every historical compose fails loudly — so the reader is told
        which moment it describes."""
        gap = foldcompose.base_gap(self.repo, self.base)
        self.assertIsInstance(gap["measured_ts"], int)
        self.assertIn("measured 20",
                      "\n".join(foldcompose.render_base_gap(gap)))

    def test_TWO_DIVERGENT_TRUNK_REFS_ARE_UNKNOWN_AND_NEVER_A_VOTE(self):
        """A STALE ref is an ANCESTOR of the true trunk — behind one way, zero
        the other — and out-voting it is exactly what taking the largest is
        for. A ref that is nonzero BOTH ways has commits of its own, so
        neither contains the other and nothing here can say which one the work
        lands on. Guessing would be a confident wrong answer in the register of
        a measurement."""
        self.advance_main(2)
        self.git("checkout", "-q", "-b", "tmp-divergent", self.base)
        self.commit("elsewhere.txt", "e")
        self.git("update-ref", "refs/remotes/origin/main", "HEAD")
        self.git("checkout", "-q", "main")
        gap = foldcompose.base_gap(self.repo, self.base)
        self.assertIsNone(gap["behind"])
        self.assertIn("DIVERGE", gap["unknown"])
        self.assertIn("BASE GAP UNKNOWN",
                      "\n".join(foldcompose.render_base_gap(gap)))
        # THE CONTROL, and without it the arm above is satisfied by a
        # measurement that calls everything UNKNOWN: the same repo with
        # origin/main moved back to being a plain ANCESTOR answers 2, because
        # a stale ref is out-voted rather than refused.
        self.git("update-ref", "refs/remotes/origin/main", self.base)
        current = foldcompose.base_gap(self.repo, self.base)
        self.assertEqual(current["behind"], 2)
        self.assertEqual(current["trunk"], "main")

    def test_a_repo_with_no_remote_is_complete_rather_than_stale(self):
        """A refusal with no cure is the shape a guard must never take. With
        nowhere to fetch FROM there is no news to have missed, so the local
        refs are the whole truth."""
        gap = foldcompose.base_gap(self.repo, self.base)
        self.assertFalse(gap["has_remote"])
        self.assertFalse(foldcompose.gap_is_a_lower_bound(gap))
        self.assertNotIn("LOWER BOUND",
                         "\n".join(foldcompose.render_base_gap(gap)))
        # THE CONTROL, on the same observable and unconditional: give the same
        # repo a remote it has never fetched and the SAME zero becomes a lower
        # bound. Without it the absence above is equally satisfied by a
        # predicate that never fires at all.
        self.git("init", "-q", "--bare", os.path.join(self.tmp, "remote.git"))
        self.git("remote", "add", "origin", os.path.join(self.tmp, "remote.git"))
        with_remote = foldcompose.base_gap(self.repo, self.base)
        self.assertTrue(with_remote["has_remote"])
        self.assertEqual(with_remote["behind"], 0)
        self.assertTrue(foldcompose.gap_is_a_lower_bound(with_remote))
        self.assertIn("LOWER BOUND",
                      "\n".join(foldcompose.render_base_gap(with_remote)))

    def test_a_zero_from_a_clone_that_never_fetched_is_a_LOWER_BOUND(self):
        """The error has ONE direction: everything trunk gained since the last
        fetch is invisible, so an unfetched clone can only ever say "nothing
        is missing". That is the sentence this whole measurement exists to
        stop being believed."""
        remote = os.path.join(self.tmp, "remote.git")
        self.git("init", "-q", "--bare", remote)
        self.git("remote", "add", "origin", remote)
        gap = foldcompose.base_gap(self.repo, self.base)
        self.assertTrue(gap["has_remote"])
        self.assertEqual(gap["behind"], 0)
        self.assertTrue(foldcompose.gap_is_a_lower_bound(gap),
                        "a zero from a clone that has never fetched was "
                        "reported as an answer")
        self.assertIn("LOWER BOUND",
                      "\n".join(foldcompose.render_base_gap(gap)))

    def test_plan_carries_the_gap_so_the_preview_can_show_it(self):
        lane = self.lane("car-a", [("a.txt", "a")])
        room, _path = self.room()
        self.advance_main(1)
        proposal, err = foldcompose.plan(
            self.repo, room, self.base,
            [{"row": "r1", "lane": lane.split("lane/", 1)[1]}])
        self.assertIsNone(err)
        self.assertEqual(proposal["base_gap"]["behind"], 1)

    def test_apply_refuses_a_behind_base_and_the_room_is_NOT_reset(self):  # noqa: VACUOUS_ASSERTION — the control is the SAME argv plus --base-behind-ok in the second half, which runs unconditionally and asserts the room DID move; the equality above is what proves the refusal happened before the destructive half, not that nothing ran
        """THE REFUSAL RUNS BEFORE THE DESTRUCTIVE HALF. --apply resets a room
        hard; a check that ran after it would have already spent the thing it
        was protecting."""
        self.lane("car-a", [("a.txt", "a")])
        room, path = self.room()
        # THE ROOM'S HEAD MUST DIFFER FROM THE RESET TARGET, or this arm
        # asserts a value rather than a behaviour.
        # `room()` creates the worktree AT self.base and the compose below
        # passes `--base self.base`, so a reset that DID run would land on the
        # same commit and the equality below would hold just as happily. The
        # arm asserted a value, not a behaviour. One commit in the room makes
        # HEAD distinct, so "HEAD did not move" can only be true if the reset
        # never ran.
        self.git("commit", "-q", "--allow-empty", "-m", "room is not base",
                 where=path)
        before = self.git("rev-parse", "HEAD", where=path)
        self.assertNotEqual(before, self.base,
                            "fixture: a reset to base must be OBSERVABLE here "
                            "or this arm cannot see one")
        self.advance_main(2)
        os.environ["HELM_WORK_INTEGRATOR"] = "1"
        argv = ["--room", room, "--base", self.base, "--car", "row-a:car-a",
                "--repo", self.repo, "--apply"]
        rc = foldcompose.cmd_compose(list(argv))
        self.assertEqual(rc, 2)
        self.assertEqual(self.git("rev-parse", "HEAD", where=path), before,
                         "a refused compose still reset the room")
        # THE CONTROL, and it is what keeps this from being a wall: composing
        # onto an older base on purpose stays possible, it just has to be said
        # out loud where the reviewer reads it.
        rc = foldcompose.cmd_compose(argv + ["--base-behind-ok"])
        self.assertEqual(rc, 0)
        self.assertNotEqual(self.git("rev-parse", "HEAD", where=path), before)

    def test_the_acknowledgement_is_recorded_where_the_reviewer_reads_it(self):
        """A judgement that lives only in the composer's shell history is a
        judgement the reviewer cannot see."""
        self.lane("car-a", [("a.txt", "a")])
        room, path = self.room()
        self.advance_main(1)
        os.environ["HELM_WORK_INTEGRATOR"] = "1"
        rc = foldcompose.cmd_compose(
            ["--room", room, "--base", self.base, "--car", "row-a:car-a",
             "--repo", self.repo, "--apply", "--base-behind-ok"])
        self.assertEqual(rc, 0)
        tip = self.git("rev-parse", "HEAD", where=path)
        with open(self.manifest_file(tip), encoding="utf-8") as fh:
            record = json.load(fh)
        self.assertTrue(record.get("base_behind_acknowledged"))
        self.assertEqual(record["base_gap"]["behind"], 1,
                         "the manifest records the gap MEASURED AT COMPOSE "
                         "TIME; a reader who recomputes it later is asking a "
                         "different question")

    def with_remote(self):
        """A real bare remote with main pushed to it, and origin/main fetched.

        Real `ls-remote` against a real remote, because the whole point of the
        r2 cure is that currency is ASKED rather than inferred, and a mocked
        answer would test my hypothesis instead of the transport."""
        bare = os.path.join(self.tmp, "remote.git")
        self.git("init", "-q", "--bare", bare)
        self.git("remote", "add", "origin", bare)
        self.git("push", "-q", "origin", "main")
        self.git("fetch", "-q", "origin")
        return bare

    def advance_remote(self):
        """One more commit on the REMOTE's main, pushed from a second clone.

        THE POINT IS THAT THIS REPO NEVER LEARNS OF IT. Advancing main here and
        winding the tracking ref back by hand looks equivalent and is not: the
        census picks the candidate with the LARGEST gap, so a local main that
        moved would win, and a local branch cannot be asked of any remote --
        the arm would then be measuring the wrong refusal."""
        bare = os.path.join(self.tmp, "remote.git")
        other = os.path.join(self.tmp, "other-clone")
        if not os.path.isdir(other):
            # `--branch main` AND NOT A BARE CLONE: `git init --bare` leaves
            # the remote HEAD on `master`, so a plain clone checks out nothing,
            # a commit there starts an UNRELATED root, and the push is refused
            # as non-fast-forward -- a fixture failure that reads like a defect
            # in the code under test.
            self.git("clone", "-q", "--branch", "main", bare, other,
                     where=self.tmp)
            self.git("config", "user.email", "other@x", where=other)
            self.git("config", "user.name", "other", where=other)
        with open(os.path.join(other, "elsewhere.txt"), "a",
                  encoding="utf-8") as fh:
            fh.write("x\n")
        self.git("add", "-A", where=other)
        self.git("commit", "-q", "-m", "pushed from elsewhere", where=other)
        # `HEAD:refs/heads/main` AND NOT `main`: a clone's local branch is
        # named after the remote HEAD it checked out, which need not be `main`
        # at all, and `push origin main` then fails with "src refspec main does
        # not match any" -- a fixture failure that reads like a defect.
        self.git("push", "-q", "origin", "HEAD:refs/heads/main", where=other)

    def test_currency_is_ASKED_of_the_remote_not_inferred_from_a_file(self):
        """task/2484 r2 R1, and it replaces three rounds of inference.

        A global FETCH_HEAD mtime is written by any fetch; a ref file's mtime
        is written by a repack; and FETCH_HEAD's CONTENT -- the cure offered in
        r1 -- still cannot date one entry, because `git fetch --append`
        refreshes the file while retaining an old line. One `ls-remote` answers
        outright what all of that was approximating."""
        self.with_remote()
        gap = foldcompose.base_gap(self.repo, self.base)
        self.assertIs(gap["ref_current"], True)
        self.assertFalse(foldcompose.gap_is_a_lower_bound(gap),
                         "a ref measured current against its own remote is "
                         "not a lower bound")

        # NOW THE REMOTE MOVES PAST THE REF THE COUNT WAS TAKEN ON, pushed
        # from SOMEWHERE ELSE and never fetched here. That is the everyday
        # stale clone, and it matters that this repo's own main does not move:
        # a local branch that wins the census cannot be asked of any remote, so
        # advancing main here would test the wrong refusal.
        self.advance_remote()
        gap = foldcompose.base_gap(self.repo, self.base)
        self.assertIs(gap["ref_current"], False)
        self.assertTrue(foldcompose.gap_is_a_lower_bound(gap),
                        "the remote has moved past the counted ref, so this "
                        "count can only UNDER-report what the base is missing")

    def test_ONE_observed_branch_cannot_certify_the_others_counted(self):
        """The census counts every candidate that resolves and the WINNER is
        only the one with the largest gap AMONG LOCAL REFS. Asking the remote
        about the winner alone and publishing an aggregate "current" is a claim
        about the whole census made from one member of it."""
        bare = self.with_remote()
        # origin/master exists here too, and it is the one that will move.
        self.git("push", "-q", "origin", "main:master")
        self.git("fetch", "-q", "origin")
        control = foldcompose.base_gap(self.repo, self.base)
        self.assertIs(control["ref_current"], True,
                      "both counted refs match their remote here")

        # A SECOND CLONE ADVANCES ONLY THE NON-WINNER, and this clone never
        # fetches. Every local count is still zero, no pair diverges, and the
        # WINNER still verifies -- which is exactly the state the winner-only
        # question answered "current" in.
        other = os.path.join(self.tmp, "mover")
        self.git("clone", "-q", "--branch", "master", bare, other,
                 where=self.tmp)
        self.git("config", "user.email", "m@x", where=other)
        self.git("config", "user.name", "m", where=other)
        with open(os.path.join(other, "moved.txt"), "w",
                  encoding="utf-8") as fh:
            fh.write("x\n")
        self.git("add", "-A", where=other)
        self.git("commit", "-q", "-m", "only master moves", where=other)
        self.git("push", "-q", "origin", "HEAD:refs/heads/master", where=other)

        gap = foldcompose.base_gap(self.repo, self.base)
        self.assertEqual(gap["behind"], 0,
                         "fixture: every LOCAL count must still read zero, or "
                         "this arm is about the count rather than currency")
        self.assertIs(gap["ref_current"], False)
        self.assertIn("origin/master", gap["ref_current_why"])
        self.assertTrue(foldcompose.gap_is_a_lower_bound(gap))

    def test_a_local_only_candidate_is_out_of_scope_not_unknown(self):
        """THE OVER-CORRECTION CONTROL. Treating a purely local branch as an
        unanswerable candidate makes the aggregate None in every ordinary
        clone -- a refusal with no cure. `main` with no remote sibling carries
        no news from anywhere; the hazard is a remote-tracking ref that has
        fallen behind what its remote publishes."""
        self.with_remote()
        gap = foldcompose.base_gap(self.repo, self.base)
        self.assertIn(("main", 0), gap["measured_refs"],
                      "fixture: a local candidate must be IN the census or "
                      "this arm proves nothing about skipping one")
        self.assertIs(gap["ref_current"], True)
        self.assertIn("local-only", gap["ref_current_why"])

    def test_no_askable_ref_is_UNKNOWN_and_never_current(self):  # noqa: VACUOUS_ASSERTION — the unconditional positive on the same object is the last line: has_remote False must still EXEMPT the gap from the lower-bound predicate, so this cannot pass by every field being empty
        """A verdict from zero observations is not a verdict."""
        gap = foldcompose.base_gap(self.repo, self.base)   # no remote at all
        self.assertIs(gap["has_remote"], False)
        self.assertIsNone(gap["ref_current"])
        # AND has_remote False is still its own exemption: a repo with no
        # upstream has nothing to have missed, so this must not become a wall.
        self.assertFalse(foldcompose.gap_is_a_lower_bound(gap))

    def test_the_census_is_read_once_and_retained(self):  # noqa: VACUOUS_ASSERTION — the positive is the call COUNT (exactly one) plus has_remote True: a run that never reached the census would fail both
        """A second `git remote` at the record write can FAIL where the first
        succeeded, and that write stamped `unknown: None` beside it
        unconditionally: has_remote UNKNOWN, behind 0, nothing unknown, admit."""
        self.with_remote()
        real, calls = foldcompose._git, {"n": 0}

        def once(where, *args, **kw):
            if args[:1] == ("remote",) and len(args) == 1:
                calls["n"] += 1
                if calls["n"] > 1:
                    return 128, "", "fatal: later failure"
            return real(where, *args, **kw)

        with mock.patch.object(foldcompose, "_git", once):
            gap = foldcompose.base_gap(self.repo, self.base)
        self.assertEqual(calls["n"], 1,
                         "the census was asked twice and the second answer "
                         "could contradict the one that was checked")
        self.assertIs(gap["has_remote"], True)
        self.assertIsNone(gap["unknown"])

    def test_the_disclosure_speaks_the_measurement_that_decided(self):
        """`ref_current_why` was produced and no renderer read it, so the
        refusal explained itself with a FETCH_HEAD timestamp that no longer
        decides anything -- and prescribed `git fetch` whatever the cause."""
        self.with_remote()
        self.git("remote", "set-url", "origin",
                 os.path.join(self.tmp, "gone.git"))
        gap = foldcompose.base_gap(self.repo, self.base)
        text = "\n".join(foldcompose.render_base_gap(gap))
        self.assertIn("could not be asked", text)
        # THE CONTROL: a reachable remote renders the positive measurement
        # rather than falling back to the file's clock. Main is NOT advanced
        # here -- that would make the local and remote-tracking counts DISAGREE
        # and route the render through the divergence branch, which is a
        # different sentence and would prove nothing about this one.
        self.git("remote", "set-url", "origin",
                 os.path.join(self.tmp, "remote.git"))
        ok = "\n".join(foldcompose.render_base_gap(
            foldcompose.base_gap(self.repo, self.base)))
        self.assertIn("matches what its remote publishes", ok)

    def test_a_remote_url_that_is_a_PREFIX_of_another_is_not_ours(self):  # noqa: VACUOUS_ASSERTION — the control is the second half, unconditional and on the same observable: the URL entire must still be read as the observation it is
        """`url in line` matches when the configured URL is a prefix of some
        other remote's -- `.../proj` against `.../proj-fork`."""
        self.with_remote()
        head = self.git("rev-parse", "HEAD")
        ours = os.path.join(self.tmp, "remote.git")
        with open(os.path.join(self.repo, ".git", "FETCH_HEAD"), "w",
                  encoding="utf-8") as fh:
            fh.write("%s\t\tbranch 'main' of %s-fork\n" % (head, ours))
        self.assertIsNone(foldcompose.base_gap(self.repo, self.base)["ref_age_s"])
        # THE CONTROL on the same observable: the URL entire still matches.
        with open(os.path.join(self.repo, ".git", "FETCH_HEAD"), "w",
                  encoding="utf-8") as fh:
            fh.write("%s\t\tbranch 'main' of %s\n" % (head, ours))
        self.assertIsNotNone(
            foldcompose.base_gap(self.repo, self.base)["ref_age_s"])

    def test_an_unequal_sha_does_not_claim_a_DIRECTION(self):
        """UNEQUAL SAYS DIFFERENT, NEVER AHEAD. The remote usually moved
        forward -- but after a REWIND it is BEHIND the counted ref, and the
        same inequality produces the same sentence with the direction
        inverted. Nothing in this reading measures ancestry, so nothing in it
        may name one."""
        bare = self.with_remote()
        was = self.git("rev-parse", "refs/remotes/origin/main")
        # REWIND THE REMOTE BENEATH A TRACKING REF THAT KEEPS THE NEWER SHA.
        # Advance and push so origin/main holds B, then wind the BARE repo's
        # own ref back to A directly -- the remote now publishes something
        # OLDER than what was counted, which is the direction the old sentence
        # got backwards.
        self.advance_main(1)
        self.git("push", "-q", "origin", "main")
        self.git("fetch", "-q", "origin")
        self.git("--git-dir=" + bare, "update-ref", "refs/heads/main", was)
        gap = foldcompose.base_gap(self.repo, self.base)
        self.assertIs(gap["ref_current"], False,
                      "fixture: the counted ref and the remote must DISAGREE "
                      "or this arm is about the agreeing case")
        why = (gap.get("ref_current_why") or "") + "\n".join(
            foldcompose.render_base_gap(gap))
        for banned in ("moved past", "is already behind", "ahead of"):
            self.assertNotIn(banned, why,
                             "an unmeasured direction was asserted: %r" % banned)
        self.assertIn("NOT measured", why)

    def test_the_census_issues_ONE_ls_remote_PER_counted_ref(self):
        """THE COST ORACLE, and it exists because the claim it replaces was
        FALSE: this does NOT group several refs into one advertisement, so an
        origin carrying main and master issues TWO commands, each with its own
        timeout. Pinning the real number is what stops the claim drifting back."""
        self.with_remote()
        self.git("push", "-q", "origin", "main:master")
        self.git("fetch", "-q", "origin")
        real, seen = foldcompose._git, []

        def count(where, *args, **kw):
            if args[:1] == ("ls-remote",):
                seen.append(args)
            return real(where, *args, **kw)

        with mock.patch.object(foldcompose, "_git", count):
            gap = foldcompose.base_gap(self.repo, self.base)
        backed = [r for r, _b in gap["measured_refs"] if "/" in r]
        self.assertEqual(len(seen), len(backed),
                         "one advertisement per remote-backed candidate")
        self.assertGreaterEqual(len(backed), 2,
                                "fixture: two remote-backed candidates or "
                                "this cannot tell grouped from per-ref")
        for args in seen:
            self.assertEqual(len([a for a in args if a.startswith("refs/")]), 1,
                             "a grouped call would carry several refs")

    def test_the_refusal_PRESCRIBES_WHAT_IT_MEASURED(self):  # noqa: VACUOUS_ASSERTION — the unconditional positive is the second half on the SAME room and the SAME argv: restore the remote and the compose must reach rc 0 with HEAD moved, so the absence asserted above cannot be satisfied by a door that refuses everything
        """The stale-zero refusal restated the FETCH_HEAD timestamp as the
        cause and prescribed `git fetch` unconditionally, one line after
        saying the remote could not be ASKED. A fetch cures exactly one of
        this predicate's reasons and is a lie for the rest."""
        self.lane("car-a", [("a.txt", "a")])
        room, path = self.room()
        self.git("commit", "-q", "--allow-empty", "-m", "room is not base",
                 where=path)
        before = self.git("rev-parse", "HEAD", where=path)
        self.with_remote()
        self.git("remote", "set-url", "origin",
                 os.path.join(self.tmp, "gone.git"))
        os.environ["HELM_WORK_INTEGRATOR"] = "1"
        argv = ["--room", room, "--base", self.base, "--car", "row-a:car-a",
                "--repo", self.repo, "--apply"]
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = foldcompose.cmd_compose(list(argv))
        text = err.getvalue()
        self.assertEqual(rc, 2)
        self.assertIn("could not be asked", text)
        self.assertIn("will NOT clear this one", text)
        self.assertNotIn("Run `git fetch` and re-run: the counted ref", text)
        # ...and not the OTHER fetch prescription either: an unaskable remote
        # and a never-fetched one are both None, and only one of them is
        # cured by fetching.
        self.assertNotIn("clears this only if the remote publishes", text)
        self.assertEqual(self.git("rev-parse", "HEAD", where=path), before,
                         "the refusal ran before the destructive half")
        # THE POSITIVE CONTROL, unconditional and on the same room: restore
        # the remote and the SAME argv composes, so this is not a fence.
        self.git("remote", "set-url", "origin",
                 os.path.join(self.tmp, "remote.git"))
        self.assertEqual(foldcompose.cmd_compose(list(argv)), 0)
        self.assertNotEqual(self.git("rev-parse", "HEAD", where=path), before)

    def test_UNKNOWN_has_two_causes_and_only_one_is_beyond_a_fetch(self):  # noqa: VACUOUS_ASSERTION — the absent "will NOT clear this one" gets its unconditional positive at the end of this arm (unreachable remote, same argv); the absent none-asked sentence gets its positive in this arm's own never-fetched half
        """The remedy must not be unconditional in EITHER direction. An
        unconditional `git fetch` was wrong for a remote that cannot be asked;
        an unconditional "a fetch will not help" is wrong for a repo that HAS
        a remote and has simply never fetched it -- there is no
        remote-tracking candidate to count, and the FIRST fetch is what
        creates one. The opposite direction -- an unaskable remote must not
        be told to fetch -- is pinned by
        test_an_unaskable_remote_is_not_cured_by_a_fetch."""
        self.lane("car-a", [("a.txt", "a")])
        room, path = self.room()
        self.git("commit", "-q", "--allow-empty", "-m", "room is not base",
                 where=path)
        before = self.git("rev-parse", "HEAD", where=path)
        os.environ["HELM_WORK_INTEGRATOR"] = "1"
        argv = ["--room", room, "--base", self.base, "--car", "row-a:car-a",
                "--repo", self.repo, "--apply"]

        # A REACHABLE REMOTE THIS CLONE HAS NEVER FETCHED: no origin/* ref
        # resolves, so nothing can be asked -- and fetching is the cure.
        # The seeding push goes to the URL, NOT to a named remote: pushing
        # through `origin` would write refs/remotes/origin/main and hand the
        # census a remote-backed candidate, which is the OTHER world.
        bare = os.path.join(self.tmp, "remote.git")
        self.git("init", "-q", "--bare", bare)
        self.git("push", "-q", bare, "main:refs/heads/main")
        self.assertEqual(self.git("for-each-ref", "refs/remotes"), "",
                         "fixture: a never-fetched clone has no remote-"
                         "tracking ref, or this arm is about something else")
        self.git("remote", "add", "origin", bare)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = foldcompose.cmd_compose(list(argv))
        never = err.getvalue()
        self.assertEqual(rc, 2)
        self.assertIn("clears this only if the remote publishes", never)
        # The reason is a clause and the cure is a sentence; without the
        # supplied stop they render as one run-on and the cure reads as part
        # of the finding.
        self.assertIn("missed news. No counted ref is published", never)

        self.assertNotIn("will NOT clear this one", never)
        self.assertEqual(self.git("rev-parse", "HEAD", where=path), before,
                         "the refusal ran before the destructive half")
        # THE PRESCRIPTION IS THE CONTROL: run exactly the cure the refusal
        # named, change NOTHING else -- not the remote, not the base, not the
        # argv -- and the same command composes. A remedy no arm ever performs
        # is a sentence, not a measurement.
        self.git("fetch", "-q", "origin")
        self.assertEqual(foldcompose.cmd_compose(list(argv)), 0)
        self.assertNotEqual(self.git("rev-parse", "HEAD", where=path), before)
        # THE CONTROL FOR THE ABSENCE ABOVE. A renderer that never emits the
        # unaskable sentence would satisfy assertNotIn just as well, so make
        # the remote unreachable -- the tracking ref now exists, which is what
        # that pole needs -- and watch the SAME door emit it.
        self.git("remote", "set-url", "origin",
                 os.path.join(self.tmp, "gone.git"))
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(foldcompose.cmd_compose(list(argv)), 2)
        self.assertIn("will NOT clear this one", err.getvalue())

    def test_every_reason_the_census_can_answer_with_has_a_remedy(self):
        """A REMEDY MAP IS ONLY AS TOTAL AS THE VOCABULARY IT WAS WRITTEN
        AGAINST. The refusal picks its cure out of CURE_BY_REASON and falls
        back to a generic line for anything unnamed, so a pole the census
        learns to report and the map does not name degrades silently -- the
        same shape as the defect the map replaced. This reads the reasons the
        SHIPPED functions actually return, out of their own source, rather
        than comparing two lists I wrote."""
        emitted = set()
        tree = ast.parse(io.open(foldcompose.__file__,
                                 encoding="utf-8").read())
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef):
                continue
            if fn.name not in ("_remote_head", "_census_current"):
                continue
            for node in ast.walk(fn):
                if not isinstance(node, ast.Return):
                    continue
                if not isinstance(node.value, ast.Tuple):
                    continue
                # The reason is the LAST element; a conditional one (the
                # mixed-kinds expression) carries its literals inside.
                for leaf in ast.walk(node.value.elts[-1]):
                    if isinstance(leaf, ast.Constant) and \
                            isinstance(leaf.value, str):
                        emitted.add(leaf.value)
        self.assertIn("unpublished", emitted,
                      "control: the scan must reach the function bodies, and "
                      "this reason exists only inside one of them")
        self.assertEqual(emitted, set(foldcompose.CURRENCY_REASONS),
                         "the declared vocabulary and the returned one have "
                         "drifted apart")
        # `current` and `local` never reach a refusal, so they earn no cure;
        # everything else must have one, and no cure may name a dead reason.
        self.assertEqual(emitted - {"current", "local"},
                         set(foldcompose.CURE_BY_REASON))

    def test_a_fetch_that_succeeds_can_leave_this_census_with_nothing(self):
        """A REMEDY MAY NOT PROMISE AN OUTCOME IT CANNOT DELIVER, and a
        successful fetch is where that breaks. A fetch creates a tracking ref
        for what the remote PUBLISHES, which is not the same as creating a
        COUNTED one -- this census counts origin/main, main, origin/master
        and master, so a reachable origin publishing only `develop` fetches
        perfectly and leaves the same argv refusing with the same words. The
        remedy states its condition now, and this arm runs the prescription
        to prove the condition is real."""
        self.lane("car-a", [("a.txt", "a")])
        room, path = self.room()
        self.git("commit", "-q", "--allow-empty", "-m", "room is not base",
                 where=path)
        before = self.git("rev-parse", "HEAD", where=path)
        os.environ["HELM_WORK_INTEGRATOR"] = "1"
        argv = ["--room", room, "--base", self.base, "--car", "row-a:car-a",
                "--repo", self.repo, "--apply"]
        bare = os.path.join(self.tmp, "remote.git")
        self.git("init", "-q", "--bare", bare)
        # PUBLISHED UNDER A NAME NOBODY COUNTS. The push goes to the URL so no
        # tracking ref is written by it; the remote is reachable and healthy.
        self.git("push", "-q", bare, "main:refs/heads/develop")
        self.git("remote", "add", "origin", bare)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(foldcompose.cmd_compose(list(argv)), 2)
        first = err.getvalue()
        self.assertIn("clears this only if the remote publishes", first)
        # AND THE REMEDY NAMES WHAT IS COUNTED, because a reader told to fetch
        # without being told that cannot see this coming.
        for cand in foldcompose.TRUNK_CANDIDATES:
            self.assertIn(cand, first)
        # RUN THE PRESCRIPTION. It succeeds, it creates origin/develop, and it
        # changes NOTHING here -- which is the whole finding.
        self.git("fetch", "-q", "origin")
        self.assertTrue(self.git("for-each-ref", "refs/remotes/origin"),
                        "fixture: the fetch created no tracking ref at all, "
                        "so this arm is not about a successful fetch")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(foldcompose.cmd_compose(list(argv)), 2)
        self.assertIn("clears this only if the remote publishes",
                      err.getvalue())
        self.assertEqual(self.git("rev-parse", "HEAD", where=path), before)
        # THE CONDITION, MET: publish a COUNTED branch on the SAME remote,
        # fetch again, and the same argv composes. So the sentence describes a
        # real condition rather than refusing whatever happens.
        self.git("push", "-q", bare, "main:refs/heads/main")
        self.git("fetch", "-q", "origin")
        self.assertEqual(foldcompose.cmd_compose(list(argv)), 0)
        self.assertNotEqual(self.git("rev-parse", "HEAD", where=path), before)

    def test_two_reasons_in_one_census_report_as_mixed(self):
        """`mixed` MAY NOT PROMISE A PARTIAL REPAIR: its members are
        unaskable and unpublished, and a fetch repairs neither. `moved` --
        the one fetch-repairable reason -- returns before mixed can form, so
        no fetch-repairable reason is ever in it.

        The candidate list is the argument `base_gap` passes, so this drives
        the SHIPPED census and the SHIPPED transport for both poles: one
        reachable remote that does not publish the branch, one configured
        remote that cannot be asked."""
        bare = self.with_remote()
        sha = self.git("rev-parse", "refs/remotes/origin/main")
        self.git("remote", "add", "gone",
                 os.path.join(self.tmp, "gone.git"))
        self.git("update-ref", "-d", "refs/heads/main", where=bare)
        current, why, reason = foldcompose._census_current(
            self.repo, [{"ref": "origin/main", "sha": sha},
                        {"ref": "gone/main", "sha": sha}])
        self.assertIsNone(current)
        self.assertEqual(reason, "mixed")
        self.assertIn("does not publish", why)
        self.assertIn("could not be asked", why)
        self.assertNotIn("clears only part",
                         foldcompose.CURE_BY_REASON["mixed"],
                         "the mixed remedy promises a partial repair its own "
                         "members cannot deliver")
        # THE CONTROL, unconditional and through the same call: restore the
        # branch and ask only the remote that can answer, and the census
        # returns current -- so `mixed` above is the disagreement and not the
        # shape of every answer.
        self.git("update-ref", "refs/heads/main", sha, where=bare)
        self.assertEqual(
            foldcompose._census_current(
                self.repo, [{"ref": "origin/main", "sha": sha}]),
            (True, foldcompose._census_current(
                self.repo, [{"ref": "origin/main", "sha": sha}])[1],
             "current"))

    def test_a_remote_that_answered_is_not_a_remote_that_could_not_be_asked(self):  # noqa: VACUOUS_ASSERTION — the absent "could not be asked at all" gets its unconditional positive at the end of this arm; the absent none-asked sentence is emitted by test_UNKNOWN_has_two_causes_and_only_one_is_beyond_a_fetch, which asserts it present on the same door
        """r5 P3: `does not publish` and `could not be asked` are
        both None and are NOT the same world. The advertisement ARRIVED for
        the first one -- there is nothing unreachable about it, no retry when
        the network returns will change it, and telling the composer the
        remote could not be asked sends them to debug a connection that
        works."""
        self.lane("car-a", [("a.txt", "a")])
        room, path = self.room()
        self.git("commit", "-q", "--allow-empty", "-m", "room is not base",
                 where=path)
        before = self.git("rev-parse", "HEAD", where=path)
        bare = self.with_remote()
        # THE REMOTE STAYS REACHABLE AND LOSES THE BRANCH. `ls-remote`
        # succeeds and advertises nothing for main, which is the pole that
        # rc != 0 cannot reach.
        self.git("update-ref", "-d", "refs/heads/main", where=bare)
        gap = foldcompose.base_gap(self.repo, self.base)
        self.assertIsNone(gap["ref_current"])
        self.assertEqual(gap["ref_current_reason"], "unpublished")
        os.environ["HELM_WORK_INTEGRATOR"] = "1"
        argv = ["--room", room, "--base", self.base, "--car", "row-a:car-a",
                "--repo", self.repo, "--apply"]
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = foldcompose.cmd_compose(list(argv))
        text = err.getvalue()
        self.assertEqual(rc, 2)
        self.assertIn("ANSWERED and does not publish", text)
        self.assertNotIn("could not be asked at all", text)
        self.assertNotIn("clears this only if the remote publishes", text)
        self.assertEqual(self.git("rev-parse", "HEAD", where=path), before)
        # THE POSITIVE CONTROL: restore the branch on the same remote and the
        # same argv composes, so this refusal is about the advertisement and
        # not about the room, the base or the car.
        self.git("update-ref", "refs/heads/main",
                 self.git("rev-parse", "refs/remotes/origin/main"), where=bare)
        self.assertEqual(foldcompose.cmd_compose(list(argv)), 0)
        self.assertNotEqual(self.git("rev-parse", "HEAD", where=path), before)
        # THE CONTROL FOR THE ABSENCE ABOVE: the unreachable sentence is one
        # this door really does emit, for the pole that really is unreachable.
        self.git("remote", "set-url", "origin",
                 os.path.join(self.tmp, "gone.git"))
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(foldcompose.cmd_compose(list(argv)), 2)
        self.assertIn("could not be asked at all", err.getvalue())

    def test_a_fresh_observation_of_a_stale_ref_does_not_clear_the_default(self):
        """The falsifier for the predicate, stated as an arm: a FRESH
        MISMATCH must not clear the default. The old test was `age <=
        FETCH_STALE_S`, and a just-written FETCH_HEAD satisfied it while the
        ref it named had already been left behind."""
        bare = self.with_remote()
        was = self.git("rev-parse", "refs/remotes/origin/main")
        self.advance_remote()
        # A brand-new, correctly-attributed observation of the counted branch.
        with open(os.path.join(self.repo, ".git", "FETCH_HEAD"), "w",
                  encoding="utf-8") as fh:
            fh.write("%s\t\tbranch 'main' of %s\n" % (was, bare))
        gap = foldcompose.base_gap(self.repo, self.base)
        self.assertIsNotNone(gap["ref_age_s"])
        self.assertLess(gap["ref_age_s"], foldcompose.FETCH_STALE_S,
                        "fixture: the observation must be FRESH or this arm "
                        "is about staleness instead of about currency")
        self.assertTrue(foldcompose.gap_is_a_lower_bound(gap))

    def test_a_remote_that_cannot_be_asked_is_not_a_current_one(self):
        """A refusal must never read as `current`. Offline is the ordinary
        case here, and the honest consequence is the acknowledgement flow, not
        a clean zero."""
        self.with_remote()
        control = foldcompose.base_gap(self.repo, self.base)
        self.assertIs(control["ref_current"], True)     # the unconditional half
        self.git("remote", "set-url", "origin",
                 os.path.join(self.tmp, "no-such-remote.git"))
        gap = foldcompose.base_gap(self.repo, self.base)
        self.assertIsNone(gap["ref_current"])
        self.assertIn("could not be asked", gap["ref_current_why"])
        self.assertTrue(foldcompose.gap_is_a_lower_bound(gap))

    def test_an_unreadable_remote_list_is_an_incomplete_census(self):
        """task/2484 r2 R2: the producer learned the third answer and the
        consumer did not. `_has_remote` returns None when `git remote` FAILS,
        and the only reader tested `is False` -- so None fell through as a
        successful census with a clean zero and no acknowledgement."""
        real = foldcompose._git

        def fail_remote(where, *args, **kw):
            if args[:1] == ("remote",) and len(args) == 1:
                return 128, "", "fatal: not a git repository"
            return real(where, *args, **kw)

        with mock.patch.object(foldcompose, "_git", fail_remote):
            gap = foldcompose.base_gap(self.repo, self.base)
        self.assertIsNotNone(gap["unknown"])
        self.assertIn("`git remote` could not be read", gap["unknown"])
        self.assertIsNone(gap["behind"])
        # THE CONTROL: the same call with the same repo answers when the
        # command works, so this refusal is about the failure and not a fence.
        self.assertEqual(foldcompose.base_gap(self.repo, self.base)["behind"], 0)

    def test_another_remotes_branch_is_not_an_observation_of_ours(self):
        """Two remotes both publish `main`. Matching the branch name alone
        credited a successful fetch of SOMEONE ELSE'S project as a record that
        this clone had asked ours."""
        self.with_remote()
        head = self.git("rev-parse", "HEAD")
        with open(os.path.join(self.repo, ".git", "FETCH_HEAD"), "w",
                  encoding="utf-8") as fh:
            fh.write("%s\t\tbranch 'main' of git@example.com:someone/else\n"
                     % head)
        gap = foldcompose.base_gap(self.repo, self.base)
        self.assertIsNone(gap["ref_age_s"],
                          "a fetch of another remote's main was read as an "
                          "observation of ours")
        # THE UNCONDITIONAL CONTROL ON THE SAME OBSERVABLE. Without it this
        # arm is satisfied by a parser that credits NOTHING -- an absence
        # asserted against a reader that can no longer read at all.
        with open(os.path.join(self.repo, ".git", "FETCH_HEAD"), "w",
                  encoding="utf-8") as fh:
            fh.write("%s\t\tbranch 'main' of %s\n"
                     % (head, os.path.join(self.tmp, "remote.git")))
        ours = foldcompose.base_gap(self.repo, self.base)
        self.assertIsNotNone(ours["ref_age_s"],
                             "the SAME line naming THIS remote must still be "
                             "read as the observation it is")
        self.assertTrue(ours["ref_observed_same"])

    def test_an_unrelated_fetch_does_not_make_the_counted_ref_look_fresh(self):
        """R1. A global FETCH_HEAD mtime is written by ANY fetch, so a fetch of
        an unrelated branch would erase the staleness qualification over a
        trunk ref nobody has asked about. Provenance is the CONTENT of
        FETCH_HEAD, not its timestamp."""
        self.advance_main(1)
        # THE REPO NEEDS SOMEWHERE TO HAVE FETCHED FROM, or the staleness
        # qualification correctly does not apply at all and this arm would
        # assert the absence of a line that was never going to be printed.
        # AND IT NEEDS origin/main TO EXIST: the census picks the candidate
        # with the largest gap, and with no fetch only the LOCAL `main`
        # resolves -- which no remote publishes, so no FETCH_HEAD line could
        # ever be an observation of it and both halves below would read None
        # for a reason that has nothing to do with what this arm is about.
        self.with_remote()
        head = self.git("rev-parse", "HEAD")
        fetch_head = os.path.join(self.repo, ".git", "FETCH_HEAD")
        with open(fetch_head, "w", encoding="utf-8") as fh:
            fh.write("%s\t\tbranch 'some-topic' of git@example.com:x/y\n" % head)
        gap = foldcompose.base_gap(self.repo, self.base)
        self.assertIsNone(gap["ref_age_s"],
                          "a fetch of another branch was read as an "
                          "observation of the counted one")
        # THE RENDER SAYS UNOBSERVED ONLY WHERE IT MATTERS, and that is not
        # here any more. Currency is now MEASURED by asking the remote, so
        # while the remote is reachable this ref is provably current and no
        # amount of missing FETCH_HEAD provenance makes it a lower bound --
        # which is the whole point of the r2 cure. The disclosure sentence
        # belongs to the case where the question could NOT be put, so that is
        # the state this half arranges.
        self.git("remote", "set-url", "origin",
                 os.path.join(self.tmp, "gone.git"))
        offline = foldcompose.base_gap(self.repo, self.base)
        rendered = "\n".join(foldcompose.render_base_gap(offline))
        self.assertIn("could not be checked against a remote", rendered)
        self.assertEqual(rendered.count("LOWER BOUND"), 1,
                         "the caveat is rendered once, after the evidence")
        self.assertIn("LOWER BOUND",
                      "\n".join(foldcompose.render_base_gap(offline)),
                      "a BEHIND count needs the caveat more than a zero does: "
                      "a reader told '1 behind' rebases over one commit and "
                      "believes they are level")
        self.git("remote", "set-url", "origin",
                 os.path.join(self.tmp, "remote.git"))
        # THE CONTROL: the same file naming the COUNTED branch IS provenance
        # -- and it must name THIS remote's URL, because an observation is now
        # attributed to the remote it came from and not to a branch name that
        # every project shares.
        with open(fetch_head, "w", encoding="utf-8") as fh:
            fh.write("%s\t\tbranch 'main' of %s\n"
                     % (head, os.path.join(self.tmp, "remote.git")))
        seen = foldcompose.base_gap(self.repo, self.base)
        self.assertIsNotNone(seen["ref_age_s"])
        self.assertTrue(seen["ref_observed_same"],
                        "the observed sha equals the counted one and was not "
                        "reported as matching")

    def test_an_unreadable_ref_is_UNKNOWN_and_never_skipped_as_absent(self):
        """R2. `rev-parse --verify --quiet` exits 1 for a genuinely missing
        ref, which a census may skip, and exits otherwise for a repository it
        could not read — which must not be flattened into the same answer."""
        self.advance_main(2)
        real = foldcompose._git

        def fail_main(where, *args):
            if args[:1] == ("rev-parse",) and args[-1].startswith("main"):
                return 128, "", "fatal: unable to read"
            return real(where, *args)

        with mock.patch.object(foldcompose, "_git", side_effect=fail_main):
            gap = foldcompose.base_gap(self.repo, self.base)
        self.assertIsNone(gap["behind"])
        self.assertIn("not proof that a ref is absent", gap["unknown"])
        # THE CONTROL, unconditional and on the same observable: with nothing
        # failing, the same census answers 2 rather than UNKNOWN.
        self.assertEqual(foldcompose.base_gap(self.repo, self.base)["behind"], 2)

    def test_a_failed_count_does_not_shrink_the_census_to_a_smaller_answer(self):
        """R2, the other manifestation of the same root: dropping a resolved
        ref whose COUNT cannot be read makes the ref carrying the gap
        disappear while `unknown` stays None — a smaller census wearing the
        shape of a complete one."""
        self.advance_main(3)
        real = foldcompose._git

        def fail_count(where, *args):
            if args[:2] == ("rev-list", "--count"):
                return 1, "", "fatal: bad revision"
            return real(where, *args)

        with mock.patch.object(foldcompose, "_git", side_effect=fail_count):
            gap = foldcompose.base_gap(self.repo, self.base)
        self.assertIsNone(gap["behind"])
        self.assertIn("partly failed census", gap["unknown"])
        self.assertEqual(foldcompose.base_gap(self.repo, self.base)["behind"], 3)

    def test_the_actuator_consumes_the_proposal_that_was_checked(self):  # noqa: VACUOUS_ASSERTION — the patched `plan` raising AssertionError is the unconditional control: it runs on every execution of this arm and fails loudly if compose re-plans, so the equality below is read only when the checked proposal was the one consumed
        """R3. A preflight that gates on one plan while the actuator re-plans
        has two measurements: the one it refused on and the one it acted on."""
        lane = self.lane("car-a", [("a.txt", "a")])
        room, path = self.room()
        checked, err = foldcompose.plan(
            self.repo, room, self.base,
            [{"row": "r1", "lane": lane.split("lane/", 1)[1]}])
        self.assertIsNone(err)
        self.advance_main(1)                 # the world moves after the check
        with mock.patch.object(foldcompose, "plan",
                               side_effect=AssertionError(
                                   "compose re-planned behind the check")):
            record, err = foldcompose.compose(
                self.repo, room, self.base,
                [{"row": "r1", "lane": lane.split("lane/", 1)[1]}],
                proposal=checked)
        self.assertIsNone(err, err)
        self.assertEqual(record["base_gap"]["behind"], 0,
                         "the record carries the SECOND plan's gap, so the "
                         "checked snapshot was not the one that ran")

    def test_a_manifest_without_the_field_reads_as_unmeasured_not_as_zero(self):
        """R4. Absence of a record is not a record of absence, and an old
        manifest must not render as a base that was current."""
        said = "\n".join(foldcompose.render_stored_gap({"result_tip": "x"}))
        self.assertIn("UNMEASURED", said)
        self.assertNotIn("none —", said)
        # THE CONTROL: a stored gap renders as what it recorded, with the ack.
        stored = foldcompose.render_stored_gap(
            {"base_gap": {"behind": 8, "trunk": "main", "measured_ts": 1},
             "base_behind_acknowledged": True})
        self.assertIn("8 commit(s) on main", "\n".join(stored))
        self.assertIn("ACKNOWLEDGED", "\n".join(stored))
