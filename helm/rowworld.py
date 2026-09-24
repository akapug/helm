"""Read the artifacts ONCE and hand `helm.rowstate.derive` an immutable World.

WHY THE READS LIVE HERE AND NOT IN THE DERIVATION. Every question a row asks is
SET-SHAPED — "is this sha on trunk", "is this patch-id on trunk", "does a
receipt bind this head" — and the naive shape asks each one per row, spawning
git a thousand times. `landreq.project()` costs ~90s on this estate for exactly
that reason and the owner's console card became unusable. So the whole
population's evidence is gathered in a fixed number of spawns that does not grow
with the row count, and `derive()` then answers every row out of dicts.

THE ONE EXPENSIVE READ, AND THE TRICK THAT MAKES IT AFFORDABLE. Content identity
needs a patch-id per commit, and `landreq._patch_id` spawns git twice per commit
— measured 13.5 ms each, so 600 trunk commits is 8.1s. But `git patch-id` reads
a STREAM: one `git log -p` piped through one `git patch-id --stable` produces
every id in a single pass. Measured on this repo 2026-08-05: 1.66s for 600 trunk
commits and 1.65s for all 287 commits across 138 lane branches, against ~44s for
the per-branch `vcs.landed_state` sweep the same answers would otherwise cost.

BOTH STAGES ARE JUDGED. The pipe is built with two captured calls rather than a
shell pipeline, because a shell pipeline reports only its LAST stage's status —
`git log` failing into a happy `git patch-id` exits 0 and yields an empty index,
which reads as "no work has landed" and is a false negative on every row.
"""

import os
import re
import sys

from . import gate
from . import dispatches
from . import projscope
from . import rowstate
from . import vcs

TRUNK_REF = "origin/main"
PATCH_SCAN_CAP = 600   # trunk commits searched for content identity
MESSAGE_SCAN_CAP = 1500  # trunk commits searched for dispatch-id bindings

# `dispatch <id>` and `chain <id>` in the FOLD GRAMMAR are the integrator's own
# binding of a trunk commit to a ledger row. Matched on the ROW ID only — a lane
# NAME is a substring and matches every sibling round of a chain, which is how a
# parent whose successor landed gets miscounted as landed itself. Where the
# token may appear is `_trunk_tokens`' law (blocker 12, narrowed by task/744
# T5): only under the FOLD GRAMMAR — the subject of a fold-declaring commit,
# or the body of one. A subject line is not itself a grammar.
_TOKEN = re.compile(r"\b(?:dispatch|chain)\s+([0-9a-f]{8,64})\b")
_FOLD_SUBJECTS = ("fold:", "land:")
_OID = re.compile(r"[0-9a-f]{40}\Z")


class World(object):
    """Immutable artifact snapshot for ONE repository. Attributes only."""

    __slots__ = ("gitdir", "trunk_ref", "trunk_shas", "trunk_trees",
                 "trunk_patch_ids", "trunk_tokens", "branches",
                 "branch_commits", "commit_patch_ids", "commit_trees", "rows",
                 "receipts_by_head", "approvals", "authors", "families",
                 "patch_scan", "unavailable", "trunk_index",
                 "trunk_cts", "carriers", "merge_bases", "trunk_reverts",
                 "receipts_unavailable", "receipts_skipped",
                 # A FAILED READ IS NOT AN EMPTY RESULT (task/744, T4). Each
                 # of these three scans used to drop its error on the floor,
                 # so the map it failed to fill was indistinguishable from a
                 # map with nothing to put in it — and every consumer read
                 # that silence as a negative fact about the repository.
                 "reverts_unavailable", "lane_scan_unavailable",
                 "branch_walk_failed",
                 # T15: the one AFFIRMATIVE artifact. `carriage` is
                 # {lane ref: True/False/None} — does trunk HEAD still carry
                 # that lane's work, by replaying it onto HEAD and asking
                 # whether the result IS HEAD. None means unaskable, which
                 # stays UNKNOWN rather than degrading to historical
                 # inference. `head_tree` is the path-bound map beside it.
                 "head_tree", "carriage",
                 # Rows that name NO repository. Present so they stay
                 # visible, named so their state cannot be derived from
                 # THIS repository's artifacts (review addendum 4).
                 "unscoped")

    def __init__(self, **kw):
        for name in self.__slots__:
            setattr(self, name, kw.get(name))

    def __repr__(self):
        return ("World(%s rows=%d branches=%d trunk=%d patch-ids=%d "
                "tokens=%d families=%s)"
                % (self.trunk_ref, len(self.rows or {}), len(self.branches or {}),
                   len(self.trunk_shas or ()), len(self.trunk_patch_ids or {}),
                   len(self.trunk_tokens or {}),
                   len(self.families or {}) if self.families else 0))


# Reads are bulk, so they get a bulk budget: the 600-commit `log -p` is ~1.6s
# and the seam's 30s default would turn a slow disk into a silent empty index.
_READ_TIMEOUT = 180


def _git(gitdir, *args, stdin=None):
    """-> (rc, stdout). Every git read in this module goes through the vcs
    seam, so there is no direct spawn here to audit: `helm/vcs.py` is where git
    is spelled, and `text(..., stdin=...)` exists precisely so `git patch-id`
    can be a normal call instead of a shell pipeline. A failed read reads as a
    non-zero rc with empty output and the caller decides what that means.

    THE SCRUB IS THE POINT OF ROUTING EVERY READ THROUGH ONE FUNCTION
    (task/744, T13). `git -C <dir>` does NOT win against an
    ambient `GIT_DIR`: the environment selects the repository and the `-C`
    only sets the working directory. Measured on a two-repo probe —
    `repo_identity(A)` returned `B/.git` with `GIT_DIR=B` — which means an
    explicit `--repo A` derived repository B's rows and called them A's,
    and every artifact under it belonged to the wrong repository.

    `dispatches` has scrubbed this since the write side of the mapping was
    found to be the identity; the read side was left exposed. The variable
    list is IMPORTED from there rather than retyped, so a ninth selection
    variable added to git cannot be honoured in one module and forgotten in
    the other — the same reason `_CLOSE_STATE_FIELDS` drives both the replay
    arm and the writer."""
    rc, out, _err = vcs.backend(gitdir).text(
        gitdir, *args, timeout=_READ_TIMEOUT, env=_scrubbed_env(),
        stdin=stdin.encode() if isinstance(stdin, str) else stdin)
    return rc, out


def _git_bytes(gitdir, *args, stdin=None):
    """-> (rc, stdout BYTES). The byte-preserving door beside `_git`.

    `_git` goes through `vcs.text`, which fsdecodes and STRIPS — correct for
    a sha or a ref listing, and destructive for a PATCH. The patch-id pipeline
    is the one caller here whose input is content rather than a record: `git
    log -p` emits the diff text, and stripping it removes exactly the trailing
    whitespace that `--verbatim` exists to hash. Measured 2026-08-08 while
    building the T15 arm — two commits differing only by a trailing space came
    back with IDENTICAL verbatim ids, because the space never survived the
    seam. Switching the hash to `--verbatim` without this would have looked
    like a fix and changed nothing.
    """
    rc, out, _err = vcs.backend(gitdir).run(
        gitdir, *args, timeout=_READ_TIMEOUT, env=_scrubbed_env(),
        stdin=stdin)
    return rc, out


