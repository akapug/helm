# Helm–Orca seam ownership audit

**Status:** research snapshot, 2026-07-26. Findings and recommended ownership
boundaries; not a claim that the recommended migrations have landed.

## Question

Where has Helm taken ownership of a seam that Orca already owns, and where is
an apparent overlap instead a justified cross-harness policy or adapter?

This audit was requested after four Helm skill files were found to carry the
same public names as version-matched skills shipped by Orca. A harness could
resolve either copy first, so both projects could change the meaning of one
skill name without either side seeing the other's change.

## Scope and evidence

The audit compares the Helm tree at this lane's base with the Orca contract
that actually receives Helm's calls. The request called the baseline “.46”,
but the host is more concerning than one stale label: the launcher selects the
first usable AppImage mount and currently runs a **1.4.152 CLI** against the
**1.4.156 runtime** identified by the live Orca process. Their
`orca agent-context --json` schemas differ. Tagged source locators are used only
where the observed public behavior matches; private RPC behavior is never
inferred from the tag alone. It covers:

- skills and harness guidance;
- repositories, projects, worktrees, and worktree metadata;
- terminal, pane, seat, and session identity;
- inter-agent messaging, task dispatch, gates, and coordinator loops;
- automations, comments, status, and adjacent CLI surfaces;
- a repository-wide completeness sweep for overlaps outside that list.

Every finding names:

1. the state or capability Orca owns;
2. the state or capability Helm built;
3. whether the two can **DISAGREE** or merely duplicate behavior;
4. a verdict and a concrete ownership repair.

The audit does not change skill files or either product's runtime state.

## Boundary rules already declared by Helm

Helm's own architecture supplies the test rather than leaving this audit to
preference:

- Helm is an **overlay, not another store**: every named source remains
  authoritative for its own fact class, projections are read-only as truth, and
  edits land at that source. Helm remains canonical for the project registry,
  lineage, typed knowledge, and authored chain it explicitly owns
  (`README.md:85-92`; `docs/ARCHITECTURE.md:3-40`).
- Helm's local-multiplayer protocol is deliberately adapter-first and does not
  name a terminal, metaharness, model, or CRDT library
  (`docs/ARCHITECTURE.md:123-129`).
- `HELM_METAHARNESS` explicitly selects Orca, Herdr, or native/headless actuation;
  cross-harness policy is therefore legitimate, but an Orca-selected mutation
  must not silently bypass Orca's owner registry (`docs/ENVIRONMENT.md:174-185`).
- Zero third-party Python dependencies and harness-agnostic operation are real
  constraints, but neither justifies a second writer for an Orca-owned fact
  (`README.md:96-99`).

## Verdict rubric

### JUSTIFIED-DIVERGENCE

Helm owns a genuinely cross-harness policy or identity that must work when
Orca is absent. Orca is an adapter or one observed source, not a second writer
of the same fact. The boundary is acceptable only when identities round-trip
and Orca-visible mutations are registered with Orca.

### SHADOW

Helm and Orca can independently author, resolve, or mutate the same named
capability or lifecycle fact. Resolution order, path inference, or eventual
reconciliation decides which version wins. The remedy is one of:

- **defer** — call Orca when Orca owns the mutation or registry;
- **namespace** — keep Helm-specific behavior under a distinct public name;
- **upstream** — contribute Helm's useful expansion to Orca instead of carrying
  a permanent fork.

### CONTRACT-DRIFT / INTEROP-BUG

One owner remains canonical, but the adapter binds the wrong version, identity,
or capability, or splits one operation across two adapters. These are serious
seam defects but do **not** count as ownership shadows; calling them shadows
would hide the actual repair.

## Answer

**Yes, but the true ownership shadows are narrower than the first pass
suggested.** Beyond the four same-name skill forks (already removed on
integration main), two current shadow families remain:

1. Helm privately writes Orca worktree-adoption state while both products can
   independently enact worktree removal;
2. Helm and Orca independently reconcile the same Claude settings hook file.

