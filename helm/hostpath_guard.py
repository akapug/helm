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

ONLY A PLAIN GITHUB URL IS ASKED ABOUT (task/3413). gh is asked about the
owner/repo read from the URL, and git sends the push wherever the URL takes
it; unless the two are one repository, a PRIVATE answer about one skips the
scan of a push to the other. MEASURED (git 2.53, libcurl 8.18, a local
logging proxy): a `#` or a `?` before the `@` sent the push to the host
before it, while the URL read as github.com/<owner/repo>; a `..` segment,
percent-encoded or not, sent it to another repository on github.com. So the
destination is asked about only when its URL is a plain GitHub repository
URL (https, ssh or scp, owner/repo and nothing more: `_plain_slug`), the
rule a PUBLIC remote's advertisement is read under too. Any other URL read
as GitHub's is "visibility UNKNOWN (not a plain GitHub repository URL)":
it is scanned, and a first push to an empty one is refused.

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
again; no remote section is named by the push URL; and the URL is not a
remote-helper `::` URL. Fail safe to the full scan, never to trust. The query
itself passes the default upload-pack explicitly, and READS THE URL THROUGH A
PIN (`_pinned`): a one-time name its own environment maps to exactly that URL,
never the URL's own spelling, which git can resolve as a remote name or
rewrite. MEASURED (git 2.53, a cross-family door read of the pinned push): with
a remote named as the push URL A pointing at B, auto-land's pinned push wrote
A and `git ls-remote A` answered with B's refs.
ONE REWRITE IS A PROOF, NOT A STEERING: a PIN (`_pin`), an insteadOf or
pushInsteadOf whose value is the whole push name and whose base is the very
URL git passed, on a name no remote section reads. git sends that push to
exactly the URL, and auto-land's push is pinned so (helm/autoland.py
`_pinned`, task/3265 races R1). MEASURED on train415: the pin read as
"a pushInsteadOf rewrite", the rung refused a fast-forward of trunk as 8965
commits a URL remote is not known to hold, and the same tip pushed by hand
through origin passed.

