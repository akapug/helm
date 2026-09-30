"""A provider's billing or credential refusal, read off the seat's OWN last
turn, typed as a MONEY or REACH wall (task/3587).

THE GAP. Measured on the fleet: grok's last turn ended on "API Error: 402 Grok
Build usage balance exhausted" while proxywatch read its upstream HEALTHY and
`helm burn` read the family GREY, so the dispatch door admitted a new build
row to a seat that could not take a turn. The refusal was written down in
exactly one place, the seat's own transcript, and nothing read it there.

WHAT IS READ. The newest main-chain assistant record of the seat's newest
transcript tail (the reader `silent_drop` already uses). Claude Code records a
failed request as its own assistant record: `isApiErrorMessage` true, the
synthetic model, and text starting "API Error". A reply from a real model that
QUOTES such a line is not one, so a seat discussing this very module never
reads walled. A later record with text or a tool call is a SUCCESSFUL turn:
the seat recovered, and it reads live again with nothing to clear by hand.

WHAT IS TYPED, and only the label leaves this module, never the vendor bytes:

  MONEY  402 in any wording (payment required is the contract itself); a 429
         or an unstatused refusal whose wording says a quota, balance or
         credit is EXHAUSTED ("exceeded your current quota", "quota
         exceeded", "usage balance exhausted", "insufficient balance").
  REACH  403 whose wording asks for account verification: no request is
         served until a person verifies, and waiting does not clear it; a
         401 whose wording names the credential (auth, a token, a key): an
         expired OAuth token or a wrong key serves nothing until replaced.
  MONEY  also a 400 whose wording says the balance is spent ("your credit
         balance is too low"), which is how one vendor spells a 402.

A 429 THAT IS ORDINARY RATE LIMITING IS NOT A WALL. The discriminator is the
wording, read in two parts: the refusal must say something is EXHAUSTED (a
quota, balance or credit), and it must name NO short window. A per-second,
per-minute or per-hour limit, an RPM/TPM/RPS budget, a named window, or a
clause saying when to come back is a limit that clears by itself, so a
refusal carrying one is transient whatever else it says. Gemini's free tier
says both at once: RESOURCE_EXHAUSTED, "You exceeded your current quota",
then "Please retry in 41.2s" and a JSON `retryDelay`. When the refusal states
HOW LONG ("retry in 41.2s", "reset in 30s", "resets in 5 days", `retryDelay`)
the longest stated wait decides: up to `SHORT_RESET_S` (three hours) is
transient, and past it the wall stands, since a balance that comes back in
five days is spent for every purpose the fleet has today. A clause naming a
clock time ("try again at 5pm", "resets at 17:00") is transient: it is later
today. A bare "rate limit" 429 carries no exhaustion wording and is not a
wall either.

ONE REFUSAL NEVER WALLS A FAMILY. `family_walls` needs two standing walls:
two distinct seats, or two consecutive walled turns of one seat. One
transient refusal the wording could not type would otherwise wall the whole
family, move its work, and hold it there: a walled family takes no turns, so
nothing would prove it recovered until the wall aged out. The seat's own
reading (the door, the mover) still counts its one turn.

A STALE READING SAYS NOTHING. A wall older than `max_age_s()` (default six
hours, HELM_TURNWALL_MAX_AGE_S) is not counted: nobody has asked the seat
anything since, so it neither proves the wall still stands nor that it
cleared, and a flag minted on it would ration the family forever.
"""
import json
import os
import re
import time

MONEY, REACH = "money", "reach"
WALL, CLEAN = "WALL", "CLEAN"
MAX_AGE_ENV = "HELM_TURNWALL_MAX_AGE_S"
MAX_AGE_S = 6 * 3600
# How much of the transcript tail is read: silent_drop's bound, so one seat's
# read costs what the drop watchdog already pays for it.
RECENT_LINES = 400
# The longest stated wait that is still transient: a refusal saying it clears
# within this many seconds is rate limiting, one saying longer is a wall.
SHORT_RESET_S = 3 * 3600
# How many standing walls, across distinct seats or consecutive turns of one
# seat, it takes to wall a family.
FAMILY_WALLS = 2

_STATUS = re.compile(r"\b([1-5]\d\d)\b")
_EXHAUSTED = re.compile(
    r"exceeded[ _](?:your[ _])?(?:current[ _])?quota|quota[ _]exceeded|"
    r"insufficient[ _](?:balance|quota|"
    r"credits?|funds)|(?:quota|balance|credits?|usage)[\w ]{0,20}"
    r"(?:exhausted|depleted|used up)|out of (?:credits?|funds)|"
    r"billing (?:hard )?limit|credit balance is too low", re.I)
