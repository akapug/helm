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
                              sid found alive is somebody else's pane), then
                              session.live_presence: the session the pane's
                              OWN process holds now outranks it (task/3208,
                              `_live_session`)
  * the pane                  orcaadopt.resolve + orcaadopt.authorized_handle
                              (a handle BOUND to a stamped live process, the
                              one primitive every adopted send routes through)
  * the home                  cred.sync (dry-run) + cred.sync_disabled +
                              cred._token_facts, and cred._cure for the remedy
                              prose. The gate is ONE predicate, asked at the
                              plan AND again at the act.
  * the exit                  orcaadopt.send_to_pane — ONE pane-input wake,
                              typed and submitted and verified by the adapter's
                              exit door (`submit_exit`: the turn verb, then
                              the exit-confirm dialog a session with background
                              tasks raises, confirmed on option 1 only when
                              this /exit opened it). NEVER a signal: a kill
                              loses the transcript flush this whole verb exists
                              to keep. It rides in as an `operation` so the
                              pane the exit is spent on is the pane the
                              relaunch types into: ONE binding, not two
                              resolutions.
  * the exit proof            beacons.pid_alive against the SAME birth stamp
                              the handle was authorized on. It also proves an
                              exit the pane could not show: a /exit this
                              apply typed whose wake reads UNVERIFIED goes on
                              to this wait, and the ledger row records the
                              exit as proven by the pid, not by the pane
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
credhome, the Orca -> home direction is the only one, and a home that would
have to take Orca's live chain while Orca still refreshes it is refused by
cred.sync's own guards rather than by a second opinion here.

