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
$ python3 -m unittest discover -s tests      # the full suite, a few minutes
```

- Tests are **`unittest`, not pytest**, and never touch a real `~/.helm`: they
  point `HELM_HOME` / `HELM_ADOPTED_DIR` at temp dirs. New tests do the same.
  The authoritative `helm gate run` executes literal same-process serial
  unittest discovery; only that serial receipt can bind landing authority.
  `helm gate equiv` and `helm/gateshard.py` are diagnostic observations whose
  fresh-worker results cannot mint or bind landing authority.
- **Python 3.9+.** The floor is declared in `scripts/install.sh` and the
  README. Run the suite with `python3 -m unittest discover -s tests`.
  Maintainers test on newer interpreters; 3.9 is declared, not CI-tested.
  Use no syntax newer than 3.9.
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
| `bin/helm` | the entry script (resolves through symlinks; the whole install is a PATH symlink) |
| `helm/` | the package — `cli.py` is the lazy dispatcher, per-verb modules beside it; `inject/`, `store/`, `work/`, chat, seats, `vcs.py`, … |
| `tests/` | the `unittest` suite |
| `docs/` | [VERBS.md](docs/VERBS.md) is the authoritative verb reference; plus ARCHITECTURE · CONCEPTS · ENVIRONMENT · HOOKS · WEB · ATTESTATION · EVOLUTION |
| `agents/claudecode/skills/` | the shipped skill layer — how an agent *uses* helm |

## Working style

- **Small diffs; match the surrounding idiom** and comment density. Read the
  neighbours before you write — code here should read as if one hand wrote it.
- **Adding a verb**: a `cmd_<name>` module, registered in `cli.py`'s `VERBS`
  via `_lazy`, a `_VERB_HELP` one-liner, an entry in
  [docs/VERBS.md](docs/VERBS.md), and a test. The dispatcher help and VERBS.md
  must never disagree — a verb without a doc entry is a bug this repo learned by
  audit.
- **Verify before you call it done**: the full suite is green, the touched verb
  was actually run against real (or realistic temp) stores, and the docs that
  state the changed behaviour were updated in the *same* change. "Compiles" is
  not "done."
- **Maintained as-public**: no secrets, no machine-local absolute paths (use
  `~` or an env var), no personal data, and no data that is not source
  (exports, dumps, snapshots, archives, address lists). The repo is kept at
  public quality at all times; the pre-commit guard enforces the staged set
  and the devops skill carries the scrub ladder for anything already in
  history.

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
- **The cross-family review gate** — the load-bearing rule: the author's model
  family may not be the reviewer's. A blind spot is a property of a shared
  frame, so a different family catches what same-family review nods through.
  Every family is an equal counterpart here, including when a Claude seat holds
  the integrator chair.
- **A reviewer of either family PATCHES what it finds** — a MECHANICAL defect
  is cured by whoever found it: commit it in your own worktree on a branch off
  the exact tip you reviewed, do not push, and name the tip on the verdict
  (`helm dispatch verdict <id> <tip> --fix --patch-tip <sha>`). The lane owner
  or integrator rebases the lane onto that tip or cherry-picks it. A DESIGN
  finding goes to a meld instead, because a design disagreement settled by one
  side's patch is the disagreement unrecorded.
- **A lane may carry several authors** — the ledger records each, and
  `helm lr close --reason landed` credits every one the chain names. What keeps
  the families independent is that the COMPOSED TIP is re-read once by a reader
  who wrote none of it, before the land gate — never a rule that one family may
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
- **Agree on the exact composed tip before shipping** — both counterparts must
  agree that no further patch is needed. Record any remaining disagreement in
  the task meld. Consensus does not replace independent composed-tip review,
  changed-surface checks, or the canonical land gate; a later patch requires
  renewed agreement and review of its affected surfaces. The integrator ships
  after those obligations and the existing release authority requirements hold.
- **Reviewed SHAs are immutable** — fix on top with a new commit; never amend
  or rebase a SHA that was posted for review. That holds for a reviewer's own
  cure too: it is a new commit off the reviewed tip, never a rewrite of it.
- **The store is the shared memory** — `helm store resolve "<the symptom in
  your own words>"` before treating any failure as novel; `/learn` captures a
  lesson so it fires for the next agent instead of being relearned.

The full first-10-minutes walkthrough is
[docs/NEW_AGENT_GUIDE.md](docs/NEW_AGENT_GUIDE.md).
