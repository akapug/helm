#!/usr/bin/env python3
"""Publish a helm release: ONE command, and a dry run unless you say --publish.

    python3 scripts/release/release.py 0.3.2              # dry run (the default)
    python3 scripts/release/release.py 0.3.2 --publish    # the real thing
    python3 scripts/release/release.py --nightly          # the nightly's dry run

WHAT A RELEASE IS. The public repository's main is a line of release commits,
one per version. Each one is authored by the public repository's owner, its
parent is the previous release, and its tree is the trunk tree minus the paths
in `scripts/release/omit.txt`. The development history stays in the private
repository. So a release is one new commit and a fast-forward, never a
history rewrite and never a force push.

WHAT THE COMMAND DOES, IN ORDER. Steps 1 to 4 run in every mode and write only
inside the work directory. Step 5 is the network writes: the dry run prints
each one and performs none of them; --publish performs them.

  1. Read. The trunk commit, its CHANGELOG section for the version (the
     release notes) and its `__version__`; the public main; the public tags.
  2. Back up the public repository: a bare mirror in the work directory,
     fsck clean, its main equal to the public main just read. The work
     directory is kept, so it is on disk: by default a new directory for
     each run under ~/.helm/releases/<version>, and a --work on a memory
     filesystem (tmpfs, ramfs) or inside the checkout is refused.
  3. Build the candidate: the trunk tree minus the omit list, committed on
     top of the public main as the owner, dated like the trunk commit. The
     same inputs build the same commit, so a reviewed dry run and the publish
     after it name one object. The annotated tag v<version> is made here too,
     in the work repository only.
  4. Gate the candidate. It must fast-forward the public main by exactly one
     commit that changes the tree, and that tree must be the trunk tree minus
     the omit list. Then the reports the owner reads before a public release
     are written under <work>/reports/ (0600, never in the repository): the
     census of private tokens, seat names, addresses, task cites, dates and
     the owner's voice that no gate refuses, the seat-attribution lines and
     the content counts. The seat-attribution gate refuses a line in helm/
     that names a seat as the one who found or reviewed something unless the
     KEEP list names it, and a KEEP entry that matches no line; without the
     KEEP list and the private patterns it does not run, and fails. Then the
     verify battery runs on a fresh clone: attribution
     lines and private needles in every message, the tag and the notes;
     forbidden filenames; private needles in every file; `bin/helm --help` and
     `bin/helm doctor` in a scratch HELM_HOME; the installer; and gitleaks.
  5. Write, in this order: stage the candidate on the private repository as
     release/<version>; push it to the public main (no force: a push that is
     not a fast-forward fails); push the tag; `gh release create --verify-tag`
     with the CHANGELOG section as the notes; then run the battery again on a
     fresh clone of the public repository.

Any failed read or gate is a refusal (exit 1) before any network write. A
publish that stops part-way (a write failed or timed out, or the public main
moved after the stage write) exits 3 and prints the writes that are still
owed, as commands; so does a published repository that fails the battery.
Any other error is printed as a refusal too, redacted and never as a
traceback: exit 1 before the first network write began, exit 3 after.
What the interpreter prints when even that line cannot be printed
(stdout closed under it) is redacted the same way.

THE NIGHTLY (--nightly). helm/releasenightly.py runs this command every night
on trunk as it stands, on a build host, to show that a release can still be
cut. It IS the dry run: steps 1 to 4 in this code, and none of step 5's
writes. Three things differ, each so that the answer is about the tree:
  - No version is given. Between releases trunk still declares the version
    last released, whose tag is already public, so a dry run of that version
    refuses at the read and judges no gate. The nightly rehearses the next
    cut of it instead: version <__version__>-nightly (its tag, like every dry
    run's, is made in the work repository only), with the CHANGELOG's newest
    section as the notes. Bumping the version and writing its section stay
    the owner's acts at the cut, and a real dry run checks them then.
  - gitleaks is required, as --publish requires it. A dry run skips it when
    it is not installed, and a nightly that did would read green where the
    publish refuses.
  - The default work directory is ~/.helm/releases/nightly, and each run
    keeps the newest NIGHTLY_KEEP runs there: one run a night leaves a clone
    and a backup behind. Only directories this command named are removed.
It closes with one line for the job to read: `NIGHTLY GREEN trunk=<sha>
candidate=<sha>`, or `NIGHTLY RED step=<step> trunk=<sha>` naming the first
step that refused: read, backup, build, gate/candidate, gate/tree,
gate/reports, gate/attribution, gate/sweep, gate/battery, gate/smoke,
gate/gitleaks, gate/summary.

THIS FILE IS STANDALONE. It runs from the checkout that holds it and imports
nothing from the helm package. It loads two sibling files that are themselves
standalone hook scripts: helm/nevertrack.py (the machine's private needles and
never-track paths, read from OUTSIDE the tree) and helm/trailer_rung.py (what
counts as an AI authoring line). The KEEP list and the private patterns are
read from OUTSIDE the tree too (~/.config/helm/, owner-only; see PRIVATE).
"""
import argparse
import collections
import importlib.util
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OMIT = "scripts/release/omit.txt"
NOREPLY = "users.noreply.github.com"

# THE PUBLISH MARKER. The managed pre-push hook's shared-history rung
# (helm/hostpath_guard.py) refuses a push whose commits share no history with
# the destination's default branch, and the stage write is exactly that: a
# release commit on the public line, pushed to the private repository. Only
# `publish()` sets this, in the environment of its git pushes, to the one
# commit it publishes; the rung passes a push when every commit it carries is
# that commit. The rung reads the same literal.
RELEASE_MARKER = "HELM_RELEASE_PUBLISH"

# GIT SELECTION ENV OVERRIDES `-C`, so every git call here runs without it.
# The same list as scripts/deploy.py and dispatches._GIT_SELECTION_ENV.
_GIT_SELECTION_ENV = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE",
                      "GIT_COMMON_DIR", "GIT_OBJECT_DIRECTORY",
                      "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_NAMESPACE",
                      "GIT_CEILING_DIRECTORIES")

# Paths that never ship, whatever the omit list says. Each with its reason.
FORBIDDEN_NAMES = (
    (re.compile(r"(^|/)\.env$"), "an environment file holds credentials"),
    (re.compile(r"(^|/)secrets?(/|\.|$)"), "a secrets file or directory"),
    (re.compile(r"(^|/)\.remember(/|$)"), "a session-memory store"),
    (re.compile(r"(^|/)\.claims(/|$)"), "a lease store"),
    (re.compile(r"(^|/)journal/"), "a working journal"),
    (re.compile(r"\.pem$|(^|/)id_(rsa|ecdsa|ed25519)$"), "a private key"),
)

# gitleaks findings that were read and accepted, by file, with why.
GITLEAKS_REVIEWED = {
    "tests/fixtures/chatnode_rebased_genesis/genesis.json":
        "a published genesis fixture whose keys are test keys (reviewed at 0.3.0)",
}

DOCTOR_LINE = re.compile(r"^helm doctor: \d+ ok, \d+ warn, \d+ fail", re.M)

# The work directory keeps the backup of the public repository, so it must
# outlive a reboot: a directory on one of these filesystems is memory, and is
# refused. The mount table names the filesystem that holds a path.
MEMORY_FILESYSTEMS = ("tmpfs", "ramfs")
MOUNTINFO = "/proc/self/mountinfo"
RELEASES = os.path.join("~", ".helm", "releases")

# The nightly (--nightly): its version suffix, the directory under RELEASES
# its runs share, how many runs it keeps there, and the names it gives them.
NIGHTLY_SUFFIX = "-nightly"
NIGHTLY_DIR = "nightly"
NIGHTLY_KEEP = 7
NIGHTLY_RUN = re.compile(r"^\d{8}T\d{6}Z-nightly-[a-z0-9_]+$")

# THE PRIVATE FILES. Two checks need lists that name private things (seats,
# people, projects), which is exactly what must not ship, so they are read
# from OUTSIDE the tree: files only their owner can reach, refused unread when
# anyone else can, and refused inside the checkout.
#   the KEEP list        JSON [{"file", "text", "reason"}]: the seat-attribution
#                        lines in helm/ that may ship, each with its reason.
#   the private patterns JSON {"seat", "private_token", "owner": regular
#                        expressions; "literals": {label: text}}: a seat
#                        instance or handle, a private token, the owner's
#                        name, and literal strings to count.
PRIVATE = os.path.join("~", ".config", "helm")
KEEP_FILE = "release-attr-keep.json"
PATTERNS_FILE = "release-audit.json"
PATTERN_KEYS = ("seat", "private_token", "owner")

