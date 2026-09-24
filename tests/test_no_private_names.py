"""helm/ carries none of the operator's private names.

The names of the operator's own projects, hosts and predecessor tools are
facts about one machine. Where helm's behaviour needs one, that machine's
local-names file supplies it (helm/localnames.py) and the source carries a
neutral default; where a name was only prose, the prose says what it meant
without it. This arm holds that line over every tracked file under helm/.

THE ARM CARRIES NO PRIVATE NAME IN THE CLEAR. An arm that spelled the names
it guards would put them into the tree it protects, so the list below is hex
of each name's UTF-8 bytes. That is an encoding, not a secret: it keeps the
names out of every grep, scanner and reader of the tree, and nothing more.
To add a name, append `"<name>".encode().hex()`.

WHAT COUNTS AS A HIT. Text splits into words at every character outside
[A-Za-z0-9], case folded, so `NAME_ROOTS`, `~/.cache/name/` and `NAME:` all
hit and `names` does not. A two-word name hits two words joined by exactly one
of `-`, `.` or `_`, so each of its spellings is one name.

NO EXEMPTION FOR A SEAT HANDLE. A private name inside a seat handle
(`@<name>-claude`, `by <name>-codex-2`) is a hit like any other: a comment
that credits a project's seat names the project.

A SHORT LOGIN IS A NAME TOO. A mailbox-shaped id whose domain is ONE label
(`<login>@<label>`, no dot after the `@`) is how an owner or seat login gets
written in passing, and neither this list nor the world-literals arm (which
reads only dotted domains) would see it. So every such id under helm/ is a
hit unless SINGLE_LABEL_ALLOWED names it with its reason: helm's own synthetic
git identities and the grammar placeholders its prose uses. A version pin
(`pkg@v1.4.143`), a reflog (`stash@{0}`) and a bare `@handle` are not the shape.

Each arm scans the tree AND one planted text in a single call and asserts
the result is exactly the planted hits: the scanner is proven able to see
every name, in every spelling, in the same observation that finds the tree
clean.
"""
import os
import re
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: The private names, hex of their UTF-8 bytes (see the module docstring).
NAMES_HEX = ("73657368", "6d63", "656d6261726b", "6275696c646572732d646576",
             "736e6f6f7079", "64726f6f7079", "686f6d656c6162", "6275696c6472",
             "706c61796170616c", "72616d7370616365", "7468696e6b706164",
             "7075672d70313473")
NAMES = tuple(bytes.fromhex(h).decode("utf-8") for h in NAMES_HEX)

_WORD = re.compile(r"[A-Za-z0-9]+")
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
    "helm@x": "a revision placeholder: the maker warning's \"you ran helm@X "
              "from tree@Y\" (helm/selfrepo.py)",
    "tree@y": "the same warning's other revision placeholder",
    "x@file": "the sweep reflex's prior-art template, \"prior-art: "
              "X@file:line\", agent-facing text (helm/actsteer.py)",
}


def hits_in(text, names=NAMES):
    """[(line number, name)] for each private name in `text`."""
    single = {n for n in names if "-" not in n}
    double = {tuple(n.split("-", 1)) for n in names if "-" in n}
    out = []
    for number, line in enumerate(text.splitlines(), 1):
        words = [(m.group(0).lower(), m.start(), m.end())
                 for m in _WORD.finditer(line)]
        found = []
        for i, (word, start, end) in enumerate(words):
            if word in single:
                found.append((word, start, end))
            if i + 1 < len(words):
                nxt, nstart, nend = words[i + 1]
                if nstart == end + 1 and line[end] in "-._" \
                        and (word, nxt) in double:
                    found.append(("%s-%s" % (word, nxt), start, nend))
        out.extend((number, name) for name, _s, _e in found)
    return out


def single_label_ids(text):
    """[(line number, id)] for each single-label id outside the allowed set."""
    return [(number, m.group(0))
            for number, line in enumerate(text.splitlines(), 1)
            for m in _SINGLE_LABEL.finditer(line)
            if m.group(0).lower() not in SINGLE_LABEL_ALLOWED]


def _production_files():
    """[(path, text)] for every tracked text file under helm/."""
    raw = subprocess.run(("git", "ls-files", "-z", "--", "helm/"), cwd=ROOT,
                         capture_output=True, check=True).stdout
    files = []
    for rel in raw.decode("utf-8", "replace").split("\0"):
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
    """[(path, line, name)] over every file."""
    return [(rel, n, name) for rel, text in files
            for n, name in hits_in(text)]


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
        for name in NAMES:
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
        must decode to a distinct lowercase word or two-word name."""
        self.assertEqual(len(set(NAMES)), len(NAMES_HEX))
        for name in NAMES:
            self.assertRegex(name, r"\A[a-z0-9]+(-[a-z0-9]+)?\Z")

    def test_helm_carries_no_short_login(self):  # noqa: VACUOUS_ASSERTION — the expected list is the planted hits, non-empty by construction, so an arm that saw nothing fails
        planted = (self.PLANTED, "\n".join((
            "the owner's someone@label row",         # 1 hit
            "held OWNER@Team and x.y@corp-2",         # 2 hit twice
            "gate@helm and Helm-Work@local",          # allowed
            "x@example.com and pkg@v1.4.143",         # dotted, a version
            "stash@{0} and %s@1 and (@codex",         # not the shape
            "<user>@<host> and d@ and hen@'s",        # no login or no label
        )) + "\n")
        hits = [(rel, n, ident) for rel, text in _production_files() + [planted]
                for n, ident in single_label_ids(text)]
        self.assertEqual(hits, [(self.PLANTED, 1, "someone@label"),
                                (self.PLANTED, 2, "OWNER@Team"),
                                (self.PLANTED, 2, "x.y@corp-2")],
                         "a short login ships in helm/: say what it was "
                         "without it, or add a synthetic one to "
                         "SINGLE_LABEL_ALLOWED with its reason")

    def test_every_allowed_short_id_is_still_in_the_tree(self):
        """An allowance nothing uses is a hole kept open for the next id of
        that spelling. Each one must still occur under helm/."""
        found = {m.group(0).lower() for _rel, text in _production_files()
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
        self.assertEqual(hits_in(text, names=("zork", "foo-bar")),
                         [(1, "zork"), (2, "zork"), (4, "zork"), (5, "zork"),
                          (6, "zork"), (7, "zork"), (8, "foo-bar"),
                          (8, "foo-bar"), (10, "zork"), (10, "zork")])


if __name__ == "__main__":
    unittest.main()
