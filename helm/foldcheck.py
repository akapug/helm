"""The five fold checks, as one refusing rung.

Every fold tonight ran these by hand and the fifth is the one that gets
skipped when tired — which is exactly the one that catches an announced-but-
unpushed land (the fold protocol verified four things and none of them was
"reached origin"). A checklist an agent performs is a print; a rung that
refuses is a tooth.

THE FIVE, in the order a fold needs them:

  1. TIP EXISTS      the object is readable here
  2. TREE == GATE    the tree that landed is the tree a RECEIPT vouches for
  3. FF-ABLE         FETCHED trunk is an ancestor, so the fold adds nothing
                     unreviewed
  4. CLEAN           worktree HEAD is the tip and nothing is dirty
  5. ORIGIN HAS IT   re-fetched, never inferred from a local ref

ONE FETCH, ONE MOMENT, BOTH ANCESTRY RUNGS. Rungs 3 and 5 ask opposite
questions about the SAME ref — `<remote>/<branch>`, which is a local snapshot
of somebody else's branch and is exactly as stale as the last fetch. `check`
therefore fetches ONCE, before either of them, and hands both the outcome; see
`_fetched_snapshot` for the reviewer scenario that proved why the fetch could
not stay inside rung 5.

TRI-STATE, NEVER BOOLEAN. Each rung answers PASS, REFUSE or UNKNOWN, and
UNKNOWN is a first-class outcome: an unreadable object, a git that times out
and a network that is down are all "I did not measure that", which is a
different claim from "no". A rung that folds UNKNOWN into REFUSE manufactures
a failure, and one that folds it into PASS is the failure this module exists
to prevent. `ok()` is True only when every rung PASSED.

EACH VERDICT CARRIES ITS DISCRIMINATOR — the value actually measured, not a
restatement of the rule. "tree <yours> != gate <theirs>", with both values
spelled out, tells the reader what to go look at; "tree check failed" tells
them nothing and reads the same whether the trees differ by one byte or the
gate was never run.

NOTHING HERE SPAWNS GIT DIRECTLY. Every question goes through `helm/vcs.py`,
which already owns the tri-state this module is built on: `Vcs.ancestry`
returns ANCESTOR / NOT_ANCESTOR / UNKNOWN with the same law and the same
incident behind it. The first version of this file hand-rolled its own
`subprocess.run(("git", ...))` boundary and re-derived that invariant beside
the seam that already had it; the direct-spawn audit in tests/test_vcs.py
refused the tree, which is the guard working exactly as intended.

THE RUNG-2 LAW, WRITTEN IN BLOOD ON THIS FILE'S SECOND DAY. The gate tree is
DERIVED FROM THE RECEIPT STORE, never handed in by the caller. The first
version took `--gate-tree` as text and compared min-length prefixes, and two
reviewers independently proved the consequence within an hour: `--gate-tree 0`
returned PASS against a real tree, and so did the real tree with eight junk
hex characters welded on — a 48-character string naming no object at all. (The
junk string itself lives in tests/test_foldcheck.py, where it is an executable
input rather than prose: a decorative hex token in a docstring is
indistinguishable to `docref_guard` from a citation it must resolve, and to a
reader from a sha somebody meant.) That is the
`a-supplied-input-lets-a-guard-be-fed-its-own-answer` class, and its signature
is that it fails OPEN: feeding it a wrong value does not produce a confusing
refusal, it produces agreement. Every other rung here fails closed. So a bare
tree is now UNKNOWN by construction — proving that two strings match is not
evidence that a suite ever ran.
"""

import re

from . import gate, vcs

PASS = "PASS"
REFUSE = "REFUSE"
UNKNOWN = "UNKNOWN"

_GATE_TOKEN = re.compile(r"\A(?:gate:)?([0-9a-f]{16})\Z")
_FULL_TREE = re.compile(r"\A[0-9a-f]{40}\Z")

# How fresh the ONE remote-tracking snapshot is that BOTH ancestry rungs read.
# Three states, not a bool, for the same reason every verdict here is
# tri-state: "I declined to ask" and "I asked and got nothing" are different
# facts, and each rung words its UNKNOWN from the one that applies.
_FRESH = "fresh"          # the fetch ran; the refs are current as of it
_SKIPPED = "skipped"      # --no-fetch: the caller declined the round trip
_FAILED = "failed"        # the remote was asked and could not answer


