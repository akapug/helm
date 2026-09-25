#!/usr/bin/env python3
"""The stop guard's read of the resident's stop-facts snapshot.

A HOOK DOES NOT REBUILD THE WORLD. Folding the dispatch ledger, asking git
about every held lane and walking worktrees are the resident's jobs: the
supervised `helm web` process computes those facts with the guard's OWN
functions whenever an input moves (`helm/stopfacts_resident.py`) and writes
them behind itself to `_global/web-cache/stop-facts.json`. This module reads
that file. It imports nothing that folds, spawns or walks, and it never starts
the resident.

WHAT IT CHECKS, all O(1) and none of it a subprocess:

  ledger  one `os.stat` of the dispatch ledger against the header. When the
          ledger grew, only the appended bytes are read (at most TAIL_CAP),
          and only the leases and seats those bytes NAME go STALE.
  code    a digest of this package's source stats against the header's. A
          resident still running the code from before a land computed its
          facts with functions this process no longer ships.
  HEAD    each lane worktree's HEAD file and branch ref against the ones the
          resident recorded before it read the lane.
  trunk   the trunk ref (or the packed-refs stat) against the recorded one.
          Landedness is advice, so a moved trunk only ages the advice.
  seam    per repository: the worktree registry's stat, a digest of each
          seat's recorded cwd, the lane-lease projection; and for a stop with
          a room and a live peer, the trunk, the gate-receipt ledgers' stats
          and the HEAD of every room the rows are about (`seam_census_moved`,
          `seam_pairs_moved` — the resident recomputes by the same rule).

A WITNESS ONLY EVER DOWNGRADES. A ref this module reads by hand can say a
fact may no longer hold; it can never make a fact EXACT that the resident did
not compute. Positive proof, which is what a claims exemption needs, comes
only from EXACT facts, and STALE and ABSENT keep every block and say how old
the facts are and why. The claims law is unchanged: positive proof only,
UNKNOWN blocks.

A READING IS NEVER WAITED FOR. Fail-closed rungs never wait on state (owner
rule, task/3042): a STALE or ABSENT reading answers at once with the
conservative line and the snapshot's age. The commonest STALE reading is this
seat's own commit a moment before its stop, and that lane is unfinished
anyway, so a wait for the resident to catch up bought an exemption the lane
had not earned yet and cost every such stop up to a second and a half.
Nothing in this module sleeps.
"""
import collections
import hashlib
import json
import os
import stat
import time

from . import home

SCHEMA = 1
FILE = "stop-facts.json"
LOCK = "stop-facts.lock"

#: Past this age every fact is ABSENT: the `/api/lr` hard ttl
#: (`web_land_model`), the oldest reading any resident surface may serve.
HARD_AGE_S = 600
#: The most appended ledger bytes a hook will read to decide WHICH facts the
#: append touched. Past it every ledger-derived fact is STALE.
TAIL_CAP = 64 * 1024
#: A snapshot is a few hundred KB at most. A larger file is not one the
#: resident wrote, and a hook may not read a file this large.
MAX_BYTES = 1 << 20

EXACT, STALE, ABSENT = "EXACT", "STALE", "ABSENT"

#: The seat the resident computes the ledger-unnamed answer AS: no row names
#: it and no roster row can, so every rung's function answers it the way it
#: answers any seat the ledger does not name.
UNNAMED = "\x00unnamed-seat\x00"


class Freshness(collections.namedtuple("Freshness",
                                       "verdict reason age advice")):
    """One fact's standing. `verdict` decides what the fact may be used for;
    `reason` says why it is not EXACT; `age` is seconds since the resident
    computed it (None when there is nothing to age); `advice` names what ages
    ADVICE only (a moved trunk) while the verdict stays as it is."""

    __slots__ = ()

    @property
    def exact(self):
        return self.verdict == EXACT

    def note(self):
        """The short clause a surface prints beside a fact that is not EXACT
        (or whose advice a moved trunk has aged), or "" for an EXACT one with
        nothing to say. ABSENT says only that: the reason is the snapshot's,
        and the surface prints it once (`View.headline`), not per line."""
        if self.verdict == ABSENT:
            return "stop-facts ABSENT"
        why = [w for w in ((self.reason if self.verdict != EXACT else None),
                           self.advice) if w]
        if not why:
            return ""
        age = "as of %ds" % int(self.age) if self.age is not None else ""
        return "stop-facts %s: %s" % (age, "; ".join(why)) if age \
            else "stop-facts: %s" % "; ".join(why)


