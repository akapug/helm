#!/usr/bin/env python3
"""helm.upstream_surface — what one Claude Code release says about itself.

A RAW STRING DIFF OF TWO RELEASES IS UNUSABLE. Measured on 2.1.280 -> 2.1.281:
about 60,000 added and 52,000 removed printable runs, nearly all of them Bun
bytecode and minifier churn. Nobody can read that, and a model handed it spends
its attention on noise.

THE SOURCE IS NOT THE BINARY, IT IS THE MODULE GRAPH INSIDE IT. A Claude Code
release is a Bun standalone executable: the runtime, then a module graph that
ends in the `---- Bun! ----` trailer. The graph holds every JavaScript chunk as
source text (about 39 MB of ASCII in 2.1.281, a fifth of the 234 MB file) and
every bundled asset — skills, their references, the SDK type declarations —
some of them zstd-compressed or stored as UTF-16. Reading the graph gives real
text in its real encoding; scanning the whole binary as Latin-1 reads the
runtime and the bytecode too.

SCHEMA-AWARE, NOT STRING-AWARE. From the JavaScript this extracts only the
surfaces helm depends on, each keyed so that a changed value pairs with its old
value instead of reading as one addition and one removal:

  settings_keys        the settings schema's dotted key paths
  settings_describes   each settings key's `.describe()` text, keyed by path
  all_describes        every `.describe()` in the program (SDK protocol, tool
                       inputs, hook payloads), keyed by the nearest object key
  flags                CLI options `(flag, help)`, with the subcommand they
                       belong to and whether they are hidden
  commands             CLI subcommands and their descriptions
  envs                 every CLAUDE_* / ANTHROPIC_* name the program spells
  hook_arrays          the hook-event name lists
  hook_meta            the per-event hook metadata table
  models               every `{id:"claude-…"}` catalog object, keyed by id

Minifier noise is masked before comparing: `${…}` interpolation bodies, and
short identifiers inside expression-valued describes and model objects, since a
minifier renames those every build.

BOUNDED. The program is read through one mmap with a size ceiling; every text
this module returns is cut with `pk.cut_marked` so a cut says so; every list in
a diff has a ceiling and reports how many items it dropped. Standard library
only: zstd needs `compression.zstd` (Python 3.14); on an older interpreter the
compressed assets are counted as unreadable and named, never silently skipped.
"""
import difflib
import hashlib
import mmap
import os
import re
import struct
from collections import Counter, defaultdict

from . import pk

try:
    from compression import zstd as _zstd  # Python 3.14+
except ImportError:  # the 3.9 floor: compressed assets report as unreadable
    _zstd = None

PROGRAM_MAX = 1 << 30          # the bound on the program read
MODULES_MAX = 1 << 16          # the bound on the module-graph record count
ASSET_DIFF_MAX = 4 << 20       # an asset larger than this reports its hash only
TEXT_KEEP = 1500               # one changed string, in any diff record
SECTION_KEEP = 150             # records per section before the rest are counted
ASSET_LINES_KEEP = 160         # unified-diff lines per changed asset

_BUN_TRAILER = b"\n---- Bun! ----\n"
_RECORD = 52
_ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
_TEXT_LOADER = 13               # Bun's `text` loader: an embedded text asset
_ASSET_EXTS = (".md", ".txt", ".mjs", ".json")


# ---------------------------------------------------------------------------
# a forward JavaScript tokenizer, good enough for minified bundle slices
# ---------------------------------------------------------------------------

_ID = re.compile(r"[A-Za-z_$\u0080-\uffff][\w$\u0080-\uffff]*")
_NUM = re.compile(r"(?:0[xXbBoO][0-9a-fA-F_]+|\d[\d_]*\.?\d*(?:[eE][+-]?\d+)?"
                  r"|\.\d+(?:[eE][+-]?\d+)?)n?")
_WS = re.compile(r"\s+")
_PUNCT = re.compile(r"\.\.\.|\?\?=|\?\.|\?\?|=>|===|!==|\*\*=|>>>=|<<=|>>=|>>>|"
                    r"&&=|\|\|=|[-+*/%&|^<>!=]=|&&|\|\||\+\+|--|<<|>>|\*\*|"
                    r"[{}()\[\];,<>+\-*/%&|^!~?:=.@#]")
_REGEX_PREV_KW = {"return", "typeof", "case", "do", "else", "in", "of", "new",
                  "delete", "void", "throw", "instanceof", "yield", "await"}
_REGEX_PREV_PUNCT = set("( , = : [ ! & | ? { } ; + - * % < > ~ ^".split()) | {
    "&&", "||", "??", "=>", "==", "===", "!=", "!==", "+=", "-=", "*=", "/=",
    "%=", "&=", "|=", "^=", "<=", ">=", "...", "<<", ">>", ">>>", "&&=", "||=",
    "??="}


def _str_end(s, i):
    q, j, n = s[i], i + 1, len(s)
    while j < n:
        c = s[j]
        if c == "\\":
            j += 2
            continue
        if c == q:
            return j + 1
        if c == "\n":
            return j                       # unterminated: stop at the line
        j += 1
    return n


