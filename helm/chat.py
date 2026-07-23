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
and tag everything else "[unsigned]". Node down -> the v1 path automatically
(fallback law: drop the signature, never the RAM property). SIGNING IS
OPT-IN: it needs the explicit cell binary (HELM_CELL_BIN — unset => off, no
PATH probe; the de-meld law), so out of the box chat is UNSIGNED BY DEFAULT
even with a live node — transport_status says "unsigned (no signer)" and no
signing leg (probe/revive/unlock) ever fires without the binary. NO transport
path writes disk, ever; RAM-side caches (join cells, node token) live in the
room dir itself. Transport is NODE-AGNOSTIC (HELM_CHAT_NODE_URL, else the
node-state url, else :8898) — the node migration just repoints it.

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
    name = home.env("CHAT_NAME")
    if name:
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


def node_head(url=None, timeout=1.5):
    """The chain head receipt: dict, {} for a live-but-empty chain, None when
    the node is down — post()'s cheap reachability probe doubles as data."""
    from . import cell
    u = url or node_url()
    if not u:
        return None
    rs = cell.get_json(u + "/api/receipts", timeout=timeout)
    if isinstance(rs, list):
        return rs[0] if rs else {}
    return None


def transport_status():
    """One probe -> {"mode", "url", "head", "signer"} — the status strip in
    `helm --human`, the web panel and doctor all read this. mode reports the
    SIGNER's truth, not just node reachability: "signed" needs the explicit
    cell binary AND a live node; a reachable node with no signer is
    "unsigned (no signer)" — the exact state where every post falls to
    [unsigned] while the node still answers (day-review #1)."""
    from . import cell
    u = node_url()
    signer = cell.bin_ready()
    h = node_head(u) if u else None
    mode = "unsigned"
    if h is not None:
        mode = "signed" if signer else "unsigned (no signer)"
    return {"mode": mode, "url": u, "head": h.get("chain_index") if h else None,
            "signer": signer}


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
    """The node lost its provisioned state (reboot wiped tmpfs; token stale;
    chain empty): re-unlock with the STORED passphrase, re-bootstrap health,
    cache the fresh token RAM-side. Returns the token or None. Chat turns
    never die on provisioning state — the provisioning exhaustion lesson,
    generalized."""
    from . import chatnode
    u = node_url()
    st = chatnode.state()
    if not (u and st.get("passphrase")):
        return None
    token, err = chatnode.unlock(u, st["passphrase"])
    if err:
        return None
    chatnode.ensure_healthy(u)
    _ensure_dir()
    pk.atomic_write(_token_path(), token or "")
    os.chmod(_token_path(), 0o600)
    return token


LOW_WATER = 3000   # ~2 signed turns (measured: a 73B digest send costs ~1442)


def _faucet(cell_hex):
    """Top up one cell from the room node's faucet (balance must never kill a
    chat turn). Fail-open — the node rate-limits faucets to 1/min/cell; a 429
    just means the retry (or the unsigned fallback) tells the truth."""
    from . import cell
    u = node_url()
    if u:
        cell.post_json(u + "/api/faucet", {"recipient": cell_hex, "amount": 10000})


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
        return None, ("join failed (rc %d): %s"
                      % (rc, (err or out).strip()[-240:])), True
    info = cell._last_json(out)
    if not (info and info.get("cell")):
        return None, "join printed no cell id", True
    _ensure_dir()
    cache[profile] = info["cell"]
    pk.write_json(cells_path(), cache)
    return info["cell"], None, True


def _sign_send(payload, profile):
    """One signed self-write turn on the room node carrying `payload`.
    (send-info, None) or (None, reason). One recovery lap (revive + faucet)
    before giving up — then the caller falls back to unsigned, loudly tagged.
    NO SIGNER => immediate, silent decline: without the explicit cell binary
    a signed turn is impossible, so the recovery lap (a live unlock POST +
    ensure_healthy against the node, burning its 5/60s unlock budget) must
    never fire — a config fact is not a fault (day-review #1)."""
    from . import cell
    if not cell.bin_ready():
        return None, ("no signer — HELM_CELL_BIN unset; posts ride the v1 "
                      "room unsigned")
    token = _node_token()
    hexid, err, launched = _room_cell(profile, token)
    if err:
        # A local signer-launch failure is not node state — decline unsigned,
        # never fire the unlock/revive lap (day-review #1, codex B1 tail).
        if not launched:
            return None, err
        token = _revive() or token
        hexid, err, launched = _room_cell(profile, token)
        if err:
            return None, err
    b = _balance(hexid)
    if b is not None and b < LOW_WATER:   # proactive top-up BEFORE the turn
        _faucet(hexid)
    rc = out = err2 = None
    for attempt in (0, 1):
        rc, out, err2 = cell.run_bin(
            ["send", "--profile", profile, "--to", hexid,
             "--topic", CHAT_TOPIC, payload],
            timeout=30, env_extra=_env_extra(token))
        if rc is None:
            return None, err2
        info = cell._last_json(out)
        if rc == 0 and info and info.get("sent"):
            return info, None
        if attempt == 0:
            token = _revive() or token   # locked node / stale token / no blocks
            _faucet(hexid)               # computron exhaustion
    return None, "send failed (rc %s): %s" % (rc, ((err2 or out) or "").strip()[-240:])


