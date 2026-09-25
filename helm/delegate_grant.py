#!/usr/bin/env python3
"""helm delegate — a seat's explicit grant to its own delegates (task/3060).

A DELEGATE IS NOT ITS SEAT, AND NOTHING THE DELEGATE CARRIES SAYS SO. A
subagent (the Agent tool) or a Workflow agent inherits its seat's whole
environment, its HELM_CHAT_NAME and its session id, so every identity door in
helm reads it as the seat and records its ledger writes as the seat's own.
Measured: a delegated reader wrote an immutable APPROVE with zero findings on
a dispatch row, the third ledger write beyond a delegate's brief that day, and
nothing on the row could say a delegate wrote it.

ONLY THE HOOK PAYLOAD KNOWS. Claude Code's PreToolUse payload carries
`agent_id` inside a subagent or a Workflow agent and not in the main thread
(helm/actors.py, SIDECHAIN_RULE). So the argv-guard (helm/chat.py) refuses a
delegate's Bash or Monitor call whose text runs a VERB-CLASS write from the
table below, the same way it refuses a delegate's beacon arm.

THE TABLE IS VERDICT-CLASS WRITES AND NOTHING WIDER. `dispatch send/add`,
`work claim/release`, `chat post/dm` and `gate run` stay open to a delegate:
a delegated builder files its own review request and runs its own gate. What
is refused is a write that decides somebody else's authority — a verdict and
its corrections, a review row cancelled, moved to another reviewer or to
another tip, a land-request terminal or landing, a store lifecycle decision —
plus the seat's own continuity file, which a delegate overwrote under its
parent's session on the same day (its handoff belongs in its final report).

ONE DELEGATE RUNG. task/1388 built a second rung from the same incident that
refused a subagent's dispatch and land-request ledger writes; its verbs
(`dispatch cancel/rebind/retip`, `lr land/expired`) are in this table, and
the spellings it let through because they write nothing are `writes_nothing`
below. The census of recorded commands found delegates running every one.

A WORKFLOW RUN IS NEVER A SEAT. Its read is recorded BY the seat as an
advisory (`dispatch verdict ... --reviewer-model M --reviewer-run RUN`). A
grant is for DELIBERATE SAME-FAMILY DELEGATION: the seat's main thread runs
`helm delegate allow --verbs "..."`, and the grant admits those verbs for the
seat's delegates until it expires or is revoked.

WHY A GRANT CANNOT BE SELF-MINTED. It is keyed on the session id, which a
delegate shares with its seat (helm/beacon_origin.py says why a session join
cannot tell them apart), and `delegate allow` / `delegate revoke` are in the
refused table with no grant able to admit them. So only a caller the hook
reads as the main thread can write one.

STDLIB ONLY, and imported by the hook only for a delegate's command that
names `helm` at all, so the main thread's calls never pay for it.
"""
import json
import os
import re
import sys
import time

from . import home, pk

#: THE DELEGATE-REFUSED TABLE, as (group, verb) pairs of `helm <group>
#: <verb>`. The `lr` aliases are spellings of `lr close` and are refused with
#: it. Order is the order a refusal names them in.
REFUSED = (
    ("dispatch", "verdict"), ("dispatch", "retract"),
    ("dispatch", "hold"), ("dispatch", "release"),
    ("dispatch", "cancel"), ("dispatch", "rebind"), ("dispatch", "retip"),
    ("lr", "close"), ("lr", "land"), ("lr", "abandon"), ("lr", "retire"),
    ("lr", "expired"),
    ("lr", "discharge"), ("lr", "withdraw"), ("lr", "close-landed"),
    ("store", "confirm"), ("store", "supersede"), ("store", "revise"),
    ("store", "retire"), ("store", "reject"),
    ("handoff", "write"),
    ("delegate", "allow"), ("delegate", "revoke"),
)
#: The verbs no grant admits: minting or revoking a grant is the seat's own
#: act, or a delegate could grant itself.
UNGRANTABLE = (("delegate", "allow"), ("delegate", "revoke"))
GRANTABLE = tuple(pair for pair in REFUSED if pair not in UNGRANTABLE)

#: THE SPELLINGS OF A REFUSED VERB THAT WRITE NOTHING, read per invocation by
#: `writes_nothing`. A dispatch or lr verb given no arguments prints its usage,
#: and so does one given -h or --help; an lr verb's --dry-run writes nothing.
#: Only these two groups: `handoff write` given no arguments WRITES.
USAGE_GROUPS = ("dispatch", "lr")
DRY_RUN_GROUPS = ("lr",)
#: The lr verbs whose census is the default and whose write takes --apply:
#: the flag that selects that mode, "" where the verb is always a census.
CENSUS = {("lr", "expired"): "", ("lr", "retire"): "--off-frontier"}


