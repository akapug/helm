"""The surface-wiring rung: every subverb the code accepts, a surface prints.

THE CLASS (#289, owner-named: "built not wired is a pervasive gremlin that
must be rooted out at all times"): each verb has TWO hand-written discovery
surfaces — cli._VERB_HELP[verb] (what `helm <verb> --help` prints; cli.py
prefers it over fn.__doc__, so a docstring-only mention is NEVER printed) and
the module's own usage strings — and nothing derives either from the parser.
A 5-agent sweep measured the drift: a whole council-voting surface reachable
only by typing a WRONG subverb, the tmpfs journal's only rebuild path named
nowhere, a usage string omitting a flag its own file accepts two lines up.

THE CONTRACT this rung pins: every string a dispatcher compares its argv HEAD
against appears in the ROOT synopsis (`_VERB_HELP[verb]` — the fleet's
existence probe), except entries in ALLOWED, where each exemption carries its
reason and its death condition. The sweep test derives the accepted set from
the AST — the NOARG_VERBS technique — so a new hidden subverb is a test
failure at write time, not a discovery incident months later.

CARRIED SCANNER BUGS (each inverted a result before a MUST-HIT caught it,
2026-08-05; the fixtures below pin all three so a rewrite cannot shed them):
  1. tuple-unpack dispatch (`verb, rest = args[0], args[1:]`) — a Name-target
     -only scan returned CLEAN for the two verbs with the MOST hidden surface;
  2. `not in (...)` allowlists ARE acceptance decisions — excluding NotIn hid
     `helm eval register`;
  3. computed tokens have NO string literal (`"--" + p` over a tuple) — a
     literal-only scan reports them unaccepted, exactly backwards. (Flags are
     NOT gated here — measured 791 findings on the live tree, a false-alarm
     rate house policy refuses in a gate — but the const-indirection shape
     applies to subverb tuples too and is pinned.)
"""
import ast
import os
import re
import unittest


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(REPO, "helm")

BAREWORD = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$")
STOP = frozenset(("json", "true", "false", "none", "ok", "yes", "no", "on",
                  "off", "all"))

# The bare-help convention: `<verb> help` is accepted as a synonym for --help
# across small verbs (board/eval/landgate/note/pi). Universal, not drift.
UNIVERSAL = frozenset(("help",))

# PARSERS EXCLUDED WITH CAUSE — these functions bind a head var from argv that
# belongs to a FOREIGN PROCESS, not to helm's own CLI, so their comparison
# strings are runtime/exec vocabulary, never subverbs. Both were hand-refuted
# in the sweep; excluding the VERB (not the token) keeps the reason attached.
#   fleet:  _ORCA_RUNTIMES process detection (fleet.py) — electron|node|orca…
#   wiring: dispatch-rebind admission parses a COMMAND's argv (exec|command|env)
EXCLUDED_VERBS = {"fleet": "process-runtime argv parser (_ORCA_RUNTIMES)",
                  "wiring": "rebind admission parses a foreign command argv"}

# THE ALLOWLIST — accepted subverbs deliberately absent from the ROOT synopsis.
# Every entry carries (verb, token): reason + the condition that DELETES it.
# The companion assertion below fails when an entry stops matching a real
# accepted token, so an exemption cannot outlive its subject (a ratchet whose
# denominator nobody rechecks reads healthy forever).
ALLOWED = {
    # hook-facing chat subverbs: invoked by settings.json hooks, never typed
    # by a person; named in the module refusal list, kept OUT of the root
    # synopsis so the human surface stays scannable. DIES: if any becomes a
    # human verb, name it in the synopsis and drop the entry.
    ("chat", "argv-guard"): "hook-facing (PreToolUse argv guard)",
    ("chat", "delegation-stop"): "hook-facing (Stop-hook delegation guard)",
    ("chat", "stop-guard"): "hook-facing (Stop-hook gate)",
}

FLAG = re.compile(r"^--[a-z][a-z0-9]*(-[a-z0-9]+)*$")

# HOUSE-WIDE OPTIONS, documented once rather than on every clause that takes
# them. Naming them per-clause would triple the synopses to say nothing, and
# an agent who guesses `--json` is right everywhere it is accepted. DIES: if
# one stops being universal, drop it here and the clauses start owing it.
UNIVERSAL_FLAGS = frozenset(("--json", "--help", "--repo", "--seat", "--room",
                             "--force", "--apply"))

