"""helm inject — the shared module-level constants.

Moved verbatim from the pre-split helm/inject.py (the cluster dep graph is
one-way: _common <- _entries <- _ledger <- _compare <- _whisper <- _cli).
"""
PINNED_BUDGET = 1540  # bytes for the always lane, RULES ONLY (WHO is charged
                      # to WHO_CAP and walks after them). Derived, not chosen:
                      # the eight ratified owner rules RENDER at 1339 bytes
                      # (gloss + "PREMISE <id>: " prefix, 33-43B of overhead
                      # per line), measured 2026-08-28 by the loud-drop alarm
                      # itself, +15% headroom. 1200 silently dropped four of
                      # them, then one, while two separate authors' arithmetic
                      # said they fit — the number now comes from the
                      # instrument, never from a sum anyone did by hand.
JIT_CAP = 4
LINE_CAP = 400        # per-entry cap — the gloss fires, the full entry stays on disk
JIT_LINE_CAP = 200    # the JIT lane's per-entry cap FOR AN UNGLOSS'D ENTRY,
                      # tighter than LINE_CAP because such a line is a
                      # REMINDER cut out of a record the store still holds —
                      # the lane's FOOTER names `helm store get`, so nothing is
                      # unreachable. A GLOSS still fires whole to LINE_CAP: it
                      # is the author's own firing line, and cutting it would
                      # retire the one lever a writer has over what a seat
                      # receives. Measured over the live store, 1,110 of 1,862
                      # entries render past this cap and 17 carry a gloss, so
                      # the reduction lands almost entirely on the entries
                      # nobody has shortened — which is where it belongs.
FIRST_SENTENCE_MIN = 24  # a candidate first sentence shorter than this is an
                      # abbreviation ("e.g.", "vs.") or a bare id ("task/1563.")
                      # rather than a sentence, so the walk continues past it.
                      # Real terse canon is longer than this: "A gate is not an
                      # approval." is 25. Also the floor under which a cut line
                      # takes a plain cut to its full cap (see _entries._cut).
FOOTER = "[helm] full text: helm store get <type>:<id>"
                      # THE ROUTE TO THE REST, ONCE PER LANE (task/2980). A
                      # route on every cut line, ` (helm store get <type>:<id>)`,
                      # cost about 64 B on 63% of store lines, 22% of their
                      # bytes, and repeated the id the line's own prefix names
                      # (MEASURED, injection-quality eval e4). A lane that
                      # carries any line shorter than its entry (a cut, a
                      # gloss, a first sentence) ends in this one line instead.
                      # <type> reads the line's own tag (PREMISE, MOVE, TERM,
                      # REF all resolve; store.find_typed), <id> its own id.
FOOTER_GATES = "; their gates: "
                      # the footer's second clause: the gates of the lane's
                      # KEYWORD lines, named rather than rendered. A rider
                      # rides only on a deterministic line (the pinned lane, a
                      # notice route); on a keyword line it tripled one block to
                      # 1,648 B (MEASURED, trigger design D8).
REPEAT_WINDOW_TURNS = 8  # the window a rendered line's identity is remembered
                      # for. Past it the same sentence is worth saying again.
REPEAT_ALLOWANCE = 2  # how many times one IDENTICAL rendered line may be shown
                      # inside that window before it is withheld. NOT 1, and
                      # the reason is a live contract: a reflex fires on a
                      # signal that is live THIS turn, so the lane is exempt
                      # from the pinned lane's once-per-session suppression
                      # (owner-asked). Saying a live steer twice honours that;
                      # saying it on turn after turn is the wallpaper the
                      # owner complained about. The allowance is where those
                      # two meet, and it is a number rather than a branch so
                      # the trade is visible.
JIT_BUDGET = 1200     # the JIT lane's BASE allowance per turn. JIT_CAP bounds
                      # the COUNT of entries and NOTHING bounded their bytes,
                      # so the lane's size was whatever four entries happened
                      # to be — and selection does not weigh length, so the
                      # entries that fire are the long ungloss'd ones. An
                      # entry past this drops LOUDLY (jit_alarm), never in
                      # silence. It is deliberately above 4 x JIT_LINE_CAP:
                      # four FULLY GLOSSED entries are authored content and
                      # must not be dropped for being what the gloss campaign
                      # is asking authors to write.
