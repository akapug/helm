#!/usr/bin/env python3
"""helm chat node — consensus-posture mirroring and honest start failures.

WHY THIS EXISTS: the chat node had never once started on the dev machine. Not a
regression, not a flake — the generated unit could not possibly work, and the two
bugs that hid it were both in helm.

dregg refuses to boot when the verified Lean executor archive is absent: it will
not "serve as if verified while running the un-verified executor". The escape
hatch belongs to the operator, and on this machine the operator had already
opened it — for the TEAM cave node, in a systemd drop-in, which is the standard
place to state machine-local posture without editing a shipped unit. helm's unit
was the same binary on the same machine and carried no Environment= lines, so it
restart-looped forever.

And helm reported that as "unit started but the API never answered". A timeout is
true of every possible cause, so it pointed nowhere; meanwhile the service had
already printed the exact remedy. Relaying a subordinate's precise diagnosis is
not optional for a provisioner that owns it.

The invariant these tests pin, in both directions: helm MIRRORS an existing
operator declaration and NEVER mints one. A machine that has not opted out of
verification must keep dregg's refusal.
"""
import os
import shutil
import tempfile
import unittest
from unittest import mock

import os as _os, sys as _sys  # noqa: E402
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

from helm import chatnode  # noqa: E402


class PostureBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-posture-")
        self.prior_home = os.environ.get("HOME")
        os.environ["HOME"] = self.tmp
        self.units = os.path.join(self.tmp, ".config", "systemd", "user")
        os.makedirs(self.units)

    def tearDown(self):
        if self.prior_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self.prior_home
        shutil.rmtree(self.tmp, ignore_errors=True)

    def declare(self, body, unit=None, name="override.conf"):
        d = os.path.join(self.units, (unit or chatnode.PEER_UNIT) + ".d")
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, name)
        with open(p, "w", encoding="utf-8") as f:
            f.write(body)
        return p


