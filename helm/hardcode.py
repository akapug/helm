#!/usr/bin/env python3
"""The hardcode rung — a staged-diff WARNER for context-identity baked into
portable logic.

OWNER CANON `hardcode-is-eventual-failure` (2026-07-31, verbatim: "if you ever
see a hardcode, it's generally bad. i wonder if we need a whisper or hook
triggered by the word hardcode."). The trigger is right, the handle is wrong:
the WORD "hardcode" gets typed when the lesson is already learned — the defect
ships silently as a reasonable-looking line. So this scans the SHAPE in the
staged diff, never the word in prose (`keyword-retrieval-is-for-the-long-tail-
not-measurable-conditions`: the condition here is measurable, so it gets a
measurable instrument).

A HARDCODE, per the canon: context-identity baked into portable logic — host,
path, user, id, tenant, branch, hostname, session id, port, seat name. The
failure mode is the point: a hardcode CANNOT ADAPT. The canonical example (the
fixture for the must-hit control) is cv's

    let root = dirs::home_dir().map(|h| h.join(".claude").join("projects"));

with a singular `root: Option<PathBuf>` — not a string literal of a path, but a
CONSTRUCTED path with no override and no plurality. Measured cost: 23 fleet
seat transcripts invisible to cv while 647 default-home ones indexed fine. A
rung that only greps for "/home/" misses that shape entirely, so this rung
fires on CONSTRUCTION shapes (home_dir()/expanduser/join of a hidden dir,
os.environ of an identity-ish name without a documented default) as well as
literal ones (absolute home paths, machine ports, seat names in helm/ logic).

SIBLING, NOT NEEDLE. never-track is ENUMERATED on purpose ("a pattern would
invite argument about whether some new file counts") and this is a pattern by
nature — different epistemics, different rung. A pattern MISS here must never
be read as an enumerated never-track miss, and never-track's enumeration is
untouched.

WARN, NEVER BLOCK. A pattern rung that refuses commits fires on every
legitimate fixture path in tests/ and is switched off within a day — this repo
has that scar (the built-but-not-wired latch in seats.py), and the in-repo
precedent for the right shape is helm/shaguard.py, which warns on unresolvable
shas and never blocks. tests/ is exempt by default (fixtures legitimately
plant identity-shaped strings), overridable with HELM_HARDCODE_SCAN_TESTS=1.

THE MUST-HIT CONTROL. Every scan seeds the cv construction shape. A rung
reporting zero across this repo is a broken probe, not a clean tree — the
control proves it can see before anyone trusts it not seeing.

Stdlib-only, no helm imports: the hook executes this file as
`python3 <this file> --staged` with cwd at the committing work tree's top,
same as nevertrack.
"""
import os
import re
import subprocess
import sys

# ---------------------------------------------------------------------------
# THE SHAPES. Each names its class and its why; a rule whose reason is lost
# gets deleted by the next person tidying up.
# ---------------------------------------------------------------------------

# LITERAL identity shapes: bytes that name THIS machine (or this fleet) in
# logic that ships. Paths and ports only; seat names are checked separately
# because they are short and collision-prone.
_LITERAL = (
    (re.compile(rb"/home/[A-Za-z0-9._-]+/"), "home-path",
     "an absolute home path baked into logic that ships"),
    (re.compile(rb"/Users/[A-Za-z0-9._-]+/"), "home-path",
     "an absolute macOS home path baked into logic that ships"),
    (re.compile(rb"\b(?:127\.0\.0\.1|localhost|0\.0\.0\.0):([0-9]{4,5})\b"),
     "host-port",
     "a loopback host:port baked into logic that ships (ports are per-fleet, "
     "not universal)"),
)

# CONSTRUCTION shapes: identity assembled at runtime with no override. The cv
# lesson: dirs::home_dir() / expanduser joined into a HIDDEN config dir, with
# no fallback — portable-looking code that silently binds one layout. Each of
# these is a TWO-PART match (home-source AND hidden-dir-join in the same added
# hunk), because either half alone is ubiquitous and innocent.
_HOME_SOURCES = (
    rb"dirs::home_dir",
    rb"os\.path\.expanduser",
    rb"Path\.home\(\)",
    rb"HOME\b",
)
_HIDDEN_JOIN = rb"\.(?:claude|codex|config|helm|config/)[A-Za-z0-9._/-]*"

_CONSTRUCTION_HOME = re.compile(
    rb"(?:" + b"|".join(_HOME_SOURCES) + rb")")
