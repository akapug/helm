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
  count      — a numeric claim about rows/things (``5 stalled rows``).
               A run of digits is a count only when it is not part of an
               identifier (``task/1945``, ``codex-7``) AND something names
               what was counted; see the two guards at _COUNT.
  sha        — a commit tip claim (40-char hex)

WHAT THIS READER CAN SEE, AND WHAT IT CANNOT — the honest reach, because the
refusal string used to overstate it. A dispatch row's re-measurable surface is
its NOTE and its LANE. It is NOT the dispatched message: `send()` stores only
``message_hash`` (blake2b-128 of the body), and the delivered text lives in the
chat store, which is tmpfs and whose ``dm-*`` rooms are excluded from the disk
journal by design (see `chat._journal_records`.emit). So the body is a one-way
hash over text that no durable store holds.

MEASURED ON THE LIVE LEDGER 2026-08-11 (2167 dispatch creations): 86 rows carry
a non-empty note, 2081 carry none, and 29 yield any measurable claim at all —
1.3%. Lane contributed a claim on ZERO rows. Of 2094 delivered events carrying a
``delivery_ref``, 9 still resolve against the whole live chat store: 0.4%. A
re-measure that reached for the body would therefore answer from RAM volatility
— the same row measurable before a reboot and unmeasurable after — which is a
worse property than silence, because the method would be unreproducible.

∴ UNKNOWN HERE MEANS UNREAD, NOT UNROTTED. It says this reader found no
file:line, count, or sha in the surfaces it can read; it does NOT say the row's
evidence is intact, and no consumer may take it as clean evidence. The refusal
string names which surface was empty (no note at all vs. a note that is prose),
because "no measurable claims" fired identically on 98.7% of rows and an
evidence column that is uniform by construction carries no information.

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
# claim parsing — extract measurable claims from a dispatch row's NOTE + LANE
# (never "the body": the dispatched message is a hash here, not text)
# ---------------------------------------------------------------------------

# A file:line reference: module.py:306, helm/module.py:306, tests/test_x.py:42
_FILE_LINE = re.compile(r"\b([\w_./-]+\.py):(\d+)\b")

# A count claim: "5 stalled rows", "18 total".
#
# TWO GUARDS, AND NEITHER SUBSUMES THE OTHER. "task/1945 open rows" carries a
# counting noun, so the counting-noun rule admits it and only the identifier
# rule refuses it. "codex-7 attestation claims" is refused by either rule
# alone. A parser with one of these guards is a parser that reads identifiers
# from a dispatch note as quantities.
#
# THE ASYMMETRY THAT SETS THE STRICTNESS. A magnitude this parser MISSES costs
# one unre-measured claim on a surface that already reports UNKNOWN for 98.7% of
# rows. A magnitude it wrongly ADMITS is compared against the live open-row
# count by _check_counts and published as "stale" — a rot verdict derived from a
# number that was never a quantity. The second failure is strictly worse, so
# both lists below are deliberately under-inclusive and both are open to
# extension by anyone who measures a real miss.

# GUARD ONE, the identifier rule, STATED AS AN ALLOW-LIST. A magnitude's digits
# must be preceded by NOTHING (string start), by WHITESPACE, or by an opening
# bracket or quote. Every other character — # : / . - @ % = ~ + & and any
# notation not yet invented — makes the digits part of a label, and a label is
# never a quantity. The allow-list is the load-bearing choice: a deny-list of
# known separators admits every sigil it does not enumerate, and the set of
# things this codebase prints before an identifier is open-ended.
_COUNT = re.compile(r"""(?<![^\s(\[{"'])(\d[\d,]*)\s+(\w[\w\s]{1,40})\b(?!\S*\d)""",
                    re.I)