Four additional high-risk findings are interoperability defects rather than
ownership shadows: a stale Orca CLI/live-runtime pairing and unadvertised private
RPCs; a colliding Helm lease key plus missing foreign identities; path-based
terminal/worktree rebinding; and resume paths that can split one operation across
adapters.

The audit also refutes a broad “Helm duplicated Orca” reading. Helm's
cross-harness lease policy, deterministic seat/lane layout, stable seat/session
binding, human fleet rooms, exact-tip review verdicts, Git-derived landing
projection, credential homes, and single-shot maintenance verbs are legitimate
higher-level or differently-scoped capabilities. They need explicit foreign
keys and owner transactions, not deletion.

## Findings

### Skills and harness guidance

#### S1 — Four public skill-name forks were a SHADOW; main fixed it during the audit

At the audit lane's original base, Helm shipped `computer-use`, `orca-cli`,
`orca-per-workspace-env`, and `orchestration` under the same public `name:` as
Orca's version-matched skills. Helm's copies were expanded forks, so harness
resolution order silently selected which project's meaning won. Orca's current
contract makes the binary authoritative through `orca skills list` and
`orca skills get <name>`; vendoring defeats the exact version matching that
surface exists to provide.

The integration branch fixed this while research was running, in the commit
that dropped the four vendored orca skill forks:
the four forks were deleted and `docs/NEW_AGENT_GUIDE.md` now directs agents to
`orca skills get orca-cli`. Helm's other skill names remain Helm-owned.

**Risk: DISAGREE. Verdict: SHADOW, fixed by defer.** If an Orca-less/offline
fallback is ever required, namespace it (for example `helm-orca-cli`) so it
cannot win Orca's name. Useful Helm expansions belong upstream in Orca or in a
distinct Helm policy skill, never in a permanent same-name fork.

**The defer left no doorway, and that cost a capability for a week.** Deleting
the forks removed the four names from the skills hub, and nothing put Orca's own
stubs there — so `orca-cli` was not loadable by any seat, while this file, the
`fleet-maintenance` skill ("use the `orca-cli` skill primitives"), and
`NEW_AGENT_GUIDE.md` all kept pointing at it. Measured 2026-08-03: a seat at a
full context window reported it *could not* inject `/compact` into its own Orca
pane and asked the owner to type it, because the documented way to reach the
pane had no installed entry point. The premise it violated
(`self-compact-orca-pane-injection`) was live the whole time.

Fixed by LINKING, which is not re-vendoring: `~/.helm/_global/skills-hub` now
carries per-skill symlinks to `orca/skills/{orca-cli,orchestration,computer-use,
orca-per-workspace-env}`. `skillsync` is built for exactly this — "its entries
are per-skill symlinks into a source repo … the source repo stays the physics
owner, helm owns DISTRIBUTION." A symlink cannot drift from the stub, and the
stub defers to the binary, so Orca's version matching survives intact. The
lesson generalizes past skills: **deleting a shadow is only half a fix when the
name was load-bearing — the replacement has to be reachable, not just correct.**

### Repository, project, and worktree ownership

#### W1 — The launcher can pair a stale CLI with a newer runtime

Helm invokes whichever `orca` launcher resolves and also calls private runtime
methods discovered through `orca-runtime.json`, but it records no negotiated
CLI/runtime pair or private capability set (`helm/harness.py:319-365,423-448`).
Measured during this audit, the launcher selects the first usable mount
(`~/.local/bin/orca:34-63`) and chose CLI 1.4.152, while `orca status` and the
runtime PID identified a different mount at 1.4.156. The two public command
schemas already differ, before private RPCs enter the picture.

`orca agent-context --json` is not sufficient to cure this: it advertises public
CLI commands, not runtime ID, app/CLI version, `repo.update`, or
`terminal.resolvePane`.

**Risk: DISAGREE. Classification: CONTRACT-DRIFT, not an ownership shadow.**
First bind the CLI launcher to the mount owned by the connected runtime, not the
first mount in a glob. Then combine the
public CLI schema with `orca status` runtime identity and direct, harmless
capability probes for every private RPC. The durable fix is a runtime-served
capability/version manifest. `helm doctor` should report the exact CLI/runtime
pair and probes it validated.

