# Contributing to helm

helm is small on purpose: Python standard library only, one checkout, no
install step, no build. That makes the on-ramp short — and the laws below are
what keep it short.

> **Contributing with an AI agent?** [AGENTS.md](AGENTS.md) is the terse
> agent-facing version of this file — build/test, the laws in one breath, the
> repo map, and the working style. This document is the human-readable long
> form; they say the same things.

## Setup and tests

```console
$ git clone <your-fork-or-origin> helm && cd helm
$ ./bin/helm --help                          # runs from the checkout, nothing to install
$ ./bin/helm gate run --focus --plan         # the tests your change can reach; runs nothing
$ ./bin/helm gate audits                     # the tree-wide audits, as one command
```

Optional PATH install (the entry script resolves the package through any
symlink):

```console
$ ln -s "$PWD/bin/helm" ~/.local/bin/helm
```

Tests are `unittest`, not pytest, and never touch your real `~/.helm` or
harness stores — they point `HELM_HOME` / `HELM_ADOPTED_DIR` at temp dirs.
New tests must do the same. The whole suite is
`python3 -m unittest discover -s tests`. It is long, and in this tree it runs
once for each landing, as the land gate (below), not once for each change.

**Python floor: 3.9+.** The floor is declared in `scripts/install.sh` and the
README. Maintainers test on newer interpreters; 3.9 is declared, not
CI-tested. The one version-gated import — `tomllib`, 3.11+ — is guarded: TOML
validation degrades to a warning without it.

### How a change is tested

A change is tested by FOCUSED rounds while it is built and reviewed, and by ONE
whole suite when it lands. In helm's own tree, `helm gate run` enforces that
split; an adopter project's gate is unchanged.

- **A focused round.** `helm gate run --focus --plan` prints the selection
  and runs nothing. Every whole suite of helm's tree records which files each
  test module reached, and the plan uses the newest usable record: the test
  modules whose recorded run reached a file changed since the record's tree,
  every tree-wide audit, and each test module that names a changed file. The
  plan names the record and its tree. With no usable record the plan falls
  back to the test modules whose imports reach a changed module (for a change
  outside the import graph, every audit plus the tests that name the file),
  and says why. `helm gate run --focus` runs that selection and mints a
  focused receipt. A selection of six or more modules runs as parallel
  slices on the workers the host grants; a smaller one runs in one
  process. The run says which runner it used and why.
- **The tree-wide audits, in every round.**
  `helm gate audits -- <your test modules>` prints one command that runs
  every audit plus the modules you name. An audit reads the whole tree
  instead of importing what it judges. A recorded plan always carries the
  audits; the import fallback does not reliably carry them for a Python
  change. If you skip this step on the fallback, a new module, verb or
  docstring reference meets the audits for the first time at the land gate.
- **A red round.** Cure it and run the focused round again. A whole suite
  does not run twice on the same tree without a reason: a tree whose last
  whole-suite receipt is red runs again only with `--again` (a suspected
  flake), and a tree that already has a green one that can authorize a land
  of it is refused, with that receipt's evidence line printed for you to
  cite. A green that cannot land (a sliced receipt, or one only a generic
  import placed) does not stop the land's own suite; "can land" is
  foldcheck's own tree-vs-gate rung.
- **The whole suite belongs to the land gate.** The land gate is one whole
  suite, on the exact tree that lands (sliced while the gate canary stands,
  serial otherwise): the integrator's train. In a lane
  room, `helm gate run` refuses a whole suite and prints the focused route
  instead. The escape is `--lane-suite --why TEXT`: the reason goes on the
  receipt label, and every escape is counted.

What each run can authorize:

| run | receipt | binds | never binds |
|---|---|---|---|
| `helm gate run --focus` | focused (v6) | a cure-round verdict (FIX, SUPERSEDE, CONCUR) at the exact tip | an APPROVE, a land |
| a whole suite with no mode flag in a lane-level room: a peek, a seat's home, a harness worktree, or a lane room admitted by `--lane-suite` | sliced (v10) | a lane tip, a review's APPROVE; a land only while the gate canary stands | a land while the canary does not stand |
| a whole suite anywhere else: the shared checkout, a compose or train room, a `train...` label, a Fab job, or `--serial` | serial, except the land gate `helm gate window launch`, which runs as slices (v10) while the gate canary stands | a lane tip, an APPROVE, a land | — |

