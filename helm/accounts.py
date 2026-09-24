#!/usr/bin/env python3
"""helm accounts — the OWNER-DECLARED account inventory: what we pay for, what
each one is for, and what it must NOT be used for.

AUTHORITATIVE ABOUT INTENT, NEVER ABOUT ANYTHING OBSERVABLE. This file says an
X Premium subscription EXISTS, costs about seven dollars a month, is for reading
the live timeline and is not for building. It never says an account is active,
healthy, exhausted or how much headroom it has — those are MEASURED, they belong
to the quota provider, and a fourth list that looked authoritative about them
would reproduce the incident orcaaccounts.py records (two registries quietly
reconciled into one wrong report). The declared layer JOINS the measured rows
and decorates them; it may not remove, reorder or overwrite a measured cell.

WHY IT EXISTS. The owner kept being asked how many accounts of each kind we
have and what each is good for, because nothing held the answer. The knowledge
was real and scattered: two free-prose rows on the integration board (nothing
reads them), a hand-written resets ledger inside a task comment thread, one
prose paragraph in the typed store, and the seat catalog's wiring table — which
knows the port and the model for the owner's paid X/grok subscription and has
nowhere to say that its quota is too small to build with. Every one of those is
unqueryable, so every agent asked him again.

AUTHORED, NOT DERIVED. registry.json is a projection and can be wiped and
re-synced; registry-authored.json cannot, and lives beside it for exactly that
reason. This file is the same class. So it gets the same corruption net: an
unparseable file is backed up beside itself BEFORE any writer can save over it,
an unreadable file reads as UNKNOWN rather than as "we have no accounts", and a
row this module cannot parse is REPORTED and SKIPPED on read yet PRESERVED
byte-for-byte on the next save — a hand-edit is legal here, and a save that
quietly dropped the line someone typed wrong would be the worst outcome this
surface can produce.

NO CREDENTIAL FIELD EXISTS. Not empty, not optional — absent from the schema, so
the page has no box to paste a token into and the refusal is structural rather
than a validator somebody can route around. On top of that every free-text field
is shape-scanned and a secret-looking value is refused with a plain sentence
that names the field and never echoes what it was handed. Addresses count as
secrets here, with ONE narrow exemption that is a fact about the world rather
than a convenience: `measured_as` holds the name the QUOTA PROVIDER minted, and
on this host most of those names are addresses, so a declared row could not find
its measured row without it. That field still refuses every key and token shape,
every other field still refuses an address, and every HUMAN rendering masks it
— the card, `helm accounts` and `helm accounts show` all print `mask_identity`'s
one letter and domain. `--json` carries it whole because that is the machine
surface the join reads, and a masked join key joins nothing. The same rule
covers a MEASURED account nobody has described yet: `join` mints the mask to
print beside an opaque `measured_key` handle to post back, so the owner's page
can offer to describe an account it was never told the name of.

THREE FIELDS ABOUT MONEY AND KEYS, and their grammar lives next door in
`accountfields`. `billing` is a closed set (sub | payg | prepaid | free)
rendered through ONE word map, so the table, `show`, the quota tab and the
agent line cannot disagree about how a row is paid for; a row that declares
none reads "not declared" and never falls to a default, because a default here
would be a guess about money. `allowance` is FREE TEXT that is also READ — the
text he typed is what is stored, and {cap, unit, cadence} is derived on the way
out, so a parse that failed costs him nothing and no older row carries a
structure a later parser did not mint. `key_where` is a LOCATION and never a
key: it says which credhome, env file or provider dashboard holds one, it faces
the tree's key grammar PLUS a rung for the short opaque token that grammar is
too coarse to catch, and `key_present` is derived from whether the label is
there at all.

BOUNDED ON PURPOSE, like fleetnotes: MAX_ACCOUNTS rows and per-field character
caps, each refusing LOUDLY rather than trimming, because this renders on the
owner's quota tab and an unbounded accumulator nobody prunes becomes a wall he
stops reading.
"""
import hashlib
import json
import os
import re
import shutil
import sys
import unicodedata

from . import accountfields, accountseed, eventledger, home, pk

MAX_ACCOUNTS = 64          # distinct declared accounts; a new id past this REFUSES
MAX_NAME = 80              # vendor / plan / reach / measured_as
MAX_SENTENCE = 300         # good_for / not_for — one sentence each
MAX_NOTES = 600
MAX_ID_CHARS = 64
MAX_COUNT = 999
MAX_HEADLINE = 120         # the owner's one glanceable line (good_for is elided here,
                           # never truncated on disk — the detail fold holds it whole)

MISSING_REVISION = "absent"   # the revision of a file that is not there yet

# An account id is an IDENTIFIER, validated at THE one ingestion seam (the same
# reflex as fleetnotes._KEY_RE): it is rendered onto the owner's quota tab and
# printed to terminals, so an ESC/bidi payload must never become one. It is also
# a SLUG and never an email: the schema has no email field at all, so the
# binding to a measured identity rides `measured_as`, which the provider mints.
_ID_RE = re.compile(r"\A[A-Za-z0-9._-]{1,%d}\Z" % MAX_ID_CHARS)
# THE HANDLE NAMESPACE IS RESERVED. A bad row's removal handle is id-shaped so
# every door admits it, which means a healthy row COULD be named exactly like
# one; then removing the broken row would pop the healthy row under the same
# spelling and leave the broken one. No declared id may take this shape, and
# remove() resolves a handle-shaped id only against unusable keys.
_HANDLE_RE = re.compile(r"\Abad-[0-9a-f]{12}\Z")

# THE OWNER-ONLY REACH, spelled once. It is the answer that actually stops the
# asking ("we have it, but no seat is wired to it yet"), so the page renders it
# as its own visible tier rather than letting it read like a missing value.
OWNER_ONLY = "owner-only, ask"

# THE SEED'S PLACEHOLDERS, NAMED. `good_for` and `not_for` are required, so a
# seeded row has to say something in them — but what it says is a PROMPT, not a
# description, and a surface that renders it as one tells the owner his account
# is already described. Naming the two strings here is what lets `project` mark
# the row `needs_describe` and lets the quota table keep its one-click describe
# affordance instead of a chip reading "for: not described yet".
NOT_DESCRIBED_FOR = "not described yet — say what this one is for"
NOT_DESCRIBED_NOT = "not described yet — say what it must not be used for"

# The pointer an agent reads. FIXED TEXT plus one live count, regenerated by the
# verb that prints it — never a number baked into injected prose, which is the
# published-measurement-keeps-steering-after-it-expires class.
POINTER_CAP = 200          # characters; asserted as a NUMBER by the suite
STORE_ENTRY_ID = "accounts-are-declared-in-helm"
# THE INJECTED LINE CARRIES NO COUNT, and that is the whole difference between
# it and `agent_line`. A gloss is FIXED TEXT: it is written once and fires
# unchanged for as long as it lives, so a number inside it is a measurement
# that keeps steering after it expires — the owner edits one account and the
# sentence in every seat's context is wrong. The counts live behind the verb,
# which is computed at read time and therefore cannot be stale.
STORE_GLOSS = ("helm holds the owner's DECLARED accounts — run `helm accounts` "
               "for vendor, plan, count and what each one is for; never ask him "
               "to recount them.")
# Narrow and high-signal. `plan` and `account` alone were cut deliberately: they
# collide with plan-mode and with ordinary chatter, and a JIT slot spent on a
# false fire is one of only four.
STORE_KEYWORDS = ("accounts", "subscription", "subscriptions", "how many accounts",
                  "which accounts", "vendor", "grok", "huggingface", "x premium")

_FIELDS = ("id", "vendor", "plan", "price_month", "count", "good_for", "not_for",
           "reach", "measured_as", "renews_on", "notes", "seeded_from",
           "billing", "allowance", "key_where",
           "confirmed", "updated_at")
# every field that holds text a human typed — the scanner's whole surface.
# `billing` is absent because it is a CLOSED SET: a value outside it is refused
# by the enum before a scanner could ever see it, and a member of it cannot be
# a secret. `key_where` is here AND faces a second rung (accountfields), because
# it is the field a pasted key is most likely to land in.
_SCANNED = ("id", "vendor", "plan", "price_month", "good_for", "not_for",
            "reach", "measured_as", "notes", "seeded_from", "allowance",
            "key_where")


class AccountsUnreadable(Exception):
    """The inventory file EXISTS but cannot be read or parsed.

    Raised by `read_strict`, so a caller that must not degrade REFUSES instead
    of answering an empty inventory: an unreadable source is indistinguishable
    from 'nothing declared', and answering zero accounts would be the exact
    wrong sentence to hand an agent who asked how many we have."""


def path():
    """The inventory file. Beside the other durable AUTHORED state under the
    helm home (registry-authored.json, owner-asks.jsonl, fleet-notes.json) —
    never .state/, which is declared lossy host-local telemetry, and never the
    repo, where an account row would be published."""
    return os.environ.get("HELM_ACCOUNTS") \
        or os.path.join(home.global_dir(), "accounts.json")


# ---------------------------------------------------------------------------
# the one ingestion seam
# ---------------------------------------------------------------------------

def _launder(s):
    """Strip C0/C1 controls, Unicode format chars (bidi overrides included) and
    line/paragraph separators. DEFENCE IN DEPTH, not the guard: the writer
    REFUSES such a payload, but this file is plain JSON on disk and a hand-edit
    is legal, so both sinks — the owner's page and a terminal — launder too."""
    if not isinstance(s, str):
        return s
    return "".join(c for c in s if c == "\n"
                   or unicodedata.category(c) not in ("Cc", "Cf", "Zl", "Zp"))


def _clean_id(raw):
    """An id is a NAME. The refusal quotes what it was handed as printable
    ASCII — a message about a hostile id must not carry the hostile id."""
    value = raw.strip() if isinstance(raw, str) else ""
    if not _ID_RE.match(value):
        raise ValueError(
            "the account id is not a legitimate id: '%s'. An id is letters, "
            "digits, dot, dash or underscore, at most %d characters — like "
            "x-premium. It is a short name, never an email address."
            % (str(raw)[:60].encode("unicode_escape").decode("ascii"),
               MAX_ID_CHARS))
    return value


def _clean_declared_id(raw):
    """An id a DECLARED row may carry: a legitimate id that is not spelled
    like a broken row's removal handle."""
    value = _clean_id(raw)
    if _HANDLE_RE.match(value):
        raise ValueError(
            "'%s' is spelled like the removal handle of a broken row and is "
            "reserved for that; pick another short name." % value)
    return value


def _clean_line(label, raw, cap, required=True):
    """One short field: NFC, newlines folded to spaces, controls refused."""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        if required:
            raise ValueError("%s is required — say it in a few words." % label)
        return None
    if not isinstance(raw, str):
        raise ValueError("%s must be text." % label)
    text = unicodedata.normalize("NFC", " ".join(raw.split())).strip()
    for ch in text:
        if unicodedata.category(ch) in ("Cc", "Cf", "Zl", "Zp"):
            raise ValueError(
                "%s may not contain control or bidi characters (found %s)."
                % (label, ch.encode("unicode_escape").decode("ascii")))
    if len(text) > cap:
        raise ValueError("%s is %d characters — the limit is %d. Say the short "
                         "version here and put the rest in notes."
                         % (label, len(text), cap))
    return text


# ---------------------------------------------------------------------------
# the secret refusal — SHAPE-BASED, and it never echoes what it refused
# ---------------------------------------------------------------------------