# THE FLAG ALLOWLIST — (verb, subverb, flag) accepted but deliberately absent
# from that subverb's clause. Same contract as ALLOWED above: a reason and a
# death condition, and the companion assertion fails when an entry stops
# matching a real accepted flag so an exemption cannot outlive its subject.
ALLOWED_FLAGS = {}


def _cli_tables():
    tree = ast.parse(_read(os.path.join(PKG, "cli.py")))
    verb_mod, verb_help = {}, {}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Assign)
                and isinstance(node.targets[0], ast.Name)):
            continue
        name = node.targets[0].id
        if name == "VERBS" and isinstance(node.value, ast.Dict):
            for k, v in zip(node.value.keys, node.value.values):
                if isinstance(v, ast.Call) \
                        and getattr(v.func, "id", "") == "_lazy":
                    verb_mod[k.value] = (v.args[0].value, v.args[1].value)
                elif isinstance(v, ast.Name):
                    verb_mod[k.value] = ("cli", v.id)
        if name == "_VERB_HELP" and isinstance(node.value, ast.Dict):
            for k, v in zip(node.value.keys, node.value.values):
                if isinstance(v, ast.Constant):
                    verb_help[k.value] = v.value
    return verb_mod, verb_help


def _modfiles(mod):
    p = os.path.join(PKG, mod + ".py")
    if os.path.isfile(p):
        return [p]
    d = os.path.join(PKG, mod)
    if os.path.isdir(d):
        return sorted(os.path.join(d, f) for f in os.listdir(d)
                      if f.endswith(".py"))
    return []


def _head_bound_names(fn):
    """Locals bound from the argv HEAD — including the tuple-unpack shape
    (`verb, rest = args[0], args[1:]`) that hid cmd_dispatch and cmd_seat."""
    # ARGV-NAMED bindings only, never every param: helm's dispatchers take
    # (args) by convention, and a generic-param rule reads tuple-shape checks
    # as dispatch (measured on the first fabric run: _guard.py's
    # `snap[0] != "file"` — snap is a (kind, bytes, mode) snapshot — surfaced
    # "file" as a phantom `helm work` subverb).
    argvish = {"args", "argv", "rest"}
    out = set()
    for n in ast.walk(fn):
        if not isinstance(n, (ast.Assign, ast.AnnAssign)):
            continue
        tgt = n.targets[0] if isinstance(n, ast.Assign) else n.target
        val = n.value
        if val is None:
            continue
        pairs = [(tgt, val)]
        if isinstance(tgt, ast.Tuple) and isinstance(val, ast.Tuple) \
                and len(tgt.elts) == len(val.elts):
            pairs = list(zip(tgt.elts, val.elts))
        for tgt, val in pairs:
            if not isinstance(tgt, ast.Name):
                continue
            for c in ast.walk(val):
                if isinstance(c, ast.Subscript):
                    base = c.value
                    while isinstance(base, ast.BoolOp):
                        base = base.values[0]
                    if isinstance(c.slice, ast.Constant) \
                            and c.slice.value == 0 \
                            and isinstance(base, ast.Name) \
                            and base.id in argvish:
                        out.add(tgt.id)
                if isinstance(c, ast.Call) \
                        and isinstance(c.func, ast.Attribute) \
                        and c.func.attr == "pop" \
                        and isinstance(c.func.value, ast.Name) \
                        and c.func.value.id in argvish:
                    out.add(tgt.id)
    return out


def _reachable(trees, entry):
    """Functions reachable from `entry` inside the verb's own module(s) — a
    module hosting two verbs otherwise reports each verb's subverbs as the
    OTHER's hidden surface, a scope error that manufactures findings."""
    funcs = {}
    for tree in trees:
        for n in ast.walk(tree):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                funcs.setdefault(n.name, []).append(n)
    seen, queue = set(), [entry]
    while queue:
        name = queue.pop()
        if name in seen or name not in funcs:
            continue
        seen.add(name)
        for fn in funcs[name]:
            for c in ast.walk(fn):
                if isinstance(c, ast.Call):
                    nm = getattr(c.func, "id", None) \
                        or getattr(c.func, "attr", None)
                    # ANOTHER verb's dispatcher is a scope BOUNDARY, not an
                    # edge: shared packages (store hosts store+index) route
                    # both entries through common helpers, and traversing
                    # into a sibling cmd_* attributed 19 of store's subverbs
                    # to `helm index` — and index's `cap` to store — on the
                    # live tree (measured; `helm index premise` prints the
                    # cap usage, proving the bleed was the scanner's).
                    if nm and nm.startswith("cmd_") and nm != entry:
                        continue
                    if nm and nm in funcs and nm not in seen:
                        queue.append(nm)
    # ENTRY ABSENT FROM THIS FILE => EMPTY SCOPE (skip the file), never None
    # (=unscoped): a package hosts several verbs across files, and treating
    # entry-not-here as scan-everything attributed all nineteen of store's
    # subverbs to `helm index` (whose entry lives in index.py) — the exact
    # scope error this function exists to prevent, reintroduced by its own
    # falsy-return rewrite. Measured before/after on the live tree: 19 -> 0.
    out = set()
    for name in seen:
        out.update(funcs[name])
    return out


