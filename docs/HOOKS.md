# Wiring `helm inject` into your harness

`helm inject` is the one active-fire surface: a hook calls it once per turn
with the prompt text; helm returns the context worth injecting — the pinned
lane (budget-capped), just-in-time typed-store matches, and live reflex
steers. No match, no output, no cost. The same call works from every harness,
which is what makes your knowledge fire wherever you work.

## The self-closing path: `helm hooks install`

For Claude Code you never hand-wire this — helm installs its own hook:

```console
$ helm hooks install            # every claude home + every seat, incl. ~/.claude
helm hooks: inject (UserPromptSubmit): timeout 10 /path/to/helm/bin/helm inject --hook-json || true
helm hooks: deliver (PostToolUse): timeout 2 /path/to/helm/bin/helm chat deliver --hook-json || true
helm hooks: join (SessionStart): timeout 5 /path/to/helm/bin/helm chat join --hook-json || true
  you-example-com              add    backup: none — new file
  (default-claude)             update backup: ~/.cache/helm/config-backups/…
helm hooks: seats (full hook contract — inject + delivery + handoff + resume):
  codex                        add    backup: none — new file
helm hooks: 2 of 2 claude homes covered
helm hooks: 1 of 1 seats covered (full hook contract)
$ helm hooks status             # per-home + per-seat coverage table, read-only
$ helm hooks install --dry      # the would-be diff per home/seat, nothing written
```

The installer merges the whole hook estate — 12 entries per credential home
and per family seat (one per `hooks.SPECS`/`hooks.SEAT_SPECS` row; the count is
test-pinned against that canonical tuple) — into each surface's
`settings.json`. Together they close the loop: turn start + tool boundary +
shell-argv gate + local-suite gate + session start + idle gate + compaction
continuity + compaction resume.

**ONE ROW IS AN EXTERNAL GUARD** (`external` in its spec): `suite-guard` runs an
executable helm does not ship. It is resolved BY NAME — `shutil.which`, or
`HELM_SUITE_GUARD` to pin a path — never as a literal host path, because helm's
own pre-push `hostpath_guard` refuses a `/home/<user>/` literal in a pushed
blob. It is OPTIONAL until configured: a host with nothing by that name on
`PATH` and no pin installs every other hook, and `helm hooks install` and
`helm hooks status` report it as "not configured (optional)". Once the
executable is on `PATH` or `HELM_SUITE_GUARD` is set, it is REQUIRED: a
configured guard that stops resolving gets the spec left OUT of what is
written (`hooks.resolved_specs`) rather than a hook pointing at nothing, every
install door returns a SHORTENED result, and `helm doctor` says so in one loud
row. Being in this table is the whole point of
task/1006: the guard existed for six days wired into `~/.claude` alone, was
hand-copied into 8 seat configs on 2026-08-11, and the two seats minted after
that patch were born unguarded while `helm hooks status` reported *"10 of 10
seats covered (full hook contract)"* — a guard named in no list is a guard no
census can count.

| event | command | what it carries |
|---|---|---|
<!-- docs/hooks/ holds hook rows; assembled at read time -->

The Stop command's cooperative budget is **17.5s inside the immutable 20s wrapper**. It follows the recorded 2x observed-sample policy (8.7s × 2, rounded), not the wrapper number. The budget is measured from the WRAPPER's start (`HELM_HOOK_T0`, read from `/proc/uptime` by `bin/helm-hook`), so interpreter startup is charged to it rather than discovered by the outer `timeout`. Before claims or seam starts, Helm requires its fitted cost plus a successor reserve to fit beneath the ambient deadline (the dispatch-ledger rung and its pin are gone: the fold runs in the resident). The 1.25× factor is an **inferred engineering margin**, selected after a fresh seam measured 6.942s—6.8% above the pinned 6.500s maximum—and rounded upward to 0.1s; it is not itself a measured fact. Since the claims and seam rungs read the resident's stop facts (task/3042) both were re-measured on the lane tip — 40 stops, fresh and post-commit, at loads 11.6-13.2 and 21.8-27.8, claims at most 0.140s and seam at most 0.050s, both on the busier run — so claims is admitted at a fitted 0.2s and seam at 0.1s, and claims reserves 2.7s for its successors instead of 8.0s (the seam fat tail that reserve covered is gone). A rung exceeding its fitted cost, or the outer 20s wrapper firing before fallback publication, falsifies the fit and requires remeasurement. An admission miss makes that rung explicitly `COVERAGE UNKNOWN` and still runs the later ladder, so one fat-tail read cannot erase whisper, claim-evidence, mechanical work, or response. **`COVERAGE UNKNOWN` is a WARN and never refuses the stop.** It used to be filed as a block, and that refusal could not be discharged by anything the seat could do: the next stop re-measured the same rung on the same board, overran again, and refused again — measured 2026-09-11, a seam rung taking 11.4s and 12.2s against its fitted 8.7s while every other rung finished under 3s, refusing one seat's stops in a row with no finding in any of them. An unexamined rung is an absence of looking, not a finding; blocks from rungs that DID finish still refuse on their own evidence, and on such a stop the advisory channel is suppressed as it always is beside a block. The current evidence is a provisional 339-run transcript-retention sample from three Claude Code seats; no native Stop timing ledger or non-Claude family sample exists yet. The pinned provenance, replay, falsifier, and re-measurement limitation live in `tests/fixtures/stop-guard-ladder-2026-09-10.txt`.

### Stop facts: what a Stop reads instead of rebuilding the world

Every Stop is a new interpreter on a shared, loaded box, and it used to fold the whole dispatch ledger (p50 7.4s on the live fleet) and ask git eight to twelve questions per held lane. Those facts are the same for every stop until an input moves, so the supervised `helm web` resident computes them — with the guard's own functions, `_gate_pending`, `_room_unfinished`, `_dispatch_advice`, `review_spiral` and `dispatches.owed` — whenever an input moves, and writes them behind itself to `_global/web-cache/stop-facts.json` (helm/stopfacts_resident.py; one writer, under a LOCK_NB lock).

A Stop reads that file (helm/stopfacts.py) and checks, with no subprocess, whether each fact is still exact:

| witness | how | what a mismatch does |
|---|---|---|
| dispatch ledger | one `os.stat`; if it grew, only the appended bytes (at most 64 KB) are read | only the leases and seats those bytes NAME go STALE |
| code | a digest of the package's source stats against the snapshot's | every fact is STALE (the resident runs other code) |
| lane HEAD | the worktree's HEAD file and branch ref against the ones recorded before the resident read the lane | that lease is STALE ("HEAD moved since") |
| trunk | the trunk ref file (or the packed-refs stat) | advice only: landedness is marked as aged |

Each fact is EXACT, STALE (with its age and reason) or ABSENT (no snapshot, older than 600s, or not yet computed). A witness only ever DOWNGRADES. An exemption (LANE IN GATE, APPROVED) comes only from EXACT facts, and its proof (`seats_delegation._gate_pending`) takes a dispatch row as this lane's only when the row was written in the lease's own repository (`repo_id`) and its label or its bound `ref_branch` names the lane; an owed frontier it cannot measure, or a snapshot it cannot index, grants none (task/2387); a STALE or ABSENT lease keeps its block, its line carries the age and reason, and one line names the snapshot's standing. **A reading is never waited for** (owner rule: fail-closed rungs never wait on state). The commonest STALE reading is this seat's own commit a moment before its stop; the lane is unfinished anyway, so the lease is held at once with the line printed for unproven idleness and the snapshot's age, and the resident's next poll is what the next stop reads. `helm doctor` reads the same header (`check_stop_facts`).

