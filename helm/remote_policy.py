"""What a DRIVEN REMOTE SESSION's read counts as: one table, and the two inputs
the table cannot take on trust.

A remote session (a Claude Code cloud session today) reads a lane in a fresh
context on another account. The store prior
`review-independence-is-model-or-context-scaled-by-reversibility` says such a
reader is independent of the author on the CONTEXT axis even when it runs the
author's model, and scales what that is worth by how reversible the lane is.
The integrator ruled on it, and this module is that ruling:

  1. A read of work another model wrote (Fable, codex, kimi, gemini, DeepSeek,
     any model but the reader's) is a full cross-model review leg.
  2. A read of the reader's OWN model's work is a full review leg on a
     REVERSIBLE lane only. An IRREVERSIBLE lane still owes a different-model
     read, and this read is CONCUR-class input to it.
  3. Two falsifiers revert the same-model arm to CONCUR-class on their own,
     and the revert is latched until someone resets it with a reason.
  4. Sonnet and Haiku never review, in any arm.

WHAT THIS MODULE DOES NOT DO. It does not decide who may APPROVE for a land.
A REVIEW-LEG read is recorded through the door every in-tier reviewer's clean
read uses (a source-clean hold; the land gate mints the approve), and a
CONCUR-class read is recorded as a hold that names the read still owed. Both
are the relay's (helm/remote_relay.py); this module only answers which.

EVERYTHING HERE IS PURE: no file, no clock, no subprocess. The relay folds its
journal and the ledger and hands the facts in, so every cell and every
falsifier can be tested by planting its inputs.
"""
import fnmatch
import os
import re

# ---------------------------------------------------------------------------
# the classes a read can count as
# ---------------------------------------------------------------------------

REVIEW_LEG = "REVIEW-LEG"   # a full review leg: APPROVE-class
CONCUR = "CONCUR"           # input only: another read is still owed
REFUSED = "REFUSED"         # this reader never reviews anything

CROSS_MODEL = "cross-model"
SAME_MODEL = "same-model"

REVERSIBLE = "reversible"
IRREVERSIBLE = "irreversible"

ARM_ON = "on"
ARM_OFF = "off"

OWES_DIFFERENT_MODEL = "a different-model read (Fable or another family)"
OWES_DELTA = ("a different-model read of the delta this read did not settle "
              "(Fable or another family)")

#: THE ONE TABLE. (author relation, lane, same-model arm) -> (class, owed).
#: Every cell is written out, including the ones a rule could derive, so the
#: table a reader audits is the table the code runs. A cross-model read is a
#: full leg in every cell (ruling 1). A same-model read is a full leg only on
#: a reversible lane with the arm standing (ruling 2); every other same-model
#: cell is CONCUR-class and names the read still owed.
TABLE = {
    (CROSS_MODEL, REVERSIBLE, ARM_ON): (REVIEW_LEG, None),
    (CROSS_MODEL, REVERSIBLE, ARM_OFF): (REVIEW_LEG, None),
    (CROSS_MODEL, IRREVERSIBLE, ARM_ON): (REVIEW_LEG, None),
    (CROSS_MODEL, IRREVERSIBLE, ARM_OFF): (REVIEW_LEG, None),
    (SAME_MODEL, REVERSIBLE, ARM_ON): (REVIEW_LEG, None),
    (SAME_MODEL, REVERSIBLE, ARM_OFF): (CONCUR, OWES_DIFFERENT_MODEL),
    (SAME_MODEL, IRREVERSIBLE, ARM_ON): (CONCUR, OWES_DELTA),
    (SAME_MODEL, IRREVERSIBLE, ARM_OFF): (CONCUR, OWES_DIFFERENT_MODEL),
}

#: The switch for ruling 2, read by `arm_switch`. `on` is the integrator's
#: ruling; `off` makes every same-model read CONCUR-class. A tripped falsifier
#: turns the arm off whatever this says.
SWITCH_ENV = "HELM_REMOTE_SAME_MODEL_ARM"

# ---------------------------------------------------------------------------
# models
# ---------------------------------------------------------------------------

#: The Claude model LINES a model id is compared by. Two versions of one line
#: are the same model for this question: the prior is about a shared frame,
#: and a point release of the author's model shares it.
_LINES = ("opus", "fable", "sonnet", "haiku")
#: Ruling 4. Matched as a whole name part, as dispatches._NEVER_REVIEWS does.
NEVER_REVIEWS = ("sonnet", "haiku")


def model_line(model):
    """The comparable name of one model id: its Claude line (`opus`, `fable`,
    `sonnet`, `haiku`) when it names one, else the id itself, case folded and
    without a context-window suffix (`[1m]` picks a window, not a model).
    An empty or non-string id is None."""
    if not isinstance(model, str) or not model.strip():
        return None
    key = re.sub(r"\[[^\]]*\]\Z", "", model.strip()).casefold()
    parts = set(re.split(r"[^a-z0-9]+", key))
    for line in _LINES:
        if line in parts:
            return line
    return key