class Rung(object):
    """One check, its verdict, and the value that decided it."""

    __slots__ = ("name", "verdict", "discriminator")

    def __init__(self, name, verdict, discriminator):
        self.name = name
        self.verdict = verdict
        self.discriminator = discriminator

    def __repr__(self):
        return "%-14s %-7s %s" % (self.name, self.verdict, self.discriminator)


def _text(backend, repo, *args, **kw):
    """(rc, stdout) through the seam.

    `Vcs.text` reports spawn trouble as rc -1 rather than raising, and a git
    that cannot answer exits 128; both are "could not ask" and every caller
    below turns them into UNKNOWN rather than REFUSE.
    """
    rc, out, _err = backend.text(repo, *args, **kw)
    return rc, (out or "").strip()


def _one_line(text):
    """git's complaint as ONE line, whitespace collapsed, bounded.

    A discriminator is rendered as one line per rung by `report`, so an
    embedded newline does not make the report longer — it makes it WRONG,
    splitting one rung's verdict across two rows that read like two rungs. git
    says `fatal: <the thing>` on its first line and boilerplate after it, so
    the first non-empty line is both the shortest and the most informative.
    """
    for line in (text or "").splitlines():
        line = " ".join(line.split())
        if line:
            return line[:100]
    return ""


def _askable(backend, repo):
    """Is this a repository we can interrogate at all?

    LOAD-BEARING, and a real bug in this module taught it: `git cat-file -t`
    exits 128 BOTH for "no such object" and for "not a repository", so an
    ABSENT SHA and an UNASKABLE GIT are indistinguishable by rc alone —
    exactly the collapse this file exists to prevent, reproduced inside it.
    It surfaced only because a rung demanded a positive control on the same
    observable, which the original test lacked.

    So askability is proved ONCE and separately. After that, a non-zero rc
    from cat-file is a genuine NO rather than a shrug.
    """
    rc, _ = _text(backend, repo, "rev-parse", "--git-dir")
    return rc == 0


def _tip_exists(backend, repo, tip):
    if not _askable(backend, repo):
        return Rung("tip-exists", UNKNOWN,
                    "%s is not an interrogable git repository — a shrug, "
                    "not a no" % repo)
    rc, out = _text(backend, repo, "cat-file", "-t", tip)
    if rc != 0 or out != "commit":
        return Rung("tip-exists", REFUSE,
                    "%s is not a readable commit here (git says %r)"
                    % (tip[:12], out))
    return Rung("tip-exists", PASS, "%s is a commit" % tip[:12])


