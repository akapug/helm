#!/usr/bin/env python3
"""A pre-push guard: REFUSE when a push to a PUBLIC remote carries files
containing host filesystem paths — the class that bit the CLIProxyAPI fork.

THE 2026-07-29 INCIDENT. The CLIProxyAPI fork carried three literal host
paths under `/home/<account>/` inside a TEST THAT ASSERTED THEY WERE ABSENT.
A forbidden-list contains what it forbids, so THE GUARD BECAME THE LEAK.

CURE: PATTERN, not literal. A regex over the host-path SHAPE removes the
literal AND generalises — `/home/<user>/path` or `/Users/<user>/path`.

PUBLIC-ONLY: a private-repo push to a named collaborator has a different bar.
UNKNOWN fails SAFE: treat as public and REFUSE — a refused push costs a
retry, a leaked path on a public remote may already be cloned or indexed.

The scan reads EVERY blob in the new tree, not just the diff. A file carrying
a host path may have been committed in an earlier push; `old..new` diff only
sees files CHANGED in this push, so we scan everything being pushed now.

Stdlib-only, no helm imports — the hook executes this as a plain script.
"""
import json
import os
import re
import subprocess
import sys

# `/home/<user>/` or `/Users/<user>/` where <user> starts with a letter.
# Excludes bare /home/ and /home without a trailing path segment.
_HOSTPATH_RE = re.compile(r"/(home|Users)/[a-zA-Z][a-zA-Z0-9._-]*/")

_HIT_CLIP = 72
_NAMED_N = 3
_ZERO = "0000000000000000000000000000000000000000"


def _git(root, *args, binary=False):
    p = subprocess.run(("git",) + args, capture_output=True, cwd=root,
                       timeout=60, text=not binary)
    return p.returncode, p.stdout, p.stderr


def _remote_url(root, remote):
    rc, out, _ = _git(root, "remote", "get-url", remote)
    return out.strip() if rc == 0 and out.strip() else None


def _is_public(remote_url):
    """True if the remote is PUBLIC or UNKNOWN (fail-safe)."""
    if not remote_url:
        return True
    # Normalise: SSH git@github.com:owner/repo -> https://github.com/owner/repo
    url = re.sub(r"^git@([^:]+):", r"https://\1/", remote_url)
    parts = url.split("github.com/", 1)
    if len(parts) != 2:
        return True  # non-GitHub, cannot determine -> public
    slug = parts[-1].rstrip("/").replace(".git", "").split("/")
    if len(slug) < 2:
        return True
    owner_repo = "/".join(slug[:2])
    try:
        p = subprocess.run(
            ("gh", "repo", "view", owner_repo, "--json", "isPrivate"),
            capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return True  # gh unavailable -> public
    if p.returncode != 0 or not p.stdout.strip():
        return True
    try:
        data = json.loads(p.stdout)
        return not data.get("isPrivate", True)
    except (json.JSONDecodeError, TypeError):
        return True


def _new_tree_blobs(root, commit):
    """Every (relpath, blob_bytes) in the commit tree."""
    rc, out, _ = _git(root, "ls-tree", "-r", "-z", "--name-only", commit)
    if rc != 0:
        raise RuntimeError("git ls-tree failed")
    result = []
    for rel in out.split("\0"):
        if not rel:
            continue
        rc2, blob, _ = _git(root, "cat-file", "blob", "%s:%s" % (commit, rel),
                            binary=True)
        if rc2 != 0 or blob is None:
            continue
        result.append((rel, blob))
    return result


def scan_push(root, old, new, remote_name, remote_url=None):
    """-> (violations, notes) for the push to `remote_name`.

    `remote_url` is the URL GIT ITSELF is pushing to (pre-push $2). When
    given it is the identity — no re-resolution by name, because (a) the
    name may not resolve at all (`git push <url>` has no remote entry), and
    (b) resolving is a second read of a fact git already handed us, and the
    two can disagree. When absent (legacy hand-run), resolve by name."""
    url = remote_url if remote_url else _remote_url(root, remote_name)
    if not _is_public(url):
        return [], [("note", "remote %r is private — host-path scan skipped"
                     % remote_name)]
    blobs = _new_tree_blobs(root, new)
    if not blobs:
        return [], [("note", "no files in outgoing push to scan")]
    hits = []
    for rel, blob in blobs:
        text = None
        for m in _HOSTPATH_RE.finditer(str(blob, errors="replace")):
            if text is None:
                text = str(blob, errors="replace")
            hits.append((rel, m.group()))
            if len(hits) >= 20:
                break
    notes = []
    if not hits:
        notes.append(("note", "%d file(s) scanned — no host-path matches"
                      % len(blobs)))
    return hits, notes


_SHA_RE = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?\Z")


def main(argv=None):
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
    except OSError as exc:
        # Unreadable stdin (a closed fd, a capturing harness) is NOT an
        # up-to-date push: we could not look, so we cannot allow.
        print("[helm hostpath] REFUSED: cannot read pre-push stdin — %s"
              % exc, file=sys.stderr)
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
    for ln in lines:
        fields = ln.split()
        # BOTH sha fields are validated. scan_push ignores the remote sha
        # today, so a malformed one is not a content bypass — but accepting
        # malformed protocol input because the current consumer happens not
        # to read it is exactly the fail-open this guard exists to refuse.
        if (len(fields) != 4 or not _SHA_RE.match(fields[1])
                or not _SHA_RE.match(fields[3])):
            print("[helm hostpath] REFUSED: cannot parse push info: %r"
                  % ln[:120], file=sys.stderr)
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
        print("[helm hostpath] REFUSED: push scan failed — %s" % exc,
              file=sys.stderr)
        return 2
    for level, msg in notes:
        print("[helm hostpath] %s" % msg, file=sys.stderr)
    if not hits:
        return 0
    n = len(hits)
    named = hits[:_NAMED_N]
    more = ""
    if n > _NAMED_N:
        more = " — +%d more match%s" % (n - _NAMED_N,
                                        "es"[:n - _NAMED_N != 1])
    print("[helm hostpath] REFUSED: %d host-path match%s in outgoing push%s"
          % (n, "es"[:n != 1], more), file=sys.stderr)
    for rel, match in named:
        d_rel = rel if len(rel) <= _HIT_CLIP else rel[:_HIT_CLIP] + "..."
        d_m = match if len(match) <= _HIT_CLIP else match[:_HIT_CLIP] + "..."
        print("[helm hostpath]   %s: %s" % (d_rel, d_m), file=sys.stderr)
    print("[helm hostpath] these match a host filesystem-path pattern "
          "and must not be pushed to a PUBLIC remote", file=sys.stderr)
    print("[helm hostpath] false positive? that is an OWNER decision: "
          "HELM_HOSTPATH_SKIP=1 skips this scan for one push",
          file=sys.stderr)
    return 1


if __name__ == "__main__":
    if os.environ.get("HELM_HOSTPATH_SKIP") == "1":
        sys.exit(0)
    sys.exit(main())
