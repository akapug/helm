#!/usr/bin/env python3
"""helm hooks — the self-closing installer for the crown-jewel wiring: every
claude credential home gets the UserPromptSubmit hook that pipes each turn's
FULL hook JSON to `helm inject --hook-json` (which derives the project scope
from the turn's cwd). Hand-wiring one home at a time was the adoption gap;
`install` closes it, `status` + doctor keep it closed.

The estate is TWO launch surfaces with ONE hook contract: the claude credential
homes (~/.claude-homes/*, ~/.claude) and the multimodel SEAT config dirs
(<helm_home>/_global/seats/<family>/claude and slice-6 instances
<family>/instances/<seat>/claude — seat.py's isolated CLAUDE_CONFIG_DIRs) both
get the full spec set. That includes inject, delivery, shell-argv and idle gates,
and both handoff producers. A launched codex/kimi/… seat therefore receives
fleet chat under its family name (seat.py exports HELM_CHAT_NAME=<family> on
launch; seats.py derive_seat keys the roster on it), cannot idle past a NEW row,
and produces the handoff artifact its installed resume-turn consumer reads.
The inbox bound remains an enumeration rather than a sentence anyone can
restate loosely: it blocks once per pending-fingerprint (a re-stop on the SAME
rows passes), and it never runs at all across a stop-active continuation — the
harness is already continuing off a stop hook, so a row landing mid-turn waits
for the next REAL stop. Two named kill switches disable it outright. `seats.py
stop_guard` is the authority and tests/test_delivery_promise_escapes is the
COMPLETE escape set; a fifth path is a finding against that enumeration.
`install` covers both surfaces; `status` reports coverage for both.

Laws:
  * MERGE-preserving: existing settings keys and foreign hook entries are
    NEVER clobbered; re-install is idempotent (an up-to-date entry reports ok);
    a stale helm entry (old path/old style) is updated in place, not doubled.
    A seat carries its own settings (model, permissions); the install only ever
    adds/updates helm's own delivery entries, never touches the rest.
  * Safety rails are configs.py's: backup -> validate -> atomic write; the file
    is re-parsed AFTER the write and the backup restored on any failure. A bad
    install must never brick a home's (or a seat's) launch. (configs' write
    gate — _classify._is_seat_home — recognizes the seat claude dirs, family
    AND slice-6 instance shapes, so the same gated path accepts them.)
  * FAIL-OPEN generated text (docs/HOOKS.md law): `timeout` + `|| true` — a
    missing or wedged helm injects nothing, never blocks a turn. ONE EXCEPTION,
    and it was a live bug for the gate's whole life: a spec marked `gate: True`
    (stop-guard) omits `|| true` and instead propagates ONLY rc 2, because
    `|| true` rewrote its deliberate BLOCK to success and the owner had
    therefore "never once seen them actually fire". Fail-open is right for a
    lane and fatal for a gate; see spec_command.
  * codex: docs/HOOKS.md carries no mechanical notify-hook recipe yet —
    reported honestly as pending, never guessed at.
"""
import difflib
import errno
import glob
import json
import os
import re
import shlex
import shutil
import stat
import sys
import time

# `home` ALONE AT MODULE SCOPE. This module is imported by cli._main on EVERY
# helm invocation -- the one door that decides whether a fleet-scoped hook
# skips this project -- so what it imports is charged to every per-tool-call
# hook on every seat. `configs` (26 ms of CPU: pathlib, shutil, json, pk) and
# `homes` are read only by the install/census verbs, and `pk` only by the
# status and scope rows; none of them is on the hook-entry path. They are
# imported where they are used, which `wiring.graph` reads as the same edge.
from . import home, hookalarm

HOOK_EVENT = "UserPromptSubmit"
# INJECT IS NOT ~ms. Measured on the live tree with
# `/usr/bin/time -f 'wall=%e user=%U sys=%S cpu=%P' helm inject --hook-json`:
# wall 0.56-0.76s at 98% CPU, i.e. ~0.6s of CPU and essentially no blocking IO.
# That matters because this budget is enforced by `timeout` as WALL time on a
# SHARED box, so what it actually measures is how oversubscribed the box is,
# not what the hook costs: at load 34 on 8 cores the same work costs seconds,
# and every seat's hook crosses the line in the SAME second. The live evidence
# is fleet-wide bursts, not one slow seat — four kills inside a single
# 10-minute alarm window, alongside a surviving fire-ledger row whose
# construction leg alone took 9366ms at that instant. A kill here is TOTAL:
# helm renders at the end, so a TERM'd process emits nothing and the seat
# silently loses brief, pinned rules, whispers and reflexes for that turn.
# 10s is therefore ~16x headroom over the idle cost and roughly one burst of
# headroom over the loaded one. Moving it is a real decision with a real cost
# on the other side (it is the never-hold-a-turn ceiling and the owner waits
# on it), so it stays until someone owns that trade with evidence.
TIMEOUT_S = 10

# The one guard helm does not SHIP. It is OPTIONAL until a host configures it
# (the executable on PATH, or HELM_SUITE_GUARD set) and REQUIRED from then on:
# a host that never installed it has nothing broken, while a configured guard
# that stops resolving is a real gap (see `optional_unconfigured`). Named here,
# beside the specs, because a required guard named nowhere cannot be counted:
# hand-wired into one config and hand-copied into others, it left the seats
# minted afterwards unguarded while `hooks status` printed "N of N seats
# covered (full hook contract)". A one-time patch is not a contract. RESOLVED
# BY NAME, never by a literal path — helm's own pre-push
# hostpath_guard refuses a `/home/<user>/` literal in a pushed blob, and this
# estate is not the only estate.
SUITE_GUARD_BIN = "fab-suite-pretooluse"

# The hook estate — one spec per event helm wires. inject is the crown jewel
# (per-turn context); deliver + join are the meld-half's delivery lane
# (tool-boundary chat nudge + session autojoin — seats.py); handoff-precompact
# + handoff-sessionend are the compaction-continuity contract (handoff.py's
# `check --hook-json` rides both triggers — it captures the now-snapshot AND
# nags when no handoff artifact exists, so the next window never starts blind).
# Same laws for every spec: merge-preserving, fail-open text, idempotent. `own`
# markers identify OUR entry in a settings file (so record.py's PostToolUse hook
# and any foreign entry are never touched); matcher rides events that take one —
# the continuity specs OMIT it (matcher=None) so they fire on EVERY compaction
# and EVERY session end, never trigger-gated (the safety-net's whole point).
SPECS = (
    {"name": "inject", "event": HOOK_EVENT, "args": "inject --hook-json",
     "timeout": TIMEOUT_S, "own": ("inject --hook-json", "helm inject"),
     "matcher": None},
    {"name": "deliver", "event": "PostToolUse", "args": "chat deliver --hook-json",
     "timeout": 2, "own": ("chat deliver --hook-json",), "matcher": "*", "scope": "helm"},
    # A PostToolUse subagent record is interval proof only while that logical
    # agent remains active. Remove its exact agent_id entry at lifecycle end;
    # otherwise the long-lived parent Claude pid would preserve stale proof.
    {"name": "delegation-stop", "event": "SubagentStop",
     "args": "chat delegation-stop --hook-json", "timeout": 2,
     "own": ("chat delegation-stop --hook-json",), "matcher": "*", "scope": "helm"},
    # saguide: the SubagentStart INITIAL-PHYSICS seam. UserPromptSubmit fires
    # for a real user turn and NOT for the internal prompt handed to a subagent,
    # so before this spec every SA ran with no premises, no moves, nothing —
    # which made the owner's "all TLAs AND SAs reliably understand" exit clause
    # mechanically unreachable however the store was worded.
    #
    # IT IS ITS OWN VERB AND NOT `inject --hook-json`, ON PURPOSE. The harness
    # DISCARDS a hookSpecificOutput whose hookEventName is not the firing event,
    # silently, at exit 0 — so borrowing inject's UserPromptSubmit envelope here
    # would install green and deliver nothing. helm/saguide.py mints
    # hookEventName at the source; see its docstring for the two other silent
    # drops on this path.
    #
    # UNSCOPED (task/2987): the payload is scoped inside saguide.payload, so a
    # subagent in another project hears that project's nested-spawn reflex
    # and nothing of helm's physics.
    {"name": "saguide", "event": "SubagentStart",
     "args": "saguide --hook-json", "timeout": 5,
     "own": ("saguide --hook-json",), "matcher": "*",
     "covers": "sa-initial-context"},
    {"name": "join", "event": "SessionStart", "args": "chat join --hook-json",
     "timeout": 5, "own": ("chat join --hook-json",), "matcher": "*", "scope": "helm"},
    # resume-turn: the RESUME LEG of a compaction (resumeturn.py). It shares
    # SessionStart with join and gates INTERNALLY on source == "compact",
    # rather than riding a `"matcher": "compact"` group: the wildcard group is
    # the shape proven live across this whole estate, and a source gate the
    # verb owns cannot be silently mis-installed into a group that never fires.
    {"name": "resume-turn", "event": "SessionStart",
     "args": "seat resume-turn --hook-json", "timeout": 5,
     "own": ("seat resume-turn --hook-json",), "matcher": "*"},
    # stop-guard: the IDLE GATE (the predecessors' arbiter capability). Blocks a stop
    # on undelivered mentions/held leases (once per pending-fingerprint),
    # warns to arm the beacon on a clean stop, silently runs the index cap.
    # Stop takes no matcher (like UserPromptSubmit). Its timeout is the
    # owner-bound P0 hook budget, not an install-time tuning default: keep it
    # here so `helm hooks install` cannot silently widen the checked-verdict SLA.
    # The NUMBER is derived below, not asserted here — a second copy of it in
    # prose is what let this comment say five while the spec said twenty.
    # gate=True — the ONE spec whose refusal must reach the harness. See
    # spec_command: `|| true` would swallow the exit-2 block, which is what
    # disarmed this gate for its entire life.
    # THE BUDGET IS 20s BECAUSE THE GUARD MEASURES 6.3s, and it was 5s because
    # nobody re-derived it as the ledgers grew. MEASURED 2026-09-09 on this
    # box, one real stop with four held leases: 7.70s before the
    # `_binding_rows` memo and 6.27s after, against a 5s budget, with three
    # back-to-back runs of the exact hook command returning rc=2 at 8.7s,
    # 8.1s and 5.9s. rc 2 is the BLOCK code — the guard had a live refusal to
    # make every time and its own clock killed it first, so a stop that should
    # have been refused was announced as ALLOWED and UNCHECKED.
    #
    # THIS DOES NOT MAKE A STOP SLOWER. The guard already spends that time; at
    # 5s the seat paid the full budget AND got no answer. What changes is that
    # the work already being done now reaches the harness.
    #
    # AND IT DOES NOT TOUCH THE FAIL-OPEN LAW one line below, which is
    # deliberate and was settled after a measured fleet-wide outage: a timeout
    # still exits 0 and still says the stop went unchecked. A budget that the
    # guard fits and a timeout that refuses are different questions, and only
    # the first one was ever wrong.
    #
    # 20 RATHER THAN 8, because the dominant remaining cost is the dispatch
    # fold and it is O(ledger rows) on a ledger that grows every day. A budget
    # sized to today's measurement is the same mistake as the 5 it replaces;
    # this one has room for the ledger to roughly triple before anyone must
    # think about it again. `_gate_outer_timeout` derives Claude's deadline
    # from this number, so the harness grace follows it.
    {"name": "stop-guard", "event": "Stop", "args": "chat stop-guard --hook-json",
     "timeout": 20, "own": ("chat stop-guard --hook-json",), "matcher": None,
     "gate": True, "scope": "helm"},
    # argv-guard: the shell-substitution gate. A chat/dispatch body composed
    # as a double-quoted shell argument lets the SHELL execute backticked
    # content before helm exists — measured three times in two days, once
    # running `git clean` in the shared checkout from inside the message
    # warning about it. Helm cannot see consumed backticks; this hook reads
    # the Bash command BEFORE any shell runs, the one place they are visible.
    # Also covers `git commit/tag -m` messages — the same hazard one surface
    # out (a landed commit read "the assignment test is , and" after the
    # shell ate its backticked phrase, 2026-07-29).
    # MONITOR JOINS BASH for the sidechain rung (task/2542): a subagent that
    # arms, replaces or stops its seat's `helm chat wait --follow` beacon takes
    # the seat's wake route, and a seat arms that beacon with a Monitor call,
    # which a Bash-only group never showed to any helm hook. Claude Code reads
    # the matcher as a pattern, so "Bash|Monitor" fires for exactly those two
    # tools; a Monitor call pays for this hook, an ordinary Read does not.
    # WRITE AND EDIT JOIN for the GitHub-Actions rung (task/2566): the owner
    # rule that CI never runs on GitHub-hosted runners was a store premise
    # and a whisper, and a seat enabled Actions unguarded. A workflow file
    # lands through the Write tool as often as through a shell redirection,
    # and a Bash-and-Monitor group never showed a Write to any helm hook.
    # AGENT JOINS for the agent-model rung, on the owner's ruling that the
    # Agent tool's `model` argument never changes the model a subagent runs
    # on: the subagent runs as the launching seat's model whatever the flag
    # says. A flag ignored in silence is the expensive failure, because the
    # caller then routes, budgets and reports as if another model did the
    # work, so the rung refuses ANY non-empty model and names the doors that
    # do reach one (the Workflow tool's per-agent model, or a seat launched on
    # it). The rung is seeded from a project lead's standalone
    # block-agent-model-flag.sh, which refused the same flag from one home;
    # here it rides the one spec every home and seat already carries, so it
    # holds fleet-wide, and argv-guard stays unscoped for that reason. An
    # Agent call is judged on its model key alone and admitted at once
    # otherwise. The matcher is a list of exact tool names, so the Workflow
    # tool, whose script spawns its agents without an Agent tool call, is
    # not matched.
    {"name": "argv-guard", "event": "PreToolUse",
     "args": "chat argv-guard --hook-json", "timeout": 2,
     "own": ("chat argv-guard --hook-json",),
     "matcher": "Bash|Monitor|Write|Edit|Agent", "gate": True},
    # continuity: the compaction/session-end handoff contract (sessions lane).
    {"name": "handoff-precompact", "event": "PreCompact",
     "args": "handoff check --hook-json", "timeout": 5,
     "own": ("handoff check --hook-json",), "matcher": None},
    {"name": "handoff-sessionend", "event": "SessionEnd",
     "args": "handoff check --hook-json", "timeout": 5,
     "own": ("handoff check --hook-json",), "matcher": None},
    # suite-guard: the LOCAL-COMPUTE gate — the one spec whose executable helm
    # does not ship (see SUITE_GUARD_BIN). It reads the Bash command string
    # BEFORE any interpreter resolves, which is the layer a PATH shim
    # structurally cannot reach: `/home/linuxbrew/.linuxbrew/bin/python3 -m
    # unittest …` never touches ~/.local/bin/python3. ~10 measured incidents of
    # agent-launched local compute made the owner's daily driver unusable
    # (roguescan.py's header is the box-layer complement to this config layer).
    #
    # gate=True for the same reason the other two are: it returns 2 to BLOCK,
    # and `|| true` would rewrite that refusal to success.
    # optional=True: a host with no such executable and no pin installs every
    # other hook and reports this one "not configured (optional)".
    {"name": "suite-guard", "event": "PreToolUse", "args": "",
     "timeout": 3, "own": (SUITE_GUARD_BIN,), "matcher": "Bash",
     "gate": True, "external": SUITE_GUARD_BIN, "optional": True},
)

# A seat is a full Claude Code launch surface, not a delivery-only client. Its
# isolated CLAUDE_CONFIG_DIR therefore receives the SAME complete hook contract
# as a credential home: inject at turn start, delivery + lifecycle gates, both
# handoff producers, and resume-turn. The old delivery-only split universally
# installed the resume consumer while omitting the artifact-producing
# PreCompact/SessionEnd triggers, and kept per-turn config injection from every
# family seat. One canonical tuple prevents those two surfaces drifting again.
def fleet_scoped_args():
    """The `args` string of every spec whose verb is helm-fleet coordination.

    THESE HOOKS ARE INSTALLED PER CLAUDE HOME, NOT PER PROJECT, so without a
    scope check they run in EVERY project on the machine. Measured 2026-08-13:
    agents working in two other projects were enrolled as helm seats by
    `chat join` and then fed helm fleet DMs by `chat deliver`; one reported an
    unlanded-work nag naming a helm seat it had never heard of. Owner's words:
    they should "only affect helm agents if they are about helm-building".

    THE AXIS IS HELM'S DEVELOPMENT APPARATUS vs HELM AS A TOOL, owner-stated
    2026-08-13: "anything that only the agents building and dogfooding helm for
    the purposes of building helm more needed should be restricted to helm
    cwds, while helm remains useful (and not distracting as if they were a
    helm-building agent) for all other local agents." Only the four hooks that
    make a session a FLEET SEAT are scoped.

    NOT SCOPED, DELIBERATELY, and three of these were over-scoped in the first
    cut of this function — a regression measured the same day. Every one is
    already project-scoped INTERNALLY, so it serves whatever project it runs
    in rather than helm's:
      inject             the per-turn store/lexicon plumbing; ~/.helm/<project>/
                         carries a full category set per project (lexicon,
                         premises, heuristics, journal) and other projects use it
      handoff-precompact
      handoff-sessionend the compaction-continuity contract, which writes to the
                         PROJECT shelf ~/.helm/<project>/journal/ — another
                         project already had an entry there, so scoping these to helm
                         broke continuity for every other project
      resume-turn        the handoff CONSUMER; its generic line tells any agent
                         to run `helm handoff check` and re-ground
      argv-guard         a shell-substitution gate about THIS MACHINE, not helm,
                         and the agent-model rung, whose fact holds on every
                         seat in every project
      saguide            the SubagentStart seam: helm's brief goes to a helm
                         cwd only, and any other project's subagent hears that
                         project's own nested-spawn reflex"""
    return tuple(s["args"] for s in SPECS if s.get("scope") == "helm")


def hook_skips_here(verb, rest):
    """True when this invocation is a fleet-scoped HOOK outside helm's project.

    THE SCOPE DECISION LIVES HERE, NOT IN THE GENERATED SHELL STRING, and that
    placement is the whole point. A `case "$PWD"` prefix on the command was
    tried first and was strictly worse: it ran BEFORE the fail-open alarm arms
    (so `THE GUARD IS MISSING` / `TIMED OUT` could never reach the harness —
    re-creating the silent-guard shape that caused the 2026-08-04 outage), and
    it broke `envtidy._helm_args`, so helm could no longer identify its own
    hooks and the estate census read them as MISSING.

    ONLY HOOK INVOCATIONS ARE GATED. `--hook-json` is the discriminator: a
    human running `helm chat deliver` by hand is never silently skipped.

    PROJECT IDENTITY, NOT PATH SHAPE. `inject._ledger.project_for_cwd` is the
    ONE derivation helm already scopes with (registry longest-prefix plus the
    lane-worktree convention), so `<root>-wt/<lane>` correctly reads `helm` —
    the exact case a hand-written path prefix got wrong, going silent in the
    rooms where lane work happens. helm's own name is derived from its
    checkout, never spelled as a literal.

    FAIL-OPEN ON TROUBLE ONLY. A resolver that RAISES means "cannot tell", and
    a hook that might be needed runs. A resolver that ANSWERS a different
    project is a determinate no — that is not trouble, and skipping is right.

    ONE EXCEPTION, the Orca door: the join of a pane Orca opened is never
    skipped (see `orca_admits`)."""
    if "--hook-json" not in rest:
        return False
    args = " ".join([verb] + [a for a in rest])
    if not any(args.startswith(sa) for sa in fleet_scoped_args()):
        return False
    if orca_admits(args):
        return False
    return outside_helm() is True


# THE ORCA DOOR, and it opens for JOIN ONLY. The fleet-seat hooks are scoped
# to helm's project (see `fleet_scoped_args`), but an agent loaded in Orca is
# part of helm automatically, whatever its project (task/2673 PART C). A pane
# Orca opened carries ORCA_PANE_KEY in the environment every hook inherits,
# so that variable is the door. Deliver, stop-guard and delegation-stop stay scoped:
# enrolment makes a pane addressable, and it does not make its project a
# helm-building one.
ORCA_JOIN = "chat join --hook-json"


def orca_admits(args):
    """True when this hook invocation is the SessionStart join of a pane Orca
    opened. `args` is the joined verb string `hook_skips_here` builds."""
    return args.startswith(ORCA_JOIN) and bool(os.environ.get("ORCA_PANE_KEY"))


def outside_helm():
    """True when the cwd resolves to a project that is not helm's, False when
    it is helm's (or helm itself is unregistered), None when it cannot tell.

    FAIL-OPEN ON TROUBLE ONLY. A resolver that RAISES means "cannot tell", and
    a hook that might be needed runs. A resolver that ANSWERS a different
    project is a determinate yes."""
    try:
        from .inject._ledger import project_for_cwd
        here = project_for_cwd(os.getcwd())
        mine = project_for_cwd(
            os.path.dirname(os.path.dirname(os.path.abspath(helm_bin()))))
    except Exception:
        return None
    if mine is None:
        return False
    return here != mine


SEAT_SPECS = SPECS

# Compatibility for callers written against the old policy name. This is no
# longer a subset: new code must say SEAT_SPECS so the full contract is explicit.
DELIVERY_SPECS = SEAT_SPECS


def external_env(spec):
    """The ONE env var name that pins an external spec's executable.

    READ WITH `os.environ.get`, NEVER `home.env`, and this is a contract rather
    than a style note. `home.env` silently falls back HELM_X -> MELD_X, so a
    variable this code never names — MELD_SUITE_GUARD — was ACCEPTED, and worse:
    a dead legacy value there MASKED a perfectly good PATH resolution, turning a
    healthy host into an unguarded one on the strength of a name nobody wrote.
    An external guard is a security boundary; its pin has exactly one spelling.
    """
    return "HELM_" + spec["name"].upper().replace("-", "_")


def external_pin(spec):
    """The raw pin as configured, or None. The single read point."""
    return os.environ.get(external_env(spec))


# THE THREE LAUNCH ANSWERS. Two of them are not the same as "fine", and the
# previous shape had no way to say so: this function returned a string for a
# gap and None for everything else, so "helm measured this and it launches",
# "an env-form shebang helm deliberately refuses to judge" and "a relative
# interpreter only the exec's cwd can resolve" all arrived at the ONE consumer
# as the same falsy value, and that consumer rendered it "ok". A file nobody
# measured was reported as a proven-launchable guard. The verdict is now a
# named constant a caller cannot silently coerce.
LAUNCH_OK = "launch-ok"              # measured: the kernel can start this file
LAUNCH_GAP = "launch-gap"            # measured contradiction; the guard is dead
LAUNCH_UNJUDGED = "launch-unjudged"  # helm could not look; never ok, never blame

# The reason keys a GAP carries. They are separate because their CURES are
# separate hands, and a message naming the wrong one costs the reader the trip.
LAUNCH_INTERPRETER_MISSING = "interpreter-missing"          # install it
LAUNCH_INTERPRETER_NOT_EXEC = "interpreter-not-executable"  # chmod it
LAUNCH_SHEBANG_UNLAUNCHABLE = "shebang-unlaunchable"        # rewrite the #! line

_UNKNOWN_LAUNCH = "#! line is unreadable, so launchability is UNKNOWN: %s"

# An interpreter may itself be a script whose own interpreter is gone. The
# kernel walks that chain and so must this, or a two-hop break reads as fine.
# Bounded because the chain is not guaranteed acyclic on a hostile filesystem;
# past the bound helm stops claiming rather than loops.
_LAUNCH_CHAIN_MAX = 4


def _interpreter_launch(path, _depth=0):
    """(verdict, reason_key, detail) for whether the kernel can start `path`.

    verdict is LAUNCH_OK, LAUNCH_GAP or LAUNCH_UNJUDGED — THREE states, and the
    third is the whole point. A caller that can only ask "was there a gap"
    turns "I could not look" into "it is fine", which is how an unmeasured
    interpreter came to be counted as a provisioned guard on every seat.

    EXECUTABLE IS NOT LAUNCHABLE: a mode-0755 script whose interpreter is gone
    passes isfile and X_OK, so external_status called it OK, provisioning wrote
    it into every config, doctor counted every seat guarded — and the kernel
    refuses the exec at the only moment that matters. MEASURED: `timeout 3
    <guard>` exits 127 with one stderr line nothing reads, the estate's
    rc-2-only wrapper turns that into ALLOW, and helm's own gate template hits
    its 127 arm — whose alarm names the WRONG cure ("nothing executable at
    <path>… reinstall") for a file sitting right there with its mode bits on.
    chmod and install-the-interpreter are different hands, so this never folds
    into pin-dead or absent, and no longer folds them into each other either.
    """
    if _depth >= _LAUNCH_CHAIN_MAX:
        return (LAUNCH_UNJUDGED, LAUNCH_UNJUDGED,
                "#! chain is deeper than %d hops; helm stopped rather than "
                "follow it further" % _LAUNCH_CHAIN_MAX)
    try:
        with open(path, "rb") as f:
            line = f.readline(256)
    except OSError as exc:
        # UNREADABLE IS NOT LAUNCHABLE-OK, AND IT IS NOT A GAP EITHER. Reporting
        # no-gap here rendered a file nobody could read as fit to run; reporting
        # a gap accuses a file on missing evidence. It is its own answer.
        return (LAUNCH_UNJUDGED, LAUNCH_UNJUDGED, _UNKNOWN_LAUNCH % exc)
    if not line.startswith(b"#!"):
        # NO SHEBANG IS NOT AUTOMATICALLY LAUNCHABLE (a second review addendum
        # on the ownership-parser fix "the ownership parser read prose as commands, five ways" — by SUBJECT, since that sha is lane-local and dead to any reader outside the lane that made it). This branch used to certify every non-script as OK on
        # the strength of its mode bits — the exact over-claim the tri-state
        # was built to end, wearing a different shape one branch away. A
        # DYNAMIC ELF names its loader in PT_INTERP, and if that path is gone
        # the kernel refuses the exec with ENOENT and the caller sees 127,
        # while every readable field looks healthy.
        return _elf_launch(path)
    # THE KERNEL DOES NOT STRIP \r, AND WE USED TO. A CRLF script gives the
    # kernel an interpreter whose name ends in a carriage return, so the exec
    # fails with ENOENT while every readable field looks right — measured:
    # a `#!/bin/sh\r` file with /bin/sh present is refused as "No such file
    # or directory" and this function answered no-gap. Strip the newline
    # ONLY, then judge what the kernel will actually see.
    raw = line.split(b"\n", 1)[0]
    if raw.endswith(b"\r"):
        return (LAUNCH_GAP, LAUNCH_SHEBANG_UNLAUNCHABLE,
                "#! line ends in CRLF; the kernel keeps the carriage return "
                "in the interpreter name and the exec fails")
    parts = raw[2:].strip().decode("utf-8", "replace").split()
    if not parts:
        return (LAUNCH_GAP, LAUNCH_SHEBANG_UNLAUNCHABLE,
                "#! line names no interpreter")
    interp = parts[0]
    if os.path.basename(interp) == "env":
        # AN env-FORM SHEBANG SPLITS INTO TWO QUESTIONS, and the old code asked
        # neither. `env` resolves its TARGET against the PATH of the process
        # that execs it, and a hook runs under a PATH this function cannot see
        # — so judging the target is wrong in both directions and stays
        # UNJUDGED. But `env` ITSELF is the file the kernel opens, and when the
        # shebang spells it absolutely that IS measurable: a missing or
        # non-executable /usr/bin/env is a measured contradiction, not an
        # absence, and the exec fails before any PATH lookup happens.
        target = next((p for p in parts[1:] if not p.startswith("-")), None)
        if target is None:
            return (LAUNCH_GAP, LAUNCH_SHEBANG_UNLAUNCHABLE,
                    "#! names env with no interpreter")
        if os.path.isabs(interp):
            verdict, key, detail = _launch_of_named_interpreter(interp, _depth)
            if verdict != LAUNCH_OK:
                return (verdict, key, detail)
        return (LAUNCH_UNJUDGED, LAUNCH_UNJUDGED,
                "#! runs %s through env, which resolves it against the PATH of "
                "whatever execs the hook — a PATH helm cannot see from here"
                % target)
    if not os.path.isabs(interp):
        # A RELATIVE INTERPRETER IS UNJUDGED. The kernel resolves it against
        # the CWD OF THE EXEC, which this function cannot know — so calling
        # it missing accuses a script that may run perfectly, and calling it
        # present asserts a lookup nobody performed. Same law as the env
        # form: no verdict rather than a guess.
        return (LAUNCH_UNJUDGED, LAUNCH_UNJUDGED,
                "#! interpreter %s is relative, so only the cwd of the exec "
                "resolves it and helm cannot know that cwd" % interp)
    return _launch_of_named_interpreter(interp, _depth)