def _tree_matches_gate(backend, repo, tip, gate_ref):
    """THE RUNG THAT MUST NOT BE FED ITS OWN ANSWER — see the module docstring.

    `gate_ref` is a RECEIPT HANDLE (`gate:<16-hex>`), and the tree it binds is
    read out of the receipt store. Anything else is UNKNOWN: a caller-supplied
    tree can only ever prove that two strings agree, which is not the question.
    """
    if not gate_ref:
        return Rung("tree-vs-gate", UNKNOWN,
                    "no gate cited — the receipt's own tree is the only thing "
                    "that proves a suite ran on what you are about to push")
    token = _GATE_TOKEN.match(str(gate_ref).strip())
    if not token:
        return Rung("tree-vs-gate", UNKNOWN,
                    "%r is not a gate handle. This rung binds a RECEIPT: a "
                    "tree handed in by the caller proves string agreement, "
                    "never that a gate ran. Pass gate:<16-hex>."
                    % str(gate_ref)[:48])
    token = token.group(1)
    try:
        rows, unavailable, _skipped = gate.receipts()
    except Exception as exc:                      # a store this rung cannot read
        return Rung("tree-vs-gate", UNKNOWN,
                    "the receipt store could not be read (%s)" % (exc,))
    if unavailable:
        return Rung("tree-vs-gate", UNKNOWN,
                    "the receipt store is unavailable (%s)" % (unavailable,))
    found = [r for r in rows if str(r.get("id") or "") == token]
    if not found:
        return Rung("tree-vs-gate", UNKNOWN,
                    "no receipt %s in this store — it may have been minted on "
                    "another host and never imported (`helm gate import`), or "
                    "its content no longer hashes to its id. Either way this "
                    "rung has not measured anything" % token)
    row = found[0]
    status = str(row.get("status") or "").upper()
    if status != "OK":
        return Rung("tree-vs-gate", REFUSE,
                    "receipt %s is %s, not OK — a gate that did not pass "
                    "cannot authorize a fold" % (token, status or "UNSTATED"))
    if row.get("dirty"):
        return Rung("tree-vs-gate", REFUSE,
                    "receipt %s was minted on a DIRTY tree (%s), so the tree "
                    "it names is not any commit's tree"
                    % (token, str(row.get("dirty"))[:40]))
    receipt_tree = str(row.get("tree") or "")
    if not _FULL_TREE.match(receipt_tree):
        return Rung("tree-vs-gate", UNKNOWN,
                    "receipt %s carries no full tree (%r) — nothing to compare"
                    % (token, receipt_tree[:48]))
    rc, out = _text(backend, repo, "rev-parse", "%s^{tree}" % tip)
    if rc != 0 or not _FULL_TREE.match(out):
        return Rung("tree-vs-gate", UNKNOWN,
                    "cannot resolve %s^{tree} here (git says %r)"
                    % (tip[:12], out[:48]))
    if out != receipt_tree:
        return Rung("tree-vs-gate", REFUSE,
                    "tree %s != receipt %s tree %s — the gate vouches for a "
                    "different tree than the one you are about to push"
                    % (out[:12], token, receipt_tree[:12]))
    return Rung("tree-vs-gate", PASS,
                "tree %s is the tree receipt %s passed on"
                % (out[:12], token))


def _fetched_snapshot(backend, repo, remote, branch="main", fetch=True):
    """ONE fetch, ONE moment, read by BOTH ancestry rungs -> (state, detail).

    THE THIRD INSTANCE OF THIS FILE'S OWN BUG CLASS, found by reviewer codex-2
    and structural rather than cosmetic. Rungs 3 and 5 both compare against
    `<remote>/<branch>`, and the fetch used to live INSIDE rung 5 — which runs
    AFTER rung 3. Two ancestry rungs, two different moments:

      clone A pushes its lane tip and never fetches again; clone B pushes a
      DESCENDANT of that tip. A's rung 3 asks its untouched local ref, where
      trunk still IS the tip, so trunk is trivially an ancestor -> PASS. Rung
      5 then fetches, and the tip really is on the refreshed origin -> PASS.
      The module prints "all five PASSED — this fold is provable", and a
      direct post-fetch `merge-base --is-ancestor <trunk> <tip>` in A proves
      the tip is NOT fast-forwardable from the trunk that exists now.

    A rung returning a confident answer from evidence that cannot support it
    is the whole thing this module exists to refuse, so the cure is structural
    and not a reordering inside one rung: the fetch happens ONCE, in `check`,
    BEFORE either ancestry rung, and both rungs are handed its outcome. Two
    rungs reading one snapshot cannot disagree about what moment it is.

    THE FAILURE DETAIL IS GIT'S OWN, not an rc. It was `rc 128` before, which
    is exactly the collapse `_askable` is about; and it now decides TWO rungs
    rather than one, so "could not fetch" has to say what the remote actually
    said (`stderr` through the seam, never a spawn of our own) — through
    `_one_line`, because a verdict that wraps is a verdict that misreads.
    """
    if not fetch:
        return _SKIPPED, ""
    # codex-3 cross-review finding: fetch without a refspec fetches every
    # branch but never binds the specific branch rungs 3+5 compare, and
    # without --prune a deleted remote branch survives in the tracking ref.
    refspec = "+refs/heads/%s:refs/remotes/%s/%s" % (branch, remote, branch)
    rc, out, err = backend.text(
        repo, "fetch", remote, refspec, "--prune", "--quiet", timeout=60)
    if rc != 0:
        return _FAILED, (_one_line(err) or _one_line(out) or "rc %d" % rc)
    return _FRESH, ""


