#!/usr/bin/env python3
"""helm doctor — the health check. READ-ONLY always: it reports, it never
repairs (repair verbs live with their legs: sync, drain, interview).

Every check is a small function returning [(level, message), ...]; the report
loop is data-driven over CHECKS (names resolved late, so any check is a test
seam), no if-forest. Exit 0 unless a FAIL.
"""
import os
import re

from . import home, pk, registry, whoami

OK, WARN, FAIL = "OK", "WARN", "FAIL"

# Adopted-store filename prefixes (the live pkstore classes).
PREFIXES = ("prior-", "prem-", "lex-", "heuristic-")


def check_home():
    """helm home exists, registry.json parses, project count."""
    root = home.helm_home()
    if not os.path.isdir(root):
        return [(WARN, "helm home missing (%s) — run `helm sync` to scaffold it" % root)]
    out = [(OK, "helm home: %s" % root)]
    path = home.registry_path()
    if not os.path.exists(path):
        out.append((WARN, "registry.json missing — run `helm sync`"))
        return out
    reg = pk.read_json(path)
    if not isinstance(reg, dict):
        out.append((FAIL, "registry.json does not parse (%s)" % path))
        return out
    n = len(reg.get("projects") or {})
    out.append((OK, "registry parses: %d project%s" % (n, "s"[:n != 1])))
    return out


def check_authored():
    """The authored registry layer: file parses, entries merge (path-matched
    projection record, or an external anchor). Raw reads only — never triggers
    the load()-time migration."""
    path = home.authored_path()
    if not os.path.exists(path):
        return []
    auth = pk.read_json(path)
    entries = auth.get("projects") if isinstance(auth, dict) else None
    if not isinstance(entries, dict):
        return [(FAIL, "registry-authored.json does not parse (%s)" % path)]
    bad = sorted(n for n, e in entries.items() if not isinstance(e, dict))
    if bad:
        # a null/garbled ENTRY inside a parseable file must report, not crash
        return [(FAIL, "registry-authored.json has non-object entr%s: %s"
                 % ("y" if len(bad) == 1 else "ies", ", ".join(bad)))]
    reg = pk.read_json(home.registry_path())
    projects = (reg.get("projects") if isinstance(reg, dict) else None) or {}
    live = sum(1 for n, e in entries.items()
               if e.get("external") or (n in projects and
                  e.get("path", projects[n].get("path")) == projects[n].get("path")))
    n = len(entries)
    return [(OK, "authored layer: %d entr%s, %d live in merge"
             % (n, "y" if n == 1 else "ies", live))]


def check_projects():
    """Per registry project: home dir sane (broken symlink = FAIL), repo path
    still on disk (gone = WARN), memory_dir pointer still valid (gone = WARN)."""
    projects = registry.load().get("projects") or {}
    out = []
    healthy = 0
    checked = 0
    for name in sorted(projects):
        rec = projects[name]
        if rec.get("retired"):
            continue
        checked += 1
        issues = len(out)
        p = home.project_dir(name)
        if not rec.get("external"):
            if os.path.islink(p) and not os.path.exists(p):
                out.append((FAIL, "%s: home is a broken symlink (%s -> %s)"
                            % (name, p, os.readlink(p))))
            elif not os.path.isdir(p):
                out.append((WARN, "%s: home dir missing (%s) — run `helm sync`" % (name, p)))
        path = rec.get("path") or ""
        if path and not os.path.exists(path):
            out.append((WARN, "%s: path gone (%s) — repo moved or deleted; "
                        "lineage/archive candidate" % (name, path)))
        mem = rec.get("memory_dir")
        if mem and not os.path.isdir(mem):
            out.append((WARN, "%s: memory_dir pointer stale (%s)" % (name, mem)))
        healthy += issues == len(out)
    if checked:
        out.append((OK, "projects: %d of %d healthy" % (healthy, checked)))
    return out


def check_adoption():
    """A registry project in the adoption map whose helm dir is a REAL dir
    (not the adoption symlink) = WARN — sync kept user data, doctor surfaces it."""
    projects = registry.load().get("projects") or {}
    out = []
    for name in sorted(set(projects) & set(registry.ADOPTED_HOMES)):
        p = home.project_dir(name)
        if os.path.isdir(p) and not os.path.islink(p):
            out.append((WARN, "%s: adoption conflict — helm dir is a real dir, expected "
                        "symlink -> %s" % (name, registry.ADOPTED_HOMES[name])))
    return out


