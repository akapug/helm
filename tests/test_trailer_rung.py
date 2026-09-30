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

    def test_every_FAMILY_the_fleet_runs_is_REFUSED_by_its_own_name(self):
        """A harness that credits its model by the model's own name never says
        "claude" or "anthropic", so the rung must know each family the fleet
        runs. Every name here reached no refusal before the marks named it."""
        state, why = self.states(msg("Co-Authored-By: Fable <bot@example.invalid>"))
        self.assertEqual(state, trailer_rung.FOUND)     # unconditional control
        self.assertIn("Fable", why)
        for name in ("Fable", "Opus", "Sonnet", "Haiku", "Kimi", "Moonshot",
                     "DeepSeek", "ds4pro", "Grok", "xAI", "Qwen", "Cursor",  # noqa: SEAT_NAME — model and product names a harness credits in a trailer, the subject under test
                     "Gemini", "Codex", "GLM"):
            for key in ("Co-Authored-By", "Assisted-By"):
                line = "%s: %s <bot@example.invalid>" % (key, name)
                state, why = self.states(msg(line))
                self.assertEqual(state, trailer_rung.FOUND,
                                 "%r passed the rung" % line)
                self.assertIn(name, why)

    def test_a_family_name_used_as_a_HUMAN_name_is_NOT_refused(self):
        """THE EDGE THE WIDENED LIST BOUGHT. Several fleet family names are
        common-name words, and `Co-Authored-By` is the human trailer: a
        substring test refuses a co-author named Kimi or Fable, the human the
        trailer is FOR, and a guard that refuses honest work is routed around.
        Each case failed before the marks were word-matched; the machine
        control on the same bytes leads the name."""
        state, why = self.states(msg("Co-Authored-By: Fable <bot@example.invalid>"))
        self.assertEqual(state, trailer_rung.FOUND)   # unconditional control
        for line in ("Co-Authored-By: Kimi Nozawa <k@example.invalid>",
                     "Co-Authored-By: Jo Fable <j@example.invalid>",
                     "Co-Authored-By: Qwendolyn Price <q@example.invalid>",
                     "Co-Authored-By: Opus Dei <o@example.invalid>",
                     "Co-Authored-By: Grokhold Ltd <g@example.invalid>",
                     "Co-Authored-By: Sonnet Simmons <s@example.invalid>",
                     "Co-Authored-By: Cursor Fitzpatrick <c@example.invalid>",
                     "Co-Authored-By: Xai Lopez <x@example.invalid>",
                     "Co-Authored-By: Deepa Seekar <d@example.invalid>",
                     "Co-Authored-By: Claude Martin <c@example.invalid>",
                     "Co-Authored-By: Jo Grok 4 <j@example.invalid>",
                     "Co-Authored-By: Grok 4 Martin <g@example.invalid>",
                     "Co-Authored-By: Kimberly (Kimi) Nozawa <k@example.invalid>",
                     "Co-Authored-By: Claude Martin (Acme) <c@example.invalid>",
                     "Co-Authored-By: Jean-Claude Van Damme <j@example.invalid>",
                     "Co-Authored-By: Ed Smith <eds4@example.invalid>",
                     "Co-Authored-By: Kids4Code Team <team@kids4code.invalid>"):
            state, why = self.states(msg(line))
            self.assertEqual(state, trailer_rung.OK,
                             "a human co-author was refused: %s" % (why,))

    def test_a_family_name_with_a_VERSION_or_EXPLICIT_alias_is_REFUSED(self):  # noqa: VACUOUS_ASSERTION — every case is asserted POSITIVELY (state == FOUND) over a non-empty literal tuple, and the human-name arm on the same bytes discriminates it
        """A model credit leads with the family name and carries a version or
        a declared alias; the same name with an ordinary successor is a person.
        The leading-name arm discriminates the two."""
        for line in ("Co-Authored-By: Kimi K3 <bot@example.invalid>",
                     "Co-Authored-By: Sonnet 4.5 <bot@example.invalid>",
                     "Co-Authored-By: Qwen2.5-Coder <bot@example.invalid>",
                     "Co-Authored-By: Moonshot AI <bot@example.invalid>",
                     "Co-Authored-By: xAI Grok <bot@example.invalid>",
                     "Co-Authored-By: Grok 4 Heavy <bot@example.invalid>",
                     "Co-Authored-By: Sonnet 4.5 Preview <bot@example.invalid>",
                     "Co-Authored-By: Fable 5.1 Max <bot@example.invalid>",
                     "Co-Authored-By: Qwen 3 Coder <bot@example.invalid>",
                     "Co-Authored-By: GLM 5 <bot@example.invalid>",
                     "Co-Authored-By: GLM 5 Air <bot@example.invalid>",
                     "Co-Authored-By: Kimi K3 Thinking <bot@example.invalid>",
                     "Co-Authored-By: Claude Code <bot@example.invalid>",
                     "Assisted-By: Claude Opus 5 <bot@example.invalid>"):
            state, why = self.states(msg(line))
            self.assertEqual(state, trailer_rung.FOUND,
                             "%r passed the rung" % line)

    def test_a_JOINED_suffix_is_read_without_its_separator(self):  # noqa: VACUOUS_ASSERTION — both tuples are non-empty literals asserted positively (FOUND) and negatively (OK) on the same rung
        """A harness that joins its family and variant with a dash
        ("moonshot-ai", "qwen-coder") credits a model; the same shape with a
        plain word ("Kimi-Lee") is a person."""
        for line in ("Co-Authored-By: moonshot-ai <bot@example.invalid>",
                     "Co-Authored-By: qwen-coder <bot@example.invalid>",
                     "Co-Authored-By: kimi-k3 <bot@example.invalid>"):
            state, why = self.states(msg(line))
            self.assertEqual(state, trailer_rung.FOUND,
                             "%r passed the rung" % line)
        for line in ("Co-Authored-By: Kimi-Lee Park <k@example.invalid>",
                     "Co-Authored-By: Opus-Marie Dunn <o@example.invalid>",
                     "Co-Authored-By: Kimi Coder <human@example.invalid>",
                     "Co-Authored-By: Kimi-Ai <human@example.invalid>"):
            state, why = self.states(msg(line))
            self.assertEqual(state, trailer_rung.OK,
                             "a human co-author was refused: %s" % (why,))

    def test_unicode_model_spellings_are_bounded_but_REFUSED(self):  # noqa: VACUOUS_ASSERTION — both non-empty literal tuples are asserted on the same verdict reader; the first requires FOUND and the second requires OK
        """NFKC covers compatibility-width aliases; the one measured Cyrillic
        homoglyph is folded only inside an otherwise-ASCII token. An ordinary
        non-Latin human name and the same mixed spelling with a human successor
        remain people, not model credits."""
        for name in ("Ｃｌａｕｄｅ－３", "Clаude-3"):
            line = "Co-Authored-By: %s <bot@example.invalid>" % name
            state, why = self.states(msg(line))
            self.assertEqual(state, trailer_rung.FOUND, line)
            self.assertIn(name, why)
        for name in ("Клод Мартен", "Clаude Martin", "Сlaude Martin"):
            line = "Co-Authored-By: %s <human@example.invalid>" % name
            state, why = self.states(msg(line))
            self.assertEqual(state, trailer_rung.OK,
                             "a human co-author was refused: %s" % why)

    def test_the_spaced_coauthor_key_is_deliberately_equivalent(self):
        line = "cO - aUtHoReD\t By  : Claude-3 <bot@example.invalid>"
        state, why = self.states(msg(line))
        self.assertEqual(state, trailer_rung.FOUND)
        self.assertIn(line, why)

    def test_every_CATALOG_family_resolves_to_a_mark(self):  # noqa: VACUOUS_ASSERTION — every case is asserted POSITIVELY (state == FOUND) over a non-empty literal tuple
        """The list and the catalog must agree: a family the fleet runs whose
        name no mark reaches is a trailer that ships. dots3, gpt-oss and
        openrouter had no mark before the cure; ds4flash rode on `ds4` only
        because the sweep's list happened to share the prefix."""
        for name in ("dots3", "gpt-oss-120b", "openrouter", "ds4flash",
                     "ds4pro", "glm-5"):
            line = "Co-Authored-By: %s <bot@example.invalid>" % name
            state, why = self.states(msg(line))
            self.assertEqual(state, trailer_rung.FOUND,
                             "%r passed the rung" % line)

    def test_every_name_the_CATALOG_holds_is_REFUSED_read_from_the_catalog(self):
        """A literal list agrees with the catalog only on the day it is
        written. This reads the catalog itself: every family key, the native
        family's model aliases, and every model id a family launches, probes
        or falls back to. `gptoss`, `grok-build-0.1`, `or-fast`, `or-code`,
        `north-mini-code` and `qwenlocal` passed while the literal list above
        said the catalog was covered."""
        from helm import homes, seat, seat_catalog, seats_identity  # noqa: F401 — the facade before its impl module
        from helm import burnflags, route
        names = (set(seat_catalog.FAMILIES) | set(burnflags.families())
                 | set(route.FROM_ALIASES) | set(homes.ROOTS)
                 | set(seats_identity._FAMILIES))
        for fam in seat_catalog.FAMILIES.values():
            names.update(m for m in (fam.get("model"), fam.get("model_fallback"))
                         if isinstance(m, str))
            names.update(fam.get("probe_models") or ())
        refused = [name for name in sorted(names) if self.states(msg(
            "Co-Authored-By: %s <noreply@example.invalid>" % name))[0]
            == trailer_rung.FOUND]
        self.assertIn("gptoss", refused)        # a catalog family, read live
        self.assertEqual(refused, sorted(names),
                         "catalog names that pass the rung: %s"
                         % sorted(names - set(refused)))

    def test_the_HARNESS_display_names_are_REFUSED_whatever_the_address(self):  # noqa: VACUOUS_ASSERTION — every case is asserted POSITIVELY (state == FOUND) over a non-empty literal tuple, and the human arms above discriminate the same reader
        """Each value below is a display name a harness wrote into this
        repository's history (Claude Fable 5 on 551 commits, Claude Opus 5
        (1M context) on 488, the kimi proxy seat's form on 7), or the owner's
        public sign-off carried into a trailer. Seats have written their own
        addresses (`<noreply@helm.invalid>`, `<codex@helm.local>`), so the
        vendor address cannot be the only thing that catches them. The rung
        before this lane refused every one on `claude`; the bounded grammar
        passed them all until a trailing qualifier and `claude-fable` were
        read."""
        for name in ("Claude Fable 5", "Claude Fable 5.1", "claude-fable-5-1",
                     "Claude Opus 5 (1M context)", "claude-opus-5-5[1m]",
                     "Claude (kimi-k3, Moonshot)",
                     "Claude Opus 5.5, working in helm",
                     "Claude 3.5 Sonnet", "aider (claude-sonnet-5)",
                     "claude[bot]", "example-claude-2", "GPT 6", "GPT OSS",
                     "Qwen3.6-27B"):
            line = "Co-Authored-By: %s <noreply@helm.invalid>" % name
            state, why = self.states(msg(line))
            self.assertEqual(state, trailer_rung.FOUND,
                             "%r passed the rung" % line)
            self.assertIn(name, why)

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