#### W2 — Repository and worktree identities need foreign keys, not names

Orca identifies a worktree as `<repoId>::<path>` and carries a separate
`instanceId` so delete-and-recreate at one path is a new object. Helm's lane
resource is currently `worktree:<basename(root)>:<lane>` and has no generation
(`helm/work/_lanes.py:45-65`). Two unrelated repositories with the same basename
and lane collide in Helm; reuse of one path remains the same Helm identity while
Orca intentionally remints it.

**Verdict: JUSTIFIED-DIVERGENCE with a Helm key-collision bug and foreign-key
gap.** A logical lane lease and a physical Orca worktree instance are different
facts. Key the Helm lease by a digest of the real Git common directory plus lane;
when Orca is present, retain `repoId`, exact worktree ID, and `instanceId` as
foreign keys without making Orca availability a requirement for the claim
ledger.

#### W3 — Deterministic layout is justified; private adoption is a SHADOW

Helm's `<repo>-wt/<lane>` and `<repo>-wt/seats/<seat>` paths plus `lane/*` and
`seat/*` branches are deterministic cross-harness policy
(`helm/work/_lanes.py:57-65`; `helm/harness.py:158-198,272-316`). Orca chooses
placement from repo/project-host configuration and exposes no public target-path
or adopt verb. Helm therefore creates with Git and privately mutates
`externalWorktreeVisibility` / `importedExternalWorktreePaths`
(`helm/harness.py:450-535`).

**Verdict: JUSTIFIED-DIVERGENCE for layout; SHADOW for private owner writes.**
Upstream an idempotent `orca worktree adopt/import --repo ... --path ...` that
returns worktree and instance IDs. Until then, treat private `repo.update` as an
explicitly capability-gated compatibility path and require a positive
registration receipt before Orca terminal creation.

#### W4 — Orca creation base and Helm integration base are different facts

Orca's `worktreeBaseRef` is a default for future **Orca-created** worktrees.
Helm's base resolver is the cross-harness integration branch used for
Helm-created lanes, merge checks, release, and GC (`helm/vcs.py:347-354`;
`helm/work/_claims.py:35-38`; `helm/work/_gc.py:35-36`). Different values do not
make two writers for one fact, and importing optional Orca configuration into
Helm would let a metaharness change headless/Herdr policy.

**Verdict: JUSTIFIED-DIVERGENCE.** Keep the scopes explicit. Helm lanes use and
stamp Helm's integration intent; Orca-created worktrees retain Orca's create-base
metadata. Compare the two only when a workflow deliberately crosses ownership,
never as automatic precedence.

#### W5 — Exact Orca IDs must replace path reselection

Orca selectors preserve repo identity through exact worktree ID, while Helm
selects its own lanes by repo path/name (`helm/work/_cli.py:67-118`) and the Orca
terminal adapter separately reselects by `path:<cwd>`
(`helm/harness.py:545-570`). Duplicate Orca repo registrations can
expose one physical path under different repo IDs; a path lookup discards that
identity, and T1's unscoped fallback discards it entirely.

**Classification: INTEROP-BUG, not an ownership shadow.** Resolve
adoption/discovery once to Orca's exact worktree ID and use that ID for
subsequent terminal and metadata operations. Orca remains the sole writer for
issue/comment/status/parent metadata.

#### W6 — Helm claims are justified policy, but Orca removal bypasses them

Orca has worktree status but no TTL lease mechanism. Helm's claim ledger binds a
resource, a confirmation nonce (NOT a secret — same-uid seats all read the
ledger; it proves a deliberate holder, nothing more), seat/session, monotonic
expiry, and fence before creating the room (`helm/seats.py:2943-3026`;
`helm/work/_claims.py:18-43`). That is a real cross-harness concurrency policy.

**Verdict: JUSTIFIED-DIVERGENCE with an interoperability gap.** Keep the lease
ledger Helm-owned; do not mirror it as Orca status. Orca removal needs a
pre-delete hook/API that asks Helm whether the exact worktree ID/path has a live
lease and refuses unless an explicit cross-system override is supplied.