def reviewer_refusal(model):
    """Why a reader running `model` may not review at all, or None."""
    line = model_line(model)
    if line is None:
        return "the reading model is not recorded, so which model read is unknown"
    if line in NEVER_REVIEWS:
        return ("%s is a %s model, and %s never reviews anything (in any arm)"
                % (model, line.capitalize(), line.capitalize()))
    return None


def relation(reviewer_model, author_model=None, author_family=None):
    """(CROSS_MODEL|SAME_MODEL, why) — how the reader's model stands to the
    author's.

    UNKNOWN IS READ AS SAME-MODEL, because that is the stricter arm: a lane
    whose author's model nobody recorded cannot be shown to have had a
    different model's eyes. A native Claude seat records no model, so this is
    the common case for a Claude author; a seat whose family is not Claude is
    cross-model on the family alone."""
    mine = model_line(reviewer_model)
    theirs = model_line(author_model)
    if theirs:
        if theirs == mine:
            return SAME_MODEL, "the author's model is %s, the reader's" % author_model
        return CROSS_MODEL, "the author's model is %s, the reader's is %s" % (
            author_model, reviewer_model)
    family = str(author_family or "").strip().casefold()
    if family and family != "claude":
        return CROSS_MODEL, "the author's family is %s" % family
    return SAME_MODEL, ("the author's model is not recorded, so the read is "
                        "treated as same-model (the stricter arm)")


def classify(reviewer_model, rel, lane, arm):
    """{class, owed, cell, why} — THE lookup. Every read the relay records
    goes through here and nowhere else."""
    refusal = reviewer_refusal(reviewer_model)
    if refusal:
        return {"class": REFUSED, "owed": "the review leg itself", "cell": None,
                "why": refusal}
    cell = (rel, lane, arm)
    if cell not in TABLE:
        # A key outside the table is a caller's bug. Refusing is the safe
        # direction: REFUSED records nothing that could count.
        return {"class": REFUSED, "owed": "the review leg itself", "cell": None,
                "why": "no policy cell for %r" % (cell,)}
    klass, owed = TABLE[cell]
    return {"class": klass, "owed": owed, "cell": cell,
            "why": "%s read, %s lane, same-model arm %s" % cell}


def arm_switch(environ=None):
    """ARM_ON or ARM_OFF from the switch. Anything but an explicit off word is
    the ruling's default, `on`."""
    raw = str((environ if environ is not None else os.environ)
              .get(SWITCH_ENV) or "").strip().casefold()
    return ARM_OFF if raw in ("off", "0", "no", "false", "concur") else ARM_ON


# ---------------------------------------------------------------------------
# reversibility: which lanes still owe a different-model read (ruling 2 a-d)
# ---------------------------------------------------------------------------

#: The four irreversible classes the ruling names. Each carries the path words
#: and the code patterns that put a lane in it. PATH WORDS are matched against
#: the words of a changed path; CODE PATTERNS against the lines the lane adds.
#: Tests and docs are left out of both: a test that plants `os.kill` changes no
#: live system, and a doc that describes a force push pushes nothing.
HARMS = (
    ("a", "touches prod, a migration, a deletion, money or credentials",
     ("migration", "migrations", "migrate", "schema", "prod", "production",
      "deploy", "deployment", "secret", "secrets", "credential",
      "credentials", "creds", "cred", "password", "passwords", "billing",
      "payment", "payments", "invoice", "wallet", "stripe", "keys"),
     (r"\bdrop\s+(?:table|column|database|index)\b", r"\bdelete\s+from\b",
      r"\btruncate\s+table\b", r"\balter\s+table\b", r"\bshutil\.rmtree\(",
      r"\bos\.(?:remove|unlink|rmdir|removedirs)\(", r"\brm\s+-[a-z]*r[a-z]*f",
      r"\bprod(?:uction)?[_ -](?:write|db|database|data)\b",
      r"\b(?:stripe|payment|invoice|billing|refund|payout)",
      r"\b(?:api|secret)[_-]?key\b", r"\b(?:access|refresh)[_-]?token\b",
      r"\bpassword\b", r"\bcredentials?\b")),
    ("b", "kills processes",
     ("kill", "killer", "reaper", "reap"),
     (r"\bos\.kill(?:pg)?\(", r"\bsignal\.SIG(?:KILL|TERM|INT|HUP|STOP)\b",
      r"\.(?:terminate|kill)\(\)", r"\bpkill\b", r"\bkillall\b",
      r"\bkill\s+-\w+", r"\bsystemctl\s+(?:--user\s+)?(?:stop|kill|restart)\b",
      r"\bdocker\s+(?:kill|rm|stop)\b")),
    ("c", "pushes public or rewrites history",
     ("release", "publish"),
     (r"\bgit\b.*\bpush\b", r"[\"']push[\"']", r"--force\b",
      r"\bforce[- ]push\b", r"\bfilter-(?:repo|branch)\b", r"\bpush\s+--mirror\b",
      r"\bgh\s+(?:release|repo\s+edit|pr\s+merge)\b",
      r"\b(?:npm|twine|cargo)\s+publish\b", r"\bupdate-ref\s+-d\b")),
    ("d", "changes a door that decides safety (land, review, a guard, an "
          "argv-guard, a hook's refusal)",
     ("guard", "guards", "hook", "hooks", "gate", "gates", "land", "landing",
      "review", "reviews", "verdict", "verdicts", "policy", "permission",
      "permissions", "auth", "sandbox", "argv"),
     ()),
)

