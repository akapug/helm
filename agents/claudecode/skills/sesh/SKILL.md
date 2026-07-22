---
name: sesh
description: Use when an agent needs session/account/resume operations — finding or reading another session, minting the command to resume a session under a specific account/credential home, checking quota headroom or picking the freshest account, rollover swaps, re-homing a session's cwd, or shrinking a big session into a resumable copy.
---

# sesh — sessions × accounts, driven by an agent

sesh composes every local coding-agent session (Claude Code, Codex; anything
[cv](https://github.com/emberian/cv) can read) with every OAuth subscription
account's live quota, and emits the **native resume command** for any (session,
account) pair. Local web app (`sesh` → http://127.0.0.1:7402) + CLI + HTTP API.
It never proxies and never touches token contents — the product of every flow
is a shell command you (or your human) paste into a terminal.

If `sesh` is not on PATH: `python3 <repo>/server/cli.py <verb>` is identical.

## CLI verbs

```
sesh doctor                      # what works on THIS machine, what's missing (⚠ = optional)
sesh creds                       # accounts: state, 5h/7d battery, weekly windows, homes, hygiene flags
sesh ls -q herdr --harness claude  # find sessions (also --cwd substring, --json for OpenSession shape)
sesh cmd 55edcba9                # print THE resume command; auto-picks the best-headroom account
sesh cmd 55edcba9 --account hey@simbi.com --model fable   # pin account/model
sesh show 55edcba9 --find "auth bug"   # read the transcript, window centered on the match (»)
sesh mv 55edcba9 ~/dev/x         # re-home: claude --resume resolves from the new dir (--reset undoes)
sesh prune 55edcba9 --dry        # estimate a smaller resumable COPY; drop --dry to create it
sesh capsule 55edcba9            # session × its git-era sha → worktree + resume block (time capsule)
sesh swap david@mv hey@simbi.com # rollover: every live agent on FROM + its resume command under TO
sesh next                        # which account to launch each model under, right now
sesh history --hours 48          # per-account quota utilization sparklines
sesh home ls                     # every credential home: auth, identity, live agents, hygiene
sesh home create a@b.com --provider claude   # prepare a home + print the login cmd (HUMAN runs it)
sesh home verify a-b-com         # post-login check: authed / identity / canonical name / link
sesh home archive a-b-com        # park a home reversibly (refuses if live); unarchive restores
sesh home migrate a-b-com        # rename a name-lies home to canonical (+alias symlink)
sesh physics a-b-com --cwd ~/dev/x [--diff other-home]  # what that seat would LOAD: MCPs, hooks, plugins
```

`sesh cmd` composes: `eval "$(sesh cmd 55edcba9)"` resumes in-place. All sids
accept unique prefixes.

## HTTP API (server on :7402; full contract in docs/API.md)

- `GET /api/catalog[?format=opensession]` — all sessions; `GET /api/creds` — accounts + quota + hygiene.
- `GET /api/history?hours=N` and `GET /api/burn?hours=N` — gauge history and burn↔session join for charts.
- `GET /api/allocate` — per-model account pick; `GET /api/status` — provider/cv/catalog health.
- `GET /api/search?q=` — deep content search (cv-powered), hits join back to resumable sids.
- `GET /api/session?sid=&find=&before=` — windowed transcript read; `GET /api/cmd?account=&sid=[&model=]` — THE command.
- Mutations are POST + `Authorization: Bearer <token>` (`/api/cwd`, `/api/prune`, `/api/homes`); the token is templated into the served page (or `SESH_API_TOKEN`). Reads are open on localhost.

## Safety canon (non-negotiable)

- **Never copy credential files** between homes — that is how OAuth refresh chains die. A cred enters a home only via the provider's own login flow.
- **Logins are HUMAN-run.** sesh prepares homes and prints the login command; you hand it to the human, never run it yourself.
- **One live agent per account-home** (one writer per refresh chain). `preflight.live_holder_pid` set ⇒ the session is already open — surface it, don't double-open.
- **cwd is metadata, never identity.** `sesh mv` re-homes safely; resuming from the wrong dir just needs an override, never a file move.
- **Mutations need the bearer token.** Reads are open on localhost; anything that changes state is POST + bearer.

## Worked flows

**1. Resume a teammate's session under the freshest account**

```
sesh ls -q "mc comms"            # or: curl -s 'http://127.0.0.1:7402/api/search?q=mc+comms'
sesh show f3ab12 --find "handoff"    # read before you resume — confirm it's the right session
sesh cmd f3ab12                  # auto-picks best headroom; stderr names the account
# paste the printed command in the target pane (or eval it in-place)
```

**2. Rollover swap (account exhausted / reset boundary)**

```
sesh creds                       # confirm FROM is drained, pick TO by battery
sesh swap cto@example.invalid hey@simbi.com
# per seat: finish/park the pane, exit the agent, paste that seat's printed block
```

**3. Read-then-optimize a big session**

```
sesh show 9c0de1 --limit 100     # tail first; --find TERM to jump to the load-bearing part
sesh prune 9c0de1 --dry          # estimate: lean = sidecar-snip bulk, lossless
sesh prune 9c0de1                # NEW smaller resumable copy; original untouched
sesh cmd <newSid>                # resume the copy under the best account
```

## cv — the session substrate (sesh's favorite dependency)

sesh is the sessions×accounts×resume layer **on top of cv (clustervision)** —
sesh's deep search, transcript reads, and resume studio ARE cv doing the work.
For deep corpus operations, go to cv directly:

```
cv ls                            # every discovered session, all harnesses
cv search "refresh chain" [--semantic]   # full-text (or embedding) search across ALL content
cv show <id> --range 650-700     # windowed read of a huge session — only those bytes load
cv name <id> my-expert           # bind a human name; works anywhere an <ID> is accepted
cv events <id>                   # what a session DID: file edits/reads, commands, errors
cv touched src/auth.py           # every session that touched a file
cv blame src/auth.py:120         # which session wrote this code, and what was it thinking?
cv prune <id> --window 20000     # transcript surgery: smaller resumable copy (sesh's studio engine)
cv pack "port the auth fix"      # compile a context bundle for a new task from the whole corpus
cv index [--semantic]            # build/refresh the search index
```

Rule of thumb: **sesh** when the question involves accounts, quota, or minting
a resume command; **cv** for corpus-wide content ops — cross-harness search,
provenance/blame, event extraction, transcript surgery, dataset export.

**cv MCP server**: cv ships an MCP server exposing `recall`, `search_sessions`,
`read_session`, `project_sessions`, and more — agents in MCP-capable harnesses
can install it for in-context corpus queries (no shell round-trips). Setup docs:
https://github.com/emberian/cv.
