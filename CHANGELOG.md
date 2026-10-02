# Changelog

## Unreleased
Lanes add change notes as `changes/<lane>.md`, never here; a release folds them.

## 0.3.3 — 2026-10-02

Changes since 0.3.2.

This release is about getting more out of plain Claude Code. A lead seat now
runs a narrower working window and a lean settings profile that cuts what
every request re-sends, a lead that helm did not spawn (started by hand or
adopted from Orca) gets the same lead posture, and a compaction hands the
seat back its working set instead of leaving it to find its paths and verbs
again. Seats are woken far less often for rows that need nothing from them.
The landing pipeline holds itself to its evidence: a source-clean hold names
the test run behind it, auto-land composes one car per lane and releases the
leases of the lanes it lands, and a landed task closes itself. The fleet's
state now reaches the owner's phone through one reading, the office weather,
which pages only when it turns stormy. Some refusals and defaults changed:
read "Breaking or behaviour changes" at the end before you upgrade.

### Leaner Claude Code sessions

- **A lead seat runs a 240k working window.** `helm launch` on a native lead
  sets `CLAUDE_CODE_MAX_CONTEXT_TOKENS` and `CLAUDE_CODE_AUTO_COMPACT_WINDOW`
  to 240,000, and the autocompact gauge divides the lead's context by the same
  number. A lead is known by its spawn register or its role declaration,
  never by its name, so it stays a lead across a relaunch or an exact resume;
  a worker is unchanged. A lead on a proxied family keeps the window its
  family is taught. Why 240k and not less: at 160k Claude Code compacts near
  121k, while a lead's floor after a compaction is about 95k per request, so
  it compacted again every 10 to 15 calls.
- **Every native Claude home takes a lean settings profile.** `helm homes
  provision --apply`, and each `helm launch` that wires a home, write it into
  the default home and every credential home: `deniedMcpServers` from this
  host's `lead-denied-mcp-servers` local name (the servers are the operator's
  own, so no shipped file names them), `skillListingBudgetFraction` 0.0025,
  plugin-dev turned off in `enabledPlugins`, and a deny of the three Artifact
  tools. The pass only adds: it fills a key the home does not hold, merges
  `deniedMcpServers` by server name, and never replaces a value the home
  holds, so an operator who turns plugin-dev back on or sets their own budget
  keeps it. Each deny is recorded, so `helm seat retire-deny` can find it, and
  a pass that changes nothing writes nothing. Measured on a post-compaction
  floor, per request: the Artifact deny saves about 10.5k tokens, the three
  MCP servers about 3k, plugin-dev 1.3k, and the skill listing re-sent on
  start and resume falls from about 10.4k tokens to 3.5k on a 1M window. A
  lead keeps the tools it uses: the local seats' lean MCP floor and their deny
  of the delegation tools are not applied.
- **A lead helm did not spawn can be a lead.** `helm launch --seat S --role
  lead`, and `helm seat resume S --role lead` for a seat Orca adopted, write a
  role declaration that helm reads when no spawn register names the seat.
  Before, every hand-started or adopted lead read as a worker, so the lead
  window, the lean profile and the lead memory class never reached it. An
  adopted lead still resumes, recovers from a compaction and gets its reboot
  sweep on the adoption path. A spawned seat takes its role from its spawn
  register only, and a `--role` that disagrees with it is refused.
- **Every agent starts at high effort.** Every Claude home helm prepares,
  provisions or launches on sets high effort for every model, and helm no
  longer writes ultracode. An Opus `xhigh` or an `ultracode: true` in a home's
  `settings.json` is drift: `helm doctor` names it, and the next `helm homes
  provision --apply` or launch changes `xhigh` to high and removes ultracode,
  keeps a backup beside the file and says what it changed. A `low` or `medium`
  the home sets is kept. `--role lead` now adds only `HELM_SEAT_ROLE=lead`, on
  every spawn and resume path; an exact resume of a seat that ran with
  ultracode does not restore it and says so.
- **A compaction hands the seat back its working set.** `helm record` keeps
  one row per main-thread tool call for each session: the tool, whether it
  worked, its paths, the verb spelling of helm, git, gh, fab, Orca and cv
  (flag names only), task and dispatch ids, and background task ids. It never
  keeps command text, flag values, file contents or output, and it never
  records an env or credential-shaped path (`*.env`, `.env*`,
  `credentials*`, `*secret*`, `*.pem`, `*.key`, `~/.aws`). A new SessionStart
  hook for `compact`, `helm now show --hook-json`, returns at most 7,000
  characters, newest first: the paths that still exist, the spellings that
  worked, the spellings that failed with a usage error marked DON'T beside the
  one that worked, recent ids, background tasks and scratch directories. A
  subagent's compaction gets nothing. Measured before: for about 50 calls after
  a compaction a seat slipped 9.5 times per 100 calls against 7.0 otherwise,
  with wrong verbs and paths about four times as common. `HELM_WORKING_SET=0`
  turns it off; `helm now show --working-set` prints the set on demand.
- **The handoff reminder no longer steers the compaction summary.** The
  PreCompact reminder printed to stdout, which Claude Code gives the summarizer
  as custom instructions; it became a "next step" in 27 of 43 measured
  summaries. It now goes to the session's reminder file and reaches the seat
  once, after the compaction, through the working-set hook or, on a seat whose
  settings predate that hook, through `helm inject --hook-json` on its next
  prompt. `HELM_WORKING_SET=0` does not drop it.
- **Proxied families compact at the right point.** The Grok and the proxied
  Opus 4.6 window pins subtracted Claude Code's own 20,000-token reserve
  twice; Grok now compacts at 173,600 tokens instead of 147,200 and Opus 4.6 at
  122,400 instead of 102,400. gpt-oss counted the reserve twice too and
  blocked right after boot; its pin goes from 62,000 to 66,000 and it launches
  without the tools it never calls. Each request still stays at least 10,000
  tokens under the route's published window.
- **A Cursor-family seat keeps one Cursor conversation per Claude Code
  session.**
  The bridge replayed the whole history into a new Cursor conversation on
  every turn, and Cursor then counted it at two to five times its size on about
  half the turns. A turn that only adds a reply and new text now continues
  Cursor's own conversation; a compaction, `/clear`, edit or interrupted turn
  still replays, and the bridge log says why. With Cursor's count near the
  estimate again, the seat is taught its full 225k window instead of 110k, so
  Claude Code compacts at about 164k instead of 73k. The seat launches without
  the tools it never calls and starts with about half its window free instead
  of a quarter, and every turn names its tools, Monitor included, in Cursor's
  own tool instructions.
- **A seat is woken only for rows it has to act on.** The stop guard leaves out
  rows that need no act (a stale-sweep digest that keeps everything, or a row
  for other seats only), says how many it left out and delivers them at the
  next tool boundary (`HELM_STOP_GUARD_NOACT=0` turns this off). A row
  @-addressed to other seats no longer wakes the rest of its room, meld rooms
  included; rows naming this seat, replies, DMs, `@all`, owner, deadline and
  failure rows still wake. Measured before: in one night one lead was woken
  235 times, 50 of them by stop-guard blocks, and each wake re-read a median
  393k-token context.
- **New `helm chat wait --follow --once`**: a one-shot beacon that wakes only
  on a mention and exits after its first ring, so a quiet seat pays no wake
  where a 30-minute Monitor pays one at every expiry. The census reads the gap
  before it is re-armed as WAKING, not DEAF.
- The doorbell rings each row once per seat. A row a ring led with is not led
  with again by the backstop or a re-armed waiter, and a row the tool-boundary
  hook already showed is not rung. The row stays owed until the seat reads or
  acks it.
- Only a seat the owner talks to gets the full operator guidance: a seat
  settled as a non-lead team member gets the operator line and the profile's
  new `longtail_guidance`. Measured before: one reviewer seat's transcript held
  the full guidance 52 times. `helm whoami` shows the new field.
- **A lead's context holds only its own project's work.** `helm dispatch send`
  and `add` refuse a review or build row to a long-lived lead whose home
  project is not the lane's project, and print a `corrected:` line that sends
  the work to your own subagent or Workflow run instead. A hand-back to the
  lead that dispatched the build is admitted.

### Landing work

- **Auto-land releases the worktree lease of each lane it lands**, whoever
  holds it, once that lane's work is on trunk. A lane with new work beyond the
  land keeps its lease, and a branch holding work no other ref has is kept. The
  LAND line names the leases released and each refused release with its
  reason; a refused release does not stop the land.
- **Compose admits one car per lane**: the hold whose tip is the branch its row
  bound. An older hold on that branch is superseded and is not merged, and a
  row with no bound branch is not composed. A review dispatched with `--ref
  <sha>` now binds the lane branch when exactly one lane branch points at that
  sha, and a review that binds no branch warns when it is written, naming
  `--ref lane/<lane>`.
- **Each car carries its own task.** At compose, each car's row and lane are
  checked against its chain. A car whose task conflicts or cannot be read is
  task UNKNOWN: it cannot close its chain's findings or sibling rows at
  auto-land or at a hand land, and the land report names what stays open.
  Train blame's ejection tells the author and never comments on the task of a
  disputed car. Auto-land saves the facts it admitted before it pushes, and
  reads each car's task again before it closes anything, so a killed tick
  never resumes from old facts.
- **A landed task closes itself** when its lane carried the whole ask (`helm
  work claim --task task/N --whole`, or `--whole` on the first dispatch);
  otherwise the land asks in the task's room whether the ask is done. `helm
  task close-candidates` lists a task as soon as its whole ask lands. A land,
  by auto-land or by hand, also closes the other open rows of the chain it
  carried and names the ones it could not close.
- **A review's findings are sub-tasks of the task under review.** A finding
  closes when a clean read past its fix is held, when the fix lands, or when
  the FIX that named it is retracted as source-clean.
- `--task` on `helm work claim` and `helm dispatch send` takes exactly one of
  `--whole` (the lane closes the ask) or the new `--part` (the lane lands a
  piece under the story).
- Auto-land no longer asks for a web restart after a land that changes the
  console: the console restarts itself on new code. A minute after the land,
  auto-land checks it, and if it still runs the old code it posts one line
  naming the old and new code digests; it restarts nothing. A land that
  changes the chat node, proxywatch, the hooks or a systemd unit still asks
  for a restart.
- The fold sweep after a push judges this train's cars by their exact ids. A
  source-clean hold that belongs to another train is listed, and its refusal
  is reported instead of stopping this train; an own-car refusal, a failed
  close or an unreadable sweep still stops before the LAND number. An indented
  `REFUSED` or `FAILED` fold row stops auto-land instead of reading as a fold.
- **A holder's approval is fixed at the hold.** The holder's exact-session
  runtime, model and approval policy are recorded with a source-clean hold,
  so a later proxy cooldown cannot revoke it and a holder without that proof
  cannot borrow a later one. Compose, final admission and closure check the
  recorded proof against the owner's current policy and fail closed when the
  policy cannot be read; an owner demotion still stops a land. Once a train
  has pushed a car, its closure may rest on the admission recorded at that
  push.
- New `helm dispatch attach-task <row> --task task/N` records that a chain
  begun with no task now serves task N, so later rounds file their findings
  under it. It rewrites no history, and it refuses a chain that already has a
  task, a closed or unknown task, and a caller who is neither the chain's
  author nor the integrator.
- A new review chain must resolve a task; neither a task-shaped lane name nor
  `--force` stands in for one. A superseding dispatch checks the new lane's
  own task against the chain's and refuses a conflict before it is recorded.
- `helm lr close --reason out-of-scope` refuses a row whose own tip is on
  trunk, and names `--reason landed`; when helm cannot tell whether the tip
  shipped, it refuses that too. Before, shipped work with no successor row
  could be closed as unwanted.
- The stale sweep no longer calls an owed review row "no visible progress"
  when a standing CONCUR on its chain answers it; it proposes
  `qualified-closure` or `unknown-landing` instead.
- **`helm work gc` clears more rooms safely.** It retires a room whose work
  landed under other commits (a lane that merged trunk back in, or a rebuild
  that landed by train), printing each commit's evidence, and keeps a room with
  a commit it cannot prove. A clean, unlanded lane nobody has touched for three
  days, whose removal loses nothing its branch does not hold, is parked: the
  checkout goes, the branch stays, and `helm work claim <lane>` re-opens it.
  A rescue of a branch that is pushed, dispatched or held commits to a side ref
  (`refs/helm-rescue/<lane>/<time>`) and leaves the branch where it was, and a
  ledger it cannot read counts as protected.
- `helm worktree gc --apply` refreshes the trunk ref first and refuses when the
  fetch fails, and the hourly `helm-gc.service` now runs it after `helm work
  gc` (re-run `helm gc --install-timer` to pick that up).
- The remote relay finishes its cleanup after a crash. A verdict closes its
  row before the cleanup runs, so a later tick now re-runs the branch deletion
  for a closed row whose session has none, at the exact shas and with the same
  lease. A failed record is journalled and the cleanup still runs, and a
  refused lease keeps the checkout as evidence.

### Test gates

- **The test-count cross-check is one-sided.** A collected count below the
  diff's count still stops the land and names the shortfall. A surplus lands,
  and the land note names it: a base class that gains a test method hands it
  to every subclass, which the diff's count never sees.
- A land gate picks the host that finishes first counting the work already
  queued there: start delay from `fab capacity --json` plus the host's measured
  median. A busy measured host loses to an idle unmeasured one, an idle
  measured host still wins, and an unreadable capacity falls back to medians
  and says UNKNOWN.
- `helm train auto --resume` after a stop at GATING launches a fresh gate of
  the same head instead of waiting out the timeout on a job that already
  finished red, also in a blame room. A second flake is recorded like the
  first, and a red that could not be read for blame is read again.
- Tests that depended on the build host's speed, clock granularity or Python
  patch level now test their contract, so a whole-suite gate gives the same
  answer on every host. No product code changed.

### Review and melds

- **A source-clean hold names the run behind it.** Its reason must carry a
  passing `Ran N` line or the fab log of a run on exactly the held tip (any
  short sha of 7 or more hex characters in the job name). A hold may rest on
  the holder's own standing CONCUR or its own fresh-context read at the tip.
  Measured before: seats held rows source-clean one to three minutes after
  dispatch with no run named, and about nine landed on those holds alone. A
  clean remote read carries no run, so it is now recorded as an ordinary hold.
- A source-clean hold is refused on a row whose recorded read found something
  (a FIX or SUPERSEDE verdict, or an advisory read at the held tip), naming
  that verdict and the re-send of the cured tip.
- **New `helm dispatch applied <parent> <tip> [--fab-receipt R]`** records an
  author's unchanged MELD-DIFF cure on the original review row as one event:
  the applied diff must have the posted diff's verbatim patch id, the tip's
  parent must be the reviewed tip, and a passing fab receipt must bind at the
  tip. No child row is made and the reviewer is not woken. A changed cure
  (whitespace included) is refused and prints the child send with `git
  range-diff` for the reviewer. The event keeps the reviewer's approval proof
  from the FIX, re-checked against the owner's current policy, and records
  nothing that lands: the whole-suite gate is still owed. A pair-meld YIELD
  naming that tip does not wake the reviewer while the proof stands.
- In a pair meld, a `[HOLD]` whose text says `HOLDING: <what you are doing>`
  keeps the round open: `recv` prints `MELD-HELD` with each holder and its
  work, and the peer's next YIELD continues the round with no new invite. A
  YIELD now @mentions each peer in the round, so it is owed work that the
  beacon counts and the stop guard waits on.
- **The review mode follows the reader's burn.** A RED family gets a
  review-only row, and an ORANGE, GREY, unknown or stale reading gets
  MELD-DIFF, never PATCH; GREEN and YELLOW keep the alternation. A mode stated
  in the brief (`REVIEW FIX MODE: <MODE>`) or with `--review-mode` wins over
  the alternation, two disagreeing stated modes are refused, and a burn-forced
  mode wins over both and records why.
- A FIX that counts uncured findings must name them with `--finding` or
  `--finding-carried`, or carry a patch or diff cure; the shared verdict writer
  enforces this for API callers too. `helm review done` forwards findings,
  carried findings and `--note` observations. An exact retry of a recorded
  count-only FIX returns the standing verdict, and a retry that differs is
  refused with the fields that changed.
- The remote relay files each uncured finding under its own first line as the
  task title (up to 256 characters, collisions made distinct), carries a
  still-open finding on a later FIX instead of filing it twice, and holds a
  cloud build from a chain with no task before its first launch.
- A native Claude seat that holds while its Workflow agents run now gets its
  approval proof when every model in that window is the seat's own family and
  admitted by the policy. Before, any subagent naming another model made the
  seat's model unknown, so the hold carried no proof and the fold refused it.
  A model of another family, an unknown model id or an unreadable window still
  leaves the hold without proof.
- A fresh-context read of a lane whose tip is a merge commit is now recorded:
  each merge is read against its first parent, so the read covers every file
  the lane changed. A Workflow run of several reader agents is one run and
  records one verdict.
- A review row to a seat the catalog marks proof-of-life or council-only is
  refused, and `--force` does not open it. A refusal of an Opus read of
  Opus-authored work names the bound the run failed, not "not an independent
  review".
- A corrected line for a `--supersedes` continuation drops `--task`,
  `--whole` and `--part`, since a continuation keeps its chain's task; a
  `--new-work` line keeps them.

### Owner pager and office weather

- **New `helm office weather`** (also `helm weather`) reads the whole floor as
  one word: `sunny` when every live seat is flowing or idle, `cloudy` when
  something is off and a steward has it, and `stormy` when the owner has to
  act. The idle-dispatch tick reads it every five minutes, and the same line
  heads `helm brief` and the console. **The weather pages the owner's phone
  only for a storm**: a change must hold four minutes to settle, the phone
  hears the settled edge into stormy, and it hears the all-clear once the
  floor has settled out of the storm, no sooner than 30 minutes after the
  storm's page. Every other change is shown, never pushed.
  `HELM_OFFICE_WEATHER=off` stops the pass and every push.
- The weather turns stormy on a P0 on the land path (a land-hook refusal, or
  a stopped auto-land train whose stop names the guard), on fleet-down, on a
  seat waiting on an owner decision, on a stuck seat no live steward has, on
  every family walled, on a fleet stall, on a chat node that does not answer,
  or on a stopped land train. Every other P0 stays on the console and in
  `#seats`.
- **Fleet-down has one definition and one page.** It is the steward seat
  unreachable, or a strict majority (at least two) of the eligible seats
  unreachable, over a complete and current census. An incomplete or stale
  census never storms: the weather shows a cloudy reason naming what it could
  not read. An episode keeps the seats it opened with and clears only when all
  of them are covered again on a fresh census. A storm the phone heard ends
  after two hours with one page that names the seats still down, so the next
  storm can page; an expiry page that failed is sent before anything else. The
  old fleet pager is retired, and its last open page is carried over once.
- Each seat's reachability change goes to its steward in `#seats`, never to
  the owner's phone. `#seats` carries each seat event once, with the steward of
  that component @mentioned, and credential walls, local-serving outages and
  budget crossings move there from the main room. Stewards are declared in
  local names (`cred-steward-seat`, `local-operator-seat`); a row with no
  declared steward says where to declare one.
- **New `helm seat mood`** shows each seat's mood (`flowing`, `grinding`,
  `stuck`, `blocked-on-owner`, `walled` or `idle`) with a 0 to 100 frustration
  score and its top reason, measured from the last hour's guard refusals,
  loops, busy minutes with no progress, credential walls and open decision
  cards. A refusal loop ends on progress or a two-minute gap. `helm seat mood
  set <word> [--rating 1-5] [--blocker TEXT] [--win TEXT]` is the seat's own
  check-in: a blocker is posted once to its steward, and a word that disagrees
  with the measurement is flagged. A seat turning grinding or stuck hears it
  once with the cheapest next move, `helm seat mood rank` ranks helm's own
  friction, and `helm chat seats` and the console show each seat's mood.
- **New `helm seat shout`** gives a seat three volumes: talk in its own room,
  `--speak-up` to its steward in `#seats`, and a shout that pushes its one
  `--need` line to the owner's phone with its Orca tab, where a reply comes
  back to the seat. Each seat has two shouts per 24 hours by default
  (`--dial`), and `--status` shows each seat's spent shouts and open question.
- **New `helm friction autopilot`**: a guard that keeps refusing gets one task
  row, filed and checked by helm. A cause that reaches 10 refusals or 3 seats
  in 24 hours gets one P1 row, P0 on 3 seats or on the land path, and later
  refusals add one counted comment a day. 48 hours after the row closes, helm
  measures the rate again and refiles when it has not fallen; a close whose
  reason starts `guard-correct:` retires the cause. The idle-dispatch tick runs
  it at most every 15 minutes, and it never pages the owner.
- helm tells an agent once when it picks smaller work while a bigger
  owner-asked item has nobody on it, or starts new work while its own sits
  unlanded, and records the reason it gives. Nothing is refused.
- **The dark-seat mover runs by itself.** The idle-dispatch tick moves a
  confirmed-dark seat's work (a walled family whose reset is over an hour
  away, a turn wall, or a pane gone for 30 minutes) to a live seat of the same
  role, else to its component's steward, and says each seat's decision once in
  `#seats`. Local name `dark-seat-mover` set to `off` makes it report only.
- `helm pressure-watch` says `HELM IS THE LOAD` when helm's own processes use
  at least half of agents.slice's CPU in a CPU stall, files or comments on one
  P0 row at most once in 30 minutes, and names short-lived CPU and births per
  second on its CPU line.
