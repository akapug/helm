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
off chat is UNSIGNED BY DEFAULT, not a failure. Join/token caches follow
HELM_CHAT_DIR (RAM at the production default; an override owns their storage
location), while failure state is always forced onto tmpfs. Transport is
NODE-AGNOSTIC (HELM_CHAT_NODE_URL, else node-state url, else :8898) — the node
migration just repoints it.

The log-after leg — the ONLY disk writer here (the canon's durable-record
layer): `helm chat log-flush` appends delivered history OUT-OF-BAND to
<helm-home>/helm/journal/chat-<date>.log, idempotent via a per-room
high-water mark, disable with HELM_CHAT_LOG=0. Never called from send/read.

Emojis (PRD addendum): shortcodes expand at post time on every surface
(:fire: -> 🔥, emoji.py), reactions ride the same transport as typed rows
{react, tts, tfrom} rendered inline under their target. Reacting is a TOGGLE
per (reactor, emoji, target): the second identical react appends a tombstone
row ({un: true}) instead of a duplicate, and every renderer aggregates
last-row-wins per reactor — so historical duplicate rows self-heal to one on
read. The reactor's identity rides the signed digest (react|tts|tfrom|emoji|
reactor) when a signer is available; unsigned reacts still land, tagged.

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
only, builders.dev style: a reply renders with a compact quote of its parent,
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
import json
import math
import os
import re
import sys
import time
import unicodedata

from . import home, pk
# cell + emoji import lazily inside the paths that use them — the delivery
# lane's PostToolUse hook rides this module on EVERY tool call fleet-wide,
# and its fast path must pay interpreter+import cost for nothing it won't use.

DEFAULT_DIR = "/dev/shm/helm-chat"
SIZE_CAP = 2 * 1024 * 1024  # per-room rotation threshold — RAM etiquette
POLL_S = 2.0                # --follow poll cadence (the web panel matches)
CHAT_TAG = "chat:b2b:"      # algorithm-tagged digest, premise.py's pattern
REPLY_TAG = "chat:reply:b2b:"   # DISJOINT payload space for parent-bound rows
REACT_TAG = "chat:react:b2b:"   # ditto for reactions (v2 shared the post tag)
CHAT_TOPIC = "helm.chat"    # the signed turn's event topic on the room node
DM_PREFIX = "dm-"           # reserved room-name namespace: the private lanes
FIELD_SEP = "\x1e"          # ASCII RS: fields cannot be slid into one another
QUOTE_CHARS = 72            # the quoted parent's snippet budget (one line)
SIGN_FAILURES_FILE = ".sign-failures.json"  # RAM-only per-profile owner truth
SIGN_FAILURES_ROOT = "/dev/shm/helm-chat-failures"
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
    """HELM_CHAT_DIR else the RAM room dir — env read through home.env."""
    return home.env("CHAT_DIR") or DEFAULT_DIR


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
    web channel list — a room is born the first time anything posts to it."""
    d = chat_dir()
    if not os.path.isdir(d):
        return []
    return sorted(n[:-6] for n in os.listdir(d) if n.endswith(".jsonl"))


def marker_path(room="main"):
    return os.path.join(chat_dir(), pk.slug(room) + ".owner-unread")


def _ensure_dir():
    d = chat_dir()
    os.makedirs(d, mode=0o700, exist_ok=True)
    os.chmod(d, 0o700)  # presence-chat is the operator's — never group-readable
    return d


def whoname():
    """$HELM_CHAT_NAME, else a session-derived AGENT name — never the operator's
    identity. The unix login is the operator's machine account (pug/david); an
    agent CLI post that fell through to it impersonated the owner in the room
    (owner-flagged 2026-07-19). The operator's own surfaces name themselves
    explicitly (web posts as 'david'; `helm --human` sets HELM_CHAT_NAME), so a
    bare CLI post is ALWAYS an agent — it gets an agent name, never the login.
    A session already in the roster answers with its SEAT name (posts and
    deliveries speak one name — the rename verb rebinds both); an unknown
    session gets seats.auto_name's meaningful project+family name, and the
    opaque agent-<sid8> hex survives only as the fail-open floor."""
    name = home.chat_name()   # THE validated seam: a hostile name is REJECTED
    if name:                  # here (home.SeatNameError), never posted as `from`
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
    disk-backed _global/.state (the send path never writes disk)."""
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


