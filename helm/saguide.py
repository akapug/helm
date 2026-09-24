#!/usr/bin/env python3
"""saguide — the ONE canonical initial-physics block, and the SubagentStart seam
that is CONFIGURED to hand it over (arrival is not observable here).

WHY THIS EXISTS. helm's north-star is that agents get the truth about the
substrate they run on. It failed on SUBAGENTS completely: `helm/hooks.py` SPECS
wired only UserPromptSubmit, which the harness fires for a real user turn and
NOT for the internal prompt handed to a subagent. So every SA ran with zero
premises, zero moves, zero guidance — measured 2026-08-25, and it is the reason
the owner's exit clause ("all TLAs AND SAs reliably understand how to
efficiently test") was mechanically unreachable no matter how the store was
worded.

WHY NOT capability.py. That index is a JIT resolver: a capability surfaces when
the agent's CURRENT reasoning touches its trigger vocabulary. A fresh SA has no
accumulated reasoning to trigger on and gets exactly one shot at start, so its
physics must be UNCONDITIONAL. Different delivery contract, different home; the
verb strings stay in sync because both quote GUIDANCE below.

THREE SILENT DROPS ON THIS PATH, read out of the shipped harness (2.1.243) and
recorded here because each one makes a CORRECT wiring deliver NOTHING while
`helm hooks status` reads green:

  1. hookEventName MISMATCH IS DISCARDED IN SILENCE. The harness validator is
     `if (e.hookEventName !== i) { count("hookSpecificOutput_event_mismatch"); return }`
     — it returns undefined, with no error, no stderr and exit 0. Reusing the
     inject envelope (which says "UserPromptSubmit") from a SubagentStart hook
     therefore delivers nothing and looks installed. THAT is why this module
     builds its own envelope instead of borrowing inject's: the event name is
     minted at the source, never inherited.
  2. ISOLATED-CONTEXT SUBAGENTS DISCARD IT ANYWAY. The harness delivery site is
     `if (contexts.length > 0 && !opts?.isolatedContext) push(hook_additional_context)`,
     and the same guard drops hook messages one branch above. An
     isolated-context SA receives nothing however correctly this is wired. That
     is a BOUNDARY to state, not a bug to fix — see COVERAGE_EXCLUSION.
  3. A managedHooksOnly FILTER can skip user hooks entirely. Measured benign:
     it is `source === "built-in" && agentType === "web-fetch"`, so it excludes
     only the harness's own web-fetch helper, never a work SA.

So a STRUCTURAL check cannot tell success from any of the three. The spec being
installed proves the hook RUNS; only a spawned agent quoting real physics back
proves it ARRIVED. Do not let this module's acceptance decay into a boolean.
"""

HOOK_EVENT = "SubagentStart"

# The ONE project whose physics this block states. A cwd that resolves to any
# other project — or to none — gets nothing rather than someone else's rules.
_PROJECT = "helm"

# The harness silently drops a payload whose hookEventName is not the firing
# event, so this name is minted here and never inherited from another envelope.
COVERAGE_EXCLUSION = (
    "isolated-context subagents: the harness drops hook context for them "
    "before it reaches the agent, so they are OUT OF REACH of this seam"
)