**After a land the resident re-execs itself onto the new tree.** Every hook compares the snapshot's code digest with its own, so a land makes every fact STALE ("computed by other code") until facts computed by the new code exist. Nothing else restarts the resident — no path unit, land step or supervisor is relied on — so it does it: once the tree's digest has read the same for one poll (a checkout still writing files is waited out) and the new tree imports in a child interpreter (a tree that does not import is never exec'd onto, and the console keeps serving), it lets any snapshot write in flight finish, releases the writer lock and `os.execv`s its own command line — same interpreter, argv (so port) and environment. The new image marks the facts it finds as being refolded (ABSENT, never exempting) until its first write, so a land costs about one full compute of exemptions. A refused import check or a failed exec is not retried until the tree moves again, and says why in the server's log. `helm doctor` names a resident whose loaded code is older than the tree: a WARN for the first `REEXEC_WITHIN_S` (60s) after the change, a FAIL after it, and a separate FAIL for a resident running another checkout.

**The untested-composition (seam) rung reads the same file.** It used to list every worktree of the repository, walk /proc, read the whole gate-receipt ledger and ask git about every live pair on the stop (1.4-4.8s per stop). The resident's seam leg does that per repository (`stopfacts_resident.compute_seam`: `work._gc.seam_rooms`, `green_receipts`, and `seam_candidates` once per LIVE room), on its own read-behind key so a slow seam refresh never holds back the lease facts, and writes `seam.roots[<repo>]` into the snapshot. The stop finds its repository and its room from the checkout's own files and the recorded registry, adds its leased rooms, and assembles its answer from the per-room rows (`work._gc.seam_assemble`: the rows whose peer is outside its own rooms and not provably its own — an arm pins that this equals the one-call answer). Its witnesses:

| witness | what a mismatch means |
|---|---|
| worktree registry (`<common>/worktrees` stat) | a room was added or removed: STALE |
| each seat's recorded cwd (a digest of the roster's `cwd` fields, not its stat) | attribution moved: STALE |
| the lane-lease projection, re-derived from the claims file | a room gained or lost its lease: STALE |
| the trunk ref, the gate-receipt, import and binding ledger stats, and the HEAD of the stop's own rooms and every live peer | a row may have moved: STALE — asked only when the stop has a room and a live peer, since with no pair there is no row for them to move |

Occupancy (/proc) has no witness a stop could check; the resident re-reads it on its minute refresh and the reading carries its age. The seam rung is advisory about facts it does not have: a STALE or ABSENT reading, or one computed by other code, is one `composition-seam rung has no exact reading` WARN naming why, and never a block. An EXACT reading decides exactly as the rung always did (blocks, latches, the blind-spot disclosure). A room that is neither occupied nor leased is not computed, so a seat standing in one is told UNKNOWN. The resident finishes the legacy import-placement scan the stop used to cut off at 0.25s, so the seat gets the complete answer.

### Act denies and steers on the argv-guard pass path (task/2980)

Guidance that belongs to an ACT is said at the act, and the prompt-lane trigger it replaces is retired in the same change (`helm/actsteer.py`):

- **Denies** (exit 2, one line of at most 300 characters that carries the cure): a `pkill -f`, or a `pgrep -f` that feeds a kill, whose pattern matches its own command text (the kill ends the seat's own turn at exit 144; the refusal prints the bracketed pattern that does not match itself); and a `gh pr|issue create|edit|comment|review|merge|close|reopen` body (from `--body`, a heredoc or `--body-file`), or a `gh api` body on an issues or pulls endpoint (a `body=` field or an `--input` JSON file), that carries an AI authoring line as `helm/trailer_rung.py` reads one (owner canon: nothing on GitHub names a model as its author). Not read, and not claimed: a body gh takes from a pipe, a body the shell computes (`"$(cat f)"`), `gh release --notes` and `gh api graphql`. A command named inside quoted text is data to both denies, so a body that QUOTES `pkill -f` passes.
- **Steers** (one line of at most 250 characters, once per context): `git --stat` piped to a search; a scrub or untrack; a pane send; timing a command under `/usr/bin/timeout`; a session transcript read by hand; a chat claim post with no CL%; a long foreground command on a non-claude seat; an outward push, PR or issue on a repo origin does not own, or a repo made public; a lane claim or a new source module (sweep first); an edit to a shell script a live shell is running; a read-only `pgrep -f` that matched its own shell. A PreToolUse line reaches the model with the call's result, so each is worded for an act that has already run.
- **The narrow-goal question, once per turn** (`helm/narrow_goal.py`): the turn's first build act (an edit; `helm work claim`, `helm dispatch send` or `helm task add`; a non-read-only Agent spawn or a Workflow; a Bash command that writes a file, unit or script outside `/tmp/`, `/dev/` and the scratchpad) is refused with one line of at most 250 characters: "am I optimizing on too narrow a goal?" plus the `Wider goal: <goal> · for: … · waits: … · exists: … · obvious: …` line that answers it. A turn whose visible text already holds a line beginning `Wider goal:` pays nothing, and so does a seat in an open meld for the task the call builds, because the meld is the plan. The question is asked while planning, not at report time, because by then the narrow thing is built. The latch is keyed on the recorder's `turn-opened-at`, so the retry passes; a subagent call, a session with no turn edge, an unwritable latch or any error never blocks. `HELM_NARROW_GOAL=0` turns it off.
- **Once per context:** the SessionStart reset that re-arms inject's pinned lane (`resumeturn`) also calls `chat.forget_steers`, so a steer latched before a compaction or `/clear` may speak once more. A subagent latches apart from its seat (the payload's `agent_id` is part of the latch), so its call never spends the seat's line; the reset clears both. A session that ended has no next boundary, so `helm gc`'s `chat-steer-latches` row reaps the latches of a dead session (the liveness answer `chat-cursors` reads).
- **Retired:** `helm sync` trims the keyword cells listed in `actsteer.MOVED` from their store entries (once, and only while every listed cell is still there), and re-keys the `publication-boundary` and `sweep-before-you-build` reflexes to the `act` signal and `cv-first-lookup` to a narrower pattern (`reflex.REKEYED`). It also retags the entries in `doors.ROUTED` with their door route cells (task/1135). `helm sync` takes no arguments and has no dry run, so each of these is a write to the live store.

  (task/3858) A doors index/rebuild on the argv-guard path (for bound acts) is now live-sized: with HELM_DOOR_SHADOW off (default) it writes only the routes index; the phrase marshal cost is paid only on the shadow path. Old "20-27 ms" claims were pre-gate.

## The per-tool-call hook budget

**Two of these hooks fire on EVERY tool call** — `PreToolUse` argv-guard and the
`PostToolUse` pair — so their cost is not paid per turn, it is paid per Bash
call, per seat, on every project on the box. That is the whole reason this
section exists. Read as a seat this morning, every one of them was printing its
own timeout banner several times a turn:

    [helm argv-guard] THE GUARD TIMED OUT after 2s — this tool call is ALLOWED and UNCHECKED
    [helm posttoolrun] delivery: handler timed out after 2s — event ALLOWED and UNCHECKED

**Both halves of that were real problems and they are different problems.** The
guard really was failing OPEN — the tool call ran unchecked — and delivery
really was being dropped; that is not noise, it is the fail-open law working as
designed and saying so. What was broken was that it said so a dozen times
before anything else could be read, which is how a loud guard becomes an
ignored one.

### Where the time went, measured

Measured on the hub between load 15 and 30 on 8 cores, interleaved so both arms
share one load window (a sequential before/after on this box measures the load,
not the change):

| | wall p50 | CPU p50 |
|---|---|---|
| argv-guard, before | 0.54 s | 0.35 s |
| argv-guard, after | 0.11 s | 0.10 s |
| `python3 -c "import helm.cli"`, before | 0.28 s | 0.26 s |

