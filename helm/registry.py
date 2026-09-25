#!/usr/bin/env python3
"""The helm registry — the master project list + per-project overlay pointers.

Two files under the helm home, composed at read time:
  registry.json           the PROJECTION of observed reality (harness stores +
                          disk) — rebuildable from a re-scan, safe to regenerate.
  registry-authored.json  the AUTHORED layer (AUTHORED_FIELDS: what a human or
                          agent wrote) — keyed by project name, path-stamped,
                          unrebuildable, never regenerated.
load() returns the merged view (authored fields overlay their path-matched
projection record; external anchors materialize even with no projection record)
so every caller sees the pre-split shape; save() splits the merged view back
out. A mixed-era registry.json migrates its authored fields out ONCE, at load,
losslessly. Sync law: additive and idempotent — a re-sync refreshes
observations, never deletes a known project, never touches authored fields.

Adoption law: where a project already has an external knowledge home (declared
host-local as its authored `adopt` path), ~/.helm/<name> becomes a SYMLINK to
it — one chain, never a second copy.

Also home of the PROJECTION REGISTRY (projections() + projection_survey()):
constitution laws 2+3 as an executable manifest — every on-disk store helm
reads or writes declares its class, and every projection its source + rebuild.
`helm projections` is the read surface; doctor.check_projection_registry
enforces it.
"""
import contextlib
import copy
import fcntl
import fnmatch
import functools
import json
import os
import pathlib
import shutil
import string
import sys
import threading
import time
import uuid

from . import automap, home, pk

_WRITE_THREADS = threading.RLock()
_WRITE_LOCAL = threading.local()


@contextlib.contextmanager
def _write_lock():
    """Serialize the existing split writer, including nested sync/save/load.

    Atomic rename prevents torn JSON, not a save overwriting a concurrently
    authored forget. All owner write doors share this lock; strict snapshots
    and dry-run membership commands remain read-only.
    """
    path = os.path.join(home.global_dir(), ".state", "registry.lock")
    with _WRITE_THREADS:
        held = getattr(_WRITE_LOCAL, "held", {})
        key = (os.getpid(), path)
        if key in held:
            yield
            return
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            _WRITE_LOCAL.held = held
            held[key] = True
            try:
                yield
            finally:
                del held[key]
                fcntl.flock(lock, fcntl.LOCK_UN)


def _serialized(fn):
    @functools.wraps(fn)
    def write(*args, **kwargs):
        with _write_lock():
            return fn(*args, **kwargs)
    return write


# Authored fields on a project record that a re-sync must never clobber.
#
# `gate` IS THE PROJECT'S OWN SUITE COMMAND, and it is authored here for the
# same reason `adopt` is: the projection is rebuildable from a re-scan and a
# declaration is not. helm gated every repo with its OWN command — a module
# constant in `helm/gate.py` read with no question about which repo it had been
# handed — so a gate on an adopter's project spawned python unittest discovery
# in a tree that has no `tests/` directory and minted an unbindable receipt
# about a command the project never asked for. A team USING helm must never
# need to know how helm is made, so the project declares and helm asks.
# See `gate.suite_command` for the shape and `gate.DECLARED_GATE_FIELD`.
AUTHORED_FIELDS = ("edges", "notes", "aliases", "external", "retired", "adopt",
                   "gate", "state", "residency")

# The project STOPLIGHT — and it is the SAME light the registry already shows,
# with the vocabulary it was always missing, NOT a second mark beside it.
#
# `status` (active / dormant / external) is that light today and every value is
# DERIVED from what the scan saw. Those values are not mutually exclusive with a
# stoplight, they are an incomplete stoplight: "active" already means green and
# "dormant" already means nothing-happening. What the derived set cannot say is
# anything the owner DECIDED — frozen, constrained, maintenance-only — because
# a scan can only report what happened, never what is wanted.
#
# So `state` is the AUTHORED value of the same light, and it SUPERSEDES the
# derived one wherever the light is read: authored when authored, derived
# otherwise. One light, one meaning ("what is this project's state right now"),
# and a single place to look.
#
# CREDS ARE SUPPLY; A PROJECT IS AUTHORIZATION, AND AUTHORIZATION OUTRANKS
# SUPPLY. Every routing surface helm has today reads a credential axis (the
# family burn flags: green / yellow / orange / red) and nothing reads a project
# one, so a seat with a green family had no door that says not-here-not-now and
# the owner had to say it by hand, once per seat, every time. A green credential
# is CAPACITY AND NEVER PERMISSION: a red project refuses work however much
# headroom the family has, and a green project behind a walled family is a
# routing problem rather than a stop.
#
# The authored colours extend the derived vocabulary rather than replacing it:
# `green` is what `active` always meant, and yellow / orange / red are the three
# things only a person can say.
STATE_COLOURS = ("green", "yellow", "orange", "red")

# Authored fields the mixed-era MIGRATION below must never adopt from the
# projection, and `gate` is the first of them because it is EXECUTABLE.
#
# The migration exists to rescue authored values that a pre-split helm wrote
# INLINE into registry.json, and for `notes` or `edges` that rescue is a
# convenience. For a command helm will SPAWN it is an authority laundering
# channel: registry.json is the projection — rebuildable by any re-scan and
# written by every discovery pass — so a `gate` block appearing there would be
# copied into the authored file by the next ordinary `load()` and thereafter be
# indistinguishable from a block the owner wrote. There is no mixed era to
# rescue, either: the field is introduced WITH the split, so nothing legitimate
# can already be inline. `save()` still routes it to the authored layer and
# `AUTHORED_FIELDS` still protects it from a re-sync clobber; what it never gets
# is promotion. `gate.suite_command` reads the authored layer directly and
# reports an inline block as a PROJECTION, which is the other half of this.
#
# `state` IS THE SECOND, AND FOR THE SAME REASON ONE STEP REMOVED. It executes
# nothing, but it is what a router will read to decide whether work may start
# in a project at all, so a projection that could mint one would let any
# discovery pass switch a project off (or back on) in the owner's name. It is
# also introduced WITH the split, so there is no inline copy to rescue. `light`
# therefore takes authorship from the authored layer and never from the merged
# record, exactly as `gate.suite_command` does.
#
# `residency` IS THE THIRD. It decides whether a project's turn text may leave
# the operator's LAN for an outside scorer, and sending data off the LAN cannot
# be undone, so only a person may say yes. A projection that could mint one
# would let a discovery pass publish a client's text in the owner's name.
NEVER_MIGRATED_FIELDS = ("gate", "state", "residency")


def _unowned(entry):
    """The keys of an authored entry that THIS code does not own: everything
    but AUTHORED_FIELDS and `path`, deep-copied.

    EVERY WRITER CARRIES THEM THROUGH, AND NONE OF THEM DELETES ONE. The
    authored layer is written by more than one version of helm at once: a
    field a newer helm authors is a key an older writer has never heard of.
    A writer that rebuilt an entry from the fields it knew erased that key on
    its next ordinary save, silently, in the owner's name. Measured on the
    residency field: an installed helm without it would have dropped two
    projects' `may-leave-lan` on the next `helm sync`, and outside scoring
    would have stopped with nothing saying why. So a read-modify-write keeps
    every key it does not own exactly as it found it; removing one is the act
    of the writer that owns it."""
    if not isinstance(entry, dict):
        return {}
    return {k: copy.deepcopy(v) for k, v in entry.items()
            if k not in AUTHORED_FIELDS and k != "path"}

# The separator helm puts between a project NAME and the path stamp it computes
# for a second-location entry — and it is "/" because the project-name grammar
# (`_checked_value`) REFUSES "/" in a name. That refusal is what makes the two
# spellings disjoint BY CONSTRUCTION: no legal name can be spelled like a stamped
# key, so no decoder has to guess which one it is holding.
#
# "@" STOOD HERE AND WAS NOT DISJOINT. `team@project` is a legal registry name,
# so a key and a stamp could be spelled identically, and every reader had to
# decide between them by recomputing the stamp — which answers correctly for
# helm's own keys and cannot answer at all for a legal name the owner chose to
# be `alpha@<the 8 hex digits sha1 of its own path>`. That name decoded to
# `alpha`, so enumeration published one spelling while every lookup
# (`authority_record`, `declaration_provenance`) and every withdrawal filter used
# the other: a genuine receipt was refused for the project that had just minted
# it, and a forgotten name kept an active declaration. One spelling everywhere is
# the cure, and the grammar is what guarantees it.
#
# A HISTORICAL "@" STAMP IS DECODED ONLY WHERE THE REGISTRY ITSELF PROVES WHO
# WROTE IT, and never by recomputing the hash alone. Reading such a key whole
# was fail-closed for a declaration and NOT fail-closed for membership: the
# stamping producer that shipped before this separator wrote `name@<stamp>` for
# a repointed project's CURRENT location, so a host carrying an ordinary
# base-era binding had its authored entry for that location keyed in a spelling
# no lookup here could form. `_apply_bindings` then refused the intact binding,
# every strict load raised, and a strict-load failure is what `project_state`
# answers UNKNOWN for — so the whole registry went unavailable on a host that
# had done nothing but repoint a project.
#
# RECOMPUTATION ALONE CANNOT DECIDE IT, which is why the proof is membership and
# history rather than the hash. An owner may legally name a project
# `alpha@<the 8 hex digits sha1 of alpha's own path>`; that name recomputes, so a
# recomputing decoder shortens it to `alpha` and publishes one spelling while
# every lookup and every withdrawal record keeps the other. `_proven_legacy_stamps`
# therefore builds the decode table from the authored file's OWN records — a
# departed-location archive (`forgotten_projects`) and a repoint archive
# (`project_bindings`, whose `locations` and transitions name every location the
# stamping producer ever stamped for that project). A full "@" spelling with no
# such record is a genuine whole NAME and stays whole.
#
# THE REGISTRY VERSION IS NOT THE DISCRIMINATOR, and this is the one the file
# does not carry: both layers are written `{"version": 1, ...}` and the split
# never bumped it, so "version older than this grammar" is true of every file on
# disk and would decode the legal lookalike above in all of them. Membership and
# history are what the bytes actually record, so they are what is read.
#
# THE RE-SPELLING IS A WRITE DOOR: `save` re-keys a proven historical stamp into
# the computed grammar (`_rekey_legacy_stamps`), so the migration happens once,
# where the owner's bytes are already being rewritten, and no read mutates the
# authored layer. MEASURED on this host: 12 authored project keys, 6 forgotten
# archives and 1 binding, and not one key of any of them carries "@" at all —
# which is why the compatibility has to be PROVEN from the file rather than
# measured from this box.
_STAMP_SEP = "/"
_LEGACY_STAMP_SEP = "@"
_STAMP_HEX = 8
_STAMP_ALPHABET = frozenset(string.hexdigits.lower())


def same_location(one, other):
    """Do two recorded paths name the same directory?

    A path stamp is compared literally first because that is free and is what
    matches in every ordinary case; the resolved compare exists because a
    registry path and a caller's realpath'd root can spell one directory two
    ways (a symlinked TMPDIR, an aliased checkout) and an authority answer must
    not turn on the spelling. Resolution failure answers NO: an unresolvable
    path establishes nothing.
    """
    if not isinstance(one, str) or not isinstance(other, str) \
            or not one or not other:
        return False
    if one.rstrip(os.sep) == other.rstrip(os.sep):
        return True
    try:
        return str(pathlib.Path(one).resolve(strict=True)).rstrip(os.sep) \
            == str(pathlib.Path(other).resolve(strict=True)).rstrip(os.sep)
    except (OSError, RuntimeError):
        return False


def _logical_name(key, path, proven=None):
    """The project NAME inside an authored key, undoing only a stamp helm minted.

    `key.split("@", 1)[0]` stood here and read "everything before the first @"
    as the name. `_checked_value` admits a project name CONTAINING one —
    `team@project` is a legal registry name — so that split truncated such a
    name to `team`, and every consumer then compared a name no registry holds:
    a same-path gate declaration was minted under the full label and the receipt
    origin check rejected helm's own receipt for a project it had just resolved.

    RECOMPUTING THE STAMP FIXES THE ORDINARY NAME AND NOT THE GRAMMAR. While a
    legal name CAN be spelled like a stamp, "is this a stamp?" has no answer
    from the key alone, and the one case recomputation cannot decide is a real
    name the owner chose to be `alpha@<sha1 of alpha's own path>[:8]`: it
    recomputes, so it would decode to `alpha`, and the enumerated spelling then
    disagrees with every lookup and every withdrawal filter that uses the key.

    SO THE COMPUTED GRAMMAR IS DISJOINT, and the first question here reads only
    it: a key helm stamps carries `_STAMP_SEP`, which `_checked_value` refuses
    inside a NAME. A stamp-shaped key whose recorded path no longer hashes to it
    keeps its full spelling and matches nothing — fail-closed, never a shortened
    label.

    A HISTORICAL "@" STAMP IS DECODED FROM `proven`, NEVER FROM THE HASH. The
    stamping producer that shipped before `_STAMP_SEP` wrote `name@<stamp>` for a
    repointed project's own CURRENT location, so reading every such key whole
    made `_apply_bindings` refuse an intact base-era binding and took the whole
    registry to UNKNOWN. `proven` is `_proven_legacy_stamps`'s table, built from
    the authored file's own departed-location and repoint archives: a key in it
    was demonstrably written by that producer for the (name, location) the table
    names, and decoding it is reading the file's own record rather than guessing
    from a hash. A full "@" spelling the table does not carry is a genuine whole
    NAME and stays whole — which is why the table, and not recomputation, is the
    argument.
    """
    head = _stamped_head(key)
    if head is not None and _qualified(head, path) == key:
        return head
    decoded = (proven or {}).get(key)
    if decoded is not None and same_location(decoded[1], path):
        return decoded[0]
    return key


def _registered_locations():
    """{project -> its registered location}, from the MERGED membership view.

    The one place a PATHLESS authored declaration can get a location from.
    Tombstoned membership is already absent from `load`, so a withdrawn
    project's pathless declaration resolves to nothing here — a location is
    where the project is registered NOW, never where it was archived from.
    """
    return {name: rec["path"] for name, rec
            in load(strict=True).get("projects", {}).items()
            if isinstance(rec, dict) and isinstance(rec.get("path"), str)
            and rec["path"]}