def _regex_end(s, i):
    j, n, cls = i + 1, len(s), False
    while j < n:
        c = s[j]
        if c == "\\":
            j += 2
            continue
        if c == "\n":
            return None
        if cls:
            if c == "]":
                cls = False
        elif c == "[":
            cls = True
        elif c == "/":
            m = _ID.match(s, j + 1)
            return m.end() if m else j + 1
        j += 1
    return None


def _tpl_end(s, i):
    j, n = i + 1, len(s)
    while j < n:
        c = s[j]
        if c == "\\":
            j += 2
            continue
        if c == "`":
            return j + 1
        if c == "$" and j + 1 < n and s[j + 1] == "{":
            last = j + 1
            for _kind, _a, b in tokenize(s, j + 1, stop_at_depth_zero=True):
                last = b
            j = last
            continue
        j += 1
    return n


def tokenize(s, i=0, end=None, stop_at_depth_zero=False):
    """Yield (kind, start, end) over s[i:end]; kind is id, str, tpl, num, re or
    punct. Start it at a code position, never inside a string. With
    `stop_at_depth_zero` the first token must open a bracket, and the walk
    stops after the bracket that closes it."""
    n = len(s) if end is None else end
    prev = None
    depth = 0
    while i < n:
        c = s[i]
        if c in " \t\r\n":
            i = _WS.match(s, i).end()
            continue
        if c == "/" and i + 1 < n and s[i + 1] == "/":
            k = s.find("\n", i)
            i = n if k < 0 else k
            continue
        if c == "/" and i + 1 < n and s[i + 1] == "*":
            k = s.find("*/", i + 2)
            i = n if k < 0 else k + 2
            continue
        if c in "\"'":
            j = _str_end(s, i)
            yield ("str", i, j)
            prev, i = ("str", None), j
            continue
        if c == "`":
            j = _tpl_end(s, i)
            yield ("tpl", i, j)
            prev, i = ("tpl", None), j
            continue
        if c == "/" and (prev is None
                         or (prev[0] == "punct" and prev[1] in _REGEX_PREV_PUNCT
                             and prev[1] not in (")", "]"))
                         or (prev[0] == "id" and prev[1] in _REGEX_PREV_KW)):
            j = _regex_end(s, i)
            if j is not None:
                yield ("re", i, j)
                prev, i = ("re", None), j
                continue
        m = _ID.match(s, i)
        if m:
            yield ("id", i, m.end())
            prev, i = ("id", m.group()), m.end()
            continue
        if c.isdigit() or (c == "." and i + 1 < n and s[i + 1].isdigit()):
            m = _NUM.match(s, i)
            yield ("num", i, m.end())
            prev, i = ("num", None), m.end()
            continue
        m = _PUNCT.match(s, i)
        if not m:
            i += 1
            continue
        t = m.group()
        yield ("punct", i, m.end())
        prev, i = ("punct", t), m.end()
        if t in "([{":
            depth += 1
        elif t in ")]}":
            depth -= 1
            if stop_at_depth_zero and depth == 0:
                return


def literal_value(tok):
    """The decoded value of a string or template token's source text."""
    import json
    q, body = tok[0], tok[1:-1]
    if q == "`":
        return body
    try:
        if q == '"':
            return json.loads(tok)
        return json.loads('"' + body.replace("\\'", "'").replace('"', '\\"') + '"')
    except ValueError:
        return body


# ---------------------------------------------------------------------------
# the Bun module graph
# ---------------------------------------------------------------------------

def bun_modules(path):
    """-> ([(name, encoding, loader, bytes)], None) or (None, why).

    The trailer is the last `---- Bun! ----` marker; the 32 bytes before it
    are the graph header (byte count, then the module table's offset and
    length). Every offset is checked against the graph before it is read, so
    a file that is not a Bun executable is refused by name, never misread."""
    try:
        f = open(path, "rb")
    except OSError as e:
        return None, "%s cannot be opened (%s)" % (path, e.strerror or e)
    with f:
        size = os.fstat(f.fileno()).st_size
        if size > PROGRAM_MAX:
            return None, "%s is larger than the %d-byte read bound" % (path, PROGRAM_MAX)
        if size < 64:
            return None, "%s is too small to hold a Bun module graph" % path
        with mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as m:
            t = m.rfind(_BUN_TRAILER)
            if t < 32:
                return None, "%s has no Bun module-graph trailer" % path
            graph, table, table_len = struct.unpack("<QII", m[t - 32:t - 16])
            base = t - 32 - graph
            count = table_len // _RECORD
            if (base < 0 or table_len % _RECORD or table + table_len > graph
                    or count > MODULES_MAX):
                return None, "%s has a Bun trailer whose module table is out of bounds" % path
            out = []
            for k in range(count):
                at = base + table + k * _RECORD
                rec = m[at:at + _RECORD]
                name_at, name_len, body_at, body_len = struct.unpack("<4I", rec[:16])
                if name_at + name_len > graph or body_at + body_len > graph:
                    return None, "%s module record %d points outside the graph" % (path, k)
                name = m[base + name_at:base + name_at + name_len].decode("utf-8", "replace")
                out.append((name, rec[48], rec[49],
                            m[base + body_at:base + body_at + body_len]))
    return out, None


