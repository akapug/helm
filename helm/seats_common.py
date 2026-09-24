#!/usr/bin/env python3
"""helm seats — the shared floor every seats module stands on.

WHAT DECIDES MEMBERSHIP HERE IS CLOSURE, NOT POPULARITY, and the difference
is not academic — it was measured. Seeding this module with the 22
most-referenced names in seats.py made the split WORSE: the module-level
cycle count went from 4 to 8, because five of those popular names
(`acting_seat`, `_cursor`, `seat_for_session`, `runtime_for_session`,
`nonpane_session`) call back into the very sections that import them. A hub
is not a floor. A floor is a set closed under its own dependencies.

So the members here are the transitive closure of a deliberately SMALL seed,
and that closure is only 37 bindings / 231 lines — it stops growing almost
immediately, which is the signal that the boundary is real. Everything here
answers a question that has no seats-specific context: how do I scrub a
string, clip it to a budget, take a lock, name a seat, read a pid's start
time, reach the roster file.

TWO MEMBERS ARE HERE BECAUSE THEY WERE MISFILED, not because they are
generic. `STATUS_BYTES` and `UNVERIFIED` were declared at L7503/L7439 of the
old single file and READ from L3567 onward — forward references that worked
only because a module-level constant resolves when the function RUNS, not
when it is defined. One file hid that; a split turns it into an ImportError
at boot. They are hoisted rather than seeded so the accident is deleted
instead of preserved.

`process_sid_scan` is here for the same reason in the other direction: it
sat in the roster section but is a generic /proc liveness primitive, and the
claims census needs it independently. Leaving it in roster would have made
claims import roster for one call.
"""

import collections
import errno
import hashlib
import json
import os
import re
import sys
import time
import unicodedata

from . import actors, chat, home, pk

MAX_BYTES = 200          # the delivery clip — meld's whisper frame budget
PREVIEW_CHARS = 80       # roster panel preview
SEAT_BYTES = 80          # the seat label clip — a name is a glance, not a payload
FRESH_S, QUIET_S = 120, 900
DEFAULT_TTL = 900        # claims lease default
# EVERY claims-lock caller waits this long, then refuses (task/3001): two
# periods of the gate's legacy renewer (gate._GATE_LEGACY_RENEW_S, 5s), which
# holds the lock for one refresh per period, so a live holder is waited for.
CLAIM_LOCK_WAIT_S = 10
CLAIM_LOCK_POLL_S = 0.01
# `--ttl`'s whole grammar, for every verb in helm that takes one. The bounded
# `[0-9]` run keeps `int()` inside its conversion limit and off other scripts'
# digits. A zero TTL is expired at birth, so it is refused like any typo.
_TTL_RE = re.compile(r"([0-9]{1,18})([smhd]?)")
_TTL_UNIT_S = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}
TTL_FORMS = ("a TTL is a whole number of seconds above zero, bare or with one "
             "unit suffix s, m, h or d: 3600, 3600s, 90m, 4h, 1d")


def ttl_flag(args, default):
    """(seconds, None) for the `--ttl` in argv `args` (`default` when absent),
    else (None, one refusal line). THE ONE --ttl READER: a bare `int()` raises
    on `4h`. The raw next token is judged, since `seats._flag` reads `-1` as
    absent; empty, missing, zero, signed, fractional or unknown-suffix values
    are refused, never defaulted or rounded, and so is a length past any
    instant this host can render. `repr` keeps the refusal on one line."""
    args = list(args or ())
    if "--ttl" not in args:
        return default, None
    i = args.index("--ttl")
    raw = args[i + 1] if i + 1 < len(args) else None
    shown = "with no value" if raw is None else repr(
        raw if len(raw) <= 40 else raw[:40] + "...")
    m = _TTL_RE.fullmatch(raw or "")
    secs = int(m.group(1)) * _TTL_UNIT_S[m.group(2)] if m else 0
    if secs <= 0:
        return None, "--ttl %s REFUSED — %s" % (shown, TTL_FORMS)
    try:
        time.gmtime(time.time() + secs)
    except (OverflowError, OSError, ValueError):
        return None, ("--ttl %s REFUSED — it ends past any instant this host "
                      "can express; %s" % (shown, TTL_FORMS))
    return secs, None

# WHAT COULD NOT BE READ, WITH ENOUGH TO ACT ON IT — and it lives HERE, at the
# floor every reader already imports, ON PURPOSE. Readers across this package
# answered a FAILURE with the value they use for an EMPTY: [] for an unreadable
# DM namespace, rows=[] for a lane holding an undecodable row, None for a
# malformed cursor, {} for a roster nothing could parse. Each one was cured
# separately, by hand, one door at a time, and the class kept reappearing one
# reader over — so the vocabulary goes at the shared floor where the NEXT
# reader cannot avoid finding it, rather than in whichever module noticed last.
#
# A BARE (value, failed) PAIR IS ENOUGH TO BRANCH ON AND NOT ENOUGH TO SAY. A
# renderer handed only a flag must rediscover which input failed, and will
# collapse them all into one generic UNKNOWN — the exact mislabel this package
# already shipped once, printing "lane unreadable" over a ROSTER failure and
# sending readers to a file that was fine. So a fault carries provenance:
#   kind   the acquisition class — dm-namespace | room-namespace | lane
#          | row | roster | cursor
#   where  the lane, path, recipient or session it is about
#   reason denied | missing | unreadable | undecodable | not-a-file | malformed
# One structured value rather than parallel booleans, whose correspondence is
# exactly what tears when someone adds the next reader.
_Fault = collections.namedtuple("_Fault", "kind where reason")
def _reason(exc):
    """An OSError -> the reason class a reader publishes. Named classes so a
    surface can say WHY without re-raising, and so 'denied' (a permission
    problem an operator can fix) never reads the same as a corrupt file."""
    return {PermissionError: "denied",
            IsADirectoryError: "not-a-file",
            NotADirectoryError: "not-a-file"}.get(type(exc), "unreadable")