def authored_declarations(field):
    """Every AUTHORED entry carrying `field`, as a tuple of records.

    THE AUTHORED FILE AND NOT THE MERGED VIEW, which is the entire point of
    this door. `load()` merges the projection under the authored layer and
    MIGRATES an inline value into it, so a caller that reads the merged record
    cannot tell a declaration the owner wrote from a value a scan-rebuilt file
    happened to carry. A consumer that EXECUTES what it reads needs the
    distinction, so it asks here.

    Each record is {"project", "path", "value"}: `project` is the logical name
    (an entry authored against a second path is keyed `name@stamp`, and the
    stamp is a collision key rather than part of the name — see
    `_logical_name`), `path` is the location the declaration applies to, and
    `value` is the raw authored block — validated by its consumer, never here.

    EVERY RECORD NAMES A LOCATION, and consumers rely on that. The DOCUMENTED
    shape of a declaration carries no `path` at all (the owner writes one block
    per project, not one per directory), and emitting "" for it made
    `gate._root_declaration` drop the record: such a declaration worked at the
    registered root through `declaration_provenance` and refused in every linked
    worktree of that root, which is exactly where a lane's gate runs. A pathless
    entry therefore resolves to the project's REGISTERED location, and an entry
    that resolves to no location at all is not emitted — it declares for a
    project this registry has no membership for.

    AND ONE SPELLING ANSWERS FOR EACH ENTRY. `_forgotten` and `_bindings` key
    their records by the project's logical name, and the inactive lookup below
    uses `_logical_name`'s answer for the authored key — so the two must agree
    exactly. They do because the grammar makes names and stamps DISJOINT
    (`_STAMP_SEP`): before that, a legal name spelled like a computed stamp was
    published here shortened while every withdrawal record kept it whole, and the
    filter below could not see the archive that had withdrawn it. A key in the
    historical grammar is decoded from `_proven_legacy_stamps`, which is built
    from those same withdrawal and repoint records, so it too resolves to the one
    spelling those records use.

    AND THE ENUMERATION IS THE RESOLVER READ BACKWARDS. A key is emitted only
    where `authority_record` resolves its decoded (name, location) back to that
    same key — so a record this enumeration publishes is the record every lookup
    finds, a shadowed spelling is never published beside the one that governs,
    and a (name, location) two keys hold raises here exactly as it raises at the
    lookup door.

    AND A FULL SPELLING NOTHING DECODED DECLARES ONLY WHERE MEMBERSHIP HOLDS IT,
    WHOLE. A key that WEARS a stamp's shape and that neither grammar decoded is a
    key this reader can infer no project from: a computed stamp whose recorded
    path no longer hashes to it, or a historical one the file has no record for.
    Emitting it under its own full spelling published a `project` no membership
    answers to — a declaration nothing can withdraw, because every withdrawal
    verb works on names. So such a key is emitted ONLY when the merged membership
    holds a project registered under that exact whole spelling at that exact
    location, which is what makes the legal `alpha@<sha1 of alpha's own path>[:8]`
    a declaring name and a stale stamp nothing at all. This is enforced here,
    in the producer: a consumer cannot tell the two apart from the record.

    A WITHDRAWN MEMBERSHIP DECLARES NOTHING, and there are two ways to withdraw
    one. `forget` archives the record and KEEPS the authored entry so `restore`
    can put it back, while membership itself is tombstoned by `load`; `repoint`
    moves a project to a new location and keeps the departed one as a reversible
    binding snapshot, publishing the authored fields under the TARGET's stamp
    while the source entry stays in the file. Enumerating the authored file alone
    re-armed a declaration for both — so whatever reappeared at a forgotten or
    departed path (possibly a different repository entirely) would have been
    gated with that command, ahead of the unregistered refusal and with no
    restore or repoint anywhere in the story. An entry whose (name, location) is
    not the project's CURRENT membership is INACTIVE until a membership verb
    makes it current again. This is the same law `adopted_homes` and `sync`
    already keep for historical path stamps, and a different mechanism from the
    projection-to-authored promotion every writer guards against: the value here
    is genuinely authored, and what is missing is the membership.

    RAISES on an authored layer that exists and cannot be read: an unreadable
    authority is not an empty one, and returning () would read as "nobody
    declared anything".
    """
    auth = _authored_load(strict=True)
    entries = auth.get("projects", {})
    if not isinstance(entries, dict):
        raise ValueError("authored project population is malformed")
    inactive = {}
    for who, row in _forgotten(auth).items():
        inactive.setdefault(who, []).append(row["record"]["path"])
    for who, binding in _bindings(auth).items():
        inactive.setdefault(who, []).extend(
            loc for loc in binding["locations"] if loc != binding["path"])
    proven = _proven_legacy_stamps(auth)
    registered = None
    out = []
    for key, entry in sorted(entries.items()):
        if not isinstance(entry, dict) or entry.get(field) is None:
            continue
        path = entry.get("path")
        path = path if isinstance(path, str) and path else ""
        name = _logical_name(key, path, proven)
        if not path:
            if registered is None:
                # READ LAZILY, and only for an entry whose location or identity
                # the authored layer cannot answer on its own: an authored file
                # with none never opens the projection through this door, so
                # every refusal a caller pins keeps its shape.
                registered = _registered_locations()
            path = registered.get(name) or ""
            if not path:
                continue
        elif name == key and _stamp_shaped(key) is not None:
            # A FULL SPELLING NOTHING DECODED, HELD TO MEMBERSHIP. See the
            # docstring: this is the fail-closed half, and it belongs here
            # because only the producer can see WHICH question the key failed.
            if registered is None:
                registered = _registered_locations()
            if not same_location(registered.get(name) or "", path):
                continue
        if any(same_location(where, path) for where in inactive.get(name, ())):
            continue
        if authority_record(auth, name, path)[0] != key:
            # THE RESOLVER DOES NOT RESOLVE (name, location) BACK TO THIS KEY:
            # a spelling shadowed by the one that governs, or a record whose
            # decoded identity another entry holds. Not this reader's to publish.
            continue
        out.append({"project": name, "path": path, "value": entry[field]})
    return tuple(out)


def declaration_provenance(name, path, field):
    """Where this project's `field` value comes from -> (value, origin).

    origin is "authored" when the OWNER's authored file carries the value for
    this (name, path), "projection" when only the scan-rebuilt registry.json
    does, and None when neither does — and the caller gets to treat those as
    three different answers. A projection value is reported rather than hidden
    so a consumer can say "this is a projection, not a declaration" instead of
    refusing as though nothing were written at all.

    THE RECORD IS THE RESOLVER'S ANSWER (`authority_record`), so this lookup and
    `authored_declarations`'s enumeration cannot disagree about one entry: the
    enumeration emits a key only where the resolver resolves back to it.
    """
    _key, entry = authority_record(_authored_load(strict=True), name, path)
    if isinstance(entry, dict) and entry.get(field) is not None:
        return entry[field], "authored"
    rec = _checked_registry(home.registry_path()).get("projects", {}).get(name)
    if isinstance(rec, dict) and rec.get(field) is not None \
            and same_location(rec.get("path"), path):
        return rec[field], "projection"
    return None, None

# Knowledge homes helm adopts by symlink instead of scaffolding. EMPTY in the
# shipped tree — no site-specific home path lives in code. A deployment declares
# each adoption host-local as the project's authored `adopt` path
# (registry-authored.json), read back by adopted_homes(); this mirrors how a
# project's authored `aliases` config-drive the drain routing.
def adopted_homes():
    """{project -> external home dir}, never historical binding storage keys.

    Unbound authored adoption config retains its pre-repoint behavior, including
    config awaiting discovery. Bound names read only the active merged record;
    historical/qualified path stamps must not route sync, doctor or whoami.
    """
    auth = _authored_load(strict=True)
    bindings = _bindings(auth)
    entries = dict(auth.get("projects", {}))
    if bindings:
        active = load(strict=True)["projects"]
        for name, binding in bindings.items():
            entries.pop(name, None)
            for path in binding["locations"]:
                for stamped in _stamp_keys(name, path):
                    entries.pop(stamped, None)
            if name in active:
                entries[name] = active[name]
    out = {}
    for name, entry in entries.items():
        path = entry.get("adopt")
        if isinstance(path, str) and path.strip():
            out[name] = os.path.expanduser(path.strip())
    return out


def _checked_registry(path):
    """Strict read for authority roots; missing is empty, failed is not."""
    value = pk.read_json(path, {"version": 1, "projects": {}}, strict=True)
    return _checked_value(value)


def name_is_malformed(name):
    """Is this outside the registry's OWN project-name contract?

    THE ONE SPELLING, and the reason it is a function rather than a clause: the
    strict population reader below is not the only door that decides whether a
    name is a project. `helm store rescope` RECORDS a project name on a store
    entry, and every reader downstream compares that recorded name EXACTLY. A
    second spelling at the recording door is how a name the registry admits —
    internal spaces included, because this contract forbids only an empty name,
    a dot name and a path separator — gets rewritten into a name no project has,
    matching nothing while the door reports success.

    NOT a membership test: `fleet` is a scope, not a registered project, and a
    name may be recorded before its project is adopted."""
    return (not isinstance(name, str) or not name or name in (".", "..")
            or "/" in name or "\\" in name)


def _checked_value(value):
    """The same population contract for live records and restorable archives."""
    if not isinstance(value, dict) or not isinstance(value.get("projects"), dict) \
            or any(not isinstance(name, str) or not isinstance(rec, dict)
                   for name, rec in value["projects"].items()):
        raise ValueError("registry project population is malformed")
    for name, rec in value["projects"].items():
        # THE GRAMMAR THAT MAKES A NAME AND A STAMP DISJOINT. A key is either a
        # project NAME — no `_STAMP_SEP` anywhere in it — or a name helm stamped
        # with a computed location suffix, which is the ONLY way that separator
        # can appear. So a key of the form `alpha/<8 hex digits>` is admitted as a
        # stamped key while `alpha/beta` and `alpha/notahex` are refused as names,
        # and no legal name
        # can ever be spelled like a stamp. See `_STAMP_SEP` for why that matters.
        #
        # THE NAME CONTRACT ITSELF STAYS IN `name_is_malformed`, asked here about
        # the LOGICAL name a key carries. Decoding the stamp is this reader's job;
        # deciding what a NAME may be is not, because this is not the only door
        # that decides it — `helm store rescope` records a name and asks the same
        # function about the bare word. Two spellings of that contract is how a
        # name one door admits becomes a name the other rewrites, so there is one.
        if name_is_malformed(_stamped_head(name) or name):
            raise ValueError("registry project name is malformed")
        for field in ("path", "name"):
            if field in rec and (not isinstance(rec[field], str) or not rec[field]):
                raise ValueError("registry project identity is malformed")
        if "cwds" in rec and (not isinstance(rec["cwds"], list) or
                any(not isinstance(cwd, str) or not cwd for cwd in rec["cwds"])):
            raise ValueError("registry project cwd population is malformed")
        if "cv_scope" in rec:
            scope = rec["cv_scope"]
            if not isinstance(scope, dict):
                raise ValueError("registry project scope is malformed")
            if "cwd_prefixes" in scope and (not isinstance(scope["cwd_prefixes"], list) or
                    any(not isinstance(cwd, str) or not cwd for cwd in scope["cwd_prefixes"])):
                raise ValueError("registry project prefixes are malformed")
    return value


def _authored_load(strict=False):
    """The authored layer, with a corruption net: an unparseable file is backed
    up beside itself BEFORE any caller can save over it — authored content is
    unrebuildable, so a garbled byte must never cascade into an empty rewrite
    (cross-family review finding, 2026-07-19)."""
    path = home.authored_path()
    if strict:
        return _checked_registry(path)
    val = pk.read_json(path)
    if isinstance(val, dict):
        return val
    if os.path.exists(path):
        # THE DIGEST IS THE BACKUP'S IDENTITY, as it is for the accounts file:
        # a name carrying a clock stamp mints another copy of the same bytes on
        # every read, and this layer has readers that run inside every turn.
        # One corruption backs up once; a different one still gets its own copy.
        import hashlib
        try:
            with open(path, "rb") as f:
                bak = path + ".corrupt-" + hashlib.sha256(f.read()).hexdigest()[:12]
        except OSError as e:
            bak = None
            print("[helm registry] authored layer %s unreadable — corrupt "
                  "backup impossible (%s)" % (path, e), file=sys.stderr)
        if bak and not os.path.exists(bak):
            try:
                shutil.copy2(path, bak)
            except OSError as e:
                # the net covers UNPARSEABLE bytes; a file that cannot even be
                # READ (permission, I/O) cannot be netted — report it instead
                # of crashing every load(). Readers that must not silently
                # degrade to the empty layer (drain's alias routing) probe the
                # file themselves and refuse.
                print("[helm registry] authored layer %s unreadable — corrupt "
                      "backup impossible (%s)" % (path, e), file=sys.stderr)
    return {"version": 1, "projects": {}}


class AuthoredUnreadable(Exception):
    """The authored layer EXISTS but cannot be read/parsed. Raised by
    authored_host() so a host-config reader REFUSES rather than degrading to the
    empty layer — an unreadable source is indistinguishable from 'unconfigured'
    and would silently drop the host's config (skills hub, canonical MCPs,
    private powerpacks) exactly as if nothing had been set. Same discipline as
    drain's unreadable-alias refusal (see _authored_load's note)."""


def authored_host():
    """The host-wide authored config block — registry-authored.json's top-level
    `host` object — read DIRECTLY, because it never surfaces through load()'s
    projects-merge. Returns {} when the authored file is ABSENT (a legitimately
    empty layer) or carries no `host` key. RAISES AuthoredUnreadable when the
    file EXISTS but cannot be parsed: a host-config reader MUST refuse rather
    than silently degrade to empty (a garbled byte would drop the operator's
    config the same shape as never having set it — OI cross-family finding
    2026-07-30). Absent-vs-unreadable is exactly the distinction drain draws for
    its alias layer."""
    path = home.authored_path()
    if not os.path.exists(path):
        return {}
    val = pk.read_json(path)
    if not isinstance(val, dict):
        _authored_load()  # trigger the corruption-backup net before refusing,
        raise AuthoredUnreadable(path)  # so the .corrupt-* backup is guaranteed
    host = val.get("host")
    return host if isinstance(host, dict) else {}


def author_host(key, value):
    """Author ONE key of the host block, the write half of `authored_host()`.
    Returns what the key held before.

    THE SAME WRITER DISCIPLINE AS EVERY PROJECT DOOR: the shared write lock, a
    STRICT read of the whole layer, one atomic rewrite of it. A layer that
    cannot be read RAISES (OSError / ValueError) and nothing is written, so an
    owner setting can never be saved over authored content this process could
    not see. A `host` that is present and is not an object is refused for the
    same reason: replacing it would erase whatever a person put there."""
    with _write_lock():
        auth = _authored_load(strict=True)
        host = auth.setdefault("host", {})
        if not isinstance(host, dict):
            raise ValueError("authored host block is malformed")
        was = host.get(key)
        host[key] = value
        pk.write_json(home.authored_path(), auth)
        return was


def _stamp(path):
    """The 8 hex digits a location contributes to a qualified key."""
    import hashlib
    return hashlib.sha1((path or "").encode()).hexdigest()[:_STAMP_HEX]


def _qualified(name, path):
    """The collision key for a same-name entry authored against a different
    path — both survive; the path stamp decides which one a project reads.

    THE SEPARATOR IS ONE THE NAME GRAMMAR REFUSES (see `_STAMP_SEP`), so this
    spelling is reachable only by computing it and no legal project name can
    collide with it.
    """
    return "%s%s%s" % (name, _STAMP_SEP, _stamp(path))


def _legacy_qualified(name, path):
    """The pre-disjoint spelling of the same key, for the two doors that must
    keep EXCLUDING a historical path stamp from live routing. It is never
    decoded into a logical name — see `_STAMP_SEP`."""
    return "%s%s%s" % (name, _LEGACY_STAMP_SEP, _stamp(path))


