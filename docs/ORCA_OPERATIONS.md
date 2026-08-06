# Orca operations — identity, visibility, and what not to reap

Companion to `ORCA_SEAM_AUDIT.md`, which settles OWNERSHIP (who may create,
name and delete what). This one is operational: how to tell what is actually
running, which of three disagreeing answers to believe, and which of them are
instruments that cannot fail in the direction you are testing.

Everything here was measured on 2026-07-27/28 against a live 16-seat fleet.
Where a claim rests on one observation it says so.

## 1. Daemon generations — the reason panes "disappear"

An orca upgrade can bump the terminal daemon protocol (v26 -> v28 on
2026-07-27, skipping v27). It starts a new daemon and **does not reap the old
one**. Old daemons keep running, holding their PTYs and every agent inside
them, so the new UI cannot render another generation's sessions and the
operator sees bare shells.

```
ls ~/.config/orca/daemon/*.sock                              # one per live generation
grep -c '"event":"startup"'  ~/.config/orca/logs/daemon.log  # vs "shutdown" = the leak
grep session-killed daemon.log | grep <restart ts>           # ZERO = nothing was killed
mount | grep -c orca                                         # leaked AppImage mounts
```

2026-07-28: 13 startups / 6 shutdowns, three daemons alive (v24/v26/v28), 15
leaked `/tmp/.mount_orca-*` mounts. All three sockets answered a `client-hello`
in under 0.2s, so an old generation is not a dead one.

**The owner's report — "my panes were killed and left at a command prompt" — is
usually false, and believing it is the expensive mistake.** On 2026-07-27 there
were ZERO `session-killed` events at the restart timestamp, every seat had a
live PTY, and a `helm chat` roll-call reached four of them in two minutes.
Delivery does not go through a pane.

**orca RE-ADOPTS panes across generations.** Four seats whose launch handle had
vanished from `orca terminal list` were each being served under a NEW handle.
14 live claude processes, 14 rendered, zero invisible. They were an hour from
being "rescued" — which would have killed live PTYs to fix nothing.

## 2. Three handles, and which question each answers

| source | answers | freshness |
|---|---|---|
| `/proc/<pid>/environ` `ORCA_TERMINAL_HANDLE` | what pane the process STARTED in | **frozen at exec, forever** |
| `orcaadopt.resolve(seat)["handle"]` | what pane serves it NOW | live, computed |
| `orca terminal list` | what the daemon serves now | live, spans generations |

The environ handle is set once at exec. It cannot change when a pane is
re-adopted, so it **returns the same value whether or not re-adoption happened**
and can never disconfirm it. Diagnosing from it is the same error as running
`tty` inside a Bash-tool subshell — which reports "not a tty" for every seat,
forever — and concluding the SESSION has no terminal.

`orca terminal list` spans daemon generations: it reported a pane owned by a
two-generation-old orca-ide as connected. CLI-visible is not sidebar-visible.

## 3. Deciding what is actually running

**First: `orcaadopt.resolve(<seat>)`.** helm already computes the live handle.
It returns seat, provenance, state, evidence, sessions, pids, handle, pane_key,
worktree_id. Where it resolves it is authoritative and free — verified against
an independent nonce measurement, exact match.

```python
from helm import orcaadopt
orcaadopt.resolve("console-design")   # -> {... 'handle': 'term_c9aa4f24-...'}
```

`helm seat resume <seat>` uses it (`seat.py:3848`, the resume verb's
orca-adopted gate), so **orca-launched claude
panes ARE resumable by helm** — the old `helm-cannot-resume-orca-launched-
claude-pane-seam-gap` is closed for any seat it can resolve.

**Its limit, measured 4/4:** resolve matches on the helm seat name carried in
the process environ. A seat whose chat name never entered `HELM_CHAT_NAME`
returns unresolved, no matter how alive it is.

```
pid 2789564  HELM_CHAT_NAME=console-design  -> resolves
pid 230219   HELM_CHAT_NAME=<ABSENT>        -> unresolved (but LIVE and rendered)
pid 2613935  HELM_CHAT_NAME=<ABSENT>        -> unresolved (but LIVE and rendered)
pid 925233   HELM_CHAT_NAME=<ABSENT>        -> unresolved (but LIVE and rendered)
```

**Unresolved is not dead.** It means helm has no name-to-process binding, which
is exactly when you need an observation instead of a lookup.

**Then: `lsof /dev/pts/N`** — binds a pts to the pids holding it. No timing
window, no wire, nothing to contaminate. Use it whenever the question is
"is a process on a live terminal".

**Last: the NONCE SCAN** — the only method that binds a pts to an orca HANDLE.

```
printf '\n%s\n' "$NONCE" > /dev/pts/N        # display-only; never enters stdin
for h in $(orca terminal list --json | grep -o 'term_[a-f0-9-]\{36\}' | sort -u); do
  orca terminal read --terminal "$h" --limit 80 | grep -q "$NONCE" && echo "$h"
done
```

