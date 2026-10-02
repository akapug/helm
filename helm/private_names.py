#!/usr/bin/env python3
"""The operator's private names: the ONE list, its scanner, and the pre-commit
rung that refuses one entering a public-bound path.

The names of the operator's own projects, hosts and predecessor tools are
facts about one machine. Where helm's behaviour needs one, that machine's
local-names file supplies it (helm/localnames.py) and the source carries a
neutral default; where a name was only prose, the prose says what it meant
without it. Two readers hold that line and both read THIS list:
tests/test_no_private_names.py over every tracked file under helm/, and the
`--staged` rung below over every line a commit adds to a public-bound path.
There is no second copy, so a name added here is refused everywhere at once.

THE LIST CARRIES NO PRIVATE NAME IN THE CLEAR. A list that spelled the names
it guards would put them into the tree it protects, so each entry is the
UPPER-CASE hex of the name's UTF-8 bytes. That is an encoding, not a secret:
it keeps the names out of every grep, scanner and reader of the tree, and
nothing more. Upper case because the citation rung reads a lower-case hex run
of seven or more characters as a commit reference. To add a name, append
`"<name>".encode().hex().upper()`.

WHAT COUNTS AS A HIT. Text splits into words at every character outside
[A-Za-z0-9], case folded, so `NAME_ROOTS`, `~/.cache/name/` and `NAME:` all
hit and `names` does not. A two-word name hits two words joined by exactly one
of `-`, `.` or `_`, so each of its spellings is one name.

NO EXEMPTION FOR A SEAT HANDLE. A private name inside a seat handle
(`@<name>-claude`, `by <name>-codex-2`) is a hit like any other: a comment
that credits a project's seat names the project.

THE RUNG (`--staged`). Every line the staged set ADDS under a public-bound
path (`public_bound`: helm/, docs/, tests/, agents/, bin/, scripts/, the
change notes in changes/, and the top-level README and documentation files)
is scanned, and a hit REFUSES the commit naming the path, the line and the
list entry's number. The name itself is never printed, so a refusal pasted
into a chat room does not repeat the leak. A new file, and a file renamed into a public-bound path from outside
one, adds every line. A line moved between public-bound files is judged like
any other added line: a private name is a leak wherever it lands, and the
committer is already holding the line. Staged bytes are read by index OID
through the conflict-marker rung's seams, never from the worktree, and a
staged set that cannot be read REFUSES as UNKNOWN. One-commit owner override:
HELM_PRIVATE_NAME_SKIP=1, honoured by the hook block that runs this rung.

EACH REFUSAL IS A LABELLED EXAMPLE. The hits go to the classify metric
journal as `private` positives through `helm classify label`: a hash of the
line and the entry's number, never the text. The advisory below is then
scored against every leak the list has caught. The write is silent, bounded
and fail-open; the refusal never depends on it.

THE ADVISORY (`--advise`) NEVER REFUSES. Added COMMENT and DOCUMENT lines in
public-bound paths go to the local classifier stream (`helm classify`,
docs/CLASSIFY.md), at most ADVISORY_LINES of them inside one total time
budget. It asks the INTERNAL-NAMES question: does the line name one of the
owner's own machines, internal projects or people? Public products,
companies, vendors, AI models, open-source projects, tools and version
numbers are not private, and the task lists PUBLIC_NAMES (plus the host's own
`public-names` local-names key) as known public names. The earlier question,
"does it name anything outside that list?", read every public name the list
does not carry as private: it flagged 8 of 15 such lines to catch 9 of 36
private ones, where this question flags 0 of 15 and catches 6 of 36. The list
owns recall; the advisory is worth running only while its flags are right.
Lines are RANKED by the probability of the `private` label, never by the
label the stream returns, and every line at or above THRESHOLD is printed,
highest first, with a pointer to this list. The classifier only flags; the list decides. It is
silent when the stream is unconfigured, unreachable or slow, when `helm` is
not on PATH, and when HELM_PRIVATE_NAME_ADVISORY=0.

Stdlib-only. The hook runs an installed snapshot of this file, so a
committing lane cannot edit its own judge; the classify calls go through the
`helm` on PATH because a snapshot has no helm package to import.
"""
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tokenize

