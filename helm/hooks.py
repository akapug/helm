#!/usr/bin/env python3
"""helm hooks — the self-closing installer for the crown-jewel wiring: every
claude credential home gets the UserPromptSubmit hook that pipes each turn's
FULL hook JSON to `helm inject --hook-json` (which derives the project scope
from the turn's cwd). Hand-wiring one home at a time was the adoption gap;
`install` closes it, `status` + doctor keep it closed.

The estate is TWO surfaces: the claude credential homes (~/.claude-homes/*,
~/.claude) get the full spec set (inject + the delivery lane); the multimodel
SEAT config dirs (<helm_home>/_global/seats/<family>/claude and slice-6
instances <family>/instances/<seat>/claude — seat.py's isolated
CLAUDE_CONFIG_DIRs) get the DELIVERY LANE (deliver + subagent-end +
join + stop-guard) so a launched codex/kimi/… seat receives fleet chat under its
family name (seat.py exports HELM_CHAT_NAME=<family> on launch; seats.py
derive_seat keys the roster on it) and cannot idle past a NEW row of it —
BOUNDED, and the bound is an enumeration rather than a sentence anyone can
restate loosely: the INBOX rung blocks once per pending-fingerprint (a re-stop
on the SAME rows passes), and it never runs at all across a stop-active
continuation — the harness is already continuing off a stop hook, so a row
landing mid-turn waits for the next REAL stop. Two named kill switches disable
it outright. `seats.py stop_guard` is the authority and
tests/test_delivery_promise_escapes is the COMPLETE escape set; a fifth path is
a finding against that enumeration. The full loop:
inject (turn start) + deliver (tool boundary) + join (session start) +
stop-guard (idle gate). `install` covers both surfaces; `status` reports
coverage for both.

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
import shlex
import shutil
import stat
import sys
import time

from . import configs, home, homes

HOOK_EVENT = "UserPromptSubmit"
TIMEOUT_S = 10  # inject is ~ms; 10s is the never-hold-a-turn ceiling

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
     "timeout": 2, "own": ("chat deliver --hook-json",), "matcher": "*"},
    # A PostToolUse subagent record is interval proof only while that logical
    # agent remains active. Remove its exact agent_id entry at lifecycle end;
    # otherwise the long-lived parent Claude pid would preserve stale proof.
    {"name": "delegation-stop", "event": "SubagentStop",
     "args": "chat delegation-stop --hook-json", "timeout": 2,
     "own": ("chat delegation-stop --hook-json",), "matcher": "*"},
    {"name": "join", "event": "SessionStart", "args": "chat join --hook-json",
     "timeout": 5, "own": ("chat join --hook-json",), "matcher": "*"},
    # resume-turn: the RESUME LEG of a compaction (resumeturn.py). It shares
    # SessionStart with join and gates INTERNALLY on source == "compact",
    # rather than riding a `"matcher": "compact"` group: the wildcard group is
    # the shape proven live across this whole estate, and a source gate the
    # verb owns cannot be silently mis-installed into a group that never fires.
    {"name": "resume-turn", "event": "SessionStart",
     "args": "seat resume-turn --hook-json", "timeout": 5,
     "own": ("seat resume-turn --hook-json",), "matcher": "*"},
    # stop-guard: the IDLE GATE (the prior harness's arbiter capability). Blocks a stop
    # on undelivered mentions/held leases (once per pending-fingerprint),
    # warns to arm the beacon on a clean stop, silently runs the index cap.
    # Stop takes no matcher (like UserPromptSubmit).
    # gate=True — the ONE spec whose refusal must reach the harness. See
    # spec_command: `|| true` would swallow the exit-2 block, which is what
    # disarmed this gate for its entire life.
    {"name": "stop-guard", "event": "Stop", "args": "chat stop-guard --hook-json",
     "timeout": 5, "own": ("chat stop-guard --hook-json",), "matcher": None,
     "gate": True},
    # argv-guard: the shell-substitution gate. A chat/dispatch body composed
    # as a double-quoted shell argument lets the SHELL execute backticked
    # content before helm exists — measured three times in two days, once
    # running `git clean` in the shared checkout from inside the message
    # warning about it. Helm cannot see consumed backticks; this hook reads
    # the Bash command BEFORE any shell runs, the one place they are visible.
    # Also covers `git commit/tag -m` messages — the same hazard one surface
    # out (a landed commit read "the assignment test is , and" after the
    # shell ate its backticked phrase, 2026-07-29).
    {"name": "argv-guard", "event": "PreToolUse",
     "args": "chat argv-guard --hook-json", "timeout": 2,
     "own": ("chat argv-guard --hook-json",), "matcher": "Bash",
     "gate": True},
    # continuity: the compaction/session-end handoff contract (sessions lane).
    {"name": "handoff-precompact", "event": "PreCompact",
     "args": "handoff check --hook-json", "timeout": 5,
     "own": ("handoff check --hook-json",), "matcher": None},
    {"name": "handoff-sessionend", "event": "SessionEnd",
     "args": "handoff check --hook-json", "timeout": 5,
     "own": ("handoff check --hook-json",), "matcher": None},
)

# The delivery lane alone (deliver + delegation-stop + join + stop-guard +
# resume-turn, no inject) — what a SEAT's isolated CLAUDE_CONFIG_DIR receives
# so @<family> and
# owner posts reach it AND it cannot idle past a NEW one — latched per
# pending-fingerprint (the SAME rows pass on a re-stop) and skipped entirely
# across a stop-active continuation; the complete escape set is enumerated in
# tests/test_delivery_promise_escapes. inject (the per-turn
# context brief) stays a home concern; a seat joins the roster under its family
# name via HELM_CHAT_NAME (seat.py launch_line).
#
# resume-turn rides the lane and not only the home set: a SEAT is the surface
# that compacts unattended, and a seat that sleeps through its own compaction
# is invisible until someone notices the fleet went quiet.
DELIVERY_SPECS = tuple(s for s in SPECS
                       if s["name"] in ("deliver", "delegation-stop", "join",
                                        "stop-guard", "resume-turn",
                                        "argv-guard"))

# The beacon's permission grease (owner hit it live): the SessionStart join
# line DIRECTS `Monitor(command: "helm chat wait … --follow")` as the
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
_GATE_ALLOWED_NOUN = {"Stop": "stop", "PreToolUse": "tool call"}
_GATE_RC = "@@rc@@"


def _gate_alarm(event, text):
    """One fail-open diagnostic, on the channels the HARNESS actually reads.

    STDERR WAS A VOID. The Claude Code hook contract, read rather than
    assumed: *"Stderr from a hook that exits 0 goes to the debug log only,
    never the transcript, and Claude never sees it."* So every arm below
    announced an UNCHECKED stop to nobody — @codex measured it — and the
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
    only unquoted token is a variable this module wrote itself."""
    def splice(s):
        args = []
        for i, part in enumerate(s.split(_GATE_RC)):
            if i:
                args.append('"$rc"')
            args.append(shlex.quote(part))
        return shlex.quote("%s" * len(args) + "\\n"), " ".join(args)

    payload = json.dumps({
        "systemMessage": text,
        "hookSpecificOutput": {"hookEventName": event,
                               "additionalContext": text},
    })
    return "printf %s %s >&2; printf %s %s" % (splice(text) + splice(payload))


def spec_command(spec):
    """The generated hook text for one spec.

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
    hb = helm_bin()
    base = "timeout %d %s %s" % (spec["timeout"], shlex.quote(hb), spec["args"])
    if spec.get("gate"):
        # rc 124 IS `timeout` KILLING THE GUARD, and it is the one non-2 code
        # that must not pass in silence. A crash prints a traceback, so a
        # swallowed rc 1/127 still leaves evidence on stderr; a timeout kill
        # prints NOTHING, so the stop looked exactly like a clean allow. That
        # is escape 5's shape — the fail-open LAW is right (a guard that can
        # wedge every seat's turn end is worse than one that misses a row) and
        # the SILENCE was the bug — reaching the hook layer this time.
        # codex-2 found it. Still exits 0; now it says why.
        #
        # "A swallowed rc 127 still leaves evidence on stderr" WAS FALSE, and
        # it cost a fleet-wide outage on 2026-08-04. Measured: `timeout 5
        # /gone/bin/helm …` writes ONE line to stderr — "timeout: failed to
        # execute process" — under an exit code the harness reads as a clean
        # allow, so nothing surfaces it to the agent or the owner. Eight
        # settings files pointed at a deleted lane room, BOTH gates among
        # them, and four credential homes ran unguarded in total silence. So
        # the arm set is now EXHAUSTIVE rather than enumerated-and-optimistic:
        # 0 passes, 2 blocks, and EVERY other code says what happened. A guard
        # that could not run is a strictly worse state than one that timed out
        # — the timeout at least proves helm was reachable — and only the
        # timeout was reported.
        #
        # `case` rather than a chain of `[ … ] &&` tests so the unknown-code
        # arm is a real default and cannot be forgotten by the next code added.
        # THE EVENT WORD IS THE SPEC'S, not the template's. This template
        # renders every gate spec, and the hardcoded "this stop" told the
        # argv-guard's PreToolUse reader a STOP had been allowed when what
        # went unchecked was a tool call.
        allowed = _GATE_ALLOWED_NOUN.get(spec["event"],
                                         "%s event" % spec["event"])
        # Each leg is ONE SENTENCE handed to `_gate_alarm`, which renders it
        # to stderr AND to the exit-0 JSON the harness surfaces. Writing the
        # sentence once is the point: two hand-kept copies of the same
        # warning is how the "this stop" / "this tool call" drift happened.
        # THE PATH IS SHELL DATA, NEVER SHELL SOURCE — helm has watched a
        # shell eat backticked content out of a message body three times in
        # two days (the argv-guard exists for that), so `hb` rides inside the
        # single-quoted text rather than being concatenated into the command.
        timed_out = _gate_alarm(
            spec["event"],
            "[helm %s] THE GUARD TIMED OUT after %ds — this %s is ALLOWED "
            "and UNCHECKED" % (spec["name"], spec["timeout"], allowed))
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
                % (base, timed_out, missing, broke))
    return base + " || true"


