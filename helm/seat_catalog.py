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

# SCHEMA-UNSAFE TOOLS ARE DENIED ON PROXY FAMILIES ONLY (task/1941, measured
# on a fresh codex instance). Claude Code 2.1.266 ships an `Artifact` tool
# whose parameter pattern uses `\p{Cc}`-style Unicode classes. Anthropic's
# validator accepts them; OpenAI's function-schema validator does not, and it
# rejects the WHOLE tool list per request:
#     API Error: 400 Invalid schema for function 'Artifact':
#       '^(?!__.*__$)[^\p{Cc}\p{Cf}\p{Zl}\p{Zp}"\\./[\]]{1,200}$' is not a 'regex'.
# so every turn on a codex seat dies before the model runs, and the seat reads
# as hung to every instrument. A claude-family seat on the same CC build was
# fine (live), which is why this is keyed on the BACKEND and not
# on the harness version. Denying uses the same mechanism the plan-entry deny
# already relies on: the tool vanishes from the session-init tools array, so
# its schema is never sent. A native claude seat keeps Artifact.
SCHEMA_UNSAFE_TOOLS = ("Artifact",)

# The three modes a family launches through CLIProxyAPI in. A seat in any of
# them spends a METERED upstream quota (codex ultra, kimi, gemini, ...) on
# every request its Claude Code process makes, subagents included.
PROXY_MODES = ("proxy", "proxy-key", "proxy-oauth")

# A PROXY SEAT CANNOT SPAWN THROUGH A SKILL (task/2287). A codex seat that
# invokes the bundled `code-review` skill has the SKILL launch background
# reviewer subagents, forks among them, whatever its brief says about
# subagents (task/796 and task/2301 are two such runs). A brief binds the
# model's own Agent calls; it cannot bind a skill's — the skill launches them
# itself — and a fork replays the parent's whole context, which is how one
# session emptied two codex ultra quotas (437 subagents, 32 forks). Two
# rules, both measured on CC 2.1.268:
#   Skill        — the WHOLE tool. MEASURED with `claude -p --disallowedTools
#                  Skill`: the session reports no Skill tool at all, while
#                  the per-skill form `Skill(code-review)` left both
#                  `code-review` entries in the tool's own description. A
#                  slash command the OWNER types in the pane is a separate
#                  path and still works; only model invocation is gone.
#   Agent(fork)  — CC's Agent tool carries a dedicated branch for it (read in
#                  the 2.1.268 binary): a call with subagent_type "fork" that
#                  a deny rule matches raises "Agent type 'fork' has been
#                  denied by permission rule 'Agent(fork)'" before any child
#                  exists. Non-fork subagents stay available; the brief's
#                  budget still governs those.
# KEYED ON THE MODE, unlike the schema-unsafe set: this is a quota law about
# metered upstreams, not a measured validator quirk, so it holds for every
# proxy family at once. A native claude seat keeps both.
#
# WORKFLOW IS NOT IN THIS SET, AND WAS (task/2491, task/2559). A workflow
# script spawns through its own `agent()` — its built-in subagent is minted
# `tools:["*"]` — so a brief binds it as little as it binds a skill, and a
# briefed reader on a codex pane once reached 14 concurrent agents through
# it. The wholesale deny that answered that took the tool away from the one
# delegation shape the owner ranks as valid on a proxy seat: "astra can run
# sol workflows just like fable can run opus workflows ... both valid
# strategies for cross-model same-family delegation within a cc process, so
# the policy needs to be more nuanced". So a proxy seat KEEPS Workflow, and
# the bound that the brief could not supply is mechanical instead, in two
# parts that the launch line and the seeded settings both carry:
#   the CAP   — WORKFLOW_AGENT_CAP concurrent agents per workflow run. READ
#               in the 2.1.272 bundle: the Workflow tool's agent gate is
#               `Wt=a.CLAUDE_CODE_WORKFLOW_MAX_CONCURRENT_AGENTS??Pr`, a
#               semaphore minted per invocation, where Pr is derived from
#               the seat-wide subagent cap (`Math.min(16,Math.max(2,n-2))`,
#               16 on a default seat). The env var is the ONLY cap knob the
#               harness exposes for workflows; it bounds one run, so two
#               workflows in flight at once may hold twice the cap, and the
#               seat-wide CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS (default 20)
#               is left as it was.
#   the TIER  — a workflow agent's model is what the script's `agent()`
#               names, resolved by CC's own alias table (read in the
#               2.1.280 bundle, `latest_per_family`: opus ->
#               claude-opus-5-5, sonnet -> claude-sonnet-5, fable ->
#               claude-fable-5-1) and sent upstream as that id, exactly like
#               a built-in agent type's frontmatter. The seat's proxy config
#               already routes every such id through its `oauth-model-alias`
#               block (task/1948) onto THIS SEAT's launch model or the
#               family's `subagent_tiers` entry for it (task/2466): on an
#               astra seat opus/fable -> gpt-6-astra and sonnet/haiku ->
#               gpt-5.6-sol; on a sol-launched seat every id -> gpt-5.6-sol,
#               because an id with no tier follows the seat's own model and
#               nothing in the generator can escalate a pane. An agent that
#               names no model inherits the pane's. NOT mechanical: a script
#               that names an upstream id verbatim (`gpt-6-astra`) is served
#               as written — the alias rows are `fork: true`, which keeps the
#               family's real ids routable on purpose.
SPAWN_DENIED_TOOLS = ("Skill", "Agent(fork)")

# A RETIRED DENY LEAVES A PROXY SEAT ONLY WHEN A RECORD SAYS WHO WROTE IT.
# The seeder (_seed_seat_settings) only ever APPENDS the current deny set to
# a seat's settings.json, and its documented contract preserves every
# pre-existing entry, so an entry it seeded under an earlier table would
# survive every later refresh — an upgraded seat would keep the Workflow deny
# a fresh seat no longer carries, and removing it from the launch argv does
# not override the settings file. RETIRED_SPAWN_DENIES names what may leave;
# three explicit legs say how, and none of them reads a list's SHAPE or a
# file's LOCATION as proof of authorship — an operator can author any list
# helm can, anywhere helm writes:
#   RECORDED BY THE OPERATOR — `helm.operator_denies` (OPERATOR_RECORD_DENIES),
#     a helm-honoured key the refresh never edits and always re-applies to
#     permissions.deny. An operator who wants Workflow denied on a proxy
#     seat records it there; the entry is kept, and re-added if missing.
#   RECORDED BY HELM — `helm.seeded_denies` (SEED_RECORD_DENIES) lists the
#     entries helm itself wrote to that file, updated on every seed; the
#     seeder is the only writer, the census and doctor may read it. A
#     retired name the record names is removed, and the record loses it. The
#     record never names an entry the list does not carry: a name the
#     operator removed by hand, or a retirement removed, is dropped from the
#     record on the next refresh.
#   UNRECORDED — left untouched, Workflow included, and the refresh prints
#     one stderr line naming the entry, the file, and `helm seat retire-deny`,
#     the verb that removes such an entry DELIBERATELY (dry-run by default;
#     `--apply` is an owner decision when run fleet-wide, and nothing in
#     deploy or doctor runs it).
# A native seat is not a proxy family: retired_denies is empty there and
# nothing is removed; its operator key is honoured like any other.
#   Workflow — seeded on every proxy family between task/2491 and task/2559,
#              now admitted under WORKFLOW_AGENT_CAP.
RETIRED_SPAWN_DENIES = ("Workflow",)

#: The settings.json key helm owns; under it, the deny entries helm itself
#: wrote to that file, and the deny entries the operator asks helm to keep.
SEED_RECORD_KEY = "helm"
SEED_RECORD_DENIES = "seeded_denies"
OPERATOR_RECORD_DENIES = "operator_denies"


def retired_denies(family):
    """The deny entries a refresh removes from a seat of `family` when helm's
    own record names them: the retired carriers on a proxy family, nothing
    anywhere else."""
    fam = FAMILIES.get(family) or {}
    if fam.get("mode") not in PROXY_MODES:
        return ()
    return tuple(t for t in RETIRED_SPAWN_DENIES if t not in SPAWN_DENIED_TOOLS)


#: Concurrent agents one workflow run may hold on a proxy seat.
WORKFLOW_AGENT_CAP = 4
#: The harness knob that carries it (read verbatim in the 2.1.272 bundle).
WORKFLOW_CAP_VAR = "CLAUDE_CODE_WORKFLOW_MAX_CONCURRENT_AGENTS"


def workflow_cap_env(family):
    """The (name, value) env pairs that cap a workflow run on a seat of
    `family` — one pair on a proxy family, none on anything else.

    KEYED ON THE MODE like SPAWN_DENIED_TOOLS: the cap is the same quota law
    about metered upstreams, so it holds for every proxy family at once and
    never rides a native claude seat, whose workflows are the owner's own."""
    fam = FAMILIES.get(family) or {}
    if fam.get("mode") not in PROXY_MODES:
        return ()
    return ((WORKFLOW_CAP_VAR, str(WORKFLOW_AGENT_CAP)),)


def workflow_cap_env_words(family):
    """The launch-line env words (leading space each) for workflow_cap_env —
    one rendering, so the pane and the seeded settings cannot drift apart."""
    return "".join(" %s=%s" % kv for kv in workflow_cap_env(family))


def denied_tools(family):
    """The tools a seat of `family` launches WITHOUT, on both deny surfaces.

    Schema-unsafe entries are KEYED ON THE FAMILY WHOSE VALIDATOR WAS
    MEASURED, never on the proxy mode as a shape — the review's point: only the
    Codex/OpenAI backend was probed, and Gemini's own request cleaner already
    strips propertyNames, so a mode-wide deny was broader feature loss than
    the evidence supported. A family earns an entry in its own
    `schema_unsafe_tools` by a measured 400, and loses it the same way. The
    spawn deny (task/2287) is keyed on the mode instead: every proxy family
    meters the same way. Every family loses plan entry."""
    fam = FAMILIES.get(family) or {}
    extra = tuple(fam.get("schema_unsafe_tools", ()))
    if fam.get("mode") in PROXY_MODES:
        extra += SPAWN_DENIED_TOOLS
    return (PLAN_ENTRY_TOOL,) + extra


# FEEDBACK ABOUT HELM NEVER LEAVES HELM (task/2328; owner ruling: "every codex
# tries to use the anthropic feedback mechanism in claudecode for helm
# feedback ... feedback should be told to go through helm team").
# Claude Code 2.1.247+ ships a `SendFeedback` tool (changelog: "Claude can
# draft a feedback report for you to review and send from /feedback"). A seat
# that hits a helm defect drafts it into CC's OWN queue — the pane shows "Bug
# report drafted: <title> ... 1 to review · 2 to send" — which submits to
# Anthropic and is invisible to the fleet; one codex seat's transcript carries
# three such drafts from one day. The prompt-only rule (store heuristic
# helm-feedback-goes-to-rows-never-cc-feedback) did not hold, so the cure is
# mechanical. Three switches, READ IN THE 2.1.269 BUNDLE (minified names kept
# so the next reader can find them with `rg -a`):
#   feedbackDrafts    a settings.json enum "notify"|"quiet"|"off" (userSettings
#                     scope). The tool's isEnabled() is iL() = SQe()!=="off" &&
#                     rCn(), and SQe() reads this key — so "off" removes the
#                     tool from the session's tool list the way a deny does.
#   DISABLE_FEEDBACK_COMMAND / DISABLE_BUG_COMMAND
#                     J8() returns "<cmd> has been disabled via the ..." when
#                     EITHER var is set, whichever of /feedback or /bug asked;
#                     rCn() is false whenever J8()!==null. So each var alone
#                     disables BOTH slash commands AND the drafts tool.
# The env pair rides EVERY seat launch — the proxy families' launch line and
# the native claude door (launch.build_env) alike: this is a law about where a
# seat's feedback GOES, not about a metered upstream, so it is not keyed on
# the mode. The setting rides the helm-minted seat config dir (the surface a
# resume or an orca relaunch inherits — the deny set's reason, verbatim).
# The owner's own homes are untouched on purpose: this never rides
# hooks.ESTATE_DEFAULTS, and /feedback at HIS keyboard stays his.
FEEDBACK_ENV = (("DISABLE_FEEDBACK_COMMAND", "1"), ("DISABLE_BUG_COMMAND", "1"))
FEEDBACK_DRAFTS_SETTING = ("feedbackDrafts", "off")
# The switches take the mechanism away; this sentence says where feedback goes
# instead, so a seat does not simply fall silent about a defect. It is seeded
# into <config dir>/CLAUDE.md — CC's User memory file (2.1.269: the "User"
# memory path is <CLAUDE_CONFIG_DIR>/CLAUDE.md; a marker planted there was
# quoted back by every `claude -p` probe run against such a dir).
FEEDBACK_RULE = ("Feedback about helm, the fleet or its processes goes to a "
                 "helm task row (helm task add) or to the integrator in the "
                 "helm room, never to Claude Code's /feedback or /bug "
                 "mechanism, which submits to Anthropic and is invisible to "
                 "the fleet.")


def feedback_env_words():
    """The launch-line env words (leading space each) that keep a seat's
    feedback inside helm — one rendering, so the pane and the native door
    cannot drift apart."""
    return "".join(" %s=%s" % kv for kv in FEEDBACK_ENV)


# CLAUDE CODE'S BUILT-IN AGENT TYPES CARRY A CLAUDE MODEL IN THEIR FRONTMATTER,
# and a proxy-family seat sends that id upstream as-is. MEASURED
# on an astra seat: a general-purpose child completed, an
# Explore child died HTTP 502 because it sent "claude-opus-5" to the Codex
# proxy — CLAUDE_CODE_SUBAGENT_MODEL does not override per-agent frontmatter.
# The owner-layer cure is the proxy's own oauth-model-alias: every one of
# these ids resolves to a model the family SERVES on the family's channel, so
# a subagent of any built-in type routes somewhere real. WHICH model is the
# family's choice: its launch model for every id by default, or — when the
# family declares a `subagent_tiers` table — the model that table names for
# that id (see subagent_tier_model below). The list is the current Claude
# model catalog; an id missing here 502s exactly as before, so extend it when
# Anthropic ships a new id. THE UNDATED HAIKU ID IS ONE SUCH: CC's own alias
# table (read in the 2.1.272 bundle, `latest_per_family`) resolves the word
# `haiku` — what a Workflow script's agent() or an Agent call names — to
# `claude-haiku-4-5`, not to the dated id the built-in frontmatter carried,
# so without its own row that agent 502s on every proxy family. Each new id
# is appended LAST so every existing block gains one row at its tail and
# nothing reorders: `claude-opus-5-5` (Claude Code 2.1.280 names it) follows
# the undated haiku id, and routes where `claude-opus-5` does.
CC_AGENT_FRONTMATTER_MODELS = ("claude-opus-5", "claude-sonnet-5",
                               "claude-haiku-4-5-20251001", "claude-fable-5-1",
                               "claude-haiku-4-5", "claude-opus-5-5")