TAG = "[helm private-name]"

#: The private names, upper-case hex of their UTF-8 bytes (see the docstring).
NAMES_HEX = ("73657368", "6D63", "656D6261726B", "6275696C646572732D646576",
             "736E6F6F7079", "64726F6F7079", "686F6D656C6162", "6275696C6472",
             "706C61796170616C", "72616D7370616365", "7468696E6B706164",
             "7075672D70313473")
NAMES = tuple(bytes.fromhex(h).decode("utf-8") for h in NAMES_HEX)

#: The public names the advisory's question lists as known public names:
#: helm's own public projects and the public tools and model families it
#: works beside. A host adds its own through the `public-names` key of its
#: local-names file (helm/localnames.py); nothing here is removed by it.
PUBLIC_NAMES = ("helm", "dregg", "cv", "orca", "herdr", "akapug", "Claude",
                "Codex", "Kimi", "Qwen", "GitHub", "git", "Python", "vLLM",
                "llama.cpp")

#: The label set the advisory sends, by the name `helm classify --labels
#: @private-name` resolves it under.
LABEL_SET = "private-name"
#: The consumer names the metric journal counts this rung under.
CONSUMER = "private-name"
ADVISORY_CONSUMER = "private-name-advisory"
#: At or above this probability of `private` the advisory prints a line. The
#: stream's own advice: below it, defer to the deterministic rule.
THRESHOLD = 0.75
#: The advisory's defaults: how many lines it asks about, and the classify
#: time all of them share.
ADVISORY_LINES = 12
ADVISORY_MS = 2500
#: How long `helm` may take to start on top of that budget, and how long the
#: positive-example write may take.
SPAWN_GRACE_S = 1.5
LABEL_TIMEOUT_S = 3.0
#: One line sent to the stream is at most this many characters; a longer
#: line is not cut, it is not asked about.
MAX_LINE = 400

#: changes/ holds the change notes the release folds into CHANGELOG.md
#: (changes/README.md), so they ship exactly as CHANGELOG.md does.
PUBLIC_DIRS = ("helm/", "docs/", "tests/", "agents/", "bin/", "scripts/",
               "changes/")
_TOP_DOC_PREFIXES = ("README", "LICENSE", "CHANGELOG", "CONTRIBUTING",
                     "SECURITY", "AGENTS")
_DOC_EXT = (".md", ".rst", ".txt")
_COMMENT_MARKS = ("#", "//", "/*", "*", "<!--")

_WORD = re.compile(r"[A-Za-z0-9]+")
_LETTERS = re.compile(r"[A-Za-z]{3}")
_OPEN_QUOTE = re.compile(r"\A[rRbBuUfF]{0,2}(\"\"\"|'''|\"|')")


def hits_in(text, names=None):
    """[(line number, name)] for each private name in `text`. `names`
    defaults to NAMES read at call time, so every reader sees one list."""
    names = NAMES if names is None else names
    single = {n for n in names if "-" not in n}
    double = {tuple(n.split("-", 1)) for n in names if "-" in n}
    out = []
    for number, line in enumerate(text.splitlines(), 1):
        words = [(m.group(0).lower(), m.start(), m.end())
                 for m in _WORD.finditer(line)]
        for i, (word, _start, end) in enumerate(words):
            if word in single:
                out.append((number, word))
            if i + 1 < len(words):
                nxt, nstart, _nend = words[i + 1]
                if nstart == end + 1 and line[end] in "-._" \
                        and (word, nxt) in double:
                    out.append((number, "%s-%s" % (word, nxt)))
    return out


def entry(name, names=None):
    """The list entry number (1-based) a matched name came from."""
    names = NAMES if names is None else names
    return names.index(name) + 1


def line_hash(text):
    """The only trace of a line the metric journal keeps."""
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]


