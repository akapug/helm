"""helm slice-limits --seats — each seat slice's memory limits, DERIVED from
the fleet's own derivation (task/4062).

THE FAILURE THIS EXISTS FOR. A probe in a local worker seat grew a python3
past 9 GB, twice. It stayed under its seat slice's ceiling and under
agents.slice's 48G throttle line, while the host swapped 28 GB and froze the
owner's Orca twice. Every seat slice carried the same literal pair, 12G
throttle and 16G kill, from three writers that nothing reconciled: the claude
entry shim's `set-property` defaults, a hand-written prefix drop-in
(agents-.slice.d/per-seat-memory.conf), and set-property stopgaps typed
by hand (10G/14G on three local seats, which the prefix drop-in undoes at the
next daemon-reload). 27 seats at 16G is nine times the fleet's 48G line, so no
seat's own kill line could bind before the fleet's throttle stalled every seat
together.

ONE SOURCE. A seat's MemoryMax is a share of agents.slice's DERIVED
MemoryHigh (slicelimits.derive), by role; its MemoryHigh is SEAT_BAND of that,
never under HIGH_FLOOR; MemorySwapMax is 0, as the fleet's is. Each share is
declared once, in SEAT_SHARES, with a floor, and rounded down to whole GiB.
On the 24-CPU, 87.7 GiB box the fleet line is 48G, so a lead gets 12G/16G
(the values every seat carries today) and a worker 6G/8G: the 9 GB runaway
dies in its own slice.

NO HIGH UNDER THE FLOOR. Seats throttled at a 3G High were measured frozen,
so a per-seat High is never under HIGH_FLOOR. A cap under HIGH_CAP_MIN has
no room for one below the kill: that seat gets MemoryHigh=infinity (no
per-seat throttle, systemd's default, written so that no other file's High
loads in its place), keeps its MemoryMax at the cap, and agents.slice's own
High does the throttling. That happens on small boxes only: on a 32G box
a worker's cap is 2G.

THE ROLE (role_of). A seat is a lead when helm launched it with the lead
posture (HELM_SEAT_ROLE=lead, seat_role.py), when its name is a lead's by the
naming teams.role_for proposes leads with (`<project>-claude`, and the
integrator's `-integrator`), or when it has no name at all: the shim names a
seat with no HELM_CHAT_NAME `pid<N>`, and an owner-opened pane is one of those
(the integrator ran in one, measured at 8.7G of anon and shmem). A seat those
rules read as a worker takes the role it RECORDS instead (task/4112): the seat
half computes one folded-key posture per pass from each seat's spawn register,
else — for a native seat helm never spawned — its role.json declaration
(seat_role.recorded_role reads the same two), and reads lead if it records
lead. A record or declaration that would not read, that names another seat, or
a fold two differently-declared seats collide into, reads no role, so the seat
stays a worker, never promoted on a guess. Every other seat is a worker.

A seat that is not running has no process to read: its name alone would
make a lead named outside LEAD_SUFFIXES a worker, so it gets no role file
unless `--role-for SEAT=ROLE` names it, and keeps the lead default.

A STOPPED SEAT'S OLD FILE (stale). A role file an earlier apply wrote while
the seat ran, or one an older name-only apply wrote for a lead, still loads
at the seat's next reload: a lead relaunched at 12G/16G drops to the
worker's 6G/8G. So a role file on a stopped seat whose default is lead, by
its name or by the lead posture its spawn register records (the register is
the authority seat_role.recorded_role reads), is stale: the plan names it
and why, and `--apply` moves it aside. A worker's file agrees with its
default and stays. A file `--role-for` wrote carries ROLE_FOR_LINE and is
never stale: --role-for is the one way to keep a non-default role. When the
registers will not read and the name does not decide, the file is refused
as UNKNOWN, never kept or removed on a guess.

THE FILES. The PREFIX drop-in agents-.slice.d/SEAT_DROPIN carries the LEAD
class: it is every seat slice's default, so a pid-named slice nobody can name
ahead gets it when systemd creates the slice, and a worker default there
would OOM-kill an owner's pane at the next reload. A running worker seat
gets its own agents-<seat>.slice.d/ROLE_DROPIN carrying the worker class.
systemd applies drop-ins with different names in lexicographic order across
every directory (systemd.unit(5)), and ROLE_DROPIN sorts after SEAT_DROPIN,
which sorts after the shim's 50-Memory*.conf. A file that sorts after
helm's in a seat's own directory, or a same-named file in a higher-priority
unit directory, would still win: `--apply` computes what each seat loads
once its files are written (stack) and refuses, naming the file, when that
is not helm's. It writes helm's files first, moves every other prefix
drop-in that sets a seat memory limit aside (a renamed file is the backup),
runs `systemctl --user daemon-reload`, reads every populated seat slice
again, and prints the rollback. It never lowers a running seat whose own
anon and shmem would not fit under the new throttle line with HEADROOM to
spare, and it lowers nothing at all without --lower.
"""
import math
import os
import re
import shlex
import subprocess
import time
from collections import namedtuple
from fractions import Fraction

