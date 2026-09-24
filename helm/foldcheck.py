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

import calendar
import re
import time

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
    if not row.get("suite"):
        # This rung's whole sentence is "a suite ran on what you are about to
        # push" — a FOCUSED or custom receipt is a true claim about a
        # DIFFERENT sentence (the tests it selected), and reading it as the
        # suite's would launder a partial run into fold authority.
        return Rung("tree-vs-gate", REFUSE,
                    "receipt %s is not a whole-suite run — it proves the "
                    "tests it selected, never the suite a fold vouches for; "
                    "run `helm gate run` on this tree" % token)
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

    THE THIRD INSTANCE OF THIS FILE'S OWN BUG CLASS, found by a review
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
    # A cross-family read found that fetch without a refspec fetches every
    # branch but never binds the specific branch rungs 3+5 compare, and
    # without --prune a deleted remote branch survives in the tracking ref.
    refspec = "+refs/heads/%s:refs/remotes/%s/%s" % (branch, remote, branch)
    rc, out, err = backend.text(
        repo, "fetch", remote, refspec, "--prune", "--quiet", timeout=60)
    if rc != 0:
        return _FAILED, (_one_line(err) or _one_line(out) or "rc %d" % rc)
    return _FRESH, ""


def _ff_able(backend, repo, tip, trunk_ref, snapshot, landed):
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
        # "rebase and re-gate" is the WRONG INSTRUCTION for a lane that already
        # landed — there is nothing to rebase onto and nothing left to gate.
        # Same tri-state discipline as origin-has-it: only an explicit True
        # rewrites the advice, and a None never masquerades as a False.
        # ASKED HERE AND ONLY HERE on this path — see check()'s supplier note.
        content = landed()
        if content is True:
            return Rung("ff-able", REFUSE,
                        "%s is NOT an ancestor of %s by sha, because this "
                        "lane ALREADY LANDED and the fold rebased it. Do not "
                        "rebase or re-gate; its change is on %s already"
                        % (trunk_ref, tip[:12], trunk_ref))
        if content is None:
            # THE COMMENT ABOVE ONCE CLAIMED THIS AND THE CODE DID NOT DO IT.
            # A None fell through to "rebase and re-gate", which ASSERTS the
            # lane never landed — the precise collapse _landed_by_content's
            # docstring warns about, committed one rung away from the warning.
            # Caught by a cross-family read because the tri-state arm covered
            # only origin-has-it, so no arm here could have reddened.
            return Rung("ff-able", REFUSE,
                        "%s is NOT an ancestor of %s by sha, and whether this "
                        "lane's change already reached %s under another sha "
                        "could NOT be measured — settle that before rebasing, "
                        "because rebasing a lane that already landed is "
                        "meaningless work"
                        % (trunk_ref, tip[:12], trunk_ref))
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


