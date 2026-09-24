#!/usr/bin/env python3
"""helm seat rehome — move ONE live seat onto a credhome in ONE verb.

WHY IT EXISTS (owner, task/2573): this was a hand procedure the integrator ran
twice in a week to spend the remaining opus quota of one account — read the
pane, exit the session, relaunch with
`helm launch --seat S --home H -- --model opus --resume SID` — and the hand
procedure has a hole the owner fell into: a home whose refresh chain has
EXPIRED is not synced from Orca, and `helm launch` says so and launches anyway
("launching on the stale token", cred.launch_sync). The seat comes back on a
dead token, and the only symptom is "Not logged in" inside a pane whose
scrollback the operator has just replaced. A one-off request that becomes
policy has to become a verb, so any helm agent can run it and no agent can
run it onto a stale home.

WHAT IT REFUSES, WHICH IS THE POINT. Nothing here is a new capability: every
leg is an existing door. The verb's own contribution is the ORDER and the
GATE — the home is proven before the live pane is touched at all, so a refusal
costs the seat nothing. Dry-run by default; the dry run IS the plan, and it
prints the exact launch line the apply would use.

THE DOORS, each named where it is called:
  * the session id            orcaadopt.roster_identity (the ADDRESSING half:
                              the CURRENT sid, never a history one — a stale
                              sid found alive is somebody else's pane)
  * the pane                  orcaadopt.resolve + orcaadopt.authorized_handle
                              (a handle BOUND to a stamped live process, the
                              one primitive every adopted send routes through)
  * the home                  cred.sync (dry-run) + cred.sync_disabled +
                              cred._token_facts, and cred._cure for the remedy
                              prose. The gate is ONE predicate, asked at the
                              plan AND again at the act.
  * the exit                  orcaadopt.send_to_pane — ONE pane-input wake,
                              typed and submitted and verified by the adapter's
                              own turn verb. NEVER a signal: a kill loses the
                              transcript flush this whole verb exists to keep.
                              It rides in as an `operation` so the pane the
                              exit is spent on is the pane the relaunch types
                              into: ONE binding, not two resolutions.
  * the exit proof            beacons.pid_alive against the SAME birth stamp
                              the handle was authorized on
  * the relaunch              seat_resume_all.pane_launch_line into the pane
                              the exit just emptied (the reuse-a-pane idiom
                              `seat resume` uses for its DEAD-PANE leg),
                              carrying this attempt's HELM_REHOME_ATTEMPT
                              assignment on the launch invocation itself
  * the verification          orcaadopt.resolve (pane) + beacons.proc_env for
                              the home AND the attempt marker, beacons.proc_argv
                              for the session and model the new process actually
                              runs on;
                              seats.roster_checked (register);
                              beacons.entries (inbox beacon). Each surface is
                              about THIS rehome, never about the seat in
                              general.
  * the ledger                eventledger.append, one row per rehome

THE STORE RULE THIS OBEYS (premise
orca-is-the-sledgehammer-credhomes-are-the-scalpel-sync-from-orca): a credhome
only has an edge when its token is synced FROM Orca's live file, and the
default ~/.claude is Orca's own to rewrite. So a rehome targets a NAMED
credhome and nothing else, the Orca -> home direction is the only one, and a
home that would have to take Orca's live chain while Orca still refreshes it
is refused by cred.sync's own guards rather than by a second opinion here.
"""
import os
import shlex
import sys
import time
import uuid

from . import beacons, cred, home, homes, orcaadopt, pk, seats

LEDGER = "seat-rehomes.jsonl"
EVENT = "seat-rehome"
SCHEMA_V = 1

# THE PER-ATTEMPT CORRELATION MARKER. One fresh value per apply, carried on the
# launch line so the child claude inherits it, and read back out of the new
# process's own environ. It is CORRELATION, NOT AUTHENTICATION: it makes THIS
# attempt distinguishable from every other process that could stand in the same
# pane, and it claims nothing against an adversary who copies it. The value is
# not secret and is printed in full wherever it is reported.
ATTEMPT_ENV = "HELM_REHOME_ATTEMPT"

# How long the exit leg waits for the seat's process to be PROVEN gone. A
# claude session flushes its transcript on /exit, and the relaunch resumes that
# same transcript, so the wait is what keeps the resumed session from being a
# copy of the conversation as it stood before the last turn.
EXIT_WAIT_S = 45.0
EXIT_POLL_S = 0.5
# How long the verification leg waits for each surface to come back. A launch
# execs claude, which joins the roster at SessionStart and arms its inbox
# beacon on its first wait — the beacon is therefore the slowest of the three
# and an UNPROVEN beacon is reported, never folded into a failure.
VERIFY_WAIT_S = 90.0
VERIFY_POLL_S = 1.0


def ledger_path():
    return os.path.join(home.global_dir(), LEDGER)


def record(event):
    """Append ONE row describing the whole rehome. -> (ok, reason).

    ONE ROW, the same shape `seat reassign` writes and for the same reason: a
    reader must never see half a rehome. `id` is not optional — eventledger's
    checked reader drops a row without one, so an append that reports success
    and cannot be read back is the worst shape a ledger has.
    """
    from . import eventledger
    row = dict(event)
    row.setdefault("v", SCHEMA_V)
    row.setdefault("event", EVENT)
    row.setdefault("ts", pk.now_ts())
    row.setdefault("id", os.urandom(16).hex())
    if not eventledger.append(ledger_path(), row):
        return False, ("the rehome ledger refused the append (lock unavailable "
                       "or the row was rejected) — %s" % ledger_path())
    return True, None


