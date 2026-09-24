#!/usr/bin/env python3
"""The helm MCP endpoint — stateless spec rev 2026-07-28, stdlib only.

OWNER-RULED SHAPE (the two owner decision cards of 2026-08-05): the
HYBRID — this server is a SECOND FRONT-END over the same verb layer the CLI
fronts; the CLI remains the universal floor, the diff oracle, and the only
attention transport (beacon/wake stay Monitor + `helm chat wait` — an MCP
notification informs a client process and never wakes an idle model).
Signing for write verbs is server-side per-seat capability (the
owner's signing verdict), landing with chat_post in slice 1; slice 0 is the compliant shell plus
ONE read-only tool proving the path end to end.

WHY STATELESS MAKES THIS A PAGE AND NOT A SUBSYSTEM: the 2026-07-28 core
removed the initialize handshake, protocol sessions, the GET/SSE side
channel, and server-initiated requests. What remains for a compliant
minimal server is exactly what this file does: one localhost POST endpoint,
three mirrored-header validations, three RPCs, three error codes, and
Origin validation. Plain-JSON responses are always legal (clients MUST
accept both shapes), so no SSE is needed until something streams.

A SEAT IS A PER-REQUEST FACT, NEVER CONNECTION STATE: the bearer token on
each request resolves to a seat; `tools/list` MAY shape per seat later.
An absent or unknown token serves the anonymous read-only set — slice 0
has only read-only tools, so anonymous equals everything, and the refusal
plumbing still exists so slice 1's write verbs arrive deny-by-default.
"""
import base64
import json
import os
import re
import secrets
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PROTOCOL_VERSION = "2026-07-28"
SUPPORTED_VERSIONS = (PROTOCOL_VERSION,)
DEFAULT_PORT = 7461
# list results must carry freshness (CacheableResult): short enough that a
# slice-1 tool addition propagates inside a minute, long enough to feed
# client prompt caches
LIST_TTL_MS = 60_000
# discover changes only when the server itself is rebuilt, and its payload is
# caller-independent, so it caches longer and publicly
DISCOVER_TTL_MS = 300_000

# JSON-RPC error codes fixed by the transport spec
E_PARSE = -32700
E_REQUEST = -32600     # parsed, but not one JSON-RPC request object
E_METHOD = -32601      # RESERVED for an unimplemented RPC METHOD (with 404)
E_INVALID = -32602     # invalid params — including an UNKNOWN TOOL NAME
E_HEADER_MISMATCH = -32020
E_UNSUPPORTED_VERSION = -32022

# `Mcp-Name` may legally arrive Base64-wrapped in this exact sentinel, and the
# spec makes DECODING BEFORE COMPARISON a MUST ("servers MUST decode an encoded
# Mcp-Name or Mcp-Param-{Name} value before comparing it to the corresponding
# request body value during Server Validation") — a raw string compare would
# refuse a conforming client the first time a tool name leaves the safe set.
_B64_SENTINEL = re.compile(r"^=\?base64\?(.*)\?=$")

_META_VERSION = "io.modelcontextprotocol/protocolVersion"

_SERVER_INFO = {"name": "helm", "version": "0.1"}
_INSTRUCTIONS = (
    "helm's verb layer over MCP. Tools mirror the `helm` CLI verbs exactly "
    "(the CLI is the diff oracle: same store, two front-ends, outputs "
    "agree). Read tools are anonymous; write tools arrive in slice 1 and "
    "require a per-seat bearer token. Nothing here wakes an idle seat — "
    "chat delivery and beacons stay on the CLI's Monitor loop.")


# ── SEAT IDENTITY (slice 1) ─────────────────────────────────────────────────
#
# WHAT THE CLI ACTUALLY PROVES, measured before designing this: chat.post takes
# `who` as a PARAMETER and the CLI fills it from home.chat_name(), i.e. from
# HELM_CHAT_NAME in the environment — forgeable by anything running as this
# user. The signature rides the poster's own cell profile, selected by that
# same env. So CLI attribution reduces to WE TRUST THE LOCAL UNIX USER.
#
# THEREFORE THE BAR IS EQUALITY, NOT PURITY. MCP chat_post must be exactly as
# strong as CLI chat_post and no weaker. A token minted under the same env
# identity clears that; reading a seat NAME out of _meta or a header does not,
# because then any caller names any seat and MCP becomes strictly weaker than
# the CLI it mirrors. THAT is the line, and it is one line.
#
# WHY NOT web_common.MUTATION_TOKEN, since helm already has a local bearer and
# a second one deserves justification: that token is PER-PROCESS ANTI-CSRF,
# shared by every caller, and its own comment says what it proves — "a hostile
# page can never READ our UI to learn the token". It answers "may you mutate
# at all", never "WHICH SEAT are you". Reusing it would authenticate the
# request and then leave attribution to the caller's claim, which is the exact
# laundering this design exists to refuse. Same shape, different question.
# chat's `_node_token` is likewise client-to-chat-node auth, not local seat
# identity. Neither is a mirror of this; do not consolidate them.
#
# SERVER-SIDE KEYS ARE KEY MANAGEMENT, NOT IDENTITY ASSURANCE. The owner ruled
# this in the MCP chat_post signing decision (2026-08-05), in his own words:
# "let's just do it right first, if easier in the long run". The decision ROW
# ID is deliberately not cited here — helm's docref rung can only vouch for
# dispatch rows and gate receipts, so an owner-decision id reads as a dead
# citation to it and to every fresh clone. Filed as its own row: the highest
# authority in the system is the one kind of citation code cannot make. One
# signer instead of one per seat is what that ruling buys; it does NOT mean
# the server knows better than the seat who is speaking, and the phrase
# invites that misreading.

