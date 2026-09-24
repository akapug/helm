#!/usr/bin/env python3
"""THE CARRIAGE REPLAY'S ANSWER, RE-DERIVED THROUGH THE FOLD CHECKPOINT'S DOOR.

THE DEFECT, MEASURED (task/2813). One projection's off-frontier census
(`landreq.frontier_verdicts` -> `off_frontier_closable` -> the `carried`
ladder -> `dispatches.carriage_proof` -> `rowworld._carriage`) spawned 141
`merge-tree --write-tree` calls for 138 DISTINCT questions and paid 19.7s of a
one-board read for them, and the projection before it had derived every one of
those answers against the same objects. The bill is irreducible WITHIN one
projection and that was measured four ways rather than assumed: there is no row
to drop the question for (every placed row routes to a door that HAS a
preflight and every refusal is the carriage refusal itself, so the "cannot be
closed anyway" set is EMPTY at this door); the two witness families partition by
the classification that routed the row, so dropping the replay for the ancestry
class takes the offer away from the one row the census offers through it; the
cheap content twin does not predict the expensive one (`_postimages_at_head`
answered False for all 141 payers INCLUDING the affirming one); and a per-walk
memo collapses nothing, because the census's question set is disjoint from the
World build's and `merge-tree` is not a memoised read verb. What is left to
remove is the RE-DERIVATION ACROSS PROJECTIONS, and that is what this is.

IT IS NOT A CACHE AND NOT THE PERSISTED ANSWER THE NOTE ABOVE
`dispatches._carriage_trunk_sha` FORBIDS, and the two conditions that note
names are exactly what this door supplies. It refuses a memo of the replay
because git resolves the answer through MERGE MACHINERY — a merge driver, an
attributes file behind a pathname, repeated config records settled by
last-value precedence — which moves the answer while every object id stands
still, and because nothing bounded the interval between an answer and its
reuse. What it asks for instead is a SEMANTIC GENERATION and A VIEW HELD
IMMUTABLE:

  * the generation is `foldckpt.policy()`, the identity of the code THIS
    PROCESS IS EXECUTING — so a deploy, a lane's own `./bin/helm`, and any edit
    under the package invalidate every entry at once, with one file per
    generation so two trees do not discard each other's;
  * the view is re-verified on EVERY READ, per repository, by ONE
    `git cat-file --batch-check` over every object and ref expression the
    witness read, plus `foldckpt.fingerprint`: the git binary, shallowness, the
    config git lists, the replacement refs, and the attributes and grafts
    files. Anything that differs discards that repository's entries and the
    witness runs live.

The interval that note could not bind is now the one between the
re-verification and the answer, which is the interval every live derivation
already has between two of its own git calls.

TRUNK IS IN THE KEY, NOT IN THE VERIFICATION, and that asymmetry is deliberate.
`carriage_proof` resolves the trunk ref to an OBJECT once and hands that object
down, so the witness's whole input is three immutable ids — base, tip and the
trunk commit — and a land moves trunk to a different id, which is a DIFFERENT
KEY rather than a stale entry. One full replay per land is expected; between
lands a projection pays none. An unresolvable trunk ref is refused outright:
a ref NAME as the key's trunk term would survive the move it exists to notice.

THE ANSWER IS ONLY SERVED INSIDE A DECLARED REGION (`derived`), which is what
keeps this away from the close WRITER. A projection declares that it is reading
a census and may be served; the ladder that authorizes a real `close`, and the
replay that re-derives one under the lock, open no region and are byte-identical
to a tree without this module. The store is the read side's, and a read side is
where a re-derivation that costs 19.7s per projection was measured.

AND IT NEVER SERVES INTO A FOLD BEING RECORDED (`foldckpt.recording_active`).
The dispatch fold keys its own checkpoint on every git answer it read; an answer
served from here does no git reads, so that fold would key on a question set
missing the inputs that decided one of its closes. Inside a recording this
module derives live and lets the recorder see the reads.

FAIL-SAFE IN ONE DIRECTION, the same direction `foldckpt` takes. Any read,
parse, plan, verification or write failure means the witness runs; there is no
partial and no stale answer. A derivation whose reads this module cannot
re-verify (`foldckpt.Unplannable` — a read outside the `vcs` seam, a question
the plan does not admit, a repository the key does not name) is simply not
stored. A failure to WRITE is a breadcrumb through `record.swallow`.

WHAT IT DOES NOT DO. It never repairs one entry: doubt about any input a
repository's entries were derived under discards that repository's whole
section, because the batch-check is one question per repository and a
per-entry one would be the 141 spawns again. It never keys on wall time.
"""
import contextlib
import json
import os
import re
import threading