- **New `helm seat cubicles`** moves each seat's Orca tab to the pane its state
  names: a local-model seat to pane 3, a walled seat to pane 1, every other
  reading to pane 2, in the workspace named by local name `cubicle-floor`. The
  `seat resume --all` timer moves a tab after two passes agree; an UNKNOWN
  reading, the lead and a hand-launched seat never move, and no tab is
  renamed. Local name `cubicle-mover` set to `off` or `dry-run` stops or
  previews it.

### Seats and chat

- **The chat node proves in the background.** `helm chat node up` puts it in
  background.slice, holds its prover to one thread per six CPUs and runs one
  proof at a time; a signed send had cost about 25 CPU-seconds on up to 12
  cores. A drop-in you wrote beside helm's is named and never changed. Status
  rows from helm's own services are no longer signed, so they cost no proof;
  land announcements and verdict nudges are still signed.
- **`helm chat node status` tells a booting node from a hung one.** A node
  replays its history before it opens its API, which can take over an hour.
  Status reads BOOTING (with how long and its main thread's CPU) while the
  main thread works, HUNG only when that thread used no CPU over 30 seconds
  past the boot wait, and CANNOT TELL when helm cannot read the CPU. `helm
  doctor` and the console read the same verdict, so nothing tells a seat to
  restart a booting node, and `helm chat node up` and `down` refuse a BOOTING
  node unless `--force --reason "<why>"`.
- **New `helm chat node move --to ram|disk|PATH`** moves the chat node's data
  off tmpfs and back (tmpfs stays the default). It runs only with the node
  stopped, copies into a stage and verifies every file by sha256, rewrites and
  reloads an installed unit, and sets the old copy aside instead of deleting
  it; a step it cannot complete rolls the move back. `helm chat node status`
  prints the data directory, its file system and the store size.
- A seat's beacon no longer lists the chat directory every 2 seconds: a poll
  where no room log changed is skipped, and the tool-call hook reuses the last
  room list. An idle `helm chat wait --follow` runs a delivery pass only when
  a room file has bytes past its cursor (learned through inotify, or one stat
  per room).
- `helm gc` removes the steer latches of ended sessions and the cursors left on
  rooms `helm chat retire-rooms` took off the bus.
- `helm handoff check` reads each journal entry whole, up to 4 MiB, instead of
  its first 65,536 characters; past the cap the entry is UNKNOWN and named
  with its size. `check` also names a newer hollow handoff beside the older one
  that wins.
- **`helm slice-limits`** derives agents.slice's CPU and memory limits from the
  box, each minus a reserve for the owner; `--apply` writes one drop-in, sets
  the others aside, reloads and prints the rollback, and never lowers a limit
  without `--lower`. `--seats` gives each seat slice its own memory limit, a
  share of the fleet's by role (a lead a third, a worker a sixth), so a runaway
  worker is killed in its own slice. A per-seat memory high under 4G is left to
  the fleet line, since a lower one freezes seats. `helm doctor` names each
  limit that does not come from the derivation.
- `helm seat where` on a family miss prints the liveness row when that row is
  a wall or a refused census.
- An error while walking a seat's identity register reads UNKNOWN instead of
  "no register".

### Tasks

- **A task shows its sub-tasks.** `helm task show` lists them with "3 of 5
  done", and `helm task list` counts open stories next to open rows. A task
  with open sub-tasks closes only with `--open-children-stay`.
- **A task names its project.** `helm task add` takes it from the checkout you
  run it in or from `--project NAME`, and refuses a task with neither; `helm
  goal add` and `helm todos promote` check the same way, and the task mirror
  files each task under its session's project. `helm task rehome --plan FILE`
  gives older tasks a project (`--apply` writes it). A malformed project
  registry stops `helm task mirror` with exit 1 instead of filing every row
  under one project.
- A task added with no parent gets one line naming up to three open stories in
  its project that share uncommon title words, so it can be filed under one
  with `--continues`.
- **New `helm task health`**: one line for each project with its open stories
  and tasks, the tasks opened and closed today, and the 7-day net. `--json`
  gives the same numbers.
- **New `helm task handoff <id> --to SEAT [--note TEXT]`**: the holder gives
  its own row to another seat on the roster, recorded as one custody event.
- A task's conversation room is named for its project and number (such as
  `helm-42`); a room opened under the old name keeps it.
- **New `helm pile [--json]`**: one read-only screen of what a seat can build
  with: idle usable seats, credentials with room, tasks merged today whose row
  is still open, committed reviewer patches not yet used, and today's owner
  rulings. A source it cannot read prints `unknown: <why>`.

### Model families, credentials and quota

- `helm creds` reads each Claude account through a live copy of it. When no
  helm home holds one, it reads Orca's copy without changing it, and when no
  copy is live it shows Orca's own last reading; an account only Orca holds is
  listed. A home whose own copy is dead says so, and seats still do not start
  in it. The cloud relay reads credit the same way and accepts a home written
  as `~/...`.
- `helm cred sync-orca` takes the token endpoint's refusal of a home's refresh
  token as proof that its login is spent and plans the refill from Orca. `helm
  keepalive` records a 429 or 5xx answer as a failed pass, not a needed login.
- A colour the owner declares from a family's sheet shows on the next read,
  marked as a declaration and not as the reading, instead of NOT MEASURED until
  the watchdog's next pass.
- A family whose catalog says on-demand spend is off, with a basis, reads
  ORANGE on money when its billed pool runs high, so it stays dispatchable
  until the vendor itself refuses. Any other family still fails closed to RED.
- A proxy cooldown that a vendor quota refusal caused now reads as the
  vendor's wall, not helm's own cooldown, and `helm burn` no longer advises a
  restart for it. The repair fits the wall: a top-up or plan change for an
  empty balance, a wait for a spent usage window.
- A Claude seat on the baseload home is no longer told to route work away
  while the watcher can switch it: an idle Max account with weekly room counts
  as room. `helm burn` shows the Claude pool's weekly pace against a horizon
  (`helm burn horizon anthropic <utc-iso>`) and one light per project, and each
  active Claude seat hears the pace once when the default home switches
  accounts.

### Hooks and guards

- **The argument guard answers from a warm resident**: about 20 to 30 ms of
  CPU per call instead of 160 to 270 ms, with the same exit code and output.
  The console keeps the resident running; when it cannot answer, the hook runs
  cold and says why. `HELM_HOOK_RESIDENT=off` turns it off.
- **More refusals print the fix.** Each ends with a `corrected:` line you can
  run or apply as is: the substitution refusal (a backtick or `$(` in a chat,
  dispatch, task or store body, a commit message or verdict evidence) prints
  the same command with each refused substitution made literal, or moves a
  heredoc body to stdin; the never-track pre-commit refusal prints a `git
  restore --staged` of exactly the offending paths, as literal pathspecs; the
  retired-name refusal prints each old name with its replacement; and the
  seat-name refusal prints each literal with its placeholder (`seat-a`,
  `seat-b`, ...). No predicate changed.
- **The GitHub Actions rung reads prose as prose.** gh's verb words
  (`workflow`, `run`, `rerun`) inside quoted prose or a quoted heredoc body no
  longer read as a workflow run when no gh appears in the command and every
  program only receives prose (a helm chat, task or store verb, a commit
  message, echo, printf, cat, a filter). A double-quoted argument holding a
  live `$(` or backtick is read in full, as before, and every real gh
  invocation is still refused. Over one day of refused commands, this rung's
  refusals fell from 114 to 67.
- **A store rule can be tied to a helm command**, and the agent sees it once
  per context, in one line, when it runs that command; nothing is refused. Tie
  one with `helm store keywords <id> --add route:act.helm.<verb>.<flag>` or
  `helm reflex add <id> | <steer> --signal act --verb "<verb> --<flag>"`, and
  check it with `helm store resolve --act '<command>'`. An owner-asked task
  given a priority also prints how often the owner asked for it.
- The typed-ledger-id steer no longer flags a plain full 32-hex id that a
  ledger command just printed.

### Web console

- **Each project shows one health line** under its name on Work › projects:
  open stories, tasks opened and closed today, and the week's net, read by the
  same code as `helm task health`, so the board and the terminal cannot
  disagree. The About tab's week figure and the story roll-up count the same
  set.
- **New Loop editor** on a seat's Config detail: it charts every request the
  seat made and where Claude Code compacted it, and replays that history at
  another window, floor or compaction limit, with a comparison against 1M and
  a best-window finder. It shows what each request re-sends after the last
  compaction, the window the live process launched with, and the exact text
  each injected turn received. Each entry in the inject pack can change its
  line and its keywords from the page, through the same writers and checks as
  the CLI. The window is display only.
- The seats panel's mood dot is now a small face in the state colour.

### Releases

- **A lane's change note is its own file, `changes/<lane>.md`**, and a lane
  never edits `CHANGELOG.md`; two lanes in one train no longer conflict there.
  `scripts/release/release.py <version> --fold` folds the notes into the
  version's section at a cut, and a tree-wide audit refuses a note written
  under `## Unreleased`.
- Each verb's help text is its own file, `helm/help/<verb>.txt`, and each
  hook's documentation its own file under `docs/hooks/`, so lanes that change
  two verbs or two hooks no longer collide. `helm <verb> --help` prints the
  same bytes as before, and the private-name check reads the new files.

### Docs

- The landing protocol is stated once and matches what helm runs: the
  integrator merges a chain at its exact reviewed shas and gates the composed
  tree, and a reviewed commit is never amended or rebased. A test refuses any
  doc or hint that tells a writer to rebase reviewed work.
- The decision-spirit skill gains two heuristics: price every agent-loop
  change in model requests times context size, and treat routing as no
  progress until the builder's first commit.

### Performance

All numbers below are measurements stated in the commits.

- The argument guard costs about 20 to 30 ms of CPU per call from its warm
  resident, down from 160 to 270 ms.
- An idle inbox beacon ran 1,801 delivery passes an hour and now runs 13; one
  waiter's idle CPU fell from 0.4 to 1.3% of a core to 0.03%.
- The lean profile cuts about 15k tokens from a lead's fixed cost per
  request, and about 7k from the skill listing re-sent on start and resume on
  a 1M window.
- helm's own status rows no longer cost the chat node a proof each.

### Breaking or behaviour changes

#### Changed defaults

- Every Claude home helm prepares, provisions or launches on runs at high
  effort. An Opus `xhigh` and `ultracode: true` are rewritten at the next
  launch or provision, with a backup, including an owner's own `/effort xhigh`
  on Opus. A lead no longer starts with ultracode.
- Every native Claude home gets the lean profile: the Artifact tools denied,
  plugin-dev off, a skill listing budget of 0.0025 and the MCP servers in
  `lead-denied-mcp-servers` denied, where the home does not already set them.
- A native lead seat runs a 240k window.
- A Cursor-family seat is taught its 225k window again (110k in 0.3.2).
  Grok, the proxied Opus 4.6 and gpt-oss compact later.
- The PreCompact handoff reminder no longer prints to stdout, and a new
  SessionStart hook for `compact` hands back the working set.
- Fleet-down pages the owner only through the office weather; the separate
  fleet pager and the per-seat reachability pushes are gone.
- The dark-seat mover, the friction autopilot and the cubicle mover run on
  their timers without `--apply`; local names `dark-seat-mover` and
  `cubicle-mover` turn them down.
- The stop guard leaves out rows that need no act, and a row addressed to
  other seats no longer wakes its room.
- A seat settled as a non-lead team member gets the operator line and
  `longtail_guidance` instead of the full operator guidance.
- Seat events move from the main room to `#seats`.
- Auto-land releases landed lanes' leases and no longer asks for a console
  restart; compose admits one car per lane; a test-count surplus lands.
- The review mode follows the reader family's burn reading.
- `helm work gc --apply` parks idle unlanded lanes after three days, and the
  hourly `helm-gc.service` runs `helm worktree gc`.
- The chat node runs in background.slice with a bounded prover, and helm's own
  status rows are unsigned.
- A family whose on-demand spend is off with a basis reads ORANGE on money
  when its pool is high, not RED.
- The argument guard answers from a warm resident.

#### New refusals

- **`--task`** on `helm work claim` and `helm dispatch send` refuses without
  exactly one of `--whole` or `--part`.
- **`helm task add`** refuses a task with no project, and `--project none`.
  **`helm task close`** and **`helm task confirm-close`** refuse a task with
  open sub-tasks unless `--open-children-stay`.
- **`helm dispatch send` and `add`** refuse a review or build row to another
  project's lead, a review row to a proof-of-life or council-only seat (neither
  opened by `--force`), a new review chain with no task, and a superseding
  dispatch whose new lane names a different task.
- **`helm dispatch hold --source-clean`** refuses a reason with no run behind
  it and a row whose recorded read found something.
- **`helm dispatch verdict`** refuses a FIX that counts uncured findings and
  names none, and a review row refuses two stated modes that disagree.
- **`helm lr close --reason out-of-scope`** refuses a row whose own tip is on
  trunk, or whose shipping it cannot read.
- **`helm chat node up` and `down`** refuse a BOOTING node unless `--force
  --reason`.
- **`helm launch` and `helm seat resume`** refuse a `--role` that disagrees
  with the seat's spawn register.
- **`helm worktree gc --apply`** refuses when the trunk fetch fails.
- **`helm task mirror`** exits 1 on a malformed project registry.
- **`helm seat shout`** refuses without exactly one `--need` line, and over the
  seat's budget.

#### Changed output

- More refusals end with a `corrected:` line.
- The LAND line names the leases released and no longer says "needs restart"
  for the console.
- `helm chat node status` reads BOOTING, HUNG or CANNOT TELL, and prints the
  data directory, its file system and the store size.
- `helm task show` lists sub-tasks with "N of M done", `helm task list` counts
  open stories, and `helm task add` may print a parent suggestion.
- A new task's room is named `<project>-<number>`.
- `helm brief` and the console open with the office weather line, and `helm
  chat seats` shows each seat's mood.
- `helm work gc` counts `parked=N`.
- `helm pressure-watch` may say `HELM IS THE LOAD`, and its CPU line adds
  short-lived CPU and births per second.
- `helm doctor` names effort and ultracode drift, the dark-seat and cubicle
  mover states, slice limits not from the derivation and a stopped task
  mirror.

### Thanks