# THE CANONICAL VALUE. One string, quoted by the SubagentStart payload AND by
# the new-agent guide/help, so the two can never drift into disagreeing about
# what an agent should run. Keep it CONCISE — it is unconditional initial
# context and every line spends the SA's attention budget before it has read
# its own task.
#
# HONESTY RULE FOR EDITORS, and it is the defect this whole seam was built
# after: state only what is TRUE RIGHT NOW. Canon that asserts a capability
# which is not actually reachable is what cost this fleet fourteen days — a
# premise said "UNTIL task/1068 LANDS" for two days after it landed, and zero
# of 2,526 receipts used the capability it was gating. If a command below stops
# working, this string is WRONG and is fixed in the same pass, not annotated.
GUIDANCE = """\
helm physics (you are a subagent; this is your initial context):
- VERIFYING A CURE: route it through the fab, never locally. The rule is that
  a cure round is FOCUSED and the WHOLE SUITE (`fab gate --repo .`) belongs at
  the LAND gate. DO NOT TRUST THIS PARAGRAPH'S NUMBERS — RUN THE CHECK, which
  is local and takes seconds: `helm gate run --repo . --focus --plan` prints
  the selection without running anything, and REFUSES if your lane touches any
  non-Python file ("focus can compute consumers only for Python modules"),
  which is itself the answer — focus cannot help that lane at all.
  WHEN LAST MEASURED, focus selected ~95% of modules whatever the lane touched,
  because one constant-prefix import makes the CLI a consumer of everything
  under `helm/` — so a cure round cost whole-suite money. That is task/1563 and
  it is a coupling fact, not a slow node. If --plan now prints a small number,
  1563 moved and this sentence is the stale one. Either way the WHOLE-SUITE
  run is `fab gate --repo .`, no custom argv. `python3 -m unittest` is REFUSED locally
  whatever its shape — named module, single method, under nice, any
  interpreter spelling. Do not spend turns finding a wording that gets past
  it; there isn't one.
- THE CURE-ROUND ROUTE IS `fab test --repo . -- python3 -m unittest <modules>`
  run from the lane worktree: the test modules you touched PLUS every module
  that consumes a touched symbol (derive the set with `git grep -ln` over
  tests, never from the module's name). The fab guard admits it; it is the
  focused run the standing law requires; its Ran/OK line is testimony, never
  a receipt.
- DO NOT hand-roll `fab gate -- python3 -m unittest`: a custom argv on the
  GATE verb mints an unbindable receipt. And know what `fab test` lacks:
  helm's gate runner supplies an ENVIRONMENT — scope, cgroup, delegation —
  that `fab test` does not, so tests.test_gate and any module that needs that
  runner read RED under `fab test` for that reason alone (measured twice: 106
  of 121, then 123 of 123, failures one identical guard error, against the
  real gate's ONE). Those are not lane failures: leave them to the land gate
  and SAY SO in the report instead of curing them.
- A RECEIPT BINDS A TREE, NEVER A LANE. Any post-commit tidy — a noqa, a
  comment, an --amend — changes the tree and the receipt stops binding.
- A CURE-ROUND RECEIPT IS NOT LAND AUTHORITY in any case. The land gate is
  whole-suite on the composed tip.
- REPORT WHAT YOU MEASURED, not what you expect. A green check that never ran
  your arm is worth nothing; name the control that proves your probe saw real
  input.
- YOU ARE NOT YOUR SEAT. You inherit its name, session and environment, so
  helm reads you as the seat, but a Monitor you arm wakes YOU and leaves the
  seat deaf once you end. You are a subagent inside the seat's session:
  never arm, replace or stop a helm chat wait beacon; report to your parent.
- A FIX OR A REVIEW IS DONE ONLY WHEN ITS SIBLINGS ARE CHECKED. Name the
  question the touched code answers, `git grep` the function, its callers and
  the same predicate spelled elsewhere, check each one, and name them in your
  report. A sweep skipped costs one more build round per sibling, because
  each later finding is the first one's class on another surface. Store:
  curing-a-defect-owes-a-sweep-for-siblings-asking-the-same-question.
- A REVIEWER PATCHES ITS OWN MECHANICAL FINDINGS. It commits the cure in its
  own worktree, on a branch off the exact reviewed tip, never pushes, and
  returns that patch tip beside its verdict; a reader who wrote none of the
  composed tip re-reads it once. A DESIGN finding goes to a meld, and a brief
  that names a live system stays read-only. A reviewer briefed "read-only,
  edit nothing" turns each mechanical finding into a full builder round.
  Store: xfam-reviewer-fixes-its-own-findings; skill
  reviewer-implements-own-findings."""