# A KEY SHAPE IS A PREFIX AT A TOKEN BOUNDARY FOLLOWED BY KEY MATERIAL, and
# all three clauses are load-bearing. Matched as a bare SUBSTRING — `prefix in
# value` — this tuple refuses ordinary English instead: task-, ask-, risk- and
# disk- all carry "sk-", and the first two are this repo's own vocabulary, so
# the guard rejected the sentence the owner was writing ABOUT his account. That
# is the same failure as refusing his account name, one field over.
_SECRET_PREFIXES = ("sk-", "sk_", "xai-", "hf_", "ghp_", "gho_", "ghu_", "ghs_",
                    "github_pat_", "pat_", "AKIA", "ASIA", "xoxb-", "xoxp-",
                    "AIza", "glpat-", "dop_v1_", "shpat_")
MIN_KEY_MATERIAL = 12      # characters AFTER the prefix; real keys are longer
# The boundary is a lookbehind rather than \b, because "sk-" begins with a word
# character and \b would match inside "task-" exactly as the substring test did.
_KEY_SHAPE_RE = re.compile(
    r"(?<![A-Za-z0-9_-])(" + "|".join(re.escape(p) for p in _SECRET_PREFIXES)
    + r")([A-Za-z0-9_-]{%d,})" % MIN_KEY_MATERIAL, re.I)
_CRED_WORD = (r"(?:bearer|authorization|api[-_ ]?key|apikey|secret|password|"
              r"passwd|token|cookie|recovery code)")
_BEARER_RE = re.compile(r"\b" + _CRED_WORD + r"\b\s*[:=]", re.I)
# THE OTHER HALF OF THE SAME SENTENCE: "Bearer abc123" carries no colon, so the
# rule above read it as prose and let it through. A credential word followed by
# a VALUE is the same disclosure.
#
# THE RUN MAY NOT CARRY A HYPHEN, and that clause is the whole difference
# between this rule and one that refuses ordinary English. The first spelling
# took any 6+ character run that was not purely alphabetic, and a hyphenated
# compound after a credential word is exactly that: "api key rotation-policy",
# "password 8-character minimum", "token bucket-style rate limiting", "bearer
# 10-year bonds" were all refused as credential lines — the surface accusing
# him of pasting a secret into a sentence about his own subscription. A
# credential is one unbroken token; an English compound is words joined by
# hyphens, so the hyphen ENDS the run and what is left ("rotation", "8",
# "bucket") faces `_looks_like_key_material` as its own short word.
_BEARER_VALUE_RE = re.compile(r"\b" + _CRED_WORD + r"\b\s+([A-Za-z0-9_+/.=]{6,})",
                              re.I)
_HEX_RE = re.compile(r"[0-9a-f]+", re.I)
# A RECOVERY CODE IS HYPHENATED ON PURPOSE, and dropping the hyphen from the
# run above — the clause that stopped ordinary compounds being refused — let
# "recovery code 4821-9930-1147-2208" through. It was the one shape in the
# corpus that is hyphenated AND a credential, so the hyphen rule needs its own
# counterweight: THREE or more groups of three or more characters with a digit
# among them. "10-year" and "2-of-3" have groups too short, "rotation-policy"
# and "reset-flow" have two groups and no digit; no compound in the corpus
# reaches three groups.
_CODE_GROUPS_RE = re.compile(
    r"\b" + _CRED_WORD + r"\b\s+([A-Za-z0-9]{3,}(?:-[A-Za-z0-9]{3,}){2,})", re.I)


def _looks_like_key_material(run):
    """Does this run, standing right after a credential word, look like key
    material rather than the next word of a sentence?

    THE QUESTION IS SHAPE, NOT LENGTH. "hunter2xyz99" and a twelve-character
    run of hex are credentials; "rotation", "Santa2024", "OAuth2Client" and
    "q4roadmap" are words. What separates them is where the digits sit: key
    material SCATTERS them through the run or never stops at all, while a word
    carries at most one digit group at one end — a year, a quarter, a version.
    Three clauses, each with a shape behind it and none of them a raw length:

      long and unbroken — 16+ characters; no English word runs that far
      hex               — 10+ characters of nothing but [0-9a-f] (the two
                          guards above have already sent pure letters and
                          pure digits home, so this run mixes them)
      scattered         — 8+ characters holding two separate digit groups

    A miss this leaves standing: a short mixed-case token with ONE digit group
    ("AbCd1234efGH") reads as a word here. Refusing that shape also refuses
    "api key OAuth2Client docs", and the field it guards is a sentence the
    owner wrote about his own subscription."""
    core = str(run or "").strip("._=/+")
    if len(core) < 6 or core.isalpha() or core.isdigit():
        return False
    if not any(c.isalpha() for c in core):
        return False                       # digits and padding: a quantity
    if len(core) >= 16:
        return True
    if len(core) >= 10 and _HEX_RE.fullmatch(core):
        return True
    return len(core) >= 8 and len(re.findall(r"\d+", core)) >= 2


_EMAIL_RE = re.compile(r"[^\s@]+@[^\s@]+\.[A-Za-z]{2,}")
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}")
# A long UNBROKEN run of credential alphabet. 32 is the floor because real prose
# does not produce it and every token shape above it does.
#
# THE RUN MAY NOT CONTAIN A DASH OR UNDERSCORE, and that is the whole
# difference between this guard and one that refuses the owner's own accounts:
# a credential is 32+ characters with no word structure, while a long account
# name is short words joined by dashes. Measured on the live provider rows, a
# 33-character home name was refused as a key by an alphabet that included the
# dash — the surface rejecting the very account it exists to describe. A token
# that happens to carry a dash still trips this on whichever of its segments is
# long, so nothing is given up.
_BLOB_RE = re.compile(r"[A-Za-z0-9+/=]{32,}")


def _mask_secret(prefix, material):
    """Enough of a refused token for the owner to FIND it in what he typed, and
    never enough to use. The old refusal named only the PREFIX, which on a long
    notes field says "something in here looks like a key" and leaves him to
    hunt; three characters of material name the token without publishing it
    (home._safe_name's rule: a rejection may not carry its payload)."""
    return "%s%s…" % (prefix, material[:3])


def mask_identity(value):
    """An address rendered as an identity rather than as an address.

    The declared card is a NEW surface, and a new surface that starts printing
    the owner's account addresses has published something no verb published
    before. The join needs the whole string; a reader needs only enough to tell
    two accounts apart, which is the first letter and the domain."""
    if not isinstance(value, str) or not _EMAIL_RE.search(value):
        return value
    local, _at, domain = value.partition("@")
    head = local[:1] or "?"
    return "%s…@%s" % (head, domain)


REDACTED = "[redacted]"


def redact_identities(text):
    """Free text with every ADDRESS masked to an identity and every piece of
    key-shaped material removed -> text that may be persisted and rendered.

    `secret_reason` is the REFUSAL half — it turns a field away at the door.
    This is the other half, for the fields helm ACCEPTS and must keep: a
    sentence the owner wrote, which he may reasonably write with an account
    address in it, and which then rides a snapshot no identity may reach. The
    grammar of an address and of credential material is this module's, so a
    consumer asking for it gets this rather than a second regular expression
    of its own."""
    if not isinstance(text, str) or not text:
        return text
    out = _JWT_RE.sub(REDACTED, text)
    out = _EMAIL_RE.sub(lambda m: mask_identity(m.group(0)), out)
    return _BLOB_RE.sub(
        lambda m: m.group(0) if m.group(0).isalpha() else REDACTED, out)


def secret_reason(label, value, allow_address=False):
    """A plain sentence when `value` looks like a credential, else None.

    THE REFUSAL NAMES THE FIELD AND THE SHAPE AND NEVER THE VALUE. A rejection
    message that carries the payload it reports on has published the secret into
    a log, a terminal and probably a transcript (home._safe_name's rule).
    """
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    shape = _KEY_SHAPE_RE.search(text)
    if shape:
        return ("%s looks like it contains an API key or token: '%s'. helm's "
                "account inventory holds NO secrets — no key, token, password, "
                "cookie or recovery code. Say what the account is, not how to "
                "log into it."
                % (label, _mask_secret(shape.group(1), shape.group(2))))
    if _JWT_RE.search(text):
        return ("%s looks like it contains a signed token. helm's account "
                "inventory holds NO secrets — say what the account is, not how "
                "to log into it." % label)
    bearer = _BEARER_RE.search(text)
    if not bearer:
        bearer = next((m for m in _BEARER_VALUE_RE.finditer(text)
                       if _looks_like_key_material(m.group(1))), None)
    if not bearer:
        bearer = next((m for m in _CODE_GROUPS_RE.finditer(text)
                       if any(c.isdigit() for c in m.group(1))), None)
    if bearer:
        return ("%s reads like a credential line ('token:', 'password:', "
                "'bearer <value>' and the like). helm's account inventory holds "
                "NO secrets — say what the account is, not how to log into it."
                % label)
    if _EMAIL_RE.search(text) and not allow_address:
        return ("%s looks like it contains an email address. Account emails are "
                "private and helm does not store them here — use a short name "
                "like x-premium instead, and leave the login to the credential "
                "homes card." % label)
    blob = next((m for m in _BLOB_RE.finditer(text)
                 if not m.group(0).isalpha()), None)
    if blob:
        return ("%s contains a %d-character run of key-shaped characters. "
                "helm's account inventory holds NO secrets — if that is a "
                "credential, it does not belong on this page at all."
                % (label, len(blob.group(0))))
    return None


# ---------------------------------------------------------------------------
# the schema
# ---------------------------------------------------------------------------

def _clean_count(raw):
    if isinstance(raw, bool) or raw is None or raw == "":
        raise ValueError("count is required — how many of this exact plan do "
                         "you have? Use 1 if there is just the one.")
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        raise ValueError("count must be a whole number, like 1 or 4.")
    if value < 1:
        raise ValueError("count must be at least 1 — an account you do not "
                         "have is not an entry, it is a note.")
    if value > MAX_COUNT:
        raise ValueError("count is %d, which is past the limit of %d."
                         % (value, MAX_COUNT))
    return value


def _clean_renews(raw):
    if raw is None or not str(raw).strip():
        return None
    import time
    value = str(raw).strip()
    try:
        time.strptime(value, "%Y-%m-%d")
    except ValueError:
        raise ValueError("renews_on must be a date like 2027-01-31, or left "
                         "empty.")
    return value


def _unknown_error(payload):
    """The sentence for a field helm does not store, or None. ONE spelling, so
    the door's early refusal and validate's cannot drift apart."""
    unknown = sorted(k for k in payload if k not in _FIELDS)
    if not unknown:
        return None
    return ("helm does not store %s on an account. The inventory holds what an "
            "account IS and what it is FOR — never a key, token, password or "
            "login." % ", ".join("'%s'" % u for u in unknown))


def field_secret_reason(label, value):
    """The refusal one FIELD's value earns, or None.

    ONE SPELLING OF WHICH RUNG A FIELD FACES, so the early door and `validate`
    cannot disagree — the drift that would let a value be refused at the CLI
    and accepted through the page, or the reverse. Two fields are special and
    both for a stated reason: `measured_as` holds a provider-minted name that
    is usually an address, and `key_where` is a LOCATION label that faces the
    general grammar plus accountfields' own short-opaque-token rung."""
    if label == "key_where":
        return accountfields.key_where_reason(value, secret_reason)
    reason = secret_reason(label, value, allow_address=(label == "measured_as"))
    if reason:
        return reason
    # NOTES FACES THE SAME RUNG AS key_where. It is the other box on this
    # screen wide enough to paste a key into, and the general grammar is built
    # for addresses rather than for opaque runs, so a short hex or dotted
    # token stored here would reach every surface that prints a row.
    if label == "notes":
        return accountfields.opaque_run_reason("notes", value)
    return None


def _secret_error(payload):
    """The first secret-shaped value in a payload, or None. Run BEFORE the lock
    so the most important refusal on this surface is never masked by a
    conflict, and so a token never rides as far as the lock."""
    for label in _SCANNED:
        reason = field_secret_reason(label, payload.get(label))
        if reason:
            return reason
    return None


