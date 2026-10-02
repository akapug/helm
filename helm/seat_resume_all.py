"""helm seat resume --all — the POST-REBOOT SWEEP, and the timer's payload.

THE INCIDENT (measured 2026-08-22, the morning after a laptop reboot). Orca
restores every pane by replaying a reconstructed `claude --dangerously-skip-
permissions --resume <sid>` with NO environment. Native-Claude seats survive:
their sessions live in ~/.claude. Every cliproxy seat (codex, codex-2..5,
kimi, ds4pro, gemini, grok) fails with "No conversation found", because its
session lives under the per-seat CLAUDE_CONFIG_DIR that only the seat's
launch.sh sets — and the pane is left at a bare shell. Because Orca bypassed
helm's launch owner, the replayed scrollback also leaves the renderer's
mouse-tracking modes armed, so every mouse move prints 35;5;40M into that
shell. `helm seat rebind --all --apply`, already firing on a timer, read all
nine seats and refused all nine ("0 exact live Claude processes") — a correct
guard with no actuator behind it. This module is the actuator.

ONE ROW PER REGISTERED SEAT, classified through rebind's OWN proof:

  LIVE       rebind proved a live claude process on a pane -> re-stamped (or
             would be), no relaunch. On a healthy fleet this is every row and
             the sweep spawns nothing — the timer stays cheap and idempotent.
  DEAD-PANE  rebind refused with exactly the two zero-count sentences
             (`rebind_refusal_means_dead`), the independent claude census
             agrees, and the register's pane KEY still resolves to a live,
             writable pane -> TERMINAL_DISARM is written to that pane, then
             the seat is resumed INTO it (`_resume(reboot_dead=True)`), so the
             owner's layout survives.
  PANE-GONE  the same death evidence and orca answers terminal_not_found for
             the pane key -> `_resume` mints a pane through the metaharness.
  NOT-LIVE-BEFORE-BOOT
             the same death evidence and a register that predates the boot,
             but the seat is absent from the live set recorded in the boot
             before this one (below) -> nothing is done; the reason names the
             dead state it was found in.
  UNKNOWN    anything else: an unreadable register, a rebind refusal that is
             NOT the death sentence (ambiguity, a live process whose pane is
             not in inventory, an RPC failure), a census that disagrees with
             rebind, a config dir with no transcript, a pane key resolving to
             a pane that is not writable, a pane key a LIVE claude process
             carries in its environ (the pane is up but not bare — typing a
             launch line there would land in someone's composer), or a dead
             row whose pre-boot live set did not read. Rendered
             with the reason, counted
             in the exit code under --apply, NEVER folded into skip or dead.

NO NEW SESSION IS EVER MINTED. A relaunch always carries `--resume <sid>` for
the newest transcript in the seat's own config dir; a seat without one is
UNKNOWN, because `--continue` in an empty config dir would start a fresh
conversation and call it a resume.

REBOOT-BOUND ACTUATION. The timer runs forever, not only after a boot, and a
seat that dies MID-DAY (a crash, a context wedge, a pane the owner closed on
purpose) reads DEAD-PANE/PANE-GONE just the same. Acting on those would have
the timer relaunch a seat five minutes after the owner shut it. So the sweep
relaunches only a seat whose register PREDATES THE CURRENT BOOT (`ts` vs
/proc/stat btime): that register describes a pane the boot destroyed, which
is the one case this verb exists for. `_resume` rewrites the register with a
fresh stamp, so a seat the sweep relaunched is never relaunched again by the
same boot, whatever happens to it next — and a post-boot casualty is reported
with "register postdates boot" so an operator can resume it by hand. An
orca-adopted seat (a roster row, no register) is bound the same way through
its roster `last_seen`.

THE PRE-BOOT LIVE SET. "Predates the boot" is necessary and not sufficient:
after ANY reboot every register predates the boot, so the stamp alone would
relaunch every seat that ever had a register — seats merged into another,
retired seats, test seats, rows whose session id is junk — and an unattended
boot (a crash, a power loss) has no pre-flight step to stop it. So every
--apply pass records the seats it found LIVE, plus the ones it resumed, in
`<HELM_HOME>/_global/.state/resume-live-set/<boot id>.json` (disk, never
tmpfs; one atomic rename; the file NAME is the kernel's boot id), and a dead
row is acted on only when its seat is in the set recorded LAST in an earlier
boot. The boot id, not btime, tells the boots apart: btime is the wall clock
minus uptime, so a clock step moves it inside one boot, while the id is fixed
for the life of a boot. The record is rewritten on every pass, so the last
pass before a boot ended is the one the next boot reads; this boot's own
record is never its population. Absent and unreadable are separate answers
and both act on nothing: no earlier record is one footer line and the dead
rows say so, while a record that does not read, or a boot id that does not,
makes each such row UNKNOWN and names the file and the error — it is never
read as an empty set (every dead row NOT-LIVE-BEFORE-BOOT on no evidence)
nor as every seat. A dry run reads the set and records nothing. A pass whose
roster did not read records nothing either: a set missing the adopted seats
would read as complete at the next boot.

FLEET HOLD, PER BOOT. When `<HELM_HOME>/_global/.state/fleet-hold` exists
AND was written during the current boot (mtime >= btime), the sweep
classifies and reports exactly as before but performs NO resume and NO disarm
write; LIVE seats are still re-stamped (read-mostly, and the timer was doing
that already). Held rows say HELD, never SKIPPED, so a held fleet cannot be
mistaken for a swept one, and the footer names the marker. A marker OLDER
than the boot is a hold that EXPIRED: the owner's hold was "keep the fleet
quiet until this next reboot", and a file on disk outlives a reboot, so
without this bound the only ways to honour it were a hand step between
"install the timer" and "reboot" (which relaunches the dead seats BEFORE the
reboot, the opposite of quiet) or a CLI step for the owner afterwards. The
sweep removes an expired marker and says so in the footer; a hold meant to
survive a boot is re-created after it.

DESIRED-DOWN, PER SEAT. A seat an operator stood down with `helm seat down`
carries a desired-down record in its proxy home (helm/seat_down.py). Its dead
row reads DOWN (desired), names who stood it down, when and why, and is never
relaunched: a reboot is not the operator changing their mind. It is not
UNKNOWN and not a failure, so it never moves rc. `helm seat up <seat>` clears
the record, and the next sweep treats the seat like any other. A record the
sweep cannot read is planned as if absent, and the row says so.

THE COMPOSED RULE (`gate`). A dead row is resumed ONLY IF its seat is in the
pre-boot live set AND it is not desired-down. When both refuse, or when the
set is missing or unreadable, the row reads DOWN (desired) and names what the
set answered: the record is the operator's standing reason and outlives the
boot, the set only measures the boot before. And the live set never records a
desired-down seat as live, even one whose pane is still up (`seat down` stops
the proxy, not the pane): a seat stood down before the last pass of a boot is
outside the next boot's population, so clearing the record after the reboot
(`seat up`) leaves it NOT-LIVE-BEFORE-BOOT, and `seat resume <seat>` is the
hand relaunch. A seat stood down after the last pass is still in the set, and
its record alone keeps it down.
"""
import calendar
import contextlib
import io
import json
import os
import re
import shlex
import sys
import time

