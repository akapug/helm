#!/usr/bin/env python3
"""helm proxy-usage — which SEAT spent a pooled proxy-family account.

THE QUESTION. A pooled codex account drains and nothing can say which seat
took which share: the pool budget lines read used_percent PER ACCOUNT from
the vendor, the session catalog carries no cred for proxy families, and the
sidecar's proxy.log carries status, latency and byte counts but no tokens.
Seat identity IS the sidecar — every proxy-family seat runs its own
CLIProxyAPI instance — so the sidecar is the meter that can attribute.

THE METER. helm's CLIProxyAPI fork publishes one usage record per upstream
request into an in-memory queue (the fork's `redisqueue` package; there is no
redis, the name is inherited) and serves it at the management route
`GET /v0/management/usage-queue?count=N`, which POPS the oldest N records.
The queue exists only while management routes are registered (the spawn's
MANAGEMENT_PASSWORD, see seat_proxy) and fills only while the generated
config carries `usage-statistics-enabled: true`; records older than the
config's retention window are pruned at the next enqueue or pop.

WHAT A RECORD CARRIES — the producer's JSON spellings, copied from the Go
struct tags (`queuedUsageDetail` embedding `requestDetail`), never renamed:
`timestamp`, `latency_ms`, `ttft_ms`, `source` (the account this request
spent, written as THE CONTRACT below says), `auth_index` (the fork's stable
per-auth hash, the join key to the pool file that spent it; `pool_index` is
the formula), `client_ip`, `x_forwarded_for`, `user_agent`, `tokens`
(legacy buckets), `failed`, `generate`, `fail` {`status_code`, `body`},
`response_headers`, `accounting_version`, `token_breakdown` {`schema_version`,
`quality`, `total_tokens`, `input` {`total_tokens`, `uncached_tokens`,
`cache_read_tokens`, `cache_write_tokens`}, `output` {`total_tokens`,
`non_reasoning_tokens`, `reasoning_tokens`}, `unclassified_tokens`},
`provider`, `executor_type`, `model`, `alias`, `endpoint`, `auth_type`,
`api_key` (the INBOUND seat token — a credential, dropped before the ledger),
`request_id` (minted by the proxy, never echoed from the client),
`reasoning_effort`, `service_tier`, `response_service_tier`.

WHAT IS JOINABLE. Seat (the sidecar) and account (`source`) are exact per
record. A dispatch ROW is not: the proxy records nothing from the request a
client controls except `user_agent`, and the seat's client cannot vary a
header per row, so per-row attribution stops here and is said so, never
approximated.

THE LEDGER. A pop is destructive and the queue dies with the sidecar, so
every popped record is appended at once to `proxy-usage.jsonl` under the
global helm home, one event per line: `kind` "request" carries the curated
record with the sidecar's pid so a restart is visible; `kind` "read" carries
the per-seat outcome of each pass, and UNREADABLE is a status with a reason,
never a zero — a seat whose sidecar cannot be read stays UNATTRIBUTED on
every surface that rolls this ledger up.

THE CONTRACT — stated once, here. Every other sentence in this module, the
verb synopsis and the two docs/VERBS.md sections is narrower than this block
or points at it.
  KEPT VERBATIM: `source`, only when the pool file this record's own
  `auth_index` names — a readable JSON file in the sidecar's declared
  auth-dir, indexed by the producer's formula (`pool_index`) — carries that
  `source` as its `email`. A per-record identity join, not membership; the
  record's shape and `auth_type` decide nothing.
  HASHED: every other `source` — an api key, an OAuth auth's fallback
  credential, a record with no `auth_index`, a record whose file is faulted
  or gone — persists as `cred:` plus the first eight hex of its sha256, so
  one credential's requests still group and its text is not written.
  THE ONE BOUND: the pool is read at pop time and the index is derived from
  the file's path, so a path whose auth file was replaced between enqueue
  and read is joined against the file now at that path, and an older
  fallback credential equal to the new file's email is written verbatim.
  Only the producer stamping the account on the record at enqueue closes
  that window; this reader cannot. "Its text is not written" above holds
  up to this bound and no further.
  READ: the usage records were collected and persisted. Account naming is
  per record under the rules above and does not move the status.
  POOL FAULTS AND UNRESOLVED: the read event's `pool` object and the verb's
  trailer carry `admitted` (pool files joined), `faults` (the auth-dir
  undeclared, absent or unenumerable, or a file by basename that is
  unreadable, not JSON, not an object, without `type` or without `email`;
  a faulted file admits nothing while its readable siblings still admit
  theirs; a fault carries basename and reason, not file content) and
  `unresolved` (records hashed although they carried an `auth_index`).
  The pool read is total and happens before the queue is popped: any
  failure in it is a fault, and a failure of any kind after the pop is a
  FAILED-PERSIST read event carrying the popped count, so no pool read
  loses a popped record.
  DROPPED before the ledger: `api_key` (the inbound seat token),
  `response_headers`, `fail.body` and the client address triple.

Stdlib only; loopback only; a two-second timeout per call; the management
secret is read into a variable, presented in the `X-Management-Key` header
(the fork accepts that or `Authorization: Bearer`), and never printed,
logged, stored or placed in an error.
"""
import errno
import fnmatch
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