# A seat-attribution line: in helm/ production code, a line that names a seat
# (the private `seat` pattern) and carries a verb of finding. Who found,
# caught, measured or reviewed something is the private engineering record,
# not a property of the code.
ATTR_SUFFIXES = (".py", ".part", ".html", ".js", ".css", ".md")
ATTR_VERB = re.compile(
    r"(?i)\b(found|finds|caught|catch|measured|flagged|raised|review(ed|er)?|"
    r"named|noticed|asked|ruled|reported|proposed|refuted|witnessed|spotted|"
    r"pointed|observed|confirmed|objected|wrote|drove|diagnosed|traced|filed|"
    r"argued)\b|'s\b")

# The owner-read census: what no gate refuses, counted for the owner to read.
# Each class is (name, its pattern here, its key in the private patterns); a
# class with neither loaded is NOT RUN.
CENSUS = (
    ("private-token", None, "private_token"),
    ("ipv4-private", re.compile(
        r"\b(10\.\d{1,3}|192\.168|172\.(1[6-9]|2\d|3[01])|"
        r"100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7])|169\.254)\.\d{1,3}\.\d{1,3}\b"), None),
    ("ipv4-public", re.compile(
        r"\b(?!(?:10|127|0)\.)(?!192\.168\.)(?!192\.0\.2\.)(?!198\.51\.100\.)"
        r"(?!203\.0\.113\.)(?!169\.254\.)(?!172\.(?:1[6-9]|2\d|3[01])\.)"
        r"(?!100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.)"
        r"(?:[1-9]\d?|1\d\d|2[0-4]\d|25[0-5])"
        r"(?:\.(?:\d{1,2}|1\d\d|2[0-4]\d|25[0-5])){3}\b"), None),
    ("task-cite", re.compile(r"\btask/\d+"), None),
    ("date-prose", re.compile(r"\b20\d\d-[01]\d-[0-3]\d\b"), None),
    ("owner-voice", re.compile(
        r"(?i)\bthe owner('s)? (said|asked|ruled|wrote|told|wants|decided|"
        r"directive|ask|ruling|quote)|owner (ruling|directive|ask|quote)"), "owner"),
    ("seat-token", None, "seat"),
    ("incident", re.compile(
        r"(?i)\b(incident|post-mortem|postmortem|halt point|halted all)\b"), None),
)
EMAIL = re.compile(r"[A-Za-z0-9._%+-]{1,64}@([A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63})+)")
PLACEHOLDER = re.compile(r"(?i)(^|\.)(example(\.(com|org|net))?|test|invalid|"
                         r"localhost|local)$|users\.noreply\.github\.com$|"
                         r"^helm\.fleet$")
NOT_A_DOMAIN = re.compile(r"\.(py|md|json|txt|sh|js|html|css|jsonl|yml|part)$")
# Literal strings the content report counts on every machine; the private
# patterns file adds its own.
LITERALS = {"claude_homes": ".claude-homes/",
            "session_url": "claude.ai/code/session"}
OWNER_READ = ("OWNER READ. It holds private values: never commit it, never "
              "paste it anywhere public.")


class Refusal(Exception):
    """A read or a gate said no. Nothing on the network was written."""


# The private needles this run loaded; Release registers them, and until it
# does there are none and redact() changes nothing.
NEEDLES = []


def _spans(text):
    """(start, stop, number) of every occurrence of every loaded needle in
    `text` as given, overlapping occurrences included; earliest first, and of
    one start the longest first."""
    spans = []
    for i, needle in enumerate(NEEDLES, 1):
        at = text.find(needle) if needle else -1
        while at >= 0:
            spans.append((at, at + len(needle), i))
            at = text.find(needle, at + 1)
    return sorted(spans, key=lambda s: (s[0], -s[1], s[2]))


def redact(text):
    """`text` with every loaded needle replaced by its number. Every needle is
    found in the text as given, before any is replaced: a needle replaced
    first would cut one that contains or overlaps it, and the rest of that
    one would print. One inside another's span is not named; two that overlap
    with neither inside the other are both named, in the order they start."""
    out, pos, end = [], 0, 0
    for start, stop, i in _spans(text):
        if start >= end:
            out.append(text[pos:start])
        elif stop <= end:
            continue
        out.append("<needle #%d>" % i)
        pos = end = stop
    out.append(text[pos:])
    return "".join(out)


def tail(text, n):
    """The last `n` characters of `text`, redacted BEFORE the cut: a cut that
    falls inside a needle leaves a piece that no longer matches it."""
    return redact(text.strip())[-n:]


def _load(name):
    spec = importlib.util.spec_from_file_location(
        "_release_" + name, os.path.join(ROOT, "helm", name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _env(**extra):
    env = {k: v for k, v in os.environ.items() if k not in _GIT_SELECTION_ENV}
    env.update(extra)
    return env


def git(repo, *args, env=None, check=True, input=None):
    """stdout of `git -C repo args`; a failure raises Refusal when `check`.
    A timeout always raises Refusal: the TimeoutExpired it replaces prints
    the whole argv, which names remotes and paths, past the redacting door."""
    try:
        p = subprocess.run(("git", "-C", repo) + args, input=input,
                           capture_output=True, text=True, env=env or _env(),
                           timeout=600)
    except subprocess.TimeoutExpired as e:
        raise Refusal("git %s: timed out after %g s" % (" ".join(args), e.timeout))
    if check and p.returncode != 0:
        raise Refusal("git %s: %s" % (" ".join(args),
                                      tail(p.stderr or p.stdout, 400)))
    return p.stdout.strip() if check else p


def say(tag, text):
    print("%-8s %s" % (tag, text), flush=True)


def resolve_remote(source, spec):
    """A remote NAME of the source checkout becomes its URL; anything else
    (a URL or a path) is used as given."""
    p = git(source, "remote", "get-url", spec, check=False)
    if p.returncode == 0 and p.stdout.strip():
        return p.stdout.strip()
    return os.path.abspath(spec) if os.path.isdir(spec) else spec


def mount_of(path, mountinfo=MOUNTINFO):
    """(mount point, filesystem type) of the mount that holds `path`: of the
    mount points in the table that are the real path or one of its
    ancestors, the longest, and of two with one mount point the one listed
    last (it is mounted on top). Raises Refusal, with the reason, when the
    table cannot be read, a line of it cannot be parsed, or no mount covers
    the path: a filesystem that cannot be determined is not assumed to be a
    disk."""
    real = os.path.realpath(path)
    why = "cannot tell which filesystem holds %s (%s)" % (path, real)
    try:
        with open(mountinfo, encoding="utf-8", errors="surrogateescape") as f:
            lines = f.read().splitlines()
    except OSError as e:
        raise Refusal("%s: cannot read %s: %s" % (why, mountinfo, e.strerror or e))
    best = None
    for n, line in enumerate(lines, 1):
        left, sep, right = line.partition(" - ")
        fields, fstype = left.split(), right.split()[:1]
        if not sep or len(fields) < 5 or not fstype:
            raise Refusal("%s: cannot parse line %d of %s: %r"
                          % (why, n, mountinfo, redact(line)[:120]))
        point = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)),
                       fields[4])
        if ((real == point or real.startswith(point.rstrip("/") + "/"))
                and (best is None or len(point) >= len(best[0]))):
            best = (point, fstype[0])
    if best is None:
        raise Refusal("%s: no mount in %s covers it" % (why, mountinfo))
    return best


def on_disk(path, mountinfo=MOUNTINFO):
    """(mount point, filesystem type) of `path`, refused when that is memory."""
    point, fstype = mount_of(path, mountinfo)
    if fstype in MEMORY_FILESYSTEMS:
        raise Refusal("the work directory %s is on %s mounted at %s, which is "
                      "memory and is emptied at reboot; the work directory "
                      "keeps the backup of the public repository, so pass a "
                      "--work on disk (the default is under %s)"
                      % (path, fstype, point, RELEASES))
    return point, fstype


def inside(path, root):
    """Whether `path` is `root` or under it, both taken as real paths."""
    path, root = os.path.realpath(path), os.path.realpath(root)
    return path == root or path.startswith(root.rstrip("/") + "/")