def family_catalogued_models(fam):
    """Every model id this family entry catalogues, in declaration order.

    ONE READING for the places that each spelled this union out: the
    routing-only refusal below, the tier validator beside it, and the
    watchdog's same-family name proof (proxywatch
    `_alias_rows_that_rename_a_route`). A model is catalogued when the family
    declares it as its launch `model`, as a `--multi` probe model, or as a
    `model_context` window key — and nothing else is a model this family is
    known to serve. Takes the ENTRY, never a family name, so a caller holding
    a test table (or a patched FAMILIES) is answered about the table it holds.
    """
    declared = ((fam.get("model"),) + tuple(fam.get("probe_models") or ())
                + tuple((fam.get("model_context") or {}).keys()))
    # DEDUPED, order kept: the same id is normally declared two or three times
    # over (codex names astra as its launch model, as a probe model and as a
    # model_context key), and the refusal below RENDERS this tuple — it read
    # "Catalogued: gpt-6-astra, gpt-6-astra, gpt-5.6-sol, gpt-5.3-codex-spark,
    # gpt-6-astra, gpt-5.6-sol", which an operator reads as a defect in the
    # catalog rather than as the answer to their question.
    return tuple(dict.fromkeys(model for model in declared if model))


#: The ONE `terms.verdict` that admits a model to a lane which reads the
#: owner's private repositories. Every other value — including an honest
#: "unknown" — keeps the model out, because the failure this gate exists to
#: stop (the PROMPTS ARE THE PRICE) is silent, irreversible and invisible in
#: every pricing field. Absence of a training claim is not a finding, so a row
#: that cannot say `private-code-safe` and say WHO read the terms and WHEN is
#: not a row this family may map.
DATA_TERMS_SAFE = "private-code-safe"

#: Keys a `model_providers` row must carry, and nothing outside this set is
#: admitted — an unknown key is a typo whose next reader is a route that
#: silently never applied.
MODEL_PROVIDER_KEYS = frozenset((
    "alias", "default", "upstream_model", "base_url", "pricing", "terms",
    "probed_context_length", "probed_max_completion_tokens"))


def family_model_providers(fam):
    """This family's per-MODEL provider table, or an empty mapping.

    NOT `pool_providers`, AND THE DIFFERENCE IS THE WHOLE POINT. ds4pro's pool
    is ONE model (`ds4-pro`) offered by SEVERAL vendors, of which a mint picks
    exactly one; this table is MANY models behind ONE vendor, every one of them
    mapped at once on its own claude-side alias, each in its own provider block
    with its OWN credential entry. Reading the two through one accessor is what
    would let a future edit collapse this family's blocks back onto a shared
    api-key entry — which is measured to take the whole family dark on one
    model's 429 (see the openrouter entry).
    """
    rows = fam.get("model_providers")
    return rows if isinstance(rows, dict) else {}


def family_route_aliases(fam):
    """Every claude-side alias this family's proxy serves as a SEAT runtime.

    `fam["model"]` for every family in the table, PLUS one alias per declared
    `model_providers` row. Deduped, declaration order, the family default
    first — the same ordering law `proxy_routes` keeps, so a caller can read
    the two side by side.

    This is deliberately NOT `family_catalogued_models`. That reading answers
    "which MODEL IDS may a tier or an instance name", and its three sources
    (launch model, probe models, model_context keys) are upstream ids checked
    against upstream ids. This one answers "which ALIAS may appear on a
    provider block's model row", which is the claude side of the same wire.
    Folding them together would make one function answer two questions, and
    the tier validator would start accepting an alias as an upstream id.
    """
    aliases = [fam.get("model")]
    for row in family_model_providers(fam).values():
        if isinstance(row, dict):
            aliases.append(row.get("alias"))
    return tuple(dict.fromkeys(a for a in aliases if a))


def family_block_alias(fam, provider):
    """The alias the provider block NAMED `provider` is expected to serve.

    `fam["model"]` for every family whose blocks all serve one alias, and the
    declared alias for a per-model family. A provider name this family does
    not declare answers with the family default, which matches no route and so
    leaves the block unselected and its bytes untouched — the right answer for
    a block helm has no custody over.
    """
    row = family_model_providers(fam).get(provider)
    if isinstance(row, dict) and row.get("alias"):
        return row["alias"]
    return fam.get("model")


def family_default_provider(fam):
    """The provider block that carries this family's DEFAULT alias, else None.

    Only that block is given CC's built-in subagent frontmatter ids. Giving
    them to every block would put five providers in front of one alias, and
    the proxy would then pool credentials across them — re-creating, for
    subagent traffic, exactly the shared-cooldown failure the per-model split
    exists to end.
    """
    for name, row in family_model_providers(fam).items():
        if isinstance(row, dict) and row.get("alias") == fam.get("model"):
            return name
    return None


def _priced(value):
    """Is this /models pricing field a NON-ZERO price? Unreadable counts.

    THE STRING IS THE VENDOR'S, NOT OURS. OpenRouter writes "0" for free and
    "0.00000009" for nearly free, and `float()` is the only reading that makes
    those comparable — a string compare would call "0.0" priced and
    "0.00000009" a different spelling of nothing. A value that will not parse
    is treated as PRICED, because an unreadable price on a funded key is not a
    reason to spend the owner's money hoping.
    """
    if value is None:
        return False          # the field is absent: the vendor charges nothing
    try:
        return float(value) != 0.0
    except (TypeError, ValueError):
        return True


def model_provider_error(family, fam):
    """Why this family's ``model_providers`` table cannot be served, else None.

    IMPORT-TIME, beside the tier and instance gates, because every failure
    here is one a later reader pays for: a row with a non-zero price bills the
    owner's FUNDED key, a row that cannot say what the vendor does with the
    prompt sends private source to a training set, and a duplicate alias makes
    two blocks answer one name so the proxy pools their credentials.

    THE PRICE ARM IS THE DECLARED RECEIPT, NOT THE LIVE VENDOR. This function
    is pure and runs at import; `zero_cost_refusal` is the live half and runs
    at `seat up`. Both exist because they fail differently: a typo in the
    table is caught here, before anything starts, and a model that CHANGED its
    price upstream is caught there, with the vendor's own numbers. Neither
    substitutes for the other.
    """
    rows = fam.get("model_providers")
    if rows is None:
        return None
    if not isinstance(rows, dict) or not rows:
        return ("%s declares model_providers that is not a non-empty mapping "
                "of provider-block name -> model row" % family)
    if fam.get("mode") != "proxy-key":
        # Only `_add_proxy_key` and the proxy-key half of `proxy_config_plan`
        # emit an openai-compatibility block at all, so a table declared on
        # any other mode is a route that is silently never written.
        return ("%s declares model_providers on mode %r: only a proxy-key "
                "family writes openai-compatibility provider blocks, so a "
                "table here would never reach a config" % (family, fam.get("mode")))
    if fam.get("pool_providers"):
        return ("%s declares BOTH pool_providers and model_providers — one "
                "model across many vendors and many models behind one vendor "
                "are different shapes, and a family claiming both leaves "
                "`proxy_routes` to guess which alias a block serves" % family)
    seen_aliases = {}
    default_rows = []
    disqualified = fam.get("disqualified_models") or {}
    for name, row in rows.items():
        where = "%s model_providers[%r]" % (family, name)
        if not isinstance(name, str) or not name or name.strip() != name:
            return "%s: the provider-block name must be an exact string" % where
        if not isinstance(row, dict):
            return "%s: the row must be a mapping" % where
        unknown = sorted(set(row) - MODEL_PROVIDER_KEYS)
        if unknown:
            return ("%s declares %s, which no reader of this table consults — "
                    "an unapplied key is a route that silently never happened"
                    % (where, ", ".join(unknown)))
        alias, upstream = row.get("alias"), row.get("upstream_model")
        if not alias or not isinstance(alias, str):
            return "%s: the row must declare an alias" % where
        if not upstream or not isinstance(upstream, str):
            return "%s: the row must declare an upstream_model" % where
        if alias in seen_aliases:
            return ("%s and %s both serve the alias %s — two provider blocks "
                    "answering one alias is what makes the proxy pool their "
                    "credentials, which is the shared-cooldown failure this "
                    "table exists to end"
                    % (where, seen_aliases[alias], alias))
        seen_aliases[alias] = where
        if upstream in disqualified:
            return ("%s maps %s, which this family's disqualified_models "
                    "refuses: %s" % (where, upstream, disqualified[upstream]))
        pricing = row.get("pricing")
        if not isinstance(pricing, dict) or not pricing.get("read"):
            return ("%s: pricing must be a record carrying the vendor's own "
                    "prompt and completion numbers and the date they were "
                    "read — a mapped model with no price receipt is an "
                    "unbounded charge on a FUNDED key" % where)
        for field in ("prompt", "completion"):
            if field not in pricing:
                return ("%s: pricing records no %s — BOTH fields decide, "
                        "because a model free on prompt and paid on "
                        "completion still bills" % (where, field))
            if _priced(pricing.get(field)):
                return ("%s prices %s at %r — the key behind this family is "
                        "the owner's FUNDED account, so a non-zero price on "
                        "EITHER field is real money"
                        % (where, field, pricing.get(field)))
        terms = row.get("terms")
        if not isinstance(terms, dict) or not terms.get("read") \
                or not terms.get("by"):
            return ("%s: terms must be a record carrying the verdict, WHO "
                    "read the vendor's data policy and WHEN. Checking price "
                    "is not checking terms: two free models were free because "
                    "THE PROMPTS WERE THE PRICE, and no pricing field says so"
                    % where)
        if terms.get("verdict") != DATA_TERMS_SAFE:
            return ("%s records terms.verdict %r, not %r — a lane that reads "
                    "the owner's private repositories may map only a model "
                    "whose data terms were read and found safe"
                    % (where, terms.get("verdict"), DATA_TERMS_SAFE))
        if row.get("default"):
            default_rows.append(name)
    if fam.get("model") not in seen_aliases:
        return ("%s declares model %s, which no model_providers row serves — "
                "the family default must be a route, or every seat launches "
                "on an alias the proxy has never heard of"
                % (family, fam.get("model")))
    if len(default_rows) != 1 or \
            rows[default_rows[0]].get("alias") != fam.get("model"):
        return ("%s must mark exactly one model_providers row `default` and it "
                "must be the one serving %s (marked: %s)"
                % (family, fam.get("model"), ", ".join(default_rows) or "none"))
    fallback = fam.get("model_fallback")
    if not fallback:
        return ("%s declares model_providers with no model_fallback — these "
                "are free PREVIEW ids, and a family that hard-fails on a "
                "vanished one takes a seat down on a vendor's schedule"
                % family)
    if fallback not in seen_aliases:
        return ("%s names model_fallback %s, which no model_providers row "
                "serves — a fallback that is not a route is a hard fail with "
                "a friendlier name" % (family, fallback))
    if fallback == fam.get("model"):
        return ("%s names its own default %s as model_fallback — the fallback "
                "exists for the case where THAT model is gone" % (family, fallback))
    return None


def family_degraded_model(fam, listing):
    """The alias a seat of this family should LAUNCH on, given a live listing.

    The family default, unless the vendor has stopped serving the model behind
    it — then the declared `model_fallback`. These are free PREVIEW ids with
    lifetimes in days (union-alpha was advertised free only until Sep 22), and
    a family that hard-fails on a vanished one hands the vendor the power to
    take a seat down on its own schedule.

    THE SEAT DEGRADES, NOT THE CONFIG, and that is deliberate. A config edited
    from a listing would be reverted inside the minute: `seat doctor --ensure`
    regenerates every proxy config from the CATALOG on a */3 cron, which is
    the same law that puts instance launch models in this table rather than on
    disk. So the routes stay exactly as declared — the vanished one simply
    stops answering — and what moves is which alias the pane is launched on.

    An unreadable listing changes nothing: with no measurement there is no
    vanished model, and the default stands.
    """
    default = fam.get("model")
    if not isinstance(listing, dict):
        return default
    for row in family_model_providers(fam).values():
        if isinstance(row, dict) and row.get("alias") == default:
            if row.get("upstream_model") in listing:
                return default
            return fam.get("model_fallback") or default
    return default


def zero_cost_refusal(family, fam, listing):
    """The live half of the money guard: why this family must not start, plus
    the degradations it should announce, read from the vendor's OWN listing.

    Returns ``(refusal_or_None, notes)``. `listing` maps model id -> the
    vendor's model record (``{"pricing": {...}, "supported_parameters": [...]}``),
    or is None when the endpoint could not be reached.

    THE THREE OUTCOMES ARE NOT ONE OUTCOME, and collapsing them is how this
    guard would do harm:

      PRICED -> REFUSE. A mapped model whose own vendor now charges on prompt
      or completion is a standing charge on the owner's FUNDED account. This is
      the only money failure mode in the family and it is the only hard stop.

      NO TOOLS -> REFUSE. `supported_parameters` without `tools` is a model
      that cannot call a tool, measured from the listing rather than recalled
      from a table (z-ai/glm-5.2:free reads exactly this way). A seat on it
      looks alive and can never do the work.

      ABSENT -> DEGRADE, NEVER REFUSE. These ids are free previews with
      lifetimes in days. An id the listing no longer carries is the vendor
      retiring a preview, not an error in this config, and refusing on it
      hands a vendor the power to take a seat down on its own schedule.

    UNREACHABLE IS NOT A VERDICT. With `listing` None this function refuses
    nothing and says so: missing evidence is not evidence against, and a seat
    that cannot start because a probe timed out is a worse failure than the one
    being prevented. The declared receipts already had to pass
    `model_provider_error` at import, so the floor under this is a table that
    was checked — just not checked TODAY, which is what the note says.
    """
    rows = family_model_providers(fam)
    if not rows:
        return None, ()
    if listing is None:
        reads = sorted({(row.get("pricing") or {}).get("read")
                        for row in rows.values()
                        if isinstance(row, dict)} - {None})
        return None, ("%s: the vendor's /models listing was not reachable, so "
                      "no price was re-checked this start; standing on the "
                      "declared receipts (read %s)"
                      % (family, ", ".join(reads) or "undated"),)
    notes = []
    for name, row in rows.items():
        if not isinstance(row, dict):
            continue
        upstream = row.get("upstream_model")
        served = listing.get(upstream)
        if served is None:
            note = ("%s: %s (%s) is no longer served by the vendor's own "
                    "listing — a free PREVIEW id was retired, which is not a "
                    "reason to refuse the start. The alias %s now answers "
                    "nothing."
                    % (family, upstream, name, row.get("alias")))
            if row.get("alias") == fam.get("model"):
                note += (" THIS IS THE FAMILY DEFAULT: launch new seats on %s "
                         "instead (`helm seat launch %s --model %s`); the "
                         "declared routes are left as they are, because "
                         "`seat doctor --ensure` regenerates every config "
                         "from the catalog on a */3 cron and would revert a "
                         "listing-derived edit inside the minute."
                         % (fam.get("model_fallback"), family,
                            fam.get("model_fallback")))
            notes.append(note)
            continue
        pricing = served.get("pricing") or {}
        for field in ("prompt", "completion"):
            if _priced(pricing.get(field)):
                return ("%s: %s (%s) now prices %s at %s on the vendor's own "
                        "/models listing. The key behind this family is the "
                        "owner's FUNDED account, so starting it would bill "
                        "real money — the seat was NOT started and the "
                        "running config was left untouched. Drop the route or "
                        "re-verify the price."
                        % (family, upstream, name, field,
                           pricing.get(field)), tuple(notes))
        supported = served.get("supported_parameters")
        if isinstance(supported, (list, tuple)) and "tools" not in supported:
            return ("%s: %s (%s) declares no `tools` in supported_parameters "
                    "— a seat on a model that cannot call a tool looks alive "
                    "and can never do the work. The seat was NOT started."
                    % (family, upstream, name), tuple(notes))
    return None, tuple(notes)


