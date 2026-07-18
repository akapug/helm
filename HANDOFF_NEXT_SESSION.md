# helm — continuation handoff (for the next Fable session)

The founding session (2026-07-18 overnight) built the core and hit ~424k context — a clean
stopping point. This is what a FRESH session picks up. The advisor (meld-maintainer in
/home/owner/dev/akapug/meld) holds the history/substrate context — write questions to
`ADVISOR_QUESTIONS.md`.

## What's DONE (validated live by the advisor's dogfood — see plans/DOGFOOD_NIGHT_2026-07-18.md)
Storehouse two-loop (drain+inject), drift, evolve, lineage (git-proven edges), CLI (18 verbs),
web UI (skills enable/disable/delete), auto-map (19 real projects + shelf-discovery tier),
sesh homes+configs absorbed. All dogfood findings fixed (python3-m-helm, cv-index, lineage,
store-resolve). Feels like buildr. 15 commits.

## BUILDABLE next (not owner-gated — a fresh session can build)
1. **Premise-capture wiring** — DESIGNED, ready: `docs/DECISION-dregg-attested-premises.md`.
   Build a `helm premise <statement>` verb that: stores the premise (type=premise, conf 1.0)
   AND commits a self-write dregg turn signed by the USER's cell writing the BLAKE3 digest
   into the WHISPER PAYLOAD slots (NOT the 8B heartbeat tag — advisor proved this live).
   Supersede = new turn ref prior hash (store's supersedes/replaced_by fields already exist,
   1:1). The `meld` binary (target/release/meld) exercises the self-write path; node :8899 live.
2. **Inject attention-budget** (advisor note, decision-spirit #23): cap `helm inject` per-turn
   (always-premises + top-N jit, truncated) so the hook doesn't blow context budget.
3. **Seed cv+dregg as truth-engine nodes** in the lineage/registry (the shelf tier exists;
   they're used-not-built deps with zero agent-sessions, so seed them explicitly).

## OWNER-GATED (wait for David — see MORNING_BRIEF_2026-07-18.md)
`helm interview` (warmth leg), `helm drain --apply --sweep-dups` (46 tombstones), CLI-proxy
multimodel auth (Kimi K3 + gpt-5.6-sol), the builders.dev seam fork (shared substrate vs islands).

## Comms
Advisor = meld-maintainer session in ~/dev/akapug/meld (reliable via ADVISOR_QUESTIONS.md file;
it polls). Daemon watchdog is armed (auto-recovers if the orca daemon restarts). Reliable-comms
rules: files-then-wake, re-list to confirm from target side.