def path():
    return os.path.join(home.global_dir(), "web-cache", FILE)


def lock_path():
    return os.path.join(home.global_dir(), "web-cache", LOCK)


# ---------------------------------------------------------------------------
# witnesses: small file reads and stats, never a subprocess
# ---------------------------------------------------------------------------

_POLICY = []

# THE SLICE RUNNER'S DATA AUDIT (helm/gateslice.py) reports any module
# data a test unit leaves behind; these names are process-wide by design.
_GATESLICE_MUTABLE = {
    "_POLICY": (
        "the digest of this package's own source, computed once per process "
        "by design (code_policy)"),
}


def _source_marks(pkg):
    marks = []
    cut = len(pkg) + 1
    for base, dirs, files in os.walk(pkg):
        dirs[:] = sorted(d for d in dirs if d != "__pycache__")
        for name in sorted(files):
            if name.endswith(".py"):
                full = os.path.join(base, name)
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                marks.append("%s\x1f%d\x1f%d" % (full[cut:], st.st_size,
                                                  st.st_mtime_ns))
    return marks


def code_root():
    """The real path of this package on disk — the tree `code_policy` names."""
    return os.path.realpath(os.path.dirname(os.path.abspath(__file__)))


def code_policy(pkg=None, fresh=False):
    """A digest naming the code on disk under this package: its real path and
    every source file's size and mtime. Computed once per process unless
    `fresh`, because a hook runs the code it imported a moment ago."""
    if _POLICY and not fresh and pkg is None:
        return _POLICY[0]
    root = os.path.realpath(pkg) if pkg else code_root()
    h = hashlib.sha256(root.encode("utf-8", "surrogateescape") + b"\0")
    for mark in _source_marks(root):
        h.update(mark.encode("utf-8", "surrogateescape") + b"\0")
    digest = h.hexdigest()[:32]
    if pkg is None and not fresh:
        _POLICY.append(digest)
    return digest


def source_newest(pkg=None):
    """When the newest source file under this package was written (seconds),
    or None: the moment the tree last changed, which `helm doctor` holds
    against the moment a resident loaded its code."""
    root = os.path.realpath(pkg) if pkg else code_root()
    newest = None
    for mark in _source_marks(root):
        ns = int(mark.rsplit("\x1f", 1)[1])
        newest = ns if newest is None or ns > newest else newest
    return newest / 1e9 if newest is not None else None


def _small(p, cap=4096):
    with open(p, "rb") as f:
        return f.read(cap)


def git_dirs(room):
    """(gitdir, common dir) of a checkout, read from its `.git` entry, or
    (None, None). A linked worktree's `.git` is a file naming its gitdir, and
    that gitdir's `commondir` names the shared repository."""
    dotgit = os.path.join(room, ".git")
    try:
        st = os.lstat(dotgit)
    except OSError:
        return None, None
    if stat.S_ISDIR(st.st_mode):
        return dotgit, dotgit
    try:
        raw = _small(dotgit).decode("utf-8", "replace").strip()
    except OSError:
        return None, None
    if not raw.startswith("gitdir:"):
        return None, None
    gitdir = raw[len("gitdir:"):].strip()
    if not os.path.isabs(gitdir):
        gitdir = os.path.normpath(os.path.join(room, gitdir))
    try:
        rel = _small(os.path.join(gitdir, "commondir")).decode(
            "utf-8", "replace").strip()
        common = os.path.normpath(os.path.join(gitdir, rel)) if rel \
            else gitdir
    except OSError:
        common = gitdir
    return gitdir, common


def _packed_mark(common):
    try:
        st = os.stat(os.path.join(common, "packed-refs"))
    except FileNotFoundError:
        return "packed:none"
    except OSError:
        return None
    return "packed:%d:%d:%d" % (st.st_ino, st.st_size, st.st_mtime_ns)