def subagent_tier_model(fam, alias):
    """The model a subagent whose frontmatter carries ``alias`` routes to.

    None when this family declares no tier for that id, and None is not a
    defect: the caller's default — the seat's own launch model — then stands,
    which is byte-for-byte what every family emitted before the table existed.
    """
    return (fam.get("subagent_tiers") or {}).get(alias)


def instance_launch_model(fam, seat=None):
    """The model THIS INSTANCE's pane launches on — its declared
    `instance_models` entry, else the family model.

    ONE FAMILY, TWO MODELS ACROSS PANES, and it is the sibling of the
    `subagent_tiers` table: that one splits models WITHIN a pane by subagent
    id, this one splits them BETWEEN panes. The owner's shape: "we may want
    to consider using sol agents as the standard and only 1 astra for
    planning/landing" — so most codex instances launch gpt-5.6-sol and one
    stays gpt-6-astra, off ONE family entry and ONE cred pool.

    Takes the ENTRY, never a family name, for the same reason
    `family_catalogued_models` does: a caller holding a test table is answered
    about the table it holds. A seat this family does not declare — every seat
    of every other family, and every codex instance with no row — resolves to
    `fam["model"]`, which is what every caller passed before this table
    existed, so their configs and launch lines stay byte-identical.

    This is a DECLARATION, not a spawn-time choice, and the two do not
    compete: `_persisted_model` (seat_lifecycle) still carries an operator's
    explicit `--model` for the pane and outranks this on the launch line. Only
    a declaration can reach the generator, because `seat doctor --ensure`
    regenerates every instance config from the catalog on a */3 cron and a
    hand edit is gone inside a minute.
    """
    return (fam.get("instance_models") or {}).get(seat) or fam.get("model")


def instance_model_error(family, fam):
    """Why this family's ``instance_models`` table cannot be served, else None.

    THE SAME PROMISE `subagent_tier_error` KEEPS, one level up: a launch model
    the family does not catalogue mints a seat whose every request the
    upstream answers 502, and the reader of that failure would be whoever is
    holding the pane rather than whoever typed the table. So it is refused at
    IMPORT, against `family_catalogued_models` — the one reading of "a model
    this family serves" that the routing refusal, the tier validator and
    proxywatch's same-family name proof all already share.

    THE KEY MUST BE AN INSTANCE NAME THIS FAMILY CAN OWN (`codex`, or
    `codex-<N>` — the spelling `seat_lifecycle_sessions._seat_family` resolves
    back to this family), because a key nothing resolves is a declaration that
    is silently never applied, and a silently ignored launch model is the
    failure this whole lane exists to end.
    """
    declared = fam.get("instance_models")
    if not declared:
        return None
    if fam.get("mode") != "proxy":
        # Only an OAuth-pool family mints PER-INSTANCE proxies
        # (seat_launch_assets._mint_instance_proxy skips every other mode): a
        # proxy-key family bakes one key into ONE family config, so a second
        # launch model here would be half-applied — on the launch line, never
        # in the config the seat's subagents route through.
        return ("%s declares instance_models on mode %r: only an OAuth proxy "
                "family mints a per-instance proxy config (see "
                "_mint_instance_proxy)" % (family, fam.get("mode")))
    catalogued = family_catalogued_models(fam)
    for seat, model in declared.items():
        base, _, tail = str(seat).rpartition("-")
        if seat != family and not (base == family and tail.isdigit()):
            return ("%s declares an instance model for %r, which is not an "
                    "instance of this family (%s or %s-<N>)"
                    % (family, seat, family, family))
        if model not in catalogued:
            return ("%s instance %s -> %s names a model this family does not "
                    "catalogue (catalogued: %s)"
                    % (family, seat, model, ", ".join(catalogued)))
    return None


def subagent_tier_error(family, fam):
    """Why this family's ``subagent_tiers`` table cannot be served, else None.

    THE TABLE IS A ROUTING PROMISE AND THE CATALOG IS WHAT CAN KEEP IT. A tier
    naming a model the family does not catalogue mints an alias row the proxy
    accepts happily and the upstream answers 502 — the exact defect task/1948
    cured, re-introduced one level down, and invisible until somebody's
    subagent dies mid-task. So the declaration is refused at IMPORT, where the
    reader is whoever typed it, rather than at the first Explore child.
    """
    tiers = fam.get("subagent_tiers")
    if not tiers:
        return None
    if fam.get("mode") != "proxy":
        # Only the OAuth generator (seat_launch_assets._frontmatter_alias_yaml)
        # emits a per-id model. A key-backed family's rows come from
        # _frontmatter_models_yaml, which has ONE upstream for every id, so a
        # table declared here would be silently ignored — and a silently
        # ignored routing table is worse than this refusal, because the seat
        # then looks configured for a burst it cannot do.
        return ("%s declares subagent_tiers on mode %r: only an OAuth proxy "
                "family emits a per-id model (extend _frontmatter_models_yaml "
                "before declaring one here)" % (family, fam.get("mode")))
    catalogued = family_catalogued_models(fam)
    for alias, model in tiers.items():
        if alias not in CC_AGENT_FRONTMATTER_MODELS:
            return ("%s declares a subagent tier for %s, which is not a "
                    "catalogued frontmatter id" % (family, alias))
        if model not in catalogued:
            return ("%s subagent tier %s -> %s names a model this family does "
                    "not catalogue (catalogued: %s)"
                    % (family, alias, model, ", ".join(catalogued)))
    return None


def proxy_runtime_model_error(family, model):
    """Why ``model`` is routing-only on a proxy family, else ``None``."""
    fam = FAMILIES.get(family) or {}
    if fam.get("mode") not in PROXY_MODES \
            or model not in CC_AGENT_FRONTMATTER_MODELS:
        return None
    catalogued = family_catalogued_models(fam)
    return ("%s is a subagent alias on this family, not a seat runtime — a "
            "seat launched on it would route but could never attest (task/1952). "
            "Catalogued: %s" % (model, ", ".join(catalogued)))


#: OAuth channels the fork's oauth-model-alias supports (config.go).
OAUTH_ALIAS_CHANNELS = ("vertex", "aistudio", "antigravity", "claude", "codex",
                        "kimi", "xai")

#: The two METERED GROUPS on the one Antigravity credential. A quota group is
#: the unit the VENDOR resets, not the unit helm seats: this account bills its
#: Gemini models against one weekly/5-hour allowance and its Claude and GPT
#: models against a second, and one has been measured exhausted while the
#: other was untouched. Families that name the same group share a wall and
#: share a bar; families in different groups are independent even on one
#: credential, which is the whole reason the antigravity seats are three
#: families and not one with fallbacks.
ANTIGRAVITY_GEMINI_GROUP = "antigravity-gemini"
ANTIGRAVITY_CLAUDE_GPT_GROUP = "antigravity-claude-gpt"

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


def local_pool_provider():
    """The name the qwen27 family's one pool row goes by: this host's own
    (`qwen27-provider` in its local names, helm/localnames.py), else the
    neutral `local-llamacpp`.

    A NAME, NOT A CHOICE. The row's key is what the mint writes into the
    proxy config's provider block and what every proxy route and proof binds,
    so a host that minted its seat under a name must keep reading that name.
    It is read once, at import, because it keys the table below; an edit
    takes effect on the next process. A local-names file that does not read
    configures nothing (`helm doctor` names it), and the neutral name then
    matches no live block, so the reconcile reports the block as not its own
    and never rewrites it."""
    from . import localnames
    return localnames.value("qwen27-provider") or "local-llamacpp"


_LOCAL_POOL = local_pool_provider()

