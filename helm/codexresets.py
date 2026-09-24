#!/usr/bin/env python3
"""helm codex resets — the earned rate-limit reset credits, read and spent.

WHAT A RESET CREDIT IS. A codex account earns a small number of rate-limit
reset credits. Redeeming one clears that account's rate-limit windows at once,
ahead of the natural reset. A credit is SCARCE, it belongs to the owner, and a
redemption cannot be undone.

THE FAILURE THIS MODULE PREVENTS. A pooled codex account reaches its WEEKLY
(7d) wall, every seat routed at it stops until a natural reset that can be days
away, and an earned credit that would have cleared the wall sits unspent —
because spending it was something a human had to notice and do by hand. The
owner asked for that to happen without him.

THE POLICY, owner-authorized (the store's
codex-weekly-zero-auto-uses-an-available-reset premise): when a pooled
account's WEEKLY window is MEASURED at zero remaining and a credit is
available, helm spends exactly ONE credit, journals the attempt, re-reads the
account, and posts one room line. Never for a 5h wall alone. Never twice for
one exhaustion. Never on a reading it cannot trust.

LAWS:
  - NO TOKEN AND NO ACCOUNT ID REACHES STDOUT, THE LEDGER, OR AN ERROR
    STRING. Rows carry the email, or a truncated account id, and nothing else
    that identifies the credential; every note this module mints passes
    through `_redact` with the call's own secrets before it is returned, and
    no vendor body is ever echoed. A diagnostic assembled from raw input is
    the way a secret escapes a module that never deliberately prints one.
  - REFUSAL AND FAILURE ARE DIFFERENT BRANCHES, AND A 2xx CAN BE EITHER.
    The vendor refusing (401, 429, another 4xx) and each of the four
    documented codes are settled answers about the balance, and a caller may
    act on them. A timeout, an unreachable host, a 5xx — AND a 2xx this
    module cannot classify, an unreadable body or a `code` it does not know —
    are FAILURES whose effect on the balance is UNKNOWN, and every consumer
    has to treat them as "a credit may have been spent". A 2xx on the consume
    POST is the case where a redemption is MOST likely, not least: the request
    reached the endpoint whose whole job is to redeem, and it answered.
  - AN UNRESOLVED OUTCOME IS NEVER RETRIED UNDER A NEW KEY. The consume
    endpoint takes a `redeem_request_id` idempotency key, so re-driving the
    SAME key is free and minting a FRESH one after an outcome nobody could
    resolve is how one wall costs two credits. The key is journaled BEFORE the
    request leaves and the outcome beside it after, so a process killed
    mid-call still leaves the key on disk; the next attempt re-drives the
    newest key the VENDOR never answered about (`open_key`).
  - HELM DOES NOT SPEND WHAT IT CANNOT RECORD. A journal that refuses the
    write-ahead row stops the send, because the ledger is the only thing that
    holds one wall to one credit.
  - A READING HELM CANNOT TRUST YIELDS NO ACTION. An absent, stale or
    unreadable weekly measurement is not a measurement of zero, and neither is
    an unreadable attempt ledger. Both answer NO-ACT, never a guess.
  - THE DECISION IS A PURE FUNCTION (`decide`). It takes the reading, its age,
    the credit listing and the account's attempt history, and it performs no
    I/O, so the whole policy is testable without a single vendor call.

THE MEASUREMENT IS HELM'S EXISTING ONE. `codexbudget` already probes every
pooled codex identity once per proxywatch pass, already knows that the binding
window for codex is the fullest ACCOUNT-WIDE window rather than the shortest,
and already publishes a snapshot with an explicit freshness bound
(`codexbudget.GATE_MAX_AGE_S`). This module reads that, and never opens a
second census or a second parser.

AND THE CREDENTIAL IS THE POOLED ONE, not the codexhome's. `codexbudget`'s
module law holds here for the same reason: the codex CLI refreshes a home's
auth.json only while it runs in it, so a home can hold an access token the
vendor answers 401 for while the POOLED copy of the same account answers 200.
Spending a credit is the last place to send a stale token.

THE HORIZON. chatgpt.com/backend-api/wham is a private vendor backend that
nobody versions for us. A vendor change breaks these two calls, and the
failure is LOUD — a non-2xx, an unknown code, or an unparseable body, each
with its own named result and each posted to the room — never a silent
no-op. The upstream metaharness calls the same two endpoints; on each upstream
upgrade, re-diff its reset-credit client against this one.
"""
import collections
import hashlib
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from . import eventledger
from . import home


# ------------------------------------------------------------------ the wire

#: The vendor's reset-credit surface. NOTHING HERE DEFAULTS TO IT: every call
#: that can reach the network takes its base as an ARGUMENT and refuses
#: without one, and `live_base_url()` is named by exactly two callers — the
#: proxywatch rung and the CLI door. An arm that forgets the argument reaches a
#: named refusal instead of the owner's account, which is the difference
#: between a fence and a convention.
#: HELM_CODEX_RESETS_BASE_URL redirects `live_base_url()` — it can only ever
#: point AWAY from the live endpoint, so it is a safety valve (and the seam a
#: dogfood run pins at a dead port), never a way to reach a credential this
#: module could not otherwise reach.
BASE_URL = "https://chatgpt.com/backend-api/wham"
LIST_PATH = "/rate-limit-reset-credits"
CONSUME_PATH = "/rate-limit-reset-credits/consume"

TIMEOUT_S = 15
#: A vendor body larger than this is malformed by definition: both answers are
#: a handful of fields. Bounded so a hostile or broken upstream cannot make a
#: watchdog pass allocate without limit.
MAX_BODY_BYTES = 256 * 1024

REDACTED = "<redacted>"
#: Shorter than this, a "secret" is too generic to blind-replace inside prose:
#: scrubbing a four-character string would corrupt unrelated words while
#: protecting nothing a real token or account id needs.
MIN_SECRET_LEN = 8


# ----------------------------------------------------------- list statuses

LIST_OK = "listed"                  # a 2xx body helm could read
LIST_UNAUTHORIZED = "unauthorized"  # 401/403 — the credential was refused
LIST_RATE_LIMITED = "rate-limited"  # 429 — the vendor asked us to back off
LIST_HTTP_ERROR = "http-error"
#: A 3xx is not a balance; the host it points at is one helm never named.
LIST_REFUSED_REDIRECT = "refused_redirect"
LIST_MALFORMED = "malformed"        # a 2xx helm could not parse
LIST_UNREACHABLE = "unreachable"    # timeout, DNS, connection failure
#: NOT A REFUSED CREDENTIAL. No pooled credential could be bound to this
#: reading at all, so nothing was ever sent; rendering that as `unauthorized`
#: told an operator the vendor had rejected a token helm never had.
LIST_NO_CREDENTIAL = "no-credential"
#: Nobody supplied a base url, so the call was refused before it was built.
LIST_NO_BASE = "no-base-url"

LIST_STATUSES = (LIST_OK, LIST_UNAUTHORIZED, LIST_RATE_LIMITED,
                 LIST_REFUSED_REDIRECT, LIST_HTTP_ERROR, LIST_MALFORMED,
                 LIST_UNREACHABLE, LIST_NO_CREDENTIAL, LIST_NO_BASE)


# --------------------------------------------------------- consume outcomes

#: The four codes the vendor documents for a 2xx consume.
OUTCOME_RESET = "reset"                        # a credit was spent, wall gone
OUTCOME_NOTHING = "nothing_to_reset"           # no live wall to clear
OUTCOME_NO_CREDIT = "no_credit"                # the balance is empty
OUTCOME_ALREADY = "already_redeemed"           # THIS key already redeemed

#: Everything else, each its own branch.
OUTCOME_UNKNOWN_CODE = "unknown-code"          # 2xx, a code helm does not know
OUTCOME_UNAUTHORIZED = "unauthorized"          # 401/403
OUTCOME_RATE_LIMITED = "rate-limited"          # 429
OUTCOME_REFUSED = "refused"                    # any other 4xx
OUTCOME_MALFORMED = "malformed"                # 2xx, unparseable body
#: THE TWO 2xx BRANCHES ABOVE ARE ANSWERS THAT SAY NOTHING HELM CAN ACT ON.
#: `unknown-code` and `malformed` both mean the redemption endpoint replied
#: 2xx and this module could not tell whether the credit left the balance, so
#: they are UNRESOLVED (below) and NOT refusals.
#: THE ONE OUTCOME THAT IS NOT AN ANSWER. A 5xx, a timeout or an unreachable
#: host leaves the credit balance UNKNOWN: the vendor may have redeemed before
#: it failed. The key rides the result so the next attempt re-drives it.
OUTCOME_UNKNOWN = "UNKNOWN"
#: WRITTEN BEFORE THE REQUEST LEAVES, never by the vendor. A process killed
#: between the send and the outcome row would otherwise leave NO record of a
#: key that may already have redeemed a credit, and the next pass would mint a
#: fresh one — the exact double spend the idempotency key exists to prevent.
#: A row still reading `pending` is therefore an UNKNOWN outcome that nobody
#: survived to name, and it is treated as one.
OUTCOME_PENDING = "pending"

VENDOR_CODES = (OUTCOME_RESET, OUTCOME_NOTHING, OUTCOME_NO_CREDIT,
                OUTCOME_ALREADY)
#: Outcomes under which a credit certainly left the balance.
SPENT_OUTCOMES = (OUTCOME_RESET, OUTCOME_ALREADY)
OUTCOMES = VENDOR_CODES + (OUTCOME_UNKNOWN_CODE, OUTCOME_UNAUTHORIZED,
                           OUTCOME_RATE_LIMITED, OUTCOME_REFUSED,
                           OUTCOME_MALFORMED, OUTCOME_UNKNOWN, OUTCOME_PENDING)
#: THE OUTCOMES THAT LEAVE THE BALANCE UNKNOWN, and whose key MUST therefore
#: be re-driven rather than replaced. The two 2xx branches are in here for the
#: 5xx's own stated reason and with more force behind it: the vendor ANSWERED,
#: and the answer came from the endpoint whose whole job is to redeem. Calling
#: either of them "settled, and nothing was spent" DROPS the idempotency key —
#: the reuse branch below never fires, the next eligible pass mints a fresh
#: key, and one wall costs two irreversible credits. That is reachable without
#: any vendor misbehaviour: a redemption that lands while the usage percentage
#: still lags is exactly the world the observed-wall branch exists for, so the
#: next pass can read the same spent week and consume again.
UNRESOLVED_OUTCOMES = (OUTCOME_UNKNOWN, OUTCOME_PENDING, OUTCOME_MALFORMED,
                       OUTCOME_UNKNOWN_CODE)

#: THE ONE OUTCOME THAT DOES NOT BURN THE COOL-DOWN. The cool-down exists to
#: stop ONE wall costing TWO credits, so it is owed only by an attempt that
#: may have moved the balance. A 401/403 moved nothing and is certain about
#: it: the credential was refused before any redemption, and making the wall
#: wait two hours after the token is refreshed costs the owner the capacity
#: the credit was meant to buy back.
#: EVERY OTHER OUTCOME STILL BURNS IT, and each for its own reason: a 429 IS
#: the vendor asking for a back-off and the cool-down is how helm gives it;
#: another 4xx is a request helm must not repeat until somebody has read why;
#: a malformed 2xx and an UNKNOWN may both have redeemed.
#: AND THE EXEMPTION IS TAKEN ON THE LAST WORD ABOUT A KEY, never row by row
#: (`cooldown_attempts`). Every attempt journals `pending` BEFORE the request
#: leaves, so the pass that collects the 401 leaves a pending row behind it
#: too; a cool-down that reads both rows finds one that names no outcome at
#: all, which can never be exempt, and burns the full two hours this exemption
#: exists to spare. Measured through `reset_pass`, not reasoned: the exemption
#: was inert in production while its arm — a hand-built one-row history the
#: acting path never produces — read green.
COOLDOWN_EXEMPT_OUTCOMES = (OUTCOME_UNAUTHORIZED,)

