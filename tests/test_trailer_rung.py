#!/usr/bin/env python3
"""The attribution rung: what it refuses, what it must NOT refuse, and why.

The rule is that no AI authoring line reaches a commit. Two failures shape the
arms, and they pull in opposite directions:

  * REFUSING TOO LITTLE. A check that only reads git's trailer BLOCK misses a
    line demoted to body text by trailing prose — and those bytes reach GitHub
    exactly like a promoted line, so the miss is the whole harm.
  * REFUSING TOO MUCH. A check keyed on a SUBSTRING refuses every commit that
    DISCUSSES the rule, this module included, and refuses `Co-Authored-By`
    lines naming real human collaborators. Either one makes the rung something
    authors route around, at which point it protects nothing.

The arms below pin both edges, because a rule with only one of them is a rule
that will be widened or narrowed by the next person who meets its false side.
"""
import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import trailer_rung  # noqa: E402

CANON = "Co-Authored-By: Claude <noreply@anthropic.com>"


def msg(*trailers, **kw):
    """A commit message with a real body and a real trailer block."""
    body = kw.get("body", "subject line\n\nA body paragraph that is prose.\n")
    return body + ("\n" + "\n".join(trailers) + "\n" if trailers else "")


class VerdictTest(unittest.TestCase):
    def states(self, message, **kw):
        """(state, why) plus the unconditional control every arm below shares.

        THE CONTROL IS ON THE SAME CALL, not a second one: a rung that returned
        None, or a constant, or blew up into a default would satisfy any
        assertEqual written against the value the author expected. Asserting
        the answer came from the declared domain is the cheapest fact that a
        broken call cannot produce."""
        state, why = trailer_rung.verdict(message, **kw)
        self.assertIn(state, (trailer_rung.OK, trailer_rung.FOUND,
                              trailer_rung.UNKNOWN),
                      "verdict answered outside its own domain: %r" % (state,))
        return state, why

    def test_a_message_with_no_authoring_line_is_admitted(self):  # noqa: VACUOUS_ASSERTION — COMPLIANCE IS DEFINED AS NOTHING TO REPORT: verdict returns why=None on OK, so this call has no non-empty channel to control on, by construction rather than by omission. The same-call domain control is in states(); the discriminating neighbours are the FOUND arms, which drive the SAME function and assert a non-empty why.
        self.assertEqual(self.states(msg())[0], trailer_rung.OK)

    def test_the_unqualified_form_is_REFUSED(self):
        """The unqualified form is an authoring line like every other, and
        nothing about it is privileged."""
        state, why = self.states(msg(CANON))
        self.assertEqual(state, trailer_rung.FOUND)
        self.assertIn(CANON, why, "the reader is not shown the line found")

    def test_a_MODEL_QUALIFIED_form_is_REFUSED(self):
        line = "Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
        state, why = self.states(msg(line))
        self.assertEqual(state, trailer_rung.FOUND)
        self.assertIn("Claude Opus 5", why)

    def test_a_SESSION_LINK_is_REFUSED_even_though_it_names_no_author(self):
        """`Claude-Session:` exists only to point at a model's transcript, so
        its PRESENCE is the disclosure whatever its value says. That is why it
        is matched by KEY while `Co-Authored-By` is matched by VALUE."""
        state, why = self.states(
            msg("Claude-Session: https://example.invalid/session_x"))
        self.assertEqual(state, trailer_rung.FOUND)
        self.assertIn("Claude-Session", why)

    def test_a_GENERATED_WITH_footer_is_REFUSED_though_it_is_no_trailer(self):
        """The PR-body footer shape. It has no trailer grammar at all, so a
        rung that only understood trailers would let it through — and it says
        the same thing in the same published place."""
        m = msg(body="subject\n\nbody\n\n"
                     "\U0001F916 Generated with [Claude Code](https://x.invalid)\n")
        state, why = self.states(m)
        self.assertEqual(state, trailer_rung.FOUND)
        self.assertIn("Generated with", why)

    def test_a_HUMAN_co_author_is_NOT_refused(self):  # noqa: VACUOUS_ASSERTION — the property under test is that nothing is reported, so there is no positive channel on this call by construction; its discriminating neighbour is test_the_unqualified_form_is_REFUSED, the SAME function on the SAME trailer token with a model value.
        """THE EDGE THAT MATTERS MOST. `Co-Authored-By` is a legitimate trailer
        for human collaborators. A rung that refused the TOKEN rather than the
        VALUE would break real co-authorship on a repo whose owner works with
        other people, and would be routed around within a day."""
        state, why = self.states(
            msg("Co-Authored-By: A Person <person@example.invalid>"))
        self.assertEqual(state, trailer_rung.OK,
                         "a human co-author was refused: %s" % (why,))

    def test_PROSE_mentioning_the_line_is_NOT_refused(self):  # noqa: VACUOUS_ASSERTION — as above: an admitted message reports nothing, and the discriminating neighbour is the same function refusing the same bytes at column zero.
        """A commit that DISCUSSES the rule — this module's own, say — carries
        the exact bytes and discloses nothing. A substring check would refuse
        the commit that introduced the rung."""
        m = msg(body="subject\n\nNo commit may carry %s as a trailer.\n" % CANON)
        self.assertEqual(self.states(m)[0], trailer_rung.OK)

    def test_an_INDENTED_copy_is_NOT_refused_and_the_hole_is_deliberate(self):
        """Quoted example text is indented, so indentation is what separates a
        specimen from a trailer. This leaves a real hole — an author who
        indents a genuine trailer passes — and the arm exists to record that
        the hole was CHOSEN, not missed: the target is the harness default,
        which is always emitted at column zero."""
        m = msg(body="subject\n\nlike this:\n\n    %s\n" % CANON)
        self.assertEqual(self.states(m)[0], trailer_rung.OK)
        self.assertIn(CANON, m,
                      "the fixture stopped carrying the line it exists to pin")

    def test_a_line_DEMOTED_to_body_text_is_still_REFUSED(self):
        """WHY THIS RUNG DOES NOT ASK GIT WHERE THE TRAILER BLOCK ENDS.
        Trailing prose demotes the line to body text, so `git
        interpret-trailers` cannot see it — and the bytes reach GitHub
        regardless. A rung that judged only the block would be blind to
        exactly the placement that still discloses."""
        m = msg(CANON) + "\nOne more thought, after the trailers.\n"
        state, why = self.states(m)
        self.assertEqual(state, trailer_rung.FOUND,
                         "a demoted authoring line went unseen — it reaches "
                         "GitHub whether or not git calls it a trailer")
        self.assertIn(CANON, why)

    def test_every_hit_is_reported_with_its_LINE_NUMBER(self):
        """The author was told to ADD this line by their own harness and may
        not believe it is there. A line number turns a search into an edit."""
        m = msg(CANON, "Claude-Session: https://example.invalid/s")
        state, why = self.states(m)
        self.assertEqual(state, trailer_rung.FOUND)
        numbers = [n for n, ln in enumerate(m.splitlines(), 1)
                   if ln.strip() in (CANON,
                                     "Claude-Session: https://example.invalid/s")]
        self.assertEqual(2, len(numbers), "the fixture stopped carrying two")
        for n in numbers:
            self.assertIn("line %d" % n, why,
                          "hit on line %d was not located for the reader" % n)