# GUARD TWO, the counting-noun rule: a magnitude is a COUNT only when something
# in the sentence says what was counted. Either a counting noun appears in the
# subject run, or a counting word leads the digits.
#
# TIME UNITS ARE DELIBERATELY ABSENT. _check_counts answers every count claim by
# re-counting OPEN LEDGER ROWS, so "held 945 minutes" admitted as a count would
# be published as "945 minutes — the ledger now says 165 open rows", which is a
# rot verdict about a duration. A noun belongs here only if a row population is
# a sane thing to compare it against.
_COUNTING_NOUNS = frozenset("""
    row rows claim claims test tests failure failures error errors
    file files commit commits line lines lane lanes dispatch dispatches
    seat seats arm arms entry entries item items match matches hit hits
    task tasks premise premises gate gates run runs case cases
    verdict verdicts message messages
    total totals
""".split())

_COUNT_LEAD = frozenset("""
    count counts counted total only just all exactly remaining
""".split())

_WORD = re.compile(r"[a-z]+")


def _is_count(text, m):
    """True when match `m` in `text` is a MAGNITUDE and not a bare number.

    Derived from the match, never transcribed: the subject is read from the
    match's own second group and the lead word from the text immediately
    before the match, so a regex change moves this check with it.
    """
    if _COUNTING_NOUNS.intersection(_WORD.findall(m.group(2).lower())):
        return True
    lead = _WORD.findall(text[:m.start()].lower())
    return bool(lead) and lead[-1] in _COUNT_LEAD

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
              for m in _COUNT.finditer(clean) if _is_count(clean, m)]
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


def _file_at_line(root, path, lineno, rev):
    """(line_text, err) — the text at lineno in file at REV, or None.

    REV IS A RESOLVED SHA AND NEVER THE WORD "HEAD", which is the whole point
    of the parameter. Dereferencing the symbolic ref here re-asks git "what is
    trunk now" at read time, while the verdict's scope was resolved earlier —
    so a fold landing between the two makes the label name one tree and the
    measurement read another, and nothing in the output can show the gap. The
    caller resolves once and binds; this function is handed the answer.

    Tries the path as-is, then resolves bare filenames by searching the tree
    (common in dispatch notes: ``dispatches.py:306`` without the helm/ prefix).
    """
    try:
        r = subprocess.run(
            ["git", "-C", root, "show", rev + ":" + path],
            capture_output=True, text=True, timeout=5)
        if r.returncode != 0:
            # Bare filename fallback: try helm/<path>, tests/<path>
            if "/" not in path:
                for prefix in ("helm/", "tests/"):
                    r2 = subprocess.run(
                        ["git", "-C", root, "show", rev + ":" + prefix + path],
                        capture_output=True, text=True, timeout=5)
                    if r2.returncode == 0:
                        r = r2
                        break
                else:
                    return None, "no such file in the measured tree"
            else:
                return None, "no such file in the measured tree"
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


def _rev_at_ts(root, ts, head):
    """The last commit at or before ts, or None — the row's filing-era tree.

    THE IMPLICIT HEAD, which is the one that survived the first cure. Every
    OTHER rung named "HEAD" in its argv and was found by grepping for it; this
    one ran `git log -1 --before=... --format=%H` with NO REVISION AT ALL, and
    git then dereferences the moving HEAD by default. A search for the string
    could never have found it, because the defect is the ABSENCE of an argument.

    A probe, round 4: bind head=ALPHA, move HEAD to BETA, and the
    file-line rung reported STALE "Then: BETA | now: ALPHA" — the filing-era
    baseline was walked from a tree the verdict never names, so the comparison
    ran between two different histories and blamed the row for the difference.
    """
    if not ts:
        return None
    try:
        r = subprocess.run(
            ["git", "-C", root, "log", "-1", "--before=" + str(ts),
             "--format=%H", head], capture_output=True, text=True, timeout=5)
        return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() \
            else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def _check_file_lines(fls, repo_root_path, head, filed_ts=None):
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
    # THE SCOPE ARRIVES BOUND; THIS FUNCTION NO LONGER RESOLVES ITS OWN. It used
    # to call _head_sha again, which made the label and the reads two separate
    # questions to a moving ref — measured on the dispatched tip:
    # re_measure captured one HEAD for the printed scope while this path
    # dereferenced HEAD again per file, so a concurrent fold labels tree X over
    # measurements taken from tree Y and the output cannot show the seam.
    filed_rev = (_rev_at_ts(repo_root_path, filed_ts, head)
                 if filed_ts else None)
    for path, lineno in fls:
        now_text, err = _file_at_line(repo_root_path, path, lineno, head)
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
    return out