def _history_view_env():
    """THE HISTORY VIEW EVERY WITNESS READ IS PINNED TO — both rewriters a
    reader CAN switch off, in one overlay. -> env overlay

    `refs/replace/<oid>` SWAPS ONE OBJECT FOR ANOTHER AT EVERY LOOKUP, so `git
    cherry`, `rev-parse` and `merge-tree` all reinterpret the same immutable
    ids: replacing the landed twin whose patch identity IS the match turns a
    uniformly `-` range into `+` while every id in the question is unchanged.
    GIT TESTS `GIT_NO_REPLACE_OBJECTS`'s PRESENCE, NEVER ITS VALUE, so `0`
    disables replacement exactly as `1` does; `1` is what a reader expects "on"
    to say.

    AND `--no-replace-objects` DOES NOT COVER A LEGACY GRAFTS FILE, which is a
    separate mechanism that rewrites parentage outright — measured on git 2.53.0
    beside `landreq._object_view`, which is where the `GIT_GRAFT_FILE` spelling
    and that measurement live. It is IMPORTED rather than retyped: a third copy
    of one history view is how two readers come to disagree about what an id
    means, and the witnesses in `dispatches.carriage_proof` and the object reads
    in `landreq` must agree exactly, because one authorizes the close the other
    measures.

    THE THIRD REWRITER HAS NO OVERLAY AT ALL. A `.git/shallow` boundary makes
    the parent objects genuinely absent, so it is REFUSED rather than disabled:
    `dispatches._carriage_shallow_refusal` answers UNKNOWN for a shallow
    repository. The gate authority pins the replacement variable for the same
    reason as the first paragraph (`gateauthority`, `gateimport`), on a
    different env base.
    """
    from . import landreq               # DEFERRED — landreq imports dispatches.
    return dict(landreq._NO_GRAFTS, GIT_NO_REPLACE_OBJECTS="1")


def _scrubbed_env():
    """The OVERLAY EVERY GIT READ IN THIS MODULE RUNS UNDER. Two properties.

    (1) IT REMOVES GIT'S REPOSITORY-SELECTION VARIABLES. `vcs.run`'s `env`
    overlays the ambient environment rather than replacing it — deliberately,
    because git needs HOME/PATH/GIT_CONFIG_*. So a dict that merely OMITS
    `GIT_DIR` removes nothing, and the first cut of this fix did exactly that
    and measured green while changing nothing. A `None` value is the seam's
    spelling for "remove this one" (helm/vcs.py `_spawn`).

    The authority for WHICH variables is `dispatches._GIT_SELECTION_ENV`,
    imported rather than retyped: two copies of a security-relevant list
    drift, and the drift is invisible — the read side would keep honouring a
    variable the write side had learned to fear.

    (2) IT PINS THE HISTORY VIEW (`_history_view_env` above): replacement
    objects off AND grafts off, for EVERY read here rather than for one
    witness. Two witness families that describe different histories are not
    comparable, and the one-directional fallthrough between them assumes they
    are. Both now describe the objects their ids name.

    IT IS NOT THE WHOLE HISTORY VIEW AND DOES NOT CLAIM TO BE. A
    `.git/shallow` boundary truncates every traversal and no overlay restores
    it, because the parent objects are absent rather than hidden. That one is
    REFUSED instead of disabled, by `dispatches._carriage_shallow_refusal`."""
    scrub = {name: None for name in dispatches._GIT_SELECTION_ENV}
    scrub.update(_history_view_env())
    return scrub


def _patch_ids(gitdir, log_args, mode="--verbatim"):
    """{commit sha: patch-id} in TWO spawns for any number of commits.

    -> (index, err). `git log -p` writes a stream of patches each headed by its
    `commit <sha>` line; `git patch-id --stable` consumes that stream and emits
    `<patch-id> <commit sha>` per patch. Merge commits are excluded because
    patch-id has nothing to say about them — their absence from the index is what
    makes `rowstate._landed` refuse to call a lane landed on a merge it cannot
    read, rather than silently skipping it.

    THE EXPLICIT PREFIXES ARE LOAD-BEARING, NOT STYLE. `git patch-id` hashes the
    diff TEXT, so the `a/` `b/` path prefixes are part of its input. Measured:
    one commit hashing to two DIFFERENT ids under `diff.noprefix=false` and
    `diff.noprefix=true` (helm room, 2026-08-05; the two ids are recorded there
    and deliberately not copied here — a patch-id is a content hash that no
    `cat-file` can resolve, so pasting one into permanent text asserts an
    existence nobody can ever check).

    `diff.noprefix=true` is a common dotfile setting, so without this pin an
    index built on one box is silently incomparable with one built on another:
    false not-landed here, false drift-refusal in compose, and unmatchable
    stored ids in land receipts that cross boxes.

    Pinning `--src-prefix`/`--dst-prefix` on the SUBCOMMAND beats
    `-c diff.noprefix=false` because it needs nothing of how the vcs seam
    composes pre-subcommand args, so a seam change cannot defeat it. Measured
    stable across both config values on two different commits, with three
    unrelated commits confirmed distinct.

    `--no-textconv` / `--no-ext-diff` / `--binary` PIN THE REMAINING DIFF
    SEMANTICS (task/744, addendum 5). A repository can declare
    `diff=<driver>` in `.gitattributes` and point that driver at a textconv
    or an external diff, and `git log -p` will then hash the DRIVER'S OUTPUT
    instead of the file's bytes — so the content of a patch-id becomes a
    property of the checkout's config rather than of the work. Measured here
    2026-08-08 with a masking textconv: the same two commits produce NO PATCH
    AT ALL through the default spelling and a real, distinct id under
    `--no-textconv`. (A weaker outcome than another probe's, which had two
    byte-distinct commits COLLIDING — but it demonstrates the same live axis:
    the diff text this module hashes was config-controlled.) `--binary`
    closes the sibling hole where a binary file's diff degrades to the prose
    "Binary files differ", which is identical for every possible content.

    `--no-renames` PINS THE SECOND CONFIG AXIS the same way (review
    defect 15): a rename-plus-edit commit prints `rename from/rename to`
    under `diff.renames=true` and a delete-plus-add pair under `false`, two
    DIFFERENT diff texts and therefore two different ids for one commit.
    Rename detection off is the deterministic spelling — every box hashes
    the same bytes regardless of dotfiles.

    `--verbatim` IS THE THIRD AXIS, AND IT IS THE ONE THAT DECIDED LANDINGS
    (task/744, T15). `--stable` deliberately normalises
    whitespace so a patch keeps its id across reformatting — a virtue when
    you are tracking a patch through rebases, and a hole when the id is being
    spent as proof that trunk carries this exact work. Measured here
    2026-08-08 on a scratch repo, two commits differing only by a trailing
    space (`"ADDED \n"` vs `"ADDED\n"`): the `--stable` ids are EQUAL and
    the `--verbatim` ids DIFFER. Under `--stable` a lane whose content had
    been changed in review derived LANDED against the version that actually
    landed. Content identity means BYTES, so the default is `--verbatim` and
    a caller wanting the looser hash has to name it."""
    rc, patches = _git_bytes(gitdir, "log", "-p", "--no-merges", "--no-renames",
                             "--no-textconv", "--no-ext-diff", "--binary",
                             "--src-prefix=a/", "--dst-prefix=b/",
                             "--format=commit %H", *log_args)
    if rc != 0:
        return {}, "git log failed while indexing patch-ids"
    if not patches:
        return {}, None
    rc, listing = _git(gitdir, "patch-id", mode, stdin=patches)
    if rc != 0:
        return {}, "git patch-id failed while indexing patch-ids"
    index = {}
    for line in listing.splitlines():
        parts = line.split()
        if len(parts) == 2:
            index[parts[1].strip().lower()] = parts[0].strip().lower()
    return index, None