def _stamp_keys(name, path):
    """Every spelling a HISTORICAL path stamp for (name, path) can wear.

    The doors that must keep a historical stamp OUT of live routing ask this,
    because a host that stamped keys before the separator became disjoint still
    has those keys on disk and they still name the same departed location. This
    is recognition, never decoding: `_logical_name` reads only the computed
    grammar, so a legacy key still names no project and declares nothing.
    """
    return (_qualified(name, path), _legacy_qualified(name, path))


def _stamped_head(key, sep=_STAMP_SEP):
    """The name half of a key spelled like a stamp under `sep`, else None.

    SHAPE ONLY: whether the stamp is the RIGHT one for a given path is
    `_logical_name`'s question, and whether the key is legal at all is
    `_checked_value`'s. A key is stamp-SHAPED when it carries the separator and
    its tail is exactly the stamp's hex width.
    """
    head, found, tail = key.rpartition(sep)
    # THE ALPHABET IS DERIVED, NOT TYPED. A literal run of hex digits in this
    # tree is a CITED COMMIT to the docref rung, which refuses the commit for a
    # sha no clone can resolve; `string.hexdigits` lowercased is 0-9a-f exactly,
    # which is what `hashlib.hexdigest` emits.
    if not found or not head or len(tail) != _STAMP_HEX \
            or any(c not in _STAMP_ALPHABET for c in tail):
        return None
    return head


def _stamp_shaped(key):
    """Is this key spelled like ANY stamp helm has ever computed? -> head or None.

    BOTH GRAMMARS, because the consumer of this question is fail-closed
    enumeration: a key that WEARS a stamp's shape and that nothing decoded is a
    key no membership can be inferred from, whichever separator it wears.
    """
    for sep in (_STAMP_SEP, _LEGACY_STAMP_SEP):
        head = _stamped_head(key, sep)
        if head is not None:
            return head
    return None


def _proven_legacy_stamps(auth):
    """{historical stamped key -> (logical name, location)} THE FILE PROVES.

    The decode table `_logical_name` and `authority_record` consult for a key
    spelled in the pre-`_STAMP_SEP` grammar. A key is in it only when the
    authored layer's OWN records show the stamping producer had that
    (name, location) pair to stamp:

      * `project_bindings[name]` — a repoint archive. Every key of `locations`,
        every `from`/`to` of the transition chain and the current `path` is a
        location that producer wrote an authored entry for, the CURRENT one
        included, which is the binding `_apply_bindings` could not otherwise
        find.
      * `forgotten_projects[name]["record"]["path"]` — a departed-location
        record, archived by `forget` under the project's own name.

    RAW AND DEFENSIVE, never `_bindings`/`_forgotten`. This table is consulted
    from inside those validators' own callers (`_apply_bindings` asks for it
    before it has finished validating anything), so it reads the dicts by shape
    and contributes nothing when they are malformed — a malformed layer is
    refused by the validators, on their own sentences, and a decode table that
    raised first would replace those sentences with this one.

    A HASH IS NEVER THE ARGUMENT BY ITSELF. `_legacy_qualified` is computed from
    a (name, location) the file RECORDS, so a full "@" spelling the file has no
    record for is absent here and stays a whole NAME — the legal
    `alpha@<sha1 of alpha's own path>[:8]` among them.
    """
    out = {}

    def offer(name, path):
        if isinstance(name, str) and name and isinstance(path, str) and path:
            out.setdefault(_legacy_qualified(name, path), (name, path))

    bindings = auth.get("project_bindings")
    if isinstance(bindings, dict):
        for name, binding in bindings.items():
            if not isinstance(binding, dict):
                continue
            offer(name, binding.get("path"))
            if isinstance(binding.get("locations"), dict):
                for path in binding["locations"]:
                    offer(name, path)
            if isinstance(binding.get("transitions"), list):
                for event in binding["transitions"]:
                    if isinstance(event, dict):
                        offer(name, event.get("from"))
                        offer(name, event.get("to"))
    forgotten = auth.get("forgotten_projects")
    if isinstance(forgotten, dict):
        for name, row in forgotten.items():
            if isinstance(row, dict) and isinstance(row.get("record"), dict):
                offer(name, row["record"].get("path"))
    return out


def _rekey_legacy_stamps(auth):
    """Re-spell every PROVEN historical stamp in the computed grammar -> bool.

    A WRITE DOOR AND NEVER A READ, which is the whole placement. The
    compatibility this closes is a decode (`_proven_legacy_stamps`), so every
    reader already answers correctly on the bytes as they stand; migrating them
    from a read would make `load` a writer of the unrebuildable layer for no
    gain. `save` is already rewriting the owner's authored file, so the
    re-spelling costs nothing extra and happens once.

    CONSERVATIVE ON EVERY AXIS: a key is moved only when the table proves it and
    its recorded `path` is the location the table names. Where the computed
    spelling is ALREADY TAKEN by the same record — the shape two shipped
    producers write between them, the stamping era's `name@<stamp>` and a later
    repoint's `name/<stamp>` for one location, same command, same provenance —
    the historical key is FOLDED AWAY, because the record it holds is the record
    that survives under the computed key. Taken by a DIFFERENT record, it is
    left exactly as found: this door converges a spelling, it never decides a
    collision, and the resolver refuses that one on its own sentence.
    """
    entries = auth.get("projects")
    if not isinstance(entries, dict):
        return False
    proven = _proven_legacy_stamps(auth)
    moved = False
    for key in sorted(entries):
        decoded = proven.get(key)
        entry = entries[key]
        if decoded is None or not isinstance(entry, dict) \
                or entry.get("path") != decoded[1]:
            continue
        fresh = _qualified(decoded[0], decoded[1])
        if fresh in entries:
            if _same_authority_record(entries[fresh], entry):
                del entries[key]        # one record, two eras' spellings
                moved = True
            continue
        entries[fresh] = entries.pop(key)
        moved = True
    return moved


# THE PROJECT AUTHORITY RECORD. One object, one resolver, one write slot.
#
# For a (project name, location) the authored layer holds EXACTLY ONE record,
# and every reader and every writer in this module asks `authority_record` which
# key holds it. Four rounds of review returned the same shape of finding twice
# each: a reader that had been handed the provenance table beside a writer that
# had not (`repoint` dropped an active declaration the readers could see), a
# refusal keyed on the NAME grammar rather than on this project's own record (an
# unrelated lawful `alpha@<hex>` at another location vetoed alpha's move), and a
# lookup that admitted a historical key by its spelling without reading the
# record's own location (a lawful name at Q captured as alpha's departed entry
# once alpha's binding proved the stamp). Each was the same defect: identity
# improvised at the door instead of resolved once. So:
#
#   * A KEY IS A LOOKUP DOMAIN, NEVER AN IDENTITY. The three spellings a record
#     can be filed under — the plain name, the computed `name/<stamp>`, the
#     historical `name@<stamp>` — are three places to LOOK for the record of
#     (name, location). A key is that record's only when the entry filed under
#     it RECORDS this location (the plain name may also record none, which is
#     the documented one-block-per-project shape), and the historical spelling
#     only when the file's own history proves the stamping producer wrote it for
#     exactly this pair (`_proven_legacy_stamps`). A lawful name spelled like a
#     stamp at any other location is therefore never this project's record, and
#     can neither veto nor capture another project's move.
#   * TWO KEYS HOLDING THE SAME RECORD ARE ONE RECORD, and two keys holding
#     DIFFERENT records is ambiguous and REFUSES, naming both. The distinction
#     is CONTENT (`_same_authority_record`): a shipped writer lawfully published
#     one record under two spellings — the stamping producer wrote the
#     historical `name@<stamp>` for a location, and a later repoint that found
#     the plain name occupied chose the computed `name/<stamp>` for the SAME
#     location and published it beside the historical one, same command, same
#     provenance — so refusing that pair unconditionally took a registry those
#     two producers had written between them to UNKNOWN and refused the suite on
#     a host that had done nothing but repoint and undo. There is no ambiguity
#     to resolve where both keys say the same thing; the spelling the resolver
#     answers with is the one the write slot would choose anyway. Only a
#     DISAGREEMENT is refused, and never by domain order.
#   * A COLLAPSE IS A WRITE, NEVER A READ. `save` — and `_rekey_legacy_stamps`
#     inside it — folds an identical duplicate into the one slot; a read
#     resolves the pair and leaves the owner's bytes exactly as found, because
#     the unrebuildable layer has one writer and `load` is not it.
#   * WRITERS WRITE WHERE THE RESOLVER READS (`_authority_slot`): the key the
#     record already lives under, else the plain name when it is free, else the
#     computed stamp. A writer that computed its own slot beside the reader's
#     lookup is how two spellings of one record came to exist.
#   * `_authored_for` is the bare domain lookup over a raw `projects` dict, kept
#     because arms pin it and because the resolver is built on it. Production
#     asks `authority_record`, which builds the provenance table itself so no
#     caller can omit it.


def _same_authority_record(first, second):
    """Do two authored entries hold the SAME record? CONTENT, never spelling.

    The recorded location, and an equal value for EVERY OTHER KEY either entry
    carries — the executable `gate` declaration among them, and the keys this
    code does not own (`_unowned`), because folding a duplicate away deletes
    it and a key only the deleted spelling held would go with it. That is the
    whole content of an authored entry, so two keys that answer this are two
    spellings of one record and every reader gets the same bytes whichever one
    it is handed.

    THE KEYS ARE NEVER COMPARED. Which spelling a producer chose is exactly what
    differs here by construction, and it carries no authority: the historical
    `name@<stamp>` and the computed `name/<stamp>` for one location are two
    eras' spellings of the same thing. A difference in what the entries SAY —
    a different command, a different declaration — is a disagreement about
    authority and is never folded away.
    """
    if not isinstance(first, dict) or not isinstance(second, dict):
        return False
    if not same_location(first.get("path"), second.get("path")):
        return False
    keys = (set(first) | set(second)) - {"path"}
    return all(first.get(field) == second.get(field) for field in keys)


def _authority_keys(entries, name, path, proven):
    """Every key (name, path)'s record is filed under, in WRITE-SLOT ORDER.

    The plain name, then the computed stamp, then the historical one — the same
    order `_authority_slot` prefers, so the head of this list is the key a write
    would choose and `_authority_key` can answer with it.

    NO JUDGEMENT HERE: this lists the lookup domains that hold a record for this
    (name, location) and says nothing about whether they agree. The caller
    decides, on the records' CONTENT.
    """
    found = []
    entry = entries.get(name)
    if isinstance(entry, dict) and (not entry.get("path")
                                    or same_location(entry.get("path"), path)):
        found.append(name)
    stamped = _qualified(name, path)
    entry = entries.get(stamped)
    if isinstance(entry, dict) and same_location(entry.get("path"), path):
        found.append(stamped)
    legacy = _legacy_qualified(name, path)
    entry = entries.get(legacy)
    if isinstance(entry, dict) and (proven or {}).get(legacy) == (name, path) \
            and same_location(entry.get("path"), path):
        found.append(legacy)
    return found


def _authority_key(entries, name, path, proven):
    """The ONE key holding (name, path)'s record, or None.

    Raises when two keys hold DIFFERENT records, naming both. Two keys holding
    the SAME record are one record published under two spellings — a shape two
    shipped producers write between them — and resolve to the key a write would
    choose, which is the head of `_authority_keys`. The pair is left on disk:
    the collapse is `save`'s (see `_rekey_legacy_stamps`), never a read's.

    `proven` is `_proven_legacy_stamps`'s table for the layer `entries` came
    from; the resolver hands it down, the primitive accepts it, nobody guesses.
    """
    found = _authority_keys(entries, name, path, proven)
    if len(found) > 1 and not all(
            _same_authority_record(entries[found[0]], entries[key])
            for key in found[1:]):
        raise ValueError(
            "authored entries %s record DIFFERENT declarations for project %s "
            "at %s; one (project, location) holds exactly one record, so which "
            "of them is the authority is ambiguous — remove the duplicate"
            % (" and ".join(repr(key) for key in found), name, path))
    return found[0] if found else None


def _authored_for(entries, name, path, proven=None):
    """The bare domain lookup: (name, path)'s entry in a raw `projects` dict.

    Asked WITHOUT `proven` it cannot see a historical key, which is why no
    production door calls it: they ask `authority_record`, which supplies the
    table from the layer itself so the omission is not possible.
    """
    key = _authority_key(entries, name, path, proven)
    return entries[key] if key is not None else None


def authority_record(auth, name, path):
    """THE ONE RESOLVER: (key, entry) for the record (name, path) owns in the
    authored layer `auth`, or (None, None). Every reader (`_load`,
    `_apply_bindings`, `declaration_provenance`, `authored_declarations`) and
    every writer (`repoint`, `forget`, `restore`, `save`) asks this and nothing
    else; `_authority_slot` is where a writer PUTS the record, derived from it.

    Raises on a malformed population or on two keys holding one record — an
    ambiguous authority is refused, not resolved, and the sentence names both.
    """
    entries = auth.get("projects", {})
    if not isinstance(entries, dict):
        raise ValueError("authored project population is malformed")
    key = _authority_key(entries, name, path, _proven_legacy_stamps(auth))
    return (key, entries[key]) if key is not None else (None, None)


def _authority_slot(auth, name, path):
    """THE ONE WRITE SLOT for (name, path): the key its record already lives
    under, else the plain name while it is free, else the computed stamp."""
    key, _entry = authority_record(auth, name, path)
    if key is not None:
        return key
    return name if name not in auth.get("projects", {}) \
        else _qualified(name, path)


def _unaccountable_stamp(auth, name, where):
    """The key a MOVE of `name` through `where` refuses over, or None.

    An entry spelled as `name`'s historical stamp for `where`, RECORDED AT
    `where`, that the file's own history cannot account for. The move is what
    makes it dangerous: the binding it writes lists `where` among the project's
    locations, and from then on `_proven_legacy_stamps` would PROVE this key as
    the project's own — so an entry nobody authored as the project's (a legal
    name the owner chose, or a stamp whose archive was lost) would be captured
    as its departed record by the act itself. Refusing before the write is the
    only moment that can tell the two apart, and the owner can re-key it.

    THIS PROJECT'S OWN RECORD, NEVER THE NAME GRAMMAR. The same spelling filed
    under ANOTHER location is a lawful whole name at that location (see the
    resolver: a key is a record's only where the entry records the location), so
    it can neither be captured by the move nor veto it.
    """
    key = _legacy_qualified(name, where)
    entry = auth.get("projects", {}).get(key)
    if isinstance(entry, dict) and same_location(entry.get("path"), where) \
            and _proven_legacy_stamps(auth).get(key) != (name, where):
        return key
    return None


ARCHIVED_AUTHORED_KEY = "authored_fields"


def _archived_authored(row):
    """Which NEVER_MIGRATED fields an ARCHIVE records as authored -> frozenset.

    THE ARCHIVE HAS TO SAY SO ITSELF, and a row that does not say is not a row
    that said yes. `forget` writes this key on every archive it creates, so its
    ABSENCE means the archive was written by a helm that did not know the
    field was executable — a base-era `forget` archived the MERGED record, gate
    block and all, and its restore left that block in the projection because
    `gate` was not an authored field yet. Those bytes are legitimate and they
    record nothing about authorship; reading them as authored authority is
    `restore` supplying a provenance the file never carried.
    """
    marks = row.get(ARCHIVED_AUTHORED_KEY)
    return frozenset(marks) if isinstance(marks, list) else frozenset()


