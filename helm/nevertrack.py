#!/usr/bin/env python3
"""The never-track law + the staged-set scanner — ONE source of truth for
"what must never reach this repo's history".

Two consumers:
  * tests/test_never_track.py — the INDEX pin: asserts nothing ALREADY
    tracked violates the never-track set. The full incident history lives in
    that file's docstring; the data lives here so the test and the hook can
    never drift apart.
  * the composed `pre-commit` guard installed by `helm work install-guard
    --apply` (helm/work/_guard.py) — the ENFORCEMENT-TIMING leg: runs this
    module as a plain script against the STAGED set between `git add` and
    the commit becoming permanent, and refuses the commit on a hit.

WHY THE HOOK EXISTS (2026-07-29). A real operator filesystem path landed in a
new test fixture and entered history even though the FULL suite ran green
right before the commit. The suite was green because the fixture was
completely untracked when pytest ran: `git ls-files` reads the index, so it
sees a file the moment it is `git add`ed — but nothing at all before that.
The seat then ran `git add` + `git commit` back-to-back with no suite run in
between, so the one moment where the guard could have seen the leak had no
guard in it.

THE FIRST PROPOSED FIX WAS MEASURED AND DISPROVED. It unioned `git ls-files`
with `git diff --cached --name-only` inside the test, reasoning that a
staged-but-uncommitted file was invisible to ls-files alone. A scratch-repo
experiment (2026-07-29, re-run before this module was written) shows both
commands read the same index and return byte-identical answers for any
staged file — the union changes nothing. The gap was never DETECTION; it is
enforcement TIMING, and only something that runs automatically inside `git
commit` can close it. A test that runs when a human remembers to run pytest
cannot, whatever it enumerates. tests/test_never_track.py pins this premise
so the union does not get re-proposed.

This module stays stdlib-only with no helm imports: the hook executes it as
`python3 <this file> --staged` with cwd at the committing work tree's top
(git guarantees that for pre-commit), so it must work with nothing but the
file itself.
"""
import os
import re
import subprocess
import sys
import tempfile

# Enumerated, not pattern-matched: a pattern would invite argument about
# whether some new file "counts". Each entry names WHY, because a rule whose
# reason is lost gets deleted by the next person tidying up. (Moved verbatim
# from tests/test_never_track.py, which now imports it from here.) A path
# whose NAME is itself private cannot be listed here without publishing the
# name, so those live in the machine-local list instead
# (load_local_never_track); the scan reads both (never_track_set).
NEVER_TRACK = {
    "agents/claudecode/skills/fleet-usage/":
        "irreducibly coupled to a PRIVATE project's MCP surface "
        "(its usage tool). Useless to a public installer and it "
        "leaks a private project name; no public-integration ruling covers it, "
        "unlike the deliberately-kept public deps.",
    "evals/":
        "internal BUILD DOCUMENTS, not published artifacts: dated dogfood "
        "reports carrying kanban card ids, internal PRD paths under ~/.helm, "
        "internal lane and branch names, and a verbatim owner quote about his "
        "private infrastructure and training-data plans. Nothing public links them (the "
        "ARCHITECTURE mention of evals/ describes helm's ~/.helm/<project> "
        "shelves, a different thing entirely).",
    "agents/claudecode/skills/vendor-as-public/":
        "a CROSS-PROJECT process skill saturated with other private repos' "
        "names plus an internal spec provenance path pinned to a commit. A "
        "public helm installer does not need this repo's policy for vendoring "
        "into the owner's other private projects.",
    "agents/claudecode/skills/x/":
        "a cross-project skill citing internal incidents and ISSUE NUMBERS "
        "from private repos as its worked examples.",
}


def load_local_never_track():
    """Machine-local never-track path prefixes, from OUTSIDE the tree:
    $HELM_NEVER_TRACK_LOCAL, else <real home>/.helm/_global/never-track.txt,
    one prefix per line, '#' comments. -> ({prefix: reason}, path).

    WHY A SECOND LIST. NEVER_TRACK ships in the tree, so each key is
    published with it. A local-only file whose very name is personal (a
    private skill, say) cannot be guarded by naming it here: the guard would
    leak exactly what it protects. Those prefixes are listed on the machine
    that holds the files, beside the private needles and for the same
    reason, and the scan treats them exactly like the shipped ones. Not
    resolved through $HELM_HOME, like load_private_needles. An absent file is
    an empty list, not an error: the shipped entries still apply. The ignore
    half belongs on the same machine too (.git/info/exclude or
    core.excludesFile), never in the tracked .gitignore."""
    path = os.environ.get("HELM_NEVER_TRACK_LOCAL") or os.path.join(
        os.path.expanduser("~"), ".helm", "_global", "never-track.txt")
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return {}, path
    why = "listed in the machine-local never-track file %s" % path
    return {ln.strip(): why for ln in lines
            if ln.strip() and not ln.lstrip().startswith("#")}, path


def never_track_set():
    """{prefix: reason}: the shipped NEVER_TRACK plus this machine's local
    prefixes. A prefix in both keeps the shipped reason."""
    local, _path = load_local_never_track()
    return dict(local, **NEVER_TRACK)


