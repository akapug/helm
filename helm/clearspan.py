#!/usr/bin/env python3
"""helm.clearspan — re-measure a dispatch row's claims against live trunk.

A row filed with "dispatches.py:306 has no deadline guard" is a measurable
claim: the file, line, and content either still match or they do not. A row
filed with "wire owner triage properly" has nothing to re-measure and stays
human-judged. The split is the whole design — measurable claims decay
automatically; prose claims stay hand-named.

THE TWO DISEASES (see the living-pipeline meld, 2026-08-03):
  born-wrong — a row that was already a duplicate when written (add-path)
  rotted      — a row that was TRUE when filed and decayed (re-measure)

This module cures ROTTED. It does not touch the add path; it answers "is this
OPEN row's evidence still true against the tree that is checked out RIGHT NOW."

MEASUREMENT TYPES:
  file:line  — a code location (``dispatches.py:306``, ``helm/dispatches.py:306``)
  count      — a numeric claim about rows/things (``5 stalled rows``)
  sha        — a commit tip claim (40-char hex)

A row whose ONLY claims are prose ("wire X", "clean up Y") reads UNKNOWN —
not stale, because nothing measurable could have rotted.

INTEGRATION: called from the list renderer and the triage surface before a seat
picks up a row. Because re-measure can spawn subprocesses, the list view runs
a LIGHTWEIGHT parse-only check; the full git-backed check runs on-demand.

The git subprocesses are direct spawns counted by DirectSpawnAuditTest in
tests/test_vcs.py. They answer a dispatch-row question using git, and vcs.py
has no dispatch-row-aware operations; routing through it would add one hop
and an import cycle for no gain. Fail-UNKNOWN by contract: a broken or
unreadable repo returns 'unavailable', which is distinct from 'fresh' and
'stale', so a failed re-measure never reads as clean evidence.
"""
import os
import re
import subprocess

# ---------------------------------------------------------------------------
# claim parsing — extract measurable claims from a dispatch row body
# ---------------------------------------------------------------------------

# A file:line reference: module.py:306, helm/module.py:306, tests/test_x.py:42
_FILE_LINE = re.compile(r"\b([\w_./-]+\.py):(\d+)\b")

# A count claim: "5 stalled rows", "18 total"
_COUNT = re.compile(r"\b(\d[\d,]*)\s+(\w[\w\s]{1,40})\b(?!\S*\d)", re.I)

# 40-char hex shas (a commit tip claim)
_SHA = re.compile(r"\b([0-9a-f]{40})\b")


def parse_claims(note, lane=""):
    """(file_lines, counts, shas) from a dispatch row's note and lane.

    Each file_line is (path, line_number). Each count is (number, subject).
    Each sha is a full 40-char hex tip.
    """
    text = " ".join(s for s in (note or "", lane or "") if s)
    fls = [(m.group(1), int(m.group(2))) for m in _FILE_LINE.finditer(text)]
    # Strip file:line refs before count-parsing, so "dispatches.py:306"
    # does not match as "306 stalled"
    clean = _FILE_LINE.sub(" ", text)
    counts = [(int(m.group(1).replace(",", "")), m.group(2).strip())
              for m in _COUNT.finditer(clean)]
    shas = list(set(_SHA.findall(text)))
    return fls, counts, shas


# ---------------------------------------------------------------------------
# file:line re-verification
# ---------------------------------------------------------------------------

