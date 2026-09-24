#!/usr/bin/env python3
"""Symbols a lane REMOVES from trunk, named out loud before the gate spends.

THE INCIDENT, measured 2026-08-04 and the reason this exists. A rebase
auto-merge dropped two live trunk functions (`_recipient_fields`,
`_recipient_label`) out of a lane: the file reported M, there were NO conflict
markers, and every suite was green — because the lane's own tests never touched
them and the suites that did still passed against what remained. That lane
would have gated, been reviewed, and LANDED a silent deletion of code nobody
intended to remove. Third member of that day's silent-regression family.

WHY THIS CANNOT BE A PRE-COMMIT RUNG, which is where every other scanner here
lives. The deletion never appears in a commit the author WROTE — it arrives
during a rebase, inside a merge git performed itself. A rung reading the staged
diff is structurally blind to it. The lane-versus-trunk comparison is the only
view that can see it, so this runs where that view exists: before the gate.

WHY IT WARNS AND NEVER REFUSES, and the number is the argument. Measured over
the LIVE lane population the day it was written: 96 unlanded lanes, and this
predicate fires on 22 of them — 23%. Most are ordinary and intentional (a
renamed test, a helper deliberately dropped), because git cannot see INTENT: a
symbol removed on purpose and a symbol eaten by a merge produce byte-identical
diffs. A guard that refuses a quarter of all lanes is one an author disables
wholesale within a day, and a disabled guard catches nothing — the lesson the
docref rung taught the same night, when it refused a token whose only legal
cure the registry could not express.

So the contract is NAMING, not judgement. The author is the only party who
knows whether they meant it, and they can tell instantly from the name: seeing
`_recipient_fields` in a list of what your lane removes, when you never touched
it, is unmissable. Silence is the only forbidden outcome.

A RENAME IS NOT A DELETION. A symbol that leaves while the same name arrives in
the same lane is excluded — otherwise every moved function reads as a loss and
the signal drowns in its own noise.
"""
import re

# `-def foo` / `-class Foo` / `-async def foo`, at any indent, in a unified
# diff. Kept deliberately narrow: a name that appears here is one a reader can
# grep for and settle in seconds, which is the entire value of the warning.
_DEL = re.compile(r"^-\s*(?:async\s+)?(def|class)\s+([A-Za-z_][A-Za-z0-9_]*)")
_ADD = re.compile(r"^\+\s*(?:async\s+)?(def|class)\s+([A-Za-z_][A-Za-z0-9_]*)")
_FILE = re.compile(r"^\+\+\+ b/(.+)$")


def parse(diff):
    """[(path, kind, name)] for symbols the diff REMOVES and does not re-add.

    Pure over text so the incident shape is a fixture and not a repository —
    the reason this half is separate from the git call below."""
    removed, added, path, out = [], set(), "?", []
    for line in (diff or "").splitlines():
        m = _FILE.match(line)
        if m:
            path = m.group(1)
            continue
        m = _DEL.match(line)
        if m:
            removed.append((path, m.group(1), m.group(2)))
            continue
        m = _ADD.match(line)
        if m:
            added.add(m.group(2))
    seen = set()
    for path, kind, name in removed:
        if name in added or (path, name) in seen:
            continue
        seen.add((path, name))
        out.append((path, kind, name))
    return out


NOT_APPLICABLE = "NOT_APPLICABLE"


def scan(repo, base=None, tip="HEAD"):
    """([(path, kind, name)], err) — what this lane removes relative to trunk.

    THE TRUNK NAME COMES FROM THE SEAM, never a literal. The first cut of this
    hardcoded "origin/main" and printed a multi-line git error on every gate in
    every repository without that ref — which is every test fixture, and every
    LOCAL-audience repository with no remote at all. `trunk_ref` already
    resolves origin/<base> when a remote exists and falls back to the local
    name when it does not, which is the same question this needs answered.

    THREE-DOT, deliberately: `base...tip` diffs the MERGE BASE to the tip, so
    it shows only what the lane did and never mistakes trunk's later additions
    for the lane's deletions. Two-dot would accuse every lane of removing
    everything that landed after it branched.

    Through the vcs seam, never a raw subprocess — tests/test_vcs.py pins the
    number of direct git spawns outside vcs.py. Trouble reads as an error and
    NEVER as a clean scan: a warning that silently stops warning is the failure
    mode this whole rung exists to end. The ONE exception is a base that does
    not resolve at all — there is no lane-versus-trunk question to answer, so
    that returns NOT_APPLICABLE and the caller stays quiet instead of printing
    a git error into every unrelated gate."""
    try:
        from . import vcs
        be = vcs.backend(repo)
    except Exception as e:                                  # noqa: BLE001
        return None, "deletion rung: no vcs backend (%s)" % e
    try:
        if base is None:
            base = be.trunk_ref(repo)
        if be.text(repo, "rev-parse", "--verify", "-q",
                   str(base) + "^{commit}")[0] != 0:
            return None, NOT_APPLICABLE
        rc, out, err = be.text(repo, "diff", "%s...%s" % (base, tip),
                               "--unified=0", "--no-color")
    except Exception as e:                                  # noqa: BLE001
        return None, "deletion rung: git failed (%s)" % e
    if rc != 0:
        return None, ("deletion rung: could not diff %s...%s (%s)"
                      % (base, tip, (err or "").strip()[:120]))
    return parse(out), None


def report(rows):
    """The operator-facing lines, or [] when the lane removes nothing.

    Names the SYMBOL and its FILE on one line each, because the author settles
    each in seconds by recognition and needs no further tooling to do it."""
    if not rows:
        return []
    lines = ["[helm deletion-rung] this lane REMOVES %d symbol(s) that are on "
             "trunk — confirm each was YOURS to remove:" % len(rows)]
    for path, kind, name in rows[:20]:
        lines.append("    %-6s %-34s %s" % (kind, name, path))
    if len(rows) > 20:
        lines.append("    ... and %d more" % (len(rows) - 20))
    lines.append("    A rebase auto-merge can delete trunk code with no "
                 "conflict markers and a green suite; that is what this names.")
    return lines