def load_private_needles():
    """The private identifiers to keep out of the tree, from OUTSIDE the tree:
    $HELM_PRIVATE_NEEDLES, else <real home>/.helm/_global/private-needles.txt,
    one per line, '#' comments. NOT resolved through $HELM_HOME — the test
    suite sandboxes that, and these identifiers describe the MACHINE's owner,
    not a helm estate. Ships zero needles: a guard that hardcodes WHOSE data
    to look for protects one person and silently protects nobody else. An
    absent file yields an EMPTY set and callers must say so out loud — a scan
    with nothing to look for is a documented no-op, never a silent pass."""
    path = os.environ.get("HELM_PRIVATE_NEEDLES") or os.path.join(
        os.path.expanduser("~"), ".helm", "_global", "private-needles.txt")
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return (), path
    needles = tuple(
        ln.strip() for ln in lines if ln.strip() and not ln.lstrip().startswith("#"))
    return needles, path


def _git(root, *args, binary=False, env=None):
    """THE ONE GIT SPAWN IN THIS MODULE. Every git question the scanner asks
    goes through here (the direct-spawn audit in tests/test_vcs.py counts
    sites, and this file is executed as a plain hook script that cannot
    import helm/vcs.py). `env` is for the attribute sandbox only."""
    p = subprocess.run(("git",) + args, capture_output=True, cwd=root,
                       timeout=60, text=not binary, env=env)
    return p.returncode, p.stdout, p.stderr


def staged_paths(root):
    """Every path the NEXT commit would create or rewrite: additions, copies,
    modifications, renames (destination side), type changes. Deletions leave
    nothing behind to leak. Works on an unborn HEAD too — `diff --cached`
    baselines against the empty tree there (measured 2026-07-29)."""
    rc, out, err = _git(root, "diff", "--cached", "--name-only", "-z",
                        "--diff-filter=ACMRT")
    if rc != 0:
        raise RuntimeError("git diff --cached failed: %s" % err.strip())
    return [f for f in out.split("\0") if f]


def _staged_blob(root, rel):
    """The STAGED bytes (index stage 0), never the worktree file — they can
    differ in both directions and the commit takes the index. For a staged
    symlink this is the link target, which is exactly the payload to scan."""
    rc, out, _err = _git(root, "cat-file", "blob", ":0:" + rel, binary=True)
    if rc != 0:
        return None      # non-blob entry (e.g. a submodule gitlink)
    return out


def commit_parents(root):
    """THE COMMITTISHES THE COMMIT NOW BEING MADE WOULD HAVE AS PARENTS — the
    one primitive every added-ness question in this guard family asks, and the
    one `git diff --cached` alone cannot answer.

    WHY IT EXISTS, MEASURED ON A REAL LANE. `git diff --cached`
    baselines against HEAD, which during a merge is the FIRST parent only, so a
    lane merging its trunk saw everything the merge took from the other side as
    ADDED BY THIS COMMIT. One measured merge was refused naming 14 files as
    carrying private needles plus 30 e-mail addresses across 17 files, and
    `git rev-parse :<path>` equalled `git rev-parse <trunk>:<path>` for all 25
    of them — not one byte of it was that commit's. Exactly four files in that
    merge differed from BOTH parents. The seat did the only thing left and set
    HELM_NEVER_TRACK_SKIP=1 over a needle that WAS a true positive for the
    history it already sat in, which is the habit this guard family must never
    teach: a guard that refuses honest work does not stop a leak, it trains the
    bypass. Bug class: `guard-refuses-what-it-cannot-fix`.

    THE GIT-PATH ANSWER IS A PATHNAME, SO IT IS CARRIED AS BYTES. `rev-parse
    --git-path` returns a filesystem path, and a filesystem path is not
    required to be valid UTF-8 — a linked worktree whose common Git directory
    sits under an ancestor with a raw non-UTF-8 byte returns one that is not.
    Decoding it as text raised UnicodeDecodeError INSIDE this helper, before
    the existence check below, on an ordinary ASCII commit with no MERGE_HEAD
    at all; that escaped into every consumer of this helper, and the
    conflict-marker `--staged` rung (which catches RuntimeError, TimeoutExpired
    and OSError) died with a traceback on a clean ASCII staged file that main
    scans without complaint. So this one query runs in BINARY, the pathname is
    joined to an os.fsencode-d root and MERGE_HEAD is read in binary; only the
    oids — ASCII by construction — are decoded. No decoding failure can leave
    this function.

    HEAD, then every oid in MERGE_HEAD. MERGE_HEAD records ONE OID PER LINE, so
    an octopus is covered by reading that file and needs no MERGE_MSG parsing
    (pinned by an arm that merges two branches at once). It is read through
    `rev-parse --git-path`, so a linked worktree's own MERGE_HEAD is the one
    read and not the shared checkout's. An unborn HEAD yields [] — in the first
    commit every path is new. A MERGE_HEAD that cannot be read falls back to
    HEAD alone: that is the OLD, over-blocking behaviour, which is the safe
    direction for a leak guard to fail in.

    A MERGE'S BULK LEG IS NOT ASKED OF THIS SET: `scan_staged` judges a blob's
    size and bulk shape against HEAD alone (the first entry here), at this
    door and at the pre-merge-commit door, while needles, conflict markers and
    addresses credit every parent. See `scan_staged`.
    """
    rc, out, _err = _git(root, "rev-parse", "--verify", "--quiet", "HEAD")
    if rc != 0 or not out.strip():
        return []
    parents = [out.strip()]
    rc, out, _err = _git(root, "rev-parse", "--git-path", "MERGE_HEAD",
                         binary=True)
    if rc != 0 or not out.strip():
        return parents
    try:
        with open(os.path.join(os.fsencode(root), out.rstrip(b"\n")),
                  "rb") as f:
            merged = f.read().split()
    except OSError:
        return parents
    for oid in merged:
        oid = oid.decode("ascii", "replace")
        if oid not in parents:
            parents.append(oid)
    return parents


