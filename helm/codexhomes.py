#!/usr/bin/env python3
"""helm codex — the codexhome roster + proxy cred pooling (stdlib only).

THE MECHANISM (the codex-credhome-proxy-pooling premise, owner-taught
2026-07-21, done by hand that night — codified here): codex accounts live as
credHOMES under ~/.codex-homes/<name>/ (each a CODEX_HOME dir carrying the
CLI-native auth.json; login stays human-only — homes.py's
`CODEX_HOME=<h> codex login --device-auth`). Codex SEATS run `claude` against
the local CLIProxyAPI (seat.py, :8317), which POOLS creds from its auth-dir
(~/.helm/_global/seats/codex/auth/), HOT-RELOADS that dir on file changes,
and FALLS THROUGH to a working cred when one is usage-capped. Pooling =
translate a home's auth.json (nested `tokens`) into the proxy's FLAT record
{type, email, account_id, access_token, id_token, refresh_token, disabled,
expired, last_refresh}, written 0600 as codex-<name>.json. The translation
itself is seat.translate_codex_auth — the proven recipe, consumed not copied.

LAWS:
  - source auth.json files are READ-ONLY, forever (seat.py's law): the codex
    CLI refreshes the home's copy, the proxy refreshes its own pooled copy.
  - the access_token `exp` is NOT a liveness verdict — the codex CLI
    autorefreshes homes, so a past exp means "refresh before pooling" (run
    `CODEX_HOME=<h> codex` once; the proxy's own refresh can 401 on a stale
    copy), never "the account is dead". Pool warns, never refuses, on it.
  - token material NEVER reaches stdout/stderr — email / account_id / plan /
    paths only. Pooled files are 0600 from creation.
  - plan tiers: chatgpt_plan_type "pro" = ultra (handles multiple concurrent
    codexes), "team" = team (one codex each). Read from the access_token's
    https://api.openai.com/auth claim, id_token fallback — decode-only
    identity metadata, tokens discarded.

KNOWN INTERACTION: `helm seat add codex` replaces only the pooled file(s)
carrying the SAME account_id it mints — other pooled accounts survive a seat
re-add (one-cred-per-seat is a default, never an invariant; the pool is the
proxy's usage-cap fall-through and must not collapse).

ORCA ONE-WAY LEG (`helm codex sync-orca [--watch]`): orca — the metaharness
host — manages per-account CODEX_HOMEs natively, and its account SELECTION
drives this pool: accounts.list over harness.OrcaAdapter's daemon socket
(the one orca client — the CLI fallback below is a SUBPROCESS, never a
second socket implementation), degrading to the `orca account list` prose
roster when the RPC fails (email + active marker only — see
_orca_cli_codex_state), then the selected identity is pooled from the LIVE
bytes with a strict SOURCE PRECEDENCE (amendment, measured 2026-08-04):
  1. orca's own managed per-account home — userData/codex-accounts/<id>/home/
     auth.json, ownership-markered (orca codex-accounts/service.ts mints it,
     host-codex-managed-home-ownership.ts guards it). codex ROTATES refresh
     tokens, and orca's copy refreshing first INVALIDATES any duplicate — so
     when orca holds the account, orca's file is the live credential and our
     ~/.codex-homes copy is potentially burned bytes. THAT PREMISE HOLDS
     ONLY WHILE ORCA ACTIVELY USES THE ACCOUNT: an idle managed copy can be
     arbitrarily stale, so overwriting an EXISTING pool file from this
     source passes a FRESHNESS RUNG — a managed auth.json whose mtime is
     strictly older than the pool file's REFUSES (both timestamps printed;
     `--force-managed` / HELM_CODEX_FORCE_MANAGED=1 is the deliberate
     override). Measured 2026-08-04 19:10Z: a July-26 managed copy pooled
     over an 18:17Z login grant took the codex family auth-dark 11 minutes.
  2. the matching ~/.codex-homes copy (account_id-then-email, codex_pool) —
     the FALLBACK when orca has no managed home for the selection (system
     default slot, WSL-managed, marker/identity mismatch, or absent).
Both sources stay READ-ONLY forever; the stale-exp WARN law applies to
whichever source is chosen.

ORCA ACTIVE-ACCOUNT FOLLOW (`helm seat cred-follow [--apply]`, and the rung
inside the proxywatch pass and `helm seat doctor --ensure`): the DAEMON-FREE
answer to the same question, cheap enough to run unattended — orca writes the
active account id to a provenance FILE on every switch, so two JSON reads say
what accounts.list charges 16 seconds for. It IMPORTS ONCE and then leaves the
pool copy alone, because the proxy rotates its own refresh token away from
orca's within a minute; see the section comment above `cred_follow` for the
measurement and the rest of the report-first laws. No managed source AND zero/many codexhome
matches = loud refusal printing BOTH rosters plus the managed-store probe
verdict, never a guess, never a write; --watch polls and re-pools only on a
selection CHANGE (the proxy's hot-reload watcher is not churned per tick).

Every public function returns a JSON-able dict (or list); errors are
{"error": "..."} — loud, attributed, never an exception across the API edge.
"""
import collections
import contextlib
import fcntl
import fnmatch
import glob
import json
import os
import re
import subprocess
import sys
import threading
import time

from . import home, pk, seat as _seat

PLAN_TIER = {"pro": "ultra", "team": "team"}

# `helm codex launch` cred-% gate (runbook 2026-07-21 fix #3): the refuse
# policy lives here, in numbers, so a launch never silently drains a capped
# pool (fall-through masks per-cred burn until everyone 429s at once).
NEAR_PCT = 80.0          # a pooled cred at/above this in ANY live window = near
STALE_S = 3600           # rollout tail older than this = stale = UNKNOWN
SCAN_ROLLOUTS = 3        # newest rollout files to tail per credhome
TAIL_BYTES = 65536       # bytes tailed per rollout (the last rate_limits win)


def tier(plan):
    """chatgpt_plan_type -> the fleet vocabulary (ultra/team); unknowns pass
    through raw so a new plan name is visible, never masked."""
    return PLAN_TIER.get(plan, plan or "?")


def personal_plan(plan):
    """Whether `plan` is KNOWN to name one person's account, so that its
    account id names one person too (task/2981).

    ONLY `PLAN_TIER` KNOWS. A plan it places in a tier other than the team
    tier is personal. Every other plan is a possible WORKSPACE, whose account
    id every member carries: "team", a plan helm has no word for (business,
    enterprise, edu, one named tomorrow), and an unreadable plan. Such a plan
    proves no member on the id alone. A new personal plan is added to
    `PLAN_TIER`, never guessed here."""
    return isinstance(plan, str) and PLAN_TIER.get(plan, "team") != "team"


# slice 6 — N-codex-per-credhome: tier IS the seat-count policy. An ultra
# credhome (plan pro, 20x) drives N concurrent codex seats against the SAME
# proxy/pool; a team credhome stays 1-each. HELM_CODEX_ULTRA_SEATS moves the
# ultra count without a state file; unknown tier = 1 (safe).
def _ultra_seats():
    try:
        return max(1, int(home.env("CODEX_ULTRA_SEATS") or 3))
    except ValueError:
        return 3


def seat_capacity(t):
    """Fleet seats a tier supports. ultra -> HELM_CODEX_ULTRA_SEATS (dflt 3);
    team/unknown -> 1."""
    return _ultra_seats() if t == "ultra" else 1


def capacity():
    """Fleet seat capacity = what the POOL holds, not what exists under
    ~/.codex-homes (an unpooled ultra contributes 0). {creds: [{email, tier,
    seats}], total, unknown, error} over every parseable, non-disabled pooled
    codex record. Read by `helm codex capacity` and the `helm seat launch -i`
    guard.

    A POOL THAT WOULD NOT ENUMERATE HAS UNKNOWN CAPACITY, NOT ZERO (task/2480
    R5). `total: 0` is the sentence "the pool holds nothing", and the seat
    guard renders it as "instance N exceeds pooled fleet capacity 0" — a
    measured over-capacity WARN produced by a directory nobody read. So the
    dict carries `unknown`, and the guard says UNKNOWN and still lets the
    launch through (the capacity rung warns, it has never refused)."""
    census = read_pool()
    creds, total = [], 0
    for r in pooled_rows(census):
        if r.get("error") or r.get("disabled") or r.get("type") != "codex":
            continue
        t = r.get("tier") or "?"
        n = seat_capacity(t)
        creds.append({"email": r.get("email"), "tier": t, "seats": n})
        total += n
    return {"creds": creds, "total": total, "unknown": census.unknown,
            "error": census.error if census.unknown else None}


def homes_root():
    """~/.codex-homes, HELM_CODEX_HOMES_DIR-overridable (tests, odd installs)."""
    return os.path.realpath(os.path.expanduser(
        home.env("CODEX_HOMES_DIR") or os.path.join("~", ".codex-homes")))


def pool_dir():
    """The codex seat proxy's auth-dir — the ONE dir CLIProxyAPI hot-reloads."""
    return os.path.join(_seat.seat_dir("codex"), "auth")


def pool_lock_path():
    """The pool's write lock, BESIDE the hot-reload dir and never inside it.

    CLIProxyAPI watches `pool_dir()` and re-reads it on every change; a lock
    file living in there would churn that watcher on every pass for nothing.
    The parent (the codex seat dir) is helm's own and nothing reloads it."""
    return os.path.join(os.path.dirname(pool_dir()), ".pool.lock")


#: In-process mutual exclusion + re-entrancy for `pool_lock`. `flock` is a
#: per-OPEN-FILE-DESCRIPTION lock, so two fds in ONE process do not exclude
#: each other (two threads would both "hold" it) and a nested acquire on a
#: fresh fd would DEADLOCK against the outer one. The threading.Lock supplies
#: the cross-thread half; the thread-local depth makes a nested take a no-op,
#: which is what lets `cred_follow` hold the lock across a `_pool_auth` that
#: also takes it.
_POOL_LOCK = threading.Lock()
_POOL_DEPTH = threading.local()


@contextlib.contextmanager
def pool_lock():
    """THE pool write lock: serialize the whole read-prove-write of any pool
    member across processes (proxywatch's timer pass, `seat doctor --ensure`,
    an operator's `helm codex pool`, `helm codex unpool`, and the seat mint
    `helm seat add codex`, which writes through `pool_provision`) and across
    threads.

    WHY, in one sentence: the proxy's auth-dir is shared mutable state with
    several unattended writers, so a destination proved free, or proved
    parked, by a read taken OUTSIDE a boundary is a fact about the past and
    can never authorize a write. Holding it is
    only half the cure — a writer must also RE-READ the destination inside
    the lock and refuse if it moved, because helm is not the only thing that
    can put a file in that dir.

    Re-entrant within a thread; mutually exclusive between threads and, via
    flock, between processes."""
    depth = getattr(_POOL_DEPTH, "n", 0)
    if depth:
        _POOL_DEPTH.n = depth + 1
        try:
            yield
        finally:
            _POOL_DEPTH.n = depth
        return
    _POOL_LOCK.acquire()
    try:
        path = pool_lock_path()
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            _POOL_DEPTH.n = 1
            try:
                yield
            finally:
                _POOL_DEPTH.n = 0
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
    finally:
        _POOL_LOCK.release()