_CONSTRUCTION_JOIN = re.compile(
    rb"join(?:path)?\(['\"]" + _HIDDEN_JOIN +
    rb"|os\.path\.join\(.{0,120}?['\"]" + _HIDDEN_JOIN)

# OVERRIDE: the home-source is a DEFAULT, not a binding. The cv defect is a
# home-dir joined into a hidden config dir WITH NO ESCAPE — a singular root
# the caller cannot redirect. When the same hunk shows the home-source as the
# right operand of an `or` whose left is a parameter, an env read, or a
# config value (x = x or expanduser("~") / home = env("HELM_HOME") or
# Path.home()), the shape is OVERRIDABLE — configuration, not the defect.
# codex-3's round-5 bar: both real warnings tonight (wiring's injected
# home_dir, beacons' HELM_HOME/MELD_HOME env) were overridable, so a warning
# that asserts "no override" without measuring one is a false factual claim.
# The override must be MEASURED, and a measured override silences the arm.
# OVERRIDE must be RELATIONAL, not existential (codex-3's round-6): an
# override ANYWHERE in the hunk must not silence a hardcode it has no
# relation to — `_hits(b'ignored = env.get("HELM_HOME")\nroot =
# Path.home().join(".claude")')` must still FIRE because the env read is on
# a DIFFERENT variable than the join base. So the analysis associates each
# hidden-join with the variable it hangs off and asks of THAT definition:
# is the home-source there overridable? A definition `VAR = RHS` makes its
# home-source overridable when (a) the home-source is the RIGHT of an `or`
# whose left is a parameter/env/config read (a fallback default), or (b) the
# RHS is itself an env/config read (the home is a configuration value a bare
# expanduser/HOME later only EXPANDS). Measured relation, not existence.
_ASSIGN = re.compile(
    rb"(?:^|\n)[ \t]*(?:let\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*(?::[^=\n]*)?="
    rb"\s*([^=\n][^\n]*)")
_ENV_READ = re.compile(
    rb"(?:os\.environ\s*[\(\[]|getenv\s*[\(\[]"
    rb"|[A-Za-z_][A-Za-z0-9_.]*\.(?:env\.get|get)\s*[\(\[]"
    rb"|[A-Za-z_][A-Za-z0-9_.]*\.env\s*[\(\[])")

# ENV-IDENTITY: reading an environment variable whose name is an identity
# (user, host, tenant, session) rather than a configuration. Whitelisted
# config-ish names are not flagged; the class is the name's SEMANTICS.
_ENV_IDENTITY = re.compile(
    rb'os\.environ(?:\.get)?[\(\[]["\x27]([A-Z_]*(?:USERNAME|LOGNAME|TENANT|'
    rb'ACCOUNT|SESSION_ID)[A-Z_]*)["\x27]')

# Identity is about WHO/WHERE the code runs as, not what it connects to.
# DATABASE_HOST, API_HOST, REDIS_HOST etc. are configured endpoints, not
# baked-in identity; USER and HOST alone are too generic to flag without a
# qualifier. The whitelist is the class, not the word.

# SEAT NAMES in helm/ logic. The fleet's seats are instance data; logic that
# names one cannot run on a second fleet. Kept to exact strings to avoid
# collisions with ordinary words ("gemini" is a constellation, "kimi" a name).
_SEAT_LITERAL = re.compile(
    rb'["\x27](?:ds4pro|opus-integrator|helm-claude-2|codex-[23])["\x27]')

# The MUST-HIT CONTROL: the cv construction shape, planted in the scan of
# every run. A rung that cannot see its own canonical example must say so
# rather than report a clean tree.
_MUST_HIT = b'let root = dirs::home_dir().map(|h| h.join(".claude").join("projects"));'

_EXEMPT_DIRS = ("tests/",)
_SCAN_TESTS = os.environ.get("HELM_HARDCODE_SCAN_TESTS") == "1"


def _git(root, *args, binary=False):
    p = subprocess.run(("git",) + args, capture_output=True, cwd=root,
                       timeout=60, text=not binary)
    return p.returncode, p.stdout, p.stderr


def _staged_paths(root):
    rc, out, err = _git(root, "diff", "--cached", "--name-only", "-z",
                        "--diff-filter=ACMRT")
    if rc != 0:
        raise RuntimeError("git diff --cached failed: %s" % err.strip())
    return [f for f in out.split("\0") if f]