#### W7 — Worktree deletion has competing actuators

Orca removal stops managed PTYs, checks lock/cleanliness, updates Orca metadata,
and safely deletes branches. Helm release/GC applies lease, occupant, rescue,
and integration policy, then removes through Git
(`helm/work/_claims.py:46-95`; `helm/work/_gc.py:58-151`). Each can bypass the
other's protection: `orca worktree rm --force` can discard a live Helm-leased
lane, while Helm removal can bypass Orca terminal teardown and immediate owner
metadata cleanup.

**Risk: DISAGREE. Verdict: SHADOW.** Route lifecycle by owner:

- Helm `lane/*`: decide through Helm lease/review policy, then ask Orca to enact
  or reconcile the registered worktree;
- Orca-created worktrees: remove through Orca;
- harness agent scratch: observe only, never infer GC ownership.

This is the same shared-serialization requirement established by the worktree-GC
review: no automatic directory removal may sit outside the one owner transaction.
Helm's safety inventory should use Orca's detected-worktree surface, not only the
visible list, and classify every unfamiliar `.claude/worktrees/*` registration
as `UNKNOWN-HARNESS` rather than dropping it by name pattern.

### Terminal, pane, seat, and session identity

#### T1 — Unscoped terminal fallback is an INTEROP-BUG

**Orca owns:** a terminal's `worktreeId` and the `path:` selector accepted by
`orca terminal create --worktree`. **Helm built:** on
`selector_not_found`, `OrcaAdapter.spawn` retries `terminal create` without a
worktree selector and carries the requested cwd only inside `cd ... && exec`
(`helm/harness.py:537-570`; `tests/test_orca_spawn_selector.py:47-61`).

These facts can **DISAGREE**: Orca may bind the terminal to its inferred current
worktree while the child process runs in Helm's requested checkout. Helm then
records the requested checkout in `spawn.json`, but Orca reports a different
`terminal.worktreeId`. This is the same seam failure that produced the original
`selector_not_found` incident.

**Classification: INTEROP-BUG, not an ownership shadow.** Do not mint an
unscoped Orca terminal. Adopt/register the checkout and retry the same selector;
if registration cannot be proved, fail
loudly or use a genuinely headless adapter. Upstreaming a public
`orca worktree adopt/open --path` (or `worktree create --path`) removes the
private-RPC dependency.

#### T2 — Resume can split spawn and kick across adapters

`orcaadopt.resume` selects an adapter, but calls `sessions.spawn_resume` without
passing it; that function independently calls `harness.detect`. The returned
handle is then kicked through the first adapter
(`helm/orcaadopt.py:400-435`; `helm/sessions.py:462-486`). Under a different
detection preference, Helm can create through Herdr and send an Orca command to
the resulting handle.

**Classification: INTEROP-BUG, not an ownership shadow.** One adapter object
must own the whole operation. Pass it through spawn and kick and return the
actual adapter, with a mixed-detection test.

#### T3 — The identity hierarchy is JUSTIFIED-DIVERGENCE

Orca owns runtime terminal and worktree identity: live `handle`, `ptyId`,
`incarnationId`, durable pane key (`tabId:leafId`), and worktree ID
(`<repo-id>::<absolute-path>`). Helm owns the additional binding from those
facts to a stable seat name and Claude session history
(`helm/harness.py:94-101,407-413`; `helm/seat.py:2705-2770,2877-2901,3334-3396`).

The values can temporarily **DISAGREE** after pane respawn. During this audit,
an Orca update reminted terminal handles and Helm pane reads went blind while
Helm chat remained live—the exact distinction between Orca actuator identity
and Helm's cross-harness protocol. Helm already resolves the durable pane key
back to a current handle and proves the handle/PTY/worktree tuple before
rebinding; that reconciliation is legitimate, not a competing terminal registry.

**Verdict: JUSTIFIED-DIVERGENCE.** Document `spawn.json.handle` as a refreshable
actuator cache, never durable authority. Keep seat/session provenance in Helm;
keep terminal/worktree facts as refreshed Orca observations.

