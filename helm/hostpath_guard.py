#!/usr/bin/env python3
"""A pre-push guard: REFUSE when a push to a PUBLIC remote carries files
containing host filesystem paths — the class that bit the CLIProxyAPI fork.

THE 2026-07-29 INCIDENT. The CLIProxyAPI fork carried three literal host
paths under `/home/<account>/` inside a TEST THAT ASSERTED THEY WERE ABSENT.
A forbidden-list contains what it forbids, so THE GUARD BECAME THE LEAK.

CURE: PATTERN, not literal. A regex over the host-path SHAPE removes the
literal AND generalises — `/home/<user>/path` or `/Users/<user>/path`.

PUBLIC-ONLY: a private-repo push to a named collaborator has a different bar.
The visibility answer has three values (`_visibility`): PRIVATE skips the
scan, PUBLIC scans, and UNKNOWN scans too. UNKNOWN fails SAFE: treat as public
and REFUSE — a refused push costs a retry, a leaked path on a public remote may
already be cloned or indexed. But UNKNOWN is not PUBLIC, and the refusal must
not say PUBLIC. MEASURED: a push of main to a PRIVATE repo was refused with
"must not be pushed to a PUBLIC remote", `gh repo view` read isPrivate:true
409 ms later, and the retried push went through. A one-bool answer cannot
tell a gh failure (non-zero exit, timeout, no gh, output that is not JSON)
from a public repository. So a failed probe is asked once more before the
answer is UNKNOWN, and the UNKNOWN answer carries the failure in fixed words,
which the refusal prints: "visibility UNKNOWN (<why>), treated as public".

THE SCAN READS WHAT THE PUSH ADDS TO THE REMOTE. A diff of `old..new` sees
only the files this push changes, and a host path committed in an earlier,
unscanned push rides along in every later one. A scan of EVERY blob in the
pushed tree covers that, but it also refuses content the remote already has.
MEASURED on the dregg fork, a fork of a public upstream: a whole-tree scan
REFUSED a push of two commits that added no host path, on 322 `/Users/<user>/`
matches in upstream files the remote already held (for example
.docs-history-noclaude/DISTRIBUTED-SERVO.md), after about 17 minutes.
Refusing content that is already public protects nothing, and that refusal
blocks every later push of the fork.

Now the scan reads the blobs of `git rev-list --objects <new> --not
<advertised>` (the exclusions go on stdin): every object reachable from the pushed sha that is not reachable
from a branch or tag the DESTINATION advertises. The advertisement comes from
`git ls-remote --heads --tags <url>` on the URL git is pushing to (pre-push
$2), and git's connectivity rule means a repository that advertises a commit
holds everything reachable from it, whatever this repo's config says. Only the
advertised shas this repo has are excluded, because rev-list can walk only
those. A new branch whose base the destination already has reads only its new
objects.

THE BASE NEEDS PROOF THAT THE PUSH GOES WHERE LS-REMOTE LOOKED. ls-remote
runs upload-pack on the URL; the push runs receive-pack, and a configured or
command-line receive-pack can write the pack into another repository. MEASURED:
with `remote.origin.receivepack` writing into an empty repository B while the
URL named A, the scan excluded A's objects, the push exited 0, and B received
the old host-path blob unread. So the base is used only when all of these
hold, and otherwise the scan reads everything ("push destination not proven to
be the queried one"): git passed the URL; the remote config reads; the argv of
the `git push` this hook runs under reads from /proc and carries no
--receive-pack, --exec or receive-pack/pushurl/url/insteadOf override; no
remote.<name>.receivepack or .vcs is configured; no pushurl differs from url;
no pushInsteadOf applies to the remote; no insteadOf rewrites the push URL
again; and the URL is not a remote-helper `::` URL. Fail safe to the full scan,
never to trust. The query itself passes the default upload-pack explicitly.

NO TRACKING REF IS TRUSTED. A remote-tracking ref records what some FETCH URL
held at the last fetch, and a pushurl, a pushInsteadOf or a `git remote
set-url` sends the push elsewhere while the ref stays. MEASURED: a host-path
blob was published to origin's old URL, `git remote set-url origin` named a
fresh empty repository and kept refs/remotes/origin/main, and a clean commit
narrowed by those refs read 1 blob and sent the old blob to the empty
repository unread. The ref line's remote sha is not a base on its own either:
it counts only when the advertisement carries it.

FAIL SAFE: when the destination cannot be described, the scan reads every blob
reachable from the pushed sha. That is the whole pushed tree plus its history,
because a destination we cannot describe can lack any of it. The cases: git
gave no URL (a hand-run, or a wrapper older than v2); ls-remote fails, times
out after _LS_REMOTE_TIMEOUT seconds, or prints a line that is not a ref; the
advertisement is empty (a first push); none of the advertised shas is here;
or rev-list refuses the base.

A PUSH URL CAN CARRY A CREDENTIAL in any spelling (`https://<user>:<token>@<host>/
repo?token=...`, an apostrophe or a percent-encoded `@` in the password), and
git echoes the URL in its errors. No redaction can know every spelling, so no
diagnostic contains the push URL or git's stderr in any form. It names the push
by the remote NAME git passed only when `remote.<name>.url` is configured, and
as "a URL remote" otherwise, so a URL or a filesystem path never prints
(`_remote_label`); and it gives a fixed reason. A failed ls-remote is sorted
into `ls-remote timed out`, `ls-remote exited N`, `ls-remote returned no
parseable refs` or `not reachable` (`_ls_remote_failure`); its stderr is read
for that and never printed. No reason carries raw output of cat-file,
rev-list or ls-remote, or raw pre-push input: a scan failure is a fixed reason
and an exit code (`_scan_failure`), and bad input is "malformed pre-push
input" with a line number and a field count. A size git prints is checked for
digits before it is parsed. A HIT LINE is the one exception, and it is bounded:
the offending file's repository path from rev-list, clipped to _HIT_CLIP
bytes, and the `/home/<user>/` shape the pattern matched, never another byte
of the blob. TWO DOORS close the class: `scan_push` turns any
exception into a fresh `_ScanError` in fixed words, raised with no chained
context, and `main` turns anything that escapes into "REFUSED: internal error
(<type name>)" with no message, repr or traceback.

THE BOUND: the advertisement is read on a second connection just before git
uploads. A ref the destination drops in between still counts as held; its
objects were on the destination when it advertised them.

The blobs are read with `git cat-file --batch`, in chunks of at most _CHUNK
bytes, never one `git cat-file blob` spawn per file. MEASURED on a clone of the
fork: the tip tree's 12,970 blobs take 251 s one spawn per file and 8 s through
`--batch`. The full read of the fork's history (56,239 blobs, 3.9 GB) takes
101 s. Two new clean commits read 2 blobs, and the whole scan takes 1.9 s, of
which the ls-remote of the fork's 28 refs on GitHub is about 1 s.

Stdlib-only, no helm imports — the hook executes this as a plain script.
"""
import json
import os
import re
import subprocess
import sys
import time

