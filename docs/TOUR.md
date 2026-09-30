# A tour of helm

The [README](../README.md) says what helm is and whether it fits. This page
walks through what it does, grouped by what each part serves: the
**knowledge** your agents read, the **cockpit** you steer from, the **fleet**
of agents, and the **surfaces** that show it all.

Every verb named here prints its full usage with `helm <verb> --help`.
[VERBS](VERBS.md) is the complete reference.

## Knowledge: what every agent reads

- **The knowledge chain, per project.** `premises / heuristics / lexicon /
  prd / journal / evals / archive` live under `~/.helm/<project>/`. They follow
  the normal development cycle: prior art feeds specs, the build happens in
  the repo, evals and journals record what happened, and the archive keeps
  history without clutter.
- **One typed store, one resolver.** Beliefs (priors, with a confidence that
  updates on evidence), vocabulary (your lexicon: the terms you coined),
  heuristics (moves you apply) and reference material sit behind one
  just-in-time resolver. An entry surfaces when a prompt touches it; nothing
  fills every turn by default. Salience is the scarce resource.
  `helm store resolve` shows what a prompt would pull.
  [CONCEPTS](CONCEPTS.md) explains the store classes and load classes.
- **The drain.** Raw memory intake is a transient inbox, never a
  destination. `helm drain` classifies raw entries, routes them into their
  typed homes and archives the source. Nothing is deleted, and everything
  stays retrievable.
- **The drift report.** Beliefs carry a confidence and an evidence log. When
  the evidence starts to contradict something you hold as true, `helm drift`
  says so, and only then. No news is silence.
  [EVOLUTION](EVOLUTION.md) describes the whole self-evolution loop.
- **Know your user.** A first-class profile of how you like to work: voice,
  autonomy, standing corrections, goals. `helm interview` takes five minutes,
  and every agent that reads the store works better with you for it.
- **Attested truths.** `helm premise` captures a certainty into the store and
  appends a record to a tamper-evident hash chain; `helm premise-check`
  verifies it again and quotes the finality tier. A belief is superseded as a
  provable chain, never deleted. [ATTESTATION](ATTESTATION.md) has the
  details, including the optional dregg anchor.

The store reaches agents through the Claude Code `UserPromptSubmit` hook:
`helm inject` adds the relevant entries to each turn. [HOOKS](HOOKS.md) is
the wiring reference.

## Cockpit: sessions, accounts, credentials

- **`helm projects`.** Your real project list, found from what your agents
  actually did, across every harness (Claude Code, Codex, OpenCode). Worktrees
  collapse into their repo, scratch directories are filtered out, and a
  project you only touched in one harness still counts.
- **`helm sessions`.** Every local Claude Code and Codex session, grouped by
  project, with the exact resume command ready to paste (it is the harness's
  own CLI, with no wrapper). Those two harnesses are the session catalog's
  whole scope: an OpenCode or pi session counts toward `helm projects`, but
  does not appear here. `helm search` searches inside transcripts,
  `helm transcript` reads one in windows, `helm rehome` moves a session to a
  new working directory, and `helm prune` makes a resume-optimized copy.
- **Accounts and quota.** `helm creds` is the live scorecard: headroom, reset
  windows, and use-it-or-lose-it verdicts. `helm burn` reads the fire danger
  for each model family, and `helm route <kind>` says which seat should take a
  piece of work right now. `helm swap` rescues a seat that ran dry, with exact
  commands to resume under a healthier account. `helm homes` manages
  credential homes safely: helm prepares, and you run every login.
- **Safe logins.** `helm cred` makes `/login` safe. It reads which account
  each credential home's metadata names (`.claude.json` `oauthAccount`, with
  the token lineage beside that label; never the directory name), snapshots
  credentials at mode `0600` so that a login that lands in a pinned session's
  home can be reversed, and heals a drifted home. It is a dry run by default,
  and it refuses while a live session holds the home.