@contextlib.contextmanager
def _sign_failure_lock():
    """Serialize the shared RAM map across fleet poster processes."""
    import fcntl
    d = sign_failures_dir()
    os.makedirs(d, mode=0o700, exist_ok=True)
    os.chmod(d, 0o700)
    with open(_sign_failures_lock_path(), "a") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


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


def sign_failures():
    """Every uncleared profile failure, newest first, projected without the
    internal epoch fields. Reads the same RAM owner that send updates. The
    no-incident read path is side-effect free (doctor/status stay read-only)."""
    if not os.path.exists(sign_failures_path()):
        return []
    try:
        with _sign_failure_lock():
            state = pk.read_json(sign_failures_path(), {}) or {}
    except Exception:
        return []
    if not isinstance(state, dict):
        return []
    now, rows = time.time(), []
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


def _record_sign_failure(profile, failure):
    """Upsert one exact profile. Event time owns reason/last; every active
    failure still increments count even when its process reaches the lock late."""
    p = _profile(profile)
    d = _normal_diag(failure)
    now, stamp = d["_event_epoch"], d["_event_ts"]
    candidate = {"profile": p, "code": d["code"], "reason": d["reason"],
                 "first_failure": stamp, "last_failure": stamp,
                 "failure_count": 1, "remediation": d["remediation"],
                 "_first_epoch": now, "_last_epoch": now}
    with _sign_failure_lock():
        state = pk.read_json(sign_failures_path(), {}) or {}
        if not isinstance(state, dict):
            state = {}
        old = state.get(p) if isinstance(state.get(p), dict) else {}
        success = _epoch(old.get("_success_epoch"))
        last = _epoch(old.get("_last_epoch"))
        if success >= now:
            return _failure_public(candidate, now)
        active = _active_failure(old)
        if last > now:
            if active:
                old = dict(old)
                old["failure_count"] = _count(old.get("failure_count")) + 1
                first = _epoch(old.get("_first_epoch"), last)
                if now < first:
                    old.update(first_failure=stamp, _first_epoch=now)
                state[p] = old
                pk.write_json(sign_failures_path(), state)
                return _failure_public(old, time.time())
            return _failure_public(candidate, now)
        candidate.update(
            first_failure=old.get("first_failure")
            if active and isinstance(old.get("first_failure"), str) else stamp,
            failure_count=(_count(old.get("failure_count")) if active else 0) + 1,
            _first_epoch=_epoch(old.get("_first_epoch"), now) if active else now,
            _success_epoch=success)
        state[p] = candidate
        pk.write_json(sign_failures_path(), state)
    return _failure_public(candidate, now)


def _clear_sign_failure(profile, succeeded_at=None):
    """Record a per-profile signed-success watermark in RAM. Keeping the
    watermark (not just deleting a failure) prevents a delayed older failure
    process from re-degrading after recovery. A success older than the current
    failure clears nothing."""
    p = _profile(profile)
    succeeded_at = _epoch(succeeded_at, time.time())
    try:
        with _sign_failure_lock():
            state = pk.read_json(sign_failures_path(), {}) or {}
            if not isinstance(state, dict):
                state = {}
            old = state.get(p) if isinstance(state.get(p), dict) else {}
            last = _epoch(old.get("_last_epoch"))
            prior = _epoch(old.get("_success_epoch"))
            active = _active_failure(old)
            if last > succeeded_at:
                return False
            state[p] = {"profile": p,
                        "_success_epoch": max(prior, succeeded_at)}
            pk.write_json(sign_failures_path(), state)
            return active
    except Exception:
        return False


def acknowledge_sign_failures(profile=None):
    """Operator retirement for dead/renamed profiles. This is an explicit ACK,
    not a recovery claim; its watermark prevents delayed pre-ack failures from
    resurrecting the incident. Returns acknowledged exact profile names."""
    now, stamp = time.time(), pk.now_ts()
    try:
        with _sign_failure_lock():
            state = pk.read_json(sign_failures_path(), {}) or {}
            if not isinstance(state, dict):
                state = {}
            targets = [_profile(profile)] if profile is not None else [
                p for p, rec in state.items()
                if isinstance(p, str) and _active_failure(rec)]
            done = []
            for p in targets:
                rec = state.get(p)
                if not _active_failure(rec):
                    continue
                state[p] = {"profile": p, "_success_epoch": now,
                            "acknowledged_at": stamp}
                done.append(p)
            if done:
                pk.write_json(sign_failures_path(), state)
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
        d = _normal_diag(failure)
        stamp = d["_event_ts"]
        rec = {"profile": _profile(profile),
               "code": d["code"], "reason": d["reason"],
               "first_failure": stamp, "last_failure": stamp,
               "failure_count": 1, "age_s": 0, "last_age_s": 0,
               "remediation": "%s; RAM incident retention also failed: %s" % (
                   d["remediation"], _safe_reason(
                       "%s: %s" % (exc.__class__.__name__, exc)))}
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