# `/home/<user>/` or `/Users/<user>/` where <user> starts with a letter.
# Excludes bare /home/ and /home without a trailing path segment.
_HOSTPATH_RE = re.compile(r"/(home|Users)/[a-zA-Z][a-zA-Z0-9._-]*/")
# The same pattern over raw blob bytes. The pattern is ASCII-only, and UTF-8
# never puts an ASCII byte inside a multibyte sequence, so the bytes match
# exactly where the text pattern matched on the replace-decoded blob, without
# decoding every blob.
_HOSTPATH_BYTES_RE = re.compile(_HOSTPATH_RE.pattern.encode("ascii"))

_HIT_CLIP = 72
_NAMED_N = 3
_ZERO = "0000000000000000000000000000000000000000"
_CHUNK = 32 << 20
_LS_REMOTE_TIMEOUT = 30
# The three visibility answers (`_visibility`), the gh budget for one probe,
# and the pause before the one retry of a failed probe.
PRIVATE, PUBLIC, UNKNOWN = "PRIVATE", "PUBLIC", "UNKNOWN"
_GH_TIMEOUT = 30
_GH_RETRY_PAUSE = 1.0
# The note level that carries the visibility answer to the refusal.
_DESTINATION = "destination"
_ARGV_DEPTH = 16
_NOT_PROVEN = "push destination not proven to be the queried one (%s)"
# A -c or --config-env key in the push argv that can move the destination.
_OVERRIDE_RE = re.compile(r"(?i)(remote\..+\.(receivepack|pushurl|url|vcs)"
                          r"|url\..+\.(pushinsteadof|insteadof))\Z")


