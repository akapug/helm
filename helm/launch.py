#!/usr/bin/env python3
"""helm launch — the metaharness seam (meld-launch.sh's capability, ported
onto helm's own machinery). Use it in place of a bare `claude` invocation:

    helm launch [--seat NAME] [--home HOME] [--room R] [--model M] [--no-install] [--] [claude args…]

It (1) wires the hook estate into the target home (inject + the delivery
lane's deliver/join — hooks.py's installer, idempotent) and runs the
homes.BENEFITS entries marked `launch` on each home it wires (ultracode +
Opus xhigh in settings.json, additive), (2) pre-writes the
seat's roster row so teammates can address it before the first boundary,
(3) exports one aligned chat + dregg signer identity so every surface speaks
and signs as the STABLE seat (meld's agent-join lesson: an addressable identity
must survive sessions), then (4) execs claude with the passed-through args.

Before the exec, a launch onto a named credhome (--home, or an inherited
CLAUDE_CONFIG_DIR — the pin `helm seat spawn` hands its pane) passes the ONE
Orca sync, cred.launch_sync: a home whose token is stale against Orca's managed
copy of the same account is synced from it, and a home whose identity disagrees
with Orca's execs nothing. A --home launch then ensures the home's `skills ->
hub` link (skillsync.link_canonical: created when missing, a link elsewhere
named and left alone — never a session that silently sees no skills). `--model` is carried onto claude's argv unless the
passed-through args already name one; with no model at all, the launch SAYS
which model the home's settings.json will pick instead of leaving it silent.

The fleet default needs no wrapper — `helm hooks install` covers every home
and the SessionStart hook joins each session under a derived name. launch
adds the stable NAME and the per-home pin (CLAUDE_CONFIG_DIR)."""
import os
import re
import socket
import sys

from . import home, homes, hooks, seat_launch_owner, seats

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def stable_seat(cwd=None):
    """meld-launch's derivation: <host>-<cwd-basename>, sanitized, ≤64."""
    host = socket.gethostname().split(".")[0] or "host"
    base = os.path.basename((cwd or os.getcwd()).rstrip(os.sep)) or "here"
    return _SAFE.sub("-", "%s-%s" % (host, base))[:64].strip("-") or "seat"


def parse_args(args):
    """-> (opts dict, claude_args). Everything after `--` (or the first
    unknown token) passes through verbatim."""
    opts = {"seat": None, "home": None, "room": None, "model": None,
            "install": True}
    rest, i = [], 0
    args = list(args or [])
    while i < len(args):
        a = args[i]
        if a == "--":
            rest.extend(args[i + 1:])
            break
        if a in ("--seat", "--home", "--room", "--model") and i + 1 < len(args):
            opts[a[2:]] = args[i + 1]
            i += 2
            continue
        if a == "--no-install":
            opts["install"] = False
            i += 1
            continue
        rest.extend(args[i:])
        break
    return opts, rest


