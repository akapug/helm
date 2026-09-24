#!/usr/bin/env python3
"""The PROSE sha check — helm/shaguard.py and its two send-path call sites.

THE DEFECT, measured four times in one session across two model families with
ZERO caught by the agent that made the error: a real short sha padded into a
fabricated 40-hex, and two announced tips whose first seven characters were
right and whose remaining hex was invented. Right shape, right length, correct
short prefix — invisible to re-reading. helm's TYPED paths already refuse an
unresolvable ref (`dispatch send --ref` caught the padded one); the PROSE paths
had nothing, and prose is where a reviewer picks a tip up and binds work to it.

WHAT EACH TEST HOLDS, and why the negatives matter as much as the positive:

  * the warning FIRES on a fabricated 40-hex;
  * it does NOT fire on a resolvable one (or the fleet learns to ignore it);
  * it does NOT fire on a short prefix — 7-12 chars are safe to DISPLAY and
    unsafe only to EXTEND, and warning on them would drown the signal;
  * the post still SUCCEEDS. Warn, never block, is a design requirement: a
    message legitimately quotes an UPSTREAM sha (helm files PRs against other
    people's repositories) or one from an unfetched lane, and blocking those
    would create a false-refusal class;
  * the warning does not OVERCLAIM. A non-resolving sha is not proof of
    fabrication. A guard that says "this sha is fake" has reproduced the
    confident-negative defect class inside itself;
  * a directory git cannot read stays SILENT — without that probe the guard
    would warn about perfectly real shas from any non-checkout cwd, which is a
    confident negative manufactured by its own blind spot.

Hermetic: a fixture git repo under tmp, tmp HELM_HOME/HELM_CHAT_DIR, signing
off. No real chat message ever leaves this module.
"""
import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests import _tmphome  # noqa: E402,F401  (plants the tmp env first)
from helm import chat, docref_guard, seats, shaguard  # noqa: E402

ENV_KEYS = ("HELM_HOME", "MELD_HOME", "HELM_CHAT_DIR", "MELD_CHAT_DIR",
            "HELM_CHAT_NAME", "MELD_CHAT_NAME", "HELM_CHAT_NODE_URL",
            "MELD_CHAT_NODE_URL", "HELM_CHAT_LOG", "MELD_CHAT_LOG",
            "HELM_CHAT_ROOM", "MELD_CHAT_ROOM", "HELM_CHAT_ROOM_SOURCE",
            "MELD_CHAT_ROOM_SOURCE", "HELM_SCRATCH_GC", "HELM_CACHE_DIR")

# A 40-hex whose every bounded prefix is absent from any fixture repo — the
# "no hint available" arm. Distinct from a fabricated sha, which by
# construction keeps a REAL prefix.
FOREIGN = "f" * 40

# The one phrase every warning carries. Kept as a constant so a reflow of
# the message body cannot silently turn these assertions vacuous.
MARK = "does not name an object"


class ShaGuardBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-shaguard-")
        self.prior = {k: os.environ.get(k) for k in ENV_KEYS}
        for key in ENV_KEYS:
            os.environ.pop(key, None)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "chat")
        os.environ["HELM_CHAT_NODE_URL"] = ""     # set-but-empty: signing off
        os.environ["HELM_CHAT_ROOM"] = "main"
        os.environ["HELM_CHAT_NAME"] = "builder"
        os.environ["HELM_SCRATCH_GC"] = "0"
        os.environ["HELM_CACHE_DIR"] = os.path.join(self.tmp, "cache")
        self.repo = os.path.join(self.tmp, "repo")
        self.bare = os.path.join(self.tmp, "not-a-repo")
        os.makedirs(self.repo)
        os.makedirs(self.bare)
        self.git("init", "-q")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "user.name", "Test")
        self.real = self.commit("one")
        self.cwd_prior = os.getcwd()

    def tearDown(self):
        os.chdir(self.cwd_prior)
        for key, value in self.prior.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(self.tmp, ignore_errors=True)

    def git(self, *args):
        p = subprocess.run(["git", "-C", self.repo, *args],
                           capture_output=True, text=True, check=True)
        return p.stdout.strip()

    def commit(self, text):
        path = os.path.join(self.repo, "state")
        with open(path, "a", encoding="utf-8") as f:
            f.write(text + "\n")
        self.git("add", "state")
        self.git("commit", "-q", "-m", text)
        return self.git("rev-parse", "HEAD")

    def patch_id(self, base, tip):
        diff = subprocess.run(
            ("git", "-C", self.repo, "diff", "--no-ext-diff", "--binary",
             base, tip, "--"), capture_output=True, check=True)
        out = subprocess.run(
            ("git", "-C", self.repo, "patch-id", "--stable"),
            input=diff.stdout, capture_output=True, check=True).stdout.decode()
        return out.split()[0]

    def fabricated(self, keep=7):
        """A real prefix + invented hex — the exact shape of all four measured
        errors. Character `keep` is forced to DIFFER from the real sha's, so
        the divergence sits at a KNOWN position instead of a coin flip: the
        prefix hunt must find `keep` characters and no more."""
        real = self.real
        nxt = "0" if real[keep] != "0" else "1"
        return real[:keep] + nxt + "a" * (40 - keep - 1)

    def stderr_of(self, fn, *a, **kw):
        """(return value, captured stderr) — the suite never writes to the
        real stderr, and the warning IS the product under test."""
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            out = fn(*a, **kw)
        return out, buf.getvalue()