class _ScanError(RuntimeError):
    """A scan failure in fixed words: `reason` is a literal and `rc` an exit
    code or None. It never carries subprocess output."""

    def __init__(self, reason, rc=None):
        super().__init__(reason)
        self.reason, self.rc = reason, rc
_SHA_RE = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?\Z")


def _git(root, *args, binary=False, feed=None, timeout=60):
    p = subprocess.run(("git",) + args, capture_output=True, cwd=root,
                       timeout=timeout, text=not binary, input=feed)
    return p.returncode, p.stdout, p.stderr


# Sorts a failed ls-remote into "not reachable"; nothing else reads them.
_UNREACHABLE_MARKERS = (
    "could not resolve", "connection refused", "connection timed out",
    "network is unreachable", "failed to connect", "unable to access",
    "could not read from remote repository", "not found",
    "does not appear to be a git repository", "authentication failed",
    "permission denied", "could not read username", "could not read password",
)


def _remote_config(root):
    """-> [(key, value)] for every remote.* and url.* entry git reads here,
    with the command line's -c entries; None when the config cannot be read.
    git lowercases section and variable names and keeps the subsection."""
    rc, out, _ = _git(root, "config", "-z", "--get-regexp",
                      r"^(remote|url)\.")
    if rc not in (0, 1):
        return None
    return [entry.partition("\n")[::2] for entry in out.split("\0") if entry]


def _remote_label(config, name):
    """THE ONE DOOR for the destination: a diagnostic names the push by this
    and by nothing else. A NAME is shown only when `remote.<name>.url` is
    configured; anything else, a URL, an scp <user>@<host>: form, a filesystem
    path or an unreadable config, is "a URL remote", never any byte of it."""
    if (config and name and not any(c in name for c in ":@")
            and "remote.%s.url" % name in {k for k, _v in config}):
        return repr(name)
    return "a URL remote"


def _push_argv(pid=None, proc="/proc"):
    """-> the argv of the `git push` whose pre-push hook runs this scan, read
    up the parent chain from /proc, or None when it cannot be read. The hook
    is a child of that `git push`, and this scanner a child of the hook."""
    pid = os.getppid() if pid is None else pid
    for _ in range(_ARGV_DEPTH):
        try:
            with open(os.path.join(proc, str(pid), "cmdline"), "rb") as f:
                argv = [a.decode(errors="replace")
                        for a in f.read().split(b"\0")]
            with open(os.path.join(proc, str(pid), "status")) as f:
                ppid = [int(ln.split()[1]) for ln in f
                        if ln.startswith("PPid:")][0]
        except (OSError, ValueError, IndexError):
            return None
        argv = argv[:-1] if argv[-1:] == [""] else argv
        prog = os.path.basename(argv[0]) if argv else ""
        if prog == "git-push" or (prog == "git" and "push" in argv[1:]):
            return argv
        if ppid <= 1:
            return None
        pid = ppid
    return None


def _argv_moves_the_push(argv):
    """-> the fixed cause when the push argv can send the pack somewhere
    other than the URL, else None. Long options may be abbreviated."""
    for i, arg in enumerate(argv):
        name = arg.split("=", 1)[0]
        if name.startswith("--") and len(name) > 2 and (
                "--receive-pack".startswith(name) and len(name) >= 5
                or "--exec".startswith(name)):
            return "a receive-pack in the push argv"
        if arg == "-c" and i + 1 < len(argv):
            key = argv[i + 1].split("=", 1)[0]
        elif arg.startswith("--config-env="):
            key = arg[len("--config-env="):].split("=", 1)[0]
        else:
            continue
        if _OVERRIDE_RE.match(key):
            return "a config override in the push argv"
    return None