from . import eventledger, home

LEDGER = "proxy-usage.jsonl"
V = 1
TIMEOUT_S = 2.0
POP_COUNT = 500
MANAGEMENT_PATH = "/v0/management/usage-queue"

UNREADABLE = "UNREADABLE"
READ = "READ"
# THE POP IS DESTRUCTIVE, SO PERSISTENCE IS PART OF THE READ. A pass whose
# ledger append refused (lock, write, fsync, or the read marker itself) is
# neither READ nor UNREADABLE: the records left the sidecar and did not all
# reach disk. It is its own state, every entrypoint exits on it, and the
# row carries exactly how many records were lost.
FAILED_PERSIST = "FAILED-PERSIST"

# The producer's record keys the ledger KEEPS. Everything else is dropped:
# `api_key` is the inbound seat token, `response_headers` and `fail.body`
# echo upstream bytes nobody rolls up, and the client address triple says
# "loopback" on every row.
_KEEP = ("timestamp", "latency_ms", "source", "auth_index", "failed",
         "generate", "provider", "executor_type", "model", "alias",
         "endpoint", "auth_type", "request_id", "reasoning_effort",
         "service_tier", "token_breakdown")


def ledger_path():
    return os.path.join(home.global_dir(), LEDGER)


def _top_scalar(config_path, key):
    """(value, err) for ONE top-level scalar of the sidecar's own
    config.yaml, through helm's one yaml scalar reader; the secret is never
    in this file. A key declared twice is refused rather than picked from."""
    from .yaml_scalar import yaml_scalar
    try:
        with open(config_path, encoding="utf-8") as f:
            text = f.read()
    except OSError as exc:
        return None, "config unreadable: %s" % exc
    found = re.findall(r"^%s:(.*)$" % re.escape(key), text, re.M)
    if not found:
        return None, "config names no %s" % key
    if len(found) > 1:
        return None, "config names %s %d times" % (key, len(found))
    value, err = yaml_scalar(found[0])
    if err:
        return None, "config %s: %s" % (key, err)
    return value, None


def _port_of(config_path):
    """(port, err) off the sidecar's own config.yaml."""
    value, err = _top_scalar(config_path, "port")
    if err:
        return None, err
    try:
        return int(value), None
    except (TypeError, ValueError):
        return None, "config port is not a number"


def pool_index(kind, path):
    """The fork's stable per-auth index for one pool file, as the producer
    computes it (the fork's auth types: indexSeed and stableAuthIndex): the
    first sixteen hex of sha256 over "<type>:<path>", where <type> is the
    file's JSON `type` normalized exactly as the producer normalizes it —
    surrounding whitespace stripped, then lowercased, so a type spelled
    " Codex " and one spelled "codex" mint the same index — and <path> is
    the absolute, cleaned path
    of the file AS THE PROXY LOADED IT — the auth-dir spelled exactly as
    the config spells it, joined to the basename and made absolute, NEVER
    realpath'd. A symlinked auth-dir spelled differently mints a different
    index, and a record under the other spelling is hashed (THE CONTRACT)."""
    seed = "%s:%s" % (kind.strip().lower(), os.path.abspath(path))
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


