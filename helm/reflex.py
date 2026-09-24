#!/usr/bin/env python3
"""Reflexes — (signal) -> (steer), delivered by inject-capable harness hooks.

A rule is what you believe; a reflex is what FIRES. Rules decay as context
scrolls; a reflex is re-delivered fresh on its signal, unconditionally — only
the heeding is probabilistic. helm re-inherits the reflex layer harness-broad:
the store lives here, delivery is one `helm inject` call any harness hook can
make (no per-harness lane gating — reflexes fire wherever the operator works).

Signal kinds (cheap, observable — a false-firing reflex is theater):
  every-turn    constant steer, budget-capped (keep the set TINY)
  prompt        regex against the current prompt text
  marker-file   fires while a filesystem path exists (AFK flags, mode markers)
  counter       recorder-fed: a record.py counter crossing a threshold — the
  stalled       named aliases below map to a counter + a field-tested default:
  thrash          stalled=stalled-turns (6)   thrash=loop-streak (3)
  drift           drift=dirty-streak (8)      stuck=stuck-streak (3, escalating)
  stuck         generic `counter` reads any counter via the `counter:` field.
                stalled-turns counts TURNS with no forward op, never calls
                (record.turn_open says which turns count).
  refused       friction-fed: refused=refusal-streak, the most refusals ONE
                guard handed THIS seat in a day (the owner's dial, else
                REFUSAL_STREAK_THRESHOLD).
  nested-spawn  NEVER fires on a turn: the SubagentStart hook (saguide) hands
                it to every build-capable subagent before its first step
                (spawn_steers; a read-only agent_type holds no Agent tool).
  act           NEVER fires on a turn: argv-guard says it at the act itself
                (helm/actsteer.py, chat._STEERS). The entry stays as the
                record of the rule; the act owns the moment (task/2980).

Counter/latch v2 (the record.py counters upgrade steering static->situational):
  * threshold   fire when counters[name] >= threshold (specificity law: a
                counter reflex with no counter or threshold<=0 is skipped, not
                wallpaper).
  * latch       fire ONCE per episode: the latch persists in the SESSION state
                dir and clears when the counter falls back under threshold (the
                inverse event — a forward op, a commit, a recovery). Without
                latch a counter is level-triggered (fires every turn over the
                line) — the seeded pack latches, per the salience law.
  * escalate:N  a latched counter RE-fires as the streak worsens (val crosses
                each further N) — stuck escalates, the rest fire once.
  * pattern     optional extra gate, matched against the PROMPT TEXT only
                (fixed-text law — a counter never reads tool/user output).
  * window:S    optional freshness gate on the counter's own ts (seconds).

Fail-open: no session and no planted counters -> every counter signal is ABSENT
(harnesses that don't thread a session_id degrade to v1 stateless), never a
raise. Latch state is ONE pruned-on-read JSON per session under the recorder's
session dir (reflex-state/<sid>/latch.json) — NOT the ancestor's per-term file
spray; gc TTL-sweeps that dir, so it never ships cross-machine.

Laws: default-silent; fire only on a live signal; steers are terse FACTS, never
commands; fixed text only (no interpolation of tool/user output). A counter
steer may carry two SLOTS, `{count}` and `{subject}`: the counter's own integer
and the token its feeder planted beside it as `<counter>-subject`. Neither is
tool or user output -- one is a number, the other is checked against a
no-whitespace token pattern and replaced by a fixed phrase when it fails.

Store: reflex-<slug>.md in _global/reflexes/ (or a project's premises sibling
dir reflexes/), frontmatter per pk.parse_simple_frontmatter. Every steer,
threshold, and cooldown is an editable, retirable store entry — the authoring
seam is data, not hardcoded Python.
"""
import os
import re

from . import delim, home, pk

_DEFAULTS = {
    "id": "", "steer": "", "signal": "prompt", "pattern": "", "marker": "",
    "counter": "", "threshold": "", "latch": "", "escalate": "", "window": "",
    "status": "live", "stated_ts": "", "notes": "", "source": "",
    # THE PROJECT THIS REFLEX IS ABOUT (task/2435), same field and same values
    # as a store entry's: a registry project name, or `fleet` for steer that
    # applies everywhere. Empty means unrecorded, and unrecorded FAILS TOWARD
    # FLEET — see load_all. A key missing from this parser allowlist is dropped
    # SILENTLY, which is why the field is declared here and not only written.
    "project": "",
    # THE ARRIVALS THIS REFLEX READS (trigger design lane 2): a CSV of
    # moments.ARRIVALS kinds, empty meaning every kind the turn's policy
    # admits. A correction or a piece of owner feedback is something a
    # PERSON types; matched against a peer's wake it is wallpaper (0 of 55
    # correction-language fires were on typed turns, MEASURED designer).
    "arrival": "",
}

EVERY_TURN_BUDGET = 3  # max constant steers per injection — habituation guard

#: THE DEFAULT OF THE OWNER'S DIAL for guard friction. When ONE guard has
#: refused THIS seat this many times inside a day, the seat is told once per
#: episode that it may stop and repair the tool. Lower it and seats repair
#: guards sooner and are interrupted more; raise it and a mistaken guard is
#: suffered longer. The seeded reflex carries no threshold of its own, so this
#: number is the one place the default lives; a `threshold:` written into the
#: store entry overrides it for that home. THE NUMBER THE OWNER SETS
#: (`friction.dial`, turned from his console or `helm friction dial N`)
#: OUTRANKS BOTH: it is the only one of the three that records who chose it
#: and when, and the card he turns it on must describe what seats really meet.
REFUSAL_STREAK_THRESHOLD = 5

