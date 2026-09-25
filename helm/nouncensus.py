#!/usr/bin/env python3
"""helm.nouncensus — the per-SITE uses-closure census for the seat-state nouns.

task/1300 slice 2. The landed design record names one
deliverable: "every writer/reader of each noun assigned exactly once, sourced
from call sites (no hand-rolled counter)". Both halves of that sentence are
load-bearing and this module exists because of the second one — a table typed
by hand is a claim about what somebody remembered, and the store already has
the entry for that (a-leading-underscore-is-a-convention-not-a-measurement:
you find call sites by measuring call sites).

WHY PER-SITE AND NOT PER-FILE, measured before this was written: seeding the
seven nouns from the record's own identity fields and counting hits across
helm/*.py gives ~3,000 hits in 130 files, and 62 OF THOSE 130 FILES TOUCH MORE
THAN ONE NOUN. So "assigned exactly once" is unsatisfiable at file grain: a
file-level census would have to give seats.py to one noun and lie about the
rest. The unit is the SITE — one attribute access, one subscript, one keyword.

AMBIGUITY IS A STATE, NOT A TIE TO BREAK, AND IT IS A PROPERTY OF THE SITE
RATHER THAN OF THE TOKEN. My first cut computed it from seed overlap — a token
appearing under two nouns — and no token does, so the state was UNREACHABLE BY
CONSTRUCTION while this docstring claimed otherwise: a dead third state, which
is the exact vacuity this module refuses elsewhere. The real ambiguity is a
LINE whose tokens span several nouns (`runtime_for_session(seat, sid)` is one
site belonging to three), and it is what "assigned exactly once" actually
collides with. MEASURED on trunk: 198 of 3,169 sites, 6.2%, dominated by
seat+session at 138 — the record's own hard rule (Actor != Session != Pane)
failing in the code it governs. The census SURFACES that count; it does not
resolve it. The whole point of the
record is that no surface may hold a truth that disagrees with the record, and
a census that silently picks a winner manufactures exactly that: a confident
assignment nobody measured. Three states, and the third is honest — ASSIGNED,
AMBIGUOUS, and (for a module that cannot be parsed) UNREADABLE, never dropped.

R2 RIDES THE ROW, NOT A SEPARATE PASS. The record's R2 says every surface
attributing a pane or session to an actor is a claim about a DIFFERENT process
and must carry its proof tier: /proc/<pid>/exe is a kernel symlink and is the
only observer-side proof; argv and environ are writable by the process they
describe and are CANDIDATE evidence. So each site carries `tier`, and a
consumer can ask the question the record cares about — which attributions rest
on evidence their subject could have forged — without a second traversal.
"""
import ast
import os
import re
import sys as _sys

from . import pk, wiring

# ---------------------------------------------------------------------------
# the nouns, seeded from the record's own map — NOT from popularity
# ---------------------------------------------------------------------------
# A seed is the set of tokens the record names as that noun's IDENTITY. Picking
# by frequency instead would drag every hub into the floor (the store's
# a-shared-floor-is-a-closure-not-a-top-n, measured on the seats.py split).
NOUNS = {
    "actor":     {"actor_id", "runtime_verified", "runtime_sessions",
                  "resolve_identity", "identity_disagreement"},
    "seat":      {"seat", "seat_id", "HELM_CHAT_NAME", "acting_seat",
                  "own_name", "foreign_seat", "_seat_label"},
    "session":   {"session", "session_id", "sid", "runtime_for_session",
                  "CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID"},
    "pane":      {"pane", "pane_key", "ORCA_PANE_KEY", "ORCA_TERMINAL_HANDLE",
                  "ORCA_TAB_ID", "leafId"},
    "lane":      {"lane"},
    "lease":     {"lease", "lease_id", "fence"},
    "routing":   {"recipient", "supersedes", "dispatch_id", "reviewed_tip"},
    "obligation": {"obligation", "owed", "discharge", "ownerasks"},
    "verb":      {"verb", "SEAT_VERBS", "MELD_VERBS", "_TEXT_VERBS"},
}
_OWNER = {tok: noun for noun, toks in NOUNS.items() for tok in toks}