def repo_identity(path):
    """The COMMON git directory's realpath — the identity dispatch rows store.
    -> (gitdir, err)

    `--absolute-git-dir` names the PER-WORKTREE gitdir —
    `.git/worktrees/<name>` — which no dispatch row has ever stored:
    `dispatches._repo_info` stamps `repo_id` from `--git-common-dir` through
    `realpath`. Measured live 2026-08-07 from a helm worktree: 1,820 rows
    carry the common `.git` and ZERO carry a worktree gitdir, so a snapshot
    filtered on the worktree gitdir derives the WRONG POPULATION — six
    repo-less legacy rows and nothing else. Canonicalize BEFORE filtering,
    with the same realpath the writer used."""
    rc, out = _git(path, "rev-parse", "--path-format=absolute",
                   "--git-common-dir")
    common = out.strip()
    if rc != 0 or not common:
        return None, "%s is not a git repository this box can read" % path
    return os.path.realpath(common), None


def _rows_for(rows, gitdir):
    """The rows that BELONG to `gitdir` — plus repo-less legacy rows, which
    predate the field and cannot be excluded without inventing a claim.

    INCLUDING THEM WAS ALSO A CLAIM (task/744, addendum 4),
    and nobody had noticed which way it pointed. A row with no `repo_id`
    enters EVERY repository's snapshot, and once inside it is judged against
    THAT repository's artifacts — a probe showed it: repo A carries `fold:
    dispatch <legacy-id>`, a notionally foreign legacy row lands in A's
    snapshot, and A's own id-binding derives LANDED for work that may have
    nothing to do with A. Excluding them invents "this row is not ours";
    including them silently invented "this row is ours", which is the one
    that mints confidence.

    So they are still included — a row nobody can place must stay VISIBLE —
    and `_unscoped` names them, so `rowstate` can refuse to spend this
    repository's artifacts on a row that never claimed to be in it."""
    return {rid: row for rid, row in (rows or {}).items()
            if isinstance(row, dict)
            and (not row.get("repo_id") or row.get("repo_id") == gitdir)}


def _unscoped(rows):
    """The ids of rows that name no repository at all. See `_rows_for`."""
    return frozenset(rid for rid, row in (rows or {}).items()
                     if isinstance(row, dict) and not row.get("repo_id"))


def _trunk_tokens(gitdir, trunk_ref, cap):
    """{id token: trunk sha} harvested from the FOLD GRAMMAR. One spawn.

    %B ACROSS ARBITRARY COMMITS WAS A FALSE BINDER (blocker 12): prose quotes
    rows it does not land. Measured 2026-08-07 over this repository's full
    trunk: 19 of 198 body-token hits were citations of REVIEW ROUNDS
    ("@codex-2's FIX on dispatch …", "Round 7 (dispatch …, tip …) found the
    same fail-open"), each of which would have LANDED a reaped row it merely
    mentioned. The integrator's real bindings live in the SUBJECT LINE (122
    `fold:` subjects plus one `merge … for land (dispatch …)`) or in the BODY
    of a commit whose subject DECLARES the fold — `fold:` / `land:`, the
    per-lane listings of a batched land.

    A SUBJECT LINE IS NOT A GRAMMAR (task/744, T5). Blocker 12 fixed the body
    and left the subject binding on ANY commit, so `docs: explain dispatch
    <rowid>` landed the row it was explaining, and any commit message that
    names a row in its first line lands it. The measurement that licensed
    trusting subjects said something NARROWER than the rule drawn from it: it
    counted 122 `fold:`/`land:` subjects and ONE merge-for-land, which is a
    fact about the FOLD GRAMMAR, not about subject lines.

    Re-measured 2026-08-08 over this repository's FULL trunk rather than the
    capped window: of every subject carrying a token, 122 are `fold:`/`land:`,
    1 is the merge-for-land, and ZERO are anything else. (The two non-zero
    counts are the must-hit that makes the zero readable — a probe that found
    nothing anywhere would have produced the same reassuring 0.) Requiring the
    grammar in the subject too therefore costs no real binding.

    So: a token binds only under the FOLD GRAMMAR — in the subject of a
    fold-declaring commit, or in the body of one."""
    rc, listing = _git(gitdir, "log", "--format=%H%x1e%B%x1f", "-n",
                       str(int(cap)), trunk_ref)
    if rc != 0:
        return {}
    tokens = {}
    for chunk in listing.split("\x1f"):
        sha, sep, body = chunk.partition("\x1e")
        if not sep:
            continue
        sha = sha.strip().lower()
        subject, _nl, rest = body.lstrip("\n").partition("\n")
        if not _declares_fold(subject):
            continue
        for match in _TOKEN.finditer(subject):
            tokens.setdefault(match.group(1), sha)
        for match in _TOKEN.finditer(rest):
            tokens.setdefault(match.group(1), sha)
    return tokens


def _declares_fold(subject):
    """Does this subject line DECLARE a land, in the integrator's grammar?

    Two spellings, both measured on trunk: the ordinary `fold:` / `land:`
    prefix, and the one merge shape — `merge <sha> into <lane> for land
    (dispatch …)`. The merge form is matched on `merge` AND `for land`
    together rather than on the phrase alone, so a subject that merely
    contains the words does not qualify."""
    subject = subject.strip()
    if subject.startswith(_FOLD_SUBJECTS):
        return True
    return subject.startswith("merge ") and " for land" in subject


def _head_tree(gitdir, trunk_ref):
    """{path: (mode, type, oid)} for CURRENT trunk head. -> (map|None, err)

    THE ONE AFFIRMATIVE ARTIFACT (task/744, T15). Every other
    landing proof this module gathers is HISTORICAL: a patch-id says the diff
    once appeared, a tree hash says a commit once matched, a fold subject says
    an integrator once named the row. None of them survives a later ordinary
    edit — no revert, no marker, just somebody changing the same lines — so
    "it appeared and I know of no revert" is ABSENCE OF CONTRADICTION, never
    current carriage.

    `ls-tree -r` of HEAD is the thing that can actually say YES: git object
    identity makes the comparison byte-exact and path/mode-bound, and one
    scan answers every lane, so the cost is O(total touched paths) rather
    than O(HEAD files per lane).

    None (not {}) on a failed read — an unread tree is not an empty one.

    BOUGHT ONCE PER PROJECTION, AND THE PARSE IS THE HALF NO SPAWN COUNT SEES.
    `ls-tree` owns no mutating subcommand but is deliberately not in
    `vcs._READ_VERBS`, so the seam's memo cannot collapse it; and the seam
    would only collapse the SPAWN, while this function's cost is the spawn
    PLUS building a dict over every path in trunk. MEASURED on one cold
    `helm lr list`: 198 calls, ONE distinct argv — the whole off-frontier
    census re-reading and re-parsing the same trunk tree once per row.

    OUTSIDE A `projscope.scope()` THIS READS AND PARSES ON EVERY CALL, which
    is the contract the memo carries everywhere in helm and is why a write
    path can share this door. The map is READ-ONLY to its callers
    (`_postimages_at_head` only looks paths up); a caller that mutated it
    would be mutating every later reader's copy inside the scope."""
    return projscope.memo(("rowworld._head_tree", gitdir, str(trunk_ref)),
                          lambda: _head_tree_read(gitdir, trunk_ref))