def hook_command():
    """The inject spec's command — the name every older caller knows."""
    return spec_command(SPECS[0])


def _ours(cmd):
    """A hook command helm owns (installer-written or hand-wired helm inject).

    Spec-driven for the `own` tokens as well as inject, so a GATE command —
    which carries no `|| true` tail — is still recognized as helm's and does
    not read as a foreign hook to status/sync."""
    if "inject --hook-json" in cmd or "helm inject" in cmd:
        return True
    return any(tok in cmd for sp in SPECS for tok in sp.get("own", ()))


def _helm_of(cmd):
    """The helm executable token a hook command calls (the word before
    `inject`), None when unparseable."""
    try:
        toks = shlex.split(cmd)
    except ValueError:
        return None
    for i, t in enumerate(toks):
        if t == "inject" and i:
            return toks[i - 1]
    return None


def _resolvable(cmd):
    h = _helm_of(cmd)
    if not h:
        return False
    if os.path.sep in h:
        return os.path.isfile(h) and os.access(h, os.X_OK)
    return bool(shutil.which(h))


def _fail_open(cmd):
    """The never-block contract in the command text (docs/HOOKS.md law)."""
    return "|| true" in cmd


def claude_homes():
    """[(name, realpath)] per claude home: every real dir under the claude
    homes root plus the default ~/.claude — homes.py's ROOTS/DEFAULTS
    discovery; alias symlinks fold onto their target, archives never appear."""
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
## of OUTSIDE it (where task/331's codex-2/-3 sat: live, healthy-looking, and
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
    <family>/instances/<seat>/claude (slice-6 instances). These get the
    delivery lane so a launched seat receives fleet chat under its own name
    (the config dir's holder: `codex`, `codex-2`). Discovered by a bounded
    walk, NOT a fixed-shape glob — the one-level `*/claude` glob was task/331:
    a census that could not SEE nested instances reported the estate healthy
    around them. configs' gated write (_classify._is_seat_home) recognizes
    the same dirs, so the merge-preserving install accepts them. The walk
    never requires settings.json: install_home is what CREATES a fresh seat's
    settings.json, so a census keyed on its presence would be blind to
    exactly the dirs the installer must initialize.

    unread: [(path, reason)] per directory the walk tried to list or stat and
    COULD NOT, for any cause but absence (_CENSUS_ABSENT). The widened walk's
    first cut caught every OSError and silently returned — @codex-3's probe:
    chmod 000 on an instances/ subtree and the census reported the one
    visible family with total confidence, seat_coverage printing healthy
    around a live invisible codex-2. That is task/331's own disease
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


def _all_hook_cmds(settings):
    """Every command across every hook event, preserving no event assumptions."""
    hooks = settings.get("hooks") if isinstance(settings, dict) else None
    if not isinstance(hooks, dict):
        return []
    return [c for event in hooks for c in _hook_cmds(settings, event)]


def _owned_specs(settings):
    """Helm spec names registered in one settings object."""
    return sorted({spec["name"] for cmd in _all_hook_cmds(settings)
                   for spec in SPECS
                   if any(tok in cmd for tok in spec.get("own", ()))})


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
    return [t for t in lane_room_tokens(cmd)
            if t != canonical and os.path.basename(t) == "helm"]


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
                rooms = lane_room_tokens(cmd)
                if not rooms:
                    continue
                if not owns(cmd):
                    # NOT OURS. Someone else's tool may live wherever it likes;
                    # we say we saw it and we do not touch a byte.
                    notes += [("foreign", event, t) for t in rooms]
                    continue
                for tok in rooms:
                    if tok == canonical:
                        # ALREADY the derived answer — see stale_room_tokens,
                        # which is now the shared spelling of this exemption.
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
    survivors = sorted({t for cmd in _all_hook_cmds(settings) if owns(cmd)
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
    # (@codex-2 found it — the third consumer of this return value tonight.)
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
            with open(real, encoding="utf-8") as f:
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
        path = os.path.join(root, ".claude", "settings.json")
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


def _canonical_entry(spec):
    entry = {"hooks": [{"type": "command", "command": spec_command(spec)}]}
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
    cmd = spec_command(spec)
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
             if isinstance(h, dict) and any(m in str(h.get("command") or "")
                                            for m in own)]
    if not found:
        groups.append(_canonical_entry(spec))
        return "add"
    action = "ok"
    homed = False          # at least one of ours sits in a matcher-correct group
    orphans = []           # ours, in a wrong-matcher group shared with foreigners
    for g, hlist, h in found:
        if h.get("command") != cmd or h.get("type") != "command":
            h["command"] = cmd
            h["type"] = "command"
            action = "update"
        if _matcher_ok(g, spec):
            homed = True
        elif len(hlist) == 1:            # the group is ours alone — repair it
            if spec["matcher"] is None:
                g.pop("matcher", None)
            else:
                g["matcher"] = spec["matcher"]
            action = "update"
            homed = True
        else:
            orphans.append((hlist, h))
    for hlist, h in orphans:             # foreign co-tenants stay untouched
        hlist.remove(h)
        action = "update"
    if orphans and not homed:
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


def _rooms_clean(settings, owns=None):
    """No helm-owned command names a STALE lane room's helm binary (the
    post-write check, the twin of _lane_live/_permits_live/_defaults_live).

    Through stale_room_tokens, which exempts the derived answer — helm may
    legitimately live under a `wt/` path. Asking `lane_room_tokens` directly
    here is what made verify reject every install on the build node."""
    owns = owns or _ours
    return not any(stale_room_tokens(cmd)
                   for cmd in _all_hook_cmds(settings) if owns(cmd))


def _merge_all(settings, specs=SPECS, path="<settings>"):
    """-> (merged_copy, {spec_name: action}) across `specs` — the whole estate
    for a home (SPECS), the delivery lane for a seat (DELIVERY_SPECS) — plus
    the beacon permit rules (every surface that gets the delivery lane must
    also be ABLE to arm the beacon without a human prompt), the scalar estate
    defaults (workflows default-small on every home and seat), and the
    lane-room repair.

    THE ROOM REPAIR RUNS HERE, NOT IN install_home's transform, and that is
    load-bearing: `verify` re-derives the expected candidate by calling this
    function again, so a mutation applied outside it would make every write
    fail verification. One merge function, one answer, both callers.

    It runs LAST so it only ever sees what the spec merges left behind — an
    owned entry for a spec this install does not carry (a seat config gets
    DELIVERY_SPECS, so its inject/handoff entries are nobody's to rewrite
    here), which is precisely the residue _merge_event cannot reach."""
    out = json.loads(json.dumps(settings))  # deep copy — never mutate the input
    actions = {s["name"]: _merge_event(out, s) for s in specs}
    actions["permits"] = _merge_permits(out)
    actions["defaults"] = _merge_defaults(out)
    notes = repair_lane_room_commands(out, path)
    actions["rooms"] = "update" if any(n[0] == "repaired" for n in notes) else "ok"
    # A tuple value, never an action word — _agg compares against "fail"/
    # "update"/"add" and must not read this as one.
    actions["rooms_notes"] = tuple(notes)
    return out, actions


def _agg(actions):
    """One home's aggregate action, worst-first (fail > update > add > ok)."""
    for a in ("fail", "update", "add"):
        if a in actions.values():
            return a
    return "ok"


def install_home(path, dry=False, specs=SPECS):
    """Install/refresh one hook estate through bounded content-revision CAS."""
    sp = os.path.join(path, "settings.json")

    def transform(cur):
        merged, actions = _merge_all(cur, specs, sp)
        return merged, {"actions": actions}

    def verify(candidate, before, _metadata):
        expected, _actions = _merge_all(before, specs, sp)
        return (candidate == expected
                and all(_lane_live(candidate, s) for s in specs)
                and _permits_live(candidate) and _defaults_live(candidate)
                and _rooms_clean(candidate))

    res = configs.transform_json_file(sp, transform, verify=verify, dry_run=dry)
    if not res.get("ok"):
        return "fail", res["error"]
    actions = (res.get("initial_metadata") or res.get("metadata") or {}).get("actions") or {}
    rooms = lane_room_report(actions.get("rooms_notes") or ())
    action = _agg(actions)
    # `ok` IS RESERVED FOR "I WROTE NOTHING". A room repair rewrites bytes, so it
    # aggregates to `update` even when every spec was already current — do not
    # "optimize away" that as a spurious update. On 2026-08-04 this verb printed
    # `update … 0 failed` for four homes it had only PARTLY fixed, and the
    # residue was found by re-running the DETECTION scan rather than by reading
    # this line. A report that says `ok` while the file changed underneath it is
    # that same disease in its purest form: a claim about intent, not about disk.
    if action == "ok":
        return "ok", "hook up to date" + ("; " + rooms if rooms else "")
    if dry:
        diff = difflib.unified_diff(
            res["before"].splitlines(), res["after"].splitlines(),
            sp, sp + " (after install)", lineterm="")
        return "dry-" + action, "\n".join(diff) + (("\n" + rooms) if rooms else "")
    return action, "backup: %s; CAS attempts: %d%s" % (
        res.get("backup") or "none — new file", res["attempts"],
        "; " + rooms if rooms else "")


def _lane_live(settings, spec):
    """A delivery lane counts as live ONLY on the exact spec command, with
    type "command", inside a group whose matcher matches the spec (codex
    B3 + final delta): a marker substring under a `Bash`-pinned group — or
    the right command string under a foreign type — is a stale install the
    harness won't run as we expect, not coverage."""
    hooks = settings.get("hooks") if isinstance(settings, dict) else None
    groups = hooks.get(spec["event"]) if isinstance(hooks, dict) else None
    cmd = spec_command(spec)
    for g in groups if isinstance(groups, list) else []:
        if not (isinstance(g, dict) and _matcher_ok(g, spec)):
            continue
        for h in g.get("hooks") or []:
            if isinstance(h, dict) and h.get("command") == cmd \
                    and h.get("type") == "command":
                return True
    return False


def status_rows():
    """Per-claude-home coverage: inject hook present? helm resolvable?
    fail-open contract present? Plus the delivery lane's two booleans —
    exact command + matcher validated, never marker presence. Read-only."""
    rows = []
    for name, path in claude_homes():
        cmd, settings = None, {}
        try:
            with open(os.path.join(path, "settings.json"), encoding="utf-8") as f:
                settings = json.load(f)
            cmd = next((c for c in _hook_cmds(settings) if _ours(c)), None)
        except (OSError, ValueError):
            pass
        lanes = {s["name"]: _lane_live(settings, s) for s in SPECS[1:]}
        rows.append({"home": name, "path": path, "hook": bool(cmd), "command": cmd,
                     "resolvable": bool(cmd) and _resolvable(cmd),
                     "fail_open": bool(cmd) and _fail_open(cmd),
                     "permits": _permits_live(settings), **lanes})
    return rows


def coverage():
    """(covered, total) claude homes — covered = hook present, helm resolvable,
    fail-open contract intact."""
    rows = status_rows()
    return (sum(1 for r in rows
                if r["hook"] and r["resolvable"] and r["fail_open"]), len(rows))


def seat_status_rows():
    """(rows, unread) — per-seat delivery-lane coverage: does the seat's
    claude/settings.json carry the deliver + subagent-end + join + stop-guard
    hooks (exact command + matcher + type validated, never marker presence —
    same _lane_live law as homes)? Read-only. inject is not a seat concern,
    so it is not reported here. `unread` is seat_homes' completeness, carried
    through untouched: rows describe the seats the census SAW, and every
    consumer printing them owes its reader the subtrees it could not. (An
    unreadable settings.json inside a VISIBLE seat needs no such channel —
    that row ships with its lanes False, an uncovered seat, which is loud.)"""
    rows = []
    homes_found, unread = seat_homes()
    for name, path in homes_found:
        settings = {}
        try:
            with open(os.path.join(path, "settings.json"), encoding="utf-8") as f:
                settings = json.load(f)
        except (OSError, ValueError):
            pass
        lanes = {s["name"]: _lane_live(settings, s) for s in DELIVERY_SPECS}
        rows.append({"seat": name, "path": path,
                     "permits": _permits_live(settings), **lanes})
    return rows, unread


def seat_coverage():
    """(covered, total, unread) seats — covered = the whole delivery lane
    (deliver + join + stop-guard) live in the seat's claude config dir. When
    `unread` is non-empty BOTH counts are a FLOOR over an incomplete census —
    "N of N" beside a non-empty unread is never "healthy", it is "healthy
    among the seats the walk could read". Consumers must print the third
    element or they are task/331's instrument again."""
    rows, unread = seat_status_rows()
    names = [s["name"] for s in DELIVERY_SPECS]
    return (sum(1 for r in rows if all(r[n] for n in names)), len(rows),
            unread)


# ── the retrofit surface (G-seatlaunch-installs): a pane that launched
# BEFORE its identity/hooks existed sits idle forever — no hook ever fires
# in an idle PTY, so it can never self-heal into delivery (the live kimi
# seat: running, absent from the roster, @kimi routing nowhere). The only
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
    tainting local coverage and project-census certainty. @codex-2 found it,
    and the fix that caused it is earlier in this same lane.

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
    — @codex-2's pid-7443 case, where ours() observes same-uid process A, a
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
    from . import beacons
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
        process between any two of them. @codex-2 built it: pid 7441 answers
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

        (2) OWNERSHIP REPROOF, for UNCERTAINTY rows only — @codex-2's seam.
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
                   "signer_bin": None, "signer_profile": None}
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
        # anything. @codex-2's pid-7443 repro: ours(base) observes same-uid
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
            # @codex-2 found it and the shape is worth naming: the cure I
            # wrote for the environ path did not cover the arm the cure
            # ITSELF introduced. A new branch is a new place for the class to
            # live, and it does not inherit the fix from its sibling.
            emit(pid, base, token,
                 {"pid": int(pid), "seat": None, "config_dir": None,
                  "family": None, "environ_unreadable": True,
                  "detect_unknown": True,
                  "signer_bin": None, "signer_profile": None})
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
                    "signer_profile": val("DREGG_PROFILE") or
                    val("HELM_CELL_PROFILE")})
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