def load_private(path, what, parse, checkouts):
    """(parse(the JSON in `path`), None), or (None, why it was not read).

    The file names private things, so it is read only from outside the
    checkout and only when its owner alone can reach it. The mode is judged
    on the open descriptor, so the file judged is the file read, and a
    refused file is never parsed. Any group or other bit refuses, not only
    the read bits: a list others can write is a list others can edit. No
    reason quotes the content."""
    for root in checkouts:
        if inside(path, root):
            return None, ("%s %s is inside the checkout %s; it names private "
                          "things, so it lives outside the tree" % (what, path, root))
    try:
        f = open(path, encoding="utf-8",
                 opener=lambda p, flags: os.open(p, flags | os.O_NONBLOCK))
    except FileNotFoundError:
        return None, "%s %s is absent" % (what, path)
    except OSError as e:
        return None, "%s %s cannot be opened: %s" % (what, path, e.strerror or e)
    with f:
        st = os.fstat(f.fileno())
        if not stat.S_ISREG(st.st_mode):
            return None, "%s %s is not a regular file (%s)" % (
                what, path, stat.filemode(st.st_mode))
        if st.st_mode & 0o077 or st.st_uid != os.getuid():
            return None, ("%s %s is %s (owner uid %d): someone other than its "
                          "owner can reach it, so it was not read; run "
                          "`chmod 600 %s` as its owner"
                          % (what, path, stat.filemode(st.st_mode), st.st_uid, path))
        try:
            data = json.load(f)
        except ValueError as e:
            return None, "%s %s is not valid JSON (line %s, column %s)" % (
                what, path, getattr(e, "lineno", "?"), getattr(e, "colno", "?"))
        try:
            return parse(data), None
        except ValueError as e:
            return None, "%s %s is not usable: %s" % (what, path, e)


def parse_keep(rows):
    """{(file, stripped line): reason} from the KEEP list's JSON."""
    if not isinstance(rows, list) or not all(
            isinstance(r, dict) and all(isinstance(r.get(k), str) and r[k].strip()
                                        for k in ("file", "text", "reason"))
            for r in rows):
        raise ValueError("it must be a JSON list of objects, each with a "
                         "non-empty \"file\", \"text\" and \"reason\"")
    return {(r["file"], r["text"]): r["reason"] for r in rows}


def parse_patterns(data):
    """{"seat", "private_token", "owner": compiled; "literals": {label: text}}
    from the private patterns' JSON. Keys starting with '_' are comments."""
    if not isinstance(data, dict):
        raise ValueError("it must be a JSON object")
    known = PATTERN_KEYS + ("literals",)
    missing = [k for k in known if k not in data]
    unknown = sorted(k for k in data if k not in known and not k.startswith("_"))
    if missing or unknown:
        raise ValueError("; ".join(
            ["missing %s" % ", ".join(missing)] * bool(missing)
            + ["%d unknown key%s; expected %s"
               % (len(unknown), "" if len(unknown) == 1 else "s",
                  ", ".join(known))] * bool(unknown)))
    got = {}
    for key in PATTERN_KEYS:
        if not isinstance(data[key], str) or not data[key]:
            raise ValueError("%s must be a non-empty regular expression" % key)
        try:
            got[key] = re.compile(data[key])
        except re.error as e:
            raise ValueError("%s is not a regular expression (%s)" % (key, e.msg))
    lits = data["literals"]
    if not isinstance(lits, dict) or not all(
            isinstance(v, str) and v for v in lits.values()):
        raise ValueError("literals must be an object of {label: non-empty text}")
    got["literals"] = lits
    return got


def attribution(files, seat):
    """(files scanned, [(path, line number, stripped line)] naming a seat,
    the subset that also carries a verb of finding) over helm/ production
    code in `files` ({path: bytes})."""
    scanned, named, found = 0, [], []
    for path in sorted(files):
        if not path.startswith("helm/") or not path.endswith(ATTR_SUFFIXES):
            continue
        scanned += 1
        text = files[path].decode("utf-8", "replace")
        for n, line in enumerate(text.splitlines(), 1):
            if seat.search(line):
                named.append((path, n, line.strip()))
                if ATTR_VERB.search(line):
                    found.append((path, n, line.strip()))
    return scanned, named, found


def _top(counter, n=6):
    return ", ".join("%s(%d)" % kv for kv in counter.most_common(n))


def census(files, patterns, why, sha):
    """-> (world_audit.txt, {class: occurrences}). What no gate refuses,
    counted by class with the files that carry most of it. Without the
    private patterns (`why` says why) the private classes are NOT RUN."""
    classes = [(name, [r for r in (rx, patterns and key and patterns[key]) if r]
                or None) for name, rx, key in CENSUS]
    tot, where = collections.Counter(), collections.defaultdict(collections.Counter)
    tops = collections.defaultdict(lambda: [0, 0, 0])
    tokens, domains, lines = collections.Counter(), collections.Counter(), 0
    for path, data in sorted(files.items()):
        top = ("helm (production)" if path.startswith("helm/")
               else path.split("/", 1)[0] if "/" in path else "(root)")
        tops[top][0] += 1
        tops[top][2] += len(data)
        if b"\0" in data[:8192]:
            continue
        text = data.decode("utf-8", "replace")
        tops[top][1] += text.count("\n")
        lines += text.count("\n")
        for name, rxs in classes:
            c = sum(len(rx.findall(text)) for rx in rxs or ())
            if c:
                tot[name] += c
                where[name][path] += c
        if patterns:
            tokens.update(m.group(0).lower() for m in patterns["private_token"].finditer(text))
        for m in EMAIL.finditer(text) if "@" in text else ():
            d = m.group(1).lower()
            if not PLACEHOLDER.search(d) and not NOT_A_DOMAIN.search(d):
                tot["email-nonplaceholder"] += 1
                where["email-nonplaceholder"][path] += 1
                domains[d] += 1
    out = [OWNER_READ, "",
           "census of candidate %s: %d paths, %d text lines" % (sha, len(files), lines),
           "private patterns: " + ("loaded" if patterns else "NOT LOADED: %s; "
                                   "private-token, seat-token and the owner's name "
                                   "are NOT RUN" % why),
           "", "by top-level: files / text lines / bytes"]
    out += ["  %-22s %5d files %8d lines %10d bytes" % (t, f, n, b)
            for t, (f, n, b) in sorted(tops.items(), key=lambda kv: -kv[1][1])]
    out += ["", "class totals (occurrences / files) and top files:"]
    for name, rxs in classes + [("email-nonplaceholder", ())]:
        out.append("  %-22s NOT RUN" % name if rxs is None else
                   "  %-22s %6d / %-4d  %s" % (name, tot[name], len(where[name]),
                                               _top(where[name])))
    out += ["", "private-token breakdown: " + ", ".join("%s=%d" % kv for kv in tokens.most_common()),
            "email domains (non-placeholder): " + ", ".join(
                "%s=%d" % kv for kv in domains.most_common(25))]
    ran = [name for name, rxs in classes if rxs] + ["email-nonplaceholder"]
    return "\n".join(out) + "\n", {name: tot[name] for name in ran}


def content(files, patterns, why, needles, needles_path, sha):
    """content.txt: the private needles (by number), the literal strings, each
    seat token and the addresses that are not placeholders, with counts and
    the files that carry most of each. Every blob, binary too."""
    tot, where = collections.Counter(), collections.defaultdict(collections.Counter)
    domains = collections.Counter()
    literals = dict(LITERALS, **(patterns["literals"] if patterns else {}))
    for path, data in sorted(files.items()):
        hits = [("needle#%d" % i, data.count(n.encode()))
                for i, n in enumerate(needles, 1)]
        hits += [("literal:" + k, data.count(v.encode())) for k, v in literals.items()]
        text = data.decode("utf-8", "replace")
        if patterns:
            hits += [("seat:" + m.group(0).lower(), 1)
                     for m in patterns["seat"].finditer(text)]
        for m in EMAIL.finditer(text) if "@" in text else ():
            d = m.group(1).lower()
            if not PLACEHOLDER.search(d) and not NOT_A_DOMAIN.search(d):
                hits.append(("email:non-placeholder", 1))
                domains[d] += 1
        for key, c in hits:
            if c:
                tot[key] += c
                where[key][path] += c
    out = [OWNER_READ, "", "== candidate %s tree (%d files)" % (sha, len(files))]
    out += ["  %-26s total=%-6d files=%-4d top: %s" % (k, tot[k], len(where[k]),
                                                       _top(where[k], 4))
            for k in sorted(tot)]
    out += ["  email domains: " + ", ".join("%s(%d)" % kv for kv in domains.most_common(15)),
            "  needles loaded: %d (from %s)" % (len(needles), needles_path),
            "  private patterns: " + ("loaded" if patterns else
                                      "NOT LOADED: %s; seat tokens and the private "
                                      "literals are NOT RUN" % why)]
    return "\n".join(out) + "\n"


