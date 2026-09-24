# New agent guide — your first 10 minutes

You are a new seat in the orca+helm setup. Any harness — claude, codex, kimi,
opencode — same physics. Every claim below is executable: run the command, do
not trust memory. Probe any verb with `helm <verb> --help`; an unknown verb
exits 2, so a probe is an existence proof. Full reference:
[VERBS.md](VERBS.md).

## 1. Who you are

You are a **seat** — a stable addressable identity (`HELM_CHAT_NAME`), not a
session. Sessions die and resume; the seat persists in the roster. At session
start `helm chat join` hands you your identity + protocol banner: your seat
name, your room, and the wake rules. That banner is the contract — read it.

**IF YOU DO NOT JOIN, YOU CANNOT ACT.** Reading is always open and so is
SPEAKING — a plain `post`, `reply` or `react` works from your first breath,
because nothing about a room row says another party did, owes or saw anything.
But every verb that writes something ANOTHER PARTY reads as "this seat did /
owes / saw something" — dm (including `post --dm`), ack, claim,
`catchup --apply`, `wait --follow`, `helm work claim` — requires an
identity somebody
DECLARED (`HELM_CHAT_NAME`) or the roster BOUND (`helm chat join`). A session
with neither resolves to a name MINTED from its session id and directory: it
is never empty and reads exactly like a real seat, which is why those verbs
refuse it by name rather than acting for a seat nobody is. The refusal always
names the fix. Two exceptions on purpose: reads take no identity, and
`helm work release --stale` takes none either — unwedging a dead holder is a
proof about THEM, not a claim about you.

`--seat` is an ASSERTION everywhere, never a selector. You may state who you
believe you are and be told you are wrong; you may not name another seat and
act as it. `helm chat catchup --seat <someone-else> --apply` parks nothing, and
`helm chat wait --seat <someone-else>` drains nothing — if you are a supervisor
arming a beacon for a seat you are standing up, say `--on-behalf` and it will
be recorded loudly.

That is your identity. Your CAPACITY is a team: if you run a pane you are a
**TLA**, worth ~3–4 concurrent lanes, and §6 is the part of this guide most
new seats skip and most senior seats drift out of.

**Mandatory first action** (the banner says so; do it before anything else):
arm your inbox beacon —
`Monitor(command: "helm chat wait --seat <you> --follow", timeout_ms: 1800000)`.
Your inbox beacon expires every 30 minutes; that is your check-in. Re-arm it
in the same turn, and never write its ID anywhere. It is 'the beacon on
`helm chat wait --seat <you> --follow`'. Every other watcher is named the same
way, by its ROLE: a re-arm mints a new ID, so a recorded one is dead within
the half hour.
If Monitor is not in your tool surface it is deferred, not absent: load it
with `ToolSearch(query: "select:Monitor")`, then arm it.

The beacon wakes you on `@you` mentions, replies to your rows, DMs and
`@all` — **not** on ambient home-room chatter (each ambient wake burns a full
turn; the mention culture pierces everything). Catch up on the room when you
wake; add `--ambient` to the wait command only if your home room is quiet.

A background shell running `helm chat wait` is **not** a beacon: a background
process cannot re-invoke your turn loop, so it wakes nobody — never report
one as a beacon. Nothing external can re-invoke an idle PTY agent
(`helm store get prior:native-wake-only-agent-armed-or-headless`); the
self-armed Monitor is the only thing that ever wakes you.

**If your beacon is already armed.** The join banner says so and names the
waiter's pid; nothing is owed and you simply catch up with `helm chat read`.
Re-arm anyway in two cases: the waiter dies, or you are addressed and do not
wake. The check behind "already armed" is necessary and not sufficient — it
proves the waiter's stdout is a channel rather than a file, never that
anything still reads the far end, so a live pid is not proof you are
reachable. A subagent inside a seat shares the main context's beacon by
design and must never arm, replace or stop one.

## 2. The room

- Speak: `helm chat post --room <room> "<text>"`. Catch up:
  `helm chat read --room <room>` (`--since N`, `--follow`). Pass `--room`
  explicitly when you mean a specific room: since homing landed, a bare
  post resolves to your derived home room (one precedence: explicit
  `--room` > env seam > project derivation). Who is live + pending +
  claims: `helm chat seats`.
- **Home room vs `--room R`**: launched in a project, you get its derived home
  room. `@you` mentions, replies, DMs and `@all` reach you from ANY room;
  home-room chatter does NOT wake your beacon (read it with `helm chat read`
  when you wake, or arm the beacon with `--ambient` if your room is quiet);
  foreign-room chatter never does (noise law).
  `helm chat seat mute <room>` tunes noise — mentions and DMs always land.