_TRANSIENT = re.compile(
    r"per[ _-]?(?:second|sec|minute|min|hour)\b|\b(?:rpm|tpm|rps|rpd)\b|"
    r"\bwindow\b|retry[ _-]?(?:after|in)\b|resets?[ _-]?(?:in|at)\b|"
    r"try again (?:in|at)\b|retry[ _-]?delay", re.I)
# A stated wait: "retry in 41.2s", "reset in 30s", "resets in 5 days", "try
# again in 20 minutes", "retry after 30s", and the JSON `"retryDelay": "41s"`.
_WAIT = re.compile(
    r"(?:retry|resets?|try again)[ _-]?(?:in|after)\s+(\d+(?:\.\d+)?)\s*"
    r"([a-z]*)|retry[ _-]?delay\W{0,4}(\d+(?:\.\d+)?)\s*([a-z]*)", re.I)
_UNIT_S = (("ms", 0.001), ("mil", 0.001), ("s", 1), ("m", 60), ("h", 3600),
           ("d", 86400), ("w", 7 * 86400))
_VERIFY = re.compile(r"\bverif(?:y|ied|ication)\b", re.I)
_CREDENTIAL = re.compile(
    r"auth|\btoken\b|api[ _-]?key|\bkey\b|credential", re.I)


def max_age_s():
    """HELM_TURNWALL_MAX_AGE_S, a whole number of seconds, else MAX_AGE_S."""
    raw = (os.environ.get(MAX_AGE_ENV) or "").strip()
    return int(raw) if raw.isdigit() and int(raw) > 0 else MAX_AGE_S


def _longest_wait(text):
    """The longest wait the refusal states, in seconds, else None. A number
    with no unit, or a unit this cannot read, is seconds (`retryDelay`'s own
    unit): the reading errs toward transient, never toward a wall."""
    waits = []
    for m in _WAIT.finditer(text):
        num, unit = (m.group(1), m.group(2)) if m.group(1) \
            else (m.group(3), m.group(4))
        unit = (unit or "s").lower()
        scale = next((v for k, v in _UNIT_S if unit.startswith(k)), 1)
        # "mo"/"month" starts with "m" and is not a minute.
        if unit.startswith("mo"):
            scale = 30 * 86400
        waits.append(float(num) * scale)
    return max(waits) if waits else None


def _transient(text):
    """Does the refusal say it clears by itself soon? A stated wait decides
    by its length; otherwise any window or come-back clause is transient."""
    wait = _longest_wait(text)
    if wait is not None:
        return wait <= SHORT_RESET_S
    return bool(_TRANSIENT.search(text))


def classify(text):
    """{"axis", "code", "label"} for a billing or credential refusal, else
    None. `text` is the refusal after "API Error"; a request-shaped error, an
    ordinary rate limit and a server error all answer None."""
    text = str(text or "")
    head = text.split("{", 1)[0]
    m = _STATUS.search(head)
    code = int(m.group(1)) if m else None
    exhausted = bool(_EXHAUSTED.search(text))
    transient = _transient(text)
    if code == 402:
        return {"axis": MONEY, "code": 402,
                "label": "402 payment required (balance or billing)"}
    if code == 403 and _VERIFY.search(text):
        return {"axis": REACH, "code": 403,
                "label": "403 account verification required"}
    if code == 401 and _CREDENTIAL.search(text):
        return {"axis": REACH, "code": 401,
                "label": "401 credential refused (auth, token or key)"}
    if code in (400, 429, None) and exhausted and not transient:
        return {"axis": MONEY, "code": code,
                "label": "%squota or balance exhausted"
                         % ("%d " % code if code else "")}
    return None


def _blocks(record):
    msg = record.get("message")
    return (msg.get("content") or []) if isinstance(msg, dict) else []


def _text(record):
    return "\n".join(str(b.get("text") or "") for b in _blocks(record)
                     if isinstance(b, dict) and b.get("type") == "text")


def _api_error(record):
    """The refusal text of an API-error record, else None."""
    msg = record.get("message") if isinstance(record.get("message"), dict) \
        else {}
    if record.get("isApiErrorMessage") is not True \
            and msg.get("model") != "<synthetic>":
        return None
    text = _text(record).strip()
    return text[len("API Error"):] if text.startswith("API Error") else None


def _worked(record):
    return any(isinstance(b, dict) and (
        b.get("type") == "tool_use"
        or (b.get("type") == "text" and str(b.get("text") or "").strip()))
        for b in _blocks(record))