> **AMENDMENT (integrator, 2026-07-26; endorsed by the reviewer as a condition of
> APPROVE).** The sentence above — "that reconciliation is legitimate, not a
> competing terminal registry" — is right about INTENT and overstated about
> FACT. The reconciliation was BROKEN, and by exactly the mechanism this section
> is about: an Orca terminal row carries `orphaned` beside `connected`, and
> `helm/harness.py` `_pane_row` mapped ten fields and not that one. So
> `_pane_live` saw `connected=True`/`writable=True` and answered LIVE for a PTY
> with no renderer, whose reads return `""` with no error.
>
> Measured while this audit was in flight: 34 panes, 30 reading 0 chars with a
> live process behind every one, 22 orphaned-while-connected, and all 4 readable
> panes `orphaned=False`. Orca's `resolvePane` was correct throughout — a seat
> process's own environ carried an `ORCA_TERMINAL_HANDLE` matching what Orca
> resolved, so the process was born on the pane Orca named and was 2d20h alive.
>
> This STRENGTHENS the finding rather than refuting it. "A refreshable actuator
> cache, never durable authority" is the correct rule, and the refresh was LOSSY:
> Helm cached an Orca observation while discarding the field that decided it. The
> reviewer's classification note stands — the INTENT is justified divergence, the
> IMPLEMENTATION was an interop bug that read as justified divergence from the
> design doc, which is precisely how it survived unexamined.
>
> Fixed and landed (`_pane_row` carries `orphaned`; `_pane_live` checks it first;
> the rejection message names the deciding field), gated cross-family at an exact
> tip. The general form is worth carrying out of this audit: when a check answers
> confidently WRONG about a foreign system, diff that system's RAW row against
> the row your adapter keeps, before theorising about the foreign system at all.

#### T4 — The terminal adapter is a justified thin subset

`OrcaAdapter` maps Helm's cross-harness create/read/send/stop capabilities to
`orca terminal create/read/send/close` without storing a second terminal list
(`helm/harness.py:537-590`; `tests/test_seat_spawn.py:708-730`). Orca-only
features such as cursored reads, interrupt sends, tab closes, splits, and focus
remain Orca capabilities rather than being emulated.

**Risk: duplicate behavior, not duplicate truth. Verdict:
JUSTIFIED-DIVERGENCE.** Keep the adapter small; add optional capabilities when a
caller actually needs them rather than widening the uniform core or recreating
Orca behavior.

#### T5 — Direct Orca panes need explicit adoption, never title inference

An Orca terminal title is mutable presentation, not seat identity. Helm
correctly refuses to treat it as one and only adopts a direct pane when stronger
session/roster evidence exists (`helm/seats.py:180-217`;
`helm/orcaadopt.py:317-343`; `helm/seat.py:2379-2391,3525-3531`).

**Verdict: JUSTIFIED-DIVERGENCE with a capability gap.** Add an explicit
`helm seat register <seat> --terminal <handle>` flow that proves
handle -> pane key -> process before binding, or let Orca terminal creation pass
the Helm seat identity. Never infer identity from the tab title.

### Orchestration, messaging, tasks, and gates

#### O1 — Helm chat and Orca orchestration mail are different protocols

Helm chat owns named-seat, cross-harness, human-visible rooms/DMs, presence, and
signed dregg delivery (`helm/chat.py:12-32,54-90`). Orca orchestration mail owns
terminal-handle-addressed task messages with lifecycle types such as dispatch,
worker completion, decision gate, and heartbeat.

Both can carry text point-to-point, but they do not author the same fact: Helm's
identity and receipt semantics survive outside Orca, while Orca messages belong
to one orchestration database and runtime terminal namespace.

**Verdict: JUSTIFIED-DIVERGENCE.** Keep dregg-primary Helm delivery canonical for
Helm rooms and DMs. An optional bridge may attach Orca `msg_id` foreign links or
translate explicitly typed orchestration events, but Orca is never a prerequisite
for direct Helm delivery and neither read state is inferred from the other.