from . import home, pk
from .seat_launch_owner import TERMINAL_DISARM

LIVE, DEAD_PANE, PANE_GONE, UNKNOWN = "LIVE", "DEAD-PANE", "PANE-GONE", "UNKNOWN"
NOT_LIVE_BEFORE_BOOT = "NOT-LIVE-BEFORE-BOOT"
STATES = (LIVE, DEAD_PANE, PANE_GONE, NOT_LIVE_BEFORE_BOOT, UNKNOWN)
HOLD_MARKER = "fleet-hold"
RESUMING = "RESUMING"          # the one ACTION value the sweep acts on
HELD = "HELD"
DOWN = "DOWN (desired)"        # an operator's `seat down`; never relaunched
# the sweep's name as an UNATTENDED caller of `seat._resume` (task/3695): no
# operator is there to choose --defaults, so a recipe missing only fields a
# defaults resume never carried resumes on the defaults, never left down
SWEEP = "the reboot sweep"

LIVE_SET_DIR = "resume-live-set"
BOOT_ID_PATH = "/proc/sys/kernel/random/boot_id"
_BOOT_ID = re.compile(r"\A[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\Z")
# read_pre_boot's three answers; only SET_READ lets a dead row be planned
SET_READ, SET_MISSING, SET_UNREADABLE = "read", "missing", "unreadable"
_LEAD = "helm seat resume --all: "

# orca's own answer for a pane key the runtime no longer knows. PANE-GONE
# requires this POSITIVE answer; any other resolve failure (daemon down,
# metadata missing, a malformed reply) is UNKNOWN — absence of a pane must be
# stated by the host, never inferred from a probe that could not ask.
PANE_NOT_FOUND = "terminal_not_found"


def hold_marker_path():
    return os.path.join(home.global_dir(), ".state", HOLD_MARKER)


def hold_state():
    """(held, expired) — a marker written during this boot holds; one that
    predates the boot expired with it. No marker: (False, False). An
    unreadable btime cannot prove expiry, so the marker holds."""
    try:
        mtime = os.stat(hold_marker_path()).st_mtime
    except OSError:
        return False, False
    boot = boot_epoch()
    if boot is not None and mtime < boot:
        return False, True
    return True, False



def disarm_line():
    """The ONE shell line that writes TERMINAL_DISARM to a pane's terminal.

    The bytes have to reach the RENDERER, and only a process writing to the
    pane's pty can put them there — so the line is `printf` of the constant,
    typed into the bare shell. Two encodings, both load-bearing: ESC travels
    as the printf escape `\\033` rather than as a raw 0x1b, because a raw ESC
    typed into readline is a KEY SEQUENCE prefix (ESC [ ? ...) that readline
    eats and mangles; and the line is prefixed with Ctrl-U (readline's
    unix-line-discard), because the replayed mouse reports this exists to
    stop have already typed 35;12;51M... into the shell's line buffer, and a
    command appended to that junk is "command not found", not a disarm.
    Derived from the constant at call time so there is exactly one source."""
    text = TERMINAL_DISARM.decode("ascii")
    assert "'" not in text and "%" not in text and "\\" not in text
    return "\x15printf '%s'" % text.replace("\x1b", "\\033")


def pane_launch_line(command, cwd):
    """The launch line for a REUSED pane: cd to the seat's cwd, then the exact
    command `_resume` would have handed `ad.spawn` — which carries the
    launch.sh PATH, never the expanded token-bearing line (seat.py's TOKEN
    LAW). No `exec`, so the pane drops back to its shell when the seat exits
    and the owner can read why."""
    return "cd %s && %s" % (shlex.quote(cwd), command) if cwd else command


def _register_epoch(rec):
    """Seconds since the epoch for a register's `ts` (pk.now_ts shape), or
    None when absent or unparseable."""
    ts = (rec or {}).get("ts")
    if not isinstance(ts, str):
        return None
    try:
        return calendar.timegm(time.strptime(ts, "%Y-%m-%dT%H:%M:%SZ"))
    except ValueError:
        return None


def boot_epoch():
    from . import rearm
    return rearm._boot_time()