def public_bound(rel):
    """True for a path this repository publishes as source or documentation."""
    if rel.startswith(PUBLIC_DIRS):
        return True
    if "/" in rel:
        return False
    return (rel.upper().startswith(_TOP_DOC_PREFIXES)
            or rel.lower().endswith(_DOC_EXT))


def advisory_labels(public=PUBLIC_NAMES):
    """(labels, task) for the internal-names question the advisory asks.
    `public` is named in the task as known public names; a public name it
    does not carry is still not private."""
    listed = ", ".join(public)
    labels = {
        "private": "names one of the owner's OWN internal things: a machine "
                   "or host of theirs, an internal project, repository or "
                   "lane, or a person on their team. Public products, "
                   "companies, vendors, AI models, open-source projects, "
                   "tools and version numbers are NOT private.",
        "clean": "names only public things (products, companies, vendors, AI "
                 "models, open-source projects, tools, version numbers) or "
                 "no names at all",
    }
    task = ("Decide whether this line from a source comment or a document "
            "names one of the owner's own internal machines, projects or "
            "people. Known public names include: %s." % listed)
    return labels, task


def labelled_set(names=None):
    """[(text, is_private)]: the advisory's measuring set, built at run time
    so no file carries a private name in the clear. Every listed name in
    three comment shapes, neutral lines of the same register, and lines that
    name public things PUBLIC_NAMES does not carry."""
    names = NAMES if names is None else names
    shapes = ("the {name} box keeps the only copy of that log",
              "moved here from the {name} lane so both callers share it",
              "see {title}'s notes before changing the retry window")
    rows = [(s.format(name=n, title=n.title()), True)
            for n in names for s in shapes]
    neutral = (
        "keep the lock until the append lands, then release it",
        "a missing file is an empty ledger, never an error",
        "the retry window doubles after each refused write",
        "this reads the index, not the worktree, so an edit cannot hide it",
        "the caller owns the timeout; this function never sleeps",
        "sorted so two runs over the same input print the same lines",
        "a blank line ends the paragraph and starts a new group",
        "one row per seat, keyed by its session rather than its name",
        "the budget is shared by every line, so a slow answer spends it",
        "an unreadable config is reported, never read as absent",
        "helm and dregg agree on the frame; cv only reads it",
        "Claude and Codex seats both take this path through orca",
        "the vLLM server behind the stream answers in about a tenth of a second",
        "GitHub rejects the push when the branch is protected",
        "git writes the marker at column zero, so indentation disarms it",
        "Python reads the docstring before the first statement runs",
        "the ledger is append-only; a correction is a new row",
        "a hash of the line is kept, never the line itself",
    )
    # public names outside the allowlist: the class the allowlist question
    # flagged as private, so a question that does it again scores fp here
    public = (
        "server gemini had been answering on since the proxy fix",
        "the fork 7.2.110-helm.14 carries the refusal-origin patch",
        "CLIProxyAPI translates the Anthropic body to OpenAI chat",
        "the polyglot MCP server exposes pa_scan and pa_trace",
        "OpenRouter's free tier returned an empty thinking block",
        "systemd restarts the unit when Docker exits non-zero",
        "xgrammar refuses a regex lookahead in a tool schema",
        "Intel's llm-scaler image ships vLLM 0.26 for the B70",
        "the DeepSeek v4 flash model answers in about two seconds",
        "Anthropic's API rejects an empty thinking block",
        "Ubuntu 26.04 ships a newer xe driver",
        "a Hugging Face mirror of the GGUF saved the download",
        "the Gemma and Mistral runs used the same prompt",
        "pnpm install --frozen-lockfile refuses a drifted lockfile",
        "Cloudflare Workers cold-start in under five ms",
    )
    return rows + [(t, False) for t in neutral + public]


# ------------------------------------------------------------ staged reads

