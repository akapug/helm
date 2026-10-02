#!/usr/bin/env python3
"""helm.console_walk — the owner's console, walked whole by a fresh reader
(task/3444).

WHY THIS EXISTS, in the owner's words (2026-09-27): "i am not even a ux
designer, im surprised im the only one who[s] noticing these issues". Every
review reads a diff against "worse than main", so no one read the console as
a whole; one fresh-agent walk found ten and more problems (duplicated views,
one count reading two ways on two pages, dead ends). This module makes that
walk a thing the fleet owes by itself.

THREE ANSWERS, and only the third one writes:
  owed(root)    DERIVED at read time from two facts that already exist: the
                trunk commits that touch the console (CONSOLE_PATHS) and the
                walk reports under `<helm home>/<project>/reviews/`. Owed when a
                console land is newer than the newest report, or that report
                is WALK_EVERY_S old, or there is none. No second writer keeps
                an "owed" flag: a report written IS the discharge.
  brief(root)   the walk's method, filled with the owed reason, the next
                report's path and N and the widths, for a seat to hand a fresh
                reader verbatim (`helm web walk`).
  surface(root, post)
                the one line auto-land posts to the integrator after a land,
                once per owed state. Its marker records only what was SAID,
                never whether a walk is owed.
"""
import json
import os
import re
import sys
import time

from . import home, vcs
from .work import _lanes

#: A walk is owed when the newest report is this old, console land or not.
WALK_EVERY_S = 7 * 86400
#: The widths the walk reads every page at: the owner's desk and his phone.
WIDTHS = (1440, 420)
#: What serves the console: the page's own parts and the server modules
#: (`helm web`'s `web*.py` family). `helm/webshot.py` captures the console and
#: serves none of it, so it is not here.
CONSOLE_PATHS = ("helm/web_ui", "helm/web.py", ":(glob)helm/web_*.py",
                 "helm/webserve.py")
REPORT = re.compile(r"^(\d{4}-\d{2}-\d{2})-console-walk-(\d+)\.md$")
SAID = "console-walk-said.json"


def reviews_dir(root):
    """Where walk reports live: `<helm home>/<project>/reviews/`."""
    return os.path.join(home.project_dir(_lanes.project_token(root)),
                        "reviews")


def _said_path(root):
    return os.path.join(home.project_dir(_lanes.project_token(root)),
                        ".state", SAID)


def _date(now):
    return time.strftime("%Y-%m-%d", time.localtime(now))