# R2: the proof tier a site's evidence can carry. KERNEL is unforgeable by the
# process being described; CANDIDATE is written by that same process.
#
# BOTH TIERS ARE ANCHORED, AND THE KERNEL ONE HAD TO BE. The first cut matched
# bare substrings — ("/proc", "exe", "st_uid", "starttime") — and a probe
# measured that ALL FOUR kernel-tier sites on trunk were false: a usage string
# in cli.py, "agent_starttime" and "starttime" as ordinary FIELD NAMES, and
# the prose "/proc evidence)" inside an error message. Not one was a kernel
# read. A tier that over-claims is worse than no tier, because R2 exists
# precisely to say which attributions rest on evidence the subject could have
# forged — and a false KERNEL row says "trust this one" about a field name.
#
# So kernel now requires the READ SHAPE, a /proc path naming exe or stat, and
# a bare "starttime" no longer qualifies: it is a kernel FACT only when read
# from /proc, and as a dict key it is just a word. The candidate tier is
# word-bounded for the same class — unbounded "environ" would match
# "environment" in prose — even though its 77 rows all carried a real token
# today. Curing one tier and leaving its sibling matching by substring is how
# the same defect comes back wearing the other tier's name.
_KERNEL_RE = re.compile(r"/proc/[^\s\"']*(?:exe|stat)\b|"
                        r"readlink\([^)]*/proc/")
_CANDIDATE_RE = re.compile(r"\b(?:environ|argv|cmdline)\b|"
                           r"\bHELM_CHAT_NAME\b|\bORCA_[A-Z_]+\b")

ASSIGNED, AMBIGUOUS, UNREADABLE = "ASSIGNED", "AMBIGUOUS", "UNREADABLE"

# The tiers _tier can actually return, minus "" (which means "this line
# attributes nothing" and is not a filterable value). Named here so the verb
# validates against the SAME set the function produces rather than a second
# hand-typed list: a filter whose vocabulary drifts from its producer either
# refuses valid input or admits invalid, and both failures look like data.
TIERS = ("kernel", "candidate")


def _tier(text):
    """R2's proof tier for a source LINE, or "" when it attributes nothing.

    THE SCOPE IS THE LINE, AND THAT BOUNDS WHAT A ZERO MEANS. A kernel read
    assembled across statements — `base = os.path.join(PROC, str(pid))` on one
    line and `os.readlink(base + "/exe")` on another — is invisible here. So
    "0 kernel-tier sites" is the honest sentence "no line BOTH touches a
    seat-state noun AND carries kernel evidence", never "helm reads nothing
    from /proc": helm/roguescan.py:182 does exactly that, on a line holding no
    noun. State the domain with the number or the number reads wider than it
    was measured."""
    if _KERNEL_RE.search(text):
        return "kernel"
    if _CANDIDATE_RE.search(text):
        return "candidate"
    return ""


class _Sites(ast.NodeVisitor):
    """One row per identity-bearing SITE, with its read/write direction.

    Direction comes from the AST context, never from the spelling: a Store on
    the left of an assignment is a WRITE and a Load is a READ. Reading it off
    the name (set_/get_, _write, save) is the convention-not-measurement
    mistake this module is named against.
    """

    def __init__(self, node_id, lines):
        self.node, self.lines, self.rows = node_id, lines, []

    def _line(self, lineno):
        return self.lines[lineno - 1] if 0 < lineno <= len(self.lines) else ""

    def _add(self, tok, lineno, ctx):
        noun = _OWNER.get(tok)
        if noun is None:
            return
        text = self._line(lineno).strip()
        self.rows.append({
            "node": self.node, "line": lineno, "token": tok,
            "noun": noun,
            "state": ASSIGNED,          # revised per SITE by _mark_ambiguous
            "nouns_here": None,
            "dir": "write" if isinstance(ctx, ast.Store) else "read",
            "tier": _tier(text),
            # MEASURED over this tree: 91 of 282,467 source lines are
            # longer than this, so a cut here is rare — and the longest runs
            # to ten thousand characters, which is why the bound stays rather
            # than the whole line being kept. A census row that reads as the
            # whole source line is a claim about code nobody can check
            # against the file, so the rare cut names both sizes.
            "text": pk.cut_marked(text, 160),
        })

    def visit_Attribute(self, node):          # row.actor_id
        self._add(node.attr, node.lineno, node.ctx)
        self.generic_visit(node)

    def visit_Name(self, node):               # seat = ...
        self._add(node.id, node.lineno, node.ctx)
        self.generic_visit(node)

    def visit_Subscript(self, node):          # row["seat"]
        key = node.slice
        if isinstance(key, ast.Constant) and isinstance(key.value, str):
            self._add(key.value, node.lineno, node.ctx)
        self.generic_visit(node)

    def visit_keyword(self, node):            # f(seat=...)
        if node.arg:
            self._add(node.arg, node.value.lineno, ast.Load())
        self.generic_visit(node)

    def visit_Constant(self, node):           # an env-var key used as a string
        if isinstance(node.value, str) and node.value in _OWNER:
            self._add(node.value, node.lineno, ast.Load())