def _parse_diff_blocks(diff_bytes):
    """ONE raw-diff -> added-blocks parser, shared by live git extraction AND
    the MUST_HIT control. Returns a list of byte-strings, one per `@@` hunk's
    added lines; returns None for a binary diff (the caller substitutes the
    whole blob — over-warn rather than under-see).

    Hunk grouping is the unit the construction shape is defined over: the cv
    defect is a home-source and a hidden-dir join in ONE statement, so two
    unrelated lines that merely share a staged change must never co-fire.
    Factoring the parser is codex-3's blocker-3 requirement — the control
    feeds a SYNTHETIC diff through this same code, so a parser that drops
    every block reads as control UNKNOWN, never silently green."""
    blocks, current, in_hunk = [], [], False
    for line in diff_bytes.split(b"\n"):
        if line.startswith(b"@@"):
            if current:
                blocks.append(b"\n".join(current))
                current = []
            in_hunk = True
            continue
        if not in_hunk:
            if line.startswith(b"Binary files ") or line.startswith(b"GIT binary patch"):
                return None
            continue
        if line[:1] == b"+":
            current.append(line[1:])
        elif line[:1] not in (b"-", b" ", b"\\"):
            if current:
                blocks.append(b"\n".join(current))
                current = []
            in_hunk = False
    if current:
        blocks.append(b"\n".join(current))
    return blocks


def _added_blocks(root, rel):
    """The '+' side of a zero-context staged diff, GROUPED BY HUNK, via the
    shared parser. Returns None for binary (the caller substitutes the blob)."""
    rc, out, err = _git(root, "diff", "--cached", "-U0", "--no-color",
                        "--no-ext-diff", "--", rel, binary=True)
    if rc != 0:
        raise RuntimeError("git diff --cached failed for %s" % err)
    return _parse_diff_blocks(out)


def _is_prose_line(line):
    """A line that is documentation, not code: a comment or a line whose
    content is a quoted narrative. Retained for the single-line fast path;
    the real prose classification is `_code_only`, which tracks spans."""
    s = line.strip()
    if not s:
        return False
    if s.startswith(("#", "//")):
        return True
    if s[:1] in ("'", '"') and s[-1:] in ("'", '"', ','):
        return True                    # a quoted string on its own line
    return False


# String literals that are DOCUMENTATION, not values: the prose arms. A
# help=/description=/doc=/usage= kwarg's string is human-facing text — an
# example URL inside it is documentation, not a route. Bare-narrative lines
# (a quoted string on its own line) are likewise prose.
_PROSE_KWARG = re.compile(
    rb"(?:help|description|doc|usage|epilog|title|caption|note)\s*=\s*$")


def _code_only(payload):
    """The payload with PROSE masked to spaces; hardcode-shaped VALUES kept.

    The semantic line codex-3's fixtures draw: a hardcode is identity baked
    into a VALUE the program routes through (an assigned URL, a path, a seat
    name), and an EXAMPLE of one in documentation is not. So:

      * comments (#, //)            -> always prose, masked
      * triple-quoted strings       -> always prose (docstrings), masked,
                                       TRACKED ACROSS LINE BREAKS so an
                                       interior line with no quote prefix is
                                       still masked (codex-3's repro 2)
      * single-line string literals -> prose ONLY in documentation context:
                                       a help=/description=/... kwarg value
                                       (codex-3's repro 1) or a bare narrative
                                       line. An ASSIGNED / returned / compared
                                       string is a VALUE and keeps its content,
                                       because url = "localhost:8318" IS the
                                       hardcode even though it is quoted.

    WHY SPANS, NOT LINES: prose has an open and a close independent of where
    lines break; a stateless per-line filter misses both the inline help
    string (prose inside a code line) and the docstring interior (prose on a
    line with no quote). Positions and newlines are preserved so a match's
    location still maps to source. An unmatched quote masks to end-of-payload
    (over-mask a fragment's prose, never under-see a docstring).
    """
    out = bytearray()
    i, n = 0, len(payload)
    # byte offset of the start of the current logical line in `out`, so a
    # string can look BACK at the code that precedes it on its own line
    # (to detect a help= kwarg) and a bare-narrative line can be detected.
    while i < n:
        c = payload[i:i+1]
        if c == b"#":
            while i < n and payload[i:i+1] != b"\n":
                out += b" "
                i += 1
            continue
        if c == b"/" and payload[i:i+2] == b"//":
            while i < n and payload[i:i+1] != b"\n":
                out += b" "
                i += 1
            continue
        triple = payload[i:i+3]
        if triple in (b'"""', b"'''"):
            out += b"   "
            i += 3
            while i < n:
                if payload[i:i+1] == b"\\":
                    out += b"  "
                    i += 2
                    continue
                if payload[i:i+3] == triple:
                    out += b"   "
                    i += 3
                    break
                out += (b"\n" if payload[i:i+1] == b"\n" else b" ")
                i += 1
            continue
        if c in (b'"', b"'"):
            # decide prose vs value from the code that precedes the quote
            line_start = payload.rfind(b"\n", 0, i) + 1
            before = payload[line_start:i]
            is_prose = bool(_PROSE_KWARG.search(before))
            close = c
            # scan the literal's extent
            j = i + 1
            closed = False
            while j < n:
                if payload[j:j+1] == b"\\":
                    j += 2
                    continue
                if payload[j:j+1] == b"\n":
                    break
                if payload[j:j+1] == close:
                    closed = True
                    break
                j += 1
            if is_prose:
                out += b" " * (j - i)          # mask quotes + content
            else:
                # a VALUE: keep the literal verbatim, quotes included — the
                # shapes expect the quote glyphs (["\x27]) and match the inner
                # bytes; an assigned/returned string can be a real hardcode.
                out += payload[i:j + (1 if closed else 0)]
            i = j + (1 if closed else 0)
            continue
        out += c
        i += 1
    return bytes(out)


