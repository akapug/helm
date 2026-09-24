"""Does a round RE-RAISE an earlier finding, or find a NEW one?

THE GUARD COUNTED ROUNDS. `dispatches.review_spiral` counts distinct reviewed
tips and, at three, prescribes a MELD — converge every open finding in one
live exchange. That is the right cure for a chain that argues about ONE
defect across three rounds. It is the wrong cure for a chain where every
round found a defect the previous round's arms were structurally unable to
see: a meld converges OPEN findings, and there are none to converge.

MEASURED, on the `the-quota-page-puts-what-he-uses-on-top` chain: six
rounds read 1 finding, CLEAN, CLEAN, 4, 2, 1; the guard fired "meld" twice and
at each firing ZERO findings stood open. The honest reading was not a spiral —
the lane was UNDER-ARMED and was being armed, one generating cause being a
harness that made equal what production lets drift. Re-litigating one finding
across rounds and finding a new defect each round read IDENTICALLY, because
the only thing being counted was how many rounds there had been.

THE KEY IS DERIVED, NEVER DECLARED. A new required marker on `helm dispatch
verdict` is the obvious design and this file refuses it: the last required
review marker this tree shipped was adopted by 2.34% of the rows that owed it,
and a guard reading a field nobody writes is a guard that never fires. So the
key comes out of what reviewers ALREADY write — the structured
`worse_than_main_paths` a FIX/SUPERSEDE verdict carries, plus every
project-relative path token standing in the evidence prose.

THE KEY IS A PATH SET, NOT A PATH:LINE SET. Evidence carries both spellings
(`helm/web_quota.py:582` and a bare path) and `worse_than_main_paths` carries
only the path, so a key mixing the two granularities could never intersect
across its own two sources. A line number also MOVES under an unrelated edit,
which would report a re-raised finding as a new one — the exact confusion this
module exists to end. A `path:line` token contributes its path.

WHAT EACH BUCKET MEANS, and why UNKNOWN is its own:
  SPIRAL      the round's key intersects a key already raised on this chain.
  NEW-DEFECT  the round's key is disjoint from every earlier round's.
  UNKNOWN     no key could be derived. It counts toward NEITHER and is never
              folded into SPIRAL. A CLEAN round lands here by construction:
              it raises no finding, so it accuses no path — and a clean round
              is visible at all only because the source-clean hold records it.

THE DIRECTION OF THE DOUBT IS MELD. This module can only ever turn MELD into
UNDER-ARMED, and only when EVERY keyed round is disjoint from every earlier
one. Any repetition at all, any shortage of keyed rounds, any unreadable
round: the caller's own prescription stands untouched. The guard's charter is
not weakened; what it counts is sharpened.
"""

import re

SPIRAL = "SPIRAL"
NEW_DEFECT = "NEW-DEFECT"
UNKNOWN = "UNKNOWN"

# The prescription this module can mint. The caller ranks it, and the stop
# rung renders it as an ADVISORY: finding a new defect each round is the
# behaviour a review exists to produce, and a rung that walls a seat for
# healthy behaviour is switched off within a day (this tree's own lesson,
# the built-but-not-wired latch). The block still fires on every chain where
# one path comes back.
UNDER_ARMED = "UNDER-ARMED"
UNDER_ARMED_CURE = (
    "each round found a defect the previous arms could not see; lift the "
    "shipped function into the arms, never re-implement it in the harness "
    "(docs/MODULE_REGISTRIES.md); ask the author for the generating cause")

# A PATH TOKEN, and every clause of it is load-bearing against prose.
# The extension must be ALPHABETIC and at least two characters, because
# `0.2`, `2.34%`, `e.g.` and `i.e.` are not files and a version number
# entering a finding key would make two unrelated rounds intersect. The stem
# must hold a letter for the same reason. Directory segments are optional:
# reviewers write both `helm/web_quota.py` and a bare `web_quota.py:582`.
# THE TRAILING BOUNDARY IS NOT DECORATION — without it
# `tests.test_web_accounts` matched as `tests.test`, a token naming no file
# that two unrelated rounds would both have produced.
_PATH = re.compile(
    r"(?:[A-Za-z0-9_.\-]+/)*"
    r"[A-Za-z0-9_\-]*[A-Za-z][A-Za-z0-9_.\-]*"
    r"\.([A-Za-z]{2,6})"
    r"(?::\d+)?"
    r"(?![A-Za-z0-9_.\-/])")

# A DOTTED NAME IS NOT A PATH. `vcs.probe` and `helm.seat` are attribute
# references and reviewers write them constantly; admitting them would let two
# rounds that merely discussed one module read as one re-raised finding. So a
# token with NO directory separator is admitted only on a suffix this tree's
# reviewers actually cite. The set is CLOSED and cannot report what it admits;
# what makes closing it safe is the direction it fails in — an unlisted suffix
# yields no key, a keyless round is UNKNOWN, and UNKNOWN leaves the caller's
# MELD exactly as it was.
_SUFFIXES = frozenset((
    "py", "js", "ts", "sh", "md", "txt", "json", "jsonl", "html", "css",
    "yml", "yaml", "toml", "ini", "cfg", "part", "log", "rs", "go", "sql",
    "service", "timer", "conf", "env", "lock"))


