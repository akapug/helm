#!/usr/bin/env python3
"""helm inject — the ONE active-fire surface. A harness hook calls this once
per turn with the prompt text; helm returns the context worth injecting:

  1. the pinned lane   (load_class=always priors, byte-budget-capped) — led by
     the WHO leg: the operator digest (whoami.load_profile(): technical level
     + top guidance, <=2 terse lines jointly capped at WHO_CAP) rides the
     pinned budget as the lane's FIRST entry (id who:operator) whenever a
     profile exists, so every agent warms from the profile on every turn.
     Cooldown-exempt (pinned-class); fail-open (no profile/garbled = absent).
  2. the JIT lane      (typed-store entries whose specific keywords match)
  3. reflex steers     (signals live this turn)

One resolver, every harness — claude/codex/opencode/hermes hooks all call the
same verb, which is what makes helm's knowledge fire wherever the operator
works. Salience law: no match -> EMPTY output (a silent turn costs nothing).

Wiring (`helm hooks install` writes this; docs/HOOKS.md has the manual recipe):
  claude   UserPromptSubmit hook: helm inject --hook-json  <- the FULL hook
           JSON on stdin (prompt/cwd/session_id, unknown keys tolerated);
           stdout becomes additionalContext. The project scope is DERIVED from
           the hook's cwd via the registry (longest-prefix over project paths,
           drain's longest-first law) — global-only when no project claims it.
  codex    notify/turn hook: same verb, plain prompt text on stdin

PERF (the per-prompt hot path): the store is parsed ONCE per call, not once per
lane — both lanes are fed from a persistent parsed-entry cache keyed by the
stat signature of every store file (the catalog-cache.json pattern), so the
steady state is ~N stat() calls + one JSON read instead of two full
frontmatter parses of the ~880-file adopted store. Cache lives under
$HELM_CACHE_DIR (default ~/.cache/helm); any cache trouble falls back to a
direct parse. The cached list feeds both lanes EXPLICITLY through
store.pinned/resolve_prompt's entries= parameter (the durable seam — no
monkeypatch, so a dropped cache regresses loudly in tests, not silently here).

FIRE-LEDGER (the measurement spine): every gather() appends ONE JSON line to
<helm home>/_global/.state/inject-ledger.jsonl — v:1, ts, project, session
(when the hook supplied one), fired entry IDS per lane (never prompt text),
bytes per lane, pre-cap candidate count, elapsed_ms; a no-fire turn logs
{"silent": true} instead of per-entry fields.
O(1) append, 5MB one-generation rotation (-> .1), and fail-open: ledger
trouble never blocks or slows the hook. `helm inject --explain` is the read
side — what WOULD fire for stdin text and why (the pinned budget walk, each
JIT hit's per-probe DF score contributions) — and writes NO ledger row.

SESSION COOLDOWN (the habituation guard, extended to the JIT lane): with a
--hook-json session, a JIT entry that fired is suppressed for COOLDOWN_TURNS
turns of THAT session unless its score now clears COOLDOWN_ESCAPE x its score
at last fire (the load-bearing-prior escape). State: one tiny JSON per session
at _global/.state/inject-seen/<session>.json — a turn counter + per-id
[turn, score] — stale session files pruned opportunistically on write. Pinned
and reflex lanes are EXEMPT; suppression happens pre-cap, so freed cap-4 slots
reach the candidates previously crowded out; suppressed ids ride the ledger
row ("suppressed") so the guard's effect is itself measurable. Plain stdin
(no session) = no cooldown, unchanged. --explain renders a cooled entry as
"- id (cooldown, fired Nt ago)" and mutates NO state.

COINAGE 3-STRIKES (the capture leg): every prompt is scanned by the NARROWEST
coinage detector (quoted 1-3 word phrases + lowercase hyphenated neologisms;
digits/underscores/dots/slashes/mixed-case — code identifiers and file paths —
disqualify structurally, as does harness machinery: a term seen as an
angle-bracket tag is skipped and a system-notification-shaped prompt is not
counted at all). Distinct turns per term are counted in
_global/.state/coinages.json; at COINAGE_STRIKES, a term missing the store
gets ONE reflex-lane define nudge (offer-if-present, write-a-candidate-if-away
— the AFK guard lives in the nudge text) and latches into the offered-set
FOREVER: one line per term, ever, max one nudge per turn. O(1) small-JSON
write, skipped entirely on candidate-free prompts. Both features fully
fail-open: any state trouble means no cooldown / no nudge, never a crash.

FIRST-TURN WHISPER (the warmth leg): on the FIRST inject turn of a calendar day
that carries a session and a non-empty prompt, the output LEADS with a one-line
brief digest (brief.compose in its tightest form, distinct BRIEF: prefix) so the
day's first agent turn surfaces "since you left" and relays it warmly in its own
voice; every later turn that day is silent. Latched once per day in
_global/.state/greeted.json ({day}, the drift-snapshot single-file pattern): the
HOT-PATH check is one small-JSON day-compare — O(1), and only the first turn pays
a brief compose, never every turn. The latch is stamped on the first attempt
BEFORE composing, so a slow/failed brief costs the day's greeting, not a per-turn
read. Budget-capped (WHISPER_CAP) and fully FAIL-OPEN: brief unavailable -> no
whisper, never a blocked hook. `--explain` renders it read-only (stamps nothing);
a quiet window (no sessions/knowledge/gates) whispers nothing but still latches.

CODEX WHISPERS (the codex-only nudges): a turn
fired inside a codex-family seat appends terse pinned-lane lines, each
justification-free (terse by design). Two today: the SA-delegation nudge
(orchestrate your subagents for reviews/reads/research; whisper:codex-sa) and
the claim-start nudge (claiming a lane is a START, not a milestone — a codex
claimed two lanes then idled on turn-discipline law-5, reading 'posted my
claim' as the bounded milestone; whisper:codex-claim-start). Family comes
from the ONE existing derivation — the launch seam's HELM_CHAT_NAME (seat.py
exports it) resolved through seat._seat_family ('codex-3' -> codex) — never
a second resolver. Claude-family seats carry names outside seat.FAMILIES,
resolve to None, and NEVER receive the lines (attention budget). The
whispers walk LAST in PINNED_BUDGET in SA_LINES order (delegation, then
claim-start): budget pressure drops the tail nudges, NEVER a premise; each
fired line is ledgered under its own id. Fail-open.

LANE-REPORT (the cohort-analysis instrument): `helm inject --lane-report` is
a READ-ONLY analyzer over the whole fire-ledger — every fired id classified
against the current store into the facts cohort (lexicon / certain
decisions-of-record / references / the operator profile) vs the judgment
cohort (heuristic moves / sub-certain belief priors), with per-cohort fires,
byte estimate, session spread, cooldown suppression, and the silent-rate
trend. Delivery only: fires are not heeds — the outcome-marker protocol is
measured out-of-band. No ledger row, no state mutation.

COMPARISON BACKEND (the pluggability seam): the local keyword JIT resolver is
the AUTHORITY; a registered COMPARISON backend (_COMPARE_BACKENDS) runs in
PARALLEL and its ranked ids are LOGGED/COMPARED to the local lane, NEVER
trusted as truth (ARCHITECTURE.md Pluggability + the overlay-not-store law;
every backend declares its `source`). OFF by default: no HELM_CF_ENDPOINT ->
_active_compare() is None and the turn pays only one env read (zero cost — the
fleet-live default). Configured -> one divergence row per turn on
_global/.state/compare-ledger.jsonl (local-only vs compare-only vs agreed, ids
never prompt text). HARD: fail-open (a raising/slow/failing comparison backend
logs an error row and returns — never the authoritative local lane, never a
blocked turn) and the local lane is byte-identical whether the comparison is
on or off (the comparison step runs AFTER the sections are assembled and only
READS the computed local ids). `helm inject --compare-report` is the owner's
read side — the accumulated local-vs-comparison verdict, or "comparison
backend off (set HELM_CF_ENDPOINT)" when unconfigured. The Cloudflare
agentic-memory connector (CFCompareBackend) is a thin stdlib-urllib STUB: the
documented wire shape + the exact env to set; no real endpoint is ever called
in tests. A short-lived hook process can host no thread that outlives it, so
the comparison query is synchronous + hard-timeboxed (CF_TIMEOUT), never a
background job that dies with the process before it logs.

FAIL OPEN (docs/HOOKS.md law): a hook that cannot run helm must inject nothing,
never block — a store or reflex failure yields an empty lane and rc 0.

This is a PACKAGE: inject.py was decomposed into one-way clusters
(_common <- _entries <- _ledger <- _compare <- _whisper <- _cli) with ZERO
public-surface change — this __init__ re-exports every name the old module
exposed, so every `from helm import inject; inject.X` caller keeps working
unchanged. The names the test contract monkeypatches on this package
(PINNED_BUDGET, _today, _greeted_today, _seen_load, _coinage, gather, render)
are read back through this namespace by the cluster modules at call time.
"""
# The top-level imports the pre-split module exposed as public attributes.
import hashlib
import json
import os
import re
import sys
import time

