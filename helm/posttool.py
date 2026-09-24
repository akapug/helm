"""Pure installed record+deliver planning; never a logical dispatcher registry.

Call prepare before legacy repair and again after requested standalone repair.
ValueError is atomic: callers must not fall through after it. Non-whole member
text is preserved by the writers and reported separately. No installer IO here.
"""
from copy import deepcopy
import os
import posixpath
import shlex

from . import hooks, record

EVENT = "PostToolUse"
ARGS = "hooks run PostToolUse --installed --hook-json"


def installed_specs():
    """Installed budgets, deliberately NOT record.HOOK_SPECS' logical 5s."""
    specs = (record.deployed_spec(),
             deepcopy(next(s for s in hooks.SPECS if s["name"] == "deliver")))
    for s, args in zip(specs, ("record --hook-json", "chat deliver --hook-json")):
        if (s["event"] != EVENT or s.get("matcher") not in (None, "*")
                or s.get("gate") or s["args"] != args
                or type(s.get("timeout")) is not int or s["timeout"] <= 0):
            raise ValueError("incompatible installed PostToolUse member declaration")
    return specs


def preparation_budget():
    """Nonlatching preparation gets a SEPARATE existing-deliver-sized budget.

    Record and delivery keep their installed budgets. Preparation is additional
    work, not borrowed delivery time or evidence of a measured timeout saving.
    """
    return installed_specs()[1]["timeout"]


def descriptor():
    """Installed record+prepare+deliver+reserve; normal harness grace outside."""
    return {"name": "posttool", "event": EVENT, "matcher": "*",
            "args": ARGS,
            "timeout": sum(s["timeout"] for s in installed_specs())
            + preparation_budget() + hooks.GRACE_S}


def entry(*, executable, renderer=hooks.spec_command):
    """Canonical native entry without executable discovery or filesystem IO."""
    if not isinstance(executable, str) or not posixpath.isabs(executable):
        raise ValueError("installed executable must be absolute")
    s = descriptor()
    return {"matcher": "*", "hooks": [{"type": "command",
            "command": renderer(s, executable=executable),
            "timeout": hooks._gate_outer_timeout(s)}]}


def _shape(words):
    """(inner budget, absolute executable) for a command this may reconstruct.

    TWO SPELLINGS, BECAUSE TWO ARE ON DISK. helm installs
    `<abs>/bin/helm-hook <kind> <name> <event> <budget> <consequence>
    <abs>/bin/helm …` today; every estate written before the ladder moved into
    bin/ carries `timeout N <abs>/bin/helm …` inline. Locating the two numbers
    grants NOTHING — ownership is still the exact whole-command reconstruction
    in `recognize`, and this only decides which arguments to hand the renderer.
    """
    if (len(words) >= 7
            and os.path.basename(words[0]) == hooks.HOOK_WRAPPER):
        budget, exe = words[4], words[6]
    elif len(words) >= 4 and words[0] == "timeout":
        budget, exe = words[1], words[2]
    else:
        return None
    if (not budget.isascii() or not budget.isdigit()
            or not posixpath.isabs(exe) or int(budget) <= 0):
        return None
    return int(budget), exe


def recognize(command, *, renderer=hooks.spec_command):
    """Closed whole-command ownership -> (name, inner budget), or None.

    Tokenization only locates renderer arguments; it NEVER grants ownership.
    Exact reconstruction admits today's renderer, every rendering helm has
    written (hooks.HISTORICAL_COMMANDS) and the proven simple historic advisory
    forms. Relative executables, shell suffixes and flag variants fail.

    TODAY'S TEMPLATE IS NOT THE ONLY ONE ON DISK, and this function asked it
    alone. That is the same defect `cred._retired_guard_command` carried one
    layer over: an installed leaf keeps the ladder that was current when it was
    written, so the first change to the template orphans it — `recognize`
    returns None, `protected` then returns True, and a standalone member helm
    itself wrote is refused as foreign and left in place forever. Nothing
    breaks loudly; the estate just keeps leaves helm believes it converted.
    The frozen renderings are data in `hooks.HISTORICAL_COMMANDS`, so a
    template change adds an entry there instead of quietly orphaning an estate,
    and both readers of ownership consult it.

    THE HISTORICAL RENDERERS ARE NOT SUBSTITUTABLE, which is why `renderer`
    does not reach them: they are frozen records of bytes on disk, and a caller
    that substitutes today's renderer is asking what helm writes NOW.
    """
    if not isinstance(command, str):
        return None
    try:
        words = shlex.split(command)
    except ValueError:
        return None
    shape = _shape(words)
    if shape is None:
        return None
    budget, exe = shape
    for s in (*installed_specs(), descriptor()):
        candidate = dict(s, timeout=budget)
        if command == renderer(candidate, executable=exe):
            return s["name"], budget
        if s["name"] != "posttool" and command == "timeout %d %s %s || true" % (
                budget, shlex.quote(exe), s["args"]):
            return s["name"], budget
        if any(command == c for c in
               (h(candidate, exe) for h in hooks.HISTORICAL_COMMANDS)
               if c is not None):
            return s["name"], budget
    return None