#: Class (d) also reads a path word that STARTS with one of these (landreq,
#: gateaudits, hookalarm, reviewer_eligibility) or ends in `guard` (stopguard):
#: a door is often named by compounding. The other classes match whole words
#: only, because `author` is not `auth` and `delegate` is not `gate`.
DOOR_PREFIXES = ("land", "gate", "hook", "review", "verdict", "guard",
                 "policy", "permission", "sandbox")

#: The phrases a BRIEF carries when it names an irreversible target. Word
#: tokens, matched as review_door's T0 phrases are, plus the ruling's classes.
BRIEF_PHRASES = (
    "prod write", "production write", "write to prod", "prod data",
    "production data", "prod database", "production database", "prod db",
    "migration", "migrations", "drop table", "drop column", "delete from",
    "hard delete", "deletion", "delete the", "purge", "credentials",
    "credential", "rotate", "secret", "secrets", "money", "payment",
    "billing", "kill", "kills", "killing", "sigkill", "sigterm", "reap",
    "force push", "force-push", "rewrite history", "history rewrite",
    "filter-repo", "push public", "make public", "publish", "release",
    "land gate", "land door", "review door", "guard", "argv guard",
    "argv-guard", "stop hook", "hook refusal", "refusal")

_TEST_WORDS = ("test", "tests", "testing", "fixtures", "spec", "specs")
_DOC_SUFFIXES = (".md", ".rst", ".txt", ".adoc")


def _path_words(path):
    return [w for w in re.split(r"[^a-z0-9]+", str(path).casefold()) if w]


def _is_test_or_doc(path):
    p = str(path).casefold()
    words = _path_words(p)
    base = os.path.basename(p)
    return (any(w in _TEST_WORDS for w in words[:-1])
            or base.startswith("test_") or re.search(r"[._-](test|spec)\.", base)
            or p.endswith(_DOC_SUFFIXES) or p.startswith("docs/")
            or "/docs/" in p)


def _phrase_hits(text, phrases):
    words = re.findall(r"[a-z0-9]+", str(text or "").casefold())
    out = []
    for phrase in phrases:
        seq = re.findall(r"[a-z0-9]+", phrase)
        n = len(seq)
        if n and any(words[i:i + n] == seq for i in range(len(words) - n + 1)):
            out.append(phrase)
    return out


def reversibility(paths, added, brief="", doors=()):
    """(REVERSIBLE|IRREVERSIBLE, hits) for one lane.

    `paths` are the files the lane changes and `added` maps each to the lines
    it adds; both come from the lane's own diff. `doors` are the project's
    declared door globs (the host facts file names them per project, because
    which files are doors is a fact about one project). `brief` is the text
    the author sent with the row.

    FAIL TOWARD OWED. `paths` of None means the diff could not be read, and
    that is IRREVERSIBLE: a lane nobody could inspect cannot be shown to be
    one revert away from undone."""
    if paths is None:
        return IRREVERSIBLE, ["unread: the lane's diff could not be read"]
    hits = []
    for path in paths:
        for glob in doors or ():
            if fnmatch.fnmatch(path, glob):
                hits.append("(d) %s is a declared door (%s)" % (path, glob))
                break
        if _is_test_or_doc(path):
            continue
        words = set(_path_words(path))
        for letter, _what, path_words, patterns in HARMS:
            word = next((w for w in path_words if w in words), None)
            if word is None and letter == "d":
                word = next((w for w in sorted(words)
                             if w.startswith(DOOR_PREFIXES)
                             or w.endswith("guard")), None)
            if word:
                hits.append("(%s) path %s names %r" % (letter, path, word))
            for line in (added or {}).get(path, ()):
                pattern = next((p for p in patterns
                                if re.search(p, line, re.IGNORECASE)), None)
                if pattern:
                    hits.append("(%s) %s adds a line matching %s"
                                % (letter, path, pattern))
                    break
    for phrase in _phrase_hits(brief, BRIEF_PHRASES):
        hits.append("brief names %r" % phrase)
    return (IRREVERSIBLE if hits else REVERSIBLE), hits


