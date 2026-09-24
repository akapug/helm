"""helm.owedpush — the DELIVERY LEG for unanswered FIX debt.

THE GAP, MEASURED (owner ask 2026-08-11: "is there a path to required commits
automatically moving a row in the system and sending a notification rather than
requiring noticing and active dispatch"). Every half of the answer already
existed except the last one:

  · the DEFECT was already diagnosed — obligation.unanswered_fixes' own
    docstring: "a FIX verdict tells the author to cure, and nothing anywhere
    tells them that CURING CREATES A NEW OBLIGATION".
  · the COMPUTATION already exists and is complete — per row it names the
    lane, the chain root, the owing seat, the reviewed tip, and a `what`
    string that spells the exact next command.
  · the SURFACES already exist — `helm owed` (obligation.cmd_owed, routed
    lazily at cli.py) and the web burn-down (web_owed.py).

BOTH SURFACES ARE PULL. One is a CLI verb somebody must TYPE, the other a web
view somebody must OPEN, and the computation therefore reaches nobody who is
not already looking. Measured on the live estate the night this was written:
130 items across 109 seat-addressable debts, median age 12.7 DAYS, while
`helm owed` could have named every one of them on demand. An author cured a
FIX verdict fifteen minutes after receiving it and the row sat 22 HOURS,
because a cure is a commit and only a DISPATCH moves a row.

THIS MODULE IS THE PUSH, AND ONLY THE PUSH. It computes nothing: every item,
every addressee and every word of remedy text comes from obligation. It is a
CALLER, never a fork — obligation.py states "it is not a second chat" and that
boundary is correct, so delivery rides `helm chat`'s existing DM rail and the
existing mention-wakes-a-seat semantics rather than a parallel notifier.

WHY THIS PREDICATE CANNOT FIRE ON NOISE, which is the usual way a reactor like
this ships broken. The obvious design reads each lane's worktree HEAD and
fires when it moves past the reviewed tip — and that design fires on a
`helm-work` bot's mid-edit SNAPSHOT COMMIT, on a docs typo, and on an
in-flight subagent's partial work, so it needs a clean-tree heuristic to
suppress its own false positives. obligation's predicate is PURE LEDGER SHAPE:
a FIX row that no LIVE row supersedes. It never reads a worktree, so a wip
commit, a dirty tree and a rebase are all INVISIBLE to it by construction, and
the only thing that clears an item is a real ledger event (a superseding
dispatch, a cancel, a withdrawal). There is no noise channel to filter.

THE COST OF THAT CHOICE, STATED HONESTLY: because it never reads git, it
cannot distinguish "cured and not re-dispatched" from "never cured at all".
obligation ruled that deliberately — both want the SAME next action from the
SAME person — and the `what` string is written to cover both branches without
guessing which one the reader is in.

IT NEVER BLOCKS. The whole surface is a chat post. Nothing here gates a land,
refuses a verb, or fails a build: a false notification costs one line in a DM,
while a false BLOCK on a busy lane costs a seat its turn.

WHY THIS IS NOT A PREDICATE INSIDE idle_dispatch, ASKED AND MEASURED. That
module already pushes DMs about dispatch rows and already separates a seat that
is QUIET-BUT-WORKING from one that is ABSENT, so folding this in as one more
predicate is the obvious economy. It is not available, and the reason is
structural rather than stylistic: idle_dispatch scans `dispatches.owed()` —
OPEN rows, explicitly "not verdict, not cancelled" — while an unanswered FIX
debt lives on a row a VERDICT already closed. Measured on the live ledger:
idle_dispatch's population was 8 rows, all `status=open`; this one was 132
rows, all `status=verdict`; the intersection was ZERO, and 0 of 132 passed an
open/held filter. A predicate added to that loop would run over a population
that by construction never contains the rows it is looking for.

AND ITS quiet-but-working DISCRIMINATOR MUST NOT BE BORROWED, for the same
reason inverted. idle_dispatch asks "should this row be REASSIGNED?", where a
seat holding a live claim is busy and the answer is no. This asks "does this
seat KNOW it owes a re-dispatch?", where the re-dispatch is a ten-second
ledger action rather than work, and being busy is the CAUSE of the defect
rather than a reason for silence. Measured: 77 of 132 debts (58%) are owed by
seats holding at least one live claim, so a busy-suppressor would mute most of
the burn-down — including the incident that motivated the whole mechanism, an
author who cured FIFTEEN MINUTES after the verdict (demonstrably active) whose
row then sat 22 hours.
"""
import fcntl
import json
import os
import sys
import time

