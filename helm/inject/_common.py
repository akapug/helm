"""helm inject — the shared module-level constants.

Moved verbatim from the pre-split helm/inject.py (the cluster dep graph is
one-way: _common <- _entries <- _ledger <- _compare <- _whisper <- _cli).
"""
PINNED_BUDGET = 1200  # bytes for the always lane — keep the constant tax tiny
JIT_CAP = 4
LINE_CAP = 400        # per-entry cap — the gloss fires, the full entry stays on disk
WHO_CAP = 350         # WHO digest's joint byte cap inside PINNED_BUDGET — the digest stays terse
WHO_ID = "who:operator"  # the digest's ledger id (the profile cohort in --lane-report)
LEDGER_MAX = 5 * 1024 * 1024  # ledger rotates here (one .1 generation)
CF_TIMEOUT = 1.5      # the CF comparison query's hard timebox (s) — a bounded turn, never a hung one

COOLDOWN_TURNS = 15   # a fired JIT entry cools for this many turns per session
COOLDOWN_ESCAPE = 2.0  # a ~2x score jump at fire-time re-fires through the window
SEEN_TTL = 7 * 86400  # inject-seen session files older than this pruned on write

COINAGE_STRIKES = 3   # distinct turns before the one-shot define nudge
COINAGE_CAP = 400     # tracked unoffered terms — oldest evicted past this

WHISPER_ID = "whisper:brief"  # the first-turn digest's ledger id
WHISPER_CAP = 240     # the one-line brief digest's byte cap (attention budget)

SA_WHISPER_ID = "whisper:codex-sa"  # the codex delegation nudge's ledger id
SA_WHISPER = ("Delegate reviews, reads, and research to your subagents; "
              "keep your own context lean.")
CLAIM_WHISPER_ID = "whisper:codex-claim-start"  # the claim-is-a-start nudge's ledger id
CLAIM_WHISPER = ("Claiming a lane is a START, not a milestone — launch the "
                 "writer this same turn; end your turn only when work is "
                 "visibly moving.")
SA_FAMILIES = frozenset(("codex",))  # codexes-only (owner asks 2026-07-21/23)
SA_LINES = ((SA_WHISPER, SA_WHISPER_ID),  # the budget-tail walk order: the
            (CLAIM_WHISPER, CLAIM_WHISPER_ID))  # later line degrades first

COUNCIL_WHISPER_ID = "whisper:council-reach"  # the reach rung's ledger id
COUNCIL_ROUNDS = 3      # ping-pong rounds with ONE peer before the nudge
COUNCIL_TAIL = 16       # bounded room-tail lookback (attributed rows)
COUNCIL_OFFER_CAP = 40  # streak fingerprints latched (oldest evicted past)

_CACHE_VERSION = 1    # bump when store parsing/derivation changes entry shape