def _mark_ambiguous(rows):
    """Second pass, because ambiguity is only visible once a SITE is whole.

    A single token cannot know it shares a line with another noun, so the
    visitor must not guess it. Every row on a multi-noun line is marked and
    carries the FULL set: a reader seeing only "seat" on an ambiguous row
    would believe the line was resolved."""
    by_site = {}
    for row in rows:
        by_site.setdefault((row["node"], row["line"]), set()).add(row["noun"])
    for row in rows:
        here = by_site[(row["node"], row["line"])]
        if len(here) > 1:
            row["state"] = AMBIGUOUS
            row["nouns_here"] = sorted(here)
    return rows


# THE REAL TREE IS CENSUSED ONCE PER PROCESS FOR EACH OWNER TABLE (task/3039).
# Measured before: 13 real-tree censuses in tests.test_nouncensus at about
# 8.3 s profiled each, 90% of the module. The key is the IDENTITY of `_OWNER`,
# held here so no other table can ever reuse its id: a caller that patches the
# table (the seven-noun comparison does) gets that table's census, and the real
# table's is still the real one afterwards. A planted `root` is censused on
# every call, and every answer is handed out as a copy.
_REAL = []          # [(owner table, rows, unparsed)], newest last

# THE SLICE RUNNER'S DATA AUDIT (helm/gateslice.py) reports any module
# data a test unit leaves behind; these names are process-wide by design.
_GATESLICE_MUTABLE = {
    "_REAL": (
        "the real tree's census, once per process per owner table "
        "(task/3039)"),
}
_REAL_KEEP = 4


def census(root=None):
    """(rows, unparsed) — every identity-bearing site under helm/.

    Modules come from wiring.modules() and are named by wiring.node_name, so
    this census and the write-path guard cannot disagree about what a file is
    called; that resolver's own docstring records the two times they did.

    UNREADABLE IS RETURNED, NOT SWALLOWED. A module that will not parse is a
    hole in the closure, and a census that drops it reports a smaller, cleaner
    world than the one it measured.
    """
    if root:
        return _census(root)
    owner = _OWNER
    hit = next((row for row in _REAL if row[0] is owner), None)
    if hit is None:
        hit = (owner,) + _census(None)
        _REAL[:] = _REAL[-(_REAL_KEEP - 1):] + [hit]
    return ([{k: list(v) if isinstance(v, list) else v for k, v in r.items()}
             for r in hit[1]], [dict(u) for u in hit[2]])


def _census(root):
    rows, unparsed = [], []
    for name, path in sorted(wiring.modules(root).items()):
        try:
            with open(path, encoding="utf-8") as fh:
                src = fh.read()
            tree = ast.parse(src)
        except (OSError, SyntaxError, ValueError) as exc:
            unparsed.append({"node": name, "why": "%s: %s"
                               % (type(exc).__name__, exc)})
            continue
        visitor = _Sites(name, src.splitlines())
        visitor.visit(tree)
        rows.extend(visitor.rows)
    return _mark_ambiguous(rows), unparsed


def summary(rows, unparsed):
    """Per-noun counts plus the two states a reader must not miss."""
    out = {}
    for noun in NOUNS:
        mine = [r for r in rows if r["noun"] == noun]
        out[noun] = {
            "sites": len(mine),
            "readers": sum(r["dir"] == "read" for r in mine),
            "writers": sum(r["dir"] == "write" for r in mine),
            "modules": len({r["node"] for r in mine}),
            "ambiguous": sum(r["state"] == AMBIGUOUS for r in mine),
            "candidate_tier": sum(r["tier"] == "candidate" for r in mine),
            "kernel_tier": sum(r["tier"] == "kernel" for r in mine),
        }
    sites = {(r["node"], r["line"]) for r in rows}
    out["_sites"] = len(sites)
    out["_ambiguous_sites"] = len(
        {(r["node"], r["line"]) for r in rows if r["state"] == AMBIGUOUS})
    out["_unreadable"] = len(unparsed)
    return out