def _elf_launch(path):
    """(verdict, reason_key, detail) for a file with no `#!` line.

    THREE ANSWERS, AND ONLY ONE OF THEM IS OK. A dynamic ELF carries a
    PT_INTERP program header naming its loader; if that path is missing the
    exec fails before a single instruction runs. A static ELF has no PT_INTERP
    and genuinely needs nothing. Anything else — a.out, a data file the shell
    would hand to itself, a format this parser does not know — is UNJUDGED,
    because "I did not recognise it" has never been evidence that it runs.
    """
    try:
        with open(path, "rb") as f:
            head = f.read(64)
            if not head.startswith(b"\x7fELF"):
                return (LAUNCH_UNJUDGED, LAUNCH_UNJUDGED,
                        "no #! line and not an ELF binary, so helm cannot "
                        "measure whether the kernel can start it")
            # THE MAGIC IS FOUR BYTES AND PROVES ALMOST NOTHING. Reaching
            # the static-ELF return below on magic alone certified a file as
            # LAUNCH_OK that the kernel refuses outright: `7f454c46` + 60 NULs
            # is executable, parses to zero program headers, and execve
            # answers ENOEXEC (errno 8). Measured on the exact tree.
            #
            # SO THE HEADER MUST BE PROVEN, NOT ASSUMED. EI_CLASS must name a
            # width, EI_DATA an endianness, e_phentsize must match the width's
            # real program-header size, and the table must actually fit inside
            # the file. Anything else is a shape helm cannot read, which is
            # UNJUDGED — never OK. A malformed binary is exactly the case
            # where "I could not parse it" and "it will run" are furthest
            # apart, and the old code returned the confident one.
            bits, data = head[4], head[5]
            if bits not in (1, 2) or data not in (1, 2):
                return (LAUNCH_UNJUDGED, LAUNCH_UNJUDGED,
                        "ELF magic with an unreadable EI_CLASS/EI_DATA "
                        "(%r/%r), so the header shape is UNKNOWN" % (bits, data))
            if len(head) < (64 if bits == 2 else 52):
                return (LAUNCH_UNJUDGED, LAUNCH_UNJUDGED,
                        "ELF header is truncated (%d bytes), so nothing about "
                        "its program table can be read" % len(head))
            little = data == 1
            end = "little" if little else "big"
            if bits == 2:               # ELF64
                e_phoff = int.from_bytes(head[32:40], end)
                e_phentsize = int.from_bytes(head[54:56], end)
                e_phnum = int.from_bytes(head[56:58], end)
                p_offset_at, p_filesz_at = 8, 32
            else:                       # ELF32
                e_phoff = int.from_bytes(head[28:32], end)
                e_phentsize = int.from_bytes(head[42:44], end)
                e_phnum = int.from_bytes(head[44:46], end)
                p_offset_at, p_filesz_at = 4, 16
            # THE PROGRAM TABLE MUST BE REAL. A zero/oversized entry size, a
            # zero count with a nonzero offset, or a table that runs past the
            # end of the file are all shapes this parser cannot speak for.
            want_phentsize = 56 if bits == 2 else 32
            size_on_disk = os.path.getsize(path)
            if e_phnum == 0 or e_phentsize != want_phentsize \
                    or e_phoff == 0 \
                    or e_phoff + e_phnum * e_phentsize > size_on_disk:
                return (LAUNCH_UNJUDGED, LAUNCH_UNJUDGED,
                        "ELF program table is unreadable (phoff=%d phnum=%d "
                        "phentsize=%d against %d bytes on disk), so whether "
                        "this binary names a loader is UNKNOWN"
                        % (e_phoff, e_phnum, e_phentsize, size_on_disk))
            for k in range(e_phnum):
                f.seek(e_phoff + k * e_phentsize)
                ph = f.read(e_phentsize)
                if len(ph) < e_phentsize:
                    break
                if int.from_bytes(ph[0:4], end) != 3:      # PT_INTERP
                    continue
                off = int.from_bytes(ph[p_offset_at:p_offset_at + 8
                                        if bits == 2 else p_offset_at + 4], end)
                size = int.from_bytes(ph[p_filesz_at:p_filesz_at + 8
                                         if bits == 2 else p_filesz_at + 4], end)
                f.seek(off)
                interp = f.read(size).split(b"\x00", 1)[0].decode(
                    "utf-8", "replace")
                if not interp:
                    break
                if os.path.isfile(interp) and os.access(interp, os.X_OK):
                    return (LAUNCH_OK, None, None)
                if os.path.isfile(interp):
                    return (LAUNCH_GAP, LAUNCH_INTERPRETER_NOT_EXEC,
                            "ELF loader %s is present but not executable"
                            % interp)
                return (LAUNCH_GAP, LAUNCH_INTERPRETER_MISSING,
                        "ELF loader %s is missing, so the kernel refuses the "
                        "exec with ENOENT" % interp)
    except (OSError, ValueError, IndexError) as exc:
        return (LAUNCH_UNJUDGED, LAUNCH_UNJUDGED, _UNKNOWN_LAUNCH % exc)
    # A STATIC ELF WITH A PROVEN HEADER AND A REAL PROGRAM TABLE: no
    # PT_INTERP, nothing to resolve, mode bits already proven. Everything that
    # could NOT be proven returned UNJUDGED above rather than arriving here.
    return (LAUNCH_OK, None, None)


def _launch_of_named_interpreter(interp, _depth):
    """The verdict for one ABSOLUTE interpreter path, following the chain.

    Split out because the env form needs exactly this judgement about `env`
    itself while still refusing to judge env's target."""
    if os.path.isfile(interp) and os.access(interp, os.X_OK):
        # AN INTERPRETER CAN BE A SCRIPT. Its own #! line is the next thing the
        # kernel opens, so a break one hop down is as fatal as a break here and
        # used to read as fine.
        return _interpreter_launch(interp, _depth + 1)
    if os.path.isfile(interp):
        # PRESENT AND NOT EXECUTABLE IS A DIFFERENT HAND. "missing" sends the
        # reader to install something that is already there; the fix here is
        # chmod, and a refusal is only as useful as the cause it names.
        return (LAUNCH_GAP, LAUNCH_INTERPRETER_NOT_EXEC,
                "#! interpreter %s is present but not executable" % interp)
    return (LAUNCH_GAP, LAUNCH_INTERPRETER_MISSING,
            "#! interpreter %s is missing" % interp)


def _interpreter_gap(path):
    """The GAP detail for `path`, or None when there is no measured gap.

    KEPT AS A NARROW READER, NOT AS THE CONTRACT: it collapses UNJUDGED into
    None, which is exactly the coercion that caused the defect, so nothing that
    decides whether a guard is provisioned may call it. `_interpreter_launch`
    is the contract; this exists for message text where a gap is already
    established."""
    verdict, _key, detail = _interpreter_launch(path)
    return detail if verdict == LAUNCH_GAP else None


def external_status(spec):
    """(path, reason) for an EXTERNAL spec — the ONE resolution every surface
    reads, so install, status and doctor cannot disagree about a host.

    reason is "ok" (path is a proven-executable ABSOLUTE path) or one of:
      relative-pin         the pin is not absolute — REFUSED, never resolved
      pin-dead             the pin is absolute and is not an executable file NOW
      interpreter-missing  executable, but its `#!` names an interpreter that
                           is not there — mode bits say yes, the kernel says no
      interpreter-not-executable  the `#!` interpreter EXISTS and its own mode
                           bits refuse; the cure is chmod, not install
      shebang-unlaunchable the `#!` line itself cannot be exec'd (CRLF, empty,
                           `env` with no target) whatever is installed
      launch-unjudged      absolute and proven executable, but helm could not
                           MEASURE the launch (an `env` form resolves against a
                           PATH helm cannot see; a relative interpreter against
                           a cwd it cannot know). THE PATH IS STILL RETURNED —
                           install needs nothing more — but the word is not
                           "ok", because an unmeasured launch reported as ok is
                           how an unprovable guard came to be counted provisioned
      absent               no pin, and the name is not on PATH

    A RELATIVE PIN IS REFUSED, NOT RESOLVED, and that is the whole of this
    function's care. `os.path.isfile("guard")` answers against whatever cwd the
    asking process happens to have: a seat launched in its worktree and a doctor
    run in the shared checkout would resolve the SAME pin to two different
    files, or one to a file and the other to nothing — and the hook text written
    from it would name a path that means something different to every reader.
    Silently picking one cwd's answer is how a guard entry comes to LOOK
    installed and not run, which is the exact class task/1006 exists to close.

    STALE IS `pin-dead`, AND IT IS DELIBERATELY NOT THE SAME AS `absent`: a pin
    that named a real executable when it was written and names nothing now
    leaves configs carrying a rendered command for it. Those configs must read
    as STALE rather than as installed (see `_gap_rows`) — the file's claim is
    not evidence the guard can run."""
    name = spec.get("external")
    if not name:
        return None, "ok"
    pin = external_pin(spec)
    if pin:
        pin = os.path.expanduser(pin)
        if not os.path.isabs(pin):
            return None, "relative-pin"
        if os.path.isfile(pin) and os.access(pin, os.X_OK):
            verdict, key, _detail = _interpreter_launch(pin)
            if verdict == LAUNCH_GAP:
                return None, key
            # UNJUDGED KEEPS THE PATH AND LOSES THE WORD "ok". The file is
            # absolute and proven executable, which is everything install
            # needs, so refusing it here would disarm every host whose guard
            # carries an ordinary `#!/usr/bin/env` line. But it is NOT a
            # measured launch, and calling it ok is the exact claim that made
            # an unmeasured interpreter count as a provisioned guard.
            return pin, ("ok" if verdict == LAUNCH_OK else LAUNCH_UNJUDGED)
        return None, "pin-dead"
    found = shutil.which(name)
    # `which` honours a relative PATH entry too; only an absolute answer is a
    # stable one to bake into another process's settings file.
    if found and os.path.isabs(found):
        verdict, key, _detail = _interpreter_launch(found)
        if verdict == LAUNCH_GAP:
            return None, key
        return found, ("ok" if verdict == LAUNCH_OK else LAUNCH_UNJUDGED)
    return None, "absent"


def external_bin(spec):
    """The absolute path to an EXTERNAL spec's executable, or None. Proven
    executable — the whole failure this closes is a guard entry that LOOKS
    installed and cannot run. `external_status` carries the WHY."""
    return external_status(spec)[0]


def external_gap_message(spec):
    """The ONE sentence every surface prints about an unresolved external guard.

    Written once because it is said in three places (doctor, `hooks install`,
    `hooks status`), and two hand-kept copies of a warning is how a hardcoded
    "this stop" ended up in front of a PreToolUse reader (see spec_command)."""
    _p, why = external_status(spec)
    env = external_env(spec)
    pinned = external_pin(spec)
    lead = "required guard %s: " % spec["name"]
    if why == "relative-pin":
        return (lead + "%s is set to a RELATIVE path (%r) — REFUSED, because it "
                "would resolve against each process's own cwd and no two "
                "readers would agree which file it names; set an absolute path"
                % (env, pinned))
    if why == "pin-dead":
        return (lead + "%s names %r, which is NOT an executable file now — a "
                "config still carrying this guard is STALE, not installed; "
                "restore the file or re-point the pin"
                % (env, pinned))
    if why in (LAUNCH_INTERPRETER_MISSING, LAUNCH_INTERPRETER_NOT_EXEC,
               LAUNCH_SHEBANG_UNLAUNCHABLE, LAUNCH_UNJUDGED):
        path = (os.path.expanduser(pinned) if pinned
                else shutil.which(spec["external"]))
        _v, _k, detail = (_interpreter_launch(path) if path
                          else (None, None, "the #! line cannot launch"))
        detail = detail or "the #! line cannot launch"
        if why == LAUNCH_UNJUDGED:
            # NOT A GAP AND NOT A CLEAR. Saying nothing here is what let an
            # unmeasured interpreter be counted as a provisioned guard; saying
            # "cannot RUN" would accuse a file that very likely runs fine. The
            # sentence names what helm could not do, and prescribes nothing.
            return (lead + "%r is installed and executable, but helm could NOT "
                    "prove the kernel can start it — %s. Nothing to fix from "
                    "here; this row is UNPROVEN, not broken"
                    % (path, detail))
        # EACH CAUSE GETS ITS OWN HAND. These used to share one sentence that
        # told every reader "chmod is not the cure", which is precisely wrong
        # for the file whose only defect is its mode bits.
        if why == LAUNCH_INTERPRETER_NOT_EXEC:
            cure = ("chmod +x the interpreter — it is already installed, only "
                    "its mode bits refuse")
        elif why == LAUNCH_SHEBANG_UNLAUNCHABLE:
            cure = ("rewrite the #! line — the file itself is fine, the first "
                    "line is not something the kernel can exec")
        else:
            cure = ("chmod is not the cure; install the interpreter or "
                    "re-point %s at a launchable file" % env)
        return (lead + "%r carries its executable bit but cannot RUN — %s. %s"
                % (path, detail, cure))
    return (lead + "nothing executable named %s on this host — every seat and "
            "home runs UNGUARDED and no install can close it; put it on PATH "
            "or pin %s to an absolute path" % (spec["external"], env))


def resolved_specs(specs=SPECS):
    """`specs` minus any EXTERNAL spec this host cannot resolve.

    THE FILTER IS AN INSTALL-TIME DECISION, NOT A CONTRACT-TIME ONE. The spec
    stays in SPECS — it is still required, and `unresolved_externals` reports it
    loudly — but writing a hook that points at nothing would arm the gate
    template's MISSING alarm on EVERY Bash call on a host that simply does not
    have the executable. Refusing to write it, and saying so once in doctor, is
    the honest shape: absence of the binary is a machine fact, not a config gap
    that `hooks install` could ever close.
    """
    return tuple(s for s in specs if not s.get("external") or external_bin(s))


def unproven_launches(specs=SPECS):
    """[(spec, reason, message)] for every external guard that RESOLVES but
    whose launch helm could not measure — the row that used to be invisible.

    SEPARATE FROM `unresolved_externals` ON PURPOSE. These specs have an
    absolute, proven-executable path, so install writes them and the guard very
    likely runs; folding them into the unresolved list would put a WARN in
    front of every operator whose guard carries an ordinary `#!/usr/bin/env`
    shebang, and an alarm that fires on the healthy majority is one nobody
    reads. But the previous shape said "ok" about them, which is a claim helm
    never measured — and an unmeasured claim on the one surface whose job is to
    say whether the guard can execute is how this class started. UNPROVEN is
    its own word and gets its own row."""
    out = []
    for s_ in specs:
        if not s_.get("external"):
            continue
        path, why = external_status(s_)
        if path is not None and why == LAUNCH_UNJUDGED:
            out.append((s_, why, external_gap_message(s_)))
    return out


def optional_unconfigured(spec):
    """True when `spec` is an OPTIONAL external guard this host has not
    configured at all: no pin, and nothing by its name on PATH.

    THAT IS THE ONE STATE AN OPTIONAL GUARD MAY BE LEFT OUT IN. helm does not
    ship the executable, so a fresh install everywhere else would otherwise
    refuse its first `helm launch` over a program it was never given. Every
    other unresolved reading — a pin that names nothing now, a relative pin, an
    executable whose interpreter is gone — means the host DID configure the
    guard and it no longer runs, which stays a required gap exactly as before."""
    # `which` WITHOUT the absolute-path filter: a name that resolves only
    # through a RELATIVE PATH entry is still a configured guard (the operator
    # put it there) that helm refuses to bake, so it stays a required gap.
    return (bool(spec.get("optional")) and not external_pin(spec)
            and shutil.which(spec["external"]) is None)


def optional_note(spec):
    """The ONE sentence every surface prints about an unconfigured optional
    guard (hooks install, hooks status)."""
    return ("%s: not configured (optional) — nothing named %s on PATH and %s "
            "is unset; every other hook is installed. Put it on PATH or set "
            "%s to an absolute path and it installs and becomes required"
            % (spec["name"], spec["external"], external_env(spec),
               external_env(spec)))


def unconfigured_optionals(specs=SPECS):
    """[(spec, message)] for every optional external guard this host has not
    configured. Not a gap: reported so the operator knows it is off."""
    return [(s, optional_note(s)) for s in specs
            if s.get("external") and optional_unconfigured(s)]


def unresolved_externals(specs=SPECS):
    """[(spec, reason, message)] for every required external guard this host
    cannot resolve — the LOUD row doctor AND `hooks install` owe the operator,
    never a silent omission. Empty is the healthy answer. An optional guard the
    host never configured is not in it (`unconfigured_optionals`)."""
    out = []
    for s in specs:
        if not s.get("external") or optional_unconfigured(s):
            continue
        path, why = external_status(s)
        if path is None:
            out.append((s, why, external_gap_message(s)))
    return out

# The beacon's permission grease (owner hit it live): the SessionStart join
# line DIRECTS the beacon Monitor call (seats_advice.beacon_monitor) as the
# mandatory first action, but a fresh home/seat has no allow rule for that
# command — so the very first act of every new session HANGS on a human
# permission prompt, and an unattended pane never arms its only wake path.
# install merges these into permissions.allow on every home AND every seat,
# through the same gated backup→validate→atomic write as the hooks block:
# additive + idempotent, existing allow entries are NEVER dropped. Both the
# Bash and Monitor rule forms ride together — the beacon command is the same
# either way the harness runs it.
PERMIT_RULES = ("Bash(helm chat wait:*)", "Monitor(helm chat wait:*)")

# Scalar estate defaults every agent config carries, merged on the same install
# pass as the hooks + beacon permits — so both launch surfaces get them from ONE
# codepath: the claude credential homes (orca-launched claude seats read these)
# AND the isolated seat config dirs (helm-minted codex/kimi/… family seats read
# these). owner directive 2026-07-29: "WORKFLOWS ENABLED, DEFAULT SIZE SMALL, ON
# ALL LAUNCH SCRIPTS / ALL AGENTS". CC defaults dynamic workflows to size MEDIUM
# (changelog v2.1.220), so small must be set explicitly — absence is not small.
# workflowSizeGuideline is a settings.json key (changelog: "can be set from any
# settings file"); this is the one place that reaches every home and every seat,
# where the two per-family seed functions (_seed_seat_settings / _seed_onboarding)
# each cover only the family-seat half. The workflows-ENABLED half is already
# universal and is NOT re-wired here: it rides cachedGrowthBookFeatures.
# tengu_workflows_enabled, which every seat inherits wholesale via seat.py's
# _FEATURE_CACHE_KEYS (measured live: all five family seats carry it True). Unlike
# the merge-preserving hook/permit rails, a scalar estate policy is AUTHORITATIVE
# for its own key — an absent OR drifted value (a seat left at CC's medium
# default) is written to the estate value, exactly as _merge_event rewrites a
# stale hook command to canonical. Every sibling settings key survives untouched.
ESTATE_DEFAULTS = {"workflowSizeGuideline": "small"}

# THE AUTO-MEMORY BASE of a config dir whose `projects` is a SYMLINK. Every
# credential home links `projects` to the one shared tree under the default
# ~/.claude, so a seat's auto-memory dir is <home>/projects/<slug>/memory/ and
# resolves to ~/.claude/projects/<slug>/memory/. Claude Code (measured on
# 2.1.280) allows a memory write only when EVERY form of the path is inside
# the memory dir. The resolved form is not, so the check falls through to the
# sensitive-path check, which refuses any path with a `.claude` segment. In
# auto mode the write then stops on a permission prompt. Neither
# `permissions.additionalDirectories` (resolved OR link form) nor a path
# `Write(...)` allow rule clears that check: measured, both still prompt.
# This variable moves the memory BASE: the memory dir becomes
# <base>/projects/<slug>/memory/, which is the real directory with no link in
# it, so the write is allowed as a memory write. The directory on disk is the
# SAME one, so no note moves and every transcript reader is untouched. A
# config dir whose `projects` is a real dir needs nothing, and gets nothing.
MEMORY_BASE_ENV = "CLAUDE_CODE_REMOTE_MEMORY_DIR"


class HookPathError(ValueError):
    """helm cannot prove where its SHARED checkout is, or is being asked to
    persist a hook that points into a lane room. Refuse rather than bake a
    guess into every credential home and every seat config.

    A ValueError ON PURPOSE, not a bare Exception: configs._io's transform
    seam catches (TypeError, ValueError) and turns it into a `fail` result
    carrying this message, so the refusal reaches the operator through helm's
    own per-home reporting — one named home fails loudly, the estate loop
    keeps going, and nothing is written. A bare Exception would be a traceback
    that aborts the whole pass at the first bad home."""


_SHARED_ROOT = {}

# THE SLICE RUNNER'S DATA AUDIT (helm/gateslice.py) reports any module
# data a test unit leaves behind; these names are process-wide by design.
_GATESLICE_MUTABLE = {
    "_SHARED_ROOT": (
        "the shared checkout per start path, a pure function of the tree"),
}


def _shared_root(start):
    """The checkout whose bin/helm a generated hook must name, or None when
    git cannot say at all.

    THE PRIMITIVE IS `vcs.backend(start).common_dir(start)` — `git rev-parse
    --git-common-dir`, the seam automap._git_root and work._lanes.find_root are
    both built on. The target is passed, never cwd-derived (tests/test_vcs.py
    pins that; selection must follow the OPERATED repo). This does NOT call find_root, and the reason is measured
    rather than stylistic: find_root layers automap's `_strip_worktree` on top
    of the measurement, and that fold is a PATH-SHAPE heuristic built for
    project attribution, not for "which checkout am I". It is wrong here in
    both directions, and the whole-suite gate found both:

      * a worktree of a BARE repository (`…/mirrors/helm.git`) has no `.git`
        basename, so _git_root discards a perfectly good answer and find_root
        returns None. That is exactly how the build fabric prepares a gate
        room — under `…/fab/wt/<lane>/`, a path the room SHAPE also rejects —
        so deriving from find_root made helm_bin raise, and tests/test_hooks.py
        evaluates helm_bin in a class body at import: the module failed to load
        and took 44 tests with it.
      * a REAL main checkout that merely lives under a `wt/` directory gets
        folded to its PARENT. No raise, no complaint, just a silently wrong
        path baked into every home — the exact failure this whole lane exists
        to end, reintroduced one layer up.

    The rule, all measurement, no shape:
      common dir unreadable  -> we cannot say (the caller refuses)
      basename == ".git"     -> a linked worktree; its OWNER is the main
                                checkout at dirname(common). This is the lane
                                room case and the one the outage was.
      anything else          -> a worktree of a bare repo: there IS no main
                                checkout to fold to, so `start` is canonical.

    Memoized on `start`: spec_command renders once per spec per home and
    status_rows loops both surfaces, so the naive form would spawn `git
    rev-parse` a hundred times for an answer that cannot change inside a
    process. Tests that move the module clear `_SHARED_ROOT`."""
    if start not in _SHARED_ROOT:
        answer = None
        try:
            from . import vcs
            common = vcs.backend(start).common_dir(start)
            if common:
                answer = (os.path.dirname(common)
                          if os.path.basename(common) == ".git" else start)
        except Exception:               # noqa: BLE001 — no git, no answer,
            answer = None               # and a guess here reaches every home
        _SHARED_ROOT[start] = answer
    return _SHARED_ROOT[start]


def _in_lane_room(path):
    """Is `path` inside a lane/seat room (`<repo>-wt/<lane>/…`)?

    REUSES automap's `_strip_worktree` — the exact fold `find_root` applies —
    so the guard and the resolver can never disagree about what a room looks
    like. A hand-rolled `"helm-wt/" in path` would be that disagreement, and
    would also be wrong for every repo not named helm."""
    from . import automap
    p = os.path.normpath(path)
    return automap._strip_worktree(p) != p


def helm_bin():
    """The SHARED CHECKOUT's bin/helm, absolute — the generated hook must
    resolve without assuming the hook-time PATH, and must NOT resolve to
    whichever checkout the running module happens to live in.

    THIS DERIVATION CAUSED A FLEET-WIDE OUTAGE (2026-08-04). It read
    `dirname(dirname(__file__))` — the checkout of the RUNNING module — so any
    seat that ran a hook-touching verb from its lane room baked that room's
    `bin/helm` into every settings file it reached; helm's own comment calls
    _merge_event "the one place that reaches every home and every seat". The
    room was later deleted. Eight settings files pointed at a path that no
    longer existed, BOTH gate hooks among them (Stop stop-guard, PreToolUse
    argv-guard), and the failure was silent — see spec_command for the 127 arm
    that now says so.

    THE SHAPE TEST IS A LAST RESORT, NOT THE ANSWER. `_shared_root` measures,
    and whatever it returns is trusted — including a worktree of a bare mirror,
    which IS canonical because no main checkout exists to fold to. Only when
    git cannot answer at all does the room shape get a vote, and then it can
    only REFUSE, never redirect. An earlier draft had the shape overruling a
    measured answer and it broke the build fabric outright: fab prepares every
    gate room under `…/fab/wt/<lane>/`, which the shape rejects."""
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    root = _shared_root(here)
    if root:
        return os.path.realpath(os.path.join(root, "bin", "helm"))
    path = os.path.realpath(os.path.join(here, "bin", "helm"))
    if _in_lane_room(path):
        # A SELF-EXPLAINING REFUSAL, because this arm has one known false
        # positive and a bare exception would make it an outage instead of a
        # support question. `_in_lane_room` is a SHAPE test that deliberately
        # shares automap's fold with the resolver — so a MAIN checkout whose
        # directory merely ends in `-wt` reads as a room. Loosening the
        # predicate is the wrong cure (guard and resolver agreeing is the
        # property worth keeping), so the message names the directory, says
        # which test rejected it, and names the exact measurement that would
        # have overruled the shape had git been able to answer.
        raise HookPathError(
            "helm cannot prove where its shared checkout is.\n"
            "  module at:     %s\n"
            "  would resolve: %s\n"
            "  REFUSED: that path matches helm's lane-room SHAPE "
            "(<repo>-wt/<lane>/…). A room is deleted when its lane lands, and "
            "a hook pointing into one exits 127 — which the harness reads as "
            "ALLOW, silently. Baking it would reach every credential home and "
            "every seat config.\n"
            "  This is a shape test, not a measurement, and it only gets a "
            "vote because the measurement — `git -C %s rev-parse "
            "--path-format=absolute --git-common-dir` — returned no answer at "
            "all. Any answer at all would have been trusted over the shape, "
            "including a bare mirror (which means this worktree IS canonical)."
            "\n"
            "  IF THIS IS REALLY A MAIN CHECKOUT that merely ends in `-wt`: "
            "make that git read work here. It is the one thing that tells the "
            "two apart — a linked worktree's common dir points OUTSIDE itself "
            "at the repo that owns it, a main checkout's is its own .git. helm "
            "does not distinguish them any other way, on purpose: the guard "
            "and the resolver must agree about what a room is."
            % (here, path, here))
    return path


# What a gate spec's timeout message calls the thing it just let through —
# keyed by hook EVENT because that is what determines what was at stake: a
# Stop gate that dies unchecked allows a STOP; a PreToolUse gate allows a
# TOOL CALL. An event outside this table renders as "<Event> event" — vague
# but never wrong, per the honest-refusals law.
# THE SHIPPED LADDER, beside bin/helm. Claude Code echoes a hook's WHOLE
# command string whenever that hook blocks or errors, so an inline rc-case
# ladder is owner-facing output on every blocked stop — the Stop gate's frozen
# inline rendering measures 2,039 characters, printed in front of the one
# sentence the owner needed. The semantics are unchanged; only
# their address moved. NAMED HERE rather than spelled at each call site,
# because `hooks status` measures the same file it installs.
HOOK_WRAPPER = "helm-hook"


