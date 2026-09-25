#!/usr/bin/env python3
"""A retired top-level name still spelled somewhere in the tree refuses the commit.

THE SHAPE THIS EXISTS FOR. A lane retires a module constant for a rendered
function; the production caller moves with it, and an arm in another test
module still reads the constant -- green on each base, red only when the
train composes them. The author's focused set cannot find it by
construction: that set is derived from consumers of the changed FUNCTION,
and this consumer reaches INTO the module from another lane. The one
instrument that finds it is a grep over the tree for the retired name -- and
the names a diff retires are derivable from the diff itself, so the grep can
run where the retirement is staged.

THE CONTRACT. A name is RETIRED when a `-` line at column zero removes a
top-level `def`, `class` or assignment from a `.py` file and no `+` line in
the same file adds it back. For each retired name the INDEX -- the tree this
commit will produce, so the removed lines themselves are gone -- is searched
for the spellings a consumer in another module uses: `module.NAME`, `from
... module import ... NAME`, a `module` reference with `"NAME"` on the same
line (a mock.patch.object or getattr double), and the bare name in the file
that retired it. Any hit REFUSES the commit and names the path and line.
Nothing else is judged: an indented method, a local variable and a name
re-added in the same file are not retirements, and a name whose only
spelling was the retired line is simply gone.

A NAME THE FILE STILL DEFINES IS NOT RETIRED, and "still defines" is narrow
on purpose: an unconditional top-level `def`, `async def` or `class` of the
name, with no module-scope `del` of it after that statement and no `global`
or `nonlocal` of it anywhere in the file (`still_defines`). No other binder
counts, so when in doubt the rung refuses. This narrow rule is the shape-A
clearance and the own-def clearance of another file below; a declared
satellite is read by the wider `defines_at_top_level`.
A merge is judged like every commit, against its first parent, so merging
trunk into a lane is charged with trunk's retirements: lanes compose in the
train room with trunk as the first parent, and HELM_RETIRED_NAME_SKIP=1
commits a merge made the other way.

A DECLARED MOVE IS NOT A RETIREMENT, and relocation alone does not earn
that. A name removed from F is spared only when F's index text carries an
`_OWNER_NAMES` literal -- the form `web_compat` uses for the web split, a
sequence of (satellite module token, (names...)) pairs -- naming that
satellite for that name, AND the satellite's own index text defines the name
at column zero. Both halves are parsed with `ast`; nothing is imported.
Exempting on relocation alone would also exempt a split that forgot to
republish, where every `module.NAME` consumer dangles -- the shape this rung
exists for, and the one a focused set cannot see. A declaration that names a
satellite which does not define the name is still a retirement, so the
exemption cannot be obtained by typing, and a table that does not parse is
UNKNOWN and exempts nothing.

THE MODULE TOKEN IS THE FILE'S BASENAME, `mod` for `helm/mod.py` and the
directory for a package `__init__.py`; a consumer spelling the full dotted
path still contains that token, so it is found.

A MODULE TOKEN IS NOT A BINDING. Another file that names the module and
holds the name is still cleared when the ast resolves every spelling of the
name to something that is NOT the module: a bare NAME bound by `from other
import NAME`; a bare NAME in a file that itself still defines NAME, by the
same narrow `still_defines` test (an unconditional top-level def, async def
or class, no module-scope `del` after it, no `global` or `nonlocal` of it);
and `R.NAME` where an import binds R to something that is not the module.
An alias import of the module (`import helm.mod as m; m.NAME`, `from helm
import mod as m`) is resolved TO the module and refuses. The own def clears
BARE spellings only: `mod.NAME`, `m.NAME` and `R.NAME` with R unbound
refuse beside it, and so does `from mod import NAME` anywhere in the file,
because either binding may be the one that wins. A file that only assigns
NAME, or defines it under an `if` or `try`, is not cleared by this.

WHAT IS NOT FOUND, said here rather than left for a reader to discover: a
name built at runtime; a file that never names the module at all (`from
other import *` where `other` re-exports the name, a module object passed
in from another file); a module rebound through a plain assignment (`c =
mod; c.NAME`), because an assignment is never resolved: it refuses when no
import binds `c`, and is missed when an import also binds `c` to another
module; and, in a file with its own top-level def of NAME, a later
module-scope rebinding of NAME to the module's value through a spelling
this rung does not read as a reference (`NAME = vars(mod)["NAME"]`), since
the own def clears every bare spelling. The trade is deliberate: the
alternative is to warn about every file in the tree that happens to share a
word with a retired symbol, which is the direction that gets a rung
switched off. A re-export by import or by `mod.NAME` is still found, in the
file that re-exports.

AND THE COST OF THE PRECISION, stated: a parameter or local that shares the
retired name still reads as a surviving use in the retiring file, and in
another file that names the module unless a foreign import or the file's
own top-level def binds the name. `R.NAME` beside a module token also reads
as a use when no import binds R (`self`, a parameter, a local, a class the
file defines), because R can hold the module. Each refuses a correct
commit, which is why the override exists and is named in the refusal.

THE NEVERTRACK CLASS. This file is snapshotted beside the shared pre-commit
hook and script-run as `python3 retired_name_rung.py --staged` where the helm
package is not importable, so it is stdlib-only and every git read is a
direct spawn. One-commit owner override: HELM_RETIRED_NAME_SKIP=1, honoured
by the hook block that invokes this rung.
"""
import ast
import io
import os
import re
import subprocess
import sys
import tokenize
import unicodedata

