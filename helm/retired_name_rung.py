#!/usr/bin/env python3
"""A retired top-level name the tree still reads from its module refuses the commit.

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
for the bare name in the file that retired it, and in every other file for
a read of the name FROM that module (below). Any hit REFUSES the commit and
names the path and line of the read. Nothing else is judged: an indented
method, a local variable and a name re-added in the same file are not
retirements, and a name whose only spelling was the retired line is simply
gone.

A NAME THE FILE STILL DEFINES IS NOT RETIRED, and "still defines" is narrow
on purpose: an unconditional top-level `def`, `async def` or `class` of the
name, with no module-scope `del` of it after that statement and no `global`
or `nonlocal` of it anywhere in the file (`still_defines`). No other binder
counts, so when in doubt the rung refuses. This narrow rule is the shape-A
clearance; a declared satellite is read by the wider `defines_at_top_level`.
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

A RETIRED NAME IS KEYED TO ITS MODULE (task/3418). Removing NAME from
`mod` retires `mod.NAME`, never every NAME in the tree. Another file refuses
only where it reads NAME FROM `mod` (`_module_reads`): `from mod import
NAME` or `from mod import *` anywhere in it, or a relative `from . import
NAME`; `.NAME` on the module, on an alias an import binds to it (`import
helm.mod as m`, `from helm import mod as m`), on a receiver no import binds
(a parameter, `self` or a local can hold the module) and on any receiver
that is not a plain name (`pkg.mod.NAME`) -- `R.NAME` is cleared only for an
R that every import binds to something else; a string that reaches the
module: a dotted target `"helm.mod.NAME"` wherever it stands, and
getattr/setattr/delattr/hasattr/patch/patch.object/getattr_static/
__getattribute__/attrgetter with `"NAME"`, `M.__dict__["NAME"]`,
`vars(M)["NAME"]` or `vars(M).get("NAME")` for the module or an alias of
it; a facade's `_OWNER_NAMES` entry `("mod", (..., "NAME"))` whose token
names the retiring file beside the facade, which publishes the module's
name as the facade's own; and, in a file with a DYNAMIC REACH on the
module (`_dynamic_reads`) -- one of those calls with a name that is not a
constant, `globals().update(vars(M))`, or `patch.multiple` on the module
or its dotted string -- every keyword argument named NAME and every string
constant equal to NAME, and after `globals().update(vars(M))` every bare
NAME too.

A BARE NAME IN ANOTHER FILE IS NEVER A READ OF THE MODULE ON ITS OWN.
Whatever binds it there -- an assignment, a def, an import from elsewhere,
or nothing -- it is that file's own name, and it can hold the module's
value only through a statement that spells the module, each read above in
its own right; the one statement that binds every bare NAME from the
module, `globals().update(vars(mod))`, makes the bare NAME the read.
Deleting `findingspass._FULL_TIP`, and earlier `findingspass._ID`, was
refused with every bare spelling in helm/dispatches.py, which imports
findingspass and binds its OWN `_FULL_TIP` and `_ID` with assignments; the
own def of another file (`chat._ledger_write`, task/3060) was the first
binder to clear a bare spelling, and an assignment never was.

WHAT IS NOT FOUND, said here rather than left for a reader to discover: a
name built at runtime (`getattr(mod, "_x" + "y")`: a dynamic reach counts
only a string or a keyword spelling the whole name), or bound from the
module by `exec`; a dynamic reach through a name no import binds to the
module (`m = mod; getattr(m, n)`); a file that never names the module
at all (`from other import *` where `other` re-exports the name, a module
object passed in from another file); a module rebound through a plain
assignment (`c = mod; c.NAME`), because an assignment is never resolved: it
refuses when no import binds `c`, and is missed when an import also binds
`c` to another module; and the readers of a facade's published name
(`facade.NAME` where the facade's `_OWNER_NAMES` hands NAME to the module):
the facade's own entry refuses while it stands, and an entry removed in the
same commit is not a retirement this rung derives from the diff. The trade
is deliberate: the alternative is to warn about every file in the tree that
happens to share a word with a retired symbol, which is the direction that
gets a rung switched off. A re-export by import or by `mod.NAME` is still
found, in the file that re-exports.

AND THE COST OF THE PRECISION, stated: a parameter or local that shares the
retired name still reads as a surviving use in the retiring file (in
another file only after `globals().update(vars(mod))`). `R.NAME` beside a module token also reads as a
use when no import binds R (`self`, a parameter, a local, a class the file
defines), because R can hold the module, and so does a relative `from .
import NAME`. In a file with a dynamic reach on the module, a string or a
keyword spelling NAME is a read even where it means something else. Each
refuses a correct commit, which is why the override exists and is named in
the refusal.

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
#: A source `ast` cannot read. MemoryError is the parser-stack overflow: a
#: source that TOKENIZES and nests past the parser's stack raises it, not
#: SyntaxError (measured on 3.14: `X = ` then 100000 `-`), and no parse
#: site caught it, so a consumer holding one killed the rung with a
#: traceback (task/3418).
_UNPARSED = (SyntaxError, ValueError, RecursionError, MemoryError)
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


#: PYTHON'S LINE BREAKS, AND ONLY THOSE. `str.splitlines` also breaks at a
#: form feed and at other separators the tokenizer reads as whitespace, so
#: every line after one was numbered one past where `ast` and a reader put it.
_LINE_BREAK = re.compile(r"\r\n?|\n")


def _source_lines(text):
    """The source's lines, numbered as `ast` numbers them (from 1)."""
    return _LINE_BREAK.split(text)