class DeclaredPostureTest(PostureBase):
    def test_reads_the_operators_dregg_flags_from_a_dropin(self):
        src = self.declare("[Service]\n"
                           "Environment=DREGG_ALLOW_UNVERIFIED_CONSENSUS=1\n"
                           "Environment=DREGG_ALLOW_UNAUDITED_PQ=1\n")
        got = chatnode.declared_posture()
        self.assertEqual([a for _s, a in got],
                         ["DREGG_ALLOW_UNVERIFIED_CONSENSUS=1",
                          "DREGG_ALLOW_UNAUDITED_PQ=1"])
        self.assertTrue(all(s == src for s, _a in got), "source must be named")

    def test_only_dregg_names_are_mirrored(self):
        """A drop-in may hold anything at all. Copying arbitrary Environment=
        lines into a second unit file would propagate unrelated — possibly
        secret-bearing — values onto disk in a new place. Only the flags that
        gate startup travel."""
        self.declare("[Service]\n"
                     "Environment=DREGG_ALLOW_UNVERIFIED_CONSENSUS=1\n"
                     "Environment=AWS_SECRET_ACCESS_KEY=hunter2\n"
                     "Environment=RUST_LOG=debug\n")
        got = [a for _s, a in chatnode.declared_posture()]
        self.assertEqual(got, ["DREGG_ALLOW_UNVERIFIED_CONSENSUS=1"])

    def test_a_multi_assignment_line_cannot_smuggle_a_secret(self):
        """kimi's HOLE 2, reproduced live before the fix. systemd's Environment=
        takes N whitespace-separated assignments on ONE line, so

            Environment=DREGG_ALLOW_UNVERIFIED_CONSENSUS=1 AWS_SECRET_ACCESS_KEY=x

        is two. The old filter asked whether the WHOLE string started with
        DREGG_, said yes, and mirrored the line whole — writing the secret into
        a second 0600 file on disk, which is exactly the leak the filter was
        cited as preventing. Validating the first token of an N-token grammar is
        not validation."""
        self.declare("[Service]\n"
                     "Environment=DREGG_ALLOW_UNVERIFIED_CONSENSUS=1 "
                     "AWS_SECRET_ACCESS_KEY=hunter2\n")
        got = [a for _s, a in chatnode.declared_posture()]
        self.assertEqual(got, ["DREGG_ALLOW_UNVERIFIED_CONSENSUS=1"])
        p = chatnode.write_posture_dropin(chatnode.declared_posture())
        self.assertNotIn("hunter2", open(p, encoding="utf-8").read(),
                         "a secret rode a multi-assignment line into the mirror")

    def test_quoted_assignments_are_split_the_way_systemd_splits_them(self):
        self.declare('[Service]\n'
                     'Environment="DREGG_A=one two" SECRET_B=nope\n')
        self.assertEqual([a for _s, a in chatnode.declared_posture()],
                         ["DREGG_A=one two"])

    def test_an_unbalanced_quote_mirrors_nothing_rather_than_guessing(self):
        """A line we cannot parse is not a line we may half-parse. Guessing at
        malformed input is how the multi-assignment hole would come back."""
        self.declare('[Service]\nEnvironment=DREGG_A=1 "unclosed\n')
        self.assertEqual(chatnode.declared_posture(), [])

    def test_a_name_merely_containing_DREGG_is_not_a_posture_flag(self):
        self.declare("[Service]\nEnvironment=NOT_DREGG_ALLOW=1\n")
        self.assertEqual(chatnode.declared_posture(), [])

    def test_no_dropin_directory_is_empty_not_an_error(self):
        self.assertEqual(chatnode.declared_posture(), [])

    def test_multiple_dropins_are_read_in_systemd_order(self):
        self.declare("[Service]\nEnvironment=DREGG_A=1\n", name="05-a.conf")
        self.declare("[Service]\nEnvironment=DREGG_B=2\n", name="90-b.conf")
        self.assertEqual([a for _s, a in chatnode.declared_posture()],
                         ["DREGG_A=1", "DREGG_B=2"])

    def test_non_conf_files_are_ignored(self):
        self.declare("[Service]\nEnvironment=DREGG_REAL=1\n", name="live.conf")
        self.declare("[Service]\nEnvironment=DREGG_STALE=1\n",
                     name="override.conf.bak")
        self.assertEqual([a for _s, a in chatnode.declared_posture()],
                         ["DREGG_REAL=1"])