def last_turn(lines):
    """(WALL|CLEAN|None, typed wall or None, epoch or None) for the newest
    main-chain assistant turn in `lines`. An API error of another kind (a 500,
    a transport failure, an ordinary rate limit) is neither a wall nor a
    success, so the answer is (None, None, at): nothing is said either way."""
    for turn in _turns(lines):
        return turn
    return None, None, None


def _turns(lines):
    """Each main-chain assistant turn in `lines` that says something, newest
    first, as `last_turn` reads it."""
    from . import silent_drop
    for line in reversed(list(lines or ())):
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict) or rec.get("type") != "assistant" \
                or rec.get("isSidechain"):
            continue
        at = silent_drop._epoch(rec.get("timestamp"))
        err = _api_error(rec)
        if err is not None:
            typed = classify(err)
            yield (WALL if typed else None), typed, at
        elif _worked(rec):
            yield CLEAN, None, at


def _streak(lines, axis):
    """How many consecutive newest turns are walls on `axis`, counting each
    distinct instant once (a record written twice is one turn)."""
    seen = set()
    for state, typed, at in _turns(lines):
        if state != WALL or (typed or {}).get("axis") != axis:
            break
        seen.add(at)
    return len(seen)


def seat_reading(seat_name, lines=None, now=None):
    """(reading, why-unread). `reading` is {seat, family, state, at, axis,
    code, label} for a seat whose newest turn is readable, else None and why.
    A WALL older than `max_age_s()` reads as no reading at all."""
    from . import autocompact, seat
    family, err = seat._seat_family(seat_name)
    if err:
        return None, "not a known seat family"
    if lines is None:
        tp = autocompact._newest_transcript(seat._instance_dir(family,
                                                               seat_name))
        if not tp:
            return None, "no transcript to read"
        lines = autocompact._tail_lines(tp)[-RECENT_LINES:]
    state, typed, at = last_turn(lines)
    if state is None:
        return None, "the newest turn is neither a typed wall nor a success"
    now = time.time() if now is None else now
    if state == WALL and (at is None or now - at > max_age_s()):
        return None, "the wall is older than %ds, so it is stale" % max_age_s()
    reading = {"seat": str(seat_name), "family": family, "state": state,
               "at": at}
    reading.update(typed or {})
    if state == WALL:
        reading["turns"] = _streak(lines, typed["axis"])
    return reading, None


def seat_wall(seat_name, now=None):
    """The seat's fresh WALL reading, else None. Never raises: a seat whose
    transcript cannot be read has no wall here."""
    try:
        reading, _why = seat_reading(seat_name, now=now)
    except Exception:                       # noqa: BLE001 — no reading, no wall
        return None
    return reading if reading and reading["state"] == WALL else None


def family_walls(seats=None, now=None, read=None):
    """{family: {axis: wall}} for every family its seats' own last turns
    wall, from FILES ONLY (the burn fold's law). A wall STANDS while NO seat
    of that family has completed a turn since it: a sibling that answered
    after the wall proves the family can still work, whatever that one
    seat's account says. A family is walled on an axis only when its standing
    walls come from `FAMILY_WALLS` DISTINCT SEATS: one spent account refused
    on many turns is one seat, and it never darks its siblings. The earliest
    standing wall on each axis dates it."""
    from . import autocompact
    now = time.time() if now is None else now
    read = read or (lambda s: seat_reading(s, now=now)[0])
    walls, cleans = {}, {}
    for name in (autocompact.proxy_seats() if seats is None else seats):
        try:
            reading = read(name)
        except Exception:                   # noqa: BLE001 — one seat, no fact
            reading = None
        if not reading or reading.get("at") is None:
            continue
        fam = reading["family"]
        if reading["state"] == WALL:
            walls.setdefault(fam, []).append(reading)
        else:
            cleans[fam] = max(cleans.get(fam, 0), reading["at"])
    out = {}
    for fam, rows in walls.items():
        standing = {}
        for w in sorted(rows, key=lambda r: r["at"]):
            if cleans.get(fam, 0) >= w["at"]:
                continue
            standing.setdefault(w["axis"], []).append(w)
        for axis, ws in sorted(standing.items()):
            count = len({str(w.get("seat") or "").casefold() for w in ws})
            if count >= FAMILY_WALLS:
                out.setdefault(fam, {})[axis] = ws[0]
    return out


def text(reading):
    """One sentence for a seat's wall, as the door and the mover print it."""
    from . import pk
    return ("TURN-WALL since %s: its last turn ended on a provider refusal "
            "typed %s (%s), so it takes no turn until the provider answers; "
            "it reads live again on its next successful turn"
            % (pk.epoch_ts(reading["at"]) if reading.get("at") else "?",
               str(reading.get("axis") or "?").upper(),
               reading.get("label") or "?"))