# WHAT THE HOOK ACTUALLY INJECTS. GUIDANCE above is the canonical RECORD and
# stays whole; this is the delivery, and the two are different jobs.
#
# The block was 2,940 characters of unconditional context, spent before the
# subagent had read its own task, and six of its nine bullets were rationale
# and measurement history rather than a rule that changes an act. An agent that
# skims nine bullets obeys none of them, so the length was not merely a cost —
# it was working against the seam's own purpose.
#
# THE BEHAVIOUR-CHANGING RULES, AND ONLY THOSE: the local-run refusal and its
# fabric route, what a cure round does not authorise, the beacon, the sibling
# sweep a fix or a review owes, and the reviewer's own patch. Everything the
# bullets argued FOR those rules is one command away, and the HONESTY RULE
# above still binds it: an unreachable `--show` is this constant lying,
# exactly as an unreachable verb inside GUIDANCE would be.
#
# THE LAST TWO ARE HERE BECAUSE A STORE RULE IS NOT DELIVERY (owner: "it
# should be an integral part of helm"). As store entries they fire only as JIT
# whisper text, on the reader's own vocabulary, so a review agent briefed
# "read-only, edit nothing" never meets either: every mechanical finding then
# costs a full builder round, and every sibling of a cured defect costs
# another. This is the one block every build-capable subagent is handed
# before its first step, so it is their home.
#
# AT MOST 560 BYTES (tests/test_hook_budgets.py). It is unconditional context
# for every build-capable child, and the suite-guard deny, not this text, is
# what stops a local run: the text only has to name the route. The budget was
# 250 for the first three rules alone; the two above cost what they cost, and
# the read-only children below, 45% of the 779 measured, now pay nothing.
BRIEF = """\
helm physics for this subagent:
- Tests never run locally. Cure round: `fab test --repo . -- python3 -m unittest <modules>`, touched plus consumers; never land authority.
- Never arm, replace or stop the seat's chat beacon.
- A fix or review is done when every other place asking the same question (callers, the predicate elsewhere) is checked and named in your report.
- A reviewer commits a MECHANICAL cure in its own worktree off the exact reviewed tip, unpushed, and returns that tip with its verdict. DESIGN stays read-only.
More: helm saguide --show"""

#: Children that cannot build: the harness's read-only agent types, which are
#: told to search and plan, not to edit or run a suite. The brief is testing
#: and beacon physics for a child that edits and runs commands, and the
#: nested-spawn reflex binds a child that holds the Agent tool, which these do
#: not, so a read-only child is sent nothing. An unknown or missing type still
#: gets it: absent evidence is not evidence of a reader.
READ_ONLY_AGENT_TYPES = frozenset(
    ("Explore", "Plan", "claude-code-guide", "statusline-setup"))

# WHY THE FOCUSED ROUTE IS NAMED ABOVE ONLY AS `--focus --plan`, AND WHAT THIS
# NOTE USED TO SAY. It used to say `helm gate run --focus` must NOT be named,
# because MEASURED 2026-08-25 the python3 shim intercepted it into a full gate
# and a remote focused mint could not bind locally. It then carried a
# condition: "when the routing lane lands AND a focused mint binds end to end,
# the bullets become --focus".
#
# THE ROUTING LANE LANDED AND NOBODY CAME BACK TO THIS NOTE. That is the
# scheduled-lie shape in its purest form — a true statement with a trigger
# nobody watches — and it left this comment contradicting the block it guards.
# Re-measured 2026-08-26: `--focus --plan` is LOCAL and returns in seconds; a
# full `--focus` run completes and routes to the fab, which is correct because
# suites never run on the interactive box.
#
# THE RULE THAT SURVIVES: never put an unreachable command into the FIRST
# CONTEXT of every subagent — agents with no accumulated context to doubt it
# with, which is precisely why they need physics at all. So the block names
# `--focus --plan` (measured reachable, local, seconds) and does NOT name a
# bare focused run as the cure route. Any future edit here owes a fresh
# measurement in the same pass, and must not carry a condition nobody watches.