class ExitCodeTest(unittest.TestCase):
    """The rung's POSTURE, which is a separate question from its verdict."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-trailer-")
        self.addCleanup(__import__("shutil").rmtree, self.tmp,
                        ignore_errors=True)
        self.prior = {k: os.environ.get(k)
                      for k in ("HELM_TRAILER_REFUSE", "HELM_TRAILER_SKIP")}
        self.addCleanup(self._restore)
        for k in self.prior:
            os.environ.pop(k, None)

    def _restore(self):
        for k, v in self.prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _write(self, text):
        p = os.path.join(self.tmp, "COMMIT_EDITMSG")
        with open(p, "w") as fh:
            fh.write(text)
        return p

    def run_main(self, path):
        """(rc, stderr) — BOTH CHANNELS OF THE SAME CALL, because an exit code
        alone cannot tell a rung that examined the message from one that
        returned early and examined nothing. The operator-facing text IS the
        rung's effect; the exit code is only its posture."""
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = trailer_rung.main(["trailer_rung", path])
        return rc, err.getvalue()

    def test_a_violation_REFUSES_BY_DEFAULT(self):
        """THE POSTURE INVERTED WITH THE RULE, and that is the point of this
        arm. A rung shipping against a rule whose compliant forms nobody has
        enumerated earns a warning release; neither half of that applies here.
        "No such line" needs no census, and this rule is an owner ruling rather
        than a measurement that might be wrong."""
        rc, err = self.run_main(self._write(msg(CANON)))
        self.assertEqual(rc, 1, "an authoring line did not stop the commit")
        self.assertIn("REFUSED", err)
        self.assertIn(CANON, err, "the refusal did not show the offending line")

    def test_the_same_violation_only_WARNS_when_switched_off(self):
        """`HELM_TRAILER_REFUSE=0` is the operator's off switch. It is asserted
        because a posture with no way back is one somebody disables by deleting
        the rung."""
        os.environ["HELM_TRAILER_REFUSE"] = "0"
        rc, err = self.run_main(self._write(msg(CANON)))
        self.assertEqual(rc, 0, "the off switch did not disarm the refusal")
        self.assertIn("WARNING", err, "the violation produced no warning")
        self.assertIn(CANON, err, "the warning did not show the offending line")

    def test_a_CLEAN_message_passes_and_says_nothing(self):  # noqa: VACUOUS_ASSERTION — a clean commit is DEFINED by the rung saying nothing, so silence is the property under test and there is no positive channel to control on. The discriminating arm is its neighbour: the SAME runner, an offending message, rc 1 with text.
        rc, err = self.run_main(self._write(msg()))
        self.assertEqual(rc, 0, "a clean message was refused")
        self.assertEqual(err, "", "a clean message drew a complaint")

    def test_a_HUMAN_co_author_survives_the_hook_end_to_end(self):  # noqa: VACUOUS_ASSERTION — as above, admission is silence; its discriminating neighbour is the same runner refusing a model-valued line on the same trailer token.
        """The false-refusal edge, asserted through `main` and not only through
        `verdict`, because this is the one a real contributor would hit."""
        rc, err = self.run_main(self._write(
            msg("Co-Authored-By: A Person <person@example.invalid>")))
        self.assertEqual(rc, 0, "a human co-author was refused: %s" % err)
        self.assertEqual(err, "")

    def test_the_skip_is_honored_and_is_this_rungs_OWN(self):  # noqa: VACUOUS_ASSERTION — a honored skip is by construction the absence of output and of refusal; the same-call positive control it would need cannot exist, because a skip that produced one would not be a skip.
        os.environ["HELM_TRAILER_SKIP"] = "1"
        rc, err = self.run_main(self._write(msg(CANON)))
        self.assertEqual(rc, 0, "the skip did not stop the refusal")
        self.assertEqual(err, "")

    def test_a_missing_message_file_warns_rather_than_refusing(self):
        os.environ["HELM_TRAILER_REFUSE"] = "1"
        rc, err = self.run_main(os.path.join(self.tmp, "absent"))
        self.assertEqual(rc, 0, "an unreadable message became a refusal")
        self.assertIn("NOT checked", err,
                      "the rung passed silently on a message it never read")