Three rules or it proves nothing:

- **Never announce the nonce.** Every seat renders the chat it reads, so a
  published string can appear in any pane that saw the message and binds
  nothing. This invalidated the first attempt.
- **Read within seconds.** `terminal read` returns a ~39-line live VIEWPORT of
  the current screen, not scrollback, and a claude TUI repaints over it. A
  nonce found twice at `--limit 500` was unfindable at every limit minutes
  later: depth is not the variable, time is.
- **Run a positive control on the same read** (grep text known to be on that
  screen), or "not found" and "this read is blind" return the same number.

**A hit is proof; a null is not disproof.** Re-issue rather than conclude.

## 4. Traps that produced confident wrong answers

- **A truncated handle reads as dead.** `orca terminal show` returns
  `terminal_handle_stale` for at least three indistinguishable conditions:
  genuinely stale, never existed, and **malformed/truncated**. A healthy pane
  reports stale when handed a shortened handle. The control is one line —
  `orca terminal show --terminal term_00000000-0000-0000-0000-000000000000`
  returns the same string. Resolve full handles from orca's own list; never
  retype one out of a chat message where it was abbreviated for reading.
- **Re-running someone else's command with their input verifies the pipeline,
  not the premise.** A second agent reproduced a wrong result exactly by
  copying the bad handle.
- **Title + cwd is not identity.** A listed terminal whose title resembled a
  seat's task was taken as proof of re-adoption. It happened to be true; the
  method could not have shown it either way.

## 5. When the reader itself is degraded (2026-07-28)

`orca status` answers in 1.5s while **every enumeration hangs**:

```
orca status                    rc=0    1.5s
orca worktree list             rc=124  >25s   (timeout)
orca terminal list             rc=124  >60s   (timeout)
orca terminal list --limit 5   rc=124  >25s   (timeout)
```

`--limit 5` hangs too, so it is not volume. All three daemon sockets answer a
hello in <0.2s and the daemon log shows live `session-created` events, so the
daemon is up. The UI's own "Terminal sessions unavailable, the list may be
stale / Update failed" is accurate.

**Do not tune a cleanup against counts a degraded reader produced.** While this
reproduces, orca's terminal/workspace counts are unusable and `git worktree
list` is the trustworthy substitute for anything worktree-shaped.

## 6. Worktree reaping — landedness owns retirement

`helm work gc` owns both normal lane rooms under `<repo>-wt/` and Claude Code
workflow/subagent rooms under `.claude/worktrees/`. `helm worktree gc` delegates
those two containers to it, then handles only the remaining unmanaged estate
worktrees and orphan `worktree-*` / `lane/*` branch stubs. Missing registry
records keep the same ownership and use exact metadata-only record removal after
a fresh proof; a revived checkout's files are never deleted, and global
`git worktree prune` is never an actuator. Peek records under `<repo>-wt/peeks/` belong to `helm work
peek --drop`. No worktree is classified by two cleanup owners, and merged
historical lane branches drain without `-D`.

The automatic proof is deliberately one-directional: **a room retires only when
its attached branch tip is an ancestor of the integration base**. This misses
cherry-picked or reworked lands, but that is the safe error direction — the room
stays and its TRIAGE row names the lane, tip, age and git committer identity (shared across seats). False-alive
costs a sidebar row; false-dead destroys work. There is no patch-id guess and no
force-delete path.

The full removal proof is re-read at enact time: no live lease, no meaningful
cwd occupant, **no Orca pane bound to the room**, no out-of-band lock, attached
branch unchanged since scan, clean working tree, and branch still landed. Dirty
rooms are rescue-committed to their own branch and then **kept**, because the
rescue commit itself is unlanded. Detached or mid-operation rooms remain
manual-only.

A PANE OUTLIVES ITS SHELL, so the cwd scan cannot clear a room on its own.
Orca's `terminal list` carries each pane's `worktreePath`, and GC keeps any room
a pane is bound to — including a pane whose shell has been hung up or has moved
its cwd elsewhere. If Orca cannot be asked (daemon down, unparseable reply), the
room is kept and the refusal says so: an unanswerable host is never read as
"nothing is there". Set `HELM_METAHARNESS=none` to sweep without consulting a
metaharness at all — an explicit, owner-made choice rather than a silent one.

One occupied process class is disposable: Orca's inert shell-ready `bash` whose
cmdline carries `--rcfile .../.config/orca/shell-ready`, Linux state is exactly
the sleeping session-leader/foreground `Ss+` shape, and whose child list is
empty. GC may hang that placeholder up (SIGHUP — bash ignores SIGTERM in that
state) before reaping an otherwise landed room, and only after the pane check
has already passed. Unreadable state, a child, a changed command, or a process
surviving the bounded wait all fail closed and keep the room. Every APPLY pass
posts one room summary: `removed=N kept=N triage=N`, so cleanup is never silent.
