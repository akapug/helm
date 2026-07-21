#!/usr/bin/env python3
"""helm chat — the human-included groupchat: one shared conv log + notify +
read/write loop, owner in the room.

Rooms live in RAM (tmpfs): /dev/shm/helm-chat/<room>.jsonl — append-only, one
JSON object per line {"ts","from","text"}, dir 0700, default room "main".
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
{react, tts, tfrom} rendered inline under their target.

The notify loop: the owner's post (web panel or `helm --human`) drops
<room>.owner-unread (the message count at post time); the shipped
owner-chat-unread reflex fires on that marker every turn until a
`helm chat read` consumes past it — identical in BOTH transports.

The owner's orca pane sidecar is exactly: helm chat read --follow
"""
import contextlib
import hashlib
import json
import os
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


def chat_dir():
    """HELM_CHAT_DIR else the RAM room dir — env read through home.env."""
    return home.env("CHAT_DIR") or DEFAULT_DIR


def room_path(room="main"):
    return os.path.join(chat_dir(), pk.slug(room) + ".jsonl")


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
    sid = os.environ.get("CLAUDE_SESSION_ID") or os.environ.get("CODEX_SESSION_ID")
    if sid:
        try:
            from . import seats
            return seats.seat_for_session(sid) or seats.auto_name(sid, os.getcwd())
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
    h = hashlib.blake2b(unicodedata.normalize("NFC", text or "").encode("utf-8"),
                        digest_size=32)
    return CHAT_TAG + h.hexdigest()


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
    env = {"MELD_NODE_URL": node_url() or ""}
    if token:
        env["MELD_NODE_TOKEN"] = token
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
            ["send", "--profile", profile, "--to", hexid, payload],
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
    fault)."""
    try:
        from . import cell
        if sign is None:
            sign = cell.bin_ready() and bool(node_url()) \
                and node_head() is not None
        if not sign:
            return row
        info, _err = _sign_send(digest_payload(payload_text),
                                profile or cell.profile_name())
        if info:
            row.update(turn=info.get("turn_hash"), receipt=info.get("receipt_hash"),
                       chain=info.get("chain_index"))
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
    with _room_lock(room):
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            f.flush()
        _rotate(path)
    return row


def post(text, room="main", who=None, profile=None, sign=None, origin=None):
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
    owner through legit tooling. Principal crypto stays dregg's."""
    _ensure_dir()
    from . import emoji
    text = emoji.expand(text)
    row = {"ts": pk.now_ts(), "from": who or whoname(), "text": text}
    if origin:
        row["origin"] = origin
    return _append(_signed_row(row, text, profile, sign), room)


def react(target, code, room="main", who=None, profile=None, sign=None):
    """Attach a reaction. target: 1-based message ordinal (negatives count
    from the end) or an explicit (ts, from) pair (the web panel's form).
    `code` is a :shortcode: or a raw emoji. Returns (row, None) or
    (None, reason). Rides the same transport as a post."""
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
    row = {"ts": pk.now_ts(), "from": who or whoname(), "react": e,
           "tts": tts, "tfrom": tfrom}
    payload = "react|%s|%s|%s" % (tts, tfrom, e)
    return _append(_signed_row(row, payload, profile, sign), room), None


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
    the aggregation the TUI and any batch renderer draws under each message."""
    msgs = [m for m in rows if not m.get("react")]
    reacts = {}
    for m in rows:
        if m.get("react"):
            k = "%s|%s" % (m.get("tts") or "", m.get("tfrom") or "")
            reacts.setdefault(k, {})
            reacts[k][m["react"]] = reacts[k].get(m["react"], 0) + 1
    return msgs, reacts


def react_line(counts):
    return "  ".join("%s×%d" % (e, counts[e]) for e in sorted(counts))


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


def _fmt(m, hhmm=True):
    """One row, rendered. Signed rows (a recorded chain receipt) print clean;
    everything else carries the visible [unsigned] tag (fallback law)."""
    ts = str(m.get("ts") or "")
    stamp = (ts[11:16] or "--:--") if hhmm else (ts or "?")
    tag = "" if m.get("chain") is not None else " [unsigned]"
    if m.get("react"):
        return "%s %s reacted %s -> %s@%s%s" % (
            stamp, m.get("from") or "?", m["react"], m.get("tfrom") or "?",
            str(m.get("tts") or "")[11:16] or "--:--", tag)
    return "%s %s: %s%s" % (stamp, m.get("from") or "?", m.get("text") or "", tag)


def _follow(room, since=0):
    """Poll-print loop — the orca pane sidecar. Ctrl-C exits clean. The read
    primitive it loops on is read() (unit-tested); the loop itself is not."""
    try:
        while True:
            msgs, total = read(room, since)
            for m in msgs:
                print(_fmt(m), flush=True)
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
    d = chat_dir()
    if rooms is None:
        rooms = sorted(n[:-6] for n in os.listdir(d)
                       if n.endswith(".jsonl")) if os.path.isdir(d) else []
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
    """The log line's tail: sender + content + signature note, full fidelity."""
    tag = (" {chain %s}" % m["chain"]) if m.get("chain") is not None else " [unsigned]"
    if m.get("react"):
        return "%s reacted %s -> %s@%s%s" % (m.get("from") or "?", m["react"],
                                             m.get("tfrom") or "?",
                                             m.get("tts") or "?", tag)
    return "%s: %s%s" % (m.get("from") or "?", m.get("text") or "", tag)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