def build_env(base, seat, home_path=None, room=None, room_source=None,
              allocate_scratch=True, attempt_token=None):
    """The child's env: one aligned chat/signer identity, optional credential-
    home pin, an explicit or derived project room, and the spawn ATTEMPT TOKEN
    this launch was handed (or none). Pure when `allocate_scratch` is False —
    tested without an exec.

    `attempt_token` IS PINNED, NEVER INHERITED. `cmd_launch` reads it through the
    one reader (`spawn_attempt_token`, off the `helm.seat` facade) and passes it
    here;
    a launch handed no attempt writes none into its child, even when the shell it
    runs in carries one from some other spawn. So `helm launch` is never a second
    producer of a spawn's identity: it can only carry forward the token the spawn
    that launched it minted, and the child's first SessionStart binds into a
    PENDING register exactly when it carries that spawn's token.

    `allocate_scratch=False` IS WHAT MAKES THIS A PREVIEW. Everything else here
    is a dictionary computation, but the TMPDIR default below runs the scratch
    router, which MAKES A DIRECTORY. A caller that only wants to SHOW what the
    child will carry — `helm seat spawn --print`, whose whole contract is that
    nothing is spawned, reaped, re-minted or created — must be able to read the
    identity back out of this producer without provisioning a mount for a
    process that will never start. The flag defers the allocation, it does not
    fake it: a preview simply reports no TMPDIR default, which is honest,
    because whether one gets allocated is a fact about the launch. The signer binary honors an explicitly configured canonical or
    legacy path (including an intentional empty kill switch), else uses helm's
    dregg default. Profiles never inherit: a child speaking as ``seat`` must
    sign as that same seat, not its launcher.

    The child-session stamp is stripped (seat.CHILD_STAMP_VARS): launched
    from inside a Claude session, an inherited CLAUDE_CODE_CHILD_SESSION/
    SID marks the child a subprocess and kills its transcript persistence."""
    from . import seat as _seat
    # THE PROXY TRIPLE NEVER RIDES INTO A NATIVE SEAT. This function is the
    # env for the shared owner's native-Claude child, which registers itself as
    # family=claude / backend=native — so if the launching shell belongs to
    # a PROXIED seat (every non-claude family here runs behind CLIProxyAPI but
    # inside Claude Code's harness, deliberately), an inherited
    # ANTHROPIC_BASE_URL aims the new seat at a proxy fronting another vendor.
    # It would look native, be billed native, and route elsewhere, and nothing
    # downstream reports it. Same seam seat.py uses — never a second scrubber.
    env = _seat.scrub_env(base)
    for v in _seat.CHILD_STAMP_VARS:
        env.pop(v, None)
    env["HELM_CHAT_NAME"] = seat
    if "HELM_CELL_BIN" not in env:
        env["HELM_CELL_BIN"] = (env["MELD_CELL_BIN"]
                                if "MELD_CELL_BIN" in env
                                else _seat.DREGG_SIGNER_DEFAULT)
    env["HELM_CELL_PROFILE"] = seat
    env["DREGG_PROFILE"] = seat
    # GIT'S IDENTITY IS DELIBERATELY NOT SET HERE, and this is the one plane
    # where a child does NOT speak as its seat. Everywhere else in this
    # function a child signing as `seat` is the law; git is the exception,
    # because git's author name is PUBLISHED the moment anything is pushed.
    #
    # Setting it announces the model. `GIT_AUTHOR_NAME=<seat>` renders on every
    # GitHub commit page as e.g. `helm-claude-2`, while the email still
    # resolves to the owner — so the identity is right and the displayed name
    # says a machine wrote it. That is the same disclosure a Co-Authored-By
    # trailer makes, arriving on a plane no message rung can read, which is why
    # `helm/trailer_rung.py` cannot be the whole of that rule.
    #
    # SEAT PROVENANCE IS REAL AND IT LIVES SOMEWHERE PRIVATE. Which seat made a
    # commit is genuinely unrecoverable from git alone — that is why `helm work
    # release` prints "git committer, shared across seats … not seat
    # provenance" on its triage lines. It is not unrecoverable from HELM: the
    # dispatch ledger records the author seat on every row and the gate receipt
    # records the room. The answer to that gap is helm's own ledger, never a
    # published field. Leaving both unset lets git fall back to the operator's
    # configured identity, which is what should reach GitHub.
    env["HELM_AGENT_HARNESS"] = "claude"
    env["HELM_MODEL_FAMILY"] = "claude"
    env["HELM_MODEL_BACKEND"] = "native"
    # Feedback about helm never leaves helm (task/2328): the same pair the
    # proxy families' launch line exports, on the native door — a law about
    # where a seat's feedback goes, so it is not keyed on the backend. Set,
    # not setdefault: a launching shell that inherited the opposite is not
    # an operator choice this child should honour.
    env.update(_seat.FEEDBACK_ENV)
    if "TMPDIR" not in env and allocate_scratch:
        # The mount plane's 'agents never think about it' half (scratch.py):
        # point the child's TMPDIR at a mount helm CHOSE and helm reaps, so
        # every mktemp / tempfile / git-temp inside the seat lands off a
        # capped tmpfs by default. An operator's own TMPDIR always wins;
        # HELM_SCRATCH_TMPDIR=0 opts out. Best-effort — a routing failure
        # leaves the child exactly as it was.
        try:
            from . import scratch
            tmp = scratch.launch_tmpdir()
        except Exception:
            tmp = None
        if tmp:
            env["TMPDIR"] = tmp
    if home_path:
        env[homes.ENV_VAR["claude"]] = home_path
    from .seat_role import SPAWN_ATTEMPT_ENV
    if attempt_token:
        env[SPAWN_ATTEMPT_ENV] = str(attempt_token)
    else:
        env.pop(SPAWN_ATTEMPT_ENV, None)
    for name in ("HELM_CHAT_ROOM", "MELD_CHAT_ROOM",
                 "HELM_CHAT_ROOM_SOURCE", "MELD_CHAT_ROOM_SOURCE"):
        env.pop(name, None)
    if room:
        env["HELM_CHAT_ROOM"] = room
        if room_source:
            env["HELM_CHAT_ROOM_SOURCE"] = room_source
    return env