TOKENS = "mcp-tokens.json"


def _tokens_path():
    from . import home
    return os.path.join(home.global_dir(), TOKENS)


# `pk` and `home` are imported INSIDE each function, matching this module's
# existing idiom (the tools do the same): mcpd is loaded by the CLI dispatcher
# on every helm invocation, so a module-level helm import would make every
# unrelated verb pay for it.


def _owner_seat(value):
    """A tokens-table VALUE -> the canonical seat it names, or None.

    THE ONE DEFINITION OF "this entry names this seat", because having two of
    them was the defect. mint() asked `str(owner or "")`
    while _seat_for() required isinstance(str) + _SEAT_NAME_RE, so an entry of
    `123` canonicalised fine for mint — which handed that token straight back
    to the caller — and was refused by _seat_for, which serves anonymous. The
    seat then held a token that could never authenticate and no surface said
    why. Every door that reads an owner out of this table comes through here,
    so the two can no longer drift apart.

    THE TABLE IS DATA AND `str()` WOULD LAUNDER IT (a residual on an
    otherwise-clean approve). The AST pin proves no CODE path produces a seat
    from request input — it cannot see this one, because the value arrives
    through a FILE. A hand-edited or corrupted table holding
    {"tok": {"evil": 1}} would stringify to the seat name "{'evil': 1}" and
    ride straight onto a surface the OWNER READS. So the shape check is
    helm's OWN seat-name rule rather than a new one: home._SEAT_NAME_RE is
    what every other join seam already enforces ([A-Za-z0-9._-], bounded),
    and a second spelling of "what is a legitimate seat name" is exactly the
    two-surfaces disease. Anything else is NOT A SEAT and resolves to
    anonymous, which cannot write.

    AND CANONICALISED, not merely shape-checked (it is my
    own named disease turned on me): _SEAT_NAME_RE answers "is this
    WELL-FORMED", never "is this the SAME SEAT". Every other helm seam folds
    case — _seat_key casefolds, _canonical_recipient casefolds, and
    seats_roster REFUSES a rename that collides case-insensitively — so a
    table written before this fix can still hold "Attacker" for an identity
    the whole rest of helm calls "attacker", and the RAW spelling was the one
    reaching a row the OWNER READS. Same shape as tasks.origin_of."""
    from . import home
    if not isinstance(value, str) or not home._SEAT_NAME_RE.fullmatch(value):
        return None
    from . import seats_common
    canon, err = seats_common._canonical_recipient(value)
    return None if err else str(canon)


def _seat_for(token):
    """Bearer token -> seat name, or None. The ONLY way a request gets a seat.

    Never falls back to a caller-supplied name: an unknown token is anonymous,
    and anonymous cannot write."""
    if not token:
        return None
    from . import pk
    try:
        table = pk.read_json(_tokens_path(), {})
    except Exception:
        return None
    if not isinstance(table, dict):
        return None
    return _owner_seat(table.get(str(token)))