def _read_json(path):
    try:
        with pk.open_regular(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _read_text(path):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return None


def _utc(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _write_pool_atomic(dest, text):
    """A pool write the proxy's hot-reload watcher can never catch half-made:
    0600 tmp sibling + os.replace (atomic on the same fs) — O_TRUNC-in-place
    (_write_private) lets the watcher read a truncated cred mid-write (slice
    6, risk 2). Mode enforced from creation like _write_private."""
    d = os.path.dirname(dest)
    os.makedirs(d, exist_ok=True)
    import tempfile
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".pool-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, dest)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _identity(auth):
    """(email, plan, account_id, exp_epoch) from a codex auth.json dict —
    decode-only claims (access_token first for plan, per the premise; id_token
    fallback). Tokens are decoded and discarded, never returned."""
    t = (auth or {}).get("tokens") or {}
    idc = _seat._jwt_claims(t.get("id_token"))
    acc = _seat._jwt_claims(t.get("access_token"))
    email = idc.get("email") or acc.get("email")
    plan = ((acc.get("https://api.openai.com/auth") or {}).get("chatgpt_plan_type")
            or (idc.get("https://api.openai.com/auth") or {}).get("chatgpt_plan_type"))
    account_id = (t.get("account_id")
                  or (idc.get("https://api.openai.com/auth") or {}).get("chatgpt_account_id")
                  or (acc.get("https://api.openai.com/auth") or {}).get("chatgpt_account_id"))
    exp = acc.get("exp")
    return (email if isinstance(email, str) and email else None,
            plan if isinstance(plan, str) and plan else None,
            account_id if isinstance(account_id, str) and account_id else None,
            exp if isinstance(exp, (int, float)) else None)


def _user_id(auth):
    """The chatgpt_user_id claim — the QUOTA BEARER — from a codex auth.json
    dict or a flat pool record wrapped as {"tokens": rec}; None when neither
    token carries it. Decode-only, tokens discarded."""
    t = (auth or {}).get("tokens") or {}
    for claims in (_seat._jwt_claims(t.get("id_token")),
                   _seat._jwt_claims(t.get("access_token"))):
        uid = (claims.get("https://api.openai.com/auth") or {}).get("chatgpt_user_id")
        if isinstance(uid, str) and uid:
            return uid
    return None


def _member_key(auth):
    """(account_id, user_id) — THE POOL MEMBERSHIP IDENTITY (task: the codex
    pool cools each credential on its own reset).

    account_id ALONE is not a credential. On a ChatGPT Team plan the id_token's
    chatgpt_account_id is the WORKSPACE id, shared by every member of the
    workspace: measured: three members all
    carry one workspace account id with three distinct chatgpt_user_ids, three
    separate logins the proxy can draw on separately, and their 429s land at
    different moments. Keyed on the account alone, the census collapsed the
    three into one row, `cred-follow` called admin@ PRESENT because d@ was
    pooled, and the budget probe relabelled d@'s reading as admin@'s. The
    user id is what one orca-managed account maps to one-to-one, so it is the
    second half of the key; the account id stays the workspace label.

    A key with an UNKNOWN user id proves no member on its own (task/2981).
    Two records may share a census row only when both halves agree
    (`_same_census_member`), and a record is matched to an identity by the
    member rule (`_serves_member`): the user ids, else the address, else the
    account id alone only on a plan KNOWN to be personal."""
    _email, _plan, account_id, _exp = _identity(auth)
    return (account_id, _user_id(auth))


def _same_census_member(a, b):
    """Whether two `_member_key`s may share ONE CENSUS ROW (task/2742).

    A fold is an ACT WITH VICTIMS, not a match: folding (A, None) into (A, u)
    prints one row carrying both files and the FIRST record's token, so a
    probe of one member reads as the other's. An unknown user id is therefore
    a DISTINCT member at the census, never a wildcard: two such records stay
    two rows, each UNKNOWN about which member it is, and the budget verdict
    reads that as unproven rather than pooled.
    """
    if not a[0] or a[0] != b[0]:
        return False
    return a[1] is not None and b[1] is not None and a[1] == b[1]


def _address(value):
    """The casefolded spelling of a login address, or "" when `value` is no
    address. A value with no "@" is no address: the pool writer spells a
    credential with no email claim with one fixed placeholder, and two such
    credentials are not one login."""
    spelled = value.strip().casefold() if isinstance(value, str) else ""
    return spelled if "@" in spelled else ""


def _same_address(a, b):
    """Whether two login addresses name ONE login: both are addresses and
    they are equal, case-folded (task/2981).

    THE ONE ADDRESS RULE. The member join (`_serves_member`) and the pool's
    email fallback (`codexbudget.pool_record_for`) both ask it, so the two
    cannot drift apart again: one folded case and the other did not."""
    mine = _address(a)
    return bool(mine) and mine == _address(b)


def _serves_member(row, user_id, email, shared):
    """Whether `row` ({user_id, email, plan}: a pooled credential or a
    codexhome) is the credential OF the identity (`user_id`, `email`). The
    caller has already matched the two on account id (task/2981).

    NEVER ON THE WORKSPACE ID ALONE. On a Team plan the account id names the
    WORKSPACE, and every member carries it (`_member_key`). A join that
    stopped at the id served every member from the first member's file.
    Measured: `helm creds` printed three members at one identical
    headroom, while one of them had walled. So the id only narrows the
    search, and one of two halves proves the member, in this order:

      1. both user ids known: they decide (the member key, as in
         `codexresets.member_id`). A known different user id is a different
         person on any plan.
      2. the same login address (`_same_address`).

    With neither proof, the id alone serves only when it names ONE person:
    the row's plan is KNOWN to be personal (`personal_plan`) and the pool
    does not show the id shared (`shared`: more than one candidate carries
    it). Every other plan is a possible workspace: "team", a plan helm has
    no word for (business, enterprise, edu), and an unreadable plan. Each of
    those once took the personal arm on the id alone and served a sibling's
    file.

    THREE ANSWERS, AND THE THIRD IS NOT THE SECOND. True: this member. False:
    PROVEN another member (the user ids differ, or two real addresses do).
    None: UNPROVEN, because no half can be compared. None is falsy, so a
    caller that only asks "is it this member" reads no, and a caller that
    must not act on a guess (retire a file, spend a credit, call the member
    absent) reads None as UNKNOWN."""
    if user_id and row.get("user_id"):
        return row["user_id"] == user_id
    if _same_address(email, row.get("email")):
        return True
    if not shared and personal_plan(row.get("plan")):
        return True
    return False if _address(email) and _address(row.get("email")) else None


def member_digests(account_id, user_id, length=None):
    """The digest KEYS one member's attempt history may live under, most
    specific first (task/2734).

    THE DIGEST IS A FUNCTION OF THE PAIR, and the pair can LOSE a half: a
    pooled record whose chatgpt_user_id claim stops being readable keeps its
    flat account_id and presents as (A, None), which is a DIFFERENT digest —
    and a ledger read keyed only on the new pair finds an EMPTY history, the
    cool-down is never consulted, and the same wall is spent twice (measured
    in the review probe: two credits, one wall, sixty seconds). So a caller
    reading history asks for BOTH spellings: the pair as it stands and the
    pair with the user id blanked. A KNOWN user id still yields both, because
    rows written while the claim was unreadable are this member's too.

    BOTH SPELLINGS ARE ASKED IN BOTH DIRECTIONS, because the claim can drop
    out in either order: a record whose user id was KNOWN when the wall was
    journaled and is unreadable now must find that history, and the reverse
    (written unknown, read known) is the same pair one week later. Neither
    direction can match a DIFFERENT known user — that digest carries their
    own id — so the worst it can do is find rows written about this account
    under an UNKNOWN user, which at write time was indistinguishable from
    this record already. The safe direction holds both ways: a shared history
    over-blocks, and `attempts_for`'s caller reads any history it cannot
    attribute with certainty as a reason NOT to spend, never a licence.

    `length` is the ledger's truncation (codexresets.MEMBER_ID_LEN) and is
    REQUIRED — a default here would be a second copy of that number.
    """
    if length is None:
        raise TypeError("member_digests needs the ledger's digest length")
    import hashlib as _h
    def _d(raw):
        return _h.sha256(raw.encode("utf-8")).hexdigest()[:length]
    if not account_id:
        return ()
    keys = []
    for uid in dict.fromkeys([user_id or "", ""]):
        d = _d("account\x00%s\x00user\x00%s" % (account_id, uid))
        if d not in keys:
            keys.append(d)
    return tuple(keys)


POOL_MEASURED = "measured"   # the dir enumerated and matched candidates
POOL_EMPTY = "empty"         # the dir enumerated and matched nothing
POOL_MISSING = "missing"     # the dir is not there at all
POOL_UNREAD = "unread"       # the dir is there and would not enumerate


class PoolCensus(collections.namedtuple("PoolCensus", "kind records error")):
    """ONE READER ANSWER TYPE FOR THE WHOLE TREE (task/2480 R5).

    Round 3 made the pool reader RAISE on any enumeration OSError but
    FileNotFoundError, which is the right distinction and the wrong CARRIER:
    the raise escaped a dozen callers that had no OSError handling and, in
    `providers._probe_one`, a documented never-raises contract — so one EIO on
    one directory lost every healthy sibling's scorecard row and printed a
    traceback at `helm creds`. The failure is not signalled by control flow at
    all now. Every read of the pool answers this value, ALWAYS, and each caller
    maps `unread` and `missing` onto the UNKNOWN or absent shape it ALREADY
    has — a per-account UNKNOWN row, a refusal before a write, a WARN that
    still lets a valid launch through.

    kind     one of POOL_MEASURED / POOL_EMPTY / POOL_MISSING / POOL_UNREAD
    records  [(filename, dict-or-None)] — None = present but unparseable
             (a census ROW, never an absence: R2). Empty unless MEASURED.
    error    the reason, ready to print, for MISSING and UNREAD; else None.
    """

    __slots__ = ()

    @property
    def read(self):
        """The enumeration ANSWERED — `records` is the whole truth about the
        pool. False means nothing about what is pooled may be concluded."""
        return self.kind in (POOL_MEASURED, POOL_EMPTY)

    @property
    def unknown(self):
        """The dir is THERE and would not enumerate: silence, not emptiness.
        MISSING is deliberately NOT unknown here — a pool dir that does not
        exist is a real measurement for most callers (nothing is pooled), and
        the one caller for whom it is not (doctor, which stat()ed the dir
        first, so absence means it DISAPPEARED mid-read) tests `kind`."""
        return self.kind == POOL_UNREAD


def read_pool():
    """The pool directory, read ONCE, as a PoolCensus. NEVER RAISES.

    ONE ENUMERATION, AND A FAILED ONE IS NEVER AN EMPTY ONE (task/2480 F1).
    This read was `glob.glob(os.path.join(pool_dir(), "*.json"))`, and glob
    answers `[]` to two facts that are opposites of each other:

      * a TRANSIENT enumeration OSError — glob swallows errors by contract —
        so a populated pool reads as empty for exactly one pass;
      * a LITERAL pool path carrying glob METACHARACTERS. HELM_HOME is taken
        as a literal path everywhere else in helm (`home` and `seat_paths`
        accept `/safe/helm[budget]` and the pool writer writes real files
        under it), but glob reads that bracket pair as a character class,
        matches nothing, and answers `[]` on EVERY pass, forever.

    Both answers reached `codexbudget.pool_census`, which published a MEASURED
    known-empty census over a populated directory — and a known-empty census
    is the one reading that retires a standing all-capped refusal. A refusal
    nothing had measured away was cleared by a directory nobody had read.

    So the pool is enumerated ONCE, by NAME, with no pattern language anywhere
    near the path: `os.listdir` plus `fnmatch` on the basename. FOUR ANSWERS,
    never two: a directory that is NOT THERE is MISSING (nothing is pooled,
    and that is a measurement for every caller but the one that already
    stat()ed it), an enumeration that SUCCEEDED and matched nothing is EMPTY,
    one that matched files is MEASURED, and ANY OTHER OSError is UNREAD,
    carrying its reason. UNREAD is the answer a caller may never read as
    "nothing is pooled".

    Dot-files stay excluded, as glob excluded them: `_write_pool_atomic`
    stages every write as a `.pool-*.tmp` sibling, and a reader that picked
    those up would race the writer it exists to read after."""
    d = pool_dir()
    try:
        names = os.listdir(d)
    except FileNotFoundError:
        return PoolCensus(POOL_MISSING, [],
                          "no codex credential pool at %s — nothing is pooled "
                          "(`helm codex pool <name>` feeds the proxy)" % d)
    except OSError as e:
        return PoolCensus(POOL_UNREAD, [],
                          "the codex credential pool %s would not enumerate "
                          "(%s: %s)" % (d, e.__class__.__name__, e))
    recs = [(n, _read_json(os.path.join(d, n)))
            for n in sorted(names)
            if not n.startswith(".") and fnmatch.fnmatch(n, "*.json")]
    return PoolCensus(POOL_MEASURED if recs else POOL_EMPTY, recs, None)


def _pooled_members(census):
    """[(filename, member_key, record)] for every parseable codex record in
    an ALREADY-READ census, in census order."""
    return [(fname, _member_key({"tokens": rec}), rec)
            for fname, rec in census.records
            if isinstance(rec, dict) and rec.get("type") == "codex"
            and rec.get("account_id")]


def _pool_members_matching(census, key, email=None):
    """(proven, unproven): the pool filenames on the account id of `key`
    that `_serves_member` proves are the member (`key`'s user id, `email`),
    and the ones it can prove neither way, each in census order.

    ON THE WORKSPACE ID ALONE THIS RETIRED A SIBLING (task/2981). The pool
    door retires every other spelling of the credential it writes, and this
    matched a missing user id on the account id alone. So pooling a Team
    member whose tokens carry no user-id claim retired a sibling's file, and
    pooling one that does retired a sibling's legacy file. A retirement
    cannot be undone, so only a proven spelling is retired. An unproven file
    stays, and the door names it."""
    verdicts = [(f, _serves_member(_member_row(rec), key[1], email, False))
                for f, k, rec in _pooled_members(census)
                if key[0] and k[0] == key[0]]
    return ([f for f, v in verdicts if v],
            [f for f, v in verdicts if v is None])


def _member_row(rec):
    """A pooled record as the {user_id, email, plan} row `_serves_member`
    reads. Decode-only, tokens discarded."""
    mail, plan, _acct, _exp = _identity({"tokens": rec})
    return {"user_id": _user_id({"tokens": rec}), "plan": plan,
            "email": rec.get("email") or mail}


def codex_list(census=None):
    """Every codexhome: real dirs claim the row, symlink aliases fold onto
    their target, and distinct dirs of ONE MEMBER collapse to the first.
    pooled = the pool filename currently carrying this member, else None.

    ONE MEMBER, NEVER ONE ACCOUNT ID (task/2981). On a Team plan the account
    id is the WORKSPACE, and every member carries it. A dir folds onto an
    earlier one, and a pool file links to a row, only when `_serves_member`
    proves them one member: the user ids agree, or the addresses do, or the
    plan is KNOWN to be personal. A dir whose tokens carry no user-id claim
    was folded into a sibling's row on the id alone, and read the sibling's
    pool file. It is now its own row, and `member_unproven` says that which
    member it is rests on its address alone: no user id, and no personal
    plan to make the id name one person.

    A POOL NOBODY COULD READ LEAVES `pooled` UNKNOWN, NOT FALSE (task/2480
    R5). `pooled: None` means "this account is not in the pool", which is a
    measurement; when the census is UNREAD nothing was measured, so the row
    also carries `pool_unread: True` and `pool_error`, and every reader of
    this row — the gate, the launch refusal, the printed table — says UNKNOWN
    instead of quietly reporting an unpooled fleet."""
    census = read_pool() if census is None else census
    root = homes_root()
    members = _pooled_members(census)
    links, dirs = [], []
    for p in sorted(glob.glob(os.path.join(root, "*"))):
        if os.path.islink(p) and os.path.isdir(p):
            links.append(p)
        elif os.path.isdir(p):
            dirs.append(p)
    rows, by_real, by_member = [], {}, []
    for p in dirs + links:  # real dirs first: the canonical name never loses to an alias
        real = os.path.realpath(p)
        name = os.path.basename(p)
        if real in by_real:
            by_real[real]["aliases"].append(name)
            continue
        auth_path = os.path.join(real, "auth.json")
        auth = _read_json(auth_path)
        email, plan, account_id, exp = _identity(auth)
        key = _member_key(auth)
        # SAME MEMBER, OTHER DIR (`_serves_member`): three Team members
        # sharing one workspace id are three rows, each with its own pooled
        # file, and a member with no user-id claim is never a sibling's alias
        twin = next((row for k, row in by_member if k[0] == account_id
                     and _serves_member(row, key[1], email, False)), None)
        if account_id and twin is not None:
            twin["aliases"].append(name)
            by_real[real] = twin
            continue
        mine = [(fname, rec) for fname, k, rec in members
                if account_id and k[0] == account_id]
        pooled = next((fname for fname, rec in mine if _serves_member(
            _member_row(rec), key[1], email, len(mine) > 1)), None)
        row = {"name": name, "path": real, "aliases": [],
               "authed": os.path.exists(auth_path), "email": email,
               "plan": plan, "tier": tier(plan) if plan else None,
               "account_id": account_id, "user_id": key[1], "access_exp": exp,
               "pooled": pooled,
               "member_unproven": bool(account_id) and not key[1]
               and not personal_plan(plan),
               "pool_unread": census.unknown, "pool_error": census.error
               if census.unknown else None}
        by_real[real] = row
        if account_id:
            by_member.append((key, row))
        rows.append(row)
    return rows


def _resolve_home(name):
    """(canonical_name, real_dir, err) for a home name, alias, or path."""
    name = (name or "").strip().rstrip("/")
    if not name:
        return None, None, {"error": "need a codexhome name (see `helm codex list`)"}
    root = homes_root()
    path = os.path.expanduser(name) if os.path.sep in name else os.path.join(root, name)
    if not os.path.isdir(path):
        return None, None, {"error": "no codexhome %r under %s (see `helm codex list`)"
                                     % (name, root)}
    real = os.path.realpath(path)
    canonical = os.path.basename(real) if os.path.dirname(real) == root \
        else os.path.basename(path.rstrip("/"))
    return canonical, real, None


#: `_admit_identity`'s four verdicts. The two REFUSALS are spelled so a
#: refusal sentence can carry the verdict verbatim as its reason.
ADMIT_EMPTY = "admit-into-empty"
ADMIT_REFRESH = "refresh-known-same"
REFUSE_DIFFERENT = "known-different"
REFUSE_UNKNOWN = "unknown-identity-cannot-replace-known"
ADMISSION_REFUSALS = (REFUSE_DIFFERENT, REFUSE_UNKNOWN)


def _admit_identity(holder, incoming):
    """THE ONE IDENTITY-ADMISSION RULE, asked by EVERY pool writer before it
    puts bytes under a name (task/2514 cured it at `_pool_provision_locked`;
    task/2517 found `_pool_auth_locked` admitting on its own and moved the
    rule here so it has exactly one home). `holder` is the account id the
    standing pool file carries (None for a free name, or a member that names
    none); `incoming` is the account id the credential about to be written
    carries (None when `translate_codex_auth` and `_identity` both find no
    id). Four verdicts, in the order they are decided:

      no holder                       -> ADMIT_EMPTY    nothing known stands
                                                        under the name
      holder, no incoming             -> REFUSE_UNKNOWN an identity that
                                                        cannot be named cannot
                                                        prove it is the holder's
      holder == incoming              -> ADMIT_REFRESH  the same account,
                                                        fresher bytes
      holder != incoming              -> REFUSE_DIFFERENT the name is not the
                                                        account; the standing
                                                        credential is somebody's

    A writer branches on the verdict and words its own refusal in its own
    caller's vocabulary; the DECISION is never re-derived at a writer. A
    verdict in `ADMISSION_REFUSALS` means: write nothing."""
    if not holder:
        return ADMIT_EMPTY
    if not incoming:
        return REFUSE_UNKNOWN
    return ADMIT_REFRESH if holder == incoming else REFUSE_DIFFERENT


def _pool_auth(src, canonical, stale_fix, refuse_stale_src=False):
    """The pool write for a SOURCE FILE, whatever store holds it: translate a
    codex auth.json into the proxy flat record and write it 0600 into the
    pool dir as codex-<canonical>.json. (`pool_provision` is the sibling door
    for an already-translated record — `helm seat add codex` — and both end
    in `_write_pool_atomic` under the same `pool_lock`.) Idempotent — re-pooling refreshes the copy
    (that IS the stale-401 cure). src is READ-ONLY forever; no token material
    in the result. stale_fix = the source-specific cure sentence the past-exp
    WARN carries (the warn-never-refuse law applies to EVERY source).
    refuse_stale_src = the FRESHNESS RUNG (managed-source callers): when the
    dest exists with DIFFERENT bytes and src's mtime is strictly older,
    refuse instead of writing — pooling an older file over a fresher cred is
    credential REGRESSION, not a refresh (measured 2026-08-04 19:10Z: a
    July-26 orca-managed copy overwrote an 18:17Z login grant and the codex
    family went auth-dark for 11 minutes). Byte-identical still skips ahead
    of this rung (an identical copy cannot regress anything), and a missing
    dest accepts src freely (first pool).

    THE CALLER OWNS THE NAME, THIS FUNCTION OWNS BYTES AND ADMISSION.
    `canonical` names the destination and the name is the caller's claim —
    `codex-<canonical>.json` is not injective over account ids (`helm codex
    pool` names a file after a HOME, `cred_follow` after an email-and-plan,
    `sync-orca` after the matching home or the email slug, and two accounts
    can share any of them). A caller that can prove the destination first
    should (`cred_follow` does, with its COLLISION row, and names the holder
    in its own vocabulary) — but the proof is NOT the caller's to skip:
    immediately before the write, under the lock, the standing member's
    account id and the incoming record's are put to `_admit_identity`, the
    same rule `_pool_provision_locked` asks, and a refusing verdict returns
    the ordinary `{"error": ...}` shape with nothing written (task/2517:
    this door admitted on its own and let a first RPC `sync-orca` replace a
    KNOWN pooled account with a credential naming no account at all).

    THE KILL SWITCH SURVIVES A REFRESH. `disabled` is the proxy's own
    kill-switch and an operator's deliberate act; it is born False on a NEW
    record and PRESERVED whenever the destination already carries it — whether
    the destination holds the same account or (a caller's mistake) another
    one. The flag is preserved UNCONDITIONALLY because no caller anywhere in
    helm re-enables a pooled cred: this line and this line alone writes the
    field, nothing reads a `--re-enable` flag, and an operator who set it by
    hand is the only one who can clear it by hand. The old code wrote
    `disabled = False` on every pass, so a refresh of a deliberately parked
    credential quietly armed it again.

    SERIALIZED. The whole body runs under `pool_lock`, so the destination
    re-reads this function already does (the byte-identical skip, the
    freshness rung, the kill-switch preservation) cannot be invalidated by a
    second writer between the read and `os.replace`. The lock is re-entrant,
    so a caller already holding it across a wider prove-then-write —
    `cred_follow` — keeps ONE boundary rather than opening a second."""
    with pool_lock():
        return _pool_auth_locked(src, canonical, stale_fix, refuse_stale_src)


def _pool_auth_locked(src, canonical, stale_fix, refuse_stale_src):
    """`_pool_auth`'s body, run under the caller's `pool_lock`. Identity is
    admitted through `_admit_identity` — the one rule shared with
    `_pool_provision_locked` — before the byte-identical skip and the
    freshness rung, because a fresher stranger is still a stranger."""
    rec, _fname, terr = _seat.translate_codex_auth(src)
    if terr:
        return {"error": terr}
    rec["disabled"] = False  # the proxy's own kill-switch field, born live
    src_auth = _read_json(src)
    email, plan, account_id, exp = _identity(src_auth)
    if not rec.get("account_id") and account_id:
        # translate + _identity share one resolution today; this guarantees the
        # pooled record ALWAYS carries what identity knows even if they ever
        # diverge — dedup, list linkage, and seat-add preservation key off it.
        rec["account_id"] = account_id
    # A POOL WE CANNOT READ IS A POOL WE MAY NOT WRITE (task/2480 R5). The
    # dupe scan is not decoration: it is how a re-pool learns the SAME account
    # already sits under another filename, and pooling blind leaves two live
    # copies of one credential with nobody told. An unread census also cannot
    # be distinguished from an empty one, so "no dupes" would be a claim made
    # out of an error. Refuse BEFORE the atomic write — nothing is written.
    census = read_pool()
    if census.unknown:
        return {"error": "%s — refusing to pool over a directory this pass "
                         "could not read (a blind write can leave two live "
                         "copies of one account); fix the pool dir and re-run"
                         % census.error}
    # THE OTHER SPELLINGS OF THIS CREDENTIAL (`_member_key`: same account,
    # same user — never a Team sibling that merely shares the workspace id).
    # They are RETIRED below, under this lock, once the survivor's bytes are
    # on disk and read back — never before, or a raising write leaves the
    # credential with no spelling at all:
    # `helm codex pool <home>` names a file after the HOME and `cred-follow`
    # after the email-and-plan, so one credential reached the proxy twice
    # (measured: one owner login under both spellings, both cooled at
    # the same reset, the refusal counting "6 cooling down" over five
    # credentials). The seat-add door (`_pool_provision_locked`) has always
    # retired them; this door only WARNED, and nobody read the warning.
    key = _member_key(src_auth)
    if not key[0] and rec.get("account_id"):
        key = (rec["account_id"], key[1])
    mine = "codex-%s.json" % canonical
    proven, unproven = _pool_members_matching(census, key,
                                              email or rec.get("email"))
    dupes = [f for f in proven if f != mine]
    unproven = [f for f in unproven if f != mine]
    dest = os.path.join(pool_dir(), "codex-%s.json" % canonical)
    existed = os.path.exists(dest)
    prev = _read_json(dest) if existed else None
    # IDENTITY BEFORE BYTES (task/2517): the standing member's account id
    # against the incoming record's, through the one rule every pool writer
    # asks. Refused verdicts write nothing and ride the ordinary error shape,
    # which every caller already renders as rc 1 — `--watch` reports it and
    # polls again, `cred_follow` files an ERROR row, `codex pool` prints it.
    holder = prev.get("account_id") if isinstance(prev, dict) else None
    verdict = _admit_identity(holder, rec.get("account_id"))
    if verdict == REFUSE_DIFFERENT:
        return {"error": "the pool file %s already holds account %s, and %s "
                         "carries account %s — one file name, two accounts. "
                         "Refusing to write over another account's "
                         "credential; nothing was written and the source is "
                         "untouched. Pool it deliberately under a name of "
                         "its own with `helm codex pool <home>`, or retire "
                         "the standing file with `helm codex unpool %s`"
                         % (dest, _short(holder), src,
                            _short(rec.get("account_id")),
                            os.path.basename(dest))}
    if verdict == REFUSE_UNKNOWN:
        return {"error": "the pool file %s already holds account %s, and %s "
                         "names NO account id (%s) — an identity that cannot "
                         "be named cannot prove it is the holder's. Refusing "
                         "to write over a known account's credential; "
                         "nothing was written and the source is untouched. "
                         "Re-login the source so its tokens carry the "
                         "account id, or retire the standing file with "
                         "`helm codex unpool %s`"
                         % (dest, _short(holder), src, REFUSE_UNKNOWN,
                            os.path.basename(dest))}
    # the destination's own kill-switch, re-read from the file about to be
    # replaced — never inferred from the source, which cannot know it. A
    # spelling about to be RETIRED carries its park across to the survivor:
    # an operator parked the CREDENTIAL, whatever file it sat in.
    standing = dict(census.records)
    kept_disabled = bool(isinstance(prev, dict) and prev.get("disabled")) or any(
        isinstance(standing.get(f), dict) and standing[f].get("disabled")
        for f in dupes)
    if kept_disabled:
        rec["disabled"] = True
    body = json.dumps(rec, indent=2) + "\n"
    warn = None
    if exp is not None and exp <= time.time():
        warn = "access token exp is past — " + stale_fix
    if kept_disabled:
        kept = ("%s carries disabled=true — the proxy's kill-switch, set "
                "deliberately — so the refreshed record KEEPS it; nothing in "
                "helm clears that field, `helm codex pooled` shows it"
                % os.path.basename(dest))
        warn = kept if warn is None else warn + "; " + kept
    if unproven:
        left = ("%s carr%s this account id and cannot be proven to be this "
                "member or another (no user id and no address to compare), "
                "so %s NOT retired: on a Team plan it may be a sibling's"
                % (", ".join(unproven), "ies" if len(unproven) == 1 else "y",
                   "it is" if len(unproven) == 1 else "they are"))
        warn = left if warn is None else warn + "; " + left
    base = {"ok": True, "pooled": os.path.basename(dest), "path": dest,
            "email": email or rec.get("email"), "account_id": account_id,
            "user_id": key[1],
            "tier": tier(plan) if plan else "?", "updated": existed,
            "also_pooled_as": dupes or None, "retired": [], "warn": warn,
            "unproven": unproven or None,
            "disabled": rec["disabled"], "kept_disabled": kept_disabled,
            "note": "the proxy hot-reloads its auth-dir — no restart needed"}
    if existed and not dupes and _read_text(dest) == body:
        # bytes-identical: skip the write entirely — an identical rewrite
        # cures nothing (the stale-401 cure IS fresh bytes) and only churns
        # the proxy's hot-reload watcher + the file mtime.
        base.update(updated=False, unchanged=True,
                    note="pool already carries these exact bytes — untouched")
        return base
    if refuse_stale_src and existed:
        try:
            src_m, dest_m = os.stat(src).st_mtime, os.stat(dest).st_mtime
        except OSError:
            src_m = dest_m = None      # dest raced away — nothing to regress
        if src_m is not None and src_m < dest_m:
            return {"error":
                    "refusing to pool orca's managed copy over a FRESHER "
                    "pooled cred: %s (%s) is older than %s (%s) — orca "
                    "refreshes a managed home only while actively using the "
                    "account, so an idle copy can be arbitrarily stale; "
                    "override deliberately with `helm codex sync-orca "
                    "--force-managed` or HELM_CODEX_FORCE_MANAGED=1"
                    % (src, _utc(src_m), dest, _utc(dest_m))}
    # THE SURVIVOR IS WRITTEN AND PROVEN BEFORE ANY SPELLING IS RETIRED.
    #
    # The old order removed the other spellings first, so the proxy's
    # hot-reload could never hold one credential under two names. But
    # `_write_pool_atomic` can RAISE — a full disk, a pool dir that lost its
    # write bit, a replace into a directory that went away — and on a re-key
    # the destination is a NEW name: `dupes` excludes it and `existed` is
    # False, so every standing spelling was already gone and the raise left
    # the pool holding ZERO copies of that credential. The seat on it goes
    # auth-dark, and the traceback says nothing about what was deleted.
    #
    # WRITE-FIRST TRADES A MOMENT FOR A CREDENTIAL. Between the replace and
    # the unlinks, still under this lock, the credential can be readable under
    # two names for the length of an unlink, and the proxy counts one cooldown
    # twice for that instant. That is recoverable by the next pass and by
    # simply waiting; losing every copy is not.
    try:
        _write_pool_atomic(dest, body)
    except OSError as e:
        return {"error": "the pool write to %s FAILED (%s) — NOTHING was "
                         "retired, so the credential keeps every spelling it "
                         "had (%s), and the source is untouched. Re-run once "
                         "the pool dir is writable"
                         % (dest, e, ", ".join(dupes) or "this one only")}
    # READ BACK BEFORE DELETING. A retirement is irreversible and the only
    # thing that justifies it is the survivor being on disk; a write that
    # returned without leaving these bytes is not that.
    if _read_text(dest) != body:
        return {"error": "the pool write to %s did not read back as the bytes "
                         "it was given — NOTHING was retired, so the "
                         "credential keeps every spelling it had (%s). "
                         "Another writer holds the pool dir, or the "
                         "filesystem did not keep the replace"
                         % (dest, ", ".join(dupes) or "this one only")}
    for f in dupes:
        # ONE FILE PER CREDENTIAL — retired now that the survivor is on disk
        # and has been read back, in the same locked breath.
        try:
            os.remove(os.path.join(pool_dir(), f))
            base["retired"].append(f)
        except FileNotFoundError:
            pass
    return base


def codex_pool(name):
    """Translate one codexhome's auth.json into the proxy flat record and
    write it 0600 into the pool dir as codex-<canonical-name>.json.
    The source file is never touched; no token material in the result."""
    canonical, real, err = _resolve_home(name)
    if err:
        return err
    src = os.path.join(real, "auth.json")
    if not os.path.exists(src):
        return {"error": "%s has no auth.json — not logged in; human-only: "
                         "CODEX_HOME=%s codex login --device-auth" % (real, real)}
    return _pool_auth(src, canonical,
                      "the codex CLI autorefreshes, so run `CODEX_HOME=%s "
                      "codex` once and re-pool the FRESH token (the proxy's "
                      "own refresh can 401 on a stale copy)" % real)


def codex_unpool(name):
    """Remove the pooled file for a home (canonical + given-name spellings
    both tried). Fail-open: nothing pooled under that name is ok, not error."""
    given = (name or "").strip().rstrip("/")
    if not given:
        return {"error": "need a codexhome name (see `helm codex pooled`)"}
    cands = [given] if given.endswith(".json") else ["codex-%s.json" % given]
    canonical, _real, err = _resolve_home(given)
    if not err and canonical and "codex-%s.json" % canonical not in cands:
        cands.append("codex-%s.json" % canonical)  # alias given, canonical pooled
    removed = []
    # A DELETION IS A POOL WRITE (task/2478 R4): it runs under the same lock
    # every other writer takes, so a follow that proved a name free, or a seat
    # add that proved it held, cannot have the file vanish between its proof
    # and its write.
    with pool_lock():
        for fname in cands:
            p = os.path.join(pool_dir(), fname)
            if os.path.isfile(p):
                os.remove(p)
                removed.append(fname)
    return {"ok": True, "removed": removed,
            "note": None if removed else
            "nothing pooled under %r (fail-open — already absent)" % given}


def pool_provision(rec, fname):
    """`helm seat add codex`'s pool write, THROUGH THE ONE DOOR (task/2478 R4).

    THE LOCK CANNOT SERIALIZE A WRITER THAT NEVER TAKES IT. Putting
    `cred_follow`'s re-proof and write under `pool_lock` covers only the
    writers that take that lock: a seat mint that scanned, removed and wrote
    the very same auth directory from `seat_provision` with its own glob and
    its own `_write_private` would land between the follow's locked re-proof
    and its write and still be replaced by orca's account. Every write into
    the pool therefore goes through THIS module under THIS lock — the follow (`_pool_auth`), the operator pool (`codex_pool`), the
    deletion (`codex_unpool`) and the seat mint (here) — and
    `tests.test_codexhomes.FollowHasOneDoorTest` reads the shipped source to
    keep it that way.

    `rec` is the translated proxy record (`translate_codex_auth`), `fname` the
    name that translator suggested. Under the lock, in order:

      * the pool dir is minted 0700 and read ONCE (`read_pool`); an UNREAD
        census refuses before anything is written (task/2480 R5);
      * IDENTITY RE-PROOF through `_admit_identity` (task/2478 R4 +
        task/2514; ONE rule for every pool writer since task/2517 — the
        source-file door `_pool_auth` asks the same function, so `helm codex
        pool`, `sync-orca` and `cred-follow` admit exactly as this door
        does): `codex-<email>-<plan>.json` is not injective over account
        ids, so the standing file under `fname` is read for WHOSE it is —
          KNOWN-SAME    the standing file carries the account this mint
                        names: a re-add IS that account's refresh, replaced;
          KNOWN-DIFFERENT  it carries ANOTHER account id: somebody else's
                        credential — REFUSED, naming both accounts and the
                        file;
          UNKNOWN INCOMING  this mint's own record carries NO account id
                        (`translate_codex_auth` admits a credential whose
                        tokens name no account) while the standing file
                        holds a KNOWN account: an identity that cannot be
                        named cannot prove it is the holder's, so it is
                        REFUSED — an unknown identity never replaces a known
                        pooled account (task/2514: before this rung the
                        collision test required BOTH ids and a nameless
                        credential fell through it and overwrote account B);
        a standing file that itself carries no readable account id, and an
        EMPTY slot, are written as before: this door is an operator's
        explicit mint, and nothing known is displaced (the unattended follow
        is the stricter one and never writes over an unreadable member);
      * every OTHER `codex-*.json` carrying the same account id is retired
        (a stale spelling of the account being re-added) — AFTER the new
        record is written and read back, so a failed write retires nothing
        and the account never loses its last copy; every other member
        — other accounts and unattributable junk alike — is KEPT and counted
        (the proxy's usage-cap fall-through survives a seat re-add, and
        nothing that cannot be attributed is ever deleted);
      * the kill-switch survives: a standing same-account record carrying
        `disabled=true` keeps it, exactly as `_pool_auth` preserves it;
      * the record is written 0600 through `_write_pool_atomic`.

    Returns {"ok", "pooled", "path", "removed": [name], "kept": n,
    "kept_disabled", "updated"} or {"error"}. No token material in either."""
    with pool_lock():
        return _pool_provision_locked(rec, fname)


def _pool_provision_locked(rec, fname):
    """`pool_provision`'s body, run under the caller's `pool_lock`. Identity
    is admitted through `_admit_identity` — the one rule shared with
    `_pool_auth_locked` — and the two refusing verdicts are worded here in
    the seat-add vocabulary (their text is pinned by arms)."""
    if not (isinstance(rec, dict) and rec.get("type") == "codex"):
        return {"error": "refusing to provision the codex pool with a record "
                         "that is not a translated codex credential"}
    if not (isinstance(fname, str) and fname.startswith("codex-")
            and fname.endswith(".json") and os.sep not in fname
            and fname not in ("codex-.json",)):
        return {"error": "refusing to provision the codex pool under %r — a "
                         "pool member is named codex-<email>-<plan>.json"
                         % (fname,)}
    d = pool_dir()
    os.makedirs(d, mode=0o700, exist_ok=True)
    census = read_pool()
    if census.unknown:
        return {"error": "%s — refusing to provision over a directory this "
                         "pass could not read (a blind write can replace "
                         "another account's credential or leave two live "
                         "copies of one); fix the pool dir and re-run"
                         % census.error}
    acct = rec.get("account_id")
    standing = dict(census.records)
    prev = standing.get(fname)
    holder = prev.get("account_id") if isinstance(prev, dict) else None
    dest = os.path.join(d, fname)
    verdict = _admit_identity(holder, acct)
    if verdict == REFUSE_DIFFERENT:
        return {"error": "the pool file %s already holds account %s, and this "
                         "seat add mints account %s — one email and one plan, "
                         "two accounts, one file name. Refusing to write over "
                         "another account's credential; nothing was written. "
                         "Pool this account deliberately under a name of its "
                         "own with `helm codex pool <home>`, or retire the "
                         "standing file with `helm codex unpool %s`"
                         % (dest, _short(holder), _short(acct), fname)}
    if verdict == REFUSE_UNKNOWN:
        # UNKNOWN INCOMING against a KNOWN holder (task/2514). The collision
        # verdict above needs both ids to compare; a credential that names no
        # account is not thereby the holder's, and letting it fall through
        # replaced a known pooled account with one nobody can attribute.
        return {"error": "the pool file %s already holds account %s, and this "
                         "seat add mints a credential that names NO account "
                         "id (unknown-identity-cannot-replace-known) — an "
                         "identity that cannot be named cannot prove it is "
                         "the holder's. Refusing to write over a known "
                         "account's credential; nothing was written. Re-login "
                         "the source so its tokens carry the account id, or "
                         "pool it deliberately under a name of its own with "
                         "`helm codex pool <home>`, or retire the standing "
                         "file with `helm codex unpool %s`"
                         % (dest, _short(holder), fname)}
    removed, kept, stale = [], 0, []
    for name, old in census.records:
        if name == fname or not name.startswith("codex-"):
            continue
        old_acct = old.get("account_id") if isinstance(old, dict) else None
        if old_acct and old_acct == acct:
            stale.append(name)                # same account, stale spelling
            continue
        kept += 1
    out = dict(rec)
    kept_disabled = bool(isinstance(prev, dict) and prev.get("disabled"))
    out["disabled"] = kept_disabled
    body = json.dumps(out, indent=2, sort_keys=False) + "\n"
    # THE SAME ORDER AS `_pool_auth_locked`, FOR THE SAME REASON. This door
    # deletes the account's other spellings too, and `fname` is excluded from
    # them, so a seat add under a NEW name that then failed to write left the
    # account with no pooled copy at all. The survivor is written and read
    # back first; a raised or unproven write retires nothing and says so.
    try:
        _write_pool_atomic(dest, body)
    except OSError as e:
        return {"error": "the pool write to %s FAILED (%s) — NOTHING was "
                         "retired, so the account keeps every spelling it had "
                         "(%s). Re-run once the pool dir is writable"
                         % (dest, e, ", ".join(stale) or "this one only")}
    if _read_text(dest) != body:
        return {"error": "the pool write to %s did not read back as the bytes "
                         "it was given — NOTHING was retired, so the account "
                         "keeps every spelling it had (%s). Another writer "
                         "holds the pool dir, or the filesystem did not keep "
                         "the replace"
                         % (dest, ", ".join(stale) or "this one only")}
    for name in stale:
        try:
            os.remove(os.path.join(d, name))
            removed.append(name)
        except FileNotFoundError:
            pass
    return {"ok": True, "pooled": fname, "path": dest, "removed": removed,
            "kept": kept, "kept_disabled": kept_disabled,
            "updated": fname in standing}


def pooled_rows(census):
    """The `codex_pooled` rows of an ALREADY-READ census — the shape, without
    a second enumeration. A caller that must tell an empty pool from an
    unread one reads `census.kind` and calls this with the same value."""
    rows = []
    for fname, rec in census.records:
        if not isinstance(rec, dict):
            rows.append({"file": fname, "error": "unparseable"})
            continue
        _email, plan, _acct, _exp = _identity({"tokens": rec})
        rows.append({"file": fname, "type": rec.get("type"),
                     "email": rec.get("email"), "account_id": rec.get("account_id"),
                     "user_id": _user_id({"tokens": rec}),
                     "plan": plan, "tier": tier(plan) if plan else None,
                     "disabled": bool(rec.get("disabled")),
                     "expired": rec.get("expired")})
    return rows


def codex_pooled():
    """What the proxy can actually draw on: every *.json in its auth-dir with
    identity metadata only (unparseable files reported, never fatal).

    NEVER RAISES, AND THEREFORE ANSWERS `[]` TO THREE DIFFERENT FACTS — an
    empty pool, an absent one, and one that would not enumerate. That is
    fine for the readouts that only ever LIST what is there; a caller that
    must tell those apart (doctor's intent-vs-actual rung, whose whole
    contract is "unreadable is UNKNOWN, never a measured zero") calls
    `read_pool()` and `pooled_rows()` itself."""
    return pooled_rows(read_pool())


# ---------------------------------------------------------------- usage gate

def _latest_rate_limits(real_home):
    """Newest rate_limits event in a credhome's own rollout logs — the local
    ground truth for headroom (the codex CLI appends one per turn; the usage
    MCP's codex feed is just these, forwarded). Reads only the newest
    SCAN_ROLLOUTS files' last TAIL_BYTES. Returns the rate_limits dict plus
    {"file": path} for attribution, or None when the home has no recent
    rollout telemetry at all (never-used / pre-rate_limits CLI = UNKNOWN)."""
    root = os.path.join(real_home, "sessions")
    try:
        files = [os.path.join(dp, f) for dp, _dn, fn in os.walk(root)
                 for f in fn if f.startswith("rollout-") and f.endswith(".jsonl")]
    except OSError:
        return None
    files.sort(key=lambda p: os.path.basename(p), reverse=True)  # ts in name
    for path in files[:SCAN_ROLLOUTS]:
        try:
            size = os.path.getsize(path)
            with open(path, "rb") as f:
                if size > TAIL_BYTES:
                    f.seek(-TAIL_BYTES, os.SEEK_END)
                tail = f.read().decode("utf-8", "replace")
        except OSError:
            continue
        for line in reversed(tail.splitlines()):
            if "rate_limits" not in line:
                continue
            try:
                ev = json.loads(line)
                rl = ((ev.get("payload") or {}).get("rate_limits")
                      if isinstance(ev, dict) else None)
            except ValueError:
                continue            # mid-line tail cut; older lines are whole
            if isinstance(rl, dict) and isinstance(rl.get("primary"), dict):
                rl["file"] = path
                return rl
    return None


def usage_gate(census=None):
    """The launch headroom verdict per credhome, from the homes' own rollout
    rate_limits. Statuses: ok (freshest event fresh + every live window under
    NEAR_PCT), near (>= NEAR_PCT or a reached-type recorded), exhausted
    (a live window at 100%), unknown (no rollout telemetry or stale >
    STALE_S — stale/unread = NOT ok, the runbook's core rule).

    THE POOL LINKAGE CARRIES ITS OWN UNCERTAINTY (task/2480 R5). `pooled` is
    what makes a row count toward `launch_gate`'s "at least one POOLED cred
    reads ok", so an unread pool would silently empty the pooled set and turn
    a healthy fleet into a refusal with no reason printed. The row therefore
    carries `pool_unread` straight off `codex_list`, and the gate says so."""
    now = time.time()
    rows = []
    for h in codex_list(census):
        if not h["authed"]:
            continue
        row = {"name": h["name"], "email": h["email"], "tier": h["tier"],
               "pooled": h["pooled"], "account_id": h["account_id"],
               "pool_unread": h.get("pool_unread", False),
               "pool_error": h.get("pool_error"), "status": "unknown"}
        rl = _latest_rate_limits(h["path"])
        if rl:
            age = now - os.path.getmtime(rl.pop("file"))
            row["age_s"] = int(age)
            if age > STALE_S:
                row["status"] = "unknown"
                row["note"] = "rollout tail stale (%dm > %dm)" % (
                    age // 60, STALE_S // 60)
            else:
                worst, live = 0.0, False
                for w in (rl.get("primary"), rl.get("secondary")):
                    if not isinstance(w, dict):
                        continue
                    resets = w.get("resets_at")
                    if isinstance(resets, (int, float)) and resets <= now:
                        continue          # window over — no longer binding
                    live = True
                    pct = w.get("used_percent")
                    if isinstance(pct, (int, float)):
                        worst = max(worst, pct)
                row["pct"] = worst
                if rl.get("rate_limit_reached_type"):
                    row["status"], row["reached"] = "exhausted", \
                        rl["rate_limit_reached_type"]
                elif not live:
                    # a fresh event whose windows have ALL reset binds
                    # nothing — the freshest reading there is = green
                    row["status"] = "ok"
                elif worst >= 100.0:
                    row["status"] = "exhausted"
                elif worst >= NEAR_PCT:
                    row["status"] = "near"
                else:
                    row["status"] = "ok"
        rows.append(row)
    return rows


def _suggest_pool(gate):
    """The concrete fix: best unpooled credhome to `helm codex pool` next —
    prefer an ok-status ultra, then any ok, then the top ultra regardless
    (its reading is the likeliest to improve once the CLI refreshes it)."""
    unpooled = [g for g in gate if not g["pooled"]]
    for pred in (lambda g: g["status"] == "ok" and g["tier"] == "ultra",
                 lambda g: g["status"] == "ok",
                 lambda g: g["tier"] == "ultra"):
        hits = [g for g in unpooled if pred(g)]
        if hits:
            return hits[0]
    return None


def launch_gate(inst=1, force=False, out=None):
    """Refuse-by-default cred-% gate ahead of a codex seat launch (runbook
    fix #3). Pooled rows near/exhausted/unknown are the problem classes; a
    launch is allowed while at least one POOLED cred reads ok, otherwise
    rc 1 with the concrete `helm codex pool <name>` fix on stderr (--force
    overrides: the gate advises, the operator decides — same law as seat
    launch's own warns). Prints the gate table; returns the rc.

    A POOL THIS PASS COULD NOT READ REFUSES, AND SAYS THAT (task/2480 R5).
    The admission rule is "at least one POOLED cred reads ok", and an
    unenumerable pool empties the pooled set — so the pre-cure shapes were
    both wrong in opposite directions: a raise took the whole launch down with
    a traceback, and swallowing it printed the ordinary "no pooled cred reads
    ok; the fix is pooling" over a fleet that may be perfectly pooled. Never
    admit on a failed read, and never send the operator to `helm codex pool`
    for a directory problem. `--force` still overrides, like every other
    verdict this gate reaches."""
    out = out or sys.stderr
    census = read_pool()
    gate = usage_gate(census)
    pooled = [g for g in gate if g["pooled"]]
    print("helm codex: launch gate (fresh = rollout tail <%dm, near >= %.0f%%)"
          % (STALE_S // 60, NEAR_PCT), file=out)
    for g in gate:
        mark = "pooled" if g["pooled"] else "-"
        extra = (" %.0f%% used" % g["pct"]) if g.get("pct") is not None else ""
        if g.get("note"):
            extra += " (%s)" % g["note"]
        if g.get("reached"):
            extra += " (reached %s)" % g["reached"]
        print("  %-6s %-28s %-32s %-6s %-6s%s" % (
            g["status"], g["name"], g["email"] or "-", g["tier"] or "?",
            mark, extra), file=out)
    if census.unknown:
        if force:
            print("helm codex: WARN — --force over an UNREAD pool (%s); "
                  "whether any cred is pooled is unknown" % census.error,
                  file=out)
            return 0
        print("helm codex: REFUSE — %s. Nothing about this fleet's pooling "
              "was measured, so admitting would be a guess; this is a "
              "DIRECTORY fault, not a missing cred — pooling another account "
              "will not cure it" % census.error, file=out)
        print("  then re-run, or override: helm codex launch -i %d --force"
              % inst, file=out)
        return 1
    ok_pool = [g for g in pooled if g["status"] == "ok"]
    if ok_pool or force:
        if not ok_pool:
            print("helm codex: WARN — --force over a gate with no ok pooled "
                  "cred; the pool may 429 under load", file=out)
        return 0
    fix = _suggest_pool(gate)
    print("helm codex: REFUSE — no pooled cred reads ok "
          "(near/exhausted/unknown); the fix is pooling, not retrying", file=out)
    if fix:
        print("  fix: helm codex pool %s   # %s, %s%s" % (
            fix["name"], fix["email"] or "-", fix["tier"] or "?",
            " (currently %s)" % fix["status"] if fix["status"] != "ok" else ""),
            file=out)
    print("  then re-run, or override: helm codex launch -i %d --force" % inst,
          file=out)
    return 1


def _run_launch(rest):
    """Delegate to the seat-launch mint (codex family): ONE mint path — the
    gate only guards entry to it; -i/--room/--model pass straight through."""
    return _seat.cmd_seat(["launch", "codex"] + list(rest))


# ------------------------------------------------------------ orca sync leg

# `--watch` cadence: selection flips are human-paced, and the pool write rides
# the CHANGE, never the tick. HELM_CODEX_SYNC_POLL_S moves it (floor 5s).
SYNC_POLL_S = 30
# accounts.list refreshes provider state daemon-side before answering (orca
# accounts.ts: refreshAccountsForMobile), so it can outlive the 5s
# pane-resolution budget — this call carries its own window. Measured live
# 2026-08-04 against the real daemon: 15.5-16.4s per call (the refresh runs
# EVERY call, warm or cold), so the old 15s budget lost the race every time
# and misread a healthy daemon as unreachable. 30 covers the measured band
# with margin; the CLI fallback covers a daemon that blows even that.
ACCOUNTS_TIMEOUT_S = 30
# `orca account list` answered instantly live even while the RPC leg was
# timing out (it reads state without the provider refresh) — 20 is generous.
CLI_TIMEOUT_S = 20


def _sync_poll_s():
    try:
        return max(5, int(home.env("CODEX_SYNC_POLL_S") or SYNC_POLL_S))
    except ValueError:
        return SYNC_POLL_S


def _accounts_timeout_s():
    # HELM_CODEX_ACCOUNTS_TIMEOUT_S: the test seam (a hanging fake daemon
    # must time out in ~1s, not 30) doubling as the operator override.
    try:
        return max(1, int(home.env("CODEX_ACCOUNTS_TIMEOUT_S")
                          or ACCOUNTS_TIMEOUT_S))
    except ValueError:
        return ACCOUNTS_TIMEOUT_S


def _orca_adapter():
    # the ONE orca daemon client (fail-open rpc, HELM_ORCA_RPC kill-switch,
    # auth token never surfaced) — never a second socket implementation here
    from .harness import OrcaAdapter
    return OrcaAdapter()


def _orca_socket_tried():
    """What the daemon client actually aimed at, for the absent-daemon exit:
    the unix endpoint from orca-runtime.json when readable, else the metadata
    path itself (with no metadata, no socket was ever tried)."""
    meta_path = os.path.join(_orca_adapter()._user_data_path(),
                             "orca-runtime.json")
    meta = _read_json(meta_path)
    transports = (meta or {}).get("transports")
    if not isinstance(transports, list):
        transports = [(meta or {}).get("transport")]
    t = next((t for t in transports if isinstance(t, dict)
              and t.get("kind") == "unix" and t.get("endpoint")), None)
    return t["endpoint"] if t else meta_path


def _parse_cli_accounts(text):
    """(codex-state, err) from `orca account list` prose. Scoped STRICTLY to
    the "Managed Codex accounts" section — the Claude section above it carries
    its own `(active)` marker that must never leak into codex selection. Rows
    become {id: email, email} (the email IS the CLI's whole identity; account
    ids are RPC-only) and the `(active)` row becomes activeAccountId. An
    empty codex section is a VALID zero-account roster; a MISSING header is a
    parse error (older/foreign CLI) so the caller can report both dead
    routes instead of a silent empty."""
    accounts, active, saw, in_codex = [], None, False, False
    for line in text.splitlines():
        if not line.strip():
            continue
        if not line[:1].isspace():           # a section header, not a row
            in_codex = line.strip().lower().startswith(
                "managed codex accounts")
            saw = saw or in_codex
            continue
        if not in_codex:
            continue
        entry = line.strip()
        is_active = entry.endswith("(active)")
        email = entry[:-len("(active)")].strip() if is_active else entry
        if not email:
            continue
        accounts.append({"id": email, "email": email})
        if is_active:
            active = email
    if not saw:
        return None, ("orca account list output has no 'Managed Codex "
                      "accounts' section — cannot read the roster")
    return {"accounts": accounts, "activeAccountId": active,
            "_helm_source": "cli"}, None


def _orca_cli_codex_state():
    """(codex-state, err) — the DEGRADED subprocess twin of the accounts.list
    RPC (the OrcaAdapter.panes() dual-source pattern: two routes, ONE
    downstream shape). The binary is discovered exactly as the adapter's CLI
    verbs discover it (PATH lookup of its `bin`); HELM_ORCA_CLI pins a path
    or switches the route off — the same live-workspace kill-switch law as
    HELM_ORCA_RPC (harness._runtime_call), because this leg reaches the REAL
    orca account roster. Output is prose, so the snapshot carries email +
    active only, stamped _helm_source=cli; no token material ever appears in
    `orca account list` output or in these errors."""
    pin = (home.env("ORCA_CLI") or "").strip()
    if pin.lower() in ("off", "0", "none", "false"):
        return None, "orca CLI fallback disabled (HELM_ORCA_CLI)"
    try:
        p = subprocess.run([pin or _orca_adapter().path, "account", "list"],
                           capture_output=True, text=True,
                           timeout=CLI_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, "orca account list: %s" % e
    if p.returncode != 0:
        return None, "orca account list: rc %d — %s" % (
            p.returncode, (p.stderr or p.stdout or "").strip()[:200])
    return _parse_cli_accounts(p.stdout)


def _orca_codex_state():
    """(codex branch of orca's AccountsSnapshot, err). RPC FIRST — the rich
    source (account ids, activeAccountIdsByRuntime, systemDefault; shape per
    orca src/shared/types.ts CodexRateLimitAccountsState: {accounts: [{id,
    email, providerAccountId, ...}], activeAccountId,
    activeAccountIdsByRuntime?: {host, wsl}, systemDefault?: {hasAuth,
    authKind, email, providerAccountId}}) — then the CLI roster as the
    fallback, so a slow/wedged daemon degrades the sync instead of blinding
    it. err only when BOTH routes fail, and it carries both lines."""
    result, rerr = _orca_adapter().rpc("accounts.list", {},
                                       timeout=_accounts_timeout_s())
    if not rerr:
        codex = (result or {}).get("codex")
        if isinstance(codex, dict):
            return codex, None
        rerr = "accounts.list answered without a codex account state"
    codex, cerr = _orca_cli_codex_state()
    if codex is not None:
        return codex, None
    return None, "rpc: %s; cli fallback: %s" % (rerr, cerr)


def _orca_selected(codex):
    """(identity, err) — the codex account orca selects for the HOST runtime
    (helm shares orca's host; WSL slots select for other roots). identity =
    {id, email, account_id}; id None = orca's system-default slot (the real
    ~/.codex), whose identity rides the snapshot's systemDefault block."""
    by_rt = codex.get("activeAccountIdsByRuntime")
    active = by_rt.get("host") if isinstance(by_rt, dict) and "host" in by_rt \
        else codex.get("activeAccountId")
    if active is None:
        if codex.get("_helm_source") == "cli":
            # the CLI roster showed no `(active)` row; the system-default
            # prose below would mislead — the CLI simply cannot see that far
            return None, ("orca CLI names no (active) codex account — the "
                          "selection detail needs the daemon RPC")
        sd = codex.get("systemDefault")
        sd = sd if isinstance(sd, dict) else {}
        if not sd.get("hasAuth") or sd.get("authKind") != "oauth":
            return None, ("orca selects its system-default slot (~/.codex) "
                          "and resolves it %r — no oauth identity to sync"
                          % (sd.get("authKind") or "unresolved"))
        return {"id": None, "email": sd.get("email"),
                "account_id": sd.get("providerAccountId")}, None
    row = next((a for a in codex.get("accounts") or []
                if isinstance(a, dict) and a.get("id") == active), None)
    if row is None:
        return None, ("orca's active codex account %r is missing from its "
                      "own roster — refusing to guess an identity" % active)
    return {"id": active, "email": row.get("email"),
            "account_id": row.get("providerAccountId"),
            "runtime": row.get("managedHomeRuntime")}, None


def _orca_managed_auth(sel):
    """(auth_path, None) for the LIVE per-account credential orca itself
    maintains, else (None, why-not). Convention read from orca's source:
    codex-accounts/service.ts mints managedHomePath = userData/codex-accounts/
    <accountId>/home and writes the `.orca-managed-home` ownership marker with
    the account id; runtime-home-service.ts keeps that file the canonical
    store (auth read-backs on every rate-limit poll, or codex refreshing it
    in place on the self-contained lane). accounts.list summaries carry the
    id but NOT the path (shared/types.ts CodexManagedAccountSummary), so the
    path is DERIVED and then PROVEN: dir exists, marker is a regular file
    naming this id, auth.json parses, and its identity matches the selection
    (account_id first, email fallback) — orca's own ownership assert,
    mirrored read-only. Any failed proof = fall back to ~/.codex-homes, which
    can at worst pool a stale copy of the RIGHT account, never a wrong one."""
    aid = sel.get("id")
    if not aid:
        return None, "orca selects its system-default slot — no managed home"
    if sel.get("runtime") == "wsl":
        return None, ("orca manages this account inside WSL — no host-side "
                      "managed home")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", aid):
        return None, "orca account id %r is not a safe path segment" % aid
    root = os.path.join(_orca_adapter()._user_data_path(), "codex-accounts")
    home_dir = os.path.join(root, aid, "home")
    if not os.path.isdir(home_dir):
        return None, "no orca-managed home under %s" % os.path.join(root, aid)
    marker = os.path.join(home_dir, ".orca-managed-home")
    if not os.path.isfile(marker) or os.path.islink(marker):
        return None, ("%s lacks the .orca-managed-home ownership marker"
                      % home_dir)
    try:
        with open(marker) as f:
            owner = f.read().strip()
    except OSError as e:
        return None, "ownership marker unreadable: %s" % e
    if owner != aid:
        return None, ("ownership marker in %s names account %r, not %r — "
                      "not orca's dir for this account" % (home_dir, owner, aid))
    src = os.path.join(home_dir, "auth.json")
    auth = _read_json(src)
    if not isinstance(auth, dict) or not isinstance(auth.get("tokens"), dict):
        return None, "%s has no parseable codex auth.json" % home_dir
    email, _plan, account_id, _exp = _identity(auth)
    want_acct, want_email = sel.get("account_id"), (sel.get("email") or "")
    if want_acct and account_id:
        if account_id != want_acct:
            return None, ("%s carries account %s, orca selects %s — identity "
                          "mismatch, not trusting the managed copy"
                          % (src, _short(account_id), _short(want_acct)))
    elif not (want_email and email
              and email.casefold() == want_email.casefold()):
        return None, ("cannot confirm %s carries the selected identity "
                      "(no account id or email agreement)" % src)
    return src, None


def _pooled_for(sel, census):
    """The pool filename currently carrying orca's selected identity —
    account_id first, else email — out of an ALREADY-READ census. The
    unchanged-tick check for the managed source, where no codexhome row
    exists to carry the pooled linkage.

    A Team workspace id is shared by its members (`_member_key`). So among
    the files carrying it, only the selected MEMBER's file answers
    (`_serves_member`). This fell back to the first file on the workspace,
    so a sibling's file answered "already pooled" (task/2981)."""
    acct = sel.get("account_id")
    email = sel.get("email")
    if acct:
        hits = [(fname, rec) for fname, rec in census.records
                if isinstance(rec, dict) and rec.get("type") == "codex"
                and rec.get("account_id") == acct]
        return next((fname for fname, rec in hits if _serves_member(
            _member_row(rec), None, email, len(hits) > 1)), None)
    return next((fname for fname, rec in census.records
                 if isinstance(rec, dict) and rec.get("type") == "codex"
                 and _same_address(rec.get("email"), email)), None)


def _orca_roster(codex, sel):
    """Print-safe rows of every codex account orca holds (managed + a
    system-default pseudo-row), the selected one flagged — the refusal's
    orca-side column. Emails/account ids only, never token material."""
    rows = [{"email": a.get("email"), "account_id": a.get("providerAccountId"),
             "selected": sel is not None and a.get("id") is not None
             and a.get("id") == sel.get("id")}
            for a in codex.get("accounts") or [] if isinstance(a, dict)]
    sd = codex.get("systemDefault")
    if isinstance(sd, dict) and sd.get("hasAuth"):
        rows.append({"email": sd.get("email"),
                     "account_id": sd.get("providerAccountId"),
                     "selected": sel is not None and sel.get("id") is None,
                     "system_default": True})
    return rows


def _sync_match(sel, rows):
    """The codexhome rows carrying orca's selected identity — account_id
    exact first, else case-folded email. Zero or many = the caller refuses;
    this never guesses.

    AN ACCOUNT HIT IS NOT UNIQUE (task/2981). `codex_list` folds dirs by
    MEMBER, so three members of one Team workspace are three rows carrying
    one account id. Among them only the selected member's row answers
    (`_serves_member`). The account id alone returned all three (a refusal
    to guess), or the one sibling present, whose credential the sync then
    pooled as the selection."""
    acct = sel.get("account_id")
    if acct:
        hits = [r for r in rows if r.get("account_id") == acct]
        if hits:
            return [r for r in hits if _serves_member(
                r, None, sel.get("email"), len(hits) > 1)]
    return [r for r in rows if _same_address(r.get("email"), sel.get("email"))]


def codex_sync_orca(prev_key=None, force_managed=False):
    """The ONE-WAY leg orca -> pool: read orca's selected codex account and
    pool its LIVE credential through the SAME atomic-0600/hot-reload/
    stale-exp-WARN-never-refuse path. SNAPSHOT ROUTE: accounts.list RPC
    first, the `orca account list` roster on RPC failure
    (_orca_codex_state); rc=2 only when BOTH die, naming both failures and
    the socket tried. SOURCE PRECEDENCE: orca's own managed per-account home
    first (_orca_managed_auth — codex rotates refresh tokens, so orca's copy
    refreshing FIRST burns our ~/.codex-homes copy), the matching codexhome
    as the fallback — BUT that premise only holds while orca is ACTIVELY
    using the account: an idle managed copy can be arbitrarily stale, so
    overwriting an EXISTING pool file from the managed store passes the
    freshness rung (_pool_auth refuse_stale_src — mtime of the managed
    auth.json vs the pool file; strictly-older managed bytes REFUSE, naming
    both timestamps; measured 2026-08-04: a July-26 managed copy overwrote
    an 18:17Z login grant, 11-minute family outage). force_managed=True (or
    HELM_CODEX_FORCE_MANAGED=1) is the deliberate override. A CLI snapshot
    carries no account ids, so its legs narrow honestly: the managed store
    is never consulted, an already-pooled selection answers unchanged (the
    pool copy may BE the managed live bytes — never overwritten with a
    possibly-staler codexhome copy), and only a pool-absent identity pools
    from its codexhome. A refusal never writes and only fires when NO
    reachable source holds the selection (both rosters + the managed-store
    probe attached) or when the freshness rung blocks a regression. rc
    rides the dict: 2 = daemon unreachable (socket named), 1 = refusal,
    else the pool result + {selected, home, key, source, snapshot}.
    prev_key = the last synced key — EMAIL-casefold first (account_id only
    when orca carries no email) so the key survives an RPC->CLI flap mid
    --watch: an unchanged, still-pooled selection answers
    {"unchanged": True} WITHOUT rewriting (the anti-churn seam; refusal
    dicts carry the key too, so a watch tick after a freshness refusal goes
    unchanged-silent instead of re-printing it)."""
    codex, err = _orca_codex_state()
    if err:
        return {"rc": 2, "error": "orca daemon unreachable (%s) — tried %s"
                                  % (err, _orca_socket_tried())}
    snap = "cli" if codex.get("_helm_source") == "cli" else "rpc"
    sel, serr = _orca_selected(codex)
    if serr:
        return {"rc": 1, "error": serr, "orca": _orca_roster(codex, None)}
    # THE POOL IS READ ONCE PER SYNC, AND AN UNREAD ONE IS A REFUSAL, NOT A
    # CRASH (task/2480 R5). Every leg below asks the pool something — is this
    # identity already pooled (the anti-churn unchanged tick), and does this
    # account already sit under another filename (the dupe scan inside
    # `_pool_auth`) — and a raise here killed `--watch` outright on a
    # transient EIO, ending the watcher that exists to survive exactly that.
    # A refusal rides the ordinary rc-1 path instead: printed, and ridden out
    # by the watch loop, which polls again next tick.
    census = read_pool()
    key = (sel.get("email") or "").casefold() or sel.get("account_id")
    if census.unknown:
        return {"rc": 1, "error": "%s — not syncing orca's selection into a "
                                  "pool this pass could not read" % census.error,
                "selected": sel, "key": key, "snapshot": snap}
    rows = codex_list(census)
    hits = _sync_match(sel, rows)
    if snap == "cli":
        pooled = _pooled_for(sel, census)
        if pooled:
            return {"ok": True, "unchanged": True, "selected": sel,
                    "key": key, "pooled": pooled, "source": "pool",
                    "snapshot": snap,
                    "home": hits[0]["name"] if len(hits) == 1 else None}
        managed, mwhy = None, ("orca CLI snapshot carries no account ids — "
                               "the managed store needs the daemon RPC")
    else:
        managed, mwhy = _orca_managed_auth(sel)
    if managed:
        # orca-managed live bytes win; a codexhome match only lends its NAME
        # so the pooled file keeps its slot (else the homes-prepare slug, so
        # a later `helm homes prepare codex <email>` lands on the same file).
        name = hits[0]["name"] if len(hits) == 1 else \
            pk.slug(sel.get("email") or "orca-" + sel["id"])
        pooled = _pooled_for(sel, census)
        if prev_key is not None and key == prev_key and pooled:
            return {"ok": True, "unchanged": True, "selected": sel, "key": key,
                    "home": name, "pooled": pooled, "source": "orca-managed",
                    "snapshot": snap}
        force = force_managed or \
            (home.env("CODEX_FORCE_MANAGED") or "").strip().lower() in \
            ("1", "true", "yes", "on")
        res = _pool_auth(managed, name,
                         "orca refreshes its managed copy in use — select/use "
                         "the account in orca once and re-run sync-orca (the "
                         "proxy's own refresh can 401 on a stale copy)",
                         refuse_stale_src=not force)
        if "error" in res:
            return {"rc": 1, "error": res["error"], "selected": sel,
                    "key": key, "snapshot": snap}
        res.update(selected=sel, home=name, key=key, source="orca-managed",
                   snapshot=snap)
        return res
    if len(hits) != 1:
        why = ("no codexhome carries that identity" if not hits else
               "%d codexhomes carry it (%s) — refusing to guess"
               % (len(hits), ", ".join(h["name"] for h in hits)))
        return {"rc": 1, "orca": _orca_roster(codex, sel), "selected": sel,
                "homes": [{"name": r["name"], "email": r["email"],
                           "account_id": r["account_id"]} for r in rows],
                "orca_managed": mwhy, "snapshot": snap,
                "error": "orca selects %s (account %s) but %s"
                         % (sel.get("email") or "?",
                            _short(sel.get("account_id")), why)}
    row = hits[0]
    if prev_key is not None and key == prev_key and row["pooled"]:
        return {"ok": True, "unchanged": True, "selected": sel, "key": key,
                "home": row["name"], "pooled": row["pooled"],
                "source": "codex-homes", "snapshot": snap}
    res = codex_pool(row["name"])
    if "error" in res:
        return {"rc": 1, "error": res["error"], "selected": sel}
    res.update(selected=sel, home=row["name"], key=key, source="codex-homes",
               snapshot=snap)
    return res


def _print_sync(res):
    """One sync verdict -> printed lines + rc. The refusal prints BOTH
    rosters — the operator's next move (mint the missing home, or flip
    orca's selection) needs the two lists side by side."""
    if res.get("rc") == 2:
        print("helm codex: sync-orca — %s" % res["error"], file=sys.stderr)
        return 2
    if "error" in res:
        print("helm codex: sync-orca REFUSE — %s" % res["error"],
              file=sys.stderr)
        for r in res.get("orca") or []:
            print("  orca%s %-8s %-32s %s" % (
                "*" if r.get("selected") else " ",
                "sysdflt" if r.get("system_default") else "managed",
                r.get("email") or "-", _short(r.get("account_id"))),
                file=sys.stderr)
        homes = res.get("homes")
        if homes is not None:
            if not homes:
                print("  codexhomes: NONE under %s" % homes_root(),
                      file=sys.stderr)
            for r in homes:
                print("  home  %-26s %-32s %s" % (
                    r["name"], r["email"] or "-", _short(r["account_id"])),
                    file=sys.stderr)
            if res.get("orca_managed"):
                # the shopping list names BOTH locations: the codexhomes
                # roster above AND why orca's managed store yielded nothing
                print("  orca-managed store: %s" % res["orca_managed"],
                      file=sys.stderr)
            print("  fix: `helm homes prepare codex <email>` + the human-only"
                  " `CODEX_HOME=~/.codex-homes/<name> codex login "
                  "--device-auth`, or select a listed account in orca",
                  file=sys.stderr)
        return 1
    if res.get("unchanged"):
        print("helm codex: sync-orca — selection unchanged (%s), pool "
              "untouched (%s)%s" % (res["selected"].get("email") or "-",
                                    res["pooled"],
                                    " [orca CLI roster — daemon RPC "
                                    "unavailable]"
                                    if res.get("snapshot") == "cli" else ""))
        if res.get("warn"):
            print("  WARN: " + res["warn"])
        return 0
    print("helm codex: sync-orca %s orca-selected %s -> %s (%s, account %s)"
          % ("refreshed" if res["updated"] else "pooled",
             res["email"] or res["home"], res["path"], res["tier"],
             _short(res["account_id"])))
    if res.get("source"):
        print("  source: %s" % (
            "orca-managed home (the live bytes — codex rotates refresh "
            "tokens, orca's copy is canonical)"
            if res["source"] == "orca-managed"
            else "codexhome %s (%s)"
            % (res["home"],
               "orca CLI roster — daemon RPC down, managed store not "
               "consulted" if res.get("snapshot") == "cli"
               else "no orca-managed home for the selection")))
    print("  " + res["note"])
    if res.get("retired"):
        print("  retired the other spelling(s) of this credential: %s — one "
              "pool file per credential" % ", ".join(res["retired"]))
    if res.get("warn"):
        print("  WARN: " + res["warn"])
    return 0


def _run_sync_orca(watch=False, force_managed=False):
    """One-shot: sync once, exit honest. --watch: poll accounts.list every
    SYNC_POLL_S (accounts.subscribe is a STREAMING method; the shared client
    reads exactly one reply per call, so polling is the deliberate framing).
    A dead daemon at START exits 2 (both snapshot routes down — the error
    names each) — a watcher aimed at nothing says so now; once watching,
    transient errors and refusals are reported and ridden out (a selection
    flip can cure either), and unchanged ticks stay silent. A poll that
    degrades to the CLI roster still answers unchanged for a still-pooled
    selection (email-keyed, source-independent), so a flapping daemon never
    spams the log or churns the pool."""
    res = codex_sync_orca(force_managed=force_managed)
    rc = _print_sync(res)
    if not watch or rc == 2:
        return rc
    key = res.get("key")
    while True:
        time.sleep(_sync_poll_s())
        res = codex_sync_orca(prev_key=key, force_managed=force_managed)
        if not res.get("unchanged"):
            _print_sync(res)
        key = res.get("key", key)


# ----------------------------------------------- orca ACTIVE-ACCOUNT follow

# THE OWNER ASKED WHY THE SWITCH IS MANUAL, verbatim: "is
# there a way to make it so that the proxy automatically updates to using
# whichever cred is active an orca? it seems weird that you manually have to
# switch it even after I have manually switched in orca too".
#
# `sync-orca` above already answers that question — through the DAEMON, which
# is exactly why nothing ever ran it unattended: accounts.list carries a
# provider refresh on EVERY call (measured 15.5-16.4s live) and the fallback
# leg spawns the `orca` binary, so no periodic pass could afford it and the
# owner was left copying a file by hand. Orca ALSO writes the answer to a
# FILE at the moment of the switch, and that file needs no daemon:
#
#   <userData>/codex-runtime-home/shared-runtime-auth-provenance.json
#       {"owner": "managed", "accountId": "<uuid>"}   rewritten on each switch
#   <userData>/codex-accounts/<accountId>/home/auth.json
#       that account's codex-CLI-native auth.json
#
# Two JSON reads is cheap enough to ride the proxywatch pass and `helm seat
# doctor --ensure`, so the pool learns the switch within one pass instead of
# within one human's spare moment.
#
# IT IMPORTS ONCE AND THEN LETS THE PROXY OWN ITS COPY. MEASURED: forty
# seconds after an import, the pool copy's refresh_token AND access_token
# both differed from orca's copy (compared by sha256) — the proxy
# refreshes an expired token on sight and the provider ROTATES the refresh
# token on use, so two holders of one refresh token diverge from the first
# refresh onward. Whether orca's older refresh token keeps working after that
# rotation is UNMEASURED. Both facts point the same way: this rung never
# writes into orca's files, and it never re-imports over a pool member that
# already carries the account — an account PRESENT BY ACCOUNT_ID is left
# untouched even when its email or its file name differs (measured on this
# host: orca's active account was pooled as
# `codex-a@example.com-team.json` while its auth.json translates to
# `codex-b@example.com-team.json`, one account under two addresses). Re-importing
# would hand the proxy a refresh token orca has since rotated past, which is
# the one way this rung could take the codex family auth-dark.
#
# It is REPORT-FIRST in every other direction too: a pool member marked
# `disabled` is reported and never flipped (that field is the proxy's own
# kill-switch and an operator's deliberate act), no other pool member is ever
# read for deletion, and an absent/unreadable provenance file, an absent auth
# file, or a provenance `owner` other than "managed" are each a ROW, never a
# refusal and never a write.
#
# AND THE DESTINATION IS RESOLVED BY ACCOUNT ID, NEVER BY NAME. The pool file
# name `codex-<email>-<plan>.json` is injective over (email, plan) and NOT
# over account ids, so the FIRST cut of this rung could take an unattended
# timer pass and replace a DIFFERENT pooled account that happened to share an
# address and a plan — clearing its `disabled` flag on the way, because
# `_pool_auth` wrote `disabled = False` on every pass. A file name is not an
# identity: the account id decides whether this account is already pooled
# (PRESENT), and a name standing in the way of a NEW file belongs to somebody
# else and stops the pass (COLLISION). See `_follow_pool_name` for the naming
# rule and why no account-id suffix is ever appended.

#: The provenance file, relative to orca's userData root.
FOLLOW_PROVENANCE = ("codex-runtime-home",
                     "shared-runtime-auth-provenance.json")

#: The `owner` value that means "orca is driving a managed per-account home".
#: Any other owner is orca telling us it is NOT the authority for this slot.
FOLLOW_OWNER = "managed"


def _orca_user_data():
    """orca's userData root. ORCA_USER_DATA_PATH first — the ONE seam the
    suite and operators already pin — else the platform default. Read off the
    adapter's STATICMETHOD: this leg constructs no daemon client, opens no
    socket, and spawns nothing."""
    from .harness import OrcaAdapter
    return OrcaAdapter._user_data_path()


def follow_provenance_path():
    """Where orca records the codex account it has active."""
    return os.path.join(_orca_user_data(), *FOLLOW_PROVENANCE)


def _managed_auth_path(account):
    """The codex auth.json of one orca-managed account. READ-ONLY, forever."""
    return os.path.join(_orca_user_data(), "codex-accounts", account, "home",
                        "auth.json")


def _pool_member_for_account(acct, user_id=None, email=None):
    """(filename, record, unproven) for the pool member carrying this
    CREDENTIAL, proven by `_serves_member` (account id, then the user id or
    the address), else (None, None, unproven). `unproven` lists the files on
    this account id that can be proven neither this member nor another.

    A lookup that answers only a filename loses the record and with it the
    `disabled` flag — this rung must REPORT a disabled member rather than
    import over it, so it needs the record and not just the name. A lookup
    keyed on the account id alone also folds three Team members into one
    workspace id, which is how admin@ read PRESENT while only d@
    was pooled. It went on matching a missing user id on the account id
    alone (task/2981), so a member whose credential carries no user-id claim
    still read PRESENT on a sibling's file. An unproven file is UNKNOWN to
    the caller: neither PRESENT nor free to import beside."""
    if not acct:
        return None, None, []
    same = [(fname, rec) for fname, key, rec in _pooled_members(read_pool())
            if key[0] == acct]
    unproven = []
    for fname, rec in same:
        verdict = _serves_member(_member_row(rec), user_id, email,
                                 len(same) > 1)
        if verdict:
            return fname, rec, []
        if verdict is None:
            unproven.append(fname)
    return None, None, unproven


def _follow_pool_name(email, plan):
    """THE POOL NAMING RULE for this rung, in one place.

    A follow-minted pool file is `codex-<email>-<plan>.json` — the name
    `helm.seat_credentials.translate_codex_auth` derives — and that name is
    injective over (email, plan) and NOT over ACCOUNT IDS: one owner can hold
    two codex accounts reachable at one address on one plan, and both want
    this one file. The rule this lane commits to, out of the two the ruling
    offered, is THE PLAIN NAME OR NOTHING:

      * free name, or a name this very account already holds -> plain name;
      * name held by ANY other account -> the follow REFUSES (`cred_follow`'s
        COLLISION row) and writes nothing.

    No account-id fragment is ever appended. A suffixed sibling would put two
    files for two accounts in the proxy's hot-reload dir on an unattended
    timer pass, with no operator having asked for a second pooled credential;
    the operator door that DOES mint a second file deliberately already
    exists and names it after the home (`helm codex pool <home>`). Existing
    plain-named files are read and never renamed by this rung.

    Both fields must be KNOWN. `translate_codex_auth` spells an absent field
    `unknown`, and `codex-unknown-unknown.json` is the one name EVERY
    incomplete credential in the world collides on — so `cred_follow` refuses
    (INCOMPLETE) rather than mint a name with a guessed field in it. The
    fields come from `_identity`, this module's own resolution (access-token
    claim first for the plan, id-token first for the email), which is strictly
    stronger than the id-token-only reading inside `translate_codex_auth`: a
    credential carrying its plan only in the access token gets its real plan
    in the name here instead of the word `unknown`."""
    return "codex-%s-%s.json" % (email, plan)


def _pool_name_holder(name):
    """(exists, account_id) for the pool file stored under this EXACT name.

    account_id is None both when the name is free and when the file there
    cannot be read as a codex record carrying one — `exists` is what
    separates those, and a caller deciding whether it may write must branch
    on `exists`, because an unreadable member is still somebody's credential."""
    path = os.path.join(pool_dir(), name)
    if not os.path.exists(path):
        return False, None
    rec = _read_json(path)
    if not (isinstance(rec, dict) and rec.get("type") == "codex"):
        return True, None
    return True, rec.get("account_id") or None


def _follow_row(**kw):
    row = {"state": None, "orca_account": None, "account_id": None,
           "email": None, "tier": None, "pooled": None, "detail": None,
           "active": False}
    row.update(kw)
    return row


def cred_follow(apply=False):
    """The one rung: make the proxy pool carry EVERY codex account orca holds
    on this host — one pool file per credential, the ACTIVE one flagged —
    without a daemon and without touching orca's files.

    EVERY ACCOUNT, NOT ONLY THE ACTIVE ONE. A rung that imports only the
    one account orca's provenance file names leaves the pool as whatever
    hand-pooling put beside it: admin@ never reached the proxy at all,
    because a census keyed on the workspace id reads d@ as admin@. The proxy's whole value is the fall-through across
    credentials, so the pool follows orca's ROSTER (`_orca_managed_roster`),
    and the provenance file only says which member is active.

    Dry-run by default — `apply=True` is the only path that writes, and it
    writes through `_pool_auth`, one of this module's locked pool doors
    (atomic 0600 sibling + os.replace, so the proxy's hot-reload watcher can
    never read a half-made credential; `helm seat add codex` writes through
    the sibling door `pool_provision` under the same lock). States, one row
    per orca-held account (`active` True on the one orca has selected):

      PRESENT      the pool already carries this credential — untouched, by
                   account id AND user id (`_member_key`), whatever the
                   member's email or file name
      IMPORTED     it did not, and this apply pass wrote it
      MISSING      it does not, and this was a dry run (`--apply` cures it)
      DISABLED     a pool member carries the account with disabled=true —
                   reported, NEVER flipped; the proxy cannot draw on it
      CHANGED      the destination moved between this pass's unlocked proof
                   and its LOCKED re-read immediately before the write —
                   another writer pooled the account, created the file, or an
                   operator parked it — so the proof no longer authorizes
                   anything and nothing is written
      COLLISION    the file name this rung would mint is already held by a
                   DIFFERENT account — refused, loudly, naming both accounts
                   and the standing file; nothing is written (the name is
                   injective over email-and-plan, never over account ids)
      INCOMPLETE   the credential names no email or no plan, so the file name
                   could only be minted with the word `unknown` in it —
                   refused, naming the missing field
      MISSING-AUTH provenance names an account with no readable auth.json
      UNREADABLE   the auth.json is there and does not translate
      SKIPPED      provenance `owner` is not "managed" — orca is telling us
                   it does not drive this slot
      UNKNOWN      no provenance file, or one that does not parse: helm
                   cannot say which account is active, which is never the
                   same claim as "none is"
      ERROR        the import itself failed (the pool write's own words)

    UNKNOWN and SKIPPED are ROWS only when orca holds no managed account on
    disk; with a roster to pool they become the pass's `active_note` and no
    row is flagged active — the roster is pooled either way.

    Returns {"apply", "provenance", "pool", "rows": [row], "imported": n,
    "active": orca account id or None, "active_note": why none is flagged}.
    No token material is in any field; `detail` carries paths and identities
    only."""
    prov_path = follow_provenance_path()
    out = {"apply": bool(apply), "provenance": prov_path, "pool": pool_dir(),
           "imported": 0, "rows": [], "active": None, "active_note": None}
    active, prov_row = _follow_active(prov_path)
    roster = _orca_managed_roster()
    if prov_row is not None:
        out["active_note"] = prov_row["detail"]
        if not roster:
            out["rows"].append(prov_row)
            return out
    out["active"] = active
    if active is not None and active not in dict(roster):
        src = _managed_auth_path(active)
        out["rows"].append(_follow_row(
            state="MISSING-AUTH", orca_account=active, active=True,
            detail="orca has account %s active but keeps no regular auth.json "
                   "at %s — nothing to import, and the pool is untouched"
                   % (_short(active), src)))
    # THE ACTIVE ACCOUNT FIRST, then the rest of orca's roster by id.
    ordered = sorted(roster, key=lambda r: (r[0] != active, r[0]))
    for account, src in ordered:
        out["rows"].append(_follow_one(out, account, src, apply,
                                       account == active))
    return out


def _orca_managed_roster():
    """[(orca account id, auth.json path)] for EVERY codex account orca holds
    on this host — each `codex-accounts/<id>/home` whose ownership marker
    names its own id and whose auth.json is a regular file (the proof
    `_orca_managed_auth` applies to the selection, applied to the roster).
    Sorted by id; [] when orca keeps no managed accounts here. Read-only."""
    root = os.path.join(_orca_user_data(), "codex-accounts")
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return []
    out = []
    for aid in names:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", aid):
            continue
        home_dir = os.path.join(root, aid, "home")
        marker = os.path.join(home_dir, ".orca-managed-home")
        src = os.path.join(home_dir, "auth.json")
        if not os.path.isfile(marker) or os.path.islink(marker):
            continue
        try:
            with open(marker) as f:
                owner = f.read().strip()
        except OSError:
            continue
        if owner != aid or not os.path.isfile(src) or os.path.islink(src):
            continue
        out.append((aid, src))
    return out


def _follow_active(prov_path):
    """(active orca account id or None, refusal row or None) from orca's
    provenance file. The row carries the old single-slot verdicts — UNKNOWN
    (no file, or one that does not parse, or an unsafe id) and SKIPPED
    (owner is not managed) — and is what the rung answers when orca holds
    NO managed accounts at all; with a roster on disk it becomes the pass's
    `active_note` and no account is flagged active."""
    if not os.path.exists(prov_path):
        return None, _follow_row(
            state="UNKNOWN",
            detail="orca records no active codex account at %s — either orca "
                   "is not installed here or it has never selected a managed "
                   "account; helm cannot say which account is active, which "
                   "is not the same as saying none is" % prov_path)
    prov = _read_json(prov_path)
    if not isinstance(prov, dict):
        return None, _follow_row(
            state="UNKNOWN",
            detail="%s does not parse as a JSON object — the active account "
                   "is UNKNOWN, and this rung will not guess one" % prov_path)
    owner = prov.get("owner")
    account = prov.get("accountId")
    if owner != FOLLOW_OWNER:
        return None, _follow_row(
            state="SKIPPED", orca_account=account if isinstance(account, str)
            else None,
            detail="orca's provenance names owner %r, not %r — orca is not "
                   "driving a managed per-account home for this slot, so "
                   "no account is flagged active"
                   % (owner, FOLLOW_OWNER))
    if not (isinstance(account, str)
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", account)):
        return None, _follow_row(
            state="UNKNOWN",
            detail="%s names accountId %r, which is not a safe path segment "
                   "— refusing to build a credential path from it"
                   % (prov_path, account))
    return account, None


def _follow_one(out, account, src, apply, active):
    """One orca-managed account's row (the states in `cred_follow`), the
    write — when `apply` and nothing carries the credential — through the
    locked re-proof. Bumps out["imported"] on IMPORTED."""
    # the translator's own filename is DELIBERATELY dropped: it reads email
    # and plan from the id_token only and spells either `unknown` when absent.
    # This rung names the destination itself, through `_follow_pool_name`.
    rec, _fname, terr = _seat.translate_codex_auth(src)
    if terr or not isinstance(rec, dict):
        return _follow_row(
            state="UNREADABLE", orca_account=account, active=active,
            detail="orca's credential for account %s does not translate: %s"
                   % (_short(account), terr or "no record produced"))
    auth = _read_json(src)
    email, plan, acct, _exp = _identity(auth)
    acct = acct or rec.get("account_id")
    if not acct:
        return _follow_row(
            state="UNREADABLE", orca_account=account, email=email,
            active=active,
            detail="orca's credential for account %s carries no provider "
                   "account id, and identity by account id is what makes this "
                   "rung idempotent — refusing to import on email alone"
                   % _short(account))
    user_id = _user_id(auth)
    base = {"orca_account": account, "account_id": acct,
            "email": email or rec.get("email"), "active": active,
            "tier": tier(plan) if plan else "?"}
    pooled, member, unproven = _pool_member_for_account(
        acct, user_id, email or rec.get("email"))
    if unproven and not pooled:
        return _follow_row(
            state="UNKNOWN",
            detail="the pool holds %s on account %s, and no file is proven "
                   "to be this member or another (no user id and no address "
                   "to compare). On a Team plan that id is the WORKSPACE, so "
                   "this rung neither calls the account PRESENT nor imports "
                   "a second copy beside it" % (", ".join(unproven),
                                                _short(acct)), **base)
    if pooled and member.get("disabled"):
        return _follow_row(
            state="DISABLED", pooled=pooled,
            detail="the pool carries orca's account as %s with "
                   "disabled=true — that field is the proxy's kill-switch and "
                   "somebody set it deliberately, so this rung reports it and "
                   "never flips it; `helm codex pooled` shows the pool"
                   % pooled, **base)
    if pooled:
        return _follow_row(
            state="PRESENT", pooled=pooled,
            detail="the pool already carries account %s as %s — untouched. "
                   "The proxy rotates its own refresh token away from orca's "
                   "copy within a minute of the first refresh, so re-importing "
                   "would replace a live credential with a stale one"
                   % (_short(acct), pooled), **base)
    # ---- NO UNKNOWN FIELDS IN A NAME -----------------------------------
    # Reached only when NO pool member carries this credential, so from here
    # the rung is about to NAME a new file. `translate_codex_auth` would spell
    # an absent email or plan `unknown`; that is a guess wearing a filename,
    # and every incomplete credential guesses the same one.
    unknown = [n for n, v in (("email", email), ("plan", plan)) if not v]
    if unknown:
        return _follow_row(
            state="INCOMPLETE", pooled=None,
            detail="orca's credential for account %s carries no %s claim, and "
                   "the pool file name is built from email and plan — helm "
                   "will not spell a field it does not know `unknown` and "
                   "write a credential under that name, because every "
                   "incomplete credential lands on the same one. Nothing was "
                   "written; log the account in again in orca so the token "
                   "carries the claim" % (_short(acct), " and ".join(unknown)),
            **base)
    # ---- IDENTITY BEFORE WRITE -----------------------------------------
    # The destination is resolved by CREDENTIAL first (that is the PRESENT
    # branch above, which ends the pass). Getting here means no pooled file
    # holds this credential, so ANY file already standing under the name this
    # rung would mint belongs to somebody else — the name is injective over
    # (email, plan), never over account ids. Replacing it would destroy
    # another account's credential and, through `_pool_auth`'s old
    # `disabled = False`, arm one an operator had deliberately parked.
    name = _follow_pool_name(email, plan)
    taken, holder = _pool_name_holder(name)
    if taken:
        return _follow_row(
            state="COLLISION", pooled=name,
            detail="the pool file %s already holds account %s, and orca's "
                   "account is %s — one email and one plan, two "
                   "accounts, one file name. Refusing to write %s over "
                   "another account's credential (the source stays at %s, "
                   "untouched). Pool that account deliberately under a name "
                   "of its own with `helm codex pool <home>`, or retire the "
                   "standing file with `helm codex unpool`"
                   % (os.path.join(pool_dir(), name),
                      _short(holder) if holder
                      else "an account id helm cannot read",
                      _short(acct), name, src),
            **base)
    if not apply:
        return _follow_row(
            state="MISSING", pooled=None,
            detail="no pool member carries orca's account %s — the "
                   "proxy cannot draw on a credential the owner holds; "
                   "`helm seat cred-follow --apply` imports it as %s"
                   % (_short(acct), name), **base)
    # ---- THE LOCKED RE-PROOF -------------------------------------------
    # Everything above is a PLAN read outside any boundary: it proved no pool
    # member carries this credential and no file stands under the name.
    # Between that proof and the write, another writer — the other unattended
    # follow path, an operator's `helm codex pool`, `helm seat add` — can
    # create exactly the file this pass is about to replace, and an operator
    # can park the account with the proxy's kill-switch. A proof taken outside
    # the boundary can never authorize a write, so the
    # write runs under `pool_lock` and the SAME two questions are asked again
    # inside it, immediately before `_pool_auth` touches anything. Anything
    # that moved is a REFUSAL naming what was found, never a retry: helm does
    # not know whether the other writer's credential is better than orca's,
    # and overwriting to find out is the class of bug this rung exists to
    # stop.
    with pool_lock():
        re_pooled, re_member, re_unproven = _pool_member_for_account(
            acct, user_id, email or rec.get("email"))
        re_taken, re_holder = _pool_name_holder(name)
        moved = None
        if re_unproven and not re_pooled:
            moved = ("the pool now holds %s on account %s, which no proof "
                     "names this member or another — written after this "
                     "pass read the pool" % (", ".join(re_unproven),
                                             _short(acct)))
        elif re_pooled and (re_member or {}).get("disabled"):
            moved = ("the pool now carries account %s as %s with "
                     "disabled=true — the proxy's kill-switch was set after "
                     "this pass read the pool, and nothing in helm clears "
                     "that field" % (_short(acct), re_pooled))
        elif re_pooled:
            moved = ("the pool now carries account %s as %s — another writer "
                     "imported it after this pass read the pool free, and its "
                     "copy is the one the proxy is refreshing"
                     % (_short(acct), re_pooled))
        elif re_taken:
            moved = ("the pool file %s was created after this pass proved "
                     "that name free; it holds %s and orca's account "
                     "is %s"
                     % (name, _short(re_holder) if re_holder
                        else "an account id helm cannot read", _short(acct)))
        if moved:
            return _follow_row(
                state="CHANGED", pooled=re_pooled or (name if re_taken
                                                      else None),
                detail="the destination changed between this pass's proof and "
                       "its locked write: %s. Nothing was written and orca's "
                       "file (%s) is untouched; re-run `helm seat cred-follow` "
                       "to read the pool as it now stands" % (moved, src),
                **base)
        canonical = name[len("codex-"):-len(".json")]
        res = _pool_auth(src, canonical,
                         "orca refreshes its managed copy while it is using "
                         "the account — use the account in orca once and "
                         "re-run `helm seat cred-follow --apply`")
    if "error" in res:
        return _follow_row(state="ERROR", detail=res["error"], **base)
    out["imported"] += 1
    return _follow_row(
        state="IMPORTED", pooled=res["pooled"],
        detail="imported orca's account %s into the pool as %s (0600); "
               "%s. From here the proxy owns this copy and later passes leave "
               "it alone" % (_short(acct), res["pooled"], res["note"]), **base)

#: The states that mean the pool does NOT follow orca right now.
FOLLOW_FAULTS = ("MISSING", "DISABLED", "MISSING-AUTH", "UNREADABLE", "ERROR",
                 "COLLISION", "INCOMPLETE", "CHANGED")


def cred_follow_lines(res):
    """The ONE rendering of a cred_follow result — shared verbatim by the
    verb, the proxywatch pass and `helm seat doctor --ensure`, so the three
    doors cannot drift into three vocabularies for one fact."""
    lines = ["cred-follow  every orca-held codex account -> %s%s"
             % (res["pool"], "" if res["apply"] else "  [dry run]")]
    if res.get("active_note") and res["rows"] \
            and res["rows"][0]["state"] not in ("UNKNOWN", "SKIPPED"):
        lines.append("    no account flagged active: %s" % res["active_note"])
    for row in res["rows"]:
        lines.append("  %-12s %-34s %s%s"
                     % (row["state"], row.get("email") or
                        _short(row.get("orca_account")),
                        row.get("pooled") or "-",
                        "  (active)" if row.get("active") else ""))
        lines.append("    " + (row["detail"] or ""))
    return lines


def cred_follow_rc(res):
    """0 when the pool follows orca or there is nothing to follow; 1 when a
    measured gap stands. UNKNOWN and SKIPPED are 0 on purpose: a host with no
    orca, or an orca that says it is not driving this slot, is not a fault,
    and this rung is wired into passes that must not go red on absence."""
    return 1 if any(r["state"] in FOLLOW_FAULTS for r in res["rows"]) else 0


def cred_follow_noteworthy(res):
    """Whether a SUPERVISORY pass should print this verdict.

    A supervisor reports what MOVED and what is WRONG; it does not narrate the
    steady state, and it must not announce on every tick that this host has no
    orca. `helm seat cred-follow` is where that question was actually asked and
    it always answers in full — these passes report an import or a fault and
    are otherwise silent, the same bar `--ensure --quiet` already holds every
    proxy row to. Measured cost of getting this wrong: `seat doctor --ensure`
    on a host with no orca printed a three-line UNKNOWN block on every run."""
    return bool(res["imported"] or cred_follow_rc(res))


def cmd_cred_follow(args):
    """`helm seat cred-follow [--apply] [--json]` — the operator's door.

    IT GUARDS ITS OWN TAIL even though `helm seat` guards this subverb before
    dispatching here. This function reads `--apply` by MEMBERSHIP and then
    WRITES into the proxy pool, which is exactly the shape the tree's
    apply-reader rung refuses (tests.test_dispatch_honest's
    ApplyReadersAreGuarded): a dispatcher's guard protects only the door that
    dispatcher owns, so a second caller routing to this function — or a
    future `helm codex` alias — would membership-read `--apply` off an
    unchecked tail and write on junk.
    The closed set is exactly {--apply, --json}; on the clean tail `helm seat`
    has already proved, guard_tail returns None and costs nothing.
    """
    from .cli import guard_tail
    rc = guard_tail("helm seat cred-follow", args,
                    flags=("--apply", "--json"),
                    usage="seat cred-follow [--apply] [--json]")
    if rc is not None:
        return rc
    res = cred_follow(apply="--apply" in args)
    if "--json" in args:
        print(json.dumps(res, indent=2, sort_keys=True))
    else:
        for line in cred_follow_lines(res):
            print(line)
    return cred_follow_rc(res)



# ---------------------------------------------------------------- CLI leg

def _short(account_id):
    return (account_id[:8] + "…") if account_id and len(account_id) > 9 \
        else (account_id or "-")


def _fail(res):
    print("helm codex: " + res["error"], file=sys.stderr)
    return 1


def _print_list():
    rows = codex_list()
    if not rows:
        print("helm codex: no codexhomes under %s — `helm homes prepare codex "
              "<email>` starts one" % homes_root())
        return 0
    # "%d pooled" is a COUNT, and a count over an unread pool is a
    # measurement nobody made (task/2480 R5) — so it renders "?" there, the
    # same answer the per-row `pooled:?` flag gives.
    n_pooled = ("?" if rows[0].get("pool_unread")
                else str(sum(1 for r in rows if r["pooled"])))
    print("helm codex: %d codexhome%s under %s (%s pooled -> %s)" % (
        len(rows), "s"[:len(rows) != 1], homes_root(), n_pooled, pool_dir()))
    for r in sorted(rows, key=lambda r: r["name"]):
        flags = []
        if not r["authed"]:
            flags.append("no-auth")
        elif not r["email"]:
            flags.append("identity?")
        if r.get("member_unproven"):
            # which member of a workspace this is rests on its address alone
            flags.append("member?")
        flags.append("pooled:" + r["pooled"] if r["pooled"]
                     else ("pooled:?" if r.get("pool_unread") else "-"))
        alias = " [alias: %s]" % ",".join(r["aliases"]) if r["aliases"] else ""
        print("  %-26s %-32s %-6s %-10s %s%s" % (
            r["name"], r["email"] or "-", r["tier"] or "?",
            _short(r["account_id"]), "; ".join(flags), alias))
    return 0


def _print_pooled():
    # AN UNREAD POOL PRINTS UNKNOWN, NOT "pool empty" (task/2480 R5). The
    # reader raised here before, so `helm codex pooled` answered a directory
    # fault with a traceback; swallowing it would be worse still, telling the
    # operator the pool is empty on the strength of an error.
    census = read_pool()
    if census.unknown:
        print("helm codex: pool UNKNOWN — %s" % census.error, file=sys.stderr)
        return 1
    rows = pooled_rows(census)
    if not rows:
        print("helm codex: pool empty (%s) — `helm codex pool <name>` feeds "
              "the proxy" % pool_dir())
        return 0
    print("helm codex: %d pooled cred%s in %s (proxy hot-reloads this dir)" % (
        len(rows), "s"[:len(rows) != 1], pool_dir()))
    for r in rows:
        if r.get("error"):
            print("  %-34s %s" % (r["file"], r["error"]))
            continue
        flags = ["disabled"] if r["disabled"] else []
        if r.get("expired"):
            flags.append("exp " + r["expired"])
        print("  %-34s %-32s %-6s %-10s %s" % (
            r["file"], r["email"] or "-", r["tier"] or r.get("type") or "?",
            _short(r["account_id"]), "; ".join(flags) or "-"))
    return 0


def _print_capacity():
    """The one policy readout: per-pooled-cred email/tier/seats + total fleet
    capacity, plus the codex* seats LIVE on the roster (so over/under is
    visible at a glance)."""
    from . import seats as _s
    cap = capacity()
    if cap.get("unknown"):
        # the same law as every other door: a directory nobody could read is
        # never rendered as a measured capacity of zero.
        print("helm codex: fleet seat capacity UNKNOWN — %s" % cap["error"],
              file=sys.stderr)
        return 1
    print("helm codex: fleet seat capacity %d (what the POOL holds, ultra=%d/"
          "cred via HELM_CODEX_ULTRA_SEATS, team=1)" % (cap["total"], _ultra_seats()))
    for c in cap["creds"]:
        print("  %-32s %-6s %d seat%s" % (
            c["email"] or "-", c["tier"], c["seats"], "s"[:c["seats"] != 1]))
    live = sorted(s for s in _s.roster() if s == "codex" or s.startswith("codex-"))
    if live:
        # the seat KEY is the unvalidated HELM_CHAT_NAME join seam — launder
        # the emitted label so a hostile codex-<ESC/bidi> seat cannot reshape
        # this readout's terminal (matching still rode the raw key above).
        print("  live codex seats: %s" % ", ".join(_s._seat_label(s) for s in live))
    return 0


def cmd_codex(args):
    """codex [list] | pool <name> | unpool <name> | pooled | capacity |
    resets [--dry-run] [--consume <account>] [--json] |
    sync-orca [--watch] [--force-managed] | launch [-i N|--instance N]
    [--force] [--room R] [--model M] — codexhome roster (ultra/team) + proxy cred
    pooling (auth.json -> 0600 pool file). resets = the earned rate-limit
    reset credits, per pooled account, and the door that spends one. launch = the cred-% gate ahead
    of the seat-launch mint; sync-orca = the one-way orca-selection -> pool
    adapter (--force-managed overrides its freshness rung). Every pool write
    here admits identity through `_admit_identity`: a pooled file holding a
    KNOWN account is refreshed only by that account and never replaced by
    another one or by a credential naming no account id."""
    args = list(args)
    verb, rest = (args[0], args[1:]) if args else ("list", [])
    from .cli import guard_tail
    if verb in ("list", "pooled", "capacity"):
        rc = guard_tail("helm codex " + verb, rest, usage="codex " + verb)
        if rc is not None:
            return rc
        return {"list": _print_list, "pooled": _print_pooled,
                "capacity": _print_capacity}[verb]()
    if verb == "launch":
        force = "--force" in rest
        rest = [a for a in rest if a != "--force"]
        # junk refuses BEFORE launch_gate: a failing pool gate (rc 1) used to
        # mask the unknown flag entirely, printing gate diagnostics as if it
        # existed. Mirrors seat's launch tail (the downstream authority);
        # drift refuses loudly here rather than silently passing junk on.
        rc = guard_tail("helm codex launch", rest, flags=("--multi",),
                        valued=("--room", "--model", "-i", "--instance"),
                        usage="codex launch [-i N|--instance N] [--force] "
                              "[--room R] [--model M] [--multi]")
        if rc is not None:
            return rc
        inst = 1
        for flag in ("-i", "--instance"):
            if flag in rest:
                try:
                    inst = int(rest[rest.index(flag) + 1])
                except (ValueError, IndexError):
                    print("helm codex: %s wants an integer" % flag,
                          file=sys.stderr)
                    return 2
        rc = launch_gate(inst=inst, force=force)
        if rc:
            return rc
        return _run_launch(rest)
    if verb == "resets":
        # THE RESET-CREDIT DOOR LIVES BESIDE THE POOL IT SPENDS FROM. The
        # credential a redemption is authorized by is the POOLED copy this
        # module owns, so the verb belongs to this surface; the mechanism —
        # the two vendor calls, the policy and the attempt ledger — lives in
        # helm/codexresets.py rather than growing this module a second domain.
        from . import codexresets
        return codexresets.cmd_resets(rest)
    if verb == "sync-orca":
        watch = "--watch" in rest
        force_managed = "--force-managed" in rest
        rest = [a for a in rest if a not in ("--watch", "--force-managed")]
        rc = guard_tail("helm codex sync-orca", rest,
                        usage="codex sync-orca [--watch] [--force-managed]")
        if rc is not None:
            return rc
        return _run_sync_orca(watch=watch, force_managed=force_managed)
    if verb == "pool":
        if not rest:
            print("usage: helm codex pool <name>", file=sys.stderr)
            return 2
        rc = guard_tail("helm codex pool", rest[1:], usage="codex pool <name>")
        if rc is not None:
            return rc
        res = codex_pool(rest[0])
        if "error" in res:
            return _fail(res)
        print("helm codex: %s %s -> %s (%s, %s)" % (
            "refreshed" if res["updated"] else "pooled",
            res["email"] or rest[0], res["path"], res["tier"],
            "account " + _short(res["account_id"])))
        print("  " + res["note"])
        if res.get("retired"):
            print("  retired the other spelling(s) of this credential: %s — "
                  "one pool file per credential" % ", ".join(res["retired"]))
        if res.get("warn"):
            print("  WARN: " + res["warn"])
        return 0
    if verb == "unpool":
        if not rest:
            print("usage: helm codex unpool <name>", file=sys.stderr)
            return 2
        rc = guard_tail("helm codex unpool", rest[1:],
                        usage="codex unpool <name>")
        if rc is not None:
            return rc
        res = codex_unpool(rest[0])
        if "error" in res:
            return _fail(res)
        print("helm codex: " + (("removed " + ", ".join(res["removed"]))
                                if res["removed"] else res["note"]))
        return 0
    print("helm codex: unknown subverb %r (%s)"
          % (verb, cmd_codex.__doc__.strip().split("\n")[0]), file=sys.stderr)
    return 2