def _unproven(config, name, url, argv):
    """-> None when the push provably goes where ls-remote on `url` looks,
    else the fixed cause. An answer that cannot be known is a cause."""
    if argv is None:
        return "push argv unreadable"
    moved = _argv_moves_the_push(argv)
    if moved:
        return moved
    if "::" in url:
        return "a remote helper"

    def get(var):
        return [v for k, v in config if k == "remote.%s.%s" % (name, var)]

    urls, pushurls = get("url"), get("pushurl")
    if get("receivepack"):
        return "a custom receive-pack"
    if get("vcs"):
        return "a remote helper"
    if any(not urls or p != urls[0] for p in pushurls):
        return "a pushurl that differs from url"
    rewrites = [(k.rsplit(".", 1)[1], v) for k, v in config
                if k.startswith("url.") and v]
    pushed = urls + pushurls + [name, url]
    if any(var == "pushinsteadof" and u.startswith(v)
           for var, v in rewrites for u in pushed):
        return "a pushInsteadOf rewrite"
    if any(var == "insteadof" and url.startswith(v) for var, v in rewrites):
        return "an insteadOf rewrite of the push URL"
    return None


def _ls_remote_failure(rc, err):
    """-> the fixed reason for an ls-remote that exited `rc`. `err` is only
    READ, to tell "not reachable" from any other exit; it is never shown."""
    low = err.lower()
    if any(m in low for m in _UNREACHABLE_MARKERS):
        return "not reachable"
    return "ls-remote exited %d" % rc


def _scan_failure(exc):
    """A failed scan in fixed words, never str(exc): a TimeoutExpired or an
    OSError can carry the command line, and a command line the push URL."""
    if isinstance(exc, _ScanError):
        if exc.rc is None:
            return exc.reason
        return "%s (exited %d)" % (exc.reason, exc.rc)
    if isinstance(exc, subprocess.TimeoutExpired):
        return "git %s timed out" % exc.cmd[1]
    if isinstance(exc, OSError):
        return "cannot run git (%s)" % type(exc).__name__
    return "internal error (%s)" % type(exc).__name__


def _remote_url(root, remote):
    rc, out, _ = _git(root, "remote", "get-url", remote)
    return out.strip() if rc == 0 and out.strip() else None


def _visibility(remote_url):
    """-> (PRIVATE | PUBLIC | UNKNOWN, why). `why` is None unless UNKNOWN.

    Only PRIVATE skips the scan; the caller treats UNKNOWN as public. A URL
    that is not on GitHub, or names no owner/repo, is UNKNOWN at once, since
    asking again cannot change it. A failed gh probe is asked once more after
    _GH_RETRY_PAUSE seconds, and only two failures make UNKNOWN. `why` is
    fixed words and an exit code or exception type name, never gh's output:
    that output can carry the repository slug and whatever gh says about
    its credential."""
    if not remote_url:
        return UNKNOWN, "no remote URL"
    # Normalise: SSH git@github.com:owner/repo -> https://github.com/owner/repo
    url = re.sub(r"^git@([^:]+):", r"https://\1/", remote_url)
    parts = url.split("github.com/", 1)
    if len(parts) != 2:
        return UNKNOWN, "not a GitHub URL"
    slug = parts[-1].rstrip("/").replace(".git", "").split("/")
    if len(slug) < 2:
        return UNKNOWN, "no owner/repo in the GitHub URL"
    owner_repo = "/".join(slug[:2])
    state, first = _gh_visibility(owner_repo)
    if state != UNKNOWN:
        return state, None
    time.sleep(_GH_RETRY_PAUSE)
    state, second = _gh_visibility(owner_repo)
    if state != UNKNOWN:
        return state, None
    return UNKNOWN, ("%s, twice" % first if second == first
                     else "%s, then %s" % (first, second))