def _sibling(name):
    """An installed snapshot neighbour, or None on a broken installation --
    the dual-mode loader the other rungs use to reach each other's seams."""
    if __package__:
        try:
            return __import__("helm." + name, fromlist=[name])
        except Exception:                                      # noqa: BLE001
            return None
    here = os.path.dirname(os.path.abspath(__file__))
    if not os.path.exists(os.path.join(here, name + ".py")):
        return None
    try:
        mod = __import__(name)
    except Exception:                                          # noqa: BLE001
        return None
    got = os.path.dirname(os.path.abspath(getattr(mod, "__file__", "") or ""))
    return mod if got == here else None


def _path(raw):
    return os.fsdecode(raw).replace(os.sep, "/")


def _lines(text):
    """The file's lines as git numbers them: split at LF alone, so a form
    feed or a lone CR inside a line cannot shift every number after it."""
    lines = [line.rstrip("\r") for line in text.split("\n")]
    return lines[:-1] if lines and lines[-1] == "" else lines


def _entered_boundary(status, old, rel):
    """A rename or copy INTO a public-bound path adds every line it carries."""
    if not status or status[0] not in ("R", "C") or old is None:
        return False
    return public_bound(rel) and not public_bound(_path(old))


def staged_added(root, cm):
    """[(rel, text, {line number: line})] for every public-bound staged text
    file: its staged text and the lines this commit adds. Raises RuntimeError
    when the staged set cannot be read; the caller refuses on it."""
    out = []
    entries = cm.staged_entries(root)
    blobs = cm._index_blobs(root)
    for status, old, raw in entries:
        rel = _path(raw)
        if not public_bound(rel):
            continue
        mode, oid = blobs.get(raw, (None, None))
        if mode == b"160000":
            continue                  # a gitlink names a commit, not lines
        if oid is None:
            raise RuntimeError("no stage-0 blob for %s" % rel)
        rc, blob, _err = cm._git(root, "cat-file", "blob", oid)
        if rc != 0:
            raise RuntimeError("staged blob unreadable for %s" % rel)
        if b"\0" in blob[:8192]:
            continue
        text = blob.decode("utf-8", "replace")
        ranges = None if _entered_boundary(status, old, rel) else \
            cm._added_ranges(root, status, old, raw)
        lines = _lines(text)
        added = {n: lines[n - 1] for n in range(1, len(lines) + 1)
                 if ranges is None or any(lo <= n <= hi for lo, hi in ranges)}
        if added:
            out.append((rel, text, added))
    return out


def violations(files, names=None):
    """[(rel, line, entry number, text)] for every added line with a hit,
    one row per list entry a line names."""
    names = NAMES if names is None else names
    out = []
    for rel, _text, added in files:
        for n in sorted(added):
            numbers = sorted({entry(name, names)
                              for _one, name in hits_in(added[n], names)})
            out.extend((rel, n, number, added[n]) for number in numbers)
    return out


def scan_staged(root, names=None):
    """(violations, error, missing_seam) for the staged set of `root`."""
    cm = _sibling("conflict_marker")
    if cm is None:
        return None, None, "conflict_marker"
    try:
        files = staged_added(root, cm)
    except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
        return None, str(exc), None
    return violations(files, names), None, None


# ------------------------------------------------------------ prose lines

def _python_prose(text):
    """{line number: prose} for the comments and bare-string statements
    (docstrings) of one Python source; comment lines alone when it does not
    tokenize."""
    out = {}
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(text).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        for n, line in enumerate(_lines(text), 1):
            if line.strip().startswith("#"):
                out[n] = line.strip().lstrip("#").strip()
        return out
    quiet = (tokenize.COMMENT, tokenize.NL)
    prev = tokenize.NEWLINE
    for i, tok in enumerate(toks):
        if tok.type == tokenize.COMMENT:
            out[tok.start[0]] = tok.string.lstrip("#").strip()
        elif tok.type == tokenize.STRING and prev in (
                tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT):
            nxt = next((t for t in toks[i + 1:] if t.type not in quiet), None)
            if nxt is not None and nxt.type in (tokenize.NEWLINE,
                                                tokenize.ENDMARKER):
                for k, piece in enumerate(tok.string.splitlines()):
                    if k == 0:
                        piece = _OPEN_QUOTE.sub("", piece.strip())
                    out[tok.start[0] + k] = piece.strip().rstrip("\"'").strip()
        if tok.type not in quiet:
            prev = tok.type
    return out