# counter-family signal aliases -> (counter name, default threshold, default
# escalate step). `counter` is the generic form: the name comes from the entry's
# `counter:` field. The counters themselves are record.py's session state.
COUNTER_SIGNALS = {
    "counter": (None, 0, 0),
    "stalled": ("stalled-turns", 6, 0),
    "thrash":  ("loop-streak", 3, 0),
    "drift":   ("dirty-streak", 8, 0),
    "stuck":   ("stuck-streak", 3, 3),
    "refused": ("refusal-streak", REFUSAL_STREAK_THRESHOLD, 0),
}

# The shipped starter pack — installed by seed_defaults() at scaffold time so
# the lane is live out of the box. Each lands as a normal store entry marked
# source: helm-default (shipped vs authored stays legible); operators edit or
# retire them like any authored reflex and seeding never touches an existing id
# again. Nothing here fires on a generic turn (specificity law): prompt signals
# demand their phrase, the marker demands the owner's chat post, and the counter
# reflexes demand a record.py streak past its field-tested line. The
# comms-fabric-entangled ancestors (meld-salience, bb-first) are dropped.
# THE CORRECTION IS ADDRESSED TO THE AGENT (task/2978). The first trigger
# matched a bare "i said", and every one of its 13 fires in the 3,054-turn E2
# replay was an agent's own first-person "I said <claim>" while correcting its
# OWN post (0 of 2 relevant in the gold). The owner's corrections say "i said"
# too, but addressed: "like i said", "as i said", "i said use X", "i (thought
# i) said to ...", "i told you". MEASURED over ~3,700 owner-typed prompts in
# the local transcripts: 11 matched the old trigger and all 11 match this
# one; over the replay, 0 of the 13 self-corrections do.
CORRECTION_PATTERN = (r"\b(no,? actually|that'?s (wrong|not what)|i told you"
                      r"|(like|as) i said|i (thought i )?said "
                      r"(to|not to|do|don'?t|go|use|stop|no)|stop doing)\b")
CORRECTION_LEGACY = (r"\b(no,? actually|that'?s (wrong|not what)"
                     r"|i (said|told you)|stop doing)\b")
# A person asking what was already said or decided, or naming cv itself
# (task/2980). The act half of the old trigger, a transcript opened by hand,
# is argv-guard's. MEASURED over the 1,284 E2 replay turns: the old trigger
# fired 24 times (10 task notices, 8 hand-backs, 4 chat wakes, 2 typed); this
# one fires once, on the typed turn that is the reflex's one consensus-gold
# positive ("cv transcript may show"), so that positive is kept.
CV_FIRST_PATTERN = (r"\b(what did we (decide|say|conclude|agree)"
                    r"|where (was|did we) (this|that|it) (discussed|decided|said"
                    r"|discuss|decide|say)|find the discussion"
                    r"|did we (already )?(decide|discuss|agree)|cv)\b")

DEFAULT_PACK = (
    {"id": "correction-language",
     "pattern": CORRECTION_PATTERN, "arrival": "typed",
     "steer": "Correction detected: capture it durably (/premise or helm store add) — "
              "a correction only complied-with is lost by the next compaction."},
    {"id": "punt-tell",
     # punt-SHAPED phrasing only — bare "later"/"todo" ride ordinary narration
     # ("3 days later...", "the todo list") and would fire wallpaper
     "pattern": r"\b(?:(?:wire|do|fix|handle|finish|add|ship|test|clean|deal)\b"
                r"[^.!?\n]{0,24}\blater\b|maybe later\b|for now\b|next session\b|"
                r"punt(?:s|ed|ing)?\b|park (?:it|this|that)\b|"
                r"(?:leave|add) (?:a |the )?todo\b|todo:)",
     "steer": "Deferral language detected: identified fixes land in-pass; anything "
              "truly deferred is loudly flagged to the owner, never silently parked."},
    {"id": "compaction-continuity",
     "pattern": r"\b(summarized|compacted|continu(ed|ing) from)\b",
     "steer": "Compaction marker detected: re-read .remember/now and the live task "
              "state before acting — settled decisions are not re-derived."},
    {"id": "owner-chat-unread",
     # the chat notify loop: the owner's web post drops the marker; any
     # `helm chat read` that consumes past it clears it (chat.consume)
     "signal": "marker-file",
     "steer": "Owner posted in helm chat — read it (`helm chat read`) and reply "
              "(`helm chat post ...`) before continuing."},
    # ── counter/latch pack: field-tested 6/8/3 defaults, latched (edge-fire) ──
    {"id": "stalled-driver",
     "signal": "stalled", "threshold": "6", "latch": "true",
     "steer": "6 turns with no forward op (edit/commit/spawn) — you may be circling; "
              "make a move or name what is blocking."},
    {"id": "loop-thrash",
     "signal": "thrash", "threshold": "3", "latch": "true",
     "steer": "Same command re-run with no change — the loop will not break itself; "
              "read the actual error or change approach, do not re-run."},
    {"id": "uncommitted-drift",
     "signal": "drift", "threshold": "8", "latch": "true",
     "steer": "Many dirtying ops with no commit — checkpoint the green slice; "
              "uncommitted work is work you can lose."},
    {"id": "stuck-commonsense",
     "signal": "stuck", "threshold": "3", "escalate": "3", "latch": "true",
     "steer": "Repeated infra/auth failures — stop retrying the same call; check "
              "creds/network/quota (helm creds) or surface the blocker."},
    # NO `threshold` HERE ON PURPOSE: the alias default is
    # REFUSAL_STREAK_THRESHOLD, so the dial stays one named constant.
    {"id": "guard-friction",
     "signal": "refused", "latch": "true",
     # THE COUNT CANNOT TELL THE TWO CASES APART, SO THE SENTENCE MUST NOT.
     # A refusal streak is equally consistent with a guard that mis-fires and
     # with a seat that keeps doing the thing a correct guard stops. The first
     # wording asserted the former outright ("You are pre-authorized ... fixing
     # the TOOL"), which sends a seat to repair a working guard on its own
     # correct behaviour — measured the day this landed: the stop-guard
     # refused one seat twice in an hour for genuinely held leases, both times
     # right. TWO WORDS FIX IT: the pre-authorization is now conditioned on
     # the guard being wrong instead of presuming it. The owner's clause is
     # otherwise untouched, including "then resume".
     #
     # IT IS TWO WORDS BECAUSE THE BUDGET IS 11 CHARACTERS. This steer rides
     # the reflex lane, whose cap is `len("REFLEX: " + filled) <= 160`
     # (tests/test_friction.py) — NOT the 450 of `PreToolUse/steer`. The
     # original fills to 149 against the longest rung name, so anything
     # longer than this does not ship.
     "steer": "{subject} refused this seat {count}x in 24h. If WRONG, you are "
              "pre-authorized to spend 30 minutes or one delegated agent "
              "fixing the TOOL, then resume."},
)


