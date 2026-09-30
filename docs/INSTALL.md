# Installing and running helm

helm is Python standard library over one folder, `~/.helm`. There is nothing
to build, no virtualenv and no package to install: a checkout runs as it is.
This page covers what helm needs, how to put it on your `PATH`, what the first
run does, and how to wire it into a fleet.

## Requirements

**Python 3.9+ on Linux, and nothing else.** That floor is declared here, in the
[README](../README.md#requirements) and in `scripts/install.sh`. Maintainers
test on newer interpreters (the land gate runs a current CPython); 3.9 is
declared, not CI-tested. `tomllib` (3.11+) is used when present, and config
validation degrades to a warning without it.

**Linux specifically, and stdlib-only does not mean portable.** helm's chat
lives in RAM at `/dev/shm`, it reads process liveness from `/proc`, locks with
`fcntl`, supervises nodes through user systemd units, and checks ownership with
`os.getuid`. Those are POSIX-and-then-some concerns, not Python-version ones:
macOS has no `/dev/shm`, and Windows has neither `fcntl` nor `/proc`. Nothing
here is a deliberate exclusion; it is what "coordination state is memory, disk
is a write-behind log" costs on the platform helm was built for. A port is
possible and is not currently claimed, tested, or supported. (`helm chat` on
macOS uses a private per-user temp directory instead of crashing, but macOS is
still not a supported platform.)

The zero-dependency promise is checked by the test suite against every import
under `helm/`, not by prose.

## Run from the checkout

```console
$ git clone https://github.com/akapug/helm && cd helm
$ ./bin/helm --help        # one line per verb
$ ./bin/helm sync          # discover your projects across every harness
$ ./bin/helm doctor        # what helm can and cannot see (read-only)
```

## Put it on your PATH (optional)

The entry script resolves the package through symlinks, so putting it on your
`PATH` is the whole install:

```console
$ sh scripts/install.sh              # -> ~/.local/bin/helm, then verifies it runs
$ sh scripts/install.sh --dry-run    # say what would happen, change nothing
$ sh scripts/install.sh --prefix DIR # somewhere else
$ sh scripts/install.sh --uninstall  # remove a link this script created
```

It checks your Python first (a `helm` on `PATH` that cannot start is worse than
no `helm`), refuses to overwrite anything it did not create, and finishes by
actually running the installed binary rather than assuming the link works. If
you would rather do it by hand, that is still all it is:

```console
$ ln -s "$PWD/bin/helm" ~/.local/bin/helm
```

## What the first run does

`helm sync` scaffolds `~/.helm`: the global chain plus one directory per
discovered project. If you already use Claude Code, the typed store **adopts
your live memory directory in place**. The resolver reads
`~/.claude/projects/<slug-of-home>/memory` as one more root: same files, no
copy, and writes in the same byte shape, so your existing hooks keep working
untouched. New helm entries land in `~/.helm`, never there. Only the lifecycle
verbs (evidence, supersede, retire) write back wherever an entry lives,
adopted entries included, and those retire in place and never delete: the file
stays as the record. [ARCHITECTURE](ARCHITECTURE.md#the-typed-store) states
the full adoption contract.

On a fresh machine with no harness stores at all, everything still works: sync
scaffolds an empty home, the project list is empty until an agent runs
somewhere, and `helm doctor` tells you exactly what it is (and is not) seeing.
Quota, recall and the attestation substrate are all optional, and each
degrades to one informative line.

## Wire it into a fleet

The cockpit (projects, sessions, the typed store, credentials, the web app)
needs nothing more. The fleet half needs three things:

1. **The Claude Code hooks.** `helm hooks install` writes helm's hook set into
   every Claude Code config directory it knows (`--dry` shows the diff and
   writes nothing), and `helm hooks status` shows which homes and seats are
   covered. Landing new helm code arms nothing by itself.
   [HOOKS](HOOKS.md) is the full reference.
2. **A metaharness**, so helm can spawn and read agent panes:
   [orca](https://github.com/stablyai/orca) (recommended) or
   [herdr](https://github.com/herdrdev/herdr). Without
   one, every pane operation is a no-op that says so.
3. **The substrate you want**: [dregg](https://github.com/emberian/dregg) for
   signed chat transport and ledger anchoring,
   [cv](https://github.com/emberian/cv) for cross-harness session recall, and
   [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI) for non-Claude
   model families inside Claude Code. Each is optional and each degrades to one
   line; `helm doctor` and `helm capabilities` say what is wired.

Then seat agents. `helm launch` starts Claude Code already seated in the
fleet room, and `helm seat spawn` starts a seat of a named model family in its
own pane, worktree and credential pool. An agent that joins reads
[NEW_AGENT_GUIDE](NEW_AGENT_GUIDE.md) first; the join banner points there.

The repository also ships the Claude Code skills its own fleet works with
(the work loop, repair, cross-family review and more), one directory each
under `agents/claudecode/skills/`. They are optional; add the ones you want
to a Claude Code skills directory.

[TOUR](TOUR.md) walks through what the fleet can then do, and
[LANDING](LANDING.md) describes how a change gets from a lane to trunk.

## Configuration

helm takes no config file for its knobs: every knob is an optional `HELM_*`
environment variable with a working default, and a fresh clone needs none of
them. `HELM_HOME` overrides the default `~/.helm`. Facts about one host (model
endpoints, local names) are small JSON files under the helm home, with
examples in this directory. [ENVIRONMENT](ENVIRONMENT.md) lists every variable
and every host-fact file, including the legacy spellings that are still read as
fallbacks.

## Immutable release (preview, not cut over)

`scripts/deploy.py` can materialize one committed helm tree as a real,
non-writable filesystem release. It is a standalone Python 3.9+ stdlib script
and does not import the mutable `helm` package it is deploying:

```console
$ python3 scripts/deploy.py --dry-run
$ python3 scripts/deploy.py                    # -> ~/.local/share/helm/artifacts
$ python3 scripts/deploy.py --root /other/root
$ ~/.local/share/helm/artifacts/bin/helm --version
```

The deployer refuses staged or unstaged tracked changes, resolves the requested
commit and tree once, and materializes only `git archive <commit>` bytes.
Untracked, ignored, and working-tree-only files therefore cannot enter the
artifact. Each `releases/<full-commit>/` directory carries a schema-versioned
manifest with the commit, tree, package version, and deterministic content
digest. A stable regular-file launcher resolves the relative `current` symlink
once and executes the concrete release path with bytecode writes disabled. Old
releases are retained.

**This does not cut any existing consumer over.** It does not install on
`PATH`, repoint hooks, change seat lifecycle, or modify systemd, cron, Git hook,
or consumer settings. The `PATH` install above still links directly to a
mutable checkout; the artifact launcher is an isolated-runtime preview until a
later, separately reviewed cutover.

## Running the tests

The suite is `python3 -m unittest discover -s tests`. How a change is tested
in this tree (focused rounds while it is built, one whole suite when it
lands: sliced while the gate canary stands, serial otherwise) is in [CONTRIBUTING](../CONTRIBUTING.md#how-a-change-is-tested).