def events():
    from . import eventledger
    return eventledger.checked_events(ledger_path())


class Refusal(object):
    """A named check plus the remedy for it. Never a bare string: an operator
    reading a refusal has to be able to act on it without reading this file."""

    __slots__ = ("check", "reason", "remedy")

    def __init__(self, check, reason, remedy=None):
        self.check, self.reason, self.remedy = check, reason, remedy

    def line(self):
        return "helm seat rehome: REFUSED at %s — %s%s" % (
            self.check, self.reason,
            ("\n  remedy: " + self.remedy) if self.remedy else "")


def _utc(ms):
    if not isinstance(ms, (int, float)) or isinstance(ms, bool):
        return "unknown"
    return time.strftime("%Y-%m-%dT%H:%MZ", time.gmtime(ms / 1000))


def _home_token_facts(real):
    """(facts, error) for the home's OWN credentials — secret-free, through
    cred's own readers. Absence is an error here, deliberately: a home with no
    token cannot be launched onto, and calling that "unknown" would let the
    stale-token launch through the one gate built to stop it."""
    path = os.path.join(real, cred.AUTH_JSON)
    try:
        blob, _mode = cred._read_regular(path)
    except FileNotFoundError:
        return None, "the home holds no %s" % cred.AUTH_JSON
    except OSError as e:
        return None, "the home's %s is unreadable (%s)" % (
            cred.AUTH_JSON, e.__class__.__name__)
    facts = cred._token_facts(blob)
    if facts is None:
        return None, "the home's %s carries no claudeAiOauth block" % cred.AUTH_JSON
    return facts, None


def check_home(home_name, now_ms=None):
    """(note, refusal) — may a session be started on this credhome?

    THE GATE THE HAND PROCEDURE DID NOT HAVE. Three questions in the order
    that makes the answers cheap:

      1. Is it a NAMED credhome at all? The default ~/.claude is the account
         Orca rewrites fleet-wide (the sledgehammer); pinning a seat there is
         not a rehome, it is the absence of one.
      2. Does Orca's live copy reach it? `cred.sync` DRY-RUN answers with the
         same decision the launch will take: would-sync (the launch syncs it),
         skip (STALE and NOT syncable — the launch would start on the stale
         token, which is the owner's measured defect), refused (identity
         disagrees, or the chain is unproven — the launch refuses too).
      3. If Orca cannot reach it, is the home's OWN chain alive? A home on its
         own login is legitimate; a home whose refreshTokenExpiresAt has PASSED
         is a dead token wearing a live identity, and nothing downstream says
         so until the pane prints "Not logged in"; and a home with NO
         refreshTokenExpiresAt at all is a chain nothing proves, which cred's
         own measure already calls UNPROVEN rather than live.

    A WOULD-SYNC IS ONLY SAFE IF THE LAUNCH WILL ACTUALLY SYNC. `cred.sync`
    answers what a sync WOULD do; `cred.launch_sync` — the door `helm launch`
    passes — does nothing at all when `HELM_CREDHOME_ORCA_SYNC` is disabled and
    launches on the home's own token anyway. A preflight that admits a home
    only a sync makes usable, in an environment where the sync is switched off,
    hands the seat the exact stale-token launch this verb exists to refuse. So
    the switch is a question this gate asks, through `cred.sync_disabled` — the
    same reader `launch_sync` uses — and not a second opinion about it.

    THAT SWITCH IS READ IN HELM'S OWN ENVIRONMENT, which is the only one this
    process can measure; the launch line runs in the seat's pane, and a pane
    whose environment differs is beyond what any preflight here can see. The
    remedy says so rather than letting the refusal imply more than was read.

    Nothing here writes. `cred.sync(apply=False)` is a measurement.
    """
    now_ms = time.time() * 1000 if now_ms is None else now_ms
    real, err = cred._resolve_home(home_name)
    if err:
        return None, Refusal("home resolution", err,
                             "`helm cred list` names every credhome helm can see")
    shown = cred._display_path(real)
    if not cred.is_credhome(real):
        return None, Refusal(
            "home is a credhome",
            "%s is not a named credhome — a credhome is a directory directly "
            "under %s. The default ~/.claude is Orca's own to rewrite on an "
            "account switch, so a seat there follows the whole fleet instead "
            "of pinning one account"
            % (shown, cred._display_path(homes.ROOTS["claude"])),
            "pick a named credhome from `helm cred list`, or leave the seat on "
            "the Orca-synced default and switch the account in Orca instead")
    plan = cred.sync(real)
    verdict, action, reason = plan["verdict"], plan["action"], plan["reason"]
    account = plan["account"] or "an unreadable account"
    if action == "would-sync":
        if cred.sync_disabled():
            return None, Refusal(
                "the launch will perform the sync this home needs",
                "home %s is %s for %s and is launchable ONLY because a launch "
                "copies Orca's live token into it first (%s) — and %s is "
                "switched OFF, so `cred.launch_sync` performs no sync at all "
                "and starts claude on this home's stale token. That is the "
                "exact failure this verb exists to refuse, reached the long "
                "way round"
                % (shown, verdict, account, reason, cred.SYNC_ENV),
                "unset %s (or set it to 1) and re-run. helm reads that switch "
                "in its OWN environment, which is the only one this process "
                "can measure — a pane that exports it differently is beyond "
                "what any preflight here can see" % cred.SYNC_ENV)
        return ("home %s is %s for %s and `helm launch --home` syncs it from "
                "Orca's live copy before exec (%s)"
                % (shown, verdict, account, reason)), None
    if action == "refused":
        return None, Refusal(
            "home token is live or syncable",
            "home %s reads %s for %s: %s — `helm launch` refuses to start a "
            "session on it, so the seat would be exited and never come back"
            % (shown, verdict, account, reason),
            cred._cure(real))
    if action == "skip":
        return None, Refusal(
            "home token is live or syncable",
            "home %s is %s for %s and is NOT syncable from Orca: %s. "
            "`helm launch` would start the session on the stale token and say "
            "so in one line nobody reads"
            % (shown, verdict, account, plan["blocked"] or reason),
            cred._cure(real))
    # action == "none": FRESH, OWN-CHAIN, NO-ORCA-COPY or UNKNOWN. Orca is not
    # going to write this home, so the home's OWN chain is what the session
    # will run on and it has to be alive.
    facts, ferr = _home_token_facts(real)
    if ferr:
        return None, Refusal(
            "home token is live or syncable",
            "home %s reads %s for %s but %s" % (shown, verdict, account, ferr),
            cred._cure(real))
    life = facts["refresh_expires_at"]
    if life is None:
        return None, Refusal(
            "home refresh chain is live",
            "home %s reads %s for %s and its %s carries NO "
            "refreshTokenExpiresAt, so NOTHING proves the chain this session "
            "would run on can still refresh — cred's own measure calls a "
            "missing lifetime UNPROVEN rather than live (%s). Unproven is not "
            "dead; it is also not the proof this verb owes, because the live "
            "session is exited BEFORE the target is ever used"
            % (shown, verdict, account, cred.AUTH_JSON, reason),
            "%s; or leave this seat on the Orca-synced default home, whose "
            "chain Orca keeps live" % cred._cure(real))
    if life <= now_ms:
        return None, Refusal(
            "home refresh chain is live",
            "home %s carries its OWN login chain and that chain EXPIRED at %s "
            "(%s for %s: %s). Orca has no live copy to sync from, so a launch "
            "here starts claude on a dead token and the pane prints Not logged "
            "in after the old session is already gone"
            % (shown, _utc(life), verdict, account, reason),
            "%s; or leave this seat on the Orca-synced default home, whose "
            "account the owner switches in Orca" % cred._cure(real))
    return ("home %s is %s for %s on its own login chain (access token expires "
            "%s, refresh lifetime to %s): %s"
            % (shown, verdict, account, _utc(facts["expires_at"]),
               _utc(life), reason)), None