**The single largest item was not helm's at all.** `python -X importtime`
priced a bare interpreter start on this box at a 488 ms `site` stage, 250 ms of
it `usercustomize` — the machine-local local-suite guard, which imported
`unittest`, `unittest.suite` and `doctest` (and through doctest, `pdb`) at
EVERY interpreter start. With about four hook processes alive at any instant
fleet-wide, that is roughly 2.4 cores spent permanently on interpreter
startups. That guard is not in this repo; it was cured at its source by moving
its doors onto the `sys.meta_path` finder it already used for pytest, so the
wrappers are installed only if a test engine is actually imported. The site
stage measures **14 ms** after, and all seven refusal doors were re-probed
individually against the old file to prove the guard is unchanged.

**helm cannot ship that file, so it MEASURES it.** Half of this cure lives
outside the repository, and every arm in this tree stays green whether or not
it holds — so a rebuilt box would lose the hook budget with nothing saying why.
`helm doctor`'s `check_startup_doors` rung starts a child interpreter and asks
the two questions the file's bytes cannot answer: does a fresh start already
carry `unittest`, `doctest` or `pdb`, and is that same child still refused a
one-case suite. Lazy plus refused is one OK row carrying the measured
site-stage cost; an eager site stage is a WARN naming the cost, the artifact (a
`usercustomize.py` in the interpreter's own per-version user site directory)
and the cure; a measurement that could not be taken is a WARN that says
UNKNOWN. The refusal half is what stops an ABSENT guard reading like a lazy
one — a box rebuilt without the file imports nothing at startup either.

The two items that ARE helm's, both import-time and both cured here:

* `chat.cmd_chat` derived a default ROOM before dispatching any verb, which
  imported `helm.seats` and the fifteen modules behind it — **93 ms of CPU on
  every Bash and Monitor call** for a gate that reads one command string and
  never touches a room (its advisory rungs read a roster only behind a string
  gate of their own). `argv-guard --hook-json` is now answered before that
  prologue. `tests/test_chat_argv_guard.py` binds it structurally rather than
  by a clock: the hook verb must not IMPORT `helm.seats`, with a refusing
  payload as the control that the probe reached the guard at all.
* `cli` imported `registry` (45 ms) and `hooks` imported `configs` (27 ms) at
  module scope, for verbs no hook runs. Both are imported where they are used.
  `wiring.graph` reads function-level imports, so the module graph and the
  reachability law see the same edges they saw before.

**An interpreter flag was measured and REFUSED.** Launching the hook entry with
`-S -E -s` was the obvious next step while `site` cost 488 ms. After the
`usercustomize` cure it buys **7 ms**, and it would move the executable out of
word 3 of the command — which `envtidy._helm_args`, `hooks._executed`,
`posttool.recognize` and `cred._retired_guard_command` all read to recognise
helm's own hooks. Seven milliseconds is not worth four identification parsers.

### One line per hook class per window

The alarm now says three things and stops: which hook, what budget, what the
seat lost.

    [helm argv-guard] TIMED OUT at 2s — this tool call is UNCHECKED
    [helm deliver] TIMED OUT at 2s — pending chat is deferred to the next call

**UNCHECKED survives the trim on purpose.** A silent fail-open is worse than a
loud one and 2026-08-04 is what it costs; the sentence keeps the fact and drops
the rest.

**A timeout carries its likeliest cause.** On 2026-09-29 every seat's hooks
timed out together because `agents.slice` itself was stalled, and each line
said only TIMED OUT. The line now ends with the fleet's stall as the kernel
counted it at the kill (task/3714):

    [helm argv-guard] TIMED OUT at 2s — this tool call is UNCHECKED; fleet stall (agents.slice PSI some avg10): cpu 62.53%, memory 41.00%

or `; fleet stall UNKNOWN: <why>` when it cannot be read. The rc-124 arm of
`bin/helm-hook` reads it with shell builtins only, and the in-process handler
timeout in `helm/hookrun.py` reads the same files (`seatceiling.stall_clause`)
and puts the clause in the outcome's `why` as well. With
`HELM_SEAT_PRESSURE=off` and no `HELM_FLEET_CGROUP` nothing is read and the
clause is absent. `helm pressure-watch` is the watcher that alarms on a
sustained stall.

**A repeat inside the window prints nothing and is counted.** The first timeout
of a class speaks in full; every repeat appends to a counter, and the next line
that speaks carries the arrears — `(+4 suppressed since 18:30Z)` — so the count
is deferred, never dropped. The machine-readable half never moves:
`hookoutcome.declare` runs whether or not this event is the one that says it
out loud, so no consumer of an outcome sees a suppressed failure as an answer.

**BOTH HALVES READ THE COUNTER, and for one release the shell half did not.**
It wrote a byte per repeat and drained nothing, so for the two banners the
owner actually reads — which come from the sh ladder BY CONSTRUCTION, because a
timeout means the helm process that would have printed from Python was already
killed — the count was dropped rather than deferred, while this page and the
module docstring both promised otherwise. Each half now drains the keys it
writes, and `tests/test_hookalarm.py` compares the two renderings byte for byte
against one seeded state.

**The keys are per CLASS and the two halves do not share them**, which is
correct and reads like a bug: the sh ladder keys on the spec name
(`deliver`), `hookrun` on `handler-<name>` and `posttoolrun` on
`posttoolrun-<phase>`. Those are three different failures with three different
sentences — the whole wrapper killed, one in-process handler over budget, one
phase of the composite — and a shared key would let one silence the others.
What has to be shared is the FORMAT, and that is what is tested.

**The since-stamp is measured, not assumed.** The clause used to read "in the
last 10 min" while the drain summed every bucket it could see with no age
limit: seeded six hours back, it still said "in the last 10 min". The oldest
bucket that contributed is in the filename on both sides, so the clause names
it.

**Every failure path of the alarm exits 0 and still prints.** The 124 arm
creates its marker with `printf '' > "$f"` and never with `: > "$f"`: `:` is a
POSIX SPECIAL BUILT-IN, and a redirection error on one makes a non-interactive
shell exit immediately. Measured under `/bin/sh` (dash — what these wrappers
actually run under) with the alarm directory uncreatable, read-only, absent
under an unwritable parent, or a non-directory: **rc 2, silent, on all four**,
where bash printed and exited 0. rc 2 out of a PreToolUse or Stop wrapper is
BLOCK, so a hook that merely TIMED OUT became a refused tool call — or a turn
the agent cannot end — with no sentence naming the hook. An internal failure of
the diagnostic must never become a refusal the owner cannot diagnose. The other
dash-safe spelling, `{ : ; } > "$f"`, is unusable for a different reason:
`hooks._segments_ex` reports `brace group`, callers that abstain DISOWN the
entry, and helm would stop recognising the hooks it wrote across the estate.

`hooks.hookalarm` owns both halves, and it has to own both because **a hook
that timed out is dead**: the process helm would print from has been killed by
its own `timeout`, so that line comes from the `case` ladder in the generated
wrapper, which is POSIX sh with no helm in it. The two agree on files and
nothing else — `<dir>/<hook>.<window>`, where the window is a UTC clock stamp
with its last digit dropped, existence means "already spoke", and size is how
many repeats were swallowed. `tests/test_hookalarm.py` EXECUTES the sh half and
reads it back with the Python half in both directions, because two
implementations agreeing in prose and disagreeing in a path is a failure
nobody would notice: the state splits and every line simply prints twice.

The window is ten minutes rather than the five first asked for, and the shell
is why: five needs an integer division, `$(( … ))` is on helm's own
`_UNMODELLED` list, and generating it would make `_segments_ex` abstain on
every hook command helm writes — and the callers that abstain DISOWN the entry,
so helm would stop recognising its own hooks across the estate. Ten is what
the clock can spell without arithmetic, it is strictly quieter, and the arrears
carry everything the wider window swallows.

`HELM_HOOK_ALARM_DIR` names a private window. A gate-suite child that names no
directory always speaks and writes nothing — a suite is not a fleet, and
durable cross-process suppression there would silence later arms and read as a
missing diagnostic rather than as a leaked channel.

### Presence is not currency

`helm hooks status` and `helm doctor` used to report a whole estate as un-wired
the first time a hook template changed. `_lane_live` matches the installed
command EXACTLY, so every entry helm wrote before the change read as `missing`,
and every count built on `missing` said **`inject coverage 0 of 7`**, **`delivery
lane 0 of 7`**, **`continuity lane 0 of 7`** — about hooks that were firing on
every tool call, on the same screen as a table printing `deliver yes`.

`stale_specs` already drew the line — an `update` action means the entry EXISTS
and would be re-rendered, an `add` means it is absent — and the MISSING *row*
already subtracted it. Only the COUNTS did not. `hooks.carried_names`,
`hooks.missing_names` and `hooks.lane_live` are now the one door the table, the
counts and the rows all read, so they cannot disagree again, and the seven
identical per-home drift lines collapse into one that names the count and the
homes.

Coverage is now PRESENCE. The vintage keeps its own row in `hooks status` and
gained one in `doctor` that it never had — loosening the count without giving
the fact a row of its own would have retired the only place the estate could
learn a re-render is owed, so the loosening and the new row are one change.
STALE and DRIFTED stay opposite failures: STALE means the config names
something this host cannot run, DRIFTED means it names something that runs and
is not the current spelling.

**`scripts/deploy.py` is deliberately NOT the place this heals.** It publishes
one immutable, digest-verified artifact under a lock on the releases directory
and writes nothing outside its own root; teaching it to rewrite settings.json
across every credential home would make a hermetic publisher a fleet mutator.
The estate already self-heals where a session begins: `hooks.preflight` calls
`install_home` on the way in, so a seat that starts a new session re-renders its
own home. What was missing was an honest reading in between, which is what the
above restores.

It does, however, now REFUSE an artifact that would arrive unguarded.
`bin/helm-hook` is the second entry point — every generated hook command execs
it — so it sits in `REQUIRED_FILES` beside `bin/helm` and in the executable-mode
check both the archive and the staged tree pass through. Shipping it absent or
non-executable would make every hook in the estate exit 127 or 126 before its
first line, which the harness reads as ALLOW and nothing in helm's voice can
report; that is a publication the deployer declines to make.

### A blocking lock inside a bounded budget

`PostToolUse delivery: handler timed out after 2s — event ALLOWED and
UNCHECKED` was the owner's live symptom, and the cause was neither the size of
his chat backlog nor the dispatch-ledger walk. **Every delivery timeout
captured from the latency ledger on this host named the same frame**:

    hooklatency.flock < proxywatch.delivery_state_guard < seats_identity._delivery_guard
      < seats_delivery.deliver_any < seats_cli.cmd < chat.cmd_chat

Five consecutive timeouts in one minute, each having waited **1.6-1.9 s of its
2 s budget** for a fleet-wide `LOCK_EX` and then been killed before reading a
single room. The holds are short (`lock-proxywatch` p50 0.2 ms, p95 0.3 ms);
the QUEUE is not, because every seat takes that lock on every tool call.

An unbounded blocking acquisition inside a bounded budget has exactly one
ending. The delivery boundary now waits with a deadline — **half the budget
left on its own `ITIMER_REAL`**, read through `hooklatency.remaining_budget()`,
so the arm and the read cannot drift and no number has to be re-measured when a
budget changes. Half guarantees the delivery keeps at least as much time as the
wait spent. If the deadline passes, the boundary yields `seats_identity.BUSY`,
declares `hookoutcome.SKIPPED`, delivers nothing and changes nothing; the next
tool call retries against the same cursors. Delivery is idempotent and
cursor-driven, so a skip costs one boundary of latency where a kill costs the
whole event plus a line the owner cannot act on.

Measured, interleaved, six pairs, one contending holder, against a copy of the
live 332 MB chat store:

| tree | delivery stage | outcome | owner banner |
|---|---|---|---|
| before | 2001-2034 ms | `timeout` ×6 | `TIMED OUT at 2s … UNCHECKED` |
| after | 1007-1035 ms | `skipped` ×6 | none |

**The bounded wait is OPT-IN and nothing else learned to skip.** `record()`
writing proxywatch state, and the chat writer that replaces it, are not retried
by a next tool call — a silent skip there would lose the write the lock exists
to order. They pass no deadline and block exactly as before.

`hooklatency.flock` takes the deadline from its CALLER and never invents one:
the instrument still replaces no flags and no deadline of its own. A skipped
acquisition records `skipped`, which is a word already in `OUTCOMES` — the
first cut wrote `busy`, `_valid` rejected the row, and the probe read twelve
span STARTs against six ENDs, losing exactly the acquisitions the change exists
to show.

### The measured envelope

Through the shipped `bin/helm` entry, interleaved against the trunk binary,
neutral cwd, `nice -n 19`, on an 8-core box:

| load | argv-guard CPU p50 | CPU p95 | wall p50 | wall p95 |
|---|---|---|---|---|
| ~18 | 85 ms | 112 ms | 86 ms | 112 ms |
| 27-30 | 154 ms | 230 ms | 280 ms | 429 ms |

**argv-guard meets the 150 ms CPU target at load at or below 20 and misses it
at load 27 to 30; the 2 s wall budget is met with margin in both windows** —
at load 30 the wall is still 4.6x inside it. The CPU target is the one that
moves with the box, and it is a target rather than the contract: nothing fails
when it is missed, and the wall budget is what the wrapper enforces. The
mechanism behind the margin is structural rather than a lucky clock: on the
guard path the lane imports NONE of `helm.seats`/`registry`/`configs`/`vcs`
where trunk imported forty such modules.

### The interpreter floor (task/3040)

Every hook is a new process, so every hook pays the cost of STARTING one
before any helm code runs. On the agents box that cost was most of the hook:
`#!/usr/bin/env python3` reaches a bash shim on `PATH`, and the interpreter it
chooses runs a site stage whose machine-local `usercustomize.py` imports
`unittest` and `doctest` eagerly (0.27-0.72 s of CPU per start, measured
before this change).

`bin/helm-hook` now starts helm's own child as `<interpreter> -S -- <checkout>/bin/helm …`:

- **Which interpreter.** The one the shebang path actually ran, recorded by
  helm itself. On a miss the wrapper takes the old shebang path and passes the
  child the `python3` it found on `PATH` (`HELM_HOOK_INTERP_KEY`) and the
  record file (`HELM_HOOK_INTERP_RECORD`). `cli._record_hook_interpreter`
  writes `<that python3> TAB <sys.executable>` to
  `_global/.state/hook-interp` in the helm home. The next hook whose `PATH`
  finds the same `python3` execs the recorded interpreter directly. The
  interpreter is never a hardcoded host path.
- **Why `-S` is safe.** helm is stdlib-only. No import anywhere in `helm/` or
  `bin/helm` (guarded or nested) names a module outside the standard library,
  helm ships no `.pth`, and no helm source calls a builtin that only the site
  stage defines. `tests/test_site_stage_precondition.py` pins all three
  facts.
- **When the old path is kept.** For every state that is not proven: a child
  that is not the `helm` beside the wrapper, a first line that is not exactly
  `#!/usr/bin/env python3`, a child that is not an executable file (126 and
  127 keep their meaning), no `python3` on `PATH`, no record, a recorded
  interpreter that is gone, or a `PATH` python3 newer than the record.
- **What it does not follow.** The record is keyed on the `PATH` python3 file,
  not on what a shim behind it would choose today. If the host changes which
  interpreter the shim picks while the recorded one stays installed, hooks
  keep running the recorded one until it is removed or the shim file changes.
  `helm doctor`'s interpreter-startup rung prints the interpreter hooks run.

One behaviour changes with it, in the direction the code was written for.
`hookalarm._under_a_test_runner` reads "`unittest` is in `sys.modules`" as
"this process is a test runner", and a test runner gets no rate limit on the
Python-side timeout alarm. On a box whose site stage imports `unittest` at
every start, every production hook matched that test, so the alarm printed
every line and never used its one-line-per-window limit. A hook started with
`-S` imports no `unittest`, so the limit now applies as its comment says it
does. The sh-side `124)` arm always had the limit.

The budget had a floor as well. The uutils `timeout` (0.8, the default
`timeout` on a rust-coreutils host) waits for its child in 100 ms polls, so
every hook's wall time rounded up to the next 100 ms. The wrapper uses GNU
`timeout` when the host ships it as `gnutimeout`. The exit codes this file
reads (124, 125, 126, 127 and 128+N) and the process-group kill were measured
identical for both.

Measured on the agents box, interleaved against trunk, hermetic helm home,
from a cwd outside helm, 25 runs per arm at 1-minute load 12.5-16.8 on the
8-CPU agents slice:

| surface | trunk p50 / p90 | this change p50 / p90 |
|---|---|---|
| argv-guard | 207 / 310 ms | 110 / 144 ms |
| deliver | 207 / 309 ms | 101 / 130 ms |
| PostToolUse composite | 207 / 312 ms | 101 / 150 ms |
| stop-guard, non-helm project | 207 / 307 ms | 106 / 149 ms |

`helm doctor`'s interpreter-startup rung now also launches a child through a
copy of the wrapper. An eager site stage that no hook pays is OK. An eager site
stage that hooks still pay is a WARN, and the WARN names the reason.

### The hook resident (task/1825)

The interpreter floor makes a cold start cheaper; it is still a cold start. On
2026-09-30 the fleet stalled twice on CPU: hook processes held 4.5 of the
agents slice's 8.7 cores at about 15 births a second, and a cold argv-guard
still cost about 133 ms of CPU after the box's site stage was fixed. argv-guard
runs on every Bash, Monitor, Write, Edit, NotebookEdit and Agent call of every
seat.

So argv-guard is now answered by a warm process when one is serving:

```
bin/helm-hook (sh)  --served argv, recorded interpreter, resident dir-->
bin/helm-hookres (bash, starts no python)  --TCP 127.0.0.1 + token-->
helm hooks resident: accept, fork; the child checks the call, says
  `ready PROOF`; on the client's `go` it runs helm's own `chat argv-guard`
  as the caller and says `rc N`.
```

- **The answer is the cold answer, by construction.** The child takes the
  calling process's environment (`/proc/PID/environ`), cwd and umask, and its
  stdin, stdout and stderr as the same open files (`pidfd_getfd`). The hook
  reads the harness's payload from the harness's pipe and writes to the
  harness's pipes. Nothing is copied back through the socket, so a pass and a
  refusal are the bytes a cold run writes. tests/test_hookres.py compares
  them, byte for byte, for a pass, a pass that prints a steer, and two
  refusals.
- **Fork per call.** The parent imports helm once and never runs a hook, so
  each call starts from the state a cold interpreter reaches after its
  imports: no cache, latch or memo carries from one call to the next.
- **What a fork cannot change is checked.** A module that reads the
  environment while it imports keeps the resident's value, so the resident
  records every key its modules read while they import and refuses a caller
  whose value differs. The record starts at the first import: the modules
  loaded before the resident runs (helm itself reads `HELM_GATE_LOADS_DIR`)
  are imported again by a fresh interpreter with the recorder installed
  first. Every `PYTHON*` variable, `TZ`, the locale's character set, the
  interpreter and the checkout are always compared.
- **Exactly once, warm or cold, never neither.** Every refusal comes before
  stdin is read. The client's `go` is the commit point: the child holds the
  caller's stdin and stdout from admission, but reads and writes them only
  after it reads `go`. A resident too slow to say `ready` inside
  `HELM_HOOK_RESIDENT_WAIT_S` (0.5 s) can therefore never run the hook beside
  the cold run the client falls back to. The child takes the caller's stderr
  only after `go`, because the client's `go` write moves only its stdout (to
  the socket, for the length of the builtin), so a client stalled there is
  still served. A failure between `go` and the hook is `refuse PROOF WHY`,
  and the client runs the call cold. After the hook starts, a resident that
  hangs up is an UNKNOWN outcome: the client says so and exits 70, which the
  wrapper reports as a failed guard (the call is unchecked), never as a
  pass.