def _decode(name, encoding, body):
    """-> (text, None), or (None, why) for a module that is not readable text."""
    if body[:4] == _ZSTD_MAGIC:
        if _zstd is None:
            return None, "zstd-compressed and this interpreter has no compression.zstd"
        try:
            return _zstd.decompress(body).decode("utf-8", "replace"), None
        except Exception as e:  # noqa: BLE001 — one bad asset is named, not fatal
            return None, "zstd decompression failed (%s)" % type(e).__name__
    if encoding == 2:
        return body.decode("utf-16-le", "replace"), None
    if encoding == 1:
        return body.decode("latin-1"), None
    return None, "binary module"


def program_text(modules):
    """The JavaScript of the graph, in table order, one module per block.
    Table order keeps a schema and the helpers it calls near each other, which
    the settings walk relies on."""
    parts = []
    for name, encoding, loader, body in modules:
        if name.endswith(".js") and loader == 1:
            text, _why = _decode(name, encoding, body)
            if text is not None:
                parts.append(text)
    return "\n;\n".join(parts)


_HASHED = re.compile(r"-[a-z0-9]{8}(?=\.[^.]+$)")
_FRONT_NAME = re.compile(r"\A\s*(?:---|<!--)\s*\n(?:.*\n){0,3}?name:\s*([^\n]{1,80})")


def assets(modules):
    """-> ({key: {"base", "sha", "text"}}, [unreadable (name, why)]).

    The bundled text assets: skills, their references and templates, and the
    SDK type declarations. Bun names each with a content hash
    (`SKILL-3kq9x2mz.md`), so the key drops the hash and adds what tells same-
    named assets apart — a front-matter `name:` or the first line. Assets that
    still collide are numbered in content order."""
    found, unreadable = defaultdict(list), []
    for name, encoding, loader, body in modules:
        base = name.rsplit("/", 1)[-1]
        plain = base[:-4] if base.endswith(".zst") else base
        if not plain.endswith(_ASSET_EXTS):
            continue
        if loader != _TEXT_LOADER and not base.endswith(".zst"):
            continue
        text, why = _decode(name, encoding, body)
        if text is None:
            unreadable.append((plain, why))
            continue
        m = _FRONT_NAME.search(text[:600])
        tag = m.group(1).strip() if m else next(
            (ln.strip() for ln in text.splitlines() if ln.strip()), "")
        key = "%s#%s" % (_HASHED.sub("", plain), pk.cut_marked(tag, 60))
        found[key].append({"base": plain, "text": text,
                           "sha": hashlib.sha256(text.encode("utf-8")).hexdigest()})
    out = {}
    for key, rows in found.items():
        rows.sort(key=lambda r: r["sha"])
        for i, row in enumerate(rows):
            out[key if i == 0 else "%s#%d" % (key, i + 1)] = row
    return out, unreadable


# ---------------------------------------------------------------------------
# schema-aware extraction over the program's JavaScript
# ---------------------------------------------------------------------------

_SETTINGS_ANCHOR = "{$schema:"
_SETTINGS_MARK = "JSON Schema reference for Claude Code settings"
_DESCRIBE_CALL = re.compile(r"\.describe\(")
_KEY_TAIL = re.compile(r"([\w$]+|\"[^\"\\]*\")$")
_STR = r"(\"(?:[^\"\\\x00-\x1f]|\\.)*\"|'(?:[^'\\\x00-\x1f]|\\.)*'|`(?:[^`\\\x00]|\\.)*`)"
_FLAG = re.compile(r"\(\s*([\"'])(-{1,2}[A-Za-z][^\"'`\\\x00-\x1f]{0,120})\1\s*,\s*" + _STR)
_CONCAT = re.compile(r"\s*\+\s*" + _STR)
_HIDDEN = re.compile(r"[^;]{0,160}?\.hideHelp\(")
_COMMAND_CTX = re.compile(r"\.command\(([\"'`])([^\"'`\\]{1,80})\1")
_COMMAND = re.compile(r"\.command\((\"(?:[^\"\\\x00-\x1f]|\\.)*\"|'(?:[^'\\\x00-\x1f]|\\.)*')")
_DESCRIPTION = re.compile(r"\.description\(" + _STR + r"\)")
_ENV = re.compile(r"(?<=[.\"'`{,\s(\[!$=:?&|])_?(?:CLAUDE|ANTHROPIC)_[A-Z0-9_]*[A-Z0-9]"
                  r"(?=[.\"'`:,)\]}=;\s?&|!+])")
_HOOK_ARRAY = re.compile(r"\[(?:\"[A-Za-z]+\",){2,}\"[A-Za-z]+\"\]")
_MODEL_OBJ = re.compile(r"\{id:\"claude-[a-z0-9-]+\"")
_SHAPE = re.compile(r"(shape|permissionsShape):\(\)=>\(\{")
_DEF = re.compile(r"function ([\w$]+)\(|(?<=[,;{\s])([\w$]+)="
                  r"(?=[\w$]+\(\(\)=>|\(\)=>|\([\w$,]*\)=>|function)")