def _repo_root(repo_path):
    """Absolute git toplevel of repo_path, or None."""
    try:
        r = subprocess.run(
            ["git", "-C", str(repo_path), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=5)
        return r.stdout.strip() if r.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def _head_sha(repo_path):
    """HEAD commit sha, or None."""
    try:
        r = subprocess.run(
            ["git", "-C", str(repo_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5)
        return r.stdout.strip() if r.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def _file_at_line(root, path, lineno):
    """(line_text, err) — the text at lineno in file at HEAD, or None.

    Tries the path as-is, then resolves bare filenames by searching the tree
    (common in dispatch notes: ``dispatches.py:306`` without the helm/ prefix).
    """
    try:
        r = subprocess.run(
            ["git", "-C", root, "show", "HEAD:" + path],
            capture_output=True, text=True, timeout=5)
        if r.returncode != 0:
            # Bare filename fallback: try helm/<path>, tests/<path>
            if "/" not in path:
                for prefix in ("helm/", "tests/"):
                    r2 = subprocess.run(
                        ["git", "-C", root, "show", "HEAD:" + prefix + path],
                        capture_output=True, text=True, timeout=5)
                    if r2.returncode == 0:
                        r = r2
                        break
                else:
                    return None, "no such file at HEAD"
            else:
                return None, "no such file at HEAD"
        lines = r.stdout.split("\n")
        if lineno < 1 or lineno > len(lines):
            return None, "line %d out of range (file has %d lines)" % (
                lineno, len(lines))
        return lines[lineno - 1], None
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, "git show failed: %s" % e


def _file_at_rev(root, rev, path):
    """(text, err) — the file's full text at rev, or an error."""
    try:
        r = subprocess.run(
            ["git", "-C", root, "show", rev + ":" + path],
            capture_output=True, text=True, timeout=5)
        if r.returncode != 0 and "/" not in path:
            for prefix in ("helm/", "tests/"):
                r2 = subprocess.run(
                    ["git", "-C", root, "show", rev + ":" + prefix + path],
                    capture_output=True, text=True, timeout=5)
                if r2.returncode == 0:
                    r = r2
                    break
        if r.returncode != 0:
            return None, "no such file at %s" % rev[:12]
        return r.stdout, None
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, "git show failed: %s" % e


def _rev_at_ts(root, ts):
    """The last commit at or before ts, or None — the row's filing-era tree."""
    if not ts:
        return None
    try:
        r = subprocess.run(
            ["git", "-C", root, "log", "-1", "--before=" + str(ts),
             "--format=%H"], capture_output=True, text=True, timeout=5)
        return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() \
            else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def _check_file_lines(fls, repo_root_path, filed_ts=None):
    """[(result, detail)] for each file:line claim.

    result is 'fresh' | 'stale' | 'unavailable'.

    CONTENT, NOT EXISTENCE — the acceptance specimen: a line that MOVED. The
    first cut read any in-range line as 'fresh', so rearm.py:353 read FRESH
    while sitting empty — the code it named was long gone. The row carries
    its own predicate: what the line said when the row was WRITTEN (the last
    commit at or before the row's ts). Same text then and now is FRESH;
    different text — including the line now holding unrelated code or
    nothing — is STALE, because the claim was about what the line SAID, and
    whatever it says now is not that. A file/line that cannot be read is
    STALE when it is definitively gone, UNAVAILABLE when git itself failed.
    Without a row ts there is no then to compare, and existence is all the
    evidence there is — said so in the detail, never silently upgraded."""
    out = []
    head = _head_sha(repo_root_path) or "-"[:12]
    filed_rev = _rev_at_ts(repo_root_path, filed_ts) if filed_ts else None
    for path, lineno in fls:
        now_text, err = _file_at_line(repo_root_path, path, lineno)
        if err:
            if "no such file" in err or "out of range" in err:
                out.append(("stale", "%s:%d — %s" % (path, lineno, err)))
            else:
                out.append(("unavailable", "%s:%d — %s" % (path, lineno, err)))
            continue
        stripped = now_text.strip()
        preview = stripped[:60] + ("…" if len(stripped) > 60 else "")
        if not filed_rev:
            out.append(("fresh", "%s:%d — %s (existence only: no row ts)"
                        % (path, lineno, preview)))
            continue
        then_text, terr = _file_at_rev(repo_root_path, filed_rev, path)
        if terr:
            out.append(("unavailable", "%s:%d — the filing-era tree is "
                        "unreadable: %s" % (path, lineno, terr)))
            continue
        then_lines = then_text.split("\n")
        if lineno > len(then_lines):
            out.append(("fresh", "%s:%d — %s (the line did not exist at "
                        "filing; it says this NOW)" % (path, lineno, preview)))
            continue
        then_line = then_lines[lineno - 1].strip()
        if then_line == stripped:
            out.append(("fresh", "%s:%d — unchanged since filing: %s"
                        % (path, lineno, preview)))
        else:
            out.append(("stale",
                        "%s:%d — the line CHANGED since the row was filed. "
                        "Then: %s | now: %s"
                        % (path, lineno,
                           (then_line[:40] or "(empty)"),
                           (stripped[:40] or "(empty)"))))
    return out, head


# ---------------------------------------------------------------------------
# sha re-verification — is the cited commit still on trunk?
# ---------------------------------------------------------------------------

def _sha_is_ancestor(root, sha):
    """True if sha is an ancestor of HEAD (i.e. the cited work landed)."""
    try:
        r = subprocess.run(
            ["git", "-C", root, "merge-base", "--is-ancestor", sha, "HEAD"],
            capture_output=True, timeout=5)
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return None


def _sha_patch_equivalent(root, sha):
    """True if the CHANGE in sha is on HEAD under a new object (git cherry).

    Ancestry alone false-strands on squash-merges: the fleet lands lanes by
    cherry-picking the gated commit onto trunk, which mints a NEW sha and
    leaves the reviewed object reachable from nothing. Measured premise
    (2026-08-03, fleet task #91's sibling): 37% of "not an ancestor" results
    on this repo are landed work wearing a new object. `git cherry` answers
    the CONTENT question — patch-id equivalence — and its +/- output is a
    fact, so this rung returns True/False/None like the ancestry one."""
    try:
        r = subprocess.run(
            ["git", "-C", root, "cherry", "HEAD", sha],
            capture_output=True, text=True, timeout=10)
        if r.returncode != 0:
            return None
        # cherry prints '-' for patch-equivalent (already upstream), '+' for not.
        marks = [ln[:1] for ln in r.stdout.splitlines() if ln[:1] in ("-", "+")]
        if not marks:
            return False
        return all(m == "-" for m in marks)
    except (OSError, subprocess.TimeoutExpired):
        return None


def _check_shas(shas, repo_root_path):
    """[(result, detail)] for each sha claim."""
    out = []
    for sha in shas:
        landed = _sha_is_ancestor(repo_root_path, sha)
        if landed is None:
            out.append(("unavailable", "%s — could not check ancestry"
                        % sha[:12]))
        elif landed:
            out.append(("fresh", "%s — is ancestor of HEAD" % sha[:12]))
        else:
            # THE CHERRY RUNG — ancestry said the object is unreachable, but
            # a squash-merged land gives the same change a NEW object, and
            # calling that NOT-on-trunk strands landed work (37% measured).
            equiv = _sha_patch_equivalent(repo_root_path, sha)
            if equiv is True:
                out.append(("fresh", "%s — not an ancestor, but the CHANGE is "
                            "on trunk under a new object (squash-landed)"
                            % sha[:12]))
            elif equiv is False:
                out.append(("stale", "%s — NOT on trunk by ancestry or "
                            "patch-identity" % sha[:12]))
            else:
                out.append(("unavailable", "%s — not an ancestor; "
                            "patch-identity unreadable" % sha[:12]))
    return out


# ---------------------------------------------------------------------------
# count re-verification — re-run the LEDGER count, never grep the tree
# ---------------------------------------------------------------------------

def _check_counts(counts):
    """[(result, detail)] for each count claim, against the LIVE dispatch
    ledger — a claim like "43 rows sit in CHANGES_REQUESTED" is a claim about
    ROWS, and only the ledger can answer it. The subject words are fuzzy by
    design: the check re-counts OPEN rows (the population every dispatch
    count claim is about) and compares. Off-by-a-little is reported as the
    number, not forced into fresh/stale — a count claim that moved from 43 to
    41 has not rotted the way a moved line has, and pretending otherwise is
    the same false precision the module exists to remove."""
    try:
        from . import dispatches
        rows = dispatches.rows()
    except Exception as e:                       # noqa: BLE001 — fail-open law
        return [("unavailable", "the dispatch ledger is unreadable: %s" % e)]
    open_n = sum(1 for r in rows.values()
                 if isinstance(r, dict) and r.get("status") == "open")
    out = []
    for n, subject in counts:
        if open_n == n:
            out.append(("fresh", "%d %s — the ledger still says %d open rows"
                        % (n, subject, open_n)))
        else:
            out.append(("stale", "%d %s — the ledger now says %d open rows "
                        "(claimed %d)" % (n, subject, open_n, n)))
    return out


# ---------------------------------------------------------------------------
# the re-measure verdict
# ---------------------------------------------------------------------------

STALE = "stale"          # at least one measurable claim has rotted
FRESH = "fresh"           # all measurable claims still hold
UNEVALUABLE = "unavailable"  # could not re-measure (git error, no repo, etc.)
UNKNOWN = "no-measurable-claims"


def re_measure(row, repo_path=None):
    """(verdict, detail_string) — is this row's evidence still true?

    verdict is one of STALE / FRESH / UNEVALUABLE / UNKNOWN.

    Called from the list renderer (lightweight — any subprocess cost is
    per-row) and from the triage surface before pickup.
    """
    repo_id = str(row.get("repo_id") or "")
    if repo_path is None:
        repo_path = repo_id
    # Normalise: repo_id is /path/to/repo/.git, git -C wants /path/to/repo
    if repo_path and os.path.basename(repo_path) == ".git":
        repo_path = os.path.dirname(repo_path)
    root = _repo_root(repo_path) if repo_path else None
    if not root:
        return UNKNOWN, "no git repository — cannot re-measure"
    note = str(row.get("note") or "")
    lane = str(row.get("lane") or "")
    fls, counts, shas = parse_claims(note, lane)
    if not fls and not shas and not counts:
        return UNKNOWN, "no measurable claims in row body"
    results = []
    head_short = "-"[:12]
    if fls:
        fl_results, head_short = _check_file_lines(
            fls, root, filed_ts=row.get("ts"))
        results.extend(fl_results)
    if shas:
        results.extend(_check_shas(shas, root))
    if counts:
        results.extend(_check_counts(counts))
    if not results:
        return UNKNOWN, "claims parsed but none re-measurable"
    stale = [r for r in results if r[0] == STALE]
    unavailable = [r for r in results if r[0] == UNEVALUABLE]
    if unavailable and not stale:
        return UNEVALUABLE, "%d unreachable; head %s" % (
            len(unavailable), head_short)
    if stale:
        return STALE, "%d STALE of %d claims at head %s: %s" % (
            len(stale), len(results), head_short,
            "; ".join(d for _, d in stale[:3]))
    return FRESH, "%d claims fresh at head %s" % (len(results), head_short)


def status_column(row, repo_path=None):
    """One-char display stamp for the list view: '✓' '✗' '?' '—' '·'.

    Only runs re-measure for OPEN rows (closed rows are done and their
    staleness is irrelevant).
    """
    from .dispatches import _open
    if not _open(row):
        return "—"
    verdict, _detail = re_measure(row, repo_path)
    chars = {FRESH: "✓", STALE: "✗", UNEVALUABLE: "?", UNKNOWN: "·"}
    return chars.get(verdict, "?")