- **Corpus and attribution.** `helm corpus` archives the Claude, Codex and
  `/tmp`-estate raw transcripts into a dated, append-only archive: your
  training corpus, copy-only and incremental. OpenCode and pi transcripts are
  not collected. `helm attribute` rolls up token effort by project, model and
  credential, and `helm who` maps each live process to the credential it is
  using.

## Fleet: many agents at once

- **The fleet room.** `helm chat` is a group chat in RAM that includes the
  human. Messages reach an agent mid-turn, between tool calls (`@seat`
  mentions and owner posts), through the Claude Code `PostToolUse` hook. The
  room carries roster presence (`helm chat seats`), advisory claim leases, and
  a doorbell: an idle seat with an armed beacon wakes when a row addressed to
  it arrives. `helm launch` starts a Claude Code session that is already
  seated. Chat lives in `/dev/shm` on purpose: coordination state is memory
  that every turn reads, and disk is the write-behind log, never the bus.
  Turns can be signed through dregg when a node is reachable.
- **Seats.** `helm seat` mints a live agent of a given model family, with its
  own pane, git worktree, proxy and credential pool, and registers it so that
  helm can find it again. `helm seat spawn|resume|where|status|doctor` is the
  lifecycle; `helm seat doctor --ensure` respawns a proxy that died quietly.
  A seat that compacts restarts its own turn loop, instead of sitting idle
  until a human types into its pane.
- **Lanes.** `helm work claim <lane>` gives a seat a lease, a guarded
  worktree and a `lane/<lane>` branch in one verb; `helm work release` hands
  it back. Nobody edits the shared checkout by hand.
- **The dispatch ledger.** Who owes what is a durable ledger, not a chat
  scroll. `helm dispatch send` books work or a review against an exact commit,
  `helm dispatch list --mine --open` is what a seat owes, and a review closes
  only through `helm dispatch verdict` on the exact reviewed tip.
- **Review, gates and landing.** A change lands only after a read by a
  reader who wrote none of it, and one whole suite on the exact tree that
  lands (as parallel slices while the gate canary stands, serially
  otherwise). [LANDING](LANDING.md) covers the review rule, the whole-suite
  gates, trains and land requests.
- **Handoffs.** `helm handoff` is the contract across a context window. A
  compaction demands DONE / REMAINING / NEXT before the window closes, and it
  refuses to call an empty handoff satisfied. The next window resumes on the
  handoff, not on a summary of a summary.
- **Local multiplayer.** `helm multiplayer` is a blind relay with presence
  that expires, for humans and agents on one machine.
  [MULTIPLAYER](MULTIPLAYER.md) has the contract, and
  [MULTIPLAYER_TESTDRIVE](MULTIPLAYER_TESTDRIVE.md) a two-terminal walkthrough.
- **Driven remote sessions.** helm drives Claude Code cloud sessions
  headlessly, as an outside review force in a fresh context and as a build
  lane whose pushed branch comes back as a local lane. `helm remote` is the
  relay. [REMOTE_SESSIONS](REMOTE_SESSIONS.md) describes it.

An agent that joins a fleet reads [NEW_AGENT_GUIDE](NEW_AGENT_GUIDE.md) first:
identity, the room, lanes, the review bar and routing, in ten minutes.

## Surfaces

- **The web app.** `helm web` serves the station as one self-contained page
  at `http://127.0.0.1:7433` by default: a Home screen and four pages, Work,
  Chat, Fleet and History. Every view has a terminal equivalent, and every
  browser mutation maps to a CLI verb. [WEB](WEB.md) covers the pages, the
  API and the service unit.
- **The lineage map.** Projects fork, compose, supersede and launch one
  another. `helm lineage` carries those edges and renders the family tree,
  including read-only external nodes, plus a ranked, read-only "safe to
  archive, and why" report.
- **The operator's brief.** `helm brief` is the morning brief: sessions,
  the knowledge delta, seats and the gates that wait on the operator. It is
  read-only.
- **The self-index.** `helm capabilities` lists what helm gives you, verb by
  verb, with what each one is wired through and whether that is live or
  absent on this machine. `helm doctor` is the read-only health report.
