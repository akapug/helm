#!/usr/bin/env python3
"""helm.planprompt — the plan-prompt ACTUATOR: the rung that ADVANCES a seat
frozen at an approval prompt, instead of one more guard that correctly declines
to hurt it.

WHY THIS EXISTS, and it is a composition failure rather than a missing check.
Three rungs already meet a seat sitting at a plan-execution dialog, and every
one of them is right:

  * `seat.seat_liveness` READS the pane tail and mints BLOCKED_ON_HUMAN with
    the plan path attached — detection, complete.
  * `proxywatch.findings` refuses to prescribe resume/reseed on that state,
    because resuming DISCARDS the pending plan (fleet #124, owner ruling:
    complying destroys work).
  * `autocompact._fire` refuses /compact on that state, for the same reason.

Detect yes, refuse-to-harm yes, ADVANCE nobody. Three correct guards with clean
consciences jointly guarantee the seat sleeps forever, and the seat sleeps
holding its lease, its claim and every dispatch row it owes. `grep
BLOCKED_ON_HUMAN helm/*.py | grep -iE 'send|inject|answer|resolve|actuat'`
returned ZERO the day this module was written: nothing in helm ANSWERED. The
integrator answered — by hand, pane by pane, all week (store premise
`seats-freeze-on-plan-mode-prompts`). Bug class
`watchdogs-correct-composition-holed`.

`seat.PLAN_ENTRY_TOOL` denies EnterPlanMode on every seat config dir, which
stops the class at the SOURCE for seats born after it. It does not reach a seat
already running or a session resumed from before it — and a suppression cannot
un-freeze a pane that is already frozen. That is the gap this rung closes, and
the owner named the level: "the fix is just injecting into the pane". A tool
permission dialog is not this actuator's: THE PROMPT-STALL WATCH below answers
the routine ones under the owner's standing authorization and routes the rest.

=== THE FOUR RUNGS, AND WHAT EACH ONE REFUSES ===

R1 STATE — consumed, never re-derived. `seat.seat_liveness` is THE pane-tail
   reader; this rung never opens a pane to classify one (proxywatch's
   standing rule). The keystroke and the state therefore describe the same
   sample of the same screen.

R2 AUTHORITY — NEVER answer an owner-driven session. #124 is canon: complying
   destroys the human's work, and plan mode exists FOR the human (owner ruling
   2026-08-03, seat.py). Two existing authorities, no new one:
     (a) the NAME. `seats.owner_names()` is the set helm already recognises as
         the owner across the posting seam and the delivery seam. A pane
         wearing one of those names is his.
     (b) the REGISTER, and this one is structural rather than a policy. A
         BLOCKED_ON_HUMAN verdict is reachable ONLY through seat_liveness's
         registered-pane arm: no spawn record, a headless record, or a stale
         handle all exit as UNKNOWN before any tail is classified. So a session
         helm did not launch cannot produce the state this rung acts on, and
         the owner's own `(default-claude)` session — the one config dir
         `_seed_seat_settings` deliberately does NOT touch — is outside the
         actuator by construction, not by remembering to check.

R3 THE PROMPT + PLAN — the liveness row must type the SAME pane snapshot as
   Claude Code's built-in plan-execution dialog. BLOCKED_ON_HUMAN is broader:
   it also includes tool-permission questions, which are the prompt-stall
   watch's to classify, never this rung's. A `plans/` path is supporting
   evidence, never that type authority — a permission question can quote one. Anything not explicitly typed
   `plan-execution` surfaces and receives no keystroke.

   The answer then comes from READING the plan. A rung that approves whatever
   is on screen is worse than one that waits, so the plan file NAMED IN THE
   PROMPT must exist and read. When it does not, the outcome is a chat row
   addressed to the owner CARRYING THE PLAN TEXT — never a guess. The path must
   carry a `plans/` segment; otherwise a transcript mentioning a README could
   hand this rung an arbitrary file to read aloud into chat.

R4 THE CHOICE — read off the dialog (`seat.affirmative_choice`), never a fixed
   digit, and then PROVED. The send returning success proves the RPC, not the
   TUI: the effect is the pane LEAVING BLOCKED_ON_HUMAN, re-measured through
   the same classifier. A send whose effect never appears is reported
   `unproven` and surfaced — it is never rounded up to `answered`.

GATED PLANS STAY THE OWNER'S. A plan that lands, pushes, deploys, destroys
working state or touches credentials is not this rung's to approve at 3am;
those are exactly the acts helm gates elsewhere (landgate, the WORLD-audience
bar). The table below is deliberately BROAD, because its two failure modes are
not symmetric: a false hit costs one surfaced row that a human reads (which is
strictly better than today, where nothing surfaces at all), and a false miss
auto-approves an irreversible act.

COMPOSED, not net-new: seat.seat_liveness (state + plan path + choices),
seat._resolve_registered_pane(for_send=True) + seat._seat_lifecycle_lock (the
identity authority every pane actuator already shares), autocompact's
proxy-seat enumeration, and silent_drop's fcntl state latch and chat-alert
idiom.
"""
import fcntl
import json
import os
import re
import stat
import sys
import time

from . import home

BLOCKED = "BLOCKED_ON_HUMAN"
# The states that PROVE the pane took the keystroke: it is working again, or it
# already finished. UNKNOWN is not proof of anything and never closes the loop.
ADVANCED = ("RUNNING", "IDLE")
VERIFY_TRIES = 6
VERIFY_WAIT_S = 2.0
PLAN_READ_BYTES = 16000        # bounded read; a plan is prose, not a corpus
PLAN_EXCERPT = 1500            # what rides the owner-bound chat row
LATCH_TTL_S = 30 * 60          # one surfaced row per seat per episode
_STATE = "planprompt.json"
ROOM = "helm"                  # fleet ops, never #main (silent_drop's ruling)
WHO = "plan-prompt"

# ACTS THAT ARE NOT THIS RUNG'S TO APPROVE. Data, not control flow — adding one
# is a row. Each traces to a gate helm already enforces somewhere else.
GATED_OPS = (
    (re.compile(r"\bgit\s+push\b|\bforce[-\s]push\b|\bpush\s+to\s+(origin|main|"
                r"the\s+remote)\b", re.I), "pushes to a remote"),
    (re.compile(r"\bgit\s+commit\b|\bcreate\s+(?:a\s+)?(?:local\s+)?commit\b|"
                r"\bcommit\s+(?:the|these|this)\s+changes\b", re.I),
     "creates a local delivery commit"),
    (re.compile(r"\bhelm\s+land\b|\bfold\s+(it\s+)?to\s+main\b|\bmerge\s+"
                r"(it\s+)?(in)?to\s+main\b", re.I), "lands to trunk"),
    (re.compile(r"\brm\s+-rf\b|\bgit\s+reset\s+--hard\b|\bworktree\s+remove\b|"
                r"\bbranch\s+-D\b", re.I), "destroys working state"),
    (re.compile(r"\bdeploy\b|\bproduction\b|\bwrangler\s+(deploy|publish)\b",
                re.I), "deploys"),
    (re.compile(r"\bANTHROPIC_API_KEY\b|\bcredential(s)?\b|\bsecret(s)?\b|"
                r"\bapi[-_\s]?key\b", re.I), "touches credentials"),
)

_USAGE = """usage: helm seat unblock [--seat S] [--dry-run] [--quiet] [--json]
  Answer only Claude Code's plan-execution prompt on a frozen AGENT seat, by
  reading the plan it names — or surface it when the dialog is a human
  permission, the plan cannot be read, or the plan is owner-gated. The owner's
  own session is never answered.
  Also runs the prompt-stall watch over every Claude seat (native ones too)
  parked at a Claude Code prompt for 3 minutes or more, read from its own
  presence record: it ANSWERS a memory write or a routine tool permission
  (a Bash command only when every program it runs is readable on screen or
  in a tracked script) itself and proves the pane moved, pages the owner only
  for a credential, money, an outward or destructive act, or a seat keys
  cannot move (a freeze candidate), and addresses the integrator for a prompt
  it cannot classify or a command it cannot read.
  HELM_PROMPT_ANSWER=0 disarms the answering (in the watch's environment for
  the fleet, in a seat's own environment for that seat).
  --dry-run assesses and writes nothing (no send, no post, no latch) and
  prints what the watch would do from each seat's transcript; --quiet skips
  the chat rows; --json prints the rows.
"""


# ---------------------------------------------------------------------------
# R2 authority
# ---------------------------------------------------------------------------

def owner_driven(seat_name):
    """(True, why) when this name is the OWNER's, not an agent seat's.

    `seats.owner_names()` is the EXISTING recognition set — the same one the
    chat delivery lane's owner rule and the web/TUI posting default resolve
    through — so this rung and the rest of helm can never disagree about who
    the human is.
    """
    from . import seats
    name = str(seat_name or "").strip().lower()
    try:
        names = seats.owner_names()
    except Exception as e:               # noqa: BLE001 — unreadable => his
        return True, ("cannot resolve the owner name set (%s); refusing to "
                      "answer a prompt that may be the owner's" % e)
    if name in names:
        return True, ("%r is an OWNER name — plan mode exists for the human "
                      "and answering it would destroy his pending work "
                      "(fleet #124)" % seat_name)
    return False, None


# ---------------------------------------------------------------------------
# R3 the plan
# ---------------------------------------------------------------------------

def plan_of(blocked_on):
    """(path, err) — the plan file the prompt NAMED, or why there is none.

    `blocked_on` is either a plan path (seat._blocked_detail extracts it) or
    the matched prompt text (its honest fallback when the pane printed no
    path). Only a `plans/` path is accepted; see R3 in the module docstring.
    """
    raw = str(blocked_on or "").strip()
    if not raw or not raw.endswith(".md"):
        return None, ("the prompt named no plan file (blocked_on=%r) — a "
                      "permission dialog with nothing to read is the owner's"
                      % (raw[:80] or None))
    if "plans" not in raw.split("/"):
        return None, ("%r is not a plan path (no plans/ segment) — refusing "
                      "to read and quote an arbitrary file" % raw[:120])
    path = os.path.expanduser(raw)
    if not os.path.isfile(path):
        return None, "the named plan %r does not exist on this host" % raw[:120]
    return path, None


def read_plan(path):
    """(text, err). A plan that will not read is a plan we did not read."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read(PLAN_READ_BYTES)
    except OSError as e:
        return None, "the plan %s could not be read (%s)" % (path, e)
    if not text.strip():
        return None, "the plan %s is empty" % path
    return text, None


def plan_verdict(text):
    """(ok, why) — is this plan the actuator's to approve?"""
    for pat, why in GATED_OPS:
        m = pat.search(text or "")
        if m:
            return False, ("the plan %s (%r) — that is the owner's call, not "
                           "a watchdog's" % (why, m.group(0).strip()[:40]))
    return True, "the plan names no owner-gated act"


# ---------------------------------------------------------------------------
# R1 + assessment
# ---------------------------------------------------------------------------

def _liveness(name):
    from . import seat
    return seat.seat_liveness(name)