def _in_parents(root, parents, rel):
    """Was this path already committed in any of the GIVEN parents? A path one
    of them carries is not created by this commit. `scan_staged` asks it of
    every parent for needles and addresses and of HEAD alone for the bulk leg
    (`bulk_base`). No
    parent at all (an unborn HEAD) is right too: in the first commit every
    path is new."""
    for ref in parents:
        rc, _out, _err = _git(root, "cat-file", "-e", ref + ":" + rel)
        if rc == 0:
            return True
    return False


def _blob_oid(root, ref, rel):
    """The oid of `rel` at `ref` (a committish, or "" for the index), or None
    when it is not there. THE OID IS THE IDENTITY OF CONTENT: a staged blob
    whose oid equals a parent's blob at that path is that parent's blob, so
    this commit adds nothing at that path whatever a first-parent diff says."""
    rc, out, _err = _git(root, "rev-parse", "--verify", "--quiet",
                         "%s:%s" % (ref, rel))
    return out.strip() or None if rc == 0 else None


def _added_lines(root, rel, base=()):
    """The '+' lines of a zero-context staged diff of `rel` against `base` (no
    base = HEAD, git's own default). None when git calls the change binary —
    binary has no line structure, and the caller must then fall back to the
    WHOLE staged blob. Falling back is deliberate: an unreadable diff may not
    become a skipped scan, so the guard over-blocks on binary rather than
    under-seeing. A git failure RAISES: an empty answer here would read as
    "adds nothing".

    Hunk membership is tracked explicitly instead of filtering header lines
    by prefix. A `+++` filter is the obvious shortcut and it is wrong: an
    added line whose own first two characters are `++` renders as `+++...`
    and would be dropped silently — a needle-shaped hole in exactly the
    branch that blocks."""
    rc, out, err = _git(root, "diff", "--cached", "-U0", "--no-color",
                        "--no-ext-diff", *base, "--", rel, binary=True)
    if rc != 0:
        raise RuntimeError("git diff --cached failed for %s: %s"
                           % (rel, (err or b"").decode("utf-8", "replace").strip()))
    added, in_hunk = [], False
    for line in out.split(b"\n"):
        if line.startswith(b"@@"):
            in_hunk = True
            continue
        if not in_hunk:
            if line.startswith(b"Binary files ") or line.startswith(b"GIT binary patch"):
                return None
            continue
        if line[:1] == b"+":
            added.append(line[1:])
        elif line[:1] not in (b"-", b" ", b"\\"):
            in_hunk = False       # out of the hunk body, back in headers
    return b"\n".join(added)


def _added_bytes(root, rel, parents=None):
    """What this commit ADDS to `rel`, newlines rejoined so a needle spanning
    adjacent added lines is still found; None means "unknowable, use the whole
    blob" (see `_added_lines`).

    ADDED BY THIS COMMIT MEANS ABSENT FROM EVERY PARENT. With one parent (or
    none) that is exactly git's default base and this is byte-for-byte the old
    single-parent scan — `parents=None` does not even ask git who the parents
    are, so a caller that has not measured them pays nothing and behaves as
    before. With a MERGE in progress the first parent is one side of two or
    more, so:
      * a staged blob whose oid equals the blob at that path in ANY parent adds
        NOTHING — that content is already in history on that side, and this
        merge is not the commit that put it there;
      * otherwise (a conflict resolution, an auto-merge) the added side is the
        INTERSECTION of the per-parent added sides: a line every parent lacks
        is this commit's, a line any parent already has is not.
    A parent whose diff git calls binary contributes no restriction rather than
    an empty set — an unreadable diff must never shrink what is scanned — and
    when NO parent yields a readable diff the answer is None, the whole-blob
    fallback the single-parent case already uses."""
    if not parents or len(parents) < 2:
        return _added_lines(root, rel)
    staged = _blob_oid(root, "", rel)
    per = []
    for ref in parents:
        if staged is not None and _blob_oid(root, ref, rel) == staged:
            return b""
        lines = _added_lines(root, rel, (ref,))
        if lines is not None:
            per.append(lines.split(b"\n") if lines else [])
    if not per:
        return None
    keep = set(per[0])
    for lines in per[1:]:
        keep &= set(lines)
    return b"\n".join([ln for ln in per[0] if ln in keep])


def _payload(root, rel, parents=None):
    """(whole staged blob, the bytes this commit ADDS) — or None when the
    staged entry is not a readable blob at all. ONE lookup returning BOTH, so
    the caller cannot end up holding a fresh blob beside a stale added-side.
    On a binary change the added-side IS the whole blob: binary has no line
    structure, so the guard over-blocks rather than under-sees. `parents` is
    this commit's parent set (`commit_parents`); None asks git's default base,
    which is right for a single-parent commit and BLIND on a merge."""
    full = _staged_blob(root, rel)
    if full is None:
        return None
    added = _added_bytes(root, rel, parents)
    return full, (full if added is None else added)