class InstalledHookTest(unittest.TestCase):
    """THE RUNG IS NOT THE HOOK. Every arm above exercises the module; this one
    runs the generated shell script the installer actually writes, because a
    correct rule reached through a broken script protects nothing."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-trailer-hook-")
        self.addCleanup(__import__("shutil").rmtree, self.tmp,
                        ignore_errors=True)

    def _script(self):
        from helm.work import _guard
        body = dict(_guard.GUARD_HOOKS)["commit-msg"]
        return body % {"trailer": __import__("shlex").quote(
                           os.path.abspath(trailer_rung.__file__)),
                       "user_hook": os.path.join(self.tmp, "absent-user-hook"),
                       # the generated hook names the profile it was minted
                       # under in its own SKIPPED remedy line (task/2504)
                       "profile": "rail"}

    def _run(self, message, env=None):
        hook = os.path.join(self.tmp, "commit-msg")
        with open(hook, "w") as fh:
            fh.write(self._script())
        os.chmod(hook, 0o755)
        path = os.path.join(self.tmp, "MSG")
        with open(path, "w") as fh:
            fh.write(message)
        return subprocess.run([hook, path], capture_output=True, text=True,
                              env=dict(os.environ, **(env or {})))

    def _run_bytes(self, raw, env=None):
        """Like `_run`, but the message file carries RAW BYTES.

        `_run` writes through a text handle and therefore cannot express the
        case under test at all: git hands the hook a FILE OF BYTES, and the
        defect these two arms pin lives entirely in how that file is decoded.
        """
        hook = os.path.join(self.tmp, "commit-msg")
        with open(hook, "w") as fh:
            fh.write(self._script())
        os.chmod(hook, 0o755)
        path = os.path.join(self.tmp, "MSG")
        with open(path, "wb") as fh:
            fh.write(raw)
        return subprocess.run([hook, path], capture_output=True, text=True,
                              env=dict(os.environ, **(env or {})))

    def test_an_undecodable_message_keeps_its_ASCII_trailer_readable(self):  # noqa: VACUOUS_ASSERTION — the positive control IS present and unconditional (same helper, same bytes, one line swapped: stderr non-empty and rc 1) but it arrives on a SECOND _run_bytes call, and this rung credits a control only from the SAME call's other channel; the control cannot be moved into the first call because a message cannot carry both a canonical and a model-qualified trailer as its only attribution
        """THE ARM THAT EARNS THE DECODE CHOICE, not merely the crash fix.

        A message is BYTES — git has `i18n.commitEncoding` because non-UTF-8
        messages are legitimate — so the commonest real shape here is a
        perfectly good ASCII trailer beside one odd byte in the body. Reading
        `errors="replace"` keeps that trailer FINDABLE; the simpler cure of
        treating any decode failure as unreadable would answer NOT CHECKED for
        a message that is in fact compliant, and the rung would go blind on the
        exact population it is meant to serve.

        SO THIS ARM DISCRIMINATES TWO CURES, not just cure-versus-bug: it is
        RED on the original (a traceback and rc 1), and it is also red on
        give-up-on-decode (a `[helm trailer]` warning instead of silence). It
        passes only if the bad byte was tolerated AND the ASCII trailer was
        still read. REFUSE is ON, so a rung that failed to find the trailer
        would refuse and the returncode alone would catch it.
        """
        raw = ("subject line\n\nbody with a bad byte: ".encode("utf-8")
               + b"\xff\xfe"
               + ("\n\n%s\n" % CANON).encode("utf-8"))
        with self.assertRaises(UnicodeDecodeError):
            raw.decode("utf-8")          # must-hit: the fixture really is undecodable
        p = self._run_bytes(raw)
        self.assertEqual(p.returncode, 1,
                         "an ASCII authoring line beside one bad byte went "
                         "unseen — the decode lost it: %s" % p.stderr)
        self.assertIn(CANON, p.stderr,
                      "the offending line was not shown to the reader")
        # THE CONTROL ON THE SAME OBSERVABLE AND IN THE OTHER DIRECTION. The
        # assertion above is a refusal, which a rung that refused EVERYTHING
        # would also produce — including one that treated every undecodable
        # message as guilty. Same helper, same bad byte, the authoring line
        # removed: this path must be able to stay silent.
        clean = raw.replace(("\n\n%s\n" % CANON).encode("utf-8"), b"\n")
        self.assertNotEqual(clean, raw, "control fixture is identical to the arm's")
        q = self._run_bytes(clean)
        self.assertEqual(q.returncode, 0, q.stderr)
        self.assertNotIn("[helm trailer]", q.stderr,
                         "control: a clean undecodable message was complained "
                         "about, so the refusal above says nothing")

    def test_an_undecodable_message_is_JUDGED_and_never_CRASHES(self):  # noqa: VACUOUS_ASSERTION — the Traceback absence is not the arm's observable and cannot stand alone: the SAME stderr carries an unconditional POSITIVE assertion in this same call (assertIn on the offending line), so a run that produced nothing at all fails before the absence is ever reached
        """A DECODE FAILURE MUST NOT BECOME A CRASH, which is a different claim
        from the verdict and survives the rule's inversion unchanged.

        `open(path, "r")` decoded with the locale default, so `read()` raised
        UnicodeDecodeError — a ValueError, outside the OSError catch — and the
        hook died with a traceback. Measured through this same generated hook
        on fab before it was cured. The bad byte becomes U+FFFD and the ASCII
        around it is judged normally; the observable is a REASONED refusal, not
        a stack trace, and not the silence a rung that died early would give.
        """
        raw = ("subject\n\n" .encode("utf-8") + b"\xff\xfe"
               + b"\n\nCo-Authored-By: Claude Opus 5 <noreply@anthropic.com>\n")
        with self.assertRaises(UnicodeDecodeError):
            raw.decode("utf-8")          # must-hit: the fixture really is undecodable
        p = self._run_bytes(raw)
        self.assertEqual(p.returncode, 1, p.stderr)
        self.assertIn("Claude Opus 5", p.stderr,
                      "the authoring line beside the bad byte went unjudged")
        self.assertNotIn("Traceback", p.stderr)

    def test_the_generated_hook_admits_a_CLEAN_message(self):  # noqa: VACUOUS_ASSERTION — admitting IS silence plus rc 0; the positive control on this same script is its neighbour, which refuses an authoring line and prints it back.
        p = self._run(msg())
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertNotIn("[helm trailer]", p.stderr,
                         "a clean message drew a complaint from the hook")

    def test_the_generated_hook_REFUSES_an_authoring_line_by_default(self):
        """No flag is set: the installed hook refuses on its own, which is what
        makes the rule hold for a seat that never reads this file."""
        p = self._run(msg(CANON))
        self.assertEqual(p.returncode, 1, p.stdout)
        self.assertIn(CANON, p.stderr,
                      "the refusal did not show the offending line")

    def test_a_missing_snapshot_warns_and_admits_rather_than_disarming_git(self):
        """A rung whose snapshot is gone must not take the commit down with
        it — and must say so, because a silent pass is how a guard becomes
        inert without anyone noticing."""
        from helm.work import _guard
        body = dict(_guard.GUARD_HOOKS)["commit-msg"] % {
            "trailer": os.path.join(self.tmp, "no-such-rung.py"),
            "user_hook": os.path.join(self.tmp, "absent-user-hook"),
            "profile": "rail"}
        hook = os.path.join(self.tmp, "commit-msg")
        with open(hook, "w") as fh:
            fh.write(body)
        os.chmod(hook, 0o755)
        path = os.path.join(self.tmp, "MSG")
        with open(path, "w") as fh:
            fh.write(msg())
        p = subprocess.run([hook, path], capture_output=True, text=True,
                           env=dict(os.environ, HELM_TRAILER_REFUSE="1"))
        self.assertEqual(p.returncode, 0)
        self.assertIn("rung missing", p.stderr)


class PlanTest(unittest.TestCase):
    def test_the_installer_plans_a_commit_msg_hook_with_no_placeholder_left(self):
        from helm.work import _guard
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        plan = {h["name"]: h for h in _guard._guard_plan(root)[1]}
        self.assertIn("commit-msg", plan, "the rung is not installed anywhere")
        script = plan["commit-msg"]["script"]
        self.assertNotIn("%(", script,
                         "an unsubstituted placeholder would run as a literal")
        self.assertIn("trailer_rung.py", script)


class ModelNameTrailerTest(unittest.TestCase):
    """task/2980 lane 5: the owner's list names a MODEL-NAME TRAILER beside
    Co-Authored-By, and the unbracketed harness footer. Before this, only
    Co-Authored-By was judged by value, so `Assisted-by: Claude` and
    "Generated with Claude Code" (no brackets) reached a commit. The gh body
    deny (helm/actsteer.py) reads bodies with this same reader, so both
    surfaces moved together."""

    def found(self, message):
        state, why = trailer_rung.verdict(message)
        self.assertIn(state, (trailer_rung.OK, trailer_rung.FOUND))
        return state, why

    def test_an_attribution_key_naming_a_model_is_REFUSED(self):
        state, why = self.found(msg("Assisted-by: Claude"))
        self.assertEqual(state, trailer_rung.FOUND)
        self.assertIn("Assisted-by: Claude", why)
        for line in ("Assisted-by: Claude Opus 5.5",
                     "Generated-by: codex-3",
                     "Written-by: GPT-5 via codex",
                     "Authored-by: Gemini 3 Pro"):
            with self.subTest(line=line):
                state, why = self.found(msg(line))
                self.assertEqual(state, trailer_rung.FOUND, line)
                self.assertIn(line, why)

    def test_the_UNBRACKETED_harness_footer_is_REFUSED(self):
        state, why = self.found(msg(body="subject\n\nbody\n\n"
                                         "Generated with Claude Code\n"))
        self.assertEqual(state, trailer_rung.FOUND)
        self.assertIn("Generated with Claude Code", why)

    def test_PROSE_about_the_footer_is_admitted(self):
        """A footer OPENS its line (after an emoji, a dash or a bullet). The
        two sentences measured in review discuss the footer and disclose
        nothing; the real footer, positive control on the same reader, is
        still refused."""
        state, why = self.found(msg(body="subject\n\nbody\n\n"
                                         "\U0001F916 Generated with [Claude "
                                         "Code](https://x.invalid)\n"))
        self.assertEqual(state, trailer_rung.FOUND)
        self.assertIn("Generated with", why)
        for line in ("the hook now refuses the Generated with Claude Code "
                     "footer", "nothing here was generated with Claude."):
            with self.subTest(line=line):
                state, why = self.found(msg(body="subject\n\n%s\n" % line))
                self.assertEqual(state, trailer_rung.OK,
                                 "prose about the rule was refused: %s" % why)

    def test_the_same_keys_naming_a_PERSON_or_prose_are_admitted(self):
        """THE OTHER EDGE, and the positive control is on the same reader: the
        model-valued line one arm up is refused, so this admission is the
        value test's doing and not a reader that refuses nothing."""
        refused, _ = self.found(msg("Assisted-by: Claude Opus 5.5"))
        self.assertEqual(refused, trailer_rung.FOUND)
        for line in ("Assisted-by: A Person <person@example.invalid>",
                     "Reviewed-by: A Person <person@example.invalid>",
                     # `Model:` is prose in a commit about routing, and is not
                     # an attribution key (trailer_rung._VALUE_KEYS says why)
                     "Model: codex serves the review lane now"):
            with self.subTest(line=line):
                state, why = self.found(msg(line))
                self.assertEqual(state, trailer_rung.OK,
                                 "%s was refused: %s" % (line, why))


if __name__ == "__main__":
    unittest.main()