#### O2 — Unread state is different and must stay namespaced

Helm's read path is a non-destructive room scan plus one owner-room watermark
(`helm/chat.py:1616-1631,1734-1756,2422-2449`). Orca tracks per-message,
per-terminal unread state; `orchestration.check --unread` consumes it while
`--peek` does not.

**Verdict: JUSTIFIED-DIVERGENCE.** Name the facts `room owner-unread` and
`terminal mailbox unread`; never aggregate or compare them.

#### O3 — Exact-tip obligations and execution tasks are related, not identical

Helm's append-only dispatch ledger binds a cross-harness recipient, lane, exact
Git tip, `build|review` kind, deadline, repository identity, evidence, and signed
delivery (`helm/dispatches.py:2-20,34-52,531-547,645-718`). Orca tasks and
dispatch contexts own generic execution state inside Orca: pending, ready,
dispatched, completed, failed, or blocked.

One real-world job may participate in both, but completion of an Orca task does
not close an exact-tip review obligation, and a Helm verdict does not complete an
Orca execution DAG.

**Verdict: JUSTIFIED-DIVERGENCE.** Neither ID is a prerequisite for the other.
When a workflow uses both, retain optional `task_id` / `ctx_id` foreign links and
surface contradictions; keep each native lifecycle authoritative for its own
fact. Useful exact-tip metadata may be upstreamed to Orca without demoting Helm's
cross-harness obligation protocol.

#### O4 — Review verdicts are not decision gates

A Helm verdict is immutable evidence that one exact dispatched Git tip received
`approve|fix|supersede`, and stale tips are rejected
(`helm/dispatches.py:206-226,721-773`). An Orca decision gate is a temporary
question blocking a task; resolving it returns the task to ready.

**Verdict: JUSTIFIED-DIVERGENCE.** Namespace `review_verdict` versus
`decision_gate`; a resolved gate is never approval. Upstream a typed exact-tip
review result if Orca is to display this evidence.

#### O5 — LR is Git-derived landing evidence, not task status

Helm LR deliberately reuses the dispatch ID and derives states such as
`READY`, `MERGED_LOCAL`, and `LANDED` by joining dispatch evidence with Git
(`helm/landreq.py:2-10,670-804`). Orca task completion does not attest that a
reviewed commit reached local or upstream trunk.

**Verdict: JUSTIFIED-DIVERGENCE.** Keep Git as landing authority and LR as a
cross-harness projection keyed to the exact reviewed tip. When an Orca task also
exists, its ID is an optional foreign link; export `reviewed_tip`,
`local_landed`, `upstream_landed`, and contradiction evidence as Orca result
metadata when useful.

#### O6 — Orca owns its coordinator substrate, not coordination in the abstract

Helm currently has manual dispatch and LR reconciliation but no coordinator-run
store or DAG scheduler. Orca owns `orchestration.run/runStop`, concurrency, gate
processing, and run IDs inside Orca's task database.

**Verdict: JUSTIFIED-DIVERGENCE, no current shadow.** Helm must not recreate
Orca's task database or run state. A future metaharness-agnostic coordinator may
still be legitimate across Orca, Herdr, and headless seats if it schedules at the
cross-harness policy layer and delegates Orca-local execution to Orca.

### Automations, comments, and adjacent surfaces

No current mutator collision was found in this group.

- Orca automations are durable scheduled-agent objects with prompt, provider,
  target workspace, schedule, enablement, and run history. Helm's
  `autocompact`, `idle_dispatch`, corpus, watchdog, and keepalive paths are
  single-shot maintenance verbs whose cadence is owned by systemd/cron or the
  caller (`helm/autocompact.py:48-54`; `helm/idle_dispatch.py:47-53`;
  `helm/corpus.py:246-252`). **Verdict: JUSTIFIED-DIVERGENCE.** If Orca later
  schedules one of the same Helm verbs, disable the external timer so one
  scheduler owns cadence.
