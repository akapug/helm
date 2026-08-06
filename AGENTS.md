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
- **Python 3.9+.** CI runs the suite on 3.9 → 3.13, so the floor is measured,
  not asserted. Use no syntax newer than 3.9.
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
  `~` or an env var), no personal data. The repo is kept at public quality at
  all times.

## If you are a seat in a helm fleet

You additionally have the coordination physics, and they are not optional:

- **Claim a lane** — `helm work claim <lane>` gives you a lease + guarded
  worktree + branch in one verb. Never a raw `git worktree add`, and never edit
  the shared checkout directly (it is the integrator's tree).
- **The cross-family review gate** — the load-bearing rule: the author's model
  family may not be the reviewer's. A blind spot is a property of a shared
  frame, so a different family catches what same-family review nods through.
- **Reviewed SHAs are immutable** — fix on top with a new commit; never amend
  or rebase a SHA that was posted for review.
- **The store is the shared memory** — `helm store resolve "<the symptom in
  your own words>"` before treating any failure as novel; `/learn` captures a
  lesson so it fires for the next agent instead of being relearned.

The full first-10-minutes walkthrough is
[docs/NEW_AGENT_GUIDE.md](docs/NEW_AGENT_GUIDE.md).