def wrapper_bin(executable=None):
    """The wrapper that runs every generated hook, beside the helm it wraps.

    DERIVED FROM THE SAME EXECUTABLE THE COMMAND CALLS, never resolved
    independently: `spec_command(spec, executable=…)` reconstructs an installed
    wrapper at its HISTORICAL helm path for exact retirement ownership, and a
    wrapper path resolved from today's checkout would make every such
    reconstruction a byte different from what is on disk.
    """
    return os.path.join(os.path.dirname(executable or helm_bin()), HOOK_WRAPPER)


_GATE_ALLOWED_NOUN = {"Stop": "stop", "PreToolUse": "tool call"}
_GATE_RC = "@@rc@@"

# EVERY MARKER `_gate_alarm` SPLICES, and the shell word each becomes. A marker
# is a token no sentence contains by accident, standing where a SHELL variable
# must appear inside text that is otherwise single-quoted data. The mapping is
# read by one split so that adding a marker cannot add a second, drifting copy
# of the quoting rule.
_MARKER_SHELL = {_GATE_RC: '"$rc"',
                 hookalarm.ARREARS_MARK: hookalarm.ARREARS_SHELL}
_MARKER_SPLIT = "(%s)" % "|".join(re.escape(m) for m in _MARKER_SHELL)

# WHAT THE SEAT LOSES when an advisory hook does not run — one sentence per
# spec, spliced into every failure arm of its generated command.
#
# The sentence names the CONSEQUENCE, never the mechanism. "helm join failed"
# sends a reader to the code; "this seat may be UNJOINED: no mentions, no DMs,
# no wake" tells them their seat is unreachable and looks merely idle, which is
# the state that cost the fleet hours on 2026-08-27. An alarm nobody can act on
# is the same silence with extra steps.
_ADVISORY_LOST = {
    "inject": "this turn ran WITHOUT its helm context — no brief, no whispers",
    # THE OWNER'S OWN WORDS FOR THIS ONE. It fires on every tool call, so the
    # sentence has to be readable at a glance and has to say that the loss is
    # RECOVERABLE — delivery is retried at the next tool boundary, which is
    # what makes this hook advisory rather than a gate.
    "deliver": "pending chat is deferred to the next call",
    "delegation-stop": "this delegation's stop was NOT recorded",
    "saguide": "this subagent started WITHOUT its initial physics",
    "join": ("this seat may be UNJOINED: no mentions, no DMs, no wake — it "
             "looks idle and is UNREACHABLE"),
    "resume-turn": ("this seat resumed WITHOUT its resume-turn — it may not "
                    "know its own obligations"),
    "handoff-precompact": "this compaction proceeds with NO handoff verified",
    "handoff-sessionend": "this session ended with NO handoff verified",
    "record": "this tool call was NOT recorded to the journal",
    "record-failure": "this tool FAILURE was NOT recorded to the journal",
}


def _gate_alarm(event, text):
    """One fail-open diagnostic, on the channels the HARNESS actually reads.

    STDERR WAS A VOID. The Claude Code hook contract, read rather than
    assumed: *"Stderr from a hook that exits 0 goes to the debug log only,
    never the transcript, and Claude never sees it."* So every arm below
    announced an UNCHECKED stop to nobody — a probe measured it — and the
    2026-08-04 outage's cure was itself unwitnessed. The exit-0 fail-open
    LAW is right and does not move: a guard that can wedge every seat's
    turn end is worse than one that misses a row.

    What moves is the channel. *"JSON output is only processed on exit 0"*
    — precisely the case these arms are in. `systemMessage` is shown to the
    OWNER; `hookSpecificOutput.additionalContext` reaches CLAUDE, and both
    gate events carry it (Stop renders it at the end of the turn,
    PreToolUse next to the tool result). Same exit code, two real readers,
    and stderr is kept because the debug log is still worth having.

    BOTH CHANNELS ARE RENDERED FROM ONE STRING, so the sentence the owner
    reads and the sentence the agent reads cannot drift apart — the failure
    that put a hardcoded "this stop" in front of a PreToolUse reader.

    `_GATE_RC` marks where the SHELL must splice `$rc`, which is why this
    emits a printf format plus arguments instead of interpolating: every
    helm-side fragment stays single-quoted, THE PATH INCLUDED. A checkout
    path is SHELL DATA, NEVER SHELL SOURCE. json.dumps runs BEFORE the
    split, so the bytes are valid JSON however the text was spelled and the
    only unquoted token is a variable this module wrote itself.

    `hookalarm.ARREARS_MARK` is the SECOND such marker and it rides the same
    machinery deliberately: the arrears clause has to reach BOTH channels and
    the JSON one too, and a second hand-kept splice is how the two would come
    to disagree. Splitting on a marker set rather than on one literal leaves
    every text that carries no marker rendered BYTE FOR BYTE as before, which
    is what `_advisory_command_v1` — a renderer that calls itself frozen and
    is checked against renderings on disk — depends on."""
    def splice(s):
        args = []
        for part in re.split(_MARKER_SPLIT, s):
            if part in _MARKER_SHELL:
                args.append(_MARKER_SHELL[part])
            elif part:
                # AN EMPTY PART IS NOT A printf ARGUMENT. A marker at either
                # end of the sentence leaves one, and emitting `''` for it
                # would print nothing while making every frozen rendering that
                # carries no marker a byte different from the one on disk.
                args.append(shlex.quote(part))
        return shlex.quote("%s" * len(args) + "\\n"), " ".join(args)

    payload = json.dumps({
        "systemMessage": text,
        "hookSpecificOutput": {"hookEventName": event,
                               "additionalContext": text},
    })
    return "printf %s %s >&2; printf %s %s" % (splice(text) + splice(payload))


def spec_command(spec, *, executable=None):
    """The generated hook text for one spec.

    `executable` reconstructs an installed wrapper at its historical helm path
    for exact retirement ownership checks; ordinary installs resolve helm_bin.

    ADVISORY specs keep `|| true`: a missing or wedged helm must inject
    nothing rather than hold a turn hostage.

    A GATE spec (`gate: True` — stop-guard and argv-guard) cannot, and this is
    the bug that disarmed helm's only enforcement layer for its whole life:
    stop_guard returns 2 to BLOCK, `|| true` rewrites that to 0, and the
    harness lets the agent stop. Measured 2026-07-26 — the guard was returning
    2 with SEVENTY-TWO undelivered messages while every turn ended cleanly.
    The owner: "ive never once seen them actually fire to stop you".

    A gate propagates ONLY rc 2 and swallows the rest, so a helm CRASH still
    fails open (stop_guard is code; a traceback here must not wedge every
    seat's turn end) while a deliberate refusal reaches the harness. Verified
    live for rc 2 / 1 / 127 / 124 before this was written — but "verified to
    exit 0" was not the whole question. rc 124 is `timeout` KILLING the guard,
    and it was the one swallowed code that left NO trace: a crash writes a
    traceback to stderr, a timeout kill writes nothing, so a guard that never
    ran was indistinguishable from one that ran and found nothing. It still
    exits 0 — that law does not move — and it now SAYS the stop went
    unchecked.

    Kill switches unchanged: HELM_STOP_GUARD=0, plus the 11 per-check
    HELM_STOP_GUARD_*=0 switches (INBOX/CLAIMS/LEASE_TTL/DELEGATION/BEACON/
    SPIRAL/PUNT/WIRING/CLAIME/WHISPER/INDEX — the complete register is the
    ENVIRONMENT.md table). The INBOX rung is LATCHED
    (once per pending-fingerprint) so a re-stop on the same rows passes; the
    CLAIMS rung listed beside it is NOT — it re-fires every stop until the
    lease is released. What keeps any rung from wedging a stop is
    `stop_hook_active`, not the latch. Do not restate this as a guard-wide
    property: that flat form was wrong in three places at once (here,
    VERBS, ENVIRONMENT) and seats.py's `stop_guard` is the only authority."""
    if spec.get("external"):
        # An external guard is its OWN executable — helm is not in the command
        # at all, so helm_bin/lane-room repair have nothing to say about it.
        # Falls back to the bare name when unresolved so this always returns a
        # string (every caller joins these); `resolved_specs` is what keeps such
        # a command from ever being WRITTEN.
        child = shlex.quote(external_bin(spec) or spec["external"])
    else:
        child = "%s %s" % (shlex.quote(executable or helm_bin()), spec["args"])
    if (spec.get("name") == "posttool"
            and spec.get("args") == "hooks run PostToolUse --installed --hook-json"):
        # The installed runner owns publication and its receipt. After a kill
        # or partial write the shell cannot know whether JSON already escaped;
        # a fallback JSON here could corrupt that one-response contract. Never
        # buffer stdout or turn UNKNOWN publication into a claimed delivery.
        return "%s %s" % (_wrapper_prefix(spec, "posttool", "-", executable),
                          child)
    if spec.get("gate"):
        # THE EVENT WORD IS THE SPEC'S, not the template's. One template
        # renders every gate spec, and a hardcoded "this stop" told the
        # argv-guard's PreToolUse reader a STOP had been allowed when what went
        # unchecked was a tool call.
        hookalarm.shell_suppressed(spec["name"], "")   # the key is argv now
        return "%s %s" % (
            _wrapper_prefix(spec, "gate",
                            _GATE_ALLOWED_NOUN.get(spec["event"],
                                                   "%s event" % spec["event"]),
                            executable),
            child)
    # ADVISORY: the same two-channel alarm as a gate, minus the teeth. The
    # cost is not symmetric across specs, which is why the sentence is
    # per-spec: a dead `join` leaves a seat UNJOINED — no mentions, no DMs, no
    # wake — indistinguishable from an idle one, and the fleet spent hours
    # chasing seats that were simply unreachable.
    hookalarm.shell_suppressed(spec["name"], "")       # the key is argv now
    return "%s %s" % (
        _wrapper_prefix(spec, "lane",
                        _ADVISORY_LOST.get(spec["name"],
                                           "helm %s did NOT run" % spec["name"]),
                        executable),
        child)


# THE LADDER AS IT WAS WRITTEN INLINE, at its two frozen spellings. Everything
# below this line is a RENDERER OF THE PAST: it is never installed, and it
# exists so that ownership of an already-installed wrapper does not expire the
# moment the template moves out of the command string and into bin/helm-hook.
# The recorded history of each arm lives with the arm, which is here, because
# this is the code those measurements were made against.


def _gate_command_v1(spec, executable):
    """FROZEN: the GATE ladder as it was rendered inline, before bin/helm-hook.

    rc 124 IS `timeout` KILLING THE GUARD, and it is the one non-2 code that
    must not pass in silence. A crash prints a traceback, so a swallowed rc
    1/127 still leaves evidence on stderr; a timeout kill prints NOTHING, so
    the stop looked exactly like a clean allow. The fail-open LAW is right (a
    guard that can wedge every seat's turn end is worse than one that misses a
    row) and the SILENCE was the bug.

    "A swallowed rc 127 still leaves evidence on stderr" WAS FALSE, and it cost
    a fleet-wide outage. Measured: `timeout 5 /gone/bin/helm …` writes ONE line
    to stderr — "timeout: failed to execute process" — under an exit code the
    harness reads as a clean allow, so nothing surfaced it to the agent or the
    owner. Eight settings files pointed at a deleted lane room, BOTH gates
    among them, and four credential homes ran unguarded in total silence. So
    the arm set is EXHAUSTIVE rather than enumerated-and-optimistic: 0 passes,
    2 blocks, and EVERY other code says what happened. A guard that could not
    run is a strictly worse state than one that timed out — the timeout at
    least proves helm was reachable — and only the timeout was reported.

    `case` rather than a chain of `[ … ] &&` tests so the unknown-code arm is a
    real default and cannot be forgotten by the next code added.

    THE PATH IS SHELL DATA, NEVER SHELL SOURCE — helm has watched a shell eat
    backticked content out of a message body three times in two days (the
    argv-guard exists for that), so the binary path rides inside the
    single-quoted text rather than being concatenated into the command.

    ONE SHORT LINE, AND IT STILL SAYS UNCHECKED. The owner read a seat where
    this banner printed several times a turn on every project, so the sentence
    keeps exactly three facts — which hook, what budget, what the seat lost —
    and drops the rest. UNCHECKED survives the trim on purpose: a silent
    fail-open is worse than a loud one. What changed is the RATE, one window
    over in `hookalarm`, and ONLY the 124 arm is rate-limited — a timeout is
    the one failure that repeats with nothing new to say, while MISSING and an
    unknown rc each name a broken estate that has to be read every time.
    """
    if not spec.get("gate"):
        return None
    if spec.get("external"):
        hb = external_bin(spec) or spec["external"]
        base = "timeout %d %s" % (spec["timeout"], shlex.quote(hb))
    else:
        hb = executable or helm_bin()
        base = "timeout %d %s %s" % (spec["timeout"], shlex.quote(hb),
                                     spec["args"])
    allowed = _GATE_ALLOWED_NOUN.get(spec["event"], "%s event" % spec["event"])
    timed_out = _gate_alarm(
        spec["event"], "[helm %s] TIMED OUT at %ds — this %s is UNCHECKED%s"
        % (spec["name"], spec["timeout"], allowed, hookalarm.ARREARS_MARK))
    missing = _gate_alarm(
        spec["event"],
        "[helm %s] THE GUARD IS MISSING — nothing executable at %s — "
        "this %s is ALLOWED and UNCHECKED; repair with: helm hooks "
        "install" % (spec["name"], hb, allowed))
    broke = _gate_alarm(
        spec["event"],
        "[helm %s] THE GUARD FAILED rc=%s — this %s is ALLOWED and "
        "UNCHECKED" % (spec["name"], _GATE_RC, allowed))
    return ("%s; rc=$?; case \"$rc\" in 2) exit 2 ;; 0) ;; 124) %s ;; "
            "127) %s ;; *) %s ;; esac; exit 0"
            % (base, hookalarm.shell_suppressed(spec["name"], timed_out),
               missing, broke))


def _advisory_command_v2(spec, executable):
    """FROZEN: the ADVISORY ladder as it was rendered inline, before bin/helm-hook.

    `|| true` was the whole advisory branch for helm's life, and it is the
    fail-open LAW written as an idiom — the law is right and does not move: a
    missing or wedged helm must never hold a turn. But `|| true` also rewrites
    rc 124 and rc 127 to success, and a `timeout` KILL prints NOTHING, so a
    lane hook that never ran was indistinguishable from one that ran clean.
    That is exactly the state docs/HOOKS.md records for the GATES — and the
    cure written for it stopped at the three gates, leaving the other eight
    silent for the same reason. v1 below is that pre-cure spelling; this is the
    cured one.

    NO `2)` ARM, ON PURPOSE. A gate propagates rc 2; an advisory spec must not,
    or every lane hook silently becomes a blocker (hookrun._stronger holds the
    same line for the in-process dispatcher). An advisory 2 is an anomaly, so
    it falls to `*)` — announced, and still exit 0. rc 0 stays SILENT on both
    channels: a hook that worked has nothing to say, and a wrapper that
    chatters on success is its own defect.
    """
    if spec.get("external") or spec.get("gate") or spec.get("name") == "posttool":
        return None
    hb = executable or helm_bin()
    base = "timeout %d %s %s" % (spec["timeout"], shlex.quote(hb), spec["args"])
    lost = _ADVISORY_LOST.get(spec["name"], "helm %s did NOT run" % spec["name"])
    timed_out = _gate_alarm(
        spec["event"], "[helm %s] TIMED OUT at %ds — %s%s"
        % (spec["name"], spec["timeout"], lost, hookalarm.ARREARS_MARK))
    missing = _gate_alarm(
        spec["event"], "[helm %s] IS MISSING — nothing executable at %s — %s; "
        "repair with: helm hooks install" % (spec["name"], hb, lost))
    broke = _gate_alarm(
        spec["event"], "[helm %s] FAILED rc=%s — %s"
        % (spec["name"], _GATE_RC, lost))
    return ("%s; rc=$?; case \"$rc\" in 0) ;; 124) %s ;; 127) %s ;; *) %s ;; "
            "esac; exit 0"
            % (base, hookalarm.shell_suppressed(spec["name"], timed_out),
               missing, broke))


def _posttool_command_v1(spec, executable):
    """FROZEN: the installed PostToolUse runner's ladder, before bin/helm-hook."""
    if (spec.get("name") != "posttool"
            or spec.get("args") != "hooks run PostToolUse --installed --hook-json"):
        return None
    hb = executable or helm_bin()
    base = "timeout %d %s %s" % (spec["timeout"], shlex.quote(hb), spec["args"])
    message = shlex.quote(
        "[helm posttool] outer runner failed; publication UNKNOWN at %s; rc=" % hb)
    return ('%s; rc=$?; case "$rc" in 0) ;; *) '
            'printf \'%%s%%s\\n\' %s "$rc" >&2 ;; esac; exit 0'
            % (base, message))


# THE CONSEQUENCE SENTENCES THAT HAVE CHANGED, at their v1 spelling. Reading
# the LIVE table from a renderer that calls itself frozen is the same defect
# this renderer exists to fix, one layer in: an installed wrapper carries the
# sentence that was current when it was written, so reproducing it needs that
# sentence and not today's. Only entries that actually moved belong here;
# everything else falls through to the live table, which is still correct for
# them by construction.
_ADVISORY_LOST_V1 = {
    "deliver": "pending chat was NOT delivered to this seat",
}


def _advisory_command_v1(spec, executable):
    """FROZEN: the advisory ladder as it was rendered before the 124 arm
    learned to speak once per window. Returns None for any spec this template
    never rendered (gates, externals, the posttool runner).

    A RETIREMENT MATCHER MUST KNOW THE SPELLINGS THAT ARE ON DISK, not the one
    today's template produces — and `cred._retired_guard_command` was written
    the other way, comparing against `spec_command` alone. That made every
    already-installed wrapper's ownership silently dependent on the template
    never changing again; the first change to it (this one) would have left
    six frozen producer renderings unrecognised, which retirement correctly
    treats as foreign and REFUSES to remove. Nothing would have broken loudly:
    the estate would simply have kept wrappers helm believed it had retired.

    So the historical renderings are DATA now, in `HISTORICAL_COMMANDS`, and a
    template change adds an entry here instead of quietly orphaning an estate.
    """
    if spec.get("external") or spec.get("gate") or spec.get("name") == "posttool":
        return None
    lost = _ADVISORY_LOST_V1.get(
        spec["name"],
        _ADVISORY_LOST.get(spec["name"], "helm %s did NOT run" % spec["name"]))
    base = "timeout %d %s %s" % (spec["timeout"], shlex.quote(executable),
                                 spec["args"])
    timed_out = _gate_alarm(
        spec["event"], "[helm %s] TIMED OUT after %ds — %s"
        % (spec["name"], spec["timeout"], lost))
    missing = _gate_alarm(
        spec["event"], "[helm %s] IS MISSING — nothing executable at %s — %s; "
        "repair with: helm hooks install" % (spec["name"], executable, lost))
    broke = _gate_alarm(
        spec["event"], "[helm %s] FAILED rc=%s — %s"
        % (spec["name"], _GATE_RC, lost))
    return ("%s; rc=$?; case \"$rc\" in 0) ;; 124) %s ;; 127) %s ;; *) %s ;; "
            "esac; exit 0" % (base, timed_out, missing, broke))


# Every rendering helm has ever WRITTEN into a settings file, newest first.
# Ownership reads this; only `spec_command` writes.
HISTORICAL_COMMANDS = (_advisory_command_v1, _advisory_command_v2,
                       _gate_command_v1, _posttool_command_v1)


def hook_command():
    """The inject spec's command — the name every older caller knows."""
    return spec_command(SPECS[0])


def _segments(cmd):
    """The segments alone — see _segments_ex, which is the whole walk."""
    return _segments_ex(cmd)[0]


# Shell constructs that CHANGE WHAT A SEPARATOR MEANS and that this walk does
# not model. Each is a way for `;` `|` `&` to appear inside a construct where
# no command begins, so a split there invents a segment that was never a
# command. Ownership is a LICENSE TO REWRITE, so inventing one is how helm
# comes to rewrite a line it does not understand.
_UNMODELLED = (
    ("((", "arithmetic evaluation"),
    ("|&", "pipe-with-stderr"),
    ("[[", "conditional expression"),
    ("<(", "process substitution"),
    (">(", "process substitution"),
    ("<<<", "here-string"),
    (">|", "clobbering redirection"),
    ("<>", "read-write redirection"),
)


def _segments_ex(cmd):
    r"""(segments, unmodelled) — split on command separators that are NOT
    inside quotes, a comment, an escape or a heredoc BODY, and report the first
    construct encountered that this walk does not model.

    WHY THE SECOND RETURN EXISTS, and it is the lesson of six rounds of grammar
    fixes on this one function: each round modelled one more construct
    correctly, and each round shipped a NEW false owner for the construct
    nobody had thought of yet. `((x && GUARD))` split at the `&&` and handed
    back ` GUARD))` as a command; `>| f` split at the `|`. The defect is not
    any particular missing rule — it is that an unrecognised construct
    produces a CONFIDENT WRONG ANSWER instead of an abstention.

    So the walk now reports its own ignorance and the caller DISOWNS on it.
    Being wrong in this direction costs helm the ability to auto-manage an
    exotic hook line, which the owner can still write by hand; being wrong in
    the other direction rewrites a line helm never understood.

    A regex split cannot do this: `echo 'a; /path/guard x'` contains a
    semicolon that starts no command, and splitting on it hands the quoted
    text back as if it were one — so a message ABOUT the guard became a
    command IN it.

    QUOTES WERE ONLY THE FIRST OF FOUR WAYS TEXT CAN LOOK LIKE A COMMAND AND
    NOT BE ONE, and ownership is a LICENSE — status counts an owned entry as
    ours and sync REWRITES it — so every one of them is a way for foreign
    prose to license a rewrite:

      an ESCAPED separator (`echo a\; b`) is a literal character the shell
      hands to echo, not a place a command begins;

      a COMMENT (`foo   # then ; /path/guard x`) is discarded by the shell
      before any command runs;

      a HEREDOC BODY is DATA. This is the one that matters most here, because
      helm's own verbs are invoked with quoted-delimiter heredocs constantly:
      a dispatch body or a chat post that MENTIONS a guard command was being
      read as executing it.

    Nothing else about shell grammar is modelled; ownership only needs to know
    where a command can BEGIN.
    """
    out, buf, quote = [], [], None
    i, n = 0, len(cmd)
    unmodelled = None
    pending_heredocs = []          # delimiters awaiting their body, in order
    while i < n:
        c = cmd[i]
        if quote:
            # A BACKSLASH IS LITERAL INSIDE SINGLE QUOTES and an escape inside
            # double quotes — the shell's own asymmetry, and getting it
            # backwards would swallow a real separator.
            if quote == '"' and c == "\\" and i + 1 < n:
                buf.append(c)
                buf.append(cmd[i + 1])
                i += 2
                continue
            if c == quote:
                quote = None
            buf.append(c)
            i += 1
            continue
        if c == "\\" and i + 1 < n:
            # ESCAPED: the next character is data whatever it is, INCLUDING a
            # newline (line continuation) and including a separator.
            buf.append(c)
            buf.append(cmd[i + 1])
            i += 2
            continue
        if c in "'\"":
            quote = c
            buf.append(c)
            i += 1
            continue
        if c == "#" and (not buf or buf[-1].isspace()):
            # A COMMENT RUNS TO THE END OF THE LINE. It must begin at a word
            # boundary — `foo#bar` is one word, not a comment — which is why
            # this looks at what precedes it rather than at the character
            # alone.
            j = cmd.find("\n", i)
            i = n if j < 0 else j
            continue
        if c == "<" and cmd.startswith("<<", i) and not cmd.startswith("<<<", i):
            # HEREDOC REDIRECTION. Record the delimiter now; its body starts
            # after the NEXT newline, and until then the rest of this line is
            # still ordinary command text.
            j = i + 2
            dash = j < n and cmd[j] == "-"
            if dash:
                j += 1
            while j < n and cmd[j] in " \t":
                j += 1
            delim, j = _heredoc_delimiter(cmd, j)
            if delim is not None:
                pending_heredocs.append((delim, dash))
                buf.append(cmd[i:j])
                i = j
                continue
        if c == "\n" and pending_heredocs:
            # THE BODY IS NOT COMMAND TEXT. Skip from here to the terminator
            # line for each pending delimiter, in the order they were opened.
            out.append("".join(buf))
            buf = []
            i = _skip_heredoc_bodies(cmd, i + 1, pending_heredocs)
            pending_heredocs = []
            continue
        if unmodelled is None:
            # OUTSIDE quotes/comments/heredocs by construction — every branch
            # that consumes those has already continued above.
            for lit, why in _UNMODELLED:
                if cmd.startswith(lit, i):
                    unmodelled = why
                    break
            else:
                # GROUPING CONSTRUCTS are WORD-SHAPED, not prefix-shaped, and a
                # literal match on them would be wrong in both directions.
                # `{` is a reserved word only as its OWN word — `${HOME}` and
                # `awk '{print}'` must NOT trip it, and disowning those would
                # unown ordinary entries. `(` opens a subshell only when it is
                # not the `$(` of a substitution nor the `((` handled above.
                # In both, the guard DOES run; we abstain because sync replaces
                # h["command"] WHOLESALE, so owning a line that also carries
                # the user's own commands destroys them (measured).
                prev = cmd[i - 1] if i else ""
                nxt = cmd[i + 1] if i + 1 < n else ""
                if c == "{" and prev != "$" and (not prev or prev.isspace()) \
                        and (not nxt or nxt.isspace()):
                    unmodelled = "brace group"
                elif c == "(" and prev != "$":
                    unmodelled = "subshell"
        if c in ";|\n&":
            # AN `&` INSIDE A REDIRECTION IS NOT A SEPARATOR. `2>&1 guard` was
            # split into `2>` and `1 guard`, so the second segment's executed
            # word came out as `1` and a RUNNING guard read as unowned. The
            # forms are `>&` / `<&` (the & follows the operator) and `&>` (it
            # leads one); `&&` is still a separator and is handled below.
            if c == "&" and not (i + 1 < n and cmd[i + 1] == "&") and (
                    (buf and buf[-1] in "><")
                    or (i + 1 < n and cmd[i + 1] == ">")):
                buf.append(c)
                i += 1
                continue
            if c in "|&" and i + 1 < n and cmd[i + 1] == c:
                i += 1                  # || and && are one separator
            out.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(c)
        i += 1
    out.append("".join(buf))
    return out, unmodelled


def _heredoc_delimiter(cmd, j):
    """(delimiter, index past it) for the word at `j`, or (None, j).

    The delimiter may be quoted — `<<'EOF'` is the form that stops the shell
    expanding the body, and it is the form helm's own call sites use — so the
    quotes are stripped to get the terminator text the shell will compare
    against."""
    n = len(cmd)
    if j >= n or cmd[j] in "\n;|&":
        return None, j
    start, quote, chars = j, None, []
    while j < n:
        c = cmd[j]
        if quote:
            if c == quote:
                quote = None
            else:
                chars.append(c)
        elif c in "'\"":
            quote = c
        elif c in " \t\n;|&":
            break
        elif c == "\\" and j + 1 < n:
            chars.append(cmd[j + 1])
            j += 1
        else:
            chars.append(c)
        j += 1
    delim = "".join(chars)
    return (delim, j) if delim else (None, start)


def _skip_heredoc_bodies(cmd, i, delims):
    """The index just past the bodies of `delims`, starting at `i`.

    `delims` is [(delimiter, stripped_with_dash)] because the two redirection
    spellings END DIFFERENTLY and getting that wrong resumes command parsing
    inside data.

    An UNTERMINATED heredoc consumes the rest of the string, which is what the
    shell does too: it reaches EOF still reading data. Returning the end here
    means the tail is never parsed as commands, and that is the honest answer —
    text the shell would have read as data must not license ownership."""
    n = len(cmd)
    for delim, dash in delims:
        while i < n:
            j = cmd.find("\n", i)
            line = cmd[i:] if j < 0 else cmd[i:j]
            i = n if j < 0 else j + 1
            # THE SHELL'S RULE EXACTLY, AND MY FIRST VERSION HAD IT BACKWARDS.
            # `<<` requires the terminator to stand alone with NO leading
            # whitespace; `<<-` strips leading TABS ONLY, never spaces. I wrote
            # `line.strip() == delim` and defended it in a comment as "the
            # forgiving direction — ends the body sooner, never treats more
            # text as commands". That is exactly inverted: ending the body
            # sooner hands THE REST OF THE BODY to the command parser, which is
            # the licensing path this whole function exists to close. A
            # heredoc whose terminator is indented under `<<` is one the shell
            # keeps reading, so we must keep reading too.
            candidate = line.lstrip("\t") if dash else line
            if candidate == delim:
                break
    return i