def ref_witness(common, ref):
    """What a ref reads as, by file: its loose ref's contents, else the
    packed-refs stat it would be answered from. None when neither can be read.
    Only equality between two readings means anything."""
    if not common or not ref:
        return None
    try:
        return "loose:" + _small(os.path.join(common, ref)).decode(
            "utf-8", "replace").strip()
    except FileNotFoundError:
        return _packed_mark(common)
    except OSError:
        return None


def head_witness(room):
    """(sha or None, witness or None) for a checkout's HEAD, by file reads.

    The witness is the HEAD file plus what its branch ref reads as, so a
    commit, a checkout of another branch and a reset all move it. The sha is
    known when HEAD is detached or its branch is a loose ref."""
    if not room:
        return None, None
    gitdir, common = git_dirs(room)
    if not gitdir:
        return None, None
    try:
        head = _small(os.path.join(gitdir, "HEAD")).decode(
            "utf-8", "replace").strip()
    except OSError:
        return None, None
    if head.startswith("ref:"):
        ref = head[len("ref:"):].strip()
        mark = ref_witness(common, ref)
        if mark is None:
            return None, None
        sha = mark[len("loose:"):] if mark.startswith("loose:") else None
        return sha, "%s\x1f%s" % (head, mark)
    return head or None, head or None


def names(tail, keys):
    """The first of `keys` the lowered ledger bytes `tail` contain, or None.

    ONE RULE FOR BOTH SIDES. The reader marks a fact STALE when the ledger
    bytes appended since the snapshot name one of its keys, and the resident
    recomputes exactly the facts whose keys the same bytes name — so a fact the
    resident kept is one the reader would have kept. Case-folded substring,
    which over-matches; over-matching only ever makes a fact STALE. Keys
    shorter than three characters would match everything and are skipped."""
    if not tail:
        return None
    for key in keys or ():
        k = str(key or "").strip().lower()
        if len(k) >= 3 and k.encode("utf-8", "replace") in tail:
            return k
    return None


def ledger_mark(p):
    """(ino, size, mtime_ns) of the ledger, or None when it cannot be stat'd."""
    try:
        st = os.stat(p)
    except OSError:
        return None
    return st.st_ino, st.st_size, st.st_mtime_ns


def registry_mark(common):
    """What a repository's worktree registry reads as, by one stat: its
    common dir's `worktrees/` directory, whose entries git adds and removes
    with every room. "none" for a repository with no linked room yet, None
    when it cannot be stat'd. Only equality between two readings means
    anything."""
    if not common:
        return None
    try:
        st = os.stat(os.path.join(common, "worktrees"))
    except FileNotFoundError:
        return "none"
    except OSError:
        return None
    return "%d:%d" % (st.st_ino, st.st_mtime_ns)


def roster_mark(p):
    """A digest of the one thing the seam census reads from the roster: each
    seat's recorded cwd. The roster is rewritten whenever a seat is seen, so
    its stat moves every few seconds while the cwds it records do not.
    "none" for no roster, None for one that cannot be read or is not a
    table — the census's own failure (a degraded reading) then reads the
    same on both sides."""
    if not p:
        return None
    try:
        with open(p, "rb") as f:
            raw = f.read(MAX_BYTES + 1)
    except FileNotFoundError:
        return "none"
    except OSError:
        return None
    if len(raw) > MAX_BYTES:
        return None
    try:
        rows = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(rows, dict):
        return None
    pairs = sorted((str(name), row["cwd"]) for name, row in rows.items()
                   if isinstance(row, dict) and isinstance(row.get("cwd"), str)
                   and row["cwd"])
    return hashlib.sha256(json.dumps(pairs).encode(
        "utf-8", "surrogateescape")).hexdigest()[:32]


def _mark_list(mark):
    """A stat mark as JSON carries it: a tuple reads back as a list."""
    return list(mark) if isinstance(mark, (tuple, list)) else mark