def _landed_by_content(backend, repo, tip, ref):
    """True / False / None — did this CHANGE ever reach `ref`, by any sha.

    THE RUNGS ABOVE ASK ANCESTRY, WHICH IS SHA IDENTITY, AND OUR PROTOCOL LANDS
    WORK REBASED: the integrator rebases the chain onto current trunk and gates
    the rebased tree, so the landed commit carries a different object id.
    Ancestry then answers "no" TRUTHFULLY about a lane whose every line is on
    trunk, and the author is told not to announce a land that already happened.
    Measured specimen, 2026-08-11: lane spiral-guard-sees-an-open-meld was
    REFUSED by three rungs while `git cherry` marked both of its commits as
    already upstream. It is on trunk as 9b3ed7640. THAT SHA IS THE LANDED ONE
    AND THE LANE TIP IS DELIBERATELY NOT QUOTED HERE, because the rebase this
    function exists to recognise is what makes a lane tip unquotable: the
    pre-fold sha resolves only in the clone that still carries the branch, so
    citing it fails the docstring-citation guard in any fresh checkout. This
    docstring cited it and the gate went red — the defect demonstrating itself
    inside its own cure.

    `landed_ever` IS THE MODE, and NOT `patch_identity`, which is the trap this
    function exists to avoid rather than a preference. patch_identity answers
    FALSE for a commit that is literally an ancestor of trunk — `git cherry`
    over an empty range has nothing to mark — so swapping ancestry for it does
    not fix the ordinary case, it INVERTS it, and every plain fast-forwardable
    lane would begin refusing. landed_ever is true for BOTH shapes.

    NONE IS NOT FALSE. It means this could not be measured — an unreadable
    gitdir, or a predicate that could not answer — and callers must test it
    with `is`, never truthiness, or a cannot-look silently becomes a did-not-
    land. Measured the same day: for one unlanded lane tip this returns False
    and for another it returns None, so the distinction is load-bearing rather
    than defensive.

    THE IMPORT IS DEFERRED ON PURPOSE. helm/landreq.py imports BOTH foldcheck
    and query, and query imports landreq, so a module-level import here closes
    the cycle and breaks the package. The gitdir is resolved with
    --absolute-git-dir because plain --git-dir answers a bare relative ".git"
    from a main checkout, which resolves against the caller's cwd rather than
    the repo, and a wrong gitdir returns None — the exact non-answer this
    docstring warns about, arriving silently.
    """
    from . import query
    rc, gitdir = _text(backend, repo, "rev-parse", "--absolute-git-dir")
    if rc != 0 or not gitdir:
        return None
    try:
        return query.query_did_land(gitdir, tip, ref, mode="landed_ever")
    except Exception:
        return None


# A dispatch row in one of these states has produced no review of any code:
# CANCELLED was withdrawn, OPEN and HELD have not answered yet. None of them
# can account for a commit, so none of them belongs in the blind horizon.
_NO_REVIEW_YET = frozenset(("cancelled", "open", "held"))


def _row_stamp(row):
    """Epoch seconds for a dispatch row's own timestamp, or 0.

    THE LEDGER WRITES `ts` AS AN ISO-8601 STRING, NOT A NUMBER, and that cost
    me a wrong published result: an `isinstance(value, (int, float))` filter
    excluded all 12,057 events and I recorded "no row carries a timestamp" as
    a measured negative. Every row carries one. Parse, never type-test.
    """
    raw = str((row or {}).get("ts") or "").strip()
    if not raw:
        return 0
    try:
        return calendar.timegm(time.strptime(raw, "%Y-%m-%dT%H:%M:%SZ"))
    except (ValueError, TypeError):
        return 0


