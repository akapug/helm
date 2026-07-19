#!/usr/bin/env python3
"""Reflexes — (signal) -> (steer), delivered by inject-capable harness hooks.

A rule is what you believe; a reflex is what FIRES. Rules decay as context
scrolls; a reflex is re-delivered fresh on its signal, unconditionally — only
the heeding is probabilistic. helm re-inherits the reflex layer harness-broad:
the store lives here, delivery is one `helm inject` call any harness hook can
make (no per-harness lane gating — reflexes fire wherever the operator works).

v1 signal kinds (cheap, observable — a false-firing reflex is theater):
  every-turn    constant steer, budget-capped (keep the set TINY)
  prompt        regex against the current prompt text
  marker-file   fires while a filesystem path exists (AFK flags, mode markers)

Laws: default-silent; fire only on a live signal; steers are terse FACTS,
never commands; fixed text only (no interpolation of tool/user output).

Store: reflex-<slug>.md in _global/reflexes/ (or a project's premises sibling
dir reflexes/), frontmatter per pk.parse_simple_frontmatter.
"""
import os
import re

from . import home, pk

_DEFAULTS = {
    "id": "", "steer": "", "signal": "prompt", "pattern": "", "marker": "",
    "status": "live", "stated_ts": "", "notes": "",
}

EVERY_TURN_BUDGET = 3  # max constant steers per injection — habituation guard


def _dirs(project=None):
    out = []
    if project:
        d = os.path.join(home.project_dir(project), "reflexes")
        if os.path.isdir(d):
            out.append(d)
    out.append(os.path.join(home.global_dir(), "reflexes"))
    return out


def reflex_path(rid, project=None):
    base = _dirs(project)[0] if project else os.path.join(home.global_dir(), "reflexes")
    return os.path.join(base, "reflex-" + pk.slug(rid) + ".md")


def load_all(project=None, include_retired=False):
    out = {}
    for d in reversed(_dirs(project)):  # global first; project shadows by id
        if not os.path.isdir(d):
            continue
        for n in sorted(os.listdir(d)):
            if not (n.startswith("reflex-") and n.endswith(".md")):
                continue
            e = pk.parse_simple_frontmatter(os.path.join(d, n), _DEFAULTS)
            if not (e and e["id"] and e["steer"]):
                continue
            if not include_retired and e["status"] != "live":
                continue
            e["path"] = os.path.join(d, n)
            out[pk.slug(e["id"])] = e
    return list(out.values())


def write(e, project=None):
    path = reflex_path(e["id"], project)
    body = [
        "---",
        "name: reflex-" + pk.slug(e["id"]),
        'description: "reflex: ' + (e["id"] + " - " + e["steer"])[:170] + '"',
        "metadata:",
        "  node_type: memory",
        "  type: reflex",
        "  id: " + e["id"],
        "  steer: " + re.sub(r"\s+", " ", e["steer"]),
        "  signal: " + (e.get("signal") or "prompt"),
    ]
    for opt in ("pattern", "marker", "notes"):
        if e.get(opt):
            body.append("  " + opt + ": " + str(e[opt]))
    body += ["  status: " + (e.get("status") or "live"),
             "  stated_ts: " + str(e.get("stated_ts") or pk.now_ts()),
             "---", "",
             "REFLEX: " + e["steer"], ""]
    pk.atomic_write(path, "\n".join(body))
    return path


def fire(text, project=None):
    """The reflexes whose signal is LIVE for this turn. Salience law: []."""
    out = []
    constant = 0
    for e in load_all(project):
        sig = e.get("signal") or "prompt"
        if sig == "every-turn":
            if constant < EVERY_TURN_BUDGET:
                out.append(e)
                constant += 1
        elif sig == "prompt" and e.get("pattern"):
            try:
                if re.search(e["pattern"], text or "", re.IGNORECASE):
                    out.append(e)
            except re.error:
                continue
        elif sig == "marker-file" and e.get("marker"):
            if os.path.exists(os.path.expanduser(e["marker"])):
                out.append(e)
    return out


def cmd_reflex(args):
    """reflex list|add|retire — manage the (signal -> steer) set."""
    if not args or args[0] == "list":
        es = load_all(include_retired="--all" in args)
        if not es:
            print("helm reflexes: none. Add one: helm reflex add <id> | <steer> "
                  "[--signal prompt --pattern <re> | --signal every-turn | "
                  "--signal marker-file --marker <path>]")
            return 0
        print("helm reflexes (%d):" % len(es))
        for e in es:
            sig = e["signal"] + (":" + e["pattern"] if e.get("pattern") else "") \
                + (":" + e["marker"] if e.get("marker") else "")
            mark = "" if e["status"] == "live" else " [" + e["status"] + "]"
            print("  - %s%s (%s): %s" % (e["id"], mark, sig, e["steer"][:90]))
        return 0
    if args[0] == "add":
        import sys
        flags = {}
        kept = []
        i = 1
        while i < len(args):
            if args[i] in ("--signal", "--pattern", "--marker", "--project") and i + 1 < len(args):
                flags[args[i][2:]] = args[i + 1]
                i += 2
                continue
            kept.append(args[i])
            i += 1
        parts = [p.strip() for p in " ".join(kept).split("|")]
        if len(parts) < 2 or not parts[0] or not parts[1]:
            print("usage: helm reflex add <id> | <steer> [--signal S] [--pattern RE] "
                  "[--marker PATH] [--project P]", file=sys.stderr)
            return 2
        e = {"id": parts[0], "steer": parts[1], "signal": flags.get("signal", "prompt"),
             "pattern": flags.get("pattern", ""), "marker": flags.get("marker", ""),
             "stated_ts": pk.now_ts()}
        if e["signal"] == "prompt" and not e["pattern"]:
            e["pattern"] = r"\b" + re.escape(parts[0]) + r"\b"
        path = write(e, flags.get("project"))
        print("helm reflex: LIVE '%s' (%s) -> %s" % (parts[0], e["signal"], path))
        return 0
    if args[0] == "retire":
        import sys
        if len(args) < 2:
            print("usage: helm reflex retire <id>", file=sys.stderr)
            return 2
        es = {pk.slug(e["id"]): e for e in load_all(include_retired=True)}
        e = es.get(pk.slug(args[1]))
        if not e:
            print("helm reflex: '%s' not found" % args[1], file=sys.stderr)
            return 1
        e["status"] = "retired"
        with open(e["path"]) as fh:
            raw = fh.read()
        pk.atomic_write(e["path"], raw.replace(
            "  status: live", "  status: retired"))
        print("helm reflex: RETIRED '%s' (file kept as the record)" % args[1])
        return 0
    print("helm reflex: unknown subcommand '%s'" % args[0])
    return 2