def seam_census_moved(facts, live=None):
    """Why one repository's seam census may no longer hold, or None.

    ONE RULE FOR BOTH SIDES, as `names` is for the ledger: the stop judges
    the census with it (`View.seam`) and the resident recomputes exactly the
    repositories it names, so a census the resident keeps is one the stop
    would have kept. The census reads the worktree registry, the lane leases
    and each seat's recorded cwd; those are its witnesses. (`live` is the
    claims reading `work._gc._live()` returns, taken once by a caller that
    asks about many repositories.)"""
    if registry_mark(facts.get("common")) != facts.get("registry_mark"):
        return "a worktree was added or removed since"
    roster = facts.get("roster") if isinstance(facts.get("roster"),
                                               dict) else {}
    if roster_mark(roster.get("path")) != roster.get("mark"):
        return "a seat's recorded cwd changed since"
    from .work import _gc
    if _gc.seam_lease_marks(facts.get("root"), facts.get("registry") or (),
                            _gc._live() if live is None else live) \
            != facts.get("leases"):
        return "a lane lease changed since"
    return None


def seam_pairs_moved(facts, rooms, head=None):
    """Why the seam ROWS about `rooms` may no longer hold, or None: a row is
    computed from each room's HEAD, the trunk, and the gate-receipt ledgers
    that decide which halves are green and which compositions were run.
    `head(room)` is the caller's HEAD witness reader (default: a fresh
    read). The resident asks it of every room it recorded; the stop, of its
    own rooms and its live peers."""
    head = head or (lambda room: head_witness(room)[1])
    trunk = facts.get("trunk")
    if isinstance(trunk, dict) and ref_witness(
            trunk.get("common"), trunk.get("ref")) != trunk.get("witness"):
        return "trunk moved since"
    receipts = facts.get("receipts")
    for p, mark in sorted(receipts.items() if isinstance(receipts, dict)
                          else ()):
        if _mark_list(ledger_mark(p)) != _mark_list(mark):
            return "a gate receipt was recorded since"
    heads = facts.get("heads") if isinstance(facts.get("heads"), dict) else {}
    for room in sorted(rooms):
        recorded = heads.get(room)
        if not recorded:
            return ("the resident could not witness %s's HEAD"
                    % os.path.basename(room))
        if head(room) != recorded:
            return "HEAD moved since in %s" % os.path.basename(room)
    return None


def lease_room(resource, row):
    """The lane room a `worktree:<project>:<lane>` claim names, derived from
    the repository the claim itself recorded — no cwd, no git.

    `helm work claim` records `repo`, the realpath of the repository's common
    dir. The room is `work._lanes.lane_path` of that repository's root when the
    root's project token is the one the resource names; anything else is None
    (UNKNOWN), never a guess."""
    parts = str(resource).split(":", 2)
    if len(parts) != 3 or parts[0] != "worktree" or not parts[1] \
            or not parts[2] or not isinstance(row, dict):
        return None
    repo = row.get("repo")
    if not isinstance(repo, str) or not repo.startswith(os.sep):
        return None
    repo = repo.rstrip(os.sep)
    if os.path.basename(repo) != ".git":
        return None
    from .work import _lanes
    root = os.path.dirname(repo)
    if _lanes.project_token(root) != parts[1]:
        return None
    return _lanes.lane_path(root, parts[2])


def _alive(resident):
    """Is the process that wrote this snapshot still running — the same pid
    AND the same start time, so a recycled pid does not read as alive."""
    if not isinstance(resident, dict):
        return False
    pid, start = resident.get("pid"), resident.get("starttime")
    if type(pid) is not int or type(start) is not int:
        return False
    try:
        with open("/proc/%d/stat" % pid, "rb") as f:
            raw = f.read()
        return int(raw[raw.rindex(b")") + 2:].split()[19]) == start
    except (OSError, ValueError, IndexError):
        return False


def own_starttime():
    """This process's start time in clock ticks, the key `_alive` compares."""
    try:
        with open("/proc/self/stat", "rb") as f:
            raw = f.read()
        return int(raw[raw.rindex(b")") + 2:].split()[19])
    except (OSError, ValueError, IndexError):
        return None


# ---------------------------------------------------------------------------
# the snapshot
# ---------------------------------------------------------------------------