def guidance():
    """The canonical block. One accessor so no caller re-derives or trims it."""
    return GUIDANCE


def brief():
    """The block the SubagentStart hook injects — the behaviour-changing rules
    and the route to the rest. One accessor, for guidance()'s reason: a caller that trims this
    itself is a second editor of a constant whose whole contract is that one
    string reaches every subagent."""
    return BRIEF


def payload(raw):
    """SubagentStart hook JSON on stdin -> (envelope_dict, project).

    envelope_dict is None when nothing should be emitted. FAIL OPEN on every
    unhappy path: a garbled payload injects nothing at rc 0 and never blocks a
    spawn — an SA starting with no physics is bad, an SA that cannot start is
    worse.

    agent_id/agent_type are never read as identity: helm identity is
    inherited from the environment seam, and guessing a seat name out of a
    harness-supplied agent label is how a foreign row gets written under a real
    seat's name. agent_type is read for ONE thing, whether this child can
    build (READ_ONLY_AGENT_TYPES), and never echoed.
    """
    import json

    try:
        d = json.loads(raw)
    except Exception:
        return None, None
    if not isinstance(d, dict):
        return None, None
    if d.get("agent_type") in READ_ONLY_AGENT_TYPES:
        return None, None
    # Project scope derives from the payload's cwd, exactly as the
    # UserPromptSubmit path does. No cwd -> global-only, never a guess.
    cwd = str(d.get("cwd") or "") or None
    project = None
    if cwd:
        try:
            from .inject._ledger import project_for_cwd
            project = project_for_cwd(cwd)
        except Exception:
            project = None
    # THE SCOPE DECIDES. It used to be computed here and discarded by the
    # caller, so another project's cwd, a helm cwd and NO cwd all received helm's
    # testing physics byte-identically — scope COMPUTED, not scope APPLIED.
    # That is the follow-the-value-to-a-reader failure this module's own
    # docstring warns about, shipped inside it.
    #
    # FAIL CLOSED ON THE PROJECT, deliberately, and it is the opposite of the
    # fail-open rule above. A malformed payload is an INFRASTRUCTURE unknown
    # and must never block a spawn. A cwd belonging to another project is a
    # KNOWN ANSWER — the right physics for that agent is not ours, and handing
    # it helm's testing rules is worse than handing it none, because it cannot
    # cross-check what it is told.
    #
    # THE NESTED-SPAWN REFLEXES ARE NOT HELM'S PHYSICS AND ARE NOT FENCED
    # HERE (task/2971). Their rule binds every subagent, and this is the one
    # seam that reaches a subagent before its first step: measured in the
    # shipped harness (2.1.280), a SubagentStart additionalContext is pushed
    # into the subagent's opening messages, where a PreToolUse one on its
    # Agent call arrives only after that call has run. They carry their own
    # scope, which reflex.spawn_steers honours for this payload's project, so
    # they follow the brief for helm and come alone for any other project.
    lines = [brief()] if project == _PROJECT else []
    try:
        from . import reflex
        lines += [line for _rid, line in reflex.spawn_steers(project)]
    except Exception:                          # noqa: BLE001 — fail open
        pass
    if not lines:
        return None, project
    return ({"hookSpecificOutput": {"hookEventName": HOOK_EVENT,
                                    "additionalContext": "\n".join(lines)}},
            project)