def validate(payload):
    """One declared account -> (row, error). The ONE door every writer uses, so
    the web form, the CLI and the seeder cannot disagree about what is legal.

    `updated_at` is set by the WRITER, never by the caller: a stamp the caller
    chooses is a stamp the caller can forge, and this row's whole value is being
    the thing the owner most recently said."""
    if not isinstance(payload, dict):
        return None, "an account must be a set of fields, not a bare value."
    unknown = _unknown_error(payload)
    if unknown:
        return None, unknown
    try:
        row = {"id": _clean_declared_id(payload.get("id"))}
        for label, cap, required in (("vendor", MAX_NAME, True),
                                     ("plan", MAX_NAME, True),
                                     ("price_month", MAX_NAME, False),
                                     # NOT REQUIRED, AND THAT IS THE OWNER'S
                                     # CORRECTION. These two were required, so
                                     # anything that created a row had to put
                                     # SOMETHING in them, and the only honest
                                     # something a machine has is a prompt —
                                     # which then rendered on his card as a
                                     # sentence he had not written. He read
                                     # those rows back to us as accounts
                                     # "unknown to me". An empty answer is a
                                     # better answer than helm's words in his
                                     # mouth: the row says nobody has described
                                     # it, and carries the affordance that
                                     # fixes that.
                                     ("good_for", MAX_SENTENCE, False),
                                     ("not_for", MAX_SENTENCE, False),
                                     ("reach", MAX_NAME, True),
                                     ("measured_as", MAX_NAME, False),
                                     ("notes", MAX_NOTES, False),
                                     ("seeded_from", MAX_NAME, False),
                                     ("allowance", accountfields.MAX_ALLOWANCE,
                                      False),
                                     ("key_where", accountfields.MAX_KEY_WHERE,
                                      False)):
            value = _clean_line(label, payload.get(label), cap, required)
            if value is not None:
                row[label] = value
        row["count"] = _clean_count(payload.get("count"))
        renews = _clean_renews(payload.get("renews_on"))
        if renews:
            row["renews_on"] = renews
        billing = accountfields.clean_billing(payload.get("billing"))
        if billing:
            row["billing"] = billing
        if payload.get("confirmed"):
            row["confirmed"] = True
    except ValueError as e:
        return None, str(e)
    for label in _SCANNED:
        # THE ONE NARROW EXEMPTION, and it is a fact about the world rather
        # than a convenience: a quota provider mints account names, and on this
        # host most of them ARE addresses. `measured_as` holds that name
        # verbatim or the declared row cannot find its measured row at all — so
        # the address rule is lifted HERE ONLY, every key/token shape still
        # refuses here, every other field still refuses an address, and nothing
        # renders this value whole (see `mask_identity` and measured_as_masked).
        reason = field_secret_reason(label, row.get(label))
        if reason:
            return None, reason
    row["updated_at"] = pk.now_ts()
    return row, None


# ---------------------------------------------------------------------------
# reading — tolerant, loud, and never destructive
# ---------------------------------------------------------------------------

def _corrupt_backup(p):
    """Back the file up beside itself BEFORE any writer can save over it.
    Authored content is unrebuildable (registry._authored_load's law).

    NAMED BY ITS CONTENT, NOT BY THE CLOCK. `read` is not a rare path: the
    quota tab fetches /api/accounts when the tab opens and again on every
    toggle of this card, so a name carrying a second-resolution stamp minted
    ANOTHER copy of the same bytes each time — an unbounded pile of identical
    files growing at the rate the owner clicks. The digest IS the identity: one
    corruption backs up once, ever, and a different corruption still gets its
    own copy, which is the property the backup exists for."""
    try:
        with open(p, "rb") as f:
            digest = hashlib.sha256(f.read()).hexdigest()[:12]
    except OSError as e:
        print("[helm accounts] %s unreadable — corrupt backup impossible "
              "(%s)" % (p, e), file=sys.stderr)
        return None
    bak = "%s.corrupt-%s" % (p, digest)
    if not os.path.exists(bak):
        try:
            shutil.copy2(p, bak)
        except OSError as e:
            print("[helm accounts] %s unreadable — corrupt backup impossible "
                  "(%s)" % (p, e), file=sys.stderr)
            return None
    return bak


def _load_raw(p):
    """(raw_accounts_map, unreadable_reason). The RAW map, unvalidated: the
    writer mutates THIS, so a row nobody can parse survives the next save."""
    if not os.path.exists(p):
        return {}, None
    value = pk.read_json(p, default=None)
    if isinstance(value, dict) and isinstance(value.get("accounts"), dict):
        return value["accounts"], None
    if isinstance(value, dict) and not value:
        return {}, None
    bak = _corrupt_backup(p)
    return {}, ("the accounts file could not be read as an inventory%s"
                % (" — a copy was kept at %s" % os.path.basename(bak)
                   if bak else ""))


def revision(raw):
    """An opaque edit identity over the CONTENT, so two tabs cannot clobber each
    other. Content rather than stat (configs._revision's other half) because
    this file is rewritten atomically on every save: a stat-derived revision
    would conflict two browsers that agree, which trains the owner to click
    through the one warning that matters."""
    if not raw:
        return MISSING_REVISION
    blob = json.dumps(raw, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:32]


def _price_value(text):
    """The monthly number inside a free-text price, or None.

    The field is a STRING on purpose — '$7 + tax', 'bundled' and 'unknown' are
    all things the owner will type and all of them are true. So the total says
    how many rows it could not price instead of pretending a float."""
    if not isinstance(text, str):
        return None
    m = re.search(r"(\d+(?:[.,]\d+)?)", text.replace(",", ""))
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def project(row):
    """One declared row + the owner-facing shape. ONE headline boundary, owned
    here, so the quota card and the CLI cannot render different sentences about
    the same account (fleetnotes' law)."""
    out = {k: _launder(v) for k, v in row.items() if k in _FIELDS}
    out.setdefault("measured_as", None)
    out.setdefault("price_month", None)
    # A ROW MAY NOW EXIST WITHOUT THESE. They were required, so every row on
    # disk carried them and nothing had to default them; a create that declines
    # to invent a sentence produces a row where they are simply absent, and a
    # projection that KeyErrors on it takes the owner's whole card down.
    out.setdefault("good_for", None)
    out.setdefault("not_for", None)
    out.setdefault("notes", None)
    out.setdefault("renews_on", None)
    out.setdefault("seeded_from", None)
    out.setdefault("billing", None)
    out.setdefault("allowance", None)
    out.setdefault("key_where", None)
    # DERIVED, NEVER STORED. `billing_word` is the ONE spelling every surface
    # prints, and a row that says nothing about billing reads "not declared"
    # rather than falling to a default — a default here would be a guess about
    # money. `allowance_parsed` is a READING of the text he typed: storing it
    # beside the text would hand every older row a structure this parser did
    # not mint, and re-reading costs nothing.
    out["billing_word"] = accountfields.billing_word(out["billing"])
    out["allowance_parsed"] = accountfields.parse(out["allowance"])
    out["key_present"] = accountfields.key_present(out["key_where"])
    out["confirmed"] = bool(row.get("confirmed"))
    out["needs_confirm"] = bool(out["seeded_from"]) and not out["confirmed"]
    out["owner_only"] = (out.get("reach") or "").strip().lower() == OWNER_ONLY
    # NOBODY HAS DESCRIBED THIS ROW YET. The seed had to fill a required field
    # to write the row at all; the value it filled it with is the ask, so a
    # renderer must draw the ask rather than quote it back as an answer.
    # NOBODY HAS DESCRIBED THIS ROW, whether because the field is EMPTY (the
    # shape every new row has now) or because it still carries a stub an older
    # seed wrote. Both are the same state and both must draw the ask; keying on
    # the stub alone would make every row created from here read as described.
    out["needs_describe"] = (not (out.get("good_for") or "").strip()
                             or out.get("good_for") == NOT_DESCRIBED_FOR)
    out["measured_as_masked"] = mask_identity(out.get("measured_as"))
    out["price_value"] = _price_value(out.get("price_month"))
    bits = [out.get("vendor") or "?", out.get("plan") or "?",
            "x%d" % out.get("count", 1)]
    if out.get("price_month"):
        bits.append("%s/mo" % out["price_month"])
    # HOW IT IS BILLED AND HOW MUCH IT ALLOWS, beside the price and before the
    # identity: "$20/mo" alone never said whether that is a subscription or a
    # balance being drawn down, which is the question the owner opened this
    # surface to stop being asked.
    bits.extend(accountfields.bits(out))
    # THE IDENTITY EARNS ITS PLACE ON THE ONE LINE. Six accounts of one vendor
    # on one plan render as six identical headlines without it, and a list the
    # owner cannot tell apart at a glance is a list he stops reading — the
    # failure this whole card exists to end. Masked, like everywhere else.
    if out.get("measured_as_masked"):
        bits.append(out["measured_as_masked"])
    head = " · ".join(bits)
    good = out.get("good_for") or ""
    room = MAX_HEADLINE - len(head) - 3
    if good and room > 12:
        bits.append(good if len(good) <= room else good[:room - 1] + "…")
    out["headline"] = " · ".join(bits)
    return out


def _unusable_key(key):
    """A key this module cannot use as an id. Its row is reported and kept —
    and it is the ONLY kind of key `remove` will resolve a handle for."""
    return not isinstance(key, str) or not _ID_RE.match(key) \
        or bool(_HANDLE_RE.match(key))


def bad_handle(key):
    """A handle that can actually REMOVE a row whose key cannot be an id.

    THE ESCAPED RENDERING IS FOR THE EYE AND CANNOT BE POSTED BACK. A bad row
    reports its key as printable ASCII exactly so a hostile one never reaches a
    terminal or the owner's page — and both surfaces then handed that escaped
    string to `remove`, whose first act is to validate it as an id. It is not
    one: that is why the row is bad. So a hand-broken row could be removed from
    neither the card nor the CLI, on a file the module's own docstring says a
    hand-edit is legal in.

    The handle is derived from the key, so it names one row and survives a
    reload; it is id-shaped, so every door that validates an id admits it; and
    it is opaque, so the control characters the escaping exists to contain
    never travel anywhere."""
    return "bad-" + hashlib.sha256(repr(key).encode("utf-8")).hexdigest()[:12]


def _bad_entry(key, why):
    """A row this module cannot parse, REPORTED rather than dropped. It stays on
    disk under its key; only an explicit remove takes it off.

    `id` is the rendering — escaped, for the eye. `handle` is what a surface
    posts back to remove it: the key itself when the key is a legitimate id
    (the row is bad for some other reason and removes normally), and an opaque
    handle when the key is the thing that is broken."""
    return {"id": str(key)[:MAX_ID_CHARS].encode("unicode_escape").decode("ascii"),
            "handle": bad_handle(key) if _unusable_key(key) else key,
            "why": why}


def read(accounts_path=None):
    """The inventory as the owner's surfaces see it.

    {"accounts": [...projected, sorted...], "bad": [...], "unreadable": str|None,
     "revision": str, "totals": {...}, "path": str}

    FAIL-OPEN AND TOTAL, like fleetnotes.read: this runs on the quota tab and a
    raise here takes the page down. But an unreadable FILE is reported in
    `unreadable` and rendered as an UNKNOWN strip — absence is unknown, never
    empty (orcaaccounts' law)."""
    p = accounts_path or path()
    raw, unreadable = _load_raw(p)
    rows, bad = [], []
    for key, value in raw.items():
        if _unusable_key(key):
            bad.append(_bad_entry(key, "the id is not a legitimate id"))
            continue
        if not isinstance(value, dict):
            bad.append(_bad_entry(key, "the entry is not a set of fields"))
            continue
        clean, err = validate(dict(value, id=key))
        if err:
            bad.append(_bad_entry(key, err))
            continue
        clean["updated_at"] = _launder(value.get("updated_at")) or ""
        rows.append(project(clean))
    rows.sort(key=lambda r: (r.get("vendor") or "", r.get("id") or ""))
    bad.sort(key=lambda b: b["id"])
    return {"accounts": rows, "bad": bad, "unreadable": unreadable,
            "revision": revision(raw), "totals": totals(rows), "path": p}