def load(p=None):
    """(snapshot dict, None) or (None, why). Never raises."""
    p = p or path()
    try:
        from . import pk
        with pk.open_regular(p, "rb") as f:
            size = os.fstat(f.fileno()).st_size
            if size > MAX_BYTES:
                return None, ("the stop-facts file is %d bytes, over the %d a "
                              "hook may read" % (size, MAX_BYTES))
            raw = f.read(MAX_BYTES + 1)
    except FileNotFoundError:
        return None, "no stop-facts have been written (is `helm web` running?)"
    except Exception as exc:                 # noqa: BLE001 — named, never raised
        return None, "the stop-facts file could not be read (%s)" \
            % type(exc).__name__
    try:
        snap = json.loads(raw.decode("utf-8"))
    except Exception:                        # noqa: BLE001
        return None, "the stop-facts file does not parse"
    if not isinstance(snap, dict) or snap.get("schema") != SCHEMA:
        return None, "the stop-facts file has an unknown schema"
    return snap, None


def _row(value):
    """A snapshot row as this module may index it: the dict, else None. The
    one file read here is another process's, so a shape the reader did not
    expect is JUDGED (ABSENT, STALE) and never raised on: a raise leaves the
    claims rung, and the guard then publishes the stop ALLOWED and UNCHECKED
    (`seats_stop_response.publish_failure`)."""
    return value if isinstance(value, dict) else None


def _keys(facts):
    """The ledger keys a fact is named by, as strings, or none."""
    ids = facts.get("ids") if isinstance(facts, dict) else None
    return tuple(str(x) for x in ids) if isinstance(ids, (list, tuple)) \
        else ()


def _seam_malformed(facts):
    """The first field of one repository's seam facts that is not the shape
    the resident writes, or None. The seam rung swallows what it raises on,
    so a field it cannot index would cost its UNKNOWN line and say nothing;
    judged here, it is an ABSENT reading that names the field."""
    census = facts.get("census")
    if census is not None and not (isinstance(census, list) and all(
            isinstance(r, dict) and isinstance(r.get("path"), str)
            for r in census)):
        return "census"
    for key, kind in (("registry", list), ("rooms", dict), ("raised", dict),
                      ("heads", dict), ("cdeg", list)):
        if facts.get(key) is not None and not isinstance(facts[key], kind):
            return key
    return None