# ---------------------------------------------------------------------------
# sha re-verification — is the cited commit still on trunk?
# ---------------------------------------------------------------------------

def _sha_is_ancestor(root, sha, head):
    """True if sha is an ancestor of HEAD (i.e. the cited work landed).

    HEAD arrives as a resolved sha for the same reason _file_at_line takes one:
    the verdict prints ONE scope, so every rung under it must be asked about
    that same tree. A fold between the label and this call would otherwise
    answer about a trunk the reader was never shown."""
    try:
        r = subprocess.run(
            ["git", "-C", root, "merge-base", "--is-ancestor", sha, head],
            capture_output=True, timeout=5)
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return None


def _sha_patch_equivalent(root, sha, head):
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
            ["git", "-C", root, "cherry", head, sha],
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


def _check_shas(shas, repo_root_path, head):
    """[(result, detail)] for each sha claim, all against ONE bound tree."""
    out = []
    for sha in shas:
        landed = _sha_is_ancestor(repo_root_path, sha, head)
        if landed is None:
            out.append(("unavailable", "%s — could not check ancestry"
                        % sha[:12]))
        elif landed:
            out.append(("fresh", "%s — is ancestor of the measured tree"
                        % sha[:12]))
        else:
            # THE CHERRY RUNG — ancestry said the object is unreachable, but
            # a squash-merged land gives the same change a NEW object, and
            # calling that NOT-on-trunk strands landed work (37% measured).
            equiv = _sha_patch_equivalent(repo_root_path, sha, head)
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

# THE NOUNS WHOSE POPULATION THIS CHECK CAN ACTUALLY ANSWER ABOUT. The
# counting-noun list one screen up decides what is a COUNT; this decides what
# is a count OF DISPATCH ROWS, which is the only thing `_check_counts` can
# re-count. The comment beside that list already states the rule — "a noun
# belongs here only if a row population is a sane thing to compare it
# against" — and TIME UNITS were excluded for exactly this reason. The list
# then grew past its own rule: tests, failures, errors, files, commits,
# lines, matches, hits, arms, cases, gates, runs and premises are all real
# countable things and none of them is a dispatch-row population.
#
# SPLIT RATHER THAN NARROWED, because the two questions are genuinely
# different and an arm in tests/test_clearspan.py deliberately pins the
# parse: "0 proof matches" IS a count claim about a real countable thing.
# Removing `matches` from the counting nouns would make the parser wrong to
# make this function right.
#
# MEMBERSHIP IS SETTLED BY A COUNT, NEVER BY A READING. Nouns that FEEL like
# rows — tasks, lanes, items, entries, claims — each name a DIFFERENT live
# ledger, and the numbers diverge by an order of magnitude: open dispatch
# rows are in the low hundreds while open task rows are in the thousands and
# live store entries higher still. Admitting one of them answers "N open
# tasks" against the dispatch count and publishes `stale`: the same rot
# verdict from the same wrong population this check exists to remove. Before
# adding a noun here, count its population and the dispatch rows on the same
# day; if the two numbers are not the same quantity, it does not belong.
#
# `total` belongs to none of those arguments: it is not a noun naming a
# population at all, it is a quantifier that can modify any subject, so one
# word re-opens the whole class.
_ROW_POPULATION = frozenset("""
    row rows dispatch dispatches
""".split())

# THE HEAD NOUN IS WHAT NAMES THE POPULATION, and presence anywhere in the
# subject is not headship. A set intersection admits "12 commits touching
# dispatch" and "5 test files under tasks" — both counts of something else
# that merely MENTION a row word. The number sits immediately before its
# noun phrase, so the FIRST counting noun in the subject run is inside that
# phrase and everything past it is postmodifier: "dispatch rows" heads on
# `dispatch`, "commits touching dispatch" heads on `commits`. Derived from
# the union so a row-population noun that is not also a counting noun is
# still findable here.
_SUBJECT_NOUNS = _COUNTING_NOUNS | _ROW_POPULATION