def launch_command(seat, home_name, model=None, session=None):
    """The exact `helm launch` line this rehome runs — the owner's own hand
    procedure, quoted. `--model` and `--resume` ride AFTER `--` because they
    are claude's flags, and launch passes everything past `--` through
    verbatim (launch.parse_args)."""
    line = ["helm", "launch", "--seat", seat, "--home", home_name, "--"]
    if model:
        line += ["--model", model]
    if session:
        line += ["--resume", session]
    return " ".join(shlex.quote(word) for word in line)


def new_attempt():
    """A fresh correlation value for ONE apply. `uuid4` and nothing else.

    WHAT IT IS FOR: TIME ORDERING CANNOT NAME A LAUNCH. Another claude can
    start in this seat's pane after the exit and before this verb's launch
    line runs, on the same home, resuming the same session, under the same
    model, and younger than the process the exit removed. Every property the
    verification reads is then true of a process this rehome did not start,
    and a timestamp or a snapshot only moves that counterexample rather than
    removing it. So the launch CARRIES a value nothing else in the world has,
    and the verification asks the candidate for it.

    NOT A SECRET AND NOT AN AUTHORITY. It proves nothing against anybody who
    copies it; it is not a capability, it grants nothing, and it is printed in
    full in the plan, the pane report and the ledger row. What it buys is the
    one thing time cannot: this-attempt attribution.
    """
    return str(uuid.uuid4())


def attempt_launch_line(command, cwd, attempt):
    """The EXACT line this apply types into the pane, composed through the one
    shipped composer (`seat_resume_all.pane_launch_line`) so a reused pane's
    `cd` has a single description in this tree.

    WHERE THE ASSIGNMENT BINDS IS THE WHOLE POINT. With a cwd the line is
    `cd <dir> && HELM_REHOME_ATTEMPT=<v> <launch>`: the assignment prefixes the
    LAUNCH INVOCATION, so it reaches that command's environment and its
    children, and only after the `cd` has succeeded. It is deliberately NOT an
    `export` and deliberately not a prefix on the `cd` builtin — an export
    would outlive the launch in that pane's shell and a later sibling launch,
    typed by a person or by another verb, would inherit a marker that says it
    was started by this rehome.
    """
    from . import seat_resume_all
    return seat_resume_all.pane_launch_line(
        "%s=%s %s" % (ATTEMPT_ENV, shlex.quote(attempt), command), cwd)


