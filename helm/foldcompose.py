"""`helm compose` — the fold that can be REQUIRED to prove its own cars.

WHY A VERB AND NOT A MANIFEST WRITTEN BESIDE A HAND COMPOSE. task/987 measured
that a lane can fold with ZERO review rows and nothing refuses it, and the two
lanes it happened to were the two handed over in chat with a green gate and the
word LANDABLE. An author who skips review skips an author-invoked guard too, so
the evidence has to be produced by the thing that does the work. This verb
replaces the hand loop one for one; the fold entry can then require its output.

WHY REPLAY AND NOT PATCH IDENTITY, which is the ruling this row was filed
under. `git patch-id` hashes CONTEXT, so a car cherry-picked underneath other
cars that touched its files re-keys — measured on the live composed train 3,
four unmatched cars, all four content-identical to their reviewed lane commits
and differing only in index lines and hunk headers. A patch-id MISS therefore
proves only that no commit carries that EXACT diff. Converting such a miss into
a refusal refused a train's own top, a lane its author had concurred on
twenty-five minutes earlier. So the manifest records only immutable addresses:
the source commit and result commit let a verifier derive both parent and tree,
re-run the pick, and compare TREES rather than diffs.

WHAT CANNOT BE REPLAYED SAYS SO. A conflicted pick has no result commit and is
not recorded — an address to an object that does not exist is not evidence.
The composer stops with `exact-review-required`, an honest handback rather than
a weaker automatic answer. A later hand edit is caught because replay no longer
reproduces the result commit's actual tree.

WHERE THE MANIFEST LIVES, and it is not a preference. It is written OUTSIDE the
worktree, under the project home and keyed by the RESULT TIP, because a gate
binds the WORKTREE: an untracked file in the room turns foldcheck red, and a
tracked one would be a car nobody reviewed. Keying by result tip is what lets a
reader who holds only a composed sha find the evidence for it.
"""
import json
import os
import pathlib
import re
import tempfile
import time

from . import home, registry
from .work import _lanes
from . import pk

#: Where the per-tip records live under `home.project_dir(<project>)`.
MANIFEST_DIR = "compose-manifests"

#: What the CLI says about a compose that stopped mid-pick. It is a message
#: to an operator, never a field in the record: a conflicted car has no
#: result commit, so it has no address, so it is not recorded at all.
EXACT_REVIEW_REQUIRED = "exact-review-required"

#: Object ids are full and lowercase in every stored field. An abbreviation is
#: ambiguous by construction and a record keyed on one cannot be verified
#: later, so the writer refuses rather than storing a shorter one.
_FULL_ID = re.compile(r"\A(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")

#: ADDRESSES ONLY. A car record carries a row id, the commit it picked, and
#: the commit that pick produced — nothing else is evidence.
#:
#: THE FIRST TWO CUTS RECORDED TREES AND A STATUS AND THE READER COMPARED
#: AGAINST THEM, which is the same trust one layer down: a hand-written record
#: could name any tree it liked and the "replay" would agree with it. Every
#: value this module needs is derivable from these three addresses plus the
#: repository, so the record holds no value at all. `lane` rides along for a
#: human reading the file and is never consulted as evidence.
_CAR_FIELDS = ("row", "source_commit", "result_commit")


def _git(where, *args, timeout=60):
    """ONE git runner for this module, and it is the lane package's.

    `helm/work/_lanes.py` already owns a named git boundary that goes through
    the VCS seam, and `helm/landgate.py` already has a second one. A third
    would make "what does helm do to git here" a question with three answers,
    which is the shape the prior-art sweep for this row was asked to avoid.
    """
    return _lanes._git(where, *args, timeout=timeout)


def project_state(root):
    """(registered | unregistered | unknown, project-or-None)."""
    if not root:
        return "unknown", None
    want = os.path.realpath(root).rstrip(os.sep)
    try:
        # STRICT, AND THE DEFAULT WOULD MAKE THE HANDLER BELOW DEAD CODE.
        # `registry.load()` without it reads through `pk.read_json(path,
        # default)`, which returns the DEFAULT on any failure -- so an
        # unreadable or corrupt registry arrives here as an empty projects
        # map, the loop never runs, and this function answers "unregistered".
        # That is a POSITIVE claim (this repo is not a registered project)
        # derived from a FAILED READ, and its caller in the fold treats it as
        # "no project can have been activated" and PASSES. The strict read is
        # the existing checked accessor for exactly this -- `_checked_registry`
        # says it in one line, "missing is empty, failed is not" -- so absence
        # still reaches the loop and only failure reaches the handler.
        reg = registry.load(strict=True)
    except Exception:                       # a broken registry is not absence
        return "unknown", None
    projects = (reg or {}).get("projects", {})
    if not isinstance(projects, dict):
        return "unknown", None
    # AN ENTRY WE COULD NOT RESOLVE IS NOT AN ENTRY WE COMPARED, and
    # "unregistered" is a claim about having compared them ALL.
    skipped = False
    for name, rec in sorted(projects.items()):
        if not isinstance(rec, dict):
            return "unknown", None
        path = rec.get("path")
        if not path:
            continue
        try:
            # STRICT, FOR THE SAME REASON THE REGISTRY READ IS. Plain realpath
            # SWALLOWS a resolution failure and returns the path UNRESOLVED, so
            # a registered ALIAS whose own parent loses search permission stops
            # matching the physical repo and this function answers
            # "unregistered" -- a positive claim produced by a failed read,
            # while the repo itself is still perfectly reachable. MEASURED:
            # with the alias's parent at 0o000, realpath returns the alias path
            # unchanged and raises nothing, while a CHECKED resolve raises
            # PermissionError.
            #
            # `Path.resolve(strict=True)` AND NOT `realpath(..., strict=True)`,
            # WHICH IS A FLOOR QUESTION AND NOT A TASTE ONE. This repo promises
            # Python 3.9 and CI measures it there; realpath's `strict` is
            # documented as 3.10, so whether it exists at the floor depends on
            # the PATCH level CI happens to resolve. `Path.resolve` has taken
            # `strict` since 3.6, so the question does not arise. MEASURED
            # IDENTICAL on 3.9.25 and 3.14.4 across all five poles -- real
            # directory, symlink to one, dangling link, never-created, and an
            # entry whose parent is 0o000.
            resolved = str(pathlib.Path(path).resolve(strict=True))
            resolved = resolved.rstrip(os.sep)
        except (OSError, RuntimeError):
            # Path.resolve reports symlink loops as RuntimeError on older
            # Python versions and OSError on newer ones. Both leave this entry
            # unresolved; neither can establish a registration or its absence.
            # PER-ENTRY, NEVER WHOLESALE: one stale or unreadable registration
            # must not make every other project's answer unknown, so this only
            # withholds the FINAL negative.
            skipped = True
            continue
        if resolved == want:
            # SORTED ORDER IS THE TIE-BREAK, SO A SKIP BEFORE THIS POINT IS NOT
            # ONLY A HOLE IN THE NEGATIVE -- IT IS A HOLE IN THE NAME. The
            # registry permits two entries to hold the same physical path, and
            # this loop answers with the FIRST that matches. An earlier entry
            # that could not be resolved might have been that one, so returning
            # this name says "the registration is `z`" on the strength of not
            # having been able to read `a`. The caller uses the NAME to find the
            # activation record, so a wrong name is a wrong activation answer,
            # not a cosmetic one.
            #
            # A registry holding two entries for one path is ambiguous whether
            # or not anything failed to read; that ambiguity is older than this
            # function and is not narrowed here. What is narrowed is the part
            # this module owns: the ambiguity must not be CREATED by a failed
            # read.
            if skipped:
                return "unknown", None
            return "registered", name
    return ("unknown" if skipped else "unregistered"), None


def project_for(root):
    """The registry NAME for a repo root, or None when it is not registered."""
    state, project = project_state(root)
    return project if state == "registered" else None


def manifest_dir(project):
    return os.path.join(home.project_dir(project), MANIFEST_DIR)


def manifest_path(project, tip):
    """The record for one composed tip. Full ids only — see `_FULL_ID`."""
    if not (project and tip and _FULL_ID.fullmatch(str(tip or ""))):
        return None
    return os.path.join(manifest_dir(project), str(tip) + ".json")


#: The activation record and its independent latch. The latch is written
#: FIRST, so an interrupted activation is UNKNOWN rather than silently
#: inactive. Deleting or corrupting either half after activation also stays
#: loud; only two genuine absences mean the feature has never been activated.
ACTIVATION = "compose-manifest-activation.json"
ACTIVATION_LATCH = "compose-manifest-activation-latch.json"
ACTIVATION_INACTIVE = "inactive"
ACTIVATION_ACTIVE = "active"
ACTIVATION_UNKNOWN = "unknown"


def activation_path(project):
    return os.path.join(manifest_dir(project), ACTIVATION)


def activation_latch_path(project):
    return os.path.join(home.project_dir(project), ACTIVATION_LATCH)


def _presence(path):
    """("absent" | "present" | "unknown") — AN I/O FAILURE IS NEVER ABSENCE.

    `os.path.lexists` answers False for a path that is genuinely gone AND for
    one it could not examine, because it swallows the OSError. MEASURED: with
    a file present inside a directory chmod'ed 0o000, `lexists` returns False
    while `lstat` raises EACCES. Every caller that reads absence as an
    exemption therefore grants that exemption on a failed read, which is the
    one direction a guard must never fail in.

    `lstat` separates them: ENOENT -- the ONE error that says nothing is
    there -- answers "absent", and EVERY other OSError answers "unknown",
    ENOTDIR included. The only exit for insufficiency is UNKNOWN.

    Conflating the two is not always wrong -- `read_manifest` does it
    deliberately, and says so, because there absence and malformation are both
    NO EVIDENCE and the fold treats both as unproven. The test is the
    DIRECTION: conflate only where the merged answer is the conservative one.
    """
    try:
        os.lstat(path)
    except NotADirectoryError:
        # ENOTDIR MEANS A PARENT IS NOT A DIRECTORY, WHICH IS A BROKEN ESTATE
        # AND NOT AN ANSWER ABOUT THIS FILE. The child cannot exist, so
        # "absent" is factually true and STILL the wrong direction: it is the
        # negative that grants the exemption, and a project home replaced by a
        # regular file would read INACTIVE on both activation paths at once.
        return "unknown"
    except FileNotFoundError:
        # ENOENT IS ORDINARY ABSENCE AND MUST STAY THAT WAY. A project that has
        # never been activated has no manifest directory at all, so a MISSING
        # container is the commonest honest "nothing here" in this module --
        # calling it unknown makes every never-activated project unreadable,
        # which is how this cure first went wrong.
        #
        # A container that EXISTS but cannot be examined is the other case, and
        # ENOTDIR above already covers a parent that is a regular file. The
        # remaining shape is a parent that is there and refuses to be read.
        parent = os.path.dirname(path) or "."
        try:
            os.stat(parent)
        except FileNotFoundError:
            # STAT FOLLOWS LINKS, SO ITS ENOENT MERGES TWO WORLDS: nothing is
            # there at all, and something IS there that does not resolve. A
            # DANGLING SYMLINK where the project home should be is the second,
            # and it is a broken estate rather than a project that was never
            # activated. lstat separates them because it does NOT follow, so
            # the link ENTRY answers for itself. MEASURED:
            #   dangling link   stat ENOENT   lstat ok      -> broken
            #   never created   stat ENOENT   lstat ENOENT  -> absent
            # lstat alone cannot do this: on a dangling link it SUCCEEDS, so a
            # reader that only asks lstat falls through to absence unchanged.
            try:
                os.lstat(parent)
            except FileNotFoundError:
                return "absent"             # nothing has been created yet
            except OSError:
                # THE SECOND READ FAILS FOR ITS OWN REASONS -- ELOOP in a
                # grandparent, EACCES if a mode changed between the two calls,
                # ENAMETOOLONG -- and NONE of them establish that nothing is
                # there. Only ENOENT does. Catching them all as absence would be
                # this module's own law broken inside the cure that states it.
                return "unknown"
            return "unknown"                # an entry is there and does not resolve
        except OSError:
            return "unknown"                # the container refuses to be read
        return "absent"
    except OSError:                         # EACCES, EIO, ELOOP, ENAMETOOLONG
        return "unknown"
    return "present"