def _head_tree_read(gitdir, trunk_ref):
    """The unmemoised read and parse — split out ONLY so the door above has
    something to call, the same split `landreq._git_spawn` is."""
    rc, out = _git_bytes(gitdir, "ls-tree", "-r", "-z", trunk_ref)
    if rc != 0:
        return None, "could not read the tree of %s" % trunk_ref
    tree = {}
    for record in out.split(b"\0"):
        if not record:
            continue
        meta, _tab, path = record.partition(b"\t")
        parts = meta.split()
        if len(parts) != 3 or not path:
            continue
        mode, kind, oid = parts
        tree[os.fsdecode(path)] = (os.fsdecode(mode), os.fsdecode(kind),
                                   os.fsdecode(oid).lower())
    return tree, None


def _nonempty_delta(gitdir, base, tip):
    """Is there any WORK between base and tip? -> True / False / None

    THE DELTA MUST BE NONEMPTY BEFORE ANY WITNESS SPEAKS (task/756).
    Both witnesses below are vacuous on an empty one: an empty replay
    reproduces whatever HEAD it is handed, and an empty touched-path set is
    satisfied by every HEAD trivially. So the emptiness check is not an
    optimisation, it is the precondition that makes the affirmations mean
    anything.

    TWO SPELLINGS OF EMPTY, both with live specimens. `base == tip` is the
    obvious one: row `8b0c8110` (a2h, authored in another repository) had a
    chain with no build row, so its base fell back to its OWN reviewed tip,
    and the empty replay affirmed carriage against THIS repository's trunk —
    where its work has never been. `tree(base) == tree(tip)` is another, and
    it slips past the first: an EMPTY REVIEWED COMMIT has a different id and
    the SAME TREE, and it affirmed carriage against an unrelated HEAD too.
    A pair is only askable when the two commits differ AND their trees do."""
    if not base or not tip or base == tip:
        return False
    trees = []
    for sha in (base, tip):
        rc, out = _git(gitdir, "rev-parse", sha + "^{tree}")
        if rc != 0 or not out.strip():
            return None
        trees.append(out.strip().lower())
    return trees[0] != trees[1]


def _postimages_at_head(gitdir, base, tip, trunk_ref):
    """WITNESS 1: does HEAD carry this delta's exact touched-path postimages?
    -> True / False / None

    For every path the delta touches: an addition, modification or rename
    DESTINATION must be present at HEAD with the EXACT postimage mode, type
    and object id; a deletion or rename SOURCE must be absent. `--no-renames`
    turns a rename into that delete-plus-add pair, which is the shape this
    rule wants and avoids a similarity threshold that is a config axis.

    FALSE HERE IS NOT ABSENCE (a review ruling). A mismatch says only that a
    LATER FOLLOW-ON may have touched the same path — measured live: a
    rebase-landed commit's block is byte-identical on trunk while the FILE's
    blob differs, because the file also carries unrelated drift. So the
    caller treats a mismatch as "this witness did not affirm", never as
    "trunk does not carry it".

    AND IT IS NOT A CHEAP PREDICTOR OF `_replay_is_a_noop`, which is the
    saving a reader of the cost numbers reaches for: this witness is about
    5ms and that one about 140ms, so a False here looks like a licence to skip
    the merge. It is not. Measured over the whole off-frontier census on a
    copy of the live ledger, this witness answered False for all 141 rows that
    reached the replay AND the replay affirmed one of them — trunk had since
    edited one of that row's three touched paths, so the postimage was gone
    while the three-way merge still reproduced trunk exactly. That is the
    carried-THEN-edited state the replay is the only witness for, and a filter
    on this answer drops it. The arm is
    `tests.test_lr_close.CarriedRebaseLandedTest`.
    THE TREE COMES FROM `_head_tree`, NOT FROM A SECOND COPY OF IT. This
    function carried its own `ls-tree -r -z` plus its own parse loop, and the
    two were byte-identical in effect — same argv, same `\0` split, same
    three-field filter, same lowercased oid — so one map had two
    implementations and only one of them could ever be bought once. MEASURED
    on one cold `helm lr list`: 198 `ls-tree` calls for ONE
    distinct argv, and memoising the other door alone moved none of them,
    because every one came through here. The duplicate also hid the half no
    spawn count sees: the parse of every path in trunk, run once per row."""
    head, _err = _head_tree(gitdir, trunk_ref)
    if head is None:
        return None
    rc, raw = _git_bytes(gitdir, "diff", "--raw", "-z", "--abbrev=40",
                         "--no-renames", base, tip)
    if rc != 0:
        return None
    fields, i, touched = raw.split(b"\0"), 0, 0
    while i + 1 < len(fields):
        meta = fields[i]
        if not meta.startswith(b":"):
            i += 1
            continue
        path = os.fsdecode(fields[i + 1])
        i += 2
        parts = meta[1:].split()
        if len(parts) < 5 or not path:
            continue
        touched += 1
        mode, oid = os.fsdecode(parts[1]), os.fsdecode(parts[3]).lower()
        status = os.fsdecode(parts[4])
        if status.startswith("D"):
            if path in head:
                return False
        elif head.get(path) != (mode, "blob", oid):
            return False
    if not touched:
        return None                    # an empty touched set affirms nothing
    return True


def _replay_is_a_noop(gitdir, base, tip, trunk_ref):
    """WITNESS 2: does replaying this delta onto HEAD reproduce HEAD exactly?
    -> True / False / None

    Applying the change and changing nothing is what "already carried" means.
    A CONFLICT IS NOT ABSENCE (a review ruling, and two measured specimens
    prove it): a rebase-landed delta conflicts BECAUSE HEAD already carries
    the change, and an adjacent follow-on conflicts while retaining it. So a
    conflict answers False to THIS witness and the caller reads that as
    silence, not as a negative."""
    rc, out = _git(gitdir, "merge-tree", "--write-tree",
                   "--merge-base=" + base, trunk_ref, tip)
    if rc == 1:
        return False                   # conflict: this witness cannot speak
    if rc != 0:
        return None
    produced = out.splitlines()[0].strip().lower() if out.strip() else ""
    rc_head, head_tree = _git(gitdir, "rev-parse", trunk_ref + "^{tree}")
    if rc_head != 0 or not head_tree.strip():
        return None
    return produced == head_tree.strip().lower()


def _carriage(gitdir, base, tip, trunk_ref):
    """Does CURRENT trunk head carry this work? -> True / False / None

    AN OR OF AFFIRMATIVE WITNESSES OVER A NONEMPTY DELTA (a review ruling,
    task/756). Either witness affirming is enough; NEITHER affirming is
    UNKNOWN, not absence.

    WHY FALSE IS ALMOST NEVER RETURNED, and why that is the fix rather than a
    weakening. The first cut of this relation was replay-only and read a
    conflict as a measured refusal. Measured against the ledger's own
    landed-with-proof rows: 98 of 105 askable came back FALSE — a 93%
    false-negative rate — because this fleet lands work REBASED, and a
    rebase-landed delta conflicts precisely BECAUSE HEAD already carries it.
    A relation that inverts on its main case is worse than no relation, and
    since affirmative carriage gates every door to LANDED, that reached
    `derive` estate-wide.

    So a non-affirming witness now means "this witness could not speak".
    Absence needs its own POSITIVE artifact — a recognized revert — and until
    one exists the honest answer is None. That keeps the guarantee the gate
    was built for (no LANDED without an affirmative witness) while removing
    the claim it could not support (no ABSENCE without evidence of removal)."""
    askable = _nonempty_delta(gitdir, base, tip)
    if askable is None:
        return None
    if askable is False:
        return None                    # nothing to carry: vacuous, not carried
    for witness in (_postimages_at_head, _replay_is_a_noop):
        if witness(gitdir, base, tip, trunk_ref) is True:
            return True
    return None


