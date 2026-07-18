#!/usr/bin/env python3
"""helm doctor — the health check. READ-ONLY always: it reports, it never
repairs (repair verbs live with their legs: sync, drain, interview).

Every check is a small function returning [(level, message), ...]; the report
loop is data-driven over CHECKS (names resolved late, so any check is a test
seam), no if-forest. Exit 0 unless a FAIL.
"""
import os

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
    duplicate count (the incomplete-migration tombstones)."""
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
                    "tombstones) — `helm drain --sweep-dups` will archive these, owner-gated"
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


def check_env():
    """HELM_*/MELD_* overrides in effect."""
    out = [(OK, "env override: %s=%s" % (var, os.environ[var]))
           for var in ("HELM_HOME", "MELD_HOME") if os.environ.get(var)]
    return out or [(OK, "no env overrides — home resolves to ~/.helm")]


CHECKS = ("check_home", "check_projects", "check_adoption", "check_adopted_store",
          "check_know_your_user", "check_cv", "check_env")


def cmd_doctor(args):
    """doctor — read-only health report over the whole helm estate."""
    results = [r for name in CHECKS for r in globals()[name]()]
    for level, msg in results:
        print("  %-4s %s" % (level, msg))
    tally = {lvl: sum(1 for l, _ in results if l == lvl) for lvl in (OK, WARN, FAIL)}
    print("helm doctor: %d ok, %d warn, %d fail" % (tally[OK], tally[WARN], tally[FAIL]))
    return 1 if tally[FAIL] else 0