- Orca's worktree `comment` and `workspaceStatus` are authored workflow
  metadata. Helm's `DIRTY`, `OCCUPIED`, conflicts, claim holder, and liveness
  are measured checkout state (`helm/work/_lanes.py:432-459`;
  `helm/work/_cli.py:178-208`). **Verdict: no overlap.** Do not translate one
  into the other implicitly.
- Helm project-lineage `notes` and Orca worktree comments have different
  identity and lifecycle. Cross-link by Orca worktree ID if needed; never mirror
  mutable text.

### Completeness sweep

#### C1 — Project identity is justified; host availability needs foreign keys

Helm explicitly owns a cross-harness project catalog and currently keys project
identity by canonical local path (`docs/ARCHITECTURE.md:30-40`;
`helm/registry.py:160-181`). Orca owns durable provider/project IDs plus a
separate host-setup record saying whether that project is available at a
concrete path (`orca project list`, `orca project setups`, and
`project setup-*`; `stablyai/orca@v1.4.143 src/cli/specs/project.ts:4-40`).

The catalogs may legitimately have different membership: Helm observes every
harness, while Orca lists projects configured for Orca. The dangerous case is
joining by display name/path and treating a Helm observation as proof that an
Orca project setup is ready. Orca can say `not-set-up`, `error`, or absent while
Helm has recent sessions at the path.

**Verdict: JUSTIFIED-DIVERGENCE with a foreign-key/inference gap.** Keep Helm's
cross-harness records canonical for Helm's project fact class and add optional
`orca_project_id + host_setup_id` bindings before Orca-selected mutations. A
Helm path observation must never be presented as Orca setup readiness.

#### C2 — Two hook managers write the same Claude settings file

Helm installs and reconciles six Claude hook events across credential homes and
seat configs (`helm/hooks.py:1-35,50-97`). Orca's less-visible
`orca agent hooks on|off|status` surface also edits `~/.claude/settings.json`,
including overlapping `UserPromptSubmit`, `Stop`, and `PostToolUse` events
(`stablyai/orca@v1.4.143 src/main/claude/hook-settings.ts:29-61,95-113`). Both preserve
foreign entries during a serial merge, but an advisory lock owned by only one
product cannot bind the other.

Helm's three settings writers now use content-revision compare-and-swap: every
attempt reads exact bytes + opaque revision, re-derives its full merge, commits
only against that revision, re-reads the committed revision, and retries at most
three times. A foreign pre-commit or immediate post-commit write is preserved;
Helm never restores an older backup over it. This closes the direction in which
Helm was the overwriter. It does **not** make an Orca read-before/late-write safe:
until Orca adopts the same expected-revision rule, Orca can still write a stale
after-image over Helm. `orca agent hooks off` also means only Orca-managed status
hooks, not every agent hook.

**Verdict: SHADOW at the configuration-mutation seam, one direction hardened.**
Upstream the same bounded exact-revision CAS convention to Orca; retain
per-product ownership markers, and make both status commands name their own
estate rather than claiming the whole file. A shared advisory lock may be a
courtesy between cooperative writers, never the correctness dependency.

#### C3 — Candidate surfaces that are not shadows

- **Claude Agent Teams:** Orca adapts Claude's native experimental team/tmux
  protocol into Orca panes. Helm coordinates independently launched,
  heterogeneous seats; it does not implement that protocol.
- **Command discovery:** `orca agent-context` is Orca's executable CLI schema;
  `helm capabilities` is a trigger index for Helm primitives and optional
  powerpacks. They answer different questions.
- **Resource diagnostics:** `orca diagnostics memory` measures app, host, and
  terminal resource state. Helm's probe is a model-proxy CPU canary. Keep the
  labels distinct; neither is authority for the other measurement.

## Ownership map and recommended sequence