SPAWN_SIGNAL = "nested-spawn"
# Said by argv-guard at the act (helm/actsteer.py), never on a turn.
ACT_SIGNAL = "act"
# The steer's width at SubagentStart, the reflex lane's own STEER_CAP. The
# text is AUTHORED, so it is cut here and says so (pk.cut_marked); "REFLEX: "
# plus this plus the cut notice stays inside the SubagentStart/nested-spawn
# budget (tests/test_hook_budgets.py).
SPAWN_STEER_KEEP = 280

# RE-KEYED TRIGGERS: an authored reflex whose rule the prompt lane cannot see,
# moved to the signal that can. seed_defaults applies a row only while the
# file still carries the EXACT legacy trigger, so an operator's own edit (a
# new pattern, a new signal) is never touched and a second pass does nothing.
# Only the trigger lines change; the steer, status, scope and any prose stay.
REKEYED = (
    # task/2971: fired on the words constraint, delegated or carries in any
    # prompt — 144 fires in 41h, 88% of the joinable ones on machine-authored
    # prompts, none about a spawn. Its rule binds a subagent, so it is said to
    # each one at its start. A recorded project survives the re-key and
    # spawn_steers honours it.
    {"id": "subagent-no-fanout-must-be-enforced",
     "was": ("prompt", r"\b(constraint|delegated|carries)\b"),
     "now": SPAWN_SIGNAL,
     "notes": "re-keyed by helm (task/2971): said to every subagent at "
              "SubagentStart; its prompt words were unrelated"},
    # task/2978: the shipped correction trigger matched an agent's own
    # "I said" while it corrected itself; the same signal keeps a narrower
    # pattern (CORRECTION_PATTERN), so this row carries a `pattern`.
    {"id": "correction-language",
     "was": ("prompt", CORRECTION_LEGACY),
     "now": "prompt",
     "pattern": CORRECTION_PATTERN,
     "notes": "re-keyed by helm (task/2978): fires on a correction addressed "
              "to the agent, never on an agent correcting its own post"},
    # task/2980 lane 5: two authored reflexes whose moment is an ACT. Their
    # prompt words fired on the topic, not the act: publication-boundary 23.6
    # times a day and never on a typed turn, sweep-before-you-build 16.4 a
    # day (designer). argv-guard now says each at its act — a scrub, untrack
    # or outward push (chat._STEERS scrub-boundary, actsteer public-push), a
    # lane claim or a new module (actsteer sweep-before-you-build).
    {"id": "publication-boundary",
     "was": ("prompt", r"\b(scrub|as-public|leak|redact|untrack|sanitiz"
                       r"|too sensitive|goes public)\b"),
     "now": ACT_SIGNAL,
     "notes": "re-keyed by helm (task/2980): said by argv-guard at a scrub, "
              "untrack or outward push, never on prompt words"},
    {"id": "sweep-before-you-build",
     "was": ("prompt", r"\b(build (a|the|it|this)|let'?s build|i'?ll build"
                       r"|claiming a lane|claim a lane|new (verb|module|command"
                       r"|tool|watcher|surface)|write a new|create a new"
                       r"|net-new|from scratch)\b"),
     "now": ACT_SIGNAL,
     "notes": "re-keyed by helm (task/2980): said by argv-guard at a lane "
              "claim or a new source module, never on prompt words"},
    # cv-first-lookup keeps the prompt lane for the one moment no act
    # signals, a person asking what was decided; its act half (a transcript
    # opened by hand) is actsteer transcript-read. The words look up,
    # recover, transcript and where was fired on machine wakes (42.2 a day).
    {"id": "cv-first-lookup",
     "was": ("prompt", r"\b(look ?up|recover|transcript|where was"
                       r"|what did we decide|find the discussion)\b"),
     "now": "prompt",
     "pattern": CV_FIRST_PATTERN,
     "notes": "re-keyed by helm (task/2980): fires on a question about what "
              "was decided; a hand-read transcript is said at the act"},
)