# Presets as data (the addendum's table). Three modes: "proxy" (OAuth cred
# translated into CLIProxyAPI, e.g. codex), "proxy-key" (an API-key provider
# behind the same proxy via its openai-compatibility block, e.g. kimi), and
# "first-party" (Anthropic-compatible endpoint, no proxy — future glm/deepseek:
# only mode+base_url+key_env needed). OAuth entries carry ``auth_type``: the
# canonical non-secret ``type`` written into their proxy auth records. Runtime
# family proof matches that measured provider metadata plus the live model; the
# family key and seat label never participate.
FAMILIES = {
    "codex": {"port": 8317, "model": "gpt-6-astra", "mode": "proxy",
              "auth_type": "codex",
              # MEASURED 2026-09-09: CC 2.1.266 Artifact schema 400s at OpenAI (task/1941).
              "schema_unsafe_tools": SCHEMA_UNSAFE_TOOLS,
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
              # THE DEFAULT MODEL'S INPUT CEILING: gpt-6-astra, 272k total − 32k
              # output − 20k reserve = 220k (derivation at model_context below).
              # LOWERED from 320000 on 2026-09-09 when the default rotated from
              # sol to astra; sol's 320000 record below stays true of SOL and is
              # now keyed by sol's id in model_context, so an explicit sol seat
              # keeps its window and autocompact and the launch line agree.
              "max_context": 220000,
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
              "probe_models": ("gpt-6-astra", "gpt-5.6-sol"),
              # SEATS: one astra for planning and landing, sol everywhere
              # else. OWNER: "we may want to consider using sol agents as the
              # standard and only 1 astra for planning/landing" (canon
              # sol-is-the-codex-standard-one-astra-plans-and-lands). The
              # family `model` above stays astra — it is the DEFAULT, and
              # codex-7 keeps it by declaring nothing — while each instance
              # named here launches on the model beside it. One family entry,
              # one OAuth cred pool, two models across panes; every key is an
              # instance of this family and every value a model it catalogues,
              # or instance_model_error refuses the table at import.
              #
              # The four named here are the review and build seats, which is
              # the population the owner's "standard" names; the two project
              # seats among them are declared here for that reason and not as
              # a property of their projects. A seat NOT listed (codex, codex-2,
              # codex-3, codex-6, and any instance minted later) resolves to
              # the family model, so this table is the whole diff from today.
              "instance_models": {"codex-4": "gpt-5.6-sol",
                                  "codex-5": "gpt-5.6-sol",
                                  "codex-8": "gpt-5.6-sol",
                                  "codex-9": "gpt-5.6-sol"},
              # ONE PANE, TWO MODELS — the owner shape: "one
              # codex TLA pane should burst into gpt-5.6-sol WORKERS and
              # gpt-6-astra CHECKERS ... without a second pane". A subagent's
              # model is decided by ITS frontmatter id, so the knob that can
              # say that is this table: per built-in frontmatter id, the model
              # this family's channel should route it to. Workers are the ids
              # CC hands its own agent types (sonnet/haiku) -> sol.
              #
              # THE JUDGEMENT IDS (opus, fable) CARRY NO ROW, AND THE ABSENT
              # ROW IS THE RULE: an id with no tier falls to the caller's
              # default, which is THE SEAT'S OWN LAUNCH MODEL
              # (_frontmatter_alias_yaml: `subagent_tier_model(fam, alias) or
              # model`). Naming gpt-6-astra on these two rows instead is the
              # same answer only while every instance launches on the family
              # model, and the wrong one the moment one does not: a sol seat
              # would escalate its own checkers back to astra, quietly
              # spending the model the owner moved off. "The checkers run what
              # the seat runs" is one sentence and it needs no entry; the
              # entries left here are the WORKERS, which is the only place
              # this family departs from its seat's model.
              #
              # A family with NO table at all maps every id to its launch
              # model, exactly as before (kimi, gemini, grok emit
              # byte-identical configs).
              #
              # EVERY VALUE MUST BE A MODEL THIS FAMILY CATALOGUES and
              # subagent_tier_error refuses the table at import otherwise:
              # both of these are probe_models above, i.e. ids one codex OAuth
              # is measured to serve on its channel (the mixed fan-out run).
              #
              # THE WINDOW A SUBAGENT BELIEVES IS THE PANE'S, NOT ITS OWN, and
              # nothing can change that from here: CLAUDE_CODE_MAX_CONTEXT_TOKENS
              # is process-global (launch_line resolves model_context[launch
              # model] or max_context ONCE) and CC ships no per-subagent
              # window knob. So read the direction, which is what the whole
              # max_context discussion above is about. On the shipped default
              # the pane launches astra and is told 220000; a sol-tier
              # subagent's true ceiling is 320000 (model_context keys it), so
              # it is UNDERSTATED — early compaction, the recoverable
              # direction, and the "when unsure go LOWER" rule already states
              # it. THE REVERSE CASE is a sol pane (told 320000) whose tiers
              # name astra: those subagents would be OVERSTATED by 100k, the
              # unrecoverable direction, and `instance_models` above mints
              # exactly such panes. The condition to hold is therefore stated
              # as a condition, not as a ban on sol seats: THE ADVERTISED
              # WINDOW MUST BE NO LARGER THAN THE model_context OF EVERY MODEL
              # THIS FAMILY ROUTES A CHILD TO — i.e. NO TIER MAY NAME A MODEL
              # WHOSE model_context IS SMALLER THAN THE LAUNCH MODEL'S.
              # (The earlier spelling of this line said LARGER and was exactly
              # backwards: it permitted the sol-pane/astra-tier pane the two
              # sentences above call unrecoverable, and banned the astra-pane/
              # sol-tier shape that ships. A wider CHILD is the safe direction;
              # a NARROWER child is the dangerous one.)
              # The table below keeps it with no minimum-taking in launch_line,
              # because every value in it is gpt-5.6-sol — a sol pane's tiers
              # are its own model, and an astra pane (220000) has only sol
              # tiers ABOVE it (320000), which is the understated, recoverable
              # direction. A tier added on a NARROWER model is what would force
              # launch_line to take the MINIMUM over the launch model and every
              # tier model.
              # Both haiku spellings are workers: the dated id the built-in
              # frontmatter carried and the undated one CC's alias table
              # resolves the word `haiku` to (CC_AGENT_FRONTMATTER_MODELS).
              "subagent_tiers": {"claude-sonnet-5": "gpt-5.6-sol",
                                 "claude-haiku-4-5-20251001": "gpt-5.6-sol",
                                 "claude-haiku-4-5": "gpt-5.6-sol"},
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
              #
              # GPT-6 ASTRA IS 220000, and it is LOWER than sol's 320k on purpose.
              # openai/codex PR #42605 (the v0.153.1 catalog backport) declares
              # astra "context_window": 272000 with "max_context_window": 872000.
              # The family ceiling was derived from sol's 372k total; carrying it
              # onto astra's 272k would OVERSTATE by 48k — the exact direction
              # that wedged this family before. Same arithmetic as spark:
              #     272k total − 32k output − 20k reserve = 220k of input.
              # The 872k figure is a tier the codex CLI catalog names as a MAX,
              # not what our proxy route is measured to serve; it is not assumed
              # here. One bounded >272k probe through the proxy would license
              # raising this — until then, go LOWER (task filed at land).
              "model_context": {"gpt-5.3-codex-spark": 76000,
                                "gpt-6-astra": 220000,
                                "gpt-5.6-sol": 320000}},
    # kimi keys come in two flavors that 401 on each other's endpoint: a
    # CODING-plan key ("sk-kimi-…") wants api.kimi.com/coding/v1 (dual-wire;
    # OpenAI wire live-verified 2026-07-20), a Moonshot PLATFORM key (plain
    # "sk-…") wants api.moonshot.ai/v1 (serves kimi-k3 too — live-verified
    # 2026-07-21). key_base_urls dispatches by key prefix at add time (first
    # match wins); base_url is the no-match default. A mismatched pairing is
    # not a loud failure: the proxy loads the key as an auth, the first call
    # 401s upstream, and CLIProxyAPI quarantines the auth so every later call
    # 503s `auth_unavailable` — hence dispatch-by-shape, not one hardcoded URL.
    # openrouter — ONE KEY, MANY ZERO-COST MODELS, added 2026-09-16 by
    # a helm seat on the owner's instruction ("just spawn them as helm seats").
    # Until this entry existed, helm refused the family and the seats had to be
    # hand-launched beside the substrate, which is the parallel-system mistake
    # compose-dont-parallel names. The proxy maps every alias below, so a new
    # model is an alias plus a seat, never a new port or credential.
    #
    # THE CREDENTIAL IS FUNDED (the owner's Minuscule Ventures OpenRouter key).
    # Every model mapped here is verified zero-cost on BOTH prompt and
    # completion; a non-free id billed through it costs real money. Two free
    # models were disqualified for TRAINING ON SUBMITTED DATA, which the
    # pricing fields do not disclose — read the model page, not just the price.
    #
    # models chosen by measurement, not by hype: the first pass ranked on
    # latency and nearly defaulted the fleet to a health-and-medicine
    # fine-tune that happened to be fast. See the seat's config.yaml for the
    # per-model reasoning and the disqualification list, and
    # seats/openrouter/free-model-bench.py to re-run the ranking — these are
    # free PREVIEW models and the set churns weekly.
    # PORT 8400, NOT 8370, AND THE TABLE HAD ALREADY SAID SO. 8370 is the top
    # of ds4pro's reserved 8350-8370 band, and the gemini entry below carries
    # the identical correction in its own words — "8390, NOT the 8370 the
    # research config happened to pick" — because a scratch config's port was
    # once carried into this table unchecked. The first openrouter entry
    # carried the same 8370 in from a hand-written launcher, and
    # `test_family_ports_unique_with_interleave_headroom` is what said so.
    # Uniqueness is not headroom: 8370 collides with nothing TODAY and still
    # takes the one port two neighbours both reserve. 8400 opens a clean band
    # (8400-8414) below the project-instance block at 8500, and it is the
    # HIGHEST base the table admits — `_project_port_block_is_clear` requires
    # 100 clear below 8500, so a future family takes an unclaimed band inside
    # 8319-8349 rather than climbing past this one.
    "openrouter": {"port": 8400, "model": "or-fast", "mode": "proxy-key",
                   "base_url": "https://openrouter.ai/api/v1",
                   "key_env": "OPENROUTER_API_KEY", "provider": "openrouter",
                   # ONE ACCOUNT, THREE FAMILIES: the free-model caps and the
                   # credit balance are account-wide, so this family, dots3
                   # and ds4flash all read the same vendor meter
                   # (moneyread's openrouter-key reader, task/2936).
                   "money_reader": "openrouter-key",
                   # or-fast = nex-agi/nex-n2.5-pro:free is the room-facing
                   # default of the ONE free lane (one seat, not one alias:
                   # the free tier is a single 20/min account bucket, so a
                   # second seat buys nothing). dots-3-note-preview:free stays
                   # reachable as or-deep for its 512000 window but is not the
                   # default: it drifts into Chinese mid-turn (30 of 265
                   # assistant turns on the live seat carried CJK, measured on
                   # its transcript), which a reviewer the owner reads cannot
                   # do. Both route under provider.data_collection=deny.
                   # 262144 is nex's real window; CC otherwise assumes 200k
                   # for an unknown id.
                   # NOTE it is a REASONING model — at a low max_tokens it
                   # spends the whole budget thinking and returns an EMPTY
                   # answer, which reads exactly like a broken family.
                   "max_context": 262144,
                   # THE PIN'S BACKING, same evidence grade as kimi's above:
                   # openrouter's PUBLIC /api/v1/models, no auth required,
                   # reports context_length=262144 for the default alias's
                   # upstream, nex-agi/nex-n2.5-pro:free, and its top_provider
                   # agrees with max_completion_tokens 235929.
                   # helm's no-guessed-window guard refuses this entry
                   # without the probe recorded, which is the correct
                   # refusal: a measured number asserted bare is
                   # indistinguishable from a guess.
                   #
                   # THE `:free` SUFFIX IS PART OF THE ID. The bare
                   # `nex-agi/nex-n2.5-pro` is ABSENT from the public listing;
                   # only the `:free` spelling is served. A recording that
                   # drops the suffix keeps the right number beside the wrong
                   # id, which is the exact class `zero_cost_refusal` below
                   # exists to make unrepresentable — on this vendor a suffix
                   # IS the price (poolside/laguna-s-2.1 prices
                   # 0.00000009/0.00000018 while poolside/laguna-s-2.1:free
                   # prices 0/0; z-ai/glm-5.2 is 0.0000014/0.0000044 against
                   # a free twin). A dropped suffix bills the owner's funded
                   # account and nothing in the config would say so.
                   "probed_context_length": 512000,
                   # ONE PROVIDER BLOCK PER MODEL, AND IT IS NOT ds4pro's
                   # SHAPE. `pool_providers` is ONE model across several
                   # vendors, picked one per mint; this is MANY models behind
                   # ONE vendor, ALL mapped at once, each on its own alias.
                   #
                   # AND IT IS A CREDENTIAL-ISOLATION INVARIANT, not a
                   # formatting choice. MEASURED on this family's own seat:
                   # CLIProxyAPI cools a CREDENTIAL, not a model. With every
                   # model under one api-key entry a 429 on ONE free preview
                   # benched the shared credential and every OTHER model began
                   # refusing locally with "no available credential, 1 cooling
                   # down" while that same model answered upstream in 0.3s on
                   # a direct curl — one rate-limited model took the family
                   # dark, and the local refusal was byte-indistinguishable
                   # from a vendor wall. Splitting to one block per model,
                   # each with its OWN api-key entry (the same key; the
                   # cooldown is per ENTRY), recovered four of five lanes
                   # instantly including the cooled one. The generator emits
                   # that split and `credential_isolation_reason` refuses a
                   # config that has collapsed back.
                   #
                   # THE SPLIT IS ISOLATION, NEVER CAPACITY, and reading it as
                   # capacity is the mistake this paragraph exists to stop.
                   # OpenRouter's free-model limits are PER ACCOUNT AND
                   # GLOBAL — their own docs, verbatim: "Making additional
                   # accounts or API keys will not affect your rate limits, as
                   # we govern capacity globally." 20 REQUESTS PER MINUTE and
                   # 1000 PER DAY, measured against the funded tier this
                   # family's key belongs to. A Claude Code turn spends several
                   # requests, so two or three lanes working at once saturate
                   # the bucket and everything 429s together — which is what
                   # the owner saw when he prompted every lane at once. More
                   # mapped models do NOT add throughput; they add CHOICE and
                   # a fallback. A pacing cap for this family is still owed
                   # (task/2643): WORKFLOW_AGENT_CAP is one global constant
                   # keyed on mode, so a per-family cap is a change to that
                   # constant's shape and not a value this entry can declare.
                   #
                   # EVERY ROW CARRIES ITS OWN EVIDENCE, and the two kinds do
                   # not substitute for each other:
                   #   `pricing`  the vendor's own /models numbers, prompt AND
                   #              completion, with the date they were read.
                   #              Re-checked live at `seat up`.
                   #   `terms`    what the vendor does with the SUBMITTED
                   #              PROMPT. NOT derivable from any pricing
                   #              field, so it is declared, attributed and
                   #              dated. Checking price is not checking terms.
                   "model_providers": {
                       "openrouter-nex": {
                           "alias": "or-fast", "default": True,
                           "upstream_model": "nex-agi/nex-n2.5-pro:free",
                           "pricing": {"prompt": "0", "completion": "0",
                                       "read": "2026-09-16"},
                           "terms": {"verdict": "private-code-safe",
                                     "read": "2026-09-16",
                                     "by": "meta-claude",
                                     "why": "routes under provider."
                                            "data_collection=deny (Nex AGI), "
                                            "so no training endpoint serves "
                                            "it; measured, not read"},
                           "probed_context_length": 262144,
                           "probed_max_completion_tokens": 235929},
                       "openrouter-cohere": {
                           "alias": "or-code",
                           "upstream_model": "cohere/north-mini-code:free",
                           "pricing": {"prompt": "0", "completion": "0",
                                       "read": "2026-09-16"},
                           "terms": {"verdict": "private-code-safe",
                                     "read": "2026-09-16",
                                     "by": "meta-claude",
                                     "why": "routes under provider."
                                            "data_collection=deny (Cohere), "
                                            "so no training endpoint serves "
                                            "it; measured, not read"},
                           "probed_context_length": 256000,
                           "probed_max_completion_tokens": 64000},
                       "openrouter-dots": {
                           "alias": "or-deep",
                           "upstream_model":
                               "dots-studio/dots-3-note-preview:free",
                           "pricing": {"prompt": "0", "completion": "0",
                                       "read": "2026-09-16"},
                           "terms": {"verdict": "private-code-safe",
                                     "read": "2026-09-16",
                                     "by": "meta-claude",
                                     "why": "routes under provider."
                                            "data_collection=deny (AtlasCloud), "
                                            "so no training endpoint serves "
                                            "it; measured, not read"},
                           "probed_context_length": 512000,
                           "probed_max_completion_tokens": 460800},
                   },
                   # WHERE THE DEFAULT ALIAS GOES WHEN ITS MODEL VANISHES.
                   # These are free PREVIEW ids with lifetimes in days, and a
                   # family that hard-fails on a vanished one takes a seat
                   # down on a vendor's schedule. So an id the live listing no
                   # longer carries DEGRADES: the default alias is served by
                   # this route instead, loudly, and `seat up` still starts.
                   # or-code is the fallback because it is the only other
                   # mapped route whose vendor is an established shop rather
                   # than a preview-only label, and because a review lane's
                   # second-choice model should still be a CODE model.
                   "model_fallback": "or-code",
                   # REFUSED IDS, WITH THE REASON THAT REFUSED THEM, so the
                   # next reader cannot re-add one off a latency table. The
                   # guard reads this table against the config's OWN model
                   # rows, because the generator preserves a block it does not
                   # own byte-for-byte — a hand-added route to one of these
                   # would otherwise survive every regeneration silently.
                   #
                   # SPELLED AS THE LISTING SPELLS THEM. The first recording
                   # of this set named `liquid/lfm-2.5-2.6b` and
                   # `poolside/laguna-s-2.1`; NEITHER id is served — the
                   # served ids carry `:free`, and a table naming ids the
                   # vendor does not list blocks nothing. Both spellings are
                   # carried now, because the paid twin is its own hazard.
                   "disqualified_models": {
                       "nvidia/nemotron-3.5-lightning:free":
                           "TRAINS ON INPUTS, MEASURED: with "
                           "provider.data_collection=deny in the request it "
                           "refuses with 'No endpoints found matching your "
                           "data policy (Free model training)', so its only "
                           "endpoint is a training endpoint. Disqualifying "
                           "for a lane that reads private repositories, "
                           "whatever its 1M window is worth.",
                       "liquid/lfm-2.5-2.6b:free":
                           "TRAINS ON SUBMITTED DATA. For a lane reading the "
                           "owner's private repositories that is "
                           "disqualifying at any benchmark score, and nothing "
                           "in the pricing fields discloses it.",
                       "liquid/lfm-2.5-2.6b":
                           "the non-free twin of a trains-on-submitted-data "
                           "model, and ABSENT from the public listing "
                           "(2026-09-16) — mapping it cannot work and must "
                           "not be attempted.",
                       "poolside/laguna-s-2.1:free":
                           "TRAINS ON SUBMITTED DATA. Same class as liquid: "
                           "the prompts are the price.",
                       "poolside/laguna-s-2.1":
                           "BILLS. prompt 0.00000009 / completion 0.00000018 "
                           "on the owner's FUNDED key (read 2026-09-16), and "
                           "one character from the free id.",
                       "stealth/union-alpha":
                           "PRIVATE-CODE-UNSAFE: its retention terms "
                           "contradict themselves between OpenRouter and "
                           "OpenCode, so no reading of them is safe for a "
                           "lane that sees private repositories. Also "
                           "advertised free only until Sep 22, which is the "
                           "churn hazard as well as the terms one. It was "
                           "the hand-written config's default; it is not a "
                           "route helm will generate.",
                       "openrouter/free":
                           "ROUTES TO A RANDOM FREE MODEL PER CALL, which "
                           "makes the reviewing family unknowable and "
                           "silently voids the cross-family guarantee a "
                           "review lane exists to provide.",
                       "z-ai/glm-5.2:free":
                           "NO TOOL CALLING and a 32768 cap — the listing's "
                           "supported_parameters carries no `tools` "
                           "(2026-09-16). A seat that cannot call a tool is "
                           "a chat toy, not a reviewer.",
                       "z-ai/glm-5.2":
                           "BILLS. prompt 0.0000014 / completion 0.0000044 "
                           "(2026-09-16), and the free twin is already "
                           "refused for having no tool calling.",
                       "thinkingmachines/inkling-small":
                           "BILLS (0.00000045 / 0.0000012, 2026-09-16). Its "
                           "FREE twin is reachable on price and still "
                           "unusable: the API refuses with 'only available "
                           "on agentic harnesses', an allowlist of listed "
                           "apps that our own HTTP-Referer and X-Title "
                           "cannot satisfy — measured, not inferred.",
                       "thinkingmachines/inkling-small:free":
                           "LISTED-APP ALLOWLIST. Measured refusal: 'only "
                           "available on agentic harnesses. Try plugging it "
                           "into a coding agent or productivity app listed "
                           "on openrouter.ai/apps' — still refused with our "
                           "own headers set. A benchmark table (SWE-bench "
                           "Verified 80.2) cannot tell you a model is "
                           "unreachable; only a call can.",
                       "inclusionai/ling-3.0-flash-sante:free":
                           "A HEALTH-AND-MEDICINE fine-tune that happens to "
                           "be fast (0.8s). It was nearly seated as the "
                           "fleet's code reviewer off a latency ranking. Its "
                           "siblings are the same trap: -fin is finance, -vl "
                           "is vision-language.",
                   },
                   # THE OUTPUT CAP IS A SAFETY KNOB ON THIS FAMILY, NOT A
                   # PREFERENCE. These are REASONING models that spend before
                   # answering. MEASURED on nex-n2.5-pro through
                   # the proxy: at max_tokens 1200 it returned NOTHING — 1200
                   # completion tokens consumed, ZERO text blocks, the whole
                   # budget eaten by reasoning — and at 4000 it returned
                   # correct code using 2241. A seat with a low output cap
                   # gets silent empty responses that look exactly like a
                   # broken family.
                   #
                   # 32000 IS DERIVED FROM BOTH ENDS, not chosen. FLOOR: the
                   # cap must be several times the reasoning spend, not merely
                   # above the answer — 4000 was enough for one small task and
                   # is not headroom for a work turn. CEILING: the SMALLEST
                   # top_provider.max_completion_tokens across the mapped set
                   # is 64000 (cohere/north-mini-code:free, and every row
                   # below records its own probed ceiling beside it), so
                   # 32000 sits under every mapped model's own ceiling with
                   # room. It is also the 32k this table already assumes
                   # elsewhere (see ds4pro's context shave), so the family
                   # does not introduce a second number for one idea.
                   # CLAUDE_CODE_MAX_OUTPUT_TOKENS is present in the shipped
                   # binary (2.1.273, `strings -a`) — the same evidence grade
                   # the MAX_CONTEXT_TOKENS note beside the launch line uses.
                   "max_output_tokens": 32000,
                   },
    # dots3 = dots-studio/dots-3-note-preview:free, THE SECOND FREE FINDINGS
    # LANE (task/2805). It rides the SAME OpenRouter key as the family above
    # and is a family of its own anyway, for three reasons that are each
    # measured rather than preferred.
    #
    # THE NAME IS THE MODEL. The owner's rule, stated at the qwen27 mint and
    # restated here: one model, one seat identity, and the identity SAYS THE
    # MODEL. `or-deep` and `or-dots` — the two spellings this lane replaces —
    # name a slot in an alias space; neither says which model answers, and a
    # reviewer's family is the one fact a cross-family verdict rests on. So
    # the family key, the seat name and the claude-side alias are all `dots3`
    # and all three spell the model.
    #
    # WHY NOT A SECOND SEAT ON THE openrouter FAMILY. Not a preference — the
    # shipped gates refuse it, twice over. `instance_model_error` refuses
    # `instance_models` on any mode but "proxy" ("a proxy-key family bakes one
    # key into ONE family config"), so a second openrouter pane could not
    # carry a different model in the config its own subagents route through;
    # and the instance-name grammar admits only `<family>` and `<family>-<N>`,
    # so the only name such a seat could have is `openrouter-2`, which says
    # nothing about which model it is. A family entry is the one shape that
    # gives a second free lane its own config, its own port and its own name.
    #
    # AND THE SPLIT IS CREDENTIAL ISOLATION, THE SAME INVARIANT THE FAMILY
    # ABOVE MEASURED: CLIProxyAPI cools a CREDENTIAL ENTRY, not a model, so a
    # 429 on one lane's entry leaves the other lane's entry answering. Two
    # families, two configs, two entries.
    #
    # WHAT IT DOES NOT BUY, SO NOBODY READS IT AS THROUGHPUT. OpenRouter's
    # free limits are per ACCOUNT and GLOBAL, and the owner's credit top-up
    # moved ONE of the two numbers: the daily cap (50 -> 1000 requests, the
    # vendor's published threshold at 10 USD purchased). THE PER-MINUTE
    # CAP DID NOT MOVE — it is still 20/min shared across every key and every
    # lane — so two free lanes prompted at the same instant still 429 together,
    # exactly as the family above records the owner seeing. What the top-up
    # bought is a DAY that two bursty findings lanes can both fit inside.
    #
    # 8316: THIS FAMILY SERVES BELOW THE CODEX BASE, because the region above
    # it is fully claimed. Every port from codex's 8317 up to the highest base
    # this table admits belongs to somebody: a family's base, the headroom
    # band drawn around one, a socket some other seat on the serving host
    # already holds, or the run codex's own base+N derivation grows into —
    # and that derivation has no upper bound short of the project block, so
    # no port above 8317 is ever outside it. Nothing derives DOWNWARD: base+N
    # only adds, no band reaches under 8317, and a proxy-key family mints no
    # numbered instances (`_instance_port`'s gate), so it needs its base port
    # and no band around it. A port under the codex base is therefore the one
    # place a single-port family can sit without spending headroom some other
    # row was promised.
    #
    # THE ENGLISH-ONLY LINE IS THE CURE FOR THIS MODEL'S ONE MEASURED DEFECT.
    # The family above records why this model is reachable there and not the
    # default: it drifts into Chinese mid-turn — 30 of 265 assistant turns on
    # the live seat carried CJK, counted on that seat's own transcript — and a
    # reviewer the owner cannot read is not a reviewer. `system_line` is the
    # lever helm holds over a seat whose request body is Claude Code's:
    # --append-system-prompt is an ARGUMENT, so it reaches the model where a
    # body parameter (the lever qwen27's entry wanted and could not have)
    # does not.
    #
    # THE ROSTER GHOST THIS NAME RETIRES, AND WHY THE ROSTER KEEPS IT.
    # `or-dots` is a rostered seat with NO seat directory (orca-adopted), and
    # DEAF for a day. It is not a row anything will collect: `helm chat seat
    # gc` reads it and answers KEEP, naming a transcript that still exists
    # for its dead session — the collector failing CLOSED on a transcript,
    # correctly, and forever. So the row outlives the lane by design and the
    # only door that moves it is `helm chat seat rename`, which is a live
    # fleet write and belongs to whoever mints the seat, not to this table.
    # What the table can do is stop the name from being worth reusing: the
    # ghost holds NOTHING (measured at the mint: zero dispatch rows name it;
    # its 139 pending is #helm room backlog of the same order every absent
    # #helm seat carries — kimi 192, another project's codex seat 138 — and not owed work),
    # so nothing is lost by retiring the word, and keeping it would leave one
    # model answering to three names.
    #
    # THE FALLBACK IS SCHEDULED, NOT THEORETICAL. The public listing carries
    # an `expiration_date` for this id — the vendor has ANNOUNCED the day
    # this route stops existing, which is the churn hazard the family above
    # describes in the abstract, with a date on it. The row's `pricing.read`
    # is when that listing was read; the expiry is weeks, not months, after
    # it. north-mini-code
    # is the fallback for the same reason or-code is over there: an established
    # shop rather than a preview-only label, and still a CODE model. Its window
    # is smaller than this family's, so `model_context` keys it; without that
    # a degraded seat would be launched claiming 512000 and wedge at 256000.
    "dots3": {"port": 8316, "model": "dots3", "mode": "proxy-key",
              "base_url": "https://openrouter.ai/api/v1",
              "key_env": "OPENROUTER_API_KEY", "provider": "openrouter",
              # the openrouter family's account and so its meter
              "money_reader": "openrouter-key",
              # 512000 is the window the vendor's PUBLIC /api/v1/models
              # reports for this id (no auth required, re-read at the mint),
              # and its sole endpoint (AtlasCloud) reports the same — the same
              # evidence grade kimi's and the openrouter family's pins carry.
              "max_context": 512000,
              "probed_context_length": 512000,
              # THE DEGRADED LAUNCH GETS THE FALLBACK MODEL'S WINDOW, not this
              # family's: cohere/north-mini-code:free reports 256000.
              "model_context": {"north-mini-code": 256000},
              # ASCII, AND SHORT ON PURPOSE. It is quoted into launch.sh and
              # read by a model that drifts, so it says the one thing it is
              # for and names the reason rather than scolding.
              "system_line": ("Write every word of every turn in English. "
                              "Your findings are read by the repository "
                              "owner, who reads English; a review written in "
                              "another language cannot be read by the person "
                              "it is for."),
              "model_providers": {
                  "dots3-atlascloud": {
                      "alias": "dots3", "default": True,
                      "upstream_model":
                          "dots-studio/dots-3-note-preview:free",
                      "pricing": {"prompt": "0", "completion": "0",
                                  "read": "2026-09-18"},
                      # THE TERMS RECORD KEEPS ITS ORIGINAL PROVENANCE. This
                      # is the original measurement, carried across from
                      # the openrouter entry that mapped the same id with its
                      # own `read` date intact — NOT re-stamped, because the
                      # price was re-read today and the data policy was not.
                      "terms": {"verdict": "private-code-safe",
                                "read": "2026-09-16",
                                "by": "meta-claude",
                                "why": "routes under provider."
                                       "data_collection=deny (AtlasCloud), "
                                       "so no training endpoint serves it; "
                                       "measured, not read"},
                      "probed_context_length": 512000,
                      "probed_max_completion_tokens": 460800},
                  "north-mini-code-cohere": {
                      "alias": "north-mini-code",
                      "upstream_model": "cohere/north-mini-code:free",
                      "pricing": {"prompt": "0", "completion": "0",
                                  "read": "2026-09-18"},
                      "terms": {"verdict": "private-code-safe",
                                "read": "2026-09-16",
                                "by": "meta-claude",
                                "why": "routes under provider."
                                       "data_collection=deny (Cohere), so no "
                                       "training endpoint serves it; "
                                       "measured, not read"},
                      "probed_context_length": 256000,
                      "probed_max_completion_tokens": 64000},
              },
              "model_fallback": "north-mini-code",
              # THE CANDIDATE THIS LANE WAS ASKED TO JUDGE, AND WHY IT LOST.
              # Both spellings, per the paired-spelling law the family above
              # states: on this vendor a suffix IS the price.
              "disqualified_models": {
                  "nvidia/nemotron-3-ultra-550b-a55b:free":
                      "ONE ENDPOINT, AND IT IS NVIDIA'S OWN FREE TIER "
                      "(measured off the public /models/<id>/endpoints "
                      "listing, 2026-09-18: a single endpoint, provider "
                      "Nvidia). That is the SAME vendor and the same tier "
                      "whose sibling nemotron-3.5-lightning:free was measured "
                      "refusing under provider.data_collection=deny with 'No "
                      "endpoints found matching your data policy (Free model "
                      "training)'. With one endpoint there is nothing to fall "
                      "back to, so no terms record short of a live probe "
                      "under deny can say private-code-safe for a lane that "
                      "reads the owner's private repositories — and absence "
                      "of a training claim is not a finding.",
                  "nvidia/nemotron-3-ultra-550b-a55b":
                      "BILLS. prompt 0.000000625 / completion 0.000003125 "
                      "read off the public listing 2026-09-18, one suffix "
                      "away from the free id, on the owner's FUNDED key.",
                  # The id the task named does not exist. Recorded because a
                  # table naming ids the vendor does not list blocks nothing,
                  # and because the next reader will type the short name too.
                  "nvidia/nemotron-3-ultra-550b:free":
                      "NOT A SERVED ID (public listing, 2026-09-18): the "
                      "served spellings carry the -a55b architecture segment. "
                      "This is the name task/2805 proposed, and a route to it "
                      "cannot work.",
                  "openrouter/free":
                      "ROUTES TO A RANDOM FREE MODEL PER CALL, which makes "
                      "the reviewing family unknowable and silently voids the "
                      "cross-family guarantee a review lane exists to "
                      "provide. Refused here as well as on the family above, "
                      "because a hand-added route is refused per CONFIG and "
                      "this family has its own.",
              },
              # The same safety knob, the same derivation, one number: above
              # the measured 4000 floor with room, under the smallest mapped
              # ceiling (64000, north-mini-code).
              "max_output_tokens": 32000,
              "probe_models": ("dots3",)},
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
             # autocompact track the true window (the owner saw kimi being
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
             # THE WINDOW ABOVE IS WHAT K3 CAN HOLD; THE BUDGET BELOW IS WHAT
             # HELM LETS THE SEAT HOLD (task/2944, see context_budget_error).
             # TWO OWNER GOALS MEET HERE, AND THIS NUMBER KEEPS BOTH.
             #  * WHY 1M WAS SET: the owner's complaint above. Taught nothing,
             #    CC assumes its 200k non-claude default and compacts at 160k,
             #    which was about 5x too often for a 1M model.
             #  * WHY THE BUDGET NARROWS IT: allowance. The weekly allowance
             #    went in about 30 h (about 3,817 requests, 1.34B input tokens),
             #    almost all from this seat. That cost came from LONG-LIVED
             #    sessions: one session carried many rows, so a typical request
             #    carried 329k tokens (p90 712k), and every request re-sends
             #    the whole context.
             # The between-rows rung (fresh_session_floor below) drops the
             # carried rows, so the budget only has to stop ONE runaway row.
             # It is sized from the p95 PER-ROW PEAK, KIMI_ROW_PEAK_P95 at the
             # end of this module: 300,373 tokens over 174 rows, replayed under
             # that rung. 380000 x 80% = 304,000, so about 95% of rows finish
             # without compacting. A 200k budget would compact about 1 row in 5
             # (80.5% of rows finish), which is the owner's complaint again.
             "context_budget": 380000,
             # An idle seat with no owed row and no claim, carrying more than
             # this, starts a FRESH session before its next row (autocompact's
             # between-rows rung). This seat's sessions ended their first turn
             # at 34k-47k in three of four sessions, so 100k leaves room above
             # an onboarding. The fourth started into a flooded inbox and
             # ended its first turn at 124k; if an onboarding lands above the
             # floor, the rung's spacing (one clear per seat per 15 minutes)
             # bounds the cost.
             "fresh_session_floor": 100000,
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
    # opencode-go = deepseek-v4-pro (LIVE, HTTP 200), deepseek-direct (native) =
    # deepseek-v4-pro (LIVE after the owner funded direct API credit on
    # 2026-08-20), openrouter = deepseek/deepseek-v4-pro (authstore key dead). So the
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
                       # THE RUNG IS DECLARED SO A SURFACE NEVER HAS TO GUESS
                       # IT. This one is the subscription the owner already
                       # pays flat, so a turn on it costs nothing further.
                       "rung": "free",
                       "authstore": "opencode-go"},
                   "deepseek": {
                       "proxy_provider": "deepseek-direct",
                       "base_url": "https://api.deepseek.com/v1",
                       "upstream_model": "deepseek-v4-pro",
                       "rung": "paid",
                       "authstore": "deepseek"},
                   # THE FLASH ROUTE IS NOT IN THIS POOL, and its absence is
                   # the cure rather than an omission. It lived here, serving
                   # this family's alias on a DIFFERENT and weaker model, so a
                   # proof measured on it resolved to family `ds4pro` and
                   # carried this family's approval identity -- the identity
                   # collapse the owner named: a fallback that "still
                   # identifies as ds4pro" when they should be "actual
                   # different seats/identities". It is now the `ds4flash`
                   # family below, council-only, and the tier refuses it
                   # through the family check that already exists.
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
               # ("cc-assumed-default"). Whether this family's recent
               # CONTEXT_FULL reports are an UPSTREAM 400 or a purely
               # client-side refusal against that 200k is NOT established
               # here and must not be written down as if it were; someone
               # has to look.
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
    # THE WEAK RUNG UNDER ITS OWN NAME. This family exists so that a cheap
    # non-reasoning route can be run WITHOUT wearing a strong family's
    # identity. It is COUNCIL-ONLY by construction and not by a guard: the
    # approval tier admits FAMILIES, and this one is simply not among them, so
    # a verdict minted on this route is refused by the same check that refuses
    # every other outsider -- no second door beside the catalog.
    #
    # WHY IT IS A FAMILY AND NOT A ROW IN ds4pro's POOL: a pool is ONE model
    # offered by SEVERAL vendors (see `family_model_providers`), and this model
    # is not that model. A pool whose rows disagree about the upstream id makes
    # the family unable to say WHICH MODEL ANSWERED, which is the fact every
    # cross-family accounting rests on -- `_pool_serves_one_model` below
    # refuses that shape at import now, so the next weak rung declared under a
    # strong family's name fails here rather than in a review weeks later.
    #
    # 8330 sits in the unclaimed 8319-8349 gap, clear of kimi's 8318 and of
    # ds4pro's 8350-8370 instance band: uniqueness is not headroom.
    # THE CLAUDE-SIDE ALIAS IS SPELLED OUT, and that is not cosmetic. An
    # alias whose first segment were `ds4` would make the word "ds4" an
    # OWNER-STATEMENT ALIAS of two families at once (`_family_owner_aliases`),
    # so a sentence the owner wrote about the pro model could back a pin on
    # the flash one -- the same cross-filing the alias-uniqueness assert in
    # seat.py exists to refuse. `deepseek` is no key's prefix here, so this
    # family answers to its key alone.
    "ds4flash": {"port": 8330, "model": "deepseek-v4-flash",
                 "mode": "proxy-key",
                 "key_env": "DS4FLASH_API_KEY",
                 # its paid rung spends the OpenRouter account's credits, so
                 # it reads that account's prepaid balance
                 "money_reader": "openrouter-key",
                 "pool_default": "openrouter",
                 "pool_providers": {
                     "openrouter": {
                         "base_url": "https://openrouter.ai/api/v1",
                         "upstream_model": "deepseek/deepseek-v4-flash",
                         # PER-TURN METERED MONEY: 0.08/0.16 per M against the
                         # pro id's 1.60/3.20, which is why it exists at all.
                         "rung": "paid",
                         "authstore": "openrouter"},
                 },
                 "probe_models": ("deepseek-v4-flash",)},
    # qwen27 = Qwen3.8-27B-UD-Q4_K_XL (dense 27B, unsloth), served by
    # llama-server on a box on the operator's LAN. THE FIRST LOCAL FAMILY IN THIS TABLE,
    # and that is the only thing new about it: the endpoint is
    # OpenAI-compatible, so it rides the same mode "proxy-key" leg kimi and
    # ds4pro ride, with one difference recorded below (there is no key).
    #
    # THE NAME IS THE MODEL, NOT THE BOX. `qwen27` is the llama-server -a
    # alias and it is also this family's claude-side alias, so the two sides
    # of the wire spell the same id. It is deliberately NOT `qwenlocal`, which
    # is a DIFFERENT model (the 35B on port 8081 of the same box) and not
    # `qwen`, which would make the word an owner-statement alias of both.
    #
    # KEYLESS, AND THAT IS DECLARED RATHER THAN FAKED. The endpoint takes no
    # Authorization header. `keyless` makes the mint write the provider block
    # with NO api-key-entries list at all, which the proxy reads as one
    # credential-less auth for the provider (its config synthesizer creates an
    # entry with no api_key attribute when the list is absent). The rejected
    # alternative was a placeholder literal in key_env: a string that looks
    # like a secret, is not one, and teaches every later reader that this
    # family has a credential to rotate.
    #
    # ONE POOL ROW, LIKE ds4flash. The row exists so the family can DECLARE
    # its cost rung — `provider_rung` reads pool rows and nothing else — and
    # "free" here is literal: the box is the owner's, the model is local, and
    # a turn spends no money at any vendor. A surface that cannot read a rung
    # renders no cost word, which for a free family reads as unknown.
    #
    # 8345, AND THE TABLE'S OWN ADVICE WOULD HAVE COLLIDED. Two neighbouring
    # entries name "the unclaimed 8319-8349 gap" as where the next family
    # belongs. That is true of this TABLE and false of the MACHINE: codex
    # mints numbered instances at base+N, and `helm seat up qwen27` at 8319
    # refused against a live listener — 8319 was held by codex-2, and a
    # socket census found codex holding 8317 and 8319-8327 continuously.
    # Uniqueness in the table is not vacancy on the host, which is the same
    # lesson one rung further out than the one the port test already records.
    # 8345 is what is actually left of that gap: above ds4flash's 8330-8344
    # and below ds4pro's 8350 floor. It is ONE PORT AND NOT A BAND, and that
    # is not a shrink so much as the rest of this comment finally being
    # obeyed: a proxy-key family mints no numbered instances at all (the
    # launch gate refuses them — see `_instance_port`), so it needs its base
    # port and no band of its own. 8346 and 8348 are NOT qwen27's to reserve:
    # opus46 and gptoss serve there, and task/2801 declares both as bases.
    #
    # AND VACANCY WAS STILL DOING THE WORK, one layer down: 8345 is what
    # codex-28 derives. So is 8330 for codex-13, and every other base above
    # codex's, because base+N was bounded only by the project-instance block
    # at 8500 — six declared ports inside one family's derived reach, with the
    # admission door answering None for each. NO port below 8500 was outside
    # that reach, so this entry's number could not be made safe by choosing a
    # different one; the refusal is what makes it safe. `_numbered_port_collision`
    # refuses a numbered instance whose derived port is another family's
    # DECLARED port and names that family, so 8345 is unreachable by derivation
    # rather than merely unused. It is a port and not a band on purpose: the
    # bands in these comments govern where the NEXT base may be declared, and
    # nothing binds the ports between them, so refusing a band would cost codex
    # every numbered seat above codex-12 for no socket. Swept for every family
    # up to the block in
    # `test_no_numbered_instance_derives_another_familys_declared_port`.
    #
    # THE WINDOW IS ONE SLOT, NOT THE SERVED TOTAL, and the slot is read off
    # the endpoint rather than off anyone's description of it. Its /v1/models
    # reports meta.n_ctx for the slot it is serving now — 131072, with
    # n_ctx_train 262144 as the weights' own window. That is recorded as
    # probed_context_length, the same evidence grade kimi's pin carries: an
    # endpoint reporting the window it serves.
    #
    # THE FIRST READING OF THIS FIELD WAS 32768 AND IT WAS NOT WRONG, IT WAS
    # OLD. The unit was re-served between two probes forty minutes apart, and
    # a seat minted against the first number refused its opening turn
    # client-side with "Prompt is too long" — a helm seat's preamble alone
    # (system prompt, tools, hooks, skills, boot brief) measures 37,257 to
    # 40,199 tokens, read off the first-turn usage record of the grok, ds4pro
    # and openrouter seats. So a local family's window is a reading with an
    # expiry, not a constant: RE-READ /v1/models before trusting this pin,
    # and if the slot has shrunk, this number comes down with it.
    #
    # max_context 115072 = 131072 - 16000, the slot less the turn's output
    # budget, because input and output share ONE slot here. Against the
    # measured preamble that leaves roughly 75k for the work itself.
    #
    # THE THINKING BUDGET IS THE OUTPUT CAP, and it is the same trap the
    # openrouter entry records AND the one helm/preread.py already cures on
    # its own leg — `reader_payload` sends chat_template_kwargs
    # enable_thinking false, in so many words, because "a thinking model that
    # exhausts max_tokens inside its reasoning returns EMPTY content with no
    # error, which reads downstream as a clean file". That cure is available
    # to preread because preread AUTHORS the request body. A seat's body is
    # Claude Code's, and nothing between here and the wire may add to it, so
    # on this leg the budget is the whole lever. MEASURED on this endpoint: the response
    # separates `reasoning_content` from `content`, both drawn from the SAME
    # max_tokens, so a budget the reasoning exhausts returns a completion with
    # no text at all — a live family that looks broken. A real code-review
    # prompt spent 370 completion tokens of which ~300 were reasoning. 16000
    # is several times that spend rather than merely above it, and it is the
    # ceiling too: half the slot is as much as output may take before input
    # has nowhere to sit.
    #
    # WHY NOT DISABLE THINKING ON THIS LEG. Both levers work AT THE ENDPOINT
    # (measured: `reasoning_effort: "none"` and
    # `chat_template_kwargs: {"enable_thinking": false}` each return
    # reasoning_content empty, and the same prompt answers in 3s instead of
    # 58s). Neither is reachable from a SEAT: the proxy's
    # openai-compatibility block carries name, base-url, api-key-entries,
    # models, headers and disable-cooling, and no field of it injects a body
    # parameter. The budget is what helm holds here — and the endpoint helps,
    # because it returns reasoning in its own `reasoning_content` field,
    # which the proxy renders as a `thinking` block rather than eating the
    # answer's place.
    "qwen27": {"port": 8345, "model": "qwen27", "mode": "proxy-key",
               "keyless": True,
               "pool_default": _LOCAL_POOL,
               "pool_providers": {
                   _LOCAL_POOL: {
                       # THE HOST IS THE OPERATOR'S OWN BOX, so the catalog
                       # names the key its URL is configured under and holds
                       # no host: unconfigured, the family is unavailable
                       # (`pool_base_url`).
                       "base_url_from": "qwen27",
                       "upstream_model": "qwen27",
                       # local weights on the owner's own box: a turn here
                       # spends no vendor money at all.
                       "rung": "free"},
               },
               "probed_context_length": 131072,
               "max_context": 115072,
               "max_output_tokens": 16000,
               "probe_models": ("qwen27",)},
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
    # distinction is load-bearing. A 70-probe sweep across both proxies (every
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
               # MEASURED 2026-09-10 20:16Z: the Gemini API 400s the Artifact
               # schema on every turn — query.where is prefixItems, and Gemini's
               # validator answers "properties[query].properties[where].items.items:
               # missing field." — so the whole tool list dies per request (task/2142).
               "schema_unsafe_tools": SCHEMA_UNSAFE_TOOLS,
               # -antigravity-login, NOT -gemini-login: this binary
               # (7.2.88-helm.1) has six login entrypoints and DoGeminiLogin is
               # not among them — the gemini/gemini-cli auth types exist with no
               # interactive driver. Antigravity is the only binary-driven
               # Google OAuth, and it meters on an Antigravity credit balance
               # rather than the Gemini app subscription.
               "login_flag": "-antigravity-login",
               "auth_glob": "antigravity-*.json",
               # Antigravity accepts the high-effort route id but its response
               # envelope names the underlying model. This is an EXACT declared
               # projection, not suffix stripping: the selected credential trace
               # still binds the full route and every undeclared response id
               # refuses. The response value cannot be derived from the OAuth
               # config (it carries no models block); it records the model measured
               # in Antigravity's authenticated response envelope.
               "response_models": ({
                   "route": {
                       "alias": "gemini-3.6-flash-high",
                       "provider": "antigravity",
                       "upstream_model": "gemini-3.6-flash-high"},
                   "response_model": "gemini-3.6-flash"},),
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
               # refuse it — correctly. A cross-family read of that arm stands:
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
               # THE GROUP, NOT THE ACCOUNT. This family bills the Gemini
               # allowance; opus46 and gptoss below ride the SAME OAuth file
               # and bill a different one, so a surface that rendered one bar
               # per credential would have shown this seat's exhaustion over
               # two seats that were fully open. Declared here so the pair
               # below can be rendered as one group and this one as another.
               "quota_group": ANTIGRAVITY_GEMINI_GROUP,
               "probe_models": ("gemini-3.6-flash-high",)},
    # opus46 + gptoss ride the SAME antigravity credential gemini rides, and
    # that is the only unusual thing about them. One Google account, one OAuth
    # file, TWO METERED GROUPS: the Gemini models bill one weekly/5-hour
    # allowance and the Claude-and-GPT models bill another. MEASURED at the
    # vendor's own quota page: the Gemini group read 0 percent remaining with
    # four days to its refresh while the Claude-and-GPT group read 100
    # percent, so the gemini SEAT was walled and these two were fully open on
    # the identical credential. That is what makes
    # them worth seating separately rather than as gemini fallbacks: a fallback
    # would change the model under one name, and a review's authority is a
    # claim about WHICH MODEL READ THE DIFF.
    #
    # OWNER EXCEPTION, SCOPED. Claude models elsewhere in this fleet run only
    # through Claude Code on the owner's Max credential; opus46 runs a Claude
    # model through a proxy, which that rule otherwise forbids. The owner
    # opened it for this subscription ("i have no problem using that usage if
    # it's available, it doesnt seem connected to the gemini usage
    # directly"): the allotment is GOOGLE'S, bought with the Antigravity
    # plan, and no Anthropic API key or SDK is involved anywhere in the path.
    # The exception is the credential, not the model name — nothing here
    # licenses an ANTHROPIC_API_KEY.
    #
    # RE-MEASURED before this entry was written, one 8-token completion each
    # through the live proxy on this credential: claude-opus-4-6-thinking 200 in 2.79s (gitleaks:allow — model ids, not a key)
    # (4 completion tokens, content "OK"), claude-sonnet-4-6 200 in 1.94s,
    # gpt-oss-120b-medium 200 in 1.76s (24 completion tokens, 59 characters of
    # reasoning beside the answer). Non-empty text on all three, which is the
    # bar the 70-probe sweep set for the council families above.
    #
    # THE SEAT NAME IS THE MODEL. `opus46` is claude-opus-4-6-thinking and
    # nothing else; it must never be read as gemini (a different group on the
    # same credential) nor as the Max-pool claude seats (a different
    # credential, a different model, and the family this fleet's approval tier
    # already names). `gptoss` is gpt-oss-120b-medium and is a NEW family: no
    # other seat in this tree runs an OpenAI open-weights model, so it is a
    # true cross-family read for both claude- and codex-authored work.
    #
    # THE FAMILY KEY IS WHAT THE TIER READS, and both keys are deliberately
    # absent from route.APPROVAL_TIER. A verdict's recorded family is the
    # seat's family key (dispatches._verdict_author_runtime_evidence puts it in
    # `resolved.family`), so both seats are findings-only the day they are
    # spawned, with no edit to the tier and no chance of one arriving by
    # accident. The resolved MODEL travels in the same envelope
    # (`resolved.model` / `resolved.upstream_model`), which is the evidence
    # task/2802's loosened rule reads when it promotes them.
    #
    # PORTS 8346 AND 8348, AND THE TABLE IS NOW FULL BELOW 8400. The free space
    # was never the 8319-8349 two older comments call unclaimed: a socket
    # census on this host found codex's numbered instances holding 8317 and
    # 8319-8327, ds4flash reserves 8330-8344, and ds4pro's floor is 8350. What
    # was left is 8328-8329 and 8345-8349. 8328-8329 is NOT taken here, because
    # it is the next two numbers codex's own base+N range grows into, and a
    # family that parks there is a collision waiting for codex-11. So both new
    # families come out of 8345-8349 with two ports each, above whatever the
    # local family takes at 8345. The NEXT family in this table cannot be given
    # a band at all without moving PROJECT_PORT_BASE or reclaiming one, and
    # that is a decision, not an oversight — the arm in tests/test_seat.py
    # records the remaining space so the next reader hits a red, not a clash.
    #
    # ONE CREDENTIAL, THREE FAMILIES, THREE COPIES. `shares_credential_with`
    # names gemini as the family whose already-minted antigravity auth file
    # this one adopts at `helm seat add` time (see _add_proxy_oauth): a COPY
    # into this seat's own auth-dir at 0600, never a shared directory. The
    # proxy REWRITES its auth file when the access token refreshes — measured:
    # the gemini seat's antigravity-*.json mtime tracks its own `expired`
    # stamp, one hour apart — so pointing two proxies at one directory would be
    # two writers on one file, and it would also be this lane writing the
    # gemini seat's credential, which its brief forbids.
    #
    # THE THREE COPIES DO NOT INVALIDATE EACH OTHER, and that is measured
    # rather than assumed. All three proxies refreshed within one second of
    # each other on their own files; afterwards each holds a DIFFERENT access
    # token and the SAME refresh token, byte-identical to the one that was
    # there before the copies existed. So this upstream does not rotate the
    # refresh token on use, which is the one thing copy-sharing would not
    # survive. Re-measure if a later seat starts failing to refresh: the
    # symptom of rotation is siblings dying one after another, not at once.
    #
    # A GROUP IS A BUDGET, NOT A LIVENESS ANSWER FOR ITS MEMBERS. Measured on
    # this credential after both seats had worked: gpt-oss-120b-medium
    # returned Google's own "Resource has been exhausted" 429 while
    # claude-opus-4-6-thinking and claude-sonnet-4-6 answered 200 in the same
    # minute on the same OAuth file. The vendor's quota page groups all three,
    # and that grouping is what `quota_group` records; a per-model wall rides
    # on top of it. So a surface may render the group's remaining percent, and
    # must never read a member's reachability off it.
    "opus46": {"port": 8346, "model": "claude-opus-4-6-thinking",
               "mode": "proxy-oauth", "auth_type": "antigravity",
               "login_flag": "-antigravity-login",
               "auth_glob": "antigravity-*.json",
               "shares_credential_with": "gemini",
               "quota_group": ANTIGRAVITY_CLAUDE_GPT_GROUP,
               # A THINKING MODEL DRAWS ITS REASONING FROM THE ANSWER'S
               # BUDGET, which is the trap the openrouter and qwen27 entries
               # both record: too small a cap returns a completion with no
               # text and a live family reads as broken. It did NOT fire at
               # max_tokens 8 in the probe above — the model answered "OK" in
               # 4 tokens — so the failure is not guaranteed at any particular
               # size and a work turn must not be left to find the edge. 32000
               # is the same number kimi's entry derives and sits under the
               # 64000 ceiling the openrouter entry measured as the smallest
               # in its mapped set.
               "max_output_tokens": 32000,
               "probe_models": ("claude-opus-4-6-thinking",)},
    # gptoss = gpt-oss-120b-medium, the OpenAI open-weights model on the same
    # Antigravity allotment. A NEW FAMILY KEY, and the reason to spend one is
    # cross-family independence: the fleet's other reviewers are claude, codex,
    # gemini, grok, kimi, ds4pro and a local qwen, and none of them is this.
    # "medium" is the reasoning-effort rung baked into the served id; the
    # endpoint offers it as one model name, not as a parameter helm can set.
    "gptoss": {"port": 8348, "model": "gpt-oss-120b-medium",
               "mode": "proxy-oauth", "auth_type": "antigravity",
               "login_flag": "-antigravity-login",
               "auth_glob": "antigravity-*.json",
               "shares_credential_with": "gemini",
               "quota_group": ANTIGRAVITY_CLAUDE_GPT_GROUP,
               # THE SAME REASONING-SHARES-THE-BUDGET TRAP, and here it is
               # MEASURED rather than inferred: the 8-token probe came back
               # with 24 completion tokens carrying 59 characters of reasoning
               # beside a two-character answer, so this endpoint spends the
               # cap on reasoning before it spends it on text.
               "max_output_tokens": 32000,
               "probe_models": ("gpt-oss-120b-medium",)},
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