def accepted_subverbs(paths, entry=None, source=None):
    """token -> first location. `source` lets fixtures pass code directly."""
    accepted = {}
    items = [("<fixture>", source)] if source is not None else \
        [(p, _read(p)) for p in paths]
    for path, text in items:
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        rel = os.path.basename(path)
        consts = {}
        for n in ast.walk(tree):
            if isinstance(n, ast.Assign) \
                    and isinstance(n.targets[0], ast.Name):
                if isinstance(n.value, (ast.Tuple, ast.List, ast.Set)):
                    vals = [e.value for e in n.value.elts
                            if isinstance(e, ast.Constant)
                            and isinstance(e.value, str)]
                    if vals:
                        consts[n.targets[0].id] = vals
                elif isinstance(n.value, ast.Dict):
                    vals = [k.value for k in n.value.keys
                            if isinstance(k, ast.Constant)
                            and isinstance(k.value, str)]
                    if vals:
                        consts.setdefault(n.targets[0].id, vals)
        scope = _reachable([tree], entry) if entry else None
        for fn in [n for n in ast.walk(tree)
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                   and (scope is None or n in scope)]:
            heads = _head_bound_names(fn)
            argvish = {"args", "argv", "rest"}

            def head_expr(node):
                if isinstance(node, ast.Name):
                    return node.id in heads
                if isinstance(node, ast.Subscript):
                    base = node.value
                    while isinstance(base, ast.BoolOp):
                        base = base.values[0]
                    return isinstance(node.slice, ast.Constant) \
                        and node.slice.value == 0 \
                        and isinstance(base, ast.Name) and base.id in argvish
                return False

            for n in ast.walk(fn):
                if isinstance(n, ast.Compare) and head_expr(n.left):
                    for op, comp in zip(n.ops, n.comparators):
                        # NotEq/NotIn ARE acceptance decisions (carried bug 2).
                        if not isinstance(op, (ast.Eq, ast.In,
                                               ast.NotEq, ast.NotIn)):
                            continue
                        cands = []
                        if isinstance(comp, ast.Constant) \
                                and isinstance(comp.value, str):
                            cands = [comp.value]
                        elif isinstance(comp, (ast.Tuple, ast.List, ast.Set)):
                            cands = [e.value for e in comp.elts
                                     if isinstance(e, ast.Constant)
                                     and isinstance(e.value, str)]
                        elif isinstance(comp, ast.Name):
                            # carried bug 3's shape: vocabulary in a named
                            # constant, no literal at the comparison site.
                            cands = consts.get(comp.id, [])
                        for cand in cands:
                            if BAREWORD.match(cand or "") and cand not in STOP:
                                accepted.setdefault(
                                    cand, "%s:%d" % (rel, n.lineno))
                if isinstance(n, ast.Subscript) \
                        and isinstance(n.slice, ast.Name) \
                        and n.slice.id in heads \
                        and isinstance(n.value, ast.Name):
                    for cand in consts.get(n.value.id, []):
                        if BAREWORD.match(cand or "") and cand not in STOP:
                            accepted.setdefault(
                                cand, "%s:%d" % (rel, n.lineno))
                if isinstance(n, ast.Call) \
                        and isinstance(n.func, ast.Attribute) \
                        and n.func.attr == "get" \
                        and isinstance(n.func.value, ast.Name) \
                        and n.args and isinstance(n.args[0], ast.Name) \
                        and n.args[0].id in heads:
                    for cand in consts.get(n.func.value.id, []):
                        if BAREWORD.match(cand or "") and cand not in STOP:
                            accepted.setdefault(
                                cand, "%s:%d" % (rel, n.lineno))
    return accepted