# ARRIVAL GATES: reflexes that read what a PERSON typed, and so fire on typed
# turns only (trigger design lane 2, task/2954's typed gate). seed_defaults
# writes the `arrival:` line into a live file that has none AND still carries
# one of the triggers listed here, REKEYED's law: an operator's own trigger,
# a file that already names its arrivals, or a retired one is never touched.
# Coinage is the third of the design's three and lives in inject (it has no
# reflex file). The owner-feedback trigger is the authored live pattern.
OWNER_FEEDBACK_PATTERN = (
    r"\b(another thing|one more thing|i recommend you|you should also"
    r"|what about (the|this|that)|don'?t forget|i noticed (that )?(you|your"
    r"|the)|quick (question|one)|while you'?re (in|on|at))\b")
ARRIVAL_GATES = (
    ("correction-language", "typed", (CORRECTION_PATTERN, CORRECTION_LEGACY)),
    ("owner-feedback-is-triage-not-interrupt", "typed",
     (OWNER_FEEDBACK_PATTERN,)),
)


def arrivals(e):
    """The arrival kinds a reflex names (its `arrival` CSV); empty = any."""
    return {a.strip() for a in str(e.get("arrival") or "").split(",")
            if a.strip()}


def _gate_arrival(rid, kinds, patterns):
    """Write `arrival: <kinds>` into the global reflex file `rid` when it has
    no arrival line and its trigger is one of `patterns`; True when it
    changed. The steer, signal and pattern stay byte-identical: only when it
    fires moves."""
    path = reflex_path(rid)
    e = pk.parse_simple_frontmatter(path, _DEFAULTS) \
        if os.path.exists(path) else None
    if not e or e.get("arrival") or e.get("status") != "live" \
            or e.get("signal") != "prompt" or e.get("pattern") not in patterns:
        return False
    with open(path, encoding="utf-8") as fh:
        lines = fh.read().split("\n")
    out, fences, done = [], 0, False
    for ln in lines:
        if ln == "---":
            fences += 1
        if fences == 1 and not done and ln.startswith("  status: "):
            out.append("  arrival: " + kinds)
            done = True
        out.append(ln)
    if not done:
        return False
    pk.atomic_write(path, "\n".join(out))
    return True


def _rekey(row):
    """Apply one REKEYED row to the global reflex file; True when it changed.
    A row with a `pattern` keeps the prompt signal and replaces the pattern
    line; a row without one moves the entry off the prompt lane and drops it."""
    path = reflex_path(row["id"])
    e = pk.parse_simple_frontmatter(path, _DEFAULTS) \
        if os.path.exists(path) else None
    if not e or (e["signal"], e["pattern"]) != row["was"]:
        return False
    with open(path, encoding="utf-8") as fh:
        lines = fh.read().split("\n")
    out, fences, noted = [], 0, bool(e.get("notes"))
    for ln in lines:
        if ln == "---":
            fences += 1
        elif fences == 1 and ln.startswith("  signal: "):
            ln = "  signal: " + row["now"]
        elif fences == 1 and ln.startswith("  pattern: "):
            if not row.get("pattern"):
                continue
            ln = "  pattern: " + row["pattern"]
        elif fences == 1 and ln.startswith("  status: ") and not noted:
            out.append("  notes: " + row["notes"])
            noted = True
        out.append(ln)
    pk.atomic_write(path, "\n".join(out))
    return True


def seed_defaults():
    """Install DEFAULT_PACK into _global/reflexes, marked source: helm-default.
    Idempotent by id: an existing file — operator-edited, re-worded, or retired
    — is never overwritten; a re-seed of a present id is a no-op. Returns the
    paths actually written (empty on re-seed). The chat marker path resolves at
    SEED time (env-respecting — HELM_CHAT_DIR + HELM_CHAT_ROOM, so a homed
    seat's reflex tracks its own team room), never at import time.

    The same pass applies REKEYED, the one way a shipped fix reaches an
    AUTHORED reflex's trigger; a re-keyed file is not returned (nothing was
    seeded) and records why in its own `notes` line."""
    out = []
    for d in DEFAULT_PACK:
        if os.path.exists(reflex_path(d["id"])):
            continue
        e = dict(d, source="helm-default")
        e.setdefault("signal", "prompt")
        if e["signal"] == "marker-file" and not e.get("marker"):
            from . import chat
            e["marker"] = chat.marker_path(home.env("CHAT_ROOM") or "main")
        out.append(write(e))
    for row in REKEYED:
        try:
            _rekey(row)
        except OSError:
            pass                        # an unwritable home keeps its trigger
    for rid, kinds, patterns in ARRIVAL_GATES:
        try:
            _gate_arrival(rid, kinds, patterns)
        except OSError:
            pass
    return out


def spawn_steers(project=None):
    """[(slug, line)] for every live nested-spawn reflex that reaches
    `project`, as the SubagentStart hook (saguide) hands them to a subagent
    before its first step.

    SCOPE IS HONOURED, the same fence as every lane (load_all): a project's
    subagent gets fleet entries plus that project's own. With no project there
    is no lens, so only fleet entries ride; an entry recorded to one project
    is never said outside it."""
    from .store.load import FLEET, entry_project
    return [(pk.slug(e["id"]),
             "REFLEX: " + pk.cut_marked(e["steer"], SPAWN_STEER_KEEP))
            for e in load_all(project)
            if e.get("signal") == SPAWN_SIGNAL
            and (project or entry_project(e) == FLEET)]


def _dirs(project=None):
    out = []
    if project:
        d = os.path.join(home.project_dir(project), "reflexes")
        if os.path.isdir(d):
            out.append(d)
    out.append(os.path.join(home.global_dir(), "reflexes"))
    return out


def reflex_path(rid, project=None):
    base = _dirs(project)[0] if project else os.path.join(home.global_dir(), "reflexes")
    return os.path.join(base, "reflex-" + pk.slug(rid) + ".md")