| Domain | Canonical owner | Helm's proper role | Orca's proper role |
|---|---|---|---|
| Orca skills/CLI contract | Orca binary | load version-matched guides; namespace Helm policy | publish executable schema and guides |
| Cross-harness project registry and lineage | Helm | own observations, knowledge scope, and authored edges | supply optional foreign bindings |
| Orca provider project, repo registration, host setup | Orca | retain foreign-ID crosswalk; never infer readiness | own provider identity and setup lifecycle |
| Physical worktree membership | Git; Orca owns enrichment | apply Helm lane policy to Helm lanes | own worktree/instance IDs, metadata, PTY teardown |
| Lane claim/lease | Helm | sole policy writer and decision authority | consult as a removal blocker |
| Seat-home/lane layout | Helm | own deterministic paths and branch namespace | publicly adopt/import and display them |
| Orca terminal/pane/worktree runtime identity | Orca | cache and reconcile Orca observations | own handles, pane keys, worktree IDs |
| Seat name and harness session binding | Helm | own stable cross-harness identity/provenance | expose runtime evidence used to prove it |
| Cross-harness rooms, DMs, presence | Helm/dregg | own named-seat delivery and receipts | optional typed bridge, never prerequisite |
| Orca terminal mail, execution tasks, coordinator | Orca | optional foreign links; no duplicate Orca run state | own mail, DAG, dispatch context, gates, runs |
| Exact-tip dispatch/review and Git landing evidence | Helm + Git | own signed obligation/verdict/LR projection | display optional linked result metadata |
| Claude settings hook file | Claude harness; shared transaction | own only Helm-marked entries | own only Orca-marked entries |
| Orca automation objects | Orca | expose idempotent verbs an automation may call | own schedule and run history |

Recommended sequence:

1. **Bind one Orca CLI/runtime pair and probe capabilities.** Select the CLI
   mount owned by the connected runtime, record both versions/runtime ID, use
   the public command schema for CLI calls, directly probe private RPCs, and use
   exact Orca worktree IDs rather than path reselection.
2. **Remove identity-destroying fallbacks.** Delete the unscoped terminal-create
   retry; require verified adoption or use a clearly headless path. Pass one
   adapter object through resume spawn and kick.
3. **Build one worktree lifecycle transaction.** Use Helm claims/policy for
   Helm lanes, Orca actuation/reconciliation for Orca-visible worktrees, one
   shared deletion lock, and owner-routed GC. Add the repo-common-dir lease key,
   Orca instance foreign key, stamped base intent, and unknown-harness inventory.
4. **Add project/setup crosswalks.** Distinguish Helm's cross-harness project
   fact class from Orca-scoped provider project/repo/host-setup readiness; no
   Orca mutation may join by display name or path alone.
5. **Namespace orchestration and bridge only deliberately.** Helm/dregg owns
   cross-harness rooms, DMs, exact-tip obligations, review, and landing evidence;
   Orca owns its typed terminal mail, task DB, and coordinator runs. Carry foreign
   message/task/context IDs only when one workflow explicitly uses both.
6. **CAS hook writers by exact revision.** Upstream Helm's bounded
   read-rederive-`expected_revision`-verify loop to Orca; preserve foreign writes,
   refuse after three conflicts, never restore an older backup over a newer file,
   and make each status command report only its marked estate.
7. **Upstream missing owner APIs.** Highest leverage: public worktree
   adopt/import with exact ID receipt, pre-delete policy hook, terminal identity
   env/adoption, and typed exact-tip review metadata.
8. **Keep the skill fix closed.** Load Orca's version-matched skills; upstream
   useful expansions or use a non-colliding Helm namespace.

## Evidence limits

- Orca was running and its runtime/graph reported ready during the audit. Public
  contract claims come from CLI help, `agent-context`, status, and
  version-matched skill guides. W1/W3 additionally use explicitly
  **non-contractual compatibility evidence**: launcher mount selection, embedded
  package versions, runtime PID identity, harmless private-RPC probes, and
  observed `repo.update` behavior. Those observations justify guards and
  upstream API requests, not promises about future Orca internals.
- Orca's registries and live terminal set are mutable snapshots. Concrete
  disagreement scenarios are supported by code paths and public identities,
  not a claim that every scenario was active at the instant of the audit.
- The integration branch moved while research was running. Findings distinguish
  lane-base observations from fixes already on integration main; the delivered
  commit history, rather than prose, records its eventual rebase point.