def assess(name, liveness=None):
    """One seat -> the row this rung would act on. READ-ONLY: it consumes the
    liveness state and reads a plan file, and never touches a pane."""
    from . import seat
    live = liveness or _liveness
    row = {"seat": name, "state": None, "outcome": None, "detail": None,
           "prompt_type": None, "plan": None, "choice": None,
           "choice_label": None, "plan_text": None}
    try:
        lv = live(name) or {}
    except Exception as e:               # noqa: BLE001 — one blind seat must
        # not blind the sweep. A rescue that dies on seat A never reaches seat
        # B, which is the same shape as a watchdog going blind at exactly its
        # target state — the class this rung lives in.
        row["state"], row["outcome"] = "UNKNOWN", "blind"
        row["detail"] = "the liveness probe crashed (%s)" % e
        return row
    row["state"] = lv.get("state")
    if lv.get("state") != BLOCKED:
        row["outcome"] = "not-blocked"
        row["detail"] = "state is %s" % (lv.get("state") or "UNKNOWN")
        return row
    gated, why = owner_driven(name)
    if gated:
        # The state STAYS BLOCKED_ON_HUMAN and stays visible. Refusing to
        # answer is not the same as clearing the flag.
        row["outcome"] = "refused-owner"
        row["detail"] = why
        return row
    row["prompt_type"] = lv.get("prompt_type")
    if row["prompt_type"] != seat.PLAN_EXECUTION_PROMPT:
        row["outcome"] = "surface"
        row["detail"] = (
            "the current dialog is %s, not Claude Code's plan-execution "
            "prompt — a plans/ path and affirmative options do not make it "
            "one, and the prompt-stall watch answers or routes a permission "
            "prompt"
            % ("a human permission prompt" if row["prompt_type"] ==
               seat.PERMISSION_PROMPT else "not typed by the pane classifier"))
        return row
    path, err = plan_of(lv.get("blocked_on"))
    if err:
        row["outcome"] = "surface"
        row["detail"] = err
        return row
    row["plan"] = path
    text, err = read_plan(path)
    if err:
        row["outcome"] = "surface"
        row["detail"] = err
        return row
    row["plan_text"] = text
    ok, why = plan_verdict(text)
    if not ok:
        row["outcome"] = "surface"
        row["detail"] = why
        return row
    choice, label = seat.affirmative_choice(lv.get("options"))
    if not choice:
        row["outcome"] = "surface"
        row["detail"] = ("the dialog offered no readable `Yes` choice "
                         "(options=%r) — helm does not type a digit it did "
                         "not read" % (lv.get("options") or []))
        return row
    row["choice"], row["choice_label"] = choice, label
    row["outcome"] = "answer"
    row["detail"] = "%s; choice %s (%s)" % (why, choice, label)
    return row


# ---------------------------------------------------------------------------
# R4 actuation + the proof
# ---------------------------------------------------------------------------

def _send_choice(name, choice, plan=None, plan_text=None, choice_label=None,
                 adapter=None):
    """(ok, detail) — re-prove the assessed prompt and write its choice into
    the ONE pane the spawn register authorizes.

    The lifecycle lock protects both identities that can race: WHICH pane the
    register names and WHAT prompt is currently accepting a keystroke. A send
    that merely re-resolves the handle can still type an old answer into the
    next screen after another actor or the TUI advances the dialog.
    """
    from . import harness, seat
    family, err = seat._seat_family(name)
    if err:
        return False, err
    d = seat._instance_dir(family, name)
    with seat._seat_lifecycle_lock(d):
        ad, handle, detail = seat._resolve_registered_pane(
            name, d=d, adapter=adapter, locked=True, for_send=True)
        if ad is None or handle is None:
            return False, detail
        try:
            tail = ad.read(handle)
        except Exception as e:               # noqa: BLE001 — unreadable => no send
            return False, "pane %s could not be re-read: %s" % (handle, e)
        state, blocked_on = seat._classify_pane_tail(tail)
        if state != BLOCKED:
            return False, ("pane %s changed before send: state is %s, not %s"
                           % (handle, state or "UNKNOWN", BLOCKED))
        if seat._prompt_type(tail) != seat.PLAN_EXECUTION_PROMPT:
            return False, ("pane %s changed before send: the current dialog is "
                           "not Claude Code's plan-execution prompt" % handle)
        current_plan, err = plan_of(blocked_on)
        if err or os.path.abspath(current_plan or "") != os.path.abspath(plan or ""):
            return False, ("pane %s changed before send: plan %r no longer "
                           "matches assessed plan %r%s"
                           % (handle, current_plan, plan,
                              " (%s)" % err if err else ""))
        current_choice, current_label = seat.affirmative_choice(
            seat._prompt_options(tail))
        if (current_choice, current_label) != (str(choice), choice_label):
            return False, ("pane %s changed before send: affirmative choice %r "
                           "no longer matches assessed choice %r"
                           % (handle, (current_choice, current_label),
                              (str(choice), choice_label)))
        current_text, err = read_plan(current_plan)
        if err or current_text != plan_text:
            return False, ("pane %s changed before send: the assessed plan "
                           "contents no longer match%s"
                           % (handle, " (%s)" % err if err else ""))
        ok, why = plan_verdict(current_text)
        if not ok:
            return False, "pane %s changed before send: %s" % (handle, why)
        try:
            ad.send(handle, str(choice), enter=True)
        except harness.HarnessError as e:
            return False, "pane %s rejected the choice: %s" % (handle, e)
    return True, "choice %s sent to pane %s (%s)" % (choice, handle, detail)


def verify_advanced(name, liveness=None, sleep=time.sleep, tries=VERIFY_TRIES):
    """(ok, detail) — did the PANE move? The send's own success proves the RPC
    reached the harness and nothing about the TUI, so the proof is the state
    the same classifier reports afterwards. UNKNOWN keeps polling and never
    counts: a pane we cannot read is not a pane that advanced."""
    live = liveness or _liveness
    last = None
    for _ in range(tries):
        sleep(VERIFY_WAIT_S)
        last = (live(name) or {}).get("state")
        if last in ADVANCED:
            return True, "pane left %s and is now %s" % (BLOCKED, last)
    return False, ("pane did not leave %s after the choice (last state %s "
                   "over %.0fs) — the send is not the effect"
                   % (BLOCKED, last or "UNKNOWN", tries * VERIFY_WAIT_S))


def act(row, liveness=None, adapter=None, sleep=time.sleep):
    """Run R4 on an `answer` row -> the row, outcome resolved to
    `answered` | `unproven`."""
    ok, detail = _send_choice(
        row["seat"], row["choice"], plan=row.get("plan"),
        plan_text=row.get("plan_text"), choice_label=row.get("choice_label"),
        adapter=adapter)
    if not ok:
        row["outcome"] = "surface"
        row["detail"] = "the choice could not be delivered: %s" % detail
        return row
    proven, proof = verify_advanced(row["seat"], liveness=liveness, sleep=sleep)
    row["outcome"] = "answered" if proven else "unproven"
    row["detail"] = "%s; %s" % (detail, proof)
    return row


# ---------------------------------------------------------------------------
# the owner surface
# ---------------------------------------------------------------------------

def _state_path():
    return os.path.join(home.helm_home(), home.GLOBAL, ".state", _STATE)


def surface_text(row):
    """The owner-bound row: what is stuck, why helm would not answer it, and
    THE PLAN ITSELF — the thing that otherwise costs him a pane visit."""
    from . import seats
    plan = ("plan %s:\n%s" % (row["plan"], (row["plan_text"] or "")[:PLAN_EXCERPT])
            if row.get("plan_text") else "no plan text was readable")
    return ("@%s seat %s is FROZEN at a human prompt and helm did not "
            "answer it: %s. The seat stays blocked until you answer its pane. "
            "%s [plan-prompt actuator]"
            % (seats.owner_name(), row["seat"], row["detail"], plan))


def answered_text(row):
    """The trail. An actuator that types into the owner's fleet without leaving
    a legible record of WHAT it approved is an unaudited hand on the keyboard."""
    from . import seats
    return ("@%s seat %s was frozen at a plan-execution prompt; helm read %s, "
            "answered choice %s and PROVED the pane advanced. %s "
            "[plan-prompt actuator]"
            % (seats.owner_name(), row["seat"], row["plan"], row["choice"],
               row["detail"]))


def _post(text):
    from . import chat
    try:
        chat.post(text, who=WHO, room=ROOM)
        return True
    except Exception as e:               # noqa: BLE001 — a down room never
        print("helm seat unblock: chat post failed: %s" % e, file=sys.stderr)
        return False                     # blocks the actuation


# ---------------------------------------------------------------------------
# one pass
# ---------------------------------------------------------------------------

def seats_to_scan():
    from . import autocompact
    return autocompact.proxy_seats()


def check(seats=None, post=True, quiet=False, dry=False, liveness=None,
          adapter=None, sleep=time.sleep):
    """One pass: assess every seat, answer what is answerable, surface the
    rest. Returns {"rows": [...], "answered": [...], "surfaced": [...]}.

    `dry` is genuinely read-only and wins over `post`: nothing is sent, nothing
    is posted, and the surface latch is NOT stamped — a simulation must never
    spend the alert budget and suppress the next real one (silent_drop's law,
    learned the hard way there).
    """
    from . import pk
    names = seats_to_scan() if seats is None else list(seats)
    rows = [assess(n, liveness=liveness) for n in names]
    actionable = [r for r in rows if r["outcome"] in ("answer", "surface")]
    if dry:
        return {"rows": rows, "answered": [], "surfaced": [],
                "would": [r["seat"] for r in actionable]}

    answered, surfaced = [], []
    for row in rows:
        if row["outcome"] == "answer":
            act(row, liveness=liveness, adapter=adapter, sleep=sleep)
        if row["outcome"] == "answered":
            answered.append(row)
        elif row["outcome"] in ("surface", "unproven"):
            surfaced.append(row)

    p = _state_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p + ".lock", "a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        st = pk.read_json(p, {}) or {}
        now = time.time()
        fresh = []
        for row in surfaced:
            e = st.get(row["seat"]) or {}
            # A NEW plan re-surfaces immediately: the latch exists to stop a
            # frozen seat re-crying the SAME block, not to hide a new one.
            if e.get("plan") == row.get("plan") and \
                    now - (e.get("at") or 0) < LATCH_TTL_S:
                row["latched"] = True
                continue
            row["latched"] = False
            st[row["seat"]] = {"at": now, "plan": row.get("plan")}
            fresh.append(row)
        pk.write_json(p, st)

    if post and not quiet:
        for row in answered:
            _post(answered_text(row))
        for row in fresh:
            _post(surface_text(row))
    return {"rows": rows, "answered": answered, "surfaced": surfaced}


