# Contributing to helm

helm is small on purpose: Python standard library only, one checkout, no
install step, no build. That makes the on-ramp short — and the laws below are
what keep it short.

## Setup and tests

```console
$ git clone <your-fork-or-origin> helm && cd helm
$ ./bin/helm --help                          # runs from the checkout, nothing to install
$ python3 -m unittest discover -s tests      # the full suite; a few seconds
```

Optional PATH install (the entry script resolves the package through any
symlink):

```console
$ ln -s "$PWD/bin/helm" ~/.local/bin/helm
```

**Python floor: 3.8+.** The source uses no syntax newer than that (verified by
AST scan — no walrus, no structural pattern matching, no positional-only
defs); the newest hard runtime requirements are 3.7-era
(`subprocess.run(capture_output=)`, `ThreadingHTTPServer`, ordered dicts). The
one version-gated import — `tomllib`, 3.11+ — is guarded: TOML validation
degrades to a warning without it.

Tests are `unittest`, not pytest, and never touch your real `~/.helm` or
harness stores — they point `HELM_HOME` / `HELM_ADOPTED_DIR` at temp dirs.
New tests must do the same.

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