# A PRELUDE runs BEFORE the command and does not change whose command it is —
# and each one has its OWN option grammar. A single "skip the dash-words" rule
# is wrong for every entry here in a different way, so the grammar is DATA.
#
# `command -v` IS DELIBERATELY ABSENT FROM operand_opts AND HANDLED AS A
# NON-PRELUDE: `command -v foo` PRINTS foo's path and never runs it, so
# treating it as transparent claimed ownership of a command that does not
# execute — the false-POSITIVE twin of the three false negatives below.
_PRELUDE = {
    "timeout": {"operand_opts": {"-s", "--signal", "-k", "--kill-after"},
                "operands": 1},          # DURATION, then the command
    "nice":    {"operand_opts": {"-n", "--adjustment"},
                "operands": 0},          # `nice cmd` has NO numeric operand
    "env":     {"operand_opts": {"-u", "--unset", "-C", "--chdir",
                                 "-S", "--split-string"},
                "operands": 0},
    "exec":    {"operand_opts": {"-a"}, "operands": 0},
    # `eval guard hook` RUNS the guard: eval concatenates its arguments and
    # executes the result, so the word after it is the executed one exactly as
    # with exec. Transparent in the only form that can be read statically —
    # a quoted `eval "$X"` is unresolvable by construction and is already
    # handled by the opaque path, not by this set.
    # `no_opts` — THIS PROGRAM TAKES NO OPTIONS AT ALL, so a dash-word in the
    # option position is not a flag to step over, it is an ERROR that stops the
    # line from running. `eval -X guard` makes bash print "invalid option" and
    # exit; the guard NEVER RUNS, so claiming it licensed a rewrite of an entry
    # that executes nothing. Skipping the dash-word (the general rule) is right
    # for `timeout -v` and WRONG here, and only the program's own grammar says
    # which — so it lives in the table like every other per-program rule.
    # NOT generalised to "unknown dash-word anywhere disowns": for a program
    # that DOES take options, an unrecognised one is usually a flag we simply
    # have no row for, and disowning there earns the entry a canonical
    # duplicate appended beside a working hook — the false negative this table
    # exists to prevent.
    "eval":    {"operand_opts": set(), "operands": 0, "no_opts": True},
    "builtin": {"operand_opts": set(), "operands": 0, "no_opts": True},
    # `command` IS transparent — `command foo args` runs foo — EXCEPT under the
    # -v/-V forms, which PRINT a path and run nothing. Dropping the word
    # entirely was my first cut and my own positive control reddened on it:
    # that unowns `command /path/guard ...`, a valid hand-wired entry, which is
    # the false-negative class this same commit exists to end.
    "command": {"operand_opts": set(), "operands": 0,
                "not_transparent_opts": {"-v", "-V", "--version"}},
    # helm's OWN wrapper (bin/helm-hook), and the one row here that is not a
    # POSIX utility. It is a prelude by the same test as the rest — it execs
    # another program — and it MUST be in this table: the executed word behind
    # it is the child helm, and a phrase marker like `chat stop-guard
    # --hook-json` claims an entry only from the child's FIRST ARGUMENT. Left
    # out, helm would read every hook it installed as foreign and append a
    # canonical duplicate beside each one. FIVE OPERANDS, fixed by
    # `_wrapper_prefix` for exactly this reason, and NO OPTIONS AT ALL: a
    # dash-word in the kind position is not a flag to step over, so disowning
    # there is the safe direction.
    HOOK_WRAPPER: {"operand_opts": set(), "operands": 5, "no_opts": True},
}
_PRELUDE_WORD = re.compile(r"^(?:%s)$" % "|".join(sorted(_PRELUDE)))

# RESERVED WORDS ARE NOT COMMANDS, AND `_segments` HANDS THEM TO US.
# MEASURED, not imagined: a census of every hook command on this host — 387
# strings across 166 settings.json files, third-party repos included — puts
# `if` SECOND by frequency (17 uses) behind `timeout` (136). `_segments`
# splits on `;`, so `if [ -x helm ]; then helm inject --hook-json; fi` arrives
# as three segments and the middle one leads with `then`. `then` is shell
# SYNTAX; it never occupies the executed position. Read as a command it made
# our own entry UNOWNED, and the cost of a false negative here is stated
# plainly two functions down: an unowned entry gets a canonical duplicate
# appended beside a working hand-wired hook.
#
# A SEPARATE SET, NOT NEW _PRELUDE ROWS, because these are a different KIND.
# A prelude word is a program that execs another program and therefore has
# options and operands to step over; a reserved word is grammar, with neither.
# Folding them in would have given each an operand_opts/operands row that
# means nothing, and blurred the one property that makes the prelude set safe
# to keep closed.
#
# WHAT IS DELIBERATELY ABSENT AND WHY — `for`, `in`, `case`, `select`. Each is
# followed by a NAME or a WORD LIST, never by a command, so stepping over one
# would hand a non-command word the executed position it never had. That is
# exactly the failure the closed prelude set exists to prevent, and it would
# arrive here through the door marked "keywords are obviously transparent".
# `fi`, `done`, `esac` and `}` are absent for the opposite reason: nothing
# executes after them, so stepping over them buys nothing.
#
# `time` IS here and it is the one judgement call: as a reserved word `time
# CMD` runs CMD, and its only option is -p.
#
# `(` AND `coproc` ARRIVED BY PROBE, NOT BY ENUMERATION, and that is the
# lesson rather than the two words. My census counted FIRST WORDS of hook
# commands, so it was structurally incapable of finding a construct that
# appears BEFORE the command without being the command — a subshell opener or
# a coproc keyword. I named that blind spot in the review brief and a reviewer
# went and measured it: `( /opt/g/guard )` and `coproc /opt/g/guard` both RUN
# the guard while `_own_hit` answered False, which makes a working hand-wired
# entry read UNOWNED and licenses a canonical duplicate beside it.
#
# `coproc` IS STEPPED OVER FOR EXACTLY ONE WORD, which is right for `coproc
# CMD` and WRONG for `coproc NAME CMD` — there the executed word reads as
# NAME. That leaves the two-word form exactly as unowned as it is today, so
# this is a strict improvement rather than a complete one, and saying so is
# the point: bash cannot be disambiguated here without knowing whether the
# next token is an identifier or a program, and guessing would trade a false
# NEGATIVE for a false POSITIVE, which is the expensive direction.
#
# THE UNSPACED SUBSHELL `(cmd)` IS NOT COVERED EITHER: shlex yields it as one
# token, so it is a lexing question rather than a grammar one.
_KEYWORD = {"if", "elif", "then", "else", "while", "until", "do", "!", "{",
            "time", "(", "coproc"}

# A LEADING ASSIGNMENT IS A SHELL *NAME*, NOT ANY WORD CONTAINING "=". The old
# test — an "=" anywhere, not starting with "-" or "/" — stepped over words no
# shell would treat as an assignment (`a-b=1`, `2=3`, a bare `=`), and every
# word stepped over hands the executed position to the next one. That is the
# same defect the closed prelude set exists to prevent, arriving through the
# other door.
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

# A SHELL NAME, for the one place bash lets a name stand where a command
# otherwise would: `coproc NAME command`.
_SHELL_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# A REDIRECTION IS GRAMMAR AND CAN LEAD A SEGMENT. `> /dev/null guard` and
# `2>&1 guard` both RUN the guard, and a scan that treats the operator as the
# executed word reads the entry as unowned — the same false-negative that
# earns a duplicate hook. Two forms: SELF-CONTAINED (`2>&1`, `>&2`), which
# consume nothing, and TARGETED (`>`, `>>`, `<`, `2>`), which take the next
# word as their file.
# SELF-CONTAINED: the operator names both sides (`2>&1`, `>&2`) and consumes
# no following word. TARGETED: the operator needs a FILE (`>`, `>>`, `<`, `2>`,
# and `&>`/`&>>`, which redirect BOTH streams to a file). I first classed `&>`
# as self-contained and its target word became the executed one — the same
# false-owner shape, arriving through my own fix.
_REDIR_SELF = re.compile(r"^\d*(?:>&|<&)\d+$")
_REDIR_TARGET = re.compile(r"^\d*(?:>>|>|<)$|^&>>?$")

# `coproc NAME` IS ONLY A NAME BEFORE A *COMPOUND* COMMAND. Bash's grammar is
# `coproc [NAME] command`, and the NAME form requires a compound command — so
# `coproc CO GUARD` runs CO with GUARD as its ARGUMENT, and reading CO as a
# label hands ownership to a guard that never executes. These are the tokens
# that actually open a compound command.
_COMPOUND_OPEN = frozenset(("{", "(", "((", "[[", "if", "while", "until",
                            "for", "case", "select"))


# Programs we SHIP. A marker phrase whose head is one of these names the
# EXECUTABLE and must anchor there; any other head is a subcommand and
# anchors at the first argument instead.
_OWN_EXECUTABLES = frozenset(
    ["helm"] + [str(_s.get("external")) for _s in SPECS if _s.get("external")])


def _executed(segment):
    """(the EXECUTED word, its arguments) for one segment.

    A segment is prelude words, then ONE executed word, then arguments. The
    prelude set is CLOSED on purpose — because an open one would let any
    unknown leading word be stepped over and hand the next word the executed
    position it never had. `_PRELUDE_WORD` IS THAT SET, and it is the set this
    function actually consults: it was previously declared naming six words
    and then hand-checked for three, so `exec`, `command` and `builtin` were
    never stepped over and every entry behind one of them read as unowned. A
    declaration that no code reads is not a contract.

    SPLITTING IS shlex, NOT str.split. A pin containing a space — an absolute
    path under a directory with one, which is ordinary on a Mac — was split
    into two words by the plain split, so the executed word came out truncated
    and the entry could never be owned. shlex answers the question the shell
    answers. An unbalanced quote makes shlex raise; that is a string no shell
    would run either, and the honest answer for it is no executed word rather
    than a guess from a lossy fallback.
    """
    try:
        words = shlex.split(segment)
    except ValueError:
        return "", []
    i = 0
    while i < len(words):
        w = words[i]
        # AN UNSPACED SUBSHELL IS STILL A SUBSHELL. `( /opt/g/guard )` splits
        # into three tokens and the bare `(` is stepped over as grammar, but
        # `(/opt/g/guard)` is ONE token that matches no keyword — so the guard
        # RAN and parsed as unowned, and unowned means it gets licensed a
        # second time. The parenthesis is punctuation here, not part of the
        # program name, so it is stripped rather than enumerated.
        # `((expr))` IS ARITHMETIC EVALUATION AND RUNS NO COMMAND AT ALL.
        # Stripping its parens the way a subshell's are stripped
        # turned `((guard))` into the word `guard` and granted ownership to a
        # program that never executed — worse than the miss it was fixing,
        # because a FALSE owner suppresses the hook that would have provisioned
        # a real one. A doubled paren is grammar, not punctuation.
        # ARITHMETIC SPANS TO ITS CLOSING `))`, however it tokenizes. shlex
        # yields `((guard))` as ONE word but `(( guard ))` as THREE, and my
        # first cure skipped a single token — so the unspaced form was handled
        # and the SPACED form handed the executed position to the word inside,
        # falsely owning a program arithmetic never runs. Same defect, one
        # spelling over: I fixed the shape I had in front of me instead of the
        # construct. Skip to the close, so neither spelling leaks a word.
        if w.startswith("(("):
            if not w.endswith("))") or w == "((":
                while i < len(words) and not words[i].endswith("))"):
                    i += 1
            i += 1                      # step past the closing token itself
            continue
        if w.endswith("))"):
            i += 1                      # a stray close is grammar
            continue
        if w.startswith("(") or w.endswith(")"):
            bare = w.strip("()")
            if not bare:
                i += 1                  # a lone ( or ) is grammar
                continue
            w = bare
            words = words[:i] + [bare] + words[i + 1:]
        if _REDIR_SELF.match(w):
            i += 1                      # `2>&1` consumes no following word
            continue
        if _REDIR_TARGET.match(w):
            i += 2                      # the operator AND its file
            continue
        if _ASSIGNMENT.match(w):
            i += 1                      # VAR=value
            continue
        if w == "coproc":
            # `coproc NAME CMD` NAMES THE COPROCESS AND EXECUTES CMD, so
            # stepping over exactly one word hands the executed position to
            # NAME and the real command reads as unowned. The two forms are
            # genuinely ambiguous to a tokenizer — `coproc foo bar` could be
            # either — so the NAME is stepped over ONLY when doing so reveals
            # a program we actually ship. That direction is safe: it can turn
            # an unowned entry owned (the duplicate-licensing bug), and it can
            # never unown a valid one, because the step happens only when the
            # word behind it is one of ours.
            i += 1
            # STEP OVER THE NAME ONLY BEFORE A COMPOUND COMMAND. My first cure
            # stepped over any name-shaped word when the word behind it was
            # one of ours, which reads `coproc CO GUARD` as naming CO and
            # executing GUARD. Bash does the OPPOSITE: with a SIMPLE command
            # there is no NAME slot, so CO is the program and GUARD is its
            # argument — and calling that entry owned means we skip
            # provisioning a guard that never runs. The real named form is
            # `coproc CO { GUARD; }`, and `{` is already grammar below.
            if i + 1 < len(words) and _SHELL_NAME.match(words[i]) \
                    and words[i + 1] in _COMPOUND_OPEN:
                i += 1
            continue
        if w in _KEYWORD:
            i += 1                      # grammar, not a command
            while i < len(words) and words[i] == "-p" and w == "time":
                i += 1
            continue
        # A COMPOUND BODY KEEPS ITS SEPARATOR: shlex does not split on `;`,
        # so `{ GUARD; }` yields the word `GUARD;` and a basename compare
        # against it can never match.
        w = w.rstrip(";&")
        base = os.path.basename(w)
        spec = _PRELUDE.get(base)
        if spec is None:
            return w, words[i + 1:]
        i += 1
        # A FORM THAT PRINTS RATHER THAN RUNS IS NOT A PRELUDE AT ALL — but
        # ONLY ITS OWN OPTIONS DECIDE THAT. The first cut scanned every
        # remaining word, which is the wrong SCOPE in both directions
        # (measured): `command foo -v` cancelled ownership on a -v
        # that belongs to FOO, unowning a valid entry and earning it a
        # duplicate hook; and `command -pv foo` slipped through because a
        # COMBINED short cluster never equals the literal "-v", so a probe that
        # only prints was claimed and would be rewritten.
        blockers = spec.get("not_transparent_opts") or ()
        if blockers:
            for word in words[i:]:
                if word == "--" or not word.startswith("-"):
                    break              # the program starts; its flags are ITS OWN
                if word in blockers:
                    return "", []
                if not word.startswith("--") and len(word) > 1:
                    # A SHORT CLUSTER IS ITS LETTERS: -pv is -p and -v.
                    if any("-" + ch in blockers for ch in word[1:]):
                        return "", []
        # OPTIONS, AND WHETHER EACH TAKES AN OPERAND. Skipping every dash-word
        # and then guessing was wrong in BOTH directions, measured: `timeout -s
        # TERM 5 cmd` made "5" the executed word and `env -u FOO cmd` made
        # "FOO" the executed word, so two valid hand-wired entries read as
        # UNOWNED — and an unowned entry gets a canonical duplicate appended
        # beside it. A false negative here is not a missed catch, it is a
        # second hook.
        if spec.get("no_opts") and i < len(words) \
                and words[i].startswith("-") and words[i] != "--":
            return "", []          # errors before it runs; owns nothing
        while i < len(words) and words[i].startswith("-") and words[i] != "--":
            opt = words[i]
            i += 1
            if opt.split("=", 1)[0] in spec["operand_opts"] and "=" not in opt:
                i += 1              # its operand, which is NOT the command
        if i < len(words) and words[i] == "--":
            i += 1
        if spec["operands"]:
            # `timeout` takes a DURATION before the command; `nice` takes none,
            # so `nice cmd` used to have its command eaten as a phantom value.
            i += spec["operands"]
        if base == "env":
            while i < len(words) and _ASSIGNMENT.match(words[i]):
                i += 1
    return "", []


def _own_hit(cmd, tok):
    """`tok` marks OUR entry, by EXECUTED-WORD ANCHORING.

    Ownership is a license, not a label: status counts an owned entry as ours
    and sync REWRITES it, so destructive authority must not come from a
    marker appearing anywhere in a command string. A segment reads as prelude
    words, one EXECUTED word, then arguments, and a marker claims the entry
    only from a position that means the entry IS ours:

      A NAME marker (a guard binary) claims iff it IS the executed word or
      its basename. `/x/bin/guard-old` is a different program.

      A PHRASE marker (`inject --hook-json`) claims iff the phrase starts at
      the FIRST ARGUMENT of the executed word. The width across executables
      is deliberate — a hand-wired command may spell the binary as a path, a
      wrapper or a rename, and `--hook-json` is our own coinage, so the
      collision population is empty.

    WHAT THIS KILLS, which is the half of the finding that was always right:
    a phrase buried in the middle of arguments, prose about a guard, and an
    echo carrying the marker as text.
    """
    tok = str(tok or "")
    if not tok:
        return False
    segments, unmodelled = _segments_ex(cmd)
    if unmodelled is not None:
        # DISOWN ON IGNORANCE. This line uses shell grammar the segment walk
        # does not model, so every segment boundary in it is a guess. Claiming
        # ownership here licenses sync to REWRITE a line helm cannot read;
        # declining costs only auto-management of an exotic entry. Measured
        # false owners this kills: `((x && GUARD))` (split at `&&`, yielding
        # ` GUARD))`) and `>| f` (split at the `|`).
        return False
    for segment in segments:
        word, args = _executed(segment)
        if not word:
            continue
        base = os.path.basename(word)
        if " " not in tok:
            # NAME: the executed word itself, or its basename.
            if word == tok or base == os.path.basename(tok):
                return True
            continue
        head, _sp, rest = tok.partition(" ")
        # TOKENS, NOT A REJOINED STRING (a review addendum on the ownership-parser fix "the ownership parser read prose as commands, five ways" — cited by SUBJECT because that commit is lane-local and its sha resolves nowhere a reader can reach). I
        # introduced shlex to stop a spaced pin being split, then immediately
        # rejoined its output with spaces — which erases the ONE thing shlex
        # recovered. A single QUOTED argument whose text is the marker is
        # PROSE; rejoined it is byte-identical to the two-token real invocation,
        # so a sentence about the guard licensed a rewrite of the entry. The
        # marker is a SEQUENCE OF TOKENS and must match a token PREFIX.
        try:
            tok_words = shlex.split(tok)
        except ValueError:
            tok_words = tok.split()
        # PHRASE, TWO SPELLINGS OF ONE IDENTITY. A marker is either the
        # SUBCOMMAND alone ("inject --hook-json"), which must start at the
        # first argument, or the EXECUTABLE PLUS its subcommand ("helm
        # inject"), whose head names the executed program and whose tail must
        # start at the first argument. Both are the same typed fact — this
        # program, that subcommand — spelled two ways in the spec table.
        if os.path.basename(head) in _OWN_EXECUTABLES:
            # BINARY-ANCHORED. The head names a program we ship, so it must
            # BE the executed word — otherwise a foreign executable taking
            # "helm chat deliver" as its ARGUMENTS is claimed, which is the
            # destructive-authority case exactly.
            if base == os.path.basename(head) and _token_prefix(
                    args, tok_words[1:]):
                return True
        elif _token_prefix(args, tok_words):
            # SUBCOMMAND-ANCHORED. The head is a subcommand, not a program,
            # so the phrase claims from the first argument WHATEVER the
            # executable is spelled like — a path, a wrapper, a rename.
            return True
    return False


def _token_prefix(args, words):
    """`args` begins with the token sequence `words`.

    THIS REPLACES A STRING startswith, AND THE STRING VERSION FAILED TWICE.
    First it had no end, so the marker `chat wait` claimed the DIFFERENT
    subcommand `chat waiting-room`. Then, once shlex recovered real argument
    boundaries, rejoining them with spaces threw those boundaries away again:
    one quoted argument of prose containing a marker rejoined to exactly the
    same bytes as the real two-token invocation.

    Comparing TOKEN LISTS answers both at once: the two-element sequence
    inject, --hook-json matches a two-argument command and does NOT match the
    single argument whose text is "inject --hook-json", because that is ONE
    token that merely contains a space. Ownership is a license to rewrite the
    entry, so the difference between a command and a sentence about one has to
    survive to this line.

    (No backticked example here on purpose: the advertised-verb resolver reads
    any backtick-quoted helm invocation in this repo as a command it must be
    able to dispatch, and a quoted argument inside one parses as a root verb
    that does not exist. A doc example that breaks a guard is a doc defect —
    and my first fix for this introduced a SECOND one in its own explanation.)"""
    if not words:
        return False
    return list(args[:len(words)]) == list(words)

def _ours(cmd):
    """A hook command helm owns (installer-written or hand-wired helm inject).

    Spec-driven for the `own` tokens (SPECS[0] carries both inject forms, so
    the old hardcoded fast-path added nothing the loop does not), so a GATE
    command — which carries no `|| true` tail — is still recognized as helm's
    and does not read as a foreign hook to status/sync."""
    return any(_own_hit(cmd, tok) for sp in SPECS for tok in sp.get("own", ()))


def _helm_of(cmd):
    """The helm executable token a hook command calls, None when unparseable.

    THE EXECUTED WORD, NOT "the word before `inject`". That scan answered
    correctly for every rendering helm had installed, because `timeout N
    <helm> inject …` was the only shape and the word before `inject` WAS the
    helm. It is the shape that moved: the wrapper carries the spec NAME as an
    operand, so in `helm-hook lane inject UserPromptSubmit …` that word is the
    literal `lane`. Adjacency was never the question this function was asking;
    `_executed` is the walk that already knows every prelude, and asking it is
    what makes the next prelude one row rather than a hunt for scans."""
    word, _args = _executed(cmd)
    return word or None


def _resolvable(cmd):
    h = _helm_of(cmd)
    if not h:
        return False
    if os.path.sep in h:
        return os.path.isfile(h) and os.access(h, os.X_OK)
    return bool(shutil.which(h))


def _fail_open(cmd):
    """The never-block contract in the command text (docs/HOOKS.md law).

    THE CONTRACT, NOT THE IDIOM. `|| true` was the only way an advisory hook
    ever spelled "cannot block", so this used to test for that literal — and
    the moment the advisory branch grew a real rc-case (so a timed-out or
    missing helm SAYS so instead of vanishing), every cured hook would have
    read as violating the very law it still obeys.

    A command never blocks when no path can exit non-zero: the `|| true` tail,
    or an rc-case ending `exit 0` with no arm that propagates the block. A GATE
    is correctly NOT fail-open — it exits 2 on purpose — and that is exactly
    the arm this looks for, rather than guessing from the spec's name.
    """
    kind = _wrapper_kind(cmd)
    if kind is not None:
        # THE WRAPPER SAYS SO IN ITS OWN ARGV. bin/helm-hook's first operand is
        # the kind, and `gate` is the only one with an arm that propagates 2.
        # Reading the kind is exact where a text test cannot be: the rc ladder
        # no longer appears in the command at all, so both of the tests below
        # answer "not fail-open" about every advisory hook helm now installs.
        return kind != "gate"
    if "|| true" in cmd:
        return True
    return cmd.rstrip().endswith("exit 0") and "2) exit 2" not in cmd


def _wrapper_token(cmd):
    """The bin/helm-hook token in `cmd`, or None when there is not one.

    Every OLDER rendering on disk carries no wrapper at all — the ladder was
    the command — so None means "an inline vintage", never "broken"."""
    try:
        toks = shlex.split(cmd)
    except ValueError:
        return None
    for t in toks:
        if os.path.basename(t.rstrip(";&")) == HOOK_WRAPPER:
            return t
    return None


def _wrapper_kind(cmd):
    """The wrapper's KIND operand (gate|lane|posttool), or None."""
    try:
        toks = shlex.split(cmd)
    except ValueError:
        return None
    for i, t in enumerate(toks):
        if os.path.basename(t.rstrip(";&")) == HOOK_WRAPPER:
            return toks[i + 1] if i + 1 < len(toks) else None
    return None


def _wrapper_ok(cmd):
    """Is the shipped ladder this command names present and executable?

    THE ONE FAILURE THE WRAPPER CANNOT ANNOUNCE ITSELF. A missing bin/helm-hook
    makes the shell exit 127 before any line of the script runs, so the harness
    reports a hook error in its own words and the hook fails OPEN — loud, but
    not in helm's voice and not naming what to repair. That is the same shape
    as the deleted-lane-room outage, where eight settings files pointed at a
    path that no longer existed and nothing said so, and the answer is the
    same: a census that MEASURES the file rather than a wrapper that reports
    on its own absence. None for a rendering with no wrapper in it.

    PER COMMAND. `_wrapper_state` is what a CONFIG's readers call; this
    answers about one command string and exists so that both have one rule."""
    tok = _wrapper_token(cmd)
    if not tok:
        return None
    return os.path.isfile(tok) and os.access(tok, os.X_OK)


def _wrapper_state(cmds):
    """One config's ladder state over EVERY command in it: True (every wrapper
    it names is on disk and executable), False (at least one is not), None (it
    names no wrapper at all — an inline vintage, or an empty config).

    CONFIG-WIDE ON PURPOSE, not per lane. A deleted checkout takes down every
    hook in the file at once, so reading only the inject entry answers a
    smaller question than any caller is asking — and "the entry I happened to
    look at was fine" is the exact shape of the blind spot this measurement
    exists to close. One unrunnable ladder in a config is that config's answer.

    THE SAME FUNCTION FEEDS EVERY SURFACE. `_gap_row` calls it, so homes, seats
    and both doctor rungs read one measurement rather than four copies of it;
    the first cut of this had exactly one hand-run reader (`hooks status`) and
    doctor reported full coverage over a home whose every hook exited 127."""
    seen = [_wrapper_ok(c) for c in cmds]
    named = [ok for ok in seen if ok is not None]
    if not named:
        return None
    return all(named)


# ONE SENTENCE FOR A STATE ONLY A CENSUS CAN SPEAK, rendered from one place so
# `hooks status` (homes and seats) and `doctor` cannot describe it differently.
# The wrapper cannot announce its own absence — the shell exits 127 before any
# line of it runs — so nothing in the estate says this except a reader that
# MEASURES the file.
WRAPPER_GONE_MSG = ("%s(s) whose hook ladder is MISSING (%s): %s — every hook "
                    "there exits 127 before it runs, which the harness reads "
                    "as ALLOW. The ladder is TRACKED: restore it in the "
                    "checkout the command names, or re-run `helm hooks "
                    "install` if that checkout is the stale one.")


def wrapper_gone(rows):
    """The labels in `_gap_row` rows whose ladder is measured absent."""
    return [r["label"] for r in rows if r.get("wrapper") is False]


def wrapper_gone_message(kind, rows):
    """That one sentence for `rows`, or None when every ladder is there."""
    labels = wrapper_gone(rows)
    return (WRAPPER_GONE_MSG % (kind, HOOK_WRAPPER, ", ".join(labels))
            if labels else None)


def claude_homes():
    """[(name, realpath)] per claude home: every real dir under the claude
    homes root plus the default ~/.claude — homes.py's ROOTS/DEFAULTS
    discovery; alias symlinks fold onto their target, archives never appear."""
    from . import homes
    out, seen = [], set()
    for p in sorted(glob.glob(os.path.join(homes.ROOTS["claude"], "*"))):
        real = os.path.realpath(p)
        if os.path.islink(p) or not os.path.isdir(real) or real in seen:
            continue
        seen.add(real)
        out.append((os.path.basename(p), real))
    d = os.path.realpath(homes.DEFAULTS["claude"])
    if os.path.isdir(d) and d not in seen:
        out.append(("(default-claude)", d))
    return out