def load_all(project=None, include_retired=False, seat=False):
    """Every live reflex this project's seats receive — SCOPE-FENCED (task/2435).

    THE GAP THIS CLOSES, measured: the store has an admission door
    (store.load.injects_into, reached by `helm store rescope`) and the reflex
    lane had NONE. _dirs() returns _global/reflexes unconditionally, so every
    reflex written there fired into every project's turn, and no field, verb or
    prefix could hold one to a project. That is the same producer the store's
    fence closed, one lane to the left: a reflex is per-turn steer a seat acts
    on, so a reflex about ONE project's mechanics is exactly the premise the
    acceptance sentence forbids a non-helm seat from receiving.

    ONE PREDICATE, NOT A SECOND LAW. The fence is store.load.injects_into,
    reading the same `project` field with the same values, so the two lanes
    cannot drift into two spellings of scope.

    IT FAILS TOWARD FLEET, deliberately and by construction: a reflex with no
    recorded project derives FLEET (it has no statement line for the helm
    artifact classifier to read), so today's 17 unrecorded global reflexes keep
    reaching every seat and NOTHING changes for them until someone records a
    scope. A steer lost everywhere is the worse error — the same asymmetry the
    store's own law was measured into.

    The project dirs already fence themselves (a project's reflexes/ is only
    read when that project is requested), so this door exists for the case the
    dirs cannot express: a reflex sitting in the GLOBAL dir that is about one
    project.

    `seat=True` is the per-turn door (fire): with no project it admits fleet
    reflexes only, the store seat door's law (task/2978). The inventory
    callers (list, rescope, smoke) keep the global lens.
    """
    from .store.load import injects_into
    out = {}
    for d in reversed(_dirs(project)):  # global first; project shadows by id
        if not os.path.isdir(d):
            continue
        for n in sorted(os.listdir(d)):
            if not (n.startswith("reflex-") and n.endswith(".md")):
                continue
            e = pk.parse_simple_frontmatter(os.path.join(d, n), _DEFAULTS)
            if not (e and e["id"] and e["steer"]):
                continue
            if not include_retired and e["status"] != "live":
                continue
            if not injects_into(e, project, seat=seat):
                continue
            e["path"] = os.path.join(d, n)
            out[pk.slug(e["id"])] = e
    return list(out.values())


def write(e, project=None):
    path = reflex_path(e["id"], project)
    body = [
        "---",
        "name: reflex-" + pk.slug(e["id"]),
        'description: "reflex: '
        + pk.description_line(e["id"] + " - " + e["steer"], 170) + '"',
        "metadata:",
        "  node_type: memory",
        "  type: reflex",
        "  id: " + e["id"],
        "  steer: " + re.sub(r"\s+", " ", e["steer"]),
        "  signal: " + (e.get("signal") or "prompt"),
    ]
    # `project` rides the optional list so it round-trips: written only when
    # set, and absent means unrecorded, which load_all reads as fleet.
    for opt in ("pattern", "marker", "counter", "threshold", "latch",
                "escalate", "window", "notes", "source", "project", "arrival"):
        if e.get(opt):
            body.append("  " + opt + ": " + str(e[opt]))
    body += ["  status: " + (e.get("status") or "live"),
             "  stated_ts: " + str(e.get("stated_ts") or pk.now_ts()),
             "---", "",
             "REFLEX: " + e["steer"], ""]
    pk.atomic_write(path, "\n".join(body))
    return path


# ── counter/latch mechanics ────────────────────────────────────────────────

def _int(v, default=0):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return default


def _truthy(v):
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _owner_dial():
    """The number the owner set for the `refused` signal, None when he has set
    none or it cannot be used: the entry's own threshold and the alias default
    then stand exactly as they did. NEVER RAISES: it runs inside every turn."""
    try:
        from . import friction
        said = friction.dial()
        return said["value"] if said["authored"] else None
    except Exception:
        return None


def counter_spec(e):
    """(counter_name, threshold, escalate) for a counter-family signal, or None
    when the entry is not a counter signal OR is under-specified — no counter
    name, or threshold<=0 (specificity law: an unarmed counter reflex is skipped
    silently, never fires as wallpaper)."""
    sig = e.get("signal") or "prompt"
    if sig not in COUNTER_SIGNALS:
        return None
    cn, dth, desc = COUNTER_SIGNALS[sig]
    if sig == "counter":
        cn = (e.get("counter") or "").strip()
    th = _owner_dial() if sig == "refused" else None
    if th is None:
        th = _int(e.get("threshold"), dth)
    esc = _int(e.get("escalate"), desc)
    if not cn or th <= 0:
        return None
    return cn, th, esc


def _fresh(ts, window_s):
    """The counter ts is within `window_s` seconds. Fail-open: an unparseable or
    absent ts cannot be judged stale, so it reads fresh."""
    if not ts:
        return True
    import calendar
    import time
    try:
        t = calendar.timegm(time.strptime(str(ts), "%Y-%m-%dT%H:%M:%SZ"))
    except (ValueError, TypeError):
        return True
    return (time.time() - t) <= window_s


def _latch_path(session):
    from . import record
    return os.path.join(record.session_dir(session), "latch.json")


def _load_latch(session):
    if not session:
        return {}
    d = pk.read_json(_latch_path(session), {})
    return d if isinstance(d, dict) else {}


def _save_latch(session, d):
    pk.write_json(_latch_path(session), d)