def _forgotten(auth):
    """Operator-authored membership removals, not scan-derived absence.

    The full record is archived in the existing authored layer. Its name AND
    path are reserved until restore, so discovery cannot resurrect the entry
    or graft its authored fields onto another repository with the same name.

    THE PROVENANCE MARK IS VALIDATED HERE or the archive is malformed: it must be
    a list of distinct NEVER_MIGRATED field names that the archived record
    actually carries. A mark for a field the record does not hold is a claim
    about nothing, and this reader refuses it rather than letting `restore`
    interpret it.
    """
    entries = auth.get("forgotten_projects", {})
    if not isinstance(entries, dict):
        raise ValueError("forgotten project population is malformed")
    for name, row in entries.items():
        if not isinstance(name, str) or not name or name in (".", "..") \
                or "/" in name or "\\" in name or not isinstance(row, dict):
            raise ValueError("forgotten project identity is malformed")
        rec = row.get("record")
        if not isinstance(rec, dict) or rec.get("name") != name \
                or not isinstance(rec.get("path"), str) or not rec["path"]:
            raise ValueError("forgotten project archive is malformed")
        marks = row.get(ARCHIVED_AUTHORED_KEY)
        if marks is not None and (not isinstance(marks, list)
                                  or len(set(marks)) != len(marks)
                                  or any(field not in NEVER_MIGRATED_FIELDS
                                         or rec.get(field) is None
                                         for field in marks)):
            raise ValueError("forgotten project provenance is malformed")
        _checked_value({"projects": {name: rec}})
    return entries


def _bindings(auth):
    """Validate imported location authority before exposing any membership.

    Snapshots are registry records needed to reverse a location decision, not
    copies of session stores. Each transition has an opaque binding generation;
    an old same-path projection cannot cross an A -> B -> A transition.
    """
    bindings = auth.get("project_bindings", {})
    if not isinstance(bindings, dict):
        raise ValueError("project binding population is malformed")
    for name, binding in bindings.items():
        if not isinstance(binding, dict):
            raise ValueError("project binding is malformed")
        locations = binding.get("locations")
        transitions = binding.get("transitions")
        if not isinstance(locations, dict) or not isinstance(transitions, list) or not transitions:
            raise ValueError("project binding history is malformed")
        for path, rec in locations.items():
            _binding_record(name, path, rec)
        previous = None
        generations = set()
        for event in transitions:
            if not isinstance(event, dict) or event.get("action") not in ("repoint", "undo"):
                raise ValueError("project binding transition is malformed")
            source, target = event.get("from"), event.get("to")
            if not isinstance(source, str) or not isinstance(target, str) or \
                    source not in locations or target not in locations or source == target or \
                    previous is not None and source != previous:
                raise ValueError("project binding chain is malformed")
            generation = event.get("generation")
            if not isinstance(generation, str) or len(generation) != 32 or \
                    any(c not in "0123456789" and c not in "abcdef" for c in generation) or \
                    generation in generations:
                raise ValueError("project binding generation is malformed")
            generations.add(generation)
            _binding_record(name, source, event.get("record"))
            previous = target
        if binding.get("generation") != transitions[-1]["generation"]:
            raise ValueError("project binding generation disagrees with history")
        if binding.get("path") != previous:
            raise ValueError("project binding current path disagrees with history")
    return bindings


def _binding_record(name, path, rec):
    if not isinstance(path, str) or not os.path.isabs(path) or \
            os.path.normpath(path) != path or not isinstance(rec, dict) or \
            rec.get("name") != name or rec.get("path") != path:
        raise ValueError("project binding record stamp is malformed")
    _checked_value({"projects": {name: rec}})


def _binding_reservations(bindings):
    reserved = {}
    for name, binding in bindings.items():
        for path in binding["locations"]:
            if path in reserved and reserved[path] != name:
                raise ValueError("project bindings reserve the same location")
            reserved[path] = name
    return reserved


def _apply_bindings(projects, auth):
    """Authored authority wins over stale or missing projections, fail closed.

    Historical authored path stamps stay intact, but cannot rematerialize old
    membership. Every exposing load validates imported cross-entry conflicts.

    AN INTACT BINDING WRITTEN BY THE OLD STAMPING PRODUCER STILL APPLIES. That
    producer keyed the target entry `name@<stamp>`, so the current-path lookup
    below found nothing on a host that had simply repointed a project and this
    function raised "no matching authored path stamp" for a binding with nothing
    wrong with it — taking every strict load, and therefore `project_state`, to
    UNKNOWN. `authority_record` decodes that key from the binding's own
    `locations` record: the proof is the file's history and not the hash, so a
    legal name that merely looks stamped is still nobody else's entry.
    """
    bindings = _bindings(auth)
    reserved = _binding_reservations(bindings)
    forgotten = _forgotten(auth)
    if not bindings:
        return bindings
    _checked_value({"projects": projects})
    for name, row in forgotten.items():
        path = row["record"]["path"]
        if path in reserved and reserved[path] != name or \
                name in bindings and path != bindings[name]["path"]:
            raise ValueError("forgotten archive conflicts with project binding")
    for name, rec in projects.items():
        path = rec.get("path")
        if path in reserved and reserved[path] != name or \
                name in bindings and path not in bindings[name]["locations"]:
            raise ValueError("projection conflicts with project binding")
    for name, entry in auth.get("projects", {}).items():
        path = entry.get("path")
        if path in reserved:
            owner = reserved[path]
            if name != owner and name not in _stamp_keys(owner, path):
                raise ValueError("authored entry conflicts with project binding")
        if name in bindings and path not in bindings[name]["locations"]:
            raise ValueError("authored name conflicts with project binding")
    # Imported aliases can collide without equal path strings. This check is
    # only a snapshot; unresolved paths still reach the strict consumer as
    # UNKNOWN, and external changes cannot justify relaxing that consumer.
    resolved, physical_owners = {}, {}
    for path, owner in reserved.items():
        try:
            physical = str(pathlib.Path(path).resolve(strict=True))
        except (OSError, RuntimeError):
            continue
        if physical in physical_owners and physical_owners[physical] != owner:
            raise ValueError("project bindings reserve aliased locations")
        resolved[path] = physical
        physical_owners[physical] = owner
    candidates = [(name, rec.get("path")) for name, rec in projects.items()]
    candidates += [(name, row["record"]["path"]) for name, row in forgotten.items()]
    candidates += [(name, row.get("path")) for name, row in auth.get("projects", {}).items()]
    for name, path in candidates:
        if not path:
            continue
        try:
            actual = str(pathlib.Path(path).resolve(strict=True))
        except (OSError, RuntimeError):
            continue
        for location, physical in resolved.items():
            owner = reserved[location]
            if actual == physical and name != owner \
                    and name not in _stamp_keys(owner, path):
                raise ValueError("project alias conflicts with reserved binding location")
    for name, binding in bindings.items():
        path = binding["path"]
        generation = binding["generation"]
        rec = projects.get(name, {})
        if rec.get("path") != path or rec.get("_binding_generation") != generation:
            rec = copy.deepcopy(binding["locations"][path])
        else:
            rec = dict(rec)
        _key, current_authored = authority_record(auth, name, path)
        if current_authored is None or current_authored.get("path") != path:
            raise ValueError("project binding has no matching authored path stamp")
        for key in AUTHORED_FIELDS:
            rec.pop(key, None)
        rec.update(name=name, path=path, _binding_generation=generation)
        projects[name] = rec
    return bindings


def repoint(name, source, target=None, apply=False, undo=False):
    """An explicit operator location decision, never repository inference."""
    with _write_lock() if apply else contextlib.nullcontext():
        return _repoint(name, source, target, apply, undo)


def _repoint(name, source, target, apply, undo):
    reg = load(strict=True)
    auth = _authored_load(strict=True)
    bindings = _bindings(auth)
    if name in _forgotten(auth):
        return None, "project is forgotten; restore before repoint or undo"
    rec = reg["projects"].get(name)
    if rec is None:
        return None, "unknown project '%s'" % name
    if not isinstance(source, str) or rec.get("path") != source:
        return None, "expected source does not match current project path"
    binding = copy.deepcopy(bindings.get(name))
    if undo:
        if target is not None or not binding:
            return None, "undo requires binding history and no target"
        target = binding["transitions"][-1]["from"]
    for path in (source, target):
        if not isinstance(path, str) or not os.path.isabs(path) or os.path.normpath(path) != path:
            return None, "source and target must be normalized absolute paths"
    if source == target:
        return None, "source and target are identical"
    if not undo:
        try:
            pathlib.Path(source).resolve(strict=True)
        except FileNotFoundError:
            pass
        except (OSError, RuntimeError) as exc:
            return None, "source path is UNKNOWN, not gone: %s" % exc
        else:
            return None, "source still exists; repoint requires a gone location"
    try:
        resolved = pathlib.Path(target).resolve(strict=True)
        if not resolved.is_dir():
            return None, "target is not a directory"
        resolved = str(resolved)
    except FileNotFoundError:
        if not undo:
            return None, "target directory does not exist"
        resolved = None
    except (OSError, RuntimeError) as exc:
        return None, "target path is UNKNOWN: %s" % exc
    candidates = [(other, row.get("path")) for other, row in reg["projects"].items()]
    candidates += [(other, row["record"]["path"]) for other, row in _forgotten(auth).items()]
    candidates += [(other, path) for other, b in bindings.items() for path in b["locations"]]
    for other, path in candidates:
        if other == name or not path:
            continue
        if path == target:
            return None, "target location belongs to another project"
        if resolved is not None:
            try:
                if str(pathlib.Path(path).resolve(strict=True)) == resolved:
                    return None, "target aliases another project location"
            except (OSError, RuntimeError):
                # This is not proof of nonidentity: project_state still keeps
                # skipped-positive UNKNOWN. No durable uniqueness is claimed.
                pass
    entries = auth.setdefault("projects", {})
    # THE WRITER ASKS THE RESOLVER THE READERS ASK. A writer that computed its
    # own lookup beside `authority_record` dropped an active declaration filed
    # under a spelling only the resolver could see (the historical `name@<stamp>`
    # a base-era repoint wrote for the current location).
    #
    # AND THIS PROJECT'S OWN UNACCOUNTABLE RECORD REFUSES THE MOVE — never the
    # name grammar. An entry spelled as this project's historical stamp for the
    # source or the target, recorded AT that location, that the file's own
    # history cannot account for would be CAPTURED by this very move: the binding
    # written below lists both locations, and the provenance table would then
    # prove the key as the project's own. So the move refuses first and names the
    # key; the owner can re-key it. An entry of the same spelling recorded at any
    # OTHER location is a lawful whole name there, is never captured (the
    # resolver reads the record's own location), and never vetoes — see
    # `_unaccountable_stamp`.
    for where in (source, target):
        unaccountable = _unaccountable_stamp(auth, name, where)
        if unaccountable is not None:
            return None, ("authored entry '%s' is recorded at %s and is spelled "
                          "like a historical path stamp of project %s for that "
                          "location, and nothing in the authored file records "
                          "that stamping; the move would capture it as the "
                          "project's own record, so repoint refused rather than "
                          "recomputing it" % (unaccountable, where, name))
    _key, destination = authority_record(auth, name, target)
    baseline = binding["locations"].get(target, {}) if binding else {}
    keep = {key: copy.deepcopy(rec[key]) for key in AUTHORED_FIELDS
            if key in rec and key not in NEVER_MIGRATED_FIELDS}
    # A NEVER-MIGRATED FIELD TRAVELS ONLY FROM THE AUTHORED LAYER. `rec` is the
    # MERGED record, so an inline `gate` block in registry.json is
    # indistinguishable here from one the owner wrote — and this writer then
    # published it into the authored file under the target's stamp, which is
    # exactly the authority-laundering channel `_load`'s migration and `save`
    # were closed against. A location change is not a promotion: the authored
    # value follows the project, the projection's copy stays a projection (this
    # transition never writes registry.json), and a projection-only block simply
    # does not survive a move — it was never authority to move.
    _key, departed = authority_record(auth, name, source)
    for field in NEVER_MIGRATED_FIELDS:
        if isinstance(departed, dict) and departed.get(field) is not None:
            keep[field] = copy.deepcopy(departed[field])
    if destination:
        for key in AUTHORED_FIELDS:
            if key in destination and destination.get(key) != baseline.get(key) and \
                    destination.get(key) != keep.get(key):
                return None, "destination authored fields conflict; repoint refused"
    if binding is None:
        binding = {"path": source, "locations": {}, "transitions": []}
    # Immutable departure records preserve all generations. The per-location
    # slot holds only the latest reversible snapshot for a return to that path.
    binding["locations"][source] = copy.deepcopy(rec)
    if target not in binding["locations"]:
        fresh = {"name": name, "path": target, "kind": "dir", "status": "active",
                 "sessions": {}, "last_seen": None}
        if "home" in rec:
            fresh["home"] = rec["home"]
        binding["locations"][target] = fresh
    next_rec = binding["locations"][target]
    for key in AUTHORED_FIELDS:
        next_rec.pop(key, None)
    next_rec.update(keep)
    generation = uuid.uuid4().hex
    binding["transitions"].append({"from": source, "to": target,
                                   "action": "undo" if undo else "repoint",
                                   "generation": generation,
                                   "at": pk.now_ts(), "record": copy.deepcopy(rec)})
    binding.update(path=target, generation=generation)
    auth.setdefault("project_bindings", {})[name] = binding
    # THE PROJECT'S UNOWNED KEYS MOVE WITH IT (`_unowned`), FROM THE DEPARTED
    # ENTRY ONLY. That entry is the project's current authority. The
    # destination of an undo is the project's own stale entry from before the
    # move, so taking its keys would bring back a key that was cleared at the
    # current location: for a helm that does not know `residency`, a cleared
    # may-leave-lan would come back open. A key whose only copy sits in a
    # stale destination entry is left for its owner to re-author.
    entries[_authority_slot(auth, name, target)] = dict(
        _unowned(departed), **keep, path=target)
    # Validate the exact candidate, including cross-entry import invariants,
    # before publication. No projection write can race ahead of this authority.
    _apply_bindings(copy.deepcopy(reg["projects"]), auth)
    if apply:
        pk.write_json(home.authored_path(), auth)
    return {"record": copy.deepcopy(next_rec), "from": source, "to": target}, None


def light(name, rec, auth=None):
    """THE ONE READ of a project's light -> {colour, authored, reason, by, ts}.

    `authored` is what makes this callable by a router rather than only a
    renderer: the same colour means a different thing depending on who said it.
    Green-because-a-scan-saw-activity is an observation that may be stale by the
    time it is read; green-because-the-owner-said-so is a standing permission.
    A caller that cannot tell them apart would report the owner's silence as
    the owner's consent, so the provenance travels WITH the value and is not
    recoverable afterwards from the colour alone.

    AUTHORSHIP COMES FROM THE AUTHORED LAYER, NEVER FROM `rec`. `rec` is the
    MERGED record, and the merge leaves a never-migrated field standing where
    it was found, so a `state` block sitting inline in registry.json reaches
    this function looking exactly like one the owner wrote. Reading it off
    `rec` would let the projection author a light; asking the resolver means a
    projection-only block is simply not a light, and the scan's answer stands.
    """
    rec = rec if isinstance(rec, dict) else {}
    auth = _authored_load(strict=True) if auth is None else auth
    _key, entry = authority_record(auth, name, rec.get("path"))
    said = entry.get("state") if isinstance(entry, dict) else None
    if isinstance(said, dict) and said.get("colour") in STATE_COLOURS:
        return {"colour": said["colour"], "authored": True,
                "reason": said.get("reason") or "", "by": said.get("by") or "",
                "ts": said.get("ts")}
    return {"colour": rec.get("status") or "dormant", "authored": False,
            "reason": "", "by": "", "ts": None}


