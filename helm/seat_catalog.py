"""Family/provider catalog and context-window invariants for :mod:`helm.seat`."""
import os
import re


# The env triple that must never reach a Claude Max-OAuth seat.
SCRUB_VARS = ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY")

# The child-session stamp that must never reach a LAUNCHED seat: a pane minted
# by a daemon that was itself started from inside a Claude session inherits
# these, and CC then treats the seat as a subprocess child — transcript
# persistence silently OFF, /branch broken, the session unrecoverable
# (bug-class child-stamp-kills-seat-persistence; live-verified 2026-07-21:
# every fleet seat carried the stamp + the daemon's inherited SID). Every mint
# (launch_line, launch.sh, smoke env) strips the trio so a seat is born a true
# top-level session.
CHILD_STAMP_VARS = ("CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_SESSION_ID",
                    "CLAUDE_CODE_BRIDGE_SESSION_ID")

# PLAN MODE IS ONLY FOR INTERACTING WITH A HUMAN (owner ruling 2026-08-03).
# Claude Code ships EnterPlanMode with a built-in standing instruction —
# "**Prefer using EnterPlanMode** for implementation tasks unless they're
# simple" — and ExitPlanMode's checkPermissions returns a hardcoded
# {behavior:"ask", message:"Exit plan mode?"} that --dangerously-skip-permissions
# does NOT bypass. A fleet seat is driven by dispatch rows from other agents
# with no human at its keyboard, so that approval prompt never gets answered:
# one seat sat frozen at it for over two hours holding all three open dispatch
# rows, and to every instrument helm has it looked exactly like an idle seat.
# A deny rule REMOVES the tool from the model's tool list (measured against
# CC 2.1.220: a denied tool disappears from the session-init `tools` array, and
# with it the description text that produced the behaviour) — so this is a
# suppression, not a refusal after the fact. Denied on BOTH seat surfaces:
# launch_line's --disallowedTools (the pane) and _seed_seat_settings'
# permissions.deny (the seat's whole CLAUDE_CONFIG_DIR, so a resume, an
# orca-relaunched pane, or an eval arm that copies the config all inherit it).
# ENTRY ONLY. ExitPlanMode stays available on purpose: if the OWNER puts a pane
# into plan mode by hand (shift-tab is a human at a keyboard — the one case the
# ruling blesses), the seat must still be able to get out.
PLAN_ENTRY_TOOL = "EnterPlanMode"

CODEX_HOMES = os.path.join(os.path.expanduser("~"), ".codex-homes")
# The hermes CLI's OAuth artifact — the mint SOURCE for hermes-keyed families
# (ds4pro). Read-only, never modified; tests point this at a fixture.
HERMES_AUTH = os.path.join(os.path.expanduser("~"), ".hermes", "auth.json")
# The opencode tool's auth store — the PREFERRED outbound-key source for pool
# families (ds4pro), owner-maintained and fresher than the hermes mirror. A
# JSON dict of provider -> {"type": "api"|"oauth", "key"/"access": <bearer>}.
# Read-only, never modified; tests point this at a fixture. Only type=="api"
# entries carry a static bearer we can bake.
OPENCODE_AUTHSTORE = os.path.join(os.path.expanduser("~"), ".local", "share",
                                  "opencode", "auth.json")
PROXY_BIN_DEFAULT = os.path.join(os.path.expanduser("~"), ".local", "bin", "cli-proxy-api")
DREGG_SIGNER_DEFAULT = os.path.join(os.path.expanduser("~"), ".local", "bin",
                                    "dregg-client-sign")