TAG = "[helm retired-name]"

_DEF = re.compile(r"^([-+])(?:async\s+)?(?:def|class)\s+([A-Za-z_][A-Za-z0-9_]*)")
#: A column-zero assignment, including the tuple form `FOO, BAR = 1, 2` — a
#: single-name pattern read that as no assignment at all and retired nothing.
_ASSIGN = re.compile(
    r"^([-+])([A-Za-z_][A-Za-z0-9_]*(?:\s*,\s*[A-Za-z_][A-Za-z0-9_]*)*)"
    r"\s*(?::[^=\n]*)?=(?!=)")
#: GIT DELIMITS A PATH THAT CONTAINS A SPACE WITH A TRAILING TAB, and a greedy
#: `.+` swallows it — the captured path then matches nothing in the index and
#: every retirement in that file is dropped in SILENCE. Measured on git 2.x:
#: `--- a/helm/with space.py\t`. The tab is excluded from the capture rather
#: than stripped afterwards, so a path that genuinely ends in a space (which
#: git would quote) is not quietly rewritten.
_OLD = re.compile(r"^--- (?:a/([^\t]+)|/dev/null)\t?$")
_NEW = re.compile(r"^\+\+\+ (?:b/([^\t]+)|/dev/null)\t?$")


def _git(root, *args):
    """Every git read this rung makes, with path quoting OFF at the ONE door.

    `core.quotePath` defaults ON, so a path outside ASCII comes back C-quoted
    — `"helm/caf\\303\\251.py"` — and a path carrying that spelling matches
    nothing in the index. Setting it on the diff alone was not enough and the
    way it failed is the argument for putting it here: the diff parsed, the
    candidate list from `git grep -l` was still quoted, and every retirement in
    such a file was dropped in SILENCE. One door, so no later call can forget.
    """
    return subprocess.run(("git", "-c", "core.quotePath=false") + args,
                          cwd=root, capture_output=True, timeout=60)


def _root():
    done = _git(os.getcwd(), "rev-parse", "--show-toplevel")
    if done.returncode != 0:
        return None
    return os.fsdecode(done.stdout.strip())