Thanks to ember arlynx ([@emberian](https://github.com/emberian)) for dregg,
which signs helm's chat.

## 0.3.2 — 2026-09-30

Changes since 0.3.1.

This release is about a fleet that runs its own landing loop. Approved work
now lands by itself: `helm train auto` composes a train, gates it in minutes
on a sliced whole suite while the gate canary agrees with serial, pushes it,
and stops for a person on anything it cannot prove. Review moves into pair
melds, seats hear owed work as one doorbell ring and clear routine stalls by
themselves, the web console is redesigned around one Home page and one Work
page, and helm now runs model families served from your own machine beside
paid ones. Some refusals and defaults changed: read "Breaking or behaviour
changes" at the end before you upgrade.

### Landing work

- **New `helm train auto`: the landing train drives itself.** It is a state
  machine that a timer ticks every two minutes (`helm train auto
  --install-timer`), one train at a time. It posts an intent that names each
  car and the veto line (`helm train veto <train> --reason TEXT`), waits
  300 s, composes only the cars still ready at the same tip, runs the
  tree-wide audits, gates the train through `helm gate window launch`, and
  lands it with a fast-forward push to the declared trunk. A green gate must
  also agree on the test count: the receipt's count over trunk's must equal
  the test methods the diff adds less those it removes. A tick that dies
  resumes from its recorded step and never pushes twice. After an ambiguous
  push (a lost acknowledgement, or a tick killed mid-push) it reads the
  remote, and it stops for a person unless trunk is at its own head.
  `--status`, `--pause`, `--resume` and `--abandon --reason TEXT` control it.
- **New `helm train blame <train-room>`** names the car that broke a red
  train. When exactly one car's diff touches the failing tests it names that
  car; otherwise it bisects the train's prefixes across hosts. The verdicts
  are TRUNK-RED, FLAKE, EJECT and UNKNOWN; an ejected car that passes alone
  on trunk is a CLASH with the cars before it. With `--apply` it tells the
  car's author first, then composes and gates the train again without the
  car. An ejected tip stays out until its lane has a new tip or `helm train
  readmit <tip> --reason TEXT` clears it.
- **Auto-land's pre-gate audits run the lane census, and a red audit can
  eject its car.** Beside the tree-wide audits and four fixed pre-gate
  audits, the audits run every test module the composed diff touches, the
  tests of each helm module it touches, the modules importing a touched one
  and every test naming a touched path; the log names the rule that chose
  each. When the audits fail, their failing modules are re-run alone once on
  the audits' own host in the audits' own runner; when they fail again,
  blame by diff reads the audits' log in place of a receipt and ejects the
  one car it names, as a red gate's culprit is, and the train goes on
  without it. A pass, a busy host, an unreadable log, or no car or two named
  stops the train as before.
- **`helm brief --report` writes the morning report** from
  trunk's merges and the land log, and `--check FILE` names a skipped LAND or
  a missing ask. Auto-land records each LAND in that log.
- **Reviewed tips are pinned.** Every tip a verdict or hold names is pinned
  under `refs/helm-reviewed/` before the verdict is written, so git's garbage
  collection cannot remove it, and the pin moves to
  `refs/helm-retired/reviewed/` when the row closes. `helm lr
  backfill-review-pins` pins the tips reviewed before this release; it is a
  dry run unless `--apply`. The reachability walk and the ref table skip
  these pins.
- Coordination verbs (`dispatch`, `chat`, `work`, `handoff`, `task`, `lr`,
  `store` and the other ledger writers) now run trunk's helm from any lane
  tree, with the same arguments and input. Before, a stale lane binary could
  write ledger rows that current readers dropped.
- `helm task release <id> [--note TEXT]` gives a task back to the pool, and
  the release is recorded in the ledger.
- **A task can carry the friction tax its work removes.** `helm task add` and
  `update` take `--tax N` (agent steps a day: the steps each time times the
  times a day, counted, never guessed) and `--tax-cost N` (the build cost in
  steps). Payback days are cost over tax. A cut that pays back within two days
  goes ahead of the other rows of its rank in `list` and `triage`, `show`
  prints the tax and its payback, and `list --by-tax` ranks the taxed rows.
- Every claims-ledger writer reads the ledger strictly: a file that does not
  parse, or holds a malformed row, refuses UNKNOWN and is left byte-identical,
  never read as empty and written back. `helm work release` keeps a landed
  room a metaharness pane is bound to, and reports an unlock that failed.
- A held source-clean tip closes on its own gate, and a patch-identical copy
  on trunk is owed by its reviewer. A FIX whose reviewed patches landed on
  trunk under rebased commits closes too, and the close records each patch's
  copy on trunk.
- `helm lr compose` runs its cherry-pick with rerere off, and calls a stopped
  pick empty only on evidence: a pick that rerere resolved or staged is a
  conflict.

### Test gates

- **A sliced receipt may authorize a land while the canary agrees.** In 0.3.1
  every land door refused the sliced kind. A land may now be authorized by a
  sliced whole-suite receipt (about 3.5 minutes) instead of a serial one, but
  only while the gate canary's evidence stands: three agreeing trees on two
  hosts, a red caught, and evidence no older than 36 hours. A DIVERGED
  reading keeps a durable veto on every path.
- The land-gate window sends a gate to the fastest admitted host by its median
  green land-gate time, and keys on the host by default, so two rooms may gate
  on two hosts at once.
- A focused gate of six or more modules runs as slices on the workers the host
  grants. A slice whose leak census is nonzero, disabled or unreadable mints an
  UNKNOWN receipt that binds nothing.
- `helm gate run` counts a green tree as already gated only when its receipt
  is one a land door would admit.
- No test process reaches the host's systemd user manager or its unit files.
  The suite plants a dead user bus and refuses any run of the host's
  `systemctl`.

### Review and melds

- **One pair meld per task.** A task gets one persistent mixed-family pair
  meld room, one round per dispatch, each round bound to its exact authority
  and scope. A working pair can keep one standing meld room across tasks with
  no exchange cap: `say` and `recv` never block, and the pair's dispatch sends
  and FIX verdicts name that room.
- **The meld review door.** A meld outcome binds a review row, and a review
  opens a meld on a design question or a disputed finding, never on an adopted
  patch. A meld counts as melded only after its peer spoke, and an invite names
  an invitee that is DEAF or busy before anyone waits on it.
- **A fresh-context read counts, the author's own included.** A subagent or
  Workflow run records as a read on a review row when it finished, began after
  the reviewed tip was committed, wrote none of the lane and names the tip in
  its transcript. A run that built the lane never counts: it was alive while
  the lane was written. One verb records the read and holds the row
  source-clean for auto-land: `helm dispatch verdict <row> <tip> --concur
  --measured --reviewer-model opus --reviewer-run <run> <evidence>`. One run
  backs one row.
- **A reviewer who patches becomes an author.** Its patch lands on pair
  agreement at exactly the patched tip: the lane's other author holds it, or
  another seat agrees there and the patcher holds. On a door lane the patched
  tip still owes one read by a reader that wrote none of it. A FIX whose patch
  tip the lane branch already holds is refused.
- A meld round bound to a review row closes only on a tip that row is about,
  and its closing block is checked by the citation's own validator in every
  room before the seal.
- A review send that could not reach its reader still goes out (fail-open),
  and the stored brief says which review mode the chain is in.
- Advisory model reads show on `helm lr show`, `helm dispatch triage` and the
  web card, marked ADVISORY, including reads recorded on a cancelled or rebound
  row.
- A door read goes to an idle approval-tier reader before a busy one. Every
  review row sent to a model family prints that family's 24-hour and 5-hour
  send counts beside its burn reading; no send is refused on the count.
- `helm dispatch cancel --dry-run` runs the checks and writes nothing. It
  leads with its banner, says WOULD CANCEL, WOULD ADVISORY-CLOSE and would
  un-carry, and names an already-cancelled row as such.
- The top-level `helm dispatch` usage names hold's `--meld` citation, as the
  hold parser accepts it.
- `helm dispatch show|read|get|status <id>` run triage. A row id or tip may be
  any unique prefix, and a wrong subverb prints one usage line and the closest
  match.
- **`helm dispatch melds` measures the PATCH and MELD-DIFF review modes.**
  For every chain with a recorded review mode it reports the mode, the
  enrolled reader's active rounds and FIX cure cycles, and the wall time from
  that reader's first send to the chain's first accepted source-clean hold.
  A retracted FIX counts as no round and no cure, a cancel never rewrites the
  send or hold that set the clock, and a chain with two modes, or a row this
  helm cannot read in full, reads UNKNOWN, never 0. The census only reads.
- **A MELD-DIFF cure is recorded as a cure, not a new round.** A reviewer's
  FIX in MELD-DIFF mode posts the exact diff in the pair meld and records it
  with `--diff-handoff ROOM/MSGID` and `--no-patch-because`. When the
  author's first advancing child of the reviewed tip proves it applied that
  diff, the send is cure confirmation and opens no new review round; a FIX
  on that child is a real round again. A MELD-DIFF FIX with only reason prose
  and no validated receipt counts as an ordinary round, as before.

### Seats and chat

- **Resuming an orca-adopted proxy seat starts it as that seat.**
  `helm seat resume` on a seat Orca adopted, whose session lives in a proxy
  seat's tree, now resumes through that seat's own `launch.sh` with the model
  it records, so the pane gets its proxy URL, token file and family. A
  renamed storage label resolves to its seat through the roster's rename
  lineage. When the family, the launch script or the lineage cannot be read,
  the resume refuses and says what it could not read; before, it started a
  native Claude session with no proxy, which reported that it was not logged
  in. A native Claude seat's resume is unchanged.
- **The beacon is a doorbell.** One ring line says how many rows wait and how
  to read them, and the rows stay owed to the tool-boundary hook. Unread rows
  ring again every `HELM_BEACON_BACKSTOP_S`, and a DM or a person's row leads
  and bypasses the hourly cap. A wake now costs one call instead of eight: the
  ring carries the owed rows and their triage in one read.
- A seat that owes a dispatch row hears it at every idle stop and on every
  wake. Each seat has one idle reading from its own hooks; `helm dispatch list`
  shows IDLE-OWING, and an idle-owing seat with an armed beacon is a ring
  candidate.
- **Seats answer routine prompts themselves.** A seat stalled at a permission
  prompt gets the plain Yes for a memory write or a legible routine command.
  An owner gate pages the owner and types nothing, and an opaque command goes
  to the integrator. A vendor dialog is answered only when a fresh capture
  proves it waits for input now, with one digit and never Enter; permission,
  plan and trust prompts are not answered by that door.
- A seat throttled on memory has its runaway child ended, never the agent,
  after a grace period.
- Autocompact never acts on an assumed or unproven context window, and it
  divides a seat's context by the window of the model that served it.
- A seat's turn age subtracts only the host suspend that fell inside its own
  window, and a seat's work is moved elsewhere only on a measured hung
  verdict: a laptop sleep helm cannot place reads unknown and moves nothing.
- A proxied seat with no tool call for three or more turns over five or more
  minutes reads TOOLLESS in `helm seat list` and `helm seat doctor`.
- **New `helm seat hold`.** A seat helm knows is broken takes no new work
  until `--clear` or `--until`. The dispatch door and the router also refuse a
  TOOLLESS seat, a seat in a silent-drop storm and a seat whose own last turn
  ended on a billing or credential refusal, and name a seat to use instead.
- **Work leaves a dark seat.** A seat is dark when its family reads RED on
  money or reach, when its own last turn ended on a billing or credential
  refusal, or when its pane is gone with no armed beacon, on every reading
  for ten minutes. `helm seat idle-dispatch` then moves its unstarted rows
  and tasks to live seats; it is a dry run unless `--apply`. Started, fenced
  or unread work stays, and a family reads RED on such refusals only when
  two distinct seats hit them.
- `helm seat doctor` reads DRIFT when a seat's launch script differs from a
  fresh mint. **New `helm seat remint <seat>|--all`** re-mints it without
  launching; it is a dry run unless `--apply`.
- `helm seat resume` and the autocompact gauge read the session the live seat
  runs, so a resume right after `/clear` no longer reloads the old session.
  After a boot, `seat resume --all` relaunches only the seats that were LIVE.
- `helm seat rehome` relaunches a seat only in the permission mode its pane's
  footer proves, checked against the startup settings. A mode it cannot prove
  is refused before the seat exits unless `--mode` names one, and a session
  that ran in the default mode is never widened by the target home.
- Seat homes get the canonical MCP servers and the global instructions
  (Codex-family seats are excluded by their own launch harness). A launch
  above a git root approves no project MCP servers and names the root to
  launch from.
- A remote or cloud session can be a review seat, read through a relay. The
  reviewed tip travels as a namespaced branch on the project's private drop
  repository, whose visibility is read before every push.
- `helm chat node up` writes a systemd drop-in (CPUWeight=20, Nice=10) so the
  chat node yields the CPU to interactive work, and `helm chat node status`
  reports it. A LOCKED chat node is visible, and a send refused at its door
  heals it with one revive and one retry.
- `helm chat ack` skips an id addressed to no one instead of refusing the rows
  the seat did handle, and an ack row is a receipt for exactly the rows it
  names.
- A failed read on the chat transport's ack and clear, the hooks pane census,
  the work-list overlap check, the task-notes store and the burn-down surface
  says FAILED with the exception class, instead of rendering as healthy.
- **New `helm pressure-watch`: the fleet's own stall, read from outside it.**
  A one-minute user timer in app.slice (`helm pressure-watch
  --install-timer`) reads the sustained stall time of agents.slice's CPU and
  memory pressure, so a stall of every seat at once cannot starve its own
  detector. A stall of 20% or more held for 180 s posts one row per episode
  to the room and one push to the owner's phone, naming the top slices and
  processes by memory and by CPU and the seat that launched each. A process
  is named by its program only, never by an argument (after `--eval=`,
  `--print=` or `--run=` the interpreter names itself). A hook's TIMED OUT
  line now carries the fleet's stall reading.
- **A delegate marks its seat's session only when it reads or acks.** A
  subagent or Workflow agent that runs `helm chat read` or `ack` marks its
  seat's session for 15 minutes, since the mark cannot tell whose read is
  whose; any other chat verb, and a help ask (`--help`, `-h`), marks nothing.
  A read that delivers nothing says why: inside the mark, piped to a filter,
  too long to be shown whole, or cutting a row. It names the read that
  delivers (rows whole in 10 KiB and 256 lines), or the ack when a row is
  too long for any read to show whole.
- **The expert registry stays current by itself.** Every `helm handoff
  write` refreshes the writing session's entry in `helm session experts` (its
  project as the domain, any `domain:` lines as subdomains, and the handoff
  path); a registry error there is one stderr line and never refuses the
  handoff. `--retire` keeps an entry as history, and `--to` names and
  registers its live successor. Each entry records its harness, and `helm
  session ask` offers only native Claude sessions, since its resume line is a
  bare `claude --resume`.

### Model families, credentials and quota

- **Model families served from your own machine.** helm runs a family served
  by your own vLLM or llama-server endpoint, keyless and free, with its window
  read from a live `/v1/models` probe. The catalog field `"profile": "lite"`
  generates a local seat's tool set, MCP floor, window pins and output caps; a
  local seat's fixed cost per request goes from 14.6k to 6.8k tokens. A local
  family's window plus its output cap plus a compaction margin of at least
  16,384 tokens must fit the server's slot.
- A local family reads GREEN only while it is certified (`helm burn
  certify-local <family> --until ...`) on the operator's live session. Local
  model seats launch with tool denies for `git push`, `gh pr` writes and `gh
  repo` writes.
- A family on a paid Cursor plan runs through a helm-supervised bridge
  sidecar with an exact environment allowlist, and a wedge is confirmed by a
  second probe before any restart.
- **Per-project teams and shares.** A project can have its own team and
  family shares. An unset share is None, not 0%, and the billed family is the
  one a seat actually spends.
- Each Claude account's five-hour window is projected from usage history and
  reads OK, WATCH or TIGHT, with hysteresis, in `helm creds`, `helm burn` and
  the quota rows. Nothing blocks work. A seat the credential watcher can move
  hears nothing while another account reads OK; when none does, it is asked
  once per state per window to route new builds and reviews to other seats
  and keep its own work going. A seat homed to one account hears that
  account's state and is asked to pace its work to the reset. An account
  nobody could read never counts as room.
- `helm burn calibrate` backtests the Codex runway reading against its own
  history.
- `helm cred list` and the quota rows show each credential home's token
  lineage against its metadata account. A token family shared by two accounts
  reads AMBIGUOUS, and a Claude metadata account is labelled as metadata.
- A proxy cooldown that a vendor quota window caused is one line per episode
  that names the window. A cooldown that an empty balance caused says there is
  no timed reset.
- `helm doctor` and `helm seat status` read the proxy binary's reported
  commit against its source checkout, name a skew and how to read the running
  code, and say UNKNOWN when either side cannot be read.
- **Codex seats run `gpt-6.1-sol` with a 220k window.** Before, one Codex
  seat ran a 320k window on `gpt-6-sol`, over the catalog's 220k cap, so its
  context read past 100%. `gpt-6-sol` stays probed as a manual rollback
  target; nothing switches to it by itself.
- **The cursor seat is taught a 110k window.** Cursor counts a Claude Code
  history at three to six times the bridge's estimate and stops making tool
  calls once its own count passes its 256k window, before Claude Code would
  compact under the old 225k seat setting. The cursor family now declares a
  110,000 `context_budget`, which the seat is taught on both context knobs and the
  autocompact watchdog gauge. The watchdog's 80% threshold is 88k; Claude
  Code can compact earlier because it reserves output tokens. The 225k
  `max_context` stays the family's measured ceiling.

### Hooks and guards

- **No AI attribution anywhere helm installs.** Every agent config the hooks
  reach carries attribution settings with no commit, PR or session line, and a
  prepared credential home carries them before its first session. The
  commit-msg trailer rung refuses every model family the fleet runs, by its own
  name, harness display names and catalog ids, a footer that names any model
  family, and vendor AI addresses.
- **A private name is refused at commit time.** Under the full ("rail")
  guard, the pre-commit hook refuses a commit that adds a line naming an entry
  on the private-names list; before, such a line first met the train's audits.
  New `helm classify` is the optional client for a local classifier that ranks
  new candidates for the list.
- **The argument guard refuses a recursive grep with no path or a broad
  root** (`/`, a home, `/tmp`, a lane parent, helm's stores), in every
  spelling (`grep`, `egrep`, `fgrep`, `ugrep`, `rgrep`, and through `sh -c`,
  `eval` and heredocs), and names `git grep` and `rg <narrow path>`. Over
  helm's 30,727 tracked shell and Markdown lines it refuses none.
- **A shell write or `git init` into the shared checkout is refused**, and a
  checkout-watch timer reports a stray shared-checkout write to the
  integrator.
- **A subagent cannot start another agent.** The argument guard refuses an
  Agent call from a subagent, and the subagent cap keys on the family helm
  verified, not the one a seat declares.
- **The stop guard stops fighting work already in progress.** A seat whose
  own Workflow run or background agent is live counts as busy, a held review
  lease reads IN PROGRESS until it nears expiry, the shared-room blind-spot
  note is said once per room per session, and the idle offer never hands a
  seat a row assigned to another seat. A seat with no live work still hears
  every row it owes at each idle stop.
- **The GitHub Actions rung reads GitHub's paths.** In 0.3.1 and earlier the
  match was on text, so a path with an `/actions/` or `/dispatches` segment, such as an
  ordinary `src/actions/` directory, was refused. Actions REST paths are now
  anchored to GitHub's owner shapes (`repos/<o>/<r>`, `orgs/<o>`,
  `organizations/<id>`, `enterprises/<e>`, or `repositories/<id>`), so
  DigitalOcean endpoints and an ordinary `src/actions/` directory pass;
  `.github/actions` remains refused. The bare segment is still refused in a
  `gh api` call that is not a read, and in a command that names
  `api.github.com`, a GHE `/api/v3` base, or a GHE.com host
  (`api.<subdomain>.ghe.com`). That rule reads a set of words over the WHOLE
  command, not a phrase about one call: a `gh api` read elsewhere on the
  line, a `gh` command beside a variable root spelled `$API`, or a token
  read of `api.github.com` beside a DigitalOcean call is refused, as it was
  before. Where the API base comes from a variable and the command names no
  GitHub host, four spellings pass that the previous rule refused: a client
  other than gh whose whole API root is the variable; gh reached through a
  variable word (`gh $SUB`, `$GHBIN api`) with a variable endpoint; a root
  written as a shell brace or a curl glob (`$H/{repos,x}/...`,
  `$H/repo[r-s]/...`), because the guard does not expand braces or globs;
  and a root word that a variable supplies or begins (`$H/$ROOT/o/r/...`,
  `$H/${X}s/o/r/...`). Two known gaps are unchanged: the check-suite and
  check-run rerequest endpoints, and SDK method calls such as Octokit's
  `rest.actions.*`, name no protected path and pass as before.
- The same rung reads two more spellings as data: the filter of a top-level
  `jq` whose output reaches only the terminal, and the prose of a message
  that names the workflow directory, when every command on the line is `cd`,
  `git add`, `git commit`, `git tag`, `helm chat post`, `helm chat ack` or
  `helm task add` and `git commit` opens no editor.
- An unquoted heredoc body that a command reads as input is data, so prose in
  a brief no longer trips the argument guard.
- The pre-push shared-history rung refuses every push whose destination it
  cannot prove.
- The Stop hook prints only the lease lines that changed, and derives each
  dispatch repository's project once per stop.
- **The GitHub Actions re-execute rung needs `gh` at its head.** Plain English
  such as "run" and "rerun" in a message or a brief no longer reads as a
  workflow re-run, and the Windows spelling `gh.exe` is refused like `gh`.

### Web console

- **The console is redesigned.** `/` opens **Home**: one screen of headline
  tiles (projects, work, supply, accounts, seats, chat, signing record and
  fleet notes), an "on you" strip only while something waits on the owner,
  and the away switch. Then four pages: **Work · Chat · Fleet · History**.
  Every section change and every link lands on its target, below the sticky
  navigation.
- **One Work page** replaces the land board, the scheduler page, the
  burn-down and the old backlog view, which drew the same work three times
  with four counts. Every piece of work is one card on one row of stages: To
  do, Building, In review, Landing and Landed, under a Backlog | Pipeline |
  Done band, with River and List lenses, filters for project, whose move,
  rank and stuck, and a drawer that shows the card's whole path and lives in
  the page address (`GET /api/work`). The nav badge, Home and each project's
  Work tab read the same count. A count not wholly read says "at least" or
  "?", never a bare number or 0, and a stale reading says how old it is. Old
  `#scheduler`, `tab=lanes` and `tab=tasks` links open the matching view.
- **Fleet › credit** opens with one families card: every model family in its
  burn colour, with the account groups that bill it in route order.
  **Fleet › models** is the model scorecard (`/api/models`, `helm eval
  board`).
- On Fleet › credit, every Claude home shows its token lineage as text. A
  spent account says it is spent and when it is back, offers no login, and
  sits with the walled rows; an account declared free is never counted as
  paid; and `helm accounts`, `helm accounts --json` and the card read one
  totals rule.
- **The land pipeline is one reading on every surface.** The nav badge, Home,
  each project's row and the Work page read one pipeline reading with its
  scope and age. A count nobody read is never drawn as 0, a partial reading
  is shown as a floor, and a reading with no measured clock says its age is
  unknown.
- Fleet › boxes no longer shows an old measurement as today's fleet: a stale
  reading opens with an Archive sentence naming its date, and dates each box's
  state.
- The land board's own text names no helm verb, and the console's dead
  references now name real places (the quota tab reads Fleet › credit and
  links to it).
- **Every `helm web` follows its code.** A board re-execs onto a changed
  tree after its import check passes, so no board serves code older than its
  checkout; a broken web module leaves it serving the old code instead of
  crashing. `HELM_WEB_FOLLOW=0` keeps a board on the code it started with,
  except the server that writes the hooks' stop facts (by default the one on
  the console port), which always follows.
- A Work poll reads only the board sections it draws, and a board waits for
  each leg only as long as that read's own budget.
- New `helm web shot` captures a view before and after a change, on a
  throwaway port, home and Chrome profile, once the view's reads settle.
  `helm web walk` prints the brief a fresh reader walks the console by.
- **`helm web`'s memory is bounded.** It grew about 2 GB of memory and
  1.5 GB of swap an hour. The ledger walks now read a per-room index, and the
  allocator is bounded in process and, through `helm web unit --install`, in
  its systemd unit. The growth itself is not cured yet: under a 2 GB memory
  cap and an hourly restart, a unit life still reaches the cap within the
  hour. `helm doctor` names every ad hoc `helm web` it finds.
- **The console never tells the owner to run a helm command.** Home, the
  Seats READY panel and roster, History (hovers included), the Chat view and
  a project's Team tab state the fact in plain words or link to where it
  lives. Where the same server text also feeds an agent CLI, the console reads
  owner-facing fields (`owner_repair` and `owner_note` in `helm ready --json`,
  `say` in `helm team --json`) and the terminal text keeps its commands.
- **One piece of work is one card on the Work page.** A reused lane's room
  sits on its own task's card, never on the card of the task the lane name
  first carried, and a room still working on landed work is shown as a
  writing move on the landed card instead of a second card. A project's About
  page counts commits on the project's own trunk branch, not "lands".
- **A seat run outside `helm launch` reads as expected, not as a signing
  alarm.** A lead running in the owner's own session inherits the owner's
  profile, and helm refuses to sign as the owner, so its rows go out
  unsigned. History's top card and the Chat view now show that as a muted
  expected line, and they say a real signing failure in plain words with who
  repairs it. A refusal under a recognised owner name stays an alarm (a
  failed identity swap), and signing is announced restored only for a seat
  that was failing.
- **One word per seat count.** "active" is a seat with a verified beat in
  the last two minutes, and Home's seats tile, the Seats strip and the
  roster count all count it; "joined" is every seat but the owner and
  subagents, on the roster and the seat picker; "able to work" is only the
  Projects headline (seats that can take work now), and "at work" only
  Projects' seats that hold no lane. Before, "able to work" named three
  different counts on one screen.
- **The Credit page says what to do in sentences, not commands.** Account
  rows, attention pills, the accounts and homes cards and the fill screen
  state the fact and who acts on it, and no longer offer a command to copy.
  A homes card refusal shows the owner's words (`owner_error` beside the
  CLI's unchanged `error`), and a misnamed home folder is said to be not
  named for the account it holds.
- **A to-do card helm has not wholly read says so.** While a project's
  pipeline or the work rooms are still being read (as they are for a while
  after a server restart), or the pipeline reading is past its bound, a
  to-do card on the Work page says it is on the to-do list as far as helm
  has read and may already be under way. It no
  longer says nobody holds it, that it is not started, or that any idle seat
  may take it, and its whose-move reads "not read", not UNOWNED. A card read
  whole keeps its words.
- **The Work page joins work to its task by the stored task key.** A lane
  whose work room records its task (`helm work claim --task`) sits on that
  task's card whatever its name says; a land joins by the task recorded at
  the land, so a lane name reused for another task never carries the old
  task's land, dots or train crossing onto the new card. When the lane
  records cannot be read, the link reads unknown, never the name's guess.
- **The Work page says what it has not read.** Landed on a project that
  merges by hand reads "not measured", and the all-projects Landed count is a
  floor while any project does. The Leftovers and Seams folds, the nav badge,
  Home and the stage chips share one bound, so a count over a source not read
  is "at least" or "?", never a bare number or 0. A request that a finished
  train pushed at its reviewed or held tip moves to Landed. Opening Chat
  clears the owner's unread only on a real gesture in the Chat view.

### Releases

- **One in-repo command publishes a release.** `python3
  scripts/release/release.py <version>` is a dry run; `--publish` performs the
  writes. It backs up the public repository, builds the release as one new
  commit that fast-forwards the public main (never a force push), gates it
  (the tree, seat attribution, private needles in every blob, `bin/helm
  --help` and `bin/helm doctor` in a scratch home, the installer, gitleaks),
  and runs the same battery again on the published repository. The work
  directory is on disk, outside the checkout.
- The release tree ships no private handles, seat attributions or lineage;
  the docs state the review rule, the land gate and the eval facts helm
  enforces today.
- **New `helm release nightly`** runs the release's read, backup, build and
  gate steps every night on a build host and prints `NIGHTLY GREEN` or
  `NIGHTLY RED step=<step>`.

### Docs

- The README is rewritten for a first-time reader: what helm is for, where it
  works today, and what needs work, with the detail moved to linked pages.
