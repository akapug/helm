"""helm/ carries none of the operator's private names.

The names of the operator's own projects, hosts and predecessor tools are
facts about one machine. Where helm's behaviour needs one, that machine's
local-names file supplies it (helm/localnames.py) and the source carries a
neutral default; where a name was only prose, the prose says what it meant
without it. This arm holds that line over every tracked file public-bound by
helm's own public_name rule, so a name that ships in tests/ or docs/ is as
much a violation as one that ships in helm/.

THE LIST AND THE SCANNER ARE NOT HERE. They live in helm/private_names.py,
which the pre-commit private-name rung also runs, so the suite and the commit
door read one list and one tokenizer: a name added there is refused by both,
and no copy here can drift from it. That module's docstring says why the
list is hex, what counts as a hit, and why a seat handle is no exemption.
`test_the_suite_and_the_rung_read_one_list` below plants a name in that one
place and proves both readers see it.

A SHORT LOGIN IS A NAME TOO. A mailbox-shaped id whose domain is ONE label
(`<login>@<label>`, no dot after the `@`) is how an owner or seat login gets
written in passing, and neither the list nor the world-literals arm (which
reads only dotted domains) would see it. So every such id under helm/ is a
hit unless SINGLE_LABEL_ALLOWED names it with its reason: helm's own synthetic
git identities and the grammar placeholders its prose uses. A version pin
(`pkg@v1.4.143`), a reflog (`stash@{0}`) and a bare `@handle` are not the shape.

Each arm scans the tree AND one planted text in a single call and asserts
the result is exactly the planted hits: the scanner is proven able to see
every name, in every spelling, in the same observation that finds the tree
clean.
"""
import ast
import importlib.util
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from helm import private_names

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: The release tool's module, loaded so the test scan and the release gate
#: exclude exactly the same paths from one parser (never a second copy).
_release_spec = importlib.util.spec_from_file_location(
    "_release_tool", os.path.join(ROOT, "scripts", "release", "release.py"))
_release = importlib.util.module_from_spec(_release_spec)
_release_spec.loader.exec_module(_release)

#: `<login>@<label>` where the label is one DNS label: nothing but a letter,
#: digits and hyphens, not followed by a dot and more of a domain.
_SINGLE_LABEL = re.compile(
    r"(?<![A-Za-z0-9._%+-])([A-Za-z0-9._+-]+)@([A-Za-z][A-Za-z0-9-]*)"
    r"(?![A-Za-z0-9-]|\.[A-Za-z0-9])")
#: The single-label ids helm/ may carry, each with why.
SINGLE_LABEL_ALLOWED = {
    "gate@helm": "the git identity `helm gate` commits its lane snapshots "
                 "under (helm/gate.py)",
    "helm-work@local": "the git identity the work actuator commits under "
                       "(helm/vcs.py, helm/obligation.py)",
    "team@project": "registry grammar: a legal registry name may carry `@` "
                    "(helm/registry.py)",
    "name@stamp": "registry grammar: the key of an entry authored against a "
                  "second path (helm/registry.py)",
    "account@domain": "the address needle class, described (helm/nevertrack.py)",
    "handle@domain": "the address needle class, described (helm/nevertrack.py)",
    "git@github": "GitHub's SSH user inside the escaped push-URL pattern "
                  "(helm/remote_session.py), not a login",
    "helm@x": "a revision placeholder: the maker warning's \"you ran helm@X "
              "from tree@Y\" (helm/selfrepo.py)",
    "tree@y": "the same warning's other revision placeholder",
    "x@file": "the sweep reflex's prior-art template, \"prior-art: "
              "X@file:line\", agent-facing text (helm/actsteer.py)",
    "room@epoch": "the pair meld's round citation grammar: `--meld "
                  "ROOM@EPOCH` binds one round (helm/review_door.py, "
                  "helm/dispatches.py)",
}

#: A synthetic name, never a real one, for the arms that plant a list entry.
PLANTED_NAME = "zorkbox"


def single_label_ids(text):
    """[(line number, id)] for each single-label id outside the allowed set."""
    return [(number, m.group(0))
            for number, line in enumerate(text.splitlines(), 1)
            for m in _SINGLE_LABEL.finditer(line)
            if m.group(0).lower() not in SINGLE_LABEL_ALLOWED]


def _production_files():
    """[(path, text)] for every tracked file public-bound by private_names,
    minus the paths the release leaves out. The current suite only scanned
    helm/; public-bound paths outside helm/ ship just the same, so the
    guard that holds over them must read the same set the guard itself
    decides. The omit list is read the way scripts/release/release.py does
    (one parser, never a second), and the omit file itself is left out, so
    the scan never counts the list it reads as its own violation."""
    tracked = subprocess.run(("git", "ls-files", "-z"), cwd=ROOT,
                             capture_output=True, check=True).stdout
    tracked = [rel for rel in tracked.decode("utf-8", "replace").split("\0")
               if rel]
    with open(os.path.join(ROOT, "scripts", "release", "omit.txt"),
              encoding="utf-8") as f:
        omit = _release.omit_entries(f.read()) + ["scripts/release/omit.txt"]
    files = []
    for rel in tracked:
        if not private_names.public_bound(rel):
            continue
        if _release.omitted(rel, omit):
            continue
        try:
            with open(os.path.join(ROOT, rel), "rb") as f:
                data = f.read()
        except OSError:
            continue
        if b"\0" in data[:8192]:
            continue
        files.append((rel, data.decode("utf-8", "replace")))
    return files