**Sliced is the fast whole suite, and it lands only under the canary.** It
runs the suite as parallel slices of one serial discovery. Every worker must agree on one
ordered test inventory, and a leak audit fails any module that leaves process
state behind. What it cannot see is data that one module leaves in a shared
module object for a module on a different worker. A serial run sees that
failure and a sliced run can miss it, so every land door refuses the sliced
kind by name unless the gate canary stands: no DISABLE marker, a clean
one-pass finder run (every module alone in a fresh process), a leak-free
report-mode sliced suite, three trees on two hosts whose serial and sliced
runs agreed test for test, a red tree whose real failures the sliced run
caught too, and evidence under 36 hours old. The
nightly canary compares a serial and a sliced run of trunk, and its first
divergence writes the DISABLE marker, which sends every land back to serial.
A lane-level room that cannot run slices (fewer than four CPUs, or a tree
whose own `helm/gate.py` predates the kind) runs serial and says why.
`helm gate equiv` and a standalone `helm/gateshard.py` run are diagnostic
fresh-worker tools: their results cannot mint or bind landing authority.

**Review and landing.**

- A reviewer whose source read is clean, with no whole-suite receipt to cite,
  HOLDS the row: `helm dispatch hold <row> --source-clean <tip> <reason>`. An
  APPROVE is refused without a verified `gate:<token>` from a whole suite; it
  is recorded against the token the land gate mints on the tree that lands. A
  CONCUR is not a way around that: it endorses the work and authorizes nothing.
- The integrator's land gate is one whole suite for each landing window (one
  trunk head), run as slices while the canary stands and serial otherwise.
  The durable road is `helm gate window launch`,
  which `helm train --apply` and `helm gate run` in a compose room both use.
  It records the window before it dispatches a keyed job, refuses a second
  whole suite on the same window, and leaves a detached client that fetches
  and imports the receipt. The classic road is `fab gate` on a train room
  that stands outside the compose container. It runs serial, and its
  receipt comes home only through the client that launched it, which imports
  it with `helm gate import`. In a compose room the classic road is refused,
  and the refusal names the durable one. `--sliced` is a usage error in a
  compose room and beside a `train...` label.

**Where tests run.** A host can refuse local suites. The fleet's hub is
agents-only: suites run on the build fabric, and a machine-local guard refuses
`python3 -m unittest` in any shape. `helm doctor`'s interpreter-startup row
says whether this interpreter refuses one. On such a host, a focused round
runs on the remote runner:
`fab test --repo <room> -- python3 -m unittest <modules>`.
Its Ran/OK line is testimony and not a receipt: a focused receipt
routed through `fab gate` cannot come home, because `helm gate import` refuses
a focused artifact. Whole suites go through `fab gate`, which asks
`helm gate run --plan` first and so meets every door above. On a machine with
no such guard, run the same module list with `python3 -m unittest`.

### Mutation matrix

Commit the baseline before mutation testing, then describe byte-exact mutants in
JSON and run:

```console
$ python3 scripts/mutation_matrix.py matrix.json
```

```json
{
  "command": ["python3", "-m", "unittest", "tests.test_example"],
  "timeout_seconds": 300,
  "mutations": [
    {
      "name": "remove-example-guard",
      "path": "helm/example.py",
      "find": "    if guarded:\n        return\n",
      "replace": ""
    }
  ]
}
```

`command` must be a Python `-m unittest` invocation. The runner resets
`sys.argv`, owns test loading, and receives the real result/count over a
parent-created anonymous pipe that is absent from test arguments, so
footer-shaped output and forged pathname receipts are never evidence.
`timeout_seconds` must be finite, positive, and at most 86,400; one monotonic
deadline covers the test leader and nonblocking receipt drain. A surviving
process-group member is killed and makes the run `MATRIX-ERROR`, never a
verdict. Mutation names are trimmed printable ASCII so result lines cannot be
injected; paths must be normalized relative, UTF-8-encodable, printable text,
and `find` and `replace` must encode as UTF-8. Targets must be tracked, clean in
index and worktree, free of semantic index flags, and every UTF-8 `find` anchor
must occur exactly once.