def live_counters(session):
    """The counters a live turn is judged against: the session's record.py
    counters, plus the seat-scoped ones the friction ledger feeds. None when
    the session's counters are not a mapping."""
    from . import friction, record
    c = record.counters(session)
    return (dict(c, **friction.counters(session=session))
            if isinstance(c, dict) else None)


def _read_counters(session, counters):
    """Planted counters win (hermetic tests); else the live counters. Fail-open:
    any trouble -> None (every counter signal absent)."""
    if counters is not None:
        return counters if isinstance(counters, dict) else None
    if not session:
        return None
    try:
        return live_counters(session)
    except Exception:
        return None


_SLOT = re.compile(r"\{(count|subject)\}")
_SUBJECT = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._:-]{0,63}\Z")


def _filled(e, cn, counters):
    """`e` with its steer's slots filled from the counter that fired it. A
    steer with no slot is returned untouched, so every authored entry keeps
    its fixed text."""
    if "{" not in e["steer"]:
        return e
    subject = counters.get(cn + "-subject")
    vals = {"count": str(_int(counters.get(cn), 0)),
            "subject": subject if isinstance(subject, str)
            and _SUBJECT.fullmatch(subject) else "One guard"}
    return dict(e, steer=_SLOT.sub(lambda m: vals[m.group(1)], e["steer"]))


def _counter_fires(e, spec, counters, low, latch, new_latch):
    """Decide one counter reflex against the counters, updating new_latch for
    the pruned-on-read persist. -> True to fire. Under threshold / stale-window
    omits the id from new_latch (the inverse event clears the latch)."""
    cn, th, esc = spec
    pat = e.get("pattern")
    if pat:  # optional prompt-text gate (fixed-text law: prompt only)
        try:
            if not re.search(pat, low, re.IGNORECASE):
                return False
        except re.error:
            return False
    win = _int(e.get("window"), 0)
    if win and not _fresh(counters.get("ts"), win):
        return False  # stale counter: inverse event — drop the latch, silent
    val = _int(counters.get(cn), 0)
    if val < th:
        return False  # under threshold: inverse event — drop the latch, silent
    rid = pk.slug(e["id"])
    if not _truthy(e.get("latch")):
        return True  # level-triggered: fires every turn over the line
    last = _int(latch.get(rid), -1)
    if last < 0 or (esc and val >= last + esc):
        new_latch[rid] = val
        return True
    new_latch[rid] = last  # already latched at this level — stay quiet, stay set
    return False


#: The two classes of signal a turn's arrival policy admits separately: what
#: the prompt SAYS, and the seat's own STATE (a counter, a marker file).
PROMPT_SIGNALS = frozenset(("prompt", "every-turn"))


def _withheld(e, sig, arrival, allow):
    """Is this reflex outside the turn's arrival? `arrival` is the kind
    (moments.ARRIVALS) or None for a caller with no arrival (smoke, direct);
    `allow` is the set of signal classes the kind admits ({"prompt",
    "state"}), or None for all."""
    if arrival is not None and arrivals(e) and arrival not in arrivals(e):
        return True
    if allow is not None:
        cls = "prompt" if sig in PROMPT_SIGNALS else "state"
        return cls not in allow
    return False


def fire(text, project=None, session=None, counters=None, persist=True,
         arrival=None, allow=None):
    """The reflexes whose signal is LIVE for this turn. Salience law: [].

    Counter-family signals read the session's record.py counters (or planted
    `counters` for hermetic tests) and fire on a threshold crossing; `latch`
    entries fire once per episode via the session's latch.json and re-arm when
    the counter clears. Fail-open: no session and no planted counters -> every
    counter signal absent (v1 degrade), never a raise. persist=False reads the
    latch read-only (smoke/explain must never count as a turn).

    `arrival` and `allow` are the turn's arrival gate (inject passes the kind
    and moments.POLICY's classes). A WITHHELD REFLEX KEEPS ITS LATCH: the
    latch is pruned on read, so a counter reflex skipped on a machine wake
    would otherwise read as cleared and re-fire mid-episode on the next
    working turn — and a latch set on a turn that delivered nothing would
    swallow the episode's one line."""
    out = []
    constant = 0
    low = text or ""
    counters = _read_counters(session, counters)
    latch = _load_latch(session) if counters is not None else {}
    new_latch = {}
    for e in load_all(project, seat=True):
        sig = e.get("signal") or "prompt"
        if _withheld(e, sig, arrival, allow):
            rid = pk.slug(e["id"])
            if rid in latch:
                new_latch[rid] = latch[rid]
            continue
        if sig == "every-turn":
            if constant < EVERY_TURN_BUDGET:
                out.append(e)
                constant += 1
        elif sig == "prompt" and e.get("pattern"):
            try:
                if re.search(e["pattern"], low, re.IGNORECASE):
                    out.append(e)
            except re.error:
                continue
        elif sig == "marker-file" and e.get("marker"):
            if os.path.exists(os.path.expanduser(e["marker"])):
                out.append(e)
        elif sig in (SPAWN_SIGNAL, ACT_SIGNAL):
            continue                    # said at the spawn or the act
        elif counters is not None:
            spec = counter_spec(e)
            if spec and _counter_fires(e, spec, counters, low, latch, new_latch):
                out.append(_filled(e, spec[0], counters))
    # pruned-on-read: only ids still latched survive; cleared/retired drop out
    if persist and session and counters is not None and new_latch != latch:
        try:
            _save_latch(session, new_latch)
        except Exception:
            pass
    return out