def home_note(home_path, asked):
    """The anti-drift line: `--home cto-example` names a DIRECTORY, and a past
    `/login` may have put a different account inside it. Print who the home
    ACTUALLY holds (cred.account_of reads the content) before exec — the
    operator asked for an account, not a path. Never blocks the launch."""
    from . import cred
    verdict, acct = cred.verdict_for(home_path)
    if verdict == "UNKNOWN":
        print("[helm launch] home %s: account unreadable (%s) — launching anyway"
              % (asked, acct["error"]), file=sys.stderr)
        return
    print("[helm launch] home %s HOLDS %s%s"
          % (asked, acct["email"],
             "  ** DRIFT: this dir's name promises another account; that account's "
             "home is %s (`helm cred list`) **" % homes.canonical_name(acct["email"])
             if verdict == "DRIFT" else ""), file=sys.stderr)


_SKILLS_LINE = {
    "linked": "skills linked: %s/skills -> %s",
    "indirect": "%s/skills -> %s resolves to the hub through another path — "
                "left untouched; `helm skills sync --apply` makes it direct",
    "unavailable": "skills hub UNAVAILABLE for %s (%s) — this session sees NO "
                   "helm skills; restore the hub, then `helm skills sync --apply`",
    "foreign": "%s/skills -> %s is NOT the skills hub — left untouched; "
               "sessions on this home see THAT target's skills until "
               "`helm skills sync --apply` normalizes it",
    "real": "%s/skills is a REAL dir (%s) — left untouched; `helm skills sync "
            "--apply` folds it into the hub",
    "error": "skills NOT linked into %s (%s) — this session sees no helm "
             "skills; `helm skills sync --apply` repairs it",
}


def link_skills(home_path, shown, out=None):
    """A launch onto a named credhome ensures `skills -> hub` before the exec,
    through skillsync.link_canonical — the same primitive that links a seat
    config dir at mint and a credhome at `helm homes prepare`. Idempotent and
    never destructive: an already-correct link is silent, a missing one is
    created and said, a link ELSEWHERE is named and left alone (the operator
    may have pointed it deliberately; `helm skills sync --apply` is the
    normalizer), a REAL dir is left whole. No hub configured is silent; a
    hub configured but UNAVAILABLE is said with its reason. A refused link is
    loud, never fatal — the session still launches, told what it will not
    see."""
    from . import skillsync
    res = skillsync.link_canonical(home_path, relink=False)
    action, detail = res.action, res.detail
    if action in _SKILLS_LINE:
        print("[helm launch] " + _SKILLS_LINE[action] % (shown, detail),
              file=out or sys.stderr)
    for line in (skillsync.failure_line(res), skillsync.degraded_line(res)):
        if line:
            print("[helm launch] %s: %s" % (shown, line), file=out or sys.stderr)
    return action


def _names_model(claude_args):
    return any(a == "--model" or a.startswith("--model=") for a in claude_args)


def carry_model(model, claude_args, config_home):
    """claude's argv with the seat's model carried, plus the one line that says
    where the model comes from. An explicit --model among the passed-through
    args wins over the launch's own; with neither, the home's settings.json
    decides, and the line names that value (read, never written) so a seat that
    lands on a settings default is visible at launch rather than discovered."""
    if _names_model(claude_args):
        return list(claude_args), None
    if model:
        return ["--model", model] + list(claude_args), \
            "[helm launch] model %s (carried onto claude's argv)" % model
    from . import pk
    path = os.path.join(config_home, "settings.json")
    try:
        settings = pk.read_json(path)
    except Exception:
        settings = None
    picked = settings.get("model") if isinstance(settings, dict) else None
    if isinstance(picked, str) and picked:
        return list(claude_args), ("[helm launch] no --model given: claude takes "
                                   "model %r from %s (not changed by helm)"
                                   % (picked, path))
    return list(claude_args), None