class MirrorTest(PostureBase):
    def test_nothing_declared_writes_nothing(self):
        """THE LOAD-BEARING ONE. helm must never be the layer that quietly opts a
        node out of verification. No operator declaration => no drop-in => dregg's
        refusal stands, which is the correct outcome."""
        self.assertIsNone(chatnode.write_posture_dropin([]))
        d = chatnode._dropin_dir(chatnode.UNIT)
        self.assertFalse(os.path.exists(d), "minted a posture nobody declared")

    def test_mirrors_the_flags_and_names_its_source(self):
        src = self.declare("[Service]\nEnvironment=DREGG_ALLOW_UNVERIFIED_CONSENSUS=1\n")
        p = chatnode.write_posture_dropin(chatnode.declared_posture())
        body = open(p, encoding="utf-8").read()
        self.assertIn("Environment=DREGG_ALLOW_UNVERIFIED_CONSENSUS=1", body)
        self.assertIn("[Service]", body)
        self.assertIn(src, body, "a mirrored opt-out must cite where it came from")
        self.assertIn("MIRRORED, not decided here", body)
        self.assertEqual(oct(os.stat(p).st_mode)[-3:], "600")

    def test_rewriting_replaces_rather_than_appends(self):
        self.declare("[Service]\nEnvironment=DREGG_ONE=1\n")
        chatnode.write_posture_dropin(chatnode.declared_posture())
        os.remove(os.path.join(self.units, chatnode.PEER_UNIT + ".d",
                               "override.conf"))
        self.declare("[Service]\nEnvironment=DREGG_TWO=2\n")
        p = chatnode.write_posture_dropin(chatnode.declared_posture())
        body = open(p, encoding="utf-8").read()
        self.assertIn("DREGG_TWO=2", body)
        self.assertNotIn("DREGG_ONE=1", body,
                         "a withdrawn declaration must not survive in the mirror")

    def test_withdrawal_deletes_the_mirror_it_must_not_outlive_its_source(self):
        """kimi's HOLE 1, reproduced live before the fix. rewrite-replaces
        handled an EDIT; full WITHDRAWAL is a different terminal state. The
        operator deletes their declaration, declared_posture() returns [], and
        the previously-mirrored file stayed on disk still carrying
        Environment=DREGG_ALLOW_*=1 — so our copy kept granting an opt-out the
        operator had rescinded, and the node went on running unverified on an
        authority that no longer existed.

        Empty posture has two meanings: never-declared (write nothing) and
        withdrawn (clear). Conflating them is the whole bug."""
        src = self.declare("[Service]\nEnvironment=DREGG_ALLOW_UNVERIFIED_CONSENSUS=1\n")
        mirror = chatnode.write_posture_dropin(chatnode.declared_posture())
        self.assertTrue(os.path.exists(mirror))
        os.remove(src)                                   # operator rescinds
        self.assertEqual(chatnode.declared_posture(), [])
        returned = chatnode.write_posture_dropin(chatnode.declared_posture())
        self.assertFalse(os.path.exists(mirror),
                         "a withdrawn opt-out outlived its source")
        self.assertEqual(returned, mirror, "the removal must be reported")

    def test_never_declared_still_writes_nothing_after_the_withdrawal_fix(self):
        """The other half of the same discrimination: clearing on withdrawal must
        not turn into writing on never-declared."""
        self.assertIsNone(chatnode.write_posture_dropin([]))
        self.assertFalse(os.path.exists(chatnode._dropin_dir(chatnode.UNIT)))

    def test_the_generated_comment_does_not_promise_what_the_code_skips(self):
        """The first version's file said 'remove the source declaration and this
        file becomes empty on the next node up'. It did not. A false guarantee
        inside a generated file is worse than no comment — it is what a future
        reader checks INSTEAD of the code."""
        self.declare("[Service]\nEnvironment=DREGG_ALLOW_UNVERIFIED_CONSENSUS=1\n")
        body = open(chatnode.write_posture_dropin(chatnode.declared_posture()),
                    encoding="utf-8").read()
        self.assertIn("DELETES", body)
        self.assertNotIn("becomes empty", body)

    def test_the_mirror_targets_our_unit_not_the_peers(self):
        self.declare("[Service]\nEnvironment=DREGG_ALLOW_UNVERIFIED_CONSENSUS=1\n")
        p = chatnode.write_posture_dropin(chatnode.declared_posture())
        self.assertIn(chatnode.UNIT + ".d", p)
        self.assertNotIn(chatnode.PEER_UNIT + ".d", p,
                         "never write back into the operator's own declaration")


class FailureRelayTest(PostureBase):
    REFUSAL = ("2026-07-25T15:17:51Z ERROR dregg_node: REFUSING TO START: "
               "`dregg_lean_ffi::lean_available()` is false ... To deliberately "
               "run an un-verified node, set DREGG_ALLOW_UNVERIFIED_CONSENSUS=1.")

    def _journal(self, stdout):
        return mock.patch("helm.chatnode.subprocess.run",
                          return_value=mock.Mock(stdout=stdout, returncode=0))

    def test_the_services_own_words_are_returned(self):
        with self._journal("Started helm-chat-node.service\n" + self.REFUSAL + "\n"):
            self.assertEqual(chatnode.last_failure(), self.REFUSAL)

    def test_the_newest_error_wins(self):
        with self._journal("x ERROR old thing\ny ERROR newest thing\n"):
            self.assertIn("newest", chatnode.last_failure())

    def test_a_clean_journal_is_None_never_a_fabricated_reason(self):
        with self._journal("Started helm-chat-node.service\nlistening on 8898\n"):
            self.assertIsNone(chatnode.last_failure())

    def test_journalctl_missing_degrades_to_None(self):
        with mock.patch("helm.chatnode.subprocess.run", side_effect=OSError("nope")):
            self.assertIsNone(chatnode.last_failure())


if __name__ == "__main__":
    unittest.main()
