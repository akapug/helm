#!/usr/bin/env python3
"""helm multiplayer — local, metaharness-agnostic shared-state transport.

The durable document model belongs to the client CRDT. Helm is a BLIND relay:
it assigns an envelope id/timestamp and appends the client's opaque update, but
never decodes, merges, interprets, or rewrites it. A browser, terminal editor,
agent harness, or future builders.dev bridge can share one adapter contract
without making Helm depend on any of them.

Presence is deliberately separate and ephemeral. Heartbeats live in their own
TTL snapshot, never enter the update log, and may vanish without affecting the
document. Both channels default to tmpfs (/dev/shm/helm-multiplayer); tests and
portable hosts override HELM_MULTIPLAYER_DIR.
"""
import contextlib
import fcntl
import hashlib
import json
import os
import secrets
import sys
import time
import unicodedata

from . import chat, home, pk

DEFAULT_DIR = "/dev/shm/helm-multiplayer"
MAX_UPDATE_BYTES = 256 * 1024
MAX_DOC_BYTES = 4 * 1024 * 1024
DEFAULT_TTL = 30
MAX_TTL = 300


def multiplayer_dir():
    return home.env("MULTIPLAYER_DIR") or DEFAULT_DIR


def default_cave():
    return home.env("MULTIPLAYER_CAVE") or home.env("CHAT_ROOM") or "main"


def actor_name():
    return home.env("MULTIPLAYER_ACTOR") or chat.whoname()


def connection_name(actor=None):
    return (home.env("MULTIPLAYER_CONNECTION") or home.session_id()
            or actor or actor_name())


def _identity(value, field):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("%s must be a non-empty name" % field)
    value = unicodedata.normalize("NFC", value.strip())
    if any(unicodedata.category(c) == "Cc" for c in value):
        raise ValueError("%s may not contain control characters" % field)
    if len(value.encode("utf-8")) > 240:
        raise ValueError("%s exceeds 240 bytes" % field)
    return value


def _key(value, field):
    value = _identity(value, field)
    digest = hashlib.blake2b(value.encode("utf-8"), digest_size=16).hexdigest()
    return "%s-%s" % (pk.slug(value)[:36], digest)


def _cave_dir(cave):
    return os.path.join(multiplayer_dir(), _key(cave, "cave"))


def _ensure_cave(cave):
    root = multiplayer_dir()
    path = _cave_dir(cave)
    os.makedirs(path, mode=0o700, exist_ok=True)
    os.chmod(root, 0o700)
    os.chmod(path, 0o700)
    return path


def _locked_text(path):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    f = open(path, "a+", encoding="utf-8")
    fcntl.flock(f.fileno(), fcntl.LOCK_EX)
    return f