def unsigned_panes(proc=None, panes=None):
    """Running NAMED panes that cannot sign their posts: HELM_CELL_BIN unset
    or pointing at a non-executable (cell.bin_ready's law), or the profile
    pair missing (DREGG_PROFILE/HELM_CELL_PROFILE) — launch_line has carried
    the trio since seat-signing landed, so such a pane predates it and posts
    [unsigned] by configuration. Read-only; fail-open [].

    A BLIND PANE IS SIGNING-UNKNOWN, NEVER SILENTLY HEALTHY. Its environ is
    unreadable, so the trio cannot be read either — and the first cut left it
    out of this bucket on the reasoning that it "belongs in neither". Every
    clause of that was true and the conclusion was wrong: absent from the
    unsigned bucket RENDERS AS CAN-SIGN, which is a claim the read never
    supported. Measured on a planted pane carrying a full trio behind an
    unreadable environ: reported by nothing at all. So it now comes back
    flagged, and `surface_uncovered` prints it under its own non-destructive
    header. (Converged with @codex-2 in a
    convergence meld; I constructed the case only after writing the wrong
    reasoning down.)
    """
    try:
        from . import cell
        out = []
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
    # mixed TWO SNAPSHOTS: @codex-2 built the same-pid case where a pane reads
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
    # @codex-2 found it on review, in the consumer I had flagged as unaudited.
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
    sign = [p for p in scanned_sign if not p.get("sign_unknown")]
    sign_unknown = [p for p in scanned_sign if p.get("sign_unknown")]
    if sign:
        print("helm hooks: %d running pane(s) posting UNSIGNED (no signing env) — "
              "relaunch from their minted launch.sh to sign:" % len(sign), file=out)
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