def _activation_file(path):
    """("absent" | "active" | "unknown", trunk-or-None)."""
    seen = _presence(path)
    if seen != "present":
        return ("absent" if seen == "absent" else "unknown"), None
    try:
        with pk.open_regular(path, encoding="utf-8") as fh:
            rec = json.load(fh)
    except (OSError, UnicodeDecodeError, ValueError):
        return "unknown", None
    if not isinstance(rec, dict) or set(rec) != {"v", "trunk"} \
            or type(rec.get("v")) is not int or rec["v"] != 1:
        return "unknown", None
    trunk = rec.get("trunk")
    if not isinstance(trunk, str) or not _FULL_ID.fullmatch(trunk):
        return "unknown", None
    return "active", trunk


def activation_state(project):
    """(inactive | active | unknown, trunk-or-None).

    ABSENT and UNREADABLE are different states. The separate latch preserves
    that distinction after either activation file is deleted or torn: an
    interrupted or damaged activation can never turn enforcement back off.
    """
    record, trunk = _activation_file(activation_path(project))
    latch, latched = _activation_file(activation_latch_path(project))
    if record == latch == "absent":
        return ACTIVATION_INACTIVE, None
    if record == latch == "active" and trunk == latched:
        return ACTIVATION_ACTIVE, trunk
    return ACTIVATION_UNKNOWN, None


def activation_trunk(project):
    """The verified activation trunk, or None when inactive or unreadable."""
    state, trunk = activation_state(project)
    return trunk if state == ACTIVATION_ACTIVE else None