def _gh_visibility(owner_repo):
    """One `gh repo view` probe -> (PRIVATE | PUBLIC | UNKNOWN, why)."""
    try:
        p = subprocess.run(
            ("gh", "repo", "view", owner_repo, "--json", "isPrivate"),
            capture_output=True, text=True, timeout=_GH_TIMEOUT)
    except subprocess.TimeoutExpired:
        return UNKNOWN, "gh repo view timed out after %gs" % _GH_TIMEOUT
    except OSError as exc:
        return UNKNOWN, "cannot run gh (%s)" % type(exc).__name__
    if p.returncode != 0:
        return UNKNOWN, "gh repo view exited %d" % p.returncode
    try:
        data = json.loads(p.stdout)
    except (ValueError, TypeError):
        return UNKNOWN, "gh repo view printed no JSON"
    # A boolean or nothing: the old read took a missing key as private and a
    # string "false" as private, which skipped the scan on an answer gh never
    # gave.
    flag = data.get("isPrivate") if isinstance(data, dict) else None
    if not isinstance(flag, bool):
        return UNKNOWN, "gh repo view gave no isPrivate true/false"
    return (PRIVATE if flag else PUBLIC), None


def _advertised_base(root, name, url, config):
    """-> (advertised shas this repo has, why the scan reads everything).

    `name` and `url` are git's pre-push $1 and $2 (`url` None on a
    hand-run), `config` from `_remote_config`. An empty base reads every blob
    reachable from the pushed sha, and `why` says why."""
    if url is None:
        return [], ("git did not pass this push's URL (a hand-run, or a "
                    "wrapper older than v2)")
    cause = ("remote config unreadable" if config is None
             else _unproven(config, name, url, _push_argv()))
    if cause:
        return [], _NOT_PROVEN % cause
    try:
        rc, out, err = _git(root, "ls-remote", "--upload-pack=git-upload-pack",
                            "--heads", "--tags", url, feed="",
                            timeout=_LS_REMOTE_TIMEOUT)
    except subprocess.TimeoutExpired:
        return [], "ls-remote timed out"
    if rc != 0:
        return [], _ls_remote_failure(rc, err)
    advertised = []
    for ln in out.splitlines():
        sha, tab, ref = ln.partition("\t")
        if not (tab and ref and _SHA_RE.match(sha)):
            return [], "ls-remote returned no parseable refs"
        advertised.append(sha)
    if not advertised:
        return [], "the destination advertises no branch or tag (a first push)"
    rc, out, err = _git(root, "cat-file", "--batch-check=%(objectname) "
                        "%(objecttype)", feed="".join(
                            sha + "\n" for sha in dict.fromkeys(advertised)))
    if rc != 0:
        return [], ("cannot check which advertised objects are here "
                    "(exited %d)" % rc)
    have = []
    for ln in out.splitlines():
        fields = ln.split()
        if len(fields) == 2 and fields[1] != "missing":
            have.append(fields[0])
    if not have:
        return [], ("none of the %d advertised object(s) is in this repo"
                    % len(set(advertised)))
    return have, None