# ---------------------------------------------------------------------------
# THE PROMPT-STALL WATCH — every Claude seat, not only the proxy seats
# ---------------------------------------------------------------------------
#
# THE MISS THIS CLOSES (measured). A native Claude seat parked on a
# permission prompt for a memory write stayed there for over an hour and
# nothing surfaced it. `check` above could have: it is the rung that posts a
# frozen seat to the owner. But it scans `seats_to_scan()`, which is the
# PROXY seats only, and no timer runs it. The one watcher that did see the
# seat, the beacons pass, saw only the consequence: it posted "DEAF SEAT — it
# must re-arm its beacon", a remedy a frozen seat cannot perform, and then
# retried a pane nudge every five minutes that never typed.
#
# THE SIGNAL IS THE VENDOR'S OWN, AND IT IS CHEAP. Claude Code rewrites its
# presence record, <config>/sessions/<pid>.json, whenever the session changes
# state: `status` is busy|idle|shell|waiting, and `waiting` carries
# `waitingFor` ("permission prompt", "input needed", "dialog open", ...) with
# `statusUpdatedAt` stamped only on a change (read in the installed program,
# 2.1.280). So how long a seat has been parked at a prompt is one bounded file
# read per session: no pane RPC (measured 4-6s per adopted seat), no
# transcript parse to FIND a stall. `session.read_session_presence` is the
# bracketed reader.
#
# A STUCK PROMPT IS A KEYPRESS, NOT A FREEZE, AND THE WATCH ANSWERS IT (the
# owner's ruling, store premise `a-stuck-prompt-is-a-keypress-not-a-freeze`).
# Every act helm builds is pre-authorized except a few named exceptions, and
# the humans are AFK unless they are typing at that moment. A seat parked at a
# memory-write prompt for over half an hour while the owner's phone was paged
# is the failure mode: one keypress clears it. So the watch READS the prompt,
# answers what the standing authorization covers, PROVES the pane moved, and
# pages the owner only for his named exceptions or when keys do not move it:
#
#   what the prompt asks                     what the watch does
#   an auto-memory write into the seat's     presses the session-wide Yes (else
#     own memory dir                         Yes), proves the move, posts one
#                                            quiet line plus the relaunch hint
#   a routine tool permission (a Bash        presses Yes, proves it, one quiet line
#     command only when LEGIBLE: every
#     program it runs is on screen)
#   a credential, real money, an outward     types NOTHING and pages the owner,
#     or a destructive act                   naming exactly what it asks
#   a shape the watch cannot classify, or    types nothing and addresses the
#     a Bash command that is not legible     INTEGRATOR with the pane excerpt
#   keys sent and the pane did not move      integrator; a second failure is a
#                                            FREEZE candidate: the owner, with
#                                            the relaunch line
#   the pane printed within HUMAN_QUIET_S    types nothing and waits: someone
#                                            may be at the keyboard
#
# TWO WITNESSES, ONE TYPIST. The pane is the only place the dialog's options
# are, so it alone decides WHICH key means what. The transcript's pending tool
# call is the only place the full path or command is, because a long dialog
# scrolls its title off a ~35-line viewport. The keystroke goes through
# `resumeturn.deliver` with an `operation` — the transaction every helm
# keystroke shares (the spawn register under the lifecycle lock, or
# `orcaadopt.authorized_handle` on a stamped pid) — exactly as the vendor
# escape does. It is not recorded as composer text: a dialog digit is not a
# message, and a recovery that later pressed Enter on it would be wrong.
#
# THE KEY IS THE OPTION'S DIGIT. Claude Code's select takes a digit with no
# Enter (installed 2.1.282: `enterConfirms:!1` beside `isValidDigit`), which
# is the arrow-then-Enter the owner described, minus the hazard: a digit that
# lands after the dialog closed is a stray character in a composer, where an
# Enter would submit a human's draft. A pane that reads BLANK (orca reports
# many orphaned panes that way) gets one bare Enter instead, which takes the
# dialog's default, its first option (Yes), and only when every pending call
# the transcript names is itself answerable.
PROMPT_STALL_S = 180           # parked this long at a prompt is a stall
STALL_REPEAT_S = LATCH_TTL_S   # one attempt, and one post, per latch window
STALL_WHO = "prompt-stall"
_STALL_KEY = "stall:"
#: The presence states the installed program writes (busy, idle, shell,
#: waiting). Any other value, or none, is a record this watch cannot read.
PRESENCE_STATES = ("busy", "idle", "shell", "waiting")
BLIND_NAME_CAP = 5
#: THE KILL SWITCH, one name in two places. `HELM_PROMPT_ANSWER=0` (or off/no)
#: in the watch's own environment disarms the answering fleet-wide; in a
#: seat's own process environment it disarms that seat, for an owner who sits
#: at its keyboard. Either way the stall is paged to the owner as before.
ANSWER_ENV = "PROMPT_ANSWER"
#: Keys that did not move the pane this many times in one episode make the
#: seat a FREEZE candidate, and the watch stops typing into it.
ANSWER_TRIES = 2
#: A pane that printed this recently, while its prompt has waited longer than
#: this, has someone at it (a human keystroke echoes): the watch waits.
HUMAN_QUIET_S = 120
MOVE_TRIES = 6
MOVE_WAIT_S = 2.0
PENDING_TAIL_BYTES = 256 * 1024
EXCERPT_LINES = 12
EXCERPT_CHARS = 900
#: A live dialog sits at the BOTTOM of the viewport: below its last choice
#: only a hint, the status bar and orca's draft row. A run with more than this
#: under it is scrollback, and its digits belong to a screen that is gone.
DIALOG_FOOT_LINES = 5

MEMORY, ROUTINE, OWNER, UNKNOWN = "memory", "routine", "owner", "unknown"
PERMISSION = "permission prompt"
FILE_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")
READ_TOOLS = ("Read", "Glob", "Grep", "LS")
WEB_TOOLS = ("WebFetch", "WebSearch")
#: A pending subagent call is never the subject of a permission dialog: the
#: dialog is about a call INSIDE it, which lives in the subagent's transcript.
SPAWN_TOOLS = ("Agent", "Task")

# THE OWNER'S NAMED EXCEPTIONS, as data — adding one is a row. Each row names
# the ask fields it reads, because a file's CONTENT must never be scanned: a
# memory note that mentions a credential is still a memory write. `_CMDQ` is
# a command, the dialog's own question, a tool name or an MCP tool's input;
# `_ANY` adds the path and the URL. A credential HOME is not itself gated: a
# homed seat's memory dir lives under it (<home>/projects/...), and gating the
# tree would page the owner for the exact prompt this watch exists to answer.
# The credential FILES in it are.
_CMDQ = ("command", "question", "tool", "input")
_ANY = _CMDQ + ("path", "url")
OWNER_GATES = (
    (_ANY, re.compile(
        r"(?:^|[\s/'\"=:~])\.(?:ssh|aws|gnupg|netrc)(?:/|\b)|"
        r"\.config/(?:simbi|gh|gcloud|op)\b|\.credentials\.json\b|"
        r"\boauth[\w.-]*\.json\b|\bid_(?:rsa|dsa|ecdsa|ed\d+)\b|"
        r"\.(?:pem|p12|pfx|keychain)\b|(?:^|/)\.env(?:\.[\w-]+)?(?:$|[\s'\"])",
        re.I), "credential", "a credential store"),
    (_CMDQ, re.compile(r"\b[A-Z][A-Z0-9]*_(?:API_KEY|AUTH_TOKEN|ACCESS_TOKEN|"
                       r"SECRET(?:_KEY)?|PASSWORD)\b"),
     "credential", "a secret"),
    (_CMDQ, re.compile(r"\b(?:gh|wrangler|npm|docker|gcloud|aws|claude|op|"
                       r"vercel|flyctl|heroku)\s+(?:auth\s+)?(?:login|logout|"
                       r"setup-token)\b|\bssh-(?:keygen|add|copy-id)\b|"
                       r"\bpasswd\b|\bsudo\b", re.I),
     "credential", "a login or a password"),
    (("question",), re.compile(r"\bapi key\b|\bpassword\b|\bpassphrase\b|"
                               r"\btoken\b|\bcredential|\blog ?in\b|"
                               r"\bsign ?in\b|\bauthenticat", re.I),
     "credential", "a credential typed at the prompt"),
    (_CMDQ, re.compile(r"\bbuy\b|\bpurchase\b|\bpayment\b|usage credits|"
                       r"\bsubscribe\b", re.I),
     "money", "spending real money"),
    (_CMDQ, re.compile(r"\bgit\s+push\b|\bforce[-\s]push\b", re.I),
     "outward", "a push to a remote"),
    (_CMDQ, re.compile(r"\bgh\s+(?:pr|issue|release|gist|repo)\s+(?:create|"
                       r"comment|merge|edit|close|delete|review|reopen)\b|"
                       r"\bgh\s+api\b.*\s-X\s*(?:POST|PATCH|PUT|DELETE)\b",
                       re.I),
     "outward", "a post on GitHub"),
    (_CMDQ, re.compile(r"\b(?:npm|pnpm|yarn|cargo|gem)\s+publish\b|"
                       r"\btwine\s+upload\b|\bdocker\s+push\b", re.I),
     "outward", "a public release"),
    (_CMDQ, re.compile(r"\bwrangler\s+(?:deploy|publish)\b|"
                       r"\b(?:vercel|netlify|flyctl)\s+deploy\b|"
                       r"\bkubectl\s+(?:apply|delete)\b|"
                       r"\bterraform\s+(?:apply|destroy)\b", re.I),
     "outward", "a deploy"),
    (_CMDQ, re.compile(r"send_?message|send_?email|post_?message|"
                       r"create_?draft|\btweet\b|"
                       # helm's own outward verbs: a chat post or dm reaches
                       # the fleet, a dispatch or store write is a ledger act
                       # (kimi's read of this lane: a plain `helm chat post`
                       # prompt answered itself)
                       r"\bhelm\s+chat\s+(?:post|dm)\b|\bhelm\s+dispatch\s+"
                       r"(?:send|add|verdict|hold|release|cancel|rebind|"
                       r"retip|retract)\b|\bhelm\s+store\s+(?:confirm|"
                       r"supersede|revise|retire|reject)\b", re.I),
     "outward", "a message sent to people"),
    (_CMDQ, re.compile(r"\bcurl\b[^|\n]*\s(?:-d\b|-X\s*(?:POST|PATCH|PUT|"
                       r"DELETE)\b|--data\b|--upload-file\b)|"
                       r"\bwget\b[^|\n]*\s--post-", re.I),
     "outward", "an HTTP write"),
    (_CMDQ, re.compile(r"\brm\s+-[a-z]*(?:rf|fr)|"
                       # the same act with its flags split or spelled long
                       # (`rm -r -f`, `rm --recursive --force`)
                       r"\brm\b(?=[^\n|;&]*\s(?:-[a-z]*r|--recursive\b))"
                       r"(?=[^\n|;&]*\s(?:-[a-z]*f|--force\b))[^\n|;&]*|"
                       r"\bgit\s+reset\s+--hard\b|"
                       r"\bgit\s+clean\s+-[a-z]*f|\bbranch\s+-D\b|"
                       r"\bworktree\s+remove\b|\bdrop\s+(?:table|database)\b|"
                       # find's delete is rm by another spelling (kimi's read)
                       r"\bfind\b[^\n|;]*\s-delete\b|"
                       r"\bmkfs\b|\bdd\s+if=", re.I),
     "destructive", "destroying work"),
)

_BOX = re.compile(r"^[\s│┃|]+|[\s│┃|]+$")
_QUESTION = re.compile(r"^Do you want to .+\?$", re.I)
_FILE_Q = re.compile(r"^Do you want to (create|overwrite|write to|make this edit "
                     r"to|insert this cell into|delete this cell from) (.+?)\?$",
                     re.I)
_FILE_TITLES = {"Create file": "create", "Overwrite file": "overwrite",
                "Write file": "write to", "Edit file": "edit",
                "Read file": "read"}
_BASH_TITLE = re.compile(r"^Bash command\b")
_FETCH_Q = re.compile(r"\bfetch this content\b", re.I)
_OPTION_1 = re.compile(r"^\s*(?:❯\s*)?1\.\s+(\S.*?)\s*$")
_WIDEN = re.compile(r"\bfor this session\b|\bduring this session\b|"
                    r"\bdon'?t ask again\b", re.I)
_PLAIN_YES = re.compile(r"^yes[.!]?$", re.I)
_YES = re.compile(r"^yes\b", re.I)


def _clean(text, limit=40):
    """A vendor string bound for a chat row: printable, bounded."""
    t = "".join(c for c in str(text or "") if c.isprintable())
    return t[:limit]


def _scrub(text, limit):
    """Pane or transcript text bound for a chat row: printable (newlines kept),
    bounded, and with every address and key-shaped token taken out by the
    tree's one redactor. A prompt can quote a command that carries a secret."""
    from . import accounts
    t = "".join(c for c in str(text or "") if c == "\n" or c.isprintable())
    return accounts.redact_identities(t[:limit]) or ""