def _reached_trunk(gitdir, tip, trunk_ref):
    """WITNESS 3: did every commit up to `tip` REACH trunk, under whatever
    sha the land rewrote it to? -> (True | None, unmatched shas | None)

    THERE IS NO False HERE, AND THAT IS A MEASURED CORRECTION rather than
    timidity. The first cut of this returned False on a `+` and the caller's
    refusal said the work "has NOT landed". Measured on a four-commit probe
    2026-08-12: `git cherry main <commit>` reads `-`, and `git cherry main
    <a merge of main into that commit's lane>` reads `+` FOR THE SAME
    COMMIT. `git cherry` marks the right side of a SYMMETRIC DIFFERENCE, so
    once the lane merges trunk the landed twin stops being upstream-only and
    there is nothing left to match against. A `+` therefore proves "this
    instrument could not match it", never "trunk does not have it" — and
    naming the weaker property as the guarantee is the exact failure this
    module's `_carriage` docstring already records for the replay-only cut
    (98 of 105 known-landed rows called FALSE). The gate is unchanged: an
    unmatched commit still REFUSES. Only the sentence is honest now, and the
    shas come back so the refusal can name them.

    IT ANSWERS A DIFFERENT QUESTION FROM `_carriage`, and the difference is
    the whole reason it exists. The two witnesses above ask about trunk
    HEAD's CONTENT — are the postimages still there, does the replay change
    nothing — and any ordinary later edit to the same files makes both of
    them go quiet. `_carriage` is right to stay silent then: it cannot tell
    a land that was later edited from a land that never happened. This one
    asks about trunk's HISTORY instead, which later edits cannot erase: did
    a commit with this patch id ever arrive.

    THIS IS THE STATE OUR OWN PROTOCOL MANUFACTURES. helm rebases before it
    lands, so the reviewed object is reachable from nothing and the landed
    copy carries a different sha. Ancestry answers "no" truthfully and every
    content witness answers "I cannot see"; `git cherry` answers "-" and
    means it. Measured on the live board 2026-08-12, three FIX-verdicted rows
    sat 2 to 14 hours in exactly this state with no door that could admit
    them: 0a36e61fd6cd, 18e895caf600, 158ce57d13fb.

    IT IS STRICTLY STRONGER THAN `_landing_proof`'S PATCH LEG, which is what
    keeps `carried` from becoming a cheaper `landed`. `_landing_proof` asks
    about ONE commit; a row whose top commit cherry-matches while a
    prerequisite beneath it never landed reads `patch-equivalent` there and
    is refused here. The RANGE must be uniformly `-`.

    IT NEEDS NO BASE, and that is what reaches the rows `_work_pair` cannot
    bind: a `--new-work` review row is its own chain root with no build
    parent, so there is no immutable base to replay FROM, and the replay
    witnesses can never be asked about it at all. `git cherry` derives its
    own range from the merge-base, so the tip alone is the whole input.

    EVERY UNRECOGNISED READING IS None, never an answer. An empty listing
    means the range is empty — a tip already reachable from trunk, or an
    unrelated history — and an empty range affirms every possible trunk,
    which is the same vacuity the degenerate-pair rung refuses upstream. A
    line this cannot parse, a tip missing from its own listing (`git cherry`
    SKIPS MERGES, so a merge tip can hand back a uniformly `-` range that
    says nothing at all about the tip it was asked about), or a git that did
    not run are all silence."""
    rc, out = _git(gitdir, "rev-parse", "--verify", "--quiet",
                   str(tip or "") + "^{commit}")
    sha = _sha(out.strip()) if rc == 0 else None
    # 40 or 64 — the sha256 length is NOT hypothetical padding; it is the
    # grammar `dispatches._FULL_TIP` already admits, and pinning 40 here
    # would make this witness silently unaskable in a sha256 repository
    # while every other rung kept working.
    if not sha or len(sha) not in (40, 64):
        return None, None
    # AN ANCESTOR LEAVES THIS WITNESS AN EMPTY RANGE, AND ANCESTRY SAYS SO FOR
    # A FORTIETH OF THE PRICE. `git cherry <trunk> <tip>` marks the right side
    # of a symmetric difference; when `tip` is already reachable from `trunk`,
    # `merge-base(trunk, tip) == tip`, that side is EMPTY, and the loop below
    # reaches `if not lines: return None, None`. So this is the identical
    # answer by the identical argument, not a new refusal: `git cherry` is
    # being asked to compute trunk's whole patch identity set to rediscover an
    # emptiness `merge-base --is-ancestor` reports outright. Same law
    # `landreq._stored_patch_index` states in its own words — ASK ANCESTRY
    # FIRST, THIS IS THE FALLBACK, NOT THE DOOR.
    #
    # MEASURED over the off-frontier census on this board: 124 of
    # the 263 rows reaching here are `landed-by-ancestry`, every one of them
    # answered None, and each paid a 128ms `cherry` to do it; the ancestry
    # probe costs 2.8ms.
    #
    # ONLY rc 0 SHORT-CIRCUITS. `merge-base --is-ancestor` exits 1 for "not an
    # ancestor" and something else for a read that did not happen, and both
    # fall through to the cherry below — an unreadable probe costs one cheap
    # spawn and changes no answer. This witness's silence must never be
    # manufactured by a failed reading.
    rc_ancestor, _ancestor_out = _git(gitdir, "merge-base", "--is-ancestor",
                                      sha, str(trunk_ref or ""))
    if rc_ancestor == 0:
        return None, None              # an empty range affirms every trunk
    rc, out = _git(gitdir, "cherry", trunk_ref, sha)
    if rc != 0:
        return None, None
    lines = out.splitlines()
    if not lines:
        return None, None              # an empty range affirms every trunk
    seen, unmatched = False, []
    for line in lines:
        parts = line.split()
        if len(parts) != 2 or parts[0] not in ("-", "+") \
                or not _sha(parts[1]):
            return None, None          # unrecognised output is not an answer
        if _sha(parts[1]) == sha:
            seen = True
        if parts[0] == "+":
            unmatched.append(_sha(parts[1]))
    if unmatched:
        return None, unmatched
    return (True, None) if seen else (None, None)