def lights(reg):
    """{registry key -> light} for a whole merged registry, one authored read."""
    auth = _authored_load(strict=True)
    return {key: light(key, rec, auth)
            for key, rec in (reg.get("projects") or {}).items()
            if isinstance(rec, dict)}


# WHAT EACH AUTHORED COLOUR MEANS AT A DOOR WHERE WORK STARTS, as
# (new work, continuation of work already in flight).
#
# ONE VOCABULARY, AND IT IS `burnflags.BEHAVIOUR`'s. The four words already
# meant something in this tree before a project could carry one, and an owner
# who sets a project ORANGE because a credential is scarce means what orange
# means there: WORK, CONSTRAINED. A project table that graded the same word one
# notch stricter froze a project its owner had only asked to be careful with.
# So the sentences below are that table's, word for word (a test pins the
# parity), and the grading follows them: only RED refuses, and it refuses only
# NEW work, because its own sentence is "finish or park what is running".
#
# The colour is the INTENSITY. What KIND of work a project wants (maintenance
# only, one lane at a time) belongs in the reason, which every door prints.
LIGHT_ADMITS = {"green": (True, True), "yellow": (True, True),
                "orange": (True, True), "red": (False, True)}
LIGHT_SAYS = {
    "green": "open more lanes for work that is already built up; never speculative",
    "yellow": "normal work, no extra lanes",
    "orange": "critical path only, one delegate at a time",
    "red": "start nothing new here; finish or park what is running",
}


def admits(path, new_work=True):
    """(ok, refusal, note) — may work start (or continue) in `path`'s project?

    THE ONE DECISION every door that starts work asks, so `helm work claim` and
    `helm dispatch send` cannot grade one colour two ways.

    ONLY AN AUTHORED LIGHT BINDS. The scan's half of the light is an
    observation: `dormant` means nothing happened lately, which is no reason to
    refuse the first thing that does. A project nobody has decided about admits
    in silence, exactly as it did before the light existed.

    AND A READER THAT FAILS ADMITS, LOUDLY. A path no project claims, an
    unreadable registry, a malformed record: none of them is the owner saying
    no. A write door that stopped on an unread input would let a corrupt file
    freeze the fleet in the owner's name, so the trouble is returned as the
    note and the caller proceeds — the same law the credential rung keeps.

    THERE IS NO FORCE ARGUMENT, DELIBERATELY. The way past a light is to change
    the light, which records who did it, when and why, on the card the owner
    reads. A bypass flag would be a second authority with no record.
    """
    try:
        from .inject import _ledger
        reg = load(strict=True)
        key = _ledger.project_for_cwd(path, projects=reg.get("projects") or {})
        if not key:
            return True, None, None
        lit = light(key, reg["projects"].get(key))
    except Exception as exc:                # noqa: BLE001 — see the law above
        return True, None, ("project light UNKNOWN — the check itself failed "
                            "(%s: %s); admitted unverified"
                            % (exc.__class__.__name__, exc))
    if not lit["authored"] or lit["colour"] == "green":
        return True, None, None
    colour = lit["colour"]
    why = "%s is %s — %s. Set%s: %s" % (
        key, colour.upper(), LIGHT_SAYS[colour],
        " by " + lit["by"] if lit["by"] else "", lit["reason"] or "no reason recorded")
    if LIGHT_ADMITS[colour][0 if new_work else 1]:
        return True, None, why + _burst_clause(colour)
    return False, (why + ". A project's light outranks any credential flag: "
                   "capacity is never permission. If this work must happen, "
                   "change the light and say why (`helm projects state %s "
                   "<colour> --reason ...`), which puts your name and reason "
                   "on the card the owner reads." % key), None


def _burst_clause(colour):
    """The measured burst exemption, appended to an ORANGE note, or "".

    ORANGE ONLY, BECAUSE ORANGE IS THE ONLY COLOUR THAT SAYS A COUNT. The
    exemption lifts "one delegate at a time" and nothing else; yellow does not
    ration delegates, green does not, and red does not admit new work at all,
    so appending arithmetic about a credential under any of them would answer a
    question that colour never asked.

    AND IT NEVER CHANGES THE VERDICT. `admits` has already decided by the time
    this runs; this adds a SENTENCE to a note the door was going to print
    anyway. A project's light outranks any credential flag — capacity is never
    permission — and the whole point of the ORANGE episode is that the light
    was RIGHT about the pool and merely silent about the one credential the
    asking seat was already homed on.

    FAIL-QUIET, like every other input to this door: an exemption that cannot
    be measured is no exemption, and no note. The refusals are readable on
    demand at `helm burn burst`, where somebody asked.
    """
    if colour != "orange":
        return ""
    try:
        from . import burst
        text = burst.note()
    except Exception:                       # noqa: BLE001 — see the law above
        return ""
    return ("\n" + text) if text else ""


def state(name, colour, reason=None, by=None, apply=False):
    """Author the project light for `name` — the write half of `light()`."""
    with _write_lock() if apply else contextlib.nullcontext():
        return _state(name, colour, reason, by, apply)


def _state(name, colour, reason, by, apply):
    if colour != "clear" and colour in STATE_COLOURS and not (reason or "").strip():
        # A LIGHT WITHOUT A REASON IS UNREADABLE BY THE NEXT PERSON, and the
        # next person is usually the owner a week later asking why a project he
        # meant to ship is amber. The rule lives HERE, under every door, so the
        # web cannot author what the CLI would have refused.
        return None, ("a colour needs a reason (what decided it, and what "
                      "would change it back)")
    if colour not in STATE_COLOURS and colour != "clear":
        return None, "colour must be one of %s (or 'clear' to hand the light " \
                     "back to the scan)" % ", ".join(STATE_COLOURS)
    reg = load(strict=True)
    auth = _authored_load(strict=True)
    rec = (reg.get("projects") or {}).get(name)
    if rec is None:
        return None, "unknown project '%s'" % name
    path = rec.get("path")
    if not path or not os.path.isabs(path):
        return None, "project path is not an absolute identity"
    slot = _authority_slot(auth, name, path)
    entry = auth.setdefault("projects", {}).setdefault(slot, {})
    entry.setdefault("path", path)
    was = entry.get("state")
    if colour == "clear":
        # CLEARING IS NOT SETTING GREEN. A project whose light nobody has
        # touched is a different fact from one the owner has declared open, and
        # writing green here would erase that difference permanently: the next
        # reader sees a standing permission that no one ever gave. Removing the
        # authored value hands the light back to the scan, which is the only
        # honest rendering of "nobody has said".
        entry.pop("state", None)
    else:
        entry["state"] = {"colour": colour, "reason": reason or "",
                          "by": by or "", "ts": int(time.time())}
    if apply:
        pk.write_json(home.authored_path(), auth)
    return {"name": name, "path": path, "was": was,
            "state": entry.get("state")}, None


# DATA RESIDENCY: may a project's turn text leave the operator's LAN?
#
# ONE VALUE OPENS THE DOOR AND EVERYTHING ELSE SHUTS IT. `may-leave-lan`, as an
# AUTHORED value, is the only answer that lets text reach an outside service.
# No field, an unknown project, an unreadable registry, a malformed value, a
# projection-only block: each reads `lan-only`, because a lost outside call
# costs one turn of ranking and a leaked client turn cannot be recalled. The
# names of client projects appear nowhere in the code; a project is local
# until someone with the authority to say otherwise writes the field.
RESIDENCY_VALUES = ("lan-only", "may-leave-lan")
RESIDENCY_OPEN = "may-leave-lan"


def residency(name, path=None, auth=None):
    """{value, authored, reason, by, ts, why} for project `name`, FAIL CLOSED.

    `value` is `may-leave-lan` only when the authored layer says exactly that
    for this (name, path); every other state answers `lan-only` and `why`
    names which one it was. Authorship is read from the authored layer, never
    from a merged record, for `light`'s reason: a projection byte must not
    pass for a person's word."""
    def closed(why):
        return {"value": "lan-only", "authored": False, "reason": "", "by": "",
                "ts": None, "why": why}
    if not name:
        return closed("no project resolved for this turn")
    try:
        if path is None:
            rec = (load(strict=True).get("projects") or {}).get(name)
            if not isinstance(rec, dict):
                return closed("project %s is not in the registry" % name)
            path = rec.get("path")
        auth = _authored_load(strict=True) if auth is None else auth
        _key, entry = authority_record(auth, name, path)
    except Exception as exc:                # noqa: BLE001 — fail closed, named
        return closed("registry unreadable (%s: %s)"
                      % (exc.__class__.__name__, exc))
    said = entry.get("residency") if isinstance(entry, dict) else None
    if said is None:
        return closed("project %s has no residency field" % name)
    if not isinstance(said, dict) or said.get("value") not in RESIDENCY_VALUES:
        return closed("project %s has a malformed residency field" % name)
    return {"value": said["value"], "authored": True,
            "reason": said.get("reason") or "", "by": said.get("by") or "",
            "ts": said.get("ts"),
            "why": "authored %s" % said["value"]}


def set_residency(name, value, reason=None, by=None, apply=False):
    """Author project `name`'s residency — the write half of `residency()`."""
    with _write_lock() if apply else contextlib.nullcontext():
        return _set_residency(name, value, reason, by, apply)


def _set_residency(name, value, reason, by, apply):
    if value not in RESIDENCY_VALUES and value != "clear":
        return None, "residency must be one of %s (or 'clear')" % \
            ", ".join(RESIDENCY_VALUES)
    if value != "clear" and not (reason or "").strip():
        return None, ("a residency needs a reason (who decided the data may or "
                      "may not leave, and on what grounds)")
    reg = load(strict=True)
    auth = _authored_load(strict=True)
    rec = (reg.get("projects") or {}).get(name)
    if rec is None:
        return None, "unknown project '%s'" % name
    path = rec.get("path")
    if not path or not os.path.isabs(path):
        return None, "project path is not an absolute identity"
    slot = _authority_slot(auth, name, path)
    entry = auth.setdefault("projects", {}).setdefault(slot, {})
    entry.setdefault("path", path)
    was = entry.get("residency")
    if value == "clear":
        # CLEARING IS LAN-ONLY. With no field the reader fails closed, so
        # clearing a `may-leave-lan` shuts the door rather than opening it.
        entry.pop("residency", None)
    else:
        entry["residency"] = {"value": value, "reason": reason or "",
                              "by": by or "", "ts": int(time.time())}
    if apply:
        pk.write_json(home.authored_path(), auth)
    return {"name": name, "path": path, "was": was,
            "residency": entry.get("residency")}, None


def forget(name, apply=False):
    """Explicitly archive a gone registration; never remove project data."""
    with _write_lock() if apply else contextlib.nullcontext():
        return _forget(name, apply)


def _forget(name, apply):
    reg = load(strict=True)
    auth = _authored_load(strict=True)
    forgotten = _forgotten(auth)
    if name in forgotten:
        return forgotten[name], None
    rec = reg["projects"].get(name)
    if rec is None:
        return None, "unknown project '%s'" % name
    path = rec.get("path")
    if not path or not os.path.isabs(path):
        return None, "project path is not an absolute identity"
    try:
        pathlib.Path(path).resolve(strict=True)
    except FileNotFoundError:
        pass
    except (OSError, RuntimeError) as exc:
        return None, "project path is UNKNOWN, not gone: %s" % exc
    else:
        return None, "project path still exists; forget only accepts a gone entry"
    archived = dict(rec, name=name)
    # THE ARCHIVE IS AUTHORED CONTENT, so a NEVER-MIGRATED field enters it only
    # from the authored layer. `rec` is the MERGED record: archiving its inline
    # `gate` block copied the PROJECTION's executable command into the authored
    # file, from where `restore` published it as a declaration the owner never
    # wrote. The projection's own copy is untouched by this transition (only the
    # authored file is written), so nothing is lost by leaving it where it lives.
    # AND IT RECORDS THAT PROVENANCE IN THE ARCHIVE. Popping the field was
    # enough for archives THIS writer creates and said nothing about the ones
    # already on disk: a base-era archive holds the merged record's block with no
    # mark at all, and `restore` cannot tell it from an authored one by looking at
    # the bytes. So every archive this writer creates carries the list, empty
    # included — an archive with NO `authored_fields` key is exactly the historical
    # one, and `restore` reads that absence as "provenance unknown".
    # THE SAME RESOLVER THE READERS USE. A lookup of this writer's own made the
    # authored entry of a project that had ever been repointed invisible from
    # here, so `forget` archived an ACTIVE authored `gate` with no mark and
    # `restore` read the absence of that mark as "provenance unknown" — the
    # declaration went into the projection and stopped being the owner's.
    _key, entry = authority_record(auth, name, path)
    authored_marks = []
    for field in NEVER_MIGRATED_FIELDS:
        if isinstance(entry, dict) and entry.get(field) is not None:
            # EQUALITY, not merely "both present": the mark says the archived
            # VALUE is the authored one. A merged record that disagreed with the
            # authored layer is a state this writer will not certify, so it is
            # left unmarked and lands back in the projection on restore.
            if archived.get(field) == entry[field]:
                authored_marks.append(field)
        else:
            archived.pop(field, None)
    row = {"record": archived, "forgotten_at": pk.now_ts(),
           ARCHIVED_AUTHORED_KEY: authored_marks}
    if apply:
        # One atomic authored write is the membership transition. Projection
        # bytes and project homes remain intact; an interrupted sync cannot
        # erase the archive or restore membership by rediscovering the path.
        auth.setdefault("forgotten_projects", {})[name] = row
        pk.write_json(home.authored_path(), auth)
    return row, None


def restore(name, apply=False):
    """Restore the archived registration, even when its path is still gone."""
    with _write_lock() if apply else contextlib.nullcontext():
        return _restore(name, apply)