def _off(value):
    return str(value).strip().lower() in ("0", "off", "no", "false")


def answer_off(row):
    """Why the watch must not type into this seat, or None.

    TWO SWITCHES, THE SAME ONE THE REST OF HELM HONOURS. The keystroke rides
    resumeturn's transaction, so a fleet that disarmed THAT (HELM_RESUME_TURN)
    has disarmed every helm keystroke into a pane — the vendor escape's rule."""
    from . import resumeturn
    if _off(home.env(ANSWER_ENV, "1")):
        return "HELM_%s is off in the watch's environment" % ANSWER_ENV
    if _off(row.get("answer_env") or "1"):
        return "HELM_%s is off in seat %s's own environment" % (ANSWER_ENV,
                                                               row["seat"])
    if not resumeturn.enabled():
        return ("HELM_RESUME_TURN is off, which disarms every helm keystroke "
                "into a pane")
    return None


def _presence_blind(rec, why):
    """Why this presence record cannot answer "is it parked at a prompt?",
    or None when it can. UNREADABLE IS NOT EMPTY: a missing, stale, corrupt,
    unsafe or schema-changed record, a status this watch does not know, and a
    waiting record with no readable stamp are each UNKNOWN, never not-stalled
    and never a stall."""
    if not isinstance(rec, dict):
        return why or "no presence record"
    status = rec.get("status")
    if status not in PRESENCE_STATES:
        return ("record carries no status" if status is None else
                "record carries an unknown status %r" % _clean(status))
    since = rec.get("statusUpdatedAt")
    if status == "waiting" and (isinstance(since, bool)
                                or not isinstance(since, (int, float))):
        return "record is waiting with no readable statusUpdatedAt"
    return None


def prompt_stalls(census=None, now=None, read=None, sids=None, owners=None):
    """-> {read, why, sessions, stalls, unnamed, blind}: every live Claude
    seat that has been parked at a prompt for PROMPT_STALL_S or longer, and
    every session whose presence record could not say (`blind`, each row
    carrying the reason; `sessions` counts only the records that could).

    A seat is named by its declared HELM_CHAT_NAME, else by the roster seat
    whose CURRENT session is the record's (orcaadopt.roster_current_sids, the
    addressing half — a history sid is never an address); a session neither
    names is counted in `unnamed`, never guessed. Either name is laundered
    through `_seat_label` before it can reach a chat row. The owner's own
    sessions are not surfaced: he is the one they wait for. Every input is
    injectable so an arm owns it.

    A stall row carries what the answerer needs and nothing else from the
    process environment: the birth stamp that authorizes a keystroke, the
    pane key that routes it, and the seat's own HELM_PROMPT_ANSWER."""
    from . import seats, seats_common, session
    now = time.time() if now is None else now
    out = {"read": True, "why": None, "sessions": 0, "stalls": [],
           "unnamed": 0, "blind": []}
    if read is None:
        uid = os.geteuid()
        read = lambda root, pid, start: session.read_session_presence(  # noqa: E731
            root, pid, uid, start)
    if census is None:
        try:
            census = session._proc_claude_census()
        except Exception as e:               # noqa: BLE001 — a watch never raises
            out.update(read=False, why="the seat census raised %s: %s"
                       % (e.__class__.__name__, e))
            return out
    if census.get("listing_failed"):
        out.update(read=False, why="the /proc enumeration failed, so no "
                   "session's prompt state could be read")
        return out
    if sids is None:
        from . import orcaadopt
        sids, _failed = orcaadopt.roster_current_sids()
    by_sid = {sid: name for name, sid in (sids or {}).items() if sid}
    if owners is None:
        try:
            owners = seats.owner_names()
        except Exception:                    # noqa: BLE001 — an unreadable
            owners = set()                   # owner set surfaces, never hides
    for row in census.get("rows") or ():
        if row.get("child") or not row.get("root") or not row.get("start"):
            continue
        rec, why = read(row["root"], row["pid"], row["start"])
        env = row.get("environ") or {}
        raw = env.get("HELM_CHAT_NAME") or \
            by_sid.get(rec.get("sessionId") if isinstance(rec, dict)
                       else row.get("session"))
        name = seats_common._seat_label(raw) if raw else None
        blind = _presence_blind(rec, why)
        if blind:
            out["blind"].append({"seat": name, "pid": row["pid"],
                                 "why": blind})
            continue
        out["sessions"] += 1
        since = rec.get("statusUpdatedAt")
        if rec["status"] != "waiting":
            continue
        waited = now - since / 1000.0
        if waited < PROMPT_STALL_S:
            continue
        if not name:
            out["unnamed"] += 1
            continue
        if str(name).lower() in owners:
            continue                         # he is the one it waits for
        out["stalls"].append({
            "seat": name, "pid": row["pid"], "root": row["root"],
            "start": row["start"], "session": rec.get("sessionId"),
            "waiting_for": _clean(rec.get("waitingFor")) or "prompt",
            "since": since / 1000.0, "stamp": since, "waited_s": int(waited),
            "pane_key": env.get("ORCA_PANE_KEY"),
            "answer_env": env.get("HELM_" + ANSWER_ENV)})
    out["stalls"].sort(key=lambda r: (-r["waited_s"], r["seat"]))
    return out


# ---------------------------------------------------------------------------
# the two witnesses: the transcript's pending call, and the pane's dialog
# ---------------------------------------------------------------------------

def pending_calls(session, root, tail_bytes=PENDING_TAIL_BYTES):
    """([{"name", "input", "cwd"}], why) — the tool calls in the session's
    LAST assistant message that have no result yet, subagent spawns left out.
    `cwd` is the directory the transcript recorded on the record that asked
    (Claude Code writes the shell's current directory into every record), so
    it is where a pending Bash command runs; None when the record carries none.

    THE LAST MESSAGE, NOT EVERY UNANSWERED CALL IN THE WINDOW. Parallel calls
    share one message id and are asked about one at a time, so an answered
    sibling leaves the rest pending; an older message's call with no result is
    history (an interrupt), never the dialog on screen. None means the
    transcript could not say, which is never the same as no pending call."""
    from . import turnresponse
    path, err = turnresponse.transcript_path(session, root=root)
    if err or not path:
        return None, err or "no transcript carries session %s" % str(session)[:8]
    recs, _cut, _partial, _bad, err = turnresponse._tail_records(path, tail_bytes)
    if err:
        return None, "its transcript could not be read (%s)" % err
    last, uses, done = object(), [], set()
    for rec in recs:
        if not isinstance(rec, dict) or rec.get("isSidechain"):
            continue
        msg = rec.get("message") if isinstance(rec.get("message"), dict) else {}
        content = msg.get("content") if isinstance(msg.get("content"), list) else []
        blocks = [b for b in content if isinstance(b, dict)]
        if rec.get("type") == "assistant":
            if msg.get("id") != last:
                last, uses = msg.get("id"), []
            cwd = rec.get("cwd") if isinstance(rec.get("cwd"), str) else None
            uses.extend((b, cwd) for b in blocks if b.get("type") == "tool_use")
        done.update(b.get("tool_use_id") for b in blocks
                    if b.get("type") == "tool_result")
    return [{"name": str(b.get("name") or ""),
             "input": b["input"] if isinstance(b.get("input"), dict) else {},
             "cwd": cwd}
            for b, cwd in uses if b.get("id") not in done
            and b.get("name") not in SPAWN_TOOLS], None


def call_ask(call):
    """One pending call -> the ask the gates and the kinds read. Only the
    fields that NAME the act: never a file's content."""
    name, inp = call["name"], call["input"]
    text = lambda k: inp.get(k) if isinstance(inp.get(k), str) else None  # noqa: E731
    ask = {"tool": name}
    if name in FILE_TOOLS + READ_TOOLS:
        ask["path"] = text("file_path") or text("notebook_path") or text("path")
    if name == "Bash":
        ask["command"] = text("command")
        # WHERE it runs, which no gate reads: the legibility reader resolves a
        # script path against it (see LEGIBILITY)
        ask["cwd"] = call.get("cwd") if isinstance(call.get("cwd"), str) \
            else None
    if name in WEB_TOOLS:
        ask["url"] = text("url") or text("query")
    if name.startswith("mcp__"):
        ask["input"] = json.dumps(inp, sort_keys=True, default=str)[:2000]
    return ask


def _flat(tail):
    """The tail's lines with ANSI and box borders stripped: what the dialog
    SAYS, in the coordinate space `seat._prompt_options` reads."""
    from . import seat
    return [_BOX.sub("", line)
            for line in seat._PANE_ANSI.sub("", tail or "").splitlines()]


def pane_ask(tail):
    """ONE pane read -> {lines, options, question, signature, tool, verb,
    name, path, command, excerpt}. `options` is the newest numbered run, read
    by the fleet's one option parser; `question` counts only when it sits
    directly above that run, so an older dialog in the scrollback cannot lend
    this one its words."""
    from . import seat
    lines = _flat(tail)
    kept = [x.strip() for x in lines if x.strip()]
    got = {"lines": kept, "options": [], "question": None, "signature": None,
           "tool": None, "verb": None, "name": None, "path": None,
           "command": None,
           "excerpt": _scrub("\n".join(kept[-EXCERPT_LINES:]), EXCERPT_CHARS)}
    opts = seat._prompt_options("\n".join(lines)) if kept else []
    got["options"] = opts
    starts = [i for i, x in enumerate(lines)
              if opts and (_OPTION_1.match(x) or [None, ""])[1] == opts[0][1]]
    if not starts:
        return got
    last = opts[-1][1]
    ends = [i for i, x in enumerate(lines)
            if i > starts[-1] and x.rstrip().endswith(last)]
    foot = sum(1 for x in lines[(ends or starts)[-1] + 1:] if x.strip())
    if foot > DIALOG_FOOT_LINES:
        return got
    q = next((i for i in range(starts[-1] - 1, -1, -1) if lines[i].strip()),
             None)
    if q is None or not _QUESTION.match(lines[q].strip()):
        return got
    got["question"] = lines[q].strip()
    got["signature"] = (got["question"],
                        tuple((str(n), str(label)) for n, label in opts))
    title = next(((i, lines[i].strip()) for i in range(q - 1, max(-1, q - 80), -1)
                  if lines[i].strip() in _FILE_TITLES
                  or _BASH_TITLE.match(lines[i].strip())), (None, None))
    fq = _FILE_Q.match(got["question"])
    if fq or title[1] in _FILE_TITLES:
        got["tool"] = "file"
        got["verb"] = fq.group(1).lower() if fq else _FILE_TITLES[title[1]]
        got["name"] = fq.group(2).strip() if fq else None
        sub = None if title[0] is None else next(
            (x.strip() for x in lines[title[0] + 1:q] if x.strip()), None)
        if sub and "/" in sub and (not got["name"] or os.path.basename(sub)
                                   == os.path.basename(got["name"])):
            got["path"] = sub
    elif title[1] and _BASH_TITLE.match(title[1]):
        got["tool"] = "Bash"
        got["command"] = "\n".join(x.strip() for x in lines[title[0] + 1:q]
                                   if x.strip()) or None
    elif _FETCH_Q.search(got["question"]):
        got["tool"] = "WebFetch"
    return got