def _write_once(path, record):
    """Atomically create one JSON record without replacing an existing one."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(record, fh, sort_keys=True, indent=1)
            fh.flush()
            os.fsync(fh.fileno())
        try:
            os.link(tmp, path)
        except FileExistsError:
            return False
        return True
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def activate(project, where, trunk):
    """Write-once activation. -> (path, error).

    The latch lands first. A crash between the two creates UNKNOWN and a retry
    with the same trunk may finish it; no intermediate state reads inactive.
    """
    resolved = _resolve(where, trunk)
    if not resolved:
        return None, "activation trunk %r names no commit" % trunk
    record_path = activation_path(project)
    latch_path = activation_latch_path(project)
    record_state, recorded = _activation_file(record_path)
    latch_state, latched = _activation_file(latch_path)
    for state, existing in ((record_state, recorded), (latch_state, latched)):
        if state == "unknown":
            return None, "activation state is unreadable; repair it before retrying"
        if state == "active" and existing != resolved:
            return None, ("activation is already bound to %s, not %s"
                          % (existing[:12], resolved[:12]))
    body = {"v": 1, "trunk": resolved}
    try:
        if latch_state == "absent" and not _write_once(latch_path, body):
            return None, "activation latch changed concurrently"
        if record_state == "absent" and not _write_once(record_path, body):
            return None, "activation record changed concurrently"
    except OSError as exc:
        return None, "activation could not be written: %s" % exc
    state, active = activation_state(project)
    if state != ACTIVATION_ACTIVE or active != resolved:
        return None, "activation did not settle to one readable trunk"
    return record_path, None


def _usable_car(car):
    """A car record this reader can act on, or None.

    WHOLE RECORD OR NOTHING. A car whose result tree is missing is not a
    smaller answer about that car, it is no answer, and a verifier that
    skipped it while calling the manifest complete would license exactly the
    unreviewed land this row exists to refuse.
    """
    if not isinstance(car, dict):
        return None
    for field in _CAR_FIELDS:
        if not isinstance(car.get(field), str) or not car[field]:
            return None
    for field in ("source_commit", "result_commit"):
        if not _FULL_ID.fullmatch(car[field]):
            return None
    return car


def read_manifest(project, tip):
    """The stored record for a composed tip, or None.

    Drops what it cannot fully understand rather than repairing it: a
    half-read manifest that still answers is a manifest whose answer nobody
    can bound. Absence and malformation are the same answer here — NO
    EVIDENCE — and the fold treats both as unproven rather than as proof of
    anything about the cars.
    """
    path = manifest_path(project, tip)
    if not path or not os.path.exists(path):
        return None
    try:
        def unique(pairs):
            out = {}
            for key, value in pairs:
                if key in out:
                    raise ValueError("duplicate manifest key")
                out[key] = value
            return out
        with pk.open_regular(path, encoding="utf-8") as fh:
            rec = json.load(fh, object_pairs_hook=unique)
    except (OSError, UnicodeError, ValueError, RecursionError):
        return None
    if not isinstance(rec, dict):
        return None
    if rec.get("result_tip") != tip:
        return None                          # a record for a different tip
    cars = rec.get("cars")
    if not isinstance(cars, list) or not cars:
        return None
    usable = [_usable_car(car) for car in cars]
    if not all(usable):
        return None
    rec["cars"] = usable
    return rec


def write_manifest(project, record):
    """Persist one composed tip's evidence. Returns the path, or None.

    Atomic by rename, because a reader that finds a half-written manifest
    cannot tell it from a composer that recorded fewer cars than it picked.
    """
    tip = record.get("result_tip") if isinstance(record, dict) else None
    path = manifest_path(project, tip)
    if not path:
        return None
    if not record.get("cars") or not all(
            _usable_car(car) for car in record["cars"]):
        return None                          # never store what cannot be read
    if "compose_land_version" in record or "bounded_rows" in record:
        from . import compose_contract
        if compose_contract.writer_error() or record.get("dry_run") is not False:
            return None
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(record, fh, sort_keys=True, indent=1)
        os.replace(tmp, path)
    except OSError:
        return None
    return path


def _resolve(where, rev):
    """A revision -> its full commit id, or None when git cannot name one."""
    rc, out, _ = _git(where, "rev-parse", "--verify", "--quiet",
                      str(rev) + "^{commit}")
    out = (out or "").strip()
    return out if rc == 0 and _FULL_ID.fullmatch(out) else None


def _tree_of(where, rev):
    """The TREE a revision names, or None. Trees are what a replay compares:
    a diff can be re-keyed by moving context, a tree cannot."""
    rc, out, _ = _git(where, "rev-parse", "--verify", "--quiet",
                      str(rev) + "^{tree}")
    out = (out or "").strip()
    return out if rc == 0 and _FULL_ID.fullmatch(out) else None


def cars_to_pick(where, base, lane_ref):
    """(commits, error) — the lane commits `git cherry` reports as PLUS.

    PLUS means "no commit upstream carries this patch", which is the right
    question for what to PICK and emphatically not for what to VERIFY: a
    MINUS is a real positive, a PLUS is only "not found by exact diff". The
    asymmetry is the whole reason this module records trees instead.

    Order is preserved as git reports it — oldest first — because a pick
    sequence that reorders cars produces a different tree and the manifest
    would then describe a compose nobody ran.
    """
    rc, out, err = _git(where, "cherry", base, lane_ref)
    if rc != 0:
        return None, (err or "git cherry could not answer")
    picks = []
    for line in (out or "").splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        if parts[0] == "+" and _FULL_ID.fullmatch(parts[1]):
            picks.append(parts[1])
    return picks, None


def _all_minus(where, upstream, head):
    """(True/False/None, detail) — is EVERY commit of `head` already present
    in `upstream` by patch? None when git could not answer, and a caller must
    not read that as True: an unanswerable control is not a passed one."""
    rc, out, err = _git(where, "cherry", upstream, head)
    if rc != 0:
        return None, (err or "git cherry could not answer")
    plus = [l.split()[1] for l in (out or "").splitlines()
            if l.split()[:1] == ["+"] and len(l.split()) == 2]
    if plus:
        return False, "%d commit(s) not carried, first %s" % (len(plus),
                                                              plus[0][:12])
    return True, "every commit carried"


def replay_car(where, car):
    """(True/False/None, detail) — re-run this ONE pick and compare TREES.

    THE INSTRUMENT THE MANIFEST EXISTS FOR, and the first cut did not have it:
    the record CLAIMED a car was replayable and nothing ever replayed it. A
    recorded tree is a claim; a recomputed one is evidence.

    `git merge-tree --write-tree --merge-base=<source>^ <parent> <source>` IS
    the cherry-pick, performed read-only: it merges the source commit into the
    parent it actually landed on, with the source's own parent as the merge
    base, and prints the resulting tree without touching any worktree. So the
    verification costs no room and can run from a bare clone.

    A ROOT SOURCE HAS NO PARENT and therefore no pre-image to merge against;
    it answers None rather than False, because "cannot be replayed" and
    "replayed to something else" are the two states a fold most needs told
    apart.
    """
    source = car.get("source_commit") or ""
    result = car.get("result_commit") or ""
    # THE TREE IS READ FROM GIT, NEVER FROM THE RECORD. Comparing a replay
    # against a tree the record supplied is not a verification: a hand-written
    # manifest naming any tree it likes would agree with itself. The record
    # supplies only the ADDRESS of the commit; what that commit holds is git's
    # to say.
    want = _tree_of(where, result)
    if not want:
        return None, "%s is not a readable commit here" % (result or "?")[:12]
    parent = _resolve(where, result + "^")
    if not parent:
        return None, "no parent for the result commit: nothing to replay onto"
    if not _resolve(where, source + "^"):
        return None, "%s is a root commit: no pre-image to replay" % source[:12]
    rc, out, err = _git(where, "merge-tree", "--write-tree",
                        "--merge-base=" + source + "^", parent, source)
    if rc != 0:
        first = (out or "").splitlines()[:1]
        return False, ("the replay CONFLICTED where the compose did not: %s"
                       % (first[0] if first else (err or "conflict")))
    got = (out or "").splitlines()[:1]
    got = got[0].strip() if got else ""
    if not _FULL_ID.fullmatch(got):
        return None, "the replay named no tree"
    if got != want:
        return False, ("replay produced %s, the record claims %s"
                       % (got[:12], want[:12]))
    return True, "replay reproduces the recorded tree"


def replay_chain(where, cars, base, tip):
    """[(name, ok, detail)] — the SEQUENTIAL replay of a whole compose.

    THIS REPLACES A CONTROL THAT COULD NOT PASS. The first cut compared the
    FINAL room tree against `merge-tree(base, lane)` once per lane, which is
    only meaningful for a ONE-CAR room: with cars A and B the room holds
    base+A+B and neither base+A nor base+B can equal it, so BOTH rungs
    refused every real two-car compose. Measured on a disposable two-car
    probe against a live train. A one-car arm cannot find it: the shape only
    appears once a second car moves the room past base+A.

    THE SEQUENCE IS THE SUBJECT, not each car against a fixed base. Three
    things are asserted and each is re-derived from git:
      · every car REPLAYS onto the parent it actually landed on;
      · the parents CHAIN — car i's result parent is car i-1's result, and
        the first car's parent is the base;
      · the chain ENDS at `tip`, which is what binds this record to the
        commit a reader is asking about. Without it a valid manifest for one
        compose would answer for a different one.
    """
    rows, previous = [], _resolve(where, base)
    if not previous:
        return [("replay-chain", None, "the base names no commit here")]
    for index, car in enumerate(cars):
        label = "replay:%s" % (car.get("row") or "?")
        parent = _resolve(where, (car.get("result_commit") or "") + "^")
        if parent != previous:
            rows.append((label, False,
                         "car %d landed on %s, not on %s — the picks do not "
                         "form one sequence"
                         % (index, (parent or "?")[:12], previous[:12])))
            return rows
        rows.append((label,) + replay_car(where, car))
        previous = car.get("result_commit") or ""
    want = _resolve(where, tip)
    if not want:
        rows.append(("replay-ends-at-tip", None,
                     "the tip names no commit here"))
    elif previous != want:
        rows.append(("replay-ends-at-tip", False,
                     "the recorded chain ends at %s, not at %s"
                     % (previous[:12], want[:12])))
    else:
        rows.append(("replay-ends-at-tip", True,
                     "the chain ends at the tip it is being read for"))
    return rows


def worktree_is_clean(where):
    """(True/False/None, detail) from porcelain. A dirty room means the tree
    a gate would bind is not the tree these cars produced."""
    rc, out, _ = _git(where, "status", "--porcelain")
    if rc != 0:
        return None, "git status could not answer"
    lines = [l for l in (out or "").splitlines() if l.strip()]
    if lines:
        return False, "%d dirty path(s), first %r" % (len(lines),
                                                      lines[0][:60])
    return True, "clean"


# How many missing commits a disclosure names before it stops listing them. A
# reader acts on the first few and on the TOTAL; a hundred lines of shas is a
# wall of text that gets scrolled past, which is the same as not disclosing.
GAP_LISTED = 12


#: The refs this census will count as trunk, in preference order. NAMED ONCE,
#: because the remedy for "nothing could be asked" has to say WHICH branches
#: would have been asked: a fetch that creates a tracking ref for some other
#: branch is a successful fetch that changes nothing here, and a reader told
#: to fetch without being told what is counted has no way to see that coming.
TRUNK_CANDIDATES = ("origin/main", "main", "origin/master", "master")


def base_gap(where, base_sha, trunk=None):
    """What TRUNK carries that this base does not.

    THE QUESTION A MANIFEST CANNOT ANSWER ABOUT ITSELF. A compose record
    proves WHAT is in the tree -- the cars picked, their source commits, the
    result trees -- and every one of those fields is true of a base that is
    weeks behind. A reviewer who checks the tree against the manifest gets a
    clean confirmation, which is exactly the kind of answer that stops them
    looking. It is not hypothetical: two cli-proxy composes were reviewed,
    approved and installed on thirteen sidecars while their base was missing
    eight commits that had been on trunk for two days, and the running proxy
    lost three shipped features for about nineteen hours.

    IT IS THE SAME LAW AS foldcheck AND IT IS ONE COMMAND. A gate binds the
    tree at its start and is silent about the base; that is why a land
    announcement runs foldcheck. Compose had no equivalent, and the missing
    measurement is a rev-list.

    A COUNT AND THE COMMITS, NEVER A BOOLEAN. "base: behind" is a word a
    reader shrugs at; "8 commits on origin/main are missing from this base,
    here are the first of them" is a sentence that stops a compose. The list
    is capped at GAP_LISTED and the record keeps `behind` and `truncated` so a
    capped rendering cannot be mistaken for the whole set.

    UNRESOLVABLE TRUNK IS UNKNOWN, NOT ZERO. A ref that names no commit, or a
    git that cannot answer, returns `unknown` with its reason -- a compose in
    a clone with no remote must not read as "nothing is missing".

    AND THE REF IS DISCLOSED BECAUSE IT IS A SNAPSHOT. `origin/main` moves
    only on fetch, so a stale remote-tracking ref reports a gap that is
    already closed, or misses one that is not. The record names which ref
    answered and where it points, and says so in the same breath rather than
    leaving the reader to remember.

    A SECOND REMOTE IS A DIFFERENT PROJECT AND IS NOT A CANDIDATE. Only the
    local default branch and ITS origin are asked. Measured in the cli-proxy
    fork, where `upstream/main` is the public repository this fork
    deliberately left: every single compose reads 504 commits "missing" from
    it, which is TRUE and is NOISE, and a line that prints 504 on every run is
    skipped by Tuesday with the eight that matter buried inside it.

    EVERY CANDIDATE TRUNK IS MEASURED AND THE WORST ANSWER WINS, because the
    refs disagree and the disagreement is invisible from inside. Measured in
    the cli-proxy fork: the compose that lost three shipped features reads
    ZERO behind against `origin/main` and EIGHT behind against local `main`,
    because the remote-tracking ref had not been fetched. Asking only the
    conventional ref would have printed "nothing is missing" over the exact
    commits that went missing. So all of them are counted, the largest is
    reported, and `measured_refs` carries the whole disagreement — the cost of
    over-reporting is one `git fetch`, and the cost of under-reporting was
    nineteen hours.

    THE NUMBER IS TRUE AT THE MOMENT IT IS TAKEN AND AT NO OTHER, WHICH IS
    WHY IT IS STAMPED. The question worth asking is "what was ALREADY on
    trunk when this base was chosen" -- the set a composer could have had and
    did not. Re-running this against a months-old manifest asks a different
    question, "how far has trunk moved since", and every historical compose
    answers it loudly. Measured across the cli-proxy fork's six historical
    compose refs: FIVE read behind against today's trunk, and FOUR of those
    five were exactly ZERO behind when they were cut. A reader shown the
    recomputed number sees five defects where there was one, and tunes the
    disclosure out inside a week -- at which point the guard is worse than
    absent, because it has trained its own audience. So this is called ONCE,
    at compose time, and `measured_ts` plus `trunk_sha` travel with the count:
    a later reader RENDERS THE STORED MEASUREMENT and never recomputes it."""
    tried = [trunk] if trunk else list(TRUNK_CANDIDATES)
    now = int(time.time())

    def count(a, b):
        """Commits in B that are not in A, or None when git cannot answer."""
        rc, out, _ = _git(where, "rev-list", "--count", "%s..%s" % (a, b))
        if rc != 0 or not (out or "").strip().isdigit():
            return None
        return int(out.strip())

    def bail(reason, refs=()):
        return {"trunk": None, "trunk_sha": None, "behind": None,
                "commits": (), "truncated": False, "measured_refs": tuple(refs),
                "ref_age_s": None, "ref_current": None,
                "ref_current_reason": None,
                "has_remote": _has_remote(where), "measured_ts": now,
                "unknown": reason}

    # THE PRODUCER LEARNED THE THIRD ANSWER AND THE CONSUMER DID NOT, which is
    # how a cure leaves its own hole. `_has_remote` was taught to return None
    # when `git remote` FAILS rather than mapping failure to "no remote" — but
    # the only place that reads it tests `is False`, so None fell through as a
    # successful census with a clean zero and no acknowledgement. A review
    # found it on task/2484 r2. A remote list helm could not read is an
    # INCOMPLETE census: nothing here can say whether there is news to have
    # missed, so it refuses rather than answering.
    has_remote = _has_remote(where)
    if has_remote is None:
        return bail("`git remote` could not be read here, so whether this "
                    "clone has an upstream to be behind of is UNKNOWN — and a "
                    "count of local refs cannot answer it")

    # ABSENT AND FAILED ARE DIFFERENT ANSWERS AND ONLY ONE MAY BE SKIPPED.
    # A ref that does not resolve is simply not present here — a repo with no
    # `origin/master` is not a broken measurement, and dropping it costs
    # nothing. A ref that RESOLVES and whose count cannot be read is a FAILED
    # REQUIRED MEASUREMENT: silently dropping it shrinks the census and hands
    # back a smaller answer wearing the same shape as a complete one, so the
    # ref that would have carried the gap disappears and `unknown` stays None.
    def resolve_ref(rev):
        """(sha, failed). A MISSING REF AND AN UNREADABLE ONE ARE NOT THE SAME
        ANSWER: `rev-parse --verify --quiet` exits 1 for "no such object",
        which is a real absence a census may skip, and exits otherwise for a
        repository it could not read or a command that never completed — which
        `_resolve` flattens to None and a caller then skips as absent. An I/O
        failure is not proof that a ref does not exist."""
        rc, out, _ = _git(where, "rev-parse", "--verify", "--quiet",
                          str(rev) + "^{commit}")
        text = (out or "").strip()
        if rc == 0 and text:
            return text, False
        return None, rc not in (0, 1)

    cands, unreadable = [], []
    for ref in tried:
        sha, failed = resolve_ref(ref)
        if failed:
            unreadable.append(ref)
            continue                         # unreadable, NOT absent
        if not sha:
            continue                         # absent, not failed
        behind = count(base_sha, sha)
        if behind is None:
            unreadable.append(ref)
            continue
        cands.append({"ref": ref, "sha": sha, "behind": behind})
    if unreadable:
        return bail(
            "%s could not be read or counted against this base, so the census "
            "is INCOMPLETE and what this base is missing is UNKNOWN — a "
            "partly failed census is not a smaller successful one, and an I/O "
            "failure is not proof that a ref is absent"
            % ", ".join(unreadable),
            [(c["ref"], c["behind"]) for c in cands])
    if not cands:
        return bail("no trunk ref could be counted (tried %s), so what this "
                    "base is missing was not measured -- which is not the "
                    "same as missing nothing" % ", ".join(tried))

    # TWO REFS THAT BOTH CLAIM TO BE TRUNK AND DISAGREE IRRECONCILABLY IS AN
    # UNKNOWN, NOT A VOTE. A STALE ref is an ANCESTOR of the true trunk --
    # behind in one direction and ZERO in the other -- and out-voting it is
    # the whole point of taking the largest. A ref that is nonzero BOTH ways
    # has its own commits, so neither contains the other and nothing here can
    # say which one the work is supposed to land on. Guessing would be a
    # confident wrong answer in the register of a measurement.
    for i, a in enumerate(cands):
        for b in cands[i + 1:]:
            a_ahead, b_ahead = count(b["sha"], a["sha"]), count(a["sha"], b["sha"])
            if a_ahead is None or b_ahead is None:
                # NONE IS NOT ZERO. Treated as falsy this asserts "they do not
                # diverge" from a comparison that never ran, and a suppressed
                # divergence is exactly the answer successfully measured counts
                # would have refused.
                return bail(
                    "%s and %s could not be compared, so which of them is "
                    "trunk is UNKNOWN — an unread comparison is not a proof "
                    "that they agree" % (a["ref"], b["ref"]),
                    [(c["ref"], c["behind"]) for c in cands])
            if a_ahead and b_ahead:
                return bail(
                    "%s and %s DIVERGE (%d and %d commits apart), so which one "
                    "is trunk cannot be decided from here -- fetch, or name "
                    "the ref explicitly" % (a["ref"], b["ref"], b_ahead, a_ahead),
                    [(c["ref"], c["behind"]) for c in cands])

    # THE REMAINING REFS FORM A CHAIN, so the largest is the furthest along
    # and the smaller ones are its ancestors. Taking the largest is what stops
    # an un-fetched `origin/main` reporting zero over commits local `main`
    # already has.
    winner = max(cands, key=lambda c: c["behind"])
    ref, sha, behind = winner["ref"], winner["sha"], winner["behind"]
    _obs = _ref_observation(where, ref, sha)
    commits = ()
    if behind:
        # THE FROZEN SHA, NOT THE MUTABLE REF. The count above was taken
        # against `sha`; asking `ref` again can list subjects from a DIFFERENT
        # commit, because another worktree may advance the shared branch
        # between the two reads. That stores a count of 1 beside two subjects,
        # or — after a rewind — omits a subject the count included.
        rc, out, _ = _git(where, "log", "--format=%H %s",
                          "--max-count=%d" % GAP_LISTED,
                          "%s..%s" % (base_sha, sha))
        if rc == 0:
            commits = tuple(l.strip() for l in (out or "").splitlines()
                            if l.strip())
    # ONE OBSERVED BRANCH CANNOT CERTIFY THE OTHERS THAT WERE COUNTED. The
    # census counts every candidate that resolves, and the WINNER is only the
    # one with the largest gap AMONG LOCAL REFS. Asking the remote about the
    # winner alone and then publishing an aggregate "current" is a claim about
    # the whole census made from one member of it: with origin/main genuinely
    # current and origin/master advanced behind an unfetched clone, every local
    # count reads zero, no pair diverges, the winner verifies, and the apply
    # proceeds unacknowledged over a branch that IS behind.
    #
    # ONE ls-remote PER COUNTED CANDIDATE, and that is a cost this comment
    # names rather than hides. `ls-remote` CAN advertise several refs in one
    # invocation; this does not do that, so an origin carrying both main and
    # master issues TWO commands. Each carries the ordinary per-call timeout,
    # so a wedged remote costs that timeout per candidate rather than once.
    # It is left per-ref because the aggregate needs to attribute a refusal to
    # a NAMED ref, and grouping would return one answer set to unpick --
    # a deliberate trade, and one worth revisiting if the census ever counts
    # more than a handful. `ref_current` is True only when EVERY candidate
    # that could be asked answered the sha it was counted on; one refusal or
    # one mismatch makes the aggregate not-True.
    current, cur_why, cur_why_kind = _census_current(where, cands)
    return {"trunk": ref, "trunk_sha": sha, "behind": behind,
            "commits": commits, "truncated": behind > len(commits),
            "measured_refs": tuple((c["ref"], c["behind"]) for c in cands),
            "ref_age_s": _obs[0], "ref_observed_same": _obs[1],
            "ref_current": current, "ref_current_why": cur_why,
            # THE REASON IS CARRIED AS A CATEGORY, not recovered later by
            # reading the prose back: a renderer that sniffs its own sentence
            # for a substring silently changes meaning the next time the
            # sentence is edited, and the remedy depends on the category.
            "ref_current_reason": cur_why_kind,
            # THE CENSUS READ ONCE, NOT TWICE. Asking `git remote` again here
            # could FAIL where the earlier check succeeded, and this write put
            # `unknown: None` beside it unconditionally -- a contradictory
            # record: has_remote UNKNOWN, behind 0, nothing unknown, admit.
            "has_remote": has_remote, "measured_ts": now,
            "unknown": None}


# Older than this and a count from local refs is a LOWER BOUND rather than an
# answer: everything trunk gained since the last fetch is invisible here, and
# the direction of that error is always toward "nothing is missing".
FETCH_STALE_S = 3600


# EVERY REASON THE CENSUS CAN ANSWER WITH, and the remedy each one earns.
# The refusal reads this map and nothing else, so a pole the census learns to
# report and this map does not name would silently fall back to the generic
# line -- a hole shaped exactly like the defect the map was built to close.
# `CURRENCY_REASONS` is the census's whole vocabulary; `current` and `local`
# never reach a refusal, and the arms hold the two sets against each other.
CURRENCY_REASONS = ("current", "local", "moved", "unaskable", "unpublished",
                    "mixed", "none-asked")

#: WHAT A REMEDY MAY SAY. A remedy is a PREDICTION about a command the reader
#: has not run yet, and this map has now been wrong in both directions: an
#: unconditional `git fetch` for a remote that cannot be asked, and an
#: unconditional "a fetch will not help" for a reachable one nobody had
#: fetched. The third mistake is the same shape one level in — promising what
#: a fetch will ACHIEVE. A fetch creates a tracking ref for what the remote
#: publishes, which is not the same as creating a COUNTED one: an origin that
#: publishes only `develop` fetches perfectly and leaves this census with
#: nothing to count, so the same command refuses again with the same words.
#: So each sentence states the CONDITION under which its command helps, and
#: never that it will.
CURE_BY_REASON = {
    "moved": "Run `git fetch` and re-run: the counted ref is out of date "
             "with what its remote publishes.",
    "unaskable": "A fetch will NOT clear this one — the remote could not be "
                 "asked at all, so re-run when it can be, or compose on "
                 "purpose with the flag.",
    "unpublished": "The remote ANSWERED and does not publish the counted "
                   "branch, so no fetch can create it. `git fetch --prune` "
                   "drops the stale tracking ref, which clears this only if "
                   "another counted ref is current; otherwise count a branch "
                   "this remote publishes, or compose on purpose with the "
                   "flag.",
    "mixed": "The counted refs failed for DIFFERENT reasons, named above, "
             "and no one remedy covers them — read each reason and answer "
             "it.",
    "none-asked": "No counted ref is published by a remote here. This census "
                  "counts %s, so `git fetch` clears this only if the remote "
                  "publishes one of those — otherwise count a branch it does "
                  "publish, or compose on purpose with the flag."
                  % ", ".join(TRUNK_CANDIDATES),
}


def _census_current(where, cands):
    """(is EVERY counted candidate current, why-not) -- the aggregate answer.

    True only when every REMOTE-BACKED candidate this census counted answered
    the sha it was counted on. False when any of them has moved; None when any
    of them could not be asked, or when none of them could be asked at all --
    a verdict from zero observations is not a verdict.

    A PURELY LOCAL CANDIDATE IS OUT OF SCOPE, NOT UNKNOWN. `main` with no
    remote sibling carries no news from anywhere, so nobody can certify it and
    nobody needs to: the hazard this answers is a REMOTE-TRACKING ref that has
    fallen behind what its remote publishes, and a local branch is not one.
    Treating it as an unknown would make the aggregate None in every ordinary
    clone -- a refusal with no cure, which is the shape a guard must never
    take."""
    asked, unknowns, moved, local = [], [], [], []
    kinds = set()
    for c in cands:
        ref = c.get("ref") or ""
        if "/" not in ref:
            local.append(ref)
            continue
        head, why, kind = _remote_head(where, ref)
        if head is None:
            unknowns.append("%s (%s)" % (ref, why))
            kinds.add(kind)
            continue
        asked.append(ref)
        if head != str(c.get("sha")):
            moved.append(ref)
    if moved:
        # UNEQUAL SAYS DIFFERENT, NEVER AHEAD. The remote usually moved
        # FORWARD, which makes a directional sentence tempting -- but after a
        # rewind the remote is BEHIND the counted ref, and the same inequality
        # is all either case presents. Nothing here measures ancestry, so
        # nothing here may name one: the count was taken against something the
        # remote no longer publishes, which is enough to make it untrusted and
        # is all that was observed.
        return False, ("%s no longer matches what its remote publishes, so "
                       "this count was taken against something that is no "
                       "longer there — which way it moved is NOT measured "
                       "here" % ", ".join(moved)), "moved"
    if unknowns:
        return None, ("%s could not be checked against a remote (%s), so "
                      "whether this census is current is UNKNOWN"
                      % (("%d of %d counted refs" % (len(unknowns), len(cands)))
                         if len(cands) > 1 else "the counted ref",
                         "; ".join(unknowns))), (
            kinds.pop() if len(kinds) == 1 else "mixed")
    if not asked:
        return None, ("no counted ref is published by a remote (%s), so "
                      "nothing here can say whether this clone has missed news"
                      % (", ".join(local) or "none resolved")), "none-asked"
    return True, ("every remote-backed counted ref matches what its remote "
                  "publishes (%s)%s"
                  % (", ".join(asked),
                     "" if not local else
                     "; %s is local-only and carries no remote news"
                     % ", ".join(local))), "current"


def _remote_head(where, ref):
    """(sha the REMOTE publishes for this ref, why-not) — ASKED, not inferred.

    THIS REPLACES A CHAIN OF INFERENCES THAT COULD NOT REACH THE ANSWER, and
    task/2484 took three rounds to show why none of them could.
    A global FETCH_HEAD mtime is written by any fetch. A ref file's mtime is
    written by a repack. And FETCH_HEAD's CONTENT — the cure I offered in r1 —
    still cannot date one entry: `git fetch --append` refreshes the FILE while
    RETAINING an old line, so a months-old observation of `main` reads as
    minutes old; and the line names a branch, so a fetch of a DIFFERENT
    remote's `main` is credited to this one. Every one of those is provenance
    guessed from a timestamp.

    ONE NETWORK ROUND TRIP ANSWERS IT OUTRIGHT. `ls-remote --heads` asks the
    remote what the branch IS right now, downloads no objects, and needs no
    fetch. Compared against the sha the count was taken on, it says whether
    the count is EXACT or whether the local ref is behind what the remote
    publishes. That is the fact a timestamp can only be a proxy for.

    IT IS ALLOWED TO FAIL AND FAILING IS NOT AN ANSWER. Offline, or a remote
    that refuses, returns None with a reason; the caller then has no currency
    measurement and treats the count as a lower bound, which is the honest
    reading of local refs and the one the acknowledgement flow already
    handles. A refusal here must never read as "current"."""
    ref = str(ref or "")
    if "/" not in ref:
        return None, ("%s is a local branch, so no remote publishes it and "
                      "nothing here can say whether it is current"
                      % ref), "local"
    remote, branch = ref.split("/", 1)
    target = "refs/heads/" + branch
    rc, out, _ = _git(where, "ls-remote", "--heads", remote, target)
    if rc != 0:
        return None, ("the remote %s could not be asked what %s is (rc %s)"
                      % (remote, branch, rc)), "unaskable"
    for line in (out or "").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] == target:
            return parts[0].strip(), "", "current"
    # THE REMOTE ANSWERED. This is not an unreachable remote and must never
    # be rendered as one: the advertisement arrived and did not carry the
    # branch, which no fetch changes and no retry-when-reachable cures.
    return None, ("the remote %s does not publish %s"
                  % (remote, branch)), "unpublished"


def _has_remote(where):
    """True, False, or None when git could not answer.

    Without a remote there is no news to have missed, so the local refs are
    the complete answer rather than a snapshot of someone else's. But a
    FAILED `git remote` is not an absent remote: mapping the command's failure
    to False silently promotes an unreadable repo to "complete", which is the
    reassuring direction and the one that suppresses the staleness
    qualification entirely."""
    rc, out, _ = _git(where, "remote")
    if rc != 0:
        return None
    return bool((out or "").strip())


def _ref_observation(where, ref, sha):
    """(seconds since the counted ref was OBSERVED from its remote, matched)
    or (None, False) when no observation can be proved.

    A TIMESTAMP IS NOT AN OBSERVATION, AND THIS IS THE SECOND TRY AT SAYING SO.
    A global `FETCH_HEAD` mtime is written by ANY fetch, so an ordinary fetch
    of an unrelated branch makes a months-old trunk ref look fresh. Swapping
    that for the REF FILE's own mtime is no better: a repack rewrites ref
    storage without anyone observing the remote, and a successful no-change
    fetch need not rewrite the ref at all. Both are unproved assertions about
    provenance wearing a timestamp's authority.

    FETCH_HEAD'S CONTENT IS THE PROVENANCE, which is why this reads the file
    rather than stat-ing it alone. Every line names the branch and the remote
    it came from and the sha that was seen — `<sha>\t\tbranch 'main' of
    <url>` — so the counted branch APPEARING there is a record that this clone
    actually asked that remote about that branch, and the file's mtime dates
    it. A counted ref with no line is not stale and not fresh: it is
    UNOBSERVED, and the caller treats that as a lower bound rather than as an
    answer.

    IT ALSO REPORTS WHETHER THE OBSERVED SHA IS THE ONE COUNTED. Equal means
    the count was taken against exactly what the remote last said. Unequal
    means the ref moved after the observation, by a local operation or a
    later partial fetch, and the observation no longer vouches for it."""
    rc, out, _ = _git(where, "rev-parse", "--git-path", "FETCH_HEAD")
    path = (out or "").strip()
    if rc != 0 or not path:
        return None, False
    if not os.path.isabs(path):
        path = os.path.join(where, path)
    # THE REMOTE, NOT ONLY THE BRANCH NAME (task/2484 r2). Two remotes both
    # publish `main`, and matching the branch alone credited a successful fetch
    # of SOMEONE ELSE'S project as a record that this clone had asked ours.
    # When the configured URL cannot be read the line cannot be attributed at
    # all, so it does not count as an observation.
    #
    # AND THE FILE'S MTIME DOES NOT DATE THE LINE, which r1 of this module
    # claimed it did. `git fetch --append` refreshes FETCH_HEAD while RETAINING
    # earlier entries, so a months-old observation of `main` sits under a
    # minutes-old mtime with nothing in the file to tell them apart. The age is
    # returned for RENDERING and no decision rests on it any more: currency is
    # measured by asking the remote (`_remote_head`).
    remote, branch = (ref.split("/", 1) if "/" in ref else ("", ref))
    url = ""
    if remote:
        urc, uout, _ = _git(where, "remote", "get-url", remote)
        url = (uout or "").strip() if urc == 0 else ""
    if not url:
        return None, False
    try:
        age = max(0, int(time.time() - os.path.getmtime(path)))
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                parts = line.split("\t")
                if len(parts) < 3 or ("branch '%s'" % branch) not in parts[2]:
                    continue
                # THE WHOLE URL, NOT A PREFIX OF ONE. A substring test also
                # matches when the configured remote's URL is a PREFIX of some
                # other remote's -- `.../proj` against `.../proj-fork` -- so
                # the field after "of " must be the URL entire.
                if parts[2].split("of ", 1)[-1].strip() != url:
                    continue
                return age, parts[0].strip() == str(sha)
    except OSError:
        return None, False
    return None, False


def _ref_path(ref):
    """`origin/main` -> `remotes/origin/main`; `main` -> `heads/main`."""
    return ("remotes/" + ref) if "/" in ref else ("heads/" + ref)


def gap_is_a_lower_bound(gap):
    """Is this count too small to trust as a zero?

    THE ERROR HAS ONE DIRECTION. Everything trunk gained since the last fetch
    is invisible to a local ref, so an unfetched clone can only UNDER-report
    what a base is missing -- never over-report it. A nonzero count is
    therefore still actionable (it is at least that bad), but a ZERO from a
    stale clone is not an answer, it is a clone that has not heard the news.

    A REPO WITH NO REMOTE IS NOT STALE, IT IS COMPLETE. There is nothing to
    have missed, so the local refs are the whole truth and the strictness this
    predicate licenses would be a refusal with no cure -- the exact shape a
    guard must never take. `has_remote` is therefore a conjunct and not a
    detail: without it this fires on every repo that has no upstream at all."""
    if not gap or gap.get("has_remote") is False:
        return False                         # nothing to have missed
    # CURRENCY IS MEASURED OR IT IS NOT CLAIMED. `ref_current` is True only
    # when the remote was ASKED and answered the same sha this count was taken
    # on; that is the one condition under which a zero here is a zero. A None
    # means the question could not be put to the remote at all, and False
    # means the remote has moved past the ref that was counted -- a review's
    # falsifier for this predicate is exactly that a fresh MISMATCH must not
    # clear the default, which the old age test did clear.
    #
    # THE FILE'S AGE IS NO LONGER A CONJUNCT, because it never dated the thing
    # it was asked about: `git fetch --append` refreshes FETCH_HEAD while
    # retaining an old entry, so a stale observation reads as a fresh one. It
    # stays on the record as a rendered detail and is not load-bearing here.
    return gap.get("ref_current") is not True


def _age_words(seconds):
    if seconds is None:
        return "unknown"
    if seconds < 90:
        return "%ds" % seconds
    if seconds < 5400:
        return "%dm" % (seconds // 60)
    if seconds < 172800:
        return "%dh" % (seconds // 3600)
    return "%dd" % (seconds // 86400)


def render_stored_gap(record):
    """What a manifest RECORDED, rendered for a later reader — never recomputed.

    THE STORAGE WAS HONEST AND THE READER PATH WAS NOT. `render_base_gap` is
    called at preview and at the refusal, so the two moments that stop a
    compose disclose the gap and every moment AFTER it does not: a successful
    acknowledged compose prints its controls and its manifest path, and
    `--verify` prints content-proof rows, and neither says that this tip was
    composed onto a base that was knowingly behind. The fact is in the file
    and the reviewer never opens the file.

    IT RENDERS THE STORED NUMBER AND ASKS GIT NOTHING. Recomputing here would
    answer "how far has trunk moved since", which every historical compose
    fails loudly and which is the reason the measurement is stamped in the
    first place. A manifest written before this field existed has nothing to
    render and says so as UNMEASURED rather than as zero — absence of a record
    is not a record of absence."""
    if not isinstance(record, dict) or "base_gap" not in record:
        return ("  base gap: UNMEASURED — this manifest predates the field, "
                "which is not the same as a base that was current",)
    gap = record.get("base_gap") or {}
    when = gap.get("measured_ts")
    stamp = ("" if not when else
             " (measured %s)" % time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                              time.gmtime(when)))
    ack = record.get("base_behind_acknowledged")
    if gap.get("unknown"):
        head = "  base gap at compose time: UNKNOWN%s — %s" % (stamp,
                                                               gap["unknown"])
    elif gap.get("behind"):
        head = ("  base gap at compose time: %d commit(s) on %s were NOT in "
                "this base%s" % (gap["behind"], gap.get("trunk") or "?", stamp))
    else:
        head = ("  base gap at compose time: none — the base carried "
                "everything on %s%s" % (gap.get("trunk") or "?", stamp))
    lines = [head]
    if ack:
        lines.append("  the composer ACKNOWLEDGED the behind base with "
                     "--base-behind-ok, and that acknowledgement is part of "
                     "this record rather than of their shell history")
    elif gap.get("behind"):
        lines.append("  NO acknowledgement is recorded beside that gap")
    return tuple(lines)


def render_base_gap(gap):
    """The disclosure lines for a gap, or () when the base is current."""
    if not gap:
        return ()
    when = gap.get("measured_ts")
    stamp = ("" if not when else
             " (measured %s)" % time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                              time.gmtime(when)))
    if gap.get("unknown"):
        return ("  BASE GAP UNKNOWN%s: %s" % (stamp, gap["unknown"]),)
    ref_name = gap.get("trunk") or "the trunk ref"
    # SAY THE MEASUREMENT THAT DECIDED, AND ITS REMEDY. What makes this count a
    # lower bound is whether every counted ref still matches what its remote
    # publishes, and this explained the refusal with a FETCH_HEAD timestamp
    # that no longer decides anything -- prescribing `git fetch` whatever the
    # cause, which is the wrong remedy for an unreachable remote and
    # meaningless for a branch no remote publishes.
    why = gap.get("ref_current_why") or ""
    if not why:
        if gap.get("ref_age_s") is None:
            why = ("no fetch of %s is recorded in FETCH_HEAD, so whether this "
                   "clone has ever asked its remote about that branch is "
                   "UNPROVED -- not stale, not fresh, UNOBSERVED" % ref_name)
        else:
            # THE FILE'S CLOCK, LABELLED AS ONE. `git fetch --append` refreshes
            # FETCH_HEAD while retaining earlier entries, so this number is the
            # age of the FILE and not the date of any line in it.
            why = ("FETCH_HEAD was last written %s ago, which is the FILE'S "
                   "clock and not the date of any one entry in it"
                   % _age_words(gap.get("ref_age_s")))
    fetch = ("  %s. Under fast-forward this count is therefore a LOWER BOUND "
             "-- what trunk gained since is invisible here; after a REWIND a "
             "stale ref can OVER-count instead, so the direction holds while "
             "history only moves forward and not otherwise." % why)
    if not gap.get("behind"):
        lines = ["  base carries everything on %s (%s) -- nothing was "
                 "missing%s" % (gap["trunk"], (gap["trunk_sha"] or "")[:12],
                                stamp)]
        if gap_is_a_lower_bound(gap):
            lines.append(fetch)
        elif gap.get("ref_current") is True:
            # A VERIFIED ZERO AND AN UNVERIFIED ONE READ IDENTICALLY OTHERWISE,
            # and only one of them is a reason to stop worrying. The reader
            # gets to see WHICH, on the same line-for-line surface where a
            # refusal would have explained itself.
            lines.append("  %s." % why)
        return tuple(lines)
    lines = ["  BASE IS BEHIND: %d commit(s) on %s were NOT in this base%s."
             % (gap["behind"], gap["trunk"], stamp)]
    # ONLY SAY SNAPSHOT WHEN IT IS ONE. A remote-tracking ref moves only on
    # fetch, so the caveat is load-bearing there and simply false about a
    # local branch — and a caveat that is false somewhere is a caveat readers
    # learn to skip everywhere.
    if gap["trunk"].startswith("origin/"):
        lines.append("  %s is a remote-tracking SNAPSHOT that moves only on "
                     "fetch, so run `git fetch` before treating this count as "
                     "current." % gap["trunk"])
    refs = gap.get("measured_refs") or ()
    if len({behind for _ref, behind in refs}) > 1:
        lines.append("  the trunk refs DISAGREE (%s) — this is the largest, "
                     "because one of them is stale and which one is not "
                     "knowable from here; under-reporting is the failure that "
                     "ships."
                     % ", ".join("%s=%d" % (r, b) for r, b in refs))
    for entry in gap.get("commits") or ():
        lines.append("      - %s" % entry[:100])
    if gap.get("truncated"):
        lines.append("      ... %d more not listed"
                     % (gap["behind"] - len(gap.get("commits") or ())))
    # THE SAME CAVEAT THE ZERO GETS, ONCE, AFTER THE EVIDENCE. A nonzero count
    # needs it MORE, not less: a reader told "1 commit behind" rebases over
    # that one commit and believes they are level, when an unverified ref means
    # the real number is AT LEAST one.
    if gap_is_a_lower_bound(gap):
        lines.append(fetch)
    return tuple(lines)


def plan(root, room, base, cars):
    """(rows, error) — what a compose WOULD pick, touching nothing.

    Read-only is the default because the actuator is destructive: it resets a
    room hard. `helm landgate` already draws this line for the land predicate
    — asking is always safe, the actuator is a separate bar — and a composer
    that could only be understood by running it would be reviewable only by
    the person willing to lose a room.
    """
    room_path = _lanes.lane_path(root, room)
    if not os.path.isdir(room_path):
        return None, "no room at %s — claim it first" % room_path
    base_sha = _resolve(room_path, base)
    if not base_sha:
        return None, "base %r names no commit in the room" % base
    rows = []
    for car in cars:
        lane_ref = _lanes.lane_branch(car["lane"])
        if not _resolve(room_path, lane_ref):
            return None, "lane %r has no branch %s" % (car["lane"], lane_ref)
        picks, err = cars_to_pick(room_path, base_sha, lane_ref)
        if picks is None:
            return None, "%s: %s" % (car["lane"], err)
        rows.append({"row": car["row"], "lane": car["lane"],
                     "lane_ref": lane_ref, "picks": picks})
    return {"room": room, "room_path": room_path, "base": base_sha,
            "base_gap": base_gap(room_path, base_sha), "cars": rows}, None


def compose(root, room, base, cars, now=None, proposal=None):
    """(record, error) — reset the room to the base, pick every car, and
    return the evidence. THE CALLER HAS ALREADY DECIDED TO ACT.

    THE CHECKED PROPOSAL IS THE ONE THAT RUNS, which is why `proposal` is a
    parameter rather than something this function always computes. A caller
    that gates on a preflight and then lets the actuator RE-PLAN has two
    different measurements: the one it refused on and the one it acted on.
    Measured as a live schedule, not as unavoidable drift — the first plan
    sees a current base, another worktree advances the shared trunk, the
    SECOND plan itself sees the gap, and the reset and the picks proceed
    anyway because nothing re-reads it. The actuator saw a disqualifying
    result and ignored it. Passing the checked proposal in makes the base,
    the cars, the count and the acknowledgement one coherent snapshot.

    The record is returned rather than written so the writer stays one door
    (`write_manifest`) and a caller that wants to inspect before persisting
    can. Every field is measured from git after the act, never predicted
    before it: the result tree is read back from the room, so a pick that
    silently produced something else cannot be recorded as what was intended.
    """
    if proposal is None:
        proposal, err = plan(root, room, base, cars)
        if err:
            return None, err
    where, base_sha = proposal["room_path"], proposal["base"]
    gap = proposal.get("base_gap")
    rc, _, gerr = _git(where, "reset", "--hard", base_sha)
    if rc != 0:
        return None, "could not reset the room to %s: %s" % (base_sha[:12],
                                                             gerr)
    records = []
    for car in proposal["cars"]:
        for sha in car["picks"]:
            # THE SOURCE SIDE'S PRE-IMAGE, which is what makes "the same
            # change" checkable at all. The RESULT side's pre-image needs no
            # field: it is the first parent of result_commit and a verifier
            # derives it. A root car has no parent tree and cannot be
            # replayed, so it says so rather than storing an empty string.
            # `--allow-empty` PLUS `--empty=keep`, AND THEY ARE DEFECT FIXES
            # RATHER THAN CONVENIENCE. Measured: `git cherry` reports a MESSAGE-ONLY
            # commit as PLUS, so it reaches this loop, and a plain
            # cherry-pick then fails it with "The previous cherry-pick is now
            # empty" — which the branch below records as
            # exact-review-required and which STOPS THE WHOLE COMPOSE. A
            # message-only commit is an ordinary thing to have on a lane, and
            # the same shape already cost this project a frozen patch-id
            # backfill on task/2049.
            #
            # CARRYING IT IS HONEST AND STAYS VISIBLE: the car keeps its row,
            # its message and its place in the sequence, its result tree
            # EQUALS its parent's, and the replay verifies exactly that. The
            # other way to reach "now empty" is a car whose change an EARLIER
            # car in this same compose already made; that too is recorded
            # rather than hidden, and a reader sees it as a result tree that
            # did not move.
            rc, _, gerr = _git(where, "cherry-pick", "--allow-empty",
                                "--empty=keep", sha)
            if rc != 0:
                # A CONFLICT IS NOT A FAILURE OF THIS VERB, it is a car that
                # cannot be verified by replay. The room is left exactly as
                # git left it — mid-pick — because resolving it is the
                # composer's judgement and silently aborting would discard
                # work they may want.
                # NOTHING IS RECORDED FOR IT. An address to a commit that
                # was never made is not an address, and a status word saying
                # so would be a CLAIM in a record that holds only addresses.
                return ({"result_tip": "", "cars": records,
                         "conflict_at": sha,
                         "detail": "cherry-pick did not apply cleanly: %s"
                                   % (gerr or "conflict")}, None)
            head = _resolve(where, "HEAD")
            if not head:
                return None, ("the pick of %s produced no readable HEAD"
                              % sha[:12])
            records.append({"row": car["row"], "lane": car["lane"],
                            "source_commit": sha, "result_commit": head})
    tip = _resolve(where, "HEAD")
    # THE GAP IS RECORDED, NEVER RECOMPUTED BY A READER. It answers "what was
    # already on trunk when this base was chosen", and asking it again later
    # answers "how far has trunk moved since" — a different question that
    # every historical manifest fails loudly. Stored with its own timestamp so
    # a reader can see which moment it describes.
    return ({"result_tip": tip or "", "base": base_sha, "room": room,
             "base_gap": gap, "cars": records,
             "composed_ts": int(now or time.time())}, None)


def controls(root, room, base, cars, record=None):
    """(rows, tip) — the post-compose rungs, each naming what it measured.

    UNANSWERABLE IS ITS OWN VERDICT. Every rung below returns None rather
    than False when git could not answer, and a caller must not read that as
    a pass: "the control did not run" and "the control ran and was satisfied"
    are the two states a fold most needs told apart.

    `record` IS THE COMPOSE'S OWN OUTPUT and the sequential replay runs
    against it. Without it the replay cannot run at all, and this says so
    rather than quietly reporting a shorter, greener list.
    """
    where = _lanes.lane_path(root, room)
    tip = _resolve(where, "HEAD")
    rows = []
    for car in cars:
        lane_ref = _lanes.lane_branch(car["lane"])
        rows.append(("cherry-room-lane:" + car["lane"],)
                    + _all_minus(where, "HEAD", lane_ref))
    rows.append(("cherry-room-main",) + _all_minus(where, "HEAD", base))
    rows.append(("porcelain",) + worktree_is_clean(where))
    if record and record.get("cars"):
        rows.extend(replay_chain(where, record["cars"], base, "HEAD"))
    else:
        rows.append(("replay-chain", None,
                     "no compose record was supplied, so nothing was "
                     "replayed — this is not a passed control"))
    return rows, tip


def _deleted_paths(where, a, b):
    """(set, error) — paths a range DELETES, by git's own filter."""
    rc, out, err = _git(where, "diff", "--diff-filter=D", "--name-only",
                        a, b)
    if rc != 0:
        return None, (err or "git diff could not answer")
    return set(l for l in (out or "").splitlines() if l.strip()), None