def _restore(name, apply):
    auth = _authored_load(strict=True)
    forgotten = _forgotten(auth)
    row = forgotten.get(name)
    if row is None:
        return None, "project '%s' has no forgotten archive" % name
    reg = _checked_registry(home.registry_path())
    rec = row["record"]
    active = load(strict=True)["projects"]
    for other, incumbent in active.items():
        if other == name and incumbent.get("path") != rec["path"]:
            return None, "project name now belongs to a different path; restore refused"
        if other != name and incumbent.get("path") == rec["path"]:
            return None, "project path now belongs to a different name; restore refused"
    entries = auth.setdefault("projects", {})
    _key, entry = authority_record(auth, name, rec["path"])
    # A NEVER-MIGRATED FIELD IS PROMOTED ONLY ON THE ARCHIVE'S OWN RECORDED
    # PROVENANCE. Copying every AUTHORED_FIELD off the archived record read a
    # base-era archive — which holds the MERGED record, projection block and all,
    # because `gate` was not an authored field when it was written — as executable
    # authority the owner never wrote. Those bytes are legitimate; what they do
    # not carry is authorship, and absence of a mark is not a yes.
    marked = _archived_authored(row)
    keep = {key: rec[key] for key in AUTHORED_FIELDS
            if key in rec and (key not in NEVER_MIGRATED_FIELDS or key in marked)}
    # UNMARKED BYTES GO BACK WHERE THE OLD RESTORE LEFT THEM: the projection. The
    # archive is the only copy if a re-scan has since rebuilt registry.json, so
    # dropping it would delete the block instead of keeping it non-authoritative,
    # and `declaration_provenance` would answer "you declared nothing" about a
    # block that is standing right there.
    unmarked = {key: rec[key] for key in NEVER_MIGRATED_FIELDS
                if rec.get(key) is not None and key not in marked}
    if entry and any(key in entry and entry[key] != value for key, value in keep.items()):
        return None, "archived authored fields conflict with current authority; restore refused"
    if apply:
        # Authored fields first, projection second, tombstone last. A legacy
        # inline record must not lose its only durable copy on a projection
        # rebuild; every interrupted prefix still leaves membership forgotten.
        if keep:
            entries[_authority_slot(auth, name, rec["path"])] = \
                dict(entry or {}, **keep, path=rec["path"])
        pk.write_json(home.authored_path(), auth)
        projected = {key: value for key, value in rec.items()
                     if key not in AUTHORED_FIELDS}
        # AND A PROJECTION VALUE STAYS IN THE PROJECTION, the same law `save`
        # keeps. `gate` is an AUTHORED_FIELD, so the comprehension strips it from
        # this rebuild while the archive (correctly) no longer carries a
        # projection-only copy — which between them DELETED an inline block that
        # was standing in registry.json before the forget. A restore is a
        # membership transition, not a layer migration: a value the authored
        # entry does not hold is written back exactly where it was found, so
        # `declaration_provenance` goes on answering "projection" for it.
        standing = reg["projects"].get(name)
        for field in NEVER_MIGRATED_FIELDS:
            if keep.get(field) is not None:
                continue
            if isinstance(standing, dict) and standing.get(field) is not None:
                # THE STANDING PROJECTION OUTRANKS THE ARCHIVE'S COPY. Live bytes
                # are what `declaration_provenance` is already answering with;
                # an archive is a snapshot of an older read of the same layer.
                projected[field] = standing[field]
            elif field in unmarked:
                projected[field] = unmarked[field]
        reg["projects"][name] = projected
        pk.write_json(home.registry_path(), reg)
        del auth["forgotten_projects"][name]
        pk.write_json(home.authored_path(), auth)
    return row, None


def load(strict=False):
    """The merged view, and A READ DOES NOT TAKE THE WRITE FLOCK.

    THE HAZARD THIS DOOR ANSWERS: an ordinary load may migrate mixed-era
    authored fields, and holding the EXCLUSIVE registry flock across every load
    to cover that write makes a READ of the registry a writer's act.
    `helm/web_core.py` loads the registry inside a request handler, so ~17
    concurrent /api/ledger requests from the owner's tabs hold the flock almost
    continuously and every helm verb and hook in every seat queues behind them —
    laptop load 33.8 with 35% of the CPU IDLE, which is a convoy and not
    saturation (task/2703, measured with py-spy: the holder was a load's own
    `_apply_bindings` realpath walk).

    THE MIGRATION IS A ONE-TIME DOOR, SO IT PAYS FOR THE LOCK ONLY ONCE. The
    merge below is computed from the bytes as they stand and costs the same
    either way; what needed serializing was never the read but the two
    `pk.write_json` calls that persist it. So the load runs UNLOCKED first and
    asks whether anything would be written. Nothing to write — the state of a
    registry that has already migrated, which is every load after the first —
    and the answer is returned with no lock taken and no sidecar created. Only
    a load that actually finds mixed-era fields takes the flock, and it then
    RE-READS under it: bytes read before a lock prove nothing about the bytes
    the writer is about to overwrite.

    STRICT AUTHORITY SNAPSHOTS DO NOT MUTATE AND MUST NOT CREATE EVEN A LOCK
    SIDECAR. That is the contract `dispatches.project_of_repo(snapshot=True)`
    reads this door by, and it is UNCONDITIONAL where the ordinary spelling's
    silence is merely usual: a strict load skips the migration whatever the
    bytes say, so no reading of the registry it does can ever put
    `<HELM_HOME>/_global/.state/` under a home no storage-safety check has
    cleared yet.

    A READER STILL NEVER SEES A HALF-MIGRATED VIEW, and not because of this
    lock. `_load` merges from BOTH layers on every call and the merge is
    idempotent — an inline field whose authored copy already exists is dropped,
    not doubled (`entry.setdefault` then `rec.update` from the entry) — while
    the persist order writes the authored layer FIRST. So the one window a
    lock-free reader can land in (authored written, projection not yet) reads
    the same merged record as before and after the write, which is exactly the
    exposure the three strict readers in this tree already had.
    """
    if strict:
        return _load(True)[0]
    reg, pending = _load(False, persist=False)
    if not pending:
        return reg
    with _write_lock():
        return _load(False)[0]


def _load(strict=False, persist=True):
    """`(merged view, migration pending)`. Migration: authored fields found
    inline in a mixed-era registry.json move to the authored file ONCE
    (idempotent; an already-authored value outranks a stale mixed copy; a
    same-name entry authored against a different path is never grafted onto —
    the collision class that once let a project inherit another's edges).

    `persist=False` computes the same merged view and PROMISES NOT TO WRITE,
    reporting instead whether a write is owed. That is what lets `load` decide
    about the flock from the bytes rather than in advance; the returned view is
    identical either way, because the merge happens in memory and the files
    only record it."""
    reg = _checked_registry(home.registry_path()) if strict else \
        pk.read_json(home.registry_path(), {"version": 1, "projects": {}})
    projects = reg.setdefault("projects", {})
    if not strict:
        _authored_load()  # retain the corruption backup, but never erase authority
    auth = _authored_load(strict=True)
    bindings = _apply_bindings(projects, auth)
    entries = auth.setdefault("projects", {})
    forgotten = _forgotten(auth)
    moved = False
    for name, rec in projects.items():
        # NEVER_MIGRATED_FIELDS IS EXCLUDED HERE AND NOWHERE ELSE. An inline
        # `gate` block stays inline: promoting it would make the projection a
        # writer of AUTHORITY, and the field is new enough that no legitimate
        # mixed-era copy can exist. See NEVER_MIGRATED_FIELDS.
        found = [k for k in AUTHORED_FIELDS
                 if k in rec and k not in NEVER_MIGRATED_FIELDS]
        if not found:
            continue
        # THE MIGRATION LANDS IN THE RECORD THAT OWNS THIS LOCATION, and mints
        # the plain name only when nothing owns it: a same-name entry authored
        # against ANOTHER path is never grafted onto (the collision class that
        # once let a project inherit another's edges) and never displaced.
        key, entry = authority_record(auth, name, rec.get("path"))
        if entry is None:
            if name in entries:
                continue
            entry = entries.setdefault(name, {"path": rec.get("path", "")})
        for k in found:
            entry.setdefault(k, rec.pop(k))
        moved = True
    pending = moved and not strict
    if pending and persist:
        # Authority snapshots are read-only; migration is an ordinary-load job.
        # AUTHORED FILE FIRST, ALWAYS: a reader that lands between the two
        # writes must find the value in the layer that owns it. The reverse
        # order would drop the field's only copy for the length of the window.
        pk.write_json(home.authored_path(), auth)
        pk.write_json(home.registry_path(), reg)
    for name, rec in projects.items():
        _key, entry = authority_record(auth, name, rec.get("path"))
        if entry:
            rec.update({k: entry[k] for k in AUTHORED_FIELDS if k in entry})
    reserved_locations = _binding_reservations(bindings)
    for name, entry in entries.items():  # external anchors outlive a projection wipe
        if name in projects or entry.get("path") in reserved_locations or not entry.get("external"):
            continue
        rec = {"name": name, "path": entry.get("path", ""), "kind": "external",
               "status": "external", "sessions": {}, "last_seen": None}
        rec.update({k: entry[k] for k in AUTHORED_FIELDS if k in entry})
        projects[name] = rec
    for name, row in forgotten.items():
        if projects.get(name, {}).get("path") == row["record"]["path"]:
            del projects[name]
    return reg, pending


@_serialized
def save(reg):
    """Split write: authored fields -> registry-authored.json (per project,
    path-stamped), everything else -> registry.json (pure projection). Entries
    for projects absent from reg are left alone — a partial save never deletes
    authored content.

    THE ONE DOOR THAT RE-SPELLS A HISTORICAL STAMP. `_rekey_legacy_stamps` runs
    here and nowhere else: readers decode such a key from the file's own records
    (`_proven_legacy_stamps`), so nothing depends on the migration, and this is
    already the writer of the unrebuildable layer. A read that migrated would be
    a read that writes authority."""
    _authored_load()  # back up corrupt bytes, but do not replace unknown authority
    auth = _authored_load(strict=True)
    _rekey_legacy_stamps(auth)
    bindings = _bindings(auth)
    for name, binding in bindings.items():
        rec = reg.get("projects", {}).get(name)
        if rec is not None and (rec.get("path") != binding["path"] or
                                rec.get("_binding_generation") != binding["generation"]):
            raise ValueError("stale project binding for '%s'; reload before saving" % name)
    _apply_bindings(copy.deepcopy(reg.get("projects", {})), auth)
    reg["generated_ts"] = pk.now_ts()
    entries = auth.setdefault("projects", {})
    proj = dict(reg)
    proj["projects"] = {}
    for name, rec in reg.get("projects", {}).items():
        keep = {k: rec[k] for k in AUTHORED_FIELDS
                if k in rec and k not in NEVER_MIGRATED_FIELDS}
        # AN EXECUTABLE DECLARATION IS CARRIED, NEVER ACCEPTED. `rec` is the
        # MERGED record, so a `gate` block that reached it from the projection
        # is indistinguishable here from one the owner authored — and writing it
        # into the authored file would make registry.json a writer of AUTHORITY
        # by the back door that `_load`'s migration was just closed against. The
        # value that survives a save is the one the authored entry ALREADY
        # holds: a projection copy cannot enter, and an authored declaration
        # cannot be lost by a round-trip that never meant to touch it.
        _key, prior = authority_record(auth, name, rec.get("path", ""))
        for field in NEVER_MIGRATED_FIELDS:
            if isinstance(prior, dict) and prior.get(field) is not None:
                keep[field] = prior[field]
        if keep:
            # A KEY THIS CODE DOES NOT OWN RIDES THROUGH (`_unowned`): the
            # merged record never holds one, so the entry being replaced is its
            # only copy.
            keep = dict(_unowned(prior), **keep)
            keep["path"] = rec.get("path", "")
            # THE ONE WRITE SLOT: the key this (name, location)'s record already
            # lives under, else the plain name while it is free, else the
            # computed stamp — so a same-name entry authored against a DIFFERENT
            # path is never clobbered (unrebuildable); the newcomer lands under
            # its own stamp, both survive, and `load()` resolves by location.
            #
            # AND THE WRITE IS WHERE A DUPLICATE SPELLING COLLAPSES. The keys
            # listed here all hold THIS record: the resolver was asked for this
            # (name, location) a few lines up, so a disagreement already raised
            # and this save never reached the write. What is left is one record
            # published under two spellings by two producer eras, and a save
            # that wrote the slot and left the other spelling standing would
            # republish the pair forever. Read from the layer as it stands
            # BEFORE the write, because `keep` is the merged record and may not
            # equal what those keys hold.
            slot = _authority_slot(auth, name, keep["path"])
            duplicates = _authority_keys(entries, name, keep["path"],
                                         _proven_legacy_stamps(auth))
            entries[slot] = keep
            for spelling in duplicates:
                if spelling != slot:
                    del entries[spelling]
        projected = {k: v for k, v in rec.items() if k not in AUTHORED_FIELDS}
        # AND A PROJECTION VALUE STAYS IN THE PROJECTION. `rec` is the MERGED
        # record and `gate` is an AUTHORED_FIELD, so the comprehension above
        # strips it from the projection write while the clause above refuses to
        # promote it — which between them DELETED a projection-only block on the
        # first ordinary save, and the gate then refused as though the project
        # had written nothing at all rather than saying "that is a projection".
        # A save is a projection REBUILD, not a layer migration: a value the
        # authored entry does not hold is one this save never owned, so it is
        # written back exactly where it was found. `declaration_provenance`
        # keeps answering "projection" for it, which is the whole point — the
        # block is REPORTED, still never executed, and still never promoted.
        for field in NEVER_MIGRATED_FIELDS:
            if rec.get(field) is not None and not (isinstance(prior, dict)
                                                   and prior.get(field) is not None):
                projected[field] = rec[field]
        proj["projects"][name] = projected
    _apply_bindings(copy.deepcopy(proj["projects"]), auth)
    pk.write_json(home.authored_path(), auth)
    pk.write_json(home.registry_path(), proj)


def _overlay_pointers(rec):
    """The read-only pointers a project record carries to its authoritative
    stores: repo (truth), per-project memory dir (claude projection), cv scope
    (recall). helm references these — it never copies them."""
    mem = None
    for cwd in [rec["path"]] + rec.get("cwds", []):
        d = home.claude_memory_dir_for(cwd)
        if os.path.isdir(d):
            mem = d
            break
    rec["memory_dir"] = mem
    # cv prefix-matches on recorded cwds; sibling-dir worktrees don't share the
    # canonical root's prefix, so the scope carries every observed cwd too.
    rec["cv_scope"] = {"cwd_prefix": rec["path"],
                       "cwd_prefixes": sorted({rec["path"], *rec.get("cwds", [])})}
    rec["home"] = home.project_dir(rec["name"])
    return rec