def plan(seat, home_name, model=None, adapter=None, now_ms=None):
    """(plan, refusal) — everything the apply would do, decided before
    anything is touched.

    ORDER IS THE SAFETY. The home is checked LAST of the three resolutions but
    BEFORE any actuation, so a refusal leaves a live seat exactly as it was;
    and the seat/pane resolutions run first so a refusal about the home is
    never printed for a seat helm could not have rehomed anyway.
    """
    from . import harness
    ad = adapter if adapter is not None else harness.detect()
    if ad is None:
        return None, Refusal(
            "metaharness", "no metaharness is detectable on this host, so no "
            "pane can be read or written",
            "run this where orca (or herdr) is reachable")
    session, _sids, roster_failed = orcaadopt.roster_identity(seat)
    if roster_failed:
        return None, Refusal(
            "seat session", "helm's chat roster could not be read, so the "
            "seat's CURRENT session id is UNKNOWN — and resuming the wrong "
            "session id attaches another conversation",
            "read the roster through `helm fleet`, repair it, and re-run")
    if not session:
        return None, Refusal(
            "seat session",
            "no CURRENT session id is recorded for %s, so there is nothing to "
            "resume on the new home" % seat,
            "let the seat take one turn so it joins the roster, or spawn it")
    info = orcaadopt.resolve(seat, adapter=ad)
    if info is None:
        return None, Refusal("seat", "helm has no record of seat %s" % seat,
                             "`helm fleet` names the seats that exist")
    if info.get("session_refused"):
        return None, Refusal(
            "pane identity", info["session_refused"],
            "resolve the seat's session evidence before moving it")
    # THE HANDLE IS BOUND TO A STAMPED PROCESS, never to a name. `pane_pids` is
    # the candidate set `resolve` actually considered, published by that door
    # so this one cannot re-derive a different one.
    pids = list(info.get("pane_pids") or ())
    handle, proof = orcaadopt.authorized_handle(pids, ad)
    if handle is None:
        return None, Refusal(
            "pane proof",
            "the pane for %s cannot be proven: %s" % (seat, proof),
            "read `helm seat where %s` — a seat whose pane helm cannot bind is "
            "one it must not type into" % seat)
    note, refusal = check_home(home_name, now_ms=now_ms)
    if refusal:
        return None, refusal
    # THE HOME AS A PATH, resolved once here so verification can ask the new
    # process which home it actually runs on (`CLAUDE_CONFIG_DIR` in its own
    # environ) instead of comparing a name to a name.
    home_real, _herr = cred._resolve_home(home_name)
    cwd = None
    for pid in pids:
        cwd = beacons.proc_cwd(int(pid))
        if cwd:
            break
    command = launch_command(seat, home_name, model=model, session=session)
    from . import seat_resume_all
    return {"seat": seat, "home": home_name, "home_real": home_real,
            "model": model,
            "session": session, "handle": handle, "adapter": ad,
            "pids": pids, "cwd": cwd, "home_note": note,
            "pane_proof": proof,
            "unidentified": info.get("unidentified"),
            "command": command,
            "pane_line": seat_resume_all.pane_launch_line(command, cwd)}, None


def print_plan(p, apply=False, out=None):
    out = out or sys.stdout
    print("helm seat rehome %s --home %s%s%s"
          % (p["seat"], p["home"],
             (" --model " + p["model"]) if p["model"] else "",
             " --apply" if apply else "  (DRY RUN — nothing is touched)"),
          file=out)
    print("  session   %s (the seat's CURRENT roster session; the relaunch "
          "resumes it)" % p["session"], file=out)
    print("  pane      %s — %s" % (p["handle"], p["pane_proof"]), file=out)
    print("  cwd       %s" % (p["cwd"] or "unreadable — the launch line "
                              "carries no cd"), file=out)
    print("  home      %s" % p["home_note"], file=out)
    if p["unidentified"]:
        print("  census    %s" % p["unidentified"], file=out)
    print("  plan      1. /exit into the pane (ONE wake, submitted and "
          "verified; never a signal)", file=out)
    print("            2. wait up to %gs for pid%s %s to be PROVEN gone"
          % (EXIT_WAIT_S, "s"[:len(p["pids"]) != 1],
             ", ".join(str(int(x)) for x in p["pids"])), file=out)
    print("            3. type the launch line into that same pane", file=out)
    print("            4. verify pane + roster register + inbox beacon", file=out)
    print("  launch    %s" % p["pane_line"], file=out)
    print("  marker    --apply mints one %s=<uuid4> onto that line and proves "
          "the process it started carries it — a claude that comes up in this "
          "pane by any other route matches everything else" % ATTEMPT_ENV,
          file=out)


def _gone(pids):
    """(all_gone, unproven) for the authorized processes.

    `beacons.pid_alive` is tri-state and only False is evidence of death, so a
    pid helm could not read is UNPROVEN and the caller keeps waiting rather
    than relaunching over a session that may still be flushing."""
    unproven = []
    for pid in pids:
        state = beacons.pid_alive(int(pid), getattr(pid, "start", None))
        if state is True:
            return False, []
        if state is None:
            unproven.append(int(pid))
    return (not unproven), unproven


def _wait_gone(pids, deadline_s=EXIT_WAIT_S, poll_s=EXIT_POLL_S, sleep=None):
    sleep = sleep or time.sleep
    end = time.time() + deadline_s
    unproven = []
    while True:
        done, unproven = _gone(pids)
        if done:
            return True, None
        if time.time() >= end:
            break
        sleep(poll_s)
    if unproven:
        return False, ("pid %s could not be read, so helm cannot say the old "
                       "session is gone" % ", ".join(str(x) for x in unproven))
    return False, "the seat's process was still alive after %gs" % deadline_s


def _standing_dialog(ad, handle):
    """The dialog text a pane that refused to exit is sitting at, or None.

    READ-ONLY, AND DELIBERATELY SO. helm has no authorized door for an
    arbitrary confirm dialog: `harness.choose_in_modal` derives its keystroke
    from the VENDOR-ESCAPE option family alone, and typing a digit through the
    composer door (`submit`) refuses on a pane that has no composer. So when a
    dialog stands between /exit and the exit, this verb NAMES it and stops —
    the seat is untouched, nothing was killed, and a human answers one prompt.
    """
    from . import seat
    try:
        tail = ad.read(handle)
    except Exception as e:                  # noqa: BLE001 — a probe error is a reason
        return "the pane could not be read back (%s: %s)" % (
            e.__class__.__name__, e)
    options = seat._prompt_options(tail)
    if not options:
        return None
    return "the pane is showing a dialog: %s" % "; ".join(
        "%s. %s" % (n, str(label).strip()[:60]) for n, label in options)