## The census walks this many path components below the seats root. The estate
## mints config dirs at depth 2 (seats/<family>/claude) and depth 4
## (seats/<family>/instances/<seat>/claude — slice 6); 6 leaves one full extra
## nesting generation of headroom so a THIRD shape lands IN the census (where
## an unrecognized dir fails the install loudly at configs' write gate) instead
## of OUTSIDE it (where task/331's codex seats sat: live, healthy-looking, and
## invisible to a "5 of 5 seats" fleet repair). The walk prunes into every
## matched config dir, so the bound guards only pathological layouts, not cost.
SEAT_WALK_DEPTH = 6

## The walk's absence set: an entry that is GONE when asked about was never a
## seat (running_panes' "gone" exclusion, the same law one substrate over) —
## the parent listed a name, the dir departed before the read, and a census
## describes NOW. ENOENT and ENOTDIR are the only errnos that MEAN gone;
## everything else (EACCES, EPERM, EIO, ELOOP, EMFILE, …) is a population the
## walk could not read, and an unread population must never flow into the
## same channel as a real absence.
_CENSUS_ABSENT = (errno.ENOENT, errno.ENOTDIR)


def _unread(e):
    """One reason string per unreadable census entry — type + kernel text."""
    return "%s: %s" % (type(e).__name__, e.strerror or e)


def seat_homes():
    """(rows, unread) — the seat census WITH its completeness, the same
    contract _project_contexts states one screen down: rows still ship when a
    subtree cannot be read, and the failure rides beside them where no
    consumer can mistake it for "no seats there".

    rows: [(seat, realpath)] per seat config dir — every dir named `claude`
    under <helm_home>/_global/seats, at any minted depth: <family>/claude AND
    <family>/instances/<seat>/claude (slice-6 instances). These get the full
    seat hook contract, including the delivery lane through which a launched
    seat receives fleet chat under its own name (the config dir's holder:
    `codex`, `codex-2`). Discovered by a bounded
    walk, NOT a fixed-shape glob — the one-level `*/claude` glob was task/331:
    a census that could not SEE nested instances reported the estate healthy
    around them. configs' gated write (_classify._is_seat_home) recognizes
    the same dirs, so the merge-preserving install accepts them. The walk
    never requires settings.json: install_home is what CREATES a fresh seat's
    settings.json, so a census keyed on its presence would be blind to
    exactly the dirs the installer must initialize.

    unread: [(path, reason)] per directory the walk tried to list or stat and
    COULD NOT, for any cause but absence (_CENSUS_ABSENT). The widened walk's
    first cut caught every OSError and silently returned — a probe proved it:
    chmod 000 on an instances/ subtree and the census reported the one
    visible family with total confidence, seat_coverage printing healthy
    around a live invisible codex seat. That is task/331's own disease
    reproduced one layer down: a read failure flowing into the same channel
    as a checked negative. The stat arm obeys the same law — os.path.isdir
    swallows OSError into False, so a child the walk can list but not stat
    (parent readable, not traversable) lands in unread rather than passing
    as "not a dir". A vanished dir (ENOENT/ENOTDIR) is a real absence: gone
    when asked about, it was never a seat — and that arm is entered on
    exactly those two errnos, so it cannot absorb an EACCES."""
    root = os.path.join(home.global_dir(), "seats")
    out, seen, unread = [], set(), []

    def walk(d, depth):
        try:
            names = sorted(os.listdir(d))
        except OSError as e:
            if e.errno not in _CENSUS_ABSENT:
                unread.append((d, _unread(e)))
            return
        for n in names:
            p = os.path.join(d, n)
            try:
                st = os.stat(p)   # follows links — the dir-ness of the TARGET
            except OSError as e:
                if e.errno not in _CENSUS_ABSENT:
                    unread.append((p, _unread(e)))
                continue          # ENOENT: raced away / dangling link — absent
            if not stat.S_ISDIR(st.st_mode):
                continue
            real = os.path.realpath(p)
            if n == "claude" and depth >= 2:
                if real not in seen:
                    seen.add(real)
                    out.append((os.path.basename(d), real))
                continue              # a config dir's insides are never seats
            if depth < SEAT_WALK_DEPTH and not os.path.islink(p):
                walk(p, depth + 1)

    walk(root, 1)
    return out, unread


def _hook_cmds(settings, event=HOOK_EVENT):
    """Every <event> command string in a settings dict (shape-tolerant)."""
    hooks = settings.get("hooks") if isinstance(settings, dict) else None
    groups = hooks.get(event) if isinstance(hooks, dict) else None
    out = []
    for g in groups if isinstance(groups, list) else []:
        if isinstance(g, dict):
            for h in g.get("hooks") or []:
                if isinstance(h, dict) and h.get("command"):
                    out.append(str(h["command"]))
    return out


def _all_hook_cmds(settings, repairable=False):
    """Every command; the room rail excludes protected PostToolUse text only."""
    from . import posttool
    hooks = settings.get("hooks") if isinstance(settings, dict) else None
    if not isinstance(hooks, dict):
        return []
    return [c for event in hooks for c in _hook_cmds(settings, event)
            if not (repairable and event == posttool.EVENT and posttool.protected(c))]


def _owned_specs(settings):
    """Helm spec names registered in one settings object.

    EVENT IS PART OF THE IDENTITY, and flattening every event into one list
    lost it. Two specs share the marker `handoff check --hook-json` —
    PreCompact and SessionEnd — so a single entry wired under ONE of them
    registered BOTH, and a settings file carrying half the pair reported the
    whole pair as installed. A marker plus an event is the identity; a marker
    alone is a family.
    """
    hooks = settings.get("hooks") if isinstance(settings, dict) else None
    if not isinstance(hooks, dict):
        return []
    from . import posttool
    # The installed composite has no logical SPECS marker. Project/home scope
    # reporting must still see its healthy member contracts, without adding it
    # to the logical dispatcher registry or granting substring rewrite authority.
    return sorted({spec["name"]
                   for event in hooks
                   for cmd in _hook_cmds(settings, event)
                   for spec in SPECS
                   if spec.get("event") == event
                   and any(_own_hit(cmd, tok) for tok in spec.get("own", ()))}
                  | ({s["name"] for s in SPECS if s.get("event") == posttool.EVENT}
                     & posttool.covered_members(settings, executable=helm_bin())))


def lane_room_tokens(cmd):
    """Every absolute-path token in `cmd` that names a lane/seat room.

    Token-wise rather than substring-wise: the check must survive a room name
    that merely contains the marker, and must not fire on the word appearing
    inside a message the command echoes."""
    try:
        toks = shlex.split(cmd)
    except ValueError:
        toks = cmd.split()
    return [t for t in toks if t.startswith(os.sep) and _in_lane_room(t)]


def stale_room_tokens(cmd, canonical=None):
    """Room-shaped helm-BINARY tokens in `cmd` that are NOT the derived answer.

    THE ONE PREDICATE for "is this command poisoned", shared by the repair
    loop, the post-write check (`_rooms_clean`) and the fail-closed backstop.
    It exists because those three sites disagreed and it cost a red gate:
    `lane_room_tokens` is a pure SHAPE read, and helm itself can legitimately
    live under a `wt/` path — the build fabric checks every gate room out at
    `…/fab/wt/<lane>/`. With the canonical answer counted as poison,
    `_rooms_clean` rejected helm's own path, `verify` failed on EVERY install,
    and 51 tests went red on the node while passing on a box whose helm is not
    under a `wt/` directory. The exemption had been written into the repair
    loop and into neither of its two siblings.

    Non-`helm` basenames are not this function's business: the repair loop
    reports those separately as unrepairable, and they are not a reason to
    refuse a write."""
    canonical = canonical or helm_bin()
    # THE WRAPPER COUNTS TOO, and for the same reason the binary does: a gate
    # whose ladder lives in a deleted room exits 127, which the harness reads as
    # ALLOW. Its canonical answer is derived from the binary's, so the two are
    # never independently stale.
    wrapper = wrapper_bin(canonical)
    return [t for t in lane_room_tokens(cmd)
            if t not in (canonical, wrapper)
            and os.path.basename(t) in ("helm", HOOK_WRAPPER)]


def _swap_room_token(cmd, tok, canonical):
    """`cmd` with one lane-room token replaced by `canonical`, byte-preserving
    everywhere else — no shlex round-trip, which would re-quote tokens this
    function has no business touching."""
    for form, repl in ((shlex.quote(tok), shlex.quote(canonical)), (tok, canonical)):
        if form in cmd:
            return cmd.replace(form, repl)
    return cmd


def repair_lane_room_commands(settings, path, owns=None):
    """Rewrite helm-OWNED hook commands that name a lane room, IN PLACE, so the
    persisted file points at the shared checkout. -> a list of note tuples
    (kind, event, detail) describing what was repaired and what was left.

    Runs on the CANDIDATE, immediately before it is persisted. It is
    deliberately a SECOND answer to a question helm_bin already answers:
    derivation can be wrong — it WAS wrong for this module's whole life, and on
    2026-08-04 it wrote a since-deleted lane room's bin/helm into eight settings
    files across the estate, both gate hooks among them. This holds even when
    the derivation is replaced, and even for a caller that composes its own
    command text without going through spec_command (record.py does exactly
    that).

    IT REPAIRS RATHER THAN REFUSES, and the counterexample that settles it
    happened four hours after the outage. The owner ran `hooks install` to
    clean four credential homes whose helm-OWNED gate entries named the dead
    room; the installer rewrote them and both gates came back. A guard that
    hard-failed on any owned room path would have failed on exactly those four
    homes — refusing on the poison it was being run to remove — leaving the
    Stop and PreToolUse gates dead and the operator holding a refusal with no
    verb that cleans it. A guard that refuses the only state its own repair
    path exists to fix converts a fixable outage into a wedged one.

    The asymmetry that makes repair safe: a helm-OWNED entry naming a room is
    exactly the thing we have both the right and the mechanism to rewrite —
    ownership is the merge law's whole basis, and pointing it at the derived
    shared path is the correction, not a clobber. A FOREIGN entry naming a room
    is somebody else's tool living in a worktree; it is left byte-identical and
    only NAMED. Neither outcome is a hard fail, and the file-level report says
    which was which rather than hiding both behind one refusal.

    The one hard failure left is an INTERNAL inconsistency: an owned helm-binary
    token that survives its own repair. That cannot be reached by any state of
    the operator's file — only by a bug in this function — so it stays fatal.

    A repo whose MAIN checkout is itself named `<something>-wt` reads as a room
    here. That is automap._strip_worktree's property, inherited on purpose (see
    helm_bin's refusal, which explains it to whoever hits it): a guard that
    disagreed with the resolver about what a room is would be worse than one
    that is wrong in the same direction."""
    owns = owns or _ours
    canonical = helm_bin()
    canonical_wrapper = wrapper_bin()
    notes = []
    hks = settings.get("hooks") if isinstance(settings, dict) else None
    events = sorted(hks.items()) if isinstance(hks, dict) else ()
    for event, groups in events:
        for g in groups if isinstance(groups, list) else []:
            if not isinstance(g, dict):
                continue
            for h in g.get("hooks") or []:
                if not isinstance(h, dict):
                    continue
                cmd = str(h.get("command") or "")
                from . import posttool
                if event == posttool.EVENT and posttool.protected(h.get("command")):
                    # Report sanitized ownership refusal through the installer,
                    # not raw room/command tokens from this protected leaf.
                    continue
                rooms = lane_room_tokens(cmd)
                if not rooms:
                    continue
                if not owns(cmd):
                    # NOT OURS. Someone else's tool may live wherever it likes;
                    # we say we saw it and we do not touch a byte.
                    notes += [("foreign", event, t) for t in rooms]
                    continue
                for tok in rooms:
                    if tok in (canonical, canonical_wrapper):
                        # ALREADY the derived answer — see stale_room_tokens,
                        # which is now the shared spelling of this exemption.
                        continue
                    if os.path.basename(tok) == HOOK_WRAPPER:
                        # THE WRAPPER IS A SIBLING OF THE BINARY AND MOVES WITH
                        # IT. It is the one non-`helm` basename whose correct
                        # value IS known — `wrapper_bin` derives it from the
                        # same canonical answer — so reporting it unrepairable
                        # would leave every repaired entry naming a live helm
                        # through a ladder in a deleted room, which is the
                        # 127-reads-as-ALLOW outage with one extra step.
                        cmd = _swap_room_token(cmd, tok, canonical_wrapper)
                        notes.append(("repaired", event, tok))
                        continue
                    if os.path.basename(tok) != "helm":
                        # ours, but not a helm binary — we know it is a room and
                        # we do NOT know what the correct value would be, so
                        # guessing is worse than reporting.
                        notes.append(("unrepairable", event, tok))
                        continue
                    cmd = _swap_room_token(cmd, tok, canonical)
                    notes.append(("repaired", event, tok))
                h["command"] = cmd
    # POST-CONDITION, the fail-closed backstop: no owned command may still name
    # a room's helm binary. Unreachable from any operator state — only a bug in
    # the swap above can trip it — so it is the one arm that still raises.
    survivors = sorted({t for cmd in _all_hook_cmds(settings, repairable=True) if owns(cmd)
                        for t in stale_room_tokens(cmd, canonical)})
    if survivors:
        raise HookPathError(
            "INTERNAL: repairing %s left %d helm-owned hook command(s) still "
            "naming a lane room's binary (%s). The repair did not take; "
            "refusing to persist a settings file whose gate would exit 127 and "
            "read as ALLOW." % (path, len(survivors), "; ".join(survivors)))
    return notes


def lane_room_report(notes):
    """One human line per outcome class, or None when there was nothing to say.
    Kept separate from the repair so the report is a pure function of the
    notes and can be asserted without a filesystem."""
    if not notes:
        return None
    out = []
    for kind, label in (("repaired", "repointed at the shared checkout"),
                        ("unrepairable", "helm-owned but not a helm binary — "
                                         "LEFT AS IS, no correct value is known"),
                        ("foreign", "not helm's — LEFT UNTOUCHED")):
        rows = [n for n in notes if n[0] == kind]
        if rows:
            out.append("lane-room paths %s: %s" % (label, "; ".join(
                "%s (%s)" % (t, ev) for _k, ev, t in rows)))
    return " | ".join(out)


def _checkout_root(cwd):
    """The checkout that owns cwd, preserving a linked worktree's own root."""
    if not cwd:
        return None
    real = os.path.realpath(cwd)
    if not os.path.isdir(real):
        return None
    from .cli import _tree_of
    return _tree_of(real) or real


def _project_contexts(panes=None):
    """Projects loaded now: current cwd plus every identity-verified live seat.

    Returns (contexts, unknown). Each context binds one checkout root to the
    active CLAUDE_CONFIG_DIRs observed there. The roster is the bounded source;
    the registry is deliberately not walked (it contains historical cwds)."""
    from . import homes
    from . import seats
    contexts, roots = {}, {}

    def add(cwd, config_dir):
        if not cwd:
            return
        real = os.path.realpath(cwd)
        root = roots.get(real)
        if root is None:
            root = roots[real] = _checkout_root(real)
        if not root:
            return
        active = contexts.setdefault(root, set())
        if config_dir:
            active.add(os.path.realpath(config_dir))

    current = seats.safe_cwd()
    add(current, os.environ.get("CLAUDE_CONFIG_DIR") or homes.DEFAULTS["claude"])
    roster, failed = seats.roster_checked()
    if failed:
        return [{"root": r, "homes": contexts[r]} for r in sorted(contexts)], \
            "live-seat roster unreadable"
    scanned = running_panes() if panes is None else panes
    # A PANE WE COULD NOT READ MAKES THIS CENSUS INCOMPLETE, and this function
    # already has the channel to say so — `unknown` is returned above the
    # moment the ROSTER is unreadable. A blind pane is that same fact one
    # level down: `p["seat"]` is None because the environ could not be read,
    # not because the pane is unnamed, and the filter below drops both alike.
    # Callers then read a COMPLETE-looking context set: project_scope_rows can
    # claim `project-only` from a census that never saw one of the live seats.
    # Same fail-open as the surface above it, in a consumer I had not audited.
    # (A re-read found it — the third consumer of this return value tonight.)
    blind = [p for p in scanned if p.get("environ_unreadable")]
    panes = {str(p.get("seat") or "").casefold(): p.get("config_dir")
             for p in scanned if p.get("seat")}
    unver = seats.unverified_seats(roster)
    for seat, row in roster.items():
        if seats.presence_with_identity(seats.last_seen(seat, row),
                                        unver.get(seat)) \
                in ("absent", seats.UNVERIFIED):
            continue
        add(row.get("cwd"), panes.get(str(seat).casefold()))
    # The census is returned WITH its completeness. Contexts still ship — a
    # blind pane holds back only the certainty, never the rows we did read,
    # which is the same blast-radius rule beacons.agent_index states for its
    # own scan.
    return ([{"root": r, "homes": contexts[r]} for r in sorted(contexts)],
            ("%d running pane(s) unreadable — census incomplete" % len(blind)
             if blind else None))


def project_scope_rows(contexts=None):
    """Project-level owned hooks and whether an active home duplicates them.

    Read-only and total. Presence alone is not a defect: project-only wiring is
    a supported manual choice. The defect is the same spec loaded at BOTH
    project and active-home scope; unreadable evidence remains UNKNOWN."""
    from . import pk
    unknown = None
    if contexts is None:
        contexts, unknown = _project_contexts()
    out, cache = [], {}
    if unknown:
        out.append({"status": "unknown", "detail": unknown})

    def load(path):
        real = os.path.realpath(path)
        if real in cache:
            return cache[real]
        try:
            with pk.open_regular(real, encoding="utf-8") as f:
                settings = json.load(f)
        except FileNotFoundError:
            result = set(), None
        except (OSError, ValueError, TypeError) as e:
            result = None, "%s: %s" % (e.__class__.__name__, e)
        else:
            result = (set(_owned_specs(settings)), None) if isinstance(settings, dict) \
                else (None, "root is not an object")
        cache[real] = result
        return result

    from .skillsync import config_dirs
    home_roots = {os.path.realpath(p) for _n, p in config_dirs()}
    for ctx in contexts:
      root = ctx["root"]
      # BOTH project surfaces: committed settings.json AND per-machine
      # settings.local.json (where `hooks install --project` writes) — a
      # local-only wiring invisible to this scan was a blind spot: the one
      # census whose job is cross-scope duplicates could not see the scope
      # the installer itself creates.
      for _fname in ("settings.json", "settings.local.json"):
        path = os.path.join(root, ".claude", _fname)
        scope_dir = os.path.realpath(os.path.dirname(path))
        active_roots = {os.path.realpath(p) for p in ctx.get("homes") or ()}
        if scope_dir in home_roots | active_roots:
            continue                 # the same physical file is USER, not project
        if not os.path.exists(path):
            continue
        specs, error = load(path)
        if error:
            out.append({"status": "unknown", "path": path,
                        "detail": "project settings unreadable (%s)" % error})
            continue
        if not specs:
            continue
        duplicates, unreadable = [], []
        homes_seen = sorted(ctx.get("homes") or ())
        for h in homes_seen:
            hp = os.path.join(h, "settings.json")
            home_specs, error = load(hp)
            if error:
                unreadable.append("%s (%s)" % (hp, error))
                continue
            overlap = sorted(specs & home_specs)
            if overlap:
                duplicates.append({"path": hp, "specs": overlap})
        shown_specs = sorted(specs)
        if duplicates:
            out.append({"status": "duplicate", "path": path,
                        "specs": shown_specs, "duplicates": duplicates})
        elif unreadable or not homes_seen:
            detail = "active home unknown" if not homes_seen else \
                "active home settings unreadable: " + "; ".join(unreadable)
            out.append({"status": "unknown", "path": path,
                        "specs": shown_specs, "detail": detail})
        else:
            out.append({"status": "project-only", "path": path,
                        "specs": shown_specs})
    return out


def project_scope_message(row):
    status = row["status"]
    if status == "duplicate":
        homes_found = ", ".join(d["path"] for d in row["duplicates"])
        specs = sorted({s for d in row["duplicates"] for s in d["specs"]})
        return ("cross-scope DUPLICATE: %s and %s both register %s — inspect; "
                "helm will not auto-delete either scope"
                % (row["path"], homes_found, ", ".join(specs)))
    if status == "project-only":
        return "project hook only: %s registers %s (no observed active-home duplicate)" \
            % (row["path"], ", ".join(row["specs"]))
    return "project hook scan UNKNOWN%s: %s" % (
        " at " + row["path"] if row.get("path") else "", row["detail"])


def _print_project_scopes(rows, prefix):
    for row in rows:
        print(prefix + project_scope_message(row))


def _matcher_ok(group, spec):
    """A spec with a matcher demands EXACTLY that matcher on its group — a
    stale `PostToolUse` group pinned to `Bash` silently misses most tool
    boundaries (codex B3). Specs without one (UserPromptSubmit) don't care."""
    return spec["matcher"] is None or group.get("matcher") == spec["matcher"]


# THE HARNESS GRACE. Claude's outer deadline must outlive the shell wrapper
# that mints its alarm, or `timeout` is killed before it can report rc 124 and
# a hook that never ran is indistinguishable from one that ran clean. Named
# here rather than inlined so the relationship is assertable.
#
# A TERM-DEAF CHILD IS STILL UNBOUNDED and that is a known, filed gap: `timeout
# N` sends TERM and nothing else. The `--kill-after` escalation that used to
# sit here is backed out — see `_wrapper_prefix` for why it could not be made
# honest — so a child that ignores TERM can still outlive this wrapper.
GRACE_S = 5


def _wrapper_prefix(spec, kind, consequence, executable=None):
    """`bin/helm-hook <kind> <name> <event> <budget> <consequence>` — the whole
    installed command except the child, and deliberately no kill escalation.

    FIVE OPERANDS, ALWAYS, WHATEVER THE KIND. `_PRELUDE` steps over exactly
    this many words to find the child's executable, and that step is what keeps
    a phrase marker anchored at the child's FIRST ARGUMENT. A variable-width
    head would make helm read its own installed hooks as foreign — and an
    unowned entry is not a missed catch, it earns a canonical duplicate
    appended beside a working hook. The posttool kind passes "-" rather than
    shortening the head for that reason.

    A `--kill-after` escalation WAS here and is backed out, because it cannot
    be made honest inside this lane and both of its problems are the same
    shape: it changes what an exit code MEANS.

      * rc 137 is 128+SIGKILL and belongs to every SIGKILL there is — the OOM
        killer, an operator's `kill -9`, a self-inflicted one. Routing it to
        the timeout arm reports "THE GUARD TIMED OUT" for kills that had
        nothing to do with the deadline, so the escalation trades a false
        FAILURE report for a false TIMEOUT report. A probe measured a
        self-SIGKILL returning 137 well BEFORE the deadline.
      * its EFFECT is host-dependent: measured 2026-09-09, it ends a
        TERM-ignoring child at 3.01s on the owner's laptop and leaves the same
        family alive for its full 8s on a fab node.

    Bounding a TERM-deaf process FAMILY and identifying an escalation this
    wrapper OWNS (rather than inferring it from a code anyone can produce) is
    a real problem and it is a different one from making the guard fit its
    budget. It has its own row. Do not re-add the flag without both halves.
    """
    return "%s %s %s %s %d %s" % (
        shlex.quote(wrapper_bin(executable)), kind, shlex.quote(spec["name"]),
        shlex.quote(spec["event"]), spec["timeout"], shlex.quote(consequence))


def _gate_outer_timeout(spec):
    """Claude's deadline must outlive the shell wrapper that mints its alarm.

    EVERY SPEC, NOT ONLY GATES, since the advisory branch grew the same rc-case
    ladder. The grace existed because Claude's outer runner defaults to five
    seconds and kills the shell before the inner `timeout` can fire rc 124 —
    and rc 124 is the arm that exists precisely because a `timeout` KILL prints
    NOTHING, so a hook that never ran is otherwise indistinguishable from one
    that ran clean.

    Gate-only grace was correct when gates were the only specs that spoke. It
    silently made the new advisory alarm unreachable for EIGHT of the ten
    advisory specs — every one whose inner budget is >= 5s, including `inject`
    and `record` at 10s, `join` and `resume-turn` at 5s. Their rc-124 arm would
    have been dead code: written, tested against the wrapper in isolation, and
    unable to fire in the estate. The alarm and the deadline that lets it speak
    are one mechanism, so they are decided in one place.

    Returns None only for a spec with no timeout to outlive.
    """
    return spec["timeout"] + GRACE_S if spec.get("timeout") else None


def _canonical_entry(spec):
    hook = {"type": "command", "command": spec_command(spec)}
    outer_timeout = _gate_outer_timeout(spec)
    if outer_timeout is not None:
        # The shell timeout owns the loud fail-open verdict. Claude's default
        # hook deadline is five seconds, so without explicit grace the
        # harness can kill this wrapper before it reports rc 124.
        hook["timeout"] = outer_timeout
    entry = {"hooks": [hook]}
    if spec["matcher"]:
        entry["matcher"] = spec["matcher"]
    return entry


def _merge_event(out, spec):
    """Merge ONE spec's entry into `out` IN PLACE -> action ok|add|update.
    An owned entry is CURRENT only when command, type AND the containing
    group's matcher all match (codex B3). A wrong matcher is repaired in
    place when the group is exclusively ours; with foreign co-tenants our
    hook relocates to a canonical group and the foreigners keep their group
    byte-identical. MERGE-preserving throughout: only the entry carrying
    this spec's own-marker is ever written. Raises ValueError on a shape we
    must not touch.

    EVERY marker-carrying entry IS VISITED, not the first one. The original
    repaired the first entry it found and RETURNED, so a second entry carrying
    the same own-marker in the same event was repaired by nobody — and the
    harness runs that one too. On 2026-08-04 the entries in question named a
    deleted lane room, and the hole meant `helm hooks install` could not have
    cleaned the estate even after helm_bin was fixed: re-running the installer
    is the standard cure, and for a duplicated entry it was a no-op forever.
    A repair path with a permanently unreachable entry is not a repair path.

    The duplicate is NORMALIZED, not deleted. helm owns the entry, so pointing
    it at the live binary is the correction; removing it is a different
    decision (the harness would run one fewer gate) and is not this function's
    to take. Two identical gate entries are wasteful; two entries where one
    silently allows are an outage."""
    from . import posttool
    cmd = spec_command(spec)
    outer_timeout = _gate_outer_timeout(spec)
    own = spec["own"]
    hooks = out.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("existing 'hooks' key is not an object — fix it by hand")
    groups = hooks.setdefault(spec["event"], [])
    if not isinstance(groups, list):
        raise ValueError("existing hooks.%s is not a list — fix it by hand"
                         % spec["event"])
    found = [(g, g.get("hooks") or [], h) for g in groups if isinstance(g, dict)
             for h in (g.get("hooks") or [])
             if isinstance(h, dict)
             and any(_own_hit(str(h.get("command") or ""), m) for m in own)
             and not (spec["event"] == posttool.EVENT
                      and h.get("command") != cmd and posttool.protected(h.get("command")))]
    if not found:
        groups.append(_canonical_entry(spec))
        return "add"
    action = "ok"
    homed = False          # an ordinary policy has a matcher-correct home
    orphans = []           # ours, in a wrong-matcher group shared with foreigners
    for g, hlist, h in found:
        if h.get("command") != cmd or h.get("type") != "command" \
                or (outer_timeout is not None
                    and h.get("timeout") != outer_timeout):
            h["command"] = cmd
            h["type"] = "command"
            if outer_timeout is not None:
                h["timeout"] = outer_timeout
            action = "update"
        ordinary = spec["event"] != posttool.EVENT or not set(h) - {"type", "command", "timeout"}
        if _matcher_ok(g, spec):
            homed = homed or ordinary
        elif len(hlist) == 1:            # the group is ours alone — repair it
            if spec["matcher"] is None:
                g.pop("matcher", None)
            else:
                g["matcher"] = spec["matcher"]
            action = "update"
            homed = homed or ordinary
        else:
            orphans.append((hlist, h, ordinary))
    ordinary_orphans = False
    for hlist, h, ordinary in orphans:   # foreign co-tenants stay untouched
        hlist.remove(h)
        if not ordinary:
            # Preserve every opaque policy, but it cannot cover an ordinary
            # orphan: e.g. async delivery is not the canonical synchronous leg.
            relocated = _canonical_entry(spec)
            relocated["hooks"] = [h]
            groups.append(relocated)
        else:
            ordinary_orphans = True
        action = "update"
    if ordinary_orphans and not homed:
        # ONE canonical group, however many orphans were pulled out of shared
        # groups — N relocations must not become N duplicate entries.
        groups.append(_canonical_entry(spec))
    return action


