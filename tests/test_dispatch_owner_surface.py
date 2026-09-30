#!/usr/bin/env python3
"""The owner-surface GUARD on `helm dispatch send`.

When a `--kind review` ref touches `helm/web_ui/`, the send refuses unless the
brief carries BEFORE: / AFTER: screenshot paths that EXIST on disk and at
least one IA: line naming what the new element replaces. `--no-owner-surface-
because REASON` is the escape, recorded on the row, exactly like
`--read-only-because` (shape in dispatches.py:215/247).

These are the RED arms: the guard does not exist yet, so every refusal
asserts against the send that currently writes a row. The green implementation
lands on top of this same module.
"""
import os
import unittest
import unittest.mock as mock

from helm import dispatches
from tests.test_dispatches import DispatchBase  # noqa: F401 — re-exports the env pin


class OwnerSurfaceGuardTest(DispatchBase):
    """A review tip that moves the owner surface owes a proof, or an escape."""

    def _origin(self):
        """Give the fixture the REMOTE-TRACKING ref the guard measures against,
        pointing at the pre-tip tip of `main` (the branch point), exactly as the
        base class does for landedness arms: the guard's `diff merge-base(ref,
        origin/main)..ref` then names the lane's own work, not the trunk it grew
        from."""
        self.git("update-ref", "refs/remotes/origin/main", self.main)

    def _ensure_dir(self, dirname):
        """Create a nested dir on disk. The fixture repo is a bare template
        (it plants only `helm/__init__.py`, never the real tree), so a
        `commit_file` of a nested path needs its parent dir to exist. The guard
        only ever checks the path by prefix, so this is all the fixture owes
        for a real commit under `helm/web_ui/`."""
        os.makedirs(os.path.join(self.repo, dirname), exist_ok=True)

    def _tip(self):
        """A tip whose diff from origin/main touches helm/web_ui/ only.

        It lives on a LANE branch cut from the pre-tip trunk tip: the tip commit
        is a descendant of `main` (so it is the lane's own commit, ff-able), but
        `main` itself is still at the cut point, so merge-base(tip, origin/main)
        is that cut point and the diff names only the lane's web_ui work. Committing
        on `main` would advance it to the tip, and the diff would be empty — the
        very failure this branch shape avoids."""
        self._origin()
        self._ensure_dir("helm/web_ui/views")
        self.git("checkout", "-q", "-b", "lane/guard", self.main)
        tip = self.commit_file("helm/web_ui/views/00-guard.html.part",
                               "guard tip\n")
        self.git("checkout", "-q", self.main)
        return tip

    def _ref_non_web(self):
        """A tip whose diff touches helm/web_ui/ not at all (same branch shape,
        a non-surface file), so the guard has nothing to refuse."""
        self._origin()
        self.git("checkout", "-q", "-b", "lane/guard", self.main)
        tip = self.commit_file("state", "non-web tip\n")
        self.git("checkout", "-q", self.main)
        return tip

    def _brief(self):
        """A proof-FULL brief: BEFORE/AFTER/IA lines, all three present, with
        the screenshot paths that the passing arms create on disk."""
        return ("Review the board changes. "
                "BEFORE: /tmp/shots/before-420.png "
                "AFTER: /tmp/shots/after-1440.png "
                "IA: new row replaces the old home tile\n")

    def _brief_no_proof(self):
        """A proof-LESS brief: a plain review note with none of the three named
        lines, so the door refuses on 'missing lines' — the shape the refusing
        arms assert against."""
        return "Review the board changes for the lane tip.\n"

    def _make(self, kind="review", tip=None, message=None,
             because=None, **kw):
        # A 1-16000-char non-empty brief, or the send dies on the message
        # check BEFORE the guard ever fires and every refusal would be
        # "message must be 1-16000 characters" rather than the door. The
        # guard arms only for the arms that pass a real message; the others
        # pass one that is deliberately proof-less so the door, not the
        # message gate, is the thing under test.
        body = message if message is not None else self._brief_no_proof()
        row, why, posted = dispatches.send(
            "seat-a", "lane-g", body, tip or self.a,
            repo=self.repo, kind=kind, new_work=True,
            owner_surface_because=because, **kw)
        return row, why, posted

    def _make_norepo(self, tip, message, kind="review",
                     because=None, **kw):
        """The NORMAL path: the CLI and every caller that never names --repo.
        That is, send() with repo=None, run from INSIDE the fixture repo so
        that _repo_info() (which reads os.getcwd()) resolves the repository the
        ref lives in. This is the normal path — the guard must fire on it, not
        just on the repo-explicit arms. The guard was passed a bare None and
        read the diff as absent."""
        old_cwd = os.getcwd()
        os.chdir(self.repo)
        try:
            row, why, posted = dispatches.send(
                "seat-a", "lane-g", message, tip,
                kind=kind, new_work=True,
                owner_surface_because=because, **kw)
            return row, why, posted
        finally:
            os.chdir(old_cwd)

    def _ledger(self):
        return {r["id"] for r in dispatches.rows()}

    def _brief_of(self, row_id):
        return dispatches.rows()[row_id]["message_body"]

    # VACUOUS_ASSERTION: the whole test IS the assertion that no row is minted
    # — a refusal; its positive control (a row minted on a passing send) lives
    # in RED 2, not here.
    def test_a_review_send_with_web_ui_tip_and_no_proof_or_ia_IS_REFUSED(self):
        """RED 1: no BEFORE/AFTER/IA -> refused, nothing written, the refusal
        names the missing lines and the escape flag."""
        tip = self._tip()
        row, why, posted = self._make(kind="review", tip=tip)
        self.assertIsNone(row, "send must refuse a review tip touching "
                               "helm/web_ui/ with no proof")
        self.assertFalse(posted, "a refused send writes nothing")
        self.assertEqual(len(self._ledger()), 0, "no row minted on refusal")
        # The refusal must name WHAT is missing and the escape.
        for missing in ("BEFORE:", "AFTER:", "IA:"):
            self.assertIn(missing, why, "refusal must name %s" % missing)
        self.assertIn("--no-owner-surface-because", why,
                      "refusal must name the escape flag")
        # A build of the same tip is out of scope (RED 6 below).

    def test_a_review_send_with_NO_REPO_arg_from_the_fixture_repo_IS_REFUSED(self):
        """F1: the guard must fire on the NORMAL path — a `send` with no
        `repo=` (what the CLI and every --repo-less caller do), run from inside
        the fixture repo, whose ref touches `helm/web_ui/` and whose brief has
        no proof. The earlier arms all passed repo=self.repo explicitly, so a
        bare None slipped through untouched; this arm calls the real path."""
        tip = self._tip()
        row, why, posted = self._make_norepo(tip, self._brief_no_proof(),
                                             kind="review")
        self.assertIsNone(row, "a repo-less review send of a web_ui tip "
                               "must refuse when proof is missing")
        self.assertFalse(posted, "a refused send writes nothing")
        self.assertEqual(len(self._ledger()), 0, "no row minted on refusal")
        for missing in ("BEFORE:", "AFTER:", "IA:"):
            self.assertIn(missing, why, "refusal must name %s" % missing)
        self.assertIn("--no-owner-surface-because", why,
                      "refusal must name the escape flag")

    def _shot(self, name):
        """Create a screenshot file on disk so the named path EXISTS (the
        proof is 'the path exists', not its pixels — the guard only checks
        os.path.exists on the two viewport paths the brief names)."""
        path = os.path.join(self.tmp, "shots", name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("png\n")
        return path

    def _shot_rel(self, rel):
        """Create a screenshot under the FIXTURE REPO root, named by a RELATIVE
        path (F3). The guard resolves relative paths against the repo root
        (`self.repo`), so the file must live there, and the brief names it
        without an absolute prefix."""
        path = os.path.join(self.repo, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("png\n")
        return rel

    def test_a_valid_before_after_ia_and_web_ui_tip_PASSES_and_stands(self):
        """RED 2: valid screenshot paths that EXIST + IA -> passes, and the
        row carries ONE standing line pointing the reader at the premise."""
        # THE PROOF IS DISK: name the viewport paths in a brief, then make
        # them exist. The guard refuses a named path that is not on disk.
        before = self._shot("before-420.png")
        after = self._shot("after-1440.png")
        tip = self._tip()
        # EACH PROOF LINE ON ITS OWN LINE — the guard reads lines that START
        # with the marker, so a single-line body would trip it.
        body = ("Review the board changes.\n"
                "BEFORE: %s\n"
                "AFTER: %s\n"
                "IA: new row replaces the old home tile\n"
                % (before, after))
        row, why, posted = self._make(kind="review", tip=tip, message=body)
        self.assertIsNotNone(row)
        self.assertIsNone(why)
        self.assertTrue(posted, "valid proof must publish")
        # THE STANDING LINE, one line, pointing the reader at the premise and
        # at the fix:
        #   "judge this lane against owner-ia-guidelines-no-duplicate-ux-mirrors-
        #    ax-overview-home; any second home it gives something, FIX it."
        body = self._brief_of(row["id"])
        self.assertIn("owner-ia-guidelines-no-duplicate-ux-mirrors-ax-overview-home",
                      body)
        self.assertIn("FIX", body)

    def test_SEVERAL_paths_on_one_proof_line_each_count(self):
        """A proof line may name several screenshots (the 420 and 1440 views
        side by side), separated by spaces or commas: each path must exist,
        one that does not is named, and all existing passes."""
        b420, b1440 = self._shot("before-420.png"), self._shot("before-1440.png")
        a420 = self._shot("after-420.png")
        gone = os.path.join(os.path.dirname(a420), "after-1440-gone.png")
        tip = self._tip()
        row, why, _posted = self._make(
            kind="review", tip=tip,
            message=("Review.\nBEFORE: %s %s\nAFTER: %s, %s\n"
                     "IA: replaces the old tile\n" % (b420, b1440, a420, gone)))
        self.assertIsNone(row)
        self.assertIn("after-1440-gone.png", why)
        self.assertNotIn(b1440, why)
        a1440 = self._shot("after-1440.png")
        row, why, _posted = self._make(
            kind="review", tip=tip,
            message=("Review.\nBEFORE: %s %s\nAFTER: %s, %s\n"
                     "IA: replaces the old tile\n" % (b420, b1440, a420, a1440)))
        self.assertIsNone(why)
        self.assertIsNotNone(row)

    # VACUOUS_ASSERTION: the whole test IS the assertion that a missing path
    # mints no row; its positive control (a row minted when the path exists)
    # lives in RED 2, not here.
    def test_a_EMPTY_BEFORE_path_is_REFUSED(self):
        """F2: a bare `BEFORE:` line names an EMPTY path. An empty path is a
        MISSING path — the sender meant to point at a shot but left it blank.
        The earlier `if path and not os.path.exists(path)` waved an empty path
        through; now it is refused, named in the refusal (as the BEFORE line)."""
        tip = self._tip()
        after = self._shot("after.png")
        # BEFORE is bare (nothing after the colon); AFTER exists; IA present.
        msg = ("Review.\n"
               "BEFORE:\n"
               "AFTER: %s\n"
               "IA: new nav replaces the old header\n" % after)
        row, why, posted = self._make(kind="review", tip=tip, message=msg)
        self.assertIsNone(row, "a blank BEFORE path must refuse — the path is "
                               "missing")
        self.assertFalse(posted)
        self.assertEqual(len(self._ledger()), 0)
        # Named as the missing path (the BEFORE line), not as a missing line.
        self.assertNotIn("missing lines", why)
        self.assertIn("BEFORE", why, "the empty BEFORE path must be named")
        self.assertIn("screenshot paths not on disk", why)

    def test_a_RELATIVE_before_path_resolves_against_the_repo(self):
        """F3: a RELATIVE BEFORE path resolves against the repository, not the
        sender's cwd. The sender names a shot relative to the repo root and
        that shot exists on disk there — the path-exists check must find it.
        The inverse (a relative path not on disk) refuses, so both halves of the
        seam are pinned."""
        tip = self._tip()
        # A relative shot under the repo root, existing on disk:
        before = self._shot_rel("shots/before-rel.png")
        after = self._shot("after-rel.png")
        msg = ("Review.\n"
               "BEFORE: %s\n"
               "AFTER: %s\n"
               "IA: new nav replaces the old header\n" % (before, after))
        # Existing relative path -> the door passes.
        row, why, posted = self._make(kind="review", tip=tip, message=msg)
        self.assertIsNotNone(row, "a relative BEFORE path that exists on disk "
                                  "in the repo must pass")
        self.assertIsNone(why)
        self.assertTrue(posted)
        # Relative path that does NOT exist -> the door refuses, naming it.
        msg = ("Review.\n"
               "BEFORE: shots/does-not-exist.png\n"
               "AFTER: %s\n"
               "IA: new nav replaces the old header\n" % after)
        row, why, posted = self._make(kind="review", tip=tip, message=msg)
        self.assertIsNone(row, "a relative BEFORE path not on disk must refuse")
        self.assertFalse(posted)
        self.assertIn("does-not-exist.png", why)
        self.assertIn("screenshot paths not on disk", why)

    def test_a_NONEXISTENT_BEFORE_path_is_REFUSED(self):
        """RED 3: BEFORE names a path not on disk -> refused, and the refusal
        names the MISSING PATH (not a missing line). All three lines present;
        only the BEFORE path is absent from disk."""
        tip = self._tip()
        # AFTER path exists (created on disk); BEFORE points at a file that does
        # not, so the path-exists check — not the line check — is the door.
        after = self._shot("after.png")
        msg = ("Review.\n"
               "BEFORE: /tmp/shots/MISSING.png\n"
               "AFTER: %s\n"
               "IA: new nav replaces the old header\n" % after)
        row, why, posted = self._make(kind="review", tip=tip, message=msg)
        self.assertIsNone(row)
        self.assertFalse(posted)
        self.assertEqual(len(self._ledger()), 0)
        # The door must NAME the missing path, not a missing line.
        self.assertNotIn("missing lines", why)
        self.assertIn("MISSING.png", why)
        self.assertIn("screenshot paths not on disk", why)

    def test_a_REF_not_touching_web_ui_is_UNTOUCHED(self):
        """RED 4: a non-web_ui review tip passes untouched (no proof, no
        escape, no standing line)."""
        tip = self._ref_non_web()
        msg = "plain review of non-web work\n"
        row, why, posted = self._make(kind="review", tip=tip, message=msg)
        self.assertIsNotNone(row)
        self.assertIsNone(why)
        self.assertTrue(posted)
        # No standing line, because nothing owed it.
        self.assertNotIn("owner-ia-guidelines", self._brief_of(row["id"]))

    def test_the_escape_flag_records_and_passes(self):
        """RED 5a: --no-owner-surface-because "pure refactor" -> passes and the
        reason rides the row (recorded escape)."""
        tip = self._tip()
        row, why, posted = self._make(kind="review", tip=tip,
                                      because="pure refactor")
        self.assertIsNotNone(row)
        self.assertIsNone(why)
        self.assertTrue(posted)
        # The row RECORDS the escape, exactly like read_only_because.
        self.assertIn("owner_surface_because", dispatches.rows()[row["id"]])
        self.assertEqual("pure refactor",
                         dispatches.rows()[row["id"]]["owner_surface_because"])

    # VACUOUS_ASSERTION: the whole test IS the assertion that a broken escape
    # mints no row; its positive control (a row minted for a valid escape)
    # lives in RED 5a, not here.
    def test_an_EMPTY_or_TOO_LONG_escape_is_REFUSED(self):
        """F4/RED 5b: empty reason or reason over 256 chars -> refused on its
        own, NAMING the broken escape. The earlier code fell through a broken
        escape to the proof check, so the refusal named the missing proof, not
        the escape that was the actual problem. A broken escape must refuse
        HERE — even when the proof is already present (below) — and the refusal
        must name WHY the escape is broken."""
        tip = self._tip()
        # Empty escape.
        row, why, posted = self._make(kind="review", tip=tip, because="")
        self.assertIsNone(row, "an empty escape refuses on its own")
        self.assertFalse(posted)
        self.assertEqual(len(self._ledger()), 0)
        self.assertIn("escape", why.lower(),
                      "the empty-escape refusal must name the escape")
        # Over-cap escape.
        row, why, posted = self._make(kind="review", tip=tip,
                                      because="x" * 300)
        self.assertIsNone(row, "an over-256 escape refuses on its own")
        self.assertFalse(posted)
        self.assertEqual(len(self._ledger()), 0)
        self.assertIn("escape", why.lower(),
                      "the over-cap escape refusal must name the escape")
        self.assertIn("256", why, "the over-cap refusal names the cap")
        # F4: a broken escape refuses even when a FULL proof is present — it
        # cannot double as that proof.
        before = self._shot("before-420.png")
        after = self._shot("after-1440.png")
        body = ("Review the board changes.\n"
                "BEFORE: %s\n"
                "AFTER: %s\n"
                "IA: new row replaces the old home tile\n"
                % (before, after))
        row, why, posted = self._make(kind="review", tip=tip, message=body,
                                      because="x" * 300)
        self.assertIsNone(row, "a broken escape refuses even with full proof")
        self.assertFalse(posted)
        self.assertIn("escape", why.lower(),
                      "proof does not excuse a broken escape")

    def test_a_build_send_is_UNTOUCHED_by_the_guard(self):
        """RED 6: --kind build does not owe a web-ui proof, even if the tip
        touches helm/web_ui/."""
        tip = self._tip()
        row, why, posted = self._make(kind="build", tip=tip)
        self.assertIsNotNone(row)
        self.assertIsNone(why)
        self.assertTrue(posted)
        self.assertNotIn("owner-ia-guidelines",
                         self._brief_of(row["id"]))

    # VACUOUS_ASSERTION: the whole test IS the assertion that a proof-less
    # web_ui review send refuses; its positive control (a passing send) lives
    # in RED 2. The three new refusal FACTS the owner-surface door must now
    # speak to a sender are what the GREEN cure adds. Each asserts a phrase the
    # door does not yet carry: fact 1 "only checks that each" (the path check
    # is existence, never a pixel read), fact 2 "never open" + "Claude seat"
    # (the text-only escape path: post your tip, a Claude seat captures the
    # shots, you send the paths it gives you), and fact 3 "cannot see" (the
    # escape is for a change the owner cannot SEE on the page — a refactor,
    # comment or test — not for a change to what the owner reads).
    def test_a_proof_less_web_ui_review_refusal_names_the_three_facts(self):
        """RED 7: a proof-less web_ui review send must refuse AND name the three
        facts, in plain words, so the next sender sees what the guard is and
        what the escape is really for."""
        tip = self._tip()
        row, why, posted = self._make(kind="review", tip=tip)
        self.assertIsNone(row, "a proof-less review tip must refuse")
        self.assertFalse(posted)
        self.assertEqual(len(self._ledger()), 0)
        # fact 1: the path check is existence only, never an open of the image.
        self.assertIn("only checks that each", why,
                      "refusal must say the guard only checks that each "
                      "BEFORE/AFTER path exists, and never opens the image")
        # fact 2: the text-only escape — post the tip, a Claude seat captures
        # the two views, the sender sends the paths it is given.
        self.assertIn("never open", why,
                      "refusal must say it never opens the image")
        self.assertIn("Claude seat", why,
                      "refusal must say a Claude seat captures the shots")
        # fact 3: the escape is for a change the owner cannot see.
        self.assertIn("cannot see", why,
                      "refusal must say the escape is for a change the owner "
                      "cannot see")
        # the joined string pieces must not leave runs of spaces (the old
        # escape line and its next piece rendered "pass   --no-owner..." with
        # three spaces).
        self.assertNotIn("   ", why,
                         "refusal must not leave runs of three spaces")


if __name__ == "__main__":
    unittest.main()
