#!/usr/bin/env python3
"""helm inject — the ONE active-fire surface. A harness hook calls this once
per turn with the prompt text; helm returns the context worth injecting:

  1. the pinned lane   (load_class=always priors, byte-budget-capped)
  2. the JIT lane      (typed-store entries whose specific keywords match)
  3. reflex steers     (signals live this turn)

One resolver, every harness — claude/codex/opencode/hermes hooks all call the
same verb, which is what makes helm's knowledge fire wherever the operator
works. Salience law: no match -> EMPTY output (a silent turn costs nothing).

Wiring (examples):
  claude   UserPromptSubmit hook: helm inject --project <p> < prompt.txt
           -> stdout becomes additionalContext
  codex    notify/turn hook: same call, same stdout
"""
import json
import os
import sys

from . import reflex

PINNED_BUDGET = 1200  # bytes for the always lane — keep the constant tax tiny
JIT_CAP = 4
LINE_CAP = 400        # per-entry cap — the gloss fires, the full entry stays on disk


def _entry_line(e):
    line = _entry_line_full(e)
    return line if len(line) <= LINE_CAP else line[:LINE_CAP - 1] + "…"


def _entry_line_full(e):
    t = e.get("type")
    if t == "prior":
        tag = "PREMISE" if e.get("class") == "certain" else "PRIOR %.2f" % e["confidence"]
        return "%s %s: %s" % (tag, e["id"], e.get("statement") or "")
    if t == "lexicon":
        return "TERM %s: %s" % (e.get("term") or e["id"], e.get("definition") or e.get("statement") or "")
    if t == "heuristic":
        return "MOVE %s: %s" % (e["id"], e.get("statement") or "")
    if t == "reference":
        return "REF %s: %s" % (e["id"], e.get("statement") or "")
    return "%s: %s" % (e["id"], e.get("statement") or "")


def gather(text, project=None):
    """-> dict {pinned: [line], jit: [line], reflex: [line]} (each may be empty)."""
    from . import store
    pinned_lines = []
    used = 0
    for e in store.pinned(project=project):
        line = _entry_line(e)
        if used + len(line) > PINNED_BUDGET:
            break
        pinned_lines.append(line)
        used += len(line)
    jit = [_entry_line(e) for e in store.resolve_prompt(text, project=project, cap=JIT_CAP)]
    steers = ["REFLEX: " + e["steer"] for e in reflex.fire(text, project=project)]
    return {"pinned": pinned_lines, "jit": jit, "reflex": steers}


def render(sections):
    lines = sections["pinned"] + sections["jit"] + sections["reflex"]
    return "\n".join(lines)


def cmd_inject(args):
    """inject [--project P] [--json] — prompt text on stdin -> context lines."""
    project = None
    if "--project" in args:
        project = args[args.index("--project") + 1]
    text = "" if sys.stdin.isatty() else sys.stdin.read()
    for a in args:
        if not a.startswith("--") and a != project:
            text = a  # allow inline text for quick tests
    sections = gather(text, project=project)
    if "--json" in args:
        print(json.dumps(sections, ensure_ascii=False))
        return 0
    out = render(sections)
    if out:
        print(out)
    return 0