def _sig_desc(e):
    """The signal column for `reflex list` — kind + its live parameters."""
    sig = e.get("signal") or "prompt"
    if counter_spec(e):
        cn, th, esc = counter_spec(e)
        s = "%s>=%d" % (cn, th)
        if esc:
            s += " esc%d" % esc
        if _truthy(e.get("latch")):
            s += " latch"
        return sig + ":" + s
    tail = (":" + e["pattern"]) if e.get("pattern") else \
           (":" + e["marker"]) if e.get("marker") else ""
    return sig + tail


def cmd_reflex(args):
    """reflex list [--all]|add|retire|smoke — manage the (signal -> steer)
    set (list --all includes retired entries)."""
    if not args or args[0] == "list":
        es = load_all(include_retired="--all" in args)
        if not es:
            print("helm reflex list: none. Add one: helm reflex add <id> | <steer> "
                  "[--signal prompt --pattern <re> | --signal every-turn | "
                  "--signal marker-file --marker <path> | "
                  "--signal stuck --threshold 3 --latch]")
            return 0
        from .store.load import scope_label
        print("helm reflex list (%d):" % len(es))
        for e in es:
            mark = "" if e["status"] == "live" else " [" + e["status"] + "]"
            if e.get("source") == "helm-default":
                mark += " [default]"
            # THE SCOPE RIDES THE ROW. load_all fences by project (the door
            # this listing is the read surface for), but the listing showed no
            # scope at all — so the whole fleet-wide set and a single
            # project's set rendered identically, and the operator asked to
            # record a scope could not see which rows still had none.
            print("  - %s%s (%s) [%s]: %s"
                  % (e["id"], mark, _sig_desc(e), scope_label(e),
                     e["steer"][:90]))
        derived = [e for e in es if not str(e.get("project") or "").strip()]
        if derived:
            print("  %d of %d record no project and therefore reach EVERY "
                  "project — `helm reflex rescope <id> <project|fleet>` "
                  "records one." % (len(derived), len(es)))
        return 0
    if args[0] == "add":
        return _cmd_add(args[1:])
    if args[0] == "retire":
        return _cmd_retire(args[1:])
    if args[0] == "rescope":
        return _cmd_rescope(args[1:])
    if args[0] == "smoke":
        return _cmd_smoke(args[1:])
    import sys
    print("helm reflex: unknown subcommand '%s'" % args[0], file=sys.stderr)
    return 2


_ADD_FLAGS = ("--signal", "--pattern", "--marker", "--counter", "--threshold",
              "--escalate", "--window", "--project")


def _cmd_add(args):
    import sys
    flags = {}
    kept = []
    i = 0
    while i < len(args):
        if args[i] == "--latch":
            flags["latch"] = "true"
            i += 1
            continue
        if args[i] in _ADD_FLAGS and i + 1 < len(args):
            flags[args[i][2:]] = args[i + 1]
            i += 2
            continue
        kept.append(args[i])
        i += 1
    # arity 2 — everything past the steer is a flag, so ANY unescaped pipe in
    # a steer is over-arity and the guard is total for this verb.
    parts, refused = delim.split(" ".join(kept), 2,
                                 "helm reflex add <id> | <steer>")
    if refused:
        print("helm reflex add: " + refused, file=sys.stderr)
        return 2
    if len(parts) < 2 or not parts[0] or not parts[1]:
        print("usage: helm reflex add <id> | <steer> [--signal S] [--pattern RE] "
              "[--marker PATH] [--counter NAME --threshold N [--latch] "
              "[--escalate N] [--window S]] [--project P]", file=sys.stderr)
        return 2
    e = {"id": parts[0], "steer": parts[1], "signal": flags.get("signal", "prompt"),
         "pattern": flags.get("pattern", ""), "marker": flags.get("marker", ""),
         "counter": flags.get("counter", ""), "threshold": flags.get("threshold", ""),
         "escalate": flags.get("escalate", ""), "window": flags.get("window", ""),
         "latch": flags.get("latch", ""), "stated_ts": pk.now_ts()}
    if e["signal"] == "prompt" and not e["pattern"]:
        e["pattern"] = r"\b" + re.escape(parts[0]) + r"\b"
    if e["signal"] in COUNTER_SIGNALS and not counter_spec(e):
        print("helm reflex: '%s' signal needs a counter+threshold (specificity "
              "law) — add --threshold N (and --counter NAME for generic counter)"
              % e["signal"], file=sys.stderr)
        return 2
    path = write(e, flags.get("project"))
    print("helm reflex: LIVE '%s' (%s) -> %s" % (parts[0], _sig_desc(e), path))
    return 0


def _cmd_retire(args):
    import sys
    if not args:
        print("usage: helm reflex retire <id>", file=sys.stderr)
        return 2
    es = {pk.slug(e["id"]): e for e in load_all(include_retired=True)}
    e = es.get(pk.slug(args[0]))
    if not e:
        print("helm reflex: '%s' not found" % args[0], file=sys.stderr)
        return 1
    with open(e["path"]) as fh:
        raw = fh.read()
    pk.atomic_write(e["path"], raw.replace("  status: live", "  status: retired"))
    print("helm reflex: RETIRED '%s' (file kept as the record)" % args[0])
    return 0