# Presets as data (the addendum's table). Three modes: "proxy" (OAuth cred
# translated into CLIProxyAPI, e.g. codex), "proxy-key" (an API-key provider
# behind the same proxy via its openai-compatibility block, e.g. kimi), and
# "first-party" (Anthropic-compatible endpoint, no proxy — future glm/deepseek:
# only mode+base_url+key_env needed). OAuth entries carry ``auth_type``: the
# canonical non-secret ``type`` written into their proxy auth records. Runtime
# family proof matches that measured provider metadata plus the live model; the
# family key and seat label never participate.
FAMILIES = {
    "codex": {"port": 8317, "model": "gpt-5.6-sol", "mode": "proxy",
              "auth_type": "codex",
              # CC hardcodes a 200k window for any non-`claude-` model and never
              # asks the proxy; gpt-5.6-sol's TOTAL window is 372k, so leaving CC
              # at 200k makes autocompact under-fire. CLAUDE_CODE_MAX_CONTEXT_TOKENS
              # (launch_line) teaches CC a real number instead.
              #
              # THAT NUMBER MUST BE AN INPUT CEILING, NOT THE TOTAL WINDOW — and
              # for a year it was the total, shaved by 12k. The old comment even
              # named the two deductions it then failed to make: seats request
              # max_tokens=32k of OUTPUT and CC reserves 20k, both of which come
              # out of the SAME 372k. So the honest arithmetic is
              #     372k total − 32k output − 20k reserve = 320k of input,
              # and 360k told CC it had 40k of room that physically was not there.
              #
              # MEASURED, not reasoned (2026-07-30): seat codex reached 369,663
              # recorded tokens and every request 400'd "input exceeds the context
              # window". /compact could not escape it — compaction REPLAYS the
              # oversized transcript, so it 400s too, and the seat is wedged with
              # no in-band exit. The owner found it before any instrument did.
              # That reading is a hard upper bound: the true ceiling is BELOW
              # 369,663. 320000 sits under it with the deductions accounted for.
              #
              # Direction matters more than precision here, and the kimi entry
              # below states the rule this violated: understating the window makes
              # CC compact early (wasteful, RECOVERABLE); overstating it sails the
              # seat into a 400 that in-band compaction cannot escape. When
              # unsure, go LOWER. Small-window codex families (spark 128k) want
              # 128000.
              "max_context": 320000,
              # THE MEASUREMENT ABOVE, AS A NUMBER THE GUARD CAN READ. A live
              # request at 369,663 tokens 400'd. That is an observed CEILING —
              # a disproof from the fatal side — and it is the strongest grade
              # in this table: it is the failure itself, measured, so it
              # outranks every claim including the owner's.
              "observed_context_ceiling": 369663,
              # 320000 DOES NOT MOVE ON 2026-08-03, and this record is why. The
              # owner restated the fleet's windows that day — "almsot all
              # models have 1m cw at this point, only codex is i think 360k" —
              # and then, directing the change that pinned gemini and ds4pro at
              # 1m, EXEMPTED this family in the same breath: "set 100% at those
              # (320k is fine for codex)". The 360k half is hedged ("i think"),
              # and raising a window on a hedge is the unrecoverable direction.
              # It is also refused twice over: 360000 clears the 369,663
              # ceiling by 1,663 tokens and blows straight past the input
              # arithmetic (372k total − 32k output − 20k reserve = 320k) that
              # test_codex_max_context_is_an_input_ceiling_not_the_total_window
              # pins. Two grades agree on this entry and neither is a guess.
              "owner_stated_window": {
                  "tokens": 320000,
                  "said": "2026-08-03",
                  "verbatim": "320k is fine for codex"},
              # --multi probe models: two DISTINCT models one codex OAuth serves,
              # the exact pair the proven mixed fan-out routed (run-1 2026-07-21).
              "probe_models": ("gpt-5.6-sol", "gpt-5.6-terra"),
              # PER-MODEL WINDOW OVERRIDES (task/379). A family is a seat-kind
              # (port, auth, mode, cred pool); a model window is a property of
              # the MODEL — a family per model would duplicate every key for
              # one integer and split the shared OAuth pool. Resolution:
              # model_context[resolved model] first, family max_context else.
              #
              # SPARK IS 76000, NOT 128000, and the arithmetic is this entry's
              # own law applied again: the number must be the INPUT CEILING,
              # never the total. Spark's TOTAL window is 128k; seats request
              # 32k of OUTPUT and CC reserves 20k, out of the SAME 128k:
              #     128k total − 32k output − 20k reserve = 76k of input.
              # The older aside above ("spark 128k want 128000") predates the
              # input-ceiling correction and carries the exact overstatement
              # that wedged this family at 369,663 — overstating is the
              # UNRECOVERABLE direction (compaction replays the oversized
              # transcript); understating merely compacts early. When unsure,
              # go LOWER.
              "model_context": {"gpt-5.3-codex-spark": 76000}},
    # kimi keys come in two flavors that 401 on each other's endpoint: a
    # CODING-plan key ("sk-kimi-…") wants api.kimi.com/coding/v1 (dual-wire;
    # OpenAI wire live-verified 2026-07-20), a Moonshot PLATFORM key (plain
    # "sk-…") wants api.moonshot.ai/v1 (serves kimi-k3 too — live-verified
    # 2026-07-21). key_base_urls dispatches by key prefix at add time (first
    # match wins); base_url is the no-match default. A mismatched pairing is
    # not a loud failure: the proxy loads the key as an auth, the first call
    # 401s upstream, and CLIProxyAPI quarantines the auth so every later call
    # 503s `auth_unavailable` — hence dispatch-by-shape, not one hardcoded URL.
    "kimi": {"port": 8318, "model": "kimi-k3", "mode": "proxy-key",
             "base_url": "https://api.moonshot.ai/v1",
             "key_base_urls": (("sk-kimi-", "https://api.kimi.com/coding/v1"),),
             "key_env": "KIMI_API_KEY", "provider": "moonshot",
             # owner rule 2026-07-29: our own PRIMARY sub first; the EMBER key
             # is loaned (unrestricted, but the fallback, not the default).
             "key_env_fallbacks": ("KIMI_API_KEY_PRIMARY",
                                   "KIMI_API_KEY_EMBER"),
             # one alias in the proxy config -> one probe; the mixed fan-out
             # leg needs two and SKIPs (loudly) for single-model families.
             # k3's real window is 1M (live-probed api.kimi.com/coding/v1/models
             # context_length=1048576, 2026-07-23); minting the max teaches CC
             # past its hardcoded 200k non-claude default so the gauge AND
             # autocompact track the true window (owner report: kimi was being
             # compacted ~5x too often).
             "max_context": 1000000,
             # THE PROBE ABOVE, AS A NUMBER THE GUARD CAN READ:
             # api.kimi.com/coding/v1/models reported context_length=1048576
             # on 2026-07-23. That is why the pin never needed a floor. ds4pro
             # carries the same grade off OpenRouter's public /v1/models; the
             # proxy-oauth families cannot, because theirs return {id, object,
             # owned_by} and nothing else. The owner independently said "kimi
             # is 1m" on 2026-08-03 — the pin was already 1000000 and did not
             # move, so no owner_stated_window is recorded here. The two
             # agreed, and the endpoint had said it first.
             "probed_context_length": 1048576,
             "probe_models": ("kimi-k3",)},
    # ds4pro = DeepSeek v4 Pro, served by whichever OpenAI-compatible gateway
    # the owner holds a LIVE bearer for. OUTBOUND KEY SOURCE: when
    # $DS4PRO_API_KEY / --key-from are absent the mint reads the bearer from
    # the OPENCODE tool auth store (OPENCODE_AUTHSTORE,
    # ~/.local/share/opencode/auth.json — owner-maintained, FRESH) by the
    # provider's `authstore` name; only type=="api" entries carry a bakeable
    # key. It FALLS BACK to the hermes credential_pool[<provider>]
    # (HERMES_AUTH) when the authstore lacks a usable key. The reader picks the
    # live one and it is baked 0600 into config.yaml at add time (value never
    # printed/logged). The former nous-portal agent_key + the STALE 2026-05-18
    # hermes pool mirror (both providers 401) are superseded by the authstore.
    # MULTI-PROVIDER: each gateway serves v4-pro under its OWN model id and
    # base_url — model ids probed live off <base_url>/models 2026-07-22:
    # opencode-go = deepseek-v4-pro (LIVE, HTTP 200), deepseek (native) =
    # deepseek-v4-pro (key valid but 402 Insufficient Balance — configured, not
    # live), openrouter = deepseek/deepseek-v4-pro (authstore key dead). So the
    # family carries a per-provider table and picks pool_default unless
    # `helm seat add ds4pro --provider <name>` overrides. pool_default =
    # opencode-go (the owner's long-term "open code go" route AND the one that
    # answers a REAL completion live). The proxy's openai-compatibility block
    # maps the claude-side alias "ds4-pro" to each provider's upstream id
    # (slashes never reach claude's --model). Note opencode-go's gateway 403s
    # (Cloudflare 1010) a request with NO User-Agent, but accepts any non-empty
    # UA — CLIProxyAPI's Go http client sends "Go-http-client/1.1" by default,
    # so the proxy leg passes. Port 8360: clear of codex 8317+N instance
    # headroom and kimi 8318 (interleave discipline: families claim ports tens
    # apart so instance ranges never collide). max_context mirrors codex's
    # shave: 1M window less headroom for the 32k max_tokens request + CC's 20k
    # reserve.
    "ds4pro": {"port": 8360, "model": "ds4-pro", "mode": "proxy-key",
               "key_env": "DS4PRO_API_KEY",
               "pool_default": "opencode-go",
               "pool_providers": {
                   "opencode-go": {
                       "base_url": "https://opencode.ai/zen/go/v1",
                       "upstream_model": "deepseek-v4-pro",
                       "authstore": "opencode-go"},
                   "deepseek": {
                       "base_url": "https://api.deepseek.com",
                       "upstream_model": "deepseek-v4-pro",
                       "authstore": "deepseek"},
                   "openrouter": {
                       "base_url": "https://openrouter.ai/api/v1",
                       "upstream_model": "deepseek/deepseek-v4-pro",
                       "authstore": "openrouter"},
               },
               # 1000000 — PROBE-BACKED, and the omission it replaces was a
               # borrowed argument rather than this family's own.
               #
               # THE MEASUREMENT WAS ALREADY IN THIS COMMENT AND WENT UNUSED.
               # OpenRouter publishes context_length=1048576 for
               # deepseek/deepseek-v4-pro on its PUBLIC /v1/models, no auth
               # required, read 2026-08-02. That is the same evidence grade
               # that has backed kimi's pin since 2026-07-23 — an endpoint
               # reporting the window of the model it serves — and it is
               # recorded as probed_context_length below so the guard enforces
               # the pin against it instead of trusting this prose.
               #
               # WHY IT WENT UNUSED: THIS ENTRY WAS GIVEN "THE GEMINI/GROK
               # POSTURE", AND THAT POSTURE RESTS ON GEMINI'S FACTS, NOT OURS.
               # gemini's proxy /v1/models returns only {id, object, owned_by},
               # so for gemini there is genuinely nothing to read and any
               # number would be a guess wearing a measurement's clothes. TRUE
               # OF GEMINI, NOT TRUE HERE: this family's model is published,
               # publicly, with a context_length. One family's reasoning was
               # copied onto another family whose facts are different.
               #
               # WHAT IS STILL UNMEASURED, STATED PLAINLY. The reading is off
               # the OPENROUTER leg and pool_default is opencode-go, whose
               # window nobody has read; api.deepseek.com answers 401 without a
               # key and we do not credential-hunt to settle a config value.
               # 1000000 therefore assumes the legs serve the same model with
               # the same window — 4.6% UNDER the one leg that is published,
               # which is the conservative side of that assumption but is still
               # an assumption. IF A LEG IS EVER MEASURED LOWER, that reading
               # governs and this pin must come down with it.
               #
               # THE CONTRARY EVIDENCE, WHICH DOES NOT GO AWAY. On 2026-07-29
               # ds4pro went hard-down: every wake 400d "Request exceeds the
               # context window", /compact ITSELF 400d, only an injected /clear
               # recovered it — and the seat kept posting healthy-looking
               # recaps throughout, so the room could not tell. That incident
               # recorded NO TOKEN COUNT, so it cannot be written as an
               # observed_context_ceiling and the guard can enforce nothing
               # from it. IF A FUTURE WEDGE IS CAUGHT WITH A NUMBER, RECORD IT
               # AS observed_context_ceiling — a measured ceiling outranks
               # every other grade and will refuse this pin automatically.
               #
               # WHAT HELM DID UNTIL NOW, MEASURED: launch_line passed CC
               # NOTHING for this family, so CC used its hardcoded 200k
               # non-claude default and autocompact gauged against 200000
               # ("cc-assumed-default"). Whether ds4pro's recent CONTEXT_FULL
               # reports are an UPSTREAM 400 or a purely client-side refusal
               # against that 200k is NOT established here and must not be
               # written down as if it were; someone has to look.
               #
               # THE DIRECTIONS ARE NOT SYMMETRIC and nothing here should be
               # read as if they were: understating costs one early compaction
               # (recoverable), overstating sails the seat into a 400 with
               # in-band compaction unable to escape (unrecoverable). The owner
               # directed this change to observe the result — "set 100% at
               # those (320k is fine for codex) and see what new errors if any
               # they get" — so treat the pin as a live experiment with a
               # reading behind it, not a settled fact. REVERT IS ONE LINE:
               # delete "max_context" and resolution falls back through
               # autocompact._window to _assume_window() 200000, exactly as
               # before. Keep probed_context_length either way; the endpoint
               # said what it said.
               "probed_context_length": 1048576,
               "max_context": 1000000,
               "probe_models": ("ds4-pro",)},
    # gemini + grok are mode "proxy-oauth": the PROXY holds the OAuth itself,
    # via its own login flag, so there is no sibling cred store to translate
    # from. Contrast "proxy" (codex), whose auth is lifted out of the codex
    # CLI's store, and "proxy-key" (kimi/ds4pro), which bakes a bearer. Here the
    # owner runs one interactive login against the seat's config and the proxy
    # writes the auth file into that config's auth-dir; helm's job is the home,
    # the config, the launch assets, and ADOPTING a credential the owner has
    # already minted elsewhere. See _add_proxy_oauth.
    #
    # THE MODEL NAMES ARE MEASURED, NOT CHOSEN BY VERSION NUMBER, and that
    # distinction is load-bearing. A 70-probe sweep across both proxies
    # (2026-07-25, ~/.helm/research/2026-07-25-council-model-sweep.md; every
    # probe recorded HTTP status AND completion_tokens, nothing counted as
    # working without non-zero tokens and non-empty text) graded each model on a
    # task whose correct answer was REFUTE. `grok-4.5` — the model anyone would
    # pick by name, and the one this family's own draft entry guessed —
    # CONFIRMED A FALSE CLAIM. Sycophancy is the exact failure a council exists
    # to defeat, so the highest version number was the worst available seat.
    # `gemini-3-pro`, the other draft guess, does not exist on this endpoint at
    # all. Re-run the sweep before changing either default.
    # 8390, NOT the 8370 the research config happened to pick: ds4pro reserves
    # 8350-8370 for its own base+N instance range, and 8370 sits exactly on that
    # boundary. Families claim ports TENS apart so no two base+N ranges ever
    # interleave (the discipline at _instance_port). Caught by the existing
    # test_family_ports_unique_with_interleave_headroom, which is a better port
    # test than the uniqueness check written alongside this entry — uniqueness
    # was satisfied while the invariant was violated.
    "gemini": {"port": 8390, "model": "gemini-3.6-flash-high",
               "mode": "proxy-oauth", "auth_type": "antigravity",
               # -antigravity-login, NOT -gemini-login: this binary
               # (7.2.88-helm.1) has six login entrypoints and DoGeminiLogin is
               # not among them — the gemini/gemini-cli auth types exist with no
               # interactive driver. Antigravity is the only binary-driven
               # Google OAuth, and it meters on an Antigravity credit balance
               # rather than the Gemini app subscription.
               "login_flag": "-antigravity-login",
               "auth_glob": "antigravity-*.json",
               # 1000000 — OWNER-STATED, 2026-08-03, and the omission it
               # replaces had become the wrong kind of honest.
               #
               # WHAT CHANGED IS EVIDENCE, NOT APPETITE. The old note asked for
               # "a real measurement, never the vendor's marketing number", and
               # half of one now exists: gemini was measured LIVE and RUNNING
               # while holding 279k, then 287k, 353k and 357k transcript tokens
               # (autocompact read 139.7% of the assumed 200k on the first of
               # those, and the seat kept answering). A seat cannot run past a
               # window it does not have, so >357k is a HARD FLOOR and 200k is
               # DISPROVEN. The owner's read from months of watching it:
               # "gemini does not die anyway, its cw is much larger than we are
               # accounting for."
               #
               # The floor is recorded as a NUMBER in observed_context_floor
               # below, not as prose, because `_unbacked_window_reason()`
               # enforces the pin against it at import. Prose evidence a test
               # can only prove non-empty is laundered vacuity.
               #
               # THE COST OF LEAVING IT UNSET WAS NOT ZERO, which the old note
               # priced as merely "wasteful". Against a disproven 200k the seat
               # sat permanently >100%, so autocompact tried to compact a
               # HEALTHY seat every pass and refused every time on an unrelated
               # pane-identity gate. That is a watchdog spending attention on a
               # seat that never needed it, and the only reason it never landed
               # a needless /compact was an accident of a different guard.
               #
               # THE PIN IS 1000000 AND IT IS NOT THE FLOOR'S NUMBER. It was
               # 750000 for part of 2026-08-03 — a value chosen to sit inside
               # OBSERVED_FLOOR_HEADROOM of the 357k floor (2.10x, admitted;
               # the 1048576 vendor page is 2.94x and refused). Then the owner
               # stated the window directly: "ds4 is 1m, kimi is 1m, gemini
               # 1m", in the same breath as "almsot all models have 1m cw at
               # this point, only codex is i think 360k". 1000000 is 2.80x the
               # floor, so THE FLOOR CANNOT BACK IT and the headroom arm would
               # refuse it — correctly. @kimi's review of that arm stands:
               # refusing 1m on a 417k floor was the right behaviour, and an
               # owner statement is not the same grade of floor as a measured
               # one.
               #
               # SO THE PIN CHANGED GRADE, NOT THE GUARD'S STANDARDS. The
               # number is backed by owner_stated_window below — a direct claim
               # about the model, quoted and dated — and it is NOT written into
               # observed_context_floor, which would say a seat was watched
               # holding a million tokens. Nobody watched that. The floor STAYS
               # at its measured 357000 and keeps doing the thing it actually
               # proves: it disproves everything below it, so the guard still
               # refuses any pin under 357000. Both keys are present in this one
               # entry precisely so the two grades are readable side by side —
               # 357000 is what we SAW, 1000000 is what the owner SAID.
               #
               # RESIDUAL RISK, STATED PLAINLY AND WORSE THAN IT WAS AT 750k.
               # 1000000 is 2.80x anything measured. If the true window sits
               # between 357k and 1m, this trades an over-eager watchdog for a
               # LATE one, and late is the unrecoverable side: the seat sails
               # past the real limit and 400s "input exceeds the context
               # window", which in-band compaction cannot escape because the
               # replay is the same oversized transcript (the wedge
               # helm/watchdog.py exists to catch). Understating costs one
               # early compaction. These are NOT symmetric.
               #
               # THE OWNER IS BUYING THAT RISK ON PURPOSE, TO GET A READING:
               # "set 100% at those (320k is fine for codex) and see what new
               # errors if any they get". Treat it as a live experiment, not a
               # settled fact. REVERT IS ONE LINE — delete "max_context" and
               # resolution falls back to _assume_window() 200000 exactly as
               # before; keep observed_context_floor and owner_stated_window,
               # which record what was seen and what was said. A binary search
               # on input size against the live 400 still settles it for good,
               # and still should — and if a wedge is ever caught WITH a token
               # count, record it as observed_context_ceiling and it will
               # outrank this statement automatically.
               "observed_context_floor": 357000,
               "max_context": 1000000,
               "owner_stated_window": {
                   "tokens": 1000000,
                   "said": "2026-08-03",
                   "verbatim": "ds4 is 1m, kimi is 1m, gemini 1m"},
               "probe_models": ("gemini-3.6-flash-high",)},
    # grok rides the native xAI OIDC device-code flow against
    # auth.x.ai/.well-known/openid-configuration, which routes to
    # cli-chat-proxy.grok.com/v1 — the SUBSCRIPTION-backed Grok CLI proxy, i.e.
    # the owner's paid X account, not a metered api.x.ai key. Not cursor-agent:
    # `cursor-agent models` returns "No models available for this account" and
    # its stored modelParameters carry no grok at all.
    # grok-build-0.1 per the owner (2026-07-25): he wants 4.5+ / build-0.1+.
    # NOT grok-4.5 — the 70-probe sweep put it in the AVOID list because it
    # CONFIRMED A DEMONSTRABLY FALSE CLAIM on a task whose right answer was
    # REFUTE, which is the sycophancy failure a council exists to catch. A higher
    # version number is not a better reviewer. build-0.1 is newer, satisfies the
    # ask, and is NOT in the avoid list; it costs ~2.2x 4.20-0309-reasoning
    # (34672 vs 15739) and ~140s vs 60s, but produced the most output of any grok
    # tested. If the X window tightens, 4.20-0309-reasoning is the cheap fallback
    # that still refutes correctly.
    "grok": {"port": 8380, "model": "grok-build-0.1",
             "mode": "proxy-oauth", "auth_type": "xai",
             "login_flag": "-xai-login",
             "auth_glob": "xai-*.json",
             # NO max_context AND NONE OF THE FOUR BACKING KEYS. This used to
             # read "same reason as gemini above"; on 2026-08-03 gemini took a
             # pin (observed floor, then an owner statement) and ds4pro took
             # one (owner statement), and grok took NEITHER, so the entries no
             # longer say the same thing and this one must state its own
             # reason. /v1/models carries no context_length here, no grok seat
             # has been watched holding more than CC's assumed 200k, and the
             # owner's 2026-08-03 sentences name ds4, kimi, gemini and codex —
             # NOT grok. So there is nothing observed, nothing probed, nothing
             # crashed into and nothing said: `_unbacked_window_reason()` would
             # refuse any pin here. Absent stays the honest value and CC's
             # conservative default is the safe direction.
             #
             # GROK IS NOW THE ONLY UNPINNED FAMILY IN THE TABLE, which makes
             # it the sole surviving control for every window test that needs
             # one (tests/test_autocompact.py, tests/test_seat.py). Pinning it
             # without first re-homing those controls turns them
             # green-for-the-wrong-reason — a `_window()` that answered one
             # number for everybody would stop being detectable.
             "probe_models": ("grok-build-0.1",)},
}