@_serialized
def sync(observations=None):
    """Run the auto-map, merge into the persistent registry, scaffold homes.
    Returns (registry, report) where report = {"new": [...], "updated": [...]}."""
    reg = load(strict=True)
    known = reg.setdefault("projects", {})
    auth = _authored_load(strict=True)
    forgotten = _forgotten(auth)
    bindings = _bindings(auth)
    reserved = {name: row["record"]["path"] for name, row in forgotten.items()}
    forgotten_paths = set(reserved.values())
    reserved.update({name: binding["path"] for name, binding in bindings.items()})
    forgotten_paths.update(path for binding in bindings.values()
                           for path in binding["locations"] if path != binding["path"])
    mapped = automap.build_map(observations=observations)
    report = {"new": [], "updated": []}

    # PATH is identity. Match on path first (a display-name rename must not
    # duplicate a project). Only merge into an existing record when its path
    # matches; a same-BASENAME newcomer at a different path must get a fresh
    # non-colliding name, never inherit the incumbent's authored edges/notes.
    by_path = {p["path"]: n for n, p in known.items()}
    for name, rec in sorted(mapped.items()):
        if rec["path"] in forgotten_paths:
            continue
        _overlay_pointers(rec)
        cur_name = by_path.get(rec["path"])
        if cur_name is None and (name in reserved or
                name in known and known[name].get("path") != rec["path"]):
            # Forgotten names remain reserved too: a newcomer must not inherit
            # their authored fields, nor take the name needed for restoration.
            taken = dict(reserved, **{n: p.get("path", "") for n, p in known.items()})
            cur_name = automap._name_for(rec["path"], taken, os.path.expanduser("~"))
            if cur_name in known:  # last-resort disambiguation
                cur_name = cur_name + "-" + rec["path"].strip("/").replace("/", "-")[-24:]
        cur = known.get(cur_name) if cur_name else None
        if cur is None:
            cur_name = cur_name or name
            rec["name"] = cur_name
            rec["first_seen"] = rec["last_seen"] or time.time()
            known[cur_name] = rec
            by_path[rec["path"]] = cur_name
            report["new"].append(cur_name)
        else:
            preserved = {k: cur[k] for k in AUTHORED_FIELDS if k in cur}
            preserved["first_seen"] = cur.get("first_seen", rec["last_seen"])
            rec["name"] = cur_name
            cur.update(rec)
            cur.update(preserved)
            report["updated"].append(cur_name)

    # second discovery tier: on-disk git repos with no observed agent activity
    # register as SHELF nodes — lineage/archive-report substrate, no home
    # scaffold, hidden from the default project list. An observed project is
    # never demoted to shelf (observation outranks presence).
    by_path = {p["path"]: n for n, p in known.items()}
    for path, name in automap.scan_repos().items():
        if path in by_path or path in forgotten_paths:
            continue
        taken = dict(reserved, **{n: p["path"] for n, p in known.items()})
        shelf_name = name if name not in taken else automap._name_for(path, taken, os.path.expanduser("~"))
        known[shelf_name] = {
            "name": shelf_name, "path": path, "kind": "git", "status": "shelf",
            "sessions": {}, "first_seen": time.time(), "last_seen": None,
        }
        by_path[path] = shelf_name
        report.setdefault("shelved", []).append(shelf_name)

    home.scaffold_global()
    for name, rec in known.items():
        if rec.get("external") or rec.get("retired") or rec.get("status") == "shelf":
            continue
        _adopt_or_scaffold(name)
    save(reg)
    reg = load()  # re-compose: a re-discovered project picks its authored fields back up
    _write_project_registries(reg)
    return reg, report


def _adopt_or_scaffold(name):
    p = home.project_dir(name)
    adopted = adopted_homes().get(name)
    if adopted and os.path.isdir(adopted):
        if os.path.islink(p):
            return
        if not os.path.exists(p):
            os.symlink(adopted, p)
            return
        # a real dir already exists where the adoption symlink belongs — keep it
        # (never destroy user data); the doctor surfaces the conflict.
        return
    home.scaffold_project(name)


def _write_project_registries(reg):
    """Each project home mirrors its own record — the chain travels as one unit."""
    for name, rec in reg["projects"].items():
        p = home.project_dir(name)
        if not os.path.isdir(p) or os.path.islink(p):
            continue
        pk.write_json(os.path.join(p, "registry.json"), rec)


def get(name):
    return load()["projects"].get(name)


@_serialized
def add_external(name, path, note=""):
    """Register a read-only external node (vendored/reference/upstream clone).
    External nodes anchor lineage and are never cleanup candidates."""
    reg = load()
    if name not in reg["projects"]:
        reg["projects"][name] = {
            "name": name, "path": path, "kind": "external", "status": "external",
            "external": True, "notes": note, "sessions": {}, "edges": [],
            "first_seen": time.time(), "last_seen": None,
        }
        save(reg)
    return reg["projects"][name]


@_serialized
def add_edge(src, rel, dst, note="", confirmed=True):
    """Lineage edge on the source project: rel in
    forked-from | composes | supersedes | launched-as (+ free-form)."""
    reg = load()
    p = reg["projects"].get(src)
    if p is None:
        return None, "unknown project '%s' (helm sync first, or add-external)" % src
    edges = p.setdefault("edges", [])
    for e in edges:
        if e["rel"] == rel and e["to"] == dst:
            e.update({"note": note or e.get("note", ""), "confirmed": confirmed})
            save(reg)
            return e, None
    e = {"rel": rel, "to": dst, "note": note, "confirmed": confirmed, "ts": pk.now_ts()}
    edges.append(e)
    save(reg)
    return e, None


# ---------------------------------------------------------------------------
# projection registry — constitution laws 2+3 as an executable manifest
# ---------------------------------------------------------------------------

# Every on-disk store helm reads or writes, classified:
#   authored     source of truth — unrebuildable, never regenerated, ships
#   projection   regenerable view — source + rebuild REQUIRED; a projection
#                that cannot name its rebuild cannot be safely wiped or
#                gitignored (the ship-pull derived/authored split rests here)
#   events       append-only receipts — lossy-by-design, never truth
#   state        host-local runtime — safe to lose, readers fail open
#   backup       recovery copies of authored/config content
# doctor walks the manifest READ-ONLY (it reports; rebuilds stay with their
# legs: sync/sessions/inject/drift); rebuild-and-converge is pinned by the
# test suite, not run in doctor.
KINDS = ("authored", "projection", "events", "state", "backup")


def cache_root():
    """~/.cache/helm — the second classified root. HELM_CACHE_DIR overrides
    (inject._cache_file's law), which is how tests point it at a tmp dir."""
    return home.env("CACHE_DIR") or os.path.join(os.path.expanduser("~"), ".cache", "helm")


