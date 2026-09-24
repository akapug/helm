"""The one door for argv that becomes a record, and the census that keeps it one.

A RULE WITHOUT A CENSUS IS A CONVENTION. Every surface that discovered this
hole cured it locally, so the rule existed three times with three refusal
texts, and the verb written next inherited the cured sibling's confidence
without its protection. The walker at the bottom is what makes the next one
impossible to write by accident; the arms above it are what make the door worth
routing to.
"""
import ast
import io
import pathlib
import unittest

from helm import freetext

HELM = pathlib.Path(__file__).resolve().parents[1] / "helm"


class TheScanStopsAtTheBody(unittest.TestCase):
    """SCOPE IS THE LEADING OPTION POSITIONS, NEVER THE BODY. Scanning a whole
    record made ordinary prose about flags unwritable."""

    def test_a_flag_shaped_leading_token_is_a_flag(self):
        self.assertEqual(freetext.scan(["--body", "hi"]),
                         (freetext.FLAG_SHAPED, 0))

    def test_a_bare_dashdash_is_the_escape(self):
        self.assertEqual(freetext.scan(["--", "--literal"]),
                         (freetext.ESCAPE, 0))

    def test_prose_that_merely_starts_with_a_dash_is_body(self):
        """`-` bullets, `->` arrows and em-dashes are text. Refusing these is
        how a first cut of this rule made "please use --force carefully"
        unsendable."""
        # The unconditional control: the scan DOES catch a flag-shaped token,
        # so the passes below are about these four shapes and not about a scan
        # that has stopped looking at anything.
        self.assertEqual(freetext.scan(["-f"]), (freetext.FLAG_SHAPED, 0))
        for token in ("- bullet", "-> arrow", "— dash", "-5 degrees"):
            with self.subTest(token=token):
                self.assertEqual(freetext.scan([token]), (freetext.BODY, 0))

    def test_a_flag_INSIDE_the_body_is_body(self):
        self.assertEqual(freetext.scan(["please", "use", "--force"]),
                         (freetext.BODY, 0))

    def test_nothing_to_look_at_is_its_own_answer(self):
        self.assertEqual(freetext.scan([]), (freetext.EXHAUSTED, 0))

    def test_start_skips_the_positionals_the_verb_already_took(self):
        """`dm <seat> <text...>` begins its options one past the RECIPIENT,
        which is the positional that made dm stop policing after one token."""
        self.assertEqual(freetext.scan(["seat", "--to", "x"], 1),
                         (freetext.FLAG_SHAPED, 1))


class TheTailRefusesAndTeaches(unittest.TestCase):

    def _tail(self, words, **kw):
        out = io.StringIO()
        text, rc = freetext.tail("helm task", "comment", list(words),
                                 "comment text", out=out, **kw)
        return text, rc, out.getvalue()

    def test_a_flag_the_verb_does_not_implement_is_refused_not_stored(self):
        text, rc, said = self._tail(["--reason", "done"])
        self.assertIsNone(text)
        self.assertEqual(rc, 2)
        self.assertIn("'--reason' is not comment text", said)

    def test_the_refusal_teaches_the_escape_in_the_same_breath(self):
        """BOTH HALVES SHIP TOGETHER OR NEITHER DOES. A refusal with no way
        through turns a corrupted record into a command nobody can run."""
        self.assertIn("put `--` before it", self._tail(["--reason", "x"])[2])

    def test_the_escape_writes_the_record_the_caller_meant(self):
        self.assertEqual(self._tail(["--", "--literal"])[0], "--literal")

    def test_ordinary_text_passes_through_joined(self):
        self.assertEqual(self._tail(["plain", "text"])[0], "plain text")

    def test_an_escape_with_nothing_after_it_is_refused(self):
        text, rc, said = self._tail(["--"])
        self.assertIsNone(text)
        self.assertEqual(rc, 2)
        self.assertIn("needs comment text after `--`", said)

    def test_the_arity_stays_with_the_caller(self):
        """An empty tail is (None, None), never a refusal: whether a verb may
        be called with no text is that verb's question, and answering it here
        would make every caller's own emptiness check dead code."""
        self.assertEqual(self._tail([]), (None, None, ""))

    def test_a_shared_hint_answers_the_question_that_was_actually_asked(self):
        self.assertIn("give the text as arguments",
                      self._tail(["--body", "x"])[2])

    def test_a_caller_hint_may_be_several_lines(self):
        """The chat `--help` refusal is the case that proves it: its usage line
        and its stdin line ARE the cure text, and a single-line hint dropped
        both when this rule was first shared."""
        said = self._tail(["--help"],
                          hints={"--help": ("first line", "second line")})[2]
        self.assertIn("first line", said)
        self.assertIn("second line", said)


class TheInPlaceDoorConsumesTheEscape(unittest.TestCase):

    def test_a_surviving_flag_refuses_and_leaves_argv_alone(self):
        args, out = ["post", "--body-stdin", "hi"], io.StringIO()
        rc = freetext.refuse_leading_flags(args, 1, ("--seat",), "post",
                                           "<text...>", "helm chat", out=out)
        self.assertEqual(rc, 2)
        self.assertEqual(args, ["post", "--body-stdin", "hi"])

    def test_the_escape_is_consumed_so_the_callers_join_sees_only_the_record(self):
        args = ["post", "--", "--literal"]
        self.assertIsNone(freetext.refuse_leading_flags(
            args, 1, (), "post", "<text...>", "helm chat"))
        self.assertEqual(args, ["post", "--literal"])

    def test_a_body_leaves_argv_untouched_and_permits_the_send(self):
        args = ["post", "hello"]
        self.assertIsNone(freetext.refuse_leading_flags(
            args, 1, (), "post", "<text...>", "helm chat"))
        self.assertEqual(args, ["post", "hello"])