def _outgoing_blobs(root, new, base):
    """-> ([(sha, path, size)], failure) for every blob the push adds.

    `base` is what the remote already has. When it is empty, or rev-list
    cannot read it, every blob reachable from `new` is read, and `failure`
    says why rev-list refused the base."""
    out, failure = None, None
    if base:
        # On stdin, not argv: a destination can advertise more refs than an
        # argv holds.
        rc, out, err = _git(root, "rev-list", "--objects", "--stdin", new,
                            binary=True, feed="".join(
                                "^%s\n" % sha for sha in base).encode("ascii"))
        if rc != 0:
            out, failure = None, ("rev-list refused the advertised base "
                                  "(exited %d)" % rc)
    if out is None:
        rc, out, err = _git(root, "rev-list", "--objects", new, binary=True)
        if rc != 0:
            raise _ScanError("object walk failed", rc)
    paths = {}
    for ln in out.split(b"\n"):
        sha, _sp, path = ln.partition(b" ")
        sha = sha.decode("ascii", errors="replace")
        if _SHA_RE.match(sha):
            paths.setdefault(sha, path.decode(errors="replace"))
    if not paths:
        return [], failure
    rc, out, err = _git(
        root, "cat-file", "--batch-check=%(objectname) %(objecttype) "
        "%(objectsize)", binary=True,
        feed="".join(sha + "\n" for sha in paths).encode("ascii"))
    if rc != 0:
        raise _ScanError("object listing failed", rc)
    blobs = []
    for ln in out.splitlines():
        fields = ln.decode("ascii", errors="replace").split()
        if (len(fields) != 3 or fields[0] not in paths
                or not fields[2].isdigit()):
            raise _ScanError("unreadable object listing")
        if fields[1] == "blob":
            blobs.append((fields[0], paths[fields[0]], int(fields[2])))
    return blobs, failure


def _read_blobs(root, blobs):
    """Yield (sha, bytes) for [(sha, path, size)], one `cat-file --batch`
    per chunk of at most _CHUNK bytes (a larger blob is a chunk alone)."""
    i = 0
    while i < len(blobs):
        j, total = i + 1, blobs[i][2]
        while j < len(blobs) and total + blobs[j][2] <= _CHUNK:
            total += blobs[j][2]
            j += 1
        chunk = blobs[i:j]
        rc, out, err = _git(
            root, "cat-file", "--batch", binary=True,
            feed="".join(b[0] + "\n" for b in chunk).encode("ascii"))
        if rc != 0:
            raise _ScanError("object read failed", rc)
        pos = 0
        for sha, _path, _size in chunk:
            nl = out.find(b"\n", pos)
            head = out[pos:nl].split() if nl >= 0 else []
            if (len(head) != 3 or head[0] != sha.encode("ascii")
                    or not head[2].isdigit()):
                raise _ScanError("unreadable object listing")
            start = nl + 1
            end = start + int(head[2])
            if end >= len(out):
                raise _ScanError("unreadable object listing")
            yield sha, out[start:end]
            pos = end + 1
        i = j


def scan_push(root, old, new, remote_name, remote_url=None):
    """-> (violations, notes) for the push to `remote_name`: THE DOOR FOR A
    CALLER. `notes` are (level, text) pairs: "note" is printed as it stands,
    and a _DESTINATION pair rides with violations only: it says what the
    visibility answer was, for the refusal's last line ("a PUBLIC remote", or
    "<remote>: visibility UNKNOWN (<why>), treated as public").
    Any exception becomes a fresh `_ScanError` in fixed words
    (`_scan_failure`), raised outside the handler so it chains no context: an
    exception's message or repr can carry raw git output or a push URL.
    SystemExit and KeyboardInterrupt pass, for `main` to answer."""
    try:
        return _scan_push(root, old, new, remote_name, remote_url)
    except (SystemExit, KeyboardInterrupt):
        raise
    except BaseException as exc:  # noqa: BLE001 — the door; see docstring
        failed = _scan_failure(exc)
    raise _ScanError(failed)