class ScanTest(ShaGuardBase):
    """The pure scan: which tokens the regex admits, and what git says."""

    def test_a_fabricated_40hex_warns(self):
        bad = self.fabricated()
        self.assertNotEqual(bad, self.real)
        said = shaguard.warnings("tip is %s, please review" % bad,
                                 root=self.repo)
        self.assertEqual(len(said), 1, said)
        self.assertIn(bad, said[0])
        self.assertIn(MARK, said[0])

    def _cat_file_answers(self, answer=None):
        """Run findings() with cat-file -e answering `answer`, everything else
        REAL.

        The wrapper delegates every attribute to the live backend and
        overrides ONE argv shape, because `_nearest` and the repo probe go
        through the same object: a double that answered for all of them would
        be testing the double. `answer` is the exact (ran, rc, out) triple
        `probe_outcome` documents, so the arm cannot pass on a shape
        production never emits.

        `answer=None` DELEGATES EVEN cat-file, which is what makes the
        positive control a control: it runs the identical call path through
        this same helper, so the control and the absence are readings of ONE
        observable rather than two expressions that merely look related.
        """
        from helm import vcs
        real = vcs.backend(self.repo)

        class _OneAnswer:
            def __getattr__(self, name):
                return getattr(real, name)

            def probe_outcome(self, cwd, *args, **kw):
                if answer is not None and args[:2] == ("cat-file", "-e"):
                    return answer
                return real.probe_outcome(cwd, *args, **kw)

        with mock.patch.object(vcs, "backend", lambda *_a, **_k: _OneAnswer()):
            return shaguard.warnings("tip is %s, please review" % self.bad_tok,
                                     root=self.repo)

    def test_a_probe_that_never_RAN_does_not_accuse_the_token(self):
        """A TIMEOUT IS NOT AN ABSENT OBJECT. `_probe` folds a completed
        nonzero, a missing binary and a timeout into one None, and the
        accusing branch read that None as "the object is not here" — so a
        transient git failure mid-sweep publishes a VALID sha as dangling,
        with exactly the confidence of a real finding.

        The repo probe at the top of findings() already refuses to speak when
        git cannot speak about the directory at all. This is that same rule
        applied PER TOKEN, which is where a timeout actually lands: the repo
        probe succeeded seconds earlier and the guard is mid-sweep."""
        self.bad_tok = self.fabricated()
        # CONTROL FIRST, UNCONDITIONAL: this token IS accusable when git
        # answers normally, or the silence below is about a token nothing
        # would have warned on.
        self.assertEqual(len(self._cat_file_answers()), 1,
                         "the fabricated token does not warn when cat-file is "
                         "delegated to the real backend, so the silence below "
                         "is about a token nothing would have warned on")
        self.assertEqual(self._cat_file_answers((False, None, None)), [],
                         "git never ran and the guard reported a valid sha as "
                         "dangling anyway")

    def test_a_probe_that_RAN_and_said_absent_still_accuses(self):
        """THE MUST-MISS FOR THE ARM ABOVE, and the reason it is a separate
        method: a cure that stopped accusing anything would satisfy that arm
        perfectly. `ran True` with a nonzero rc is git ANSWERING — the object
        is not here — and that is the finding this guard exists to make."""
        self.bad_tok = self.fabricated()
        said = self._cat_file_answers((True, 1, None))
        self.assertEqual(len(said), 1,
                         "git ran and said the object is absent, and the "
                         "guard stayed silent — the cure traded a false "
                         "accusation for a missed one")
        self.assertIn(self.bad_tok, said[0])

    def test_a_resolvable_40hex_is_silent(self):
        """The signal dies the moment it fires on real shas."""
        self.assertEqual(
            shaguard.warnings("landed at %s" % self.real, root=self.repo), [])

    def test_a_verified_patch_id_is_not_misclassified_as_a_sha(self):
        base = self.real
        tip = self.commit("two")
        patch = self.patch_id(base, tip)
        self.assertEqual(len(patch), 40)
        self.assertNotEqual(patch, tip)                 # content, not object id
        self.assertEqual(shaguard.warnings(
            "patch-id AFTER rebase %s" % patch, root=self.repo), [])

        # MUST-MISS controls on the same surface: an unlabeled content id and a
        # fabricated labeled value still reach the SHA warning.
        self.assertEqual(len(shaguard.warnings(
            "tip %s" % patch, root=self.repo)), 1)
        self.assertEqual(len(shaguard.warnings(
            "patch-id %s" % FOREIGN, root=self.repo)), 1)

    def test_a_patch_label_applies_only_to_tokens_on_its_line(self):
        base = self.real
        tip = self.commit("two")
        patch = self.patch_id(base, tip)
        said = shaguard.warnings(
            "patch-id %s\ntip %s" % (FOREIGN, patch), root=self.repo)
        self.assertEqual(len(said), 2, said)
        self.assertIn(FOREIGN, said[0])
        self.assertIn(patch, said[1])

        # MUST-HIT on the same content identity: label the patch on its own line
        # and only that warning disappears. A message-wide label scope would
        # silence the unlabeled token above and fail this comparison.
        said = shaguard.warnings(
            "tip %s\npatch-id %s" % (FOREIGN, patch), root=self.repo)
        self.assertEqual(len(said), 1, said)
        self.assertIn(FOREIGN, said[0])

    def test_a_registered_patch_id_is_silent_outside_its_source_checkout(self):
        patch = next(iter(docref_guard.PATCH_IDS))
        self.assertEqual(len(patch), 40)
        self.assertEqual(shaguard.warnings(
            "patch-id %s" % patch, root=self.repo), [])
        self.assertEqual(len(shaguard.warnings(
            "patch-id %s" % FOREIGN, root=self.repo)), 1)  # MUST-MISS

    def test_a_short_prefix_never_warns(self):
        """7-12 chars are safe to DISPLAY and unsafe only to EXTEND.

        BOTH arms matter, and the second is the one that measures the width of
        the regex: a prefix that DOES resolve would stay silent even if the
        pattern were widened to {7,40}, so it cannot detect that mutation. A
        NON-resolving short prefix can, and prose is full of 7-letter words
        that are accidentally hex ('acceded', 'defaced') — widening this
        pattern would warn on English.
        """
        bad = self.fabricated()
        for n in (7, 8, 10, 12):
            self.assertEqual(
                shaguard.warnings("see %s" % self.real[:n], root=self.repo),
                [], "a resolvable %d-char prefix must be silent" % n)
        for n in (8, 10, 12):
            self.assertEqual(
                shaguard.warnings("see %s" % bad[:n], root=self.repo), [],
                "an UNRESOLVABLE %d-char prefix must still be silent — only "
                "40-hex is checked" % n)
        # and the MUST-HIT: the very same token at full length does warn, so
        # the silence above is the rule working rather than the scan broken
        self.assertEqual(len(shaguard.warnings("see %s" % bad,
                                               root=self.repo)), 1)

    def test_a_64_hex_digest_is_not_a_40_hex_token(self):
        """chat's own payload ids are 64-hex blake2b. The word boundaries are
        what stop the scan from reading the first 40 characters of one as a
        sha and warning about every signed row helm writes."""
        digest = "ab" * 32
        self.assertEqual(shaguard.tokens("chat:b2b:%s" % digest), [])
        self.assertEqual(shaguard.warnings("chat:b2b:%s" % digest,
                                           root=self.repo), [])
        # MUST-HIT: a 40-hex in the same shape IS seen
        self.assertEqual(shaguard.tokens("chat:b2b:%s" % ("ab" * 20)),
                         ["ab" * 20])

    def test_one_token_named_twice_warns_once(self):
        bad = self.fabricated()
        said = shaguard.warnings("%s ... and again %s" % (bad, bad),
                                 root=self.repo)
        self.assertEqual(len(said), 1, said)
        self.assertEqual(shaguard.tokens("%s %s" % (bad, bad)), [bad])

    def test_two_distinct_tokens_each_warn(self):
        bad, other = self.fabricated(), FOREIGN
        said = shaguard.warnings("%s and %s" % (bad, other), root=self.repo)
        self.assertEqual(len(said), 2, said)
        self.assertIn(bad, said[0])
        self.assertIn(other, said[1])


