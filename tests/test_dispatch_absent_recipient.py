#!/usr/bin/env python3
"""An obligation surface says when its addressee does not exist.

THE DEFECT. A dispatch row names a recipient by roster key. Seats get renamed
and retired; the row keeps the old key. Every listing here rendered such a row
exactly like a live obligation, so a rename orphaned rows SILENTLY — the chat
send door already asks whether an addressee resolves and already prints ABSENT,
and no obligation surface asked at all.

WHAT THIS IS NOT. It is not a burn-down. Measured on the live ledger the day it
was built: of the rows the verb's own `--open` selects, ZERO name an absent
recipient — the ones that do are superseded parents whose successor is alive
and whose obligation moved with it. The value here is legibility on listings
that show historical or cross-direction rows, and a refusal to let a reader
mistake a stale name for a live debt.

AND IT NEVER ADOPTS. A reader seeing their own retired name here will want to
take the row; that is a substitution, and the footer says so in words.
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
_tmp_home(prefix="helm-test-home-", var="HELM_HOME")

from helm import dispatches  # noqa: E402

ENV_KEYS = ("HELM_HOME", "MELD_HOME", "HELM_ADOPTED_DIR", "MELD_ADOPTED_DIR",
            "HELM_CHAT_DIR", "MELD_CHAT_DIR", "HELM_CHAT_NAME",
            "MELD_CHAT_NAME", "HELM_CHAT_NODE_URL", "HELM_CHAT_ROOM")

LIVE = "seat-a"
GONE = "seat-retired"
OTHER = "seat-b"


def run_list(repo, *args):
    """Drive the real verb with the ledger homed where the fixture wrote.

    `dispatch_home` is the same seam the dispatch suite mints through, and
    the comment there says why: the write door refuses a ref whose repository
    is not this project's, so a fixture that does not home itself files its
    rows into whatever repo the runner happens to stand in — which on the fab
    is the LANE, not the tmp tree the arms then read."""
    from tests._tmphome import dispatch_home
    out = io.StringIO()
    with dispatch_home(repo):
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            rc = dispatches.cmd_dispatch(["list"] + list(args))
    return rc, out.getvalue()


class AbsentRecipientBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-absent-")
        self.prior = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_ADOPTED_DIR"] = os.path.join(self.tmp, "adopted")
        os.makedirs(os.environ["HELM_ADOPTED_DIR"])
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "chat")
        os.environ["HELM_CHAT_NODE_URL"] = ""
        os.environ["HELM_CHAT_ROOM"] = "main"
        os.environ["HELM_CHAT_NAME"] = LIVE

    def tearDown(self):
        for k, v in self.prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def reach(self, mapping):
        """Patch the ONE door recipient_reach asks, not recipient_reach itself.

        Patching the reach map would let every arm below pass over a function
        that never ran. This replaces the roster question and leaves the
        mapping, the caching and the unknown-word handling under test."""
        from helm import seats

        def cap(name, *a, **k):
            return {"membership": mapping.get(str(name), "JOINED")}
        return mock.patch.object(seats, "recipient_capability", cap)


class TheReachProbeTest(AbsentRecipientBase):

    def test_it_reports_JOINED_and_ABSENT_from_the_roster_door(self):
        with self.reach({GONE: "ABSENT"}):
            got = dispatches.recipient_reach([LIVE, GONE, LIVE])
        self.assertEqual(got, {LIVE: "JOINED", GONE: "ABSENT"})

    def test_an_UNREADABLE_probe_is_UNKNOWN_and_never_ABSENT(self):
        """A ROSTER THAT CANNOT BE READ HAS PROVED NOTHING ABOUT WHO EXISTS,
        and rendering that as ABSENT would tell a reader the whole estate was
        orphaned by one bad file handle."""
        from helm import seats
        # POSITIVE CONTROL FIRST, same names, same call: a working door
        # answers, so the UNKNOWN below is about the failure and not about
        # the probe never running.
        with self.reach({GONE: "ABSENT"}):
            self.assertEqual(dispatches.recipient_reach([GONE]),
                             {GONE: "ABSENT"})
        def boom(name, *a, **k):
            raise OSError("roster unreadable")
        with mock.patch.object(seats, "recipient_capability", boom):
            got = dispatches.recipient_reach([LIVE, GONE])
        self.assertEqual(got, {LIVE: "UNKNOWN", GONE: "UNKNOWN"})

    def test_a_membership_word_it_does_not_know_is_UNKNOWN_not_ABSENT(self):
        """THE FAILURE DIRECTION THAT MATTERS IS CLAIMING AN ORPHAN THAT IS
        NOT ONE. A membership state added upstream must not be read as proof
        a seat is gone."""
        with self.reach({GONE: "SOMETHING-NEW"}):
            got = dispatches.recipient_reach([GONE])
        self.assertEqual(got, {GONE: "UNKNOWN"})

    def test_an_EMPTY_name_is_not_probed_at_all(self):
        """AND A REAL NAME BESIDE THEM STILL IS, which is the control: a
        function that probed nothing at all would satisfy the emptiness
        assertion perfectly."""
        with self.reach({GONE: "ABSENT"}):
            got = dispatches.recipient_reach(["", "   ", None, GONE])
        self.assertEqual(got, {GONE: "ABSENT"})
        for blank in ("", "   ", None):
            self.assertNotIn(blank, got)

    def test_it_probes_each_distinct_name_ONCE(self):
        """The listing calls this on the hot path beside a comment promising
        it stays parse-only, so a per-ROW probe is a real cost."""
        from helm import seats
        calls = []

        def cap(name, *a, **k):
            calls.append(str(name))
            return {"membership": "JOINED"}
        with mock.patch.object(seats, "recipient_capability", cap):
            got = dispatches.recipient_reach([LIVE, LIVE, LIVE, OTHER, OTHER])
        # THE UNCONDITIONAL POSITIVE: it probed, and it answered for both
        # names. A function that probed nothing would satisfy a de-duplication
        # assertion trivially.
        self.assertEqual(got, {LIVE: "JOINED", OTHER: "JOINED"})
        self.assertEqual(sorted(calls), sorted({LIVE, OTHER}))


class TheListingNamesAnAbsentRecipientTest(AbsentRecipientBase):
    """The marker, the footers, and the selector, driven through the real verb."""

    def setUp(self):
        super().setUp()
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        import subprocess
        def git(*a):
            subprocess.run(("git",) + a, cwd=self.repo, check=True,
                           capture_output=True)
        git("init", "-q")
        git("config", "user.email", "t@example.com")
        git("config", "user.name", "T")
        with open(os.path.join(self.repo, "f"), "w") as fh:
            fh.write("x")
        git("add", "f")
        git("commit", "-qm", "a")
        self.tip = subprocess.run(("git", "rev-parse", "HEAD"), cwd=self.repo,
                                  capture_output=True, text=True
                                  ).stdout.strip()
        for who, lane in ((LIVE, "lane-live"), (GONE, "lane-gone")):
            # `_reason=True` is what makes this door return (row, err); the
            # bare call returns the row or None and a fixture that unpacks it
            # dies before any arm runs.
            from tests._tmphome import dispatch_home
            with dispatch_home(self.repo):
                row, err = dispatches.add(who, lane, ref=self.tip,
                                          kind="review", new_work=True,
                                          repo=self.repo, notify=False,
                                          _reason=True)
            self.assertIsNone(err, err)
            self.assertTrue(row, "fixture: %s row was not minted" % lane)

    def test_the_row_carries_the_marker_and_the_LIVE_one_does_not(self):
        with self.reach({GONE: "ABSENT"}):
            rc, out = run_list(self.repo)
        self.assertEqual(rc, 0, out)
        # UNCONDITIONAL POSITIVE ON THE SAME OBSERVABLE: both rows rendered.
        self.assertIn("lane-live", out)
        self.assertIn("lane-gone", out)
        gone_line = [ln for ln in out.splitlines() if "lane-gone" in ln]
        live_line = [ln for ln in out.splitlines() if "lane-live" in ln]
        self.assertEqual(len(gone_line), 1, out)
        self.assertEqual(len(live_line), 1, out)
        self.assertIn("RECIPIENT ABSENT", gone_line[0])
        self.assertNotIn("RECIPIENT ABSENT", live_line[0],
                         "a live recipient was marked absent")

    def test_the_footer_names_the_retired_name_and_REFUSES_adoption(self):
        with self.reach({GONE: "ABSENT"}):
            _rc, out = run_list(self.repo)
        self.assertIn("RECIPIENT ABSENT (1 row(s), 1 name(s): %s)" % GONE, out)
        # THE SENTENCE THAT MATTERS. A marker without a disposition is an
        # alarm, and the disposition a reader reaches for first is the wrong
        # one.
        self.assertIn("DO NOT", out)
        self.assertIn("a rename is not a claim", out)
        self.assertIn("--orphaned", out)

    def test_an_UNREADABLE_roster_says_UNMEASURED_not_absent(self):
        from helm import seats
        def boom(name, *a, **k):
            raise OSError("nope")
        with mock.patch.object(seats, "recipient_capability", boom):
            _rc, out = run_list(self.repo)
        self.assertIn("RECIPIENT UNKNOWN", out)
        self.assertIn("UNMEASURED", out)
        # AND IT MUST NOT WEAR THE OTHER FOOTER: absent and unreadable are
        # opposite facts about the world, and this listing knows which it has.
        self.assertNotIn("RECIPIENT ABSENT (", out)

    def test_orphaned_selects_only_the_absent_row_and_says_so(self):
        with self.reach({GONE: "ABSENT"}):
            rc, out = run_list(self.repo, "--orphaned")
        self.assertEqual(rc, 0, out)
        self.assertIn("whose recipient has no roster row", out,
                      "the selector did not describe itself, so an empty "
                      "answer could not be read")
        self.assertIn("lane-gone", out)
        self.assertNotIn("lane-live", out,
                         "a live recipient survived the orphan filter")

    def test_orphaned_over_an_all_live_roster_is_an_EMPTY_answer_not_an_error(self):
        """ZERO IS THE GOOD NEWS AND IT MUST READ AS GOOD NEWS."""
        with self.reach({}):
            rc, out = run_list(self.repo, "--orphaned")
        self.assertEqual(rc, 0, out)
        self.assertIn("no matching rows", out)
        self.assertIn("whose recipient has no roster row", out)

    def test_an_unknown_option_still_refuses_and_now_names_orphaned(self):
        rc, out = run_list(self.repo, "--nonsense")
        self.assertEqual(rc, 2)
        self.assertIn("unknown option", out)
        self.assertIn("--orphaned", out)


def setUpModule():
    """No dispatch row this module writes walks the host's process table
    (task/3039; see tests._tmphome.pin_live_seats)."""
    from tests._tmphome import pin_live_seats
    pin_live_seats()


if __name__ == "__main__":
    unittest.main()