def _bind(pane, calls):
    """The ONE pending call this dialog is about, or None. A file dialog names
    its file; a Bash dialog shows its command. Anything less is no binding."""
    squash = lambda s: " ".join(str(s or "").split())  # noqa: E731
    screen = squash(" ".join(pane["lines"]))
    hits = []
    for c in calls or ():
        a = call_ask(c)
        if pane["tool"] == "file" and c["name"] in FILE_TOOLS + READ_TOOLS:
            if a.get("path") and pane["name"] and os.path.basename(a["path"]) \
                    == os.path.basename(pane["name"]):
                hits.append(a)
        elif pane["tool"] in ("Bash", None) and c["name"] == "Bash":
            if a.get("command") and squash(a["command"])[:40] in screen:
                hits.append(a)
        elif pane["tool"] == "WebFetch" and c["name"] == "WebFetch":
            hits.append(a)
    return hits[0] if len(hits) == 1 else None


# ---------------------------------------------------------------------------
# the classifier
# ---------------------------------------------------------------------------

def _own_memory(path, root):
    """Is `path` an auto-memory note in THIS seat's home: <root>/projects/
    <slug>/memory/<note>.md, compared on real paths, so the linked form the
    session writes through and the resolved form the dialog may print are one
    directory."""
    if not path or not root:
        return False
    p = os.path.expanduser(str(path))
    if not os.path.isabs(p) or not p.endswith(".md"):
        return False
    d = os.path.realpath(os.path.dirname(p))
    return (os.path.basename(d) == "memory" and os.path.dirname(os.path.dirname(d))
            == os.path.realpath(os.path.join(root, "projects")))


def plain(ask):
    """An ask in the words a person reads on a phone."""
    tool = ask.get("tool")
    if ask.get("command"):
        return "run `%s`" % _scrub(ask["command"].splitlines()[0], 160)
    if ask.get("path") or ask.get("name"):
        verb = ask.get("verb") or {"Edit": "edit", "MultiEdit": "edit",
                                   "NotebookEdit": "edit", "Write": "write",
                                   "Read": "read"}.get(tool, "write to")
        return "%s %s" % (verb, _scrub(ask.get("path") or ask["name"], 200))
    if ask.get("url"):
        return "fetch %s" % _scrub(ask["url"], 160)
    if tool and tool not in ("file",):
        return "use the %s tool" % _scrub(tool, 80)
    return _scrub(ask.get("question") or "answer its prompt", 160)


def _gate(ask):
    """(category, words, matched) of the first OWNER_GATES row `ask` hits,
    reading only the fields each row names, or None."""
    for fields, pat, category, words in OWNER_GATES:
        text = "\n".join(str(ask[f]) for f in fields if ask.get(f))
        m = pat.search(text)
        if m:
            return category, words, _clean(m.group(0).strip(), 40)
    return None


# ---------------------------------------------------------------------------
# LEGIBILITY: the routine Yes needs every program a command runs on screen
# ---------------------------------------------------------------------------
#
# THE GATES ARE A DENYLIST, AND A DENYLIST READS ONLY WHAT IS ON SCREEN (the
# integrator's ruling on kimi's read of this lane). `C=push; git $C origin
# main` passed every OWNER_GATES row, and so did `bash scripts/ship.sh` whose
# script pushes: both got the routine Yes. So a Bash prompt is ROUTINE only
# when its command is LEGIBLE, meaning every program it runs can be read from
# its own text. An OPAQUE command is UNKNOWN: nothing is typed and the
# integrator reads the excerpt (never the owner: it is not one of his named
# exceptions, only a shape the watch cannot read). The reason names the part.
#
#   OPAQUE                                             because
#   a program word spelled as an expansion ($C, $(..))  its value is not shown
#   an expansion among the words of a program an        it can hide the very
#     OWNER gate reads by name (`git $C`, `rm $X`)       verb the gate refuses
#   a re-executor: eval, source, ., trap, alias, xargs, it runs text or input
#     find -exec, a shell's -c or stdin, python -c /     as a command that no
#     stdin, node/ruby/perl, PATH=/BASH_ENV= and kin     reader here reads
#   a script run by path or through an interpreter,     its text is not on
#     unless it is a regular tracked file of the seat's  screen; a script that
#     repo, at most 64 KiB, passes the gates, and is     runs another script is
#     itself legible (shell: this reader, one level;    opaque (ONE level)
#     python: the inert-import allowlist)
#   `python3 -m` any module but unittest and pytest,    as a script: resolved
#     unless it resolves to such a file                  in the repo, or opaque
#   a runner of repo-defined scripts: make, just, task, the recipe is not on
#     npx, npm/pnpm/yarn/bun (all but a read verb),      screen
#     cargo run, go run, uv/poetry/pipenv run
#
# A script's text that hits a gate is OWNER, exactly as if it were on screen.
#
# ONE PARSER. The shell is read by chat.py's reader (`_ShellReader`,
# `_simple_commands`, `_program`), the one the Actions and owner-posture rungs
# stand on; this module never splits a command itself. chat is imported
# lazily, as `_post` already does in the same pass, so the watch pays nothing
# new (measured: ~40 ms cold, once per process).
#
# THE SEAT'S REPO is the repo holding the pending call's own `cwd` (see
# `pending_calls`). A pane-only witness has no cwd, and then a script is
# simply opaque: the watch never guesses which directory a path is in. A
# command that changes directory makes every relative script in it opaque,
# for the same reason.
SCRIPT_READ_BYTES = 64 * 1024
#: shells: `-c` and a shell reading stdin are re-executors
_SHELLS = frozenset(("bash", "sh", "zsh", "dash", "ksh", "mksh", "ash", "fish"))
#: the shells whose scripts this reader can read (fish's grammar is not bash's)
_READ_SHELLS = _SHELLS - {"fish"}
#: interpreters with no reader here: their eval flag is a re-executor and
#: their script is opaque; only a version or help read is legible
_OTHER_INTERPRETERS = {"node": ("-e", "--eval", "-p", "--print"),
                       "nodejs": ("-e", "--eval", "-p", "--print"),
                       "ruby": ("-e",), "perl": ("-e", "-E")}
_VERSION_READS = frozenset(("--version", "-v", "-V", "--help", "-h"))
_TEST_MODULES = frozenset(("unittest", "pytest"))
#: programs that run a command they build from their input or argv
_ARGV_RUNNERS = frozenset(("xargs", "parallel", "watch"))
_FIND_EXEC = frozenset(("-exec", "-execdir", "-ok", "-okdir"))
#: rebinding what a later name runs, in the current shell
_REBINDERS = frozenset(("alias", "enable", "hash"))
_DIR_CHANGES = frozenset(("cd", "pushd", "popd"))
#: runners of repo-defined or downloaded code, whatever their arguments
_RUNNERS = frozenset(("make", "gmake", "just", "task", "rake", "npx", "pnpx",
                      "bunx"))
#: package managers run a repo script by a bare name or a lifecycle script on
#: install, so only a read verb is legible
_PACKAGE_MANAGERS = frozenset(("npm", "pnpm", "yarn", "bun"))
_PACKAGE_READS = frozenset(("ls", "list", "view", "info", "why", "outdated",
                            "help")) | _VERSION_READS
#: runners of repo code by one verb
_VERB_RUNNERS = {"cargo": ("run",), "go": ("run", "generate"), "uv": ("run",),
                 "poetry": ("run",), "pipenv": ("run",), "bundle": ("exec",)}
#: THE PROGRAMS AN OWNER GATE READS BY NAME. The gates match their spelled
#: words, so a word of theirs spelled as an expansion can hide exactly the verb
#: or option a gate refuses. A gate row that names a new program owes a row
#: here (tests/test_planprompt.py counts them).
_GATE_PROGRAMS = frozenset((
    "git", "gh", "helm", "curl", "wget", "rm", "find", "dd", "mkfs", "sudo",
    "passwd", "npm", "pnpm", "yarn", "cargo", "gem", "twine", "docker",
    "wrangler", "vercel", "netlify", "flyctl", "heroku", "gcloud", "aws",
    "claude", "op", "kubectl", "terraform", "ssh-keygen", "ssh-add",
    "ssh-copy-id"))


def _opaque(why):
    return UNKNOWN, why, None


def _unshown(word):
    """What an unsettled word is, in the reason's words. A word is a
    command's text and may carry a secret, so it rides the redactor."""
    raw = _scrub(word.raw, 60)
    if "$" in word.raw or "`" in word.raw:
        return "`%s`, an expansion whose value is not on screen" % raw
    return "`%s`, a quoted or escaped spelling the watch does not settle" % raw


def legibility(command, cwd=None, depth=0, moved=False):
    """None when every program `command` runs can be read from its own text;
    else (kind, why, category): OWNER when a script it runs hits a gate,
    UNKNOWN when a part is opaque, `why` naming that part. `depth` 1 is a
    script's own text, where running another script is opaque."""
    from . import chat
    text = command or ""
    try:
        reader = chat._ShellReader(text)
        tokens = reader.read()
        commands = chat._simple_commands(tokens)
        programs = [chat._program(c.words) for c in commands]
    except chat._Unsettled:
        return _opaque("the watch cannot settle how the shell reads it")
    except Exception as e:               # noqa: BLE001 — a reader defect
        # is never a Yes: it reads as opaque, exactly as an unsettled text does
        return _opaque("the shell reader failed on it (%s)"
                       % e.__class__.__name__)
    for _at, quoted, start, end in reader.bodies:
        body = "\n".join(reader.lines[start:end])
        if not quoted and ("$(" in body or "`" in body):
            return _opaque("its heredoc body runs a substitution the watch "
                           "does not read")
    moved = moved or any(p[0] in _DIR_CHANGES for p in programs)
    for cmd, (name, k) in zip(commands, programs):
        got = _read_simple(cmd.words, name, k, cwd, depth, moved)
        if got:
            return got
    # THE SUBSTITUTIONS RUN TOO: `$(..)`, backticks and `<(..)`/`>(..)`, each
    # read as a command of its own (the reader records their spans; nested
    # spans are read by the recursion, so only the outermost are walked)
    kept = []
    for s, e in sorted(reader.substs + reader.outs):
        if not any(a <= s and e <= b for a, b in kept):
            kept.append((s, e))
    for s, e in kept:
        got = legibility(text[s:e], cwd, depth, moved)
        if got:
            return got
    return None


def _read_simple(words, name, k, cwd, depth, moved):
    """legibility's verdict on ONE simple command (see the table above)."""
    from . import chat
    for w in words:
        if chat._REBINDING_ASSIGNMENT.match(w.raw):
            return _opaque("it sets %s, which changes what a program name runs"
                           % _clean(w.raw.split("=", 1)[0].rstrip("+"), 40))
    if k is None:
        return None                          # assignments only: nothing runs
    if name is None:
        if words[k].plain:
            return _opaque("its wrapper `%s` takes an option the watch does "
                           "not read, so the program it runs is not settled"
                           % _clean(words[k].raw, 40))
        return _opaque("its program is %s" % _unshown(words[k]))
    raw, base = words[k].raw, name.rsplit("/", 1)[-1]
    args = words[k + 1:]
    vals = [a.value() for a in args]
    if "/" in raw and not raw.startswith(chat._READER_HOMES):
        return _script(raw, None, cwd, depth, moved)
    if base in chat._EVALUATORS or base in _REBINDERS or base in _ARGV_RUNNERS \
            or (base == "find" and _FIND_EXEC.intersection(vals)):
        spelled = "find %s" % sorted(_FIND_EXEC.intersection(vals))[0] \
            if base == "find" else base
        return _opaque("it runs `%s`, which runs a command whose text is not "
                       "read as one here" % spelled)
    if base in _SHELLS:
        how, word = _shell_run(args)
        if how in ("code", "stdin"):
            return _opaque("it hands %s to `%s`, a re-executor" % (
                "a command string (-c)" if how == "code" else "its input",
                base))
        if how == "script":
            return (_opaque("its script is %s" % _unshown(word))
                    if word.value() is None else
                    _script(word.value(), "shell" if base in _READ_SHELLS
                            else base, cwd, depth, moved))
        return None
    if chat._PYTHON.fullmatch(base):
        return _python(args, base, cwd, depth, moved)
    if base in _OTHER_INTERPRETERS:
        if _OTHER_INTERPRETERS[base] and set(vals) & set(
                _OTHER_INTERPRETERS[base]):
            return _opaque("it hands a program string to `%s`, a re-executor"
                           % base)
        if args and all(v in _VERSION_READS for v in vals):
            return None
        return _opaque("it runs a %s program, and the watch reads only shell "
                       "and python scripts" % base)
    verb = next((v for v in vals if v is not None and not v.startswith("-")),
                None)
    if base in _RUNNERS or (base in _PACKAGE_MANAGERS and not (
            verb in _PACKAGE_READS or (verb is None and vals and all(
                v in _VERSION_READS for v in vals)))) \
            or verb in _VERB_RUNNERS.get(base, ()):
        return _opaque("it runs `%s`, a runner of code its repo defines or "
                       "it fetches, which is not on screen"
                       % _scrub(" ".join(filter(None, (base, verb))), 60))
    if base in _GATE_PROGRAMS:
        hidden = next((a for a, v in zip(args, vals) if v is None), None)
        if hidden is not None:
            return _opaque("`%s` is a program the owner's gates read by its "
                           "words, and its word %s" % (base, _unshown(hidden)))
    return None