def mint(seat):
    """Mint (or return) this seat's bearer token -> (token, error).

    MINTS FOR THE CALLING SEAT ONLY — the caller does not get to name a seat.
    If a seat could mint for a peer, MCP would be strictly WEAKER than the CLI
    it mirrors, which is the one outcome this whole design refuses. The seat
    comes from the same env identity `helm chat post` already trusts, so this
    is exactly as strong as the CLI and no stronger."""
    seat = str(seat or "").strip()
    if not seat:
        return None, ("no seat identity — HELM_CHAT_NAME is unset, and a "
                      "token minted without one would attribute posts to "
                      "nobody")
    # IDENTITY, NOT SPELLING (measured through the documented
    # path with nothing but env): the docstring below promised "one live token
    # per seat" and delivered one per SPELLING, because `owner == seat`
    # compares RAW while every other helm seam compares canonically —
    # _seat_key and _canonical_recipient casefold, and seats_roster refuses a
    # rename colliding case-insensitively. So HELM_CHAT_NAME=Attacker then
    # =attacker minted TWO live tokens for what helm elsewhere says out loud
    # is ONE seat, and the raw spelling rode onto a surface the owner reads.
    # One authority, not a second rule.
    from . import seats_common
    canon, err = seats_common._canonical_recipient(seat)
    if err:
        return None, ("%r is not an exact seat token, so no token can be "
                      "minted for it: %s" % (seat, err))
    seat = str(canon)
    from . import pk
    path = _tokens_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
    except OSError as e:
        return None, "could not create %s: %s" % (os.path.dirname(path), e)

    # UNDER THE LOCK, because mint is READ-MODIFY-WRITE and pk.write_json
    # replaces the WHOLE file ("multiprocess mint loses
    # tokens"). Two seats minting at once both read the same table, each adds
    # its own token, and the second replace DROPS the first — whose seat is
    # now holding a bearer token the table has never heard of. The file is
    # never corrupt, which is exactly why this stayed invisible: atomic_write
    # makes every INDIVIDUAL write safe and says nothing about the pair.
    # _flocked is helm's own lock and takes a STABLE sibling path, never the
    # atomic-replaced file itself.
    with seats_common._flocked(path + ".lock") as lock:
        # REFUSE WITHOUT THE LOCK — mint cannot inherit _flocked's
        # availability-first contract (a second read raised this against the
        # cure for its own finding, and it was right). _flocked FAILS OPEN by
        # design: on OSError it yields holding nothing and the body runs
        # anyway. That is correct for a shared-state mutation, where finishing
        # matters more than serialising. It is WRONG HERE, because mint issues
        # a CAPABILITY: a bearer token handed out without exclusive ownership
        # of the table is a capability we cannot promise to keep.
        #
        # MY FIRST CURE WAS A POST-WRITE READBACK, AND IT IS NOT SUFFICIENT.
        # It proves the token was present AT THE INSTANT OF THE READBACK,
        # which is a strictly weaker claim than "this token is live". codex's
        # deterministic schedule, both writers lockless:
        #     A and B both read {}
        #     A writes {tok-A}, reads back tok-A, returns SUCCESS
        #     B writes its STALE {tok-B}, reads back tok-B, returns SUCCESS
        #     final table is {tok-B} — A was told success and holds a token
        #     that authenticates as nobody
        # Nothing probabilistic about it; B's write is gated on A's readback.
        # So the readback stays as defense-in-depth below and exclusive
        # ownership is the actual guarantee.
        if lock.f is None:
            return None, ("could not take the tokens-table lock at %s.lock, "
                          "so this mint cannot own the table exclusively — "
                          "refusing rather than issuing a bearer token that "
                          "another writer can silently drop" % path)
        table = pk.read_json(path, {})
        if not isinstance(table, dict):
            table = {}
        # _owner_seat is the SAME rule _seat_for applies, so mint can no
        # longer match an entry its own reader would refuse.
        mine = sorted(t for t, owner in table.items()
                      if _owner_seat(owner) == seat)
        # PRUNE, don't merely match ("case-fold duplicates
        # remain live"). The previous round canonicalised the COMPARISON, so
        # mint stopped ADDING a duplicate — and left every duplicate already
        # in the table live. {"a": "Attacker", "b": "attacker"} is TWO working
        # bearer tokens for one identity, so revoking one revokes nothing and
        # this docstring's "one live token per seat" was false for precisely
        # the tables that predate the fix. `sorted` picks the SAME survivor in
        # every process, so two concurrent pruners converge instead of each
        # deleting the other's keeper.
        if mine:
            tok, extra = mine[0], mine[1:]
            for dup in extra:
                table.pop(dup, None)
            # AND FOLD THE STORED SPELLING, which my own prune test caught me
            # not doing: dropping the duplicates left the SURVIVOR holding
            # whatever raw case it was written with, so the table still said
            # "Attacker" for the identity every other seam calls "attacker".
            # Canonicalising the COMPARISON and leaving the DATA is the exact
            # half-cure this round is here to stop repeating.
            if not extra and table.get(tok) == seat:
                return tok, None        # idempotent: one live token per seat
            table[tok] = seat
        else:
            tok = secrets.token_hex(24)
            table[tok] = seat
        # pk.write_json RETURNS None — it has no return statement and raises
        # on failure via atomic_write. My first cut wrote `if not
        # pk.write_json(...)` and so reported "could not write" on EVERY
        # SUCCESSFUL MINT: a None-returning writer read as a boolean is a
        # failure check that can only ever say failed. Caught by running it
        # rather than by reading it.
        try:
            pk.write_json(path, table)
        except OSError as e:
            return None, "could not write %s: %s" % (path, e)

    # READ BACK — DEFENSE IN DEPTH, EXPLICITLY NOT THE GUARANTEE. The lock
    # above is what makes the table ours; this catches the residue (a hand
    # edit, a foreign writer that never takes the lock, a filesystem that
    # accepted the write and lost it).
    #
    # SAYING WHAT IT CANNOT DO, because I first shipped it as the guarantee
    # and it is not one: this proves the token was present AT THE INSTANT OF
    # THIS READ, and a later writer holding an older snapshot can still
    # overwrite it. Any comment claiming this "turns a lost write into an
    # error" is describing a point-in-time observation as an invariant —
    # which is exactly the mistake, so it is named here rather than deleted.
    check = pk.read_json(path, {})
    if not isinstance(check, dict) or _owner_seat(check.get(tok)) != seat:
        return None, ("the tokens table changed underneath this mint, so the "
                      "minted token is not in it and would authenticate as "
                      "nobody — nothing was handed out, retry")
    return tok, None


