#!/usr/bin/env python3
"""No non-raw string literal may contain an invalid escape sequence.

THE CLASS. A backslash followed by a character Python does not recognise as an
escape is not an error today — it is a SyntaxWarning, and CPython has announced
it becomes a hard SyntaxError. The sequence survives into the string unchanged,
so nothing about the program's behaviour reveals the problem, and the warning
fires only on a FRESH COMPILE. Once a .pyc exists the file imports in silence.

That last property is what makes it worth a test rather than a fix. Measured on
2026-08-03 in helm/work/_guard.py, the git reference-transaction hook:

  * :244 wrote a backslash before a backtick, intending the SHELL to receive an
    escaped backtick (an unescaped one inside double quotes is command
    substitution). Emitting one backslash from a non-raw Python literal takes
    two, so the single form was an invalid Python escape.
  * :270 did the same before a dollar sign, intending the shell to PRINT a
    variable name rather than expand it.

Neither was caught by 6807 gate tests, three seats, or a nine-cell cross-family
review of that exact file. helm-claude found the first one only because a
freshly claimed lane room has no __pycache__ — which gives the defect a nasty
distribution: SILENT for the author who can fix it, NOISY for every seat that
claims a room afterward.

WHY tokenize AND NOT `python -W error::SyntaxWarning`. The obvious guard does
not work, and finding that out cost a wrong "all clear" on this very lane.
CPython reports invalid escapes at roughly one per STRING LITERAL, and both
defects above lived inside a single 268-line triple-quoted hook script. A
compile-based scan found :244, and the fix for :244 is what revealed :270 —
so a compile scan reports "clean" while defects remain in the same literal.
Reading the tokens sees every occurrence at once and does not dedupe.

WHAT IS EXEMPT. Raw literals (r"", rb"", Rf"" ...) are skipped: in a raw string
a backslash IS the literal character, which is exactly why regexes use them.
That exemption is the whole reason this check cannot be a plain text grep — a
naive scan over source lines flags every re.sub(r"\\s+") in the tree.

THE FIX IS NEVER "delete the backslash". These sequences are load-bearing for a
downstream consumer (a shell, a regex engine, a format string). Double the
backslash, or make the literal raw if nothing else in it needs escapes. Then
assert the CONSUMER still receives the same bytes — silencing the warning by
changing what the shell sees would be a regression wearing a fix's clothes.
"""
import os
import re
import tokenize
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Everything CPython accepts after a backslash inside a non-raw literal. A
# newline is the line-continuation form. Anything outside this set warns today
# and is scheduled to raise.
_VALID_ESCAPE = set("\n\\'\"abfnrtv01234567xNuU")

_SCAN_DIRS = ("helm", "tests", "bin")


def _sources():
    for base in _SCAN_DIRS:
        top = os.path.join(_ROOT, base)
        if not os.path.isdir(top):
            continue
        for dirpath, dirnames, filenames in os.walk(top):
            dirnames[:] = [d for d in dirnames
                           if d not in (".git", "__pycache__")]
            for name in filenames:
                if name.endswith(".py"):
                    yield os.path.join(dirpath, name)


def _invalid_escapes(path):
    """[(line, char)] for every invalid escape in a non-raw literal."""
    found = []
    with open(path, "rb") as fh:
        try:
            toks = list(tokenize.tokenize(fh.readline))
        except (tokenize.TokenError, SyntaxError):
            # An unparseable file is a different failure with its own louder
            # test; reporting it here as an escape defect would misattribute.
            return found
    for tok in toks:
        if tok.type != tokenize.STRING:
            continue
        prefix = re.match(r"[A-Za-z]*", tok.string).group(0).lower()
        if "r" in prefix:
            continue
        body = tok.string[len(prefix):]
        for m in re.finditer(r"\\(.)", body, re.S):
            if m.group(1) not in _VALID_ESCAPE:
                line = tok.start[0] + body[:m.start()].count("\n")
                found.append((line, m.group(1)))
    return found


def _planted_defect():
    """A file with one invalid escape, one raw literal, one valid escape.

    Written to disk rather than held as a string because _invalid_escapes
    tokenizes a PATH — a control that exercised a different entry point would
    prove something other than what the caller is about to trust.
    """
    import tempfile
    fh = tempfile.NamedTemporaryFile("w", suffix=".py", delete=False)
    with fh:
        fh.write('x = "a \\` b"\ny = r"a \\` b"\nz = "ok \\n"\n')
    return fh.name


class EscapeHygieneTest(unittest.TestCase):

    def setUp(self):
        self._planted = []

    def tearDown(self):
        for p in self._planted:
            try:
                os.unlink(p)
            except OSError:
                pass

    def test_no_invalid_escape_sequences_in_tree(self):
        # THE PLANT GOES THROUGH THE SAME LIST. The assertion that matters here
        # is that a findings list is EMPTY, and an empty list has two causes:
        # nothing is wrong, or the finder cannot find. Proving the detector on a
        # separate variable (or in a sibling test) leaves THIS list unproven —
        # so a known-bad file is scanned in the SAME pass, and `findings` is
        # asserted to contain it before the rest is asserted clean. The list is
        # demonstrably populatable, every run, or the test fails.
        probe = _planted_defect()
        self._planted.append(probe)

        findings = []
        scanned = 0
        for path in list(_sources()) + [probe]:
            scanned += 1
            for line, ch in _invalid_escapes(path):
                findings.append((path, "%s:%d  invalid escape \\%s"
                                 % (os.path.relpath(path, _ROOT), line, ch)))

        planted = [d for p, d in findings if p == probe]
        self.assertEqual(1, len(planted),
                         "the planted defect did not come back through this "
                         "list, so an empty real-tree result below would prove "
                         "nothing; got %r" % (planted,))
        # Second blindness guard, on the WALK rather than the detector: a scan
        # that silently walked nothing would still find the plant and pass. If
        # _SCAN_DIRS ever stops resolving, the zero below stops meaning "clean"
        # and starts meaning "blind".
        self.assertGreater(scanned, 50,
                           "scanned only %d files — the walk is broken, so a "
                           "clean result here proves nothing" % scanned)

        bad = [d for p, d in findings if p != probe]
        self.assertEqual([], bad, "\n".join(
            ["invalid escape sequences (SyntaxWarning today, SyntaxError "
             "later); double the backslash or use a raw literal, and verify "
             "the downstream consumer still receives the same bytes:"] + bad))

    def test_detector_discriminates_raw_from_non_raw(self):
        """The three-way discrimination, asserted as one exact equality.

        Line 1 is a non-raw literal with an invalid escape and MUST be flagged.
        Line 2 is the same bytes in a RAW literal and must NOT be — that
        exemption is the whole reason this check reads tokens instead of text,
        and a detector that lost it would flag every regex in the tree. Line 3
        is a valid escape and must not be flagged either.

        Asserting the exact list rather than truthiness is deliberate: a
        detector that flagged all three would satisfy any "did it find
        something" check while being useless.
        """
        probe = _planted_defect()
        self._planted.append(probe)
        hits = _invalid_escapes(probe)
        self.assertEqual([(1, "`")], hits,
                         "expected exactly the non-raw line 1: raw literals and "
                         "valid escapes must both stay clean; got %r" % (hits,))


if __name__ == "__main__":
    unittest.main()