def boot_id():
    """This boot's kernel id, or None when it does not read as one. It names
    the live-set file a pass records and excludes that file from the pre-boot
    choice, so a value that is not the kernel's uuid shape is None, never a
    file name."""
    try:
        with open(BOOT_ID_PATH, encoding="ascii") as fh:
            raw = fh.read().strip()
    except (OSError, ValueError):
        return None
    return raw if _BOOT_ID.match(raw) else None


def live_set_dir():
    return os.path.join(home.global_dir(), ".state", LIVE_SET_DIR)


def _load_live_set(path, boot):
    """(seats, why) for one recorded set, validated WHOLE: a file that is not
    exactly what `record_live_set` writes is unreadable, never partly read."""
    try:
        doc = pk.read_json(path, strict=True)
    except (OSError, ValueError) as e:
        return None, str(e)
    if not isinstance(doc, dict) or doc.get("v") != 1 \
            or isinstance(doc.get("v"), bool):
        return None, "not a version-1 live-set record"
    if doc.get("boot_id") != boot:
        return None, "its boot_id is not the boot its file name says"
    seats = doc.get("seats")
    if not isinstance(seats, dict):
        return None, "its seats field is not an object"
    if not all(isinstance(e, dict)
               and isinstance(e.get("session"), (str, type(None)))
               for e in seats.values()):
        return None, "a seat entry is not an object with a session id or null"
    return {k: e["session"] for k, e in seats.items()}, None


def read_pre_boot(boot):
    """(answer, seats, line): the live set recorded LAST in a boot other than
    `boot` — the only seats a post-boot pass may relaunch.

    answer is SET_READ with seats {seat: session or None}, SET_MISSING when no
    earlier boot recorded one, or SET_UNREADABLE when this boot's id, the
    directory or the newest earlier record did not read. `line` is the one
    footer sentence saying which. "Last" is the newest file by mtime among
    the other boots' files, and each boot's file is rewritten by every pass
    in it, so the last pass before a boot ended wins."""
    if boot is None:
        return SET_UNREADABLE, None, _LEAD + (
            "this boot's id did not read from %s, so no record can be told "
            "from this boot's own; no seat is relaunched as a reboot casualty"
            % BOOT_ID_PATH)
    d = live_set_dir()
    try:
        names = sorted(os.listdir(d))
    except FileNotFoundError:
        names = []
    except OSError as e:
        return SET_UNREADABLE, None, _LEAD + (
            "the live-set directory %s did not read: %s; no seat is "
            "relaunched as a reboot casualty" % (d, e))
    found = []
    for n in names:
        stem = n[:-len(".json")] if n.endswith(".json") else ""
        if stem == boot or not _BOOT_ID.match(stem):
            continue                    # this boot's own, a tmp, or a stranger
        path = os.path.join(d, n)
        try:
            found.append((os.lstat(path).st_mtime, path, stem))
        except OSError as e:
            return SET_UNREADABLE, None, _LEAD + (
                "the live-set record %s did not read: %s; no seat is "
                "relaunched as a reboot casualty" % (path, e))
    if not found:
        return SET_MISSING, None, _LEAD + (
            "no live set was recorded in an earlier boot (%s); no seat is "
            "relaunched as a reboot casualty until one is" % d)
    _mtime, path, stem = max(found)
    seats, why = _load_live_set(path, stem)
    if why:
        return SET_UNREADABLE, None, _LEAD + (
            "the pre-boot live set %s did not read: %s; no seat is relaunched "
            "as a reboot casualty, and it is never read as empty" % (path, why))
    return SET_READ, seats, _LEAD + (
        "pre-boot live set %s names %d seat(s); a dead row outside it reads "
        "%s and is left alone" % (path, len(seats), NOT_LIVE_BEFORE_BOOT))


def admit(row, pre):
    """True when a dead row the boot binding kept actionable may be planned:
    its seat is in the pre-boot live set. Any other answer rewrites the row
    to say why nothing is done, and is False."""
    answer, seats, _line = pre
    if answer == SET_MISSING:
        row.action = "none: no pre-boot live set"
        return False
    if answer != SET_READ:
        row.state = UNKNOWN
        row.reason += "; its pre-boot live set did not read (see footer)"
        return False
    if row.seat not in seats:
        row.reason = "%s now: %s" % (row.state, row.reason)
        row.state, row.action = NOT_LIVE_BEFORE_BOOT, "none: not live before boot"
        return False
    return True