def parse(diff):
    """[(old_path, name)] -- the top-level names the diff retires.

    Pure over text so the incident shape is a fixture and not a repository.
    `-` lines belong to the OLD path and `+` lines to the NEW one; a name
    removed and added within the same file pair is a change, not a
    retirement, whichever order the hunks come in."""
    old = new = None
    removed, added = [], set()
    for line in (diff or "").splitlines():
        m = _OLD.match(line)
        if m:
            old = m.group(1)
            continue
        m = _NEW.match(line)
        if m:
            new = m.group(1)
            continue
        if line.startswith("---") or line.startswith("+++"):
            continue
        m = _DEF.match(line) or _ASSIGN.match(line)
        if not m:
            continue
        sign = m.group(1)
        for name in [n.strip() for n in m.group(2).split(",")]:
            if not name:
                continue
            if sign == "-" and old and old.endswith(".py"):
                removed.append((old, new or old, name))
            elif sign == "+" and new and new.endswith(".py"):
                added.add((new, name))
    out, seen = [], set()
    for old_path, new_path, name in removed:
        if (new_path, name) in added or (old_path, name) in seen:
            continue
        seen.add((old_path, name))
        # THE PATH THE FILE NOW HAS, because that is where the index holds it.
        # A rename retires the name from the OLD path and continues the file at
        # the NEW one; searching the old path asks the index about a file that
        # is no longer there and always answers nothing.
        out.append((old_path, name, new_path))
    return out


def module_token(path):
    """The token a consumer spells before the dot."""
    base = os.path.basename(path)
    if base == "__init__.py":
        return os.path.basename(os.path.dirname(path)) or None
    return base[:-3] if base.endswith(".py") else None


def _index_text(root, path):
    """The index's copy of one file, or None when it cannot be read."""
    done = _git(root, "show", ":" + path)
    if done.returncode != 0:
        return None
    return os.fsdecode(done.stdout)


def _names_and_strings(text):
    """({NAME tokens}, {string literal values}) for one python source.

    THE TOKENIZER IS THE INSTRUMENT, and the line-oriented regexes it replaces
    were wrong in BOTH directions. They missed a consumer that imports across
    lines --
        from helm.mod import (
            RETIRED,
        )
    -- because `git grep` sees one line at a time and no line carries `from`,
    `import` and the name together. And they REFUSED a correct commit for a
    deprecation comment or a docstring mentioning the retired name in the file
    that retired it, which is the most likely sentence an author writes at
    exactly that moment; a rung that refuses correct commits is a rung that
    gets switched off. Tokens carry no comments and strings are answered
    separately, so both directions close at once.

    NAME TOKENS ARE NFKC-NORMALISED, BECAUSE PYTHON BINDS THE NORMALISED FORM
    AND `tokenize` REPORTS THE RAW ONE. Measured: a file spelling the name in
    fullwidth latin is tokenised as that fullwidth word, while the interpreter
    binds the ASCII name -- so comparing raw tokens asks a question Python does
    not ask, and answers NOT A CONSUMER about a file that consumes it. Both
    spellings are added, so a hit on either answers yes; the raw form stays
    because it is what a reader greps for.

    A source that does not tokenize answers ({}, {}) rather than raising: a
    file this rung cannot read is not a file it may accuse."""
    names, strings = set(), set()
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type == tokenize.NAME:
                names.add(tok.string)
                names.add(unicodedata.normalize("NFKC", tok.string))
            elif tok.type == tokenize.STRING:
                body = tok.string.strip("rbuf").strip("'\"")
                strings.add(body)
    except (tokenize.TokenError, IndentationError, SyntaxError, ValueError):
        return set(), set()
    return names, strings


def _lines_with(text, name):
    """[(lineno, text)] for the source lines carrying `name` as a word."""
    pat = re.compile(r"\b%s\b" % re.escape(name))
    return [(n, line.rstrip())
            for n, line in enumerate(text.splitlines(), 1) if pat.search(line)]