def _tool_store_resolve(arguments, seat):
    """store_resolve — the first tool, read-only, chosen because its CLI
    twin is the fleet's highest-value single-shot query (JIT canon lookup)
    and its output is pure text: the thinnest possible end-to-end proof."""
    # the SAME two functions the CLI verb composes (store/cli.py resolve):
    # one renderer, two fronts — the owner's differential-oracle ruling is
    # honored by construction and tested against drift in test_mcpd
    from .store.resolve import resolve_prompt
    # THE PRIVATE IMPORT IS DELIBERATE AND LOAD-BEARING (flagged in review):
    # sharing the CLI's OWN renderer is the entire reason the two front-ends
    # can be pinned equal. A second formatter here would satisfy the module
    # boundary and REINTRODUCE the divergence this slice exists to remove —
    # the coupling IS the guarantee. If store ever publishes a render API
    # this moves to it unchanged; until then the test that pins both fronts
    # line-for-line is what keeps the coupling honest.
    from .store.cli import _fmt
    # PROJECT SCOPE IS A PER-REQUEST FACT, and leaving it out made the two
    # front-ends genuinely disagree: `helm store resolve` INFERS the project
    # from cwd on reads (store/cli.py:188-217) while this tool passed none and
    # got _global only — so a seat asking over MCP saw FEWER entries than the
    # same seat asking on the CLI, in the same directory. Review caught it; my
    # differential oracle did not, because its fixture had no project at all,
    # which is the oracle blind spot worth remembering: a test env with one
    # project cannot see a project bug.
    #
    # The server's OWN cwd is not the caller's, so the argument is explicit and
    # per-request (the stateless idiom — credentials and context are request
    # input, never connection state), defaulting to the server's inference so
    # the common case matches the CLI without anyone passing anything.
    # "Servers MUST: Validate all tool inputs" — the advertised inputSchema is
    # a CONTRACT: str()-coercing a non-string silently resolved a Python repr,
    # and additionalProperties:false is advertised, so an unknown key is a
    # client bug worth naming. Input-shape problems are the EXECUTION tier.
    if not isinstance(arguments, dict):
        return None, "arguments must be an object"
    extra = sorted(set(arguments) - {"text", "project"})
    if extra:
        return None, ("unknown argument(s) %s — this tool's schema declares "
                      "additionalProperties:false and takes only `text`"
                      % ", ".join(repr(k) for k in extra))
    raw = arguments.get("text")
    if raw is not None and not isinstance(raw, str):
        return None, "text must be a string, got %s" % type(raw).__name__
    text = (raw or "").strip()
    if not text:
        return None, "text is required and was empty"
    project = arguments.get("project")
    if project is not None and not isinstance(project, str):
        return None, "project must be a string when given"
    # NO CWD DEFAULT. The server's cwd is a guess about the CALLER, and a
    # guess that is right only while exactly one seat shares the daemon —
    # codex's point, and correct: two seats in different projects would get
    # one wrong answer each, silently. Absent scope means GLOBAL, which is
    # the honest reading of "no scope given", and a caller wanting CLI
    # parity passes the project it already knows. Per-request input, never
    # daemon state: the same rule the whole transport runs on.
    hits = resolve_prompt(text, project=project)
    rendered = "\n".join(_fmt(e) for e in hits)
    structured = {"fired": bool(hits), "hits": len(hits)}
    # the spec's backwards-compat SHOULD: a tool returning structuredContent
    # SHOULD also serialize it into a TextContent block. The FIRST block stays
    # the rendered lines — that is the differential-oracle payload the CLI
    # front emits verbatim — and the JSON rides second.
    return {"content": [{"type": "text",
                         "text": rendered or "(no entries fire)"},
                        {"type": "text", "text": json.dumps(structured)}],
            "structuredContent": structured}, None