def _head_noun(subject):
    """The first counting noun in `subject`, or None when it names none.

    A subject with no counting noun reached _check_counts through the
    leading-word rule ("all 43 remaining"), which says a magnitude was
    counted and NOT what was counted — so there is no population to compare
    against and the caller must not invent one."""
    for word in _WORD.findall(str(subject).lower()):
        if word in _SUBJECT_NOUNS:
            return word
    return None


def _counts_rows(subject):
    """True when the HEAD noun of `subject` names a population of dispatch rows.

    Derived from the subject the parser handed back, never transcribed: the
    words are read out of that string, so a change to what the parser
    captures moves this check with it."""
    return _head_noun(subject) in _ROW_POPULATION


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
        if not _counts_rows(subject):
            # A COUNT THIS FUNCTION CANNOT ANSWER IS NOT RE-MEASURED, AND IT
            # MUST NOT BE GIVEN A VERDICT. The only population available here
            # is OPEN LEDGER ROWS, so comparing "0 proof matches" against it
            # published `stale` — a ROT VERDICT derived from a number that was
            # never that quantity. Every disposition surface reads the
            # VERDICT; the prose beside it naming both populations is not what
            # they act on.
            #
            # DROPPED RATHER THAN MARKED UNAVAILABLE, and the difference
            # matters at the composition one function up: `unavailable and not
            # stale` short-circuits the whole row, so marking it would take a
            # row whose FILE-LINE claims are genuinely fresh and report it as
            # unreachable. A claim this check has no instrument for is not a
            # failed measurement, it is an absent one — and the caller already
            # has the honest answer for a row with nothing left to measure.
            #
            # THE SKIP IS DISCLOSED, NEVER SILENT: the caller counts what came
            # back against what it sent and names the difference in the method
            # label, because a check that quietly declines the hardest inputs
            # reads exactly like one that passed them.
            #
            # THE PARSE IS UNTOUCHED. A zero IS a measurement and a countable
            # subject IS a count; what changes is that this function stops
            # answering questions it was never asked.
            continue
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

# THE READER'S OWN VERSION, AND IT IS THE ERROR-CORRECTION CHANNEL'S HINGE.
# A stored verdict is only as good as the reader that produced it, and tonight's
# council turned on exactly that: recording a derived fact once, with no way to
# tell WHICH reader decided it, freezes that reader's bugs forever — nothing
# later can disagree. Every derived answer here names the version that produced
# it, so a consumer holding an old answer can tell it predates a fix instead of
# trusting it indefinitely.
#
# BUMP THIS WHENEVER THE SEMANTICS MOVE, not when the code is merely edited: a
# new claim kind, a changed verdict rule, a different notion of "rotted". v2 is
# the head-scope fix — every verdict now names the tree it measured against,
# where before only file-line claims did.
#
# v3 IS A CHANGED VERDICT RULE TWICE OVER, which is why it is a bump and not an
# edit. (a) UNKNOWN's assertion narrowed: it used to be published as "nothing
# measurable could have rotted" and now says only "this reader found nothing
# measurable in the surfaces it can read" — the same bytes, a strictly weaker
# claim, so a stored v2 UNKNOWN must not be read as a v3 one. (b) An
# unresolvable repository moved from UNKNOWN to UNEVALUABLE: cannot-look was
# wearing found-nothing, which is the same disease one line up.
RE_MEASURE_VERSION = 3


