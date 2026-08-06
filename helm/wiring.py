#!/usr/bin/env python3
"""helm.wiring — the built/wired/exercised ladder, measured instead of promised.

THE RULE ALREADY EXISTED AND STILL FAILED, which is the whole reason this is
code and not another premise. `feature-and-rsh-must-both-be-wired` sits in the
store at confidence 1.00 and says, in the owner's words, that this is THE
reason the vibe-coding world hits 90%-done-never-100%. `closed-is-the-full-loop`
says done is a loop, not an arrow. Both were live, both were canon, and on
2026-07-29 three separate instances shipped anyway:

  * `helm/silent_drop.py` — the detector for the exact token-drop the owner had
    been reporting for a week. Fully written, its own test suite, root-cause
    documented from 75 observed cases. ZERO callers. Not in SPECS, not in
    DELIVERY_SPECS, not in any installed hook. Every "fix round" since it
    landed ran without the instrument that would say whether it worked.
  * `resumeturn._registered` — the compaction resume leg stopped at
    `_seat_family`'s error while `orcaadopt.resolve()` answered correctly for
    the same seat in the same minute. 53 of 59 seats could not resume.
  * the ds4pro keepalive — fixed at the generator, never checked on the files
    that were never regenerated.

A rule an agent must REMEMBER to apply is enforced by vigilance, and vigilance
does not scale past the surfaces someone happens to look at. So: a census.

THE LADDER, and each rung is a different failure:

  BUILT       the code exists.                     (nothing to measure)
  REACHABLE   an import path reaches it from an    R1 UNREACHABLE
              entry point.
  ACTUATED    an enabled schedule or executable    R4 NO ACTUATOR
              hook invokes the declared action.
  EXERCISED   it actually ran.                     R3 UNEXERCISED
  VERIFIED    something asserted its effect.       R2 UNTESTED

WHY R1 IS REACHABILITY AND NOT "HAS AN IMPORTER". A module imported only by
another dead module is still dead, and a cluster of three mutually-importing
dead modules would pass a has-an-importer check unanimously. Reachability from
the real entry points is the only formulation that cannot be satisfied by dead
code vouching for dead code.

HONEST LIMITS, stated because a census that overclaims is worse than none:
imports are read STATICALLY, so a module reached only through `importlib` or a
string-keyed dispatch table reads as unreachable and belongs in ALLOWED with a
reason. R3 reads a LOSSY, ROTATING telemetry trail (pk's fire-ledger is
explicitly not a durable record), so "unexercised" means "no receipt in the
window we still have" and is reported as weaker evidence than R1 — never as
proof that something never ran. R4 is declaration-backed: Python cannot infer
which useful functions ought to run unattended. ACTUATORS names that contract;
the census then proves it against installed action surfaces rather than trusting
the declaration itself.
"""
import ast
import glob
import json
import os
import re
import subprocess

# The real front doors. `cli` dispatches every verb; `__main__` is `python -m
# helm`. Anything a hook runs, it runs through a cli verb, so hooks need no
# separate entry — which is itself worth knowing: a hook that named a module
# cli could not reach would be a broken hook.
ENTRIES = ("cli", "__main__")

# Modules that are legitimately unreachable by STATIC import, each with the
# reason. An entry here is a claim someone made on purpose; an empty reason is
# not accepted. This list is the pressure: a new module either gets wired or
# gets defended in writing, and "I forgot" is not available as an outcome.
ALLOWED = {
    "__init__": "the package marker; imported by the interpreter, not by code",
    "nevertrack": "reached OUTSIDE the import graph on purpose, twice over: "
                  "the composed pre-commit guard snapshots this file beside "
                  "the shared hook and runs that stable copy as a plain "
                  "`python3 nevertrack.py --staged` script, so the scan works "
                  "with no importable helm package and an installing lane "
                  "cannot edit or delete the live detector. Two import edges "
                  "DO exist but "
                  "sit where this census cannot see them: work/_guard.py's "
                  "_scanner_path (a subpackage file — the module-granular "
                  "scan reads only work/__init__.py) and "
                  "tests/test_never_track.py's law import (tests are not "
                  "entry points). The never-track-pre-commit obligation now "
                  "re-reads the executable installed hook; prose here cannot "
                  "prove it. Exercised end-to-end by "
                  "tests/test_never_track_hook.py.",
    "hostpath_guard": "same CLASS as nevertrack (line above). Reached "
                      "OUTSIDE the import graph: the composed pre-push guard "
                      "snapshots this file beside the shared hook and runs "
                      "that stable copy as `python3 hostpath_guard.py "
                      "--pre-push`, so it works with no importable helm package "
                      "and an installing lane cannot mutate the live guard. "
                      "The import edge sits in "
                      "work/_guard.py's _hostpath_scanner_path() — invisible "
                      "to module-granular scan. The hostpath-pre-push "
                      "obligation re-reads the executable installed hook; an "
                      "uninstalled promise is reported. Exercised by "
                      "tests/test_hostpath_guard.py.",
    "inflight_gate": "same stable-snapshot class as nevertrack. The "
                     "pre-commit hook SNAPSHOTS it beside itself at install "
                     "time and runs `python3 inflight_gate.py` as a "
                     "subprocess, so a lane worktree cannot edit or delete "
                     "the rung that polices its own commit — which is the "
                     "whole point of the snapshot law. Nothing imports it; "
                     "helm/gate.py owns the READ side (inflight()) and this "
                     "module is only the hook's entry point. Exercised by "
                     "tests/test_inflight_gate.py.",
    "vacuous_assertion": "same stable-snapshot class as nevertrack. The "
                         "composed pre-commit hook runs an installed copy as "
                         "`python3 vacuous_assertion.py --staged` before "
                         "never-track; the author lane is never the live "
                         "detector that judges its own staged tests. The "                         "vacuous-assertion-pre-commit obligation re-reads the "
                         "installed hook. Exercised end-to-end by "
                         "tests/test_never_track_hook.py.",
    "hardcode": "same stable-snapshot class as nevertrack (above). The "
                "composed pre-commit hook snapshots this file beside the "
                "shared hook and runs it as `python3 hardcode.py --staged`, "
                "WARN-only ahead of the refusing never-track scan — a "
                "warn-rung must work with no importable helm package and a "
                "lane cannot edit its own checked-out copy to dodge the rung "
                "for its own commit. The import edge sits in work/_guard.py's "
                "_hardcode_rung_path() — invisible to module-granular scan. "
                "Exercised by tests/test_hardcode.py.",
    "conflict_marker": "same stable-snapshot class as nevertrack (above). The "
                       "composed pre-commit hook snapshots this file beside "
                       "the shared hook and runs it as `python3 "
                       "conflict_marker.py --staged`, REFUSING staged "
                       "merge-conflict marker lines ahead of never-track and "
                       "under its own skip (HELM_CONFLICT_MARKER_SKIP=1) — "
                       "the rung must work with no importable helm package "
                       "and a lane cannot edit the detector that judges its "
                       "own staged markers. The import edge sits in "
                       "work/_guard.py's _conflict_marker_rung_path() — "
                       "invisible to module-granular scan. Exercised "
                       "end-to-end by tests/test_conflict_marker.py.",
    "lane_discipline": "same stable-snapshot class as nevertrack (above). The "
                       "composed pre-commit hook snapshots this file beside "
                       "the shared hook and runs it FIRST as `python3 "
                       "lane_discipline.py --staged`, REFUSING a commit that "
                       "ORIGINATES work on the shared checkout's base branch "
                       "— the VENUE check, ahead of every content rung, under "
                       "its own skip (HELM_LANE_DISCIPLINE_SKIP=1) and the "
                       "integrator declaration (HELM_WORK_INTEGRATOR=1). It "
                       "must work with no importable helm package, and a lane "
                       "must not be able to edit the rung that judges where "
                       "its own commit is being made. The import edge sits in "
                       "work/_guard.py's _lane_discipline_rung_path() — "
                       "invisible to module-granular scan. Exercised "
                       "end-to-end by tests/test_lane_discipline.py.",
}