def _resembles(command):
    # Refusal only, NEVER deletion ownership. Protect legacy substring repair
    # from laundering an unknown wrapper into a subsequently deletable leaf.
    return any(s in str(command or "") for s in (
        "record --hook-json", "helm record", "chat deliver", "hooks run PostToolUse"))


def protected(command, *, renderer=hooks.spec_command):
    """Member-like text without whole-command ownership is never rewritten."""
    return _resembles(command) and recognize(command, renderer=renderer) is None


def refusals(settings):
    """Persistent, sanitized categories; never include command/payload bytes."""
    hks = settings.get("hooks") if isinstance(settings, dict) else None
    groups = hks.get(EVENT) if isinstance(hks, dict) else None
    for group in groups if isinstance(groups, list) else ():
        leaves = group.get("hooks") if isinstance(group, dict) else None
        if isinstance(leaves, list) and any(protected(leaf.get("command"))
                for leaf in leaves if isinstance(leaf, dict)):
            return ("non-whole-PostToolUse-member",)
    return ()


def refusal_detail(path, categories):
    home = str(path).replace("\n", "\\n").replace("\r", "\\r")
    return "; ".join("REFUSED %s [%s]: original leaf preserved; canonical wiring may coexist; "
                     "possible duplicate recorder execution requires operator resolution"
                     % (home, category) for category in categories)


def _known(group, leaf):
    return (set(group) <= {"matcher", "hooks"}
            and set(leaf) <= {"type", "command", "timeout"}
            and group.get("matcher") in (None, "*"))


def covers(group, leaf, *, executable, renderer=hooks.spec_command):
    """Full composite coverage, not just ownership. Never covers failure events.

    The caller supplies a PostToolUse group (or uses covered_members(settings)).
    Unknown keys are not asserted harmless by the installed planner.
    """
    return (isinstance(group, dict) and isinstance(leaf, dict)
            and _known(group, leaf) and leaf.get("type") == "command"
            and leaf.get("command") == entry(
                executable=executable, renderer=renderer)["hooks"][0]["command"]
            and type(leaf.get("timeout")) is int
            and leaf["timeout"] == hooks._gate_outer_timeout(descriptor()))


def _scan(settings, renderer):
    if not isinstance(settings, dict) or not isinstance(settings.get("hooks", {}), dict):
        raise ValueError("settings and hooks must be objects")
    groups = settings.get("hooks", {}).get(EVENT, [])
    if not isinstance(groups, list):
        raise ValueError("PostToolUse groups must be a list")
    found = {"record": [], "deliver": [], "posttool": []}
    position = 0
    for gi, group in enumerate(groups):
        if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
            # Legacy writers preserve these ignored foreign branches. They do
            # not authorize conversion across an unknown execution boundary.
            position += 1
            continue
        for hi, leaf in enumerate(group["hooks"]):
            if not isinstance(leaf, dict):
                position += 1
                continue
            owned = recognize(leaf.get("command"), renderer=renderer)
            if owned:
                found[owned[0]].append((gi, hi, position, owned[1]))
            position += 1
    return groups, found


def covered_members(settings, *, executable, renderer=hooks.spec_command):
    """Names covered by one unambiguous, fully healthy installed composite."""
    try:
        groups, found = _scan(settings, renderer)
    except ValueError:
        return frozenset()
    if len(found["posttool"]) != 1 or found["record"] or found["deliver"]:
        return frozenset()
    gi, hi, _, _ = found["posttool"][0]
    return frozenset(("record", "deliver")) if covers(
        groups[gi], groups[gi]["hooks"][hi], executable=executable,
        renderer=renderer) else frozenset()