def _source_parses(text):
    """Did this source tokenize? A file that did NOT is a file this rung
    could not clear, which is not the same as a file it cleared.

    `_names_and_strings` answers empty sets for an unparseable source, and
    empty sets look exactly like "holds nothing" to the caller -- so a
    consumer with an unclosed bracket at the BOTTOM of the file and a live
    use of the retired name at the TOP was silently dropped and the commit
    passed unchecked (found in review). A guard may not turn "I cannot read
    this" into "this is clean", so the caller now keeps the candidate."""
    try:
        list(tokenize.generate_tokens(io.StringIO(text).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError, ValueError):
        return False
    return True


#: THE TWO PATCH-SITE EVIDENCES ARE NOT INTERCHANGEABLE AT THE CALLER, so the
#: predicate reports WHICH one it found rather than a bare True. A DOTTED
#: target carries its own module inside the string; a STRUCTURAL one is a call
#: holding the module as an expression, which means a module token is present
#: in the file by construction. Both are truthy, so a caller that only asks
#: "is this a patch site" reads exactly as it did before.
PATCH_DOTTED = "dotted"
PATCH_STRUCTURAL = "structural"


def _patch_site(text, mod, name):
    """Which evidence makes `name` a STRING reaching into `mod` -- or None.

    THE INSTRUMENT IS THE AST, AND TWO CHEAPER ONES BOTH FAILED FIRST.

    File-wide token matching was a false-refusal engine, measured on this
    repo's `tests/test_chat.py` (found in review): it imports `chat` and also
    contains the string literals "status", "read", "join" and "post"
    somewhere, so retiring any of those four from `helm/chat.py` refused a
    commit over a file that never touches them.

    Narrowing to the SAME LINE fixed two of the four and not the other two,
    which is the measurement that killed the whole idea of matching on
    proximity: `chat.cmd_chat(["join", "--room", "main"])` puts the module and
    the string on one line and is an argv, not a patch. Proximity is evidence
    of typing, never of reference.

    So the question is asked STRUCTURALLY: a call to `getattr`/`setattr`/
    `delattr`/`hasattr` or to something spelled `patch`/`patch.object` whose
    arguments are the module and the name. And a DOTTED string is read
    wherever it stands, because `mock.patch("helm.chat.helper")` carries one
    token holding its own subject -- the spelling the bare-name comparison
    missed entirely.

    An unparseable source answers False here; `_source_parses` is what keeps
    such a file from reading as clean."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return False

    def dotted_hit(value):
        if not isinstance(value, str) or "." not in value:
            return False
        head, _, last = value.rpartition(".")
        return last == name and bool(mod) and mod in head.split(".")

    def names_the_module(node):
        return ((isinstance(node, ast.Name) and node.id == mod)
                or (isinstance(node, ast.Attribute) and node.attr == mod))

    structural = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and dotted_hit(node.value):
            # DOTTED WINS WHEREVER IT APPEARS, even after a structural hit:
            # it is the stronger evidence, and returning the weaker one would
            # make the caller ask for a module token this file need not have.
            return PATCH_DOTTED
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            spelling = func.id
        elif isinstance(func, ast.Attribute):
            spelling = func.attr
        else:
            continue
        if spelling not in ("getattr", "setattr", "delattr", "hasattr",
                            "patch", "object"):
            continue
        args = list(node.args) + [kw.value for kw in node.keywords]
        holds_name = any(isinstance(a, ast.Constant) and a.value == name
                         for a in args)
        if holds_name and any(names_the_module(a) for a in args):
            structural = True
    return PATCH_STRUCTURAL if structural else None


def _bound_elsewhere(text, mod, name):
    """Does every spelling of `name` here resolve to a binding that is NOT `mod`?

    A `mod` token in the file is not a binding. When `cell.roster_path` is
    retired, a file that does `from .seats_common import roster_path` in one
    function and `from . import cell` in another holds both tokens and is
    not a consumer: its `roster_path` is `seats_common.roster_path`, a
    different function. So is a test that spells `seats.roster_path()`
    beside a `cell` token. Refusing either refuses a correct commit.

    A FILE'S OWN DEFINITION IS A BINDING TOO. When `chat._ledger_write` is
    retired, helm/dispatches.py names `chat` and the refusal listed 18 of its
    lines, each one its own top-level `def _ledger_write`, a bare call to
    that def, or prose about it. Measured by replaying the task/3060 commit
    "argv-guard: one delegate rung". No spelling there can reach `chat`, and
    refusing it refused a correct commit.

    So the ast answers, and only for the three shapes it can resolve: a bare
    `name` bound by `from X import name` where X is not `mod`; a bare `name`
    in a file that itself still defines it, by the same narrow test as shape
    A (`still_defines`: an unconditional top-level def, async def or class,
    no module-scope `del` after it, no `global` or `nonlocal` of it); and
    `R.name` where R is a name that an import binds to something other than
    `mod`. Every other spelling is still a use: `from mod import name`,
    `from mod import *`, a relative `from . import name` -- each refuses even
    beside an own def, because it may be the binding that wins -- `mod.name`,
    `pkg.mod.name`, `alias.name` for an alias an import binds to `mod`, a
    bare `name` that no foreign import and no own def binds (an assignment or
    a conditional def is not one), and `R.name` for a receiver no import
    binds. A parameter or a local can hold the module, and a guess about that
    is not this function's to make. An unparseable source answers False,
    because this function only removes refusals and an unknown may not
    remove one."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        return False
    # THE OWN DEF IS ITSELF A RESOLVED SPELLING: a file whose only use is the
    # def statement holds the name and has nothing that reaches `mod`.
    own = still_defines(text, name)
    if own:
        # THE STRING DOOR BEATS THE OWN DEF: `getattr(c, "NAME")`,
        # `c.__dict__["NAME"]` and `vars(c)["NAME"]` reach `mod`'s NAME
        # through an alias the attribute walk never sees (c carries no
        # `.NAME` attribute node), and the own def cleared the file anyway
        # (a review of this lane). `_patch_site` answers exactly this
        # question for the module's OWN name; here the alias is the file's,
        # so the strings are asked of it directly: any alias bound to `mod`
        # whose text reaches the name through one of those spellings refuses
        # the clearance.
        aliases = _aliases_to(tree, mod)
        if aliases and _string_reach(tree, aliases, name):
            return False
    to_mod, to_other, resolved = {mod}, set(), own
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.asname:
                    last = a.name.rpartition(".")[2]
                    (to_mod if last == mod else to_other).add(a.asname)
                else:
                    (to_mod if a.name == mod else to_other).add(
                        a.name.partition(".")[0])
        elif isinstance(node, ast.ImportFrom):
            src = (node.module or "").rpartition(".")[2]
            for a in node.names:
                if a.name in (name, "*") and (src == mod or not node.module):
                    return False
                bound = a.asname or a.name
                resolved = resolved or name in (a.name, bound)
                (to_mod if a.name == mod else to_other).add(bound)
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == name:
            recv = node.value
            if not isinstance(recv, ast.Name) or recv.id not in to_other \
                    or recv.id in to_mod:
                return False
            resolved = True
        elif isinstance(node, ast.Name) and node.id == name:
            if not own and (name not in to_other or name in to_mod):
                return False
            resolved = True
    return resolved


def _aliases_to(tree, mod):
    """The names an import statement binds TO `mod`, as a set: the alias of
    `import helm.chat as c` / `from helm import chat as c`, the BARE name of
    `from helm import chat` / `from . import chat` (the live shape), and the
    module's own name for `import helm.chat`, whose receiver is the dotted
    chain itself. A plain assignment (`c = chat`) is never resolved, exactly
    as `_bound_elsewhere` states."""
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.asname:
                    if a.name.rpartition(".")[2] == mod:
                        out.add(a.asname)
                elif a.name == mod or a.name.endswith("." + mod):
                    out.add(mod)
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                if a.name == mod:
                    out.add(a.asname or mod)
    return out


def _receiver_names(node):
    """The names an expression resolves through: a Name's own id, and every
    attribute in a dotted chain (`helm.chat` yields both)."""
    out = set()
    while True:
        if isinstance(node, ast.Name):
            out.add(node.id)
            return out
        if isinstance(node, ast.Attribute):
            out.add(node.attr)
            node = node.value
            continue
        return out


def _string_reach(tree, aliases, name):
    """Does any call or subscript reach `name` as a STRING through one of
    `aliases`: getattr/setattr/delattr/hasattr(a, "name"), a.__dict__["name"],
    vars(a)["name"], where `a` may be a plain name or a dotted chain whose
    last attribute is one (helm.chat). globals()["name"] names no receiver
    and is not read."""
    def on_alias(expr):
        return bool(_receiver_names(expr) & aliases)

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            spelling = func.id if isinstance(func, ast.Name) \
                else func.attr if isinstance(func, ast.Attribute) else None
            args = list(node.args) + [kw.value for kw in node.keywords]
            holds = any(isinstance(a, ast.Constant) and a.value == name
                        for a in args)
            if holds and any(on_alias(a) for a in args) and spelling in (
                    "getattr", "setattr", "delattr", "hasattr", "vars"):
                return True
        elif isinstance(node, ast.Subscript):
            # a.__dict__["name"] or vars(a)["name"], keyed on the receiver
            target = node.slice
            if isinstance(target, ast.Constant) and target.value == name:
                if isinstance(node.value, ast.Attribute) \
                        and node.value.attr == "__dict__" \
                        and on_alias(node.value.value):
                    return True
                if isinstance(node.value, ast.Call) \
                        and isinstance(node.value.func, ast.Name) \
                        and node.value.func.id == "vars" \
                        and node.value.args and on_alias(node.value.args[0]):
                    return True
    return False


def consumers(root, path, name, live_path=None):
    """([(path, lineno, text)], error) -- where the INDEX still spells it.

    THE QUESTION IS ASKED OF PYTHON, NOT OF TEXT. A file is a consumer when it
    holds the retired name as a NAME TOKEN and also names the module it came
    from -- which covers `mod.NAME`, a one-line `from ... import NAME`, and the
    parenthesized multi-line import that a line-oriented search cannot see --
    or when it holds the name as a STRING beside the module, the shape a
    patched double takes. A file whose every spelling of the name the ast
    resolves to ANOTHER module or to the file's own top-level def
    (`_bound_elsewhere`) is not a consumer, even when it names the module
    somewhere else. The file that RETIRED the name
    is a consumer of itself whenever a token survives there.

    `git grep -l` is only a PREFILTER and is deliberately MORE PERMISSIVE than
    the test: an ASCII NAME token is also a word-boundary text match, so a file
    it drops can hold no such token. The reverse is what a prefilter may never
    be.

    THE NAMED LIMIT, MEASURED RATHER THAN ASSUMED, and the reason the sentence
    above says ASCII. Python NFKC-normalises identifiers, so a file may spell
    the name in a form whose BYTES differ while the interpreter binds the very
    name being retired -- fullwidth latin is the readable example. That file is
    a real consumer and `git grep -e '\bNAME\b'` cannot see it, because the
    bytes it searches for are not there.

    The TEST half is closed: `_names_and_strings` adds the NFKC form of every
    token, so once a file reaches the test its equivalent spellings are found.
    The PREFILTER half is NOT, and cannot be cheaply: git grep would have to
    search every NFKC-equivalent spelling of the name, an unbounded set.

    IT IS LEFT OPEN DELIBERATELY. The falsifying population is adversarial --
    nobody writes a fullwidth identifier by accident -- and the alternative is
    dropping the prefilter, which would tokenise every `.py` in the tree on
    every commit. A guard that names a miss it chose is honest; one that claims
    a completeness it does not have teaches the next reader to stop looking.
    This rung already chose the miss direction once, for comments and
    docstrings, and for the same stated reason."""
    live = live_path or path
    mod = module_token(live) or module_token(path)
    done = _git(root, "grep", "-l", "--cached", "-E", "-e",
                r"\b%s\b" % re.escape(name), "--", "*.py")
    if done.returncode not in (0, 1):
        return None, os.fsdecode(done.stderr).strip() or "git grep failed"
    hits = []
    for raw in done.stdout.splitlines():
        cand = os.fsdecode(raw)
        text = _index_text(root, cand)
        if text is None:
            continue
        if not _source_parses(text):
            # UNREADABLE IS NOT CLEAN. The prefilter already matched the name
            # in this file's text and nothing here can rule it out, so it is
            # reported rather than dropped.
            hits.extend((cand, n, t) for n, t in _lines_with(text, name))
            continue
        names, _strings = _names_and_strings(text)
        patched = _patch_site(text, mod, name)
        if name not in names and not patched:
            continue        # a comment, a docstring, or a longer word
        if cand != live and cand != path:
            # ANOTHER MODULE: it must name the one the symbol came from, or it
            # is a different thing that happens to share a name.
            #
            # A DOTTED PATCH TARGET IS THE ONE EXCEPTION, and it is the
            # commonest spelling there is. `mock.patch("helm.chat.helper")`
            # names `chat` INSIDE the string, and a file that patches only
            # that way has no `chat` NAME token anywhere -- so asking for one
            # dropped the hit `_patch_site` had just proven, one branch later,
            # and the retirement shipped with a live consumer behind it.
            # Measured on this lane and reproduced through this
            # door; the predicate's own arm was green the whole time.
            if patched != PATCH_DOTTED and (not mod or mod not in names):
                continue
            # A MODULE TOKEN IS NOT A BINDING. The file names `mod`, but when
            # every spelling of the name resolves to another module or to the
            # file's own top-level def, it is a different thing by the same
            # name. A patch site is a string reaching into `mod` and is never
            # resolved away.
            if not patched and _bound_elsewhere(text, mod, name):
                continue
        elif name not in names:
            continue        # in the retiring file, a mere string is not a use
        hits.extend((cand, n, t) for n, t in _lines_with(text, name))
    return hits, None


def owner_declarations(text):
    """{(satellite token, NAME)} one module DECLARES it has handed away.

    The literal is the form `web_compat` already uses for the web split: a
    module-level `_OWNER_NAMES` bound to a sequence of (satellite module
    token, (names...)) pairs. It is read from the INDEX with `ast` and never
    imported, so this stays a stdlib textual rung.

    NONE MEANS UNKNOWN AND EXEMPTS NOTHING. A file that does not parse has
    not been cleared, which is not the same as a file that was; the caller
    then judges every retirement in it, which is this rung's behaviour
    without the declaration.

    A DECLARATION IS A CLAIM, NEVER A CLEARANCE ON ITS OWN. The caller also
    requires the named satellite to DEFINE the name at column zero, so a
    table cannot vouch for a name nobody wrote -- otherwise the exemption
    would be available by typing.
    """
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        return None
    owned = set()
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == "_OWNER_NAMES"
                   for t in node.targets):
            continue
        if not isinstance(node.value, (ast.Tuple, ast.List)):
            continue
        for pair in node.value.elts:
            if not isinstance(pair, (ast.Tuple, ast.List)) or len(pair.elts) != 2:
                continue
            mod, names = pair.elts
            if not (isinstance(mod, ast.Constant) and isinstance(mod.value, str)):
                continue
            if not isinstance(names, (ast.Tuple, ast.List)):
                continue
            for item in names.elts:
                if isinstance(item, ast.Constant) and isinstance(item.value, str):
                    owned.add((mod.value, item.value))
    return owned