# Static reachability cannot prove an OUTSIDE-the-package claim. Every such
# exemption names an installed consumer obligation; otherwise ALLOWED would be
# a permanent promise nobody re-checks — the exact class this census exists to
# expose. __init__ is intrinsic interpreter machinery, not an external action.
INTRINSIC_ALLOWED = {"__init__"}
ALLOWED_ACTUATORS = {
    "nevertrack": "never-track-pre-commit",
    "hostpath_guard": "hostpath-pre-push",
    "vacuous_assertion": "vacuous-assertion-pre-commit",
    "hardcode": "hardcode-pre-commit",
    "conflict_marker": "conflict-marker-pre-commit",
    "lane_discipline": "lane-discipline-pre-commit",
}

# A detector/maintenance path is not shipped merely because a person can type
# its verb. These are the declared actions that must have an installed,
# executable consumer. Kinds are explicit because a source-tree mention or an
# inert unit file cannot vouch for a schedule or hook.
ACTUATORS = {
    "never-track-pre-commit": {
        "tokens": ("nevertrack.py",),
        "line_tokens": ("exec", "python3", "$scanner", "--staged"),
        "kinds": ("git-hook",), "names": ("pre-commit",),
    },
    "hardcode-pre-commit": {
        "tokens": ("hardcode.py", "--staged"),
        "line_tokens": ("python3", "$hardcode_rung", "--staged", "||", "true"),
        "kinds": ("git-hook",), "names": ("pre-commit",),
    },
    "hostpath-pre-push": {
        "tokens": ("hostpath_guard.py",),
        "line_tokens": ("exec", "python3", "$scanner", "--pre-push"),
        "kinds": ("git-hook",), "names": ("pre-push",),
    },
    "vacuous-assertion-pre-commit": {
        "tokens": ("vacuous_assertion.py",),
        "line_tokens": ("python3", "$vacuous", "--staged", "||", "true"),
        "kinds": ("git-hook",), "names": ("pre-commit",),
    },
    "conflict-marker-pre-commit": {
        "tokens": ("conflict_marker.py",),
        # a REFUSING rung's invocation must sit on the refusal path: the
        # whole line IS the exit-propagating command (exec form, or
        # `|| exit $?`) — `... || true` and `exit 0; ...` prefixes fail the
        # fullmatch, and _reachable_lines drops everything below a top-level
        # unconditional exit.
        "refusal_line": r'exec\s+python3\s+\S*conflict\S*\s+--staged'
                        r'|python3\s+\S*conflict\S*\s+--staged'
                        r'\s+\|\|\s+exit\s+\$\?',
        "kinds": ("git-hook",), "names": ("pre-commit",),
    },
    "lane-discipline-pre-commit": {
        "tokens": ("lane_discipline.py",),
        # same refusal-path law as conflict-marker: the invocation line must
        # itself propagate the rung's exit status, so a `|| true` demotion to
        # advisory fails the fullmatch instead of passing silently.
        "refusal_line": r'exec\s+python3\s+\S*lane_discipline\S*\s+--staged'
                        r'|python3\s+\S*lane_discipline\S*\s+--staged'
                        r'\s+\|\|\s+exit\s+\$\?',
        "kinds": ("git-hook",), "names": ("pre-commit",),
    },
    "corpus-backup": {
        "tokens": ("helm corpus backup",),
        "kinds": ("systemd", "crontab", "claude-hook", "mcp"),
    },
    "worktree-gc": {
        "tokens": ("helm work gc --apply",),
        "kinds": ("systemd", "crontab", "claude-hook", "mcp"),
    },
    # THE OWNER-DECISION RETRY EDGE (task/232). `helm beacons --post` flushes
    # decision cards whose push never left the box — and I WROTE that it "rides
    # an EXISTING PERIODIC PASS ... already runs on a schedule" WITHOUT EVER
    # MEASURING WHETHER IT RUNS HERE. @offbox-claude's base-interaction refute
    # measured it: zero beacons timers, zero cron rows, zero hook references on
    # this box. helm-beacons.timer is INSTALLABLE (beacons.ensure_timer) and was
    # never installed, so the edge existed in code and fired never — task/232's
    # own dependency-on-noticing premise, un-cured one layer down at deployment.
    #
    # DECLARING IT HERE RATHER THAN MINTING A DETECTOR: check_actuator_wiring
    # already exists and already asks exactly this question — "does every
    # declared action have an installed scheduled or hooked reader" — so the
    # cure is to make the census SEE this obligation, not to build a second
    # census beside it. An unarmed timer now reports NO ACTUATOR by the same
    # path that catches every other built-not-wired action.
    "owner-decision-flush": {
        "tokens": ("helm beacons --post",),
        "kinds": ("systemd", "crontab", "claude-hook", "mcp"),
    },
    "dispatch-mix": {
        "tokens": ("helm dispatch mix",),
        "kinds": ("systemd", "crontab", "claude-hook", "mcp", "chat-verb"),
    },
    # The stranded-obligation re-route (task #42, codex FIX on 6ccc7347:
    # "rebind exists, but ACTUATORS lacks dispatch-rebind and only the CLI
    # calls it"). The verb is evidence-gated PRECISELY so an unattended
    # caller is safe — and then nothing unattended called it, which this
    # census could not say because no obligation named it. Quiet-by-omission
    # is the sensor-with-no-actuator disease one layer up: declared here, the
    # missing scheduler reads NO ACTUATOR until a real consumer (stall
    # watcher hook, timer, chat verb) actually invokes the verb.
    "dispatch-rebind": {
        "tokens": ("helm dispatch rebind",),
        # The parser REQUIRES a row id and --to; a bare
        # `helm dispatch rebind --json` exits 2 before the verb runs, so a
        # consumer carrying only the verb words is a sensor wired to nothing
        # (codex blocker 2 on 6a8f9530). The admission is the CLI arm's OWN
        # (dispatches.rebind_argv_ok) — a second partial grammar here
        # certified four parser-refused shapes (codex meld e:1785584307).
        # chat-verb is DELIBERATELY absent (codex r5, gate 80f95d7e): those
        # rows are synthetic `helm chat <verb>` text and can never satisfy
        # `helm dispatch rebind` — listing the kind made the census pretend
        # a surface existed that no chat verb can ever be.
        "argv_ok": "dispatch-rebind",
        "kinds": ("systemd", "crontab", "claude-hook", "mcp"),
    },
}