def _age(seconds):
    s = max(0, int(seconds))
    if s >= 86400:
        return "%dd" % (s // 86400)
    if s >= 3600:
        return "%dh" % (s // 3600)
    return "%dm" % (s // 60)


def _reports(root):
    """([{path, n, ts}] for every walk report, oldest first, why). A missing
    directory is no walk yet; an unreadable one is a why, so the answer
    reads UNKNOWN, never owed or discharged. An empty stub is no walk, but
    its N is taken, so the next report does not reuse it."""
    d = reviews_dir(root)
    try:
        names = os.listdir(d)
    except FileNotFoundError:
        return [], [], None
    except OSError as exc:
        return [], [], "reviews dir %s unreadable: %s" % (d, exc)
    found, taken = [], []
    for name in names:
        m = REPORT.match(name)
        if not m:
            continue
        path = os.path.join(d, name)
        taken.append(int(m.group(2)))
        try:
            st = os.stat(path)
        except OSError:
            continue
        if st.st_size > 0:
            found.append({"path": path, "n": int(m.group(2)),
                          "ts": st.st_mtime})
    return sorted(found, key=lambda r: (r["ts"], r["n"])), taken, None


def _console_lands(root, after=None):
    """([{sha, ts, subject}] newest first, why): trunk's first-parent commits
    that touch the console, those after `after` only, or the newest one when
    `after` is None."""
    be = vcs.backend(root)
    ref = be.trunk_ref(root)
    args = ["log", "--first-parent", "--format=%H%x09%ct%x09%s"]
    args += ["-1"] if after is None else ["--since=@%d" % (int(after) - 1)]
    rc, out, err = be.text(root, *(args + [ref, "--"] + list(CONSOLE_PATHS)))
    if rc != 0:
        return None, "`git log %s` failed: %s" % (ref, err or "rc %d" % rc)
    lands = []
    for line in out.splitlines():
        sha, ts, subject = (line.split("\t", 2) + ["", ""])[:3]
        if sha and ts.isdigit() and (after is None or int(ts) > after):
            lands.append({"sha": sha, "ts": int(ts), "subject": subject})
    return lands, None


def owed(root, now=None):
    """Whether a console walk is owed, derived now. -> {owed (True, False or
    None when trunk cannot be read), reason, land (the land that made it owed,
    or None), since, age_s, last (the newest report, or None), next_n,
    next_path}."""
    now = time.time() if now is None else now
    reports, taken, why = _reports(root)
    last = reports[-1] if reports else None
    next_n = max(taken or [0]) + 1
    got = {"owed": False, "reason": None, "land": None, "since": None,
           "age_s": None, "last": last, "next_n": next_n,
           "next_path": os.path.join(reviews_dir(root), "%s-console-walk-%d.md"
                                     % (_date(now), next_n))}
    if not why:
        lands, why = _console_lands(root,
                                    None if last is None else last["ts"])
    if why:
        got.update(owed=None, reason="UNKNOWN: %s" % why)
        return got
    if last is None:
        land = lands[0] if lands else None
        got.update(owed=True, land=land, since=land["ts"] if land else None,
                   reason="no walk on record")
    else:
        land = lands[-1] if lands else None      # the first land after it
        aged = last["ts"] + WALK_EVERY_S
        name = os.path.basename(last["path"])
        # one source names the cause and starts the age: whichever came
        # first, the land after the walk or the walk turning old
        if land and land["ts"] < aged:
            got.update(owed=True, land=land, since=land["ts"],
                       reason="a console land after the last walk (%s)"
                              % name)
        elif now >= aged:
            got.update(owed=True, since=aged,
                       reason="the last walk (%s) is %d days old"
                              % (name, WALK_EVERY_S // 86400))
        else:
            got["reason"] = "walked %s ago (%s); no console land since" % (
                _age(now - last["ts"]), name)
    if got["owed"] and got["since"] is not None:
        got["age_s"] = int(now - got["since"])
    return got


def _land_words(land):
    subject = land["subject"]
    if len(subject) > 80:
        # a cut inside a parenthetical drops it whole, never half of it:
        # the OUTERMOST one still open at the cut, so a nested pair that
        # closed before the cut cannot leave its parent open
        subject = subject[:80]
        open_at = []
        for i, ch in enumerate(subject):
            if ch == "(":
                open_at.append(i)
            elif ch == ")" and open_at:
                open_at.pop()
        if open_at:
            subject = subject[:open_at[0]].rstrip()
        subject += "…"
    return "%s %s" % (land["sha"][:11], subject)


def owed_line(got, hint=True):
    """The one line that says a walk is owed, or None when none is. `hint`
    names the verb that prints the brief; the brief itself leaves it out."""
    if not got["owed"]:
        return None
    head = "console walk owed"
    if got["last"] is None:
        head += " (no walk on record)"
    if got["land"]:
        since = " since %s" % _land_words(got["land"])
    elif got["last"] is not None:
        since = " since %s turned %d days old" % (
            os.path.basename(got["last"]["path"]), WALK_EVERY_S // 86400)
    else:
        since = ""
    age = ", %s" % _age(got["age_s"]) if got["age_s"] is not None else ""
    return "%s%s%s.%s" % (head, since, age, " `helm web walk` prints the "
                          "brief to hand a reader who built none of it."
                          if hint else "")


def brief(root, now=None, checks=()):
    """The walk's method, filled for this walk, to hand a fresh reader.
    `checks`, a sequence of one-line checks, prints as a "CHECK THESE
    (release gate)" block before the method (task/3938)."""
    got = owed(root, now=now)
    if got["owed"] is None:
        status = "Whether a walk is owed is %s." % got["reason"]
    elif got["owed"]:
        status = owed_line(got, hint=False)
        if got["last"] is not None:
            status += " Reason: %s." % got["reason"]
    else:
        status = "No walk is owed (%s); a walk now is still welcome." \
            % got["reason"]
    from .web_common import DEFAULT_PORT    # DEFERRED — the owner's port
    widths = " and ".join("%d px" % w for w in WIDTHS)
    lines = []
    checks = tuple(c for c in (checks or ()) if isinstance(c, str)
                    and c.strip())
    if checks:
        lines.append("CHECK THESE (release gate)")
        for i, c in enumerate(checks, 1):
            lines.append("  %d. %s" % (i, c))
        lines.append("")
    lines.append("\n".join((
        "CONSOLE WALK %d — walk the owner's web console whole, as a fresh "
        "reader." % got["next_n"],
        "",
        status,
        "",
        "WHO: a reader who built none of the change and carries no context "
        "from its review.",
        "",
        "METHOD:",
        "- Walk the owner's own console, the server his machine already "
        "runs, at http://127.0.0.1:%d, in one browser tab. Walk EVERY page "
        "and EVERY section, at %s." % (DEFAULT_PORT, widths),
        "- Start no server: never run `helm web` or `helm web shot`. Each "
        "starts a server of its own, and a second console server on the "
        "owner's laptop is what the laptop-health rule bans (one reached "
        "4.5 GB).",
        "- Read-only: click nothing that writes (no post, verdict, toggle, "
        "save or delete).",
        "- Never open Chat, nor any address under it: a click, key or scroll "
        "there marks the owner's unread messages as read. Read chat's counts "
        "off the nav badge and Home only.",
        "- Save a screenshot of each page at each width from that same tab, "
        "and name the screenshot a finding cites. Close the browser when "
        "the walk is done.",
        "- Write the report to:",
        "    %s" % got["next_path"],
        "",
        "EACH FINDING, most harmful first:",
        "  Grade: P1, P2 or P3 (the grades below)",
        "  Where: page › section, width",
        "  What: what the owner sees",
        "  Evidence: MEASURED (the reading, with the screenshot name) or "
        "INFERRED",
        "  Rule: which rule below it breaks, or none",
        "  Direction: what would cure it",
        "",
        "THE OWNER'S TWO RULES:",
        "  RULE 1: one number and one word per noun everywhere. The same count "
        "reads the same on every page, its scope is named, and no interface "
        "is duplicated.",
        "  RULE 2: the console never tells the owner to run a CLI verb (he "
        "does not use a terminal), and every noun links to its home.",
        "",
        "THE GRADES:",
        "  P1: on a page body, a head, a badge or a tile the owner reads "
        "directly, the console shows a false or contradicting number or word "
        "for one noun (RULE 1), or it shows him a CLI verb to run (RULE 2).",
        "  P2: the same kind of break in a hover, a footer or a secondary "
        "fold, or on a surface that often fails to read.",
        "  P3: weak or cosmetic.",
    )) + "\n")
    return "\n".join(lines)


def _read_said(root):
    try:
        with open(_said_path(root)) as f:
            return (json.load(f) or {}).get("key")
    except (OSError, ValueError, AttributeError):
        return None


def _write_said(root, key, now):
    path = _said_path(root)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = "%s.%d.tmp" % (path, os.getpid())
        with open(tmp, "w") as f:
            json.dump({"key": key, "ts": now}, f)
        os.replace(tmp, path)
    except OSError as exc:
        return "%s: %s" % (path, exc)
    return None


def surface(root, post, now=None, say=None):
    """Say the walk owed through `post` (text -> ok) ONCE per owed state.
    -> the line when it was said now, else None.

    The owed state is the newest report ("-" when none is on record) and
    nothing else: a later console land, or the walk turning old after a land
    was said, is the same state and is not said again, and only a new walk
    starts a new one. A post that fails records nothing and is said on the
    next call; a record that fails after the post is told to `say`."""
    now = time.time() if now is None else now
    got = owed(root, now=now)
    if not got["owed"]:
        return None
    key = "%s|owed" % (os.path.basename(got["last"]["path"])
                       if got["last"] else "-")
    if _read_said(root) == key:
        return None
    line = owed_line(got)
    if not post(line):
        return None
    failed = _write_said(root, key, now)
    if failed and say is not None:
        say("console walk owed was said but not recorded (%s); it will be "
            "said again on the next land" % failed)
    return line


def _release_design_doc(reviews_dir_path, release):
    """The design doc for `release` in the project's reviews dir: the file
    whose name ends in -design.md and carries the release (a date-stamped
    <date>-<release>-design.md), or None when there is no such doc."""
    try:
        names = os.listdir(reviews_dir_path)
    except OSError:
        return None
    for name in sorted(names):
        if name.endswith("-%s-design.md" % release):
            return os.path.join(reviews_dir_path, name)
    return None


def _acceptance_console_checks(doc_path):
    """The release-gate checks on the design doc's Acceptance section: the
    single item that names "the owner's console", its clauses split on the
    commas and the final "and" into the checks to run, in order. -> a list
    of check strings, or None when the doc, its Acceptance section or its
    console line cannot be read."""
    try:
        with open(doc_path) as fh:
            text = fh.read()
    except OSError:
        return None
    m = re.search(r"^##\s*Acceptance\s*\(.*?\)[^\n]*\n(.*?)(?=\n##\s|$)",
                  text, re.M | re.S)
    if not m:
        return None
    for line in m.group(1).splitlines():
        if "owner's console" not in line:
            continue
        clause = re.sub(r"^\s*\d+\.\s*", "", line).strip()
        m2 = re.search(r"console:\s*([^.]*)", clause)
        if not m2:
            return None
        rest = m2.group(1).strip().rstrip(".")
        items = [p.strip() for p in re.split(r",\s+", rest)]
        if items and items[-1].lower().startswith("and "):
            items[-1] = items[-1][4:].strip()
        return [c for c in items if c.strip()]
    return None


def release_checks(root, release):
    """The checks a release's confirm walk verifies, read from the design
    doc's Acceptance section console line (task/3938). Graceful degrade: an
    unreadable doc, missing Acceptance section or missing console line gives
    an empty list, never an error — the walk prints no check block in that
    case."""
    doc = _release_design_doc(reviews_dir(root), release)
    if doc is None:
        return []
    return _acceptance_console_checks(doc) or []


def cmd_walk(args):
    """web walk [--repo DIR] [--check "TEXT"]... [--release VERSION] — print
    the console walk brief. Repeated --check (or --release, which reads the
    checks from the design doc's Acceptance section console line) prints a
    "CHECK THESE (release gate)" block in the brief."""
    repo = None
    checks = []
    release = None
    args = list(args or [])
    while args:
        a = args.pop(0)
        if a == "--repo" and args:
            repo = args.pop(0)
        elif a.startswith("--repo="):
            repo = a.split("=", 1)[1]
        elif a == "--check" and args:
            checks.append(args.pop(0))
        elif a.startswith("--check="):
            checks.append(a.split("=", 1)[1])
        elif a == "--release" and args:
            release = args.pop(0)
        elif a.startswith("--release="):
            release = a.split("=", 1)[1]
        elif a in ("-h", "--help"):
            print("usage: helm web walk [--repo DIR] [--check CHECK]... "
                  "[--release VERSION] — print the brief a fresh reader "
                  "walks the console by")
            return 0
        else:
            print("usage: helm web walk [--repo DIR] [--check CHECK]... "
                  "[--release VERSION]", file=sys.stderr)
            return 2
    root = _lanes.find_root(repo or os.getcwd())
    if not root:
        print("helm web walk: %s is not inside a git repository"
              % (repo or os.getcwd()), file=sys.stderr)
        return 1
    if release:
        checks = release_checks(root, release) + list(checks or [])
    print(brief(root, checks=checks), end="")
    return 0