def _shell_run(args):
    """(how, word) a shell is asked to run: ("code", None) for -c, ("stdin",
    None) when it reads its input, ("script", word), or ("none", None) for a
    version or help read."""
    j = 0
    while j < len(args):
        v = args[j].value()
        if v is None:
            return "script", args[j]
        if v == "--":
            return ("script", args[j + 1]) if j + 1 < len(args) else ("stdin",
                                                                      None)
        if v in ("-o", "+o", "-O", "+O", "--rcfile", "--init-file"):
            j += 2
        elif v in ("--version", "--help"):
            return "none", None
        elif v.startswith("--"):
            j += 1
        elif v[:1] in ("-", "+") and len(v) > 1:
            if "c" in v[1:]:
                return "code", None
            if "s" in v[1:]:
                return "stdin", None
            j += 1
        else:
            return "script", args[j]
    return "stdin", None


def _python(args, base, cwd, depth, moved):
    """legibility's verdict on one python invocation."""
    j, read_only = 0, False
    while j < len(args):
        v = args[j].value()
        if v is None:
            return _opaque("its python script is %s" % _unshown(args[j]))
        if v == "-":
            break
        if not v.startswith("-"):
            return _script(v, "python", cwd, depth, moved)
        if v in ("--version", "--help", "-V", "-h", "-VV"):
            read_only = True
            j += 1
            continue
        if v.startswith("--"):
            j += 1
            continue
        flags = v[1:]
        cut = next((i for i, c in enumerate(flags) if c in "cmWX"), None)
        if cut is None:
            j += 1
            continue
        if flags[cut] == "c":
            return _opaque("it hands a program string (-c) to `%s`, a "
                           "re-executor" % base)
        value = flags[cut + 1:] or (args[j + 1].value() if j + 1 < len(args)
                                    else None)
        if flags[cut] == "m":
            return _module(value, cwd, depth, moved)
        j += 1 if flags[cut + 1:] else 2
    if read_only:
        return None
    return _opaque("it hands its input to `%s` as a program, a re-executor"
                   % base)


def _module(name, cwd, depth, moved):
    """`python -m <name>`: tests are legible; any other module is a script,
    legible only as tracked files of the repo — its own and every parent
    package's `__init__.py`, all of which python runs."""
    if name in _TEST_MODULES:
        return None
    if not name or not re.fullmatch(r"[A-Za-z_][\w.]*", name):
        return _opaque("it runs a python module the text does not settle "
                       "(%s)" % _clean(name, 60))
    if depth:
        return _opaque("it runs a python module from a script: %s (the watch "
                       "reads one level)" % _clean(name, 60))
    if not cwd or moved:
        return _opaque("it runs the python module %s, and %s" % (
            _clean(name, 60), "the watch cannot tell which repo it runs in"
            if not cwd else "it changes directory first"))
    parts = name.split(".")
    files = [os.path.join(*(parts[:i] + ["__init__.py"]))
             for i in range(1, len(parts))]
    leaf = os.path.join(*parts)
    files.append(os.path.join(leaf, "__main__.py")
                 if os.path.isdir(os.path.join(cwd, leaf)) else leaf + ".py")
    for rel in files:
        if rel.endswith("__init__.py") and not os.path.lexists(
                os.path.join(cwd, rel)):
            continue                     # a namespace package runs no file
        got = _script(rel, "python", cwd, depth, moved)
        if got:
            return got
    return None