- New: docs/INSTALL.md (requirements, install, first run), docs/TOUR.md
  (every part, verb by verb), docs/LANDING.md (review, the gate and trains)
  and docs/EVAL.md (the 11-axis agent-experience scores over time, with the
  rubric and the independent-rater battery).
- AGENTS.md states the review rule as context, then model, then family.
- docs/ARCHITECTURE.md has a section on harness, metaharness, model family
  and backend.

### Performance

All numbers below are measurements stated in the commits.

- `helm --help` lists each verb on one line: the root listing falls from
  120,691 to 11,866 bytes. `helm <verb> --help` still prints the full entry.
- `helm web` asks git about one sha at a time instead of listing all 1.49M
  objects every 22 s.
- A cold ledger fold memoizes its git questions and batches the small ones, so
  a refold after a land no longer pays about 2,000 git processes. A carried
  close replays against the trunk it recorded, and the dispatch fold runs
  single-flight.
- The lite profile cuts a local seat's fixed cost per request from 14.6k to
  6.8k tokens.
- Dead code is deleted, and `helm/dispatches.py` sheds six concerns into
  sibling modules (1,042,221 to 832,609 bytes).

### Breaking or behaviour changes

#### Changed defaults

- Land doors admit a sliced whole-suite receipt while the gate canary's
  evidence stands. In 0.3.1 every land door refused it. A DIVERGED canary
  vetoes it.
- Coordination verbs run trunk's helm from a lane tree.
- The gate window keys on the host, so two rooms may gate at once.
- The doorbell rings once with a count; a seat reads its rows with `helm
  dispatch triage` instead of receiving them whole in the wake.
- The hooks write attribution settings with no commit, PR or session line
  into every agent config they reach.
- Seat homes get the canonical MCP servers and the global instructions, and
  local-model seats launch with push and `gh` write denies.