def _paths_in(text):
    """Every project-relative path a blob of reviewer prose names.

    TWO SPELLINGS OF ONE FILE MUST INTERSECT. A reviewer writes
    `helm/web_quota.py` one round and `web_quota.py:582` the next, and as
    plain strings those are disjoint — so a re-raised finding would read as a
    new defect, which is the one direction this module may not fail in. The
    basename enters the key beside the full path. Two files sharing a basename
    in different directories therefore intersect too; that reads a new defect
    as a repeat, keeps MELD, and is the safe half of the trade.

    THE BOUND, stated because it is invisible in the output: a round whose
    evidence names only a document ABOUT the review (a verdict journal file)
    keys on that document, and each round writes its own. Such a chain reads
    as disjoint on prose alone. What stops it is the same thing that makes the
    key worth having — a FIX verdict that accuses a file carries
    `worse_than_main_paths`, and those do not move between rounds.
    """
    out = set()
    for match in _PATH.finditer(str(text or "")):
        path = match.group(0).split(":")[0].replace("\\", "/")
        # An absolute path names a box, never a tree, and `..` names nothing
        # comparable across two rounds. Both are the door's own rule for
        # `worse_than_main_paths`; a key derived from prose keeps it.
        parts = path.split("/")
        if path.startswith("/") or ".." in parts:
            continue
        if len(parts) == 1 and match.group(1).lower() not in _SUFFIXES:
            continue
        out.add(path)
        out.add(parts[-1])
    return out


def finding_key(rows):
    """frozenset of the paths ONE round accuses, across its fan-out.

    A round is a reviewed tip, and a tip can carry several reviewers' rows —
    that is cross-family fan-out, one round by this tree's own law. The union
    is the round's accusation; no row's paths are dropped for disagreeing with
    another's, because two reviewers naming two files found two things.
    """
    out = set()
    for row in rows or ():
        if not isinstance(row, dict):
            continue
        for value in (row.get("worse_than_main_paths") or ()):
            out |= _paths_in(value)
        out |= _paths_in(row.get("verdict_ref"))
    return frozenset(out)


def classify(keys):
    """Ordered round keys -> the bucket each round falls in.

    `keys` is one frozenset per round, OLDEST FIRST. The first keyed round is
    NEW-DEFECT by the rule's own words — its key is disjoint from every
    EARLIER round, of which there are none.
    """
    kinds, raised = [], set()
    for key in keys:
        if not key:
            kinds.append(UNKNOWN)
            continue
        kinds.append(SPIRAL if (key & raised) else NEW_DEFECT)
        raised |= set(key)
    return kinds


def round_keys(bucket):
    """The chain bucket's per-round keys, oldest round first.

    The bucket is `review_spiral`'s own already-folded state: `tips` maps a
    reviewed tip to when it was dispatched, `observations` maps that tip to
    every row recorded against it. Reading the caller's structure rather than
    re-folding the ledger keeps ONE reader of the rounds — a second fold is
    how two surfaces come to disagree about how many rounds there were.
    """
    tips = (bucket or {}).get("tips") or {}
    observations = (bucket or {}).get("observations") or {}
    return [finding_key(observations.get(tip) or ())
            for tip in sorted(tips, key=tips.get)]


def sharpen(bucket, rounds, block_rounds, prescription, evidence):
    """(prescription, evidence) — the caller's own, sharpened by the keys.

    ONLY MELD IS TOUCHED. FINISH is a convergence the typed observations
    already proved and this module measures nothing that could refute it.

    UNDER-ARMED needs `block_rounds` KEYED rounds, all mutually disjoint. Two
    rounds fire nothing here for the same reason two rounds do not block: two
    is the ordinary shape of a review, not a diagnosis.
    """
    if prescription != "MELD":
        return prescription, evidence
    kinds = classify(round_keys(bucket))
    keyless = kinds.count(UNKNOWN)
    if keyless:
        # SAID OUT LOUD, in both outcomes. A round whose finding identity
        # could not be derived is the reason a census does not add up, and a
        # reader who cannot see that reads the remaining counts as the whole.
        evidence = "%s; %d round(s) keyless — finding identity UNKNOWN" % (
            evidence, keyless)
    keyed = len(kinds) - keyless
    try:
        needed = int(block_rounds)
    except (TypeError, ValueError):
        return prescription, evidence
    if keyed < needed or SPIRAL in kinds:
        return prescription, evidence
    return UNDER_ARMED, ("%d rounds, %d disjoint finding path sets, none "
                         "re-raised: %s" % (int(rounds or keyed), keyed,
                                            UNDER_ARMED_CURE))