def _script(path, lang, cwd, depth, moved):
    """legibility's verdict on one script: read ONLY when it is a regular file
    tracked in the repo `cwd` is in, at most SCRIPT_READ_BYTES; then its text
    is gated and read, one level deep. `lang` is the interpreter it was handed
    to (None: its own shebang decides)."""
    shown = _scrub(path, 200)

    def unread(why):
        return _opaque("it runs a script whose text is not on screen: %s (%s)"
                       % (shown, why))
    if depth:
        return unread("run by a script; the watch reads one level")
    if not cwd:
        return unread("the watch cannot tell which repo the command runs in")
    p = os.path.expanduser(path)
    if not os.path.isabs(p):
        if moved:
            return unread("the command changes directory first, so which "
                          "file runs is not settled")
        p = os.path.join(cwd, p)
    try:
        st = os.lstat(p)
    except OSError:
        return unread("no such file")
    if not stat.S_ISREG(st.st_mode):
        return unread("not a regular file")
    if st.st_size > SCRIPT_READ_BYTES:
        return unread("larger than %d KiB, so its text was not read"
                      % (SCRIPT_READ_BYTES // 1024))
    if not _tracked(cwd, p):
        return unread("not a file tracked in its repo")
    try:
        with open(p, "rb") as f:
            data = f.read(SCRIPT_READ_BYTES + 1)
    except OSError as e:
        return unread("it could not be read: %s" % e.__class__.__name__)
    if len(data) > SCRIPT_READ_BYTES:
        return unread("larger than %d KiB, so its text was not read"
                      % (SCRIPT_READ_BYTES // 1024))
    if b"\0" in data:
        return unread("a binary, not a script")
    text = data.decode("utf-8", "replace")
    hit = _gate({"command": text})
    if hit:
        return OWNER, ("it runs %s, whose text holds %s (%r), one of the "
                       "owner's named exceptions" % (shown, hit[1], hit[2])
                       ), hit[0]
    lang = lang or _shebang(text)
    if lang == "shell":
        got = legibility(text, cwd, depth + 1)
        return got and (got[0], "it runs %s, and in that script %s"
                        % (shown, got[1]), got[2])
    if lang == "python":
        from . import chat
        return None if chat.python_text_inert(text) else unread(
            "a python script that imports past the inert allowlist, so what "
            "it runs is not on screen")
    return unread("it runs under %s, whose scripts the watch does not read"
                  % _clean(lang, 40))


def _shebang(text):
    """The language a script run by its path is read as: "shell" with no
    shebang (the shell runs such a file itself), else its interpreter."""
    from . import chat
    first = text.split("\n", 1)[0]
    if not first.startswith("#!"):
        return "shell"
    words = first[2:].split()
    prog = words[0].rsplit("/", 1)[-1] if words else "?"
    if prog == "env":
        rest = [w for w in words[1:] if not w.startswith("-") and "=" not in w]
        prog = rest[0].rsplit("/", 1)[-1] if rest else "env"
    if prog in _READ_SHELLS:
        return "shell"
    if chat._PYTHON.fullmatch(prog):
        return "python"
    return prog


def _tracked(cwd, path):
    """Is `path` a file git tracks in the repo `cwd` is in? Asked through the
    vcs seam under its authority overlay (repository selection and config
    injection removed, so the repository is the one `cwd` names), with the
    path a LITERAL pathspec (a `*` in a name is a letter). Spawn trouble is
    rc -1, which reads as untracked: opaque, never a Yes."""
    from . import vcs
    rc, _out, _err = vcs.backend(cwd).run(
        cwd, "--literal-pathspecs", "ls-files", "--error-unmatch", "--", path,
        timeout=5, env=vcs._authority_env())
    return rc == 0


def judge_ask(ask, root):
    """(kind, why, category) for ONE ask. The owner's gates first, then the
    kinds this watch answers; anything else is UNKNOWN, never a guess."""
    hit = _gate(ask)
    if hit:
        return OWNER, ("it asks to %s — %s (%r), one of the owner's named "
                       "exceptions" % (plain(ask), hit[1], hit[2])), hit[0]
    tool = ask.get("tool")
    if tool in FILE_TOOLS or (tool == "file" and ask.get("verb") != "read"):
        path = ask.get("path")
        if not path:
            return UNKNOWN, ("it asks to %s, and the file's full path is not "
                             "on screen and no pending call names it"
                             % plain(ask)), None
        if _own_memory(path, root):
            return MEMORY, "an auto-memory write (%s)" % plain(ask), None
        if any(part.startswith(".claude") for part in path.split("/")):
            return UNKNOWN, ("it asks to %s, a Claude config folder that is not "
                             "its memory" % plain(ask)), None
        return ROUTINE, "a file write in its work (%s)" % plain(ask), None
    if tool in READ_TOOLS or tool == "file" or tool in WEB_TOOLS:
        return ROUTINE, "a read (%s)" % plain(ask), None
    if tool == "Bash":
        if not ask.get("command"):
            return UNKNOWN, ("a Bash prompt whose command is not on screen and "
                             "no pending call names it"), None
        seen = legibility(ask["command"], ask.get("cwd"))
        if seen:
            return seen[0], "it asks to %s — %s" % (plain(ask), seen[1]), seen[2]
        return ROUTINE, "a routine command (%s)" % plain(ask), None
    return UNKNOWN, ("it asks to %s, a kind of prompt the watch does not "
                     "answer" % plain(ask)), None


def _pick(options, widen):
    """(digit, label) the kind calls for, read off the dialog, or (None, None).
    A memory write takes the session-wide Yes when offered so the next one does
    not stop; a routine ask takes exactly `Yes` and never widens a rule."""
    yes = [(str(n), str(label)) for n, label in options or ()
           if _YES.match(str(label))]
    if widen:
        for n, label in yes:
            if _WIDEN.search(label):
                return n, label
    for n, label in yes:
        if _PLAIN_YES.match(label.strip()):
            return n, label
    return None, None


def judge(tail, row, calls):
    """The verdict on one stalled prompt from both witnesses ->
    {kind, why, category, ask, keys, enter, label, signature, excerpt}.

    `tail` is one pane read ("" when the pane is blind); `calls` is the
    transcript's pending calls (None when unread). A readable pane that shows
    no dialog while the record says a prompt waits is a contradiction, and
    UNKNOWN. A blind pane is answered from the transcript alone, and only with
    the dialog's default."""
    pane = pane_ask(tail)
    v = {"kind": UNKNOWN, "why": None, "category": None, "ask": None,
         "keys": None, "enter": False, "label": None,
         "signature": pane["signature"], "excerpt": pane["excerpt"]}
    blind = not pane["lines"]
    # THE DIALOG'S OWN QUESTION is gated only when it is not a file action: a
    # file question quotes a file NAME, and `token_store.py` is not a login.
    if pane["question"] and not _FILE_Q.match(pane["question"]):
        kind, why, cat = judge_ask({"question": pane["question"]}, row.get("root"))
        if kind == OWNER:
            v.update(kind=kind, why=why, category=cat,
                     ask={"question": pane["question"]})
            return v
    if row.get("waiting_for") != PERMISSION:
        v["why"] = ("its record says it waits for %r, not a tool permission, "
                    "so helm does not guess an answer" % row.get("waiting_for"))
        return v
    if not blind and not pane["question"]:
        v["why"] = ("its pane shows no current Yes/No dialog under a question "
                    "although its record says a prompt waits")
        return v
    bound = _bind(pane, calls)
    shown = {k: pane[k] for k in ("tool", "verb", "name", "path", "command")}
    asks = ([bound] if bound else
            [shown] if not blind and pane["tool"] else
            [call_ask(c) for c in calls or ()])
    if not asks:
        v["why"] = ("neither its pane nor its transcript names what the "
                    "prompt asks%s" % ("" if calls is not None else
                                       " (the transcript could not be read)"))
        return v
    verdicts = [(a,) + judge_ask(a, row.get("root")) for a in asks]
    for want in (OWNER, UNKNOWN):
        for a, kind, why, cat in verdicts:
            if kind == want:
                v.update(kind=kind, why=why, category=cat, ask=a)
                return v
    kind = MEMORY if any(k == MEMORY for _a, k, _w, _c in verdicts) else ROUTINE
    v.update(kind=kind, ask=asks[0], why="; ".join(w for _a, _k, w, _c in verdicts))
    if blind:
        v.update(keys="", enter=True,
                 label="Enter, the dialog's default (its first option, Yes)")
        return v
    digit, label = _pick(pane["options"], kind == MEMORY and len(asks) == 1)
    if not digit:
        v.update(kind=UNKNOWN, why="%s, but the dialog offers no plain Yes "
                 "(options %r)" % (v["why"], pane["options"]))
        return v
    v.update(keys=digit, label="%s (%s)" % (digit, _clean(label, 80)))
    return v


# ---------------------------------------------------------------------------
# the typist: one operation through resumeturn's delivery transaction
# ---------------------------------------------------------------------------

def _presence_of(row):
    from . import session
    return session.read_session_presence(row["root"], row["pid"],
                                         os.geteuid(), row["start"])


def _pending_of(row):
    return pending_calls(row.get("session"), row.get("root"))


def _left(rec, row):
    """Did the vendor's own record leave THIS waiting episode?"""
    return rec.get("status") != "waiting" or \
        rec.get("statusUpdatedAt") != row.get("stamp")


def _output_age(ad, handle, now):
    """Seconds since this pane last printed, or None when the host cannot say
    (then the humans are AFK, the owner's standing assumption)."""
    try:
        rows = ad.list()
    except Exception:                        # noqa: BLE001 — unmeasured is AFK
        return None
    for r in rows or ():
        at = r.get("last_output_at") if r.get("handle") == handle else None
        if isinstance(at, (int, float)) and not isinstance(at, bool):
            return now - at / 1000.0
    return None


def _await_move(row, presence, ad, handle, signature, sleep):
    """(True | False | None, proof) — did the pane MOVE? The vendor's record
    leaving this waiting episode is the proof; the pane showing a different
    dialog is the second witness (a prompt that follows another at once is
    not rewritten into the record). None: neither could be read."""
    last_why = None
    for _ in range(MOVE_TRIES):
        sleep(MOVE_WAIT_S)
        rec, why = presence(row)
        if isinstance(rec, dict):
            last_why = None
            if _left(rec, row):
                return True, ("its record left the prompt (status %s)"
                              % _clean(rec.get("status")))
        else:
            last_why = why or "no presence record"
    try:
        after = ad.read(handle) or ""
    except Exception:                        # noqa: BLE001 — a blind re-read
        after = ""
    shown = pane_ask(after)
    if shown["lines"] and signature and shown["signature"] != signature:
        return True, "its pane no longer shows that dialog"
    if last_why and not shown["lines"]:
        return None, ("whether it moved could not be read (%s, and a blank "
                      "pane)" % last_why)
    return False, ("it did not move in %.0fs: its record still waits since "
                   "%s" % (MOVE_TRIES * MOVE_WAIT_S,
                           time.strftime("%H:%MZ", time.gmtime(row["since"]))))


def _press(ad, handle, keys, enter):
    """THE keystroke. `handle` is a parameter on purpose: it is the one the
    delivery transaction proved and handed to answer_stall's operation, and
    this function never chooses a pane."""
    ad.send(handle, keys, enter=enter)


def answer_stall(row, now=None, deliver=None, presence=None, calls=None,
                 adapter=None, sleep=time.sleep):
    """Answer ONE stalled prompt -> {outcome, kind, why, category, ask, keys,
    label, handle, detail, excerpt, typed}.

    outcome is answered | not-moved | unproven | owner | integrator | human |
    moved | refused. Everything between the pane read and the keystroke runs
    inside the delivery transaction, so the verdict, the keys and the proof
    describe one pane: read it, judge it, re-read the record (a prompt someone
    else answered types nothing), press, then prove the move."""
    from . import harness, orcaadopt, resumeturn
    now = time.time() if now is None else now
    presence = presence or _presence_of
    pending, pending_why = (calls or _pending_of)(row)
    out = _result()

    def op(ad, handle, _on_typed):
        out["handle"] = handle
        age = _output_age(ad, handle, now)
        if age is not None and age < HUMAN_QUIET_S:
            out.update(outcome="human", detail=(
                "pane %s printed %.0fs ago while its prompt has waited %ds, so "
                "someone may be at it; nothing was typed" % (handle, age,
                                                              row["waited_s"])))
            return harness.NOT_DELIVERED, out["detail"]
        try:
            tail = ad.read(handle) or ""
        except Exception:                    # noqa: BLE001 — a blind pane
            tail = ""
        v = judge(tail, row, pending)
        out.update({k: v[k] for k in ("kind", "why", "category", "ask",
                                      "excerpt", "label")})
        if v["kind"] in (OWNER, UNKNOWN):
            out.update(outcome=OWNER if v["kind"] == OWNER else "integrator",
                       detail="nothing was typed: %s" % v["why"])
            if pending is None and v["kind"] == UNKNOWN:
                out["detail"] += " (%s)" % pending_why
            return harness.NOT_DELIVERED, out["detail"]
        rec, why = presence(row)
        if not isinstance(rec, dict):
            out.update(outcome="recheck", detail=(
                "its record could not be re-read before the keystroke (%s), so "
                "nothing was typed; the watch reads it again next pass" % why))
            return harness.NOT_DELIVERED, out["detail"]
        if _left(rec, row):
            out.update(outcome="moved", detail=(
                "the prompt left before the keystroke (status %s); nothing "
                "was typed" % _clean(rec.get("status"))))
            return harness.NOT_DELIVERED, out["detail"]
        try:
            _press(ad, handle, v["keys"], v["enter"])
        except harness.HarnessError as e:
            if getattr(e, "request_absent", False):
                out.update(outcome="refused", detail=(
                    "the keystroke never left this box: %s" % e))
                return harness.NOT_DELIVERED, out["detail"]
            out.update(outcome="unproven", typed=True, keys=v["keys"], detail=(
                "the send of %s failed in a way that cannot say whether it "
                "arrived: %s" % (v["label"], e)))
            return harness.UNCERTAIN, out["detail"]
        out.update(typed=True, keys=v["keys"])
        moved, proof = _await_move(row, presence, ad, handle, v["signature"],
                                   sleep)
        out.update(outcome={True: "answered", False: "not-moved",
                            None: "unproven"}[moved], proof=proof,
                   detail="pressed %s into pane %s; %s" % (v["label"], handle,
                                                           proof))
        return ({True: harness.DELIVERED, False: harness.NOT_DELIVERED,
                 None: harness.UNKNOWN}[moved], out["detail"])

    pids = ([orcaadopt.ProcIdent(row["pid"], row.get("start"))]
            if row.get("pane_key") else None)
    try:
        _mode, proof = (deliver or resumeturn.deliver)(
            row["seat"], "", row.get("session"), adapter=adapter, pids=pids,
            operation=op)
    except Exception as e:                   # noqa: BLE001 — one seat, not the pass
        _mode, proof = None, "the delivery raised %s: %s" % (
            e.__class__.__name__, e)
    if out["outcome"] is None:
        out.update(outcome="refused", detail=(
            "no pane was bound, so nothing was typed: %s" % proof))
    return out


def preview_stall(row, calls=None):
    """The DRY verdict: the transcript witness alone, no pane read, nothing
    typed. What the live pass would do when the pane agrees."""
    pending, why = (calls or _pending_of)(row)
    v = judge("", row, pending)
    if v["kind"] in (MEMORY, ROUTINE):
        return "would answer (%s)" % v["why"]
    if v["kind"] == OWNER:
        return "would page the owner (%s)" % v["why"]
    return "would address the integrator (%s%s)" % (
        v["why"], "" if pending is not None else "; %s" % why)


# ---------------------------------------------------------------------------
# the words
# ---------------------------------------------------------------------------

#: WHO READS EACH OUTCOME. `quiet` names nobody and pushes nothing; `integrator`
#: addresses the integrator seat; `owner` addresses the owner (and the
#: integrator) and rides the one phone push. None posts nothing.
ROUTES = {"answered": "quiet", "owner": "owner", "freeze": "owner",
          "off": "owner", "integrator": "integrator", "refused": "integrator",
          "not-moved": "integrator", "unproven": "integrator",
          "human-long": "integrator", "human": None, "moved": None,
          "recheck": None}


def stall_text(row, owner, integrator=None, relaunch=None, result=None):
    """The row for ONE stall, in plain words: who must act (if anyone), what
    the prompt asks, and what the watch DID. Keyed on the result's outcome."""
    from .seatstale import span
    result = result or {"outcome": "integrator", "why": "nothing classified it"}
    outcome = result["outcome"]
    row = dict(row, waiting_for=(row["waiting_for"] if "prompt" in
                                 row["waiting_for"] else
                                 "prompt (%s)" % row["waiting_for"]))
    at = time.strftime("%H:%MZ", time.gmtime(row["since"]))
    waited = "%s (since %s)" % (span(row["waited_s"]), at)
    asks = plain(result["ask"]) if result.get("ask") else None
    pane = (" Pane %s:\n%s" % (result.get("handle") or "?", result["excerpt"])
            if result.get("excerpt") else "")
    both = " ".join("@%s" % n for n in dict.fromkeys(
        x for x in (owner, integrator) if x))
    orca = ("`orca terminal send --terminal %s --text <digit>` (a digit picks "
            "that option; --enter alone takes the default)"
            % (result.get("handle") or "<its terminal>"))
    if outcome == "answered":
        text = ("seat %s had waited %s at a Claude Code %s to %s; the watch "
                "answered %s in pane %s and the pane moved: %s."
                % (row["seat"], waited, row["waiting_for"], asks or "act",
                   result.get("label"), result.get("handle"),
                   result.get("proof") or result.get("detail")))
    elif outcome == "owner":
        text = ("%s seat %s has waited %s at a Claude Code %s: %s. That is "
                "yours to decide, so the watch typed nothing. Answer it in seat "
                "%s's pane." % (both, row["seat"], waited, row["waiting_for"],
                           result["why"], row["seat"]))
    elif outcome == "freeze":
        text = ("%s seat %s is a FREEZE candidate: it has waited %s at a "
                "Claude Code %s, and the watch pressed %s %d times without the "
                "pane moving (%s). Relaunch it: %s."
                % (both, row["seat"], waited,
                   row["waiting_for"], result.get("label") or "its answer",
                   ANSWER_TRIES, result.get("detail"),
                   result.get("relaunch") or "its launch door with --resume"))
    elif outcome == "off":
        text = ("@%s seat %s has waited %s at a Claude Code %s. helm's prompt "
                "answering is off for it (%s), so it waits for you: answer it "
                "in its pane." % (owner, row["seat"], waited,
                                  row["waiting_for"], result["why"]))
    elif outcome in ("not-moved", "unproven"):
        text = ("@%s seat %s has waited %s at a Claude Code %s to %s; %s. "
                "Answer it with orca-cli: %s. A second failure pages the owner "
                "as a freeze.%s"
                % (integrator or owner, row["seat"], waited, row["waiting_for"],
                   asks or "act", result.get("detail"), orca, pane))
    else:                                    # integrator | refused | human-long
        text = ("@%s seat %s has waited %s at a Claude Code %s that the watch "
                "did not answer: %s. Answer it with orca-cli: %s.%s"
                % (integrator or owner, row["seat"], waited, row["waiting_for"],
                   result.get("detail") or result.get("why"), orca, pane))
    if relaunch and outcome != "freeze":
        text += (" Its session predates its home's auto-memory base, so each "
                 "memory write stops at this prompt until it is relaunched at a "
                 "convenient moment with --resume: %s." % relaunch)
    return text + " [prompt-stall watch]"


# ---------------------------------------------------------------------------
# one pass
# ---------------------------------------------------------------------------

def _relaunch_of(row):
    from . import doctor
    return doctor.memory_relaunch({
        "seat": row["seat"], "session": row.get("session"),
        "root": row.get("root"),
        "home": os.path.basename(str(row.get("root") or "").rstrip(os.sep))})


def _result(**kw):
    base = {"outcome": None, "kind": None, "why": None, "category": None,
            "ask": None, "keys": None, "label": None, "handle": None,
            "detail": None, "proof": None, "excerpt": None, "typed": False}
    base.update(kw)
    return base


def _step(row, entry, now, run):
    """(result, entry) for ONE stall under the latch. At most one attempt, and
    one post, per STALL_REPEAT_S; ANSWER_TRIES failed attempts make a FREEZE
    candidate that is paged and never typed into again this episode."""
    entry = dict(entry) if entry.get("since") == row["since"] else {}
    if entry.get("at") is not None and now - entry["at"] < STALL_REPEAT_S:
        return None, entry
    off = answer_off(row)
    if off:
        return _result(outcome="off", why=off), entry
    if entry.get("tries", 0) >= ANSWER_TRIES:
        return _result(outcome="freeze", relaunch=_relaunch_of(row),
                       **(entry.get("last") or {})), entry
    result = _result(**run(row, now=now))
    if result["typed"]:
        entry["tries"] = entry.get("tries", 0) + 1
        entry["last"] = {k: result.get(k) for k in ("label", "detail", "keys")}
        if result["outcome"] != "answered" and entry["tries"] >= ANSWER_TRIES:
            result = dict(result, outcome="freeze", relaunch=_relaunch_of(row))
    if result["outcome"] == "human":
        entry.setdefault("human_at", now)
        if now - entry["human_at"] >= STALL_REPEAT_S:
            result = dict(result, outcome="human-long")
    return result, entry


def stall_pass(post=True, dry=False, now=None, found=None, memory=None,
               answer=None, preview=None):
    """One pass: find the stalls, ANSWER what the standing authorization
    covers, and route the rest — the owner for his named exceptions and for a
    freeze, the integrator for a shape the watch cannot read.

    -> {"stalls", "blind", "answered", "posted", "lines"}. `dry` types, posts
    and latches nothing and prints the transcript's verdict for each stall.
    The typing latch is stamped whether or not its post lands (a failed post
    must never re-type); a post that did not land is owed and retried without
    a keystroke. A row that typed nothing is latched only once its post lands,
    so a failed post re-surfaces next pass."""
    from . import pk, seats
    now = time.time() if now is None else now
    found = prompt_stalls(now=now) if found is None else found
    lines = []
    if not found["read"]:
        lines.append("prompt-stall watch: %s — whether any seat is frozen at a "
                     "prompt is UNKNOWN" % found["why"])
    blind = found.get("blind") or []
    if blind:
        named = ["%s (%s)" % ("seat %s" % b["seat"] if b["seat"] else
                              "an unnamed session, pid %s" % b["pid"], b["why"])
                 for b in blind[:BLIND_NAME_CAP]]
        more = len(blind) - BLIND_NAME_CAP
        lines.append("prompt-stall watch: %d session(s) UNKNOWN — their presence "
                     "record could not say whether they are parked at a "
                     "prompt: %s%s" % (len(blind), "; ".join(named),
                                       " and %d more" % more if more > 0 else ""))
    stalls = found["stalls"]
    res = {"stalls": stalls, "blind": blind, "answered": [], "posted": [],
           "lines": lines}
    if dry:
        see = preview or preview_stall
        lines.extend("seat %s at a %s for %ds: %s (dry run: nothing typed or "
                     "posted)" % (r["seat"], r["waiting_for"], r["waited_s"],
                                  see(r)) for r in stalls)
    if not stalls or dry:
        return res
    if memory is None:
        try:
            from . import doctor, seatstale
            memory = {r["pid"]: doctor.memory_relaunch(r) for r in
                      seatstale.memory_base_state()["predates"]}
        except Exception:                    # noqa: BLE001 — the relaunch hint
            memory = {}                      # is extra; the stall still posts
    try:
        from .seats_integrator import integrator_seat
        integrator, _why = integrator_seat()
    except Exception:                        # noqa: BLE001
        integrator = None
    owner = seats.owner_name()
    run = answer or answer_stall
    paged = []
    p = _state_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p + ".lock", "a") as lock:
        # ONE TYPIST AT A TIME: the lock that latches also serializes the
        # keystrokes, so two passes can never both answer one prompt.
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        st = pk.read_json(p, {}) or {}
        live = {_STALL_KEY + r["seat"] for r in stalls}
        for key in [k for k in st if k.startswith(_STALL_KEY) and k not in live]:
            del st[key]                      # that stall ended; a new one posts
        for row in stalls:
            key = _STALL_KEY + row["seat"]
            owed = (st.get(key) or {}).get("owed")
            if owed and post and _post_stall(owed):
                st[key].pop("owed", None)
            result, entry = _step(row, st.get(key) or {}, now, run)
            if result is None:
                lines.append("seat %s at a %s for %ds (already handled this "
                             "window)" % (row["seat"], row["waiting_for"],
                                          row["waited_s"]))
                continue
            route = ROUTES.get(result["outcome"], "integrator")
            if route == "integrator" and not integrator:
                route = "owner"              # nobody else is named to act
            text = stall_text(row, owner, integrator, memory.get(row["pid"]) or
                              (_relaunch_of(row) if result.get("kind") == MEMORY
                               else None), result)
            if result["outcome"] == "answered":
                res["answered"].append(row)
            lines.append("seat %s at a %s for %ds: %s — %s"
                         % (row["seat"], row["waiting_for"], row["waited_s"],
                            result["outcome"], result.get("detail")
                            or result.get("why")))
            landed = route is not None and post and _post_stall(text)
            if landed:
                res["posted"].append(row)
                if route == "owner":
                    paged.append((row, result))
            elif route is not None and post:
                lines.append("prompt-stall watch: chat post FAILED for seat %s "
                             "— %s" % (row["seat"], "it is owed and retried "
                                       "without a keystroke" if result["typed"]
                                       else "it re-surfaces next pass"))
            if result["typed"] or route is None or landed or not post:
                entry["at"] = None if route is None else now
                if route is not None and post and not landed:
                    entry["owed"] = text
                entry["since"] = row["since"]
                st[key] = entry
        pk.write_json(p, st)
    if post and paged:
        from . import notify
        body = "; ".join("%s waits at a %s for %d min: %s" % (
            r["seat"], r["waiting_for"], r["waited_s"] // 60,
            "a FREEZE candidate" if x["outcome"] == "freeze" else
            _clean(x.get("why"), 160)) for r, x in paged)
        if not notify.owner_push(body + " — answer it in the seat's pane",
                                 title="helm: a seat waits on you",
                                 receipt=("prompt-stall", "push")):
            lines.append("prompt-stall watch: owner push FAILED — the fleet "
                         "room has the rows, the phone does not")
    return res


def _post_stall(text):
    """Post under the module constant, so the machine-sender walker resolves
    the label (a `who` passed through a parameter is unresolvable)."""
    from . import chat
    try:
        chat.post(text, who=STALL_WHO, room=ROOM)
        return True
    except Exception as e:               # noqa: BLE001 — a down room never
        print("prompt-stall watch: chat post failed: %s" % e, file=sys.stderr)
        return False


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def unreadable(rows):
    """[seat] whose state nobody established this pass — a crashed probe or an
    UNKNOWN pane."""
    return [r["seat"] for r in rows
            if r["outcome"] == "blind" or r["state"] in (None, "UNKNOWN")]


def clean_bill(rows):
    """The no-findings line, QUALIFIED by what the pass could not see.

    NEVER a bare "nobody is blocked" while a pane went unread. A seat whose
    state is UNKNOWN is exactly a seat that could be sitting at a prompt with
    nothing able to see it, so a clean bill over it is this rung's own failure
    mode restated as output (silent_drop's law). Measured on the live fleet the
    day this was written: 7 seats scanned, one UNKNOWN.
    """
    blind = unreadable(rows)
    if not blind:
        return "no seat is blocked on a human prompt (%d scanned)" % len(rows)
    return ("no blocked prompt in %d readable seat(s) — %d pane(s) COULD NOT "
            "BE READ, so this is not a clean bill: %s"
            % (len(rows) - len(blind), len(blind), ", ".join(blind)))


def cmd_unblock(argv=None):
    args = list(argv if argv is not None else sys.argv[1:])
    if "-h" in args or "--help" in args:
        print(_USAGE)
        return 0
    want = None
    if "--seat" in args:
        i = args.index("--seat")
        want = [args[i + 1]] if i + 1 < len(args) else None
        if want is None:
            print("helm seat unblock: --seat needs a seat name", file=sys.stderr)
            return 2
    dry = "--dry-run" in args
    res = check(seats=want, post=not dry, quiet="--quiet" in args, dry=dry)
    # Every Claude seat's prompt state, not only the proxy seats `check` scans
    # (see THE PROMPT-STALL WATCH). A --seat read is one proxy seat's question.
    stalls = {"stalls": [], "blind": [], "lines": []} if want else stall_pass(
        post=not dry and "--quiet" not in args, dry=dry)
    if "--json" in args:
        res["stalls"] = stalls["stalls"]
        res["stalls_blind"] = stalls.get("blind") or []
        res["stalls_answered"] = [r["seat"] for r in stalls.get("answered") or ()]
        print(json.dumps(res))
        return 0
    for line in stalls["lines"]:
        print(line)
    acted = [r for r in res["rows"] if r["outcome"] != "not-blocked"]
    for r in acted:
        print("%-12s %-14s %s" % (r["seat"], r["outcome"], r["detail"]))
    if not acted and not stalls["stalls"] and not stalls.get("blind"):
        # NEVER a bare "nobody is blocked" while a pane went unread. A seat
        # whose state is UNKNOWN is exactly a seat that COULD be sitting at a
        # prompt with nothing able to see it — reporting a clean bill over it
        # is the failure this rung exists to end, restated as output. Measured
        # on the live fleet the day this landed: 7 seats, one UNKNOWN.
        print(clean_bill(res["rows"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(cmd_unblock())