def _cmd_rescope(args):
    """rescope <id> <project|fleet|-> — RECORD THE PROJECT A REFLEX IS ABOUT.

    THE OTHER HALF OF THE SCOPE DOOR. load_all fences reflex delivery on the
    `project` field and says in its own docstring that nothing changes for an
    unrecorded reflex "until someone records a scope" — and there was no verb
    that records one. `add --project P` chooses a DIRECTORY for a NEW reflex;
    it cannot answer the case the fence was built for, which is a reflex
    already sitting in the global dir that is about one project. The only
    remaining way to record a scope was to hand-edit a file under ~/.helm,
    which is exactly what the store's own rescope door exists to avoid.

    Measured before this verb: all 17 live reflexes record no project, so the
    fence admits all 17 into every project's turn.

    THE FILE DOES NOT MOVE. A recorded project is a statement about what the
    reflex is ABOUT, not about where it is kept — and the case the fence
    exists for is precisely a GLOBAL-dir reflex held to one project. Moving it
    into the project dir would ALSO fence it, but it would silently change
    which reflex shadows which (project dirs shadow global by id), so this
    records the field and leaves the bytes where they are.

    ONE VALIDATION DOOR, NOT A SECOND SPELLING OF IT: the operand passes the
    same two contracts `helm store rescope` applies — the registry's name
    contract, and representability as one flat-frontmatter field value —
    because a name recorded here is compared EXACTLY against a name recorded
    there by the one shared predicate (store.load.injects_into)."""
    import sys
    from .store.load import FLEET
    if len(args) < 2:
        print("usage: helm reflex rescope <id> <project|fleet|->",
              file=sys.stderr)
        return 2
    rid, want = args[0], str(args[1]).strip()
    if want == "-":
        want = ""
    if want and want != FLEET:
        from . import registry
        if registry.name_is_malformed(want):
            print("helm reflex rescope: a project is a registry name or '%s', "
                  "never '%s'" % (FLEET, args[1]), file=sys.stderr)
            return 2
    if want and not pk.field_value_is_representable(want):
        print("helm reflex rescope: a recorded project is one field value on "
              "one line of flat frontmatter, and %r does not survive that "
              "round trip. Nothing was recorded." % want, file=sys.stderr)
        return 2
    es = {pk.slug(e["id"]): e for e in load_all(include_retired=True)}
    e = es.get(pk.slug(rid))
    if not e:
        print("helm reflex rescope: '%s' not found" % rid, file=sys.stderr)
        return 1
    before = str(e.get("project") or "").strip()
    if before == want:
        print("helm reflex: '%s' already records %s"
              % (e["id"], want or "no project (it reaches every project)"))
        return 0
    with open(e["path"]) as fh:
        raw = fh.read()
    ok, out = _sub_project_line(raw, want)
    if not ok:
        print("helm reflex rescope: %s (%s) — nothing was written"
              % (out, e["path"]), file=sys.stderr)
        return 1
    pk.atomic_write(e["path"], out)
    if want:
        print("helm reflex: '%s' is about %s" % (e["id"], want))
    else:
        print("helm reflex: '%s' records no project — it reaches EVERY project"
              % e["id"])
    return 0


def _sub_project_line(raw, want):
    """(ok, text-or-reason) — set/clear the `project` metadata line in place.

    SURGICAL, NOT RE-SERIALIZED. Rebuilding the file through `write` would
    discard anything the frontmatter parser's allowlist does not carry and any
    hand-written body prose beneath the steer, on a file an operator is
    explicitly allowed to edit. This rewrites ONE line.

    It refuses rather than guesses when the metadata block it must edit is not
    there: a file whose shape it cannot recognise is a file whose meaning it
    cannot preserve, and a silent no-op here would report a scope that was
    never recorded — the exact failure the store-listing defect taught."""
    lines = raw.split("\n")
    meta = status = project = None
    for i, ln in enumerate(lines):
        if ln.startswith("metadata:"):
            meta = i
        elif meta is not None and ln.startswith("  project:"):
            project = i
        elif meta is not None and status is None and ln.startswith("  status:"):
            status = i
    if meta is None or status is None:
        return False, "no metadata block with a status line to edit"
    if project is not None:
        if want:
            lines[project] = "  project: " + want
        else:
            del lines[project]
    elif want:
        lines.insert(status, "  project: " + want)   # write()'s own ordering
    return True, "\n".join(lines)


def _cmd_smoke(args):
    """smoke [--session S] [--project P] — LIVE read-only counter check: the
    real record.py counters, which counter reflexes fire against them, and each
    one's armed/latched state. Mutates nothing (persist=False)."""
    from . import friction, record
    project = args[args.index("--project") + 1] if "--project" in args \
        and args.index("--project") + 1 < len(args) else None
    session = args[args.index("--session") + 1] if "--session" in args \
        and args.index("--session") + 1 < len(args) else None
    reflexes = [e for e in load_all(project) if counter_spec(e)]
    if not reflexes:
        print("helm reflex smoke: no counter reflexes live — `helm reflex list`")
        return 0
    sessions = [(session, record.counters(session))] if session else \
        [(k, pk.read_json(p, {}) or {}) for k, p, _ in record._sessions()[:5]]
    if not sessions:
        print("helm reflex smoke: no recorded sessions yet (`helm record status`)")
        return 0
    # the friction counters are the CALLING seat's, not a session's: they ride
    # on every row shown, exactly as they ride on every live turn of this seat
    seat_counters = friction.counters()
    for sid, counters in sessions:
        counters = dict(counters if isinstance(counters, dict) else {},
                        **seat_counters)
        fired = {pk.slug(e["id"]) for e in fire("", project=project, session=sid,
                                                counters=counters, persist=False)}
        print("session %s:" % (sid or "(none)"))
        for e in reflexes:
            cn, th, esc = counter_spec(e)
            val = _int(counters.get(cn), 0)
            state = "FIRE" if pk.slug(e["id"]) in fired else \
                    "armed" if val >= th else "idle"
            print("  %-5s %-20s %s=%d/%d : %s" % (
                state, e["id"], cn, val, th, e["steer"][:60]))
    return 0