def record_live_set(table, apply, partial, boot, btime, recipes=None):
    """(line, failed): write THIS boot's live set, the next boot's
    population. It holds every row found LIVE and every row resumed with rc 0
    — a crash before the next pass must not forget a seat this pass brought
    back. Written only under --apply, and never from a partial table.

    `recipes` rides beside the population, never inside it (task/3695): each
    seat's LAUNCH RECIPE as its live process was last captured, carried
    forward from record to record (helm/seat_recipe.py `carried`), so a seat
    that parks or dies still has the settings a resume restores exactly.
    `seats` alone decides who is relaunched; `recipes` decides nothing here.

    A DESIRED-DOWN SEAT IS NEVER LIVE HERE. `seat down` stops a proxy seat's
    proxy, not its pane, so a stood-down seat can still read LIVE; recorded,
    it would be the next boot's population, and the only thing then keeping
    it dead would be its record. Left out, a seat stood down before the last
    pass of a boot is outside the next boot's set, and reads DOWN (or, once
    `seat up` clears the record, NOT-LIVE-BEFORE-BOOT) after it. Only a
    READABLE record leaves a seat out: an unreadable one is supervised as if
    absent (helm/seat_down.py), and the row already says so. The line names
    every seat left out."""
    from . import seats as seats_mod
    found = [r for r in table if r.state == LIVE or r.action == "resumed"]
    seats = {r.seat: {"session": r.session,
                      "via": "live" if r.state == LIVE else "resumed"}
             for r in found if not r.down}
    down = sorted(seats_mod._seat_label(r.seat) for r in found if r.down)
    left = ("; %d desired-down seat(s) left out, never recorded live: %s"
            % (len(down), ", ".join(down))) if down else ""
    if not apply:
        return _LEAD + ("this boot's live set (%d seat(s)) is not recorded: "
                        "DRY RUN%s" % (len(seats), left)), False
    if partial:
        return _LEAD + ("this boot's live set is NOT recorded: the roster did "
                        "not read, and a set missing the adopted seats would "
                        "read as complete at the next boot"), True
    if boot is None:
        return _LEAD + ("this boot's live set is NOT recorded: this boot's id "
                        "did not read from %s" % BOOT_ID_PATH), True
    path = os.path.join(live_set_dir(), boot + ".json")
    doc = {"v": 1, "boot_id": boot, "btime": btime, "written": pk.now_ts(),
           "seats": seats}
    try:
        if recipes is None:
            pk.atomic_write(path, json.dumps(doc, indent=1, sort_keys=True)
                            + "\n")
        else:
            # TWO PASSES CAN WRITE AT ONCE (the timer and a hand pass): the
            # recipes merge with the map on disk UNDER ONE LOCK, the later
            # capture of each seat winning (task/3695), so a pass that read
            # the map before another wrote it never writes an older capture
            # over a newer one
            from . import seat_recipe
            from .seats_common import _flocked
            with _flocked(os.path.join(live_set_dir(), ".recipes.lock")):
                doc["recipes"] = seat_recipe.keep_newest(
                    recipes, seat_recipe.newest_recipes())
                pk.atomic_write(path, json.dumps(doc, indent=1,
                                                 sort_keys=True) + "\n")
    except OSError as e:
        return _LEAD + ("could not record this boot's live set at %s: %s"
                        % (path, e)), True
    return _LEAD + ("recorded this boot's live set, %d seat(s): %s%s"
                    % (len(seats), path, left)), False


def _memo(fn):
    box = []

    def get():
        if not box:
            box.append(fn())
        return box[0]
    return get


class Row(object):
    # `down` is the seat's READABLE desired-down record, set by `gate`: a row
    # carrying one is never relaunched and never recorded in the live set
    # `pids` is an adopted LIVE row's resolved processes, kept so the recipe
    # capture needs no second /proc walk (task/3695)
    __slots__ = ("seat", "kind", "state", "session", "action", "reason",
                 "down", "pids")

    def __init__(self, seat, kind, state, session=None, action="-", reason=""):
        self.seat, self.kind, self.state = seat, kind, state
        self.session, self.action, self.reason = session, action, reason
        self.down = None
        self.pids = None

    def line(self):
        # the seat column is the table's first and widest, and for an adopted
        # seat it is a ROSTER KEY (unvalidated at the join seam) — laundered
        # here, at the one emit site, the same law as `seat panes`
        from . import seats
        return _ROW % (
            seats._seat_label(self.seat), self.kind, self.state,
            (self.session or "-")[:8], self.action, self.reason)


# the STATE column fits its widest word, so every row stays aligned
_ROW = "%%-14s %%-14s %%-%ds %%-8s %%-30s %%s" % max(map(len, STATES))
HEADER = _ROW % ("SEAT", "FAMILY/HARNESS", "STATE", "SESSION", "ACTION",
                 "REASON")


def _inventory_row(rows, handle):
    return next((r for r in rows if r.get("handle") == handle), None)


def _resolve_dead_pane(seat, ad, rec, rows):
    """(state, handle, reason) for a seat rebind proved nobody holds.

    The pane KEY is the durable half of the register (tab:leaf survives the
    boot; the handle does not — measured 2026-08-22: 0 of 9 registered
    handles were in inventory while 6 of 9 pane keys resolved to the bare
    shells orca had restored at the same positions). A registered handle that
    is itself live in inventory and DIFFERS from what the key resolves to is
    a contradiction, and contradictions are UNKNOWN."""
    old = rec.get("handle")
    old_row = _inventory_row(rows, old) if old else None
    old_live = old_row is not None and seat._pane_live(old_row)
    key = rec.get("pane_key")
    if not key:
        if not old:
            return UNKNOWN, None, "register carries no pane key and no handle"
        if old_live:
            return DEAD_PANE, old, "registered handle %s is a live pane" % old
        if old_row is not None:
            return UNKNOWN, None, ("registered handle %s is in inventory but "
                                   "not live (%s); no pane key to re-resolve"
                                   % (old, old_row.get("status") or "?"))
        return PANE_GONE, None, ("registered handle %s is absent from "
                                 "inventory and the register has no pane key"
                                 % old)
    try:
        resolved = ad.resolve_pane(key)
    except Exception as e:                  # noqa: BLE001 — every probe error is UNKNOWN, by name
        if PANE_NOT_FOUND in str(e):
            if old_live:
                return UNKNOWN, None, ("orca: pane key %s not found, yet "
                                       "registered handle %s is live"
                                       % (key[:16], old))
            return PANE_GONE, None, "orca: pane key %s not found" % key[:16]
        return UNKNOWN, None, "pane key resolution failed: %s" % e
    handle = (resolved or {}).get("handle")
    if not handle:
        return UNKNOWN, None, "pane key %s resolved without a handle" % key[:16]
    if old_live and old != handle:
        return UNKNOWN, None, ("pane key resolves to %s but registered "
                               "handle %s is live too" % (handle, old))
    row = _inventory_row(rows, handle)
    if row is None:
        return UNKNOWN, None, ("pane key resolves to %s, absent from inventory"
                               % handle)
    if not seat._pane_live(row):
        why = ("orphaned (PTY has no live renderer)" if row.get("orphaned")
               else row.get("status") or "not live")
        return UNKNOWN, None, "pane %s is %s" % (handle, why)
    return DEAD_PANE, handle, "pane key resolves to live bare pane %s" % handle


