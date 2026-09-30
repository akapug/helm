#!/usr/bin/env python3
"""helm team — the verb over `helm.teams` (task/3156).

    helm team [<project>] [--json]
    helm team set <project> --expect V [--add SEAT:ROLE[:FAMILY]]
                  [--remove SEAT] [--role SEAT:ROLE] [--share FAM=PCT]
                  --reason TEXT [--apply]
    helm team seed [--apply]
    helm team history <project> [--json]
    helm team capacity [<family> <lanes|unset> --reason TEXT [--apply]]
                       [--json]

GUI FIRST, AND THIS IS THE SAME DOOR. The owner edits teams on the web card;
an agent runs this verb on his behalf from chat. Both reach `teams.write`, so
the page refuses what the verb refuses, in the same words, and every write
records who made it (`by`: the seat name here, "owner" on the web).

A WRITE IS A DRY RUN until `--apply`, like `helm projects state`, and it is
COMPARE-AND-SET: `--expect` is the version you were looking at (0 for a
proposed team), and a team that moved since answers stale and writes nothing.

`capacity` RECORDS A MEASUREMENT: how many concurrent lanes a local family
serves, written by the seat that measured it, with how. Every team's slot
share of that family is a share of this number; until it is recorded the
family reads "capacity not measured".
"""
import json
import sys
import time

# AT MODULE SCOPE, so the dispatch sweep's branch probes can name the call
# each subverb must never reach on junk (`teams.write`, `teams.seed`, ...;
# tests/test_dispatch_honest.py EXEMPT). The verb is loaded lazily by
# `cli.VERBS`, so nothing here costs another verb's start.
from . import registry, teams

USAGE = ("helm team [<project>] [--json] | team set <project> --expect V "
         "[--add SEAT:ROLE[:FAMILY]] [--remove SEAT] [--role SEAT:ROLE] "
         "[--share FAM=PCT] --reason TEXT [--apply] | team seed [--apply] | "
         "team history <project> [--json] | team capacity [<family> "
         "<lanes|unset> --reason TEXT [--apply]] [--json]")

# Every flag `set` takes, and whether it repeats. `--add`, `--remove`,
# `--role` and `--share` may each be given as often as there are edits.
_SET_VALUED = {"--expect": False, "--reason": False, "--add": True,
               "--remove": True, "--role": True, "--share": True}


def _when(ts):
    if not isinstance(ts, (int, float)):
        return "(no time recorded)"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def _parse(args, valued, flags):
    """({flag: value or [values]}, [positional], problem)."""
    opts, pos, i = {}, [], 0
    while i < len(args):
        a = args[i]
        if a in valued:
            if i + 1 >= len(args) or args[i + 1].startswith("--"):
                return None, None, "%s wants a value" % a
            if valued[a]:
                opts.setdefault(a, []).append(args[i + 1])
            elif a in opts:
                return None, None, "%s given twice" % a
            else:
                opts[a] = args[i + 1]
            i += 2
            continue
        if a in flags:
            opts[a] = True
        elif a.startswith("-"):
            return None, None, "unknown flag %s" % a
        else:
            pos.append(a)
        i += 1
    return opts, pos, None


def _by():
    from . import home
    try:
        return home.chat_name() or ""
    except ValueError as exc:        # a seat name this helm will not record
        raise SystemExit("helm team: %s" % exc)


def _state_word(world, seat):
    placed = (world.get("seats") or {}).get(seat)
    if seat not in (world.get("roster") or {}) or not placed:
        return "wanted"
    return placed.get("presence") or "absent"