_CODEX_PENDING = ("codex: recipe pending — docs/HOOKS.md carries no mechanical "
                  "notify-hook shape yet; wire it by hand per that doc's codex section")

_USAGE = """usage: helm hooks install [--harness claude|codex] [--home NAME] [--dry]
       helm hooks status
       helm hooks sync [--apply]   (reconcile every home to the canonical set)"""


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
    """hooks [install [--harness claude|codex] [--home NAME] [--dry] | status
    | sync [--apply]] — self-wire the per-turn inject hook into every claude
    home, and the fleet-delivery lane (DELIVERY_SPECS) into every seat config
    dir; sync (envtidy) reconciles every home to the named canonical hook set,
    dry-run by default."""
    args = list(args)
    if not args:
        print(_USAGE, file=sys.stderr)
        return 2
    verb, rest = args[0], args[1:]

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
            return r.get("handoff-precompact") and r.get("handoff-sessionend")
        for r in rows:
            print("  %-28s %-5s %-5s %-9s %-7s %-5s %-5s %-8s %s" % (
                r["home"], "yes" if r["hook"] else "-",
                "ok" if r["hook"] and r["resolvable"] else ("NO" if r["hook"] else "-"),
                "ok" if r["hook"] and r["fail_open"] else ("NO" if r["hook"] else "-"),
                "yes" if r.get("deliver") else "-",
                "yes" if r.get("join") else "-",
                "yes" if r.get("stop-guard") else "-",
                "yes" if hoff(r) else "-",
                "yes" if r.get("resume-turn") else "-"))
        n, m = coverage()
        line = "inject coverage: %d of %d claude homes" % (n, m)
        print(line if n == m else line + " — `helm hooks install` closes the gap")
        lanes = [s["name"] for s in DELIVERY_SPECS]
        d = sum(1 for r in rows if all(r.get(k) for k in lanes))
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
        # ONE walk feeds the table, the counts AND the completeness note, so
        # the three can never disagree about a census raced by a live estate.
        # The gate is `srows or sunread`: a fully unreadable seats root has
        # ZERO rows and MUST still print — behind `if srows:` the loudest
        # failure (nothing readable at all) was the quietest surface.
        srows, sunread = seat_status_rows()
        if srows or sunread:
            print("seats (fleet delivery — chat deliver/join/stop-guard + seat "
                  "resume-turn):")
            print("  %-28s %-8s %-5s %-5s %s" % ("seat", "deliver", "join", "stop",
                                              "resume"))
            for r in srows:
                print("  %-28s %-8s %-5s %-5s %s" % (
                    r["seat"], "yes" if r["deliver"] else "NO",
                    "yes" if r["join"] else "NO",
                    "yes" if r["stop-guard"] else "NO",
                    "yes" if r["resume-turn"] else "NO"))
            sc = sum(1 for r in srows if all(r.get(k) for k in lanes))
            st = len(srows)
            sline = "seat delivery: %d of %d seats" % (sc, st)
            print(sline if sc == st else sline + " — `helm hooks install` wires it")
            sp = sum(1 for r in srows if r.get("permits"))
            if sp < st:
                print("seat beacon permit: %d of %d seats — `helm hooks "
                      "install` grants it" % (sp, st))
            for path, why in sunread:
                print("seat census INCOMPLETE — unreadable, any seat inside "
                      "is INVISIBLE to the counts above: %s (%s)" % (path, why))
        _print_project_scopes(scope_rows, "project scopes: ")
        print(_CODEX_PENDING)
        return 0

    if verb == "install":
        # refuse junk BEFORE any home is touched — `hooks install --bogus`
        # used to run the full install and exit 0 as if --bogus existed.
        from .cli import guard_tail
        rc = guard_tail("helm hooks install", rest, flags=("--dry",),
                        valued=("--harness", "--home"),
                        usage="hooks install [--harness claude|codex] "
                              "[--home NAME] [--dry]")
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
        targets, err = _select_homes(home_name)
        if err:
            print("helm hooks: " + err, file=sys.stderr)
            return 1
        # THE CENSUS IS TAKEN BEFORE ANY RETURN. A dry run's product is its
        # REPORT, and the exit code says whether that report is COMPLETE — so
        # the census that feeds it cannot sit behind an early exit. The old
        # shape returned 0 at "no claude homes" without ever calling
        # seat_homes (@codex-3's probe: census_calls=0, stderr empty), which
        # made a seat-only estate invisible and an unreadable one
        # indistinguishable from an empty one — the walk's honesty (task/331)
        # unobservable to the one command whose job is wiring what it finds.
        # Seats get the delivery lane only on a full install; a
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
        def _apply(name, path, specs):
            nonlocal failed
            action, detail = install_home(path, dry=dry, specs=specs)
            failed += action == "fail"
            if action.startswith("dry-"):
                print("  %-28s %s (dry — nothing written)" % (name, action[4:]))
                if detail:
                    print("    " + detail.replace("\n", "\n    "))
            else:
                print("  %-28s %-6s %s" % (name, action, detail))
        for name, path in targets:
            _apply(name, path, SPECS)
        if seats:
            print("helm hooks: seats (fleet delivery — deliver + join + "
                  "stop-guard + resume-turn):")
            for name, path in seats:
                _apply(name, path, DELIVERY_SPECS)
        if sunread:
            # UNKNOWN-and-report, never abort: every seat the walk DID see
            # was just wired above (the visible fleet keeps its delivery
            # lane), but a subtree the install could not read is a
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
                print("helm hooks: %d of %d seats covered (fleet delivery)%s"
                      % (sc, st, " — census INCOMPLETE, the count is a floor"
                         if sunread2 else ""))
            if sunread2:
                # The SECOND census obeys the first census's law. A subtree
                # that became unreadable AFTER wiring means the coverage line
                # above is a floor over a population this install cannot
                # vouch for — its own job failing — so it reaches rc and
                # names the exact paths on stderr, not just a stdout caveat
                # a script will never see (@codex-3: `count is a floor` beside
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
