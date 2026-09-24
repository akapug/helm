#!/usr/bin/env python3
"""The three fields the owner asked for on top of :mod:`helm.accounts`: HOW a
row is billed, HOW MUCH it allows, and WHERE its key is kept.

A SIBLING MODULE RATHER THAN THREE MORE BLOCKS IN accounts.py. That file is
already the longest thing on this surface and it owns one subject — what an
account IS and what it is FOR. The grammar of a billing enum, of a free-text
allowance and of a location label is a second subject with its own rules, and
it is the half a reviewer has to read closely: the key-where rung is a refusal.
accounts.py keeps the door (`validate`), this module keeps the grammar.

BILLING IS A CLOSED SET AND RENDERS AS A WORD. Four values cover everything the
owner pays for and an open field would give the fleet four spellings of
"subscription" to reconcile. It is stored as the token he typed on the flag and
rendered through ONE map, so the table, `show`, the quota tab and the agent
line cannot print different words about the same row — the drift the headline
boundary in accounts.project exists to stop.

ALLOWANCE IS FREE TEXT THAT IS *ALSO* PARSED, NEVER FREE TEXT THAT IS REJECTED.
"20 requests per week", "a 5h window", "unlimited as far as I know" and "no
idea" are all things he will type and all of them are true. So the text he
typed is what is STORED, and the structure is DERIVED from it on the way out
(`parse`): a parse that failed costs him nothing, and a parse that succeeded
costs no byte on disk. Deriving rather than storing is deliberate — a parsed
copy beside the text is a second answer that goes stale the moment the parser
improves, and every older row would then carry a structure this code did not
mint (helm's additive-derived-field class).

KEY_WHERE IS A LOCATION, AND A LOCATION IS NOT A KEY. The field answers "which
credhome, env file or provider dashboard holds it" so nobody has to guess where
to look — and it is the one field on this surface most likely to receive a
paste of the thing itself. accounts.secret_reason is the tree's grammar for
key-shaped material and runs first. It is not enough on its own here: its blob
rung needs 32 characters and its prefix rung needs a known prefix, so a short
opaque token — exactly the shape of a paste from a provider dashboard — walks
through a scanner built for the general field. `key_where_reason` adds the rung
this field needs and NAMES the field in the refusal without echoing the value.
"""
import re

#: The closed set, in the spelling the flag takes.
BILLING = ("sub", "payg", "prepaid", "free")

#: The ONE rendering. A row's billing appears in four places and must read the
#: same in all of them.
BILLING_WORDS = {"sub": "subscription", "payg": "pay-as-you-go",
                 "prepaid": "prepaid", "free": "free"}

#: What a row with no billing says. NEVER a default: "free" would be a guess
#: about money, and a guess about money is the one this card must not make.
BILLING_UNDECLARED = "not declared"

MAX_ALLOWANCE = 80
MAX_KEY_WHERE = 80

#: "<N> <unit> per <cadence>" — the shape the owner writes most.
_PER_RE = re.compile(
    r"\A(?:about\s+|~\s*)?(\d+(?:[.,]\d+)?)\s*([A-Za-z][A-Za-z /-]{0,20}?)"
    r"\s+(?:per|a|every|/)\s*([A-Za-z][A-Za-z-]{0,15})\b", re.I)
#: "a 5h window", "5 hour window" — a rolling window, which is a cadence too.
_WINDOW_RE = re.compile(
    r"\A(?:a\s+|about\s+|~\s*)?(\d+(?:[.,]\d+)?)\s*(h|hr|hrs|hour|hours|"
    r"d|day|days)\b\s*(?:rolling\s+)?window", re.I)
#: A bare cadence with no number: "weekly", "resets daily".
_CADENCE_RE = re.compile(r"\b(weekly|daily|monthly|hourly|yearly)\b", re.I)

_CADENCE_WORD = {"weekly": "week", "daily": "day", "monthly": "month",
                 "hourly": "hour", "yearly": "year"}
_UNIT_WORD = {"h": "hour", "hr": "hour", "hrs": "hour", "hour": "hour",
              "hours": "hour", "d": "day", "day": "day", "days": "day"}

#: An unbroken run of key characters, this long, carrying at least one digit.
#: A LOCATION HAS STRUCTURE — a path, a home name, two words, a dashboard's
#: name — and the structure shows as whitespace between its parts. A run with
#: no whitespace in it is what a pasted token looks like, and it is under
#: every bound accounts.secret_reason enforces.
#:
#: THE RUN IS FOUND INSIDE THE VALUE, never matched against the whole of it: a
#: token pasted after a few words of prose is the same token. THE DIGIT IS
#: WHAT SPARES ORDINARY PATHS: "~/.helm/creds/openrouter.env" is one 26-
#: character run over this alphabet and carries no digit, so it passes, while
#: a 16-character hex token does not.
_OPAQUE_MIN = 16
_OPAQUE_RUN = re.compile(r"[A-Za-z0-9_+=/.-]{%d,}" % _OPAQUE_MIN)
#: Separate places digits appear in one run. One group is how language and
#: filenames carry a number ("25th-anniversary", "openrouter2.env"); two or
#: more is how a key carries them.
_DIGIT_RUN = re.compile(r"\d+")


