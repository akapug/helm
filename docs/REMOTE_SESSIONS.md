# Driven remote sessions

A **driven remote session** is an agent that helm drives but that is not a
helm agent. It runs off this machine: a Claude Code cloud session today
(`claude --cloud`), and later a container session that another harness hosts
and helm drives headlessly. Such a session has no beacon, cannot reach the chat
node, cannot write the dispatch ledger, and has no local process whose
liveness helm can read.

helm uses these sessions as an outside review force: a cloud session on a
promotional credit account reads a lane in a fresh context and reports back.
They are also a BUILD lane: a build row's brief goes to a cloud session, and
its pushed branch comes back as a local lane that another session reviews
(see [The build lane](#the-build-lane)). This page describes the class, the
channel, the relay that acts for each session, and the policy that decides
what a read counts as.

The code is in four modules:

| module | what it owns |
|---|---|
| `helm/remote_session.py` | the channel: host facts, the journal, the two transports (a pushed branch, or a bundle), launch and delivery, the drop read and its strict parse, the cure, and state inference |
| `helm/remote_policy.py` | what a read counts as: the one table, the reversibility classifier, and the falsifiers |
| `helm/remote_credit.py` | which account pays and how fast the credit may be spent |
| `helm/remote_relay.py` | the relay: row pickup, delivery into standing sessions (or a launch) and follow-up, the build lane, recording on the ledger, the falsifier revert, and the `helm remote` verb |

## The channel is asymmetric

- **helm reaches in** one message at a time. `claude -p MSG --cloud SID`
  queues one message and exits. It gives no reply. With
  `--output-format json` it answers `{ok, session_id, url}` or
  `{ok: false, error}`. A send to an archived session is refused, and that
  refusal is the only state signal the CLI gives.
- **The session reaches out** only through a durable surface that both sides
  can read: one GitHub issue per project, the **drop**. Each session posts one
  comment per read.

### Standing sessions are the transport

A session created from the CLI with `claude --cloud` now comes up as a bundle
session with no GitHub access: its checkout has no remote, and GitHub calls
answer 403 ("GitHub access to this repository is not enabled for this
session"). Setting branch tracking before the launch did not change that: 23
launches on 2026-09-28 pushed nothing (MEASURED). A session the owner creates
in the claude.ai web UI with the repository selected can fetch, push and
comment. So the owner creates one **standing session** per account, names it
in the host facts (`standing_session` on the account) together with the
repository it was created on (`standing_repo`, `owner/repo`), and the relay
feeds it with `deliver` (`claude -p MSG --cloud SID`), one task per row:

- **A standing session pushes to the repository the web UI attached**, which
  the relay's PRIVATE check on its own pushes never sees. So a session is used
  only when its account's `standing_repo` names the project's `push_repo`; a
  row with no such session is held, naming each session's binding (or that it
  names none) and the push repository. Every standing task also tells the
  session to run `git remote get-url origin` before any push and to push
  nothing, and say so, unless it names the push repository.

- When a seat's chosen account has a standing session, the relay delivers the
  row's task into it instead of launching. The task names the branch to
  fetch or push and the drop issue to comment on, and it states the model and
  effort the seat expects, so a session running something else says so in
  its report (a report naming another model is not recorded).
- A seat with no usable standing session is **held with a plain reason**. It
  never falls back to a CLI launch, because that path is measured broken.
  The CLI launch is kept behind the seat's explicit `"transport": "cli"`.
- A send that is refused as archived is recorded on the account
  (`standing-archived`, shown by `helm remote status` as "standing session
  archived"), and the relay never delivers to that session again. The row
  moves to the next account's standing session, or is held.
- A launch refused before any session started (a refused delivery, a refused
  checkout) is counted; after three such refusals the row is held naming the
  last one. The daily credit refusal is not counted: it only defers the row.
- The journal keys each read by the standing session and the row, so several
  rows can share one standing session without one's report being read as
  another's.

So each session gets a local relay that is its helm-side identity. The relay
delivers rows in, reads the drop out, records verdicts on the ledger under a
named seat, and owns the wake-back edge: it polls the drop, nudges on silence,
and retires a session when a send is refused as archived.

## The states

Nothing on this machine can see a remote session, so the relay infers each
state from its journal and the drop. When the evidence does not decide, the
state is `UNKNOWN`.

| state | meaning |
|---|---|
| `LAUNCHED` | the session was created and its first read is under way |
| `AWAITING` | a follow-up (a re-read at a new tip) was delivered and is not answered |
| `ANSWERED` | a report for the session's current label and tip is on the drop |
| `NUDGED` | the session was silent past the window, and a nudge was sent |
| `SILENT-IDLE` | the account's credit has not moved for `HELM_REMOTE_IDLE_FLAT_S` since the read began, and no report is on the drop |
| `UNDELIVERED` | nudged `HELM_REMOTE_MAX_NUDGES` times, the account flat for `HELM_REMOTE_IDLE_FLAT_S` since the last nudge, and still no report. Completion is unknown: it may still be running, but it holds no concurrency slot and needs manual report recovery |
| `ARCHIVED` | a send was refused as archived |
| `UNKNOWN` | the launch named no session, three sends failed with no archive signal, the session stayed silent after every nudge with no flat reading, or the account stayed flat after the idle check-in |

**The idle signal belongs to the account, not to one session.** The usage
endpoint meters an account. A flat reading means that nothing on the account
is spending, which is evidence about every session on that account. A moving
reading is evidence about no session in particular. So a flat account can
make a session `SILENT-IDLE`, but a moving account never marks a session as
busy. The meter also lags, so the window is long (30 minutes by default). The
relay takes one credit reading per tick of each account that a live session
bills, and never more often than every five minutes. It does not refresh a
dead token only to take a reading.

A `SILENT-IDLE` session gets ONE check-in: a message that asks it to post its
report, or, when the post fails, to run `gh auth status`, retry once, and end
its reply with the report and the error. When the account stays flat for the
same window after the check-in, the state is `UNKNOWN`. The relay then holds
the row, and it tells the author and the room where the session can be
opened, because it cannot read the session's own reply (see
[Not built yet](#not-built-yet)).

A session already nudged to the cap, whose account stays flat for the same
window after the last nudge and which still has no report, is `UNDELIVERED`.
That says nothing about process completion: a long container test bills no
model credit. The relay posts one dispatcher row naming the session URL and the
next step (`claude --teleport <sid>` from the session's workdir, then read its
last reply), keeps the review row open for a late report, frees the concurrency
slot, and does not nudge again.

## The relay

A remote seat is addressed like any seat:

```console
$ helm dispatch send cloud-opus <lane> --ref TIP --kind review --new-work
```

A remote seat has no roster row and no pane, so the send door asks the relay
first. The door refuses when remote sessions are switched off, when the seat
entry cannot be driven (for example a Sonnet or Haiku model), or when every
account the seat may bill was read at or below the credit floor. An account
that nobody has read yet is admitted, and the row says so.

`helm remote tick` is one relay pass. For each open review row addressed to a
remote seat, it does one of these (a build row is
[the build lane](#the-build-lane)):

1. **Start.** The relay checks the credit and the pace, then prepares the
   session's workspace (see [The transport](#the-transport)) and delivers the
   task into the paying account's standing session. On the CLI opt-in it
   instead marks the workspace trusted, refreshes the token if necessary, and
   launches. Details are in [The launch](#the-launch).
2. **Re-read.** When the row supersedes a row that a live session already
   read, and the new tip descends from the old one, the relay continues that
   session. On the branch transport it pushes the new tip as its own branch
   and the message tells the session to fetch it. On the bundle transport the
   session cannot fetch, so the delta travels in the message as
   `git format-patch old..new` for `git am`; when that does not fit one
   message (120 KB), the relay launches a new session instead.
3. **Attend.** The relay reads the drop, and on the branch transport also the
   session's fallback report branch. A report for this row's label and
   tip is recorded. A malformed report gets one correction message. Silence
   past `HELM_REMOTE_NUDGE_AFTER_S` gets a nudge. When a send is refused as
   archived, the relay launches again once. After that, it holds the row with
   the reason.

Every act is one line in the journal,
`<helm home>/_global/.state/remote-sessions/journal.jsonl`. `helm remote
status` folds the journal into the current picture.

### The transport

**A pushed branch is the default.** Sessions launched from an uploaded
bundle never posted to the drop, even after a follow-up asked them to post or
explain, while a session launched from a checkout whose origin was a GitHub
repository posted without trouble (MEASURED). A bundle session most likely
gets no GitHub credentials. So, per project:

1. The relay asks GitHub for the visibility of the push repository
   (`gh repo view <push_repo> --json visibility`) before **every** push. Only
   `PRIVATE` passes. `PUBLIC`, `INTERNAL`, an error or an unreadable answer
   refuses the launch, because a push to anything else publishes the lane.
2. It makes a scratch clone whose origin is that repository, and pushes the
   exact reviewed tip as `<prefix><label>` and the lane's merge-base as
   `<prefix><label>-base`. The objects come from the project repository on
   this machine, so only the push touches the network.
3. It launches from that clone, on that branch, with no `CCR_FORCE_BUNDLE`
   (an ambient one is removed), so the cloud session clones the private
   repository at that branch and has its GitHub auth.
4. The session posts its report with `gh`, and pushes any cure as a new
   branch, `<prefix><label>-cure`. When the post fails, it also pushes the
   report as `REVIEW_REPORT.md` on `<prefix><label>-report`, which the relay
   reads as one more drop comment, with the same strict parse and checks.
5. After the read is recorded, the relay deletes every branch the session was
   given (the read's, `-base`, `-cure` and `-report`, for the launch and each
   re-read), then the scratch clone. A held row keeps them, so a person can
   still open the session and its branches.

`push_repo` defaults to the drop's own repository, `branch_prefix` to
`cloudrev/`, and `push_url` to its https address. Claude Code clones only
when the Claude GitHub App is installed on that repository, or when
`/web-setup` connected a `gh` token that can reach it; otherwise Claude Code
bundles the checkout instead, and the session is a bundle session again.

**The bundle is kept as an explicit option** (`"transport": "bundle"`). The
relay runs `git init`, then fetches exactly the tip (as branch `review`) and
its merge-base (as `base`). The bundle has no remote, and the relay refuses
to launch one that has a remote. With `CCR_FORCE_BUNDLE=1`, Claude Code
uploads the repository, so the session has nothing it could push to. Its
report may be **unreachable**: a bundle session may not be able to post to
the drop, and nothing here can read its reply.

### The launch

On the default standing transport there is no launch: the relay delivers the
task (the standing preface, then the task below) into the account's standing
session, which fetches the pushed branch itself. What follows is the CLI
launch, which runs only on the seat's `"transport": "cli"`.

- **The account.** A cloud session bills the account of its config dir. So
  each account in the seat's list has its own Claude home, and the relay
  never uses the default home that an autoswitch may rotate. The relay
  refuses a home that is logged into another account.
- **The token.** `--cloud` does not refresh an expired access token; the
  create answers HTTP 401. So when the token is not live, the relay first runs
  one minimal `claude -p` call in that home (`HELM_REMOTE_REFRESH_MODEL`,
  default `haiku`). This call reviews nothing.
- **The workspace.** The scratch clone on the pushed branch, or the bundle
  (see [The transport](#the-transport)).
- **Trust.** The relay marks the workspace folder trusted in the home's
  `.claude.json`. Otherwise `--cloud` stops at the trust prompt.
- **The command.** `claude --model M --permission-mode P --cloud "<task>"`
  runs under `script`, because `--cloud` refuses to run without a TTY. Every
  flag must come before `--cloud`, because `--cloud` takes the next token as
  its value.
- **The permission mode.** A session that nobody watches must not wait on an
  approval. So the relay passes `HELM_REMOTE_PERMISSION_MODE` (default
  `bypassPermissions`; `none` omits the flag) and records the mode in the
  launch event. The documentation does not say whether `--permission-mode`
  carries into a session that `--cloud` creates. The first live launch
  settles that. If the mode does not carry, a second route is a
  `.claude/settings.json` on the pushed branch that allows `gh issue comment`,
  because a cloud session reads the repository's settings. That route is not
  built, because the branch holds the reviewed tree exactly.
- **The effort.** A seat's `effort` (one of `low`, `medium`, `high`,
  `xhigh`, `max`; the fleet's rule is Opus at `high`) is passed as
  `--effort <level>`, before `--cloud` like every flag, and is stated in
  every task delivered to a standing session.
- **The scratch clone is an ordinary one.** It is made with `git init` and a
  fetch, so every object is copied. Claude Code refuses a repository that
  borrows objects through an alternates file, so the relay never uses
  `git clone --shared`, and it refuses a clone that has one.
- **Never the shared default home.** Writing the trust flag of `~/.claude`,
  which Orca syncs and live seats share, races those seats. A seat entry that
  names it is refused unless the account says `"default_home": true`.
- **The task.** The task is a header with the label and the tip, then the
  author's brief, then the project's protocol with its placeholders filled.
  The protocol text is in the section below.

### The report protocol

The session posts one comment on the project's drop. Its first line must be
exactly:

```text
CLOUD REVIEW <label> <tip12> model=<model id> account=<email>
```

or the same fields under the report's own kind, followed by optional
`Branch:` and `Tip:` lines (the tip in full, and it must start with `tip12`):

```text
REVIEW <label> at <tip12> model=<model id> account=<email>
Branch: <the branch read>
Tip: <the full tip>
```

`REREAD` replaces `REVIEW` for a re-read. See [the report headers](#the-report-headers)
for every kind.

Then `VERDICT: APPROVE` or `VERDICT: FIX`, then `FINDING-COUNT: <n>`, then the
numbered findings. Each finding starts with `[BLOCKING|MINOR]
[MEASURED|INFERRED]`, followed by title / file:line / failure scenario / how it
was verified / the proposed cure. On the branch transport a cure is a pushed
branch; on the bundle transport it follows as a `git format-patch` inside one
`<details><summary>patch</summary>` block (a pasted patch is also accepted on
the branch transport when no cure branch exists). A project can replace the
default protocol with its own file (`brief` in the host facts). The
placeholders are `{label}`, `{tip12}`, `{account}`, `{issue}`, `{drop_repo}`,
`{branch}`, `{base_branch}`, `{cure_branch}`, `{report_branch}`, and the
transport's own parts `{workspace}`, `{report_only}`, `{cures}` and
`{fallback}`.

### The drop is untrusted data

The parse is strict. It refuses a second header, a second `VERDICT` or
`FINDING-COUNT` line, text before the verdict, misnumbered findings, a count
that does not match, an APPROVE that carries a BLOCKING finding, a FIX with no
finding, and a patch block that is not an mbox. A refusal reason never quotes
the comment. The report must also name the account and the model line the
session was launched with. When the project lists `drop_authors`, a comment
from any other author is ignored.

Nothing from a comment is executed. The comment text never reaches a shell, an
argv position that could be read as a flag, or a message sent back to a
session. A cure branch must descend from the reviewed tip, and it becomes a
patch (`git format-patch --binary tip..cure`). A cure, pasted or pushed, then
goes through the same steps:

1. Every `From:` header becomes the project's `cure_author`.
2. Every AI authoring line is removed from each commit message: every
   `Co-Authored-By`, `Claude-Session`, and "Generated with" line, including
   indented ones, and every `Model:` line, line naming a model id
   (`claude-opus-5-5`, `gpt-6-sol`) or link to an AI tool (a bare
   `claude.ai/code/session_…` URL too), which the build protocol forbids.
3. The patch is applied with `git am` in a scratch clone at the exact
   reviewed tip, with repository hooks off and the cure author as committer.
4. Each new commit is then **checked**: author and committer are the cure
   author, no AI line remains, and every change is a plain text file. A
   symlink, a submodule, a binary file, a file over 1 MiB, a path under
   `.github/`, and a `.gitmodules` change are refused by name. The relay
   refuses the whole cure when a check fails, and records the FIX with the
   reason.

Only then does the relay fetch the commit into the project repository as
`refs/heads/review/<seat>/<label>`. When the shared checkout's ref guard
refuses a branch that was not created in a room, the relay uses
`refs/remote-review/<seat>/<label>` instead. The verdict names the commit as
its `--patch-tip`.

## The build lane

A **build row** is a dispatch row of kind `build` addressed to a cloud seat:

```console
$ helm dispatch send cloud-opus <lane> --ref <trunk> --kind build --new-work
```

The relay runs it end to end, and nothing in the loop needs a local seat:

1. **Deliver.** The relay pushes the row's tip (normally trunk) to the PRIVATE
   push repository as `<prefix>build-<label>`, after the same visibility check
   as every push, from an ordinary scratch clone. A start or `-build` branch
   that already exists refuses the launch. It then delivers the brief with the
   build preamble into a standing session: commit as the project's
   `build_author` (else its `cure_author`; the owner's identity is a host
   fact, and a project with neither is refused), no Co-Authored-By or other AI
   attribution in any commit message, push only to
   `<prefix>build-<label>-build`, and report with a `BUILD` header.
2. **Fetch.** When `<prefix>build-<label>-build` appears on the push
   repository and descends from the start, the relay fetches it into the
   local branch `lane/<label>` at the pushed tip (or `refs/remote-build/<label>`
   where the shared checkout's ref guard refuses a branch born outside a
   room). It is a fetch: nothing is pushed anywhere but the private push
   repository. The build's two remote branches are then deleted, and the
   build row is held naming the lane and the tip, which is the hand-back.
   A `BUILD` report whose branch is not on the push repository is recorded
   as **UNMEASURED**, never as done, and the session is told once. A pushed
   build that fails the cure door (a foreign author or committer, an AI line,
   a file that is not plain text) is UNMEASURED too: the row is held and the
   builder is told why, once.
3. **Review.** The relay mints a review row for the seat's `review_seat`
   (default: the build seat itself), superseding the build row, with a
   review brief: ONE fresh-context subagent that holds none of the build's
   working context, the touched modules and their neighbours at tip and base,
   findings with `file:line` and severity, then APPROVE or FIX. That row
   prefers a standing session **other than the one that built the lane**, as
   load-spreading only: with only the builder's session usable, the builder's
   own session serves it, because the read is the fresh subagent's and owner
   canon (2026-09-27) counts a fresh-context instance's read even when its
   seat is on the chain. The exclusion walks the review row's supersedes
   chain and matches hand-backs on tip, ref or label, so a re-sent review
   prefers another session too. Every standing review task carries the
   fresh-subagent clause, not only the relay's own brief, and so does a
   re-read delivered into a review on the builder's session. The launch
   journals `builder_session: true` there, and the verdict's evidence line
   says the read was on the builder's own session.
4. **Record.** The reviewer's report is parsed and recorded exactly as any
   cloud read (see [What a read counts as](#what-a-read-counts-as)). A report
   that does not parse is recorded as UNMEASURED (`report-malformed`), never
   as a verdict, and gets one correction. The ledger records the build seat
   as a chain author, so a review under the same seat name is the author's
   own read to it: an APPROVE is an ordinary hold naming that refusal, not a
   source-clean one. A distinct `review_seat` is the chain's outsider, and
   its APPROVE is the source-clean hold auto-land reads.
5. **Cure.** A FIX goes back to the **builder's** standing session as a cure
   build row (superseding the review row) on the same branches: the reviewed
   tip is pushed as `<prefix>build-<label>` again, the brief names the review
   comment by id (no drop text is sent into a session), and the builder
   reports under `CURE <label>`, then `CURE2`, and so on, up to three rounds.
   The hand-back moves `lane/<label>` forward (a rewrite is refused) and
   mints the next review. The relay lands nothing itself.

### The report headers

Every report is one comment on the drop, and its first line names its kind,
its id (the row's label) and its tip. A session can be told this once:

```text
BUILD <label>                    CURE <label> / CURE2 <label> / ...
Branch: <prefix>build-<label>-build
Tip: <the full commit id pushed>
Model: <model id>                (optional)
Effort: <effort level>           (optional)
<free text: what was built and how it was tested>
```

```text
REVIEW <label> at <tip12> model=<model id> account=<email>
Branch: <the branch read>        (optional)
Tip: <the full tip>              (optional)
VERDICT: APPROVE or FIX
FINDING-COUNT: <n>
<numbered findings>
```

`REREAD <label> at <tip12> ...` is a re-read's header, and the original
`CLOUD REVIEW <label> <tip12> model=... account=...` is still accepted. The
parse is strict for both families: the `Branch:` and `Tip:` lines come right
after a hand-back header, and no second header, `Branch:` or `Tip:` line may
appear anywhere.

## What a read counts as

A remote reader is independent of the author on the CONTEXT axis, even when
it runs the author's model. The store prior
`review-independence-is-model-or-context-scaled-by-reversibility` scales that
independence by how reversible the lane is. The integrator's ruling of
2026-09-25 sets the table:

| author relation | lane | same-model arm | class | still owed |
|---|---|---|---|---|
| cross-model | reversible | on | REVIEW-LEG | — |
| cross-model | reversible | off | REVIEW-LEG | — |
| cross-model | irreversible | on | REVIEW-LEG | — |
| cross-model | irreversible | off | REVIEW-LEG | — |
| same-model | reversible | on | REVIEW-LEG | — |
| same-model | reversible | off | CONCUR | a different-model read |
| same-model | irreversible | on | CONCUR | a different-model read of the delta |
| same-model | irreversible | off | CONCUR | a different-model read |
| a Sonnet or Haiku reader | any | any | REFUSED | the review leg itself |

`helm remote policy` prints the table from the code (`remote_policy.TABLE`).

- **Cross-model** means that the author's model (from its runtime record) is
  not the reader's model line. A seat whose family is not Claude is
  cross-model on its family alone. When the author's model is not recorded,
  the relay reads it as **same-model**, because that is the stricter arm. A
  native Claude seat records no model, so this is the common case for a
  Claude author.
- **Irreversible** means the lane (a) touches prod, a migration, a deletion,
  money or credentials; (b) kills processes; (c) pushes public or rewrites
  history; or (d) changes a door that decides safety, such as land, review, a
  guard, an argv-guard or a hook's refusal. The classifier reads the lane's
  diff (path words and added lines, but not tests or docs), the project's
  declared `doors` globs, and the brief. When the diff cannot be read, the
  lane is irreversible: the classifier fails toward "owes a different-model
  read".
- **The switch.** `HELM_REMOTE_SAME_MODEL_ARM` (default `on`, the ruling)
  turns the same-model arm off by hand.

How each class is recorded on the row, always under the seat's name:

| class and verdict | recorded as |
|---|---|
| REVIEW-LEG, APPROVE | a **source-clean hold** at the tip (`dispatch hold --source-clean`). The integrator's land gate mints the approve on the tree that lands, as for any reviewer's clean read. |
| CONCUR, APPROVE | an ordinary **hold** whose reason names the read that is still owed. The row stays visible, and no land can close on it. |
| any, FIX (or an APPROVE with a cure) | a **FIX verdict** with no seat proof (the ledger reads it as nonauthorizing), with the finding count, the blocking paths the lane touches, and the cure as `--patch-tip` |
| REFUSED | an ordinary hold that says the review leg is still owed |

The relay does not change who may approve a land. A remote seat has no pane,
so it has no runtime proof and cannot mint an APPROVE. No reviewer mints one
from a clean read.

**The identity.** Every ledger write runs with `HELM_CHAT_NAME` set to the
seat. Every harness session id and cell profile is removed for that write.
The caller-identity law therefore reads the seat, and no post is signed with
another seat's key. A remote seat holds no dregg identity, so its chat
attestations are unsigned rows, and the ledger event is the record. After each
recorded read, the relay sends one DM to the row's sender.

## The falsifiers

Two falsifiers revert the same-model arm to CONCUR-class automatically. The
revert is latched: it writes one journal line and one chat post, and it stays
until `helm remote falsifier reset --why TEXT`. Evidence from before a reset
does not count after it.

1. **Calibration.** For each blind calibration read, record the share of the
   P1/P2 mechanical findings that another model recorded on that tip and that
   the remote read found independently:
   `helm remote calibrate <label> --found N --of M`. When the sum over the
   `main` sample falls below one half, the arm reverts, but only over a
   minimum sample of five reads (`remote_policy.CALIBRATION_MIN_READS`): one
   read below half used to trip it, and one read is not a sample. Which findings match
   is a judgement, so a person records it. The relay records what it can read
   by itself (`calibration-candidate`): the remote read's counts beside every
   other reader's recorded count on the same tip.
2. **Contradictions.** A same-model APPROVE-class read is contradicted when a
   FIX by a reader of another family lands later on the same tip. The relay
   records each contradiction that it can read. Two contradictions within
   seven days revert the arm. A FIX from a reader whose family is unknown
   never counts, because a falsifier must be built from evidence.

## Credit and pace

- **Accounts.** The seat's `accounts` list is the order of use: one account
  at a time, the first one above the floor. A live reading comes from the
  usage endpoint. The same endpoint reports the promotional credit under its
  own key, with the balance and the expiry. When the token is dead and the
  refresh fails, the last reading of that account stands in if it is at most
  24 hours old. An account that nothing can read is skipped. The relay never
  spends on an account whose credit it cannot read.
- **Floor.** An account at or below `HELM_REMOTE_CREDIT_FLOOR_USD` is not
  launched on.
- **Daily budget.** None by default: cloud credit is not rationed per day
  unless the operator asks. `HELM_REMOTE_DAILY_BUDGET_USD=auto` spreads what
  is left evenly to each account's expiry. A number sets a fixed cap, and `0`
  stops all launches. The amount spent today is measured from the readings, so it
  lags the vendor's own meter.
- **A scarce Max pool.** While the cached anthropic burn flag is ORANGE or
  RED, expiring promo credit spends first: `auto` stops spreading it to each
  expiry and caps the day at `HELM_REMOTE_SCARCE_CEILING_USD` (default 200)
  instead. The floor still holds, and a fixed number or an unset budget is
  not changed. The flag is read from the cached snapshot, never probed. `HELM_REMOTE_MAX_ACTIVE` limits how many
  sessions can be in flight, and so limits how far one tick can overshoot the
  budget.

The token is never printed or stored. The journal holds dollars and dates
only.

## Host facts

The seats, the accounts and the projects are facts about one machine. They are
in `<helm home>/_global/remote-sessions.json`, which is read on every call
([example](remote-sessions.example.json)):

```json
{
  "seats": {
    "cloud-opus": {
      "driver": "claude-cloud",
      "model": "opus",
      "effort": "high",
      "accounts": [
        {"email": "reviewer@example.com", "home": "~/.claude-homes/reviewer",
         "standing_session": "session_01..."}
      ]
    }
  },
  "projects": {
    "my-project": {
      "repo": "~/src/my-project",
      "base": "origin/main",
      "drop": "owner/private-repo#5",
      "drop_authors": ["owner"],
      "brief": "~/src/my-project/review-protocol.txt",
      "doors": ["src/auth/*", "deploy/*"],
      "cure_author": "Owner Name <owner@example.com>",
      "build_author": "Owner Name <owner@example.com>",
      "transport": "branch",
      "push_repo": "owner/private-repo",
      "branch_prefix": "cloudrev/"
    }
  }
}
```

A seat's `transport` is `standing` (the default) or `cli`; `effort` is one
of `low`, `medium`, `high`, `xhigh`, `max`; `review_seat` names the seat a
build's review row is addressed to. A row finds its project by repository
identity (the git common dir), so any
project can use the relay, and nothing in the code names a project, a path or
an account. A project with no `cure_author` records a FIX without its cure and
says why.

## Why a timer

A session answers asynchronously, often an hour after launch, and often while
no local seat is working. A Stop hook fires only when some local seat ends a
turn, and it runs on that seat's turn path. So the relay runs as its own
oneshot systemd user timer (`helm remote ensure-timer`, every
`HELM_REMOTE_TICK_INTERVAL_S`), in the same pattern as `owed-push`:

- No seat's turn waits on gh or the vendor.
- When the relay fails, no other periodic leg fails with it.

The unit carries every `HELM_REMOTE_*` knob that is set where the timer is
installed, and the absolute paths of `claude` and `gh`.

## Not built yet

- The `helm reviewers` census does not list remote seats. The dispatch door
  admits them, but `reviewers` reads panes.
- A container session that another harness hosts would be a second `driver`.
  Only `claude-cloud` exists.
- **A report that a session wrote only in its own reply is not read.** The
  CLI gives no reply to `claude -p --cloud`. `claude --teleport <session>`
  pulls the conversation, but it is interactive, it needs a clean checkout
  whose GitHub remote is the session's repository, and it needs the session's
  branch pushed to that remote. For a bundle session it hung at "Checking out
  branch" and wrote no transcript (MEASURED). The branch transport's
  fallback report branch covers a session that can push but not post; for
  the rest, a session nudged to the cap whose account then stays flat is
  `UNDELIVERED`: one dispatcher row names the teleport step, the source row
  stays open for a late report, and the relay stops nudging.
- Bundle sessions most likely get no GitHub credentials (inferred from two
  sessions that never posted); this is why the branch transport is the
  default.
- The relay removes a row's workspace once its read is recorded. The task
  files and the launch transcripts stay in the work dir for diagnosis until an
  operator removes them.