- **It fails open and says so.** Down, gone, stopped, not beating, slow,
  stale or an environment it cannot stand in for: one line on stderr,
  `[helm argv-guard] hook resident <why>; this call runs cold`, then the exact
  cold command the wrapper would have run, under the same budget. Three
  states are silent: no endpoint file (no resident serves this checkout for
  this helm home), an answer without the resident's proof (see Auth), and a
  busy resident (at its limit of calls in flight, or unable to fork), which
  refuses before it reads the token and so without the proof.
- **A resident that cannot accept is never dialled.** Before it connects,
  the client reads, with builtins, the endpoint pid's `/proc/PID/stat` and
  the resident's heartbeat. A pid that is gone (or now names another
  process, by its start time) or stopped (state `T`) runs cold. So does a
  heartbeat that is missing or more than 3 s from now: every poll (once a
  second) writes the epoch second to `<endpoint>.beat`, so a stale beat
  means the serve loop is not running. That covers a resident that is alive
  and reads as running but does not accept (a frozen cgroup, a loop stuck in
  the kernel). Without it, its accept queue fills, each connect blocks until
  the wrapper's budget, and a gate's timeout ALLOWS the call unchecked, on
  every seat at once.
- **Auth, both ways.** TCP on 127.0.0.1 is open to every local user. The
  endpoint file is `0600` in a `0700` directory (`_global/.state/hookres/`
  in the helm home) and holds two secrets. The token proves the caller: a
  request without it is refused as soon as its token field arrives. The
  proof proves the resident: it rides `ready PROOF` and every refusal to a
  caller that sent the token, and never an answer to one that did not. An
  endpoint outlives a resident killed without cleanup, and anyone may then
  listen on its port, so the client compares the proof with a builtin and
  trusts no answer without it: the call runs cold, and none of that
  answer's text is printed. The request names the calling process's pid, and
  the child refuses it unless that process holds this very connection on its
  fd 3 (the kernel's socket table gives the connection's inode), so a token
  holder cannot name another process. One pidfd, opened first, backs every
  read of that process for the rest of the call, so a pid reused after the
  client dies is never read.