#: The operator's endpoints file under the helm home's global dir: a flat JSON
#: object of key -> base URL. A pool row whose host is the operator's own box
#: names its key in `base_url_from`; the shape is in ENDPOINTS_EXAMPLE.
ENDPOINTS_CONFIG = "endpoints.json"
ENDPOINTS_EXAMPLE = "docs/endpoints-config.example.json"


def pool_base_url(row):
    """(url, None) or ("", why): one pool row's endpoint, read at call time.

    A row with `base_url` carries a public host. A row with `base_url_from`
    carries none: its URL is the value under that key in the endpoints file,
    so the tree names no host on the operator's LAN, and re-pointing it is an
    edit of that file with no restart.

    AN UNCONFIGURED ENDPOINT ANSWERS "" AND NEVER A DEFAULT HOST. The mint
    refuses and names the file and the key, no route can bind a proof, and the
    reconcile has no desired state to rewrite a live config to."""
    if not isinstance(row, dict):
        return "", "the pool row is not a table"
    key = row.get("base_url_from")
    if not key:
        return str(row.get("base_url") or "").rstrip("/"), None
    from . import home
    path, table, why = home.global_json(ENDPOINTS_CONFIG)
    value = (table or {}).get(key)
    if why is None and value is None:
        why = ("the %s endpoint is not configured: add \"%s\": "
               "\"http://<host>:<port>/v1\" to %s (the shape is in %s)"
               % (key, key, path, ENDPOINTS_EXAMPLE))
    elif why is None and not (isinstance(value, str) and value.strip()
                              .startswith(("http://", "https://"))):
        why = ("the %s endpoint in %s is not an http(s) URL: %r"
               % (key, path, value))
    if why:
        return "", why
    return value.strip().rstrip("/"), None