# name -> (handler, inputSchema, description). A dict literal, deterministic
# order by construction (insertion order is the wire order — the spec wants
# stable ordering for client prompt caches).
def _tool_chat_read(arguments, seat):
    """chat_read — ANONYMOUS ON PURPOSE. Reading is not attribution, so it
    needs no token: a caller who can reach the port can already read the room
    on disk. Gating it would buy nothing and would make the write refusal look
    like general access control rather than what it is — an identity rule.

    COMPOSES chat's OWN reader, never a second projection. The owner bought
    the CLI/MCP pair as MUTUAL ORACLES ("they can always be compared against
    each other"), and that only holds while both front-ends run the same code:
    a second implementation here would make the two agree by luck."""
    from . import chat
    room = arguments.get("room")
    if room is not None and not isinstance(room, str):
        return None, "room must be a string"
    limit = arguments.get("limit", 20)
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        return None, "limit must be a positive integer"
    try:
        # THE CLI'S OWN THREE-STEP, NOT A PARAPHRASE: read returns (rows,
        # total); index_rows numbers the WHOLE room so a reply target means
        # the same thing on both front-ends; react_prefix supplies the [n]
        # tag; _fmt renders the line. Reimplementing any of these would make
        # the two surfaces agree by luck instead of by construction, and the
        # owner bought this pair AS mutual oracles.
        rows, _total = chat.read(room or "main")
        idx = chat.index_rows(rows)
        tag = chat.react_prefix(rows)
        shown = rows[-min(limit, 200):]
        text = "\n".join(tag(rows.index(m)) + chat._fmt(m, idx=idx)
                          for m in shown)
    except Exception as e:
        return None, "chat unreadable: %s" % e
    return {"content": [{"type": "text", "text": text}]}, None


def _tool_chat_post(arguments, seat):
    """chat_post — REFUSES WITHOUT A RESOLVED SEAT, and that refusal is the
    feature. A post whose author the server cannot establish would attribute
    text to whoever the caller named, on a surface the OWNER READS. Signing it
    with the server's key would make that laundering look authentic, which is
    worse than not shipping the tool.

    The seat arrives ONLY from a bearer token resolved server-side. There is
    deliberately no path from a name in `_meta` or a header to this argument."""
    from . import chat
    if not seat:
        return None, ("chat_post needs a seat and this request has none. Mint "
                      "one with `helm mcpd token` AS THE SEAT THAT WILL POST, "
                      "then send it as `Authorization: Bearer <token>`. The "
                      "server will not take a seat name from the request: a "
                      "post it cannot attribute is a post it will not sign.")
    text = arguments.get("text")
    if not isinstance(text, str) or not text.strip():
        return None, "text is required"
    room = arguments.get("room")
    if room is not None and not isinstance(room, str):
        return None, "room must be a string"
    try:
        # `who=seat` is the TOKEN's seat, never the caller's claim, and the
        # via marker records that this row travelled a different road than a
        # CLI post — visible to the owner rather than inferred by him.
        row = chat.post(text, room=room, who=seat, origin="mcpd")
    except Exception as e:
        return None, "post failed: %s" % e
    return {"content": [{"type": "text",
                         "text": "posted as %s: %s" % (seat, row.get("id"))}]}, None


TOOLS = {
    "store_resolve": (
        _tool_store_resolve,
        {"type": "object",
         "properties": {
             "text": {
                 "type": "string",
                 "description": "the sentence to resolve against the typed "
                                "store, in the asker's own words"},
             "project": {
                 "type": "string",
                 "description": "project scope. OMIT for global-only. The "
                                "CLI infers this from YOUR cwd on reads, so "
                                "pass your project to get the same answer "
                                "the CLI would give you"}},
         "required": ["text"],
         "additionalProperties": False},
        "Resolve a sentence against helm's typed knowledge store "
        "(premises/heuristics/lexicon) — the JIT canon lookup; read-only."),
    "chat_read": (
        _tool_chat_read,
        {"type": "object",
         "properties": {
             "room": {"type": "string",
                      "description": "room name; defaults to main"},
             "limit": {"type": "integer",
                       "description": "newest N rows (default 20, max 200)"}},
         "required": []},
        "Read a helm chat room — the newest rows, rendered exactly as "
        "`helm chat read` renders them. Anonymous: reading is not "
        "attribution."),
    "chat_post": (
        _tool_chat_post,
        {"type": "object",
         "properties": {
             "text": {"type": "string",
                      "description": "the message body"},
             "room": {"type": "string",
                      "description": "room name; defaults to main"}},
         "required": ["text"]},
        "Post to a helm chat room AS THE SEAT THAT OWNS THE BEARER TOKEN. "
        "Requires `Authorization: Bearer <token>` from `helm mcpd token`; "
        "the server will not accept a seat name from the request, so a "
        "post it cannot attribute is refused rather than signed."),
}


