#!/usr/bin/env python3
"""WHAT A SEAT IS HANDED BEFORE ITS FIRST TURN, PER CHECKOUT.

The instruction files a harness loads for a working directory, and the
project memory index beside them, are FIXED TEXT: they arrive whole in every
context a seat ever opens there, and nothing on the per-turn path measures
them because nothing on that path put them there. `helm injectbudget` reports
what helm's own hooks add; this is the share that was already in the room.

THE UNIT IS THE CHECKOUT, NEVER THE SEAT. The harness keys a project's memory
directory by WORKING DIRECTORY, so every seat standing in one checkout loads
the same index and any one of them can grow it for all of them. A report per
seat would show each seat a number it cannot change alone and hide who else
is paying it. So each row names the checkout, its bytes, and every seat the
roster homes there -- and nothing here caps or prunes anything: a ceiling on
shared text is an agreement between the seats that load it, and an instrument
that enforced one on behalf of whoever ran it would be one seat editing the
others' memory.

ABSENT AND UNREADABLE ARE DIFFERENT FACTS. A file that does not exist is not
loaded and costs nothing; a file that exists and cannot be sized is a number
this report does not have, and the row says PARTIAL rather than printing a
total that is quietly too small.
"""
import json
import os
import re
import sys

from . import home

#: The file names EACH HARNESS reads in every directory from the root down to
#: the working directory, and the personal file in its own home. A file is
#: fixed text for a checkout only when a seat homed there runs the harness
#: that loads it: a codex MODEL served behind the proxy still runs inside the
#: claude harness and is handed the claude files, never the codex one.
DEFAULT_HARNESS = "claude"
CHAIN_NAMES = {
    "claude": ("CLAUDE.md", os.path.join(".claude", "CLAUDE.md"),
               "CLAUDE.local.md"),
    "codex": ("AGENTS.md",),
}
_SLUG = re.compile(r"[^A-Za-z0-9]")


def config_dir():
    """The harness's own home: where its personal instruction file and its
    per-project memory live. The harness's own override names it when set,
    which is how a seat launched under another credential home is read."""
    return (os.environ.get("CLAUDE_CONFIG_DIR")
            or os.path.join(os.path.expanduser("~"), ".claude"))


def memory_index(cwd, config=None):
    """The project memory index the harness loads for `cwd`. The project
    directory is the working directory's path with every character outside
    the alphanumerics folded to a dash."""
    return os.path.join(config or config_dir(), "projects",
                        _SLUG.sub("-", cwd), "memory", "MEMORY.md")


def codex_dir():
    return (os.environ.get("CODEX_HOME")
            or os.path.join(os.path.expanduser("~"), ".codex"))


def candidates(cwd, config=None, harness=DEFAULT_HARNESS):
    """Every path that is fixed text for `cwd` under one harness IF it
    exists, outermost first: the personal file, the chain from the root
    down, and for the claude harness the project memory index."""
    cwd = os.path.abspath(cwd)
    names = CHAIN_NAMES[harness]
    if harness != DEFAULT_HARNESS:
        paths = [os.path.join(codex_dir(), names[0])]
    else:
        paths = [os.path.join(config or config_dir(), "CLAUDE.md")]
    parts = cwd.split(os.sep)
    for depth in range(1, len(parts) + 1):
        at = os.sep.join(parts[:depth]) or os.sep
        paths += [os.path.join(at, name) for name in names]
    if harness != DEFAULT_HARNESS:
        return _once(paths)
    return _once(paths + [memory_index(cwd, config)])


def _once(paths):
    """ONE FILE IS LOADED ONCE. The personal file is also what the chain
    finds when the walk passes through the home directory, and counting it at
    both doors would print a total larger than anything a seat is handed."""
    seen, out = set(), []
    for path in paths:
        key = os.path.realpath(path)
        if key not in seen:
            seen.add(key)
            out.append(path)
    return out


def _sized(paths):
    files, unreadable = [], []
    for path in paths:
        try:
            files.append((path, os.stat(path).st_size))
        except FileNotFoundError:
            continue
        except OSError:
            unreadable.append(path)
    return files, unreadable