def accepted_flags(paths, entry=None, source=None):
    """{subverb: {flag: where}} — option tokens bound to the BRANCH that
    handles the subverb, never to the module.

    WHY BRANCH-BOUND AND NOT MODULE-WIDE. A module-wide flag scan is the thing
    this file's header already refuses with a number, and re-measuring it
    2026-08-06 agreed: the widest reading (every `--flag` literal anywhere in
    the verb's files) gives 445 findings across 69 verbs. That is not a gate,
    it is a wall of noise. An `if head == "claim":` owns the flags inside its
    own body, and that one narrowing takes the same scan to 8.

    The head test reuses `_head_bound_names`, so every dispatch shape the
    subverb scan learned the hard way — tuple-unpack, NotIn allowlists,
    argv[0] subscripts — is inherited rather than re-derived."""
    out = {}
    items = [("<fixture>", source)] if source is not None else \
        [(p, _read(p)) for p in paths]
    for path, text in items:
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        rel = os.path.basename(path)
        scope = _reachable([tree], entry) if entry else None
        for fn in [n for n in ast.walk(tree)
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                   and (scope is None or n in scope)]:
            heads = _head_bound_names(fn)
            for node in ast.walk(fn):
                if not isinstance(node, ast.If):
                    continue
                toks = []
                for cmp_ in [n for n in ast.walk(node.test)
                             if isinstance(n, ast.Compare)]:
                    if not (isinstance(cmp_.left, ast.Name)
                            and cmp_.left.id in heads):
                        continue
                    for comp in cmp_.comparators:
                        if isinstance(comp, ast.Constant) \
                                and isinstance(comp.value, str):
                            toks.append(comp.value)
                        elif isinstance(comp, (ast.Tuple, ast.List, ast.Set)):
                            toks += [e.value for e in comp.elts
                                     if isinstance(e, ast.Constant)
                                     and isinstance(e.value, str)]
                if not toks:
                    continue
                subs = [t for t in toks
                        if BAREWORD.match(t or "") and t not in STOP]
                if not subs:
                    continue
                flags = {}
                for n in ast.walk(node):
                    if isinstance(n, ast.Constant) \
                            and isinstance(n.value, str) \
                            and FLAG.match(n.value):
                        flags.setdefault(n.value, "%s:%d" % (rel, n.lineno))

                # A PER-SUBVERB TAIL TABLE DISAMBIGUATES A SHARED BRANCH, and
                # without this the scan manufactures corpses. helm/seat.py
                # guards five subverbs in ONE branch — `if verb in ("add",
                # "up", "down", "launch", "smoke")` — and then splits the
                # option tails inside it: tails = {"add": (...), "smoke":
                # (("--multi",), ()), ...}. Attributing the branch's literals
                # to every subverb it names reported `seat add --multi`,
                # `seat up --multi` and `seat down --multi`, none of which is
                # accepted. This is carried bug 3's shape (vocabulary behind a
                # named constant) one level over: the KEY says which subverb
                # owns the flags beside it.
                table = {}
                for n in ast.walk(node):
                    if not isinstance(n, ast.Dict):
                        continue
                    for k, v in zip(n.keys, n.values):
                        if not (isinstance(k, ast.Constant)
                                and isinstance(k.value, str)
                                and k.value in subs):
                            continue
                        own = {}
                        for sub_n in ast.walk(v):
                            if isinstance(sub_n, ast.Constant) \
                                    and isinstance(sub_n.value, str) \
                                    and FLAG.match(sub_n.value):
                                own.setdefault(sub_n.value,
                                               "%s:%d" % (rel, n.lineno))
                        table.setdefault(k.value, {}).update(own)
                if table:
                    for tok in subs:
                        out.setdefault(tok, {}).update(table.get(tok, {}))
                    continue

                # NO TABLE AND MORE THAN ONE SUBVERB: the branch's flags belong
                # to SOME of them and nothing here says which. Attributing to
                # all of them is a false alarm per subverb, so this declines to
                # answer rather than guessing — the same "unknown is not a
                # rejection" discipline the rest of this file keeps.
                if len(subs) > 1:
                    continue
                out.setdefault(subs[0], {}).update(flags)
    return out