def prose_lines(rel, text):
    """{line number: prose} for the comment and document lines of one file."""
    lower = rel.lower()
    if lower.endswith(_DOC_EXT) or ("/" not in rel and rel.upper().startswith(
            _TOP_DOC_PREFIXES)):
        return {n: line.strip() for n, line in enumerate(_lines(text), 1)
                if line.strip()}
    if lower.endswith(".py"):
        return _python_prose(text)
    out = {}
    for n, line in enumerate(_lines(text), 1):
        stripped = line.strip()
        mark = next((m for m in _COMMENT_MARKS if stripped.startswith(m)), None)
        if mark:
            out[n] = stripped[len(mark):].strip()
    return out


def candidates(root, cm, names=None):
    """([(rel, line, prose)], total): the added comment and document lines
    worth asking about, in diff order, and how many there were."""
    names = NAMES if names is None else names
    found = []
    for rel, text, added in staged_added(root, cm):
        # the whole staged file, because only the whole file says which
        # lines are inside a docstring
        prose = prose_lines(rel, text)
        for n in sorted(added):
            said = " ".join((prose.get(n) or "").split())
            if (len(said) < 12 or len(said) > MAX_LINE
                    or not _LETTERS.search(said) or hits_in(said, names)):
                continue
            found.append((rel, n, said))
    return found, len(found)


# ------------------------------------------------------------ the classifier

def _env_int(name, default, low, high):
    try:
        value = int(os.environ.get(name, ""))
    except ValueError:
        return default
    return min(max(value, low), high)


def _helm():
    return shutil.which("helm")


def ask(texts, budget_ms):
    """[row] from `helm classify --each-line` over `texts`, or None when the
    helm on PATH could not be run inside the budget. Each row carries the
    1-based `line` of the text it answers."""
    exe = _helm()
    if not exe or not texts:
        return None
    argv = (exe, "classify", "--each-line", "--labels", "@" + LABEL_SET,
            "--source", "lane-diff", "--consumer", ADVISORY_CONSUMER,
            "--budget-ms", str(budget_ms), "--json")
    try:
        done = subprocess.run(argv, input="\n".join(texts) + "\n",
                              capture_output=True, text=True,
                              timeout=budget_ms / 1000.0 + SPAWN_GRACE_S)
    except (OSError, subprocess.SubprocessError):
        return None
    rows = []
    for raw in done.stdout.splitlines():
        try:
            row = json.loads(raw)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def rank(found, rows, threshold=THRESHOLD):
    """[(score, (rel, line, prose))] at or above `threshold`, highest first.
    The score is the `private` label's own probability: a ranking reorders
    the lines and hides none of those that cross the threshold."""
    scored = []
    for row in rows or ():
        n, scores = row.get("line"), row.get("scores")
        if row.get("outcome") != "ok" or not isinstance(scores, dict):
            continue
        p = scores.get("private")
        if not isinstance(n, int) or not 1 <= n <= len(found) \
                or not isinstance(p, (int, float)) or p < threshold:
            continue
        scored.append((float(p), found[n - 1]))
    return sorted(scored, key=lambda s: (-s[0], s[1][0], s[1][1]))