def deletions_are_accounted_for(where, base, sources):
    """(True/False/None, detail) — is every path the ROOM deletes deleted by
    some CAR?

    THE DIRECTION IS DELIBERATE AND THE OTHER ONE WOULD BE WRONG. Room
    deletions must be a SUBSET of the cars' deletions; they need not be equal,
    because one car may delete a file a later car recreates and the room then
    correctly shows no deletion at all. What this refuses is the case with no
    innocent reading: the composed tip is missing a file and NO reviewed car
    asked for that.
    """
    room, err = _deleted_paths(where, base, "HEAD")
    if room is None:
        return None, err
    asked = set()
    for sha in sources:
        got, err = _deleted_paths(where, sha + "^", sha)
        if got is None:
            return None, "%s: %s" % (sha[:12], err)
        asked |= got
    orphan = sorted(room - asked)
    if orphan:
        return False, ("%d path(s) deleted by no car, first %r"
                       % (len(orphan), orphan[0]))
    return True, "%d room deletion(s), all asked for by a car" % len(room)


def _review_snapshot():
    """One canonical ledger instant for every car in a proof stage."""
    from . import dispatches
    try:
        rows, verdicts, unavailable = dispatches.snapshot_with_verdicts()
    except Exception as exc:                    # noqa: BLE001 — unreadable
        return None, "the dispatch ledger could not be read: %s" % exc
    if unavailable:
        return None, "the dispatch ledger could not be read: %s" % unavailable
    return (rows or {}, verdicts or {},
            dispatches.gate_epoch(rows, verdicts)), None


