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
only, never nested: a reply renders with a compact quote of its parent,
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
import contextlib
import hashlib
import tempfile
import json
import math
import os
import re
import stat
import sys
import threading
import time
import unicodedata

from . import home, pk
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
_SIGN_FAILURE_FALLBACK_LOCK = threading.RLock()
_QUOTED_VALUE = r'''(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')'''
_UNCLOSED_QUOTED_VALUE = r'''(?:"[^\r\n;}]*|'[^\r\n;}]*')'''
_AUTH_VALUE = re.compile(
    r'''(?ix)["']?authorization["']?\s*[:=]\s*(?:''' +
    _QUOTED_VALUE + r'''|''' + _UNCLOSED_QUOTED_VALUE + r'''|[^\r\n;}]+)''')
_SECRET_VALUE = re.compile(
    r'''(?ix)(?:["']?)(bearer[_-]?token|access[_-]?token|refresh[_-]?token|'''
    r'''id[_-]?token|api[_-]?key|client[_-]?secret|token|passphrase|password|'''
    r'''secret|credentials?)(?:["']?)(?:\s*[:=]\s*|\s+)(?:''' +
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
    "join_failed": "repair this profile's room-node join, then retry",
    "send_failed": "inspect `helm chat node status` and this profile's balance, then retry",
    "signing_exception": "inspect `helm doctor` and the named exception, then retry",
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


def room_path(room="main"):
    """Room name -> its RAM file. The `dm-` prefix is the DM namespace: those
    lanes live in the dm/ subdir, invisible to list_rooms (no room fanout, no
    web channel row, no default log-flush) while every reader/cursor/rotation
    mechanic composes unchanged."""
    r = pk.slug(room)
    if r.startswith(DM_PREFIX):
        return os.path.join(chat_dir(), "dm", r[len(DM_PREFIX):] + ".jsonl")
    return os.path.join(chat_dir(), r + ".jsonl")


def dm_room(to):
    """The recipient's private lane, as a (reserved-namespace) room name —
    keyed by seats' exact-token seat key, so `team.a` and `team-a` hold
    DIFFERENT lanes (the slug-collision class never crosses a DM)."""
    from . import seats
    return DM_PREFIX + seats._seat_key(to)


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
    by breaking every chat verb that lists rooms."""
    _auto_restore_once()
    d = chat_dir()
    if not os.path.isdir(d):
        return []
    return sorted(n[:-6] for n in os.listdir(d) if n.endswith(".jsonl"))


_AUTO_RESTORE_RUNNING = False


def _restored_sentinel():
    """chat_dir()/.restored-this-boot — boot-scoped state on boot-scoped
    storage. TMPFS dies at boot (exactly when the latch must re-arm) and the
    file survives room ROTATION (exactly what a marker ROW cannot — the
    marker is posted once at boot, always among the oldest rows, and
    rotation evicts the oldest half first: hc2's F3, the decaying fast
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
    bus pays one stat(), never this scan (hc2's F3: keying the fast path on
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


def dead_cursors(live=None):
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
    session is provably not running, per `sessions.live_sids()` (pid-keyed, with
    a liveness check on the pid it names). Age cannot answer this — a pane that
    has been thinking for an hour looks exactly like one that exited an hour ago,
    and dropping a LIVE session's cursor makes that seat re-read its whole room
    and re-deliver everything it already saw. If liveness cannot be established
    at all we keep EVERYTHING and report the error: an unknown session is not a
    dead one.

    Dry-run by default, like every other helm gc.
    """
    d = chat_dir()
    if not os.path.isdir(d):
        return [], 0, None
    if live is None:
        try:
            from . import sessions
            live = set(sessions.live_sids())
        except Exception as e:            # cannot prove liveness -> touch nothing
            return [], 0, "liveness unavailable (%s) — kept everything" % e
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
        i = n.find(".cursor.")
        if i < 0:
            continue
        # A `.lock` sibling also contains ".cursor." — counting it as a cursor
        # doubles the reported total and parses its sid as the literal "lock".
        # Locks are removed WITH their cursor below, never counted as one.
        if n.endswith(".lock"):
            continue
        sid = n.rsplit(".", 1)[-1]
        if not sid or sid[:8] in live_pfx:
            kept += 1
            continue
        victims.append(os.path.join(d, n))
        lk = os.path.join(d, n + ".lock")
        if os.path.exists(lk):
            victims.append(lk)          # the lock is half the directory entries
    return victims, kept, None


def _reap_for_test(live=None):
    """Test-only actuator. Production deletion belongs to `helm gc` — this
    exists so the reap LOGIC can be pinned without importing the gc plane."""
    victims, kept, err = dead_cursors(live=live)
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
    shm = os.path.realpath("/dev/shm")
    if d == shm or d.startswith(shm + os.sep):
        return chat_dir()
    key = hashlib.blake2b(os.path.abspath(chat_dir()).encode("utf-8"),
                          digest_size=8).hexdigest()
    return os.path.join(SIGN_FAILURES_ROOT, key)


def sign_failures_path():
    return os.path.join(sign_failures_dir(), SIGN_FAILURES_FILE)


def _sign_failures_lock_path():
    return os.path.join(sign_failures_dir(), ".sign-failures.lock")


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


def _safe_reason(reason):
    """Bounded operator diagnostic. Redact BEFORE whitespace folding so an
    auth assignment can consume its full multiword value without eating the
    next diagnostic clause."""
    s = str(reason or "unknown signing failure")
    s = _URL_USERINFO.sub(r"\1[redacted]@", s)
    s = _AUTH_VALUE.sub("authorization=[redacted]", s)
    s = _SECRET_VALUE.sub(lambda m: "%s=[redacted]" % m.group(1), s)
    s = _AUTH_SCHEME.sub("auth=[redacted]", s)
    return _dsan(" ".join(s.split()))[:360]


def _diag(code, reason, remediation=None, event_epoch=None, event_ts=None):
    """Structured, secret-safe failure captured at the failure boundary. The
    private event clock is sampled FIRST, so scheduling during redaction or
    before the RAM lock cannot reorder it behind a newer success."""
    event_epoch = _epoch(event_epoch, time.time())
    event_ts = event_ts if isinstance(event_ts, str) and event_ts else pk.now_ts()
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
    with open(p, encoding="utf-8") as f:
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
    from . import cell
    reason = "incident state unreadable (%s: %s)" % (
        exc.__class__.__name__, exc)
    d = _diag("incident_state_unreadable", reason, event_epoch=now)
    rec = {"profile": _profile(cell.profile_name()), "code": d["code"],
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
    The persistent incident fields come from RAM, never disk coordination."""
    from . import cell
    u = node_url()
    signer = cell.bin_status()
    failures = sign_failures()
    out = {"mode": "unsigned", "url": u, "head": None,
           "signer": signer["usable"],
           "signer_configured": signer["configured"]}
    if signer["configured"] and not signer["usable"]:
        current = next((f for f in failures
                        if f.get("profile") == _dsan(cell.profile_name())
                        and f.get("code") == "signer_unavailable"), None)
        current = current or _transient_failure(
            cell.profile_name(), "signer_unavailable", signer["reason"])
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
        f = _transient_failure(
            cell.profile_name(), "node_unreachable", reason)
        out.update(f, mode="degraded", state="DEGRADED",
                   failed_profiles=[f])
        return out
    if h is not None:
        if not signer["usable"]:
            out["mode"] = "unsigned (no signer)"
        elif _signed_success_epoch(cell.profile_name()):
            out.update(mode="signed", state="SIGNED", label="signed")
        else:
            out.update(mode="ready", state="READY",
                       label="ready (unproven)",
                       detail="no committed signing receipt observed for this profile")
            # A FLEET SURFACE DESCRIBES THE SYSTEM, NOT THE PROCESS RENDERING
            # IT. Owner, live 2026-07-29: "does the webui need retarting or
            # something?" — the web ledger read "signing ready (unproven)"
            # while codex, ds4pro, gemini and opus-integrator all carried
            # committed receipts and rows were anchoring with real turn hashes.
            # Nothing was stale. The web process has no signing profile of its
            # own (it is `helm-agent`, which never signs), so an exact-profile
            # question was the wrong question to put on a fleet panel.
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
        if not isinstance(rec, dict):
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
    A row with a bare truthy `turn` is not signed (codex-3 xrev E: consumers
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


LOW_WATER = 3000   # ~2 signed turns (measured: a 73B digest send costs ~1442)


def _faucet(cell_hex):
    """Top up one cell from the room node's faucet. Returns the validated
    response or a precise refusal; success:false is never discarded."""
    from . import chatnode
    u = node_url()
    if not u:
        return None, "faucet unavailable: no chat node URL"
    return chatnode.faucet(u, cell_hex, 10000)


def _balance(cell_hex):
    from . import cell
    u = node_url()
    info = cell.get_json(u + "/api/cell/" + cell_hex, timeout=2) if u else None
    return info.get("balance") if isinstance(info, dict) else None


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
        # LOCAL launch failure (binary missing/unusable/failed to exec) — NOT a
        # node-state fault, so the caller must NOT revive/unlock over it.
        return None, err, False
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


def _sign_send(payload, profile, topic=CHAT_TOPIC):
    """One signed self-write turn under `topic` (helm.chat by default, but the
    emit path is TOPIC-PARAMETERIZED — helm.land/helm.claim ride the SAME signer,
    revive, faucet and completeness checks, never a forked second signer).
    Returns (send-info, None) or (None, structured diagnostic). One recovery
    lap; both send attempts and every revive/faucet refusal survive into the
    final operator reason."""
    from . import cell
    signer = cell.bin_status()
    if not signer["usable"]:
        return None, _diag("signer_unavailable", signer["reason"])
    token = _node_token()
    hexid, err, launched = _room_cell(profile, token)
    if err:
        if not launched:
            return None, _diag("signer_unavailable", err)
        revived, revive_err = _revive()
        if revive_err is None:
            token = revived if revived is not None else token
        hexid, err, launched = _room_cell(profile, token)
        if err:
            detail = err if not revive_err else "%s; revive failed: %s" % (
                err, revive_err)
            return None, _diag("join_failed", detail)
    recovery = []
    b = _balance(hexid)
    if b is not None and b < LOW_WATER:
        _r, faucet_err = _faucet(hexid)
        if faucet_err:
            recovery.append("proactive %s" % faucet_err)
    attempts = []
    for attempt in (1, 2):
        rc, out, err = cell.run_bin(
            ["send", "--profile", profile, "--to", hexid,
             "--topic", topic, payload],
            timeout=30, env_extra=_env_extra(token))
        if rc is None:
            return None, _diag("signer_unavailable", err)
        info = cell._last_json(out)
        complete = _complete_send(info)
        if rc == 0 and complete:
            info = dict(info)
            info["_helm_signed_at"] = time.time()
            return info, None
        detail = (err or out) or "no complete sent receipt"
        if rc == 0 and isinstance(info, dict) and info.get("sent") is True \
                and not complete:
            detail = "sent:true response has invalid turn_hash/receipt_hash/chain_index"
        attempts.append("attempt %d rc %s: %s" % (
            attempt, rc, detail.strip()))
        if attempt == 1:
            revived, revive_err = _revive()
            if revive_err:
                recovery.append("revive failed: %s" % revive_err)
            elif revived is not None:
                token = revived
            _r, faucet_err = _faucet(hexid)
            if faucet_err:
                recovery.append("recovery %s" % faucet_err)
    reason = "; ".join(attempts + recovery)
    return None, _diag("send_failed", reason)


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
    # naming the OWNER, which is how agent rows came to render as
    # owner-signed. An explicit `profile` is STATED INTENT and still wins; a
    # DISAGREEMENT between the seat this process provably is and the profile
    # the environment names is refused, because the only alternative to
    # refusing is signing with a key that belongs to someone else.
    p, refusal = cell.signing_identity(profile)
    if refusal:
        return None, _diag("identity_conflict", refusal)
    try:
        return _sign_send(payload, p, topic=topic)
    except Exception as exc:
        return None, _diag("signing_exception",
                           "%s: %s" % (exc.__class__.__name__, exc))


def _signed_row(row, payload_text, profile, sign):
    """Common signing owner for posts/reactions. The RAM row ALWAYS lands.
    A failed attempted signature is stamped + retained per profile; only an
    observed signed send clears that profile's incident. On the production
    `sign=None` path, an unset signer/node URL is configured-off v1, while a
    configured-but-unusable signer is a `signer_unavailable` incident.
    `sign=True` is the explicit force/test seam and therefore records inability
    to honor that forced attempt; `sign=False` always skips."""
    p = profile
    try:
        from . import cell
        p = p or cell.profile_name()
        if sign is None:
            signer = cell.bin_status()
            if signer["configured"] and not signer["usable"]:
                return _stamp_sign_failure(
                    row, p, _diag("signer_unavailable", signer["reason"]))
            if not signer["configured"] or not node_url():
                return row
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
            sign = True
        if not sign:
            return row
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
def _room_lock(room):
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
            # rebuilding under it (codex r4 LOCK).
            os.makedirs(chat_dir(), mode=0o700, exist_ok=True)
            lf = open(os.path.join(chat_dir(), pk.slug(room) + ".lock"), "a")
            fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
        except OSError:
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
        with open(path, encoding="utf-8") as f:
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


def _append(row, room, event_id=None):
    """ONE serialized write path for every room writer: id-stamp, append the
    whole row in one write, flush, then rotate — all under the room lock.
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
    if event_id is None:
        row.setdefault("id", os.urandom(6).hex())
    else:
        row["id"] = _event_row_id(room, row.get("from") or "", event_id)
        row["event"] = 1
    path = room_path(room)
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)  # dm/ lane
    with _room_lock(room) as locked:
        receipts = None
        if event_id is not None:
            if not locked:
                raise OSError("chat room lock unavailable; refusing an "
                              "unproven idempotent append")
            receipts = _event_receipts(room)
            prior = receipts.get(row["id"])
            if prior is not None:
                _prove_event_receipt(_event_receipt_path(room))
                return prior
            prior = _row_with_id(path, row["id"])
            if prior is not None:
                receipts[row["id"]] = prior
                _write_event_receipts(_event_receipt_path(room), receipts)
                return prior
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
                _write_event_receipts(_event_receipt_path(room), receipts)
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
        _rotate(path, room=room)
    return row


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
        raise ValueError(
            "helm chat: refusing to post a padded short sha — resolve it "
            "($(git rev-parse <prefix>)) or set %s=1 to quote it deliberately"
            % shaguard.SKIP_ENV)
    shaguard.warn(text)
    row = {"ts": pk.now_ts(), "from": who or whoname(), "text": text}
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
        row.update(_parent_fields(room, reply_to))
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
    return _append(_signed_row(row, text, profile, sign), room,
                   event_id=event_id)


def _touch_poster_presence(name):
    """Presence-on-post: a seat that SPEAKS is alive, beacon or no beacon — so
    keep its roster row fresh and the reaper never drops a live-but-idle poster
    (roster-truth invariant). Best-effort + local import
    (chat<-seats would cycle); owner/broadcast names are not seats — skip them
    so no spurious presence file is minted."""
    try:
        from . import seats
        if not name or name.lower() in seats.owner_names():
            return
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
    reactor = who or whoname()
    on = _react_state(rows).get(("%s|%s" % (tts, tfrom), reactor, e))
    row = {"ts": pk.now_ts(), "from": reactor, "react": e,
           "tts": tts, "tfrom": tfrom}
    if on:
        row["un"] = True   # toggle OFF — the tombstone every renderer honors
    # the payload is derived from the ROW (payload_for -> _react_payload), so
    # the signer and `helm chat verify` read one definition, never two
    return _append(_signed_row(row, None, profile, sign), room), None


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


def _rotate(path, cap=None, room=None):
    """Past the cap, keep the newest half plus every paused unread suffix.

    Normal chat remains a bounded tmpfs surface. A measured credential wall is
    different: its cursor cannot advance, and rotation may not turn that pause
    into message loss. The state-transition lock orders this rewrite against both
    dark persistence and delivery commits. Any observer/import failure preserves
    the whole room; exceeding a RAM etiquette cap is safer than dropping an owed
    address.
    """
    cap = SIZE_CAP if cap is None else cap
    try:
        if os.path.getsize(path) <= cap:
            return False
    except OSError:
        return False
    try:
        from . import proxywatch, seats
        with proxywatch.delivery_state_guard():
            state, err = proxywatch._read_watch_state()
            observed = state if not err else []
            # split on exactly "\n" (the writer's terminator) — splitlines()
            # also splits on U+2028/U+2029/\x85 INSIDE message text.
            with open(path, encoding="utf-8", errors="replace") as f:
                st = os.fstat(f.fileno())
                lines = [x for x in f.read().split("\n") if x]
            cut = len(lines) // 2
            offsets, off = [], 0
            for line in lines:
                offsets.append(off)
                off += len((line + "\n").encode("utf-8"))
            if room is None:
                stem = os.path.basename(path).removesuffix(".jsonl")
                room = DM_PREFIX + stem if os.path.basename(
                    os.path.dirname(path)) == "dm" else stem
            hold = seats.rotation_hold_offset(room, st.st_dev, st.st_ino,
                                              observed)
            if hold is not None and offsets:
                keep = min(offsets[cut] if cut < len(offsets) else off,
                           max(0, hold))
                cut = next((i for i, start in enumerate(offsets)
                            if start >= keep), len(lines))
            pk.atomic_write(path, "".join(x + "\n" for x in lines[cut:]))
        return True
    except Exception:
        return False


def _msg(raw):
    try:
        m = json.loads(raw)
    except ValueError:
        return None
    return m if isinstance(m, dict) else None


def read(room="main", since=0):
    """(rows[since:], total) — the ONE poll primitive: the CLI read, --follow,
    the web GET and the TUI all sit on this. Rows are messages AND reaction
    rows (renderers aggregate — thread()). A since past the end (the room
    rotated) resets to 0 so a poller re-syncs instead of starving;
    unparseable lines are skipped, never fatal."""
    try:
        # exactly "\n", never splitlines() — a message carrying U+2028 (a voice
        # paste can) must not tear its row for every reader (found 2026-07-20)
        with open(room_path(room), encoding="utf-8", errors="replace") as f:
            raw = [x for x in f.read().split("\n") if x]
    except OSError:
        return [], 0
    msgs = [m for m in map(_msg, raw) if m]
    total = len(msgs)
    return msgs[since if 0 <= since <= total else 0:], total


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

    def emit(cur):
        ts, room, lines = cur
        # dm-* was never journaled. Meld rows restore like any room: the
        # 2026-08-02 reboot emptied every meld room (lifecycle skeletons
        # survived; the conversation did not) and the participants had to
        # rebuild their converged spec from transcripts by hand.
        if room.startswith("dm-"):
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
    """(minted, repaired): make every cursor of `room` point at reality.

    Two consumer populations can replay restored history as new:
      - a seat with NO cursor reads from offset 0 by design (the tracked-seat
        backfill law) — cured by MINTING an end-of-file cursor;
      - a cursor whose rid is NOT in the file hits _tail's rotation fallback
        ("reset once to zero and accept duplicate replay") — the 290-row
        beacon flood, measured live 2026-07-28 — cured by REPAIRING any
        cursor (seat-level or session-scoped) whose rid the file no longer
        carries. rid-in-file is the ONE test; everything else is left alone.
    Runs on already-restored rooms too, so re-running the verb after a crash
    between swap and sweep finishes the job instead of recreating the flood.
    """
    from . import seats
    minted = repaired = 0
    st = None
    prefix = pk.slug(room) + ".cursor."
    try:
        names = os.listdir(chat_dir())
    except OSError:
        names = []
    for name in names:
        if not name.startswith(prefix):
            continue
        path = os.path.join(chat_dir(), name)
        cur = pk.read_json(path, None)
        if not (isinstance(cur, dict) and isinstance(cur.get("off"), int)):
            continue
        if cur.get("rid") in valid_ids:
            continue                      # a live cursor — never touched
        if st is None:
            st = seats._baseline_state(room, at_start=False)
        pk.write_json(path, {"dev": st[0], "ino": st[1], "off": st[2],
                             "rid": st[3], "active": bool(cur.get("active")),
                             "base": st[2]})
        repaired += 1
    for seat in sorted(k for k in seats.roster() if isinstance(k, str) and k):
        if seats._cursor(room, seat) is None:
            if st is None:
                st = seats._baseline_state(room, at_start=False)
            seats._write_cursor(room, seat, *st, base=st[2])
            minted += 1
    return minted, repaired


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
    r"|store\s+add\b"
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
    # the value itself and is exempt. Found live by ds4pro post-land: the
    # equals-form let a backticked body sail through as a "flag value".
    return (word.startswith("--") and word != "--"
            and ("=" not in word or word.endswith("=")))


_MSG_FLAGS = re.compile(r"-[a-zA-Z]*m|--message=?")


def _flag_body_position(region, i, matches):
    """Whether region[i] opens prose owned by a selected flag."""
    return bool(matches(_preceding_word(region, i)))


def _positional_body_position(region, i, unused):
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


def _verdict_body_position(region, i, unused):
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


_UHEREDOC_TAGS = re.compile(r"<<(-?)[ \t]*([A-Za-z_][A-Za-z0-9_]*)")


def _mask_quoted_line(line):
    out = list(line)
    quote = None
    esc = False
    for i, ch in enumerate(line):
        if esc:
            if quote:
                out[i] = " "
            esc = False
            continue
        if ch == "\\" and quote != "'":
            if quote:
                out[i] = " "
            esc = True
            continue
        if quote:
            out[i] = " "
            if ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
            out[i] = " "
    return "".join(out)


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


_QHEREDOC_TAGS = re.compile(r"<<-?[ \t]*(?:'([^']+)'|\"([^\"]+)\")")


def _excise_quoted_heredocs(command):
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
    """
    lines = command.split("\n")
    out, i = [], 0
    while i < len(lines):
        out.append(lines[i])
        tags = _QHEREDOC_TAGS.findall(lines[i])
        i += 1
        for a, b in tags:                # bodies follow in operator order
            tag = a or b
            while i < len(lines) and lines[i].strip() != tag:
                i += 1
            i += 1                       # the terminator line is data too
    return "\n".join(out)


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
            body = ch == '"' and body_position(region, i, body_arg)
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
     # real range WITH a pathspec and must still fire (@codex-3).
     re.compile(r"\bgit\s+(?:-C\s+\S+\s+)?diff\b"
                r"(?![^|;&]*\.\.\.)"
                r"[^|;&]*?\s[\w./@{}^~-]+\.\.[\w./@{}^~-]+"),
     "TWO-DOT DIFF ON A LANE. `git diff A..B` is a plain A-to-B diff, so on a "
     "branch BEHIND its base it renders the BASE'S OWN commits as differences "
     "— they read exactly like files your lane touched. Measured today: a "
     "6-file lane reported 8, and the 2 extras were another seat's live work. "
     "Use three dots (merge-base) to ask what YOUR lane changes: "
     "git diff --name-only origin/main...HEAD"),

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
    and @codex-3 broke both by moving it: `pipefail` set AFTER the pipeline,
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


def argv_steers(command):
    """[(steer-id, text)] — advisory redirections for one command. NEVER
    blocks; the caller prints and exits 0 regardless.

    Pure regex over the command string the hook already carries. No file
    reads, no git, no snapshot: this runs on EVERY Bash call, and the reflex
    law is cheap-local-only."""
    out = []
    cmd = command or ""
    for sid, pattern, text in _STEERS:
        if not pattern.search(cmd):
            continue
        if _steer_suppressed(sid, cmd, pattern.search(cmd)):
            continue                 # the cure is already applied
        out.append((sid, text))
    return out


def _steer_latch(session, steer_id):
    """The once-per-(session, steer) path, RAM-side in the chat dir beside
    the stop-guard's own fired-set. Returns None when there is no session to
    key on — an unkeyable steer fires rather than latching, because silence
    is the failure mode that costs more here."""
    sid = str(session or "").strip()
    if not sid:
        return None
    # THE FULL SESSION IDENTITY, HASHED — never truncated. An 8-char prefix
    # collides: two sessions sharing it map to ONE latch and the second seat
    # is silently muted, which is the worst failure this surface has (a steer
    # that does not fire is indistinguishable from correct work). Hashing
    # keeps the filename bounded without throwing identity away, which a
    # prefix does. @codex-3 caught it; I have a premise about exactly this
    # and wrote the truncation anyway.
    import hashlib
    key = hashlib.blake2b(sid.encode("utf-8"), digest_size=8).hexdigest()
    return os.path.join(chat_dir(), "ptusteer.%s.%s" % (pk.slug(steer_id), key))


def steer_unfired(session, steer_id):
    """True the FIRST time this steer is seen in this session; latches after.
    Fail-open in both directions — an unreadable latch fires (a missed steer
    is worse than a repeated one) and an unwritable latch simply does not
    latch."""
    path = _steer_latch(session, steer_id)
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
    command = command or ""
    # Bash removes backslash-newline before parsing redirections; mirror that
    # only for heredoc ownership so a continued opener stays one command line.
    blocked = _guard_unquoted_heredocs(command.replace("\\\n", ""))
    if blocked:
        return blocked
    command = _excise_quoted_heredocs(command)
    for segment in _shell_segments(command):
        blocked = _argv_guard_segment(segment)
        if blocked:
            return blocked
    return None


def cmd_argv_guard(args):
    """chat argv-guard --hook-json — the PreToolUse gate over Bash.

    Reads the hook payload, applies argv_guard to tool_input.command, and
    exits 2 with the cure when it matches. Everything else — wrong tool,
    unreadable payload, helm's own bugs — exits 0: a guard on EVERY Bash
    call must fail open or it wedges the fleet (the stop-guard's law).
    """
    try:
        d = json.load(sys.stdin)
        if d.get("tool_name") != "Bash":
            return 0
        cmd = (d.get("tool_input") or {}).get("command") or ""
        blocked = argv_guard(cmd)
        if not blocked:
            # STEERS RIDE THE PASS PATH ONLY. A blocked command never runs,
            # so its cure is the only thing worth saying; stacking advice on
            # top of a refusal buries the refusal. Advisory always: printed,
            # then exit 0, because a redirection that can stop your work is a
            # gate wearing the wrong name.
            for sid, text in argv_steers(cmd):
                if steer_unfired(d.get("session_id"), sid):
                    print("[helm steer] " + text, file=sys.stderr)
            return 0
        cure, why = blocked
        # three cures, one law: the block must name the safe route for ITS
        # surface — an undiscoverable safe path is no safe path
        if cure == "verdict":
            print("[helm argv-guard] BLOCKED: %s. The immutable verdict evidence "
                  "would be mangled and the substitution RUNS ON YOUR BOX "
                  "(measured: a FIX verdict lost its backticked command after "
                  "the shell executed it). Put the evidence in a shell variable "
                  "through a quoted heredoc, then pass the variable:\n"
                  "  evidence=$(cat <<'EOF'\n  ...evidence...\nEOF\n  )\n"
                  "  helm dispatch verdict ID TIP --fix \"$evidence\"\n"
                  "or single-quote evidence that needs no apostrophes."
                  % why, file=sys.stderr)
            return 2
        if cure == "commit":
            print("[helm argv-guard] BLOCKED: %s. The landed message would "
                  "be mangled and the substitution RUNS ON YOUR BOX "
                  "(measured: a commit landed reading 'the assignment test "
                  "is , and' where a backticked phrase had been). Use the "
                  "file route — write the message with the Write tool or a "
                  "quoted heredoc, then:\n"
                  "  git commit -F <file>\n"
                  "or single-quote a message that needs no apostrophes."
                  % why, file=sys.stderr)
            return 2
        print("[helm argv-guard] BLOCKED: %s. The message body would be "
              "mangled and the substitution RUNS ON YOUR BOX (measured: "
              "a backticked git clean executed in the shared checkout). "
              "Use the literal route — pipe stdin or quote the heredoc delimiter:\n"
              "  helm chat post --room R <<'EOF'\n  ...body...\n  EOF\n"
              "or single-quote a body that needs literal backticks."
              % why, file=sys.stderr)
        return 2
    except Exception:
        return 0                       # fail-open, always


def log_flush(rooms=None):
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
    from . import meld
    meld.flush_lifecycle(rooms=rooms)
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
    for room in rooms:
        rows, _total = read(room)
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
        appended += len(new)
        if new or room not in state:
            state[room] = {"n": len(rows), "tail": _fp(rows[-1]) if rows else None}
    if lines:
        os.makedirs(journal_dir(), exist_ok=True)
        path = os.path.join(journal_dir(),
                            "chat-%s.log" % time.strftime("%Y-%m-%d"))
        with open(path, "a", encoding="utf-8") as f:
            f.write("".join(x + "\n" for x in lines))
        pk.write_json(_flush_state_path(), state)
    return appended


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
              "ack", "pending", "status", "claim", "release", "claims",
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
HELP = {
    "wait": "usage: helm chat wait [--seat S] [--room R] [--any] [--follow] "
            "[--ambient] [--timeout SECONDS]\n"
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
            "  for a quiet room.",
    "join": "usage: helm chat join [--seat S] [--room R]  (register this "
            "session's seat; hooks run it on session start)",
    "deliver": "usage: helm chat deliver [--seat S] [--room R]  (drain the "
               "seat's pending rows once — the boundary hook's verb; "
               "advances the delivery cursor)",
    "delegation-stop": "usage: helm chat delegation-stop --hook-json  "
                       "(SubagentStop lifecycle hook; tombstones the exact "
                       "completed agent's delegation evidence)",
    "stop-guard": "usage: helm chat stop-guard [--seat S] [--room R]  (the "
                  "idle gate: rc 2 lists every blocker — unread inbox, live "
                  "leases; rc 0 passes)",
    "seats": "usage: helm chat seats [--all] [--room R]  (the roster: "
             "presence, pending, home room, todo; --all shows absent seats)",
    "seat": "usage: helm chat seat rename <sid|oldname> <newname> | "
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
               "CONSUMED: SENT-not-SEEN (the\n"
               "  recipient never surfaced it — dead / away / wedged?) or "
               "SEEN-not-ACTED (surfaced, not\n"
               "  yet acked). Acked rows drop off — a stranded word is a "
               "visible object, not silent loss.",
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
    "post": "usage: helm chat post <text...> [--room R] [--seat S] "
            "[--dm SEAT] [--reply-to <id|n>]\n"
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
                  "over Bash: blocks a messaging-verb command whose "
                  "double-quoted body carries backticks or $( — the shell "
                  "would execute them and mangle the message before helm "
                  "sees argv. Points at a quoted-delimiter stdin heredoc. "
                  "Fail-open on "
                  "everything else)",
    "log-flush": "usage: helm chat log-flush [--room R] [--install-timer "
               "[--interval SEC] [--apply]]  (append new rows to "
                 "the disk journal)",
    "node": "usage: helm chat node up|down|status  (supervise the chat "
            "tmpfs node)",
    "transport": "usage: helm chat transport status | ack --profile NAME | "
                 "ack --all  (ACK retires dead/renamed incidents; it does not "
                 "claim signing recovered)",
    # meld/council/standup HELP is populated dynamically by the MELD_VERBS
    # loop below (each spelling in its own voice) — main's static "meld" entry
    # is superseded by that restructure, so it is dropped here on purpose.
    # verdict/reveal EXIST as dispatchable verbs but are deferred to 0.3
    # (council — design doc §11): --help answers honestly with the deferral
    # instead of the bare rc-2 message the verb itself returns.
    # the 0.3 deferral is CASHED ("council: cash the 0.3 deferral —
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


def _pop_flag(args, name):
    """Pop `<name> VALUE` out of a verb's argv -> the value (None when absent
    or valueless) — the same flag shape --seat/--dm already use."""
    if name not in args:
        return None
    i = args.index(name)
    v = args[i + 1] if i + 1 < len(args) else None
    del args[i:i + 2]
    return v


def _seat_flag(args):
    """Pop `--seat S` out of a verb's argv: the caller's DECLARED display
    name. None when absent. NOTE: --seat NEVER selects the signer — use
    _seat_actor for any verb that posts/signs/acks (codex-3 xrev 2026-07-23);
    this raw popper stays for the non-signing verbs (mute, status, wait…)."""
    if "--seat" not in args:
        return None
    i = args.index("--seat")
    v = args[i + 1] if i + 1 < len(args) else None
    del args[i:i + 2]
    return v


def _seat_actor(args):
    """Resolve the acting seat for a SIGNING verb (post/reply/react/dm/ack) +
    enforce --seat as an ASSERTION, never a signer selector. The row identity
    AND the signer are the AMBIENT session identity (whoname() —
    HELM_CHAT_NAME / the session-bound seat); the signer profile is the
    ambient HELM_CELL_PROFILE (cell.profile_name). --seat may only ASSERT it:
      omitted            -> the ambient actor
      == ambient (casefold) -> the ambient actor (assertion satisfied)
      != ambient         -> (None, err): REFUSE the whole verb BEFORE any row
                            append / ACK transition / signer call — a seat may
                            not act or sign AS ANOTHER (was: --seat drove
                            who= AND profile=, so `--seat kimi` signed as kimi;
                            confirmed forgeable, codex-3 9/10).
    FOOTGUN SCOPE, honestly: a same-user process can still forge identity by
    setting HELM_CHAT_NAME/HELM_CELL_PROFILE itself — this prevents ACCIDENTAL
    --seat drift + the wrong-signer class, NOT a malicious local peer (that
    needs the owner-key trust domain, held by the owner). Returns (actor, None) or
    (None, err)."""
    claimed = _seat_flag(args)          # pops --seat (None if absent)
    ambient = whoname()
    if claimed and str(claimed).strip().casefold() != str(ambient).casefold():
        return None, ("--seat %r cannot act as another seat: this session is "
                      "%r — set HELM_CHAT_NAME to your own name to act as it"
                      % (claimed, ambient))
    # DISPUTED AMBIENT IDENTITY REFUSES THE SIGNING VERB WHOLE (2026-08-02):
    # when the declared env name and the session's roster binding answer
    # DIFFERENT seats, a post/reply/react/dm/ack under either would be signed
    # testimony from a contested author — refuse before any row append.
    try:
        from . import seats
        dis = seats.identity_disagreement(home.session_id())
    except Exception:
        dis = None                      # an unreadable roster never blocks
    if dis:
        sid = str(home.session_id() or "")
        return None, ("this process declares %r but session %.8s is rostered "
                      "to %r — refusing to act under a disputed identity. An "
                      "inherited HELM_CHAT_NAME is free; the roster can be "
                      "corrupt; only agreement is clean. Fix: unset/re-export "
                      "HELM_CHAT_NAME, or `helm chat seat disown %s %.8s`"
                      % (dis[0], sid, dis[1], dis[1], sid))
    return ambient, None


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
        # matched @ownerson and @owner-extra for the owner name owner, which
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
    restore-journal [--apply] | dm <seat> <text...> [--seat S] |
    node up|down|status | meld|council|standup invite|join|recv|say|status |
    verdict|reveal|council-status|council-abort |
    join|deliver|stop-guard [--hook-json] | wait [--any] [--follow]
    [--ambient] [--seat S]
    | seats [--all] | status [<one-line>|--clear] [--seat S]
    | seat rename <sid|oldname> <newname>
    | seat mute|unmute <room> [--seat S] | seat mutes [--seat S]
    | seat gc [--apply] | claim|release <resource> | claims [--json]
    [--room R]"""
    args = list(args or [])
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
        seat, _serr = _seat_actor(args)
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
        # REFUSE an unrecognised LEADING flag instead of POSTING it.
        #
        # Everything post does not consume falls into the message body, so a
        # misremembered flag does not fail — it publishes. Live 2026-07-22:
        # six `--to <seat>` posts went to #main as public messages, `post
        # --help` posted the literal string "--help" fleet-wide for a day, and
        # `-h` (single dash) broadcast the same way.
        #
        # SCOPE (xrev-corrected): only the LEADING OPTION POSITIONS are
        # policed — scanning the whole body made ordinary prose about CLI
        # flags unsendable ("please use --force carefully" was refused). The
        # scan stops at the first token that is not flag-shaped, and an
        # explicit `--` ends options POSIX-style so even a body that BEGINS
        # with a flag-shaped token can be sent deliberately. Flag-shape is
        # -{1,2}<letter>, so prose starters like "-" bullets, "->" arrows and
        # em-dashes are body, never flags.
        KNOWN = ("--room", "--seat", "--dm", "--reply-to")
        k = 1
        while k < len(args):
            a = args[k]
            if a == "--":               # end of options; the rest is body
                del args[k]
                break
            if not re.match(r"^-{1,2}[A-Za-z]", a):
                break                    # body begins
            # every KNOWN flag was consumed above, so any surviving
            # flag-shaped leading token is unrecognised by construction
            print("helm chat: unknown flag %s — REFUSING to post it as text."
                  % a, file=sys.stderr)
            print("  post accepts: %s" % ", ".join(KNOWN), file=sys.stderr)
            if a in ("--to", "--recipient", "--text"):
                print("  to reach ONE seat privately use --dm <seat>; without "
                      "it every post is PUBLIC to the room.", file=sys.stderr)
            elif a in ("--help", "-h"):
                print("  usage: helm chat post <text...> [--room R] [--seat S] "
                      "[--dm SEAT] [--reply-to <id|n>]", file=sys.stderr)
                print("  no body argument = read from stdin (pipe it or use "
                      "a quoted heredoc delimiter for literal backticks/$)",
                      file=sys.stderr)
                print("  nothing was sent — bare `helm chat post --help` asks "
                      "usage; `--` before the body sends it.", file=sys.stderr)
            else:
                print("  to send a message that STARTS with a flag-shaped "
                      "token, put `--` before it.", file=sys.stderr)
            return 2
        text = " ".join(args[1:]).strip()
        if not text and not sys.stdin.isatty():
            text = sys.stdin.read().strip()
        if not text:
            print("usage: helm chat post <text...> [--room R] [--seat S] "
                  "[--dm SEAT] [--reply-to <id|n>]\n"
                  "  or pipe the body on stdin / use <<'EOF' with no text argument",
                  file=sys.stderr)
            return 2
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
        # guard_tail (the settled guard-tail precedent): every remaining token must be a
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
            print("helm chat [%s]: no messages — post one: helm chat post "
                  "<text>%s" % (label, " (or: helm chat dm <seat> <text>)"
                                if label == "dm" else ""))
        consume(room, total)
        return 0
    if verb == "react":
        seat, _serr = _seat_actor(args)
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
        try:
            n = log_flush(rooms=[room] if room_given else None)
        except Exception as exc:
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
        print("helm chat: log-flush appended %d row%s -> %s" % (
            n, "s"[:n != 1], journal_dir()))
        return 0
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
          "verify|log-flush|restore-journal|argv-guard|node|transport|meld|"
          "council|standup|roster|%s)"
          % (verb, "|".join(SEAT_VERBS)), file=sys.stderr)
    return 2