def _merge_permits(out):
    """Merge PERMIT_RULES into permissions.allow IN PLACE -> ok|add. Additive
    only: existing entries (and every sibling permissions key — deny, ask,
    defaultMode …) survive byte-identical; a home with no permissions key
    gains one. Raises ValueError on a shape we must not touch."""
    perms = out.setdefault("permissions", {})
    if not isinstance(perms, dict):
        raise ValueError("existing 'permissions' key is not an object — fix it by hand")
    allow = perms.setdefault("allow", [])
    if not isinstance(allow, list):
        raise ValueError("existing permissions.allow is not a list — fix it by hand")
    missing = [r for r in PERMIT_RULES if r not in allow]
    allow.extend(missing)
    return "add" if missing else "ok"


def _permits_live(settings):
    """Every PERMIT_RULE present in permissions.allow (the post-write check)."""
    perms = settings.get("permissions") if isinstance(settings, dict) else None
    allow = perms.get("allow") if isinstance(perms, dict) else None
    return isinstance(allow, list) and all(r in allow for r in PERMIT_RULES)


def _merge_defaults(out):
    """Merge ESTATE_DEFAULTS into the settings root IN PLACE -> ok|add|update. A
    scalar estate policy: the estate owns these keys, so an absent OR drifted
    value is written to the estate default (the same authority _merge_event has
    over its own hook command). An ABSENT key reads as `add` (as _merge_permits
    does for a missing rule, so a fresh install still aggregates to `add`); a
    PRESENT-but-drifted value reads as `update`. Every sibling key survives
    byte-identical."""
    missing = present = False
    for k, v in ESTATE_DEFAULTS.items():
        if k not in out:
            out[k] = v
            missing = True
        elif out[k] != v:
            out[k] = v
            present = True
    if present:
        return "update"
    return "add" if missing else "ok"


def _defaults_live(settings):
    """Every ESTATE_DEFAULT present at the settings root (the post-write check)."""
    return isinstance(settings, dict) and all(
        settings.get(k) == v for k, v in ESTATE_DEFAULTS.items())


def memory_base(cdir):
    """The MEMORY_BASE_ENV value config dir `cdir` needs, or None when it needs
    none: `projects` is not a symlink, or it links to a dir not named
    `projects` (the variable can only name the PARENT of a `projects` dir)."""
    link = os.path.join(cdir, "projects")
    if not os.path.islink(link):
        return None
    real = os.path.realpath(link)
    return os.path.dirname(real) if os.path.basename(real) == "projects" else None


def _merge_memory(out, cdir):
    """Render MEMORY_BASE_ENV into settings env IN PLACE -> ok|add|update. The
    estate owns this one key (as it owns ESTATE_DEFAULTS): absent -> `add`,
    drifted -> `update`, and a config dir that no longer needs it (its
    `projects` became a real dir) has the key REMOVED -> `update`. Every other
    env key survives byte-identical. Raises ValueError on a shape we must not
    touch."""
    want = memory_base(cdir)
    env = out.get("env")
    if env is None and want is None:
        return "ok"
    if env is None:
        env = out["env"] = {}
    if not isinstance(env, dict):
        raise ValueError("existing 'env' key is not an object — fix it by hand")
    have = env.get(MEMORY_BASE_ENV)
    if have == want:
        return "ok"
    if want is None:
        del env[MEMORY_BASE_ENV]
        return "update"
    env[MEMORY_BASE_ENV] = want
    return "add" if have is None else "update"


def _memory_live(settings, cdir):
    """Does settings env carry exactly the memory base `cdir` needs, and none
    when it needs none? The post-write check, and the doctor rung's question."""
    env = settings.get("env") if isinstance(settings, dict) else None
    have = env.get(MEMORY_BASE_ENV) if isinstance(env, dict) else None
    return have == memory_base(cdir)


def memory_gap_message(kind, row):
    """The one sentence `hooks status` and doctor print for a gap row whose
    `memory` is False."""
    want = row.get("memory_base")
    what = ("settings env lacks %s=%s, so every auto-memory write stops on a "
            "permission prompt" % (MEMORY_BASE_ENV, want) if want else
            "settings env carries %s it no longer needs" % MEMORY_BASE_ENV)
    return ("%s %s: %s (%s) — `helm hooks install` renders it; only new "
            "sessions read it" % (kind, row["label"], what, row["path"]))


def _rooms_clean(settings, owns=None):
    """No helm-owned command names a STALE lane room's helm binary (the
    post-write check, the twin of _lane_live/_permits_live/_defaults_live).

    Through stale_room_tokens, which exempts the derived answer — helm may
    legitimately live under a `wt/` path. Asking `lane_room_tokens` directly
    here is what made verify reject every install on the build node."""
    owns = owns or _ours
    return not any(stale_room_tokens(cmd)
                   for cmd in _all_hook_cmds(settings, repairable=True) if owns(cmd))


def _merge_all(settings, specs=SPECS, path="<settings>", defaults=True, home=None):
    """-> (merged_copy, {spec_name: action}) across `specs` — SPECS for a
    credential home and the identical SEAT_SPECS contract for a seat — plus the
    beacon permit rules (every launch surface must be ABLE to arm the beacon
    without a human prompt), the scalar estate defaults (workflows default-small
    on every home and seat), the auto-memory base (only when `home`, the config
    dir, is given: the answer is read from its `projects` link), and the
    lane-room repair.

    THE ROOM REPAIR RUNS HERE, NOT IN install_home's transform, and that is
    load-bearing: `verify` re-derives the expected candidate by calling this
    function again, so a mutation applied outside it would make every write
    fail verification. One merge function, one answer, both callers.

    It runs LAST so it sees any owned residue left behind by a deliberately
    narrowed caller, which is precisely what _merge_event cannot reach."""
    from . import posttool
    # Preflight protects composite ownership; ineligible standalone contracts
    # retain their existing repair/declaration path. Conversion runs again only
    # after those requested repairs have produced whole eligible contracts.
    requested = resolved_specs(specs)
    out, remaining, actions = posttool.prepare(
        settings, requested, executable=helm_bin())
    if out != settings and not actions:
        # An existing pair can be repointed even when this narrowed caller
        # requests neither member. A real write must not aggregate to 'ok'.
        actions["posttool"] = "update"
    actions.update((s["name"], _merge_event(out, s)) for s in remaining)
    converted, _remaining, covered = posttool.prepare(out, requested, executable=helm_bin())
    if converted != out:
        actions.update({name: "update" for name in covered} or {"posttool": "update"})
    out = converted
    actions["posttool_refusals"] = posttool.refusals(out)
    actions["permits"] = _merge_permits(out)
    actions["defaults"] = _merge_defaults(out) if defaults else "ok"
    actions["memory"] = _merge_memory(out, home) if home else "ok"
    notes = repair_lane_room_commands(out, path)
    actions["rooms"] = "update" if any(n[0] == "repaired" for n in notes) else "ok"
    # A tuple value, never an action word — _agg compares against "fail"/
    # "update"/"add" and must not read this as one.
    actions["rooms_notes"] = tuple(notes)
    return out, actions


def _short_detail(detail, short):
    """Append the shortened note to a detail string, so even a door that only
    ever prints `detail` cannot report a shortened write as a clean one."""
    if not short:
        return detail
    note = ("SHORTENED — %d required guard(s) NOT installed: %s"
            % (len(short), ", ".join(x["name"] for x, _w, _m in short)))
    return (detail + "; " + note) if detail else note


class InstallResult(tuple):
    """(action, detail) PLUS the SHORTENED state of that write.

    SHORTENED IS OWNER STATE OF THE SHARED INSTALL RESULT, produced where the
    resolvable subset is selected and written — not estate-CLI policy. It lived
    in `cmd_hooks` alone, so `hooks install` was loud while a project install, a
    direct home install, `launch --install` and a fresh seat mint all wrote a
    quietly shortened contract, returned rc 0, and the seat mint said
    "wired for full hook contract" while omitting a required guard.

    A TUPLE SUBCLASS so every existing `action, detail = install_*(…)` caller
    keeps working untouched, while any door that must not claim a full contract
    reads `.shortened` off the value it already has. One shape for both
    installers was the explicit alternative to four caller-local patches — the
    same reasoning as one spec list: a claim and its evidence that cannot come
    from different places cannot drift apart."""

    def __new__(cls, action, detail, unresolved=()):
        self = super().__new__(cls, (action, detail))
        self.unresolved = tuple(unresolved)
        return self

    @property
    def action(self):
        return self[0]

    @property
    def detail(self):
        return self[1]

    @property
    def shortened(self):
        """True when a REQUESTED spec could not be written at all."""
        return bool(self.unresolved)

    @property
    def missing_names(self):
        return ", ".join(s["name"] for s, _why, _msg in self.unresolved)

    def note(self, surface):
        """The one sentence a door prints. `surface` names what was written."""
        return ("%s: SHORTENED contract — %d required guard(s) NOT installed "
                "(%s); this is not a full hook contract"
                % (surface, len(self.unresolved), self.missing_names))


def _agg(actions):
    """One home's aggregate action, worst-first (fail > update > add > ok)."""
    for a in ("fail", "update", "add"):
        if a in actions.values():
            return a
    return "ok"


def _install_metadata(result):
    """Action evidence, distinct from winning-snapshot warnings and first intent."""
    if result.get("action") == "dry":
        return (result.get("metadata") or {},)
    return result.get("committed_metadata") or ()


def install_home(path, dry=False, specs=SPECS):
    """Install/refresh one hook estate through bounded content-revision CAS.

    Returns an `InstallResult`: still `(action, detail)` to every existing
    caller, and carrying the REQUESTED specs this host could not resolve so no
    door can report a shortened write as a full contract."""
    from . import configs
    sp = os.path.join(path, "settings.json")
    short = unresolved_externals(specs)

    def transform(cur):
        merged, actions = _merge_all(cur, specs, sp, home=path)
        return merged, {"actions": actions}

    def verify(candidate, before, _metadata):
        expected, _actions = _merge_all(before, specs, sp, home=path)
        return (candidate == expected
                and all(_lane_live(candidate, s) for s in resolved_specs(specs))
                and _permits_live(candidate) and _defaults_live(candidate)
                and _memory_live(candidate, path)
                and _rooms_clean(candidate))

    res = configs.transform_json_file(sp, transform, verify=verify, dry_run=dry)
    if not res.get("ok"):
        return InstallResult("fail", res["error"], short)
    from . import posttool
    actions = (res.get("metadata") or {}).get("actions") or {}
    rooms = "; ".join(filter(None, (
        lane_room_report(actions.get("rooms_notes") or ()),
        posttool.refusal_detail(sp, actions.get("posttool_refusals") or ()))))
    action = _agg({i: _agg(meta.get("actions") or {})
                   for i, meta in enumerate(_install_metadata(res))})
    # `ok` IS RESERVED FOR "I WROTE NOTHING". A room repair rewrites bytes, so it
    # aggregates to `update` even when every spec was already current — do not
    # "optimize away" that as a spurious update. On 2026-08-04 this verb printed
    # `update … 0 failed` for four homes it had only PARTLY fixed, and the
    # residue was found by re-running the DETECTION scan rather than by reading
    # this line. A report that says `ok` while the file changed underneath it is
    # that same disease in its purest form: a claim about intent, not about disk.
    if action == "ok":
        return InstallResult("ok", _short_detail("hook up to date" + ("; " + rooms if rooms else ""), short), short)
    if dry:
        diff = difflib.unified_diff(
            res["before"].splitlines(), res["after"].splitlines(),
            sp, sp + " (after install)", lineterm="")
        return InstallResult("dry-" + action, _short_detail("\n".join(diff) + (("\n" + rooms) if rooms else ""), short), short)
    return InstallResult(action, _short_detail(
        "backup: %s; CAS attempts: %d%s" % (
            res.get("backup") or "none — new file", res["attempts"],
            "; " + rooms if rooms else ""), short), short)


def project_settings_path(project_dir):
    """<project>/.claude/settings.local.json — the per-machine project surface.

    LOCAL, deliberately: the generated commands carry THIS machine's absolute
    helm path, so the file must never ride a commit into someone else's
    checkout. The harness merges local scope last, so a project install is
    additive over user settings, never a replacement of them."""
    return os.path.join(os.path.abspath(os.path.expanduser(project_dir)),
                        ".claude", "settings.local.json")


def install_project(project_dir, dry=False, specs=SPECS):
    """Install/refresh the full hook estate into ONE project — the scoped
    alternative to a home install: every session launched in the project gets
    the physics, every other session on the machine stays hook-free.

    Two deltas from install_home, both deliberate: ESTATE_DEFAULTS are NOT
    seeded (a project file is neither a home nor a seat — helm does not own a
    project's scalar settings), and the target is settings.local.json (see
    project_settings_path). The beacon permits ARE granted, same as a home: a
    fresh session in the project must arm its wake beacon without a human
    prompt. Same CAS pipeline, same merge-preservation laws."""
    from . import configs
    sp = project_settings_path(project_dir)
    short = unresolved_externals(specs)

    def transform(cur):
        merged, actions = _merge_all(cur, specs, sp, defaults=False)
        return merged, {"actions": actions}

    def verify(candidate, before, _metadata):
        expected, _actions = _merge_all(before, specs, sp, defaults=False)
        return (candidate == expected
                and all(_lane_live(candidate, s) for s in resolved_specs(specs))
                and _permits_live(candidate) and _rooms_clean(candidate))

    res = configs.transform_json_file(sp, transform, verify=verify, dry_run=dry)
    if not res.get("ok"):
        return InstallResult("fail", res["error"], short)
    from . import posttool
    actions = (res.get("metadata") or {}).get("actions") or {}
    rooms = "; ".join(filter(None, (
        lane_room_report(actions.get("rooms_notes") or ()),
        posttool.refusal_detail(sp, actions.get("posttool_refusals") or ()))))
    action = _agg({i: _agg(meta.get("actions") or {})
                   for i, meta in enumerate(_install_metadata(res))})
    if action == "ok":
        return InstallResult("ok", _short_detail("hook up to date" + ("; " + rooms if rooms else ""), short), short)
    if dry:
        diff = difflib.unified_diff(
            res["before"].splitlines(), res["after"].splitlines(),
            sp, sp + " (after install)", lineterm="")
        return InstallResult("dry-" + action, _short_detail("\n".join(diff) + (("\n" + rooms) if rooms else ""), short), short)
    return InstallResult(action, _short_detail(
        "backup: %s; CAS attempts: %d%s" % (
            res.get("backup") or "none — new file", res["attempts"],
            "; " + rooms if rooms else ""), short), short)


def _lane_live(settings, spec):
    """A hook lane counts as live ONLY on its complete executable contract.

    Command, type and matcher must match. A gate additionally requires the
    explicit outer deadline that lets its inner timeout report a checked alarm;
    otherwise status would call the exact live unchecked-stop shape covered.
    """
    from . import posttool
    if (spec.get("event") == posttool.EVENT
            and spec.get("name") in posttool.covered_members(
                settings, executable=helm_bin())):
        # Use the planner's requested-policy check too: a healthy default pair
        # must not satisfy a caller's incompatible budget, scope or custom spec.
        try:
            _out, _remaining, actions = posttool.prepare(
                settings, (spec,), executable=helm_bin())
        except ValueError:
            return False
        return actions.get(spec["name"]) == "ok"
    hooks = settings.get("hooks") if isinstance(settings, dict) else None
    groups = hooks.get(spec["event"]) if isinstance(hooks, dict) else None
    cmd = spec_command(spec)
    outer_timeout = _gate_outer_timeout(spec)
    for g in groups if isinstance(groups, list) else []:
        if not (isinstance(g, dict) and _matcher_ok(g, spec)):
            continue
        for h in g.get("hooks") or []:
            if isinstance(h, dict) and h.get("command") == cmd \
                    and h.get("type") == "command" \
                    and (outer_timeout is None
                         or h.get("timeout") == outer_timeout):
                return True
    return False


def stale_specs(settings, specs=SPECS):
    """Owned specs whose installed rendering differs from canonical. -> names,
    or None when the answer cannot be taken.

    Presence is not currency. Reuse `_merge_all`, the same canonical producer
    install and verify call, on a deep copy. An `update` means the entry exists
    and would be rewritten; `add` means absent, which the presence columns
    already report and must not be mislabeled as drift.
    """
    try:
        current = resolved_specs(specs)
        _merged, actions = _merge_all(settings, current)
    except Exception:                          # noqa: BLE001
        return None                            # unreadable is not clean
    return sorted(spec["name"] for spec in current
                  if actions.get(spec["name"]) == "update")


def status_rows():
    """Per-claude-home coverage: inject hook present? helm resolvable?
    fail-open contract present? Is the installed rendering current? Plus every
    remaining hook lane — exact command + matcher validated, never marker
    presence. Read-only."""
    from . import pk
    rows = []
    for row in home_gap_rows():
        cmd, settings, readable = None, {}, False
        path = os.path.join(row["path"], "settings.json")
        try:
            with pk.open_regular(path, encoding="utf-8") as f:
                settings = json.load(f)
            readable = True
            cmd = next((c for c in _hook_cmds(settings) if _ours(c)), None)
        except FileNotFoundError:
            readable = not os.path.lexists(path)  # absent is known; broken link is not
        except (OSError, ValueError):
            pass
        from . import posttool
        drifted = stale_specs(settings) if readable else None
        rows.append(dict(row, home=row["label"], hook=bool(cmd), command=cmd,
                         refusal_detail=posttool.refusal_detail(row["path"], posttool.refusals(settings)),
                         posttool_refusals=posttool.refusals(settings),
                         resolvable=bool(cmd) and _resolvable(cmd),
                         fail_open=bool(cmd) and _fail_open(cmd),
                         # `wrapper` rides in from the `_gap_row` above, which
                         # measures it over EVERY command in the config. It was
                         # recomputed here from the inject entry alone, which
                         # made this the only surface that could see the state
                         # at all and made it see only one lane's worth of it.
                         drifted=drifted))
    return rows


def carried_names(row):
    """Spec names this config CARRIES, at ANY rendering vintage.

    PRESENCE IS NOT CURRENCY, and reading one as the other made the whole
    estate report itself un-wired the moment a template changed. `_lane_live`
    matches the installed command EXACTLY, so every entry helm wrote before the
    change became `missing` — and every count built on `missing` then said
    "inject coverage 0 of 7", "delivery lane 0 of 7", "continuity lane 0 of 7"
    about hooks that were demonstrably firing on every tool call. A console or
    a doctor reading those numbers sees a fleet-wide outage where the true
    state is "the same hooks, an older spelling of the same ladder".

    `stale_specs` ALREADY draws the line — an `update` action means the entry
    EXISTS and would be re-rendered, an `add` means it is absent — and the
    MISSING row already subtracted it. Only the COUNTS did not, so the table
    said `deliver yes` on the same screen as `delivery lane 0 of 7`. This is
    the one door all of them read now, so the three cannot disagree again.

    An UNREADABLE config (`drifted` None) carries nothing here: not knowing is
    not presence, and a count must never round an unknown up.
    """
    drifted = row.get("drifted")
    return set(drifted) if isinstance(drifted, list) else set()


def missing_names(row):
    """The specs this config genuinely lacks — absent, not merely older."""
    return [n for n in row.get("missing", ()) if n not in carried_names(row)]


def lane_live(row, name):
    """Does this config carry `name` at all? Exact rendering or an older one."""
    return bool(row.get(name)) or name in carried_names(row)


def drifted_rows(rows):
    """The rows whose entries work but are not the rendering helm ships now."""
    return [r for r in rows if r.get("drifted")]


def inject_covered(rows):
    """How many of `rows` carry the NARROW inject lane — derived FROM the rows
    the caller already holds, never from a fresh scan.

    ONE SNAPSHOT, BOTH CONSUMERS. `hooks status` printed a table from one scan
    and its counts from a SECOND scan inside `coverage()`, so a live estate
    racing between the two produced a count over a population the table never
    showed — "guard 1 of 2" beside no MISSING row, each half true and the pair
    incoherent. Reading the same thing twice and letting the readings disagree
    is its own bug class, and the cure is structural: derive, do not re-read."""
    # COVERAGE IS PRESENCE, NEVER VINTAGE — see `carried_names`. Dropping a
    # home whose inject entry is merely an older rendering makes every deploy
    # read as a coverage outage until someone runs `helm hooks install`, about
    # hooks that are firing on every turn. The drift is reported on its own row
    # and on its own doctor rung, where it can be acted on without claiming the
    # seat is running unhooked. `None` (unreadable) is still not a clean
    # answer: not knowing is not coverage.
    # AND A LADDER THAT IS NOT THERE IS NOT COVERAGE, which is a different
    # question from vintage. A drifted entry RUNS; a home whose `helm-hook` is
    # gone runs nothing at all — every hook in it exits 127 and the harness
    # reads ALLOW — so counting it was this census reporting an estate that
    # could not fire a single hook as fully wired. Measured in a temp HOME
    # whose commands named a wrapper that was not on disk: `inject coverage:
    # 1 of 1 claude homes`. `None` (no wrapper named — an inline vintage) is
    # not a gap: that rendering never needed the file.
    return sum(1 for r in rows
               if r["hook"] and r["resolvable"] and r["fail_open"]
               and r.get("wrapper") is not False
               and isinstance(r.get("drifted"), list))


def coverage():
    """(covered, total) claude homes — covered = inject present, resolvable
    and fail-open. One scan, both numbers off it. Currency is `drifted_rows`:
    an older rendering of a working ladder is a re-render owed, not a gap."""
    rows = status_rows()
    return inject_covered(rows), len(rows)


def preflight(config_dir, specs=None, apply=True):
    """(rc, message) — THE ONE runtime answer to "may a session start here?".

    THE GENERATED launch.sh CALLS THIS. It used to carry a static shell
    reimplementation of the rule — `[ -x "$pin" ]` plus `command -v` — and that
    was a SECOND RESOLVER, which is the disease this lane has now produced
    three times: two implementations of one question, which diverge. Measured
    divergences, every one of them a session that started while the canonical
    resolver said refuse:

      * a RELATIVE pin       -> external_status "relative-pin", shell rc 0
      * an executable DIR    -> external_status "pin-dead",     shell rc 0
      * a relative PATH entry-> external_status "absent",       shell rc 0
      * shortened mint, then a valid guard installed -> external_status "ok"
        and the shell rc 0, while the guard was STILL ABSENT from the config,
        because a static shell check cannot know what is in settings.json

    And a fifth, of staleness rather than logic: generated text predates a
    newly-added external spec, so the asset enforces last month's contract.
    Reading SEAT_SPECS here, at run time, is what ends that class — the asset
    carries a CALL, never a copy of the rule.

    Three things, in order, because each can pass while the next fails:
      1. RESOLVE  — canonical `unresolved_externals` over current SEAT_SPECS.
      2. REFRESH  — re-install, so a config written during a shortened mint is
                    completed the moment the guard exists.
      3. VERIFY   — `_lane_live` per spec against the file ON DISK, so what is
                    asserted is the config the session will actually load, not
                    the write we believe we just made.

    FAIL-CLOSED, unlike every hook spec in this module: those must never hold a
    turn hostage, but this one decides whether a session begins at all, and an
    unverifiable answer is a refusal.
    """
    from . import pk
    specs = SEAT_SPECS if specs is None else specs
    short = unresolved_externals(specs)
    if short:
        return 1, ("REFUSED — %d required guard(s) cannot run on this host "
                   "(%s); a session started here would be UNGUARDED. %s"
                   % (len(short), ", ".join(x["name"] for x, _w, _m in short),
                      " ".join(m for _x, _w, m in short)))
    if apply:
        res = install_home(config_dir, specs=specs)
        if res.action == "fail":
            return 1, ("REFUSED — the seat config could not be refreshed (%s); "
                       "refusing to start a session against a config this "
                       "process cannot verify" % res.detail)
        if res.shortened:
            return 1, "REFUSED — " + res.note(config_dir)
    settings, err = {}, None
    try:
        with pk.open_regular(os.path.join(config_dir, "settings.json"),
                             encoding="utf-8") as f:
            settings = json.load(f)
    except (OSError, ValueError) as e:
        err = e
    if err is not None:
        return 1, ("REFUSED — %s/settings.json is unreadable (%s); an "
                   "unverifiable config is not a guarded one"
                   % (config_dir, err))
    missing = [sp["name"] for sp in resolved_specs(specs)
               if not _lane_live(settings, sp)]
    if missing:
        return 1, ("REFUSED — %d required hook(s) absent from %s after refresh "
                   "(%s); a session started here would be UNGUARDED"
                   % (len(missing), config_dir, ", ".join(missing)))
    return 0, ""


def seat_status_rows():
    """(rows, unread) — per-seat full hook-contract coverage: does the seat's
    claude/settings.json carry every SEAT_SPECS entry (exact command + matcher +
    type validated, never marker presence — same _lane_live law as homes)?
    Read-only. `unread` is seat_homes' completeness, carried through untouched:
    rows describe the seats the census SAW, and every consumer printing them
    owes its reader the subtrees it could not. (An unreadable settings.json
    inside a VISIBLE seat needs no such channel — that row ships with every
    lane False, an uncovered seat, which is loud.)"""
    found, unread = seat_homes()
    return ([dict(_gap_row(name, path, SEAT_SPECS), seat=name)
             for name, path in found], unread)


def _gap_row(label, cdir, specs):
    """ONE read of ONE config dir, from which the table, the counts AND the
    audit all derive: {label, path, permits, missing, stale, <lane booleans>}.

    EVERYTHING DOWNSTREAM READS THIS ONE ROW. `hooks status` used to recompute
    its coverage claim inline over raw SPECS while the audit measured the
    RESOLVED set — two lists answering one question, which is exactly how "10
    of 10 seats covered" got printed over two unguarded seats. A claim and its
    audit that cannot come from different lists cannot drift.

    `missing`: a RESOLVABLE required spec whose exact entry is not live — the
    gap `hooks install` closes.

    `stale`: an UNRESOLVABLE external whose own-marker IS in the file. The
    config claims that guard and this host cannot run it, so the entry is a
    claim rather than coverage. It is kept apart from `missing` because the two
    have different cures — install writes the first, only the machine fixes the
    second — and because collapsing them would let a file's own claim stand as
    evidence the guard works, which is the shape task/1006 is made of."""
    from . import pk
    settings = {}
    try:
        with pk.open_regular(os.path.join(cdir, "settings.json"), encoding="utf-8") as f:
            settings = json.load(f)
    except (OSError, ValueError):
        pass
    owned = set(_owned_specs(settings))
    lanes = {s["name"]: _lane_live(settings, s) for s in resolved_specs(specs)}
    return {"label": label, "path": cdir, "permits": _permits_live(settings),
            # The auto-memory base (MEMORY_BASE_ENV): False names a config dir
            # whose memory writes stop on a permission prompt until install
            # renders it. `memory_base` rides along so the row can say what.
            "memory": _memory_live(settings, cdir),
            "memory_base": memory_base(cdir),
            "missing": [n for n, live in lanes.items() if not live],
            "stale": [s["name"] for s in specs
                      if s.get("external") and external_bin(s) is None
                      and s["name"] in owned],
            # THE LADDER ITSELF, measured here so every consumer of this row
            # inherits it. A config can carry every required guard, resolve
            # every helm, satisfy the fail-open contract — and still have
            # nothing executable at the wrapper each of those commands names,
            # in which case every hook in it exits 127 and the harness reads
            # ALLOW. `missing` and `stale` are about the ENTRIES; this is
            # about the file they all run.
            "wrapper": _wrapper_state(_all_hook_cmds(settings)),
            **lanes}


def seat_gap_rows():
    """([row, …], unread) — the per-seat contract audit doctor renders, one
    `_gap_row` per seat. A seat with the complete contract still ships a row,
    because "which seats did you actually look at" is half the answer:
    task/1006's whole shape was a census that printed `10 of 10 seats covered`
    while two of those seats carried no suite guard at all — the lane was
    absent from every list, so nothing could report it missing.

    `unread` rides along untouched: rows describe the seats the census SAW."""
    return seat_status_rows()


def home_gap_rows():
    """The same audit for CREDENTIAL HOMES, read from the same spec list.

    A home is a full launch surface too — the owner's own `~/.claude` is one —
    so a guard missing there is exactly as unguarded as a seat, and it went
    unreported for the first cut of this rung. One function, both surfaces: a
    guard added to SPECS is audited on every surface at once, which is the
    property that keeps this from decaying into two hand-kept enumerations."""
    return [_gap_row(name, path, SPECS) for name, path in claude_homes()]