def _module_name(path):
    return os.path.splitext(os.path.basename(path))[0]


def node_name(rel):
    """THE path -> graph-node resolver, given a path relative to helm/.

      helm/cli.py            -> "cli"
      helm/clarity/__init__.py -> "clarity"      (the PACKAGE, not __init__)
      helm/clarity/rules.py  -> "clarity.rules"

    ONE function because the census and the WRITE-PATH guard must agree on what
    a file is called. They have now disagreed twice, both times failing toward
    SILENCE: first a new subpackage keyed as `__init__` never intersected the
    census, and then a new SUBMODULE keyed by its bare stem missed the dotted
    node the census had just started using. Both let an unwired addition slip
    the Stop gate, and both were found by cross-review rather than by use —
    which is what "fails toward silence" means in practice."""
    parts = [p for p in str(rel).replace(os.sep, "/").split("/") if p]
    if not parts or not parts[-1].endswith(".py"):
        return None
    stem = parts[-1][:-3]
    if len(parts) == 1:
        return stem
    return parts[-2] if stem == "__init__" else "%s.%s" % (parts[-2], stem)


def modules(root=None):
    """{name: path} for every module in the helm package."""
    root = root or os.path.dirname(os.path.abspath(__file__))
    out = {}
    for p in sorted(glob.glob(os.path.join(root, "*.py"))):
        out[_module_name(p)] = p
    for p in sorted(glob.glob(os.path.join(root, "*", "__init__.py"))):
        out[os.path.basename(os.path.dirname(p))] = p
    # SUBMODULES ARE THEIR OWN NODES, named `package.module`. Without them a
    # package is a single opaque node and its dead files are invisible.
    for p in sorted(glob.glob(os.path.join(root, "*", "*.py"))):
        if os.path.basename(p) == "__init__.py":
            continue
        out[node_name(os.path.relpath(p, root))] = p
    return out


def _package_of(name, path):
    """The dotted package a file's RELATIVE imports resolve against.

    For `helm/clarity/rules.py` (node 'clarity.rules') that is 'clarity'; for
    `helm/clarity/__init__.py` (node 'clarity') it is also 'clarity', because
    a package's own __init__ resolves `from .rules import x` against itself.
    Top-level modules resolve against the helm root, spelled ''."""
    if "." in name:
        return name.rpartition(".")[0]
    if os.path.basename(path) == "__init__.py" and name != "__init__":
        return name
    return ""


def _str_arg(node, i=0):
    """The i-th argument of a call when it is a plain string literal."""
    if len(node.args) > i and isinstance(node.args[i], ast.Constant) \
            and isinstance(node.args[i].value, str):
        return node.args[i].value
    return None


def _join(base, tail):
    return "%s.%s" % (base, tail) if base else tail


def _relative_base(pkg, level):
    """Where a relative import lands. `from . import x` inside helm/clarity/
    resolves against 'clarity'; `from .. import x` against the helm root ('')."""
    base = pkg
    for _ in range(level - 1):
        base = base.rpartition(".")[0]
    return base


def _candidates(dotted):
    """A dotted target and its package. `known` decides which exists, so both
    are offered: 'clarity.rules' when the submodule is a node, 'clarity' when
    the reference is to something the package re-exports."""
    return {dotted, dotted.split(".")[0]} if dotted else set()