def read_strict(accounts_path=None):
    """The authority read: REFUSES an unreadable file instead of answering an
    empty inventory. `helm accounts` uses this, because telling an agent we have
    zero accounts when the file is garbled is worse than telling it nothing."""
    view = read(accounts_path)
    if view["unreadable"]:
        raise AccountsUnreadable(view["unreadable"])
    return view


def totals(rows):
    """accounts N · monthly spend $X — so nobody has to recount.

    UNPRICED ROWS ARE COUNTED, NOT ASSUMED FREE. A sum that silently treats
    'unknown' as zero understates the bill and reads authoritative doing it."""
    units = sum(int(r.get("count") or 0) for r in rows)
    spend, unpriced = 0.0, 0
    for r in rows:
        value = r.get("price_value")
        if value is None:
            unpriced += 1
            continue
        spend += value * int(r.get("count") or 0)
    return {"accounts": len(rows), "units": units,
            "monthly_spend": round(spend, 2), "unpriced": unpriced}


# ---------------------------------------------------------------------------
# writing — read and write inside ONE lock
# ---------------------------------------------------------------------------

def _load_removed(value):
    """The tombstone list off a parsed inventory document — the ids the owner
    has DELETED and does not want seeded back.

    HIS CLEANUP IS THE FRAGILE THING HERE. The seed re-reads the same sources
    every time it runs, so an id it minted once it will mint again forever: he
    deletes a duplicate, the next seed puts it straight back, and the surface
    teaches him that tidying his own inventory does not work. Nobody removed
    the duplicates by hand today for exactly that reason. A tombstone is the
    cheapest thing that makes a deletion mean something."""
    if not isinstance(value, dict):
        return []
    removed = value.get("removed")
    if not isinstance(removed, list):
        return []
    return [r for r in removed if isinstance(r, str) and _ID_RE.match(r)]


def _update_doc(mutate, accounts_path=None):
    """Apply mutate(raw_accounts_map, removed_ids) under the exclusive sibling
    lock, and write BOTH back.

    `_update` is this with the tombstones held constant, which is what every
    writer but `remove` and `seed` wants."""
    p = accounts_path or path()
    with eventledger.locked(p) as held:
        if not held:
            return None, ("the accounts file is locked by another writer (or "
                          "the lock could not be taken) — nothing was written.")
        raw, unreadable = _load_raw(p)
        if unreadable:
            return None, unreadable + ". Fix or move that file before saving, "\
                                      "so an edit cannot overwrite it."
        removed = _load_removed(pk.read_json(p, default=None))
        out = mutate(raw, removed)
        doc = {"version": 1, "accounts": raw}
        # AN EMPTY LIST IS NOT WRITTEN, so an inventory nobody has ever deleted
        # from keeps the exact shape every older reader expects of it.
        if removed:
            doc["removed"] = sorted(set(removed))
        pk.write_json(p, doc)
        return out, None


def removed_ids(accounts_path=None):
    """The tombstoned ids, for a reader outside the lock. FAIL-OPEN like
    `read`: an unreadable file answers none rather than raising on the page."""
    try:
        return _load_removed(pk.read_json(accounts_path or path(), default=None))
    except Exception:                       # noqa: BLE001 — the tab must render
        return []


def _update(mutate, accounts_path=None):
    """Apply mutate(raw_accounts_map) under the exclusive sibling lock.

    fleetnotes._update's contract verbatim, and for its reason: two writers in
    the same second is the ordinary case on a surface with a web form and a
    CLI, and an API that hands back the parsed map for the caller to write later
    re-opens the lost update with more steps. A mutate that raises writes
    nothing. The map passed in is the RAW one, so a row this module could not
    parse is carried through untouched."""
    return _update_doc(lambda raw, _removed: mutate(raw), accounts_path)


class _Conflict(ValueError):
    """Somebody else saved between this tab's read and its save."""


class _Vanished(ValueError):
    """A write that CANNOT BE A CREATE, about a row that is not there.

    `confirm` sends {id, confirmed} and nothing else: it means "the row I am
    looking at is right". With that row removed underneath, the merge has
    nothing to merge over and validate answers "vendor is required — say it in
    a few words", a sentence about a box he never touched. The row's absence
    is the news.

    THE LINE IS DRAWN AT "carries no required field AT ALL", not at "carries
    them all", and the difference is a whole class of sentences: a create
    missing one field is a create, and it must keep hearing which field —
    `helm accounts set acct-new --vendor v --plan p` is a person adding an
    account, not a person editing a ghost. A payload holding NONE of the
    required fields cannot be a create under any reading."""


# the fields validate() requires of a NEW row. `good_for`/`not_for` left this
# tuple with the stub sentences: a create that cannot name what an account is
# for should say nothing, not quote the question back.
_REQUIRED = ("vendor", "plan", "reach")


def _merge_over_stored(payload, stored):
    """The fields this writer CARRIES, over the row already on disk.

    ABSENCE PRESERVES, AN EXPLICIT EMPTY CLEARS. A save replaces the row under
    its key, so a writer holding only some of the fields DELETED the rest: the
    quota tab's form round-trips ten of fourteen, and every edit of a seeded
    row silently destroyed `renews_on`, `confirmed` and `seeded_from` — the
    details fold rendering the renewal date the edit button was about to throw
    away. The merge lives at the DOOR rather than in the form because the CLI's
    `set <id> --confirm` is the same partial write, and a second copy of this
    rule in a second writer is a second chance to get it wrong."""
    if not isinstance(stored, dict):
        return dict(payload)
    unknown = sorted(k for k in stored if k not in _FIELDS)
    if unknown:
        # SILENTLY DROPPING IT WOULD BE THIS FINDING AGAIN, one layer down: an
        # unknown key on disk is somebody's hand-edit, and a merge that ate it
        # is exactly the destruction this function exists to stop.
        raise ValueError(
            "the stored row carries %s, which helm does not know. Fix that row "
            "on disk before editing it here — saving over it would drop what "
            "helm cannot read." % ", ".join("'%s'" % u for u in unknown))
    # NO FIELD IS FILTERED OUT HERE. An earlier spelling dropped `updated_at`
    # on the way in "so the caller cannot forge a stamp" — but validate() sets
    # it unconditionally one call later, so the filter could not change any
    # outcome and only said something untrue about where the rule lives. The
    # stamp is the WRITER's, and validate is the writer.
    merged = dict(stored)
    merged.update(payload)
    return merged


def save(payload, expected_revision=None, accounts_path=None, original_id=None):
    """Add or MERGE one declared account -> (row, error, code).

    A field the payload does not carry keeps the value already on disk; an
    explicit empty or null clears it (`_merge_over_stored`). The merge happens
    under the same lock as the write, because a caller that read the row, built
    a full payload and sent it back would re-open the lost update this module
    refuses everywhere else.

    `expected_revision` is the revision the editor actually opened. It is
    compared INSIDE the lock, so two tabs editing the same inventory cannot
    clobber each other: the second save is refused, told to reload, and writes
    nothing. Passing None skips the check, which is what a fresh CLI call
    legitimately wants and what a browser must never do.

    `original_id` IS THE EDITOR SAYING WHICH ROW IT OPENED, and it makes an
    edit that changes the id a REFUSAL rather than a second row. The id is the
    key this row is stored under, so a save carrying a different one is not an
    edit at all: it writes a new row and leaves the old one exactly where it
    was — two rows for one subscription, under a form whose own text says the
    id stays the same. A rename would have to move the row and refuse a
    collision; a refusal costs the owner one remove-and-re-add and cannot end
    with a duplicate, so that is the contract. An ADD passes None and is
    unaffected: this is about an editor that already has a row open."""
    if not isinstance(payload, dict):
        return None, "an account must be a set of fields, not a bare value.", \
            "refused"
    unknown = _unknown_error(payload)
    if unknown:
        return None, unknown, "refused"
    try:
        # THE ID IS CLEANED FIRST, which is validate's own order: an
        # address-shaped id is an ID problem with a slug to suggest, and the
        # scanner's "that looks like an email" would bury the one sentence
        # that tells him what to type instead.
        account_id = _clean_declared_id(payload.get("id"))
    except ValueError as e:
        return None, str(e), "refused"
    if original_id is not None:
        try:
            original = _clean_id(original_id)
        except ValueError as e:
            return None, str(e), "refused"
        if original != account_id:
            # BEFORE THE LOCK because it needs nothing from disk, and it is a
            # refusal rather than a rename so the failure mode cannot be the
            # duplicate this check exists to stop. Both ids are cleaned, so
            # the sentence carries no payload a hostile one could smuggle.
            return None, (
                "the id is the row's key, so an edit cannot change it: '%s' "
                "cannot become '%s'. Remove that row and add it again under "
                "the new id — nothing was changed." % (original, account_id)
            ), "refused"
    secret = _secret_error(payload)
    if secret:
        return None, secret, "refused"

    def _mutate(raw, removed):
        if expected_revision is not None and revision(raw) != expected_revision:
            raise _Conflict("someone else changed the accounts list while this "
                            "one was open. Reload the page and make the change "
                            "again — nothing was overwritten.")
        stored_row = raw.get(account_id)
        if stored_row is None and not any(f in payload for f in _REQUIRED):
            raise _Vanished(
                "there is no declared account '%s' to change — nothing in the "
                "inventory has that id any more. Reload; if you still pay for "
                "it, add it again with vendor, plan and what it is for."
                % account_id)
        # A DESCRIBE NEVER RE-BINDS A ROW. Two measured accounts can slug to
        # one suggested id; the second describe would then merge over the
        # first row and silently point it at another subscription. A row that
        # already names a different measured account refuses the describe and
        # says which id to pick instead.
        incoming = payload.get("measured_as")
        held = (stored_row or {}).get("measured_as")
        if incoming and held and incoming != held:
            raise ValueError(
                "'%s' already describes another measured account (%s); "
                "describe this one under a different id, such as '%s-2'."
                % (account_id, mask_identity(held), account_id))
        row, bad = validate(_merge_over_stored(payload, stored_row))
        if bad:
            raise ValueError(bad)
        if row["id"] not in raw and len(raw) >= MAX_ACCOUNTS:
            raise ValueError(
                "the inventory holds %d accounts, which is the limit. Remove "
                "one you no longer pay for before adding another."
                % len(raw))
        stored = dict(row)
        stored.pop("id", None)
        raw[row["id"]] = stored
        # A ROW HE WRITES IS A ROW HE WANTS, so writing one lifts its tombstone.
        # Without this the fence outlives the reason for it: he deletes a
        # duplicate, changes his mind, adds it back by hand — and the next seed
        # is still refusing to know about an account that is sitting on his own
        # card. A tombstone answers "do not RE-MINT this"; it was never a
        # standing ban on the name.
        while row["id"] in removed:
            removed.remove(row["id"])
        return dict(row)

    try:
        out, err = _update_doc(_mutate, accounts_path)
    except _Conflict as e:
        return None, str(e), "conflict"
    except _Vanished as e:
        # its own code, because the card has to RELOAD on it exactly as it
        # does on a conflict: he is editing a row that is not there.
        return None, str(e), "gone"
    except ValueError as e:
        return None, str(e), "refused"
    if err:
        return None, err, "refused"
    pk.event("accounts", out["id"], "declared %s %s x%d"
             % (out["vendor"], out["plan"], out["count"]))
    return project(out), None, None