def cmd_launch(args):
    """launch [--seat S] [--home H] [--room R] [--model M] [--no-install] [--] [args…]"""
    opts, claude_args = parse_args(args)
    home_path = None
    if opts["home"]:
        targets, err = hooks._select_homes(opts["home"])
        if err:
            print("helm launch: " + err, file=sys.stderr)
            return 1
        home_path = targets[0][1]
        home_note(home_path, opts["home"])
    # seats.resolve_homing is THE one precedence (CLI --room > env seam >
    # project derivation) — launch never re-derives its own copy. safe_cwd:
    # a deleted process cwd must not crash the launch seam (eager-getcwd
    # class), it just launches un-homed — hoisted above the seat default
    # because stable_seat() derives from cwd too.
    cwd = seats.safe_cwd()
    # home.chat_name / home.validate_seat_arg are THE two validated seams: a
    # hostile name from EITHER the --seat CLI arg or the HELM_CHAT_NAME env is
    # rejected (home.SeatNameError) here rather than exported to the child as
    # its identity or written as a roster key.
    seat = home.validate_seat_arg(opts["seat"])
    inherited = None if seat else home.chat_name()
    seat = seat or inherited or stable_seat(cwd or "here")
    if inherited:
        # SAY WHAT THE MIDDLE RUNG DID (identity refusal set D, 2026-08-02):
        # this names the child after the LAUNCHER's exported HELM_CHAT_NAME —
        # an exported name outlives the pane it named, which is the exact
        # contagion vector of the integrator-name hijack. Kept for the
        # deliberate relaunch-myself shape, but never again silently.
        print("[helm launch] seat %r INHERITED from this shell's "
              "HELM_CHAT_NAME — an exported name outlives the pane it named; "
              "pass --seat to name the child deliberately" % seat,
              file=sys.stderr)
    room, source = seats.resolve_homing(opts["room"], cwd)
    room_source = "derived" if source == "derived" else None
    room_explicit = source == "explicit"
    if opts["install"]:
        for name, path in ([(opts["home"], home_path)] if home_path
                           else hooks.claude_homes()):
            res = hooks.install_home(path)
            action, detail = res
            if action == "fail":
                print("helm launch: hook install failed in %s: %s"
                      % (name, detail), file=sys.stderr)
            elif res.shortened:
                # REFUSE THE LAUNCH — do not warn and proceed. `action ==
                # "fail"` alone was the whole check here, and a shortened write
                # is not a FAILED write: it succeeds at writing less than the
                # contract. Warning about it and then joining and exec'ing
                # anyway put a session on the box running WITHOUT a required
                # guard, with the warning already scrolled past — which is the
                # unguarded seat this whole lane exists to prevent, announced
                # instead of stopped. The session that must not exist is the
                # one we decline to start.
                print("helm launch: REFUSED — %s\nhelm launch: no session was "
                      "started; install the guard or pin it, then relaunch"
                      % res.note("hook install in %s" % name), file=sys.stderr)
                return 1
            # THE HOME LIST'S LAUNCH ENTRIES, through homes.provision and not a
            # writer of launch's own: a home made outside `helm homes prepare`
            # still gets what every Opus seat must carry (ultracode + xhigh)
            # the first time a launch wires it. A pure read once it is there.
            notes, err = homes.provision(path, at_launch=True)
            for line in notes + ([err] if err else []):
                print("[helm launch] %s: %s" % (name, line), file=sys.stderr)
    # THE ONE ORCA SYNC, before anything durable names this session. The home
    # the child will run on is the --home pin or the inherited
    # CLAUDE_CONFIG_DIR a spawn put in the pane command; cred.launch_sync
    # ignores anything that is not a named credhome and refuses (False) only
    # when the home's identity disagrees with Orca's copy of it.
    config_home = home_path or os.environ.get(homes.ENV_VAR["claude"]) \
        or homes.DEFAULTS["claude"]
    from . import cred
    if not cred.launch_sync(config_home, opts["home"] or config_home):
        print("helm launch: REFUSED — no session was started", file=sys.stderr)
        return 1
    if home_path:
        link_skills(home_path, opts["home"])
    claude_args, model_note = carry_model(opts["model"], claude_args, config_home)
    if model_note:
        print(model_note, file=sys.stderr)
    # THE ATTEMPT THIS LAUNCH IS THE CHILD OF, read through the one reader and
    # pinned into claude's env — never minted here. A `helm seat spawn` threads
    # its attempt token into this command; a manual `helm launch --seat` typed
    # in another pane arrives with none, and its SessionStart is then refused by
    # a PENDING register it did not come from, with that attempt named.
    #
    # THROUGH THE FACADE, not through the impl module that happens to define
    # it. `helm.seat` is the one door onto the seat family's implementation;
    # reaching past it into `seat_lifecycle_runtime` is exactly the coupling
    # `tests/test_seat_facade_injection.py` refuses, and an exemption for this
    # one caller would buy the coupling back for the price of a line in an
    # allowlist. The facade re-exports the same one reader, so the value is
    # identical and only the import path changes.
    from .seat import spawn_attempt_token
    attempt_token = spawn_attempt_token()
    if attempt_token:
        print("[helm launch] spawn attempt %s — this pane is that spawn's "
              "child; its SessionStart binds into that attempt's register"
              % attempt_token, file=sys.stderr)
    seats.join(cwd=cwd, seat=seat, room=room or "main",
               room_explicit=room_explicit, room_source=room_source,
               runtime={"agent_harness": "claude", "family": "claude",
                        "backend": "native"})
    env = build_env(os.environ, seat, home_path, room, room_source,
                    attempt_token=attempt_token)
    print("[helm launch] seat '%s'%s — exec claude" % (
        seat, (" home " + opts["home"]) if opts["home"] else ""), file=sys.stderr)
    return seat_launch_owner.exec_attached(["claude"] + claude_args, env)