def _family_port_bases_are_unique():
    """One collision-free owner for the port namespace: no two families share
    a base port. The instance derivation (base+N) is per-family, so distinct
    bases are the floor the whole scheme stands on; the interleave headroom
    between a proxy family's base+N range and the next family's base is a
    FAMILIES-table discipline (see `_instance_port`)."""
    bases = [f["port"] for f in FAMILIES.values()]
    return len(bases) == len(set(bases))


assert _family_port_bases_are_unique(), \
    "FAMILIES base ports must be distinct (the instance-port scheme's floor)"

# --- what may back a pinned context window ---------------------------------
# FOUR GRADES OF EVIDENCE, AND THEY NEVER MERGE. Each one says something
# DIFFERENT about where a number came from, so each gets its OWN key and every
# refusal below NAMES the key it is about. A reader of one FAMILIES entry can
# therefore always tell whether a window was watched, probed, crashed into, or
# spoken — and no future edit can blur two of them by writing into a shared
# "evidence" field, because there is no shared field to write into.
#
#   observed_context_floor    A LIVE SEAT WAS SEEN HOLDING THIS. A seat alive
#                             and answering at N transcript tokens cannot have
#                             a window smaller than N. Disproves everything
#                             BELOW it; says nothing about how far above the
#                             true window sits — hence OBSERVED_FLOOR_HEADROOM.
#   observed_context_ceiling  A REQUEST THIS SIZE 400'd. The failure itself,
#                             measured. Strongest grade in the table because
#                             it is a disproof from the fatal side, and it
#                             OUTRANKS EVERY OTHER GRADE INCLUDING THE OWNER'S.
#   probed_context_length     THE ENDPOINT REPORTED IT. /v1/models carrying
#                             context_length for the model actually served —
#                             kimi off api.kimi.com (2026-07-23), ds4pro off
#                             OpenRouter's public no-auth listing (2026-08-02).
#                             A proxy-oauth /v1/models returns only {id,
#                             object, owned_by} (probed 2026-07-25, both
#                             council families), which is why gemini and grok
#                             can never earn this grade.
#   owner_stated_window       THE OWNER SAID SO — added 2026-08-03. A direct
#                             claim about the model from the person who owns
#                             the subscriptions and has watched these seats for
#                             months. NOT a measurement, NOT laundered into
#                             observed_context_floor (which would assert a seat
#                             was watched holding a number nobody watched), and
#                             carried as a RECORD, not an int: {tokens, said,
#                             verbatim}. See _owner_statement_reason.
#
# THE TABLE CARRIES A WORKED EXAMPLE OF THE DISTINCTION, ON PURPOSE. ds4pro's
# 1000000 came from a PUBLISHED ENDPOINT and reads as probed_context_length;
# gemini's 1000000 came from the OWNER SAYING SO and reads as
# owner_stated_window with his sentence attached. Same number, same day,
# different provenance, and no reader of either entry can mistake one for the
# other. If that ever stops being true the design has failed its only job.
#
# HEADROOM IS WHAT KEEPS A FLOOR FROM BECOMING A LICENCE — and it binds the
# FLOOR grade only. A floor comes from whatever the seat happened to be holding
# when someone looked, never from a search, so the true window sits above it by
# an unknown amount and a floor-backed pin must be allowed above the reading.
# It may not sit ARBITRARILY above it, because "arbitrarily above the last
# thing we saw" is exactly where a vendor's marketing number lives. 2.5 is set
# at the line between those two: against gemini's 357000 floor, 750000 is 2.10x
# and would be ADMITTED while the 1048576 a vendor page publishes is 2.94x and
# is REFUSED. WIDENING THIS CONSTANT TO ADMIT A VENDOR NUMBER DEFEATS THE ONLY
# THING IT DOES; raise the floor with a new live reading instead.
#
# THE RATIO DOES NOT BIND AN OWNER-STATED WINDOW, DELIBERATELY. This ceiling
# bounds EXTRAPOLATION FROM A FLOOR: it exists because the floor-backed pin is
# computed FROM the floor, so without a bound the floor becomes a licence to
# multiply. An owner statement is not extrapolated from anything — it is an
# independent direct claim about the model, and it would be a category error to
# rule that the owner may only say things within 2.5x of whatever a seat
# happened to be holding the last time somebody looked. That would make the
# owner's knowledge a function of OUR sampling luck. What the floor still does
# to an owner statement is the thing it actually proves: it disproves
# everything below itself, so an owner statement UNDER a live reading is
# refused (the `win < floor` arm, reached before the owner grade is consulted).
OBSERVED_FLOOR_HEADROOM = 2.5