def _lines_with(text, name):
    """[(lineno, text)] for the source lines carrying `name` as a word."""
    pat = re.compile(r"\b%s\b" % re.escape(name))
    return [(n, line.rstrip())
            for n, line in enumerate(_source_lines(text), 1)
            if pat.search(line)]


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


#: THE TWO PATCH-SITE EVIDENCES ARE NOT INTERCHANGEABLE, so the predicate
#: reports WHICH one it found rather than a bare True. A DOTTED target carries
#: its own module inside the string; a STRUCTURAL one is a call or subscript
#: holding the module as an expression, which means a module token is present
#: in the file by construction. Both are truthy, so a caller that only asks
#: "is this a patch site" reads exactly as it did before.
PATCH_DOTTED = "dotted"
PATCH_STRUCTURAL = "structural"

#: The calls that read a module's name through a string: the module first
#: and the name second -- `getattr(M, "NAME")`, `inspect.getattr_static(M,
#: "NAME")`, `object.__getattribute__(M, "NAME")`, `mock.patch.object(M,
#: "NAME", v)`, which is spelled `object`.
_REACHERS = ("getattr", "setattr", "delattr", "hasattr", "patch", "object",
             "getattr_static", "__getattribute__")


def _dotted_hit(value, mod, name):
    """Is `value` a dotted string naming `name` inside `mod`? The
    `mock.patch("helm.chat.helper")` target carries its own module."""
    if not mod or not isinstance(value, str) or "." not in value:
        return False
    head, _, last = value.rpartition(".")
    return last == name and mod in head.split(".")


def _spelling(func):
    """The last name a call is spelled with (`mock.patch.object` is
    `object`), or None."""
    return func.id if isinstance(func, ast.Name) \
        else func.attr if isinstance(func, ast.Attribute) else None


def _module_test(mod, to_mod):
    """The predicate "this expression is the module": a name in `to_mod`
    (its own token, or an alias an import binds to it) or a dotted chain
    whose LAST attribute is it (`helm.chat`); `z.sub` for an alias z is
    `sub`, a different object."""
    def on_mod(expr):
        return ((isinstance(expr, ast.Name) and expr.id in to_mod)
                or (isinstance(expr, ast.Attribute) and expr.attr == mod))
    return on_mod


def _namespace(expr, on_mod):
    """Is `expr` the module's namespace dict, `vars(M)` or `M.__dict__`?"""
    return ((isinstance(expr, ast.Attribute) and expr.attr == "__dict__"
             and on_mod(expr.value))
            or (isinstance(expr, ast.Call) and _spelling(expr.func) == "vars"
                and len(expr.args) == 1 and on_mod(expr.args[0])))


def _reach(node, on_mod):
    """(keys, dotted) when `node` reads a name of the module through a
    string, else None. `keys` are the expressions that carry the name --
    none when the call passes one it cannot see (`patch.object(M, **kw)`)
    -- and `dotted` says the name is a key's FIRST segment, because
    `attrgetter("NAME.x")(M)` reads M.NAME:

        getattr / setattr / delattr / hasattr / getattr_static /
        patch.object / object.__getattribute__ (M, KEY, ...)
        M.__getattribute__(KEY)
        vars(M)[KEY]  M.__dict__[KEY]  vars(M).get(KEY)  M.__dict__.get(KEY)
        attrgetter(KEY, ...)(M)"""
    if isinstance(node, ast.Subscript):
        return ([node.slice], False) if _namespace(node.value, on_mod) \
            else None
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    if isinstance(func, ast.Call) and _spelling(func.func) == "attrgetter":
        return (list(func.args), True) \
            if node.args and on_mod(node.args[0]) else None
    if isinstance(func, ast.Attribute) and (
            (func.attr == "__getattribute__" and on_mod(func.value))
            or (func.attr == "get" and _namespace(func.value, on_mod))):
        return node.args[:1], False
    named = {k.arg: k.value for k in node.keywords}
    target = node.args[0] if node.args else named.get("target")
    if _spelling(func) in _REACHERS and target is not None \
            and on_mod(target):
        key = node.args[1:2] or [named[k] for k in ("attribute", "name")
                                 if k in named][:1]
        return key, False
    return None