class NearestPrefixTest(ShaGuardBase):
    """The hint — the single most useful thing this guard can tell an author.

    All three measured announcements kept a correct 7-char prefix, so the
    object the author MEANT is one lookup away; printing it lets them compare
    two strings instead of re-reading the one they already wrote.
    """

    def test_the_real_object_the_prefix_points_at_is_named(self):
        bad = self.fabricated(keep=7)
        said = shaguard.warnings("tip %s" % bad, root=self.repo)[0]
        self.assertIn(self.real[:7], said)
        self.assertIn(self.real, said, "the REAL sha must appear in full — "
                                       "comparing is the whole point")
        self.assertIn("DOES resolve here", said)

    def test_the_longest_resolving_prefix_wins(self):
        """A 10-character correct prefix must report 10, not 7 — the closer
        the reported prefix, the more obviously the tail is invented."""
        bad = self.fabricated(keep=10)
        said = shaguard.warnings("tip %s" % bad, root=self.repo)[0]
        self.assertIn("10-char prefix %s" % self.real[:10], said)

    def test_no_resolving_prefix_means_no_hint_invented(self):
        """A sha wholly foreign to this checkout gets the honest warning and
        NO hint. Manufacturing a nearest-neighbour would be the same defect
        one layer down."""
        said = shaguard.warnings("upstream carries %s" % FOREIGN,
                                 root=self.repo)[0]
        self.assertIn(FOREIGN, said)
        self.assertNotIn("DOES resolve here", said)
        self.assertNotIn("If you meant", said)