from . import home, obligation, pk, seats_integrator

BOT = "owed-bot"
# One ask per debt per three days. Matches stalebot.REPROPOSE_S: at a
# sub-hourly sweep, a fresh debt is delivered within the hour, and an old one
# re-surfaces every three days rather than every pass.
RENAG_S = 3 * 86400
MAX_LINES = 12          # digest cap; the remainder is COUNTED, never dropped
_STATE = "owed_push.json"

_USAGE = """usage: helm owed-push [--dry-run] [--quiet] [--json] [--ensure-timer]
  DELIVER the unanswered-FIX burn-down to the seats that owe it, as one DM per
  owing seat. `helm owed` computes exactly this and waits to be typed; this
  pushes it. Rows nobody owns ride one digest to the integrator.
    --dry-run       route and render the digests; post nothing, write no state
    --quiet         sweep and record state without posting
    --json          the full machine report
    --ensure-timer  install/refresh the hourly systemd user timer and exit
"""


# ---------------------------------------------------------------------------
# routing — obligation's items, grouped by the seat that owes them
# ---------------------------------------------------------------------------

def route(items):
    """{seat: [item]} — every debt addressed to the seat that owes it.

    DEDUPED BY WORK IDENTITY FIRST, through obligation.unanswered_lanes, which
    keys on chain_root and never on the lane LABEL. Skipping that step would
    bill one author six times for one lane: measured on the live ledger,
    `resolve-matches-session-not-just-env` contributed SIX rows and
    `gate-mints-its-own-evidence` SEVEN. A digest that triple-counts its
    worst-maintained chains is a digest nobody believes twice, and the person
    reading it is deciding what to work on.

    An item with no `owed_seat` — a fifth of the live rows, almost all of them
    undeclared-verdict rows — rides the INTEGRATOR'S digest rather than being
    dropped. A debt whose owner cannot be named is exactly the debt that
    otherwise goes forever unread.

    THE INTEGRATOR IS RESOLVED, NOT SPELLED. A module-level
    `INTEGRATOR = "<a seat name>"` keeps addressing that name after the roster
    stops carrying it, and nothing fails loudly: the constant is truthy, the
    digest is keyed, the DM is written, and the sweep reports a delivery it
    never made. seats_integrator is the one door that reads the name at the
    moment of use, and it announces the substitution when it cannot prove a
    seat live.

    RESOLVED LAZILY AND ONCE. The roster is not cached, so asking per item
    would read it once per debt, and asking eagerly would read it on every
    sweep whose items all name their owner. It is read the first time a row
    needs it and not at all otherwise."""
    out = {}
    unowned = None
    for i in obligation.unanswered_lanes(items):
        seat = str(i.get("owed_seat") or "").strip()
        if not seat:
            if unowned is None:
                unowned = seats_integrator.integrator_seat_or_default()
            seat = unowned
        out.setdefault(seat, []).append(i)
    return out


def _latch_key(item):
    """What must CHANGE before this debt is worth re-raising.

    The row id alone is too weak. A row that is re-tipped, or whose verdict
    kind changes from undeclared to a declared FIX, is NEWS on the same id —
    latching on the id alone would swallow it for RENAG_S. So the key carries
    the facts whose change makes the ask different: the kind and the reviewed
    tip the debt is bound to."""
    return "%s|%s|%s" % (str(item.get("kind") or ""),
                         str(item.get("reviewed_tip") or ""),
                         str(item.get("owed_since") or ""))