def defines_at_top_level(text, name):
    """Does this source bind `name` at column zero? Parsed, never imported."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        return False
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)) and node.name == name:
            return True
        if isinstance(node, ast.Assign):
            if any(isinstance(t, ast.Name) and t.id == name
                   for t in node.targets):
                return True
        if isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.target.id == name:
                return True
    return False


#: A `del` or `except ... as` inside one of these touches that scope's own
#: name. A `global` that would make it the module's refuses on its own.
_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)


def still_defines(text, name):
    """Does this source still DEFINE `name`? Parsed, never imported.

    The shape-A clearance in `scan_staged`, and the own-def clearance of a
    bare spelling in another file (`_bound_elsewhere`); a declared satellite
    is read by the wider `defines_at_top_level`.

    Yes only for an unconditional module-top-level `def`, `async def` or
    `class` of the name, when no module-scope statement after the last one
    deletes it (`del NAME`, or `except ... as NAME`, which Python deletes as
    the handler ends) and no `global` or `nonlocal` of the name appears
    anywhere in the file. No other binder counts -- an assignment, an
    annotation, an import, a loop or with target -- and neither does a
    definition under an `if` or `try`. That refuses some correct commits (a
    def moved to a sibling and imported back), which the override is for. A
    deletion built at runtime (`globals()`, `exec`) is not seen. An
    unparseable source answers False."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        return False
    if any(isinstance(n, (ast.Global, ast.Nonlocal)) and name in n.names
           for n in ast.walk(tree)):
        return False
    last = None
    for i, node in enumerate(tree.body):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)) and node.name == name:
            last = i
    if last is None:
        return False
    stack = [n for n in tree.body[last + 1:] if not isinstance(n, _SCOPES)]
    while stack:
        n = stack.pop()
        if isinstance(n, ast.Name) and n.id == name and \
                isinstance(n.ctx, ast.Del):
            return False
        if isinstance(n, ast.ExceptHandler) and n.name == name:
            return False
        stack.extend(c for c in ast.iter_child_nodes(n)
                     if not isinstance(c, _SCOPES))
    return True