def _errno_name(exc):
    return errno.errorcode.get(exc.errno) or exc.__class__.__name__


def pool_accounts(config_path):
    """(accounts, faults) for the sidecar: `accounts` maps the fork's
    `auth_index` of every readable pool file in the auth-dir its config
    declares (the directory CLIProxyAPI loads and hot-reloads) to that
    file's `email`; `faults` names, one string each, everything that could
    not be admitted — the auth-dir undeclared, absent or unenumerable, or
    a file (by basename) unreadable, not JSON, not an object, without a
    `type` or without an `email`. The map is what `account_label` joins
    against and the faults ride the read event (THE CONTRACT, module
    docstring). This reader opens only the `type` and `email` keys of a
    pool file (the other fields hold tokens) and puts a basename and a
    reason, not file content, into a fault.

    TOTAL: this function does not raise. A per-file failure of any class
    (a nesting depth the decoder refuses, an encoding fault, anything
    else) is that file's fault under the exception's class name, and a
    failure anywhere else in the read is one fault naming the class —
    because `snapshot` reads the pool beside a destructive pop, and an
    exception here would reach the watch guard and lose the pop."""
    try:
        return _pool_accounts(config_path)
    except Exception as exc:                # noqa: BLE001 — total by contract
        return {}, ["pool unreadable: %s" % exc.__class__.__name__]


def _pool_accounts(config_path):
    auth_dir, err = _top_scalar(config_path, "auth-dir")
    if err or not isinstance(auth_dir, str) or not auth_dir:
        return {}, ["auth-dir undeclared" if err and "names no" in err
                    else "auth-dir undeclared: %s" % (err or "empty auth-dir")]
    try:
        names = os.listdir(auth_dir)
    except FileNotFoundError:
        return {}, ["auth-dir absent: %s" % auth_dir]
    except OSError as exc:
        return {}, ["auth-dir unenumerable: %s" % _errno_name(exc)]
    except Exception as exc:                # noqa: BLE001 — total by contract
        return {}, ["auth-dir unenumerable: %s" % exc.__class__.__name__]
    accounts, faults = {}, []
    for name in sorted(names):
        if name.startswith(".") or not fnmatch.fnmatch(name, "*.json"):
            continue
        path = os.path.join(auth_dir, name)
        try:
            with open(path, encoding="utf-8") as f:
                rec = json.load(f)
        except OSError as exc:
            faults.append("%s: unreadable (%s)" % (name, _errno_name(exc)))
            continue
        except ValueError:
            faults.append("%s: not JSON" % name)
            continue
        except Exception as exc:            # noqa: BLE001 — total by contract
            faults.append("%s: unreadable (%s)" % (name, exc.__class__.__name__))
            continue
        if not isinstance(rec, dict):
            faults.append("%s: not an object" % name)
            continue
        kind, email = rec.get("type"), rec.get("email")
        if not isinstance(kind, str) or not kind.strip():
            # an empty or whitespace-only type: the producer falls back to
            # the provider there, which a pool file cannot tell us, so the
            # file is a fault rather than a guessed index
            faults.append("%s: no type" % name)
            continue
        if not isinstance(email, str) or not email:
            faults.append("%s: no email" % name)
            continue
        accounts[pool_index(kind, path)] = email
    return accounts, faults