def _spells(key, name, dotted=False):
    """Is `key` the constant string `name` (its first segment, `dotted`)?"""
    if not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
        return False
    return (key.value.partition(".")[0] if dotted else key.value) == name


def _computed(reach):
    """Does a `_reach` carry its name as anything but a constant string?"""
    return reach is not None and not (reach[0] and all(
        isinstance(k, ast.Constant) and isinstance(k.value, str)
        for k in reach[0]))


def _string_reads(tree, mod, name, to_mod):
    """{lineno: PATCH_DOTTED | PATCH_STRUCTURAL} -- where `name` is a STRING
    that reaches into `mod`, keyed to the string's own line.

    DOTTED: a string `x.mod.name` wherever it stands. STRUCTURAL: a `_reach`
    whose key is the constant `name`, and a call spelled getattr, setattr,
    delattr, hasattr, patch or patch.object that holds `name` as a string
    and the module as ANY argument. The module is `_module_test`. On one
    line DOTTED wins, because it is the stronger evidence."""
    on_mod = _module_test(mod, to_mod)
    found = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and _dotted_hit(node.value, mod,
                                                          name):
            found[node.lineno] = PATCH_DOTTED
            continue
        reach = _reach(node, on_mod)
        keys = [k for k in reach[0] if _spells(k, name, reach[1])] \
            if reach else []
        if isinstance(node, ast.Call) and _spelling(node.func) in _REACHERS:
            args = list(node.args) + [kw.value for kw in node.keywords]
            if any(map(on_mod, args)):
                keys.extend(a for a in args if _spells(a, name))
        for key in keys:
            found.setdefault(key.lineno, PATCH_STRUCTURAL)
    return found


def _multiple_on(node, mod, on_mod):
    """Is `node` `patch.multiple` on the module or on its dotted string
    (`"helm.mod"`)? A target inside the module (`"helm.mod.Klass"`) is the
    class, not the module."""
    if not (isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "multiple"
            and _spelling(node.func.value) == "patch"):
        return False
    target = node.args[0] if node.args else next(
        (k.value for k in node.keywords if k.arg == "target"), None)
    if isinstance(target, ast.Constant) and isinstance(target.value, str):
        return target.value.rpartition(".")[2] == mod
    return target is not None and on_mod(target)


def _globals_from(node, on_mod):
    """Is `node` `globals().update(vars(M))` or `globals().update(
    M.__dict__)` -- `from M import *`, at run time?"""
    return (isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "update"
            and isinstance(node.func.value, ast.Call)
            and _spelling(node.func.value.func) == "globals"
            and len(node.args) == 1 and _namespace(node.args[0], on_mod))