def unconfigured_endpoint(family, table=None):
    """Why one of this family's pool rows has no endpoint, else None."""
    table = FAMILIES if table is None else table
    fam = table.get(family) if isinstance(table, dict) else None
    rows = (fam or {}).get("pool_providers")
    for row in (rows.values() if isinstance(rows, dict) else ()):
        why = pool_base_url(row)[1]
        if why:
            return why
    return None


def proxy_routes(family, table=None):
    """Ordered exact proxy routes declared by one configured family.

    ONE ROUTE PER CATALOGUED MODEL, the default first. A family's
    `probe_models` are the exact ids it is known to serve on its channel, and
    a seat spawned before a default rotation is still running one of them:
    a probe measured sol live on three seats in the hour astra became the
    default (2026-09-09). Attestation compares a seat's LOADED route against
    this tuple, so a default-only tuple would have rejected every honest
    pre-rotation seat as "unknown or ambiguous" and dropped its proxy proof —
    the same P0 that blocked astra seats before task/1941, handed to the
    other half of the fleet. Key-backed families bind alias + provider +
    upstream + endpoint per pool row and probe only their default, so the
    union is a no-op there."""
    table = FAMILIES if table is None else table
    configured = table.get(family) if isinstance(table, dict) else None
    if not isinstance(configured, dict):
        return ()
    alias = configured.get("model")
    if configured.get("mode") == "proxy-key":
        per_model = configured.get("model_providers")
        if isinstance(per_model, dict):
            # ONE ROUTE PER MODEL, THE DEFAULT FIRST, and every route carries
            # its OWN alias. The pool branch below keeps ONE alias across many
            # endpoints because ds4pro is one model several vendors serve;
            # here the alias is what TELLS the routes apart, so a reader that
            # matched on `configured["model"]` would see exactly one of them
            # and call the rest foreign.
            base = str(configured.get("base_url") or "")
            rows = [(name, row) for name, row in per_model.items()
                    if isinstance(row, dict)]
            rows.sort(key=lambda kv: kv[1].get("alias") != alias)
            return tuple({"alias": row.get("alias"),
                          "provider": name,
                          "upstream_model": row.get("upstream_model"),
                          "base_url": str(row.get("base_url") or base).rstrip("/")}
                         for name, row in rows)
        providers = configured.get("pool_providers")
        if isinstance(providers, dict):
            return tuple({"alias": alias,
                          "provider": row.get("proxy_provider") or name,
                          "upstream_model": row.get("upstream_model"),
                          "base_url": pool_base_url(row)[0]}
                         for name, row in providers.items()
                         if isinstance(row, dict))
        urls = [configured.get("base_url")]
        urls.extend(url for _prefix, url in configured.get("key_base_urls", ()))
        # THE FRONTMATTER ALIASES ARE DELIBERATELY NOT ROUTES HERE (task/1952,
        # integrator ruling): they are subagent routing conveniences served by
        # the proxy's models block, never SEAT runtimes — so an explicit
        # `--model claude-opus-5` launch must be refused at the launch door
        # rather than attested as a family route. Adding them here made the
        # measured alias route attributable and taught the wrong lesson:
        # that a seat may run on a subagent id.
        return tuple({"alias": alias, "provider": configured.get("provider"),
                      "upstream_model": configured.get("upstream_model") or alias,
                      "base_url": str(url or "").rstrip("/")} for url in urls)
    if configured.get("mode") in ("proxy", "proxy-oauth"):
        provider = configured.get("auth_type")
        known = (alias,) + tuple(
            m for m in configured.get("probe_models") or () if m != alias)
        return tuple({"alias": m, "provider": provider, "upstream_model": m}
                     for m in known)
    return ()