def _cars_reviewed(backend, repo, tip, trunk_ref):
    """THE SIXTH, and the only one that asks about REVIEW rather than the tree.

    THE GAP IT CLOSES (task/987, measured 2026-08-11 on the raw dispatch
    ledger). Every other rung here is about the tree and the push: does the tip
    exist, does it match its gate, can trunk fast-forward, is the worktree
    clean, does origin have it. NOTHING asked whether anyone had reviewed the
    work — so two lanes folded with ZERO review rows and nothing refused. The
    shape was not forgetfulness: both were handed to the integrator directly by
    posting a green gate and the word LANDABLE, and a gate binds a TREE while a
    review binds CONTENT. Rung 3's own comment says "so the fold adds nothing
    unreviewed", which is a claim about ANCESTRY — no unreviewed commits ride
    in BEHIND you — and reads exactly like the check this module did not have.

    WHY THIS IS A PATCH-IDENTITY JOIN AND NOT A ROW COUNT. Three formulations
    were tried on paper first and all three FALSE-REFUSE the workflow the fleet
    actually uses, every one in the same direction:

      (1) matching an approve's reviewed tip by SHA refuses every REBASED lane;
      (2) requiring an approve to name THIS gate token refuses every COMPOSED
          train, because approvals carry by CONTENT across a rebase while the
          final-tree gate is minted AFTER it;
      (3) counting review rows on the lane LABEL refuses every COMPOSE, which
          has none of its own — the integrator merges N reviewed cars onto a
          fresh branch, so the composed label is new and its row count is zero
          while every car underneath it was properly reviewed.

    The common cause is that a fold operates on a TREE deliberately not
    identical to anything any reviewer saw, and the review evidence is attached
    to the CARS. So the question is asked per COMMIT, by patch identity, which
    is the join an integrator already performs by hand on a composed train:
    derive each car as its CHAIN-BOUND range (`rowworld._work_pair`, base and
    tip from the ledger, never a whole-branch diff, which absorbs unrelated
    ancestry), and ask whether every commit being folded appears in one.

    EVERY RANGE IN ONE INDEX, because `git log -p` accepts many ranges in a
    single invocation and `_patch_ids` turns any number of commits into two
    spawns. Per-row indexing would be two spawns per row on a ledger with
    thousands of them, which is how a correct rung becomes one nobody runs.

    WHAT THIS RUNG DOES NOT PROVE, stated because the weaker property is the
    one I would otherwise be tempted to ship under the stronger name: it
    proves COVERAGE — every folded commit is inside some dispatched review
    range — and it does NOT read verdict polarity. A car covered by a row that
    was never approved, or approved without a gate token, passes here. That
    second question is `landreq._chain_authority`'s, it keys on a ROW ID while
    this holds a TIP, and wiring it is the next layer rather than a line. The
    discriminator says so in words so a reader cannot borrow the stronger
    claim from the rung's name.

    NOT WIRED INTO `check()` YET, AND THE REASON IS MEASURED RATHER THAN
    CAUTIOUS. Two formulations of the row population were tried against the
    live ledger and BOTH produce a rung that can never refuse:

      (1) `rowworld._rows_for(rows, gitdir)` returns the WRONG POPULATION for a
          worktree lane. Measured on a lane whose review row exists and carries
          CONCUR at its exact tip: the worktree gitdir is `.git/worktrees/<lane>`
          while the row recorded a different repo_id, so the filter returned six
          rows, NONE of them this lane's, and none of them pairing.
      (2) Widening to every row and scoping by RESOLVABILITY fixes the
          population — 1954 of 3241 rows yield a chain-bound pair — but 1287 do
          not, so a global "some row was unpairable" escape is permanently true
          and the REFUSE branch is unreachable on every fold that will ever run.

    So the honest state is: the join WORKS, and the open question is which
    unpaired rows could plausibly have covered a given commit. A guard that
    cannot fire is worse than the gap it covers, because it reads as coverage —
    which is the same failure this rung exists to fix, one layer up. task/987
    carries both measurements.

    UNKNOWN IS LOAD-BEARING AND OUTNUMBERS REFUSE ON PURPOSE. An unmatched
    commit is only a refusal when the reviewed index was COMPLETE. If any row
    could not yield a chain-bound pair — `_work_pair` returns (None, None) for
    a row with no carrier and no verified retip, which is a legitimate and
    common state — then a range that might have covered this commit was never
    built, and the honest answer is that it could not be measured. A rung that
    refuses wrongly on the fleet's busiest path is worse than the gap it
    closes, and this door is on every fold.
    """
    if not _askable(backend, repo):
        return Rung("cars-reviewed", UNKNOWN,
                    "%s is not an interrogable git repository — a shrug, "
                    "not a no" % repo)
    # DEFERRED FOR THE CYCLE, same reason as `_landed_by_content`: landreq
    # imports foldcheck, and dispatches/rowworld sit under that same package
    # graph, so a module-level import here closes the loop.
    from . import dispatches, rowworld
    # THE COMMON GITDIR, VIA THE PRIMITIVE THAT EXISTS FOR THIS. My first cut
    # used `--absolute-git-dir`, which in a worktree is `.git/worktrees/<name>`
    # and matches ZERO stored rows — `dispatches._repo_info` stamps `repo_id`
    # from `--git-common-dir` through realpath. `rowworld.repo_identity` was
    # written for exactly this and its docstring had already measured my
    # result a month early, to the number: filtering on the worktree gitdir
    # "derives the WRONG POPULATION — six repo-less legacy rows and nothing
    # else". I got six. Read the neighbouring module's docstring before
    # resolving an identity by hand.
    canonical, identity_err = rowworld.repo_identity(repo)
    if identity_err:
        return Rung("cars-reviewed", UNKNOWN,
                    "%s, so no review evidence could be joined"
                    % _one_line(str(identity_err)))
    rc, gitdir = _text(backend, repo, "rev-parse", "--absolute-git-dir")
    if rc != 0 or not gitdir:
        return Rung("cars-reviewed", UNKNOWN,
                    "the git directory could not be resolved, so no patch "
                    "index could be built")
    folded, err = rowworld._patch_ids(gitdir, ["%s..%s" % (trunk_ref, tip)])
    if err:
        return Rung("cars-reviewed", UNKNOWN,
                    "the commits being folded could not be indexed by patch "
                    "identity (%s)" % _one_line(str(err)))
    if not folded:
        # NOT A PASS BY LUCK. An empty range means trunk already carries this
        # tip, and rungs 3 and 5 own that question; there is simply no commit
        # here whose review could be missing.
        return Rung("cars-reviewed", PASS,
                    "%s adds no commit over %s, so there is no unreviewed "
                    "car to find" % (tip[:12], trunk_ref))
    rows, unavailable = dispatches.snapshot()
    if unavailable:
        return Rung("cars-reviewed", UNKNOWN,
                    "the dispatch ledger could not be read (%s), so no review "
                    "evidence could be joined" % _one_line(str(unavailable)))
    # SCOPED BY WHAT THIS REPOSITORY CAN RESOLVE, NOT BY `repo_id`.
    # `rowworld._rows_for` filters on repo_id == gitdir, and that is the right
    # rule where a row's ARTIFACTS get spent on it — but it is the wrong
    # population here and it silently produced a dead rung. Measured on a lane
    # whose review row exists and is CONCUR at its exact tip: the worktree's
    # gitdir is `.git/worktrees/<lane>` while the row recorded a different
    # repo_id, so `_rows_for` returned SIX rows, none of them this lane's, NONE
    # of them pairing — and the rung then answered UNKNOWN forever, unable to
    # pass or refuse. A guard that cannot fire is worse than the gap it covers,
    # because it reads as coverage.
    #
    # PATCH IDENTITY IS SELF-SCOPING, which is what makes the wider population
    # safe: a range from unrelated work simply does not contain this commit's
    # patch id, so admitting foreign rows cannot manufacture a false PASS. What
    # it CAN do is break the batch, because `git log A..B` fails outright on a
    # rev this repository has never heard of. So the filter becomes
    # RESOLVABILITY — both endpoints must exist here — which is a question
    # about what can actually be measured rather than about bookkeeping.
    # SCOPED BY THE CANONICAL IDENTITY THE WRITER USED, which is what makes
    # this the right population rather than the whole ledger.
    mine = rowworld._rows_for(rows, canonical)
    carriers = rowworld._carriers(rows)
    ranges, unpaired, unmeasured, blind_until = set(), 0, 0, 0
    reach_refs = set()
    for row in mine.values():
        # A ROW THAT REVIEWED NOTHING CANNOT HAVE COVERED ANYTHING, so it
        # contributes NEITHER CREDIT NOR HORIZON. THIS GUARD DOMINATES
        # `_work_pair` — it must run before the pair is derived, not inside
        # either arm.
        #
        # ROUND ONE (row 251e6648583581b4): it sat BELOW the ref
        # handling, so a CANCELLED or still-OPEN row was already in `ranges`
        # by the time it was skipped and its tip's patch identity counted as
        # review evidence.
        #
        # ROUND TWO (row 5bf9eef37584496b, Fab-measured): I then put
        # it inside the PAIRLESS arm, which fixed the instance and not the
        # invariant. An OPEN row with a verified RETIP derives a real
        # (base, tip) from `_work_pair`, so it never reached the guard at all
        # and its unreviewed patch was credited — a probe returned a false
        # PASS, "all 1 folded commit inside a dispatched review range", and my
        # own focused arms were green through it. `retip` is OPEN-only by
        # construction, so that state is production-reachable, not contrived.
        #
        # An over-PASS is the worse direction: a false refusal argues with
        # you, a false pass says nothing.
        #
        # Measured on this ledger: 411 of 1263 pairless rows are CANCELLED
        # — withdrawn, never reviewed — and another 51 are OPEN or HELD,
        # i.e. pending rather than lost. Counting those as "a row that might
        # cover this commit" also inflated the blind set by 37%.
        if str(row.get("status") or "") in _NO_REVIEW_YET:
            continue
        base, car_tip = rowworld._work_pair(row, rows, carriers)
        if not (base and car_tip):
            # NO CHAIN-BOUND RANGE, BUT THE ROW STILL NAMES ITS REVIEWED TIP.
            # That single commit is real evidence and discarding it was what
            # made the unmeasured set look four times bigger than it is:
            # measured on this ledger, 949 of the 1255 pairless rows carry a
            # ref that resolves here, leaving 306 genuinely unknowable rather
            # than 1255.
            ref = str(row.get("ref") or "").strip()
            resolvable = bool(ref) and _text(
                backend, repo, "cat-file", "-e", "%s^{commit}" % ref)[0] == 0
            if resolvable:
                ranges.add(ref)
                reach_refs.add(ref)
            else:
                unpaired += 1
            # EVERY REMAINING PAIRLESS ROW RAISES THE BLIND HORIZON, INCLUDING ONE WHOSE
            # REF RESOLVED. Contributing a tip is not contributing a RANGE: a
            # first-round review row covers the commits BEFORE its tip too, and
            # those are exactly the ones this join cannot derive. Counting only
            # the fully-unmeasurable rows produced FALSE REFUSALS on two lanes
            # I knew to be reviewed — the misses were each lane's EARLIEST
            # commits, reviewed by a recent pairless row, and the rung accused
            # them because that row was not "unmeasurable enough" to count.
            unmeasured += 1
            # ONLY AN UNRESOLVABLE ROW RAISES THE *TIME* HORIZON, and splitting
            # the two is what brought this rung back from the dead. The comment
            # below still argues correctly that an unmeasurable row cannot have
            # reviewed a commit that did not exist yet — but it applied that
            # escape to EVERY pairless row, and blind_until is a HIGH-WATER
            # MARK, so in a live fleet it tracks the PRESENT: any seat recording
            # any verdict on any pairless row pushes it to now, and then every
            # commit in every lane predates it. Measured 2026-09-09: the newest
            # raiser was 24 MINUTES old and all three live lanes I ran this
            # against answered UNKNOWN, including two I knew to be reviewed.
            # The comment three screens down says "the newest such row is twelve
            # days old" — that was TRUE WHEN WRITTEN and the ledger moved.
            #
            # A row whose ref RESOLVES is not unmeasurable in that way: what it
            # could have covered is exactly the ANCESTORS OF THAT REF, which git
            # enumerates, so it contributes a bounded REACH instead of an
            # unbounded clock. Measured on the same ledger: 576 of the 803
            # raisers resolve, and excluding them moves the horizon from 24
            # minutes old to 2026-08-12 — four weeks — which is what makes a
            # REFUSE reachable again.
            if not resolvable:
                stamp = _row_stamp(row)
                if stamp and stamp > blind_until:
                    blind_until = stamp
            continue
        # THE TIP, NOT THE RANGE, AND THE REASON IS A GIT SEMANTIC THAT MADE
        # THIS RUNG SILENTLY WRONG. `git log A..B C..D` is NOT the union of two
        # ranges: it is (all positive tips) MINUS (all negative bases), one
        # global negative set. So a base recorded by ANY row removes that commit
        # from the WHOLE index, including from a sibling spec that names it
        # explicitly. Measured 2026-09-09, minimal control on the live repo:
        #   ["<A>^!"]                 -> 1 id, A's patch id PRESENT
        #   ["<A>^!", "<A>..<B>"]     -> 9 ids, A's patch id GONE
        #   ["<B>", "^origin/main"]   -> 10 ids, PRESENT
        # A's own singleton is cancelled by a neighbouring range that happens to
        # start at A — and a review chain produces exactly that shape, because
        # round two's base IS round one's tip. That is why every lane's EARLIEST
        # commit went unmatched and why a composed train of properly reviewed
        # cars was refused: the first commit of each car is the one every
        # later round of its own chain excludes.
        #
        # So the index is built from POSITIVE TIPS with ONE negative, the trunk.
        # No base is ever a negative, nothing can cancel anything, and crediting
        # a row with its whole lane rather than its chain segment is correct on
        # its own terms: a reviewer binds the content from trunk to the tip they
        # name, and the commits below a chain-derived base were themselves the
        # reviewed tip of an earlier round.
        if _text(backend, repo, "cat-file", "-e",
                 "%s^{commit}" % car_tip)[0] == 0:
            ranges.add(car_tip)
    reviewed = {}
    if ranges:
        reviewed, rerr = rowworld._patch_ids(
            gitdir, sorted(ranges) + ["^%s" % trunk_ref])
        if rerr:
            return Rung("cars-reviewed", UNKNOWN,
                        "%d reviewed range(s) could not be indexed by patch "
                        "identity (%s)" % (len(ranges), _one_line(str(rerr))))
    covered = set(reviewed.values())
    missing = sorted(sha for sha, pid in folded.items() if pid not in covered)
    if not missing:
        return Rung("cars-reviewed", PASS,
                    "all %d folded commit(s) are inside a dispatched review "
                    "range (%d range(s) joined by patch identity; polarity "
                    "NOT read — coverage only)"
                    % (len(folded), len(ranges)))
    # THE ESCAPE IS PER-COMMIT AND BOUNDED BY TIME, WHICH IS WHAT MAKES REFUSE
    # REACHABLE AT ALL. A row that yielded no range and no resolvable ref is
    # unmeasurable, and a global "some row was unmeasurable" escape is
    # PERMANENTLY TRUE on this ledger — 306 such rows — so it would make this
    # rung incapable of ever refusing, which is worse than the gap it covers.
    #
    # But an unmeasurable row cannot have reviewed a commit THAT DID NOT EXIST
    # WHEN THE ROW WAS WRITTEN. So the escape applies only to commits at or
    # older than the newest unmeasurable row; a commit authored after all of
    # them is a MEASURED absence. Measured on this ledger: the newest such row
    # is twelve days old, and all 24 commits of a live composed train postdate
    # every one of them.
    # THE SECOND ESCAPE, AND IT IS A REACH RATHER THAN A CLOCK. A pairless row
    # whose ref resolves contributed that one commit to `ranges`, but a review
    # covers the commits BEFORE its tip too and this join cannot derive that
    # range. Those commits are not unknown, though: they are exactly the
    # ANCESTORS of that ref. Enumerating them turns "some row might cover this"
    # into a set membership test, and it is SELF-SCOPING the same way patch
    # identity is — a lane tip is not an ancestor of another lane's tip, so a
    # foreign row's reach cannot swallow this lane's commits. One rev-list, and
    # only when something is actually missing.
    reach = set()
    if reach_refs:
        rc_r, out_r = _text(backend, repo, "rev-list", *sorted(reach_refs))
        if rc_r != 0:
            # UNREADABLE REACH IS NOT AN EMPTY REACH. Falling through with an
            # empty set would silently promote every missing commit to a
            # MEASURED absence and refuse the fold on a question we failed to
            # ask, so the whole rung shrugs instead.
            return Rung("cars-reviewed", UNKNOWN,
                        "%d reviewed tip(s) could not be walked for their "
                        "ancestors, so what those rows may already cover is "
                        "unmeasured" % len(reach_refs))
        reach = set(out_r.split())
    blind, dated = [], []
    for sha in missing:
        rc_t, when = _text(backend, repo, "show", "-s", "--format=%ct", sha)
        try:
            ctime = int(when.strip()) if rc_t == 0 else 0
        except ValueError:
            ctime = 0
        # A commit whose own date cannot be read is treated as possibly
        # covered — unreadable is not evidence against.
        (blind if (sha in reach or not ctime or ctime <= blind_until)
         else dated).append(sha)
    if blind:
        return Rung("cars-reviewed", UNKNOWN,
                    "%d folded commit(s) matched no reviewed range, and %d of "
                    "them (%s%s) are either inside a reviewed tip's ancestry or "
                    "predate the newest of %d dispatch row(s) that yielded "
                    "nothing to match against — one of those may cover this "
                    "work, so it is not proof of an unreviewed car"
                    % (len(missing), len(blind),
                       ", ".join(s[:12] for s in blind[:4]),
                       "" if len(blind) <= 4 else ", +%d more" % (len(blind) - 4),
                       unmeasured))
    # THIS ARM ONCE REFUSED AND IT HAD NO RIGHT TO. A cross-family placement
    # ruling (row 48eb1ddffc4248ef, 2026-09-09): NO SOUND FOLD-TIME REFUSAL CAN
    # COME FROM A PATCH-ID MISS AFTER A REWRITE. `git patch-id` hashes CONTEXT
    # lines, so a car cherry-picked under other cars that touched its files
    # re-keys and misses — and the store has said so at confidence 1.00 since
    # before this rung existed: a MISS proves only that no commit carries that
    # EXACT diff, never that the work was unreviewed. Measured on the live
    # composed train: FOUR commits refused, and all four were content-identical
    # to their reviewed lane commits (same files, same insertion and deletion
    # counts, differing only in index lines and hunk headers). Every accusation
    # this arm made was false, and the failure SCALES WITH COMPOSITION DEPTH —
    # worst on exactly the folds it exists to protect.
    #
    # SO IT REPORTS AND DOES NOT JUDGE. The commits are named, because naming
    # them is the whole remaining value. What CANNOT be said from here is that
    # they were unreviewed — AND THE REMEDIATION LINE MUST NOT PRESCRIBE A
    # COMPARISON THIS RUNG CANNOT SET UP. It used to read "Diff each against
    # its lane commit to settle it", which reads as a one-command fix; but the
    # rung emits folded SHAs and counts only, and the source-row/source-commit
    # mapping that would name the lane side is precisely the provenance the
    # paragraph below says does not exist. Found by a cross-family read (row
    # 5bf9eef37584496b, independent code trace) on the same pass that caught
    # the guard placement: the message now says the mapping is unavailable, so
    # the operator learns what they must supply instead of being sent after a
    # comparison they cannot derive. Ambiguous multi-lane matches are not
    # settleable from here either, and saying so is the honest surface.
    #
    # WHAT A SOUND REFUSAL WOULD NEED, per the same ruling, so this is a stated
    # gap and not a shrug: composer-produced provenance — source row, source
    # commit, result commit — verified by replaying the pick onto the actual
    # parent and comparing TREES, with conflict and manual cases falling back
    # to an exact review of the composed tip. Author/date/subject is heuristic
    # and was rejected. task/987 closes when the FOLD REQUIRES that manifest,
    # so that a green gate and the word LANDABLE in chat cannot bypass it.
    return Rung("cars-reviewed", UNKNOWN,
                "%d folded commit(s) matched no reviewed range (%s%s) and are "
                "outside every reviewed tip's ancestry — REPORTED, NOT "
                "REFUSED: a patch-id miss cannot tell an unreviewed commit "
                "from a car whose context moved under a rewrite. THIS RUNG "
                "CANNOT NAME THE LANE COMMIT TO COMPARE AGAINST — it has no "
                "source-row or source-commit mapping (the composer provenance "
                "task/987 is about), so settling one of these means locating "
                "its lane by hand. %d row(s) yielded no derivable range"
                % (len(dated), ", ".join(s[:12] for s in dated[:4]),
                   "" if len(dated) <= 4 else ", +%d more" % (len(dated) - 4),
                   unmeasured))