def _reviewed_covers(where, row, source, authority=None):
    """(True/False/None, detail) — did an AUTHORIZING APPROVE cover this car?

    The manifest supplies only a row ADDRESS. Fold time replays the canonical
    dispatch snapshot, proves the row is an accepted review verdict, asks the
    land owner layer for its recorded tier/gate authority, and only then asks
    Git whether the reviewed tip contains the source commit. CONCUR is not a
    softer approval: its canonical meaning is that it authorises nothing.
    """
    from . import dispatches, landreq
    if authority is None:
        authority, err = _review_snapshot()
        if err:
            return None, err
    rows, verdicts, epoch = authority
    projected = rows.get(row)
    if projected is None:
        return False, "row %s is not in the dispatch ledger" % str(row)[:12]
    index = dispatches.verdict_index(verdicts, row)
    authorised, why = landreq.approval_authority(
        projected, index=index, epoch=epoch, repo=where)
    if authorised is not True:
        return authorised, "row %s: %s" % (str(row)[:12], why)
    reviewed = str(projected.get("reviewed_tip") or "").strip()
    if not reviewed or not _resolve(where, reviewed):
        return None, ("row %s reviewed %s, which is not a readable commit "
                      "here — a shrug, not a no"
                      % (str(row)[:12], reviewed[:12] or "?"))
    # THE SECOND DOOR, INDEPENDENT OF THE FIRST ON PURPOSE. dispatches.send
    # refuses a dirty-tree snapshot at the moment a row is written, which does
    # nothing for rows already on the ledger — and the two instances that
    # reached trunk were both already written. This one fires at fold time on
    # whatever the manifest names, so a pre-existing row cannot ride in. It is
    # a hard False rather than a shrug: an unreadable tip is UNKNOWN above,
    # but a tip we CAN read and that says it is a snapshot is a decided no.
    snapshot = dispatches.snapshot_tip_refusal(where, reviewed)
    if snapshot:
        return False, "row %s: %s" % (str(row)[:12], snapshot)
    rc, _out, err = _git(where, "merge-base", "--is-ancestor",
                         source, reviewed)
    if rc == 0:
        return True, ("row %s APPROVE at %s contains %s; %s"
                      % (str(row)[:12], reviewed[:12], source[:12], why))
    if rc == 1:
        return False, ("row %s reviewed %s, which does NOT contain %s"
                       % (str(row)[:12], reviewed[:12], source[:12]))
    return None, ("git could not decide whether row %s tip %s contains %s: %s"
                  % (str(row)[:12], reviewed[:12], source[:12],
                     err or "ancestry probe failed"))