def synopsis_clauses(synopsis, verb, known):
    """{subverb: the clause that documents it}.

    `|` IS OVERLOADED AND SPACING CANNOT DISAMBIGUATE IT. It separates clauses
    AND alternates inside one — `--kind build|review`, `--new-work|--supersedes`,
    `<message...|body on stdin>` — and helm never spaces the separator
    (measured: ZERO ` | ` occurrences across work, dispatch and task, 40 bare
    pipes between them). Splitting on every pipe tore `dispatch send` into
    fragments and reported four of its documented flags missing; splitting on
    a spaced pipe found no clause boundaries at all and reported NOTHING,
    including the one finding this scan exists for.

    So the boundary is SEMANTIC: a fragment opens a new clause only when its
    first bareword is a KNOWN ACCEPTED SUBVERB — which the sibling scan in this
    same file already computes. Anything else is alternation and belongs to the
    clause before it."""
    out, cur = {}, None
    for frag in (synopsis or "").split("|"):
        head = None
        for tok in [t.strip(",") for t in frag.split()[:2]]:
            if tok == verb:
                continue
            if BAREWORD.match(tok or ""):
                head = tok
                break
        if head in known:
            cur = head
            out.setdefault(cur, "")
        if cur:
            out[cur] += " " + frag
    return out


def _named_in(text, token):
    return bool(re.search(r"(?<![\w-])%s(?![\w-])" % re.escape(token),
                          text or ""))


def scan():
    """(verb, token, where) for every accepted subverb the root synopsis
    does not name, excluding UNIVERSAL and EXCLUDED_VERBS."""
    verb_mod, verb_help = _cli_tables()
    findings = []
    for verb, (mod, entry) in sorted(verb_mod.items()):
        if verb in EXCLUDED_VERBS:
            continue
        paths = _modfiles(mod)
        if not paths:
            continue
        root = verb_help.get(verb, "")
        for token, where in sorted(
                accepted_subverbs(paths, entry=entry).items()):
            if token in UNIVERSAL or token == verb:
                continue
            if not _named_in(root, token):
                findings.append((verb, token, where))
    return findings


def flag_scan():
    """(verb, subverb, flag, where) for every accepted flag its OWN clause
    does not name, where the verb's synopsis names it somewhere else.

    THE SCOPE IS DELIBERATE AND IT IS THE ONLY REASON THIS CAN BE A GATE.
    A flag absent from the WHOLE synopsis is the wider built-not-wired class
    and measures at 100 findings — real, but not gateable today. A flag the
    verb DOES document, on a DIFFERENT clause from the one that accepts it, is
    a different and nastier animal: the reader finds the flag, binds it to the
    wrong subverb, and concludes the capability does not exist where they need
    it. That set is 8.

    THE INCIDENT THIS EXISTS FOR: the root synopsis prints `--lease ID` on
    `work release` and omits it on `work claim`, where it EXTENDS a lease
    (work/_claims.py). A seat read the surface, concluded no extend existed,
    and RELEASED a room 273 seconds from lapsing. Undiscoverable does not
    merely fail to help — an agent who believes a capability is absent takes
    the destructive alternative CONFIDENTLY."""
    verb_mod, verb_help = _cli_tables()
    findings = []
    for verb, (mod, entry) in sorted(verb_mod.items()):
        if verb in EXCLUDED_VERBS:
            continue
        paths = _modfiles(mod)
        if not paths:
            continue
        root = verb_help.get(verb, "")
        known = set(accepted_subverbs(paths, entry=entry))
        clauses = synopsis_clauses(root, verb, known)
        for sub, flags in sorted(accepted_flags(paths, entry=entry).items()):
            clause = clauses.get(sub)
            if clause is None:          # undocumented subverb: scan()'s job
                continue
            for flag, where in sorted(flags.items()):
                if flag in UNIVERSAL_FLAGS or (verb, sub, flag) in ALLOWED_FLAGS:
                    continue
                if _named_in(clause, flag):
                    continue
                if not _named_in(root, flag):
                    continue            # absent everywhere: the wider class
                findings.append((verb, sub, flag, where))
    return findings