def _work_pair(row, rows, carriers):
    """The IMMUTABLE (base, tip) this row's work is bound to. -> (B, T)

    THE PAIR COMES FROM THE LEDGER, NOT FROM REFS (task/744, meld e:1786207689).
    Both halves were wrong before:

    B WAS `merge-base(lane, current trunk)`, and that is a VACUITY rather than
    a looseness. Once the work lands directly, that merge-base BECOMES the
    lane tip — so the replay has NOTHING IN IT, and an empty replay reproduces
    every possible HEAD. The check passed hardest exactly where it could see
    least. B is therefore the base the CHAIN recorded: a build row's own `tip`
    field, which `landreq` stores as the base it was cut from.

    T WAS THE LIVE BRANCH HEAD, which is mutable and belongs to whoever last
    pushed. A build row's own tip cannot stand in for it either — that IS its
    base, and 48 of 52 live build refs are already ancestors of trunk, so
    spending it as the work would affirm carriage for every build row ever
    dispatched. T is the immutable reviewed tip of the row itself when it is a
    review row, or of the CARRIER that took its obligation, or a verified
    same-work retip. A branch head is a candidate for other rungs and never
    terminal authority here.

    NO CARRIER AND NO RETIP MEANS NO PAIR, reaped or not — a review barred a
    reaped-branch exception explicitly, and (None, None) reaches the caller as
    UNKNOWN rather than as any confident word."""
    rows = rows or {}
    rid = str((row or {}).get("id") or "")

    # THE PAIR MUST BE TWO DIFFERENT COMMITS OR IT PROVES NOTHING — see the
    # degenerate-pair rung in `_carriage`. A row whose only available base is
    # its own tip has no immutable WORK bound to it, and the honest answer is
    # no pair at all rather than a pair that affirms everything.
    base = None
    if row.get("kind") == "build":
        base = _sha(row.get("tip"))
    root = str(row.get("chain_root") or "")
    if not base and root and isinstance(rows.get(root), dict):
        base = _sha(rows[root].get("tip"))
    if not base:
        for other in rows.values():
            if isinstance(other, dict) and other.get("kind") == "build" \
                    and str(other.get("chain_root") or "") == (root or rid):
                base = _sha(other.get("tip"))
                break

    tip = None
    if row.get("kind") != "build":
        tip = _sha(row.get("reviewed_tip"))
    if not tip:
        holder = rows.get(str((carriers or {}).get(rid) or ""))
        if isinstance(holder, dict):
            tip = _sha(holder.get("reviewed_tip"))
    if not tip:
        tip = _verified_retip(row)
    if base and tip and base == tip:
        return None, None
    return base, tip


def _verified_retip(row):
    """The tip a VERIFIED retip bound to this row, or None. Mirrors
    `rowstate._retipped_tip` — only the newest entry, only `verified`, and
    only while it still agrees with the row's current tip."""
    retips = (row or {}).get("retips")
    if not isinstance(retips, (list, tuple)) or not retips:
        return None
    latest = retips[-1]
    if not isinstance(latest, dict) or latest.get("identity") != "verified":
        return None
    tip = _sha(latest.get("tip"))
    return tip if tip and tip == _sha(row.get("tip")) else None


def _sha(value):
    text = str(value or "").strip().lower()
    return text if re.fullmatch(r"[0-9a-f]{7,64}", text) else None


def _carriage_by_row(gitdir, rows, carriers, trunk_ref):
    """{row id: True/False/None} — does trunk HEAD carry each row's work?

    Cached per (B, T) because a chain's rounds share a pair, so a 14-hop chain
    costs ONE replay rather than fourteen. A row with no pair is absent from
    the map entirely, which `rowstate` reads as UNKNOWN — and that is the
    reaped-branch answer a review ruled for, not an oversight."""
    seen, out = {}, {}
    for rid, row in (rows or {}).items():
        if not isinstance(row, dict):
            continue
        base, tip = _work_pair(row, rows, carriers)
        if not base or not tip:
            continue
        if (base, tip) not in seen:
            seen[(base, tip)] = _carriage(gitdir, base, tip, trunk_ref)
        out[str(rid)] = seen[(base, tip)]
    return out


def _branch_heads(gitdir):
    rc, listing = _git(gitdir, "for-each-ref",
                       "--format=%(objectname) %(refname:short)", "refs/heads/")
    heads = {}
    for line in listing.splitlines() if rc == 0 else ():
        sha, _, name = line.partition(" ")
        if name:
            heads[name.strip()] = sha.strip().lower()
    return heads


def _commit_trees(gitdir, trunk_ref, wanted):
    """{commit: tree} for trunk plus every explicitly wanted commit. TWO spawns.

    THE GAP THIS CLOSES. A gate receipt binds the head it ran on, and that head
    is routinely NEITHER on trunk NOR a branch head — an approved tip whose lane
    has moved on is reachable only from the receipt. Without its tree,
    `rowstate._judge_receipt` cannot compare receipt tree to commit tree and
    correctly refuses to bind, so a properly gated row read BUILDING. Measured
    on the live estate: land-request row `ce2194293b0d`, whose approved tip a
    rebase has since orphaned — which is why this cites the ROW.

    `cat-file --batch-check` resolves any number of `<sha>^{tree}` requests in
    one pass and answers `<input> missing` for objects git no longer has, which
    is the honest answer for a pruned tip — it stays absent from the map and the
    caller reports the tree as unreadable rather than assuming one."""
    trees, order, dates = {}, {}, []
    rc, listing = _git(gitdir, "log", "--format=%H %T %ct", trunk_ref)
    for line in listing.splitlines() if rc == 0 else ():
        parts = line.split()
        if len(parts) != 3:
            continue
        sha = parts[0].strip().lower()
        trees[sha] = parts[1].strip().lower()
        order[sha] = len(dates)
        dates.append(int(parts[2]))
    todo = sorted({s for s in wanted if s and s not in trees})
    if not todo:
        return trees, order, tuple(dates)
    rc, answered = _git(gitdir, "cat-file", "--batch-check=%(objectname)",
                        stdin="".join(sha + "^{tree}\n" for sha in todo))
    if rc != 0:
        return trees, order, tuple(dates)
    answers = answered.splitlines()
    for sha, answer in zip(todo, answers):
        oid = answer.strip().split(" ")[0].lower()
        if _OID.fullmatch(oid):
            trees[sha] = oid
    return trees, order, tuple(dates)


def _rank_receipt(receipt):
    """Sort key putting the most bindable receipt for a head first."""
    return (receipt.get("status") == "OK", receipt.get("dirty") is False,
            str(receipt.get("ts") or ""))


def _carriers(rows):
    """{row id: the id of the successor that ACTUALLY holds its obligation}.

    `dispatches.carrier` is the ONE owner of the supersession question — it
    walks the successor SET (never the frozen `superseded_by` pointer, which
    names the first successor forever), honors chain identity, and treats
    cancelled/withdrawn/abandoned successors as pass-throughs. Computed here
    once per population, because `rowstate` is pure and must not import the
    ledger; a row with no carrier is simply absent from the map, which is the
    walk's honest stop."""
    index = dispatches._successor_index(rows)
    cycles = dispatches._cycle_components(index)
    out = {}
    for rid, row in (rows or {}).items():
        if not isinstance(row, dict):
            continue
        kid = dispatches.carrier(row, rows, index, cycles)
        if isinstance(kid, dict) and kid.get("id"):
            out[str(rid)] = str(kid["id"])
    return out


def _family_seats(rows, gitdir):
    """Every seat the APPROVED arm needs resolved for THIS repository: the
    approve REVIEWERS and the lane AUTHORS (blocker 10). Cross-family is a
    RELATION — `rowstate._approved` compares the reviewer's families against
    the author's — so resolving only the approve recipients left every
    approvable row GATED with 'no verified family evidence for the author'.
    Scoped through `_rows_for` so a proxied seat that never touched this
    repository costs no canary."""
    seats = set()
    for row in _rows_for(rows, gitdir).values():
        seat = row.get("recipient")
        if not seat:
            continue
        if row.get("polarity") == "approve" or row.get("kind") == "build":
            seats.add(seat)
    return seats


def resolve_families(seats):
    """{seat: frozenset(families) or None} — the one artifact-hostile read.

    A native seat resolves from its verified roster runtime in ~1 ms; a proxied
    seat costs a live authenticated canary, measured 1.5-2.0s each. So this is
    called ONCE PER DISTINCT SEAT and never per row: the live estate has 1472
    rows and 11 distinct approvers, and resolving per row would cost ~40 minutes
    to learn 11 facts. A seat whose family cannot be established maps to None,
    and `rowstate` refuses to call anything cross-family on a None."""
    out = {}
    for seat in sorted({s for s in (seats or ()) if s}):
        families, _evidence, _anchor, err = \
            dispatches._approval_identity_family_evidence(seat)
        out[seat] = frozenset(families) if families and not err else None
    return out