def render(key, rec, world):
    """The lines one project's team prints."""
    team = rec["team"]
    lit = rec.get("light") or {}
    if team["authored"]:
        head = "helm team %s — team v%d, set by %s %s: %s" % (
            key, team["v"], team["by"] or "an unnamed terminal",
            _when(team["ts"]), team["reason"] or "no reason recorded")
    else:
        head = ("helm team %s — PROPOSED from today's seats (binds nothing "
                "until accepted: `helm team seed --apply`, or `helm team set "
                "%s --expect 0 ... --apply`)" % (key, key))
    out = [head]
    if team.get("problem"):
        out.append("  NOTE — %s" % team["problem"])
    if rec.get("failed"):
        # A READER THAT RAISED IS FAILED, NEVER "unmeasured" (design read D6)
        out.append("  readings FAILED: %s — a reading that raised is not "
                   "unmeasured; the lines below say FAILED where it mattered"
                   % teams.failed_text(rec["failed"]))
    out.append("  light %s%s%s" % (
        lit.get("colour") or "?",
        " (set by %s)" % lit["by"] if lit.get("authored") and lit.get("by")
        else "" if lit.get("authored") else " (what the scan saw)",
        ": %s" % lit["reason"] if lit.get("reason") else ""))
    if not team["members"]:
        out.append("  members: none")
    for m in team["members"]:
        out.append("  %-9s %-28s %-10s %s" % (m["role"], m["seat"],
                                              m["family"],
                                              _state_word(world, m["seat"])))
    shares = team.get("shares") or {}
    out.append("  shares: %s" % (", ".join("%s %d%%" % kv for kv in
                                            sorted(shares.items()))
                                 or "none"))
    alloc = rec.get("allocation") or {}
    if alloc:
        out.append("  budgets%s:" % ("" if rec["binding"] else
                                     " (a PREVIEW — this team is proposed)"))
        for fam, row in sorted(alloc.items()):
            out.append("    " + teams.line(key, fam, row))
            if row.get("shared"):
                out.append("      shared, attributed to no project: %s"
                           % ", ".join(row["shared"]))
            if row.get("over_promised"):
                out.append("      over-promised: %s shares add to %d%%, so "
                           "every budget is scaled down"
                           % (fam, row["shares_total"]))
    drift = rec.get("drift") or []
    if drift:
        out.append("  what the lead does next:")
        out.extend("    - " + d["text"] for d in drift)
    else:
        out.append("  what the lead does next: nothing — the running seats "
                   "match the team")
    return out


def _json_view(rec):
    import math
    def clean(v):
        if isinstance(v, float) and math.isinf(v):
            return "inf"
        if isinstance(v, dict):
            return {k: clean(x) for k, x in v.items()}
        if isinstance(v, list):
            return [clean(x) for x in v]
        return v
    return clean(rec)