#: THE FLOOR THE EXEMPTION KEEPS. Exempting an outcome from the cool-down
#: removes the ONLY bound on how often that request may be repeated, so the
#: exemption carries two bounds of its own, and this is the first: one pass.
#: A refused credential is re-driven on the NEXT pass and never twice inside
#: one — the manual door typed beside the rung, or a long pass whose successor
#: overlaps it, would otherwise send two refused requests in a breath. One
#: rung interval is the smallest bound that can mean anything here, since the
#: rung is the only unattended caller; an arm pins this number to
#: `proxywatch.INTERVAL_S` rather than letting the two drift.
REFUSAL_FLOOR_S = 900
#: AND A RUN OF REFUSALS ON ONE KEY ENDS THE EXEMPTION ALTOGETHER. The
#: exemption is for a TRANSIENT refusal — the minutes between a token expiring
#: and the rotation that cures it — and a refusal that outlives the age a
#: budget reading may carry and still be acted on (`codexbudget.GATE_MAX_AGE_S`
#: divided by the floor above: four passes, one hour) is not transient. It is
#: a standing property of the credential, no pass of this rung will move it,
#: and it falls back to the ordinary cool-down. So four is not a taste: it is
#: the tree's own bound on how old a fact may be, counted in passes, and an
#: arm derives it from both modules rather than transcribing it.
#: MEASURED, through the real pass against a local vendor: a credential whose
#: listing succeeds and whose redemption answers 403 sent EIGHT requests over
#: eight passes under the unbounded exemption, and sends four under this one —
#: after which the cool-down takes over and one refusal costs one request
#: every two hours.
REFUSAL_RUN_MAX = 4

#: THE OUTCOMES THE ROOM HEARS ONCE PER STATE INSTEAD OF ONCE PER EVENT, and
#: that they are exactly the cool-down's exempt outcomes is a derivation
#: rather than a coincidence: the cool-down is what bounds how often an
#: attempt can repeat, so an outcome exempt from it is the one an unlatched
#: room line would repeat every pass. Any future exemption inherits the latch
#: with it, which is the half this module shipped without.
LATCHED_OUTCOMES = COOLDOWN_EXEMPT_OUTCOMES


# ------------------------------------------------------------- the policy

#: A window at or past this percent of its allowance is spent.
WEEKLY_EXHAUSTED_PCT = 100.0
#: The weekly window is the account-wide one at least this long. The vendor
#: sends it as secondary_window on one plan shape and as the only window on
#: another, so the LENGTH identifies it and the field name does not.
WEEKLY_MIN_SECONDS = 6 * 86400

#: A NATURAL RESET THIS CLOSE MAKES THE CREDIT WASTE. A credit restores the
#: window now; if the window restores itself in T seconds, the credit buys T
#: seconds and nothing more, while remaining unavailable for a wall that
#: arrives with days to run. One hour is the floor because the pass that
#: decides this runs every fifteen minutes: a floor narrower than several
#: passes would let one pass spend a credit the clock was about to refund for
#: free, and an hour of one pooled account's idle time is cheap against a
#: week of that account's capacity.
NATURAL_RESET_FLOOR_S = 3600

#: ONE EXHAUSTION, ONE ATTEMPT. Any recorded attempt for an account inside
#: this window blocks the next one. It must exceed the age the reading itself
#: may carry (`codexbudget.GATE_MAX_AGE_S`, one hour): a cool-down shorter
#: than the snapshot's own staleness bound lets a single wall be SEEN twice
#: and spend two credits. Two hours leaves a margin for a pass that was
#: skipped, and it also rate-limits the room line.
CONSUME_COOLDOWN_S = 7200

#: ONE REDEMPTION PER PASS, ACROSS THE WHOLE POOL. The per-account cool-down
#: cannot see a SECOND account, and several pooled accounts reach their weekly
#: wall within hours of each other — measured on this fleet, three at once — so
#: the first unattended pass after this ships would otherwise spend three
#: irreversible owner assets in one breath, before anybody had watched it spend
#: one. The others are not refused, only deferred: the pass runs every fifteen
#: minutes, and by the next one the first redemption's effect is measurable.
MAX_CONSUMES_PER_PASS = 1

#: The attempt ledger, beside proxywatch's own state and the budget snapshot.
LEDGER_NAME = "codex-reset-attempts.jsonl"

NOTICE_TAG = "CODEX-RESET"


# --------------------------------------------------- what kind of wall it is

#: THE ONE REACHED-TYPE A RESET CREDIT ANSWERS. The vendor names why an
#: account is walled, `codexbudget` already parses it onto every row, and a
#: RATE-LIMIT reset credit lifts a RATE-LIMIT wall and nothing else.
RATE_LIMIT_REACHED = "rate_limit_reached"

#: THE WALLS A RESET CREDIT CANNOT LIFT. Both name the WORKSPACE'S CREDIT
#: BALANCE rather than a rate-limit window, so a rate-limit reset buys nothing
#: and the credit is gone. Whether the vendor would even accept one against
#: such a wall is UNVERIFIED, and it cannot be verified without spending the
#: owner's asset to ask — which is precisely why this refuses instead.
CREDITS_DEPLETED_TYPES = ("workspace_member_credits_depleted",
                          "workspace_owner_credits_depleted")

#: HOW THE WEEKLY WALL WAS ESTABLISHED, on the row and in the reason text.
WALL_MEASURED = "measured"   # the usage endpoint reads 100% used
WALL_OBSERVED = "observed"   # a 429 helm already recorded, naming this window

#: THE SECOND SIGNAL OF ZERO, AND ITS FLOOR. The owner's own account of this
#: failure is that he notices the wall by seeing a 429, not by reading a
#: percentage: the usage endpoint's weekly percent LAGS the refusals, so a
#: credential can be refused at 92% used. helm therefore accepts a 429 it has
#: ALREADY OBSERVED for the same credential as a measurement of zero — but
#: only as a small correction to a reading that already stands at the wall's
#: edge. Below this percent the reading and the 429 CONTRADICT each other, and
#: a contradiction is never an authorization to spend: the stale-cooldown row
#: exists because a sidecar holds a 429 belief long after the window it was
#: about has reopened, and that belief over a reading at 4% is that defect,
#: not a wall.
#: AND IT IS A SIGNAL OF ZERO ONLY, NEVER OF WHY. Measured across every
#: cooling pooled credential on this fleet, the vendor's 429 body reads
#: error.type `usage_limit_reached` for the rate-limited accounts and for the
#: credits-depleted ones alike. The reached-type gate below is therefore not
#: something a 429 can satisfy.
WEEKLY_NEAR_WALL_PCT = 90.0

#: WHICH WINDOW THAT 429 WAS ABOUT. A sidecar's cooldown instant comes from
#: the vendor's own `resets_in_seconds` at the moment of the refusal, and the
#: weekly window's `reset_at` comes from the same vendor's usage answer, so
#: for one weekly wall the two name one instant, to within the gap between the
#: two calls. Wider than a pass interval, so a cooldown minted one pass ago
#: still matches; far narrower than the distance between a 5h window's reset
#: and a 7d window's, which is what stops a 5h-wall 429 being read as a weekly
#: one.
COOLING_MATCH_TOLERANCE_S = 900

#: The digest length of a ledger member key: long enough that two pooled
#: credentials cannot collide, short enough to read in a row.
MEMBER_ID_LEN = 16


# ---------------------------------------------------------------- redaction

def _redact(text, *secrets):
    """`text` with every secret blinded. The LAST thing every note passes
    through, because the strings that carry a credential out of a module are
    the ones assembled from input nobody inspected — a vendor error page, an
    exception repr, a URL somebody appended a query to."""
    out = str(text)
    for secret in secrets:
        s = str(secret or "")
        if len(s) >= MIN_SECRET_LEN:
            out = out.replace(s, REDACTED)
    return out


def _secrets(account):
    """The values that must never appear in this account's output."""
    return ((account or {}).get("access_token"), (account or {}).get("account_id"))


def short_account(account_id):
    """An account id truncated to a recognizable handle. The full id is an
    identifier for the owner's account and never leaves this module; eight
    characters are enough for a human to match a row against `helm codex
    list`, which renders the same handle."""
    if not account_id:
        return None
    text = str(account_id)
    return text[:8] + "…" if len(text) > 9 else text


def short_file(name):
    """A six-hex handle for a pool file, for a surface that must name WHICH
    credential without printing one. The pool file is named from the
    account's own canonical spelling and can carry an email, so the name
    itself is never printed. The handle is a sha256 prefix: stable across
    passes, and wide enough to separate a pool this size. It is NOT the
    ledger's member digest — that one hashes a different preimage — so the
    two must never be matched against each other."""
    if not name:
        return "?"
    return hashlib.sha256(str(name).encode("utf-8")).hexdigest()[:6] + "\u2026"


def member_id(record):
    """THE STABLE NAME OF ONE POOLED CREDENTIAL, as a digest.

    THE IDENTITY IS NOT THIS MODULE'S. It is `codexhomes._member_key`'s — the
    workspace account id AND the chatgpt user id — because on a ChatGPT Team
    plan the account id ALONE is the WORKSPACE, shared by every member, and
    `codexbudget.pool_accounts` already resolves both halves for every pooled
    record. A record carrying no account id falls back to the pool FILE, which
    is the census's own fallback key for exactly that case.

    WHAT IS WRITTEN TO DISK IS A DIGEST OF THAT PAIR, NOT THE PAIR, because
    this module's own law is that no account id reaches the ledger. And the
    ledger cannot be keyed on the DISPLAY LABEL instead: the label falls back
    email -> short account id -> file, and `codexbudget.probe_record` takes the
    email from the VENDOR'S BODY, so a body that omits it on one pass renames
    the account, empties its history and clears its cool-down.

    Always taken from the RESOLVED POOLED RECORD rather than from a budget
    row, so the key names the credential a redemption would actually be sent
    with."""
    record = record or {}
    account_id = record.get("account_id")
    if account_id:
        raw = "account\x00%s\x00user\x00%s" % (account_id,
                                                 record.get("user_id") or "")
    else:
        raw = "file\x00%s" % (record.get("file") or "")
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:MEMBER_ID_LEN]


def member_digests_for(record):
    """The WRITES keep `member_id`'s single key; the READS ask for both
    spellings the member's history may live under (task/2734). One door so
    the ledger's truncation is named once."""
    from . import codexhomes
    record = record or {}
    return codexhomes.member_digests(record.get("account_id"),
                                     record.get("user_id"),
                                     MEMBER_ID_LEN)