class NoOverclaimTest(ShaGuardBase):
    """CONSTRAINT 2 — the one that makes this correct rather than clever.

    A non-resolving sha is NOT proof of fabrication: it may simply be foreign
    to this repo. Three times in one night this codebase shipped a repair that
    introduced a new confident negative (a docs fix with an invented rationale,
    an outbox whose corrupt read returned an empty queue, a card fix whose
    corrupt row rendered a clean zero). A guard against confident negatives
    that itself asserts one would be the fourth.
    """

    def test_the_warning_states_the_measurement_not_a_verdict(self):
        said = shaguard.warnings("tip %s" % self.fabricated(),
                                 root=self.repo)[0]
        low = said.lower()
        for verdict in ("fake", "fabricat", "invalid", "made up", "made-up",
                        "does not exist", "no such commit", "bogus", "wrong "
                        "sha", "hallucinat"):
            self.assertNotIn(verdict, low,
                             "the warning asserts %r — that is a verdict this "
                             "check cannot support: %s" % (verdict, said))
        self.assertIn("not proof", low)

    def test_the_benign_explanations_are_named(self):
        """The author must be told, in the warning itself, why a real sha can
        land here — otherwise the next reader treats it as an accusation."""
        said = shaguard.warnings("tip %s" % FOREIGN, root=self.repo)[0]
        low = said.lower()
        self.assertIn("upstream", low)
        self.assertIn("never fetched", low)
        self.assertIn("this checkout cannot resolve it", low)

    def test_it_says_the_message_was_sent(self):
        said = shaguard.warnings("tip %s" % FOREIGN, root=self.repo)[0]
        self.assertIn("never a block", said)


