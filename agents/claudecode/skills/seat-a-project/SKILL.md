---
name: seat-a-project
description: Use when any agent must MAKE SESSIONS for a project — register a checkout with Orca and helm, put a claude lead on its own credhome, spawn its codex pair partner, pin that codex seat to one pooled account, or move a running lead onto a home. Triggers: "get the project lead going again", "home that lead on the new cred", "give this lead a codex partner", "put that codex on the team account", "how many lanes can one team cred run". Owner-directed as a skill every helm agent can run; the owner's cross-project chief seat is the first dogfood case.
license: MIT
metadata:
  author: helm
  version: "1.0.0"
---

# seat-a-project — make sessions for a project on the cred the owner names

The owner picks WHICH account a project runs on. helm makes that pin durable and
measurable. Every step below is one verb an agent runs; where a step is still a
hand action the task that institutionalizes it is named, so the next agent can
check whether the verb has landed (`helm task show <id>`).

Two mechanisms, never confused:

- **Orca's account store** is fleet-wide and SLEDGEHAMMER: every native claude
  pane on `~/.claude` bills whichever account the owner has active in Orca.
- **A credhome** (`~/.claude-homes/<account>`) is a SCALPEL: a seat launched
  with `helm launch --home H` runs with `CLAUDE_CONFIG_DIR` pinned there and
  bills exactly that account, whatever Orca switches to. helm only borrows
  Orca's token bytes for it (`helm cred sync-orca`); Orca itself homes nothing
  per pane, and its terminal launcher has no per-terminal account option.

## 0. Read before you act

```
helm creds                     # live headroom per account, both families
helm homes                     # every credhome, its real occupant, hygiene flags
helm projects | rg <project>   # is the checkout registered, and at which path
orca-ide repo list --json      # is the checkout an Orca workspace
helm fleet | rg <seat>         # what home a live seat really runs on (home=…)
```

A team (5x) account carries ONE lead and one agent at a time; a Max/Pro
account carries a fleet. Straggler weekly usage is spent by rehoming seats,
not by waiting.

## 1. The checkout must be registered in both places

Orca: `orca-ide repo add --path <git checkout>` (the git root, not a parent
folder). helm: the registry follows harness observations (`helm sync`); a
moved checkout with a lingering copy at the old path refuses `helm projects
repoint` until the copy is moved aside. A seat name is `<registered project>-<family>`; the spawn refuses
a cwd outside that project's registered scope.

## 2. Put a claude lead on its own credhome

Credential bytes move only through the guarded door, `helm cred sync-orca`.
The one hand step left is the NON-SECRET identity file.

```
helm homes prepare claude <email>      # dir 0700 + projects link + skills link; seats NO credential
```

`prepare` refuses when that email is already seated in another named home
(one home = one login) and writes no `.claude.json`.

Identity, by hand: find Orca's dir for the account by reading ONLY
`emailAddress` from each
`~/.config/orca/claude-accounts/<id>/auth/oauth-account.json`, then write
`<home>/.claude.json` as `{"oauthAccount": {...}}` carrying exactly THREE
fields copied from that file: `emailAddress`, `organizationUuid` and
`accountUuid`. Copy the named fields, never the whole object: the door
(`helm/cred/orca.py`) matches only those three, and the file belongs to
Orca, whose schema helm neither controls nor verifies, so a whole-object
copy would silently carry any field Orca adds later.

Token, through the door:

```
helm cred sync-orca --home <name>            # dry run: STALE-vs-ORCA, "home holds no .credentials.json", WOULD SYNC
helm cred sync-orca --home <name> --apply    # Orca's live bytes land 0600, pre-image first, Orca untouched
helm cred list                               # the row must read AGREE and FRESH
```

The door accepts a home that carries the agreeing identity and no
`.credentials.json` yet (it reads STALE-vs-ORCA, chain `no-home-token`); a
home with no identity file reads UNKNOWN and is never written. Before it
writes it also requires: the Orca dir carries its ownership marker and names
one account; the home is not DRIFTED; Orca's refresh family is live in no
other home, `~/.claude` included; no live claude process holds the home; the
keepalive writer lock is free. Every refusal names its cure. The one an agent
meets most: Orca is currently switched TO this account, so its family lives
in `~/.claude`; the owner switches Orca to another account first, or the seat
logs in fresh in the home (`CLAUDE_CONFIG_DIR=<home> claude /login`,
human-only). `helm launch --home` runs the same door before exec, so a home
left STALE is synced by its first launch when the door allows.