def _pop(port, secret, count=POP_COUNT, timeout=TIMEOUT_S):
    """One management pop -> (records, err). `secret` goes into the header
    and nowhere else; an error names the HTTP class, never the header."""
    url = "http://127.0.0.1:%d%s?count=%d" % (port, MANAGEMENT_PATH, count)
    req = urllib.request.Request(url, headers={"X-Management-Key": secret})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None, ("management routes not enabled (HTTP 404): the "
                          "sidecar was spawned before the meter; it reads "
                          "after its next respawn")
        if exc.code == 401:
            return None, "management key refused (HTTP 401)"
        return None, "HTTP %d" % exc.code
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return None, "management call failed: %s" % (
            getattr(exc, "reason", None) or exc.__class__.__name__)
    try:
        records = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None, "management response is not JSON"
    if not isinstance(records, list):
        return None, "management response is not a list"
    return records, None


def drain(port, secret, timeout=TIMEOUT_S):
    """Every queued record -> (records, err). Pops until a page comes back
    short; a failure mid-way keeps what was popped and names the error."""
    out = []
    count = POP_COUNT
    while True:
        page, err = _pop(port, secret, count=count, timeout=timeout)
        if err:
            return out, err
        out.extend(page)
        if len(page) < count:
            return out, None


def curate(record, accounts=None):
    """The producer's record, kept only where a rollup reads it, with
    `source` passed through `account_label` against the sidecar's pooled
    `accounts` (see `pool_accounts`; None admits nothing) under the
    record's own `auth_index` (THE CONTRACT, module docstring). A record
    that is not an object is returned
    as None: the endpoint marshals raw queue bytes, so a non-JSON item
    arrives as a string."""
    if not isinstance(record, dict):
        return None
    kept = {k: record[k] for k in _KEEP if k in record}
    if "source" in kept:
        kept["source"] = account_label(kept["source"], accounts,
                                       record.get("auth_index"))
    return kept


def _unresolved(record, kept):
    """True when a record carried an `auth_index` and its `source` was
    still hashed: the pool file the producer spent could not be joined."""
    return (bool(record.get("auth_index")) and isinstance(record.get("source"), str)
            and bool(record["source"]) and kept.get("source") != record["source"])


CRED_PREFIX = "cred:"


def account_label(source, accounts=None, auth_index=None):
    """The account a record names, as the ledger writes it: `source`
    verbatim when the pool file its own `auth_index` names (`accounts`,
    index -> email) carries exactly that email, else `cred:` plus the first
    eight hex of its sha256. Why the join is per record and not membership,
    why shape and `auth_type` decide nothing, and the one read-time bound
    under which a credential can still be written are THE CONTRACT in the
    module docstring, and this function adds no promise to it."""
    if not isinstance(source, str) or not source:
        return source
    if accounts and isinstance(auth_index, str) and auth_index \
            and accounts.get(auth_index) == source:
        return source
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()[:8]
    return CRED_PREFIX + digest


def _record_epoch(stamp):
    """Epoch seconds off the proxy's RFC 3339 `timestamp`, or None. Go emits
    nanoseconds; Python reads at most microseconds, so the tail is cut."""
    from datetime import datetime
    if not isinstance(stamp, str):
        return None
    text = re.sub(r"(\.\d{6})\d+", r"\1", stamp.strip())
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


def _request_id(seat, pid, kept):
    """The row id eventledger's grammar requires: the proxy's own request id
    under the sidecar incarnation that minted it, or a digest of the kept
    record when the producer named none."""
    rid = kept.get("request_id")
    if not rid:
        rid = hashlib.sha256(json.dumps(kept, sort_keys=True).encode()).hexdigest()[:16]
    return "%s:%s:%s" % (seat, pid, rid)


def request_event(family, seat, pid, port, record, ts, accounts=None):
    """One ledger event for one popped record; `accounts` is the sidecar's
    index -> email map (`pool_accounts`), the only provenance that keeps a
    `source` verbatim."""
    kept = curate(record, accounts)
    if kept is None:
        return None
    return {"v": V, "id": _request_id(seat, pid, kept), "kind": "request",
            "ts": ts, "seat": seat, "family": family, "pid": pid, "port": port,
            "at": _record_epoch(kept.get("timestamp")), **kept}


