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
the owner named the level: "the fix is just injecting into the pane". A human
permission dialog is intentionally outside this actuator: typing there would
impersonate the authorization it asks the owner to supply.

=== THE FOUR RUNGS, AND WHAT EACH ONE REFUSES ===

R1 STATE — consumed, never re-derived. `seat.seat_liveness` is THE pane-tail
   reader; this module never opens a pane to classify one (proxywatch's
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
   it also includes permission questions, whose answers are attributed to the
   human. A `plans/` path is supporting evidence, never that type authority — a
   permission question can quote one. Anything not explicitly typed
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
  Also names every Claude seat (native ones too) parked at a Claude Code
  prompt for 3 minutes or more, read from its own presence record, and posts
  it to the owner and the integrator; it never answers one.
  --dry-run assesses and writes nothing (no send, no post, no latch); --quiet
  skips the chat rows; --json prints the rows.
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
            "prompt — a plans/ path and affirmative options do not authorize "
            "a peer to impersonate human permission"
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
# transcript parse. `session.read_session_presence` is the bracketed reader.
#
# SURFACE, NEVER ANSWER. A permission prompt is the owner's authority; this
# watch posts it LOUDLY (the owner and the integrator by name, plus one phone
# push per pass) and types nothing anywhere.
PROMPT_STALL_S = 180           # parked this long at a prompt is a stall
STALL_REPEAT_S = LATCH_TTL_S   # a stall that persists re-surfaces this often
STALL_WHO = "prompt-stall"
_STALL_KEY = "stall:"
#: The presence states the installed program writes (busy, idle, shell,
#: waiting). Any other value, or none, is a record this watch cannot read.
PRESENCE_STATES = ("busy", "idle", "shell", "waiting")
BLIND_NAME_CAP = 5


def _clean(text, limit=40):
    """A vendor string bound for a chat row: printable, bounded."""
    t = "".join(c for c in str(text or "") if c.isprintable())
    return t[:limit]


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
    through `_seat_label` before it can reach a chat row. The owner's own sessions are not surfaced: he is
    the one they wait for. Every input is injectable so an arm owns it."""
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
        raw = (row.get("environ") or {}).get("HELM_CHAT_NAME") or \
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
            "session": rec.get("sessionId"),
            "waiting_for": _clean(rec.get("waitingFor")) or "prompt",
            "since": since / 1000.0, "waited_s": int(waited)})
    out["stalls"].sort(key=lambda r: (-r["waited_s"], r["seat"]))
    return out


def stall_text(row, owner, integrator=None, relaunch=None):
    """The LOUD row. It names who must act, what the seat is stuck on and for
    how long, why nothing else will fix it, and — when the seat's session
    predates its home's memory base — the relaunch that stops it recurring."""
    from .seatstale import span
    at = time.strftime("%H:%MZ", time.gmtime(row["since"]))
    who = "@%s" % owner + (" @%s" % integrator if integrator else "")
    text = ("%s seat %s has been FROZEN at a Claude Code %s for %s (since "
            "%s). Nothing it owes can move until a human answers that prompt "
            "in its pane: its inbox beacon cannot be re-armed from inside it, "
            "and helm does not answer a permission for a human. Open seat %s's "
            "pane and answer it."
            % (who, row["seat"], row["waiting_for"], span(row["waited_s"]),
               at, row["seat"]))
    if relaunch:
        text += (" Its session started before its home carried the auto-memory "
                 "base, so every memory write it makes will stop here again "
                 "until it is relaunched with --resume: %s." % relaunch)
    return text + " [prompt-stall watch]"


def stall_pass(post=True, dry=False, now=None, found=None, memory=None):
    """One pass: find the stalls, post each NEW one (or one still standing
    after STALL_REPEAT_S) to the fleet room addressed to the owner and the
    integrator, push the batch to the owner's phone once, and latch.

    -> {"stalls", "posted", "lines"}. `dry` reads and writes nothing. The
    latch is stamped only for a row whose chat post succeeded, so a failed
    post re-surfaces next pass instead of going quiet."""
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
    res = {"stalls": stalls, "blind": blind, "posted": [], "lines": lines}
    if dry:
        lines.extend("seat %s FROZEN at a %s for %ds (dry run: nothing posted)"
                     % (r["seat"], r["waiting_for"], r["waited_s"])
                     for r in stalls)
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
    p = _state_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p + ".lock", "a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        st = pk.read_json(p, {}) or {}
        live = {_STALL_KEY + r["seat"] for r in stalls}
        for key in [k for k in st if k.startswith(_STALL_KEY) and k not in live]:
            del st[key]                      # that stall ended; a new one posts
        for row in stalls:
            e = st.get(_STALL_KEY + row["seat"]) or {}
            if e.get("since") == row["since"] and \
                    now - (e.get("at") or 0) < STALL_REPEAT_S:
                continue
            text = stall_text(row, owner, integrator, memory.get(row["pid"]))
            if post and not _post_stall(text):
                lines.append("prompt-stall watch: chat post FAILED for seat %s "
                             "— it re-surfaces next pass" % row["seat"])
                continue
            st[_STALL_KEY + row["seat"]] = {"at": now, "since": row["since"]}
            res["posted"].append(row)
        pk.write_json(p, st)
    for row in stalls:
        lines.append("seat %s FROZEN at a %s for %ds%s"
                     % (row["seat"], row["waiting_for"], row["waited_s"],
                        "" if row in res["posted"] else " (already surfaced)"))
    if post and res["posted"]:
        from . import notify
        body = "; ".join("%s frozen at a %s for %d min" % (
            r["seat"], r["waiting_for"], r["waited_s"] // 60)
            for r in res["posted"])
        if not notify.owner_push(body + " — answer it in the seat's pane",
                                 title="helm: seat frozen at a prompt",
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