def cmd_saguide(args):
    """saguide [--hook-json] [--show] — the SubagentStart initial-physics seam.

    --hook-json is the wired path: the harness's SubagentStart JSON on stdin,
    a hookSpecificOutput envelope carrying BRIEF on stdout. --show prints the
    canonical GUIDANCE block as plain text — the long form the brief points at,
    and the same string the new-agent guide/help prints, so a human and an SA
    who follows the pointer read the SAME record.
    """
    import json
    import os
    import sys

    # THE HOUSE GUARD, NOT A HAND-ROLLED COPY OF IT. This used to validate
    # flags itself, and tests.test_dispatch_honest caught that: a handler that
    # reads its flags by membership must call guard_tail or be declared exempt
    # with a probed alternative. Mine was neither — it was a private
    # reimplementation that happened to behave the same today and would drift
    # tomorrow, which is exactly what that structural test exists to prevent.
    # guard_tail carries the same suggest() did-you-mean, so nothing is lost.
    from .cli import guard_tail
    rc = guard_tail("helm saguide", args, flags=("--hook-json", "--show"),
                    usage="saguide [--hook-json] [--show]")
    if rc is not None:
        return rc
    if not args:
        # A BARE INVOCATION PRINTS USAGE rather than proceeding: guard_tail
        # only answers -h/--help, and a no-arg saguide has no work to do.
        print("saguide [--hook-json] [--show]")
        return 0
    if "--show" in args:
        print(guidance())
        return 0
    # --hook-json: FAIL OPEN on every path. A spawn must never die because its
    # physics could not be composed.
    #
    # THE READ IS ITSELF ONE OF THOSE PATHS, and it was the hole. stdin is
    # decoded as UTF-8, so a payload carrying one invalid byte raises
    # UnicodeDecodeError HERE — before payload(), whose own except therefore
    # never runs — and the hook exits 1 with a traceback onto a spawning
    # agent's stderr. Measured on this tree: a payload prefixed with NUL/0xff
    # exited 1 while every other malformed shape exited 0, so the docstring's
    # "every path" was false about the first path in the function. Decode
    # STRICTLY: a payload we cannot read is one we DECLINE, never a spawn we
    # break.
    #
    # STRICT, NOT `errors="replace"`, AND THE DIFFERENCE IS SCOPING. Replacing
    # kept a payload whose JSON still parsed — measured: one 0xff byte inside
    # an irrelevant string returned rc 0 and a full envelope. That looks
    # generous until the bad byte lands in `cwd`, because cwd is what DECIDES
    # which project's physics this block states. A silently repaired path can
    # resolve to the wrong scope, and this module's whole contract is that a
    # foreign cwd gets nothing rather than someone else's rules. Declining a
    # payload we cannot read exactly is the conservative half of that promise.
    try:
        if sys.stdin.isatty():
            raw = ""
        else:
            buf = getattr(sys.stdin, "buffer", None)
            raw = (buf.read().decode("utf-8") if buf is not None
                   else sys.stdin.read())
    except Exception:
        return 0
    env, _project = payload(raw)
    if env is None:
        return 0
    # A CLOSED READER MUST NOT BECOME A FAILED HOOK, and wrapping print() is
    # NOT enough: the write can succeed into a buffer and the BROKEN PIPE then
    # surfaces during interpreter shutdown, outside any try in this function.
    # Measured before this cure: a valid payload with the read end closed
    # exited 120 with BrokenPipeError on stderr. So the flush is forced HERE
    # where it can be caught, and on failure stdout is redirected to devnull so
    # the interpreter's own final flush has nothing left to fail on.
    try:
        print(json.dumps(env, ensure_ascii=False))
        sys.stdout.flush()
    except Exception:
        # dup2 DUPLICATES the descriptor; the original is still open and is
        # ours to close. Leaking one fd per broken-pipe hook is small and
        # unbounded is the wrong shape for a seam that runs on every spawn.
        try:
            null = os.open(os.devnull, os.O_WRONLY)
            try:
                os.dup2(null, sys.stdout.fileno())
            finally:
                os.close(null)
        except Exception:
            pass
        return 0
    return 0
