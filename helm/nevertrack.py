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
import subprocess
import sys

# Enumerated, not pattern-matched: a pattern would invite argument about
# whether some new file "counts". Each entry names WHY, because a rule whose
# reason is lost gets deleted by the next person tidying up. (Moved verbatim
# from tests/test_never_track.py, which now imports it from here.)
NEVER_TRACK = {
    "agents/claudecode/skills/i-have-audhd/":
        "personal owner material, including verbatim private quotes. Never "
        "appropriate on any remote, private or otherwise. (Kept deliberately "
        "vague — see tests/test_never_track.py: a reason must not restate "
        "what it protects.)",
    "agents/claudecode/skills/fleet-usage/":
        "irreducibly coupled to a PRIVATE project's MCP surface. Useless to "
        "a public installer and it "
        "leaks a private project name; no public-integration ruling covers it, "
        "unlike the deliberately-kept public deps.",
    "evals/":
        "internal BUILD DOCUMENTS, not published artifacts: dated dogfood "
        "reports carrying kanban card ids, internal PRD paths under ~/.helm, "
        "internal lane and branch names, and a verbatim owner quote about "
        "private infrastructure plans. Nothing public links them (the "
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


def _git(root, *args, binary=False):
    p = subprocess.run(("git",) + args, capture_output=True, cwd=root,
                       timeout=60, text=not binary)
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


def _in_head(root, rel):
    """Was this path already committed? Nonzero on an unborn HEAD too, which is
    right: in the first commit every path is new."""
    rc, _out, _err = _git(root, "cat-file", "-e", "HEAD:" + rel)
    return rc == 0


def _added_bytes(root, rel):
    """What this commit ADDS to `rel`: the '+' side of a zero-context staged
    diff, newlines rejoined so a needle spanning adjacent added lines is still
    found. Returns None when git calls the change binary — binary has no line
    structure, and the caller must then fall back to the WHOLE staged blob.
    Falling back is deliberate: an unreadable diff may not become a skipped
    scan, so the guard over-blocks on binary rather than under-seeing. A git
    failure RAISES: an empty answer here would read as "adds nothing".

    Hunk membership is tracked explicitly instead of filtering header lines
    by prefix. A `+++` filter is the obvious shortcut and it is wrong: an
    added line whose own first two characters are `++` renders as `+++...`
    and would be dropped silently — a needle-shaped hole in exactly the
    branch that blocks."""
    rc, out, err = _git(root, "diff", "--cached", "-U0", "--no-color",
                        "--no-ext-diff", "--", rel, binary=True)
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


def _payload(root, rel):
    """(whole staged blob, the bytes this commit ADDS) — or None when the
    staged entry is not a readable blob at all. ONE lookup returning BOTH, so
    the caller cannot end up holding a fresh blob beside a stale added-side.
    On a binary change the added-side IS the whole blob: binary has no line
    structure, so the guard over-blocks rather than under-sees."""
    full = _staged_blob(root, rel)
    if full is None:
        return None
    added = _added_bytes(root, rel)
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


_MISSING = object()      # "not looked up yet", distinct from a None lookup

_PRE_EXISTING = (" — ALREADY IN HEAD, so this commit is not the leak and is "
                 "NOT blocked. It needs its own scrub commit; until then this "
                 "line prints on every commit that touches the file.")


def _under_never_track(rel):
    for prefix, why in NEVER_TRACK.items():
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
    Bug class: `guard-refuses-what-it-cannot-fix`."""
    needles, needles_path = load_private_needles()
    violations, notes = [], []
    for rel in staged_paths(root):
        prefix, why = _under_never_track(rel)
        if prefix:
            violations.append("%s — under never-track path '%s': %s"
                              % (rel, prefix, why))
        if not needles:
            continue
        new_path = not _in_head(root, rel)
        payload = _MISSING
        for i, needle in enumerate(needles, 1):
            if needle in rel:
                # A path that already exists in HEAD already carries whatever
                # its name carries; only a NEW or renamed path adds it.
                (violations if new_path else notes).append(
                    "%s — its PATH carries private needle #%d of %s%s"
                    % (rel, i, needles_path,
                       "" if new_path else _PRE_EXISTING))
                # and the CONTENT is still scanned for that same needle: a
                # file already sitting at a needle-carrying path is exactly
                # the one whose content nobody re-reads.
            if payload is _MISSING:
                payload = _payload(root, rel)
                if payload is None:
                    notes.append("note: %s is not a readable blob (submodule?)"
                                 " — content scan skipped for it" % rel)
                    break
            full, added = payload
            nb = needle.encode()
            if nb in added:
                violations.append(
                    "%s — its staged CONTENT carries private needle #%d of "
                    "%s, ADDED BY THIS COMMIT" % (rel, i, needles_path))
            elif nb in full:
                notes.append(
                    "%s — its staged CONTENT carries private needle #%d of "
                    "%s%s" % (rel, i, needles_path, _PRE_EXISTING))
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
    w("[helm never-track] law + incident history: tests/test_never_track.py; "
      "scanner: helm/nevertrack.py\n")
    w("[helm never-track] false positive? that is an OWNER decision: "
      "HELM_NEVER_TRACK_SKIP=1 skips this scan for one commit.\n")
    return 1


if __name__ == "__main__":
    sys.exit(main())