def imports_of(path, known, pkg=""):
    """The intra-package modules this file imports OR dispatches to.

    ast, not regex: helm imports inside functions constantly (the lazy-import
    idiom that keeps CLI startup cheap), and those are exactly as load-bearing
    as top-level ones. A line-based scan would also count a module named in a
    docstring or a comment, which is how a census starts lying in the
    reassuring direction.

    STRING-KEYED DISPATCH IS A REAL EDGE, and missing it is what made the first
    draft of this module useless. helm's entire verb surface is
    `_lazy("<module>", "<fn>")` — a table of string-keyed lazy imports, so that
    `helm projects` never pays for the web server's imports. A pure
    import-statement scan reported 31 of 83 modules unreachable, including
    `doctor`, `web`, `landreq` and `drain`, every one of which had been run by
    hand minutes earlier. A census whose false-positive rate is 37% does not
    get read twice, and its silence would then mean nothing.

    So `_lazy(...)` and a literal `import_module("helm.X")` are followed as
    edges. They are indirection, not absence — the difference between "nothing
    reaches this" and "I do not understand how this is reached" is the entire
    value of the measurement.
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            tree = ast.parse(f.read(), filename=path)
    except (OSError, SyntaxError):
        return set()
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level:
                base = _relative_base(pkg, node.level)
                if node.module is None:                 # from . import a, b
                    out.update(_join(base, a.name) for a in node.names)
                else:                                   # from .mod import x
                    out.update(_candidates(_join(base, node.module)))
            elif node.module and node.module.startswith("helm."):
                out.update(_candidates(node.module.split(".", 1)[1]))
        elif isinstance(node, ast.Import):
            for a in node.names:                        # import helm.mod
                if a.name.startswith("helm."):
                    out.update(_candidates(a.name.split(".", 1)[1]))
        elif isinstance(node, ast.Call):
            fn = node.func
            name = (fn.id if isinstance(fn, ast.Name)
                    else fn.attr if isinstance(fn, ast.Attribute) else "")
            if name == "_lazy":                          # the verb table
                got = _str_arg(node)
                if got:
                    out.add(got)
            elif name == "import_module":                # importlib, literal
                got = _str_arg(node)
                if got and got.startswith("helm."):
                    out.update(_candidates(got.split(".", 1)[1]))
    return {m for m in out if m in known}


def graph(root=None):
    """{module: {modules it imports}} over the whole package.

    A PACKAGE SUBMODULE IS ITS OWN NODE, and that is the whole subtlety here.
    Two wrong models were tried first, in this order:

      1. A package's edges = its __init__.py's edges. This DROPPED
         helm/clarity/rules.py's `from .. import helmese`, so the census
         accused a module the clarity die imports on every owner-mode check of
         being "built, not wired" (#216).
      2. A package's edges = the UNION of every file it contains. This is
         worse, and a reviewer caught it before it landed: an UNIMPORTED dead
         submodule would then vouch for everything it imports. Synthetic
         repro, now a negative control below — cli -> pkg/__init__ (empty)
         plus pkg/dead.py importing `orphan` made `orphan` read as reached
         although nothing imports dead.py. A census that launders its own dead
         cluster is worse than no census.

    So submodules are real nodes with real intra-package edges: `clarity`
    reaches `clarity.rules` because __init__ imports it, and `clarity.rules`
    reaches `helmese`. A dead submodule is simply unreached, which is exactly
    what it is."""
    mods = modules(root)
    return {name: imports_of(path, mods, _package_of(name, path))
            for name, path in mods.items()}


def reachable(g, entries=ENTRIES):
    """Every module reachable from the entry points, transitively."""
    seen, stack = set(), [e for e in entries if e in g]
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        stack.extend(g.get(cur, ()) - seen)
    return seen


def tested(root=None):
    """Modules some test file imports — the VERIFIED rung's cheap proxy.

    Cheap and honest about being a proxy: importing a module in a test is not
    proof that anything asserts its behaviour. It is, however, a hard floor —
    a module no test even names is certainly unverified.
    """
    root = root or os.path.dirname(os.path.abspath(__file__))
    tests = os.path.join(os.path.dirname(root), "tests")
    known, out = modules(root), set()
    for p in glob.glob(os.path.join(tests, "*.py")):
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                src = f.read()
        except OSError:
            continue
        for m in re.findall(r"from helm import ([A-Za-z0-9_, ]+)", src):
            out.update(x.strip() for x in m.split(","))
        # DOTTED PATHS, against the SAME node identity graph() uses. Collapsing
        # every import to its first component made `from helm.clarity import
        # rules` credit only `clarity`, so every submodule node read as untested
        # the moment submodules became nodes — 36 of them at once, and census()
        # publishes live-minus-tested to the owner.
        for dotted, names in re.findall(
                r"from helm\.([A-Za-z0-9_.]+) import ([A-Za-z0-9_, *]+)", src):
            out.add(dotted)
            out.add(dotted.split(".")[0])
            for n in names.split(","):     # from helm.clarity import rules
                n = n.strip()
                if n and n != "*":
                    out.add("%s.%s" % (dotted, n))
        for dotted in re.findall(r"import helm\.([A-Za-z0-9_.]+)", src):
            out.add(dotted)
            out.add(dotted.split(".")[0])
    out &= set(known)
    return out | _reached_within(out, known, root)


def _reached_within(named, known, root=None):
    """Submodules a named PACKAGE actually reaches — never merely co-located.

    A test that says `from helm import store` and drives store's public surface
    DOES exercise store.cli; the submodule is simply never spelled. Measured at
    the moment submodules became nodes: 21 of them landed in `untested` and ALL
    21 were reachable from a package a test names, so publishing them would have
    turned a 3-finding census into a 24-finding one with 21 false alarms — which
    is how a census stops being read.

    THE DISTINCTION IS THE ONE THE REACHABILITY RUNG DRAWS, and it is why this
    is not the vouching that was rejected there: credit follows the package's
    own IMPORT CLOSURE, so a dead submodule nobody imports stays uncredited.
    Being in the directory earns nothing."""
    g = graph(root)
    out = set()
    for pkg in named:
        if "." in pkg:
            continue
        seen, stack = set(), [pkg]
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            stack.extend(g.get(cur, set()) - seen)
        out |= {m for m in seen
                if m.startswith(pkg + ".") and m in known}
    return out


def exercised(path=None, limit=200000):
    """Module-ish names that appear in the runtime event trail.

    WEAKER EVIDENCE THAN THE OTHER RUNGS, deliberately: pk's journal is a
    lossy, rotating, host-local trail that its own docstring refuses to call a
    durable record. Absence here means "no receipt in the window we still
    have", never "this never ran", and the census labels it that way.
    """
    from . import pk
    path = path or pk.events_path()
    out = set()
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for i, ln in enumerate(f):
                if i > limit:
                    break
                try:
                    row = json.loads(ln)
                except ValueError:
                    continue
                verb = str(row.get("verb") or "")
                out.update(re.split(r"[.\-_ :]+", verb))
    except OSError:
        return None            # no trail readable: UNKNOWN, never "nothing ran"
    return {m for m in out if m}


def _active_lines(text, shell_comments=False):
    """Nonblank executable/config lines — prose cannot impersonate action."""
    out = []
    for line in str(text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if shell_comments:
            line = line.split(" #", 1)[0].rstrip()
        if line:
            out.append(line)
    return out


def _read(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def _settings_candidates(home_dir, repo):
    pats = (
        os.path.join(home_dir, ".claude", "settings.json"),
        os.path.join(home_dir, ".claude-homes", "*", "settings.json"),
        os.path.join(home_dir, ".helm", "_global", "seats", "*",
                     "claude", "settings.json"),
        os.path.join(home_dir, ".claude.json"),
        os.path.join(repo, ".mcp.json"),
    )
    return sorted({p for pat in pats for p in glob.glob(pat)})


def _dicts_named(node, key):
    out = []
    if isinstance(node, dict):
        if isinstance(node.get(key), dict):
            out.append(node[key])
        for value in node.values():
            out += _dicts_named(value, key)
    elif isinstance(node, list):
        for value in node:
            out += _dicts_named(value, key)
    return out


def _mcp_argvs(node):
    """mcpServers entries as STRUCTURED argv ([command, *args]) — flattened
    text cannot recover argv boundaries, so `{command: "echo", args: [">",
    "helm", …]}` joined to a string read as shell and censused WIRED for a
    verb echo would only ever print (codex r5, gate 80f95d7e). The row keeps
    a joined text for token matching; the argv list is the grammar."""
    out = []
    if isinstance(node, dict):
        servers = node.get("mcpServers")
        if isinstance(servers, dict):
            for cfg in servers.values():
                if not isinstance(cfg, dict) \
                        or not isinstance(cfg.get("command"), str):
                    continue
                args = cfg.get("args") if isinstance(cfg.get("args"), list) else []
                # The MCP args contract is string[] — coercing a non-string
                # with str() manufactured valid-looking argv from a
                # malformed config (codex terminal pass: a numeric row id
                # censused WIRED). A row breaking the contract is rejected
                # whole, never repaired.
                if any(not isinstance(a, str) for a in args):
                    continue
                out.append([cfg["command"]] + list(args))
        for value in node.values():
            out += _mcp_argvs(value)
    elif isinstance(node, list):
        for value in node:
            out += _mcp_argvs(value)
    return out


def _default_hook_dir(repo):
    try:
        from .work import _guard
        return os.path.dirname(_guard.hook_path(repo, "pre-commit"))
    except Exception:
        return None


def consumer_census(repo=None, hook_dir=None, unit_dir=None, crontab=None,
                    settings_paths=None, home_dir=None):
    """Installed action surfaces -> rows plus source kinds that were UNKNOWN.

    Only executable hooks, enabled systemd units, active crontab lines, live
    Claude hook JSON, MCP command definitions, and registered chat verbs count.
    Shipped unit files and comments are documentation, not actuators.
    """
    repo = repo or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    home_dir = home_dir or os.path.expanduser("~")
    hook_dir = hook_dir if hook_dir is not None else _default_hook_dir(repo)
    unit_dir = unit_dir or os.path.join(home_dir, ".config", "systemd", "user")
    settings_paths = (_settings_candidates(home_dir, repo)
                      if settings_paths is None else settings_paths)
    rows, unknown = [], set()

    # TRI-STATE on the hook estate: an ABSENT directory honestly has no
    # hooks (obligations there are MISSING); a directory that cannot be READ
    # is UNKNOWN, never missing — `isdir`/`exists` swallow EACCES into False,
    # so a permission-denied estate used to read as absent and every hook
    # obligation reported MISSING for a state nobody had measured.
    if hook_dir is None:
        names = ()
        unknown.add("git-hook")
    else:
        try:
            names = sorted(os.listdir(hook_dir))
        except FileNotFoundError:
            names = ()
        except OSError:
            names = ()
            unknown.add("git-hook")
    for name in names:
        path = os.path.join(hook_dir, name)
        if name.endswith(".sample") or not os.path.isfile(path) \
                or not os.access(path, os.X_OK):
            continue
        text = _read(path)
        if text is None:
            unknown.add("git-hook")
            continue
        rows.append({"kind": "git-hook", "name": name,
                     "text": "\n".join(_active_lines(
                         text, shell_comments=True))})

    if os.path.isdir(unit_dir):
        try:
            links = sorted(glob.glob(os.path.join(
                unit_dir, "*.target.wants", "*")))
        except OSError:
            links = []
            unknown.add("systemd")
        seen = set()
        for link in links:
            if not os.path.exists(link):
                continue
            unit = os.path.basename(link)
            service = (unit[:-6] + ".service"
                       if unit.endswith(".timer") else unit)
            if not service.endswith(".service") or service in seen:
                continue
            seen.add(service)
            candidates = [os.path.join(unit_dir, service)]
            target_dir = os.path.dirname(os.path.realpath(link))
            candidates.append(os.path.join(target_dir, service))
            text = next((got for got in (_read(p) for p in candidates)
                         if got is not None), None)
            if text is None:
                # User systemd wants also contains distro/snap units whose
                # definitions live outside this user-unit directory. Their
                # unreadability cannot make every Helm obligation UNKNOWN;
                # only a Helm-named candidate could plausibly be ours.
                if "helm" in unit.lower() or "helm" in service.lower():
                    unknown.add("systemd")
                continue
            commands = [line.split("=", 1)[1].strip()
                        for line in _active_lines(text)
                        if line.startswith("ExecStart=")]
            if commands:
                rows.append({"kind": "systemd", "name": unit,
                             "text": "\n".join(commands)})
    elif os.path.exists(unit_dir):
        unknown.add("systemd")

    if crontab is None:
        try:
            p = subprocess.run(["crontab", "-l"], capture_output=True,
                               text=True, timeout=5)
            if p.returncode == 0:
                crontab = p.stdout
            elif p.returncode == 1 and "no crontab" in p.stderr.lower():
                crontab = ""
            else:
                crontab = ""
                unknown.add("crontab")
        except FileNotFoundError:
            crontab = ""       # no crontab binary means no crontab actuator
        except (OSError, subprocess.SubprocessError):
            crontab = ""
            unknown.add("crontab")
    for i, line in enumerate(_active_lines(crontab, shell_comments=True)):
        rows.append({"kind": "crontab", "name": "line-%d" % (i + 1),
                     "text": line})

    from .physics import _hook_commands
    for path in settings_paths:
        if not os.path.exists(path):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            unknown.update(("claude-hook", "mcp"))
            continue
        rows += [{"kind": "claude-hook", "name": path, "text": cmd}
                 for cfg in _dicts_named(data, "hooks")
                 for cmd in _hook_commands(cfg)]
        rows += [{"kind": "mcp", "name": path, "text": " ".join(argv),
                  "argv": argv}
                 for argv in _mcp_argvs(data)]

    try:
        from . import chat
        rows += [{"kind": "chat-verb", "name": verb,
                  "text": "helm chat " + verb}
                 for verb in sorted(chat.HELP)]
    except Exception:
        unknown.add("chat-verb")
    return {"rows": rows, "unknown": sorted(unknown)}


def _token_present(text, token):
    return re.search(r"(?<![A-Za-z0-9_.-])" + re.escape(token) +
                     r"(?![A-Za-z0-9_.-])", text) is not None


def _reachable_lines(text):
    """Active shell lines up to the first TOP-LEVEL unconditional `exit` —
    everything below it can never run, so nothing below it can vouch for an
    obligation. Bounded shell reading, honestly limited: if/case nesting is
    tracked, conditional exits (`[ ... ] && exit 0`, an exit inside a
    then-branch) survive, and no fuller shell semantics are attempted — a
    hook estate is our own composed template, not arbitrary shell."""
    depth = 0
    for line in _active_lines(text, shell_comments=True):
        if re.match(r"(?:if|case)\b", line):
            depth += 1
        elif re.fullmatch(r"fi|esac", line):
            depth = max(0, depth - 1)
        elif depth == 0 and re.fullmatch(r"exit(?:\s+\S+)?", line):
            return
        yield line




_COMPOUND_CHARS = set("|&;(){}`")


def _simple_command_argv(text):
    """The ONLY shell shape that earns argv credit: ONE line, ONE simple
    command — optional NAME=value assignments, optional exec/env/command
    wrapper, redirections consumed as syntax, and nothing else. Any control
    operator, compound or function syntax, substitution backtick/paren, or
    cross-line construct refuses OUTRIGHT. Six review rounds proved the
    alternative: judging any richer shell (segments, reachable lines,
    lexical trackers) reduces case by case toward a reachability engine,
    and every partial model over-credited somewhere — heredoc bodies,
    continuations, quoted strings, uncalled function bodies, dead code
    behind exit/exec/false&& (codex r6 closure root). A census must never
    say WIRED falsely; under-credit is the safe direction, and a real
    consumer of this obligation IS a one-line simple command. Returns the
    argv after assignments/wrappers, or None."""
    lines = _active_lines(text, shell_comments=True)
    if len(lines) != 1 or lines[0].endswith("\\"):
        return None
    argv, pending = [], False
    for tok in lines[0].split():
        if pending:
            pending = False
            if set(tok) & (_COMPOUND_CHARS | set("<>")):
                return None
            continue
        if _REDIR_SELF.fullmatch(tok):
            continue
        if _REDIR_NEED.fullmatch(tok):
            pending = True
            continue
        if set(tok) & (_COMPOUND_CHARS | set("<>")):
            return None
        argv.append(tok)
    if pending:
        return None
    # Bounded wrapper forms, matched semantically rather than stripped
    # greedily — shell REJECTS what the old loop accepted (codex r7: `exec
    # FOO=1 helm`, `env exec helm`, `command FOO=1 helm`, `env command
    # helm` are all rc127, not invocations): leading assignments before a
    # DIRECT command; exec/command immediately before the command; env,
    # then assignments, then the command. Wrappers never chain, and a
    # leftover wrapper/assignment at the head simply fails head-is-verb.
    if argv and argv[0] in ("exec", "command"):
        argv = argv[1:]
    elif argv and argv[0] == "env":
        argv = argv[1:]
        while argv and _ASSIGN.fullmatch(argv[0]):
            argv = argv[1:]
    else:
        while argv and _ASSIGN.fullmatch(argv[0]):
            argv = argv[1:]
    return argv or None


def _line_tokens_present(text, tokens):
    """One REACHABLE active shell line carries the whole command relation."""
    return any(all(_token_present(line, tok) for tok in tokens)
               for line in _reachable_lines(text))


def _refusal_line_present(text, pattern):
    """The obligation's command sits ON the refusal path: some reachable
    active line fully IS the exit-propagating invocation. 'exit 0; cmd' and
    'cmd || true' both fail the fullmatch; a line below a top-level
    unconditional exit is never seen."""
    return any(re.fullmatch(pattern, line) for line in _reachable_lines(text))


def _rebind_admission(toks):
    from . import dispatches
    return dispatches.rebind_argv_ok(toks)[0]


# Validator per obligation: the CLI arm's OWN admission decides whether a
# consumer command could actually run — never a second grammar beside the
# parser (a partial one certified four parser-refused shapes; codex meld
# e:1785584307). Known miss, documented non-blocking: a quoted or attached
# CONTROL operator character ("a|b" inside one word) truncates the segment
# early — a false NEGATIVE, so the census under-credits, never over-credits.
_ARGV_OK = {"dispatch-rebind": _rebind_admission}


_ASSIGN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=\S*")
_CMD_WRAPPERS = ("exec", "env", "command")

# Control operators SEPARATE commands; redirections are SYNTAX OF one
# command. The old splitter broke segments at any of |&;<> — so in
# `echo > helm dispatch rebind …` the `>` handed helm a fresh segment and
# COMMAND POSITION, when the shell makes helm a FILENAME echo writes to
# (codex r5, gate 80f95d7e). Self-contained redirections (fd-dups like
# 2>&1, attached targets like 2>/dev/null) consume only their own token;
# bare operators (>, >>, <, 2>, 2>>, &>, >&) consume the NEXT word as
# their target; an operator with no target word is a shell syntax error —
# it invalidates the whole segment.
_CTRL_OPS = {"||", "&&", ";", "|", "&"}
_CTRL_CHARS = set("|&;")
_REDIR_SELF = re.compile(r"(?:\d*|&)(?:>>|>|<<<|<<|<)&?[^<>|&;]+|\d*[<>]&-")
_REDIR_NEED = re.compile(r"\d*(?:>>|>|<<<|<<|<|>&|<&)|&>>?")


def _cron_command(line):
    """The command part of one crontab line: 5 schedule fields (or one
    @reboot-style token) precede it — they are cron grammar, not argv.
    Cron then ends the COMMAND at the first UNESCAPED percent and feeds
    the suffix to stdin, so `X=% helm …` runs only the assignment while
    helm arrives as text on nothing's stdin (codex terminal pass); the
    command part stops there, and an escaped percent stays literal."""
    toks = line.split(None, 1)
    if toks and toks[0].startswith("@"):
        cmd = toks[1] if len(toks) > 1 else ""
    else:
        parts = line.split(None, 5)
        cmd = parts[5] if len(parts) > 5 else ""
    cut = re.search(r"(?<!\\)%", cmd)
    return cmd[:cut.start()] if cut else cmd


def _head_is_verb(toks, words, validator):
    """helm (bare or path-suffixed) in COMMAND POSITION, then the verb words
    literally, then the remainder judged by the verb's own admission."""
    head, rest = toks[:len(words)], toks[len(words):]
    if len(head) != len(words):
        return False
    if not (head[0] == words[0] or head[0].endswith("/" + words[0])):
        return False
    if head[1:] != words[1:]:
        return False
    return bool(validator(rest))


def _systemd_argv(line):
    """One ExecStart value as DIRECT-EXEC argv: systemd execs the first
    token itself (less its @-+!: prefix chars) — there is no shell, so a
    literal `&&`, a redirection, or a NAME=value word is an ORDINARY
    ARGUMENT handed to that executable, never an operator or a wrapper."""
    toks = line.split()
    if not toks:
        return toks
    # systemd's @ prefix makes the SECOND token argv[0]: the first names
    # the file to exec, the second names what it sees as its own name —
    # so `@/usr/local/bin/helm dispatch …` hands the EXECUTABLE argv0
    # "dispatch" and helm never sees the verb where the census read it
    # (codex terminal pass). Prefix chars COMBINE IN ANY ORDER (`-@` and
    # `@-` are both live systemd), so the @ is detected in the whole
    # prefix run, never by startswith (codex final blocker). Discard the
    # explicit argv0 token; an @ form with nothing following credits
    # nothing.
    head, i = toks[0], 0
    while i < len(head) and head[i] in "@-+!:":
        i += 1
    prefixes, toks[0] = head[:i], head[i:]
    # `+` and `!`/`!!` select mutually-exclusive privilege modes. systemd
    # rejects the whole ExecStart before it can produce argv, so the census
    # must refuse the whole prefix run rather than crediting stripped text.
    if "+" in prefixes and "!" in prefixes:
        return []
    if "@" in prefixes:
        if len(toks) < 2 or not toks[0]:
            return []
        return [toks[0]] + toks[2:]
    return toks


def _argv_usable(row, prefix, validator):
    """The consumer must INVOKE the verb, not merely mention it — under ITS
    OWN KIND'S grammar, because the kinds do not share an interpreter
    (codex r5, gate 80f95d7e: shell redirections, structured MCP argv and
    native systemd ExecStart all censused WIRED with zero helm calls).

    mcp is STRUCTURED: [command, *args] matched directly — no shell at all,
    so no wrappers, no assignments, no redirections; an entry whose command
    is `env` is NOT unwrapped (env-as-executable parsing is deliberately
    unimplemented — conservative under-credit). systemd ExecStart is
    direct-exec argv (see _systemd_argv). Only claude-hook command strings
    and the crontab COMMAND PART are shell text: segments break at control
    operators, redirections are consumed as syntax, exec/env/command
    wrappers and NAME=value assignments may precede the helm token, and
    the remaining argv must pass the verb's own parser admission —
    substring search once credited `echo helm dispatch rebind …`, printf
    bodies, and CMD="…" assignments (codex r4, hermetic fake-helm proof).
    Any other kind claims no grammar and never credits. Static throughout,
    so a shell substitution ("$ROW") counts as an argv value — the census
    asserts the SHAPE can run, not that any one expansion succeeds."""
    words, kind = prefix.split(), row.get("kind")
    if kind == "mcp":
        return _head_is_verb(row.get("argv") or [], words, validator)
    if kind == "systemd":
        return any(_head_is_verb(_systemd_argv(line), words, validator)
                   for line in str(row.get("text") or "").splitlines())
    if kind not in ("claude-hook", "crontab"):
        return False
    text = str(row.get("text") or "")
    if kind == "crontab":
        lines = _active_lines(text, shell_comments=True)
        if len(lines) != 1:
            return False
        text = _cron_command(lines[0])
    argv = _simple_command_argv(text)
    return bool(argv) and _head_is_verb(argv, words, validator)


def actuator_census(repo=None, hook_dir=None, unit_dir=None, crontab=None,
                    settings_paths=None, home_dir=None, obligations=None):
    """Declared action -> installed proof, MISSING, or UNKNOWN."""
    obligations = ACTUATORS if obligations is None else obligations
    consumers = consumer_census(
        repo=repo, hook_dir=hook_dir, unit_dir=unit_dir, crontab=crontab,
        settings_paths=settings_paths, home_dir=home_dir)
    wired, missing, unsure = {}, [], []
    unknown = set(consumers["unknown"])
    for name, spec in sorted(obligations.items()):
        hits = [r for r in consumers["rows"]
                if r["kind"] in spec["kinds"]
                and (not spec.get("names") or r["name"] in spec["names"])
                and all(_token_present(r["text"], tok)
                        for tok in spec["tokens"])
                and (not spec.get("line_tokens")
                     or _line_tokens_present(r["text"],
                                             spec["line_tokens"]))
                and (not spec.get("refusal_line")
                     or _refusal_line_present(r["text"],
                                              spec["refusal_line"]))
                and (not spec.get("argv_ok")
                     or _argv_usable(r, spec["tokens"][0],
                                     _ARGV_OK[spec["argv_ok"]]))]
        if hits:
            wired[name] = ["%s:%s" % (r["kind"], r["name"])
                           for r in hits]
        elif unknown & set(spec["kinds"]):
            unsure.append(name)
        else:
            missing.append(name)
    invalid = sorted(m for m in ALLOWED if m not in INTRINSIC_ALLOWED
                     and ALLOWED_ACTUATORS.get(m) not in obligations)
    return {"consumers": len(consumers["rows"]), "wired": wired,
            "missing": missing, "unknown": unsure,
            "invalid_allowed": invalid}


def actuator_issue_summary(data):
    parts = []
    if data.get("missing"):
        parts.append("NO ACTUATOR: " + ", ".join(data["missing"]))
    if data.get("unknown"):
        parts.append("actuator UNKNOWN: " + ", ".join(data["unknown"]))
    if data.get("invalid_allowed"):
        parts.append("unchecked ALLOWED: " +
                     ", ".join(data["invalid_allowed"]))
    return "; ".join(parts)


def actuator_report_lines(data):
    issue = actuator_issue_summary(data)
    if issue:
        return ["  " + issue]
    return ["  every declared detector has an installed actuator (%d consumers)"
            % data.get("consumers", 0)]


def census(root=None, with_events=True):
    """The four rungs -> a dict. Nothing here writes or refuses; a census
    reports, and the caller decides what a finding is worth."""
    g = graph(root)
    live = reachable(g)
    all_mods = set(g)
    unreachable = sorted(m for m in all_mods - live if m not in ALLOWED)
    have_tests = tested(root)
    untested = sorted(m for m in live - have_tests if m not in ALLOWED)
    ran = exercised() if with_events else None
    # A SUBMODULE IS CREDITED BY ITS PACKAGE HERE, and only here. The trail
    # records VERBS, never module paths, so it has no submodule granularity to
    # resolve — marking every dotted node unexercised would not be a finding,
    # it would be this rung reporting its own blind spot as evidence. The other
    # three rungs keep full dotted identity.
    unexercised = (None if ran is None
                   else sorted(m for m in live - ran
                               if m not in ALLOWED and m in have_tests
                               and m.split(".")[0] not in ran))
    out = {"modules": len(all_mods), "reachable": len(live),
           "unreachable": unreachable, "untested": untested,
           "unexercised": unexercised, "allowed": dict(ALLOWED)}
    if root is None:
        out["actuators"] = actuator_census()
    return out


def report_lines(root=None, verbose=False, actuators=None, data=None):
    """Owner-facing lines. Missing actions fold into one readable class."""
    c = data or census(root)
    out = ["wiring: %d modules, %d reachable from %s"
           % (c["modules"], c["reachable"], "/".join(ENTRIES))]
    if c["unreachable"]:
        out.append("  UNREACHABLE (built, not wired — nothing can ever call these):")
        out.extend("    %s" % m for m in c["unreachable"])
    else:
        out.append("  every module is reachable from an entry point")
    if c["untested"]:
        out.append("  untested (wired, unverified — no test names them): %d"
                   % len(c["untested"]))
        if verbose:
            out.extend("    %s" % m for m in c["untested"])
    if c["unexercised"] is None:
        out.append("  exercised: UNKNOWN (no readable event trail)")
    elif verbose and c["unexercised"]:
        out.append("  no receipt in the retained event window: %d (weak signal "
                   "— the trail rotates)" % len(c["unexercised"]))
    actuators = actuators or c.get("actuators") or actuator_census()
    return out + actuator_report_lines(actuators)


# ---------------------------------------------------------------------------
# the session-scoped gate — "you may not go idle having shipped this"
# ---------------------------------------------------------------------------
# A CENSUS THAT ONLY REPORTS IS ANOTHER THING SOMEONE HAS TO REMEMBER TO READ,
# and the failure being fixed is precisely that nobody remembers. So the census
# gets teeth through the primitive helm already has: the Stop hook, which
# already refuses an idle stop while addressed chat rows are undelivered. This
# is one more rung on it, not a second mechanism.
#
# SCOPED TO WHAT THIS WORKING TREE ADDED, deliberately. A guard that fires on
# the whole repo's backlog would block every seat on debt none of them created,
# and a guard that blocks everyone always is turned off within the day. Asking
# only "did YOU add a module nothing can reach" keeps it precise, keeps it
# rare, and makes it self-clearing: wire the module, or defend it in ALLOWED,
# and the gate opens.

def _git(repo, *args):
    import subprocess
    try:
        p = subprocess.run(["git", "-C", repo, *args], capture_output=True,
                           text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return p.stdout if p.returncode == 0 else None


def added_modules(repo, base="origin/main"):
    """Module names this tree ADDED relative to `base` — committed or not.

    None (not an empty list) when git cannot answer, so a caller can tell
    "nothing was added" from "I could not look" — the same law the rest of
    this file is about.
    """
    seen, any_ok = set(), False
    for args in (("diff", "--name-only", "--diff-filter=A", base + "...HEAD"),
                 ("ls-files", "--others", "--exclude-standard")):
        out = _git(repo, *args)
        if out is None:
            continue
        any_ok = True
        for line in out.splitlines():
            line = line.strip()
            if not (line.startswith("helm/") and line.endswith(".py")):
                continue
            # THE SAME RESOLVER THE CENSUS USES. Keying a file any other way
            # here means the guard and the census talk about different objects,
            # and the failure is always silent exemption — see node_name.
            name = node_name(line[len("helm/"):])
            if name:
                seen.add(name)
    return sorted(seen) if any_ok else None


def _unwired_memo_path():
    from . import home
    return os.path.join(home.helm_home(), home.GLOBAL, ".state",
                        "wiring-unwired-memo.json")


def _source_graph_fp(root):
    """A content fingerprint of the package's source graph — the reachability
    inputs the census actually reads. ~12ms over the full package.

    THE KEY MUST BIND THE GRAPH, NOT THE FILENAMES. The first memo keyed on
    the added-set alone, and codex-3's no-mock repro killed it (2026-07-31):
    commit a new module wired through an existing file's import, census reads
    clean; remove ONLY that existing-file import — the added-set is
    unchanged, so the memo returns the stale clean answer while the module is
    now genuinely unreachable. That is the one lie this rung can tell, and a
    filename key cannot see it. A content hash over every helm/*.py sees
    every source change the census can see, at one hundredth of its cost."""
    import hashlib
    root = root or os.path.dirname(os.path.abspath(__file__))
    h = hashlib.blake2b(digest_size=12)
    try:
        paths = sorted(os.path.join(dp, f)
                       for dp, _, fs in os.walk(root) for f in fs
                       if f.endswith(".py"))
        for p in paths:
            h.update(p.encode())
            with open(p, "rb") as fh:
                h.update(fh.read())
    except OSError:
        return None                    # an unreadable graph is no key at all
    return h.hexdigest()


def _unwired_memo_schema(key, dead, added, repo, base):
    body = {"v": 1, "key": key, "dead": list(dead), "added": list(added),
            "repo": repo, "base": base}
    import hashlib as _hl
    body["digest"] = _hl.blake2b(
        _json_canonical(body).encode(), digest_size=12).hexdigest()
    return body


def _json_canonical(obj):
    import json as _json
    return _json.dumps({k: obj[k] for k in sorted(obj)}, separators=(",", ":"))


def _unwired_memo_valid(memo, key):
    """A memo is authority only when its SHAPE, its KEY, and its PAYLOAD
    agree. codex-3's second probe (2026-07-31): a valid-shaped memo with
    dead swapped to [] passed the schema check and returned the prohibited
    false clean — shape is not integrity. The digest binds the canonical
    payload, so any tampering with dead/added/repo/base invalidates."""
    if not isinstance(memo, dict) or memo.get("v") != 1:
        return False
    if memo.get("key") != key or not isinstance(memo.get("dead"), list):
        return False
    if not all(isinstance(m, str) for m in memo["dead"]):
        return False
    import hashlib as _hl
    expect = {k: memo[k] for k in ("v", "key", "dead", "added", "repo", "base")
              if k in memo}
    return memo.get("digest") == _hl.blake2b(
        _json_canonical(expect).encode(), digest_size=12).hexdigest()


def unwired_additions(root=None, repo=None, base="origin/main"):
    """(modules, note) — modules this tree added that NOTHING can reach.

    THE STOP-PATH MEMO. The Stop hook calls this on every stop, and the
    census costs ~1s of ast-walking — measured 2026-07-31 at 2.6s per stop
    with a module-adding lane in flight, against a 5s hook ceiling the owner
    hit three times in one morning ("still timing out, 3rd time ive
    mentioned this morning"). The memo key binds BOTH cheap inputs the
    answer depends on: the added-set (~5ms) AND the source-graph content
    fingerprint (~12ms). The expensive dead-list is recomputed only when
    EITHER changes — a new lane's first stop pays once, an existing-file
    edit that unwires a module pays once, and every stop in between reads
    the memo. A corrupt or schema-foreign memo recomputes — never trusted,
    never fatal, never read as clean, because a stale clean answer is the
    one lie this rung can tell. Concurrent first-misses single-flight on a
    lockfile: one stop pays the census, the rest read what it wrote.
    """
    repo = repo or os.path.dirname(os.path.dirname(os.path.abspath(
        root or __file__)))
    added = added_modules(repo, base)
    if added is None:
        return [], "git could not be asked; the gate stays open"
    if not added:
        return [], None
    import hashlib
    graph_fp = _source_graph_fp(root)
    if graph_fp is None:
        # an unreadable graph yields no key at all: compute live, memo nothing
        return sorted(set(added) & set(
            census(root, with_events=False)["unreachable"])), None
    key = hashlib.blake2b(
        ("%s\0%s\0%s\0%s" % (os.path.realpath(repo), base,
                             ",".join(added), graph_fp)).encode(),
        digest_size=12).hexdigest()
    from . import pk
    memo = pk.read_json(_unwired_memo_path(), None)
    if _unwired_memo_valid(memo, key):
        return sorted(memo["dead"]), None
    import fcntl
    os.makedirs(os.path.dirname(_unwired_memo_path()), exist_ok=True)
    with open(_unwired_memo_path() + ".lock", "a") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX)
        # re-check inside the lock: a concurrent first-miss may have written
        memo = pk.read_json(_unwired_memo_path(), None)
        if _unwired_memo_valid(memo, key):
            return sorted(memo["dead"]), None
        dead = sorted(set(added) & set(
            census(root, with_events=False)["unreachable"]))
        # TOCTOU, the strict rule (codex-3's A->B repro): the source moved
        # DURING the census, so the answer describes no single state — it is
        # A's beginning read against B's end. Publishing it under EITHER key
        # poisons that key: under A it answers for a state already gone, under
        # B it answers for a state never measured. A measurement that straddles
        # a state change is written NOWHERE. The answer is still returned
        # (this stop deserves it), just never trusted again.
        post_fp = _source_graph_fp(root)
        if post_fp is not None and post_fp != graph_fp:
            return dead, "source moved during the census; answer uncached"
        if post_fp is not None:
            try:
                pk.write_json(_unwired_memo_path(),
                              _unwired_memo_schema(key, dead, added,
                                                   os.path.realpath(repo),
                                                   base))
            except Exception:               # noqa: BLE001 — never blocks
                pass
    return dead, None


def gate_lines(root=None, repo=None, base="origin/main"):
    """The Stop-hook rung -> lines to show, empty when there is nothing to say.

    FAIL-OPEN is not negotiable here: a Stop hook that raises wedges every
    session on the host. Anything unexpected leaves the gate open and silent.
    """
    try:
        dead, note = unwired_additions(root, repo, base)
    except Exception:                       # noqa: BLE001 — the stop-guard law
        return []
    if not dead:
        return []
    return ["you added %d module(s) that NOTHING can reach — built, not wired:"
            % len(dead)] + \
           ["    helm/%s.py" % m for m in dead] + \
           ["  wire it to a verb or a caller, or declare it in "
            "helm/wiring.py ALLOWED with the reason it is unreachable.",
            "  `helm wiring` shows the full ladder."]


_USAGE = """usage: helm wiring [--verbose] [--json] [--gate]
  The built/wired/exercised ladder over package imports AND installed action
  surfaces. UNREACHABLE means no entry point can call a module; NO ACTUATOR
  means a declared detector has no enabled schedule or executable hook. --gate
  asks only about modules THIS tree added (what the Stop hook checks). Exit 1
  on a grounded unreachable or missing-actuator finding.
"""


def cmd_wiring(args):
    """wiring — which modules are built, wired, exercised."""
    args = list(args or [])
    # guard_tail, not a bare membership test: helm's law is that an unknown
    # flag is NAMED rather than silently ignored. A verb that quietly drops
    # `--gaet` reports a clean ladder for a question nobody actually asked,
    # which is the same shape of lie this whole module exists to prevent.
    from .cli import guard_tail
    rc = guard_tail("helm wiring", [a for a in args if a.startswith("--")],
                    flags=("--verbose", "--json", "--gate"), usage=_USAGE)
    if rc is not None:
        return rc
    if "--gate" in args:
        lines = gate_lines()
        for ln in lines:
            print(ln)
        if not lines:
            print("wiring: nothing this tree added is unreachable")
        return 1 if lines else 0
    c = census()
    actuators = c["actuators"]
    bad = bool(c["unreachable"] or actuators["missing"]
               or actuators["invalid_allowed"])
    if "--json" in args:
        print(json.dumps(c, indent=2, sort_keys=True))
        return 1 if bad else 0
    for ln in report_lines(verbose="--verbose" in args,
                           actuators=actuators, data=c):
        print(ln)
    return 1 if bad else 0