_USAGE = """usage: helm nouncensus [--noun N] [--ambiguous] [--tier kernel|candidate] [--json]
  The per-site uses-closure census for the seat-state nouns (task/1300 slice 2).
  Bare: the per-noun table. --ambiguous lists the sites whose tokens span more
  than one noun — the "assigned exactly once" violations. --tier filters by R2
  proof tier, so `--tier candidate` is "attributions resting on evidence the
  subject could have forged". An unknown flag REFUSES rather than silently
  widening the answer."""


def cmd_nouncensus(args):
    """helm nouncensus — who reads and writes each seat-state noun, measured."""
    import json as _json
    if "--help" in args or "-h" in args:
        print(_USAGE)
        return 0
    from .cli import guard_tail
    # THE FULL TAIL, NOT A FILTERED ONE. Passing only the "--" tokens strips
    # the VALUES off every valued flag, so guard_tail sees `--tier` with
    # nothing after it and refuses "--tier wants a value" on a call that
    # supplied one. Measured on trunk the minute this verb landed: both
    # --noun and --tier were dead, and I had documented them in VERBS.md.
    # Every other verb in the tree (attribute, autocompact, beacons) passes
    # `args` whole; the filtered spelling was copied from punt.py, where it is
    # equally broken and had been for longer.
    rc = guard_tail("helm nouncensus", args,
                    flags=("--json", "--ambiguous"), valued=("--noun", "--tier"),
                    usage=_USAGE)
    if rc is not None:
        return rc
    want_noun = args[args.index("--noun") + 1] if "--noun" in args else None
    want_tier = args[args.index("--tier") + 1] if "--tier" in args else None
    if want_noun and want_noun not in NOUNS:
        print("helm nouncensus: unknown noun %r — known: %s"
              % (want_noun, ", ".join(sorted(NOUNS))), file=_sys.stderr)
        return 2
    # THE SIBLING VALIDATION, whose absence made a TYPO indistinguishable from
    # a real empty result: `--tier nosuchtier` exited 0 printing "0 sites",
    # the same observable a correct filter with nothing to show produces. So a
    # misspelling read as "no attributions rest on forgeable evidence", which
    # is the OPPOSITE of what R2 exists to say. A probe measured it on the
    # very tip that fixed --noun and left this one open — validating one
    # alternative and not its sibling makes the whole verb read as finished.
    if want_tier and want_tier not in TIERS:
        print("helm nouncensus: unknown tier %r — known: %s"
              % (want_tier, ", ".join(sorted(TIERS))), file=_sys.stderr)
        return 2

    rows, unparsed = census()
    if want_noun:
        rows = [r for r in rows if r["noun"] == want_noun]
    if want_tier:
        rows = [r for r in rows if r["tier"] == want_tier]
    if "--ambiguous" in args:
        rows = [r for r in rows if r["state"] == AMBIGUOUS]

    if "--json" in args:
        print(_json.dumps({"rows": rows, "unparsed": unparsed}))
        return 0

    if want_noun or want_tier or "--ambiguous" in args:
        for r in rows:
            here = ("  [%s]" % ",".join(r["nouns_here"])) if r["nouns_here"] else ""
            print("  %-22s:%-5d %-5s %-11s %-9s %s%s"
                  % (r["node"], r["line"], r["dir"], r["noun"],
                     r["tier"] or "-", r["text"][:60], here))
        print("helm nouncensus: %d site%s" % (len(rows), "s"[:len(rows) != 1]))
        return 0

    s = summary(rows, unparsed)
    print("  %-11s %7s %7s %7s %8s %10s %10s"
          % ("noun", "sites", "read", "write", "modules", "ambiguous", "candidate"))
    for noun in sorted(NOUNS):
        v = s[noun]
        print("  %-11s %7d %7d %7d %8d %10d %10d"
              % (noun, v["sites"], v["readers"], v["writers"], v["modules"],
                 v["ambiguous"], v["candidate_tier"]))
    print("  %d sites, %d AMBIGUOUS (%.1f%%) — a site whose tokens span more "
          "than one noun is an 'assigned exactly once' violation"
          % (s["_sites"], s["_ambiguous_sites"],
             100.0 * s["_ambiguous_sites"] / max(1, s["_sites"])))
    # UNREADABLE IS PRINTED, NEVER DROPPED: a hole in the closure that
    # disappears makes this report a smaller, cleaner world than the measured one.
    if unparsed:
        print("  %d module%s UNREADABLE — the closure has holes:"
              % (len(unparsed), "s"[:len(unparsed) != 1]), file=_sys.stderr)
        for u in unparsed:
            print("    %-22s %s" % (u["node"], u["why"]), file=_sys.stderr)
    return 0
