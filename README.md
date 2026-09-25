# helm ⎈

[![License: AGPL v3](https://img.shields.io/badge/license-AGPL--3.0-blue.svg)](LICENSE)
[![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue.svg)](https://www.python.org/downloads/)
[![Platform: Linux](https://img.shields.io/badge/platform-linux-lightgrey.svg)](#requirements)
[![Dependencies: none](https://img.shields.io/badge/dependencies-none-brightgreen.svg)](#requirements)

**Run a team of AI coding agents from different model families on one
codebase, and trust what lands.**

helm is for anyone who wants more from coding agents than one chat window
gives: the vibecoder who has never read the code and needs to know the work is
right, the developer running ten agents across five accounts, and the team that
wants Claude, codex, gemini, grok, kimi and deepseek checking each other's work
instead of each trusting its own.

- **Steer in plain words.** You talk to the whole fleet in one chat room and
  one web page, and the agents run the commands. Your standing rules and the
  lessons the fleet has learned are fed to every agent on every turn, so you
  say a thing once, not once per session.
- **Quality from disagreement.** The model family that wrote a change may
  never be the one that reviews it. A second family catches the blind spots
  the first one shares with itself, and work lands only after that review and
  a whole-suite test run bound to the exact commit.
- **Scale without losing the thread.** Each agent gets its own worktree,
  terminal pane and credential pool. One view shows every session, account and
  quota window. A context compaction or a crashed proxy costs a restart, not
  the work: agents write handoffs, resume their sessions, and respawn what
  died.
- **Nothing to install.** Python standard library only, zero dependencies: a
  CLI and a web app over one folder, `~/.helm`.

**Try it:**

```console
$ ./bin/helm sync     # discover your projects across every harness
$ ./bin/helm web      # the whole cockpit, in a browser
```

The full quickstart, PATH install, and requirements are [below](#quickstart).

---

## How it actually works

Read this part first. helm is an **overlay** — it owns almost none of the
machinery it steers, and knowing which piece does what is the difference
between "a CLI with a lot of verbs" and understanding the system.

### The harness — where an agent runs

A **harness** is the coding-agent CLI itself. **Claude Code is helm's harness
of expertise, and the reason is hooks.** Claude Code exposes lifecycle events
— before a tool call, after one, at session start, before a compaction, at
stop — and helm's entire per-turn physics rides them:

| hook | what helm does with it |
| --- | --- |
| `PreToolUse` | refuse a command whose shell would mangle or execute a message body |
| `PostToolUse` | deliver chat messages to an agent **mid-turn**, between tool calls |
| `SessionStart` | join the room; restart the turn loop after a compaction |
| `PreCompact` | demand a handoff before the context window is rewritten |
| `Stop` | refuse an idle stop while addressed messages are undelivered |

None of that is polling. A message reaches a working agent because a hook
fires between its tool calls, which is why an agent here can be *interrupted*
rather than merely *queued*. `helm hooks install` writes them; `helm hooks
status` tells you which are armed — landing helm's code arms nothing by
itself.

Other harnesses (Codex, OpenCode) are discovered for sessions and projects.
They do not have the hook surface, so they get the cockpit but not the
per-turn physics.

### The metaharness — where a pane lives

A **metaharness** manages the terminal panes agents live in, so helm can spawn
a seat, read its screen, send it a keystroke, and resolve which pane belongs
to which session. helm is metaharness-**agnostic**: `helm/harness.py` is an
adapter seam exposing one uniform `spawn / list / read / send / stop /
resolve_pane`.

- **[orca](https://github.com/stablyai/orca)** — the recommended companion,
  and what supercharges the whole thing: real pane control plus per-seat git
  worktrees, so nine agents are not editing one checkout.
- **herdr** — also implemented; wins automatically inside a herdr session,
  because panes should spawn where you already live.
- **Anything else** — `ADAPTERS` in `helm/harness.py` is a two-entry dict and
  a small base class. tmux, cmux and friends are **not supported today**; they
  are a contained amount of work, not an architecture change.

With no metaharness installed, helm still works — every pane operation
degrades to a no-op and says so.

### The model families — one harness, many vendors

A **family** is the model behind a seat: codex, gemini, grok, kimi, deepseek,
and the Claude models. Non-Claude families run behind
**[CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI)**, which speaks
the Anthropic wire protocol to Claude Code and the vendor's own protocol
upstream.

That is a deliberate choice with one large consequence: **a CLAUDE-HARNESS
seat routed through CLIProxyAPI inherits the hooks, whatever family is behind
it.** A gemini seat gets the same mid-turn delivery, the same stop-guard, the
same compaction resume as a Claude seat, because it *is* a Claude Code process
with a different model behind it. One integration, not six.

Read that as the HARNESS axis, not the proxy: a seat carries a family, an
agent harness AND a backend, and the three are independent
(`seats._runtime_metadata`). Going through the proxy is not what earns the
hooks — `helm pi run` is `family · pi/proxy`, proxy backend and all, and execs
the **pi** binary (`helm/pi.py`, pinned by
`tests/test_pi_start_resume.py::test_run_pins_model_registers_real_roster_row_and_execs_key_in_env`).
It writes a roster row before exec, so the seat is addressable in chat — but it
is not a `claude` process, so no Claude Code hook fires in it, `helm hooks
install` does not cover it (it enumerates claude homes and seat
`CLAUDE_CONFIG_DIR`s), and the uncovered-pane scan cannot even see it running
(it matches argv `claude`). Address a pi seat and nothing wakes it.

The consequence to know when reading an error: a `403` or a quota wall
rendered in a Claude Code pane belongs to **the underlying vendor**, not to
Anthropic. Read the vendor off the error text, never off the harness.

### The substrate — the parts helm does not own

| project | what it gives helm | without it |
| --- | --- | --- |
| **[dregg](https://github.com/emberian/dregg)** | signed transport and an attested ledger: a turn can be *signed*, a premise committed as a verifiable digest, a claim proven rather than asserted | turns post unsigned; attestation verbs say so in one line |
| **[cv](https://github.com/emberian/cv)** (clustervision) | cross-harness session recall — semantic search over what every agent, in every harness, has already done | `helm search` falls back to local transcript scanning |
| **[CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI)** | non-Claude families inside the Claude Code harness | a Claude-only fleet; every other verb is unaffected |
| **[orca](https://github.com/stablyai/orca)** / herdr | panes, worktrees, seat spawn and resume | pane ops are no-ops; you drive the terminals |

**None of these are required to try helm.** The core — projects, the typed
store, sessions, credentials, the web app — is Python standard library over
`~/.helm` and nothing else. They are what the *fleet* half is built on, and
each one degrades to a single honest line rather than a stack trace.
`helm doctor` tells you exactly what it can and cannot see.

---

The pillars, grouped by what they serve — the **knowledge** your agents read,
the **cockpit** you steer from, the **fleet** of agents, and the **surfaces**
that render it all:

### Knowledge — what every agent reads

- **The knowledge chain, per project** — `premises / heuristics / lexicon /
  prd / journal / evals / archive` under `~/.helm/<project>/`, following the
  organic dev cycle: prior art feeds specs, the build happens in the repo,
  evals and journals record what happened, the archive keeps history without
  clutter.
- **One typed store, one resolver** — beliefs (priors, with confidence that
  updates on evidence), vocabulary (your lexicon — the terms you coined),
  heuristics (moves you apply), and reference material, unified behind a
  single just-in-time resolver. Relevant entries surface when a prompt touches
  them; nothing wallpapers every turn. *Salience is the scarce resource.*
- **The drain** — raw memory intake is a transient inbox, never a destination.
  `helm drain` classifies raw entries and routes them into their typed homes,
  archiving the source. Nothing is deleted; everything stays retrievable.
- **The drift report** — beliefs carry confidence and evidence logs. When the
  evidence starts contradicting something you hold as true, `helm drift`
  surfaces it — and only then. No news is silence.
- **know-your-user** — a first-class profile of how you like to work: voice,
  autonomy, standing corrections, goals. The interview takes five minutes and
  every agent that reads the store gets warmer.
- **Attested truths** — `helm premise` captures a certainty into the store
  *and* commits a signed digest to a verifiable ledger; `helm premise-check`
  re-verifies it and quotes the finality tier. Belief history supersedes as a
  provable chain, never deleted.

### Your cockpit — sessions, accounts, credentials

- **`helm projects`** — your real project list, discovered from what your
  agents actually did, across every harness (Claude Code, Codex, OpenCode —
  more welcome). Worktrees collapse into their repo; scratch dirs are filtered;
  a project you only ever touched in one harness still counts.
- **`helm sessions`** — every local Claude Code + Codex session, grouped by
  project, with the exact resume command one paste away (it's just the
  harness's own CLI — no wrapper). Those two are the session catalog's whole
  scope; an OpenCode or pi session still counts toward `helm projects` above,
  but does not appear here. Content search inside transcripts
  (`helm search`), windowed reads (`helm transcript`), re-homing, and
  resume-optimized copies (`helm prune`) included.
- **Accounts and quota** — `helm creds` is the live scorecard (headroom,
  reset windows, use-it-or-lose-it verdicts); `helm swap` rescues a seat that
  ran dry with exact resume-under-a-healthier-account commands; `helm homes`
  manages credential homes safely (helm prepares, you run every login).
  **`helm cred`** makes `/login` safe: it reads which account each credential
  home ACTUALLY holds from the home's own content (never from the directory
  name), snapshots credentials at `0600` so a login that lands in a pinned
  session's home is reversible, and heals a drifted home — dry-run by default,
  refusing while any live session holds it.
- **Corpus + attribution** — `helm corpus` archives the Claude, Codex and
  `/tmp`-estate raw transcripts into a dated append-only archive (your
  training corpus, copy-only, incremental — OpenCode and pi transcripts are
  not collected); `helm attribute` rolls up token effort by
  project/model/cred, and `helm who` maps live pids to the credentials
  they're burning.

### The fleet — many agents at once

- **The fleet room** — `helm chat`: the human-included groupchat in RAM,
  owner in the room. The delivery lane reaches an agent mid-turn, between
  tool calls (`@seat` mentions and owner posts), with roster presence
  (`helm chat seats`), advisory claim leases, and `helm launch` to start a
  session already seated. Chat lives in `/dev/shm` on purpose: coordination
  state is memory that is read every turn, and disk is the write-behind log —
  never the bus.
- **Seats** — `helm seat` mints a live agent of a given family: its own pane,
  its own git worktree, its own proxy and credential pool, registered so helm
  can find it again. `helm seat spawn|resume|where|status|doctor` are the
  lifecycle; `doctor --ensure` respawns a proxy that died quietly, and
  compaction is survivable — a seat that compacts restarts its own turn loop
  instead of sitting idle until a human types into its pane.
- **The cross-family review gate** — the load-bearing rule of the whole
  project: **the author's resolved runtime model family may not be the
  reviewer's.** Family comes from each canonical roster row's verified runtime
  record, never the seat name, agent/subagent type, harness, or UI label; absent
  or unverified runtime is UNKNOWN and cannot authorize the gate. `helm
  dispatch send <seat> <lane> --ref <tip> --kind review
  --new-work|--supersedes <id>` books a review against an exact commit and
  says which WORK it belongs to — the lane is only a label, so a renamed
  continuation would otherwise read as round one; the reviewer replies with a verdict whose polarity
  is *required* (`--approve` / `--fix`), because a decision that forgot to say
  which way it went is recorded as UNDECLARED forever. A gemini seat refuting
  a codex seat's work catches what neither catches alone — not because either
  is better, but because a blind spot is a property of a shared frame.
  **The families are equal counterparts, not a writing tier and a witnessing
  tier.** A reviewer of any family who finds a MECHANICAL defect commits the
  cure in its own worktree on a branch off the exact reviewed tip and names it
  on the verdict (`--patch-tip <sha>`); the lane owner or integrator rebases
  onto that tip, the ledger records both authors, and `helm lr close --reason
  landed` credits each. A DESIGN finding goes to a meld instead. What preserves
  the independence the gate exists for is that the composed tip is re-read once
  before the land gate by a reader who wrote none of it.
- **Minted whole-suite gates** — `helm gate run` binds interpreter, exact tree,
  before/after cleanliness, process exit and unittest summary into one receipt.
  In helm's own tree the whole suite belongs to the land gate: a lane is
  tested in focused rounds (`helm gate run --focus` plus `helm gate audits`), and
  `helm gate run` refuses a whole suite in a lane room, refuses a second one
  on a tree that is already green, and runs a red tree again only with
  `--again`. Only a SERIAL receipt authorizes a land. A sliced one (the
  default with no mode flag in a lane-level room) binds a lane tip and a
  review's approve and never a land — see
  [How a change is tested](CONTRIBUTING.md#how-a-change-is-tested).
  Expensive whole-suite runs enter a process-owned repository FIFO first: every
  linked worktree shares one ordered slot, poll speed cannot barge, dead
  positions are skipped visibly, and the slot stays held through receipt append.
  During mixed-version rollout, the FIFO head also holds the legacy
  `gatelock:<project>` mutex, waits boundedly through transient claims-lock
  contention, strictly refreshes it, and validates its exact lease while appending
  the receipt so older/manual runners cannot overlap it. Syntactically or
  structurally malformed strict claim state refuses rather than reading empty. A
  Linux child subreaper waits behind a one-byte launch barrier until its exact PID
  is bound into the FIFO row. A pipe-free watchdog resumes a stopped bound
  supervisor immediately when the exact launcher generation dies, remains an
  unreaped zombie, or stops long enough that it cannot renew the compatibility
  lease; its argv also carries the immutable position token so a later
  dead-launcher sweep can recover even a stopped pre-bind supervisor. A dedicated
  guard enters a delegated per-position cgroup before spawning the inner supervisor,
  so every suite descendant inherits one kernel-owned membership without changing
  the PID namespace the tests are validating. `cgroup.kill` removes the entire
  membership despite `setsid`, including simultaneous guard and supervisor
  `SIGKILL`. A watchdog outside the cgroup kills and removes it after launcher or
  guard death, while unrelated launcher children remain structurally outside
  cleanup. The inner
  supervisor freezes a fork-capable detached tree before killing
  it, and a stopped supervisor is resumed
  into that cleanup path; pipe collection remains bounded after shutdown. Queue
  wait is outside the child timeout; custom diagnostic commands remain unqueued
  and cannot bind a verdict.
- **Land requests** — `helm lr` primarily tracks the queue between "reviewed"
  and "on main": which tips are waiting, which have receipts, which reviewer
  owes a verdict, and which recorded proofs no longer resolve. It also closes an
  OPEN BUILD for a non-code delivered report when a typed artifact reference,
  full Helm chat row id, and concise handoff evidence are explicitly recorded —
  a terminal that claims no review or Git land. Git-backed proofs are bound to commits,
  and a commit id is content-addressed over *history* — so `lr refs` audits
  what a history rewrite broke, and `lr migrate` translates it into a sidecar
  that never rewrites an attested tip. When the exact reviewed commit is truly
  gone and no proof can be recovered, `lr abandon` writes off that reviewed work
  explicitly as **ABANDONED — LAND STATE UNKNOWN** instead of inventing a land.
- **Handoffs** — `helm handoff` is the contract across a context window: a
  compaction demands DONE / REMAINING / NEXT before the window closes, and
  refuses to call an empty one satisfied. The next window resumes on the
  handoff, not on a summary of a summary.

### Surfaces

- **The web app** — `helm web` serves four views from one self-contained
  page: your knowledge home, the quota chart + accounts table + credential
  homes, the full sessions browser (search inside transcripts, role-colored
  drawer, one-click resume, team tray), and the config-cascade editor
  (owner commands + rules included; conflict-safe atomic writes and restore).
- **The lineage map** — projects fork, compose, supersede, and launch. helm's
  registry carries those edges and renders the family tree, including
  read-only external nodes, plus a ranked (read-only) "safe to archive and
  why" report.

## Principles

1. **Overlay, not another store.** helm references authoritative homes — your
   repos, your harness session stores, your recall index — it never copies
   them. Every projection is read-only as truth; an edit lands at the source.
2. **The session store is data, not identity.** Working directories are an
   attribute; sessions are the key. helm decodes real paths from inside
   transcripts, never from directory names.
3. **Additive and idempotent.** `helm sync` never deletes a known project.
   Cleanup means *archive with a reference back* — like old photo albums you
   keep — never deletion.
4. **CLI-first, with a web equivalent.** Every curation verb works in a
   terminal and in the browser (`helm web`).
5. **Zero dependencies.** Python standard library only. One checkout, no
   install step: `./bin/helm sync`.

## Quickstart

```console
$ ./bin/helm sync              # discover your projects across all harnesses
$ ./bin/helm projects          # the list, newest activity first
$ ./bin/helm show <project>    # one project's full record (any name from the list)
$ ./bin/helm sessions          # every claude + codex session; resume in one paste
$ ./bin/helm doctor            # health check (read-only)
$ ./bin/helm web               # the same, warm, in a browser
```

### Requirements

**Requires Python 3.9+ on Linux**, and nothing else. That floor is declared
here and in `scripts/install.sh`. The suite is
`python3 -m unittest discover -s tests`; how a change is tested (focused
rounds, then one whole suite at the land gate) is in
[CONTRIBUTING](CONTRIBUTING.md#how-a-change-is-tested). Maintainers test on
newer interpreters; 3.9 is declared, not CI-tested. `tomllib` (3.11+) is used when present
and config validation degrades to a warning without it.

**Linux specifically, and stdlib-only does not mean portable.** helm's chat
lives in RAM at `/dev/shm`, it reads process liveness from `/proc`, locks with
`fcntl`, supervises nodes through user systemd units, and checks ownership with
`os.getuid`. Those are POSIX-and-then-some, not
Python-version, concerns — macOS has no `/dev/shm` and Windows has neither
`fcntl` nor `/proc`. Nothing here is a deliberate exclusion; it is what
"coordination state is memory, disk is a write-behind log" costs on the platform
it was built for. A port is possible and is not currently claimed, tested, or
supported.

### Immutable artifact deployment (artifact only; not cut over)

`scripts/deploy.py` can materialize one committed Helm tree as a real,
non-writable filesystem release. It is a standalone Python 3.9+ stdlib script
and does not import the mutable `helm` package it is deploying:

```console
$ python3 scripts/deploy.py --dry-run
$ python3 scripts/deploy.py                    # -> ~/.local/share/helm/artifacts
$ python3 scripts/deploy.py --root /other/root
$ ~/.local/share/helm/artifacts/bin/helm --version
```

The deployer refuses staged or unstaged tracked changes, resolves the requested
commit and tree once, and materializes only `git archive <commit>` bytes.
Untracked, ignored, and working-tree-only files therefore cannot enter the
artifact. Each `releases/<full-commit>/` directory carries a schema-versioned
manifest with the commit, tree, package version, and deterministic content
digest. A stable regular-file launcher resolves the relative `current` symlink
once and executes the concrete release path with bytecode writes disabled. Old
releases are retained.

**This slice does not cut any existing consumer over.** It does not install on
`PATH`, repoint hooks, change seat lifecycle, or modify systemd, cron, Git hook,
or consumer settings. The existing install path below still links directly to
a mutable checkout; the artifact launcher above is an explicit isolated-runtime
preview until a later, separately reviewed cutover.

Optional mutable-checkout PATH install — the entry script resolves through
symlinks, so putting it on your PATH is the whole install:

```console
$ sh scripts/install.sh            # -> ~/.local/bin/helm, then verifies it runs
$ sh scripts/install.sh --dry-run  # say what would happen, change nothing
$ sh scripts/install.sh --uninstall
```

It checks your Python first (a `helm` on PATH that cannot start is worse than
no `helm`), refuses to overwrite anything it did not create, and finishes by
actually running the installed binary rather than assuming the link works. If
you would rather do it by hand, that is still all it is:

```console
$ ln -s "$PWD/bin/helm" ~/.local/bin/helm
```

**What first run does.** `helm sync` scaffolds `~/.helm` (the global chain +
one dir per discovered project); and if you already use Claude Code, the
typed store **adopts your live memory dir in place** — the resolver reads
`~/.claude/projects/<slug-of-home>/memory` as one more root: same files, no
copy, byte-shape-compatible writes, so your existing hooks keep working
untouched. New helm entries land in `~/.helm`, never there; only the
lifecycle verbs (evidence / supersede / retire) write back wherever an entry
lives, adopted included — and those retire in place, never delete (the file
stays as the record). On a fresh
machine with no harness stores at all, everything
still works: sync scaffolds an empty home, the project list is empty until an
agent runs somewhere, and `helm doctor` tells you exactly what it is (and
isn't) seeing. Quota, recall, and the attestation substrate are all
optional — each degrades to one informative line.

- **Command reference** — every verb with syntax and examples:
  [docs/VERBS.md](docs/VERBS.md). That file is the authoritative verb
  surface; the pillars above are a sample, not the list.
- **Environment** — every `HELM_*` variable (all optional):
  [docs/ENVIRONMENT.md](docs/ENVIRONMENT.md). `HELM_HOME` overrides the
  default `~/.helm`; legacy `MELD_*` spellings, and those of a predecessor
  tool the [local names](docs/ENVIRONMENT.md#local-names) declare, are
  accepted as fallbacks forever.

## Docs

[VERBS](docs/VERBS.md) — the command reference ·
[ENVIRONMENT](docs/ENVIRONMENT.md) — every knob ·
[ARCHITECTURE](docs/ARCHITECTURE.md) — the two-source model ·
[CONCEPTS](docs/CONCEPTS.md) — the axes and laws ·
[DESIGN PHILOSOPHY](docs/DESIGN_PHILOSOPHY.md) — why helm is shaped this way ·
[HOOKS](docs/HOOKS.md) — wiring `helm inject` into your harness ·
[WEB](docs/WEB.md) — the browser surface, API, service unit ·
[ATTESTATION](docs/ATTESTATION.md) — the ledger leg ·
[EVOLUTION](docs/EVOLUTION.md) — the self-evolution loop ·
[COUNCIL EVAL](docs/methodology/COUNCIL_EVAL.md) — cross-family self-evaluation SOP ·
[FAMILY FAILOVER](docs/methodology/MODEL_FAMILY_FAILOVER.md) — model-family failover SOP ·
[AGENTS](AGENTS.md) — working in the codebase (for agents) ·
[NEW AGENT GUIDE](docs/NEW_AGENT_GUIDE.md) — your first 10 minutes as a seat ·
[CONTRIBUTING](CONTRIBUTING.md) — setup, tests, the laws new code obeys

## Status

Young and moving fast. The registry/auto-map, typed store, drain, drift,
lineage, and web views are live; reflexes and the self-evolution loop are in
active development. Issues and harness-format reports welcome — see
[CONTRIBUTING.md](CONTRIBUTING.md) for the on-ramp.

## License

[AGPL-3.0-or-later](LICENSE) — the same copyleft as dregg, the attested-ledger
substrate helm composes with (cv is MIT/Apache-2.0-licensed; helm's copyleft is
its own choice, not required by a dependency). Modified network-served versions
must share source; running helm for yourself, or inside your own fleet, asks
nothing of you.