The baseline, each mutant, and final control run in separate disposable clones
at the full HEAD captured on startup, so repository-local test state cannot leak
between observations. Tests must still be deterministic with respect to state
outside the repository. Each mutant is an atomic byte replacement. Before any
verdict, the runner restores every declared target with `git restore
--source=<captured-full-sha> --staged --worktree -- <literal-path>`, removes the
clone, and proves the caller's symbolic+commit HEAD identity plus target diffs,
semantic index flags, blob/mode, and bytes. The mutant and controls must all run
the same nonzero test count. Only exact process/result pairs are verdicts:
exit 0 with `OK` is `SURVIVED`, and exit 1 with `FAILED` is `KILLED`.

Signals, other exits, apply, collection/import, count, command,
timeout/interruption, Git, checkout, or restore failures print `MATRIX-ERROR`
and can never print `ALL-KILLED`.

Exit 0 means a valid all-killed matrix, exit 1 means one or more valid survivors,
and exit 2 means the matrix itself was invalid. An unexpected survivor is a
finding: delete genuinely inert code or add a direct primitive-contract witness
that kills it; never dismiss it.

The matrix runs its `unittest` children on the machine that starts it, so on a
host that refuses local suites (see "Where tests run" above), start it where
suites may run.

## Choosing what to work on

Friction is a cost, and we count it: the friction tax. Each step repeated by
hand, each workaround of a hook or guard, each wait, re-read, re-ask or false
refusal is paid again by every later task that meets it. When you meet
friction, name it and file it with its size: steps (or minutes) times how
often it happens per day, counted, never guessed. `helm friction` counts
guard refusals from the friction ledger; the chat and the transcripts show
the rest. Count from a bounded sample (a few hundred transcripts at most, or
a few minutes of work) or run the count through `fab`: a scan of every
transcript on the machine the agents share can stall every one of them. A fix that removes it is a tax cut, and its payback time is its
build cost divided by the tax it removes each day. A tax cut that pays back
within about two days goes ahead of new features. When it lands, say how
much tax it removed. For example, landing a change by hand once took about 15
steps, about 20 times a day; automating the land cut about 300 steps a day
and paid back in about a day.

## The laws new code obeys

1. **Zero dependencies.** Stdlib only — no pip, no vendored packages, no
   optional imports that change behavior beyond graceful degrade. A feature
   that needs a third-party library needs a different design.

2. **The two-source model** (the constitution — full text in
   [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)):
   - Every fact is an **event** (immutable, append-only) or an **artifact**
     (authored content with identity). Events point at artifacts, never back.
   - Every derived view (index, report, registry) is **read-only as truth**:
     change the source, let the view re-derive. Never store the same fact in
     two writable places.
   - Every store **names its source** — source, projection, replica, or
     trash. A store that can't is pretending to be canonical.
   - **One recall index.** helm records how to query it; it never builds a
     second one.

3. **Additive and idempotent.** Sync never deletes a known project. Cleanup
   means archive-with-a-reference-back, never deletion. Lifecycle verbs
   tombstone; files are kept as the record.

4. **The env2 pattern.** New configuration is a `HELM_*` variable with a
   working default; if it replaces an older variable, the legacy
   spelling is read as a fallback forever and written never (see
   [docs/ENVIRONMENT.md](docs/ENVIRONMENT.md)). Machine-local paths never
   live in code — they arrive by env.

5. **Store writes are byte-shape-compatible.** The typed store adopts the
   live Claude memory dir in place: external hooks keep reading and writing
   the same files. Writers must preserve the exact frontmatter key order and
   serialization the incumbent writers produce — a helm write followed by an
   external read (or vice versa) must be invisible. New writes never target
   the adopted root unless explicitly pointed there; lifecycle updates write
   back in place wherever the entry lives. When in doubt, extend
   `tests/test_store.py` with a round-trip case first.