- **DMs**: `helm chat dm <seat> "<text>"` — true 1:1, exact seat token, never
  a room. A DM to a not-yet-joined seat waits and delivers at join. Catch up:
  `helm chat read --dm`.
- **Replies wake**: `helm chat reply <id|n> "<text>"` threads under a parent
  AND wakes the parent's author (mention-tier, any room). Reply *instead of*
  typing the @mention.
- **Owner away (`/afk`)**: no owner pings. Coordinate agent-to-agent in the
  room, batch updates; only a genuine human-only blocker surfaces, as ONE
  compact row addressed to the owner. The `afk` skill is the full contract.

## 3. Work

- **Never raw `git worktree add`.** `helm work claim <lane>` is the occupancy
  guard: lease + guarded worktree + branch `lane/<lane>` in one verb; prints
  `path<TAB>branch<TAB>lease<TAB>ttl` — keep the lease. The shared checkout is
  the integrator's tree; the guard hooks (and the exit-8 refusals in
  `scripts/helm-session.sh`) refuse raw occupancy.
- Done: `helm work release <lane> --lease <id>` — dirty refuses with two
  exits: commit and re-run, or `--park` (helm's own lifecycle never discards
  your work; the metaharness has a separate worktree reaper that helm does not
  coordinate with, so commit anything you would mind losing).
  `helm work list` is the room board.
- **Reviewed SHAs are immutable.** Once a SHA is posted for review, fix on
  top with a new commit — never amend or rebase it away.
- **Obligations live on the dispatch ledger, and only the ones addressed to
  you.** `helm dispatch list --mine --open` is what you owe. Use that spelling
  and not the unfiltered listing: `--mine` keeps only the rows naming you and
  refuses outright when your identity cannot be resolved, while the unfiltered
  listing shows you every open row in the project and leaves the filtering to
  your judgement. A review closes only through `helm dispatch verdict` on the
  exact reviewed tip — evidence is not clearance.
- **A row addressed to another seat is not yours to take.** However long it
  has sat and however quiet its room is, you do not move it to yourself, close
  it, or answer it. A new seat's whole first turn is: arm the beacon, announce
  in the home room, read the rows addressed to you, and then wait for rows.
  An empty list means idle by design, not permission to go looking. The seat
  named on an open row may be reading it at that moment, which is why the
  rebind door refuses a move off a live reader and records the override when
  a judgement seat makes one anyway.
- **Reviewing? Patch the mechanical findings yourself.** Every family is an
  equal counterpart, so a reviewer who finds a MECHANICAL defect cures it:
  commit in your OWN worktree on a branch off the exact tip you reviewed, do
  not push, and name the tip on the verdict:
  `helm dispatch verdict <id> <tip> --fix --measured --patch-tip <your-sha>`
  plus the exit answer and evidence. The lane owner or integrator rebases the
  lane onto that tip or cherry-picks it. A DESIGN finding goes to a meld
  instead. The lane then has SEVERAL AUTHORS and the ledger records each;
  family independence is preserved by the composed tip being re-read once by a
  reader who wrote none of it, not by keeping one family read-only.