class FlagDetectorMustHitTest(unittest.TestCase):
    """Same bar as the subverb detector: each shape provably fires, and every
    narrowing carries a control proving it narrows the RIGHT thing. Written
    after three drafts of the clause splitter each produced a confidently
    wrong number — 22 findings, then 0, then 8."""

    def _flags(self, source):
        return accepted_flags([], source=source)

    def test_a_flag_is_bound_to_its_own_branch_not_to_the_module(self):
        """The whole reason this can be a gate. Two subverbs in one module,
        one flag each: a module-wide scan gives both flags to both."""
        src = ("def cmd_x(args):\n"
               "    verb = args[0]\n"
               "    if verb == 'claim':\n"
               "        n = args.index('--lease')\n"
               "    if verb == 'release':\n"
               "        n = args.index('--park')\n")
        got = self._flags(src)
        self.assertEqual({"--lease"}, set(got.get("claim", {})))
        self.assertEqual({"--park"}, set(got.get("release", {})),
                         "a flag leaked across branches — the module-wide "
                         "scan this narrowing exists to replace")

    def test_a_non_flag_string_is_not_a_flag(self):
        # CONTROL: the branch is found, and its non-option strings stay out.
        src = ("def cmd_x(args):\n"
               "    verb = args[0]\n"
               "    if verb == 'claim':\n"
               "        msg = 'not-a-flag'\n"
               "        n = args.index('--lease')\n")
        self.assertEqual({"--lease"}, set(self._flags(src).get("claim", {})))

    def test_a_clause_opens_only_on_a_KNOWN_subverb(self):
        """`|` separates clauses AND alternates inside one. The alternation
        must attach to the clause before it, or a documented flag reads as
        missing — this is the draft that reported four of `dispatch send`'s
        own flags undocumented."""
        syn = "d send <to> --kind build|review --new-work|--supersedes ID|add <to> --ref TIP"
        cl = synopsis_clauses(syn, "d", {"send", "add"})
        self.assertIn("--new-work", cl["send"])
        self.assertIn("--supersedes", cl["send"])
        self.assertIn("--ref", cl["add"])
        # CONTROL, the other direction: `add` must NOT absorb send's flags.
        self.assertNotIn("--kind", cl["add"])

    def test_a_spaced_pipe_is_not_the_separator_helm_uses(self):
        """The draft that split on ' | ' found NO boundaries and reported
        zero findings — a green that proved only that the splitter was
        blind. Pinned so nobody reintroduces the tidier-looking rule."""
        syn = "d claim <lane>|release [<lane>] --lease ID"
        cl = synopsis_clauses(syn, "d", {"claim", "release"})
        self.assertNotIn("--lease", cl["claim"])
        self.assertIn("--lease", cl["release"])


class DetectorMustHitTest(unittest.TestCase):
    """The rung is vacuous unless each carried scanner bug provably fires —
    every fixture hides exactly one token behind exactly one shape, plus a
    named control token that must NOT flag."""

    def _tokens(self, source, entry=None):
        return set(accepted_subverbs([], entry=entry, source=source))

    def test_tuple_unpack_dispatch_is_seen(self):
        src = ("def cmd_x(args):\n"
               "    verb, rest = args[0], args[1:]\n"
               "    if verb == 'hidden-token':\n"
               "        return 1\n")
        self.assertIn("hidden-token", self._tokens(src))

    def test_not_in_allowlist_is_an_acceptance_decision(self):
        src = ("def cmd_x(args):\n"
               "    verb = args[0]\n"
               "    if verb not in ('register', 'arms'):\n"
               "        return 2\n")
        self.assertEqual({"register", "arms"}, self._tokens(src))

    def test_vocabulary_behind_a_named_constant_is_seen(self):
        src = ("WORDS = ('alpha-two', 'beta-two')\n"
               "def cmd_x(args):\n"
               "    verb = args[0]\n"
               "    if verb in WORDS:\n"
               "        return 3\n")
        self.assertEqual({"alpha-two", "beta-two"}, self._tokens(src))

    def test_dict_dispatch_and_get_dispatch_are_seen(self):
        src = ("TABLE = {'delta': 1, 'echo': 2}\n"
               "def cmd_x(args):\n"
               "    verb = args.pop(0)\n"
               "    if TABLE.get(verb):\n"
               "        return TABLE[verb]\n")
        self.assertEqual({"delta", "echo"}, self._tokens(src))

    def test_reachability_scopes_out_the_other_verbs_dispatcher(self):
        # One module, two dispatchers: entry-scoped analysis must not report
        # the OTHER verb's subverb (the scope error that manufactures
        # findings in shared modules like store/ and ownerasks).
        src = ("def cmd_mine(args):\n"
               "    verb = args[0]\n"
               "    if verb == 'mine-sub':\n"
               "        return 1\n"
               "def cmd_other(args):\n"
               "    verb = args[0]\n"
               "    if verb == 'other-sub':\n"
               "        return 2\n")
        self.assertEqual({"mine-sub"},
                         self._tokens(src, entry="cmd_mine"))

    def test_a_non_head_comparison_does_not_flag(self):  # noqa: VACUOUS_ASSERTION — the five sibling must-hit fixtures fire the SAME analyzer; this arm proves selectivity, not detection
        # kind is bound from args[1], not the head — comparing it is flag
        # -value vocabulary, not a subverb, and must stay silent.
        src = ("def cmd_x(args):\n"
               "    kind = args[1]\n"
               "    if kind == 'not-a-subverb':\n"
               "        return 1\n")
        self.assertEqual(set(), self._tokens(src))