def _var_overridable(rhs):
    """Does this assignment RHS make its variable's home-source overridable?
    (a) the home-source is the RIGHT of an `or` whose left is NOT itself a
    bare home-source (a fallback default: `x = x or expanduser("~")`,
    `home = env.get("HELM_HOME") or Path.home()`); or (b) the RHS is itself
    an env/config read with NO home-source (the home is a configuration
    value a later expanduser/HOME only expands)."""
    has_home = bool(_CONSTRUCTION_HOME.search(rhs))
    has_env = bool(_ENV_READ.search(rhs))
    if has_env:
        # an env/config read makes the home a configuration value, even when
        # the env var is itself named HOME (env.get("HOME") reads a home the
        # caller controls; the HOME token there is the config key, not a
        # bare home-source binding)
        return True                          # (b) env-derived home
    if has_home and re.search(rb"\bor\b", rhs):
        # a fallback: something other than a lone home-source is the default
        left = re.split(rb"\bor\b", rhs, maxsplit=1)[0]
        if left.strip() and not (
                _CONSTRUCTION_HOME.fullmatch(left.strip()) or
                left.strip() in (b"HOME",)):
            return True                      # (a) `or` fallback default
    return False


def _constructed_home_fires(body):
    """The cv shape, RELATIONALLY: a hidden-join whose base traces to a
    home-source with NO override. An override unrelated to the join's base
    must not silence it (codex-3's round-6).

    Build the hunk's assignment map, then for each hidden-join find its base:
    an INLINE home-source (join(expanduser("~"), ".claude") /
    Path.home().join(".claude")) fires iff that expression carries no
    override; a VARIABLE base fires iff its definition's home-source is not
    overridable. A base we cannot resolve to a home-source does not fire
    (UNKNOWN stays silent — never over-warn)."""
    if not (_CONSTRUCTION_HOME.search(body) and _CONSTRUCTION_JOIN.search(body)):
        return False
    defs = {}
    for m in _ASSIGN.finditer(body):
        defs[m.group(1)] = m.group(2)
    for line in body.split(b"\n"):
        if not _CONSTRUCTION_JOIN.search(line):
            continue
        # the join's base: the expression before the join call
        # inline home-source joined directly (no intermediate variable)
        if _CONSTRUCTION_HOME.search(line):
            # inline construction: fire unless the SAME line overrides it
            if not _var_overridable(line):
                return True
            continue
        # variable base: resolve it through the assignment map
        base_m = re.search(rb"join\(\s*([A-Za-z_][A-Za-z0-9_]*)", line)
        if base_m:
            var = base_m.group(1)
            rhs = defs.get(var)
            if rhs and _CONSTRUCTION_HOME.search(rhs) \
                    and not _var_overridable(rhs):
                return True
    return False