- **It follows its code.** It uses the `helm web` follower
  (`stopfacts_resident.Follower`): when the tree's digest moves and settles,
  it removes its endpoint, checks that the new tree imports, and re-execs in
  place. Between a land and that poll, every call compares the checkout's
  HEAD with the HEAD it loaded, so a trunk change is refused at once.
- **Who runs it.** The console `helm web` keeps one alive
  (`hookres.supervise`), started with the interpreter the hooks run. It lives
  in the `helm-web` unit's cgroup and stops with it; a call it has said
  `ready` to ignores the unit's SIGTERM and finishes, so a web restart never
  turns an in-flight call into an unchecked one. `helm hooks resident
  --status` says whether one is serving. `HELM_HOOK_RESIDENT=off` turns off
  both the starting and the asking.
- **It does not start where it cannot take a socket.** Claude Code gives a
  hook sockets for its stdin, stdout and stderr, and only `pidfd_getfd` can
  take a socket from another process. Where the kernel refuses that for a
  process that is not the caller's descendant (`kernel.yama.ptrace_scope` 1
  or more), a resident would refuse every call and only slow it. So it
  probes `pidfd_getfd` against its parent when it starts (a probe against
  itself always passes), and on a refusal it exits (status 4) with one line
  that names the reason, writes no endpoint, and `helm web` does not restart
  it. Every call then runs cold in silence, as on a host with no resident,
  and `helm hooks resident --status` says why.

