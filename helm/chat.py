#!/usr/bin/env python3
"""helm chat — the human-included groupchat: one shared conv log + notify +
read/write loop, owner in the room.

Rooms live in RAM (tmpfs): /dev/shm/helm-chat/<room>.jsonl — append-only, one
JSON object per line {"ts","from","text"}, dir 0700, default room "main"
(HELM_CHAT_ROOM re-homes a seat's default — team-room homing).
HELM_CHAT_DIR overrides (tests point it at a tmp dir). This is ephemeral
presence-chat, NOT the durable record — /premise anything that must outlive
the room; past SIZE_CAP the oldest half rotates out (RAM etiquette).

v2 — THE SIGNED TRANSPORT (PRD CHAT_V2, premises a2a-ram-only-disk-log-after
+ comms-presets-optimize-their-novel-purity): when the chat ROOM NODE (a
dregg node whose data-dir lives on tmpfs — chatnode.py supervises it)
answers, every post also rides a signed self-write turn on the poster's cell
there: the turn payload carries the message digest ("chat:b2b:<blake2b-256>",
73 B inside the whisper frame budget), the RAM room carries the text — thin
claim, fat corroboration, exactly the premise-attestation pattern. The
message row gains {turn, receipt, chain}; renderers show signed rows clean
and tag everything else "[unsigned]". Node/sign/faucet failure -> the v1 path
automatically (fallback law: drop the signature, never the RAM property), BUT
NEVER QUIETLY: the fallback row carries structured `transport:{state,profile,
code,reason,first_failure,last_failure,...}` and a per-profile tmpfs incident
keeps every status surface DEGRADED until that profile completes a signed turn
(`/dev/shm/helm-chat-failures/<room-key>` when CHAT_DIR is not itself tmpfs).
SIGNING IS OPT-IN: it needs the explicit cell binary
(HELM_CELL_BIN — unset => off, no PATH probe; the de-meld law), so configured-
off chat is UNSIGNED BY DEFAULT, not a failure. A set-but-unusable signer is
configured-on and stamps `signer_unavailable`. Join/token caches follow
HELM_CHAT_DIR (RAM at the production default; an override owns their storage
location), while failure state is always forced onto tmpfs. Transport is
NODE-AGNOSTIC (HELM_CHAT_NODE_URL, else node-state url, else :8898) — the node
migration just repoints it.

The log-after leg remains the ONLY bulk transcript writer: `helm chat
log-flush` appends delivered history OUT-OF-BAND to
<helm-home>/helm/journal/chat-<date>.log, idempotent via a per-room high-water
mark, disable with HELM_CHAT_LOG=0. Never called from send/read. The narrow
exception is a caller-keyed machine post: its operation receipt is committed
after the RAM append and before rotation under a durable bus-keyed
chat-event-receipts root (`HELM_CHAT_EVENT_DIR` overrides), so a lost
acknowledgement stays deduplicable across reboot without forcing an unrelated
transcript flush.

Emojis (PRD addendum): shortcodes expand at post time on every surface
(:fire: -> 🔥, emoji.py), reactions ride the same transport as typed rows
{react, tts, tfrom} rendered inline under their target. Reacting is a TOGGLE
per (reactor, emoji, target): the second identical react appends a tombstone
row ({un: true}) instead of a duplicate, and every renderer aggregates
last-row-wins per reactor — so historical duplicate rows self-heal to one on
read. The reactor's identity rides the signed digest (react|tts|tfrom|emoji|
reactor) when a signer is available; unsigned reacts still land, tagged. A
REACTION ADDITION WAKES ONLY THE TARGET ROW'S AUTHOR (`tfrom`, mention tier in
any room); self-reactions and toggle-off tombstones stay silent. The shared
delivery drain coalesces a contiguous same-target burst into one wake line for
both active boundaries and idle beacons, with target identity first and an
honest omitted-count plus the exact room/DM pull command if every reactor/emoji
cannot fit the frame budget.

The notify loop: the owner's post (web panel or `helm --human`) drops
<room>.owner-unread (the message count at post time); the shipped
owner-chat-unread reflex fires on that marker every turn until a
`helm chat read` consumes past it — identical in BOTH transports.

DMs (premise exact-token-addressee-match): `helm chat dm <seat> <text>` (and
`post --dm <seat>`) is a TRUE 1:1 — the row rides the recipient's private
lane <chat-dir>/dm/<seat-key>.jsonl (the `dm-` room-name prefix is that
lane's reserved namespace; room_path routes it, list_rooms never shows it),
stamped {dm: <recipient>} so only the EXACT-token recipient's beacon/boundary
delivers it. No room ever sees it; it renders as a DM, not a room row; it
signs like any post.

Replies (PRD CHAT_REPLY): a row may carry {reply_to} — the PARENT ROW'S
STABLE ID (chat._append's id law, reused; no second identity is invented) —
plus {rts, rfrom}, the parent's (ts, from). A parent predating the id law also
carries {rtext}: ts|from is not unique when one author posts twice inside a
second, so exact text disambiguates without inventing an identity. One level
only, the predecessor chat's style: a reply renders with a compact quote of its parent,
a parent renders its reply count, and an orphan parent (rotated out) renders
as such — never a crash. A REPLY WAKES ITS PARENT'S AUTHOR
(seats.deliverable reads rfrom): replying is a direct address, the same tier
as an @mention — the owner's stated reason for replies was "I'm tired of
typing agent names to mention", so a reply that woke nobody delivered the
mechanism while dropping its purpose (2026-07-22; inverted the original
threading-is-invisible law). Only the parent's author wakes — a reply stays
quieter than the mention it replaces.

Signed replies bind the parent: the payload is a DISTINCT algorithm tag
("chat:reply:b2b:") over \\x1e-joined, injectively escaped parent fields +
text (a pre-id parent includes rtext) — a separate tag, never an in-band
prefix on the plain-post payload,
because the text is attacker-chosen and an in-band prefix would let a plain
post whose text reads `reply|<id>|hi` mint a reply's digest (a free
re-parenting forgery). Plain posts stay BYTE-IDENTICAL to v2 — every row
already on disk recomputes exactly as before. payload_for() is the ONE
shape-dispatching recomputer (post/react/reply); `helm chat verify`
re-derives it and compares against the `payload` the signed row records.
HONEST SCOPE: helm cannot yet ask the node what payload a turn carried
(cell.verify_anchor — "payload binding unavailable"), so verify proves the
row's SELF-consistency (naive re-parenting or text edits are caught) and the
turn hash is what will bind it remotely once dregg discloses payloads.

The owner's orca pane sidecar is exactly: helm chat read --follow
"""
import bisect
import contextlib
import hashlib
import itertools
import tempfile
import json
import math
import os
import posixpath
import re
import shlex
import stat
import sys
import threading
import time
import unicodedata

from . import freetext, home, pk, projscope
from .verdicts import POLARITY_FLAGS
# cell + emoji import lazily inside the paths that use them — the delivery
# lane's PostToolUse hook rides this module on EVERY tool call fleet-wide,
# and its fast path must pay interpreter+import cost for nothing it won't use.

DEFAULT_DIR = "/dev/shm/helm-chat"
SIZE_CAP = 2 * 1024 * 1024  # per-room rotation threshold — RAM etiquette
POLL_S = 2.0                # --follow poll cadence (the web panel matches)
CHAT_TAG = "chat:b2b:"      # algorithm-tagged digest, premise.py's pattern
REPLY_TAG = "chat:reply:b2b:"   # DISJOINT payload space for parent-bound rows
VERDICT_TAG = "chat:verdict:b2b:"  # DISJOINT space for dispatch-verdict turns
REACT_TAG = "chat:react:b2b:"   # ditto for reactions (v2 shared the post tag)
CHAT_TOPIC = "helm.chat"    # the signed turn's event topic on the room node
DM_PREFIX = "dm-"           # reserved room-name namespace: the private lanes
FIELD_SEP = "\x1e"          # ASCII RS: fields cannot be slid into one another
QUOTE_CHARS = 72            # the quoted parent's snippet budget (one line)
SIGN_FAILURES_FILE = ".sign-failures.json"  # RAM-only per-profile owner truth
SIGN_FAILURES_ROOT = "/dev/shm/helm-chat-failures"
_SIGN_FAILURE_FALLBACK = {}  # owner path -> exact raw profile -> private state

# THE SLICE RUNNER'S DATA AUDIT (helm/gateslice.py) reports any module
# data a test unit leaves behind; these names are process-wide by design.
_GATESLICE_MUTABLE = {
    "_SIGN_FAILURE_FALLBACK": (
        "keyed by owner path, and every chat dir a test plants is its own "
        "key"),
}
_SIGN_FAILURE_FALLBACK_LOCK = threading.RLock()
_QUOTED_VALUE = r'''(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')'''
_UNCLOSED_QUOTED_VALUE = r'''(?:"[^\r\n;}]*|'[^\r\n;}]*')'''
_AUTH_VALUE = re.compile(
    r'''(?ix)["']?authorization["']?\s*[:=]\s*(?:''' +
    _QUOTED_VALUE + r'''|''' + _UNCLOSED_QUOTED_VALUE + r'''|[^\r\n;}]+)''')
_SECRET_VALUE = re.compile(
    # `jwt` earns its place the hard way: a probe of this lane found a
    # complete JWT surviving into the kept tail. The key list matched `token`
    # but never the bare spelling `jwt`, so `jwt=eyJ...` was never redacted at
    # all — main only appeared safe because its head-cut happened to land before
    # it, and main leaks the same token when it appears EARLY (measured). So
    # this is a pre-existing hole that tail-preservation exposed, not one it
    # created, and redacting by KEY fixes both positions instead of making the
    # tail a special case.
    r'''(?ix)(?:["']?)(bearer[_-]?token|access[_-]?token|refresh[_-]?token|'''
    r'''id[_-]?token|api[_-]?key|client[_-]?secret|token|jwt|passphrase|'''
    r'''password|secret|credentials?)(?:["']?)(?:\s*[:=]\s*|\s+)(?:''' +
    _QUOTED_VALUE + r'''|''' + _UNCLOSED_QUOTED_VALUE +
    r'''|[^\s,;}\]]+)''')
_AUTH_SCHEME = re.compile(
    r'''(?ix)\b(?:bearer|basic)\s+(?:''' + _QUOTED_VALUE + r'''|''' +
    _UNCLOSED_QUOTED_VALUE + r'''|[^\r\n,;}]+)''')
_URL_USERINFO = re.compile(r"(https?://)[^/@\s]+@", re.I)
_ACK_REMEDIATION = ("if this profile was retired or renamed: "
                    "helm chat transport ack --profile <name>")

_REMEDIATION = {
    "node_unreachable": "restore the chat node, then send one signed turn as this profile",
    "node_probe_failed": "inspect `helm chat node status`, restore the probe, then retry",
    "signer_unavailable": "restore HELM_CELL_BIN to an executable signer, then retry",
    "signer_launch_failed": "repair the configured signer executable, then retry",
    "signing_timeout": "inspect this profile's node receipts before retrying; the timed-out operation may already have committed",
    "send_outcome_unknown": "inspect this profile's node receipts before retrying; the send may already have committed",
    "faucet_source_dry": "refuel the cell the room node's faucet pays out of (`helm chat node refuel`), then retry; the node refused this send for its fee and the faucet could not top it up — it never mints",
    "join_failed": "repair this profile's room-node join, then retry",
    "send_failed": "inspect `helm chat node status` and this profile's balance, then retry",
    "signing_exception": "inspect `helm doctor` and the named exception, then retry",
    "identity_conflict": "relaunch this seat through `helm launch` so its signing profile is its own, then retry",
    "identity_unreadable": "repair the seat roster (`helm doctor`), then retry; until it reads, a seat cannot prove its signing profile is its own",
    "incident_state_unreadable": "repair incident-state ownership/permissions/space, then retry; transport ack cannot retire an unreadable owner",
}


_ID_FIELDS = ("from", "tfrom", "rfrom", "dm")   # the NAME-carrying row fields


def _dsan(s):
    """DISPLAY-launder an IDENTITY string (a from/tfrom/rfrom/dm name) for a
    text OR JSON sink: strip C0/C1 controls, Unicode format chars (bidi
    overrides included), and line/paragraph separators, so a row's name column
    can never reshape a terminal or reorder a rendered line. Defense-in-depth
    BENEATH the validated join seam (home.chat_name already rejects a hostile
    HELM_CHAT_NAME at the source) — a legit name is unchanged; this catches any
    row whose name was planted OUTSIDE the seam (a foreign/pre-fix jsonl row).
    Message TEXT is deliberately NOT touched — it legitimately carries unicode
    (voice pastes, emoji, U+2028); only names are laundered."""
    if not isinstance(s, str):
        return s
    return "".join(ch for ch in s if ch == "\t"
                   or unicodedata.category(ch) not in ("Cc", "Cf", "Zl", "Zp"))


def _public_transport(value):
    """Copy one transport projection with signer identities display-laundered.
    The incident map remains keyed by the exact raw profile; only emitted row,
    status, and JSON copies pass through this boundary."""
    if not isinstance(value, dict):
        return value
    out = dict(value)
    if isinstance(out.get("profile"), str):
        out["profile"] = _dsan(out["profile"])
    if isinstance(out.get("reason"), str):
        out["reason"] = _safe_reason(out["reason"])
    if isinstance(out.get("failed_profiles"), list):
        out["failed_profiles"] = [_public_transport(v)
                                  for v in out["failed_profiles"]]
    return out


def public_rows(rows):
    """Copies of `rows` with every identity display-laundered — the shape the
    web /api/chat wire and any JSON sink emits. The stored rows keep raw NAME
    fields for reaction/reply matching; transport.profile is an emitted signer
    identity and is laundered too."""
    out = []
    for m in rows:
        c = dict(m)
        for k in _ID_FIELDS:
            if isinstance(c.get(k), str):
                c[k] = _dsan(c[k])
        if "transport" in c:
            c["transport"] = _public_transport(c["transport"])
        out.append(c)
    return out


def chat_dir():
    """HELM_CHAT_DIR else the RAM room dir — derived via home.surface_dir.

    A redirected HELM_HOME pulls the WHOLE chat surface (rooms, DMs, roster,
    claims) under itself: isolation that skips the identity surface is not
    isolation (#117 — an isolated-looking probe read, and could write, the
    live fleet roster)."""
    return home.surface_dir("CHAT_DIR", "helm-chat", DEFAULT_DIR)


STATE_SUBDIR = "state"
STATE_FAMILIES = ("deleg",)


def state_path(family, name):
    """A non-room file's NEW home: chat_dir()/state/<name>.

    THE DM SUBDIR, APPLIED TO EXHAUST. `room_path` already parks DM lanes in
    `dm/` so they are "invisible to list_rooms... while every reader/cursor/
    rotation mechanic composes unchanged". Cursors, locks and delegation
    markers want the same treatment for the same reason, and they outnumber
    the rooms by two orders of magnitude.

    WHY THE FLAT DIRECTORY IS THE COST. `list_rooms` getdents the room
    directory, and `seats._scan_rooms` calls it on EVERY TOOL BOUNDARY and
    every beacon poll, so each non-room entry is paid for by every seat on
    every tool call. MEASURED 2026-08-26 on this host: 29,235 entries of
    which 222 are rooms — 99.2pct of every scan is state no room listing
    wants. The files are tiny; the DIRECTORY ENTRY is the cost.

    This is NOT a reap and deletes nothing. Every one of those files is live
    and load-bearing: `dead_cursors` finds none dead, and a cursor removed
    from under a tracked seat re-delivers its room from offset 0. The
    population is a by-design cross product of live identities x rooms, so
    the only honest fix is to move it out of the scan, not to shrink it.

    ONE SUBDIR PER FAMILY, not one shared bucket. Every family has its own
    prefix-scanning reader — the delegation rung listdirs for
    `.deleg_*`, delivery listdirs for cursors — so a single `state/` would
    move the 29k scan rather than remove it, and each reader would still pay
    for the other families. Family subdirs make every scanner see only its
    own population."""
    return os.path.join(state_family_dir(family), name)


def state_family_dir(family):
    """The directory a single exhaust family lives in."""
    return os.path.join(chat_dir(), STATE_SUBDIR, family)


def state_scan_dirs(family):
    """(new, legacy) roots a prefix-scanning reader must walk during compat.

    Readers scan BOTH because a beacon runs the code it armed with: an
    un-relaunched writer keeps producing into the flat directory while new
    writers produce into the family subdir, and a reader that sees only one
    of them reports a FALSE NEGATIVE — for the delegation rung that means
    telling a seat to delegate work it has already delegated. The legacy leg
    retires when the relaunch wave completes, not on a timer."""
    return (state_family_dir(family), chat_dir())


def state_read_path(family, name):
    """Read a non-room file NEW-FIRST, then LEGACY, for the compat window.

    Writers moved to `state_path` in one release; the flat-directory copies
    survive until every seat has relaunched, because a beacon executes the
    code it armed with and an old writer keeps writing flat. Returning the
    legacy path when the new one is absent is what keeps those two writers
    readable by one reader. The fallback retires a release later, with the
    one-shot migration as the backstop for stragglers."""
    new = state_path(family, name)
    if os.path.exists(new):
        return new
    return os.path.join(chat_dir(), name)


def room_path(room="main"):
    """Room name -> its RAM file. The `dm-` prefix is the DM namespace: those
    lanes live in the dm/ subdir, invisible to list_rooms (no room fanout, no
    web channel row, no default log-flush) while every reader/cursor/rotation
    mechanic composes unchanged."""
    r = pk.slug(room)
    if r.startswith(DM_PREFIX):
        return os.path.join(chat_dir(), "dm", r[len(DM_PREFIX):] + ".jsonl")
    return os.path.join(chat_dir(), r + ".jsonl")


def empty_room_line(room, label):
    """What to say when a read found nothing — AND THAT IS THREE FACTS.

    A room that exists and holds nothing, a name with NO LIVE ROOM, and a room
    whose file could not be READ all render zero rows, and one sentence for
    the three of them is a confident report about a world the reader never
    reached. Three reads of `dm:seat-b` answer "no messages" while three DMs
    sit delivered in its identity-keyed lane, and a reader who trusts the
    sentence concludes that messages it can see nothing of were never sent.

    AND NONE OF THE THREE IS "NOTHING WAS EVER POSTED". Rooms live in RAM and
    history lives in the journal, which the first list or post restores after
    a reboot or a wipe, so an absent file is a fact about THIS HOST RIGHT NOW
    and is silent about the past. The claim this line makes is bounded to the
    stat it performed and the restore state it can prove.

    THE DM CASE IS NOT A TYPO, IT IS A SHAPE THAT CANNOT EXIST. `pk.slug`
    folds `dm:seat-b` to `dm-seat-b`, which IS in the DM namespace, so the
    reader resolves a real path — `dm/seat-b.jsonl` — that nothing will ever
    write, because `dm_room` keys a lane by seat identity and appends a
    digest. So the honest answer names the lane that does exist. DM lanes stay
    out of `list_rooms` by design (no fanout, no web row, no default
    log-flush), which is exactly why a reader cannot find this by browsing and
    has to be told here.
    """
    state, detail = _live_room_state(room)
    if state in ("has", "empty"):
        return ("helm chat [%s]: no messages — post one: helm chat post "
                "<text>%s" % (label, " (or: helm chat dm <seat> <text>)"
                              if label == "dm" else ""))
    if state == "unreadable":
        return ("helm chat [%s]: CANNOT SAY — the room file for %s could not "
                "be read (%s), so whether anything was posted here is "
                "UNKNOWN, which is a third answer and not the empty one."
                % (label, pk.slug(room), detail))
    hint = _dm_lane_hint(room)
    # HISTORY LIVES IN THE JOURNAL AND THE BUS LIVES IN RAM, so an absent
    # file is silent about what was EVER posted: a reboot or a wipe leaves
    # the journal holding rows that the first list or post restores. The
    # restore sentinel is the cheap instrument -- one stat, and the slow
    # journal scan stays where it belongs.
    #
    # AND ITS ABSENCE PROVES UNPROVEN, NOT UNRESTORED. The restore can
    # succeed and the sentinel write then fail or be interrupted, so a
    # missing stamp means only that nothing here can show the restore
    # happened. Saying it HAS NOT been restored asserts a fact about the past
    # from an instrument that only reports on its own bookkeeping -- the same
    # overclaim, one level up, that this whole line exists to end.
    restorable = True
    try:
        restorable = not os.path.exists(_restored_sentinel())
    except Exception:                        # noqa: BLE001 — an unanswerable
        restorable = True                    # sentinel means UNKNOWN, so keep
                                             # the weaker sentence
    return ("helm chat [%s]: NO LIVE ROOM — nothing is posted to %s on this "
            "host right now, so this is not an empty room.%s%s"
            % (label, pk.slug(room),
               " This host cannot prove the journal was restored since it "
               "came up, so history under this name may exist and return on "
               "the next list or post." if restorable else "", hint))


def _live_room_state(room):
    """('has'|'empty'|'absent'|'unreadable', detail) for a room's RAM file.

    `os.path.exists` ANSWERS FALSE FOR TWO DIFFERENT WORLDS: the file is not
    there, and the question could not be asked -- an unstatable parent, a
    permission wall, a broken mount. Only the first is absence, and a caller
    that treats the second as absence publishes a confident report about a
    world it never reached. Size separates the last pair: a zero-byte lane
    EXISTS and holds nothing, which is not the same as holding messages."""
    try:
        st = os.stat(room_path(room))
    except FileNotFoundError:
        return "absent", ""
    except OSError as exc:
        return "unreadable", (exc.strerror or str(exc))
    if st.st_size <= 0:
        return "empty", ""
    # SIZE IS NOT A SUCCESSFUL PARSE. A torn or malformed lane holds bytes and
    # yields ZERO rows, so a pointer that promises messages on the strength of
    # a byte count sends a reader to a file that shows them nothing.
    #
    # AND ZERO ROWS FROM `read` IS NOT AN EMPTY LANE. `read` fails open by
    # contract -- an open that fails after this stat and a lane of torn lines
    # both come back as ([], 0), the same answer an empty lane gives -- so
    # counting through it turned a false HAS into a false EMPTY. The stricter
    # door reports WHY an answer is short. Rows that did parse are messages
    # whatever else is torn; no rows plus a fault is UNREADABLE.
    try:
        rows, _total, fault = read_checked(pk.slug(room))
    except Exception as exc:                 # noqa: BLE001 — a lane that
        return "unreadable", str(exc) or type(exc).__name__  # cannot be read
    if rows:
        return "has", ""
    if fault:
        return "unreadable", "the lane holds %d bytes that yield no rows (%s)" \
            % (st.st_size, fault)
    # NO ROWS, NO FAULT, NONZERO SIZE AT STAT: either the bytes are only line
    # breaks (a lane that holds nothing) or the file went away between the stat
    # and the open, which `read_checked` reports as a proven-empty room. Ask
    # the stat again rather than let a removal read as an empty lane.
    try:
        os.stat(room_path(room))
    except FileNotFoundError:
        return "absent", ""
    except OSError as exc:
        return "unreadable", (exc.strerror or str(exc))
    return "empty", ""


def _dm_lane_hint(room):
    """The pointer to the lane a DM-shaped name MEANT, or "" when there is
    none to give.

    THE SEAT NAME IS TAKEN OFF THE RAW STRING, because `pk.slug` is where the
    identity dies: `api.a` and `api-a` are DIFFERENT seats with different
    `_seat_key` digests and different lanes, and both slug to `api-a`. A hint
    computed from the slug sends a reader to the wrong recipient's lane and
    reads as authoritative. A rename redirect is followed for the same
    reason -- the lane that HAS the messages is the one the redirect names."""
    raw = str(room or "")
    if not pk.slug(raw).startswith(DM_PREFIX):
        return ""
    tail = re.match(r"(?i)^dm[-:_/](.+)$", raw)
    if not tail:
        return ""
    try:
        real = dm_room(tail.group(1))
        real = _dm_redirect_target(real)
    except Exception:                        # noqa: BLE001 — a name that
        return ""                            # resolves to nothing is the
                                             # ordinary case, not a fault
    if not real or pk.slug(real) == pk.slug(raw):
        return ""
    state, _detail = _live_room_state(real)
    if state == "has":
        return (" A DM lane is keyed by seat identity, not by the name you "
                "type: %s is the lane for that seat and it HAS messages. "
                "Read it with --room %s." % (real, real))
    if state == "empty":
        return (" A DM lane is keyed by seat identity, not by the name you "
                "type: %s is the lane for that seat. It exists and is empty, "
                "so nothing is hiding there." % real)
    if state == "unreadable":
        return (" A DM lane is keyed by seat identity, not by the name you "
                "type: %s is the lane for that seat, and it could not be read "
                "(%s), so whether anything is there is UNKNOWN." % (real, _detail))
    return ""


def dm_room(to):
    """The recipient's private lane, keyed by exact-token seat identity."""
    from . import seats
    return DM_PREFIX + seats._seat_key(to)


def _dm_redirect_path(room):
    return os.path.join(chat_dir(), ".dm-redirect.%s" % pk.slug(room))


def _dm_redirect(room):
    """Return a valid redirect, proven absence, or raise for UNKNOWN storage."""
    try:
        with open(_dm_redirect_path(room), encoding="utf-8") as f:
            raw = f.read().strip()
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError) as exc:
        raise OSError("DM redirect is unreadable") from exc
    target = pk.slug(raw)
    if not raw or target != raw or not target.startswith(DM_PREFIX) \
            or target == pk.slug(room):
        raise OSError("DM redirect is malformed")
    return target


class _DMRedirect(Exception):
    pass


def _dm_redirect_target(room):
    """Resolve a DM lane before deriving room-bound semantics."""
    from .seats_common import ROOM_SCAN_CAP

    target, seen = room, set()
    for _hop in range(ROOM_SCAN_CAP):
        canonical = pk.slug(target)
        if not canonical.startswith(DM_PREFIX):
            return target
        if canonical in seen:
            raise OSError("DM redirect cycle")
        seen.add(canonical)
        moved = _dm_redirect(target)
        if moved is None:
            return target
        target = moved
    raise OSError("DM redirect depth exceeds bounded scan")


def _dm_parent_fields(room, ref):
    """Resolve reply metadata against a recovered, rename-stable DM lane."""
    from .seats_common import _flocked, roster_path
    from .seats_rename import recover_seat_rename

    with _flocked(roster_path() + ".lock"):
        if not recover_seat_rename(roster_locked=True):
            raise OSError("interrupted seat rename is not recoverable")
        return _parent_fields(_dm_redirect_target(room), ref)


def list_rooms():
    """Every room that has a RAM file, sorted (the '<room>.jsonl' basenames).
    One source for the CLI `rooms` verb, log-flush's all-rooms default, and the
    web channel list — a room is born the first time anything posts to it.

    First touch after a reboot AUTO-RESTORES the journal: rooms live on
    tmpfs and die with the machine, and the 2026-08-02 reboot measured the
    alternative — nobody ran restore-journal for ~20 minutes, so the fleet
    read an EMPTY bus as ground truth and verdicted off archive history
    (replay-reads-as-live). The latch is one-time per boot per helm-home,
    best-effort: a failed restore is reported by the restore itself, never
    by breaking every chat verb that lists rooms.

    ONE GETDENTS PER PASS, AND THE LIFETIME IS THE WHOLE POINT. A room list is
    a fact about NOW — a room is born the first time anything posts to it — so
    a durable or process-lifetime cache here would make a new room invisible
    for as long as the process lives, which is the stale-surface class helm
    has paid for twice. `projscope.memo` is dropped when the pass that opened
    the scope ends and is INERT when no scope is open, so this is byte-identical
    to the bare scan for every caller that has not declared a pass.

    WHAT IT COSTS WHEN IT IS NOT MEMOISED, measured on the live bus: the chat
    directory is FLAT and holds 98,518 entries, of which 50,058 are `.lock`
    and only 450 are rooms — so one call reads ninety-eight thousand dirents
    to answer a question about four hundred. `helm chat seats` asked it 39
    times in one render, through `roster_report` -> `_pending_all` ->
    `_scan_rooms`, once per roster row, for 2.8s of a 4.2s screen: ONE
    distinct question. The cost is unbounded in the LOCK count rather than the
    room count, which is the part no caller can see from the signature.

    SO THE DIRECTORY IS BOUNDED, NOT INDEXED (helm/chatdebris.py). An index
    would be a second answer to which rooms exist, and a writer that missed it
    would hide a room from delivery. Instead gc reaps the sibling locks and
    the cursors of sessions that are over, `helm chat retire-rooms` archives
    idle meld rooms with their cursors, and `helm doctor` warns, naming both,
    once the entries per room climb past the budget again.

    THE RETURNED LIST IS A COPY, never the memoised value. Callers own their
    result — `seats_mute` builds a set from it, `chat` binds it to a local —
    and handing two callers in one pass the same mutable list would make one
    caller's edit the other's input. The tuple is what is cached; each caller
    gets its own list of it.
    """
    _auto_restore_once()
    d = chat_dir()
    if not os.path.isdir(d):
        return []
    return list(projscope.memo(
        ("chat.list_rooms", d),
        lambda: tuple(sorted(n[:-6] for n in os.listdir(d)
                             if n.endswith(".jsonl")))))


_AUTO_RESTORE_RUNNING = False


def _restored_sentinel():
    """chat_dir()/.restored-this-boot — boot-scoped state on boot-scoped
    storage. TMPFS dies at boot (exactly when the latch must re-arm) and the
    file survives room ROTATION (exactly what a marker ROW cannot — the
    marker is posted once at boot, always among the oldest rows, and
    rotation evicts the oldest half first: F3, the decaying fast
    path). The F1 lesson is not 'no sentinels', it is 'never on persistent
    disk' — this one lives where the rooms live."""
    return os.path.join(chat_dir(), ".restored-this-boot")


def _restore_pending():
    """Unrestored journal history, keyed on the cutover marker: any journal
    record whose (ts, body) matches NO live row in its room is history the
    bus has not seen since the wipe. The discharge is the dedupe itself:
    restored rows re-render to exactly their journal bodies, so a restored
    room's records all match and the predicate goes False. This is the SLOW
    path, called only when the tmpfs sentinel is absent — a steady-state
    bus pays one stat(), never this scan (F3: keying the fast path on
    the marker ROW decayed — the marker is always among the oldest rows and
    rotation evicts the oldest half first, so within a day or two every
    busy room would have paid the full journal scan per post, and worse,
    the rotated-out old rows would have read as pending and triggered a
    restore-rotate-restore oscillator that re-delivers day-old traffic to
    every beacon as fresh)."""
    records, state, _meta = _journal_records()
    if state != "ok":
        return False
    for room, recs in records.items():
        rows, _ = read(room)
        have = set()
        for r in rows:
            have.add((r.get("ts"), _fmt_body(r)))
            have.add((r.get("ts"), "%s: %s" % (r.get("from"), r.get("text"))))
        if any((ts, body) not in have for ts, body in recs):
            return True
    return False


def _auto_restore_once():
    """First-writer-restores latch: restore at most once per unrestored
    state, from EITHER side of the bus — the first LIST (an agent reading
    an empty room as ground truth) or the first POST (a timer speaking into
    the void — proxywatch posted row [1] at 23:30Z tonight while every
    agent was down, and an empty-dir-keyed latch would have read that as
    'live traffic' and disarmed for the boot). The merge is dedupe-driven
    and proven safe on a LIVE bus (tonight's manual restore kept the one
    live row, zero losses), so there is no empty-dir clause at all. Fail-
    open by law: any exception returns silently so listing/posting never
    breaks."""
    global _AUTO_RESTORE_RUNNING
    if _AUTO_RESTORE_RUNNING:
        return                          # re-entrant call from inside restore
    try:
        if os.path.exists(_restored_sentinel()):
            return                      # one stat() on the steady-state path
        _AUTO_RESTORE_RUNNING = True
        try:
            if not _restore_pending():
                # nothing to restore (fresh host, or a bus whose journal
                # fully dedupes): stamp the sentinel so the journal scan is
                # paid at most once per boot, not once per list/post
                try:
                    os.makedirs(chat_dir(), exist_ok=True)
                    with open(_restored_sentinel(), "w"):
                        pass
                except OSError:
                    pass
                return
            restore_journal(apply=True)
            try:
                with open(_restored_sentinel(), "w"):
                    pass
            except OSError:
                pass
        finally:
            _AUTO_RESTORE_RUNNING = False
    except Exception:
        return


def _waiter_cursor_sessions(live):
    """Sessions whose cursor is still owned by a live/uncertain inbox waiter.

    A compaction may rename the harness session while the already-armed waiter
    keeps running with its launch-time session. Beacon classification owns that
    process/session lifetime distinction; cursor GC consumes its verdict instead
    of independently calling the old session dead. UNKNOWN keeps state, exactly
    as it keeps the waiter itself. Only GHOST/GONE relinquish the cursor."""
    from . import beacons
    rows = beacons.entries()
    if not rows:
        return set()
    records = beacons.holder_records()
    keep = set()
    for row in rows:
        state = beacons.classify(
            row["pid"], row.get("seat"), row=row, live=live, records=records)
        sid = state.get("session")
        if state.get("state") in (beacons.LIVE, beacons.UNKNOWN) \
                and isinstance(sid, str) and sid:
            keep.add(sid)
    return keep


def dead_cursors(live=None, waiters=None):
    """Per-session read cursors whose SESSION is dead. (victim_paths, kept, err)

    IDENTIFIES ONLY — `helm gc` owns the deletion, via the same _reap every other
    retention stream goes through. Two actuators for one kind of file is how you
    get two policies, two log formats, and a divergence nobody notices.

    THE LIFETIME MISMATCH THIS FIXES. A cursor is created per (room, seat,
    SESSION) — `<room>.cursor.<seat>-<hash>.<sid>` — but the only reaper,
    `seats._unlink_seat_state`, is keyed on the SEAT. So a seat that survives a
    hundred sessions accumulates a hundred cursors and nothing ever collects
    them. Measured on the live fleet 2026-07-25: 24,349 cursor files across 520
    sessions of which NINE were alive — 24,052 files, 99%, owned by the dead.

    Why that costs real latency rather than just disk: `list_rooms` scans the
    whole directory to find 26 rooms among 25,094 entries, a 965:1 noise ratio,
    and `seats._scan_rooms` calls it on EVERY TOOL BOUNDARY and every beacon
    poll. 85% of a delivery pass was getdents64. The files are tiny; the
    DIRECTORY ENTRY is the cost.

    LIVENESS BEFORE AGE, and never the reverse: a cursor is dropped only when its
    session is provably not running. Current harness sessions come from
    `sessions.live_sids()`; launch-time sessions still owned by an armed waiter
    come from the beacon process verdict. A compaction may rename the former
    while the latter keeps running, so either one retains the cursor. Age cannot
    answer this — a pane that has been thinking for an hour looks exactly like
    one that exited an hour ago, and dropping a LIVE session's cursor makes that
    seat re-read its whole room and re-deliver everything it already saw. If
    liveness cannot be established at all we keep EVERYTHING and report the
    error: an unknown session is not a dead one.

    Dry-run by default, like every other helm gc.
    """
    d = chat_dir()
    if not os.path.isdir(d):
        return [], 0, None
    if live is None:
        try:
            from . import sessions
            held = sessions.live_sids()
            live = set(held)
            waiters = _waiter_cursor_sessions(
                held if isinstance(held, dict) else dict.fromkeys(live))
        except Exception as e:            # cannot prove liveness -> touch nothing
            return [], 0, "liveness unavailable (%s) — kept everything" % e
    live = set(live)
    live.update(waiters or ())
    # NORMALISE BOTH SIDES TO THE SAME 8 CHARS, then compare exactly. Filenames
    # do not agree on a sid length — measured on the live dir: 7,480 cursors
    # carry an 8-char sid, 3,613 carry 23, with a tail out to 34 — and
    # `live_sids` returns full uuids. An earlier version truncated only the LIVE
    # side and then tried `p.startswith(sid)`, which can never be true for a
    # 23-char sid against an 8-char prefix: a live session holding a long-form
    # cursor would have been read as DEAD and reaped. It happened to be safe
    # today only because no live session had one, which is luck, not a design.
    live_pfx = frozenset(str(s)[:8] for s in live if s)
    victims, kept = [], 0
    try:
        names = os.listdir(d)
    except OSError as e:
        return [], 0, str(e)
    for n in names:
        if ".cursor." not in n:
            continue
        # Locks and atomic-write temporaries are not cursor records. A bare tail
        # is the seat-level admission authority, not a session suffix; deleting
        # it removes the only baseline a replacement waiter can inherit.
        if n.endswith((".lock", ".tmp")):
            continue
        tail = n.rsplit(".cursor.", 1)[1]
        if "." not in tail:
            kept += 1
            continue
        sid = tail.rsplit(".", 1)[-1]
        if not sid or sid[:8] in live_pfx:
            kept += 1
            continue
        victims.append(os.path.join(d, n))
        lk = os.path.join(d, n + ".lock")
        if os.path.exists(lk):
            victims.append(lk)          # the lock is half the directory entries
    return victims, kept, None


def _reap_for_test(live=None, waiters=None):
    """Test-only actuator. Production deletion belongs to `helm gc` — this
    exists so the reap LOGIC can be pinned without importing the gc plane."""
    victims, kept, err = dead_cursors(live=live, waiters=waiters)
    if err:
        return [], kept, err
    for v in victims:
        try:
            os.remove(v)
        except OSError:
            pass
    return victims, kept, None


def marker_path(room="main"):
    return os.path.join(chat_dir(), pk.slug(room) + ".owner-unread")


def _ensure_dir():
    d = chat_dir()
    os.makedirs(d, mode=0o700, exist_ok=True)
    os.chmod(d, 0o700)  # presence-chat is the operator's — never group-readable
    # the exhaust subdir is minted with the surface, not lazily at first write:
    # a writer that has to create it races every other writer doing the same.
    for fam in STATE_FAMILIES:
        os.makedirs(os.path.join(d, STATE_SUBDIR, fam), mode=0o700,
                    exist_ok=True)
    return d


def whoname():
    """$HELM_CHAT_NAME, else a session-derived AGENT name — never the operator's
    identity. The unix login is the operator's machine account (e.g. the machine
    login); an agent CLI post that fell through to it impersonated the owner in
    the room. The operator's own surfaces name themselves explicitly (the owner's
    surfaces set HELM_CHAT_NAME; `helm --human` sets it), so a bare CLI post is
    ALWAYS an agent — it gets an agent name, never the login.
    A session already in the roster answers with its SEAT name (posts and
    deliveries speak one name — the rename verb rebinds both); an unknown
    session gets seats.auto_name's meaningful project+family name, and the
    opaque agent-<sid8> hex survives only as the fail-open floor."""
    name = home.chat_name()   # THE validated seam: a hostile name is REJECTED
    if name:                  # here (home.SeatNameError), never posted as `from`
        try:                  # read-only: WARN on a declared-vs-rostered
            from . import seats   # dispute (2026-08-02), never block a read
            seats._warn_disagreement(home.session_id(), "whoname")
            # a live rename alias speaks as the row it names, like every
            # other identity door (seats_common.own_name)
            return seats.own_name() or name
        except Exception:
            pass
        return name
    sid = home.session_id()
    if sid:
        try:
            from . import seats
            return seats.seat_for_session(sid) \
                or seats.auto_name(sid, seats.safe_cwd())
        except Exception:
            return "agent-" + sid[:8]
    return "agent"


# ---------------------------------------------------------------------------
# v2 transport: the signed leg on the room node (RAM-pure — the only writes
# outside the node are tmpfs-side caches in the room dir itself)
# ---------------------------------------------------------------------------

def node_url():
    """The room node. HELM_CHAT_NODE_URL wins (SET-BUT-EMPTY disables the
    signed transport entirely — the hermetic-test/ops kill switch), else the
    node-state file's url (how the node migration repoints chat), else :8898."""
    v = home.env("CHAT_NODE_URL")
    if v is not None:
        return v.rstrip("/") or None
    from . import chatnode
    return (chatnode.state().get("url") or chatnode.default_url()).rstrip("/")


def cells_path():
    """RAM-side join cache {profile: cell_hex} — in the room dir, NOT the
    disk-backed _global/.state. Signed transport never writes disk; only the
    separate caller-keyed event receipt seam does."""
    return os.path.join(chat_dir(), ".cells.json")


def _token_path():
    return os.path.join(chat_dir(), ".node-token")


def sign_failures_dir():
    """A RAM-backed coordination dir even when HELM_CHAT_DIR is overridden to
    durable storage. The production default already lives under /dev/shm; a
    custom room gets an isolated tmpfs key, so status never writes disk."""
    d = os.path.realpath(chat_dir())
    shm = os.path.realpath(home.ram_root())
    if d == shm or d.startswith(shm + os.sep):
        return chat_dir()
    key = hashlib.blake2b(os.path.abspath(chat_dir()).encode("utf-8"),
                          digest_size=8).hexdigest()
    return os.path.join(
        SIGN_FAILURES_ROOT.replace("/dev/shm", home.ram_root(), 1), key)


def sign_failures_path():
    return os.path.join(sign_failures_dir(), SIGN_FAILURES_FILE)


def _sign_failures_lock_path():
    return os.path.join(sign_failures_dir(), ".sign-failures.lock")


#: The longest one signed send waits for the node's send lock before it goes
#: ahead without it. A send holds the lock for about 3.5 s (2.55 s measured for
#: one submission, plus the signer's one retry), so this covers a queue of
#: several seats posting at once. A holder that hangs is ended by the send's
#: own 30 s timeout.
SEND_LOCK_WAIT_S = 20.0


def _send_lock_path(url=None):
    """The send lock of ONE chat node, beside the incident lock in the RAM
    coordination dir. It is keyed by the node URL because the chain it guards
    is the node's, so a send to a scratch node never waits on the live one."""
    key = hashlib.blake2b(str(url if url is not None else node_url() or "")
                          .encode("utf-8"), digest_size=8).hexdigest()
    return os.path.join(sign_failures_dir(), ".node-send-%s.lock" % key)


def _send_lock_wait():
    """SEND_LOCK_WAIT_S, or half the hook alarm left if that is shorter. The
    row is appended only after it is signed, so a hook killed while it waits
    here would lose the row. The same rule as proxywatch.delivery_wait."""
    from . import hooklatency
    left = hooklatency.remaining_budget()
    return SEND_LOCK_WAIT_S if left is None else min(SEND_LOCK_WAIT_S,
                                                     left / 2.0)


@contextlib.contextmanager
def _node_send_lock():
    """Hold this node's send lock for one send. Yields None while it is held,
    else a note that says why the send goes ahead without it.

    THE NODE KEEPS ONE RECEIPT CHAIN FOR EVERY AGENT (task/3032). The chat
    node's solo build appends every client receipt to its one cipherclerk
    chain, and admits a turn only when the turn threads that chain's head. The
    signer reads the head, signs and submits. If another seat commits between
    the read and the submit, the node refuses with "receipt chain mismatch".
    Every seat has its own cell and they still race, because the head is not
    per cell. Measured on the live node: 49 of 49 recent receipts link to the
    receipt before them on the node, from 8 agents, and two seats were refused
    one second apart against the same head. Two sends from one shared profile
    race on its nonce in the same way ("nonce replay"). One send at a time per
    node removes both races between helm processes.

    FAIL-OPEN. A lock that cannot be opened, or is not free within
    `_send_lock_wait`, does not stop the send: the send goes out unlocked, as
    it did before, and the note goes into its failure reason if it then fails.
    The signer runs with close_fds, so a hung signer cannot keep the lock after
    this process ends."""
    import fcntl
    from . import hooklatency
    lf = None
    note = None
    wait = _send_lock_wait()
    try:
        try:
            os.makedirs(sign_failures_dir(), mode=0o700, exist_ok=True)
            _validate_sign_failure_owner()
            lf = open(_send_lock_path(), "a")
            if os.fstat(lf.fileno()).st_uid != os.geteuid():
                raise PermissionError("the send lock is not owned by this uid")
            if not hooklatency.flock(lf.fileno(), fcntl.LOCK_EX, "lock-send",
                                     fail_open=True,
                                     deadline=time.monotonic() + wait):
                note = ("sent without the node's send lock: another send held "
                        "it for more than %.1fs" % wait)
        except OSError as exc:
            note = "sent without the node's send lock: %s" % exc
        yield note
    finally:
        if lf is not None:
            lf.close()


def _epoch(value, default=0.0):
    """Finite non-negative event time; bool/string are never timestamps."""
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        return default
    return float(value)


def _count(value, default=0):
    return value if type(value) is int and value >= 0 else default


def _active_failure(rec):
    """Conservative activity test for current and older incident rows. A valid
    reason without a usable failure clock remains loud until an exact profile
    success writes a valid watermark; malformed clocks never reach comparisons."""
    if not isinstance(rec, dict) or not isinstance(rec.get("reason"), str) \
            or not rec["reason"]:
        return False
    last = _epoch(rec.get("_last_epoch"), None)
    success = _epoch(rec.get("_success_epoch"), None)
    if last is None:
        return success is None or success == 0
    return last > (success or 0)


def _profile(profile):
    """Exact signer identity. Never truncate: prefix collisions cross-clear."""
    p = str(profile or "helm-agent")
    return p or "helm-agent"


REASON_CAP = 360
_REASON_ELISION = " …%d chars elided… "


def _safe_reason(reason):
    """Bounded operator diagnostic. Redact BEFORE whitespace folding so an
    auth assignment can consume its full multiword value without eating the
    next diagnostic clause.

    THE CAP ELIDES THE MIDDLE, NEVER THE TAIL, AND SAYS THAT IT DID. A plain
    head-cut is silent and always eats the same half: a diagnostic states the
    problem first and the REMEDY last, so the operator loses exactly the
    sentence telling them what to do, and what survives is grammatical enough
    to read as the whole message. Measured 2026-09-09: cell.py's identity
    conflict is 407 characters, so `[:360]` cut inside the word 'explicit' and
    deleted "or pass an explicit profile if you mean to speak for X". Two
    seats read that as the complete refusal and neither saw the escape hatch.

    Redaction runs BEFORE this, so eliding a middle can never uncover a secret
    the head-cut was hiding. The bound itself is unchanged: the result is
    never longer than REASON_CAP.
    """
    s = str(reason or "unknown signing failure")
    s = _URL_USERINFO.sub(r"\1[redacted]@", s)
    s = _AUTH_VALUE.sub("authorization=[redacted]", s)
    s = _SECRET_VALUE.sub(lambda m: "%s=[redacted]" % m.group(1), s)
    s = _AUTH_SCHEME.sub("auth=[redacted]", s)
    s = _dsan(" ".join(s.split()))
    if len(s) <= REASON_CAP:
        return s
    # Budget the marker at its widest (the count can never exceed len(s)), so
    # the assembled line is <= REASON_CAP even though the printed count is the
    # true one, which is smaller.
    keep = REASON_CAP - len(_REASON_ELISION % len(s))
    if keep < 80:
        # No room to keep both ends usefully — mark the cut rather than lie.
        return s[:REASON_CAP - 1] + "…"
    head = keep * 2 // 3
    tail = keep - head
    return s[:head] + (_REASON_ELISION % (len(s) - head - tail)) + s[-tail:]


def _diag(code, reason, remediation=None, event_epoch=None, event_ts=None):
    """Structured, secret-safe failure captured at the failure boundary. The
    private event clock is sampled FIRST, so scheduling during redaction or
    before the RAM lock cannot reorder it behind a newer success."""
    event_epoch = _epoch(event_epoch, time.time())
    event_ts = event_ts if isinstance(event_ts, str) and event_ts else pk.now_ts()
    # A NODE REFUSAL WITH ONE KNOWN CURE NAMES THE CURE, not the code's generic
    # "inspect and retry": a retry of a refused PQ identity or a THE SWAP veto
    # fails identically forever (chatnode.REFUSALS).
    if not remediation and isinstance(reason, str):
        from . import chatnode
        remediation = chatnode.refusal_remedy(reason)
    fix = remediation or _REMEDIATION.get(
        code, "repair the named signing failure, then retry")
    if "transport ack" not in fix:
        fix += "; " + _ACK_REMEDIATION
    return {"code": str(code or "signing_exception"),
            "reason": _safe_reason(reason), "remediation": fix,
            "_event_epoch": event_epoch, "_event_ts": event_ts}


def _normal_diag(value, code="signing_exception"):
    if isinstance(value, dict):
        return _diag(value.get("code") or code, value.get("reason"),
                     value.get("remediation"), value.get("_event_epoch"),
                     value.get("_event_ts"))
    return _diag(code, value)


def _validate_sign_failure_owner():
    """Reject deterministic tmpfs-key squats before trusting or mutating them."""
    uid = os.geteuid()
    d = sign_failures_dir()
    if os.path.lexists(d):
        s = os.stat(d, follow_symlinks=False)
        if not stat.S_ISDIR(s.st_mode) or s.st_uid != uid:
            raise PermissionError("incident-state directory is not owned by this uid")
        if not os.access(d, os.R_OK | os.W_OK | os.X_OK):
            raise PermissionError("incident-state directory is not accessible")
    for p in (_sign_failures_lock_path(), sign_failures_path()):
        if not os.path.lexists(p):
            continue
        s = os.stat(p, follow_symlinks=False)
        if not stat.S_ISREG(s.st_mode) or s.st_uid != uid:
            raise PermissionError("incident-state file is not owned by this uid")


@contextlib.contextmanager
def _sign_failure_lock():
    """Serialize the shared RAM map across fleet poster processes."""
    import fcntl
    d = sign_failures_dir()
    os.makedirs(d, mode=0o700, exist_ok=True)
    _validate_sign_failure_owner()
    os.chmod(d, 0o700)
    with open(_sign_failures_lock_path(), "a") as f:
        if os.fstat(f.fileno()).st_uid != os.geteuid():
            raise PermissionError("incident-state lock is not owned by this uid")
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def _read_sign_failure_state():
    """Strict owner read: unlike pk.read_json, I/O and JSON errors stay loud."""
    p = sign_failures_path()
    if not os.path.exists(p):
        return {}
    with pk.open_regular(p, encoding="utf-8") as f:
        return json.load(f)


def _fallback_sign_failure_state():
    return _SIGN_FAILURE_FALLBACK.setdefault(
        os.path.abspath(sign_failures_path()), {})


def _combined_sign_failure_state(*records):
    """One exact-profile private state from persisted and process-local views."""
    records = [r for r in records if isinstance(r, dict)]
    if not records:
        return {}
    success = max(_epoch(r.get("_success_epoch")) for r in records)
    active = [r for r in records if _active_failure(r)]
    if not active:
        out = dict(max(records, key=lambda r: _epoch(r.get("_success_epoch"))))
        out.pop("reason", None)
        out["_success_epoch"] = success
        return out
    out = dict(max(active, key=lambda r: _epoch(r.get("_last_epoch"))))
    first = min(active, key=lambda r: _epoch(
        r.get("_first_epoch"), float("inf")))
    out["first_failure"] = first.get("first_failure", out.get("first_failure"))
    out["_first_epoch"] = min(_epoch(r.get("_first_epoch"),
                                    _epoch(out.get("_last_epoch")))
                              for r in active)
    out["failure_count"] = max(_count(r.get("failure_count"), 1)
                               for r in active)
    out["_success_epoch"] = success
    return out


def _next_sign_failure(old, profile, diag):
    """Apply one event-clock-ordered failure to one exact profile state."""
    now, stamp = diag["_event_epoch"], diag["_event_ts"]
    candidate = {"profile": profile, "code": diag["code"],
                 "reason": diag["reason"], "first_failure": stamp,
                 "last_failure": stamp, "failure_count": 1,
                 "remediation": diag["remediation"],
                 "_first_epoch": now, "_last_epoch": now}
    old = old if isinstance(old, dict) else {}
    success = _epoch(old.get("_success_epoch"))
    last = _epoch(old.get("_last_epoch"))
    if success >= now:
        return old, candidate, False
    active = _active_failure(old)
    if last > now:
        if not active:
            return old, candidate, False
        out = dict(old)
        out["failure_count"] = _count(out.get("failure_count")) + 1
        first = _epoch(out.get("_first_epoch"), last)
        if now < first:
            out.update(first_failure=stamp, _first_epoch=now)
        return out, out, True
    candidate.update(
        first_failure=old.get("first_failure")
        if active and isinstance(old.get("first_failure"), str) else stamp,
        failure_count=(_count(old.get("failure_count")) if active else 0) + 1,
        _first_epoch=_epoch(old.get("_first_epoch"), now) if active else now,
        _success_epoch=success)
    return candidate, candidate, True


def _failure_public(rec, now=None):
    """Validate one persisted incident before it reaches any status surface."""
    if not isinstance(rec, dict) or not isinstance(rec.get("reason"), str) \
            or not rec["reason"]:
        return None
    now = _epoch(now, time.time())
    first = _epoch(rec.get("_first_epoch"), now)
    last = _epoch(rec.get("_last_epoch"), first)
    return {"profile": _dsan(rec.get("profile"))
            if isinstance(rec.get("profile"), str) else "?",
            "code": rec.get("code") if isinstance(rec.get("code"), str)
            else "incident_state_corrupt",
            "reason": _safe_reason(rec["reason"]),
            "first_failure": rec.get("first_failure")
            if isinstance(rec.get("first_failure"), str) else "?",
            "last_failure": rec.get("last_failure")
            if isinstance(rec.get("last_failure"), str) else "?",
            "failure_count": max(1, _count(rec.get("failure_count"), 1)),
            "remediation": rec.get("remediation")
            if isinstance(rec.get("remediation"), str) else _ACK_REMEDIATION,
            "age_s": max(0, int(now - first)),
            "last_age_s": max(0, int(now - last))}


def _sign_failure_rows(state, now=None):
    now, rows = _epoch(now, time.time()), []
    if not isinstance(state, dict):
        return rows
    for p, rec in state.items():
        if not _active_failure(rec):
            continue
        normalized = dict(rec)
        if isinstance(p, str):
            normalized["profile"] = p
        row = _failure_public(normalized, now)
        if row:
            rows.append(row)
    return sorted(rows, key=lambda r: r.get("last_failure") or "", reverse=True)


def _incident_state_unreadable(exc, now=None):
    reason = "incident state unreadable (%s: %s)" % (
        exc.__class__.__name__, exc)
    d = _diag("incident_state_unreadable", reason, event_epoch=now)
    rec = {"profile": _post_identity(None, admit=False)[0], "code": d["code"],
           "reason": d["reason"], "first_failure": d["_event_ts"],
           "last_failure": d["_event_ts"], "failure_count": 1,
           "remediation": d["remediation"],
           "_first_epoch": d["_event_epoch"],
           "_last_epoch": d["_event_epoch"]}
    return _failure_public(rec, d["_event_epoch"])


def sign_failures():
    """Every uncleared profile failure, newest first, projected without the
    internal epoch fields. Persisted and process-local owner views are merged;
    an unreadable owner is itself a public degradation, never healthy/empty."""
    now = time.time()
    with _SIGN_FAILURE_FALLBACK_LOCK:
        fallback = {p: dict(r) for p, r in
                    _fallback_sign_failure_state().items()
                    if isinstance(r, dict)}
        try:
            _validate_sign_failure_owner()
            if not os.path.exists(sign_failures_path()):
                state = {}
            else:
                with _sign_failure_lock():
                    state = _read_sign_failure_state()
        except Exception as exc:
            rows = [_incident_state_unreadable(exc, now)]
            rows.extend(_sign_failure_rows(fallback, now))
            return sorted(rows, key=lambda r: r.get("last_failure") or "",
                          reverse=True)
    if not isinstance(state, dict):
        state = {}
    merged = {}
    profiles = list(state) + [p for p in fallback if p not in state]
    for p in profiles:
        if isinstance(p, str):
            merged[p] = _combined_sign_failure_state(state.get(p),
                                                     fallback.get(p))
    return _sign_failure_rows(merged, now)


def _record_sign_failure(profile, failure):
    """Upsert one exact profile. The process fallback is updated before the
    shared write, so lock/write failure cannot erase the incident."""
    p = _profile(profile)
    d = _normal_diag(failure)
    remembered = False
    with _SIGN_FAILURE_FALLBACK_LOCK:
        fallback = _fallback_sign_failure_state()
        try:
            with _sign_failure_lock():
                state = _read_sign_failure_state()
                if not isinstance(state, dict):
                    state = {}
                old = _combined_sign_failure_state(state.get(p), fallback.get(p))
                stored, shown, changed = _next_sign_failure(old, p, d)
                if stored:
                    fallback[p] = stored
                remembered = True
                if changed:
                    state[p] = stored
                    pk.write_json(sign_failures_path(), state)
                return _failure_public(shown, d["_event_epoch"])
        except Exception:
            if not remembered:
                stored, shown, changed = _next_sign_failure(fallback.get(p), p, d)
                if changed:
                    fallback[p] = stored
            raise


def _clear_sign_failure(profile, succeeded_at=None):
    """Record a per-profile committed-signing watermark in both owner views.
    The real receipt-confirmed signing path is the sole production caller; the
    fallback changes only after the shared watermark write succeeds."""
    p = _profile(profile)
    succeeded_at = _epoch(succeeded_at, time.time())
    try:
        with _SIGN_FAILURE_FALLBACK_LOCK:
            fallback = _fallback_sign_failure_state()
            with _sign_failure_lock():
                state = _read_sign_failure_state()
                if not isinstance(state, dict):
                    state = {}
                old = _combined_sign_failure_state(state.get(p), fallback.get(p))
                last = _epoch(old.get("_last_epoch"))
                prior = _epoch(old.get("_success_epoch"))
                active = _active_failure(old)
                if last > succeeded_at:
                    return False
                cleared = {"profile": p,
                           "_success_epoch": max(prior, succeeded_at)}
                state[p] = cleared
                pk.write_json(sign_failures_path(), state)
                fallback[p] = cleared
                return active
    except Exception:
        return False


def _signed_success_epoch(profile):
    """Exact-profile committed-signing evidence. ACK watermarks still retire
    incidents but never claim that a signing receipt was observed."""
    p = _profile(profile)
    with _SIGN_FAILURE_FALLBACK_LOCK:
        fallback = _fallback_sign_failure_state().get(p)
        try:
            _validate_sign_failure_owner()
            if not os.path.exists(sign_failures_path()):
                state = {}
            else:
                with _sign_failure_lock():
                    state = _read_sign_failure_state()
        except Exception:
            return 0.0
    rec = _combined_sign_failure_state(
        state.get(p) if isinstance(state, dict) else None, fallback)
    if rec.get("acknowledged_at"):
        return 0.0
    return _epoch(rec.get("_success_epoch"))


def acknowledge_sign_failures(profile=None):
    """Operator retirement for dead/renamed profiles. This is an explicit ACK,
    not a recovery claim; its watermark prevents delayed pre-ack failures from
    resurrecting the incident. Returns acknowledged exact profile names."""
    now, stamp = time.time(), pk.now_ts()
    try:
        with _SIGN_FAILURE_FALLBACK_LOCK:
            fallback = _fallback_sign_failure_state()
            with _sign_failure_lock():
                state = _read_sign_failure_state()
                if not isinstance(state, dict):
                    state = {}
                profiles = list(state) + [p for p in fallback if p not in state]
                targets = [_profile(profile)] if profile is not None else [
                    p for p in profiles if isinstance(p, str) and _active_failure(
                        _combined_sign_failure_state(state.get(p), fallback.get(p)))]
                done, cleared = [], {}
                for p in targets:
                    rec = _combined_sign_failure_state(state.get(p), fallback.get(p))
                    if not _active_failure(rec):
                        continue
                    cleared[p] = {"profile": p, "_success_epoch": now,
                                  "acknowledged_at": stamp}
                    state[p] = cleared[p]
                    done.append(p)
                if done:
                    pk.write_json(sign_failures_path(), state)
                    fallback.update(cleared)
                return done
    except Exception:
        return []


def _stamp_sign_failure(row, profile, failure):
    """Preserve RAM-pure fail-open delivery, but make the fallback self-
    describing on the row and persistent in the shared transport status. Even
    a failed RAM-state write cannot kill the chat row."""
    try:
        rec = _record_sign_failure(profile, failure)
    except Exception as exc:
        p = _profile(profile)
        with _SIGN_FAILURE_FALLBACK_LOCK:
            private = _fallback_sign_failure_state().get(p)
            rec = _failure_public(private) if private else None
        d = _normal_diag(failure)
        stamp = d["_event_ts"]
        rec = rec or {"profile": p, "code": d["code"], "reason": d["reason"],
                      "first_failure": stamp, "last_failure": stamp,
                      "failure_count": 1, "age_s": 0, "last_age_s": 0,
                      "remediation": d["remediation"]}
        rec["remediation"] = "%s; shared incident retention failed: %s; process-local fallback active" % (
            rec["remediation"], _safe_reason(
                "%s: %s" % (exc.__class__.__name__, exc)))
    row["transport"] = _public_transport(dict(rec, state="DEGRADED"))
    return row


def node_head(url=None, timeout=1.5):
    """The chain head receipt: dict, {} for a live-but-empty chain, None when
    the node is down — post()'s cheap reachability probe doubles as data."""
    from . import cell
    u = url or node_url()
    if not u:
        return None
    rs = cell.get_json(u + "/api/receipts", timeout=timeout)
    if isinstance(rs, list):
        return rs[0] if rs and isinstance(rs[0], dict) else {} if not rs else None
    return None


def _transient_failure(profile, code, reason):
    d = _diag(code, reason)
    rec = {"profile": _profile(profile), "code": d["code"],
           "reason": d["reason"], "first_failure": d["_event_ts"],
           "last_failure": d["_event_ts"], "failure_count": 1,
           "remediation": d["remediation"],
           "_first_epoch": d["_event_epoch"],
           "_last_epoch": d["_event_epoch"]}
    return _failure_public(rec, d["_event_epoch"])


def transport_status(fleet=False):
    """The ONE transport truth for CLI/TUI/web/doctor. A reachable node plus an
    executable signer is only *ready*; any profile whose attempted signed turn
    fell back remains DEGRADED until that SAME profile completes a signed turn.
    An unset signer is configured-off; a set-but-unusable signer is unavailable.
    The persistent incident fields come from RAM, never disk coordination.

    THIS PROCESS'S SIGNER IS THE ONE ITS POSTS WOULD USE. `_post_identity` is
    asked once, under the same configured-off rule `_signed_row` applies, and
    every per-profile question below is keyed on its answer. Keyed on the raw
    ambient profile instead, a seat whose posts the gate refuses read SIGNED on
    the OWNER's receipt, and read SIGNED again after `transport ack` retired
    its incident, while its next post was refused. A refusal is live, not
    retained, so no ack can hide it: it leads `failed_profiles` until the seat
    can sign as itself. A `fleet` reader describes the system, not the process
    rendering it, so it reports retained incidents and skips its own identity."""
    from . import cell
    u = node_url()
    signer = cell.bin_status()
    failures = sign_failures()
    out = {"mode": "unsigned", "url": u, "head": None,
           "signer": signer["usable"],
           "signer_configured": signer["configured"]}
    attempts = signer["configured"] and (not signer["usable"] or bool(u))
    # A FLEET READER DISCARDS `refused` BELOW, so it asks for the label only:
    # the label is the seat whether the gate would swap or refuse, and a web
    # poll must not run the identity layer (task/3049).
    me, refused = (_post_identity(None, admit=not fleet) if attempts
                   else (None, None))
    if refused and not fleet:
        current = next((f for f in failures
                        if f.get("profile") == _dsan(me)
                        and f.get("code") == refused["code"]), None)
        current = current or _transient_failure(
            me, refused["code"], refused["reason"])
        rows = [current] + [f for f in failures if f is not current]
        out.update(current, mode="degraded", state="DEGRADED",
                   failed_profiles=rows)
        return out
    if signer["configured"] and not signer["usable"]:
        current = next((f for f in failures
                        if f.get("profile") == _dsan(me)
                        and f.get("code") == "signer_unavailable"), None)
        current = current or _transient_failure(
            me, "signer_unavailable", signer["reason"])
        rows = [current] + [f for f in failures if f is not current]
        out.update(current, mode="degraded", state="DEGRADED",
                   failed_profiles=rows)
        return out
    probe_error = None
    try:
        h = node_head(u) if u else None
    except Exception as exc:
        h = None
        probe_error = "%s: %s" % (exc.__class__.__name__, exc)
    out["head"] = h.get("chain_index") if isinstance(h, dict) and h else None
    if failures:
        out.update(failures[0], mode="degraded", state="DEGRADED",
                   failed_profiles=failures)
        return out
    if u and signer["usable"] and h is None:
        reason = "configured chat node unreachable at %s" % u
        if probe_error:
            reason += " (%s)" % probe_error
        f = _transient_failure(me, "node_unreachable", reason)
        out.update(f, mode="degraded", state="DEGRADED",
                   failed_profiles=[f])
        return out
    if h is not None:
        if not signer["usable"]:
            out["mode"] = "unsigned (no signer)"
        elif _signed_success_epoch(me):
            out.update(mode="signed", state="SIGNED", label="signed")
        else:
            out.update(mode="ready", state="READY",
                       label="ready (unproven)",
                       detail="no committed signing receipt observed for this profile")
            # A FLEET SURFACE DESCRIBES THE SYSTEM, NOT THE PROCESS RENDERING
            # IT. Owner, live 2026-07-29: "does the webui need retarting or
            # something?" — the web ledger read "signing ready (unproven)"
            # while codex, ds4pro, gemini and the integrator all carried
            # committed receipts and rows were anchoring with real turn hashes.
            # Nothing was stale. The web process asks as its own identity,
            # usually the `helm-agent` service label, whose receipts cover only
            # what that one process signed, so an exact-profile question was
            # the wrong question to put on a fleet panel.
            #
            # The per-profile answer is UNCHANGED for a seat asking about
            # itself — being told the fleet is fine when YOU cannot sign is the
            # failure this whole projection exists to prevent. `fleet` is
            # opt-in, and the label names the profile it borrowed, so the
            # surface never implies the reader signed something it did not.
            if fleet:
                seen = fleet_signed()
                if seen:
                    who, when = seen
                    out.update(mode="signed", state="SIGNED",
                               label="signed (fleet)",
                               fleet_signer=who, fleet_signed_epoch=when,
                               detail="this reader has no receipt of its own; "
                                      "newest fleet receipt is %s" % who)
    return out


def fleet_signed():
    """(profile, epoch) of the NEWEST committed signing receipt on this host,
    or None when no profile has ever signed.

    Read-only over the same state _signed_success_epoch consults, so the fleet
    answer and the per-profile answer can never disagree about a given profile
    — one store, two questions."""
    try:
        state = _read_sign_failure_state()
    except Exception:                     # noqa: BLE001 — advisory projection
        return None
    if not isinstance(state, dict):
        return None
    best = None
    for name, rec in state.items():
        # AN ACK IS NOT A RECEIPT. `transport ack` writes a success watermark
        # to retire an incident, and read here it named a profile that never
        # signed as the fleet's newest signer. _signed_success_epoch already
        # skips it; the fleet question must agree with the per-profile one.
        if not isinstance(rec, dict) or rec.get("acknowledged_at"):
            continue
        try:
            epoch = float(rec.get("_success_epoch") or 0)
        except (TypeError, ValueError):
            continue
        if epoch > 0 and (best is None or epoch > best[1]):
            best = (name, epoch)
    return best


def transport_label(st):
    """Public wording shared by every text renderer."""
    if not isinstance(st, dict):
        return "unknown"
    return st.get("label") or st.get("mode", "unknown")


def transport_failure_summary(st):
    """One loud, bounded operator line from transport_status()."""
    if not isinstance(st, dict) or st.get("mode") != "degraded":
        return ""
    st = _public_transport(st)
    return ("DEGRADED profile '%s': %s — first %s, last %s (%ss ago), "
            "%d failure%s; remediation: %s" % (
                st.get("profile") or "?", st.get("reason") or "unknown",
                st.get("first_failure") or "?", st.get("last_failure") or "?",
                st.get("last_age_s") or 0, st.get("failure_count") or 1,
                "s"[:(st.get("failure_count") or 1) != 1],
                st.get("remediation") or "retry a signed turn"))


def _cmd_transport(args):
    verb = args[0] if args else "status"
    if verb == "status" and len(args) == 1 or not args:
        st = transport_status()
        print("helm chat transport: " + (
            transport_failure_summary(st) if st.get("mode") == "degraded"
            else "%s%s" % (transport_label(st).upper(),
                            " — chain #%s" % st["head"]
                            if st.get("head") is not None else "")))
        return 1 if st.get("mode") == "degraded" else 0
    if verb != "ack":
        print(HELP["transport"], file=sys.stderr)
        return 2
    profile = _pop_flag(args, "--profile")
    all_profiles = "--all" in args
    if all_profiles:
        args.remove("--all")
    if (profile is None) == (not all_profiles) or len(args) != 1:
        print(HELP["transport"], file=sys.stderr)
        return 2
    done = acknowledge_sign_failures(None if all_profiles else profile)
    if not done:
        print("helm chat transport: no matching active incident", file=sys.stderr)
        return 1
    print("helm chat transport: ACKNOWLEDGED (not recovered): %s" %
          ", ".join(repr(p) for p in done))
    return 0


def digest_payload(text):
    """chat:b2b:<blake2b-256 of the NFC text> — the whole signed claim (the
    text itself stays in the RAM room; the chain corroborates)."""
    return CHAT_TAG + _b2b(text)


def _b2b(s):
    return hashlib.blake2b(unicodedata.normalize("NFC", s or "").encode("utf-8"),
                           digest_size=32).hexdigest()


def is_verdict(row):
    return bool(row.get("vtip") and row.get("vrid"))


def committed_signed(row):
    """The transport's committed-signing truth for a stored row: a turn is
    signed only when the COMPLETE receipt landed — turn + receipt + chain.
    A row with a bare truthy `turn` is not signed (xrev E: consumers
    must share this predicate, never invent a looser one)."""
    return bool(row.get("turn") and row.get("receipt")
                and row.get("chain") is not None)


def verdict_digest(lane, tip, rid, ref, text):
    """chat:verdict:b2b:<blake2b-256 of RS-joined verdict fields + text> — the
    signed claim of a VERDICT TURN: "this author asserts this verdict evidence
    binds this exact dispatched tip on this lane". Disjoint tag for the same
    reason replies have one: text is attacker-chosen, so an in-band prefix on
    the plain tag would let ordinary prose mint a verdict's digest — free
    attestation forgery. The structured binding lives IN the signed claim, by
    construction (bd-173ca9 / the dregg-gate stamp seam)."""
    fields = [lane, tip, rid, ref, text]
    return VERDICT_TAG + _b2b(FIELD_SEP.join(map(_reply_field, fields)))


def _reply_field(value):
    """An injective field encoding that leaves ordinary payloads byte-for-byte
    unchanged. RS is the tuple separator, so a literal RS inside an
    attacker-controlled author/text must be escaped; backslash is escaped
    first so the encoding itself cannot collide."""
    return str(value or "").replace("\\", "\\\\").replace(FIELD_SEP, "\\x1e")


def reply_digest(pid, pts, pfrom, text, parent_text=None):
    """chat:reply:b2b:<blake2b-256 of RS-joined parent fields + text> — the
    signed claim of a REPLY: "this author, at this chain index, asserts this
    text is a child of that exact row".

    Why a distinct TAG and not a prefix inside the plain-post payload: the
    text is attacker-chosen, so an in-band `reply|<id>|…` prefix on the SAME
    tag would let an ordinary post whose text reads like that prefix produce
    a reply's digest — free re-parenting. Disjoint tags make the two payload
    spaces disjoint by construction (the `<alg>:<hash>` pattern premise.py
    and ATTESTATION.md already canonize). RS separates injectively-escaped
    fields. A pre-id parent additionally binds its text: (ts, from) alone is
    not an identity when one author posts twice inside a second."""
    fields = [pid, pts, pfrom]
    if parent_text is not None:
        fields.append(parent_text)
    fields.append(text)
    return REPLY_TAG + _b2b(FIELD_SEP.join(map(_reply_field, fields)))


def react_digest(row):
    """chat:react:b2b:<blake2b-256 of RS-joined reaction fields> — the signed
    claim of a REACTION, in its OWN payload space.

    Why the retag: v2 signed reactions as `react|tts|tfrom|emoji|reactor`
    under the PLAIN-POST tag, so a post whose TEXT was literally that string
    digested identically to a reaction — a free forgery, exactly the in-band
    prefix collision reply_digest refuses to repeat. It was harmless while
    nothing re-derived a chat digest; `helm chat verify` (landed with
    threading) means something now does, which turns a latent collision into
    a live one. Reactions therefore get a disjoint tag and the same
    injectively-escaped RS join, so no post text can ever reach this space.

    Retagging costs nothing: every signed reaction already on disk predates
    the recorded `payload` field, so verify already classes them `legacy` and
    re-derives none of them (measured: 0 ok / 103 legacy on the live room)."""
    fields = ["unreact" if row.get("un") else "react",
              row.get("tts") or "", row.get("tfrom") or "",
              row.get("react") or "", row.get("from") or ""]
    return REACT_TAG + _b2b(FIELD_SEP.join(map(_reply_field, fields)))


def _react_payload(row):
    """The v2 reaction payload string, kept ONLY to recompute pre-retag rows.
    New reactions sign react_digest(); this exists so a historical row can
    still be explained rather than silently mis-verified."""
    return "%s|%s|%s|%s|%s" % ("unreact" if row.get("un") else "react",
                               row.get("tts") or "", row.get("tfrom") or "",
                               row.get("react") or "", row.get("from") or "")


def is_reply(m):
    """Does this row point at a parent? (An unresolvable ref still carries
    reply_to, a pre-id parent leaves only rts — either identity marks the
    shape.) A reaction is never a reply: it has its own tts/tfrom pointer."""
    return not m.get("react") and bool(m.get("reply_to") or m.get("rts"))


def payload_for(row, text=None):
    """THE ONE recomputer: the exact payload string a row's signed turn
    carries, dispatched on ROW SHAPE (the rule already in force in v2 —
    posts sign their text, reactions sign a react tuple). `text` is the
    caller's payload text at post time; None ⇒ read it off the stored row,
    which is what `verify` does.

      reaction  -> chat:react:b2b: over the RS-joined reaction fields
      reply     -> chat:reply:b2b: over the parent binding + the text
      any other -> chat:b2b: over the text   (BYTE-IDENTICAL to v2 — every
                   signed row already on disk recomputes exactly as before)"""
    if row.get("react"):
        return react_digest(row)
    body = row.get("text") if text is None else text
    if is_verdict(row):
        return verdict_digest(row.get("vlane"), row["vtip"], row["vrid"],
                              row.get("vref"), body)
    if is_reply(row):
        return reply_digest(row.get("reply_to"), row.get("rts"),
                            row.get("rfrom"), body,
                            row.get("rtext") if "rtext" in row else None)
    return digest_payload(body)


def _node_token():
    try:
        with open(_token_path()) as f:
            t = f.read().strip()
        if t:
            return t
    except OSError:
        pass
    from . import chatnode
    return chatnode.state().get("token") or ""


def _env_extra(token):
    """Aim the signer at the ROOM node (not the attest node): dregg-native
    DREGG_* plus the legacy meld-style names — the seam drives either bin.

    A chat send is ALWAYS a single EmitEvent (topic helm.chat) — the
    coordination class dregg's Stage B waives the admission charge for. When
    `cell.coord_fee()` is 0 (the default = the room node opted into the exempt
    class), forward `DREGG_COORDINATION_EXEMPT=1` so the signer estimates the
    turn's declared fee at 0 and it rides free: no cell drain, no faucet grant,
    no [unsigned] throttle. Setting HELM_NODE_COORD_FEE to a nonzero fee (a
    node that has NOT opted in) disables the signal so the signer funds the
    full computron cost as before — ONE knob for both the anchor and chat
    paths."""
    from . import cell
    u = node_url() or ""
    env = {"MELD_NODE_URL": u, "DREGG_NODE_URL": u}
    if token:
        env["MELD_NODE_TOKEN"] = token
        env["DREGG_API_TOKEN"] = token
    if cell.coord_fee() == 0:
        env["DREGG_COORDINATION_EXEMPT"] = "1"
    return env


def _revive():
    """Re-unlock + re-bootstrap a wiped/stale room node. Returns (token, None)
    or (None, precise reason); unlock/faucet health failures never disappear."""
    from . import chatnode
    u = node_url()
    st = chatnode.state()
    if not u:
        return None, "node revive unavailable: no chat node URL"
    if not st.get("passphrase"):
        return None, "node revive unavailable: no stored chat-node passphrase"
    token, err = chatnode.unlock(u, st["passphrase"])
    if err:
        return None, err
    healthy, err = chatnode.ensure_healthy_result(u)
    if not healthy:
        return None, err
    _ensure_dir()
    pk.atomic_write(_token_path(), token or "")
    os.chmod(_token_path(), 0o600)
    return token, None


def _faucet(cell_hex):
    """Top up one cell from the room node's faucet. Returns the validated
    response or a precise refusal; success:false is never discarded.

    CALLED ONLY AFTER THE NODE HAS SAID THE CELL CANNOT PAY (`_short_of_fee`),
    never before a send. The proactive top-up this replaced fired before every
    send whose cell held under 3000 — a line drawn from a ~1442-computron send
    measured before the coordination class made chat turns fee-free. On a
    fee-free node every cell sits at zero, so EVERY send asked for a grant
    nothing needed, and each grant is a faucet Transfer whose own fee burns
    (measured on the live node: an 8000 refill gone in ten minutes, with no
    cell holding any of it).

    A REFUSAL IS CLASSIFIED, NOT JUST RELAYED. "faucet refused: …" covers a
    rate limit, an unreachable node and a SOURCE that has nothing left, and the
    repairs are opposite — waiting cures the first and cures nothing in the
    last. chatnode reads which cell the node named and says which case this
    is."""
    from . import chatnode
    u = node_url()
    if not u:
        return None, "faucet unavailable: no chat node URL"
    r, err = chatnode.faucet(u, cell_hex, 10000)
    if err:
        return None, chatnode.classify_faucet_refusal(err, cell_hex)
    return r, None


def _room_cell(profile, token):
    """The profile's cell ON THE ROOM NODE: RAM-cache first, else one
    idempotent COORDINATION-EXEMPT `join` aimed at the room node via env_extra.
    Returns (cell_hex, None, True) on success, else (None, reason, launched) —
    `launched` False ONLY when the signer binary never ran (local launch
    failure), so the caller never revives over it.

    --fund 0 IS THE WHOLE FIX, and its absence cost twenty hours (2026-07-29).
    This used to join with no --fund at all, so the signer defaulted to asking
    the faucet for 5000 computrons. Watch what that does:

        faucet grant was already in flight but cell 69d709b0… did not reach
        5000 computrons within 10s: {"error":"rate limited: 1 request per
        cell per minute"}

    The client waits TEN SECONDS; the faucet rate-limits to ONE REQUEST PER
    CELL PER MINUTE. The client's patience is shorter than the server's minimum
    retry interval, so a contended faucet can never be satisfied — the join was
    not slow, it was unable to complete by construction. One failure then
    latched the transport DEGRADED for a day, long after the rate limit expired
    sixty seconds later.

    A ROOM CELL NEVER NEEDS A BALANCE. Every turn it emits is a coordination
    turn — EmitEvent only, no balance_change — which this module already
    declares at fee 0 (`is_coordination_actions`/`turn_fee`, and dregg's
    matching Turn::is_coordination). Asking the faucet to fund it was asking
    for money to pay a bill of zero. MEASURED against the live node: `join
    --fund 0` returns rc 0, "joined": true, "materialized": false, and never
    touches the faucet at all.

    HELM_CHAT_JOIN_FUND overrides for a node that has NOT opted into the
    coordination-exempt class, mirroring HELM_NODE_COORD_FEE on the anchor
    path — same leash, same escape hatch, same default of zero."""
    from . import cell
    cache = pk.read_json(cells_path(), {}) or {}
    hexid = cache.get(profile)
    if hexid:
        return hexid, None, True
    fund = str(home.env("CHAT_JOIN_FUND", "0") or "0")
    rc, out, err = cell.run_bin(["join", "--profile", profile,
                                 "--fund", fund], timeout=30,
                                env_extra=_env_extra(token))
    if rc is None:
        # A timeout DID launch; preserve its typed reason so the caller does
        # not revive/retry an operation whose outcome is unknown.
        return None, err, isinstance(err, cell.BinTimeout)
    if rc != 0:
        return None, "join failed (rc %d): %s" % (rc, (err or out).strip()), True
    info = cell._last_json(out)
    if not (info and info.get("cell")):
        return None, "join printed no cell id", True
    _ensure_dir()
    cache[profile] = info["cell"]
    pk.write_json(cells_path(), cache)
    return info["cell"], None, True


def _hex64(value):
    return isinstance(value, str) and len(value) == 64 \
        and all(c in "0123456789abcdefABCDEF" for c in value)


def _complete_send(info):
    return isinstance(info, dict) and info.get("sent") is True \
        and _hex64(info.get("turn_hash")) \
        and _hex64(info.get("receipt_hash")) \
        and type(info.get("chain_index")) is int and info["chain_index"] >= 0


def _signer_failure(reason):
    from . import cell
    return _diag("signing_timeout" if isinstance(reason, cell.BinTimeout)
                 else "signer_unavailable", reason)


NODE_REFUSAL_MARK = "node refused the turn: "
_SIGNER_ERROR_PREFIX = "[client-sign] error: "
_SIGNER_ACCEPTED_MARK = "[client-sign] turn accepted"
_SIGNER_NO_REASON = "no reason given"


def _node_refusal(rc, out, err):
    """The node's own text when the signer reported an EXPLICIT refusal of the
    send, else None — and None means the outcome stays UNKNOWN.

    ONE SHAPE, AND IT IS THE SIGNER'S EXIT PATH. dregg-client-sign prints
    `[client-sign] error: node refused the turn: <error>` as its LAST stderr
    line and exits 1 when `/turns/submit` answered 2xx JSON whose `accepted`
    was not true. The node's submit handler sends `accepted: false` only on
    paths that roll the turn back (NotCommitted, never gossiped), and it always
    serializes `accepted`, so a string `error` from it is a refusal of a turn
    that did not commit. Dropping that text sent an operator to inspect
    receipts for a turn the node had already said no to.

    WHAT THE PROSE CANNOT PROVE STAYS UNKNOWN. The signer folds a missing or
    malformed `accepted` into the same words, and with no `error` string it
    prints `no reason given`: the shape a `{}` answer produces. So that text,
    an empty text, a zero or signal exit, anything on stdout, any
    `turn accepted` line, and a refusal that is not the last line are all
    UNKNOWN. A timeout never reaches here: run_bin keeps none of its output.
    This classifies; it never replays. `_sign_send` sends a second time only
    after a refusal of this shape that names the cell's own fee."""
    last = _signer_last_word(rc, out, err)
    if last is None or not last.startswith(NODE_REFUSAL_MARK):
        return None
    text = last[len(NODE_REFUSAL_MARK):].strip()
    return text if text and text != _SIGNER_NO_REASON else None


def _signer_last_word(rc, out, err):
    """The signer's last stderr line, `[client-sign] error: ` stripped, when
    it exited nonzero, printed nothing on stdout and never said a turn was
    accepted — else None. The one door both refusal shapes are read through."""
    if not isinstance(rc, int) or rc <= 0 or (out or "").strip():
        return None
    lines = [line.strip() for line in (err or "").splitlines() if line.strip()]
    if not lines or any(line.startswith(_SIGNER_ACCEPTED_MARK) for line in lines):
        return None
    last = lines[-1]
    return last[len(_SIGNER_ERROR_PREFIX):] if last.startswith(
        _SIGNER_ERROR_PREFIX) else last


FUNDING_REFUSAL_MARK = "faucet refused funding: "


def _funding_refusal(rc, out, err):
    """The faucet's own error when the signer's PRE-SUBMIT top-up was refused,
    else None.

    THE BOUND SIGNER FUNDS BEFORE IT SUBMITS. dregg-client-sign's cmd_send
    runs ensure_cell(minimum = the turn's fee) ahead of POST /turns/submit, and
    when the faucet answers success:false it exits 1 with `[client-sign]
    error: faucet refused funding: <the faucet's JSON>` as its last line. The
    turn was never submitted, so nothing can have committed: reading that as
    `send_outcome_unknown` told an operator a never-sent turn "may already
    have committed". The JSON's `error` is the faucet's own refusal, which
    `chatnode.classify_faucet_refusal` reads like any other; an unparseable
    body is kept whole."""
    import json
    last = _signer_last_word(rc, out, err)
    if last is None or not last.startswith(FUNDING_REFUSAL_MARK):
        return None
    body = last[len(FUNDING_REFUSAL_MARK):].strip()
    try:
        answer = json.loads(body)
    except ValueError:
        answer = None
    said = answer.get("error") if isinstance(answer, dict) else None
    return said.strip() if isinstance(said, str) and said.strip() \
        else (body or _SIGNER_NO_REASON)


def _funding_diag(rc, said, cell_hex):
    """The row for a send whose pre-submit top-up the faucet refused: a dry
    source leads with faucet_source_dry, anything else is send_failed, and
    both say the turn was never submitted."""
    from . import chatnode
    classified = chatnode.classify_faucet_refusal("faucet refused: " + said,
                                                 cell_hex)
    never = ("cell send rc %s: the signer's top-up before submitting was "
             "refused, so the turn was never submitted" % rc)
    if chatnode.FAUCET_DRY_MARK in classified:
        return _diag("faucet_source_dry", "; ".join([classified, never]))
    return _diag("send_failed", "%s: %s" % (never, classified))


def _short_of_fee(refused, cell_hex):
    """(need, have) when the node refused the send because the SENDING cell
    could not pay its fee, else None.

    ONE REFUSAL IS CURED BY A GRANT, and it is the node's own. The fee-loop
    node's /turns/submit answers a rejected turn with `rejected: <TurnError>`,
    and TurnError::InsufficientBalance renders `insufficient balance on cell
    <16 hex>: need N, have M` (turn/src/error.rs); for a chat turn the only
    balance the executor checks is the agent's, against the declared fee. A
    shortfall on any OTHER cell is not this cell's problem. `computron budget
    exceeded` is not either: it says the DECLARED fee was below the metered
    cost, and a grant does not change what the signer declares."""
    from . import chatnode
    parsed = chatnode.parse_insufficient(refused)
    if not parsed or not str(cell_hex or "").lower().startswith(parsed[0]):
        return None
    return parsed[1], parsed[2]


def _sign_send(payload, profile, topic=CHAT_TOPIC):
    """One signed self-write turn under `topic` (helm.chat by default, but the
    emit path is TOPIC-PARAMETERIZED — helm.land/helm.claim ride the SAME signer,
    revive, faucet and completeness checks, never a forked second signer).
    Returns (send-info, None) or (None, structured diagnostic). The idempotent
    join can recover once. The signer loses the distinction between explicit
    rejection and a malformed submit response, so even a nonzero exit may
    follow a committed turn: every unsuccessful send is `send_outcome_unknown`
    except an explicit node refusal (`_node_refusal`), which is `send_failed`
    and carries the node's text.

    THE TOP-UP IS REACTIVE. The send goes first. Only when the node refuses it
    because this cell cannot pay its fee (`_short_of_fee`) does helm ask the
    faucet, once, and send once more — safe, because a refused turn appended
    nothing. A send that fails any other way never touches the faucet, and a
    retried send is never retried again. On a fee-free node that is zero
    grants; on a fee-charging one it is a grant exactly when one is owed.

    ONE SEND AT A TIME PER NODE. The send, its retry and the top-up between
    them run under `_node_send_lock`, because the node admits a turn only on
    its one node-wide receipt head, and two seats that read that head at the
    same time cannot both commit."""
    from . import cell
    signer = cell.bin_status()
    if not signer["usable"]:
        return None, _diag("signer_unavailable", signer["reason"])
    token = _node_token()
    hexid, err, launched = _room_cell(profile, token)
    if err:
        if not launched or isinstance(err, cell.BinTimeout):
            return None, _signer_failure(err)
        revived, revive_err = _revive()
        if revive_err is None:
            token = revived if revived is not None else token
        hexid, err, launched = _room_cell(profile, token)
        if err:
            if not launched or isinstance(err, cell.BinTimeout):
                return None, _signer_failure(err)
            detail = err if not revive_err else "%s; revive failed: %s" % (
                err, revive_err)
            return None, _diag("join_failed", detail)
    with _node_send_lock() as unlocked:
        return _send_attempts(payload, profile, topic, hexid, token,
                              [unlocked] if unlocked else [])


def _send_attempts(payload, profile, topic, hexid, token, notes):
    """The send and its one fee retry, run while `_sign_send` holds the node's
    send lock (`_node_send_lock`). `notes` follow the reason of EVERY send that
    does not commit, including a signer timeout, which is where a send that
    went out unlocked behind a hung holder lands."""
    from . import cell

    def noted(d):
        if notes:
            d["reason"] = _safe_reason("; ".join([str(d.get("reason") or "")]
                                                 + notes))
        return d
    for attempt in (1, 2):
        rc, out, err = cell.run_bin(
            ["send", "--profile", profile, "--to", hexid,
             "--topic", topic, payload],
            timeout=30, env_extra=_env_extra(token))
        if rc is None:
            return None, noted(_signer_failure(err))
        info = cell._last_json(out)
        if rc == 0 and _complete_send(info):
            info = dict(info)
            info["_helm_signed_at"] = time.time()
            return info, None
        funding = _funding_refusal(rc, out, err)
        if funding is not None:
            return None, noted(_funding_diag(rc, funding, hexid))
        refused = _node_refusal(rc, out, err)
        if attempt == 2 or not (refused and _short_of_fee(refused, hexid)):
            break
        _r, faucet_err = _faucet(hexid)
        if faucet_err:
            # THE ONE CASE THE FAUCET IS THE CAUSE. The node refused this send
            # for its fee and the top-up that would pay it failed: a dry source
            # leads with its own code; any other faucet failure (a rate limit,
            # an unreachable node) stays the node's refusal, with the faucet's
            # answer after it.
            from . import chatnode
            said = _refusal_reason(rc, refused)
            if chatnode.FAUCET_DRY_MARK in faucet_err:
                return None, noted(_diag("faucet_source_dry",
                                         "; ".join([faucet_err, said])))
            return None, noted(_diag("send_failed",
                                     "; ".join([said, "the top-up failed: %s"
                                                % faucet_err])))
        notes.append("the node refused the first send for its fee, the faucet "
                     "paid a top-up, and this is the one retry")
    code = "send_failed" if refused else "send_outcome_unknown"
    reason = (_refusal_reason(rc, refused) if refused else
              "cell send returned rc %s without an unambiguous committed "
              "receipt; outcome unknown" % rc)
    # THE NODE'S OWN REFUSAL LEADS. A faucet code that won whenever the faucet
    # was dry labelled chain-race refusals ("receipt chain mismatch", "nonce
    # replay") as faucet_source_dry and sent the fleet to refuel a faucet
    # those sends never needed. Only a fee refusal whose top-up failed, above,
    # names the faucet.
    return None, _diag(code, "; ".join([reason] + notes))


def _refusal_reason(rc, refused):
    return ("cell send rc %s: the node refused the turn, so it did not "
            "commit: %s" % (rc, refused))


def emit_coordination_turn(topic, payload, profile=None):
    """Emit ONE signed, chain-ordered COORDINATION turn under `topic` on the
    room node — the general, topic-parameterized reuse of chat's signed-emit
    path (helm.land, helm.claim, …). It rides the EXACT same signer/revive/
    faucet/completeness machinery as a chat post (no second signer is forked),
    and the coordination class is fee-free (cell.is_coordination_actions), so
    it never drains a cell.

    Returns (send-info, None) with {turn_hash, receipt_hash, chain_index} on a
    committed turn, else (None, structured diagnostic). FAIL-OPEN is the
    CALLER's contract: a None info means the signer/node was unavailable, and
    the caller must degrade to its pre-signature behavior and NEVER block. A
    raising signing leg is caught here and returned as that same fail-open
    signal, so a coordination emit can never crash the operation it annotates."""
    from . import cell
    # THE IDENTITY GATE, AT SIGNING TIME. `launch.py` already states the law —
    # "a child speaking as `seat` must sign as that same seat, not its
    # launcher" — but enforces it only when it SPAWNS the seat, so any seat
    # adopted or started by another door inherits whatever the ambient
    # environment claims. On this box that is a machine-global shell export
    # naming the OWNER, which is how agent rows came to render as signed by
    # the owner. An explicit `profile` is STATED INTENT and still wins; a
    # DISAGREEMENT between the seat this process provably is and the profile
    # the environment names is refused, because the only alternative to
    # refusing is signing with a key that belongs to someone else.
    reading = ("", "") if profile else cell.seat_reading()
    p, refusal = cell.signing_identity(profile, reading=reading)
    if refusal:
        return None, _identity_refusal(refusal, reading[1])
    try:
        return _sign_send(payload, p, topic=topic)
    except Exception as exc:
        return None, _diag("signing_exception",
                           "%s: %s" % (exc.__class__.__name__, exc))


def _post_identity(profile, admitted=None, admit=True):
    """(label, refusal) for one chat row: who signs it, or whose row it is when
    it may not be signed. `admitted` is the `AdmittedActor` the caller's door
    already minted for this process (the CLI's `_seat_actor`), so a seat under
    the owner's inherited profile is swapped to itself without a second
    admission; `admit=False` is a reader that only wants the LABEL (the label
    is the seat whether the gate swaps or refuses) and consults nothing more.
    `label` is never empty; `refusal` is None or the structured diagnostic to
    stamp (`identity_conflict`, `identity_unreadable`, or `signing_exception`
    when the gate itself raised). Never raises.

    THE POST PATH MUST ASK THE SAME GATE AS THE EMIT PATH. Reading
    `cell.profile_name()` here, the raw HELM_CELL_PROFILE with no identity
    check, lets a machine-wide shell export of the owner's profile speak for
    every seat that inherits it: the gate refuses that process while its posts
    and reactions go out signed with the owner's key.

    ON A CONFLICT THE LABEL IS THE SEAT, NEVER THE PROFILE THE GATE REJECTED.
    The label keys the per-profile incident, and that incident clears only when
    the SAME profile commits a signed turn. Filed under the owner's name, it
    would report his profile as broken, and his next good turn from his own
    session would retire an incident that is about a seat he is not. Filed
    under `helm-agent`, any no-profile process's next good turn would retire
    it. Filed under the seat, it stays until that seat signs as itself.

    THE SEAT IS READ ONCE AND THE LABEL EXISTS BEFORE THE GATE RUNS. The one
    `seat_reading` feeds both the label and the decision, so the two cannot
    disagree about a roster that changed between two reads, and a gate that
    raises still files its failure under the seat.

    BUT ONLY A SEAT THE ROSTER PROVED, OR ONE THIS PROCESS DECLARED, IS A
    LABEL. With HELM_CHAT_NAME unset, the name comes from `derive_seat`: an
    invented auto-name nothing will ever sign as. Filed under it while the
    roster is unreadable, the incident outlived the repair, because the next
    good turn signed as the ambient profile and never retired it; the status
    line and the fleet panel stayed DEGRADED until a manual ack. So an
    undeclared name that the roster cannot prove, and a process that could not
    name itself at all, file under the ambient profile. That process is
    provably not self-identified, and the ambient's next good turn is the
    right one to retire the incident. A real seat among them files its own
    `identity_conflict` on its first post after the repair.

    NO PROFILE AND NO SEAT KEEPS THE OLD ANONYMOUS LABEL. The gate refuses that
    case too ("no profile and no derivable seat"), but nobody's name is claimed:
    `helm-agent` is this box's service identity, not a person. It signs today,
    and refusing it would turn working signed rows into a standing DEGRADED
    incident. Every other refusal needs an ambient profile, so no ambient
    profile means this is that case."""
    from . import cell, home
    ambient = cell.signer_profile()[0]
    seat, unreadable = ("", "") if profile else cell.seat_reading()
    try:
        declared = (home.chat_name() or "").strip()
    except Exception:                    # noqa: BLE001 — a hostile name is
        declared = ""                    # declared by nobody
    own = seat if seat and (not unreadable or seat == declared) else ""
    label = _profile(profile or own or ambient)
    try:
        p, refusal = cell.signing_identity(profile,
                                           reading=(seat, unreadable),
                                           admitted=admitted, admit=admit)
    except Exception as exc:
        return label, _diag("signing_exception", "the identity gate raised "
                            "%s: %s" % (exc.__class__.__name__, exc))
    if not refusal:
        return p, None
    if not ambient:
        return _profile(None), None
    return label, _identity_refusal(refusal, unreadable)


def _own_admission(who):
    """`who` when it is an `AdmittedActor` — the capability the CLI door
    minted for THIS process — else None. The signing gate takes it as the
    identity layer's answer instead of asking again (task/3049), so the
    common post path re-runs no admission. A raw name is not one: a string
    can never stand in for the capability."""
    from . import actors
    return who if isinstance(who, actors.AdmittedActor) else None


def _identity_refusal(refusal, roster_unreadable):
    """The ONE classification of a gate refusal, for the post and the emit
    path alike: `identity_unreadable` when the roster could not say who this
    seat is (a relaunch does not repair a roster), else `identity_conflict`.

    The parameter is not named `unreadable` on purpose: that name marks a
    PROCESS-census consumer to tests/test_orcaadopt.py's census, and this is
    the roster's reason, not a /proc census."""
    return _diag("identity_unreadable" if roster_unreadable
                 else "identity_conflict", refusal)


def _signed_row(row, payload_text, profile, sign, admitted=None):
    """Common signing owner for posts/reactions. The RAM row ALWAYS lands.
    A failed attempted signature is stamped + retained per profile; only an
    observed signed send clears that profile's incident. On the production
    `sign=None` path, an unset signer/node URL is configured-off v1, while a
    configured-but-unusable signer is a `signer_unavailable` incident.
    `sign=True` is the explicit force/test seam and therefore records inability
    to honor that forced attempt; `sign=False` always skips.

    The signer comes from `_post_identity`, the same gate the coordination emit
    uses. A seat whose ambient profile names someone else is stamped
    `identity_conflict` under its own name, and a seat whose roster row cannot
    be read is stamped `identity_unreadable`; neither reaches `_sign_send`.
    Identity is resolved only for a row that will attempt a signature, so a
    configured-off or `sign=False` row reads no roster and raises no incident.
    The gate runs before the signer and node checks, in the order
    `emit_coordination_turn` uses: a row that may not be signed costs no probe."""
    p = profile
    try:
        from . import cell
        if sign is None:
            signer = cell.bin_status()
            if not signer["configured"] or (
                    signer["usable"] and not node_url()):
                return row
        elif not sign:
            return row
        p, refused = _post_identity(profile, admitted=admitted)
        if refused:
            return _stamp_sign_failure(row, p, refused)
        if sign is None:
            if not signer["usable"]:
                return _stamp_sign_failure(
                    row, p, _diag("signer_unavailable", signer["reason"]))
            try:
                live = node_head()
            except Exception as exc:
                return _stamp_sign_failure(
                    row, p, _diag("node_probe_failed", "%s: %s" % (
                        exc.__class__.__name__, exc)))
            if live is None:
                return _stamp_sign_failure(
                    row, p, _diag("node_unreachable",
                                  "chat node unreachable at %s" % node_url()))
        payload = payload_for(row, payload_text)
        info, failure = _sign_send(payload, p)
        if not info:
            return _stamp_sign_failure(row, p, failure)
        if not _complete_send(info):
            return _stamp_sign_failure(row, p, _diag(
                "send_failed", "signer returned no committed signing receipt"))
        signed_at = info.pop("_helm_signed_at", time.time())
        _clear_sign_failure(p, signed_at)
        row.update(turn=info.get("turn_hash"), receipt=info.get("receipt_hash"),
                   chain=info.get("chain_index"), payload=payload)
    except Exception as exc:
        return _stamp_sign_failure(
            row, p or "helm-agent", _diag(
                "signing_exception", "%s: %s" % (exc.__class__.__name__, exc)))
    return row


@contextlib.contextmanager
def _room_lock(room, timeout_s=None):
    """The room's write lock: a STABLE `<room>.lock` sibling, flock'd for the
    whole append+rotation window — every room writer (CLI, web POST, TUI,
    hooks, log-independent) serializes here. Never the jsonl inode itself:
    rotation replaces that inode, which would let a fresh opener bypass a
    lock held on the old one (codex C4). Fail-open: a lock that cannot be
    taken degrades to the unlocked v1 behavior rather than dropping the
    message — the fallback law is drop the GUARANTEE, never the row."""
    import fcntl                  # POSIX advisory lock (Linux fleet)
    lf = None
    try:
        try:
            # the lock file's parent must exist — after a RAM wipe (the
            # meld replay path) chat_dir() may be gone, and a bare open
            # would fail-open to NO LOCK exactly when a restore is
            # rebuilding under it (r4 LOCK).
            os.makedirs(chat_dir(), mode=0o700, exist_ok=True)
            lf = open(os.path.join(chat_dir(), pk.slug(room) + ".lock"), "a")
            from . import hooklatency
            # The outer finally already owns lf cleanup before either telemetry
            # call. This span brackets the WHOLE existing acquisition/poll loop.
            with hooklatency.stage("lock-room") as timing:
                try:
                    if timeout_s is None:
                        fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
                    else:
                        deadline = time.monotonic() + max(0.0, float(timeout_s))
                        while True:
                            try:
                                fcntl.flock(lf.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                                break
                            except BlockingIOError:
                                if time.monotonic() >= deadline:
                                    if timing is not None:
                                        timing.outcome = "timeout"
                                    raise OSError("chat room lock timed out")
                                time.sleep(0.05)
                    if timing is not None:
                        timing.outcome = "acquired"
                except OSError:
                    if timing is not None and timing.outcome != "timeout":
                        timing.outcome = "failed-open"
                    raise
        except OSError:
            if lf is not None:
                lf.close()
            lf = None
        yield lf is not None
    finally:
        if lf is not None:
            try:
                fcntl.flock(lf.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            lf.close()


def _room_destination(room):
    """Canonical storage identity shared by the row id and durable receipt."""
    return os.path.relpath(room_path(room), chat_dir())


def _event_row_id(room, author, event_id):
    body = json.dumps(["helm.chat.event.v1", _room_destination(room), author,
                       event_id], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:12]


def _row_with_id(path, row_id):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            raw = [x for x in f.read().split("\n") if x]
    except FileNotFoundError:
        return None
    for text in raw:
        try:
            row = json.loads(text)
        except ValueError as exc:
            raise ValueError("chat room contains malformed JSON; duplicate "
                             "status is unknown") from exc
        if not isinstance(row, dict):
            raise ValueError("chat room contains a non-row value; duplicate "
                             "status is unknown")
        if row.get("id") == row_id:
            return row
    return None


def _event_receipts_dir():
    """One host-durable ledger root per canonical chat bus.

    Receipt ownership is a pure function of realpath(chat_dir): every process
    sharing that bus selects the same hash bucket regardless of HELM_HOME or
    whether the surface was explicit/derived. HELM_CHAT_EVENT_DIR replaces the
    host root for hermetic or separately isolated estates; it never changes the
    bus key.
    """
    override = home.env("CHAT_EVENT_DIR")
    root = os.path.abspath(os.path.expanduser(override)) if override else \
        os.path.join(home.default_home(), home.GLOBAL, ".state",
                     "chat-event-receipts")
    bus = os.path.realpath(chat_dir())
    key = hashlib.sha256(bus.encode("utf-8")).hexdigest()[:16]
    return os.path.join(root, key)


def _event_receipt_path(room):
    """Durable operation proof, outside the reboot-scoped RAM room tree."""
    return os.path.join(_event_receipts_dir(),
                        _room_destination(room) + ".events.json")


def _legacy_event_receipt_path(room):
    return room_path(room) + ".events.json"


def _read_event_receipts(path):
    try:
        with pk.open_regular(path, encoding="utf-8") as f:
            receipts = json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise ValueError("chat event receipts are unreadable; duplicate status "
                         "is unknown") from exc
    if not isinstance(receipts, dict) or any(
            not isinstance(row_id, str) or not isinstance(row, dict) or
            row.get("id") != row_id for row_id, row in receipts.items()):
        raise ValueError("chat event receipts have an invalid shape; duplicate "
                         "status is unknown")
    return receipts


class _EventReceiptPublishedError(OSError):
    """Receipt is visible, but its directory durability was not proven."""


def _fsync_directory(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _ensure_receipt_dir(path):
    """Create private dirs bottom-up, fsyncing every new parent entry."""
    missing, current = [], path
    while not os.path.exists(current):
        missing.append(current)
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    for directory in reversed(missing):
        try:
            os.mkdir(directory, 0o700)
            created = True
        except FileExistsError:
            created = False
        os.chmod(directory, 0o700)
        if created:
            _fsync_directory(os.path.dirname(directory))
    os.chmod(path, 0o700)


def _prove_event_receipt(path):
    try:
        _fsync_directory(os.path.dirname(path))
    except Exception as exc:
        raise _EventReceiptPublishedError(
            "chat event receipt is visible but directory durability is "
            "unproven") from exc


def _write_event_receipts(path, receipts):
    """Atomic 0600 receipt write below 0700 ledger directories."""
    root = _event_receipts_dir()
    _ensure_receipt_dir(root)
    _ensure_receipt_dir(os.path.dirname(path))
    tmp = "%s.%d.%x.tmp" % (path, os.getpid(), threading.get_ident())
    fd = None
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            fd = None
            f.write(json.dumps(receipts, indent=2, ensure_ascii=False,
                               sort_keys=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        _prove_event_receipt(path)
    finally:
        if fd is not None:
            os.close(fd)
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def _event_receipts(room):
    """Durable receipts plus one in-lock migration from the released RAM path."""
    path = _event_receipt_path(room)
    receipts = _read_event_receipts(path)
    legacy_path = _legacy_event_receipt_path(room)
    legacy = _read_event_receipts(legacy_path)
    if not legacy:
        return receipts
    for row_id, row in legacy.items():
        prior = receipts.get(row_id)
        if prior is not None and prior != row:
            raise ValueError("chat event receipts conflict; duplicate status "
                             "is unknown")
        receipts[row_id] = row
    _write_event_receipts(path, receipts)
    try:
        os.unlink(legacy_path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise ValueError("legacy chat event receipts cannot be retired; "
                         "duplicate status is unknown") from exc
    return receipts


def _append_once(row, room, event_id=None, event_room=None):
    """ONE serialized write path for every room writer: id-stamp and append the
    whole row in one write under the room lock; the wrapper rotates afterwards.
    The stable per-row id is what delivery cursors key on (codex H5); rows
    predating it (or hand-written) simply have no id and never match one.

    A caller-supplied event id is an operation key, not display metadata. Its
    canonical-room+author-scoped deterministic row id is checked under the
    same lock as append, so a crash after append or a concurrent retry returns
    the first row instead of adding another. A small durable receipt preserves
    that proof across room rotation and a reboot before periodic log-flush;
    log-flush also carries the deterministic id into restored transcript rows.
    The caller owns semantic key construction; the first rendering wins. Unlike
    ordinary best-effort chat, this keyed path fails closed when the lock, room,
    or receipt ledger cannot prove uniqueness."""
    receipt_room = event_room or room
    if event_id is None:
        row.setdefault("id", os.urandom(6).hex())
    else:
        row["id"] = _event_row_id(
            receipt_room, row.get("from") or "", event_id)
        row["event"] = 1
    path = room_path(room)
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)  # dm/ lane
    with _room_lock(room, timeout_s=5 if event_id is not None else None) as locked:
        redirect = (_dm_redirect(room)
                    if pk.slug(room).startswith(DM_PREFIX) else None)
        if redirect is not None:
            raise _DMRedirect(redirect)
        receipts = None
        if event_id is not None:
            if not locked:
                raise OSError("chat room lock unavailable; refusing an "
                              "unproven idempotent append")
            receipts = _event_receipts(receipt_room)
            prior = receipts.get(row["id"])
            if prior is not None:
                _prove_event_receipt(_event_receipt_path(receipt_room))
                return prior
            prior = _row_with_id(path, row["id"])
            if prior is not None:
                receipts[row["id"]] = prior
                _write_event_receipts(
                    _event_receipt_path(receipt_room), receipts)
                return prior
        if not row.get("dm"):
            from .seats_receipts import broadcast_census
            census = broadcast_census(row, room)
            if census is not None:
                row["delivery_census"] = census
        encoded = (json.dumps(row, ensure_ascii=False) + "\n").encode("utf-8")
        with open(path, "ab") as f:
            start = f.tell()
            f.write(encoded)
            f.flush()
        if receipts is not None:
            receipts[row["id"]] = row
            # Append precedes the receipt and rotation follows it. A process
            # crash in the gap leaves the row for the next claimant to repair;
            # a synchronous receipt failure rolls back this writer's bytes so
            # an unacknowledged row cannot rotate away prooflessly.
            try:
                _write_event_receipts(
                    _event_receipt_path(receipt_room), receipts)
            except _EventReceiptPublishedError:
                raise
            except Exception:
                try:
                    with open(path, "r+b") as f:
                        f.seek(0, os.SEEK_END)
                        if f.tell() != start + len(encoded):
                            raise OSError("chat room changed after keyed append; "
                                          "unsafe rollback refused")
                        f.seek(start)
                        if f.read() != encoded:
                            raise OSError("keyed append bytes changed; unsafe "
                                          "rollback refused")
                        f.truncate(start)
                except OSError as rollback:
                    raise OSError("chat event receipt failed and the unproven "
                                  "row could not be rolled back") from rollback
                raise
    return row


def _append(row, room, event_id=None):
    """Follow DM redirects; broadcasts freeze roster through physical append."""
    from .seats_common import ROOM_SCAN_CAP, _BROADCAST, _flocked, roster_path
    from .seats_rename import recover_seat_rename

    dm = bool(row.get("dm"))
    broadcast = not dm and _BROADCAST.search(str(row.get("text") or ""))
    rename_guarded = dm or broadcast
    roster_scope = (_flocked(roster_path() + ".lock") if rename_guarded
                    else contextlib.nullcontext())
    result, redirect_error = None, "DM redirect depth exceeds bounded scan"
    with roster_scope:
        if not recover_seat_rename(roster_locked=rename_guarded):
            raise OSError("interrupted seat rename is not recoverable")
        target, seen = room, set()
        for _hop in range(ROOM_SCAN_CAP):
            canonical = pk.slug(target)
            if canonical in seen:
                redirect_error = "DM redirect cycle"
                break
            seen.add(canonical)
            redirect = (_dm_redirect(target)
                        if canonical.startswith(DM_PREFIX) else None)
            if redirect is None:
                try:
                    result = _append_once(
                        row, target, event_id=event_id, event_room=room)
                    break
                except _DMRedirect as moved:
                    target = str(moved)
                    continue
            target = redirect
    if result is None:
        raise OSError(redirect_error)
    _rotate(room_path(target), room=target)
    return result


def resolve_ref(rows, ref):
    """A user-facing message reference -> the parent ROW, or None. Accepts a
    stable row id (exact, or an unambiguous >=4-char prefix — ids are 12 hex
    chars, nobody types those in full) or a 1-based ordinal over the room's
    message rows, negatives from the end (`-1` = latest, the same ordinal the
    `react` verb already speaks). Reaction rows are never parents."""
    msgs = [m for m in rows if not m.get("react")]
    ref = str(ref or "").strip()
    if not ref:
        return None
    # IDENTITY FIRST, ordinal only as the fallback: ids are hex, so ~6% of
    # 6-char id prefixes are all-digits and a digits-first rule silently read
    # them as message numbers (caught as a 1-in-16 flake in the full suite).
    # Matching the id the user actually copied can never be the wrong answer.
    hit = [m for m in msgs if m.get("id") == ref]
    if not hit and len(ref) >= 4:
        hit = [m for m in msgs if str(m.get("id") or "").startswith(ref)]
    if len(hit) == 1:
        return hit[0]
    if hit:
        return None                     # ambiguous prefix = no parent, no guess
    if len(ref) <= 7 and ref.lstrip("-").isdigit() and ref.lstrip("-"):
        n = int(ref)
        if n and -len(msgs) <= n <= len(msgs):
            return msgs[n - 1 if n > 0 else n]
    return None


def _parent_fields(room, ref):
    """Parent fields for one reference — the ONE place a ref becomes a
    row-shaped pointer. An unresolvable ref (already rotated out, or a typo)
    still records the literal ref: the reply lands, renders as an orphan, and
    nothing crashes (fallback law — drop the LINK, never the message).

    Rows predating stable ids also carry `rtext`. Their (ts, from) pair is not
    unique when one author posts twice inside a second; the exact parent text
    disambiguates the surviving rows and rides the signed digest."""
    p = resolve_ref(read(room)[0], ref)
    if not p:
        return {"reply_to": str(ref)}
    out = {"reply_to": p.get("id") or "", "rts": p.get("ts") or "",
           "rfrom": p.get("from") or ""}
    if not p.get("id"):
        out["rtext"] = p.get("text") or ""
    return out


def _default_post_room():
    """The room a caller gets when it names NONE — derived, never a literal.

    room=\"main\" as the parameter default WAS the hardcode under every
    room-partition symptom of 2026-07-29: eleven call sites (watchdog,
    autocompact, resume-turn, verdict attestations, …) relied on it and
    posted fleet traffic into the one room the fleet does not work in; the
    six timer units were then \"fixed\" at the PROCESS layer (WorkingDirectory)
    to make cwd derive a project — a workaround whose only job was keeping
    this default unreached. Fix the shared mechanism, not the eleventh
    instance: None now resolves through resolve_homing — THE one home-room
    precedence (env seam, then cwd project) — so a caller inherits the room
    its process is actually working in. Reserved #main stays the honest
    last resort for a genuinely project-less helm, and an EXPLICIT
    room=\"main\" is untouched: deliberate centralization keeps working."""
    try:
        from . import seats
        room, _source = seats.resolve_homing(cwd=seats.safe_cwd())
        return room or "main"
    except Exception:
        return "main"              # homing must never break a post


def post(text, room=None, who=None, profile=None, sign=None, origin=None,
         dm=None, dm_display=None, ambient=False, reply_to=None, ack=None,
         ackstate=None, verdict=None, event_id=None):
    """Append one message; returns it. v2: shortcodes expand, and when the
    room node answers the digest rides a signed self-write turn FIRST — the
    row carries {turn, receipt, chain}. Node down -> plain v1 row (rendered
    with the [unsigned] tag). O(1) append; rotation only past the cap.

    `origin` marks the WRITE RAIL, not the author: the server-side owner
    surfaces stamp "web"/"tui" and ONLY those rows get the delivery lane's
    owner-rule (a CLI post claiming an owner name delivers as an ordinary
    mention). ADVISORY by design — the room dir is same-uid 0700 tmpfs, so
    any local process could forge the field; what it defends against is the
    real threat here: an agent (or a prompt-injected one) impersonating the
    owner through legit tooling. Principal crypto stays dregg's.

    `dm` names the ONE recipient. This final write seam canonicalizes it even
    for direct callers that bypass seats.dm: {dm} stores the unconditional
    casefolded exact-token routing key, while {dm_display} keeps roster/original
    casing for rendering. The row lands in that recipient's private lane,
    OVERRIDING `room` — a DM never touches a room file (no fanout).

    `ambient` stamps the row NON-WAKING: it renders on every read surface
    (the room, `helm chat read`, the web feed) but seats.deliverable() drops
    it before any wake rule — including the home-room rule, which otherwise
    hands EVERY plain row in a team channel to every seat homed there. It is
    the class for machine status a teammate PULLS (the todo mirror), never
    the class for a word addressed to anyone; a mention or DM must not use
    it (and does not: only the mirror passes ambient=True).

    `reply_to` is a parent REFERENCE (row id, id prefix, or ordinal) resolved
    against the room the row lands in: the row gains {reply_to, rts, rfrom}
    (plus rtext for a pre-id parent) and, when signed, a parent-bound digest.
    A reply WAKES the parent row's author (seats.deliverable reads rfrom,
    casefold, mention-tier — before mute, any room): replying is a direct
    address, the owner's stated substitute for typing @names. Only the
    parent's author wakes; every other seat sees exactly what the text
    alone would have reached.

    Every non-DM `@token` also carries an `addressees` capability snapshot in
    the returned AND persisted row. That is the machine disclosure surface:
    automation does not need to scrape CLI prose, and later readers see the same
    tri-state/evidence fact the author saw when the row landed. Broadcast tokens
    stay fanout vocabulary, not fictional seat recipients.

    `event_id` is an optional caller-owned operation key for retryable machine
    announcements. The canonical room+author namespace derives one deterministic
    row id;
    under the room lock, a retry returns the first row without appending again.
    Callers must derive it from the immutable logical event and use `sign=False`
    unless their external signed transport is independently idempotent."""
    if event_id is not None and (
            not isinstance(event_id, str) or not event_id or len(event_id) > 256):
        raise ValueError("chat event_id must be a non-empty string of at most "
                         "256 characters")
    if room is None:
        # None means DERIVE (the caller's own project room); an explicit
        # "main" keeps meaning main. A DM overwrites this below either way.
        room = _default_post_room()
    if not dm:
        # the write-side half of the auto-restore latch: tonight's fleet-dark
        # window had proxywatch post row [1] while every agent was down —
        # list_rooms never ran, and a latch that only reads would never have
        # fired. A DM skips it: private lanes are never journaled, so there
        # is nothing to restore and no reason to pay the scan.
        _auto_restore_once()
    _ensure_dir()
    from . import emoji
    text = emoji.expand(text)
    # THE PROSE SHA CHECK, at the ONE funnel. Every send path helm has —
    # `chat post`, `chat reply`, `chat dm` (seats.dm), the meld/council/standup
    # `say`, the web panel's /api/chat, the owner TUI, the dispatch verdict
    # announcement and every machine announcer — reaches this line, so the
    # guard is spelled here exactly once. Putting a second copy in cmd_chat
    # would mean neither could ever be measured: two checks rejecting the same
    # input mask each other under mutation. Warn only, after expansion (the
    # text that will actually be READ), before the row is built: shaguard.warn
    # cannot raise and cannot refuse.
    from . import shaguard
    if shaguard.refuse(text):
        # A PADDED SHA IS THE ONE FINDING THIS GUARD CAN PROVE, and prose is
        # where a reader picks a tip up and binds work to it. Warning let one
        # through on 2026-08-04: it reached a teammate holding a build row and
        # made their merge probe report a CONFLICT against a rev that does not
        # exist. Refuse BEFORE the row is built, so nothing durable records the
        # wrong sha; every ambiguous finding still only warns.
        # RAISING IS DELIBERATE, AND SO IS WHO CATCHES IT. Of the 23 callers,
        # the two HUMAN surfaces handle it — human.py turns it into a TUI
        # notice and hands the text back, web.py answers 400 — because
        # crashing the surface an operator types into would be a worse trade
        # than the bug. The machine announcers (seats.py, meld.py) do NOT
        # catch it on purpose: an agent posting a fabricated sha SHOULD fail
        # loudly, which is the entire point.
        from . import friction
        friction.record("shaguard", reason="padded-sha")
        raise ValueError(
            "helm chat: refusing to post a padded short sha — resolve it "
            "($(git rev-parse <prefix>)) or set %s=1 to quote it deliberately"
            % shaguard.SKIP_ENV)
    shaguard.warn(text)
    # `who` may be an ADMITTED ACTOR (what every CLI/act door hands down now)
    # or an explicit on-behalf-of name (a bot, the web surface naming itself).
    # actors.name_of unwraps the first and passes the second through: a
    # capability must never be stringified into a durable row by accident, so
    # it does not render as its own name.
    #
    # THE AMBIENT FLOOR STAYS, and the classification is why: a bare post is
    # SPEECH, and `seats.auto_name` exists precisely so an un-named join can
    # speak as a meaningful project+family name. What must not run on a minted
    # name is an ACT — a DM, an ack, a verdict, a lease, a cursor — and each of
    # those has its own door (seats.dm / seats.ack / council / the claim verbs)
    # where an AdmittedActor is required. Refusing here instead would have made
    # the first honest word of every fresh seat impossible.
    row = {"ts": pk.now_ts(),
           "from": _actor_label(who) or whoname(), "text": text}
    if not dm:
        capabilities = _post_addressee_capabilities(text)
        if capabilities:
            # THE MACHINE SURFACE IS THE ROW ITSELF. Direct/automated callers
            # receive this dict, and _append persists the identical JSON shape;
            # the CLI below renders from it after landing. One capability read
            # therefore serves automation, durability and the human sentence.
            row["addressees"] = capabilities
    if dm:
        from . import seats
        dm, err = seats._canonical_recipient(dm, display=dm_display)
        if err:
            raise ValueError("helm chat: " + err)
        row["dm"] = str(dm)
        row["dm_display"] = dm.display
        room = dm_room(dm)
    if origin:
        row["origin"] = origin
    if ambient and not dm:
        row["ambient"] = 1
    if ack:
        # the ACK / CONSUME LADDER row (seats.ack): a NON-waking marker that
        # names the target row it closes — seats.deliverable drops it like a
        # reaction, and the SENDER reads it off `helm chat pending`. The note
        # (if any) is the text; a blocked ack carries its reason there.
        row["ack"] = ack
        row["ackstate"] = ackstate or "done"
    if reply_to:
        row.update(_dm_parent_fields(room, reply_to) if dm else
                   _parent_fields(room, reply_to))
    if verdict and (reply_to or ack):
        raise ValueError("a verdict turn is its own shape — it cannot be a "
                         "reply or an ack (shapes are mutually exclusive so "
                         "no semantics ride outside the signed claim)")
    if verdict:
        # The dispatch-verdict binding (bd-173ca9): lane+tip+rid+ref become
        # ROW FIELDS and, via payload_for's shape dispatch, part of the signed
        # claim itself — a verdict turn attests, it does not merely mention.
        row["vlane"] = str(verdict.get("lane") or "")
        row["vtip"] = str(verdict["tip"])
        row["vrid"] = str(verdict["rid"])
        row["vref"] = str(verdict.get("ref") or "")
    _touch_poster_presence(row["from"])
    return _append(_signed_row(row, text, profile, sign,
                               admitted=_own_admission(who)), room,
                   event_id=event_id)


def _touch_poster_presence(name):
    """Presence-on-post: a seat that SPEAKS is alive, beacon or no beacon — so
    keep its roster row fresh and the reaper never drops a live-but-idle poster
    (roster-truth invariant). Best-effort + local import (chat<-seats would
    cycle).

    A NAME THAT IS NOT A SEAT GETS NO BEAT AND NO COMPLAINT. Three kinds of
    name reach this door: a seat, an owner/broadcast name, and a SUBSYSTEM
    LABEL — `post(..., who="dispatches")`, "proxywatch", "resume-turn",
    "autocompact" and their siblings, which exist so the room can see which
    subsystem spoke. Only the first can hold a presence file.

    A SUBSYSTEM LABEL MUST NOT REACH `touch_seen`, WHICH ANSWERS A DIFFERENT
    QUESTION. That guard asks whether the name is THIS PROCESS'S seat; a
    subsystem label never is, so a seat posting under one trips
    `_warn_foreign` and prints a cross-seat identity refusal about a write
    that was never attempted and is not possible.

    THE STAKE IS THE CHANNEL, NOT THE BEAT. `foreign_seat` declines correctly
    and no presence file is minted either way. But `_warn_foreign` exists to
    be loud about a REAL identity violation, and its own docstring says
    silence is what let that class run undetected; a line that also fires on
    an ordinary dispatch verdict teaches every reader to skip the one line
    that must never be skipped.

    SO THE QUESTION IS "IS THIS A SEAT AT ALL", NOT "IS IT MINE", AND IT IS A
    TRI-STATE. A roster that lists the name means a foreign beat on it IS a
    real violation and must still be loud — `who=` is seat-valued at many
    call sites, not only literal, so a process CAN post under another seat's
    name and that is exactly what the warning is for. A roster that provably
    lists no such name means a subsystem label: skip, silently. A roster that
    cannot be READ proves nothing about who exists, so it falls through to the
    old behaviour and lets the guard speak — collapsing unreadable into
    not-a-seat would silence real violations on a transient read failure.
    """
    try:
        from . import seats
        if not name or name.lower() in seats.owner_names():
            return
        # THE PROCESS'S OWN NAME NEVER CONSULTS THE ROSTER. A seat that
        # speaks before it is rostered still beats, exactly as before — the
        # roster question is about names that are NOT mine, which is the only
        # population the cross-seat warning was ever about. Gating the own
        # case on the roster would silently stop presence-on-post for a seat
        # whose row has not been written yet, which is a live seat with a
        # cold dot rather than a quieter diagnostic.
        if not seats.foreign_seat(name):
            seats.touch_seen(name)
            return
        rows, failed = seats.roster_acquired()
        if not failed and name.lower() not in {str(k).lower() for k in rows}:
            return               # provably not a seat: a subsystem label
        seats.touch_seen(name)
    except Exception:
        pass


def react_ordinals(rows):
    """Map each row's LIST POSITION -> its 1-based react target ordinal (the
    `n` for `helm chat react n`), or None for a reaction row (not targetable).

    This is the SAME filter react() resolves against (non-react rows), so the
    `[n]` that `read` prints beside a row is EXACTLY the number `react n`
    toggles — reactions interleaved above notwithstanding. Without it the two
    index spaces diverged silently: `read` renders every row (reaction lines
    included) while `react n` counts only non-react rows, so a human counting
    the printed lines lands off-by-(reactions-above) —
    landed on the wrong post."""
    out, n = {}, 0
    for i, m in enumerate(rows):
        if m.get("react"):
            out[i] = None            # a reaction row: shown, but never a target
        else:
            n += 1
            out[i] = n
    return out


def react_prefix(rows):
    """A `tag(i)` fn that renders row i's read prefix: `[n] ` for a react
    target, an aligned blank for a reaction row (width-matched to the widest
    ordinal). ONE owner so `read` and `read --follow` print the SAME [n] the
    `react n` verb targets — the two read surfaces can never drift apart."""
    ords = react_ordinals(rows)
    w = max((len("[%d]" % o) for o in ords.values() if o), default=0)

    def tag(i):
        o = ords.get(i)
        return (("[%d]" % o).rjust(w) if o else " " * w) + " "
    return tag


def react(target, code, room="main", who=None, profile=None, sign=None):
    """TOGGLE a reaction. target: 1-based message ordinal (the `[n]` shown by
    `helm chat read`; negatives count from the end) or an explicit (ts, from)
    pair (the web panel's form).
    `code` is a :shortcode: or a raw emoji. Returns (row, None) or
    (None, reason). Rides the same transport as a post.

    Idempotent toggle: re-reacting with the same (reactor, emoji) on the same
    target appends a TOMBSTONE row ({un: true}) — first click adds, second
    removes, never a duplicate (the standard reaction UX; a UI re-render or
    retry can no longer mint phantom counts). The reactor is `who` (the seat
    threaded through by the caller — web/TUI name themselves, the CLI passes
    --seat) with whoname() only as the ambient agent fallback, and the signed
    digest binds that identity: react|tts|tfrom|emoji|reactor. Fail-open —
    no signer just means the row renders [unsigned], attested vs not stays
    distinguishable."""
    e = (code or "").strip()
    from . import emoji
    if not any(ord(c) > 127 for c in e):   # shortcode form -> expand it
        e = emoji.expand(e if e.startswith(":") else ":%s:" % e.strip(":"))
    if not e or len(e) > 8 or any(ord(c) < 128 for c in e):
        return None, "unknown emoji %r (see helm/emoji.py MAP)" % code
    rows, _total = read(room)
    msgs = [m for m in rows if not m.get("react")]
    if isinstance(target, (list, tuple)):
        tts, tfrom = target
        if not any(m.get("ts") == tts and m.get("from") == tfrom for m in msgs):
            return None, "no such message %s@%s (rotated out?)" % (tfrom, tts)
    else:
        if not msgs or not -len(msgs) <= target <= len(msgs) or target == 0:
            return None, "message %s out of range (room has %d)" % (target, len(msgs))
        t = msgs[target - 1 if target > 0 else target]
        tts, tfrom = t.get("ts"), t.get("from")
    _ensure_dir()
    # SAME UNWRAP as post: a capability from the CLI door, an explicit
    # on-behalf-of name from the web/TUI, or the ambient speech floor.
    reactor = _actor_label(who) or whoname()
    on = _react_state(rows).get(("%s|%s" % (tts, tfrom), reactor, e))
    row = {"ts": pk.now_ts(), "from": reactor, "react": e,
           "tts": tts, "tfrom": tfrom}
    if on:
        row["un"] = True   # toggle OFF — the tombstone every renderer honors
    # the payload is derived from the ROW (payload_for -> _react_payload), so
    # the signer and `helm chat verify` read one definition, never two
    return _append(_signed_row(row, None, profile, sign,
                               admitted=_own_admission(who)), room), None


def _react_state(rows):
    """(target-key, reactor, emoji) -> True while the reaction is ON — the
    LAST row wins. Any pile of duplicated add rows (the pre-toggle bug's
    residue) is still just ON, and one tombstone turns the whole pile OFF:
    render self-heals the historical data instead of migrating it."""
    state = {}
    for m in rows:
        if m.get("react"):
            state[("%s|%s" % (m.get("tts") or "", m.get("tfrom") or ""),
                   m.get("from") or "", m["react"])] = not m.get("un")
    return state


def _rotate(path, cap=None, room=None, state_guarded=False, room_locked=False):
    """Install compacted bytes under delivery-state then room serialization."""
    cap = SIZE_CAP if cap is None else cap
    try:
        from . import proxywatch, seats
        from .seats_cursor import (cleanup_rotation_temp,
                                   rotation_journal_path,
                                   rotation_temp_path)
        if room is None:
            stem = os.path.basename(path).removesuffix(".jsonl")
            room = (DM_PREFIX + stem if os.path.basename(
                os.path.dirname(path)) == "dm" else stem)
        if os.path.getsize(path) <= cap and not os.path.exists(
                rotation_journal_path(room)):
            return False
        if not state_guarded:
            with proxywatch.delivery_state_guard():
                return _rotate(path, cap=cap, room=room, state_guarded=True)
        if not room_locked:
            with _room_lock(room) as locked:
                return locked and _rotate(
                    path, cap=cap, room=room, state_guarded=True,
                    room_locked=True)
        if not seats.recover_room_rotation(room, room_locked=True):
            return False
        if os.path.getsize(path) <= cap:
            return False
        state, err = proxywatch._read_watch_state()
        observed = state if not err else []
        with open(path, "rb") as f:
            st = os.fstat(f.fileno())
            data = f.read()
            records, start = [], 0
            while start < len(data):
                end = data.find(b"\n", start)
                if end < 0:
                    records.append((data[start:], start))
                    break
                records.append((data[start:end + 1], start))
                start = end + 1
            cut = len(records) // 2
            offsets = [start for _raw, start in records]
            if room is None:
                stem = os.path.basename(path).removesuffix(".jsonl")
                room = (DM_PREFIX + stem if os.path.basename(
                    os.path.dirname(path)) == "dm" else stem)
            hold = seats.rotation_hold_offset(room, st.st_dev, st.st_ino,
                                              observed)
            if hold is not None and offsets:
                keep = min(offsets[cut] if cut < len(offsets) else len(data),
                           max(0, hold))
                cut = next((i for i, off in enumerate(offsets) if off >= keep),
                           len(records))
            cut_offset = offsets[cut] if cut < len(offsets) else len(data)
            # A cut at 0 drops nothing (a paused hold at 0, or one record).
            # Rotating anyway wrote the same bytes to a new inode and remapped
            # every cursor under the state guard on every post (task/2931).
            if not cut_offset:
                return False
            retained_starts = offsets[cut:]
            if not cleanup_rotation_temp(room):
                return False
            tmp = rotation_temp_path(room)
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(b"".join(raw for raw, _off in records[cut:]))
                    f.flush()
                    os.fsync(f.fileno())
                new = os.stat(tmp)
                dropped, retained = [], []
                for raw, old_start in records:
                    body = raw[:-1] if raw.endswith(b"\n") else raw
                    try:
                        row = json.loads(body.decode("utf-8"))
                    except (UnicodeError, ValueError):
                        continue
                    row_id = row.get("id") if isinstance(row, dict) else None
                    if not row_id:
                        continue
                    old = seats._occurrence(st.st_dev, st.st_ino, old_start)
                    if old_start < cut_offset:
                        dropped.append((row_id, old))
                    else:
                        retained.append((row_id, old, seats._occurrence(
                            new.st_dev, new.st_ino, old_start - cut_offset)))
                from .seats_cursor import (clear_rotation_journal,
                                           prepare_rotation_journal)
                from .seats_receipts import (
                    finish_room_receipt_remap, prune_room_receipts,
                    remap_room_receipts, rollback_room_receipt_remap)
                prepare_rotation_journal(
                    room, st.st_dev, st.st_ino, new.st_dev, new.st_ino,
                    cut_offset, retained_starts, receipts=retained,
                    dropped=dropped, temp=tmp)
                remapped = remap_room_receipts(room, retained)
                if not remapped:
                    raise OSError("receipt rotation remap failed")

                def prepare(cursors, _updates):
                    prepare_rotation_journal(
                        room, st.st_dev, st.st_ino, new.st_dev, new.st_ino,
                        cut_offset, retained_starts, cursors=cursors,
                        receipts=retained, dropped=dropped)

                def install():
                    os.replace(tmp, path)
                    _fsync_directory(os.path.dirname(path))

                committed = seats.remap_rotated_cursors(
                    room, st.st_dev, st.st_ino, cut_offset,
                    new.st_dev, new.st_ino, retained_starts,
                    prepare=prepare, install=install)
                if not committed:
                    rollback_room_receipt_remap(room, retained)
                    raise OSError("cursor rotation transaction failed")
                if not finish_room_receipt_remap(room, retained) \
                        or not prune_room_receipts(room, dropped):
                    raise OSError("receipt rotation finalize failed")
                if not clear_rotation_journal(room):
                    raise OSError("rotation journal cleanup failed")
            finally:
                try:
                    os.unlink(tmp)
                except FileNotFoundError:
                    pass
        return True
    except Exception:
        return False


def _msg(raw):
    try:
        m = json.loads(raw)
    except ValueError:
        return None
    return m if isinstance(m, dict) else None


def read_checked(room="main", since=0):
    """(rows[since:], total, fault) — `read`, plus WHY the answer may be short.

    read() FAILS OPEN TWICE OVER, and both are right for the caller it was
    written for. An unreadable room returns ([], 0), which is the same answer
    an empty room gives; and a torn line is skipped in silence. A live tail
    MUST behave that way — a poller that dies on one bad line stops the fleet
    for a byte.

    They are wrong for a caller proving a row ABSENT, because the row it
    cannot find is precisely the one that may have been dropped. "No message
    matches that id" over a lane nothing could read sends someone to re-type a
    perfectly good id. Same shape as roster_checked over the fail-open
    roster(), and as checked_events over a missing ledger: the primitive keeps
    its forgiving contract and a SECOND door answers the stricter question.

    THE FAULT IS A BARE STRING, NOT A STRUCTURE, and that is deliberate:
    seats_common (where _Fault lives) reads the chat dir, so importing it here
    would close a cycle. Callers wrap it in their own fault type.

    A MISSING FILE IS NOT A FAULT. A room nobody has posted to is PROVEN
    empty, exactly as a missing roster is a proven-empty roster; only a room
    that exists and will not open is unknown. Collapsing those two is the
    error this whole family of readers exists to stop.
    """
    try:
        with open(room_path(room), "rb") as f:
            data = f.read()
    except FileNotFoundError:
        return [], 0, None
    except OSError:
        return [], 0, "unreadable"
    # DECODING IS ITS OWN FAULT, AND errors="replace" HID IT COMPLETELY.
    # The reader used to decode with replacement inline. Invalid UTF-8 inside a
    # row becomes U+FFFD while the JSON around it stays perfectly valid, so
    # every line still parses, len(msgs) == len(raw), and this reported NO
    # FAULT. If the corruption lands in an ID, `_locate_row` then issues its
    # confident "no message matches" over a row whose identity was silently
    # rewritten — the exact false absence read_checked exists to prevent, on
    # the one input where the row is present and unrecognisable.
    #
    # STRICT FIRST, THEN REPLACE. read() must keep the forgiving text (a live
    # tail cannot die on one bad byte), so the replacement decode still
    # happens — but the STRICTER door reports that it happened.
    fault = None
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text, fault = data.decode("utf-8", "replace"), "undecodable"
    # exactly "\n", never splitlines() — a message carrying U+2028 (a voice
    # paste can) must not tear its row for every reader (found 2026-07-20)
    raw = [x for x in text.split("\n") if x]
    msgs = [m for m in map(_msg, raw) if m]
    total = len(msgs)
    # _msg answers None ONLY for invalid JSON or a non-dict — there is no
    # legitimate line shape it drops — so a short count IS torn data and this
    # cannot fire on a healthy room. Checked before wiring it to a refusal:
    # a spurious fault here would make every ack refuse fleet-wide.
    # A DECODE fault outranks it: undecodable bytes are why the rows are torn.
    if fault is None and len(msgs) != len(raw):
        fault = "torn-rows"
    return msgs[since if 0 <= since <= total else 0:], total, fault


def read(room="main", since=0):
    """(rows[since:], total) — the ONE poll primitive: the CLI read, --follow,
    the web GET and the TUI all sit on this. Rows are messages AND reaction
    rows (renderers aggregate — thread()). A since past the end (the room
    rotated) resets to 0 so a poller re-syncs instead of starving;
    unparseable lines are skipped, never fatal.

    UNCHANGED BY read_checked, deliberately: it delegates and DROPS the fault,
    so every existing caller gets byte-identical answers — a missing room and
    an unreadable one both still return ([], 0) here. Callers that cannot
    accept that call read_checked instead."""
    rows, total, _ = read_checked(room, since)
    return rows, total


def rkey(m):
    """A row's reaction-target identity: ts|from (what react rows reference)."""
    return "%s|%s" % (m.get("ts") or "", m.get("from") or "")


def thread(rows):
    """rows -> (messages, reacts) where reacts["ts|from"] = {emoji: count} —
    the aggregation the TUI and any batch renderer draws under each message.
    Counts are per-REACTOR (dedup by (from, emoji), tombstones drop the pair):
    duplicate historical rows collapse to one and toggled-off reacts vanish."""
    msgs = [m for m in rows if not m.get("react")]
    reacts = {}
    for (k, _reactor, e), on in _react_state(rows).items():
        if on:
            reacts.setdefault(k, {})
            reacts[k][e] = reacts[k].get(e, 0) + 1
    return msgs, reacts


def react_line(counts):
    return "  ".join("%s×%d" % (e, counts[e]) for e in sorted(counts))


# ---------------------------------------------------------------------------
# threading: ONE index, ONE resolver — every renderer (CLI, --follow, journal,
# the web endpoint) resolves a parent the same way, so an orphan looks the same
# everywhere and nothing anywhere has to trust reply_to blindly.
# ---------------------------------------------------------------------------

def tkey(m):
    """A row's THREAD key: its stable id, else exact (ts, from, text). Never
    rkey alone — two pre-id rows from one seat inside the same second share a
    ts|from, so an rkey-keyed reply count leaks onto the sibling even when the
    parent resolver chose correctly. Reaction targeting keeps rkey."""
    return m.get("id") or FIELD_SEP.join((rkey(m), _reply_field(m.get("text"))))


def index_rows(rows):
    """{"by_id", "by_key", "replies"} for a room's rows. `by_key` retains
    EVERY ts|from twin; first-writer-wins would make a pre-id reply quote the
    wrong sibling. `replies` counts resolved children per parent tkey — an
    orphan reply is never counted against a row that isn't there."""
    idx = {"by_id": {}, "by_key": {}, "replies": {}}
    for m in rows:
        if m.get("react"):
            continue
        if m.get("id"):
            idx["by_id"][m["id"]] = m
        idx["by_key"].setdefault(rkey(m), []).append(m)
    for m in rows:
        p = parent_of(m, idx)
        if p is not None:
            k = tkey(p)
            idx["replies"][k] = idx["replies"].get(k, 0) + 1
    return idx


def parent_of(m, idx):
    """The row `m` replies to, or None (never posted here / rotated out). The
    stable id wins. A pre-id parent resolves only when its remembered
    (ts, from, text) identifies exactly one surviving row.

    A non-empty reply_to NAMES one row, so if that row is gone the parent is
    gone: resolving its ts|from TWIN instead would quote a DIFFERENT message
    under the reply and hang a phantom ↩N on an innocent row. The same rule
    applies to pre-id rows: (ts, from) alone is ambiguous even before rotation,
    and after rotation cannot prove which twin disappeared. New pre-id replies
    therefore record `rtext`; a malformed/older pointer without it stays an
    orphan rather than guessing (bug-class orphan-resolves-to-same-second-twin)."""
    if not is_reply(m):
        return None
    pid = m.get("reply_to")
    if pid:
        return idx["by_id"].get(pid)
    if not m.get("rts") or "rtext" not in m:
        return None
    rows = idx["by_key"].get("%s|%s" % (m.get("rts") or "",
                                        m.get("rfrom") or ""), [])
    hits = [p for p in rows if (p.get("text") or "") == m.get("rtext")]
    return hits[0] if len(hits) == 1 else None


def _snip(text, cap=QUOTE_CHARS):
    """One line of a parent's text, clipped — a quote never reflows the pane."""
    s = " ".join((text or "").split())
    return s if len(s) <= cap else s[:cap - 1] + "…"


def quote_of(m, idx):
    """(author, snippet) of m's parent for the one-level quote, or None when
    m is not a reply. An ORPHAN answers the remembered author (or "?") with an
    explicit rotated-out snippet — it renders, it never raises."""
    if not is_reply(m):
        return None
    p = parent_of(m, idx)
    if p is None:
        return (m.get("rfrom") or "?", "(parent rotated out)")
    return (p.get("from") or "?", _snip(p.get("text") or ""))


def mark_owner_unread(room="main"):
    """The owner posted (the web surface calls this): drop the marker carrying
    the message count at post time — the shipped reflex fires on its existence."""
    _ensure_dir()
    pk.atomic_write(marker_path(room), str(read(room)[1]))


def consume(room="main", total=None):
    """A read reached `total` messages — clear the owner-unread marker once the
    reader has seen past the owner's post. True iff cleared."""
    mp = marker_path(room)
    try:
        with open(mp) as f:
            mark = int(f.read().strip() or 0)
    except (OSError, ValueError):
        return False
    if total is not None and total < mark:
        return False
    try:
        os.remove(mp)
    except OSError:
        return False
    return True


_MONTH_NAMES = ["???", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
                "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _day_stamp(ts):
    """HH:MMZ for today's rows; Mmm DD HH:MMZ for any other day.

    Stateless — callers need no day-boundary tracking across iterations.
    A single-row echo (post/react) works identically to the multi-row read
    loop. Malformed timestamps return '--:--', same contract as _hhmmz."""
    s = str(ts or "")
    if len(s) < 16:
        return "--:--"
    hhmm = s[11:16]
    if not hhmm:
        return "--:--"
    today = time.strftime("%Y-%m-%d", time.gmtime())
    if s[:10] == today:
        return hhmm + "Z"
    try:
        m = int(s[5:7])
        month = _MONTH_NAMES[m] if 1 <= m <= 12 else "???"
    except (ValueError, IndexError):
        month = "???"
    return "%s %s %sZ" % (month, s[8:10], hhmm)


def _hhmmz(ts):
    """HH:MMZ for today, Mmm DD HH:MMZ for another day, or '--:--'.

    One helper so a reaction row and its parent cannot disagree about which
    clock they are printed in — the failure that motivated the marker was
    exactly a comparison between two timestamps nobody had checked were the
    same clock. With the day marker, the comparison extends to which DAY they
    are printed in."""
    return _day_stamp(ts)


def _transport_tag(m):
    """The row-local signing truth. Attempted fallbacks are loud and precise;
    configured-off v1 rows keep the ordinary [unsigned] fact."""
    if m.get("chain") is not None:
        return ""
    t = _public_transport(m.get("transport"))
    if isinstance(t, dict) and t.get("state") == "DEGRADED":
        return " [DEGRADED %s/%s: %s]" % (
            t.get("profile") or "?", t.get("code") or "signing",
            t.get("reason") or "unknown failure")
    return " [unsigned]"


def _fmt(m, hhmm=True, idx=None):
    """One row, rendered. Signed rows (a recorded chain receipt) print clean;
    configured-off rows carry [unsigned], attempted signing fallbacks carry the
    precise DEGRADED diagnostic.

    With an `idx` (index_rows of the room) the one-level thread renders too: a
    reply carries a compact ↳author "quote" of its parent, and a parent carries
    ↩N, its reply count. Without one — a single-row echo, an old caller — the
    line is byte-identical to before."""
    ts = str(m.get("ts") or "")
    # HH:MM + the ZONE MARKER. Rows are stamped UTC (pk.now_ts uses gmtime),
    # but the slice used to drop the Z, so the fleet's primary coordination
    # surface printed a bare "09:27" on a box whose wall clock said 02:27 —
    # seven hours of silent ambiguity against every OTHER timestamp an agent
    # reads, because stat, ps and date are all LOCAL. Measured 2026-07-25: a
    # seat compared a chat stamp to an mtime by eye, concluded a config had
    # been unchanged for hours when it had been written ninety seconds
    # earlier, and publicly told the integrator to stop doing the right
    # thing. One character makes the two clocks distinguishable on sight.
    stamp = _day_stamp(ts) if hhmm \
        else (ts or "?")
    tag = _transport_tag(m)
    # identity fields are display-laundered (_dsan) so a name column can never
    # reshape the terminal — defense-in-depth beneath the validated join seam.
    if m.get("react"):
        return "%s %s %s %s -> %s@%s%s" % (
            stamp, _dsan(m.get("from") or "?"),
            "un-reacted" if m.get("un") else "reacted",
            m["react"], _dsan(m.get("tfrom") or "?"),
            _hhmmz(m.get("tts")), tag)
    if m.get("ack"):        # the consume-ladder ACTED marker (seats.ack)
        note = (": " + (m.get("text") or "")) if m.get("text") else ""
        return "%s %s ACK %s -> %s%s%s" % (
            stamp, _dsan(m.get("from") or "?"),
            str(m.get("ackstate") or "done").upper(),
            str(m.get("ack") or "")[:8], note, tag)
    q = quote_of(m, idx) if idx else None
    quote = ' ↳%s "%s"' % (_dsan(q[0]), q[1]) if q else ""
    n = (idx or {}).get("replies", {}).get(tkey(m), 0)
    thread_tail = " ↩%d" % n if n else ""
    if m.get("dm"):     # a DM row is a DM everywhere it renders — never a
        return "%s %s%s -> @%s (dm): %s%s%s" % (
            stamp, _dsan(m.get("from") or "?"), quote,
            _dsan(m.get("dm_display") or m["dm"]), m.get("text") or "",
            thread_tail, tag)
    return "%s %s%s: %s%s%s" % (stamp, _dsan(m.get("from") or "?"), quote,
                                m.get("text") or "", thread_tail, tag)


def verify(room="main"):
    """Re-derive every row's signed payload (payload_for — the shape
    dispatcher) and check it against the payload the row RECORDS. Returns one
    dict per row: {n, from, state, payload, stored, reply_to}, state one of

      ok        signed, and the row still recomputes to the payload it signed
      MISMATCH  signed, but the row NO LONGER recomputes — text or the parent
                pointer was edited under the signature (naive re-parenting) —
                OR the row is a signed REPLY or VERDICT turn carrying no
                payload at all, which cannot be legacy: each structured shape
                shipped in the SAME change as recorded {payload}, so
                signed+shaped+payload-less means the payload was STRIPPED to
                dodge this check (bug-class
                verify-downgrades-to-legacy-when-payload-stripped). A row
                claiming BOTH structured shapes (verdict+reply) is likewise
                MISMATCH: post() refuses the combination, so it can only be
                forged — payload_for would sign one shape while the other
                shape's semantics (wake/render) ride outside the claim
      legacy    signed before rows recorded their payload — nothing to compare
      unsigned  no chain receipt (the v1 path / no signer): nothing to verify

    HONEST SCOPE: this is SELF-consistency, not remote re-verification —
    cell.verify_anchor still cannot ask the node what payload a turn carried
    ("payload binding unavailable"), so a forger who rewrites BOTH the row and
    its stored payload is only caught once dregg discloses payloads. What the
    reply tag buys today is that the signer's claim NAMES the parent, and this
    is the one place a reader re-derives it."""
    rows, _total = read(room)
    out = []
    for i, m in enumerate(rows, 1):
        want = payload_for(m)
        stored = m.get("payload")
        state = ("unsigned" if m.get("chain") is None else
                 "MISMATCH" if (is_verdict(m) and is_reply(m)) else
                 ("ok" if stored == want else "MISMATCH") if stored else
                 "MISMATCH" if (is_reply(m) or is_verdict(m)) else "legacy")
        # DISPLAY-launder the from at the one owner (the emitted dict), the
        # public_rows pattern: verify()'s only consumer is the CLI print, which
        # writes r["from"] raw to stderr — a MISMATCH row planted with a
        # hostile from would reshape the terminal there. The RAW row is
        # untouched (payload_for re-derives off `m`, not this dict).
        out.append({"n": i, "from": _dsan(m.get("from") or "?"), "state": state,
                    "payload": want, "stored": stored,
                    "reply_to": m.get("reply_to")})
    return out


def _follow(room, since=0):
    """Poll-print loop — the orca pane sidecar. Ctrl-C exits clean. The read
    primitive it loops on is read() (unit-tested); the loop itself is not."""
    try:
        while True:
            rows, total = read(room)
            idx = index_rows(rows)
            tag = react_prefix(rows)      # same [n] as `read` — react targets it
            start = since if 0 <= since <= total else 0
            for i, m in enumerate(rows[start:], start):
                print(tag(i) + _fmt(m, idx=idx), flush=True)
            consume(room, total)
            since = total
            time.sleep(POLL_S)
    except KeyboardInterrupt:
        print()
        return 0


# ---------------------------------------------------------------------------
# The log-after leg — the ONLY disk writer in chat, out-of-band by law
# ---------------------------------------------------------------------------

def journal_dir():
    return os.path.join(home.project_dir("helm"), "journal")


def _flush_state_path():
    return os.path.join(journal_dir(), ".chat-flush.json")


def _fp(m):
    return hashlib.blake2b(json.dumps(m, sort_keys=True, ensure_ascii=False)
                           .encode("utf-8"), digest_size=8).hexdigest()


def log_disabled():
    return (home.env("CHAT_LOG") or "").lower() in ("0", "off", "no")


_JREC = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z) \[([^\]]+)\] (.*)$")
_JAUTHOR = re.compile(r"^([A-Za-z0-9._-]{1,40}): (.*)$", re.S)
_JEVENT = re.compile(r" \{event-id:([0-9a-f]{12})\}$")


def _journal_records():
    """Parse every chat-*.log journal file back into (ts, room, body) records.

    The journal is log_flush's RENDERED output — one `ts [room] body` header
    line per row, with the body's own newlines written raw, so a record runs
    until the next header. `# ` lines at record boundaries are flush gap
    notes, not rows. Returns ({room: [(ts, body)]}, state) where state is
    "absent" | "ok" — absent means no journal exists, which is not an error,
    it is a fresh host. Unreadable raises to the caller: a restore that
    silently skips unreadable history would report the opposite of the truth.
    """
    jd = journal_dir()
    meta = {"files": 0, "unreadable": [], "orphan_lines": 0}
    if not os.path.isdir(jd):
        return {}, "absent", meta
    rooms, seen = {}, set()
    names = sorted(n for n in os.listdir(jd)
                   if n.startswith("chat-") and n.endswith(".log"))
    if not names:
        return {}, "absent", meta
    meta["files"] = len(names)
    # A RETIRED ROOM STAYS RETIRED. `helm chat retire-rooms` archived it off
    # the bus, raw bytes and all, so its journaled rows are history the
    # archive already holds. Restoring them would rebuild the room on every
    # reboot and mint a cursor pair in it for every rostered seat — which is
    # how 404 idle meld rooms and ~47k of their cursors kept coming back. A
    # row posted AFTER the retirement (a room reborn under the same name) is
    # newer than `through` and restores like any other.
    from . import chatdebris
    retired = chatdebris.retired_through()

    def emit(cur):
        ts, room, lines = cur
        # dm-* was never journaled. Meld rows restore like any room: the
        # 2026-08-02 reboot emptied every meld room (lifecycle skeletons
        # survived; the conversation did not) and the participants had to
        # rebuild their converged spec from transcripts by hand.
        if room.startswith("dm-"):
            return
        if room in retired and ts <= retired[room]:
            return
        body = "\n".join(lines).rstrip()
        if not body:
            return
        key = (room, ts, body)          # rotation-gap re-logs duplicate rows
        if key in seen:
            return
        seen.add(key)
        rooms.setdefault(room, []).append((ts, body))

    for name in names:
        cur = None
        try:
            f = open(os.path.join(jd, name), encoding="utf-8")
        except OSError:
            # an unreadable file is UNKNOWN history, never a confident zero —
            # a recovery verb that silently skips input reports the opposite
            # of the truth (a-pass-whose-input-was-missing-is-vacuous)
            meta["unreadable"].append(name)
            continue
        with f:
            for line in f:
                line = line.rstrip("\n")
                m = _JREC.match(line)
                if m:
                    if cur:
                        emit(cur)
                    cur = (m.group(1), m.group(2), [m.group(3)])
                elif line.startswith("# ") and cur is None:
                    continue            # flush gap note between records
                elif cur:
                    cur[2].append(line)  # continuation of a multi-line body
                else:
                    # a line before any header belongs to nothing — count it
                    # so mangled history is REPORTED, not silently dropped
                    meta["orphan_lines"] += 1
        if cur:
            emit(cur)
    return rooms, "ok", meta


def _restored_row(ts, body):
    """A journal record back as a room row — a RECONSTRUCTION, honestly so.

    The id is a hash of (ts, body), so re-running the restore mints the SAME
    ids and idempotence falls out of dedupe instead of needing state. The
    author is parsed back off the rendered `author: text` prefix; a body
    that never had one (system lines) is attributed to `journal`. restored=1
    marks provenance for any reader that cares; signatures are not
    reconstructable and are not faked.
    """
    event = _JEVENT.search(body)
    event_row_id = event.group(1) if event else None
    if event:
        body = body[:event.start()]
    m = _JAUTHOR.match(body)
    frm, text = (m.group(1), m.group(2)) if m else ("journal", body)
    # the journal stores _fmt_body output, which ends in the rendered
    # signature tag; _fmt re-appends one live, so a kept tag doubles forever
    text = re.sub(r"\s*\[unsigned\]$", "", text)
    rid = event_row_id or hashlib.sha1(
        ("%s|%s" % (ts, body)).encode()).hexdigest()[:12]
    row = {"id": rid, "ts": ts, "from": frm, "text": text, "restored": 1}
    if event_row_id:
        row["event"] = 1
    return row


def _restore_marker(room):
    return {"id": hashlib.sha1(("restore-marker|" + room).encode()).hexdigest()[:12],
            "ts": pk.now_ts(), "from": "journal", "restored": 1,
            "text": ("-- rows above were RESTORED from the disk journal "
                     "(reconstructions: keyed event ids retained, other ids "
                     "new, no signatures, threading flattened into the "
                     "rendered quotes). Originals: "
                     + journal_dir() + "/chat-*.log --")}


def _restore_cursor_sweep(room, valid_ids):
    """Repair/mint paired delivery+wake cursors after room restoration."""
    from .seats_cursor import restore_cursor_sweep
    return restore_cursor_sweep(room, valid_ids)


def restore_journal(apply=False):
    """Rebuild pre-wipe room history from the disk journal (log_flush's
    inverse). The rooms live on tmpfs, so a reboot wipes them; the journal is
    the write-behind durability log. Dry-run by default.

    THE MERGE IS DEDUPE-DRIVEN, NOT CUTOFF-DRIVEN: a journal record whose
    (ts, rendered body) already matches a live row is already present — the
    journal body IS _fmt_body(row) by construction, so equality is exact,
    and no boot timestamp needs guessing. Rooms whose marker row already
    exists are skipped entirely (deterministic ids make re-runs no-ops).

    Live rows are read UNDER the room lock and preserved byte-identical
    below the marker — delivery cursors carry {ino, off, rid}, so the
    replaced file reads as a rotation and rid-suppression parks the restored
    history for every consumer that has a cursor. For every consumer that
    does NOT, this mints a seat-level end-of-file cursor (the join-at-end
    baseline): a tracked seat with no cursor reads a room from offset 0 by
    design, and without the mint a restore re-delivers days of old mentions
    as new — measured live 2026-07-28, pending 279 on one seat. The two
    writes are one act because either alone is a trap.

    After the swap the log-flush high-water mark is re-baselined; the old
    mark's fingerprint no longer matches, and the next flush would re-log
    entire rooms into the journal as duplicates.
    """
    from . import meld
    meld_report = meld.replay_durable(apply=apply)
    records, jstate, meta = _journal_records()
    if jstate == "absent" and meld_report["state"] == "absent":
        return {"state": "absent", "rooms": {}, "meta": meta,
                "meld": meld_report}
    report = {}
    flush_updates = {}
    for room in sorted(records):
        marker = _restore_marker(room)
        live_rows, _ = read(room)
        if any(r.get("id") == marker["id"]
               or (r.get("from") == "journal"
                   and "RESTORED from the disk journal" in str(r.get("text", "")))
               for r in live_rows):
            report[room] = {"restored": 0, "skipped": "already restored"}
            if apply:
                # the repair pass: a crash between a past restore's swap and
                # its cursor sweep left stale cursors behind — re-running the
                # verb must finish the job, not shrug at the marker
                m, rep = _restore_cursor_sweep(
                    room, {r.get("id") for r in live_rows})
                report[room].update(cursors_minted=m, cursors_repaired=rep)
            continue
        # a live row matches a journal record under EITHER rendering: an
        # original row renders back to exactly the journal body
        # (journal == _fmt_body by construction), while an already-restored
        # reconstruction matches on its raw author-prefixed text
        have = set()
        live_ids = {r.get("id") for r in live_rows if r.get("id")}
        for r in live_rows:
            have.add((r.get("ts"), _fmt_body(r)))
            have.add((r.get("ts"), "%s: %s" % (r.get("from"), r.get("text"))))
        fresh = [(ts, body) for ts, body in records[room]
                 if (ts, body) not in have and
                 _restored_row(ts, body).get("id") not in live_ids]
        if not fresh:
            report[room] = {"restored": 0, "skipped": "nothing new"}
            if apply:
                m, rep = _restore_cursor_sweep(
                    room, {r.get("id") for r in live_rows})
                report[room].update(cursors_minted=m, cursors_repaired=rep)
            continue
        report[room] = {"restored": len(fresh), "live_kept": len(live_rows)}
        if not apply:
            continue
        path = room_path(room)
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        with _room_lock(room):
            live_raw = []
            if os.path.exists(path):
                with open(path, encoding="utf-8") as f:
                    live_raw = [x for x in f.read().split("\n") if x]
            out = [json.dumps(_restored_row(ts, b), ensure_ascii=False)
                   for ts, b in fresh]
            out.append(json.dumps(marker, ensure_ascii=False))
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path))
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write("".join(x + "\n" for x in out + live_raw))
            os.replace(tmp, path)
            # the sweep runs INSIDE the room lock, in the same act as the
            # swap: readers are lock-free, so the window is not zero, but a
            # crash here is recovered by re-running the verb (the
            # already-restored path repairs cursors too)
            valid = {r["id"] for r in
                     (_restored_row(ts, b) for ts, b in fresh)}
            valid.add(marker["id"])
            for x in live_raw:
                try:
                    valid.add(json.loads(x).get("id"))
                except ValueError:
                    pass
            minted, repaired = _restore_cursor_sweep(room, valid)
        rows, _ = read(room)
        flush_updates[room] = {"n": len(rows),
                               "tail": _fp(rows[-1]) if rows else None}
        report[room]["cursors_minted"] = minted
        report[room]["cursors_repaired"] = repaired
    if apply and flush_updates:
        state = pk.read_json(_flush_state_path(), {}) or {}
        state.update(flush_updates)
        os.makedirs(journal_dir(), exist_ok=True)
        pk.write_json(_flush_state_path(), state)
    state_ = "UNKNOWN" if meld_report["state"] == "UNKNOWN" else "ok"
    return {"state": state_, "applied": bool(apply), "rooms": report,
            "meta": meta, "meld": meld_report}


# The STORE-WRITING verbs join the chat/dispatch bodies because they write
# DURABLE PROSE from a positional argument, and a body corrupted on the way in
# is worse there than anywhere else: the store is where a lesson goes to
# survive compaction, and a premise that lost a word gives a future reader no
# signal that anything is missing.
# MEASURED 2026-08-04: `helm store add premise "... NOT a top-level
# `terminals` key ..."` stored as "NOT a top-level key". The backticks ran as
# command substitution inside the double-quoted argv, bash printed
# "terminals: command not found", and the verb exited 0 with the word deleted.
# chat post and dispatch send BLOCK that exact shape and have stopped me twice;
# store add was simply never added to the table.
# `premise\s` not `premise\b`, so `helm premise-check` — a READ verb — stays
# computable.
_BODY_VERBS = re.compile(
    r"helm[\w./-]*\s+(?:chat\s+(?:post|reply|dm)\b"
    r"|chat\s+(?:standup|meld|council)\s+say\b"
    # a revised statement is canon text like an added one; the id before it
    # stays computable
    r"|store\s+(?:add|revise)\b"
    # THE TASK LEDGER IS PROSE TOO, and it was missing for the same reason
    # store add was: nobody added it. MEASURED 2026-09-09 — a `helm task add`
    # note describing a hazard had two backticked phrases EXECUTED by the
    # shell and stored as gaps, and the row read as complete prose afterwards.
    # A task row outlives most chat, so the durability argument is stronger
    # here than on the surfaces already covered, not weaker.
    # THE SET IS THE SYNOPSIS'S FREE-TEXT DOORS, not the verbs I happened to
    # have typed: add <title...>, close <id> <reason...>, comment <id>
    # <text...>, update (prose only through flags). A review found
    # `close` missing — its reason is positional prose exactly like a title.
    r"|task\s+(?P<task_verb>add|close|comment|update)\b"
    r"|premise\s"
    r"|coach\s"
    r"|dispatch\s+send\b)")

# Flag-body surfaces invert the positional-message grammar: the protected prose
# is the value of selected flags, while every other argument stays computable.
# The -[cC] skip covers the cwd-reset idiom (`git -C /path commit`).
_MSG_FLAG_VERBS = re.compile(r"\bgit(?:\s+-[cC]\s+\S+)*\s+(?:commit|tag)\b")
_VERDICT_FLAG_VERBS = re.compile(r"helm[\w./-]*\s+dispatch\s+verdict\b")


def _preceding_word(region, i):
    """The whitespace-delimited word just before region[i] — the lookback
    both position tests share (what flag, if any, owns the quote at i)."""
    k = i - 1
    while k >= 0 and region[k] in " \t":
        k -= 1
    j = k
    while j >= 0 and region[j] not in " \t":
        j -= 1
    return region[j + 1:k + 1]


def _flag_value_position(region, i):
    """Is the quote at region[i] opening the VALUE of a --flag?

    The hazard this guard exists for is the BODY — a positional argument
    someone composed as prose. --ref/--room/--kind take VALUES, and
    double-quoting a substitution there (`--ref "$(git rev-parse HEAD)"`)
    is CORRECT shell — the unquoted form word-splits on whitespace. A guard
    that blocks it trains people out of correct quoting, which is the same
    shape as the bug it prevents (measured live on the integrator, minutes
    after the land). Bare `--` is the end-of-flags marker — what follows
    it is positional again, so it exempts nothing."""
    word = _preceding_word(region, i)
    # a word CONTAINING '=' already carries its value (--room=helm), so the
    # NEXT quoted region is positional — the body — and stays guarded. A word
    # ENDING with '=' (--ref=") is the attached-value form: the quote opens
    # the value itself and is exempt. Found live post-land: the
    # equals-form let a backticked body sail through as a "flag value".
    return (word.startswith("--") and word != "--"
            and ("=" not in word or word.endswith("=")))


_MSG_FLAGS = re.compile(r"-[a-zA-Z]*m|--message=?")
#: `helm task`'s prose also arrives through FLAGS, and the positional rule
#: deliberately exempts flag values — so covering the verb alone leaves
#: `--note` and `--title` computable. That is the half that bit: the mangled
#: note came through `--note`, not through the title.
#: The flag set is every `helm task` flag whose value is free text in the
#: synopsis — note, title, reason, and the posture-na REASON — so a new
#: prose flag is added HERE, never inferred from the ones already listed.
_TASK_FLAGS = re.compile(r"--(?:note|title|reason|posture-na)=?")
#: Boolean task flags consume NO value. Treating every ``--word`` as valued
#: exempted the title after these two from protection, so the shell executed
#: its substitutions before task add saw it.
_TASK_VALUELESS_FLAGS = frozenset(("--mine", "--owner-asked"))
#: These verbs take one identity operand before any prose. That first slot is
#: computable; everything after it is prose positionally or through a selected
#: prose flag.
_TASK_ID_FIRST_VERBS = frozenset(("close", "comment", "update"))


def _task_valueless_flag(word):
    """Whether WORD is a boolean task flag, including a rejected =value form.

    Bash substitutes before task parsing, so ``--mine=\"$(cmd)\"`` must remain
    protected even though the CLI will later reject the attached value.
    """
    return any(word == flag or word.startswith(flag + "=")
               for flag in _TASK_VALUELESS_FLAGS)


def _flag_body_position(region, i, matches, unused_match):
    """Whether region[i] opens prose owned by a selected flag."""
    return bool(matches(_preceding_word(region, i)))


def _positional_body_position(region, i, unused, match):
    """Whether region[i] opens helm prose — positionally, or via a prose FLAG.

    THE EARLIEST VERB OWNS THE SEGMENT, so `helm task add ...` is claimed by
    this grammar and a separate task-flag entry later in the table never gets
    a look. I shipped exactly that mistake and the probe caught it: titles and
    comment text went from computable to protected while `--note` stayed
    computable, which is the slot that had actually been mangled.
    So the prose flags live HERE, as an exception to the flag-value exemption
    rather than as a second grammar. Ordinary flag values — --owner, --room,
    --seat, --ref — stay computable, which is the whole reason that exemption
    exists.

    Task's grammar has two further non-uniform regions. --mine and
    --owner-asked are valueless, so the following quote is still the title;
    close/comment/update take one computable id before their prose. Model both
    explicitly rather than pretending every flag owns a value and every
    positional word is prose.
    """
    word = _preceding_word(region, i)
    if _TASK_FLAGS.fullmatch(word) or _task_valueless_flag(word):
        return True
    verb = match.groupdict().get("task_verb")
    if verb in _TASK_ID_FIRST_VERBS:
        words, prefix = _words_before_quote(region, i)
        if not words and not prefix:
            return False
    return not _flag_value_position(region, i)


def _words_before_quote(region, i):
    """Completed shell words plus the current unquoted prefix before region[i]."""
    words, word = [], []
    quote = None
    esc = False
    for ch in region[:i]:
        if esc:
            word.append(ch)
            esc = False
            continue
        if ch == "\\" and quote != "'":
            esc = True
            continue
        if quote:
            if ch == quote:
                quote = None
            else:
                word.append(ch)
            continue
        if ch in "'\"":
            quote = ch
        elif ch in " \t\r\n":
            if word:
                words.append("".join(word))
                word = []
        else:
            word.append(ch)
    return words, "".join(word)


def _verdict_body_position(region, i, unused, unused_match):
    """Whether this quote opens verdict evidence under cmd_dispatch grammar."""
    words, prefix = _words_before_quote(region, i)
    if prefix in POLARITY_FLAGS or any(
            prefix.startswith(flag + "=") for flag in POLARITY_FLAGS):
        # The parser rejects attached forms, but Bash substitutes before that
        # refusal, so the guard still owns their hazardous quote.
        return True
    operands = sum(word not in POLARITY_FLAGS for word in words)
    return operands >= 2


# needle avoids regex passes on unrelated Bash commands. The earliest verb owns
# the command so protected prose may safely mention a later grammar.
_ARGV_GRAMMARS = (
    ("helm", _BODY_VERBS, _positional_body_position, None,
     ("body", "helm"), "message", True),
    ("git", _MSG_FLAG_VERBS, _flag_body_position, _MSG_FLAGS.fullmatch,
     ("-m message", "git"), "commit", False),
    ("verdict", _VERDICT_FLAG_VERBS, _verdict_body_position, None,
     ("verdict evidence", "helm"), "verdict", False),
)


def _shell_segments(command):
    """Top-level command segments split at shell control operators."""
    start = 0
    quote = None
    esc = False
    i = 0
    while i < len(command):
        ch = command[i]
        if esc:
            esc = False
        elif ch == "\\" and quote != "'":
            esc = True
        elif quote:
            if ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
        elif ch in ";|&":
            yield command[start:i]
            if command[i + 1:i + 2] == ch and ch in "|&":
                i += 1
            start = i + 1
        i += 1
    yield command[start:]


_COMMENT_OPENS_AFTER = " \t;|&()\n"      # …and at the start of the text
_COMMENT_FILL = "\uE00E"     # the private-use fill _mask_quoted writes
# over a comment, which a reader can tell from a quoted word's space


def _shell_comment_start(line):
    """Index of the unquoted word-start # opening a comment, or -1.

    Shell comment recognition lives HERE, beside the segment splitter, so
    every reader of command text shares one answer. Word start means line
    start, whitespace, or the position right after an unquoted operator
    (true;# ignored exits 0). A quoted #, an escaped \\#, $# and a mid-word
    x#y are all data. _mask_quoted reads the same rule, which is how the
    quote-state carrier knows a comment's characters are not syntax."""
    quote = None
    esc = False
    prev = None
    for i, ch in enumerate(line):
        if esc:
            esc = False
        elif ch == "\\" and quote != "'":
            esc = True
        elif quote:
            if ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
        elif ch == "#" and (prev is None or prev in _COMMENT_OPENS_AFTER):
            return i
        prev = ch
    return -1


_UHEREDOC_TAGS = re.compile(r"<<(-?)[ \t]*([A-Za-z_][A-Za-z0-9_]*)")


def _mask_quoted(text, quote=None):
    """``(masked, quote)``: `text` with every QUOTED character replaced by a
    space, and the quote still open at its end (None when none is).

    The state enters and leaves so a caller walking a command LINE BY LINE
    can carry a quote that spans newlines — which is what the argument of a
    multiline `bash -c '…'` is, and the reason an opener on its second line
    is data at this level.

    A COMMENT's characters are masked too and open nothing: an apostrophe
    in `# Don't execute the example.` is not a quote, and a carrier that
    read it as one left the quote open over the real `<<'EOF'` beneath it,
    so the heredoc went unrecognised and its prose body was read as
    commands (found in review). Word-start is _shell_comment_start's rule,
    the one owner; a comment ends at its newline and hands the next line no
    state, because there is none to hand — a comment can only begin where
    no quote is open. A comment's fill is _COMMENT_FILL, not a space, so a
    reader that must tell an erased comment from a quoted word's space
    still can."""
    out = list(text)
    esc = False
    comment = False
    prev = None
    for i, ch in enumerate(text):
        if comment:
            if ch == "\n":
                comment = False
            else:
                out[i] = _COMMENT_FILL
        elif esc:
            if quote:
                out[i] = " "
            esc = False
        elif ch == "\\" and quote != "'":
            if quote:
                out[i] = " "
            esc = True
        elif quote:
            out[i] = " "
            if ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
            out[i] = " "
        elif ch == "#" and (prev is None or prev in _COMMENT_OPENS_AFTER):
            comment = True
            out[i] = _COMMENT_FILL
        prev = ch
    return "".join(out), quote


def _mask_quoted_line(line):
    """`line` with its quoted characters replaced by spaces, the quote state
    starting and ending at this line."""
    return _mask_quoted(line)[0]


def _heredoc_terminates(line, spec):
    """Shell-accurate delimiter line for <<TAG versus <<-TAG."""
    strip_tabs, tag = spec
    text = line.rstrip("\r\n")
    return (text.lstrip("\t") if strip_tabs else text) == tag


def _unquoted_command(command):
    """Same-length command with quoted data masked, except live heredoc bodies."""
    out, tags = [], []
    for line in command.splitlines(True):
        if tags:
            out.append(line)
            if _heredoc_terminates(line, tags[0]):
                tags.pop(0)
            continue
        masked = _mask_quoted_line(line)
        out.append(masked)
        tags.extend(_UHEREDOC_TAGS.findall(masked))
    return "".join(out)


def _select_argv_grammar(command):
    """Earliest executable protected verb in one shell segment."""
    searchable = _unquoted_command(command)
    match = grammar = None
    for candidate_grammar in _ARGV_GRAMMARS:
        needle, pattern = candidate_grammar[0], candidate_grammar[1]
        candidate = pattern.search(searchable) if needle in searchable else None
        if candidate is not None and (
                match is None or candidate.start() < match.start()):
            match, grammar = candidate, candidate_grammar
    return match, grammar


def _heredoc_hazard(line):
    """The first substitution operator active in an unquoted heredoc line."""
    esc = False
    for i, ch in enumerate(line):
        if esc:
            esc = False
        elif ch == "\\":
            esc = True
        elif ch == "`":
            return "a backtick"
        elif ch == "$" and line[i + 1:i + 2] == "(":
            return "$("
    return None


def _guard_unquoted_heredocs(command):
    """Guard substitution-active bodies attached to protected commands."""
    pending = []
    for line in command.splitlines(True):
        if pending:
            spec, cure, part = pending[0]
            if _heredoc_terminates(line, spec):
                pending.pop(0)
                continue
            hazard = _heredoc_hazard(line)
            if hazard:
                return cure, ("%s inside an unquoted heredoc body EXECUTES "
                              "before %s sees it" % (hazard, part[1]))
            continue
        for segment in _shell_segments(line):
            match, grammar = _select_argv_grammar(segment)
            if match is None:
                continue
            _needle, _pattern, _position, _arg, part, cure, _bare = grammar
            masked = _mask_quoted_line(segment)
            pending.extend((spec, cure, part)
                           for spec in _UHEREDOC_TAGS.findall(masked))
    return None


# A HEREDOC DELIMITER IS ONE SHELL WORD, and _delimiter_word is the one
# reader of it. Rounds 3 to 6 each matched a delimiter by SHAPE — an
# identifier, then an identifier with punctuation, then a quoted run — and
# each spelling cured uncovered the next, because a shape test can only
# answer for the spellings its author thought of. Bash does not test a
# shape: after `<<` (or `<<-`) and any blanks it reads ONE WORD, and the
# terminator line is that word after QUOTE REMOVAL. So `<<'DATA'-END`
# closes on the line DATA-END, `<<\EOF` on EOF, `<<"E"OF` on EOF, and
# `<<DATA-END` on DATA-END; and the body is LITERAL — no expansion — when
# any quote character or backslash appears anywhere in the word, which is
# bash's own rule and not a fourth spelling.
#
# THE BOUNDARY, the one thing this reader refuses to read: a word that
# cannot START a delimiter — anything but a letter, an underscore, a quote
# or a backslash — is not an opener at all, so the arithmetic `$((1<<2))`
# stays arithmetic. That leaves such a body in the command text, which is
# the over-block direction, never a missed act.
_HEREDOC_OPEN = re.compile(r"(?<!<)<<(?!<)-?")
_DELIMITER_STARTS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz_'\"\\")
_DELIMITER_ENDS = frozenset(" \t;|&()<>")


class _Opener(object):
    """Where a heredoc opener stands on its line: `start` at the `<<` and
    `end` past the DELIMITER WORD.

    The two consumers split the opener line at exactly those points — what
    comes BEFORE the operator is the command so far, what comes AFTER the
    delimiter is the rest of its redirections — so this carries the same two
    methods the regex match it replaces carried."""

    __slots__ = ("_start", "_end")

    def __init__(self, start, end):
        self._start, self._end = start, end

    def start(self):
        return self._start

    def end(self):
        return self._end


def _delimiter_word(text, at):
    """``(delimiter, quoted, end)`` — the shell WORD at `text[at:]` — or
    None where no delimiter word starts there.

    `delimiter` is the word after QUOTE REMOVAL, the text a terminator line
    must equal; `quoted` is True when a quote character or a backslash
    appeared in it, which is exactly when bash makes the body literal; `end`
    is the index just past the word. The word ends at UNQUOTED whitespace or
    an unquoted operator character, so everything a quote or an escape
    covers belongs to it, however the two are mixed.

    An unclosed quote runs the word to the end of the line: no terminator
    then equals it, which leaves the body running to the end of the command
    for the caller to read rather than hiding one."""
    if text[at:at + 1] not in _DELIMITER_STARTS:
        return None
    out, quoted, quote, i = [], False, None, at
    while i < len(text):
        ch = text[i]
        if quote is None and ch in _DELIMITER_ENDS:
            break
        if ch == "\\" and quote != "'":
            # outside quotes a backslash escapes ANY next character; inside
            # double quotes only these four, and elsewhere it is literal
            nxt = text[i + 1:i + 2]
            quoted = True
            if nxt and (quote is None or nxt in "\\\"$`"):
                out.append(nxt)
                i += 2
                continue
            out.append(ch)
        elif quote is None and ch in "'\"":
            quoted, quote = True, ch
        elif ch == quote:
            quote = None
        else:
            out.append(ch)
        i += 1
    return "".join(out), quoted, i


def _heredoc_opens(line):
    """``(opener, quoted, delimiter)`` for every heredoc operator on `line`,
    in the order the shell reads their bodies in.

    THE OPERATOR IS FOUND IN THE QUOTE-MASKED LINE and the delimiter word
    read from the line itself: a `<<` inside a quoted word is text, not a
    redirection, so `echo "<<'EOF'"` opens nothing. Reading the operator
    from the raw line instead let a quoted FAKE opener take over the walk —
    the lines below it were given to a document that the shell never opens,
    the excision then cut them as data, and a real `helm chat wait --follow`
    standing there was never seen by the rung that must refuse it (measured
    against this guard by an outsider review). A masked line is the same
    length as its own, so the offsets index either; the BLANKS between the
    operator and its word are skipped in the line itself, because a masked
    quoted word is spaces there and skipping those would step over the
    delimiter."""
    masked = _mask_quoted_line(line)
    out, at = [], 0
    while True:
        m = _HEREDOC_OPEN.search(masked, at)
        if m is None:
            return out
        start = m.end()
        while line[start:start + 1] in (" ", "\t"):
            start += 1
        word = _delimiter_word(line, start)
        if word is None:                 # not a delimiter: not an opener
            at = m.end()
            continue
        delimiter, quoted, end = word
        out.append((_Opener(m.start(), end), quoted, delimiter))
        at = end


def _heredoc_openers(command):
    """Yield ``(index, openers)`` for every COMMAND line of `command`:
    its heredoc openers in operator order, each as
    ``(match, quoted, body_start, body_end, resume)`` — `body_end` is the
    line the body stops at and `resume` the next line this walker visits.

    A heredoc BODY is never yielded, and never contributes quote state to
    the line after it: its characters are the command's DATA, whatever the
    tag (found in review: an apostrophe in the body of `cat <<EOF` left a
    quote open that hid the real `<<'EOF'` two lines down, and its prose
    was then read as commands). Bodies of both tag kinds are located here,
    for that reason alone; what each CALLER does with a body still turns on
    `quoted`, because only a quoted tag makes the body inert.

    An opener whose `<<` stands inside a quoted word is NOT one of these,
    because the shell does not open a heredoc there — see _heredoc_opens,
    which reads the operator out of the quote-masked line for exactly that
    reason.

    A TERMINATOR THAT NEVER ARRIVES ends the body AT THE END OF THE COMMAND,
    because that is where bash ends it: the shell warns about the delimiter
    it never saw and reads the rest of its input as the document, and it
    never runs a line of that document as a command. So `body_end` and
    `resume` are both the end, and no line below such an opener is visited.
    Round 6 bounded the body at the next line that LOOKED like an opener
    instead, which fabricated a boundary no shell has and promoted a literal
    document's own lines — the `gh workflow enable` inside a `<<'DATA'-END`
    document — to commands (found in review)."""
    lines = command.split("\n")
    i = 0
    while i < len(lines):
        found = _heredoc_opens(lines[i])
        openers, at = [], i + 1
        for opener, quoted, tag in found:
            end = at
            while end < len(lines) and lines[end].strip() != tag:
                end += 1
            if end < len(lines):
                resume = end + 1            # the terminator is data too
            else:                           # never terminated: bash reads
                end = resume = len(lines)   # the rest as the document
            openers.append((opener, quoted, at, end, resume))
            at = min(resume, len(lines))
        yield i, openers
        i = at if openers else i + 1


def _excise_quoted_heredocs(command, keep=frozenset()):
    """The command minus the BODIES of quoted-tag heredocs (<<'TAG').

    No substitution happens inside a quoted-tag body, so its content is
    DATA — including a messaging-verb string with backticks, which is
    exactly what a file being written ABOUT this guard contains (found
    live: the guard blocked its own hook-payload fixture three times).
    Scanning data as if it were the command line anchors the verb search
    inside text that will never run. Unquoted-tag bodies STAY: the shell
    substitutes inside those, so their backticks execute for real.

    Fail direction, both ways safe: excising too much shows the scanner
    LESS (fail-open, the guard's law); a terminator we never find leaves
    the body in (the pre-fix over-block, never a new miss).

    The two flags this took — one to leave an opener inside a quoted word
    alone, one to cut UNQUOTED-tag bodies as well — existed for the
    GitHub-Actions rung, which parsed shell words and needed both. That rung
    now reads its raw text whole, so neither flag has a caller and both are
    gone: an unquoted body stays in the text, which is how the beacon rung
    and the steer see a script written into one.

    `keep` names quoted bodies to leave in by their ORDINAL among every
    opener in the walk's order: the GitHub-Actions rung keeps the bodies a
    shell reads as a script (task/2973), which are the command's own code
    whatever their tag."""
    lines = command.split("\n")
    out, ordinal = [], 0
    for i, openers in _heredoc_openers(command):
        out.append(lines[i])
        for _match, quoted, start, _end, resume in openers:
            if not quoted or ordinal in keep:       # substitutes, or a script
                out.extend(lines[start:resume])
            ordinal += 1
    return "\n".join(out)


def _quoted_heredoc_bodies(command):
    """Every quoted-tag heredoc in `command` as ``(opener, body)``: the
    command line that opens it, with the lines a trailing backslash joins
    onto it, and its BODY, which is the lines `_excise_quoted_heredocs`
    cuts less the terminator. Two documents opened on one line share that
    line.

    Read off the same walk, so the two never disagree about where a
    document starts or stops — a second heredoc reader beside the first is
    the shape the walker's own history warns about. A backslash before a
    line's end is read as a continuation whether or not it is itself
    escaped, which can only join more text to an opener."""
    lines = command.split("\n")
    out = []
    for i, openers in _heredoc_openers(command):
        bodies = ["\n".join(lines[start:end])
                  for _match, quoted, start, end, _resume in openers
                  if quoted]
        if not bodies:
            continue
        first = i
        while first and lines[first - 1].endswith("\\"):
            first -= 1
        opener = "\n".join(lines[first:i + 1])
        out.extend((opener, body) for body in bodies)
    return tuple(out)


def _argv_guard_segment(command):
    """None, or ``(cure, why)`` when this shell segment must not run.

    THE HAZARD IS UPSTREAM OF HELM AND INVISIBLE TO IT: composing a chat or
    dispatch body as a double-quoted shell argument lets the SHELL execute
    any backticked or $(...) content and splice its stdout into the message
    before helm ever sees argv. Measured three times in two days — a gate
    request truncated at its load-bearing token, a fleet post mangled, and a
    backticked `git clean` that RAN in the shared checkout inside the very
    message warning about it (refused only by clean.requireForce). Advisory
    warnings failed all three times, including on the author who wrote them.

    So this runs where the substitution is still VISIBLE: a PreToolUse gate
    over the Bash tool's command string, before any shell exists. It blocks
    the narrowest thing that covers every incident: a messaging verb whose
    DOUBLE-QUOTED argument region contains a backtick or $( — plus bare
    backticks after the verb, which have no legitimate use there. Everything
    else passes: single-quoted bodies (no substitution), quoted-delimiter stdin
    heredocs (the literal route this guard points at), and $(...) OUTSIDE double quotes
    after the verb (`--ref $(git rev-parse HEAD)` is normal and safe —
    it substitutes into an argument the author MEANT to compute).

    Conservative by design and FAIL-OPEN: a guard this close to every Bash
    call must miss an exotic edge rather than block legitimate work.

    PRIOR ART, read before reverting: store premise
    payload-mangling-is-a-grammar-gap-not-a-boundary-guard REFUTED an
    earlier guard for this hazard — correctly. That guard lived INSIDE helm,
    at receive time, where bash has already substituted and helm holds a
    hole indistinguishable from typed text. The refutation kills THAT layer,
    not this one: PreToolUse fires on the Bash tool's command string before
    any shell exists — the one layer that still sees the backticks. A store
    sweep matching that refutation to this diff by shape should match it to
    this paragraph instead. The same premise records why the cure text names
    the stdin route explicitly (an undiscoverable safe path is no safe path)
    and why `"$(cat <<'EOF'…)"` is no cure — it protects the heredoc body
    but still passes an argv word, so this guard blocks it and teaches the
    stronger route.

    THREE GRAMMARS, one scanner:
      positional       chat/dispatch-send body; ordinary flag values exempt
      message-flag     git commit/tag -m/--message value; other slots exempt
      verdict-flag     dispatch verdict --approve/--fix/--supersede evidence
    The classes are DATA because their scan is identical and only the body
    position differs. Selection happens independently inside each top-level
    shell segment, and verb discovery ignores quoted data, so one protected
    command cannot hide a later leg or turn a literal command example into code.
    """
    m, grammar = _select_argv_grammar(command)
    if m is None:
        return None
    _needle, _pattern, body_position, body_arg, part, cure, bare = grammar
    region = command[m.end():]
    # stop at an unquoted command separator — a later && segment's $() is
    # someone else's business (crude split; a separator inside a quoted body
    # truncates the scan early, which fails OPEN, never closed)
    quote = None
    body = False
    esc = False
    for i, ch in enumerate(region):
        if esc:
            esc = False
            continue
        if ch == "\\" and quote != "'":
            esc = True
            continue
        if quote:
            if ch == quote:
                quote = None
            elif quote == '"' and body:
                if ch == "`":
                    return cure, ("a backtick inside a double-quoted %s "
                                  "EXECUTES before %s sees it" % part)
                if ch == "$" and region[i + 1:i + 2] == "(":
                    return cure, ("$( inside a double-quoted %s EXECUTES "
                                  "before %s sees it" % part)
            continue
        if ch in "'\"":
            quote = ch
            # body = this double-quoted region gets hazard-checked. The scanner
            # is shared; each grammar owns only the region predicate.
            body = ch == '"' and body_position(region, i, body_arg, m)
        elif ch == "<" and region[i + 1:i + 2] == "<":
            # a QUOTED-tag heredoc (<<'EOF') suppresses all substitution —
            # it is the shell-safe route this guard exists to point at, so
            # its body is data and the scan ends here. An UNQUOTED tag
            # (<<EOF) keeps substituting, so scanning continues into it.
            j = i + 2
            while region[j:j + 1] in ("-", " ", "\t"):
                j += 1
            if region[j:j + 1] in ("'", '"'):
                return None
        elif ch == "`" and bare:
            # after a POSITIONAL-body verb a bare backtick has no legitimate
            # use; flag-body grammars still allow computed non-body arguments.
            return cure, "a bare backtick after a messaging verb executes"
        elif ch in ";|&" and quote is None:
            break
    return None


# ---------------------------------------------------------------------------
# PTU STEERS — advisory redirections that ride the hook we ALREADY pay for.
#
# MEASURED 2026-08-05 before any of this was written, because the budget
# question decides the shape: every Bash tool call already costs 211ms of
# hooks (argv-guard 82ms PreToolUse + chat deliver 129ms PostToolUse), of
# which ~55ms is interpreter+import and ~80ms is work. So a NEW hook costs a
# whole extra process, and a rung inside one we already pay for costs only
# its own regex. That is the entire design: no new hook, no new process.
#
# PRE, NOT POST, for command-shaped signals. A steer here fires before the
# wrong number EXISTS, not merely before someone acts on it.
#
# NEVER BLOCKING. argv_guard's exit-2 arm stays reserved for the
# shell-substitution hazard it owns, where the damage is irreversible and
# local. A steer is a redirection, and a redirection that can stop your work
# is a gate wearing the wrong name.
#
# ONCE PER (SESSION, STEER). The attention budget is the hard constraint on
# the highest-frequency surface helm owns: noise that gets ignored is worse
# than silence, because it trains the seat to skip the one that mattered.
# Each steer speaks at most once per session; the latch is a file in the
# tmpfs chat dir, the same RAM-side, dies-with-the-boot lane the stop-guard's
# fired-set uses. If this proves too quiet it can be loosened; starting loose
# cannot be undone, because the seats will already have learned to skim.
_STEERS = (
    ("two-dot-lane-diff",
     # The three-dot lookahead keeps the CORRECT form silent. Position
     # relative to `--` is decided in _steer_suppressed, not here: a `--`
     # makes a token a PATHSPEC only when the token comes AFTER it, and a
     # lookahead cannot express "after" — `git diff base..head -- path` is a
     # real range WITH a pathspec and must still fire.
     re.compile(r"\bgit\s+(?:-C\s+\S+\s+)?diff\b"
                r"(?![^|;&]*\.\.\.)"
                r"[^|;&]*?\s[\w./@{}^~-]+\.\.[\w./@{}^~-]+"),
     "TWO-DOT DIFF ON A LANE. `git diff A..B` is a plain A-to-B diff, so on a "
     "branch BEHIND its base it renders the BASE'S OWN commits as differences "
     "— they read exactly like files your lane touched. Measured today: a "
     "6-file lane reported 8, and the 2 extras were another seat's live work. "
     "Use three dots (merge-base) to ask what YOUR lane changes: "
     "git diff --name-only origin/main...HEAD"),

    ("fab-gate-is-a-remote-job",
     # FIRES ON THE LAUNCH, NOT THE KILL, because the kill is unmatchable: by
     # the time a seat types `kill 1093213` the command string carries no
     # trace of what that pid fronts. The launch is the only moment the
     # knowledge is both needed later and catchable now.
     re.compile(r"\bfab\s+gate\b"),
     # A STEER IS THE TAP ON THE SHOULDER, NOT THE RECORD. The measured
     # incident, the synthesised exit codes and the kill-intent file are in
     # the store entry this names, whole; restating them here would spend a
     # kilobyte on every `fab gate` to say what one command says on demand.
     "`fab gate` is a wrapper, not the job. A local kill — Ctrl-C, kill, "
     "TaskStop, a SIGTERM sweep — ends the follower and leaves the run going "
     "on the build node, with no recorded reason for the stop. Stop one with "
     "`fab kill <host> <run-id>`; the run-id is the lane-... token in "
     "~/fab/logs on that host. Why, and how to read the exit code: helm store "
     "get prior:taskstop-kills-the-wrapper-never-the-remote-fab-job"),

    # `pkill -f` was a steer here and is a DENY now (actsteer.pkill_refusal):
    # a steer's advice reaches the model with the call's RESULT, so it could
    # never arrive before the first self-kill (32 of them, measured). A
    # read-only `pgrep -f` that matched its own shell keeps a steer
    # (actsteer.PGREP_STEER), because nothing it printed has been acted on.

    ("pipeline-exit-status",
     # TWO SPELLINGS OF ONE HAZARD. The first is an EXPLICIT read: `$?` after
     # the pipeline. The second is an IMPLICIT one — `||` / `&&` branch on the
     # pipeline's status without ever naming it, and that is the shape the
     # incident this rung exists for actually took: a `|| fallback` placed
     # where it can never fire, because the fallback keys on tail's success.
     # An explicit-only rung stays silent on the more common half.
     #
     # grep is deliberately ABSENT from the second alternative and present in
     # the first: `cmd | grep -q x && ...` branches on grep's status ON
     # PURPOSE and is correct, whereas `rc=$?` after it is still ambiguous
     # enough to be worth a word. tail/head/awk/sed/cat carry no status a
     # caller ever means to branch on.
     re.compile(r"\|\s*(?:tail|head|grep|awk|sed)\b[^;&|]*[;&]{1,2}[^;&|]*\$\?"
                r"|\|\s*(?:tail|head|awk|sed|cat)\b[^;&|]*(?:\|\||&&)"),
     "A PIPELINE'S EXIT STATUS IS ITS LAST STAGE. `cmd | tail` then reading "
     "$? gives you TAIL's status, not cmd's — so a failed command reads as "
     "success and any `|| fallback` hung off it can never fire. Capture the "
     "real status: run the command alone and read $? on its own line, or set "
     "`set -o pipefail` first."),

    ("typed-ledger-id",
     re.compile(r"\bhelm\s+(?:dispatch|lr|work)\s+\S+\s+[0-9a-f]{12,40}\b"),
     "A LEDGER ID TYPED RATHER THAN INTERPOLATED. Extending a short prefix "
     "with invented hex produces a token of the right shape, right length and "
     "right prefix that does not exist — measured six times, and once against "
     "a real 12-hex id that was called nonexistent. Resolve it into a shell "
     "variable and pass the VARIABLE, so the value you send is the value "
     "something produced."),

    # ── ACT STEERS (task/2980 lane 5). Each replaces a store entry or reflex
    # that rode every prompt whose words looked like its topic; the rows it
    # retired are actsteer.MOVED and reflex.REKEYED. These lines are SHORT and
    # worded for an act that has already run, because a PreToolUse line
    # reaches the model with the call's result (actsteer's docstring); the
    # cap is actsteer.STEER_CAP, held by tests/test_actsteer.py.
    ("git-stat-grep",
     # --stat output piped into a text search: the path-existence check that
     # read a truncated `.../dir/file.sh` as a coverage gap (56 such moments
     # in 8 days, designer's count).
     re.compile(r"\bgit\b[^|;&\n]*\s--stat\b[^|;&\n]*\|\s*"
                r"(?:/usr/bin/)?(?:grep|rg|ugrep|awk)\b"),
     "--stat shortens long paths to .../file, so a grep of its output misses "
     "files that are there. Re-run with --name-only (or --name-status) before "
     "you call a path absent."),

    ("scrub-boundary",
     # The publication-boundary reflex's moment: a history rewrite or an
     # untrack. Its prompt words (scrub, leak, redact...) fired 23.6 times a
     # day and never on a typed turn (designer).
     re.compile(r"\bgit\s+(?:-C\s+\S+\s+)?(?:filter-repo|filter-branch)\b"
                r"|\bgit\s+(?:-C\s+\S+\s+)?rm\b[^|;&\n]*\s--cached\b"
                r"|(?:^|[\s;&|(])bfg\b"),
     "You just scrubbed or untracked. First settle whose artifact it is and "
     "where it is meant to be published: that answer ends the job, and a hunt "
     "for every reference does not."),

    ("pane-send",
     # event-notify-supersedes-paneinject: typing into a pane is the LITE
     # tool (read-only proof); work the recipient must act on needs a row.
     re.compile(r"\b(?:orca\s+terminal\s+send|herdr\s+pane\s+"
                r"(?:run|send-text|send)|tmux\s+send-keys)\b"),
     "You just typed into another pane. That suits read-only proof; if the "
     "recipient must act, also leave a durable row (helm chat dm <seat>). A "
     "busy composer holds typed text until it is idle."),
    # timeout-timing is computed (actsteer.TIMEOUT_STEER): it must see which
    # words are quoted, and a table regex cannot.
)


# SUPPRESSORS — a steer must never fire on the CURE ITS OWN TEXT RECOMMENDS.
# Keyed by steer id; if the pattern matches anywhere in the command, that rung
# stays silent. This is a SEPARATE PASS on purpose: a negative lookahead
# inside the trigger cannot express it, because `search` retries at later
# offsets where the earlier token is behind the cursor and the lookahead
# trivially succeeds — measured, my first cure did exactly that and still
# fired on `set -o pipefail`.
_PIPEFAIL = re.compile(r"set\s+([-+])o\s+pipefail")
_TWO_DOT = re.compile(r"\s([\w./@{}^~-]+\.\.[\w./@{}^~-]+)")


def _steer_suppressed(sid, cmd, hit):
    """Is this MATCH a false positive? Position and effect, never presence.

    My first suppressors keyed on a token appearing ANYWHERE in the command,
    and a review broke both by moving it: `pipefail` set AFTER the pipeline,
    or DISABLED with `set +o`, or set inside a SUBSHELL that has already
    closed, all silenced a real hazard; and a `--` anywhere hid
    `git diff base..head -- path`, which is a genuine range that merely also
    carries a pathspec. Presence is not effect, and a false NEGATIVE here is
    worse than the false positive it cured — a steer that does not fire is
    indistinguishable from correct work.
    """
    if sid == "pipeline-exit-status":
        # only a pipefail ENABLED BEFORE the pipeline, at the same paren
        # depth, actually protects it
        state = None
        for m in _PIPEFAIL.finditer(cmd):
            if m.start() >= hit.start():
                break                      # set AFTER the pipeline: no effect
            before = cmd[:m.start()]
            if before.count("(") > before.count(")"):
                continue                   # inside a subshell: does not escape
            state = m.group(1)             # last one before the pipeline wins
        return state == "-"
    if sid == "two-dot-lane-diff":
        dashdash = cmd.find(" -- ")
        if dashdash < 0:
            return False
        tok = _TWO_DOT.search(cmd)
        # a PATHSPEC only if the dotted token sits AFTER the `--`
        return bool(tok) and tok.start() > dashdash
    return False


# THE SPAWN RUNG — the one steer whose text is computed, because the fact it
# surfaces lives on the roster, not in the command.
#
# The owner's requirement, verbatim, is the spec: "that's where I think Helm
# should have told you that there was already an active idle Kimmy, and I'm
# not sure we can rely on the Claude code harness to do that, I think that is
# Helm's job." A seat under load concludes a family is unreachable and runs
# the bare family CLI to mint a new one while a fresh, idle seat of that
# family sits on the roster. A pull-only roster is the wrong instrument for
# that moment: the seat who most needs it is exactly the one who will not
# stop to pull it. The collision is knowable — a family binary invoked while
# a proven seat of that family is alive — and helm holds both facts, so it
# says so at the moment of the decision and then lets the command run.
#
# PROXY FAMILIES ONLY: the seat catalog's proxy-mode families. `claude` is
# excluded because many legitimate claude sessions coexist by design, and
# `helm` is the tool itself. This is NOT the process-host classifier in
# seats_report (_COULD_HOST_SEAT): that answers "could this process carry a
# seat's environment" and includes shells and interpreters; this answers "is
# this command minting a family seat".
#
# A LITERAL, NOT A CATALOG READ, on purpose. The gate below consumes this
# list on EVERY Bash call, and argv-guard is a fresh process per call, so an
# import of the catalog here — eager or deferred — would be paid by every
# non-spawn command. The literal keeps the no-match path at exactly its
# prior cost; the test module pins it equal to the catalog's proxy-mode
# families so the two cannot drift apart silently.
#
# THE GATE IS THE FIRST REAL TOKEN of a top-level segment — after any leading
# env assignments, by basename so an absolute path matches. A family name
# anywhere else (an argument, a quoted string, a path component) is not a
# spawn and must not pay for a read.
#
# ONE SHELL GRAMMAR IN THIS FILE. The segments come from the SAME two helpers
# the blocking argv guard runs on every Bash call — _excise_quoted_heredocs
# and _shell_segments, called exactly as the guard calls them, with no
# steer-private widening. So the steer sees exactly the command heads the
# blocking guard sees, no more: where the guard's segmenter is blind (an
# unquoted NEWLINE is not a boundary to it, so `cd /tmp` NEWLINE `kimi` is
# one segment whose head is `cd`), the steer is silent in the same place and
# for the same reason. That gap is the guard's, filed once as the shared
# remainder — a shell-aware command-start lexer, built for the guard first —
# not patched here with a second grammar that would drift from the first.
# One such grammar — an opt-in newline split plus an all-openers heredoc
# excision — measurably misreads quoted `<<EOF`, `<<<`, arithmetic shifts,
# function bodies and inactive branches, every one a shell-lexing question
# the guard already owns.
_SPAWN_FAMILIES = ("codex", "kimi", "gemini", "grok", "ds4pro", "ds4flash",
                   "openrouter", "qwen27", "dots3", "opus46", "gptoss")
_SPAWN_HEAD = re.compile(
    r"^[\s({]*(?:[A-Za-z_]\w*=(?:\"[^\"]*\"|'[^']*'|\S*)\s+)*"
    r"(?:\S*/)?(" + "|".join(_SPAWN_FAMILIES) + r")(?=\s|$)")


def _spawn_families(command):
    """The proxy families a command invokes as first real tokens, in order,
    each once. Pure string work — this is the gate that keeps the roster
    read off every non-spawn Bash call. The steer sees exactly the command
    heads the blocking guard sees, no more: same excision, same segmenter,
    same blind spots.

    AND IT DECLINES TO SPEAK OVER A HEREDOC IT CANNOT READ. The shared
    excision removes QUOTED-tag heredocs only, so an unquoted-tag body
    survives into the segmenter, which splits it at `;` like any other
    text: `cat <<EOF` NEWLINE `notes; kimi --print` NEWLINE `EOF` yields a
    segment whose head is `kimi`, and Bash executes no kimi at all
    (measured — it returned ["kimi"] before this refusal existed). A
    FALSE POSITIVE is the one failure a WARN-only line must never have: a
    false negative withholds a hint, a false positive asserts a spawn that
    is not happening. Reading the body correctly needs a shell lexer, which
    is the filed remainder and does not belong in a second grammar here —
    so while any heredoc operator survives the excision, this rung says
    NOTHING rather than something wrong. That is deliberately wider than
    the defect: `<<<` here-strings and arithmetic `<<` silence it too, and
    a genuine spawn written beside a heredoc goes unremarked. Every one of
    those costs a hint; none of them asserts a falsehood."""
    masked = _excise_quoted_heredocs(command)
    if "<<" in masked:
        return []
    seen = []
    for segment in _shell_segments(masked):
        m = _SPAWN_HEAD.match(segment)
        if m and m.group(1) not in seen:
            seen.append(m.group(1))
    return seen


def _spawn_roster():
    """The roster read the spawn rung pays for — ONE seam, so an arm can
    prove a non-spawn command never reaches it and a spawn does."""
    from . import seats
    return seats.roster()


def _live_same_family(family, rows):
    """[(seat, presence, idle_minutes)], freshest first: every seat PROVEN to
    run `family` and still seated, judged against ONE roster snapshot.

    `rows` is that snapshot, read once by the caller and passed in. Reading
    it here instead would mean one read per invoked family, so a command
    naming two families could render one from a roster that has since
    changed and the other from a newer one — two families described from
    two different moments, in one line each, with nothing saying so.

    PROVEN means THE CANONICAL READER says so: for at least one of the row's
    runtime_sessions, seats_runtime._verified_exact_runtime(row, session)
    returns a runtime whose family is exactly `family`. That reader is the
    one authority every runtime consumer already uses — it takes `verified`
    only as the exact boolean True, and a source=proxywatch entry only when
    its immutable proof re-derives to the recorded runtime for that very
    session. A bare `verified: true` beside an empty, foreign or
    contradictory proof is therefore UNKNOWN here exactly as it is there:
    reading the flag raw accepts `verified: true` beside `proxy_proof={}`,
    a shape the canonical reader rejects. Anything short
    of proven, False included, is UNKNOWN, not absent and not
    present: such a seat is skipped, and its absence from this list is not
    evidence about it. A roster that cannot prove anything therefore yields
    an empty list, and an empty list means "nothing proven", never "nobody
    alive".

    SEATED means fresh or quiet (seats_report.presence_of over the presence
    beat). Quiet counts on purpose: a quiet seat is joined and idle, which is
    precisely the seat the spawn is about to duplicate. Absent does not.

    THE SEAT NAME IS A ROSTER KEY, untrusted text that reaches a terminal
    inside a PASTEABLE command (helm chat dm <seat>). Laundering it at the
    sink would print a silently wrong command, so it is validated at the
    seam instead: a key that survives seats_common._seat_label unchanged and
    matches the seat-token alphabet is named; any other key cannot be named
    inertly and is not named at all — it drops out of this list, so a
    planted row can never forge a line or smuggle a control character into
    a command the seat is being told to run.

    Raises on anything it cannot read; the caller turns that into silence."""
    from .dispatches import _TOKEN
    from .seats_common import _seat_label
    from .seats_report import presence_of
    from .seats_roster import last_seen
    from .seats_runtime import _verified_exact_runtime
    now = time.time()
    alive = []
    for seat, row in (rows or {}).items():
        if not isinstance(row, dict):
            continue
        name = _seat_label(seat)
        if name != seat or not _TOKEN.fullmatch(name):
            continue                 # cannot be named inertly: not named
        entries = row.get("runtime_sessions")
        if not isinstance(entries, dict):
            continue
        # the canonical reader is the ONLY authority consulted: no raw read
        # of `verified` or `runtime` happens on this path
        proven = any(
            (_verified_exact_runtime(row, sid)[0] or {}).get("family") == family
            for sid in entries)
        if not proven:
            continue
        ls = last_seen(seat, row)
        presence = presence_of(ls)
        if presence == "absent":
            continue
        alive.append((name, presence, max(0, int((now - ls) // 60))))
    alive.sort(key=lambda a: a[2])
    return alive


def _spawn_steers(command):
    """[(steer-id, text)] for the spawn rung, or [] — including on ANY
    failure. Silence here means "could not look" or "nothing proven", never
    "nobody alive": this is the advisory pass path, and a false all-clear
    would be worse than the incident it exists to prevent.

    One steer per invoked family, latched per family ("spawn-live-<family>")
    so one family's fire never mutes another's. The line names the freshest
    seat — already validated inert by _live_same_family — and the cure. The
    undelivered-row count is deliberately absent: it is a room scan, a
    second read this rung does not pay.

    THE CONTRACT, in one sentence: the steer sees exactly the command heads
    the blocking guard sees, no more — and calls a seat alive only on the
    canonical exact-session runtime reader's word."""
    out = []
    try:
        families = _spawn_families(command)
        if not families:
            return []
        rows = _spawn_roster()             # ONE snapshot, every family judged
        for family in families:            # against the same frozen opinion
            alive = _live_same_family(family, rows)
            if not alive:
                continue
            seat, presence, idle = alive[0]
            more = " +%d more" % (len(alive) - 1) if len(alive) > 1 else ""
            out.append(("spawn-live-" + family,
                        "a %s seat is ALREADY ALIVE: %s (%s, idle %dm)%s — "
                        "reach it with helm chat dm %s before spawning another"
                        % (family, seat, presence, idle, more, seat)))
    except Exception:                          # noqa: BLE001 — fail open
        return []
    return out


# THE CLONE RUNG — a full clone of a LOCAL repository into RAM.
#
# The measured source of a seat frozen in its own memory cgroup: verifier
# subagents each ran `git clone <this repo> <dir in the seat's tmpfs
# scratchpad>`, and every clone copied the whole object store into shared
# memory charged to the seat, which has no swap to put it in. The facts that
# decide it — is the source a local directory, is the target on tmpfs — live
# in scratch.clone_into_ram; this rung owns only the grammar, and it is the
# same grammar the spawn rung uses: the guard's own excision and segmenter,
# silence over any heredoc the excision leaves, and a `cd DIR` segment ahead
# of the clone moving the directory its relative paths resolve against.
#
# ADVISORY, NEVER A BLOCK: a full local clone into RAM is sometimes exactly
# what someone means, and a verifier that cannot copy the tree is worse off
# than one that was told what the copy costs. Silent on `--shared`, the cure.
_CLONE_GATE = re.compile(r"\bgit\b.*\bclone\b", re.S)
_CLONE_STEER = (
    "A FULL CLONE OF A LOCAL REPO INTO RAM. The target is tmpfs: every byte is "
    "shared memory charged to YOUR seat's cgroup, which cannot swap it. A "
    "local clone of helm is 1.4G, and three such verifier copies froze a "
    "seat. Share the objects instead: `git clone --shared <repo> <dir>` "
    "(45M, 0.2s) or `git -C <repo> worktree add --detach <dir>` (for helm, "
    "`helm work peek <rev>`, on disk). `helm scratch big` is RAM too.")


def _clone_heads(command, cwd):
    """[(argv-after-clone, cwd)] for each top-level `git ... clone` segment,
    in order. Pure grammar; no filesystem read."""
    masked = _excise_quoted_heredocs(command)
    if "<<" in masked:
        return []
    out, here = [], cwd
    for segment in _shell_segments(masked):
        try:
            words = shlex.split(segment)
        except ValueError:
            continue
        while words and re.match(r"^[A-Za-z_]\w*=", words[0]):
            words = words[1:]
        if len(words) == 2 and words[0] == "cd":
            nxt = os.path.expanduser(words[1])
            here = (posixpath.normpath(nxt) if os.path.isabs(nxt) else
                    posixpath.normpath(posixpath.join(here, nxt))
                    if here else None)
            continue
        if not words or posixpath.basename(words[0]) != "git":
            continue
        i, at = 1, here
        while i < len(words) and words[i] != "clone":
            w = words[i]
            if w == "-C" and i + 1 < len(words):
                nxt = os.path.expanduser(words[i + 1])
                at = (posixpath.normpath(nxt) if os.path.isabs(nxt) else
                      posixpath.normpath(posixpath.join(at, nxt))
                      if at else None)
                i += 2
            elif w == "-c" and i + 1 < len(words):
                i += 2
            elif w.startswith("-"):
                i += 1
            else:
                break                 # a different subcommand
        if i < len(words) and words[i] == "clone":
            out.append((words[i + 1:], at))
    return out


def _clone_steers(command, cwd=None):
    """[(steer-id, text)] for the clone rung, or [] — including on ANY
    failure. Silence means "could not look" or "not the costly shape", never
    "this copy is free"."""
    try:
        if not _CLONE_GATE.search(command or ""):
            return []
        heads = _clone_heads(command, cwd)
        if not heads:
            return []
        from . import scratch
        table = scratch.mount_table()
        if any(scratch.clone_into_ram(args, at, table) for args, at in heads):
            return [("local-clone-into-ram", _CLONE_STEER)]
    except Exception:                          # noqa: BLE001 — fail open
        return []
    return []


# THE TREE RUNG — whose checkout a tool call is standing in.
#
# The owner's requirement, verbatim, is the spec: "it is ESPECIALLY important
# for TLAs never to suddenly ruin their own context windows randomly working
# on other projects". A lead read another project's commit and wrote a probe
# file into that project's checkout while that project's own lead was live in
# its own pane, and nothing in helm noticed. A dispatch-door rung was measured
# first and would almost never fire, because the harm is not a row: it is a
# TOOL CALL, and this hook is the one door every Bash, Monitor, Write and Edit
# call already passes.
#
# AN ADVISORY, NEVER A BLOCK. Reading an upstream, a reference tree or a fork
# is ordinary work here, so the rung speaks ONLY when all of these hold: the
# call names a path inside a REGISTERED project's checkout, that project is
# not the caller's own (its lane rooms at <repo>-wt included, and any tree the
# caller's own project contains), and a PROVEN-NATIVE seat other than the
# caller is homed in that checkout and still seated. A shared tree with no
# lead, a project whose lead is absent and the caller's own rooms are silent.
#
# "LEAD" IS READ OFF THE ROSTER, which carries no role: a lead is a native
# seat (seats_runtime.runtime_for_session, the delivery lane's own reader,
# says VERIFIED and backend native for the row's current session) whose
# recorded cwd is inside the project's REGISTERED checkout. A proxy seat
# homed there is somebody's reviewer, and a native seat homed in a lane room
# is a worker; neither owns the tree.
#
# COST IS THE DESIGN CONSTRAINT, because this runs on every tool call in the
# fleet. The reads are ordered by what they cost and each one can end the
# rung:
#   1. PURE STRING. A path under a scan root (home.scan_roots, the one reader
#      of that variable) that is not under the payload's own cwd or that
#      cwd's lane rooms. No path, no read — of anything.
#   2. THE ROSTER, through seats_common alone. No seat other than the caller
#      homed over the path means nobody to name, and the registry is never
#      opened: this is the arm a reference-tree read ends on.
#   3. THE REGISTRY (a strict load: no lock, no migration, no write) and the
#      path resolver the injection layer already owns, then the runtime
#      reader, for the few calls that reach them.
#
# WHAT IT CANNOT SEE, stated as a limit: it reads ONE call's text. A relative
# path is not resolved, so `cd ../other` names nothing; and step 1 trusts the
# payload's cwd, so a session whose cwd already stands inside a foreign
# checkout reads that checkout as its own room. The `cd` that got it there
# named the path, which is the call this rung is for. Each miss withholds a
# hint; none asserts a falsehood.
_TREE_TAIL = re.compile(r"[^\s'\"`;|&<>()$*?\[\]{},:=]*")
_TREE_EDGE = re.compile(r"[\w.~/-]")


def _under(path, base):
    return bool(base) and (path == base or path.startswith(base + "/"))


def _room_bases(tree):
    """A checkout and the lane rooms `helm work claim` mints beside it; from
    inside a lane room, the repo that room belongs to as well."""
    tree = tree.rstrip("/")
    cut = tree.find("-wt/")
    if cut > 0:
        return (tree, tree[:cut], tree[:cut] + "-wt")
    if tree.endswith("-wt"):
        return (tree, tree[:-3])
    return (tree, tree + "-wt")


def tree_paths(text, cwd=None):
    """The paths `text` names under a scan root and OUTSIDE the payload cwd's
    own rooms, normalised, each once, in order. Pure string work: this is the
    gate that keeps every read off a call that names no foreign path."""
    text = text or ""
    if "/" not in text:
        return []
    user = os.path.expanduser("~")
    own = _room_bases(posixpath.normpath(cwd)) if cwd else ()
    out = []
    for root in home.scan_roots():
        root = root.rstrip("/")
        if not root:
            continue
        # every spelling of the root a shell would expand to it
        spellings = [root]
        if _under(root, user) and root != user:
            spellings += [h + root[len(user):]
                          for h in ("~", "$HOME", "${HOME}")]
        for spelt in spellings:
            at = text.find(spelt + "/")
            while at >= 0:
                if at == 0 or not _TREE_EDGE.match(text[at - 1]):
                    tail = _TREE_TAIL.match(text, at + len(spelt)).group(0)
                    path = posixpath.normpath(root + tail)
                    if (_under(path, root) and path != root
                            and path not in out
                            and not any(_under(path, b) for b in own)):
                        out.append(path)
                at = text.find(spelt + "/", at + 1)
    return out


def _tree_roster():
    """The roster read the tree rung pays for — ONE seam, through
    seats_common alone: the `seats` facade behind the spawn rung's seam costs
    an order of magnitude more to import, and a read of a reference tree ends
    on this read."""
    from . import seats_common
    return seats_common.roster()


def _tree_registry():
    """The registry read the tree rung pays for — ONE seam. STRICT: an
    ordinary load takes the write lock and may migrate, and a hook on every
    tool call must never be a writer of authority."""
    from . import registry
    return registry.load(strict=True).get("projects") or {}


def tree_line(project, seat, room):
    return ("[helm] this is %s's tree, and its lead @%s is live. Message it "
            "(helm chat post --room %s ...), then leave: a lead works only "
            "its own project." % (project, seat, room))


def tree_steers(text, cwd=None, session=None):
    """[(steer-id, line)] for the tree rung, or [] — including on ANY
    failure. Silence means "could not look" or "nobody to name", never "this
    tree is yours".

    One line per foreign project, latched per project by the caller
    ("foreign-tree-<project>") so one project's fire never mutes another's.

    EVERY NAME IN THE LINE IS UNTRUSTED TEXT bound for a pasteable command —
    a roster key, a registry key, a recorded room. Each is named only if it
    is inert as written (the seat-token alphabet, and for a seat the label
    launder's fixed point); a project or a seat that cannot be named inertly
    is not named at all, and a room that cannot falls back to the project."""
    try:
        paths = tree_paths(text, cwd)
        if not paths:
            return []
        from . import seats_common, seats_roster
        rows = _tree_roster()
        # THE CALLER IS ITS DECLARED NAME, ELSE THE SEAT ITS SESSION IS BOUND
        # TO — the order every delivery path uses. A pane need not carry the
        # name in its environment to be a seat, and a rung that asked only
        # the environment would be silent for exactly those panes.
        me = home.chat_name()
        if not me:
            # AN AMBIGUOUS SESSION IS "COULD NOT LOOK", NOT A TIE TO BREAK.
            # The shared resolver picks the freshest binding and says so on
            # STDERR, which a hook that exits 0 sends to the debug log and
            # nobody. Guessing here would name a seat on the strength of a
            # warning the reader never sees, and the line it prints tells a
            # lead to go message whoever was named. This rung says nothing
            # instead, which is what its own contract above promises.
            bound = seats_roster.seats_for_session_in(
                seats_roster.roster_indexes(rows)[0], session)
            if len(bound) != 1:
                return []
            me = seats_common._seat_label(bound[0])
        if not me:
            return []
        mine = seats_common.canonical_keys(me, rows)
        if len(mine) != 1 or not isinstance(rows.get(mine[0]), dict):
            return []
        # A CALLER HELM CANNOT PLACE HEARS NOTHING: with no recorded home
        # there is no tree to call its own, so none can be called foreign.
        home_tree = rows[mine[0]].get("cwd")
        if not isinstance(home_tree, str) or not home_tree:
            return []
        roots = [r.rstrip("/") for r in home.scan_roots()]
        hosts = []
        for seat, row in rows.items():
            if seat == mine[0] or not isinstance(row, dict):
                continue
            at = row.get("cwd")
            if not isinstance(at, str) or not at:
                continue
            at = posixpath.normpath(at)
            # AN UMBRELLA SEAT HOSTS NOTHING. A seat homed AT or ABOVE a scan
            # root stands over every checkout beneath it and leads none of
            # them; counting it as a host would make this early exit
            # unreachable, since it contains every path step 1 can produce.
            if any(_under(root, at) for root in roots):
                continue
            if any(_under(p, b) for p in paths for b in _room_bases(at)):
                hosts.append((seat, row, at))
        if not hosts:
            return []
        from .dispatches import _TOKEN
        from .inject import _ledger
        from .seats_common import _seat_label
        from .seats_report import presence_of
        from .seats_roster import last_seen
        from .seats_runtime import runtime_for_session
        projects = _tree_registry()

        def scope(name):
            rec = next((r for k, r in projects.items()
                        if str(r.get("name") or k) == name), None) or {}
            return [str(p).rstrip("/") for p in
                    ((rec.get("cv_scope") or {}).get("cwd_prefixes")
                     or [rec.get("path")]) if p]

        home_tree = posixpath.normpath(home_tree)
        own = _ledger.project_for_cwd(home_tree, projects=projects)
        mine_bases = [b for pre in (scope(own) if own else [home_tree])
                      for b in _room_bases(pre)]
        out, seen = [], set()
        for path in paths:
            if any(_under(path, b) for b in mine_bases):
                continue
            project = _ledger.project_for_cwd(path, projects=projects)
            if not project or project == own or project in seen:
                continue
            seen.add(project)
            if not _TOKEN.fullmatch(project):
                continue
            checkout = scope(project)
            leads = []
            for seat, row, at in hosts:
                name = _seat_label(seat)
                if name != seat or not _TOKEN.fullmatch(name):
                    continue
                if not any(_under(at, pre) for pre in checkout):
                    continue             # a lane room or a sibling: a worker
                runtime, proven = runtime_for_session(row, row.get("session"))
                if not proven or (runtime or {}).get("backend") != "native":
                    continue
                seen_at = last_seen(seat, row)
                if presence_of(seen_at) == "absent":
                    continue
                leads.append((seen_at, name, row))
            if not leads:
                continue
            _at, name, row = max(leads, key=lambda lead: lead[0])
            room = str(row.get("home_room") or "")
            out.append(("foreign-tree-" + project, tree_line(
                project, name, room if _TOKEN.fullmatch(room) else project)))
        return out
    except Exception:                          # noqa: BLE001 — fail open
        return []


def advise(lines):
    """Say advisory lines where the HARNESS reads them, and exit 0 regardless.

    Stderr from a hook that exits 0 reaches the debug log and nobody else;
    the agent's channel on the pass path is the exit-0 JSON envelope, whose
    `hookSpecificOutput.additionalContext` the harness attaches to the tool
    call. `hookEventName` is minted here as the firing event, because the
    harness discards an envelope naming any other. ONE document per process:
    every line rides one envelope, and nothing else on this hook's pass path
    writes stdout. Stderr is kept, for the debug log."""
    lines = [l for l in lines if l]
    if not lines:
        return
    for line in lines:
        print(line, file=sys.stderr)
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "additionalContext": "\n".join(lines)}}))


def _tree_advise(d, text, first=()):
    """Run the tree rung for one payload and say what it found, latched once
    per (session, foreign project). Never raises, never blocks.

    `first` is whatever the pass path already has to say, as (steer-id,
    line) pairs NOT YET LATCHED: `admit` spends a latch only on a line it has
    room to say. It rides the SAME envelope, because the harness reads one
    JSON document per hook process, and it is said even when the tree rung
    fails: a steer that depended on an unrelated reader working is a steer
    that goes quiet for a reason nobody would look for."""
    wanting = list(first)
    try:
        wanting += tree_steers(text, d.get("cwd"), d.get("session_id"))
    except Exception:                          # noqa: BLE001 — fail open
        pass
    try:
        advise(admit(d.get("session_id"), wanting, agent=d.get("agent_id")))
    except Exception:                          # noqa: BLE001 — fail open
        pass


def _act_write_steers(d):
    """[(steer-id, line)] from the act steers for one Write or Edit payload:
    a new source module, or a shell script a live process is running.
    Fail-open to []."""
    try:
        from . import actsteer
        return [(sid, "[helm steer] " + text)
                for sid, text in actsteer.write_steers(
                    d.get("tool_name"), d.get("tool_input") or {},
                    d.get("cwd"))]
    except Exception:                          # noqa: BLE001 — fail open
        return []


# One call's whole advisory, in characters. Each line has its own ceiling in
# the hook budget table; this is the ceiling on what one tool call can be
# handed at once, and it holds two of the widest lines.
ENVELOPE_BUDGET = 900


def admit(session, wanting, budget=ENVELOPE_BUDGET, agent=None):
    """The lines that ride THIS call's envelope, from [(steer-id, line)].

    THE BUDGET DECIDES BEFORE THE LATCH IS SPENT. A line that does not fit is
    neither said nor latched, so it is said on the next call that trips it:
    deferred, never lost. The first line always rides, because a budget that
    could silence every line is the defect this emitter was built to end.

    `agent` is the payload's agent_id: a subagent's calls latch apart from
    its seat's (see _steer_latch)."""
    lines, used = [], 0
    for sid, line in wanting:
        if lines and used + len(line) + 1 > budget:
            continue
        if steer_unfired(session, sid, agent):
            lines.append(line)
            used += len(line) + 1
    return lines


def argv_steers(command, cwd=None, tool_input=None):
    """[(steer-id, text)] — advisory redirections for one command. NEVER
    blocks; the caller prints and exits 0 regardless.

    Pure regex over the command string the hook already carries. No file
    reads, no git, no snapshot: this runs on EVERY Bash call, and the reflex
    law is cheap-local-only.

    ONE GATED EXCEPTION, the spawn rung: its regex gate (first real token is
    a proxy-family binary) is as cheap as the table above and runs on every
    call; only a MATCH pays the roster read. A non-spawn Bash call costs
    exactly what it cost before the rung existed — pinned by an arm whose
    roster reader raises. Silence from that rung means "could not look" or
    "nothing proven", never "nobody alive".

    A SECOND GATED EXCEPTION, the clone rung: only a command whose text holds
    `git` and `clone` pays for the mount-table read that decides it. `cwd` is
    the payload's, and resolves the clone's relative paths."""
    out = []
    cmd = command or ""
    # A QUOTED-TAG HEREDOC BODY IS DATA: a message that NAMES a command is
    # not running it, and a steer fired on its prose spends the session's one
    # latch on a moment the advice does not apply to. An unquoted body stays,
    # as it does for the guard. Fail-open to the whole text.
    try:
        cmd = _excise_quoted_heredocs(cmd)
    except Exception:                          # noqa: BLE001 — fail open
        pass
    for sid, pattern, text in _STEERS:
        if not pattern.search(cmd):
            continue
        if _steer_suppressed(sid, cmd, pattern.search(cmd)):
            continue                 # the cure is already applied
        out.append((sid, text))
    out.extend(_spawn_steers(cmd))
    out.extend(_clone_steers(cmd, cwd))
    out.extend(_act_steers(command or "", cmd, tool_input, cwd))
    return out


def _act_steers(raw, cmd, tool_input=None, cwd=None):
    """The computed act steers (helm/actsteer.py) for one Bash call. `raw`
    keeps the heredoc bodies a claim post carries; `cmd` is the text the
    table above reads. Fail-open to []."""
    try:
        from . import actsteer
        return actsteer.bash_steers(raw, cmd, tool_input, cwd)
    except Exception:                          # noqa: BLE001 — fail open
        return []


def forget_steers(session):
    """Drop this session's steer latches, so each steer may speak once more.

    Called at a CONTEXT BOUNDARY (resumeturn, beside inject's
    forget_session): the session id survives a compaction and the context
    does not, so a latch keyed on the session alone kept a steer silent for a
    seat that had just lost the line it was holding back (task/2980 lane 5,
    "once per (context, route)"). Fail-open; returns how many were dropped."""
    sid = str(session or "").strip()
    if not sid:
        return 0
    key = _latch_key(sid)
    dropped = 0
    try:
        for name in os.listdir(chat_dir()):
            # the seat's own latches AND every subagent's under this session:
            # ptusteer.<steer>.<session>[.a<agent>] (_steer_latch)
            parts = name.split(".")
            if parts[0] == "ptusteer" and len(parts) in (3, 4) \
                    and parts[2] == key:
                try:
                    os.remove(os.path.join(chat_dir(), name))
                    dropped += 1
                except OSError:
                    pass
    except OSError:
        pass
    return dropped


def _latch_key(value):
    """A bounded, collision-safe filename token for one identity."""
    import hashlib
    return hashlib.blake2b(value.encode("utf-8"), digest_size=8).hexdigest()


def _steer_latch(session, steer_id, agent=None):
    """The once-per-(session, steer) path, RAM-side in the chat dir beside
    the stop-guard's own fired-set. Returns None when there is no session to
    key on — an unkeyable steer fires rather than latching, because silence
    is the failure mode that costs more here.

    A SUBAGENT LATCHES APART FROM ITS SEAT. It shares the seat's session id,
    so a latch keyed on the session alone let a subagent's call spend the
    seat's one line: the seat never heard a steer about its own act (found in
    review, both orders). The payload's agent_id, the only field that tells a
    subagent's calls from the seat's (actors.SIDECHAIN_RULE), is folded into
    the name as a fourth part, so forget_steers can still clear the seat and
    every subagent of one session together."""
    sid = str(session or "").strip()
    if not sid:
        return None
    # THE FULL SESSION IDENTITY, HASHED — never truncated. An 8-char prefix
    # collides: two sessions sharing it map to ONE latch and the second seat
    # is silently muted, which is the worst failure this surface has (a steer
    # that does not fire is indistinguishable from correct work). Hashing
    # keeps the filename bounded without throwing identity away, which a
    # prefix does. A second read caught it; a premise covers exactly this
    # and the truncation was written anyway.
    import hashlib
    key = hashlib.blake2b(sid.encode("utf-8"), digest_size=8).hexdigest()
    name = "ptusteer.%s.%s" % (pk.slug(steer_id), key)    # == _latch_key(sid)
    agent = str(agent or "").strip()
    if agent:
        name += ".a" + _latch_key(agent)
    return os.path.join(chat_dir(), name)


def steer_unfired(session, steer_id, agent=None):
    """True the FIRST time this steer is seen in this session (or, with
    `agent`, this subagent of it); latches after.
    Fail-open in both directions — an unreadable latch fires (a missed steer
    is worse than a repeated one) and an unwritable latch simply does not
    latch."""
    path = _steer_latch(session, steer_id, agent)
    if path is None:
        return True
    try:
        if os.path.exists(path):
            return False
    except OSError:
        return True
    try:
        with open(path, "w") as f:
            f.write("1")
    except OSError:
        pass
    return True


def argv_guard(command):
    """Guard every top-level shell segment; return the first required cure."""
    # Bash removes backslash-newline before it parses anything, so the join
    # happens ONCE here and every reader below sees the joined text: a
    # continued heredoc opener stays one command line for the ownership
    # guard AND for the excision, which read the same delimiter word.
    command = (command or "").replace("\\\n", "")
    blocked = _guard_unquoted_heredocs(command)
    if blocked:
        return blocked
    command = _excise_quoted_heredocs(command)
    for segment in _shell_segments(command):
        blocked = _argv_guard_segment(segment)
        if blocked:
            return blocked
    return None


# ---------------------------------------------------------------------------
# THE FOLD — the text the two PRESENCE rungs below read, and the whole of
# what either of them knows about shells.
#
# A fold is not a parse. Every step deletes or rewrites characters
# unconditionally, and none of them asks which word owns a character, so
# there is no context here to be wrong about. The steps, in order:
#
#   1. join backslash-newline continuations, as bash does before it reads
#      anything;
#   2. decode the NUMERIC escapes wherever they stand, because
#      $'\x67\x68 workflow enable' runs gh and spells no gh. THE DIGIT COUNT
#      IS BASH'S, NOT A FIXED WIDTH: \xH{1,2}, \uH{1,4}, \UH{1,8} and
#      \O{1,3}, each taking as many digits as it can and no more. A fixed
#      four and eight admitted $'\u67\u68' and $'\U67\U68', which are `gh`
#      in bash 5.3 (measured), and an octal above 255 is MASKED TO A BYTE
#      there — $'\547\550' is `gh`, $'\777' is \xff and $'\400' is the empty
#      string — so the decode masks too, and a byte of 0 is dropped rather
#      than left standing between two halves of a word;
#   3. delete every quote MARK — `'`, `"`, and the two-character ANSI-C and
#      locale marks `$'` and `$"` with the `$` that introduces them — and
#      every backslash, because `g\h`, `gh "workflow" enable`,
#      `actions/'permissions'` and `acti$''ons` all reach the same act;
#   4. collapse every run of whitespace to one space, and casefold;
#   5. collapse the `./`, `//` and `x/..` segments of every slash-bearing
#      word, so `.github/x/../workflows` is the directory it resolves to.
#
# THREE OF THOSE STEPS DISAGREE WITH THEMSELVES, so the fold yields more
# than one reading and a rung refuses if a spelling stands in ANY of them:
#
#   * a LETTER escape is a character inside $'…' and a deleted backslash
#     everywhere else. `gh work\flow enable` needs the deletion — the f is
#     a letter of the word — and $'workflow\nenable' needs the decode, where
#     the escape is the space between two words. One reading cannot be both.
#   * a path collapse can only ever REMOVE text, and
#     `.github/workflows/../ISSUE_TEMPLATE/x` still NAMES the directory in
#     the text the operator typed. So the uncollapsed reading is kept beside
#     the collapsed one.
#   * an UNRESOLVED EXPANSION — `$name`, `${…}`, `$(…)`, a backtick span,
#     nested by counting — is a hole in the text and text is all there is:
#     `acti${x}ons/permissions` and `gh work$(echo flow) enable` run the
#     protected argv and spell no row. So the expansions are deleted in a
#     reading of their own, and where the deleted span stood INSIDE a word
#     (a `[\w.-]` character against either side of it) the deletion leaves a
#     MARK a row piece may span, because the text that was removed is
#     unknown and could be any of it: `acti<mark>ons` holds `actions` and
#     `work<mark> enable` holds `workflow enable`. A WHOLE token from a
#     runtime value leaves NO mark and is the limit this rung accepts
#     (docs/HOOKS.md says so in those words): `gh $W enable` is one token of
#     argv that the command text does not contain, and a mark there would
#     make every piece match every `$name`, which is a refusal population of
#     nearly every command the fleet runs — the wrong trade for a guard that
#     stands on every Bash call.
#
# All three disagreements resolve toward MORE refusals, which is the only
# direction a `never` rule may resolve them in. The plainest reading comes
# first, because its character positions are the ones a refusal names.
#
# AND THE READINGS ARE SCANNED JOINED, not one at a time: a row is a SET of
# pieces, and pieces of one row stood in DIFFERENT readings while each
# reading was scanned alone, so the evidence was split and discarded
# (measured: `gh work\flow ci$'\n'enable` holds `workflow` only where the
# backslash is deleted and `enable` only where the escape is decoded, and
# was ALLOWED). The readings are joined by a newline, which no folded
# reading contains and no piece can span, so joining adds no spelling that
# no reading held.
_CONTINUED = re.compile(r"\\\r?\n")
# THE DIGIT COUNTS ARE BASH'S. Measured against bash 5.3: $'\u67' and
# $'\U67' are both `g`, so \u and \U take 1 TO 4 and 1 TO 8 digits and not a
# fixed width — a fixed width left both spellings undecoded and the rung
# returned 0 on `gh $'\u77\u6f\u72\u6b\u66\u6c\u6f\u77' enable`.
#
# THE INTRODUCER LETTER IS CASE-SENSITIVE and the DIGITS ARE NOT, which is
# also bash's spelling ($'\X67' is the four characters \X67 there, measured)
# and which this pattern got backwards: one IGNORECASE flag over the whole
# alternation let the `u` branch match `\U`, take the first four of its
# eight digits and hand back a NUL — so the `U` branch never ran at all and
# $'\U00000067\U00000068' folded to neither `gh` nor its own text. A flag
# that widens one thing widens every branch beside it.
_NUMERIC_ESCAPE = re.compile(
    r"\\(?:x([0-9a-fA-F]{1,2})|u([0-9a-fA-F]{1,4})|U([0-9a-fA-F]{1,8})"
    r"|([0-7]{1,3}))")
# THE CONTROL ESCAPES, both spellings bash has for one. `\n` and its seven
# siblings are one, and `\cX` is the other — and the second was NOT decoded,
# so `$'workflow\cIenable'` folded to `workflowcienable`, a single word that
# holds neither piece, while bash made it a TAB and handed gh two words
# (measured through `eval` and `bash -c`, with no runtime value anywhere in
# the command: both protected words were characters of the text). An
# encoding bash decodes and the fold does not is the class F1 named, and a
# separator is the worst member of it, because a separator SPLITS a glued
# spelling into real argv.
#
# THE ARITHMETIC IS BASH'S, measured against bash 5.3 rather than recalled:
# `\ca` and `\cA` are both \x01, `\cI` is TAB, `\cJ` is NEWLINE, `\c[` is
# ESC, `\c]` \x1d, `\c^` \x1e, `\c_` \x1f, `\c0` \x10 and `\cx` \x18 — which
# is `toupper(c) & 0x1f`, not the `^ 0x40` that a letter table suggests
# (`\c0` would be `p` under that, and is not). `\c?` is the one exception,
# DEL rather than \x1f. A bare `\c` at the end of the text stays its own two
# characters there, so the escape takes exactly one character after it.
#
# `\c@` IS A ZERO BYTE AND IS DROPPED, for the reason an octal zero is: the
# mark below is a NUL, and a character the fold invents must never be one a
# command could have spelled — a forged mark would widen the piece matcher
# from the operator's side of it.
_LETTER_ESCAPE_CHARS = {"a": "\a", "b": "\b", "e": "\x1b", "f": "\f",
                        "n": "\n", "r": "\r", "t": "\t", "v": "\v"}
_CONTROL_ESCAPE = re.compile(r"\\([abefnrtv])|\\c(.)", re.DOTALL)


def _control_char(m):
    """The character a control escape spells, or the empty string for a zero
    byte."""
    letter, ctrl = m.groups()
    if letter:
        return _LETTER_ESCAPE_CHARS[letter]
    if ctrl == "?":
        return "\x7f"
    point = ord(ctrl.upper()) & 0x1F
    return chr(point) if point else ""
# A QUOTE MARK IS UP TO TWO CHARACTERS. bash's ANSI-C and locale quoting
# open with `$'` and `$"`, so deleting only the quote leaves the `$` standing
# INSIDE the word it was inserted into: `acti$''ons` read as `acti$ons`,
# which holds no protected spelling, while the shell hands `actions` to gh.
# Two characters typed into any token defeated both presence rungs that way
# (measured), so the `$` of a mark goes with it.
_SHELL_LITERAL_MARKS = re.compile(r"\$?['\"]|\\")
_WHITESPACE_RUN = re.compile(r"\s+")


def _escaped_char(m):
    """The character a numeric escape spells, or the escape's own text when
    it spells none (a code point past the last one).

    AN OCTAL ESCAPE IS A BYTE. bash masks it to one — $'\\547' is `g`,
    $'\\777' is \\xff and $'\\400' is the empty string (measured against
    bash 5.3) — and an unmasked chr(0o547) is U+0167, which spells no row
    while the shell runs gh. A zero byte is DROPPED rather than kept,
    because a character bash never puts in the word must not stand between
    two halves of a spelling."""
    hexed, uni, wide, octal = m.groups()
    try:
        point = (int(octal, 8) & 0xFF) if octal \
            else int(hexed or uni or wide, 16)
    except ValueError:                      # not a character: leave the text
        return m.group(0)
    try:
        return chr(point) if point else ""
    except ValueError:                      # a code point past the last one
        return m.group(0)


def _flattened(text):
    """Steps 2 to 4 of the fold: escapes decoded, quotes and backslashes
    deleted, whitespace runs collapsed, case folded."""
    text = _NUMERIC_ESCAPE.sub(_escaped_char, text)
    text = _SHELL_LITERAL_MARKS.sub("", text)
    return _WHITESPACE_RUN.sub(" ", text).casefold()


def _collapsed_paths(text):
    """Step 5: the `./`, `//` and `x/..` segments of every slash-bearing
    word collapsed — posixpath.normpath, lexical, no filesystem probing."""
    return " ".join(posixpath.normpath(w) if "/" in w else w
                    for w in text.split(" "))


# THE EXPANSION READING. A NUL is the mark because bash cannot put one in a
# command — it is the one character whose presence in a folded reading can
# only have been written here — and every NUL in the raw command is dropped
# before the fold starts so the mark stays unforgeable.
_EXPANSION_MARK = "\x00"
_EXPANSION_MARK_CLASS = "[\x00]"
_MARK_GAP = _EXPANSION_MARK_CLASS + "*"
_PIECE_CHAR = re.compile(r"[\w.-]")
# the parameter spellings bash takes after a bare `$`: a name, a positional
# digit, or one of the special parameters. `$'` and `$"` are NOT here — they
# are quote marks, and step 3 deletes them.
_PARAM_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|[0-9]|[@*#?$!-]")


def _expansion_end(text, i):
    """One past the unresolved expansion starting at text[i], or None.

    NESTING IS COUNTED, never matched by a lone closer: `$(echo $(date))`
    and `${x:-$(y)}` are one span each. An unterminated span runs to the end
    of the text, which is where bash's own reader would run out."""
    n = len(text)
    ch = text[i]
    if ch == "`":
        j = i + 1
        while j < n:
            if text[j] == "\\":
                j += 2
                continue
            if text[j] == "`":
                return j + 1
            j += 1
        return n
    if ch != "$" or i + 1 >= n:
        return None
    opener = text[i + 1]
    if opener in "({":
        closer = ")" if opener == "(" else "}"
        depth, j = 0, i + 1
        while j < n:
            if text[j] == "\\":
                j += 2
                continue
            if text[j] == opener:
                depth += 1
            elif text[j] == closer:
                depth -= 1
                if depth == 0:
                    return j + 1
            j += 1
        return n
    named = _PARAM_NAME.match(text, i + 1)
    return named.end() if named else None


def _neighbour_left(text, i):
    """The character before text[i] that decides whether an expansion stands
    inside a WORD, with the quote marks between them stepped over."""
    j = i - 1
    while j >= 0 and text[j] in "'\"":
        j -= 1
        if j >= 0 and text[j] == "$":     # the `$` of a `$'` or `$"` mark
            j -= 1
    return text[j] if j >= 0 else ""


def _neighbour_right(text, end):
    """The character after an expansion ending at `end`, quote marks stepped
    over the same way."""
    n, j = len(text), end
    while j < n:
        if text[j] in "'\"":
            j += 1
        elif text[j] == "$" and j + 1 < n and text[j + 1] in "'\"":
            j += 2
        else:
            break
    return text[j] if j < n else ""


def _expansions_marked(text):
    """`text` with every unresolved expansion DELETED, and a mark left where
    the deleted span stood inside a word.

    The mark is what a row piece may span, so a piece assembled AROUND an
    expansion is present: `acti${x}ons` reads `acti<mark>ons`, which holds
    `actions`, and `work$(echo flow)` reads `work<mark>`, which holds
    `workflow`. A span that is a WHOLE token leaves no mark, because a mark
    there would stand for the whole of any piece and refuse nearly every
    command the fleet runs; that is the limit stated in docs/HOOKS.md. A
    backslash-escaped `$` expands to nothing in bash and is not an expansion
    here either.

    A QUOTE MARK BESIDE THE EXPANSION IS NOT A WORD BREAK, and reading it as
    one deleted the mark outright: `gh work"$(echo flow)" enable` left the
    readings `gh work$(echo flow) enable` and `gh work enable`, neither of
    which holds `workflow`, while bash joins the quoted halves into one word
    and hands gh the row (measured, eight spellings, the file door among
    them). The neighbour that decides is the first character that is not
    part of a quote mark on either side — never past whitespace, because
    whitespace IS bash's word break: `gh "work" "$(echo flow)" enable` is
    two words of argv and leaves no mark, exactly as the unquoted spelling
    with a space in it does.

    Returns the text and whether any expansion stood in it at all — marked
    or not — because the scoped rule below asks that second question and a
    second walk over the same characters would answer it differently the
    day one of the two is edited."""
    out, i, n, changed = [], 0, len(text), False
    while i < n:
        if text[i] == "\\" and i + 1 < n:
            out.append(text[i:i + 2])
            i += 2
            continue
        end = _expansion_end(text, i)
        if end is None:
            out.append(text[i])
            i += 1
            continue
        inside = _PIECE_CHAR.match(_neighbour_left(text, i)) or \
            _PIECE_CHAR.match(_neighbour_right(text, end))
        out.append(_EXPANSION_MARK if inside else "")
        changed = True
        i = end
    return ("".join(out) if changed else text), changed


def holds_expansion(command):
    """Whether an unresolved expansion — `$name`, `${…}`, `$(…)` or a
    backtick span — stands anywhere in `command`, continuations joined the
    way the fold joins them.

    Every expansion bash has starts with a `$` or a backtick, so a text
    holding neither holds none, and that membership test is the whole answer
    for the commands this rung mostly sees. The walk runs only past it —
    this rung stands on EVERY Bash call and the scoped rule must not make
    the ordinary one pay for a character-by-character pass."""
    text = command or ""
    if "$" not in text and "`" not in text:
        return False
    return _expansions_marked(_CONTINUED.sub("", text))[1]


def folded_commands(command):
    """Every reading of `command` the presence rungs scan, plainest first.

    ONE reading for an ordinary command; a second where an expansion stands
    in it, a third where a letter escape does, a fourth where a slash does,
    and the products of those, deduplicated. The rungs scan them JOINED (see
    `_readings`), so a row whose pieces stand in different readings is
    present, not discarded."""
    joined = _CONTINUED.sub("", (command or "").replace(_EXPANSION_MARK, ""))
    bases = [joined]
    marked, _stood = _expansions_marked(joined)
    if marked != joined:
        bases.append(marked)
    texts = []
    for base in bases:
        texts.append(base)
        if _CONTROL_ESCAPE.search(base):
            texts.append(_CONTROL_ESCAPE.sub(_control_char, base))
    out = []
    for text in texts:
        flat = _flattened(text)
        for reading in (flat, _collapsed_paths(flat)):
            if reading not in out:
                out.append(reading)
    return tuple(out)


# The separator between readings: step 4 collapses every whitespace run to a
# single space, so no folded reading holds a newline, and no row piece holds
# one either — joining on it can complete no spelling that no single reading
# held, which is the only property the union needs.
_READING_BREAK = "\n"


def _readings(command):
    """The folded readings of `command` as ONE text the piece-sets scan.

    A ROW IS A SET OF PIECES and a set is not evidence until it is counted
    together: scanning each reading alone discarded a row whose pieces stood
    in two of them (measured: `gh work\\flow ci$'\\n'enable` holds
    `workflow` only where the backslash is deleted and `enable` only where
    the escape is decoded, and was ALLOWED). The plainest reading is first,
    so a spelling that stands in the command as typed keeps the character
    position a refusal names."""
    return _READING_BREAK.join(folded_commands(command))


# ---------------------------------------------------------------------------
# THE GITHUB-ACTIONS RUNG — an owner rule that was a store premise and a
# whisper, and fired at neither moment that mattered (task/2566): a workflow
# agent recommended enabling Actions, and the seat then ran
# `gh api -X PUT repos/<org>/<repo>/actions/permissions -F enabled=true`
# unguarded; the owner caught it twenty minutes later by luck. Cheap-to-detect
# owner rules belong in guards, beside the Artifact deny and the proxy-seat
# deny sets (seat_catalog.denied_tools), so this is one more rung on the hook
# every Bash call already pays for — never a sibling hook (see the PTU STEERS
# budget note above).
#
# THE RULE, in the owner's words (store premise
# ci-runs-on-the-local-fabric-never-github-actions): CI, builds and releases
# for every local project run on the local-network build fabric, never on
# GitHub Actions or any GitHub-hosted runner; the owner does not want to pay
# for GitHub runners.
#
# THE DECISION IS PRESENCE OVER THE FOLDED INVOCATION TEXT. A reader that
# ALLOWS on which word it thinks runs has to be right about every way a shell
# can spell one act, and the spellings do not run out. Nine of them were
# measured admitted while this rung parsed that way: an override boundary
# read one way and spelled another, a backslash or a quote inside a token,
# gh's global options standing past a fixed gap, a heredoc excision that hid
# a real arm, an Actions API spelling the table had not learned, a path that
# reached the directory by traversal, a substitution whose interior was
# quoted, a grant a receiver stage inherited. So no reader here decides
# which word runs. The whole decision is:
#
#     fold the invocation text once, and refuse if any protected spelling
#     in the table below stands ANYWHERE in it.
#
# THE INVOCATION TEXT IS THE COMMAND LESS ITS DATA (task/2973, the
# integrator's ruling, `_invocation_text`): a quoted argument a recording or
# printing program is given, a grep pattern, a gh api GET, and a quoted
# heredoc body a data program reads are cut, because a shell hands them to a
# program that runs none of them. That reader only ever REMOVES spans it can
# show bash hands to such a program, and anything it cannot settle as bash
# does — or any error in it — leaves the text whole: a misreading costs a
# refusal it would have lifted, never one the rung owes. `_row_the_shell_runs`
# then reads the quoted bodies still standing: a SHELL's body is its script
# and every row there counts; any other program's body counts only for an
# ANCHORED row (task/2870), because `run cancel` in the body of a program
# this rung does not know is more often English than an act, and `workflow`,
# `rerun`, `.github`, `/actions` and `/dispatches` mean nothing outside
# GitHub Actions.
#
# WHAT THAT COSTS, said plainly: a command that MENTIONS one of these
# spellings anywhere but in data is refused too — as an unquoted argument to
# any program, as a quoted argument to a program the reader does not name,
# in a substitution, or in a body a shell reads. (A plain READ of the
# workflow directory passes as well: `_only_the_directory_is_read`.)
# A row of two pieces costs the same way and costs it wider,
# because the pieces need not touch: `echo the run finished; echo the
# workflow is local`, unquoted, holds `workflow` and `run` and is refused,
# and so is a command that names `.github` in one place and a `workflows`
# directory in another. The cure is one word at the front of that command
# and it is in the refusal. A conservative superset is the correct answer
# for a rule whose answer is never; precision about which word RUNS was the
# spiral, and the data cut asks only which spans a shell cannot run.
#
# WHAT IT STILL CANNOT SEE, so that nobody reads the rule as wider than it
# is — TWO limits, and they are limits rather than conservatism (a guard
# described as refusing everything unknown is a guard nobody checks; the
# superset above is a superset of the ACTS, never of everything that could
# perform one). FIRST, this rung reads ONE command's text, and the shell's
# working directory is not in it: a `cd` and a relative write INSIDE one
# command are both in the text and refused; a `cd` in an earlier call, whose
# directory the next call inherits, is not text this rung ever sees. SECOND,
# a token supplied WHOLE by a runtime value is accepted where the row it
# would complete has NO ANCHOR: `gh $W enable ci.yml` and `gh $W download
# 12` run the act with a word the command text does not contain, and no
# reading of that text can hold a piece nobody wrote. An expansion INSIDE a
# word is refused either way (see the fold's mark). The same shape beside an
# ANCHOR IS refused — `gh workflow $V ci.yml`, `gh $W rerun 123` — because
# the anchor is the piece that means nothing outside GitHub Actions; the
# blanket version of that rule, which reads every piece as evidence, refuses
# one command in nine and is what the anchor bought out.
# docs/HOOKS.md says all of this in the same words.
GITHUB_ACTIONS_OVERRIDE = "HELM_ALLOW_GITHUB_ACTIONS=1"
# THE GRANT IS ONE WHOLE-COMMAND GRANT: the override word FIRST in the RAW
# command — leading whitespace allowed, nothing else in front of it — and a
# WORD there, so that `HELM_ALLOW_GITHUB_ACTIONS=1x`,
# `HELM_ALLOW_GITHUB_ACTIONS=1-fake` and `XHELM_ALLOW_GITHUB_ACTIONS=1`
# grant nothing: none of them assigns this name to this value.
#
# NOWHERE ELSE, and that is the design rather than an omission. A grant read
# loosely is a hole, and every loose reading this rung shipped grew one: the
# word buried mid-command lifted a compound whose other half the operator
# never looked at, a receiver stage inherited it through `bash -c`, and a
# revocation grammar (the same word set to 0) then had to be read against a
# shell's last-assignment rule with no ordering to read it by. The front of
# the command is the one position a reader and a writer agree on, so it is
# the only one that grants; the revocation grammar is deleted with the
# nesting it existed for.
#
# Three consequences, all deliberate: the word admits the ONE command it
# stands in and nothing that runs after it (the guard sees one command
# string per Bash call, and this word is never read from os.environ — an
# exported variable would silence the guard for every act that follows); a
# command granted at the front is granted WHOLE, including a second act
# after a `;` that the operator has to be responsible for; and a command
# that carries the word anywhere but the front is refused exactly as if it
# carried no word at all.
#
# THE WORD ENDS WHERE BASH ENDS AN ASSIGNMENT WORD: at whitespace, or at the
# end of the command. Nothing else. The lookahead that also admitted `;`,
# `&`, `|`, `(` and `)` glued to the value was reading an operator as if it
# ended the assignment WORD in the command's favour, and bash does the
# opposite with the ones that end the COMMAND: `HELM_ALLOW_GITHUB_ACTIONS=1;
# gh workflow enable` is a bare assignment and THEN a separate command,
# which does not get the variable at all (measured: the following command's
# environment does not carry it), and the rung granted the whole line. The
# operators that do NOT end the command — a redirection glued to the value,
# `…=1<input gh workflow enable`, which bash assigns exactly 1 for — are
# refused by the same rule, and the cure is the space that every writer of
# this word puts there anyway; a `never` rule resolves toward the refusal.
# Everything after the whitespace is the command, redirections and operators
# included: `…=1 gh workflow enable ci.yml < input` is granted whole.
#
# AND BASH'S WHITESPACE IS NOT PYTHON'S. `\s` is ` \t\n\r\f\v`; bash's
# blanks are the space and the TAB, and a NEWLINE is not a blank there, it
# is a command TERMINATOR — exactly the `;` this rule already refuses.
# `HELM_ALLOW_GITHUB_ACTIONS=1<newline>gh workflow enable ci.yml` is a bare
# assignment and then a separate command, which bash runs with the variable
# UNSET (measured: the receiving process's environment does not carry it),
# and the `\s` class granted that whole command — reopening through the
# whitespace class the hole the operator rule had just closed, and doing it
# in the commonest shape there is, a multi-line command whose first line is
# the word. So the boundary is a blank or the end, and the first line of a
# multi-line command grants only that line's command, which is no command
# at all. `\r`, `\f` and `\v` are not blanks to bash either and end nothing,
# so they grant nothing here.
#
# A BACKSLASH-NEWLINE AFTER THE VALUE IS A GRANT, and the same rule refused
# it: bash removes a continuation before it reads anything, so
# `…=1\<newline> gh workflow enable` assigns exactly 1 (measured) while the
# raw text put a backslash where the blank had to be. The grant is matched
# against the CONTINUATION-JOINED command for that reason — the same join
# the fold does first, for the same reason.
#
# SO THE GRANT IS: the word, a blank, AND A COMMAND AFTER IT ON THAT LINE.
# The last clause is what makes the rule one rule instead of a list of
# terminators, because a blank alone does not make the word a prefix of
# anything: `…=1 <newline>gh …`, `…=1 ; gh …`, `…=1 > out<newline>gh
# …` and `…=1 # note<newline>gh …` are all a BARE assignment and then a
# command bash runs without the variable, which is the `;` hole spelled four
# more ways. A `never` rule resolves the remaining disagreements toward the
# refusal: a command that is only this assignment grants nothing, and it
# performs no act either.
#
# AND `A COMMAND` IS NOT DECIDED BY ONE CHARACTER. Bash's COMMAND PREFIX
# stands between the blank and the command word: more assignment words and
# redirections, in any order and any number of them. `…=1 X=2 gh …`,
# `…=1 2>/dev/null gh …` and `…=1 <in gh …` all run gh with this name
# assigned exactly 1 (measured, a fake gh reading its own environment), so all
# three are grants — and a lookahead that asked only whether the next
# CHARACTER could open a command word admitted the prefix ITSELF as the
# command and granted the line it stands alone on:
# `…=1 2>/dev/null<newline>gh workflow enable` and
# `…=1 X=2<newline>gh workflow enable` ran the act with the name UNSET
# (measured: `<unset>` in gh's environment, against `1` for the one-line
# spelling of each). That is the bare-assignment hole one token further along,
# and the file descriptor's digit was admitted although the bare `>` beside it
# was not. So the rule WALKS the prefix and asks for a word that is no part of
# it — not `NAME=…`, not a redirection, not a character that ends the
# command — standing before the line does.
_GRANT_BLANK = " \t"
# the characters that end the grant where a command word had to begin: a
# newline and `;` terminate the command, `&` and `|` start another one, `(`
# and `)` open the subshell this grant has never reached into, and `#` opens
# a comment that runs to the end of the line.
_GRANT_ENDS = ";&|()#\n"
# an assignment word as bash spells one: a name, an optional array subscript,
# an optional `+` for append, and the `=`.
_GRANT_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z_0-9]*(?:\[[^]]*\])?\+?=")
# a redirection operator with its file descriptor, so `2>`, `1>&`, `0<`,
# `>>`, `>|`, `<<-` and `<<<` are prefix and not command. `&>` is NOT here:
# `&` ends the grant above, refusing a spelling bash does assign for, which
# is the direction a never rule resolves in.
_GRANT_REDIRECTION = re.compile(r"[0-9]*(?:<<-|<<<|<<|<|>>|>\||>)&?")


def _grant_word_end(text, i):
    """One past the bash WORD starting at `text[i]`: at a blank, at a
    character that ends the command, or at a redirection operator — with
    `'`, `"` and a backslash honoured, because a blank inside a quoted value
    does not end the word and `X='a b' gh …` is one prefix and a command."""
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "\\":
            i += 2
            continue
        if ch in "'\"":
            closed = text.find(ch, i + 1)
            if closed < 0:
                return n
            i = closed + 1
            continue
        if ch in _GRANT_BLANK or ch in _GRANT_ENDS or ch in "<>":
            return i
        i += 1
    return n


def _grants_whole_command(command):
    """Whether `command` carries the override word at its FRONT as the
    prefix of a command that runs on that line — the ONE grant, read against
    the continuation-joined text the way bash reads a command prefix."""
    text = _CONTINUED.sub("", command or "")
    i = len(text) - len(text.lstrip(" \t\n"))
    if not text.startswith(GITHUB_ACTIONS_OVERRIDE, i):
        return False
    i += len(GITHUB_ACTIONS_OVERRIDE)
    n = len(text)
    if i >= n or text[i] not in _GRANT_BLANK:
        return False        # the word ends at a blank, or it is not the word
    while True:
        while i < n and text[i] in _GRANT_BLANK:
            i += 1
        if i >= n or text[i] in _GRANT_ENDS:
            return False    # the word prefixes no command on this line
        redirection = _GRANT_REDIRECTION.match(text, i)
        if redirection is not None:
            i = redirection.end()
            while i < n and text[i] in _GRANT_BLANK:
                i += 1
            i = _grant_word_end(text, i)        # its target, then round again
            continue
        if _GRANT_ASSIGNMENT.match(text, i) is not None:
            i = _grant_word_end(text, i)
            continue
        return True         # a command word: the grant prefixes something


# THE PROTECTED SPELLINGS — one table, so a reviewer reads the whole deny
# set in one place, and `says` is what the refusal tells the operator it
# found.
#
# A ROW IS A SET OF PIECES, ALL PRESENT, IN ANY ORDER AND AT ANY DISTANCE —
# the shape the beacon rung below already uses for `chat` + `wait` + a flag.
# It is not a phrase: a row of two words asks only whether the folded text
# holds both of them, because the two positional readings this rung shipped
# were both wrong about a gap. A piece that could run on into a longer word
# carries the edge that stops it, so `run` is not `rerun` and not `runs`,
# and a piece whose own first character is its edge (`/actions`) needs none
# in front of it.
#
# A PIECE WRITTEN WITH A LEADING `+` IS THE ROW'S ANCHOR — the noun or path
# fragment that means nothing outside GitHub Actions — and that is the ONLY
# thing the scoped rule below reads: an ANCHOR beside an unresolved
# expansion is a row. A row marks AT MOST ONE anchor and may mark NONE, and
# a row with no anchor has no scoped rule at all; it still refuses when all
# of its pieces stand in the text, as every row always has. The flag is per
# piece and lives in this table so the deny set stays one artifact; a piece
# standing in several rows carries the same flag in each, which
# `tests.test_chat_argv_guard` asserts rather than trusting the eye.
#
# THE ANCHORS ARE `workflow`, `rerun`, `.github`, `/actions` and
# `/dispatches`. NOT anchors, deliberately: `run`, `list`, `view`, `watch`,
# `cancel`, `download`, `enable` and `disable` — ordinary English words this
# fleet types every day, which say nothing about GitHub standing beside a
# hole. The two-piece directory row anchors on `.github` and not on
# `workflows`, on both readings of the word: this fleet's own agents ARE
# workflows and it says that word in 341 of its commands against 64, and the
# half of that path that is GitHub's is the dotted directory.
#
# A BASE-RATE THRESHOLD STOOD HERE AND ITS OWN MEASUREMENT REFUTED IT.
# Rarity is not the property that matters. Base rate = the share of the
# 120,275 DISTINCT Bash and Monitor commands this project's own session
# transcripts hold whose folded readings hold the piece, asked with the
# shipped matchers. Below one in two thousand stand only `/dispatches`,
# `enable`, `/actions` and `download` — so that rule ALLOWED `gh workflow $V
# ci.yml` and REFUSED `gh $W enable ci.yml`, exactly backwards from what
# protects the repository, because the NOUN is what makes a command an
# Actions command and the verb is an ordinary English word. The rates still
# say what an anchor COSTS. They no longer choose one.
#
# piece / present / one in / carrying an expansion / NEWLY REFUSED as an
# anchor (what it adds to the row rule, which already refuses 581):
#
#     list 13,719 / 9 / 6,544 / 6,426     run 10,364 / 12 / 7,093 / 6,754
#     cancel 1,041 / 116 / 561 / 447      view 622 / 193 / 375 / 364
#     workflow 415 / 290 / 207 / 107      watch 345 / 349 / 226 / 139
#     workflows 341 / 353 / 181 / 140     rerun 156 / 771 / 114 / 48
#     disable 77 / 1,562 / 51 / 48        .github 64 / 1,879 / 53 / 22
#     /dispatches 45 / 2,673 / 18 / 0     enable 41 / 2,934 / 25 / 21
#     /actions 12 / 10,023 / 7 / 0        download 4 / 30,069 / 4 / 1
#
# THESE NUMBERS EXPIRE. The corpus is this project's own transcripts and it
# grows with every session, and much of what makes `workflow` and `.github`
# common in it is the fleet's work ON THIS GUARD. Re-measure before quoting
# them: the probe is one pass of the shipped matchers over the transcript
# commands.
#
# WHAT THIS RUNG COSTS, measured by running each build over the same corpora
# and diffing, with EVERY baseline named — because a round of this lane
# called one of them "before the lane" and it was not one:
#   BEFORE THE LANE the rung does not exist and refuses 0 of the 120,275.
#     This build refuses 757, one command in 159 (0.63%), and that is what
#     the WHOLE rung costs the fleet.
#   THE ROW RULE ALONE, no anchors at all: 581 (0.483%).
#   THE ROUND BEFORE THE SCOPED RULE (row rule, earlier fold): 572. Against
#     it this build newly refuses 185 (0.154%) and loses NONE.
#   THE ROUND BEFORE THIS ONE (the base-rate scoped rule): 603. Against it
#     this build newly refuses 176 (0.146%) and gives back 22 — every one an
#     `enable` or `download` allow this design intends, and not one row
#     refusal among them.
# Over this repo's own tracked `*.sh` and `*.md` (18,232 non-blank lines):
# 0 before the lane, 5 row-only, 16 now — 11 newly refused against the
# 572 build (0.060%), none lost, 4 given back against the round before.
#
# NOTHING WAS DROPPED FROM THE ANCHOR SET, AND THE NUMBERS ARE WHY. The
# budget this round carried — under 0.5% newly refused, zero lost — cannot
# bind at the pre-lane baseline: the ROW rule alone spends 0.483% of it, so
# 20 commands of the 601 are left for any scoped rule at all, and the
# cheapest anchor that decides anything (`.github`, 22) does not fit.
# Dropping in cost order reads 651 (0.541%) without `workflow`, 603 (0.501%)
# without `rerun` too, and 581 (0.483%) without `.github` — the budget is
# reached only where NO anchor is left and the scoped rule is gone with
# them. Against either baseline where a loss is possible the whole set is
# three times inside the budget with zero row refusals lost, so the whole
# set ships. WHAT IT BUYS is `gh workflow $V ci.yml` and `gh $W rerun 123`;
# WHAT IT COSTS is 176 fleet commands — 107 naming `workflow`, 47 `rerun`,
# 22 `.github` — most of them chat posts and notes that MENTION a workflow
# beside a `$` or a backtick and perform nothing.
#
# THE FLAG IS INERT ON A ONE-PIECE ROW (`+/actions`, `+/dispatches`):
# presence of that piece is already a whole row and refuses with no
# expansion needed, which is why both add 0 above. It is written anyway,
# because the anchor is a property of the PIECE and a reader comparing this
# table to the rates must find every one of them.
_ACTIONS_TOKENS = (
    # gh's Actions verb families, as the NOUN and the VERB and never `gh`
    # plus them. gh strips flags before it resolves a subcommand, so a
    # repo flag stands BETWEEN the noun and the verb as readily as before
    # the noun — `gh workflow --repo o/r enable ci.yml`, `gh workflow -R o/r
    # enable 1234` and `gh -R o/r workflow enable ci.yml` are one documented
    # act — and a table that says how much may stand in either gap is
    # reading POSITION, which is the reading this rung deleted and the one
    # whose fixed gap admitted the documented spellings twice.
    ("+workflow enable", "enables GitHub Actions"),
    ("+workflow disable", "is a GitHub Actions verb"),
    ("+workflow run", "runs a workflow"),
    ("+workflow view", "is a GitHub Actions verb"),
    ("+workflow list", "is a GitHub Actions verb"),
    ("run +rerun", "runs a workflow"),
    ("run watch", "is a GitHub Actions verb"),
    ("run cancel", "is a GitHub Actions verb"),
    ("run download", "is a GitHub Actions verb"),
    # the Actions REST API, whatever the client or the host, and whatever the
    # method except where the invocation text shows a `gh api` GET, which is
    # a read and is cut as data before this table is asked. ONE
    # fragment covers every spelling the rounds kept adding one at a time —
    # `repos/<o>/<r>/actions`, `orgs/<o>/actions`, `/actions/permissions`,
    # `/actions/runs/<id>/rerun`, `/actions/jobs/<id>/rerun`,
    # `/actions/workflows/<id>/dispatches` — because each of them spells
    # this segment, and `.github/actions` spells it too.
    ("+/actions", "is the GitHub Actions REST API"),
    # …and the ONE documented trigger that spells no `/actions` segment:
    # `repos/<o>/<r>/dispatches` starts a run wherever a workflow subscribes
    # to `on: repository_dispatch`.
    ("+/dispatches", "triggers repository_dispatch workflows"),
    # the workflow directory, which no `/actions` row can cover — and as two
    # pieces, because a `cd` names it across two words: `cd .github &&
    # printf ... > workflows/evil.yml` writes a workflow file and spells no
    # `.github/workflows` anywhere (measured, and the write was real). The
    # ONE row a command can be let off: a command that only READS this
    # directory passes (`_only_the_directory_is_read`, task/2973).
    ("+.github workflows", "is the Actions workflow directory"),
)
_PIECE_EDGE = re.compile(r"[\w.-]")


def _mark_spanning(piece):
    """`piece` as a pattern that may SPAN the fold's expansion mark, with no
    edges of its own — the construction both deny rungs build their pieces
    from, because a piece assembled around an expansion is present either
    way: `acti<mark>ons` holds `actions` and `ch<mark>` holds `chat`.

    The mark stands for unknown text, so it may stand for a run of the
    piece's own characters; it may FINISH a piece that has begun in the text
    and never START one (see `_row_piece` for the measurement behind that
    boundary)."""
    n = len(piece)
    spans = [_MARK_GAP.join(re.escape(c) for c in piece)]
    spans.extend(re.escape(piece[:i]) + _EXPANSION_MARK_CLASS
                 + re.escape(piece[j:])
                 for i in range(1, n + 1)   # the piece BEGINS in the text
                 for j in range(i + 1, n + 1))
    return "(?:" + "|".join(spans) + ")"


def _row_piece(piece):
    """One row piece, matched as a whole piece: an edge in front of it when
    its first character could be the middle of a longer word, an edge behind
    it when its last one could.

    A PIECE MAY SPAN AN EXPANSION MARK, which stands only in the expansion
    reading and only where a deleted span stood inside a word. The mark is
    unknown text, so it may stand for a RUN OF THE PIECE'S OWN CHARACTERS —
    `work<mark>` holds `workflow`, which is what `gh work$(echo flow)`
    runs.

    A MARK MAY FINISH A PIECE THAT HAS BEGUN IN THE TEXT AND NEVER START
    ONE. The piece's own first character must be one the operator typed, and
    that boundary is MEASURED rather than tasteful: with the mark allowed to
    open a piece, a run over this repository's shell scripts and doc command
    blocks (9,035 lines, 3,193 of them carrying a `$` or a backtick) refused
    three lines of ordinary prose that no rung had ever objected to, all of
    the same shape — a backticked code span pluralised by the letter after
    it (``git worktree``s), where the mark stood for `workflow` and the `s`
    finished `workflows`. One trailing letter is not evidence of an act. The
    cost of the boundary is the other residual named in docs/HOOKS.md: an
    expansion that supplies the BEGINNING of a word (`$Wflow enable`) is
    outside this guard, exactly as a whole token from a runtime value is.

    The mark is not a word character, so an edge is satisfied beside it.

    A PIECE MAY END ON A NARROWER EDGE THAN A WORD (`_PIECE_TAIL`), where the
    act it names is a path shape and not a word."""
    return re.compile(
        (r"(?<![\w.-])" if _PIECE_EDGE.match(piece) else "")
        + _mark_spanning(piece)
        + _PIECE_TAIL.get(piece, r"(?![\w.-])" if _PIECE_EDGE.match(piece[-1])
                          else ""))


# THE REPOSITORY_DISPATCH TRIGGER IS AN API PATH'S LAST SEGMENT (task/2973).
# `repos/<o>/<r>/dispatches` starts a run; `helm/dispatches*.py` and a
# `.../dispatches/<name>` directory are helm's own module and ledger paths,
# and a survey of the rung found 21 of its 212 refusals were
# those, and none of the 212 a real act. So the piece ends where that segment
# ends: not before a glob `*`, and not before a deeper path segment. A lone
# trailing slash (`.../dispatches/`) is still the trigger, and so is a query.
_PIECE_TAIL = {"/dispatches": r"(?![\w.*-])(?!/[\w.*-])"}


ANCHOR_MARK = "+"


def _table_row(spelling, says):
    """One row of the table read into what the rungs use: the spelling as a
    reader sees it, and each piece as (text, matcher, anchor).

    The `+` that marks the row's ANCHOR is written in the spelling so the
    flag cannot drift away from the piece it belongs to, and it is stripped
    here — no refusal ever shows it, because the operator did not type it.

    A row marks AT MOST ONE anchor, and a row may mark none."""
    pieces = []
    for word in spelling.split(" "):
        flagged = word.startswith(ANCHOR_MARK)
        text = word[len(ANCHOR_MARK):] if flagged else word
        pieces.append((text, _row_piece(text), flagged))
    return (" ".join(text for text, _m, _a in pieces), tuple(pieces), says)


_ACTIONS_ROWS = tuple(_table_row(spelling, says)
                      for spelling, says in _ACTIONS_TOKENS)


def _actions_evidence(readings, window=True):
    """What the joined folded readings hold, in ONE pass over the table:
    every complete ROW, earliest first, and the earliest ANCHOR piece of any
    row.

    Both answers come from the same searches because they are the same
    searches — the row rule asks whether every piece of some row stands in
    the text, the scoped rule whether an anchor does, and running the table
    twice made this rung, which stands on every Bash call, pay twice for one
    scan (measured: a 4 KB command went from 2.6 ms to 9.7 ms before this
    was one pass).

    EVERY complete row is returned and not only the earliest, because the
    document rule below can DROP one: a row whose pieces stand only in text
    the shell hands to a program is not evidence, and a second row in the
    same command still is. Returning one row made that rule an allow for the
    whole command whenever the dropped row happened to stand first.

    A row is counted over the readings TOGETHER, because a set of pieces
    split across two readings is the same evidence as a set in one (see
    `_readings`). One piece alone is the MENTION this rung deliberately
    allows (`git add .github`, `echo the workflow is local`, `helm gate run
    --repo $WT`); an ANCHOR becomes evidence beside an unresolved expansion,
    which is the scoped rule in `github_actions_refusal`, and an ordinary
    word never does — that boundary is the table's `+` flag."""
    rows, anchor = [], None
    for spelling, pieces, says in _ACTIONS_ROWS:
        hits = []
        anchored_row = False
        for text, matcher, anchored in pieces:
            hit = matcher.search(readings)
            hits.append(hit)
            anchored_row = anchored_row or anchored
            if hit is not None and anchored and (anchor is None
                                                 or hit.start() < anchor[1]):
                anchor = (text, hit.start())
        at = _row_start(pieces, readings, hits, window)
        if at is not None:
            rows.append((spelling, at, says, pieces, anchored_row))
    rows.sort(key=lambda found: found[1])
    return tuple(rows), anchor


def _every_hit(matcher, readings):
    """Every place `matcher` stands in `readings`, overlapping ones too."""
    out, hit = [], matcher.search(readings)
    while hit is not None:
        out.append(hit)
        hit = matcher.search(readings, hit.start() + 1)
    return out


def _clear_of(mine, others):
    """The hits in `mine` that no hit in `others` overlaps.

    IN N LOG N, NOT PAIRWISE: this runs inside the PreToolUse hook, whose
    deadline fails OPEN, and the input is the caller's own command. Testing
    every hit against every other hit was quadratic, and 6,400 repeats of a
    marked `r$(x)n` took 2.16s through the installed hook, past its 2s
    deadline, so a guarded act trailing that padding was admitted
    (a cross-model read of task/2855). Sort the others by start
    and keep the running maximum of their ends: the others that start
    before a hit ends are one prefix, and one of them overlaps the hit
    exactly when that prefix's furthest end passes the hit's start."""
    others = sorted(others, key=lambda m: m.start())
    starts = [m.start() for m in others]
    reach = list(itertools.accumulate((m.end() for m in others), max))
    kept = []
    for hit in mine:
        j = bisect.bisect_left(starts, hit.end())
        if not j or reach[j - 1] <= hit.start():
            kept.append(hit)
    return kept


def _row_start(pieces, readings, hits, window=True):
    """Where one row starts in `readings`, or None where it does not stand.
    `hits` is each piece's first match, already searched.

    A SPAN THAT READS AS TWO PIECES OF ONE ROW IS NEITHER OF THEM. Every
    piece carries an edge, so in typed text one word is one piece and never
    two — `rerun` is not `run` — but a piece that SPANS the expansion mark
    may stand for several at once: `r<mark>n` holds `run` AND `rerun`,
    because the mark may stand for `u` and for `eru`, and so does a word
    ending `r<mark>`. So three characters were a complete `run rerun` row,
    and a Python regex written through `cat > x.py <<'PY'` — a backtick pair
    whose close is followed by `\\n`, the fold deleting the backslash — was
    refused as a workflow run twice in one session (task/2855,
    reproduced in `tests.test_chat_argv_guard`). Asking
    only that the chosen hits not overlap is NOT enough and was measured
    not to be: the readings are joined, so the one source word stands once
    in each of two readings, and `run` from the first beside `rerun` from
    the second is two disjoint spans of one ambiguous word. So a hit is
    evidence for its piece only where NO hit of another piece of the same
    row overlaps it; a row stands when every piece keeps one such hit.

    WHAT IT GIVES UP: nothing an anchor does not still hold. Two pieces can
    share a span only through the mark, and only where they begin with the
    same character — in this table only `run` and `rerun`. A real act that
    spells either word around an expansion (`gh run r$(echo eru)n 1`,
    `gh r$(echo 'un rer')un 1`) still holds the ANCHOR `rerun` beside an
    expansion in one region, and the scoped rule refuses it.

    A reading with no mark in it cannot hold an overlap at all, so the
    ordinary command is answered from the first matches with no second
    search."""
    if not all(hits):
        return None
    windowed = window and _windowed_row(pieces)
    if _EXPANSION_MARK not in readings:
        if not windowed:
            return min(hit.start() for hit in hits)
        return _near_start(pieces, [_every_hit(matcher, readings)
                                    for _t, matcher, _a in pieces], readings)
    every = [_every_hit(matcher, readings) for _t, matcher, _a in pieces]
    kept = [_clear_of(mine, [other for k, theirs in enumerate(every) if k != i
                             for other in theirs])
            for i, mine in enumerate(every)]
    if not all(kept):
        return None
    if windowed:
        return _near_start(pieces, kept, readings)
    return min(mine[0].start() for mine in kept)


# THE NOUN THAT IS ALSO THE OWNER'S WORD: A WINDOW FOR PROSE, NO CAP FOR AN
# EXECUTED INVOCATION (task/2948). `workflow` anchors five rows, and it is
# ALSO what the owner calls the canonical review fallback — "launch a fable
# 1-agent workflow" — so the task body filing that very directive was refused
# as `workflow list` at character 394: the owner's noun in one sentence, `helm
# seat list` in another, inside ONE quoted argument. The rows asked only that
# both words stand SOMEWHERE in the text.
#
# A WINDOW OVER EVERYTHING WOULD BE A HOLE: `python3 x.py workflow <13 words>
# enable` hands a program the noun and the verb as SEPARATE ARGV, it is an
# executed invocation, and a distance cap there admits a real act. So the
# shape is:
#
#   * AN EXECUTED INVOCATION HAS NO DISTANCE CAP. The command with every
#     quoted string that holds a blank taken out (`_argv_text`) is the text a
#     shell splits into argv, heredoc bodies included, and a `workflow` row
#     standing there refuses at any distance — the rule every other row keeps.
#   * GH IS READ BY ITS OWN GRAMMAR, everywhere, strings included: the noun,
#     then flags and their one value each, then the verb as the noun's first
#     positional. The word count below counts a flag and its value as
#     nothing, so the verb of a real gh call is one counted word after the
#     noun however many flags stand between, and
#     `bash -c 'gh workflow <any flags> enable'` refuses however far apart.
#   * THE WINDOW IS FOR PROSE ONLY: the words of ONE quoted string that holds
#     a blank. There the pieces must stand within _ANCHOR_WINDOW counted words
#     of each other, in either order, a flag and its one value counting as
#     nothing — so prose about the owner's word passes, and the same two words
#     side by side in a string still refuse.
#
# THE SCOPED RULE READS THE SAME SPLIT: an anchor that is an ARGV word beside a
# hole anywhere in the executed text refuses (`A=workflow; ...; gh $A $V`),
# while an anchor inside a quoted prose string needs its hole inside the
# window. Inside a QUOTED heredoc BODY nothing is windowed, as before.
#
# WHAT IT GIVES UP, stated so the next reader can check it: a program that is
# handed the noun and the verb far apart INSIDE ONE QUOTED STRING, and is not
# gh (`python3 x.py "workflow <13 words> enable"`), passes. The other four
# anchors are unchanged. Corpus and the labelled regression suite (task/2973)
# are measured in the lane report of task/2948.
_WINDOWED_ANCHOR = "workflow"
_ANCHOR_WINDOW = 12


def _windowed_row(pieces):
    """Whether this row's anchor is the one the window narrows."""
    return any(anchor and text == _WINDOWED_ANCHOR
               for text, _m, anchor in pieces)


def _word_positions(readings):
    """position -> COUNTED words before it in its own reading.

    A word counts 1; a FLAG (a word opening with `-`) counts 0, and so does
    the ONE word after a flag that carries no `=`, which is where gh reads the
    flag's value. So `gh workflow -R o/r enable` puts the verb one counted
    word after the noun, exactly as `gh workflow enable` does. Each reading
    counts from zero: the readings are one command folded several ways, so a
    word's count lines up across them closely enough for a window this wide,
    and a noun in one reading beside a verb in another (the split the joined
    readings exist to catch) is measured the same way."""
    starts, counts = [], []
    pos = 0
    for line in readings.split(_READING_BREAK):
        n, after_flag = 0, False
        for word in line.split(" "):
            starts.append(pos)
            counts.append(n)
            flag = word.startswith("-")
            if not flag and not after_flag:
                n += 1
            after_flag = flag and "=" not in word
            pos += len(word) + 1
    return lambda at: counts[bisect.bisect_right(starts, at) - 1]


def _near_start(pieces, lists, readings):
    """The start of the earliest windowed row whose pieces stand within
    _ANCHOR_WINDOW counted words of one anchor hit, or None."""
    where = _word_positions(readings)
    anchor = next(i for i, (_t, _m, a) in enumerate(pieces) if a)
    others = [sorted((where(hit.start()), hit.start()) for hit in hits)
              for i, hits in enumerate(lists) if i != anchor]
    best = None
    for hit in lists[anchor]:
        at, starts = where(hit.start()), [hit.start()]
        for near in others:
            j = bisect.bisect_left(near, (at - _ANCHOR_WINDOW, -1))
            if j >= len(near) or near[j][0] > at + _ANCHOR_WINDOW:
                break
            starts.append(near[j][1])
        else:
            best = min(starts) if best is None else min(best, min(starts))
    return best


#: every anchor piece of the table, once: (text, matcher)
_ANCHOR_PIECES = tuple({text: matcher for _s, pieces, _says in
                        (_table_row(spelling, says)
                         for spelling, says in _ACTIONS_TOKENS)
                        for text, matcher, anchor in pieces if anchor}.items())
#: a word of a folded reading that still holds an unresolved expansion: its
#: `$` or backtick in the plain reading, or the fold's mark in the other
_HOLE_WORD = re.compile(r"[^ \n]*[$`\x00][^ \n]*")


def _scoped_anchor(readings, executed, argv=None):
    """(anchor, position) of the earliest anchor in one REGION that counts
    beside that region's expansion, or None. `executed` says the region is
    the executed text; there `workflow` counts wherever it stands as an ARGV
    word (`argv`, the region's `_argv_text` readings), and inside a quoted
    prose string only with a hole inside the window. Any other anchor, and
    any anchor in a quoted body's region, counts wherever it stands (the
    caller has seen the expansion)."""
    found, holes = None, None
    for text, matcher in _ANCHOR_PIECES:
        if not (executed and text == _WINDOWED_ANCHOR) or (
                argv is not None and matcher.search(argv)):
            hit = matcher.search(readings)
            at = hit.start() if hit else None
        else:
            if holes is None:
                where = _word_positions(readings)
                holes = sorted(where(h.start())
                               for h in _HOLE_WORD.finditer(readings))
            at = None
            for hit in _every_hit(matcher, readings):
                n = where(hit.start())
                j = bisect.bisect_left(holes, n - _ANCHOR_WINDOW)
                if j < len(holes) and holes[j] <= n + _ANCHOR_WINDOW:
                    at = hit.start()
                    break
        if at is not None and (found is None or at < found[1]):
            found = (text, at)
    return found


def _argv_text(text):
    """`text` with every quoted string that holds a blank replaced by one
    placeholder word: what is left is the words a shell splits into ARGV.

    A quoted string with a blank in it reaches its program as ONE argument, so
    the words inside it are prose to that program unless the program runs them
    (`bash -c '…'`), and that case is the window's.
    A quoted string with no blank (`'workflow'`, `"$V"`) is one word either
    way and stays. `$'…'` is a quote too, with its backslash escapes."""
    out, i, n = [], 0, len(text)
    while i < n:
        ch = text[i]
        if ch == "\\":
            out.append(text[i:i + 2])
            i += 2
            continue
        ansi = ch == "$" and i + 1 < n and text[i + 1] == "'"
        if ch in "'\"" or ansi:
            quote = "'" if ansi else ch
            j = i + (2 if ansi else 1)
            while j < n and text[j] != quote:
                j += 2 if text[j] == "\\" and (quote == '"' or ansi) else 1
            span = text[i:j + 1]
            inner = span[(2 if ansi else 1):-1]
            out.append("_" if any(c in " \t\n" for c in inner) else span)
            i = j + 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _executed_windowed_row(command, readings):
    """A `workflow` row the window let pass that the shell would still run:
    one standing in the command's ARGV words, at any distance. `command` is
    the invocation text, so a data program's body is already gone from it.

    GH'S OWN GRAMMAR NEEDS NO SEPARATE READER, and one was built and measured
    dead: a mutation that removed it left every arm green. gh takes the
    noun, then flags and their one value each, then the verb as the noun's
    first positional, and `_word_positions` counts exactly that, a flag and
    its value as nothing. So a real gh call puts its verb one counted word
    after the noun however many flags stand between, inside a string or
    not, and the window refuses it."""
    argv = _readings(_argv_text(command))
    for spelling, _at, says, pieces, anchored in \
            _actions_evidence(argv, window=False)[0]:
        if _windowed_row(pieces):
            starts = [m.search(readings) for _t, m, _a in pieces]
            at = min((h.start() for h in starts if h), default=0)
            return (spelling, at, says, pieces, anchored)
    return None


def _row_stands(pieces, readings, window=True):
    """Whether every piece of one row stands in `readings`, each on a span
    that is not also another piece of the row (see `_row_start`)."""
    return _row_start(pieces, readings, [matcher.search(readings)
                                         for _t, matcher, _a in pieces],
                      window) is not None


def _executed_readings(command, keep=frozenset()):
    """The folded readings of the part of `command` a SHELL WILL RUN, or
    None where the whole command is that part.

    A QUOTED-TAG HEREDOC BODY is the one span of a command that is neither a
    command nor an argument: the shell copies it, byte for byte and with no
    expansion of any kind, onto some program's stdin. `_excise_quoted_heredocs`
    removes exactly those bodies, from the RAW lines and before the fold
    joins continuations — which is the order that matters, because a
    backslash at the end of a line inside a literal document is not a
    continuation to bash, and an excision that ran after the join lost the
    terminator and swallowed everything below it (the miss the sidechain
    rung above still carries in its comment). The same walker masks quotes
    before it looks for the `<<`, so `echo "<<'EOF'"` opens nothing and no
    real command below a FAKE opener is cut away. `keep` is the bodies a
    shell reads as its script (`_invocation_text`), which the shell runs.

    None where nothing was cut, so the ordinary command — which has no
    heredoc at all — never pays for a second fold of its own text."""
    rest = _excise_quoted_heredocs(command, keep)
    return None if rest == command else _readings(rest)


# `helm` as the recording set names it: `helm`, `bin/helm`, `./bin/helm`, or
# an ABSOLUTE path whose last two components are `bin/helm` (the symlink on
# PATH, or a checkout's own entry) built from plain path characters only, so
# no glob, tilde or expansion stands in it. `./helm` and `~/bin/helm` are
# not it, so they are read as an unknown program and nothing in them is cut.
_HELM_ARGV0 = r"helm|bin/helm|\./bin/helm|(?:/[A-Za-z0-9_.-]+)*/bin/helm"


def _anchor_in_a_region_with_an_expansion(command, readings, anchor):
    """The anchor of the first REGION of `command` that also holds an
    unresolved expansion, or None. `readings` and `anchor` are what the
    whole command already folded to, and the caller has already seen an
    expansion somewhere in it.

    A REGION is the executed text — the command less its quoted-tag bodies,
    unquoted bodies included, because those substitute — or ONE quoted body
    TOGETHER WITH THE LINE THAT OPENS IT. The outer shell never expands a
    quoted body, but the program that consumes one is on its opener line,
    and that line's words reach the program as its argv and its environment
    prefix: `python3 - "$V" <<'EOF'` and `V=$X python3 - <<'EOF'` hand
    the value to a body that reads `sys.argv` or `os.environ`, and
    `bash -s workflow <<'EOF'` hands the noun to a body that says `$1`. So
    the opener line joins every body it opens, in both directions, and is
    still part of the executed text as well. A line BEFORE the opener joins
    no body, unless a trailing backslash continues it onto the opener.

    The command with no quoted body is one region and has been read
    already, so it is answered from `anchor` without a second fold. The
    position returned is where the region's anchor piece FIRST stands in
    the folded WHOLE command, which is the text every refusal of this rung
    counts in — so it can land on a mention of the same piece in another
    region, the same residual a row's position carries. Where the fold of
    the whole does not hold that piece at all (measured: a continuation that
    joins across a cut body), it is the position in the region's own
    fold."""
    executed = _excise_quoted_heredocs(command)
    if executed == command:
        # ONE REGION, AND IT IS THE EXECUTED TEXT: the windowed anchor needs
        # its hole within the window (task/2948, see _WINDOWED_ANCHOR). The
        # readings are the ones the caller already folded.
        return anchor if anchor[0] != _WINDOWED_ANCHOR \
            else _scoped_anchor(readings, True, _readings(_argv_text(command)))
    regions = (executed,) + tuple(opener + "\n" + body for opener, body
                                  in _quoted_heredoc_bodies(command))
    for index, region in enumerate(regions):
        found = _scoped_anchor(
            _readings(region), index == 0,
            _readings(_argv_text(region)) if index == 0 else None) \
            if holds_expansion(region) else None
        if found is not None:
            text, at = found
            hit = _row_piece(text).search(readings)
            return text, (hit.start() if hit else at)
    return None


# THE WORKFLOW DIRECTORY MAY BE READ AND NEVER WRITTEN (task/2973, the
# integrator's ruling on it). The owner's rule is about WHERE CI RUNS, and
# reading a file runs nothing: a survey of this rung found 27 of
# its 212 labelled refusals were reads of that directory, and none an act.
# So a command whose ONLY row is the directory's passes when every simple
# command in it is one of the ruling's readers — `cat`, `ls`, `grep`,
# `git grep`, `git show`, and a `sed -n`, `head` or `tail` read — or `rg`,
# the grep this fleet is told to use, or `cd` or `echo`, which write nothing.
#
# THIS IS AN ALLOWLIST THAT FAILS CLOSED, and it is the one place this rung
# reads which word runs. Everything it cannot settle is a refusal exactly as
# before: an expansion or a substitution anywhere, a heredoc, a subshell or
# a group, an assignment prefix, a wrapper, a program named by a relative
# path, a shlex error, and every redirect of output except into /dev/null
# or onto another descriptor — because a `cd` earlier in the same command
# can put the working directory INSIDE the workflow directory, so any other
# target is a write whose destination the text does not settle. A READER
# THAT CAN WRITE OR RUN A PROGRAM is refused in the spelling that does it:
# sed without `-n`, with `-i` anywhere (GNU sed permutes, so a trailing
# `-i` edits), with a script file, or with a script that is anything but
# `[address[,address]]p`, which excludes `w`, `s///w` and `e`; git with any
# global option but `-C` and `--no-pager` (a `-c core.pager=` runs one),
# any subcommand but `grep` and `show`, or `-O`/`--open-files-in-pager`/
# `--output`; rg with `--pre`. A quoted word that spells an operator is read
# AS the operator, which only ever narrows the exemption.
#
# WHAT IT TRUSTS, the same residual the invocation text carries: a NAME. An
# alias, a shell function or a `PATH` inherited from an earlier call that
# makes `cat` a program which writes is outside this rung.
_DIRECTORY_ROW = ".github workflows"
_READERS = frozenset(("cat", "ls", "grep", "rg", "head", "tail", "sed", "git",
                      "cd", "echo"))
_READER_HOMES = ("/usr/bin/", "/bin/")
_OPERATOR_CHARS = frozenset(";&|<>()\n")
_SEPARATOR_CHARS = frozenset(";&|\n")
_TO_NOWHERE = frozenset((">", ">>", ">|", "&>", "&>>"))
_GIT_GLOBALS = {"-C": 2, "--no-pager": 1}
_GIT_READS = frozenset(("grep", "show"))
_GIT_WRITES = ("-O", "--open-files-in-pager", "--output")
_SED_QUIET = frozenset(("-n", "--quiet", "--silent"))
_SED_FLAGS = _SED_QUIET | frozenset((
    "-E", "-r", "--regexp-extended", "-s", "--separate", "-u", "--unbuffered",
    "-z", "--null-data", "--posix"))
_SED_SHORT = re.compile(r"-[nErsuz]+\Z")
_SED_ADDRESS = r"(?:\d+(?:~\d+)?|\$|/(?:[^/\\\n]|\\.)*/I?)"
_SED_PRINT = r"(?:%s(?:,(?:%s|[+~]\d+))?)?!?p" % (_SED_ADDRESS, _SED_ADDRESS)
_SED_PRINTS = re.compile(r"%s(?:;%s)*" % (_SED_PRINT, _SED_PRINT))


def _sed_reads(args):
    """Whether a sed argv only PRINTS what it reads (see above)."""
    scripts, operands, quiet = [], [], False
    words = iter(args)
    for word in words:
        if word in ("-e", "--expression"):
            scripts.append(next(words, ""))
        elif word.startswith("--expression="):
            scripts.append(word.split("=", 1)[1])
        elif word in _SED_FLAGS or _SED_SHORT.match(word):
            quiet = quiet or word in _SED_QUIET \
                or (word[1] != "-" and "n" in word)
        elif word.startswith("-"):
            return False
        else:
            operands.append(word)
    if not scripts and operands:
        scripts.append(operands.pop(0))
    return quiet and bool(scripts) and all(
        _SED_PRINTS.fullmatch(s) for s in scripts)


def _git_reads(args):
    """Whether a git argv is `git [-C dir] [--no-pager] grep|show …` with no
    option that opens a pager or writes a file."""
    i = 0
    while i < len(args) and args[i] in _GIT_GLOBALS:
        i += _GIT_GLOBALS[args[i]]
    return i < len(args) and args[i] in _GIT_READS \
        and not any(a.startswith(_GIT_WRITES) for a in args[i + 1:])


_READER_ARGS = {"sed": _sed_reads, "git": _git_reads,
                "rg": lambda args: not any(a.startswith("--pre")
                                           for a in args)}


def _reader(words):
    """Whether one simple command's words are a reader the exemption names."""
    verb = words[0]
    name = next((verb[len(h):] for h in _READER_HOMES if verb.startswith(h)),
                verb)
    return name in _READERS \
        and _READER_ARGS.get(name, lambda _args: True)(words[1:])


def _expands(command):
    """Whether a `$` or a backtick stands anywhere bash could expand it —
    everywhere but inside single quotes and behind a backslash. Narrower
    than `holds_expansion`, which cannot afford a quote walk on every call
    and reads `sed -n '$p'` as a hole; this runs only on a command the
    directory's row already stopped, and a `$'…'` quote counts as an
    expansion here, which only narrows the exemption."""
    quote, escaped = None, False
    for c in command:
        if escaped:
            escaped = False
        elif quote == "'":
            quote = None if c == "'" else quote
        elif c == "\\":
            escaped = True
        elif c in "$`":
            return True
        elif c == '"':
            quote = None if quote == '"' else '"'
        elif c == "'" and quote is None:
            quote = "'"
    return False


def _only_reads(command):
    """Whether every simple command in `command` is a reader and nothing in
    it can write (see above): False wherever that is not settled."""
    if _expands(command):
        return False
    lex = shlex.shlex(command, posix=True,
                      punctuation_chars="".join(sorted(_OPERATOR_CHARS)))
    lex.whitespace, lex.whitespace_split, lex.commenters = " \t\r", True, ""
    try:
        tokens = list(lex)
    except ValueError:
        return False
    commands, words, target = [], [], None
    for token in tokens:
        if target is not None:
            if not target(token):
                return False
            target = None
        elif not token or not set(token) <= _OPERATOR_CHARS:
            words.append(token)             # `''` is an empty WORD
        elif set(token) <= _SEPARATOR_CHARS:
            commands.append(words)
            words = []
        elif token in _TO_NOWHERE:
            target = "/dev/null".__eq__
        elif token == ">&":
            target = str.isdigit
        elif token == "<":
            target = bool
        else:
            return False
    commands.append(words)
    return target is None and all(_reader(w) for w in commands if w)


def _only_the_directory_is_read(command, readings, found):
    """Whether the row `found` is lifted by the read exemption: it is the
    directory's row, NO other row stands anywhere in the command at any
    distance, and the command only reads."""
    return found[0] == _DIRECTORY_ROW \
        and all(row[0] == _DIRECTORY_ROW
                for row in _actions_evidence(readings, window=False)[0]) \
        and _only_reads(command)


# ---------------------------------------------------------------------------
# THE INVOCATION TEXT (task/2973, the integrator's ruling). An Actions verb is
# an Actions invocation only where a shell RUNS it: gh as the program with its
# noun and verb, directly or through a wrapper. Text a shell hands to some
# OTHER program as data is not one, and reading it as one refused 141 of
# one seat's 212 labelled commands on the day the ruling was made — chat
# posts, task comments and notes, grep patterns, a python heredoc, a gh api
# read of another repository's PR template — while none of the 212 ran CI.
#
# SO THE TABLE READS THE COMMAND WITH ITS DATA CUT OUT, and nothing else
# about the rung changes: the fold, the rows, the anchors, the scoped rule,
# the window and the directory's read exemption all read what is left, in
# which each cut word stands as one `_` and each cut heredoc body is gone.
# What is cut, and why each is data:
#
#   * a QUOTED ARGUMENT that holds a blank, given to a program that records
#     or prints its arguments and runs none of them — `echo`, `printf`, the
#     helm verbs that post or record text (`_HELM_PROSE`), the git
#     subcommands that take a message or a pattern (`_GIT_PROSE`), and the gh
#     commands that are not Actions (`_GH_PROSE`);
#   * EVERY argument of `grep`, `egrep`, `fgrep`, `rg` and `git grep` — a
#     pattern or a file to read, and none of them runs either. `rg --pre`,
#     `rg --hostname-bin` and `git grep -O` DO run a program, so a command
#     spelling one of those is not cut;
#   * EVERY argument of a `gh api` READ. gh sends a GET unless `-X` or
#     `--method` names another method, or a `-f`, `-F`, `--field`,
#     `--raw-field` or `--input` gives the request a body, so a call with none
#     of those performs nothing, whatever its path says;
#   * a QUOTED heredoc BODY whose program reads its stdin as data — `cat`,
#     `tee`, `python`, `grep`, `rg` and the recording programs above.
#
# WHAT STAYS, which is what keeps this a guard and not a hole:
#
#   * every UNQUOTED word, whoever it is given to — `python3 x.py workflow
#     <13 words> enable` hands a program the noun and the verb as argv, and a
#     wrapper script can run them (the must-refuse row of task/2948);
#   * every argument of gh's Actions nouns, of a gh api WRITE, of a shell
#     (`bash -c '…'`), of `eval`, `ssh`, `sudo`, `xargs`, `curl`, and of any
#     program not named above, quoted or not; and every argument of a data
#     program spelled so it WRITES or ASSIGNS — `printf -v NAME`, `git …
#     --output`, `gh release download -O`/`-D` — and any quoted argument
#     whose text holds `.github` (the directory belt);
#   * any word holding a command or process substitution, because the
#     substitution RUNS whatever program's argument it sits in;
#   * EVERYTHING in a command that runs text the reader does not read as
#     commands: a current-shell evaluator (`eval`, `source`, `.`, `trap`,
#     `mapfile`), which can rebind `echo` or re-read what a data program
#     recorded, or a program word that is a VALUE (`$c`, `$(git log …)`);
#   * a heredoc body a SHELL reads — `bash`, `sh`, `zsh`, `dash`, `ksh`,
#     `ssh`, `su`, `xargs`, or a data program piped into one (`sudo -s`
#     reads as unseen text, below, which is stricter).
#     That body is a script, so it is read and EVERY row in it counts,
#     including the three with no anchor that any other quoted body is
#     forgiven (task/2870). So is the body of a program the reader does not
#     know when a shell, an evaluator or a value-run stands ANYWHERE in the
#     command — `cat <<'EOF' | (sh)`, and `while read l; do $l; done
#     <<'EOF'`, whose body the `done` owns and whose `$l` runs each line;
#   * an UNQUOTED heredoc body, which substitutes, and every other body,
#     which is read as before.
#
# IT FAILS CLOSED TWICE. First, a pipe: a data program's output is cut only
# while EVERY pipe in the command leads into a filter that runs nothing it
# reads (`_SAFE_FILTERS`) and no process substitution takes output anywhere,
# because `echo '<act>' | bash` and `for …; do echo '<act>'; done | bash`
# run their data. Second, the reader: it follows bash's own reading of
# quotes, escapes, comments, continuations, substitutions and heredocs, and
# it must agree with the heredoc walker the rest of this module uses about
# every opener and with bash about every terminator. Anywhere it cannot — an
# unclosed quote, a `case` or `coproc`, a heredoc opened inside a quote, a
# terminator the walker trims and bash does not — NOTHING is cut and the
# rung reads the whole command as it did before this reader existed.
#
# WHAT IT GIVES UP, stated so the next reader can check it: a program that
# builds a gh Actions argv inside its own source passes when that source is
# data under this ruling — a `python3 - <<'EOF'` body that calls
# `subprocess.run(['gh', …])`, and a script written by `cat > x.sh <<'EOF'`
# or `echo '…' > x.sh` and run by the next command. Both are the Write tool's
# door too, which this rung has never read, and a script run by its path is
# invisible to any reader of the command. An ANCHORLESS row in the quoted
# body of a program the reader does not know (`docker exec -i c sh <<'EOF'`,
# with no shell elsewhere in the command) passes, as it always has, and so
# does one in a data program's body that a later VALUE re-reads (`git commit
# -F - <<'EOF'` then `$(git log -1 --format=%B)`) — the value stops every
# cut, and that body is then read as before, where the three anchorless rows
# are forgiven; an evaluator re-reading it is refused. And the reader trusts
# a NAME, as the directory's read exemption does: an alias or function that
# an EARLIER call defined, making `echo` or `helm` a program that runs its
# arguments, is outside this rung.
class _Unsettled(Exception):
    """The reader met a spelling it does not read the way bash does."""


class _Word(object):
    """One shell WORD: where it stands, and what the reader saw inside it —
    a quoted blank (`prose`), a command or process substitution (`subst`),
    or nothing but plain characters (`plain`)."""

    __slots__ = ("start", "end", "raw", "prose", "subst", "plain")

    def __init__(self, text, start, end, prose, subst, plain):
        self.start, self.end, self.raw = start, end, text[start:end]
        self.prose, self.subst, self.plain = prose, subst, plain

    def value(self):
        """The word the program receives, where the text settles it: quote
        removal of a word that holds no expansion, else None."""
        if self.plain:
            return self.raw
        if "$" in self.raw or "`" in self.raw:
            return None
        try:
            parts = shlex.split(self.raw)
        except ValueError:
            return None
        return parts[0] if len(parts) == 1 else None


_WORD_BREAKS = frozenset(" \t\n;&|()<>")
_CONTROL_OPS = (";;&", ";;", ";&", "&&", "||", "|&", ";", "&", "|")
_REDIRECT_OPS = ("&>>", "&>", "<<<", "<<-", "<<", "<>", "<&", ">>", ">|",
                 ">&", "<", ">")
_FD_WORD = re.compile(r"\d+|\{[A-Za-z_][A-Za-z0-9_]*\}")
# a command word that makes bash read the text differently from this reader:
# a `case` pattern closes a parenthesis nobody opened, and a coprocess takes
# output where no pipe shows it
_UNREAD_COMMANDS = frozenset(("case", "coproc"))
# THE NAMES ARE TRUSTED, BUT NOT AGAINST THE COMMAND ITSELF. A function or an
# alias defined in the same command, a `hash -p` or `enable -f` that rebinds
# a name, or an assignment to the variables that choose which program a name
# runs, what it loads, or which editor git hands a message to, can make
# `echo`, `helm` or `git commit` run its arguments. Spelled at the TOP LEVEL
# the text shows each of them, and a command holding one is not cut at all.
_REBINDS = frozenset(("alias", "enable", "function", "hash"))
# …BUT A DEFINITION CARRIED INSIDE A STRING IS NOT SPELLED AT THE TOP LEVEL.
# `eval 'echo() { eval "$@"; }'`, a `.` or `source` of a heredoc or a file, a
# `trap` body, and a `mapfile -C` callback each run text in the CURRENT shell
# that this reader never reads as commands, so each can rebind a name the
# cut trusts — and each can re-read what a data program recorded (`git
# commit -m '<act>' && eval "$(git log -1 --format=%s)"`; measured by the
# cross-model read of task/2973 with real bash and a stub gh: all ran the
# act). A command whose program is one of these, or whose program word is a
# VALUE (`$c`, `$(…)`) the text does not settle, has nothing cut, and every
# quoted body in it is read as code. `bash -c` is a CHILD and rebinds nothing
# in the shell that runs the cut words, so it is not here.
_EVALUATORS = frozenset(("eval", "source", ".", "trap", "mapfile",
                         "readarray"))
_REBINDING_ASSIGNMENT = re.compile(
    r"(?:PATH|LD_PRELOAD|LD_LIBRARY_PATH|PYTHONPATH|PYTHONHOME|BASH_ENV"
    r"|EDITOR|VISUAL|GIT_EDITOR|GIT_SEQUENCE_EDITOR)\+?=")


def _bash_terminates(line, tag, dash):
    """Whether bash ends a heredoc body at `line`: the line IS the delimiter,
    exactly, after only the leading TABS `<<-` strips."""
    return (line.lstrip("\t") if dash else line) == tag


class _ShellReader(object):
    """Bash's reading of one command, as far as the invocation text needs it:
    the top-level tokens, every heredoc operator and whether it stands inside
    a substitution, and whether a process substitution takes output.

    THE HEREDOC WALKER IS THE AUTHORITY on bodies, and this reader must agree
    with it on every line: the same operators at the same places, and no
    operator pending while a quote or a continuation runs past a line's end,
    where bash would start the body later than the walker does. Its
    terminators must also be bash's, exactly — the walker trims a line before
    it compares, so `  EOF` ends a body for it and not for bash, and a body
    that bash kept reading would have been cut as data (measured: the line
    after such a terminator RAN). Any disagreement raises `_Unsettled`."""

    def __init__(self, text):
        self.t, self.n = text, len(text)
        self.lines = text.split("\n")
        self.starts = [0]
        for line in self.lines[:-1]:
            self.starts.append(self.starts[-1] + len(line) + 1)
        self.walk, self.resume, self.bodies = {}, {}, []
        for i, openers in _heredoc_openers(text):
            self.walk[i] = [self.starts[i] + op.start()
                            for op, _q, _s, _e, _r in openers]
            if openers:
                self.resume[i] = openers[-1][4]
            for (op, quoted, start, end, _r), (_o, _q, tag) in zip(
                    openers, _heredoc_opens(self.lines[i])):
                dash = self.lines[i].startswith("<<-", op.start())
                if any(_bash_terminates(line, tag, dash)
                       for line in self.lines[start:end]) or (
                        end < len(self.lines)
                        and not _bash_terminates(self.lines[end], tag, dash)):
                    raise _Unsettled
                self.bodies.append((self.starts[i] + op.start(), quoted,
                                    start, end))
        self.seen, self.nested, self.outproc = {}, set(), False
        # (start, end) of the text inside every `$(…)`, backtick span and
        # `<(…)` read, nested ones included, and of every `>(…)` in `outs`:
        # the env-dump rung re-reads what a printer's argument runs, and what
        # an output substitution runs on this shell's stdout. Recorded only;
        # nothing here reads it.
        self.substs, self.outs = [], []

    def line_of(self, i):
        return bisect.bisect_right(self.starts, i) - 1

    def agree(self, ln):
        """The walker's openers on line `ln`, which must be the ones read."""
        if ln not in self.walk or self.seen.get(ln, []) != self.walk[ln]:
            raise _Unsettled
        return self.walk[ln]

    def inside(self, i):
        """A newline at t[i] that does NOT end the command line — inside a
        word, or joined away by a backslash: no body may be pending there."""
        if self.agree(self.line_of(i)):
            raise _Unsettled

    def newline(self, i, nested):
        """Past the newline at t[i] that ends a command line, and past the
        bodies of the heredocs opened on it."""
        ln = self.line_of(i)
        if not self.agree(ln):
            return i + 1
        if nested and any(pos not in self.nested for pos in self.walk[ln]):
            raise _Unsettled
        resume = self.resume[ln]
        return self.starts[resume] if resume < len(self.lines) else self.n

    def read(self):
        """The top-level tokens. The last line has no newline to check it at,
        so it is checked here — unless a body the walker read ran to the end
        of the text, and then the reader never stood on it."""
        tokens = self.tokens(0, False)[0]
        if len(self.lines) - 1 in self.walk:
            self.agree(len(self.lines) - 1)
        return tokens

    def tokens(self, i, nested):
        """The tokens from t[i] to the end of the text or — `nested`, inside
        a substitution — to its closing parenthesis: (tokens, index past)."""
        t, n = self.t, self.n
        out, depth = [], 0
        while i < n:
            c = t[i]
            if c in " \t":
                i += 1
            elif c == "\\" and t[i + 1:i + 2] == "\n":
                self.inside(i + 1)
                i += 2
            elif c == "\n":
                out.append(("s", c))
                i = self.newline(i, nested)
            elif c == "#":
                j = t.find("\n", i)
                i = n if j < 0 else j
            elif c == "(":
                depth += 1
                out.append(("s", c))
                i += 1
            elif c == ")":
                if not depth:
                    if nested:
                        return out, i + 1
                    raise _Unsettled
                depth -= 1
                out.append(("s", c))
                i += 1
            elif t.startswith("&>", i) or (c in "<>" and t[i + 1:i + 2] != "("):
                i = self.redirect(i, out, nested)
            elif c in ";&|":
                op = next(o for o in _CONTROL_OPS if t.startswith(o, i))
                out.append(("p" if op in ("|", "|&") else "s", op))
                i += len(op)
            else:
                i = self.word(i, out)
        if nested:
            raise _Unsettled                # a substitution that never closes
        return out, n

    def redirect(self, i, out, nested):
        t = self.t
        op = next(o for o in _REDIRECT_OPS if t.startswith(o, i))
        if out and out[-1][0] == "w" and out[-1][1].end == i \
                and _FD_WORD.fullmatch(out[-1][1].raw):
            out.pop()                       # `2>`, `3<<`: the fd is the op's
        j = i + len(op)
        while t[j:j + 1] in (" ", "\t"):
            j += 1
        if op in ("<<", "<<-"):
            ln = self.line_of(i)
            word = _delimiter_word(self.lines[ln], j - self.starts[ln])
            if word is None:
                raise _Unsettled            # `$((1<<2))` is not an opener
            self.seen.setdefault(ln, []).append(i)
            if nested:
                self.nested.add(i)
            out.append(("h", i))
            return self.starts[ln] + word[2]
        if j >= self.n or (t[j] in _WORD_BREAKS
                           and not (t[j] in "<>" and t[j + 1:j + 2] == "(")):
            raise _Unsettled                # an operator with no word
        target = []
        j = self.word(j, target)
        out.append(("r", op, target[0][1]))
        return j

    def word(self, i, out):
        t, n = self.t, self.n
        start, prose, subst, plain = i, False, False, True
        while i < n:
            c = t[i]
            nxt = t[i + 1:i + 2]
            if c == "\\":
                if nxt == "\n":
                    self.inside(i + 1)
                i, plain = i + 2, False
            elif c in "<>" and nxt == "(":
                self.outproc = self.outproc or c == ">"
                at, i = i + 2, self.tokens(i + 2, True)[1]
                (self.substs if c == "<" else self.outs).append((at, i - 1))
                subst, plain = True, False
            elif c in _WORD_BREAKS:
                break
            elif c == "'":
                i, blank = self.single(i + 1)
                prose, plain = prose or blank, False
            elif c == '"':
                i, blank, ran = self.double(i + 1)
                prose, subst, plain = prose or blank, subst or ran, False
            elif c == "`":
                i, subst, plain = self.backtick(i + 1), True, False
            elif c == "$":
                plain = False
                if nxt == "'":
                    i, blank = self.ansi(i + 2)
                    prose = prose or blank
                elif nxt == '"':
                    i, blank, ran = self.double(i + 2)
                    prose, subst = prose or blank, subst or ran
                elif nxt in "({" and nxt:
                    i, ran = self.expansion(i)
                    subst = subst or ran
                else:
                    i += 1
            else:
                i += 1
        word = _Word(t, start, i, prose, subst, plain)
        if word.raw in _UNREAD_COMMANDS and word.plain and (
                not out or out[-1][0] in "sp" or (
                    out[-1][0] == "w" and out[-1][1].raw in _COMMAND_PREFIX)):
            raise _Unsettled
        out.append(("w", word))
        return i

    def blanks(self, i, j):
        """Whether t[i:j] holds a blank, each newline in it checked."""
        k = self.t.find("\n", i, j)
        while k >= 0:
            self.inside(k)
            k = self.t.find("\n", k + 1, j)
        return any(c in " \t\n" for c in self.t[i:j])

    def single(self, i):
        j = self.t.find("'", i)
        if j < 0:
            raise _Unsettled
        return j + 1, self.blanks(i, j)

    def ansi(self, i):
        t, n, start = self.t, self.n, i
        while i < n and t[i] != "'":
            i += 2 if t[i] == "\\" else 1
        if i >= n:
            raise _Unsettled
        return i + 1, self.blanks(start, i)

    def double(self, i):
        t, n = self.t, self.n
        blank = ran = False
        while i < n:
            c = t[i]
            if c == '"':
                return i + 1, blank, ran
            if c == "\\":
                if t[i + 1:i + 2] == "\n":
                    self.inside(i + 1)
                i += 2
            elif c == "`":
                i, ran = self.backtick(i + 1), True
            elif c == "$" and t[i + 1:i + 2] in ("(", "{"):
                i, sub = self.expansion(i)
                ran = ran or sub
            else:
                if c == "\n":
                    self.inside(i)
                blank = blank or c in " \t\n"
                i += 1
        raise _Unsettled

    def backtick(self, i):
        t, n, at = self.t, self.n, i
        while i < n and t[i] != "`":
            if t[i] == "\n":
                self.inside(i)
            i += 2 if t[i] == "\\" else 1
        if i >= n:
            raise _Unsettled
        self.substs.append((at, i))
        return i + 1

    def expansion(self, i):
        """Past the `$(…)`, `$((…))` or `${…}` at t[i]: (index, whether it
        runs a program — arithmetic is counted as one, which only narrows
        what is cut). A single quote inside `${…}` is a quote in one context
        and a letter in another, and bash versions disagree about which, so
        it is not read at all."""
        t, n = self.t, self.n
        if t.startswith("$((", i):
            depth, i = 2, i + 3
            while i < n and depth:
                c = t[i]
                if c == "\n":
                    self.inside(i)
                depth += {"(": 1, ")": -1}.get(c, 0)
                i += 1
            if depth:
                raise _Unsettled
            return i, True
        if t.startswith("$(", i):
            end = self.tokens(i + 2, True)[1]
            self.substs.append((i + 2, end - 1))
            return end, True
        depth, ran, i = 1, False, i + 2
        while i < n:
            c = t[i]
            if c == "\\":
                if t[i + 1:i + 2] == "\n":
                    self.inside(i + 1)
                i += 2
            elif c == "'":
                raise _Unsettled
            elif c == '"':
                i, _blank, sub = self.double(i + 1)
                ran = ran or sub
            elif c == "`":
                i, ran = self.backtick(i + 1), True
            elif c == "$" and t[i + 1:i + 2] == "(":
                i, ran = self.expansion(i)[0], True
            elif c == "$" and t[i + 1:i + 2] == "{":
                depth, i = depth + 1, i + 2
            elif c == "}":
                depth, i = depth - 1, i + 1
                if not depth:
                    return i, ran
            else:
                if c == "\n":
                    self.inside(i)
                i += 1
        raise _Unsettled


class _Simple(object):
    """One simple command of the top level: its argv words, its redirection
    targets, the heredoc operators it owns, and its place in a pipeline."""

    __slots__ = ("words", "targets", "docs", "pipeline", "stage")

    def __init__(self, pipeline, stage):
        self.words, self.targets, self.docs = [], [], []
        self.pipeline, self.stage = pipeline, stage


def _simple_commands(tokens):
    """The simple commands of a token stream, empty ones kept: an empty stage
    after a pipe is a subshell or a group, and reads as unknown."""
    out = [_Simple(0, 0)]
    for token in tokens:
        kind, cmd = token[0], out[-1]
        if kind == "w":
            cmd.words.append(token[1])
        elif kind == "r":
            cmd.targets.append(token[2])
        elif kind == "h":
            cmd.docs.append(token[1])
        elif kind == "p":
            out.append(_Simple(cmd.pipeline, cmd.stage + 1))
        else:
            out.append(_Simple(cmd.pipeline + 1, 0))
    return out


def _options(words, k, valued=(), flags=()):
    """The index of the first operand at or after `words[k]`, past the
    options this program takes — a flag, or an option and its value, joined
    (`-n5`, `--signal=9`) or not — or None at an option it does not take."""
    while k < len(words):
        v = words[k].value()
        if v is None:
            return None if words[k].raw.lstrip("'\"\\").startswith("-") else k
        if v == "--":
            return k + 1
        if not v.startswith("-") or v == "-":
            return k
        if v in flags:
            k += 1
        elif v in valued:
            k += 2
        elif v.split("=", 1)[0] in valued and v.startswith("--") and "=" in v:
            k += 1
        elif v[:2] in valued and not v.startswith("--"):
            k += 1
        else:
            return None
    return k


def _after_env(words, k):
    k = _options(words, k, valued=("-u", "-C", "--unset", "--chdir"),
                 flags=("-i", "-0", "-v", "--ignore-environment", "--null",
                        "--debug"))
    while k is not None and k < len(words) \
            and _GRANT_ASSIGNMENT.match(words[k].raw):
        k += 1
    return k


def _after_timeout(words, k):
    k = _options(words, k, valued=("-s", "-k", "--signal", "--kill-after"),
                 flags=("-v", "--verbose", "--preserve-status", "--foreground"))
    return None if k is None or k >= len(words) else k + 1     # the duration


def _after_nice(words, k):
    if k < len(words) and re.fullmatch(r"-\d+", words[k].value() or ""):
        k += 1
    return _options(words, k, valued=("-n", "--adjustment"))


# The programs that run the command after their own options, whose name bash
# does not strip itself. `sudo -s`, `sudo -i`, `sudo -e`, `env -S` and
# `command -v` are none of this (a shell, an editor, a split string, a
# lookup). An option a row does not name stops the walk, and a program the
# text does not settle has nothing cut in the whole command and its heredoc
# read as code (`_EVALUATORS`) — which is how `sudo -s <<'EOF'` and `sudo -i
# <<'EOF'`, a shell reading its stdin, are read — except `command -v`, which
# only prints a path.
_WRAPPERS = {
    "builtin": _options,
    "exec": lambda words, k: _options(words, k, valued=("-a",),
                                      flags=("-c", "-l")),
    "env": _after_env,
    "timeout": _after_timeout,
    "nice": _after_nice,
    "nohup": _options,
    "command": lambda words, k: _options(words, k, flags=("-p",)),
    "stdbuf": lambda words, k: _options(
        words, k, valued=("-i", "-o", "-e", "--input", "--output", "--error")),
    "setsid": lambda words, k: _options(
        words, k, flags=("-c", "-f", "-w", "--ctty", "--fork", "--wait")),
    "sudo": lambda words, k: _options(
        words, k,
        valued=("-u", "-g", "-p", "-C", "-T", "-r", "-t", "-U", "-D", "-R",
                "--user", "--group", "--prompt", "--close-from",
                "--command-timeout", "--role", "--type", "--other-user",
                "--chdir", "--chroot"),
        flags=("-A", "-b", "-E", "-H", "-k", "-K", "-n", "-P", "-S",
               "--askpass", "--background", "--preserve-env", "--set-home",
               "--non-interactive", "--preserve-groups", "--stdin")),
}
# the words bash reads before a command without making them its argv
_COMMAND_PREFIX = frozenset(("!", "time", "if", "then", "elif", "else",
                             "while", "until", "do", "{"))


def _program(words):
    """(name, index) of the program a simple command runs — its prefix
    words, assignments and wrappers stepped over. `(None, None)` where there
    is no program (only assignments); `(None, index)` where the text does not
    settle it — a program word that is a value, or a wrapper option no row
    names — which `_cut_data` reads as unseen text run. A path in a trusted
    home reads as its name, and helm reads as `helm` in every spelling the
    recording set trusts."""
    k = 0
    while k < len(words):
        w = words[k]
        if w.plain and w.raw in _COMMAND_PREFIX:
            k += 2 if w.raw == "time" and k + 1 < len(words) \
                and words[k + 1].raw == "-p" else 1
        elif _GRANT_ASSIGNMENT.match(w.raw):
            k += 1
        elif w.plain and w.raw in _WRAPPERS:
            at, k = k, _WRAPPERS[w.raw](words, k + 1)
            if k is None:
                return ("command", at) if w.raw == "command" else (None, at)
        elif not w.plain:
            return None, k
        elif re.fullmatch(_HELM_ARGV0, w.raw):
            return "helm", k
        else:
            return next((w.raw[len(h):] for h in _READER_HOMES
                         if w.raw.startswith(h)), w.raw), k
    return None, None


# THE SETS, each closed and each read by NAME.
#: helm verbs that post, send or record the text they are given
_HELM_PROSE = frozenset(("asks", "chat", "dispatch", "handoff", "lr", "note",
                         "premise", "store", "task"))
#: git subcommands whose text arguments are a message, a pattern or a path
_GIT_PROSE = frozenset(("blame", "commit", "diff", "grep", "log", "merge",
                        "notes", "shortlog", "show", "stash", "status", "tag"))
#: the only git options that may stand before one of those
_GIT_PROSE_GLOBALS = frozenset(("-C", "--no-pager", "-P"))
#: gh commands that are not Actions and run nothing they are given
_GH_PROSE = frozenset(("gist", "issue", "label", "pr", "release", "repo",
                       "search"))
_GREPS = frozenset(("grep", "egrep", "fgrep", "rg"))
#: the programs that find or list processes: every argument is a pattern or a
#: selector, and none of them runs or writes anything it is given
_PROCESS_READERS = frozenset(("pgrep", "pkill", "pidof", "ps"))
#: the configuration a `git -c` may set before a prose subcommand and leave
#: it prose: none of these names a program git would run (an editor, a pager,
#: a hook, an alias) or a file it would write
_GIT_SAFE_CONFIG = frozenset(("user.name", "user.email", "commit.gpgsign",
                              "core.quotepath", "color.ui"))
#: python text that starts another process, and so is code wherever a rung
#: would otherwise read python's text as data. The module name is matched
#: QUOTED TOO: `__import__("os").system(...)` and
#: `importlib.import_module("subprocess")` hide it behind string marks, and
#: the pre-cut fold caught the verb in that text (a cross-family delta read).
#: Kept for `_authority_text`'s command-wide spawn question; python's own
#: text is decided by the allowlist (`python_text_inert`), never this list.
_SPAWNS = re.compile(r"subprocess|\bPopen\b|\bpexpect\b|\brunpy\b"
                     r"|\b__import__\b|\bimport_module\b"
                     r"|[\"']?os[\"']?\s*\.\s*(?:system|popen|exec|spawn|posix_spawn)"
                     r"|[\"']?pty[\"']?\s*\.\s*spawn")

#: THE INERT-IMPORT ALLOWLIST (task/3071). Python's `-c` text and a heredoc
#: fed to python count as DATA only when every import comes from this set —
#: modules that cannot start a process, open a socket or write a file the
#: text did not name — and no dynamic primitive appears anywhere. The
#: denylist it replaces (`_SPAWNS` as the data test) enumerated ways to
#: spawn and kept missing them: from-os-import-system, `import os as o`,
#: `getattr(os, "system")` and `exec` of a built string all read as data
#: beside it (a cross-family delta read of task/3060). An allowlist fails the other
#: way: what it does not know is code.
_PY_INERT_MODULES = frozenset((
    "re", "json", "sys", "pathlib", "ast", "difflib", "textwrap",
    "collections", "itertools", "functools", "io", "datetime", "hashlib",
    "base64", "csv", "glob", "fnmatch", "string", "math", "statistics",
    "tokenize", "shlex", "time", "typing"))
#: the os attributes that start nothing and write nothing
_PY_OS_SAFE = frozenset((
    "path", "environ", "getcwd", "listdir", "walk", "makedirs", "remove",
    "rename", "replace", "mkdir", "rmdir", "getenv", "urandom", "sep",
    "linesep"))
#: the dynamic primitives: text naming one is code, however it was imported
_PY_DYNAMIC = frozenset((
    "exec", "eval", "compile", "__import__", "import_module", "getattr",
    "globals", "vars", "breakpoint"))


def _parses_python(src):
    """Whether the text parses as python at all. A body that does not is
    prose the interpreter would only crash on, and the standing answer for
    it (task/2973) is data."""
    import ast                              # local: paid only by python calls
    try:
        ast.parse(src)
        return True
    except (SyntaxError, ValueError, RecursionError):
        return False


def python_text_inert(src):
    """Whether python source is INERT under the allowlist above: every import
    from _PY_INERT_MODULES (os only through its safe attributes), no dynamic
    primitive named or attributed anywhere. Unparseable is False — text that
    may not be read as data is never data."""
    import ast                              # local: paid only by python calls
    try:
        tree = ast.parse(src)
    except (SyntaxError, ValueError, RecursionError):
        return False
    os_aliases = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                root, _, rest = a.name.partition(".")
                if root == "os":
                    if rest:
                        # import os.path: the safe subtree, whole
                        if rest.split(".")[0] != "path":
                            return False
                    else:
                        os_aliases.add(a.asname or "os")
                elif root not in _PY_INERT_MODULES:
                    return False
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            root, _, rest = module.partition(".")
            if root == "os":
                if rest.split(".")[0] == "path":
                    continue            # from os.path import X: safe whole
                for a in node.names:
                    if a.name.partition(".")[0] not in _PY_OS_SAFE:
                        return False
            elif root not in _PY_INERT_MODULES:
                return False
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in _PY_DYNAMIC:
            return False
        if isinstance(node, ast.Attribute):
            if node.attr in _PY_DYNAMIC:
                return False
            # an attribute off an os alias must be a safe one
            if isinstance(node.value, ast.Name) \
                    and node.value.id in os_aliases \
                    and node.attr not in _PY_OS_SAFE:
                return False
    return True
#: the programs whose stdin is DATA, besides the recording ones above
_STDIN_DATA = frozenset(("cat", "tee")) | _GREPS
_PYTHON = re.compile(r"python(?:[23](?:\.\d+)?)?")
#: the programs whose stdin is a SCRIPT, or the arguments of one
_STDIN_SHELLS = frozenset(("bash", "sh", "zsh", "dash", "ksh", "mksh", "ash",
                           "fish", "ssh", "su", "xargs", "source", ".", "eval"))
#: a pipe may lead into these and a data program's output stays data
_SAFE_FILTERS = frozenset(("base64", "cat", "column", "cut", "egrep", "fgrep",
                           "fold", "grep", "head", "jq", "nl", "rg", "sort",
                           "tail", "tee", "tr", "uniq", "wc"))
# a gh api option: those that take a value, those that take none, and those
# that give the request a body
_GH_API_VALUED = frozenset(("-H", "--header", "-q", "--jq", "-t",
                            "--template", "-p", "--preview", "--cache",
                            "--hostname"))
_GH_API_FLAGS = frozenset(("-i", "--include", "--paginate", "--silent",
                           "--slurp", "--verbose"))
_GH_API_BODIES = frozenset(("-f", "-F", "--field", "--raw-field", "--input"))


def _runs_a_program(args, spellings):
    """Whether an argument spells one of the options that run a program,
    read with its quotes and backslashes stripped so a quoted one counts."""
    return any(re.sub(r"['\"\\]", "", a.raw).startswith(spellings)
               for a in args)


def _gh_api_reads(args):
    """Whether a `gh api` call (`args` are the words after `api`) is a READ:
    one endpoint that is not `graphql`, no method but GET or HEAD, and no
    option that gives the request a body. An option this does not know, or
    one spelled with an expansion, is not a read."""
    endpoints, k = [], 0
    while k < len(args):
        v = args[k].value()
        nxt = args[k + 1].value() if k + 1 < len(args) else None
        if v is None:
            if args[k].raw.lstrip("'\"\\").startswith("-"):
                return False
            endpoints.append(v)
            k += 1
            continue
        if v == "--":
            endpoints.extend(a.value() for a in args[k + 1:])
            break
        if not v.startswith("-") or v == "-":
            endpoints.append(v)
            k += 1
            continue
        name, eq, attached = v.partition("=")
        if not name.startswith("--"):
            # a cluster of short options: `-i` any number of times, then at
            # most one that takes a value, joined to it or in the next word
            j = 1
            while j < len(v) and v[j] == "i":
                j += 1
            if j == len(v):
                k += 1
                continue
            name, attached, eq = "-" + v[j], v[j + 1:], v[j + 1:] != ""
        if name in _GH_API_BODIES:
            return False
        if name in ("-X", "--method"):
            method = attached if eq else nxt
            if method is None or method.upper() not in ("GET", "HEAD"):
                return False
        elif name in _GH_API_FLAGS and not eq:
            k += 1
            continue
        elif name not in _GH_API_VALUED:
            return False
        k += 1 if eq else 2
    return len(endpoints) == 1 and endpoints[0] != "graphql"


# THE DIRECTORY BELT (the integrator's ruling, task/2973): a
# quoted argument whose text holds `.github` is never cut as prose, whatever
# program it is given to. An option that writes a file through a quoted path
# with a blank in it (`git log --output='<dir>/ci .yml'`, `gh release
# download -O …`) was data to every rule above it, and cutting it admitted a
# real write (measured by the cross-model read); the options themselves are
# named below too, and this is the belt behind them. A mention in a post
# costs one refusal; a write costs a runner. The pattern of a grep and the
# path of a gh api GET are not prose cuts and keep theirs: neither program
# writes.
_DIRECTORY_ANCHOR = _DIRECTORY_ROW.split(" ")[0]


def _names_the_directory(word):
    """Whether any reading of `word` holds the directory row's anchor."""
    return any(_DIRECTORY_ANCHOR in reading
               for reading in folded_commands(word.raw))


def _python_text(args):
    """The `-c` program text of a python invocation (`args` are the words
    after the program), as the one data word, or [] where there is none or
    it is code: the text is not INERT under the import allowlist
    (`python_text_inert`, task/3071), it holds a substitution the shell runs
    first, or it names the workflow directory (the belt above). A script
    path or `-m` makes every later word the program's argv, which it may
    run, so nothing after it is data."""
    j = 0
    while j < len(args):
        v = args[j].value()
        if v is None or v == "-m" or not v.startswith("-") or v == "-":
            return []
        if re.fullmatch(r"-[A-Za-z]*c", v):
            code = args[j + 1] if j + 1 < len(args) else None
            if code is None or code.subst \
                    or not python_text_inert(code.value() or "") \
                    or _names_the_directory(code):
                return []
            return [code]
        j += 2 if v in ("-W", "-X") else 1
    return []


def _command_data(words, interpreters=None, bodies=None, docs=()):
    """(the words of one simple command that are data, what its program
    makes of its stdin: "data", "shell" or None where unknown).

    THE ONE DATA PREDICATE, shared by the GitHub-Actions, owner-posture and
    sidechain authority rungs: which words a program is handed that it never
    runs (a grep pattern, a process reader's selector, a post's prose, a
    commit message, python's `-c` text under the inert-import allowlist), and
    whether the body it reads on stdin is data. A program it does not name
    answers nothing, and its words stay code.

    `bodies` maps a heredoc's operator token to its text and `docs` is the
    operator tokens THIS command owns (the caller's reader.bodies keyed by
    `_simple_commands`' docs), so a program whose stdin is data ONLY of one
    kind — python's, which must parse inert under the allowlist (task/3071)
    — can ask the body which it is. Without them, python's stdin answers as
    before: data.

    `interpreters` (a compiled pattern, or None) names programs a CALLER's
    rung reads as running their text: their arguments and their stdin are
    code, exactly like a shell's. The Actions rung passes none, because a
    python body naming a gh act runs no gh; the owner-posture rung passes
    python, because a python body naming the mint runs the mint."""
    name, k = _program(words)
    if name is None:
        return (), None
    base = name.rsplit("/", 1)[-1]
    if base in _STDIN_SHELLS or (interpreters is not None
                                 and interpreters.fullmatch(base)):
        return (), "shell"
    args = words[k + 1:]
    prose = [w for w in args
             if w.prose and not w.subst and not _names_the_directory(w)]
    every = [w for w in args if not w.subst]
    # `printf -v NAME` ASSIGNS its output to a shell variable instead of
    # printing it, and `$NAME` then runs it: an argument of that printf is
    # the command's code, not data
    if name == "printf" and _runs_a_program(args, ("-v",)):
        return (), None
    if name in ("echo", "printf"):
        return prose, "data"
    if name in _GREPS:
        if _runs_a_program(args, ("--pre", "--hostname-bin")):
            return (), None
        return every, "data"
    if name in _PROCESS_READERS:
        return every, "data"
    if _PYTHON.fullmatch(name):
        # A HEREDOC BODY FED TO PYTHON IS ITS PROGRAM, and a program is data
        # only under the same allowlist as the -c text (task/3071): the body
        # that imports os.system read as data beside the denylist. A body
        # that does not PARSE as python is prose python would only crash on
        # (task/2973's standing answer: a python body is data); only a
        # parseable body that fails the allowlist stays code.
        if bodies:
            for at in docs:
                body = bodies.get(at)
                if body is not None and _parses_python(body) \
                        and not python_text_inert(body):
                    return _python_text(args), None
        return _python_text(args), "data"
    if name in _STDIN_DATA:
        return (), "data"
    if name == "helm":
        verb = args[0].value() if args else None
        return (prose, "data") if verb in _HELM_PROSE else ((), None)
    if name == "git":
        j = 0
        while j < len(args):
            v = args[j].value()
            if v in _GIT_PROSE_GLOBALS:
                j += 2 if v == "-C" else 1
            elif v == "-c" and j + 1 < len(args) and (
                    args[j + 1].value() or "").split("=", 1)[0].lower() \
                    in _GIT_SAFE_CONFIG:
                j += 2
            else:
                break
        sub = args[j].value() if j < len(args) else None
        # `-O` runs a pager and `--output` WRITES a file (`_GIT_WRITES`,
        # the read exemption's own list)
        if sub not in _GIT_PROSE or _runs_a_program(args, _GIT_WRITES):
            return (), None
        if sub == "grep":
            return [w for w in args[j + 1:] if not w.subst], "data"
        # `-e` hands the message to an editor, which is a program of its own
        if any(a.value() in ("-e", "--edit") for a in args[j + 1:]):
            return (), None
        return prose, "data"
    if name == "gh":
        j = 0
        while j < len(args) and (args[j].value() or "").split("=")[0] in (
                "-R", "--repo", "--hostname"):
            j += 1 if "=" in (args[j].value() or "") else 2
        sub = args[j].value() if j < len(args) else None
        if sub == "api":
            rest = args[j + 1:]
            return (([w for w in rest if not w.subst], "data")
                    if _gh_api_reads(rest) else ((), None))
        # an option that names where a download or a clone is WRITTEN
        if sub not in _GH_PROSE or _runs_a_program(
                args, ("-O", "--output", "-D", "--dir")):
            return (), None
        return prose, "data"
    return (), None


def _safe_stage(cmd):
    """Whether a pipe may lead into this stage and a data program's output
    stay data: a filter that runs nothing it reads, and runs no program of
    its own through a substitution."""
    name, _k = _program(cmd.words)
    return name in _SAFE_FILTERS \
        and not any(w.subst for w in cmd.words + cmd.targets) \
        and not _runs_a_program(cmd.words, ("--pre", "--hostname-bin"))


def _invocation_text(command, interpreters=None):
    """(text, code): `command` with its DATA cut out (see above) — each data
    word read as one `_`, each data heredoc body gone — and the ordinals, in
    the heredoc walker's order, of the quoted bodies a SHELL reads, which
    `_row_the_shell_runs` reads whole. `(command, frozenset())` wherever the
    reader cannot settle bash's reading.

    AND WHEREVER THE READER BREAKS. This rung stands on every Bash call and
    its hook fails OPEN on an exception, so an error in the reader would
    admit the command whole, every act in it included. An error here is
    caught and the command is read whole instead: a defect in this reader
    costs the refusals it was meant to lift, never a refusal the rung owes.
    The failure is not hypothetical: while this section was built, a name it
    shadowed made `_git_reads`, one call further on, raise, and the hook
    admitted `git -C <dir> add <workflow file>` (tests caught it).

    ONE FOLD, TWO RUNGS. The owner-posture rung (task/3018) reads the same
    text, passing `interpreters` so python's arguments and stdin count as
    code there (`_command_data`); every other rule, the re-executors
    included, is this one."""
    try:
        return _cut_data(command, interpreters)
    except Exception:           # the reader's defect must not open the rung
        return command, frozenset()


def _cut_data(command, interpreters=None):
    """`_invocation_text`'s work, which may raise."""
    try:
        reader = _ShellReader(command)
        tokens = reader.read()
    except _Unsettled:
        return command, frozenset()
    if any(kind == "w" and (_REBINDING_ASSIGNMENT.match(word.raw)
                            or tokens[k + 1:k + 3] == [("s", "("), ("s", ")")])
           for k, (kind, word) in enumerate(t[:2] for t in tokens)):
        return command, frozenset()
    commands = _simple_commands(tokens)
    programs = [_program(cmd.words) for cmd in commands]
    if any(name in _REBINDS for name, _k in programs):
        return command, frozenset()
    # UNSEEN TEXT RUNS: a current-shell evaluator, or a program word that is a
    # value, runs text this reader does not read as commands — it can rebind
    # a name, or re-read what a data program recorded — so nothing is cut and
    # every quoted body is code (`_EVALUATORS`)
    evaluated = any(name in _EVALUATORS for name, _k in programs)
    unseen = evaluated or any(name is None and k is not None
                              for name, k in programs)
    safe = not unseen and not reader.outproc and all(
        _safe_stage(cmd) for cmd in commands if cmd.stage)
    body_text = {at: "\n".join(reader.lines[start:end])
                 for at, _q, start, end in reader.bodies}
    stdins = [_command_data(cmd.words, interpreters, body_text, cmd.docs)
              for cmd in commands]
    # A SHELL ANYWHERE in the command can be the one that reads a body a
    # program the reader does not know hands on — through a pipe into `(sh)`
    # or `{ sh; }`, or a loop whose body runs each line it reads (`while read
    # l; do bash -c "$l"; done <<'EOF'`, whose heredoc the `done` owns)
    shell = any(stdin == "shell" for _data, stdin in stdins)
    cuts, kinds = [], {}
    for cmd, (data, stdin) in zip(commands, stdins):
        if safe:
            cuts.extend((w.start, w.end, "_") for w in data)
        # a shell later in the body's OWN pipeline reads it, whatever the pipe
        # fence says: the shell set is a second layer behind the fence
        piped = any(later == "shell" for c, (_d, later) in zip(commands, stdins)
                    if c.pipeline == cmd.pipeline and c.stage > cmd.stage
                    ) if cmd.docs else False
        for at in cmd.docs:
            if at in reader.nested:
                continue
            if stdin == "shell" or piped or (stdin != "data" and (
                    shell or unseen)) or (shell and not safe):
                kinds[at] = "code"
            elif stdin == "data" and safe:
                kinds[at] = "data"
    code, expected = set(), []
    for ordinal, (at, quoted, start, end) in enumerate(reader.bodies):
        cut = quoted and kinds.get(at) == "data"
        if quoted and (evaluated or kinds.get(at) == "code"):
            code.add(ordinal)
        if cut and start < end:
            # the body's lines go and its terminator stays; a body that runs
            # to the end of the text takes the opener line's newline with it,
            # or the walker would read the empty last line as its body
            cuts.append((reader.starts[start], reader.starts[end], "")
                        if end < len(reader.lines)
                        else (reader.starts[start] - 1, reader.n, ""))
        expected.append((quoted, 0 if cut else end - start))
    out, at = [], 0
    for start, end, fill in sorted(cuts):
        out.append(command[at:start] + fill)
        at = end
    text = "".join(out) + command[at:]
    walked = [(quoted, end - start) for _i, openers in _heredoc_openers(text)
              for _o, quoted, start, end, _r in openers]
    if walked != expected:
        return command, frozenset()
    return text, frozenset(code)


def github_actions_refusal(command=None, path=None):
    """The act a Bash `command` or a Write/Edit `path` performs that the
    owner's local-fabric rule refuses, or None.

    For a COMMAND this is presence over the folded INVOCATION TEXT — the
    command with the data a shell hands to programs that never run it cut
    out (`_invocation_text`, task/2973). The earliest protected spelling in
    the plainest reading wins, and the answer names it, the character it
    stands at in the FOLDED invocation text, and whether it stands in a
    heredoc body, because a refusal that will not say WHERE it looked cannot
    be argued with — and a number alone was argued with WRONGLY (see
    `_row_refusal`). Where no row and no anchor stands in the whole command,
    nothing is cut and nothing is read: the ordinary command never pays for
    the reader.

    A QUOTED heredoc BODY a program reads that is neither a data program nor
    a shell — an unknown one — is read as it always was: an ANCHORLESS row
    standing only there is not evidence, and an anchored one is. A body a
    SHELL reads is its script, and every row in it is evidence.
    `_row_the_shell_runs` holds both rules and states what each gives up.

    The override word at the FRONT of the raw command lifts the whole
    command; the same word anywhere else lifts nothing. A command that only
    READS the workflow directory, and names no other row, is lifted without
    it (`_only_the_directory_is_read`, task/2973); a WRITE into that
    directory never is, and a `path` below is always a write.

    A `path` IS TEXT TOO, and goes through the same fold and the same table.
    It had a private matcher of its own once, which knew only
    `.github/workflows` and did not fold case, so the Write door admitted
    `.github/actions/build/action.yml` and `.GITHUB/WORKFLOWS/ci.yml` while
    Bash refused the identical act (measured). Two guards for one property
    disagree sooner or later, so there is one: a path is refused when its
    text holds a row, which makes `.github/workflows/../ISSUE_TEMPLATE/x` a
    refusal as well — the text names the directory, and the cure is to spell
    the path it resolves to. A Write cannot carry the override word, so the
    refusal sends deliberate work to Bash, where the grant is a word.

    AN ANCHOR BESIDE AN UNRESOLVED EXPANSION IS A ROW TOO — the scoped
    rule, and as much of the runtime-value limit as the fleet's own traffic
    will pay for. A token supplied WHOLE by a runtime value
    (`gh workflow $V ci.yml`) runs the act with a word the text does not
    hold, and no reading of that text can hold a piece nobody wrote; the
    blanket cure — refuse every command carrying an expansion — costs nearly
    every command the fleet runs.

    BESIDE MEANS IN ONE REGION. A region is the executed text — the command
    less its quoted-tag bodies, unquoted bodies kept, because those
    substitute — or ONE quoted body together with the line that opens it.
    The outer shell never expands a quoted body, so an expansion on a
    DIFFERENT line cannot supply a word inside it; the opener line can,
    because the program that consumes the body stands there and takes that
    line's words as its argv and its environment prefix
    (`python3 - "$V" <<'EOF'` reading `sys.argv[1]`,
    `V=$X python3 - <<'EOF'` reading `os.environ`). Taken from anywhere,
    the anchor and the expansion refused a chat post whose quoted document
    said `rerun` as prose, because a separate command on a later line
    echoed `$?` (reported by a seat, reproduced in
    `tests.test_chat_argv_guard`). An anchor and an expansion in the SAME
    quoted body are still refused, because `bash <<'EOF'` and
    `python3 - <<'EOF'` execute that body: the anchor stays evidence
    wherever it stands, and only the pairing is scoped. The opener line is
    read whole and on purpose: `echo $? ; python3 - <<'EOF'` joins its `$?`
    to the body too, which costs a refusal and never a miss.

    WHAT THE REGION GIVES UP: a body that reads the missing word from state
    an EARLIER line left for it — `export V=$X` or a file written from `$X`,
    then a later `python3 - <<'EOF'` whose body names the anchor and reads
    `os.environ` or the file — holds its anchor in one region and its
    expansion in another, and passes. A body that asks the SHELL for the
    value spells an expansion of its own and is refused wherever the value
    came from.

    THE SCOPED REFUSAL NAMES THE ANCHOR AND ITS POSITION AND NO ROW. The
    anchor belongs to several rows, and the one a row description would
    name is a guess made by table order (`gh workflow $V` is not known to
    enable anything); it also costs up to forty characters of a line whose
    length is pinned (`PreToolUse/refusal-ci-runner`), and with it the
    scoped refusal does not fit that pin. `tests.test_hook_budgets` renders
    this refusal through the rung for every anchor at a five-digit position
    and holds it to the pin.

    THE SCOPE IS THE ANCHOR, WHICH IS WHAT MAKES A COMMAND AN ACTIONS
    COMMAND. Scoped to every piece of the table it refused one command in
    nine of the 120,275 the fleet actually ran, because `run`, `list`,
    `view`, `watch` and `cancel` are ordinary English that ordinary work
    interpolates around. Scoped to the anchors — the noun or path fragment
    that means nothing outside GitHub Actions, the `+` of each row — it
    refuses the shapes that reach the act: `gh workflow $V ci.yml` and
    `gh $W rerun 123` are refused, while `gh pr view $(git branch
    --show-current)`, `helm gate run --repo $WT`, `helm chat read --room $R`
    and `echo $HOME` are not.

    WHAT THAT LEAVES OPEN is written here because it is the same limit as
    before, narrowed and not closed: a row with no anchor has no scoped rule
    at all, so `gh $W enable ci.yml` and `gh $W download 12` pass. `enable`,
    `disable`, `download`, `watch` and `cancel` are ordinary English words
    this fleet types every day, and an ordinary word beside a hole says
    nothing about GitHub; the verb is never what makes a command an Actions
    command. A runtime value that supplies the NOUN is outside this guard,
    exactly as a whole token beside no piece at all is. THE SAME THREE
    ANCHORLESS ROWS are what the document rule gives up, and they are the
    same words for the same reason: `gh run cancel|watch|download` written
    inside a quoted heredoc whose program this rung does not know passes,
    while every anchored act there is refused, and so is every act in a body
    a shell reads.

    A PATH takes the row rule and not this one: a `file_path` is handed to
    the tool literally, nothing expands it, and `$W` in a path is a
    directory named `$W`."""
    if path is not None:
        rows = _actions_evidence(_readings(path))[0]
        if rows:
            return "%s, whose path holds %s, which %s" % (
                path, rows[0][0], rows[0][2])
        return None
    if _grants_whole_command(command):
        return None
    readings = _readings(command)
    rows, anchor = _actions_evidence(readings)
    if not rows and anchor is None:
        return None             # every windowed row carries its anchor
    text, code = _invocation_text(command)
    if text != command:
        readings = _readings(text)
        rows, anchor = _actions_evidence(readings)
    found = _row_the_shell_runs(text, rows, code)
    if found is None:
        found = _executed_windowed_row(text, readings)
    if found is not None:
        if _only_the_directory_is_read(text, readings, found):
            return None
        return _row_refusal(text, found)
    if anchor is None or not holds_expansion(text):
        return None
    anchor = _anchor_in_a_region_with_an_expansion(text, readings, anchor)
    if anchor is None:
        return None
    return ("%s at character %d of the folded command, beside an "
            "unresolved expansion" % anchor)


def _row_the_shell_runs(command, rows, code=frozenset()):
    """The earliest row of `rows` that is evidence, or None. `command` is the
    invocation text, so a body a data program reads is gone from it already,
    and `code` names the quoted bodies a SHELL reads (`_invocation_text`).

    AN ANCHOR IS EVIDENCE WHEREVER IT STANDS IN THAT TEXT. `workflow`,
    `rerun`, `.github`, `/actions` and `/dispatches` mean nothing outside
    GitHub Actions, so a row carrying one is refused in any body this rung
    still reads exactly as it is on the command line.

    A BODY A SHELL READS IS THE COMMAND'S OWN CODE (task/2973, the ruling:
    "a heredoc fed to a SHELL is code, so parse it"). `bash <<'EOF'`,
    `sh -s <<'EOF'`, `ssh host <<'EOF'` and `cat <<'EOF' | bash` run every
    line of their body, so every row in one counts, the three with no anchor
    included — which is what task/2870 gave up for them, now taken back. The
    body of a program the reader does not know counts the same way when a
    shell, an evaluator or a program word that is a value stands anywhere in
    the command (`cat <<'EOF' | (sh)`, `while read l; do $l; done <<'EOF'`),
    and every body does beside an evaluator (`_cut_data` names them).

    A ROW OF ORDINARY WORDS STANDING ONLY IN ANY OTHER QUOTED BODY IS NOT
    EVIDENCE (task/2870). `run cancel`, `run watch` and `run download` are
    two ENGLISH WORDS each — this rung's own corpus holds `run` in one
    command of every twelve and `cancel` in one of 116 — and the body of a
    program this rung does not know is more often prose than a script. A
    ROW SPLIT BETWEEN THE COMMAND LINE AND SUCH A BODY is dropped too: the
    pieces of one row must stand together in the text the shell runs.

    WHAT THAT GIVES UP, exactly and not vaguely: an ANCHORLESS row — `gh run
    cancel 123`, `gh run watch 123`, `gh run download 123` and no other
    spelling — in a QUOTED body whose program is neither a shell nor a data
    program (`docker exec -i c sh <<'EOF'` is one) passes. Nothing anchored
    passes by this rule, no unquoted heredoc is touched (its body
    substitutes, so its bytes are still read), and nothing outside a heredoc
    body changes at all.

    Measured when it was a rule for every quoted body: over 3,455 distinct
    Bash and Monitor commands it gave back NINE refusals and newly refused
    NONE, every one an anchorless `run cancel` or `run watch` inside the
    quoted heredocs of a command writing a handoff file or a chat post —
    bodies the data rule now cuts before this rule is asked."""
    if not rows:
        return None
    anchored = [found for found in rows if found[4]]
    if anchored:
        return anchored[0]
    executed = _executed_readings(command, code)
    if executed is None:                # no document: the text is all command
        return rows[0]
    for found in rows:
        if _row_stands(found[3], executed):
            return found
    return None


# WHERE THE MATCH LANDED, IN TWENTY CHARACTERS. The refusal's own length is
# pinned (`PreToolUse/refusal-ci-runner` in tests/test_hook_budgets.py), and
# the act description is the only part of it that varies, so a clause here
# is bought out of a budget that is already tight: quoting the folded text
# around the match — the thing that would let a reader SEE what the rung
# saw — costs about sixty characters and does not fit, and raising the
# number is what that pin exists to refuse. This clause says the one thing
# the reader cannot derive and would otherwise get WRONG.
_IN_A_DOCUMENT = " (in a heredoc body)"


def _row_refusal(command, found):
    """The refusal for one row: the spelling, where it stands in the folded
    text, and whether that text is a document the shell will not run.

    THE OFFSET ALONE HAS BEEN DIAGNOSED WRONG, which is measured rather than
    supposed: it counts in the FOLDED command — continuations joined, quotes
    and backslashes deleted, whitespace collapsed, readings joined — and the
    author of the refused command never typed that text. The seat that filed
    task/2870 counted the reported character in its own RAW command, landed
    on the word `trunk`, and reported that this rung had matched `run`
    inside it. It had not, and cannot: every piece carries the edge that
    stops it running into a longer word, which `tests.test_chat_argv_guard`
    now asserts in both directions. The real match was two ordinary words of
    a message body, and the document rule above is what stops that one from
    being a refusal at all.

    The clause is written only when it is TRUE of this match — the row
    stands in the command and not in the part of it a shell runs, which can
    only happen to an ANCHORED row now — so a reader who sees it knows the
    rung read a heredoc body deliberately, and a reader who does not see it
    knows the match is on the command line. That is the residual, stated:
    for a match on the command line the offset is still fold-relative and
    still has to be re-derived by hand."""
    spelling, at, says, pieces, _anchored = found
    executed = _executed_readings(command)
    document = executed is not None \
        and not _row_stands(pieces, executed, window=False)
    return ("%s at character %d of the folded command%s %s"
            % (spelling, at, _IN_A_DOCUMENT if document else "", says))


# ---------------------------------------------------------------------------
# THE SIDECHAIN BEACON RUNG — a subagent may not arm, replace or stop its
# seat's inbox beacon (task/2542), and only the PreToolUse payload can say
# the caller is one (actors.SIDECHAIN_RULE): the beacon process it would
# start inherits the seat's name and session and so passes every identity
# door downstream.
#
# IT ASKS PRESENCE OVER THE SAME FOLDED TEXT, for the same reason the rung
# above does and with one more of its own: this rung's failure mode is a
# DEAF SEAT. It walked the shell once — heredoc openers, delimiter words,
# wrapper scripts, pipeline stages that own a document — and the walk is
# what hid a real arm, twice. A quoted FAKE opener handed the lines below it
# to a document the shell never opens and the excision cut a real
# `helm chat wait --follow` standing among them; a continuation inside a
# literal document destroyed its terminator and swallowed the arm beneath
# it. A guard whose miss costs the seat its wake route does not get to be
# the more precise reader.
#
# THE SPELLING IS THREE PIECES TOGETHER in one folded reading: the word
# `chat`, the word `wait`, and one of the beacon flags. Wherever they stand
# in it, and whatever stands between them — a `--room R` before the verb, a
# path in front of `helm`, `python3 -m helm`, a wrapper's `-c` string, a
# heredoc a shell reads as its script, a continuation through the middle of
# any of it. The words are whole words, so `HELM_CHAT_NAME=s1` is not the
# word `chat`.
#
# WHAT THAT COSTS: a subagent that only writes ABOUT the beacon is refused
# too — a post quoting the arm, a grep for it, and a non-beacon wait that
# happens to share a command with `tail --follow`. The subagent's cure is to
# say it without the flag word or to write it with a tool that is not a
# shell; the seat's main conversation is unaffected, because no payload it
# sends carries an agent_id. A wait with neither flag registers no beacon
# and is never refused, which is the spelling that matters — `--any` room
# reads and one-shot deliveries stay a subagent's to run.
# THE THREE PIECES SPAN THE EXPANSION MARK, the way the Actions pieces do
# and for the same reason one function down: the mark stands where an
# expansion was deleted from INSIDE a word, the text it hides is unknown,
# and a word assembled around one is the word bash runs. These three did
# not span it while the Actions pieces did, and the gap was measured: from
# a subagent, `helm ch$(echo at) wait --follow` folded to
# `helm ch<mark> wait --follow`, held neither `chat` nor a mark-spanning
# piece, and ARMED A REAL BEACON — while the Actions rung refused the
# neighbouring `helm chat wa"$(echo it)" --follow` through its own `watch`.
# One rung reading the mark and its sibling ignoring it is two readers of
# one fold, which is the shape this module keeps paying for.
#
# Their EDGES stay their own (`[\w-]`, no dot), because a beacon word is a
# shell word and `bin/helm.chat wait --follow` must still read as the word
# `chat`; only the span construction is shared.
_BEACON_WORDS = (re.compile(r"(?<![\w-])" + _mark_spanning("chat")
                            + r"(?![\w-])"),
                 re.compile(r"(?<![\w-])" + _mark_spanning("wait")
                            + r"(?![\w-])"))
_BEACON_FLAGS = re.compile("(?:" + _mark_spanning("--follow") + "|"
                           + _mark_spanning("--replace") + r")(?![\w-])")


def sidechain_beacon_presence(command):
    """Does `command`'s text name a beacon arm anywhere in it?

    True when the JOINED readings hold all three pieces — the same union the
    Actions rung counts its rows over, and for the sharper reason: a piece
    set scanned per reading discards a set whose pieces stand in two of
    them, and the miss here costs the seat its wake route. `--follow` and
    `--replace` are whole flags, so `git log --follow-tags` beside the words
    is not an arm.

    Each piece may span the fold's expansion mark, so a word assembled
    around a substitution (`ch$(echo at)`, `wa"${x}"it`, `--fol$(echo low)`)
    is the word it assembles to. A mark may finish a piece begun in the text
    and never start one, so a word supplied WHOLE by a runtime value is
    outside this rung exactly as it is outside its sibling: `helm $V wait
    --follow` passes (measured). A substitution that SPELLS the words is not
    that case and never was — `$(echo helm chat wait) --follow` is refused
    by the plainest reading, which holds the text a substitution's interior
    is made of."""
    readings = _readings(command)
    return bool(_BEACON_FLAGS.search(readings)) and \
        all(word.search(readings) for word in _BEACON_WORDS)


# ---------------------------------------------------------------------------
# THE SIDECHAIN AUTHORITY RUNG (task/3060) — the beacon rung's fact, one
# ledger over, and the ONE delegate rung. A delegate (an Agent-tool subagent
# or a Workflow agent) shares its seat's session and HELM_CHAT_NAME, so a
# verdict it writes is recorded as the seat's own and no field on the row can
# say otherwise. Measured: a delegated reader wrote an immutable APPROVE with
# zero findings that its brief never authorized. The table of refused verbs,
# the grant that admits them, the reason each is on it and the spellings of
# them that write nothing live in helm/delegate_grant.py. task/1388 built a
# second rung from the same incident; its verbs and the spellings it let
# through are folded into that one table and into `_authority_exempt` below.
#
# IT READS WHAT THE SHELL RUNS, NOT WHAT THE COMMAND MENTIONS (the
# integrator's ruling). A delegate writes ABOUT these verbs all day: a grep
# or rg pattern, a pgrep or ps argument, a brief in a heredoc fed to cat or
# tee, an echo or printf argument, a python edit whose string names a verb,
# a commit message, a post. None of those runs the verb, and a census of
# recorded delegate commands found them to be two thirds of what a
# text-reading rung refused. So the fold reads the command LESS THE DATA the
# shell reader proves (`_invocation_text`, the predicate the GitHub-Actions
# and owner-posture rungs already cut with: `_command_data` says which words
# of each program, and which heredoc bodies, are data).
#
# WHY THE COMMAND LESS ITS DATA, AND NOT THE READER'S HELM CALLS ALONE. A verb
# runs through programs the reader does not unwrap: `/usr/bin/time
# ./bin/helm ...`, `$HELM dispatch verdict` and `python3 bin/helm ...` (each
# measured in the census), `xargs helm ...`, `ssh host 'helm ...'`, `find
# -exec helm ...`, and a script the same command writes and then runs. Reading only the invocations the reader
# resolves to helm would pass every one of them. Cutting only what is PROVEN
# data keeps them all: an unknown program's arguments, a body fed to an
# unknown program, and text the reader cannot settle are code, and the verb
# the fold finds there is refused (unknown refuses).
#
# THE SAME FOLD AS THE BEACON RUNG, AND ONE DIFFERENCE IN WHAT IT ASKS. The
# readings and the mark-spanning pieces are the beacon rung's, so a word
# assembled around an expansion (dis$(echo patch), for the group `dispatch`)
# is the word it assembles to. What differs is ORDER: the beacon's three
# pieces may stand anywhere, and a verb pair here must stand as one
# invocation, `helm`, the group, at most three options, the verb. A delegate
# reads the ledger all day (`helm dispatch list | grep verdict`, `helm lr
# show X` beside a `close`), and a rung that refused every command holding
# both words would refuse the reads the delegate exists to do.
#
# WHAT IT STILL COSTS: a mention the reader cannot prove is data is refused
# like a run. That covers args to a program the predicate does not know
# (awk, node -e, a for-loop's word list), anything piped into a stage that
# could run its input (python, sed), a heredoc fed to a program given as a
# value (`$H dispatch send <<EOF`), an unquoted heredoc (its substitutions
# run), and python text that starts a process. The cure is the same words
# without `helm`, or a file written with a tool that is not a shell. A word
# supplied WHOLE by a runtime value (`$H dispatch verdict`) is outside this
# rung, exactly as it is outside the beacon rung.
_AUTHORITY_OPTION = r"(?: +--?[\w.-]+(?:=\S*)?(?: +(?!-)\S+)?){0,3}"
_AUTHORITY_HELM = r"(?<![\w-])" + _mark_spanning("helm") + r"(?![\w-])"
# THE CHEAP GATE: a delegate command that never names `helm` pays for no
# import and no table, which is almost every command a delegate runs.
_AUTHORITY_GATE = re.compile(_AUTHORITY_HELM)
_AUTHORITY_PATTERNS = []
_HOME_ASSIGNMENT = "HELM_HOME="


def _authority_patterns():
    """[(pattern, "group verb")] for the refused table, compiled once."""
    if not _AUTHORITY_PATTERNS:
        from . import delegate_grant
        helm = _AUTHORITY_HELM
        for group, verb in delegate_grant.REFUSED:
            _AUTHORITY_PATTERNS.append((re.compile(
                helm + " +" + _mark_spanning(group) + r"(?![\w-])"
                + _AUTHORITY_OPTION + " +" + _mark_spanning(verb)
                + r"(?![\w-])"), "%s %s" % (group, verb)))
    return _AUTHORITY_PATTERNS


# WHAT WRITES NOTHING, READ PER INVOCATION. A refused verb may be spelled so
# that it writes nothing (delegate_grant.writes_nothing: its usage, -h and
# --help, an lr --dry-run, `lr expired` and `lr retire --off-frontier`
# without --apply), or pointed at a TEST home with HELM_HOME, and a delegate
# runs all of these (the census of recorded commands). The fold deleted what
# says which: quote marks, word boundaries and newlines, so `--reason "not a
# --dry-run"` and `close x` on one line with `--dry-run` on the next both
# fold to text holding the flag. So the arguments are read from the SHELL
# READER's words for each invocation (`_commands_run`), and a verb is let
# through only when the reader reads as many helm invocations of it as the
# fold found in its busiest reading, and every one of them writes nothing.
# A spelling the reader does not see (a word assembled around an expansion,
# an option between the group and the verb, a quoted mention) leaves a text
# match unaccounted for, and the verb is refused; so is every verb in text
# the reader cannot settle. The reader only ever LETS THROUGH what the fold
# found: it never adds a refusal.
#
# A TEST HOME is a HELM_HOME assignment in the invocation's OWN prefix
# (`HELM_HOME=v helm ...`, `env HELM_HOME=v helm ...`) that names a LITERAL
# path: `~` and a leading `$HOME` expand as bash expands them, a relative
# path joins the directory the invocation runs in, and the result must
# resolve (realpath) to neither the home this process's helm resolves
# (home.helm_home) nor the default one (home.default_home). A value holding
# another variable or a substitution, no value, and a relative path in a
# directory the text does not settle are not test homes.
#
# OWN PREFIX ONLY, BECAUSE THE READER DOES NOT MODEL SHELL STATE. A value set
# by an earlier statement (`export HELM_HOME=v; helm ...`) reaches the
# invocation only if nothing between them ended or undid it, and the reader
# cannot say: it flattens a subshell `( ... )`, a pipeline element and a
# background job, which each drop the export; it reads what `bash -c` runs
# without knowing that the child's export dies with it; and `unset`, `export
# -n` and every env spelling that clears a name undo it. Each of those was
# measured exempting a write to the LIVE ledger, one instance at a time
# (a reviewer's fold read, then the coordinator's), so no export counts at
# all: a delegate that means a test home spells it on the call. For the same
# reason, in the prefix any option word after an `env` word (`-`, `-i`, `-u
# X`, `-uX`, `--unset=X`, `-C`, `-S`, a cluster, a word the text does not
# settle) voids the assignment beside it, and so does `sudo`: refusing `env
# -u OTHER HELM_HOME=v helm ...` is the accepted cost.


def _named_home(raw, here):
    """The helm home the value `raw` of a HELM_HOME assignment names, as
    helm resolves it, or None where the text does not settle it."""
    m = _HOME_WORD.match(raw)
    if m:
        rest = raw[m.end():]
        if "$" in rest or "`" in rest or rest.count('"') > 1:
            return None
        path = os.path.expanduser("~") + rest.replace('"', "")
    else:
        if "$" in raw or "`" in raw:
            return None
        try:
            parts = shlex.split(raw)
        except ValueError:
            return None
        if len(parts) != 1 or not parts[0]:
            return None
        path = os.path.expanduser(parts[0])
    if not os.path.isabs(path):
        if here is None:
            return None
        path = os.path.join(here, path)
    return path


def _test_home(raw, here):
    """Whether HELM_HOME value `raw`, for an invocation run in `here`, names
    a home that is not the live one."""
    path = None if raw is None else _named_home(raw, here)
    if path is None:
        return False
    live = {os.path.realpath(home.helm_home()),
            os.path.realpath(home.default_home())}
    return os.path.realpath(path) not in live


def _helm_argv(name, words, k):
    """The argument values of a helm invocation (the words after `helm`),
    or None when the command at words[k] is not helm."""
    vals = [w.value() for w in words[k + 1:]]
    if name and _PYTHON.fullmatch(name) and vals[:2] == ["-m", "helm"]:
        return vals[2:]
    if name and name.rsplit("/", 1)[-1] == "helm":
        return vals
    return None


def _own_home(prefix):
    """The value of the HELM_HOME assignment in an invocation's own prefix
    words, or None: none there, or voided by `sudo` or by any option word
    after an `env` word (see above)."""
    if any(w.value() == "sudo" for w in prefix):
        return None
    for i, w in enumerate(prefix):
        if w.value() == "env" and any(
                (v.value() or "-").startswith("-") for v in prefix[i + 1:]):
            return None
    own = [w.raw[len(_HOME_ASSIGNMENT):] for w in prefix
           if w.raw.startswith(_HOME_ASSIGNMENT)]
    return own[-1] if own else None


#: xargs/parallel options that take a VALUE in the next word (a superset of
#: both tools': skip the value so it is not read as the invoked command)
_FED_VALUE_OPTS = frozenset((
    "-a", "--arg-file", "-E", "-I", "--replace", "-L", "--max-lines",
    "-n", "--max-args", "-P", "--max-procs", "-s", "--max-chars",
    "-d", "--delimiter", "--process-slot-var", "-j", "--jobs", "-N",
    "--colsep", "-S", "--sshlogin", "--slf", "--sshloginfile", "--tmpdir",
    "--joblog", "--results", "--retries", "--timeout"))
#: their value-less flags
_FED_FLAG_OPTS = frozenset((
    "-0", "--null", "-p", "--interactive", "-r", "--no-run-if-empty",
    "-t", "--verbose", "-x", "--exit", "-o", "--open-tty", "--show-limits",
    "-i", "-l", "-e", "-k", "--keep-order", "-u", "--ungroup", "-m", "-X",
    "--will-cite", "-c", "--gnu"))
#: wrappers that RUN the command after their own options; helm reached
#: through one is still invoked (`xargs sudo helm`), so an unpeeled wrapper
#: is the unknown. `env` is peeled below because it is the common one and a
#: grep it wraps (`xargs env grep helm`) is a read.
_FED_WRAPPERS = frozenset(("sudo", "doas", "nohup", "setsid", "nice",
                           "ionice", "stdbuf", "time", "timeout", "chrt",
                           "taskset"))
#: env options that take a value in the next word
_ENV_VALUE_OPTS = frozenset(("-u", "--unset", "-C", "--chdir", "-P",
                             "-S", "--split-string"))


def _peel_env(ws, i):
    """The index in `ws` of the command `env` runs, past its assignments and
    its own options, or None where the words do not settle it."""
    while i < len(ws):
        w = ws[i].value()
        if w is None:
            return None
        if w == "--":
            return i + 1
        if re.match(r"[A-Za-z_][A-Za-z_0-9]*=", w):
            i += 1                          # NAME=VALUE assignment
            continue
        if not w.startswith("-"):
            return i                        # the command word
        if "=" in w:                        # --unset=NAME
            i += 1
            continue
        if len(w) > 2 and w[1] != "-" and ("-" + w[1]) in _ENV_VALUE_OPTS:
            i += 1                          # -uNAME: value attached
            continue
        if w in _ENV_VALUE_OPTS:            # -u NAME: value in the next word
            i += 2
            continue
        i += 1                              # a flag (-i, -0, -v) or unknown
    return None


def _fed_command_word(ws):
    """The basename of the command xargs/parallel invokes, or None when the
    option list cannot be settled — an unknown option that might take a value,
    an unpeeled wrapper, a word the reader cannot read, or a `parallel` arg
    separator (task/3071 cure). "" where the tool runs its default (only
    options, no command). Only the INVOKED command is the runtime feed:
    `xargs grep -n helm` runs grep and hands it helm as a pattern, which is a
    read; `xargs env helm`, `xargs sudo helm` and `xargs python3 -m helm` run
    helm and are the feed."""
    i = 0
    while i < len(ws):
        w = ws[i].value()
        if w is None:
            return None                     # a substitution/unsettleable word
        if w in (":::", "::::", ":::+", "::::+"):
            return None                     # parallel arg separators
        if not w.startswith("-") or w == "-":
            base = w.rsplit("/", 1)[-1]
            if _PYTHON.fullmatch(base):
                # python -m helm runs helm; python script.py does not
                rest = [ws[j].value() for j in range(i + 1, len(ws))]
                if "-m" in rest:
                    nxt = rest[rest.index("-m") + 1:rest.index("-m") + 2]
                    if nxt and nxt[0] and nxt[0].rsplit("/", 1)[-1] == "helm":
                        return "helm"
                return base
            if base == "env":
                nxt = _peel_env(ws, i + 1)
                if nxt is None:
                    return None
                i = nxt
                continue
            if base in _FED_WRAPPERS:
                return None                 # a wrapper we do not peel
            return base
        if "=" in w:                        # --opt=value: attached
            i += 1
            continue
        if w in _FED_FLAG_OPTS:
            i += 1
            continue
        if w in _FED_VALUE_OPTS:            # value in the next word
            i += 2
            continue
        if len(w) > 2 and w[1] != "-" and ("-" + w[1]) in _FED_VALUE_OPTS:
            i += 1                          # short option with attached value
            continue
        if len(w) >= 2 and w[1] != "-" \
                and all(("-" + c) in _FED_FLAG_OPTS for c in w[1:]):
            i += 1                          # a cluster of known flags
            continue
        return None                         # unknown option → cannot settle
    return ""                               # no command word: the default


def _runtime_fed_helm(command):
    """Whether `command` hands `helm` its arguments at RUNTIME: xargs or
    parallel INVOKING `helm` (its verb then arriving from stdin), or
    eval/source of text the reader cannot settle that the fold saw the name
    in (task/3071, the row owner's ruling: the reader sees helm but not its
    verb, and unknown refuses). The verb then comes from stdin or a
    substitution, and no spelling rule can see it. Only the invoked command
    counts: helm handed to another program as data (`xargs grep -n helm`) is
    a read; an option list the reader cannot settle is the unknown too."""
    try:
        calls = _commands_run((command or "").replace("\\\n", ""), None)
    except Exception:
        return False
    for name, words, k in ((c[0], c[1], c[2]) for c in calls):
        if name in ("xargs", "parallel"):
            word = _fed_command_word(words[k + 1:])
            if word is None or word == "helm":
                return True
        if name in ("eval",):
            # eval of a COMMAND SUBSTITUTION or variable whose raw text names
            # helm: the helm the fold saw may live inside the text eval runs.
            # A $'...' ANSI-C word is statically readable and its content has
            # already met the fold — only a substitution ($(...) or `...`),
            # where the text is genuinely unknowable, is the unknown.
            for w in words[k + 1:]:
                if w.value() is None and "helm" in w.raw \
                        and ("$(" in w.raw or "`" in w.raw):
                    return True
    return False


def _authority_exempt(command, cwd, hits, readings):
    """The verbs of `hits` [(pattern, "group verb")] that `command` runs only
    in spellings that write nothing (see above), as a set."""
    from . import delegate_grant
    try:
        calls = _commands_run((command or "").replace("\\\n", ""),
                              cwd if isinstance(cwd, str)
                              and os.path.isabs(cwd) else None)
    except Exception:          # _Unsettled, or the reader's defect
        return set()
    runs = {}
    for name, words, k, at in calls:
        vals = _helm_argv(name, words, k)
        if not vals or len(vals) < 2 \
                or (vals[0], vals[1]) not in delegate_grant.REFUSED:
            continue
        raw = _own_home(words[:k])
        quiet = delegate_grant.writes_nothing(vals[0], vals[1], vals[2:]) \
            or _test_home(raw, at)
        seen = runs.setdefault("%s %s" % (vals[0], vals[1]), [0, True])
        seen[0] += 1
        seen[1] = seen[1] and quiet
    texts = readings.split(_READING_BREAK)
    out = set()
    for pattern, verb in hits:
        count, quiet = runs.get(verb, (0, False))
        if quiet and count >= max(len(pattern.findall(t)) for t in texts):
            out.add(verb)
    return out


def _runs_what_it_wrote(command):
    """Whether `command` runs a file it writes itself: a file an output
    redirect or `tee` writes that is also a program word, or a script handed
    to a shell, node, perl or ruby. What the data cut took from such a
    command may be that script's text, so none of it is data. A script
    handed to python counts only where the command names a way to start a
    process (`_SPAWNS`), the rule python's own text is read by. Text the
    reader cannot settle is answered True."""
    try:
        tokens = _ShellReader(command).read()
    except Exception:          # _Unsettled, or the reader's defect
        return True
    commands = _simple_commands(tokens)
    written = {tok[2].raw for tok in tokens if tok[0] == "r" and ">" in tok[1]
               and not tok[2].raw.isdigit()
               and not tok[2].raw.startswith("/dev/")}
    for cmd in commands:
        name, k = _program(cmd.words)
        if name == "tee":
            written.update(w.raw for w in cmd.words[k + 1:]
                           if not (w.value() or "-").startswith("-"))
    if not written:
        return False
    bases = {posixpath.basename(w.strip("'\"")) for w in written}
    # python's own text is read by the allowlist now (task/3071): a written
    # script fed to python counts as code when its TEXT is not inert — the
    # denylist this replaces read `import os as o` beside a written spawn as
    # data and the verb in it never reached the fold.
    import re as _re
    py_texts = [m.group(2) for m in
                _re.finditer(r"(?ms)<<'?(\w+)'?\n(.*?)\n\1", command)]
    python_is_code = bool(_SPAWNS.search(command)) or any(
        not python_text_inert(t) for t in py_texts)
    for cmd in commands:
        name, k = _program(cmd.words)
        if k is None:
            continue
        base = (name or "").rsplit("/", 1)[-1]
        runs = [cmd.words[k]]
        if base in _STDIN_SHELLS or base in ("node", "perl", "ruby") or (
                _PYTHON.fullmatch(base) and python_is_code):
            runs += cmd.words[k + 1:]
        if any(w.raw in written
               or posixpath.basename(w.raw.strip("'\"")) in bases
               for w in runs):
            return True
    return False


def _argv_sequence_verbs(elts):
    """The refused verbs a SEQUENCE of ast elements spells as an argv, or [].

    ONE RULE for every way python spells an argv as separate words (task/3071,
    the row owner's ruling): the elements of a list or tuple
    (`subprocess.run(["helm","dispatch","verdict",row,tip,"--approve"])`) and
    the positional args of a call (`os.execlp("helm","helm","lr","close",x)`,
    `os.execvp("helm",[...])`, `asyncio.create_subprocess_exec("helm",...)`).
    A constant `helm` word — `helm`, a string ending in `/helm`, or `helm`
    after a constant `-m` — with the constant group and verb after it names
    the invocation, and no text pattern sees it: the words are comma-joined,
    not shell-spaced. Elements BEFORE the helm word may be anything (a
    variable python path, `sys.executable`, an exec mode); elements AFTER the
    verb may be variables (the row, the tip). Only the group and the verb
    must be spelled constants — which is exactly what the table asks about.

    THE READ REUSES THE TEXT TABLE, not a second rule for the option window.
    (An `import ast` is local, paid only by python calls.)
    Each maximal RUN of single-token string constants (a value with a space
    is one arg, not two, so it never joins a run — a whole `helm dispatch
    verdict` STRING handed to grep stays one word and spells nothing) is
    joined with spaces and the refused-verb patterns are searched over it,
    so `helm dispatch --json verdict` matches through the same option window
    as a shell invocation, and `helm` beside a variable group or verb does
    not."""
    import ast                              # local: paid only by python calls
    out = []
    run = []
    runs = []
    for e in elts:
        if isinstance(e, ast.Constant) and isinstance(e.value, str) \
                and " " not in e.value and "\t" not in e.value:
            run.append(e.value)
        else:
            if run:
                runs.append(run)
            run = []
    if run:
        runs.append(run)
    for run in runs:
        text = " ".join(run)
        for pattern, verb in _authority_patterns():
            if pattern.search(text):
                out.append(verb)
    return out


def _python_argv_verbs(command):
    """The refused verbs ("group verb") a python text in `command` spells as
    an argv of separate words (`_argv_sequence_verbs`, task/3071): a list or
    tuple, or the positional args of a call. The walk is ast over each python
    text the command holds — a `-c` word, a heredoc body fed to python, and
    the body of a python script the same command WRITES then RUNS (task/3071:
    `cat > s.py <<'EOF' … EOF; python3 s.py`, the body `_runs_what_it_wrote`
    proves is run). Each invocation found is judged by the same table and
    joins the hits, so the grant door below admits it exactly as a spelled
    invocation would be admitted. The writes-nothing exemptions do NOT reach
    it — a `--dry-run` or `--help` SPELLED INSIDE A LIST is refused, and the
    cure is to spell that read in the shell, where the exemption is read."""
    import ast                              # local: paid only by python calls

    texts = []

    try:
        tokens = _ShellReader(command or "").read()
    except Exception:
        return ()
    for cmd in _simple_commands(tokens):
        name, k = _program(cmd.words)
        if not name or not _PYTHON.fullmatch(name.rsplit("/", 1)[-1]):
            continue
        args = cmd.words[k + 1:]
        j = 0
        while j < len(args):
            v = args[j].value()
            if v is None or v == "-m" or not v.startswith("-") or v == "-":
                break
            if re.fullmatch(r"-[A-Za-z]*c", v) and j + 1 < len(args):
                if args[j + 1].value():
                    texts.append(args[j + 1].value())
                break
            j += 2 if v in ("-W", "-X") else 1
    # heredoc bodies fed to a python command, and — when the command writes a
    # python script and then runs it — every heredoc body it holds (the
    # written script is fed to `cat`, not python, so `cmd.docs` misses it;
    # `_runs_what_it_wrote` proves a written file is run). An inert or
    # non-python body is dropped by the loop below.
    try:
        reader = _ShellReader(command or "")
        reader.read()
        bodies = {at: "\n".join(reader.lines[start:end])
                  for at, _q, start, end in reader.bodies}
        for cmd in _simple_commands(tokens):
            name, k = _program(cmd.words)
            if name and _PYTHON.fullmatch(name.rsplit("/", 1)[-1]):
                for at in cmd.docs:
                    if at in bodies:
                        texts.append(bodies[at])
        if _runs_what_it_wrote(command or ""):
            texts.extend(bodies.values())
    except Exception:
        pass

    out = []
    for src in texts:
        # an INERT text is data the cut removed; judging its contents is the
        # write-about refusal the data cut exists to lift
        if python_text_inert(src):
            continue
        try:
            tree = ast.parse(src)
        except (SyntaxError, ValueError, RecursionError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.List, ast.Tuple)):
                out.extend(_argv_sequence_verbs(node.elts))
            elif isinstance(node, ast.Call):
                out.extend(_argv_sequence_verbs(node.args))
    return tuple(dict.fromkeys(out))


def _authority_text(command):
    """`command` less the data the shell reader proves (see above). Python's
    text is code when the command anywhere names a way to start a process or
    holds python text that is not inert under the allowlist (task/3071);
    a command that runs a file it writes keeps all its text."""
    interpreters = _PYTHON if _SPAWNS.search(command or "") else None
    text, _code = _invocation_text(command or "", interpreters)
    if text != command and _runs_what_it_wrote(command or ""):
        return command
    return text


def sidechain_authority_verbs(command, cwd=None):
    """Every refused verb ("group verb") `command` RUNS, in the table's order,
    less those it runs only in spellings that write nothing; () when it runs
    none. The fold reads the command less its data (`_authority_text`), and
    only once its whole text has named a refused verb, so a command that
    names none pays for no cut. `cwd` is the payload's, for a relative
    HELM_HOME."""
    readings = _readings(command)
    if not _AUTHORITY_GATE.search(readings):
        return ()
    # a python argv LIST holds its words comma-joined, which no text pattern
    # matches; the ast walk answers it and pays only where python is spelled
    extra = _python_argv_verbs(command) \
        if _PYTHON.search(readings) else ()
    runtime_unknown = not extra and _runtime_fed_helm(command)
    if not any(pattern.search(readings)
               for pattern, _verb in _authority_patterns()) and not extra \
            and not runtime_unknown:
        return ()
    readings = _readings(_authority_text(command))
    hits = [(pattern, verb) for pattern, verb in _authority_patterns()
            if pattern.search(readings)]
    # the LIST walk's verbs join the hits and pass through the same grant
    # door and exemptions below
    extra = [verb for verb in extra if verb not in {v for _p, v in hits}]
    # RUNTIME-SUPPLIED ARGS (task/3071, the owner's ruling): the command
    # named helm but its verb arrives at runtime — xargs/parallel feeding a
    # helm word, eval of unsettleable text. Unknown refuses, and the refusal
    # says UNKNOWN rather than naming a verb nobody saw.
    if runtime_unknown and not extra and not hits:
        extra = ["helm (runtime-supplied arguments)"]
    if not hits and not extra:
        return ()
    if extra == ["helm (runtime-supplied arguments)"]:
        return tuple(extra)
    exempt = _authority_exempt(command, cwd, hits, readings)
    return tuple(verb for _pattern, verb in hits if verb not in exempt) \
        + tuple(verb for verb in extra if verb not in exempt)


def sidechain_authority_presence(command, cwd=None):
    """The first refused verb `command` runs, or None — the one question the
    rung asks of a delegate's command before it asks the grant."""
    verbs = sidechain_authority_verbs(command, cwd)
    return verbs[0] if verbs else None


def _sidechain_authority(d, cmd):
    """(refusal-or-None, admitted lines) for a DELEGATE's Bash or Monitor
    command. Every refused verb the command runs must be admitted by a live
    grant on the payload's session, or the call is refused naming the first
    that is not; each admitted verb says which grant admitted it."""
    verbs = sidechain_authority_verbs(cmd, d.get("cwd"))
    if not verbs:
        return None, []
    from . import actors, delegate_grant
    # RUNTIME-SUPPLIED ARGS: no grant can name a verb nobody saw, so the
    # unknown is refused outright, in its own words.
    if verbs[0] == "helm (runtime-supplied arguments)":
        return ("[helm argv-guard] BLOCKED: %s"
                % actors.sidechain_authority_refusal(
                    "(runtime-supplied arguments: the command runs helm with "
                    "words the reader cannot see — xargs/parallel feeding it, "
                    "or eval of unsettleable text — and unknown refuses. "
                    "Spell the invocation, or report to your parent)",
                    False)), []
    admitted = []
    for verb in verbs:
        grant = delegate_grant.admits(d.get("session_id"), verb)
        if grant is None:
            grantable = tuple(verb.split()) in delegate_grant.GRANTABLE
            return ("[helm argv-guard] BLOCKED: %s"
                    % actors.sidechain_authority_refusal(verb, grantable)), []
        admitted.append((
            "delegate-grant-%s" % grant["id"],
            "[helm argv-guard] admitted under grant %s: `%s` from a "
            "delegate, by this seat's own grant"
            % (grant["id"], "helm " + verb)))
    return None, admitted


# TWO ROUTES, BECAUSE THE LONG FORM LIVES IN TWO PLACES. The premise carries the
# owner's rule in his words and nothing else; the folding semantics that decide
# where the character index counts, and the full allow list (a single spelling
# passes; an anchor beside an unresolved expansion does not), are in the hooks
# doc. A pointer that RESOLVES and does not ANSWER is a route to nothing with a
# green check on it, so each half of the refusal's promise names the place that
# keeps it.
GITHUB_ACTIONS_PREMISE = "prior:ci-runs-on-the-local-fabric-never-github-actions"
GITHUB_ACTIONS_DOC = "docs/HOOKS.md, the argv-guard row"


def github_actions_message(act, tool="Bash"):
    """The refusal: what was found, the rule in one sentence, the one next act.

    For a Bash command `act` names the SPELLING that was found and the
    character it stands at, because this rung reads text and a reader who
    cannot see what offended cannot fix it; for a Write or Edit it names the
    file.

    THE FOLDING SEMANTICS AND THE ALLOW LIST ARE NOT HERE, deliberately: that
    is rule prose, it is the premise's job, and restating it identically on
    every fire costs a kilobyte to say what one command says on demand. What
    belongs here is the part a reader cannot reconstruct — which spelling
    offended, where it stands, and the exact word that sends the act through.
    This function writes words and decides nothing."""
    if tool == "Bash":
        cure = ("Deliberate? Put %s first in the command; nowhere else "
                "counts, nor does the environment."
                % GITHUB_ACTIONS_OVERRIDE)
    else:
        act = "the %s tool writes %s" % (tool, act)
        cure = ("%s cannot carry the override word: write the file through "
                "Bash (`%s tee <path> <<'EOF'`)."
                % (tool, GITHUB_ACTIONS_OVERRIDE))
    return ("[helm argv-guard] BLOCKED: %s. CI runs on the local fabric "
            "(`fab test --repo <tree> -- <gate>`), never a GitHub runner. "
            "%s Why: helm store get %s. What passes: %s"
            % (act, cure, GITHUB_ACTIONS_PREMISE, GITHUB_ACTIONS_DOC))


# THE AGENT-MODEL RUNG. The Agent tool's `model` argument does not change the
# model a subagent runs on: on every seat the subagent runs as the launching
# seat's model, whatever the flag says (owner ruling; the store keeps it under
# the id below). A flag the harness ignores in silence is worse than a
# refusal, because the caller then routes, budgets and reports as if another
# model did the work, and nothing ever tells it otherwise. So the call is
# refused and the refusal names the three doors that do reach the seat's own
# model or another one, in the order a reader should try them.
AGENT_MODEL_PREMISE = "heuristic:fable-via-workflow-not-agent-tool"
# The flag's value is echoed so a reader sees what was refused. It is cut at
# this many characters, and every character outside a model name's alphabet is
# shown as `?`, so each character of input renders as exactly one character of
# output and no value can carry the line past its budget
# (tests/test_hook_budgets.py draws the line at the cut).
AGENT_MODEL_ECHO = 32
_AGENT_MODEL_UNSHOWN = re.compile(r"[^A-Za-z0-9._:\[\]-]")


def agent_model_refusal(tool_input):
    """The flag's value when an Agent call names a model, else None.

    Only a non-empty STRING names one. No key, an empty string and a null
    each mean "the seat's own model", which is what the subagent gets anyway,
    so they are admitted unchanged. A value of any other type is malformed
    input and is admitted too, which is how this handler treats every payload
    it cannot read: a guard in front of a whole tool must fail open."""
    model = tool_input.get("model")
    return model if isinstance(model, str) and model else None


def agent_model_message(model):
    """The refusal: what was refused, the fact, then the three doors in
    order. This function writes words and decides nothing."""
    return ("[helm argv-guard] BLOCKED: Agent called with model='%s'. "
            "Subagents run as the launching seat's model whatever the flag "
            "says. (1) Drop the flag. (2) Another model: the Workflow tool, "
            "agent(prompt, {model}). (3) Or a seat launched on it: "
            "`helm launch --seat S --model M`. Why: helm store get %s"
            % (_AGENT_MODEL_UNSHOWN.sub("?", model[:AGENT_MODEL_ECHO]),
               AGENT_MODEL_PREMISE))


# ---------------------------------------------------------------------------
# THE OWNER'S WEB DOOR IS NOT AN AGENT'S (task/2997). helm web's decision
# POSTs record the OWNER's verdict and comments, and the bearer that guards
# them is CSRF protection: every agent runs as the owner's uid, so it can GET
# the page, read the bearer and POST as him. This rung refuses the accidental
# route — an agent shell command whose text sends to the local decision
# endpoints with an HTTP client. It reads the text only (quote marks and
# backslashes dropped, casefolded), matches ANY loopback port because a seat
# can start its own server, and lets a plain read of the queue through; a
# lone curl call that names the door is judged by its own words, not by the
# commands beside it (task/3027, `_door_curl_argv`). A
# same-uid agent that deliberately goes around it (a script file) is outside
# the threat model; ownerasks' module docstring says so.
_DOOR_CLIENT = re.compile(
    r"\b(?:curl|wget|urllib|urlopen|requests|http\.client|httpx|aiohttp|"
    r"httpie|xh|socat|nc|ncat|netcat)\b|\bhttps?\s+(?:post|put|patch)\b|"
    r"\bopenssl\s+s_client\b|/dev/tcp/|"
    r"\b(?:node|bun)\s+(?:-e|--eval)\b.*\bfetch\s*\(|"
    r"\bdeno\s+eval\b.*\bfetch\s*\(|"
    r"\bruby\s+-e\b.*\bnet::http\b|"
    r"\bperl\s+-e\b.*\b(?:lwp(?:::useragent)?|http::tiny)\b|"
    r"\bphp\s+-r\b.*\b(?:file_get_contents|curl_exec|fopen)\b")
_DOOR_LOOPBACK = re.compile(
    r"\b127\.\d{1,3}\.\d{1,3}\.\d{1,3}\b|\blocalhost\b|\[::1\]|\b0\.0\.0\.0\b")
_DOOR_WRITE_PATH = re.compile(r"/api/decisions/\w+|/api/decide\w*")
# The queue path, and the owner's away card (task/3018): a GET of either is a
# read any seat may make, a body or a writing method is his door.
_DOOR_QUEUE_PATH = re.compile(r"/api/decisions\b|/api/owner/posture\b")
# The text is casefolded before this reads it, so curl's `-T` (--upload-file,
# whose long form was already here) is `-t`, and the glued `-XPOST` is
# `-xpost`, which `\bpost\b` cannot see. The away card is READ and WRITTEN on
# one path, so on that door a missed write spelling is the whole rung.
_DOOR_SENDS = re.compile(
    r"\b(?:post|put|patch)\b|(?:^|\s)-x(?:post|put|patch)\b|(?:^|\s)-d|"
    r"(?:^|\s)-t(?=\s|$)|--data|--json|--form|--post-|"
    r"--body-|--upload-file|\bdata\s*=|\bjson\s*=")
_DOOR_CURL_GET = re.compile(
    r"^\s*(?i:curl)\b[^\n]*(?:\s)(?:-G|--get)(?=\s|$)")
# curl's own flags, read on the UNFOLDED text: the casefold makes a form body
# (-F) curl's --fail (-f), and a short-option cluster (-sd, -sF, -fsST,
# -sXPOST) hides a body or a method from the word-start spellings above.
_DOOR_CURL_WRITE_METHOD = re.compile(
    r"(?:^|\s)(?:-[A-Za-z0-9#:]*X(?:\s+|=)?|--request(?:\s+|=))"
    r"(?:POST|PUT|PATCH)(?=\s|$)", re.I)
_DOOR_CURL_BODY = re.compile(r"(?i:\bcurl\b)[^;|&\n]*?\s-[A-Za-z0-9#:]*[dFT]")
# a config curl reads (-K, --config) can name a method or a body from text
# outside its own words, as can a curlrc another command writes
_DOOR_CURL_CONFIG = re.compile(r"(?:^|\s)(?:-[A-Za-z0-9#:]*K|--config\b)")


def _door_curl_argv(command):
    """The argv of the command's one top-level curl call, as one line, when
    that call's own words settle how it sends; else None.

    THE EVIDENCE OF A SEND IS READ WHERE THE CLIENT RUNS (task/3027). Read
    over the whole command, a GET of the away card beside any later command
    whose text said "patch." was refused as a write. The shell reader the
    data fold uses (`_ShellReader`) finds the call, so a quoted `;` or `|`
    inside it splits nothing. A word the text does not settle ($VAR, $(…),
    `…`) can carry another command's text into the call, and so can a
    config, so either is None and the whole command is read, as is a curl
    only inside a substitution and a command the reader cannot settle."""
    try:
        calls = [c for c in _simple_commands(_ShellReader(command).read())
                 if (_program(c.words)[0] or "").rsplit("/", 1)[-1] == "curl"]
        values = [w.value() for w in calls[0].words] if len(calls) == 1 \
            else [None]
    except Exception:           # the reader's defect must not open the rung
        return None
    if None in values:
        return None
    # the words are settled, so a separator left in one is its data
    argv = re.sub(r"[;|&\n]", " ", " ".join(values))
    return None if _DOOR_CURL_CONFIG.search(argv) else argv


def owner_door_post_refusal(command):
    """The decision path an agent command sends to, or None when it sends
    to none. A POST-only subpath (/api/decisions/verdict and its siblings)
    is refused on sight; the queue path itself only when the text also
    sends a body or names a writing method, so a GET of the queue passes.
    When the command's ONE client is a curl call that names the door in its
    own words (`_door_curl_argv`), the body and the method are read in that
    call alone; every other command is read whole."""
    raw = re.sub(r"['\"\\]", "", (command or "").replace("\\\n", ""))
    text = raw.casefold()
    if not (_DOOR_CLIENT.search(text) and _DOOR_LOOPBACK.search(text)):
        return None
    hit = _DOOR_WRITE_PATH.search(text)
    if hit:
        return hit.group(0)
    hit = _DOOR_QUEUE_PATH.search(text)
    if not hit:
        return None
    # A GET-only curl is exempt only when it is the command's ONE client: a
    # leading `curl -G` must not vouch for a second client later in the
    # same command that POSTs to the queue, and a second client's text is
    # never another call's to leave unread.
    one = len(_DOOR_CLIENT.findall(text)) == 1
    argv = _door_curl_argv(command) if one and "curlrc" not in text else None
    if argv is not None and _DOOR_LOOPBACK.search(argv.casefold()) \
            and _DOOR_QUEUE_PATH.search(argv.casefold()):
        raw, text = argv, argv.casefold()
        hit = _DOOR_QUEUE_PATH.search(text)
    if not (_DOOR_SENDS.search(text) or _DOOR_CURL_BODY.search(raw)
            or _DOOR_CURL_WRITE_METHOD.search(raw)):
        return None
    if one and _DOOR_CURL_GET.search(raw) \
            and not _DOOR_CURL_WRITE_METHOD.search(raw):
        return None
    return hit.group(0)


def owner_door_post_message(path):
    """The refusal: what was refused, whose door it is, and what a seat does
    instead. Writes words and decides nothing."""
    from .ownerasks import OWNER_DOORS
    if "/posture" in path:
        return ("[helm argv-guard] BLOCKED: this command sends to helm web's "
                "owner away card (%s). That door records the OWNER's away "
                "posture and his fleet notice, which every seat is shown as "
                "his word, and an agent never writes there; %s. A seat that "
                "needs him posts in helm chat, and `helm away status` reads "
                "the card." % (path, OWNER_DOORS))
    return ("[helm argv-guard] BLOCKED: this command sends to helm web's owner "
            "decision door (%s). That door records the OWNER's verdict and "
            "comments, and an agent never writes there; %s. A seat adds to a "
            "card with `helm decide comment <id> <text>`, which records the "
            "seat." % (path, OWNER_DOORS))



# THE OWNER'S POSTURE IS HIS WORD, AND A SEAT NEVER FORGES IT (task/3018). The
# away flag and the fleet notice reach every seat's context as the owner's own
# word, and the writers accept him only through an ownerasks.OwnerDoor that
# his web card mints. Every agent runs as his uid, so two accidental routes
# stay open that the owner-door rung above cannot see, because neither sends
# anything over HTTP: a one-liner that mints the door itself, and a redirect
# (or tee, cp, mv, install, ln, touch, a python write) onto either file. This
# rung refuses both in the command text, and a Write or Edit whose path is
# either file. It stops the ACCIDENT; a deliberate process (a script file run
# later) is outside the threat model, as ownerasks and ownernotice state.
#
# The file names are literals so this hook imports nothing; an arm pins them
# to away.MARKER_NAME and ownernotice.NOTICE_NAME.
_POSTURE_NAMES = ("owner-away", "owner-notice")
_POSTURE_MINT = re.compile(
    r"\bowner_door\s*\(|\bownerasks\s*\.\s*owner_door\b|\bOwnerDoor\s*\(|"
    r"\b_DOOR_MINT\b")
# The directory may carry a quote ("$HELM_CHAT_DIR"/x, '/dir/'x) and the name
# may open with one (/dir/'x'): the same file, so the class admits them.
_P_NAME = (r"(?:[^\s;|&<>()]*/)?['\"]?(owner-away|owner-notice)"
           r"(?=['\"\s;|&)]|$)")
_POSTURE_WRITES = tuple(re.compile(x, re.M) for x in (
    # a redirect: > >> >| 2> &> onto the file
    r"[0-9&]?>>?\|?\s*['\"]?" + _P_NAME,
    # tee writes every file it is given
    r"\btee\b[^;|&\n]*?\s['\"]?" + _P_NAME,
    # cp, mv, install, ln, rsync: the file is the DESTINATION, the segment's
    # last word (the file as a SOURCE is a read and passes)
    r"\b(?:cp|mv|install|ln|rsync)\b[^;|&\n]*\s['\"]?" + _P_NAME
    + r"['\"]?[ \t]*(?:$|[;|&)])",
    r"\btouch\b[^;|&\n]*?\s['\"]?" + _P_NAME,
    r"\bof=['\"]?" + _P_NAME,
    r"\bsed\b[^;|&\n]*\s-i\b[^;|&\n]*" + _P_NAME,
    # python: open(..., 'w'/'a'/'x'), Path(...).write_text/bytes, os.open
    # with a writing flag, and a link/rename/copy ONTO the file
    r"\bopen\s*\([^)]*?(owner-away|owner-notice)[^)]*,\s*['\"]?[wax]",
    r"(owner-away|owner-notice)['\"]?\s*\)\s*\.\s*write_(?:text|bytes)\b",
    r"\bos\s*\.\s*open\s*\([^)]*(owner-away|owner-notice)[^)]*"
    r"O_(?:WRONLY|RDWR|CREAT|APPEND|TRUNC)",
    r"\b(?:os\s*\.\s*(?:link|symlink|replace|rename)|shutil\s*\.\s*"
    r"(?:copy\w*|move))\s*\([^)]*,\s*['\"]?" + _P_NAME,
))
# A HAND DELETE OF THE NOTICE un-says the owner's word fleet-wide, and clearing
# it is his (ownernotice.clear_notice takes his door). The away FLAG is not
# here on purpose: lifting it is everyone's by design (away.lift, `helm
# back`), so rm, unlink and mv of the flag pass.
_N_NAME = (r"(?:[^\s;|&<>()]*/)?['\"]?(owner-notice)(?=['\"\s;|&)]|$)")
_POSTURE_DELETES = tuple(re.compile(x, re.M) for x in (
    r"\b(?:rm|unlink|shred|truncate)\b[^;|&\n]*?\s['\"]?" + _N_NAME,
    # mv with the notice anywhere: as the destination it is a write (above),
    # as the source it takes the notice away
    r"\bmv\b[^;|&\n]*?\s['\"]?" + _N_NAME,
    r"\b(?:os\s*\.\s*(?:remove|unlink|rename|replace)|shutil\s*\.\s*move)"
    r"\s*\([^)]*?(owner-notice)",
    r"(owner-notice)['\"]?\s*\)\s*\.\s*(?:unlink|rename|replace)\b",
))
# The programs whose text this rung reads as CODE in the shared fold, besides
# the shells and the evaluators the fold already knows (`_command_data`).
_POSTURE_INTERPRETERS = re.compile(
    r"python(?:[23](?:\.\d+)?)?|pypy3?|ipython3?")
# A command whose every program only SEARCHES or READS may name the mint: that
# is how a seat finds the code. It is asked of the FOLDED text, so a message,
# a post body, a store statement, a search pattern or a document a data
# program reads is already gone (task/2973's fold, `_invocation_text`), and
# a re-executor (eval, source, bash -c, sh -c, python -c, printf -v, xargs,
# a substitution) keeps its words. Anything else that names the mint is
# refused.
_POSTURE_READERS = frozenset((
    "cat", "cd", "cut", "diff", "echo", "egrep", "fgrep", "file", "grep",
    "head", "less", "ls", "more", "nl", "printf", "rg", "sed", "sort", "stat",
    "tail", "tr", "true", "ugrep", "uniq", "wc"))
_POSTURE_GIT_READS = frozenset(("blame", "diff", "grep", "log", "show"))
_POSTURE_PREFIX = frozenset(("builtin", "command", "env", "nice", "nohup",
                             "sudo", "time"))
_POSTURE_SPLIT = re.compile(r"&&|\|\||[;|\n`]|\$\(")


def _posture_reads_only(command):
    """True when every program the text runs is a search or a read."""
    for seg in _POSTURE_SPLIT.split(command or ""):
        words = [w.strip("'\"(){}!") for w in seg.split()]
        words = [w for w in words if w]
        while words and (re.match(r"^\w+=", words[0])
                         or words[0] in _POSTURE_PREFIX):
            words.pop(0)
        if not words:
            continue
        prog = words[0].rsplit("/", 1)[-1]
        if prog == "git":
            if not _POSTURE_GIT_READS.intersection(words[1:]):
                return False
        elif prog not in _POSTURE_READERS:
            return False
    return True


def owner_posture_forge_refusal(command=None, path=None):
    """What a command (or a Write/Edit path) would forge of the owner's
    posture, or None: ("mint", None), ("write", <file name>) or ("delete",
    "owner-notice")."""
    if path:
        name = os.path.basename(str(path))
        return ("write", name) if name in _POSTURE_NAMES else None
    text = command or ""
    low = text.casefold()
    if "owner" not in low and "door_mint" not in low:
        return None
    # THE DATA IS CUT FIRST, through the one fold the Actions rung uses: a
    # word that no program runs cannot mint a door or write a file
    text, _code = _invocation_text(text, _POSTURE_INTERPRETERS)
    if _POSTURE_MINT.search(text) and not _posture_reads_only(text):
        return ("mint", None)
    for kind, rungs in (("write", _POSTURE_WRITES),
                        ("delete", _POSTURE_DELETES)):
        for rx in rungs:
            m = rx.search(text)
            if m:
                return (kind, next(g for g in m.groups() if g))
    return None


def owner_posture_forge_message(hit, tool="Bash"):
    """The refusal: what would have been forged, whose word it is, and what a
    seat does instead. Writes words and decides nothing."""
    kind, name = hit
    what = ("mints the owner's door (ownerasks.owner_door) in code it runs"
            if kind == "mint" else
            "deletes the owner's fleet notice (%s) by hand; clearing it is "
            "his, from the away card, while the away flag anyone may lift "
            "with `helm back`" % name if kind == "delete" else
            "writes the owner's %s (%s) by hand"
            % ("away flag" if name == "owner-away" else "fleet notice", name))
    return ("[helm argv-guard] BLOCKED: this %s %s. The away flag and the "
            "fleet notice reach every seat as the owner's word, so only his "
            "web console's away card writes them as him. A seat that must "
            "mark him away runs `helm away`, which records the seat as the "
            "declarer, or posts in helm chat; `helm away status` reads both. "
            "A search or a read of either (git grep, cat) passes."
            % ("command" if tool in ("Bash", "Monitor") else tool + " call",
               what))


# THE ENV-DUMP RUNG (task/3037). A command that PRINTS the environment, the
# value of a secret-looking variable or a credentials file puts every value in
# it into the transcript, and the transcript goes to the model provider. In
# another project a real production secret left that way, through a box's
# env dump. This rung refuses the PRINT and never the USE: a variable passed
# to the command that needs it, a presence check, and a file that is loaded or
# copied all pass.
#
# IT READS PROGRAMS, NOT WORDS. The command goes through the shell reader the
# Actions rung uses (`_ShellReader`), and a rule applies only to the program a
# simple command RUNS. So `env` in a quoted string, a commit message, a grep
# pattern or a heredoc body written to a file is never read as a dump. The
# rung follows a command into a printer's `$(…)` (and `$(< FILE)`, which is
# FILE), a reader's `<(…)` as an operand, a redirect or a here-string, the
# substitutions of an unquoted heredoc body a reader prints, `bash -c`,
# `eval`, a heredoc a shell reads, and the command that `ssh`, `docker exec`,
# `kubectl exec` or `su -c` runs somewhere else, because that output comes
# back to this terminal. The command in an output substitution `>(…)` writes
# to THIS shell's stdout, so a dump run in one is read as a command of its own.
#
# A PRINT LEAKS ONLY WHERE IT REACHES THE TERMINAL: through every later stage
# of its pipeline that passes its input on (`_ENV_PASSES`), and with no
# redirect of its stdout to a file. A redirect to stderr or the tty, a `tee`
# operand or an output file that is one, and a `>(…)` whose command passes
# its input on reach it past the pipe. A reader whose output option or
# operand names a file (`sort -o F`, `uniq IN OUT`, `_ENV_WRITERS`) writes
# there and prints nothing, and that file is never one it reads. So `env |
# sort` and `grep KEY .env` are refused, and `env | wc -l`, `grep -c KEY
# .env`, `printenv TOKEN | gh auth login --with-token`, `cat .env > backup`,
# `sort -o out .env` and `x=$(cat token)` pass. A
# NAMES-ONLY filter (`cut -d= -f1`, `sed 's/=.*//'`, `awk -F= '{print $1}'`)
# passes too: it prints no value. `export $(…)` is refused wherever its output
# goes: an empty substitution runs a bare `export`, which prints every value,
# and bash's error for a malformed line quotes that line.
#
# WHAT IT CANNOT SEE, stated for the next reader: a script run by its path,
# an interpreter's own dump (`python3 -c 'print(os.environ)'`), `set -x`
# tracing an expanded secret, a recursive grep that walks into a `.env`, a CLI
# that prints its own token (`gh auth token`), and a file that is SOURCED but
# is not valid shell, whose parse errors echo its values. The argv-guard never
# reads file content, so the cures that load a file send its errors away
# (`. FILE 2>/dev/null`). A lowercase name (`$key`) is read as a shell variable of
# the command itself, not as an environment secret. Where the shell reader
# cannot settle the text, only a bare dump on a line of its own is refused.
_ENV_GATE = re.compile(
    r"\b(?:env|printenv|export|set|declare|typeset|inspect)\b|environ"
    r"|\$\{?[#!]?[A-Z_0-9]*(?:KEY|TOKEN|SECRET|PASS|CRED|PRIVATE|AUTH|MASTER"
    r"|DSN|_UR[LI])"
    r"|\$\{!|(?i:cred|secret|\.pem\b|\.key\b|token|\.netrc|\.pgpass"
    r"|\.npmrc|\.pypirc|id_(?:rsa|[a-z]*dsa|ed\d+))")
# a secret-looking NAME: a part (split at `_`) that is, or ends with, one of
# these words, or starts with one of the heads. AUTHOR and AUTHORITY are not
# AUTH, a name whose last part says WHERE a secret lives (FILE, PATH, DIR,
# SOCK) holds a location, not the secret, and a KEY after a word that says
# what it indexes (ORCA_PANE_KEY, SORT_KEY) is a handle, not a secret. A
# database's URL, URI or DSN carries its password, so it is one.
_SECRET_WORDS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "PASSPHRASE",
                 "PASS", "CRED", "PRIVATE", "AUTH", "MASTER")
_SECRET_HEADS = ("SECRET", "PASSWORD", "PASSWD", "PASSPHRASE", "CRED",
                 "PRIVATE", "AUTH")
_SECRET_WHERE = frozenset(("FILE", "PATH", "DIR", "SOCK"))
_SECRET_HANDLES = frozenset(("PANE", "SORT", "CACHE", "PARTITION", "ROUTING",
                             "LOOKUP", "IDEMPOTENCY", "PRIMARY", "FOREIGN"))
_SECRET_STORES = frozenset(("DATABASE", "DB", "POSTGRES", "POSTGRESQL", "PG",
                            "MYSQL", "REDIS", "MONGO", "MONGODB", "AMQP",
                            "RABBITMQ"))
# a credentials file, by its base name; a template, a public key and a source
# or document file are not one
_CRED_FILE = re.compile(
    r"(?i)^\.env(?:$|[._-])|\.env$|(?<![a-z])(?:credential|secret)"
    r"|\.pem$|\.key$|(?<![a-z])token(?!s|i[sz])|(?:^|[._-])creds?(?:$|[._-])"
    r"|^id_(?:rsa|[a-z]*dsa|ed\d+)"
    r"|^\.(?:netrc|pgpass|npmrc|pypirc)$")
_NOT_CRED_FILE = re.compile(
    r"(?i)\.(?:example|sample|template|tmpl|dist)(?:\.|$)"
    r"|\.(?:pub|py|pyc|js|mjs|cjs|ts|tsx|jsx|rs|go|rb|java|kt|kts|scala|c|h"
    r"|cc|cpp|hpp|cs|swift|php|sh|bash|zsh|fish|md|rst|html|css|scss|vue"
    r"|svelte|lua|pl|pm|ex|exs|erl|hs|ml|sql|proto|lock|orig|rej|diff|patch"
    r"|mdx|log|jsonl|out|err|csv|tsv)$")
_PROC_ENVIRON = re.compile(r"^/proc/[^/\s]+/(?:task/[^/\s]+/)?environ$")
# the readers whose first operand is a pattern or a script, not a file
_ENV_SCRIPTED = frozenset(("grep", "egrep", "fgrep", "zgrep", "rg", "ag",
                           "ugrep", "sed", "awk", "gawk", "mawk", "nawk",
                           "jq", "yq"))
_ENV_GREPS = frozenset(("grep", "egrep", "fgrep", "zgrep", "rg", "ag",
                        "ugrep"))
_ENV_AWKS = frozenset(("awk", "gawk", "mawk", "nawk"))
# a grep's options that take the NEXT word as their value, which is neither
# its pattern nor a file (rg alone reads -E and -r so)
_ENV_GREP_VALUED = frozenset((
    "-A", "-B", "-C", "-m", "-d", "-D", "--include", "--exclude",
    "--exclude-dir", "--exclude-from", "--label", "--context",
    "--after-context", "--before-context", "--max-count", "-g", "--glob",
    "--iglob", "-t", "--type", "-T", "--type-not", "-M", "--max-columns",
    "--max-depth", "-j", "--threads", "--max-filesize", "--encoding",
    "--replace", "--sort", "--sortr", "--pre", "--pre-glob", "--ignore-file",
    "--type-add", "--colors", "--path-separator", "--context-separator",
    "--engine"))
# the programs that print what they read from a FILE operand. The formatters
# fmt/pr/expand/unexpand print a file operand exactly as `cat` does, so they
# belong here, not only in _ENV_PASSES (task/3037).
_ENV_READERS = _ENV_SCRIPTED | frozenset((
    "cat", "tac", "head", "tail", "less", "more", "bat", "batcat", "nl",
    "strings", "od", "xxd", "hexdump", "base64", "sort", "uniq", "cut",
    "diff", "column", "fold", "paste", "rev", "zcat", "fmt", "pr", "expand",
    "unexpand", "comm", "join", "iconv"))
# THE READERS THAT CAN WRITE WHAT THEY PRINT TO A FILE (task/3037): the file
# an output option or an output operand names is WRITTEN, never read, and a
# reader that writes to a file prints nothing. Each: the short letters and the
# long options that take a value (xxd reads whole words, `-c 8`, `-cols 8`),
# the role of an option whose value is a file ("in" read, "out" written), and
# whether the second operand is the output (uniq, xxd). BSD base64 takes -i and
# -o; GNU base64 refuses -o and prints nothing.
_ENV_WRITERS = {
    "sort": ("kotST", ("--key", "--output", "--field-separator",
                       "--buffer-size", "--temporary-directory",
                       "--files0-from", "--batch-size", "--parallel",
                       "--compress-program", "--random-source", "--sort"),
             {"-o": "out", "--output": "out"}, False),
    "uniq": ("fsw", ("--skip-fields", "--skip-chars", "--check-chars"), {},
             True),
    "base64": ("bwio", ("--wrap", "--break", "--input", "--output"),
               {"-i": "in", "--input": "in", "-o": "out", "--output": "out"},
               False),
    "xxd": ("", ("-c", "-cols", "-g", "-groupsize", "-l", "-len", "-n",
                 "-name", "-o", "-offset", "-s", "-seek", "-R"), {}, True),
    "iconv": ("fto", ("--from-code", "--to-code", "--output"),
              {"-o": "out", "--output": "out"}, False),
}
# the programs that print what they read from STDIN: a stage of these carries
# a dump on to the terminal, and any other program is the command that NEEDS
# the value (a login, an upload, a count) and prints none of it. `tee` reads
# stdin but takes no cred-file OPERAND (its operands are write targets), so
# it is here and not in _ENV_READERS.
_ENV_PASSES = _ENV_READERS | frozenset((
    "tr", "tee", "xargs"))
_ENV_PRINTERS = frozenset(("echo", "printf", "print"))
_ENV_SHELLS = frozenset(("bash", "sh", "zsh", "dash", "ksh", "mksh", "ash",
                         "fish"))
_ENV_BOXES = frozenset(("docker", "podman", "nerdctl", "kubectl", "oc",
                        "lxc", "incus"))
_ENV_BOX_VALUED = frozenset((
    "-e", "--env", "--env-file", "-u", "--user", "-w", "--workdir",
    "--detach-keys", "-n", "--namespace", "-c", "--container", "--context",
    "--cluster", "--kubeconfig", "--name", "-v", "--volume", "-p",
    "--publish", "--network", "--net", "--entrypoint", "--platform", "-l",
    "--label", "--mount", "-m", "--memory", "--cpus", "--add-host", "-h",
    "--hostname", "--restart", "--pull", "--cap-add", "--cap-drop",
    "--device", "--gpus", "--ipc", "--pid", "--security-opt", "--tmpfs",
    "--ulimit", "--log-driver", "--log-opt", "--shm-size", "-f", "--file",
    "--project-name", "--profile", "--index", "--pod-running-timeout"))
# a container runtime's `inspect` of these nouns prints no environment
_ENV_BOX_NOUNS = frozenset(("network", "volume", "node", "plugin", "context",
                            "manifest", "buildx", "secret"))
_SSH_VALUED = frozenset("BbcDEeFIiJLlmOopPQRSWw")
# a write target that is this command's stdout, and one that is the terminal
# past its stdout; a bare digit is an fd only after `>&`, and a file elsewhere
_ENV_STDOUTS = frozenset(("/dev/stdout", "/dev/fd/1", "/proc/self/fd/1"))
_ENV_ASIDE = frozenset(("/dev/stderr", "/dev/tty", "/dev/fd/2",
                        "/proc/self/fd/2"))
_ENV_EXPANSION = re.compile(
    r"\$(?:([A-Za-z_]\w*)|\{([#!]?)([A-Za-z_]\w*)([^}]*)\})")
_ENV_ENUMERATES = re.compile(r"\bcompgen\b|\$\{![A-Za-z_]\w*[*@]\}")
_ENV_CRUDE_SPLIT = re.compile(r"&&|\|\||[;&|\n()`]|\$\(")
_ENV_CRUDE_DOC = re.compile(
    r"(?<!<)<<-?[ \t]*['\"]?([A-Za-z_][\w.-]*)")
_ENV_SPELL = 60


def _secret_name(name):
    """Whether an environment NAME looks like it holds a secret."""
    if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", name or "") \
            or not re.search(r"[A-Z]", name):
        return False
    parts = [p for p in name.split("_") if p]
    if len(parts) > 1 and (parts[-1] in _SECRET_WHERE or (
            parts[-1] == "KEY" and parts[-2] in _SECRET_HANDLES)):
        return False
    if "DSN" in parts or (_SECRET_STORES.intersection(parts)
                          and {"URL", "URI"}.intersection(parts)):
        return True
    return any(not (p.startswith("AUTHOR") and not p.startswith("AUTHORIZ"))
               and (p.startswith(_SECRET_HEADS)
                    or any(p == w or p.endswith(w) for w in _SECRET_WORDS))
               for p in parts)


def _env_text(word):
    """What a word spells with its quotes gone: its value where the text
    settles it, else its raw text less one pair of outer quotes."""
    v = word.value()
    if v is not None:
        return v
    raw = word.raw
    if len(raw) > 1 and raw[0] == raw[-1] and raw[0] in "'\"":
        return raw[1:-1]
    return raw


def _cred_path(text):
    """The spelling of a credentials file or a process environment that a
    path names, or None."""
    if _PROC_ENVIRON.match(text):
        return "/proc/<pid>/environ"
    base = text.rstrip("/").rsplit("/", 1)[-1]
    if _CRED_FILE.search(base) and not _NOT_CRED_FILE.search(base):
        return base
    return None


def _secret_expansions(raw, body=False):
    """(the first secret-looking name whose VALUE `raw` expands, whether it
    expands a variable by indirection). Single quotes hide an expansion
    except in a heredoc `body`; a length (`${#N}`), an alternate value
    (`${N:+x}`) and a list of names (`${!N*}`) print no value."""
    found, indirect, dq, i, n = None, False, False, 0, len(raw)
    while i < n:
        c = raw[i]
        if c == "\\":
            i += 2
        elif c == "'" and not dq and not body:
            j = raw.find("'", i + 1)
            i = n if j < 0 else j + 1
        elif c == '"' and not body:
            dq, i = not dq, i + 1
        elif c == "$" and raw.startswith("$'", i) and not dq and not body:
            j = i + 2
            while j < n and raw[j] != "'":
                j += 2 if raw[j] == "\\" else 1
            i = j + 1
        elif c == "$":
            m = _ENV_EXPANSION.match(raw, i)
            if not m:
                i += 1
                continue
            name, mark, braced, op = m.groups()
            if name:
                found = found or (name if _secret_name(name) else None)
            elif mark == "!":
                indirect = indirect or not op.startswith(("*", "@", "["))
            elif not mark and not op.startswith((":+", "+")) \
                    and _secret_name(braced):
                found = found or braced
            i = m.end()
        else:
            i += 1
    return found, indirect


def _env_redirect_fd(text, op, target):
    """The fd a redirection names before its operator, '' for none. The
    reader drops it, and `2>/dev/null` must not read as stdout."""
    j = target.start
    while j > 0 and text[j - 1] in " \t":
        j -= 1
    j -= len(op)
    m = re.search(r"(?:^|[\s;&|()])(\d+|\{[A-Za-z_]\w*\})$",
                  text[max(0, j - 16):j])
    return m.group(1) if m else ""


def _env_stages(tokens, text):
    """The pipelines of a token stream: each a list of stages, each stage
    (words, redirections as (op, fd, target), heredoc positions)."""
    pipes = [[([], [], [])]]
    for tok in tokens:
        kind, stage = tok[0], pipes[-1][-1]
        if kind == "w":
            stage[0].append(tok[1])
        elif kind == "r":
            stage[1].append((tok[1], _env_redirect_fd(text, tok[1], tok[2]),
                             tok[2]))
        elif kind == "h":
            stage[2].append(tok[1])
        elif kind == "p":
            pipes[-1].append(([], [], []))
        else:
            pipes.append([([], [], [])])
    return pipes


def _env_options(words, k, valued=(), flags=()):
    """`_options` for a program whose unknown option is a flag."""
    while k < len(words):
        v = words[k].value()
        if v is None or v == "-" or not v.startswith("-"):
            return k
        if v == "--":
            return k + 1
        k += 2 if v in valued else 1
    return k


def _env_unwrap(words):
    """(index, name, dump) of what a stage runs: past its prefix words,
    assignments and wrappers, and past `env` launching a command. `dump` is
    the spelling of an `env` that launches nothing and so prints the
    environment; `name` is None where nothing runs or the text does not
    settle it. `env -S` hands its string on as `(index, "env -S", None)`."""
    k = 0
    while k < len(words):
        w = words[k]
        v = w.value()
        if w.plain and w.raw in _COMMAND_PREFIX:
            k += 2 if w.raw == "time" and k + 1 < len(words) \
                and words[k + 1].raw == "-p" else 1
        elif _GRANT_ASSIGNMENT.match(w.raw):
            k += 1
        elif v == "env" or (v or "").endswith("/env"):
            k, cleared = k + 1, False
            while k < len(words):
                o = words[k].value()
                if o in ("-S", "--split-string") or (o or "").startswith(
                        ("-S", "--split-string=")):
                    return k, "env -S", None
                if o in ("-u", "--unset", "-C", "--chdir"):
                    k += 2
                elif o == "--":
                    k += 1
                    break
                elif o in ("-", "--ignore-environment") or (
                        o and re.fullmatch(r"-[i0v]*i[i0v]*", o)):
                    cleared, k = True, k + 1
                elif o and o.startswith("-") and len(o) > 1:
                    k += 1
                else:
                    break
            while k < len(words) and _GRANT_ASSIGNMENT.match(words[k].raw):
                k += 1
            if k >= len(words):
                return k, None, None if cleared else "env"
        elif v in _WRAPPERS:
            k = _WRAPPERS[v](words, k + 1)
            if k is None:
                return None, None, None
        elif v is None:
            return k, None, None
        else:
            return k, v.rsplit("/", 1)[-1], None
    return k, None, None


def _env_getopt(name, args):
    """(read, written) of a reader in _ENV_WRITERS, under its own reading of
    its options: the operands and input files it reads, less the ones a
    substitution fills, and the write targets its output goes to."""
    short, valued, roles, second = _ENV_WRITERS[name]
    ops, read, written, k = [], [], [], 0
    while k < len(args):
        w, k = args[k], k + 1
        v = _env_text(w)
        if v == "--":
            ops += args[k:]
            break
        if w.subst or v == "-" or not v.startswith("-"):
            ops.append(w)
            continue
        opt, value = v, None
        if v.startswith("--") and "=" in v:
            opt, value = v.split("=", 1)
        elif v.startswith("--") or not short:
            if v in valued:
                value, k = args[k] if k < len(args) else None, k + 1
        else:
            at = next((i for i, c in enumerate(v) if i and c in short), None)
            if at is not None:
                opt, value = "-" + v[at], v[at + 1:] or None
                if value is None:
                    value, k = args[k] if k < len(args) else None, k + 1
        if roles.get(opt) and value is not None:
            (read if roles[opt] == "in" else written).append(value)
    if second and len(ops) > 1:
        written.append(ops.pop(1))
    return ([_env_text(x) for x in ops + read
             if isinstance(x, str) or not x.subst],
            [x if isinstance(x, str) else _env_text(x) for x in written])


def _env_sink(text):
    """Where a write target sends what is written to it: "stdout", "aside"
    (the terminal past this command's stdout: stderr, the tty, or an output
    substitution whose command passes its input on), or None for a file."""
    if text in _ENV_STDOUTS or text == "-":
        return "stdout"
    return "aside" if text in _ENV_ASIDE or _env_proc_passes(text) else None


def _env_proc_passes(text):
    """Whether `text` is an output substitution `>(…)` whose command carries
    what it reads on to the terminal, since its stdout is this shell's."""
    if not (text.startswith(">(") and text.endswith(")")):
        return False
    inner = text[2:-1]
    try:
        pipes = _env_stages(_ShellReader(inner).read(), inner)
    except _Unsettled:
        return True
    return any(_env_reaches([([], [], [])] + p, 0) for p in pipes)


def _env_quiet(name, args):
    """Whether a reader stage prints NONE of what it reads: a grep that
    counts, tests or lists files, a sed or yq editing in place, a reader
    writing its output to a file, or a names-only filter over NAME=value
    lines."""
    vals = [_env_text(a) for a in args]
    if name in _ENV_WRITERS:
        return any(_env_sink(t) is None for t in _env_getopt(name, args)[1])
    if name == "yq":
        return any(v in ("-i", "--inplace") or v.startswith("--inplace=")
                   for v in vals)
    if name in _ENV_GREPS:
        return any(v in ("--count", "--quiet", "--silent", "--count-matches",
                         "--files-with-matches", "--files-without-match")
                   or (re.fullmatch(r"-[A-Za-z]+", v)
                       and set(v[1:]) & set("cqlL"))
                   for v in vals)
    if name == "sed":
        if any(v.startswith("--in-place") or re.match(r"-[A-Za-z]*i", v)
               for v in vals):
            return True
        return any(re.fullmatch(r"s(.)=\.\*\$?\1\1g?", v) for v in vals)
    if name in _ENV_AWKS:
        sep = any(v in ("-F=", "-F'='") for v in vals) or any(
            a == "-F" and b == "=" for a, b in zip(vals, vals[1:]))
        return sep and any(re.fullmatch(r"\{\s*print\s+\$1\s*;?\s*\}", v)
                           for v in vals)
    if name == "cut":
        joined = " ".join(vals)
        return bool(re.search(r"(?:^| )(?:-d ?=|--delimiter[= ]=)(?: |$)",
                              joined)
                    and re.search(r"(?:^| )(?:-f ?1|--fields[= ]1)(?: |$)",
                                  joined))
    return False


def _env_narrows(name, args):
    """Whether a grep prints no value but a named variable's: every pattern
    alternative an exact NAME that does not look secret, bounded by `=`, -w
    or -x so it matches no longer name; or, with -o, a pattern over name
    characters that prints the name alone. Context lines, -v, -a and -f
    patterns can print any line, so none of them narrows."""
    if name not in _ENV_GREPS:
        return False
    words = [_env_text(a) for a in args]
    pats, flags, k = [], set(), 0
    while k < len(words):
        w = words[k]
        if w in ("-e", "--regexp"):
            pats.append(words[k + 1] if k + 1 < len(words) else "")
            k += 2
        elif w.startswith("--regexp="):
            pats.append(w.split("=", 1)[1])
            k += 1
        elif w.startswith("--"):
            flags.add(w.split("=", 1)[0])
            k += 2 if w in _ENV_GREP_VALUED else 1
        elif w.startswith("-") and len(w) > 1:
            k += 1
            for i, ch in enumerate(w[1:]):
                flags.add(ch)
                if ch == "e" or ch in "ABCmdDgtTMj":
                    rest = w[i + 2:]
                    if ch == "e":
                        pats.append(rest or (words[k] if k < len(words)
                                             else ""))
                    k += 0 if rest else 1
                    break
        else:
            if not pats:
                pats.append(w)
            k += 1
    if not pats or flags & {"v", "f", "a", "A", "B", "C", "--invert-match",
                            "--file", "--text", "--context", "--after-context",
                            "--before-context", "--passthru"}:
        return False
    only = bool(flags & {"o", "--only-matching"})
    whole = bool(flags & {"w", "x", "--word-regexp", "--line-regexp"})
    # after `=`: anything where the whole line is printed (the name is exact
    # and not secret), but only literal text where -o prints the match, or
    # `=.+` would print the value itself
    value = r"=[A-Za-z0-9_:/@-]*" if only else r"=.*"
    for pat in pats:
        group = re.fullmatch(r"\^?\((.*)\)(%s)?\$?" % value, pat)
        body, eq = group.groups() if group else (pat, None)
        for alt in re.split(r"\\?\|", body):
            m = re.fullmatch(r"\^?([A-Za-z_][A-Za-z0-9_]*)(%s)?\$?" % value,
                             alt)
            if only:
                if not (m or re.fullmatch(
                        r"(?:\^|[A-Za-z_]{3})[\w\[\]^*+?{}(),-]*(%s)?"
                        % value, alt)):
                    return False
            elif not m or _secret_name(m.group(1).upper()) \
                    or not (eq or m.group(2) or whole):
                return False
    return True


def _env_files(name, args):
    """The words a reader reads as FILES: its operands, less the pattern or
    script a scripted reader takes first unless an option already gave it,
    less a file it writes (_ENV_WRITERS), and with the file jq's --rawfile or
    --slurpfile loads into a variable its program can print."""
    if name in _ENV_WRITERS:
        return _env_getopt(name, args)[0]
    files, loaded, given, skip, ended = [], [], False, 0, False
    for i, a in enumerate(args):
        if skip:
            skip -= 1
            continue
        if a.subst:                 # read by `_env_inner`, not as a name
            continue
        s = _env_text(a)
        if not ended and s.startswith("-") and s != "-":
            if s == "--":
                ended = True
            elif name == "jq":
                given = given or s in ("-f", "--from-file")
                if s in ("--slurpfile", "--rawfile") and i + 2 < len(args) \
                        and not args[i + 2].subst:
                    loaded.append(_env_text(args[i + 2]))
                skip = 2 if s in ("--arg", "--argjson", "--slurpfile",
                                  "--rawfile") else int(
                    s in ("-f", "--from-file", "--indent"))
            elif name in _ENV_GREPS and (s in _ENV_GREP_VALUED or (
                    name == "rg" and s in ("-E", "-r"))):
                skip = 1
            elif s in ("--regexp", "--file", "--expression", "--source") or (
                    name in _ENV_SCRIPTED
                    and re.fullmatch(r"-[A-Za-z]*[ef]", s)):
                given, skip = True, 1
            elif name in _ENV_SCRIPTED and re.fullmatch(r"-[A-Za-z]*[ef].+",
                                                        s):
                given = True
            elif name in _ENV_AWKS and s in ("-v", "-F"):
                skip = 1
            continue
        files.append(s)
    return (files[1:] if name in _ENV_SCRIPTED and not given else files) \
        + loaded


def _env_xargs(args):
    """(whether xargs prints what it reads, the file it reads with -a)."""
    k, src = 0, None
    while k < len(args):
        v = args[k].value()
        if v is None:
            break
        if v in ("-a", "--arg-file"):
            src = _env_text(args[k + 1]) if k + 1 < len(args) else None
            k += 2
        elif v.startswith("--arg-file="):
            src, k = v.split("=", 1)[1], k + 1
        elif v.startswith("-a") and len(v) > 2:
            src, k = v[2:], k + 1
        elif v == "--":
            k += 1
            break
        elif v.startswith("-") and v != "-":
            k += 2 if v in ("-I", "-n", "-L", "-P", "-d", "-E", "-s", "-J",
                            "-R") else 1
        else:
            break
    prog = args[k].value() if k < len(args) else "echo"
    return (prog or "").rsplit("/", 1)[-1] in _ENV_PASSES | _ENV_PRINTERS, src


def _env_passes_on(stage):
    """Whether a later pipeline stage carries what it reads to its stdout."""
    k, name, _dump = _env_unwrap(stage[0])
    if name == "xargs":
        return _env_xargs(stage[0][k + 1:])[0]
    return name in _ENV_PASSES and not _env_quiet(name, stage[0][k + 1:]) \
        and not _env_narrows(name, stage[0][k + 1:])


def _env_out(stage):
    """Where what a stage prints goes: "stdout" on down its pipeline,
    "aside" to the terminal past it (a redirect to stderr or the tty, a tee
    operand or an output file that is one, or a `>(…)` that passes its input
    on), or None into a file or nowhere."""
    k, name, _dump = _env_unwrap(stage[0])
    args = stage[0][k + 1:] if k is not None else []
    if name in _ENV_WRITERS or name == "tee":
        outs = _env_getopt(name, args)[1] if name != "tee" else [
            _env_text(a) for a in args if not _env_text(a).startswith("-")]
        if any(_env_sink(t) == "aside" for t in outs):
            return "aside"
    out = "stdout"
    for op, fd, target in stage[1]:
        if op.startswith("<") or (fd not in ("", "1")
                                  and not op.startswith("&")):
            continue
        text = _env_text(target)
        if op == ">&" and text.isdigit():
            out = {"1": "stdout", "2": "aside"}.get(text)
        else:                               # `> -` is a file, `>&-` a close
            out = None if text == "-" else _env_sink(text)
    return out


def _env_reaches(pipe, s):
    """Whether what stage `s` prints reaches the terminal."""
    for i, st in enumerate(pipe[s:]):
        if i and not _env_passes_on(st):
            return False
        out = _env_out(st)
        if out != "stdout":
            return out == "aside"
    return True


def _env_inner(reader, word, depth):
    """The first leak a command or process substitution inside `word`
    prints into it."""
    outer = -1
    for start, end in sorted(reader.substs):
        if start < word.start or start >= word.end or start < outer:
            continue
        outer = end
        text = reader.t[start:end]
        hit = _env_source(text if reader.t[start - 2:start] == "<(" else
                          _env_lone_read(text), depth + 1)
        if hit:
            return hit
    return None


def _env_lone_read(text):
    """A command substitution's text as bash runs it: `$(< FILE)` alone is
    FILE's content, read as `cat` reads it."""
    s = text.strip()
    return "cat " + s if s.startswith("<") and not s.startswith(
        ("<<", "<(")) else text


def _env_body_substs(body, depth):
    """The first leak a `$(…)` or backtick substitution in an unquoted
    heredoc body prints into it: bash substitutes there as it does inside
    double quotes, so a single quote is a letter and a backslash escapes.
    ONE reader walks the body, so a body of many substitutions costs its
    length, not its length times their count."""
    if "`" not in body and "$(" not in body:
        return None
    try:
        r = _ShellReader(body)
    except _Unsettled:
        return _env_crude(body)
    i = 0
    while i < len(body):
        c = body[i]
        if c == "`" or (body.startswith("$(", i)
                        and not body.startswith("$((", i)):
            try:
                i = r.backtick(i + 1) if c == "`" else r.expansion(i)[0]
            except _Unsettled:
                return _env_crude(body[i:])
            start, end = r.substs[-1]         # the outermost, recorded last
            hit = _env_source(_env_lone_read(body[start:end]), depth + 1)
            if hit:
                return hit
        else:
            i += 2 if c == "\\" else 1
    return None


def _env_bodies(reader, docs, depth):
    """The first leak in a heredoc body a shell reads as its script."""
    for at, _quoted, start, end in reader.bodies:
        if at in docs:
            hit = _env_source("\n".join(reader.lines[start:end]), depth + 1)
            if hit:
                return hit
    return None


def _env_wrap(prefix, hit):
    """A leak found in text a stage runs, spelled with that stage."""
    return (hit[0], prefix + " " + hit[1], hit[2]) if hit else None


def _env_stage(stage, reader, depth):
    """(kind, spelling, always) of what one stage prints, or None."""
    words, redirs, docs = stage
    k, name, dump = _env_unwrap(words)
    if dump:
        return "dump", dump, False
    if k is None or name is None:
        return None
    args = words[k + 1:]
    vals = [a.value() for a in args]
    if name == "env -S":
        head = _env_text(words[k])
        head = "" if head in ("-S", "--split-string") else re.sub(
            r"^(?:--split-string=|-S)", "", head)
        return _env_wrap("env -S", _env_source(" ".join(
            [head] + [_env_text(a) for a in args]).strip(), depth + 1))
    if name == "printenv":
        ops = [_env_text(a) for a in args if not _env_text(a).startswith("-")]
        if not ops:
            return "dump", "printenv", False
        hit = next((o for o in ops if _secret_name(o)), None)
        return ("name", "printenv " + hit, False) if hit else None
    if name == "set":
        return None if args else ("dump", "set", False)
    if name in ("export", "declare", "typeset"):
        opts, ops = set(), []
        for a in args:
            v = a.value()
            if not ops and v and v.startswith("-") and len(v) > 1:
                opts |= set(v[1:])
            else:
                ops.append(a)
        if not ops:
            return None if opts & set("fFn") else (
                "dump", name + ("" if not opts else " -"
                                + "".join(sorted(opts))), False)
        if name == "export":
            risky = [a for a in ops if not _GRANT_ASSIGNMENT.match(a.raw)
                     and a.raw[:1] not in "'\"" and ("$" in a.raw
                                                     or "`" in a.raw)]
            if any(a.subst for a in risky) or len(risky) == len(ops):
                return ("export", "export $(…)" if any(a.subst for a in risky)
                        else "export $NAME", True)
            return None
        hit = next((o for o in (_env_text(a).split("=", 1)[0] for a in ops)
                    if _secret_name(o)), None)
        return ("name", "%s -p %s" % (name, hit), False) \
            if "p" in opts and hit else None
    if name in _ENV_PRINTERS:
        if name == "printf" and any((v or "").startswith("-v") for v in vals):
            return None
        for a in args:
            found, indirect = _secret_expansions(a.raw)
            if found:
                return "name", "%s $%s" % (name, found), False
            if indirect and _ENV_ENUMERATES.search(reader.t):
                return "dump", name + " ${!name} over every name", False
            hit = _env_inner(reader, a, depth) if a.subst else None
            if hit:
                return _env_wrap(name, hit)
        return None
    if name in _ENV_READERS or name in ("tr", "tee", "xargs", "done"):
        if _env_quiet(name, args) or _env_narrows(name, args):
            return None
        paths = [] if name in ("tr", "tee", "done") else _env_files(name, args)
        if name == "xargs":
            prints, src = _env_xargs(args)
            if not prints:
                return None
            paths = [src] if src else []
        paths += [_env_text(t) for op, _fd, t in redirs if op in ("<", "<>")]
        for p in paths:
            found = _cred_path(p)
            if found:
                return ("proc" if found.startswith("/proc/") else "file",
                        "%s %s" % (name, found), False)
        for op, _fd, t in redirs:
            found = _secret_expansions(t.raw)[0] if op == "<<<" else None
            if found:
                return "name", "%s <<< $%s" % (name, found), False
        for at, quoted, start, end in reader.bodies:
            if at in docs and not quoted:
                body = "\n".join(reader.lines[start:end])
                found = _secret_expansions(body, body=True)[0]
                if found:
                    return "name", "%s <<EOF $%s" % (name, found), False
                hit = _env_body_substs(body, depth)
                if hit:
                    return _env_wrap(name + " <<EOF", hit)
        # an operand, and what a `<`, `<>` or `<<<` hands its stdin
        for a in args + [t for op, _fd, t in redirs
                         if op in ("<", "<>", "<<<")]:
            hit = _env_inner(reader, a, depth) if a.subst else None
            if hit:
                return _env_wrap(name, hit)
        return None
    if name == "eval":
        # eval joins its words and runs them in THIS shell, so a dump reaches
        # this terminal exactly as `bash -c` does (task/3037). A leading
        # `-` or `--` is eval's only option; the rest is the command it runs.
        j = 0
        while j < len(vals) and vals[j] in ("-", "--"):
            j += 1
        joined = " ".join(_env_text(a) for a in args[j:]).strip()
        return _env_wrap("eval", _env_source(joined, depth + 1))
    if name in _ENV_SHELLS:
        j = 0
        while j < len(args):
            v = vals[j]
            if v is None:
                return None
            if v in ("-o", "+o", "-O", "+O", "--rcfile", "--init-file"):
                j += 2
            elif v.startswith("--"):
                j += 1
            elif v[:1] in "-+" and len(v) > 1:
                if v[0] == "-" and "c" in v[1:]:
                    return _env_wrap(name + " -c", _env_source(
                        _env_text(args[j + 1]), depth + 1)) \
                        if j + 1 < len(args) else None
                j += 1
            else:
                return None                # a script file: not readable here
        return _env_wrap(name, _env_bodies(reader, docs, depth))
    if name in ("su", "runuser"):
        for j, v in enumerate(vals):
            if v in ("-c", "--command") and j + 1 < len(args):
                return _env_wrap(name + " -c", _env_source(
                    _env_text(args[j + 1]), depth + 1))
            if v and v.startswith("--command="):
                return _env_wrap(name + " -c", _env_source(
                    v.split("=", 1)[1], depth + 1))
        return None
    if name == "ssh":
        j = 0
        while j < len(args) and vals[j] and vals[j].startswith("-") \
                and len(vals[j]) > 1:
            at = next((i for i, ch in enumerate(vals[j][1:])
                       if ch in _SSH_VALUED), None)
            j += 1 if at is None or at < len(vals[j]) - 2 else 2
        rest = args[j + 1:]
        if rest:
            return _env_wrap("ssh …", _env_source(
                " ".join(_env_text(a) for a in rest), depth + 1))
        return _env_wrap("ssh …", _env_bodies(reader, docs, depth))
    if name in _ENV_BOXES:
        verb = next((j for j, v in enumerate(vals[:4])
                     if v in ("exec", "run", "inspect")), None)
        if verb is None:
            return None
        if vals[verb] == "inspect":
            if name in ("kubectl", "oc", "lxc", "incus") or (
                    verb and vals[verb - 1] in _ENV_BOX_NOUNS):
                return None
            return None if _env_inspect_narrow(args[verb + 1:]) else (
                "inspect", name + " inspect", False)
        rest = args[verb + 1:]
        cut = next((j for j, a in enumerate(rest) if a.value() == "--"),
                   None)
        if cut is not None:
            run = rest[cut + 1:]
        else:
            j = _env_options(rest, 0, valued=_ENV_BOX_VALUED)
            run = rest[j + 1:]
        if not run:
            return None
        inner = _env_stage((run, [], docs), reader, depth + 1)
        return _env_wrap("%s %s …" % (name, vals[verb]), inner)
    return None


def _env_inspect_narrow(args):
    """Whether `docker inspect` is given a --format that prints no
    environment: one that names neither Env nor the whole object."""
    for j, a in enumerate(args):
        v = a.value() or ""
        fmt = None
        if v in ("-f", "--format") and j + 1 < len(args):
            fmt = _env_text(args[j + 1])
        elif v.startswith("--format="):
            fmt = v.split("=", 1)[1]
        elif v.startswith("-f") and not v.startswith("--") and len(v) > 2:
            fmt = v[2:]
        if fmt is not None:
            # a count of the variables prints none of them
            fmt = re.sub(r"len\s+\.Config\.Env\b", "", fmt)
            return not re.search(
                r"(?i)env|\{\{-?\s*(?:json\s+)?\.\s*(?:Config\s*)?-?\}\}"
                r"|^\s*json\s*$", fmt)
    return False


def _env_crude(text):
    """The reading where the shell reader cannot settle the text: a bare
    dump standing as a command of its own, OUTSIDE every heredoc body and
    every quote. The census found this reading's refusals were all prose
    when it read those too: a markdown table in a --body, a python line."""
    for words in _crude_commands(text):
        prog, rest = words[0], words[1:]
        if (prog in ("env", "printenv", "set", "export", "declare", "typeset")
                and not rest) or (prog in ("export", "declare", "typeset")
                                  and rest in (["-p"], ["-x"], ["-px"])):
            return "dump", " ".join(words), False
    return None


def _crude_commands(text):
    """The words of each command in `text` as a crude split reads them,
    for a rung whose shell reader could not settle the text: every heredoc
    body and every quoted string is cut, the rest is split at every
    separator, and a command's prefix words, wrappers and assignments are
    stepped over. Yields each non-empty list of words."""
    kept, end = [], None
    for line in text.split("\n"):
        if end is not None:
            end = None if line.lstrip("\t") == end else end
            continue
        kept.append(line)
        m = _ENV_CRUDE_DOC.search(line)
        end = m.group(1) if m else None
    code, quote, i, text = [], None, 0, "\n".join(kept)
    while i < len(text):
        c = text[i]
        if quote:
            i += 2 if c == "\\" and quote == '"' else 1
            quote = None if c == quote else quote
        elif c == "\\":
            code.append(" ")
            i += 2
        else:
            quote = c if c in "'\"" else None
            code.append(" _ " if quote else c)
            i += 1
    for seg in _ENV_CRUDE_SPLIT.split("".join(code)):
        words = seg.split()
        while words and (words[0] in _COMMAND_PREFIX
                         or words[0] in ("sudo", "command", "builtin", "exec",
                                         "nohup")
                         or _GRANT_ASSIGNMENT.match(words[0])):
            words.pop(0)
        if words:
            yield words


def _env_source(text, depth=0):
    """(kind, spelling, always) of the first leak `text` prints to its
    stdout, or None. `always` marks a leak refused wherever its output goes
    (`export $(…)`)."""
    if depth > 4 or not _ENV_GATE.search(text):
        return None
    try:
        reader = _ShellReader(text)
        tokens = reader.read()
    except _Unsettled:
        return _env_crude(text)
    for pipe in _env_stages(tokens, text):
        for s, stage in enumerate(pipe):
            hit = _env_stage(stage, reader, depth)
            if hit and (hit[2] or _env_reaches(pipe, s)):
                return hit
    # the command in a `>(…)` writes to this text's stdout, unless it stands
    # inside a substitution whose output something else takes
    for start, end in reader.outs:
        if not any(a <= start < b for a, b in reader.substs):
            hit = _env_wrap(">(…)", _env_source(text[start:end], depth + 1))
            if hit:
                return hit
    return None


def env_dump_refusal(command):
    """(kind, spelling) of the environment, secret variable or credentials
    file a Bash command prints to the transcript, or None. kind is "dump",
    "name", "file", "proc", "export" or "inspect"."""
    text = (command or "").replace("\\\n", "")
    try:
        hit = _env_source(text)
    except Exception:           # the reader's defect must not open the rung
        hit = _env_crude(text)
    if hit is None:
        return None
    spelling = hit[1]
    if len(spelling) > _ENV_SPELL:
        spelling = spelling[:_ENV_SPELL - 1] + "…"
    return hit[0], spelling


_ENV_WHAT = {
    "dump": "the whole environment",
    "name": "the value of a secret-looking variable",
    "file": "a credentials file",
    "proc": "a process's whole environment",
    "export": "the whole environment when its argument expands to nothing",
    "inspect": "a container's environment (Config.Env)",
}
_ENV_CURE = {
    "dump": "Check a name without its value ([ -n \"$NAME\" ] && echo "
            "set), list names only (env | cut -d= -f1), or print exact names "
            "that are not secrets (printenv HOME, grep -E '^(A|B)=').",
    "name": "Check it without printing it ([ -n \"$NAME\" ] && echo set, or "
            "${#NAME} for its length), and pass it to the command that needs "
            "it.",
    "file": "List its names only (cut -d= -f1 FILE), check one (grep -c "
            "'^NAME=' FILE), or load it for the command that needs it (set "
            "-a; . FILE 2>/dev/null; set +a).",
    "proc": "Print exact names that are not secrets, or count one: tr "
            "'\\0' '\\n' < /proc/PID/environ | grep -E '^(A|B)=' (or grep "
            "-c '^NAME=').",
    "export": "Load the file without printing it: set -a; . ./.env "
              "2>/dev/null; set +a.",
    "inspect": "Give it --format with a template that leaves out "
               ".Config.Env, such as --format '{{.State.Status}}'.",
}


def env_dump_message(hit):
    """The refusal: what the command would print, why that leaks, and the
    route that does not. Writes words and decides nothing."""
    kind, spelling = hit
    return ("[helm argv-guard] BLOCKED: this command prints %s (%s) into the "
            "transcript, and the transcript goes to the model provider. %s"
            % (_ENV_WHAT[kind], spelling, _ENV_CURE[kind]))


# WHAT A COMMAND RUNS, AND WHERE. Two rungs ask this reader two questions:
# which git verb runs in which directory (the shared-checkout rung below),
# and which spellings of a refused helm verb write nothing (the sidechain
# authority rung above, `_authority_exempt`). The walk is theirs alone; the
# env-dump rung above reads output, not directories.
_CHDIR_OPTIONS = frozenset(("-C", "--chdir", "-D"))
_HOME_WORD = re.compile(r"\A\"?\$(?:HOME|\{HOME\})(?=[/\"]|\Z)")


def _walk_dir(word, here):
    """The directory a `cd` operand or a `git -C` value names, resolved
    against `here`, or None where the text does not settle it. A leading
    `~/` and a leading `$HOME` are the directory bash expands them to."""
    v = word.value()
    if v is None:
        m = _HOME_WORD.match(word.raw)
        rest = word.raw[m.end():] if m else "$"
        if "$" in rest or "`" in rest or rest.count('"') > 1:
            return None
        v = os.path.expanduser("~") + rest.replace('"', "")
    elif word.raw == "~" or word.raw.startswith("~/"):
        v = os.path.expanduser(v)
    if not os.path.isabs(v):
        if here is None:
            return None
        v = os.path.join(here, v)
    return os.path.normpath(v)


def _cd_dir(args, here):
    """Where `cd ARGS` leaves the shell: HOME with no operand, None for
    `cd -` and anything the text does not settle."""
    j = 0
    while j < len(args) and (args[j].value() or "") in (
            "-L", "-P", "-e", "-@", "-LP", "-PL"):
        j += 1
    if j < len(args) and args[j].value() == "--":
        j += 1
    if j >= len(args):
        return os.path.expanduser("~")
    return None if args[j].value() == "-" else _walk_dir(args[j], here)


def _shell_script(args):
    """What a shell given ARGS runs: ("c", word) for its -c string, ("stdin",
    None) when it reads its script from stdin, or None for a script file or
    options the text does not settle."""
    j = 0
    while j < len(args):
        v = args[j].value()
        if v is None:
            return None
        if v in ("-o", "+o", "-O", "+O", "--rcfile", "--init-file"):
            j += 2
        elif v.startswith("--"):
            j += 1
        elif v[:1] in "-+" and len(v) > 1:
            if v[0] == "-" and "c" in v[1:]:
                return ("c", args[j + 1]) if j + 1 < len(args) else None
            j += 1
        else:
            return None
    return "stdin", None


def _commands_run(text, cwd, depth=0):
    """[(name, words, k, here)] for every simple command `text` runs: the
    program `_program` names (None where the text does not settle it), the
    command's words, the index of the program word, and the directory the
    command runs in, None where the text does not settle it.

    A `cd` or `pushd` that stands as a pipeline of its own moves every later
    command in its scope, and a subshell's parentheses close the scope; a
    `cd` in a pipeline runs in a subshell and moves nothing. A wrapper that
    changes directory itself (`env -C`, `sudo -D`) leaves its command's
    directory unsettled. The walk goes into what a command's `$(…)`,
    backticks, `<(…)` and `>(…)` run, the string a shell runs with -c, the
    words `eval` joins, and a heredoc a shell reads as its script, each in
    the directory of the command that holds it. Raises `_Unsettled` where
    the reader cannot settle `text`; nested text it cannot settle is not
    read."""
    reader = _ShellReader(text)
    tokens = reader.read()
    spans, end = [], -1
    for a, b in sorted(reader.substs + reader.outs):
        if a >= end:                    # the outermost of each nest
            spans.append((a, b))
            end = b
    # each pipeline with the operator before it: a `cd` after `||` runs only
    # when the command before it failed, so it settles no directory
    items, pipe, cur, sep = [], [], ([], [], []), None
    for tok in tokens:
        if tok[0] == "w":
            cur[0].append(tok[1])
        elif tok[0] == "r":
            cur[2].append(tok[2])
        elif tok[0] == "h":
            cur[1].append(tok[1])
        else:
            pipe.append(cur)
            cur = ([], [], [])
            if tok[0] == "s":
                items.append((pipe, sep))
                pipe, sep = [], tok[1]
                if tok[1] in ("(", ")"):
                    items.append(tok[1])
    items.append((pipe + [cur], sep))
    out, scope = [], [cwd]

    def inner(body, at):
        if depth >= 4:
            return
        try:
            out.extend(_commands_run(body, at, depth + 1))
        except _Unsettled:
            pass

    for item in items:
        if item == "(":
            scope.append(scope[-1])
            continue
        if item == ")":
            if len(scope) > 1:
                scope.pop()
            continue
        pipe, sep = item
        for words, docs, targets in pipe:
            at = scope[-1]
            for a, b in spans:
                if any(w.start <= a < w.end for w in words + targets):
                    inner(text[a:b], at)
            if not words:
                continue
            name, k = _program(words)
            if k is None:
                continue
            if any((w.value() or "") in _CHDIR_OPTIONS
                   or (w.value() or "").startswith("--chdir=")
                   for w in words[:k]):
                at = None
            out.append((name, words, k, at))
            args = words[k + 1:]
            if name in ("cd", "pushd") and len(pipe) == 1:
                scope[-1] = None if sep == "||" else _cd_dir(args, at)
            elif name == "popd" and len(pipe) == 1:
                scope[-1] = None
            elif name == "eval":
                inner(" ".join(_env_text(a) for a in args), at)
            elif name in _ENV_SHELLS:
                script = _shell_script(args)
                if script and script[0] == "c":
                    inner(_env_text(script[1]), at)
                elif script:
                    for op, _q, start, stop in reader.bodies:
                        if op in docs:
                            inner("\n".join(reader.lines[start:stop]), at)
    return out


# THE SHARED-CHECKOUT RUNG (task/3057). A `git stash pop` meant for a lane
# ran in helm's shared checkout and left a conflict marker in a module every
# helm verb imports, so every verb and hook in the fleet raised SyntaxError
# until the tree was cleaned by hand. Two facts made it easy to do: git's
# stash is ONE list for every worktree of a repository, so a lane's stash
# pops anywhere, and the Bash tool puts a seat back in the shared checkout
# after every call. This rung refuses a WORKING-TREE git verb whose effective
# directory is the shared checkout, and its refusal spells the same verb with
# `git -C <lane>`.
#
# THE EFFECTIVE DIRECTORY is the payload's cwd, moved by `cd X &&` / `cd X;`
# (and `pushd`) in its scope, then by every `git -C X`, each resolved against
# the one before (`_commands_run`). A directory the text does not settle
# (`cd "$d"`, `git -C $d`, `cd -`, a `cd` after `||`, `env -C`), a
# repository chosen by
# --git-dir, --work-tree or GIT_DIR, and text the shell reader cannot settle
# all PASS: a guard on every Bash call misses an exotic edge rather than
# block work it cannot read.
#
# THE SHARED CHECKOUT is the PRIMARY worktree (its `.git` is a directory; a
# lane's is a file) of a repository with at least one linked worktree, that
# DECLARES the rail guard profile, and whose top is a path the registry
# holds. A repository with no lane has no lane's stash to pop and no fleet
# working from its checkout (`lane_discipline`'s estate clause). And the rail
# is helm's own shared-checkout law, opt-in for a project repository (the
# guard profiles, work._guard): a census of the fleet's recorded commands
# found a rung keyed on registration and lanes alone refusing 740 commands in
# other projects' checkouts, most of them a lead's branch work in its own
# tree. A repository that declares nothing is not refused, even helm's own
# source checkout, whose undeclared default is the rail. The rung reads the
# directory tree first, then one git config read, and the registry last.
#
# THE VERBS are the ones that write the working tree or leave a merge in it
# (`_TREE_WRITES`). Reads, fetch, `pull --ff-only` and `merge --ff-only`
# (which never leave a conflict), `worktree add/remove`, branch reads and a
# `stash`/`stash push`/`stash list` pass; so does `-h`/`--help`. Anything is
# allowed with HELM_WORK_INTEGRATOR=1 in the command or the environment, the
# declaration the integrator already makes to the reference-transaction
# hook. What it cannot see: a git alias, `git apply`, `rm`/`mv`, and a verb
# run by a script or a program that is not git.
_TREE_GATE = re.compile(r"(?<![\w-])(?:stash|checkout|restore|reset|merge"
                        r"|rebase|cherry-pick|revert|am|switch|clean|pull)"
                        r"(?![\w-])")
_TREE_WRITES = {
    "stash": lambda a: bool(a) and a[0] in ("pop", "apply", "drop", "branch"),
    "checkout": bool,
    "restore": bool,
    "switch": lambda a: True,
    "reset": lambda a: any(v in ("--hard", "--merge", "--keep") for v in a),
    "merge": lambda a: "--ff-only" not in a,
    "pull": lambda a: "--ff-only" not in a,
    "rebase": lambda a: True,
    "cherry-pick": lambda a: True,
    "revert": lambda a: True,
    "am": lambda a: True,
    "clean": lambda a: not any(v == "--dry-run" or re.fullmatch(
        r"-[A-Za-z]*n[A-Za-z]*", v or "") for v in a),
}
# git's own options that take the next word; --git-dir and --work-tree choose
# another repository or tree and are read as unsettled
_GIT_VALUED = frozenset(("-c", "--namespace", "--config-env",
                         "--attr-source"))
_GIT_ELSEWHERE = ("--git-dir", "--work-tree", "--bare")
_RAIL_KEY, _RAIL = "helm.guard.profile", "rail"
INTEGRATOR_DECLARATION = "HELM_WORK_INTEGRATOR=1"
_TREE_SPELL = 60


def _git_subcommand(words, k, at):
    """(directory, index of the subcommand word) for `git` at words[k], its
    -C chain applied to `at`, or None where the text does not settle which
    tree the verb writes."""
    if any(w.raw.startswith(("GIT_DIR=", "GIT_WORK_TREE="))
           for w in words[:k]):
        return None
    j = k + 1
    while j < len(words):
        v = words[j].value()
        if v is None or v.startswith(_GIT_ELSEWHERE):
            return None
        if v == "-C":
            if j + 1 >= len(words):
                return None
            at = _walk_dir(words[j + 1], at)       # an absolute one settles it
            j += 2
        elif v in _GIT_VALUED:
            j += 2
        elif v.startswith("-"):
            j += 1
        else:
            return (at, j) if at is not None else None
    return None


def _under_the_rail(top):
    """Whether the repository at `top` DECLARES the rail guard profile. One
    git read, through the seam: the key and the profile name are the guard's
    own (work._guard.PROFILE_KEY, GUARD_PROFILES), pinned equal by a test
    rather than imported, because importing the guard costs this hook half a
    second."""
    from . import vcs
    return vcs.backend(top).probe(top, "config", "--get", _RAIL_KEY,
                                  timeout=2) == _RAIL


def _shared_checkout(path):
    """The top of the shared checkout `path` stands in, or None: a primary
    worktree with a linked lane, whose repository declares the rail and whose
    top the registry holds. Any directory this cannot read answers None."""
    try:
        top = os.path.realpath(path)
        if not stat.S_ISDIR(os.stat(top).st_mode):
            return None
        while True:
            try:
                dot = os.stat(os.path.join(top, ".git"))
                break
            except FileNotFoundError:
                up = os.path.dirname(top)
                if up == top:
                    return None
                top = up
        if not stat.S_ISDIR(dot.st_mode):
            return None                          # a lane: `.git` is a file
        with os.scandir(os.path.join(top, ".git", "worktrees")) as lanes:
            if next(lanes, None) is None:
                return None
    except OSError:
        return None
    if not _under_the_rail(top):
        return None
    paths = [str(r.get("path")) for r in _tree_registry().values()
             if isinstance(r, dict) and r.get("path")]
    if any(os.path.normpath(p) == top for p in paths) or any(
            os.path.realpath(p) == top for p in paths):
        return top
    return None


def shared_checkout_refusal(command, cwd=None, env=None):
    """(checkout, spelling) when a working-tree git verb in `command` would
    run in the shared checkout of a registered repository, else None. `cwd`
    is the payload's; `env` defaults to this process's environment."""
    text = (command or "").replace("\\\n", "")
    if "git" not in text or not _TREE_GATE.search(text) \
            or INTEGRATOR_DECLARATION in text \
            or (os.environ if env is None else env).get(
                "HELM_WORK_INTEGRATOR") == "1":
        return None
    try:
        start = cwd if isinstance(cwd, str) and os.path.isabs(cwd) else None
        for name, words, k, at in _commands_run(text, start):
            call = _git_subcommand(words, k, at) if name == "git" else None
            if call is None:
                continue
            at, j = call
            args = [w.value() for w in words[j + 1:]]
            write = _TREE_WRITES.get(words[j].value())
            if write is None or "-h" in args or "--help" in args \
                    or not write(args):
                continue
            top = _shared_checkout(at)
            if top:
                spelled = " ".join(w.raw for w in words[j:])
                if len(spelled) > _TREE_SPELL:
                    spelled = spelled[:_TREE_SPELL - 1] + "…"
                return top, spelled
    except Exception:          # _Unsettled, or the reader's defect: fail open
        return None
    return None


def shared_checkout_message(hit):
    """The refusal: where the verb would run, why that breaks the fleet, and
    the same verb spelled with the lane. Writes words and decides nothing."""
    top, spelled = hit
    return ("[helm argv-guard] BLOCKED: git %s would run in %s, the shared "
            "checkout: a conflict or another lane's stash left there breaks "
            "every seat working from it (git stash is one list for every "
            "worktree). Use your lane: git -C %s-wt/<lane> %s. The integrator "
            "declares %s." % (spelled, top, top, spelled,
                              INTEGRATOR_DECLARATION))


def cmd_argv_guard(args):
    """chat argv-guard --hook-json — the PreToolUse gate over Bash, Monitor,
    Write, Edit and Agent.

    An Agent call whose tool_input.model is a non-empty string exits 2 (the
    agent-model rung above): the flag never changes the model a subagent runs
    on, and the refusal names the doors that do. Every other Agent call exits
    0 at once and meets no other rung. (A nested-spawn reflex is NOT said
    here: PreToolUse context reaches the model after this call has run, so it
    rides SubagentStart instead — helm/saguide.py.)

    Reads the hook payload, applies argv_guard to a Bash tool_input.command,
    and exits 2 with the cure when it matches. A Bash or Monitor call from a
    subagent (agent_id present) whose TEXT names a beacon arm exits 2 too,
    and so does one that runs a verdict-class write from
    helm.delegate_grant.REFUSED, in a spelling that writes something, unless
    a live grant on the payload's session admits it (the sidechain authority
    rung above, the one delegate rung); an admitted call says which grant
    admitted it. A Bash or Monitor command that runs a working-tree git verb
    in the shared checkout of a registered repository exits 2 (the
    shared-checkout rung above), for a seat and a subagent alike.
    A Bash or Monitor command whose folded text holds a GitHub-Actions
    spelling — a gh Actions noun and verb, an Actions API path, the workflow
    directory — exits 2 unless the command carries HELM_ALLOW_GITHUB_ACTIONS=1
    at its FRONT (the GitHub-Actions rung above), and a Write/Edit whose
    file_path holds one of the same spellings exits 2 with no grant
    available to it; a Monitor's command runs exactly as a Bash command
    does, so it is read by the same rungs. Both rungs decide on
    PRESENCE in folded text; the Actions rung folds the command less the
    data a shell hands to programs that never run it (task/2973), and its
    reader only ever cuts, so it never allows on which word runs.
    Everything else — wrong tool,
    unreadable payload, helm's own bugs — exits 0: a guard on EVERY Bash
    call must fail open or it wedges the fleet (the stop-guard's law).

    ON THE PASS PATH of Bash, Monitor, Write and Edit the TREE RUNG may add
    one advisory line (tree_steers): the call names a path in another
    registered project's checkout whose own native lead is live. It exits 0
    like every pass, and the line rides the JSON envelope on stdout, the one
    channel an agent reads from a hook that exits 0.
    """
    try:
        d = json.load(sys.stdin)
        tool = d.get("tool_name")
        # THE AGENT-MODEL RUNG, first and alone: an Agent call is judged on
        # its `model` key and nothing else. Its prompt is prose for a
        # subagent, not a shell command, so no Bash rung reads it, and the
        # admit path imports nothing and reads no file.
        if tool == "Agent":
            model = agent_model_refusal(d.get("tool_input") or {})
            if model is None:
                return 0
            print(agent_model_message(model), file=sys.stderr)
            return 2
        if tool in ("Write", "Edit"):
            forged = owner_posture_forge_refusal(
                path=(d.get("tool_input") or {}).get("file_path") or "")
            if forged is not None:
                print(owner_posture_forge_message(forged, tool),
                      file=sys.stderr)
                return 2
            act = github_actions_refusal(
                path=(d.get("tool_input") or {}).get("file_path") or "")
            if act is not None:
                print(github_actions_message(act, tool), file=sys.stderr)
                return 2
            _tree_advise(d, (d.get("tool_input") or {}).get("file_path"),
                         first=_act_write_steers(d))
            return 0
        if tool not in ("Bash", "Monitor"):
            return 0
        cmd = (d.get("tool_input") or {}).get("command") or ""
        # THE OWNER-DOOR RUNG, Bash and Monitor alike (task/2997).
        door = owner_door_post_refusal(cmd)
        if door is not None:
            print(owner_door_post_message(door), file=sys.stderr)
            return 2
        # THE OWNER-POSTURE RUNG, Bash and Monitor alike (task/3018): a mint
        # of his door, or a hand write onto his away flag or notice.
        forged = owner_posture_forge_refusal(cmd)
        if forged is not None:
            print(owner_posture_forge_message(forged, tool), file=sys.stderr)
            return 2
        # THE SIDECHAIN RUNG, Bash and Monitor alike: a subagent may not arm,
        # replace or stop its seat's beacon (task/2542). Only agent_id on this
        # payload can say the caller is a subagent (see actors.SIDECHAIN_RULE);
        # the beacon process it would start inherits the seat's name and so
        # passes every identity door. The key is read before the import so a
        # main-thread call, which carries no agent_id, pays nothing for it.
        # THE SIDECHAIN AUTHORITY RUNG rides the same key (task/3060): a
        # delegate may not run a verdict-class write unless its seat granted
        # it (`_sidechain_authority`, helm/delegate_grant.py).
        granted = []
        if d.get("agent_id"):
            from . import actors
            if actors.sidechain_agent(d) and sidechain_beacon_presence(cmd):
                print("[helm argv-guard] BLOCKED: %s"
                      % actors.sidechain_beacon_refusal(), file=sys.stderr)
                return 2
            if actors.sidechain_agent(d):
                refusal, granted = _sidechain_authority(d, cmd)
                if refusal:
                    print(refusal, file=sys.stderr)
                    return 2
        act = github_actions_refusal(command=cmd)
        if act is not None:
            print(github_actions_message(act), file=sys.stderr)
            return 2
        # THE ENV-DUMP RUNG, Bash and Monitor alike (task/3037): a command
        # whose output would carry the environment, a secret-looking
        # variable or a credentials file into the transcript.
        leak = env_dump_refusal(cmd)
        if leak is not None:
            print(env_dump_message(leak), file=sys.stderr)
            return 2
        # THE SHARED-CHECKOUT RUNG, Bash and Monitor alike (task/3057): a
        # working-tree git verb whose directory is the shared checkout.
        tree = shared_checkout_refusal(cmd, d.get("cwd"))
        if tree is not None:
            print(shared_checkout_message(tree), file=sys.stderr)
            return 2
        if tool != "Bash":
            _tree_advise(d, cmd, first=granted)
            return 0
        blocked = argv_guard(cmd)
        if not blocked:
            # THE ACT DENIES (helm/actsteer.py): a self-matching `pkill -f`
            # and an AI authoring line in a gh PR or issue body. Each must not
            # run as written, and a steer on either arrives after the damage.
            # The substring gate keeps every other call off the import.
            if "pkill" in cmd or "pgrep" in cmd or "gh " in cmd:
                from . import actsteer
                deny = actsteer.refusal(cmd, d.get("cwd"))
                if deny:
                    print(deny, file=sys.stderr)
                    return 2
            # STEERS RIDE THE PASS PATH ONLY. A blocked command never runs,
            # so its cure is the only thing worth saying; stacking advice on
            # top of a refusal buries the refusal. Advisory always: printed,
            # then exit 0, because a redirection that can stop your work is a
            # gate wearing the wrong name.
            #
            # SAID WHERE THE AGENT READS IT. Stderr from a hook that exits 0
            # reaches the debug log and nobody else, so a steer printed there
            # spends its once-per-session latch on telling no one. The lines
            # ride the pass path's one envelope (see `advise`).
            _tree_advise(d, cmd, first=granted + [
                (sid, "[helm steer] " + text)
                for sid, text in argv_steers(cmd, d.get("cwd"),
                                             d.get("tool_input"))])
            return 0
        cure, why = blocked
        # three cures, one law: the block must name the safe route for ITS
        # surface — an undiscoverable safe path is no safe path.
        #
        # THE SAFE-ROUTE SHELL IS THE PAYLOAD and is verbatim. What left each
        # of these is the measured anecdote — the FIX verdict that lost its
        # backticked command, the commit that landed reading "the assignment
        # test is , and", the backticked git clean that ran in the shared
        # checkout. Each is recorded in this module's own docstrings, where it
        # belongs: it explains the rung to an editor, and a reader mid-refusal
        # needs the route, not the history.
        if cure == "verdict":
            print("[helm argv-guard] BLOCKED: %s. The evidence would be "
                  "mangled and the substitution runs on your box. Put it in a "
                  "variable through a quoted heredoc, then pass the "
                  "variable:\n"
                  "  evidence=$(cat <<'EOF'\n  ...evidence...\nEOF\n  )\n"
                  "  helm dispatch verdict ID TIP --fix \"$evidence\"\n"
                  "or single-quote evidence that needs no apostrophes."
                  % why, file=sys.stderr)
            return 2
        if cure == "commit":
            print("[helm argv-guard] BLOCKED: %s. The landed message would be "
                  "mangled and the substitution runs on your box. Use the "
                  "file route — write the message with the Write tool or a "
                  "quoted heredoc, then:\n"
                  "  git commit -F <file>\n"
                  "or single-quote a message that needs no apostrophes."
                  % why, file=sys.stderr)
            return 2
        print("[helm argv-guard] BLOCKED: %s. The message body would be "
              "mangled and the substitution runs on your box. Pipe stdin, or "
              "quote the heredoc delimiter:\n"
              "  helm chat post --room R <<'EOF'\n  ...body...\n  EOF\n"
              "or single-quote a body that needs literal backticks."
              % why, file=sys.stderr)
        return 2
    except Exception:
        return 0                       # fail-open, always


def log_flush(rooms=None, report=None):
    """Append delivered history to <helm-home>/helm/journal/chat-<date>.log.
    OUT-OF-BAND ONLY (exit-time / operator / cron) — never from send/read
    (premise a2a-ram-only-disk-log-after). Idempotent: a per-room high-water
    mark (row count + tail fingerprint); a rotation under the mark reconciles
    against the fingerprint and logs a loud gap note when history was lost
    before a flush. HELM_CHAT_LOG=0 disables (returns -1); else returns rows
    appended."""
    if log_disabled():
        return -1
    # Lifecycle truth flushes BEFORE lossy rendered rows, and its copy stays
    # WHOLE: the meld lifecycle journal is the coherent state machine, so a
    # partial flush would diverge durable-from-RAM by construction. Import is
    # local to avoid the chat↔meld module cycle; no meld verb itself touches
    # disk.
    #
    # THE ORDERING IS RIGHT AND ITS FAILURE BRANCH WAS NOT. This call used to
    # run bare, so ONE diverged meld raised here and discarded the flush of ALL
    # 156 rooms — including the room the whole fleet coordinates in. Nobody
    # chose that trade; it fell out of the ordering. Measured 2026-08-11: a
    # 3-minute timer fired ~160 consecutive times and wrote ZERO rows for eight
    # hours while 110 of 156 rooms were flushable the entire time.
    #
    # The coherence rule is UNCHANGED: a meld whose lifecycle cannot flush must
    # not get a partial rendered-row flush either, so its room is skipped below
    # by name. Only the blast radius changes.
    from . import meld
    quarantined = {}
    lifecycle_down = None
    try:
        _lc_appended, quarantined = meld.flush_lifecycle(rooms=rooms)
    except Exception as exc:
        # A GLOBAL lifecycle precondition failed (the RAM directory itself is
        # unknown), so NO meld room can be mirrored. That is a reason to skip
        # every meld room — a per-room name cannot express it, and naming a
        # sentinel key here would skip nothing, because the loop below matches
        # real room names. It is NEVER a reason to drop the ordinary chat rooms
        # whose rows are the fleet's actual coordination record.
        lifecycle_down = str(exc) or exc.__class__.__name__
    if rooms is None:
        rooms = list_rooms()
    else:
        # dm-* was never journaled; meld-* rooms now flush like any room —
        # the 2026-08-02 reboot proved a meld's CONTENT is the work product
        # the fleet loses, not clutter.
        rooms = [r for r in rooms if not r.startswith("dm-")]
    state = pk.read_json(_flush_state_path(), {}) or {}
    lines = []
    appended = 0
    stranded = 0
    flushed_rooms = 0
    for room in rooms:
        # HONOUR THE COHERENCE RULE BY NAME. A meld whose lifecycle could not be
        # mirrored gets no partial rendered-row flush; when the lifecycle leg is
        # down globally, that applies to EVERY meld room, because none of them
        # were mirrored.
        why = quarantined.get(room)
        if why is None and lifecycle_down and room.startswith("meld-"):
            why = lifecycle_down
        if why is not None:
            quarantined[room] = why
            try:
                rows, _total = read(room)
                st = state.get(room) or {}
                stranded += max(0, len(rows) - (st.get("n") or 0))
            except Exception:
                # COUNTING the stranded rows must never be able to strand the
                # rest. read() raising is the very failure this function now
                # isolates, so calling it unguarded inside the quarantine branch
                # would resurrect the whole-batch abort inside its own cure. The
                # count is diagnostic; the flush is not.
                pass
            continue
        try:
            appended += _flush_one_room(room, state, lines)
            # COUNT THE CALLS THAT SUCCEEDED. Anything derived from set sizes is
            # a guess about this line; see the report block below.
            flushed_rooms += 1
        except Exception as exc:
            # ONE UNREADABLE ROOM MUST NOT DISCARD THE ROOMS AFTER IT. This loop
            # had no per-room guard, so any read() or fingerprint failure carried
            # the identical blast radius as the lifecycle leg — the whole batch,
            # silently, on a timer nobody was reading.
            quarantined[room] = str(exc) or exc.__class__.__name__
    if lines:
        os.makedirs(journal_dir(), exist_ok=True)
        path = os.path.join(journal_dir(),
                            "chat-%s.log" % time.strftime("%Y-%m-%d"))
        with open(path, "a", encoding="utf-8") as f:
            f.write("".join(x + "\n" for x in lines))
        pk.write_json(_flush_state_path(), state)
    if report is not None:
        # A CALLER COULD NOT TELL "nothing new" FROM "156 ROOMS REFUSED" — both
        # returned 0. The count stays an int for every existing caller; the
        # refusal detail rides here, so the verb and the watchdog can say which
        # rooms are stranded and how many rows are sitting in RAM only.
        report["quarantined"] = quarantined
        report["stranded_rows"] = stranded
        # COUNTED, NEVER INFERRED. This read `len(rooms) - len(quarantined)`,
        # which is a SUBTRACTION OF TWO DIFFERENT POPULATIONS: `rooms` is the
        # chat rooms this run walked, while `quarantined` also carries melds the
        # LIFECYCLE leg refused — and `_lifecycle_rooms()` enumerates the meld
        # store, not `list_rooms()`, so a meld whose chat room is gone (or which
        # never had one) is in the second set and not the first. The difference
        # could therefore go NEGATIVE, and a negative int is TRUTHY, so
        # `flush_outcome`'s `if quarantined and not flushed` — the branch that
        # says "nothing reached disk" — would read a total outage as merely
        # degraded. It could equally be INFLATED, crediting a room the loop
        # never touched. Both readings are guesses about a line one counter
        # answers exactly.
        report["flushed_rooms"] = flushed_rooms
        # THE LIFECYCLE LEG FAILING IS ITS OWN FACT, not merely the cause of a
        # room quarantine. My first arm asserted quarantined was non-empty on a
        # global failure and it was {} — correctly, because that fixture had no
        # meld room to skip. A caller must still learn the leg is down: an empty
        # quarantine after a lifecycle failure means "nothing needed skipping",
        # never "nothing went wrong", and those must not render identically.
        report["lifecycle_down"] = lifecycle_down
    return appended


class FlushFault(Exception):
    """A room whose READ cannot be trusted, so its cursor must not move.

    The caller quarantines it exactly like any other per-room failure — the
    point is that this failure had NO exception to catch."""


def _flush_one_room(room, state, lines):
    """Render ONE room's new rows into `lines` and advance its high-water mark.
    Raises on a room whose read is not trustworthy; the caller quarantines it
    and continues."""
    # read() FAILS OPEN AND THIS CALLER CANNOT AFFORD IT. Its contract is right
    # for the live tail it was written for — an unreadable room answers ([], 0)
    # exactly as an empty one does, and a torn line is dropped in silence — and
    # both are DATA LOSS WEARING A SUCCESS here, because this function's other
    # job is to ADVANCE THE HIGH-WATER MARK.
    #
    # Work it through on an unreadable room: rows == [], `new` is empty, and the
    # `room not in state` clause then writes {"n": 0, "tail": None}. The run
    # reports the room FLUSHED, the watchdog stays green, and the rows it never
    # read are neither on disk nor named as stranded. On a TORN room it is
    # worse, because the loss is silent AND partial: the unparseable line is
    # dropped, the rows around it render, and the mark advances to
    # len(parsed_rows) — past a row that a repair of those bytes could still
    # have recovered. That is precisely the class this whole row was filed for,
    # rebuilt one level down: a mechanism reporting success over rows nobody has.
    #
    # read_checked is the strict door that already exists for this question. A
    # MISSING room is still not a fault (a room nobody posted to is PROVEN
    # empty, and `--room ghost` must stay a clean no-op); only a room that
    # exists and does not read is one.
    rows, _total, fault = read_checked(room)
    if fault:
        raise FlushFault(
            "%s — room read is not trustworthy, so the flush cursor stays put "
            "(rows kept in RAM, repairable)" % fault)
    st = state.get(room) or {}
    n, tail = st.get("n", 0), st.get("tail")
    start = 0
    if n and n <= len(rows) and _fp(rows[n - 1]) == tail:
        start = n
    elif n:  # the room rotated (or was replaced) under the mark
        idx = next((i for i in range(len(rows) - 1, -1, -1)
                    if _fp(rows[i]) == tail), None)
        if idx is not None:
            start = idx + 1
        elif rows:
            lines.append("# %s [%s] rotation gap — some rows were dropped "
                         "before this flush; re-logging the surviving room"
                         % (pk.now_ts(), room))
    new = rows[start:]
    lines.extend("%s [%s] %s" % (m.get("ts") or "?", room, _fmt_body(m))
                 for m in new)
    if new or room not in state:
        state[room] = {"n": len(rows), "tail": _fp(rows[-1]) if rows else None}
    return len(new)


# ---------------------------------------------------------------------------
# THE DURABILITY WATCHDOG — a flush that keeps failing must WAKE somebody
# ---------------------------------------------------------------------------
#
# MEASURED 2026-08-11: helm-chat-logflush.timer was enabled and fired faithfully
# every ~3 minutes, and wrote ZERO rows for EIGHT HOURS — roughly 160 consecutive
# failures without one alert. Every row of the fleet's morning chat existed in
# RAM only, with the owner about to restart the machine.
#
# Per-room isolation (above) removes the CAUSE of that outage and nothing about
# the next one, because the defect underneath is separate and worse: the
# mechanism could not observe its own silence. So loudness is its own leg with
# its own failure mode.
#
# TWO THINGS ARE DELIBERATE HERE.
#
# 1. A RUN THAT RAISES NOTHING CAN STILL HAVE FAILED. After isolation, a flush
#    that quarantines every room returns 0 and raises nothing — indistinguishable
#    from "no new rows" to every caller that reads only the int. The classifier
#    below reads the report, not the exception.
#
# 2. THE ALARM IS A DELIVERED MESSAGE. Not a log line: the timer's stdout goes to
#    a journal nobody reads, and it already printed ~160 times. Not a state
#    field: `.chat-flush.json` WAS a state field, eight hours stale, read by
#    nothing. The second consecutive failure DMs an obligated seat.

# ONE failed run can be a room mid-rotation. TWO IN A ROW IS A MECHANISM — and
# at a 3-minute cadence the second one lands ~6 minutes into an outage that
# previously ran eight hours.
FLUSH_ALERT_STREAK = 2

# How far behind its own timer a journal may fall before a reader is told, as
# MULTIPLES OF LOGFLUSH_INTERVAL_S — beside the cadence they multiply, so the
# two can never drift apart, and read by every surface rather than restated in
# each one. Three missed runs is a warning (one is a slow box); twenty is an
# hour, and the measured outage ran eight.
FLUSH_STALE_WARN_INTERVALS = 3
FLUSH_STALE_FAIL_INTERVALS = 20

# Who signs the alarm. A distinct name so the row is filterable and so the
# alert never reads as a teammate's opinion.
FLUSH_WATCHDOG_WHO = "chat-durability"

# The fleet-ops room, not #main: every seat homed to #main would be woken by a
# row addressed to the integrator (owner ruling 2026-07-24, via silent-drop).
FLUSH_ALERT_ROOM = "helm"


def _flush_health_path():
    """Watchdog state — beside the journal directory, NEVER inside it.

    restore-journal parses everything it finds under journal_dir(), so a
    watchdog that wrote its bookkeeping there could feed its own alarm text
    back into the history it exists to protect.
    """
    return os.path.join(home.project_dir("helm"), ".state",
                        "chat-flush-health.json")


def flush_outcome(appended, report, error=None):
    """Classify ONE flush run -> (state, reason). state is one of:

        "ok"        every room that had rows got them onto disk
        "degraded"  some rooms flushed, some were refused — rows are stranded
        "failed"    nothing reached disk, or the meld lifecycle leg is down
        "off"       HELM_CHAT_LOG disables the leg; the operator chose it

    THE POINT OF A CLASSIFIER RATHER THAN A try/except: after per-room
    isolation the eight-hour outage would return 0 and raise NOTHING. `0` is
    also what a healthy quiet fleet returns. A watchdog reading the int alone
    is blind to precisely the incident it was built for.

    `lifecycle_down` is "failed" and not "degraded" even when ordinary rooms
    flushed fine, because when that leg is down NO meld room can be mirrored —
    every one of them is undurable for as long as it lasts, and today there is
    a detector and no cure, so it stays that way until a human acts.
    """
    report = report or {}
    if error is not None:
        return "failed", ("log-flush raised: %s"
                          % (str(error) or error.__class__.__name__))
    if appended is not None and appended < 0:
        return "off", "log-flush disabled (HELM_CHAT_LOG=%s)" % (
            home.env("CHAT_LOG") or "")
    down = report.get("lifecycle_down")
    if down:
        return "failed", ("meld lifecycle leg DOWN (%s) — no meld room can be "
                          "mirrored while it is" % down)
    quarantined = report.get("quarantined") or {}
    flushed = report.get("flushed_rooms") or 0
    if quarantined and not flushed:
        return "failed", ("all %d room%s refused — nothing reached disk"
                          % (len(quarantined), "s"[:len(quarantined) != 1]))
    if quarantined:
        return "degraded", ("%d room%s refused (%s) — %d row%s stranded in RAM"
                            % (len(quarantined),
                               "s"[:len(quarantined) != 1],
                               _first_names(quarantined),
                               report.get("stranded_rows") or 0,
                               "s"[:(report.get("stranded_rows") or 0) != 1]))
    return "ok", ""


def _first_names(names, cap=3):
    """Name the refused rooms, capped — a 46-room list in a chat alert buries
    the sentence that says what to do about it."""
    got = sorted(names)
    head = ", ".join(got[:cap])
    return head + (" +%d more" % (len(got) - cap) if len(got) > cap else "")


def flush_watchdog(appended, report, error=None, post_alert=True, now=None):
    """Record ONE flush outcome and DELIVER an alert on the second consecutive
    failure. Returns {state, reason, streak, fired, delivered, recovered}.

    RE-ALERT ON DOUBLING, not every run. The measured incident was ~160
    consecutive failures; alerting each time is 160 wakes and teaches the fleet
    to filter the alarm, while alerting once leaves an eight-hour outage
    represented by a single six-minute-old message. Doubling gives 2, 4, 8, 16,
    32, 64, 128 — seven messages across those eight hours, each one louder than
    the last by construction.

    THE ALERTED MARK IS ONLY WRITTEN WHEN A MESSAGE ACTUALLY LANDED. Recording
    "alerted" for an alert that failed to deliver would build this row's own
    defect inside its cure: a state field claiming somebody was told.
    """
    state, reason = flush_outcome(appended, report, error)
    if state == "off":
        # The operator turned the leg off. Not a failure, and not a success
        # either — leaving the streak untouched keeps a disable from laundering
        # a real failure streak into health. doctor reports the disable itself.
        return {"state": state, "reason": reason, "streak": 0, "fired": False,
                "delivered": False, "recovered": False}
    now = now if now is not None else time.time()
    path = _flush_health_path()
    st = pk.read_json(path, {}) or {}
    prior_streak = st.get("streak") or 0
    prior_alerted = st.get("alerted_streak") or 0
    prior_alerted_state = st.get("alerted_state")
    report = report or {}
    if state == "ok":
        st = {"state": "ok", "streak": 0, "reason": "", "last_ok": now,
              "since": None, "alerted_streak": 0, "alerted_state": None,
              "quarantined": [], "stranded_rows": 0}
    else:
        st["state"] = state
        st["streak"] = prior_streak + 1
        st["reason"] = reason
        st["since"] = st.get("since") or now
        st["alerted_streak"] = prior_alerted
        st["quarantined"] = sorted(report.get("quarantined") or {})
        st["stranded_rows"] = report.get("stranded_rows") or 0
    st["last_run"] = now
    # A CHRONIC CONDITION MUST NOT BUY SILENCE FOR A TOTAL ONE — the rate
    # limiter's own hazard, found by re-reading it against the incident it was
    # written for. Doubling is right for ONE condition persisting and exactly
    # wrong across an ESCALATION: a room quarantined for two days pushes the
    # next scheduled alert two days out, so the lifecycle leg going fully down
    # inside that window would be silent for two days. That is the eight-hour
    # silence this whole row exists to kill, rebuilt inside its own alarm. A
    # WORSENING therefore fires on its own next run, on the schedule's floor
    # and never on its ceiling. The reverse — failed easing back to degraded —
    # is an improvement and stays quiet.
    worsened = state == "failed" and prior_alerted_state not in (None, "failed")
    fired = st["streak"] >= FLUSH_ALERT_STREAK and (
        worsened or st["streak"] >= max(FLUSH_ALERT_STREAK, prior_alerted * 2))
    recovered = state == "ok" and prior_alerted > 0
    delivered = False
    if fired and post_alert:
        delivered = _deliver_flush_alert(_flush_alert_text(st, now))
        if delivered:
            st["alerted_streak"] = st["streak"]
            st["alerted_state"] = state
            st["alerted_at"] = now
        # AN UNDELIVERED ALERT IS NOT AN ALERT. Leaving alerted_streak at its
        # prior value makes the very next run try again, which is what a
        # durability alarm owes: the transport being down is not a reason for
        # the fleet to stop being told its record is in RAM only.
    if recovered and post_alert:
        delivered = _deliver_flush_alert(
            "CHAT DURABILITY RESTORED: log-flush succeeded again after %d "
            "consecutive failure%s. The rows that were in RAM only are on "
            "disk." % (prior_streak or prior_alerted,
                       "s"[:(prior_streak or prior_alerted) != 1]))
    pk.write_json(path, st)
    return {"state": state, "reason": reason, "streak": st["streak"],
            "fired": bool(fired), "delivered": delivered,
            "recovered": bool(recovered)}


def _dur(seconds):
    """Seconds -> a duration a human reads at a glance ("8h", not "28800s").

    Local and private on purpose: helm carries a dozen private `_age` copies and
    none of them is a shared door, so importing one module's private formatter
    into another would couple two subsystems for a string. Consolidating them is
    real work and belongs to its own row, not to a P0 durability alarm.
    """
    s = max(0, int(seconds))
    for cut, div, unit in ((120, 1, "s"), (7200, 60, "m"),
                           (172800, 3600, "h")):
        if s < cut:
            return "%d%s" % (s // div, unit)
    return "%dd" % (s // 86400)


def _flush_alert_text(st, now):
    """The message an obligated seat wakes to. OUTCOME FIRST — what is at risk
    for the owner — then the age, then the one command that shows the refusal.

    THE STRANDED COUNT IS ONLY STATED WHEN IT IS NON-ZERO. My first draft said
    "0 chat rows live in RAM ONLY and a reboot loses them" on a genuine
    lifecycle-down run, because that fixture had no meld room to strand — a
    false sentence in the alarm's loudest position, which is how an alarm earns
    a filter. A leg that is down with nothing stranded YET is still an alarm;
    it just is not that one.
    """
    since = st.get("since")
    streak = st.get("streak") or 0
    stranded = st.get("stranded_rows") or 0
    at_risk = ((" %d row%s stranded in RAM RIGHT NOW."
                % (stranded, "s"[:stranded != 1])) if stranded else "")
    return ("CHAT DURABILITY FAILING: log-flush has failed %d consecutive "
            "run%s over %s. %s.%s Chat lives in RAM (%s) and the journal is its "
            "ONLY durable copy, so whatever it cannot flush dies with the "
            "machine. `helm chat log-flush` shows the refusal, `helm doctor` "
            "shows how far behind the journal is."
            % (streak, "s"[:streak != 1], _dur(now - since) if since else "?",
               st.get("reason") or "no reason recorded", at_risk, chat_dir()))


def _obligated_seat():
    """The seat this alarm is owed to, named in ONE place fleet-wide.

    dispatches._default_lander IS that place (HELM_LANDER, else the folding
    seat) and its docstring records the hardcode rung catching the second copy
    of the literal. This is where the third copy would have gone.
    """
    try:
        from . import dispatches
        return dispatches._default_lander()
    except Exception:
        return os.environ.get("HELM_LANDER") or ""


def _deliver_flush_alert(text):
    """Put the alarm in front of somebody who is ACTUALLY THERE.

    A DM "succeeds" for a seat that never joined — seats.dm is fail-open by
    design, and rightly so for ordinary mail, which waits in a lane until its
    seat arrives. For an ALARM that success return is the founding defect
    wearing a green light: a message parked in a dead seat's lane has woken
    nobody, and the whole row exists because a mechanism could not tell those
    apart. So MEMBERSHIP picks the channel:

      JOINED  -> DM. A real addressee, and the delivery lane wakes it.
      ABSENT  -> the roster says nobody holds that name. Post the alert
                 ADDRESSED into #helm instead, where the seats homed there
                 read it — never into a lane with no reader.
      UNKNOWN -> BOTH. An empty or unreadable roster is not evidence the seat
                 is gone (ABSENT and UNKNOWN are different verdicts and helm
                 hands them over separately for exactly this reason), and
                 over-waking is recoverable where silence is not.

    #helm and not #main: a row addressed to the integrator would otherwise wake
    every seat homed to #main (owner ruling 2026-07-24, via silent-drop).

    Returns True when a message actually landed. NEVER RAISES — a durability
    alarm that can crash the flush would take out the thing it guards, from a
    systemd oneshot where the crash is one more line in the unread journal.

    THE RETURN IS WHAT MARKS THE STREAK "alerted", so it decides whether the
    next run retries — and it may only ever be True on POSITIVE EVIDENCE. The
    UNKNOWN branch used to answer `_post_alert(...) or dm_landed`, which is the
    founding defect of this entire row rebuilt inside the alarm built to cure
    it: the roster could not say whether the seat exists, `seats.dm` is
    FAIL-OPEN and returns a row for a lane nobody holds, and that fail-open
    success then stood in for the #helm leg it was explicitly paired with
    BECAUSE the DM might reach nobody. One failed room post and the alarm marked
    itself delivered on the strength of the channel it did not trust.
    Membership UNKNOWN therefore requires the ROOM leg's own answer.

    A DM is sufficient evidence in exactly one case, and it is above this line:
    the roster POSITIVELY said JOINED. Everywhere else the room is the proof.
    """
    to = _obligated_seat()
    membership = _alert_membership(to)
    if to and membership != "ABSENT":
        # UNKNOWN still DMs — over-waking is recoverable, silence is not — the
        # send is simply no longer allowed to answer "was anybody told?".
        if _dm_alert(to, text) and membership == "JOINED":
            return True            # a live addressee has it; one message is enough
    return _post_alert(to, text)


def _alert_membership(to):
    """"JOINED" | "ABSENT" | "UNKNOWN" for the alarm's addressee. An
    unreadable roster is UNKNOWN and never ABSENT: refusing the DM leg on
    absence of evidence is how a live seat stops being told."""
    if not to:
        return "ABSENT"
    try:
        from . import seats
        return seats.recipient_capability(to)["membership"]
    except Exception:
        return "UNKNOWN"


def _dm_alert(to, text):
    try:
        from . import seats
        sent, why = seats.dm(to, text, who=FLUSH_WATCHDOG_WHO)
        # BOTH LEGS MUST AGREE. dm's contract is (row, None) or (None, reason);
        # reading any non-None row as delivery would suppress the room leg
        # while reporting success — a silent wake-path loss inside the cure for
        # silent wake-path loss.
        return sent is not None and why is None
    except Exception:
        return False


def _post_alert(to, text):
    """The room leg. ADDRESSED when there is a name to address, and a plain row
    when there is not — never a literal "@" with nothing after it, which is the
    unaddressed post the whole mechanism exists to avoid. Seats homed to #helm
    are woken by an ordinary row either way."""
    try:
        return post(("@%s %s" % (to, text)) if to else text,
                    room=FLUSH_ALERT_ROOM, who=FLUSH_WATCHDOG_WHO) is not None
    except Exception:
        return False


def flush_health(now=None):
    """Every durability fact a reader needs in ONE read — the surface that did
    not exist while the cursor sat EIGHT HOURS behind a THREE-MINUTE timer.

    `age_s` IS "HOW LONG SINCE ANYTHING REACHED DISK", which is deliberately
    NOT "how long since a healthy run". Two facts feed it and each covers the
    other's blind spot: the cursor's mtime only moves when rows were actually
    written, so a quiet fleet would age it legitimately and a threshold on it
    alone would cry wolf every night; `last_ok` only moves on a clean run, so
    it alone would read as a stall on a fleet whose flushes are partial but
    real. Whichever is newer answers the question a reader is asking — is the
    record on disk — and the streak answers the other one.

    -> {disabled, state, streak, reason, quarantined, stranded_rows,
        last_ok, cursor_ts, last_run, age_s, interval_s}
    age_s is None when nothing here has ever flushed — "never ran" and "ran an
    hour ago" are different sentences to a human and must not render alike.
    """
    now = now if now is not None else time.time()
    st = pk.read_json(_flush_health_path(), {}) or {}
    try:
        cursor_ts = os.path.getmtime(_flush_state_path())
    except OSError:
        cursor_ts = None
    fresh = max([t for t in (st.get("last_ok"), cursor_ts) if t] or [0])
    return {"disabled": log_disabled(),
            "state": st.get("state") or "unknown",
            "streak": st.get("streak") or 0,
            "reason": st.get("reason") or "",
            "quarantined": st.get("quarantined") or [],
            "stranded_rows": st.get("stranded_rows") or 0,
            "last_ok": st.get("last_ok"), "cursor_ts": cursor_ts,
            # WHEN THE TIMER LAST RAN AT ALL, which is a different question from
            # when it last worked — a masked or uninstalled unit leaves the
            # streak frozen at zero forever, so an unread `last_run` would have
            # been a write-only field on the one state that answers it.
            "last_run": st.get("last_run"),
            "age_s": (now - fresh) if fresh else None,
            "interval_s": LOGFLUSH_INTERVAL_S}


_LOGFLUSH_UNIT_SERVICE = """[Unit]
Description=helm chat durable log-flush (RAM->disk write-behind for a2a chat)
Documentation=premise:a2a-ram-only-disk-log-after

[Service]
WorkingDirectory=%(cwd)s
Type=oneshot
# Idempotent (per-room high-water mark); appends delivered rows OUT-OF-BAND to
# <helm-home>/helm/journal/chat-<date>.log so a machine reboot (tmpfs wipe of
# /dev/shm/helm-chat) never loses more than one interval of chat. A dregg
# restart/reseed does not touch chat at all (separate tmpfs).
ExecStart=%(helm)s chat log-flush
Nice=10
"""

_LOGFLUSH_UNIT_TIMER = """[Unit]
Description=periodic helm chat log-flush (bounds reboot-loss of tmpfs a2a chat)

[Timer]
OnBootSec=%(boot)ss
OnUnitActiveSec=%(interval)ss
Persistent=true

[Install]
WantedBy=timers.target
"""

# Installed is truth: these mirror the INSTALLED helm-chat-logflush.timer
# (OnBootSec=2min, OnUnitActiveSec=3min) because the write-behind cadence is
# operational — code updates to match the machine, never the reverse (ruled
# 2026-08-04, #195; supersedes the 30s that never reached the box).
LOGFLUSH_BOOT_DELAY_S = 120
LOGFLUSH_INTERVAL_S = 180


def _logflush_timer_units(interval=LOGFLUSH_INTERVAL_S,
                          boot=LOGFLUSH_BOOT_DELAY_S):
    # Same two laws as autocompact's units: never capture a worktree's helm
    # (a persistent unit outlives the lane), and WorkingDirectory is DERIVED
    # (find_root folds a lane worktree back to the shared checkout) — a
    # literal operator path in a tracked template is a never-track needle.
    helm_bin = os.path.join(os.path.expanduser("~"), ".local", "bin", "helm")
    from . import work
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cwd = work.find_root(here) or here
    service = _LOGFLUSH_UNIT_SERVICE % {"helm": helm_bin, "cwd": cwd}
    timer = _LOGFLUSH_UNIT_TIMER % {"interval": interval, "boot": boot}
    udir = os.path.join(os.path.expanduser("~"), ".config", "systemd", "user")
    return (os.path.join(udir, "helm-chat-logflush.service"), service,
            os.path.join(udir, "helm-chat-logflush.timer"), timer)


def _logflush_install_timer(args):
    interval = LOGFLUSH_INTERVAL_S
    if "--interval" in args:
        try:
            interval = int(args[args.index("--interval") + 1])
        except (ValueError, IndexError):
            print("helm chat log-flush: --interval wants seconds",
                  file=sys.stderr)
            return 2
    if interval < 1:
        print("helm chat log-flush: --interval must be at least 1 second",
              file=sys.stderr)
        return 2
    spath, service, tpath, timer = _logflush_timer_units(interval)
    if "--apply" not in args:
        print("# %s\n%s\n# %s\n%s" % (spath, service, tpath, timer))
        print("# install:\n#   helm chat log-flush --install-timer --apply\n"
              "# or write the two files above, then:\n"
              "#   systemctl --user daemon-reload && "
              "systemctl --user enable --now helm-chat-logflush.timer")
        return 0
    for path, body in ((spath, service), (tpath, timer)):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(body)
    import subprocess
    rc = 0
    for argv in (("daemon-reload",),
                 ("enable", "--now", "helm-chat-logflush.timer")):
        p = subprocess.run(["systemctl", "--user"] + list(argv),
                           capture_output=True, text=True)
        if p.returncode:
            print("helm chat log-flush: systemctl %s FAILED — %s"
                  % (" ".join(argv), (p.stderr or p.stdout).strip()),
                  file=sys.stderr)
            rc = 1
    if not rc:
        print("helm chat log-flush: timer installed at %ds — %s"
              % (interval, tpath))
    return rc


def _fmt_body(m):
    """The log line's tail: sender + content + signature note, full fidelity —
    including a reply's parent pointer, so the durable journal never loses the
    thread the RAM room showed."""
    tag = (" {chain %s}" % m["chain"]) if m.get("chain") is not None \
        else _transport_tag(m)
    # the journal is a terminal sink: `cat chat-<date>.log` renders these lines,
    # so the IDENTITY columns (from/tfrom/rfrom) are _dsan-laundered against a
    # planted/foreign row's ESC/bidi. The message TEXT is left full-fidelity by
    # law (voice pastes, emoji, U+2028) — same split as _fmt / _fmt_body's
    # sibling render.
    if m.get("react"):
        return "%s %s %s -> %s@%s%s" % (_dsan(m.get("from") or "?"),
                                        "un-reacted" if m.get("un") else "reacted",
                                        m["react"], _dsan(m.get("tfrom") or "?"),
                                        m.get("tts") or "?", tag)
    ref = (" ↳%s@%s" % (_dsan(m.get("rfrom") or "?"), m.get("rts") or m.get("reply_to")
                        or "?")) if is_reply(m) else ""
    event = " {event-id:%s}" % m["id"] if m.get("event") and m.get("id") else ""
    return "%s%s: %s%s%s" % (_dsan(m.get("from") or "?"), ref,
                              m.get("text") or "", tag, event)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

SEAT_VERBS = ("join", "deliver", "delegation-stop", "stop-guard", "wait",
              "seats", "seat", "dm",
              "ack", "pending", "receipts", "status", "claim", "release", "claims",
              "verdict", "reveal", "council-status", "council-abort",
              "catchup")
                                               # the delivery lane — seats.py.
                                               # the four council verbs (the
                                               # 0.3 deferral, CASHED): signal
                                               # / quorum-gated open / the
                                               # count-without-names tally /
                                               # the permanent-seal escape

# Per-verb usage, answered at the DISPATCHER before any verb runs. The class
# this closes (live-probed 2026-07-22): a chat verb that CONSUMES --help
# without answering it — `wait --help` and `read --follow --help` blocked
# forever (the loop started before any help check; a fresh seat probing the
# mandatory-first-action verb stalled to harness timeout, rc 124, zero
# output), and join/deliver/claim/log-flush DID WORK under --help (`claim
# --help` leased a resource literally named "--help"). Same family as
# lane/help-honest's silent-dispatcher class; that lane's guard_tail lives in
# cli.py's root verbs only, so the chat tree answers here — one gate covers
# seats.py, chatnode.py and meld.py without duplicating its machinery.

def _node_usage():
    """`chat node`'s help text, read from the subcommand that owns it.

    LAZY-SAFE AT MODULE LEVEL: every `from . import chat` inside `chatnode` is
    function-local, so importing it from here cannot close a cycle. That is a
    measured property of that file, not an assumption, and if it ever changes
    the fallback below still renders — a pointer to where the list lives,
    never a copy of it.

    THE FALLBACK NAMES NO VERBS, and that is deliberate rather than lazy. A
    hand-copied list here is a THIRD copy of the same list — it reads as a
    safety net while being one more thing to forget, and its failure mode is
    the exact one this derivation exists to end: a help entry confidently
    advertising a set the dispatcher no longer serves. A degraded path that
    says "I could not read the list, here is where it lives" is honest at
    every future moment; a degraded path carrying today's list is honest only
    until somebody adds a verb.
    """
    # TWO WORLDS, TWO CATCHES, TWO SENTENCES — AND THIS FUNCTION MAY NEVER
    # RAISE IN EITHER OF THEM. It is called at MODULE IMPORT, in the `HELP`
    # dict literal below, so anything escaping here stops `helm.chat` itself
    # from importing and takes EVERY chat verb down with it, not just `node`.
    # Measured in a fresh interpreter with `helm.chatnode` unimportable: an
    # escaping import error yields ModuleNotFoundError on `from helm import
    # chat`; contained, the module imports and every other verb works.
    #
    # THE TWO FAILURES ARE DIFFERENT DIAGNOSES AND EACH GETS ITS OWN TEXT. A
    # module that could not be IMPORTED is a broken installation; a renderer
    # that RAISED is a broken verb table. They lead a reader to different
    # repairs, so collapsing them into one sentence — in either direction —
    # makes the help text wrong in whichever world it was not written about.
    try:
        from . import chatnode
    except Exception:                    # noqa: BLE001 — help must render
        # NOT A RENDERING FAULT, AND SAYING SO IS THE WHOLE VALUE HERE. The
        # module that owns the verb list could not be imported at all, which
        # is an installation problem; the reader is looking at a different
        # kind of broken than the branch below describes, and every other
        # `helm chat` verb is unaffected because this returns instead of
        # raising.
        return ("usage: helm chat node <verb>  (supervise the chat tmpfs "
                "node — helm/chatnode.py, which owns this verb list and "
                "which the subcommand dispatches, could not be IMPORTED: "
                "this is a broken installation, not a rendering fault, and "
                "the rest of `helm chat` is unaffected)")
    try:
        return chatnode._usage()
    except Exception:                        # noqa: BLE001 — help must render
        # NO COMMAND HERE, AND THAT IS THE WHOLE POINT. This branch is reached
        # exactly when rendering the verb list RAISED, so every command that
        # would print that list — `helm chat node <anything unknown>` included
        # — re-enters the renderer that just failed and raises again. A remedy
        # is a prediction about a command the reader has not run yet, and a
        # remedy routed through the broken path is false in the only world
        # where it is ever printed. So this names WHERE the list lives, as a
        # fact a reader can check with their eyes, and prescribes nothing.
        return ("usage: helm chat node <verb>  (supervise the chat tmpfs "
                "node — the verb list could not be rendered here; it is the "
                "_VERBS table in helm/chatnode.py, which is also what the "
                "subcommand dispatches)")


HELP = {
    "wait": "usage: helm chat wait [--seat S] [--room R] [--any] [--follow] "
            "(--follow belongs INSIDE a Monitor, re-armed at each 30-minute "
            "expiry — its stdout must "
            "be a pipe; a background shell writes wake-lines to a file that "
            "wakes nobody, and the beacon refuses that shape) "
            "[--replace] [--ambient] [--on-behalf] [--timeout SECONDS]\n"
            "  --on-behalf: this process declares no identity and is naming "
            "ANOTHER seat — a supervisor arming a beacon for a seat it is "
            "standing up. It drains that seat's inbox, so it must be said.\n"
            "  Block until the next word arrives for the seat (DMs, "
            "@mentions, replies, home room, @all —\n"
            "  any live room). No --timeout = wait forever; with it, rc 0 + "
            "the line on a match,\n"
            "  rc 1 on timeout. --seat declares the waiting identity "
            "(default: this session's seat).\n"
            "  --room sets the primary room (--any watches ONLY that room, "
            "every row, no cursor).\n"
            "  --follow never returns on a match: it streams each matching "
            "row as one flushed\n"
            "  line (the idle-wake beacon) and returns rc 0 only on timeout. "
            "The beacon default is\n"
            "  MENTION-ONLY: @mentions/replies/DMs/@all wake you; ambient "
            "home-room rows do NOT\n"
            "  (helm chat read covers them when you wake). --ambient restores "
            "full home-room wakes\n"
            "  for a quiet room. A bare --follow is idempotent when this exact "
            "seat+session already\n"
            "  has one live beacon with the same room/any/ambient/timeout behavior. "
            "A scope mismatch\n"
            "  is left untouched and requires --replace; --replace explicitly "
            "rotates that incumbent.",
    "join": "usage: helm chat join [--seat S] [--room R]  (register this "
            "session's seat; hooks run it on session start)",
    "deliver": "usage: helm chat deliver [--seat S] [--room R]  (drain the "
               "seat's pending rows once — the boundary hook's verb; "
               "advances the delivery cursor)",
    "delegation-stop": "usage: helm chat delegation-stop --hook-json  "
                       "(SubagentStop lifecycle hook; tombstones the exact "
                       "completed agent's delegation evidence)",
    "stop-guard": "usage: helm chat stop-guard [--seat S] [--room R] "
                  "[--detail]  (the idle gate: rc 2 lists every blocker — "
                  "unread inbox, live leases; rc 0 passes. --detail prints "
                  "the long form of the lease block and bypasses its "
                  "same-state latch — it is the flag that block's own footer "
                  "names, and it is rendering only: no read, no write, and "
                  "the verdict does not move)",
    "seats": "usage: helm chat seats [--all] [--room R]  (the roster: "
             "presence, pending, home room, todo; --all shows absent seats)",
    "seat": "usage: helm chat seat rename <sid|oldname> <newname> "
            "[--dry-run] [--alias-hours H] | "
            "mute|unmute <room> [--seat S] | mutes [--seat S] | "
            "rehome <sid|name> <room|main|none>",
    "dm": "usage: helm chat dm <seat> <text...> [--seat S]  (one private "
          "recipient — never a room)",
    "ack": "usage: helm chat ack <id> [done|blocked] [--note ...] [--seat S]\n"
           "  Mark a message ADDRESSED TO YOU as acted — done (loop closed) or "
           "blocked (--note the\n"
           "  reason). One append-only ack row on the same lane; the sender "
           "watches it leave their\n"
           "  `helm chat pending`. <id> is the row's stable id (helm chat read "
           "shows it). Only the\n"
           "  recipient may ack; a foreign or unknown id is refused; an "
           "identical repeat is a no-op.",
    "pending": "usage: helm chat pending [--seat S]\n"
               "  YOUR outbound addressed rows (DMs, @mentions) not yet "
               "CONSUMED. Acked rows drop off —\n"
               "  a stranded word is a visible object, not silent loss.\n"
               "    SENT   written, never reached their cursor\n"
               "    SEEN   surfaced, not acted\n"
               "    UNRES  no roster row answers that @name — a typo, OR a "
               "live seat that has not\n"
               "           joined. What could be RESOLVED, never whether "
               "anyone is there.\n"
               "    BASED  posted BEFORE they joined, so deliver skipped it "
               "and never reaches\n"
               "           back — they cannot ack a row nobody showed them. "
               "THIS ONE IS YOURS:\n"
               "           repost or re-address it.\n"
               "    UNKN   a read failed; the row names which input.\n"
               "  Past the display cap the header says how many are not "
               "listed.",
    "receipts": "usage: helm chat receipts <broadcast-id>[@occurrence]\n"
                "  Read the recipient-session census frozen on that broadcast "
                "row and render\n"
                "  current delivery pause, wake mute, exact-session sink "
                "eligibility, and best-effort effects\n"
                "  observed after emission. wake_succeeded and turn_executed "
                "remain UNKNOWN: helm\n"
                "  has no instrument that can prove either.",
    "claim": "usage: helm chat claim <resource> [--ttl SECONDS] [--seat S] "
             "[--lease ID to extend]  (the printed lease id confirms release; "
             "`helm chat claims` reprints your own)",
    "release": "usage: helm chat release <resource> --lease ID [--seat S]  "
               "(`helm chat claims` reprints your own lease id)",
    "claims": "usage: helm chat claims [--json]\n"
              "  The live leases across the box (one ledger per chat dir, not "
              "per room). Each row\n"
              "  carries resource, holder, seconds left and fence; rows THIS "
              "seat holds also carry\n"
              "  the lease id — the token `release` wants back. Another "
              "holder's token is never\n"
              "  shown here. --json prints the same rows machine-readably.",
    "post": "usage: helm chat post [--measured|--inferred|--unverified] "
            "<text...> [--room R] [--seat S] "
            "[--dm SEAT] [--reply-to <id|n>] [--to-integrator]\n"
            "  A post that addresses @all (or @fleet, @everyone) MUST state "
            "its basis, in\n"
            "  leading position; any other post may. The word heads the row: "
            "[MEASURED] ...\n"
            "  --to-integrator RESOLVES the integrator from the roster "
            "and prefixes the\n"
            "  mention. A literal @name is a bet that the roster still "
            "carries it, and the\n"
            "  send door accepts any token and reports success — so an "
            "escalation to a name\n"
            "  nobody holds is written, delivered nowhere, and reported "
            "as posted. When no\n"
            "  integrator resolves the body still posts, carrying "
            "UNROSTERED and the reason.\n"
            "  Leading option positions are policed, the body is never "
            "scanned: prose ABOUT\n"
            "  --help past the leading position sends fine. Bare `post "
            "--help` = this usage\n"
            "  (rc 0); `post --help <body>` refuses loudly (rc 2, nothing "
            "sent); `post --\n"
            "  <body>` sends a body that STARTS with a flag-shaped token.\n"
            "  STDIN: with NO body argument the text is read from stdin, which is\n"
            "  the SAFE path for anything containing backticks, $, or pipes — the\n"
            "  payload never becomes a shell word, so nothing can expand it:\n"
            "      helm chat post --room R <<'EOF'\n"
            "      ...body...\n"
            "      EOF\n"
            "  Composing the body as a double-quoted argument instead lets the\n"
            "  SHELL run any backticked content and splice its stdout in, silently,\n"
            "  with delivery reported OK (measured 2026-07-25).",
    "reply": "usage: helm chat reply <id|n> <text...> [--room R] [--seat S]  "
             "(id = the parent row's id, n = its 1-based number, -1 = latest)",
    "read": "usage: helm chat read [--room R] [--since N] [--limit N] "
            "[--follow] [--dm [--seat S]]  (--limit = the NEWEST N rows; "
            "--follow streams new rows until killed — it never returns on "
            "its own)",
    "react": "usage: helm chat react <n> <:shortcode:|emoji> [--room R] "
             "[--seat S]  (n counts messages, 1-based; -1 = latest; same "
             "react again toggles it off)",
    "rooms": "usage: helm chat rooms  (every room: message count, "
             "owner-unread, last row)",
    "verify": "usage: helm chat verify [--room R]  (recompute signed payload "
              "digests; rc 1 on any mismatch)",
    "restore-journal": "usage: helm chat restore-journal [--apply]  (rebuild "
                       "pre-wipe room history from the disk journal — "
                       "log-flush's inverse. The rooms are tmpfs and die with "
                       "a reboot; the journal survives. Dry-run by default; "
                       "idempotent; restored rows carry restored=1 and sit "
                       "above a provenance marker; delivery cursors are "
                       "protected (rid-suppression for existing cursors, "
                       "end-of-file mints for cursor-less seats, so a restore "
                       "can never re-deliver old mentions as new)",
    "retire-rooms": "usage: helm chat retire-rooms [--apply] [--idle-days N]"
                    "  (archive meld rooms with no post for N days, default "
                    "7, to <journal>/retired-rooms/: the raw room and its meld "
                    "snapshots are copied and fsynced first, then the room and "
                    "every cursor it carried leave the bus, and the restore "
                    "never resurrects it. Dry-run by default)",
    "catchup": "usage: helm chat catchup [--room R] [--seat S] "
               "[--including-mentions] [--apply]  (park YOUR OWN pending "
               "backlog deliberately: dry-run lists it, --apply moves your "
               "cursors to end-of-file and clears the stop-guard latch. "
               "Ambient rows park freely; a room holding rows ADDRESSED to "
               "you is refused unless --including-mentions, which also posts "
               "an in-room trace naming who parked how many asks from whom — "
               "parked rows stay readable, only delivery state moves. "
               "Self-only: another seat's backlog is never yours to park)",
    "argv-guard": "usage: helm chat argv-guard --hook-json  (PreToolUse gate "
                  "over Bash, Monitor, Write, Edit and Agent: blocks an Agent "
                  "call that names a model, since a subagent runs as the "
                  "launching seat's model whatever the flag says; blocks a "
                  "subagent's `helm chat "
                  "wait --follow` beacon arm; a Bash or Monitor command whose "
                  "TEXT names a GitHub-Actions token — the gh write verbs "
                  "(gh workflow enable/run, gh run rerun, gh's global options "
                  "allowed between them), the Actions API paths "
                  "(actions/permissions, actions/workflows/<id>/enable or "
                  "/dispatches, actions/runs/<id>/rerun) and .github/workflows "
                  "— or a Write/Edit whose file_path is under "
                  ".github/workflows/. The text is read NORMALISED "
                  "(continuations joined, quotes and backslashes dropped, "
                  "whitespace collapsed) and the rung refuses on PRESENCE, so "
                  "a mention is refused with an act and no word-splitting is "
                  "read at all, "
                  "unless HELM_ALLOW_GITHUB_ACTIONS=1 stands as a word of "
                  "that same command (a =0 anywhere takes it back); "
                  "and a messaging-verb command whose "
                  "double-quoted body carries backticks or $( — the shell "
                  "would execute them and mangle the message before helm "
                  "sees argv. Points at a quoted-delimiter stdin heredoc. "
                  "A PASSING call that names a path in another registered "
                  "project's tree while that project's native lead is live "
                  "gets one advisory line naming the project and the seat, "
                  "exit 0, once per session and project. "
                  "Fail-open on "
                  "everything else)",
    "log-flush": "usage: helm chat log-flush [--room R] [--install-timer "
               "[--interval SEC] [--apply]]  (append new rows to "
                 "the disk journal)",
    # DERIVED, NEVER RESTATED. This entry is what `helm chat node --help` and
    # `helm chat node refuel --help` print, and a hand-written copy of the
    # verb list drifts from the verbs that actually dispatch the moment one is
    # added. It did: `prepare` and `refuel` shipped and this string still said
    # up|down|status, so an operator following the dry-faucet degrade — whose
    # own cure line is `helm chat node refuel` — checked the help and was told
    # by helm that the verb it had just recommended does not exist. Measured
    # twice from opposite directions: a fresh seat hit it following a doctor
    # cure (task/2590, which fixed the SYNOPSIS one level up), and I hit it
    # running the faucet cure while the faucet was actually dry.
    #
    # `chatnode._VERBS` (the dispatch table) is the authority: `_usage()`
    # renders the text the subcommand itself prints when it refuses from that
    # table, so deriving here makes the two surfaces incapable of disagreeing
    # rather than merely equal today.
    "node": _node_usage(),
    "transport": "usage: helm chat transport status | ack --profile NAME | "
                 "ack --all  (ACK retires dead/renamed incidents; it does not "
                 "claim signing recovered)",
    # meld/council/standup HELP is populated dynamically by the MELD_VERBS
    # loop below (each spelling in its own voice) — main's static "meld" entry
    # is superseded by that restructure, so it is dropped here on purpose.
    # verdict/reveal EXIST as dispatchable verbs but are deferred to 0.3
    # (council — design doc §11): --help answers honestly with the deferral
    # instead of the bare rc-2 message the verb itself returns.
    # the 0.3 deferral is CASHED (8dab216, "council: cash the 0.3 deferral —
    # an embargoed N-of-M quorum + the honest N>3 floor"). These strings
    # advertised
    # "DEFERRED" for a full release after the verbs worked — and a reviewer
    # grepping chat.py for the feature read the stale HELP as proof it had
    # never landed, then set out to rebuild it. A lying help surface costs
    # more than a missing one: it makes a teammate delete or duplicate
    # correct work. Same class as the CLEAR|FIX vocabulary drift, same fix —
    # say what the code does.
    "verdict": "usage: helm chat verdict <room> <YES|NO|ABSTAIN> --tip <sha> "
               "[--evidence <ref>] — SEAL one member's judgment in a council. "
               "Embargoed: nothing (not even WHO signed) is visible until "
               "quorum. Identical retry is idempotent; a CONFLICTING second "
               "signal is refused. See: helm chat council-status <room>",
    "reveal": "usage: helm chat reveal <room> — lift a council's embargo once "
              "quorum is reached and print every sealed judgment with its "
              "bound tip + evidence. Below quorum it refuses (and still names "
              "no signer). An ABORTED council never reveals.",
    "council-status": "usage: helm chat council-status <room> — the tally "
                      "(N of THRESHOLD) and the member roster, deliberately "
                      "WITHOUT naming who has signalled: a partial signer "
                      "list is itself an anchor on the members still forming "
                      "a judgment.",
    "council-abort": "usage: helm chat council-abort <room> <reason...> — the "
                     "honest escape hatch. When members must COLLABORATE "
                     "before quorum the council does not partially leak: it "
                     "dies, the work moves to a standup, and a fresh council "
                     "convenes on the superseding tip. An aborted council "
                     "never reveals — judgment formed under collaboration is "
                     "not independent.",
}
# One preset, three spellings (premise council-is-the-number-one-feature:
# MELD is the GENUS and stays the primary verb; standup = the informal 2+
# convergence species — today's 2-party mindmeld included; council = the big
# FORMAL species, whose N-of-M verdict machinery is 0.3's). Each spelling
# answers help in its own voice and echoes itself in every printed
# next-command (meld.cmd via=).
MELD_VERBS = ("meld", "council", "standup")
for _v in MELD_VERBS:
    HELP[_v] = ("usage: helm chat %s invite <peer[,peer...]> <topic...> "
                "[--wait]%s | join <room> | recv <room> [--timeout S] | say "
                "<room> --marker YIELD|HOLD|DONE|ABORT <text...> | status   "
                "[--seat S on any verb]\n"
                "  One preset, three spellings: meld = the genus, standup = "
                "the informal 2+ convergence, council = the FORMAL one with a "
                "quorum. The spelling you type echoes back in every "
                "next-command.\n"
                "  A COUNCIL additionally takes --tip <sha> OR --question <text> (exactly ONE — the "
                "exact artifact every member judges) and --threshold K "
                "(default: majority), and unlocks the sealed-judgment verbs: "
                "verdict <room> <%s> --tip <sha> [--evidence <ref>] | "
                "council-status <room> | reveal <room> | council-abort <room> "
                "<reason...>. Nothing — not the verdicts, not WHO, not even "
                "HOW MANY — is visible before quorum."
                % (_v, " (--tip SHA | --question TEXT) [--threshold K]" \
                if _v == "council" else "",
                   "YES|NO|ABSTAIN"))
del _v
# Free-text verbs scan only the LEADING position for a help ask — prose
# ABOUT --help stays sendable (the same scope law as post's flag refusal:
# leading option positions are policed, the body is never scanned).
#
# The CONTRACT (judged at review, 2026-07-22): bare leading --help/-h is a
# help ask -> usage on stdout, rc 0. Leading --help WITH a body refuses
# LOUDLY (rc 2, usage on stderr, nothing sent) — the parent refused that
# shape too, and rc 0 here would SUCCESS-CODE a silently dropped message on
# a comms substrate. `-- ` before the body sends it literally. Repo swept
# for consumers of the parent's rc-2-on-bare---help shape: none exist.
_TEXT_VERBS = ("post", "reply", "dm") + MELD_VERBS


def _help_ask(verb, tail):
    """True when this invocation is asking for help, not work."""
    probe = tail[:1] if verb in _TEXT_VERBS else tail
    return any(a in ("-h", "--help") for a in probe)


def refuse_unknown_leading_flags(args, start, known, label, usage):
    """rc 2 if a flag-shaped LEADING token survived flag parsing, else None.

    THE SCAN AND THE TEACHING LIVE IN `helm.freetext`, which every verb that
    turns leftover argv into a record shares. What stays here is what is true
    of chat and of nothing else: which flag a caller reached for when they
    meant `--dm`, and the two lines a confused sender learns the stdin idiom
    from.

    THE COPY IS THE BUG, and this surface is where that was measured. `post`
    grew the guard after six `--to <seat>` posts went to the room as public
    text; `dm` did not have it, and three DMs reporting delivery OK carried the
    single word "--body-stdin" as their entire body — a flag that does not
    exist, silently promoted to the message, with a recipient acting on the
    absence for twenty minutes because a send that prints a row looks sent.

    `start` is where the options begin: 1 for `post <text...>`, but 1 past the
    RECIPIENT for `dm <seat> <text...>`, which is the positional that made `dm`
    stop policing after one token in the first place.
    """
    return freetext.refuse_leading_flags(
        args, start, known, label, usage, "helm chat", what="the message",
        hints=_CHAT_HINTS)


_TO_HINT = ("to reach ONE seat privately use --dm <seat>; without it every "
            "post is PUBLIC to the room.")
# The `--help` refusal's extra lines. They are the cure text a confused sender
# learns from — the one finding on the first sharing of this rule was that
# the shared helper dropped them, and rc was 2 either way so no gate could see
# it. `freetext.refuse` prints the usage line itself.
_HELP_HINT = (
    "no body argument = read from stdin (pipe it or use a quoted heredoc "
    "delimiter for literal backticks/$)",
    "nothing was sent — a bare `--help` asks usage; `--` before the body "
    "sends it.",
)
_CHAT_HINTS = {"--to": _TO_HINT, "--recipient": _TO_HINT, "--text": _TO_HINT,
               "--help": _HELP_HINT, "-h": _HELP_HINT}


def stdin_has_a_body_fd(stream, window=0.2):
    """Is there ACTUAL DATA (or EOF) waiting on `stream`, within a window?

    THE QUESTION IS NOT "IS STDIN A TTY". A non-tty fd may be a heredoc, a
    pipe, a regular file, `/dev/null`, or a live socket that never closes --
    and only the last one turns `sys.stdin.read()` into a permanent hang with
    no output, no row and no error. `select` answers the question that actually
    matters, and an fd at EOF counts as ready, so the `/dev/null` scripted pole
    still reaches the read and gets an empty body rather than a refusal.

    Unselectable stdin answers True, which is the pre-existing behaviour: this
    exists to convert a hang into a usage error, never to invent a new refusal
    on an fd it cannot classify.

    THE WINDOW HAS TERMS AND THEY ARE ACCEPTED ONES: a pipe writer whose FIRST
    byte lands after the window reads as no-stdin. Every real producer is ready
    at fork, and the only cure -- a blocking read -- is precisely the hang this
    exists to remove, so the window stays advisory by choice.

    ONE PREDICATE, TWO CALLERS. `helm dispatch send` asks the same question
    about the same fd and delegates here rather than carrying a second copy:
    two implementations of one question drift, and this one's terms (EOF is
    ready, unselectable is True, the window is advisory) are exactly the kind
    of detail that drifts silently.
    """
    import select
    try:
        ready, _w, _x = select.select([stream], [], [], window)
    except Exception:                    # noqa: BLE001 — unselectable stdin
        return True
    return bool(ready)


def resolve_one_body(text, label):
    """(body, rc) — the ONE body this invocation carries, or a refusal.

    TWO BODIES, ONE MESSAGE — REFUSE RATHER THAN CHOOSE. A positional message
    alongside a piped or heredoc one means the caller sent two and only one can
    travel; picking either would report "sent" while the other never left their
    shell, so both together are a refusal.

    NO BODY AT ALL MEANS STDIN IS THE BODY, but only when something is waiting
    on it. An fd with nothing on it and no writer to close it never reaches the
    EOF a read waits for, and that read has no timeout of its own.

    BOTH DOORS REST ON ONE QUESTION -- is there DATA, never is the fd a TTY --
    and `stdin_has_a_body_fd` above answers it once per invocation, with the
    terms that make /dev/null and every scripted caller pass through untouched.
    """
    if sys.stdin.isatty():
        return text, None
    # ONE READINESS ANSWER FOR BOTH DOORS BELOW. They ask the same question of
    # the same fd, and asking it twice would both cost a second window and let
    # the two answers disagree about one invocation.
    ready = stdin_has_a_body_fd(sys.stdin)
    if text:
        if ready and sys.stdin.read(1):
            print("helm chat %s: REFUSING — both a positional message and "
                  "piped/heredoc stdin are present, and choosing either "
                  "silently discards the other. Send ONE body: drop the "
                  "positional text to use stdin, or close stdin to use the "
                  "positional form." % label, file=sys.stderr)
            return None, 2
        return text, None
    # NO POSITIONAL BODY, SO STDIN IS THE BODY -- BUT ONLY IF SOMETHING IS
    # THERE. A bare read() returns only at EOF, and under an agent harness
    # stdin is a live UNIX socket that never reaches one: entering that read
    # with nothing waiting is a permanent hang with no output, no row and no
    # error, which reads to every observer as the author forgetting to type.
    # Not ready means no body: fall through and let the caller print its usage.
    if ready:
        text = sys.stdin.read().strip()
    return text, None


def dm_argv(args):
    """(to, text, rc) for `dm <seat> <text...> [--seat S]`; rc is not None
    when the caller must return it (the usage or refusal is already printed).

    THE RECIPIENT IS A POSITIONAL, AND THAT IS WHY THIS VERB WENT UNGUARDED.
    `post` polices leading option positions starting at args[1]; here args[1]
    is the seat name, which is not flag-shaped, so the identical scan would
    stop before seeing anything. Options begin AFTER the recipient, so the
    scan does too — measured 2026-09-09, when `dm <seat> --body-stdin <<'EOF'`
    sent the literal word "--body-stdin" as the entire message, three times,
    each printing a normal delivered row while the heredoc was never read.
    The sibling half: a positional body alongside a piped one used to take
    the positional and DISCARD the pipe at rc 0. Lives here, beside the two
    doors it composes, so the seats facade stays inside its line budget.
    """
    to = args[0] if args else None
    if to is not None:
        rc = refuse_unknown_leading_flags(
            args, 1, ("--seat",), "dm", "<seat> <text...> [--seat S]")
        if rc is not None:
            return to, "", rc
    text = " ".join(args[1:]).strip()
    if to:
        text, rc = resolve_one_body(text, "dm")
        if rc is not None:
            return to, text, rc
    if not (to and text):
        print("usage: helm chat dm <seat> <text...> [--seat S]  "
              "(one private recipient — never a room)\n"
              "  or give NO text and pipe the body on stdin / use a "
              "quoted heredoc: helm chat dm <seat> <<'EOF'",
              file=sys.stderr)
        return to, text, 2
    return to, text, None


def _pop_flag(args, name):
    """Pop `<name> VALUE` out of a verb's argv -> the value (None when absent
    or valueless) — the same flag shape --seat/--dm already use."""
    if name not in args:
        return None
    i = args.index(name)
    v = args[i + 1] if i + 1 < len(args) else None
    del args[i:i + 2]
    return v


def _actor_label(who):
    """The NAME to record for `who`, or None for the ambient floor.

    One unwrap shared by post and react so neither stringifies a capability by
    accident: `AdmittedActor.__str__` is deliberately `<AdmittedActor x #...>`,
    not the bare name, so a row built by interpolation would be visibly wrong
    rather than plausibly right. Lives here rather than in helm.actors because
    it is the RENDER half — the authorization half is actors.attributed."""
    from . import actors
    return actors.name_of(who)


def _seat_flag(args):
    """Pop `--seat S` out of a verb's argv: the caller's DECLARED display
    name. None when absent. NOTE: --seat NEVER selects the signer — use
    _seat_actor for any verb that posts/signs/acks (an xrev finding);
    this raw popper stays for the non-signing verbs (mute, status, wait…)."""
    if "--seat" not in args:
        return None
    i = args.index("--seat")
    v = args[i + 1] if i + 1 < len(args) else None
    del args[i:i + 2]
    return v


def _seat_actor(args, speech=False):
    """(AdmittedActor, None) or (None, err) — the acting ACTOR for a SIGNING
    verb (post/reply/react/dm/ack/council), with --seat enforced as an
    ASSERTION and never a signer selector.

    IT RETURNS A CAPABILITY NOW, NOT A NAME, and that is the point. This
    function used to answer `whoname()`, which hands back a MINTED name for a
    process that declares none and is rostered nowhere — so every CLI verb
    below it wrote durable signed rows under a stranger identity, and passed
    that stranger down as `who=`, sailing past a guard added at the far end.
    The bypass was the return TYPE. `helm.actors.resolve_actor` refuses
    malformed / disputed / DERIVED / mis-asserted in one pass and hands back
    an opaque capability, so the `who=` a verb passes on CANNOT be a derived
    name any more — the closure is at the source, not at the sink.

    --seat may only ASSERT the ambient identity:
      omitted               -> the ambient actor
      == ambient (casefold) -> the ambient actor (assertion satisfied)
      != ambient            -> (None, err): REFUSE the whole verb BEFORE any
                               row append / ACK transition / signer call — a
                               seat may not act or sign AS ANOTHER (was:
                               --seat drove who= AND profile=, so `--seat X`
                               signed as X; confirmed forgeable, 9/10).
    The DISPUTED refusal (2026-08-02: an inherited HELM_CHAT_NAME plus a
    roster row naming someone else) is unchanged in effect and now lives in
    the resolver, where every other actuator gets it too rather than each one
    re-implementing it.

    FOOTGUN SCOPE, honestly: a same-user process can still forge identity by
    setting HELM_CHAT_NAME/HELM_CELL_PROFILE itself — this prevents ACCIDENTAL
    --seat drift + the wrong-signer class, NOT a malicious local peer (that
    needs the owner-key trust domain, which is out of this check's scope)."""
    from . import actors
    claimed = _seat_flag(args)          # pops --seat (None if absent)
    # `act="act"` deliberately: the refusal reads "--seat X cannot act as
    # another seat", which is the sentence six years of tests, docs and muscle
    # memory already know. A more specific verb would have been a nicer
    # message and a gratuitously different one.
    #
    # `speech=True` for post/reply/react: a plain room row is SPEECH, and
    # `seats.auto_name` exists so an un-named join can say its first word.
    # Those verbs then get (None, None) and fall to the ambient floor — and a
    # `post --dm` still refuses, because the None routes into `seats.dm`,
    # whose own door treats AMBIENT as unadmitted. Speech that turns out to be
    # an act is caught by the act's door, not by this one.
    door = actors.resolve_speaker if speech else actors.resolve_actor
    return door(home.session_id(), asserted=claimed, act="act")


_ADDRESSEE = re.compile(
    r"(?<![A-Za-z0-9._/-])@([^\s@,;:!?()\[\]{}<>\"']+)"
)


def _post_addressee_capabilities(text):
    """Structured recipient facts for one row, from seats' one resolver."""
    from . import seats                 # deferred: seats imports chat at top
    out, seen = [], set()
    for match in _ADDRESSEE.finditer(text):
        # A final period is prose punctuation in the overwhelmingly common
        # sentence-ending form. Interior periods remain legal seat-name bytes.
        raw = match.group(1).rstrip(".")
        if not raw or seats._BROADCAST.fullmatch("@" + raw) \
                or raw.casefold() in seen:
            continue
        seen.add(raw.casefold())
        cap = seats.recipient_capability(raw)
        out.append({k: str(v) if k == "canonical" and v is not None else v
                    for k, v in cap.items() if not k.startswith("_")})
    return out


def _disclose_post_addressees(row):
    """Render the durable capability snapshot after the row has landed."""
    for cap in row.get("addressees") or ():
        raw = cap["raw"]
        if cap["error"]:
            print("helm chat: addressee @%s is MALFORMED — %s; the post was "
                  "still written." % (raw, cap["error"]))
        elif cap["membership"] == "JOINED":
            canonical = str(cap.get("canonical") or "")
            if canonical and canonical != str(raw).casefold():
                # the name is a live rename alias: say which row it reaches
                print("helm chat: addressee @%s is JOINED as %s — a rename "
                      "alias still inside its window; the post was written."
                      % (raw, canonical))
            else:
                print("helm chat: addressee @%s is JOINED — a current roster "
                      "row exists; the post was written." % raw)
        elif cap["membership"] == "ABSENT":
            print("helm chat: addressee @%s is ABSENT — no current roster row; "
                  "the post was still written." % raw)
        elif cap["evidence"] == "empty":
            print("helm chat: addressee @%s is UNKNOWN — no seat has joined "
                  "this box yet, so membership cannot be determined; the post "
                  "was still written." % raw)
        else:
            print("helm chat: addressee @%s is UNKNOWN — the roster could not "
                  "be read, so membership cannot be determined; the post was "
                  "still written." % raw)


def _advise_owner_post(text):
    """OWNER-BOUND ADVISORY, CONDITIONAL. Most chat rows are agent-to-agent and
    must never be nagged — the register is exactly what agents are meant to
    speak bare between themselves. A post is owner-bound only when it addresses
    him, so the gate is an @mention of a name from seats.owner_names(), the same
    resolver the delivery side uses. Advisory and fail-open, like every other
    owner-bound call-site."""
    try:
        from . import seats
        # BOUNDARY-AWARE, via the same resolver delivery uses. A substring test
        # matched @dariason and @daria-extra for the owner name daria, which
        # spends owner-mode advice on rows addressed to someone else.
        if not any(seats.mentions(text, n) for n in seats.owner_names() if n):
            return
        from .clarity import advise
        advise(text, "chat")
    except Exception:                    # noqa: BLE001 — fail-open by law
        pass


def cmd_chat(args):
    """chat post <text...> [--seat S] [--dm SEAT] [--reply-to <id|n>] |
    reply <id|n> <text...> [--seat S] | verify [--room R] | read [--since N]
    [--follow] [--dm] | rooms | react <n> <emoji> [--seat S] | log-flush |
    restore-journal [--apply] | retire-rooms [--apply] [--idle-days N] |
    dm <seat> <text...> [--seat S] |
    node up|down|status | meld|council|standup invite|join|recv|say|status |
    verdict|reveal|council-status|council-abort |
    join|deliver|stop-guard [--hook-json] | wait [--any] [--follow]
    [--ambient] [--seat S]
    | seats [--all] | receipts <broadcast-id>[@occurrence] | status [<one-line>|--clear] [--seat S]
    | seat rename <sid|oldname> <newname> [--dry-run] [--alias-hours H]
    | seat mute|unmute <room> [--seat S] | seat mutes [--seat S]
    | seat gc [--apply] | claim|release <resource> | claims [--json]
    [--room R]"""
    args = list(args or [])
    # THE PreToolUse GATE IS ANSWERED BEFORE THE ROOM PROLOGUE, and this is a
    # budget fix with a measurement behind it. `argv-guard` reads one Bash
    # command string and touches no room; its two gated advisory rungs read a
    # roster ONLY when the command names a family binary or a path in another
    # project's tree, through seams of their own — but the
    # prologue below imports `seats` unconditionally to derive a default room,
    # and that import measured 93 ms of CPU (seats_cli, seats_stop_signals,
    # seats_ack, vcs, subprocess…) on EVERY Bash and Monitor tool call, inside
    # a 2 s budget the owner watched blow several times a turn on every
    # project. Nothing below this line is reachable from `cmd_argv_guard`, so
    # skipping to it changes no answer the guard can give — the refusing and
    # passing arms are identical, which is what the gate's arms assert.
    #
    # `--hook-json` IS THE DISCRIMINATOR, not the verb alone: a human typing
    # `helm chat argv-guard --help` still falls through to the help gate below
    # and gets usage, exactly as before.
    if args[:1] == ["argv-guard"] and "--hook-json" in args:
        return cmd_argv_guard(args[1:])
    # Every no---room verb — posts, reads, join, deliver, the hooks pass no
    # --room — defaults to THE one homing precedence (seats.resolve_homing:
    # env seam > cwd project derivation), the SAME resolver the SessionStart
    # join writes the roster through. A private 'env or main' default here was
    # a second truth (roster-scatter class): a seat the join homed to proj-a
    # posted and read 'main' by default, zero rows reaching its home. Un-homed
    # contexts (no env, no project cwd) keep main. Homed delivery scans only
    # {home, main}; un-homed seats retain the legacy all-room inbox.
    room_given = "--room" in args
    room, room_source = "main", None
    if room_given:
        i = args.index("--room")
        val = args[i + 1] if i + 1 < len(args) else None
        if not val or val.startswith("-"):
            # guard_tail law: a flag-shaped value is never a room name.
            # Consuming it here ATE `--help` before the help gate below, so
            # `wait --room --help` blocked forever — the lane's own bug, one
            # token over — and `post --room --help x` posted into a room
            # literally named "--help". Refuse fast, name the problem.
            print("helm chat: --room wants a room name%s"
                  % (" — got %r" % val if val else ""), file=sys.stderr)
            return 2
        room = val
        del args[i:i + 2]
    else:
        from . import seats     # deferred: seats imports chat at module top
        # seats.safe_cwd, NEVER a bare os.getcwd(): this prologue runs before
        # verb dispatch AND before the hook branches' fail-open try blocks —
        # an eager getcwd here crashed every default verb + all three
        # delivery hooks for a session whose cwd was deleted (a pruned lane
        # worktree is routine). None ⇒ un-homed ⇒ #main; the session lives.
        resolved, source = seats.resolve_homing(None, seats.safe_cwd())
        if resolved:
            room = resolved
            room_source = "derived" if source == "derived" else None
    verb = args[0] if args else "read"
    if verb == "roster":            # roster: a friendlier alias for `seats`
        verb = "seats"
    # --help answered HERE, before ANY verb runs: wait/read --follow must
    # never enter their loops on a help ask, join/deliver/claim/log-flush
    # must never do work under one. rc 0 — an honest existence probe. On a
    # text verb, leading --help WITH a body is the loud rc-2 shape instead
    # (contract above _TEXT_VERBS): nothing is ever silently dropped.
    tail = args[1:]
    if verb in HELP and _help_ask(verb, tail):
        if verb in _TEXT_VERBS and len(tail) > 1:
            print(HELP[verb], file=sys.stderr)
            print("helm chat: NOTHING was sent — bare `helm chat %s --help` "
                  "asks usage; to send a body that STARTS with --help, put "
                  "`--` before it." % verb, file=sys.stderr)
            return 2
        print(HELP[verb])
        return 0
    # guard_tail law for the one flag every leg shares: a flag-shaped value
    # is never a seat name — refuse FAST instead of letting a later pop
    # swallow `--help` as an identity (`post --seat --help hi` posted as a
    # seat literally named "--help"; same class as the --room guard above).
    if "--seat" in args:
        i = args.index("--seat")
        v = args[i + 1] if i + 1 < len(args) else None
        if not v or v.startswith("-"):
            print("helm chat: --seat wants a seat name%s"
                  % (" — got %r" % v if v else ""), file=sys.stderr)
            return 2
    if verb == "transport":
        return _cmd_transport(args[1:])
    if verb == "node":
        from . import chatnode
        return chatnode.cmd_node(args[1:])
    if verb in MELD_VERBS:              # one preset, three spellings — meld.py
        from . import meld
        return meld.cmd(args[1:], via=verb)
    if verb in SEAT_VERBS:
        from . import seats
        return seats.cmd(
            verb, args[1:], room, room_explicit=room_given,
            room_source=room_source)
    if verb in ("post", "reply"):
        seat, _serr = _seat_actor(args, speech=True)
        if _serr:
            print("helm chat: " + _serr, file=sys.stderr)
            return 2
        if "--reply-to" in args:
            i = args.index("--reply-to")
            if i + 1 >= len(args) or not args[i + 1] \
                    or args[i + 1].startswith("--"):
                print("helm chat: --reply-to wants a parent id or number",
                      file=sys.stderr)
                return 2
        ref = _pop_flag(args, "--reply-to")
        if verb == "reply":
            # `reply <ref> <text...>` — the ref is positional, everything else
            # (seat, dm, signing, room) is the post path, unchanged
            if len(args) < 3:
                print("usage: helm chat reply <id|n> <text...> [--room R] "
                      "[--seat S]  (id = the parent row's id, n = its 1-based "
                      "number, -1 = latest)", file=sys.stderr)
                return 2
            ref = args[1]
            del args[1]
        to = None
        if "--dm" in args:      # post --dm SEAT = the dm verb, flag-shaped
            i = args.index("--dm")
            to = args[i + 1] if i + 1 < len(args) else None
            del args[i:i + 2]
            if not to or to.startswith("-"):
                print("helm chat: --dm wants a seat name%s"
                      % (" — got %r" % to if to else ""), file=sys.stderr)
                return 2
        # REFUSE an unrecognised LEADING flag instead of POSTING it, and
        # REFUSE two bodies rather than silently discarding one. Both guards
        # now live in shared helpers so the `dm` verb cannot drift away from
        # them again — it had neither, and sent a nonexistent flag as a body.
        # --to-integrator RESOLVES the addressee instead of spelling it.
        # A LITERAL MENTION IS A BET THAT THE ROSTER STILL CARRIES THAT NAME,
        # and the send door accepts any token and reports success — so a
        # subsystem escalating to a name nobody holds writes the row, reaches
        # nobody, and is told it posted. That failure direction is
        # silent-on-danger, which is the one an escalation must never take.
        to_integrator = "--to-integrator" in args
        if to_integrator:
            args.remove("--to-integrator")
        KNOWN = ("--room", "--seat", "--dm", "--reply-to", "--to-integrator")
        # THE BASIS OF A CLAIM, taken from LEADING position only. The body is
        # never scanned for it: prose that mentions one of these words is a
        # sentence, and a basis minted from a sentence is one nobody chose.
        from .verdicts import BASES, BASIS_FLAGS
        basis = []
        while len(args) > 1 and args[1] in BASIS_FLAGS:
            basis.append(args.pop(1)[2:])
        if len(basis) > 1:
            print("helm chat: a post states ONE basis — %s — and this one "
                  "gave %d, so nothing was sent."
                  % (" | ".join("--" + b for b in BASES), len(basis)),
                  file=sys.stderr)
            return 2
        if to_integrator and to:
            print("helm chat: --to-integrator addresses the ROOM and --dm "
                  "opens a private lane — they are different deliveries, so "
                  "nothing was sent. Pick one.", file=sys.stderr)
            return 2
        rc = refuse_unknown_leading_flags(
            args, 1, KNOWN, "post",
            "<text...> [--room R] [--seat S] [--dm SEAT] [--reply-to <id|n>] "
            "[--to-integrator]")
        if rc is not None:
            return rc
        text = " ".join(args[1:]).strip()
        text, rc = resolve_one_body(text, "post")
        if rc is not None:
            return rc
        if not text:
            print("usage: helm chat post <text...> [--room R] [--seat S] "
                  "[--dm SEAT] [--reply-to <id|n>] [--to-integrator]\n"
                  "  or pipe the body on stdin / use <<'EOF' with no text argument",
                  file=sys.stderr)
            return 2
        # A BROADCAST STEERS EVERY SEAT THAT READS IT, so it says how its
        # sender knows. The same three words a verdict already owes, asked at
        # the one chat door where a wrong reading costs the most seats the
        # most turns: an inferred reading broadcast as fact is acted on by
        # everyone before anyone checks it. Asked of the SENDER'S door only —
        # a subsystem that calls `post()` reports what it measured by
        # construction and is not routed through here.
        from .seats_common import _BROADCAST
        if not to and _BROADCAST.search(text) and not basis:
            print("helm chat: this post addresses EVERY seat, so it states "
                  "its basis. Nothing was sent.\n"
                  "  --measured   = you observed it yourself, this session\n"
                  "  --inferred   = reasoned from code or output you read\n"
                  "  --unverified = relayed or recalled, not checked\n"
                  "  Put ONE before the body: helm chat post --measured "
                  "<text...>. The word is printed at the head of the row, so "
                  "every reader sees how far to trust it before acting.",
                  file=sys.stderr)
            return 2
        if basis:
            text = "[%s] %s" % (basis[0].upper(), text)
        if to:
            from . import seats
            row, err = seats.dm(to, text, who=seat, reply_to=ref,
                                session=home.session_id())
            if err:
                print("helm chat: " + err, file=sys.stderr)
                return 1
            print("helm chat [dm] %s"
                  % _fmt(row, idx=index_rows(read(dm_room(row.get("dm")
                                                          or to))[0])))
            return 0
        if to_integrator:
            from .seats_integrator import integrator_addressed
            # THE TEXT SURVIVES EITHER WAY. Dropping an escalation because
            # nobody could be addressed loses the danger along with the
            # delivery; carrying the reason IN the body means the row itself
            # says it reached no addressee, so a reader who finds it later
            # is not left inferring that from silence.
            text = integrator_addressed(text)
        row = post(text, room, who=seat, reply_to=ref)
        # the echo carries the quote (and the parent's new count): the poster
        # SEES which row it landed under — an unresolvable ref is visible as an
        # orphan right here, not three surfaces later
        print("helm chat [%s] %s" % (room, _fmt(row, idx=index_rows(read(room)[0]))))
        # ...and the row ID, LAST, because a long body scrolls the echo away.
        #
        # This id is what every OTHER verb wants — `helm asks report <ask>
        # <post-id>`, `chat reply <id>`, `react <id>` — and it was printed
        # NOWHERE, so the only way to obtain it was to scrape `chat read` for
        # the row you just wrote. That does not compose: a verb that REQUIRES an
        # id must be reachable from the verb that MINTS one.
        #
        # Live 2026-07-26: scraping picked up an unrelated row from a previous
        # day (a `--limit 1` read returned scrollback, not the newest row), and
        # 16 owner-ask reports were recorded against a post about something
        # else. The asks ledger is append-only and refuses to re-report, so
        # those refs are permanently wrong. Printing the id costs one line.
        _disclose_post_addressees(row)
        if row.get("id"):
            print("helm chat: id %s" % row["id"])
        _advise_owner_post(text)
        return 0
    if verb == "read":
        label = room
        if "--dm" in args:      # the recipient's own private lane
            args.remove("--dm")
            room = dm_room(_seat_flag(args) or whoname())
            label = "dm"
        # guard_tail (the 9ce7b8c precedent): every remaining token must be a
        # known flag — `--limit` once meant 'the newest row' and returned
        # scrollback (16 owner-asks misreported off it, 2026-07-26). Placed in
        # the read branch, not cmd_chat's dispatcher: a dispatcher-level guard
        # would have to enumerate every OTHER verb's flags, and
        # ApplyReadersAreGuarded watches exactly that seam.
        from .cli import guard_tail
        grc = guard_tail("helm chat read", args[1:],
                         flags=("--follow",),
                         valued=("--since", "--limit"),
                         usage="usage: helm chat read [--room R] [--since N] "
                               "[--limit N] [--follow] [--dm [--seat S]]")
        if grc is not None:
            return grc
        since = 0
        if "--since" in args:
            try:
                since = int(args[args.index("--since") + 1])
            except (IndexError, ValueError):
                print("helm chat: --since wants an integer", file=sys.stderr)
                return 2
        limit = None
        if "--limit" in args:
            try:
                limit = int(args[args.index("--limit") + 1])
                if limit < 1:
                    raise ValueError
            except (IndexError, ValueError):
                print("helm chat: --limit wants a positive integer (the "
                      "NEWEST N rows; for everything since an offset, "
                      "--since N is the route)", file=sys.stderr)
                return 2
        if "--follow" in args:
            return _follow(room, since)
        rows, total = read(room)
        idx = index_rows(rows)            # the WHOLE room indexes the thread:
        tag = react_prefix(rows)          # [n] beside a row IS its `react n`,
        if limit is not None:
            # --limit = the NEWEST N (what every caller means), counting from
            # the END of the room; [n] tags stay whole-room so a react still
            # resolves. --since composes as the window's floor.
            start = max(since, total - limit)
        else:
            start = since if 0 <= since <= total else 0   # counted over the WHOLE
        msgs = rows[start:]               # room so --since never shifts [n]
        for i, m in enumerate(rows[start:], start):   # a quote resolves to a
            print(tag(i) + _fmt(m, idx=idx))          # parent older than --since
        if not msgs:
            print(empty_room_line(room, label))
        consume(room, total)
        return 0
    if verb == "react":
        seat, _serr = _seat_actor(args, speech=True)
        if _serr:
            print("helm chat: " + _serr, file=sys.stderr)
            return 2
        if len(args) < 3:
            print("usage: helm chat react <n> <:shortcode:|emoji> [--room R] "
                  "[--seat S] (n is the [n] shown by `helm chat read`, 1-based; "
                  "-1 = latest; same react again toggles it off)",
                  file=sys.stderr)
            return 2
        try:
            n = int(args[1])
        except ValueError:
            print("helm chat: react wants a message number", file=sys.stderr)
            return 2
        row, err = react(n, args[2], room, who=seat)
        if err:
            print("helm chat: " + err, file=sys.stderr)
            return 1
        print("helm chat [%s] %s" % (room, _fmt(row)))
        return 0
    if verb == "restore-journal":
        # closed-set tail: this verb REWRITES room files — a junk token must
        # refuse before any of that runs, never ride along silently
        junk = [a for a in args[1:] if a != "--apply"]
        if junk:
            print("helm chat restore-journal: unknown argument%s %s — takes "
                  "only --apply" % ("s"[:len(junk) != 1], " ".join(junk)),
                  file=sys.stderr)
            return 2
        apply = "--apply" in args[1:]
        out = restore_journal(apply=apply)
        if out["state"] == "absent":
            print("helm chat: no journal at %s — nothing to restore (a fresh "
                  "host, or log-flush never ran here)" % journal_dir())
            return 0
        meta = out.get("meta") or {}
        meld_report = out.get("meld") or {"state": "absent", "rooms": {}}
        for room_, info in sorted((meld_report.get("rooms") or {}).items()):
            if info.get("state") == "UNKNOWN":
                print("helm chat: meld lifecycle %s is UNKNOWN — %s" %
                      (room_, _safe_reason(info.get("reason") or
                                           "unreadable durable input")),
                      file=sys.stderr)
            else:
                verb = "replayed" if apply and info.get("replayed") else "would replay"
                print("  %-24s meld %s %d actor%s from %d lifecycle event%s" %
                      (room_, verb, info.get("actors", 0),
                       "s"[:info.get("actors", 0) != 1], info.get("events", 0),
                       "s"[:info.get("events", 0) != 1]))
        if meld_report.get("state") == "UNKNOWN" and not meld_report.get("rooms"):
            print("helm chat: meld lifecycle journal is UNKNOWN — %s" %
                  _safe_reason(meld_report.get("reason") or
                               "unreadable durable input"), file=sys.stderr)
        # tri-state honesty: unreadable input is UNKNOWN history, and an
        # empty journal must not read like a successful no-op restore
        for name in meta.get("unreadable") or ():
            print("helm chat: journal file %s is UNREADABLE — this restore "
                  "view is INCOMPLETE, not empty" % name, file=sys.stderr)
        if meta.get("orphan_lines"):
            print("helm chat: %d journal line(s) preceded any record header "
                  "and were counted, not restored — the journal may be "
                  "damaged" % meta["orphan_lines"], file=sys.stderr)
        if (not out["rooms"] and not (meta.get("unreadable") or ())
                and meld_report.get("state") == "absent"):
            print("helm chat: journal is EMPTY (%d file(s), 0 records) — "
                  "nothing to restore" % meta.get("files", 0))
            return 0
        acted = {k: v for k, v in out["rooms"].items() if v.get("restored")}
        for room_, info in sorted(out["rooms"].items()):
            if info.get("restored"):
                print("  %-24s %d row%s%s%s" % (
                    room_, info["restored"], "s"[:info["restored"] != 1],
                    " + marker, %d live kept" % info.get("live_kept", 0),
                    (", %d cursor%s minted" % (info["cursors_minted"],
                     "s"[:info["cursors_minted"] != 1]))
                    if "cursors_minted" in info else ""))
            else:
                extra = ""
                if info.get("cursors_repaired") or info.get("cursors_minted"):
                    extra = " — cursors: %d repaired, %d minted" % (
                        info.get("cursors_repaired", 0),
                        info.get("cursors_minted", 0))
                print("  %-24s skipped (%s)%s"
                      % (room_, info.get("skipped"), extra))
        if not apply:
            print("helm chat: DRY RUN — %d room%s would restore; add --apply"
                  % (len(acted), "s"[:len(acted) != 1]))
        else:
            print("helm chat: restored %d room%s from %s"
                  % (len(acted), "s"[:len(acted) != 1], journal_dir()))
        # unreadable input means the answer above is a floor, not the truth
        return 1 if ((meta.get("unreadable") or ())
                     or meld_report.get("state") == "UNKNOWN") else 0
    if verb == "argv-guard":
        return cmd_argv_guard(args[1:])
    if verb == "log-flush":
        if "--install-timer" in args:
            return _logflush_install_timer(args)
        # A SCOPED RUN IS NOT THE FLEET'S DURABILITY RUN. `--room helm` is what
        # an operator types while REPAIRING an outage, and counting it would let
        # one hand-flushed room reset the streak that watches the timer's
        # all-rooms run — the health record would read "ok" while the timer kept
        # failing, which is the exact silence this watchdog exists to break. It
        # also keeps the concurrent writer out of the state file.
        report, watched = {}, not room_given
        try:
            n = log_flush(rooms=[room] if room_given else None, report=report)
        except Exception as exc:
            # A RAISING RUN IS A FAILURE AND NOW COUNTS AS ONE. This branch
            # printed and returned 1 roughly 160 consecutive times into a
            # journal nobody reads.
            if watched:
                flush_watchdog(None, report, error=exc)
            from . import meld
            if not isinstance(exc, meld.LifecycleError):
                raise
            print("helm chat: log-flush FAILED before rendered rows — %s" % exc,
                  file=sys.stderr)
            return 1
        if n < 0:
            print("helm chat: log-flush disabled (HELM_CHAT_LOG=%s)"
                  % home.env("CHAT_LOG"))
            return 0
        health = (flush_watchdog(n, report) if watched
                  else dict(zip(("state", "reason"), flush_outcome(n, report))))
        print("helm chat: log-flush appended %d row%s -> %s" % (
            n, "s"[:n != 1], journal_dir()))
        if health["state"] not in ("ok", "off"):
            # THE COUNT ALONE COULD NOT TELL "nothing new" FROM "every room
            # refused" — both print 0 rows appended, and for eight hours the
            # second one rendered as the first.
            print("helm chat: log-flush %s — %s%s"
                  % (health["state"].upper(), health["reason"],
                     (" [consecutive failure %d%s]"
                      % (health["streak"],
                         "; alerted %s" % _obligated_seat()
                         if health.get("delivered") else ""))
                     if health.get("streak") else ""),
                  file=sys.stderr)
        return 1 if health["state"] == "failed" else 0
    if verb == "verify":
        rep = verify(room)
        bad = [r for r in rep if r["state"] == "MISMATCH"]
        n = {s: sum(1 for r in rep if r["state"] == s)
             for s in ("ok", "MISMATCH", "legacy", "unsigned")}
        for r in bad:
            print("helm chat [%s] row %d (%s) MISMATCH: signed %s, recomputes "
                  "%s%s" % (room, r["n"], r["from"],
                            r["stored"] or "(no payload — STRIPPED from a "
                            "signed reply)", r["payload"],
                            " — reply_to %s" % r["reply_to"]
                            if r["reply_to"] else ""), file=sys.stderr)
        print("helm chat [%s] verify: %d row%s — %d ok, %d mismatch, %d legacy "
              "(signed pre-payload), %d unsigned" %
              (room, len(rep), "s"[:len(rep) != 1], n["ok"], n["MISMATCH"],
               n["legacy"], n["unsigned"]))
        print("  (self-consistency only — the node cannot yet disclose a "
              "turn's payload, so a signed row's REMOTE binding stays "
              "'turn observed', never re-verified)")
        return 1 if bad else 0
    if verb == "retire-rooms":
        from . import chatdebris
        return chatdebris.cmd_retire_rooms(args[1:])
    if verb == "rooms":
        names = list_rooms()
        if not names:
            print("helm chat: no rooms yet — helm chat post <text> starts main")
            return 0
        for n in names:
            msgs, total = read(n)
            unread = " [owner-unread]" if os.path.exists(marker_path(n)) else ""
            last = ("  last: " + _fmt(msgs[-1])) if msgs else ""
            print("  %s  %d msg%s%s%s" % (n, total, "s"[:total != 1], unread, last))
        return 0
    print("helm chat: unknown subcommand '%s' (post|reply|read|rooms|react|"
          "verify|log-flush|restore-journal|retire-rooms|argv-guard|node|"
          "transport|meld|"
          "council|standup|roster|%s)"
          % (verb, "|".join(SEAT_VERBS)), file=sys.stderr)
    return 2