def remove(account_id, expected_revision=None, accounts_path=None):
    """DELETE one declared account -> (ok, error, code). Destructive and the
    name says so; nothing about the measured account changes.

    IT ALSO TAKES A BAD ROW'S HANDLE, and that is the only way a hand-broken
    row leaves the file: its key is not an id, so nothing that validates an id
    can name it (`bad_handle`). The handle is resolved under the same lock as
    the delete, and ONLY against keys that are themselves unusable — a handle
    computed over a good row's key resolves to nothing, so this door can never
    become a second way to delete a healthy account."""
    try:
        account_id = _clean_id(account_id)
    except ValueError as e:
        return False, str(e), "refused"

    def _mutate(raw, removed):
        if expected_revision is not None and revision(raw) != expected_revision:
            raise _Conflict("someone else changed the accounts list while this "
                            "one was open. Reload the page and try again — "
                            "nothing was removed.")
        # THE TOMBSTONE IS LAID INSIDE THIS LOCK, so a seed that takes the lock
        # next cannot see the deletion without also seeing the intent behind
        # it. It marks THE KEY THAT WENT, and only when one did: a remove that
        # found nothing removed nothing, and a tombstone for an id that was
        # never there would silently fence off a name the owner may yet want.
        def _tomb(key):
            if key not in removed:
                removed.append(key)
            return True

        # TYPED, NOT TRIED IN ORDER: a handle-shaped id resolves only against
        # unusable keys and never pops a literal, so the two namespaces cannot
        # cross even on a file where a healthy row was hand-named like one.
        if _HANDLE_RE.match(account_id):
            for key in [k for k in raw if _unusable_key(k)]:
                if bad_handle(key) == account_id:
                    raw.pop(key)
                    # NOT TOMBSTONED, and the asymmetry is the point: this key
                    # is one no id can spell, so no seed can ever mint it back.
                    # A tombstone here would be a fence around nothing, written
                    # into the owner's file forever.
                    return True
            return False
        return raw.pop(account_id, None) is not None and _tomb(account_id)

    try:
        gone, err = _update_doc(_mutate, accounts_path)
    except _Conflict as e:
        return False, str(e), "conflict"
    except ValueError as e:
        return False, str(e), "refused"
    if err:
        return False, err, "refused"
    if not gone:
        return False, "there is no declared account called '%s'." % account_id, "refused"
    pk.event("accounts-rm", account_id, "removed from the declared inventory")
    return True, None, None


# ---------------------------------------------------------------------------
# the join — three states, rendered as three things, never merged
# ---------------------------------------------------------------------------

def measured_key(name):
    """An OPAQUE, ID-SHAPED HANDLE for one measured account name.

    THE MASK IS NOT A KEY, and that is the whole reason this exists. A surface
    that renders `mask_identity` and then has to say WHICH account the owner
    just clicked cannot post the masked string back: two accounts whose local
    parts start with the same letter on the same domain mask to one string, and
    a door that resolved a mask would either bind the wrong subscription or
    refuse both — leaving the owner unable to describe either of them from the
    page at all. So the name travels as a handle: derived from the name, never
    reversible into it, and shaped like an id so it survives every door that
    validates one.

    A DIGEST IS NOT A SECRET-KEEPER — anyone who already knows an address can
    confirm it — and it does not need to be. The claim is narrower and exactly
    the module's claim: no human rendering of this surface PRINTS an address."""
    return "m-" + hashlib.sha256(str(name).encode("utf-8")).hexdigest()[:16]


def resolve_measured_key(key, measured):
    """The measured name one handle came from, or None when nothing measures it
    any more. The resolution is against what the provider reports RIGHT NOW, so
    a handle from a page that has been open for an hour resolves to the account
    it named or to nothing — never to a different one."""
    if not key:
        return None
    for m in measured or []:
        name = m.get("name") if isinstance(m, dict) else None
        if name and measured_key(name) == key:
            return name
    return None


def join(declared, measured):
    """Declared rows x measured quota rows -> the three honest states.

    {"matched": {<measured name>: <declared row>},
     "declared_only": [...],          # normal: an X or HF account has no provider
     "measured_only": [{"name", "name_masked", "key", "suggest_id"}, ...],
     "aliases": [...]}                # another home of a subscription already
                                      # described — never an undescribed row

    The key is the measured row's `name`, which is the identity
    providers.accounts() mints and web_quota.get_creds keys everything else on.
    A declared row joins ONLY through an explicit `measured_as`: guessing from
    the vendor would silently bind the wrong subscription to the wrong quota,
    which is the failure that made a declared layer necessary in the first
    place.

    AN UNDESCRIBED ACCOUNT IS A ROW, NOT A NAME, AND THAT IS THE MASKING SEAM.
    A bare list of names left every consumer to mask for itself; the card did
    not, and printed provider addresses whole in the one place this module's
    own docstring promises it masks. So the row carries the three things a
    surface actually needs — the masked spelling to PRINT, the handle to POST
    BACK and the id to SUGGEST — and `name` (the whole spelling, which only the
    join and `--json` need) is the one field a human-facing projection drops.
    The mask cannot be the handle: see `measured_key`."""
    rows = [m for m in (measured or []) if isinstance(m, dict)]
    names = [m.get("name") for m in rows]
    known = set(n for n in names if n)
    matched, declared_only = {}, []
    for row in declared or []:
        target = row.get("measured_as")
        if target and target in known:
            matched.setdefault(target, row)
        else:
            declared_only.append(row)
    claimed = set(matched)
    # A SECOND HOME OF A SUBSCRIPTION HE HAS ALREADY DESCRIBED IS AN ALIAS,
    # NEVER AN UNDESCRIBED ACCOUNT. `measured_as` names ONE credential home, so
    # matching by name alone left the OTHER home of the same login on the
    # "not described yet" strip — and the button there MINTS A RECORD. One
    # click and there are two rows for one bill, which is the alias row his
    # ruling forbids ("one row per PAID SUBSCRIPTION; homes and seats attach to
    # a row as aliases and never mint their own"), offered by the card that
    # exists to clear duplicates.
    #
    # THE SUBSCRIPTION IS WHAT DECIDES IT, on both sides: the declared rows say
    # which subscriptions are described, the measured rows say which
    # subscription each home is on. A home whose login helm cannot read keys to
    # nothing and stays undescribed, which is right — helm cannot prove it is
    # one of these, so it still asks.
    described = set()
    for row in declared or []:
        key = subscription_key(declared_identity(row, rows))
        if key:
            described.add(key)
    by_name = {m.get("name"): m for m in rows}

    def row_for(n):
        return {"name": n, "name_masked": mask_identity(n),
                "key": measured_key(n),
                # the id to suggest, computed HERE because the slug is
                # taken from the whole name and the card is given only
                # the mask (`declSlug`'s rule, one side of the wire over)
                "suggest_id": _slug(n.partition("@")[0])}

    aliases, measured_only = [], []
    for n in names:
        if not n or n in claimed:
            continue
        key = subscription_key(measured_identity(by_name.get(n) or {}))
        # AND IT IS NOT DROPPED ON THE FLOOR. An alias is still a home helm
        # measures; it rides the row of the subscription it belongs to (the
        # table and the fill screen both group on that key) and it stays in the
        # join, which is what `helm accounts --json` reads. Losing it here
        # would be the same class pointed the other way.
        (aliases if key and key in described else measured_only).append(
            dict(row_for(n), **({"subscription": key} if key else {})))
    return {"matched": matched, "declared_only": declared_only,
            "measured_only": measured_only, "aliases": aliases}


# ---------------------------------------------------------------------------
# the seed — day one is not empty, and nothing is invented
# ---------------------------------------------------------------------------

def _slug(text):
    return re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()[:MAX_ID_CHARS]


#: The owner's rule for this inventory, in his words: "one row per PAID
#: SUBSCRIPTION, keyed by vendor plus login identity. Homes and seats attach to
#: a row as aliases and never mint their own row."
#:
#: A home is not a subscription and a default is not an account. He read his own
#: card and could not recognise half of it, because the seed keyed on the
#: provider's HANDLE for a credential directory rather than on the thing he
#: actually pays for: two credhomes logged into one account made two rows, and
#: the `~/.claude` default — which is whichever account he has selected — made a
#: third. `identity` is the one key that collapses all three back into the one
#: bill, and every function below keys on it.
DEFAULT_HANDLE_RE = re.compile(r"^\(default-[a-z]+\)$")


def subscription_identity(vendor, name=None, email=None):
    """(vendor, login) for one PAID SUBSCRIPTION, or None when the handle names
    no login at all.

    THE LOGIN IS THE KEY, NOT THE HANDLE. `providers.accounts` mints a name per
    credential HOME and disambiguates two homes holding one login by appending
    `#<home>` — its own comment says "two distinct homes, same identity" — so
    the part before the `#` is the provider's own statement of which of these
    are one account. The `email` it reports beside the name is better still: it
    is read from the credential itself, so a home whose NAME lies about who is
    logged into it — the live fleet has one, and the quota table flags it
    `drifted` — still keys to the account that is really billed.

    A DEFAULT HOME IS A POINTER AND NEVER A SUBSCRIPTION. `(default-claude)` is
    whichever account is currently selected; on a live host it reads as one
    of the owner's own logins. Keyed by its handle it mints a row for an
    account the owner already pays for under its own name, which is one of
    the duplicates he could not place. Keyed by its login it collapses onto
    that row. With no login
    readable it keys to NOTHING — it mints no row at all, rather than inventing
    a subscription out of a pointer. It stays visible either way: the quota
    table measures it, and `join` reports it as a measured row nobody has
    described, so the owner can still describe it deliberately."""
    who = (email or "").strip().lower()
    if not who:
        handle = (name or "").strip()
        if DEFAULT_HANDLE_RE.match(handle.lower()):
            return None
        who = handle.partition("#")[0].strip().lower()
    vendor = (vendor or "").strip().lower()
    if not who or not vendor:
        return None
    return (vendor, who)


def subscription_key(identity):
    """An OPAQUE, ID-SHAPED HANDLE for one subscription, or None.

    THE KEY TRAVELS, THE LOGIN DOES NOT. Both halves of the quota table have to
    agree on which rows are the same subscription, and the only thing that
    decides that is a login — which on this host is an address. `measured_key`
    already made this trade for one account name and its reasoning holds
    unchanged here: the page needs something it can GROUP BY and hand back,
    never something it can read. Derived from the identity, not reversible into
    it, and shaped like an id so it survives every door that validates one."""
    if not identity:
        return None
    vendor, who = identity
    raw = "%s|%s" % (vendor, who)
    return "s-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def measured_identity(row):
    """The subscription one MEASURED row belongs to, or None."""
    if not isinstance(row, dict):
        return None
    return subscription_identity(row.get("provider"), row.get("name"),
                                 row.get("email"))


def declared_identity(row, measured=None):
    """The subscription one DECLARED row claims, or None when it claims none.

    A declared row states its claim through `measured_as`, which holds the
    provider's NAME for the account. Resolving that name against the measured
    rows recovers the login behind it, so the row and the measurement key
    alike. A `measured_as` that matches nothing measured is NOT discarded — an
    owner's row can be added by hand against a login this host cannot
    measure, and dropping its claim would let the next seed mint a second row
    for the account he had just told us about."""
    if not isinstance(row, dict):
        return None
    target = (row.get("measured_as") or "").strip()
    if not target:
        return None
    for m in measured or []:
        if isinstance(m, dict) and m.get("name") == target:
            return measured_identity(m)
    return subscription_identity(row.get("vendor"), target, None)


def declared_identities(rows, measured=None):
    """{identity: [id, ...]} over the declared rows that claim one — the map
    that answers "which of these are the same subscription?" for the seed's
    skip and for the owner's card alike."""
    out = {}
    for row in rows or []:
        key = declared_identity(row, measured)
        if key:
            out.setdefault(key, []).append(row.get("id"))
    return out