# Every key that may back a max_context. A pin carrying none of them is a bare
# assertion; the refusal hands the reader this whole menu so the fix is never
# "invent a floor".
WINDOW_BACKINGS = ("observed_context_floor", "observed_context_ceiling",
                   "probed_context_length", "owner_stated_window")

# Mirrors autocompact.CC_ASSUMED_WINDOW. Not imported: autocompact imports
# seat (see autocompact._window), so the dependency only runs one way. The
# two are pinned equal by
# tests/test_seat_proxy_oauth.py::test_the_assumed_window_mirror_matches_autocompact.
_CC_ASSUMED_WINDOW_MIRROR = 200000

# A window as the OWNER writes one: "1m", "320k", or bare digits. The unit
# suffixes are not decoration — they are how the number appears in the quoted
# sentence, and _owner_statement_reason requires the pinned number to be one
# the quote actually STATES.
_QUOTED_COUNT = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)\s*([mk])?(?![\w])",
                           re.IGNORECASE)
_QUOTED_WORD = re.compile(r"[a-z0-9]+")


def _quoted_token_counts(text):
    """Every token count a quoted sentence STATES, in the owner's notation."""
    out = set()
    for digits, unit in _QUOTED_COUNT.findall(text or ""):
        mult = {"m": 1000000, "k": 1000}.get(unit.lower(), 1)
        out.add(int(round(float(digits) * mult)))
    return out