from . import pk, seatceiling, slicelimits

GIB = slicelimits.GIB
#: Every seat's default, in the prefix drop-in directory: the lead class.
SEAT_DROPIN = "zzz-helm-seat-limits.conf"
#: A named worker seat's own drop-in. "zzz-helm-seat-r" sorts after
#: "zzz-helm-seat-l", so it applies after the prefix default.
ROLE_DROPIN = "zzz-helm-seat-role.conf"
#: The limits a seat drop-in owns, in the order every line prints them.
SEAT_MANAGED = ("MemoryHigh", "MemoryMax", "MemorySwapMax")
ROLES = ("lead", "worker")

Share = namedtuple("Share", "fraction floor")
#: WHAT ONE SEAT MAY HOLD, declared once: its MemoryMax is the larger of a
#: fraction of agents.slice's derived MemoryHigh and a floor, never above that
#: line. A lead holds a third of the fleet: an integrator's pane measured 8.7G
#: of memory it can neither reclaim nor swap. A worker holds a sixth: the
#: largest worker measured 2.5G, and builds and suites belong on the fabric.
SEAT_SHARES = {"lead": Share(Fraction(1, 3), 4 * GIB),
               "worker": Share(Fraction(1, 6), 2 * GIB)}
#: MemoryHigh as a share of the seat's MemoryMax: the throttle band below the
#: kill (per-seat-memory.conf's lesson: with High == Max the first symptom is
#: the kill).
SEAT_BAND = Fraction(3, 4)
#: The High at which seats were measured throttle-frozen.
SEAT_FREEZE = 3 * GIB
#: No per-seat MemoryHigh is ever under this: one GiB above the freeze.
HIGH_FLOOR = SEAT_FREEZE + GIB
#: The smallest MemoryMax that holds HIGH_FLOOR one GiB below its kill. A
#: smaller cap gets no per-seat High at all.
HIGH_CAP_MIN = HIGH_FLOOR + GIB
#: Why a seat has no per-seat High: every line that omits one says it.
NO_HIGH = ("throttle left to the fleet line: a per-seat High under %dG "
           "freezes seats" % (HIGH_FLOOR // GIB))
#: A running seat is lowered only when its anon + shmem, times this, fits
#: under the new MemoryHigh: the seat keeps room to work after the change.
HEADROOM = Fraction(3, 2)
#: teams.role_for's lead names: the integrator, and `<project>-claude`.
LEAD_SUFFIXES = ("-integrator", "-claude")
#: The name the shim gives a seat with no HELM_CHAT_NAME.
UNNAMED = re.compile(r"^pid[0-9]+$")
#: A seat name as the shim folds it.
FLAT = re.compile(r"^[A-Za-z0-9_]+$")
ROLE_ENV = "HELM_SEAT_ROLE"
#: A member's environ is read for the lead posture only up to this many pids.
ENV_PIDS = 64
#: The line a role file named by --role-for carries: on a stopped seat it is
#: the owner's choice, never stale.
ROLE_FOR_LINE = "# --role-for %s=%s"

#: One populated seat slice. `held` is anon + shmem (None when memory.stat
#: would not read); `live` is {limit: bytes, INF or None}.
Seat = namedtuple("Seat", "name path pids role marked live current peak "
                          "anon shmem file held events")


def _fold(text):
    return re.sub(r"[^A-Za-z0-9_]", "_", text)


def seat_of(slice_name):
    """The folded seat name inside `agents-<seat>.slice`, or None."""
    m = re.match(r"^agents-(.+)\.slice$", slice_name or "")
    return m.group(1) if m else None


def role_of(slice_name, marked=False, postures=None):
    """"lead" or "worker" for a seat slice. `marked` is the lead posture
    (HELM_SEAT_ROLE=lead) of a process in it. `postures` {folded seat: role}
    is the recorded-role map (a seat's spawn register, else its native
    declaration): a seat the name rule reads as a worker takes the role it
    records, and a seat the map cannot decide (a collision, or an unreadable
    or mismatched record) stays a worker, never promoted on a guess."""
    seat = seat_of(slice_name) or ""
    if marked or not seat or UNNAMED.match(seat) \
            or any(seat.endswith(_fold(s)) for s in LEAD_SUFFIXES):
        return "lead"
    role = postures.get(seat) if postures else None
    return role if role in ROLES else "worker"


def derive(fleet_high, role):
    """{limit: (value, why)} for one seat of `role` under a fleet whose
    derived MemoryHigh is `fleet_high` bytes."""
    s = SEAT_SHARES[role]
    top = min(Fraction(fleet_high), max(Fraction(s.floor),
                                        s.fraction * fleet_high))
    cap = max(1, math.floor(top / GIB)) * GIB
    share = slicelimits._share(s.fraction)
    if cap < HIGH_CAP_MIN:
        high = (slicelimits.INF, "none per seat: MemoryMax %s is under %s, "
                "too small to hold a %s High below it; %s" % (
                    slicelimits.show("MemoryMax", cap),
                    slicelimits.show("MemoryMax", HIGH_CAP_MIN),
                    slicelimits.show("MemoryHigh", HIGH_FLOOR), NO_HIGH))
    else:
        high = (max(HIGH_FLOOR, math.floor(cap * SEAT_BAND / GIB) * GIB),
                "%s of MemoryMax %s, down to whole GiB, never under %s: the "
                "throttle band below the kill" % (
                    slicelimits._share(SEAT_BAND),
                    slicelimits.show("MemoryMax", cap),
                    slicelimits.show("MemoryHigh", HIGH_FLOOR)))
    return {"MemoryHigh": high,
            "MemoryMax": (cap, "max(%s, %s of agents.slice's derived "
                          "MemoryHigh %s), down to whole GiB"
                          % (slicelimits._gib(s.floor), share,
                             slicelimits.show("MemoryHigh", fleet_high))),
            "MemorySwapMax": (0, "a runaway seat process is killed, never "
                              "swapped, as agents.slice's are")}


def no_high(r):
    """[line] naming each class that gets no per-seat High, and why."""
    return ["%s: no per-seat MemoryHigh (MemoryMax %s): %s" % (
        role, slicelimits.show("MemoryMax", got["MemoryMax"][0]), NO_HIGH)
        for role, got in sorted((r.get("classes") or {}).items())
        if got["MemoryHigh"][0] == slicelimits.INF]


def classes(r):
    """{role: derive()} from a slicelimits reading, or {} when the box would
    not read."""
    fleet = (r.get("derived") or {}).get("MemoryHigh")
    return {k: derive(fleet[0], k) for k in ROLES} if fleet else {}


def props(seat, marked=False, b=None):
    """The set-property arguments a launch stamps on `seat`'s slice, or
    (None, why). The claude entry shim's one call into helm."""
    b = b or slicelimits.box()
    facts, why = slicelimits.read_box(b)
    if not facts:
        return None, why
    fleet = slicelimits.derive(facts["cpus"], facts["mem"])["MemoryHigh"][0]
    name = seatceiling.seat_slice_name(seat) if seat else "agents-pid0.slice"
    # the recorded role counts here as it does in the reading: a lead whose
    # name is not lead-shaped (a numbered seat) is stamped a lead's limits
    post = postures()[0] if not marked else None
    got = derive(fleet, role_of(name, marked, post))
    return ["%s=%s" % (k, slicelimits.render(k, got[k][0]))
            for k in SEAT_MANAGED], ""


# ---------------------------------------------------------------- reading

def _num(text):
    v = seatceiling.parse_size(text)
    return slicelimits.INF if v == seatceiling.UNLIMITED else \
        v if isinstance(v, int) else None


def _marked(pids, proc):
    """True when a process of the slice carries HELM_SEAT_ROLE=lead. Only
    that one entry is looked for; nothing else in the environ is kept."""
    want = ("%s=lead" % ROLE_ENV).encode()
    for pid in sorted(pids)[:ENV_PIDS]:
        try:
            with open(os.path.join(proc, str(pid), "environ"), "rb") as fh:
                if want in fh.read().split(b"\0"):
                    return True
        except OSError:
            continue
    return False


def read_seats(b, role_for=None, postures=None):
    """([Seat] for every seat slice with a live same-uid process, why).
    `role_for` {folded seat: role} overrides the role a seat reads as;
    `postures` (postures()) is the recorded-role map a seat the name rule
    reads as a worker consults."""
    role_for = role_for or {}
    if not slicelimits._may_read(b):
        return [], "this box is not read: %s is off" % seatceiling.SWITCH
    members, trouble = seatceiling.slice_members(b.cgroup, b.proc)
    if trouble:
        return [], trouble
    out = []
    for path, pids in sorted(members.items()):
        name = os.path.basename(path)
        marked = _marked(pids, b.proc)

        def f(n):
            return seatceiling._read(os.path.join(path, n))
        stat = {k: seatceiling.stat_value(path, k)
                for k in ("anon", "shmem", "file")}
        held = None if None in (stat["anon"], stat["shmem"]) \
            else stat["anon"] + stat["shmem"]
        ev = seatceiling.parse_events(f("memory.events")) or {}
        out.append(Seat(name, path, tuple(sorted(pids)),
                        role_for.get(seat_of(name))
                        or role_of(name, marked, postures),
                        marked,
                        {"MemoryHigh": _num(f("memory.high")),
                         "MemoryMax": _num(f("memory.max")),
                         "MemorySwapMax": _num(f("memory.swap.max"))},
                        _num(f("memory.current")), _num(f("memory.peak")),
                        stat["anon"], stat["shmem"], stat["file"], held, ev))
    return out, ""


def reading(b=None, status=None, role_for=None):
    """Everything one seat pass knows, on top of slicelimits.reading."""
    return extend(slicelimits.reading(b, status), role_for)


def extend(r, role_for=None):
    """A slicelimits reading with the seat half added. `role_for` {folded
    seat: role} is what --role-for names; the recorded-role map is computed
    once here and shared by the seat reading and the stale check."""
    b = r["box"]
    post, post_why = postures()
    seats, why = read_seats(b, role_for, post)
    prefix = [c for c in slicelimits._confs(b.dirs, slicelimits.SEAT_DROPINS)
              if c.name != SEAT_DROPIN or c.path != prefix_path(b)]
    old, unknown = stale(b, seats, role_for or {},
                         read=lambda: (post, post_why)) if not why else ([], [])
    return dict(r, seats=seats, seats_why=why, classes=classes(r),
                role_for=dict(role_for or {}), prefix=prefix, stale=old,
                stale_unknown=unknown, postures=dict(post),
                postures_why=post_why)


def postures():
    """({folded seat: role}, why). Each fold takes the role its spawn register
    records, else, for a native seat helm never spawned, the role its
    role.json declaration names — the two seat_role.recorded_role reads. A
    record or declaration with no role, or one helm does not know, is a
    worker's; two seats folding to one name with different records read as a
    lead when either is. `why` names every register, declaration or listing
    that would not read, and a fold two differently-declared native seats
    collide into reads unknown there, named, never guessed."""
    # the facade import binds the names seat_lifecycle reads at call time
    from . import home, seat  # noqa: F401
    from .seat_lifecycle import _register_candidates, _register_entries, \
        _spawn_record_read
    out, blind, collide = {}, [], []

    def failed(path, exc):
        if not isinstance(exc, FileNotFoundError):
            blind.append(path)
    for name, path in _register_candidates(failed):
        if not os.path.lexists(path):
            continue
        rec, why = _spawn_record_read(os.path.dirname(path))
        if why or rec is None:
            blind.append(path)
            continue
        flat = _fold(str(rec.get("seat") or name))
        role = rec.get("role") if rec.get("role") in ROLES else "worker"
        if out.get(flat) != "lead":
            out[flat] = role
    # a native seat helm never spawned has no register: its role is its
    # role.json declaration, keyed on its REAL name, folded to the slice's.
    # A missing, unreadable or mismatched declaration records no role, so it
    # neither promotes nor collides; only a listing that would not read is
    # named, as an unread register is. A name with a spawn register on disk
    # is the register's alone: one that read is above, and one that would
    # not is broken, never absent, so its declaration does not stand in for
    # it (seat_role._declared_role reads it the same way).
    from .seat_role import _unresolved_register
    base = os.path.join(home.global_dir(), "seats", seat.NATIVE_FAMILY,
                        "instances")
    declared = {}
    for name, d in _register_entries(base, failed):
        if _unresolved_register(name):
            continue
        rec = pk.read_json(os.path.join(d, "role.json"), None)
        if not isinstance(rec, dict) or rec.get("seat") != name \
                or rec.get("role") not in ROLES:
            continue
        declared.setdefault(_fold(name), {}).setdefault(
            rec["role"], []).append(name)
    for flat, roles in sorted(declared.items()):
        if flat in out:
            continue
        if len(roles) == 1:
            out[flat] = next(iter(roles))
        else:
            out[flat] = "unknown"
            collide.append("the native role.json declarations of %s fold to "
                           "one name with different roles, so its default is "
                           "unknown" % ", ".join(sorted(
                               n for names in roles.values() for n in names)))
    notes = list(collide)
    if blind:
        notes.append("would not read: %s" % ", ".join(blind))
    return out, "; ".join(notes)


def stale(b, seats, role_for, read=postures):
    """([(seat, path, why)], [refusal]) for helm's role file on each seat
    that is not running and that --role-for does not name: stale when the
    seat's default is lead and --role-for did not write the file; a refusal
    when that default is UNKNOWN."""
    running = {seat_of(s.name) for s in seats}
    where = b.home
    try:
        dirs = sorted(os.listdir(where))
    except FileNotFoundError:
        return [], []
    except OSError as exc:
        return [], ["%s would not list (%s); a stopped seat's role file there "
                    "is UNKNOWN" % (where, exc.__class__.__name__)]
    old, unknown, known = [], [], None
    for d in dirs:
        seat = seat_of(d[:-2]) if d.endswith(".slice.d") else None
        path = os.path.join(where, d, ROLE_DROPIN)
        if not seat or seat in running or seat in role_for \
                or not os.path.lexists(path):
            continue
        head = "agents-%s.slice is stopped" % seat
        text = slicelimits._text(path)
        if text is None:
            unknown.append("%s: %s would not read; what it loads at the "
                           "seat's next reload is UNKNOWN" % (head, path))
            continue
        # only the line --role-for writes for THIS seat keeps the file: a
        # file copied from another seat's directory names that seat
        if ROLE_FOR_LINE % (seat, "worker") in text.splitlines():
            continue
        if role_of("agents-%s.slice" % seat) == "lead":
            why = "its name is a lead's"
        else:
            known = known or read()
            if known[0].get(seat) == "lead":
                why = "its spawn register records the lead posture"
            elif known[1]:
                unknown.append("%s: its default is UNKNOWN (spawn registers "
                               "%s), so %s is neither kept nor moved aside"
                               % (head, known[1], path))
                continue
            else:
                continue
        old.append((seat, path, "%s and %s: this role file would load the "
                    "worker class at its next reload; only --role-for keeps "
                    "a non-default role" % (head, why)))
    return old, unknown


def parse_role_for(text):
    """({folded seat: role}, "") from `SEAT=ROLE[,SEAT=ROLE...]`, or
    (None, why)."""
    out = {}
    for part in (text or "").split(","):
        seat, eq, role = part.strip().partition("=")
        flat = _fold(seat.strip())
        if not eq or role.strip() not in ROLES or not flat \
                or UNNAMED.match(flat):
            return None, ("--role-for takes SEAT=ROLE[,SEAT=ROLE...] with "
                          "ROLE one of %s and a named seat; got %r"
                          % ("/".join(ROLES), part.strip()))
        out[flat] = role.strip()
    return out, ""


def prefix_path(b):
    return os.path.join(b.home, slicelimits.SEAT_DROPINS, SEAT_DROPIN)


def role_path(b, seat):
    return os.path.join(b.home, "agents-%s.slice.d" % seat, ROLE_DROPIN)


def dropin_dirs(slice_name):
    """The drop-in directories systemd reads for `slice_name`, most specific
    first: its own, then one per truncation of its name after each dash,
    longest first (systemd.unit(5)): agents-a-b.slice reads
    agents-a-b.slice.d, agents-a-.slice.d and agents-.slice.d."""
    stem, _dot, kind = slice_name.rpartition(".")
    out, i = [slice_name + ".d"], stem.rfind("-")
    while i >= 0:
        out.append("%s-.%s.d" % (stem[:i], kind))
        i = stem.rfind("-", 0, i)
    return out


def stack(b, slice_name, files=None, gone=()):
    """[Conf] of every drop-in a reload reads for `slice_name`, in name
    order, a hidden one marked. systemd walks the unit directories by
    priority and, inside each, the slice's own directory then each
    dash-prefix one (dropin_dirs), then the type-level <type>.d (slice.d
    here), and the first file of a name hides every later one (systemd's
    unit_file_find_dropin_paths, then conf_files_list_strv): a more specific
    directory wins only inside one unit directory, a higher-priority unit
    directory's prefix file hides a same-named file in a lower one's own
    directory, and the type-level dir is lowest precedence overall, so any
    same-named name-specific file in any unit directory masks a type-level
    file. `files` {path: text} and `gone` [path] are what an apply writes and
    moves aside: the stack once it has."""
    files, seen, out = files or {}, set(), []
    for d in b.dirs:
        for sub in dropin_dirs(slice_name):
            where = os.path.join(d, sub)
            confs = {c.path: c for c in slicelimits._confs([d], sub)
                     if c.path not in gone}
            confs.update({p: slicelimits.Conf(
                p, os.path.basename(p), *slicelimits.parse_conf(t),
                masked=False) for p, t in files.items()
                if os.path.dirname(p) == where})
            for c in sorted(confs.values(), key=lambda c: c.name):
                out.append(c._replace(masked=c.name in seen))
                seen.add(c.name)
    # type-level <type>.d is last: systemd adds it after every name-specific
    # dir (unit_file_find_dropin_paths), and conf_files_list_strv's name-based
    # masking then hides a type-level file under any same-named name-specific
    # file, from ANY unit directory.
    type_d = slice_name.rpartition(".")[2] + ".d"
    for d in b.dirs:
        where = os.path.join(d, type_d)
        confs = {c.path: c for c in slicelimits._confs([d], type_d)
                 if c.path not in gone}
        confs.update({p: slicelimits.Conf(
            p, os.path.basename(p), *slicelimits.parse_conf(t),
            masked=False) for p, t in files.items()
            if os.path.dirname(p) == where})
        for c in sorted(confs.values(), key=lambda c: c.name):
            out.append(c._replace(masked=c.name in seen))
            seen.add(c.name)
    return sorted(out, key=lambda c: (c.name, c.masked))


def files_for(b, slice_name, files=None, gone=(), mem=None):
    """{limit: (value, raw, path)} a reload loads for `slice_name`: the
    last assignment of the stack winning."""
    return slicelimits.effective(None, stack(b, slice_name, files, gone), mem)


def roles(r):
    """{folded seat: role} for every seat whose role is KNOWN: each running
    named seat's, read from its process, and each seat --role-for names. A
    stopped seat's name alone never decides it: a lead named outside
    LEAD_SUFFIXES would get the worker's kill line at its next launch."""
    out = {seat_of(s.name): s.role for s in r["seats"]
           if FLAT.match(seat_of(s.name) or "")
           and not UNNAMED.match(seat_of(s.name))}
    out.update(r.get("role_for") or {})
    return out


def others(r):
    """Every prefix drop-in besides helm's that sets a seat memory limit."""
    return [c for c in r["prefix"]
            if c.sets is None or set(c.sets) & set(SEAT_MANAGED)]


# ---------------------------------------------------------------- the plan

def target(r, seat):
    """{limit: value} a seat slice gets once the files load."""
    return {k: v[0] for k, v in r["classes"][seat.role].items()}


def plan(r, lower=False):
    """(files {path: text}, removes [path], refusals [str])."""
    b = r["box"]
    files = {prefix_path(b): render(r, "lead", "every seat slice's default "
                                    "(a pid-named seat is an owner pane)")}
    want = roles(r)
    removes = []
    for seat, role in sorted(want.items()):
        path = role_path(b, seat)
        if role == "worker":
            files[path] = render(r, "worker", "agents-%s.slice" % seat,
                                 seat if seat in r.get("role_for", {})
                                 else None)
        elif os.path.exists(path):
            removes.append(path)
    removes += [p for _seat, p, _why in r.get("stale") or ()]
    refuse = list(r.get("stale_unknown") or []) + overrides(r, files, removes)
    # under an agents.slice whose live swap ceiling is 0, a seat's own
    # MemorySwapMax going from infinity to 0 changes nothing it can do
    swap_bound = r["live"].get("MemorySwapMax") == 0
    for s in r["seats"]:
        to = target(r, s)
        blind = [k for k in SEAT_MANAGED if s.live[k] is None]
        if blind:
            refuse.append("%s: live %s UNKNOWN" % (s.name, ", ".join(blind)))
            continue
        low = [k for k in SEAT_MANAGED if s.live[k] is not None
               and to[k] < s.live[k]
               and not (k == "MemorySwapMax" and swap_bound)]
        if low and not lower:
            refuse.append("%s: %s lower than now; --lower sets it" % (
                s.name, ", ".join("%s %s -> %s" % (
                    k, slicelimits.show(k, s.live[k]),
                    slicelimits.show(k, to[k])) for k in low)))
            continue
        # the seat's first new line: its High, or its kill when it has none
        line = min(("MemoryHigh", "MemoryMax"), key=lambda k: to[k])
        if low and (s.held is None or s.held * HEADROOM > to[line]):
            refuse.append("%s holds %s of anon + shmem; x%s does not fit "
                          "under the new %s %s" % (
                              s.name, "UNKNOWN" if s.held is None
                              else slicelimits._gib(s.held),
                              float(HEADROOM), line,
                              slicelimits.show(line, to[line])))
    return files, removes, refuse


def overrides(r, files, removes):
    """[refusal] for each seat slice helm decides (every running one, every
    seat it writes or moves a role file for) where a limit would load from a
    file other than helm's once the apply has run."""
    b = r["box"]
    gone = list(removes) + [c.path for c in others(r)]
    names = sorted({s.name for s in r["seats"]}
                   | {"agents-%s.slice" % k for k in roles(r)}
                   | {"agents-%s.slice" % k for k, _p, _w in r.get("stale")
                      or ()})
    out = []
    for name in names:
        seat = seat_of(name)
        want = role_path(b, seat) if role_path(b, seat) in files \
            else prefix_path(b)
        confs = stack(b, name, files, gone)
        out += ["%s: %s would not read (not UTF-8, or unreadable); what it "
                "sets is UNKNOWN" % (name, c.path) for c in confs
                if c.sets is None and not c.masked]
        got = slicelimits.effective(None, confs, r["facts"]["mem"])
        off = [k for k in SEAT_MANAGED if got.get(k, (0, 0, None))[2] != want]
        out += ["%s: %s would load %s from %s, which wins over helm's %s" % (
            name, k, slicelimits.show(k, got[k][0]) if k in got
            else "nothing", got[k][2] if k in got else "no file", want)
            for k in off]
        out += ["%s: %s hides %s" % (name, c.path, want) for c in confs
                if off and c.name == os.path.basename(want)
                and c.path != want and not c.masked]
    return out


def render(r, role, who, named=None):
    """One seat drop-in's text. `named` is the folded seat --role-for names:
    its file carries ROLE_FOR_LINE, so a stopped seat keeps it."""
    f, got = r["facts"], r["classes"][role]
    lines = ["# helm slice-limits --seats (task/4062): the %s class, for %s."
             % (role, who),
             "# Written by `helm slice-limits --seats --apply`; `helm "
             "slice-limits --seats` prints the derivation. Do not edit by "
             "hand.",
             "# Box: %s; %s." % (f["cpus_from"], f["mem_from"])]
    lines += [ROLE_FOR_LINE % (named, role)] if named else []
    lines += ["# %s: %s" % (k, got[k][1]) for k in SEAT_MANAGED]
    return "\n".join(lines + ["[Slice]"] + [
        "%s=%s" % (k, slicelimits.render(k, got[k][0]))
        for k in SEAT_MANAGED]) + "\n"


def rollback_lines(moves, wrote, made=()):
    """`moves` are (backup, path); `wrote` the paths that did not exist;
    `made` the directories the apply created."""
    out = ["rm -f %s" % shlex.quote(p) for p in wrote]
    out += ["rmdir %s" % shlex.quote(d) for d in made]
    out += ["mv -f %s %s" % (shlex.quote(a), shlex.quote(p))
            for a, p in moves]
    return out + ["systemctl --user daemon-reload"]


def apply(r, lower=False, stamp=None):
    """Write helm's seat drop-ins, move the other prefix ones aside, reload.
    -> (rc, lines, rollback lines)."""
    if not r["classes"]:
        return 2, ["helm slice-limits --seats: cannot derive (%s)"
                   % r["why"]], []
    if r["seats_why"]:
        return 2, ["helm slice-limits --seats: the running seats would not "
                   "read (%s); refusing to change limits under them"
                   % r["seats_why"]], []
    files, removes, refuse = plan(r, lower)
    unread = [c.path for c in others(r) if c.sets is None]
    if unread:
        refuse.append("unreadable prefix drop-in(s): %s" % ", ".join(unread))
    if refuse:
        return 2, ["helm slice-limits --seats: refusing: %s" % "; ".join(
            refuse)], []
    stamp = stamp or time.strftime("%Y%m%dT%H%M%S")
    moves, wrote, made, lines = [], [], [], []
    try:
        # helm's files FIRST: from here on a reload loads the derivation,
        # whatever stops this process next
        for path, text in files.items():
            if slicelimits._text(path) == text:
                continue
            if os.path.exists(path):
                os.link(path, path + slicelimits.MOVED + stamp)
                moves.append((path + slicelimits.MOVED + stamp, path))
            else:
                wrote.append(path)
                if not os.path.isdir(os.path.dirname(path)):
                    made.append(os.path.dirname(path))
            pk.atomic_write(path, text)
            lines.append("wrote %s" % path)
        whys = {p: why for _seat, p, why in r.get("stale") or ()}
        for path in removes + [c.path for c in others(r)]:
            backup = path + slicelimits.MOVED + stamp
            os.rename(path, backup)
            moves.append((backup, path))
            lines.append("moved aside: %s -> %s%s" % (
                path, backup, ": %s" % whys[path] if path in whys else ""))
    except OSError as exc:
        return 2, lines + ["helm slice-limits --seats: stopped (%s: %s); the "
                           "rollback below undoes what was done"
                           % (exc.__class__.__name__, exc)], \
            rollback_lines(moves, wrote, made)
    back = rollback_lines(moves, wrote, made)
    if not lines:
        off = [s.name for s in r["seats"] if s.live != target(r, s)]
        if not off:
            return 0, ["helm slice-limits --seats: the files carry the "
                       "derivation and every running seat matches; nothing "
                       "written"], []
    try:
        done = subprocess.run(["systemctl", "--user", "daemon-reload"],
                              capture_output=True, text=True, timeout=60)
        fail = done.returncode and "exit %d: %s" % (
            done.returncode, (done.stderr or done.stdout).strip()[:200])
    except (OSError, subprocess.TimeoutExpired) as exc:
        fail = "%s: %s" % (exc.__class__.__name__, exc)
    if fail:
        return 1, lines + ["helm slice-limits --seats: the files are "
                           "written, but `systemctl --user daemon-reload` "
                           "failed (%s); they apply at the next reload"
                           % fail], back
    lines += no_high(r)
    seats, why = read_seats(r["box"], r.get("role_for"), r.get("postures"))
    off = [s.name for s in seats if s.live != target(r, s)]
    lines.append("daemon-reloaded; %d running seat slice(s) read %s" % (
        len(seats), "the derivation" if not off and not why
        else "UNKNOWN (%s)" % why if why
        else "otherwise: %s" % ", ".join(off)))
    return (1 if off or why else 0), lines, back


# ---------------------------------------------------------------- reports

def _m(v):
    return "?" if v is None else "max" if v == slicelimits.INF \
        else "%.1fG" % (float(v) / GIB)


def report(r, lower=False):
    """(lines, drift)."""
    lines = ["helm slice-limits --seats: each seat slice's memory limits, "
             "derived from agents.slice's (task/4062)"]
    if not r["classes"]:
        return lines + ["  cannot derive: %s" % r["why"]], True
    for role in ROLES:
        got = r["classes"][role]
        lines.append("  %-6s %s" % (role, "; ".join(
            "%s %s = %s" % (k, slicelimits.show(k, got[k][0]), got[k][1])
            for k in SEAT_MANAGED)))
    lines += ["  %s" % line for line in no_high(r)]
    drift = False
    if r["seats_why"]:
        drift = True
        lines.append("  running seats UNKNOWN: %s" % r["seats_why"])
    lines.append("  %-40s %-6s %6s %6s %5s %6s %6s %6s %6s %8s %4s  %s" % (
        "running seat slice", "role", "high", "max", "swap", "cur", "peak",
        "anon", "shmem", "ev.high", "oom", "-> high/max"))
    for s in r["seats"]:
        to = target(r, s)
        same = s.live == to
        drift |= not same
        lines.append("  %-40s %-6s %6s %6s %5s %6s %6s %6s %6s %8s %4s  %s" % (
            s.name, s.role + "*" * s.marked, _m(s.live["MemoryHigh"]),
            _m(s.live["MemoryMax"]), _m(s.live["MemorySwapMax"]),
            _m(s.current), _m(s.peak), _m(s.anon), _m(s.shmem),
            s.events.get("high", "?"), s.events.get("oom_kill", "?"),
            "same" if same else "%s/%s" % (_m(to["MemoryHigh"]),
                                           _m(to["MemoryMax"]))))
    lines.append("  (* = HELM_SEAT_ROLE=lead; cur and peak count page cache, "
                 "anon and shmem do not reclaim in a swapless slice)")
    for c in others(r):
        drift = True
        lines.append("  other source: %s: %s" % (c.path, slicelimits._sets(c)))
    files, removes, refuse = plan(r, lower)
    fresh = [p for p, t in sorted(files.items()) if slicelimits._text(p) != t]
    drift |= bool(fresh)
    if prefix_path(r["box"]) in fresh:
        lines.append("  --apply writes %s (the lead class)"
                     % prefix_path(r["box"]))
    workers = [seat_of(os.path.basename(os.path.dirname(p))[:-2])
               for p in fresh if p != prefix_path(r["box"])]
    if workers:
        lines.append("  --apply writes %s (the worker class) for %d named "
                     "seat(s): %s" % (os.path.join(
                         r["box"].dirs[-1], "agents-<seat>.slice.d",
                         ROLE_DROPIN), len(workers), " ".join(workers)))
    whys = {p: why for _seat, p, why in r.get("stale") or ()}
    for path in removes:
        drift = True
        lines.append("  --apply moves aside %s" % path + (
            ": %s" % whys[path] if path in whys
            else " (that seat is a lead now)"))
    for why in refuse:
        lines.append("  --apply refuses: %s" % why)
    return lines, drift


def doctor_rows(r):
    """[("warn", text)] when running seats or the files differ from the
    derivation; [] on a box with no seat slice and no seat drop-in. A
    ("note", text) row names each class with no per-seat High, and why."""
    if not r.get("classes") or not (r["seats"] or r["prefix"]
                                    or r["seats_why"] or r.get("stale")
                                    or r.get("stale_unknown")):
        return []
    notes = [("note", "seat limits: %s" % line) for line in no_high(r)]
    if r["seats_why"]:
        return [("warn", "seat limits: running seats UNKNOWN (%s)"
                 % r["seats_why"])] + notes
    off = ["%s (%s: %s)" % (s.name, s.role, ", ".join(
        "%s %s, derived %s" % (k, _m(s.live[k]), _m(target(r, s)[k]))
        for k in SEAT_MANAGED if s.live[k] != target(r, s)[k]))
        for s in r["seats"] if s.live != target(r, s)]
    rows = [("warn", "seat limits: stale role file %s: %s. `helm slice-limits "
             "--seats --apply` moves it aside" % (p, why))
            for _seat, p, why in r.get("stale") or ()]
    rows += [("warn", "seat limits: %s" % why)
             for why in r.get("stale_unknown") or ()]
    if off:
        rows.append(("warn", "seat limits: %d running seat slice(s) differ "
                     "from the derivation: %s. `helm slice-limits --seats` "
                     "prints it" % (len(off), "; ".join(off))))
    extra = others(r)
    if extra:
        rows.append(("warn", "seat limits: %d prefix drop-in(s) besides "
                     "helm's set seat memory limits: %s" % (len(extra), "; ".join(
                         "%s (%s)" % (c.path, slicelimits._sets(c))
                         for c in extra))))
    return (rows or [("ok", "seat limits: every running seat slice matches "
                      "the derivation (lead %s/%s, worker %s/%s)" % (
                          _m(r["classes"]["lead"]["MemoryHigh"][0]),
                          _m(r["classes"]["lead"]["MemoryMax"][0]),
                          _m(r["classes"]["worker"]["MemoryHigh"][0]),
                          _m(r["classes"]["worker"]["MemoryMax"][0])))]) \
        + notes


def as_json(r, lower=False):
    def num(v):
        return None if v is None else "infinity" if v == slicelimits.INF \
            else v
    files, removes, refuse = plan(r, lower) if r["classes"] else ({}, [], [])
    return {"classes": {role: {k: {"value": num(v), "why": w}
                               for k, (v, w) in got.items()}
                        for role, got in r["classes"].items()},
            "shares": {k: {"fraction": str(v.fraction), "floor": v.floor}
                       for k, v in SEAT_SHARES.items()},
            "band": str(SEAT_BAND), "headroom": str(HEADROOM),
            "high_floor": HIGH_FLOOR, "high_cap_min": HIGH_CAP_MIN,
            "no_high": no_high(r),
            "seats": [{"slice": s.name, "role": s.role, "marked": s.marked,
                       "pids": len(s.pids),
                       "live": {k: num(v) for k, v in s.live.items()},
                       "current": s.current, "peak": num(s.peak),
                       "anon": s.anon, "shmem": s.shmem, "file": s.file,
                       "events": s.events,
                       "target": target(r, s) if r["classes"] else None}
                      for s in r["seats"]],
            "seats_why": r["seats_why"], "role_for": r.get("role_for") or {},
            "writes": sorted(p for p, t in files.items()
                             if slicelimits._text(p) != t),
            "removes": removes, "refuses": refuse,
            "stale": [{"seat": k, "path": p, "why": w}
                      for k, p, w in r.get("stale") or ()],
            "others": [c.path for c in others(r)]}