def _scan_push(root, old, new, remote_name, remote_url):
    """-> (violations, notes) for the push to `remote_name`.

    `old` is the ref line's remote sha. It is not read: a remote sha counts
    only when the destination's advertisement carries it, and then the
    advertisement has already put it in the base. `new` is the pushed sha.
    `remote_url` is the URL GIT ITSELF is pushing to (pre-push $2). When
    given it is the identity — no re-resolution by name, because (a) the
    name may not resolve at all (`git push <url>` has no remote entry), and
    (b) resolving is a second read of a fact git already handed us, and the
    two can disagree. When absent (legacy hand-run), resolve by name."""
    config = _remote_config(root)
    label = _remote_label(config, remote_name)
    url = remote_url if remote_url else _remote_url(root, remote_name)
    visibility, unknown = _visibility(url)
    if visibility == PRIVATE:
        return [], [("note", "remote %s is private — host-path scan skipped"
                     % label)]
    base, why = _advertised_base(root, remote_name, remote_url, config)
    blobs, failure = _outgoing_blobs(root, new, base)
    why = failure or why
    notes = []
    if why:
        notes.append(("note", "full scan: %s: %s — every blob reachable from "
                      "%s is read" % (label, why, new[:12])))
    if not blobs:
        return [], notes + [("note", "no files in outgoing push to scan")]
    paths = {sha: path for sha, path, _size in blobs}
    hits = []
    for sha, blob in _read_blobs(root, blobs):
        for m in _HOSTPATH_BYTES_RE.finditer(blob):
            hits.append((paths[sha], m.group().decode("ascii")))
            if len(hits) >= 20:
                break
    if not hits:
        notes.append(("note", "%d blob(s) scanned%s — no host-path matches"
                      % (len(blobs), "" if why else
                         ", the ones %s does not already have" % label)))
    else:
        notes.append((_DESTINATION, "a PUBLIC remote"
                      if visibility == PUBLIC else
                      "%s: visibility UNKNOWN (%s), treated as public"
                      % (label, unknown)))
    return hits, notes


def main(argv=None):
    """THE TOP-LEVEL DOOR: anything that escapes the pre-push entry becomes
    one fixed refusal, "internal error (<type name>)", rc 2, with no message,
    repr, traceback or chained context; the type name is the only variable
    text. KeyboardInterrupt is answered too ("interrupted", rc 130): left to
    escape, the interpreter prints a traceback that chains whatever exception
    it interrupted. SystemExit passes: nothing here raises it, and its code is
    the caller's to set."""
    try:
        return _main(argv)
    except SystemExit:
        raise
    except KeyboardInterrupt:
        failed, rc = "interrupted", 130
    except BaseException as exc:  # noqa: BLE001 — the door; see docstring
        failed, rc = "internal error (%s)" % type(exc).__name__, 2
    print("[helm hostpath] REFUSED: %s" % failed, file=sys.stderr)
    return rc