def measure(cwd, config=None, harnesses=(DEFAULT_HARNESS,)):
    """{"files", "unreadable", "total", "unloaded"} for one checkout.

    `files` is what the harnesses actually standing here load, and `total`
    is their sum. `unloaded` is an instruction file that EXISTS here for a
    harness no seat here runs: it is shown, because a reader deserves to see
    it, and it is kept out of the total, because a number that counted text
    nobody is handed would be wrong in the other direction. A missing file
    is skipped; one that exists and cannot be sized is named in `unreadable`
    and left out of `total`."""
    here = [h for h in sorted(CHAIN_NAMES) if h in set(harnesses)]
    files, unreadable = _sized(_once(
        [p for h in here for p in candidates(cwd, config, h)]))
    loaded = {os.path.realpath(p) for p, _size in files}
    unloaded, _cannot = _sized(_once(
        [p for h in sorted(CHAIN_NAMES) if h not in here
         for p in candidates(cwd, config, h)]))
    return {"files": files, "unreadable": unreadable,
            "total": sum(size for _path, size in files),
            "unloaded": [(p, size) for p, size in unloaded
                         if os.path.realpath(p) not in loaded]}


def checkouts(rows):
    """{checkout: [seat, ...]} from roster rows: every seat the roster homes
    in a working directory, names sorted. A row with no recorded home is a
    seat this report cannot place, and is left out rather than guessed. So is
    a home that is not printable text: the path is a roster field bound for a
    terminal, and one carrying a control character is not a path anyone
    stands in."""
    out = {}
    for seat, row in sorted((rows or {}).items()):
        cwd = row.get("cwd") if isinstance(row, dict) else None
        if isinstance(cwd, str) and cwd and cwd.isprintable():
            out.setdefault(os.path.abspath(cwd), []).append(seat)
    return out


def harness_of(row):
    """The harness a roster row records, else the default: the harness every
    recorded seat has ever run. A name this module holds no file list for is
    read as the default too, never as a harness that loads nothing."""
    runtime = row.get("runtime") if isinstance(row, dict) else None
    name = runtime.get("agent_harness") if isinstance(runtime, dict) else None
    return name if name in CHAIN_NAMES else DEFAULT_HARNESS


def survey(rows, config=None):
    """One row per checkout, heaviest first."""
    found = [dict(measure(cwd, config,
                          {harness_of((rows or {}).get(s)) for s in seats}),
                  checkout=cwd, seats=seats)
             for cwd, seats in checkouts(rows).items()]
    return sorted(found, key=lambda r: (-r["total"], r["checkout"]))


def _kb(size):
    return "%.1f KB" % (size / 1024.0)


def _line(row, label):
    return "%s loads %s of fixed text before its first turn%s, shared by %s" % (
        row["checkout"], _kb(row["total"]),
        " (PARTIAL: %d file(s) unreadable)" % len(row["unreadable"])
        if row["unreadable"] else "",
        ", ".join(label(s) for s in row["seats"]))


def live_rows():
    """(rows, failed) from the roster, through its checked reader."""
    from . import seats_roster
    return seats_roster.roster_checked()


def findings(rows=None, failed=False, config=None):
    """The `helm doctor` rung: [(level, message)]. It names the heaviest
    checkout on EVERY run, because a number shown only once it is bad
    teaches no reader what normal looks like."""
    from .seats_common import _seat_label
    if rows is None:
        rows, failed = live_rows()
    if failed:
        return [("WARN", "fixed text: the roster could not be read, so which "
                         "seats share which checkout is UNMEASURED")]
    found = survey(rows, config)
    if not found:
        return [("OK", "fixed text: no rostered seat has a recorded home")]
    worst = found[0]
    return [("WARN" if worst["unreadable"] else "OK",
             "fixed text: %s — `helm fixedtext` lists every checkout and file"
             % _line(worst, _seat_label))]


USAGE = ("usage: helm fixedtext [--json]\n"
         "  The instruction files and project memory index each checkout "
         "loads before a seat's first turn, with the seats that share it. "
         "Read-only: it caps and prunes nothing.")


def cmd(args):
    from .cli import guard_tail
    args = list(args or [])
    rc = guard_tail("helm fixedtext", args, flags=("--json",), usage=USAGE)
    if rc is not None:
        return rc
    from .seats_common import _seat_label
    rows, failed = live_rows()
    if failed:
        print("helm fixedtext: UNREADABLE — the roster could not be read, so "
              "no checkout can be attributed to its seats", file=sys.stderr)
        return 1
    found = survey(rows)
    if "--json" in args:
        print(json.dumps([dict(r, seats=[_seat_label(s) for s in r["seats"]])
                          for r in found], indent=2))
        return 0
    for row in found:
        print(_line(row, _seat_label))
        for path, size in row["files"]:
            print("    %9s  %s" % (_kb(size), path))
        for path in row["unreadable"]:
            print("   UNREADABLE  %s" % path)
        for path, size in row["unloaded"]:
            print("    %9s  %s  (NOT COUNTED: no seat here runs the harness "
                  "that loads it)" % (_kb(size), path))
    if not found:
        print("no rostered seat has a recorded home")
    return 0