def moved_to_a_declared_satellite(root, path, name):
    """Did `name` leave `path` for a satellite that path DECLARES and that
    actually defines it? Then it was never retired.

    BOTH HALVES OR NEITHER. The declaration answers "this module handed the
    name away on purpose"; the satellite's own definition answers "and it is
    really there". A move that declares nothing, and a declaration whose
    satellite lacks the name, are both still retirements -- which keeps the
    shape this rung exists for: a split that forgets to republish leaves
    every `module.NAME` consumer dangling, and a focused set cannot see it.
    """
    text = _index_text(root, path)
    if text is None:
        return False
    declared = owner_declarations(text)
    if not declared:
        return False
    here = os.path.dirname(path)
    for mod, owned_name in declared:
        if owned_name != name:
            continue
        satellite = os.path.join(here, mod + ".py") if here else mod + ".py"
        stext = _index_text(root, satellite)
        if stext is not None and defines_at_top_level(stext, name):
            return True
    return False


def scan_staged(root):
    """({(path, name): [(path, lineno, text)]}, error).

    The staged diff is taken against HEAD, so a merge is judged against its
    first parent, as every commit is."""
    done = _git(root, "diff", "--cached", "-U0", "--no-color", "--", "*.py")
    if done.returncode != 0:
        return None, os.fsdecode(done.stderr).strip() or "git diff failed"
    found = {}
    for path, name, live in parse(os.fsdecode(done.stdout)):
        # One of two top-level definitions removed leaves the name defined.
        # Asked at the live path, the file a rename produced.
        text = _index_text(root, live)
        if text is not None and still_defines(text, name):
            continue
        if moved_to_a_declared_satellite(root, path, name):
            continue
        hits, err = consumers(root, path, name, live_path=live)
        if err:
            return None, err
        if hits:
            found[(path, name)] = hits
    return found, None