def resolve_credential(accounts, reading):
    """(the ONE pooled credential that serves this reading, refusal reason).

    NEVER BY ACCOUNT ID ALONE, AND THIS IS A MEASURED LAW, NOT A PRECAUTION.
    `codexhomes._member_key` records it: on a Team plan the id_token's
    chatgpt_account_id is the WORKSPACE id, shared by every member, and three
    members of one workspace carry one account id with three distinct user
    ids. Resolving on the account id returns whichever sibling the census
    listed first — and this rung would then redeem the WALLED member's wall
    with a SIBLING'S bearer token, spending the sibling's credit, clearing the
    sibling's windows, and leaving the walled account exactly as walled as it
    was. Measured on this fleet: three of six pooled rows share one account
    id.

    So the match is the MEMBER RULE, `codexhomes._serves_member`: on the
    account id, the user ids decide when both are known, else the same login
    address, else the id alone only on a plan KNOWN to be personal. It must
    return EXACTLY ONE. It matched a missing user id on the account id alone
    (task/2981): a reading with no user id, beside one pooled sibling,
    resolved to that SIBLING's token.
    Among the candidates not proven another member, the pool FILE the
    reading was probed from breaks a tie (the census's own row identity,
    unique per credential). A candidate nothing proves either way, and that
    the file does not single out, is AMBIGUOUS and refuses: an asset this
    module cannot un-spend is not spent on a guess about whose it is."""
    from . import codexhomes
    reading = reading or {}
    named_file = reading.get("file")

    def by_file(rows):
        return [a for a in rows
                if named_file and named_file in (a.get("files") or ())]

    want = (reading.get("account_id"), reading.get("user_id"))
    if want[0]:
        same = [a for a in accounts or () if a.get("account_id") == want[0]]
        verdicts = [(a, codexhomes._serves_member(
            a, want[1], reading.get("email"), len(same) > 1)) for a in same]
        hits = [a for a, v in verdicts if v]
        # not proven ANOTHER member: this one, or nobody can say
        open_ = [a for a, v in verdicts if v is not False]
    else:
        # No account id on the reading (an unreadable pool file's row is one):
        # the census's own fallback key is the file name.
        hits = open_ = by_file(accounts or ())
    if len(hits) != 1:
        narrowed = by_file(hits or open_)
        if len(narrowed) == 1:
            hits = narrowed
    if not hits and open_:
        return None, R_AMBIGUOUS_CREDENTIAL
    if not hits:
        return None, R_NO_CREDENTIAL
    if len(hits) > 1:
        return None, R_AMBIGUOUS_CREDENTIAL
    if not hits[0].get("access_token"):
        return None, R_NO_CREDENTIAL
    return hits[0], None


def label(row):
    """The one name a reset row is known by, on every surface and in the
    ledger: the email when the credential carries one, else a truncated
    account id, else the pool file. Deliberately NOT `codexbudget._name`,
    which falls back to the FULL account id — correct for a local budget
    table, wrong for a record this module writes to disk and hands to a room."""
    row = row or {}
    return (row.get("email") or short_account(row.get("account_id"))
            or row.get("file") or "?")


# ------------------------------------------------------------------ the HTTP

def live_base_url():
    """THE LIVE VENDOR, NAMED ON PURPOSE. The only function in this module
    that resolves to the real endpoint, so the two production doors that call
    it — the proxywatch rung and `helm codex resets` — are the complete list
    of places a request to the owner's account can originate. Every other
    caller must be handed a base, and is refused without one."""
    return str(home.env("CODEX_RESETS_BASE_URL") or BASE_URL).rstrip("/")


def _url(url_base, path):
    """The endpoint, or None when nobody supplied a base."""
    base = str(url_base or "").rstrip("/")
    return (base + path) if base else None


def _headers(account):
    """Bearer plus the account header the vendor scopes the call by. Spelled
    exactly as `codexbudget` spells it for the usage read — one credential,
    one header grammar, measured against this vendor."""
    headers = {"Authorization": "Bearer " + str(account.get("access_token"))}
    if account.get("account_id"):
        headers["chatgpt-account-id"] = account["account_id"]
    return headers


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect handler that redirects nothing. `redirect_request`
    answering None makes the opener fall through to the default error
    handler, which raises the 3xx as an `HTTPError` — so the caller sees the
    redirect itself rather than an answer from somewhere it never named."""

    def redirect_request(self, *_args, **_kwargs):
        return None


def _call(url, headers, payload=None, timeout=None, follow_redirects=True):
    """(http status, raw body bytes). Raises `urllib.error.HTTPError` for a
    non-2xx and the OSError family for everything else, so each verb below
    classifies once, in one place, with the branch names its own callers
    reason about.

    `follow_redirects=False` IS THE SPENDING PATH'S SETTING, and it is not a
    preference. urllib follows a 302 on a POST by RE-ISSUING IT AS A GET with
    the body dropped, and the redirect target's answer then becomes this
    call's answer: a URL nobody in this module named could return the vendor's
    own `{"code": "reset"}` shape, close the idempotency key, and have helm
    tell the owner a credit of his was spent. A 3xx on a redemption is
    therefore an answer helm does not have."""
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    hdrs = dict(headers)
    if data is not None:
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=hdrs,
                                 method="GET" if data is None else "POST")
    opener = urllib.request.urlopen if follow_redirects \
        else urllib.request.build_opener(_NoRedirect).open
    with opener(req, timeout=timeout or TIMEOUT_S) as r:
        return getattr(r, "status", r.getcode()), r.read(MAX_BODY_BYTES + 1)


def _body(raw):
    """A parsed object, or None. The body is never returned to a caller and
    never quoted in a note: a vendor page assembled around our own request can
    contain the request, and the request carries the credential."""
    if raw is None or len(raw) > MAX_BODY_BYTES:
        return None
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _int(value):
    """A non-negative count, or None. `True` is not a count — bool is an int
    subclass, and a body carrying available_count=true would otherwise read as
    one credit in hand."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= 0 else None


#: A credit whose `status` is not this cannot be spent.
CREDIT_AVAILABLE = "available"


def list_credits(account, url_base, timeout=None):
    """One account's reset-credit balance. READ-ONLY; never raises.

    {status, available, spendable, total_earned, credits, note}.

    EACH CREDIT IS REDUCED TO THE FOUR FIELDS A DECISION USES — when it
    expires, whether it is still available, what it resets, and whether this
    plan honours it. The vendor's own entries carry more, including a profile
    id and a profile image url, and none of that may cross into a row helm
    prints or journals.

    AND THE COUNT IS NOT TAKEN ON TRUST. `available_count` is one number the
    vendor computes; the entries say, one by one, whether a credit is
    `available` and supported by this plan. `spendable` is the SMALLER of the
    two whenever entries are present, which can only ever make helm spend
    less — the correct direction of error for an asset that cannot be
    un-spent."""
    token, acct = _secrets(account)
    url = _url(url_base, LIST_PATH)
    if url is None:
        return _list_result(LIST_NO_BASE,
                            note="no vendor base url was supplied, so the "
                                 "listing was refused before it was built")
    try:
        # A REDIRECT ON A READ IS AS FOREIGN AS A REDIRECT ON A SPEND. The
        # consume path refuses them because a re-issued request reaches a
        # host helm never named; the same is true of the listing — a
        # followed 302's body would be read as THIS account's balance while
        # answering somebody else's, which is how a foreign balance reads as
        # ours.
        status, raw = _call(url, _headers(account), timeout=timeout,
                            follow_redirects=False)
    except urllib.error.HTTPError as e:
        code = e.code
        e.close()               # an HTTPError IS a response — close its fp
        return _list_result(
            LIST_REFUSED_REDIRECT if 300 <= code < 400
            else LIST_UNAUTHORIZED if code in (401, 403)
            else LIST_RATE_LIMITED if code == 429 else LIST_HTTP_ERROR,
            note=_redact("the reset-credit listing answered http_%d" % code,
                         token, acct))
    except Exception as e:                  # noqa: BLE001 — never a failure
        return _list_result(LIST_UNREACHABLE,
                            note=_redact("the reset-credit listing did not "
                                         "complete (%s)" % e.__class__.__name__,
                                         token, acct))
    data = _body(raw)
    if data is None:
        return _list_result(LIST_MALFORMED,
                            note="the reset-credit listing answered http_%d "
                                 "with a body helm could not read" % status)
    available = _int(data.get("available_count"))
    if available is None:
        return _list_result(LIST_MALFORMED,
                            note="the reset-credit listing carries no numeric "
                                 "available_count")
    credits_ = [{"expires_at": c.get("expires_at"), "status": c.get("status"),
                 "reset_type": c.get("reset_type"),
                 "supported": c.get("is_supported_by_plan")}
                for c in (data.get("credits") or ())
                if isinstance(c, dict)]
    return _list_result(LIST_OK, available=available,
                        total_earned=_int(data.get("total_earned_count")),
                        credits=credits_)


def _usable(credits):
    """The entries helm may act on: available, and honoured by this plan. An
    entry that states NEITHER field is counted — a vendor that stops sending
    them must not silently zero a real balance."""
    return [c for c in credits or ()
            if c.get("status") in (None, CREDIT_AVAILABLE)
            and c.get("supported") is not False]


def _list_result(status, available=None, total_earned=None, credits=None,
                 note=None):
    credits = list(credits or ())
    spendable = available
    if credits and available is not None:
        spendable = min(available, len(_usable(credits)))
    return {"status": status, "available": available, "spendable": spendable,
            "total_earned": total_earned, "credits": credits, "note": note}


def mint_key():
    """A fresh idempotency key. Minted OUTSIDE `decide` so the policy stays a
    pure function of what it was handed."""
    return str(uuid.uuid4())


def consume(account, redeem_request_id, url_base, timeout=None):
    """Redeem ONE reset credit. Never raises, NEVER RETRIES.

    {outcome, spent, settled, redeem_request_id, http_status, note}:
      spent   — True when a credit certainly left the balance, False when it
                certainly did not, None when the call's effect is UNKNOWN.
      settled — whether the vendor gave an answer at all.

    A retry inside this call is the bug it exists to prevent: the second
    request of a pair whose first outcome is unknown is the one that spends
    the owner's second credit. The key comes back with the result so the
    caller can journal it and re-drive THAT key later, which is free."""
    token, acct = _secrets(account)
    url = _url(url_base, CONSUME_PATH)
    key = str(redeem_request_id or "")
    if url is None:
        # THE VENDOR IS NEVER A DEFAULT ON THE SPENDING PATH. A base url is an
        # argument of this call, so an arm, a script or a future rung that
        # forgets to supply one is refused here rather than redeeming a real
        # credit against the owner's real account.
        return _consume_result(OUTCOME_REFUSED, key,
                               note="no vendor base url was supplied; a "
                                    "consume never defaults to the live "
                                    "endpoint")
    if not key:
        return _consume_result(OUTCOME_REFUSED, key,
                               note="a consume without an idempotency key is "
                                    "refused here, never sent: an unkeyed "
                                    "retry is how one wall spends two credits")
    try:
        status, raw = _call(url, _headers(account),
                            payload={"redeem_request_id": key},
                            timeout=timeout, follow_redirects=False)
    except urllib.error.HTTPError as e:
        code = e.code
        e.close()
        if 300 <= code < 400:
            # A REDIRECT IS NOT AN ANSWER ABOUT THE REDEMPTION, and it is not
            # a refusal either: this request reached the vendor, and whether
            # anything behind that hop acted on it is exactly what helm cannot
            # know. So it is UNRESOLVED — the key is kept and the SAME request
            # is re-driven, rather than a second credit being put at risk on a
            # fresh one.
            return _consume_result(OUTCOME_UNKNOWN, key, http_status=code,
                                   note="the consume answered http_%d, a "
                                        "redirect this path never follows — "
                                        "the credit may or may not have been "
                                        "spent" % code)
        if code in (401, 403):
            return _consume_result(OUTCOME_UNAUTHORIZED, key, http_status=code,
                                   note="the credential was refused; no credit "
                                        "was spent")
        if code == 429:
            return _consume_result(OUTCOME_RATE_LIMITED, key, http_status=code,
                                   note="the vendor asked for a back-off; no "
                                        "credit was spent")
        if 400 <= code < 500:
            return _consume_result(OUTCOME_REFUSED, key, http_status=code,
                                   note=_redact("the consume was refused "
                                                "http_%d" % code, token, acct))
        # A 5xx IS NOT A REFUSAL. The vendor may have redeemed the credit and
        # then failed to say so, so the balance is unknown and the key is the
        # only safe way back in.
        return _consume_result(OUTCOME_UNKNOWN, key, http_status=code,
                               note=_redact("the consume answered http_%d — "
                                            "the credit may or may not have "
                                            "been spent" % code, token, acct))
    except Exception as e:                  # noqa: BLE001 — never a failure
        return _consume_result(OUTCOME_UNKNOWN, key,
                               note=_redact("the consume did not complete "
                                            "(%s) — the request may have "
                                            "reached the vendor"
                                            % e.__class__.__name__,
                                            token, acct))
    data = _body(raw)
    if data is None:
        return _consume_result(OUTCOME_MALFORMED, key, http_status=status,
                               note="the consume answered http_%d with a body "
                                    "helm could not read" % status)
    code = data.get("code")
    if code in VENDOR_CODES:
        return _consume_result(code, key, http_status=status)
    return _consume_result(OUTCOME_UNKNOWN_CODE, key, http_status=status,
                           note="the consume answered a code this helm does "
                                "not know — re-diff the upstream client")