Measured on the agents box on 2026-09-30, argv-guard through `bin/helm-hook`,
30 calls per arm, arms interleaved, in six rounds at one-minute load 14-36 on
the 8-CPU agents slice. CPU is everything the call costs: the wrapper,
`timeout`, the client or the cold interpreter, and the resident with its
forked children. The last two rounds, at load 15-18, are the quietest box:

| arm | CPU per call | wall p50 / p90 |
|---|---|---|
| cold (`HELM_HOOK_RESIDENT=off`), load 15-18 | 159-200 ms | 153-213 / 266-295 ms |
| resident, load 15-18 | 19-20 ms (client 6-7, resident 13) | 16-17 / 19-22 ms |
| cold, all rounds (load 14-36) | 159-273 ms | 153-440 / 266-693 ms |
| resident, all rounds | 19-32 ms (client 6-10, resident 13-22) | 16-46 / 19-122 ms |

A serving resident also polls its tree once a second (the digest walk), about
0.7 ms of CPU per call at the rate these rounds ran.

After the cure round for the fresh read (the resident's proof, the sender
check, the client's state read before it connects, the first-import record),
measured the same way on the same box on 2026-09-30, 20 calls per arm in two
interleaved rounds at one-minute load about 11: cold 106-127 ms of CPU per
call (wall p50 105-131 ms), resident 19-22 ms (client 7-9 ms, resident
12-13 ms; wall p50 16-27 ms). The checks add no measurable cost per call.

Most of the resident's share is the fork and the copy-on-write faults of its
child (about 4 ms for a fork and exit of the 40 MB parent, and about 8 ms of
faults while the hook runs, against 2.3 ms for the same hook in-process). That
is the price of a fresh process state per call.

**Only argv-guard, deliberately.** The PostToolUse composite (`helm hooks run
PostToolUse --installed`) runs on every tool call too, but the delivery and
seat-identity code under it reads this process's ancestry and session
(`seats_common`, `pull_delivery`, `beacons`), which a forked child would
answer with the resident's. It stays cold until those reads take the caller's
pid. `hookres.SERVED` and the test in `bin/helm-hook` name the served argv,
and tests/test_hookres.py holds the two to each other.

### What is NOT cured

`chat deliver` measures **~0.35 s of CPU** with its imports warm, and that is
its own work — rooms, roster, cursors — not startup. That cost is not what
blew the budget in production, but it is real and unimproved, and a box under
enough load will still spend it.

The delivery store itself is the standing debt. `chat.list_rooms` getdents the
room directory, and that directory holds **90,987 entries of which 445 are
rooms** — measured **238-302 ms per call under load**. Since task/3848 a
delivery pass no longer pays it on every tool call: it reads the listing the
last pass made (`seats_roomscan.room_names`), which is made again only when a
room log is created (`chat.rooms_generation`) or the listing is 30 s old, and
a beacon waiter whose rooms have not moved since it proved them read skips the
pass entirely (`seats_roomscan.QuietRooms`). `chat.state_path` and
`STATE_FAMILIES` exist precisely to move that exhaust into per-family
subdirectories and only the `deleg` family has moved; the cursors and their
locks, which are ~97% of the entries, have not, so every listing that does run
— that shared one, gc, rename, restore — still reads them all.

The proxywatch delivery-state lock is now waited on with a deadline (below),
which stops a queue from consuming the budget — it does not make the queue
shorter. The lock is fleet-wide and taken by every seat on every tool call;
narrowing what it covers, rather than bounding the wait for it, is the fix that
removes the contention instead of surviving it.


**THREE rows are GATES, not lanes: `Stop` (stop-guard), `PreToolUse` (argv-guard) and `PreToolUse` (suite-guard, the external one).** **The ladder below is a SHIPPED FILE, `bin/helm-hook`, and the installed command is the short invocation of it: `<abs>/bin/helm-hook <gate|lane|posttool> <name> <event> <N> <consequence> <abs>/bin/helm …`.** Claude Code echoes a hook's WHOLE command string whenever that hook blocks or errors, so an inline rc-case ladder is owner-facing output on every blocked stop — measured at 2,039 characters for the Stop gate, printed in front of the one sentence the owner needed, and the owner asked twice why the stop hook was so long. The arms, their exit codes and their sentences are UNCHANGED; only their address moved. Five operands always precede the child, because `hooks._executed` steps over exactly that many to find it and a phrase marker claims an entry only from the child's FIRST ARGUMENT — a variable-width head would make helm read every hook it installed as foreign and append a duplicate beside it. **TWO ABSENCES, AND HELM CAN ONLY SPEAK FOR ONE OF THEM.** A missing CHILD is unchanged and still speaks in helm's own voice: `timeout` returns 127, the `127)` arm runs, and the wrapper prints THE GUARD IS MISSING — naming the path and `helm hooks install` — on stderr AND as the exit-0 JSON the harness surfaces. That is the deleted-lane-room case, and it is pinned by an executed arm on both channels. A missing WRAPPER is the new state and helm has no voice in it at all: the shell exits 127 before any line of `bin/helm-hook` runs, so the hook still fails OPEN (non-zero and not 2) but in the harness's words, on neither of helm's channels, and no code inside that process ever runs to say otherwise. **Only a reader that MEASURES the file can report it**, so the ones that matter do, from ONE function (`hooks._wrapper_state`, rendered by `hooks.wrapper_gone_message`): `helm hooks status` prints it for credential homes and for seat config dirs; `doctor`'s inject-coverage rung prints it and no longer counts such a home as covered; `doctor`'s guard-contract rung prints it per config. So a hook that produced rc 127 and no output at all is the WRAPPER missing, not the guard — ask `helm doctor` or `helm hooks status`, never the hook. The nine other hooks never block either — but they no longer do it in SILENCE. They used to be generated with a bare `|| true`, which is the fail-open law written as an idiom: right about the law, and it also rewrote rc 124 and rc 127 to success. A `timeout` KILL prints NOTHING, so a lane hook that never ran was indistinguishable from one that ran clean — the identical failure this section records for the GATES on 2026-08-04, whose cure stopped at the three gates. On 2026-08-27 the fleet spent hours chasing seats that were simply unreachable, and a silently-dead `chat join` is exactly that: no mentions, no DMs, no wake, indistinguishable from idle. Every advisory hook runs the `lane` kind, whose ladder is `timeout N helm …; rc=$?; case "$rc" in 0) ;; 124) <alarm> ;; 127) <alarm> ;; *) <alarm> ;; esac; exit 0` — the gate ladder minus the `2) exit 2` arm, because an advisory spec that propagated 2 would arm every lane hook into a blocker (`hookrun._stronger` holds the same line in-process). **The exit code never moves: it is 0 on every path.** Each alarm names WHAT THE SEAT LOST rather than which verb failed — the sentences live in `hooks._ADVISORY_LOST`, one per spec, and an advisory spec added without one fails its arm rather than shipping a generic line. A gate cannot be: it returns **rc 2 to BLOCK**, and `|| true` rewrites that to 0 — the harness then lets the agent proceed anyway. Measured 2026-07-26, the stop-guard was returning 2 with **72 undelivered messages** while every turn ended cleanly, and the owner's report was exactly right: *"ive never once seen them actually fire to stop you from ending a turn"*. A gate runs the `gate` kind, whose ladder is `timeout N helm …; rc=$?; case "$rc" in 2) exit 2 ;; 0) ;; 124) <alarm "TIMED OUT at Ns"> ;; 127) <alarm "THE GUARD IS MISSING — nothing executable at <path> … repair with: helm hooks install"> ;; *) <alarm "THE GUARD FAILED rc=$rc"> ;; esac; exit 0`, where every alarm ends "— this <stop|tool call> is UNCHECKED" (it read "ALLOWED and UNCHECKED" until the budget section below trimmed it; UNCHECKED is the half that must survive any trim) — it propagates the refusal, and every other outcome still exits 0 so a helm crash fails open. **The arm set is exhaustive on purpose.** The rc-124 leg exists because a `timeout` kill prints NOTHING, so an unchecked stop was indistinguishable from a clean allow. The rc-127 leg exists because "a crash at least leaves a traceback" was FALSE for the case that mattered: a hook whose helm path no longer exists exits **127**, which was neither 2 nor 124 and so fell straight through to `exit 0`. On 2026-08-04 eight settings files named a deleted lane room's `bin/helm`, both gates among them, and four credential homes ran unguarded in silence (see **helm_bin and the shared checkout** below). A guard that could not run is a strictly worse state than one that timed out, and only the timeout was reported. The `*)` default means the next unhandled code is loud by construction rather than after the next outage. Fail-open is right for a lane and fatal for a gate. The hook entry also gives Claude's outer runner `N+5` seconds: without that explicit grace its default five-second deadline can kill the shell before rc 124 emits the alarm, recreating an allowed unchecked stop outside Helm's wrapper. This does not widen the checked verdict budget; the inner `timeout N` remains authoritative.

**AN ALARM IS TWO CHANNELS, AND STDERR IS NOT THE ONE THAT COUNTS.** Every arm above exits 0 by the fail-open law, and the harness contract is explicit about what that means: *"Stderr from a hook that exits 0 goes to the debug log only, never the transcript, and Claude never sees it."* So from 2026-08-04 until this was fixed, all three warnings — including the ones written to cure that very outage — were announced to nobody. @codex found it on `lane/helm-bin-derives-from-file`, and the arms that were supposed to prove visibility could not: they captured the SUBPROCESS's stderr, which proves the text was written, not that anyone can read it. `_gate_alarm` now emits the same sentence on **both** channels — stderr for the debug log, and JSON on stdout, which is *"only processed on exit 0"* and is therefore available exactly when a gate fails open. `systemMessage` reaches the OWNER; `hookSpecificOutput.additionalContext` reaches CLAUDE, and both gate events carry it (Stop renders at the end of the turn, PreToolUse next to the tool result). **The exit code does not move** — that is the whole point: the guard still fails open, it just stops doing it in silence. Two rules for anyone editing this: the sentence is written ONCE and rendered to both channels (two hand-kept copies is how a hardcoded "this stop" ended up in front of a PreToolUse reader), and rc 0 / rc 2 emit NOTHING on stdout — a pass must stay quiet, and on exit 2 the harness IGNORES stdout and reads stderr instead, so emitting there would drop the refusal's reason entirely.

**A REFUSAL IS COUNTED, AND THE REFUSAL DOES NOT WAIT FOR THE COUNT.** On the `2)` arm the gate kind starts `<abs>/bin/helm friction record <name> --reason <event>` — the helm beside the wrapper, so the external gate is counted too — DETACHED, with stdin, stdout and stderr on `/dev/null`, and exits 2 at once. One line reaches the friction ledger carrying the gate's name and the event, never the payload, which the wrapper does not read. It is detached because a gate that overran the harness deadline while bookkeeping would be read as a hook error, and a hook error ALLOWS. A `dispatch-*` entry is not counted here: the in-process dispatcher counts its own refusal under the handler's name. `helm friction` reads the ledger.

**helm_bin and the shared checkout.** The absolute path baked into every generated hook is the **shared checkout's** `bin/helm`, resolved through `work._lanes.find_root` (git's `--git-common-dir`), never `dirname(dirname(__file__))`. A seat running a hook-touching verb from its lane room would otherwise write that room's path into every credential home and every seat config — `_merge_event` is the one place that reaches all of them — and a lane room is deleted when its lane lands. Two independent rails hold this: `hooks.helm_bin()` **raises** rather than return a room path, and `hooks.refuse_lane_room_commands()` runs on the merged candidate **immediately before any write**, so a caller that composes its own command text (`record.py` does) still cannot persist one. The write guard refuses rather than repairs — silently rewriting an entry the merge law does not own would be a clobber — and it names the file and the offending path.