def github_repo(url):
    """"owner/name" for a GitHub remote URL, else None."""
    m = re.match(r"^(?:git@github\.com:|ssh://git@github\.com/|"
                 r"https://github\.com/)([^/]+)/([^/]+?)(?:\.git)?/?$", url)
    return "%s/%s" % m.groups() if m else None


def same_repo(a, b):
    """Whether two remote specs (URLs or paths) name one repository: the same
    string, the same GitHub owner/name, or the same directory."""
    if a == b:
        return True
    ga, gb = github_repo(a), github_repo(b)
    if ga and gb:
        return ga.lower() == gb.lower()
    return (os.path.isdir(a) and os.path.isdir(b)
            and os.path.realpath(a) == os.path.realpath(b))


def changelog_section(text, version):
    """The body under `## <version>` up to the next `## ` heading, or None."""
    lines = text.splitlines()
    head = re.compile(r"^## %s(\s|$)" % re.escape(version))
    for i, line in enumerate(lines):
        if head.match(line):
            end = next((j for j in range(i + 1, len(lines))
                        if lines[j].startswith("## ")), len(lines))
            return "\n".join(lines[i + 1:end]).strip() or None
    return None


def newest_section(text):
    """(its heading, its body) of the first `## ` section with text under
    it, or (None, None). The nightly's notes: the section a cut made now
    would publish nearest to."""
    for line in text.splitlines():
        words = line[3:].split() if line.startswith("## ") else []
        body = changelog_section(text, words[0]) if words else None
        if body:
            return line[3:].strip(), body
    return None, None


def default_message(version, notes):
    """`helm <version>`, the section's first paragraph that is not the
    "Changes since" line, and where the full list lives."""
    paras = [p.strip() for p in re.split(r"\n\s*\n", notes) if p.strip()]
    lead = next((p for p in paras if not p.startswith("Changes since")), "")
    tail = "CHANGELOG.md's %s section lists every change." % version
    return "helm %s\n\n%s%s\n" % (version, lead + "\n\n" if lead else "", tail)


def omit_entries(text):
    return [ln.strip() for ln in text.splitlines()
            if ln.strip() and not ln.lstrip().startswith("#")]


def omitted(path, entries):
    return any(path.startswith(e) if e.endswith("/") else path == e
               for e in entries)


def tree_map(repo, rev):
    """{path: "mode type sha"} for every entry of `rev`'s tree."""
    out = git(repo, "ls-tree", "-r", "-z", "--full-tree", rev) + "\0"
    return {e.split("\t", 1)[1]: e.split("\t", 1)[0]
            for e in out.split("\0") if "\t" in e}


def blobs(repo, shas):
    """{sha: bytes} for the given blob shas, in one cat-file process."""
    if not shas:
        return {}
    try:
        p = subprocess.run(("git", "-C", repo, "cat-file", "--batch"),
                           input=("\n".join(shas) + "\n").encode(),
                           capture_output=True, env=_env(), timeout=600)
    except subprocess.TimeoutExpired as e:
        raise Refusal("git cat-file --batch: timed out after %g s" % e.timeout)
    if p.returncode != 0:
        raise Refusal("git cat-file --batch: %s"
                      % tail(os.fsdecode(p.stderr), 400))
    out, i, got = p.stdout, 0, {}
    for sha in shas:
        e = out.index(b"\n", i)
        size = int(out[i:e].split()[2])
        got[sha] = out[e + 1:e + 1 + size]
        i = e + 1 + size + 1
    return got