class CreditLineTest(unittest.TestCase):
    """task/3102: four credit shapes that passed the rung before this lane.

    The owner's rule forbids every model-name trailer and every
    Generated-with footer, not only the Claude spelling of Co-Authored-By.
    Each arm below names a line that reached a commit before this lane,
    and a human or prose line on the same reader that must still pass.
    The seat arms read a FIXTURE roster planted through HELM_SEAT_NAMES,
    the variable the suite already isolates. The names in it are synthetic,
    so no real seat name is written into tests/.
    """

    ROSTER = ("# fixture seat-name authority\n"
              "example-claude\nseat-under-test\n!example-opus\njd\n")

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-trailer-seats-")
        self.addCleanup(__import__("shutil").rmtree, self.tmp,
                        ignore_errors=True)
        self.roster = os.path.join(self.tmp, "seat-names.txt")
        with open(self.roster, "w") as fh:
            fh.write(self.ROSTER)
        env = mock.patch.dict(os.environ, {"HELM_SEAT_NAMES": self.roster})
        env.start()
        self.addCleanup(env.stop)

    def verdict(self, line):
        state, why = trailer_rung.verdict(msg(line))
        self.assertIn(state, (trailer_rung.OK, trailer_rung.FOUND))
        return state, why

    def refused(self, lines):
        for line in lines:
            with self.subTest(line=line):
                state, why = self.verdict(line)
                self.assertEqual(state, trailer_rung.FOUND,
                                 "%r passed the rung" % line)
                self.assertIn(line, why)

    def admitted(self, lines):
        for line in lines:
            with self.subTest(line=line):
                state, why = self.verdict(line)
                self.assertEqual(state, trailer_rung.OK,
                                 "%r was refused: %s" % (line, why))

    def test_a_footer_naming_ANY_family_is_REFUSED(self):  # noqa: VACUOUS_ASSERTION — every case is asserted POSITIVELY (FOUND, and the line shown) over a non-empty literal tuple
        """The footer check read only `claude`. The same footer naming
        another family, or a model id, passed."""
        self.refused(("Generated by Fable 5.1",
                      "Generated with Opus 5.5",
                      "\U0001F916 Generated with Codex",
                      "Written by Fable",
                      "Generated with Claude.",
                      "\U0001F916 Generated with [Claude Code](https://x.invalid)",
                      "Written by Claude Opus 5 (1M context)",
                      "Authored by codex-3 against a base, delivered as",
                      "Generated with ChatGPT"))

    def test_line_start_PROSE_after_a_footer_verb_is_admitted(self):
        """Only the words right after the verb are judged. A wrapped line of
        prose that opens with the same verb names no model there, even when
        a model name appears later in the line. The first three lines are
        from trunk. The control on the same reader leads."""
        state, why = self.verdict("Written by Fable")
        self.assertEqual(state, trailer_rung.FOUND)
        self.assertIn("Written by Fable", why)
        self.admitted((
            "written by the shipped `codex_pool` writer, reports CENSUS",
            "(written by other code, no checkpoint yet) leaves a miss",
            "AUTHORED BY THE REVIEWER, APPLIED AND GATED BY THE LANE HOLDER.",
            "Written by Kimi Nozawa",
            "Written by Claude Martin for the codex migration",
            "Generated by the gate runner"))

    def test_every_CREDIT_key_is_judged_by_its_value(self):  # noqa: VACUOUS_ASSERTION — both helpers assert per case over non-empty literal tuples, FOUND and OK on the same reader
        """Reviewed-by and its siblings were never read. The value is judged
        by the same grammar as Co-Authored-By. The last model line is from
        trunk, where prose follows the credit after a dash."""
        keys = ("Reviewed-by", "Signed-off-by", "Helped-by", "Assisted-by",
                "Acked-by", "Tested-by")
        self.refused(["%s: kimi (cross-family)" % k for k in keys] +
                     ["Reviewed-by: kimi (cross-family, 0.2 council) — "
                      "raised the finding."])
        self.admitted(["%s: Jane Doe <jane@example.com>" % k for k in keys] +
                      ["Reviewed-by: Kimi Nozawa <kimi@example.com>"])

    def test_an_exact_SEAT_NAME_in_a_credit_value_is_REFUSED(self):  # noqa: VACUOUS_ASSERTION — every case is asserted POSITIVELY (FOUND) over a non-empty literal tuple
        """A seat name without a digit is not in the model grammar, because
        `<word>-claude` also spells Jean-Claude. The roster names the seats
        exactly. A held name (`!`) is held only from the tests/ guard, and
        it is still a seat."""
        self.refused(("Reviewed-by: example-claude",
                      "Co-Authored-By: seat-under-test <x@example.invalid>",
                      "Assisted-by: example-opus (held in the roster)",
                      "Reviewed-by: Jane Doe, Example-Claude",
                      "Written by example-claude",
                      "Co-Authored-By: A Person <example-claude@example.invalid>"))

    def test_a_human_name_that_only_RESEMBLES_a_seat_is_admitted(self):
        """The match is exact. A name that contains a seat's shape is a
        person, and a short roster name with no separator and no digit
        (`jd`) is read as initials, not a seat. The control leads."""
        state, why = self.verdict("Reviewed-by: example-claude")
        self.assertEqual(state, trailer_rung.FOUND)
        self.assertIn("example-claude", why)
        self.admitted((
            "Co-Authored-By: Jean-Claude Van Damme <j@example.invalid>",
            "Co-Authored-By: Example-Claude Martin <e@example.invalid>",
            "Co-Authored-By: JD <jd@example.invalid>",
            "Reviewed-by: Jane Doe <example-claude.fan@example.invalid>"))

    def test_the_ROSTER_is_what_refuses_a_seat_name(self):
        """With no roster there is no seat to name. The same line passes,
        and the model grammar on the same reader still refuses."""
        with mock.patch.dict(os.environ, {"HELM_SEAT_NAMES": os.path.join(
                self.tmp, "absent-seat-names.txt")}):
            state, why = self.verdict("Reviewed-by: kimi (cross-family)")
            self.assertEqual(state, trailer_rung.FOUND)
            self.assertIn("kimi", why)
            state, why = self.verdict("Reviewed-by: example-claude")
            self.assertEqual(state, trailer_rung.OK, why)

    def test_the_roster_path_is_the_seat_name_guards_own(self):  # noqa: VACUOUS_ASSERTION — the control above the loop is unconditional and asserts BOTH resolvers return the planted roster path; the loop only widens the override spellings
        """This rung ships as a standalone snapshot and cannot import
        seatname_guard, so it repeats that module's path resolution. The two
        must name one file for every spelling of the override."""
        from helm import seatname_guard
        roster = self.roster
        self.assertEqual(trailer_rung._roster_path(), roster)   # control
        self.assertEqual(seatname_guard.authority_path()[0], roster)
        for env in ({"HELM_SEAT_NAMES": roster},
                    {"MELD_SEAT_NAMES": roster},
                    {"HELM_SEAT_NAMES": "", "MELD_SEAT_NAMES": roster},
                    {}):
            with self.subTest(env=env), mock.patch.dict(os.environ, env):
                for var in {"HELM_SEAT_NAMES", "MELD_SEAT_NAMES"} - set(env):
                    os.environ.pop(var, None)
                path = trailer_rung._roster_path()
                self.assertTrue(path, "no roster path resolved")
                self.assertEqual(path, seatname_guard.authority_path()[0])

    def test_a_model_named_only_in_the_ADDRESS_is_REFUSED(self):  # noqa: VACUOUS_ASSERTION — every case is asserted POSITIVELY (FOUND) over a non-empty literal tuple
        """A vendor AI domain names the model whatever the display name
        says."""
        self.refused(tuple("Co-Authored-By: A Person <%s>" % a for a in (
            "kimi@moonshot.ai", "cursoragent@cursor.com", "opus@x.ai",
            "bot@deepseek.com", "bot@mail.moonshot.ai", "bot@mistral.ai")))

    def test_a_first_name_ADDRESS_at_a_human_domain_is_admitted(self):
        """A bare first name in the local part is a person at a human
        domain, and a domain that only ends in a vendor's letters is not the
        vendor's. The control leads."""
        state, why = self.verdict("Co-Authored-By: A Person <kimi@moonshot.ai>")
        self.assertEqual(state, trailer_rung.FOUND)
        self.assertIn("moonshot.ai", why)
        self.admitted((
            "Co-Authored-By: Kimi Nozawa <kimi@example.com>",
            "Co-Authored-By: Claude Martin <claude@example.org>",
            "Co-Authored-By: Opus Dei <opus@example.net>",
            "Co-Authored-By: Jo Box <jo@box.ai>",
            "Co-Authored-By: Ann Lee <ann@notcursor.com>"))

    def test_a_footer_name_ENDS_at_its_address(self):
        """The footer's name ran on into its address, so "Claude
        <noreply@anthropic.com>" read as a person. The claude-only footer
        before this lane refused the first three lines. The address is
        judged as a trailer's is, and a person's address still passes. The
        control leads."""
        state, why = self.verdict("Written by Claude <noreply@anthropic.com>")
        self.assertEqual(state, trailer_rung.FOUND)
        self.assertIn("noreply@anthropic.com", why)
        self.refused((
            "Generated with Claude <noreply@anthropic.com>",
            "Generated with Claude Code <https://claude.com/claude-code>",
            "Co-Authored-By Claude <noreply@anthropic.com>",
            "Written by A Person <bot@moonshot.ai>",
            "Written by A Person <example-claude@example.invalid>",
            "Generated with <Claude>"))
        self.admitted((
            "Written by Kimi Nozawa <kimi@example.com>",
            "Written by Claude Martin <claude@example.org>",
            "Written by Jane Doe <jane@example.com>, then bot@openai.com"))

    def test_an_alias_with_a_TIER_before_its_version_is_a_model(self):
        """An alias followed by tiers was a model, and adding the version
        after the tier made it a person again. The claude-only footer before
        this lane refused the footer line. The control leads."""
        state, why = self.verdict("Co-Authored-By: Claude Code Opus")
        self.assertEqual(state, trailer_rung.FOUND)
        self.assertIn("Claude Code Opus", why)
        self.refused(("Co-Authored-By: Claude Code Opus 5.5",
                      "Generated with Claude Code Opus 5.5",
                      "Reviewed-by: Claude Code Pro 2"))
        self.admitted(("Co-Authored-By: Claude Martin 2 <c@example.org>",
                       "Co-Authored-By: Claude Code Martin <c@example.org>"))


    def test_a_trailer_name_ENDS_at_its_first_comma(self):
        """A credit value's name ends at its first comma, as a footer's
        does: "Claude Opus, working in helm" credits a model. The first piece
        is judged as a name; later pieces count only when they name a model
        unambiguously, so a surname-first person ("Nozawa, Kimi") stays a
        person. The control leads."""
        state, why = self.verdict(
            "Co-authored-by: Claude Opus, working in helm")
        self.assertEqual(state, trailer_rung.FOUND)
        self.assertIn("Claude Opus", why)
        self.refused(("Co-authored-by: Claude Code, working in helm",
                      "Co-Authored-By: Claude Sonnet, wrote the parser",
                      "Reviewed-by: Jane Doe, kimi-k3"))
        self.admitted(("Signed-off-by: Nozawa, Kimi <k@example.jp>",
                       "Signed-off-by: Doe, Jane <jane@example.com>",
                       "Co-Authored-By: Claude Martin, PhD <c@example.org>"))

    def test_a_piece_with_NO_WORD_is_not_the_name(self):
        """The comma cut made an empty first piece the name, so ", Claude"
        passed after the cut and was refused before it. A piece with no
        letter is skipped, and the next piece is the name. The control
        leads."""
        state, why = self.verdict("Co-authored-by: , Claude")
        self.assertEqual(state, trailer_rung.FOUND)
        self.assertIn(", Claude", why)
        self.refused(("Co-authored-by: *, Kimi",
                      "Co-authored-by: (bot), Fable",
                      "Co-authored-by: , , Opus, working in helm"))
        self.admitted(("Signed-off-by: , Nozawa, Kimi <k@example.jp>",
                       "Co-Authored-By: (Kimi), Nozawa <k@example.jp>"))

    def test_a_trailer_name_ENDS_where_a_footer_name_does(self):
        """A credit's names are split by one list, whether they follow a
        trailer key or a footer verb. Each line below names a model at a
        colon, a sentence end, a slash, an ampersand, a connective, a
        semicolon, or inside brackets or angle brackets with no name
        beside them. The same words after a footer verb are refused, so
        the trailer refuses them too. The control is the footer."""
        state, why = self.verdict("Generated with Claude Opus via Bedrock")
        self.assertEqual(state, trailer_rung.FOUND)
        self.assertIn("Claude Opus via Bedrock", why)
        self.refused(("Co-authored-by: Claude Opus via Bedrock",
                      "Co-authored-by: Claude Opus: working in helm",
                      "Co-authored-by: Claude Opus. Thanks",
                      "Co-authored-by: Jane Doe & Claude Opus",
                      "Co-authored-by: Jane Doe / Claude Opus",
                      "Co-authored-by: Jane Doe and Claude Opus",
                      "Co-authored-by: Jane Doe; Claude Opus 5.5",
                      "Co-authored-by: <Claude>",
                      "Co-authored-by: (Claude)"))

    def test_a_footer_judges_EVERY_name_it_lists(self):
        """A footer can list names as a trailer can. The first name is
        judged alone; a later one counts only when it names a model
        unambiguously, so a person listed second stays a person. The
        control leads."""
        state, why = self.verdict("Generated with Claude Opus")
        self.assertEqual(state, trailer_rung.FOUND)
        self.assertIn("Generated with Claude Opus", why)
        self.refused(("Generated with Jane Doe and Claude Opus",
                      "Written by Jane Doe / Claude Opus",
                      "Written by Jane Doe; Claude Opus 5.5",
                      "Generated with Jane Doe, kimi-k3",
                      "Written by Jane Doe (claude-opus-5)"))
        self.admitted(("Written by Jane Doe and Claude Martin",
                       "Generated with Jane Doe, Kimi"))

    def test_a_piece_with_NO_LETTER_is_not_the_name(self):
        """A name has a letter. A piece of digits or a numeral symbol is
        dropped before the first name is chosen, in a trailer and in a
        footer, so the model named after it is the first name. The
        control leads."""
        state, why = self.verdict("Co-authored-by: , Claude")
        self.assertEqual(state, trailer_rung.FOUND)
        self.assertIn(", Claude", why)
        self.refused(("Co-authored-by: 1, Claude",
                      "Co-authored-by: 5.5, Claude",
                      "Co-authored-by: ①, Claude",
                      "Generated with , Claude",
                      "Written by —, Claude",
                      "Generated with 5.5, Opus",
                      "Written by ①, Fable"))

    PEOPLE = ("Nozawa, Kimi", "Claude Martin, PhD", "Jane Doe, Claude Martin",
              "Van Damme, Jean-Claude", "山田, Kimi", "Claude Monet",
              "Kimi Räikkönen, Jr.", "Jane Doe and Claude Martin",
              "Jane Doe & Kimi Nozawa", "Jean-Claude Van Damme / Kimi Nozawa")

    def test_people_stay_people_on_BOTH_surfaces(self):
        """The splitter cuts names more often than a comma alone did, and
        every cut makes a new piece to judge. A person's name, alone or in
        a list, is admitted after a trailer key and after a footer verb.
        The control is a model credit on the same reader."""
        state, why = self.verdict(
            "Co-authored-by: Claude Opus, working in helm")
        self.assertEqual(state, trailer_rung.FOUND)
        self.assertIn("Claude Opus", why)
        self.admitted(["Co-Authored-By: %s <p@example.jp>" % name
                       for name in self.PEOPLE] +
                      ["Written by %s" % name for name in self.PEOPLE])

    def test_a_surname_first_person_named_for_a_model_is_REFUSED(self):
        """A DESIGN CHOICE. The first name in a credit is judged alone, and
        alone "Claude" is the model's own name: nothing in "Claude, Albert"
        says that a surname stands first. So a person whose surname is a
        model word, written surname first, is refused. A later name counts
        only when it names a model unambiguously, so the same person written
        given name first ("Albert Claude") or with the model word second
        ("Albert, Claude") is admitted. The refusal is the cost of reading
        "Claude, working in helm" as the model it credits. The control
        leads."""
        state, why = self.verdict("Co-Authored-By: Claude, Albert <a@example.org>")
        self.assertEqual(state, trailer_rung.FOUND)
        self.assertIn("Claude, Albert", why)
        self.refused(("Written by Claude, Albert",))
        self.admitted(("Co-Authored-By: Albert Claude <a@example.org>",
                       "Co-Authored-By: Albert, Claude <a@example.org>",
                       "Written by Albert, Claude"))

    def test_the_public_SIGNOFF_is_neither_trailer_nor_footer(self):
        """A public post ends with an informal signoff that names its model
        and links helm. It opens with two dashes, so no trailer key and no
        footer verb reads it, and no name in it is judged. The same words as
        a credit value are refused. The control leads."""
        state, why = self.verdict(
            "Co-Authored-By: Claude Opus 5.5, working in helm")
        self.assertEqual(state, trailer_rung.FOUND)
        self.assertIn("Claude Opus 5.5", why)
        state, why = trailer_rung.verdict(msg(body=(
            "subject\n\nThanks, landed.\n\n-- Claude Opus 5.5, working in "
            "helm (https://github.com/akapug/helm) for @akapug\n")))
        self.assertEqual(state, trailer_rung.OK, why)

    def test_a_BRACKET_ends_a_name(self):
        """A bracketed qualifier is read apart from the name it follows, and
        the name ends where the bracket opens: "Claude Opus (bot) working in
        helm" credits Claude Opus, after a trailer key and after a footer
        verb. Joined across the bracket, the words read "Claude Opus working"
        and passed. A nickname between a given name and a surname stays a
        person. The control leads."""
        state, why = self.verdict(
            "Co-Authored-By: Claude Opus 5 (1M context) <c@example.invalid>")
        self.assertEqual(state, trailer_rung.FOUND)
        self.assertIn("Claude Opus 5 (1M context)", why)
        self.refused(("Co-authored-by: Claude Opus (bot) working in helm",
                      "Generated with Claude Opus (bot) working in helm",
                      "Reviewed-by: Fable 5.1 [bot] working"))
        self.admitted(("Co-Authored-By: Kimberly (Kimi) Nozawa <k@example.jp>",
                       "Written by Kimberly (Kimi) Nozawa",
                       "Written by Claude Martin (Acme)"))

    def test_EVERY_angle_bracketed_address_is_judged(self):
        """What stands in angle brackets is an address, and a credit can
        carry more than one. Each is judged by its domain and its local
        part, after a footer verb as in a trailer value. An address outside
        angle brackets is prose after a footer's name. The control leads."""
        state, why = self.verdict("Written by Claude <noreply@anthropic.com>")
        self.assertEqual(state, trailer_rung.FOUND)
        self.assertIn("noreply@anthropic.com", why)
        self.refused((
            "Written by Jane Doe <Claude> <noreply@anthropic.com>",
            "Generated with Jane Doe <jane@example.com> <bot@moonshot.ai>",
            "Written by A Person <a@example.org> <example-claude@example.invalid>"))
        self.admitted((
            "Written by Jane Doe <jane@example.com> <j.doe@example.org>",
            "Written by Jane Doe <jane@example.com>, then bot@openai.com"))

    def test_a_name_AFTER_an_address_is_judged(self):
        """A credit lists names, and each name may carry its address. The
        list was cut at the first angle bracket, so a model or a seat named
        after a person's address was never listed: "Jane Doe
        <jane@example.com> and Claude Opus 5.5" passed while the same words
        with no address were refused. An address ends a name as a comma
        does, and the names after it are judged as later names, after a
        trailer key and after a footer verb. A person after a person's
        address stays a person. The control leads."""
        state, why = self.verdict("Co-authored-by: Jane Doe and Claude Opus 5.5")
        self.assertEqual(state, trailer_rung.FOUND)
        self.assertIn("Claude Opus 5.5", why)
        self.refused((
            "Co-authored-by: Jane Doe <jane@example.com> and Claude Opus 5.5",
            "Co-authored-by: Jane Doe <jane@example.com>, Claude Opus 5.5 "
            "<c@example.invalid>",
            "Co-authored-by: Jane Doe <jane@example.com> & kimi-k3",
            "Co-authored-by: Jane Doe <jane@example.com>, example-claude "
            "<e@example.invalid>",
            "Written by Jane Doe <jane@example.com> and Claude Opus 5.5",
            "Written by Jane Doe <jane@example.com> and seat-under-test"))
        self.admitted((
            "Co-authored-by: Jane Doe <jane@example.com> and Claude Martin",
            "Co-authored-by: Jane Doe <jane@example.com>, Kimi Nozawa "
            "<k@example.jp>",
            "Written by Jane Doe <jane@example.com> and Claude Martin"))

    def test_a_credit_that_OPENS_with_a_bracketed_name_is_judged_by_it(self):
        """A credit whose first word is a bracketed name with no address,
        "<Claude> working in helm", is judged by that name first, as a bare
        "<Claude>" is. A person before the brackets still leads, so "Jane Doe
        <Claude>" reads the bracket as Jane's address. The control leads."""
        state, _why = self.verdict("Co-authored-by: <Claude>")
        self.assertEqual(state, trailer_rung.FOUND)
        self.refused((
            "Co-authored-by: <Claude> working in helm",
            "Written by <Fable> and Jane Doe",
            "Co-authored-by: <seat-under-test> working"))
        self.admitted((
            "Co-authored-by: Jane Doe <Claude>",
            "Written by Jane Doe <jane@example.com>"))


if __name__ == "__main__":
    unittest.main()