class _Program(object):
    """One program's JavaScript and the walks over it. Every walk is bounded
    by an explicit window, so a malformed program costs a missing section,
    never an unbounded scan."""

    def __init__(self, s):
        self.s = s

    def txt(self, tok):
        return self.s[tok[1]:tok[2]]

    def lit(self, a, b):
        return literal_value(self.s[a:b])

    # -- one `.describe(` argument ------------------------------------------
    def describe_arg(self, paren):
        """The argument of the call whose `(` is at `paren`: literal pieces
        joined by `+` are concatenated; anything else is kept as source."""
        s = self.s
        toks = list(tokenize(s, paren, min(len(s), paren + 20000), stop_at_depth_zero=True))
        if not toks:
            return "<?>"
        inner = toks[1:-1]
        if inner and len(inner) % 2 == 1 and all(
                (k in ("str", "tpl")) if ix % 2 == 0 else (s[a:b] == "+")
                for ix, (k, a, b) in enumerate(inner)):
            return "".join(self.lit(a, b) for ix, (_k, a, b) in enumerate(inner)
                           if ix % 2 == 0)
        return "<expr:" + pk.cut_marked(s[paren + 1:toks[-1][1]], 400) + ">"

    # -- the enclosing `function NAME(...){...}` of a position -----------------
    def fn_span(self, pos):
        s = self.s
        k = pos
        while True:
            k = s.rfind("function ", 0, k)
            if k < 0 or pos - k > 400000:
                return None
            toks = tokenize(s, k)
            try:
                next(toks)
                t = next(toks)
                if t[0] != "id":
                    continue
                name = self.txt(t)
                if self.txt(next(toks)) != "(":
                    continue
                t = self._close(toks, 1)
                body = next(toks)
                if self.txt(body) != "{":
                    continue
                t = self._close(toks, 1)
            except StopIteration:
                return None
            if t is not None and t[2] > pos:
                return name, k, body[1], t[2]

    def _close(self, toks, depth):
        """Consume tokens until the bracket depth returns to zero."""
        t = None
        for t in toks:
            x = self.txt(t)
            if t[0] == "punct" and x in "([{":
                depth += 1
            elif t[0] == "punct" and x in ")]}":
                depth -= 1
                if depth == 0:
                    return t
        return t

    # -- one walk over an object-schema region --------------------------------
    def walk(self, a, b, callees, base_path=(), force_first=False):
        """-> (keys, describes, toks) over s[a:b]. keys: {dotted path: None};
        describes: [(path, direct, text)]."""
        keys, describes, stack = {}, [], []
        toks = list(tokenize(self.s, a, b))
        n = len(toks)
        txt = self.txt
        for idx, t in enumerate(toks):
            kind, x = t[0], txt(t)
            if kind == "punct" and x in "([{":
                prevx = txt(toks[idx - 1]) if idx else ""
                prev2 = toks[idx - 2] if idx > 1 else None
                fr = {"open": x, "key": None, "schema": False, "fn": prevx == "=>"}
                if (x == "{" and prevx == "(" and prev2 is not None
                        and prev2[0] == "id" and txt(prev2) in callees):
                    fr["schema"] = True
                if x == "{" and force_first and not stack and not fr["schema"]:
                    fr["schema"] = True
                    force_first = False
                encl = [f for f in stack if f["open"] == "{"]
                if (x == "{" and prevx in ("&&", "?", ":", "||", "...", "(") and encl
                        and encl[-1]["schema"] and encl[-1]["key"] is None
                        and encl[-1].get("spread")):
                    fr["schema"] = True
                if x == "{":
                    fr["expect"] = True
                stack.append(fr)
                continue
            if kind == "punct" and x in ")]}":
                if stack:
                    stack.pop()
                continue
            top = stack[-1] if stack else None
            if top and top["open"] == "{":
                if x == "," and kind == "punct":
                    top["expect"] = True
                    continue
                if top.get("expect"):
                    top["expect"] = False
                    if kind in ("id", "str", "num") and idx + 1 < n and txt(toks[idx + 1]) == ":":
                        top["key"] = literal_value(x) if kind == "str" else x
                        top["spread"] = False
                        objs = [f for f in stack if f["open"] == "{"]
                        first = next((ix for ix, f in enumerate(objs) if f["schema"]), None)
                        inner = stack[stack.index(objs[first]):] if first is not None else []
                        if (top["schema"] and all(f["schema"] for f in objs[first:])
                                and not any(f["fn"] for f in inner)):
                            path = base_path + tuple(f["key"] for f in objs[first:]
                                                     if f["key"] is not None)
                            keys.setdefault(".".join(path), None)
                        continue
                    if x == "...":
                        top["key"], top["spread"] = None, True
                        continue
            if (kind == "id" and x == "describe" and idx and txt(toks[idx - 1]) == "."
                    and idx + 3 < n and txt(toks[idx + 1]) == "("):
                objs = [f for f in stack if f["open"] == "{"]
                first = next((ix for ix, f in enumerate(objs) if f["schema"]), len(objs))
                path = base_path + tuple(f["key"] or "?" for f in objs[first:]
                                         if not (f["key"] is None and f.get("spread")))
                direct = bool(stack) and stack[-1]["open"] == "{" and stack[-1]["schema"]
                describes.append((".".join(path), direct, self.describe_arg(toks[idx + 1][1])))
        return keys, describes, toks

    # -- the settings schema --------------------------------------------------
    def settings(self):
        """-> (keys, describes, None) or (None, None, why)."""
        s = self.s
        anchor = None
        for m in re.finditer(re.escape(_SETTINGS_ANCHOR), s):
            if _SETTINGS_MARK in s[m.start():m.start() + 200]:
                anchor = m.start()
                break
        if anchor is None:
            return None, None, "the settings schema anchor (%r) was not found" % _SETTINGS_MARK
        m = re.search(r"return\s*([\w$]+)\($", s[max(0, anchor - 40):anchor])
        span = self.fn_span(anchor)
        if not m or not span:
            return None, None, "the settings schema's enclosing function was not found"
        root_callee = m.group(1)
        _fname, fstart, bopen, fend = span
        callees = {root_callee, "extend", "object", "strictObject", "looseObject", "merge"}
        locals_ = self._locals(bopen, fend)
        local_names = {name for name, _a, _b in locals_}
        root_keys, root_desc, _t = self.walk(anchor - len(root_callee) - 1, fend - 1, callees)
        refs = defaultdict(set)
        key_re = re.compile(r"[{,]([\w$]+):")
        for name in local_names:
            for hit in re.finditer(r"(?<![\w$.])" + re.escape(name) + r"(?![\w$])",
                                   s[anchor:fend]):
                pos = anchor + hit.start()
                keys_before = list(key_re.finditer(s[max(anchor, pos - 300):pos]))
                if keys_before:
                    refs[name].add(keys_before[-1].group(1))
        keys, describes = dict(root_keys), list(root_desc)
        for name, a, b in locals_:
            top = [r for r in sorted(refs.get(name, ())) if r in root_keys]
            base = (top[0],) if len(top) == 1 else ("<" + name + ">",)
            k, d, _t = self.walk(a, b, callees, base)
            keys.update(k)
            describes.extend(d)
        k, d = self._external(anchor, fend, callees, root_callee, local_names)
        keys.update(k)
        describes.extend(d)
        k, d = self._registry_shapes(fstart, callees)
        keys.update(k)
        describes.extend(d)
        return sorted(keys), describes, None

    def _locals(self, bopen, fend):
        """[(name, start, end)] of `NAME=<init>` declared at the function body's
        top level, each running to the next top-level `,` / `;` / `return`."""
        s = self.s
        toks = list(tokenize(s, bopen + 1, fend - 1))
        out, depth, cur, i = [], 0, None, 0
        while i < len(toks):
            t = toks[i]
            x = s[t[1]:t[2]]
            if t[0] == "punct" and x in "([{":
                depth += 1
            elif t[0] == "punct" and x in ")]}":
                depth -= 1
            elif depth == 0:
                if (t[0] == "id" and x in ("let", "var", "const")) or (
                        t[0] == "punct" and x == "," and cur is not None):
                    if cur:
                        out.append((cur[0], cur[1], t[1]))
                        cur = None
                    if i + 3 < len(toks):
                        nt, eq = toks[i + 1], toks[i + 2]
                        if nt[0] == "id" and s[eq[1]:eq[2]] == "=":
                            cur = (s[nt[1]:nt[2]], toks[i + 3][1])
                            i += 3
                            continue
                elif (t[0] == "punct" and x == ";") or (t[0] == "id" and x == "return"):
                    if cur:
                        out.append((cur[0], cur[1], t[1]))
                        cur = None
            i += 1
        return out

    def _top_level_calls(self, a, b):
        """{top-level key: [callee names]} of the object literal at s[a]."""
        s = self.s
        toks = list(tokenize(s, a, b))
        vals, depth, expect, cur = defaultdict(list), 0, False, None
        for i, (k, x0, x1) in enumerate(toks):
            x = s[x0:x1]
            if k == "punct" and x in "([{":
                depth += 1
                if depth == 1:
                    expect = True
                    continue
            elif k == "punct" and x in ")]}":
                depth -= 1
                if depth == 0:
                    break
            if depth == 1 and x == ",":
                expect, cur = True, None
                continue
            nxt = s[toks[i + 1][1]:toks[i + 1][2]] if i + 1 < len(toks) else ""
            if depth == 1 and expect:
                expect = False
                if k == "id" and nxt == ":":
                    cur = x
                    continue
            if cur and k == "id" and nxt == "(" and i and s[toks[i - 1][1]:toks[i - 1][2]] not in (".", "new"):
                vals[cur].append(x)
        return vals

    def _find_def(self, defs, name, near):
        s = self.s
        best = min((p for p in defs.get(name, ()) if abs(p - near) < 400000),
                   key=lambda p: abs(p - near), default=None)
        if best is None:
            return None
        is_fn = s.startswith("function ", best)
        depth, started, end = 0, False, None
        for k, x0, x1 in tokenize(s, best, min(len(s), best + 300000)):
            x = s[x0:x1]
            if k == "punct" and x in "([{":
                depth += 1
                started = True
            elif k == "punct" and x in ")]}":
                depth -= 1
            if started and depth == 0 and k == "punct" and x in "})":
                if is_fn and x != "}":
                    continue
                end = x1
                break
        return None if end is None else (best, end)

    def _external(self, anchor, fend, callees, root_callee, local_names):
        """Sub-schemas the root's top-level keys build by calling other schema
        builders, resolved up to five calls deep."""
        s = self.s
        lo = max(0, anchor - 6000000)
        defs = defaultdict(list)
        for m in _DEF.finditer(s, lo, min(len(s), anchor + 6000000)):
            defs[m.group(1) or m.group(2)].append(m.start())
        keys, describes, visited = {}, [], set()

        def resolve(names, base, level, near):
            for name in names:
                if ((name, base) in visited or level > 5 or name == root_callee
                        or name in local_names):
                    continue
                visited.add((name, base))
                span = self._find_def(defs, name, near)
                if not span:
                    continue
                a, b = span
                if "_zod" in s[a:b] or 'jn("' in s[a:b]:
                    continue       # the schema library's own internals
                k, d, toks = self.walk(a, b, callees, base,
                                       force_first=not s.startswith("function ", a))
                keys.update(k)
                describes.extend(d)
                sub = sorted({s[t1[1]:t1[2]] for t0, t1, t2 in zip(toks, toks[1:], toks[2:])
                              if t1[0] == "id" and s[t2[1]:t2[2]] == "("
                              and s[t0[1]:t0[2]] not in (".", "new")})
                resolve(sub, base, level + 1, a)

        for key, names in self._top_level_calls(anchor, fend).items():
            resolve(sorted(set(names)), (key,), 1, anchor)
        return keys, describes

    def _registry_shapes(self, fstart, callees):
        """The settings-module registry: `{mod:{buildGate:…, shape:()=>({…})}}`
        spread into the root; `permissionsShape` lands under `permissions`."""
        s = self.s
        lo = max(0, fstart - 300000)
        keys, describes = {}, []
        for m in _SHAPE.finditer(s, lo, fstart):
            base = () if m.group(1) == "shape" else ("permissions",)
            a = m.end() - 1
            last = a
            for tk in tokenize(s, a, min(len(s), a + 300000), stop_at_depth_zero=True):
                last = tk[2]
            k, d, _t = self.walk(a, last, callees, base, force_first=True)
            keys.update(k)
            describes.extend(d)
        return keys, describes

    # -- the other surfaces ---------------------------------------------------
    def key_guess(self, pos):
        """Walk back from pos at bracket depth zero to the nearest `KEY:`."""
        s = self.s
        depth, j, lo = 0, pos - 1, max(0, pos - 4000)
        while j > lo:
            c = s[j]
            if c in ")]}":
                depth += 1
            elif c in "([{":
                if depth == 0:
                    return None
                depth -= 1
            elif c in "\"'`":
                k = j - 1
                while k > lo and not (s[k] == c and s[k - 1] != "\\"):
                    k -= 1
                j = k
            elif c == ":" and depth == 0:
                m = _KEY_TAIL.search(s[max(lo, j - 80):j])
                if m and s[j - len(m.group(1)) - 1] in ",{":
                    return m.group(1).strip('"')
                return None
            elif c == "," and depth == 0:
                return None
            j -= 1
        return None

    def all_describes(self):
        s = self.s
        out = []
        for m in _DESCRIBE_CALL.finditer(s):
            before = s[m.start() - 1] if m.start() else ""
            if not before.isalnum() and before not in ")]_$":
                continue
            out.append((self.key_guess(m.start()), self.describe_arg(m.end() - 1)))
        return out

    def flags(self):
        s = self.s
        out = []
        for m in _FLAG.finditer(s):
            hidden = bool(_HIDDEN.match(s, m.end(), min(len(s), m.end() + 200)))
            ctx = ""
            for cm in _COMMAND_CTX.finditer(s, max(0, m.start() - 6000), m.start()):
                ctx = cm.group(2)
            help_ = self.lit(m.start(3), m.end(3))
            at = m.end()
            while True:
                mm = _CONCAT.match(s, at, min(len(s), at + 4000))
                if not mm:
                    break
                help_ += literal_value(mm.group(1))
                at = mm.end()
            out.append((m.group(2), help_, hidden, ctx))
        return out

    def commands(self):
        s = self.s
        out = []
        for m in _COMMAND.finditer(s):
            dm = _DESCRIPTION.search(s, m.end(), min(len(s), m.end() + 400))
            out.append((self.lit(m.start(1), m.end(1)),
                        self.lit(dm.start(1), dm.end(1)) if dm else ""))
        return out

    def envs(self):
        return sorted(set(m.group() for m in _ENV.finditer(self.s)))

    def hooks(self):
        s = self.s
        arrays = sorted({m.group() for m in _HOOK_ARRAY.finditer(s)
                         if '"PreToolUse"' in m.group() or '"SessionStart"' in m.group()
                         or ('"Stop"' in m.group() and '"SubagentStop"' in m.group())})
        meta = {}
        at = s.find("{PreToolUse:{summary:")
        if at < 0:
            return arrays, meta
        toks = list(tokenize(s, at, min(len(s), at + 400000), stop_at_depth_zero=True))
        depth, event = 0, None
        for idx, t in enumerate(toks):
            x = self.txt(t)
            if t[0] == "punct" and x in "([{":
                depth += 1
                continue
            if t[0] == "punct" and x in ")]}":
                depth -= 1
                continue
            if (idx + 2 < len(toks) and self.txt(toks[idx + 1]) == ":"
                    and t[0] in ("id", "str") and self.txt(toks[idx - 1]) in ",{"):
                k = literal_value(x) if t[0] == "str" else x
                if depth == 1:
                    event = k
                    meta[k] = {}
                elif depth == 2 and event is not None:
                    nt = toks[idx + 2]
                    meta[event][k] = (self.lit(nt[1], nt[2]) if nt[0] in ("str", "tpl")
                                      else "<%s...>" % self.txt(nt))
        return arrays, meta

    def models(self):
        """[(id, normalized object source)] for every `{id:"claude-…"` object."""
        s = self.s
        out = []
        for m in _MODEL_OBJ.finditer(s):
            end = m.start()
            for _k, _a, b in tokenize(s, m.start(), min(len(s), m.start() + 4000),
                                      stop_at_depth_zero=True):
                end = b
            model = s[m.start() + 5:m.end() - 1]
            out.append((model, pk.cut_marked(_mask(s[m.start():end]), 800)))
        return out