def _stamp(value):
    """A birth stamp as a NUMBER, or None. /proc field 22 is monotone in boot
    clock ticks, so a larger stamp is a younger process — and a stamp helm
    cannot read as a number orders against nothing, which is UNPROVEN and
    never "young enough"."""
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _flag(argv, name):
    """The value that follows `name` in an argv, or None."""
    argv = list(argv or ())
    for i, word in enumerate(argv[:-1]):
        if word == name:
            return argv[i + 1]
    return None


def _planned_process(p, info):
    """(ident, mismatch) — the pane holder this rehome's OWN launch produced.

    A SEAT NAME ANSWERS NONE OF THE FOUR QUESTIONS. The pane a rehome typed
    into still answers for the seat when the launch line never ran, the old
    process is still named by the seat it held, and every subagent a seat
    spawns inherits that name. So a candidate is the rehome's own new session
    only when all four hold:

      * it is a DIFFERENT PROCESS from the one the exit removed — identity,
        never the pid number, because a recycled pid is the same miss wearing
        the victim's number (`orcaadopt.ident_key`);
      * it was BORN AFTER that process. This is the strongest thing /proc
        supports: field 22 orders births against each other, not against a
        wall clock, so what is proven is "younger than the session it
        replaced", and that is what the detail says;
      * it RUNS ON THE PLANNED HOME, read from its own environ
        (`CLAUDE_CONFIG_DIR`) — the one reading that distinguishes the seat
        this verb moved from the seat that simply came back;
      * its argv carries the PLANNED session, and the planned model when one
        was asked for;
      * and it carries THIS APPLY'S `HELM_REHOME_ATTEMPT` in its own environ.

    THE LAST ONE IS THE ONLY ONE TIME CANNOT FAKE, and the first four are why
    it is needed. A second claude can be started in this pane after the exit —
    by a person, by a sibling verb, by a crash-loop supervisor — on the planned
    home, resuming the planned session, under the planned model, and born after
    the process the exit removed. It then satisfies every ordering test this
    function can write, and it is not the process this launch started: our
    launch line may have started nothing at all. So the apply mints one value
    per attempt, the launch line carries it into the child's environment, and
    a candidate that cannot show it is UNPROVEN with the marker named. It is
    correlation only: a process that copied the value would pass, which is a
    hazard nobody has and never the one this refuses.

    An unreadable environ is UNKNOWN and refuses here, never accepted: a
    process helm cannot ask about its home or its attempt marker is exactly the
    process that could have come up on the old one.
    """
    old = {orcaadopt.ident_key(x) for x in p["pids"]}
    stamps = [s for s in (_stamp(getattr(x, "start", None)) for x in p["pids"])
              if s is not None]
    floor = max(stamps) if stamps else None
    why = []
    for cand in info.get("pane_pids") or ():
        pid = int(cand)
        if orcaadopt.ident_key(cand) in old:
            why.append("pid %d (birth stamp %s) is the SAME process the exit "
                       "removed" % (pid, getattr(cand, "start", None)))
            continue
        stamp = _stamp(getattr(cand, "start", None))
        if stamp is None:
            why.append("pid %d carries no readable birth stamp, so helm "
                       "cannot say it is younger than the session it would "
                       "replace" % pid)
            continue
        if floor is not None and stamp <= floor:
            why.append("pid %d was born at stamp %d, at or before the exited "
                       "session's %d — it is not a process this launch "
                       "started" % (pid, stamp, floor))
            continue
        env = beacons.proc_env(pid)
        if env is None:
            why.append("pid %d's environment could not be read, so the home it "
                       "runs on and the %s it carries are both UNKNOWN"
                       % (pid, ATTEMPT_ENV))
            continue
        ran = env.get(homes.ENV_VAR["claude"])
        want = p.get("home_real")
        if not ran or not want or os.path.realpath(ran) != os.path.realpath(want):
            why.append("pid %d runs on home %s and this rehome planned %s"
                       % (pid, cred._display_path(ran) if ran else
                          "the default (no %s)" % homes.ENV_VAR["claude"],
                          p["home"]))
            continue
        argv = beacons.proc_argv(pid)
        if argv is None:
            why.append("pid %d's argv could not be read, so the session and "
                       "model it resumed are UNKNOWN" % pid)
            continue
        resumed = _flag(argv, "--resume")
        if resumed != p["session"]:
            why.append("pid %d resumed session %s and this rehome planned %s"
                       % (pid, resumed or "none", p["session"]))
            continue
        if p["model"] and _flag(argv, "--model") != p["model"]:
            why.append("pid %d runs model %s and this rehome planned %s"
                       % (pid, _flag(argv, "--model") or "the home default",
                          p["model"]))
            continue
        # ASKED LAST, ON THE SAME CANDIDATE AND THE SAME ENVIRON READ. Every
        # check above describes a process that could be somebody else's; this
        # one is the only one that says the launch line typed by THIS apply is
        # what produced it, so a process that fails it has otherwise matched
        # everything and the detail says exactly that.
        want = p.get("attempt")
        if not want:
            why.append("pid %d cannot be attributed to this rehome at all: no "
                       "%s value was minted for this attempt, so nothing "
                       "distinguishes the process this launch started from any "
                       "other claude in this pane" % (pid, ATTEMPT_ENV))
            continue
        mark = env.get(ATTEMPT_ENV)
        if not mark:
            why.append("pid %d matches the home, the session and the model but "
                       "carries no %s, and this rehome launched with %s=%s — a "
                       "claude that came up in this pane by any other route "
                       "matches everything else this check can read"
                       % (pid, ATTEMPT_ENV, ATTEMPT_ENV, want))
            continue
        if mark != want:
            why.append("pid %d carries %s=%s and this rehome launched with %s "
                       "— it is another attempt's process, not this one's"
                       % (pid, ATTEMPT_ENV, mark, want))
            continue
        return cand, None
    return None, ("; ".join(why) or "no process carries the seat's pane")