def _signed_row(row, payload_text, profile, sign):
    """Common signing leg for posts and reactions: attempt the turn when the
    transport is up; annotate the row with its receipt on success. sign=None
    probes; True forces the attempt; False skips (v1 path). FAIL-OPEN TOTAL:
    any surprise in the signing leg (a raced cache write, a raising client)
    degrades to the v1 unsigned row — the fallback law is drop the SIGNATURE,
    never the message. The signer gate comes FIRST: no cell binary => no
    node probe at all (the row is unsigned by configuration, not by
    fault).

    The payload is payload_for(row) — shape-dispatched, so the signer and
    `helm chat verify` can never drift apart — and the signed row RECORDS it
    ({payload}), making the parent binding re-derivable from the row alone."""
    try:
        from . import cell
        if sign is None:
            sign = cell.bin_ready() and bool(node_url()) \
                and node_head() is not None
        if not sign:
            return row
        payload = payload_for(row, payload_text)
        info, _err = _sign_send(payload, profile or cell.profile_name())
        if info:
            row.update(turn=info.get("turn_hash"), receipt=info.get("receipt_hash"),
                       chain=info.get("chain_index"), payload=payload)
    except Exception:
        pass
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


def _fmt(m, hhmm=True, idx=None):
    """One row, rendered. Signed rows (a recorded chain receipt) print clean;
    everything else carries the visible [unsigned] tag (fallback law).

    With an `idx` (index_rows of the room) the one-level thread renders too: a
    reply carries a compact ↳author "quote" of its parent, and a parent carries
    ↩N, its reply count. Without one — a single-row echo, an old caller — the
    line is byte-identical to before."""
    ts = str(m.get("ts") or "")
    stamp = (ts[11:16] or "--:--") if hhmm else (ts or "?")
    tag = "" if m.get("chain") is not None else " [unsigned]"
    if m.get("react"):
        return "%s %s %s %s -> %s@%s%s" % (
            stamp, m.get("from") or "?",
            "un-reacted" if m.get("un") else "reacted",
            m["react"], m.get("tfrom") or "?",
            str(m.get("tts") or "")[11:16] or "--:--", tag)
    q = quote_of(m, idx) if idx else None
    quote = ' ↳%s "%s"' % q if q else ""
    n = (idx or {}).get("replies", {}).get(tkey(m), 0)
    thread_tail = " ↩%d" % n if n else ""
    if m.get("dm"):     # a DM row is a DM everywhere it renders — never a
        return "%s %s%s -> @%s (dm): %s%s%s" % (stamp, m.get("from") or "?",
                                                quote, m["dm"],
                                                m.get("text") or "",
                                                thread_tail, tag)
    return "%s %s%s: %s%s%s" % (stamp, m.get("from") or "?", quote,
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
        out.append({"n": i, "from": m.get("from") or "?", "state": state,
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
    tag = (" {chain %s}" % m["chain"]) if m.get("chain") is not None else " [unsigned]"
    if m.get("react"):
        return "%s %s %s -> %s@%s%s" % (m.get("from") or "?",
                                        "un-reacted" if m.get("un") else "reacted",
                                        m["react"], m.get("tfrom") or "?",
                                        m.get("tts") or "?", tag)
    ref = (" ↳%s@%s" % (m.get("rfrom") or "?", m.get("rts") or m.get("reply_to")
                        or "?")) if is_reply(m) else ""
    return "%s%s: %s%s" % (m.get("from") or "?", ref, m.get("text") or "", tag)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

SEAT_VERBS = ("join", "deliver", "stop-guard", "wait", "seats", "seat", "dm",
              "claim", "release", "claims", "verdict", "reveal")
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
    | seats [--all] | seat rename <sid|oldname> <newname>
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
          "verify|log-flush|node|meld|roster|%s)" % (verb, "|".join(SEAT_VERBS)),
          file=sys.stderr)
    return 2