def client_rows(accounts_path=None, measured=None):
    """THE ONE PRODUCER OF A RECORD THE BROWSER SEES. Every door that hands the
    page an account row goes through here — the GET that draws the table and
    the SAVE that answers one cell — so the client can never be shown two
    shapes of the same record.

    IT IS NOT A CONVENIENCE WRAPPER, and the bug it closes is the whole reason
    it exists. The save answered `project(row)` while the GET answered
    `project(row)` PLUS the fields derived against the measured world. The one
    that went missing was `subscription`: the page groups its rows on that key,
    so a record created by typing in a cell came back without it, keyed itself
    by name instead, and split off as a SECOND row for an account that already
    had one — with the row he was typing in still blank, and the next cell
    creating a third. That is exactly the "make new" his ruling forbids, shipped
    by the door that was supposed to end it.

    `subscription` IS DERIVED AND LIVES HERE RATHER THAN IN `project`, and the
    reason is structural rather than stylistic: it is derived from a JOIN
    between the record and what the providers measure, and `project(row)` is
    handed one row and nothing else. It is NOT in `_FIELDS`, which is what
    keeps it out of the two places it must never reach — the payload validator,
    where a page could otherwise POST a key of its choosing and bind a record to
    a subscription it picked, and the stored-row validator, where persisting it
    would hand every older row a structure a later keying did not mint."""
    view = read(accounts_path)
    return dict(view, accounts=decorate_duplicates(view["accounts"], measured))


def decorate_duplicates(rows, measured=None):
    """The rows, each carrying what helm can say about whether it is a SECOND
    row for something already on the card. Derived at read time, never stored.

    THREE THINGS, AT THREE DIFFERENT STRENGTHS, and they are kept apart because
    the owner acts on them: a verdict he cannot check is worse than a question.

    `duplicate_of` is MEASURED: both rows name the same login, so they are the
    same bill and one of them should go.

    `points_at_default` is MEASURED too, and is the other half of the same
    complaint. `(default-claude)` is whichever account is currently selected —
    it is a pointer at a subscription, never one of its own. When the pointer
    resolves, its row ALSO carries `duplicate_of`; when it does not, this is
    all helm can say, and it is still enough to act on.

    `vendor_siblings` is a QUESTION and says so on the card. It is carried by
    the row that names NO login, and lists the other rows of its vendor: that
    row may be a real subscription nobody has bound yet, or a second name for
    one of the others. The `grok`/`x-premium` and `deepseek`/`deepseek-direct`
    pairs he named are the second, and the bare `codex` family row is too — it
    sat beside the six codex accounts it merely reaches. Helm cannot prove
    which from here, so it asks him instead of deciding for him.

    IT IS THE UNBOUND ROW THAT ASKS, AND ONLY THAT ROW. Hanging the question on
    every row of the vendor put a seven-name list on each of six accounts whose
    logins helm had already proven — noise of exactly the kind he opened this
    card to be rid of, and it would have buried the one row worth reading.

    THE MEASURED SIDE DEFAULTS TO THE SEED'S OWN SOURCE, and that is not a
    convenience. The quota card's merged rows and `_measured`'s raw ones spell
    an account differently — the merged ones drop the `email` the provider read
    out of the credential — so a caller that handed over the wrong one would get
    a card naming duplicates the seed does not skip, and skipping duplicates the
    card does not name. One source, one answer, no drift."""
    if measured is None:
        measured = _measured()
    dups = duplicate_groups(rows, measured)
    by_vendor = {}
    for row in rows or []:
        by_vendor.setdefault((row.get("vendor") or "").strip().lower(),
                             []).append(row)
    out = []
    for row in rows or []:
        account_id = row.get("id")
        target = (row.get("measured_as") or "").strip()
        siblings = by_vendor.get((row.get("vendor") or "").strip().lower(), [])
        unbound = not declared_identity(row, measured)
        out.append(dict(
            row,
            # THE ONE THING THAT JOINS THIS ROW TO A MEASURED ONE, and it is
            # the SUBSCRIPTION rather than `measured_as`. `measured_as` names
            # ONE credential home, so a subscription reached through two homes
            # matches on one of them and the other reads as a second account.
            # Keyed here, every home under a login lands on the row it belongs
            # to.
            subscription=subscription_key(declared_identity(row, measured)),
            duplicate_of=dups.get(account_id, []),
            points_at_default=bool(DEFAULT_HANDLE_RE.match(target.lower())),
            vendor_siblings=([s.get("id") for s in siblings
                              if s.get("id") != account_id]
                             if unbound and len(siblings) > 1 else []),
        ))
    return out


def duplicate_groups(rows, measured=None):
    """{id: [the OTHER ids for this same subscription]} — DERIVED, never
    stored, and empty on a clean inventory.

    THE CARD HAS TO SAY WHICH ROWS ARE THE SAME BILL. The owner's complaint was
    not that duplicates existed, it was that he could not tell which of them
    was which: "some of them were dupes or unknown to me". A row that names its
    twin is a row he can act on, and `remove` now makes that action stick."""
    out = {}
    for ids in declared_identities(rows, measured).values():
        if len(ids) < 2:
            continue
        for account_id in ids:
            out[account_id] = [other for other in ids if other != account_id]
    return out


def _seed_id(provider, name, used):
    """A distinct, readable id for one measured account.

    THE ID IS NEVER AN ADDRESS: when the measured name is address-shaped the
    local part is what carries the meaning and the domain is dropped, so a key
    on the owner's page is a name rather than a way to reach anybody. A
    provider name that is NOT address-shaped has no domain to drop and is
    slugged whole — it was never an address to begin with.

    AND TWO MEASURED ACCOUNTS CAN SHARE A LOCAL PART — the live provider rows on
    this host contain such a pair. One id for both would mean the second row
    REPLACES the first under the same key, so one real subscription disappears
    from the inventory at seed time, silently, which is the class this module
    refuses everywhere else. The domain's first label separates them, then an
    ordinal, and a name that cannot be made distinct is skipped rather than
    allowed to overwrite."""
    local, _at, domain = name.partition("@")
    base = _slug("%s-%s" % (provider or "acct", local))
    if not base:
        return None
    if base not in used:
        return base
    tails = ([domain.split(".")[0]] if domain else []) + \
        [str(n) for n in range(2, 20)]
    for tail in tails:
        candidate = _slug(base + "-" + tail)
        if candidate and candidate not in used:
            return candidate
    return None


def seed_rows(measured=None, orca=None):
    """What to seed on an empty inventory, from what helm ALREADY knows.

    Every seeded row carries `seeded_from` and is marked unconfirmed, so the
    page says "seeded from <source>, please confirm" rather than presenting a
    derived guess as the owner's own words. Nothing here is invented: the
    measured rows come from the quota provider, the grok row is the owner's own
    sentence, and no other account is asserted."""
    rows = [{
        "id": "x-premium",
        # THE CATALOG'S SPELLING OF THE SAME VENDOR, and the owner's: the
        # `grok` family is billed by `xai`, and he hand-corrected this very row
        # to `xai` on his own card today. Spelled `x` it read as a different
        # vendor from the family that reaches it, so the seed offered him two
        # rows for one $7 subscription — the pair he named as duplicates.
        "vendor": "xai",
        "plan": "X Premium (about $7/month)",
        "price_month": "$7",
        "count": 1,
        "good_for": "Grok with live X/Twitter access — getting the latest from "
                    "Twitter, which other agents are blocked from",
        "not_for": "building; too little Grok quota",
        "reach": OWNER_ONLY + " until a seat is wired",
        "seeded_from": "the owner's own words",
    }]
    used = {r["id"] for r in rows}
    # A MEASURED ACCOUNT GETS NO ROW HERE, and that is the owner's ruling: "i
    # dont understand why the accounts we pay for [aren't handled] by making it
    # editable and adding fields intelligently, to compose instead of make new".
    # An account the quota table already shows IS a row on his screen. Minting a
    # declared record beside it made two rows out of one subscription and filled
    # the second with the seeder's own placeholder sentences — which is most of
    # why half his card read as accounts he did not recognise. The measured row
    # now carries its declared cells EMPTY, and filling one CREATES the record
    # against that same account: composition, rather than a second row to
    # reconcile afterwards.
    #
    # WHAT IS STILL SEEDED IS WHAT NOTHING MEASURES — a prepaid API balance, a
    # subscription with no seat, a login only Orca can see. Those have no
    # measured row to attach to, so a declared row is the only way they reach
    # the surface at all, which is the case this half exists for.
    rows.extend(_orca_rows(measured, used, orca))
    return rows


def measured_groups(measured):
    """[(identity, [rows...])] — the measured rows gathered into the
    SUBSCRIPTIONS they belong to, first-seen order, primary row first.

    ONE GROUP IS ONE BILL AND THEREFORE ONE ROW. Two of the live claude rows
    are one Max plan reached through two credential homes; keyed by name they
    seeded two ids differing only by the domain tail `_seed_id` appends to
    break a collision, which is the pair the owner read back to us as
    duplicates he could not place.

    THE PRIMARY IS THE PLAINEST NAME IN THE GROUP, because it becomes the row's
    `measured_as` and therefore the one handle the join binds on. A `#home`
    suffix exists only to break a tie between two homes and a `(default-…)`
    handle points at whichever account is selected, so either would bind the
    subscription to the more fragile of its names: re-selecting a default or
    retiring one home would silently unbind a row the owner had described."""
    order, groups = [], {}
    for row in measured or []:
        identity = measured_identity(row)
        if not identity:
            continue
        if identity not in groups:
            order.append(identity)
            groups[identity] = []
        groups[identity].append(row)

    def rank(row):
        name = row.get("name") or ""
        return (bool(DEFAULT_HANDLE_RE.match(name.lower())), "#" in name, name)

    return [(identity, sorted(groups[identity], key=rank)) for identity in order]


def _orca_rows(measured, used, orca=None):
    """Rows for the claude subscriptions ORCA holds that no credential home on
    this host measures as.

    THE HOMES ARE NOT THE WHOLE ESTATE, and this is the hole the owner found by
    reading his own card: one of his Max plans was not listed at all. No claude
    home measures as that login — the home NAMED for it is logged in as a
    different account — so every source the seed had was blind to a plan he
    pays for every month. Orca keeps its own account
    store and states the login of each one, so it can see exactly the accounts a
    misnamed home hides.

    IT ONLY EVER ADDS WHAT NOTHING ELSE SAW. An Orca account that a home does
    measure is already a group above, and minting a second row for it here
    would be the duplication this whole pass exists to end.

    `orca` IS INJECTABLE FOR THE SAME REASON `families` IS. Left to itself this
    reads a real store on the real host, so every existing arm about the seed
    would silently start depending on which accounts happen to be logged into
    Orca on the machine running it — an assertion about the ambient tree rather
    than about the seeder."""
    have = {identity for identity, _group in measured_groups(measured)}
    rows = []
    for account in (accountseed.orca_identities() if orca is None else orca):
        email = account.get("email")
        identity = subscription_identity("anthropic", email, email)
        if not identity or identity in have:
            continue
        have.add(identity)
        account_id = _seed_id("anthropic", email, used)
        if not account_id:
            continue
        used.add(account_id)
        rows.append({
            "id": account_id,
            "vendor": "anthropic",
            "plan": "unknown plan",
            "count": 1,
            "good_for": NOT_DESCRIBED_FOR,
            "not_for": NOT_DESCRIBED_NOT,
            "reach": "anthropic",
            # THE LOGIN IS THE HANDLE HERE, and it is the honest one: no quota
            # row on this host carries this account, so there is no provider
            # spelling to bind to. `declared_identity` reads it as the identity
            # it is, which is what stops a later seed — or a home that finally
            # measures this login — from minting a second row beside it.
            "measured_as": email,
            "seeded_from": accountseed.SEEDED_FROM_ORCA,
        })
    return rows