def _needle_census(needles):
    """(note, classes) — what SHAPES the needle set can and cannot see.

    A shape-CENSUS heuristic, never a required-classes checklist: the scanner
    ships zero needles on purpose ("a guard that hardcodes WHOSE data to look
    for protects one person"), so the census must derive class coverage from
    whatever needles happen to be loaded — never presume which classes matter.

    Two shapes are unambiguous and load-bearing:
      address  `@` anywhere in a needle — an email, account@domain shape. If
              absent, the guard cannot see the single most common owner PII
              class (owner email, GitHub handle@domain, agent account id).
      path     `/` or `~` anywhere — a filesystem path or homedir shape. If
              absent, the guard cannot see paths that name where the owner's
              machines live.

    Returns "" when the census is impossible (no needles at all — the caller
    owns the "no needles" note which carries more detail)."""
    if not needles:
        return "", {}
    total = len(needles)
    has_addr = any("@" in n for n in needles)
    has_path = any(("/" in n) or n.startswith("~") for n in needles)
    absent = []
    if not has_addr:
        absent.append("address-shaped (@)")
    if not has_path:
        absent.append("path-shaped")
    if absent:
        return ("note: %d private needles loaded — shape census: no %s among "
                "them; the guard is armed against what IS listed but cannot "
                "see what shape-class is missing"
                % (total, " and no ".join(absent))), \
            {"total": total, "address": has_addr, "path": has_path}
    return ("note: %d private needles loaded — address and path shapes "
            "present (guard is not obviously thin)" % total), \
        {"total": total, "address": has_addr, "path": has_path}


# ---------------------------------------------------------------------------
# THE BULK-DATA LEG — what never enters history WHATEVER repo this is.
#
# The never-track set above is enumerated per repo and the needle scan is
# armed with the owner's own identifiers. Neither can see the third class of
# leak, and it is the expensive one: DATA THAT IS NOT THE PROJECT'S SOURCE —
# a customer's content export, a database dump, a browser-session snapshot,
# an archive, a subscriber list. It is nobody's needle (the people in it are
# not the owner), it sits at no enumerated path (it arrives wherever a tool
# wrote it), and once it is in history the remedy is a rewrite of a shared
# remote plus a fresh clone for every collaborator — the one git mistake
# that costs more to cure than every other kind put together.
#
# So this leg needs no configuration at all. It judges SHAPE: a blob larger
# than source ever is, content that declares itself an export or a dump or
# an archive, or a text that carries a crowd of e-mail addresses. Each is a
# refusal when THIS commit adds it and a note when HEAD already carries it
# (the same split as the needle scan — a guard that refuses honest work
# because of a leak it cannot undo teaches everyone the bypass).
#
# THE OVERRIDE IS A TRACKED DECLARATION, NOT AN ENV FLAG. A file that is
# legitimately large or legitimately export-shaped (a golden fixture, a
# vendored corpus) is declared in .gitattributes as `<path> helm-bulk=ok`,
# and a source file that QUOTES an export or dump header (a parser test, a
# fixture builder) as `<path> helm-bulk=quotes`, where a reviewer sees it in
# the diff and a later reader finds it in the tree. The declaration is the
# ONE door: no path suffix and no byte scan infers "this is source that
# quotes the format" — quote characters occur inside the raw formats. The one-commit env skip exists for the false positive nobody wants
# to encode; it is not the way to commit an export.
# ---------------------------------------------------------------------------

BULK_CEILING = 1 << 20     # bytes; source files are not this big
BULK_ADDRESSES = 20        # distinct e-mail addresses ADDED by one commit,
                           # reserved-domain ones excluded (reserved_address)
BULK_ATTR = "helm-bulk"    # .gitattributes: `<path> helm-bulk=ok|quotes` declares it
_SNIFF = 1 << 16

# THE ADDRESS SCAN IS ANCHORED ON THE `@`, NOT SWEPT OVER THE BYTES. A sweeping
# `[local]+@` pattern is quadratic on a long run of local-part characters (a
# megabyte of one letter, which a fixture for the size ceiling is): every
# start position consumes the run and backtracks it looking for the `@`.
# Anchoring on each `@` and validating a BOUNDED window on either side costs
# O(number of @) with a fixed per-hit ceiling, whatever the blob is made of.
_LOCAL_RE = re.compile(rb"[A-Za-z0-9._%+-]{1,64}\Z")
_DOMAIN_RE = re.compile(
    rb"[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63}){0,8}\.[A-Za-z]{2,24}(?![A-Za-z0-9.-])")

# WHY A SOURCE FILE THAT QUOTES A HEADER IS DECLARED, NOT INFERRED. A parser
# test that quotes the WXR header, a fixture builder that carries a dump's
# banner in a string, contains the declaration without BEING the thing
# declared. That difference cannot be read off the bytes: leading bytes,
# a prolog grammar, a keyword list and the quoting spans of the language
# each admit a witness, because a rename is not a door and quote characters
# occur inside the raw formats themselves (an XML attribute value, an SQL
# comment) — an XML attribute quotes the namespace URL, an SQL comment can
# hold a quoted banner, a DOCTYPE's internal subset holds a `>`, a dump can
# open with any keyword. So the self-declaration shapes
# fire on every path, and the one thing that waives them is the STAGED
# `<path> helm-bulk=quotes` — a reviewed decision in the diff — which still
# leaves the magic bytes, the size ceiling and the address count judging.
_WXR_MARKS = (b"wp:wxr_version", b"wordpress.org/export/")
_DUMP_MARKS = (b"-- MySQL dump", b"-- PostgreSQL database dump", b"CREATE TABLE", b"INSERT INTO")


# THE THIRD ELEMENT: whether the shape is a SELF-DECLARATION (the content
# says what it is), which is the only kind a `helm-bulk=quotes` declaration
# waives. Magic bytes and the snapshot path are what the content IS.
_BULK_SHAPES = (
    ("an archive or database file",
     lambda rel, head, blob: head.startswith((b"PK\x03\x04", b"\x1f\x8b",
                                              b"SQLite format 3\x00",
                                              b"7z\xbc\xaf\x27\x1c", b"BZh"))
     or blob[257:262] == b"ustar", False),
    ("a WordPress export (WXR)",
     lambda rel, head, blob: any(m in head for m in _WXR_MARKS), True),
    ("a database dump",
     lambda rel, head, blob: any(m in head for m in _DUMP_MARKS[:2])
     or all(m in head for m in _DUMP_MARKS[2:]), True),
    ("a browser-session snapshot",
     lambda rel, head, blob: "/.playwright-mcp/" in "/" + rel, False),
)
_SELF_DECLARED = frozenset(reason for reason, _hit, declared in _BULK_SHAPES if declared)