The two continuity entries OMIT a matcher on purpose: they must fire on EVERY
compaction and EVERY session end, never gated to one trigger. `helm handoff
check --hook-json` is FAIL-OPEN TOTAL — rc 0 always, silent when the contract
is satisfied, and it captures `_global/now.md` on the same trigger.

**Seats are part of the full estate.** A full `install` (no `--home` filter)
wires `hooks.SEAT_SPECS`, the same complete contract as `hooks.SPECS`, into
every multimodel seat's isolated `CLAUDE_CONFIG_DIR`
(`<helm_home>/_global/seats/<family>/claude`). That includes per-turn inject,
the delivery and gate hooks, both `handoff check` producers, and resume-turn.
The shared contract matters compositionally: resume-turn consumes the seat's
own handoff artifact, so installing that consumer without PreCompact and
SessionEnd producers leaves recovery universally armed but unfed. A launched
codex/kimi/… seat receives `@<family>` and owner posts under its family name
(`seat launch` exports `HELM_CHAT_NAME=<family>`), cannot idle past NEW inbox
rows, and writes the continuity artifact its next compacted window reads. See
the `stop-guard` contract in VERBS for the inbox bound: it blocks once per
pending-fingerprint, so a re-stop on the SAME rows passes and a seat CAN idle
past a static inbox it has already been shown once. `helm hooks status` prints
a `seats (full hook contract)` block with inject and paired-handoff columns
plus `seat hooks: N of M seats`; `helm hooks install` reports `N of M seats
covered (full hook contract)`.

Same laws for every entry: MERGE-preserving (existing hooks — `helm record`'s
PostToolUse/PostToolUseFailure legs, a seat's foreign hooks — and settings keys
are never clobbered), idempotent (re-install reports `ok`), on configs.py's
safety rails. Every mutation now uses **content-revision compare-and-swap**:
read exact bytes + opaque revision, re-derive the full domain merge from that
snapshot, write only with `expected_revision`, then re-read and verify the exact
committed revision. A pre-commit conflict or a foreign write immediately after
Helm's commit is preserved and retried from its newer bytes, at most three
attempts. Three conflicts refuse loudly; non-conflict storage/shape failures do
not retry. Backups remain recovery artifacts, but Helm never unconditionally
restores one over a revision another writer may have produced. Semantic no-ops
preserve the file's exact formatting.

`--home NAME` narrows to one home (and skips seats); `helm doctor` reports
`inject coverage: N of M claude homes` so a gap in HELM'S OWN ESTATE can't hide
— and that is the whole of the claim. Orca's `orca agent hooks on|off|status`
edits the same `settings.json` with overlapping UserPromptSubmit/Stop/PostToolUse
entries. A Helm-only advisory lock cannot bind that external writer, so locking
is not the correctness mechanism; exact-revision CAS is. A later intentional
Orca edit can still change the estate after Helm returns, so a green coverage
number is a read of the current file, not a perpetual ownership claim.