def _ff_able(backend, repo, tip, trunk_ref, snapshot):
    """THE THIRD, and it stands on the SAME fetched moment as the fifth.

    `trunk_ref` is `<remote>/<branch>` — a REMOTE-TRACKING ref, which is a
    LOCAL snapshot of somebody else's branch and therefore exactly as stale as
    the last fetch. That is rung 5's substrate, so this rung inherits rung 5's
    law, and its dangerous direction is the same one: a snapshot taken BEFORE
    trunk advanced makes an outdated trunk look like an ancestor and this rung
    says PASS about a tip current trunk cannot fast-forward to. It fails OPEN,
    which is the signature of every defect this file has had.

    SO AN UNREFRESHED SNAPSHOT IS UNKNOWN HERE TOO — the same answer
    `--no-fetch` already gets on rung 5, for the identical argument, and a
    DELIBERATE choice rather than a fallthrough. The considered alternative was
    to keep refusing offline on the stale reading, on the theory that rung 3's
    negative fails closed; it was rejected because the verdict a caller acts on
    is the POSITIVE one, and offline this rung cannot earn a positive. The cost
    is real and is the right cost: an offline `--no-fetch` run no longer prints
    "trunk moved; rebase and re-gate". It never had the standing to print it —
    it had a stale ref that happened to be right.

    THE HINT IS ONE-DIRECTIONAL, ON PURPOSE. When the stale ref ALREADY reads
    NOT an ancestor, the verdict stays UNKNOWN and the discriminator says so,
    because that reading can only send a caller to fetch and look. The opposite
    reading is never echoed: "the stale ref says trunk is an ancestor" is the
    sentence a tired operator reads as consent, and it is the exact sentence
    the scenario above produces.
    """
    fresh, detail = snapshot
    if fresh != _FRESH:
        hint = (" (and the stale ref ALREADY reads NOT an ancestor — expect a "
                "REFUSE once this can be fetched)"
                if backend.ancestry(repo, trunk_ref, tip) == vcs.NOT_ANCESTOR
                else "")
        return Rung("ff-able", UNKNOWN,
                    "%s: %s is a LOCAL ref that may be arbitrarily stale, and "
                    "a trunk that advanced since the last fetch reads here "
                    "exactly like one that did not. This rung did not measure "
                    "whether %s is fast-forwardable%s"
                    % ("--no-fetch" if fresh == _SKIPPED
                       else "could not fetch (%s)" % (detail or "no detail"),
                       trunk_ref, tip[:12], hint))
    state = backend.ancestry(repo, trunk_ref, tip)
    if state == vcs.ANCESTOR:
        return Rung("ff-able", PASS, "%s is an ancestor of the tip" % trunk_ref)
    if state == vcs.NOT_ANCESTOR:
        return Rung("ff-able", REFUSE,
                    "%s is NOT an ancestor of %s — trunk moved; rebase and "
                    "re-gate (the approve carries on patch-id, the gate does not)"
                    % (trunk_ref, tip[:12]))
    return Rung("ff-able", UNKNOWN,
                "cannot compare %s to %s" % (trunk_ref, tip[:12]))


def _clean(backend, repo, tip):
    rc, head = _text(backend, repo, "rev-parse", "HEAD")
    if rc != 0 or not head:
        return Rung("head-clean", UNKNOWN,
                    "cannot read HEAD in %s (git says %r)" % (repo, head[:48]))
    n = min(len(head), len(tip))
    if head[:n] != tip[:n]:
        return Rung("head-clean", REFUSE,
                    "worktree HEAD is %s, not the tip %s" % (head[:12], tip[:12]))
    rc, porcelain = _text(backend, repo, "status", "--porcelain")
    if rc != 0:
        return Rung("head-clean", UNKNOWN,
                    "cannot read the working tree state in %s" % repo)
    dirty = [l for l in porcelain.splitlines() if l.strip()]
    if dirty:
        return Rung("head-clean", REFUSE,
                    "%d uncommitted path(s), first: %s"
                    % (len(dirty), dirty[0].strip()))
    return Rung("head-clean", PASS, "HEAD is the tip and nothing is dirty")