def bulk_shape(rel, blob, waived=frozenset()):
    """The first bulk-data shape `blob` (staged at `rel`) matches and that is
    not in `waived`, as a reason for a human, or None. Reads the head of the
    blob for the self-declarations and the magic bytes; the tar check reads
    its fixed offset. A waiver removes shapes from the question, never the
    question: with the self-declarations waived the remaining shapes are
    still asked, so a snapshot or an archive that also carries a header is
    still what it is."""
    head = blob[:_SNIFF]
    for reason, hit, _declared in _BULK_SHAPES:
        if reason not in waived and hit(rel, head, blob):
            return reason
    return None


# RESERVED NAMES ARE NOT ANYBODY'S ADDRESS. RFC 2606 and RFC 6761 set aside
# example.com/.net/.org and the .example, .test, .invalid and .localhost
# top-level names so that documentation and tests can write an address that
# can never reach a person. A fixture that uses them is doing exactly what
# the leak law asks for, so they do not count toward BULK_ADDRESSES: the
# refusal is for real data (a client export, an address table), and
# counting the synthetic ones made rewriting fixtures onto reserved names
# refuse the very commit that removed the real-looking ones. Matching is on
# the whole domain or a whole trailing label, never a substring, so
# myexample.com, example.com.co and test.io still count.
_RESERVED_DOMAINS = (b"example.com", b"example.net", b"example.org")
_RESERVED_TLDS = (b"example", b"test", b"invalid", b"localhost")


def reserved_address(addr):
    """True when `addr` (bytes, as `addresses_in` yields it) is at a domain
    RFC 2606 / RFC 6761 reserves, so it names no one."""
    domain = addr.rsplit(b"@", 1)[-1].lower().rstrip(b".")
    if domain.rsplit(b".", 1)[-1] in _RESERVED_TLDS:
        return True
    return any(domain == d or domain.endswith(b"." + d)
               for d in _RESERVED_DOMAINS)


def address_count(added):
    """Distinct e-mail addresses in the bytes a commit ADDS — the shape of a
    subscriber list, a comment export, an order table. Case-folded, so one
    address written two ways is one address."""
    return len(addresses_in(added))


def addresses_in(added):
    """The set behind address_count, so the scan can take the UNION across
    every staged file: the threshold is per COMMIT, and a list split over
    two files is still one list."""
    seen = set()
    at = added.find(b"@")
    while at != -1:
        local = _LOCAL_RE.search(added[max(0, at - 64):at])
        domain = _DOMAIN_RE.match(added, at + 1)
        if local and domain:
            seen.add((local.group(0) + b"@" + domain.group(0)).lower())
        at = added.find(b"@", at + 1)
    return seen


def _bulk_declaration(root, rel, notes=None):
    """The VALUE the STAGED .gitattributes — and nothing else — declares for
    `rel`: "ok" (every bulk finding waived), "quotes" (only the
    self-declaration shapes waived: the file is source that QUOTES an export
    or dump header; the ceiling, the magic bytes and the address count still
    judge it), or None. An unrecognised value is None, said out loud. This is
    the tracked, reviewed override — the ONE door, and it is a decision a
    reviewer sees in the diff, never a property inferred from the bytes:
    quote characters occur inside the raw formats themselves, so no scanner
    can establish provenance. git consults four sources for an
    attribute (the working tree's .gitattributes, the index's, the repo's
    info/attributes and a global file) and only the index's is in the diff a
    reviewer reads, so the decision is asked of git inside a SANDBOX that has
    only that source: a temporary git dir (no info/attributes) over the real
    index and object store, the global file disabled, the system file
    ignored. Matching stays git's own; what changes is what it may read.

    THE ENVIRONMENT IS BUILT, NOT INHERITED. Any GIT_* variable in the
    caller's environment is repository-selection authority (GIT_COMMON_DIR
    alone would point the sandbox's info/attributes back at the real repo),
    so every GIT_* is dropped and only the four the sandbox needs are set.
    The sandbox carries the real repo's object format (a sha256 repo's index
    is unreadable under the default) and links its split-index shares, and
    a git error is reported as "could not decide" — treated as undeclared,
    but never silently."""
    rc, gitdir, _err = _git(root, "rev-parse", "--absolute-git-dir")
    rc2, common, _err = _git(root, "rev-parse", "--path-format=absolute",
                             "--git-common-dir")
    if rc != 0 or rc2 != 0 or not gitdir.strip() or not common.strip():
        return False
    gitdir, common = gitdir.strip(), common.strip()
    index = os.environ.get("GIT_INDEX_FILE") or os.path.join(gitdir, "index")
    _rc, objfmt, _err = _git(root, "config", "--get", "extensions.objectformat")
    objfmt = objfmt.strip()
    with tempfile.TemporaryDirectory(prefix="helm-attr-") as sandbox:
        os.makedirs(os.path.join(sandbox, "refs"))
        os.makedirs(os.path.join(sandbox, "objects"))
        with open(os.path.join(sandbox, "HEAD"), "w") as f:
            f.write("ref: refs/heads/main\n")
        with open(os.path.join(sandbox, "config"), "w") as f:
            f.write("[core]\n\trepositoryformatversion = %s\n"
                    % ("1" if objfmt else "0"))
            if objfmt:
                f.write("[extensions]\n\tobjectformat = %s\n" % objfmt)
        for name in os.listdir(gitdir):
            if name.startswith("sharedindex."):
                os.symlink(os.path.join(gitdir, name),
                           os.path.join(sandbox, name))
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env.update(GIT_DIR=sandbox, GIT_WORK_TREE=root, GIT_INDEX_FILE=index,
                   GIT_OBJECT_DIRECTORY=os.path.join(common, "objects"),
                   GIT_ATTR_NOSYSTEM="1")
        rc, out, err = _git(root, "-c", "core.attributesFile=/dev/null",
                            "check-attr", "--cached", BULK_ATTR, "--", rel,
                            env=env)
    if rc != 0:
        if notes is not None:
            notes.append("note: %s — the staged-attributes sandbox could not "
                         "decide %s (git: %s); treated as NOT declared"
                         % (rel, BULK_ATTR, (err or "").strip()[:120]))
        return None
    value = out.rsplit(":", 1)[-1].strip()
    if value == "set":
        return "ok"
    if value in ("ok", "quotes"):
        return value
    if value not in ("unspecified", "unset") and notes is not None:
        notes.append("note: %s — %s=%s is not a value this guard knows (ok | "
                     "quotes); treated as NOT declared" % (rel, BULK_ATTR, value))
    return None