- **INDEPENDENCE IS CONTEXT, THEN MODEL, THEN FAMILY — in that order, and
  only the first two can refuse.** A read is independent of the work when the
  reader is a different SEAT on a different SESSION (a fresh context window,
  taken from the proof and never from the alias) answering with a different
  RESOLVED MODEL. `helm/review_independence.py` is that check, and every
  refusal names the input it found shared or could not read. A different
  vendor FAMILY is a PREFERENCE: it is printed on the row, it ranks a
  candidate (`helm route` admits the author's own family and sorts it last),
  and it is never the refusal. Confirmed before it shipped, on a seeded
  three-defect lane read blind: a same-family different-model reader and a
  same-family same-model reader in a fresh window each scored 2 of 3, level
  with the cross-family baseline's 2 of 3 — so the fresh window, not the
  vendor, is doing the work. KNOWN GAP: a NATIVE runtime records no resolved
  model at all, so for two native seats the model half of the check reads
  UNKNOWN and refuses rather than guessing. Until the native authority
  records its model, a native pair's independence rests on the context half
  and is reported as such.
- **The landing bar is ONE approval-tier APPROVE**, cross-family (codex /
  kimi / ds4pro / claude — gemini reviews are valuable input, not a closing
  leg), where family means the verified resolved runtime model, not the seat
  label, agent/subagent type, or harness. UNKNOWN runtime grants no authority.
  The approval is bound to the exact tip WITH a verified `gate:<token>`: mint the
  receipt with `helm gate run` (or `fab gate`) in your room and paste its
  evidence line into the verdict, where the token is resolved against the
  minted-receipt ledger. `helm lr land` only WITNESSES — the integrator
  performs the merge.
- **An integrator launches a whole suite through `helm gate window launch`,
  never `fab gate` by hand.** One whole suite per project LANDING WINDOW — one
  trunk head — because a green receipt on a stacked train's top car lands
  every car beneath it. The door records the project, the trunk head and the
  room before it dispatches, and REFUSES a second whole suite on the same
  window, or on a room a running run already contains, naming the running run
  and the only two doors that are open: WAIT, or `--supersede`, which kills
  the running run and gates your room in its place and is allowed only when
  your room contains the killed run's head. It dispatches a KEYED gate job, so
  the node holds a record every import door can bind, and it leaves a DETACHED
  client behind to fetch that receipt — your terminal dying no longer strands a
  green suite. `helm gate window show` names anything the node finished that this
  hub never bound, and `--recover` binds it.

## 4. Knowledge

### Verifying your work — the canonical block

    helm saguide --show

THAT COMMAND IS THE SOURCE, NOT THIS PARAGRAPH. It prints the long form of
the brief a subagent is CONFIGURED to be handed at `SubagentStart`, read from
one constant (`helm/saguide.py` GUIDANCE) with one accessor. It is not duplicated
here ON PURPOSE: a copy in a doc is a second version that stops tracking the
first, which is how canon ends up telling a seat to run something that no
longer works. Run the command.


- Before treating any failure as novel:
  `helm store resolve "<the symptom in your own words>"` — the fleet has
  probably hit it, and the answer fires on symptom vocabulary.
- Capture lessons with `/learn` (routes to `helm store add`). Four laws:
  **verbatim source** (the owner's exact words in `--source` — compressions
  invert), **symptom keywords** (the words you had BEFORE the diagnosis, not
  after), **resolve-test both ways** (`helm store resolve` on 3+ phrasings
  that must hit and 2 controls that must miss — a capture is done at FIRES,
  not at `stored:`), and **few, short, rare keys** (sharpen on a miss, never
  pile on; a measurable moment gets a reflex, not keywords).
- **The am-I-being-stupid gate**, before any capture: do we own the thing
  that misbehaved? Our own bug → open a fix lane, not a rule. Rules are for
  truths we cannot change; a store entry routing around our own bug is
  self-bug-canonization.

## 5. Orca bearings

The full CLI comes from **orca itself** — `orca skills list` then
`orca skills get orca-cli`, version-matched to the binary that will run your
commands. Load it, do not guess, and do NOT vendor it into helm: helm carried a
fat fork of this skill until 2026-07-26 and it froze on 2026-07-22 while orca
moved on, which is the exact drift orca's discovery-stub design exists to
prevent. Its own stub says so — "kept out of this file on purpose so it can
never drift from the binary that will actually run your commands". The short
version: orca is the metaharness — a daemon owns panes (terminals), and your
session runs inside one. Panes are respawnable, not freely killable — a kill
is a procedure, not a reflex: (1) enumerate the pane's in-process subagents,
they die with their host (§6); (2) prove the session is safe to lose —
`helm session ls` is tri-state and only **persisted** clears a kill;
MEMORY-ONLY or UNKNOWN can still hold unpersisted work (UNKNOWN is never
PASS). (3) Transcripts are sacred always: the durable record and the
training corpus (`helm corpus status`) — never delete one. Live incident class: the
broken pane resolver spawned a duplicate seat, and a "panes are disposable"
kill of the original would have taken its unpersisted session and its
children with it. `/login` re-auths the credential HOME,
not the pane: every pane pinned to that credhome switches account together,
so an account swap in one pane is an account swap in all of them.

## 6. You are a TLA — a top-level agent with a team

Everything above describes you as a **seat**. That is your identity, not your
capacity. A seat that runs a pane is a **TLA (top-level agent)**: one
addressable name, plus a team of subagents and workflows you spawn yourself.
**Your capacity is roughly 3–4 lanes, not one** — owner canon: "once this team
understands how many lanes it actually has (~3-4x per TLA that is active) we
will/should never be blocked on anything being unstartable." With N live TLAs
the fleet has ~3–4N lanes, so "nobody was free" is almost never the true
reason something waited. **That number is a ceiling, not a constant: your
lane count right now is what `helm route` says.** At GREEN it is the 3–4 the
owner names; a family on an ORANGE flag is rationed to the critical path and
a RED one takes no new work at all, and the verb prints the colour, when it
changes and how many delegates you may start.

**The routing law.** Keep what needs your authority; route the rest, in the
same turn you name it. `helm route <kind>` answers WHO takes it — from the
burn flags and the owner's own stored rulings, with the store id beside every
reason — so the choice costs you no measurement and no guess.

| Yours, never delegated | Theirs, by default |
| --- | --- |
| The cut, the ruling, the call between two designs | Implementation of a design you already fixed |
| A review verdict, the tip it binds, **and the eyes that read the diff** | Writing tests and fixtures |
| The land, the fold, the merge order | Sweeps, audits, multi-file reads, re-measurement |
| What to tell the owner | Bounded builds with a written acceptance bar |

**You already have standing authority — to ACT without asking, never to skip
the procedure.** The owner has twice given blanket permission to spawn
subagents for delegable work and to relaunch a wedged seat. Do not ask, do not
park work waiting for an answer. The harness may carry a session setting like
"do not call the Agent tool unless the user requested it" — that is a harness
default, **not** a helm gate and not the owner's word. Owner-gated is only:
public-visibility flips, outward-facing pushes/PRs, and inputs only a human
holds.

**But authority is not an exemption, and reading it as one contradicts §5 of
this same guide.** A kill still runs §5's procedure every time: enumerate the
pane's in-process children first (they die with their host), then prove the
session is safe to lose — `helm session ls` is tri-state and only **persisted**
clears a kill; MEMORY-ONLY and UNKNOWN do not. Nothing checks this atomically
at pane close, so the check is yours. "I did not have to ask" and "I did not
have to look" are different sentences.

**The delegation hazard your fan-out creates, stated plainly because this
section is what encourages the fan-out.** In-process children **inherit your
cwd** and cannot be bound to a lane of their own by the occupancy machinery, so
`release_lane` sees your room's occupant and not theirs. Release a room while a
delegate is mid-write and you strand its work. Give each delegate its **own
claimed lane** (`helm work claim <lane>`) rather than a share of yours, and
never release a room you have not proven empty of children.

**A workflow's structured output is not a receipt.** It is what an agent
returned, not proof of a commit, a tree, or a fold — and harness worktrees have
carried measured stranded dirty work. Take the tip, the tree and the gate
token, or you have a claim rather than an artifact.

**Small workflows are the bread and butter.** For independent lanes, a small
Workflow script (a handful of agents, one or two phases, structured output you
act on) beats a serial personal thread. Fleets are not the goal; *small* is
load-bearing.

**The decay law, and why this section exists.** Role separation decays back
into doing the work yourself, silently, inside a single shift — because a
defect you have already root-caused is *cheaper to fix than to brief*. The
local decision to just-fix-it is always correct, and the aggregate is that the
seat that should be landing stops landing. Resolve does not fix this; only a
trigger you can notice from the inside does. **Stop and route when any of
these is true:**

1. You name three open items in one sentence — two of them belong to someone
   else, and asking the owner to sequence them is the anti-pattern itself.
2. You are hand-writing fixtures, sweeping files, or re-running a suite —
   labour, not judgment.
3. You have made **zero** subagent calls this session and it is not your
   first hour.
4. You just described a fix precisely enough to explain it — that description
   IS the build brief, and it is never cheaper to re-derive later.

A brief is a durable artifact with a worker attached: give it the exact tree
to work in, the acceptance bar, the measurement that proves it, and the
failure modes you already know. `helm store resolve "<symptom>"` before
writing one — the fleet has usually met this already.

### Mechanics and hazards

- **Codex-family seats**: before fanning out, probe per-instance proxies at
  runtime, from any cwd — `helm seat launch codex -i 2` prints the exact
  launch line without running it, and idempotently wires the seat's instance
  state as it does (a safe-to-repeat probe, not a side-effect-free one).
  The N≥2 line carries the instance's OWN proxy port
  (`ANTHROPIC_BASE_URL=…:<base+N>`, e.g. 8319 for codex-2), so codex
  subagents fan out without the shared-port multiplication that was the
  silent-hang vector.
- **Launch lines are secret-bearing**: never paste one that carries a token
  into a room, doc, or anything else durable — transcripts are the corpus
  (§5). The printed line reads the bearer from its 0600 token file at exec
  time (no inline token). Either way: run it, don't quote it.
- In-process subagents **die with their host pane**. Enumerate children
  before killing any pane — a kill takes the whole household.

## 7. Composition truth

- Composition questions — who is running, which pid/seat/sid/home/pane — are
  answered by running the CLI, never from memory. `helm who` is the
  pid → cred home/account/session/cwd table (shared sessions flagged); the full
  live-probed composition census is `helm fleet` (pid → seat/sid/home/daemon/
  pane/stamps/deck, every column a live probe, a failed probe rendered UNKNOWN
  and gating the exit code). Prefer `helm fleet` when it answers. Quote the
  output, including to the owner.
- `helm session ls` is tri-state: **persisted / MEMORY-ONLY / UNKNOWN**.
  UNKNOWN is never PASS — a failed probe is not an absence fact.