class LrSynopsisClauseSeparationTest(unittest.TestCase):
    def test_grafted_clauses_keep_their_sentence_boundary(self):
        """The delta FIX on 16d62b33: the lr synopsis union concatenated
        the compose and refs clauses without punctuation ('...localizes a red
        batch `refs` audits...'). Byte-pin the boundary: each grafted clause
        ends before the next begins.

        THE ARM ASSERTS THE BOUNDARY, NOT THE ADJACENCY, and the difference is
        a real one this suite already paid for: the original pin required the
        compose and refs clauses to be NEIGHBOURS, so grafting any new clause
        BETWEEN them failed an arm whose stated property was untouched. The
        defect is a MISSING SENTENCE BOUNDARY before a grafted clause; it is
        not an ordering, and the union is explicitly a place clauses get added.
        So each clause is checked for a terminator of its own, wherever it
        sits, and the exact concatenation a probe measured stays pinned
        verbatim."""
        import re
        from helm import cli
        entry = cli._VERB_HELP["lr"]
        # the measured defect, pinned exactly as it was found
        self.assertNotIn("red batch `refs`", entry)
        # and the property it was an instance of: every grafted clause opens
        # after a sentence terminator, in whatever order the union grafts them
        # unconditional: a one-element loop buys nothing and can empty out
        self.assertIn("`refs` audits", entry)
        self.assertIn("localizes a red batch", entry)
        self.assertRegex(
            entry, r"[.!?]\s+" + re.escape("`refs` audits"),
            "the refs clause must open after a sentence terminator, not be "
            "concatenated onto the clause before it")
        self.assertRegex(entry, r"localizes a red batch\.")


class RogueSynopsisSafetyTest(unittest.TestCase):
    def test_network_bound_commands_print_the_never_signal_contract(self):
        from helm import cli
        entry = cli._VERB_HELP["rogue"]
        self.assertIn("`wrangler`", entry)
        self.assertIn("`opennextjs-cloudflare`", entry)
        self.assertIn("NETWORK-BOUND", entry)
        self.assertIn("direct or via npx or pnpm/npm exec/dlx", entry)
        self.assertIn("alerted once and NEVER signalled", entry)


class LiveTreeControlTest(unittest.TestCase):
    def test_meld_is_not_reportable(self):  # noqa: VACUOUS_ASSERTION — scan() liveness is pinned by test_verbs_and_help_tables_parsed_nonempty and by the must-hit fixtures on the same analyzer
        """#169 (chat meld absent from usage) is CLOSED on trunk — a rung
        that re-reports it is broken, by the dispatching brief's own
        must-hit. Standup and council ride the same closure."""
        flagged = {(v, t) for v, t, _ in scan()}
        for token in ("meld", "council", "standup"):
            self.assertNotIn(("chat", token), flagged)

    def test_verbs_and_help_tables_parsed_nonempty(self):
        # The scan is vacuous if cli.py parsing returns nothing — positive
        # control on the extractor before any absence below means anything.
        verb_mod, verb_help = _cli_tables()
        self.assertGreater(len(verb_mod), 40)
        self.assertGreater(len(verb_help), 40)