def journal_positives(hits):
    """Count each refused line as a labelled `private` example. Silent,
    bounded and fail-open: the refusal never depends on it."""
    exe = _helm()
    if not exe or not hits:
        return False
    rows = "".join("%s %d\n" % (line_hash(text), number)
                   for _rel, _n, number, text in hits)
    try:
        subprocess.run((exe, "classify", "label", "--consumer", CONSUMER,
                        "--label", "private"), input=rows,
                       capture_output=True, text=True, timeout=LABEL_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return False
    return True


# ------------------------------------------------------------ entry points

def _root():
    cm = _sibling("conflict_marker")
    if cm is None:
        return None, None
    rc, out, _err = cm._git(os.getcwd(), "rev-parse", "--show-toplevel")
    if rc != 0 or not out.strip():
        return cm, None
    return cm, os.fsdecode(out.strip())


def refuse(root, err=sys.stderr, names=None):
    """The `--staged` door: 0 clean, 1 refused, 2 when the scan cannot run."""
    found, error, missing = scan_staged(root, names)
    if missing:
        err.write("%s WARNING: hardened scanner seam missing: %s — staged "
                  "private-name scan SKIPPED; reinstall the guard\n"
                  % (TAG, missing))
        return 0
    if error:
        err.write("%s REFUSED: staged private-name scan is UNKNOWN — %s\n"
                  % (TAG, error))
        return 2
    if not found:
        return 0
    err.write("%s REFUSED: %d added line(s) in a public-bound path name one of "
              "the operator's private projects, hosts or people:\n"
              % (TAG, len(found)))
    for rel, n, number, _text in found:
        err.write("%s   %s:%d — private-names list entry %d\n"
                  % (TAG, rel, n, number))
    err.write("%s say what the name stood for without it, or read it from "
              "this host's local names (helm/localnames.py). The name is not "
              "printed here so this refusal can be pasted anywhere. One-commit "
              "owner override: HELM_PRIVATE_NAME_SKIP=1\n" % TAG)
    journal_positives(found)
    return 1


def advise(root, err=sys.stderr, names=None):
    """The `--advise` door: warnings only, always 0."""
    if os.environ.get("HELM_PRIVATE_NAME_ADVISORY", "").strip().lower() in (
            "0", "off", "no", "false"):
        return 0
    cm = _sibling("conflict_marker")
    if cm is None:
        return 0
    try:
        found, total = candidates(root, cm, names)
    except (RuntimeError, OSError, subprocess.SubprocessError):
        return 0
    cap = _env_int("HELM_PRIVATE_NAME_ADVISORY_LINES", ADVISORY_LINES, 1, 64)
    budget = _env_int("HELM_PRIVATE_NAME_ADVISORY_MS", ADVISORY_MS, 100, 30000)
    asked = found[:cap]
    flagged = rank(asked, ask([p for _r, _n, p in asked], budget))
    if not flagged:
        return 0
    err.write("%s ADVISORY: the local classifier reads %d added comment or "
              "document line(s) as naming one of the owner's own machines, "
              "projects or people (it flags; the private-names list "
              "decides):\n" % (TAG, len(flagged)))
    for score, (rel, n, prose) in flagged:
        err.write("%s   %.2f  %s:%d  %s\n" % (TAG, score, rel, n, prose))
    if total > len(asked):
        err.write("%s   (asked about %d of %d candidate lines)\n"
                  % (TAG, len(asked), total))
    err.write("%s if a line names a private project, host or person, add the "
              "name to NAMES_HEX in helm/private_names.py "
              "(\"<name>\".encode().hex().upper()) so the rung refuses it. "
              "Off switch: HELM_PRIVATE_NAME_ADVISORY=0\n" % TAG)
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv not in (["--staged"], ["--advise"]):
        sys.stderr.write("usage: private_names.py --staged|--advise  (run by "
                         "the helm pre-commit guard; cwd inside the repo)\n")
        return 2
    cm, root = _root()
    if cm is None:
        if argv == ["--staged"]:
            sys.stderr.write("%s WARNING: hardened scanner seam missing: "
                             "conflict_marker — staged private-name scan "
                             "SKIPPED; reinstall the guard\n" % TAG)
        return 0
    if root is None:
        if argv == ["--advise"]:
            return 0
        sys.stderr.write("%s REFUSED: not inside a git work tree\n" % TAG)
        return 2
    return refuse(root) if argv == ["--staged"] else advise(root)


if __name__ == "__main__":
    sys.exit(main())