SCAN_CAP = 512 * 1024    # deliver never reads more than this per room
ROOM_SCAN_CAP = 16       # rooms per boundary/beacon pass — the multi-room bound
BEACON_DRAIN_CAP = 4     # emitted wakes per --follow drain pass. An idle beacon
GUIDE_PATH = os.path.abspath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "docs",
    "NEW_AGENT_GUIDE.md"))
_BROADCAST = re.compile(r"(?<![A-Za-z0-9._-])@(all|fleet|everyone)(?![A-Za-z0-9._-])", re.I)
def own_name():
    """This PROCESS's own DECLARED identity ($HELM_CHAT_NAME through the one
    validated seam), or None when it declares none. The only answer to 'which
    seat am I?' that another process cannot supply for me — a session id is
    NOT it, because a session id can end up sitting in another seat's roster
    row (that is exactly the bug acting_seat exists to close). A hostile name
    is no name (home.SeatNameError => None), never a seat.

    A LIVE RENAME ALIAS RESOLVES TO THE ROW'S CURRENT KEY. A seat that was
    renamed while its process kept running still carries the OLD name in its
    environ, and nothing outside that process can rewrite it. Without an
    alias every identity door reads the inherited pin as a TAKEOVER of the
    row it was just renamed to (measured on a scratch seat: join, post and
    beacon-arm all refused "declares 'old' but session is rostered to 'new'").
    The rename records the old name as an alias with a window, and inside
    that window the declared old name IS the new seat, at the one seam every
    door reads. After the window the pin is a stranger again — the dispute
    returns, and the fix it names (re-export HELM_CHAT_NAME) is the right
    one. `declared_name` carries the alias evidence for doors that must say
    so out loud."""
    return declared_name()[0]


def declared_name():
    """(name, alias) — `own_name` plus its provenance: `alias` is None when
    the declared name is its own roster key (or has none), else the
    (raw, until) pair naming the live rename alias it was admitted through."""
    try:
        raw = home.chat_name()
    except Exception:
        return None, None
    if not raw:
        return None, None
    # ONE STAT PER CALL, NOT ONE PARSE: own_name rides every identity door,
    # and a live beacon asks it every poll. The answer is memoised on the
    # roster file's identity (path, mtime, size) so a rename or an expiry
    # written by another process is seen on the next call, and a fixture
    # that swaps HELM_HOME under the same declared name never reads a
    # stale answer through the cache.
    try:
        st = os.stat(roster_path())     # inode too: the roster is published
        stamp = (roster_path(), raw,     # by atomic replace, so two writes
                 st.st_ino, st.st_mtime_ns, st.st_size)   # inside one mtime
    except OSError:                      # tick still differ
        stamp = None
    hit = _DECLARED_CACHE.get(stamp) if stamp else None
    if hit is not None and (hit[1] is None or time.time() < hit[1][1]):
        return hit
    # A READ THAT FAILED IS NOT AN EMPTY ROSTER, and it is never memoised:
    # the fail-open reader collapses a transient EACCES/EIO to {}, and a
    # long-lived process that cached THAT answer would keep disputing its
    # own retained pin until the roster's identity changed — a chmod back
    # changes neither inode, mtime nor size (CL98). This read is
    # strict and direct; the roster() call-site census cannot see it.
    rows, failed = _roster_read_checked()
    if failed:
        return raw, None
    key, until = live_alias(raw, rows)
    out = (raw, None) if key is None else (key, (raw, until))
    if stamp:
        _DECLARED_CACHE.clear()
        _DECLARED_CACHE[stamp] = out
    return out


_DECLARED_CACHE = {}


def _roster_read_checked():
    """(rows, failed) — the roster, or ({}, True) for a read that FAILED as
    opposed to a file that is absent (a legitimate empty)."""
    try:
        got = pk.read_json(roster_path(), None, strict=True)
    except FileNotFoundError:
        return {}, False
    except Exception:                     # noqa: BLE001 — EACCES, EIO, junk
        return {}, True
    if got is None:
        return {}, False
    return (got, False) if isinstance(got, dict) else ({}, True)
def _scrub(s):
    """Meld's reader-side defense, ported: strip anything that could reshape
    the single-line label the content rides in — C0/C1 controls, format
    chars, line/paragraph separators. Tab survives."""
    return "".join(ch for ch in s if ch == "\t"
                   or unicodedata.category(ch) not in ("Cc", "Cf", "Zl", "Zp"))
def _clip(s, cap=MAX_BYTES):
    """Byte-budget clip on a codepoint boundary (meld's re-clip law)."""
    enc = s.encode("utf-8")
    if len(enc) <= cap:
        return s
    end = cap
    while end > 0 and (enc[end] & 0xC0) == 0x80:
        end -= 1
    return enc[:end].decode("utf-8", errors="ignore") + "…"