def in_scope(where, project, tip):
    """(True/False/None, detail) for composition-proof activation.

    False means the stage does not exist for this tip: never activated, or
    already historical at activation. None means activation exists but its
    record or ancestry could not be read; that is an UNKNOWN stage and must
    refuse. Git rc 1 is the only measured not-ancestor answer.
    """
    state, trunk = activation_state(project)
    if state == ACTIVATION_INACTIVE:
        return False, "composition proof has never been activated"
    if state == ACTIVATION_UNKNOWN:
        return None, ("composition-proof activation exists but is unreadable, "
                      "incomplete, mismatched, or deleted")
    rc, _out, err = _git(where, "merge-base", "--is-ancestor", tip, trunk)
    if rc == 0:
        return False, ("%s was already reachable from activation trunk %s"
                       % (str(tip)[:12], trunk[:12]))
    if rc == 1:
        return True, ("%s is newer than activation trunk %s"
                      % (str(tip)[:12], trunk[:12]))
    return None, ("git could not decide whether %s predates activation %s: %s"
                  % (str(tip)[:12], trunk[:12],
                     err or "ancestry probe failed"))


def bounded_members(where, record, raw_rows):
    """Validate explicit bounded groups; ordinary cars keep _reviewed_covers.

    A discriminator is not authority. Full source coverage, measured effects,
    event-time reviewer authority and rich carried content are re-derived from
    this stage's SAME canonical raw snapshot. Reader is never environment gated.
    """
    from . import compose_contract
    if "compose_land_version" not in record and "bounded_rows" not in record:
        return {}, None
    ids = record.get("bounded_rows")
    if type(record.get("compose_land_version")) is not int \
            or record["compose_land_version"] != 1 \
            or record.get("dry_run") is not False \
            or not isinstance(ids, list) or not ids \
            or any(not isinstance(rid, str) or not rid for rid in ids) \
            or len(ids) != len(set(ids)):
        return None, "bounded composition discriminator or row census malformed"
    members = {}
    for rid in ids:
        positions = [i for i, car in enumerate(record["cars"]) if car["row"] == rid]
        if not positions or positions != list(range(positions[0], positions[-1] + 1)):
            return None, "bounded row cars missing or interleaved"
        row = raw_rows.get(rid)
        if not isinstance(row, dict):
            return None, "bounded row missing from canonical dispatch snapshot"
        member, err = compose_contract.group_member(
            where, row, raw_rows, [record["cars"][i] for i in positions])
        if err:
            return None, err
        members[rid] = member
    return members, None