def transport_status():
    """The ONE transport truth for CLI/TUI/web/doctor. A reachable node plus an
    executable signer is only *ready*; any profile whose attempted signed turn
    fell back remains DEGRADED until that SAME profile completes a signed turn.
    The persistent incident fields come from RAM, never disk coordination."""
    from . import cell
    u = node_url()
    signer = cell.bin_ready()
    failures = sign_failures()
    probe_error = None
    try:
        h = node_head(u) if u else None
    except Exception as exc:
        h = None
        probe_error = "%s: %s" % (exc.__class__.__name__, exc)
    out = {"mode": "unsigned", "url": u,
           "head": h.get("chain_index") if isinstance(h, dict) and h else None,
           "signer": signer}
    if failures:
        out.update(failures[0], mode="degraded", state="DEGRADED",
                   failed_profiles=failures)
        return out
    if u and signer and h is None:
        reason = "configured chat node unreachable at %s" % u
        if probe_error:
            reason += " (%s)" % probe_error
        f = _transient_failure(
            cell.profile_name(), "node_unreachable", reason)
        out.update(f, mode="degraded", state="DEGRADED",
                   failed_profiles=[f])
        return out
    if h is not None:
        out["mode"] = "signed" if signer else "unsigned (no signer)"
    return out


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
            else "%s%s" % (st.get("mode", "unknown").upper(),
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
    DREGG_* plus the legacy meld-style names — the seam drives either bin."""
    u = node_url() or ""
    env = {"MELD_NODE_URL": u, "DREGG_NODE_URL": u}
    if token:
        env["MELD_NODE_TOKEN"] = token
        env["DREGG_API_TOKEN"] = token
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
    idempotent `join` (creates + faucet-funds on first use) aimed at the room
    node via env_extra. Returns (cell_hex, None, True) on success, else
    (None, reason, launched) — `launched` False ONLY when the signer binary
    never ran (local launch failure), so the caller never revives over it."""
    from . import cell
    cache = pk.read_json(cells_path(), {}) or {}
    hexid = cache.get(profile)
    if hexid:
        return hexid, None, True
    rc, out, err = cell.run_bin(["join", "--profile", profile], timeout=30,
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


def _sign_send(payload, profile):
    """One signed self-write turn. Returns (send-info, None) or
    (None, structured diagnostic). One recovery lap; both send attempts and
    every revive/faucet refusal survive into the final operator reason."""
    from . import cell
    if not cell.bin_ready():
        return None, _diag(
            "signer_unavailable",
            "no signer — HELM_CELL_BIN is unset or not executable")
    token = _node_token()
    hexid, err, launched = _room_cell(profile, token)
    if err:
        if not launched:
            return None, _diag("signer_launch_failed", err)
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
             "--topic", CHAT_TOPIC, payload],
            timeout=30, env_extra=_env_extra(token))
        if rc is None:
            return None, _diag("signer_launch_failed", err)
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


def _signed_row(row, payload_text, profile, sign):
    """Common signing owner for posts/reactions. The RAM row ALWAYS lands.
    A failed attempted signature is stamped + retained per profile; only an
    observed signed send clears that profile's incident. On the production
    `sign=None` path, no signer/node URL is configured-off v1, not an alarm.
    `sign=True` is the explicit force/test seam and therefore records inability
    to honor that forced attempt; `sign=False` always skips."""
    p = profile
    try:
        from . import cell
        p = p or cell.profile_name()
        if sign is None:
            if not cell.bin_ready() or not node_url():
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
            lf = open(os.path.join(chat_dir(), pk.slug(room) + ".lock"), "a")
            fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
        except OSError:
            lf = None
        yield
    finally:
        if lf is not None:
            try:
                fcntl.flock(lf.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            lf.close()


def _append(row, room):
    """ONE serialized write path for every room writer: id-stamp, append the
    whole row in one write, flush, then rotate — all under the room lock.
    The stable per-row id is what delivery cursors key on (codex H5); rows
    predating it (or hand-written) simply have no id and never match one."""
    row.setdefault("id", os.urandom(6).hex())
    path = room_path(room)
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)  # dm/ lane
    with _room_lock(room):
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            f.flush()
        _rotate(path)
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