class _flocked:
    """flock a STABLE sibling lock file (never an atomic-replaced file).

    Shared-state mutations retain the historical blocking/fail-open behavior.
    Hot hook evidence passes `blocking=False`: contention is UNKNOWN and must
    return immediately, never consume the PostToolUse/Stop timeout budget.
    The CLAIMS lock never blocks here: `_claim_flocked` polls
    `blocking=False` against CLAIM_LOCK_WAIT_S and its callers refuse."""

    def __init__(self, path, blocking=True):
        self.path, self.blocking, self.f = path, blocking, None

    def __enter__(self):
        try:
            import fcntl
            self.f = open(self.path, "a")
            flags = fcntl.LOCK_EX | (0 if self.blocking else fcntl.LOCK_NB)
            from . import hooklatency
            hooklatency.flock(self.f.fileno(), flags, "lock-seats", fail_open=True)
        except OSError:
            if self.f is not None:
                self.f.close()
            self.f = None
        except BaseException:
            # A telemetry END may cancel after acquisition, before __enter__
            # returns. No body owns cleanup yet; close before propagating.
            if self.f is not None:
                self.f.close()
                self.f = None
            raise
        return self

    def __exit__(self, *exc):
        if self.f is not None:
            try:
                import fcntl
                fcntl.flock(self.f.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            self.f.close()
        return False
def roster_path():
    return os.path.join(chat.chat_dir(), ".roster.json")
def _seat_key(seat):
    """The seat's STATE-FILE key: readable slug + a short hash of the
    casefolded raw seat. pk.slug alone collides ('api.a' and 'api-a' both
    slug to 'api-a'), and a shared cursor lets one seat silently CONSUME the
    other's rows (codex B1 — loss, not a duplicate). Every per-seat state
    path (cursor, cursor lock, seen) derives from this one key; case-only
    variants fold together deliberately — case-insensitive @mentions cannot
    address them apart anyway."""
    import hashlib
    h = hashlib.blake2b(str(seat).casefold().encode("utf-8"),
                        digest_size=4).hexdigest()
    return "%s-%s" % (pk.slug(seat), h)
def roster():
    return pk.read_json(roster_path(), {}) or {}
def canonical_keys(seat, rows=None):
    """Every roster key that NAMES this identity — none, one, or repairably
    more.

    SEAT IDENTITY IS CASEFOLD-EXACT and the roster is the one place that fact
    has to be applied on the way IN as well as the way out. `write_roster`
    already resolves the unique casefold-equivalent key before it loads a row,
    and REFUSES a roster holding two; `recipient_matches` is the same relation
    for addressing. A READER that indexes the mapping with a raw spelling asks
    a different question and gets a silent miss, which reads exactly like an
    absent seat.

    IT RETURNS THE LIST, not a decision, because callers want different rules
    from it: a door that must load THE row needs exactly one and must refuse
    the ambiguous case, while a guard asking "is the name I claim already
    somebody's" is answered YES by any of them."""
    rows = roster() if rows is None else (rows or {})
    want = str(seat or "").casefold()
    return [k for k in rows if str(k).casefold() == want] if want else []


def canonical_seat(seat, rows=None):
    """(key, err) — the roster's OWN spelling for this identity, or None.

    err is set ONLY for the ambiguous roster, and it is not the same answer as
    absence: "no row names this seat" and "two rows do and I will not guess
    which" send an operator to different places, and collapsing them is how a
    repairable state becomes an invisible one. `write_roster` raises on this;
    a reader on a never-raise path gets the sentence instead and decides."""
    keys = canonical_keys(seat, rows)
    if not keys:
        return None, None
    if len(keys) > 1:
        return None, ("the roster holds %d case-variant rows for %r (%s) — "
                      "refusing to guess which one is the identity. Repair "
                      "with `helm chat seat rename` before reading."
                      % (len(keys), str(seat), ", ".join(sorted(keys))))
    return keys[0], None


RENAME_ALIAS_FIELD = "renamed"     # the rename EVENT on the renamed row:
                                   # {"old": name, "at": ts, "until": ts,
                                   #  "prior": [{"old": .., "until": ..}]}
RENAME_ALIAS_HOURS = 24.0          # default transition window


def rename_aliases(row, now=None):
    """[(old_name, until_epoch), ...] — every rename alias on this roster
    row that is still inside its window, latest hop first. The record is
    the rename event itself, not a second map; a second rename inside an
    open window carries the earlier hops forward under their ORIGINAL
    expiry (`prior`), so renaming again never silently cancels a window
    (task/2338)."""
    event = row.get(RENAME_ALIAS_FIELD) if isinstance(row, dict) else None
    if not isinstance(event, dict):
        return []
    at = time.time() if now is None else now
    out = []
    for hop in [event] + [p for p in event.get("prior") or ()
                          if isinstance(p, dict)]:
        old, until = hop.get("old"), pk.parse_ts_epoch(hop.get("until"))
        if isinstance(old, str) and old and until is not None and at < until:
            out.append((old, until))
    return out


def retire_alias_claims(rows, name):
    """Drop every rename-alias hop (latest AND prior, casefold) that claims
    `name`, on every row of `rows`, in place — called by the doors that
    ADMIT `name` as an exact key, inside the same locked roster publication.

    A FRESH ADMISSION RETIRES THE OLDER GENERATION'S CLAIM, durably. The
    exact-key rule already out-ranks a live hop while the exact row exists,
    but the hop itself stayed on the older row: rename the fresh row away
    again and the roster holds two live claims to the same name, decided by
    dict order. Neither the newest timestamp nor iteration order is an
    authority; admission is, and it is recorded here.
    Untouched hops keep their original expiry."""
    want = str(name or "").casefold()
    for key, row in list((rows or {}).items()):
        event = row.get(RENAME_ALIAS_FIELD) if isinstance(row, dict) else None
        if not isinstance(event, dict):
            continue
        hops = [h for h in [event] + [p for p in event.get("prior") or ()
                                       if isinstance(p, dict)]
                if str(h.get("old") or "").casefold() != want]
        if len(hops) == 1 + len(event.get("prior") or ()):
            continue                          # nothing on this row claims it
        if not hops:
            row.pop(RENAME_ALIAS_FIELD, None)
            continue
        head = dict(hops[0])
        head["prior"] = [{"old": h.get("old"), "until": h.get("until")}
                         for h in hops[1:]]
        head.setdefault("at", event.get("at"))
        row[RENAME_ALIAS_FIELD] = head


def rename_alias(row, now=None):
    """(old_name, until_epoch) of the LATEST live hop, else (None, None)."""
    hops = rename_aliases(row, now)
    return hops[0] if hops else (None, None)


def live_alias(name, rows=None, now=None):
    """(key, until_epoch) — the roster key whose LIVE rename alias `name`
    is, else (None, None). An exact roster key is never an alias: a name
    that names its own row is that row, whatever another row remembers.
    Casefold, like every seat-identity match."""
    want = str(name or "").casefold()
    if not want:
        return None, None
    rows = roster() if rows is None else (rows or {})
    if any(str(k).casefold() == want for k in rows):
        return None, None
    for key, row in rows.items():
        for old, until in rename_aliases(row, now):
            if old.casefold() == want:
                return key, until
    return None, None


def row_aliases(seat, rows=None, now=None):
    """[(old_name, until_epoch), ...] — the OLD names that still address
    `seat` through a live rename alias, latest hop first.

    THE REVERSE LOOKUP GOES THROUGH THE SAME RESOLVER AS THE FORWARD ONE.
    The rename record on the row is a claim; whether the old name is still
    this seat's alias is decided by `live_alias`, which refuses whenever
    that name has been re-admitted as its own row (write_roster lawfully
    mints a fresh incarnation under a retired name). Reading the record
    alone gave the old name to BOTH rows at once — the renamed seat woke
    on the new occupant's mentions and suppressed its posts as its own
    while the recipient door already selected the occupant (CL99).
    """
    row, _err = seat_row(seat, rows)
    out = []
    for old, until in rename_aliases(row, now):
        key, _until = live_alias(old, rows, now)
        if key is not None and str(key).casefold() == str(seat).casefold():
            out.append((old, until))
    return out


def row_alias(seat, rows=None, now=None):
    """(old_name, until_epoch) — the latest live hop of row_aliases."""
    hops = row_aliases(seat, rows, now)
    return hops[0] if hops else (None, None)


def alias_names(seat, rows=None, now=None):
    """The OLD names that still address `seat` through a live rename alias
    — [] for a seat never renamed, past its windows, or whose old names
    are seats again (row_aliases)."""
    return [old for old, _until in row_aliases(seat, rows, now)]


def alias_keys(name, rows=None, now=None):
    """[key] when `name` is a live rename alias of a roster row, else []."""
    key, _until = live_alias(name, rows, now)
    return [key] if key is not None else []


def names_match(value, names):
    """Exact canonical-token equality against a seat AND its live aliases —
    `recipient_matches` widened by exactly the alias list, nothing else."""
    return any(recipient_matches(value, n) for n in names or ())


def seat_row(seat, rows=None):
    """(row, err) — the row this identity owns under ANY spelling.

    THE ONE DOOR for reading a seat's roster row by a name that may not be
    spelled the way the roster spells it. A raw `roster().get(seat)` is the
    same question asked wrong, and its miss is indistinguishable from a seat
    that has never joined."""
    key, err = canonical_seat(seat, rows)
    if err or key is None:
        return None, err
    rows = roster() if rows is None else (rows or {})
    row = rows.get(key)
    return (row if isinstance(row, dict) else None), None


def roster_for_write():
    """The read a WHOLE-FILE roster writer must use.

    IT CAN RAISE, and the first draft of this line said it could not. roster()
    resolves roster_path() first, and under a relative HELM_HOME with the
    process cwd removed that raises FileNotFoundError — the exact hazard
    roster_checked was fixed for tonight. Every caller already meets it one
    line earlier, at `_flocked(roster_path() + ".lock")`, so nothing new
    reaches a caller; but "never raises" was a sentence about code I had not
    followed one call down, which is the failure this whole lane is about.

    PROBE-PROVEN, task/910: write alpha, write beta, corrupt the file, write a
    third seat, and the roster holds ONLY the third. Two live seats gone.
    roster() fail-opens to {} for UNPARSEABLE bytes exactly as for a missing
    file, and every roster writer follows its read with a pk.write_json of the
    WHOLE dict — so one transient parse failure does not lose A row, it
    OVERWRITES every row the reader could not parse. That is the whole of how
    a fleet of live seats becomes unlisted between one join and the next.

    SAME LAW AS process_sid_scan BELOW, which already states it for /proc: an
    absence we OBSERVED and an absence we could not READ are different facts,
    and for a DESTRUCTIVE absence the conflation is the entire bug. Tonight
    landed that law three times on readers. The writer never had it.

    IT IS ONE DOOR BECAUSE THERE ARE FIVE DESTROYERS. write_roster and the
    four admin verbs (disown_session, rename_seat, rehome_seat, set_mute) all
    read the whole roster and write the whole roster back. Curing the one that
    happened to be measured would leave four, so the rule lives at the
    acquisition every one of them already calls.

    WHAT IT DOES WITH BYTES IT CANNOT PARSE: preserves them beside the roster
    and says so LOUDLY. Not a refusal — a refusal leaves the unparseable file
    in place, so every later join refuses too and the whole fleet stays
    unlisted. Preserving turns silent total loss into a recoverable file and a
    message, which is the difference between a bug someone finds and a bug
    nobody can.

    AND IF PRESERVATION ITSELF FAILS, IT REFUSES (measured on
    the first cut). That version printed NOT PRESERVED and returned anyway, so
    the caller overwrote the bytes: no copy, no refusal, and an outcome
    bit-for-bit identical to the bug this exists to cure, differing only by a
    stderr line that a hook context swallows. The preserve-over-refuse
    argument above weighs a fleet re-join against bytes WE KEPT; it says
    nothing about bytes we are about to delete with no copy. So the one branch
    where the net breaks is the one branch that fails CLOSED — and a
    read-only or full filesystem is exactly when the replace fails AND nobody
    can recover by hand.

    THE DETECTION IS NARROW ON PURPOSE, and the first draft of this paragraph
    overstated it. It said only bytes that exist and will not PARSE can make
    the read empty; that is false — [], null, 0 and an empty file all read
    empty through pk.read_json's `or {}`. The true statement is that none of
    those carries a seat ROW, so overwriting them destroys nothing. What the
    detection isolates is bytes at risk, not bytes that failed. A missing file
    is genuinely absent, a parseable empty object is genuinely empty, and
    wrong-shaped-but-parseable JSON is returned INTACT and never looks empty —
    that last tolerance has to survive, because demanding roster_checked's
    stricter VALID of every write cost 142 failures and 63 errors when tried.

    ONE READ, not two. The first cut called roster() and then re-opened the
    file to classify it, which is two reads of one fact — the exact defect
    class this lane cures — and under a failed-open lock a repair landing
    between them reads as genuinely empty and gets overwritten. pk.read_json
    is open-plus-json.load with a bare except, so doing that read here loses
    nothing and closes the window.
    """
    path = roster_path()            # may raise; every caller already meets
    try:                            # this one line earlier, at the lock
        with pk.open_regular(path, encoding="utf-8") as f:
            value = json.load(f)
    except FileNotFoundError:
        return {}                   # proven absent: nothing to destroy
    except (OSError, ValueError):
        pass                        # bytes we cannot parse ARE rows at risk
    else:
        return value or {}          # exactly pk.read_json(path, {}) or {}
    keep = "%s.unreadable.%d" % (path, int(time.time()))
    try:
        os.replace(path, keep)
    except OSError as exc:
        raise OSError(
            "the roster could not be parsed AND its bytes could not be "
            "preserved (%s) — refusing a write that would delete the only "
            "copy" % type(exc).__name__)
    sys.stderr.write(
        "helm: the roster could not be PARSED and this write would have "
        "overwritten every row in it. The unparseable bytes are preserved at "
        "%s — they may be truncated or partial, so treat them as evidence "
        "rather than a backup. Every seat must re-join.\n" % keep)
    return {}
_GONE = frozenset((errno.ENOENT, errno.ESRCH))
PROC_DEFAULT = "/proc"
# THE FOUR READINGS OF ONE PROCESS TABLE, and only the first certifies an
# absence. They were one boolean, and the boolean could not say WHICH of them
# happened: a table whose root could not even be listed and a table walked end
# to end with one ssh-agent's environ closed to us both read `False`, and the
# refusal printed "nothing was looked at" over a walk that read hundreds of
# processes. Both are correctly NOT proof of death. Only one of them is
# "nothing was looked at".
SCAN_WHOLE = "whole"          # root listed, every same-uid pid read or gone
SCAN_PARTIAL = "partial"      # root listed and walked, some pids unreadable
SCAN_UNLISTED = "unlisted"    # the root itself could not be listed
SCAN_RAISED = "raised"        # the scan died before it could answer
def _errname(exc):
    return errno.errorcode.get(getattr(exc, "errno", None)) \
        or type(exc).__name__
def proc_root(proc_dir=None):
    """(root, source) — the table a process scan walks, and WHICH INPUT named
    it. A refusal that blames HELM_PROC while HELM_PROC is unset sends the
    reader to a variable that is not there; the value still comes from
    home.env, only its label is derived here."""
    if proc_dir:
        return proc_dir, "the caller"
    value = home.env("PROC")
    if value:
        return value, ("HELM_PROC" if os.environ.get("HELM_PROC") is not None
                       else "MELD_PROC")
    return PROC_DEFAULT, "the default root"
def process_sid_reading(sids, proc_dir=None):
    """Which of `sids` a live same-uid process references, AND what the walk
    managed to read -> {"hits": {sid: pid}, "state", "root", "source",
    "walked", "unread", "error"}.

    A PID THAT EXITED AND A PID WE COULD NOT READ ARE DIFFERENT FACTS. For a
    positive hit the difference is harmless. For a DESTRUCTIVE ABSENCE it is
    the whole question: an unreadable same-uid pid is a live process that was
    never examined, so certifying that nothing holds the session is a claim
    the walk did not earn. Only ENOENT/ESRCH — the pid left mid-walk — is
    genuine absence.

    ANY DENIED LEAF BLOCKS CERTIFYING ABSENCE, not only a pid with both
    leaves denied: a readable cmdline beside an EACCES environ can hide the
    ONLY copy of the sid. Every descendant of a session inherits
    CLAUDE_CODE_SESSION_ID, so an ssh-agent started from one of its tool
    calls carries the sid in exactly the leaf a non-dumpable process closes
    to us.

    THE STATE SAYS WHICH INPUT FAILED; `unread` SAYS WHERE. SCAN_PARTIAL is
    still EVIDENCE — every hit in it is a process we read, so a positive stands
    — but its negative is bounded by `unread`, and it can never prove a death.
    On an ordinary desktop it is the common reading: systemd --user, ssh-agent
    and sandboxed browser renderers are same-uid and non-dumpable, so their
    environ answers EACCES to their own user. `unread` rows are
    (pid, comm, leaf, errno name); `comm` is world-readable, which is what
    makes the row actionable, and is None when even that failed.

    `hits` maps each found sid to the FIRST pid seen carrying it, so a verdict
    of live can name its evidence — including when that pid is the very
    command asking, which is what a sid typed into a one-liner's argv finds.

    Never raises for an unlistable root: that is SCAN_UNLISTED with `error`
    naming the errno, because a reader handed an exception has to rediscover
    which of the four readings it was."""
    want = {str(s): str(s).encode("utf-8") for s in sids if s}
    root, source = proc_root(proc_dir)
    out = {"hits": {}, "state": SCAN_WHOLE, "root": root, "source": source,
           "walked": 0, "unread": [], "error": None}
    if not want:
        return out
    try:
        names = os.listdir(root)
    except OSError as exc:
        out.update(state=SCAN_UNLISTED, error=_errname(exc))
        return out
    hit, unread, me = out["hits"], out["unread"], os.getuid()
    for pid in names:
        if not pid.isdigit() or len(hit) == len(want):
            continue
        d = os.path.join(root, pid)
        try:
            if os.stat(d).st_uid != me:
                continue
        except OSError as exc:
            if exc.errno not in _GONE:
                unread.append((pid, None, "stat", _errname(exc)))
            continue
        out["walked"] += 1
        blob, denied = b"", []
        for leaf in ("cmdline", "environ"):
            try:
                with open(os.path.join(d, leaf), "rb") as f:
                    blob += f.read(1 << 20)
            except OSError as exc:
                if exc.errno not in _GONE:
                    denied.append((leaf, _errname(exc)))
        if denied:
            try:
                with open(os.path.join(d, "comm"), "rb") as f:
                    comm = f.read(64).decode("utf-8", "replace").strip()
            except OSError:
                comm = None
            unread.extend((pid, comm, leaf, err) for leaf, err in denied)
        for sid, needle in want.items():
            if sid not in hit and needle in blob:
                hit[sid] = int(pid)
    if unread:
        out["state"] = SCAN_PARTIAL
    return out
def process_sid_scan(sids, proc_dir=None):
    """({hits}, complete) — the historical pair over `process_sid_reading`.

    `complete` is True ONLY for SCAN_WHOLE: a partial walk, an unlistable root
    and a scan that died are all False, exactly as before. What changed is that
    an unlistable root now answers (set(), False) instead of raising out of a
    bare os.listdir — both callers already turned the raise into that same
    pair. Callers that must SAY why a negative is not a proof read the
    reading itself."""
    got = process_sid_reading(sids, proc_dir)
    return set(got["hits"]), got["state"] == SCAN_WHOLE
def _sessions_with_a_process(sids, proc_dir=None):
    """The hits alone — the historical signature, for callers that only ever
    asked a POSITIVE question (the join path). Anything deciding an ABSENCE
    must use `process_sid_scan` and read its completeness."""
    return process_sid_scan(sids, proc_dir)[0]
def raised_reading(exc):
    """The reading of a scan that died before it could answer."""
    return {"hits": {}, "state": SCAN_RAISED, "root": None, "source": None,
            "walked": 0, "unread": [], "error": type(exc).__name__}
def _some(items, cap=4):
    """At most `cap` items, then how many were left out — a refusal naming 11
    pids is noise, one naming none is unactionable."""
    items = [str(i) for i in items]
    more = len(items) - cap
    return ", ".join(items[:cap]) + (" and %d more" % more if more > 0 else "")
# What each claude-census flag MEANS, in the words a refusal prints — the
# producer's own docstring definitions, shortened.
_CENSUS_GAP_PHRASES = (
    ("listing_failed", "the /proc listing failed"),
    ("census_partial", "a process could not be probed far enough to tell "
                       "whether it is claude"),
    ("who_failed", "the `helm who` rung was never probed"))
def census_gaps(census, loose):
    """One phrase per census input that is set, plus the live rows nobody
    could attribute (`loose`). A census that is INCOMPLETE is usually not an
    UNREADABLE one — the common gap is a live claude process whose session
    nobody can name, which may be the very holder being asked about."""
    out = [phrase for key, phrase in _CENSUS_GAP_PHRASES if census.get(key)]
    if loose:
        out.append("%d live claude process%s with no attributable session "
                   "(pid %s)" % (len(loose), "es"[:2 * (len(loose) != 1)],
                                 _some(r.get("pid") if isinstance(r, dict)
                                       else None for r in loose)))
    return out
def scan_refusal(scan):
    """Why a process scan cannot certify an absence, naming WHICH of its
    readings happened and never a cause it did not measure.

    A RUNG MUST SAY WHICH OF ITS INPUTS FAILED. On an ordinary desktop with
    HELM_PROC unset the walk lists /proc, reads every claude process, and is
    PARTIAL only because a handful of same-uid non-dumpable processes
    (ssh-agent, systemd --user, sandboxed renderers) close their environ to
    their own user. "HELM_PROC unreadable, so nothing was looked at" is false
    there in every clause except the verdict — and the verdict is the part
    that must not change: a partial walk cannot prove a death."""
    scan = scan or {}
    state = scan.get("state")
    where = "%s (%s)" % (scan.get("root"), scan.get("source"))
    if state == SCAN_UNLISTED:
        return ("the process table at %s could not be LISTED (%s), so "
                "nothing was looked at — an absence nobody measured is not a "
                "death" % (where, scan.get("error")))
    if state == SCAN_PARTIAL:
        unread = scan.get("unread") or []
        return ("no process that could be read carries it, but the walk of %s "
                "was PARTIAL: %d of %d same-uid processes could not be fully "
                "read (%s), and any of them may hold its only copy — an "
                "absence the walk could not finish is not a death"
                % (where, len({u[0] for u in unread}), scan.get("walked") or 0,
                   _some("pid %s %s %s:%s" % (pid, comm or "?", leaf, err)
                         for pid, comm, leaf, err in unread)))
    if state == SCAN_RAISED:
        return ("the process scan raised %s before it could answer, so "
                "nothing it saw can be trusted — an absence nobody measured "
                "is not a death" % scan.get("error"))
    return ("the process scan reported itself incomplete without saying why "
            "— an absence it could not certify is not a death")
def _evidence_pid(scan, session):
    """Name the pid a live verdict rests on. A sid typed into a one-liner's
    argv is found in that one-liner — the scan cannot tell the question from a
    holder — so when the pid is the asking process or its parent, say so. Only
    against the real /proc: a fixture root's pids are not ours to compare."""
    pid = ((scan or {}).get("hits") or {}).get(session)
    if pid is None:
        return ""
    if os.path.realpath(scan.get("root") or "") == PROC_DEFAULT \
            and pid in (os.getpid(), os.getppid()):
        return (" (pid %d, which is the command asking or its parent — if "
                "the sid appears only in what was typed, that hit is the "
                "question, not a holder)" % pid)
    return " (pid %d)" % pid
def dm_lane(seat):
    """The seat's own private DM lane, as a reserved-namespace room name."""
    return chat.DM_PREFIX + _seat_key(seat)
_SEAT_TOKEN = re.compile(r"[A-Za-z0-9._-]{1,64}\Z")   # what an @mention can say
class _CanonicalRecipient(str):
    """Canonical routing key plus the spelling an operator supplied or rostered."""

    def __new__(cls, value, display=None):
        out = str.__new__(cls, value)
        out.display = str(value if display is None else display)
        return out
def _canonical_recipient(value, display=None):
    """One exact recipient identity: unconditional @-strip + casefold.

    Routing and durable state use the string value. `display` is deliberately
    separate metadata, so roster/original casing can render without shaping
    identity. A typed value preserves its display spelling across owner layers.
    """
    shown = getattr(value, "display", None) if display is None else display
    value = str(value or "").strip().lstrip("@")
    shown = value if shown is None else str(shown).strip().lstrip("@")
    if not _SEAT_TOKEN.fullmatch(value):
        return None, "canonical recipient must be an exact seat token"
    if not _SEAT_TOKEN.fullmatch(shown):
        shown = value
    return _CanonicalRecipient(value.casefold(), shown), None
def recipient_matches(left, right):
    """Exact canonical-token equality — never substring or slug equality."""
    left, lerr = _canonical_recipient(left)
    right, rerr = _canonical_recipient(right)
    return lerr is None and rerr is None and left == right
def status_write_refused(werr, wreason, asserted, target, actor_of):
    """Must this status annotation be REFUSED rather than written anonymously?

    AN UNATTRIBUTABLE ANNOTATION IS REFUSED, NOT WRITTEN ANONYMOUSLY:
    discarding the resolver's error writes with `by=None`, dropping
    `status_by` — the one field saying who annotated another seat — so rc is 0
    and the result is indistinguishable from a SELF write, which is the
    confusion that field exists to end.

    TWO CASES ARE EXCLUDED AND EACH IS EXACTLY ONE CASE WIDE.

    THE DERIVED FLOOR (UNRESOLVED) is the env-less operator this CLI has
    always served, who was never going to carry provenance.

    THE SELF WRITE is exempt only at UNCORROBORATED. Spelling your own row is
    the same operation as a bare `status <line>`: `acting_seat` answers the
    same name for both and `set_status` omits `status_by` either way, so
    typing the target buys no attribution and refusing it only loses a call
    that already works unspelled. Every OTHER refusal still fires on a
    self-named target, and two of them must. DISPUTED is the inherited-name
    incident: there "my own row" IS the contested claim, so an exemption keyed
    on that assertion would exempt the dispute from itself. UNAVAILABLE is an
    unreadable actor store, which never fails open at a write door. MALFORMED
    and MISASSERTED are the same shape. IDENTITY EQUALITY IS NOT ACTOR
    ADMISSION.

    SAMENESS IS THE CASEFOLD RELATION PRODUCTION USES, not string equality:
    `write_roster` keeps ONE canonical key per casefold-equivalent name, so
    `Selfie` and `selfie` are one identity.

    `actor_of` is a THUNK, not a value, and the laziness is load-bearing: the
    acting identity is read ONLY on the UNCORROBORATED tier, and that tier
    INCLUDES ONE REFUSAL — uncorroborated with a target that is not self,
    the exact case this predicate exists to refuse. Five of the six
    refusal reasons still cost no identity resolution at all. A caller
    passing an already computed name would make the read eager on every
    one of them, which is why this takes a callable."""
    if not werr or not asserted or wreason == actors.UNRESOLVED:
        return False
    return not (wreason == actors.UNCORROBORATED
                and recipient_matches(target, actor_of()))
def _proc_stat_link(pid, proc_dir="/proc", with_state=False):
    """(starttime, ppid) — plus state when requested — else None.

    `comm` is process-controlled and may contain spaces or `)`, so fields are
    counted only after the final close-paren. One parser owns both the
    delegation census and the PostToolUse holder ancestry."""
    if not isinstance(pid, int) or pid <= 0:
        return None
    try:
        with open(os.path.join(proc_dir, str(pid), "stat"), "rb") as f:
            tail = f.read(512).rpartition(b")")[2].split()
        if len(tail) <= 19 or not tail[1].isdigit() or not tail[19].isdigit():
            return None
        link = int(tail[19]), int(tail[1])
        return link + (tail[0].decode("ascii"),) if with_state else link
    except (OSError, UnicodeDecodeError, ValueError):
        return None
def _get_pid_starttime(pid, proc_dir="/proc"):
    """Return Linux process starttime (field 22 in /proc/<pid>/stat) or None."""
    link = _proc_stat_link(pid, proc_dir=proc_dir)
    return link[0] if link else None
def _get_live_pid_starttime(pid, proc_dir="/proc"):
    """Return an exact executable generation; terminal rows are not live."""
    link = _proc_stat_link(pid, proc_dir=proc_dir, with_state=True)
    return link[0] if link and link[2] not in ("Z", "X") else None
def claims_path():
    return os.path.join(chat.chat_dir(), ".claims.json")
def _now_mono():
    return time.monotonic()
class _claim_flocked:
    """THE claims lock, with a bounded wait for EVERY caller (task/3001).

    It polls a non-blocking take until `wait` (CLAIM_LOCK_WAIT_S) passes. If
    the wait ends without the lock, `.f` is None and the caller REFUSES,
    naming the holder with `_lock_unavailable`. No caller proceeds without
    the lock: a stopped holder that resumes writes its stale snapshot over
    anything written meanwhile, and a lost update is worse than a refusal.
    `wait=0` is one take, for a caller whose write is optional. `create_dir`
    makes a missing ledger directory first, as the non-strict doors' own
    write always did; a strict door refuses it by name instead."""
    def __init__(self, *, wait=None, create_dir=False):
        self.wait = CLAIM_LOCK_WAIT_S if wait is None else wait
        self.create_dir, self.lock, self.f = create_dir, None, None

    def __enter__(self):
        path = claims_path() + ".lock"
        if self.create_dir:
            try:
                chat._ensure_dir()
            except OSError:
                pass   # the loop below refuses it, naming the missing directory
        # time.monotonic, not _now_mono: tests freeze _now_mono to age leases,
        # and a frozen clock would make this bounded wait endless.
        deadline = time.monotonic() + self.wait
        while True:
            lock = _flocked(path, blocking=False)
            lock.__enter__()
            if lock.f is not None:
                self.lock, self.f = lock, lock.f
                return self
            lock.__exit__(None, None, None)
            # A lock whose directory is gone can never be opened, so waiting
            # out the bound would only delay the refusal that says so.
            if time.monotonic() >= deadline \
                    or not os.path.isdir(os.path.dirname(path)):
                return self
            time.sleep(CLAIM_LOCK_POLL_S)

    def __exit__(self, *exc):
        if self.lock is not None:
            return self.lock.__exit__(*exc)
        return False
def _sweep(c):
    now = _now_mono()
    return {r: v for r, v in c.items()
            if r == "_fence" or (isinstance(v, dict)
                                 and v.get("exp_mono", 0) > now)}
UNVERIFIED = "unverified"
STATUS_BYTES = 160   # the explicit one-liner stays a glance, never a post
def _seat_label(s):
    """Launder a seat KEY for terminal display (the `seat gc` listing and
    `status` bare-show read the roster dict directly, not roster_report).
    A legit seat — [A-Za-z0-9._-], a slug, or auto_name — is unchanged; a
    hostile HELM_CHAT_NAME (the seat key is unvalidated at the join seam)
    loses its ESC/bidi so the most prominent, first-printed column cannot
    reshape the operator's terminal. The report path routes through
    _pub_row; this is the same law for the two direct-read surfaces."""
    return _clip(_scrub(str(s)).strip(), SEAT_BYTES)
class UnresolvedRepo:
    """A repository a caller NAMED and FAILED to resolve — never an omission.

    `repo` IS THE GRANT'S PROVENANCE, AND IT IS RECORDED BECAUSE A RESOURCE
    NAME IS NOT INJECTIVE (task/2437 round three, the seventh root). A lane
    resource is `worktree:<repo-root-basename>:<lane>`, so a project's checkout
    and a fork of it kept elsewhere mint the SAME key — the claims ledger
    recorded who holds a lease and for how long, and nothing at all about WHICH
    repository the lease was taken out for. `dispatches.progress_state` then had
    only the room on disk to ask, and a room OUTLIVES the grant that cut it: an
    unlanded `helm work release` keeps the room and the worktree lock, so the
    next reader saw a directory and called it work in progress. The repository
    belongs in the GRANT, beside the nonce and the holder, written and swept as
    one record — a fact about the lease rather than a fact about a directory.

    Callers that supply nothing leave it None, which readers must treat as
    UNKNOWN provenance rather than as a match (it is what every grant minted
    before this field carries). An authenticated EXTEND of the same lease keeps
    the binding it already had when the extender names no repository: the lease
    id is the grant's identity, so carrying its provenance across its own
    extension states nothing new, while a supplied repository MOVES it — which
    is exactly what a seat re-claiming the same lane from a different checkout
    is doing.

    OMITTED AND UNRESOLVED ARE DIFFERENT FACTS, AND ONE VALUE CANNOT CARRY
    BOTH (task/2437). `claim(repo=None)` means "I am saying nothing about
    provenance", and the documented answer to that on an authenticated
    extension is to KEEP the binding the grant already has — a lease's identity
    is its nonce, so re-stating its repository adds nothing. This value means "I
    tried to establish which repository this lease is for and FAILED", and
    inheriting there converts a failed observation into a positive assertion
    about a repository nobody just looked at — which every overdue surface reads
    as WORKING, silencing a real obligation.

    A CALLER THAT TRIED AND FAILED PASSES `UnresolvedRepo`, NOT None. An
    EXTENSION carrying it clears the binding to UNKNOWN and REFUSES rather than
    inherit an authority nobody just confirmed; a FRESH grant records UNKNOWN,
    having no prior authority to inherit and no reading for refusing new lane
    work over a transient git failure.

    MEASURED SHAPE: two checkouts of the same basename share one lane resource
    (`worktree:<basename>:<lane>`). A seat holding A under nonce N re-claims
    lane L in B with `--lease N`; B's gitdir lookup fails transiently while the
    worktree lookup and the flock both succeed. With a None there the extension
    landed and the grant still said A. `work/_claims._grant_repo` is the
    producer that mints this, and `seats_claims.claim` is the door that parts
    the two answers.

    THE INSTANCE CARRIES ITS OWN EVIDENCE — the path that would not resolve and
    why — because a refusal the caller cannot act on gets re-run verbatim. It is
    deliberately NOT a str subclass: no accidental truthiness test (`if repo:`)
    or `str(repo)` can smuggle it into the ledger as a repository identity.

    IT LIVES HERE, beside `claims_path` and `_sweep`, because it is claim-record
    VOCABULARY rather than claim mechanism: the producer is in `helm/work` and
    the consumer is `seats_claims`, so a home in either one would make the other
    import across the split.
    """
    __slots__ = ("path", "why")

    def __init__(self, path, why=None):
        self.path = str(path)
        self.why = str(why) if why else "unresolvable"

    def __repr__(self):
        return "UnresolvedRepo(%r, %r)" % (self.path, self.why)


def _unresolved_repo_refusal(resource, repo):
    """Why an authenticated extension is refused rather than inherited.

    THE HOLDER IS NOT BEING ACCUSED OF ANYTHING — their binding authenticated.
    The refusal exists because this call was the only party that could confirm
    which repository the extended lease is for, it could not, and the record it
    would otherwise leave standing is read by `dispatches.progress_state` as
    PROOF that that repository's rows are being worked. So the sentence names
    the path, the reason, and both repairs: retry (a transient git failure is
    the common case), or release and re-claim, which mints a fresh grant whose
    provenance this checkout can actually prove.
    """
    return ("%s: the repository this lease would be extended for could not be "
            "established (%s: %s). An extension must not inherit the grant's "
            "recorded repository from a failed observation, because the overdue "
            "surfaces read that record as proof that repository's rows are "
            "being worked — so this grant's provenance is now UNKNOWN and "
            "suppresses nothing. Retry the check-in, or `helm work release` "
            "this lane and claim it again from this checkout."
            % (resource, repo.path, repo.why))
