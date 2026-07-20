# Eval — web ledger tab (2026-07-20)

Card: the meld-viewer port (owner-confirmed gap: the :8900 turn-ledger viewer
was a SEPARATE process, never part of helm web). Closes it as a sixth web tab
over the node helm cell already talks to.

## What shipped

- `web.py`: two GET endpoints, read-only by construction —
  - `/api/ledger` — one aggregate over the node's public reads (`/status`,
    `/api/receipts`, `/api/cells` via `cell.get_json`, HELM_NODE_URL law):
    status subset (declared key list, never passthrough), newest 40 signed
    turns, cells with `last_turn_ts`/`recent_turns` joined off the bounded
    receipt window (receipt.agent ↔ cell.id).
  - `/api/ledger/turn?hash=` — the durable finality certificate for one turn,
    64-hex gate on the hash so the proxied path stays literal-only.
- `web_ui.html`: the **ledger** tab — node status strip (ingress vs finalized,
  producer, federation), turn ledger with finality badges (the consensus
  certificate `final @ h<N>` wins; the receipt's hash-bound `finality` field is
  only the older-node fallback), seat activity (active <2m / quiet <30m /
  stuck? / idle), and the message-a-seat form. Polls ~3s only while open.
- The ONE write rides the EXISTING `/api/chat` POST (`@seat …`, bearer token,
  owner-unread marker) — zero new mutation surface; the ledger endpoints have
  no POST route at all (a bearer-carrying POST is 404).

## Laws honored

- **Fail-open**: node down/absent → `{"offline": true}` at 200; the tab shows
  a quiet "substrate offline" strip and retries — never an error page.
  Unknown hash and dead node share one degrade shape on the turn proxy.
- **Read-only proxy**: loopback-only server, same-origin guard, GETs open;
  the node's status object is projected through a declared key list.
- **One write rail**: owner→seat messages go through chat (signed server-side
  when the room node answers), not a second whisper-shaped web write.

## Verification

- `tests/test_web_ledger.py` — 9 hermetic tests: aggregate shape + newest-first
  order, status-subset projection (a planted `secret_seed` must not project),
  activity join (acting vs never-acted cell, active-first sort), turn proxy
  roundtrip, hash gate (empty/short/non-hex/traversal → 400), unknown-hash
  degrade, read-only 404s, and an offline class against a nothing-listens port.
- Suite: 836 tests, `-W error::ResourceWarning`, OK.
- Live smoke against the running node (:8899): 13 turns, head #12
  `consensus_final @ h343`, 42 cells joined; turn-status certificate verified
  for the head hash. Browser-rendered: `helm-ledger-live.png`.

## Measurement (owner-surface)

The gap this closes was owner-visible (screenshot of the :8900 viewer). The
heed signal: the owner reads finality/seat state from :7433/#ledger and the
:8900 process gets retired (consolidation note:
`~/.helm/helm/prd/2026-07-19-web-ui-consolidation.md`). If :8900 stays alive a
month from now, the port missed something — find it and fold it in.