class SurfaceWiringRungTest(unittest.TestCase):
    def test_every_accepted_subverb_is_printed_or_reasoned(self):  # noqa: VACUOUS_ASSERTION — the same scan listed the full pre-wiring inventory red on the fabric (run lane-wire-the-verbs-ff1b3734); emptiness here is the wiring, not a dead scanner
        findings = [(v, t, w) for v, t, w in scan()
                    if (v, t) not in ALLOWED]
        self.assertEqual(
            [], findings,
            "\naccepted subverbs the root synopsis does not name — wire each "
            "into cli._VERB_HELP (the surface that actually prints) or add a "
            "REASONED entry to ALLOWED:\n  " + "\n  ".join(
                "helm %s %s  (%s)" % f for f in findings))

    def test_no_allowlist_entry_outlives_its_subject(self):  # noqa: VACUOUS_ASSERTION — same live scan() as above; the extractor positive control pins non-vacuity
        """An exemption whose token is no longer accepted anywhere is a stale
        ratchet — it would silently exempt a FUTURE unrelated token of the
        same name. Delete entries when their subject dies."""
        live = {(v, t) for v, t, _ in scan()}
        stale = sorted(k for k in ALLOWED if k not in live)
        self.assertEqual(
            [], stale,
            "ALLOWED entries whose token is gone or now printed: %s" % stale)


class FlagClauseRungTest(unittest.TestCase):
    """task/365 — a flag documented on the WRONG clause is worse than an
    absent one, because the reader binds it to the wrong subverb and takes
    the destructive alternative believing the capability does not exist.

    SHIPPED RED FIRST, as the dispatch required: against pre-fix trunk this
    listed 8, including `helm work claim --lease` (the room released 273
    seconds from lapsing) and `helm task claim --owner` (which this seat hit
    itself an hour before writing the scan — `helm task claim 373` refused
    with "no seat name — pass --owner SEAT" against a clause reading
    `claim <id>`). The fixtures above are what make a later green mean
    something.
    """

    def test_every_accepted_flag_is_named_on_its_own_clause(self):  # noqa: VACUOUS_ASSERTION — this scan listed 8 findings RED on pre-fix trunk (the fixtures in FlagDetectorMustHitTest pin each narrowing); emptiness here is the wiring, not a dead scanner
        findings = flag_scan()
        self.assertEqual(
            [], findings,
            "\naccepted flags their OWN subverb clause does not name, while "
            "the verb documents them elsewhere — name each on its clause in "
            "cli._VERB_HELP, or add a REASONED entry to ALLOWED_FLAGS:\n  "
            + "\n  ".join("helm %s %s %s  (%s)" % f for f in findings))

    def test_the_seat_doctor_clause_names_ensure_itself(self):
        """The `doctor` clause names `--ensure` itself, pinned as a NAMED
        regression so this one clause cannot silently re-open.

        THE FAILURE MODE IS ASYMMETRIC, which is why it needs its own arm. A
        flag absent from the WHOLE seat synopsis sits in the wider ungateable
        class and this gate says nothing about it. The moment any OTHER part
        of the synopsis mentions `seat doctor --ensure` — the cred-follow
        prose does — the same flag becomes DOCUMENTED-ELSEWHERE, and a reader
        binds --ensure to whichever subverb the prose sits beside. So adding
        prose about one subverb can redden a different subverb's clause.

        Derived from the SHIPPED table through the shipped splitter, never
        transcribed."""
        verb_mod, verb_help = _cli_tables()
        mod, entry = verb_mod["seat"]
        known = set(accepted_subverbs(_modfiles(mod), entry=entry))
        clauses = synopsis_clauses(verb_help["seat"], "seat", known)
        self.assertTrue(
            _named_in(clauses.get("doctor", ""), "--ensure"),
            "seat doctor's OWN clause does not name --ensure: %r"
            % clauses.get("doctor"))
        # CONTROL — blast radius is this method only. It proves the splitter
        # actually CUTS doctor away from its neighbours: a splitter that fuses
        # the synopsis into one clause makes the assertion above pass on the
        # cred-follow prose alone and say nothing about the doctor clause.
        self.assertFalse(
            _named_in(clauses.get("cred-follow", ""), "--ensure"),
            "the clause splitter fused cred-follow with doctor: %r"
            % clauses.get("cred-follow"))

    def test_no_flag_allowlist_entry_outlives_its_subject(self):  # noqa: VACUOUS_ASSERTION — ALLOWED_FLAGS is empty by design; this fails the moment an entry is added and then cured, which is exactly when a stale ratchet appears
        """Same ratchet guard as the subverb allowlist: an exemption whose
        flag is no longer accepted there would silently exempt a future
        unrelated flag of the same name."""
        live = {(v, s, f) for v, s, f, _ in flag_scan()}
        stale = sorted(k for k in ALLOWED_FLAGS if k not in live)
        self.assertEqual(
            [], stale,
            "ALLOWED_FLAGS entries whose flag is gone or now named: %s"
            % stale)


if __name__ == "__main__":
    unittest.main()