def read_event(family, seat, pid, port, status, reason, records, ts,
               persisted=None, pool=None):
    """`pool` is the pass's account-naming outcome, {admitted, faults,
    unresolved} (see `pool_accounts`); it does not move `status` (THE
    CONTRACT, module docstring)."""
    ev = {"v": V, "id": "read:%s:%r" % (seat, ts), "kind": "read", "ts": ts,
          "seat": seat, "family": family, "pid": pid, "port": port,
          "status": status, "reason": reason, "records": records,
          "persisted": records if persisted is None else persisted}
    if pool is not None:
        ev["pool"] = pool
    return ev


def instances():
    """(family, seat, proxy home) for every minted sidecar of a proxy family.
    Resolved through the `seat` facade: the impl modules are seeded by it and
    answer NameError when imported alone."""
    from . import seat as seatmod
    from .seat_catalog import FAMILIES
    for family, seat in seatmod._minted_seats():
        if FAMILIES[family].get("mode") in ("proxy", "proxy-key", "proxy-oauth"):
            yield family, seat, seatmod._proxy_home(family, seat)


def read_seat(family, seat, proxy_home, timeout=TIMEOUT_S):
    """(status, reason, records, pid, port) for one sidecar — the sidecar
    read alone, five values, the seam proxywatch's arms double. A read that
    fails is UNREADABLE with a reason; what READ means is THE CONTRACT in
    the module docstring. The pool is a separate store and is read by
    `snapshot` beside this call, not inside it."""
    from . import seat as seatmod
    from .seat_paths import MGMT_SECRET_FILE, _mgmt_secret_present
    port, err = _port_of(os.path.join(proxy_home, "config.yaml"))
    pid = seatmod._running_pid(family, seat)
    if err:
        return UNREADABLE, err, [], pid, port
    if not _mgmt_secret_present(proxy_home):
        return (UNREADABLE, "no management secret minted: the sidecar was "
                "spawned before the meter; it reads after its next respawn",
                [], pid, port)
    if not seatmod._port_open(port):
        return UNREADABLE, "port %d closed" % port, [], pid, port
    with open(os.path.join(proxy_home, MGMT_SECRET_FILE), encoding="utf-8") as f:
        secret = f.read().strip()
    records, err = drain(port, secret, timeout=timeout)
    del secret
    if err:
        return UNREADABLE, err, records, pid, port
    return READ, None, records, pid, port


def snapshot(now=None, timeout=TIMEOUT_S, seats=None):
    """Read every sidecar once, append what it said to the ledger, and
    return one row per seat: {seat, family, status, reason, records,
    persisted, lost, marker, pid, port, pool}. `records` is what the pop
    took, `persisted` how many of them the ledger durably holds, `lost`
    the difference, `marker` whether this pass's read event is on disk,
    `pool` the account-naming outcome {admitted, faults, unresolved} that
    the read event carries too and that does not move the status (THE
    CONTRACT, module docstring). Any
    refused append turns the status into FAILED_PERSIST with the reason —
    the pop is accounted only once the ledger has it, and a row never says
    READ over records that are gone."""
    ts = now if now is not None else time.time()
    rows = []
    path = ledger_path()
    for family, seat, proxy_home in (seats if seats is not None else instances()):
        # THE POOL IS READ FIRST, ONCE PER SIDECAR, BESIDE THE SIDECAR READ
        # AND NOT INSIDE IT: read_seat keeps its five-value seam (a consumer
        # doubles it with exactly those five), the pop it ends with is
        # destructive, and pool_accounts is total — so nothing that can
        # happen in the pool read happens after a record has left the
        # sidecar. A record collected through any read_seat persists; with
        # no pool it joins nothing and is hashed, it is not dropped.
        accounts, faults = pool_accounts(os.path.join(proxy_home, "config.yaml"))
        status, reason, records, pid, port = read_seat(
            family, seat, proxy_home, timeout=timeout)
        try:
            row = _persist(family, seat, pid, port, status, reason, records,
                           ts, accounts, faults, path)
        except Exception as exc:            # noqa: BLE001 — the pop is already done
            row = _lost_after_pop(family, seat, pid, port, records, ts,
                                  accounts, faults, path, exc)
        rows.append(row)
    return rows