def _parent_size(root, ref, rel):
    """Size of `ref`'s blob at `rel`, or None when that parent has none."""
    rc, out, _err = _git(root, "cat-file", "-s", ref + ":" + rel)
    return int(out.strip()) if rc == 0 and out.strip().isdigit() else None


def _parent_shape(root, ref, rel, waived=frozenset()):
    """The bulk shape of `ref`'s copy at `rel`, or None when that parent has
    none or it is not bulk-shaped. Reached only when the STAGED copy is
    bulk-shaped and the path already exists in some parent, so the whole-blob
    read this costs is paid on the rare path, not on every commit; the shape
    test itself looks at the prefix."""
    rc, out, _err = _git(root, "cat-file", "blob", ref + ":" + rel, binary=True)
    if rc != 0:
        return None
    return bulk_shape(rel, out[:_SNIFF], waived)


def _already_over(root, parents, rel):
    """Did any of the GIVEN parents already carry `rel` over the ceiling? Then
    this commit is not what put an oversized blob at that path. `scan_staged`
    gives it HEAD alone (`bulk_base`), so a merge's incoming parent never
    credits a size here, at either door."""
    for ref in parents:
        size = _parent_size(root, ref, rel)
        if size is not None and size > BULK_CEILING:
            return True
    return False


def _already_shaped(root, parents, rel, shape, waived=frozenset()):
    """Did any of the GIVEN parents' copies of `rel` already have this bulk
    shape? An export written over an ordinary file is ADDED at an existing
    path. `scan_staged` gives it HEAD alone (`bulk_base`), so an export a merge
    takes unchanged from its incoming parent is refused at both doors and
    takes helm-bulk=ok."""
    for ref in parents:
        if _parent_shape(root, ref, rel, waived) == shape:
            return True
    return False


def _mib(n):
    return "%.1f MiB" % (n / float(1 << 20))


def bulk_findings(root, rel, full, added, new_path, notes=None, parents=None):
    """-> (violations, notes, addresses) for one staged path under the
    bulk-data leg. Adding is what blocks: a NEW path, or a size/shape HEAD's
    copy did not have. A pre-existing one is a note naming the scrub it
    needs. A path declared `helm-bulk=ok` in .gitattributes turns every
    would-be refusal into a note that says who declared it, and contributes
    no addresses to the commit-wide count; `helm-bulk=quotes` waives only a
    self-declaration shape (a fixture or test that quotes an export or dump
    header) and leaves the ceiling, the magic bytes and the addresses judging. `addresses` is the set this file
    ADDS, minus reserved-domain ones (`reserved_address`); the caller unions
    them across the commit and judges the total."""
    parents = ["HEAD"] if parents is None else parents
    hits = []
    size = len(full)
    if size > BULK_CEILING:
        grew_past = new_path or not _already_over(root, parents, rel)
        hits.append((grew_past, "%s — a %s blob (ceiling %s): not source"
                     % (rel, _mib(size), _mib(BULK_CEILING))))
    shape = bulk_shape(rel, full)
    if shape:
        # ADDED when no given parent had a copy, or no given parent's copy was
        # this shape: an export written over an ordinary file at an existing
        # path is exactly as new as one at a new path, and the pathname must
        # not launder it. The given parents are HEAD alone for a merge, so an
        # export the incoming parent carries unchanged is still ADDED here.
        adds = new_path or not _already_shaped(root, parents, rel, shape)
        hits.append((adds, "%s — its content is %s" % (rel, shape)))
    # Only addresses that could reach a person count toward the crowd.
    addrs = {x for x in addresses_in(added) if not reserved_address(x)}
    if not hits and not addrs:
        return [], [], set()
    declared = _bulk_declaration(root, rel, notes)
    if declared == "ok":
        return [], ["%s — declared %s=ok in .gitattributes, so it lands "
                    "(a tracked decision; revisit it if the file changed "
                    "purpose)" % (text.split(" — ", 1)[0], BULK_ATTR)
                    for _adds, text in hits], set()
    waived = []
    if declared == "quotes" and shape in _SELF_DECLARED:
        # The waiver removes the self-declaration from the QUESTION and asks
        # the remaining shapes again: a snapshot or an archive that also
        # carries a header is still refused as what it is.
        hits = [h for h in hits if not h[1].endswith(shape)]
        waived = ["%s — declared %s=quotes in .gitattributes: source that "
                  "quotes %s; the ceiling, magic bytes and addresses still "
                  "judge it" % (rel, BULK_ATTR, shape)]
        rest = bulk_shape(rel, full, _SELF_DECLARED)
        if rest:
            adds = new_path or not _already_shaped(root, parents, rel, rest,
                                                   _SELF_DECLARED)
            hits.append((adds, "%s — its content is %s" % (rel, rest)))
    violations = [text + ", ADDED BY THIS COMMIT" for adds, text in hits if adds]
    notes = waived + [text + _pre_existing(parents)
                      for adds, text in hits if not adds]
    return violations, notes, addrs