def verify(p, deadline_s=VERIFY_WAIT_S, poll_s=VERIFY_POLL_S, sleep=None,
           since=None, found=None):
    """[(surface, proven, detail)] for the three surfaces a rehomed seat owes.

    THREE QUESTIONS, NEVER ONE WORD. A pane can be back while the register is
    not, and the register can be back while the seat is still deaf — the
    beacon is the slowest because it is armed by the seat's own first wait.
    Each row is proven or UNPROVEN with its reason; nothing is folded.

    AND EACH QUESTION IS ABOUT THE REHOME THAT WAS REQUESTED, not about the
    seat in general. A surface that merely EXISTS proves the seat exists,
    which it did before this verb ran and would go on doing if the launch line
    never executed. So the pane must be held by the process
    `_planned_process` describes, and the beacon must have been ARMED SINCE
    the act (`since`, the wall clock read the moment before the exit) — a
    beacon that was already standing is the seat's previous incarnation and
    reads UNPROVEN with that said.

    THE REGISTER IS AN AGREEMENT CHECK AND SAYS SO. A rehome RESUMES the
    seat's current session id, so the roster row it lands on is the row that
    was already there; this surface can therefore only prove the roster still
    names this seat on the session the rehome planned, and reads UNPROVEN
    naming the mismatch when it moved. The causal weight sits on the other
    two, which is why they are the ones the detail lines describe.

    `found` is filled with the new process identity when the pane proves one,
    so the caller can put IN THE LEDGER what it actually proved rather than a
    bare boolean. The proven pane row publishes the attempt marker beside that
    identity: the marker is what makes the row a statement about THIS rehome
    rather than about whatever claude is standing in the pane, so a reader who
    cannot see it cannot audit the claim.
    """
    sleep = sleep or time.sleep
    since = time.time() if since is None else since
    end = time.time() + deadline_s
    rows, misses = {}, {}
    while True:
        if "pane" not in rows:
            info = orcaadopt.resolve(p["seat"], adapter=p["adapter"])
            if info and info.get("handle"):
                ident, mismatch = _planned_process(p, info)
                if ident is None:
                    misses["pane"] = ("pane %s answers for %s but is not "
                                      "holding the session this rehome "
                                      "planned: %s"
                                      % (info["handle"], p["seat"], mismatch))
                else:
                    if found is not None:
                        found["process"] = [int(ident),
                                            getattr(ident, "start", None)]
                        found["handle"] = info["handle"]
                        found["attempt"] = p.get("attempt")
                    rows["pane"] = (True, (
                        "pane %s holds pid %d (birth stamp %s), a process "
                        "younger than the one the exit removed, running on "
                        "home %s, resuming %s and carrying this attempt's "
                        "%s=%s"
                        % (info["handle"], int(ident),
                           getattr(ident, "start", None), p["home"],
                           p["session"], ATTEMPT_ENV, p.get("attempt"))))
            else:
                misses["pane"] = "no pane answers for the seat"
        if "register" not in rows:
            roster, failed = seats.roster_checked()
            from .seats_common import canonical_seat
            key, ambiguous = (None, False) if failed else \
                canonical_seat(p["seat"], roster)
            row = roster.get(key) if key and not ambiguous else None
            if not isinstance(row, dict):
                misses["register"] = ("the chat roster could not be read"
                                      if failed else
                                      "the seat is not in the chat roster")
            elif str(row.get("session") or "") != p["session"]:
                misses["register"] = (
                    "the roster names %s on session %s and this rehome "
                    "planned %s" % (p["seat"], row.get("session") or "none",
                                    p["session"]))
            else:
                rows["register"] = (
                    True, "roster names %s on the planned session %s"
                    % (p["seat"], p["session"]))
        if "beacon" not in rows:
            live = [e for e in beacons.entries(p["seat"])
                    if beacons.pid_alive(e["pid"], e.get("starttime")) is True]
            fresh = [e for e in live
                     if str(e.get("session") or "") == p["session"]
                     and float(e.get("armed") or 0) >= since]
            if fresh:
                rows["beacon"] = (
                    True, "inbox beacon armed by pid %d for session %s after "
                    "the exit" % (fresh[0]["pid"], p["session"]))
            elif live:
                misses["beacon"] = (
                    "the only live inbox beacon%s for %s %s the one this "
                    "rehome asked for: %s"
                    % ("s"[:len(live) != 1], p["seat"],
                       "are not" if len(live) != 1 else "is not",
                       "; ".join(
                           "pid %d on session %s armed %s"
                           % (e["pid"], e.get("session") or "none",
                              "before the exit" if float(e.get("armed") or 0)
                              < since else "since the exit")
                           for e in live)))
            else:
                misses["beacon"] = "no live inbox beacon is armed"
        if len(rows) == 3 or time.time() >= end:
            break
        sleep(poll_s)
    out = []
    for surface in ("pane", "register", "beacon"):
        if surface in rows:
            out.append((surface, True, rows[surface][1]))
        else:
            out.append((surface, False, misses.get(surface, "not measured")))
    return out