def projections():
    """The executable manifest: one row per declared on-disk store —
    {name, kind, root, globs, source, sources, rebuild, fresh_days, genesis,
    mutable: False}. globs are root-relative, fnmatch semantics (* crosses
    /); `sources` are the authoritative paths a projection re-derives from
    (at least one must exist while the projection does); `fresh_days` is the
    declared staleness horizon (None = self-invalidating: sig-keyed or TTL).
    `genesis` optionally declares the exact empty JSON a producer writes before
    any source exists. Any file under either root that NO row names is an
    unclassified squatter — the ~/.remember rot class, flagged by doctor."""
    from . import store
    u = os.path.expanduser("~")
    scans = tuple(automap._scan_roots())
    harness_roots = (os.path.join(u, ".claude", "projects"),
                     os.path.join(u, ".codex", "sessions"),
                     os.path.join(u, ".local", "share", "opencode"),
                     os.path.join(u, ".pi", "agent", "sessions")) + scans
    store_roots = (store.adopted_dir(), home.global_dir())
    master = home.registry_path()
    g_cats = sorted(set(home.GLOBAL_CATEGORIES) | set(store.TYPE_SUBDIR.values()))
    # + reflexes: mentor-taught project reflexes are authored (provenanced)
    p_cats = sorted(set(home.PROJECT_CATEGORIES) | set(store.TYPE_SUBDIR.values())
                    | {"reflexes"})

    def row(name, kind, root, globs, source="", sources=(), rebuild=None,
            fresh_days=None, genesis=None):
        return {"name": name, "kind": kind, "root": root, "globs": tuple(globs),
                "source": source, "sources": tuple(sources), "rebuild": rebuild,
                "fresh_days": fresh_days, "genesis": genesis, "mutable": False}

    return (
        row("registry", "projection", "home", ("_global/registry.json",),
            source="harness session stores + disk repo scan",
            sources=harness_roots, rebuild="helm sync", fresh_days=30,
            genesis={"json": {"version": 1, "projects": {}},
                     "volatile_strings": ("generated_ts",)}),
        row("project-registry", "projection", "home", ("*/registry.json",),
            source="_global/registry.json (+ authored layer), re-mirrored per project",
            sources=(master,), rebuild="helm sync", fresh_days=30),
        row("registry-authored", "authored", "home", ("_global/registry-authored.json",)),
        row("registry-write-lock", "state", "home", ("_global/.state/registry.lock",)),
        row("store-global", "authored", "home",
            tuple("_global/%s/*" % c for c in g_cats)),
        row("store-project", "authored", "home",
            tuple("*/%s/*" % c for c in p_cats)),
        row("host-blocks", "projection", "home", ("_global/hosts/*",),
            source="this host's registry.json observations",
            sources=(master,), rebuild="helm ship --apply"),
        row("gitignore", "authored", "home", (".gitignore",)),
        # The fleet task ledger classifies itself from birth. Every other
        # durable _global ledger (owner-asks, owner-decisions, dispatches) is
        # currently an unclassified squatter, which is how 2,761 files came to
        # sit in a root that has a manifest — a new store that does not declare
        # itself inherits exactly that rot.
        row("task-ledger", "events", "home", ("_global/tasks.jsonl*",)),
        # One file per dispatch BRIEF, named for the brief's own blake2b-128
        # digest, written before the ledger row that references it. AUTHORED,
        # not a projection: the text is the sender's, nothing derives it, and
        # deleting a file loses the only whole copy — the row keeps a bounded
        # one and every reader says so out loud when the file is gone.
        # Declared at birth for the reason the comment above gives: the
        # dispatch ledger's own neighbours are unclassified squatters, and a
        # new store that does not declare itself inherits exactly that rot.
        row("dispatch-briefs", "authored", "home",
            ("_global/dispatch-briefs/*",)),
        # The write-behind cache for the land projection: one file per cache
        # key, holding the last computed body plus the INPUT WITNESS it was
        # computed under, so a restart has a past to reason about instead of
        # re-opening a 22-28s cold build against a 12s fetch deadline. A pure
        # PROJECTION — every file is derived, every one is disposable, and the
        # loader already refuses anything it cannot date, so deleting the
        # directory costs one cold build and nothing else. Declared here at
        # birth for the reason stated three lines up: a new store that does not
        # declare itself is an unclassified squatter, and the survey correctly
        # reported it as one.
        row("web-cache", "projection", "home", ("_global/web-cache/*.json",),
            source="the land projection's last computed body per cache key",
            sources=(master,), rebuild="delete; the next read rebuilds it",
            fresh_days=1),
        # THE STOP GUARD'S FACTS: what every seat's Stop hook reads instead of
        # folding the dispatch ledger and asking git per lease, written behind
        # itself by the `helm web` resident (helm/stopfacts_resident.py) under
        # its single-writer lock. A pure PROJECTION: deleting it costs every
        # stop its exemptions until the resident's next refresh, a second or
        # so later. The tmp glob is `pk.atomic_write`'s per-writer temporary.
        row("stop-facts", "projection", "home",
            ("_global/web-cache/stop-facts.json",
             "_global/web-cache/stop-facts.lock",
             "_global/web-cache/stop-facts.json.*.tmp"),
            source="the dispatch ledger, the claims file and each held "
                   "lane's git state, computed with the stop guard's own "
                   "functions (helm.stopfacts_resident)",
            sources=(os.path.join(home.global_dir(), "dispatches.jsonl"),),
            rebuild="delete; the resident writes it again within its poll",
            fresh_days=1),
        # THE LEDGER FOLD CHECKPOINTS (task/2770): the whole fold of one
        # append-only ledger at a byte offset, one file per code version, keyed
        # on the ledger prefix, the code, the gate-epoch marker and every git
        # answer the fold read (helm/foldckpt.py). A pure PROJECTION: any read
        # of the ledger rebuilds it, and deleting it costs exactly one full
        # replay. The tmp glob is `pk.atomic_write`'s per-writer temporary,
        # which only a crash between write and rename can leave behind.
        #
        # ONE DIRECTORY PER LEDGER, so the glob carries the ledger-key level:
        # every durable ledger shares `_global`, and a store addressed by code
        # alone gave two ledgers one file that each read overwrote as the
        # other's. The FLAT globs are that retired address — declared so the
        # store survey does not read leftovers as a squatter, and collected by
        # the same `rebuild` as the rest.
        row("ledger-fold", "projection", "home",
            ("_global/.state/ledger-fold/*/*.ckpt",
             "_global/.state/ledger-fold/*/*.tmp",
             "_global/.state/dispatch-fold/*.ckpt",
             "_global/.state/dispatch-fold/*.tmp"),
            source="an append-only ledger, folded to a byte offset and keyed "
                   "on what the fold read (helm.foldckpt)",
            sources=(os.path.join(home.global_dir(), "dispatches.jsonl"),),
            rebuild="delete; the next read of that ledger folds the whole "
                    "ledger and writes it again"),
        # THE CARRIAGE REPLAY'S DERIVATIONS (task/2813): the tri-state answer
        # for each (repository, trunk object, base, tip) the off-frontier
        # census's replay witness was asked about, one file per code
        # generation, served only after every object and ref expression the
        # witness read is re-asked in one batch-check per repository and the
        # merge machinery is fingerprinted again (helm/carriageckpt.py). A pure
        # PROJECTION: deleting it costs one census's worth of merge-tree
        # spawns and nothing else. The tmp glob is `pk.atomic_write`'s, which
        # only a crash between write and rename can leave behind.
        row("carriage-replay", "projection", "home",
            ("_global/.state/carriage-replay/*.json",
             "_global/.state/carriage-replay/*.tmp"),
            source="the carriage replay witness, keyed on the three objects "
                   "it was asked about and on what it read (helm.carriageckpt)",
            sources=(os.path.join(home.global_dir(), "dispatches.jsonl"),),
            rebuild="delete; the next off-frontier census derives the "
                    "witness live and writes it again"),
        # THE LANDING-WINDOW LAUNCH RECORDS. Declared at birth, for the reason
        # the ledger-fold row two rows up gives: an undeclared store is a
        # squatter. What it holds is the half a NODE cannot answer — which
        # project, trunk head and room a dispatched whole-suite run covers —
        # and nothing here is believed about liveness, which is re-measured
        # against the node on every read. The glob names pk.atomic_write's
        # `*.tmp` and the read-decide-write lock, because a crash between
        # write and rename leaves one of each.
        row("gate-window", "state", "home",
            ("_global/.state/gate-window/*.json",
             "_global/.state/gate-window/*.tmp",
             "_global/.state/gate-window/*.lock"),
            source="helm gate window launch, written before it dispatches",
            rebuild="delete; the next launch records itself again, and every "
                    "read re-measures liveness against the node"),
        row("events-journal", "events", "home", ("_global/.state/events.jsonl*",)),
        # THE PRE-READ ESTATE, declared at birth for the reason two rows up:
        # an undeclared store is a squatter. The config is AUTHORED (an
        # operator writes the endpoints, the models and the path to the
        # judge's key file; helm never generates it and a wipe is not
        # recoverable by a rebuild). The readings are a pure PROJECTION of a
        # tip — delete one and `helm preread` builds it again from the same
        # ref, at the price of the council's wall time. The pre-read ledger is
        # EVENTS: one line per run, which is how review quality per model and
        # per rung becomes measurable later.
        row("preread-config", "authored", "home", ("_global/preread.json",)),
        row("prereads", "projection", "home", ("_global/prereads/*.md",),
            source="a council of reader models over one review row's diff",
            sources=(master,), rebuild="helm preread <dispatch-id>"),
        row("preread-ledger", "events", "home",
            ("_global/prereads/ledger.jsonl*",)),
        # THE qwen27 FINDINGS PASS'S WORKSHOP (helm/findingspass.py): the
        # one flock every queued pass waits on, the detached workers' log and
        # its one rotated generation, and the script's `--out` file while a
        # run is in flight (read, stored by reference, then removed). The
        # RESULT is not here — it is a `findings-note` on the dispatch ledger
        # and a content-addressed file under dispatch-briefs.
        row("findings-pass", "state", "home",
            ("_global/.state/findings-pass/*.lock",
             "_global/.state/findings-pass/*.log",
             "_global/.state/findings-pass/*.log.1",
             "_global/.state/findings-pass/run-*.md"),
            source="helm/findingspass.py, a detached worker per review row",
            rebuild="delete while no pass runs; the next pass recreates the "
                    "lock and the log"),
        row("chat-event-receipts", "events", "home",
            ("_global/.state/chat-event-receipts/*",)),
        row("inject-ledger", "events", "home", ("_global/.state/inject-ledger.jsonl*",)),
        # THE LONG-TAIL RE-RANK (helm/relevance.py). The settings file and the
        # operator's endpoints file are what a person wrote. The per-session
        # score cache (one small JSON per session, its lock, the detached
        # workers' log and a job file that lives only until its worker has
        # read it) is state. The ledger is events: one `submit` row per turn
        # from the hook and one `score` row from its worker. The counters and
        # the remeasure receipt are state the report and the gate read.
        row("relevance-config", "authored", "home", ("_global/relevance.json",)),
        row("endpoints", "authored", "home", ("_global/endpoints.json",)),
        row("relevance-cache", "state", "home", ("_global/.state/relevance/*",)),
        row("relevance-ledger", "events", "home",
            ("_global/.state/relevance-ledger.jsonl*",)),
        row("relevance-counters", "state", "home",
            ("_global/.state/relevance-counters.json*",)),
        row("relevance-remeasure", "state", "home",
            ("_global/.state/relevance-remeasure.json",)),
        # one line per guard refusal; `helm friction` counts it and the reflex
        # layer reads a seat's worst guard from it. The glob covers the single
        # rotated generation and the append lock beside it.
        row("friction-ledger", "events", "home", ("_global/.state/friction.jsonl*",)),
        row("attest-queue", "state", "home", ("_global/.state/attest-queue.jsonl",)),
        row("inject-seen", "state", "home", ("_global/.state/inject-seen/*",)),
        row("coinages", "state", "home", ("_global/.state/coinages.json",)),
        # task/2978: the per-turn word-form counts behind "is this word common
        # in prompts" (helm.promptcensus). Self-bounding (WINDOW halves the
        # counts, CAP sheds the rarest forms); the second glob is
        # pk.atomic_write's per-writer temp, as for board-read below.
        row("prompt-census", "state", "home",
            ("_global/.state/prompt-census.json",
             "_global/.state/prompt-census.json.*.tmp")),
        # One record per host: the outcome of the last land-pipeline read the
        # owner's board asked for, and the identity of the `helm web` that took
        # it. STATE, not a projection: nothing re-derives it — it is the only
        # trace that a board read failed, and a lost file means the failure was
        # never observed rather than that it can be recomputed. The second glob
        # is `pk.atomic_write`'s per-writer temp, which a crash between write
        # and rename leaves behind, and an undeclared leftover is a squatter.
        row("board-read", "state", "home",
            ("_global/.state/board-read.json",
             "_global/.state/board-read.json.*.tmp")),
        row("storage-matrix", "state", "home",
            ("_global/.state/storage-matrix.json*",)),
        # `helm upstream-watch`: the last Claude Code release read, each tier-2
        # vendor page's hash, a release waiting for its CHANGELOG entry, and
        # any task a failed pass filed but never announced. STATE, not a
        # projection: nothing re-derives which release was already read, and
        # a lost file re-reads the newest pair. The directory holds each
        # vendor page's last text, which the next diff reads; the lock
        # serializes passes, and `*.tmp` is pk.atomic_write's per-writer temp.
        row("upstream-watch", "state", "home",
            ("_global/.state/upstream-watch.json",
             "_global/.state/upstream-watch.json.*.tmp",
             "_global/.state/upstream-watch.json.lock",
             "_global/.state/upstream-watch/*")),
        row("autocompact", "state", "home",
            ("_global/.state/autocompact.json*",)),
        # THE RECIPE NAMES THE VERB THAT ACTUALLY WRITES, AND SAYS WHAT ELSE IT
        # DOES (task/2480 R7). Bare `helm proxywatch` is a STATUS READ: it
        # renders health and returns before the budget producer, so following
        # it as a rebuild left the missing or stale snapshot untouched and told
        # the operator nothing. `--post` is the timed pass, and it is the only
        # path that reaches the writer — so the disclosure rides the recipe: it
        # probes the vendor once per pooled account and it MAY POST to the helm
        # room (proxy-health changes, and FAMILY-BUDGET-LOW on a crossing).
        row("codex-pool-budget", "projection", "home",
            ("_global/.state/codex-pool-budget.json",),
            source="the codex seat proxy's pooled creds, probed against the "
                   "vendor usage endpoint (percentages only, never a token)",
            sources=(os.path.join(home.global_dir(), "seats", "codex", "auth"),),
            rebuild="helm proxywatch --post  # NOT the bare verb: only the "
                    "posting pass reaches the writer. SIDE EFFECTS: one vendor "
                    "usage probe per pooled account, and a post to the helm "
                    "room if proxy health changed or the pool crossed the "
                    "weekly ceiling"),
        # THE BURN-FLAG FOLD RIDES THE SAME PASS AND THE SAME DISCLOSURE. It
        # spends no probe of its own: it folds the readings that pass already
        # took — the pooled budget above, the upstream family states, and the
        # native usage log the creds cycle writes — so the rebuild recipe is
        # the recipe for its inputs, and the only new side effect is the room
        # post on a family CROSSING, which replaced the pool's own.
        row("burn-flags", "projection", "home",
            ("_global/.state/burn-flags.json",),
            source="the pooled codex budget, the watchdog's upstream family "
                   "states, the native usage history and the owner's "
                   "declarations, folded into one colour per family "
                   "(counts and percentages only, never an identity)",
            sources=(os.path.join(home.global_dir(), ".state"),),
            rebuild="helm proxywatch --post  # NOT the bare verb: only the "
                    "posting pass reaches the writer. SIDE EFFECTS: the same "
                    "probes the pooled budget row names, and a post to the "
                    "helm room when a family crosses a colour"),
        # THE CODEX PACE (task/2984). The history is EVENTS: one line per
        # posting pass, which nothing can re-derive once the pass is gone, and
        # the rate is a slope over those lines. The runway snapshot is a
        # PROJECTION of the history and the proxy-usage ledger. The walls
        # ledger is STATE: which walls already spoke, and on which channel —
        # losing it re-announces a standing wall once, it re-derives nothing.
        # The `.lock` siblings and `pk.atomic_write` temps ride each glob.
        row("codex-budget-history", "events", "home",
            ("_global/.state/codex-budget-history.jsonl*",)),
        row("codex-runway", "projection", "home",
            ("_global/.state/codex-runway.json",
             "_global/.state/codex-runway.json.*.tmp"),
            source="the codex budget history and the proxy-usage ledger, "
                   "read into percent per hour per account and the fleet's "
                   "runway against the next Pro reset (masked addresses, "
                   "never a token)",
            sources=(os.path.join(home.global_dir(), ".state"),),
            rebuild="helm proxywatch --post  # NOT the bare verb: only the "
                    "posting pass reaches the writer. SIDE EFFECTS: the "
                    "probes the pooled budget row names, and one room line "
                    "plus one phone push for a codex account whose week is "
                    "newly spent"),
        row("codex-walls", "state", "home",
            ("_global/.state/codex-walls.json*",)),
        # THE DECLARATIONS ARE AUTHORED, NOT DERIVED: the owner types a colour
        # and an expiry, nothing recomputes them, and losing the file loses
        # what he said rather than a cache.
        row("burn-declarations", "authored", "home",
            ("_global/.state/burn-declarations.json",),
            source="the owner, through `helm burn declare`"),
        # THE BOARD'S REPOSITORY BADGES: whether each GitHub repository a
        # project pushes to is public or private, as gh answered, kept a day.
        # A PROJECTION with no rebuild recipe of its own: every entry is asked
        # again once past its age, and a deleted file is re-asked on the next
        # board read. The second glob is `pk.atomic_write`'s per-writer temp.
        row("repo-visibility", "projection", "home",
            ("_global/.state/repo-visibility.json",
             "_global/.state/repo-visibility.json.*.tmp"),
            source="gh repo view <owner/name> --json visibility, for the "
                   "remotes of each project the Work page shows",
            sources=(os.path.join(home.global_dir(), ".state"),),
            rebuild="open the Work page (helm web): it asks what is missing"),
        row("drift-snapshot", "projection", "home",
            ("_global/.state/drift-snapshot*.json",),
            source="the typed store's prior confidences",
            sources=store_roots, rebuild="helm drift"),
        row("now", "state", "home", ("_global/now.md",)),
        row("chat-node", "state", "home", ("_global/.state/chat-node.json",)),
        row("cells", "state", "home", ("_global/.state/cells.json",)),
        row("reflex-state", "state", "home", ("_global/.state/reflex-state/*",)),
        row("beacons", "state", "home", ("_global/.state/beacons/*",)),
        row("seats", "state", "home", ("_global/seats/*",)),
        row("catalog-cache", "projection", "cache", ("catalog-cache.json",),
            source="local claude/codex transcripts (cv ls, or the scanner)",
            sources=harness_roots, rebuild="helm sessions"),
        row("syn-cache", "projection", "cache", ("syn-cache.json",),
            source="transcript first-bytes (synthetic-session peek)",
            sources=harness_roots, rebuild="helm sessions",
            genesis={"json": {}, "volatile_strings": ()}),
        row("codex-cwd-cache", "projection", "cache", ("codex-cwd-cache.json",),
            source="codex rollout session_meta cwd (stat-signature keyed sniff)",
            sources=harness_roots, rebuild="helm sync",
            genesis={"json": {}, "volatile_strings": ()}),
        row("gitfacts", "projection", "cache", ("gitfacts/*/*",),
            source="git answers about immutable object ids (helm.gitfacts)",
            sources=(master,),
            rebuild="delete; every entry re-derives on the next read"),
        row("store-cache", "projection", "cache", ("store-cache-*.json",),
            source="the typed store roots (stat-signature keyed)",
            sources=store_roots, rebuild="helm inject"),
        row("cwd-overrides", "authored", "cache", ("cwd-overrides.json",)),
        # the relevance scorer service's row vectors, one file per embedder,
        # keyed by the hash of each row's text (helm/relevanced.py)
        row("relevance-rows", "projection", "cache", ("relevance-rows/*",),
            source="the embedding server over each store line's text",
            sources=store_roots,
            rebuild="delete; the service re-embeds a row the first time it "
                    "scores it (or `helm relevance warm`)"),
        row("mints", "events", "cache", ("mints.jsonl", "mints.jsonl.*")),
        row("keepalive-log", "events", "cache", ("keepalive-log.jsonl", "keepalive-log.jsonl.*")),
        row("usage-history", "events", "cache", ("native-usage-history.jsonl", "native-usage-history.jsonl.*")),
        row("backups", "backup", "cache",
            ("config-backups/*", "settings-backups/*", "skills-backups/*",
             "skills-trash/*")),
        row("scratch", "state", "cache", ("*.lock", "*.tmp")),
    )


def projection_is_genesis(row):
    """Whether one surveyed projection is its exact declared empty JSON."""
    spec = row.get("genesis")
    files = row.get("files") or ()
    if not spec or len(files) != 1:
        return False
    root = {"home": home.helm_home(), "cache": cache_root()}.get(row.get("root"))
    if not root:
        return False
    try:
        with pk.open_regular(os.path.join(root, files[0]), encoding="utf-8") as f:
            body = json.load(f)
    except (OSError, ValueError):
        return False
    if not isinstance(body, dict):
        return False
    body = dict(body)
    for key in spec.get("volatile_strings", ()):
        if not isinstance(body.get(key), str):
            return False
        del body[key]
    return body == spec.get("json")


def _walk_root(root_dir):
    """Every regular file under root_dir as /-relative paths. .git pruned and
    symlinks never crossed or listed — a symlink is a pointer into someone
    else's estate (the adoption law), not a store to classify."""
    out = []
    for dirpath, dirnames, filenames in os.walk(root_dir):
        dirnames[:] = [d for d in dirnames if d != ".git"
                       and not os.path.islink(os.path.join(dirpath, d))]
        for n in filenames:
            p = os.path.join(dirpath, n)
            if not os.path.islink(p):
                out.append(os.path.relpath(p, root_dir))
    return sorted(out)


def projection_survey():
    """The manifest joined to disk: (rows, squatters) where each row copies
    its manifest row + `files` (matched, root-relative) and squatters is
    {"home": [...], "cache": [...]} — every file no row names. Overlapping
    rows both collect a file (classification, not ownership). Read-only;
    an absent root reads as empty."""
    rows = [dict(r, files=[]) for r in projections()]
    squat = {}
    for root_key, root_dir in (("home", home.helm_home()), ("cache", cache_root())):
        files = _walk_root(root_dir) if os.path.isdir(root_dir) else []
        mine = [r for r in rows if r["root"] == root_key]
        squat[root_key] = []
        for f in files:
            hit = False
            for r in mine:
                if any(fnmatch.fnmatchcase(f, g) for g in r["globs"]):
                    r["files"].append(f)
                    hit = True
            if not hit:
                squat[root_key].append(f)
    return rows, squat


def cmd_projections(args):
    """projections [--json] — the projection registry: every on-disk store
    helm writes, classified (authored/projection/events/state/backup), each
    projection naming its source + rebuild. Laws 2+3's read surface;
    `helm doctor` enforces it (source present, rebuild declared, staleness,
    squatters)."""
    # flags-only membership reader — guard the tail before the survey:
    # `projections --bogus` silently printed the table and exited 0.
    from .cli import guard_tail
    rc = guard_tail("helm projections", args, flags=("--json",),
                    usage="projections [--json]")
    if rc is not None:
        return rc
    rows, squat = projection_survey()
    if "--json" in args:
        import json
        print(json.dumps({"rows": rows, "squatters": squat}, ensure_ascii=False, indent=2))
        return 0
    print("helm projections (%d rows over %s + %s):"
          % (len(rows), home.helm_home(), cache_root()))
    w = max(len(r["name"]) for r in rows)
    for r in rows:
        extra = "" if r["kind"] != "projection" else \
            "  <- %s | rebuild: %s" % (r["source"], r["rebuild"])
        n = len(r["files"])
        print("  %-*s %-10s %4d file%s%s" % (w, r["name"], r["kind"], n,
                                             "s"[:n != 1] or " ", extra))
    n = sum(len(v) for v in squat.values())
    if not n:
        print("  no unclassified squatters")
        return 0
    print("  %d UNCLASSIFIED squatter file%s (no row names them — classify or evict):"
          % (n, "s"[:n != 1]))
    for root_key, root_dir in (("home", home.helm_home()), ("cache", cache_root())):
        for f in squat[root_key][:8]:
            print("    ? " + os.path.join(root_dir, f))
        if len(squat[root_key]) > 8:
            print("    … +%d more under %s" % (len(squat[root_key]) - 8, root_dir))
    return 0