def _helm_files():
    """[(path, text)] for every tracked text file under helm/ only. Some
    arms were scoped to helm/ when they were written (the short-login
    test hardcodes the helm/ surface); the private-name arms above now
    scan every public-bound path, but this keeps the helm-only arms on
    the surface they name."""
    tracked = subprocess.run(("git", "ls-files", "-z", "--", "helm/"),
                             cwd=ROOT, capture_output=True, check=True).stdout
    files = []
    for rel in tracked.decode("utf-8", "replace").split("\0"):
        if not rel:
            continue
        try:
            with open(os.path.join(ROOT, rel), "rb") as f:
                data = f.read()
        except OSError:
            continue
        if b"\0" in data[:8192]:
            continue
        files.append((rel, data.decode("utf-8", "replace")))
    return files


def scan(files):
    """[(path, line, name)] over every file, by the one shared scanner."""
    return [(rel, n, name) for rel, text in files
            for n, name in private_names.hits_in(text)]


def _planted(name):
    """One must-hit line per spelling of `name`, the seat handles a credit
    would write among them, and the line that must miss beside them: the
    name inside a longer word."""
    if "-" in name:
        a, b = name.split("-", 1)
        hit = ["x = '%s-%s'" % (a, b), "see %s.%s" % (a, b),
               "%s_%s_usage" % (a.upper(), b)]
    else:
        hit = ["x = %s" % name, "%s_CONFIG_ROOTS" % name.upper(),
               "~/.cache/%s/" % name, "(%s-codex, a seat)" % name]
    hit += ["(@%s-claude, found it)" % name,
            "measured by %s-codex-2: it held" % name]
    miss = ["%sx and x%s" % (name.replace("-", ""), name.replace("-", ""))]
    return hit, miss


class NoPrivateNamesTest(unittest.TestCase):
    PLANTED = "<planted>"

    def test_helm_names_none_of_the_operators_private_names(self):  # noqa: VACUOUS_ASSERTION — the expected list is the planted hits, non-empty by construction (every name in every spelling), so an arm that saw nothing fails
        lines, expected = [], []
        for name in private_names.NAMES:
            hit, miss = _planted(name)
            for text in hit:
                lines.append(text)
                expected.append((self.PLANTED, len(lines), name))
            lines.extend(miss)
        planted = (self.PLANTED, "\n".join(lines) + "\n")
        self.assertEqual(scan(_production_files() + [planted]), expected,
                         "a private name ships in helm/: read it from this "
                         "host's local names (helm/localnames.py) or say "
                         "what it meant without it")

    def test_the_list_decodes_to_distinct_lowercase_names(self):  # noqa: VACUOUS_ASSERTION — the count equality and the per-name regex are positive assertions over a non-empty tuple
        """A misspelt hex entry would guard a name nobody uses. Each entry
        must decode to a distinct lowercase word or two-word name, and stay
        upper-case hex so the citation rung never reads it as a commit."""
        self.assertEqual(len(set(private_names.NAMES)),
                         len(private_names.NAMES_HEX))
        for name in private_names.NAMES:
            self.assertRegex(name, r"\A[a-z0-9]+(-[a-z0-9]+)?\Z")
        for h in private_names.NAMES_HEX:
            self.assertRegex(h, r"\A[0-9A-F]+\Z")
            self.assertEqual(h, h.upper())

    def test_helm_carries_no_short_login(self):  # noqa: VACUOUS_ASSERTION — the expected list is the planted hits, non-empty by construction, so an arm that saw nothing fails
        planted = (self.PLANTED, "\n".join((
            "the owner's someone@label row",         # 1 hit
            "held OWNER@Team and x.y@corp-2",         # 2 hit twice
            "gate@helm and Helm-Work@local",          # allowed
            "x@example.com and pkg@v1.4.143",         # dotted, a version
            "stash@{0} and %s@1 and (@codex",         # not the shape
            "<user>@<host> and d@ and hen@'s",        # no login or no label
        )) + "\n")
        hits = [(rel, n, ident) for rel, text in _helm_files() + [planted]
                for n, ident in single_label_ids(text)]
        self.assertEqual(hits, [(self.PLANTED, 1, "someone@label"),
                                (self.PLANTED, 2, "OWNER@Team"),
                                (self.PLANTED, 2, "x.y@corp-2")],
                         "a short login ships in helm/: say what it was "
                         "without it, or add a synthetic one to "
                         "SINGLE_LABEL_ALLOWED with its reason")

    def test_the_scan_reads_every_public_path_and_skips_what_the_release_omits(self):  # noqa: VACUOUS_ASSERTION — the positive assertIn loop runs over a literal four-path tuple
        """The scanned set is every tracked public-bound file minus the
        release's omit list: files outside helm/ that ship (a test module,
        the README, docs/) are in it, and no omitted path is. A planted name
        handed to scan() proves only the scanner; this proves what it reads."""
        scanned = {rel for rel, _text in _production_files()}
        for shipped in ("tests/test_no_private_names.py", "README.md",
                        "docs/VERBS.md", "helm/private_names.py"):
            self.assertIn(shipped, scanned)
        with open(os.path.join(ROOT, "scripts", "release", "omit.txt"),
                  encoding="utf-8") as f:
            omit = _release.omit_entries(f.read())
        self.assertTrue(omit)
        self.assertEqual({rel for rel in scanned if _release.omitted(rel, omit)},
                         set())
        self.assertNotIn("scripts/release/omit.txt", scanned)

    def test_every_allowed_short_id_is_still_in_the_tree(self):
        """An allowance nothing uses is a hole kept open for the next id of
        that spelling. Each one must still occur under helm/."""
        found = {m.group(0).lower() for _rel, text in _helm_files()
                 for m in _SINGLE_LABEL.finditer(text)}
        self.assertIn("gate@helm", found)       # the scan reads the tree
        self.assertEqual(sorted(set(SINGLE_LABEL_ALLOWED) - found), [])

    def test_every_shape_the_arm_names(self):
        """Both halves on a synthetic name, one line per shape, so a change
        to the tokenizer shows which rule moved."""
        text = "\n".join((
            "zork",                     # 1 hit: a bare word
            "ZORK_HOME=1",              # 2 hit: underscore ends a word
            "zorkish and unzork",       # miss: inside a longer word
            "(@zork-claude, found it)",  # 4 hit: a seat handle as an actor
            "found by zork-qwen-3",     # 5 hit: the `by` form
            "zork-claude said so",      # 6 hit: a seat name that is no actor
            "@zork-claudette",          # 7 hit: not a family tail
            "foo-bar and foo.bar",      # 8 hit twice: one two-word name
            "foo bar and foo--bar",     # miss: not joined by one separator
            "@zork-codex and zork",     # 10 hit twice: a handle and a word
        ))
        self.assertEqual(private_names.hits_in(text, names=("zork", "foo-bar")),
                         [(1, "zork"), (2, "zork"), (4, "zork"), (5, "zork"),
                          (6, "zork"), (7, "zork"), (8, "foo-bar"),
                          (8, "foo-bar"), (10, "zork"), (10, "zork")])