def bounded_gate(where, tip, receipt, consuming=None):
    """Explicit final whole-tree authority; compose-before-gate stays pending.

    `consuming` is the CHECKOUT the row records, when the caller has it. `where`
    is documented as the tree and one caller legitimately hands it the
    repository (`compose_contract` works in `repo_id`), so the declared-command
    clause was answered from a shared admin dir that covers a repository root
    and every linked worktree of it — ambiguous exactly where a root and a lane
    declare different commands. The row's own coordinate answers it.
    """
    from . import gate, landgate, vcs
    tree = _tree_of(where, tip)
    repo = vcs.backend(where).common_dir(where)
    if not tree or not repo:
        return None, "composed tree/repository unreadable"
    # `repo` IS THE SHARED ADMIN DIR AND `where` IS THE TREE. The authority door
    # wants the repository; the declared-command origin clause wants the location
    # whose declaration would RUN, and a common dir has no declaration of its own —
    # handing it that value skipped a worktree's exact-path precedence silently.
    state, token, why = gate.bind(receipt, tip, repo_id=repo, need=gate.NEED_SUITE,
                                 consuming_repo=consuming or where)
    if state != "VERIFIED":
        return False, "whole composed-tree suite gate pending/refused: " + str(why)
    state, why = landgate.gate_binds_tree(token, tree, repo=repo, tip=tip)
    return state == landgate.OK, why


def record_proof(where, record, authority=None, gate_ref=None, consuming=None):
    """Complete car authority shared by fold and scoped live close.

    The locked writer supplies its own (raw rows, verdicts, epoch); it must not
    recapture a different ledger instant. Other callers capture exactly once.
    Historical close-event replay retains its captured-proof contract instead.
    """
    # `dispatches` IS A FUNCTION-SCOPE IMPORT IN THIS MODULE and the bounded
    # branch below needs it. The same omission in the sibling door shipped
    # fail-open earlier today: the NameError was swallowed and the guard
    # admitted every case it existed to refuse while reading as a pass.
    from . import dispatches
    tip = record["result_tip"]
    rows = replay_chain(where, record["cars"], record.get("base") or "", tip)
    if authority is None:
        authority, err = _review_snapshot()
        if err:
            rows.append(("review-authority", None, err))
            return rows, None
    members, err = bounded_members(where, record, authority[0])
    if err:
        rows.append(("bounded-review", False, err))
        return rows, None
    for car in record["cars"]:
        if car["row"] in members:
            continue
        rows.append(("reviewed:%s" % (car.get("row") or "?"),)
                    + _reviewed_covers(where, car["row"],
                                       car["source_commit"], authority))
    for rid in members:
        # THE BOUNDED PATH SKIPS _reviewed_covers ENTIRELY — the loop above
        # `continue`s on every member — so the snapshot refusal there never
        # sees a bounded car, and a dirty-tree snapshot riding as a member
        # would be admitted with an unconditional True. The bounded range
        # establishes that the CARS are covered; it says nothing about whether
        # the reviewed object is a commit of anyone's work.
        bounded_row = (authority[0] or {}).get(rid) or {}
        bounded_tip = str(bounded_row.get("reviewed_tip") or "").strip()
        snapshot = dispatches.snapshot_tip_refusal(where, bounded_tip)
        if snapshot:
            rows.append(("bounded-review:" + rid, False,
                         "row %s: %s" % (str(rid)[:12], snapshot)))
            continue
        rows.append(("bounded-review:" + rid, True,
                     "complete immutable range, recorded authority and carried effects bind"))
    if members:
        rows.append(("bounded-suite",)
                    + bounded_gate(where, tip, gate_ref, consuming=consuming))
    return rows, members


def composition_proof(where, project, tip, gate_ref=None):
    """[(name, ok, detail)] for a tip in scope, or None when it is not.

    THE SEPARATE STAGE. It is deliberately NOT one of `foldcheck.check`'s
    five rungs: that five-rung shape is a public contract other lanes assert
    on, and quietly making it six turned eleven arms red in one gate.
    """
    scoped, detail = in_scope(where, project, tip)
    if scoped is False:
        return None
    if scoped is None:
        return [("activation", None, detail)]
    record = read_manifest(project, tip)
    if record is None:
        return [("manifest", False,
                 "%s is in scope for composition proof and carries no "
                 "readable manifest — its cars cannot be replayed, and a "
                 "green gate is not a review" % tip[:12])]
    rows, _members = record_proof(where, record, gate_ref=gate_ref)
    return rows


_USAGE = (
    "usage: helm compose --room LANE --base REF --car ROWID:LANE [--car ...] "
    "[--apply] [--repo PATH]\n"
    "       helm compose --verify TIP [--repo PATH]\n"
    "       helm compose --activate TRUNK [--repo PATH]\n"
    "  READ-ONLY WITHOUT --apply: prints the cars it WOULD pick and touches "
    "nothing, because the actuator resets a room hard and a verb that can "
    "only be understood by running it is reviewable only by someone willing "
    "to lose a room.\n"
    "  --apply RESETS the room to --base and cherry-picks every plus commit "
    "of every car, then records per car only the review-row, source-commit, "
    "and result-commit addresses under the project home keyed by the composed "
    "tip. Fold time re-derives every tree, parent, replay and approval claim.\n"
    "  --activate atomically binds enforcement to one trunk and is write-once. "
    "Activation and mutation require HELM_WORK_INTEGRATOR=1, the same door "
    "`helm work` uses for the integrator's own overrides.")


def _cars_from_args(values):
    """(cars, error) from ROWID:LANE strings.

    BOTH HALVES ARE REQUIRED AND NEITHER IS GUESSED. A car with no row id is
    a car with no reviewer, which is the exact state task/987 says nothing
    refuses — so it is refused HERE, at the cheapest possible moment, rather
    than discovered by a fold rung after the compose already happened.
    """
    cars = []
    for raw in values:
        row, sep, lane = str(raw or "").partition(":")
        if not (sep and row.strip() and lane.strip()):
            return None, ("--car %r is not ROWID:LANE — a car with no row id "
                          "is a car with no reviewer" % raw)
        cars.append({"row": row.strip(), "lane": lane.strip()})
    return cars, None


def render(rows):
    """One line per control, naming what it MEASURED. UNKNOWN is its own
    word: a rung git could not answer is not a rung that passed."""
    out = []
    for name, ok, detail in rows:
        mark = "ok  " if ok is True else ("STOP" if ok is False else "????")
        out.append("%s  %-34s %s" % (mark, name, detail))
    return "\n".join(out)


