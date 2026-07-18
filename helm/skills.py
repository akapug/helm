#!/usr/bin/env python3
"""Skills census — every skill home, deduped, hygiene-flagged. READ-ONLY.

Skills are repo/engine artifacts, not helm-store knowledge — helm references
and audits them (the census + dupes report feed the web UI's enable/disable
surface later; mutation stays a deliberate verb, not a side effect).

Homes scanned: the shared ~/.claude/skills, every cred-home's skills dir
(deduped on realpath — several are symlinks to one target), and any extra
dirs passed in. Flags: same NAME in multiple distinct homes (shadowing),
same CONTENT under different names (true duplicates), self-referential or
broken symlinks (the audit that's easy to skip until it bites).
"""
import glob
import hashlib
import os


def _skill_homes():
    home = os.path.expanduser("~")
    candidates = [os.path.join(home, ".claude", "skills")] + \
        sorted(glob.glob(os.path.join(home, ".claude-homes", "*", "skills")))
    seen = {}
    bad = []
    for c in candidates:
        if not os.path.exists(c):
            if os.path.islink(c):
                bad.append((c, "broken symlink -> " + os.readlink(c)))
            continue
        if os.path.islink(c) and os.readlink(c).rstrip("/") == c.rstrip("/"):
            bad.append((c, "self-referential symlink"))
            continue
        real = os.path.realpath(c)
        seen.setdefault(real, c)
    return list(seen.values()), bad


def _sha(path):
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()[:12]
    except Exception:
        return None


def census(extra_homes=()):
    homes, bad = _skill_homes()
    homes = list(homes) + [h for h in extra_homes if os.path.isdir(h)]
    skills = []
    for h in homes:
        for name in sorted(os.listdir(h)):
            d = os.path.join(h, name)
            md = os.path.join(d, "SKILL.md")
            if not os.path.isdir(d):
                continue
            if os.path.islink(d) and not os.path.exists(d):
                bad.append((d, "broken skill symlink -> " + os.readlink(d)))
                continue
            skills.append({"name": name, "home": h, "real": os.path.realpath(d),
                           "hash": _sha(md), "has_manifest": os.path.isfile(md)})
    return skills, bad


def dupes(skills):
    by_name, by_hash = {}, {}
    for s in skills:
        by_name.setdefault(s["name"], []).append(s)
        if s["hash"]:
            by_hash.setdefault(s["hash"], []).append(s)
    name_dupes = {n: v for n, v in by_name.items()
                  if len({s["real"] for s in v}) > 1}
    content_dupes = {h: v for h, v in by_hash.items()
                     if len({s["name"] for s in v}) > 1}
    return name_dupes, content_dupes


def cmd_skills(args):
    """skills [dupes] — census of every skill home; hygiene flags. Read-only."""
    skills, bad = census()
    if args and args[0] == "dupes":
        name_dupes, content_dupes = dupes(skills)
        if not (name_dupes or content_dupes or bad):
            print("helm skills: no duplicates, no hygiene flags (%d skills)." % len(skills))
            return 0
        identical = sum(1 for v in name_dupes.values()
                        if len({s["hash"] for s in v}) == 1)
        print("helm skills dupes: %d names multi-homed (%d identical everywhere "
              "= safe to consolidate; %d DIVERGED):"
              % (len(name_dupes), identical, len(name_dupes) - identical))
        for n, v in sorted(name_dupes.items()):
            same = len({s["hash"] for s in v}) == 1
            print("  %-11s '%s' in %d homes:"
                  % ("identical" if same else "DIVERGED", n, len(v)))
            for s in v:
                print("      " + s["real"] + ("" if same else "  [" + str(s["hash"]) + "]"))
        for h, v in sorted(content_dupes.items()):
            print("SAME CONTENT %s under different names: %s"
                  % (h, ", ".join(sorted({s["name"] for s in v}))))
        for path, why in bad:
            print("HYGIENE %s: %s" % (path, why))
        return 0
    uniq = {}
    for s in skills:
        uniq.setdefault(s["real"], s)
    print("helm skills (%d unique across %d entries):" % (len(uniq), len(skills)))
    for real in sorted(uniq, key=lambda r: uniq[r]["name"]):
        s = uniq[real]
        flag = "" if s["has_manifest"] else "  [no SKILL.md]"
        print("  %-28s %s%s" % (s["name"], real, flag))
    if bad:
        print("hygiene flags (%d) — see `helm skills dupes`" % len(bad))
    return 0