GATE_BUDGET = 2 * LINE_CAP  # the JIT lane's RIDER allowance per turn (task/1346): the
                      # bytes a fired rule's gates may add beyond the cap-4 lines,
                      # two glossed gates' worth; a rider past it drops LOUDLY.
                      # MEASURED 2026-08-22: under the cap's own ceiling (JIT_CAP x
                      # LINE_CAP) the live specimen could not carry its second gate
                      # even fully glossed — 1408 B of base lines + a 443 B rider —
                      # because two unrelated single-word hits held half the lane.
                      # A gate is the rule's precondition, not a fifth entry
                      # competing on vocabulary; it rides on its own allowance.
JIT_LANE_MAX = JIT_BUDGET + GATE_BUDGET  # the whole JIT lane's per-turn
                      # ceiling — bases plus riders. The two allowances stay
                      # separate (a gate is its rule's precondition, not a
                      # fifth entry competing for the same bytes); this is the
                      # number a budget test asserts against. It is a CEILING
                      # reachable only by fully glossed content: the measured
                      # live lane renders at 719.
STEER_CAP = 280       # per-reflex-steer cap at the inject — pack norm is 101-137B,
                      # and a 943B wall reached every turn verbatim (#reflex-steers
                      # -are-uncapped; reflex.py:44 "steers are terse FACTS")
WHO_CAP = 350         # WHO digest's joint byte cap — its WHOLE budget, NOT
                      # a slice of PINNED_BUDGET. The digest is charged here
                      # alone and walks AFTER the rules (2026-08-28 ruling).
WHO_ID = "who:operator"  # the digest's ledger id (the profile cohort in --lane-report)
LEDGER_MAX = 5 * 1024 * 1024  # ledger rotates here (one .1 generation)
CF_TIMEOUT = 1.5      # the CF comparison query's hard timebox (s) — a bounded turn, never a hung one

# NO TURN WINDOW. A fired JIT entry stays suppressed for the LIFE of the
# session, because turns are not when a seat forgets — a context boundary is
# (compaction or /clear), and _ledger.forget_session clears this state there. The old
# COOLDOWN_TURNS=15 window re-sent every entry about every 16th turn forever:
# measured on the fire-ledger, 94.2% of JIT re-deliveries (6,949 of 7,375) were
# the window merely expiring, not the escape firing — 66.9% of the whole JIT
# lane, ~622k tokens of content the seats already had in context.
COOLDOWN_ESCAPE = 2.0  # a ~2x score jump at fire-time re-fires through suppression
SEEN_TTL = 7 * 86400  # inject-seen session files older than this pruned on write

COINAGE_STRIKES = 3   # distinct turns before the one-shot define nudge
COINAGE_CAP = 400     # tracked unoffered terms — oldest evicted past this

WHISPER_ID = "whisper:brief"  # the first-turn digest's ledger id
WHISPER_CAP = 240     # the one-line brief digest's byte cap (attention budget)
PINNED_DROP_NAMES = 6  # ids named in the dropped-pinned alarm; the COUNT is
                       # always exact, only the LIST is capped — an alarm that
                       # grows with the fault teaches readers to skip it

SA_WHISPER_ID = "whisper:codex-sa"  # the codex delegation nudge's ledger id
SA_WHISPER = ("Delegate reviews, reads, and research to your subagents; "
              "keep your own context lean.")
CLAIM_WHISPER_ID = "whisper:codex-claim-start"  # the claim-is-a-start nudge's ledger id
CLAIM_WHISPER = ("Claiming a lane is a START, not a milestone — launch the "
                 "writer this same turn; end your turn only when work is "
                 "visibly moving.")
SA_FAMILIES = frozenset(("codex",))  # codexes-only (owner asks 2026-07-21/23)
# Budget-tail walk and content identity: the later line degrades first; each
# line that fits records its own identity only after delivery.
SA_LINES = ((SA_WHISPER, SA_WHISPER_ID),
            (CLAIM_WHISPER, CLAIM_WHISPER_ID))

COUNCIL_WHISPER_ID = "whisper:council-reach"  # the reach rung's ledger id
COUNCIL_ROUNDS = 3      # ping-pong rounds with ONE peer before the nudge
COUNCIL_TAIL = 16       # bounded room-tail lookback (attributed rows)
COUNCIL_OFFER_CAP = 40  # streak fingerprints latched (oldest evicted past)

_CACHE_VERSION = 5    # bump when store parsing/derivation changes entry shape
#            ^ 5: a UUID's hex group is no longer read as a row id, so the
#               scope derivation changed with no store mutation (task/2547)
#            ^ 3: entries carry "project" and the lane is scope-fenced (task/2435)
                      # (2: gates/gate_probes — load.link_gates, task/1346)