def _consume_result(outcome, key, http_status=None, note=None):
    # `settled` SAYS THE VENDOR ANSWERED, NEVER THAT THE EFFECT IS KNOWN, and
    # the two are different questions for exactly one class: a 2xx helm could
    # not classify is an answer that ARRIVED and told helm nothing, so it is
    # settled AND its `spent` is None.
    spent = None if outcome in UNRESOLVED_OUTCOMES \
        else outcome in SPENT_OUTCOMES
    return {"outcome": outcome, "spent": spent,
            "settled": outcome != OUTCOME_UNKNOWN,
            "redeem_request_id": key, "http_status": http_status, "note": note}


# ---------------------------------------------------------------- the ledger

def ledger_path():
    """Beside proxywatch's watch state and the budget snapshot, under the helm
    home — so one env var isolates a suite from the real record, exactly as it
    isolates the budget snapshot.

    THE LEDGER IS HOST-LOCAL, AND SO IS THE MUTUAL EXCLUSION IT CARRIES. Two
    hosts each holding a copy of the same pool would keep two ledgers, neither
    visible to the other, and one wall could cost two credits with both hosts
    behaving correctly. Nothing here can see that, so the rung is bound the
    only way it can be: it acts only over the pool THIS host enumerates, and
    the pool lives on one host. A second host that starts holding these
    credentials needs a shared ledger before this rung may run there."""
    return os.path.join(home.helm_home(), home.GLOBAL, ".state", LEDGER_NAME)


def attempt_row(member, key, outcome, source, reused=False, note=None,
                now=None, label_=None):
    """One ledger row, or None when it carries no idempotency key.

    KEYED ON THE MEMBER DIGEST, LABELLED WITH THE DISPLAY NAME. `member` is
    `member_id` of the pooled credential, which is stable for as long as the
    credential is; `account` is whatever the surfaces print today, and a
    reader may use it to recognise a row but nothing may FILTER on it — a
    vendor body that omits the email renames the label and would hand the
    same credential a second, empty history.

    NO TOKEN AND NO ACCOUNT ID, as everywhere else in this module."""
    # `id` IS THE IDEMPOTENCY KEY, and it is not decoration: the event-ledger
    # grammar admits only rows carrying a non-empty id, so a row without one
    # is WRITTEN and then silently dropped by every read — a journal that
    # reports an empty history over a ledger full of attempts, which is the
    # one sentence that authorizes spending another credit. Keying on the
    # redeem_request_id also makes the ledger's own `latest` view answer the
    # right question: the last word about THIS key.
    keys = ([str(m) for m in member]
            if isinstance(member, (tuple, list)) else [str(member or "")])
    row = {"id": str(key or ""), "v": 1,
           "ts": float(time.time() if now is None else now),
           # EVERY SPELLING THE MEMBER'S HISTORY MAY BE ASKED UNDER rides on
           # the row, so a reader whose pair lost its user half — or gained
           # it back — still finds this attempt (task/2734). `member` stays
           # the first, most specific key for any reader that prints one.
           "member": keys[0], "members": keys,
           "account": str(label_ or keys[0] or ""),
           "redeem_request_id": str(key or ""),
           "outcome": str(outcome), "source": str(source),
           "reused_key": bool(reused)}
    if not row["id"] or not row["member"]:
        return None
    if note:
        row["note"] = str(note)
    return row


def record_attempt(member, key, outcome, source, reused=False, note=None,
                   now=None, path=None, label_=None):
    """Journal ONE attempt under its own lock. Durable, append-only,
    torn-tail repairing — the tree's own event-ledger primitive, not a second
    appender. `False` when the row could not be built or would not write."""
    row = attempt_row(member, key, outcome, source, reused=reused, note=note,
                      now=now, label_=label_)
    if row is None:
        return False
    return eventledger.append(path or ledger_path(), row)


def attempts(path=None):
    """(rows, unavailable reason). An unreadable ledger is UNKNOWN, never an
    empty history: "nothing has been attempted" is the one sentence that
    authorizes spending, and no failed read may speak it."""
    rows, err = eventledger.checked_events(path or ledger_path())
    if err:
        return None, err
    return sorted((r for r in rows if isinstance(r, dict)),
                  key=lambda r: r.get("ts") or 0), None


def attempts_for(rows, member):
    """This CREDENTIAL's attempts, oldest first, selected by the member digest
    and never by the display label. `None` in, `None` out — the
    unknown-ledger state survives the filter.

    `member` is one digest OR the tuple `codexhomes.member_digests` returns:
    the pair as the credential presents NOW plus, when the user id is known,
    the pair with the user id blanked — because a pooled record whose user
    claim stops being readable keeps its account id and CHANGES DIGEST, and a
    read keyed only on the new pair finds an empty history, consults no
    cool-down, and spends twice for one wall (task/2734, measured in the
    review probe). The blanked variant can only name rows written about this
    account under an UNKNOWN user — never a different known user, whose
    digest carries their own id — so matching it over-blocks at worst, which
    is the safe direction for an asset helm cannot un-spend.

    Rows written before this ledger carried a member key match nothing, and
    they need no migration: this ledger has never shipped, so on any host that
    runs it the file is either absent or already in this format."""
    if rows is None:
        return None
    keys = frozenset(member) if isinstance(member, (tuple, list)) \
        else frozenset((member,))
    mine = []
    for r in rows:
        row_keys = frozenset(r.get("members") or ()) | {r.get("member")}
        hit = keys.intersection(row_keys)
        if not hit:
            continue
        if hit == row_keys - {""}:
            mine.append(r)
            continue
        # A row carrying keys BEYOND the ones this member asks for was
        # written by a credential with a MORE COMPLETE identity — the
        # full-pair key on it names a specific known user, which either is
        # this member (then the pair key was among the asked) or is not.
        if keys - row_keys and row_keys - hit - {""}:
            continue
        mine.append(r)
    return mine


def cooldown_attempts(rows):
    """The attempts a cool-down may be taken on: the LAST WORD about each
    idempotency key, in the order the keys were driven.

    A WRITE-AHEAD ROW ITS OWN PASS THEN SETTLED IS NOT AN ATTEMPT OF ITS OWN.
    Every attempt journals `pending` before the request leaves and its outcome
    after, so a cool-down read row by row sees TWO attempts for one request —
    and the row it reads first names no outcome at all, so no exemption can
    ever apply to it. A row still reading `pending` with nothing after it is a
    different fact: nobody survived to resolve it, it is the last word about
    its key, and it still burns the cool-down."""
    last = {}
    for i, r in enumerate(rows or ()):
        last[str(r.get("redeem_request_id") or "")] = i
    return [r for i, r in enumerate(rows or ())
            if last.get(str(r.get("redeem_request_id") or "")) == i]


def refusal_run(rows, key):
    """How many times IN A ROW the vendor refused this credential on this
    idempotency key — the count that ends the cool-down exemption.

    THE WRITE-AHEAD ROW IS NOT AN ANSWER, so it neither counts nor breaks the
    run: every attempt journals `pending` before its request leaves, and a run
    read row by row would be broken by the very row announcing the next
    attempt in it. Any outcome the vendor DID name that is not a refusal sets
    the run back to zero, because what is being bounded is a credential that
    answers nothing but "refused"."""
    run = 0
    for r in rows or ():
        if str(r.get("redeem_request_id") or "") != str(key or ""):
            continue
        outcome = r.get("outcome")
        if outcome == OUTCOME_PENDING:
            continue
        run = run + 1 if outcome in COOLDOWN_EXEMPT_OUTCOMES else 0
    return run


def open_key(rows):
    """The idempotency key whose question is still OPEN — the newest key the
    VENDOR never answered about — or None when the next attempt must mint one.

    A KEY IS CLOSED BY A VENDOR CODE AND BY NOTHING ELSE. The four documented
    codes are the endpoint speaking about the redemption itself; every other
    row is either a request that never got that far (a refused credential, a
    back-off, another 4xx) or an answer helm could not classify. Re-driving an
    open key is FREE — it finds the credit already redeemed or redeems it once
    — and MINTING is the only act that can spend a second credit, so doubt is
    resolved in favour of the key already on disk.

    Only the NEWEST key is asked: an older key under a newer one was already
    superseded by an attempt this policy allowed, and reviving it would
    re-drive a question that is cool-downs old."""
    rows = list(rows or ())
    if not rows:
        return None
    key = str(rows[-1].get("redeem_request_id") or "")
    if not key:
        return None
    mine = [r for r in rows if str(r.get("redeem_request_id") or "") == key]
    if any(r.get("outcome") in VENDOR_CODES for r in mine):
        return None
    if not any(r.get("outcome") in UNRESOLVED_OUTCOMES for r in mine):
        return None
    return key


# -------------------------------------------------------------- the decision

CONSUME = "CONSUME"
NO_ACT = "NO-ACT"
#: The local state warrants a consume and the credit balance has not been read
#: yet. Splitting this out is what keeps the vendor listing off the hot path:
#: an account that is not exhausted never costs a round-trip.
NEED_CREDITS = "NEED-CREDITS"

R_NO_READING = "no-reading"
R_STALE_READING = "stale-reading"
R_UNKNOWN_READING = "unknown-reading"
R_NO_WEEKLY = "no-weekly-window"
R_NOT_EXHAUSTED = "weekly-not-exhausted"
R_NATURAL_RESET_NEAR = "natural-reset-near"
R_LEDGER_UNKNOWN = "ledger-unknown"
R_COOLDOWN = "cooldown"
R_CREDITS_UNREAD = "credits-unread"
R_JOURNAL_UNWRITABLE = "journal-unwritable"
R_PASS_BUDGET = "pass-budget"
R_RUNG_ERROR = "rung-error"
R_NO_CREDIT = "no-credit"
R_READY = "ready"
#: The wall is real and the vendor says it is not a rate-limit one.
R_CREDITS_DEPLETED = "credits-depleted"
#: The vendor names a wall reason helm does not recognise.
R_REACHED_UNRECOGNISED = "wall-reason-unrecognised"
#: The vendor names no reason at all and no 429 helm observed names one.
R_REACHED_UNKNOWN = "wall-reason-unknown"
#: No pooled credential serves this reading — nothing was ever sent.
R_NO_CREDENTIAL = "no-pooled-credential"
#: More than one pooled credential could serve it, and a redemption sent with
#: the wrong one spends a sibling's asset.
R_AMBIGUOUS_CREDENTIAL = "ambiguous-credential"
#: The attempt ledger's lock could not be taken, so two passes could not be
#: kept apart.
R_LEDGER_LOCKED = "ledger-locked"
#: Nobody supplied a vendor base url.
R_NO_BASE_URL = "no-base-url"
#: The pool could not be enumerated on this host.
R_NO_POOL = "pool-unread"