def snapshot(gitdir, trunk_ref=TRUNK_REF, rows=None, families=None,
             patch_cap=PATCH_SCAN_CAP, message_cap=MESSAGE_SCAN_CAP,
             lane_glob="lane/*"):
    """Build the World for one repository. -> World

    `families` is injectable so a caller can supply a cached map, and DEFAULTS
    TO NOT RESOLVING: family costs seconds per proxied seat and only the
    APPROVED arm needs it, so a board that renders LANDED/SUPERSEDED/BUILDING
    pays nothing for it. Pass `resolve_families(...)` to enable APPROVED."""
    if rows is None:
        rows, _verdicts, unavailable = dispatches.snapshot_with_verdicts()
    else:
        unavailable = None
    # TWO IDENTITIES, DELIBERATELY KEPT APART. Rows are filtered on the
    # CANONICAL identity (the common .git realpath — the only thing a row
    # ever stored), while git keeps spawning against the CALLER'S path:
    # worktree-local refs like HEAD must resolve in the worktree the caller
    # named, and refs/heads/ plus origin/* are shared either way. Collapsing
    # the two the other way filtered 1,491 canonical rows to zero.
    canonical, _identity_err = repo_identity(gitdir)
    mine = _rows_for(rows, canonical or gitdir)

    rc, revs = _git(gitdir, "rev-list", trunk_ref)
    trunk_shas = frozenset(l.strip().lower() for l in revs.splitlines()
                           if l.strip()) if rc == 0 else frozenset()
    branches = _branch_heads(gitdir)
    # THE SKIPPED COUNT TRAVELS (blocker 11): a receipt gate.receipts()
    # could not judge is PRESENT AND UNREADABLE, and rowstate must refuse to
    # read "no receipt binds this tip" off a ledger with holes in it.
    receipts, receipts_unavailable, receipts_skipped = gate.receipts()
    by_head = {}
    for receipt in receipts or ():
        head = str(receipt.get("head") or "").strip().lower()
        if head:
            by_head.setdefault(head, []).append(receipt)
    # Every commit any row might be judged AT: branch heads, receipt-bound
    # heads, and the tips rows record. Resolved in one pass so no arm of the
    # derivation is ever blind for want of a tree.
    wanted = set(branches.values()) | set(by_head)
    for row in mine.values():
        for field in ("tip", "reviewed_tip"):
            value = str(row.get(field) or "").strip().lower()
            if value:
                wanted.add(value)
    commit_trees, trunk_index, trunk_cts = _commit_trees(
        gitdir, trunk_ref, wanted)
    trunk_patch_ids_by_commit, trunk_err = _patch_ids(
        gitdir, ("-n", str(int(patch_cap)), trunk_ref))
    trunk_patch_ids = {}
    for sha, pid in trunk_patch_ids_by_commit.items():
        trunk_patch_ids.setdefault(pid, sha)
    # THE REVERSE INDEX SEES REVERTS (blocker 6). A clean `git revert` of C
    # has C's diff REVERSED, so hashing every trunk patch with `-R` makes a
    # revert of C collide with C's FORWARD id — `{forward pid: newest commit
    # that UNDOES it}`. rowstate orders the newest undo against the newest
    # forward match to tell historically-appeared from currently-carried.
    # One more ~1.6s streamed pass over the same window; the forward index
    # keeps newest-first via setdefault and so does this one.
    #
    # ITS FAILURE IS CARRIED, NOT DROPPED (task/744, T4). An empty
    # `trunk_reverts` says "nothing on trunk was reverted", and after T3 that
    # sentence licenses EVERY landing proof — so a `-R` scan that simply
    # failed would read as a clean bill of health for the whole population.
    trunk_reverse_by_commit, reverse_err = _patch_ids(
        gitdir, ("-R", "-n", str(int(patch_cap)), trunk_ref))
    trunk_reverts = {}
    for sha, pid in trunk_reverse_by_commit.items():
        trunk_reverts.setdefault(pid, sha)
    # SCAN COMPLETENESS COMES FROM COMMIT ENUMERATION, NEVER PATCH COUNT
    # (blocker 8): merge and empty commits produce no patch, so `len(index)`
    # both over-called the cap on a complete history with exactly cap
    # patch-bearing commits and under-called it when an empty commit sat in a
    # genuinely truncated window. The window is -n <cap> non-merge COMMITS,
    # so the one honest question is whether trunk holds more of them than the
    # window took. Shallow history and a failed enumeration are not a smaller
    # answer, they are NO answer (blocker 9): "unavailable" propagates as
    # inconclusive/UNKNOWN, never as negative evidence.
    rc_count, nonmerge_total = _git(gitdir, "rev-list", "--count",
                                    "--no-merges", trunk_ref)
    rc_shallow, shallow = _git(gitdir, "rev-parse", "--is-shallow-repository")
    if trunk_err or rc_count != 0 or not nonmerge_total.strip().isdigit() \
            or rc_shallow != 0 or shallow.strip() == "true":
        patch_scan = "unavailable"
    elif int(nonmerge_total.strip()) > int(patch_cap):
        patch_scan = "capped"
    else:
        patch_scan = "complete"
    head_tree, head_tree_err = _head_tree(gitdir, trunk_ref)
    lane_patch_ids, lane_err = _patch_ids(
        gitdir, ("--branches=" + lane_glob, "--not", trunk_ref))

    branch_commits, merge_bases, walk_failed = {}, {}, set()
    for name in branches:
        if not name.startswith("lane/"):
            continue
        rc, walk = _git(gitdir, "rev-list", trunk_ref + ".." + name)
        if rc != 0:
            # A BRANCH WHOSE WALK FAILED IS NOT A BRANCH WITH NO COMMITS
            # (task/744, T4). Skipping it silently left the ref absent from
            # `branch_commits`, which reads downstream as "this lane has
            # nothing past the merge-base" — a positive claim about a repo
            # nobody could read. Named here so `rowstate.derive` can say
            # UNKNOWN for that lane instead of asserting a build stage.
            walk_failed.add(name)
            continue
        branch_commits[name] = tuple(l.strip().lower()
                                     for l in walk.splitlines() if l.strip())
        # The merge-base anchors the TREE-IDENTITY proof: a trunk commit whose
        # tree equals the lane tip's counts as a squash land only when it is
        # NEWER than the fork point, which is what rules out a net-zero branch
        # matching its own base.
        rc, mb = _git(gitdir, "merge-base", trunk_ref, name)
        if rc == 0 and mb.strip():
            merge_bases[name] = mb.strip().lower()

    carriers = _carriers(mine)
    carriage = _carriage_by_row(gitdir, mine, carriers, trunk_ref)

    receipts_by_head = {head: tuple(sorted(group, key=_rank_receipt,
                                           reverse=True))
                        for head, group in by_head.items()}

    approvals, authors = {}, {}
    for row in mine.values():
        lane = str(row.get("lane") or "").strip()
        if not lane:
            continue
        if row.get("kind") == "build" and row.get("recipient"):
            authors.setdefault(lane, set()).add(row["recipient"])
        if row.get("polarity"):
            approvals.setdefault(lane, []).append(row)
    return World(
        gitdir=gitdir, trunk_ref=trunk_ref, trunk_shas=trunk_shas,
        trunk_trees={t: s for s, t in commit_trees.items() if s in trunk_shas},
        trunk_patch_ids=trunk_patch_ids, trunk_reverts=trunk_reverts,
        trunk_tokens=_trunk_tokens(gitdir, trunk_ref, message_cap),
        branches=branches, branch_commits=branch_commits,
        merge_bases=merge_bases,
        commit_patch_ids=dict(lane_patch_ids, **trunk_patch_ids_by_commit),
        commit_trees=commit_trees, trunk_index=trunk_index,
        trunk_cts=trunk_cts, rows=mine,
        receipts_by_head=receipts_by_head,
        approvals={k: tuple(v) for k, v in approvals.items()},
        authors={k: frozenset(v) for k, v in authors.items()},
        carriers=carriers,
        families=families,
        patch_scan=patch_scan,
        receipts_unavailable=receipts_unavailable,
        receipts_skipped=int(receipts_skipped or 0),
        head_tree=head_tree, carriage=carriage,
        unscoped=_unscoped(mine),
        reverts_unavailable=reverse_err,
        lane_scan_unavailable=lane_err,
        branch_walk_failed=frozenset(walk_failed),
        unavailable=unavailable or receipts_unavailable or trunk_err
        or reverse_err or lane_err or head_tree_err)