def _dynamic_reads(tree, mod, name, to_mod):
    """{lineno} -- where a file with a DYNAMIC REACH on `mod` spells `name`
    (task/3418, round 2).

    A NAME THAT TRAVELS THROUGH A VARIABLE IS READ WHERE IT IS SPELLED. An
    approval-tier read of the module key measured it on the real tree:
    tests/test_gate_fifo.py has a helper `guard_reason(**patches)` running
    `mock.patch.object(gatechild, name, value)` for each keyword, called
    with `_arm_parent_death=...` at three lines, and a rename of
    gatechild._arm_parent_death was admitted -- the composed train then
    failed AttributeError, because `patch.object` without `create=True`
    needs the attribute. No expression in that file holds the name AND the
    module; the keyword is the only spelling of the read.

    A DYNAMIC REACH is a `_reach` on the module whose name is not a
    constant string (`getattr(M, n)`, `attrgetter(n)(M)`, `vars(M).get(n)`,
    `patch.object(M, **kw)`), `globals().update(vars(M))`, or
    `patch.multiple` on the module or its dotted string. A file with one
    reads every keyword argument named `name` and every string constant
    equal to `name` in it: that is where a computed name is spelled, and
    `patch.multiple`'s own keywords are among them. `globals().update(
    vars(M))` is `from M import *` at run time, so there a bare `name` is
    the module's as well. A reach on ANOTHER module counts nothing here,
    and without a reach a bare `name` is still the file's own binding."""
    on_mod = _module_test(mod, to_mod)
    nodes = list(ast.walk(tree))
    star = any(_globals_from(n, on_mod) for n in nodes)
    if not star and not any(_multiple_on(n, mod, on_mod)
                            or _computed(_reach(n, on_mod)) for n in nodes):
        return set()
    at = {n.lineno for n in nodes
          if (isinstance(n, ast.keyword) and n.arg == name)
          or (isinstance(n, ast.Constant) and n.value == name)}
    if star:
        at.update(n.lineno for n in nodes
                  if isinstance(n, ast.Name) and n.id == name)
    return at


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

    So the question is asked STRUCTURALLY (`_string_reads`): a call to
    `getattr`/`setattr`/`delattr`/`hasattr` or to something spelled
    `patch`/`patch.object` whose arguments are the module and the name. And a
    DOTTED string is read wherever it stands, because
    `mock.patch("helm.chat.helper")` carries one token holding its own
    subject -- the spelling the bare-name comparison missed entirely.

    This asks through the module's own name only; the door
    (`_module_reads`) asks the same question through every name an import
    binds to the module. An unparseable source answers False here;
    `_source_parses` is what keeps such a file from reading as clean."""
    try:
        tree = ast.parse(text)
    except _UNPARSED:
        return False
    kinds = set(_string_reads(tree, mod, name, {mod}).values())
    if PATCH_DOTTED in kinds:
        return PATCH_DOTTED
    return PATCH_STRUCTURAL if kinds else None


def _bindings(tree, mod):
    """({names an import binds TO `mod`}, {names an import binds elsewhere}).

    To `mod`: always its own token, the alias of `import helm.mod as m`, and
    the bound name of `from helm import mod [as m]` or `from . import mod`.
    `import helm.mod` binds `helm`, which is not the module; its receiver is
    the dotted chain itself. A plain assignment (`c = mod`) is never
    resolved, and a name bound both ways counts as the module's."""
    to_mod, to_other = {mod}, set()
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
            for a in node.names:
                (to_mod if a.name == mod else to_other).add(a.asname or a.name)
    return to_mod, to_other


def _satellite_paths(here, token):
    """The paths a `_OWNER_NAMES` token names beside the facade in `here`."""
    return {os.path.join(here, token + ".py"),
            os.path.join(here, token, "__init__.py")}


def _module_reads(tree, names, mod, name, cand, retiring):
    """{lineno} -- where ANOTHER file reads `name` FROM `mod` (task/3418).

    A RETIRED NAME IS `mod.name`, NEVER EVERY `name` IN THE TREE. Removing
    `_FULL_TIP` from findingspass was refused with every bare `_FULL_TIP` in
    helm/dispatches.py, which imports findingspass and binds its OWN
    `_FULL_TIP` with an assignment; earlier findingspass._ID the same way. A
    bare `name` in another file is that file's own binding, whatever binds
    it -- an assignment, a def, an import from elsewhere, nothing. It can
    hold the module's value only through a statement that spells the
    module, and each of those is read here in its own right:

      - `from mod import name` or `from mod import *` anywhere in the file,
        and a relative `from . import name`, which may import from the
        package the module belongs to;
      - `.name` on a receiver an import binds to the module, on a receiver
        no import binds (a parameter, `self` or a local can hold the
        module), and on anything that is not a plain name (`pkg.mod.name`);
        `R.name` is cleared only for an R every import binds elsewhere;
      - a string that reaches the module (`_string_reads`): a dotted target,
        getattr/setattr/delattr/hasattr/patch/patch.object/getattr_static/
        __getattribute__/attrgetter, `__dict__`, `vars`, `.get` on either,
        through the module or any alias of it;
      - a facade's `_OWNER_NAMES` entry `("mod", (..., "name"))` whose token
        names the retiring file beside the facade: it publishes the module's
        name as the facade's own, and the publish fails once it is gone;
      - in a file with a DYNAMIC REACH on the module (`_dynamic_reads`), a
        keyword argument named `name` or a string constant equal to it, and
        after `globals().update(vars(mod))` a bare `name` too.

    The dotted string, the facade entry and a `patch.multiple` target can
    carry the module as a string, so they are asked of every file; the rest
    need the module's NAME token in `names`, and a file without it cannot
    spell them. The lines are where the name is read, so the refusal names
    the read and not the file's own unrelated lines."""
    to_mod, to_other = _bindings(tree, mod)
    at = set(_string_reads(tree, mod, name, to_mod))
    at |= _dynamic_reads(tree, mod, name, to_mod)
    here = os.path.dirname(cand)
    at.update(line for token, owned, line in _owner_pairs(tree)
              if owned == name and _satellite_paths(here, token) & retiring)
    if mod not in names:
        return at
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            src = (node.module or "").rpartition(".")[2]
            if src == mod or not node.module:
                at.update(getattr(a, "lineno", node.lineno)
                          for a in node.names if a.name in (name, "*"))
        elif isinstance(node, ast.Attribute) and node.attr == name:
            recv = node.value
            if not (isinstance(recv, ast.Name) and recv.id in to_other
                    and recv.id not in to_mod):
                at.add(node.end_lineno)
    return at


