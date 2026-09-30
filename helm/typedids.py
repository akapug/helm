#!/usr/bin/env python3
"""What a typed id that resolved to nothing, or to more than one thing, could
have meant (task/3382). Every function here NAMES candidates and never binds
one: the refusal stays the resolver's, and this only makes it say which row
or commit the seat probably meant.

MEASURED over 40.5 h of the three local seats' transcripts: 85 of 4,866 hex
tokens they typed named nothing (qwenlocal 2.6 %), and the odd lengths were
bad 51-69 % of the time. The cause was one shape: `verdict` demanded 40 hex
while every listing prints 12, so a seat padded the 12 out to 40. The padded
token's first 12 still name the row or commit, so the refusal says which.

THE RESOLVERS STAY WHERE THEY ARE. `dispatches._resolve_row` resolves a row
id and `dispatches._resolve_tip` a commit; both call in here only on the path
that already refuses. This module reads their helpers at CALL TIME through
the module (`dispatches.NAME`), so a test that patches one reaches both.
"""
import os
import re

from . import dispatches

#: How many candidates a refusal lists before it counts the rest.
LISTED = 6
#: The shortest row id prefix `dispatches._ID` resolves.
ROW_PREFIX_MIN = 8
#: The shortest commit prefix `dispatches._resolve_tip` resolves.
TIP_PREFIX_MIN = 7


def listed_rows(rows):
    """The candidates of an ambiguous row prefix, each at the length that
    tells it from the others (never under the 12 a listing prints) beside
    its lane. A refusal that says only HOW MANY rows matched sends the seat
    to a listing to learn which."""
    rows = list(rows)
    ids = sorted(str(r.get("id") or "") for r in rows)
    width = max(12, len(os.path.commonprefix(ids)) + 1)
    lanes = {str(r.get("id") or ""): str(r.get("lane") or "-")[:40]
             for r in rows}
    shown = ", ".join("%s %s" % (i[:width], lanes[i]) for i in ids[:LISTED])
    more = len(ids) - LISTED
    return shown + (", and %d more" % more if more > 0 else "")


def near_rows(current, rid):
    """' — did you mean <id>?' when a token that named no row shares its
    longest prefix (at least ROW_PREFIX_MIN hex) with exactly one row, the
    rows sharing it when there are several, a note on what a short token
    lacks, or ''. An uppercase token is compared lowercased; it is still
    refused, because the resolver compares ids exactly."""
    token = str(rid or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]+", token):
        return ""
    if len(token) < ROW_PREFIX_MIN:
        return " — an id prefix is at least %d hex characters" % ROW_PREFIX_MIN
    best, near = ROW_PREFIX_MIN - 1, []
    for key in current:
        n = len(os.path.commonprefix((token, str(key))))
        if n > best:
            best, near = n, [key]
        elif n == best and n >= ROW_PREFIX_MIN:
            near.append(key)
    if len(near) == 1:
        return (" — did you mean %s? (its first %d characters are yours; a "
                "token that names no row is never resolved)" % (near[0], best))
    if near:
        return " — %d rows share its first %d characters: %s" % (
            len(near), best, listed_rows([current[k] for k in near]))
    return ""


def tip_hint(repo, token, known=()):
    """' — ...' saying what a typed commit id that resolved to nothing could
    mean in `repo`, or ''.

    AMBIGUOUS lists the objects the token prefixes. NO MATCH names the one
    commit that shares the token's longest prefix (at least TIP_PREFIX_MIN
    hex), so a 12-hex prefix padded with zeros to 40 is told which commit
    its first 12 name. `known` adds full ids the caller already holds (a
    row's dispatched tip), so the answer survives a repository this process
    cannot read. A candidate from git is named only once `_resolve_tip`
    peels it to itself as a commit."""
    token = str(token or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{%d,64}" % TIP_PREFIX_MIN, token):
        return ""
    known = [k for k in known if isinstance(k, str)
             and dispatches._FULL_TIP.fullmatch(k)]
    names = set(known)
    if repo:
        try:
            names.update(dispatches._disambiguate(repo,
                                                  token[:TIP_PREFIX_MIN]))
        except (OSError, dispatches.subprocess.TimeoutExpired):
            pass
    exact = sorted(n for n in names if n.startswith(token))
    if len(exact) > 1:
        return " — ambiguous: %d objects start with %s: %s%s" % (
            len(exact), token, ", ".join(exact[:LISTED]),
            ", and %d more" % (len(exact) - LISTED)
            if len(exact) > LISTED else "")
    if exact:
        return ""
    best, near = TIP_PREFIX_MIN - 1, []
    for name in names:
        n = len(os.path.commonprefix((token, name)))
        if n > best:
            best, near = n, [name]
        elif n == best:
            near.append(name)
    if best < TIP_PREFIX_MIN:
        return ""
    commits = [n for n in near if n in known or dispatches._resolve_tip(
        repo, n, infer_sha_branch=False)[0] == n]
    if len(commits) != 1:
        return ""
    return (" — %s names no commit; its first %d characters resolve to %s "
            "(type that, or any unique prefix of it)"
            % (token, best, commits[0]))