def _tools_payload():
    tools = []
    for name, (_fn, schema, description) in TOOLS.items():
        tools.append({"name": name, "description": description,
                      "inputSchema": schema})
    return tools


def _result(rid, payload):
    payload.setdefault("resultType", "complete")
    meta = payload.setdefault("_meta", {})
    meta.setdefault("io.modelcontextprotocol/serverInfo", _SERVER_INFO)
    return {"jsonrpc": "2.0", "id": rid, "result": payload}


def _error(rid, code, message, data=None):
    err = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": rid, "error": err}


def dispatch(body, seat=None):
    """One JSON-RPC request -> (http_status, response_object).

    Pure function of the request (plus the ledgers the tools read): the
    statelessness the spec promises is enforced here by construction —
    nothing above this call holds per-caller state, and the handler class
    below carries no instance fields between requests."""
    rid = body.get("id")
    method = body.get("method")
    # HOSTILE-SHAPE GUARDS FIRST. A body can be valid JSON and still be a
    # nonsense request: params as a string, _meta as a string, an unhashable
    # tool name. Each used to raise INSIDE the handler thread; socketserver
    # ate the traceback and closed the socket, so the client saw "closed
    # without response" — neither error tier, indistinguishable from a dead
    # legacy server. Malformed requests are PROTOCOL errors (-32602).
    params = body.get("params")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        return 400, _error(rid, E_INVALID, "params must be an object")
    meta = params.get("_meta")
    if meta is None:
        meta = {}
    if not isinstance(meta, dict):
        return 400, _error(rid, E_INVALID, "params._meta must be an object")
    version = meta.get(_META_VERSION)
    if version is not None and version not in SUPPORTED_VERSIONS:
        return 400, _error(rid, E_UNSUPPORTED_VERSION,
                           "unsupported protocol version %r" % version,
                           {"supported": list(SUPPORTED_VERSIONS),
                            "requested": version})
    if method == "server/discover":
        # caching hints are a MUST on every complete result — discover as
        # much as tools/list; discover is the more stable of the two
        return 200, _result(rid, {
            "supportedVersions": list(SUPPORTED_VERSIONS),
            "capabilities": {"tools": {}},
            "instructions": _INSTRUCTIONS,
            "ttlMs": DISCOVER_TTL_MS,
            "cacheScope": "public"})
    if method == "tools/list":
        return 200, _result(rid, {
            "tools": _tools_payload(),
            "ttlMs": LIST_TTL_MS,
            "cacheScope": "private"})
    if method == "tools/call":
        name = params.get("name")
        # UNKNOWN TOOL IS -32602, NOT 404/-32601: the spec's own example is
        # {"code": -32602, "message": "Unknown tool: invalid_tool_name"}, and
        # 404/-32601 is reserved for an unimplemented METHOD — a meaning that
        # doubles as the legacy-server fallback signal, so conflating the two
        # sends a modern client hunting for a 2024-era endpoint.
        entry = TOOLS.get(name) if isinstance(name, str) else None
        if entry is None:
            return 200, _error(rid, E_INVALID, "Unknown tool: %r" % (name,))
        fn, _schema, _desc = entry
        arguments = params.get("arguments") or {}
        try:
            payload, why = fn(arguments, seat)
        except Exception as exc:  # a tool bug is an EXECUTION error the
            # model can read and route around, never a protocol crash —
            # the spec's two-tier convention
            payload, why = None, "%s: %s" % (exc.__class__.__name__, exc)
        if why:
            return 200, _result(rid, {
                "content": [{"type": "text", "text": why}],
                "isError": True})
        return 200, _result(rid, payload)
    return 404, _error(rid, E_METHOD, "unknown method %r" % method)


