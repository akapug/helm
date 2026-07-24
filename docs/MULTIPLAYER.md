# local multiplayer

Helm owns **local human + agent multiplayer inside one cave**. builders.dev owns
the remote/web product. The boundary between them is an adapter contract, not a
shared implementation, so neither side needs to know whether the local operator
uses Orca, herdr, a bare shell, a browser, or another metaharness.

## first-slice contract

The slice deliberately separates two channels:

1. **Blind durable-state relay.** A CRDT client publishes an opaque update
   string. Helm envelopes and appends it, but never decodes, interprets, merges,
   validates as a particular CRDT, or rewrites it. Clients read from an opaque
   byte cursor and own materialization, conflict resolution, snapshots, and
   durable history.
2. **Ephemeral presence.** A client heartbeats
   `{actor, connection, state, seen, expires}`
   into a separate TTL snapshot. Presence may disappear at any moment. It never
   enters or mutates the update log, so reconnecting or expiring cannot corrupt
   shared state.

This is the useful pair from Mosaic: blind relay + opaque CRDT at the state
boundary; a decoupled, disposable presence channel at the attention boundary.

```text
CRDT adapter A ── opaque update ──┐
                                 ├─ RelayAdapter ─ append-only cave/doc log
CRDT adapter B ── opaque update ──┘

human/agent ── heartbeat ─────────── PresenceAdapter ─ independent TTL snapshot
```

The shipped adapters are `LocalRelay` and `LocalPresence` in
`helm/multiplayer.py`. A remote bridge implements the same methods:

```python
relay.publish(cave, doc, actor, update) -> acknowledgement
relay.updates(cave, doc, cursor=0) -> {updates, cursor}
presence.heartbeat(cave, actor, state="active", ttl=30, connection=id) -> peer
presence.peers(cave) -> [peer, ...]
presence.leave(cave, actor, connection=id) -> bool
```

No method mentions a pane, terminal, model, browser, socket, or CRDT library.
That is the metaharness-agnostic and CRDT-agnostic seam.

## placement and failure semantics

The default root is `/dev/shm/helm-multiplayer` (override with
`HELM_MULTIPLAYER_DIR`). One cave contains one append-only `*.updates.jsonl`
file per document and one `presence.json` snapshot. Mode is 0700 for directories
and 0600 for files.

This is intentionally **ephemeral local coordination**, not another canonical
store under `~/.helm`:

- a restart may lose the cave; clients resupply state from their own CRDT;
- cursors bind a random document generation to a complete-record byte offset;
  stale generations and mid-record offsets fail loudly, and reads never advance
  over a crashed partial append;
- a document refuses new writes at 4 MiB rather than rotating and silently
  invalidating cursors;
- one update is capped at 256 KiB;
- presence TTL is 1–300 seconds (30 default), and expired peers prune on read;
- file writes are serialized with `flock`; updates preserve arrival order;
- update payloads remain byte-for-byte opaque UTF-8 strings.

The relay's only authority is transport order. It is not the document authority.

## try it (owner walkthrough)

For a hands-on, gui-first test drive — open the cockpit **cave** tab, publish
from a terminal, and watch two actors converge on a shared board with a live
presence panel — see **[MULTIPLAYER_TESTDRIVE.md](MULTIPLAYER_TESTDRIVE.md)**.
That walkthrough uses the built-in demo board: a last-writer-wins keyed map the
*client* materializes (`helm multiplayer set|status`, and the cockpit's cave
tab), while the relay stays blind.

## dogfood

Two local participants can share opaque updates without sharing a harness:

```console
$ helm multiplayer presence --cave helm --actor owner --connection phone --state editing
$ printf %s 'base64:opaque-crdt-update-a' | helm multiplayer publish board --stdin --cave helm --actor owner
$ printf %s 'base64:opaque-crdt-update-b' | helm multiplayer publish board --stdin --cave helm --actor codex
$ helm multiplayer read board --cave helm --json
$ helm multiplayer peers --cave helm --json
```

`HELM_MULTIPLAYER_CAVE`, `HELM_MULTIPLAYER_ACTOR`, and
`HELM_MULTIPLAYER_CONNECTION` set defaults. If absent, the cave follows
`HELM_CHAT_ROOM`, the actor follows `HELM_CHAT_NAME`/the normal seat identity,
and the connection follows the harness session id (then actor). Multiple tabs or
sessions under one actor therefore keep independent presence rows. Opaque input
should use `--stdin`; publish acknowledgements report only byte count, never echo
the payload. Human-readable reads show payload sizes only; CRDT consumers use
`--json`.

## next adapters, not core changes

- A local editor integrates its CRDT by encoding updates into opaque strings and
  consuming the returned cursor.
- builders.dev can bridge remote/web caves by implementing the relay/presence
  contract; Helm's local core does not grow hosted networking.
- Snapshot/compaction is a client or bridge policy. If added locally, it must be
  a new adapter capability with an explicit cursor epoch, never an in-place log
  rewrite.