def _bind_to_boot(row, stamp, boot, what):
    """Keep a dead row actionable only when `stamp` predates the boot."""
    if stamp is None or boot is None:
        row.state = UNKNOWN
        row.reason += ("; cannot bind %s to a boot (stamp %r, btime %r)"
                       % (what, stamp, boot))
        return False
    if stamp >= boot:
        row.action = "none: %s postdates boot" % what
        row.reason += "; not a reboot casualty, resume by hand"
        return False
    return True


def classify_spawned(name, ad, rows, census, held_sids, boot, apply,
                     bind_boot=True, locked=False):
    """(row, handle) for one seat with a helm spawn register. `apply` is the
    REBIND apply flag (the hold never withholds a re-stamp). `census` and
    `held_sids` are zero-arg callables, consulted only once rebind has
    refused — a healthy fleet never pays for a /proc walk here. `bind_boot`
    is False only for `prove_reboot_dead` (a hand resume names its seat), and
    `locked` is its statement that `_resume` already holds the seat lock."""
    from . import orcaadopt, seat
    family, err = seat._seat_family(name)
    if err:
        return Row(name, "?/orca", UNKNOWN,
                   reason="register sits under no known family: " + err), None
    kind = "%s/orca" % family
    d = seat._instance_dir(family, name)
    rec = seat._spawn_record(d)
    if rec is None:
        return Row(name, kind, UNKNOWN,
                   reason="spawn register unreadable or not a JSON object"), None
    sid_reg = rec.get("session")
    if rec.get("seat") != name:
        return Row(name, kind, UNKNOWN, sid_reg,
                   reason="spawn register names seat %r" % rec.get("seat")), None
    kind = "%s/%s" % (family, rec.get("harness") or "?")
    if rec.get("harness") != "orca":
        return Row(name, kind, UNKNOWN, sid_reg,
                   reason="register harness %r is outside this sweep (orca "
                          "panes only)" % rec.get("harness")), None
    fields, err = seat.rebind_seat(name, d, ad, apply=apply, locked=locked)
    if not err:
        if fields.get("unchanged"):
            action = "already current"
        else:
            action = "re-stamped register" if apply else "would re-stamp register"
        # The pane TITLE is re-stamped inside `rebind_seat`, beside the
        # register it re-stamps — one hook serving this sweep and `seat
        # rebind` both. What arrives here is its REPORT, rendered on the row
        # the owner reads; it is empty when the tab already read correctly.
        return Row(name, kind, LIVE, fields.get("session") or sid_reg, action,
                   "live claude process on pane %s%s"
                   % (fields.get("handle"),
                      fields.get("title_note") or "")), None
    if not seat.rebind_refusal_means_dead(err, rec):
        return Row(name, kind, UNKNOWN, sid_reg,
                   reason="rebind refused: " + err), None
    # rebind found no process by session and none by name. Two independent
    # instruments must agree before that reads as death: the claude census
    # (HELM_CHAT_NAME + roster history + cannot-look) and the live-session map
    # (pid-keyed records across every config dir + `--resume` argv).
    procs, unreadable = census()
    state, evidence = orcaadopt.seat_liveness(name, procs=procs,
                                              unreadable=unreadable)
    if state != orcaadopt.DEAD:
        return Row(name, kind, UNKNOWN, sid_reg,
                   reason="rebind found no process but the claude census says "
                          "%s: %s" % (state, evidence)), None
    # A pane that RESOLVES is up, not BARE. The one process that could be
    # sitting in it without answering to this seat's name or session is
    # another claude — and every live claude's environ carries the key of
    # the pane it runs in, so the census already holds the answer.
    key = rec.get("pane_key")
    squat = next((p for p in procs if key and p.get("pane_key") == key), None)
    if squat:
        return Row(name, kind, UNKNOWN, sid_reg,
                   reason="rebind found no process for this seat, but pane "
                          "key %s is held by live claude pid %s (seat %r); "
                          "the pane is not bare"
                          % (key[:16], squat.get("pid"), squat.get("seat"))), None
    held = held_sids()
    if sid_reg and sid_reg in held:
        return Row(name, kind, UNKNOWN, sid_reg,
                   reason="rebind found no process but session %s is open in "
                          "pid %s" % (sid_reg[:8], held[sid_reg])), None
    sid, _cwd = seat._newest_seat_session(d, prefer_source=sid_reg)
    if not sid:
        return Row(name, kind, UNKNOWN, sid_reg,
                   reason="config dir holds no session transcript; nothing to "
                          "resume and no new session is minted"), None
    # THE SAME BAR THE ACT USES: `_resume` pins this exact session by id,
    # and the by-id lookup refuses a stub (no assistant turn) or an
    # ambiguous id. A table that selected what the act will refuse would
    # print RESUMING over a guaranteed failure.
    if not seat._seat_session_by_id(d, sid)[0]:
        return Row(name, kind, UNKNOWN, sid_reg,
                   reason="newest transcript %s is a stub (no assistant turn) "
                          "or ambiguous; nothing real to resume" % sid[:8]), None
    state, handle, why = _resolve_dead_pane(seat, ad, rec, rows)
    row = Row(name, kind, state, sid, reason=why)
    if state == UNKNOWN:
        return row, None
    if bind_boot and not _bind_to_boot(row, _register_epoch(rec), boot,
                                       "register"):
        return row, None
    return row, handle