def prepare(settings, requested_specs, *, executable, renderer=hooks.spec_command):
    """Plan eligible conversion; ineligible standalones keep their repair path.

    Call before legacy repair to protect exact ownership, then again after it
    to convert only the resulting compatible contracts. Requested custom policy
    is a standalone declaration, not permission to substitute the default pair.
    Existing composites have no legacy standalone equivalent: ambiguous or
    unknown composite policy still requires an explicit operator decision.
    """
    requested = tuple(requested_specs)
    members = {s["name"]: s for s in installed_specs()}
    groups, found = _scan(settings, renderer)
    wanted, compatible = {}, True
    for s in requested:
        if s.get("event") != EVENT:
            continue
        name = s.get("name")
        if name not in members:
            if (name == "posttool" or _resembles(s.get("args"))
                    or _resembles(s.get("own"))
                    or any(hooks._own_hit(renderer(descriptor(), executable=executable), m)
                           for m in s.get("own", ()))):
                compatible = False
            continue
        expected = members[name]
        if (set(s) - (set(expected) | {"gate"})
                or s.get("args") != expected["args"]
                or s.get("timeout") != expected["timeout"]
                or type(s.get("timeout")) is not int
                or s.get("matcher") not in (None, "*") or s.get("gate")
                or s.get("scope") != expected.get("scope")
                or s.get("own", expected["own"]) != expected["own"]):
            compatible = False
        wanted[name] = s
    out = deepcopy(settings)
    if found["posttool"]:
        if len(found["posttool"]) != 1:
            raise ValueError("duplicate PostToolUse composite; operator decision required")
        if found["record"] or found["deliver"]:
            raise ValueError("composite coexists with standalone member; operator decision required")
        if not compatible:
            raise ValueError("incompatible requested custom policy cannot replace or split installed composite")
        gi, hi, _, budget = found["posttool"][0]
        group, leaf = groups[gi], groups[gi]["hooks"][hi]
        if (not _known(group, leaf) or leaf.get("type") != "command"
                or budget != descriptor()["timeout"]
                or ("timeout" in leaf and (type(leaf["timeout"]) is not int
                    or leaf["timeout"] != hooks._gate_outer_timeout(descriptor())))):
            raise ValueError("unknown keys, matcher, type, budget or deadline for composite")
        action = "ok" if covers(group, leaf, executable=executable, renderer=renderer) else "update"
        if action == "update":
            out["hooks"][EVENT][gi]["hooks"][hi] = entry(executable=executable, renderer=renderer)["hooks"][0]
    else:
        # Main's requested standalone writer owns type, matcher and budget
        # repair, retaining unknown keys. Unrequested drift is not normalized.
        # Unknown keys remain conversion-ineligible even after safe repair.
        eligible = compatible and all(found[n] or n in wanted for n in members)
        for name in members:
            for gi, hi, _, budget in found[name]:
                group, leaf = groups[gi], groups[gi]["hooks"][hi]
                spec = members[name]
                eligible = eligible and (_known(group, leaf)
                    and leaf.get("type") == "command" and budget == spec["timeout"]
                    and ("timeout" not in leaf or (type(leaf["timeout"]) is int
                         and leaf["timeout"] == hooks._gate_outer_timeout(spec))))
        locations = sorted((loc for n in members for loc in found[n]), key=lambda p: p[2])
        if locations and (locations[-1][2] - locations[0][2] + 1 != len(locations)
                or (found["record"] and found["deliver"]
                    and found["record"][-1][2] >= found["deliver"][0][2])):
            eligible = False
        if not eligible:
            return out, deepcopy(requested), {}
        canonical = entry(executable=executable, renderer=renderer)
        if not locations:
            out.setdefault("hooks", {}).setdefault(EVENT, []).append(canonical)
        else:
            gi, hi, _, _ = locations[0]
            out["hooks"][EVENT][gi]["hooks"][hi] = canonical["hooks"][0]
            for dg, dh, _, _ in reversed(locations[1:]):
                del out["hooks"][EVENT][dg]["hooks"][dh]
                if not out["hooks"][EVENT][dg]["hooks"]:
                    del out["hooks"][EVENT][dg]
        action = "update" if locations else "add"
    remaining = tuple(deepcopy(s) for s in requested
                      if not (s.get("event") == EVENT and s.get("name") in wanted))
    return out, remaining, {n: action for n in wanted}