def _fmt_age(stamp):
    """A debt's age, rendered for a human -> "12d17h" / "3h04m" / "UNKNOWN".

    obligation._age_s answers in SECONDS or None, and both need translating
    before they reach a DM. The first draft of this digest printed the integer
    raw ("owed 537614"), which is the number-nobody-can-read failure; None is
    worse, because "owed None" reads like a bug in the debt rather than an
    unparseable stamp. UNPARSEABLE STAYS UNKNOWN, never 0 — obligation's own
    rule, for the reason its docstring gives: a zero sorts a debt of unknown
    age to the freshest end of the list, which is the one place nobody looks."""
    s = obligation._age_s(stamp)
    if s is None:
        return "UNKNOWN"
    d, rem = divmod(int(s), 86400)
    if d:
        return "%dd%dh" % (d, rem // 3600)
    return "%dh%02dm" % (rem // 3600, (rem % 3600) // 60)


def digest_text(seat, its, total=None):
    """One seat's DM.

    THE REMEDY TEXT IS obligation's OWN `what` STRING, VERBATIM. It already
    names --supersedes over --new-work (the flag whose absence manufactured
    124 unclosable land loops), and it already states the fact this whole
    mechanism exists to deliver: curing alone creates no review. Re-composing
    it here would fork the one sentence that has to stay right."""
    n = total if total is not None else len(its)
    head = ("@%s [%s] %d lane(s) owe YOUR next move — a FIX verdict on each is "
            "UNANSWERED. Curing alone creates no review: the re-dispatch is "
            "the step nothing else will tell you about." % (seat, BOT, n))
    lines = [head]
    for k, i in enumerate(its[:MAX_LINES], 1):
        lines.append("%d. %s (%s, owed %s) — %s"
                     % (k, i.get("lane") or "(no lane)", str(i.get("row"))[:12],
                        _fmt_age(i.get("owed_since")), i.get("what")))
    # ONCE PER DIGEST, NOT PER LINE: the fallback ladder is the same for every
    # row, and each `what` above already names that it exists (task/2948).
    from . import dispatches
    lines.append("No reviewer you can reach? " + dispatches.review_fallback_text())
    if n > MAX_LINES:
        lines.append("… +%d more — `helm owed --seat %s --rows` lists every "
                     "one." % (n - MAX_LINES, seat))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# state
# ---------------------------------------------------------------------------

def _state_path():
    return os.path.join(home.helm_home(), home.GLOBAL, ".state", _STATE)


def read_state(path=None):
    return pk.read_json(path or _state_path(), {}) or {}


def _post(text, seat):
    """True when the DM reached the seat's lane.

    THROUGH seats.dm, NOT chat.post(dm=). chat.post is the raw write seam that
    "direct callers bypass seats.dm" reach; seats.dm is the door that RESOLVES
    the recipient first and hands back a REASON when it cannot. That matters
    here more than anywhere: `owed_seat` is a raw `sender` string off an
    ancient ledger row, and a seat name that no longer resolves must read as
    an undelivered digest — which never latches, and therefore re-surfaces —
    rather than as a silent write into a lane nobody reads.

    A {dm} row wakes its EXACT-token recipient in any room and is non-ambient
    by construction (seats_identity.deliverable), which is why this rides chat
    rather than a new rail. A down chat node is an UNDELIVERED digest, never a
    crash."""
    try:
        from . import seats
        row, err = seats.dm(seat, text, who=BOT)
        if err or not row:
            print("helm owed-push: DM to @%s undelivered: %s"
                  % (seat, err or "no row written"), file=sys.stderr)
            return False
        return True
    except Exception as e:                # noqa: BLE001 — undelivered, not fatal
        print("helm owed-push: DM to @%s failed: %s" % (seat, e),
              file=sys.stderr)
        return False


def sweep(now=None, post=True, quiet=False, state_path=None, items=None,
          unavailable=None):
    """One pass: read obligation -> route -> latch -> DM -> record.

    AN UNREADABLE LEDGER IS UNKNOWN, NEVER EMPTY, and this reader must be even
    stricter about that than the screens are: publishing "nobody owes
    anything" from the one moment we cannot see would be a silence that reads
    exactly like a clean burn-down. It reports the failure and delivers
    nothing.

    A DIGEST WHOSE POST FAILS DOES NOT LATCH ITS ROWS (repo-watch's cursor
    lesson, inherited through stalebot): a watcher that fails silent while its
    cursor marches on has dropped the window, and here the window is somebody's
    unanswered debt."""
    now = time.time() if now is None else now
    if items is None and unavailable is None:
        items, _forks, unavailable = obligation.unanswered_fixes()
    if unavailable:
        return {"unavailable": str(unavailable), "swept": 0, "digests": {},
                "posted": [], "failed": [], "latched": 0, "items": []}

    by_seat = route(items or [])
    p = state_path or _state_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    posted, failed, latched = [], [], 0
    digests, due_by_seat = {}, {}
    with open(p + ".lock", "a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        st = read_state(p)
        rows_st = st.get("rows") or {}
        live_keys = set()
        for seat, its in by_seat.items():
            due = []
            for i in its:
                key = str(i.get("row") or "")
                live_keys.add(key)
                prev = rows_st.get(key)
                if prev and prev.get("latch") == _latch_key(i) \
                        and now - float(prev.get("sent_at") or 0) < RENAG_S:
                    latched += 1
                    continue
                due.append(i)
            if due:
                due_by_seat[seat] = due
                digests[seat] = digest_text(seat, due, len(due))
        if post and not quiet:
            for seat, text in digests.items():
                ok = _post(text, seat)
                (posted if ok else failed).append(seat)
                if ok:
                    for i in due_by_seat[seat]:
                        rows_st[str(i.get("row") or "")] = {
                            "sent_at": now, "latch": _latch_key(i)}
        if post:
            # RE-ARM: a debt that left the owed set (re-dispatched, cancelled,
            # discharged) drops its latch, so if the SAME row ever re-enters
            # the set it is delivered again rather than silently swallowed by
            # a stale entry.
            for k in [k for k in rows_st if k not in live_keys]:
                rows_st.pop(k, None)
            pk.write_json(p, {"last_run": now, "rows": rows_st,
                              "swept": len(items or []),
                              "seats": len(by_seat),
                              "delivered": len(posted),
                              "failed": len(failed), "latched": latched})
    return {"swept": len(items or []), "seats": len(by_seat),
            "digests": digests, "posted": posted, "failed": failed,
            "latched": latched, "unavailable": None, "state_path": p,
            "items": [{"row": i.get("row"), "lane": i.get("lane"),
                       "seat": s, "kind": i.get("kind")}
                      for s, its in by_seat.items() for i in its]}


# ---------------------------------------------------------------------------
# cadence — hourly, because the debt this delivers is HOURS old when it matters
# ---------------------------------------------------------------------------

DEFAULT_INTERVAL_S = 3600
# stalebot sweeps DAILY because aged means days. This one must not: measured
# on the live board, 18 of 130 debts were younger than 24 hours, and the
# incident that prompted the whole mechanism was a cure committed FIFTEEN
# MINUTES after its verdict whose row then sat 22 hours. A daily cadence would
# have delivered that one a day late. The re-nag latch (RENAG_S) is what keeps
# an hourly sweep from becoming hourly noise.

_UNIT_SERVICE = """[Unit]
Description=helm owed-push — deliver unanswered FIX debt to the seats that owe it

[Service]
Type=oneshot
WorkingDirectory=%(cwd)s
Environment=HELM_CHAT_NAME=owed-bot
UnsetEnvironment=CLAUDE_CODE_SESSION_ID CLAUDE_SESSION_ID CODEX_SESSION_ID
ExecStart=%(helm)s owed-push
"""
# Environment + UnsetEnvironment together are repo-watch's measured env
# hygiene, inherited verbatim from stalebot: an inherited session id makes the
# declared name a DISPUTE against the session's rostered seat.

_UNIT_TIMER = """[Unit]
Description=helm owed-push cadence (external, no demons)

[Timer]
OnBootSec=600
OnUnitActiveSec=%(interval)ss

[Install]
WantedBy=timers.target
"""


def _timer_units(interval=DEFAULT_INTERVAL_S):
    # A persistent unit must never capture a DISPOSABLE WORKTREE's path: the
    # binary is the stable install, the cwd is the lane folded back to the
    # shared checkout (stalebot._timer_units' law, same reason).
    from . import work
    helm_bin = os.path.join(os.path.expanduser("~"), ".local", "bin", "helm")
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cwd = work.find_root(here) or here
    udir = os.path.join(os.path.expanduser("~"), ".config", "systemd", "user")
    return (os.path.join(udir, "helm-owed-push.service"),
            _UNIT_SERVICE % {"helm": helm_bin, "cwd": cwd},
            os.path.join(udir, "helm-owed-push.timer"),
            _UNIT_TIMER % {"interval": interval})


def ensure_timer(interval=DEFAULT_INTERVAL_S):
    """Install/refresh and enable the cadence -> (ok, detail). Idempotent."""
    import shutil
    import subprocess
    if interval < 1:
        return False, "interval must be at least 1 second"
    systemctl = shutil.which("systemctl")
    if not systemctl:
        return False, ("systemctl unavailable; run `helm owed-push` from "
                       "another scheduler")
    spath, service, tpath, timer = _timer_units(interval)
    try:
        os.makedirs(os.path.dirname(spath), exist_ok=True)
        pk.atomic_write(spath, service)
        pk.atomic_write(tpath, timer)
    except OSError as e:
        return False, "unit write failed: %s" % e
    for cmd in ([systemctl, "--user", "daemon-reload"],
                [systemctl, "--user", "enable", "--now",
                 "helm-owed-push.timer"]):
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            return False, "%s failed: %s" % (
                " ".join(cmd), (r.stderr or r.stdout or "").strip()[:200])
    return True, "owed-push cadence enabled every %ds (%s)" % (interval, tpath)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def cmd_owed_push(args):
    args = list(args or [])
    if args and args[0] in ("-h", "--help"):
        print(_USAGE)
        return 0
    from . import cli
    rc = cli.guard_tail("helm owed-push", args,
                        flags=("--dry-run", "--quiet", "--json",
                               "--ensure-timer"), usage=_USAGE)
    if rc is not None:
        return rc
    if "--ensure-timer" in args:
        ok, detail = ensure_timer()
        print("helm owed-push: %s" % detail,
              file=sys.stdout if ok else sys.stderr)
        return 0 if ok else 1
    rep = sweep(post="--dry-run" not in args, quiet="--quiet" in args)
    if rep.get("unavailable"):
        # UNKNOWN, never a clean burn-down. rc=1 so a scheduler records that
        # this pass answered nothing rather than that nobody owed anything.
        print("helm owed-push: dispatch ledger UNAVAILABLE — debts UNKNOWN, "
              "nothing delivered (%s)" % rep["unavailable"], file=sys.stderr)
        return 1
    if "--json" in args:
        print(json.dumps(rep, ensure_ascii=False, indent=1))
        return 0
    for seat in sorted(rep["digests"]):
        print("@%-20s %d lane(s)" % (seat, len(rep["digests"][seat].splitlines()) - 1))
    if "--dry-run" in args:
        for seat, text in rep["digests"].items():
            print("\n--- would DM (@%s) ---\n%s" % (seat, text))
    print("helm owed-push: %d debt(s) over %d seat(s), %d digest(s) %s, "
          "%d latched"
          % (rep["swept"], rep["seats"], len(rep["digests"]),
             "would deliver" if "--dry-run" in args else
             ("delivered" if rep["posted"] or not rep["digests"] else "FAILED"),
             rep["latched"]))
    for seat in rep["failed"]:
        print("helm owed-push: DM to @%s FAILED — its debts stay unlatched "
              "and re-deliver next sweep" % seat, file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(cmd_owed_push(sys.argv[1:]))
