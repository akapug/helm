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
$ python3 -m unittest discover -s tests      # the full suite; a few minutes
```

Optional PATH install (the entry script resolves the package through any
symlink):

```console
$ ln -s "$PWD/bin/helm" ~/.local/bin/helm
```

**Python floor: 3.9+.** CI runs the full suite on 3.9 through 3.13, so the
floor is measured, not asserted. (The source itself uses no syntax newer than
3.8 — an AST scan finds no walrus, no structural pattern matching, no
positional-only defs, and the newest hard runtime requirements are 3.7-era:
`subprocess.run(capture_output=)`, `ThreadingHTTPServer`, ordered dicts — but
3.8 is untested, so 3.9 is the supported floor.) The one version-gated
import — `tomllib`, 3.11+ — is guarded: TOML validation degrades to a warning
without it.

Tests are `unittest`, not pytest, and never touch your real `~/.helm` or
harness stores — they point `HELM_HOME` / `HELM_ADOPTED_DIR` at temp dirs.
New tests must do the same.

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
   working default; if it replaces a predecessor's variable, the legacy
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

## Adding a verb

Add `cmd_<name>` in its own module, register it in `cli.py`'s `VERBS` via
`_lazy`, add its one-liner to `_VERB_HELP`, document it in
[docs/VERBS.md](docs/VERBS.md), and test it. The dispatcher help and VERBS.md
must never disagree — a verb without a doc entry is a bug (this repo learned
that by audit).

## Verify before you call it done

`compiles` is not done. Before a change lands:

- the full suite passes: `python3 -m unittest discover -s tests`;
- the touched verb was actually run against real (or realistic temp) stores;
- for web changes, the affected view was exercised in a browser;
- docs that state the changed behavior were updated in the same change.

## Repo hygiene

This repo is maintained as-public: no secrets, no tokens, no personal data,
no machine-local absolute paths in code or docs (use `~` or env). Internal
planning files are gitignored — keep them out of commits.