def seat_coverage():
    """(covered, total, unread) seats — covered = the full SEAT_SPECS contract
    live in the seat's claude config dir. When `unread` is non-empty BOTH counts
    are a FLOOR over an incomplete census — "N of N" beside a non-empty unread
    is never "healthy", it is "healthy among the seats the walk could read".
    Consumers must print the third element or they are task/331's instrument
    again."""
    rows, unread = seat_status_rows()
    return (covered_count(rows), len(rows), unread)


def _saguide_exclusion():
    """The SA-coverage caveat, READ FROM saguide so this surface can never
    drift into a friendlier version of the boundary than the seam itself
    states. Fail-open to a bare truth if the import is unavailable."""
    try:
        from .saguide import COVERAGE_EXCLUSION
        return COVERAGE_EXCLUSION
    except Exception:
        return "some subagent kinds are out of reach of this seam"


def covered_count(rows):
    """How many `_gap_row`s carry the full contract — the ONE reading every
    consumer takes. A seat with a STALE entry is NOT covered: the file names a
    guard this host cannot run, and counting it would let a config's own claim
    stand in for the guard working.

    AN OLDER RENDERING IS NOT A GAP (`missing_names`). A stale entry and a
    drifted one are opposite failures and only one of them is an absence: STALE
    means the config names something this host cannot run, DRIFTED means it
    names something that runs and is not the current spelling.

    AND A CONFIG WHOSE LADDER IS GONE IS NOT COVERED, the same subtraction
    `inject_covered` makes and for the same reason: every entry in it exits
    127 before a line runs and the harness reads ALLOW. The two counters
    disagreeing is not a rounding difference, it is one screen saying both
    things — `hooks status` printed "seat hooks: 2 of 2 seats" directly under
    its own line naming those two seats as running nothing. `None` (an inline
    vintage that names no wrapper) is not a gap, and a hand-built row with no
    `wrapper` key at all is left alone."""
    return sum(1 for r in rows if not missing_names(r) and not r["stale"]
               and r.get("wrapper") is not False)


# ── the retrofit surface (G-seatlaunch-installs): a pane that launched
# BEFORE its identity/hooks existed sits idle forever — no hook ever fires
# in an idle PTY, so it can never self-heal into delivery (the live kimi
# seat: running, absent from the roster, its mentions routing nowhere). The only
# fix is a relaunch, so install/launch SURFACE the uncovered running panes.

def running_panes(proc=None):
    """[{pid, seat, config_dir, family}] for every live claude-harness PTY
    PROVEN to belong to this uid: /proc scan — cmdline argv0/argv1 basename
    'claude', environ read for HELM_CHAT_NAME / CLAUDE_CONFIG_DIR (a seat
    config dir names its family). HELM_PROC overrides the proc root (tests);
    fail-open [].

    MEMBERSHIP REQUIRES PROOF OF OWNERSHIP, and that phrasing is the fix. This
    docstring used to say "other-uid entries are unreadable and skipped", which
    was true only INCIDENTALLY: both reads sat in one try and another user's
    EACCES environ hit a bare `continue`. The cure for the vanishing-pane bug
    removed that `continue` — and in doing so turned an incidental guarantee
    into a FALSE CLAIM. Another user's claude has a world-readable comm and an
    EACCES environ, so it passed detection, failed the environ read, and
    entered OUR snapshot as OUR blind pane: a foreign process permanently
    tainting local coverage and project-census certainty. A second read
    found it, and the fix that caused it is earlier in this same lane.

    So ownership is now PROVEN by st_uid rather than inferred from a read
    failure, and it fails CLOSED: a pid whose owner cannot be established is
    excluded. That asymmetry is deliberate. Admitting a foreign pane taints
    every downstream certainty claim, while excluding an unprovable one can
    only lose a process whose /proc entry we could not stat — which on Linux
    procfs means it is gone, since the directory metadata is world-readable
    even when its contents are not.

    THE SCAN OF ONE PID IS BRACKETED BY ITS INCARNATION TOKEN, and the order
    is load-bearing: `beacons.proc_starttime` is read FIRST, before the
    ownership check, so the ownership proof and every read below it belong to
    the same incarnation. Taking it after left the proof OUTSIDE the bracket
    — the pid-7443 case, where ours() observes same-uid process A, a
    foreign-uid claude B replaces it before that check returns, and token,
    reads and re-token then agree coherently about B. Every mechanism honest,
    a foreign pane admitted.

    FOUR EXCLUSIONS, EACH FOR ITS OWN REASON, none of them silent-by-accident:
      not ours       st_uid != our uid — a real process, just not this
                     census's
      gone           the /proc entry is absent when ownership is asked, at
                     the top of the scan or again in `emit` for an
                     uncertainty row; a departed process was never a pane
      owner unknown  stat failed for any other cause — unprovable, excluded
      recycled       the incarnation token CHANGED across the scan, so the
                     reads describe two processes and the row would be true
                     at no instant

    AND ONE DOWNGRADE, WHICH IS NOT AN EXCLUSION. If the token cannot be read
    at either end, the incarnation is UNPROVABLE — and a missing pin must not
    validate derived fields, because None == None is two failures rather than
    agreement and production /proc can withhold stat. The row then keeps only
    what the evidence supports: a process is here and we could not establish
    what it is. Never silently dropped, which is this file's founding defect,
    and never silently trusted. It carries `incarnation_unknown` — ITS OWN
    cause bit, not the detector's, because cmdline and comm may both have
    answered perfectly and only the pin failed; borrowing `detect_unknown`
    made both operator surfaces blame reads that worked.

    A DETECTOR THAT CANNOT READ IS NOT A DETECTOR THAT SAID NO. When BOTH
    cmdline and comm fail on a process that is ours, `is_agent(None, None)`
    returns False for the same reason it does for vim, and the pid used to
    vanish — an absence claim from a read that never happened, the same
    defect one layer up. Such a pid is carried as UNCLASSIFIED, so the
    consumers' existing UNKNOWN buckets report it. A process that ANSWERED
    and is not claude is a judgement and still leaves.

    OWNERSHIP REPROOF LIVES IN `emit`, NOT IN ANY READ'S FAILURE ARM. It
    applies to UNCERTAINTY rows only — a read that FAILED tells us nothing,
    so a row built on one must re-prove present-and-ours; a read that
    ANSWERED is valid snapshot evidence and gets no freshness recheck. The
    test reads the ROW rather than a caller's flag, so a future arm emitting
    an unreadable row inherits the seam without asking for it."""
    proc = proc or home.env("PROC") or "/proc"
    out = []
    try:
        pids = sorted(n for n in os.listdir(proc) if n.isdigit())
    except OSError:
        return out
    from . import beacons, cell
    sroot = os.path.realpath(os.path.join(home.global_dir(), "seats"))
    our_uid = os.getuid()

    def ours(base):
        """Is this /proc entry PRESENT and owned by us? One definition,
        asked at two moments — before the reads and again after an environ
        failure, because a pid can be recycled in between.

        Defined once on purpose. Two copies of this predicate is two readers
        of one question, which is the defect class this whole lane is about;
        they would drift, and the drift would be invisible because each copy
        is individually correct."""
        try:
            return os.stat(base).st_uid == our_uid
        except OSError:
            return False

    def emit(pid, base, token, row):
        """THE ONLY WAY A ROW LEAVES THIS SCAN, and it enforces the two
        properties a row needs to be true at some instant.

        (1) INCARNATION COHERENCE, for EVERY row. A scan of one pid is
        several reads, and the kernel can hand that pid to a different
        process between any two of them. A probe built it: pid 7441 answers
        [claude] to proc_argv, is replaced by vim with its own
        HELM_CHAT_NAME, and answers vim to proc_comm — ALL READS SUCCEED, and
        the row emitted is a claude pane wearing vim's environ. A row true at
        NO INSTANT. `beacons.proc_starttime` is helm's canonical anti-reuse
        token, so the scan is bracketed by it: same token, one incarnation,
        and the reads describe one process.

        THIS IS COHERENCE, NOT FRESHNESS, and the distinction is why it
        applies to successful reads too. It does not claim the process is
        still alive — it claims the evidence is about a single process. A row
        that was never simultaneously true was never evidence at all.

        (2) OWNERSHIP REPROOF, for UNCERTAINTY rows only.
        A read that FAILED tells us nothing, so a row built on one must
        re-prove the process is still there and still ours; otherwise an
        ordinary exit mid-scan becomes a standing "coverage UNKNOWN, do NOT
        relaunch" row about a pid nobody can look at. A read that ANSWERED is
        valid snapshot evidence and gets NO freshness recheck: it was true
        when taken, which is all a scan of a live system can offer.

        THE UNCERTAINTY TEST READS THE ROW, NOT A CALLER'S FLAG. A future arm
        that emits an unreadable row gets the seam without its author
        remembering to ask for it — which is the difference between a class
        that is closed and one that merely has no known members today.
        """
        now = beacons.proc_starttime(int(pid), proc)
        if token is None or now is None:
            # THE PIN IS UNAVAILABLE, AND A MISSING PIN MUST NOT VALIDATE
            # DERIVED FIELDS. None == None is not agreement, it is two
            # failures — and production /proc can withhold stat. Every field
            # below was derived from reads this scan cannot bind to one
            # incarnation, so the identity goes and the row carries only what
            # the evidence actually supports: there is a process here and we
            # could not establish what it is. Never silently dropped, which
            # is this lane's founding law, and never silently trusted either.
            # ITS OWN CAUSE BIT, NOT detect_unknown. The instrument that
            # failed here is the INCARNATION PIN — cmdline, comm and environ
            # may all have answered perfectly. Reusing the detector's bit
            # made both surfaces tell an operator this process "refused both
            # identity reads", which is false and sends them to the wrong
            # place. Same bucket and same remedy; a different diagnosis needs
            # a different bit and its own sentence.
            row = {"pid": row["pid"], "seat": None, "config_dir": None,
                   "family": None, "environ_unreadable": True,
                   "incarnation_unknown": True,
                   "signer_bin": None, "signer_profile": None,
                   "session": None}
        elif now != token:
            return                       # a different process wore this pid
        if (row.get("environ_unreadable") or row.get("detect_unknown")
                or row.get("incarnation_unknown")):
            if not ours(base):
                return
        out.append(row)

    for pid in pids:
        base = os.path.join(proc, pid)
        # OWNERSHIP FIRST, and the order is load-bearing: it bounds every check
        # below to processes we can actually reason about. Run detection first
        # and the UNCLASSIFIED arm would carry every unreadable process on a
        # multi-user box into a report about OUR fleet.
        # THE INCARNATION TOKEN IS TAKEN FIRST, BEFORE THE OWNERSHIP READ,
        # because the ownership proof has to live INSIDE the bracket to mean
        # anything. The pid-7443 repro: ours(base) observes same-uid
        # process A, A is replaced by a FOREIGN-uid claude B before it
        # returns, and token/reads/re-token then agree coherently about B.
        # The bracket was truthful and the ownership proof belonged to a
        # different incarnation, so a foreign pane entered our census with a
        # clean bill of health. Taking the token first makes any replacement
        # anywhere below fail the re-check.
        token = beacons.proc_starttime(int(pid), proc)
        if not ours(base):
            continue
        # DETECTION DELEGATES TO beacons, THE CANONICAL AGENT DETECTOR. This
        # scan used to hand-roll `basename(argv[0..1]) == "claude"`, a SECOND
        # census beside the one helm already owns — the same duplication that
        # made gate.py count 434 panes on a box holding twelve.
        argv = beacons.proc_argv(int(pid), proc)
        comm = beacons.proc_comm(int(pid), proc)
        if not beacons.is_agent(argv, comm):
            # BOTH READS FAILED IS NOT A NO. is_agent(None, None) is False and
            # is indistinguishable here from a genuine non-agent, so the two
            # are separated by the EVIDENCE rather than by the answer: a pid of
            # ours, not proven gone, whose cmdline AND comm both refused to be
            # read, is UNCLASSIFIED and stays in the census under the consumers'
            # UNKNOWN buckets. A process that answered and simply is not claude
            # is a clean no and leaves.
            if argv is not None or comm is not None:
                continue
            # AND THE SAME QUESTION AGAIN, for the same reason as the environ
            # arm below — which is exactly why the predicate has a name. This
            # branch appended IMMEDIATELY, so a process that exited BETWEEN
            # the ownership stat and the detector reads became a permanent
            # UNCLASSIFIED row about a pid that is gone, and an operator
            # cannot go look at a pid that no longer exists.
            #
            # A second read found it and the shape is worth naming: the cure I
            # wrote for the environ path did not cover the arm the cure
            # ITSELF introduced. A new branch is a new place for the class to
            # live, and it does not inherit the fix from its sibling.
            emit(pid, base, token,
                 {"pid": int(pid), "seat": None, "config_dir": None,
                  "family": None, "environ_unreadable": True,
                  "detect_unknown": True,
                  "signer_bin": None, "signer_profile": None,
                  "session": None})
            continue
        # THE ENVIRON IS READ SEPARATELY, AND FAILING TO READ IT NO LONGER
        # DELETES THE PANE. Both reads used to sit in one try with a bare
        # `continue`, so a pane whose environ is unreadable — another uid, a
        # hardened proc, a process exiting mid-scan — VANISHED from a surface
        # whose entire job is naming panes that cannot self-heal. The operator
        # then read a short clean list as "all covered": an absence claim the
        # read never supported. beacons.agent_index already states this law in
        # its own docstring — an unreadable pane "is recorded as declaring
        # NOTHING, which is what it is" — and this scan simply did not follow
        # it. Detection keys on comm/argv, which stay readable.
        env, environ_unreadable = {}, False
        try:
            with open(os.path.join(base, "environ"), "rb") as f:
                env = dict(kv.split(b"=", 1)
                           for kv in f.read(1 << 20).split(b"\0") if b"=" in kv)
        except OSError:
            # A PROCESS THAT EXITED MID-SCAN IS GONE, NOT BLIND. The two look
            # identical from here — both are an OSError on the environ — and
            # conflating them turns every ordinary exit between the ownership
            # stat above and this read into a permanent "coverage UNKNOWN,
            # look, do NOT relaunch" row about a pid that no longer exists.
            # The distinction is EVIDENCE, not inference: ask whether the
            # /proc entry is still there. Present means the read genuinely
            # failed and the pane is blind; absent means it left, and a
            # departed process was never a pane this report is about.
            #
            # This is the same instrument as the ownership check, deliberately
            # — a second liveness subsystem would be a second census, which is
            # the duplication the detector delegation above exists to end.
            #
            # AND IT RE-ASKS OWNERSHIP, NOT MERELY EXISTENCE, because a bare
            # exists() reopens the very hole this commit closes: if the pid is
            # RECYCLED between the ownership stat and here, exists() says yes
            # about a DIFFERENT process — possibly another user's — and it
            # re-enters as our blind pane. Rare, and rarity is not a property
            # anything enforces. The same two questions asked once are the
            # same two questions asked twice.
            environ_unreadable = True

        def val(k):
            v = env.get(k.encode())
            return v.decode("utf-8", "replace") if v else None

        # THE ASYMMETRY THIS COMMENT USED TO DEFEND IS GONE, and saying so is
        # the honest version. It argued that only the UNCERTAIN rows needed a
        # liveness recheck, because a successful environ read is a FACT about
        # a process that existed a moment ago. That reasoning is fine and it
        # bought a rule with an exception in it — which is exactly what the
        # gap-by-gap cure kept costing us. `emit` asks once for EVERY row, so
        # the function named running_panes reports only processes that were
        # still running when the row left it. One rule, no exception, and a
        # guard can then enforce it.
        # Seat classification is beacons.seat_of_config_dir — ONE definition
        # of "which seat does this config dir declare", shared with
        # declared_seats. The inline two-dirname check it replaces was
        # task/331's second blindness: exactly one level deep, so a slice-6
        # instance pane (seats/<family>/instances/<seat>/claude) classified
        # as family=None and rode only on its HELM_CHAT_NAME stamp.
        cdir = val("CLAUDE_CONFIG_DIR")
        family = beacons.seat_of_config_dir(
            os.path.realpath(cdir), sroot) if cdir else None
        emit(pid, base, token,
             {"pid": int(pid), "seat": val("HELM_CHAT_NAME") or
                    val("MELD_CHAT_NAME"), "config_dir": cdir, "family": family,
                    # UNKNOWN identity, not absent pane. Consumers must not
                    # read `seat is None` here as "an unnamed home pane" when
                    # it means "we could not look".
                    "environ_unreadable": environ_unreadable,
                    # signing readiness rides the scan (the environ is already
                    # in hand): a pane launched before launch_line baked the
                    # trio posts [unsigned] by configuration and cannot
                    # self-heal — surface it, never let it break silently.
                    "signer_bin": val("HELM_CELL_BIN") or val("MELD_CELL_BIN"),
                    # THE GATE'S EXACT RULE (cell.signer_profile): the first
                    # NON-BLANK value in cell.PROFILE_ENV order, stripped, so
                    # the profile this row names is the one helm would sign
                    # with — a blank HELM_CELL_PROFILE falls through, as it
                    # does in the gate.
                    "signer_profile": next(
                        (v for v in ((val(k) or "").strip()
                                     for k in cell.PROFILE_ENV) if v), None),
                    # THE HARNESS SESSION ID, in home.session_id's order, when
                    # the pane's own environ carries one (a proxy/codex pane
                    # may; a claude pane's does NOT — Claude Code sets it only
                    # for its children, measured across every live pane, so
                    # `unsigned_panes` falls back to the pid-keyed session
                    # record). Only the signing report reads it (task/3049).
                    "session": next((val(k) for k in home._SESSION_ENV
                                     if val(k)), None)})
    return out


def uncovered_panes(proc=None, quiet_s=900, panes=None):
    """Running claude PTYs whose NAMED chat identity is not live on the
    roster (+reason) — covered = the pane's seat name (HELM_CHAT_NAME, else
    its seat family) has a roster row seen within quiet_s. Un-named home
    panes are NOT judged here: their auto-name binds via session id, which
    a /proc scan cannot see — their coverage check is `helm hooks status`
    (the hooks ARE the join path). Read-only; fail-open []."""
    try:
        from . import seats
        scanned = running_panes(proc) if panes is None else panes
        # A PANE WE COULD NOT READ IS REPORTED, NEVER FILTERED. It has no seat
        # and no family for the same reason a home pane has none — because the
        # environ was unreadable, not because it is unnamed — and the old
        # filter dropped both alike. UNKNOWN coverage is the honest verdict:
        # the operator gets a pid to look at instead of a silently shorter
        # list. This is the failure direction that loses panes; the roster
        # join below is the one that only costs a line.
        blind = [p for p in scanned if p.get("environ_unreadable")]
        panes = [p for p in scanned
                 if not p.get("environ_unreadable") and (p["seat"] or p["family"])]
        now, r, out = time.time(), seats.roster(), []
        for p in blind:
            p["coverage_unknown"] = True
            # THE REASON NAMES WHAT ACTUALLY REFUSED, because the two causes
            # send an operator to different places. "environ unreadable" says
            # go look at the environ; a pane whose cmdline AND comm also
            # refused is a process that answered NOTHING, and the thing to
            # look at is the process. Both land in this same UNKNOWN bucket —
            # correctly, since the remedy is identical — but a bucket is not
            # a diagnosis, and the row is the only place the difference can
            # still be told.
            p["reason"] = (
                "coverage UNKNOWN — this process could not be pinned to one "
                "incarnation (no start-time), so nothing ties the identity we "
                "read to the process we read it from"
                if p.get("incarnation_unknown") else
                "coverage UNKNOWN — this process refused both identity reads "
                "(cmdline and comm), so it could not even be classified as a "
                "pane"
                if p.get("detect_unknown") else
                "coverage UNKNOWN — environ unreadable, so this pane's "
                "identity could not be read at all")
            out.append(p)
        for p in panes:
            name = p["seat"] or p["family"]
            if not seats._SEAT_TOKEN.fullmatch(name):
                # a hostile pane name — refused by identity so the operator
                # identifies the PROCESS by pid, never by a laundered alias
                p["identity_refused"] = True
                p["reason"] = "identity refused — not a valid seat token"
                out.append(p)
                continue
            row = r.get(name)
            ls = seats.last_seen(name, row) if row else None
            if row and ls and now - ls < quiet_s:
                continue
            # the seat name is a raw HELM_CHAT_NAME (from /proc environ, the
            # unvalidated join seam) — launder it INTO the reason so the
            # printed line cannot reshape the operator's terminal.
            lbl = seats._seat_label(name)
            p["reason"] = ("no roster row for '%s' — never joined" % lbl
                           if not row else "roster row for '%s' is stale "
                           "(%.0fm quiet)" % (lbl, (now - (ls or 0)) / 60))
            out.append(p)
        return out
    except Exception:
        return []


def _pane_sessions():
    """{pid: session id} for the claude panes that hold one open, from the
    pid-keyed session records (`sessions.live_sids`), or {} when that read
    fails. A claude pane's own environ does not carry its session id — Claude
    Code sets it only for the processes it starts (measured across every live
    pane) — so the signing report reads it where it IS written."""
    try:
        from . import sessions
        return {pid: sid for sid, pid in (sessions.live_sids() or {}).items()}
    except Exception:                    # noqa: BLE001 — fail-open report
        return {}


def unsigned_panes(proc=None, panes=None):
    """Running NAMED panes that cannot sign their posts: HELM_CELL_BIN unset
    or pointing at a non-executable (cell.bin_ready's law), or the profile
    pair missing (DREGG_PROFILE/HELM_CELL_PROFILE) — launch_line has carried
    the trio since seat-signing landed, so such a pane predates it and posts
    [unsigned] by configuration. A pane whose profile is not its own
    HELM_CHAT_NAME is flagged too, exactly when the signing gate refuses it:
    its seat is a fleet actor, or the roster cannot be read. Read-only;
    fail-open [].

    A BLIND PANE IS SIGNING-UNKNOWN, NEVER SILENTLY HEALTHY. Its environ is
    unreadable, so the trio cannot be read either — and the first cut left it
    out of this bucket on the reasoning that it "belongs in neither". Every
    clause of that was true and the conclusion was wrong: absent from the
    unsigned bucket RENDERS AS CAN-SIGN, which is a claim the read never
    supported. Measured on a planted pane carrying a full trio behind an
    unreadable environ: reported by nothing at all. So it now comes back
    flagged, and `surface_uncovered` prints it under its own non-destructive
    header. (Converged in a
    convergence meld; I constructed the case only after writing the wrong
    reasoning down.)

    A PANE THAT SIGNS AS ITSELF DESPITE THE OWNER'S EXPORT IS INFO, NOT A
    RELAUNCH (task/3049). A seat started outside `helm launch` inherits the
    owner's profile from his shell; the gate now sets that profile aside and
    signs as the seat when its session is bound to its row. Such a pane comes
    back with `sign_info` set, and `surface_uncovered` prints it under a
    no-action header: telling an operator to relaunch a pane that signs would
    spend its context on nothing.
    """
    try:
        from . import cell
        out, roster, pane_sids = [], None, None
        for p in (running_panes(proc) if panes is None else panes):
            if p.get("environ_unreadable"):
                p["sign_unknown"] = True
                # Same bucket, same remedy, different diagnosis — see the
                # matching branch in uncovered_panes. A pane that answered no
                # read at all did not merely hide its signer trio.
                p["sign_reason"] = (
                    "signing UNKNOWN — this process could not be pinned to "
                    "one incarnation, so a signer trio read from it would "
                    "describe an unproven process"
                    if p.get("incarnation_unknown") else
                    "signing UNKNOWN — this process refused its identity "
                    "reads, so the signer trio could not be reached"
                    if p.get("detect_unknown") else
                    "signing UNKNOWN — environ unreadable, so the signer "
                    "trio could not be read")
                out.append(p)
                continue
            if not (p["seat"] or p["family"]):
                continue
            b = p.get("signer_bin")
            if not cell._usable(b):
                p["sign_reason"] = "no HELM_CELL_BIN (pre-signing launch)"
            elif not p.get("signer_profile"):
                p["sign_reason"] = "no DREGG_PROFILE/HELM_CELL_PROFILE"
            elif p["seat"] and p["signer_profile"] != p["seat"]:
                # A BORROWED PROFILE IS NOT A SIGNING ENV — for a FLEET ACTOR.
                # The signing gate refuses a seat whose profile names someone
                # else only when the roster proves the seat is an actor
                # (home_room), or cannot say; a named pane that never joined
                # still signs as the ambient profile (cell.signing_identity),
                # so flagging it would claim a refusal that does not happen.
                # One strict roster read per scan, the gate's reader. Both
                # names are laundered: they come from an unvalidated environ.
                #
                # THE SAME THREE READINGS THE GATE MAKES (task/3049): a live
                # rename alias names the renamed row; a profile that is that
                # row's own old name agrees; and the OWNER's inherited profile
                # is set aside for a seat whose session is bound to its row —
                # that pane SIGNS, as itself, and is reported as INFO, never
                # as a relaunch. The gate admits through the actor layer; this
                # /proc scan must not call it, so it asks the binding it can
                # see: the pane's session (environ, else the pid-keyed session
                # record) against the row's current or remembered sessions.
                from . import seats, seats_common
                if roster is None:
                    try:
                        roster = seats.roster_checked()
                    except Exception:            # noqa: BLE001 — the gate
                        roster = ({}, True)      # reads a raise as unreadable
                rows, failed = roster
                prof = p["signer_profile"]
                key = None
                if not failed:
                    key = (p["seat"] if isinstance(rows.get(p["seat"]), dict)
                           else seats_common.live_alias(p["seat"], rows)[0])
                row = rows.get(key) if key else None
                if failed:
                    why = ("the roster cannot be read, so the signing gate "
                           "refuses it (identity_unreadable)")
                elif not (isinstance(row, dict) and row.get("home_room")):
                    continue
                elif str(prof).casefold() == str(key).casefold() or str(
                        seats_common.live_alias(prof, rows)[0] or ""
                ).casefold() == str(key).casefold():
                    continue            # its own row's name or old name
                elif cell.is_owner_cell(prof) and not cell.is_owner_cell(key):
                    if pane_sids is None:
                        pane_sids = _pane_sessions()
                    sid = p.get("session") or pane_sids.get(p["pid"])
                    held = [row.get("session")] + list(
                        row.get("sessions") or [])
                    if sid and sid in held:
                        p["sign_info"] = True
                        p["sign_reason"] = (
                            "launched outside `helm launch`: profile '%s' is "
                            "the owner's inherited export, set aside — it "
                            "signs as its own seat '%s'" % (
                                seats._seat_label(prof),
                                seats._seat_label(key)))
                        out.append(p)
                        continue
                    why = ("the signing gate refuses it (identity_conflict): "
                           "the owner's profile is set aside only for a seat "
                           "whose session is bound to its row, and %s" % (
                               "this pane's session is not"
                               if sid else "this pane's session is unknown"))
                else:
                    why = "the signing gate refuses it (identity_conflict)"
                p["sign_reason"] = "profile '%s' is not this pane's seat '%s' — %s" % (
                    seats._seat_label(prof),
                    seats._seat_label(key or p["seat"]), why)
            else:
                continue
            out.append(p)
        return out
    except Exception:
        return []