#: What a `keyless` family writes where a bearer would go, and it is a
#: PLACEHOLDER BY MEASUREMENT rather than by preference. The first cut of this
#: omitted `api-key-entries` entirely, on the reading that CLIProxyAPI's
#: config synthesizer has a "no APIKeyEntries" fallback that creates a
#: credential-less auth. Probed on the running binary (7.2.110-helm.11):
#: with the key absent the proxy logged `0 OpenAI-compat` clients and every
#: request hung until the caller gave up — /v1/models still listed the alias,
#: because the models block is config and needs no auth, so the family looked
#: alive and answered nothing. With ONE entry holding this string the same
#: proxy logged `1 OpenAI-compat` and the same request completed in 3.5s.
#: So the proxy requires an entry and the upstream ignores what is in it.
#: The string is deliberately self-describing: a reader who finds it in a
#: config must be able to tell at a glance that it is not a secret, that
#: nothing rotates it, and that it reaches an endpoint which takes no
#: Authorization header at all.
KEYLESS_API_KEY_PLACEHOLDER = "no-key-required"

#: The two cost rungs a provider block may declare, and nothing else. A rung
#: is a claim about MONEY, so an unreadable or absent value answers None and
#: every surface renders no cost word rather than the cheaper one.
PROVIDER_RUNGS = ("free", "paid")