A synced home SHARES Orca's refresh chain: while the seat runs, the owner
does not switch Orca to that same account, or the first refresh on either
side revokes both; a separate `/login` into the home avoids that coupling.

Fallback, named because it is the thing the door exists to prevent: copying
`.credentials.json` bytes by hand into the home. It skips the holder check,
the family-elsewhere check and the pre-image, and it can put one refresh
token under two live refreshers. Use it only when the door reads
NO-ORCA-COPY or UNKNOWN for a reason you have read in `helm cred list` and
cannot cure, say so in the room, and file the reason as a task.

Launch it in the project's Orca workspace, resuming its last session
(transcripts are shared through the home's `projects` link; a home whose
`projects` is a REAL dir strands sessions — `helm homes verify` says so):

```
orca-ide terminal create --worktree path:<git checkout> --title <seat> \
  --command "cd <lead cwd> && HELM_CHAT_NAME=<seat> helm launch --seat <seat> \
  --home <home> --model fable -- --resume <session id> --dangerously-skip-permissions"
```

Claude asks to trust a folder the home has never seen: answer it through
`orca-ide terminal send` (down arrow, Enter), never leave it parked. Then
`helm fleet | rg <seat>` must show `home=~/.claude-homes/<home>`.

## 3. Move a RUNNING lead onto a home

Ask it to write its handoff first (`helm handoff write`), wait for idle (no
subagent line on its pane), then relaunch as in step 2 with `--resume` of its
current session, or run `helm seat rehome <seat> --home H --apply`, which
does the exit, relaunch and proof in one verb (dry run without `--apply`
prints the plan). Prefer the verb: a pane exited by hand is a live agent
whose session, claims and rows the verb proves and the hand does not.

## 4. Spawn the codex pair partner

```
helm seat spawn <project>-codex --cwd <git checkout> --model gpt-5.6-sol --role worker --print
helm seat spawn <project>-codex --cwd <git checkout> --model gpt-5.6-sol --role worker
```

`--cwd` must be an Orca workspace path or Orca adopts the pane into whatever
workspace contains it (a pane spawned in a non-repo parent folder landed in
the owner's broad top-level workspace, invisible beside its lead). If the seat
must live in a folder that is not a workspace, create the terminal yourself
in the right workspace running the minted
`~/.helm/_global/seats/codex/instances/<seat>/launch.sh`, submit the
onboarding line `Run `helm seat boot-brief` and follow it.`, then
`helm seat rebind <seat> --apply`.

## 5. Pin a codex seat to ONE pooled account

Every codex sidecar reads the shared pool `~/.helm/_global/seats/codex/auth`
and falls through to any account with headroom; that is right for the fleet
and wrong for a measurement. To home a seat on one account:

1. bring the account's `auth.json` into the pool. If it is the account Orca
   has ACTIVE, `helm codex sync-orca` pools Orca's live copy through the
   guarded door (identity admitted, freshness rung) and step 2 is not needed.
   For an account Orca is not active on there is no door yet: copy Orca's
   `~/.config/orca/codex-accounts/<id>/home/auth.json` to
   `~/.codex-homes/<name>/auth.json` (0600, bytes never printed; the id maps
   to an email through the id token's claims, print only the email), and say
   so in the room;
2. `helm codex pool <name>` translates the codexhome's `auth.json` into the
   pool format: a local-store to local-store write, 0600, source untouched;
3. move that one file into `<instances>/<seat>/auth/` (0700 dir), point the
   seat's `config.yaml` `auth-dir` at it, then
   `helm seat down <seat>` and `helm seat up <seat>` restart only the sidecar;
   the pane keeps running.

`helm seat launch` or `helm seat resume` regenerates `config.yaml` and undoes
the pin, so re-pin after any relaunch and say so in the room.

## 6. Prove it, then report what the owner reads

`helm fleet` (home per seat), `helm seat where <seat>` (pane, liveness),
`helm creds` (the account moved, the others did not). Report to the owner as
outcomes: which project runs on which account, and what still needs a hand.
File every hand step you had to take as a task naming the verb that should
have done it; the owner's rule is that a one-off that becomes policy is
institutionalized for any helm agent.

## Prior art

`fleet-maintenance` §5 (cred swap for a seat that ran dry), `sessions`
(moving a transcript between cwds and harnesses; not accounts), the store
premise `orca-is-the-sledgehammer-credhomes-are-the-scalpel-sync-from-orca`,
tasks 2573, 2585, 2586, 2592, 2598.