def surface_uncovered(out=None):
    """Print the uncovered running panes — called by `helm hooks install`
    and `helm seat launch`: nothing external can wake an idle PTY agent, so
    a relaunch (human/driver) is the only repair and SURFACING is the lever.
    Same law for signing: a pane launched before the signing trio landed in
    launch_line posts [unsigned] forever (a process cannot retrofit its own
    environment) — name those too (no-silent-break)."""
    from . import seats
    out = out or sys.stdout

    def label(p):
        name = p["seat"] or p["family"] or ""
        return (seats._seat_label(name)
                if seats._SEAT_TOKEN.fullmatch(name) else "<invalid>")

    # ONE SCAN FOR THE WHOLE REPORT. This called uncovered_panes() and
    # unsigned_panes() separately and each walked /proc ITSELF, so a report
    # mixed TWO SNAPSHOTS: a probe built the same-pid case where a pane reads
    # coverage-UNKNOWN from scan 1 and then "posting UNSIGNED — relaunch" with
    # a seat name from scan 2. One pid, two rows, OPPOSITE recommended
    # actions, both individually correct about the snapshot they saw.
    #
    # That is the split-clock defect in a different currency — two READS of
    # one population where the report claims one observation — and it is why
    # per-item correctness is never the standard: all four buckets were
    # individually right and mutation-pinned, and the composition still lied.
    one_scan = running_panes()
    scanned = uncovered_panes(panes=one_scan)
    # TWO POPULATIONS, TWO ACTIONS, AND ONE HEADER USED TO CLAIM BOTH. Making
    # an unreadable pane VISIBLE (the cure below this one) put it under a
    # DEFINITE "NOT receiving fleet chat — relaunch them", rendering as
    # `<invalid>` with an UNKNOWN reason. The header asserted a fact the row
    # underneath denied, and the recommended action is the DESTRUCTIVE one: a
    # relaunch discards the pane's context, so telling an operator to relaunch
    # a pane whose coverage we could not even read spends real work on a guess.
    # A second read found it, in the consumer I had flagged as unaudited.
    rows = [p for p in scanned if not p.get("coverage_unknown")]
    unknown = [p for p in scanned if p.get("coverage_unknown")]
    if rows:
        print("helm hooks: %d running pane(s) NOT receiving fleet chat — relaunch "
              "them (an idle pane cannot self-heal into delivery):" % len(rows),
              file=out)
        for p in rows:
            # Valid names are laundered for terminal safety; invalid identities
            # render only the neutral sentinel from label().
            print("  pid %-7d %-14s %s" % (
                p["pid"], label(p),
                p["reason"]), file=out)
    if unknown:
        print("helm hooks: %d running pane(s) whose COVERAGE COULD NOT BE READ "
              "— look, do NOT relaunch (a relaunch discards context, and this "
              "is an unread fact, not a proven gap):" % len(unknown), file=out)
        for p in unknown:
            print("  pid %-7d %-14s %s" % (p["pid"], "UNKNOWN", p["reason"]),
                  file=out)
    scanned_sign = unsigned_panes(panes=one_scan)
    # SAME SPLIT AS THE COVERAGE BUCKET ABOVE, for the same reason. "Posting
    # UNSIGNED — relaunch from their minted launch.sh" is a PROVEN claim with
    # a destructive remedy; a pane whose environ we could not read has not
    # been proven anything, and a relaunch discards its context. Non-destructive
    # header, no relaunch instruction.
    sign = [p for p in scanned_sign
            if not (p.get("sign_unknown") or p.get("sign_info"))]
    sign_unknown = [p for p in scanned_sign if p.get("sign_unknown")]
    sign_info = [p for p in scanned_sign if p.get("sign_info")]
    if sign:
        print("helm hooks: %d running pane(s) posting UNSIGNED (no signing env, "
              "or a profile that is not their own) — relaunch from their "
              "minted launch.sh to sign:" % len(sign), file=out)
        for p in sign:
            print("  pid %-7d %-14s %s" % (
                p["pid"], label(p),
                p["sign_reason"]), file=out)
    if sign_unknown:
        print("helm hooks: %d running pane(s) whose SIGNING COULD NOT BE READ "
              "— look, do NOT relaunch (unread, not proven unsigned):"
              % len(sign_unknown), file=out)
        for p in sign_unknown:
            print("  pid %-7d %-14s %s" % (p["pid"], "UNKNOWN",
                                           p["sign_reason"]), file=out)
    if sign_info:
        print("helm hooks: %d running pane(s) launched outside `helm launch` "
              "that SIGN AS THEIR OWN SEAT (the owner's inherited profile is "
              "set aside) — no action needed:" % len(sign_info), file=out)
        for p in sign_info:
            print("  pid %-7d %-14s %s" % (p["pid"], label(p),
                                           p["sign_reason"]), file=out)


_CODEX_PENDING = ("codex: recipe pending — docs/HOOKS.md carries no mechanical "
                  "notify-hook shape yet; wire it by hand per that doc's codex section")

_USAGE = """usage: helm hooks install [--harness claude|codex] [--home NAME] [--project DIR] [--dry]
       helm hooks status
       helm hooks latency [--json] [--since T] [--until T]   (T carries a zone: a trailing Z or an offset)
       helm hooks sync [--apply]   (reconcile every home to the canonical set)
       helm hooks preflight --config-dir DIR   (may a session start here? resolve+refresh+verify; fail-closed)"""


def _select_homes(name):
    all_homes = claude_homes()
    if not name:
        return all_homes, None
    want = os.path.realpath(os.path.expanduser(name)) if os.path.sep in name else None
    hits = [(n, p) for n, p in all_homes if n == name or p == want]
    if hits:
        return hits, None
    return [], "unknown claude home %r (have: %s)" % (
        name, ", ".join(n for n, _ in all_homes) or "none")


def cmd_hooks(args):
    """hooks [install [--harness claude|codex] [--home NAME] [--project DIR] [--dry] | status
    | sync [--apply] | run <EVENT> [--tool NAME] [--hook-json]] — self-wire the
    complete hook contract into every claude home and seat config dir; sync
    (envtidy) reconciles every home to the named canonical hook set, dry-run by
    default; run dispatches every in-process handler for one event in a SINGLE
    interpreter (the one-spawn-per-event door, task/630)."""
    args = list(args)
    if not args:
        print(_USAGE, file=sys.stderr)
        return 2
    verb, rest = args[0], args[1:]

    if verb == "run":
        # THE ONE-SPAWN DOOR (task/630). `helm hooks run <EVENT>` runs every
        # in-process handler for that event in SPECS order inside a SINGLE
        # interpreter, replacing one hook entry per handler. The exit code is
        # the gate contract this file already states: only a gate's rc 2
        # escapes. See helm/hookrun.py for why each property holds.
        ev = None
        tool = None
        installed = hook_json = False
        i = 0
        while i < len(rest):
            a = rest[i]
            if a == "--hook-json":
                hook_json = True
            elif a == "--installed":
                installed = True
            elif a == "--tool":
                i += 1
                if i >= len(rest):
                    print("usage: helm hooks run <EVENT> [--tool NAME] [--hook-json]",
                          file=sys.stderr)
                    return 2
                tool = rest[i]
            elif a.startswith("-"):
                print("helm hooks run: unknown flag '%s'" % a, file=sys.stderr)
                return 2
            elif ev is None:
                ev = a
            else:
                print("helm hooks run: one EVENT only (got '%s' after '%s')"
                      % (a, ev), file=sys.stderr)
                return 2
            i += 1
        if not ev:
            print("usage: helm hooks run <EVENT> [--tool NAME] [--hook-json]",
                  file=sys.stderr)
            return 2
        if installed:
            if ev != "PostToolUse" or tool is not None or not hook_json:
                print("helm hooks run: --installed requires PostToolUse --hook-json "
                      "and forbids --tool", file=sys.stderr)
                return 2
            from . import posttoolrun
            return posttoolrun.run(payload=None)
        from . import hookrun
        return hookrun.run_event(ev, tool_name=tool)

    if verb == "latency":
        from . import hooklatency
        return hooklatency.cmd(rest)

    if verb == "preflight":
        # THE DOOR THE GENERATED launch.sh CALLS. Deliberately a real verb
        # rather than shell in the asset: one implementation of the rule, read
        # from the CURRENT SEAT_SPECS at run time.
        cdir = None
        if len(rest) == 2 and rest[0] == "--config-dir":
            cdir = rest[1]
        if not cdir:
            print("usage: helm hooks preflight --config-dir DIR",
                  file=sys.stderr)
            return 2
        rc, msg = preflight(cdir)
        if msg:
            print("helm hooks preflight: " + msg, file=sys.stderr)
        return rc

    if verb == "sync":   # reconcile every home to the canonical set (envtidy)
        from . import envtidy
        return envtidy.cmd_hooks_sync(rest)

    if verb == "status":
        from .cli import guard_tail
        rc = guard_tail("helm hooks status", rest, usage="hooks status")
        if rc is not None:
            return rc
        rows = status_rows()
        scope_rows = project_scope_rows()
        if not rows:
            print("helm hooks: no claude homes found")
            _print_project_scopes(scope_rows, "project scopes: ")
            print(_CODEX_PENDING)
            return 0
        print("helm hooks status (claude):")
        print("  %-28s %-5s %-5s %-9s %-7s %-5s %-5s %-8s %s" % (
            "home", "hook", "helm", "fail-open", "deliver", "join", "stop",
            "handoff", "resume"))
        def hoff(r):  # the continuity lane is live only when BOTH triggers are
            return (lane_live(r, "handoff-precompact")
                    and lane_live(r, "handoff-sessionend"))
        for r in rows:
            print("  %-28s %-5s %-5s %-9s %-7s %-5s %-5s %-8s %s" % (
                r["home"], "yes" if r["hook"] else "-",
                "ok" if r["hook"] and r["resolvable"] else ("NO" if r["hook"] else "-"),
                "ok" if r["hook"] and r["fail_open"] else ("NO" if r["hook"] else "-"),
                # THE COLUMNS READ PRESENCE, LIKE THE COUNTS BESIDE THEM.
                # Exact-rendering columns put `deliver yes` on the same screen
                # as `delivery lane 0 of 7` — one table disagreeing with its own
                # footer about the same seven files.
                "yes" if lane_live(r, "deliver") else "-",
                "yes" if lane_live(r, "join") else "-",
                "yes" if lane_live(r, "stop-guard") else "-",
                "yes" if hoff(r) else "-",
                "yes" if lane_live(r, "resume-turn") else "-"))
            if r.get("refusal_detail"):
                print("  " + r["refusal_detail"])
        # THE ONE FAILURE THE WRAPPER CANNOT ANNOUNCE ITSELF, so the census
        # does. A missing bin/helm-hook makes the shell exit 127 before any
        # line of the ladder runs: non-zero and not 2, which the harness reads
        # as ALLOW, in the shell's words rather than helm's. That is the same
        # silence the deleted-lane-room outage had, and the answer is the same
        # — measure the file rather than wait for it to report on itself. A
        # LINE RATHER THAN A COLUMN: it is exceptional, and a column of `ok`
        # across every home is the kind of width that stops being read.
        gone = wrapper_gone_message("home", rows)
        if gone:
            print(gone)
        # BOTH COUNTS COME OFF THE `rows` PRINTED ABOVE. `coverage()` re-scanned
        # the estate here, so the table and the numbers beside it described two
        # different moments; a probe racing a live estate saw the count claim a
        # population the table never listed.
        n, m = inject_covered(rows), len(rows)
        # EXPLICITLY NARROW. This counts ONE lane, and reading it as the home's
        # health is how a credential home missing the suite guard passed for
        # healthy here: "1 of 1" was true and answered a smaller question than
        # the reader was asking. The guard contract count sits beside it.
        line = "inject coverage (this lane only): %d of %d claude homes" % (n, m)
        print(line if n == m else line + " — `helm hooks install` closes the gap")
        hc = covered_count(rows)
        gline = ("home guard contract: %d of %d homes carry every required "
                 "guard" % (hc, m))
        print(gline if hc == m else gline + " — `helm hooks install` wires them")
        # THE GUARD GAP, ON THE HOME SIDE, FROM THE ROWS DOCTOR READS. `inject
        # coverage: N of N` is a NARROW claim about one lane, and printing it
        # alone let a credential home missing the suite guard read as healthy
        # here while doctor called it out — status building its own home
        # picture independently is finding 4's divergence wearing a different
        # hat. Same rows, same words, both surfaces.
        for r in rows:
            missing = (r["missing"] if r.get("drifted") is None
                       else missing_names(r))
            if missing:
                print("home %s is MISSING %d guard(s): %s — `helm hooks "
                      "install` wires them"
                      % (r["home"], len(missing), ", ".join(missing)))
            if r["stale"]:
                print("home %s carries a STALE guard entry: %s — the config "
                      "names it and this host cannot run it"
                      % (r["home"], ", ".join(r["stale"])))
            if r.get("drifted") is None:
                print("home %s: currency UNKNOWN — its settings could not be "
                      "read, so whether its guards are current is not known"
                      % r["home"])
        # ONE ROW FOR ONE FACT. A template change drifts EVERY home at once, so
        # per-home lines printed the identical sentence seven times and buried
        # the single action they all ask for under themselves. The homes are
        # named, because a partial drift is a different estate from a total one.
        drift = drifted_rows(rows)
        if drift:
            names = sorted({n for r in drift for n in r["drifted"]})
            print("%d of %d homes run an OUT-OF-DATE rendering of %d spec(s) "
                  "(%s): the entries WORK and these homes are covered above — "
                  "`helm hooks install` re-renders them (a seat that starts a "
                  "new session re-renders its own home on the way in)"
                  % (len(drift), m, len(names), ", ".join(names)))
            print("  drifted homes: %s"
                  % ", ".join(r["home"] for r in drift))
        # UNPROVEN LAUNCHES GET A ROW HERE AND NOWHERE ELSE (Q2 of
        # the guard-set meld). `unproven_launches` existed with only its test
        # as a reader, which is the built-not-wired shape this repo files rows
        # about — but the obvious home was WRONG. A doctor RUNG cannot hold it:
        # that rung's contract forbids OK rows outright, so a permanent WARN
        # about a guard that demonstrably runs is a false alarm invented to
        # cure a silent claim, and an alarm that fires on the healthy majority
        # is one nobody reads.
        #
        # STATUS IS A REPORT, NOT A RUNG, and that is the whole difference. It
        # already prints MISSING and STALE per home without either being a
        # failure; UNPROVEN is the third word in that vocabulary and the one
        # the old code said "ok" about. It is HOST-WIDE rather than per-home,
        # because the launchability of a path does not vary by which home
        # names it, so it prints once after the per-home rows.
        unproven = unproven_launches()
        for spec, _why, msg in unproven:
            print("guard %s RESOLVES but its launch is UNPROVEN: %s"
                  % (spec["name"], msg))
        if unproven:
            print("unproven is not broken — the file is there and executable, "
                  "and helm could not measure whether the kernel can START it "
                  "(an `#!/usr/bin/env` shebang resolves against a PATH helm "
                  "cannot see from here). Nothing to fix unless one of these "
                  "later exits 127")
        delivery_names = ("deliver", "delegation-stop", "join", "stop-guard",
                          "resume-turn", "argv-guard")
        d = sum(1 for r in rows if all(lane_live(r, k) for k in delivery_names))
        if d < m:
            print("delivery lane (chat deliver/join/stop-guard + seat "
                  "resume-turn): %d of %d homes"
                  " — `helm hooks install` wires it" % (d, m))
        c = sum(1 for r in rows if hoff(r))
        if c < m:
            print("continuity lane (handoff PreCompact/SessionEnd): %d of %d homes — "
                  "`helm hooks install` wires it" % (c, m))
        p = sum(1 for r in rows if r.get("permits"))
        if p < m:
            print("beacon permit (permissions.allow %s): %d of %d homes — "
                  "`helm hooks install` grants it (without it every fresh "
                  "session hangs on a human prompt arming its wake beacon)"
                  % (PERMIT_RULES[0], p, m))
        for r in rows:
            if not r.get("memory"):
                print(memory_gap_message("home", r))
        # ONE walk feeds the table, the counts AND the completeness note, so
        # the three can never disagree about a census raced by a live estate.
        # The gate is `srows or sunread`: a fully unreadable seats root has
        # ZERO rows and MUST still print — behind `if srows:` the loudest
        # failure (nothing readable at all) was the quietest surface.
        srows, sunread = seat_status_rows()
        if srows or sunread:
            print("seats (full hook contract — inject + delivery + handoff + "
                  "resume):")
            print("  %-28s %-7s %-8s %-5s %-5s %-8s %-7s %s" % (
                "seat", "inject", "deliver", "join", "stop", "handoff",
                "resume", "sa-ctx"))
            for r in srows:
                print("  %-28s %-7s %-8s %-5s %-5s %-8s %-7s %s" % (
                    r["seat"], "yes" if r["inject"] else "NO",
                    "yes" if r["deliver"] else "NO",
                    "yes" if r["join"] else "NO",
                    "yes" if r["stop-guard"] else "NO",
                    "yes" if hoff(r) else "NO",
                    "yes" if r["resume-turn"] else "NO",
                    "yes" if r.get("saguide") else "NO"))
            # ONE SOURCE FOR THE CLAIM AND THE AUDIT. This used to recompute
            # coverage inline over raw SEAT_SPECS while `seat_coverage` (and
            # doctor) measured the RESOLVED set, so the two could report
            # different numbers for the same estate — which is precisely the
            # shape that printed "10 of 10" over two unguarded seats: a claim
            # taken from a different list than the one that knows the answer.
            # THE SAME MEASUREMENT ON THE SEAT SIDE. The lane's own rollout
            # installs the wrapper command into every seat config dir, so a
            # seat whose ladder is gone is exactly as unguarded as a home —
            # and until this line the seat surface carried no `wrapper` key at
            # all, so nothing could report it.
            sgone = wrapper_gone_message("seat", srows)
            if sgone:
                print(sgone)
            sc, st = covered_count(srows), len(srows)
            sline = "seat hooks: %d of %d seats" % (sc, st)
            print(sline if sc == st else sline + " — `helm hooks install` wires it")
            # SA INITIAL CONTEXT GETS ITS OWN LINE BECAUSE IT IS ITS OWN
            # DIMENSION — and it IS part of the full-contract total above,
            # because SEAT_SPECS is SPECS and a seat missing this hook is
            # genuinely not fully covered.
            #
            # AN EARLIER VERSION OF THIS COMMENT SAID "never folded into the
            # seat-hooks total" WHILE covered_count COUNTED IT. A comment that
            # contradicts its own computation is worse than no comment: it
            # tells a reader the number means something it does not, and this
            # surface exists precisely so nobody has to guess what a coverage
            # figure covers.
            #
            # WHY THE DEDICATED LINE STILL EARNS ITS SPACE: `inject` covers a
            # TLA's per-turn physics and says NOTHING about a subagent — the
            # harness fires UserPromptSubmit for a real user turn and not for
            # the prompt handed to an SA. Folded into one total, a single
            # uncovered seat is indistinguishable from a fleet where every
            # subagent starts blank. The total answers "is this seat wired";
            # this line answers "do its subagents get physics".
            #
            # AND THE NUMBER IS BOUNDED OUT LOUD. Even at N of N this counts
            # seats whose CONFIG carries the hook — it is not a claim that
            # every subagent receives physics, because the harness drops hook
            # context for isolated-context agents before it reaches them. A
            # coverage line that quietly promised delivery it cannot make
            # would be the same vacuous green this seam exists to end.
            sa = sum(1 for r in srows if r.get("saguide"))
            # THE HEADLINE STATES WHAT IT MEASURES. "SA initial context:
            # N of M seats" asserted DELIVERY while counting INSTALLATION, and
            # a caveat one line below does not cure a headline — the headline
            # is what gets read and quoted. Delivery is not observable from a
            # settings file, so the number says INSTALLED and says so first.
            saline = ("SA initial-context hook INSTALLED for %d of %d seats "
                      "(delivery unobserved)" % (sa, st))
            if sa < st:
                saline += " — `helm hooks install` wires it"
            print(saline)
            print("  (SA coverage counts CONFIG, not delivery: %s)"
                  % _saguide_exclusion())
            sp = sum(1 for r in srows if r.get("permits"))
            if sp < st:
                print("seat beacon permit: %d of %d seats — `helm hooks "
                      "install` grants it" % (sp, st))
            for path, why in sunread:
                print("seat census INCOMPLETE — unreadable, any seat inside "
                      "is INVISIBLE to the counts above: %s (%s)" % (path, why))
        # The same sentence doctor and install print — third surface, one
        # source (external_gap_message), so no reader of any of the three is
        # told a different story about the same host.
        for _spec, _why, gmsg in unresolved_externals():
            print(gmsg)
        for _spec, note in unconfigured_optionals():
            print(note)
        _print_project_scopes(scope_rows, "project scopes: ")
        print(_CODEX_PENDING)
        return 0

    if verb == "install":
        # refuse junk BEFORE any home is touched — `hooks install --bogus`
        # used to run the full install and exit 0 as if --bogus existed.
        from .cli import guard_tail
        rc = guard_tail("helm hooks install", rest, flags=("--dry",),
                        valued=("--harness", "--home", "--project"),
                        usage="hooks install [--harness claude|codex] "
                              "[--home NAME] [--project DIR] [--dry]")
        if rc is not None:
            return rc
        harness = "claude"
        home_name = None
        dry = "--dry" in rest
        if "--harness" in rest:
            i = rest.index("--harness")
            harness = rest[i + 1] if i + 1 < len(rest) else ""
        if "--home" in rest:
            i = rest.index("--home")
            home_name = rest[i + 1] if i + 1 < len(rest) else None
        if harness not in ("claude", "codex"):
            print("helm hooks: unknown harness %r (claude | codex)" % harness,
                  file=sys.stderr)
            return 2
        if harness == "codex":
            print("helm hooks: " + _CODEX_PENDING)
            return 0
        if "--project" in rest:
            i = rest.index("--project")
            project = rest[i + 1] if i + 1 < len(rest) else None
            if not project:
                print("helm hooks: --project needs a directory", file=sys.stderr)
                return 2
            if home_name is not None:
                # contradictory scopes: a home install and a project install
                # write different files for different populations — refusing
                # beats guessing which one the operator meant.
                print("helm hooks: --project and --home are different scopes"
                      " — pick one", file=sys.stderr)
                return 2
            if not os.path.isdir(project):
                print("helm hooks: no such project directory: %s" % project,
                      file=sys.stderr)
                return 1
            for s in SPECS:
                print("helm hooks: %s (%s): %s"
                      % (s["name"], s["event"], spec_command(s)))
            pres = install_project(project, dry=dry)
            action, detail = pres
            sp = project_settings_path(project)
            # The project door propagates the SAME result the estate door
            # does. It used to return 0 with no warning while omitting a
            # required guard from every session launched in the project.
            for _spec, _why, gmsg in pres.unresolved:
                print("helm hooks: " + gmsg, file=sys.stderr)
            for _spec, note in unconfigured_optionals():
                print("helm hooks: " + note)
            if pres.shortened:
                print("helm hooks: " + pres.note("project %s" % sp),
                      file=sys.stderr)
            if action.startswith("dry-"):
                print("  %-28s %s (dry — nothing written)"
                      % (os.path.basename(os.path.abspath(project)), action[4:]))
                if detail:
                    print("    " + detail.replace("\n", "\n    "))
            else:
                print("  %-28s %-6s %s"
                      % (os.path.basename(os.path.abspath(project)), action, detail))
            if action != "fail":
                print("helm hooks: project scope %s — sessions launched in the"
                      " project get the physics; the rest of the machine stays"
                      " hook-free. Only NEW sessions pick it up (the harness"
                      " snapshots hooks at start)." % sp)
                print("helm hooks: keep %s out of the repo (per-machine paths)"
                      " — add `.claude/settings.local.json` to the project's"
                      " .gitignore if it is not already there; helm never edits"
                      " a repo's ignore file for you." % os.path.basename(sp))
            return 1 if action == "fail" or pres.shortened else 0
        targets, err = _select_homes(home_name)
        if err:
            print("helm hooks: " + err, file=sys.stderr)
            return 1
        # THE CENSUS IS TAKEN BEFORE ANY RETURN. A dry run's product is its
        # REPORT, and the exit code says whether that report is COMPLETE — so
        # the census that feeds it cannot sit behind an early exit. The old
        # shape returned 0 at "no claude homes" without ever calling
        # seat_homes (a probe: census_calls=0, stderr empty), which
        # made a seat-only estate invisible and an unreadable one
        # indistinguishable from an empty one — the walk's honesty (task/331)
        # unobservable to the one command whose job is wiring what it finds.
        # Seats get the full hook contract only on a full install; a
        # --home-narrowed run stays scoped to that one home and its report
        # claims nothing about seats.
        seats, sunread = seat_homes() if home_name is None else ([], [])
        if not targets:
            print("helm hooks: no claude homes found — `helm homes prepare claude <email>` starts one")
            if not seats and not sunread:
                _print_project_scopes(project_scope_rows(), "helm hooks: ")
                return 0
        for s in SPECS:
            print("helm hooks: %s (%s): %s" % (s["name"], s["event"], spec_command(s)))
        failed = 0
        # THE LOUD ROW REACHES THE INSTALL SURFACE, NOT ONLY DOCTOR. This verb
        # used to write every resolvable spec, print "N of N covered (full hook
        # contract)" and exit 0 on a host where a REQUIRED guard resolved to
        # nothing — a green run that had silently shortened its own contract,
        # which is the same disease as the count that read 10 of 10 over two
        # unguarded seats. An operator who runs the repair verb and reads
        # success must not have to also run doctor to learn it did not repair.
        gaps = unresolved_externals()
        for _spec, _why, msg in gaps:
            print("helm hooks: " + msg, file=sys.stderr)
        for _spec, note in unconfigured_optionals():
            print("helm hooks: " + note)
        # NOTE: rc is contributed per-door by `_apply` below, not here — the
        # host-level lines above are the WHY, the doors are the WHAT.

        def _apply(name, path, specs):
            nonlocal failed
            res = install_home(path, dry=dry, specs=specs)
            action, detail = res
            # RC COMES FROM THE DOOR'S OWN RESULT, not from a second reading of
            # the host taken beside it: every surface that WRITES a shortened
            # contract contributes, so this cannot report success for a write it
            # just made smaller than the contract.
            failed += (action == "fail") or res.shortened
            if action.startswith("dry-"):
                print("  %-28s %s (dry — nothing written)" % (name, action[4:]))
                if detail:
                    print("    " + detail.replace("\n", "\n    "))
            else:
                print("  %-28s %-6s %s" % (name, action, detail))
        for name, path in targets:
            _apply(name, path, SPECS)
        if seats:
            print("helm hooks: seats (full hook contract — inject + delivery + "
                  "handoff + resume):")
            for name, path in seats:
                _apply(name, path, SEAT_SPECS)
        if sunread:
            # UNKNOWN-and-report, never abort: every seat the walk DID see
            # was just wired above (the visible fleet gets its full hook
            # contract), but a subtree the install could not read is a
            # population it could not wire — that is this install FAILING
            # at its own job, so it counts into rc, not just into prose.
            failed += len(sunread)
            print("helm hooks: seat census INCOMPLETE — %d unreadable "
                  "subtree(s); any seat inside is UNWIRED and INVISIBLE:"
                  % len(sunread), file=sys.stderr)
            for path, why in sunread:
                print("  %s (%s)" % (path, why), file=sys.stderr)
        if not dry:
            n, m = coverage()
            print("helm hooks: %d of %d claude homes covered" % (n, m))
            sc, st, sunread2 = seat_coverage()
            if st or sunread2:
                # "full hook contract" is a CLAIM, and an unresolvable required
                # guard falsifies it however many configs were written: the
                # count was taken over a shortened list. Say which contract.
                print("helm hooks: %d of %d seats covered (%s)%s"
                      % (sc, st,
                         "full hook contract" if not gaps else
                         "SHORTENED contract — %d required guard(s) unresolved: %s"
                         % (len(gaps), ", ".join(s["name"] for s, _w, _m in gaps)),
                         " — census INCOMPLETE, the count is a floor"
                         if sunread2 else ""))
            if sunread2:
                # The SECOND census obeys the first census's law. A subtree
                # that became unreadable AFTER wiring means the coverage line
                # above is a floor over a population this install cannot
                # vouch for — its own job failing — so it reaches rc and
                # names the exact paths on stderr, not just a stdout caveat
                # a script will never see (`count is a floor` beside
                # rc 0 and an empty stderr is this bug's class at exit).
                failed += len(sunread2)
                print("helm hooks: seat census INCOMPLETE after wiring — %d "
                      "unreadable subtree(s); the coverage count is a floor:"
                      % len(sunread2), file=sys.stderr)
                for path, why in sunread2:
                    print("  %s (%s)" % (path, why), file=sys.stderr)
            surface_uncovered()   # settings fixed ≠ live panes fixed — a pane
        _print_project_scopes(project_scope_rows(), "helm hooks: ")
        return 1 if failed else 0  # launched pre-install still needs a relaunch

    print("helm hooks: unknown subverb %r" % verb, file=sys.stderr)
    print(_USAGE, file=sys.stderr)
    return 2