def inventory_failure(exc):
    """Why the inventory is missing, in the terms the reader needs.

    A failed inventory is a fact about the INSTRUMENT. Rendered as a bare
    "pane inventory failed" it reads as a statement about the ESTATE, and the
    reader concludes the fleet is gone when nothing about the fleet was
    measured — the exact reading that turned one broken CLI wrapper into
    hours of "every seat is dead" (2026-08-27). One renderer, shared by both
    callers, so the distinction cannot hold on one path and be lost on the
    other."""
    from .harness import CLI_PROTOCOL_UNREACHED
    detail = "pane inventory failed: %s" % exc
    if getattr(exc, "code", None) != CLI_PROTOCOL_UNREACHED:
        return detail
    return (detail + " — the metaharness CLI failed BEFORE it could answer, so "
            "NOTHING about the fleet was measured: no seat is known dead and "
            "no pane is known gone. Repair the CLI, then re-run")


def prove_reboot_dead(name, ad, bind_boot=False):
    """(state, handle, reason) for `helm seat resume <seat>` BY HAND — the
    sweep's proof for ONE named seat, after the reap resolver has refused.

    The reap asks "can I stop the recorded pane?", and for a pane the boot
    destroyed (or left as a bare shell under a rotated handle) the honest
    answer is "cannot be checked or reaped" — right for its question, fatal
    for the operator's: `helm seat resume codex-3` on the morning after a
    reboot aborted on every proxy seat, so the ONLY manual path was the
    whole-fleet sweep. This is the sweep's classifier run for one seat,
    UNBOUND from the boot stamp and the fleet-hold marker: those two guards
    exist so a TIMER never relaunches a seat the owner closed on purpose,
    and an operator naming a seat is exactly the decision they stand in
    for — except that the SWEEP's own in-lock re-proof passes
    bind_boot=True, so its act is bound to the boot exactly as its table
    was (the hold is a sweep-level decision, taken before any act).
    Anything but DEAD-PANE / PANE-GONE is returned with its reason so
    the caller can abort with BOTH refusals on screen; nothing is sent.
    Called from INSIDE `_resume`'s lifecycle lock, so rebind runs with
    locked=True (same lock file; a second flock from the same process never
    returns)."""
    from . import orcaadopt, sessions
    try:
        rows = ad.list()
    except Exception as e:                  # noqa: BLE001 — no inventory, no proof
        return UNKNOWN, None, inventory_failure(e)
    row, handle = classify_spawned(name, ad, rows,
                                   _memo(orcaadopt.claude_processes),
                                   _memo(sessions.live_sids),
                                   boot_epoch() if bind_boot else None, False,
                                   bind_boot=bind_boot, locked=True)
    if row.state in (DEAD_PANE, PANE_GONE) and row.action != "-":
        # bound to the boot and found to postdate it: not a casualty to act on
        return UNKNOWN, None, row.action + "; " + row.reason
    return row.state, handle, row.reason


def classify_adopted(name, ad, boot, roster_row=None, rows=(), apply=False):
    """(row, actionable) for a roster seat helm never spawned (orca-adopted):
    no register to re-stamp, no pane key to resolve, so DEAD is always
    PANE-GONE and the relaunch is orcaadopt.resume via `_resume`.

    NO REGISTER DOES NOT MEAN NOTHING TO RE-STAMP. An adopted seat's pane
    title reverts on a reboot exactly like a spawned one's, and this is the
    population the owner's OWN coordinating seats live in — hand-launched, so
    they carry no spawn register and `rebind` has never been able to reach
    them. `rows` is the pane inventory the sweep already holds and `apply` is
    its write flag; the resolve below has already paid for the /proc walk a
    title needs, so this costs nothing further."""
    from . import orcaadopt, orcatitle, seats
    kind = "adopted/orca"
    try:
        info = orcaadopt.resolve(name, adapter=ad)
    except Exception as e:                  # noqa: BLE001 — a probe error is a reason, not a verdict
        return Row(name, kind, UNKNOWN,
                   reason="orcaadopt.resolve failed: %s" % e), False
    if info is None:
        return Row(name, kind, UNKNOWN,
                   reason="roster row carries no session id and no live "
                          "process claims it"), False
    sids = info.get("sessions") or []
    sid = sids[0] if sids else None
    state = info.get("state")
    if state == orcaadopt.LIVE:
        stamp = orcatitle.restamp_one(
            ad, name, _inventory_row(rows, info.get("handle")), apply=apply)
        row = Row(name, kind, LIVE, sid, "none: no register to stamp",
                  (info.get("evidence") or "") + orcatitle.note(stamp))
        row.pids = list(info.get("pids") or ())
        return row, False
    if state != orcaadopt.DEAD:
        return Row(name, kind, UNKNOWN, sid,
                   reason=info.get("evidence") or "state %r" % state), False
    if not sid:
        return Row(name, kind, UNKNOWN, sid,
                   reason="no session id recorded; nothing to resume"), False
    row = Row(name, kind, PANE_GONE, sid,
              reason=(info.get("evidence") or "") +
                     "; adopted seats carry no pane key")
    seen = seats.last_seen(name, roster_row)
    return row, _bind_to_boot(row, seen if isinstance(seen, (int, float))
                              else None, boot, "roster last_seen")


def desired_down(name):
    """(record, error) for a seat's desired-down record (helm/seat_down.py).
    Never raises into the sweep: a read that breaks is an error, and an error
    is the SUPERVISED direction — the seat is swept as if no record existed,
    and its row says why."""
    from . import seat_down
    try:
        return seat_down.read_seat(name)
    except Exception as e:                  # noqa: BLE001 — a reason on the row, never a dead sweep
        return None, "desired-down record could not be read: %s" % e


