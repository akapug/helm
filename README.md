# helm ⎈

[![License: AGPL v3](https://img.shields.io/badge/license-AGPL--3.0-blue.svg)](LICENSE)
[![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue.svg)](https://www.python.org/downloads/)
[![Platform: Linux](https://img.shields.io/badge/platform-linux-lightgrey.svg)](#requirements)
[![Dependencies: none](https://img.shields.io/badge/dependencies-none-brightgreen.svg)](#requirements)

**Run a team of AI coding agents from different model families on one
codebase, and trust what lands.**

helm is the coordination layer for a fleet of coding agents. It puts Claude,
Codex, Kimi, DeepSeek, Grok, Gemini and local models in one chat room that
reaches them mid-turn. It tracks who owes what on a durable ledger. It makes
sure the agent that wrote a change is never the one that approves it, and it
merges work to your main branch only after the whole test suite passes on
the exact code being merged. For you, it is one place to see every project, session, account and
quota window.

- **Steer in plain words.** You talk to the whole fleet in one chat room and
  one web page, and the agents run the commands. Your standing rules and the
  lessons the fleet has learned reach each agent on the turn they matter, so
  you say a thing once, not once per session.
- **Quality from independent review.** A reader that wrote none of the
  change, in a fresh context that holds none of the author's working context,
  reads the exact commit; a different model, ideally from another model
  family, is preferred. Model families are
  equal counterparts, not a writing tier and a witnessing tier: readers
  commit the fixes they find, and a design dispute moves into a shared room
  where both sides agree the bar.
- **Lands with nobody at the wheel.** A reviewed change lands by itself:
  helm announces the train, waits five minutes for a veto, runs the whole
  suite on the exact tree, names and ejects a car that breaks it, and pushes.
  43 lands in a row went in that way with no integrator action
  ([Auto-land](#auto-land-lands-with-nobody-at-the-wheel)).
- **Scale without losing the thread.** Each agent gets its own worktree,
  terminal pane and credential pool. A context compaction or a crashed proxy
  costs a restart, not the work: agents write handoffs, resume their sessions,
  and respawn what died.
- **Nothing to install.** Python standard library only, zero dependencies: a
  CLI and a web app over one folder, `~/.helm`.

**Try it** (nothing to build):

```console
$ git clone https://github.com/akapug/helm && cd helm
$ ./bin/helm sync     # discover your projects across every harness
$ ./bin/helm web      # the whole cockpit at http://127.0.0.1:7433
```

**If you are an AI agent:** if you were seated in a helm fleet, read
[NEW_AGENT_GUIDE](docs/NEW_AGENT_GUIDE.md) first. If you are changing helm's
own code, read [AGENTS.md](AGENTS.md) first. `helm --help` lists every verb,
and [LANDING](docs/LANDING.md) says how work gets reviewed and landed.

## What it looks like

**Words helm uses.** A **seat** is one agent with a stable name in the fleet
room: its sessions come and go, the seat stays. A **lane** is one piece of
work, with a lease on it, its own git worktree and a `lane/<name>` branch. A
**tip** is the exact commit a review reads. The **dispatch ledger** is the
durable list of who owes what, and each entry on it is a **row**. A **cure**
is a fix a reviewer finds: PATCH commits it, while MELD-DIFF gives the author an exact diff
in the pair meld and records its validated message receipt with
`--diff-handoff ROOM/MSGID`. A MELD-DIFF cure requires both that receipt and
send/add proof that the child applies the diff. Prose alone, historical rows
without a typed receipt, and unrelated children count as ordinary rounds.
A **train** merges reviewed tips and runs the whole suite once; to **land** is to reach trunk
(`main`) that way. A **metaharness** (orca or herdr) owns the terminal panes
the agents run in. A **meld** is a shared room where two seats settle a
design question. The **cockpit** is the half of helm that needs no fleet:
your projects, sessions, memory, accounts and quota, in the CLI and the web
page. The **per-turn physics** is what helm's hooks add to each agent turn:
the relevant knowledge, messages delivered mid-turn, the command guards, the
stop guard and the compaction handoff.

One change, moving through a fleet:

1. **You post the goal** in the fleet room (`helm chat post`, or the web
   page). Each agent hears it between two tool calls, not at the end of its
   turn.
2. **A Claude seat claims a lane.** `helm work claim <lane>` gives it a
   lease, its own worktree and a `lane/<lane>` branch.
3. **It builds, runs focused tests, and books a review** of the exact commit
   with `helm dispatch send`
   ([LANDING](docs/LANDING.md#booking-and-answering-a-review) has the full
   command).
4. **A reviewer reads that commit** in a fresh context. For a mechanical
   defect, the reviewer follows the row's fix mode: PATCH commits the cure;
   MELD-DIFF posts an exact diff for the author to apply. The reviewer records
   the verdict with `helm dispatch verdict`.
5. **A train lands it.** `helm train --apply` merges the reviewed tips and
   runs one whole suite on the exact tree that lands; `helm lr foldcheck`
   proves what landed.
6. **Meanwhile, nothing is lost.** A seat near its context limit writes a
   handoff (`helm handoff write`) and resumes on it. A seat whose account ran dry
   gets exact resume commands under a healthier one (`helm swap`). `helm burn`
   shows which model families have quota left.

```text
                 you ─ chat room · web page · morning brief
                              │  (reaches agents mid-turn)
      ┌──────────────┬────────┴─────┬───────────────┐
  Claude seat    Codex seat     Kimi seat     local model seat
      └──── each: own pane, worktree, proxy, credential pool ────┘
                              │
   dispatch ledger: work, reviews and verdicts, bound to exact commits
                              │
     train ──► one whole suite on the tree that lands ──► trunk
```

## What helm was built for

- **One operator steering a mixed-model fleet on one Linux host.** helm
  was built running a live fleet of 16 to 21 agent seats under
  [orca](https://github.com/stablyai/orca), across several
  projects, model families and credential pools, with quota measured for each
  pool. At its founding in July 2026, its adopted memory store held about
  880 entries and its session catalog about 15,000 sessions across harnesses.
- **helm is built with helm.** Every change to helm is claimed on a lane,
  read by a reader that did not write it, and landed through helm's own gate:
  one whole suite of more than 27,000 tests, run as parallel slices while a
  nightly serial run agrees with them test for test, and serially otherwise.
- **Many model families at once.** Claude, Codex (GPT), Kimi, DeepSeek V4,
  Grok, Gemini, Cursor through a bridge, OpenRouter's free router, and local
  Qwen and Bonsai models served by vLLM or llama-server. The maintainers'
  stability target is a core of Claude, Codex and Kimi; their Gemini and
  Grok accounts often sit in quota cooldowns, and helm routes around them.

| If you are... | helm fits... |
|---|---|
| running several coding agents, across model families or accounts, on one Linux machine | exactly: this is what it was built for |
| a team that wants models from different vendors to check each other's work | well: independent review and a whole-suite land gate are the core |
| one Claude Code user who wants projects, sessions, memory and quota in one place | well: the cockpit half works alone, with no fleet and no other tool |
| managing agent panes with tmux or cmux | partly: helm runs, but pane operations are no-ops until an adapter exists |
| on macOS or Windows | not yet: Linux only today |

## Where it works today

| Area | Status |
|---|---|
| Maturity | 0.x. The maintainers use it every day on their own fleet, but verbs and defaults still change between releases: read the [CHANGELOG](CHANGELOG.md) before you upgrade. |
| Platform | Linux. Python 3.9+ is declared; the land gate runs CPython 3.14. macOS and Windows are not supported. |
| Harness | Claude Code gets everything, through its hooks. Codex gets the cockpit (sessions and projects) and OpenCode its projects; neither gets the per-turn physics. [pi](https://github.com/earendil-works/pi) seats can be addressed in chat, but have no hooks. |
| Metaharness | orca (recommended) or [herdr](https://github.com/herdrdev/herdr). tmux and cmux are not supported. With none, pane operations are no-ops. |
| Review | The approval tier (whose APPROVE can close a review) is a policy you store; [LANDING](docs/LANDING.md#the-review-rule) explains it. The maintainers' fleet admits a fresh-context Claude Opus, Codex, DeepSeek V4 Pro, Kimi and Grok; Gemini and local models give input only. |
| Session catalog | Claude Code and Codex. OpenCode and pi sessions count toward projects only. |
| Web | `helm web` serves Home, Work, Chat, Fleet and History at `http://127.0.0.1:7433`. The alarm badge, Home, the land board and the scheduler count the land pipeline from one reading; a count not fully read shows as a floor or `?`, never a 0, and a stale reading says its age. |
| Multiplayer | Local only: humans and agents on one machine. |
| Remote sessions | helm drives Claude Code cloud sessions for reviews and builds. |

## Quick start

```console
$ ./bin/helm --help      # one line per verb
$ ./bin/helm sync        # discover your projects across every harness
$ ./bin/helm projects    # the list, newest activity first
$ ./bin/helm sessions    # every Claude Code + Codex session; resume in one paste
$ ./bin/helm doctor      # what helm can and cannot see (read-only)
$ ./bin/helm web         # the same, in a browser
```

`helm sync` scaffolds `~/.helm` and reads your existing Claude Code memory in
place; it never copies it. To put helm on your `PATH`, run
`sh scripts/install.sh`, or link it by hand:
`ln -s "$PWD/bin/helm" ~/.local/bin/helm`.

To run a fleet, install the hooks (`helm hooks install`), add a metaharness,
and start seats with `helm launch` or `helm seat spawn`.
[INSTALL](docs/INSTALL.md) walks through each step.

### Requirements

**Python 3.9+ on Linux, and nothing else.** helm is standard library only,
with zero dependencies, and the test suite checks that. It is Linux-only
because its chat lives in RAM at `/dev/shm`, and it relies on `/proc`,
`fcntl` and systemd user units. [INSTALL](docs/INSTALL.md#requirements) has
the details.

## How it works

helm is an overlay: it owns almost none of the machinery it steers.
[ARCHITECTURE](docs/ARCHITECTURE.md#harness-metaharness-family-and-backend)
has the full model.

1. **Claude Code is the harness, because of its hooks.** helm's per-turn
   behaviour rides Claude Code lifecycle events, so a message reaches a
   working agent between its tool calls: it is interrupted, not queued.
   `helm hooks install` writes the hooks, and `helm hooks status` shows which
   are armed. [HOOKS](docs/HOOKS.md) lists all of them.

| Hook | What helm does with it |
| --- | --- |
| `UserPromptSubmit` | add the standing rules and relevant knowledge to the turn |
| `PostToolUse` | deliver chat messages mid-turn, between tool calls |
| `PreToolUse` | refuse a command whose shell would mangle or execute a message body |
| `SessionStart` | join the room; restart the turn loop after a compaction |
| `PreCompact` | demand a handoff before the context window is rewritten |
| `Stop` | refuse an idle stop while addressed messages are undelivered |

2. **A metaharness holds the panes.** [orca](https://github.com/stablyai/orca)
   is the recommended companion: real pane control plus a git worktree for
   each seat. herdr is also implemented. tmux and cmux are not supported
   today; a new adapter is a contained amount of work, not an architecture
   change. With none, helm still runs, and pane operations say they did
   nothing.
3. **One harness, many model families.** Non-Claude families run behind
   [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI), so a Gemini or
   Kimi seat is a Claude Code process with a different model behind it, and
   it inherits every hook. One integration, not six.
4. **A ledger and a gate decide what lands.** Work and reviews are rows on a
   durable dispatch ledger, bound to exact commits. A review counts when the
   reader holds none of the author's working context (a different seat on a
   fresh session); a different model or family is preferred, not required.
   A change lands with one approval-tier read and one whole suite on the
   exact tree that lands. [LANDING](docs/LANDING.md) has the whole path.
5. **A typed knowledge store feeds every turn.** Beliefs, vocabulary,
   heuristics and reference material sit behind one just-in-time resolver, so
   an entry reaches an agent when its prompt touches it. `helm drain` routes
   raw notes into the store, `helm drift` speaks up when evidence contradicts
   a belief, and `helm premise` records a certainty on a tamper-evident
   chain. The repository also ships the Claude Code skills its own fleet
   works with, in `agents/claudecode/skills/`.
   [TOUR](docs/TOUR.md) walks through every part.

The fleet half composes with projects helm does not own. None of them is
needed to try helm, and each one degrades to a single line:

| Project | What it gives helm | Without it |
| --- | --- | --- |
| **[dregg](https://github.com/emberian/dregg)** | signed transport and an attested ledger: a turn can be signed, a premise anchored as a verifiable digest | turns post unsigned; attestation stays on helm's native chain |
| **[cv](https://github.com/emberian/cv)** (clustervision) | cross-harness session recall: semantic search over what every agent has already done | `helm search` says it needs cv; `helm search --scope <dir>` still scans that project's local transcripts |
| **[CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI)** | non-Claude families inside the Claude Code harness | a Claude-only fleet; every other verb is unaffected |
| **[orca](https://github.com/stablyai/orca)** / **[herdr](https://github.com/herdrdev/herdr)** | panes, worktrees, seat spawn and resume | pane operations are no-ops; you drive the terminals |

`helm doctor` and `helm capabilities` say what is wired on your machine.

## Auto-land: lands with nobody at the wheel

Landing used to be a person's job, repeated about 20 times a day: list what
is ready, merge it onto trunk, run the tree-wide audits, launch the whole
suite, read the receipt, check the test count, push, fast-forward the shared
checkout, close each row and task, announce. About 15 steps each time, each
waiting on one agent's attention. `helm train auto` does all of it as a state
machine that a timer ticks every two minutes, and it resumes where it stopped
if a tick dies halfway.

1. **Intent.** When a lane is ready on the ledger (a reader that wrote none
   of it approved or held that exact commit), auto-land posts which train
   will land, every car in it (lane, task, commit, row) and the veto line.
   Anyone can stop it within five minutes with
   `helm train veto <train> --reason R`.
2. **Compose.** Only the cars still ready at the same commit are merged, each
   at its exact tip, into a fresh room on top of trunk. A car that conflicts
   is dropped and named; no machine resolves a conflict.
3. **Gate.** The tree-wide audits run on the composed room, then the whole
   suite runs on that exact tree. The receipt must be for that tree, and the
   test count must change by exactly the tests the change adds less the tests
   it removes.
4. **Blame, never guess.** A red gate is re-run alone once, and a pass there
   is recorded as a flake. A real failure goes to `helm train blame`, which
   names the car that broke the train (by its diff, or by bisecting the
   train), ejects that exact commit and lands the rest.
5. **Land.** Every car, the veto and the destination are asked once more,
   then the push is fast-forward-only against the trunk that was gated. The
   shared checkout follows, the rows close, and the room gets one line naming
   the land, its gate receipt and what would prove the claim wrong.

A change behind a door (production, a migration, a deletion, money,
credentials, a process kill, a public push, or a safety door such as land,
review, a guard or a hook's refusal) rides only when an approval-tier reader
approved or held it. A land that needs a person stops loudly and waits for one: a red
trunk, a receipt it cannot read, or a push whose outcome is unclear.

**Measured.** From LAND 461 (2026-09-28 18:19 PDT) through LAND 503
(2026-09-29 16:17 PDT), 43 lands in a row went in with no integrator action,
each after the whole suite passed on the exact tree that landed (26,824 tests
at LAND 461, 27,633 at LAND 503). The integrator now reads what lands instead
of running it. [LANDING](docs/LANDING.md) has the whole path, and
`helm train auto --status` shows the train in flight.

## How well it works: the 11-axis eval

helm's maintainers score it on **11 agent-experience axes**, 1 to 100, against a
written rubric that ties every 80 and every 90 to a measurement. Across five
snapshots scored under the fixed rubric, over three and a half days
(2026-09-25 07:00 to 2026-09-28 21:54 PDT), the mean rose from **73.1 to
78.1**, and the axes at 80 or above went from 2 to **5 of 11**. No axis is at
90.

Mean at each snapshot (PDT): 09-25 07:00 **73.1** → 09-25 13:55 **72.1** →
09-25 20:41 **74.5** → 09-28 11:10 **76.5** → 09-28 21:54 **78.1**.

| Axis | 2026-09-25 07:00 | 2026-09-28 21:54 | 80 bar |
|---|---|---|---|
| Test suite speed | 64 | 84 | met |
| Land safety and guards | 80 | 82 | met |
| Review process | 78 | 81 | met |
| Landing pipeline | 77 | 80 | met |
| Release process | 75 | 80 | met |
| Ledger reads | 82 | 78 | not yet: scored before the post-land read timer (`helm lr postland`) landed |
| Hooks | 76 | 77 | not yet: stop hook p95 is 1.5 s under load; the bar is 1 s |
| Owner surface | 64 | 77 | not yet: `helm brief --report` exists but missed lands; the report of record is still hand-written |
| Delegation | 74 | 75 | not yet: builder claims are not checked automatically |
| Agent chat | 68 | 74 | not yet: the idle re-ring is built but fired 0 times in 9 h |
| Seat health | 66 | 71 | not yet: idle seats that owe work are not re-surfaced |
| **Mean** | **73.1** | **78.1** | 5 of 11 met |

**Read these as self-scores.** The rubric is the maintainers' own, and
read-only agents took the measurements on the maintainers' fleet. It is not a
cross-family council, and it is not an independent rating. The version number
0.3.5 is reserved for the first release that independent raters, each working
real helm tasks, score 90 or above on every axis. The release cut from these
scores is 0.3.2. [EVAL](docs/EVAL.md) has the rubric, every snapshot
over time, charts, and the independent-rater battery.

## What needs work

- **The six axes below 80.** The next steps, in order: make the idle re-ring
  fire, make `helm brief --report` complete enough to replace the hand-written
  report, bind every builder claim to a
  checking command, get the stop hook under 1 s at load, and score ledger
  reads from the post-land read timer (`helm lr postland`).
  [EVAL](docs/EVAL.md#what-is-next) has the detail.
- **A single work view in the web console (being built).** Each piece of
  work becomes one card that moves from To do through Building, In review and
  Landing to Landed. It marks each time the work was sent back, and a drawer
  shows its whole path.
- **Independent ratings.** The independent-rater battery has run one dry
  round, with one local model. The full battery, with raters from several
  model families and a deliberately broken control, has not run.
- **More metaharnesses and platforms.** tmux, cmux and zellij need adapters.
  macOS and Windows ports are not claimed.
- **Per-turn physics beyond Claude Code.** Native Codex and OpenCode
  sessions, and pi seats, get no hooks and cannot be woken.
- **Model records for native seats.** A native Codex or OpenCode runtime
  records no resolved model, so helm cannot tell which model read a change on
  those seats; `helm route` says so beside the seat rather than barring it.
- **More model families as approvers.** Gemini and local models give
  findings only. Admitting every family as an approver waits until the
  Claude, Codex and Kimi core is stable.
- **Version control.** helm works with git only; Jujutsu (jj) support is
  not built, and some direct git calls still sit outside the version-control
  seam.
- **Packaging.** The immutable, non-writable install is a preview and does
  not replace the checkout install yet ([INSTALL](docs/INSTALL.md#immutable-release-preview-not-cut-over)).
  Remote and web multiplayer are outside the local core.
- **Docs.** The verb reference is one very large file; `helm --help` is the
  browsable overview today.

## Principles

1. **Overlay, not another store.** helm references authoritative homes — your
   repos, your harness session stores, your recall index — it never copies
   them. Every projection is read-only as truth; an edit lands at the source.
2. **The session store is data, not identity.** Working directories are an
   attribute; sessions are the key. helm decodes real paths from inside
   transcripts, never from directory names.
3. **Additive and idempotent.** `helm sync` never deletes a known project.
   Cleanup means *archive with a reference back*, never deletion.
4. **CLI-first, with a web equivalent.** Every curation verb works in a
   terminal and in the browser (`helm web`).
5. **Zero dependencies.** Python standard library only. One checkout, no
   install step.

[DESIGN_PHILOSOPHY](docs/DESIGN_PHILOSOPHY.md) explains why helm is shaped
this way.

## Where to go next

| If you are... | Read |
|---|---|
| trying helm for the first time | [INSTALL](docs/INSTALL.md), then [TOUR](docs/TOUR.md) |
| an agent seated in a helm fleet | [NEW_AGENT_GUIDE](docs/NEW_AGENT_GUIDE.md): your first 10 minutes |
| an agent changing helm's own code | [AGENTS.md](AGENTS.md) |
| a human contributor | [CONTRIBUTING](CONTRIBUTING.md) |
| running a fleet | [LANDING](docs/LANDING.md), [HOOKS](docs/HOOKS.md), [WEB](docs/WEB.md), [MODEL_FAMILY_FAILOVER](docs/methodology/MODEL_FAMILY_FAILOVER.md) |
| asking why it is built this way | [ARCHITECTURE](docs/ARCHITECTURE.md), [CONCEPTS](docs/CONCEPTS.md), [DESIGN_PHILOSOPHY](docs/DESIGN_PHILOSOPHY.md) |
| asking how well it works | [EVAL](docs/EVAL.md), [COUNCIL_EVAL](docs/methodology/COUNCIL_EVAL.md) |
| upgrading | [CHANGELOG](CHANGELOG.md) |
| reporting a vulnerability | [SECURITY](SECURITY.md) |

**Looking up a verb or a setting.** `helm --help` prints one line per verb,
`helm <verb> --help` prints that verb's full usage, and `helm capabilities`
lists what is wired on this machine. [docs/VERBS.md](docs/VERBS.md) is the
authoritative verb surface: about 1 MB, so search it rather than read it.
[docs/ENVIRONMENT.md](docs/ENVIRONMENT.md) lists every `HELM_*` variable (all
optional; `HELM_HOME` overrides the default `~/.helm`), including the legacy
spellings still read as fallbacks.

**More reference:** [ATTESTATION](docs/ATTESTATION.md) (the premise hash
chain) · [EVOLUTION](docs/EVOLUTION.md) (the self-evolution loop) ·
[MELD_REVIEW_DOOR](docs/MELD_REVIEW_DOOR.md) (when a review becomes a meld) ·
[MULTIPLAYER](docs/MULTIPLAYER.md) and
[MULTIPLAYER_TESTDRIVE](docs/MULTIPLAYER_TESTDRIVE.md) ·
[REMOTE_SESSIONS](docs/REMOTE_SESSIONS.md) ·
[CLASSIFY](docs/CLASSIFY.md) (the optional classifier client) ·
[FOLD_CHECKPOINT](docs/FOLD_CHECKPOINT.md) (why ledger reads are fast) ·
[DISPATCH-ADD-CONTRACT](docs/DISPATCH-ADD-CONTRACT.md) ·
[MODULE_REGISTRIES](docs/MODULE_REGISTRIES.md) (what a new module owes) ·
[STE-CLARITY](docs/research/STE-CLARITY.md) (the controlled language behind
`helm clarity`)

## Credits and license

helm composes with [dregg](https://github.com/emberian/dregg) and
[cv](https://github.com/emberian/cv) by Ember
([github.com/emberian](https://github.com/emberian)),
[CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI), the
[Orca](https://github.com/stablyai/orca) and
[herdr](https://github.com/herdrdev/herdr) metaharnesses as hosts, and
Claude Code's hook surface.

[AGPL-3.0-or-later](LICENSE): the same copyleft as dregg (cv is
MIT/Apache-2.0-licensed; helm's copyleft is its own choice, not required by a
dependency). Modified network-served versions must share source; running helm
for yourself, or inside your own fleet, asks nothing of you.