def writes_nothing(group, verb, args):
    """Whether verb `verb` of the helm group `group`, given `args`, writes
    nothing: its usage, its help, an lr dry run, or an lr census without
    --apply. `args` are the words after the verb, None where the text does
    not settle one."""
    if group not in USAGE_GROUPS:
        return False
    if not args or "-h" in args or "--help" in args:
        return True
    if group in DRY_RUN_GROUPS and "--dry-run" in args:
        return True
    mode = CENSUS.get((group, verb))
    return mode is not None and (not mode or mode in args) \
        and "--apply" not in args

TTL_DEFAULT_S = 30 * 60
TTL_MAX_S = 24 * 3600
_NOTE_CAP = 200
_SESSION = re.compile(r"[A-Za-z0-9._-]{1,128}\Z")
_GID = re.compile(r"[0-9a-f]{12}\Z")
_TTL = re.compile(r"(\d+)([smh]?)\Z")

USAGE = ("usage: helm delegate allow --verbs \"GROUP VERB[,GROUP VERB...]\" "
         "[--ttl 30m] [--note TEXT] [--json] | list [--json] | revoke "
         "<id>|--all  (a seat's grant to its OWN delegates — subagents and "
         "Workflow agents sharing its session — to run verdict-class writes "
         "the argv-guard otherwise refuses them. Run it from the seat's main "
         "thread; a delegate cannot mint one)")


def verb_name(pair):
    """`("dispatch", "verdict")` -> "dispatch verdict"."""
    return "%s %s" % pair


def _pair(text):
    """A typed verb -> its (group, verb) pair, or None."""
    words = str(text or "").strip().lower().split()
    return tuple(words) if len(words) == 2 else None


def grant_dir():
    return os.path.join(home.global_dir(), ".state", "delegate-grants")


def _path(session):
    return os.path.join(grant_dir(), "%s.json" % session)


def _valid_session(session):
    return isinstance(session, str) and bool(_SESSION.fullmatch(session))


def _read(session):
    """The session's grant records as a list; [] when there are none or the
    file cannot be read. An unreadable file admits NOTHING, which is the
    direction a grant may fail in."""
    if not _valid_session(session):
        return []
    try:
        with open(_path(session), encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError):
        return []
    grants = doc.get("grants") if isinstance(doc, dict) else None
    return [g for g in grants or () if isinstance(g, dict)]


def _live(grant, now):
    expires = grant.get("expires")
    return isinstance(expires, (int, float)) and not isinstance(expires, bool) \
        and expires > now and isinstance(grant.get("id"), str) \
        and isinstance(grant.get("verbs"), list)


def live(session, now=None):
    """The session's unexpired grants, oldest first."""
    now = time.time() if now is None else now
    return [g for g in _read(session) if _live(g, now)]


def admits(session, verb, now=None):
    """The live grant admitting `verb` ("group verb") for `session`, or None.

    An ungrantable verb is never admitted, whatever a file on disk says: the
    file is data a writer could have got wrong, and this is the door."""
    pair = _pair(verb)
    if pair is None or pair not in GRANTABLE:
        return None
    for grant in live(session, now):
        if verb_name(pair) in grant["verbs"]:
            return grant
    return None


def parse_ttl(text):
    """(seconds, None) or (None, why). Bare digits are seconds."""
    got = _TTL.fullmatch(str(text or "").strip().lower())
    if not got:
        return None, "--ttl is a number of seconds, or NNm / NNh (got %r)" % text
    seconds = int(got.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600}[got.group(2)]
    if seconds <= 0:
        return None, "--ttl must be positive"
    if seconds > TTL_MAX_S:
        return None, "--ttl is capped at 24h: a standing grant is a policy, " \
                     "not a delegation"
    return seconds, None


def parse_verbs(text):
    """(["group verb", ...], None) or (None, why)."""
    out = []
    for part in str(text or "").split(","):
        if not part.strip():
            continue
        pair = _pair(part)
        if pair in UNGRANTABLE:
            return None, ("%s is the seat's own act and no grant admits it"
                          % verb_name(pair))
        if pair not in GRANTABLE:
            return None, ("%r is not a verb delegates are refused; grantable: "
                          "%s" % (part.strip(), ", ".join(
                              verb_name(p) for p in GRANTABLE)))
        if verb_name(pair) not in out:
            out.append(verb_name(pair))
    if not out:
        return None, "--verbs names no verb"
    return out, None