SEAT_VERBS = ("join", "deliver", "stop-guard", "wait", "seats", "seat",
              "claim", "release", "claims", "verdict", "reveal")
                                               # the delivery lane — seats.py
                                               # (verdict/reveal answer with
                                               # the 0.3 council deferral)


def cmd_chat(args):
    """chat post <text...> | read [--since N] [--follow] | rooms |
    react <n> <emoji> | log-flush | node up|down|status |
    join|deliver|stop-guard [--hook-json] | wait [--any] [--follow] [--seat S]
    | seats [--all] | seat rename <sid|oldname> <newname>
    | claim|release <resource> | claims  [--room R]"""
    args = list(args or [])
    room = "main"
    room_given = "--room" in args
    if room_given:
        i = args.index("--room")
        if i + 1 >= len(args):
            print("helm chat: --room wants a name", file=sys.stderr)
            return 2
        room = args[i + 1]
        del args[i:i + 2]
    verb = args[0] if args else "read"
    if verb == "node":
        from . import chatnode
        return chatnode.cmd_node(args[1:])
    if verb in SEAT_VERBS:
        from . import seats
        return seats.cmd(verb, args[1:], room)
    if verb == "post":
        text = " ".join(args[1:]).strip()
        if not text and not sys.stdin.isatty():
            text = sys.stdin.read().strip()
        if not text:
            print("usage: helm chat post <text...> [--room R]", file=sys.stderr)
            return 2
        print("helm chat [%s] %s" % (room, _fmt(post(text, room))))
        return 0
    if verb == "read":
        since = 0
        if "--since" in args:
            try:
                since = int(args[args.index("--since") + 1])
            except (IndexError, ValueError):
                print("helm chat: --since wants an integer", file=sys.stderr)
                return 2
        if "--follow" in args:
            return _follow(room, since)
        msgs, total = read(room, since)
        for m in msgs:
            print(_fmt(m))
        if not msgs:
            print("helm chat [%s]: no messages — post one: helm chat post <text>" % room)
        consume(room, total)
        return 0
    if verb == "react":
        if len(args) < 3:
            print("usage: helm chat react <n> <:shortcode:|emoji> [--room R] "
                  "(n counts messages, 1-based; -1 = latest)", file=sys.stderr)
            return 2
        try:
            n = int(args[1])
        except ValueError:
            print("helm chat: react wants a message number", file=sys.stderr)
            return 2
        row, err = react(n, args[2], room)
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
    if verb == "rooms":
        d = chat_dir()
        names = sorted(n[:-6] for n in os.listdir(d)
                       if n.endswith(".jsonl")) if os.path.isdir(d) else []
        if not names:
            print("helm chat: no rooms yet — helm chat post <text> starts main")
            return 0
        for n in names:
            msgs, total = read(n)
            unread = " [owner-unread]" if os.path.exists(marker_path(n)) else ""
            last = ("  last: " + _fmt(msgs[-1])) if msgs else ""
            print("  %s  %d msg%s%s%s" % (n, total, "s"[:total != 1], unread, last))
        return 0
    print("helm chat: unknown subcommand '%s' (post|read|rooms|react|"
          "log-flush|node|%s)" % (verb, "|".join(SEAT_VERBS)), file=sys.stderr)
    return 2