def _family_owner_aliases(name, fam):
    """The words an owner may use for this family — DERIVED FROM WHAT THE
    FAMILY ALREADY DECLARES, never inferred from the shape of its key.

    The owner types the MODEL, not the FAMILIES key: "ds4 is 1m" is about
    ds4pro. So the key alone is too narrow. The first version of this reached
    for `name.startswith(word)`, which is too WIDE in the one direction that
    matters — it makes every 3+char prefix an alias, so a fabricated quote
    reading "gem 1m" backs a gemini pin (@kimi probed it live against the real
    predicate, 2026-08-03). An anti-fabrication arm that accepts a word the
    owner would never type is failing open.

    So an alias is DECLARED, and matched exactly:
      * the FAMILIES key itself;
      * the FIRST alphanumeric segment of each probe_model and of model —
        "ds4-pro" -> ds4, "kimi-k3" -> kimi. This is the segment that names
        the family; the rest is version and variant.
      * anything in an explicit `owner_aliases`, for an owner idiom that is
        not any model's stem.
    ONLY THE FIRST SEGMENT, deliberately. Taking every segment would make
    "flash" and "high" aliases of gemini (from gemini-3.6-flash-high), so
    "flash 1m" would back a gemini pin — the same failing-open in a new coat.

    A DERIVED STEM COUNTS ONLY WHERE IT IS CONSISTENT WITH THE KEY — the stem
    and the key must share a prefix in one direction or the other. The first
    cut of this function derived from the models alone, and the existing
    re-filing test caught it immediately: copy gemini's whole entry under the
    key "grokish" and the entry BRINGS gemini-3.6-flash-high with it, so
    "gemini" stayed an alias and the owner's gemini sentence backed a pin
    filed under another name. That is the precise cross-filing this arm
    exists to refuse, reintroduced by the fix for a different hole.

    This prefix test is NOT the one @kimi refuted, and the distinction is the
    whole design: it compares two values the TABLE declares (a model stem
    against its own key), never the key against arbitrary quoted text. "gem"
    is nobody's declared model stem, so it can never become an alias by this
    route. A family whose model is genuinely unrelated to its key — codex
    declares gpt-5.6-sol — gets only its key, and must say `owner_aliases`
    out loud if the owner really does type the other word. Declared beats
    inferred in exactly the place where inference was silently wrong."""
    key = name.lower()
    out = {key}
    models = list(fam.get("probe_models") or ())
    if fam.get("model"):
        models.append(fam["model"])
    for model in models:
        segments = _QUOTED_WORD.findall(str(model).lower())
        stem = segments[0] if segments else ""
        if stem and (key.startswith(stem) or stem.startswith(key)):
            out.add(stem)
    # An explicit alias is the owner's vocabulary, not the table's, so it is
    # exempt from the key-consistency rule — and still bound by the
    # cross-family collision assert, which is what stops it being a backdoor.
    out.update(str(a).lower() for a in (fam.get("owner_aliases") or ()))
    return {a for a in out if len(a) >= 3 and not a.isdigit()}