from . import foldckpt, home, pk, projscope, record

FORMAT = "helm-carriage-replay-derivation"
VERSION = 1
DIRNAME = "carriage-replay"
SUFFIX = ".json"
# One file per CODE GENERATION, the newest KEEP kept — `foldckpt`'s own bound,
# for its reason: a lane's `./bin/helm` and the hub binary each maintain their
# own instead of invalidating the other's on every read.
KEEP = 8
#: How many derivations one file may hold. The live census asks about 228
#: placed rows in one repository; the bound is the estate's growth, not a page.
ENTRY_CAP = 8192

#: Nothing is stored for this question — distinct from a stored None, which is
#: the witness's own "no witness affirmed" and is the expensive answer to lose.
MISS = object()

_OID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
# PER-THREAD, like the fold's recorder and the projection memo: helm web reads
# on several threads at once and one walk's region must never serve another's.
_LOCAL = threading.local()


def store_dir():
    """Where the derivations for every generation live."""
    return os.path.join(home.global_dir(), ".state", DIRNAME)


def _key(gitdir, trunk, base, tip):
    """The question, or None when it is not one this store may hold.

    EVERY TERM MUST BE AN OBJECT ID. `rowworld._carriage` is handed the trunk
    ref's resolved commit by its caller, and that resolution is the whole
    reason a trunk move cannot be missed here; a term that is not a full id is
    a name, and a name is the mutable thing this key exists to exclude."""
    if not isinstance(gitdir, str) or not gitdir:
        return None
    terms = [str(trunk or "").lower(), str(base or "").lower(),
             str(tip or "").lower()]
    if not all(_OID.fullmatch(term) for term in terms):
        return None
    return (gitdir, terms[0], terms[1], terms[2])