def check_adopted_store(adopted_dir=None):
    """Adopted-store shape: entry counts by prefix + the prem-/prior- same-slug
    duplicate count (the incomplete-migration leftovers)."""
    d = adopted_dir or home.adopted_memory_dir()
    if not os.path.isdir(d):
        return [(WARN, "adopted store missing (%s) — no live memory dir to adopt" % d)]
    files = [f for f in os.listdir(d)
             if f.endswith(".md") and os.path.isfile(os.path.join(d, f))]
    counts = {p: sum(1 for f in files if f.startswith(p)) for p in PREFIXES}
    other = len(files) - sum(counts.values())
    dups = {f[5:] for f in files if f.startswith("prem-")} \
        & {f[6:] for f in files if f.startswith("prior-")}
    out = [(OK, "adopted store: %s (%s, other=%d)" % (
        d, ", ".join("%s=%d" % (p.rstrip("-"), counts[p]) for p in PREFIXES), other))]
    if dups:
        n = len(dups)
        out.append((WARN, "%d prem-/prior- same-slug duplicate%s (incomplete-migration "
                    "leftovers) — `helm drain --sweep-dups` will archive these, owner-gated"
                    % (n, "s"[:n != 1])))
    return out


def check_know_your_user():
    """The warmth leg: WARN while empty and un-interviewed."""
    p = whoami.load_profile()
    notes = whoami.load_notes()
    if not (p["technical_level"] or p["guidance"] or notes) and p["interview_status"] != "done":
        return [(WARN, "know-your-user: the warmth leg is empty — run `helm interview`")]
    return [(OK, "know-your-user: level=%s, %d guidance, %d active note%s (interview %s)" % (
        p["technical_level"] or "-", len(p["guidance"]), len(notes),
        "s"[:len(notes) != 1], p["interview_status"] or "not offered"))]


def check_cv(cv_dir=None):
    """cv presence — the recall plane."""
    d = cv_dir or os.path.join(os.path.expanduser("~"), ".clustervision")
    if os.path.exists(d):
        return [(OK, "cv recall plane live (%s)" % d)]
    return [(WARN, "cv missing (%s) — recall plane offline" % d)]


def check_inject_coverage():
    """The crown-jewel wiring: how many claude homes carry the per-turn inject
    hook (present + resolvable helm + fail-open contract)."""
    from . import hooks
    try:
        rows = hooks.status_rows()
    except Exception as e:
        return [(WARN, "inject coverage unknown (%s: %s)" % (e.__class__.__name__, e))]
    if not rows:
        return []
    n = sum(1 for r in rows if r["hook"] and r["resolvable"] and r["fail_open"])
    msg = "inject coverage: %d of %d claude homes" % (n, len(rows))
    if n < len(rows):
        return [(WARN, msg + " — `helm hooks install` closes the gap")]
    return [(OK, msg)]


def check_env():
    """HELM_*/MELD_* overrides in effect."""
    out = [(OK, "env override: %s=%s" % (var, os.environ[var]))
           for var in ("HELM_HOME", "MELD_HOME") if os.environ.get(var)]
    return out or [(OK, "no env overrides — home resolves to ~/.helm")]


# The harness versions the physics facts were probed against (physics.py
# header). A harness upgrade makes those facts quietly wrong, never loudly
# broken — this sentinel is the loud part.
PHYSICS_PROBED = {"claude": "2.1.207", "codex": "0.144.1"}


def check_physics_currency():
    """Installed harness version vs the version physics.py was probed against."""
    import shutil as _sh
    import subprocess as _sp
    out = []
    for tool, probed in sorted(PHYSICS_PROBED.items()):
        if not _sh.which(tool):
            continue
        try:
            v = _sp.run([tool, "--version"], capture_output=True, text=True,
                        timeout=15).stdout.strip()
        except (_sp.TimeoutExpired, OSError) as e:
            # the sentinel must not fail QUIET exactly when currency is unknowable
            out.append((WARN, "%s --version failed (%s) — physics facts probed "
                              "at %s, currency UNKNOWN" % (tool, type(e).__name__, probed)))
            continue
        m = re.search(r"\d+\.\d+\.\d+", v)
        if not m:
            out.append((WARN, "%s --version unparseable (%r) — physics facts "
                              "probed at %s, currency unknown" % (tool, v[:40], probed)))
        elif m.group(0) != probed:
            out.append((WARN, "%s is %s but physics.py facts were probed at %s — "
                              "re-verify config-resolution behavior (settings "
                              "precedence, MCP sources) and update PHYSICS_PROBED"
                              % (tool, m.group(0), probed)))
        else:
            out.append((OK, "%s %s matches the physics probe baseline" % (tool, probed)))
    return out


CHECKS = ("check_home", "check_authored", "check_projects", "check_adoption",
          "check_adopted_store", "check_know_your_user", "check_cv",
          "check_inject_coverage", "check_env", "check_physics_currency")


def cmd_doctor(args):
    """doctor — read-only health report over the whole helm estate."""
    results = [r for name in CHECKS for r in globals()[name]()]
    for level, msg in results:
        print("  %-4s %s" % (level, msg))
    tally = {lvl: sum(1 for l, _ in results if l == lvl) for lvl in (OK, WARN, FAIL)}
    print("helm doctor: %d ok, %d warn, %d fail" % (tally[OK], tally[WARN], tally[FAIL]))
    return 1 if tally[FAIL] else 0