def _vtag():
    """The reader-version stamp every verdict carries, including UNKNOWN ones.

    UNKNOWN NEEDS THIS MOST, which is not obvious and is why it is spelled out.
    "no file:line, count, or sha" is a statement about THIS READER's parser and
    THIS READER's reach, not about the row — a later version that understands a
    new claim kind, or that can read a surface this one cannot, would answer
    differently on identical bytes. An unstamped UNKNOWN is therefore the one
    verdict a consumer would most wrongly treat as settled.
    """
    return "v%d" % RE_MEASURE_VERSION

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
    # THE FREE ANSWER COMES FIRST, and it used to come second. `_repo_root`
    # shells out to `git rev-parse --show-toplevel` — 0.0024s over 200 spawns
    # measured 2026-08-11 — and it ran BEFORE the parse that decides whether a
    # toplevel is wanted at all. On the live ledger that same day it is 2138 of
    # 2167 dispatch creations buying a lookup they discard: ~5.1s of subprocess
    # for an answer the parser already held, paid on the LIST path, per row,
    # per render. Nothing between the two reads `root`, so the reorder is
    # behaviour-preserving on every row that HAS a claim.
    note = str(row.get("note") or "")
    lane = str(row.get("lane") or "")
    fls, counts, shas = parse_claims(note, lane)
    if not fls and not shas and not counts:
        # IT NAMED A BODY IT NEVER READ. This said "no measurable claims in
        # row BODY", and the body — the dispatched message — is not one of
        # the two surfaces parsed above and is not reachable from here at
        # all: `send()` keeps a blake2b-128 of it and nothing else, and the
        # chat store that held the text is tmpfs whose dm-* rooms the disk
        # journal excludes by design. So the sentence asserted a read that
        # never happened, and downstream ("claims no-measurable-claims" in
        # every stale-bot KEEP line) inherited the overstatement.
        #
        # It also fired identically on 2138 of 2167 rows, which makes it
        # uniform BY CONSTRUCTION — an evidence column that cannot vary
        # cannot discriminate, so a digest built on it says nothing. The two
        # populations it merged are genuinely different and cheap to tell
        # apart: 2081 rows recorded no note at all (nothing was ever written
        # down to re-measure) versus 57 whose note is prose (a human wrote
        # evidence and none of it is machine-checkable). Naming which one
        # is the whole gain here.
        return UNKNOWN, ("%s, and the dispatched message text is stored "
                         "nowhere (hash only) — UNREAD, not unrotted [%s]"
                         % ("this row records no note"
                            if not note.strip() else
                            "note and lane hold no file:line, count, or sha",
                            _vtag()))
    repo_id = str(row.get("repo_id") or "")
    if repo_path is None:
        repo_path = repo_id
    # Normalise: repo_id is /path/to/repo/.git, git -C wants /path/to/repo
    if repo_path and os.path.basename(repo_path) == ".git":
        repo_path = os.path.dirname(repo_path)
    root = _repo_root(repo_path) if repo_path else None
    if not root:
        # CANNOT-LOOK IS NOT FOUND-NOTHING, and this returned UNKNOWN — the
        # verdict spelled "no-measurable-claims" — for a repository it could
        # not resolve. The module docstring already promised 'unavailable'
        # here; `_head_sha`'s failure four rungs down already answers that
        # way for the identical reason. The reorder above sharpens the
        # contradiction into a flat one: every row that now reaches this
        # line has ALREADY had its claims parsed, so telling it that it has
        # none contradicts the count in this very string.
        return UNEVALUABLE, ("no git repository — cannot re-measure the %d "
                             "claim(s) this row does carry [%s]"
                             % (len(fls) + len(counts) + len(shas), _vtag()))
    results = []
    # THE VERDICT NAMES THE SCOPE IT WAS MEASURED AGAINST, ALWAYS — and until
    # now it did so only for FILE-LINE claims. This was seeded with a literal
    # placeholder and overwritten solely inside the file-lines branch, so a row
    # whose claims are SHAS or COUNTS produced "…at head -": an accusation that
    # a recorded claim has rotted, with no statement of the tree it rotted
    # against. Live specimen on the ledger tonight: "1 STALE of 1 claims at
    # head -: 17 orphaned — the ledger now says 52 open rows (claimed 17)".
    #
    # A re-measurement IS a derived fact, so it owes the same scope every other
    # derived fact owes: WHICH tree answered. Without it the reader cannot tell
    # a claim that rotted from one measured against a different HEAD than they
    # are looking at, and cannot re-run the measurement to check. `_head_sha`
    # already existed and _check_file_lines was already calling it — the value
    # was simply never resolved on the other two paths.
    #
    # SLICE THREE MADE THAT SCOPE *BOUND* RATHER THAN MERELY PRINTED, which is
    # a finding on the dispatched tip and a strictly deeper defect than
    # the one above. Naming a tree is worth nothing if the rungs then ask git
    # "what is trunk NOW" a second time: HEAD was dereferenced afresh inside the
    # file-line reads, the ancestry rung and the cherry rung, so a fold landing
    # mid-measurement labels tree X over readings taken from tree Y — and every
    # individual answer is honest, which is what makes the composite unfalsifiable.
    # One resolution, threaded down, and the label IS the tree by construction.
    head = _head_sha(root)
    if not head:
        # AND AN UNREADABLE HEAD IS NOT A SCOPE OF "-". The old seed let a
        # repo whose HEAD could not be resolved fall through to a confident
        # FRESH/STALE from the count rungs, which need no tree at all — a
        # verdict about trunk from a reader that could not find trunk. Cannot-
        # look is its own answer and it is this one.
        return UNEVALUABLE, "HEAD unresolvable — no tree to measure against [%s]" % _vtag()
    head_short = head[:12]
    if fls:
        results.extend(_check_file_lines(
            fls, root, head, filed_ts=row.get("ts")))
    if shas:
        results.extend(_check_shas(shas, root, head))
    skipped_counts = 0
    if counts:
        answered = _check_counts(counts)
        # A CHECK THAT QUIETLY DECLINES ITS HARDEST INPUTS READS EXACTLY LIKE
        # ONE THAT PASSED THEM. `_check_counts` answers only counts of
        # dispatch rows, because open rows are the only population it can
        # re-count; every other count is dropped rather than given a verdict.
        # The difference is measured HERE, against what was sent, so the
        # method label can say it — an undisclosed skip is the same failure
        # as the false verdict it replaced, one surface quieter.
        skipped_counts = len(counts) - len(answered)
        results.extend(answered)
    if not results:
        # AND IT SAYS WHICH KIND WENT UNMEASURED. A row whose only claim was a
        # count of something that is not dispatch rows lands here, and "none
        # re-measurable" alone would leave a reader unable to tell that from a
        # parse that produced nothing at all.
        return UNKNOWN, ("claims parsed but none re-measurable%s [%s]"
                         % ("; %d count(s) name no population this check can "
                            "answer about" % skipped_counts
                            if skipped_counts else "", _vtag()))
    stale = [r for r in results if r[0] == STALE]
    unavailable = [r for r in results if r[0] == UNEVALUABLE]
    # THE METHOD IS NAMED, NOT IMPLIED. "3 claims fresh" does not say WHICH
    # instruments answered, so a reader cannot tell a verdict that checked
    # ancestry from one that only counted rows — and those decay differently.
    # Naming the kinds that actually ran, with the reader version that ran
    # them, is what lets a stored answer be re-derived rather than merely
    # believed. Scope (head) + method (kinds) + version = the whole record.
    method = "%s %s" % (_vtag(),
                        "+".join(k for k, on in (
                            ("file-lines", bool(fls)),
                            ("shas", bool(shas)),
                            ("counts" + ("(%d not a row population)"
                                         % skipped_counts
                                         if skipped_counts else ""),
                             bool(counts))) if on))
    if unavailable and not stale:
        return UNEVALUABLE, "%d unreachable; head %s [%s]" % (
            len(unavailable), head_short, method)
    if stale:
        return STALE, "%d STALE of %d claims at head %s [%s]: %s" % (
            len(stale), len(results), head_short, method,
            "; ".join(d for _, d in stale[:3]))
    return FRESH, "%d claims fresh at head %s [%s]" % (
        len(results), head_short, method)


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