def seed_candidates(measured=None, families=None, orca=None):
    """Everything a seed WOULD write: what helm measures, plus every provider
    family the seat catalog declares.

    TWO SOURCES, ONE LIST, AND THE MEASURED ONE WINS A COLLISION. A measured
    row binds to a real quota row through `measured_as` and a catalog row
    cannot; if both want one id, the row that can find its measurement is the
    one worth having.

    AND A VENDOR ALREADY ACCOUNTED FOR WINS TOO. A catalog family is a ROUTE to
    a vendor, not a bill from one: the `codex` family sat on the owner's card
    beside the six codex accounts it reaches, and he read it as a seventh
    subscription he did not recognise. A family whose vendor already has a row
    adds no account he does not have — only a second name for one — so it mints
    nothing. A family whose vendor has no row still mints one, because that is a
    vendor nobody has written down, which is the hole this source exists to
    fill."""
    rows = seed_rows(measured, orca)
    taken = {r["id"] for r in rows}
    vendors = {(r.get("vendor") or "").strip().lower() for r in rows}
    # …AND EVERY VENDOR THE QUOTA TABLE ALREADY MEASURES, which this has to
    # read from `measured` directly: a measured account is described in place
    # and mints no row here, so nothing else in this list carries its vendor.
    # Without it the bare `codex` family row stands beside the six codex
    # accounts it merely reaches, which is the row the owner could not place.
    for m in measured or []:
        if isinstance(m, dict):
            vendors.add((m.get("provider") or "").strip().lower())
    vendors.discard("")
    for candidate in accountseed.candidates(NOT_DESCRIBED_FOR,
                                            NOT_DESCRIBED_NOT, families):
        vendor = (candidate.get("vendor") or "").strip().lower()
        if candidate["id"] in taken or (vendor and vendor in vendors):
            continue
        taken.add(candidate["id"])
        if vendor:
            vendors.add(vendor)
        rows.append(candidate)
    return rows


def seed(measured=None, accounts_path=None, families=None, orca=None):
    """Add every account helm knows about that is NOT declared yet ->
    (written, skipped, error).

    IT ADDS AND IT NEVER TOUCHES. A row already under an id is left exactly as
    it is — not merged, not re-stamped, not re-marked unconfirmed — because the
    owner's own sentences are the only unrebuildable thing on this surface. An
    earlier spelling refused a non-empty inventory outright for that reason,
    which was safe and useless: the inventory stops being empty after the first
    seed, so every family added to the catalog afterwards could never reach his
    card and he was back to being asked about accounts nobody wrote down.
    Skipping by id keeps the guarantee and drops the uselessness.

    THE READ AND THE WRITES ARE ONE MUTATION, and that is the only way the
    sentence above can be true. Deciding which ids are missing OUTSIDE the lock
    and then writing one row at a time leaves a window the width of the whole
    seed: a save landing inside it is invisible to a seeder that read the file
    before it existed, and its derived placeholder lands on the owner's own
    first sentence. Two seeders racing do the same to each other. One `_update`
    closes it: whoever takes the lock second sees the other's rows and skips
    them.

    AND IT SKIPS WHAT HE HAS ALREADY ANSWERED, by id, by DELETION and by
    SUBSCRIPTION. Skipping by id alone was enough only while the seed's id was
    the only name an account could have: it re-minted a row he had deleted, and
    it minted a second row for a subscription he had already declared under a
    name of his own — which is how a row added by hand at his word could
    have been duplicated by the very pass that went looking for it.
    A candidate is skipped when a row on his card already claims the same
    subscription, whatever that row is called."""
    candidates = seed_candidates(measured, families, orca)

    def _mutate(raw, removed):
        written, skipped = [], []
        tombstoned = set(removed)
        # THE CLAIMS ON THE FILE, read once and kept current as rows land, so a
        # candidate cannot duplicate a row this same seed just wrote either.
        claimed, vendors = set(), set()
        for key, value in raw.items():
            if isinstance(value, dict):
                identity = declared_identity(dict(value, id=key), measured)
                if identity:
                    claimed.add(identity)
                vendors.add((value.get("vendor") or "").strip().lower())
        for m in measured or []:
            if isinstance(m, dict):
                vendors.add((m.get("provider") or "").strip().lower())
        vendors.discard("")
        for candidate in candidates:
            if candidate["id"] in raw:
                # NOT A FAILURE AND NOT A SILENCE. The row is already declared,
                # which is the ordinary outcome of every seed after the first.
                skipped.append("%s (already declared — left exactly as it is)"
                               % candidate["id"])
                continue
            if candidate["id"] in tombstoned:
                skipped.append("%s (you removed this one — it stays removed)"
                               % candidate["id"])
                continue
            identity = declared_identity(candidate, measured)
            if identity and identity in claimed:
                skipped.append("%s (already declared under another name — one "
                               "row per subscription)" % candidate["id"])
                continue
            # A VENDOR HE HAS ALREADY WRITTEN DOWN needs no second row from a
            # catalog family: the family is the ROUTE to that vendor's
            # accounts, and a row for it as well is a subscription he does not
            # hold. Checked against the FILE rather than the candidate list,
            # because his own rows are most of what that list cannot see.
            vendor = (candidate.get("vendor") or "").strip().lower()
            if vendor and not candidate.get("measured_as") and vendor in vendors:
                skipped.append("%s (you already have a %s row — this is the "
                               "route to it, not another account)"
                               % (candidate["id"], vendor))
                continue
            if len(raw) >= MAX_ACCOUNTS:
                skipped.append("%s (the inventory holds %d accounts, which is "
                               "the limit)" % (candidate["id"], len(raw)))
                continue
            row, bad = validate(candidate)
            if bad:
                skipped.append("%s (%s)" % (candidate["id"], bad))
                continue
            stored = dict(row)
            stored.pop("id", None)
            raw[row["id"]] = stored
            if identity:
                claimed.add(identity)
            vendors.add((row.get("vendor") or "").strip().lower())
            written.append(row)
        return written, skipped

    try:
        out, err = _update_doc(_mutate, accounts_path)
    except ValueError as e:
        return [], [], str(e)
    if err:
        return [], [], err
    written, skipped = out
    for row in written:
        pk.event("accounts", row["id"], "declared %s %s x%d"
                 % (row["vendor"], row["plan"], row["count"]))
    return [row["id"] for row in written], skipped, None


# ---------------------------------------------------------------------------
# what agents read
# ---------------------------------------------------------------------------

def agent_line(view=None):
    """ONE short line pointing an agent at the inventory, or "" when there is
    nothing to point at.

    DELIVERED ONLY WHERE RELEVANT. An empty inventory produces no line at all,
    because a pointer to nothing is the per-turn paragraph this must not become.
    It carries the COUNT and the VERB, never the contents: the verb is the live
    answer, and a list baked into prose is stale the moment the owner edits it."""
    try:
        view = view if view is not None else read()
    except Exception:                      # noqa: BLE001 — a pointer never raises
        return ""
    if view.get("unreadable"):
        return ("helm's declared account inventory is unreadable — run "
                "`helm accounts` for the reason; do not assume we have none.")[:POINTER_CAP]
    rows = view.get("accounts") or []
    if not rows:
        return ""
    line = ("helm holds %d declared account(s) — run `helm accounts` for "
            "vendor/plan/count and what each one is for; never ask the owner "
            "to recount them." % len(rows))
    return line[:POINTER_CAP]


#: A per-row line is a GLANCE, not a page: the verb prints one for every
#: declared account and a fleet reads them together.
ROW_LINE_CAP = 120


def agent_rows(view=None):
    """One short line per declared account — vendor, plan, price, how it is
    billed and how much it allows.

    THE POINTER SAYS THERE IS AN ANSWER; THIS IS THE ANSWER. `agent_line` is
    the JIT gloss and carries a count and a verb on purpose (a list baked into
    injected prose is stale the moment the owner edits one row). This is what
    the verb it points at PRINTS, computed at read time, so an agent that runs
    it sees "opencode-go: opencode Go · subscription · 20 requests per week"
    instead of having to open each row.

    BILLING AND ALLOWANCE COME FROM THE ONE PRODUCER (`accountfields.bits`),
    the same one the headline uses, so this line and the card cannot say the
    account is billed two ways."""
    try:
        view = view if view is not None else read()
    except Exception:                      # noqa: BLE001 — a reading never raises
        return []
    out = []
    for row in view.get("accounts") or []:
        bits = ["%s %s" % (row.get("vendor") or "?", row.get("plan") or "?")]
        if row.get("count", 1) != 1:
            bits.append("x%d" % row["count"])
        if row.get("price_month"):
            bits.append("%s/mo" % row["price_month"])
        bits.extend(accountfields.bits(row))
        if row.get("needs_confirm"):
            bits.append("unconfirmed")
        out.append(("%s: %s" % (row["id"], " · ".join(bits)))[:ROW_LINE_CAP])
    return out


def store_command(_view=None):
    """The runnable pair that teaches the JIT lane this pointer.

    PRINTED, NOT RUN. The store is the owner's knowledge base and a verb that
    silently writes canon into it is a verb nobody reviewed; these are two real
    commands whose flags this repo's instructions-are-runnable rung checks."""
    summary = ("The owner's paid accounts are declared in helm, not in an "
               "agent's memory: vendor, plan, count, price, what each is for "
               "and what it must not be used for. `helm accounts` is the live "
               "answer; `helm accounts show <id>` is one row.")
    return ["helm store add reference %s | %s |  | %s"
            % (STORE_ENTRY_ID, summary, ",".join(STORE_KEYWORDS)),
            "helm store gloss %s --set %s" % (STORE_ENTRY_ID, STORE_GLOSS)]


# ---------------------------------------------------------------------------
# the verb
# ---------------------------------------------------------------------------

_USAGE = """usage: helm accounts [--json]                  the declared inventory
       helm accounts show <id> [--json]        one account, every field
       helm accounts set <id> --vendor V --plan P --count N --good-for TEXT
                             --not-for TEXT --reach R [--price P] [--measured-as NAME]
                             [--renews-on YYYY-MM-DD] [--notes TEXT] [--confirm]
                             [--billing sub|payg|prepaid|free] [--allowance TEXT]
                             [--key-where TEXT]
       helm accounts rm <id>                   DELETE one declared account
       helm accounts seed [--apply]            add a row for every account helm
                                               knows about — measured, and every
                                               provider family the seat catalog
                                               declares — that is not declared
                                               yet; a declared row is untouched
       helm accounts line                      the pointer for agents, then one
                                               line per account: vendor, plan,
                                               price, billing, allowance
       helm accounts teach                     print the store commands that put
                                               that pointer on the JIT lane
  The OWNER-DECLARED account inventory: what we pay for, what each one is FOR and
  what it must NOT be used for. Authoritative about inventory and intent only —
  never about anything measured (active, healthy, headroom: those are the quota
  provider's). NO SECRETS: there is no field for a key, token, password, cookie
  or recovery code, and a value shaped like one is refused. The one field that
  may hold an address is `measured_as` — the quota provider's own account name,
  which is how a declared row finds its measured one; every human rendering
  masks it and only `--json` carries it whole. A save MERGES: a field you do
  not pass keeps what is on disk, and an empty value clears it. The owner edits
  this on the web cockpit's quota tab; this verb is for agents. At most %d
  accounts; HELM_ACCOUNTS overrides the path.
  --billing is a closed set (%s) and renders as a word; --allowance is free
  text ("20 requests per week", "a 5h window") that helm also READS for a
  cap/unit/cadence without ever refusing what it cannot read; --key-where says
  WHERE a key is kept — a credhome, an env file, a provider dashboard — and is
  refused if what you paste is the key instead of the place.
""" % (MAX_ACCOUNTS, " | ".join(accountfields.BILLING))