class RefusalSplitTest(ShaGuardBase):
    """WARN BY DEFAULT, REFUSE ONLY WHAT IS PROVEN.

    The module's original law was WARN, NEVER BLOCK, and its reasoning still
    holds for what it was about: an unresolvable sha may be upstream, in
    another fork, or simply unfetched, so refusing it would refuse a true
    statement. What is NOT ambiguous is a token whose LONG prefix resolves to a
    different local object — the author had the real thing in hand.

    Warning was not enough for that case. 2026-08-04: four padded shas from one
    seat; the three typed into `dispatch verdict` were refused by that verb and
    cost nothing, and the one posted to chat was warned about, SENT, and made a
    teammate's merge probe report a CONFLICT against a rev that does not exist.
    """

    def test_a_long_prefix_match_refuses(self):
        bad = self.fabricated(keep=12)
        self.assertTrue(shaguard.refusals("tip %s" % bad, root=self.repo))
        said = shaguard.warnings("tip %s" % bad, root=self.repo)[0]
        self.assertIn("REFUSED", said)
        self.assertNotIn("never a block", said)

    def test_a_SHORT_prefix_match_still_only_warns(self):
        """THE FLOOR IS THE POINT. At this object count a 7-char collision with
        a foreign sha runs about 1 in 17,000 — rare, but a real chance of
        refusing a true statement, which is the defect the old law prevented.
        Ten characters is about 1 in 5 billion and stops being a trade."""
        bad = self.fabricated(keep=7)
        self.assertEqual(shaguard.refusals("tip %s" % bad, root=self.repo), [])
        said = shaguard.warnings("tip %s" % bad, root=self.repo)[0]
        self.assertIn("never a block", said)
        self.assertNotIn("REFUSED", said,
                         "refusal language must not appear where no refusal happens")

    def test_an_unresolvable_sha_is_never_refused(self):
        """The ambiguous case the original law was written for: no prefix
        match, so nothing was measured beyond 'not here'."""
        # UNCONDITIONAL CONTROL on the same observable: this guard DOES
        # refuse something here, so "not refused" below is the ambiguity being
        # respected and not a refuse() that never fires in this fixture.
        self.assertTrue(shaguard.refuse("tip %s" % self.fabricated(keep=12),
                                        root=self.repo))
        said = "upstream " + ("a" * 40)
        self.assertEqual(shaguard.refusals(said, root=self.repo), [])
        self.assertFalse(shaguard.refuse(said, root=self.repo))

    def test_a_real_sha_is_silent(self):
        # UNCONDITIONAL CONTROL, same observable, same repo.
        self.assertTrue(shaguard.refuse("tip %s" % self.fabricated(keep=12),
                                        root=self.repo))
        self.assertEqual(shaguard.refusals("tip %s" % self.real, root=self.repo), [])
        self.assertFalse(shaguard.refuse("tip %s" % self.real, root=self.repo))

    def test_the_escape_covers_quoting_it_to_correct_it(self):
        """Correcting a padded sha REQUIRES quoting it, so a hard block with no
        escape would make the correction unpostable — the guard would enforce
        silence about its own findings."""
        bad = self.fabricated(keep=12)
        self.assertTrue(shaguard.refuse("tip %s" % bad, root=self.repo))
        os.environ[shaguard.SKIP_ENV] = "1"
        self.addCleanup(os.environ.pop, shaguard.SKIP_ENV, None)
        self.assertFalse(shaguard.refuse("tip %s" % bad, root=self.repo))

    def test_refuse_fails_OPEN_when_it_cannot_measure(self):
        """A guard that cannot run must never become a guard that blocks
        everything — the same law the rest of this module already keeps."""
        bad = self.fabricated(keep=12)
        # UNCONDITIONAL CONTROL: the SAME token refuses against a real repo, so
        # the silence below is the unmeasurable root and not a harmless token.
        self.assertTrue(shaguard.refuse("tip %s" % bad, root=self.repo))
        self.assertFalse(shaguard.refuse("tip %s" % bad, root=self.bare),
                         "not a checkout: nothing measured, nothing refused")