# Words a minifier never renames; everything else of four characters or fewer
# outside a key position is a minified name.
_KEEP_WORDS = {"true", "null", "new", "void", "this", "var", "let", "if",
               "else", "case", "in", "of", "do", "for", "try"}


def _mask(text):
    """Minifier-renamed identifiers become `#`, so a rebuild that renamed them
    compares equal. Only identifier TOKENS are masked (never string contents),
    and never an object key or a property read after a dot."""
    toks = list(tokenize(text))
    out, last = [], 0
    for i, (kind, a, b) in enumerate(toks):
        word = text[a:b]
        if kind != "id" or len(word) > 4 or word in _KEEP_WORDS:
            continue
        nxt = text[toks[i + 1][1]:toks[i + 1][2]] if i + 1 < len(toks) else ""
        prv = text[toks[i - 1][1]:toks[i - 1][2]] if i else ""
        if nxt == ":" or prv in (".", "?."):
            continue
        out.append(text[last:a])
        out.append("#")
        last = b
    out.append(text[last:])
    return "".join(out)


def extract(s):
    """-> the surface dict of one program's JavaScript. A section that could
    not be read is absent from the dict and named in `unavailable`."""
    p = _Program(s)
    out = {"unavailable": []}
    keys, describes, why = p.settings()
    if why:
        out["unavailable"].append("settings: " + why)
    else:
        out["settings_keys"] = keys
        out["settings_describes"] = describes
    out["all_describes"] = p.all_describes()
    out["flags"] = p.flags()
    out["commands"] = p.commands()
    out["envs"] = p.envs()
    out["hook_arrays"], out["hook_meta"] = p.hooks()
    out["models"] = p.models()
    return out