def opaque_run_reason(field, value):
    """A plain sentence when FIELD's value carries a key-shaped run, else None.

    Shared by every free-text field a key can be pasted into, so one grammar
    decides and two fields cannot drift apart. THE SENTENCE NAMES THE FIELD
    AND THE LENGTH AND NOTHING ELSE — echoing the run would print the secret
    into the log the refusal is meant to keep it out of."""
    if not isinstance(value, str) or not value.strip():
        return None
    for run in _OPAQUE_RUN.findall(value):
        # A PATH ANCHOR IS A LOCATION, and this is the discriminator a key
        # never crosses: a pasted token does not lead with a path marker, and
        # the answer he is being asked for usually does. Without it the rung
        # refuses "~/.helm/creds/openrouter2.env" — his own correct answer to
        # "where is the key kept" — and a masked "that looks like a key" on
        # that is the failure that makes the screen read as broken. The digit
        # clause still stands, so a bare "deepseek-direct.env" is unaffected
        # and a token with no anchor is still refused.
        if run.startswith(("/", ".")):
            continue
        if not any(c.isalpha() for c in run):
            continue
        # TWO CLAUSES, BECAUSE PROSE AND PATHS CARRY DIGITS TOO. A run with no
        # punctuation at all is a token when it mixes a digit in: that is the
        # original rung's insight and it stands. A run that DOES carry
        # punctuation is ordinary language — "25th-anniversary" is sixteen
        # characters with a digit — so it is a token only when digits appear
        # in two or more SEPARATE places. English puts them in one leading
        # segment; a key interleaves them.
        broken = any(c in "/.-" for c in run)
        groups = len(_DIGIT_RUN.findall(run))
        if (not broken and groups) or groups >= 2:
            return ("%s carries a %d-character run with no spaces in it, "
                    "which is what a key looks like and not what a place "
                    "looks like. helm stores WHERE the key is kept — 'the "
                    "codex credhome', 'the provider dashboard', an env file's "
                    "name — and never the key itself." % (field, len(run)))
    return None


def clean_billing(raw):
    """One billing token, or None when it is not declared. Raises ValueError
    naming every value that IS legal — a refusal that does not say what would
    have worked makes him guess at a closed set."""
    if raw is None or not str(raw).strip():
        return None
    value = str(raw).strip().lower()
    if value in BILLING:
        return value
    return _refuse(value)


def _refuse(value):
    raise ValueError(
        "billing must be one of %s — that is not one of them. 'sub' is a monthly "
        "or yearly subscription, 'payg' is billed per use, 'prepaid' is credit "
        "bought up front, 'free' costs nothing."
        % (", ".join(BILLING),))


def billing_word(value):
    """The ONE human spelling of a billing token, including the absent one."""
    return BILLING_WORDS.get(value) or BILLING_UNDECLARED


def parse(text):
    """The structure inside a free-text allowance, or None.

    {cap, unit, cadence} when the text says a number of something per
    something; {cap: None, unit: None, cadence: ...} when it names only a
    cadence. NEVER raises and never refuses: the caller stores the text either
    way, and this is a reading of it rather than a gate in front of it."""
    if not isinstance(text, str) or not text.strip():
        return None
    value = text.strip()
    m = _WINDOW_RE.match(value)
    if m:
        return {"cap": _number(m.group(1)),
                "unit": _UNIT_WORD.get(m.group(2).lower(), m.group(2).lower()),
                "cadence": "window"}
    m = _PER_RE.match(value)
    if m:
        cadence = m.group(3).lower()
        return {"cap": _number(m.group(1)),
                "unit": m.group(2).strip().lower(),
                "cadence": _CADENCE_WORD.get(cadence, cadence)}
    m = _CADENCE_RE.search(value)
    if m:
        return {"cap": None, "unit": None,
                "cadence": _CADENCE_WORD[m.group(1).lower()]}
    return None


def _number(raw):
    value = float(raw.replace(",", "."))
    return int(value) if value == int(value) else value


def key_where_reason(value, secret_reason):
    """A plain sentence when a LOCATION label is really the key, else None.

    `secret_reason` is passed in rather than imported so this module stays a
    leaf of accounts.py instead of a cycle with it, and so there is still only
    ONE grammar of key-shaped material in the tree. That grammar runs first;
    this adds the rung the general scanner cannot have — it is built for fields
    where a long opaque word is ordinary prose, and here it is the defect.

    THE REFUSAL NEVER ECHOES THE VALUE, which is the whole file's law."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    reason = secret_reason("key_where", text)
    if reason:
        return reason
    return opaque_run_reason("key_where", text)


def key_present(value):
    """Does the owner say a key exists somewhere? DERIVED from the label, so a
    row cannot claim a key it never says the location of."""
    return bool(isinstance(value, str) and value.strip())


def bits(row):
    """The billing/allowance fragments a one-line rendering appends, in order.

    ONE PRODUCER FOR EVERY ONE-LINER — the headline, `helm accounts line` and
    the quota tab's fill header all take their words from here, so the three
    cannot say the account is billed three ways."""
    out = []
    if row.get("billing"):
        out.append(billing_word(row["billing"]))
    if row.get("allowance"):
        out.append(str(row["allowance"]))
    return out