# ---------------------------------------------------------------------------
# the falsifiers (ruling 3)
# ---------------------------------------------------------------------------

#: Arm (i): the share of the recorded P1/P2 mechanical findings a blind
#: calibration read found on its own, summed over the main sample. Below this
#: the same-model arm reverts.
CALIBRATION_FLOOR = 0.5
#: ...and only over at least this many reads. One read below half tripped the
#: arm (task/3517): a single small read is an anecdote, not a sample.
CALIBRATION_MIN_READS = 5
MAIN_SAMPLE = "main"
#: Arm (ii): this many same-model APPROVE-class reads contradicted by a
#: cross-family finding on the same tip, within this window, revert it.
CONTRADICTIONS_TRIP = 2
CONTRADICTION_WINDOW_S = 7 * 86400


def calibration_trip(records, sample=MAIN_SAMPLE):
    """(tripped, share, detail) over the calibration records of one sample.

    Each record is {found, of, sample}: of the `of` P1/P2 mechanical findings
    another model recorded on that tip, the remote read found `found` without
    being shown them. The share is summed over the sample, so one small read
    cannot outvote a large one. No record with `of` above zero is no evidence,
    and no evidence never trips; fewer than CALIBRATION_MIN_READS reads are
    reported with their share and never trip either."""
    rows = [r for r in records or ()
            if (r.get("sample") or MAIN_SAMPLE) == sample
            and isinstance(r.get("of"), int) and r["of"] > 0
            and isinstance(r.get("found"), int) and 0 <= r["found"] <= r["of"]]
    if not rows:
        return False, None, "no %s-sample calibration read recorded" % sample
    found = sum(r["found"] for r in rows)
    of = sum(r["of"] for r in rows)
    share = found / float(of)
    detail = ("%s sample: %d of %d recorded findings found independently "
              "over %d read(s) (%.0f%%)" % (sample, found, of, len(rows),
                                            share * 100))
    if len(rows) < CALIBRATION_MIN_READS:
        return False, share, detail + "; under the minimum of %d reads, so " \
            "it cannot trip" % CALIBRATION_MIN_READS
    return share < CALIBRATION_FLOOR, share, detail


def contradictions(approves, fixes):
    """The same-model APPROVE-class reads a cross-family FIX later contradicted.

    `approves` are {tip, ts} (ts in epoch seconds) for each same-model read
    recorded as a full review leg; `fixes` are {tip, ts, family} for each FIX
    another reader recorded. A FIX counts when it is on the SAME tip, LATER
    than the approve, and from a reader whose family is known and is not
    Claude: an unknown family proves nothing about independence, and a falsifier
    must be built from evidence. Returns [{tip, approve_ts, fix_ts}], one per
    contradicted approve, at the earliest contradicting FIX."""
    out = []
    for a in approves or ():
        later = [f["ts"] for f in fixes or ()
                 if f.get("tip") == a.get("tip")
                 and isinstance(f.get("ts"), (int, float))
                 and isinstance(a.get("ts"), (int, float)) and f["ts"] > a["ts"]
                 and str(f.get("family") or "").casefold() not in ("", "claude")]
        if later:
            out.append({"tip": a["tip"], "approve_ts": a["ts"],
                        "fix_ts": min(later)})
    return out


def contradiction_trip(found):
    """(tripped, detail): CONTRADICTIONS_TRIP contradictions whose FIXes fall
    within one CONTRADICTION_WINDOW_S window. Pairs are read off the sorted
    times, so the answer does not depend on when the relay happened to run."""
    times = sorted(c["fix_ts"] for c in found or ())
    need = CONTRADICTIONS_TRIP
    for i in range(len(times) - need + 1):
        if times[i + need - 1] - times[i] <= CONTRADICTION_WINDOW_S:
            return True, ("%d same-model APPROVE-class reads contradicted by a "
                          "cross-family finding within %d days"
                          % (need, CONTRADICTION_WINDOW_S // 86400))
    return False, "%d contradiction(s) on record" % len(times)


def table_lines():
    """The table as printable rows, for `helm remote policy` and the docs."""
    out = []
    for (rel, lane, arm), (klass, owed) in sorted(TABLE.items()):
        out.append("%-11s %-12s arm %-3s -> %-10s %s"
                   % (rel, lane, arm, klass, ("owes " + owed) if owed else ""))
    out.append("sonnet/haiku reader, any cell -> %s (never reviews)" % REFUSED)
    return out