class _Region(object):
    """One walk's served answers, its new derivations, and what they read."""

    __slots__ = ("code", "loaded", "entries", "repos", "verified", "pending",
                 "plans", "served", "derived", "refused")

    def __init__(self, code):
        self.code = code
        self.loaded = False
        self.entries = {}      # key -> (answer, ts) as the store holds it
        self.repos = {}        # gitdir -> {"env", "fingerprint", "exprs"}
        self.verified = {}     # gitdir -> does its section still describe git
        self.pending = {}      # key -> (answer, ts) derived on this walk
        self.plans = {}        # gitdir -> what this walk's derivations read
        self.served = 0
        self.derived = 0
        self.refused = 0

    # ------------------------------------------------------------------ read

    def path(self):
        return os.path.join(store_dir(), self.code[:16] + SUFFIX)

    def _load(self):
        """Read this generation's file once. A file that does not describe
        itself is not read at all — an unreadable store and an empty one both
        mean every question is derived live, which is the safe direction."""
        if self.loaded:
            return
        self.loaded = True
        projscope.spend_or_raise("carriage replay derivation key")
        try:
            with pk.open_regular(self.path(), "rb") as fh:
                blob = fh.read()
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            record.swallow("carriageckpt.load-read", exc)
            return
        try:
            body = json.loads(blob.decode("utf-8"))
            if not isinstance(body, dict) or body.get("format") != FORMAT \
                    or body.get("v") != VERSION \
                    or body.get("policy") != self.code:
                return
            repos = body.get("repos")
            if not isinstance(repos, dict):
                return
            for gitdir, section in repos.items():
                if _section_ok(section):
                    self.repos[gitdir] = section
            for row in body.get("entries") or ():
                if not isinstance(row, list) or len(row) != 6:
                    continue
                key = _key(row[0], row[1], row[2], row[3])
                if key is None or key[0] not in self.repos \
                        or row[4] not in (True, False, None):
                    continue
                self.entries[key] = (row[4], row[5])
        except Exception as exc:                             # noqa: BLE001
            record.swallow("carriageckpt.load", exc)
            self.entries, self.repos = {}, {}

    def _holds(self, gitdir):
        """Does this repository's recorded section still describe git? ONE
        batch-check and ONE fingerprint per repository per walk."""
        if gitdir in self.verified:
            return self.verified[gitdir]
        section = self.repos.get(gitdir)
        answer = False
        try:
            if section is not None:
                env = dict((k, v) for k, v in section["env"])
                fp, _facts = foldckpt.fingerprint(gitdir, env)
                answer = fp is not None and fp == section["fingerprint"] \
                    and foldckpt.batch_check(
                        gitdir, env, section["exprs"]) == section["exprs"]
        except projscope.Expired:
            raise
        except Exception as exc:                             # noqa: BLE001
            record.swallow("carriageckpt.verify", exc)
            answer = False
        self.verified[gitdir] = answer
        return answer

    def stored(self, key):
        """The answer this store holds for `key`, or MISS."""
        self._load()
        if key not in self.entries or not self._holds(key[0]):
            return MISS
        self.served += 1
        return self.entries[key][0]

    # ----------------------------------------------------------------- write

    def remember(self, key, answer, rec):
        """Hold `answer` for `key` together with everything the witness read.

        A derivation this module cannot re-verify is DROPPED HERE rather than
        written and discarded later, so the file never holds an entry whose
        key is weaker than the answer it serves."""
        self.derived += 1
        gitdir = key[0]
        try:
            fresh = foldckpt.plan(rec)
        except foldckpt.Unplannable as exc:
            record.swallow("carriageckpt.unplannable", exc)
            self.refused += 1
            return
        if set(fresh) != {gitdir}:
            # The witness read no repository, or one this key does not name.
            # Either way the key does not describe what the answer depended on.
            self.refused += 1
            return
        entry = fresh[gitdir]
        plan = self.plans.setdefault(gitdir, {"env": entry["env"], "obs": [],
                                              "shallow": set(),
                                              "machinery": False})
        if plan["env"] != entry["env"]:
            self.refused += 1
            return
        plan["obs"].extend(entry["exprs"].items())
        plan["shallow"].add(entry["shallow"])
        plan["machinery"] = plan["machinery"] or entry["machinery"]
        self.pending[key] = (answer, pk.now_ts())

    def flush(self):
        """Write what this walk derived, or write nothing. Never raises."""
        try:
            return self._write()
        except Exception as exc:                             # noqa: BLE001
            # EXPIRED INCLUDED, for `foldckpt.save`'s reason: the answers this
            # file would describe are already computed and already served to
            # the caller, so a budget that ran out while writing is a reason
            # not to write and never a reason to lose them.
            record.swallow("carriageckpt.flush", exc)
            return False

    def _write(self):
        if not self.pending or not foldckpt.may_save():
            return False
        self._load()
        if foldckpt.policy() != self.code:
            return False
        projscope.spend_or_raise("carriage replay derivation save")
        repos, entries = dict(self.repos), dict(self.entries)
        wrote = False
        for gitdir, plan in self.plans.items():
            section, keep = self._section(gitdir, plan, repos.get(gitdir))
            if section is None:
                continue
            if not keep:
                # The repository moved since the entries already in the file
                # were derived. They are discarded WHOLE: the batch-check is
                # one question per repository, so there is no per-entry answer
                # to keep, and a section that describes git today must not
                # vouch for an answer taken under yesterday's view.
                entries = {k: v for k, v in entries.items() if k[0] != gitdir}
            repos[gitdir] = section
            for key, value in self.pending.items():
                if key[0] == gitdir:
                    entries[key] = value
                    wrote = True
        if not wrote:
            return False
        entries = {k: v for k, v in entries.items() if k[0] in repos}
        rows = sorted(([k[0], k[1], k[2], k[3], v[0], v[1]]
                       for k, v in entries.items()),
                      key=lambda row: str(row[5]), reverse=True)
        # THE NEWEST SURVIVE and the rest are re-derived by the next read of
        # the question they answered. Nothing in the file records the cut
        # because nothing can act on it: every row here is derived, a dropped
        # one costs exactly one `merge-tree`, and a notice would name a loss
        # with no reader.
        rows = rows[:ENTRY_CAP]  # noqa: SILENT_CAP — a dropped derivation is re-derived on the next read; see above
        keys = {(row[0], row[1], row[2], row[3]) for row in rows}
        repos = {g: s for g, s in repos.items()
                 if any(key[0] == g for key in keys)}
        blob = json.dumps({"format": FORMAT, "v": VERSION,
                           "policy": self.code,
                           "repos": {g: s for g, s in repos.items()},
                           "entries": rows},
                          sort_keys=True, separators=(",", ":"))
        path = self.path()
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        pk.atomic_write(path, blob.encode("utf-8"), mode=0o600)
        _prune(path)
        return True

    def _section(self, gitdir, plan, old):
        """(the section to write, whether the old entries survive), or
        (None, _) when this repository must not be written at all.

        MEASURED NOW AND CROSS-CHECKED AGAINST WHAT THE WITNESS SAW, which is
        the half that makes the key honest: a batch-check line that disagrees
        with the observation the witness took means git moved DURING the walk,
        and an answer derived across that move is not the answer this file
        would be read back as."""
        env = dict((k, v) for k, v in plan["env"])
        fp, facts = foldckpt.fingerprint(gitdir, env)
        if fp is None:
            return None, False
        if any(seen not in (None, facts["shallow"])
               for seen in plan["shallow"]):
            return None, False
        if plan["machinery"] and facts["inside"] != "false":
            # A work tree's own .gitattributes steer merge-tree and are not in
            # the fingerprint, so a derivation taken inside one is not reusable
            # — `foldckpt._header` refuses a fold for the same measurement.
            record.swallow("carriageckpt.unplannable", foldckpt.Unplannable(
                "merge machinery ran inside the work tree %s" % gitdir))
            return None, False
        exprs = {expr for expr, _obs in plan["obs"]}
        if old is not None:
            exprs |= set(old["exprs"])
        lines = foldckpt.batch_check(gitdir, env, exprs)
        if lines is None:
            return None, False
        if not all(foldckpt.agrees(expr, obs, lines[expr])
                   for expr, obs in plan["obs"]):
            return None, False
        # ONE SPELLING ON BOTH SIDES. The file holds the environment as JSON
        # lists and `foldckpt.plan` hands it over as tuples, which never
        # compare equal: unnormalized, every write would discard the entries
        # it should keep. `foldckpt.Session._header` normalizes the same way.
        rows = [list(kv) for kv in plan["env"]]
        keep = old is not None and old["env"] == rows \
            and old["fingerprint"] == fp \
            and all(lines.get(expr) == line
                    for expr, line in old["exprs"].items())
        mine = {expr: lines[expr] for expr, _obs in plan["obs"]}
        return ({"env": rows, "fingerprint": fp,
                 "exprs": dict(lines) if keep else mine}, keep)