6. **Graceful degrade, never a traceback.** A missing substrate binary, an
   absent quota provider, a harness store that isn't there — each is one
   informative line and a working remainder. New legs are dispatched lazily
   (`cli.py`'s `_lazy`) so one broken leg never takes the CLI down, and web
   endpoints answer `{"unavailable": true}` rather than 500.

7. **Mutations are opt-in and reversible.** Dry-run is the default for
   anything that moves files (`helm drain`); destructive-looking web verbs are
   renames/moves behind the mutation token; config writes are
   backup → validate → atomic. Logins and credential decisions stay human.

## Adding a module

A new file under `helm/` owes the tree's registries, and an author who does not
know that discovers them one red gate at a time. The full list — what each one
demands and the exact edit that satisfies it — is
[docs/MODULE_REGISTRIES.md](docs/MODULE_REGISTRIES.md). The short version: wire
it (or defend it in `helm/wiring.py`'s `ALLOWED` with a reason), give it a test
that imports it, keep it stdlib-only, and register whatever your module *does* —
roster reads, bare seat-name resolution, direct `git` spawns, stores under the
helm home, new `HELM_*` variables. The Stop hook catches the first of those.
Tree-wide audits enforce the rest. Run the command `helm gate audits` prints
in each focused round: a lane that skips it learns them at the land gate's
whole suite, which is the slowest place in the tree to learn anything.

## Adding a verb

Add `cmd_<name>` in its own module, register it in `cli.py`'s `VERBS` via
`_lazy`, add its help entry to `_VERB_HELP` in `helm/cli_help.py` (the root
listing, `helm --help`, prints one line cut from that entry: its usage with
the bracketed groups taken out but an optional subverb group such as
`[show|record]`, all of it one optional group when the verb runs bare, then
its first sentence; a verb that runs bare says so in its entry, by bracketing
the subverb bare runs, as `codex [list]|pool <name>`, or by a form of flags
alone, as `ship [--apply] | ship pull`),
document it in [docs/VERBS.md](docs/VERBS.md), and test it. The dispatcher help
and VERBS.md must never disagree — a verb without a doc entry is a bug (this repo learned
that by audit).

## Verify before you call it done

`compiles` is not done. Before a change lands:

- the focused round and the tree-wide audits pass on your tip, and the whole
  suite passes at the land gate on the tree that lands (see
  [How a change is tested](#how-a-change-is-tested));
- the touched verb was actually run against real (or realistic temp) stores;
- for web changes, the affected view was exercised in a browser;
- docs that state the changed behavior were updated in the same change.

## Repo hygiene

This repo is maintained as-public: no secrets, no tokens, no personal data,
no machine-local absolute paths in code or docs (use `~` or env). Internal
planning files are gitignored — keep them out of commits. Data that is not
source — exports, dumps, snapshots, archives, address lists, blobs over the
scanner's ceiling — never enters history in ANY repo; the pre-commit guard
(`helm work install-guard --apply`, `helm/nevertrack.py`) refuses it, and
the devops skill's "Repository Hygiene" section carries the scrub ladder
for a leak already in history (untracking is not a scrub).

## Releasing

A release is one command. It is a dry run unless you pass `--publish`.

```console
$ python3 scripts/release/release.py 0.3.2              # dry run: every step except the network writes
$ python3 scripts/release/release.py 0.3.2 --publish    # stage, push, tag, create the GitHub release
```

The public repository's `main` is a line of release commits. Each release is
one commit on top of the previous one. Its tree is the trunk tree minus the
paths in `scripts/release/omit.txt`, and its author is the public
repository's owner (the GitHub noreply identity). So a release is a
fast-forward: it never rewrites history and never force-pushes. The
development history stays in the private repository.

Before you run it:

- `CHANGELOG.md` has a `## <version>` section. That section is the release
  notes.
- `helm/__init__.py` declares that `__version__`.
- The trunk commit passed the land gate. The command does not run the whole
  suite again: it proves that the release tree is the trunk tree minus the
  omit list, byte for byte.

What it does, in order:

1. **Read.** The trunk commit (`--trunk`, default `main`), the public main
   (`--public`, default the remote `aspublic`) and the public tags. It
   refuses a version whose tag is already public.
2. **Back up.** A bare mirror of the public repository in the work directory,
   `git fsck` clean, its main equal to the public main it read. The work
   directory is kept, so it must survive a reboot: by default each run gets a
   new directory under `~/.helm/releases/<version>/`, and a `--work` on a
   memory filesystem (`tmpfs`, `ramfs`; `/tmp` is one on many hosts), on a
   filesystem the mount table cannot name, or inside the checkout is refused.
3. **Build.** The candidate commit and the annotated tag `v<version>`, in a
   work repository. The same inputs build the same commit, so the sha that a
   dry run prints is the sha that `--publish` pushes.
4. **Gate.** The candidate must fast-forward the public main by exactly one
   commit that changes the tree, and that tree must be the trunk tree minus
   the omit list. Then the command writes the reports you read before a
   public release, under `<work>/reports/` and never in the repository:
   `world_audit.txt` (private tokens, seat names, addresses that are not
   placeholders, task cites, dates and the owner's voice, counted per class
   with the files that carry them), `seat_attribution.txt`, `arm_attr.txt`
   and `content.txt`. They hold private values, so they are readable by you
   only and the terminal shows counts. The seat-attribution gate refuses a
   line in `helm/` that names a seat as the one who found or reviewed
   something unless the KEEP list names it, and a KEEP entry that matches no
   line. Then the verify battery runs on a fresh clone: no AI
   authoring line and no private needle in any commit message, the tag
   message or the notes; no forbidden filename; no private needle in any
   file; `bin/helm --help`; `bin/helm doctor` in a scratch `HELM_HOME`;
   `scripts/install.sh`; and `gitleaks`.
5. **Write, with `--publish` only.** Push the candidate to
   `release/<version>` on the private repository (`--private`, default
   `origin`); push it to the public `main` with no force; push the tag; run
   `gh release create v<version> --verify-tag` with the CHANGELOG section as
   the notes; then run the battery again on a fresh clone of the public
   repository. The dry run prints each of these writes and performs none.

Exit status: 0 when the dry run or the publish succeeded; 1 when a read or a
gate refused, before any network write; 3 when the publish stopped part-way
(a write failed or timed out, or the public main moved after the stage write;
the output lists the writes still owed, as commands) or when the published
repository failed the battery. Any other error is printed as a refusal,
redacted like every other line and never as a traceback: exit 1 before the
first network write began, exit 3 after it. What the interpreter prints when
even that line cannot be printed (stdout closed under it) is redacted the
same way.

The private needles and the machine-local never-track paths come from
outside the tree (`HELM_PRIVATE_NEEDLES` and `HELM_NEVER_TRACK_LOCAL`; see
`helm/nevertrack.py`). `--publish` refuses when no needles are loaded, and
when `gitleaks` is not installed unless you pass `--without-gitleaks`.

Two more files come from outside the tree, because they name the private
things the tree must not: the KEEP list (`--keep-file`, default
`~/.config/helm/release-attr-keep.json`, a JSON list of `{"file", "text",
"reason"}`) and the private patterns (`--audit-file`, default
`~/.config/helm/release-audit.json`, a JSON object with the regular
expressions `seat`, `private_token` and `owner` and a `literals` object of
labelled strings). Each must be readable by its owner only (`chmod 600`) and
must not be inside the checkout; one that is absent, loose or malformed is
not read, and the seat-attribution gate then refuses, in a dry run and in
`--publish`.

To publish a candidate that was already reviewed, pass `--candidate <rev>`
(for example the staged `origin/release/<version>`). If the public main moved
since that candidate was cut, the push would not fast-forward, and the
command refuses before it writes anything. `--help` lists every option;
`tests/test_release_tool.py` drives the command against a local bare
"public" repository and a stub `gh`.
