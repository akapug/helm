#!/usr/bin/env python3
"""An instruction helm PRINTS must be one its reader can RUN.

The class, found by auditing outward from one instance. On 2026-07-31 the
stop-guard told a seat to `helm chat release <resource> --lease <id>` for a
lease the ACTUATOR had claimed on its behalf — the seat was never handed the
id, so the guard was demanding an action its target was structurally incapable
of performing (that instance is pinned in test_lease_recovery.py). Auditing
every other "run this" string in the package turned up no nonexistent verb,
but a sharper sub-class: a printed remediation that OMITS A FLAG THE VERB
REQUIRES. The verb resolves, so it fails at argument parsing with rc 2 — or,
worse, succeeds as a DRY RUN and changes nothing while reading like success.

Two of the four were the stated cure of a BLOCKING rung, which is the harm the
owner named: an instruction that cannot be followed is worse than silence,
because it teaches people to ignore the channel it came on. `helm/seats.py`
already diagnoses this class in prose ("An instruction that cannot be obeyed
is worse than no instruction: it is obeyed, and nothing happens") directly
above a string that reproduced it one flag over.

Each test here BINDS THE TWO HALVES TOGETHER: it proves the flag is really
required by running the verb WITHOUT it, and proves the printed instruction
carries that flag. Either half drifting turns the test red — which is the only
way this stays fixed, since nothing else connects a message to the parser it
is describing.
"""
import ast
import contextlib
import glob
import html
import importlib
import inspect
import io
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import textwrap
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import (chat, clarity, cli, dispatches, landreq, ownerasks, punt,  # noqa: E402
                  seats, verdicts)
from tests import _release  # noqa: E402
from tests._tmphome import declare as _tmp_declare  # noqa: E402

ENV_KEYS = ("HELM_HOME", "MELD_HOME", "HELM_CHAT_DIR", "MELD_CHAT_DIR",
            "HELM_CHAT_NAME", "MELD_CHAT_NAME", "HELM_CHAT_NODE_URL",
            "MELD_CHAT_NODE_URL", "HELM_CHAT_LOG", "MELD_CHAT_LOG",
            "HELM_CHAT_OWNER_NAMES", "HELM_CHAT_ROOM", "HELM_ADOPTED_DIR",
            "CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID", "CODEX_SESSION_ID",
            "HELM_PRIVATE_NEEDLES", "HELM_SCRATCH_GC")

_COMMAND_PATTERNS = (
    re.compile(r"`(helm\s+[^`\n]+)`", re.I),
    re.compile(r"<code>(helm\s+.*?)</code>", re.I),
    re.compile(r"^\s*\$\s*(helm\s+\S.*)$", re.I | re.M),
    re.compile(r"usage:\s*(helm\s+[^\n\"']+)", re.I),
)
_COMMAND_LABEL = re.compile(
    r"^\[?(helm\s+(?:%s|[a-z][a-z0-9-]*"
    r"(?:\s+[a-z][a-z0-9-]*)?))(?=:)",
    re.I,
)
_NON_SURFACE_DOCS = {
    "docs/CANON_CONTROLLED_LANGUAGE.md":
        "design vocabulary showing proposed store verbs",
    "docs/CANON_CONTROLLED_LANGUAGE_LANES.md":
        "design lanes showing proposed store verbs",
    "docs/ORCA_SEAM_AUDIT.md":
        "audit evidence names an absent seat-register proposal",
}
_NON_COMMAND_MENTIONS = {
    ("helm/orcaadopt.py", "helm coo"):
        "a truncated pane title quoted as incident evidence",
    ("docs/ENVIRONMENT.md", "helm remember"):
        "the sentence explicitly says this legacy verb does not exist",
    ("docs/VERBS.md", "helm manifest"):
        "the paragraph explicitly defers this proposed verb to v2",
    # The sidechain rung is ABOUT commands a shell assembles, so its
    # evidence has to SPELL the assembled shapes. Each of the four below is
    # quoted as the measured input to that rung — two obfuscations it must
    # catch, one fold of what bash actually ran, and one variable spelling
    # it deliberately lets pass — and none is a recipe a reader may run.
    ("helm/chat.py", "helm ch$(echo"):
        "a substitution-obfuscated beacon arm, quoted as the rung's input",
    ("helm/chat.py", "helm ch<mark>"):
        "the mark-folded form of that same input, quoted as what bash ran",
    ("docs/HOOKS.md", "helm ch$(echo"):
        "a substitution-obfuscated beacon arm, quoted as the rung's input",
    ("docs/HOOKS.md", "helm $V"):
        "a variable-spelled command the rung deliberately PASSES (measured)",
    ("helm/beacons.py", "helm '>'"):
        "a COUNTEREXAMPLE whose whole point is that it is NOT an invocation: "
        "the quoted redirection operator is an ARGUMENT, so the shell passes "
        "literal argv and runs no waiter. Spelling it any other way would "
        "stop it being the witness it is",
}
_FORMATTED_COMMAND_MENTIONS = {
    ("helm/clarity/__init__.py", "helm %s)"): "helm " + clarity._USAGE_CHECK,
}
_SUBVERBS = {}
_POSITIONAL_FIRST = {
    "sessions": "the first token is a free project selector; resume is optional",
}