def _section_ok(section):
    """A stored repository section has the shape this module wrote."""
    if not isinstance(section, dict) or set(section) != {
            "env", "fingerprint", "exprs"}:
        return False
    if not isinstance(section["fingerprint"], str) \
            or not isinstance(section["exprs"], dict) \
            or not isinstance(section["env"], list):
        return False
    if not all(isinstance(pair, list) and len(pair) == 2
               and isinstance(pair[0], str) for pair in section["env"]):
        return False
    return all(isinstance(k, str) and isinstance(v, str)
               for k, v in section["exprs"].items())


def _prune(keep):
    """Keep the newest KEEP files (one per code generation)."""
    root = os.path.dirname(keep)
    try:
        paths = sorted((os.path.join(root, n) for n in os.listdir(root)
                        if n.endswith(SUFFIX)),
                       key=lambda p: os.stat(p).st_mtime_ns, reverse=True)
    except OSError:
        return
    for path in paths[KEEP:]:
        if path != keep:
            try:
                os.unlink(path)
            except OSError:
                pass


@contextlib.contextmanager
def derived():
    """Serve carriage replay answers from the store for this walk, and write
    what the walk derives when it ends.

    -> the region, whose `served`/`derived`/`refused` counts are what an
    instrument reads; None when this process must not touch the store at all
    because it cannot name its own code (`foldckpt.policy`).

    ONE REGION PER WALK. A nested caller shares the outer one rather than
    opening a second: the file is written once, at the end, so two regions over
    one walk would have the inner one write a section the outer one is still
    adding derivations to.

    THE WRITE HAPPENS EVEN WHEN THE WALK RAISES. Every answer it derived was
    correct when it was taken and is already in the caller's hands; a census
    that ran out of budget half way still paid for what it derived."""
    outer = getattr(_LOCAL, "region", None)
    if outer is not None:
        yield outer
        return
    code = foldckpt.policy()
    region = _Region(code) if code is not None else None
    _LOCAL.region = region
    try:
        yield region
    finally:
        _LOCAL.region = None
        if region is not None:
            region.flush()


def replayed(gitdir, base, tip, trunk, compute):
    """`compute()`'s answer for this (base, tip, trunk object), re-derived
    through this door when it can be and taken live when it cannot.

    `compute` is the witness itself and is called exactly once on a miss, so
    the answer a caller gets is always one measurement of one relation — never
    a reconstruction of one."""
    region = getattr(_LOCAL, "region", None)
    key = _key(gitdir, trunk, base, tip)
    if region is None or key is None or foldckpt.recording_active():
        return compute()
    got = region.stored(key)
    if got is not MISS:
        return got
    with foldckpt.recording() as rec:
        answer = compute()
    region.remember(key, answer, rec)
    return answer