NO TRACKING REF SAYS WHAT THE DESTINATION HOLDS. A remote-tracking ref
records what some FETCH URL held at the last fetch, and a pushurl, a
pushInsteadOf or a `git remote set-url` sends the push elsewhere while the ref
stays. MEASURED: a host-path blob was published to origin's old URL, `git
remote set-url origin` named a fresh empty repository and kept
refs/remotes/origin/main, and a clean commit narrowed by those refs read 1
blob and sent the old blob to the empty repository unread. The ref line's
remote sha is not a base on its own either: it counts only when the
advertisement carries it.

WHAT A PUBLIC REMOTE ALREADY CARRIES IS NOT READ AGAIN (task/3395). MEASURED
on the dregg fork: a branch based on the current upstream main could not be
pushed to the public fork without the skip, because upstream's own public
commits carry host paths and the fork does not advertise them yet. Those
commits leak nothing. So a push the first walk refuses is walked again
(`_public_base`) without the branch and tag tips a PUBLIC remote ADVERTISES
NOW, and their history. A remote is asked when it holds a remote-tracking ref
here (`ref_owner`: the one remote whose namespace holds it), the sign it was
fetched, and it is all of these: CREDITED, its URL set in the checkout's own
config and only one (`credited_remotes`, the rule the or-free privacy door
`dispatches._commit_public` reads too); NOT THE DESTINATION, by name or by
repository, whose own advertisement is already the base; and PUBLIC by gh,
asked at push time (`_slug_visibility`), so no answer is kept to go stale.

ITS ADVERTISEMENT IS THE EVIDENCE, NEVER ITS TRACKING REF. A tracking ref is a
local record of some fetch. MEASURED by a cross-family approval-tier read of
the previous tip, which credited them: a remote that fetched a private
repository and was then set to a public one, a fetch refspec that writes a
private branch into a public remote's namespace, and a hand `git update-ref`
each let a private host-path commit's first publication to the public fork
pass. So the remote's evidence is `git ls-remote` on ITS URL, the very string
gh's slug was read from (`_advertised_public`), and only an advertised tip
this repository has counts. The URL must name that one repository and no
other (`_steered`): a plain GitHub URL (https, ssh or scp form, owner/repo
and nothing more), no insteadOf rewriting it, and no remote configured under
the URL's own name, which `git ls-remote <url>` resolves first; and the read
follows no HTTP redirect (`_no_redirect_view`). MEASURED (git 2.53): an
insteadOf, a remote named as the URL, and a `<repo>.git/../<other>.git` path
each made `git ls-remote <url>` answer with another repository. A pushurl and
a pushInsteadOf never reach an ls-remote of a URL. A remote gh cannot answer
for, a private one, one not on GitHub, a steered URL, an advertisement that
fails, times out, is empty or names nothing here, and any read that fails
count nothing, and the first walk's hits stand. The note names each remote
that counted and the tips it advertised.

ITS TIME: the second walk runs only after a hit, so a push the first walk
passes asks gh and the network nothing more. After a hit, each remote asked
costs one gh probe (_GH_TIMEOUT seconds, one retry) and, when PUBLIC, one
ls-remote bounded by _LS_REMOTE_TIMEOUT seconds; a timeout counts nothing.

ITS BOUND: gh and ls-remote are two reads seconds apart, and the transport
the user configures (an ssh command, an ssh host alias, an http proxy) is
trusted here as the destination's own ls-remote trusts it.

THE OR-FREE PRIVACY DOOR READS THE SAME EVIDENCE (task/3410).
`dispatches._commit_public` credits a commit as public only when a credited
remote whose repository gh reads PUBLIC, and whose URL `_steered` finds
plain and unsteered, ADVERTISES NOW a tip that reaches it, read by
`_advertised_here`, the reader `_advertised_public` ends in. It keeps its
own visibility rule (a cached answer, admitted only when FRESH) and reads
under its own scrubbed environment, which it hands to `_remote_config` and
`_advertised_here` so that `_steered` judges the config its ls-remote reads.

THE SCAN READS THE HISTORY THE PUSH SENDS (`_object_view`): every object
read runs with replacement objects off. MEASURED (git 2.53): after `git
replace <leak> <stand-in>`, the walk read the stand-in's tree and passed,
while `git push` sent the leak's own objects, because pack-objects never
honours a replacement. It does honour a grafts file, so the walk does too,
and while one is in effect no commit counts as already public: a graft can
make a private commit read as the parent of a public one.

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

THE SHARED-HISTORY RUNG (`--shared-history`) is the second rung this file
carries, and the pre-push hook runs it FIRST, for every push to every remote,
private or public. A push is refused when it would put on the destination a
root commit (a commit with no parent) that the destination does not already
hold, or when it shares no history with the destination's default branch and
carries a commit the destination does not hold. The class it closes: a seat
ran `git push <public remote> <branch>` from the private checkout, the public
repository's main was a release line that shares no history with the private
one, and the push published the whole private development history. The
host-path scan read the blobs and found nothing wrong with them; nothing asked
whether the destination had ever seen this history.

WHY A ROOT, NOT ONLY AN ANCESTOR. The first predicate asked only whether the
pushed tip shares an ancestor with the default branch. MEASURED by a Fable
door read in a scratch world: a branch of the public main that merges the
private lane in (`git merge --allow-unrelated-histories`), and a lane that
merged the public main in (the `git pull <public> main` reflex), both share
history with the public main, both passed, and each put the whole private
history on the public repository. A private history always enters another
repository through its root, so `git rev-list --max-parents=0 <pushed> --not
<held>` names it in every shape: disjoint, joined by a merge, or an orphan
branch. An ordinary train adds no root, so it never fires there.

WHY THE ANCESTOR CHECK STAYS. It carries one fact the root question cannot
see: a destination can hold the private root on a ref that is not its
default branch (an earlier leak left in place, a stray mirror branch). Then
the push adds no root, and every further private commit would pass; the
ancestor check refuses a push that shares no history with the default branch
while it still carries a commit the destination does not hold.

What it reads, in order:

  1. The release marker. The release tool's publish (`scripts/release/
     release.py --publish`) is the one sanctioned way to put a history the
     destination does not share on a remote, and it sets RELEASE_MARKER in its
     push's environment to the ONE commit it publishes. A push passes on the
     marker only when every commit it carries is that commit (a tag counts as
     the commit it peels to). The refusal names the release publish as the
     owner's door and never names the marker.
  2. The destination, from `git ls-remote --symref` on the URL git is pushing
     to, read through the same pin (`_pinned`) and under the same proof
     `_advertised_base` requires (`_proof_failure`).
     A push whose destination is NOT PROVEN is refused here, before any
     advertisement is read: nothing describes the repository it reaches.
     That is a push STEERED away from the remote's fetch URL (a pushurl that
     differs from url, a pushInsteadOf, a -c override, a receive-pack, a
     remote helper), and a proof that could not be made (the push argv, the
     remote config or the URL cannot be read). A pin of the push name to
     exactly the URL git passed (`_pin`, auto-land's push) is proven: its
     default branch is read by ls-remote on that URL like any other.
     MEASURED: a pushurl on origin naming the public repository passed `git
     push origin <lane>` on origin's own tracking refs and published the
     whole private history; then (helm-codex, a cross-family door read) a git
     run under another name hid its argv, and that failure kept the
     tracking-ref judgement, so the same pushurl passed again.
     Otherwise HEAD names the default branch. NO REF AT ALL is a brand-new
     repository: it takes a first push when it is private (`_visibility`) or
     its URL is not a GitHub URL (a bare mirror, a peek room), and refuses it
     when it is PUBLIC or on GitHub with a visibility that cannot be read
     (a URL read as GitHub's that is not plain has none, `_plain_slug`).
     MEASURED by the same read: an empty destination passed unconditionally,
     so `gh repo create --public` and a push published the whole checkout.
     Seats never create remotes, so the unreadable case fails closed.
  3. The judgement, when the tip HEAD advertises is in this repo. Held is
     every commit the destination advertises that this repo has. A pushed
     commit passes when it carries no root that held does not reach and
     shares history with the default branch (`git merge-base`), or when the
     destination already holds every commit it carries (a warning). A
     pushed object that peels to no commit (a tag of a tree or a blob) has
     no history to be judged by, and is refused unless the destination
     already advertises that object: it carries content all the same.
  4. A PROVEN destination whose default branch CANNOT BE READ (ls-remote
     failing, timing out or unparseable, no HEAD advertised, or a HEAD tip
     that is not in this repo) is named plainly, with its fixed reason, and
     the push is refused unless the destination's own advertisement already
     holds every commit it carries: that push publishes nothing, and passes
     with a warning.
     NO REMOTE-TRACKING REF IS EVIDENCE, here or anywhere in the rung. It
     records what the remote's FETCH URL held at the last fetch, and the
     proof (2) binds only the URL. MEASURED by a cross-family door read
     (helm-codex) of the fallback this replaced, which judged such a push by
     the remote's tracking refs: a user pre-push hook, run after git had
     resolved the pushurl and opened receive-pack, removed the pushurl and
     moved the destination, the rung's own ls-remote failed, the push was
     judged by origin's tracking refs and passed, and the open receive-pack
     wrote the lane into the moved repository. Nor is a side branch the
     destination advertises: it can reach every root of a push (an earlier
     leak left in place) while nothing says how the push relates to the
     default branch. The cost is deliberate: a transient ls-remote failure,
     a clone whose view of the default branch is stale, and a remote whose
     HEAD names no branch refuse until the tip is fetched here, or the
     remote names a default branch.

A KNOWN, DELIBERATE REFUSAL: an orphan branch (the gh-pages shape), a merge
of an unrelated project's history (a `git subtree add`, with or without
--squash: the squash commit has no parent either), or a first push of
`refs/notes/*` (its own history) starts a history on any destination, the
private origin too, and is refused; the only door the refusal names is the
release publish.

The refusal names the count of new roots, the pushed tip and the owner's door.
The count it publishes is `git rev-list <pushed> --not <held>`, the number of
commits the push would put on the destination. The rung prints what the
host-path scan prints and no more: a configured remote's NAME
(`_remote_label`), a default branch name that matches a plain ref-name shape,
hex shas, counts and fixed reasons, never a URL, a ref name git passed on
stdin, a remote-tracking ref, or git's output.

The rung has no skip. HELM_HOSTPATH_SKIP belongs to the host-path scan alone.

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
# The UNKNOWN reason for a URL that is not on GitHub at all: a local path, a
# bare mirror, another host. The shared-history rung tells it from a GitHub
# repository whose visibility could not be read.
NOT_GITHUB = "not a GitHub URL"
# The UNKNOWN reason for a URL read as a GitHub repository's that is not a
# plain GitHub repository URL (`_plain_slug`): git can send it elsewhere.
NOT_PLAIN = "not a plain GitHub repository URL"
_GH_TIMEOUT = 30
_GH_RETRY_PAUSE = 1.0
# The note level that carries the visibility answer to the refusal.
_DESTINATION = "destination"
_ARGV_DEPTH = 16
_NOT_PROVEN = "push destination not proven to be the queried one (%s)"
#: The prefix of the one-time name every advertisement query reads through
#: (`_pinned`). It holds a '/', so git reads no remotes or branches file for
#: it, and it is fresh per query, so no remote section is named by it.
QUERY_ALIAS = "helm-hostpath-query/"
# A GIT_CONFIG_COUNT the pin can be appended after: digits, or unset.
_COUNT_RE = re.compile(r"[0-9]*\Z")
_NO_URL = ("git did not pass this push's URL (a hand-run, or a wrapper older "
           "than v2)")
# The two proof causes that say nothing about where the push goes. Every
# other cause proves it goes somewhere the remote's fetch URL is not.
_ARGV_UNREADABLE = "push argv unreadable"
_CONFIG_UNREADABLE = "remote config unreadable"
# The proof failures that say nothing of where the push goes, by the reason
# `_proof_failure` gives, each with what the shared-history refusal says
# could not be read. Every other failure steers the push off the fetch URL.
_BLIND = {_NO_URL: "git did not pass this push's URL",
          _NOT_PROVEN % _ARGV_UNREADABLE: "the argv of the git push this hook "
                                          "runs under cannot be read",
          _NOT_PROVEN % _CONFIG_UNREADABLE: "the remote config cannot be read "
                                            "here"}
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


def _git(root, *args, binary=False, feed=None, timeout=60, env=None):
    p = subprocess.run(("git",) + args, capture_output=True, cwd=root,
                       timeout=timeout, text=not binary, input=feed, env=env)
    return p.returncode, p.stdout, p.stderr


# Sorts a failed ls-remote into "not reachable"; nothing else reads them.
_UNREACHABLE_MARKERS = (
    "could not resolve", "connection refused", "connection timed out",
    "network is unreachable", "failed to connect", "unable to access",
    "could not read from remote repository", "not found",
    "does not appear to be a git repository", "authentication failed",
    "permission denied", "could not read username", "could not read password",
)


def _remote_config(root, env=None):
    """-> [(key, value)] for every remote.* and url.* entry git reads here,
    with the command line's -c entries; None when the config cannot be read.
    git lowercases section and variable names and keeps the subsection.

    The read drops GIT_CONFIG from its environment: `git config` alone
    honours it, and git push reads the repository's own config. MEASURED:
    under GIT_CONFIG=/dev/null this read held no remote entry, the push was
    proven by the URL git passed, and a remote.<name>.receivepack wrote the
    pack into another repository. `env` is the whole environment the reads
    it describes run under (the process's by default): the or-free door
    passes its scrubbed one, so `_steered` judges the config its ls-remote
    reads."""
    rc, out, _ = _git(root, "config", "-z", "--get-regexp",
                      r"^(remote|url)\.",
                      env={k: v for k, v in (
                          os.environ if env is None else env).items()
                           if k != "GIT_CONFIG"})
    if rc not in (0, 1):
        return None
    return [entry.partition("\n")[::2] for entry in out.split("\0") if entry]


def _named(config, name):
    """Is `name` a configured remote (`remote.<name>.url` set) that reads as a
    name, not a URL, an scp <user>@<host>: form or a path?"""
    return bool(config and name and not any(c in name for c in ":@")
                and "remote.%s.url" % name in {k for k, _v in config})


def _remote_label(config, name):
    """THE ONE DOOR for the destination: a diagnostic names the push by this
    and by nothing else. A NAME is shown only when `remote.<name>.url` is
    configured; anything else, a URL, an scp <user>@<host>: form, a filesystem
    path or an unreadable config, is "a URL remote", never any byte of it."""
    if _named(config, name):
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


def _pin(config, name, url):
    """-> the config entries that PIN the push name `name` to exactly `url`,
    else an empty set. Auto-land pushes this way (helm/autoland.py
    `_pinned`, task/3265 races R1): a fresh name that its own environment
    maps to the vetted URL, so no config written after its checks can
    redirect the push.

    A pin is a url.<url>.insteadOf or url.<url>.pushInsteadOf entry whose
    value is the WHOLE name, where the name is not the URL and no remote
    section is named by it (any remote.<name>.* key: git would read that
    section and never rewrite the name). git rewrites a push argument by the
    longest value that is a prefix of it, and appends the rest; the whole
    name is the longest prefix there can be, and the rest is empty, so the
    push goes to exactly `url`, which is the URL git passed (pre-push $2) and
    the one the guard's own pinned query reads (`_pinned`). MEASURED (git
    2.53, a pre-push probe of the pinned push): git passed the name as $1 and
    the pinned URL as $2, with a remote named as that URL configured too.

    Only an entry whose base IS the URL git passed counts. Every other entry
    on the name stays a cause (`_unproven`): one whose base is another URL
    (MEASURED, same probe: on a tie git follows the entry it read first, so
    a repository entry on the name beat the pin's command-line one and $2
    named the other repository), and, conservatively, a shorter prefix of
    the name, which git would not follow past the pin."""
    if not name or name == url or any(
            k.startswith("remote.%s." % name) for k, _v in config):
        return frozenset()
    keys = {"url.%s.%s" % (url, var) for var in ("insteadof", "pushinsteadof")}
    return frozenset((k, v) for k, v in config if k in keys and v == name)


def _unproven(config, name, url, argv):
    """-> None when the push provably goes where ls-remote on `url` looks,
    else the fixed cause. An answer that cannot be known is a cause. An argv
    that cannot be read (`argv` None) hides only what the argv alone carries,
    a --receive-pack: the config, which a -c override reaches through
    GIT_CONFIG_PARAMETERS, is read first, and "push argv unreadable" is the
    cause only when it shows no steering. A PIN of the push name to exactly
    `url` (`_pin`) is not a rewrite that steers the push: it is how git is
    sent to `url` and nowhere else. The guard's own query of `url` is pinned
    too (`_pinned`), so it cannot be answered by a remote section named as
    `url`; such a section is a cause all the same, and so is a rewrite rule
    on the query's own names, the one shape that could tie with its pin."""
    moved = argv is not None and _argv_moves_the_push(argv)
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
    if any(k.startswith("remote.%s." % url) for k, _v in config):
        return "a remote is configured under the push URL's own name"
    pin = _pin(config, name, url)
    rewrites = [(k.rsplit(".", 1)[1], v) for k, v in config
                if k.startswith("url.") and v and (k, v) not in pin]
    if any(var == "insteadof" and v.startswith(QUERY_ALIAS)
           for var, v in rewrites):
        return "a rewrite rule on the guard's own query names"
    pushed = urls + pushurls + [name, url]
    if any(var == "pushinsteadof" and u.startswith(v)
           for var, v in rewrites for u in pushed):
        return "a pushInsteadOf rewrite"
    if any(var == "insteadof" and url.startswith(v) for var, v in rewrites):
        return "an insteadOf rewrite of the push URL"
    return _ARGV_UNREADABLE if argv is None else None


def _proof_failure(config, name, url):
    """-> None when the push to `name` at `url` provably goes where
    ls-remote on `url` looks, else the fixed reason it is not proven: THE
    ONE PROOF both rungs read. `config` is `_remote_config`'s answer; `url`
    is git's pre-push $2, None on a hand-run."""
    if url is None:
        return _NO_URL
    cause = (_CONFIG_UNREADABLE if config is None
             else _unproven(config, name, url, _push_argv()))
    return _NOT_PROVEN % cause if cause else None


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


# The URL's HOST is github.com itself, read where the host is rather than
# searched for: `notgithub.com`, or a path segment `github.com/`, is not
# GitHub.
_GITHUB_URL = re.compile(
    r"^[a-z][a-z0-9+.-]*://(?:[^@/]+@)?(?:www\.)?github\.com/(.*)$", re.I)
# A URL whose repository gh may be asked about: ONE repository on github.com,
# spelled so that nothing but that repository can answer it (no port, query,
# fragment, percent-escape or path beyond owner/repo, so no dot segment can
# climb into another). `_plain_slug` also refuses a repo part of dots alone.
_PLAIN_GITHUB_URL = re.compile(
    r"(?:https://(?:[A-Za-z0-9_.~-]+(?::[A-Za-z0-9_.~-]*)?@)?github\.com/"
    r"|ssh://git@github\.com/|git@github\.com:)"
    r"[A-Za-z0-9-]+/([A-Za-z0-9_.-]+?)(?:\.git)?/?\Z")


def _github_slug(remote_url):
    """-> (`owner/repo`, None) for a URL on github.com, else (None, why):
    the one reading of a URL that `_visibility` asks gh about.

    A NAME ASKED WRONG IS ANOTHER REPOSITORY'S ANSWER, and a private one
    skips the scan. So the host must be github.com itself (a host that only
    ends in it, or a path that contains it, was once read as GitHub), and
    only a trailing `.git` is removed (removing every one asked about
    `owner/ownerhub.io` for `owner/owner.github.io`)."""
    if not remote_url:
        return None, "no remote URL"
    # Normalise: SSH git@github.com:owner/repo -> https://github.com/owner/repo
    url = re.sub(r"^git@([^:]+):", r"https://\1/", remote_url)
    m = _GITHUB_URL.match(url)
    if not m:
        return None, NOT_GITHUB
    slug = re.sub(r"\.git$", "", m.group(1).rstrip("/")).split("/")
    if len(slug) < 2:
        return None, "no owner/repo in the GitHub URL"
    return "/".join(slug[:2]), None


def _plain_slug(url):
    """-> the `owner/repo` of a PLAIN GitHub repository URL, else None: THE
    ONE RULE for a URL whose repository gh may be asked about, the push
    destination's (`_visibility`), a PUBLIC remote's (`_steered`), and every
    remote `repofacts.slug_of` names for the board and the or-free door.

    Plain is `_PLAIN_GITHUB_URL` with a repo part that is not dots alone.
    Every other URL `_github_slug` reads can reach a host or a repository
    that is not the one it names. MEASURED (git 2.53, libcurl 8.18, through a
    local logging proxy, a logging GIT_SSH_COMMAND and a local path): a `#`
    or a `?` before the `@` ends the host for curl, so the push went to the
    host before it; curl removes `..` and `%2e%2e` segments before the
    request, so the push went to another repository on github.com; ssh and
    scp send a `..` path to the server as written; and a file:// URL on the
    host github.com is a local path. Each of them read as the one
    github.com/<owner/repo> in the URL."""
    m = _PLAIN_GITHUB_URL.match(url or "")
    if not m or not m.group(1).strip("."):
        return None
    return _github_slug(url)[0]


def _visibility(remote_url):
    """-> (PRIVATE | PUBLIC | UNKNOWN, why). `why` is None unless UNKNOWN.

    Only PRIVATE skips the scan; the caller treats UNKNOWN as public. A URL
    that is not on GitHub, or names no owner/repo, is UNKNOWN at once, since
    asking again cannot change it (`_github_slug`). So is a GitHub URL that
    is not plain (`_plain_slug`): git can send its push to another host or
    another repository than the one gh would be asked about, so no answer
    about that one may skip the scan. Otherwise gh is asked
    (`_slug_visibility`)."""
    owner_repo, why = _github_slug(remote_url)
    if owner_repo is None:
        return UNKNOWN, why
    if _plain_slug(remote_url) != owner_repo:
        return UNKNOWN, NOT_PLAIN
    return _slug_visibility(owner_repo)


def _slug_visibility(owner_repo):
    """-> (PRIVATE | PUBLIC | UNKNOWN, why) for github.com/<owner_repo>,
    asked NOW: no answer is kept, so none can go stale.

    A failed gh probe is asked once more after _GH_RETRY_PAUSE seconds, and
    only two failures make UNKNOWN. `why` is fixed words and an exit code or
    exception type name, never gh's output: that output can carry the
    repository slug and whatever gh says about its credential. gh is asked
    `github.com/<owner/repo>` (`_gh_visibility`)."""
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
    """One `gh repo view` probe -> (PRIVATE | PUBLIC | UNKNOWN, why).

    The host is spelled out: a bare `owner/repo` goes to whatever host
    GH_HOST names. MEASURED with gh 2.46: GH_HOST=bogus.invalid sent
    `akapug/helm` there, while `github.com/akapug/helm` was answered by
    github.com."""
    try:
        p = subprocess.run(
            ("gh", "repo", "view", "github.com/" + owner_repo, "--json",
             "isPrivate"),
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


# ---- what a PUBLIC remote already carries (see the module docstring) ----
# `credited_remotes` and `ref_owner` are THE ONE RULE for which remote may be
# asked what it carries, `_steered` and `_advertised_here` the one rule for
# how it is asked, and `_plain_slug` the one rule for which URL names a
# repository. The or-free privacy door (`dispatches._commit_public`) and
# `repofacts.slug_of` call them too: this file imports nothing from helm, so
# the shared rules live here.

#: the configs that are THIS checkout's (`git config --show-scope`); an
#: include is reported with the scope of the file that includes it
OWN_CONFIG_SCOPES = ("local", "worktree")
# Every scope `git config --show-scope` prints for a remote URL. Any other
# word is read as "unknown", so no byte of git's output reaches a reason.
_SCOPES = ("system", "global", "local", "worktree", "command")
_NOT_COUNTED = "not counted as public: %s: %s"


def credited_remotes(rows):
    """({remote: slug} a verdict may credit, {remote: why not} for each
    other remote a GitHub URL was read for). `rows` are {remote, slug,
    scope}, one per `remote.<name>.url` git reads, a remote named with two
    URLs being two rows; `slug` is None for a URL that is not GitHub's.

    A REMOTE VOUCHES FOR ITS REFS ONLY WHEN ONE URL IS PLAINLY ITS OWN. Two
    shapes pair a public slug with refs fetched from elsewhere, and both are
    credited with nothing:

    A URL SET OUTSIDE THE CHECKOUT. A reader keeps HOME's and the system's
    config because git needs them, and git reads both BEFORE the checkout's
    own. MEASURED (the or-free door, reading `repofacts.remotes`): a HOME
    .gitconfig naming a GitHub URL for a remote the checkout points at a
    backup path admitted the backup's private tip.

    TWO URLS. `git remote set-url --add` gives one remote a second URL; git
    fetches from the first, and the refs do not say which. MEASURED: the
    dict this replaced kept the LAST URL, so a public second URL admitted
    the private first one's tip.

    Two URLs that name ONE repository (an ssh and an https spelling) are
    one slug and stay credited."""
    named = {}
    for row in rows:
        if row.get("remote"):
            named.setdefault(row["remote"], []).append(row)
    credited, doubted = {}, {}
    for name, own in named.items():
        slugs = {row.get("slug") for row in own}
        if not any(slugs):
            continue                    # not GitHub: never asked or doubted
        foreign = sorted({row.get("scope") or "unknown" for row in own}
                         - set(OWN_CONFIG_SCOPES))
        if foreign:
            doubted[name] = ("a URL for it is set in the %s config, outside "
                             "this checkout's own" % " and ".join(foreign))
        elif len(slugs) > 1:
            doubted[name] = ("this checkout names %d different URLs for it, "
                             "and git fetches from only the first"
                             % len(slugs))
        else:
            credited[name] = slugs.pop()
    return credited, doubted


def ref_owner(ref, claimants):
    """-> the one remote among `claimants` whose namespace
    `refs/remotes/<name>/` holds `ref`, or None. A ref two remotes' names
    could both own (`a` and `a/b`) is credited to neither, so an ambiguous
    name can only refuse. `claimants` names every remote the checkout
    configures, credited or not: each owns a namespace."""
    owners = {n for n in claimants if ref.startswith("refs/remotes/%s/" % n)}
    return owners.pop() if len(owners) == 1 else None


def _object_view(base=None):
    """The environment of every object read in the host-path scan: the
    history `git push` itself sends. No replacement objects: MEASURED (git
    2.53), with `git replace <leak> <stand-in>` the walk read the stand-in's
    tree while the push sent the leak's objects, because pack-objects never
    honours a replacement. A grafts file stays in effect, because
    pack-objects honours it: MEASURED, a graft that adds a parent sent that
    parent's objects, and a walk with grafts off never read them. A grafts
    file can also make a private commit read as the parent of a public one,
    so no commit counts as already public while one is in effect
    (`_public_evidence`). `base` is the environment it is built on, the
    process's by default."""
    return dict(os.environ if base is None else base,
                GIT_NO_REPLACE_OBJECTS="1")


def _remote_rows(root):
    """-> [{remote, slug, scope}] for every `remote.<name>.url` git reads
    here, with the config scope it read it from (`credited_remotes`). A
    command-line -c entry reads as scope "command". Raises `_ScanError` when
    the config cannot be read. GIT_CONFIG is dropped, as `_remote_config`
    drops it. The records are NUL-separated: a value may hold a newline."""
    rc, out, _err = _git(root, "config", "-z", "--show-scope",
                         "--get-regexp", r"^remote\..*\.url$",
                         env={k: v for k, v in os.environ.items()
                              if k != "GIT_CONFIG"})
    # `--get-regexp` exits 1 when nothing matched: a checkout with no remote
    if rc not in (0, 1) or (rc == 1 and out.strip()):
        raise _ScanError("the remote URLs cannot be read", rc)
    fields = out.split("\0")
    if fields[-1:] == [""]:
        fields.pop()
    if len(fields) % 2:
        raise _ScanError("the remote URLs cannot be read")
    rows = []
    for scope, entry in zip(fields[0::2], fields[1::2]):
        key, _nl, url = entry.partition("\n")
        name = key[len("remote."):-len(".url")] if (
            key.startswith("remote.") and key.endswith(".url")) else ""
        if name:
            rows.append({"remote": name, "url": url,
                         "slug": _github_slug(url)[0],
                         "scope": scope if scope in _SCOPES else "unknown"})
    return rows


def _steered(config, url, slug):
    """-> None when `git ls-remote <url>` here can be answered by
    github.com/<slug> and no other repository, else the fixed reason it
    could be steered (module docstring). `config` is `_remote_config`'s
    answer, every scope and the command line's -c entries included."""
    if _plain_slug(url) != slug:
        return "its URL is not a plain GitHub repository URL"
    if config is None:
        return "the remote config cannot be read"
    for key, value in config:
        if (key.startswith("url.") and key.endswith(".insteadof")
                and url.startswith(value)):
            return "an insteadOf rewrite applies to its URL"
        if key.startswith("remote.%s." % url):
            return "a remote is configured under its URL's own name"
    return None


def _no_redirect_view(base=None):
    """The environment of a PUBLIC remote's ls-remote: git reads
    http.followRedirects as false, so a redirect fails the read instead of
    letting another repository answer. It is appended to the command-line
    config, which git reads after every file and after GIT_CONFIG_COUNT, so
    it is the value in effect. MEASURED (git 2.53): after a GIT_CONFIG_COUNT
    entry and an earlier GIT_CONFIG_PARAMETERS entry each setting true, git
    reads false. `base` is the environment it is built on, the process's by
    default."""
    base = os.environ if base is None else base
    prior = base.get("GIT_CONFIG_PARAMETERS", "")
    return dict(base, GIT_CONFIG_PARAMETERS=(
        prior + " " if prior else "") + "'http.followredirects=false'")


def _advertised_public(root, config, slug, url):
    """-> ([the tips `url` advertises now that are here], None) when gh
    reads github.com/<slug> PUBLIC and nothing else can answer `url`, else
    ([], why); `why` is None for a PRIVATE repository, which is not
    named."""
    why = _steered(config, url, slug)
    if why:
        return [], why
    state, unknown = _slug_visibility(slug)
    if state == PRIVATE:
        return [], None
    if state == UNKNOWN:
        return [], "visibility UNKNOWN (%s)" % unknown
    return _advertised_here(root, url)


def _advertised_here(root, url, env=None, timeout=None):
    """-> ([the tips `url` advertises now that are here], None), or ([], the
    fixed reason none counts): THE ONE READ of what a PUBLIC remote carries,
    for the host-path scan's second walk (`_advertised_public`) and for the
    or-free privacy door (`dispatches._commit_public`). The caller has
    already found `url` not steered (`_steered`) and its repository PUBLIC,
    each by its own visibility rule. The read follows no redirect
    (`_no_redirect_view`) and runs past `timeout` seconds
    (_LS_REMOTE_TIMEOUT by default) as a failure; `env` is the whole
    environment of both reads, the process's by default."""
    advertised, why = _advertisement(root, url, env=_no_redirect_view(env),
                                     timeout=timeout)
    if why:
        return [], why
    if not advertised:
        return [], "it advertises no branch or tag"
    return _held_here(root, advertised, env=env)


def _public_base(root, config, name, url):
    """-> (the tips a PUBLIC remote advertises now and this repo has, the
    lines that say which remote advertised which tip and what did not
    count). NEVER RAISES: a read that fails counts nothing as public, and
    the caller keeps every hit it had.

    `name` and `url` are the push destination's: git's pre-push $1, and the
    URL the scan asked gh about. The destination's own remote, and any
    remote naming its repository, is skipped (module docstring)."""
    try:
        return _public_evidence(root, config, name, url)
    except Exception as exc:  # noqa: BLE001 — every failure counts nothing
        return [], ["no commit is counted as already public: %s"
                    % _scan_failure(exc)]


def _public_evidence(root, config, name, url):
    """See `_public_base`."""
    rc, out, _err = _git(root, "for-each-ref", "--format=%(refname)",
                         "refs/remotes/")
    if rc != 0:
        raise _ScanError("the remote-tracking refs cannot be read", rc)
    tracked = out.splitlines()
    if not all(ref.startswith("refs/remotes/") for ref in tracked):
        raise _ScanError("the remote-tracking refs cannot be read")
    if not tracked:
        return [], []
    rc, out, _err = _git(root, "rev-parse", "--git-path", "info/grafts")
    grafts = out.rstrip("\n")
    if rc != 0 or not grafts:
        raise _ScanError("the grafts file cannot be located", rc)
    if os.path.lexists(os.path.join(root, grafts)):
        raise _ScanError("a grafts file rewrites which commits are parents "
                         "here")
    rows = _remote_rows(root)
    claimants = {row["remote"] for row in rows}
    owned = {ref_owner(ref, claimants) for ref in tracked} - {None}
    credited, doubted = credited_remotes(rows)
    # the URL a remote's advertisement is read from: its first, the one git
    # fetches from (every URL of a credited remote names its one slug)
    urls = {}
    for row in rows:
        urls.setdefault(row["remote"], row["url"])
    # GitHub reads an owner/repo in any case, so one spelled in another
    # case is still the destination.
    here = (_github_slug(url)[0] or "").lower()
    public, counted, lines = [], [], []
    for remote in sorted(owned):
        if remote == name or (here and credited.get(remote, "").lower()
                              == here):
            continue
        label = _remote_label(config, remote)
        if remote in doubted:
            lines.append(_NOT_COUNTED % (label, doubted[remote]))
            continue
        if remote not in credited:
            continue                    # not GitHub: never asked
        tips, why = _advertised_public(root, config, credited[remote],
                                       urls[remote])
        if why:
            lines.append(_NOT_COUNTED % (label, why))
        if tips:
            public.extend(tips)
            counted.append("already public: %s advertises %s%s now and gh "
                           "reads it PUBLIC, so what that reaches is not read "
                           "again" % (label, ", ".join(
                               t[:12] for t in tips[:_NAMED_N]),
                               " and %d more" % (len(tips) - _NAMED_N)
                               if len(tips) > _NAMED_N else ""))
    return list(dict.fromkeys(public)), counted + lines


def _advertised_base(root, name, url, config):
    """-> (advertised shas this repo has, why the scan reads everything).

    `name` and `url` are git's pre-push $1 and $2 (`url` None on a
    hand-run), `config` from `_remote_config`. An empty base reads every blob
    reachable from the pushed sha, and `why` says why."""
    why = _proof_failure(config, name, url)
    if why:
        return [], why
    advertised, why = _advertisement(root, url)
    if why:
        return [], why
    if not advertised:
        return [], "the destination advertises no branch or tag (a first push)"
    return _held_here(root, advertised)


def _pinned(url, env=None):
    """-> (name, env): the one-time name an advertisement query is told to
    read, and the environment that makes git resolve that name to exactly
    `url`; or (None, the fixed reason no pin can be built). EVERY QUERY OF
    WHAT A REPOSITORY HOLDS goes through it: the push destination's, in both
    rungs, a PUBLIC remote's, and the or-free door's (`_advertisement`,
    `_destination`).

    A QUERY OF `url` BY ITS OWN SPELLING CAN READ ANOTHER REPOSITORY.
    `git ls-remote <url>` reads a remote section NAMED `url` first, and a
    remotes or branches file for a name with no '/'; then the longest
    insteadOf value that prefixes it. MEASURED (git 2.53, a cross-family
    door read of the pinned push): auto-land's pinned push went to A with a
    remote named A pointing at B configured, git passed A as pre-push $2, and
    `git ls-remote A` answered with B's refs, so the rung judged B for a push
    that wrote A. So the query is given a fresh name holding a '/' (no
    section or file can name it) and this environment maps that WHOLE name
    to `url` for insteadOf, the longest value that can prefix it; git
    rewrites once and never reads the result as a remote name. MEASURED on
    the same git: the pinned query read A beside that remote; beside a rule
    whose value is a prefix of A (which the unpinned query followed to
    A/A.git); after auto-land's own inherited pin pairs; and beside a
    repository rule on QUERY_ALIAS itself, which is shorter and loses.

    The pair is appended to the command-line config the caller's
    environment already carries (`env`, the process's by default), so the
    query sees every other setting the push sees. A GIT_CONFIG_COUNT that is
    not plain digits is not appended to, and the query is not run: git reads
    ' 1' and '+1' as 1 (MEASURED, git 2.53), and a count read otherwise here
    would put the pin where git does not read it."""
    base = dict(os.environ if env is None else env)
    count = base.get("GIT_CONFIG_COUNT") or ""
    if not _COUNT_RE.match(count):
        return None, "the command-line config count cannot be read"
    n = int(count or 0)
    name = QUERY_ALIAS + os.urandom(16).hex()
    base.update({"GIT_CONFIG_COUNT": str(n + 1),
                 "GIT_CONFIG_KEY_%d" % n: "url.%s.insteadOf" % url,
                 "GIT_CONFIG_VALUE_%d" % n: name})
    return name, base


def _advertisement(root, url, env=None, timeout=None):
    """-> ([the sha of every branch and tag `url` advertises now], None), or
    (None, the fixed reason it cannot be read): THE ONE READER of an
    advertisement, the push destination's, a PUBLIC remote's and the or-free
    door's. A read that runs past `timeout` seconds (_LS_REMOTE_TIMEOUT by
    default) is a failure. The query passes the default upload-pack
    explicitly, and reads `url` through a pin (`_pinned`), never by its own
    spelling."""
    query, env = _pinned(url, env)
    if query is None:
        return None, env
    try:
        rc, out, err = _git(root, "ls-remote", "--upload-pack=git-upload-pack",
                            "--heads", "--tags", query, feed="",
                            timeout=_LS_REMOTE_TIMEOUT if timeout is None
                            else timeout, env=env)
    except subprocess.TimeoutExpired:
        return None, "ls-remote timed out"
    if rc != 0:
        return None, _ls_remote_failure(rc, err)
    advertised = []
    for ln in out.splitlines():
        sha, tab, ref = ln.partition("\t")
        if not (tab and ref and _SHA_RE.match(sha)):
            return None, "ls-remote returned no parseable refs"
        advertised.append(sha)
    return advertised, None


def _held_here(root, advertised, env=None):
    """-> ([the advertised objects this repo has], None), or ([], why): only
    those can bound a walk here. Only an id that was asked for is kept.
    `env` is the environment the read is built on, the process's by
    default."""
    asked = list(dict.fromkeys(advertised))
    rc, out, _err = _git(root, "cat-file", "--batch-check=%(objectname) "
                        "%(objecttype)", feed="".join(
                            sha + "\n" for sha in asked),
                        env=_object_view(env))
    if rc != 0:
        return [], ("cannot check which advertised objects are here "
                    "(exited %d)" % rc)
    wanted, have = set(asked), []
    for ln in out.splitlines():
        fields = ln.split()
        if len(fields) == 2 and fields[1] != "missing" and fields[0] in wanted:
            have.append(fields[0])
    if not have:
        return [], ("none of the %d advertised object(s) is in this repo"
                    % len(asked))
    return have, None


def _outgoing_blobs(root, new, base, what="the advertised base"):
    """-> ([(sha, path, size)], failure) for every blob the push adds.

    `base` is what need not be read: what the remote already has, and after
    a hit what a PUBLIC remote already carries (`what` names it). When it is
    empty, or rev-list cannot read it, every blob reachable from `new` is
    read, and `failure` says why rev-list refused the base. Every read runs
    in `_object_view`."""
    out, failure, view = None, None, _object_view()
    if base:
        # On stdin, not argv: a destination can advertise more refs than an
        # argv holds.
        rc, out, err = _git(root, "rev-list", "--objects", "--stdin", new,
                            binary=True, env=view, feed="".join(
                                "^%s\n" % sha for sha in base).encode("ascii"))
        if rc != 0:
            out, failure = None, ("rev-list refused %s (exited %d)"
                                  % (what, rc))
    if out is None:
        rc, out, err = _git(root, "rev-list", "--objects", new, binary=True,
                            env=view)
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
        "%(objectsize)", binary=True, env=view,
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
    per chunk of at most _CHUNK bytes (a larger blob is a chunk alone), in
    `_object_view`: a replaced blob would read the stand-in's bytes."""
    i, view = 0, _object_view()
    while i < len(blobs):
        j, total = i + 1, blobs[i][2]
        while j < len(blobs) and total + blobs[j][2] <= _CHUNK:
            total += blobs[j][2]
            j += 1
        chunk = blobs[i:j]
        rc, out, err = _git(
            root, "cat-file", "--batch", binary=True, env=view,
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
    two can disagree. When absent (legacy hand-run), resolve by name.

    A HIT IS READ AGAIN without what a PUBLIC remote already carries
    (`_public_base`), and only a hit: a push the first walk passes asks gh
    nothing more, and the second walk can only drop a hit the first found
    or, when rev-list refuses its base, read everything."""
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
    held = [] if why else ["%s does not already have" % label]
    hits = _host_paths(root, blobs)
    if hits:
        public, told = _public_base(root, config, remote_name, url)
        notes.extend(("note", ln) for ln in told)
        if public:
            blobs, failure = _outgoing_blobs(
                root, new, base + public, "the advertised and public base")
            if failure:
                notes.append(("note", "full scan: %s: %s — every blob "
                              "reachable from %s is read"
                              % (label, failure, new[:12])))
                held = []
            else:
                held.append("no PUBLIC remote advertises")
            hits = _host_paths(root, blobs)
    if not blobs:
        return [], notes + [("note", "no files in outgoing push to scan")]
    if not hits:
        notes.append(("note", "%d blob(s) scanned%s — no host-path matches"
                      % (len(blobs), ", the ones " + " and ".join(held)
                         if held else "")))
    else:
        notes.append((_DESTINATION, "a PUBLIC remote"
                      if visibility == PUBLIC else
                      "%s: visibility UNKNOWN (%s), treated as public"
                      % (label, unknown)))
    return hits, notes


def _host_paths(root, blobs):
    """-> [(path, match)] for the host-path shapes in `blobs`."""
    paths = {sha: path for sha, path, _size in blobs}
    hits = []
    for sha, blob in _read_blobs(root, blobs):
        for m in _HOSTPATH_BYTES_RE.finditer(blob):
            hits.append((paths[sha], m.group().decode("ascii")))
            if len(hits) >= 20:
                break
    return hits


# ---- the shared-history rung (`--shared-history`; see the docstring) ----

#: The release tool sets this in the environment of its publish pushes, to
#: the one commit it publishes. scripts/release/release.py carries the same
#: literal, and tests/test_shared_history_push.py pins that the two agree.
RELEASE_MARKER = "HELM_RELEASE_PUBLISH"
_HISTORY = "[helm shared-history]"
_OWNER_DOOR = ("a history the destination does not share is published only "
               "through the owner's door, the release publish: python3 "
               "scripts/release/release.py <version> --publish")
# The destination's four states (`_destination`).
READ, EMPTY, UNREADABLE, UNPROVEN = "READ", "EMPTY", "UNREADABLE", "UNPROVEN"
# A default branch name is printed only in this plain shape.
_BRANCH_RE = re.compile(r"refs/heads/([A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*)\Z")


def _destination(root, name, url, config):
    """-> (state, branch, tip, advertised, why) for the push destination.

    READ: `tip` is the sha its HEAD advertises and `branch` the short name of
    the branch HEAD names, when that name has a plain ref-name shape (else
    None). EMPTY: it advertises no ref at all, a brand-new repository.
    UNREADABLE: the destination is proven, its advertisement or default
    branch cannot be read, `why` says why in fixed words, and `advertised`
    holds what it did advertise. UNPROVEN: where the push goes is not proven
    (`_proof_failure`), `why` is that reason, and nothing was queried.
    `advertised` is every sha the destination advertises."""
    why = _proof_failure(config, name, url)
    if why:
        return UNPROVEN, None, None, [], why
    query, env = _pinned(url)
    if query is None:
        return UNREADABLE, None, None, [], env
    try:
        rc, out, err = _git(root, "ls-remote", "--symref",
                            "--upload-pack=git-upload-pack", query, feed="",
                            timeout=_LS_REMOTE_TIMEOUT, env=env)
    except subprocess.TimeoutExpired:
        return UNREADABLE, None, None, [], "ls-remote timed out"
    if rc != 0:
        return UNREADABLE, None, None, [], _ls_remote_failure(rc, err)
    branch = tip = None
    advertised = []
    for ln in out.splitlines():
        left, tab, ref = ln.partition("\t")
        if tab and ref and left.startswith("ref: "):
            if ref == "HEAD":
                m = _BRANCH_RE.match(left[len("ref: "):])
                branch = m.group(1) if m else None
            continue
        if not (tab and ref and _SHA_RE.match(left)):
            return UNREADABLE, None, None, [], (
                "ls-remote returned no parseable refs")
        advertised.append(left)
        if ref == "HEAD":
            tip = left
    if not advertised:
        return EMPTY, None, None, [], None
    if tip is None:
        return UNREADABLE, None, None, advertised, (
            "it advertises no default branch")
    return READ, branch, tip, advertised, None


def _held_commits(root, shas):
    """-> the commits among `shas` that this repo has, deduplicated."""
    want = list(dict.fromkeys(shas))
    if not want:
        return []
    rc, out, _err = _git(root, "cat-file", "--batch-check=%(objectname) "
                         "%(objecttype)", feed="".join(s + "\n" for s in want))
    if rc != 0:
        raise _ScanError("object listing failed", rc)
    wanted, have = set(want), []
    for ln in out.splitlines():
        fields = ln.split()
        if len(fields) == 2 and fields[1] == "commit" and fields[0] in wanted:
            have.append(fields[0])
    return list(dict.fromkeys(have))


def _commit_of(root, rev):
    """-> the commit `rev` (a sha or a ref name) peels to here, or None."""
    rc, out, _err = _git(root, "cat-file", "--batch-check=%(objectname) "
                         "%(objecttype)", feed=rev + "^{commit}\n")
    if rc != 0:
        raise _ScanError("object listing failed", rc)
    # One input, one answer line: the first.
    fields = out.split("\n", 1)[0].split()
    if len(fields) == 2 and fields[1] == "commit" and _SHA_RE.match(fields[0]):
        return fields[0]
    return None


def _shares(root, commit, base):
    """Does `commit` have a common ancestor with `base`?"""
    rc, _out, _err = _git(root, "merge-base", commit, base)
    if rc == 0:
        return True
    if rc != 1:
        raise _ScanError("merge-base failed", rc)
    return False


def _walk_feed(tips, held):
    return "".join(["%s\n" % t for t in tips] + ["^%s\n" % h for h in held])


def _unshared(root, tips, held):
    """-> how many commits reachable from `tips` are not reachable from
    `held`: what the push would put on a destination that holds `held`."""
    rc, out, _err = _git(root, "rev-list", "--stdin",
                         feed=_walk_feed(tips, held))
    if rc != 0:
        raise _ScanError("commit walk failed", rc)
    return len({ln for ln in out.splitlines() if _SHA_RE.match(ln)})


def _new_roots(root, tips, held):
    """-> the root commits (no parent) reachable from `tips` and not from
    `held`: each is the first commit of a history the push would start on a
    destination that holds `held`."""
    rc, out, _err = _git(root, "rev-list", "--max-parents=0", "--stdin",
                         feed=_walk_feed(tips, held))
    if rc != 0:
        raise _ScanError("root walk failed", rc)
    return list(dict.fromkeys(ln for ln in out.splitlines()
                              if _SHA_RE.match(ln)))


def judge_push(root, name, url, pushes):
    """-> (refused, lines) for the push to `name` at `url`: THE DOOR FOR A
    CALLER of the shared-history rung. `pushes` is [local sha] for each
    outgoing ref that is not a deletion; `lines` are what the rung prints.
    Any exception becomes a fresh `_ScanError` in fixed words, raised with no
    chained context, as `scan_push` does."""
    try:
        return _judge_push(root, name, url, pushes)
    except (SystemExit, KeyboardInterrupt):
        raise
    except BaseException as exc:  # noqa: BLE001 — the door; see docstring
        failed = _scan_failure(exc)
    raise _ScanError(failed)


def _judge_push(root, name, url, pushes):
    """See `judge_push` and the module docstring's shared-history section."""
    out, tips, bare = [], [], []
    for sha in dict.fromkeys(pushes):
        commit = _commit_of(root, sha)
        if commit is None:
            bare.append(sha)
        elif commit not in tips:
            tips.append(commit)
    if not tips and not bare:
        return False, out
    marker = os.environ.get(RELEASE_MARKER, "")
    if tips and not bare and marker and all(t == marker for t in tips):
        return False, out + ["%s the release publish of %s: passed"
                             % (_HISTORY, marker[:12])]
    config = _remote_config(root)
    label = _remote_label(config, name)
    state, branch, tip, advertised, why = _destination(root, name, url, config)
    if state == EMPTY:
        return _judge_empty(root, url, label, tips, bare)
    # A pushed object that peels to no commit (a tag of a tree or a blob)
    # carries content with no history to judge it by, and this door refuses
    # what it cannot judge: it passes only what the destination already
    # advertises. MEASURED: a tag of the private main's tree passed as "no
    # history to judge" and put every blob of that tree on the public repo.
    leak = [s for s in bare if s not in advertised]
    if leak:
        return True, out + [
            "%s REFUSED: %s carries no commit (a tag of a tree or a blob): "
            "its content has no history to judge by, and %s does not hold it"
            % (_HISTORY, ", ".join(s[:12] for s in leak), label),
            "%s %s" % (_HISTORY, _OWNER_DOOR)]
    out.extend("%s %s carries no commit: %s already holds it"
               % (_HISTORY, s[:12], label) for s in bare)
    if not tips:
        return False, out
    # A push whose destination is not proven reaches a repository nothing
    # here describes, so nothing is evidence of what it holds: no
    # advertisement was read, and no remote-tracking ref is consulted.
    # Steered (a pushurl, a pushInsteadOf, a -c override, a receive-pack, a
    # remote helper), it goes where the fetch URL is not. MEASURED: a pushurl
    # naming the public repository passed `git push origin <lane>` on origin's
    # own tracking refs and published the whole private history. Blind (an
    # argv, a remote config or a URL that cannot be read), nothing says where
    # it goes. MEASURED by a cross-family door read: a git run
    # under another name hid its argv, that failure kept the tracking-ref
    # judgement, and a pushurl naming the public repository passed again.
    if state == UNPROVEN:
        blind = _BLIND.get(why)
        return True, out + [
            "%s REFUSED: the default branch of %s cannot be read (%s), and "
            "this push carries %d new root commit(s) that %s is not known to "
            "hold" % (_HISTORY, label, why, len(_new_roots(root, tips, [])),
                      label),
            "%s   %s" % (_HISTORY, (
                "%s, so where this push goes is not proven, and no "
                "remote-tracking ref is evidence of what its destination "
                "holds" % blind) if blind else (
                "the remote-tracking refs of %s describe its fetch URL, not "
                "where this push goes, and are no evidence of what its "
                "destination holds" % label)),
            _publishes(root, tips, [], label, "is not known to hold"),
            "%s %s" % (_HISTORY, _OWNER_DOOR)]
    held = _held_commits(root, advertised)
    if state == READ and tip in held:
        return _judge_default(root, label, branch, tip, tips, held, out)
    # The destination is proven and its default branch cannot be read: an
    # ls-remote that fails or does not parse, no HEAD, or a HEAD tip not
    # here. Only its own advertisement is evidence (module docstring, 4):
    # a push it already holds in full publishes nothing, and anything else
    # is refused. No remote-tracking ref stands in for the default branch.
    if state == READ:
        why = "its tip is not in this repo"
        lead = "the tip of that branch is not in this repo until it is fetched"
    elif advertised:
        lead = "the destination advertises no default branch"
    else:
        lead = "the destination cannot be read"
    if held and not _unshared(root, tips, held):
        return False, out + [
            "%s WARNING: the default branch of %s cannot be read (%s), but %s "
            "already holds every commit this push carries: passed"
            % (_HISTORY, label, why, label)]
    roots = _new_roots(root, tips, held)
    head = ("this push carries %d new root commit(s) that %s is not known to "
            "hold" % (len(roots), label) if roots else
            "this push carries no new root commit, but carries commits %s is "
            "not known to hold" % label)
    return True, out + [
        "%s REFUSED: the default branch of %s cannot be read (%s), and %s"
        % (_HISTORY, label, why, head),
        "%s   %s, so this push cannot be checked against that branch: no "
        "remote-tracking ref stands in for it, and only a push whose every "
        "commit %s advertises passes" % (_HISTORY, lead, label),
        _publishes(root, [t for t in tips if _unshared(root, [t], held)],
                   held, label, "is not known to hold"),
        "%s %s" % (_HISTORY, _OWNER_DOOR)]


def _judge_default(root, label, branch, tip, tips, held, out):
    """-> (refused, lines) for `tips` pushed to a destination whose default
    branch `branch` advertises the tip `tip`, which is in this repo and in
    `held`, the commits it advertises that this repo has."""
    default = "the default branch %s of %s" % (branch or "HEAD", label)
    seen = "(tip %s)" % tip[:12]
    roots = _new_roots(root, tips, held)
    disjoint = [t for t in tips if not _shares(root, t, tip)]
    apart = bool(disjoint) and _unshared(root, disjoint, held) > 0
    if not roots and not apart:
        if disjoint:
            return False, out + [
                "%s WARNING: this push shares no history with %s, but %s "
                "already holds every commit it carries: passed"
                % (_HISTORY, default, label)]
        return False, out + ["%s %s: shares history with its default branch "
                             "%s, and carries no new root commit"
                             % (_HISTORY, label, branch or "HEAD")]
    offenders = [t for t in tips if _new_roots(root, [t], held)
                 or (t in disjoint and _unshared(root, [t], held))]
    if apart:
        head = "this push shares no history with %s %s" % (default, seen)
        detail = ("it carries %d new root commit(s) that %s does not hold"
                  % (len(roots), label) if roots else
                  "it carries no new root commit: %s holds its root on a ref "
                  "other than its default branch" % label)
    else:
        head = ("this push carries %d new root commit(s) that %s does not "
                "hold" % (len(roots), label))
        detail = ("it joins that history to %s %s through a merge"
                  % (default, seen))
    return True, out + [
        "%s REFUSED: %s" % (_HISTORY, head), "%s   %s" % (_HISTORY, detail),
        _publishes(root, offenders, held, label, "does not hold"),
        "%s %s" % (_HISTORY, _OWNER_DOOR)]


def _publishes(root, offenders, evidence, label, holds):
    """The refusal line that counts what the offending tips would publish."""
    return ("%s   it would publish %d commit(s) that %s %s (pushed tip %s)"
            % (_HISTORY, _unshared(root, offenders, evidence), label, holds,
               ", ".join(t[:12] for t in offenders)))


def _judge_empty(root, url, label, tips, bare):
    """-> (refused, lines) for a destination that advertises no ref at all.
    It takes a first push when it is private or not a GitHub URL (a bare
    mirror, a peek room); a PUBLIC one, or a GitHub one whose visibility
    cannot be read (a URL that is not plain among them, `_plain_slug`),
    refuses it. `_visibility` prints nothing of the URL."""
    visibility, unknown = _visibility(url)
    new = "%s %s has no branch yet (a brand-new repository), " % (_HISTORY,
                                                                  label)
    if visibility == PRIVATE:
        return False, [new + "and it is private: passed"]
    if unknown == NOT_GITHUB:
        return False, [new + "and it is not a GitHub remote: passed"]
    lines = ["%s REFUSED: %s has no branch yet (a brand-new repository), and "
             "%s" % (_HISTORY, label, "it is PUBLIC" if visibility == PUBLIC
                     else "its visibility is UNKNOWN (%s), treated as public"
                     % unknown)]
    if tips:
        lines += ["%s   it carries %d new root commit(s) that %s does not hold"
                  % (_HISTORY, len(_new_roots(root, tips, [])), label),
                  _publishes(root, tips, [], label, "does not hold")]
    if bare:
        lines.append("%s   %s carries no commit (a tag of a tree or a blob), "
                     "and %s does not hold it"
                     % (_HISTORY, ", ".join(s[:12] for s in bare), label))
    return True, lines + ["%s %s" % (_HISTORY, _OWNER_DOOR)]


def main(argv=None):
    """THE TOP-LEVEL DOOR: anything that escapes the pre-push entry becomes
    one fixed refusal, "internal error (<type name>)", rc 2, with no message,
    repr, traceback or chained context; the type name is the only variable
    text. KeyboardInterrupt is answered too ("interrupted", rc 130): left to
    escape, the interpreter prints a traceback that chains whatever exception
    it interrupted. SystemExit passes: nothing here raises it, and its code is
    the caller's to set. The refusal carries the prefix of the rung asked
    for: `--shared-history` is that rung's entry, anything else the
    host-path scan's."""
    if argv is None:
        argv = sys.argv[1:]
    history = argv[:1] == ["--shared-history"]
    try:
        return _history_main(argv) if history else _main(argv)
    except SystemExit:
        raise
    except KeyboardInterrupt:
        failed, rc = "interrupted", 130
    except BaseException as exc:  # noqa: BLE001 — the door; see docstring
        failed, rc = "internal error (%s)" % type(exc).__name__, 2
    print("%s REFUSED: %s" % (_HISTORY if history else "[helm hostpath]",
                              failed), file=sys.stderr)
    return rc


def _ref_records(prefix, quiet=False):
    """-> (records, rc): git's pre-push ref lines on stdin, as
    [(local ref, local sha, remote ref, remote sha)], and rc None; or
    (None, rc) after printing why the input is refused. `quiet` keeps the
    nothing-to-push line unsaid, for the rung that is not the last."""
    try:
        stdin_raw = sys.stdin.read()
    except OSError:
        # Unreadable stdin (a closed fd, a capturing harness) is NOT an
        # up-to-date push: we could not look, so we cannot allow. The error
        # is not printed: its text can carry whatever the reader held.
        print("%s REFUSED: unreadable pre-push input" % prefix,
              file=sys.stderr)
        return None, 2
    if not stdin_raw:
        # Empty pre-push stdin IS the protocol for nothing-to-push: git
        # feeds the hook zero ref lines on an up-to-date push. Not a scan
        # failure — allow. Malformed NON-empty input still refuses below,
        # and that includes whitespace-only bytes: git never emits them,
        # so they mean a broken feeder, not an up-to-date push.
        if not quiet:
            print("%s nothing to push — empty pre-push stdin, no outgoing "
                  "refs to scan" % prefix, file=sys.stderr)
        return [], None
    # splitlines() drops only the ordinary terminal newline. Interior blank
    # or whitespace-only records are NOT filtered: git emits one four-field
    # record per ref and never a blank line, so a blank one means a broken
    # feeder — and filtering it would let a malformed line ride along with
    # valid ones, which is the fail-open the whole chain refuses.
    lines = stdin_raw.splitlines()
    if not any(ln.strip() for ln in lines):
        print("%s REFUSED: non-empty pre-push stdin carries no ref lines "
              "(whitespace only)" % prefix, file=sys.stderr)
        return None, 2
    records = []
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
            print("%s REFUSED: malformed pre-push input — line %d: %s"
                  % (prefix, n, bad), file=sys.stderr)
            return None, 2
        records.append(tuple(fields))
    return records, None


def _history_main(argv):
    """The shared-history entry: `--shared-history [remote [url]]`, git's
    pre-push arguments, and its ref lines on stdin. rc 1 refuses the push."""
    if len(argv) > 3:
        print("usage: hostpath_guard.py --shared-history [remote [url]]  "
              "(run by the helm pre-push guard; stdin = git pre-push ref "
              "lines)", file=sys.stderr)
        return 2
    records, rc = _ref_records(_HISTORY, quiet=True)
    if records is None:
        return rc
    pushes = [sha for _lref, sha, _rref, _rsha in records
              if set(sha) != {"0"}]
    if not pushes:
        return 0
    try:
        refused, lines = judge_push(os.getcwd(), argv[1] if len(argv) > 1
                                    else None, argv[2] if len(argv) > 2
                                    else None, pushes)
    except (RuntimeError, subprocess.TimeoutExpired, OSError) as exc:
        print("%s REFUSED: history check failed — %s"
              % (_HISTORY, _scan_failure(exc)), file=sys.stderr)
        return 2
    for ln in lines:
        print(ln, file=sys.stderr)
    return 1 if refused else 0


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
    records, rc = _ref_records("[helm hostpath]")
    if records is None:
        return rc
    to_scan = []
    deletions = 0
    for _lref, local_sha, _rref, remote_sha in records:
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
    # The skip is the host-path scan's own. The shared-history rung has none.
    if (sys.argv[1:2] == ["--pre-push"]
            and os.environ.get("HELM_HOSTPATH_SKIP") == "1"):
        sys.exit(0)
    sys.exit(main())