def _origin_has_it(backend, repo, tip, remote, branch, snapshot):
    """THE FIFTH, and the one worth the network round trip.

    A local ref answers "did I merge this", never "can anyone else see it".
    Two lands were announced that origin did not have because this question
    was answered from a local branch, so the fetch is not optional and a
    failed fetch is UNKNOWN — an unreachable remote has told you nothing.

    AND `fetch=False` IS UNKNOWN, NOT A CHEAPER PASS. The flag exists so the
    suite can run without a network, and a reviewer proved what it cost:
    build an origin, push, let a second clone force-rewind origin behind you,
    and the stale local ref still says ANCESTOR. Same repository state, same
    tip: PASS without the fetch, REFUSE with it. A flag that can turn the
    announced-land lie back on is not a performance option.

    THE FETCH ITSELF NOW HAPPENS IN `check`, not here — rung 3 reads the same
    ref and had been reading it one moment EARLIER (see `_fetched_snapshot`).
    This rung's three answers are unchanged; it is handed the snapshot instead
    of taking it.
    """
    ref = "%s/%s" % (remote, branch)
    fresh, detail = snapshot
    if fresh == _SKIPPED:
        return Rung("origin-has-it", UNKNOWN,
                    "--no-fetch: %s is a LOCAL ref that may be arbitrarily "
                    "stale, and a rewound origin reads here exactly like a "
                    "current one. This rung did not measure %s"
                    % (ref, tip[:12]))
    if fresh == _FAILED:
        return Rung("origin-has-it", UNKNOWN,
                    "could not fetch %s (%s) — a stale local ref cannot "
                    "answer this" % (remote, detail or "no detail"))
    state = backend.ancestry(repo, tip, ref)
    if state == vcs.ANCESTOR:
        return Rung("origin-has-it", PASS, "%s is on %s" % (tip[:12], ref))
    if state == vcs.NOT_ANCESTOR:
        return Rung("origin-has-it", REFUSE,
                    "%s is NOT on %s — do not announce this land; the push "
                    "has not been accepted" % (tip[:12], ref))
    return Rung("origin-has-it", UNKNOWN,
                "cannot compare %s to %s" % (tip[:12], ref))


def check(repo, tip, gate_ref=None, remote="origin", branch="main", fetch=True):
    """Run all five against ONE fetched moment. Returns the rungs in fold
    order, never a bare bool.

    `gate_ref` is a receipt handle (`gate:<16-hex>`), not a tree.

    THE FETCH IS HERE, ONCE, AND BEFORE BOTH ANCESTRY RUNGS. Rungs 3 and 5 ask
    opposite questions about the same `<remote>/<branch>` ref; while the fetch
    sat inside rung 5 they answered from two different moments, and that gap is
    a reported all-five-PASS over a tip current trunk could not fast-forward to
    (`_fetched_snapshot` carries the scenario). Binding both rungs to one
    snapshot is what makes the report a single measurement rather than two.
    """
    backend = vcs.backend(repo)
    trunk_ref = "%s/%s" % (remote, branch)
    snapshot = _fetched_snapshot(
        backend, repo, remote, branch=branch, fetch=fetch)
    return [
        _tip_exists(backend, repo, tip),
        _tree_matches_gate(backend, repo, tip, gate_ref),
        _ff_able(backend, repo, tip, trunk_ref, snapshot),
        _clean(backend, repo, tip),
        _origin_has_it(backend, repo, tip, remote, branch, snapshot),
    ]


def ok(rungs):
    """True only if every rung PASSED — UNKNOWN is not consent."""
    return bool(rungs) and all(r.verdict == PASS for r in rungs)


def report(rungs):
    """Lines for a human. Silent-on-success is wrong here: a fold is a
    load-bearing act and its evidence should be readable afterwards."""
    lines = ["%s  %-14s %s" % (
        {PASS: "ok  ", REFUSE: "STOP", UNKNOWN: "????"}[r.verdict],
        r.name, r.discriminator) for r in rungs]
    bad = [r for r in rungs if r.verdict == REFUSE]
    unk = [r for r in rungs if r.verdict == UNKNOWN]
    if bad:
        lines.append("REFUSED by %d rung(s): %s"
                     % (len(bad), ", ".join(r.name for r in bad)))
    elif unk:
        lines.append("NOT PROVEN — %d rung(s) could not be measured: %s. "
                     "That is not a pass." % (len(unk), ", ".join(r.name for r in unk)))
    else:
        lines.append("all five PASSED — this fold is provable")
    return lines