def address_findings(by_file):
    """The commit-wide address judgement: `by_file` maps each staged path to
    the addresses it ADDS; the union across the commit is what is judged,
    so a list split over two files is refused as one list. Names every file
    that contributed."""
    union = set()
    for addrs in by_file.values():
        union |= addrs
    if len(union) < BULK_ADDRESSES:
        return []
    files = sorted(rel for rel, addrs in by_file.items() if addrs)
    return ["%s — this commit adds %d distinct e-mail addresses across %d "
            "file(s): a subscriber or customer list, ADDED BY THIS COMMIT"
            % (", ".join(files), len(union), len(files))]



_PRE_EXISTING = (" — ALREADY IN HEAD, so this commit is not the leak and is "
                 "NOT blocked. It needs its own scrub commit; until then this "
                 "line prints on every commit that touches the file.")
# THE SAME SENTENCE ABOUT A MERGE. Derived from the one above rather than
# written twice, so the two cannot drift: the only difference is WHERE the
# content already is, and on a merge "already in HEAD" is simply false for a
# blob this merge took from the other side.
_PRE_EXISTING_MERGE = _PRE_EXISTING.replace(
    "ALREADY IN HEAD", "ALREADY IN A PARENT OF THIS MERGE", 1)


def _pre_existing(parents):
    """The suffix for a finding this commit did not add. One parent keeps the
    old wording byte-for-byte (that parent IS HEAD); a merge names the parent
    set instead of a HEAD that is only one of its sides."""
    return _PRE_EXISTING if len(parents or ()) < 2 else _PRE_EXISTING_MERGE


def _under_never_track(rel, table):
    for prefix, why in table.items():
        bare = prefix.rstrip("/")
        if rel == bare or rel.startswith(bare + "/"):
            return prefix, why
    return None, None