def post(text, room="main", who=None, profile=None, sign=None, origin=None,
         dm=None, ambient=False, reply_to=None):
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

    `dm` names the ONE recipient (exact token — seats.dm resolves it): the
    row is stamped {dm} and lands in the recipient's private lane, OVERRIDING
    `room` — a DM never touches a room file (no fanout, by construction).

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
    alone would have reached."""
    _ensure_dir()
    from . import emoji
    text = emoji.expand(text)
    row = {"ts": pk.now_ts(), "from": who or whoname(), "text": text}
    if dm:
        row["dm"] = dm
        room = dm_room(dm)
    if origin:
        row["origin"] = origin
    if ambient and not dm:
        row["ambient"] = 1
    if reply_to:
        row.update(_parent_fields(room, reply_to))
    _touch_poster_presence(row["from"])
    return _append(_signed_row(row, text, profile, sign), room)


def _touch_poster_presence(name):
    """Presence-on-post: a seat that SPEAKS is alive, beacon or no beacon — so
    keep its roster row fresh and the reaper never drops a live-but-idle poster
    (roster-truth, owner-caught 2026-07-21). Best-effort + local import
    (chat<-seats would cycle); owner/broadcast names are not seats — skip them
    so no spurious presence file is minted."""
    try:
        from . import seats
        if not name or name.lower() in seats.owner_names():
            return
        seats.touch_seen(name)
    except Exception:
        pass


def react(target, code, room="main", who=None, profile=None, sign=None):
    """TOGGLE a reaction. target: 1-based message ordinal (negatives count
    from the end) or an explicit (ts, from) pair (the web panel's form).
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


def _rotate(path, cap=None):
    """Past the cap, keep the NEWEST half (atomic). The dropped half was
    presence-chat, not a record; pollers' since counters self-heal (read()
    resets a past-the-end since)."""
    cap = SIZE_CAP if cap is None else cap
    try:
        if os.path.getsize(path) <= cap:
            return False
    except OSError:
        return False
    # split on exactly "\n" (the writer's terminator) — str.splitlines() also
    # splits on U+2028/U+2029/\x85 INSIDE a message's text, tearing the JSON row
    with open(path, encoding="utf-8", errors="replace") as f:
        lines = [x for x in f.read().split("\n") if x]
    pk.atomic_write(path, "".join(x + "\n" for x in lines[len(lines) // 2:]))
    return True


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
    stamp = (ts[11:16] or "--:--") if hhmm else (ts or "?")
    tag = _transport_tag(m)
    # identity fields are display-laundered (_dsan) so a name column can never
    # reshape the terminal — defense-in-depth beneath the validated join seam.
    if m.get("react"):
        return "%s %s %s %s -> %s@%s%s" % (
            stamp, _dsan(m.get("from") or "?"),
            "un-reacted" if m.get("un") else "reacted",
            m["react"], _dsan(m.get("tfrom") or "?"),
            str(m.get("tts") or "")[11:16] or "--:--", tag)
    q = quote_of(m, idx) if idx else None
    quote = ' ↳%s "%s"' % (_dsan(q[0]), q[1]) if q else ""
    n = (idx or {}).get("replies", {}).get(tkey(m), 0)
    thread_tail = " ↩%d" % n if n else ""
    if m.get("dm"):     # a DM row is a DM everywhere it renders — never a
        return "%s %s%s -> @%s (dm): %s%s%s" % (stamp, _dsan(m.get("from") or "?"),
                                                quote, _dsan(m["dm"]),
                                                m.get("text") or "",
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
                OR the row is a signed REPLY carrying no payload at all, which
                cannot be legacy: reply_to and the recorded {payload} shipped
                in the SAME change, so signed+threaded+payload-less means the
                payload was STRIPPED to dodge this check (bug-class
                verify-downgrades-to-legacy-when-payload-stripped)
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
                 ("ok" if stored == want else "MISMATCH") if stored else
                 "MISMATCH" if is_reply(m) else "legacy")
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
            msgs = rows[since if 0 <= since <= total else 0:]
            for m in msgs:
                print(_fmt(m, idx=idx), flush=True)
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
    if rooms is None:
        rooms = list_rooms()
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
    return "%s%s: %s%s" % (_dsan(m.get("from") or "?"), ref, m.get("text") or "", tag)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

SEAT_VERBS = ("join", "deliver", "stop-guard", "wait", "seats", "seat", "dm",
              "status", "claim", "release", "claims", "verdict", "reveal")
                                               # the delivery lane — seats.py
                                               # (verdict/reveal answer with
                                               # the 0.3 council deferral)

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
            "[--timeout SECONDS]\n"
            "  Block until the next word arrives for the seat (DMs, "
            "@mentions, home room, @all —\n"
            "  any live room). No --timeout = wait forever; with it, rc 0 + "
            "the line on a match,\n"
            "  rc 1 on timeout. --seat declares the waiting identity "
            "(default: this session's seat).\n"
            "  --room sets the primary room (--any watches ONLY that room, "
            "every row, no cursor).\n"
            "  --follow never returns on a match: it streams each matching "
            "row as one flushed\n"
            "  line (the idle-wake beacon) and returns rc 0 only on timeout.",
    "join": "usage: helm chat join [--seat S] [--room R]  (register this "
            "session's seat; hooks run it on session start)",
    "deliver": "usage: helm chat deliver [--seat S] [--room R]  (drain the "
               "seat's pending rows once — the boundary hook's verb; "
               "advances the delivery cursor)",
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
    "claim": "usage: helm chat claim <resource> [--ttl SECONDS] [--seat S] "
             "[--lease ID to extend]  (keep the printed lease id — it is the "
             "release capability)",
    "release": "usage: helm chat release <resource> --lease ID [--seat S]",
    "claims": "usage: helm chat claims [--room R]  (the live leases)",
    "post": "usage: helm chat post <text...> [--room R] [--seat S] "
            "[--dm SEAT] [--reply-to <id|n>]\n"
            "  Leading option positions are policed, the body is never "
            "scanned: prose ABOUT\n"
            "  --help past the leading position sends fine. Bare `post "
            "--help` = this usage\n"
            "  (rc 0); `post --help <body>` refuses loudly (rc 2, nothing "
            "sent); `post --\n"
            "  <body>` sends a body that STARTS with a flag-shaped token.",
    "reply": "usage: helm chat reply <id|n> <text...> [--room R] [--seat S]  "
             "(id = the parent row's id, n = its 1-based number, -1 = latest)",
    "read": "usage: helm chat read [--room R] [--since N] [--follow] [--dm "
            "[--seat S]]  (--follow streams new rows until killed — it never "
            "returns on its own)",
    "react": "usage: helm chat react <n> <:shortcode:|emoji> [--room R] "
             "[--seat S]  (n counts messages, 1-based; -1 = latest; same "
             "react again toggles it off)",
    "rooms": "usage: helm chat rooms  (every room: message count, "
             "owner-unread, last row)",
    "verify": "usage: helm chat verify [--room R]  (recompute signed payload "
              "digests; rc 1 on any mismatch)",
    "log-flush": "usage: helm chat log-flush [--room R]  (append new rows to "
                 "the disk journal)",
    "node": "usage: helm chat node up|down|status  (supervise the chat "
            "tmpfs node)",
    "transport": "usage: helm chat transport status | ack --profile NAME | "
                 "ack --all  (ACK retires dead/renamed incidents; it does not "
                 "claim signing recovered)",
    "meld": "usage: helm chat meld invite <peer> <topic...> | join <room> | "
            "recv <room> [--timeout S] | say <room> --marker "
            "YIELD|HOLD|DONE|ABORT <text...> | status",
    # verdict/reveal EXIST as dispatchable verbs but are deferred to 0.3
    # (council — design doc §11): --help answers honestly with the deferral
    # instead of the bare rc-2 message the verb itself returns.
    "verdict": "usage: helm chat verdict — DEFERRED to 0.3 (council, design "
               "doc §11); until it lands the verb answers with the deferral: "
               "use the room + /premise",
    "reveal": "usage: helm chat reveal — DEFERRED to 0.3 (council, design "
              "doc §11); until it lands the verb answers with the deferral: "
              "use the room + /premise",
}
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
_TEXT_VERBS = ("post", "reply", "dm", "meld")


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
    """Pop `--seat S` out of a verb's argv: the caller's DECLARED identity,
    threaded into who= AND profile= so the row records the true reactor and
    the signer attests it (same identity plumbing as a post). None when
    absent — the ambient whoname() fallback stays for un-seated calls."""
    if "--seat" not in args:
        return None
    i = args.index("--seat")
    v = args[i + 1] if i + 1 < len(args) else None
    del args[i:i + 2]
    return v


def cmd_chat(args):
    """chat post <text...> [--seat S] [--dm SEAT] [--reply-to <id|n>] |
    reply <id|n> <text...> [--seat S] | verify [--room R] | read [--since N]
    [--follow] [--dm] | rooms | react <n> <emoji> [--seat S] | log-flush |
    dm <seat> <text...> [--seat S] | node up|down|status |
    meld invite|join|recv|say|status |
    join|deliver|stop-guard [--hook-json] | wait [--any] [--follow] [--seat S]
    | seats [--all] | status [<one-line>|--clear] [--seat S]
    | seat rename <sid|oldname> <newname>
    | seat mute|unmute <room> [--seat S] | seat mutes [--seat S]
    | seat gc [--apply] | claim|release <resource> | claims  [--room R]"""
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
    if verb == "meld":                  # the mindmeld preset — meld.py
        from . import meld
        return meld.cmd(args[1:])
    if verb in SEAT_VERBS:
        from . import seats
        return seats.cmd(
            verb, args[1:], room, room_explicit=room_given,
            room_source=room_source)
    if verb in ("post", "reply"):
        seat = _seat_flag(args)
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
                  "[--dm SEAT] [--reply-to <id|n>]", file=sys.stderr)
            return 2
        if to:
            from . import seats
            row, err = seats.dm(to, text, who=seat, reply_to=ref,
                                session=home.session_id(), profile=seat)
            if err:
                print("helm chat: " + err, file=sys.stderr)
                return 1
            print("helm chat [dm] %s"
                  % _fmt(row, idx=index_rows(read(dm_room(row.get("dm")
                                                          or to))[0])))
            return 0
        row = post(text, room, who=seat, profile=seat, reply_to=ref)
        # the echo carries the quote (and the parent's new count): the poster
        # SEES which row it landed under — an unresolvable ref is visible as an
        # orphan right here, not three surfaces later
        print("helm chat [%s] %s" % (room, _fmt(row, idx=index_rows(read(room)[0]))))
        return 0
    if verb == "read":
        label = room
        if "--dm" in args:      # the recipient's own private lane
            args.remove("--dm")
            room = dm_room(_seat_flag(args) or whoname())
            label = "dm"
        since = 0
        if "--since" in args:
            try:
                since = int(args[args.index("--since") + 1])
            except (IndexError, ValueError):
                print("helm chat: --since wants an integer", file=sys.stderr)
                return 2
        if "--follow" in args:
            return _follow(room, since)
        rows, total = read(room)
        idx = index_rows(rows)            # the WHOLE room indexes the thread:
        msgs = rows[since if 0 <= since <= total else 0:]   # a quote resolves
        for m in msgs:                    # to a parent older than --since
            print(_fmt(m, idx=idx))
        if not msgs:
            print("helm chat [%s]: no messages — post one: helm chat post "
                  "<text>%s" % (label, " (or: helm chat dm <seat> <text>)"
                                if label == "dm" else ""))
        consume(room, total)
        return 0
    if verb == "react":
        seat = _seat_flag(args)
        if len(args) < 3:
            print("usage: helm chat react <n> <:shortcode:|emoji> [--room R] "
                  "[--seat S] (n counts messages, 1-based; -1 = latest; "
                  "same react again toggles it off)", file=sys.stderr)
            return 2
        try:
            n = int(args[1])
        except ValueError:
            print("helm chat: react wants a message number", file=sys.stderr)
            return 2
        row, err = react(n, args[2], room, who=seat, profile=seat)
        if err:
            print("helm chat: " + err, file=sys.stderr)
            return 1
        print("helm chat [%s] %s" % (room, _fmt(row)))
        return 0
    if verb == "log-flush":
        n = log_flush(rooms=[room] if room_given else None)
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
          "verify|log-flush|node|transport|meld|roster|%s)" %
          (verb, "|".join(SEAT_VERBS)),
          file=sys.stderr)
    return 2