def _cmd_read(args):
    opts, pos, problem = _parse(args, {}, ("--json",))
    if problem or len(pos) > 1:
        print("helm team: %s\nusage: %s" % (problem or "one project at a "
                                              "time", USAGE), file=sys.stderr)
        return 2
    try:
        reg = registry.load(strict=True)
    except (OSError, ValueError) as exc:
        print("helm team: registry UNKNOWN: %s" % exc, file=sys.stderr)
        return 1
    if pos and pos[0] not in (reg.get("projects") or {}):
        print("helm team: unknown project '%s'" % pos[0], file=sys.stderr)
        return 1
    world = teams.placements(projects=reg.get("projects"))
    got = teams.view(keys=pos or None, live=True, reg=reg, world=world)
    if opts.get("--json"):
        print(json.dumps(_json_view(got), indent=1, sort_keys=True,
                         default=str))
        return 0
    if not got:
        print("helm team: no project has a team yet, and no seat seen in "
              "the last %d days is placed on one" % (teams.DERIVE_SEEN_S
                                                     // 86400))
        return 0
    first = True
    for key, rec in got.items():
        if not first:
            print()
        first = False
        for text in render(key, rec, world):
            print(text)
    return 0


def _cmd_set(args):
    opts, pos, problem = _parse(args, _SET_VALUED, ("--apply",))
    if problem or len(pos) != 1:
        print("helm team set: %s\nusage: %s" % (
            problem or "name one project", USAGE), file=sys.stderr)
        return 2
    project = pos[0]
    expect = opts.get("--expect")
    if expect is None or not str(expect).isdigit():
        print("helm team set: --expect V is required — the version you were "
              "looking at (0 for a proposed team; `helm team %s` prints it)"
              % project, file=sys.stderr)
        return 2
    reason = opts.get("--reason")
    if not reason:
        print("helm team set: --reason is required (it rides the room "
              "notice and the history)", file=sys.stderr)
        return 2
    try:
        reg = registry.load(strict=True)
    except (OSError, ValueError) as exc:
        print("helm team set: registry UNKNOWN: %s" % exc, file=sys.stderr)
        return 1
    if project not in (reg.get("projects") or {}):
        print("helm team set: unknown project '%s'" % project,
              file=sys.stderr)
        return 1
    world = teams.placements(projects=reg.get("projects"))
    base = teams.read(project, reg=reg, world=world)
    if int(expect) != base["v"]:
        # STALE BEFORE THE EDITS ARE READ: an edit made against a team that
        # moved is judged against the wrong members, and the sentence that
        # helps is the version, not an edit's refusal. `write` checks again
        # under the lock.
        print("helm team set: STALE — the team is v%d now and this change "
              "was made against v%s, so nothing was saved; read `helm team "
              "%s` and make it again" % (base["v"], expect, project),
              file=sys.stderr)
        return 1

    def families_of(seat):
        placed = (world.get("seats") or {}).get(seat) or {}
        return placed.get("family") or teams.family_of(
            seat, (world.get("roster") or {}).get(seat))
    team, problem = teams.edit(base, add=opts.get("--add") or (),
                               remove=opts.get("--remove") or (),
                               roles=opts.get("--role") or (),
                               shares=opts.get("--share") or (),
                               families_of=families_of)
    if problem:
        print("helm team set: %s" % problem, file=sys.stderr)
        return 2
    apply = bool(opts.get("--apply"))
    row, problem, code = teams.write(project, team, int(expect), by=_by(),
                                     reason=reason, apply=apply)
    if problem:
        print("helm team set: %s%s" % (
            "STALE — " if code == "stale" else "", problem), file=sys.stderr)
        # A ROSTER THAT DID NOT READ is no usage error: exit 1, as STALE
        return 1 if code in ("stale", "unread") else 2
    print("helm team set: %s %s v%d → v%d" % (
        "applied" if apply else "dry-run", project, int(expect), row["v"]))
    for text in row["diff"] or ["(no member or share changed)"]:
        print("  " + text)
    if not apply:
        print("  repeat with --apply to author it")
    elif row["posted"] is False:
        print("  NOTE — the team is saved; the TEAM-CHANGED notice to #%s "
              "did not post" % project, file=sys.stderr)
    return 0


def _cmd_seed(args):
    opts, pos, problem = _parse(args, {}, ("--apply",))
    if problem or pos:
        print("helm team seed: %s\nusage: %s" % (
            problem or "takes no project", USAGE), file=sys.stderr)
        return 2
    apply = bool(opts.get("--apply"))
    proposals, unmeasured = teams.seed()
    if unmeasured:
        # REFUSED, DRY RUN AND --apply ALIKE (design read D2): the shares a
        # seed proposes are the measured split, so with no burn measured it
        # would author every team with no share.
        print("helm team seed: NOT seeded — %s" % unmeasured, file=sys.stderr)
        return 1
    if not proposals:
        print("helm team seed: nothing to propose — every project with seats "
              "on it already has an authored team")
        return 0
    by = _by() if apply else ""
    rc = 0
    for p in proposals:
        team = p["team"]
        print("%s %s" % ("seeding" if apply else "would seed", p["project"]))
        for m in team["members"]:
            print("  %-9s %-28s %s" % (m["role"], m["seat"], m["family"]))
        print("  shares: %s" % (", ".join(
            "%s %d%%" % kv for kv in sorted(team["shares"].items()))
            or "none measured (each family is unrationed until a share is "
               "set)"))
        if apply:
            row, problem, _code = teams.write(
                p["project"], team, 0, by=by,
                reason="seeded from today's seats and the measured burn split",
                apply=True)
            if problem:
                print("  NOT seeded: %s" % problem, file=sys.stderr)
                rc = 1
            else:
                print("  authored v%d" % row["v"])
    if not apply:
        print("dry run — repeat with --apply to author these as v1; each is "
              "the owner's to change on the Work page")
    return rc


def _cmd_history(args):
    opts, pos, problem = _parse(args, {}, ("--json",))
    if problem or len(pos) != 1:
        print("helm team history: %s\nusage: %s" % (
            problem or "name one project", USAGE), file=sys.stderr)
        return 2
    rows, err = teams.history(pos[0])
    if err:
        print("helm team history: UNKNOWN — the history did not read (%s)"
              % err, file=sys.stderr)
        return 1
    if opts.get("--json"):
        print(json.dumps(rows, indent=1, sort_keys=True))
        return 0
    if not rows:
        print("helm team history: nothing authored for %s yet" % pos[0])
        return 0
    for r in rows:
        print("%-6s %s  %s — %s" % (
            ("v%d" % r["v"]) if r.get("kind") == "team" else "light",
            _when(r.get("ts")), r.get("by") or "an unnamed terminal",
            r.get("reason") or "no reason recorded"))
        for text in r.get("diff") or ():
            print("         " + text)
    return 0


def _use_text(use):
    if use is None:
        return "in use unread"
    held = ", ".join("%s %d" % kv for kv in sorted(
        (use.get("by_project") or {}).items()))
    return "%d in use%s%s" % (
        use.get("total") or 0, " (%s)" % held if held else "",
        "; %d on no project" % use["unplaced"] if use.get("unplaced") else "")


def _cmd_capacity(args):
    opts, pos, problem = _parse(args, {"--reason": False},
                                ("--apply", "--json"))
    if problem or len(pos) not in (0, 2):
        print("helm team capacity: %s\nusage: %s" % (
            problem or "name a family and a lane count (or unset)", USAGE),
            file=sys.stderr)
        return 2
    if pos:
        fam, word = pos
        if word == "unset":
            lanes = None
        elif word.isdigit():
            lanes = int(word)
        else:
            print("helm team capacity: lanes must be a whole number or "
                  "'unset', not '%s'" % word, file=sys.stderr)
            return 2
        apply = bool(opts.get("--apply"))
        row, problem = teams.set_capacity(fam, lanes, by=_by(),
                                          reason=opts.get("--reason"),
                                          apply=apply)
        if problem:
            print("helm team capacity: %s" % problem, file=sys.stderr)
            return 2
        print("helm team capacity: %s %s %s → %s" % (
            "applied" if apply else "dry-run", fam,
            "not measured" if row["was"] is None else "%d lanes" % row["was"],
            "not measured" if lanes is None else "%d lanes" % lanes))
        if not apply:
            print("  repeat with --apply to record it")
        return 0
    try:
        reg = registry.load(strict=True)
    except (OSError, ValueError) as exc:
        print("helm team capacity: registry UNKNOWN: %s" % exc,
              file=sys.stderr)
        return 1
    got = teams.slot_reading(projects=reg.get("projects"))
    if opts.get("--json"):
        print(json.dumps(got, indent=1, sort_keys=True))
        return 0
    if got.get("capacity_problem"):
        print("helm team capacity: NOTE — %s" % got["capacity_problem"],
              file=sys.stderr)
    for fam in got["families"]:
        cap = got["capacity"].get(fam)
        use = None if got["in_use"] is None else got["in_use"].get(fam)
        if cap:
            head = "%-12s %d lanes — measured by %s %s: %s" % (
                fam, cap["lanes"], cap["by"] or "an unnamed terminal",
                _when(cap["ts"]), cap["reason"] or "no reason recorded")
        else:
            head = ("%-12s capacity not measured (`helm team capacity %s "
                    "<lanes> --reason ... --apply`)" % (fam, fam))
        print(head)
        print("%-12s %s" % ("", _use_text(use)))
    if got["in_use"] is None and got.get("why"):
        print("helm team capacity: lanes in use UNKNOWN — %s" % got["why"],
              file=sys.stderr)
    return 0


def cmd_team(args):
    """team [<project>] [--json] | team set ... | team seed | team history |
    team capacity.

    POSITIONAL BY DESIGN: a first word that is no subverb is the <project> to
    read, so an unknown word is an unknown project (exit 1, not found) and
    never a default. Each subverb refuses junk with exit 2 before its first
    read or write; tests/test_dispatch_honest.py's EXEMPT pins both halves."""
    args = list(args or ())
    if args[:1] in (["--help"], ["-h"], ["help"]):
        print("usage: " + USAGE)
        return 0
    if not args:
        return _cmd_read(args)
    verb = args[0]
    if verb == "set":
        return _cmd_set(args[1:])
    if verb == "seed":
        return _cmd_seed(args[1:])
    if verb == "history":
        return _cmd_history(args[1:])
    if verb == "capacity":
        return _cmd_capacity(args[1:])
    return _cmd_read(args)