def _persist(family, seat, pid, port, status, reason, records, ts,
             accounts, faults, path):
    """One sidecar's popped records into the ledger, then its read marker;
    -> the snapshot row. Every refused append is FAILED_PERSIST with the
    lost count, per the row grammar in `snapshot`."""
    joined = [(r, request_event(family, seat, pid, port, r, ts, accounts))
              for r in records]
    events = [e for _r, e in joined if e is not None]
    pool = {"admitted": len(accounts), "faults": faults,
            "unresolved": sum(1 for r, e in joined
                              if e is not None and _unresolved(r, e))}
    persisted, marker = 0, False
    with eventledger.locked(path) as held:
        if held:
            for e in events:
                persisted += 1 if eventledger.append_unlocked(path, e) else 0
            lost = len(events) - persisted
            if lost:
                status = FAILED_PERSIST
                reason = "ledger refused %d of %d record(s)%s" % (
                    lost, len(events), " after: %s" % reason if reason else "")
            marker = eventledger.append_unlocked(
                path, read_event(family, seat, pid, port, status, reason,
                                 len(events), ts, persisted=persisted,
                                 pool=pool))
    lost = len(events) - persisted
    if not marker:
        status = FAILED_PERSIST
        reason = ("ledger %s; read marker not written%s%s" % (
            "lock refused" if not held else "refused the read marker",
            ", %d of %d record(s) lost" % (lost, len(events)) if lost else "",
            " (after: %s)" % reason if reason and lost == 0 and held else ""))
    return {"seat": seat, "family": family, "status": status,
            "reason": reason, "records": len(events),
            "persisted": persisted, "lost": lost, "marker": marker,
            "pid": pid, "port": port, "pool": pool}


def _lost_after_pop(family, seat, pid, port, records, ts, accounts, faults,
                    path, exc):
    """THE POST-POP GUARD. The records have left the sidecar and something
    between the pop and the append raised; nothing may swallow that into a
    silent zero (proxywatch's watch guard would turn a raise into "no
    meter rows" and exit 0). The pass is FAILED_PERSIST naming the class
    and message, every popped record counts as lost (the ledger is
    append-only, so over-counting loss is the safe direction), a read
    marker saying so is attempted, and the row exits non-zero through the
    same door a refused append does."""
    count = len(records) if isinstance(records, list) else 0
    reason = "reader raised after the pop (%s: %s); %d popped record(s) lost" % (
        exc.__class__.__name__, exc, count)
    pool = {"admitted": len(accounts), "faults": list(faults), "unresolved": 0}
    marker = False
    try:
        marker = eventledger.append(
            path, read_event(family, seat, pid, port, FAILED_PERSIST, reason,
                             count, ts, persisted=0, pool=pool))
    except Exception:                       # noqa: BLE001 — the row still says so
        marker = False
    return {"seat": seat, "family": family, "status": FAILED_PERSIST,
            "reason": reason, "records": count, "persisted": 0, "lost": count,
            "marker": marker, "pid": pid, "port": port, "pool": pool}