def read_version(path):
    """-> ({"surface", "assets", "unreadable"}, None) or (None, why)."""
    modules, why = bun_modules(path)
    if modules is None:
        return None, why
    found, unreadable = assets(modules)
    return {"surface": extract(program_text(modules)), "assets": found,
            "unreadable": unreadable}, None


# ---------------------------------------------------------------------------
# the diff
# ---------------------------------------------------------------------------

def _norm(t):
    """Strip minifier noise: `${…}` bodies, and identifiers in expressions."""
    t = str(t)
    out, i, n = [], 0, len(t)
    while i < n:
        if t.startswith("${", i):
            depth, j = 1, i + 2
            while j < n and depth:
                depth += {"{": 1, "}": -1}.get(t[j], 0)
                j += 1
            out.append("${}")
            i = j
            continue
        out.append(t[i])
        i += 1
    t = "".join(out)
    if t.startswith("<expr:"):
        t = re.sub(r"[A-Za-z_$][\w$]*", "X", t)
    return t


def _cut(text):
    return pk.cut_marked(text, TEXT_KEEP)


def _paired(a_items, b_items):
    """Multiset diff of [(key, text)] on (key, normalized text). One removal
    and one addition under the same key is one CHANGED record."""
    na = Counter(_norm(t) for _k, t in a_items)
    nb = Counter(_norm(t) for _k, t in b_items)
    ca = Counter((k, _norm(t)) for k, t in a_items)
    cb = Counter((k, _norm(t)) for k, t in b_items)
    first_a, first_b = {}, {}
    for k, t in a_items:
        first_a.setdefault((k, _norm(t)), t)
    for k, t in b_items:
        first_b.setdefault((k, _norm(t)), t)
    # a key guess that moved while its text stayed is guess noise, not a change
    add = {kt: c for kt, c in (cb - ca).items() if nb[kt[1]] > na[kt[1]]}
    rem = {kt: c for kt, c in (ca - cb).items() if na[kt[1]] > nb[kt[1]]}
    added, removed = defaultdict(list), defaultdict(list)
    for (k, nt), c in add.items():
        added[k] += [first_b[(k, nt)]] * c
    for (k, nt), c in rem.items():
        removed[k] += [first_a[(k, nt)]] * c
    out = []
    for k in sorted(set(added) | set(removed), key=str):
        if k is not None and len(added[k]) == len(removed[k]) == 1:
            out.append({"op": "changed", "key": k, "old": _cut(removed[k][0]),
                        "new": _cut(added[k][0])})
            continue
        out.extend({"op": "added", "key": k, "new": _cut(t)} for t in added[k])
        out.extend({"op": "removed", "key": k, "old": _cut(t)} for t in removed[k])
    return out