class Release:

    def __init__(self, args):
        self.a = args
        # A nightly has no version until read() derives it from trunk.
        self.version = args.version
        self.tag = "v" + args.version if args.version else None
        # The step now running: a refusal names the first one that refused.
        self.step = "read"
        self.nevertrack = _load("nevertrack")
        self.trailers = _load("trailer_rung")
        self.needles, self.needles_path = self.nevertrack.load_private_needles()
        NEEDLES[:] = self.needles
        # Whether a network write began: after one, an error can leave the
        # writes partial.
        self.begun = False

    def say(self, tag, text):
        """One redacting door for tagged terminal output after needles load."""
        say(tag, self.hide(text))

    def output(self, text):
        """One redacting door for untagged terminal output after needles load."""
        print(self.hide(text))

    # ---- 1. read ---------------------------------------------------------

    def read(self):
        a = self.a
        self.source = os.path.abspath(a.source)
        self.trunk = git(self.source, "rev-parse", "--verify",
                         a.trunk + "^{commit}")
        self.say("read", "trunk %s (%s) from %s" % (self.trunk, a.trunk, self.source))
        self.public = resolve_remote(self.source, a.public)
        self.private = resolve_remote(self.source, a.private)
        if same_repo(self.private, self.public):
            raise Refusal("the private repository %s is the public repository; "
                          "the stage write is a private branch (release/%s) and "
                          "would land on the public repo. Pass --private"
                          % (self.private, self.version))
        self.gh_repo = a.gh_repo or github_repo(self.public)
        if not self.gh_repo:
            raise Refusal("cannot tell the GitHub repository from the public "
                          "remote %s; pass --gh-repo OWNER/NAME" % self.public)
        owner = self.gh_repo.split("/", 1)[0]
        m = re.match(r"^\s*(.+?)\s*<([^>]+)>\s*$", a.identity or "")
        self.name, self.email = m.groups() if m else (owner, "%s@%s" % (owner, NOREPLY))

        def show(path):
            p = git(self.source, "show", "%s:%s" % (self.trunk, path), check=False)
            return p.stdout if p.returncode == 0 else None

        changelog = show("CHANGELOG.md") or ""
        init = show("helm/__init__.py") or ""
        m = re.search(r"^__version__\s*=\s*[\"']([^\"']+)[\"']", init, re.M)
        if a.nightly:
            self.rehearse(changelog, m.group(1) if m else None)
        else:
            self.notes = changelog_section(changelog, self.version)
            if not self.notes:
                raise Refusal("CHANGELOG.md at %s has no section for %s (a '## "
                              "%s' heading with text under it); the release "
                              "notes are that section" % (
                                  self.trunk[:12], self.version, self.version))
            if not m or m.group(1) != self.version:
                raise Refusal("helm/__init__.py at %s declares __version__ %s, "
                              "not %s" % (self.trunk[:12],
                                          m.group(1) if m else "(none)",
                                          self.version))
        self.omit = omit_entries(show(OMIT) or "")
        self.say("read", "CHANGELOG section %s: %d lines; omit list: %d entries"
            % (self.version, len(self.notes.splitlines()), len(self.omit)))
        if a.message_file:
            with open(a.message_file, encoding="utf-8") as f:
                self.message = f.read()
        else:
            self.message = default_message(self.version, self.notes)

        self.checkouts = self.checkout_roots()
        self.read_private()
        self.work, point, fstype = self.work_directory()
        self.say("work", "%s (%s at %s: kept, it holds the backup and the reports)"
            % (self.work, fstype, point))
        self.repo = os.path.join(self.work, "release.git")
        git(self.work, "init", "-q", "--bare", "--template=", self.repo)
        common = git(self.source, "rev-parse", "--git-common-dir")
        objects = os.path.join(os.path.abspath(os.path.join(self.source, common)),
                               "objects")
        with open(os.path.join(self.repo, "objects", "info", "alternates"), "w") as f:
            f.write(objects + "\n")

        p = git(self.repo, "fetch", "-q", "--no-tags", self.public,
                "+refs/heads/main:refs/scratch/public-main", check=False)
        if p.returncode != 0:
            raise Refusal("cannot read the public main from %s: %s"
                          % (self.public, tail(p.stderr, 300)))
        self.pub = git(self.repo, "rev-parse", "refs/scratch/public-main")
        self.say("read", "public main %s from %s" % (self.pub, self.public))
        if git(self.repo, "ls-remote", "--tags", self.public,
               "refs/tags/" + self.tag):
            raise Refusal("the public repository already has tag %s" % self.tag)
        self.say("ok", "tag %s is not on the public repository" % self.tag)

    def rehearse(self, changelog, declared):
        """The nightly's version and notes: the next cut of the version trunk
        declares, as <declared>-nightly, with the CHANGELOG's newest section
        as the notes (see THE NIGHTLY above)."""
        if not declared:
            raise Refusal("helm/__init__.py at %s declares no __version__; a "
                          "nightly rehearses the next cut of the version "
                          "trunk declares" % self.trunk[:12])
        heading, self.notes = newest_section(changelog)
        if not self.notes:
            raise Refusal("CHANGELOG.md at %s has no section with text under "
                          "it; the release notes are a CHANGELOG section"
                          % self.trunk[:12])
        self.version = declared + NIGHTLY_SUFFIX
        self.tag = "v" + self.version
        self.say("read", "NIGHTLY: rehearsing the next cut as %s (trunk declares "
                 "%s); notes: the newest CHANGELOG section, '## %s'"
                 % (self.version, declared, heading))

    def read_private(self):
        """The KEEP list and the private patterns, from outside the tree. A
        file that cannot be used is not a refusal here: the reports run
        without it and say so, and the seat-attribution gate then refuses."""
        a = self.a
        self.keep_path, self.patterns_path = (
            os.path.abspath(os.path.expanduser(given or os.path.join(PRIVATE, name)))
            for given, name in ((a.keep_file, KEEP_FILE),
                                (a.audit_file, PATTERNS_FILE)))
        self.keep, self.keep_why = load_private(
            self.keep_path, "the KEEP list", parse_keep, self.checkouts)
        self.patterns, self.patterns_why = load_private(
            self.patterns_path, "the private patterns", parse_patterns,
            self.checkouts)
        self.say(*(("UNUSABLE", self.keep_why) if self.keep_why else (
            "read", "the KEEP list %s: %d entries" % (self.keep_path, len(self.keep)))))
        self.say(*(("UNUSABLE", self.patterns_why) if self.patterns_why else (
            "read", "the private patterns %s: %d patterns, %d literals" % (
                self.patterns_path, len(PATTERN_KEYS),
                len(self.patterns["literals"])))))

    def checkout_roots(self):
        """Every linked working tree of the source's exact repository: what
        `inside the checkout` means for private inputs and the work directory."""
        p = subprocess.run(("git", "-C", self.source, "worktree", "list",
                            "--porcelain", "-z"), capture_output=True,
                           env=_env(), timeout=600)
        if p.returncode != 0:
            raise Refusal("git worktree list: %s"
                          % tail(os.fsdecode(p.stderr or p.stdout), 400))
        if p.stdout and not p.stdout.endswith(b"\0"):
            raise Refusal("git worktree list returned truncated porcelain")
        roots = [os.fsdecode(field[9:]) for field in p.stdout.split(b"\0")
                 if field.startswith(b"worktree ")]
        if not roots:
            raise Refusal("git worktree list named no checkout for %s" % self.source)
        return list(dict.fromkeys(roots))

    def work_directory(self):
        """-> (work directory, mount point, filesystem type). --work as given,
        else a new directory for this run under ~/.helm/releases/<version>.
        Both are refused in memory (the backup must survive a reboot) and
        inside the checkout (the reports name private things)."""
        if (not self.version or self.version in (".", "..")
                or os.path.basename(self.version) != self.version):
            raise Refusal("version %s is not one safe path component for the "
                          "default work directory" % self.version)
        if self.a.work:
            work = os.path.abspath(self.a.work)
        else:
            releases = os.path.abspath(os.path.expanduser(RELEASES))
            work = os.path.join(releases, NIGHTLY_DIR if self.a.nightly
                                else self.version)
            if not inside(work, releases) or inside(releases, work):
                raise Refusal("the default work directory %s escapes the releases "
                              "directory %s" % (work, releases))
        for root in self.checkouts:
            if inside(work, root):
                raise Refusal("the work directory %s is inside the checkout %s; "
                              "it holds the reports for the owner, which name "
                              "private things, so pass a --work outside it"
                              % (work, root))
        point, fstype = on_disk(work)

        def owner_only(path):
            try:
                os.chmod(path, 0o700)
                st = os.stat(path)
                mode = stat.S_IMODE(st.st_mode)
            except OSError as e:
                raise Refusal("the work directory %s cannot be secured: %s"
                              % (path, e.strerror or e))
            if mode != 0o700:
                raise Refusal("the work directory %s is %s after chmod 700"
                              % (path, stat.filemode(st.st_mode)))

        if self.a.work:
            if os.path.lexists(work) and not os.path.isdir(work):
                raise Refusal("the work directory %s is not a directory" % work)
            try:
                os.makedirs(work, mode=0o700, exist_ok=True)
                entries = os.listdir(work)
            except OSError as e:
                raise Refusal("the work directory %s cannot be prepared: %s"
                              % (work, e.strerror or e))
            if entries:
                raise Refusal("the work directory %s is not empty" % work)
            owner_only(work)
            return work, point, fstype
        try:
            os.makedirs(work, mode=0o700, exist_ok=True)
            if self.a.nightly:
                self.prune_nightly(work)
            run = time.strftime("%Y%m%dT%H%M%SZ-", time.gmtime()) + (
                "publish-" if self.a.publish else
                "nightly-" if self.a.nightly else "dry-run-")
            work = tempfile.mkdtemp(prefix=run, dir=work)
        except OSError as e:
            raise Refusal("the work directory %s cannot be prepared: %s"
                          % (work, e.strerror or e))
        owner_only(work)
        return work, point, fstype

    def prune_nightly(self, parent):
        """Keep the newest NIGHTLY_KEEP - 1 nightly runs in `parent`, so the
        run about to start makes NIGHTLY_KEEP. Each holds a clone of its
        candidate and a backup of the public repository, and the nightly
        makes one a night. Only a real directory named like a nightly run
        (NIGHTLY_RUN) is removed; a link, a file or any other name is left.
        An error removing one is the caller's refusal."""
        runs = sorted(n for n in os.listdir(parent) if NIGHTLY_RUN.match(n)
                      and not os.path.islink(os.path.join(parent, n))
                      and os.path.isdir(os.path.join(parent, n)))
        drop = runs[:max(0, len(runs) - (NIGHTLY_KEEP - 1))]
        for name in drop:
            shutil.rmtree(os.path.join(parent, name))
        # Tagged `pruned`, never `work`: the `work` line names this run's
        # directory, and a reader takes the first one.
        if drop:
            self.say("pruned", "removed %d older nightly run%s from %s; the "
                     "newest %d stay" % (len(drop), "" if len(drop) == 1 else
                                         "s", parent, NIGHTLY_KEEP - 1))

    # ---- 2. backup -------------------------------------------------------

    def backup(self):
        self.step = "backup"
        self.backup_dir = os.path.join(self.work, "backup-public.git")
        git(self.work, "clone", "-q", "--mirror", "--template=", self.public,
            self.backup_dir)
        git(self.backup_dir, "fsck", "--no-progress")
        main = git(self.backup_dir, "rev-parse", "refs/heads/main")
        if main != self.pub:
            raise Refusal("the backup's main %s is not the public main %s just "
                          "read; the public repository moved" % (main, self.pub))
        self.say("ok", "backup %s (fsck clean, main %s)" % (self.backup_dir, main[:12]))

    # ---- 3. build --------------------------------------------------------

    def ident_env(self):
        date = git(self.repo, "log", "-1", "--format=%cd", "--date=raw", self.trunk)
        return _env(GIT_AUTHOR_NAME=self.name, GIT_AUTHOR_EMAIL=self.email,
                    GIT_COMMITTER_NAME=self.name, GIT_COMMITTER_EMAIL=self.email,
                    GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date)

    def build(self):
        self.step = "build"
        entries = tree_map(self.repo, self.trunk)
        dead = [e for e in self.omit if not any(omitted(p, [e]) for p in entries)]
        if dead:
            raise Refusal("omit entries that match nothing at %s: %s (a stale "
                          "list must not pass silently)"
                          % (self.trunk[:12], self.hide(", ".join(dead))))
        if self.a.candidate:
            self.cand = git(self.source, "rev-parse", "--verify",
                            self.a.candidate + "^{commit}")
            self.say("given", "candidate %s (%s)" % (self.cand, self.a.candidate))
        else:
            keep = [(p, meta) for p, meta in entries.items()
                    if not omitted(p, self.omit + [OMIT])]
            with tempfile.TemporaryDirectory() as d:
                env = _env(GIT_INDEX_FILE=os.path.join(d, "index"))
                # Through git(): what update-index says on stderr names index
                # paths, so it reaches the terminal only inside a refusal.
                git(self.repo, "update-index", "-z", "--index-info", env=env,
                    input="".join("%s\t%s\0" % (m, p) for p, m in keep))
                tree = git(self.repo, "write-tree", env=env)
            msg = os.path.join(self.work, "message.txt")
            fd = os.open(msg, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(self.message)
            self.cand = git(self.repo, "commit-tree", "--no-gpg-sign", tree,
                            "-p", self.pub, "-F", msg, env=self.ident_env())
            self.say("built", "candidate %s: trunk tree minus %d paths, on public main"
                % (self.cand, len(entries) - len(keep)))
        git(self.repo, "update-ref", "refs/heads/main", self.cand)
        git(self.repo, "symbolic-ref", "HEAD", "refs/heads/main")
        git(self.repo, "-c", "tag.gpgSign=false", "tag", "-a", "-m",
            "helm " + self.version, self.tag, self.cand, env=self.ident_env())
        self.notes_file = os.path.join(self.work, "notes-%s.md" % self.tag)
        fd = os.open(self.notes_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(self.notes + "\n")

    # ---- 4. gates --------------------------------------------------------

    def gate(self):
        self.step = "gate/candidate"
        r, c, pub = self.repo, self.cand, self.pub
        if git(r, "merge-base", "--is-ancestor", pub, c, check=False).returncode:
            raise Refusal("the public main %s is not an ancestor of the candidate "
                          "%s, so pushing it would not fast-forward the public "
                          "main (it moved, or the candidate was cut from another "
                          "base); build again from the current public main"
                          % (pub[:12], c[:12]))
        n = git(r, "rev-list", "--count", "%s..%s" % (pub, c))
        if n != "1":
            raise Refusal("the candidate is %s commits past the public main, "
                          "not one" % n)
        self.say("ok", "fast-forward: one commit on the public main %s" % pub[:12])
        who = git(r, "log", "-1", "--format=%an <%ae>|%cn <%ce>", c)
        want = "%s <%s>" % (self.name, self.email)
        if who != want + "|" + want:
            raise Refusal("the candidate is authored/committed as %s, not %s"
                          % (who, want))
        if git(r, "rev-parse", c + "^{tree}") == git(r, "rev-parse", pub + "^{tree}"):
            raise Refusal("nothing to release: the candidate's tree is the "
                          "public main's tree")
        self.say("ok", "identity %s; the tree changes" % want)
        self.step = "gate/tree"
        self.tree_is_trunk_minus_omissions()
        self.step = "gate/reports"
        self.owner_reads()
        self.step = "gate/attribution"
        self.attribution_gate()
        self.step = "gate/sweep"
        tag_msg = git(r, "cat-file", "-p", "refs/tags/" + self.tag)
        tag_msg = tag_msg.split("\n\n", 1)[1] if "\n\n" in tag_msg else ""
        self.sweep_text({"the tag message": tag_msg, "the release notes": self.notes})
        self.step = "gate/battery"
        clone = self.battery(self.repo, "candidate")
        self.step = "gate/smoke"
        self.smoke(clone)
        self.step = "gate/gitleaks"
        self.gitleaks(clone)
        self.step = "gate/summary"
        stat = git(r, "diff", "--stat", pub, c).splitlines()
        self.say("info", "against the public main: %s" % (stat[-1].strip() if stat else "no change"))

    def owner_reads(self):
        """Write the reports the owner reads before a public release under
        <work>/reports/, never into the repository: the census of what no
        gate refuses (world_audit.txt), the seat-attribution lines
        (seat_attribution.txt, arm_attr.txt) and the content counts
        (content.txt). They carry private values, so the directory is 0700
        and each file 0600, and a private needle in them is written by
        number; the terminal gets counts and paths only."""
        tree = tree_map(self.repo, self.cand)
        sha = {p: m.split()[2] for p, m in tree.items() if m.split()[1] == "blob"}
        data = blobs(self.repo, sorted(set(sha.values())))
        files = {p: data[s] for p, s in sha.items()}
        world, totals = census(files, self.patterns, self.patterns_why, self.cand)
        reports = {"world_audit.txt": world,
                   "content.txt": content(files, self.patterns, self.patterns_why,
                                          self.needles, self.needles_path, self.cand)}
        if self.patterns:
            scanned, named, self.attr = attribution(files, self.patterns["seat"])
            reports.update(self.attribution_reports(scanned, named))
        else:
            self.attr = None
            reports["seat_attribution.txt"] = reports["arm_attr.txt"] = (
                "%s\n\nNOT RUN: %s\n" % (OWNER_READ, self.patterns_why))
        self.reports = os.path.join(self.work, "reports")
        os.makedirs(self.reports, mode=0o700, exist_ok=True)
        os.chmod(self.reports, 0o700)
        for name, text in sorted(reports.items()):
            fd = os.open(os.path.join(self.reports, name),
                         os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(self.hide(text))
        self.say("report", "census: %s%s" % (
            ", ".join("%s %d" % kv for kv in totals.items()),
            "" if self.patterns else "; the private classes NOT RUN"))
        self.say("OWNER", "read before --publish (private values, never commit them): "
            "%s/{%s}" % (self.reports, ",".join(sorted(reports))))

    def attribution_reports(self, scanned, named):
        """{seat_attribution.txt, arm_attr.txt} for the lines in `self.attr`."""
        found = self.attr
        keep = self.keep or {}
        per_file = lambda rows: collections.Counter(p for p, _n, _t in rows)
        seen = {(p, t) for p, _n, t in found}
        rows = [
            OWNER_READ, "",
            "seat attribution in helm/ production code at candidate %s" % self.cand,
            "production files scanned=%d" % scanned,
            "A (seat token on the line):            lines=%-5d files=%d"
            % (len(named), len(per_file(named))),
            "B (seat token + attribution verb):     lines=%-5d files=%d"
            % (len(found), len(per_file(found))),
            "top files by B: " + _top(per_file(found), 12),
            "", "B lines (KEEP with the list's reason, else OFFENDING):"]
        rows += ["  %s:%d: %s  %s" % (p, n, "KEEP (%s)" % keep[(p, t)]
                                         if (p, t) in keep else "OFFENDING",
                                         self.hide(t)[:150])
                    for p, n, t in found]
        bad = [(p, n, t) for p, n, t in found if (p, t) not in keep]
        gate = [OWNER_READ, "",
                "candidate %s: definition-B lines=%d  (KEEP-listed %d, offending %d)"
                % (self.cand, len(found), len(found) - len(bad), len(bad)),
                "KEEP list: " + (self.keep_path if self.keep_why is None
                                 else "NOT READ: " + self.keep_why)]
        gate += ["  OFFENDING %s:%d: %s" % (p, n, self.hide(t)[:150])
                 for p, n, t in bad]
        gate += ["  STALE KEEP entry (no longer present): %s: %s" % k
                 for k in sorted(keep) if k not in seen]
        return {"seat_attribution.txt": "\n".join(rows) + "\n",
                "arm_attr.txt": "\n".join(gate) + "\n"}

    def attribution_gate(self):
        """Refuse a seat-attribution line in helm/ that the KEEP list does not
        name, and a KEEP entry that names no line. Without both private files
        the gate does not run, and a gate that did not run fails."""
        unusable = [w for w in (self.keep_why, self.patterns_why) if w]
        if unusable:
            raise Refusal(
                "the seat-attribution gate did not run: %s. A public release "
                "does not pass without it. Both files name private things, so "
                "they live outside the tree, readable by their owner only "
                "(chmod 600); --keep-file and --audit-file name other paths"
                % "; ".join(unusable))
        seen = {(p, t) for p, _n, t in self.attr}
        bad = ["%s:%d" % (p, n) for p, n, t in self.attr if (p, t) not in self.keep]
        stale = [k for k in self.keep if k not in seen]
        why = []
        if bad:
            why.append("%d line(s) in helm/ production code name a seat as the "
                       "one who found or reviewed something and are not on the "
                       "KEEP list %s: %s" % (len(bad), self.keep_path,
                                              ", ".join(bad[:8])))
        if stale:
            why.append("%d KEEP entr%s not match a line at the candidate, so the "
                       "list is stale and must not pass silently (in %s)"
                       % (len(stale), "y does" if len(stale) == 1 else "ies do",
                          ", ".join(sorted({f for f, _t in stale}))))
        if why:
            raise Refusal(self.hide("seat attribution: %s. %s/arm_attr.txt quotes "
                                    "them" % ("; ".join(why), self.reports)))
        self.say("ok", "seat attribution: %d line%s in helm/, on the KEEP list (%s)"
            % (len(self.attr), "" if len(self.attr) == 1 else "s", self.keep_path))

    def hide(self, text):
        """`text` with every private needle replaced by its number. A hit is
        reported as needle #N and never as the value, and that holds when the
        value is part of a PATH being named: the path is printed redacted."""
        return redact(text)

    def tree_is_trunk_minus_omissions(self):
        trunk = tree_map(self.repo, self.trunk)
        cand = tree_map(self.repo, self.cand)
        kept = {p: m for p, m in trunk.items() if not omitted(p, self.omit + [OMIT])}
        extra = sorted(set(cand) - set(kept))
        missing = sorted(set(kept) - set(cand))
        changed = sorted(p for p in set(kept) & set(cand) if kept[p] != cand[p])
        if extra or missing or changed:
            raise Refusal(self.hide("the candidate tree is not the trunk tree minus "
                                    "the omit list: extra %s, missing %s, changed %s"
                                    % (extra[:5], missing[:5], changed[:5])))
        self.say("ok", "tree = trunk %s minus %d omitted paths (%d paths)"
            % (self.trunk[:12], len(trunk) - len(kept), len(cand)))

    def sweep_text(self, texts):
        """Refuse an AI authoring line or a private needle in any of `texts`."""
        for where, text in texts.items():
            for n, line in self.trailers.offending(text):
                raise Refusal("%s carries an AI authoring line (line %d): %s"
                              % (where, n, self.hide(line.strip())))
            for i, needle in enumerate(self.needles, 1):
                if needle in text:
                    raise Refusal("%s carries private needle #%d of %s"
                                  % (where, i, self.needles_path))

    def battery(self, remote, label):
        """The verify battery on a fresh clone of `remote` -> the clone."""
        if not self.needles:
            raise Refusal("no private needles loaded from %s; the needle sweeps have "
                          "nothing to look for (set HELM_PRIVATE_NEEDLES)"
                          % self.needles_path)
        clone = os.path.join(self.work, "check-" + label)
        git(self.work, "clone", "-q", "--no-local", "--template=", remote, clone)
        head = git(clone, "rev-parse", "HEAD")
        if head != self.cand:
            raise Refusal("a fresh clone of %s checks out %s, not the candidate %s"
                          % (remote, head, self.cand))
        if label == "public" and git(clone, "rev-parse", self.tag + "^{commit}") != self.cand:
            raise Refusal("the public tag %s does not point at %s" % (self.tag, self.cand))
        log = git(clone, "log", "--format=%H%x00%B%x01")
        msgs = {}
        for entry in filter(str.strip, log.split("\x01")):
            sha, _, body = entry.lstrip("\n").partition("\x00")
            msgs["commit %s's message" % sha[:12]] = body
        self.sweep_text(msgs)
        self.say("ok", "%s: messages of %d commits carry no authoring line or needle"
            % (label, len(msgs)))
        tree = tree_map(clone, "HEAD")
        never = self.nevertrack.never_track_set()
        for path in sorted(tree):
            why = next((w for rx, w in FORBIDDEN_NAMES if rx.search(path)), None)
            why = why or next(("under the never-track path %s" % p
                               for p in never if path.startswith(p)), None)
            why = why or next(("its path carries private needle #%d of %s"
                               % (i, self.needles_path)
                               for i, n in enumerate(self.needles, 1) if n in path), None)
            why = why or ("the omit list names it" if omitted(path, self.omit) else None)
            if why:
                raise Refusal("%s: forbidden filename %s (%s)"
                              % (label, self.hide(path), why))
        shas = sorted({m.split()[2] for m in tree.values() if m.split()[1] == "blob"})
        data = blobs(clone, shas)
        for path, meta in sorted(tree.items()):
            body = data.get(meta.split()[2], b"")
            # Every blob, binary too: a needle inside a picture, a dump or an
            # archive ships exactly like one in a text file.
            for i, needle in enumerate(self.needles, 1):
                if needle.encode() in body:
                    raise Refusal("%s: %s carries private needle #%d of %s"
                                  % (label, self.hide(path), i, self.needles_path))
        self.say("ok", "%s: %d paths, no forbidden filename, no needle in any file "
            "(%d needles)" % (label, len(tree), len(self.needles)))
        bindir = os.path.join(self.work, "install-" + label)
        p = subprocess.run(("sh", "scripts/install.sh", "--prefix", bindir),
                           cwd=clone, capture_output=True, text=True, timeout=300,
                           env=_env())
        if p.returncode != 0:
            raise Refusal("%s: scripts/install.sh --prefix failed (rc %d): %s"
                          % (label, p.returncode, tail(p.stdout + p.stderr, 300)))
        self.say("ok", "%s: the installer ran from the fresh clone" % label)
        return clone

    def smoke(self, clone):
        scratch = os.path.join(self.work, "smoke-home")
        env = {k: v for k, v in _env().items() if not k.startswith("HELM_")}
        env["HELM_HOME"] = scratch
        helm = os.path.join(clone, "bin", "helm")
        p = subprocess.run((helm, "--help"), cwd=clone, env=env, text=True,
                           capture_output=True, timeout=120)
        if p.returncode != 0:
            raise Refusal("bin/helm --help failed on the fresh clone (rc %d): %s"
                          % (p.returncode, tail(p.stderr, 300)))
        p = subprocess.run((helm, "doctor"), cwd=clone, env=env, text=True,
                           capture_output=True, timeout=600)
        out = p.stdout + p.stderr
        with open(os.path.join(self.work, "doctor.txt"), "w", encoding="utf-8") as f:
            f.write(out + "\nrc=%d\n" % p.returncode)
        line = DOCTOR_LINE.search(out)
        if not line or "Traceback" in out:
            raise Refusal("bin/helm doctor did not complete on the fresh clone "
                          "(see %s/doctor.txt)" % self.work)
        self.say("ok", "smoke: --help; %s (fail rows are host facts: doctor.txt)"
            % line.group(0))

    def gitleaks(self, clone):
        """The gitleaks scan of the fresh clone. A dry run skips it when
        gitleaks is not installed; --publish and --nightly refuse, a nightly
        because it must never read green where the publish would stop."""
        exe = shutil.which(self.a.gitleaks)
        if not exe:
            if (self.a.publish or self.a.nightly) and not self.a.without_gitleaks:
                raise Refusal("%s is not on PATH; install it, or pass "
                              "--without-gitleaks to %s without that scan"
                              % (self.a.gitleaks, "publish" if self.a.publish
                                 else "run the nightly"))
            self.say("SKIP", "gitleaks: %s is not on PATH" % self.a.gitleaks)
            return
        report = os.path.join(self.work, "gitleaks.json")
        empty = os.path.join(self.work, "gitleaks-cwd")
        os.makedirs(empty, exist_ok=True)
        p = subprocess.run((exe, "dir", "--no-banner", "--redact", "--report-format",
                            "json", "--report-path", report, clone), cwd=empty,
                           capture_output=True, text=True, timeout=600, env=_env())
        try:
            with open(report, encoding="utf-8") as f:
                found = json.load(f) or []
        except (OSError, ValueError):
            raise Refusal("gitleaks wrote no readable report (rc %d): %s"
                          % (p.returncode, tail(p.stderr, 300)))
        new = sorted({"%s (%s)" % (os.path.relpath(x.get("File", ""), clone),
                                   x.get("RuleID", "?"))
                      for x in found
                      if os.path.relpath(x.get("File", ""), clone) not in GITLEAKS_REVIEWED})
        if new:
            raise Refusal("gitleaks: findings outside the reviewed set: %s"
                          % self.hide(", ".join(new)))
        self.say("ok", "gitleaks: %d findings, all in reviewed files" % len(found))

    # ---- 5. writes -------------------------------------------------------

    def writes(self):
        r, c, t = self.repo, self.cand, self.tag
        return [
            ("stage", ["git", "-C", r, "push", self.private,
                       "%s:refs/heads/release/%s" % (c, self.version)]),
            ("main", ["git", "-C", r, "push", self.public, "%s:refs/heads/main" % c]),
            ("tag", ["git", "-C", r, "push", self.public,
                     "refs/tags/%s:refs/tags/%s" % (t, t)]),
            ("release", [self.a.gh, "release", "create", t, "--repo", self.gh_repo,
                         "--verify-tag", "--title", "helm " + self.version,
                         "--notes-file", self.notes_file]),
        ]

    def public_moved(self):
        """None while the public main is still the one read, else what it is."""
        now = git(self.repo, "ls-remote", self.public, "refs/heads/main").split()
        return None if now[:1] == [self.pub] else (now[:1] or ["none"])[0]

    def publish(self, plan):
        if not shutil.which(self.a.gh):
            raise Refusal("%s is not on PATH; the GitHub release needs it" % self.a.gh)
        moved = self.public_moved()
        if moved:
            raise Refusal("the public main moved since it was read (now %s); "
                          "build again from it" % moved)
        for n, (label, argv) in enumerate(plan):
            try:
                moved = label == "main" and self.public_moved()
            except Refusal as e:
                moved = "unreadable: %s" % e
            if moved:
                self.owed(plan[n:], "the public main moved since it was read "
                          "(now %s); nothing public was written" % moved)
                return 3
            self.say("write", shlex.join(argv))
            self.begun = True
            env = (_env(**{RELEASE_MARKER: self.cand}) if argv[0] == "git"
                   else _env())
            try:
                p = subprocess.run(argv, capture_output=True, text=True,
                                   env=env, timeout=600)
            except subprocess.TimeoutExpired as e:
                self.owed(plan[n:], "%s timed out after %g s, so whether it "
                          "reached its remote is unknown; read the remote "
                          "before running it again" % (label, e.timeout))
                return 3
            if p.returncode != 0:
                self.owed(plan[n:], "%s failed (rc %d): %s" % (
                    label, p.returncode, tail(p.stderr or p.stdout, 300)))
                return 3
            self.say("done", label)
        try:
            self.battery(self.public, "public")
        except Refusal as e:
            self.say("FAILED", "published, but the public repository failed the "
                "battery: %s" % e)
            return 3
        self.say("PUBLISHED", "%s: public main %s, tag %s, GitHub release %s; backup %s"
            % (self.version, self.cand[:12], self.tag, self.gh_repo, self.backup_dir))
        return 0

    def verdict(self, green):
        """The nightly's closing line, which helm/releasenightly.py reads:
        GREEN with the trunk and the candidate it judged, or RED with the
        first step that refused. Only under --nightly."""
        if not self.a.nightly:
            return
        trunk = getattr(self, "trunk", None) or "unknown"
        self.say("NIGHTLY", "GREEN trunk=%s candidate=%s" % (trunk, self.cand)
                 if green else "RED step=%s trunk=%s" % (self.step, trunk))

    def owed(self, rest, why):
        self.say("STOPPED", why)
        self.output("writes still owed, in order:")
        for _label, argv in rest:
            self.output("  " + shlex.join(argv))


def parse(argv):
    p = argparse.ArgumentParser(
        prog="scripts/release/release.py",
        description="Publish a helm release. A dry run unless --publish.")
    p.add_argument("version", nargs="?",
                   help="the version, e.g. 0.3.2 (tag v<version>); none with "
                        "--nightly")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true",
                      help="the default: every step but the network writes, "
                           "which are printed")
    mode.add_argument("--publish", action="store_true",
                      help="perform the writes: stage, push main, push the "
                           "tag, create the GitHub release")
    mode.add_argument("--nightly", action="store_true",
                      help="the nightly job's dry run of trunk as it stands: "
                           "no version (it rehearses the next cut of the "
                           "version trunk declares, the CHANGELOG's newest "
                           "section as the notes), gitleaks required, its "
                           "default work directory under ~/.helm/releases/"
                           "nightly keeping the newest %d runs, and a "
                           "closing NIGHTLY GREEN|RED line naming the first "
                           "step that refused" % NIGHTLY_KEEP)
    p.add_argument("--source", default=ROOT,
                   help="the development checkout (default: this one)")
    p.add_argument("--trunk", default="main",
                   help="the trunk revision to release (default: main)")
    p.add_argument("--public", default="aspublic",
                   help="the public repository: a remote name of the source "
                        "or a URL (default: aspublic)")
    p.add_argument("--private", default="origin",
                   help="where release/<version> is staged: a remote name or "
                        "a URL (default: origin)")
    p.add_argument("--gh-repo", help="OWNER/NAME for gh (default: from the "
                                     "public remote's GitHub URL)")
    p.add_argument("--identity", help="'Name <email>' for the release commit "
                                      "and tag (default: the owner's GitHub "
                                      "noreply identity)")
    p.add_argument("--message-file",
                   help="the release commit's message (default: 'helm "
                        "<version>' and the CHANGELOG section's lead paragraph)")
    p.add_argument("--candidate",
                   help="publish this existing commit (e.g. a staged "
                        "release/<version>) instead of building one")
    p.add_argument("--work", help="an empty or absent directory on disk, "
                                  "outside the checkout, for the work "
                                  "repository, backup and reports (default: a "
                                  "new directory under ~/.helm/releases/"
                                  "<version>)")
    p.add_argument("--keep-file",
                   help="the KEEP list: the seat-attribution lines in helm/ "
                        "that may ship, with a reason each; outside the "
                        "checkout, mode 0600 (default: %s)"
                        % os.path.join(PRIVATE, KEEP_FILE))
    p.add_argument("--audit-file",
                   help="the private patterns: the seat, private-token and "
                        "owner-name regular expressions and literals the "
                        "gate and the reports look for; outside the checkout, "
                        "mode 0600 (default: %s)" % os.path.join(PRIVATE, PATTERNS_FILE))
    p.add_argument("--gh", default="gh", help="the gh executable (default: gh)")
    p.add_argument("--gitleaks", default="gitleaks",
                   help="the gitleaks executable (default: gitleaks)")
    p.add_argument("--without-gitleaks", action="store_true",
                   help="allow --publish or --nightly when gitleaks is not "
                        "installed")
    args = p.parse_args(argv)
    if args.nightly and args.version:
        p.error("--nightly takes no version: it rehearses the next cut of "
                "the version trunk declares")
    if not args.nightly and not args.version:
        p.error("the version is required (or --nightly)")
    if args.nightly and args.candidate:
        p.error("--nightly builds its candidate from trunk; --candidate names "
                "a staged one for a publish")
    return args


def excepthook(kind, value, tb):
    """The interpreter's last-resort printer, through the door. It runs when
    the door itself failed: printing the REFUSED line raised (stdout closed
    under it, as by a `| head` that had read enough), or an interrupt landed
    inside a handler. What it prints then is the chain of exceptions, the one
    that was being handled included, and that one is the refusal or the
    tool's error raw, a needle in it as it is."""
    sys.stderr.write(redact("".join(traceback.format_exception(kind, value, tb))))


def main(argv=None):
    args = parse(sys.argv[1:] if argv is None else argv)
    rel = Release(args)
    sys.excepthook = excepthook
    try:
        return run(rel, args)
    except Exception as e:
        # AN ERROR NO REFUSAL NAMES LEAVES BY THE SAME DOOR. A traceback
        # prints argv, paths and a tool's stderr as they are, and any of them
        # can carry a private needle. KeyboardInterrupt and SystemExit are not
        # Exceptions, so they still end the run.
        rel.say("REFUSED", "%s: %s" % (type(e).__name__, e))
        if not rel.begun:
            rel.output("nothing was pushed, tagged on a remote or released")
            rel.verdict(False)
            return 1
        rel.output("a network write had begun, so the writes may be partial: "
                   "read the public and private repositories before a rerun")
        return 3


def run(rel, args):
    if args.nightly:
        rel.say("helm", "release: NIGHTLY (a dry run of trunk as it stands, as "
                "if cut now; nothing is pushed, tagged on a remote or released)")
    else:
        mode = "PUBLISH" if args.publish else "DRY RUN"
        rel.say("helm", "release %s: %s%s" % (args.version, mode,
                                               "" if args.publish else
                                               " (nothing is pushed or "
                                               "released; --publish does that)"))
    try:
        rel.read()
        rel.backup()
        rel.build()
        rel.gate()
    except Refusal as e:
        rel.say("REFUSED", str(e))
        rel.output("nothing was pushed, tagged on a remote or released")
        rel.verdict(False)
        return 1
    plan = rel.writes()
    if not args.publish:
        rel.output("writes (DRY RUN: none performed):")
        for n, (label, argv) in enumerate(plan, 1):
            rel.output("  %d. %-8s %s" % (n, label, shlex.join(argv)))
        rel.output("  then: the battery on a fresh clone of %s" % rel.public)
        rel.say("DRY RUN", "OK: candidate %s passed every gate%s" % (
            rel.cand, "" if args.nightly else "; rerun with --publish to "
            "perform the %d writes" % len(plan)))
        rel.verdict(True)
        return 0
    try:
        return rel.publish(plan)
    except Refusal as e:
        rel.say("REFUSED", str(e))
        return 1


if __name__ == "__main__":
    sys.exit(main())