def report(found, out=sys.stderr):
    print("%s REFUSED: %d retired top-level name(s) still spelled in the tree "
          "this commit would produce:" % (TAG, len(found)), file=out)
    for (path, name), hits in sorted(found.items()):
        mod = module_token(path) or path
        print("%s   %s.%s (retired from %s)" % (TAG, mod, name, path),
              file=out)
        for where, lineno, text in hits[:8]:
            print("%s     %s:%s: %s" % (TAG, where, lineno, text[:100]),
                  file=out)
        if len(hits) > 8:
            print("%s     ... and %d more" % (TAG, len(hits) - 8), file=out)
    print("%s move every consumer in the same commit (the focused set cannot "
          "see a consumer that reaches into the module from another lane); if "
          "a spelling is a different thing by the same name, one-commit "
          "owner override: HELM_RETIRED_NAME_SKIP=1" % TAG, file=out)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv != ["--staged"]:
        print("usage: retired_name_rung.py --staged", file=sys.stderr)
        return 2
    root = _root()
    if root is None:
        print("%s REFUSED: not inside a git work tree" % TAG, file=sys.stderr)
        return 2
    try:
        found, error = scan_staged(root)
    except (OSError, subprocess.TimeoutExpired) as exc:
        found, error = None, "%s: %s" % (exc.__class__.__name__, exc)
    if error:
        print("%s REFUSED: the retired-name scan is UNKNOWN — %s"
              % (TAG, error), file=sys.stderr)
        return 2
    if not found:
        return 0
    report(found)
    return 1


if __name__ == "__main__":
    sys.exit(main())