def allow(session, verbs, ttl_s=None, note=None, minted_by=None, now=None):
    """(grant, None) or (None, why) — mint one grant for `session`."""
    if not _valid_session(session):
        return None, ("a grant is keyed on the session its delegates share, "
                      "and this process has none")
    now = time.time() if now is None else now
    ttl_s = TTL_DEFAULT_S if ttl_s is None else ttl_s
    note = " ".join(str(note or "").split())[:_NOTE_CAP] or None
    grant = {"id": os.urandom(6).hex(), "verbs": list(verbs),
             "minted": pk.now_ts(), "expires": now + ttl_s,
             "minted_by": minted_by, "minted_pid": os.getpid(), "note": note}
    kept = live(session, now) + [grant]
    try:
        os.makedirs(grant_dir(), exist_ok=True)
        pk.write_json(_path(session), {"v": 1, "session": session,
                                       "grants": kept})
    except OSError as exc:
        return None, "grant NOT written: %s" % exc
    return grant, None


def revoke(session, gid=None, now=None):
    """(revoked ids, None) or (None, why). `gid` None revokes every grant."""
    if not _valid_session(session):
        return None, "this process has no session, so it holds no grant"
    now = time.time() if now is None else now
    grants = live(session, now)
    gone = [g["id"] for g in grants if gid is None or g["id"] == gid]
    if gid is not None and not gone:
        return None, "no live grant %s on this session" % gid
    kept = [g for g in grants if g["id"] not in gone]
    try:
        if kept:
            pk.write_json(_path(session), {"v": 1, "session": session,
                                           "grants": kept})
        elif os.path.exists(_path(session)):
            os.unlink(_path(session))
    except OSError as exc:
        return None, "grant NOT revoked: %s" % exc
    return gone, None


def _minted_by():
    try:
        return home.chat_name() or None
    except Exception:                    # noqa: BLE001 — unknown, not a crash
        return None


def _describe(grant, now):
    left = max(0, int(grant["expires"] - now))
    return "%s  %s  expires in %dm%s" % (
        grant["id"], ", ".join(grant["verbs"]), left // 60,
        ("  (%s)" % grant["note"]) if grant.get("note") else "")


def cmd_delegate(args):
    """delegate allow|list|revoke — a seat's grant to its own delegates."""
    args = list(args or [])
    if not args or args[0] in ("-h", "--help"):
        print(USAGE, file=sys.stderr if not args else sys.stdout)
        return 2 if not args else 0
    verb, rest = args[0], args[1:]
    if "-h" in rest or "--help" in rest:
        print(USAGE)
        return 0
    as_json = "--json" in rest
    rest = [a for a in rest if a != "--json"]
    session = home.session_id()
    now = time.time()
    if verb == "allow":
        opts, i = {}, 0
        while i < len(rest):
            if rest[i] not in ("--verbs", "--ttl", "--note") \
                    or i + 1 >= len(rest):
                print("helm delegate allow: unexpected %r (%s)"
                      % (rest[i], USAGE), file=sys.stderr)
                return 2
            opts[rest[i]] = rest[i + 1]
            i += 2
        verbs, err = parse_verbs(opts.get("--verbs"))
        ttl_s, terr = parse_ttl(opts["--ttl"]) if "--ttl" in opts \
            else (TTL_DEFAULT_S, None)
        if err or terr:
            print("helm delegate allow: %s" % (err or terr), file=sys.stderr)
            return 2
        grant, err = allow(session, verbs, ttl_s, opts.get("--note"),
                           _minted_by(), now)
        if err:
            print("helm delegate allow: %s" % err, file=sys.stderr)
            return 1
        if as_json:
            print(json.dumps(grant, sort_keys=True))
            return 0
        print("helm delegate: GRANT %s — this seat's delegates may run %s "
              "until it expires in %dm; `helm delegate revoke %s` ends it"
              % (grant["id"], ", ".join(verbs), ttl_s // 60, grant["id"]))
        return 0
    if verb == "list":
        if rest:
            print("helm delegate list: unexpected %r" % rest[0],
                  file=sys.stderr)
            return 2
        grants = live(session, now)
        if as_json:
            print(json.dumps(grants, sort_keys=True))
            return 0
        if not grants:
            print("helm delegate: no live grant on this session — this seat's "
                  "delegates are refused every verdict-class write")
        for grant in grants:
            print(_describe(grant, now))
        return 0
    if verb == "revoke":
        if rest == ["--all"]:
            gid = None
        elif len(rest) == 1 and _GID.fullmatch(rest[0]):
            gid = rest[0]
        else:
            print("helm delegate revoke: name one grant id or --all (%s)"
                  % USAGE, file=sys.stderr)
            return 2
        gone, err = revoke(session, gid, now)
        if err:
            print("helm delegate revoke: %s" % err, file=sys.stderr)
            return 1
        print(json.dumps(gone) if as_json else
              "helm delegate: revoked %s" % (", ".join(gone) or "nothing"))
        return 0
    print("helm delegate: unknown subverb %r (%s)" % (verb, USAGE),
          file=sys.stderr)
    return 2