def cmd_compose(args):
    """helm compose — the fold that produces its own evidence."""
    import sys
    args = list(args or [])
    if not args:
        print(_USAGE)
        return 2
    if len(args) == 1 and args[0] in ("-h", "--help", "help"):
        print(_USAGE)
        return 0

    # A CLOSED-SET TAIL, REFUSED BEFORE ANY WORK RUNS. `--apply` resets a
    # room hard, so a junk tail must never reach it — `helm seat down codex
    # --bogus` once stopped the seat and exited 0, which is the incident the
    # fleet-wide apply-reader census exists to prevent.
    #
    # `cli.guard_tail` is the usual instrument and CANNOT express this verb:
    # `--car` is a REPEATED valued flag and guard_tail refuses a duplicate
    # valued flag by design. So the closed set is spelled here and probed by
    # an arm, which is what the census's exemption clause asks for.
    _FLAGS = ("--apply", "--base-behind-ok")
    _VALUED = ("--room", "--base", "--repo", "--car", "--verify",
               "--activate")
    seen = set()
    index = 0
    while index < len(args):
        token = args[index]
        if token in _FLAGS:
            if token in seen:
                print("helm compose: duplicate %s is ambiguous" % token,
                      file=sys.stderr)
                return 2
            seen.add(token)
            index += 1
            continue
        if token in _VALUED:
            if index + 1 >= len(args) or args[index + 1].startswith("-"):
                print("helm compose: %s wants a value" % token,
                      file=sys.stderr)
                return 2
            if not args[index + 1]:
                # AN EMPTY VALUE IS A CALLER ERROR, NOT A MODE. Every
                # downstream reader of these flags is a string and an empty
                # string is FALSY, so an empty value satisfies a "wants a
                # value" check and then reads as ABSENT to every truthiness
                # test after it: the complete mode evaporates and the tail
                # falls through to the composition path, where --apply resets
                # a room hard. Refused here, before any work.
                print("helm compose: %s was given an empty value; a mode is "
                      "chosen by naming the flag, and an empty value is a "
                      "caller error rather than a request for another mode"
                      % token, file=sys.stderr)
                return 2
            if token != "--car" and token in seen:
                print("helm compose: duplicate %s is ambiguous" % token,
                      file=sys.stderr)
                return 2
            seen.add(token)
            index += 2
            continue
        print("helm compose: unknown token %r — refusing before any work; "
              "--apply resets a room hard and a junk tail must never reach "
              "it" % token, file=sys.stderr)
        return 2

    def opt(name, default=None):
        return args[args.index(name) + 1] if name in args \
            and args.index(name) + 1 < len(args) else default

    car_args = [args[i + 1] for i, a in enumerate(args)
                if a == "--car" and i + 1 < len(args)]
    verify, activate_ref = opt("--verify"), opt("--activate")
    # A MODE IS SELECTED BY THE FLAG BEING NAMED, NEVER BY ITS VALUE BEING
    # TRUTHY. The two questions are different -- "was this mode requested" and
    # "what did the caller pass" -- and answering the first out of the second
    # is what let a complete mode disappear when its value was empty. The
    # parse loop above now refuses an empty value, so these agree today; they
    # are kept separate so that they cannot silently stop agreeing.
    verify_given = "--verify" in args
    activate_given = "--activate" in args
    room, base = opt("--room"), opt("--base", "origin/main")
    repo = opt("--repo") or os.getcwd()
    apply_it = "--apply" in args
    base_behind_ok = "--base-behind-ok" in args
    compose_args = bool(room or car_args or "--base" in args or apply_it
                        or base_behind_ok)
    if verify_given and (activate_given or compose_args):
        print("helm compose: --verify is a complete mode and cannot be mixed "
              "with activation or composition options", file=sys.stderr)
        return 2
    if activate_given and compose_args:
        print("helm compose: --activate is a complete mode and cannot be mixed "
              "with composition options", file=sys.stderr)
        return 2
    if verify_given:
        # THE READER HALF, so the proof has a caller and is not built-not-
        # wired. The fold entry asks this about a composed tip; it spends no
        # room and needs no lane.
        root = _lanes.find_root(opt("--repo") or os.getcwd())
        project = project_for(root) if root else None
        if not project:
            print("helm compose: not a registered project", file=sys.stderr)
            return 2
        where = root
        rows = composition_proof(where, project, verify)
        if rows is None:
            print("helm compose: %s is NOT IN SCOPE for composition proof — "
                  "no activation record, or it is already reachable from the "
                  "trunk manifests became required at. THAT IS THE ABSENCE OF "
                  "A STAGE, not a pass." % verify[:12])
            return 0
        print("helm compose: composition proof for %s" % verify[:12])
        print(render(rows))
        # THE REVIEWER'S PATH IS THIS ONE, so the stored measurement is
        # rendered here or it is rendered nowhere a reviewer looks.
        for line in render_stored_gap(read_manifest(project, verify)):
            print(line)
        if any(ok is False for _n, ok, _d in rows):
            return 1
        if any(ok is None for _n, ok, _d in rows):
            print("\n  UNKNOWN is not consent: something could not be "
                  "measured, which is a different answer from a car that "
                  "failed.", file=sys.stderr)
            return 1
        return 0
    if activate_given:
        root = _lanes.find_root(repo)
        project = project_for(root) if root else None
        if not project:
            print("helm compose: not a registered project", file=sys.stderr)
            return 2
        if home.env("WORK_INTEGRATOR") != "1":
            print("helm compose: activation changes fold enforcement and is "
                  "gated by HELM_WORK_INTEGRATOR=1", file=sys.stderr)
            return 2
        path, err = activate(project, root, activate_ref)
        if err:
            print("helm compose: " + err, file=sys.stderr)
            return 1
        state, trunk = activation_state(project)
        print("helm compose: composition proof ACTIVE at %s\n  activation: %s"
              % ((trunk or "?")[:12], path))
        return 0 if state == ACTIVATION_ACTIVE else 1
    if not (room and car_args):
        print(_USAGE, file=sys.stderr)
        return 2
    cars, err = _cars_from_args(car_args)
    if err:
        print("helm compose: " + err, file=sys.stderr)
        return 2

    root = _lanes.find_root(repo)
    if not root:
        print("helm compose: %r is not inside a git repo" % repo,
              file=sys.stderr)
        return 2
    project = project_for(root)
    if not project:
        print("helm compose: %s is not a registered project, so a manifest "
              "written for it would land where nothing looks — run `helm "
              "sync` or name the repo with --repo" % root, file=sys.stderr)
        return 2

    if not apply_it:
        proposal, err = plan(root, room, base, cars)
        if err:
            print("helm compose: " + err, file=sys.stderr)
            return 1
        print("helm compose: PLAN for room %s onto %s — nothing was touched"
              % (proposal["room"], proposal["base"][:12]))
        for line in render_base_gap(proposal.get("base_gap")):
            print(line)
        for car in proposal["cars"]:
            print("  row %s  lane %s  %d commit(s) to pick"
                  % (car["row"], car["lane"], len(car["picks"])))
            for sha in car["picks"]:
                print("      + %s" % sha[:12])
        print("\n  --apply performs it and writes the manifest. This plan "
              "proves what WOULD be picked, never that the picks apply.")
        return 0

    # THE ACTUATOR DOOR, and it is the one `helm work` already uses rather
    # than a second authority this module invented. An override that lives in
    # only one module is an override nobody can audit in one place.
    # `home.env` PREFIXES. It looks up HELM_<name>, so the argument is
    # WORK_INTEGRATOR and the variable an operator exports is
    # HELM_WORK_INTEGRATOR — the same one `helm work`'s shell guard reads as
    # $HELM_WORK_INTEGRATOR. Passing the full name here asks for
    # HELM_HELM_WORK_INTEGRATOR, which nothing ever sets, and the door then
    # refuses a caller who HAS declared themselves. The arm below is what
    # found it: the refusal was indistinguishable from a correct one.
    if home.env("WORK_INTEGRATOR") != "1":
        print("helm compose: --apply RESETS the room hard and is gated by "
              "HELM_WORK_INTEGRATOR=1, the same door `helm work` uses. "
              "Without --apply this verb is read-only and always safe.",
              file=sys.stderr)
        return 2

    # THE BASE IS CHECKED BEFORE THE ROOM IS RESET, because the reset is the
    # destructive half and a refusal afterwards would have already spent it.
    gate_proposal, gerr = plan(root, room, base, cars)
    if gerr:
        print("helm compose: " + gerr, file=sys.stderr)
        return 1
    gap = gate_proposal.get("base_gap") or {}
    # A ZERO FROM A CLONE THAT HAS NOT FETCHED IS NOT A ZERO. The error has
    # one direction -- everything trunk gained since the last fetch is
    # invisible -- so an unfetched clone can only ever say "nothing is
    # missing". That refusal is cleared by one command rather than by a
    # judgement, which is why it names the command instead of only the flag.
    stale_zero = (not gap.get("behind") and not gap.get("unknown")
                  and gap_is_a_lower_bound(gap))
    if (gap.get("behind") or gap.get("unknown") or stale_zero) \
            and not base_behind_ok:
        for line in render_base_gap(gap):
            print(line, file=sys.stderr)
        print("helm compose: REFUSING to compose onto this base. A manifest "
              "proves what the tree CONTAINS and is silent about what its "
              "base is MISSING, so a reviewer who checks the tree against the "
              "manifest gets a clean confirmation either way — which is how "
              "three shipped features left the running proxy for nineteen "
              "hours with two approvals on the compose.\n"
              "  Composing onto an older base on purpose is a real thing to "
              "want, so this is not a wall: pass --base-behind-ok and the "
              "acknowledgement is RECORDED IN THE MANIFEST, where the "
              "reviewer reads it.\n"
              "  UNKNOWN is refused for the same reason a failed control is: "
              "it is not a measurement that found nothing.",
              file=sys.stderr)
        if stale_zero:
            # THE REMEDY FOLLOWS THE REASON, or it contradicts the line above
            # it. Restating the FETCH_HEAD timestamp as the cause and
            # prescribing `git fetch` unconditionally is wrong wherever the
            # sentence printed a line earlier says the remote could not be
            # ASKED, or had moved, or publishes nothing here: `git fetch` cures
            # exactly one of those and is noise or a lie for the rest.
            why_now = gap.get("ref_current_why") or ""
            # NONE IS NOT ONE CAUSE, and each of its causes has a different
            # remedy: an unconditional `git fetch` is a lie for a remote that
            # cannot be asked, and an unconditional "a fetch will not help" is
            # a lie for a clone that has simply never fetched a reachable one.
            # The category comes from the census, which is the only place that
            # KNOWS which pole it hit.
            cure = CURE_BY_REASON.get(
                gap.get("ref_current_reason"),
                "Re-run once the count can be checked against a remote.")
            # The reason is written as a clause and the cure as a sentence,
            # so the join has to supply the stop the clause does not carry.
            reason = why_now or "currency is UNKNOWN"
            if not reason.endswith("."):
                reason += "."
            print("  THE COUNT IS ZERO AND IT IS A LOWER BOUND: %s %s The "
                  "flag is for composing onto an older base ON PURPOSE, not "
                  "for skipping the measurement."
                  % (reason, cure), file=sys.stderr)
        return 2

    record, err = compose(root, room, base, cars, proposal=gate_proposal)
    if err:
        print("helm compose: " + err, file=sys.stderr)
        return 1
    if record.get("conflict_at"):
        print("helm compose: STOPPED at %s — the pick did not apply cleanly "
              "and the room is left exactly as git left it, mid-pick, "
              "because resolving it is your judgement and aborting would "
              "discard it. That car is recorded %s: a conflicted pick has no "
              "deterministic replay, so the composed tip needs exact review."
              % (record["conflict_at"][:12], EXACT_REVIEW_REQUIRED))
        return 1

    rows, tip = controls(root, room, base, cars, record=record)
    sources = [c["source_commit"] for c in record["cars"]
               if c.get("source_commit")]
    rows.append(("deletions",)
                + deletions_are_accounted_for(
                    _lanes.lane_path(root, room), record["base"], sources))
    print("helm compose: composed %d car(s) into %s at %s"
          % (len(record["cars"]), room, (tip or "?")[:12]))
    print(render(rows))
    if any(ok is not True for _n, ok, _d in rows):
        print("\n  THE MANIFEST IS NOT WRITTEN. A control that did not pass "
              "means the room is not what these cars produce, and evidence "
              "for a tip nobody should land is worse than none — a later "
              "reader would find a record and stop asking.", file=sys.stderr)
        return 1
    # THE ACKNOWLEDGEMENT BELONGS IN THE RECORD, not only in the shell
    # history of whoever typed it. A composer who knowingly used an older base
    # has made a judgement, and the reviewer is the person who needs to read
    # it back.
    if base_behind_ok:
        record["base_behind_acknowledged"] = True
    path = write_manifest(project, record)
    if not path:
        print("\n  helm compose: the manifest could not be written — the "
              "compose HAPPENED and is unrecorded, so treat this tip as "
              "needing exact review.", file=sys.stderr)
        return 1
    print("\n  manifest: %s" % path)
    for line in render_stored_gap(record):
        print(line)
    # THE VERB VERIFIES ITS OWN OUTPUT WHEN THE TIP IS IN SCOPE, so a compose
    # that produced an unprovable manifest says so at the moment it happened
    # rather than at somebody else's fold.
    proof = composition_proof(root, project, record["result_tip"])
    if proof is not None:
        print("\n  composition proof:")
        print(render(proof))
        if any(ok is not True for _n, ok, _d in proof):
            return 1
    return 0