def provider_rung(family, provider, table=None):
    """The declared cost rung of one provider block, or None when undeclared.

    DELIBERATELY NOT A FIELD OF `proxy_routes`. A route dict is matched by
    EXACT EQUALITY against a proof's route (`proxy_route_family`, and
    `_PROXY_KEY_ROUTE_FIELDS` in proxywatch pins its field set), so a cost
    word added there would not describe a route -- it would stop every
    existing proof from matching one. The rung is a fact about the BLOCK,
    asked for separately by whoever renders it.
    """
    table = FAMILIES if table is None else table
    fam = table.get(family) if isinstance(table, dict) else None
    rows = (fam or {}).get("pool_providers")
    if not isinstance(rows, dict) or not provider:
        return None
    for name, row in rows.items():
        if not isinstance(row, dict):
            continue
        if (row.get("proxy_provider") or name) != provider:
            continue
        rung = row.get("rung")
        return rung if rung in PROVIDER_RUNGS else None
    return None


def proxy_route_family(route, table=None):
    """The unique catalog family declaring one exact proxy route, else why."""
    table = FAMILIES if table is None else table
    matches = sorted(family for family in table
                     if route in proxy_routes(family, table))
    if len(matches) != 1:
        return None, ("proxy route maps to %d configured families%s" %
                      (len(matches), ": " + ", ".join(matches) if matches else ""))
    return matches[0], None


def proxy_route_response_model(family, route, table=None):
    """Exact response model one full declared route may return, else why.

    A response envelope is provider evidence, not a value derivable from every
    proxy config: OAuth configs carry no models block. Each exceptional value is
    therefore typed beside the complete route that measured it. Alias-only keys
    are insufficient because key-backed families can reuse one alias across
    providers, upstream ids and endpoints.
    """
    table = FAMILIES if table is None else table
    configured = table.get(family) if isinstance(table, dict) else None
    routes = proxy_routes(family, table)
    if not isinstance(configured, dict) or route not in routes:
        return None, "proxy response-model projection has no declared route"
    declared = configured.get("response_models", ())
    if not isinstance(declared, tuple):
        return None, "proxy response-model projection is malformed"
    selected, seen = route["upstream_model"], []
    for projection in declared:
        if not isinstance(projection, dict) \
                or set(projection) != {"route", "response_model"}:
            return None, "proxy response-model projection is malformed"
        candidate = projection["route"]
        response = projection["response_model"]
        if not isinstance(candidate, dict) or candidate not in routes \
                or candidate in seen or not isinstance(response, str) \
                or not response or response.strip() != response:
            return None, "proxy response-model projection is malformed"
        seen.append(candidate)
        if candidate == route:
            selected = response
    return selected, None


def _pool_serves_one_model(table=None):
    """The first pool family whose rows disagree about the model, else None.

    A POOL IS ONE MODEL OFFERED BY SEVERAL VENDORS -- `family_model_providers`
    states that contract in prose and nothing enforced it. A pool whose rows
    name two upstream ids cannot answer WHICH MODEL ANSWERED from the family,
    and a proof measured on either row resolves to the same family, so the
    weaker route inherits the stronger name. That is not a style point: family
    is what the approval tier admits, so a cheap rung smuggled into a strong
    family's pool is granted that family's review authority by declaration.

    READ OFF THE TABLE, never a hand-listed set of today's families, so the
    check covers a family that does not exist yet.
    """
    for family, fam in (FAMILIES if table is None else table).items():
        rows = fam.get("pool_providers")
        if not isinstance(rows, dict):
            continue
        models = sorted({row.get("upstream_model") for row in rows.values()
                         if isinstance(row, dict)})
        if len(models) != 1:
            return ("family %s declares a POOL serving %d models (%s): a pool "
                    "is one model across vendors, and a second model here "
                    "would carry this family's identity and its review "
                    "authority" % (family, len(models),
                                   ", ".join(str(m) for m in models)))
    return None


_POOL_MODEL_REFUSAL = _pool_serves_one_model()
assert _POOL_MODEL_REFUSAL is None, _POOL_MODEL_REFUSAL


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


def _credential_sharing_error(table=None):
    """The first family whose `shares_credential_with` cannot mean what it
    says, as the reason text — None when every declaration is coherent.

    THE FIELD MOVES A CREDENTIAL BETWEEN SEAT HOMES, so a typo in it is not a
    stale comment: `_add_proxy_oauth` would copy a file this family cannot
    authenticate with into this family's auth-dir and then report a seat. The
    three conjuncts are exactly the three ways the copy is wrong — the source
    does not exist, the source authenticates a DIFFERENT upstream channel, or
    the two families disagree about which filenames are credentials, so the
    glob would either find nothing or find the wrong file. Import-time, beside
    the port check, because the next reader of a bad value is a seat that
    mints and cannot answer."""
    table = FAMILIES if table is None else table
    for family, fam in table.items():
        source = fam.get("shares_credential_with")
        if source is None:
            continue
        other = table.get(source)
        if other is None:
            return ("family %s shares a credential with %r, which is not a "
                    "family in this table" % (family, source))
        if other.get("auth_type") != fam.get("auth_type"):
            return ("family %s shares a credential with %s, but they "
                    "authenticate different channels (%s vs %s)"
                    % (family, source, fam.get("auth_type"),
                       other.get("auth_type")))
        if other.get("auth_glob") != fam.get("auth_glob"):
            return ("family %s shares a credential with %s, but they disagree "
                    "about which files are credentials (%s vs %s)"
                    % (family, source, fam.get("auth_glob"),
                       other.get("auth_glob")))
    return None


_CREDENTIAL_SHARING_REFUSAL = _credential_sharing_error()
assert _CREDENTIAL_SHARING_REFUSAL is None, _CREDENTIAL_SHARING_REFUSAL


def quota_group(family, table=None):
    """The metered group this family bills, or None when it declares none.

    A family with no group is not "in its own group": it is a family nothing
    has measured a shared wall for, and every surface says so rather than
    inventing a bar of one."""
    table = FAMILIES if table is None else table
    fam = table.get(family) if isinstance(table, dict) else None
    return (fam or {}).get("quota_group") if isinstance(fam, dict) else None


def quota_group_families(group, table=None):
    """Every family billing `group`, sorted — the pair a group bar covers."""
    table = FAMILIES if table is None else table
    if not group or not isinstance(table, dict):
        return ()
    return tuple(sorted(name for name, fam in table.items()
                        if isinstance(fam, dict)
                        and fam.get("quota_group") == group))


#: What a group's remaining percent reads as before anything reads it. The
#: endpoint the Antigravity CLI's own Models-and-Quota page uses is task/2800's
#: subject and is NOT read here, so the only honest rendering is that nobody
#: has measured it. A guess in this slot would be a number an owner acts on.
QUOTA_GROUP_UNMEASURED = "not yet measured"


def quota_group_phrase(family, remaining_pct=None, table=None):
    """`quota group <g> (shared with <f>): <remaining>` — or "" for a family
    that declares no group.

    `remaining_pct` is the group's measured remaining percent when a caller
    has one; with nothing passed the phrase says UNMEASURED and names nothing
    else, because the failure this shape exists to prevent is a seat printing
    a plausible percent it never read."""
    group = quota_group(family, table=table)
    if not group:
        return ""
    others = [name for name in quota_group_families(group, table=table)
              if name != family]
    shared = (" (shared with %s)" % ", ".join(others)) if others else ""
    if remaining_pct is None:
        reading = QUOTA_GROUP_UNMEASURED
    else:
        reading = "%g%% remaining" % remaining_pct
    return "quota group %s%s: %s" % (group, shared, reading)


def _unserveable_subagent_tier():
    """The first declared subagent_tiers table this catalog cannot serve, as
    the reason text — None when every table names only its own family's
    catalogued models. Import-time, beside the port check, because a tier
    typo's next reader would otherwise be a 502 in somebody's subagent."""
    for family, fam in FAMILIES.items():
        reason = subagent_tier_error(family, fam)
        if reason:
            return reason
    return None


_TIER_REFUSAL = _unserveable_subagent_tier()
assert _TIER_REFUSAL is None, _TIER_REFUSAL


def _unserveable_instance_model():
    """The first declared instance_models table this catalog cannot serve, as
    the reason text — None when every table names only its own family's
    instances and its own family's catalogued models. Import-time, beside the
    tier gate, for the same reason: a launch-model typo's next reader would
    otherwise be a 502 on every request the seat makes."""
    for family, fam in FAMILIES.items():
        reason = instance_model_error(family, fam)
        if reason:
            return reason
    return None


_INSTANCE_MODEL_REFUSAL = _unserveable_instance_model()
assert _INSTANCE_MODEL_REFUSAL is None, _INSTANCE_MODEL_REFUSAL


def _unserveable_model_provider():
    """The first declared model_providers table this catalog cannot serve, as
    the reason text — None when every row carries a zero price on both fields,
    a read data-terms verdict, a unique alias, and a declared fallback.

    Import-time, beside the tier and instance gates, and for a harder reason
    than either: the arm this one adds is the only one in the table whose
    failure SPENDS THE OWNER'S MONEY. A price typo committed here would be
    read next by a billing statement."""
    for family, fam in FAMILIES.items():
        reason = model_provider_error(family, fam)
        if reason:
            return reason
    return None


_MODEL_PROVIDER_REFUSAL = _unserveable_model_provider()
assert _MODEL_PROVIDER_REFUSAL is None, _MODEL_PROVIDER_REFUSAL

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
    reading "gem 1m" backs a gemini pin (a live probe against the real
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

    This prefix test is NOT the one a review refuted, and the distinction is
    the whole design: it compares two values the TABLE declares (a model stem
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


# --- a context BUDGET below the window (task/2944) --------------------------
# THE WINDOW AND THE BUDGET ARE DIFFERENT FACTS, AND EACH GETS ITS OWN KEY for
# the reason the four backing grades above do. `max_context` answers "how much
# can this model hold?" and carries the evidence for its number. A budget
# answers "how much does helm let a seat hold?", which is a SPEND decision for a
# family whose allowance is limited. Writing the budget into `max_context` would
# erase the measurement, and the window guard would then treat a policy number
# as the model's capacity.
#
# A BUDGET MAY ONLY NARROW. Understating a window costs an early compaction and
# is recoverable. Overstating one is the unrecoverable 400. So a budget is
# honored only under a pinned window, and a budget at or above that window
# refuses at import: it would read as a cap that is not in force.
#
# The same number reaches both readers through `taught_window`: the launch line
# (CLAUDE_CODE_MAX_CONTEXT_TOKENS and CLAUDE_CODE_AUTO_COMPACT_WINDOW) and the
# autocompact watchdog's gauge. If they disagreed, CC would be told one window
# and the watchdog would compact against another.

def taught_window(fam, window):
    """The window a seat of this family is TAUGHT, given its model's `window`.

    `window` is the model's own (a `model_context` entry or `max_context`).
    The family's `context_budget` narrows it when declared below it, and never
    widens it. A falsy `window` passes through unchanged: an unpinned family is
    already taught CC's 200k default, and a budget has no window to narrow."""
    budget = fam.get("context_budget")
    if window and budget and budget < window:
        return budget
    return window


def context_budget_error(family, fam):
    """'' when this family's context_budget and fresh_session_floor are
    coherent; otherwise the reason, naming the key it is about."""
    budget = fam.get("context_budget")
    window = fam.get("max_context")
    if budget is not None:
        if type(budget) is not int or budget <= 0:
            return ("%s: context_budget must be a positive token count, "
                    "got %r" % (family, budget))
        if not window:
            return ("%s declares context_budget=%d with no max_context: there "
                    "is no pinned window to narrow, and an unpinned family is "
                    "already taught CC's %d" % (family, budget,
                                                 _CC_ASSUMED_WINDOW_MIRROR))
        if budget >= window:
            return ("%s declares context_budget=%d at or above its max_context "
                    "%d: a budget can only narrow, so this one caps nothing"
                    % (family, budget, window))
    floor = fam.get("fresh_session_floor")
    if floor is not None:
        if type(floor) is not int or floor <= 0:
            return ("%s: fresh_session_floor must be a positive token count, "
                    "got %r" % (family, floor))
        taught = taught_window(fam, window) or _CC_ASSUMED_WINDOW_MIRROR
        trigger = taught * int(AUTOCOMPACT_PCT_OVERRIDE) // 100
        if floor >= trigger:
            return ("%s declares fresh_session_floor=%d at or above the %d "
                    "where the watchdog compacts (%s%% of the taught %d): the "
                    "seat compacts first, so the floor is never reached"
                    % (family, floor, trigger, AUTOCOMPACT_PCT_OVERRIDE,
                       taught))
    return ""


def _incoherent_context_budget(table=None):
    """The first family whose budget or fresh-session floor is incoherent, as
    the reason text, or None. `table` lets a test drive a bogus table through
    the real predicate without mutating the live FAMILIES."""
    for family, fam in (FAMILIES if table is None else table).items():
        reason = context_budget_error(family, fam)
        if reason:
            return reason
    return None


_CONTEXT_BUDGET_REFUSAL = _incoherent_context_budget()
assert _CONTEXT_BUDGET_REFUSAL is None, _CONTEXT_BUDGET_REFUSAL


# THE MEASUREMENT kimi's context_budget is sized from. A record, like
# owner_stated_window, so the number travels with its date and its source, and
# an arm pins the budget to it (the budget's compaction point, 80% of it, must
# sit at or above this peak).
#
# HOW IT WAS MEASURED, read-only off the seat's own transcripts (all five
# sessions on disk). A ROW starts at the first beacon event that delivers a
# dispatch row to the seat and ends at the next row. Its peak is the largest
# context of any request in that span. Raw peaks are useless here: they carry
# every earlier row of a long-lived session (p50 418k, p95 762k). So the
# history is REPLAYED under the between-rows rung: at each quiet gap of 5
# minutes or more, with the context at or above the 100k floor and 15 minutes
# since the last clear, the seat restarts at a 70k onboarding (the highest
# onboarding-phase reading). Owed rows cannot be seen in a transcript, so the
# replay lets the rung fire whenever the seat is quiet. Replayed per-row peaks:
# p50 104,766, p90 211,205, p95 300,373, max 600,756; no row needs more than
# 1M / 80%. The row's own growth, measured without the replay, agrees in
# shape: p50 17k, p95 171k.
KIMI_ROW_PEAK_P95 = {
    "tokens": 300373,
    "percentile": 95,
    "rows": 174,
    "measured": "2026-09-23",
    "source": "kimi seat transcripts, dispatch-row spans replayed under the "
              "between-rows rung (task/2944)",
}