class _Handler(BaseHTTPRequestHandler):
    server_version = "helm-mcpd/0.1"
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass  # stderr logging is the operator's choice, not per-request spam

    def _refuse(self, status, obj):
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _gone(self):
        """GET and DELETE are the LEGACY verbs (standalone SSE stream, session
        termination). Both were removed in this revision, and the spec names
        405 for both so an old client learns the era rather than guessing."""
        raw = json.dumps(_error(
            None, E_METHOD,
            "%s is not part of the stateless transport; POST only"
            % self.command)).encode("utf-8")
        self.send_response(405)
        self.send_header("Allow", "POST")
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    do_GET = _gone
    do_DELETE = _gone

    def _accepted(self):
        """202 with NO body — the notification contract, verbatim: 'If the
        server accepts it, the server MUST return HTTP status code 202
        Accepted with no body.' Answering a notification with a result also
        violates JSON-RPC itself, which forbids replying to one at all."""
        self.send_response(202)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _header_value(self, name):
        """One header, Base64-sentinel decoded when it carries one."""
        got = self.headers.get(name)
        if got is None:
            return None
        m = _B64_SENTINEL.match(got)
        if not m:
            return got
        try:
            return base64.b64decode(m.group(1), validate=True).decode("utf-8")
        except Exception:
            return got      # undecodable stays raw and fails the comparison

    ENDPOINT = "/mcp"

    def do_POST(self):
        # ONE endpoint path, per the transport's own words ("The server MUST
        # provide a single HTTP endpoint path"). Serving every path made the
        # server answer /anything, which hides client misconfiguration and
        # widens the surface for no gain (codex).
        # EXACT match, not a normalized one. My first cut rstrip()ed the
        # trailing slash and then admitted "" — which let BOTH "/" and
        # "/mcp/" through the check written to stop exactly that (codex).
        # A fix that re-opens the hole it closes is worse than no fix, so
        # this compares the literal path and nothing else.
        if self.path.split("?", 1)[0] != self.ENDPOINT:
            self._refuse(404, _error(None, E_METHOD,
                                     "no MCP endpoint at %r; POST %s"
                                     % (self.path, self.ENDPOINT)))
            return
        origin = self.headers.get("Origin")
        if origin is not None and not re.match(
                r"^https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$",
                origin):
            # DNS-rebinding defence is a MUST; absent Origin is a non-browser
            # client and legal
            self._refuse(403, _error(None, E_INVALID,
                                     "origin %r refused" % origin))
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, TypeError):
            self._refuse(400, _error(None, E_PARSE, "unparseable body"))
            return
        if not isinstance(body, dict):
            # PARSED FINE, wrong shape — -32600, not -32700. This revision
            # forbids batches ("The body of the HTTP POST MUST be a single
            # JSON-RPC request or notification"), so an array lands here.
            self._refuse(400, _error(None, E_REQUEST, "body must be ONE "
                                     "JSON-RPC request or notification "
                                     "object; batches are not part of this "
                                     "revision"))
            return
        if body.get("jsonrpc") != "2.0":
            self._refuse(400, _error(body.get("id"), E_REQUEST,
                                     "jsonrpc must be \"2.0\"; got %r"
                                     % (body.get("jsonrpc"),)))
            return
        rid = body.get("id")
        # A NOTIFICATION IS ANYTHING WITHOUT AN ID, answered with a bare 202 —
        # never a result. This revision defines no client-to-server
        # notifications over HTTP, so accept-and-drop is both compliant and
        # the whole behaviour. Header requirements are explicitly NOT defined
        # for notification POSTs, so the presence checks must not run here.
        if "id" not in body or rid is None:
            self._accepted()
            return
        # The mirrored headers let infrastructure route without parsing
        # bodies. They are REQUIRED, and Server Validation lists "a required
        # standard header is missing" as its FIRST failure condition — a
        # modern-only server MUST reject a header-less request rather than
        # serving it, because omitting the version header is how a
        # pre-2025-06-18 client speaks and this server supports no such era.
        params = body.get("params") if isinstance(
            body.get("params"), dict) else {}
        meta = params.get("_meta") if isinstance(
            params.get("_meta"), dict) else {}
        required = [("MCP-Protocol-Version", meta.get(_META_VERSION)),
                    ("Mcp-Method", body.get("method"))]
        if body.get("method") == "tools/call":
            required.append(("Mcp-Name", params.get("name")))
        for hdr, want in required:
            got = self._header_value(hdr)
            if got is None:
                self._refuse(400, _error(
                    rid, E_HEADER_MISMATCH,
                    "required header %s is missing" % hdr))
                return
            if want is not None and got != want:
                self._refuse(400, _error(
                    rid, E_HEADER_MISMATCH,
                    "%s header %r does not match the body's %r"
                    % (hdr, got, want)))
                return
        # "Every request MUST include the required _meta fields" — the
        # headers mirror the body, so a request carrying headers and NO body
        # metadata satisfied the mirror check while being non-conforming.
        if not meta.get(_META_VERSION):
            self._refuse(400, _error(
                rid, E_INVALID,
                "params._meta.%s is required on every request"
                % _META_VERSION))
            return
        hdr_version = self._header_value("MCP-Protocol-Version")
        if hdr_version not in SUPPORTED_VERSIONS:
            self._refuse(400, _error(
                rid, E_UNSUPPORTED_VERSION,
                "unsupported protocol version %r" % hdr_version,
                {"supported": list(SUPPORTED_VERSIONS),
                 "requested": hdr_version}))
            return
        # SEAT COMES FROM THE TOKEN AND NOWHERE ELSE. There is deliberately
        # no branch here that reads a seat name from `_meta` or a header: that
        # single line is the difference between mirroring the CLI's trust and
        # being strictly weaker than it.
        auth = self._header_value("Authorization") or ""
        token = auth[7:].strip() if auth[:7].lower() == "bearer " else ""
        seat = _seat_for(token)
        status, obj = dispatch(body, seat=seat)
        self._refuse(status, obj)