class View(object):
    """One stop's reading of the snapshot, with every witness taken once.

    Built by `read`; asked per lease, per seat and for the fleet-wide rows.
    Answers never raise and never spawn."""

    def __init__(self, snap, why, now=None, policy=None):
        self.snap = snap if isinstance(snap, dict) else None
        self.why = why
        self.now = time.time() if now is None else now
        self._policy = policy
        self._ledger = None
        self._trunks = {}
        self._heads = {}
        self.age = None
        self.absent = None
        if self.snap is None:
            self.absent = why or "no stop-facts"
            return
        written = self.snap.get("written_at")
        if not isinstance(written, (int, float)):
            self.absent = "the stop-facts header carries no write time"
            return
        self.age = max(0.0, self.now - written)
        resident = _row(self.snap.get("resident")) or {}
        if self.age > HARD_AGE_S:
            self.absent = ("stop-facts are %ds old, past the %ds bound — %s"
                           % (self.age, HARD_AGE_S, self._resident_state()))
        elif resident.get("replaying_since") is not None:
            self.absent = "the resident is refolding (%s)" \
                % self._resident_state()

    # -- the header ---------------------------------------------------------

    def _resident_state(self):
        r = _row((self.snap or {}).get("resident")) or {}
        pid = r.get("pid")
        if not _alive(r):
            return "resident pid %s is not running; `helm web` owns the " \
                   "refresh" % pid
        since = r.get("replaying_since")
        if isinstance(since, (int, float)):
            return "resident pid %s refolding for %ds" % (
                pid, max(0, self.now - since))
        return "resident pid %s is running" % pid

    def resident_alive(self):
        return self.snap is not None and _alive(self.snap.get("resident"))

    def code_matches(self):
        policy = self._policy or code_policy()
        return bool(self.snap) and self.snap.get("policy") == policy

    def ledger(self):
        """("exact"|"tail"|"moved", why, lowered tail bytes or None)."""
        if self._ledger is not None:
            return self._ledger
        head = _row((self.snap or {}).get("ledger")) or {}
        p = head.get("path")
        mark = ledger_mark(p) if isinstance(p, str) else None
        offset = head.get("offset")
        if mark is None and head.get("ino") is None and not \
                head.get("unavailable") and isinstance(p, str) \
                and not os.path.lexists(p):
            # NO LEDGER THEN, NO LEDGER NOW: a home that has never dispatched.
            # Nothing was appended, so nothing the facts said has moved.
            self._ledger = ("exact", None, None)
        elif mark is None or type(offset) is not int:
            self._ledger = ("moved", "the dispatch ledger cannot be witnessed",
                            None)
        elif head.get("unavailable"):
            self._ledger = ("moved", "the resident could not read the "
                            "dispatch ledger (%s)" % head.get("unavailable"),
                            None)
        elif mark[0] != head.get("ino") or mark[1] < offset:
            self._ledger = ("moved", "the dispatch ledger was replaced", None)
        elif mark[1] == offset:
            self._ledger = ("exact", None, None)
        elif mark[1] - offset > TAIL_CAP:
            self._ledger = ("moved", "the dispatch ledger grew %d bytes, past "
                            "the %d a stop reads" % (mark[1] - offset,
                                                     TAIL_CAP), None)
        else:
            try:
                with open(p, "rb") as f:
                    f.seek(offset)
                    tail = f.read(TAIL_CAP)
            except OSError:
                self._ledger = ("moved", "the dispatch ledger tail could not "
                                "be read", None)
            else:
                events = tail.count(b"\n")
                self._ledger = ("tail", "ledger +%d event(s) since" % events,
                                tail.lower())
        return self._ledger

    def _named(self, keys):
        """Why the appended ledger tail may have moved a fact keyed on `keys`,
        or None when it names none of them."""
        state, why, tail = self.ledger()
        if state == "exact":
            return None
        if state == "moved":
            return why
        hit = names(tail, keys)
        return "%s naming %s" % (why, hit[:40]) if hit else None

    def _trunk(self, trunk):
        if not isinstance(trunk, dict):
            return None
        key = (trunk.get("common"), trunk.get("ref"))
        if key not in self._trunks:
            self._trunks[key] = ref_witness(*key)
        now = self._trunks[key]
        if now is None or now != trunk.get("witness"):
            return "trunk moved since"
        return None

    def _head(self, room):
        if room not in self._heads:
            self._heads[room] = head_witness(room)[1]
        return self._heads[room]

    # -- the facts ----------------------------------------------------------

    def _base(self, facts):
        """The part of the verdict every fact shares: snapshot, age, code."""
        if self.absent:
            return Freshness(ABSENT, self.absent, self.age, None)
        if facts is None:
            return None
        age = self.age
        at = facts.get("computed_at") if isinstance(facts, dict) else None
        if isinstance(at, (int, float)):
            age = max(0.0, self.now - at)
        if not self.code_matches():
            return Freshness(STALE, "computed by other code (the resident "
                             "has not restarted on this tree)", age, None)
        return Freshness(EXACT, None, age, None)

    def lease(self, resource):
        """(facts dict or None, Freshness) for one held lease."""
        leases = _row((self.snap or {}).get("leases")) or {}
        facts = _row(leases.get(resource))
        base = self._base(facts)
        if base is None:
            return None, Freshness(ABSENT, "no stop-facts row for this lease "
                                   "(taken after the resident's last "
                                   "refresh)", self.age, None)
        if base.verdict != EXACT:
            return facts, base
        named = self._named((facts.get("stem"),) + _keys(facts))
        if named:
            return facts, Freshness(STALE, named, base.age, None)
        room = facts.get("room")
        if room:
            recorded = facts.get("head_witness")
            if not recorded and facts.get("room_exists") is False:
                # NOTHING TO WITNESS: the room was not on disk, and the facts
                # say so. That still holds while the room is still absent; a
                # room that has appeared since is a lane the facts never read.
                if os.path.isdir(room):
                    return facts, Freshness(STALE, "the lane room appeared "
                                            "since", base.age, None)
                return facts, Freshness(EXACT, None, base.age, None)
            if not recorded:
                return facts, Freshness(STALE, "the resident could not "
                                        "witness this lane's HEAD", base.age,
                                        None)
            if self._head(room) != recorded:
                return facts, Freshness(STALE, "HEAD moved since", base.age,
                                        None)
        return facts, Freshness(EXACT, None, base.age,
                                self._trunk(facts.get("trunk")))

    def seat(self, name):
        """(facts dict or None, Freshness) for one seat's dispatch facts."""
        seats = _row((self.snap or {}).get("seats")) or {}
        facts = None
        if name:
            # A SEAT IS KEYED AS THE ROSTER SPELLS IT, OR CASE-FOLDED AS THE
            # LEDGER RECORDS ITS SENDER — `review_spiral` folds case itself.
            facts = _row(seats.get(name))
            if facts is None:
                facts = _row(seats.get(str(name).casefold()))
        if facts is None and name:
            facts = self._unnamed(name)
        base = self._base(facts)
        if base is None:
            return None, Freshness(ABSENT, "no stop-facts row for seat %s"
                                   % str(name)[:40], self.age, None)
        if base.verdict != EXACT:
            return facts, base
        named = self._named((name,) + _keys(facts))
        if named:
            return facts, Freshness(STALE, named, base.age, None)
        return facts, base

    def _unnamed(self, name):
        """The resident's answer for a seat the ledger names nowhere, as this
        seat: its rounds error names this seat, never the stand-in."""
        fleet = (self.snap or {}).get("fleet")
        got = fleet.get("unnamed") if isinstance(fleet, dict) else None
        if not isinstance(got, dict):
            return None
        facts = dict(got)
        spiral = facts.get("spiral")
        if isinstance(spiral, list) and len(spiral) == 2 \
                and isinstance(spiral[1], str):
            facts["spiral"] = [spiral[0], spiral[1].replace(
                repr(UNNAMED), repr(str(name)))]
        facts["unnamed"] = True
        return facts

    def fleet(self):
        """(facts dict or None, Freshness) for the fleet-wide rows. Any ledger
        append makes them STALE: every row in them can be moved by it."""
        facts = (self.snap or {}).get("fleet")
        base = self._base(facts if isinstance(facts, dict) else None)
        if base is None:
            return None, Freshness(ABSENT, "no fleet-wide stop-facts",
                                   self.age, None)
        if base.verdict != EXACT:
            return facts, base
        state, why, _tail = self.ledger()
        if state != "exact":
            return facts, Freshness(STALE, why, base.age, None)
        return facts, base

    def row(self, rid):
        rows = (self.snap or {}).get("rows") or {}
        got = rows.get(rid) if isinstance(rows, dict) and rid else None
        return dict(got) if isinstance(got, dict) else None

    # -- the composition seam ----------------------------------------------

    def seam(self, root):
        """(facts dict or None, Freshness) for one repository's room census.

        THE CENSUS HALF OF THE SEAM FACTS: which rooms exist, which are live,
        who holds and who answers to each. Its witnesses are the ones that can
        move those answers — the worktree registry, the lane leases, and each
        seat's recorded cwd — re-read here by stat and small file reads. What
        occupancy (/proc) says is NOT witnessed: the resident re-reads it on
        its whole refresh, and the age says how old that reading is.
        Whether the ROWS still hold is `seam_pairs`, asked only when the
        stopping seat has rooms and peers for them to be about."""
        seam = (self.snap or {}).get("seam")
        roots = seam.get("roots") if isinstance(seam, dict) else None
        facts = roots.get(root) if isinstance(roots, dict) and root else None
        base = self._base(facts if isinstance(facts, dict) else None)
        if base is None:
            return None, Freshness(ABSENT, "the resident holds no seam facts "
                                   "for this repository", self.age, None)
        if base.verdict != EXACT:
            return facts, base
        if facts.get("unavailable"):
            return facts, Freshness(ABSENT, str(facts["unavailable"]),
                                    base.age, None)
        bad = _seam_malformed(facts)
        if bad:
            return facts, Freshness(ABSENT, "the resident's seam facts for "
                                    "this repository carry a %s of the wrong "
                                    "shape" % bad, base.age, None)
        moved = seam_census_moved(facts)
        if moved:
            return facts, Freshness(STALE, moved, base.age, None)
        return facts, base

    def seam_pairs(self, facts, rooms):
        """Freshness of the inputs the seam ROWS about `rooms` (a stop's own
        rooms and its live peers) are computed from: `seam_pairs_moved`."""
        age = None
        at = facts.get("computed_at") if isinstance(facts, dict) else None
        if isinstance(at, (int, float)):
            age = max(0.0, self.now - at)
        moved = seam_pairs_moved(facts, rooms, head=self._head)
        if moved:
            return Freshness(STALE, moved, age, None)
        return Freshness(EXACT, None, age, None)

    # -- the doors the Stop rungs take -------------------------------------

    def owed_pair(self, seat=None):
        """(owed frontier by id, None), or ({}, why): a `(state, unavailable)`
        pair every dispatch rung already takes as its `dispatch_snapshot`.

        THE FRONTIER IS ENOUGH FOR THEM, EXACTLY. `_beacon_obligation`,
        `stop_candidate` and `open_rows` read the ledger only through
        `dispatches.owed`, and `owed` of the owed rows is those rows again: a
        row is owed when it is open and no live successor carries it, and a
        live successor that carried one would have made it not owed. So these
        rungs, handed the resident's frontier, answer what they answered over
        the whole fold — without a fold.

        STALE IS UNAVAILABLE, per seat. An append that names this seat or any
        owed row may have moved what the seat owes, so the pair is refused;
        an append naming neither can only have ADDED rows this reading lacks,
        which keeps every answer here conservative (a new obligation is not
        yet seen; nothing discharged is still offered)."""
        facts = (self.snap or {}).get("fleet")
        base = self._base(facts if isinstance(facts, dict) else None)
        if base is None:
            return {}, "stop-facts carry no owed frontier"
        if base.verdict != EXACT:
            return {}, "stop-facts %s" % (base.reason or base.verdict)
        # THE LEDGER'S OWN FAULT FIRST. A fold that raised leaves no frontier,
        # and its reason is the ledger's, not the facts': the whisper reads a
        # `stop-facts` prefix as "the ledger is fine, the resident is behind"
        # and falls silent (`_whisper_candidates`), the wrong silence for an
        # unreadable ledger.
        if facts.get("unavailable"):
            return {}, str(facts["unavailable"])
        owed = facts.get("owed")
        if not isinstance(owed, dict):
            return {}, "stop-facts carry no owed frontier"
        named = self._named((seat,) + tuple(owed))
        if named:
            return {}, "stop-facts %s" % named
        return {str(k): dict(v) for k, v in owed.items()
                if isinstance(v, dict)}, None

    def spiral(self, seat):
        """`review_spiral`'s (info, err) from EXACT facts; otherwise no info
        and an err naming why, which the rung renders as rounds UNKNOWN."""
        facts, fresh = self.seat(seat)
        if not fresh.exact or not isinstance(facts, dict):
            return None, (fresh.note() or "stop-facts %s" % fresh.reason)
        got = facts.get("spiral")
        if not isinstance(got, list) or len(got) != 2:
            return None, "stop-facts carry no review rounds for this seat"
        info, err = got
        return (info if isinstance(info, dict) else None), err

    def headline(self):
        """One line naming the snapshot's standing, for a surface that has to
        say why facts were not used."""
        if self.absent:
            return "stop-facts ABSENT: %s" % self.absent
        state, why, _tail = self.ledger()
        parts = ["ledger exact" if state == "exact" else why]
        if not self.code_matches():
            parts.append("computed by other code")
        return "stop-facts as of %ds (%s; %s)" % (
            int(self.age or 0), ", ".join(parts), self._resident_state())


def read(p=None, now=None):
    """One stop's View of the snapshot at `p` (default `path()`)."""
    snap, why = load(p)
    return View(snap, why, now=now)


class Lazy(object):
    """One stop's stop facts: read on the first ask and shared by every rung
    of that stop, so they all judge the same reading. There is no second
    reading and no wait for a fresher one (see the module docstring)."""

    def __init__(self, p=None):
        self.p = p
        self._view = None

    def view(self):
        if self._view is None:
            self._view = read(self.p)
        return self._view