def apply_rehome(p, out=None, sleep=None):
    """(rc, refusal) — drive the plan. Every actuation is an existing door.

    THE LAUNCH IS MARKED SO THE VERIFICATION CAN NAME IT. One `uuid4` is
    minted per apply and rides on the launch line as `HELM_REHOME_ATTEMPT=<v>`,
    bound to the launch invocation itself, so the process this line starts
    carries it in its own environ and no other process in the pane does. Every
    other property the verification can read — the home, the session, the
    model, a birth later than the exited process — is also true of a claude
    somebody else started in the same pane at the same moment. See
    `new_attempt`: it is correlation, never authority.

    THE ONE PANE-INPUT WAKE is the /exit. A signal is never sent: SIGTERM on a
    claude session loses the transcript flush, and the transcript is the thing
    the relaunch resumes, so killing the seat would silently truncate the
    conversation this verb exists to carry onto the new home.

    THE HOME IS RE-ASKED HERE, through `check_home` — the SAME predicate the
    plan used, not a second opinion about it. A plan can sit in a terminal for
    an hour, and everything it proved about the home (Orca's copy, the home's
    own chain, the launch-time sync switch) can move in that hour. One
    predicate asked twice means a world that changed between the plan and the
    act REFUSES, while a re-implementation of the same rules here would mean
    two gates to keep in step and one of them eventually wrong.

    ONE PANE ACROSS THE EXIT, THE PROOF AND THE RELAUNCH. This function binds
    the pane to the stamped live process, and the exit is then delivered as an
    `operation` INSIDE `send_to_pane` — the door takes its own send-time
    binding, as it must, and the operation refuses to type anything at all
    unless that binding is the one bound here. TWO INDEPENDENT RESOLUTIONS ARE
    THE HAZARD: a pane-key remap between them exits the process behind one
    handle and types the launch line into another, and the two readings
    disagreeing is the only observable that says so. The handle the exit was
    actually spent on is then the handle the exit proof reads and the relaunch
    types into, so nothing downstream re-resolves.
    """
    from . import harness
    out = out or sys.stdout
    ad, seat_name = p["adapter"], p["seat"]
    # THE ACT'S OWN CLOCK, read before anything is touched: every surface the
    # verification accepts has to be newer than this.
    since = time.time()
    _note, refusal = check_home(p["home"])
    if refusal:
        return 1, Refusal(
            refusal.check,
            "the home was admitted when this plan was made and is REFUSED now, "
            "so the world moved between the plan and the act: %s. Nothing was "
            "typed and the seat is still on its old home" % refusal.reason,
            refusal.remedy)
    # THE ATTEMPT MARKER, MINTED ONCE, HERE. After the gate that can still
    # refuse (nothing is minted for a rehome that never happens) and before
    # anything at all is typed, so the value exists before the exit and the
    # launch line that carries it is composed from it rather than the other way
    # round. The plan's `pane_line` was the dry run's rendering of this line;
    # the line this function types is the one below, and it is the line the
    # ledger row and every report carry.
    attempt = new_attempt()
    p = dict(p, attempt=attempt,
             pane_line=attempt_launch_line(p["command"], p["cwd"], attempt))
    # RE-BOUND AT THE ACT, never carried from the plan. The plan's handle is a
    # claim about the past; the pane this verb is about to type into has to be
    # bound to the stamped process NOW, through the one primitive every adopted
    # send routes through. A pane relaunched between the plan and the apply is
    # a different agent, and the refusal below is the whole reason that cannot
    # become a keystroke.
    handle, proof = orcaadopt.authorized_handle(p["pids"], ad)
    if handle is None:
        return 1, Refusal(
            "pane proof at the act",
            "the pane for %s could not be re-bound at the moment of the exit: "
            "%s" % (seat_name, proof),
            "nothing was typed and the seat is untouched; re-run the dry run "
            "and read what changed")
    bound = {"handle": None, "drift": None}

    def exit_into_the_bound_pane(adapter, at_send, typed):
        """The /exit, typed ONLY into the pane this function bound.

        `send_to_pane` takes its own binding at send time, which is the one
        re-proof that may never be skipped — so this compares rather than
        replaces. Disagreement means the pane key was remapped between the two
        readings, and the answer is to type NOTHING: the process that would be
        exited and the pane that would receive the launch line are no longer
        the same place, and that is the defect, not the cure.
        """
        bound["handle"] = at_send
        if at_send != handle:
            bound["drift"] = (
                "the pane bound for the exit is %s and the pane bound one "
                "moment earlier for this seat's authorized process is %s — a "
                "pane-key remap between the two readings, so an exit here and "
                "a launch line there would be two different panes"
                % (at_send, handle))
            return harness.NOT_DELIVERED, bound["drift"]
        return adapter.submit(at_send, "/exit", on_typed=typed)

    mode, detail = orcaadopt.send_to_pane(
        seat_name, "/exit", expect_pids=p["pids"], adapter=ad,
        operation=exit_into_the_bound_pane)
    if bound["drift"]:
        return 1, Refusal(
            "one pane across the exit and the relaunch", bound["drift"],
            "nothing was typed and the seat is untouched; re-run the dry run, "
            "which prints the pane it resolved")
    print("  exit      /exit -> %s (%s)" % (mode, detail), file=out)
    if mode != "resumed":
        return 1, Refusal(
            "clean exit",
            "the /exit wake was not delivered (%s): %s" % (mode, detail),
            "the seat is untouched and still on its old home; read the pane "
            "and re-run when it takes input")
    # THE ONE HANDLE, CARRIED. Everything below addresses the pane the exit was
    # actually spent on, never a fresh resolution of the seat's name.
    handle = bound["handle"]
    gone, why = _wait_gone(p["pids"], sleep=sleep)
    if not gone:
        dialog = _standing_dialog(ad, handle)
        return 1, Refusal(
            "old session gone",
            "%s%s" % (why, ("; " + dialog) if dialog else ""),
            "answer the pane by hand and re-run — helm never signals a claude "
            "session, because a killed session loses the transcript flush the "
            "relaunch resumes")
    print("  exited    pid%s %s PROVEN gone"
          % ("s"[:len(p["pids"]) != 1],
             ", ".join(str(int(x)) for x in p["pids"])), file=out)
    # THE RELAUNCH TYPES INTO A PANE THAT NOW HOLDS A BARE SHELL. Its authority
    # is the handle `orcaadopt.authorized_handle` bound to the stamped process
    # above and the exit was spent on, plus the exit proof just printed: the
    # authorized process is gone and its pane is the one it left behind. Same
    # shape `seat resume` uses for its DEAD-PANE leg.
    try:
        ad.send(handle, p["pane_line"], enter=True)
    except Exception as e:                  # noqa: BLE001 — a send failure is a reason
        return 1, Refusal(
            "relaunch",
            "the launch line did not reach pane %s (%s: %s)"
            % (handle, e.__class__.__name__, e),
            "the old session has exited; type the line into the pane by "
            "hand:\n  " + p["pane_line"])
    print("  launched  %s" % p["pane_line"], file=out)
    found = {}
    rows = verify(p, deadline_s=VERIFY_WAIT_S, sleep=sleep, since=since,
                  found=found)
    for surface, proven, detail in rows:
        print("  %-9s %s — %s" % (surface, "PROVEN" if proven else "UNPROVEN",
                                  detail), file=out)
    unproven = [s for s, proven, _d in rows if not proven]
    # THE LEDGER ROW IS WRITTEN WHETHER OR NOT EVERY SURFACE CAME BACK, and it
    # carries what was proven. A rehome that left a seat half-up is exactly the
    # event an operator needs recorded; only recording the clean ones would
    # make the ledger an optimist.
    ok, why = record({"seat": seat_name, "home": p["home"],
                      "model": p["model"], "session": p["session"],
                      "handle": handle, "command": p["command"],
                      "pane_line": p["pane_line"], "attempt": attempt,
                      "home_note": p["home_note"],
                      "process": found.get("process"),
                      "verified": {s: bool(v) for s, v, _d in rows}})
    if not ok:
        # A MISSING DURABLE ROW IS A PARTIAL EFFECT, NEVER A SUCCESS. The seat
        # HAS been moved — it was exited and relaunched, and no rollback is
        # offered here because re-exiting a session that is coming up is a
        # second actuation on a pane whose state nobody has read. So the
        # report says exactly what happened, what is missing, and what to do,
        # and the exit status carries it: a caller that only reads rc must not
        # be told a rehome with no record of it went cleanly.
        return 1, Refusal(
            "rehome ledger row",
            "the seat WAS moved — %s exited and relaunched on home %s "
            "(session %s%s), and %s. Nothing was rolled back and nothing was "
            "retried: the move is real and only its durable row is missing"
            % (seat_name, p["home"], p["session"],
               ", model " + p["model"] if p["model"] else "",
               why),
            "the launch line that ran was:\n  %s\n  surfaces: %s\n  re-run "
            "`helm seat rehome %s --home %s` (dry run) to re-read the seat, "
            "and repair the ledger at %s — it is the only thing missing"
            % (p["pane_line"],
               ", ".join("%s %s" % (s, "PROVEN" if v else "UNPROVEN")
                         for s, v, _d in rows),
               seat_name, p["home"], ledger_path()))
    if unproven:
        return 1, Refusal(
            "post-launch verification",
            "the launch line was typed into pane %s but %s did not come back "
            "within %gs" % (handle, " and ".join(unproven), VERIFY_WAIT_S),
            "read the pane: `helm seat where %s`, then `helm beacons`" % seat_name)
    return 0, None