THE ONE OTHER TARGET IS THE DEFAULT, BY NAME (task/3515). A seat on the
default home has its credit walls handled by the fleet's watcher (Orca
switches the account there for every seat at once); a seat PINNED to a named
credhome whose account is exhausted or kicked has to come off the pin, and
that was done by hand. `--home default` (DEFAULT_TARGET) is that move: a
reserved word, never a path, so the default's own path and every other
directory keep `check_home`'s credhome refusal. The launch runs with
CLAUDE_CONFIG_DIR REMOVED (`env -u`), because the default home is the absence
of the variable; the gate asks only what is true there (a token is present and
its chain is live and proven, by cred's own readers), with no sync leg,
because Orca and not helm refreshes that token.

EVERY REHOME KEEPS THE PERMISSION MODE (task/3515, measured: a seat on a home
whose settings default to bypass came back on another home in auto mode). Each
home's settings.json carries its own permissions.defaultMode, so a relaunch
that carries no flag takes the NEW home's. The plan reads the mode the old
process ran with (its argv flag, else the old home's settings default) and
the launch line passes it explicitly; verification asks the new argv for it.

AND IT NAMES WHAT THE EXIT TOOK. The exit-confirm dialog stops the session's
monitors and scheduled tasks, and no background shell survives a relaunch.
The rows the dialog listed are recorded in the ledger row and handed to the
seat in one DM after the relaunch, as a re-arm line.
"""
import glob
import json
import os
import shlex
import sys
import time
import uuid

from . import beacons, cred, home, homes, orcaadopt, panetail, pk, seats

LEDGER = "seat-rehomes.jsonl"
# THE RESERVED TARGET for the provider default home. A word, never a path: a
# path is resolved like any home and refused unless it is a named credhome.
DEFAULT_TARGET = "default"
# The words claude takes a permission mode on.
SKIP_FLAG = "--dangerously-skip-permissions"
MODE_FLAG = "--permission-mode"
# The modes claude's --permission-mode takes ("default" is its hidden one). A
# settings value outside them is never passed: the relaunch would exit on it.
PERMISSION_MODES = frozenset(("acceptEdits", "auto", "bypassPermissions",
                              "manual", "dontAsk", "plan", "default"))
# What an operator may name with --mode: the choices claude lists, never the
# hidden `default` word (it draws manual, so the operator names manual).
OPERATOR_MODES = PERMISSION_MODES - {"default"}
# The project layers that set a mode for a cwd, in claude's precedence order.
PROJECT_SETTINGS = ("settings.local.json", "settings.json")
# What an UNKNOWN live mode asks of the operator.
MODE_UNKNOWN_REMEDY = ("re-run with --mode M naming the mode the seat runs "
                       "in (one of acceptEdits, auto, bypassPermissions, "
                       "manual, dontAsk, plan)")
# Who the post-relaunch wake is from.
WAKE_WHO = "seat-rehome"
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

    `DEFAULT_TARGET` is the one name that skips rule 1, and it is asked its
    own questions (`check_default_home`); a PATH to the default home is still
    refused by rule 1.
    """
    now_ms = time.time() * 1000 if now_ms is None else now_ms
    if is_default_target(home_name):
        named = os.path.join(homes.ROOTS["claude"], DEFAULT_TARGET)
        if os.path.isdir(named):
            return None, Refusal(
                "home name is unambiguous",
                "`%s` is ambiguous: it is the reserved word for the default "
                "home, and a credhome named %s exists too"
                % (DEFAULT_TARGET, cred._display_path(named)),
                "name that credhome by its path to move onto it, or rename it "
                "to move onto the default home")
        return check_default_home(now_ms=now_ms)
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


def is_default_target(home_name):
    return home_name == DEFAULT_TARGET


def default_home():
    """The provider default home as a path: where claude reads its config
    when CLAUDE_CONFIG_DIR is unset."""
    return os.path.realpath(homes.DEFAULTS["claude"])


def check_default_home(now_ms=None):
    """(note, refusal) — may a session be started on the default home?

    ONLY WHAT IS TRUE THERE. Orca, not helm, writes and refreshes the default
    home's token, so there is no sync to ask about: the home must hold
    credentials, and their refresh chain must be proven and alive by cred's
    own readers. A token Orca has let lapse is refused here, before the live
    pane is touched, for the same reason a spent credhome is."""
    now_ms = time.time() * 1000 if now_ms is None else now_ms
    real = default_home()
    shown = cred._display_path(real)
    remedy = ("switch or re-log the account in Orca, which owns the default "
              "home's token, then re-run")
    facts, ferr = _home_token_facts(real)
    if ferr:
        return None, Refusal("default home token is live",
                             "the default home %s: %s" % (shown, ferr), remedy)
    life = facts["refresh_expires_at"]
    if life is None:
        return None, Refusal(
            "default home token is live",
            "the default home %s carries NO refreshTokenExpiresAt, so nothing "
            "proves its chain can still refresh — cred's own measure calls "
            "that UNPROVEN, and the live session is exited before the target "
            "is ever used" % shown, remedy)
    if life <= now_ms:
        return None, Refusal(
            "default home token is live",
            "the default home %s carries a chain that EXPIRED at %s, so a "
            "launch there starts claude on a dead token"
            % (shown, _utc(life)), remedy)
    return ("the default home %s (Orca's to refresh; the launch runs with %s "
            "unset): access token expires %s, refresh lifetime to %s"
            % (shown, homes.ENV_VAR["claude"], _utc(facts["expires_at"]),
               _utc(life))), None


def permission_words(argv):
    """The permission words an argv carries, as the launch line would pass
    them: [SKIP_FLAG], [MODE_FLAG, mode], or [] for none."""
    argv = list(argv or ())
    if SKIP_FLAG in argv:
        return [SKIP_FLAG]
    mode = _flag(argv, MODE_FLAG)
    if mode is None:
        for word in argv:
            if word.startswith(MODE_FLAG + "="):
                mode = word.split("=", 1)[1]
    return [MODE_FLAG, mode] if mode else []


def _valid_mode(mode):
    """`mode` when claude's --permission-mode takes it, else None."""
    return mode if mode in PERMISSION_MODES else None


def _settings_mode(config_dir, name="settings.json"):
    """permissions.defaultMode from a settings file in `config_dir`, or
    None."""
    try:
        with open(os.path.join(config_dir, name), encoding="utf-8") as f:
            body = json.load(f)
    except (OSError, ValueError):
        return None
    perms = body.get("permissions") if isinstance(body, dict) else None
    mode = perms.get("defaultMode") if isinstance(perms, dict) else None
    return mode if isinstance(mode, str) and mode.strip() else None


def _mode_of(words):
    """The mode name a set of permission words asks for: the skip flag is
    bypassPermissions, and `default` is the manual mode claude draws."""
    if SKIP_FLAG in (words or ()):
        return "bypassPermissions"
    mode = words[1] if words and len(words) > 1 else None
    return "manual" if mode == "default" else mode


def startup_mode(pids, session=None):
    """(mode, source) — the mode the startup layers of the seat's OLD process
    name, or (None, why) when none names one or they cannot be read. Its own
    argv flag first, then the old cwd's project layers
    (.claude/settings.local.json, then .claude/settings.json), then its home's
    settings.json permissions.defaultMode (the home is the process's
    CLAUDE_CONFIG_DIR, else the default home). A value --permission-mode does
    not take names nothing. A pid whose argv resumes the planned session is
    asked first, so a helper claude in the pane does not name the mode.

    THESE ARE NEVER PROOF OF THE LIVE MODE (task/3515): a session started
    with no flag runs `auto`, and a live shift+tab moves it. They are the
    cross-check on the footer (`old_permission`)."""
    argvs = {int(pid): beacons.proc_argv(int(pid)) for pid in pids}
    ordered = sorted(pids, key=lambda pid: 0 if session and session in
                     (argvs[int(pid)] or ()) else 1)
    whys = []
    for pid in ordered:
        argv = argvs[int(pid)]
        if argv is None:
            whys.append("pid %d's argv is unreadable" % int(pid))
            continue
        words = permission_words(argv)
        if words:
            return _mode_of(words), "pid %d's argv carries %s" % (
                int(pid), " ".join(words))
        cwd = beacons.proc_cwd(int(pid))
        for layer in PROJECT_SETTINGS if cwd else ():
            mode = _valid_mode(_settings_mode(os.path.join(cwd, ".claude"),
                                              layer))
            if mode:
                return _mode_of([MODE_FLAG, mode]), (
                    "pid %d's cwd .claude/%s sets %s" % (int(pid), layer, mode))
        env = beacons.proc_env(int(pid))
        if env is None:
            whys.append("pid %d's environment is unreadable" % int(pid))
            continue
        where = env.get(homes.ENV_VAR["claude"]) or default_home()
        mode = _valid_mode(_settings_mode(where))
        if mode:
            return _mode_of([MODE_FLAG, mode]), (
                "pid %d's home %s defaults to %s"
                % (int(pid), cred._display_path(where), mode))
        whys.append("pid %d carries no flag and no settings layer sets a mode"
                    % int(pid))
    return None, "; ".join(whys) or "no pid"


def old_permission(pids, session=None, footer=(None, "the pane was not read")):
    """(words, note) — the permission words the relaunch passes, or
    (None, note) when the LIVE mode is UNKNOWN.

    THE LIVE MODE IS KNOWN ONLY WHEN THE PANE PROVES IT (task/3515). `footer`
    is `panetail.permission_footer` of the bound pane: the row Claude Code
    draws for the mode the session is in now. The startup layers
    (`startup_mode`) cannot prove it (no flag runs `auto`; shift+tab moves it)
    and are the cross-check only: a footer they contradict is UNKNOWN, since
    one of the two readings is wrong and a guess could widen the seat (plan
    to manual allows edits after consent; dontAsk to anything prompts). An
    UNKNOWN mode is never guessed: the apply refuses before /exit unless the
    operator names the mode (`--mode`). The words passed are the old argv's
    own when they name the same mode, else `--permission-mode <mode>`."""
    live, row = footer
    if live is None:
        return None, "UNKNOWN: %s, so the live mode is not proved" % row
    started, source = startup_mode(pids, session)
    if started and started != live:
        return None, ("UNKNOWN: the pane's footer `%s` says %s and %s (%s), "
                      "so one reading is wrong" % (row, live, started, source))
    words = None
    for pid in pids:
        argv = beacons.proc_argv(int(pid))
        if argv and _mode_of(permission_words(argv)) == live:
            words = permission_words(argv)
            break
    words = words or [MODE_FLAG, live]
    return words, "proved by the pane's footer `%s`%s" % (
        row, "; the startup layers agree (%s)" % source if started else
        "; no startup layer names a mode (%s)" % source)


def launch_command(seat, home_name, model=None, session=None, fresh=False,
                   permission=()):
    """The exact `helm launch` line this rehome runs — the owner's own hand
    procedure, quoted. `--model` and `--resume` ride AFTER `--` because they
    are claude's flags, and launch passes everything past `--` through
    verbatim (launch.parse_args). `fresh` is a session with no transcript
    yet: claude refuses `--resume` for it and accepts `--session-id`
    (`_live_session`).

    THE DEFAULT HOME IS THE ABSENCE OF CLAUDE_CONFIG_DIR, so its line names no
    --home and runs under `env -u`: a pane shell that exported the old pin
    would otherwise hand it straight to the relaunch. `permission` rides after
    the model, so the kept mode is explicit rather than the new home's."""
    if is_default_target(home_name):
        line = ["env", "-u", homes.ENV_VAR["claude"], "helm", "launch",
                "--seat", seat, "--"]
    else:
        line = ["helm", "launch", "--seat", seat, "--home", home_name, "--"]
    if model:
        line += ["--model", model]
    line += list(permission or ())
    if session:
        line += ["--session-id" if fresh else "--resume", session]
    return " ".join(shlex.quote(word) for word in line)


def _live_session(roster_sid, pids):
    """(session, note, fresh, root, refusal) — the session this rehome
    relaunches, and the config root whose transcripts decided `fresh` (None
    when the roster decided).

    THE PANE'S OWN PROCESS NAMES IT (task/3208). The roster's CURRENT session
    is whatever the last SessionStart joined; the process in the pane names
    the session it holds NOW in its presence record, which claude rewrites the
    instant a /clear lands (MEASURED on 2.1.283). A /clear the roster never saw
    leaves the two apart, and resuming the roster's would bring the pre-clear
    conversation back on the new home. So a readable record decides; with
    none readable the roster decides, as before. Several pane processes
    naming different sessions, none of them the roster's, is UNKNOWN and
    refuses — before anything is typed.

    ONLY THE PANE'S OWN CLAUDE MAY OVERRULE THE ROSTER. Every process the seat
    starts inherits its pane key and its name, so a `claude -p` helper sits in
    the pane's candidate set beside the seat's own claude and writes a record
    of its own session (MEASURED on 2.1.283: entrypoint "sdk-cli"; the pane's
    interactive claude writes "cli"). A record overrules the roster only when
    it is the interactive "cli" process; any other record counts only when it
    names the roster's session already. `seat resume` answers the same
    question from its spawn register's evidence (`live_seat_session`); the
    roster pins no pid, so the process's own entrypoint is the evidence here.

    A SESSION WITH NO TRANSCRIPT CANNOT BE RESUMED. claude writes a session's
    transcript at its first message and answers `--resume` for one with none
    with "No conversation found" and exit 1 (MEASURED) — after this verb has
    already exited the seat. It accepts `--session-id` for exactly that id
    (MEASURED), so `fresh` launches the same id new. Asked where the process
    writes its transcripts, its own config root; exiting a session with no
    messages writes none (MEASURED), so the answer holds across the exit.
    """
    from . import session as sessionmod
    held, whys = {}, []
    for pid in pids:
        rec, root, why = sessionmod.live_presence(
            int(pid), getattr(pid, "start", None))
        if rec is None:
            whys.append(why)
            continue
        if rec["sessionId"] != roster_sid and rec.get("entrypoint") != "cli":
            whys.append("pid %d is not the pane's interactive claude "
                        "(entrypoint %r) and its session is not the roster's"
                        % (int(pid), rec.get("entrypoint")))
            continue
        held.setdefault(rec["sessionId"], root)
    if not held:
        return (roster_sid, "the seat's CURRENT roster session; no presence "
                "record answered (%s)" % ("; ".join(whys) or "no pane process"),
                False, None, None)
    if roster_sid in held:
        chosen = roster_sid
    elif len(held) == 1:
        chosen = next(iter(held))
    else:
        return None, None, False, None, Refusal(
            "seat session",
            "the pane's live processes hold sessions %s and the roster's "
            "current session is %s, so which conversation this rehome would "
            "resume is UNKNOWN" % (", ".join(sorted(held)), roster_sid),
            "let the seat's helper processes finish, then re-run")
    fresh = not _has_transcript(held[chosen], chosen)
    note = "the session the pane's live process holds (its presence record)"
    if chosen != roster_sid:
        note += "; the roster still names %s" % roster_sid
    if fresh:
        note += ("; it has no transcript yet, so the relaunch starts it FRESH "
                 "under the same id (--session-id)")
    return chosen, note, fresh, held[chosen], None


def _has_transcript(root, sid):
    """Does claude's config root `root` hold a transcript for `sid`?"""
    return bool(glob.glob(os.path.join(glob.escape(root), "projects", "*",
                                       sid + ".jsonl")))


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


def plan(seat, home_name, model=None, adapter=None, now_ms=None, mode=None):
    """(plan, refusal) — everything the apply would do, decided before
    anything is touched.

    ORDER IS THE SAFETY. The home is checked LAST of the three resolutions but
    BEFORE any actuation, so a refusal leaves a live seat exactly as it was;
    and the seat/pane resolutions run first so a refusal about the home is
    never printed for a seat helm could not have rehomed anyway.

    `mode` is the operator's `--mode M`: the relaunch mode when the live one
    cannot be proved, recorded as operator-chosen and verified like any.
    """
    from . import harness
    if mode is not None and mode not in OPERATOR_MODES:
        return None, Refusal(
            "--mode is a mode claude takes",
            "%r is not a --permission-mode claude takes" % mode,
            "name one of %s (claude draws its hidden `default` as manual)"
            % ", ".join(sorted(OPERATOR_MODES)))
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
    session, session_note, fresh, root, refusal = _live_session(session, pids)
    if refusal:
        return None, refusal
    note, refusal = check_home(home_name, now_ms=now_ms)
    if refusal:
        return None, refusal
    # THE HOME AS A PATH, resolved once here so verification can ask the new
    # process which home it actually runs on (`CLAUDE_CONFIG_DIR` in its own
    # environ) instead of comparing a name to a name.
    if is_default_target(home_name):
        home_real = default_home()
    else:
        home_real, _herr = cred._resolve_home(home_name)
    cwd = None
    for pid in pids:
        cwd = beacons.proc_cwd(int(pid))
        if cwd:
            break
    try:
        footer = panetail.permission_footer(ad.read(handle))
    except Exception as e:                  # noqa: BLE001 — unread is UNKNOWN
        footer = (None, "the pane could not be read (%s: %s)"
                  % (e.__class__.__name__, e))
    permission, permission_note = old_permission(pids, session, footer)
    if mode is not None:
        permission, permission_note = [MODE_FLAG, mode], (
            "operator-chosen with --mode %s (the pane: %s)"
            % (mode, permission_note))
    command = launch_command(seat, home_name, model=model, session=session,
                             fresh=fresh, permission=permission)
    from . import seat_resume_all
    return {"seat": seat, "home": home_name, "home_real": home_real,
            "default": is_default_target(home_name),
            "permission": permission, "permission_note": permission_note,
            "mode_chosen": mode, "model": model,
            "session": session, "session_note": session_note,
            "flag": "--session-id" if fresh else "--resume",
            "transcript_root": root,
            "handle": handle, "adapter": ad,
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
    print("  session   %s (%s)" % (p["session"], p.get("session_note") or
                                   "the seat's CURRENT roster session"),
          file=out)
    print("  pane      %s — %s" % (p["handle"], p["pane_proof"]), file=out)
    print("  cwd       %s" % (p["cwd"] or "unreadable — the launch line "
                              "carries no cd"), file=out)
    print("  home      %s" % p["home_note"], file=out)
    if p.get("permission") is None:
        print("  mode      UNKNOWN (%s) — --apply refuses before the /exit: "
              "%s" % (p.get("permission_note") or "not read",
                      MODE_UNKNOWN_REMEDY), file=out)
    else:
        print("  mode      %s (%s)" % (" ".join(p["permission"]),
                                       p.get("permission_note")), file=out)
    if p["unidentified"]:
        print("  census    %s" % p["unidentified"], file=out)
    print("  plan      1. /exit into the pane (ONE wake, submitted and "
          "verified; never a signal) — the exit-confirm dialog it opens on a "
          "seat with background tasks is confirmed on '%d. %s' and nowhere "
          "else" % panetail.EXIT_CONFIRM, file=out)
    print("            2. wait up to %gs for pid%s %s to be PROVEN gone "
          "(which also proves an exit the pane could not show)"
          % (EXIT_WAIT_S, "s"[:len(p["pids"]) != 1],
             ", ".join(str(int(x)) for x in p["pids"])), file=out)
    print("            3. type the launch line into that same pane", file=out)
    print("            4. verify pane + roster register + inbox beacon", file=out)
    print("            5. DM the seat what the exit stopped, to re-arm",
          file=out)
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


def _pane_now(ad, handle):
    """What the pane shows NOW, as one clause a refusal can carry.

    A REFUSAL AFTER /exit HAS BEEN TYPED MUST SAY WHERE THE PANE IS, never
    assume it is where it started (task/3201: a refusal called a pane
    "untouched" while it sat in the exit-confirm dialog its own /exit opened).
    READ-ONLY: this answers, it never types. The one dialog this verb ever
    confirms is the exit-confirm dialog, and only inside `submit_exit`, the
    door that knows its own /exit opened it; every other dialog is NAMED here
    and left to a person.
    """
    from . import harness, seat
    try:
        tail = ad.read(handle)
    except Exception as e:                  # noqa: BLE001 — a probe error is a reason
        return "the pane could not be read back (%s: %s)" % (
            e.__class__.__name__, e)
    if not (tail or "").strip():
        return ("the pane read back EMPTY, which is what a failed read "
                "returns, so what it shows is UNKNOWN")
    dialog = panetail.exit_dialog(tail)
    if dialog.standing:
        return panetail.dialog_summary(dialog)
    options = seat._prompt_options(tail)
    if options:
        return "the pane is showing a dialog: %s" % "; ".join(
            "%s. %s" % (n, str(label).strip()[:60]) for n, label in options)
    line = harness._prompt_line(tail)
    if line is None:
        return ("the pane shows no composer helm can read and no dialog it "
                "recognises")
    body = harness.composer_body(line)
    if not body:
        return "the pane is at an EMPTY composer"
    if body == harness.EXIT_COMMAND:
        return ("the pane's composer still holds the typed %s, unsubmitted"
                % harness.EXIT_COMMAND)
    return ("the pane's composer holds other text (%d characters, not shown)"
            % len(body))


def _unproven_exit(p, ad, handle, mode, detail, waited=None):
    """The refusal for an exit nothing proved: neither the pane (the wake's
    verdict) nor, when it was asked, the pid (`waited`, the wait's reason).

    IT SAYS WHERE THE PANE IS AND WHETHER THE SESSION RUNS, both measured
    now. "Untouched" was the old claim, and it was false exactly when /exit
    had landed and left the pane mid-dialog (task/3201)."""
    now = _pane_now(ad, handle)
    state, proc = _process_now(p["pids"])
    remedy = {
        "running": "the session is still running on its OLD home and "
                   "nothing was signalled. Answer what the pane shows by "
                   "hand (in the exit-confirm dialog, Esc keeps the session "
                   "and Enter on '%d. %s' ends it), then re-run once the "
                   "pane is back at an empty composer" % panetail.EXIT_CONFIRM,
        "exited": "the session EXITED although neither the pane nor the wait "
                  "proved it in time, so the seat is DOWN and its pane holds "
                  "a shell. This verb verifies only a launch it typed after "
                  "proving the exit; type the line into the pane by hand:\n"
                  "  %s" % p["pane_line"],
        "unknown": "read the pane (`helm seat where %s`) before doing "
                   "anything: nothing proves whether the session is still "
                   "running" % p["seat"]}[state]
    return Refusal(
        "clean exit",
        "the /exit wake was not proven (%s): %s%s. The pane now: %s; the "
        "seat's process: %s"
        % (mode, detail,
           "; and the pid did not prove it either: %s" % waited
           if waited else "", now, proc),
        remedy)


def _process_now(pids):
    """The seat's authorized process, measured once: running, EXITED, or
    unreadable. A refusal states it because a pane is not a process — the
    session behind an open dialog is still running."""
    done, unproven = _gone(pids)
    shown = ", ".join(str(int(x)) for x in pids)
    if done:
        return "exited", "pid %s has EXITED" % shown
    if unproven:
        return "unknown", ("pid %s could not be read, so whether the session "
                           "is still running is UNKNOWN"
                           % ", ".join(str(x) for x in unproven))
    return "running", "pid %s is still running" % shown


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
        if p.get("default"):
            # THE DEFAULT IS THE ABSENCE: no variable, or one that names the
            # default home itself. The old pin still standing is the miss.
            on = (not ran) or bool(want and os.path.realpath(ran) ==
                                   os.path.realpath(want))
        else:
            on = bool(ran and want and
                      os.path.realpath(ran) == os.path.realpath(want))
        if not on:
            why.append("pid %d runs on home %s and this rehome planned %s"
                       % (pid, cred._display_path(ran) if ran else
                          "the default (no %s)" % homes.ENV_VAR["claude"],
                          p["home"]))
            continue
        named = env.get("HELM_CHAT_NAME")
        if (named or "").casefold() != p["seat"].casefold():
            why.append("pid %d is named %s and this rehome relaunched %s"
                       % (pid, named or "nothing (no HELM_CHAT_NAME)",
                          p["seat"]))
            continue
        argv = beacons.proc_argv(pid)
        if argv is None:
            why.append("pid %d's argv could not be read, so the session and "
                       "model it resumed are UNKNOWN" % pid)
            continue
        flag = p.get("flag") or "--resume"
        resumed = _flag(argv, flag)
        if resumed != p["session"]:
            why.append("pid %d carries %s %s and this rehome planned %s"
                       % (pid, flag, resumed or "none", p["session"]))
            continue
        if p["model"] and _flag(argv, "--model") != p["model"]:
            why.append("pid %d runs model %s and this rehome planned %s"
                       % (pid, _flag(argv, "--model") or "the home default",
                          p["model"]))
            continue
        # THE MODE IS VERIFIED WHENEVER THE PLAN KNOWS IT: a flag it passed,
        # or none at all when the cwd's project layer sets the mode.
        kept = p.get("permission")
        if kept is not None and permission_words(argv) != list(kept):
            why.append("pid %d runs with %s and this rehome kept the old "
                       "process's %s"
                       % (pid, " ".join(permission_words(argv)) or "no "
                          "permission flag", " ".join(kept)))
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
    # AN UNPROVED LIVE MODE IS NEVER GUESSED: a relaunch in the wrong mode
    # can widen the seat, so the apply refuses before anything is typed.
    if p.get("permission") is None:
        return 1, Refusal(
            "permission mode is proved",
            "%s. Nothing was typed and the seat is still on its old home"
            % p.get("permission_note"), MODE_UNKNOWN_REMEDY)
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
    # `typed` records that THIS apply's /exit reached the composer: the one
    # fact that lets the pid, rather than the pane, prove the exit below.
    bound = {"handle": None, "drift": None, "typed": None, "stopped": None}

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
        def placed(at, text):
            bound["typed"] = text
            if typed is not None:
                typed(at, text)

        # THE EXIT DOOR, not the bare turn verb: a seat holding background
        # tasks answers /exit with a confirm dialog, and only the door that
        # typed the /exit may answer it (task/3201). It answers through the
        # one dialog door (task/3209), and the seat's OWN stamped process
        # lends it the vendor's presence record as a second witness.
        stamped = [x for x in p["pids"] if getattr(x, "start", None)]
        presence = (harness.presence_witness(
            int(stamped[0]), stamped[0].start,
            panetail.EXIT_KIND.waiting_for) if stamped else None)
        def listed(tail):
            # WHAT THIS /exit'S OWN DIALOG SAYS IT WILL STOP, read off the
            # screen before it is answered (task/3515).
            bound["stopped"] = panetail.exit_dialog_items(tail)

        return adapter.submit_exit(at_send, on_typed=placed,
                                   presence=presence, on_dialog=listed)

    mode, detail = orcaadopt.send_to_pane(
        seat_name, harness.EXIT_COMMAND, expect_pids=p["pids"], adapter=ad,
        operation=exit_into_the_bound_pane)
    if bound["drift"]:
        return 1, Refusal(
            "one pane across the exit and the relaunch", bound["drift"],
            "nothing was typed and the seat is untouched; re-run the dry run, "
            "which prints the pane it resolved")
    print("  exit      /exit -> %s (%s)" % (mode, detail), file=out)
    # THE PID OUTRANKS THE PANE WHEN THE PANE CANNOT SAY (task/3201). The
    # wake's verdict is a reading of the composer, and a session that exits
    # takes its composer with it: the pane then holds a shell prompt under the
    # old frame, the read-back finds no composer, and the wake answers
    # UNVERIFIED about a /exit that worked. What the rest of this plan needs is
    # the old process DEAD, which `_wait_gone` proves against the same birth
    # stamp the handle was authorized on. So an UNVERIFIED wake whose /exit
    # THIS apply typed goes on to that wait, and the exit counts as proven by
    # the pid, never by the pane.
    #
    # TWO THINGS STAY REFUSALS. A PROVEN non-exit (`manual`: the pointer sat
    # on Stay, the composer refused, the pane was remapped) is an answer, not
    # an absence of one, so there is nothing to wait for. And a /exit this
    # apply never typed proves nothing about why the process might end: a
    # person exiting their own pane is not this verb's exit.
    by_pid = mode == "unverified" and bool(bound["typed"])
    if mode != "resumed" and not by_pid:
        return 1, _unproven_exit(p, ad, bound["handle"] or handle, mode,
                                 detail)
    # THE ONE HANDLE, CARRIED. Everything below addresses the pane the exit was
    # actually spent on, never a fresh resolution of the seat's name.
    handle = bound["handle"]
    gone, why = _wait_gone(p["pids"], deadline_s=EXIT_WAIT_S, sleep=sleep)
    if not gone:
        if by_pid:
            return 1, _unproven_exit(p, ad, handle, mode, detail, waited=why)
        return 1, Refusal(
            "old session gone",
            "%s; the pane now: %s" % (why, _pane_now(ad, handle)),
            "answer the pane by hand and re-run — helm never signals a claude "
            "session, because a killed session loses the transcript flush the "
            "relaunch resumes")
    proven_by = "pid" if by_pid else "pane"
    if p.get("flag") == "--session-id" and p.get("transcript_root") and \
            _has_transcript(p["transcript_root"], p["session"]):
        # The session took its first message between the plan and the exit,
        # so it is resumable now and `--session-id` on it would be refused
        # ("already in use", MEASURED). Asked after the exit is proven: no
        # process is left to write it.
        command = launch_command(p["seat"], p["home"], model=p["model"],
                                 session=p["session"],
                                 permission=p.get("permission"))
        p = dict(p, flag="--resume", command=command,
                 pane_line=attempt_launch_line(command, p["cwd"], attempt))
        print("  session   %s wrote its first transcript before the exit; "
              "resuming it instead" % p["session"], file=out)
    print("  exited    pid%s %s PROVEN gone%s"
          % ("s"[:len(p["pids"]) != 1],
             ", ".join(str(int(x)) for x in p["pids"]),
             " — the pane never showed the exit (%s), so the exit is proven "
             "by the pid, not by the pane" % mode if by_pid else ""),
          file=out)
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
    stopped = list(bound["stopped"] or ())
    wake = _wake(p, stopped)
    print("  wake      %s" % wake["detail"], file=out)
    # THE LEDGER ROW IS WRITTEN WHETHER OR NOT EVERY SURFACE CAME BACK, and it
    # carries what was proven. A rehome that left a seat half-up is exactly the
    # event an operator needs recorded; only recording the clean ones would
    # make the ledger an optimist.
    ok, why = record({"seat": seat_name, "home": p["home"],
                      "model": p["model"], "session": p["session"],
                      "handle": handle, "command": p["command"],
                      "pane_line": p["pane_line"], "attempt": attempt,
                      "home_note": p["home_note"],
                      "permission": list(p.get("permission") or ()),
                      "permission_note": p.get("permission_note"),
                      "stopped": stopped, "wake": wake,
                      "exit_wake": mode, "exit_proven_by": proven_by,
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


def wake_text(p, stopped):
    """The one DM a rehomed seat gets: where it is now, and what the exit
    stopped that nothing brought back."""
    where = ("the default home (CLAUDE_CONFIG_DIR unset)" if p.get("default")
             else "home %s" % p["home"])
    head = ("helm seat rehome: this session was exited and relaunched on %s, "
            "resuming %s%s." % (where, p["session"],
                                " with " + " ".join(p["permission"])
                                if p.get("permission") else ""))
    if stopped:
        return ("%s The exit stopped these and nothing restarted them, so "
                "re-arm these: %s. Background shells never survive a "
                "relaunch." % (head, "; ".join(stopped)))
    return ("%s The exit showed no list of stopped tasks; background shells, "
            "monitors and scheduled tasks never survive a relaunch, so re-arm "
            "any this session held." % head)


def _wake(p, stopped):
    """{posted, detail} — the post-relaunch wake, through the seat's own DM
    lane (`seats.dm`), where it waits for the seat's next read whether or not
    the pane is back yet. An undelivered wake is a reported row, never a
    failure of the move that already happened."""
    from . import seats as seatsmod
    text = wake_text(p, stopped)
    try:
        row, err = seatsmod.dm(p["seat"], text, who=WAKE_WHO, sign=False)
    except Exception as e:                  # noqa: BLE001 — undelivered, not fatal
        row, err = None, "%s: %s" % (e.__class__.__name__, e)
    if err or not isinstance(row, dict):
        return {"posted": False,
                "detail": "the DM to %s was not written (%s): %s"
                          % (p["seat"], err or "no row", text)}
    return {"posted": True, "id": row.get("id"),
            "detail": "DM to %s naming %d stopped item%s to re-arm"
                      % (p["seat"], len(stopped), "s"[:len(stopped) != 1])}


def cmd_rehome(args):
    """seat rehome <seat> --home H|default [--model M] [--mode M] [--apply]"""
    from .cli import guard_tail
    usage = ("usage: helm seat rehome <seat> --home H|default [--model M] "
             "[--mode M] [--apply]\n"
             "  move one live seat onto a named credhome, or with `--home "
             "default` onto the Orca-synced default home: prove the home's "
             "token, exit the session cleanly, relaunch it on the home with "
             "the same session id and permission mode. Dry-run without "
             "--apply.")
    args = list(args or [])
    if not args or args[0].startswith("-"):
        print(usage, file=sys.stderr)
        return 2
    seat_name, rest = args[0], args[1:]
    rc = guard_tail("helm seat rehome", rest, flags=("--apply",),
                    valued=("--home", "--model", "--mode"), usage=usage)
    if rc is not None:
        return rc
    if "--home" not in rest:
        print("helm seat rehome: --home names the credhome to move %s onto "
              "(`helm cred list`)" % seat_name, file=sys.stderr)
        return 2
    home_name = rest[rest.index("--home") + 1]
    model = rest[rest.index("--model") + 1] if "--model" in rest else None
    mode = rest[rest.index("--mode") + 1] if "--mode" in rest else None
    apply = "--apply" in rest
    p, refusal = plan(seat_name, home_name, model=model, mode=mode)
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