def _main(argv):
    """Pre-push entry: git feeds `local-ref local-sha remote-ref remote-sha`
    per outgoing ref on stdin — one line each, zero lines when up to date."""
    if argv is None:
        argv = sys.argv[1:]
    # v2 wrapper forwards git's own pre-push arguments: <remote> <url>.
    # v1 dropped them, and the scanner's fallback quietly visibility-checked
    # `origin` for EVERY push — measured 2026-08-06 when a push to a second
    # remote printed "remote 'origin' is private". A bare --pre-push (v1
    # wrapper still installed somewhere, or a hand-run) keeps the fallback.
    if not argv or argv[0] != "--pre-push" or len(argv) > 3:
        print("usage: hostpath_guard.py --pre-push [remote [url]]  (run by "
              "the helm pre-push guard; stdin = git pre-push ref lines)",
              file=sys.stderr)
        return 2
    argv_remote = argv[1] if len(argv) > 1 else None
    argv_url = argv[2] if len(argv) > 2 else None
    try:
        stdin_raw = sys.stdin.read()
    except OSError:
        # Unreadable stdin (a closed fd, a capturing harness) is NOT an
        # up-to-date push: we could not look, so we cannot allow. The error
        # is not printed: its text can carry whatever the reader held.
        print("[helm hostpath] REFUSED: unreadable pre-push input",
              file=sys.stderr)
        return 2
    if not stdin_raw:
        # Empty pre-push stdin IS the protocol for nothing-to-push: git
        # feeds the hook zero ref lines on an up-to-date push. Not a scan
        # failure — allow. Malformed NON-empty input still refuses below,
        # and that includes whitespace-only bytes: git never emits them,
        # so they mean a broken feeder, not an up-to-date push.
        print("[helm hostpath] nothing to push — empty pre-push stdin, "
              "no outgoing refs to scan", file=sys.stderr)
        return 0
    # splitlines() drops only the ordinary terminal newline. Interior blank
    # or whitespace-only records are NOT filtered: git emits one four-field
    # record per ref and never a blank line, so a blank one means a broken
    # feeder — and filtering it would let a malformed line ride along with
    # valid ones, which is the fail-open the whole chain refuses.
    lines = stdin_raw.splitlines()
    if not any(ln.strip() for ln in lines):
        print("[helm hostpath] REFUSED: non-empty pre-push stdin carries "
              "no ref lines (whitespace only)", file=sys.stderr)
        return 2
    to_scan = []
    deletions = 0
    for n, ln in enumerate(lines, 1):
        fields = ln.split()
        # BOTH sha fields are validated. The scan takes its base from the
        # destination's advertisement, not from this field, but malformed
        # protocol input means a broken feeder, and nothing past it is read.
        bad = ("%d field(s), not 4" % len(fields) if len(fields) != 4
               else "the local sha is not a sha"
               if not _SHA_RE.match(fields[1])
               else "the remote sha is not a sha"
               if not _SHA_RE.match(fields[3]) else None)
        if bad:
            print("[helm hostpath] REFUSED: malformed pre-push input — line "
                  "%d: %s" % (n, bad), file=sys.stderr)
            return 2
        local_sha, remote_sha = fields[1], fields[3]
        if set(local_sha) == {"0"}:
            deletions += 1  # ref deletion pushes no content
            continue
        if local_sha not in [s for s, _ in to_scan]:
            to_scan.append((local_sha, remote_sha))
    if deletions:
        print("[helm hostpath] %d ref deletion(s) — no outgoing content"
              % deletions, file=sys.stderr)
    if not to_scan:
        return 0
    root = os.getcwd()
    # Identity precedence: git's own argv (the push's REAL destination) wins;
    # the env override and the origin default exist only for invocations that
    # have no argv to trust (hand-runs, a v1 wrapper). An env var must not
    # outrank what git measured about THIS push.
    remote = argv_remote or os.environ.get("HELM_HOSTPATH_REMOTE", "origin")
    hits, notes = [], []
    try:
        for local_sha, remote_sha in to_scan:
            h, n = scan_push(root, remote_sha, local_sha, remote,
                             remote_url=argv_url)
            hits.extend(h)
            notes.extend(n)
    except (RuntimeError, subprocess.TimeoutExpired, OSError) as exc:
        print("[helm hostpath] REFUSED: push scan failed — %s"
              % _scan_failure(exc),
              file=sys.stderr)
        return 2
    destinations = []
    for level, msg in notes:
        if level != _DESTINATION:
            print("[helm hostpath] %s" % msg, file=sys.stderr)
        elif msg not in destinations:
            destinations.append(msg)
    if not hits:
        return 0
    n = len(hits)
    named = hits[:_NAMED_N]
    more = ""
    if n > _NAMED_N:
        more = " — +%d more match%s" % (n - _NAMED_N,
                                        "es" if n - _NAMED_N != 1 else "")
    print("[helm hostpath] REFUSED: %d host-path match%s in outgoing push%s"
          % (n, "es" if n != 1 else "", more), file=sys.stderr)
    for rel, match in named:
        d_rel = rel if len(rel) <= _HIT_CLIP else rel[:_HIT_CLIP] + "..."
        d_m = match if len(match) <= _HIT_CLIP else match[:_HIT_CLIP] + "..."
        print("[helm hostpath]   %s: %s" % (d_rel, d_m), file=sys.stderr)
    # The scan's own visibility answer, never an assumed PUBLIC: an UNKNOWN
    # answer refuses exactly as PUBLIC does, and says it is UNKNOWN.
    print("[helm hostpath] these match a host filesystem-path pattern "
          "and must not be pushed to %s"
          % ("; ".join(destinations) or "a remote not proven private"),
          file=sys.stderr)
    print("[helm hostpath] false positive? that is an OWNER decision: "
          "HELM_HOSTPATH_SKIP=1 skips this scan for one push",
          file=sys.stderr)
    return 1


if __name__ == "__main__":
    if os.environ.get("HELM_HOSTPATH_SKIP") == "1":
        sys.exit(0)
    sys.exit(main())
