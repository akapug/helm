#!/usr/bin/env python3
"""Suggestion-only seat aliases — identity evidence that can REFUSE, never route.

A canonical chat seat is an operation operand. A pane/worktree label or owner
shorthand is only evidence explaining why another token looks familiar. This
module deliberately exposes no ``resolve_alias() -> seat`` API: callers can ask
whether they must refuse, but can never receive a replacement identity to pass
onward by accident.
"""
import os

from . import home


_ENV = "HELM_SEAT_ALIASES"
_ROLES = {"pane": "pane label", "owner": "owner shorthand"}
_RANK = {
    "orca-live-worktree": 0,
    "spawn-worktree": 1,
    "roster-worktree": 2,
    "declared-pane": 3,
    "declared-owner": 4,
}


def _valid(name):
    try:
        return home.validate_seat_arg(name) == name
    except home.SeatNameError:
        return False


def _declarations(raw):
    """([declaration], error). An explicitly malformed map fails loudly."""
    if raw is None or not raw.strip():
        return [], None
    out = []
    for item in raw.split(","):
        item = item.strip()
        try:
            left, canonical = item.split("=", 1)
            kind, alias = left.split(":", 1)
        except ValueError:
            return [], ("%s entry %r must be kind:alias=canonical"
                        % (_ENV, item))
        kind, alias, canonical = kind.strip(), alias.strip(), canonical.strip()
        if kind not in _ROLES:
            return [], ("%s entry %r has kind %r; use pane or owner"
                        % (_ENV, item, kind))
        if not _valid(alias) or not _valid(canonical):
            return [], ("%s entry %r must use 1-64 character seat tokens"
                        % (_ENV, item))
        out.append({"alias": alias, "canonical": canonical, "kind": kind})
    return out, None


def _canonical_sources(roster=None):
    """(roster, spawned, canonicals, paths) from existing identity owners.

    A caller that already sampled roster evidence passes that exact snapshot.
    ``None`` preserves the ordinary standalone alias path; an explicit empty
    dict means the caller looked and found no trustworthy roster rows, so this
    layer must not silently read a different world.
    """
    from . import orcaadopt, seats
    if roster is None:
        roster = seats.roster()
    spawned = orcaadopt.helm_spawned()
    canonicals = {name for name in list(roster) + list(spawned) if _valid(name)}
    paths = [seats.safe_cwd()]
    paths.extend(row.get("cwd") for row in roster.values()
                 if isinstance(row, dict) and row.get("cwd"))
    paths.extend(row.get("worktree") for row in spawned.values()
                 if isinstance(row, dict) and row.get("worktree"))
    return roster, spawned, canonicals, paths


def _registered_paths(alias, paths):
    """Exact Git registrations for deterministic ``seat/<alias>`` worktrees."""
    from . import harness, vcs
    roots = []
    for path in paths:
        root = harness.find_repo_root(path)
        if root and root not in roots:
            roots.append(root)
    found = []
    for root in roots:
        rows, err = vcs.backend(root).worktrees(root)
        if err:
            continue
        expected = os.path.realpath(harness.seat_worktree_path(root, alias))
        branch = "refs/heads/" + harness.seat_branch(alias)
        for row in rows:
            path = row.get("path")
            if path and os.path.realpath(path) == expected \
                    and row.get("branch") == branch:
                found.append(expected)
                break
    return sorted(set(found))


def _same_path(path, registered):
    return bool(path) and os.path.realpath(path) in registered