@contextlib.contextmanager
def _relay_lock(path, exclusive):
    """A sidecar lock serializes file creation as well as append/read. Locking
    the data inode itself leaves a first-publish race between open() and flock()."""
    lock_path = path + ".lock"
    os.makedirs(os.path.dirname(lock_path), mode=0o700, exist_ok=True)
    with open(lock_path, "a+b") as f:
        os.chmod(lock_path, 0o600)
        fcntl.flock(f.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        yield


def _cursor(generation, offset):
    return "%s:%d" % (generation, offset)


class LocalRelay:
    """Append/read adapter for opaque CRDT updates on this machine.

    Cursors bind a file generation to a complete-record byte boundary. Reads
    share-lock against publishers and never advance over a partial tail. A full
    relay refuses writes instead of rotating: cursors never silently change
    meaning. The tmpfs cave is disposable; durable history is the client's CRDT
    concern.
    """

    name = "local"

    def path(self, cave, doc):
        return os.path.join(_cave_dir(cave), _key(doc, "doc") + ".updates.jsonl")

    def _header(self, f):
        f.seek(0)
        try:
            header = json.loads(f.readline().decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise ValueError("document relay has a corrupt header")
        if not isinstance(header, dict):
            raise ValueError("document relay has an invalid header")
        generation = header.get("generation")
        if header.get("kind") != "helm.multiplayer" or not generation:
            raise ValueError("document relay has an invalid header")
        return generation, f.tell()

    def _prepare(self, f, cave, doc):
        f.seek(0, os.SEEK_END)
        end = f.tell()
        if not end:
            generation = secrets.token_hex(12)
            header = {"v": 1, "kind": "helm.multiplayer",
                      "generation": generation, "cave": cave, "doc": doc}
            f.write((json.dumps(header, ensure_ascii=False,
                                separators=(",", ":")) + "\n").encode("utf-8"))
            return generation
        f.seek(end - 1)
        if f.read(1) != b"\n":
            f.seek(0)
            data = f.read()
            complete = data.rfind(b"\n")
            if complete < 0:
                raise ValueError("document relay has no complete header")
            f.truncate(complete + 1)  # discard only a crashed partial append
        generation, _ = self._header(f)
        f.seek(0, os.SEEK_END)
        return generation

    def publish(self, cave, doc, actor, update):
        cave, doc, actor = (_identity(cave, "cave"), _identity(doc, "doc"),
                            _identity(actor, "actor"))
        if not isinstance(update, str) or not update:
            raise ValueError("update must be a non-empty opaque string")
        raw = update.encode("utf-8")
        if len(raw) > MAX_UPDATE_BYTES:
            raise ValueError("update exceeds %d bytes" % MAX_UPDATE_BYTES)
        _ensure_cave(cave)
        persisted = {"v": 1, "id": secrets.token_hex(12), "ts": time.time(),
                     "cave": cave, "doc": doc, "actor": actor, "update": update}
        line = (json.dumps(persisted, ensure_ascii=False,
                           separators=(",", ":")) + "\n").encode("utf-8")
        path = self.path(cave, doc)
        with _relay_lock(path, True):
            with open(path, "a+b") as f:
                generation = self._prepare(f, cave, doc)
                if f.tell() + len(line) > MAX_DOC_BYTES:
                    raise ValueError("document relay is full (%d byte cap)" % MAX_DOC_BYTES)
                f.write(line)
                f.flush()
                os.fsync(f.fileno())
                cursor = _cursor(generation, f.tell())
        os.chmod(path, 0o600)
        ack = {k: v for k, v in persisted.items() if k != "update"}
        ack["cursor"] = cursor
        ack["bytes"] = len(raw)
        return ack

    def _offset(self, f, generation, header_end, cursor):
        if cursor in (None, 0, "0", ""):
            return header_end
        if not isinstance(cursor, str) or ":" not in cursor:
            raise ValueError("cursor must be 0 or <generation>:<offset>")
        got_generation, raw_offset = cursor.rsplit(":", 1)
        if got_generation != generation:
            raise ValueError("stale cursor generation")
        try:
            offset = int(raw_offset)
        except ValueError:
            raise ValueError("cursor offset must be an integer")
        f.seek(0, os.SEEK_END)
        end = f.tell()
        if offset < header_end or offset > end:
            raise ValueError("cursor offset is outside the document")
        if offset:
            f.seek(offset - 1)
            if f.read(1) != b"\n":
                raise ValueError("cursor is not on an update boundary")
        return offset

    def updates(self, cave, doc, cursor=0):
        cave, doc = _identity(cave, "cave"), _identity(doc, "doc")
        path = self.path(cave, doc)
        with _relay_lock(path, False):
            try:
                f = open(path, "rb")
            except FileNotFoundError:
                if cursor not in (None, 0, "0", ""):
                    raise ValueError("stale cursor for missing document")
                return {"cave": cave, "doc": doc, "cursor": "0", "updates": []}
            with f:
                generation, header_end = self._header(f)
                offset = self._offset(f, generation, header_end, cursor)
                f.seek(offset)
                rows = []
                final = offset
                while True:
                    start = f.tell()
                    line = f.readline()
                    if not line:
                        break
                    if not line.endswith(b"\n"):
                        final = start  # crashed writer: retry this record later
                        break
                    try:
                        row = json.loads(line.decode("utf-8"))
                    except (UnicodeDecodeError, ValueError):
                        raise ValueError("corrupt complete update at offset %d" % start)
                    if not isinstance(row, dict) or "update" not in row:
                        raise ValueError("invalid update at offset %d" % start)
                    rows.append(row)
                    final = f.tell()
                return {"cave": cave, "doc": doc,
                        "cursor": _cursor(generation, final), "updates": rows}


class LocalPresence:
    """Independent TTL presence adapter. No heartbeat touches a document log."""

    name = "local"

    def path(self, cave):
        return os.path.join(_cave_dir(cave), "presence.json")

    def _load(self, f):
        f.seek(0)
        try:
            value = json.load(f)
        except (ValueError, OSError):
            value = {}
        return value if isinstance(value, dict) else {}

    def _store(self, f, peers):
        f.seek(0)
        f.truncate()
        json.dump(peers, f, ensure_ascii=False, separators=(",", ":"))
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())

    def _peer_key(self, actor, connection):
        raw = json.dumps([actor, connection], ensure_ascii=False,
                         separators=(",", ":")).encode("utf-8")
        digest = hashlib.blake2b(raw, digest_size=16).hexdigest()
        return "%s-%s" % (pk.slug(actor)[:36], digest)

    def heartbeat(self, cave, actor, state="active", ttl=DEFAULT_TTL,
                  connection=None):
        cave, actor = _identity(cave, "cave"), _identity(actor, "actor")
        connection = _identity(connection or connection_name(actor), "connection")
        state = str(state or "active")[:80]
        try:
            ttl = int(ttl)
        except (TypeError, ValueError):
            raise ValueError("ttl must be an integer")
        if ttl < 1 or ttl > MAX_TTL:
            raise ValueError("ttl must be between 1 and %d seconds" % MAX_TTL)
        _ensure_cave(cave)
        now = time.time()
        path = self.path(cave)
        with _locked_text(path) as f:
            peers = self._load(f)
            peers = {k: v for k, v in peers.items()
                     if isinstance(v, dict) and v.get("expires", 0) > now}
            row = {"actor": actor, "connection": connection, "state": state,
                   "seen": now, "expires": now + ttl}
            peers[self._peer_key(actor, connection)] = row
            self._store(f, peers)
        os.chmod(path, 0o600)
        return row

    def peers(self, cave):
        cave = _identity(cave, "cave")
        path = self.path(cave)
        if not os.path.exists(path):
            return []
        now = time.time()
        with _locked_text(path) as f:
            before = self._load(f)
            peers = {k: v for k, v in before.items()
                     if isinstance(v, dict) and v.get("expires", 0) > now}
            if peers != before:
                self._store(f, peers)
        return sorted(peers.values(), key=lambda p: (p.get("actor", ""),
                                                      p.get("connection", "")))

    def leave(self, cave, actor, connection=None):
        cave, actor = _identity(cave, "cave"), _identity(actor, "actor")
        connection = _identity(connection or connection_name(actor), "connection")
        path = self.path(cave)
        if not os.path.exists(path):
            return False
        with _locked_text(path) as f:
            peers = self._load(f)
            removed = peers.pop(self._peer_key(actor, connection), None) is not None
            if removed:
                self._store(f, peers)
            return removed


def _local_adapters():
    return LocalRelay(), LocalPresence()


ADAPTER_FACTORIES = {"local": _local_adapters}


def register_adapter(name, factory):
    """Register a relay/presence factory for an embedding process."""
    ADAPTER_FACTORIES[_identity(name, "adapter")] = factory


def adapters(name=None):
    """Resolve the selected adapter pair. The CLI ships local only; embeddings
    and future builders.dev bridges register another factory without changing
    the consumer."""
    name = _identity(name or home.env("MULTIPLAYER_BACKEND") or "local", "adapter")
    factory = ADAPTER_FACTORIES.get(name)
    if not factory:
        raise ValueError("unknown multiplayer backend %r" % name)
    return factory()


def _opts(args):
    values, flags, rest = {}, set(), []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            rest.extend(args[i + 1:])
            break
        if a in ("--json", "--stdin"):
            flags.add(a)
        elif a in ("--cave", "--actor", "--connection", "--after", "--state",
                   "--ttl", "--backend"):
            if i + 1 >= len(args):
                raise ValueError("%s needs a value" % a)
            values[a[2:]] = args[i + 1]
            i += 1
        else:
            rest.append(a)
        i += 1
    return values, flags, rest


def _display(value):
    return json.dumps(str(value), ensure_ascii=True)[1:-1]


def _print(value, as_json):
    if as_json:
        print(json.dumps(value, ensure_ascii=False, sort_keys=True))
        return
    if isinstance(value, list):
        for row in value:
            print("%s\t%s\t%s\tseen %.1fs ago" % (
                _display(row["actor"]), _display(row["connection"]),
                _display(row["state"]), max(0, time.time() - row["seen"])))
        return
    for row in value.get("updates", []):
        size = len(str(row.get("update", "")).encode("utf-8"))
        print("%s\t%s\t<%d opaque bytes>" % (
            _display(row["id"]), _display(row["actor"]), size))
    print("cursor %s" % _display(value["cursor"]))


def _print_status(doc, cave, cursor, view, peers):
    """The demo board + peers, materialized for a human. Cell values ride the
    blind relay from any actor, so launder every field like read/peers do — an
    ESC/bidi payload must never reach a terminal through this view."""
    print("board %s @ %s" % (_display(doc), _display(cave)))
    for cell in view["board"]:
        ago = "" if not cell.get("ts") else (
            " (%.0fs ago)" % max(0, time.time() - cell["ts"]))
        print("  %s = %s\t— %s%s" % (_display(cell["key"]), _display(cell["value"]),
                                     _display(cell["actor"]), ago))
    if not view["board"]:
        print("  (no cells yet)")
    if view["foreign"]:
        print("  +%d opaque update(s) from other clients — relay stays blind"
              % view["foreign"])
    print("peers:")
    for p in peers:
        print("  %s@%s\t%s\t(seen %.1fs ago)" % (
            _display(p["actor"]), _display(p["connection"]), _display(p["state"]),
            max(0, time.time() - p.get("seen", time.time()))))
    if not peers:
        print("  (nobody connected)")
    print("cursor %s" % _display(cursor))


def cmd_multiplayer(args, adapter_factory=adapters):
    """multiplayer set|status|publish|read|presence|peers|leave — local opaque updates + TTL presence (set/status = the built-in LWW demo board)."""
    if not args:
        print("usage: helm multiplayer set <doc> <key> <value> [--cave C] [--actor A]\n"
              "       helm multiplayer status <doc> [--cave C] [--json]\n"
              "       helm multiplayer publish <doc> --stdin [--cave C] [--actor A]\n"
              "       helm multiplayer read <doc> [--cave C] [--after CURSOR] [--json]\n"
              "       helm multiplayer presence [--cave C] [--actor A] [--connection ID] [--state S] [--ttl N]\n"
              "       helm multiplayer peers [--cave C] [--json]\n"
              "       helm multiplayer leave [--cave C] [--actor A] [--connection ID]", file=sys.stderr)
        return 2
    try:
        verb = args[0]
        if verb not in ("set", "status", "publish", "read", "presence",
                        "peers", "leave"):
            # refuse BEFORE the adapters are constructed — an unknown verb
            # must not create relay/presence state as a side effect.
            raise ValueError("invalid multiplayer command")
        values, flags, rest = _opts(args[1:])
        cave = values.get("cave") or default_cave()
        actor = values.get("actor") or actor_name()
        connection = values.get("connection") or connection_name(actor)
        relay, presence = adapter_factory(values.get("backend"))
        if verb == "set" and len(rest) >= 3:
            # the demo LWW client: encode a cell, hand the relay an opaque
            # string. `set doc key a b c` joins the tail so unquoted multi-word
            # values work; quote for exactness. Raw `publish` stays the blind path.
            from . import multiplayer_demo
            update = multiplayer_demo.encode(rest[1], " ".join(rest[2:]), actor)
            row = relay.publish(cave, rest[0], actor, update)
            print(json.dumps(row, ensure_ascii=False, sort_keys=True))
        elif verb == "status" and len(rest) == 1:
            from . import multiplayer_demo
            state = relay.updates(cave, rest[0], values.get("after", 0))
            view = multiplayer_demo.materialize(state["updates"])
            peers = presence.peers(cave)
            if "--json" in flags:
                print(json.dumps(
                    {"cave": _identity(cave, "cave"),
                     "doc": _identity(rest[0], "doc"), "cursor": state["cursor"],
                     "board": view["board"], "foreign": view["foreign"],
                     "peers": peers}, ensure_ascii=False, sort_keys=True))
            else:
                _print_status(rest[0], cave, state["cursor"], view, peers)
        elif verb == "publish" and len(rest) == 1:
            if "--stdin" not in flags:
                raise ValueError("publish requires --stdin (opaque updates never ride argv)")
            update = sys.stdin.read(MAX_UPDATE_BYTES + 1)
            if not update:
                raise ValueError("publish received an empty opaque update")
            row = relay.publish(cave, rest[0], actor, update)
            print(json.dumps(row, ensure_ascii=False, sort_keys=True))
        elif verb == "read" and len(rest) == 1:
            _print(relay.updates(cave, rest[0], values.get("after", 0)),
                   "--json" in flags)
        elif verb == "presence" and not rest:
            row = presence.heartbeat(cave, actor, values.get("state", "active"),
                                     values.get("ttl", DEFAULT_TTL), connection)
            print(json.dumps(row, ensure_ascii=False, sort_keys=True))
        elif verb == "peers" and not rest:
            rows = presence.peers(cave)
            if "--json" in flags:
                print(json.dumps({"cave": _identity(cave, "cave"), "peers": rows},
                                 ensure_ascii=False, sort_keys=True))
            else:
                _print(rows, False)
        elif verb == "leave" and not rest:
            print("left" if presence.leave(cave, actor, connection) else "absent")
        else:
            raise ValueError("invalid multiplayer command")
        return 0
    except (OSError, ValueError) as e:
        print("helm multiplayer: %s" % e, file=sys.stderr)
        return 2