def _quote_names_family(name, text, fam=None):
    """Does the quote name the family it is filed under? EXACT match against
    the family's declared aliases (`_family_owner_aliases`) — never a prefix.

    `fam` exists so a test can drive a bogus family through the real predicate
    without mutating the live FAMILIES; production passes nothing."""
    if fam is None:
        fam = FAMILIES.get(name) or {}
    aliases = _family_owner_aliases(name, fam)
    return any(word in aliases
               for word in _QUOTED_WORD.findall((text or "").lower()))


def _owner_statement_reason(name, win, owner, fam=None):
    """Empty when an owner-stated window is really the owner's and really is
    this number; else why it is not.

    WHAT STOPS AN AGENT WRITING "OWNER SAID SO". Three mechanical constraints,
    none of which an agent can satisfy by typing a number it likes:
      1. the record must carry the DATE and the OWNER'S OWN WORDS, so the claim
         is a quotation with a timestamp rather than an assertion;
      2. the quote must STATE the number — "1m" or "320k" or the digits — so
         the pin cannot be an agent's summary, rounding, or extrapolation of
         what the owner meant. A quote that says "1m" backs 1000000 and
         nothing else;
      3. the quote must NAME the family, so one sentence about gemini cannot
         be re-filed under grok.
    HONEST LIMIT, STATED HERE BECAUSE IT CANNOT BE CLOSED IN CODE: nothing in
    this process can prove the owner ever said the words. What it does is
    force the fabrication to be an explicit false QUOTATION in a committed
    diff, attributed and dated, instead of a bare integer nobody can question.
    """
    if not isinstance(owner, dict):
        return ("%s: owner_stated_window must be a record carrying %s, not a "
                "bare value — a number with no words attached is exactly the "
                "agent-invented window this grade exists to keep out"
                % (name, "{tokens, said, verbatim}"))
    tokens, said = owner.get("tokens"), owner.get("said")
    verbatim = owner.get("verbatim")
    missing = [k for k, v in (("tokens", tokens), ("said", said),
                              ("verbatim", verbatim)) if not v]
    if missing:
        return ("%s: owner_stated_window is missing %s — an owner statement "
                "is only an evidence grade while it carries the DATE and the "
                "OWNER'S OWN WORDS" % (name, "/".join(missing)))
    if win > tokens:
        return ("%s pins max_context=%d ABOVE the %d its owner_stated_window "
                "records — an owner statement backs the number the owner said "
                "and never an agent's enlargement of it. Under it is allowed: "
                "that is the recoverable direction" % (name, win, tokens))
    if tokens not in _quoted_token_counts(verbatim):
        return ("%s: the quoted owner words do not state %d — an owner-stated "
                "window must be readable in the quote itself (\"1m\", "
                "\"320k\", or the digits), never summarised from it: %r"
                % (name, tokens, verbatim))
    if not _quote_names_family(name, verbatim, fam):
        return ("%s: the quoted owner words name none of %s — a sentence about "
                "one model may not back a pin on another, and a prefix of the "
                "family name is not the family: %r"
                % (name,
                   "/".join(sorted(_family_owner_aliases(
                       name, FAMILIES.get(name) or {} if fam is None else fam))),
                   verbatim))
    return ""

# CC's autocompact trigger = pct × (window − 20k). 80% lands the trigger with real
# headroom (sol ≈ 272k, well under the ~340k reject point; spark ≈ 86k, under
# 128k). ALIGNED to the helm watchdog's DEFAULT_THRESHOLD=80 (autocompact.py) so
# the two knobs can never imply different firing points — the owner watching this
# 78 while the watchdog armed at 90 was exactly the confusion the 2026-07-29
# diagnosis surfaced. Honesty about what this knob does: for a PROXIED seat it is
# INERT (the cli-proxy translator reports message_start.usage=0, so CC's live
# gauge never leaves ~0% and this percent multiplies a near-zero numerator — see
# autocompact.py's DEFAULT_THRESHOLD note); the watchdog reading the transcript is
# the real enforcer. Kept aligned, not dropped, so the launch line still declares
# one coherent 80. Honored only for non-`claude-` model names — exactly the proxy
# seats. Both env knobs verified in CC 2.1.216 (undocumented — re-verify on CC
# upgrades: `strings` the binary for the names).
AUTOCOMPACT_PCT_OVERRIDE = "80"