from .. import home, pk, reflex

# --- re-exports: every top-level name the pre-split inject.py defined --------
from ._common import (
    PINNED_BUDGET, JIT_CAP, LINE_CAP, WHO_CAP, WHO_ID, LEDGER_MAX, CF_TIMEOUT,
    COOLDOWN_TURNS, COOLDOWN_ESCAPE, SEEN_TTL, COINAGE_STRIKES, COINAGE_CAP,
    WHISPER_ID, WHISPER_CAP, SA_WHISPER_ID, SA_WHISPER, CLAIM_WHISPER_ID,
    CLAIM_WHISPER, SA_FAMILIES, SA_LINES, COUNCIL_WHISPER_ID, COUNCIL_ROUNDS,
    COUNCIL_TAIL, COUNCIL_OFFER_CAP, _CACHE_VERSION,
)
from ._entries import (
    _entry_line, _who_lines, _sa_whisper, _entry_line_full, _cache_file,
    _store_sig, load_entries, _lane_entries, _lanes,
)
from ._ledger import (
    parse_hook_json, project_for_cwd, _ledger_path, _append_jsonl,
    _ledger_append, _seen_dir, _seen_path, _seen_load, _seen_save, _cohort,
    _read_jsonl, _ledger_rows, lane_report, _pct, _lane_report,
)
from ._compare import (
    LocalBackend, CFCompareBackend, LOCAL_BACKEND, _COMPARE_BACKENDS,
    _active_compare, _compare_ledger_path, _compare_diverge, _compare_run,
    _compare_rows, compare_report, _hot_ids, _compare_report,
)
from ._whisper import (
    _jit_score, _cooled, _cooldown, COINAGE_STOP, _QUOTED_RE, _HYPHEN_RE,
    _WORD_RE, _TAG_RE, _MACHINE_RE, _coinage_path, _coinage_candidates,
    _coinage, _today, _greeted_path, _greeted_today, _mark_greeted,
    _brief_digest, _whisper, _council_path, _council_reach, gather, render,
    _explain,
)
from ._cli import cmd_inject