def serve(port=DEFAULT_PORT, host="127.0.0.1"):
    """Blocking serve loop; 127.0.0.1 by the spec's own SHOULD."""
    httpd = ThreadingHTTPServer((host, int(port)), _Handler)
    return httpd


def serve_background(port=DEFAULT_PORT, host="127.0.0.1",
                     poll_interval=None):
    """(httpd, thread) for tests and embedders; caller owns shutdown().

    POLL_INTERVAL IS A TRADE, NOT A FREE WIN, AND AN EARLIER VERSION OF THIS
    DOCSTRING GOT IT WRONG. It sets how often serve_forever wakes to re-check
    the stop flag, so it is BOTH shutdown latency AND an idle wakeup cost —
    the stdlib says plainly that polling wastes CPU. I previously defaulted it
    to 0.01 and wrote that it "buys nothing while running". A run measured the
    opposite on this exact lane: service_actions 4 -> 203 over 2.05s, process
    CPU 0.000544 -> 0.008316s, and an independent 100-server control going
    from ~0.48% to 25.4% OF A CORE. A 100 Hz idle loop in every embedder is a
    real production regression and it was mine.

    SO THE DEFAULT IS THE STDLIB'S. Passing nothing forwards nothing, and
    serve_forever keeps its own 0.5 — production and embedders are byte-for-
    byte unchanged. Only a caller that KNOWS it wants a fast shutdown asks for
    one, which is what tests/test_mcpd does: it starts a server per test and
    registers shutdown() as cleanup, so it paid one poll per test (39 tests,
    median 501.0ms, max 502.6ms, a distribution with no work in it). That
    fixture passes poll_interval explicitly and the module goes 19.6s -> 0.5s.

    The thread takes KWARGS rather than a lambda so the forwarding is visible
    to a reader and to a test, instead of hidden in a closure."""
    httpd = serve(port=port, host=host)
    kwargs = {} if poll_interval is None else {"poll_interval": poll_interval}
    t = threading.Thread(target=httpd.serve_forever, kwargs=kwargs,
                         daemon=True)
    t.start()
    return httpd, t


def cmd_mcpd(args):
    """helm mcpd serve [--port N] | token. A bind failure is an OPERATOR problem —
    the port is taken, or privileged — so it gets a sentence naming the port
    and the likely cause, never a traceback (codex)."""
    rest = list(args)
    if rest and rest[0] == "token":
        # MINTS FOR THE CALLING SEAT ONLY — there is no --seat flag, on
        # purpose. A flag naming another seat would let any caller forge a
        # peer's identity over MCP, making it strictly WEAKER than the CLI it
        # mirrors; the seat comes from the same env identity `helm chat post`
        # already trusts, so this is exactly as strong and no stronger.
        from . import home
        tok, err = mint(home.chat_name())
        if err:
            print("helm mcpd: %s" % err, file=sys.stderr)
            return 2
        print(tok)
        print("helm mcpd: send this as `Authorization: Bearer <token>`. It "
              "identifies YOU (%s) to the daemon; anyone holding it posts as "
              "you, so treat it like your shell history." % home.chat_name(),
              file=sys.stderr)
        return 0
    port = DEFAULT_PORT
    if rest and rest[0] == "serve":
        rest = rest[1:]
    while rest:
        if rest[0] == "--port" and len(rest) > 1:
            try:
                port = int(rest[1])
            except ValueError:
                print("helm mcpd: --port wants an integer, got %r"
                      % rest[1], file=sys.stderr)
                return 2
            if not 1 <= port <= 65535:
                print("helm mcpd: --port %d is outside 1-65535" % port,
                      file=sys.stderr)
                return 2
            rest = rest[2:]
            continue
        print("usage: helm mcpd serve [--port N]")
        return 2
    try:
        httpd = serve(port=port)
    except OSError as exc:
        print("helm mcpd: cannot bind 127.0.0.1:%d — %s. Another server may "
              "already hold it (`ss -ltnp | grep %d`), or pass --port N."
              % (port, exc.strerror or exc, port), file=sys.stderr)
        return 1
    print("helm mcpd: stateless MCP (%s) on http://127.0.0.1:%d/mcp — "
          "%d tool(s), CLI remains the floor"
          % (PROTOCOL_VERSION, port, len(TOOLS)))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.shutdown()
    return 0