class OneListTest(unittest.TestCase):
    """The suite and the commit door read ONE list: a name planted in
    helm/private_names.py alone is seen by this module's scan and by the
    rung's staged scan, and neither sees it before it is planted."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-one-list-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        for args in (("init", "-q", "-b", "main"),
                     ("config", "user.email", "t@example.invalid"),
                     ("config", "user.name", "t")):
            self.git(*args)
        self.write("README", "seed\n")
        self.git("add", "README")
        self.git("commit", "-qm", "seed")
        self.write("helm/note.py", "# the %s box keeps the log\nX = 1\n"
                   % PLANTED_NAME)
        self.git("add", "helm/note.py")

    def git(self, *args):
        done = subprocess.run(("git",) + args, cwd=self.tmp,
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)

    def write(self, rel, text):
        path = os.path.join(self.tmp, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)

    def both(self):
        planted = ("<planted>", "the %s box keeps the log\n" % PLANTED_NAME)
        suite = scan([planted])
        rung, error, missing = private_names.scan_staged(self.tmp)
        self.assertIsNone(error)
        self.assertIsNone(missing)
        return suite, [(rel, n) for rel, n, _number, _text in rung]

    def test_the_suite_and_the_rung_read_one_list(self):
        self.assertEqual(self.both(), ([], []),
                         "control: an unlisted word is nobody's hit")
        hexed = PLANTED_NAME.encode().hex().upper()
        with mock.patch.object(private_names, "NAMES_HEX",
                               private_names.NAMES_HEX + (hexed,)), \
                mock.patch.object(private_names, "NAMES",
                                  private_names.NAMES + (PLANTED_NAME,)):
            self.assertEqual(self.both(),
                             ([("<planted>", 1, PLANTED_NAME)],
                              [("helm/note.py", 1)]))

    def test_this_module_keeps_no_list_of_its_own(self):
        """A second list is how the two readers drift: no module-level
        binding here may hold hex entries or decoded names."""
        with open(os.path.abspath(__file__).replace(".pyc", ".py"),
                  encoding="utf-8") as f:
            tree = ast.parse(f.read())
        bound = {t.id for node in tree.body if isinstance(node, ast.Assign)
                 for t in node.targets if isinstance(t, ast.Name)}
        self.assertIn("SINGLE_LABEL_ALLOWED", bound, "control: the walk "
                      "reads this module's bindings")
        self.assertFalse(bound & {"NAMES", "NAMES_HEX"}, bound)
        hexes = [node.value for node in ast.walk(tree)
                 if isinstance(node, ast.Constant) and isinstance(node.value, str)
                 and re.fullmatch(r"[0-9A-Fa-f]{6,}", node.value)]
        self.assertEqual(hexes, [])


if __name__ == "__main__":
    unittest.main()