def _handler(verb):
    fn = cli.VERBS[verb]
    closed = inspect.getclosurevars(fn).nonlocals
    if "module" in closed and "fn" in closed:
        module = importlib.import_module("helm." + closed["module"])
        return getattr(module, closed["fn"])
    return fn


def _arg0(node, params):
    return (
        isinstance(node, ast.Subscript)
        and isinstance(node.value, ast.Name)
        and node.value.id in params
        and isinstance(node.slice, ast.Constant)
        and node.slice.value == 0
    )


def _function_nodes(fn):
    out = []

    def walk(node):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)) \
                and node is not fn:
            return
        out.append(node)
        for child in ast.iter_child_nodes(node):
            walk(child)

    for stmt in fn.body:
        walk(stmt)
    return out


def _first_token_values(fn, namespace):
    """One body's literal first-argv-token comparisons, and where it hands on.

    -> (values, delegates). `delegates` are MODULE-LEVEL functions this body
    calls with its OWN argv, which is the one case where stopping here reads
    the door wrong: a handler that selects on args[0] and then passes the
    WHOLE argv to another module function has moved its verb table into that
    function, and a scan of the door alone sees only the verbs the door itself
    names. `helm dispatch` is that shape — its door opens a memo scope for the
    read verbs and delegates every verb — and without this hop its writes read
    as undocumented.
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn))).body[0]
    nodes = _function_nodes(tree)
    params = {a.arg for a in tree.args.args}
    names = set()
    for node in nodes:
        if not isinstance(node, ast.Assign):
            continue
        if len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Tuple) \
                and isinstance(node.value, ast.Tuple):
            for target, value in zip(node.targets[0].elts, node.value.elts):
                if isinstance(target, ast.Name) and _arg0(value, params):
                    names.add(target.id)
        elif any(_arg0(child, params) for child in ast.walk(node.value)):
            names.update(target.id for target in node.targets
                         if isinstance(target, ast.Name))

    values = set()

    def add(comparator):
        if isinstance(comparator, ast.Constant) \
                and isinstance(comparator.value, str):
            values.add(comparator.value)
        elif isinstance(comparator, (ast.Tuple, ast.List, ast.Set)):
            values.update(
                item.value for item in comparator.elts
                if isinstance(item, ast.Constant)
                and isinstance(item.value, str)
            )
        elif isinstance(comparator, ast.Name):
            value = namespace.get(comparator.id)
            if isinstance(value, (tuple, list, set, frozenset)):
                values.update(item for item in value
                              if isinstance(item, str))

    for node in nodes:
        if not isinstance(node, ast.Compare):
            continue
        if not (_arg0(node.left, params)
                or isinstance(node.left, ast.Name) and node.left.id in names):
            continue
        for comparator in node.comparators:
            add(comparator)

    delegates = []
    for node in nodes:
        if not isinstance(node, ast.Call):
            continue
        callee = namespace.get(getattr(node.func, "id", None))
        if not inspect.isfunction(callee) or callee is fn:
            continue
        # THE WHOLE ARGV, not a tail. `cmd_mix(rest)` receives a SLICE and
        # parses a different grammar; only a call carrying the token this body
        # branched on can still be routing the same first token.
        if any(isinstance(arg, ast.Name) and (arg.id in params or arg.id in names)
               for arg in node.args):
            delegates.append(callee)
    return values, delegates


def _subverbs(verb):
    """Literal branches reached from the handler's first argv token.

    The root table is authoritative for the first token. This derives the next
    token from the registered handler itself rather than maintaining a second
    table: names assigned from args[0], direct args[0] comparisons, and module
    constants used by those comparisons all count. A handler that hands its
    whole argv to another MODULE-LEVEL function is followed ONE HOP, because
    that is where its verb table went; the transitive closure is deliberately
    NOT chased, since every extra body only widens the accepted set and a scan
    wide enough to accept anything stops catching an undocumented verb. Nested
    functions are not followed at all — their argv belongs to a different
    parser scope.
    """
    if verb in _SUBVERBS:
        return _SUBVERBS[verb]
    handler = _handler(verb)
    values, delegates = _first_token_values(handler, handler.__globals__)
    for fn in delegates:
        more, _ = _first_token_values(fn, fn.__globals__)
        values |= more
    got = frozenset(
        value for value in values
        if re.fullmatch(r"[a-z][a-z0-9-]*", value)
    )
    _SUBVERBS[verb] = got
    return got


def _command_mentions(root):
    """Command-shaped public text: code spans, console lines and CLI labels."""
    paths = [root / "README.md"] if (root / "README.md").is_file() else []
    for base in (root / "helm", root / "docs"):
        for path in base.rglob("*"):
            rel = str(path.relative_to(root))
            if path.is_file() and rel not in _NON_SURFACE_DOCS \
                    and (path.suffix in {".py", ".md", ".html", ".js"}
                         or path.name.endswith(".part")):
                paths.append(path)

    out = []
    for path in paths:
        rel = str(path.relative_to(root))
        text = path.read_text(encoding="utf-8", errors="ignore")
        for pattern in _COMMAND_PATTERNS:
            for match in pattern.finditer(text):
                out.append((rel, text.count("\n", 0, match.start()) + 1,
                            html.unescape(match.group(1)).strip()))
        if path.suffix == ".py":
            try:
                tree = ast.parse(text)
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Constant) \
                        or not isinstance(node.value, str):
                    continue
                for line in node.value.splitlines():
                    match = _COMMAND_LABEL.match(line.strip())
                    if match:
                        out.append((rel, node.lineno, match.group(1)))
        else:
            for match in re.finditer(
                    r"[\"']\[?(helm\s+(?:%s|[a-z][a-z0-9-]*"
                    r"(?:\s+[a-z][a-z0-9-]*)?))(?=:)", text, re.I):
                out.append((rel, text.count("\n", 0, match.start()) + 1,
                            match.group(1)))
    return list(dict.fromkeys(out))


def _command_problem(command):
    words = re.sub(r"\s+", " ", command).strip().split()
    if len(words) < 2:
        return None
    root = words[1].strip(".,:;()[]{}")
    if root in {"...", "…", "#", "<verb>", "--human", "--help",
                "--version", "-V", "version", "help"}:
        return None
    if root not in cli.VERBS:
        return "unknown root verb %r" % root
    if len(words) < 3:
        return None
    token = words[2].strip(".,:;()[]{}")
    if not token or token.startswith(("-", "<", "{", "%")) \
            or token in {"...", "…"}:
        return None
    known = _subverbs(root)
    if not known or root in _POSITIONAL_FIRST:
        return None
    choices = re.split(r"[|/]", token)
    missing = [choice for choice in choices
               if re.fullmatch(r"[a-z][a-z0-9-]*", choice)
               and choice not in known]
    return "unknown %s subverb(s): %s" % (root, ", ".join(missing)) \
        if missing else None


class InstructionBase(unittest.TestCase):
    """Hermetic: a scratch HELM_HOME per test. Nothing here reads or writes
    the real estate, and no live lease is ever touched."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-instr-")
        self.prior = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_ADOPTED_DIR"] = os.path.join(self.tmp, "adopted")
        os.makedirs(os.environ["HELM_ADOPTED_DIR"])
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "chat")
        os.environ["HELM_CHAT_NODE_URL"] = ""
        os.environ["HELM_CHAT_LOG"] = "0"
        os.environ["HELM_CHAT_ROOM"] = "main"
        # DECLARED *AND* CORROBORATED: catchup is an ACT door, and the probe
        # seat has to be somebody rather than merely named.
        _tmp_declare(self, "seat-under-test")
        os.environ["HELM_SCRATCH_GC"] = "0"
        os.environ["HELM_PRIVATE_NEEDLES"] = os.path.join(self.tmp, "none.txt")

    def tearDown(self):
        for k, v in self.prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_cmd(self, fn, args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = fn(list(args))
        return rc, out.getvalue(), err.getvalue()


class PuntGateCureRunsTest(InstructionBase):
    """The PUNT GATE is a Stop-hook BLOCK. Its whole design is 'file the ask
    and the gate opens' — so the filing command it prints has to work."""

    PUNT = "I did not restart the node because it would disrupt the fleet."

    def test_asks_add_really_refuses_without_needs(self):
        rc, _out, err = self.run_cmd(ownerasks.cmd_asks,
                                     ["add", "some deferred thing"])
        self.assertEqual(rc, 2, "the flag is not required — retire this test")
        self.assertIn("--needs", err)

    def test_the_gate_prints_that_flag(self):
        """Asserts the COMMAND FORM, not the mere presence of the string
        '--needs'. A mutation that removed the flag from the command survived
        an `assertIn("--needs", text)` because the sentence EXPLAINING the
        requirement still contained the word — a test that reads the prose
        about a fix instead of the fix."""
        text = " ".join(punt.gate_lines(self.PUNT, open_ask=False))
        self.assertIn('helm asks add "<what you are not doing and why>" '
                      '--needs "<what only the owner can supply>"', text)

    def test_the_printed_cure_actually_opens_the_gate(self):
        """The effect, end to end: run the gate's OWN command, then re-derive
        the gate. A cure that does not clear the block is not a cure."""
        self.assertTrue(punt.gate_lines(self.PUNT, open_ask=False))
        rc, out, err = self.run_cmd(ownerasks.cmd_asks, [
            "add", "waiting on a credential",
            "--needs", "the API key only the owner holds"])
        self.assertEqual(rc, 0, err)
        self.assertIn("open:", out)
        self.assertEqual(punt.gate_lines(self.PUNT), [])   # reads the ledger


class OverdueDispatchCureRunsTest(InstructionBase):
    def test_verdict_really_refuses_without_a_polarity_flag(self):
        rc, _out, err = self.run_cmd(
            dispatches.cmd_dispatch,
            ["verdict", "a" * 32, "b" * 40, "whole-suite Ran 1 OK"])
        self.assertEqual(rc, 2, "polarity is not required — retire this test")
        self.assertIn("DECLARE the polarity", err)

    def test_the_overdue_whisper_prints_that_flag(self):
        """`_dispatch_candidate`'s check-in rung ends in a verdict command;
        it named no polarity, so following it verbatim exited 2. The rung is
        driven by ledger state a unit test cannot cheaply age into OVERDUE, so
        this reads the emitted template out of the function's own source."""
        body = inspect.getsource(seats._dispatch_candidate)
        self.assertIn("helm dispatch verdict %s %s", body)
        head, _sep, tail = body.partition("helm dispatch verdict %s %s")
        # THE WORK POLARITIES, NOT THE WHOLE VOCABULARY. This rung tells a
        # reviewer how to CLOSE an overdue row, so it must name every polarity
        # that closes one and no polarity that does not. `concur` records an
        # endorsement and authorizes nothing — offering it here would hand
        # someone a verb that leaves the row exactly as overdue as it was.
        # (Caught by this test when concur joined POLARITIES: the assertion was
        # reading the vocabulary register where it meant the authorization set.)
        for flag in verdicts.WORK_POLARITIES:
            self.assertIn("--" + flag, tail[:200],
                          "the check-in rung omits --%s" % flag)
        self.assertNotIn("--concur", tail[:200],
                         "the check-in rung offers a polarity that closes nothing")


class InboxCureRunsTest(InstructionBase):
    """`helm chat catchup` is DRY-RUN by default. Every rung that named it as
    the way to park an obligation named the form that parks nothing — the
    worst shape in this class, because it exits 0 and reads like it worked."""

    def test_catchup_without_apply_parks_nothing(self):
        """The parser half, MEASURED: the exact form the rungs named is a
        dry run that parks nothing and still exits 0."""
        from helm import chat
        seats.join(session="s-inbox", seat="seat-under-test", cwd=self.tmp)
        chat.post("@seat-under-test please look at this", who="someone-else",
                  sign=False)
        rc, out, err = self.run_cmd(
            lambda a: seats.cmd("catchup", a, "main"), ["--including-mentions"])
        self.assertEqual(rc, 0, err)
        self.assertIn("DRY RUN", out)
        self.assertIn("--apply", out)

    def test_the_inbox_block_names_apply(self):
        """The RENDERED block, and the exact command form. Asserting only
        that '--apply' appears somewhere let a mutation that stripped the flag
        off the command survive, because the clause explaining the flag still
        mentioned it."""
        from helm import chat
        seats.join(session="s-block", seat="seat-under-test", cwd=self.tmp)
        chat.post("@seat-under-test this is an obligation", who="someone-else",
                  sign=False)
        blocks, _warns = seats.stop_guard(session="s-block", room="main",
                                          seat="seat-under-test", cwd=self.tmp)
        inbox = [b for b in blocks if "undelivered message(s)" in b]
        self.assertEqual(len(inbox), 1, blocks)
        self.assertIn("helm chat catchup --including-mentions --apply",
                      inbox[0])

    def test_the_beacon_drain_hint_names_apply_too(self):
        """The same sentence is streamed by the beacon drain, a polling loop
        with no return value to assert on — so this reads the module source.
        Weaker than a run, and said out loud rather than dressed up; it exists
        so a future edit cannot fix one copy and leave the other behind."""
        # THE PACKAGE, NOT `seats.__file__`. This read the facade by name, and
        # the split moved BOTH copies into helm/seats_join.py — so the facade
        # holds zero and the count assertion below failed while the property it
        # guards was perfectly intact. Same defect the two scans above were
        # rewritten for, in the one test that still pinned a single file: a
        # sentence can be present twice in the package and absent from the
        # module this test happened to name.
        pkg = os.path.dirname(seats.__file__)
        mods = sorted(glob.glob(os.path.join(pkg, "seats*.py")))
        # CONTROL, unconditional: an empty glob makes assertNotIn vacuous and
        # makes the count 0, which is a FAILURE that looks like a real one.
        self.assertTrue(mods, "the seats-package glob found nothing, so this "
                              "test measures the filesystem and not the code")
        self.assertIn("seats.py", [os.path.basename(m) for m in mods],
                      "the facade itself was not scanned")
        src = "\n".join(open(m, encoding="utf-8").read() for m in mods)
        # the OLD, park-nothing form must not survive anywhere in the package
        self.assertNotIn("catchup --including-mentions` parks", src)
        self.assertEqual(
            src.count("helm chat catchup --including-mentions --apply"), 2)


class HelpTableMatchesTheParserTest(InstructionBase):
    """`helm help <verb>` is where someone looks up a form BEFORE typing it.
    Both entries carried the pre-requirement form of a flag their own module
    USAGE had already been corrected for."""

    def test_asks_help_carries_the_required_needs_flag(self):
        self.assertIn("--needs", cli._VERB_HELP["asks"])
        self.assertIn("--needs", ownerasks.USAGE)

    def test_dispatch_help_carries_the_required_polarity_flag(self):
        entry = cli._VERB_HELP["dispatch"]
        self.assertIn("verdict <id-or-unique-prefix> <full-reviewed-tip> "
                      "--approve|--fix|--supersede", entry)
        for flag in dispatches.POLARITIES:
            self.assertIn("--" + flag, entry)


class RespawnRecipeNamesWhatTheRowCarriesTest(InstructionBase):
    """`helm rearm`'s advisory rows are ANY pre-HEAD long-lived helm process
    and carry {pid, verb, start, cmdline} — no `seat` key exists on them, yet
    the recipe asked the reader to substitute one."""

    def test_advisory_rows_carry_no_seat_to_substitute(self):
        from helm import rearm
        src = rearm.__file__
        with open(src, encoding="utf-8") as f:
            text = f.read()
        self.assertIn('advisory.append({"pid": pid, "verb": verb', text)
        self.assertNotIn("helm seat down <seat> && helm seat up <seat>", text)
        self.assertIn("each row above names its PID", text)


class AdvertisedVerbResolutionTest(unittest.TestCase):
    """A command-shaped surface resolves through the dispatch table it names."""

    def test_every_advertised_root_and_subverb_resolves(self):  # noqa: VACUOUS_ASSERTION — extractor has separate synthetic controls
        root = Path(__file__).resolve().parent.parent
        for path, reason in _NON_SURFACE_DOCS.items():
            # A design document the release export omits has no grammar to
            # scan or exempt there; it is judged wherever it is present.
            omitted = _release.omitted(path)
            if omitted:
                with self.subTest(doc=path):
                    self.skipTest(omitted)
                continue
            text = (root / path).read_text(encoding="utf-8")
            self.assertTrue(
                any(pattern.search(text) for pattern in _COMMAND_PATTERNS),
                "%s no longer contains proposed command grammar; remove its "
                "scan exemption (%s)" % (path, reason),
            )
        for verb in _POSITIONAL_FIRST:
            self.assertIn(verb, cli.VERBS)
            self.assertTrue(_subverbs(verb))
        mentions = _command_mentions(root)
        self.assertGreater(len(mentions), 500,
                           "the extractor stopped seeing the public surfaces")
        exempt_seen = set()
        formatted_seen = set()
        bad = []
        for path, line, command in mentions:
            words = re.sub(r"\s+", " ", command).strip().split()
            key = (path, " ".join(words[:2])) if len(words) >= 2 else None
            if key in _NON_COMMAND_MENTIONS:
                exempt_seen.add(key)
                continue
            if key in _FORMATTED_COMMAND_MENTIONS:
                formatted_seen.add(key)
                command = _FORMATTED_COMMAND_MENTIONS[key]
            problem = _command_problem(command)
            if problem:
                bad.append("%s:%d: %s (%s)" % (
                    path, line, command, problem))
        self.assertEqual(
            set(_NON_COMMAND_MENTIONS) - exempt_seen, set(),
            "a non-command exemption went stale; delete it rather than widening")
        self.assertEqual(
            set(_FORMATTED_COMMAND_MENTIONS) - formatted_seen, set(),
            "a formatted command moved; update the exact interpolation resolver")
        self.assertEqual(
            bad, [],
            "command-shaped text promises verbs the registered parser does not "
            "dispatch:\n" + "\n".join(bad))

    def test_the_chat_synopsis_names_every_node_verb_its_own_usage_offers(self):
        """A SYNOPSIS THAT DENIES A WORKING VERB SENDS AN OPERATOR AWAY FROM A
        CURE. `helm/chat.py` answers the dry-faucet degrade by telling the
        operator to run `helm chat node refuel`, and the top-level chat
        synopsis listed only up, down and status — so a reader who checks the
        help concludes the cure names a verb that does not exist. Both verbs
        work; the defect was discoverability, and it was found by a fresh seat
        following that exact cure line.

        THE TWO SIDES MUST AGREE, so this reads BOTH rather than asserting a
        literal: `chatnode._usage()` is the subcommand's own offer and the
        synopsis is what a reader checks first."""
        from helm import chatnode
        offered = re.search(r"node <([a-z|]+)>", chatnode._usage())
        self.assertTrue(offered, "MUST-HIT: chatnode._usage() still names its "
                                 "verb set in the shape this reads")
        verbs = offered.group(1).split("|")
        self.assertIn("refuel", verbs, "MUST-HIT: the verb the cure names")
        # THE WHOLE RUN, not verb-by-verb: the node verbs sit inside a longer
        # pipe-joined chat synopsis with no delimiter marking where they end,
        # so a per-verb substring search matches other chat verbs by accident
        # and misses the FIRST one, which follows a space rather than a pipe.
        # Comparing the exact run is both precise and order-faithful, and the
        # usage string is the source of that order.
        synopsis = cli._VERB_HELP["chat"]
        self.assertIn("node " + "|".join(verbs), synopsis,
                      "the chat synopsis must offer exactly the node verbs "
                      "chatnode._usage() does, in its order — a synopsis that "
                      "denies a working verb sends an operator away from a "
                      "cure that names it")

    def test_the_resolver_rejects_both_levels_it_claims_to_cover(self):
        self.assertIn("unknown root", _command_problem("helm invented"))
        self.assertIn("unknown root", _command_problem("helm %s"))
        self.assertIn("unknown seat subverb",
                      _command_problem("helm seat peek"))
        self.assertIn("unknown mcp subverb",
                      _command_problem("helm mcp inspect"))
        self.assertIsNone(_command_problem("helm seat where <seat>"))
        self.assertIsNone(_command_problem("helm sessions myproject"))

    def test_new_literal_and_dynamic_runtime_labels_enter_the_scan(self):
        with tempfile.TemporaryDirectory(prefix="helm-test-verb-surface-") as d:
            root = Path(d)
            (root / "helm").mkdir()
            (root / "docs").mkdir()
            (root / "helm" / "future.py").write_text(
                'print("helm seat peek: impossible")\n'
                'print("helm %s: impossible" % "seat")\n', encoding="utf-8")
            mentions = _command_mentions(root)
        self.assertEqual(
            set(mentions),
            {
                ("helm/future.py", 1, "helm seat peek"),
                ("helm/future.py", 2, "helm %s"),
            },
        )
        problems = [_command_problem(command) for _path, _line, command in mentions]
        self.assertTrue(any("unknown seat subverb" in problem
                            for problem in problems))
        self.assertTrue(any("unknown root" in problem for problem in problems))


class PrintedCommandGrammarTest(InstructionBase):
    """Exact argv beyond token existence: flags and dependent operands."""

    def test_the_land_pipeline_hint_uses_a_parser_accepted_flag(self):
        bad, _out, err = self.run_cmd(landreq.cmd_lr, ["list", "--landed"])
        self.assertEqual(bad, 2)
        self.assertIn("unknown arg '--landed'", err)
        good, out, err = self.run_cmd(landreq.cmd_lr,
                                      ["list", "--all", "--help"])
        self.assertEqual(good, 0, err)
        self.assertIn("lr list [--all]", out + err)

    def test_seat_filtered_chat_read_requires_the_dm_mode(self):
        bad, _out, err = self.run_cmd(
            chat.cmd_chat,
            ["read", "--seat", "synthetic-seat"])
        self.assertEqual(bad, 2)
        self.assertIn("unknown arg '--seat'", err)
        good, _out, err = self.run_cmd(
            chat.cmd_chat,
            ["read", "--dm", "--seat", "synthetic-seat"])
        self.assertEqual(good, 0, err)

    def test_non_token_grammar_misstatements_do_not_regrow(self):  # noqa: VACUOUS_ASSERTION — required forms are positive controls
        from helm import toolwhisper
        root = Path(__file__).resolve().parent.parent
        # PINS FOLLOW THE CODE, THEY DO NOT ENUMERATE IT. A hand-written
        # tuple goes BLIND the moment a module is split: measured on this very
        # line, helm/seat.py had FOURTEEN siblings and this tuple named three,
        # so eleven files of moved code went unscanned and the rung stayed
        # green. The seats.py split was about to do it again.
        #
        # Globbing the family means a split cannot silently shrink the scan,
        # and a NEW sibling is covered the day it lands.
        # EVERY SPLITTABLE FAMILY IS A GLOB, and the coverage assertion is
        # PER FAMILY. A review broke one version by adding
        # helm/meld_status.py carrying a forbidden form: it went unseen,
        # because "helm/meld.py" was an EXACT PATH while my only control was a
        # GLOBAL count — and seat*.py alone supplies sixteen hits, so that
        # count passed no matter what meld did. An assertion satisfiable by a
        # DIFFERENT family than the one that regressed is not a control; it is
        # decorative, which is the exact class I keep writing premises about.
        #
        # SINGLE is declared, never inferred. A file with no siblings today is
        # marked so deliberately, and if it ever grows a family the glob picks
        # them up without anyone remembering this line.
        families = (("helm/meld*.py", "family"),
                    ("helm/seat*.py", "family"),
                    ("helm/premise/*.py", "family"),
                    ("helm/web_ui/views/00-home.html.part", "single"),
                    ("docs/VERBS.md", "single"))
        paths = []
        for pat, kind in families:
            hits = sorted(root.glob(pat))
            self.assertTrue(hits, "pattern matched nothing: %s" % pat)
            if kind == "family":
                # THE CONTROL IS ABOUT THE PATTERN, NOT TODAY'S FILESYSTEM.
                # My first version asserted len(hits) > 1 and it was wrong:
                # helm/meld.py has no siblings YET, so it demanded a fact
                # about the current tree when the property I want is
                # structural — that a splittable family is expressed as a
                # GLOB, so the day it gains a sibling that sibling is scanned
                # without anyone editing this line. That is what makes
                # The review's break (helm/meld_status.py carrying a forbidden
                # form) impossible: the glob picks it up on its own.
                self.assertIn(
                    "*", pat,
                    "family %s is an EXACT PATH, so a sibling of it would go "
                    "unscanned the day a split creates one — which is the "
                    "whole defect this test was rewritten to close" % pat)
            paths.extend(hits)
        text = "\n".join(pth.read_text(encoding="utf-8") for pth in paths)
        whisper = next(rule["say"] for rule in toolwhisper.RULES
                       if rule["id"] == "memoryhole-write")
        forbidden = (
            "helm %s:",
            "helm seat launch/resume",
            "helm dispatch send ...",
            "<code>helm premise-check</code>",
        )
        self.assertEqual([s for s in forbidden if s in text], [])
        self.assertNotIn("premise|heuristic <id>", whisper)
        for required in (
                "helm chat %s:",
                "helm seat launch <seat>",
                "helm dispatch send <recipient> <lane>",
                "<code>helm premise-check &lt;id&gt;</code>"):
            self.assertIn(required, text)
        self.assertIn(
            "helm store add premise '<id> | <statement> | <keywords>'",
            whisper)


if __name__ == "__main__":
    unittest.main()