class FailOpenTest(ShaGuardBase):
    """Unknowable is SILENT. Every leg here would otherwise manufacture a
    confident negative out of its own blind spot."""

    def test_a_directory_git_cannot_read_says_nothing(self):
        """Without the repo probe, `cat-file -e` fails for EVERY token outside
        a checkout and the guard warns about perfectly real shas."""
        self.assertEqual(shaguard.warnings("tip %s" % FOREIGN,
                                           root=self.bare), [])
        self.assertEqual(shaguard.warnings("tip %s" % self.fabricated(),
                                           root=self.bare), [])
        # MUST-HIT: the same token in the fixture repo DOES warn, so the
        # silence above is the probe working and not a broken scan
        self.assertEqual(len(shaguard.warnings("tip %s" % FOREIGN,
                                               root=self.repo)), 1)

    def test_a_missing_root_says_nothing(self):
        self.assertEqual(
            shaguard.warnings("tip %s" % FOREIGN,
                              root=os.path.join(self.tmp, "gone")), [])

    def test_empty_and_none_bodies_are_free(self):
        for body in ("", None, "no shas here at all"):
            self.assertEqual(shaguard.tokens(body), [])
            self.assertEqual(shaguard.warnings(body, root=self.repo), [])

    def test_warn_never_raises(self):
        """A guard that can raise is a guard that can break the send it was
        only ever meant to annotate."""
        buf = io.StringIO()
        with mock.patch.object(shaguard, "warnings",
                               side_effect=RuntimeError("store on fire")):
            self.assertEqual(shaguard.warn("tip %s" % FOREIGN, stream=buf), 0)
        self.assertEqual(buf.getvalue(), "")
        # MUST-HIT: unpatched, the same call DOES print — the zero above is
        # the swallow working, not a call that never happened
        buf2 = io.StringIO()
        self.assertEqual(shaguard.warn("tip %s" % FOREIGN, root=self.repo,
                                       stream=buf2), 1)
        self.assertIn(FOREIGN, buf2.getvalue())