def _origin_has_it(backend, repo, tip, remote, branch, snapshot, landed):
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
        # THE REFUSAL IS RIGHT AND THE REASON WAS NOT. Landed work must still
        # not be folded, so the verdict does not soften to PASS; what changes
        # is that this rung stops asserting the push was never accepted about
        # a change that plainly arrived. Three answers, because `landed` is a
        # tri-state and collapsing it is the whole defect.
        # ASKED HERE AND ONLY HERE on this path — see check()'s supplier note.
        content = landed()
        if content is True:
            return Rung("origin-has-it", REFUSE,
                        "%s is not on %s BY SHA, but its change already "
                        "reached it under a rebased sha — this lane HAS "
                        "LANDED and there is nothing left to fold. Do NOT "
                        "rebase or re-gate it; close its row instead"
                        % (tip[:12], ref))
        if content is None:
            return Rung("origin-has-it", REFUSE,
                        "%s is NOT on %s by sha, and whether its change "
                        "reached %s under another sha could NOT be measured "
                        "— do not announce this land" % (tip[:12], ref, ref))
        return Rung("origin-has-it", REFUSE,
                    "%s is NOT on %s — do not announce this land; the push "
                    "has not been accepted" % (tip[:12], ref))
    return Rung("origin-has-it", UNKNOWN,
                "cannot compare %s to %s" % (tip[:12], ref))