def _sets(a, b):
    a, b = set(a), set(b)
    return ([{"op": "added", "key": k} for k in sorted(b - a)]
            + [{"op": "removed", "key": k} for k in sorted(a - b)])


def _collapse(records):
    """Identical records (the same object repeated across chunks) become one
    record carrying `times`, so a repeat is counted rather than re-read."""
    seen, out = {}, []
    for r in records:
        key = (r["op"], str(r.get("key")), r.get("old"), r.get("new"))
        if key in seen:
            seen[key]["times"] = seen[key].get("times", 1) + 1
            continue
        seen[key] = dict(r)
        out.append(seen[key])
    return out


def diff_surface(a, b):
    """-> {section: [record]} of every surface both versions could read. A
    section one side could not read is named in `unavailable`, never
    reported as all-added or all-removed."""
    out = {"unavailable": sorted(set(a.get("unavailable", ())) | set(b.get("unavailable", ())))}
    if "settings_keys" in a and "settings_keys" in b:
        out["settings_keys"] = _sets(a["settings_keys"], b["settings_keys"])
        out["settings_describes"] = _paired(
            [(p, t) for p, _d, t in a["settings_describes"]],
            [(p, t) for p, _d, t in b["settings_describes"]])
    out["all_describes"] = _paired([tuple(x) for x in a["all_describes"]],
                                   [tuple(x) for x in b["all_describes"]])
    out["flags"] = _paired([("%s %s" % (c, f), h) for f, h, _x, c in a["flags"]],
                           [("%s %s" % (c, f), h) for f, h, _x, c in b["flags"]])
    shown = {f: [(c, "hidden" if x else "shown") for g, _h, x, c in b["flags"] if g == f]
             for f, _h, _x, _c in b["flags"]}
    old_names = {f for f, _h, _x, _c in a["flags"]}
    out["flag_names"] = [dict(r, where=shown.get(r["key"], [])) if r["op"] == "added" else r
                         for r in _sets(old_names, shown)]
    out["commands"] = _paired([tuple(x) for x in a["commands"]],
                              [tuple(x) for x in b["commands"]])
    out["envs"] = _sets(a["envs"], b["envs"])
    out["hook_arrays"] = _sets(a["hook_arrays"], b["hook_arrays"])
    out["hook_meta"] = [
        {"op": "changed", "key": ev, "old": _cut(a["hook_meta"].get(ev)),
         "new": _cut(b["hook_meta"].get(ev))}
        for ev in sorted(set(a["hook_meta"]) | set(b["hook_meta"]))
        if a["hook_meta"].get(ev) != b["hook_meta"].get(ev)]
    out["models"] = _paired([tuple(x) for x in a["models"]], [tuple(x) for x in b["models"]])
    return {k: v if k == "unavailable" else _collapse(v) for k, v in out.items()}