Decision = collections.namedtuple("Decision", "action reason detail reuse_key")


def weekly_window(reading):
    """The account-wide WEEKLY window of a `codexbudget` row, or None.

    Identified by LENGTH, never by position: `codexbudget.windows_of` already
    drops the scoped metered gauges and sorts what remains longest-first, and
    the vendor puts the 7d allowance in secondary_window on one plan shape and
    in the only window on another."""
    for w in (reading or {}).get("windows") or ():
        if (w.get("seconds") or 0) >= WEEKLY_MIN_SECONDS:
            return w
    return None


def _pct(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if value == value else None


def _epoch(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if value == value else None


def cooling_names_weekly(reading, cooling_reset_at, now,
                         tolerance_s=COOLING_MATCH_TOLERANCE_S):
    """Whether a 429 helm ALREADY OBSERVED for this credential is about this
    account's WEEKLY window, and about no other window of it.

    `cooling_reset_at` is the instant a proxy sidecar THAT LOADS THIS POOL
    says it will retry the credential (`proxywatch.codex_cooling_by_file`),
    which the sidecar set from the vendor's own `resets_in_seconds` when the
    vendor refused it 429. The scope is part of the evidence: the roster names
    a credential by its pool FILE, so a same-named file in a foreign proxy's
    auth dir is a different credential wearing this one's name.
    The vendor's usage answer independently gives each window's `reset_at`. If
    the two name one instant, the refusal was about that window.

    BOTH HALVES ARE LOAD-BEARING. Matching the weekly window says the 429 was
    a WEEKLY refusal; NOT matching any shorter window is what stops a 5h wall's
    429 — whose retry instant can drift within tolerance of a weekly reset
    that happens to be near — from being read as a weekly one. A belief that
    names two windows names neither.

    An elapsed belief is no evidence: a sidecar that will retry NOW is not
    holding this credential out of anything."""
    at = _epoch(cooling_reset_at)
    if at is None or at <= now:
        return False
    weekly = _epoch((weekly_window(reading) or {}).get("reset_at"))
    if weekly is None or abs(at - weekly) > tolerance_s:
        return False
    for w in (reading or {}).get("windows") or ():
        if (w.get("seconds") or 0) >= WEEKLY_MIN_SECONDS:
            continue
        other = _epoch(w.get("reset_at"))
        if other is not None and abs(at - other) <= tolerance_s:
            return False
    return True


def weekly_wall(reading, cooling_reset_at, now,
                near_wall_pct=WEEKLY_NEAR_WALL_PCT,
                tolerance_s=COOLING_MATCH_TOLERANCE_S):
    """How this account's weekly window is known to be spent, or None.

    TWO SIGNALS, EITHER SUFFICIENT, AND ONE OWNER FOR BOTH. `decide` asks this
    and so does the report row, because two spellings of "is it walled" is how
    a surface starts describing a decision the policy did not take.

      WALL_MEASURED  the usage endpoint reads the weekly window at 100% used.
      WALL_OBSERVED  the reading is at the wall's edge AND a 429 helm itself
                     recorded for this credential names this window — the
                     owner's own signal, because the percentage lags the
                     refusals and he learns of the wall by seeing a 429."""
    weekly = weekly_window(reading)
    used = _pct((weekly or {}).get("used_percent"))
    if used is None:
        return None
    if used >= WEEKLY_EXHAUSTED_PCT:
        return WALL_MEASURED
    if used >= near_wall_pct and cooling_names_weekly(
            reading, cooling_reset_at, now, tolerance_s):
        return WALL_OBSERVED
    return None


def decide(reading, reading_age_s, credits, account_attempts, now,
           credential_error, cooling_reset_at=None, max_age_s=None,
           cooldown_s=CONSUME_COOLDOWN_S,
           refusal_floor_s=REFUSAL_FLOOR_S,
           refusal_run_max=REFUSAL_RUN_MAX,
           reset_floor_s=NATURAL_RESET_FLOOR_S,
           near_wall_pct=WEEKLY_NEAR_WALL_PCT,
           tolerance_s=COOLING_MATCH_TOLERANCE_S):
    """Should helm spend a credit on this account right now, and why.

    PURE: no clock, no network, no disk — every input is an argument, so the
    whole policy is exercised without a vendor call.

      reading          a `codexbudget` budget row, or None
      reading_age_s    how old that reading is, or None for "unknown"
      credits          a `list_credits` result, or None for "not read yet"
      account_attempts this CREDENTIAL's ledger rows, or None for "unreadable"
      now              epoch seconds
      credential_error None when the reading is bound to EXACTLY ONE pooled
                       credential, else the reason it is not. REQUIRED, and
                       positional, because a caller that forgets to resolve
                       whose credential this is would otherwise spend one
                       member's asset on another member's wall.
      cooling_reset_at the instant a proxy sidecar that loads this pool says
                       it will retry THIS credential, or None for "no 429 on
                       record"

    The order of the gates is the order of their cost. Everything that can
    refuse from LOCAL state refuses before the vendor is consulted, so the
    listing is spent only on an account that is otherwise ready."""
    from . import codexbudget
    max_age_s = codexbudget.GATE_MAX_AGE_S if max_age_s is None else max_age_s
    if not reading:
        return Decision(NO_ACT, R_NO_READING,
                        "no budget reading for this account", None)
    if reading_age_s is None or reading_age_s > max_age_s:
        return Decision(NO_ACT, R_STALE_READING,
                        "the budget reading is %s, and a reading helm cannot "
                        "trust is not a measurement of zero"
                        % ("of unknown age" if reading_age_s is None
                           else "%dm old, past the %dm bound"
                                % (reading_age_s // 60, max_age_s // 60)), None)
    if reading.get("state") == "unknown" or reading.get("longest_pct") is None:
        return Decision(NO_ACT, R_UNKNOWN_READING,
                        "this account's budget could not be read (%s)"
                        % (reading.get("status") or "unknown"), None)
    weekly = weekly_window(reading)
    if weekly is None:
        return Decision(NO_ACT, R_NO_WEEKLY,
                        "this account reports no account-wide weekly window",
                        None)
    used = _pct(weekly.get("used_percent"))
    if used is None:
        return Decision(NO_ACT, R_UNKNOWN_READING,
                        "the weekly window carries no numeric percentage", None)
    # EITHER SIGNAL OF ZERO. The percentage is the measurement; the 429 helm
    # already observed for this credential is the correction for the lag
    # between the vendor refusing a call and the usage endpoint admitting it.
    wall = weekly_wall(reading, cooling_reset_at, now, near_wall_pct,
                       tolerance_s)
    if wall is None:
        # THE 5h WALL ALONE IS NEVER ENOUGH, and this is the line that says so:
        # the row's own `state` can read `exhausted` off a five-hour wall or a
        # reached-type while the week still has room, and a credit spent there
        # buys back an hour of a window that refills by itself.
        return Decision(NO_ACT, R_NOT_EXHAUSTED,
                        "the weekly window is %.0f%% used, not spent%s" % (
                            used,
                            "" if not _epoch(cooling_reset_at) else
                            ", and the 429 on record for this credential does "
                            "not name the weekly window"), None)
    # WHY IT IS WALLED DECIDES WHETHER A RESET CREDIT CAN HELP AT ALL. The
    # vendor says so on the row helm already parses, and a rate-limit reset
    # lifts a rate-limit wall and nothing else.
    reached = reading.get("reached_type")
    if reached in CREDITS_DEPLETED_TYPES:
        return Decision(NO_ACT, R_CREDITS_DEPLETED,
                        "this wall is %s — the workspace's CREDIT balance is "
                        "spent, not a rate-limit window, and a rate-limit "
                        "reset does not lift it" % reached, None)
    if reached is not None and reached != RATE_LIMIT_REACHED:
        return Decision(NO_ACT, R_REACHED_UNRECOGNISED,
                        "the vendor calls this wall %r, which this helm does "
                        "not recognise — re-diff the reached types before "
                        "spending a credit against it" % reached, None)
    if reached is None:
        # A WALL WITH NO STATED REASON IS NEVER AN AUTHORIZATION, AND THE 429
        # CANNOT STAND IN FOR ONE. That is measured, not assumed: the
        # credential rosters this rung reads carry the vendor's own 429 body,
        # and for every cooling pooled credential on this fleet — the
        # rate-limited ones AND the credits-depleted ones alike — that body
        # reads error.type `usage_limit_reached`. The refusal says WALLED; it
        # does not say WHY, and the two reasons differ in whether a reset
        # credit does anything at all. Only the usage endpoint's reached type
        # separates them, so a reading that carries none refuses — the credit
        # keeps, the wall does not, and the row says which.
        return Decision(NO_ACT, R_REACHED_UNKNOWN,
                        "the weekly window is spent and the vendor names no "
                        "reason for it; a 429 says this credential is walled "
                        "and never says whether a reset credit would lift it",
                        None)
    left = weekly.get("reset_after_seconds")
    if isinstance(left, (int, float)) and not isinstance(left, bool) \
            and left <= reset_floor_s:
        return Decision(NO_ACT, R_NATURAL_RESET_NEAR,
                        "the weekly window resets naturally in %dm, inside the "
                        "%dm floor — a credit would buy that much and no more"
                        % (left // 60, reset_floor_s // 60), None)
    if credential_error:
        # WHOSE CREDENTIAL IS THIS. A reading that cannot be bound to exactly
        # one pooled credential cannot be acted on at all: the listing would
        # report a sibling's balance as this account's, and the redemption
        # would spend the sibling's credit on a wall it does not have.
        return Decision(NO_ACT, credential_error,
                        "no single pooled credential serves this reading (%s),"
                        " so neither its balance nor its wall can be acted on"
                        % credential_error, None)
    if account_attempts is None:
        return Decision(NO_ACT, R_LEDGER_UNKNOWN,
                        "the attempt ledger could not be read, so helm cannot "
                        "prove this wall has not already been acted on", None)
    # ONE REQUEST IS ONE ATTEMPT, however many rows it left behind. The
    # ledger carries a write-ahead `pending` row and an outcome row for each
    # send, and a cool-down taken row by row can only ever see the pending
    # one first — which names no outcome, so no exemption reaches it.
    # AND AN EXEMPTION IS A WINDOW OF ITS OWN, never the absence of one: an
    # outcome released from the two hours is released from the only thing
    # bounding how often its request repeats, so each exempt row is asked for
    # the floor it does keep and for how long it has kept answering nothing
    # else. The window that refused rides beside the row, because the sentence
    # the owner reads has to name the bound he is actually waiting on.
    recent = []
    for r in cooldown_attempts(account_attempts):
        age = now - float(r.get("ts") or 0)
        if age > cooldown_s:
            continue
        if r.get("outcome") not in COOLDOWN_EXEMPT_OUTCOMES:
            recent.append((r, cooldown_s, "cool-down"))
        elif refusal_run(account_attempts,
                         r.get("redeem_request_id")) >= refusal_run_max \
                and age >= refusal_floor_s:
            # THE BINDING BOUND IS NAMED, AND THE FLOOR STILL COMES FIRST IN
            # TIME. A standing refusal falls back to the two-hour cool-down
            # for as long as it stands — but only once the fifteen-minute
            # floor under the last attempt has run out; inside the floor the
            # floor is the bound, and the chain must say so.
            recent.append((r, cooldown_s,
                           "cool-down a standing refusal falls back to"))
        elif age < refusal_floor_s:
            recent.append((r, refusal_floor_s,
                           "floor one pass keeps under a refused credential"))
    if recent:
        last, window_s, window = recent[-1]
        return Decision(NO_ACT, R_COOLDOWN,
                        "an attempt %dm ago ended %s, inside the %dm %s"
                        % ((now - float(last.get("ts") or 0)) // 60,
                           last.get("outcome"), window_s // 60, window), None)
    # THE KEY IS DECIDED HERE AND MINTED BY THE CALLER. An attempt the vendor
    # never answered about — a 5xx, a timeout, a 2xx helm could not classify,
    # or one nobody survived to resolve — may already have spent a credit, so
    # the only safe next request is the SAME request: re-driving its key
    # either finds the credit already redeemed or redeems it once. `open_key`
    # owns which key that is, and a key a VENDOR CODE answered is closed.
    reuse = open_key(account_attempts)
    if credits is None:
        return Decision(NEED_CREDITS, R_READY,
                        "the weekly window is spent and nothing local refuses "
                        "— read the credit balance", reuse)
    if credits.get("status") != LIST_OK:
        return Decision(NO_ACT, R_CREDITS_UNREAD,
                        "the credit balance could not be read (%s)"
                        % credits.get("status"), reuse)
    if not (credits.get("spendable") or 0) > 0:
        return Decision(NO_ACT, R_NO_CREDIT,
                        "the weekly window is spent and this account has no "
                        "spendable reset credit (%s available by count, %s "
                        "spendable)" % (credits.get("available"),
                                        credits.get("spendable")), reuse)
    return Decision(CONSUME, R_READY,
                    "the weekly window is spent (%s), %d credit(s) spendable%s"
                    % ("measured at %.0f%% used" % used if wall == WALL_MEASURED
                       else "%.0f%% used and refused 429 for this window" % used,
                       credits["spendable"],
                       "" if reuse is None
                       else ", re-driving the key of an attempt the vendor "
                            "never answered about"), reuse)


# ------------------------------------------------------------------ the rung

def _pool_accounts():
    """(every pooled codex identity WITH its live token, measured?) through
    the budget module's own census — one enumeration, one identity rule, no
    second reader. `measured` False means this host could not read the pool,
    which is never "the pool is empty" and never an authorization to act."""
    from . import codexbudget
    accounts, census = codexbudget.pool_census()
    measured = census == codexbudget.CENSUS_MEASURED
    return (accounts if measured else []), measured


def safe_error(exc, accounts=None):
    """An exception rendered for a log line, with every pooled secret blinded.

    THE LAST STRING IN THIS FEATURE THAT IS BUILT FROM INPUT NOBODY READ. An
    exception raised anywhere under a vendor client can carry the request url,
    the headers, or the token itself, and the rung that prints it has no idea
    what is in the message. So the message is scrubbed against every pooled
    credential's own secrets — and if the pool cannot be read to learn what
    those are, the message is DROPPED and only the class name survives, which
    is the answer that cannot leak."""
    name = exc.__class__.__name__
    try:
        rows = accounts if accounts is not None else _pool_accounts()[0]
        secrets = [v for a in rows or () for v in _secrets(a) if v]
    except Exception:                       # noqa: BLE001 — never a failure
        return name
    if not secrets:
        # NOTHING KNOWN TO BLIND IS NOT THE SAME AS NOTHING TO HIDE: an empty
        # pool means the scrub has no needles, not that the message is clean.
        return name
    return "%s: %s" % (name, _redact(exc, *secrets))


def reset_pass(budget_rows, reading_age_s=0.0, now=None, url_base=None,
               timeout=None, accounts=None, ledger=None, probe=None,
               source="auto", only=None, dry_run=False, cooling=None):
    """Decide and, unless `dry_run`, act for every account in `budget_rows`.

    Returns one row per account — the decision, its reason, and the outcome
    when an attempt was made. NEVER RAISES for one account's sake: a vendor
    failure on one credential must not cost the pass its report on the others.

    `url_base` is REQUIRED for anything to be sent; without one every row
    refuses, because the live vendor is named by the production doors and
    never inherited as a default.

    `cooling` is {pool file: retry instant} from
    `proxywatch.codex_cooling_by_file` — the 429s helm has already observed
    per credential, taken ONLY from the sidecars that load this pool. Absent,
    the policy runs on the measured percentage alone.

    `probe` re-reads an account after a successful reset so the room line can
    carry the before and the after; it is `codexbudget.probe_record` by
    default and is read-only.

    THE MUTUAL EXCLUSION THIS HOLDS, AND THE ONE IT DOES NOT. Within this
    host, the ledger lock spans re-read -> cool-down -> write-ahead append, so
    two overlapping passes (a slow timed pass and its successor, or the manual
    door typed beside the rung) cannot both decide one wall is unattempted.
    ACROSS HOSTS it holds nothing: the ledger lives under this host's helm
    home and two hosts share none, so the rung acts only over the pool THIS
    host enumerates — see `ledger_path`."""
    now = time.time() if now is None else now
    ledger = ledger or ledger_path()
    if accounts is None:
        accounts, measured = _pool_accounts()
    else:
        measured = True
    if not url_base:
        pass_error = (R_NO_BASE_URL,
                      "no vendor base url was supplied to this pass, so "
                      "nothing is read and nothing is sent")
    elif not measured:
        pass_error = (R_NO_POOL,
                      "this host could not enumerate the codex pool, and an "
                      "unread pool is never an account helm may spend from")
    else:
        pass_error = None
    rows, history_err = attempts(ledger)
    out, sent = [], 0
    for reading in budget_rows or ():
        try:
            sent += _one_account(reading, out, rows, reading_age_s, now,
                                 url_base, timeout, accounts, ledger, probe,
                                 source, only, dry_run, cooling, pass_error,
                                 budget_left=MAX_CONSUMES_PER_PASS - sent)
        except Exception as e:                  # noqa: BLE001 — see below
            # ONE CREDENTIAL'S FAILURE IS ONE ROW. The pass reports on a pool,
            # and a reading helm could not even decide about must not take the
            # other accounts' answers down with it — including the account
            # that was about to have its wall cleared.
            # The name is taken defensively: `label` is one of the calls that
            # can raise here, and a handler that re-raises the exception it
            # exists to contain would lose the whole pass anyway.
            out.append(dict(_blank_row(label(reading)
                                       if isinstance(reading, dict) else "?"),
                            action=NO_ACT, reason=R_RUNG_ERROR,
                            detail="this account could not be decided (%s)"
                                   % e.__class__.__name__))
    if history_err:
        for row in out:
            row["ledger_error"] = history_err
    return out


def _blank_row(name):
    """One account's report row before anything has been decided about it."""
    return {"account": name, "weekly_pct": None, "reset_in_s": None,
            "available": None, "spendable": None, "spendable_after": None,
            "outcome": None, "spent": None, "key": None, "reused_key": False,
            "after_weekly_pct": None, "note": None, "wall": None}


def _one_account(reading, out, rows, reading_age_s, now, url_base, timeout,
                 accounts, ledger, probe, source, only, dry_run, cooling,
                 pass_error, budget_left):
    """One account's leg of `reset_pass`: appends at most one row to `out` and
    answers how many redemptions it spent (0 or 1)."""
    name = label(reading)
    if only is not None and name != only:
        return 0
    row = _blank_row(name)
    weekly = weekly_window(reading)
    if weekly:
        row["weekly_pct"] = weekly.get("used_percent")
        row["reset_in_s"] = weekly.get("reset_after_seconds")
    if pass_error:
        row["action"], row["reason"], row["detail"] = \
            NO_ACT, pass_error[0], pass_error[1]
        out.append(row)
        return 0
    # WHOSE CREDENTIAL THIS IS, RESOLVED BEFORE ANYTHING IS READ OR SENT. It
    # costs nothing (the census is already in hand) and everything downstream
    # depends on it: the ledger is keyed on this credential, the listing is
    # sent with it, and the redemption spends ITS credit. An account id alone
    # is a WORKSPACE on a Team plan, so the reading is bound to one MEMBER or
    # to none.
    account, credential_error = resolve_credential(accounts, reading)
    # WRITE the digest of the pair as it stands NOW; READ with both spellings
    # (the tuple), so history written while the user claim was unreadable is
    # still this member's (task/2734). The two are different shapes of one
    # identity on purpose: the journal's `member` column is one digest.
    member_write = member_digests_for(account) if account else None
    member = member_write
    cool_at = _cooling_for(cooling, account)
    row["wall"] = weekly_wall(reading, cool_at, now)
    mine = attempts_for(rows, member) if member else []
    listing = None
    # THE FIRST DECISION IS TAKEN WITH NO CREDIT BALANCE IN HAND, and it
    # is what makes a consume unreachable until the vendor has been asked:
    # `decide` can only answer CONSUME once it has a listing, so the
    # balance is read on the NEED-CREDITS leg below, and nowhere else.
    decision = decide(reading, reading_age_s, None, mine, now,
                      credential_error, cooling_reset_at=cool_at)
    if decision.action == NEED_CREDITS:
        # THE LISTING GOES OUT UNDER THE RESOLVED CREDENTIAL, so "a credit is
        # available" is a fact about the walled member and about nobody else.
        listing = list_credits(account, url_base, timeout=timeout)
        row["available"] = listing.get("available")
        row["spendable"] = listing.get("spendable")
        row["note"] = listing.get("note")
        decision = decide(reading, reading_age_s, listing, mine, now,
                          credential_error, cooling_reset_at=cool_at)
    if decision.action == CONSUME and budget_left <= 0:
        # THE POOL-WIDE BUDGET, applied after the per-account decision so
        # the row still says the account WAS ready — an operator reading
        # this must see a deferral, never a reading that looks healthy.
        decision = Decision(NO_ACT, R_PASS_BUDGET,
                            "ready, and this pass has already spent its "
                            "one redemption — the next pass takes this "
                            "account up", decision.reuse_key)
    row["action"] = decision.action
    row["reason"] = decision.reason
    row["detail"] = decision.detail
    if decision.action != CONSUME or dry_run:
        out.append(row)
        # A DRY RUN PREDICTS THE CAP TOO. It answers what the pass WOULD
        # do, and what the pass would do is take one wall and defer the
        # rest — a dry run showing three CONSUME rows would describe a
        # burst that cannot happen.
        return 1 if decision.action == CONSUME else 0
    # THE LEDGER LOCK SPANS RE-READ -> COOL-DOWN -> WRITE-AHEAD APPEND, and
    # that span is the whole point. The history above was read at the top of
    # the pass, and between then and now a SECOND pass — the successor of a
    # long one, or the manual door typed while this rung runs — can have
    # decided the same wall was unattempted and spent for it. Under the lock
    # this leg re-asks the ledger and re-runs the cool-down, so the second
    # pass to arrive sees the first's write-ahead row and refuses. Measured:
    # without it, two overlapping passes send two consumes for one wall.
    with eventledger.locked(ledger) as held:
        if not held:
            row["action"], row["reason"] = NO_ACT, R_LEDGER_LOCKED
            row["detail"] = ("the attempt ledger's lock could not be taken, "
                             "so helm cannot prove a second pass is not "
                             "spending for this same wall")
            out.append(row)
            return 0
        fresh, fresh_err = attempts(ledger)
        final = decide(reading, reading_age_s, listing,
                       attempts_for(fresh, member) if fresh_err is None
                       else None, now, credential_error,
                       cooling_reset_at=cool_at)
        if final.action != CONSUME:
            row["action"], row["reason"] = final.action, final.reason
            row["detail"] = final.detail
            out.append(row)
            return 0
        key = final.reuse_key or mint_key()
        row["key"], row["reused_key"] = key, final.reuse_key is not None
        # THE KEY IS ON DISK BEFORE THE REQUEST LEAVES, and a journal that
        # would not take it STOPS THE SEND. The ledger is the only thing that
        # keeps one wall to one credit, so acting while it cannot record the
        # act is how a crash — or a full disk — turns into a second
        # redemption nobody can account for.
        ahead = attempt_row(member_write, key, OUTCOME_PENDING, source,
                            reused=row["reused_key"], now=now, label_=name)
        if ahead is None or not eventledger.append_unlocked(ledger, ahead):
            row["action"] = NO_ACT
            row["reason"] = R_JOURNAL_UNWRITABLE
            row["detail"] = ("the attempt ledger would not accept this "
                             "attempt, and helm does not spend a credit it "
                             "cannot record")
            row["key"] = None
            out.append(row)
            return 0
    # THE VENDOR CALL IS OUTSIDE THE LOCK. It can take the full timeout, and
    # holding a file lock across a network round-trip would make one slow
    # vendor stall every other door on this ledger for as long as it lasts.
    # The write-ahead row already holds the wall.
    result = consume(account, key, url_base, timeout=timeout)
    row["outcome"] = result["outcome"]
    row["spent"] = result.get("spent")
    row["note"] = result.get("note") or row["note"]
    record_attempt(member_write, key, result["outcome"], source,
                   reused=row["reused_key"], note=result.get("note"),
                   now=now, path=ledger, label_=name)
    if result["outcome"] == OUTCOME_RESET:
        row["after_weekly_pct"], row["spendable_after"] = _after(
            account, probe, url_base, timeout)
    out.append(row)
    return 1


def _cooling_for(cooling, account):
    """The 429 on record for THIS credential: the latest retry instant any
    sidecar holds for any spelling of its pool file, or None.

    Bound to the RESOLVED credential rather than to the reading, so the
    evidence and the credential a redemption would be sent with are the same
    thing by construction."""
    if not cooling or not account:
        return None
    seen = [cooling[f] for f in (account.get("files") or ())
            if f in cooling]
    return max(seen) if seen else None


def _after(account, probe, url_base, timeout=None):
    """(weekly percent, credits left) AFTER a redemption, both MEASURED.

    The percentage comes back through the budget module's own probe and the
    balance through a second read-only listing. Neither is derived: a room
    line that announced "1 left" by subtracting one from the count it read
    BEFORE the spend would be arithmetic wearing a measurement's clothes, and
    the one surface that reports an irreversible act is the last place for
    that. A read that fails answers None and the line says the balance is
    unread — a confirmation that fails tells the room nothing and must never
    turn a successful redemption into an error."""
    from . import codexbudget
    probe = probe or codexbudget.probe_record
    pct = None
    try:
        weekly = weekly_window(probe(account))
        pct = weekly.get("used_percent") if weekly else None
    except Exception:                       # noqa: BLE001 — never a failure
        pct = None
    try:
        listing = list_credits(account, url_base, timeout=timeout)
        left = listing.get("spendable") if listing.get("status") == LIST_OK \
            else None
    except Exception:                       # noqa: BLE001 — never a failure
        left = None
    return pct, left


def acted(rows):
    """The rows where helm made a vendor attempt."""
    return [r for r in rows or () if r.get("outcome")]


def wanted_and_could_not(rows):
    """The rows where the weekly wall is real, helm wanted to clear it, and
    could not. A routine NO-ACT — a healthy window, a cool-down, a natural
    reset minutes away — is deliberately NOT in here: those are the policy
    working, and a room line for each would be noise."""
    # A pass-budget deferral is NOT in here: the wall is real and helm chose
    # to take it next pass, which is the policy working rather than a failure
    # to act. It rides the report lines, not the room.
    # THE TWO SYSTEMIC REASONS ARE, and for the owner's own acceptance test:
    # he stops watching, so SILENCE HAS TO MEAN HEALTHY. A pass with no vendor
    # base and a pass over a pool this host cannot enumerate can act on
    # NOTHING, for a reason no wall of his will ever cure, and an inert rung
    # that says nothing is indistinguishable from a quiet working one — the
    # single state he cannot detect by not looking. They are latched like
    # every other blocked wall, so each is said once and not every pass.
    blocked = (R_NO_CREDIT, R_CREDITS_UNREAD, R_LEDGER_UNKNOWN,
               R_JOURNAL_UNWRITABLE, R_RUNG_ERROR, R_LEDGER_LOCKED,
               R_NO_CREDENTIAL, R_AMBIGUOUS_CREDENTIAL, R_CREDITS_DEPLETED,
               R_REACHED_UNRECOGNISED, R_REACHED_UNKNOWN,
               R_NO_BASE_URL, R_NO_POOL)
    return [r for r in rows or ()
            if not r.get("outcome") and r.get("reason") in blocked]


def pass_notice(rows):
    """ONE room line for the pass, or None. Posted when helm spent a credit,
    when an attempt did not end in a reset, and when it wanted to act and
    could not — never for an ordinary quiet pass.

    THE RENDERER, NOT THE DECISION TO SPEAK: a blocked wall stands for days
    and this returns its line on every pass of them. `watch_notice` is what
    the room goes through, and it latches."""
    made = acted(rows)
    blocked = wanted_and_could_not(rows)
    if not made and not blocked:
        return None
    lines = ["%s codex: %d attempt(s), %d blocked wall(s)."
             % (NOTICE_TAG, len(made), len(blocked))]
    lines.extend(result_lines(made + blocked))
    return "\n".join(lines)


def latched_rows(rows):
    """The rows the room hears ONCE PER STATE rather than once per event:
    every wall helm wanted and could not take, and every attempt whose outcome
    is a standing property of the credential rather than an event
    (`LATCHED_OUTCOMES` — the outcomes the cool-down exempts, and therefore
    the only ones a pass can repeat every fifteen minutes)."""
    return wanted_and_could_not(rows) + [
        r for r in rows or () if r.get("outcome") in LATCHED_OUTCOMES]


def latch_reason(r):
    """Why a latched row is in the latch: the outcome when the vendor answered
    at all, else the reason this pass refused. One grammar over both halves,
    so a credential that starts being refused is a CHANGE of state and says so
    once, and a credential that keeps being refused is the same state."""
    return str(r.get("outcome") or r.get("reason") or "?")


def blocked_digest(rows):
    """A fingerprint of the states helm can only report — the walls it WANTED
    AND COULD NOT TAKE, and the credentials the vendor REFUSES — or "" when
    there are none. The room's dedup is taken on this and on nothing else.

    WHICH ACCOUNT AND WHY, AND NOTHING THAT MOVES BY ITSELF. A percentage
    changes every pass by construction, so a digest over the numbers would
    post every pass and say the same thing; what a reader needs once is that
    a wall helm cannot clear APPEARED, and again when a different one does."""
    # THE LATCH KEYS ON THE MEMBER, NEVER THE LABEL. The label is
    # email-first and the vendor's body supplies the email, so a credential
    # whose address changed re-posted a state the room had already heard —
    # the digest of the member's identity is stable across exactly that
    # change, which is the whole reason the ledger is keyed on it. A row
    # carrying no member (an unreadable pool file's row) falls back to its
    # account label, which is all the identity it has.
    return _pairs_digest(blocked_pairs(rows))


def blocked_pairs(rows):
    """The (member, reason) pairs the room hears ONCE PER STATE, sorted —
    the latch's CONTENT, kept as pairs rather than only a digest so a pass
    that probed nothing about one credential can carry that credential's
    pair forward instead of forgetting it."""
    return sorted({(str(r.get("member") or r.get("account") or "?"),
                    latch_reason(r)) for r in latched_rows(rows)})


def _pairs_digest(pairs):
    if not pairs:
        return ""
    raw = "\x00".join("%s\x1f%s" % tuple(p) for p in pairs)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def watch_notice(rows, prior_blocked=None):
    """(the room post to make THIS pass or None, the blocked latch to keep).

    AN EVENT SPEAKS EVERY TIME IT HAPPENS; A STATE SPEAKS ONCE, and the room
    needs them on different terms. An attempt that moved the balance or may
    have — a credit left the owner's account, or a request that might have
    spent one did not come back — is an EVENT, and the cool-down is what
    bounds how often one can happen. A wall helm cannot clear is a STATE, and
    SO IS A CREDENTIAL THE VENDOR REFUSES: the cool-down exempts a 401/403 so
    a wall need not wait two hours after a token is refreshed, and an
    exemption from the cool-down is an exemption from the only bound on how
    often that line can post. Measured through the real pass: a credential the
    listing accepts and the redemption refuses spoke on every one of eight
    passes. Both states are latched on (account, state) — measured on this
    fleet, four accounts sit blocked at a spent week for days at a stretch
    (two whose wall is a credit balance a reset does not lift, two with no
    credit left to spend), and the pass that decides this runs every fifteen
    minutes, so an unlatched line would post the same four sentences a hundred
    times a day and bury the one pass where something actually happened.

    The latch is `codexbudget.watch_notice`'s shape, for the same reason it
    has one. A pass that could not run the rung at all returns nothing here
    and the caller carries the prior latch forward rather than clearing it."""
    body = pass_notice(rows)
    # THE LATCH IS A SET OF STATES, AND A PASS THAT DID NOT ASK IS NOT A PASS
    # THAT FORGETS. The cool-down keeps a refused credential OUT of this
    # pass's rows entirely, so a latch taken only on what this pass saw would
    # clear the pair, and the state would re-post when the refusal returned —
    # measured: the standing-refusal line spoke once per cool-down CYCLE
    # instead of once per state. The latch is therefore the PAIRS, carried
    # forward: a prior pair whose member appears in NO row this pass is
    # kept, because nothing measured its leaving; one whose member WAS
    # probed and no longer latches has genuinely changed state and drops.
    pairs = blocked_pairs(rows)
    if isinstance(prior_blocked, (list, tuple)):
        seen = {str(r.get("member") or r.get("account") or "?")
                for r in rows or ()}
        carried = [p for p in prior_blocked
                   if p[0] not in seen and tuple(p) not in pairs]
        if carried:
            pairs = sorted(set(pairs) | {tuple(p) for p in carried})
    blocked = _pairs_digest(pairs)
    if body is None:
        return None, pairs
    # THE EVENTS IN THIS PASS, which is the acted rows MINUS the ones whose
    # outcome is a latched state: an attempt that is only the vendor refusing
    # the credential again is not news, and it is the one attempt that can
    # arrive every pass.
    events = [r for r in acted(rows)
              if r.get("outcome") not in LATCHED_OUTCOMES]
    prior_fp = (prior_blocked if isinstance(prior_blocked, str)
                else _pairs_digest(sorted(tuple(p) for p in prior_blocked))
                if prior_blocked else "")
    if not events and blocked and blocked == prior_fp:
        return None, pairs
    return body, pairs


def _used(pct):
    return "?" if pct is None else "%.0f%% used" % pct


def result_line(r):
    """ONE ROW, IN OUTCOME WORDS. The reader of this line is the owner, being
    told what just happened to a scarce thing of his, so it says which account,
    what happened to its weekly window, whether a credit left the balance, and
    what is LEFT — the balance AFTER the spend, measured, never the one the
    decision was taken on. A row that wanted to act and could not says why in
    the same grammar."""
    name = (r.get("account") or "?")[:30]
    outcome, spent = r.get("outcome"), r.get("spent")
    if outcome == OUTCOME_RESET:
        # THE BALANCE CLAUSE IS A SENTENCE EITHER WAY. The after-balance is a
        # second listing that can fail on its own, and "1 credit spent,
        # balance unread left" is not English — the one line that announces an
        # irreversible act is the last place to make a reader parse a
        # template.
        return ("    %-30s weekly window RESET (%s -> %s); 1 credit spent, %s"
                % (name, _used(r.get("weekly_pct")),
                   _used(r.get("after_weekly_pct")),
                   "and the balance left could not be read"
                   if r.get("spendable_after") is None
                   else "%d left" % r["spendable_after"]))
    if outcome:
        # AN UNRESOLVED OUTCOME STATES NEITHER FACT IT DOES NOT HAVE: not that
        # a credit was spent, and not that none was. It also says what helm
        # does next, because "unknown" without that reads as an abandoned
        # credit when the truth is that the SAME request is re-driven.
        moved = ("a credit was spent" if spent
                 else "no credit was spent" if spent is False
                 else "WHETHER A CREDIT WAS SPENT IS UNKNOWN, and the same "
                      "request is retried rather than a new one")
        return ("    %-30s weekly %s NOT reset (%s); %s%s"
                % (name, _used(r.get("weekly_pct")), outcome, moved,
                   "" if not r.get("note") else " — %s" % r["note"]))
    return ("    %-30s weekly %s, no credit spent (%s); %s"
            % (name, _used(r.get("weekly_pct")), r.get("reason") or "?",
               r.get("detail") or "no decision"))


def result_lines(rows):
    """One line per row, account order."""
    return [result_line(r)
            for r in sorted(rows or (), key=lambda r: r.get("account") or "")]


def pass_lines(rows):
    """The proxywatch report section. Silence means the rung decided nothing
    was to be done, which is the ordinary state."""
    interesting = acted(rows) + wanted_and_could_not(rows)
    if not interesting:
        return []
    return ["  codex reset credits:"] + result_lines(interesting)


# --------------------------------------------------------------------- the CLI

def _reset_in(seconds):
    if seconds is None:
        return "?"
    if seconds < 3600:
        return "%dm" % (seconds // 60)
    return "%.1fh" % (seconds / 3600.0)


def _listing_rows(url_base, timeout=None, now=None):
    """(rows, note) — every pooled account with its weekly reading and its
    credit balance. One vendor listing per account; this is the verb whose
    whole job is the balance, so it reads it for all of them."""
    from . import codexbudget
    now = time.time() if now is None else now
    readings, age = codexbudget.cached_budget(now=now)
    note = None
    if readings is None:
        readings, age = codexbudget.pool_budget(now=now, write_cache=False), 0.0
        note = ("no fresh pool-budget snapshot, so this reading was probed "
                "now; `helm proxywatch --post` is the snapshot's writer")
    accounts, measured = _pool_accounts()
    if not measured:
        note = ("this host could not enumerate the codex pool, so no balance "
                "below was read" if note is None else note)
    out = []
    for reading in readings or ():
        name = label(reading)
        # THE SAME BINDING THE ACTING PATH USES. A balance read under a
        # sibling's credential is a true number about the wrong account, and
        # this table is where an operator decides whether to spend.
        account, credential_error = resolve_credential(accounts, reading)
        listing = list_credits(account, url_base, timeout=timeout) \
            if account \
            else _list_result(LIST_NO_CREDENTIAL,
                              note="no single pooled credential serves this "
                                   "account (%s)" % credential_error)
        weekly = weekly_window(reading)
        out.append({"account": name,
                    # the MEMBER DIGEST rides beside the label so the room
                    # latch keys on the identity an email change cannot move
                    "member": member_id(account) if account else "",
                    "state": reading.get("state"),
                    "weekly_pct": weekly.get("used_percent") if weekly else None,
                    "reset_in_s": weekly.get("reset_after_seconds")
                    if weekly else None,
                    "available": listing.get("available"),
                    "spendable": listing.get("spendable"),
                    "total_earned": listing.get("total_earned"),
                    "expires": [c.get("expires_at") for c in listing["credits"]],
                    "status": listing.get("status"),
                    "note": listing.get("note")})
    return out, note, readings, age


def cmd_resets(args):
    """codex resets [--dry-run] [--consume <account>] [--json] — the earned
    rate-limit reset credits: what each pooled account holds, what the
    automatic policy would do with them right now, and the explicit manual
    door for spending one."""
    from .cli import guard_tail
    args = list(args)
    rc = guard_tail("helm codex resets", args, flags=("--dry-run", "--json"),
                    valued=("--consume",),
                    usage="codex resets [--dry-run] [--consume <account>] "
                          "[--json]")
    if rc is not None:
        return rc
    as_json = "--json" in args
    # `guard_tail` has already refused a `--consume` carrying no value, which
    # is the whole reason this door cannot pick an account for you.
    target = args[args.index("--consume") + 1] if "--consume" in args else None
    if target:
        return _run_consume(target, as_json=as_json)
    if "--dry-run" in args:
        return _run_dry_run(as_json=as_json)
    return _run_list(as_json=as_json)


#: The listing table's columns, one definition read by the renderer and by
#: the doc-parity arm, so the sample block in docs/VERBS.md cannot drift from
#: what the verb prints.
LIST_COLUMNS = ("account", "credits", "weekly", "natural", "balance read")
LIST_ROW_FMT = "  %-30s %-9s %-14s %-9s %s"


def list_header():
    return LIST_ROW_FMT % LIST_COLUMNS


def list_line(r):
    """One listing row. The CREDITS column is the SPENDABLE count, never the
    vendor's raw `available_count`: an entry already redeemed, or one this
    plan does not honour, is not a credit an operator can spend today. The
    last column says whether the BALANCE COULD BE READ AT ALL — it is a read
    status, not a number, and naming it `balance` made a status read as one."""
    return LIST_ROW_FMT % (
        r["account"][:30],
        "?" if r["spendable"] is None else str(r["spendable"]),
        "-" if r["weekly_pct"] is None else "%.0f%% used" % r["weekly_pct"],
        _reset_in(r["reset_in_s"]),
        r["status"] + ("" if not r["note"] else " — " + r["note"]))


def dry_run_line(r):
    return "  %-30s %-13s %-24s %s" % (r["account"][:30], r["action"],
                                       r["reason"], r["detail"])


def _run_list(as_json=False):
    import sys
    rows, note, _readings, _age = _listing_rows(live_base_url())
    if as_json:
        print(json.dumps({"rows": rows, "note": note}, indent=2, sort_keys=True))
        return 0
    if not rows:
        print("helm codex resets: no pooled codex account was read — "
              "`helm codex pooled` says what the pool holds")
        return 1
    if note:
        print("helm codex resets: " + note, file=sys.stderr)
    print("helm codex resets (%d account%s):"
          % (len(rows), "s"[:len(rows) != 1]))
    print(list_header())
    for r in sorted(rows, key=lambda r: r["account"]):
        print(list_line(r))
    return 0


#: NOTHING IS SPENT IS NOT NOTHING IS SENT. A dry run still asks the vendor
#: for the balance of every account the local gates did not refuse, because
#: "is there a credit to spend" is a fact only the vendor holds. The banner
#: says so rather than letting a reader infer an offline run — and it is a
#: constant so an arm can hold the sentence against the measured behaviour
#: instead of against itself.
DRY_RUN_BANNER = ("helm codex resets --dry-run (reading %dm old; NOTHING IS "
                  "SPENT — a read-only credit listing IS sent for each "
                  "account nothing local refuses):")


def _cooling_now():
    """The 429s helm has on record per pooled credential, read through
    proxywatch's sidecar roster — or None when that read is not available.

    NOT A NEW PROBE: it is the same management call the stale-cooldown rung
    already makes every pass, asked here so the CLI's answer matches the
    unattended rung's. None means "no 429 evidence this run", which can only
    make the policy refuse more.

    SCOPED TO THE SIDECARS THAT LOAD THE POOL, because the roster names a
    credential by its FILE and nothing else: a same-named file in any other
    proxy's auth dir would otherwise supply evidence for a pooled credential
    it has no relation to (`proxywatch.pool_sidecar_seats`)."""
    try:
        from . import proxywatch
        return proxywatch.codex_cooling_by_file(
            proxywatch.sidecar_rosters(), proxywatch.pool_sidecar_seats())
    except Exception:                       # noqa: BLE001 — never a failure
        return None


def _run_dry_run(as_json=False):
    """What the automatic rung WOULD do this instant, per account, and why —
    the same `decide` the proxywatch rung calls, with the acting leg off."""
    from . import codexbudget
    now = time.time()
    readings, age = codexbudget.cached_budget(now=now)
    if readings is None:
        readings, age = codexbudget.pool_budget(now=now, write_cache=False), 0.0
    rows = reset_pass(readings, reading_age_s=age, now=now, dry_run=True,
                      url_base=live_base_url(), cooling=_cooling_now())
    if as_json:
        print(json.dumps({"rows": rows, "reading_age_s": age}, indent=2,
                         sort_keys=True))
        return 0
    print(DRY_RUN_BANNER % ((age or 0) // 60))
    for r in sorted(rows, key=lambda r: r["account"]):
        print(dry_run_line(r))
    return 0


def _run_consume(target, as_json=False):
    """The explicit manual door. It requires the account by name, it prints
    the automatic policy's own verdict beside what it is about to do, and it
    re-drives a recorded UNKNOWN key rather than minting a second one."""
    import sys
    from . import codexbudget
    now = time.time()
    readings, age = codexbudget.cached_budget(now=now)
    if readings is None:
        readings, age = codexbudget.pool_budget(now=now, write_cache=False), 0.0
    names = sorted({label(r) for r in readings or ()})
    if target not in names:
        print("helm codex resets: no pooled codex account named %r (%s)"
              % (target, ", ".join(names) or "the pool read nothing"),
              file=sys.stderr)
        return 2
    # A DISPLAY NAME IS NOT AN IDENTITY, AND THIS DOOR SPENDS A CREDIT. The
    # label falls back email -> truncated account id -> pool file, and on a
    # Team plan the account id is the WORKSPACE id, so two members of one
    # workspace whose readings carry no email render the SAME name. Acting on
    # "whichever matched first" would spend the credit of the sibling the
    # owner did not mean. The candidates are named by their masked file
    # handles rather than guessed between.
    candidates = [r for r in readings or () if label(r) == target]
    if len(candidates) > 1:
        print("helm codex resets: %r names %d pooled codex accounts (%s) — "
              "refusing to guess which one. They share a display name because "
              "their readings carry no email and their account id is one "
              "workspace's; `helm codex list` shows the pooled credentials "
              "one row each."
              % (target, len(candidates),
                 ", ".join(sorted(short_file(r.get("file"))
                                  for r in candidates))),
              file=sys.stderr)
        return 2
    rows = reset_pass(readings, reading_age_s=age, now=now, only=target,
                      source="manual", url_base=live_base_url(),
                      cooling=_cooling_now())
    if as_json:
        print(json.dumps({"rows": rows}, indent=2, sort_keys=True))
        return 0
    for line in result_lines(rows) or ["  (nothing to report)"]:
        print(line)
    row = rows[0] if rows else {}
    if row.get("outcome") == OUTCOME_RESET:
        return 0
    if row.get("outcome") is None:
        print("helm codex resets: nothing was sent — %s (%s)"
              % (row.get("detail") or "no decision", row.get("reason") or "?"),
              file=sys.stderr)
    return 1