def consumers(root, path, name, live_path=None):
    """([(path, lineno, text)], error) -- where the INDEX still reads it.

    THE QUESTION IS ASKED OF PYTHON, NOT OF TEXT. The file that RETIRED the
    name is a consumer of itself wherever a NAME token of it survives --
    which covers a stale bare use and ignores a comment or a docstring about
    the retirement. Another file is a consumer only where it reads the name
    FROM the module (`_module_reads`): an attribute on the module or a
    receiver that can hold it, an import of the name from it -- the
    parenthesized multi-line import a line-oriented search cannot see
    included -- a string that reaches into it, a facade entry that
    publishes it, or a string or keyword spelling it in a file that reaches
    the module by a computed name. A bare spelling in another file is that
    file's own name and never refuses (task/3418), except after
    `globals().update(vars(mod))`, which binds it from the module.

    `git grep -l` is only a PREFILTER and is deliberately MORE PERMISSIVE than
    the test: an ASCII NAME token, and a string holding the name, are also a
    word-boundary text match, so a file it drops can hold neither. The
    reverse is what a prefilter may never be.

    THE NAMED LIMIT, MEASURED RATHER THAN ASSUMED, and the reason the sentence
    above says ASCII. Python NFKC-normalises identifiers, so a file may spell
    the name in a form whose BYTES differ while the interpreter binds the very
    name being retired -- fullwidth latin is the readable example. That file is
    a real consumer and `git grep -e '\\bNAME\\b'` cannot see it, because the
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
    retiring = {path, live}
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
        if cand in retiring:
            # In the retiring file a bare token IS the module's own name; a
            # mere string or comment is not a use.
            if name in names:
                hits.extend((cand, n, t) for n, t in _lines_with(text, name))
            continue
        try:
            tree = ast.parse(text)
        except _UNPARSED:
            # IT TOKENIZES AND DOES NOT PARSE, so no binding can be resolved:
            # a file holding both the name and the module token is reported.
            if name in names and mod in names:
                hits.extend((cand, n, t) for n, t in _lines_with(text, name))
            continue
        lines = _source_lines(text)
        hits.extend((cand, n, lines[n - 1].rstrip()) for n in sorted(
            _module_reads(tree, names, mod, name, cand, retiring)))
    return hits, None


def _owner_pairs(tree):
    """[(satellite token, NAME, lineno)] for every entry of a module-level
    `_OWNER_NAMES` literal: a sequence of (satellite module token,
    (names...)) pairs. An entry that is not that shape is skipped."""
    out = []
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
            out.extend((mod.value, item.value, item.lineno)
                       for item in names.elts
                       if isinstance(item, ast.Constant)
                       and isinstance(item.value, str))
    return out


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
    except _UNPARSED:
        return None
    return {(mod, name) for mod, name, _line in _owner_pairs(tree)}


def defines_at_top_level(text, name):
    """Does this source bind `name` at column zero? Parsed, never imported."""
    try:
        tree = ast.parse(text)
    except _UNPARSED:
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

    The shape-A clearance in `scan_staged`; a declared satellite is read by
    the wider `defines_at_top_level`.

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
    except _UNPARSED:
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
    print("%s REFUSED: %d retired top-level name(s) still read from their "
          "module in the tree this commit would produce:" % (TAG, len(found)),
          file=out)
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
          "a line is a different thing by the same name (a receiver no import "
          "binds, a relative `from . import`, a string or keyword in a file "
          "that reaches the module by a computed name), one-commit owner "
          "override: HELM_RETIRED_NAME_SKIP=1" % TAG, file=out)


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