def gate_authority(repo, tip, gate_ref):
    """Re-derive one receipt's authority for one tip without running a fold.

    The approval reader and the five-rung fold must not invent parallel receipt
    semantics. Both enter the same tree-vs-receipt rung; this wrapper only owns
    backend selection so callers do not reach through the VCS seam themselves.
    """
    return _tree_matches_gate(vcs.backend(repo), repo, tip, gate_ref)


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
    # AND THE CONTENT QUESTION IS SHARED FOR THE SAME REASON THE FETCH IS.
    # Rungs 3 and 5 both need to know whether this change already reached
    # trunk, and each of them asked independently — two runs of a MUTABLE
    # query at two different moments, which is the identical defect the
    # paragraph above describes for the fetch, reintroduced one query over. A
    # concurrent fold between the two calls makes one rung say ALREADY LANDED
    # while the other says trunk moved, inside a single report.
    #
    # A SUPPLIER, NOT A VALUE, AND THE DIFFERENCE IS NETWORK. My first cure
    # computed it eagerly here on the strength of a cost measurement — 22ms
    # worst case, 2-3ms ordinary — that only ever sampled tips which RESOLVE.
    # For an UNREADABLE tip landreq._landing_proof then fell through to
    # _vanished_proof, which ran `git ls-remote origin`: so eager evaluation
    # made `foldcheck --no-fetch` contact the remote and block on it to compute
    # an answer both rungs then discard, and did avoidable work after a FAILED
    # fetch too. --no-fetch exists so this can run without a network; an
    # unconditional call takes that guarantee away. Caught by a cross-family
    # read, from the same reader whose earlier round produced the race this
    # supplier fixes. The ladder no longer takes that leg for an object it
    # cannot resolve, and the supplier stays: an answer no rung reads is work
    # nobody should pay for.
    #
    # Memoised on first use, so the one-moment property is preserved exactly —
    # whichever rung asks first fixes the answer for both — while a report that
    # never reaches a NOT_ANCESTOR branch never asks at all.
    _content = {}

    def landed():
        if "v" not in _content:
            _content["v"] = _landed_by_content(backend, repo, tip, trunk_ref)
        return _content["v"]

    return [
        _tip_exists(backend, repo, tip),
        _tree_matches_gate(backend, repo, tip, gate_ref),
        _ff_able(backend, repo, tip, trunk_ref, snapshot, landed),
        _clean(backend, repo, tip),
        _origin_has_it(backend, repo, tip, remote, branch, snapshot, landed),
        # `_cars_reviewed` IS DELIBERATELY NOT WIRED HERE YET — see its
        # docstring's closing section. It is written, it runs, and it cannot
        # currently REFUSE, so wiring it would add a rung that reports "not
        # proven" on every fold: alarm fatigue that degrades the five rungs
        # that do work, in exchange for coverage it does not actually provide.
        #
        # THE COMPOSITION PROOF IS NOT A SIXTH RUNG, deliberately. It lives in
        # `foldcompose.composition_proof` and the FOLD ENTRY invokes it. This
        # five-rung shape is a PUBLIC CONTRACT other lanes assert on — eleven
        # arms count these rungs or say "all five" — and quietly making it six
        # turned every one of them red in a single gate. A stage with a
        # different lifecycle (it is not invoked at all for a tip out of
        # scope) does not belong inside a list whose length is a promise.
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
