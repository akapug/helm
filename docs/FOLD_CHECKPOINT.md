# The saved fold

Reading the dispatch ledger means folding every event in it. That fold asks git
questions — about three thousand git processes on today's board — and takes
roughly two minutes cold. Almost every read does not have to pay that, because the fold is
**checkpointed**: the answer is saved, and the next read restores it instead of
recomputing it.

This page is for the operator watching a read take two minutes and wondering
whether something is wrong. Usually nothing is: a checkpoint was discarded for
a stated reason, and the reason is worth reading.

## What you should see

| you just | the next fresh read costs |
|---|---|
| landed something touching `helm/*.py` | about two minutes, once |
| landed only tests, docs or web assets | about a second |
| nothing — trunk has not moved | about a tenth of a second |
| force-pushed, rewound or rewrote trunk | about two minutes, once |

Only the FIRST read after an invalidation pays. It writes a fresh checkpoint,
and every reader after it restores.

If a two-minute read happens when the table says a second, that is worth
looking into. If several seats read at once in the window after a land they all
pay it, because each started before any of them finished writing.

## Where it lives

`_global/.state/dispatch-fold/` under the helm home — so `HELM_HOME` isolates
it, which is what keeps it from being a channel between tests. One file per
code version and per gate-epoch lens, the newest 8 kept. Deleting the directory is safe: the next
read folds cold and writes a new one. Nothing else reads these files, and
nothing is lost by removing them — a checkpoint is a cache of an answer the
ledger can always reproduce.

## What makes one stale

A checkpoint is only served when EVERY key still holds. Each check has its own
sentence, and they are separate on purpose: a reader chasing a slow fold should
learn which fact changed, not merely that something did.

**The code that computed it**

- `written by other code` — any `.py` under the package changed. This is why a
  land touching `helm/*.py` always costs a cold read, whatever trunk did. The
  key is deliberately coarse: it hashes every module rather than the ones the
  fold imports, so it can only ever be too conservative. Narrowing it was
  measured and declined; see task/2860 for the numbers.
- `not a checkpoint of this format` — written by a different format or version.

**The gate epoch**

- `folded under a different gate-epoch lens`
- `the gate-epoch marker moved`

**The ledger underneath it**

- `the ledger is shorter than the checkpoint` — the file it describes no longer
  reaches that far.
- `the ledger prefix was rewritten` — the bytes it folded are not the bytes
  there now.
- `the payload is not the one the header describes` — the saved answer failed
  its own digest.

**The repositories it read**

- `a repository path resolves differently` — a path the fold read now realpaths
  elsewhere.
- `git's view of <dir> changed` — git's config, replace refs, attributes or
  version moved. Those change what git ANSWERS without changing any id, so the
  fingerprint covers them.
- `an object or ref the fold read could not be re-read in <dir>` — git did
  not answer for every expression the fold recorded, so nothing can be
  compared.
- `an object or ref the fold read changed in <dir> (<expr>)` — something the
  fold resolved is not what it was, and it is not the one movement a restore
  survives; the four refusals below spell which.

**The checkpoint file itself**

- `malformed realpaths` and `malformed git section` — the header is not the
  shape this code writes; treated exactly like a checkpoint of another format.

## When a restore survives a land

A land moves trunk, so every recorded expression naming it resolves to a new
commit. That used to discard the checkpoint every time.

**A trunk that ADVANCED is not a trunk that changed.** When the commit the fold
proved against is an ANCESTOR of the one that replaced it, the checkpoint is
restored. One `rev-parse` and one `merge-base` per repository, asked only about
the expressions that actually moved.

This is safe because of what the fold reads git FOR. Only the `carried` close
replay consults trunk at all; every other close reason is arithmetic over
immutable ids. Measured: with that one witness stubbed, a cold fold spawns zero
git processes while 1,177 non-carried closes across nine other reasons replay
unchanged. And a carried close now stays proven when its own work reaches trunk
(task/2863), so advancing trunk cannot change the answer.

`tests/test_foldckpt.py` holds one arm per way trunk can move, each saying by
name whether it restores or replays.

**Four ways an advance is refused instead**, and they are distinct because they
mean different things:

- `an object or ref the fold read changed in <dir> (<expr>)` — what moved is
  not a commit. A tree, a tag, a blob or something that stopped resolving.
- `the trunk <sha> the fold proved against is gone from <dir>` — the recorded
  commit is no longer readable, so nothing can be asked about it.
- `<new> no longer descends from the <old> the fold proved against in <dir>` —
  a force-move, rewrite or rollback. Trunk is different rather than further on,
  and the work the fold proved may no longer be there.
- `could not ask whether <new> descends from <old> in <dir>` — the probe itself
  failed. Not an answer, and deliberately not spelled like one.

The third is the one to read carefully: it says trunk was rewritten under work
that had been proven against it.

## How to read a stale reason

Match the sentence to the section above and it tells you which fact moved. Two
that surprise people:

- **`written by other code` after a docs-only change.** The key hashes `.py`
  files under the package only. Docs, tests and web assets do not move it — if
  you see this, some `.py` did change.
- **A refusal naming a probe that could not run** is never a finding that
  anything is wrong with the ledger. It says a question could not be asked. The
  read falls back to a full fold, which is correct and merely slower.

A stale checkpoint is never a correctness problem. The worst it costs is the
two minutes, and the design prefers that to serving an answer it cannot prove.