_SET_FLAGS = {"--vendor": "vendor", "--plan": "plan", "--count": "count",
              "--good-for": "good_for", "--not-for": "not_for",
              "--reach": "reach", "--price": "price_month",
              "--measured-as": "measured_as", "--renews-on": "renews_on",
              "--notes": "notes", "--billing": "billing",
              "--allowance": "allowance", "--key-where": "key_where"}


def _set_args(rest):
    """`set <id> --flag value...` — values join, so a sentence needs no quoting.
    Unknown or repeated flags REFUSE rather than becoming owner-facing text."""
    if not rest:
        raise ValueError("set needs an account id")
    payload, current = {"id": rest[0]}, None
    for token in rest[1:]:
        if token == "--confirm":
            payload["confirmed"] = True
            current = None
        elif token in _SET_FLAGS:
            current = _SET_FLAGS[token]
            if current in payload:
                raise ValueError("%s may appear only once" % token)
            payload[current] = []
        elif token.startswith("--"):
            raise ValueError("unknown accounts set option %s (known: %s)"
                             % (token, " ".join(sorted(_SET_FLAGS))))
        elif current is None:
            raise ValueError("stray value %r — every value follows a flag"
                             % token[:40])
        else:
            payload[current].append(token)
    for key, value in list(payload.items()):
        if isinstance(value, list):
            payload[key] = " ".join(value).strip()
    return payload


def _print_rows(view, measured=None):
    """THE HUMAN RENDERING, AND IT MASKS. Every measured name printed here goes
    through `mask_identity`, including the measured-but-undeclared list, which
    is the provider's own naming and on this host is mostly addresses. `--json`
    is the other surface and carries them whole: it is what the join reads, and
    a masked join key joins nothing."""
    rows = view["accounts"]
    linked = join(rows, measured or [])
    print("helm accounts (%d declared · %s):" % (len(rows), view["path"]))
    for row in rows:
        mark = " [seeded — confirm]" if row["needs_confirm"] else ""
        print("  %-20s %s%s" % (row["id"], row["headline"], mark))
        print("      for: %s" % row["good_for"])
        print("      NOT: %s" % row["not_for"])
        # BILLING IS SAID ON EVERY ROW, including the rows that do not say it.
        # An account whose billing nobody has declared is exactly the row the
        # owner opened this surface to find, and a line that only appears when
        # the answer exists makes the gap invisible.
        print("      billing: %s%s%s"
              % (row["billing_word"],
                 "" if not row.get("allowance") else
                 "  · allows %s" % row["allowance"],
                 "" if not row.get("key_where") else
                 "  · key at %s" % row["key_where"]))
        print("      reach: %s%s" % (row["reach"], "" if not row["measured_as"]
                                     else "  · measured as %s%s"
                                     % (row["measured_as_masked"],
                                        "" if row["measured_as"] in linked["matched"]
                                        else " (declared, not measured)")))
    t = view["totals"]
    print("  totals: %d account(s), %d unit(s), $%s/month%s"
          % (t["accounts"], t["units"], t["monthly_spend"],
             " (%d unpriced)" % t["unpriced"] if t["unpriced"] else ""))
    if linked["measured_only"]:
        print("  measured but NOT declared (%d): %s"
              % (len(linked["measured_only"]),
                 ", ".join(m["name_masked"]
                           for m in linked["measured_only"][:8])))
        print("      describe one: helm accounts set <id> --vendor V --plan P "
              "--count 1 --good-for TEXT --not-for TEXT --reach R "
              "--measured-as <measured name>")
    for bad in view["bad"]:
        # THE HINT IS RUNNABLE. The escaped id is the rendering and `rm`
        # refuses it; the handle is what actually removes this row.
        print("  SKIPPED '%s': %s (still on disk — fix it or "
              "`helm accounts rm %s`)" % (bad["id"], bad["why"], bad["handle"]))


def _measured():
    """The measured rows, or [] when this host has no quota provider. NEVER a
    raise: the declared inventory is readable on a machine with no provider at
    all, which is most of the point."""
    try:
        from .providers import ProviderError, default_provider
        try:
            return default_provider().accounts()
        except ProviderError:
            return []
    except Exception:                      # noqa: BLE001 — the join is optional
        return []


def cmd_accounts(args):
    """accounts — the owner-declared account inventory (what we pay for, what
    each is for).

    EVERY BRANCH GUARDS ITS TAIL BEFORE IT READS OR WRITES ANYTHING. This verb
    both reads the owner's inventory and rewrites it, so an unguarded tail
    carries a token helm does not have into the work: `helm accounts seed
    --bogus --apply` is a WRITE that answers 0, and `helm accounts --json
    --bogus` answers as though the token existed and teaches him a flag. A
    flag helm does not have refuses rc 2 and names itself, which is
    `guard_tail`'s contract and the tree's law for every `--apply` reader.

    `set` is the ONE branch guard_tail cannot hold, and not by omission: its
    values are unquoted sentences (`--good-for reading the live timeline`), so
    every word after a flag would read as junk. `_set_args` is its closed-set
    refusal — an unknown `--option` raises rc 2 naming the option, and a stray
    value with no flag in front of it is refused too."""
    from .cli import guard_tail, suggest
    args = list(args or [])
    verb = args[0] if args else ""
    if verb in ("-h", "--help", "help"):
        print(_USAGE)
        return 0
    # the tail split into the flags a subverb knows and the operands it takes
    # positionally, so `show --json <id>` and `show <id> --json` stay the same
    # command while the flags still face a closed set. Each guard carries the
    # ONE-LINE synopsis of its own branch rather than _USAGE: guard_tail prints
    # what it is given inside the refusal, and the whole block in parentheses
    # buries the token that was actually wrong.
    tail = args[1:]
    flagged = [a for a in tail if a.startswith("-")]
    operands = [a for a in tail if not a.startswith("-")]
    if not verb or verb == "--json":
        rc = guard_tail("helm accounts", args, flags=("--json",),
                        usage="accounts [--json]")
        if rc is not None:
            return rc
        try:
            view = read_strict()
        except AccountsUnreadable as e:
            print("helm accounts: " + str(e), file=sys.stderr)
            return 1
        if verb == "--json":
            print(json.dumps(dict(view, join=join(view["accounts"], _measured())),
                             indent=2, ensure_ascii=False))
            return 0
        if not view["accounts"] and not view["bad"]:
            print("helm accounts: nothing declared yet — the owner adds these "
                  "on the cockpit's quota tab, or `helm accounts seed --apply` "
                  "adds a row for every account helm already knows about.")
            return 0
        _print_rows(view, _measured())
        return 0
    if verb == "show":
        rc = guard_tail("helm accounts show", flagged, flags=("--json",),
                        usage="accounts show <id> [--json]")
        if rc is not None:
            return rc
        rest = operands
        if len(rest) != 1:
            print(_USAGE, file=sys.stderr)
            return 2
        try:
            view = read_strict()
        except AccountsUnreadable as e:
            print("helm accounts: " + str(e), file=sys.stderr)
            return 1
        row = next((r for r in view["accounts"] if r["id"] == rest[0]), None)
        if not row:
            print("helm accounts: no declared account '%s'%s"
                  % (str(rest[0])[:40].encode("unicode_escape").decode("ascii"),
                     suggest(rest[0], [r["id"] for r in view["accounts"]])),
                  file=sys.stderr)
            return 1
        if "--json" in args:
            print(json.dumps(row, indent=2, ensure_ascii=False))
            return 0
        for field in _FIELDS:
            if field == "billing":
                # the word, and it is printed even when nothing was declared —
                # `show` is where he comes to find out what a row is missing
                print("  %-12s %s" % (field, row["billing_word"]))
                continue
            if row.get(field) in (None, "", False):
                continue
            # the join key is a provider-minted name and often an address; the
            # human rendering masks it, `--json` above carries it whole
            value = row["measured_as_masked"] if field == "measured_as" \
                else row[field]
            print("  %-12s %s" % (field, value))
        if row.get("allowance_parsed"):
            parsed = row["allowance_parsed"]
            print("  %-12s helm reads that as %s%s per %s"
                  % ("(allowance)",
                     "" if parsed["cap"] is None else "%s " % parsed["cap"],
                     parsed["unit"] or "use", parsed["cadence"]))
        if row.get("measured_as") != row.get("measured_as_masked"):
            print("  (measured_as is masked here — `helm accounts show %s "
                  "--json` carries the name the join needs)" % row["id"])
        return 0
    if verb == "set":
        try:
            payload = _set_args(args[1:])
        except ValueError as e:
            print("helm accounts: " + str(e), file=sys.stderr)
            print(_USAGE, file=sys.stderr)
            return 2
        row, err, _code = save(payload)
        if err:
            print("helm accounts: " + err, file=sys.stderr)
            return 1
        print("helm accounts: %s — %s" % (row["id"], row["headline"]))
        return 0
    if verb == "rm":
        rc = guard_tail("helm accounts rm", flagged, usage="accounts rm <id>")
        if rc is not None:
            return rc
        rest = operands
        if len(rest) != 1:
            print(_USAGE, file=sys.stderr)
            return 2
        _ok, err, _code = remove(rest[0])
        if err:
            print("helm accounts: " + err, file=sys.stderr)
            return 1
        print("helm accounts: %s removed from the declared inventory "
              "(the account itself is untouched)" % rest[0])
        return 0
    if verb == "seed":
        # BEFORE _measured(), before seed_rows, before the write: this is the
        # branch that made the census red, and `--apply` here is a write.
        rc = guard_tail("helm accounts seed", tail, flags=("--apply",),
                        usage="accounts seed [--apply]")
        if rc is not None:
            return rc
        measured = _measured()
        if "--apply" not in args:
            # THE DRY RUN SAYS WHICH ONES ARE NEW. Listing every candidate on a
            # non-empty inventory reads as "this will write all of these",
            # which is the one thing --apply will not do.
            try:
                have = {a["id"] for a in read()["accounts"]}
            except Exception:                  # noqa: BLE001 — a dry run never raises
                have = set()
            print("helm accounts seed (dry run — add --apply to write):")
            for row in seed_candidates(measured):
                print("  %-20s %s · %s x%d — %s%s"
                      % (row["id"], row["vendor"], row["plan"], row["count"],
                         row["seeded_from"],
                         "  [already declared — untouched]"
                         if row["id"] in have else ""))
            return 0
        written, skipped, err = seed(measured)
        if err:
            print("helm accounts: " + err, file=sys.stderr)
            return 1
        print("helm accounts: added %d (%s)"
              % (len(written), ", ".join(written) or "nothing was missing"))
        for s in skipped:
            print("  skipped %s" % s)
        return 0
    if verb == "line":
        rc = guard_tail("helm accounts line", tail, usage="accounts line")
        if rc is not None:
            return rc
        line = agent_line()
        if line:
            print(line)
        # …and then the answer the pointer points at. The pointer is FIXED TEXT
        # plus a count so it can be injected without going stale; these are
        # computed now, which is why they may carry the contents.
        for row_line in agent_rows():
            print("  " + row_line)
        return 0
    if verb == "teach":
        rc = guard_tail("helm accounts teach", tail, usage="accounts teach")
        if rc is not None:
            return rc
        for command in store_command():
            print(command)
        return 0
    safe = str(verb)[:40].encode("unicode_escape").decode("ascii")
    print("helm accounts: unknown verb '%s'%s" % (
        safe, suggest(verb, ("show", "set", "rm", "seed", "line", "teach"))),
        file=sys.stderr)
    print(_USAGE, file=sys.stderr)
    return 2