def _hits(payload, rel=""):
    """Every (class, why, sample-line) the shapes find in ONE added block.

    `payload` is a single hunk's added lines (from `_added_blocks`) or a bare
    code blob (a direct probe, the control constant). The per-hunk grouping is
    done by the CALLER — this function never re-derives boundaries, because the
    markers it would need are stripped during extraction.

    PROSE IS CLASSIFIED BEFORE EVERY MATCHER, not just the construction arm:
    a docstring quoting a home path, a CLI-help string naming localhost:8318,
    or a comment `target=ds4pro` is documentation of a shape, not the shape
    (codex-3's blocker 2 — prose filtering applied only to constructed-home
    let the literal and seat arms keep warning on prose). `rel` scopes the
    file class: docs (.md/.rst/.txt) are prose by construction."""
    if rel.endswith((".md", ".rst", ".txt")):
        return []
    # mask prose SPANS (strings + comments) ONCE, before any matcher runs —
    # an example inside a docstring, help string, or comment is not a
    # hardcode in any class, and prose is a lexical span, not a line shape
    body = _code_only(payload)
    out = []
    for rx, cls, why in _LITERAL:
        m = rx.search(body)
        if m:
            out.append((cls, why, m.group(0)[:80]))
    if _constructed_home_fires(body):
        out.append(("constructed-home", "a home-dir joined into a hidden "
                    "config dir with no override — the cv shape: portable-"
                    "looking code that binds one layout", b""))
    m = _ENV_IDENTITY.search(body)
    if m:
        out.append(("env-identity", "an identity-named env var read as "
                    "configuration", m.group(1)))
    m = _SEAT_LITERAL.search(body)
    if m:
        out.append(("seat-name", "a fleet seat name baked into logic that "
                    "ships — instance data, not code", m.group(0)))
    return out


# A synthetic staged diff carrying the cv shape, fed through the SAME parser
# as live git output. The control never touches the index: it proves the
# raw-diff -> added-blocks parser can see the shape it must fire on.
_CONTROL_DIFF = (b"diff --git a/helm/x.py b/helm/x.py\n"
                 b"--- a/helm/x.py\n"
                 b"+++ b/helm/x.py\n"
                 b"@@ -0,0 +1 @@\n"
                 b"+" + _MUST_HIT + b"\n")


def _control_ok(root):
    """The MUST-HIT control, through the REAL extraction parser.

    Feeds a synthetic valid diff carrying the cv shape through the one shared
    `_parse_diff_blocks` parser that live git extraction uses, then confirms
    the matcher fires on the resulting block. A control that probed an
    in-memory constant (or checked `paths is not None`) would certify a blind
    parser forever — codex-3's blocker 3. This control is one the parser can
    actually lose: break the parser to return [] and this returns False,
    never silently green."""
    try:
        blocks = _parse_diff_blocks(_CONTROL_DIFF)
    except Exception:
        return False
    if not blocks:
        return False            # the parser dropped every block — blind
    return any("constructed-home" in [c for c, _, _ in _hits(b)]
               for b in blocks)


def scan(root):
    """(hits, control_ok, scanned_paths) over the staged set."""
    paths = [p for p in _staged_paths(root)
             if _SCAN_TESTS or not p.startswith(_EXEMPT_DIRS)]
    hits = []
    for rel in paths:
        blocks = _added_blocks(root, rel)
        if blocks is None:
            rc, blob, _ = _git(root, "cat-file", "blob", ":0:" + rel, binary=True)
            blocks = [blob if rc == 0 else b""]
        for block in blocks:
            for cls, why, sample in _hits(block, rel=rel):
                hits.append((rel, cls, why, sample))
    return hits, _control_ok(root), paths


def main(args):
    root = os.getcwd()
    try:
        hits, control_ok, paths = scan(root)
    except RuntimeError as e:
        print("[helm hardcode] scan FAILED: %s — WARN-only, commit allowed; "
              "an unscanned staged set is not a known-clean one" % e,
              file=sys.stderr)
        return 0                    # warn-rung law: never block
    if not control_ok:
        print("[helm hardcode] MUST-HIT CONTROL FAILED: the rung cannot see "
              "the cv construction shape it must fire on. The zero-hit "
              "report below is a broken probe, not a clean tree.",
              file=sys.stderr)
        return 0
    for rel, cls, why, sample in hits:
        line = " [%s]" % sample.decode("utf-8", "replace") if sample else ""
        print("[helm hardcode] %s: %s — %s%s" % (rel, cls, why, line),
              file=sys.stderr)
    if hits:
        print("[helm hardcode] %d staged shape%s. WARN only — a hardcode is "
              "debt, not a crime; if any is deliberate (a fixture, a doc, a "
              "default that names itself), commit on. Otherwise route the "
              "identity through config/env/plurality."
              % (len(hits), "s"[:len(hits) != 1]), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