def _evidence(token, raw=None, roster=None):
    """(canonicals, evidence, config_error) for one exact alternate token.

    THE CONFIG ERROR IS AN ACCUSATION ABOUT THE OPERATOR'S SETUP, so it may
    only be made from a census that could see. This function used to read the
    process census twice and discard the blindness both times — `procs,
    _unreadable = claude_processes()` and `rows, _note = pane_rows()` — and
    then, if the declared alias target was missing from the names it had
    gathered, return "target %r is not an exact roster, spawn, or live seat".
    A seat whose only evidence was an unreadable /proc entry produced exactly
    that: `helm` telling a human their alias config is wrong when the truth was
    that helm could not look.

    @codex named this in round 8 of this lane and I dismissed it, because the
    consumer table's own rationale said a missing name makes the declaration
    FAIL and failing is the safe direction. Failing IS safe; failing WITH A
    DIAGNOSIS is not, and reading a declaration instead of running the function
    is the whole reason this lane needed a paired census."""
    from . import orcaadopt
    if roster is None:
        roster, spawned, canonicals, paths = _canonical_sources()
    else:
        roster, spawned, canonicals, paths = _canonical_sources(roster=roster)
    raw = os.environ.get(_ENV) if raw is None else raw
    declarations, err = _declarations(raw)
    if err:
        return canonicals, [], err

    # ONE census per call, taken LAZILY and remembered. Lazily because
    # `alias_refusal` sits on the DM/dispatch addressing path and a /proc walk
    # per addressed message is a real cost; once because two independent scans
    # can disagree with each other, and a function that reads the host twice
    # has two answers and no rule for which wins.
    taken = []

    def census():
        if not taken:
            procs, unreadable = orcaadopt.claude_processes()
            taken.append((procs, orcaadopt.cannot_look(
                unreadable, "which live seats exist, so it cannot say a "
                            "declared alias target is not one")))
        return taken[0]

    # A declaration may name a live process not yet represented by the tmpfs
    # roster or Helm spawn register. Read only the closed identity keys; never a
    # whole environ and never presentation text.
    if declarations and any(d["canonical"] not in canonicals for d in declarations):
        canonicals.update(p.get("seat") for p in census()[0]
                          if p.get("seat") and _valid(p.get("seat")))

    registered = _registered_paths(token, paths) if _valid(token) else []
    evidence = []

    def add(canonical, role, source):
        if canonical in canonicals and canonical != token:
            evidence.append({"canonical": canonical, "role": role,
                             "source": source})

    if registered:
        for canonical, row in roster.items():
            if isinstance(row, dict) and _same_path(row.get("cwd"), registered):
                add(canonical, "pane label", "roster-worktree")
        for canonical, row in spawned.items():
            if isinstance(row, dict) and row.get("seat") == canonical \
                    and _same_path(row.get("worktree"), registered):
                add(canonical, "pane label", "spawn-worktree")

        # This is the only live pane join: pane_rows assigns ``seat`` from the
        # process's HELM_CHAT_NAME + ORCA_PANE_KEY, not title/preview/handle.
        rows, _note = orcaadopt.pane_rows(procs=census()[0], unreadable=[])
        for row in rows:
            if _same_path(row.get("worktree"), registered):
                add(row.get("seat"), "pane label", "orca-live-worktree")

    for declaration in declarations:
        if declaration["alias"] != token:
            continue
        target = declaration["canonical"]
        if target not in canonicals:
            # BLIND: the target may be exactly the seat helm could not read, so
            # this cannot name the operator's config as the fault. Both answers
            # REFUSE the alias — the difference is entirely in whose fault the
            # human is told it is, which is the whole point.
            blind = census()[1]
            if blind:
                return canonicals, evidence, (
                    "%s target %r could not be checked — %s"
                    % (_ENV, target, blind))
            return canonicals, evidence, (
                "%s target %r is not an exact roster, spawn, or live seat"
                % (_ENV, target))
        if declaration["kind"] == "pane" and not registered:
            return canonicals, evidence, (
                "%s pane alias %r is not an exact registered seat worktree"
                % (_ENV, token))
        add(target, _ROLES[declaration["kind"]],
            "declared-" + declaration["kind"])
    return canonicals, evidence, None


def _best(rows):
    return sorted(rows, key=lambda row: (
        _RANK.get(row["source"], 99), row["role"], row["source"]))[0]


def alias_refusal(token, closed=False, fuzzy=(), raw=None, roster=None):
    """Return a refusal string for a known alias/ambiguity, else ``None``.

    ``closed`` permits a fuzzy hint only because the caller has already decided
    an unknown token cannot proceed (seat spawn/where/resume). Open-world DM and
    dispatch addressing never refuses a future seat merely for looking similar.
    The function never returns a canonical operation operand.
    """
    token = str(token or "")
    canonicals, evidence, err = _evidence(token, raw=raw, roster=roster)
    if token in canonicals:
        return None
    if err:
        return "seat aliases misconfigured: " + err
    targets = sorted(set(row["canonical"] for row in evidence))
    if len(targets) > 1:
        return ("seat name %r is ambiguous — it names %s; use an exact canonical "
                "seat" % (token, " and ".join(targets)))
    if targets:
        row = _best([item for item in evidence
                     if item["canonical"] == targets[0]])
        return ("unknown seat %r — did you mean %s? (%s is its %s)"
                % (token, targets[0], token, row["role"]))
    if closed:
        from . import cli
        hint = cli.suggest(token, sorted(set(fuzzy) | canonicals))
        if hint:
            return "unknown seat %r%s" % (token, hint)
    return None
