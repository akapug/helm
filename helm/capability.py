#!/usr/bin/env python3
"""capability — the wired-substrate SELF-INDEX (AX meta-gap, owner 2026-07-23).

helm's north-star is "agents get the truth about the substrate they run on,
injected every turn" — but it FAILED ON ITSELF: an agent reasoning about the
per-toolcall whisper meld did NOT reach for helm's OWN wired primitive
(`helm chat deliver`); it had to grep. The store held DOMAIN premises but no
reachable "what helm gives you + it is LIVE-WIRED" index. This is that index.

A wired capability = (verb/tool) + (trigger-context). That maps ONE-to-ONE onto
the JIT store-entry shape: trigger-context -> `keywords` (the probe vocabulary),
verb/what -> the surfaced line. So a capability rides the ONE JIT resolver
(store.resolve_prompt) via inject's `entries=` seam — DF-weighted, specificity-
gated (generic-only never fires), cap-4, per-session cooldown — with NO parallel
injector. When the agent's current reasoning touches a capability's trigger, the
capability surfaces one terse line: "you have <verb>: <what> (wired via <hook>,
live)". The owner never has to say "reach for cv / a meld / polyana now".

live/absent is a PROBE, not a static fact: a POWERPACK only surfaces if actually
wired (its MCP server present, or an explicit HELM_CAP_<id> flag). CORE is helm's
own substrate — always live. VISIBILITY gates the PUBLIC export only: a
private-hold powerpack (polyana) still surfaces to its OWN wired agent (that is
the whole point — deep-code-analysis reasoning should reach for it), but helm
does NOT advertise it in any public powerpack catalog (public_set withholds it).
"""
import os

TIER_CORE = "core"
TIER_POWERPACK = "powerpack"
VIS_PUBLIC = "public"
VIS_HOLD = "private-hold"