def cmd_rehome(args):
    """seat rehome <seat> --home H [--model M] [--apply]"""
    from .cli import guard_tail
    usage = ("usage: helm seat rehome <seat> --home H [--model M] [--apply]\n"
             "  move one live seat onto a named credhome: prove the home's "
             "token, exit the session cleanly, relaunch it on the home with "
             "the same session id. Dry-run without --apply.")
    args = list(args or [])
    if not args or args[0].startswith("-"):
        print(usage, file=sys.stderr)
        return 2
    seat_name, rest = args[0], args[1:]
    rc = guard_tail("helm seat rehome", rest, flags=("--apply",),
                    valued=("--home", "--model"), usage=usage)
    if rc is not None:
        return rc
    if "--home" not in rest:
        print("helm seat rehome: --home names the credhome to move %s onto "
              "(`helm cred list`)" % seat_name, file=sys.stderr)
        return 2
    home_name = rest[rest.index("--home") + 1]
    model = rest[rest.index("--model") + 1] if "--model" in rest else None
    apply = "--apply" in rest
    p, refusal = plan(seat_name, home_name, model=model)
    if refusal:
        print(refusal.line(), file=sys.stderr)
        return 1
    print_plan(p, apply=apply)
    if not apply:
        print("  (dry run — nothing was typed, nothing was written; re-run "
              "with --apply)")
        return 0
    rc, refusal = apply_rehome(p)
    if refusal:
        print(refusal.line(), file=sys.stderr)
    return rc