def scan_staged(root):
    """-> (violations, notes): violations BLOCK the commit; notes are reported
    and let it through. Needle hits name the needle's POSITION in the local
    needle file, never its value — the guard stays quiet about the thing it
    guards.

    THE NEEDLE SCAN READS THE DIFF, NOT THE FILE (2026-07-30). It first scanned
    each staged file WHOLE, so one needle sitting in a file's HISTORY refused
    every later commit that touched that file — for content the commit did not
    write and could not remove. Measured: three occurrences of one needle in
    helm/web_ui.html's hint copy blocked every commit to a 3,000-line file, and
    the seat that hit it did the only thing left and used the documented
    one-commit bypass. That is the actual damage: a guard that refuses honest
    work does not stop the leak (the leak is already in history — blocking a
    NEW commit cannot un-commit it), it just teaches everyone the bypass flag,
    and then it is not guarding anything at all.

    So the two cases are separated, and BOTH are reported:
      * the commit ADDS the needle (new path, or a '+' line) -> VIOLATION.
        This is the only case blocking can still prevent.
      * the needle is already in HEAD's copy -> NOTE, every single run, naming
        the file and telling the reader it is a pre-existing leak needing a
        scrub commit. Silence here would be the opposite failure: a leak that
        no longer blocks anything and that nobody is ever told about.
    A needle in the PATH splits the same way — a NEW path adds its own name,
    a path already in HEAD does not — and a path hit never short-circuits the
    content scan for that needle, because a file already living at a
    needle-carrying path is precisely the one whose body nobody re-reads.
    Bug class: `guard-refuses-what-it-cannot-fix`.

    AND ADDED MEANS ABSENT FROM EVERY PARENT, NOT FROM HEAD. The
    same split was measured against the FIRST parent only, so a merge of a
    trunk into a lane read every blob the merge took from the trunk as this
    commit's — and refused the merge over needles already in the trunk's
    history. `commit_parents` carries that measurement; the rule here is that a
    needle, a conflict marker or an address already present in ANY parent is
    never a leak of this commit, so for those a merge is refused only for what
    its conflict resolutions and auto-merges actually introduce. A blob's SIZE
    and BULK SHAPE are not credited that way: they are judged against HEAD
    alone (`bulk_base`), so a blob over the size line that only the incoming
    parent carries is refused here as it is at the merge door."""
    needles, needles_path = load_private_needles()
    never_track = never_track_set()
    violations, notes = [], []
    addresses_by_file = {}
    # MEASURED ONCE FOR THE WHOLE SCAN: every added-ness question below is
    # asked against this set, never against HEAD alone. `staged_paths` stays a
    # first-parent diff on purpose — it is a SUPERSET of what can be added
    # here, since a result blob equal to HEAD's is a blob a parent already has.
    parents = commit_parents(root)
    if len(parents) > 1:
        notes.append("note: merge in progress — added-ness is measured against "
                     "all %d parents (%s), so content any parent already "
                     "carries is not this commit's; a blob's size and bulk "
                     "shape are measured against HEAD (%s), the branch the "
                     "merge lands on"
                     % (len(parents), ", ".join(p[:12] for p in parents),
                        parents[0][:12]))
    # THE BULK SIZE/SHAPE LEG JUDGES A MERGE AGAINST THE FIRST PARENT. The
    # merge door (pre-merge-commit) runs before git writes MERGE_HEAD, so it
    # sees HEAD alone; a merge concluded with `git commit` (--no-commit, a
    # conflict) reaches this scan with MERGE_HEAD present. Crediting every
    # parent there let a blob only the merged branch carries land through the
    # door beside the one that refuses it. Needles and addresses keep the
    # every-parent law: those are the over-blocks measured on a trunk merge.
    bulk_base = parents[:1]
    pre_existing = _pre_existing(parents)
    for rel in staged_paths(root):
        prefix, why = _under_never_track(rel, never_track)
        if prefix:
            violations.append("%s — under never-track path '%s': %s"
                              % (rel, prefix, why))
        new_path = not _in_parents(root, parents, rel)
        # THE PATH IS SCANNED BEFORE THE BLOB IS FETCHED, so an entry with no
        # readable blob (a submodule gitlink) still has its NAME checked — a
        # needle in a path is a leak whatever the entry's content is.
        for i, needle in enumerate(needles, 1):
            if needle in rel:
                # A path that already exists in HEAD already carries whatever
                # its name carries; only a NEW or renamed path adds it.
                (violations if new_path else notes).append(
                    "%s — its PATH carries private needle #%d of %s%s"
                    % (rel, i, needles_path,
                       "" if new_path else pre_existing))
        payload = _payload(root, rel, parents)
        if payload is None:
            notes.append("note: %s is not a readable blob (submodule?)"
                         " — content scan skipped for it" % rel)
            continue
        bulk_v, bulk_n, addrs = bulk_findings(
            root, rel, payload[0], payload[1],
            new_path if bulk_base == parents
            else not _in_parents(root, bulk_base, rel), notes, bulk_base)
        violations.extend(bulk_v)
        notes.extend(bulk_n)
        addresses_by_file[rel] = addrs
        if not needles:
            continue
        for i, needle in enumerate(needles, 1):
            # the CONTENT is scanned for the same needle a path hit named: a
            # file already sitting at a needle-carrying path is exactly the
            # one whose content nobody re-reads.
            full, added = payload
            nb = needle.encode()
            if nb in added:
                violations.append(
                    "%s — its staged CONTENT carries private needle #%d of "
                    "%s, ADDED BY THIS COMMIT" % (rel, i, needles_path))
            elif nb in full:
                notes.append(
                    "%s — its staged CONTENT carries private needle #%d of "
                    "%s%s" % (rel, i, needles_path, pre_existing))
    violations.extend(address_findings(addresses_by_file))
    if not needles:
        notes.append("note: no private needles configured (%s) — the content "
                     "scan is a NO-OP on this estate, not a pass" % needles_path)
    else:
        census_note, _ = _needle_census(needles)
        if census_note:
            notes.append(census_note)
    return violations, notes


def main(argv=None):
    """The pre-commit entry: scan the staged set of the repo at cwd, refuse
    on any hit. Exit 0 clean, 1 on violations, 2 when the scan itself cannot
    run (fail CLOSED — an unscannable commit is not a scanned one)."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv != ["--staged"]:
        sys.stderr.write("usage: nevertrack.py --staged  (run by the helm "
                         "pre-commit guard; cwd must be inside the repo)\n")
        return 2
    rc, out, _err = _git(os.getcwd(), "rev-parse", "--show-toplevel")
    if rc != 0 or not out.strip():
        sys.stderr.write("[helm never-track] REFUSED: not inside a git work "
                         "tree — cannot scan the staged set\n")
        return 2
    root = out.strip()
    try:
        violations, notes = scan_staged(root)
    except (RuntimeError, subprocess.TimeoutExpired, OSError) as exc:
        sys.stderr.write("[helm never-track] REFUSED: staged-set scan failed "
                         "— %s\n" % exc)
        return 2
    for note in notes:
        sys.stderr.write("[helm never-track] %s\n" % note)
    if not violations:
        return 0
    w = sys.stderr.write
    w("[helm never-track] REFUSED: this commit stages material that must "
      "never enter history:\n")
    for v in violations:
        w("[helm never-track]   %s\n" % v)
    w("[helm never-track] unstage with `git restore --staged <path>` — the "
      "file STAYS on disk and keeps working locally.\n")
    w("[helm never-track] bulk data (exports, dumps, snapshots, archives, "
      "address lists, anything over the ceiling) lives OUTSIDE the tree: "
      "gitignore its directory. A legitimately large or export-shaped "
      "tracked file is declared `<path> helm-bulk=ok` in .gitattributes; "
      "source that QUOTES an export or dump header (a parser test, a "
      "fixture builder) is declared `<path> helm-bulk=quotes`.\n")
    w("[helm never-track] already in history? untracking is NOT a scrub — "
      "the scrub ladder is in the devops skill (Repository hygiene).\n")
    w("[helm never-track] law + incident history: tests/test_never_track.py; "
      "scanner: helm/nevertrack.py\n")
    w("[helm never-track] false positive? that is an OWNER decision: "
      "HELM_NEVER_TRACK_SKIP=1 skips this scan for one commit.\n")
    return 1


if __name__ == "__main__":
    sys.exit(main())