`helm hooks status` reports currency as well as presence. Each readable home is
compared with the same canonical hook-spec merge used by install and verify;
installed entries that would be rewritten print `OUT-OF-DATE` instead of also
being labeled missing, while an unreadable or broken-link settings file keeps
the established missing-contract alarm and also prints `currency UNKNOWN`, never
clean. A genuinely absent settings file is known-empty rather than unreadable.
This comparison covers resolved hook specs only —
unrelated estate defaults and lane-room repair
are not hook-rendering drift. The explicitly inject-only coverage count falls
only when inject itself is out of date; drift in another guard remains visible
without changing that narrower number. Status also adds per-home
`deliver`/`join`/`stop`/`handoff`/`resume` columns plus the seat block.

**Scope de-duplication is cross-file.** Claude loads user and project settings
together, so an owned hook in both `<CLAUDE_CONFIG_DIR>/settings.json` and
`<project>/.claude/settings.json` fires twice even though each file is internally
clean. `hooks status`, `hooks install`, and `doctor` scan the current checkout and
every identity-verified live seat's checkout for this pair. A confirmed pair is
printed as `cross-scope DUPLICATE`; unreadable roster/settings evidence is
`UNKNOWN`, never clean. Helm reports but does not delete either entry: a project
hook may be deliberate, and choosing which authored scope to remove is a judgment
rather than an installer merge. The scan is bounded by current/live projects; it
does not walk the historical project registry.

The generated command pipes the hook's FULL JSON to `helm inject --hook-json`,
which extracts the prompt, derives `--project` from the hook's `cwd` (longest
registry-path prefix; global-only when no project claims it), and stamps the
`session_id` onto the fire-ledger row. New rows are schema v2 and carry an exact
UTF-8 sample: normal text-mode stdout bytes (including its terminal newline when
non-empty) plus each lane payload. Their point-in-time context records only
authoritative evidence (`session_id`, absolute hook `cwd`, runtime-stamped
harness, and an explicitly set harness config-home environment variable). An absent config-home variable stays unknown; it is never
turned into `~/.claude`. Historical v1 rows remain readable as approximate Unicode
character counts (`len(str)`, with join separators absent), and are never relabelled
as exact bytes.

The fire ledger uses a durable per-attempt spool. A stable, data-free
`inject-ledger.jsonl.lock` is never replaced or unlinked. Before Helm constructs,
mutates, or returns an injection, it holds the shared lock and durably writes
`inject-ledger.jsonl.queue/<attempt>.intent`; inability to open/lock or sync that
admission suppresses Helm output while the hook still returns rc 0. Seen-state,
reflex, coinage, council, and greeting latch mutations are staged while that
shared lock is held; none is applied unless the completed row first becomes an
immutable, file-and-directory-synced `<attempt>.ready` envelope. Only then is the
shared lock released and the staged plan applied. Exclusive recovery renames READY to a plain
`inflight.<generation>` file, appends with `O_APPEND` plus a write loop and fsync,
then plants a durable per-attempt COMMITTED tombstone before cleanup. The
tombstone stays until every other artifact for that attempt is durably gone, so
failed cleanup and later rotations cannot replay an already appended row.
Intent-only attempts become generation-bound UNKNOWN witnesses;
append/flush/close failures retain inflight evidence for a later writer, and an
inflight attempt never ages into a complete census. Current and `.1` carry ignored
protocol/generation headers, rotation still uses the 5MB threshold with one `.1`
and no `.2`, and legacy raw rows remain dual-format input. The reader captures
both generation fds/sizes/ids and the complete queue census under one exclusive
lock boundary, then preads exactly those bytes. Completeness is tri-state: exact,
known incomplete, or unavailable; missing protocol markers, migration history,
malformed/pending/live-generation evidence never become false zeroes. The outer
hook keeps `timeout` and its exit-0 contract, so missing or failing Helm still injects
nothing instead of blocking.

`helm hooks install --harness codex` reports honestly: the codex notify-hook
recipe below is not yet mechanical, so codex stays hand-wired for now.

### Project scope: `helm hooks install --project DIR`

Not every operator wants the physics machine-wide. `--project` installs the
same full spec set into `DIR/.claude/settings.local.json` instead of any
claude home: sessions launched in that project get inject, the delivery lane,
the stop-guard and the handoff contract; every other session on the machine
stays hook-free. Same CAS pipeline, same merge-preservation laws, same beacon
permits — with one deliberate delta: the scalar estate defaults are **not**
seeded, because a project file is neither a home nor a seat and helm does not
own a project's other settings.

`settings.local.json`, deliberately: the generated commands carry this
machine's absolute helm path, so the file is per-machine and must never ride
a commit into someone else's checkout. Add `.claude/settings.local.json` to
the project's `.gitignore` — the installer reminds you, and never edits a
repo's ignore file itself. Only *new* sessions pick the hooks up (the harness
snapshots hook config at session start). `helm hooks status` reports
project-scoped wiring in its project-scopes lines, and flags a spec loaded at
both project and active-home scope as a cross-scope duplicate.

## The contract

`helm hooks status` reports SA initial-context coverage on its OWN line (`SA initial-context hook INSTALLED for N of M seats (delivery unobserved)`) and in an `sa-ctx` column, separate from `inject`. THE TWO ARE DIFFERENT POPULATIONS: `inject` covers a TLA's per-turn physics and says nothing about the subagents that seat spawns. The figure counts CONFIG, NOT DELIVERY — it is not a claim that every subagent receives physics, because the harness limits above are not observable from a settings file. It IS part of the full-contract total, since a seat missing this hook is not fully covered.

```console
$ echo "<the user's prompt text>" | helm inject [--project <name>]
PREMISE naming-extremes: metaphors live at the extremes only ...
TERM drain: routing raw memory intake to typed homes ...
REFLEX: checkpoint the green slice
```

stdout is the injection payload (may be empty). `--json` returns
`{"pinned": [...], "jit": [...], "reflex": [...]}` for hooks that want
structure. `--hook-json` reads the harness hook's full JSON on stdin instead
(`prompt` / `cwd` / `session_id`, unknown keys tolerated; malformed JSON
injects nothing, rc 0). Without `--hook-json`, resolve `--project` from your
registry name (`helm projects`) yourself.

## Manual recipes (the fallback appendix)

### Claude Code

What `helm hooks install` writes, if you'd rather wire it by hand in one scope
(user **or** project `settings.json`, never both for the same event):

```json
{
  "hooks": {
    "UserPromptSubmit": [{
      "hooks": [{
        "type": "command",
        "command": "timeout 10 /path/to/helm/bin/helm inject --hook-json || true"
      }]
    }]
  }
}
```

Hook stdout becomes `additionalContext` automatically. The prompt-only
variant (`jq -r .prompt | helm inject --project myproject`) still works but
loses the cwd-derived project scope and the session on the ledger.

### Codex

`UserPromptSubmit`-equivalent notify hook: run `helm inject` with the prompt
text on stdin; return the output via `hookSpecificOutput.additionalContext`.
Keep it sparse — codex renders injected context as a visible developer
message.

### OpenCode

An `@opencode-ai/plugin` with `experimental.chat.system.transform`: shell out
to `helm inject`, append the lines to `output.system`. Feature-check the
experimental namespace and fail open (empty) if it moved.

### Hermes

A `pre_llm_call` plugin hook returning `{'context': <helm inject output>}` —
appended to the current turn, preserving the cached system-prefix.

## Rules of the road

- **Fail open.** A hook that cannot run helm must inject nothing, never block
  the turn. The installer generates the rc-case wrapper (a `timeout`
  guard plus an arm per outcome, always `exit 0`); a hand-wired hook
  may still use a bare `|| true`, which never blocks but also never
  reports a helm that could not run.
- **Budget is helm's job.** The pinned lane is byte-capped and JIT is capped
  at 4 entries; hooks should not add their own truncation.
- **Per-project scoping.** `--hook-json` derives it from the turn's cwd; in
  manual wiring pass `--project` when the session's cwd maps to a registry
  project. Global entries fire everywhere, project entries only where they
  belong.
