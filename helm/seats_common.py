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

import errno
import hashlib
import os
import re
import time
import unicodedata

from . import chat, home, pk

MAX_BYTES = 200          # the delivery clip — meld's whisper frame budget
PREVIEW_CHARS = 80       # roster panel preview
SEAT_BYTES = 80          # the seat label clip — a name is a glance, not a payload
FRESH_S, QUIET_S = 120, 900
DEFAULT_TTL = 900        # claims lease default
STRICT_CLAIM_LOCK_WAIT_S = 2
STRICT_CLAIM_LOCK_POLL_S = 0.01
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
    is no name (home.SeatNameError => None), never a seat."""
    try:
        return home.chat_name()
    except Exception:
        return None
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
    return immediately, never consume the PostToolUse/Stop timeout budget."""

    def __init__(self, path, blocking=True):
        self.path, self.blocking, self.f = path, blocking, None

    def __enter__(self):
        try:
            import fcntl
            self.f = open(self.path, "a")
            flags = fcntl.LOCK_EX | (0 if self.blocking else fcntl.LOCK_NB)
            fcntl.flock(self.f.fileno(), flags)
        except OSError:
            if self.f is not None:
                self.f.close()
            self.f = None
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
_GONE = frozenset((errno.ENOENT, errno.ESRCH))
def process_sid_scan(sids, proc_dir=None):
    """({hits}, complete) — which of `sids` a live same-uid process references,
    AND whether the walk actually read everything it needed to say "no".

    A PID THAT EXITED AND A PID WE COULD NOT READ ARE DIFFERENT FACTS, and this
    loop used to `continue` on both under one comment ("unreadable/exited: no
    evidence, skip"). For a positive hit that conflation is harmless. For a
    DESTRUCTIVE ABSENCE it is the whole bug: an unreadable same-uid pid means a
    live process was never examined, so certifying that nothing holds the
    session is a claim the walk did not earn. @codex-2, meld producer shape C —
    reproduced with a readable proc root containing a numeric dir whose
    cmdline/environ could not be opened.

    `complete` is FALSE the moment any live pid goes unread. Callers wanting a
    positive answer only can ignore it; callers about to DELETE something may
    not."""
    want = {str(s): str(s).encode("utf-8") for s in sids if s}
    if not want:
        return set(), True
    proc_dir = proc_dir or home.env("PROC") or "/proc"
    hit, me, complete = set(), os.getuid(), True
    for pid in os.listdir(proc_dir):
        if not pid.isdigit() or len(hit) == len(want):
            continue
        d = os.path.join(proc_dir, pid)
        try:
            if os.stat(d).st_uid != me:
                continue
            blob, examined, denied = b"", False, False
            for leaf in ("cmdline", "environ"):
                try:
                    with open(os.path.join(d, leaf), "rb") as f:
                        blob += f.read(1 << 20)
                    examined = True
                except OSError as exc:
                    if exc.errno not in _GONE:
                        denied = True
            # ANY DENIED LEAF BLOCKS CERTIFYING ABSENCE, and my earlier
            # "neither leaf" line was unsound — @codex-2 case (C): a readable
            # cmdline with an EACCES environ that holds the ONLY copy of the
            # sid returns (set(), complete=True), and a claim gets DELETED on
            # the strength of a file we could not open.
            #
            # I CHOSE CONVENIENCE OVER SOUNDNESS THERE AND SAID SO AT THE TIME:
            # the measurement (582 same-uid leaf reads OK, 6 environ:EACCES
            # with cmdline readable) shows this makes `complete` False whenever
            # such a pid exists, which on this box is always. The honest
            # reading is that THIS SCAN CANNOT PROVE DEATH HERE — and the
            # feature's job is to make a stale release SAFE, not frequent. A
            # verdict of "unknown, not releasable" on a box whose /proc we
            # cannot fully read is the correct answer, not a degraded one.
            if denied:
                complete = False
        except OSError as exc:
            if exc.errno not in _GONE:
                complete = False
            continue
        for sid, needle in want.items():
            if sid not in hit and needle in blob:
                hit.add(sid)
    return hit, complete
def _sessions_with_a_process(sids, proc_dir=None):
    """The hits alone — the historical signature, for callers that only ever
    asked a POSITIVE question (the join path). Anything deciding an ABSENCE
    must use `process_sid_scan` and read its completeness."""
    return process_sid_scan(sids, proc_dir)[0]
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