def gate(row, apply, held, pre):
    """THE COMPOSED RULE, over one classified row: a dead row is resumed ONLY
    IF its seat is in the pre-boot live set AND no operator stood it down.

    Each half answers a different question and neither implies the other. The
    live set measures the boot before (was this seat running when the boot
    ended?); the desired-down record states the operator's intent (should it
    run at all?). A seat live before the boot and stood down after the last
    pass is IN the set, so only its record keeps it dead; a seat stood down
    and never live is OUTSIDE it and carries the record too.

    DOWN WINS when both say no, and when the set did not read at all. The
    record is a standing statement that outlives any number of boots and
    names its own remedy; the set is only a measurement of the boot before,
    and a set helm could not read or never wrote says nothing about a seat
    whose operator already answered the one question the row asks. So the row
    reads DOWN (desired), never NOT-LIVE-BEFORE-BOOT or UNKNOWN, never moves
    rc, and still says what the set answered — after `seat up` clears the
    record, that answer is the row's word.

    A LIVE row whose seat carries a record stays LIVE (its pane IS alive: the
    proxy is what `seat down` stops) and is marked so `record_live_set`
    leaves it out. An UNREADABLE record is supervised as if absent on every
    row it reaches, and the row says so (helm/seat_down.py). A dead row the
    boot binding already answered (a register stamped this boot) is left to
    that answer, with the record named beside it."""
    from . import seat_down
    if row.state not in (LIVE, DEAD_PANE, PANE_GONE):
        return row
    rec, unreadable = desired_down(row.seat)
    if unreadable:
        row.reason += "; ⚠ " + seat_down.unreadable_text(row.seat, unreadable)
    row.down = rec or None
    if row.state == LIVE:
        if rec:
            row.reason += ("; %s — left out of this boot's live set, so no "
                           "later boot relaunches it" % seat_down.describe(rec))
        return row
    if row.action != "-":
        if rec:
            row.reason += "; %s" % seat_down.describe(rec)
        return row
    if rec:
        return plan(row, apply, held, rec, pre)
    if admit(row, pre):
        plan(row, apply, held)
    return row


def _beside_the_set(seat_name, pre):
    """What a DOWN row says about the pre-boot live set it did not need."""
    answer, seats, _line = pre
    if answer == SET_READ and seat_name in seats:
        return ""
    if answer == SET_READ:
        return ("; also absent from the pre-boot live set — DOWN wins: the "
                "record is the operator's standing reason, the set only "
                "measures the boot before, so once `helm seat up %s` clears "
                "the record this row reads %s, and `helm seat resume %s` "
                "relaunches it by hand" % (seat_name, NOT_LIVE_BEFORE_BOOT,
                                            seat_name))
    if answer == SET_MISSING:
        return ("; no pre-boot live set either (see footer) — DOWN wins: the "
                "operator's record needs no set")
    return ("; its pre-boot live set did not read (see footer) — DOWN wins: "
            "the operator's record already answers whether it is relaunched, "
            "so the row is not UNKNOWN")


def plan(row, apply, held, down=None, pre=None):
    """Fill the ACTION column for a dead row `gate` admitted, or for one
    whose seat carries a readable desired-down record `down`. RESUMING is
    what the sweep acts on, HELD is rendered as itself so a held fleet never
    reads as swept.

    DOWN comes first and is its own action: an operator ran `helm seat down`
    on this seat, and a reboot is not an operator changing their mind. The
    seat is reported, never relaunched, and never counted as a failure; it
    outranks HELD too, because the hold lifts with the boot and the record
    does not. `helm seat up <seat>` clears the record and the next sweep
    treats the seat like any other — through the pre-boot live set, which
    `_beside_the_set` names on the row whenever it would not admit the seat."""
    from . import seat_down
    if down:
        row.action = DOWN
        row.reason += "; %s — not relaunched; %s%s" % (
            seat_down.describe(down), seat_down.resume_hint(row.seat),
            _beside_the_set(row.seat, pre) if pre else "")
        return row
    if held:
        row.action = HELD
    elif not apply:
        row.action = ("would disarm + resume into pane" if row.state == DEAD_PANE
                      else "would resume into a new pane")
    else:
        row.action = RESUMING
    return row


def act(name, ad, sid):
    """(rc, captured) — run `_resume` for one dead seat, output captured so
    the table stays readable; the caller prints it indented under the row.
    The table's classification is a REPORT; the proof that licenses the
    write is re-taken by `_resume` inside the seat's lifecycle lock, and the
    pane it resolves there is the one written to — so a seat that came
    alive between this table and this call is refused, not injected over.
    The SESSION is pinned too: `sid` is the transcript the table classified,
    and `_resume` resumes that one or refuses — never a newer other one."""
    from . import seat
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        try:
            rc = seat._resume(name, [], adapter=ad, reboot_dead=True,
                              reboot_sid=sid, unattended=SWEEP)
        except Exception as e:              # noqa: BLE001 — the row must say FAILED, not die mid-table
            print("helm seat: resume raised %s: %s" % (type(e).__name__, e))
            rc = 1
    return rc, out.getvalue()


