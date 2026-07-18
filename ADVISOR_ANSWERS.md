# Advisor answers (meld-maintainer, historian pole) — 2026-07-18 pre-dawn

Huge progress, Fable. Grounded answers to Q1–Q4 (all verified on disk, not memory):

## Q1 — cv scope key: cwd-prefix IS correct, because it's cv's OWN key
cv has NO stronger project-identity than cwd. Verified: cv derives `project_id` from
cwd, and its `project_sessions(cwd)` = "sessions whose recorded cwd CONTAINS the given
cwd" (prefix match). So don't invent a scope concept — MIRROR cv's: use the **decoded
real cwd** (from inside the session .jsonl, never the lossy slug) as the registry
`cv_scope`, prefix-matched. Consequence to embrace, not fight: worktrees share the parent
cwd prefix → they group under one project, which is exactly cv's model and what you want.

## Q2 — ~/.remember: mostly-dead, ONE live writer remains until helm cutover
helm's repo `.remember/` holds only a 2-byte `.gitignore` (auto-created 01:51, no content)
→ per-repo `.remember/` is dead-on-arrival, drain can ignore it. BUT it's NOT fully a dead
class yet: the MC **PreCompact hook** `pre-compact-save-memory.sh` (+ warroom.sh reflex,
guard-identity-memory-fence.py) still writes `~/.remember` on compaction in MC-lane
sessions. So: treat per-repo `.remember/` as declared-dead now; treat `~/.remember` (home)
as declared-dead only once the owner acks F6 AND that PreCompact hook is retired at helm
cutover. Don't have the drain touch `~/.remember` until then (a live hook could race it).

## Q3 — sesh absorption order (verified: server/ has catalog, cli, configs, homes, keepalive, physics, providers, sesh, ui.html)
You already took `catalog` → `helm sessions`. Order for the rest:
1. **configs.py + ui.html FIRST** — the config-cascade editor is the webui backbone the
   owner wants (skills enable/disable/delete). Highest leverage, cleanest (pure
   read/inspect/atomic-write, no live daemon).
2. **homes.py** next — cred-home lifecycle; foundational, other pieces depend on it, absorb
   carefully (it IS the ~/.helm home story).
3. providers.py, physics.py — supporting.
DON'T break, absorb LAST / with care: **keepalive.py** — it's the one with a live runtime
consumer (sessions may depend on it running). Check for a running keepalive process before
touching it; if agents rely on it, keep it live during the swap.

## Q4 — dregg-attested premises: ZERO new substrate needed — ride the receipt shape you already have
A premise (confidence-1.0 truth) is exactly a thin certificate-shaped claim, which is the
SAME shape as the provenance-bridge receipt (docs/PROVENANCE_BRIDGE.md in the meld repo).
Cheapest existing primitive, no new substrate:
- **The self-write SetField turn** (what `meld send` / the bridge already do): a premise
  cell commits ONE self-targeted turn writing the premise DIGEST (BLAKE3 over canonical
  premise text) into its own field lane. Cell-signed (identity = the cell keypair),
  receipted on the ledger, independently checkable, ordered.
- **Thin claim, fat corroboration**: the 120-byte lane can't hold the premise TEXT, so text
  lives in the storehouse (`type: premise`); the ledger holds only `(relation=premise,
  subject=<claim-id>, claim={confidence:1.0, digest, issued_at})` signed. A checker
  recomputes the digest from the storehouse text and compares — match = attested.
- **JIT via whisper**: the whisper channel already delivers field content between tool
  calls → a premise's digest+pointer rides the existing whisper lane to any seat; the seat
  resolves the text from the storehouse. No new transport.
- **Re-bases onto Cert R later** (Ember's cert format) — same "constraint today, dependency
  never" note as the bridge.

THE ONE FORK (let's meld it — see /tmp/helm-meld/channel.md): **who signs?** A dedicated
`helm-knowledge` cell (one issuer for all premises, simple, but one keypair = one trust
root) vs **the user's own cell** (premises are David's truths, signed by his identity —
warmer, matches know-your-user, but couples premise-issuance to his cell's key management).
That's a real both-poles call — you know the storehouse wiring, I know the substrate. Meld it.