def events(since=None, path=None):
    """(requests, last_read_by_seat, unknown) off the ledger. `since` is an
    epoch floor on the record's own `at` (the proxy's timestamp; `ts` when
    absent). A read event always updates the seat's latest status, so a seat
    that never produced a request still has a row to say why.

    `unknown` IS THE CHANNEL A FAILED READ TRAVELS ON. None means the ledger
    was read whole (absent counts as empty). A ledger that exists but cannot
    be read (permissions, a directory in its place, an unsafe path) yields NO
    rows and the checked reader's reason; a ledger with a malformed complete
    row yields the rows that parse and a reason naming the first row that
    did not — those stay UNKNOWN and uncounted. A caller that prints a total
    from this function prints `unknown` beside it or the total is a lie."""
    path = path or ledger_path()
    rows, unavailable = eventledger.checked_events(path)
    if unavailable:
        return [], {}, "ledger unreadable: %s" % unavailable
    # The strict read poisons on the first malformed COMPLETE row and names
    # it; the ledger is append-only, so a second open sees the same rows
    # plus at most a newer tail.
    _strict_rows, corrupt = eventledger.checked_events(path, strict=True)
    unknown = "malformed ledger row(s) skipped, uncounted: %s" % corrupt \
        if corrupt else None
    requests, last_read = [], {}
    for ev in rows:
        if not isinstance(ev, dict) or ev.get("v") != V:
            continue
        if ev.get("kind") == "read":
            last_read[ev.get("seat")] = ev
            continue
        if ev.get("kind") != "request":
            continue
        when = ev.get("at") if isinstance(ev.get("at"), (int, float)) else ev.get("ts")
        if since is not None and (when or 0) < since:
            continue
        requests.append(ev)
    return requests, last_read, unknown


def tokens_of(ev):
    """(input, output) off one request event's `token_breakdown`; a record
    without the v2 breakdown counts nothing rather than something guessed."""
    tb = ev.get("token_breakdown")
    if not isinstance(tb, dict):
        return 0, 0
    inp = (tb.get("input") or {}).get("total_tokens")
    out = (tb.get("output") or {}).get("total_tokens")
    return (int(inp) if isinstance(inp, (int, float)) else 0,
            int(out) if isinstance(out, (int, float)) else 0)


def cmd_proxy_usage(args):
    """proxy-usage [--json] — read every proxy-family sidecar's usage queue
    now, append it to the ledger, and print one status line per seat."""
    args = list(args or [])
    from .cli import guard_tail
    rc = guard_tail("helm proxy-usage", args, flags=("--json",),
                    usage="proxy-usage [--json]")
    if rc is not None:
        return rc
    rows = snapshot()
    rc = 1 if any(r["status"] != READ for r in rows) else 0
    if "--json" in args:
        print(json.dumps({"ledger": ledger_path(), "rows": rows}, indent=2,
                         sort_keys=True))
        return rc
    if not rows:
        print("helm proxy-usage: no minted proxy-family sidecar to read")
        return 0
    print("helm proxy-usage — %d sidecar(s) read; ledger %s"
          % (len(rows), ledger_path()))
    for r in rows:
        print("  %-12s %-6s port %-5s pid %-7s %s"
              % (r["seat"], r["family"], r["port"] or "?", r["pid"] or "?",
                 row_note(r)))
        if r["pool"]["faults"] or r["pool"]["unresolved"]:
            print("      %s" % pool_note(r["pool"]))
    return rc


def pool_note(pool):
    """The account-naming trailer under a seat line: how many pool files
    were admitted, every fault, and how many records were hashed although
    they carried an `auth_index`. Printed only when something is non-zero;
    READ over such a trailer is what THE CONTRACT (module docstring) says
    READ means."""
    faults = pool["faults"]
    return "pool: %d admitted, %d fault%s%s, %d unresolved" % (
        pool["admitted"], len(faults), "" if len(faults) == 1 else "s",
        " (%s)" % "; ".join(faults) if faults else "", pool["unresolved"])


def row_note(r):
    """One row's status phrase, shared by the verb and proxywatch's render."""
    if r["status"] == READ:
        return "%d record(s)" % r["records"]
    if r["status"] == FAILED_PERSIST:
        # THE PHRASE SAYS WHAT `lost` SAYS: a refused read marker over fully
        # persisted records lost nothing but the marker, and must not read
        # as records not persisted.
        if not r["lost"]:
            return "%s — read marker not persisted, 0 of %d record(s) lost — %s" % (
                FAILED_PERSIST, r["records"], r["reason"])
        return "%s — %d of %d record(s) not persisted — %s" % (
            FAILED_PERSIST, r["lost"], r["records"], r["reason"])
    return "%s — %s" % (UNREADABLE, r["reason"])