def cmd_resume_all(rest):
    # the verb guards its own tail — a sweep that can relaunch a fleet must
    # not read --apply off an argv it never closed. The positional check
    # comes FIRST: `--all codex --help` must refuse the name, not print help
    # rc 0 over it (a residual found on the first under-the-lock cut).
    names = [a for a in rest if not a.startswith("--")]
    if names:
        # `--all codex --apply` is not "just codex": the sweep has no per-seat
        # filter, and silently dropping the name would act on the FLEET under
        # an argv that reads as one seat (found in review)
        print("helm seat resume --all: takes no seat name (got %s); for one "
              "seat run `helm seat resume %s`" % (" ".join(names), names[0]),
              file=sys.stderr)
        return 2
    from .cli import guard_tail
    rc = guard_tail("helm seat resume --all", rest,
                    flags=("--all", "--apply"),
                    usage="seat resume --all [--apply]")
    if rc is not None:
        return rc
    apply = "--apply" in rest
    held, expired = hold_state()
    from . import harness, orcaadopt, seat, seats, sessions
    registered, blind = seat.registered_seats()
    if blind:
        print("helm seat resume --all: the seat tree is only partly readable "
              "— refusing to sweep a short list that reads as complete",
              file=sys.stderr)
        return 1
    ad = harness.detect()
    if ad is None or getattr(ad, "name", None) != "orca":
        print("helm seat resume --all: no orca metaharness detected — the "
              "sweep resolves orca panes and has nothing to do here",
              file=sys.stderr)
        return 1
    try:
        rows = ad.list()
    except Exception as e:                  # noqa: BLE001 — no inventory, no sweep
        print("helm seat resume --all: %s" % inventory_failure(e),
              file=sys.stderr)
        return 1
    roster, roster_failed = seats.roster_checked()
    adopted = [] if roster_failed else sorted(
        n for n in roster if n not in registered and seat._seat_family(n)[1])
    census = _memo(orcaadopt.claude_processes)
    held_sids = _memo(sessions.live_sids)
    boot = boot_epoch()
    this_boot = boot_id()
    pre = read_pre_boot(this_boot)

    print(HEADER)
    table, failed = [], 0
    pending = [(n, lambda n=n: classify_spawned(n, ad, rows, census, held_sids,
                                                 boot, apply))
               for n in sorted(registered)]
    pending += [(n, lambda n=n: _adopted_pair(n, ad, boot, roster.get(n),
                                              rows, apply))
                for n in adopted]
    for name, classify in pending:
        row, handle = classify()
        table.append(row)
        gate(row, apply, held, pre)
        print(row.line())
        if row.action != RESUMING:
            continue
        rc, captured = act(name, ad, row.session)
        row.action = "resumed" if rc == 0 else "resume FAILED rc=%d" % rc
        failed += rc != 0
        print("%14s -> %s" % ("", row.action))
        for line in captured.rstrip("\n").splitlines():
            print("%14s  | %s" % ("", line))
    if roster_failed:
        row = Row("(roster)", "adopted/orca", UNKNOWN,
                  reason="helm's chat roster could not be read; orca-adopted "
                         "seats were not enumerated")
        table.append(row)
        print(row.line())
    # THE VENDOR-DIALOG ESCAPE RIDES THIS WAKE. A seat parked at a limit dialog
    # is LIVE and so never appears in the relaunch half above — it has a
    # healthy process at a prompt that will wait forever. Reusing this sweep
    # keeps it on an existing timer instead of minting a second one, and it
    # honours --apply for the same reason every other act here does.
    from . import vendorescape
    for line in vendorescape.sweep(sorted(registered) + adopted, apply=apply):
        print(line)
    # THE CUBICLE MOVER RIDES THIS WAKE TOO (task/3900): each fleet seat's
    # tab moved to the owner's Orca pane its state names. It reuses this
    # adapter and this write flag, reads no /proc, and is silent until
    # local-names names a floor. Like a title, a move types nothing and
    # starts nothing, so the fleet hold does not withhold it.
    from . import cubicles
    for line in cubicles.tick(ad, apply=apply):
        print(line)
    counts = {}
    for row in table:
        counts[row.state] = counts.get(row.state, 0) + 1
    acted = sum(1 for r in table if r.action.startswith("resume"))
    stood_down = sum(1 for r in table if r.action == DOWN)
    print("helm seat resume --all: %s%s%s%s" % (
        ", ".join("%d %s" % (counts[k], k) for k in STATES if counts.get(k))
        or "no registered seats",
        "" if apply else " — DRY RUN, nothing written, nothing sent",
        "; %d resumed, %d failed" % (acted - failed, failed) if acted else "",
        "; %d desired-down, not relaunched" % stood_down if stood_down
        else ""))
    print(pre[2])
    # EVERY LIVE SEAT'S RECIPE, captured while it runs (task/3695): its argv
    # and allow-listed environ never change for the life of the process, so
    # the last capture before it parks or dies is exact. Only under --apply,
    # the one pass that writes, and never from a partial table. The pid comes
    # from the row's own proof (a spawned seat's register pin, an adopted
    # seat's resolved pids), so a healthy fleet still costs no /proc walk.
    recipes = None
    if apply and not roster_failed:
        from . import seat_recipe
        recipes = seat_recipe.carried(seat_recipe.capture_seats(
            [(r.seat, r.session, r.pids) for r in table if r.state == LIVE]))
    line, unrecorded = record_live_set(table, apply, roster_failed, this_boot,
                                       boot, recipes=recipes)
    print(line)
    if held:
        print("helm seat resume --all: FLEET HOLD in effect — no resume and no "
              "disarm write while %s exists" % hold_marker_path())
        return 0
    if expired:
        try:
            os.unlink(hold_marker_path())
            gone = "removed"
        except OSError as e:
            gone = "could not remove it: %s" % e
        print("helm seat resume --all: the fleet hold at %s predates this boot "
              "and EXPIRED with it — %s" % (hold_marker_path(), gone))
    if apply and (counts.get(UNKNOWN) or failed or unrecorded):
        return 1
    return 0


def _adopted_pair(name, ad, boot, roster_row, rows=(), apply=False):
    """classify_adopted in classify_spawned's (row, handle) shape: an adopted
    relaunch has no pane to reuse, so the handle is always None."""
    return classify_adopted(name, ad, boot, roster_row, rows, apply)[0], None
