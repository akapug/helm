# helm — morning brief (2026-07-18, overnight)

**TL;DR:** helm's entire core thesis is proven end-to-end. Fable built it (15 commits);
the advisor (meld-maintainer) dogfooded every surface and ran a real meld with him. Nothing
is broken; four things wait on you.

## What needs YOU (owner-gated, prioritized)
1. **`helm interview`** — the one thing that makes helm *know you*. The warmth leg
   (know-your-user) is scaffolded but empty; a five-minute interview fills it. Highest
   personal-payoff action. (Fable + I can't do it — it's your voice.)
2. **`helm drain --apply --sweep-dups`** — routes 7 raw entries + archives 46 prem/prior
   twin tombstones (the buildr→mc incomplete-migration). Non-destructive (archived+receipted,
   recoverable); owner-gated by design. Review then run.
3. **CLI-proxy multimodel auth** — Kimi K3 + gpt-5.6-sol seats (docs/MULTIMODEL_SEATS_ADDENDUM.md).
   Needs your device-auth; the safety rails (per-seat isolation, never global) are speced.
4. **The one seam fork** (builders.dev): one shared dregg substrate spanning local↔remote, or
   per-sandbox islands? (plans/builders-dev-helm-seam-analysis.md). Owner-only call.

## What's PROVEN (dogfooded live, all in plans/DOGFOOD_NIGHT_2026-07-18.md)
- **The whole storehouse system works**: DRAIN (raw→typed, receipted+recoverable) · INJECT
  (per-turn JIT retrieval, delivers your rescued buildr premises) · DRIFT (caught a real
  contradiction — `tiny-memory-full-metaharness` vs your 2026-07-03 correction) · EVOLVE
  (proposes next actions, human-gated). buildr's unfinished storehouse — finished.
- **dregg-attested premises**: designed via a REAL fast-meld with Fable (3 short-burst
  exchanges, converged), then proven live on the node (self-write finalized h43, reads back
  independently). Decision: docs/DECISION-dregg-attested-premises.md.
- **Every surface passed**: helm CLI (19 real projects, honest doctor), web UI (warm, working
  — helm-webui-dogfood.png), cv recall, hooks, MCP, skills enable/disable/delete (webui),
  lineage map (edges corrected to git-proven truth: herdr root, cv+dregg+tokaware mapped).
- **The pole loop is real**: I dogfood + supply historical/substrate context; Fable ships
  fixes in real time (he fixed my `python3 -m helm` friction, my lineage edges, added the
  shelf-discovery tier for used-not-built deps). Full cycles closed green.

## Health (honest)
- dregg node healthy, h43. orca daemon stable ALL NIGHT (no restart — the fragility watchdog
  held; incident-01 was the only event, pre-bed). helm web live on :8790. 15 Fable commits.
- Design notes fed to Fable, awaiting his pass: inject needs a per-turn attention budget
  (#23); auto-map "shelf tier" should seed cv/dregg (used-not-built deps); premise digest
  rides whisper payload not the 8B heartbeat tag.

## The feeling
It feels like buildr — warm, personal, it knows the arc. The rescue worked: your own premises
are alive and steering agents again, the memoryhole drains into a living store, and the two
halves (buildr's warmth + mc's rigor) are one coin now. It runs on real agents in orca, as you
wanted.