- `helm chat node up` writes a CPU-priority drop-in for the chat node.
- Every `helm web` follows its code after a land (`HELM_WEB_FOLLOW=0` to
  keep a board on the code it started with, except the server that writes
  the hooks' stop facts).
- The GitHub Actions rung matches GitHub's own paths, so an ordinary
  `src/actions/` directory and DigitalOcean's actions API pass.
- Codex seats launch on `gpt-6.1-sol` with a 220k window.
- `helm web` bounds its allocator (`MALLOC_ARENA_MAX=2`,
  `MALLOC_MMAP_THRESHOLD_=131072`); `helm web unit --install` writes the same
  bound into its unit.
- A delegate's chat verbs other than read and ack, and its help asks, no
  longer mark the seat's session.

#### New refusals

- **The argument guard** refuses a recursive grep with no path or a broad
  root, a shell write or `git init` into the shared checkout, and an Agent
  call from a subagent.
- **The commit-msg rung** refuses any model family's name as a trailer
  credit, a footer that names a model family, and vendor AI addresses.
- **The pre-commit hook**, under the full guard, refuses a staged line that
  names an entry on the private-names list.
- **`helm dispatch send`** refuses two bodies, a positional message and a
  piped or heredoc body.
- **The dispatch door** refuses a seat under `helm seat hold`, a TOOLLESS
  seat, a seat in a silent-drop storm and a seat whose own last turn ended on
  a billing or credential refusal. `--force` past a hold, a storm or a wall
  needs `--reason`, and the admission is recorded; before, `--force` alone
  filed a row past a family walled on money.
- **`helm burn declare`** refuses a declaration that reads better than the
  measured one.
- **`helm dispatch verdict`** refuses to record a fresh-context run that began
  before the reviewed tip was committed, a run already recorded on another
  row, and a FIX patch tip the lane branch already holds; `helm dispatch
  hold --source-clean` refuses a holder who patched the lane, unless pair
  agreement carries it.
- **`helm task release`** refuses a note over 2,000 characters, and **`helm
  task claim`** refuses an unknown flag or a stray word.
- **`helm seat rehome`** refuses a permission mode it cannot prove unless
  `--mode` names one.
- **A seat launch** above a git root approves no project MCP servers.

#### Changed output

- The root `helm --help` prints one line per verb.
- The land board, the scheduler page and the burn-down are retired into the
  one Work page; their old links open the matching Work view.
- `helm dispatch list` shows IDLE-OWING; `helm seat list` and `helm seat
  doctor` show TOOLLESS; `helm seat doctor` shows DRIFT.
- Refusals name `--force` where they said `force=True`.
- `helm cred list` and the quota rows show token lineage.
- `helm doctor` and `helm seat status` print the proxy's build against its
  source checkout.
- `/api/work` and `/api/backlog` are new, and `/api/board` feeds Work ›
  projects, Home and the families card on Fleet › credit.
- The Work page shows "not measured", "at least" or "?" where it showed 0 or
  a bare count over a reading not whole.
- A chat read that delivers nothing says why on stderr and names the read
  or ack that does.
- `helm ready --json` adds `owner_repair` and `owner_note` to each signal,
  and `helm team --json` adds `say` to drift rows.

### Thanks

Thanks to ember arlynx ([@emberian](https://github.com/emberian)) for dregg,
which signs helm's chat.

## 0.3.1 — 2026-09-24

Changes since 0.3.0.

This release is about the cost of running a fleet every day. A stop no longer
rebuilds fleet state, a dispatch send or verdict no longer waits behind a
ledger rebuild, and a seat stays reachable after its 30-minute watch ends. In
helm's own repository, a lane no longer runs its own whole test suite: the one
serial whole suite runs at the land gate, and a lane that needs a whole suite
gets a sliced run that is about six times faster. The argument guard gains
refusals for commands that print secrets into a transcript, change the shared
checkout, or write verdicts from a subagent. Some refusals and defaults
changed: read "Breaking or behaviour changes" at the end before you upgrade.
The one new requirement is to keep `helm web` running, because the Stop hook
now reads the facts it writes.

### Test gates

The whole-suite rules below are helm's own landing rules. They apply when
`helm gate run` runs helm's own test suite. A project that declares its own
gate command keeps its own rules.

- **A whole suite runs once, at the land gate.** The land gate is one serial
  whole suite on the exact tree that lands: the integrator's train. In a lane
  room (`<repo>-wt/<lane>`), `helm gate run` refuses a whole suite and prints
  the focused route instead. The escape is `--lane-suite --why TEXT`: the
  reason goes on the receipt label, and every escape is counted. In one
  measured week, 142 whole suites (68.6 hours) had run on lane tips.
- **A green tree is never gated twice, and a red tree reruns only with
  `--again`.** When a tree's last whole-suite receipt is green and binds it,
  `helm gate run` refuses and prints that receipt's evidence line for you to
  cite. When the last receipt is red, a rerun measures only a flake, so it
  needs `--again`, which is recorded on the label. In a compose room, the
  whole suite launches through `helm gate window launch`.
- **A lane-level whole suite runs as slices.** With no mode flag, a whole
  suite in a lane room admitted by `--lane-suite`, a peek, a seat's home or a
  harness worktree runs as parallel slices of one serial discovery and mints
  a sliced receipt. On one build node, the same 22,533 tests measured 906.5 s
  serial and 145.1 s as slices. Every worker must agree on one ordered test
  inventory, and an audit fails any test module that leaves process state or
  module data behind. Everywhere else (the shared checkout, a compose or train
  room, a gate labelled `train...`, a Fab job) the suite runs serial. A room
  that cannot run slices (fewer than four CPUs, or a tree whose own
  `helm/gate.py` predates the sliced kind) runs serial and says why.
  `--serial` forces serial, and `--sliced --box HOST` runs slices on that
  host.
- **A sliced receipt never authorizes a land.** It binds a lane tip and a
  review's APPROVE. Slices do not see data that one test module leaves in a
  shared module for a module on another worker, so every land door refuses the
  sliced kind by name and a land needs a serial whole-suite receipt.
- **New `helm gate canary`** keeps checking that slices agree with serial. It
  holds one serial and one sliced receipt of trunk's tip and compares them
  test by test. A divergence posts one alert and writes a marker that stays
  until a person clears it (`helm gate canary clear --reason ...`). While
  the marker stands, no sliced receipt may authorize a land; in this release
  none does. `--install-timer` installs the nightly timer, which is not
  installed for you.
- **Refusals name the move that works in your room.** Every focus-planner
  refusal, and every refusal that told a lane room to run the whole suite, now
  names the tree-wide audits plus the lane's own test modules (`helm gate
  audits`) or the `--lane-suite --why` escape. Outside a lane room they name
  `helm gate run`, or `fab gate` on a host that refuses local suites.
- `helm gate slice-timings` prints the newest sliced receipt's per-module
  seconds as one JSON object. `helm gate run --sliced --timings FILE` reads
  such a file to schedule the longest modules first. A file that does not
  validate is named and ignored, never trusted.
- `helm gate run --plan --json` names the resolved `mode` and `mode_reason`,
  and for a sliced run the slice command and the most `workers` it starts, so
  a runner outside helm can forward `--sliced` and size its grant.
- A gated suite no longer inherits the gate's own `HELM_HOME` and
  `MELD_HOME`. Before, a test could write rows into the ledger that the gate's
  receipt goes to, and the receipt's import was then refused.
- **A suite killed by a signal reports 128+N.** A suite that died of SIGTERM
  reported exit 241 (256-15), which reads as an exit code. It now reports 143
  (128+15). A worker of the diagnostic shard runner (`helm/gateshard.py`)
  reports the same way, and its pool names the signal.
- A whole-suite gate of helm's own tree could end UNKNOWN part-way: one test
  sent SIGTERM to the suite runner itself. That test now runs its subject in a
  separate interpreter. The receipt parser still refuses a run that died
  part-way, instead of reading an earlier summary as the result.

### Landing work

- **New `helm train`** composes the landing window. By default it is a dry
  run: it lists this project's approve-ready rows, their reviewed tips and the
  merge order, oldest approve first, and names every READY row it leaves out
  and why. Rows that read READY-SELF-REVIEW or READY-CONTESTED, and rows whose
  independence reads UNKNOWN, are left out by name. `--apply` makes one room
  under `<repo>-wt/compose/`, merges each reviewed tip by its exact commit,
  with rerere off, and launches one whole suite through `helm gate window
  launch`. A conflict is aborted, never resolved: the row and its lane are
  named, and the other rows still compose. A car whose reviewed tip is more
  than `--max-behind N` commits behind trunk (default 200) is skipped and
  named. `--apply` refuses when the local trunk differs from the declared
  remote trunk, or when that trunk is undeclared or cannot be read.
- **A land takes only a receipt helm can prove it ran.** Every check `helm
  gate import` makes (schema, content id, head, tree) is one the artifact's
  submitter can make too, so an edited receipt could import and bind a land.
  Every land door now asks, last, which authenticated door placed the receipt
  in the repository being landed: a local `helm gate run`, a completion helm
  observed for a job that `helm gate window launch` dispatched, or a routed
  `helm gate run --box` session. A receipt that only `helm gate import` placed
  still binds a lane tip and an APPROVE, and the land refusal names `helm gate
  window launch` as the cure. This check is off until the integrator runs
  `helm gate provenance --activate`, and it is forward-only: a land made
  before the activation, and a receipt the ledger already held, are judged as
  they were. A project that declares its own gate command has no
  authenticated remote door yet, so its land binds as before and every door
  says `provenance: unauthenticated-no-door`. `helm gate provenance` prints
  the state.
- The label given to `helm gate window launch --label` now travels with the
  job to the receipt, where the remote runner accepts it, and the launch says
  whether it did. `helm gate show` measures a receipt's standing in the
  repository the receipt was placed in, and says so, instead of reading
  UNKNOWN from any other directory. In a compose room, a green receipt that
  no land door would take no longer stops a new whole suite: the run goes to
  the window with a note that says why.
- **New `helm dispatch retract`** takes back a wrong verdict without
  rewriting it. It appends one event after the verdict, and the row then reads
  RETRACTED wherever authority is read, so a wrong APPROVE authorizes nothing.
  The verdict's author, the integrator or the owner may retract. `--reissue`
  sends the successor review in the same call. A second verdict on a row now
  names `retract` in its refusal.
- **Sends and verdicts no longer wait behind a ledger rebuild.** A land makes
  every ledger checkpoint cold, and the first writer after a land rebuilt it
  while it held the dispatch ledger lock (102.2 s measured once). Every
  dispatch-ledger writer now reads outside the lock and holds it only to
  write. Of two sends of one cured operation, the one that finds the other's
  successor reports not sent.
- A dispatch brief longer than the row's cap now says, on the row's copy,
  that the whole brief is stored by reference and how to read it (`helm
  dispatch triage <id>`). `helm dispatch triage` labels a cut copy TRUNCATED,
  with the bytes kept and sent, and the send says how many bytes the recipient
  sees unless it follows the reference.
- A build sent against trunk no longer shows as landed the moment it is sent.
  helm asks a build's own lane whether its work reached trunk, never the base
  it was sent against. A build lane landed by cherry-pick counts as landed
  when a kept patch-identity proof says so.
- `helm lr compose` refuses a closed row that still reads READY, by name.
  `helm lr list`, `helm lr show`, `helm train` and the READY ladder use one
  test for a live READY row.

### Seats and chat

- **A seat stays reachable after its 30-minute watch.** Claude Code stops
  every Monitor at 30 minutes, which ends a seat's waiter without its cleanup,
  and helm then read the seat as DEAF and refused dispatches to it. A seat
  whose waiter ended at that deadline now reads WAKING for a grace period:
  helm never types into it, and it shows as DEGRADED, not UNUSABLE. A dispatch
  to a seat whose only refusal is DEAF is filed with an advisory, the router
  ranks that seat last, and `helm reviewers` keeps it eligible with a
  "DEAF (nudge pending)" note. With `--post`, `helm beacons` re-arms a DEAF
  seat that owes work by typing into its pane, once per DEAF spell, for any
  model family. A seat that owes nothing is never typed into.
- A seat relaunched onto a fresh session was refused at its own chat join as
  a claim-jump, so its waiter never armed and `helm seat list` read UNUSABLE
  over a live pane. A session that no process holds now counts as a seat
  between panes.
- **A seat under your inherited profile signs as itself.** A seat started
  outside `helm launch` inherits your `HELM_CELL_PROFILE` from your shell, and
  its signed posts were refused. When the identity layer admits the process as
  its seat, it now signs with the seat's own key, which the signer makes on
  first use. Any other profile that names someone else still posts unsigned.
- **`helm chat node up` remembers the binary it installed** (its path and
  sha256), and a bare `up` uses it. Before, a bare `up` could bring back the
  old node binary against a new chain's data. A record that cannot be honoured
  refuses and prints `HELM_CHAT_NODE_BIN=<binary> helm chat node up`. `helm
  chat node status` and `helm doctor` print the binary and where it came from.
- The session catalog honours `HELM_CACHE_DIR`. Before, a scratch or test
  home still wrote its catalog caches into `~/.cache/helm`.

### Hooks and guards

- **The Stop hook reads a snapshot instead of rebuilding fleet state.** The
  `helm web` resident computes the facts every stop needs (gate and approval
  exemptions, room advice, what each seat owes, review rounds and the
  composition-seam rows) when their inputs move, and writes them to one file.
  A stop checks that each fact is still exact, with no subprocess, and never
  waits for one. On a live fleet, stops measured about 0.3-0.8 s (p50 326 ms,
  p95 824 ms over 14 stops), where one seat's stops had taken 12-20 s. A fact
  that is stale or absent never grants an exemption: the stop keeps its block
  and prints the snapshot's age. After a land, the resident restarts itself
  on the new code once that code imports. `helm doctor` reports a missing
  snapshot and a resident that runs older code than the tree.
- **Hooks start faster.** helm's hook child now starts Python without its
  site stage, from the interpreter helm recorded the first time, and uses GNU
  timeout (`gnutimeout`) when the host has it. Measured at load 12.5-16.8: the
  argument guard went from p50/p90 207/310 ms to 110/144 ms. Every state helm
  cannot prove keeps the old start. `helm doctor` says whether hooks still pay
  for an eager site stage.
- `helm hooks latency` now measures every hook event, not only PostToolUse. A
  hook that timed out or was cancelled is also kept in an incidents file that
  holds about a day, so a day's timeouts can be counted.
- **The argument guard refuses a command that prints secrets into the
  transcript**: the whole environment, a process's environment, a
  credentials file, or a secret-looking variable, for Bash and Monitor calls.
  It follows `bash -c`, `eval`, `ssh`, `su -c`, `docker exec`, `kubectl exec`
  and `podman exec`, follows process substitutions, and tells a file a
  command writes from a file it prints. `env` as a launcher, a presence
  check, `grep -c`, `wc -l`, a names-only filter and a redirect to a file
  pass. `export $(...)` is refused, because an empty substitution runs a bare
  `export`. The refusal names the cure. Over about 400,000 recorded fleet
  commands it refuses 0.10%.
- **A working-tree git command cannot run in the shared checkout.** In a
  repository that uses the full ("rail") guard profile and has lanes, the
  argument guard refuses a git command that would change the shared
  checkout's working tree: `stash pop`, `apply`, `drop` or `branch`;
  `checkout` or `restore` with an operand; `reset --hard`, `--merge` or
  `--keep`; `merge` or `pull` without `--ff-only`; `rebase`, `cherry-pick`,
  `revert`, `am` and `switch`; and `clean` without `-n`. It follows `cd`,
  `pushd` and `git -C`, and the refusal spells the command for the lane.
  `HELM_WORK_INTEGRATOR=1` allows it.
- **A subagent cannot write verdicts as its seat.** A subagent or Workflow
  agent shares its seat's name and session, so its ledger writes looked like
  the seat's own. The argument guard now refuses a delegate's verdict-class
  writes unless the seat grants them with the new `helm delegate allow --verbs
  "..."` (30 minutes by default, 24 hours at most): `helm dispatch` verdict,
  retract, hold, release, cancel, rebind and retip; `helm lr` close, land,
  abandon, retire and expired; `helm store` confirm, supersede, revise,
  retire and reject; and `helm handoff write`. `helm delegate list` and
  `revoke` manage grants, and no grant admits `allow` or `revoke`. Sends,
  claims, chat posts, gate runs, usage calls, dry runs and a literal test
  `HELM_HOME` stay open.
- **Words that are data are not commands.** The GitHub Actions and delegate
  rungs refuse a verb only where the shell runs it. A grep pattern, a `pgrep`
  or `ps` argument, a quoted heredoc body read by `cat` or `tee`, an `echo`
  argument, and a `git -c user.name=... commit` are data. Python text given
  to `python -c` or in a heredoc is data only when every import comes from a
  list of modules that cannot start a process and it names no dynamic
  primitive such as `exec`, `eval`, `getattr` or `__import__`; a heredoc
  body that does not parse as Python is prose, and data too. In Python text
  that is not data, a list or call that spells a helm command is read as that
  command. For a delegate, `xargs` or `parallel` feeding a helm command
  is refused as UNKNOWN, and no grant admits it.
- The substitution guard now covers `helm store revise`, as it covers `helm
  store add`: a backtick or `$(...)` in a double-quoted statement runs before
  helm starts.
- **The pre-push host-path guard says UNKNOWN when it cannot read a
  repository's visibility.** It reads PRIVATE, PUBLIC or UNKNOWN with the
  cause, and asks `gh` once more after a failed probe. UNKNOWN still scans and
  refuses, and the refusal now reads `<remote>: visibility UNKNOWN (<why>),
  treated as public`. Before, it called a private repository public.
- **The removed-name check refuses less.** The full guard no longer refuses a
  commit that renames one of two duplicate top-level definitions while the
  file still defines the name with a `def` or `class`. In another file that
  defines its own top-level function of the same name, a bare use of that name
  no longer counts as a use of the removed one.
- The pre-commit vacuous-assertion advisory no longer warns about a mock's
  `assert_not_called()` or `assert_not_awaited()` when a positive assertion
  on the same double stands beside it. The same absence with no positive
  assertion on that double still warns.

### Web cockpit

- **The owner board lists only live obligations.** The Board's waits list
  had grown to 900 rows up to 60 days old, most of them already landed or
  settled. The waits and the kanban now list live obligations only. Every
  other open row is counted on one line per class, with its oldest age and
  the command that lists it: work left over after landing
  (`helm lr retire --off-frontier`), work on main with no verdict recorded
  (`helm lr list`), and rounds that a later round absorbed (`helm lr list
  --all`). A row on main under a live FIX stays listed, and an owner-gated
  hold stays listed.

### Performance

All numbers below are measurements stated in the commits.

- A ledger read no longer takes minutes when Orca-launched and plain seats
  take turns. Orca launches seats with extra credential settings in git's
  configuration, which gave the two kinds of seat different fingerprints for
  one checkpoint file, so each replaced the other's checkpoint with a cold one
  (one measured read: 92.8 s instead of 4.5 s). Credential settings are no
  longer part of the fingerprint.
- Checking whether each build lane landed reads ancestry and the lane's own
  history, and no longer walks every trunk patch: 0.18 s cold over 92 build
  lanes, where it had taken minutes.
- In the Stop hook, the claims check costs at most 0.14 s and the
  composition-seam check at most 0.05 s, measured over 40 stops. The seam
  check had taken 1.4-4.8 s per stop.
- `helm work claim` asks git for the hooks directory once per guard check
  instead of once per hook name (about 41 times per claim before).

### Breaking or behaviour changes

#### Required on upgrade

- **Keep `helm web` running.** The Stop hook now reads the stop facts that the
  `helm web` server on the console port writes (docs/WEB.md shows a systemd
  user unit for it). With no `helm web` running, every claims exemption (a
  lane in gate, a lane approved) is refused at every stop, the
  composition-seam check only warns, and `helm doctor` says so. The
  memory-index cap and the scratch reaper also moved from the stop to that
  server, which runs them every minute. `HELM_STOP_FACTS_LEG=1` turns the
  facts on for a server on another port.

#### Changed defaults

- In helm's own repository, a whole suite with no mode flag in a lane-level
  room runs as slices and mints a sliced receipt, which cannot authorize a
  land. Pass `--serial` for a serial run.
- A seat that inherited your signing profile from your shell signs as itself
  when the identity layer admits it. In 0.3.0 it posted unsigned.
- `helm beacons --post` re-arms a DEAF seat that owes work on any model
  family. On a paid family this spends one paid turn per DEAF spell. Before,
  it re-armed only native Claude seats.
- A seat whose waiter ended at the 30-minute watch reads WAKING, not DEAF,
  for a grace period. A dispatch to a seat whose only refusal is DEAF is filed
  with an advisory instead of refused.
- Hooks start Python with `-S`, from the interpreter recorded in
  `<helm home>/_global/.state/hook-interp`, and use `gnutimeout` when it is on
  `PATH`.
- With `HELM_CHAT_NODE_BIN` unset, `helm chat node up` uses the binary it
  recorded, not the default search.
- The session catalog writes its caches under `HELM_CACHE_DIR` when it is
  set.

#### New refusals

- **Whole suites, in helm's own repository.** `helm gate run` refuses a
  whole suite in a lane room without `--lane-suite --why TEXT`, a whole suite
  on a tree whose green receipt binds it, and a rerun of a red tree without
  `--again`. `--lane-suite` without `--why`, `--sliced` in a compose room or
  beside a `train...` label, `--sliced` with `--serial`, and `--timings` on a
  run that is not sliced or with `--box` exit 2.
- **Lands.** Every land door refuses a sliced receipt. After `helm gate
  provenance --activate`, every land door also refuses a receipt that no
  authenticated door placed, except in a project that declares its own gate
  command.
- **Gate declarations.** A project's declared gate command that names one of
  helm's diagnostic runners (`helm/gateslice.py`, `helm/gateshard.py`) is
  refused, so a diagnostic run can never mint a landing receipt.
- **The argument guard** refuses, in every project where helm's hooks are
  installed: a command that prints the environment, a process's environment,
  a credentials file or a secret-looking variable; a delegate's verdict-class
  write without a grant; and a `helm store revise` statement that holds a
  command substitution. In a repository under the full ("rail") guard, it
  refuses a working-tree git command in the shared checkout.
- **`helm train --apply`** refuses when the local trunk differs from the
  declared remote trunk or the remote cannot be read. `--max-behind` below 10,
  or not a whole number, exits 2.
- **`helm lr compose`** refuses a closed row that still reads READY.
- **Retracted rows.** `helm dispatch verdict`, `helm dispatch cancel`, `helm
  lr close`, `helm lr retire` and a second `helm dispatch retract` refuse a
  retracted row and name the retraction.
- **`helm chat node up`** refuses a recorded binary that is gone or whose
  sha256 changed, a record it cannot read, and a binary it cannot hash.
- **The pre-push host-path guard** treats a `gh` answer with no true or false
  `isPrivate` as UNKNOWN, so it scans and refuses. Before, such an answer
  skipped the scan as private.

#### Changed output

- A suite, or a worker of the diagnostic shard runner, killed by signal N
  reports 128+N (143 for SIGTERM), not 256-N.
- The host-path guard's refusal for a visibility it could not read ends with
  `<remote>: visibility UNKNOWN (<why>), treated as public`.
- `helm gate run --plan --json` adds `mode` and `mode_reason`, and for a
  sliced run `slice` and `workers`.
- `/api/board` carries `waits_collapsed` beside the live waits, and each
  kanban's lanes carry an on-main count line and the collapsed lines. The
  web server rebuilds its saved board once after the upgrade, because the
  saved shape changed.
- `helm dispatch list`, `helm dispatch triage`, `helm lr list` and `helm lr
  show` print RETRACTED for a retracted verdict.
- After the provenance activation, a land door's success text ends with the
  door that placed the receipt, or `provenance: unauthenticated-no-door`.

#### Project infrastructure

- helm's repository now tracks `.claude/settings.json` with commit and PR
  attribution turned off, so a cloud session that starts on a fresh clone adds
  no AI trailer. Everything else under `.claude/` is ignored.
- CONTRIBUTING.md has a new section, "How a change is tested": focused rounds
  and the tree-wide audits while a change is built, and one serial whole
  suite at the land gate.
- The slice runner's audit found test modules that left process state or
  module data for the modules after them. Each of those modules now restores
  its own.

### Thanks

Thanks to ember arlynx ([@emberian](https://github.com/emberian)) for dregg,
which signs helm's chat.

## 0.3.0 — 2026-09-23

Changes since 0.2.0.

This release is about trusting what helm tells you, and about moving work from
review to your main branch without rows getting stuck on the way. Status
surfaces now print UNKNOWN when they cannot read something, instead of an empty
or healthy answer. The review-and-land loop gains a way to close every kind of
stuck row, and reviewers can now commit the fixes they find. The fleet can now
run across several projects, model families and credential pools, with helm
measuring quota for each. Many commands are also much faster. Some refusals and
defaults changed: read "Breaking or behaviour changes" at the end before you
upgrade.

**dregg.** helm's signed chat runs on [dregg](https://github.com/emberian/dregg):
an external dregg signer signs each post, and a dregg node commits it. This
release runs with a signer built on current upstream dregg. The chat node still
runs the earlier dregg node. Moving the node to current dregg follows in a later
release, after emberian/dregg#86 is resolved.

### Reviewing and landing work

A dispatch (`helm dispatch`) books a build or a review against an exact commit.
A land request (`helm lr`) tracks reviewed work until it reaches trunk (your
main branch).

- **Reviewers can commit the fix they find.** A reviewer of any model family
  that finds a mechanical defect can commit the fix on a branch off the exact
  reviewed commit and name it on the verdict (`helm dispatch verdict --fix
  --patch-tip <sha>`). The land request records both authors, and `helm lr
  show` and `helm dispatch triage` print them.
- **A land request is READY only after an outside approval.** A seat that wrote
  none of the review chain, including patches that reviewers contributed, must
  approve the final commit. If only a contributor approved, the refusal says so.
- **Verdicts wake the seat that must act next.** A FIX, SUPERSEDE, CONCUR (a
  non-blocking endorsement) or undeclared verdict now sends a DM to the author.
  Before, only an APPROVE woke anyone. After an approval, the land message names
  the pipeline state instead of telling every row to land.
- **Conflicting reviews are visible.** A land request reads `READY-CONTESTED`,
  with a `?N` mark, when another reviewer's FIX verdict is bound to the same
  commit on a different review chain. An approval can no longer hide a
  concurrent FIX.
- **Rebased and cherry-picked work is recognised as landed.** helm matches
  patches as well as commit ids. It keeps these proofs on disk and reuses them
  after `helm web` restarts. When git cannot place a commit by ancestry or by
  patch, a content-equivalence proof can close the row, which then shows as
  `LANDED_CONTENT`.
- **Every kind of stuck row now has a way to close.** New close reasons:
  - `carried`: the reviewed work is proven on trunk under other commits.
  - `chain-proof`: a history rewrite deleted the reviewed commits, but an
    approved successor landed the work.
  - `expired`: the verdict authorizes nothing and the work never landed. `helm
    lr expired` lists these rows (dry run by default).
  - `endorsement-moot`: a CONCUR whose commit is already on trunk.

  `withdrawn` now also accepts approved rows whose work will never land.
- **`helm lr retire`** closes rows that nobody can act on any more, one at a
  time or in bulk (`--sweep --older-than 14d`, `--off-frontier`). It refuses
  unless every party is measured as gone. "Could not tell" is a refusal.
- **The rung a re-sent review leaves behind closes when its successor
  lands.** An open earlier rung of the same chain closes as `discharged`. A
  held one closes only when it was held with a clean reading and no findings;
  otherwise the refusal and `helm dispatch list` name the verdict it still
  owes.
- **Closing a row as landed writes the signed land receipt**, which before only
  `helm lr land` did. It never writes a duplicate.
- **New `helm owed`** lists every unanswered FIX verdict and every fix that was
  never sent back for review, oldest first, with the exact next command. **`helm
  owed-push`** sends each owing seat one DM, once per debt, and can run on a
  timer.
- **New `helm reviewers <row-id>`** shows who can review a row now. For each
  seat that cannot, it gives the one reason, so you know whether to wait, repair
  or send the review to another seat.
- **New `helm preread <dispatch-id>`** sends each changed file to a set of
  cheaper reader models before the main review. A different model judges each
  file's findings. Findings that quote code which is not in the diff are
  dropped. The result is reading material, never a verdict.
- **New `helm compose`** combines several reviewed changes into one tested
  branch. It records a manifest for each change and verifies that the result
  can be reproduced. Before `--apply`, it compares the base with the remote and
  lists the trunk commits the base is missing.
- **A helm older than the dispatch ledger does not write rows it cannot
  read.** When a row carries an event kind this helm does not know, its
  writers refuse the row and name the kind and the cure, and `helm dispatch
  list`, `helm dispatch triage` and `helm lr show` print the unknown kinds
  beside the row. Before, an older helm wrote events that every current reader
  dropped. New `helm dispatch collisions` lists each dropped event as LIVE
  (its row is still open or held) or HISTORY, and `helm doctor` warns on each
  LIVE one and counts the rest.
- **New `helm derive`** works out each dispatch row's state from git, gate
  receipts and verdicts. It prints only the rows where that state disagrees
  with the ledger.
- **`helm dispatch list` tells you whose rows it shows.** `--mine`, `--issued`
  and `--to SEAT` combine with `--open`, `--overdue` and `--held`. Held rows say
  whether their work already landed, and a recipient that no longer exists
  reads ABSENT. The footer names each filter and the number of rows it
  excluded. `--open` no longer drops open rows inside a review chain.
- **Dispatches work in any registered project.** A row is keyed to the
  repository its commit lives in, and a seat in that project's checkout uses
  plain `helm`. An unregistered repository is refused, and the message names
  `helm sync` as the fix.
- **Dispatch briefs are stored whole** in a file checked by a digest. Before,
  they were cut at about 3.8 KB. `helm dispatch briefs --cut` lists open rows
  whose brief survives only as a shortened copy.
- **Rows can wait on you.** `helm dispatch hold <id> <reason> --owner-gated`
  marks a row as waiting for your decision, so it no longer shows as a stalled
  build. A reviewer can use `--source-clean` to hold a row with a structured
  reason.
- **`helm lr list` tells more of the truth.** Rows it ran out of time to check
  read `BUDGET EXPIRED` instead of open. Rows whose branch no longer exists are
  counted apart from owed work. Work that reached trunk outside helm is no
  longer shown as owed by a reviewer.
- **`helm lr foldcheck`** recognises a branch whose content is already on trunk
  after a rebase. Before, it told the author to rebase and test again.
- **New `helm stale sweep`** re-checks stalled land requests, overdue
  dispatches and tasks that nobody touched for 3 days. It posts one summary per
  owner and takes no action itself. `helm stale redispatch` sends a finished
  fix back for review.
- A test receipt made before the reviewed commit now counts if the tested tree
  is identical to the reviewed tree.

### Test gates

- **Each project can declare its own gate command.** A project's registry entry
  can declare `gate: {command, protocol}`, for example a `pnpm test` run judged
  by its exit code. `helm gate run --repo <project>` then runs that command, and
  the receipt keeps the full output it judged.
- **Focused runs for fix rounds.** `helm gate run --focus` finds the changed
  files and the test modules that import them, and runs only those. The focused
  receipt lists what ran and cannot be used to land. `--plan` prints the
  selection and runs nothing.
- **New `helm gate window launch`** refuses a second whole-suite run on the same
  trunk head, and names the run already in progress. `--supersede` overrides.
  `helm gate window show --recover` imports a receipt that was lost.
- **A full node is not a red suite.** When the node refuses to start a run
  because it is at its whole-suite limit, short of memory or low on temporary
  storage, `helm gate run` prints NOT RUN and exits 3. Wait for the named runs,
  free the storage, or run on another node. A red suite still exits 1, and
  `helm gate window show` lists such a run as NOT RUN with the command that
  relaunches it. A run is NOT RUN only when its receipt is shown to be absent:
  a receipt helm cannot read keeps the run STRANDED.
- **Gate results are saved before they are announced.** A queued waiter that
  died, a cancellation, a finish, or a failed receipt write is saved to disk
  first. `helm gate list` shows it even when the receipt ledger cannot be read.
  `helm gate run` no longer prints a green result while it exits 1.
- **Failed receipts are easier to diagnose.** They keep the test runner's
  stderr tail, full assertion messages and exception messages. `helm gate show`
  lists the 20 slowest modules of a whole-suite run.
- When `helm gate show` cannot rank the modules of a whole-suite run by time,
  it now says which input was missing and names the tests or modules it could
  not account for, in up to 2,000 characters. Before, it printed the same
  sentence for every cause.
- `helm doctor` compares the gate scripts installed in a deploy directory (the
  `deploy-dir` and `deploy-project` local names) with the committed source they
  came from. For each file that differs it says
  whether the installed copy is ahead of the source, behind it or divergent,
  and it reports a file that differs only in its mode apart. When the source
  checkout cannot be found or read, it warns instead of passing.
- When you check whether failures are yours or trunk's, tests that are new on
  your branch now get an answer instead of UNKNOWN.
- When you check whether failures are yours or trunk's, helm no longer says
  that the failing tests "do not exist on current trunk" unless it has shown
  that each one is gone. Before, a test that ran and passed on trunk could be
  cleared by that sentence. When helm cannot tell, it now says that it could
  not establish that the tests are absent.
- A gate that ran on a snapshot of a dirty tree now says so on every surface
  that shows the receipt.
- The gate supervisor now reaps finished child processes while it runs.
  Before, a long gate collected zombie processes until it exited.

### Seats

- **Bring the fleet back after a reboot.** `helm seat resume --all` sorts every
  registered seat into LIVE, DEAD-PANE, PANE-GONE or UNKNOWN. With `--apply`,
  it relaunches only the seats whose pane died, and each one resumes its exact
  session. It also puts back each pane's tab title.
- **Renames keep working.** After `helm seat rename`, the old name works for 24
  hours (`--alias-hours`). The rename moves the seat's open dispatches, tasks
  and worktree leases to the new name.
- **New `helm seat reassign`** moves a dead or renamed seat's dispatches, tasks
  and leases to another seat. It checks every part of the move before the first
  write.
- **New `helm seat rehome <seat> --home H`** moves a running seat onto another
  credential home. By default it only prints the relaunch line. It refuses a
  home whose token it cannot prove usable.
- **Seats can belong to a project.** `helm seat spawn <project>-claude` and
  `<project>-codex` spawn seats for a registered project, and a project codex
  seat gets its own proxy endpoint and home.
- **Each seat keeps its model.** The model given to `helm seat spawn --model`
  or `helm launch --model` survives resume and `--replace`. `helm seat list`
  shows the model each seat's next spawn will use.
- **`helm seat list` gives a verdict for each seat**: USABLE, DEGRADED,
  UNUSABLE or UNKNOWN, with turn age, pane and work held. A seat of an unknown
  family no longer stops the list.
- **Seats out of vendor credit show UNAVAILABLE**, with family, cause and time,
  and they become available again without your help. When every credential in a
  codex seat's pool is cooling down, the seat shows `BLOCKED_ON_QUOTA` and its
  wake-ups pause until the pool resets, instead of printing an error on each
  wake.
- **helm never submits a human draft.** Before helm presses Enter in a seat's
  input box, it checks the box again. It refuses when it cannot read the box.
  `helm seat composers` finds panes that hold unsent text. `--submit` recovers
  a prompt that helm typed itself and that was never sent.
- A long first message now finishes drawing before helm submits it. Before, it
  could be left unsent, and the new seat stayed silent.
- **New `helm seat unblock`** answers Claude Code's plan-approval prompt on a
  stuck seat after it reads the plan. It never answers a permission prompt, and
  it shows you any plan that pushes, deploys, deletes work or touches
  credentials.
- **Compaction recovery is quieter.** The resume instruction no longer arrives
  twice. Claude Code's automatic compaction no longer triggers a false
  "could not be resumed" alert.
- A seat's chat room now follows its working directory when it is spawned or
  resumed in another project.
- When a seat's harness exits, helm resets the terminal modes it turned on, so
  the pane no longer prints mouse escape codes.
- `helm doctor` and `helm fleet` show the kernel's memory-throttle counters for
  each seat, so a seat stalled at its memory limit is visible.
- **A seat reading that fails shows as UNKNOWN.** When helm cannot read a
  seat's memory pressure, vendor availability or throttle counters, `helm chat
  seats`, `helm fleet` and the web roster show UNKNOWN with the reason. A failed
  read never shows the empty value of a calm, healthy seat.
  `HELM_SEAT_PRESSURE=off` stops helm reading this host's seat memory.
- A new `seat-a-project` agent skill walks an agent through giving a project
  its own credential home, lead seat and codex partner.
- **A seat frozen at a Claude Code prompt is reported.** The beacon pass
  (`helm beacons --post`) reads each Claude seat's own presence record. A seat
  that has waited on a prompt for 3 minutes or more is posted to you and the
  integrator by name, with one push to your phone, once per episode, and the
  pass exits 1. helm types nothing into the seat. A presence record that
  cannot be read shows as UNKNOWN, never as "not stalled", and `helm seat
  unblock` lists these seats too.
- `helm doctor` fails a running Claude session that started before its
  credential home set Claude Code's memory directory, because every memory
  write that session makes stops on a permission prompt until it is
  relaunched. The row gives the exact `--resume` relaunch line.
- `HELM_AUTOCOMPACT_TIMER=0` stops `helm seat launch`, `spawn` and `resume`
  from installing the autocompact timer. Use it where another scheduler runs
  `helm seat autocompact --once`, or where a process must not change the
  host's scheduler. The seat verbs then print one note and continue.

### Model families, credentials and quota

- **OpenRouter is a seat family.** helm makes one proxy route per model, turns
  off data collection, and refuses models that charge for prompts or train on
  them. Also new: a second free OpenRouter family, a local Qwen family served
  by llama-server (no key needed; its endpoint is the `qwen27` key of
  `<helm home>/_global/endpoints.json`), and two families that share one
  credential with gemini but are metered as a separate quota group.
- **Proxied families work better inside Claude Code.** Claude Code's built-in
  subagents on those seats are now served by the family's own model instead of
  failing with HTTP 502. A family can map subagent tiers to different models.
- **Verdicts record the model that answered.** Seat rows, the ledger and
  verdicts show the model and provider that actually replied, not only the
  alias. A family's cheaper fallback route is now a family of its own, so an
  answer from the fallback model does not carry the primary family's approval
  authority.
- **Native seats record the model their transcript names.** A native Claude
  seat's runtime record now carries the model from its own session transcript,
  marked SELF-REPORTED, beside proxy seats' MEASURED models. A session that
  used more than one model reads UNKNOWN, and a session before its first
  answer reads ABSENT. The model is recorded when a session resumes or is
  compacted. Existing records and verdicts are not changed.
- **OpenRouter families have a capacity colour.** helm reads each OpenRouter
  key's free daily requests and prepaid balance from the vendor, with reads
  that use no quota, so `helm burn` gives these families a colour instead of
  GREY. A refused or failed read shows as unread, never as zero, and helm never
  sends the key on to a redirected address.
- **Kimi seats keep a smaller context.** A model family can declare a context
  budget below its model's window. Every request re-sends the whole context, so
  the context size sets how fast a limited allowance goes. Kimi seats are
  taught 380k tokens, not the model's 1M, and compact at about 304k. A kimi seat
  that holds at least 100k tokens also starts a fresh session between dispatch
  rows, but only when it has been quiet for 5 minutes, its input box is empty,
  and it owes no open row and holds no claim.
- **Codex is paced by its weekly window.** `helm creds` shows the 5-hour and
  7-day windows for each pooled codex account. `helm proxywatch` posts one
  warning when the pool crosses the ceiling (`HELM_CODEX_WEEKLY_CEILING_PCT`,
  default 90).
- **The codex pool follows Orca.** Change the active codex account in Orca and
  the proxy pool picks it up within one watch pass. `helm seat cred-follow`
  shows or applies the change by hand. It never overwrites a different pooled
  account, and it never re-enables a disabled one.
- **Credential homes are compared with Orca's live copy.** `helm cred list`
  has a FRESHNESS column, so a stale token no longer reads as AGREE. `helm
  launch` syncs a stale home from Orca before it starts, but only when the
  home's own token chain is used up. `helm cred sync-orca` does this by hand.
- **New `helm codex resets`** reads each pooled codex account's reset credits.
  When an account's weekly quota measures zero, it spends one credit, at most
  one per pass.
- **`helm keepalive --ensure-timer`** installs an hourly user timer that keeps
  helm's copies of account tokens fresh. A token that can still refresh now
  reads `due-refresh` instead of `reauth-needed`. helm ships no OAuth client
  identity for these refreshes: it reads it from your installed Claude Code
  (`HELM_CLAUDE_CLI`, else the newest version under
  `~/.local/share/claude/versions/`).
- **New `helm accounts`** records what you pay for: vendor, plan, price,
  billing type, allowance and where each key is kept, never the key itself. The
  web quota tab has a fill mode that edits one account per line.
- **New `helm burn`** gives each model family one capacity colour (GREEN to RED,
  or GREY when not measured) with reasons. `helm burn why <family>` explains a
  colour, and `helm burn declare` sets a worse one until a time you give. `helm
  burn burst` says when a seat's credential will expire unused quota before its
  window resets.
- **An account helm cannot read no longer makes a family's colour worse.**
  `helm burn` counts unread and stale accounts at the favourable end. If the
  colour then depends on them, the family reads GREY and the repair is named,
  instead of a colour computed from the readable accounts alone.
- `helm doctor` names a credential whose helm copy has given no readable quota
  reading for longer than the 8-hour token lifetime, across two or more
  probes. It says whether the fix is a sync or a new login, and whether the
  current seat is serving turns on that credential, which means the account is
  fine and helm's copy is stale.
- **New `helm route <kind>`** says who should take a review, build, verify,
  delegate, research or council task. The answer comes from the capacity
  colours and your stored rulings, and each line cites its rule.
- **New `helm proxy-usage`** records per-request usage from each proxied seat.
  `helm attribute --by seat` shows which seat and model spent a pooled
  account's quota. Data that is unread or partly lost shows as UNREADABLE or
  PARTIAL, never as zero.
- `helm creds` now says why an account is unknown (re-auth needed, an HTTP
  error, a network error, no credentials) instead of leaving it blank.
- `helm doctor` compares the accounts you declare with the accounts that are
  actually wired, and names missing, extra, disabled and expired accounts.
- New credential homes get the same skills folder and MCP servers as your
  default home. Before, seats on them started without some MCP servers and
  reported no error.
- **The proxy health check no longer marks healthy families as down.** It
  accepts gemini replies with empty thinking, `" OK"` with extra spaces, and
  reasoning models that use the whole check on thinking. A local proxy cooldown
  is no longer labelled an upstream outage. A content-filter refusal is named
  `CONTENT-FLAGGED` instead of `UNKNOWN`.
- `helm seat doctor --ensure --quiet` prints only rows that need attention, and
  after a laptop suspend, `helm doctor --ensure` restarts the family proxies.

### Chat and waking agents

A seat is woken by a small background waiter, `helm chat wait --follow`, that
watches for messages addressed to it. `helm beacons` reports on these waiters.

- **Waiters are protected.** Running `helm chat wait --follow` again keeps the
  live waiter and prints its PID. To replace it, use the new `--replace` flag.
  A subagent can no longer start, replace or kill its parent seat's waiter.
- **"Deaf" seats are detected.** `helm beacons` reports `DEAF-IN-EFFECT` when a
  waiter and its agent are alive but an addressed message stays unread past a
  grace period. With `--post`, helm nudges that seat once. The Stop hook also
  tells a deaf seat about its state on its next turn.
- `helm ready` no longer tells you to wait for a seat's next turn to re-arm its
  waiter, because a turn does not re-arm it. The repair line names `helm seat
  resume <seat>` for a seat with no pane, and says that a seat whose pane is
  alive can only be re-armed from that pane.
- **The tool-call hook delivers what is addressed to the seat.** At each tool
  call a seat gets its @mentions, replies and reactions to its rows, DMs, @all,
  and the owner's plain rows in its home room. Plain rows from helm's own
  subsystems (proxy health, gc summaries, beacon notes) stay in the room for
  `helm chat read` and are not pushed. One registry of machine senders decides
  what counts as a subsystem, and the per-turn hook's arrival check reads the
  same registry. A hook payload is read whole, and one that is empty or does
  not parse delivers nothing, so no message is used up without being shown.
- **A beacon cannot arm into a filter that holds lines.** `helm chat wait
  --follow` refuses to start when its output is piped into a filter that is
  not proven to pass each line at once, such as `cut`, `head` or plain `sed`,
  because the filter can keep a wake line until the waiter is stopped. `cat`,
  `tee`, `sed -u`, and `grep` or `rg` with `--line-buffered` pass.
- **Delivery receipts.** A post with @mentions reports each recipient as
  JOINED, ABSENT, UNKNOWN or MALFORMED. `helm chat receipts <broadcast-id>`
  shows, for each recipient, whether the post was delivered and whether a wake
  was tried.
- **The chat log to disk no longer stalls.** One bad room had stopped the flush
  for every room. Two failed flushes in a row now post an alert.
- `helm chat pending` now marks a mention that no seat answers, a roster that
  cannot be read, and a message sent before its recipient joined, as three
  separate states. It also says that it counts your own messages that are
  still waiting. `helm chat catchup --including-mentions --apply` now sets a
  backlog aside. Before, it reported a failure on every run.
- `helm chat read --room X` now tells a room that does not exist or cannot be
  read apart from an empty room.
- `helm chat wait` and `helm meld recv` with a timeout now return at that
  timeout. Before, they could wait up to one more poll interval past it.
- **The chat directory holds rooms, not old debris.** Every delivery hook lists
  the whole chat directory, and on one measured host it had grown to 108,177
  entries for 468 rooms. New `helm chat retire-rooms` (dry run by default;
  `--apply`) archives meld rooms with no post for 7 days (`--idle-days N`) to
  the journal, and a reboot does not bring them back. `helm gc` now also
  removes per-cursor lock files that nothing uses and the cursors a session
  left under a seat it no longer runs. `helm doctor` warns above 200 entries
  per room or 20,000 in total, and names both commands.
- A signed chat send that times out is reported as UNKNOWN and is never sent
  again, so a message is not posted twice.
- **A seat never signs as someone else.** Chat posts and reactions choose their
  signing key through the same identity check as coordination turns. A seat
  whose environment carries another profile, such as the owner's profile
  exported by a shell startup file, posts unsigned and is stamped
  `identity_conflict`; while the roster cannot be read, it is stamped
  `identity_unreadable`. Its transport status reads DEGRADED, and `helm hooks
  install` and `helm seat launch` list its pane as posting unsigned. An
  explicit profile still wins.
- **New `helm telegram`** makes Telegram a two-way channel to you. Alerts that
  must reach you when the fleet is unreachable, decision cards and provider
  outages go to your phone, and your replies come back into chat.
- **The local MCP endpoint (`helm mcpd serve`)** gains `chat_read` and a
  `chat_post` that signs each post as the seat that owns the caller's token.
  `helm mcpd token` creates a token for the calling seat only.
- Chat no longer creates a lock file per read cursor that could never be
  deleted. `helm gc` can remove the ones it can prove nobody holds.
- `helm chat node status` shows when the signing node cannot fund message
  grants, and `helm chat node refuel` (dry run by default) moves funds to it.
- **The chat faucet warns before it runs dry.** Below five grants
  (`HELM_CHAT_FAUCET_LOW_GRANTS`), `helm doctor` warns and `helm chat node
  status` says FAUCET LOW, with the refuel that restores it. The first LOW
  reading posts one wake to the integrator and to the seat that
  `HELM_CHAT_NODE_OWNER` names; a refilled faucet re-arms it. helm tops a
  cell up only after the node refuses a send for its fee, so fee-free chat
  turns no longer spend grants. A send whose funding the faucet refused
  before it was submitted reads as not sent, never as UNKNOWN. Without
  `--amount`, `helm chat node refuel` leaves two fees in the source cell, and
  it settles an UNKNOWN move from the two balances when they show whether it
  committed. On a node where chat turns carry no fee, a cell at zero is
  healthy and is not reported.
- **`helm chat node up` waits for a slow node.** A node built on current dregg
  runs its verified runtime's start-up before it answers (about 150 s
  measured). `up` now waits up to `HELM_CHAT_NODE_BOOT_WAIT_S` (default
  600 s), stops at once when the node's process exits, and names the cure for
  each known refusal. A node that runs past that wait without answering reads
  `hung` in `up`, `helm chat node status` and `helm doctor`, and a node that
  was stopped cleanly reads down, not refused. `helm chat node status` also
  says when the node binary is older than its source, as it does for the
  signer.
- **New `helm chat node prepare`** is the node unit's start step. It restores
  the node's chain descriptor from the snapshot beside helm's state, or makes
  one around the saved node key, and it never gives a live node a new key.
  The data directory is built beside the target and renamed into place, so
  `--data-dir` must not be a mount point.
- `helm cell` passes only `join` and `send` to the signer, the only verbs it
  serves. helm finds signer profiles where the signer does:
  `$DREGG_HOME/profiles`, else `~/.dregg/profiles`.
- The rogue-compute and silent-drop alerts address the integrator that the
  roster resolves (`HELM_INTEGRATOR_SEAT`, else `helm-integrator`). Before,
  they named a fixed seat that a new fleet does not have. When no integrator
  resolves, the alert still posts and says why nobody was addressed.

### Hooks and injected context

- **Hook output is much shorter.** The installed hook command is now a short
  call to `bin/helm-hook` instead of a long inline script that Claude Code
  printed on every blocked stop. One blocked stop went from 4,904 to 389
  characters. Each hook line says what happened and the one next action.
- **Hooks keep to their time budgets.** The per-tool-call hooks now finish
  inside 2 seconds (argument guard median 0.54 s to 0.09 s, chat delivery
  1.28 s to 0.35 s). The Stop hook names the check that uses its time, runs all
  of its checks under one deadline, and `helm doctor` reports slow stops.
- **New `helm hooks latency`** reports hook timings (p50, p95, timeouts), with
  `--since` and `--until`. A failed hook now says whether it timed out, crashed
  or exited with an unknown code.
- **`helm hooks status` reports whether installed hooks are current,** not only
  whether they exist. Missing and changed hooks are shown apart. The installer
  recognises a helm entry that you wrapped in `timeout`, `nice` or `env`, and
  does not add a duplicate beside it.
- **New `helm hooks install --project DIR`** installs helm's hooks for one
  project only, in `DIR/.claude/settings.local.json`.
- Seats of other model families launched by helm now get the full hook set,
  including per-turn injection, handoff and resume.
- A new SubagentStart hook (`helm saguide`) gives each subagent helm's working
  guide when it starts. Before, subagents started with no guidance. The guide
  includes two review rules: a fix or a review checks every other place that
  asks the same question and names them in its report, and a reviewer commits
  the fix for a mechanical finding in its own worktree, off the exact commit it
  reviewed, and returns that commit with its verdict. Only subagents that can
  build get it: read-only types such as Explore and Plan get nothing.
- **Injected context follows who started the turn.** The per-turn hook tells a
  typed turn from a peer's wake, a hand-back, a background result, a machine
  broadcast and a Monitor expiry, and gives each its own size cap (900 bytes
  for a typed turn, 150 to 200 for machine notices, which get only the lines
  routed to them). An empty, duplicate or replayed notice injects nothing and
  does not load the store. The pinned rules and the WHO profile wait for a turn
  that someone will read, and correction and owner-feedback reminders fire on
  typed turns only. A turn over its cap shortens its long lines instead of
  dropping any. A store entry names the turn it answers with a `route:<id>`
  keyword, and `helm inject --moment-report` shows, per route, what was
  expected, detected and delivered.
- **A relevance re-rank for injected knowledge, off by default.** `helm
  relevance` can score each turn's candidate knowledge lines, with a local
  scorer service or, for projects that allow it, an outside evaluator. It does
  nothing unless `<helm home>/_global/relevance.json` sets a mode. `shadow`
  scores every turn and records what it would keep, without changing one
  injected byte; `live` re-ranks the lines when a score arrives inside a
  bounded wait. A project's turns go to the outside evaluator only after
  `helm projects residency <name> may-leave-lan` is set for it. Every other
  case, an unreadable registry included, reads `lan-only`, and a turn that
  looks like it holds a secret is never sent out.
- **Injected knowledge is scoped by project.** A seat gets fleet-wide entries
  and entries about its own project. `helm store rescope <id> <project|fleet>`
  and `helm reflex rescope` set the scope, and `helm store counts` shows it.
- **See what helm injects.** `helm injectbudget` shows what share of a seat's
  context its hooks injected, by hook. `helm fixedtext` sizes the instruction
  files each checkout loads. `helm configs injection` and the web Config pane
  show what each turn injected, entry by entry, and you can demote or rescope
  an entry with one click.
- **Typed store additions.** `helm store revise` queues a correction to a live
  entry. `helm store gloss` sets the short line that is injected in place of a
  long statement. `helm store gates` injects a rule's preconditions with it.
  `helm store doctor --fix` repairs malformed keywords. `helm store confirm`
  records who confirmed an entry.
- Pinned rules that do not fit the per-turn budget are named instead of
  dropped silently.
- **New `helm friction`** counts how often each guard refused each seat. `helm
  friction dial`, also on the web home view, sets how many refusals in a day
  let a seat stop and repair that guard.
- **New Stop-hook checks.** A turn cannot end by saying code needs a rewrite
  unless it does the work or names where it is tracked. A stop is blocked when
  two live worktrees changed the same file, each passed its own gate, and no
  whole-suite run has tested them together (`HELM_STOP_GUARD_SEAM` controls
  this). Advice about repeated review rounds now tells a repeated finding apart
  from a new defect each round.
- **Advice arrives with the act.** The argument guard gives one short line,
  once per context, right after an act it is about: `git --stat` piped to a
  search, a pane send, a push, PR or issue on a repository you do not own, a
  new source module, and others. These rules no longer ride on every turn's
  injected context.
- Advisory hints from the argument guard now reach the agent. Before, they
  went only to a debug log.
- An unreadable state file no longer reads as clean. A named pipe in place of
  a state file no longer hangs `helm doctor` or the stop hook.

### Web cockpit

- **The ten tabs are grouped into Board, Chat and Fleet.** Nothing was removed,
  and old links and bookmarks open the same panel.
- **The Board overview was rebuilt.** Each card names the command it mirrors
  and its age, and clears old values after four missed polls. New cards show
  who owes the next move and which replies mention you.
- **The work tab** holds your decision queue and the task backlog. You can
  decide, comment on a decision card, and comment on a task from the page. The
  backlog opens with a totals line (P0 to P3, unranked, age of the oldest P0
  and P1) and filter chips.
- **New scheduler view** groups every open land request by who it waits on
  next.
- **New burn-down section** shows what the fleet owes, oldest first. A failed
  read shows as a named failure, never as "0 owed".
- **The pipeline card no longer shows UNREADABLE on a healthy system.** After a
  server restart, the last view is loaded from disk while it rebuilds, but only
  if nothing it was built from has changed. Several review rounds of one piece
  of work now show as one box.
- When a Board section's source stops answering, the section shows as
  unavailable, with the reason and the age of its last good reading, instead
  of showing that reading's numbers as current. The count of seats able to
  work waits until the credit flags are read, so a seat out of credit is never
  counted as able.
- The Lands card now works when trunk only moves by fast-forward.
- The ledger's turn detail (`/api/ledger/turn`) now shows the node's verdict
  on a turn (accepted, rejected, pending or unknown). It had asked for a route
  that no node serves, so it always answered unavailable. On a node that
  serves neither the verdict route nor the anchor route, it says which routes
  were missing.
- **The web server is sturdier.** An unhandled error returns HTTP 500 with a
  JSON body. A stalled client gets 408. Bursts of connections are queued
  instead of refused, and a closed tab no longer prints a stack trace.
- The task comment button works again. It had sent the wrong request and shown
  a 404.
- **The web chat puts no one's name on your posts.** The name box holds only a
  name you type. When it is empty, your posts, replies, reactions and seat
  messages carry the owner name helm resolves: `owner_name` in the `host`
  block of `<helm home>/_global/registry-authored.json`, else the first word
  of git's `user.name`, else your login. The empty box shows that name, and
  your own rows are marked with it. When the server cannot resolve a name, the
  box reads "owner" and no row is marked. Before, the page filled in one
  person's name.
- `helm doctor` lists running `helm web` servers, so forgotten servers are
  visible.

### Tasks, projects and your time

- **Tasks belong to projects.** New rows record their project, and `helm task
  list` shows the current project and says how many rows it left out.
  `--project NAME` and `--all-projects` select others.
- **Priorities.** `helm task add --priority P0..P3`, a rank column in `helm task
  list`, and `helm task triage` to rank rows in bulk (dry run by default, never
  assigns P0). `--continues <id>` groups sub-tasks under a parent story.
- **New `helm task standdown <id> <reason>`** parks a task, with an optional
  expiry, so idle seats are not offered it.
- **New `helm task takeover`** hands a build task to a successor, but only when
  the current holder is measured as hung, starved or idle with an expired
  claim.
- **New `helm task mirror`** copies agents' own task lists from their harness
  into the shared task ledger, without duplicates.
- **Project lights.** `helm projects state <name> green|yellow|orange|red`
  sets a project's light with a reason, and it overrides the scanned status.
  In an orange or red project, new claims and dispatches are refused.
- **`helm projects forget`, `restore` and `repoint`** archive a registration
  whose path is gone, or move a registration to a new path. They are dry runs
  by default, and a repoint can be undone.
- **New `helm away` and `helm back`.** While you are away, each clean stop
  shows a short progress chart: what waits on you, how old it is, and where the
  work is.
- **Away mode from the web.** The Work page has an away mode card. I'm away
  and I'm back write the same flag as `helm away` and `helm back`, so the card
  and the terminal always agree. The card also keeps one fleet notice: your
  words to every agent, up to 240 characters, with one preset for working
  through helm chat and dispatch while you are out. Nothing is woken: no chat
  row is posted and no seat is mentioned. Each seat is told once, at its next
  working turn, and again only when the flag or the notice changes. New `helm
  away status` reads both and writes nothing, and `helm back` reminds you when
  your notice is still showing. A flag set from a terminal records who set it,
  and seats are told when it was not set from one of your doors. A flag or
  notice that cannot be read shows as UNKNOWN, never as "here" or "no notice".
- The "WAITING ON YOU" part of `helm brief` now has two lists, fleet health
  and the requests that agents filed for you, each with its age.
- **New `helm ownership census`** finds open rows held by seats that are no
  longer working.
- Every registry writer (`helm sync`, `helm projects repoint` and the others)
  keeps the authored project fields it does not know, so a field that one
  helm version adds is not erased by another version's write.

### Repository guards and worktrees

- **A light "leak" guard for any repository.** `helm work install-guard
  --profile leak` refuses commits that add bulk data: blobs over 1 MiB, content
  that identifies as an export, dump or archive, and files full of e-mail
  addresses (addresses at reserved names such as `example.com` or `.test` do
  not count). An exception is declared in `.gitattributes`
  (`helm-bulk=quotes` marks a file that only quotes a dump header). The guard
  also checks merge commits, and `helm doctor` names repositories without a
  guard.
- A guard whose installed scanner is older than your helm now says so when it
  refuses.
- **The pre-push host-path guard reads only what the push adds.** It asks the
  push destination which branches and tags it has (`git ls-remote`), and
  scans only the objects those do not already hold. It never trusts a local
  tracking ref. It narrows the scan only when it can prove that the push goes
  where it asked: a custom receive-pack, a `pushurl`, a `pushInsteadOf`, an
  unreadable `git push` command line, a first push or an unreadable answer
  makes it scan everything the push can reach. Its diagnostics name a
  configured remote or "a URL remote", and never print the push URL or path, a
  credential, or git's own output.
- The guard's list of paths that must never be committed can be extended on
  one machine. `HELM_NEVER_TRACK_LOCAL` (default
  `~/.helm/_global/never-track.txt`) holds one path prefix per line, so a path
  whose name is private is not named in the repository.
- `helm work install-guard` without `--apply` reports how the installed hooks
  differ from the expected ones.
- The full ("rail") guard refuses a commit that removes a top-level Python
  function, class or assignment that another file still uses. It resolves what
  each name is bound to, so a function of the same name imported from another
  module does not count.
- **Worktree cleanup is safer.** `helm work gc` and lease release never move a
  branch that another worktree has checked out. They refuse a worktree that is
  in the middle of a rebase.
- **A landed lane shows as landed.** A land releases no lease, so `helm work
  list` now prints a line under a held room whose work is already on trunk,
  with the release command. The web Work page draws that lane as landed, and
  its lanes line says how many running lanes are landed with the lease still
  held. A lane whose branch was reset back to trunk after it committed is not
  landed, and helm never offers it for release, because its work then lives
  only in the reflog that a release deletes. A landed lane whose room has
  uncommitted changes says DIRTY on every surface.
- **Claims never hang behind a stopped process.** `helm work claim`, lease
  release and refresh, `helm chat claim` and the session rebind on resume wait
  at most 10 s for the claims lock. Then they refuse, and the refusal names the
  process that holds the lock, its state (for example STOPPED) and how to free
  it. A refused release keeps its lease.
- **`--ttl` takes units.** `helm work claim`, `helm chat claim` and `helm
  multiplayer presence` read `--ttl` as seconds, bare or with one unit (`3600`,
  `3600s`, `90m`, `4h`, `1d`). Any other spelling is refused with exit 2 and a
  line that names the accepted forms. Before, a unit crashed the command.
- **New `helm work release --superseded REASON`** retires a worktree whose work
  will never land, and keeps that work.
- The Stop hook's lease advice no longer prints a ready-to-paste release
  command for a lane lease. It points to `helm work list`, which carries the
  lane's token. When any of its checks on the lane's room fails, it reports
  UNKNOWN and lists the checks it did make. A lease that strands no work, such
  as a port lease, still gets its `helm chat release` command.
- Inside a linked worktree, helm reads trunk from the shared checkout. This
  fixes repositories whose trunk is not `main`.

### Performance

All numbers below are measurements stated in the commits.

- `helm lr list` can answer from the web server's built view: 18,621 ms cold,
  3 ms warm. `helm lr list --json` uses the same fast path (90 s before,
  about 0.3 s after).
- Building the land-request view starts far fewer git processes: 248 s went to
  80 s over four changes. A cold land-request board went from 138.8 s to
  77.3 s.
- `helm dispatch list` caches git answers about fixed commits across processes
  (24.9 s to 5.1 s warm).
- `helm seat list` 52.9 s to 31.3 s, `helm chat seats` 4.2 s to 1.5 s, and the
  `helm doctor` trunk check 25.4 s to 2.3 s. A full rebuild of dispatch state
  went from 33.0 s to 23.5 s.
- Checking stored gate receipts at stop time no longer reads the whole ledger
  once per receipt: 109.2 s to 0.162 s on 3,553 receipts. One measured stop
  went from 4.3–8.9 s to 2.9–3.9 s.
- The Stop hook's delegation check reads the process table once per stop
  instead of once per held lane lease: 0.356 s to 0.020 s with 18 leases.
- The per-turn brief's board summary is time-bounded (7.07 s to 3.12 s cold,
  same counts).
- Web: the sessions page 10.9 s to 0.09 s per 200 rows; the configs scan
  6.63 s to 0.80 s; a cold readiness check 46 s to 8.7 s. The task list
  payload went from 4.4 MB to 2.1 MB and the quota history from 766 KB to
  449 KB, with the same rows. The board no longer rebuilds all the time.
- `helm dispatch list` works out review-chain cycles once per listing instead
  of once per row. The per-row pass grew with the square of the ledger and
  took minutes on a large one.
- Dry-run closes, which the land-request board asks once per row, no longer
  take the dispatch ledger lock, so dispatch sends and verdicts do not wait
  behind a board rebuild. A note added to a dispatch row holds that lock only
  while it writes.
- Reading the project registry no longer takes the write lock, so helm
  commands no longer wait behind the web console.

### Platform and installation

- **Immutable releases (preview).** `scripts/deploy.py` builds a read-only helm
  release from one committed tree, and refuses when there are uncommitted
  changes. `--project` puts a release inside another repository, in a
  gitignored path. This is not yet the default install.
- **New `helm upstream-watch`** reads each new Claude Code release once a day.
  It builds a bounded summary of what changed (settings, flags, environment
  variables, hook events, models, bundled skills and the release notes), asks
  one headless Claude Code run to propose helm changes that quote that
  evidence, and asks a second run to refute each proposal. Each proposal that
  survives becomes a task, and one chat digest reports them; nothing is posted
  when none survives. The runs use your own Claude Code login with read-only
  tools. `--dry-run` files and posts nothing, `--install-timer` installs the
  daily timer, and `HELM_UPSTREAM_WATCH=0` turns it off.
- **Facts about one host are files, not source.** A local model endpoint and
  the credential homes that `helm doctor --probe-memory` must never borrow are
  set in `endpoints.json` and `probe-reserved-homes.json` under
  `<helm home>/_global/`. helm reads them on every call, so an edit needs no
  restart. A missing file configures nothing, and a file that does not parse
  is an error. A family whose endpoint is not configured is unavailable, and
  `helm seat add` names the file and the key to set.
- **This host's own names are settings.** `<helm home>/_global/local-names.json`
  holds the few names helm needs from one machine: the tool helm replaced there
  (`predecessor`, whose `<NAME>_*` variables and directories are then
  honoured), the provider name of the local Qwen pool row, the deploy
  directory and project that `helm doctor` checks gate scripts against, and a
  few more. Every key is optional, and without it the behaviour is off or
  neutral. `helm doctor` warns about an unreadable file, an unknown key or a
  value of the wrong shape. `docs/local-names.example.json` shows every key.
- When you run helm from inside a helm checkout that is behind `origin/main`,
  it prints one line that says how far behind the checkout is and how to
  bring it up to date (`HELM_NO_TREE_WARNING=1` turns it off). It never
  fetches.
- The agent skills that ship with helm now use helm's own commands throughout,
  and they point only at skills that ship with them.
- On macOS, `helm chat` no longer crashes for lack of `/dev/shm`. It uses a
  private per-user temporary directory instead. macOS is still not a supported
  platform.

### Breaking or behaviour changes

#### Required on upgrade

- **Run `helm hooks install` again.** Installed hooks from 0.2.0 read as STALE,
  and the reinstall replaces them without duplicating them. The new commands
  call `bin/helm-hook`, and the Stop hook timeout goes from 5 s to 20 s.
- **Per-turn credential backup and repair hooks are retired.** Run `helm cred
  switch-guard --install`, which now removes them. Then run `helm doctor
  --ensure` on your own schedule, because it now does the backup and repair.
- **Re-run `helm work install-guard --apply`** on guarded repositories. Merge
  commits are checked only after you do.
- **`helm dispatch retip` on a rebased build branch needs a declared trunk.**
  Set `helm.trunkRef` and `helm.trunkRemote` in the repository's git config.

#### Changed defaults

- **The local test-suite guard is optional.** One hook, the local test-suite
  guard, runs an executable that helm does not ship. With
  `fab-suite-pretooluse` on `PATH` or `HELM_SUITE_GUARD` set, it installs and
  is required: if it then stops resolving, `helm hooks install` reports a
  SHORTENED install and exits nonzero, and `helm launch` and seat launches
  refuse. With neither, every other hook installs and the guard reads "not
  configured (optional)".
- On turns no person started (machine broadcasts, background results and
  Monitor expiries), the per-turn hook injects only the lines routed to that
  kind of turn, and the pinned rules wait for the next working turn. Empty,
  duplicate and replayed notices inject nothing.
- A seat whose environment names another signing profile posts unsigned
  instead of signing with that profile's key.
- The `<NAME>_*` variables and directories of the tool helm replaced are read
  only when `local-names.json` declares it as `predecessor`.
- `helm whoami` finds an external user profile at
  `$HELM_PROFILE_HOME/user-profile/profile.json`, else in the first adopted
  home that carries one. The variable it read before is no longer honoured.
- `--ttl 0` is now refused, as are a sign, a fraction and any unit other than
  `s`, `m`, `h` or `d`, on `helm work claim`, `helm chat claim` and `helm
  multiplayer presence` (exit 2).
- Kimi seats are taught a 380k-token context, not the model's 1M, and a quiet
  kimi seat starts a fresh session between dispatch rows. A running seat gets
  the budget at its next launch.
- New codex seats default to `gpt-6-astra` with a 220k input ceiling. Pass
  `--model gpt-5.6-sol` to keep the old model and its 320k ceiling.
- Seats on proxied families start with Claude Code's Artifact tool, skills and
  forked subagents turned off. The Workflow tool is allowed, capped at 4
  agents. helm-launched seats also start with Claude Code's feedback commands
  turned off.
- `helm launch` no longer sets the git author and committer to the seat name.
  Seat commits use your own git identity, and `helm doctor` warns when a seat
  name is set in the environment. The full ("rail") guard profile refuses AI
  authoring lines in commit messages: a co-author, `Assisted-by`,
  `Generated-by`, `Written-by` or `Authored-by` trailer that names a model, and
  a "Generated with Claude" footer (`HELM_TRAILER_REFUSE=0` turns this off).
- A flagless `helm work install-guard` on a project repository now installs
  the leak profile, not the full rail profile. Pass `--profile rail` to keep
  the full guard.
- `helm rogue` runs with the silent-drop timer. By default it stops heavy test,
  build and install processes that run outside helm's approved runner, 60 s
  after it alerts. Set `HELM_ROGUE_KILL=0` for alerts only.
- The hooks that make a session a fleet seat (chat join, chat delivery, the
  stop guard and delegation stop) now run only in sessions in the same project
  as the helm checkout. Other projects keep per-turn injection, handoffs and
  resume. This does not apply when helm's own checkout is not a registered
  project. The subagent guide runs in every project, but gives helm's brief
  only in helm's own project.
- At a tool call, a seat no longer receives plain room rows from helm's own
  subsystems. They stay readable with `helm chat read`, and rows from people
  and seats, and anything addressed to the seat, still arrive.
- Read-only subagents (Explore, Plan and similar) no longer get the subagent
  guide.
- The relevance re-rank is off unless `relevance.json` turns it on, and a
  project's residency is `lan-only` unless you set it.
- Store entries with no recorded project that name project-specific items now
  reach only their own project. Use `helm store rescope <id> fleet` for wider
  entries.
- `helm task list` shows the current project by default.
- helm no longer asks the chat faucet for funds before a signed post. It tops
  a cell up only after the node refuses the post for its fee, and sends it
  once more.
- `helm chat node up` waits up to 600 s for the node to answer, not 30 s
  (`HELM_CHAT_NODE_BOOT_WAIT_S`).
- helm reads signer profiles from `$DREGG_HOME/profiles`, else
  `~/.dregg/profiles`. `DREGG_PROFILES_DIR` and `HELM_ROSTER` are no longer
  read.
- The pre-push host-path guard decides what a push adds from the
  destination's own list of branches and tags, not from local tracking refs,
  and scans everything when it cannot prove where the push goes.
- The web chat's name box starts empty, and a post from an empty box carries
  the owner name helm resolves. A name saved by an earlier version of the page
  is not reused: type it again to keep it.
- `helm lr close --reason landed` also closes every other open row bound to the
  same commit, except rows with a FIX or SUPERSEDE verdict on it.
- Other projects' rows are left out of the default land-request lists, with a
  one-line count.

#### New refusals

- **Verdicts.** A FIX or SUPERSEDE verdict must name each path that is worse
  than main (`--worse-than-main PATH`). It must also name the reviewer's patch
  (`--patch-tip`) or give a reason for none (`--no-patch-because`). Findings
  that do not make the work worse than main go on an APPROVE, filed as new rows.
- **Dispatch sends are refused** when:
  - the recipient is live but cannot work, or helm cannot wake it (`--force`
    skips the first check);
  - every pooled codex account is past the weekly ceiling;
  - the project's light is orange or red;
  - the commit is a snapshot of a dirty tree;
  - the brief is larger than 32 KB;
  - a review brief tells the reviewer not to edit, and no
    `--read-only-because REASON` is given;
  - `--new-work` follows a round that ended in FIX (use `--supersedes`);
  - a task or dispatch proposes to fork, patch or vendor a dependency without
    answering what happens at the next upgrade (`--posture-na REASON` skips
    this).
- **Rebinding** a dispatch row away from a live seat that is reading it is
  refused unless forced.
- **A dispatch row with an event kind this helm does not know** is refused to
  every writer. The refusal names the kind and says to run the trunk helm or
  fast-forward this helm's checkout.
- **Free-text verbs refuse an unknown leading flag** instead of saving it as
  the text. This covers chat, tasks, dispatch, board, asks, store and others.
  Text that starts with `--` must follow a `--` separator. `helm work` and
  `helm store` verbs also refuse unknown flags; for example, `--base` on `helm
  work claim` had been ignored silently.
- **`helm chat post`** with both a message argument and piped input exits 2.
  A post to @all, @fleet or @everyone must begin with "measured", "inferred" or
  "unverified".
- **Identity.** A seat name alone is no longer enough to act for a seat. It
  must match a rostered session. Names are compared without case, and `helm
  chat join --seat` refuses a name that conflicts with the session's identity.
- **Beacon pipes.** `helm chat wait --follow` refuses to arm when its output
  is piped into a filter that is not proven to pass each line at once.
- **Subagents** cannot start or replace `helm chat wait --follow`. `helm chat
  wait --follow` with its output sent to a regular file also refuses
  (`HELM_BEACON_FILE_SINK_OK=1` allows it).
- **GitHub Actions.** In every project where helm's hooks are installed, the
  argument guard refuses agent shell commands that touch GitHub Actions (`gh
  workflow`, `gh run`, the Actions API, the `.github/workflows` directory)
  unless the command starts with `HELM_ALLOW_GITHUB_ACTIONS=1`. Only the
  command a shell runs counts: quoted text given to `echo` or `printf`, a helm
  verb that posts text, a commit message, a search pattern, a `gh api` read or
  a quoted heredoc that no shell runs is data and is not refused. Write and Edit
  calls whose file path matches are refused with no override. The match is on
  text, so a path with an `/actions/` or `/dispatches` segment, such as an
  ordinary `src/actions/` directory, is also refused.
- **Commands that would end your own turn.** The argument guard refuses
  `pkill -f`, or a `pgrep -f` that feeds a kill, when the pattern matches the
  command's own text. The refusal prints a bracketed pattern that does not
  match itself.
- **AI authorship on GitHub.** The argument guard refuses a `gh pr` or `gh
  issue` body, or a `gh api` body on an issue or pull request, that carries an
  AI authoring line.
- **Claims.** A claim, refresh or release that cannot take the claims lock
  within 10 s refuses and names the process that holds it. Before, it waited
  until that process resumed or exited.
- **Removed names.** The full ("rail") guard refuses a commit that removes a
  top-level Python name another file still uses (`HELM_RETIRED_NAME_SKIP=1`
  skips it for one commit).
- **Your away flag and notice.** The argument guard refuses an agent's
  command that presses the away mode card, that mints the card's owner door
  in code it runs, or that writes the away flag or the notice by hand, in the
  shell or with a Write or Edit call. It also refuses a hand delete of the
  notice. Text that only names them, such as a commit message or a chat post,
  passes. A script that runs as your user can still write them on purpose.
- **`helm cell accept`, `recv`, `heartbeat` and `roster`** are refused by helm.
  The signer serves only `join` and `send`.
- **`helm chat node prepare`** refuses a snapshot that has no `node.key`, and a
  second prepare while one runs (it names the lock).
- **`helm chat node refuel --amount N`** refuses an amount that would leave
  the source cell less than one fee.
- **`helm keepalive --apply`** refuses a grant when it cannot find or read the
  installed Claude Code executable, because helm no longer ships that
  program's OAuth client identity.
- **Tasks.** `helm task claim --force` now refuses; use `helm task takeover`.
  `helm task add` refuses a near-duplicate title unless you pass `--force-new`.
  "UNOWNED" is no longer accepted as an owner.
- **Worktrees.** `helm work claim <label>` refuses when a live dispatch row has
  work under that label that is not proven on the base.
- **`helm compose --apply`** refuses a base that is behind trunk or not proven
  current, unless you pass `--base-behind-ok`.
- **`helm gate run`** refuses to start when temporary storage is low, and on a
  registered project with no declared gate command. Before, it ran helm's own
  test suite there.
- **`helm launch`** exits 1 when a credential home's identity disagrees with
  Orca's copy of the same account.

#### Changed output

- `helm who --json` returns an object with `rows` (the old array) and a `scan`
  completeness block.
- `helm lr list` and the web board no longer print one combined `open` count.
- The web Board overview drops the per-loop in-flight row, the receipt badge
  and the `window_total` and `window_s` response keys.
- The Stop hook no longer prints `helm work release <lane> --lease <token>` for
  a lane lease. Take the token from `helm work list`.
- `helm burn` can read GREY for a family whose colour depends on accounts that
  helm cannot read.
- `helm gate run` exits 3 when the node refuses to start the suite for lack of
  capacity, and its `--json` answer carries `"not_run": "capacity"`. A red
  suite exits 1.

#### Project infrastructure

- helm's own test suite no longer changes the host it runs on: no test
  installs the autocompact timer or runs the host's `systemctl`. Tests that
  slept out real timeouts now wait on the event itself or on scaled bounds.
- The repository no longer has a GitHub Actions workflow. The Python 3.9
  minimum is still declared in the README and `scripts/install.sh`, but it is
  no longer tested on every push across Python 3.9 to 3.13.
- At this release the repository's history was replaced with one commit, a
  snapshot of 0.3.0. The sections below record 0.2.0 and 0.1.0-alpha.

### Thanks

Thanks to ember arlynx ([@emberian](https://github.com/emberian)) for dregg,
which signs helm's chat, and for the macOS support and `helm hooks install
--project` that he contributed to 0.2.

## 0.2.0 — 2026-08-06

- The seat estate is split into focused modules — identity, roster, claims,
  delegation, delivery, join, report, stop-guard, stop-signals, work-offer,
  gate-queue, room advice — plus a seat_* lifecycle family (catalog, paths,
  ports, provisioning, credentials, proxy, launch assets, health, runtime and
  session lifecycle) behind unchanged `helm.seats` / `helm.seat` facades. The
  split carries its own contract tests: setattr fan-out across siblings,
  split-boundary parity, and an honest-presence suite that keeps a seat's
  roster row, its process, and its transcript from ever disagreeing silently.
- `helm gate` — the receipted verification gate. `gate run` executes the
  suite and MINTS a receipt (id, tree, verdict, host) into a receipt ledger;
  a green report with no receipt is not a pass. Concurrency is governed
  per-host by a pane predicate (does this box carry live agent panes), never
  a hostname allowlist; suites route to an external build host when one is
  configured, wrapped in a user-delegated cgroup scope with a fail-closed
  resource guard. Child gates, import receipts and a routing layer
  (`gateroute`) keep probing consent, execution consent and host identity
  separate — an eligibility reading is never a capability.
- The land-request ledger (`helm lr`) grows chain identity end-to-end:
  v3 rows carry a chain root, edges are proven per-endpoint (an absent root
  is unknown identity, not a pass), landing proof falls back from ancestry to
  patch identity, a vanished object scores `absent` only when no reachable
  source holds it, and the close ladder gains the `resolved` door with
  confirmation rounds — approve/supersede polarity only, the door's own
  sentence parsed by the door's own parser. Superseded parents are annotated
  and swept rather than left looking actionable.
- The dispatch ledger learns chains and honest signals: `add` notifies (an
  obligation that tells nobody is a silent net), replay cannot skip
  retroactive policy, spiral detection reads the LAST round's polarity
  rather than counting rounds, cross-family fan-out is measured on distinct
  reviewed tips, and room fences rebind when a walled recipient rebinds.
- `helm beacons` + `helm wiring` — attendance/wake edges as data, installed
  and revalidated by a wiring registry instead of scattered call sites; a
  concurrent pass delivering the same edge twice is the tested-for defect.
- A chat-native council: convened quorums with an epoch-fenced protocol, a
  locked read-modify-write signal registry (two concurrent signals must both
  count), and verdict basis recorded beside every reviewer verdict.
- Chat v2 hardening: an argv guard on every read verb (unknown tokens refuse
  instead of returning scrollback), catch-up and restore journals, DM
  channels, boundary-aware owner-mention resolution shared with delivery,
  and signing identity that refuses a profile/seat disagreement rather than
  signing with someone else's key.
- The hook estate: `resume-turn` (SessionStart re-briefing with adopted-pane
  support), tool whisper (per-toolcall context injection — the layer beneath
  per-turn), a JIT injection ledger whose cooldown measures turns rather
  than context, and a stop-guard that blocks a stop on undelivered mentions
  or held leases.
- `helm proxywatch` — proxy liveness as a tri-state (on/off/unknown, and
  UNKNOWN never authorizes), self-labeling canary probes, a fork watch for
  the proxy binary, and silent-drop detection with cross-family fan-out.
- `helm orca adopt` — adopting externally-launched panes safely: process
  identity is (pid, birth-stamp) typed, never a bare int; the addressing
  ladder refuses on stale, historical or ambiguous evidence instead of
  falling through to a neighbouring pane; an authoritative census with a
  hole in it authorizes nothing.
- Work lanes mature: claims with lease recovery, a stash, lane discipline,
  and a gc that reads renames and facade splits (a 3805-line file shrinking
  to a 156-line facade is not a delete) before proposing anything.
- Owner-surface subsystems: typed task rows (`helm tasks`, numeric ids with
  an origin field — owner vs agent — and resurrection refused without
  witness), the seat todo mirror bridged into the task ledger, a
  writer-per-key board (any seat may APPEND to any key; REPLACING a
  narrative key is the owner's), and fleet notes with headline,
  click-to-detail and a validated goto pointer.
- `helm vcs` — one backend seam for every git spawn, and `landed_state`:
  ancestry asks the wrong question about rebased work, so lane retirement is
  proven by patch identity (`git cherry`) with an independent count
  cross-check; UNKNOWN is never spendable as a verdict. Beside it, shaguard
  documents and trips on sha fabrication — a padded short sha or an invented
  middle is caught before it becomes an announcement.
- The docref citation registry: hex citations in docstrings and comments are
  validated by CATEGORY — a commit sha must resolve from a fresh clone, a
  ledger row id must be vouched by a live ledger, a patch identity must
  recompute from its recorded diff, and a runtime token needs a reasoned
  SKIP entry — enforced by a fast pre-commit rung and the suite both.
- The scanner rungs grow: never-track (files that must never be tracked,
  with reasons that do not restate what they protect), vacuous-assertion,
  hardcode, conflict-marker, foldcheck, clearspan, a deletion rung, and
  hostpath-guard v2 — the push guard now scans the git the push DESCRIBES,
  not the remote it guessed.
- `helm clarity` — the controlled-language advisor over a curated lexicon,
  and the promote gauntlet: three deterministic layers (provenance,
  structure, speech act) deciding which owner messages become durable canon,
  regression-tested against a labelled corpus whose positive controls are
  asserted individually.
- Boxes and storage: a box inventory chain with an optional runtime-discovered
  external CLI (absent is silent, this box alone is a complete inventory), a
  cross-box storage matrix, and scratch shelves.
- Eval instruments: `evalpin`/`evalrun` (premise-checked eval atoms — a
  refuted premise writes stale-and-skipped with evidence instead of burning
  a run), and a mutation matrix script that proves the suite kills
  line-moving mutations, with pycache and committed-state discipline
  baked in.
- The web cockpit is decomposed: `web.py` is now a facade over web_* modules
  and `web_ui/` assembled parts (manifest-ordered shell/views/styles/scripts)
  with per-tab views — home, quota, boxes, sessions, configs, work, chat,
  roster, ledger — SSE, the land board, DM channels and a storage-matrix
  panel; runtime harnesses exercise the client JS against the real server.
- CI and packaging: a GitHub Actions workflow tuned to batch at slice ends,
  `AGENTS.md` (the agent-facing repo contract), `.gitattributes`, and
  `install.sh` relocated to `scripts/` with the README install section
  pointing at it.
- Docs: `VERBS.md` rewritten as the complete verb reference; new design docs
  (ledger-is-index, subsystem serializer, design philosophy, controlled
  language + lanes, dispatch-add contract, orca operations and seam audit)
  and methodology notes (council eval, model-family failover, STE clarity);
  `DEPENDENCIES.md` supersedes `ATTRIBUTION.md`.

- Proxy-seat autocompaction is now operational rather than alert-only. The
  actuator resolves the authoritative `spawn.json` pane identity; mutable Claude
  Code titles, copied launch text, and visible content never authorize input.
  Orca handle remints recover through the exact registered session's live
  pid/procStart -> `ORCA_PANE_KEY` -> `terminal.resolvePane` chain, require one
  connected+writable handle+PTY+worktree inventory match, then atomically repair
  `spawn.json` under its lifecycle lock. One resolver owns injection,
  duplicate-seat reap, dry-run, and `seat where`. Queued `/compact` blocks a
  duplicate, and the flock-serialized latch re-arms only after context drops or
  the session changes, never from elapsed time alone. Repeated terminal 400
  context-overflow loops queue `/clear` once; the SessionStart-bound session
  transition proves completion before the empty session receives its onboarding
  brief. Every launch/spawn/resume refreshes the external 60-second systemd
  cadence through the installed `helm` binary, never an ephemeral worktree.
  Missing, headless, session-unbound, stale, or ambiguous identity fails loudly.
  The CV seam is fleet-complete too: Helm appends all family and instance seat
  transcript roots through `CLUSTERVISION_CLAUDE_ROOTS` on every CV subprocess,
  and the corresponding CV-core multi-root discovery change preserves the one
  recall index across custom `CLAUDE_CONFIG_DIR` homes.
- Homing review round (fable composition + adversarial lenses).
  HIGH closed: the homing prologue's EAGER `os.getcwd()` crashed every
  default chat verb and all three delivery hooks (join/deliver/stop-guard)
  for a session whose cwd was deleted — a pruned lane worktree is routine;
  main handled it, the lane regressed it. `seats.safe_cwd()` fails open to
  None (un-homed -> #main) and every lane-introduced call site (`cmd_chat`'s
  prologue, `helm launch`, `seat._resolve_homing`) plus the adjacent
  same-class sites (`whoname`'s auto-name, the hook join's cwd fallback) now
  resolve through it. LOW closed: the hook seam re-resolves a DERIVED
  pre-resolution against the hook PAYLOAD's cwd (the session's ground
  truth) instead of trusting the hook PROCESS's cwd. LOW closed (adversarial review): `_unlink_seat_state`/`_move_seat_state` match keyed state files at a
  KEY BOUNDARY (`<marker><key>` then `.` or end) — the bare substring test
  let pruning/renaming seat `foo` destroy the delivery ground of a live
  seat literally named foo's key. Documented-accepted LOWs: the catalog's
  glob-empty-root proof-of-absence bound (transcript proof is gc's LAST
  tier behind fail-closed presence/process tiers; loss bounded to roster
  row + cursors, rejoin self-heals) and the explicit-beats-explicit tier
  gap (an operator rehome holds until a pane with a stale explicit
  `HELM_CHAT_ROOM` env restarts — follow-up: rank operator above stale
  explicit env or re-mint launch.sh on rehome).
- Roster GC gets ONE evidence owner (an independent cross-family review, three
  HIGHs closed). (1) Transcript truth is no longer a hand-rolled root list —
  `seat gc` delegates to session's persistence census, which covers helm's
  own seat homes (`~/.helm/_global/seats/**/claude/projects`); the old list
  omitted them, so an inactive-but-fully-persisted proxy seat probed as
  junk. The catalog now also scans `~/.claude-homes/*/projects` so the one
  owner keeps the coverage the deleted list had. (2) The legacy auto-reap
  that rode `roster_report` is DELETED, not fenced: it dropped any stale row
  on presence alone, bypassing every transcript/process guard and the
  dry-run gate — a report is a read; only `seat gc --apply` deletes.
  (3) `--apply` re-runs the FULL keep-evidence probe fresh under the roster
  lock before each deletion (a transcript flushing between scan and apply
  wins); the process probe is same-uid scoped, counts a live
  `HELM_CHAT_NAME=<seat>` environ for rows with no remembered session, and
  any same-uid read failure keeps the row (only a pid proven exited
  mid-scan — ENOENT/ESRCH — reads as absence, so gc never degenerates into
  a fail-closed no-op).
- Home-room scatter, as-prevented (owner mandate: "how they got scattered —
  needs to be as-prevented"). A live roster held THREE `home_room` truths for
  one team — `main` (the spawn mirror defaulted `room or "main"` and
  `write_roster`'s unlabeled seam stamped it *explicit*), `<project>` (cwd
  derivation), `<env room>` (the launch seam) — because four writers each
  re-derived the precedence privately. Now `seats.resolve_homing()` is THE
  one precedence (explicit CLI/operator room > `HELM_CHAT_ROOM` env, honoring
  the seam's `derived` stamp > cwd git-project derivation) and every writer
  (`seats.join`, `helm launch`, `helm seat add/launch/spawn`'s
  `_resolve_homing`, the spawn-register roster mirror) resolves through it;
  `write_roster` is the one enforcement gate and now tracks provenance on
  every path: an UNLABELED `home_room` reads as *derived* (unknown provenance
  takes the weakest tier), and a derived value can NEVER overwrite an
  explicit/operator home — a re-join/resume/mirror never downgrades a
  deliberate choice. The spawn record carries `room_source`, and a room-less
  spawn writes NO home instead of inventing `main` (the SessionStart join
  derives the real one). Plus `helm chat seat gc [--apply]` — the MANUAL
  roster junk pruner (dry-run default, never automatic): prunes only rows
  with no presence beat in the reap window, no transcript for any remembered
  session, and no live process naming one; every probe fails CLOSED.
- `helm todos` — the seat todo mirror: what every agent in the fleet is
  working on right now, without asking it. The recorder gains a todo leg
  that captures the CURRENT list off `TodoWrite` **and** the `Task*` family
  (`TaskCreate`/`TaskUpdate`/`TaskUpdateTODO` — what the live fleet actually
  emits; a create's id is parsed out of its tool_result string) into
  `todos.json` beside `counters.json` — bounded, atomic, no subprocess, and
  walled off behind its own `try` so a broken mirror can never cost the
  counters/command-log/edit-targets that back the stop-whisper. DIGEST +
  PULL by law: `helm todos` / `helm todos --all` / `GET /api/todos` / the
  roster row + `helm chat seats` read it on demand, and PUSH is a
  rate-capped exception — one room line per MEANINGFUL transition (a task
  finished, or an idle seat picking up a new in-progress task), collapsed to
  the latest state, at most once per 5 min per seat, nothing at all when
  nothing materially changed, `HELM_TODO_POST=0` to silence. Never an
  @mention, never a DM, and **never a wake**: every `@` is stripped from the
  posted text, and the row rides the new `ambient` class — a row
  `seats.deliverable()` drops before every wake rule, including the
  home-room rule that would otherwise have handed the line to every seat on
  a `helm launch --room team-x` team. It renders everywhere and wakes
  nobody. The owner's parity surface is the ledger tab's **fleet todos**
  panel; `/api/todos` carries the digest only (item lists ride
  `helm todos --all --json`) and caps unclaimed sessions at the 25 freshest.
- Chat replies + the owner-surface UX pass. `helm chat reply <id|n> <text…>`
  (and `post … --reply-to`) threads a message under a parent by REUSING the
  stable row id — additive `{reply_to, rts, rfrom}` (plus `rtext` only for a
  pre-id parent), no second identity. Signed replies BIND the parent through a
  disjoint algorithm tag (`chat:reply:b2b:` over RS-joined, injectively escaped
  parent fields + text) — never an
  in-band prefix on the plain-post payload, which attacker-chosen text could
  forge into a free re-parenting. Plain posts stay byte-identical, so every
  signed row already on disk verifies unchanged; `payload_for()` is the one
  shape-dispatching recomputer and `helm chat verify` re-derives it (honest
  scope: self-consistency, not remote re-verification — the node still cannot
  disclose a turn's payload). Rendering is one level, a compact one-level style — a
  compact parent quote, a `↩N` count, graceful orphans — in `helm chat read`,
  the journal, and the web panel. Threading now REACHES beacon-wake — the
  original "threading is invisible to the beacon" law was inverted
  (the intent: replying should replace typing an @mention): a reply is
  a direct address of the parent's author, mention-tier, any room, casefold —
  and of NOBODY else; every other row wakes exactly what its text alone would
  have woken (asserted over the full scope matrix). The web chat surface also
  gains: per-channel unread/mention badges
  with last-activity age and dimmed quiet rooms, readable seat rows (age +
  legend, distinguishing-tail truncation), collapse for long agent posts,
  @mention completion from the live roster, an unread divider and
  jump-to-latest.

  Two defects found by adversarial re-verification and fixed in the same
  slice: (1) a rotated-out parent could resolve to its **same-second twin** —
  one seat posting twice inside a second shares `ts|from`, rotation drops the
  oldest half, and the `(rts, rfrom)` fallback then quoted the wrong message
  under the reply and hung a phantom `↩N` on an innocent row; the fallback now
  fires only when there is no id to honor, in `chat.parent_of` AND the web
  panel's `chatParent` (they must agree). (2) `helm chat verify` reported
  `legacy` — the one never-alarming state — when a signed reply's recorded
  payload was **stripped**, which was the cheapest re-parenting forgery
  available; a signed row that is a reply and carries no payload is now
  MISMATCH, because `reply_to` and the recorded payload shipped together and
  the combination cannot occur honestly. Follow-up review closed the same
  twin hole for **pre-id** parents: `(ts, from)` was already ambiguous before
  rotation and could still resolve to the wrong survivor. Such replies now
  bind and match `rtext`, with every ts|from candidate retained; zero or
  multiple exact matches render an orphan. It also escaped literal RS bytes
  inside digest fields, preventing author/text field sliding from preserving
  a signed digest after an edit.
- The SAFE `/login` — `helm cred`. Hitting a session limit and running
  `/login` must not be a problem to do, and it cannot be redirected (a live
  session's CLAUDE_CONFIG_DIR is fixed), so the write is made truthful,
  non-destructive and reversible instead. Identity now comes from CONTENT
  (`cred.account_of` reads `<home>/.claude.json`'s oauthAccount, mtime-cached,
  fail-closed — an unreadable file claims NO account rather than guessing from
  the directory name), and `homes.py`'s identity reader, `helm launch --home`,
  the keepalive audit log and `helm doctor` all read it, so a dir name can no
  longer speak for an account. `helm cred list` is the owner-visible truth
  surface (DIR NAME | ACTUAL ACCOUNT | verdict); `helm cred backup [--all] --apply`
  snapshots credentials + identity into `~/.cred-backups/<folded-email>/<ts>/`
  (0700 dirs, 0600 files, idempotent, newest-20 retention);
  `helm cred switch-guard [--install] --apply` is the pre-login guard (explicit verb,
  or wired as a SessionStart hook in every claude home); `helm cred heal`
  restores a drifted home — DRY-RUN BY DEFAULT, backing up the current
  occupant first, verifying after, and refusing when a live session holds the
  home (`/proc` probe, re-checked immediately before the write), when there is
  no `/proc` to prove it free, when no snapshot exists, or when the restore
  would leave byte-copies of one refresh token in two homes (the revocation
  bomb). Secrets never surface: credential bytes are copied and compared,
  never printed, logged, or placed in an error string; the only derived value
  written is a 12-hex sha256 fingerprint. doctor gains the loud drift row and
  a `no cred backup for <account>` row. Keepalive — the one place helm writes
  credential files — now snapshots a pre-image before every rotation and logs
  the ACCOUNT beside the home name.

- `helm cred` hardened under adversarial review (five findings, all fixed):
  (1) heal now REFUSES with `no-preimage` when the current occupant cannot be
  snapshotted — it used to evict anyway, deleting the only copy of a live
  credential, which is the exact loss the verb exists to prevent;
  (2) `restore` is all-or-nothing across `.credentials.json` and
  `.claude.json` — both are staged before either is committed, and a failed
  commit rolls the credentials file back, so a half-written restore can no
  longer leave a home holding one account's tokens under another's identity
  block; (3) `restore` REFUSES a present-but-unparseable `.claude.json`
  instead of rewriting it from `{}` (that file holds the home's whole state —
  projects, MCP servers, history — and the old path silently destroyed it);
  (4) a leftover temp file can no longer block a restore permanently (staging
  uses random exclusive sibling names, not a recycled PID name); (5) the
  switch-guard now rides `Stop` as well as
  `SessionStart`, because a live session refreshes its own credentials and the
  grant ROTATES the refresh token — a session-start-only pre-image is dead
  hours before the `/login` it exists for, and keepalive cannot cover the gap
  (it skips every home with a live holder). heal additionally flags
  `stale_pre_image` when a snapshot's own access token had already expired,
  the temporal twin of the shared-family revocation bomb. Two more: snapshot
  dirs are now CLAIMED with an exclusive `mkdir` (one account can occupy two
  homes and the guard runs per turn, so two backups could land on the same
  name in the same second and the loser's error path deleted the winner's
  finished pre-image); and `helm cred list` + doctor's drift row now report
  whether the EVICTED account is recoverable — the BACKUPS column counts the
  ARRIVING account, which reads as `0` at exactly the moment the owner needs
  to know the evicted one is safe (live estate: `admin-example-com`
  now says plainly that nothing was ever snapshotted for it).

- Second independent CRED-SAFE-SWITCH review closed the remaining safety gaps:
  every credential mutation, including `backup`, guard installation, and
  `keepalive`, is now dry-run unless `--apply`; keepalive takes its stable
  pre-image before the rotating network grant; backup brackets identity and
  credential reads so a concurrent `/login` cannot cross-file a snapshot;
  restore rejects symlinks and snapshot identity/digest/length mismatches, then
  performs durable two-file commit with exact
  bytes/mode/absence rollback at every staging/rename/fsync boundary; heal
  re-probes holders after capture and at commit, with permission/read/PID-reuse
  uncertainty refusing; lossy folded-name collisions have exact-account
  counting/retention and ambiguous heal selection refuses; and CLI,
  doctor, keepalive log, invalid-path, non-UTF8 and exception surfaces report
  class/reason only, never credential or token-shaped values. Native quota
  usage and command-mint attribution now share `cred.account_of`, so content
  identity is consistent end-to-end.

- Stop-whisper slice 2 — the verify-grounding rungs: the contextual
  continuation ladder gains three signals read from record.py's own logs
  (one bounded read, fail-closed): a RED gate (a test-runner's latest run
  exited nonzero — stopping on a known-red gate is the premature stop the
  lane exists for; a green rerun silences it), UNVERIFIED edits (code files
  edited, tree dirty, no gate ever ran — doc-only sessions never arm it),
  and UNBANKED green (every latest gate run green, tree still dirty —
  commit is the named next step). Same laws as slice 1: one 240B line per
  stop, once-per-fingerprint latch, salience order, kill-switch,
  fail-closed to silence.

- Multi-model in ONE claude-code process (the proven per-agent-frontmatter
  mechanism): `helm router` — a stdlib-only transparent router at
  ANTHROPIC_BASE_URL that forwards `claude-*` requests VERBATIM to
  api.anthropic.com (claude-code's own OAuth, never an API key, no
  substitution) and conducts non-claude models to their seat's CLIProxyAPI
  with the seat token, logging every request's model/route to a paste-safe
  conductor log. Plus `helm seat launch|smoke --multi`: the mixed-fleet
  launch shape (drops the CLAUDE_CODE_SUBAGENT_MODEL blunt pin, mints
  per-model probe agents) and a smoke fan-out leg that passes only when the
  conductor log shows both probe models on the wire.

- `helm codex` — codexhome roster + proxy cred pooling as a first-class
  verb (the manual night codified): `list` classifies ultra/team from the
  token plan claim with aliases and same-account dirs folded, `pool`
  translates a home's auth.json into the seat proxy's hot-reloaded auth-dir
  (0600, idempotent, stale-exp warns never refuses), `unpool` fail-open,
  `pooled` shows what the :8317 proxy can draw on. Sources read-only
  forever; token material never printed.
- Evolve's last two behavior-observer legs — dead reflexes (live, zero
  ledger fires over ≥200 turns, one batched review line) and recorder
  signatures (a stuck/loop-thrash tell recurring across ≥3 sessions' last
  counters proposes a captured lesson); propose-only as ever. The drain-v2
  upgrade pass ran LIVE: 194 dark typed-prefix files upgraded to real
  priors/lexicon (archive-first net + receipt), the adopted store's episodic
  pile down from 257 to 37.
- `helm corpus` — backup/status for the training-corpus transcript archive:
  every harness's transcripts copied append-only into dated archive dirs,
  incremental by manifest, fail-open per file, fail-safe on space, with a
  daily systemd timer in `scripts/`.
- Attestation is dregg-primary in production: owner web posts and every
  multimodel seat route through the dregg-native client signer under distinct
  named profiles, leaving hybrid-signed, consensus-final cave turns. The local
  blake2b chain / unsigned RAM path remains temporary fail-open coordination
  scaffolding while dregg is unavailable — never the target architecture.
- Kimi proxy-key seat — API-key provider families join the seat roster
  (`helm seat add kimi`, key baked into the seat's 0600 config, never read
  from the environment again) behind the same proxy as the OAuth seats.
- Credential crosscheck parity — creds crosscheck against a local-session second source,
  shared-refresh-token-family hygiene in list/verify/doctor, `helm attribute`
  (token-effort rollup) + `helm who` (pid→cred attribution, evidence-only),
  hermetic providers.py test coverage, and a git-presence doctor row.
- The A2A delivery lane — meld's agent-facing half collapsed onto the chat
  room: deliver/join hooks reach a seat mid-autonomous-turn at tool
  boundaries, TTL claims whose lease nonce is the capability, the seats
  panel in the ledger tab, and `helm launch` (the metaharness seam).
- Web UI Tuftian pass — one type scale, aligned nav stats, uniform
  attention pills, collision-free chart legend, decluttered account table;
  plus the ledger tab (the turn-ledger viewer ported in) and a proper
  'home' landing tab.

## 0.1.0-alpha — 2026-07-19

The founding release: the whole steering station, built and fleet-deployed in
its first day.

### The home
- `~/.helm` — per-project knowledge chains (premises / heuristics / lexicon /
  prd / journal / evals / archive) + a global chain; existing knowledge homes
  adopted by symlink, never copied.
- Auto-map: real projects discovered from what agents actually did, across
  Claude Code, Codex, and OpenCode; worktrees collapse to their repo; scratch
  dirs filtered; a shelf tier registers on-disk repos with no agent activity.

### The knowledge engine
- One typed store (prior / premise / lexicon / heuristic / reference /
  episodic) with one JIT resolver over every root; confidence with evidence
  logs; premises are human-only by construction.
- The drain: raw memory intake classified and routed to typed homes, with
  verified rollback nets and receipts. The drift report: contradictions,
  tier-crossings, and decays — silent when steady.
- Reflexes (signal → steer) and `helm inject`: one budget-capped, salience-
  gated per-turn context call any harness hook can make.
- Attested premises: captures commit a signed digest to a verifiable ledger;
  `premise-check` recomputes and quotes the finality tier; supersession forms
  a provable chain.

### The cockpit
- Sessions: every local session, all harnesses, project-grouped; content
  search inside transcripts; windowed transcript reads; re-homing;
  resume-optimized copies; one-paste resume commands; session capsules
  (the git era a session ended on).
- Accounts: live quota scorecard, rollover rescue (`swap`), credential-home
  lifecycle with identity verification and reversible archives.
- Configs: every config across every home and project, including owner-authored
  commands and Codex rules, the load cascade resolved per seat, and a
  revision-aware atomic editor with exact backup, fsync, conflict rollback, and
  symlink/device/escape refusal.
- The web app: four views (home / quota / sessions / configs) in one
  self-contained page — charts, transcript drawer, team tray, config editor —
  with a per-process mutation token on every write.

### The map
- Lineage: the project family tree with git-proven edges, external anchors,
  and a read-only ranked archive report.

### Operations
- `helm doctor` (read-only health), `helm evolve` (proposes, never mutates),
  skills census with divergence detection, `python3 -m helm`, PATH shim,
  systemd web service, fleet hook deployment with per-home backups.