def derive_all(gitdir, trunk_ref=TRUNK_REF, rows=None, families=None):
    """{row id: Derivation} for one repository. -> (results, world)

    The whole point in one call: read the artifacts ONCE, then answer every
    row out of them. Measured on the live estate 2026-08-05 — 3.3s to build
    the world, 14 microseconds per row, 20ms for all 1473 rows."""
    world = snapshot(gitdir, trunk_ref=trunk_ref, rows=rows, families=families)
    return ({rid: rowstate.derive(row, world)
             for rid, row in (world.rows or {}).items()}, world)


_USAGE = ("derive [--repo PATH] [--trunk REF] [--families] [--json] — the "
          "state the ARTIFACTS support, beside the one the ledger remembers")


def cmd_derive(args):
    """derive [--repo PATH] [--trunk REF] [--families] [--json] — what the
    ARTIFACTS say, beside what the ledger remembers.

    Prints one line per row whose derived state DISAGREES with the ledger's,
    because agreement is not news. `--families` pays for the cross-family
    approval evidence (seconds per proxied seat) and is off by default: only
    APPROVED needs it, and without it an approvable row honestly reads GATED.
    """
    import json as _json

    from .cli import guard_tail
    # Trailing junk REFUSES before any work runs. A verb that silently ignores
    # `--familes` would print a families-free answer under a flag the reader
    # believes they passed, which is the same class of lie this whole module
    # exists to end.
    rc = guard_tail("helm derive", args, flags=("--families", "--json"),
                    valued=("--repo", "--trunk"), usage=_USAGE)
    if rc is not None:
        return rc
    repo = _flag(args, "--repo") or os.getcwd()
    # The identity check REFUSES a non-repository with a reason; the CALLER'S
    # path is what the reads run against (worktree-local refs must resolve in
    # the worktree), and `snapshot` filters rows on the canonical common
    # gitdir — never on `--absolute-git-dir`, which in a worktree is
    # `.git/worktrees/<name>` and matches ZERO stored rows (measured
    # 2026-08-07: 1,820 canonical rows filtered to six legacy ones).
    canonical, identity_err = repo_identity(repo)
    if identity_err:
        _note("helm derive: %s" % identity_err)
        return 2
    trunk = _flag(args, "--trunk") or TRUNK_REF
    rows, _verdicts, unavailable = dispatches.snapshot_with_verdicts()
    if unavailable:
        _note("helm derive: dispatch ledger unavailable: %s" % unavailable)
        return 2
    families = None
    if "--families" in args:
        # BOTH SIDES OF THE RELATION (blocker 10): authors and reviewers.
        # Resolving approve recipients alone answered half the cross-family
        # question and every approvable row stayed GATED.
        families = resolve_families(_family_seats(rows, canonical))
    results, world = derive_all(repo, trunk_ref=trunk, rows=rows,
                                families=families)
    if not world.trunk_shas:
        _note("helm derive: cannot read %s in %s — nothing derived, and an "
              "unread trunk is not an empty one" % (trunk, repo))
        return 2
    # AN UNREADABLE RECEIPT LEDGER REFUSES (blocker 11): every stage state
    # below GATED leans on receipt absence, so publishing rows off a ledger
    # that could not be read would print confident stages with no ground.
    # (Individually skipped rows degrade only their dependent rows to
    # UNKNOWN inside the derivation, and the count is printed.)
    if world.receipts_unavailable:
        _note("helm derive: the gate receipt ledger could not be read: %s — "
              "refusing to publish stages that lean on its absence"
              % world.receipts_unavailable)
        return 2
    # A DIAGNOSTIC ON STDOUT CORRUPTS THE DATA STREAM (task/744, T7). This
    # printed BEFORE the `--json` branch, so `helm derive --json | jq` was fed
    # a prose line ahead of the document and failed to parse — a caveat that
    # broke the very consumers it was warning. It goes to stderr, where every
    # refusal above it now goes too, AND it rides inside the envelope: moving
    # a warning off a channel must not take it out of reach of the reader who
    # is on that channel.
    if world.receipts_skipped:
        _note("helm derive: %d receipt rows could not be judged; rows whose "
              "stage leans on receipt absence read UNKNOWN"
              % world.receipts_skipped)
    out = []
    for rid, got in sorted(results.items()):
        row = world.rows[rid]
        out.append({"id": rid, "lane": row.get("lane"), "kind": row.get("kind"),
                    "ledger_status": row.get("status"),
                    "derived": got.state, "unknown": got.unknown,
                    "evidence": [[e.kind, _plain(e.detail)]
                                 for e in got.evidence]})
    if "--json" in args:
        print(_json.dumps({"trunk": trunk,
                           "receipts_skipped": world.receipts_skipped,
                           "rows": out}, indent=1))
        return 0
    stale = [r for r in out if r["ledger_status"] == "open"
             and r["derived"] in (rowstate.LANDED, rowstate.SUPERSEDED)]
    print("%d rows read from the artifacts; %d are OPEN on the ledger while "
          "the artifacts say otherwise" % (len(out), len(stale)))
    for r in stale:
        print("  %s  %-38s ledger=%-9s artifacts=%-10s %s"
              % (r["id"][:12], (r["lane"] or "-")[:38], r["ledger_status"],
                 r["derived"], r["evidence"][-1][0] if r["evidence"] else "-"))
    return 0


def _note(message):
    """A DIAGNOSTIC, on the channel diagnostics belong to (task/744, T7).

    stdout is this verb's DATA — a JSON document under `--json`, a report
    otherwise — and every caveat printed onto it corrupted the thing it was
    trying to caveat. `helm derive --json | jq` was fed a prose line ahead of
    the document and failed to parse, so the warning about an incomplete
    receipt ledger broke exactly the consumers it existed to warn."""
    print(message, file=sys.stderr)


def _flag(args, name):
    """The value after `name` in argv, or None."""
    args = list(args or ())
    return args[args.index(name) + 1] \
        if name in args and args.index(name) + 1 < len(args) else None


def _plain(detail):
    """Evidence detail as JSON-safe data — tuples become lists, and a nested
    Evidence becomes its own pair rather than a repr nobody can parse."""
    if isinstance(detail, rowstate.Evidence):
        return [detail.kind, _plain(detail.detail)]
    if isinstance(detail, (tuple, list)):
        return [_plain(d) for d in detail]
    return detail
