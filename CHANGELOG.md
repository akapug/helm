# Changelog

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