def diff_assets(a, b):
    """-> [record] over the bundled text assets, with a bounded unified diff
    for each changed one."""
    out = []
    for key in sorted(set(a) | set(b)):
        old, new = a.get(key), b.get(key)
        if old and new and old["sha"] == new["sha"]:
            continue
        if not old:
            out.append({"op": "added", "key": key, "new": _cut(new["text"])})
        elif not new:
            out.append({"op": "removed", "key": key, "old": _cut(old["text"])})
        elif max(len(old["text"]), len(new["text"])) > ASSET_DIFF_MAX:
            out.append({"op": "changed", "key": key,
                        "diff": "too large to diff (%d -> %d chars); sha %s -> %s" % (
                            len(old["text"]), len(new["text"]),
                            old["sha"][:12], new["sha"][:12])})
        else:
            lines = list(difflib.unified_diff(old["text"].splitlines(),
                                              new["text"].splitlines(),
                                              lineterm="", n=2))[2:]
            kept = lines[:ASSET_LINES_KEEP]
            if len(lines) > len(kept):
                kept.append("… [cut: %d of %d diff lines]" % (len(kept), len(lines)))
            out.append({"op": "changed", "key": key, "diff": "\n".join(kept)})
    return out


def bounded(sections, keep=SECTION_KEEP):
    """-> ({section: records kept}, {section: records dropped}): every list
    over `keep` is cut to it, and the drop is counted, never silent."""
    kept, dropped = {}, {}
    for name, records in sections.items():
        if isinstance(records, list) and len(records) > keep and name != "unavailable":
            kept[name] = records[:keep]
            dropped[name] = len(records) - keep
        else:
            kept[name] = records
    return kept, dropped


def diff_versions(old_path, new_path):
    """-> ({"surface", "assets", "counts", "dropped", "unreadable"}, None)
    or (None, why) when either program cannot be read at all."""
    a, why = read_version(old_path)
    if a is None:
        return None, why
    b, why = read_version(new_path)
    if b is None:
        return None, why
    surface = diff_surface(a["surface"], b["surface"])
    counts = {k: len(v) for k, v in surface.items()
              if isinstance(v, list) and k != "unavailable"}
    asset_records = diff_assets(a["assets"], b["assets"])
    counts["assets"] = len(asset_records)
    surface, dropped = bounded(surface)
    kept_assets, asset_drop = bounded({"assets": asset_records})
    dropped.update(asset_drop)
    return {"surface": surface, "assets": kept_assets["assets"], "counts": counts,
            "dropped": dropped,
            "unreadable": sorted({"%s: %s" % (n, w) for n, w in b["unreadable"]})}, None
