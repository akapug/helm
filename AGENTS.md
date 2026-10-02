# AGENTS.md — working in the helm codebase

helm is an **agent-fleet coordination substrate**: Python standard library
only, one checkout, no build step, an overlay over `~/.helm`. This file orients
an AI agent (any harness) that is **contributing to the helm codebase**.

If instead you are running *as a seat inside a helm fleet*, read
[docs/NEW_AGENT_GUIDE.md](docs/NEW_AGENT_GUIDE.md) — that is the operating
manual (rooms, beacons, lanes, the review gate). This file is the contributor
guide: how to change helm's own code safely.

## Build & test

```console
$ ./bin/helm --help                          # runs from the checkout — nothing to install
$ ./bin/helm gate run --focus --plan         # the tests your change can reach; runs nothing
$ ./bin/helm gate audits -- <test modules>   # the tree-wide audits plus yours, as one command
```

- Tests are **`unittest`, not pytest**, and never touch a real `~/.helm`: they
  point `HELM_HOME` / `HELM_ADOPTED_DIR` at temp dirs. New tests do the same.
- **Focused rounds while you build; ONE whole suite when it lands.** Each
  round (a lane's, or a cure's) runs `helm gate run --focus` together with the
  command `helm gate audits` prints. The focused selection is the test
  modules the change's imports reach, and for a change outside the import
  graph (a doc, a script) every tree-wide audit and the tests that name the
  file. The audits import nothing they judge, so a Python change's selection
  does not reliably include them. The whole suite
  (`python3 -m unittest discover -s tests`) is the land gate: one whole suite
  on the exact tree that lands, sliced while the gate canary stands and
  serial otherwise. In helm's own tree `helm gate run` refuses a whole
  suite in a lane room (`--lane-suite --why TEXT` is the counted escape),
  refuses a second one on a tree that already has a green receipt, and runs a
  red tree again only with `--again`. The full process, and what each receipt
  authorizes: [How a change is tested](CONTRIBUTING.md#how-a-change-is-tested).
- **A serial whole-suite receipt always authorizes a land; a sliced one (v10)
  authorizes one only while the gate canary stands.** A whole suite with no
  mode flag in a lane-level room (a peek, a seat's home, a harness worktree,
  or a lane room admitted by `--lane-suite`) runs as parallel slices. It
  binds a lane tip and a review's APPROVE, and a land only while the canary
  stands; the land gate itself runs sliced under the canary and serial
  otherwise. A focused receipt (v6) binds a cure-round
  verdict only. `helm gate equiv` and a standalone `helm/gateshard.py` run are
  diagnostic: their results cannot mint or bind landing authority.
- **Where tests run.** A host with a local-suite guard (the fleet's hub is
  agents-only) refuses `python3 -m unittest` in any shape. There, run a
  focused round on the remote runner:
  `fab test --repo . -- python3 -m unittest <modules>`. Its Ran/OK line is
  testimony, never a receipt.
- **Python 3.9+.** The floor is declared in `scripts/install.sh` and the
  README. Maintainers test on newer interpreters; 3.9 is declared, not
  CI-tested. Use no syntax newer than 3.9.
- Mutation runs use `python3 scripts/mutation_matrix.py SPEC.json` against a
  committed baseline and tracked, clean targets. Each observation gets an
  isolated clone and parent-owned anonymous `unittest` receipt pipe; only
  verified restore, exact exit/status, unchanged count, and final control can
  yield a verdict. See
  [Mutation matrix](CONTRIBUTING.md#mutation-matrix) for the full contract.

## The laws new code obeys

Full text and rationale live in [CONTRIBUTING.md](CONTRIBUTING.md). In one breath:

1. **Zero dependencies** — stdlib only. A feature that needs a third-party
   library needs a different design.
2. **The two-source model** — every fact is an *event* or an *artifact*; every
   derived view is read-only-as-truth; every store names its source
   ([docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)).
3. **Additive & idempotent** — sync never deletes; cleanup is
   archive-with-a-reference-back, never deletion.
4. **The env2 pattern** — new config is a `HELM_*` variable with a working
   default; machine-local paths arrive by env, never as literals in code.
5. **Byte-shape-compatible store writes** — the store adopts the live Claude
   memory dir in place, so external hooks keep reading the same files; preserve
   exact frontmatter key order (round-trip test in `tests/test_store.py` first).
6. **Graceful degrade, never a traceback** — a missing substrate binary, an
   absent quota provider, a store that isn't there: each is one honest line and
   a working remainder.
7. **Mutations opt-in & reversible** — dry-run is the default for anything that
   moves files; logins and credential decisions stay human.

## The map

| path | what |
|---|---|
| `bin/helm` | the entry script (resolves through symlinks; the whole install is a PATH symlink). A coordination verb (`dispatch`, `chat`, `work`, …) runs trunk helm from any tree; `HELM_LANE_COORDINATION=1` runs this tree's own ([ENVIRONMENT](docs/ENVIRONMENT.md#which-helm-runs)) |
| `helm/` | the package — `cli.py` is the lazy dispatcher, per-verb modules beside it; `inject/`, `store/`, `work/`, chat, seats, `vcs.py`, … |
| `tests/` | the `unittest` suite |
| `docs/` | [VERBS.md](docs/VERBS.md) is the authoritative verb reference; plus ARCHITECTURE · CONCEPTS · ENVIRONMENT · HOOKS · WEB · ATTESTATION · EVOLUTION |
| `agents/claudecode/skills/` | the shipped skill layer — how an agent *uses* helm |
| `changes/` | one change note per lane, `changes/<lane>.md`, until a release folds them into `CHANGELOG.md` ([README](changes/README.md)) |

## Working style

- **Friction is a tax, and a cut that pays back fast goes first.** A step
  repeated by hand, a workaround of a hook or guard, a wait, a re-read or a
  false refusal is paid again by every later task that meets it. Name it when
  you meet it, and put its size on the task: steps (or minutes) times how
  often it happens per day, counted, never guessed (`helm friction` counts
  guard refusals; the chat and transcripts show the rest, counted from a
  bounded sample or through `fab`, never a scan of every transcript on the
  shared machine). A fix that removes
  it is a tax cut. One that pays back its build cost within about two days
  goes ahead of new features; report the tax it removed when it lands
  ([Choosing what to work on](CONTRIBUTING.md#choosing-what-to-work-on)).
- **Small diffs; match the surrounding idiom** and comment density. Read the
  neighbours before you write — code here should read as if one hand wrote it.
- **Adding a verb**: a `cmd_<name>` module, registered in `cli.py`'s `VERBS`
  via `_lazy`, a `_VERB_HELP` entry in `helm/cli_help.py`, an entry in
  [docs/VERBS.md](docs/VERBS.md), and a test. The dispatcher help and VERBS.md
  must never disagree — a verb without a doc entry is a bug this repo learned by
  audit.
- **Verify before you call it done**: the focused round and the tree-wide
  audits are green on your tip (the whole suite is the land gate's, on the tree
  that lands), the touched verb was actually run against real (or realistic
  temp) stores, and the docs that state the changed behaviour were updated in
  the *same* change. "Compiles" is not "done."
- **A change note is its own file**: write `changes/<lane>.md` (markdown
  bullets, written like a CHANGELOG bullet) and never edit `CHANGELOG.md` in
  a lane. Lanes that each added a bullet under `## Unreleased` conflicted in
  every train that carried two of them. `tests/test_change_notes.py` is red
  on a line a lane writes there; the release's `--fold` is the one writer
  ([A change note](CONTRIBUTING.md#a-change-note)).
- **Maintained as-public**: no secrets, no machine-local absolute paths (use
  `~` or an env var), no personal data, and no data that is not source
  (exports, dumps, snapshots, archives, address lists). The repo is kept at
  public quality at all times; the pre-commit guard enforces the staged set
  and the devops skill carries the scrub ladder for anything already in
  history.
- **Releasing** is one command, a dry run unless you pass `--publish`:
  `python3 scripts/release/release.py <version>`, after `--fold` has made the
  version's CHANGELOG section from the change notes. It fast-forwards the public
  main by one commit (the trunk tree minus `scripts/release/omit.txt`), tags
  it and creates the GitHub release. Its reports under the work directory
  (`~/.helm/releases/<version>/` by default) are for the owner to read before
  `--publish`; they hold private values, so never commit or paste them. See
  CONTRIBUTING.md, "Releasing".

## If you are a seat in a helm fleet

You additionally have the coordination physics, and they are not optional:

- **Claim a lane** — `helm work claim <lane>` gives you a lease + guarded
  worktree + branch in one verb. Never a raw `git worktree add`, and never edit
  the shared checkout directly (it is the integrator's tree).
- **Plan together before splitting work** — use a task-local meld to agree on
  the premise, invariants, acceptance checks, and who owns verification of the
  combined artifact. Split independent work, not responsibility for the seam;
  every family can plan, implement, and review. Keep detailed exchanges in that
  task's meld and send only decisions, blockers, and artifact pointers to the
  fleet room.
- **The independent review gate** — the load-bearing rule: a review counts
  when the reader is a different seat on a fresh session that holds none of
  the author's working context (a fresh-context subagent or Workflow run
  counts as its own reader, the author's own included, when it began after
  the tip was committed and wrote none of the lane; a run that built the
  lane never counts). A blind spot is a property of a shared frame, so a different model,
  and a different model family, are preferred and rank readers first, but
  neither is required. Every family is an equal counterpart here, including
  when a Claude seat holds the integrator chair.
- **A reviewer of either family fixes what it finds in the row's REVIEW FIX
  MODE.** For a MECHANICAL defect, PATCH commits off the exact reviewed tip —
  in a room the reviewer holds, or, for a subagent with no room of its own (the
  default), a `git clone --shared` clone whose commit is fetched into the repo
  (skill reviewer-implements-own-findings, step 3; never a shared branch and
  never `git worktree add`). For an explicit BUILD, a parent may instead assign
  its sole delegate a REGISTERED lane room (`helm work claim`); the parent owns
  the lease through completion or accepted handoff, then returns the room
  (`helm work release`); unfinished work is not released. This narrower build
  allocation is never general reviewer authority. Never an unregistered shared
  worktree, unleased shared branch, protected or shared ref rewrite, permission
  bypass, config/canon edit, test-home leak or publication. The reviewer does
  not push, and names that tip with `--fix --patch-tip <sha>`; the lane owner
  or integrator adopts it. MELD-DIFF posts the exact fix as a diff in the pair meld and records
  `--fix --diff-handoff ROOM/MSGID --no-patch-because <reason>` without a
  reviewer patch tip. The typed, validated message receipt (not the reason
  prose) and send/add proof that the first advancing direct child actually
  applies that diff let it count as cure confirmation rather than a new round
  or T1 design nudge. An unrelated direct child remains an ordinary round. A missing or invalid receipt, including on historical FIXes,
  proves no cure: `--no-patch-because` alone remains an ordinary round and T1
  design nudge. A DESIGN finding goes to a meld instead: one side's patch
  cannot settle a disagreement.
- **A lane may carry several authors** — the ledger records each, and
  `helm lr close --reason landed` credits every one the chain names. Agreement
  on a mechanical cure suffices for a reversible lane; a SAFETY DOOR or
  non-mechanical cure owes one re-read of the COMPOSED TIP by a reader who
  wrote none of it before the land gate — never a rule that one family may
  only look.
- **READY means an OUTSIDER approved the final tip** — and helm checks it. A
  chain contributor is any seat the ledger records as an author on any round of
  the chain: a round's sender, or the `patch_author` of a round whose FIX
  carried a cure. Patch a round and you are an author of that chain, so your
  own later APPROVE records and counts toward agreement but never carries the
  lane: the row stays `READY-SELF-REVIEW` and names you — *contributor approval
  present (`<seat>`), outsider approval missing*. Route the composed tip to a
  seat that wrote none of it. A sibling APPROVE on that exact tip must pass
  the same row-local tier, writer-capability and receipt-presence checks as
  the row being rendered — not a stricter kind or deep receipt replay policy.
  Unreadable identity or approval evidence is UNKNOWN (`READY-UNVERIFIED`),
  never permission; identity damage affects only connected work. Canonical
  seat comparison preserves recorded display names. Recorded contributors
  are submission provenance, not proven Git authorship. These readiness
  checks do not replace independent composed-tip review or the land gate.
- **A clean read with no gate is a HOLD, not an approve** — an APPROVE is
  refused without a verified `gate:<token>` from a whole suite, and a lane runs
  none of its own. Record the read with
  `helm dispatch hold <row> --source-clean <tip> <reason>`; the approve is then
  recorded against the token the integrator's land gate mints on the tree that
  lands. A CONCUR is not a way around it: it endorses and authorizes nothing.
- **Agree on the exact composed tip before shipping** — both counterparts must
  agree that no further patch is needed. Record any remaining disagreement in
  the task meld. A SAFETY DOOR or non-mechanical cure still owes independent
  composed-tip review; every lane owes changed-surface checks and the canonical
  land gate. A later patch requires renewed agreement and review of its
  affected surfaces. The integrator ships after those obligations and the
  existing release authority requirements hold.
- **Reviewed SHAs are immutable** — fix on top with a new commit; never amend
  or rebase a SHA that was posted for review. That holds for a reviewer's own
  cure too: it is a new commit off the reviewed tip, never a rewrite of it.
- **The store is the shared memory** — `helm store resolve "<the symptom in
  your own words>"` before treating any failure as novel; `/learn` captures a
  lesson so it fires for the next agent instead of being relearned.

### How a helm team is shaped

This is the shape helm's own fleet runs by. No code enforces it; the verbs
below assume it. Name the seats as you like, but keep the separations.

- **One front door for the owner, separate from the integrator.** The owner
  talks to one seat that answers for the whole fleet. It routes, summarizes
  and asks; it builds and lands nothing, so the owner's questions never wait
  behind a land.
- **One integrator per project.** It lands reviewed work onto trunk (with
  `helm train auto` doing the mechanics), keeps trunk green and owns the
  release.
- **Each project lead answers the owner for its project.** A lead is the
  owner's direct line for that codebase; the front door does not relay it.
- **One written home for each kind of knowledge.** Rules and lessons live in
  the store (`helm store`), work lives in tasks (`helm task`), and where a
  seat left off lives in its handoff (`helm handoff`). A fact written in two
  homes drifts; one written in none is relearned.

The full first-10-minutes walkthrough is
[docs/NEW_AGENT_GUIDE.md](docs/NEW_AGENT_GUIDE.md).