# The wired-capability catalog. Each row: the verb the agent runs, the one-line
# what, the HOOK/mechanism that makes it LIVE (wired_via), the trigger-context
# keywords (the probe vocabulary — rare, specific tokens; NO bare generics), the
# tier (core = helm's own substrate, always live; powerpack = an installed
# capability, probed), the visibility (public vs private-hold), and — for a
# powerpack — the `mcp` server name its live-probe checks.
CAPABILITIES = (
    {
        "id": "chat-deliver",
        "verb": "helm chat deliver",
        "tool": "helm chat deliver",
        "what": "push a per-toolcall whisper to the fleet — fires on EVERY "
                "tool call, fleet-wide (the a2a/whisper/meld primitive)",
        "wired_via": "the PostToolUse hook (helm chat)",
        # the owner canon COUPLES these: "the per-toolcall whisper meld = helm
        # chat deliver" — so whisper/meld/converge ARE this verb's trigger
        # context, and it co-fires with `meld` on the a2a-converge moment.
        "keywords": "a2a,whisper,per-toolcall,per toolcall,whisper meld,meld,"
                    # NO bare "converge" — it collides with dregg protocol vocab
                    # ("converge with the finalized root"); a2a/whisper/meld
                    # already carry the real a2a-converge moment.
                    "deliver,broadcast,tell the fleet,push to the fleet,"
                    "nudge the fleet,agent-to-agent,steer another agent,"
                    "inject into another",
        "tier": TIER_CORE,
        "visibility": VIS_PUBLIC,
    },
    {
        "id": "meld",
        "verb": "helm chat meld",
        "tool": "helm chat meld",
        "what": "converge with another model/seat in-turn — independent drafts, "
                "you synthesize the final answer",
        "wired_via": "helm chat meld + the /com council skill",
        "keywords": "meld,a2a,second opinion,another seat,another model,"
                    "second seat,another take,pair with,two models,cross-family,"
                    # the converge MOMENT in the words agents actually use (owner
                    # canon: reach for meld WITHOUT being told). Every token is a
                    # multi-word A2A-SCOPED phrase — NO bare "converge"/"consensus"
                    # (they collide with dregg protocol vocab this fleet debugs
                    # constantly: "consensus root", "converge with the finalized
                    # root" — a relevance regression the cross-family gate caught).
                    "reach consensus,reach agreement,can we agree,"
                    "agree on this,get on the same page,same page,hash this out,"
                    "hash it out,sync up,another perspective,another agent,"
                    "bounce this off,bounce it off,we disagree,resolve the "
                    "disagreement,converge with a peer,converge with another,"
                    "align with a peer",
        "tier": TIER_CORE,
        "visibility": VIS_PUBLIC,
    },
    {
        "id": "council",
        "verb": "/com (council-of-models)",
        "tool": "council-of-models skill",
        "what": "run a multi-model panel beneath one acting agent — proposers "
                "draft independently, you synthesize (Hermes MoA)",
        "wired_via": "the council-of-models skill (/com)",
        "keywords": "council,com,panel of models,multi-model,proposers,moa,"
                    "model diversity,panel,ensemble",
        "tier": TIER_CORE,
        "visibility": VIS_PUBLIC,
    },
    {
        "id": "dispatch",
        "verb": "helm dispatch / lr",
        "tool": "helm dispatch",
        "what": "hand a bounded task to another seat and track it to done "
                "(the durable dispatch ledger + land-request loop)",
        "wired_via": "helm dispatch + lr (the dispatch ledger)",
        "keywords": "dispatch,delegate,hand off,handoff,land request,"
                    "land-request,farm out,assign to,another seat do,"
                    "review loop,track to done",
        "tier": TIER_CORE,
        "visibility": VIS_PUBLIC,
    },
    {
        "id": "pending",
        "verb": "helm chat pending",
        "tool": "helm chat pending",
        "what": "see whether your @mention/DM was SEEN or ACTED on and which "
                "addressees STRANDED (no seat answers) — the SENT/SEEN/ACTED "
                "consume ladder, so a silent reply is never mistaken for lost",
        "wired_via": "helm chat pending + the ack-ladder consume ladder",
        "keywords": "did they see,did they see it,was it seen,did they act,"
                    "did they ack,did they read it,did my message land,did it "
                    "reach,no reply,no response,unanswered,who has not "
                    "responded,waiting on a reply,message stranded,stranded,"
                    "did it get delivered,ghosted",
        "tier": TIER_CORE,
        "visibility": VIS_PUBLIC,
    },
    {
        "id": "work-claims",
        "verb": "helm work claim",
        "tool": "helm work claim",
        "what": "claim a lane/worktree before touching it so two seats never "
                "collide on the same work",
        "wired_via": "helm work (the claims lane + worktree locks)",
        "keywords": "claim a lane,work claim,lane ownership,who owns,collision,"
                    "avoid overlap,double-claim,claim the task,worktree lock",
        "tier": TIER_CORE,
        "visibility": VIS_PUBLIC,
    },
    {
        "id": "store",
        "verb": "helm premise / coach / store",
        "tool": "helm premise",
        "what": "capture a durable premise/heuristic/lexicon that JIT-surfaces "
                "on relevant prompts (this index rides the same lane)",
        "wired_via": "helm store + the inject JIT resolver",
        "keywords": "remember this,from now on,make this a rule,capture a premise,"
                    "durable rule,learn this,standing truth,add to the store,"
                    "never again,canon",
        "tier": TIER_CORE,
        "visibility": VIS_PUBLIC,
    },
    {
        "id": "recall",
        "verb": "cv recall (helm recall)",
        "tool": "mcp__cv__recall",
        "what": "cold semantic search over past agent sessions — 'have we "
                "solved this before / where's the prior art'",
        "wired_via": "clustervision (cv) MCP + the recall skill",
        "keywords": "recall,have we solved,solved this before,prior art,"
                    "prior session,past session,cold recall,already solved,"
                    "seen this before,did we do this,previously,dejavu",
        "tier": TIER_POWERPACK,
        "visibility": VIS_PUBLIC,
        "mcp": "cv",
    },
    {
        "id": "polyana",
        "verb": "polyana (pa_scan / pa_trace / pa_xray)",
        "tool": "mcp__polyana__pa_scan",
        "what": "deep cross-language code analysis — run/trace/step-debug/"
                "bug-scan across 8 languages, findings CONFIRMED by execution",
        "wired_via": "the polyana MCP server",
        "keywords": "polyana,deep code analysis,cross-language,cross language,"
                    "polyglot,step-debug,step through,bug scan,code analysis,"
                    "find impls,goto def,semantic navigation,confirm impl,"
                    "trace across languages",
        "tier": TIER_POWERPACK,
        "visibility": VIS_HOLD,
        "mcp": "polyana",
    },
)