class ChatFunnelTest(ShaGuardBase):
    """chat.post is the ONE funnel — post, reply, dm (seats.dm), meld/council/
    standup say, the web panel's /api/chat, the owner TUI and every machine
    announcer reach it. The guard is spelled there exactly once, so reverting
    it cannot be masked by a second copy at the CLI layer."""

    def setUp(self):
        super().setUp()
        os.chdir(self.repo)          # the guard resolves against the cwd repo

    def test_a_post_carrying_a_fabricated_sha_warns_AND_LANDS(self):
        """CONSTRAINT 1 — warn, never block. The row must be in the room."""
        bad = self.fabricated()
        row, err = self.stderr_of(chat.post, "landed at %s" % bad, "main")
        self.assertIn(MARK, err)
        self.assertIn(bad, err)
        self.assertTrue(row and row.get("id"), "the post must have SUCCEEDED")
        rows, total = chat.read("main")
        self.assertEqual(total, 1)
        self.assertIn(bad, rows[0]["text"])

    def test_a_post_carrying_a_LONG_padded_prefix_never_LANDS(self):
        """THE ONE THAT ESCAPED. On 2026-08-04 a padded sha was warned about
        and posted, reached a teammate holding a build row, and made their
        merge probe report a CONFLICT computed against a rev that does not
        exist — a fabricated sha does not fail loudly at the reader, it
        produces a confident wrong answer inside someone else's instrument.

        The refusal happens BEFORE the row is built, so nothing durable ever
        records the token. The 7-char case above still lands, deliberately."""
        bad = self.fabricated(keep=12)
        with self.assertRaises(ValueError) as caught:
            chat.post("landed at %s" % bad, "main")
        self.assertIn("padded short sha", str(caught.exception))
        rows, total = chat.read("main")
        self.assertEqual(total, 0, "the row must NOT be in the room")
        # CONTROL: the same funnel accepts the REAL sha, so the refusal above
        # is the guard acting and not a post path that never works.
        row, _err = self.stderr_of(chat.post, "landed at %s" % self.real, "main")
        self.assertTrue(row and row.get("id"))
        self.assertEqual(chat.read("main")[1], 1)

    def test_the_escape_lets_a_correction_be_posted(self):
        """A correction must QUOTE the token it corrects, so a block with no
        escape would make the guard enforce silence about its own finding."""
        bad = self.fabricated(keep=12)
        os.environ[shaguard.SKIP_ENV] = "1"
        self.addCleanup(os.environ.pop, shaguard.SKIP_ENV, None)
        row, _err = self.stderr_of(chat.post, "CORRECTION: %s is wrong" % bad, "main")
        self.assertTrue(row and row.get("id"), "the correction must be postable")
        self.assertEqual(chat.read("main")[1], 1)

    def test_a_post_carrying_a_real_sha_is_silent(self):
        row, err = self.stderr_of(chat.post, "landed at %s" % self.real,
                                  "main")
        self.assertEqual(err, "", "a resolvable sha must not be warned about")
        self.assertTrue(row and row.get("id"))

    def test_a_post_carrying_a_verified_patch_id_is_silent(self):
        base = self.real
        tip = self.commit("two")
        patch = self.patch_id(base, tip)
        row, err = self.stderr_of(
            chat.post, "patch-id BEFORE/AFTER rebase %s" % patch, "main")
        self.assertEqual(err, "")
        self.assertTrue(row and row.get("id"))
        self.assertIn(patch, chat.read("main")[0][0]["text"])

    def test_a_post_carrying_a_short_prefix_is_silent(self):
        _row, err = self.stderr_of(chat.post,
                                   "landed at %s" % self.fabricated()[:10],
                                   "main")
        self.assertEqual(err, "")

    def test_the_dm_path_is_guarded(self):
        """A DM is exactly where a tip gets handed to one reviewer, with no
        room fanout and nobody else to notice."""
        bad = self.fabricated()
        out, err = self.stderr_of(seats.dm, "reviewer",
                                  "review %s please" % bad, who="builder")
        row, reason = out
        self.assertIsNone(reason, reason)
        self.assertTrue(row and row.get("id"), "the DM must have been sent")
        self.assertEqual(row.get("dm"), "reviewer")
        self.assertIn(bad, err)
        self.assertIn(MARK, err)

    def test_the_cli_post_verb_warns_and_still_exits_zero(self):
        bad = self.fabricated()
        out = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = chat.cmd_chat(["post", "--room", "main", "tip", bad])
        self.assertEqual(rc, 0, "the verb must not refuse: %s" % err.getvalue())
        self.assertIn(bad, err.getvalue())
        self.assertIn("never a block", err.getvalue())
        self.assertEqual(chat.read("main")[1], 1)


class StatusLineTest(ShaGuardBase):
    """`helm chat status <line>` is the OTHER free-text body under helm chat:
    160 bytes, rendered on every roster and console surface, and it does NOT
    pass through chat.post — the funnel guard cannot see it."""

    def setUp(self):
        super().setUp()
        os.chdir(self.repo)
        seats.write_roster("builder", presence_beat=False)

    def test_a_status_line_carrying_a_fabricated_sha_warns_and_still_sets(self):
        bad = self.fabricated()
        out, err = self.stderr_of(seats.set_status, "builder",
                                  "gating %s" % bad)
        ok, _msg = out
        self.assertTrue(ok, "the status must still have been SET")
        self.assertIn(bad, err)
        self.assertEqual(seats.roster()["builder"]["status"], "gating %s" % bad)

    def test_a_real_sha_in_a_status_line_is_silent(self):
        out, err = self.stderr_of(seats.set_status, "builder",
                                  "gating %s" % self.real)
        self.assertTrue(out[0])
        self.assertEqual(err, "")

    def test_a_token_the_clip_cuts_off_is_not_warned_about(self):
        """The scan reads the STORED line, not the caller's argument: warning
        about a token the 160-byte clip is about to remove would be a warning
        about text nobody will ever see."""
        bad = self.fabricated()
        long_line = "x" * seats.STATUS_BYTES + " " + bad
        out, err = self.stderr_of(seats.set_status, "builder", long_line)
        self.assertTrue(out[0])
        self.assertNotIn(bad, seats.roster()["builder"]["status"])
        self.assertEqual(err, "", "the clipped-away token must not be warned "
                                  "about: %s" % err)


if __name__ == "__main__":
    unittest.main()