# ---------------------------------------------------------------- the census

ARGVISH = ("rest", "args", "argv", "words", "tail", "extra", "leftover")

# The one verb that is DELIBERATELY unguarded, with the reason its own source
# gives. Anything else appearing here is a new hole, not a new entry.
EXEMPT = {
    ("helm/telegram.py", "cmd_telegram"):
        "declared in-source: this bridge is self-announcing, so a stray token "
        "arrives as text on the owner's phone where he can SEE it — unlike a "
        "dropped flag on `poll`, which would silently change what the verb did",
}


def _base(arg):
    node = arg
    for _ in range(4):
        if isinstance(node, ast.Subscript):
            node = node.value
        elif isinstance(node, ast.BinOp):
            node = node.left
        elif isinstance(node, ast.Call) and node.args:
            node = node.args[0]
        else:
            break
    return node.id if isinstance(node, ast.Name) else None


def _unguarded(source, name):
    """(file, fn) for every CLI dispatcher in `source` that joins a slice of
    argv into something that is NOT a diagnostic and never reaches the door.

    A DIAGNOSTIC IS NOT A RECORD. `print("... %s" % " ".join(rest))` renders an
    argv slice into a refusal the operator reads and nothing stores, so
    demanding the door there would be a census that fires on its own cure."""
    found = set()
    for fn in [n for n in ast.walk(ast.parse(source))
               if isinstance(n, ast.FunctionDef) and n.name.startswith("cmd")]:
        body = ast.unparse(fn)
        if "freetext" in body or "_free_text" in body \
                or "refuse_unknown_leading_flags" in body:
            continue
        for stmt in ast.walk(fn):
            if not isinstance(stmt, (ast.Assign, ast.Expr, ast.Return)):
                continue
            text = ast.unparse(stmt)
            if text.startswith("print(") or "file=sys.stderr" in text:
                continue
            for node in ast.walk(stmt):
                if isinstance(node, ast.Call) \
                        and isinstance(node.func, ast.Attribute) \
                        and node.func.attr == "join" \
                        and isinstance(node.func.value, ast.Constant) \
                        and any(_base(a) in ARGVISH for a in node.args):
                    found.add((name, fn.name))
                    break
    return found


class EveryFreeTextVerbReachesTheDoor(unittest.TestCase):

    def test_the_census_can_detect_the_defect_it_claims_to_ban(self):
        """THE MUST-HIT. A walker with a clean sweep and no positive control
        cannot tell "nothing is wrong" from "the detector is broken", and this
        one reports the first as the second every time it runs."""
        caught = _unguarded(
            "def cmd_demo(args):\n"
            "    note = ' '.join(args[1:])\n"
            "    return store(note)\n", "synthetic.py")
        self.assertEqual(caught, {("synthetic.py", "cmd_demo")})

    def test_a_diagnostic_render_is_not_counted(self):
        """The control for the exclusion above: without it the census would
        fire on refusal messages, which is every cured verb."""
        stored = ("def cmd_demo(args):\n"
                  "    note = 'bad: %s' % ' '.join(args[1:])\n"
                  "    return store(note)\n")
        printed = ("import sys\n"
                   "def cmd_demo(args):\n"
                   "    print('bad: %s' % ' '.join(args[1:]), file=sys.stderr)\n"
                   "    return 2\n")
        # The same join, once stored and once printed. The first must be
        # caught or the second's clean sweep says nothing.
        self.assertEqual(_unguarded(stored, "synthetic.py"),
                         {("synthetic.py", "cmd_demo")})
        self.assertEqual(_unguarded(printed, "synthetic.py"), set())

    def test_a_verb_that_reaches_the_door_is_not_counted(self):
        bare = ("def cmd_demo(args):\n"
                "    note = ' '.join(args[1:])\n"
                "    return store(note)\n")
        routed = ("def cmd_demo(args):\n"
                  "    note, rc = freetext.tail('helm x', 'demo', args[1:],"
                  " 'a note')\n"
                  "    return store(note)\n")
        # Same verb, same join, one line apart. The bare form must be caught
        # or the routed form's silence proves nothing about the routing.
        self.assertEqual(_unguarded(bare, "synthetic.py"),
                         {("synthetic.py", "cmd_demo")})
        self.assertEqual(_unguarded(routed, "synthetic.py"), set())

    def test_every_dispatcher_in_the_tree_routes_through_it_or_is_declared(self):  # noqa: VACUOUS_ASSERTION — test_the_census_can_detect_the_defect_it_claims_to_ban is this walker's positive control and runs unconditionally in the same class; EXEMPT is non-empty, so this is an equality against a known set rather than an absence assertion
        found = set()
        for path in sorted(HELM.glob("*.py")) + sorted(HELM.glob("*/*.py")):
            if path.name == "freetext.py":
                continue
            found |= _unguarded(path.read_text(encoding="utf-8"),
                                str(path.relative_to(HELM.parent)))
        self.assertEqual(
            found, set(EXEMPT),
            "a CLI verb joins an argv slice into a record without reaching "
            "helm.freetext. Route it through freetext.tail (or "
            "refuse_leading_flags if the verb keeps parsing in place), or add "
            "it to EXEMPT with the reason its own source gives.")


if __name__ == "__main__":
    unittest.main()