# ---------------------------------------------------------------------------
# live-probe — a powerpack surfaces ONLY when actually wired
# ---------------------------------------------------------------------------

def _flag(cap_id, env):
    """The explicit HELM_CAP_<ID> override (the registered-flag mechanism +
    the deterministic test hook). 1/true/yes/on -> True; 0/false/no/off/'' ->
    False; unset or garbled -> None (defer to the MCP probe)."""
    raw = env.get("HELM_CAP_" + cap_id.upper().replace("-", "_"))
    if raw is None:
        return None
    s = raw.strip().lower()
    if s in ("1", "true", "yes", "on"):
        return True
    if s in ("0", "false", "no", "off", ""):
        return False
    return None


def _effective_mcps():
    """The MCP server NAMES wired for the CURRENT agent: the resolved claude
    home's .claude.json mcpServers ∪ enabled-plugin basenames (envtidy.home_mcps)
    ∪ the runtime cwd's project .mcp.json. Read-only, fail-open ({} on any
    trouble) — an unreadable config never fabricates a live powerpack."""
    names = set()
    try:
        from . import envtidy
        cdir = os.environ.get("CLAUDE_CONFIG_DIR") \
            or os.path.join(os.path.expanduser("~"), ".claude")
        names |= set(envtidy.home_mcps(cdir).get("effective") or ())
    except Exception:
        pass
    try:
        import json
        with open(os.path.join(os.getcwd(), ".mcp.json"), encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            names |= set((data.get("mcpServers") or {}).keys())
    except Exception:
        pass
    return names


def is_wired(cap, env=None, mcps=None):
    """Is this capability LIVE for the current agent? core -> always. powerpack
    -> the explicit flag if set, else its MCP server present in `mcps` (probed
    once per call by the caller; None here means probe now)."""
    env = os.environ if env is None else env
    if cap.get("tier") == TIER_CORE:
        return True
    f = _flag(cap["id"], env)
    if f is not None:
        return f
    server = cap.get("mcp")
    if not server:
        return False
    if mcps is None:
        mcps = _effective_mcps()
    return server in mcps


def _probe_mcps_if_needed(env):
    """One _effective_mcps() call per surfacing pass — and ONLY when a powerpack
    lacks a decisive flag (a pure-core estate reads no config at all, keeping
    the per-turn tax at zero)."""
    need = any(c.get("tier") != TIER_CORE and c.get("mcp")
               and _flag(c["id"], env) is None for c in CAPABILITIES)
    return _effective_mcps() if need else set()


# ---------------------------------------------------------------------------
# entries — the JIT-store shape the ONE resolver already scores
# ---------------------------------------------------------------------------

def _line(cap):
    """The surfaced line body — the owner's exact template."""
    return "you have %s: %s (wired via %s, live)" % (
        cap["verb"], cap["what"], cap["wired_via"])


def _entry(cap):
    """A capability as a JIT-store entry: the SAME fields resolve_prompt scores
    (type/id/keywords/confidence/last_updated), so it rides the one JIT lane
    unmodified. confidence 1.0 by construction — a wired capability is a fact,
    not a belief. last_updated '' keeps it timestamp-neutral in the tiebreak."""
    return {"type": "capability", "id": cap["id"], "statement": _line(cap),
            "keywords": cap["keywords"], "confidence": 1.0,
            "class": "capability", "load_class": "jit", "status": "live",
            "domain": "capability", "verb": cap["verb"], "what": cap["what"],
            "wired_via": cap["wired_via"], "tier": cap["tier"],
            "visibility": cap["visibility"], "tool": cap.get("tool", ""),
            "pinned": False, "scope": "capability", "root": "capability",
            "last_updated": ""}


def live_entries(project=None, env=None):
    """The WIRED capabilities as JIT entries for the inject resolver — every
    capability whose live-probe passes. Visibility is NOT a gate here: a
    private-hold powerpack (polyana) still surfaces to its OWN wired agent (the
    deep-code-analysis reasoning moment must reach for it); the hold bars only
    the PUBLIC export (public_set). Fail-open per row: a raising probe drops
    that one capability, never the lane."""
    env = os.environ if env is None else env
    mcps = _probe_mcps_if_needed(env)
    out = []
    for cap in CAPABILITIES:
        try:
            if is_wired(cap, env=env, mcps=mcps):
                out.append(_entry(cap))
        except Exception:
            continue
    return out


# ---------------------------------------------------------------------------
# browse — the full self-index (helm capabilities)
# ---------------------------------------------------------------------------

def all_capabilities(env=None):
    """Every capability with its computed live/absent — the owner's OWN
    self-index (helm capabilities) sees everything, private-hold rows tagged."""
    env = os.environ if env is None else env
    mcps = _probe_mcps_if_needed(env)
    return [dict(cap, live=is_wired(cap, env=env, mcps=mcps))
            for cap in CAPABILITIES]


def public_set(env=None):
    """The SHAREABLE capability set: public-visibility only. private-hold
    (polyana) is withheld — helm does not advertise it in any public powerpack
    catalog (owner packaging note, 2026-07-23)."""
    return [c for c in all_capabilities(env=env) if c.get("visibility") == VIS_PUBLIC]


# ---------------------------------------------------------------------------
# CLI — `helm capabilities` (the on-demand "what does helm give me?")
# ---------------------------------------------------------------------------

def _render(rows):
    """The grouped browse: CORE then POWERPACK, each row a live/absent dot +
    verb + what, its wired-via, and a [hold] tag on the private ones."""
    lines = ["helm capabilities — what helm gives you (● live · ○ absent)", ""]
    for tier, label in ((TIER_CORE, "CORE"), (TIER_POWERPACK, "POWERPACK")):
        group = [c for c in rows if c.get("tier") == tier]
        if not group:
            continue
        lines.append(label)
        for c in group:
            dot = "●" if c.get("live") else "○"
            hold = "  [hold: private]" if c.get("visibility") == VIS_HOLD else ""
            lines.append("  %s %s — %s%s" % (dot, c["verb"], c["what"], hold))
            state = "live" if c.get("live") else "absent"
            lines.append("      wired via %s  ·  %s" % (c["wired_via"], state))
        lines.append("")
    return "\n".join(lines).rstrip()


def cmd_capabilities(args):
    """capabilities [--public] [--json] — the wired-substrate SELF-INDEX: what
    helm gives you, grouped core vs powerpack, each with wired-via + live/absent.
    --public shows only the shareable set (private-hold powerpacks withheld).
    The SAME index JIT-surfaces one line at the reasoning moment via inject."""
    import json
    import sys
    from .cli import guard_tail
    rc = guard_tail("helm capabilities", args, flags=("--public", "--json"),
                    usage="capabilities [--public] [--json]")
    if rc is not None:
        return rc
    rows = public_set() if "--public" in args else all_capabilities()
    if "--json" in args:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return 0
    print(_render(rows))
    return 0
