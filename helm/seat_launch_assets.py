"""Launch configuration and asset minting for :mod:`helm.seat`."""
import glob
import json
import math
import os
import re
import shlex
import stat
import sys
import time


def proxy_debug():
    """The proxy's `debug:` value — "false" unless HELM_PROXY_DEBUG says so.

    DIAGNOSTIC ONLY, and off by default on purpose: a debug proxy logs request
    and response shapes, and these configs front sessions carrying the owner's
    work. It is opt-in per re-mint, never sticky.

    WHY IT IS A KNOB AND NOT A CONSTANT. `debug: false` was written into both
    generators with no override, so the fleet has spent a week trying to
    diagnose a silent token-drop with logging structurally impossible to turn
    on — every "fix round" was blind, which is a plausible reason none of them
    stuck. The stock proxy's gin log carries no model names at debug:false
    (see `_smoke_multi_leg`), so the one artifact that could distinguish an
    upstream drop from a proxy swallow was the one artifact nobody could
    produce. A constant standing where an operator knob belongs is invisible
    until the day the knob is needed.
    """
    # case-FOLDED: "False" and "NO" are the spellings an operator actually
    # types, and a case-sensitive list turns both of them into ENABLE — a knob
    # that does the opposite of what it was told, on the value most likely to
    # be typed by someone trying to turn it off.
    return "true" if os.environ.get("HELM_PROXY_DEBUG", "").strip().lower() \
        not in ("", "0", "false", "no", "off") else "false"


def _config_yaml(port, auth_dir, token):
    """The proxy config that passed the live eval, verbatim shape. The
    nonstream-keepalive-interval is NOT optional: a long non-streaming pass
    (compaction's ~360k summarize — the longest single request a session
    makes) sits silent while the upstream thinks, the proxy reaps the idle
    socket, and Claude Code gets an empty HTTP 200 ('proxy or gateway
    intercepting') — owner-witnessed on a codex-2 /compact 2026-07-22. The
    live family configs carry 15s by hand; the generator must emit it too or
    every re-mint silently strips the fix (as-prevented)."""
    return ('host: "127.0.0.1"\n'
            "port: %d\n"
            'auth-dir: "%s"\n'
            "api-keys:\n"
            '  - "%s"\n'
            "debug: %s\n"
            "usage-statistics-enabled: false\n"
            "remote-management:\n"
            "  allow-remote: false\n"
            '  secret-key: ""\n'
            "  disable-control-panel: true\n"
            # heartbeat during long non-streaming thinking passes — see docstring.
            "nonstream-keepalive-interval: 15\n"
            # transient-error benching (the 503-storm as-prevented): on ANY
            # transient upstream error (408/500/502/503/504) the proxy benches
            # the credential for transientErrorCooldown — and the fork's
            # DEFAULT is 60s, with config value 0 MEANING that default
            # (conductor.go:89,149-152 — a footgun, 0 does not disable). A
            # codex seat runs a tiny 2-cred pool: one blip benches a cred a
            # full minute, both bench in a window -> len(available)==0 -> 503
            # "no available client" born at auth-selection. That is NOT quota:
            # real quota exhaustion is a clean 429 with its own cooldown,
            # untouched by this knob (transient-only). 5s recovers a blipped
            # cred 12x faster; -1 would never bench (too permissive on a
            # genuinely sick upstream). Omit this line and every re-mint
            # silently restores the 60s footgun.
            "transient-error-cooldown-seconds: 5\n"
            # the STREAMING leg too (owner-witnessed 2026-07-22: with the
            # nonstream keepalive already loaded, EVERY request at ~90% context
            # still died empty-200 — the stream stalls before/during bytes at
            # extreme payload sizes). keepalive-seconds emits SSE heartbeats so
            # a long stream stays alive; bootstrap-retries retries a stream
            # that stalls before its first byte. StreamingConfig has ONLY these
            # two knobs — no upstream/read timeout field exists in the schema.
            "streaming:\n"
            "  keepalive-seconds: 15\n"
            "  bootstrap-retries: 2\n") % (port, auth_dir, token, proxy_debug())


def _config_yaml_key(port, token, provider, base_url, model, api_key,
                     upstream=None):
    """The proxy-key config: same inbound head (the per-seat token claude
    presents), no auth-dir (no OAuth cred), plus the openai-compatibility
    provider block carrying the outbound API key (0600 via _write_private —
    the same trust level as the seat token beside it). `upstream` is the
    provider-side model id when it differs from the claude-side alias
    (ds4pro: alias ds4-pro -> deepseek/deepseek-v4-pro); default: same id
    both sides (kimi)."""
    return ('host: "127.0.0.1"\n'
            "port: %d\n"
            "api-keys:\n"
            '  - "%s"\n'
            "debug: %s\n"
            "usage-statistics-enabled: false\n"
            "remote-management:\n"
            "  allow-remote: false\n"
            '  secret-key: ""\n'
            "  disable-control-panel: true\n"
            "openai-compatibility:\n"
            '  - name: "%s"\n'
            '    base-url: "%s"\n'
            "    api-key-entries:\n"
            '      - api-key: "%s"\n'
            "    models:\n"
            '      - name: "%s"\n'
            '        alias: "%s"\n'
            # same long-nonstream keepalive as _config_yaml (compaction survival)
            "nonstream-keepalive-interval: 15\n"
            # same transient-bench shortening as _config_yaml (503-storm
            # as-prevented; 0 means the 60s default — see _config_yaml)
            "transient-error-cooldown-seconds: 5\n"
            "streaming:\n"
            "  keepalive-seconds: 15\n"
            "  bootstrap-retries: 2\n"
            # proxy_debug() is THIRD, not appended: %-args bind by position in
            # the template and `debug:` sits above the openai-compatibility
            # block. Appending it would have silently shifted provider ->
            # base-url -> api-key by one and written a config that parses.
            % (port, token, proxy_debug(), provider, base_url, api_key,
               upstream or model, model))


def _key_base_url(fam, api_key):
    """The outbound base-url for THIS key: some providers mint key flavors
    bound to different endpoints (kimi coding-plan "sk-kimi-…" vs Moonshot
    platform "sk-…"), and the wrong pairing 401s upstream — which CLIProxyAPI
    answers by quarantining the auth (every later call 503s auth_unavailable).
    Shared by every proxy-key family: an optional key_base_urls tuple of
    (prefix, url) pairs dispatches by key shape, first match wins; families
    without it (or with an unmatched key) keep fam["base_url"]."""
    for prefix, url in fam.get("key_base_urls", ()):
        if api_key.startswith(prefix):
            return url
    return fam["base_url"]


def _instance_dir(family, seat):
    """An instance's isolated config root: seat != family (codex-2, codex-3…)
    lives under instances/<seat>; instance 1 keeps the family dir (back-compat)."""
    return os.path.join(seat_dir(family), "instances", seat) \
        if seat and seat != family else seat_dir(family)


_SEAT_SURFACE_REFUSED = object()


def _seat_surface_error(family, seat, path=None):
    """None only when the requested seat path resolves to its literal identity.

    Containment under seats_root is not ownership: `seats/kimi -> seats/codex`
    stays inside the root while making a kimi-targeted writer mutate codex. Build
    the expected path beneath the REAL root without resolving its family/instance
    components, then require the actual path to resolve to those exact bytes.
    Any symlink component below the root changes the identity and is refused.

    `path` also binds a caller-supplied mutation target to that identity; a writer
    cannot prove one seat and then write through a different directory.
    """
    expected = os.path.abspath(_instance_dir(family, seat))
    if path is not None and os.path.abspath(path) != expected:
        return ("refusing %s surface %s — the requested mutation target is %s"
                % (seat, path, expected))
    root = os.path.realpath(seats_root())
    rel = os.path.relpath(expected, os.path.abspath(seats_root()))
    literal = os.path.normpath(os.path.join(root, rel))
    actual = os.path.realpath(expected)
    if actual != literal:
        return ("refusing %s surface — its instance path changes identity "
                "through a symlink (%s -> %s; expected %s)"
                % (seat, expected, actual, literal))
    return None


def _nested_surface_error(d, target):
    """None only when `target` resolves INSIDE the instance dir it claims.

    THE INSTANCE DIR PASSING IS NOT ENOUGH, and that gap is the whole finding
    (@codex-2 on 5e5bcfd5): `seats/ds4pro` can be a REAL directory whose CHILD
    `claude` is a symlink into `seats/codex/claude`. _seat_surface_error
    resolves the instance path and never looks below it, so the surface gate
    passes, `os.makedirs(child, exist_ok=True)` FOLLOWS the existing link
    without complaint, every writer beneath it lands in another seat's
    settings, and launch returns 0. A zero exit while overwriting a different
    seat is worse than a crash.

    CONTAINMENT ON THE RESOLVED CHILD is the only test that separates "my
    subtree" from "a link wearing my subtree's name". Compared against the
    RESOLVED parent so a symlinked seats_root (a legitimate deployment) does
    not read as an escape — the question is whether the child leaves the
    instance, never whether either path contains a link.

    THE SEPARATOR IS LOAD BEARING: a bare prefix test admits seats/codex-evil
    under seats/codex, which is the same cross-seat write by a different
    spelling. Mutation-pinned.

    NO try/except HERE, DELIBERATELY. realpath defaults to strict=False and
    swallows everything — a missing path (the normal first launch, which is
    exactly why makedirs is called at all) and even a symlink LOOP, which
    resolves to itself. A guard clause for an exception that cannot arrive is
    dead code wearing the look of safety, and the first draft of this function
    had one. A loop at the child stays contained, so makedirs raises ELOOP and
    launch dies LOUDLY — noisy, but never another seat's tree, which is the
    property this guards."""
    parent = os.path.realpath(d)
    actual = os.path.realpath(target)
    if actual != parent and not actual.startswith(parent.rstrip(os.sep) + os.sep):
        return ("refusing surface %s — it resolves outside its own instance "
                "(%s -> %s; expected to stay under %s)"
                % (target, target, actual, parent))
    return None


def _dq_escape(s):
    """Literal text for a DOUBLE-QUOTED shell word. shlex.quote is wrong here:
    its single quotes become literal characters inside "..." — and the four
    characters below keep their meaning in double quotes, so an unescaped path
    expands ($VAR, no-op → truncation) or EXECUTES (`cmd`) at launch."""
    for ch in ("\\", '"', "`", "$"):
        s = s.replace(ch, "\\" + ch)
    return s


def launch_line(family, model=None, room=None, seat=None, room_source=None,
                multi=False):
    """The exact seat launch command. env -u ANTHROPIC_API_KEY is part of the
    line: an inherited key must never ride into a proxied seat either. The
    child-stamp trio (CHILD_STAMP_VARS) is unset right beside it: a spawning
    daemon born inside a Claude session stamps its panes CLAUDE_CODE_CHILD_
    SESSION=1 (+ its own SID/bridge id), and CC then silently disables the
    seat's transcript persistence — the seat must start top-level.
    HELM_CHAT_NAME=<seat> is the STABLE seat identity: the SessionStart join
    hook (seats.py derive_seat) keys the roster on it, so the seat joins as
    'codex'/'codex-2'/'kimi'/… instead of an ephemeral agent-<sid8> — and
    @codex / @codex-2 / @kimi fleet posts then deliver to it. HELM_CELL_PROFILE
    + DREGG_PROFILE bind both helm's signing call and the dregg SDK fallback to
    that SAME seat identity; HELM_CELL_BIN selects the dregg-native client
    signer. A seat therefore never inherits the owner's ambient profile. `seat`
    (slice 6 — N-per-credhome) defaults to the family name; when set it swaps
    the three identity vars + the config dir (instances/<seat>). Instances
    share ONLY the family OAuth cred pool (same account — no quota
    multiplication); everything else is per-instance: config/session state AND,
    since per-instance proxies, the proxy fate itself — each instance gets its
    own port/config/token/log (`_mint_instance_proxy`), so one instance's
    restart or 429-stall never takes a sibling down. `room` adds
    HELM_CHAT_ROOM=<room>; a project-derived default also
    carries HELM_CHAT_ROOM_SOURCE=derived so later SessionStart joins cannot
    undo an operator rehome/clear. The command clears inherited room/source
    first, making explicit --room and project-less un-homed launches stable.
    --dangerously-skip-permissions is CANONICAL for a fleet seat
    (owner-asked 2026-07-21): an agent pane exists to do work unattended, and
    a per-tool permission prompt strands it silently (the owner had to flip
    kimi/codex into auto-mode by hand). The beacon permit narrows an
    interactive session; a launched seat skips wholesale — it never has a
    human at its keyboard to answer a prompt. `multi` (the proven mixed-model
    law, premise multimodel-one-cc-proven-per-agent-frontmatter-no-fork):
    DROP CLAUDE_CODE_SUBAGENT_MODEL entirely — that env var blunt-pins EVERY
    subagent to one model, overriding the per-agent `model:` frontmatter that
    IS the mixed-fleet mechanism; the probe agents minted beside this line
    carry the per-model pins instead."""
    fam = FAMILIES[family]
    model = model or fam["model"]
    seat = seat or family
    port = _instance_port(family, seat)
    # The eval-arm seam (§C pilot FINDING 5): CLAUDE_CONFIG_DIR is emitted as
    # "${HELM_EVAL_CONFIG_DIR:-<seat claude>}" so an eval arm can ride the
    # seat's REAL launch path (same bearer export, same unsets, same model
    # pin — FINDING 1's fix) while pointing at a per-run HOOK-STRIPPED config
    # copy. A seat launch never sets the var, so it expands to today's exact
    # path; evalrun.arm_command refuses a launch.sh whose text lacks the var
    # (minted pre-seam) rather than silently handing the arm the seat's real
    # config dir, fleet hooks included. shlex.quote cannot be used inside the
    # ${...:-} word (its single quotes would ride in as literal characters),
    # so the path is BACKSLASH-ESCAPED for the double-quoted context instead:
    # unescaped, a $ in the path expands and a backtick EXECUTES at every seat
    # launch (kimi measured both live under /bin/sh, 2026-07-29). "seat paths
    # are helm-minted, never attacker-shaped" was the assumption the argv-guard
    # family exists to refuse — HELM_HOME is operator-set, and this line runs
    # on every re-mint, not only eval.
    cfgdir = '"${HELM_EVAL_CONFIG_DIR:-%s}"' % _dq_escape(
        os.path.join(_instance_dir(family, seat), "claude"))
    homing = (" HELM_CHAT_ROOM=%s" % shlex.quote(room)) if room else ""
    if room and room_source:
        homing += " HELM_CHAT_ROOM_SOURCE=%s" % shlex.quote(room_source)
    # Teach CC the seat's real context window + a safe autocompact margin so a
    # non-claude model never sails past its window into the unrecoverable 400
    # (navigate-multimodel-cc-context / ctx-window-recovery-is-clear). Appended
    # AFTER the signing env so HELM_CELL_BIN/PROFILE + DREGG_PROFILE stay
    # byte-identical.
    #
    # TWO KNOBS, NOT SYNONYMS, and #182 nearly swapped one for the other.
    # VERIFIED against the shipped binary (2.1.221, `strings -a`, control
    # ANTHROPIC_BASE_URL present) rather than against the docs, because
    # MAX_CONTEXT_TOKENS is UNDOCUMENTED AND READ — a distinction the docs page
    # cannot make, and the reason a correct line read as an invention:
    #   capacity:  NEu() -> `if(n>0 && !model.startsWith("claude-")) return n`
    #              so MAX_CONTEXT_TOKENS applies to EXACTLY the non-claude proxy
    #              seats this branch mints for, and to nothing else.
    #   window:    SJ()  -> `{window: Math.min(o, c), source:"env"}` where `o` is
    #              that capacity. AUTO_COMPACT_WINDOW is CLAMPED BY IT, so
    #              setting the window alone cannot raise a capacity nobody set.
    # Hence both, always together. Dropping either one silently re-narrows the
    # seat to CC's 200k default for non-claude models.
    #
    # NOT SETTLED, stated so the next reader does not inherit it as fact: NEu
    # consults a per-model table BEFORE the env var, so a proxy model name that
    # IS in that table ignores both. `/context` in a live seat is what settles
    # it; nothing in this file can.
    ctxenv = " CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=%s" % AUTOCOMPACT_PCT_OVERRIDE
    # the MODEL's window outranks the family's: model_context carries
    # per-model input ceilings (spark 76k inside the codex family — task/379);
    # the family max_context stays the default for every unlisted model.
    _ctx = fam.get("model_context", {}).get(model) or fam.get("max_context")
    if _ctx:
        ctxenv += " CLAUDE_CODE_MAX_CONTEXT_TOKENS=%d" % _ctx
        ctxenv += " CLAUDE_CODE_AUTO_COMPACT_WINDOW=%d" % _ctx
    # --multi: no pin (frontmatter routes per-subagent); default: today's line.
    pin = "" if multi else " CLAUDE_CODE_SUBAGENT_MODEL=%s" % model
    # NO-keys-in-argv (codex-2 re-review): the bearer is NEVER a NAME=value arg
    # to the EXTERNAL `env` binary — `env TOKEN=$(cat f)` would put the
    # resolved secret in env's OWN argv (/proc/pid/cmdline). Instead the token
    # is exported into the seat's environ by `_token_export` (a shell builtin,
    # no argv), and the `env` call below only UNSETS inherited vars and sets
    # the non-secret ones. claude inherits the token from the export, so it
    # never transits any process argv, the script text, or the printed line —
    # and it resolves AT EXEC, so the line stays mint-order-immune (the
    # first-mint finding).
    # -u HELM_EVAL_CONFIG_DIR: the SHELL expands the ${...:-} default above
    # before `env` runs, so unsetting it here strips the seam var from the
    # child AFTER it has done its one job — the arm's claude process never
    # carries it onward, and a polluted parent can't ride it into grandkids.
    # --disallowedTools EnterPlanMode sits BEFORE --dangerously-skip-permissions
    # deliberately: --disallowedTools is variadic and swallows a trailing
    # positional (the same live-found 2026-07-18 hazard the smoke legs note),
    # so the next token must be an option. launch.sh ends with "$@", and a
    # resume passes a bare positional prompt — last position would eat it.
    return ("env -u ANTHROPIC_API_KEY %s -u HELM_CHAT_ROOM"
            " -u MELD_CHAT_ROOM -u HELM_CHAT_ROOM_SOURCE"
            " -u MELD_CHAT_ROOM_SOURCE -u HELM_EVAL_CONFIG_DIR"
            " ANTHROPIC_BASE_URL=http://127.0.0.1:%d"
            "%s"
            " CLAUDE_CONFIG_DIR=%s"
            " HELM_CHAT_NAME=%s%s"
            " HELM_AGENT_HARNESS=claude"
            " HELM_MODEL_FAMILY=%s"
            " HELM_MODEL_BACKEND=proxy"
            " HELM_CELL_BIN=%s"
            " HELM_CELL_PROFILE=%s"
            " DREGG_PROFILE=%s%s"
            " claude --disallowedTools %s"
            " --dangerously-skip-permissions --model %s"
            % (child_stamp_unsets(),
               port,
               pin, cfgdir, shlex.quote(seat), homing, shlex.quote(family),
               shlex.quote(DREGG_SIGNER_DEFAULT), shlex.quote(seat),
               shlex.quote(seat), ctxenv, PLAN_ENTRY_TOOL, model))


def _seat_token(family, d):
    """Read-or-mint the per-seat proxy token — stable across re-adds so a
    minted launch line stays valid."""
    token = _read_token(family)
    if not token:
        import secrets
        token = secrets.token_hex(32)
        _write_private(os.path.join(d, "token"), token + "\n")
    return token


def _link_skills(cdir):
    """A seat's config dir is a fresh CLAUDE_CONFIG_DIR, so CC discovers NO
    skills there (it never reads the host's ~/.claude or the owner's home) —
    without this a seat agent can't /learn, /premise, /afk, etc. Canonical-
    first (universal skill distribution): point <cdir>/skills at skillsync's
    CANONICAL source, the same target every credhome carries — a seat is born
    with exactly the fleet set, and a skill added to canonical is instantly
    visible here. Only when no canonical dir exists on this host (foreign
    machine, no skills checkout, no HELM_SKILLS_CANONICAL) fall back to mirroring the
    minting host's own CLAUDE_CONFIG_DIR skills, as before. Symlink (not
    copy) so skill edits propagate live; a stale or indirect symlink is
    normalized to the canonical target, but a REAL skills dir is never
    clobbered at mint — that estate repair (backup + move + link, superset-
    checked) is `helm skills sync`'s deliberate job, not a mint side effect.
    Best-effort: a link failure is loud (stderr) but never fatal — the seat
    still mints, exactly like the delivery-hook install."""
    from . import skillsync, registry
    try:
        src = skillsync.canonical()
    except registry.AuthoredUnreadable as e:
        print("helm seat: authored layer unreadable (%s) — skipping skills link "
              "(best-effort mint continues)" % e, file=sys.stderr)
        src = None
    if not src or not os.path.isdir(src):
        base = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(
            os.path.expanduser("~"), ".claude")
        src = os.path.join(base, "skills")
        if not os.path.isdir(src):
            return
    link = os.path.join(cdir, "skills")
    try:
        if os.path.islink(link):
            if os.readlink(link).rstrip(os.sep) == src.rstrip(os.sep):
                return
            os.unlink(link)
        elif os.path.exists(link):
            print("helm seat: %s/skills is a REAL dir — left untouched; "
                  "`helm skills sync --apply` folds it into the canonical "
                  "source" % cdir, file=sys.stderr)
            return
        os.symlink(src, link)
    except OSError as e:
        print("helm seat: skills not linked into %s (%s); a seat agent won't "
              "see /learn until fixed" % (cdir, e), file=sys.stderr)


# The onboarding state that, if absent, makes CC run its first-run wizard (theme
# picker, bypass-permissions accept, tips) — which STALLS a launched seat at an
# interactive prompt before it ever reaches the composer or runs SessionStart,
# so it never joins chat. Copied (not invented) from an already-onboarded config
# so lastOnboardingVersion matches the CC the host actually runs.
_ONBOARD_KEYS = ("hasCompletedOnboarding", "lastOnboardingVersion", "theme",
                 "numStartups", "tipsHistory", "bypassPermissionsModeAccepted",
                 "hasAcknowledgedCostThreshold")

# Claude Code gates deferred tools (including Monitor) behind GrowthBook state.
# Proxy seats cannot reliably refresh that state themselves, so a fresh instance
# borrows ONLY these cache fields from a working same-family seat. Never widen
# this tuple to identity, auth, project, session, or metric state.
_FEATURE_CACHE_KEYS = ("cachedGrowthBookFeatures", "cachedExperimentFeatures",
                       "cachedGrowthBookFeaturesAt")
_FEATURE_CACHE_GATE = "tengu_deferred_stub_tool"
_FEATURE_CACHE_MIN_FEATURES = 100
_FEATURE_CACHE_MAX_AGE_MS = 7 * 24 * 60 * 60 * 1000
_FEATURE_CACHE_FUTURE_SKEW_MS = 5 * 60 * 1000


def _feature_cache_complete(state, now_ms=None):
    """Whether state can preserve the deferred-tool surface this seed exists for."""
    if not isinstance(state, dict):
        return False
    features = state.get("cachedGrowthBookFeatures")
    experiments = state.get("cachedExperimentFeatures")
    fetched = state.get("cachedGrowthBookFeaturesAt")
    if (not isinstance(features, dict)
            or len(features) < _FEATURE_CACHE_MIN_FEATURES
            or features.get(_FEATURE_CACHE_GATE) is not True
            or not isinstance(experiments, list)
            or isinstance(fetched, bool)
            or not isinstance(fetched, (int, float))
            or not math.isfinite(fetched)):
        return False
    now_ms = time.time() * 1000 if now_ms is None else now_ms
    return (now_ms - _FEATURE_CACHE_MAX_AGE_MS <= fetched
            <= now_ms + _FEATURE_CACHE_FUTURE_SKEW_MS)


def _feature_cache_seed(family, dst):
    """The freshest complete cache, preferring same-family then any family,
    excluding dst.

    Freshness is the cache's own millisecond timestamp; file mtime only breaks a
    tie because Claude rewrites unrelated state independently. Resolved paths
    must remain inside the global seats root, so a symlink cannot import arbitrary
    files outside seats.
    """
    from . import pk
    all_root = os.path.realpath(seats_root())
    fam_root = os.path.realpath(seat_dir(family)) if family else None
    dst = os.path.realpath(dst)

    # Search candidates: same family first, then all other families
    fam_refs = []
    if fam_root:
        fam_refs.append(os.path.join(fam_root, "claude", ".claude.json"))
        fam_refs.extend(glob.glob(os.path.join(
            fam_root, "instances", "*", "claude", ".claude.json")))

    other_refs = []
    for f_dir in sorted(glob.glob(os.path.join(all_root, "*"))):
        if fam_root and os.path.realpath(f_dir) == fam_root:
            continue
        other_refs.append(os.path.join(f_dir, "claude", ".claude.json"))
        other_refs.extend(sorted(glob.glob(os.path.join(
            f_dir, "instances", "*", "claude", ".claude.json"))))

    # Helper to evaluate candidate list
    def _evaluate(refs):
        best = None
        for ref in refs:
            if os.path.islink(ref) or not os.path.isfile(ref):
                continue
            real = os.path.realpath(ref)
            try:
                inside = os.path.commonpath((all_root, real)) == all_root
            except ValueError:
                inside = False
            if not inside or real == dst:
                continue
            state = pk.read_json(real, None)
            if not _feature_cache_complete(state):
                continue
            try:
                mtime = os.path.getmtime(real)
            except OSError:
                mtime = 0
            rank = (state["cachedGrowthBookFeaturesAt"], mtime, real)
            if best is None or rank > best[0]:
                best = rank, state
        return best

    best = _evaluate(fam_refs)
    if best is None:
        best = _evaluate(other_refs)

    if best is None:
        return None
    return {k: best[1][k] for k in _FEATURE_CACHE_KEYS}


def _warn_feature_cache(cdir):
    print("helm seat: WARNING — feature cache not seeded for %s; no recent "
          "seat has a complete %s cache. A launched seat may omit "
          "deferred tools including Monitor until Claude refreshes it"
          % (cdir, _FEATURE_CACHE_GATE), file=sys.stderr)


def _onboarded_refs():
    """Config files to borrow onboarding flags from, best first: the minting
    host's own config (its CC version matches what a seat will run), then the
    plain ~/.claude.json."""
    refs = []
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    if base:
        refs.append(os.path.join(base, ".claude.json"))
    refs.append(os.path.join(os.path.expanduser("~"), ".claude.json"))
    return refs


def _git_toplevel(path):
    """The git root of path (read-only, best-effort) — the trust dialog keys on
    the git-root realpath, so a seat's workdir trust must name it exactly."""
    try:
        import subprocess
        r = subprocess.run(["git", "-C", path, "rev-parse", "--show-toplevel"],
                           capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            return r.stdout.strip() or None
    except Exception:
        pass
    return None


def _seed_onboarding(cdir, workdir=None, family=None):
    """Seed a fresh seat's safe boot state without copying identity or sessions.

    Onboarding/trust state skips two interactive wizards. Same-family feature
    cache state preserves Claude Code's deferred tool surface (proven 2026-07-22:
    cache-less codex-2 omitted Monitor while a cache-backed A/B launch exposed
    it). Never clobber a seat's own state. Best-effort, non-fatal, but a missing
    feature source is loud because the launched seat may be unwakeable.
    """
    from . import pk
    dst = os.path.join(cdir, ".claude.json")
    if os.path.lexists(dst):
        if os.path.islink(dst) or not os.path.isfile(dst):
            print("helm seat: WARNING — refusing non-regular seat state %s; "
                  "feature cache cannot be repaired" % dst, file=sys.stderr)
            return
        state = pk.read_json(dst, None)
        if not isinstance(state, dict):
            print("helm seat: WARNING — unreadable seat state %s; refusing to "
                  "overwrite it for feature-cache repair" % dst, file=sys.stderr)
            return
        if _feature_cache_complete(state):
            return
        cache = _feature_cache_seed(family, dst) if family else None
        if cache is None:
            _warn_feature_cache(cdir)
            return
        state.update(cache)          # preserve every seat-owned field; repair allowlist only
        try:
            mode = stat.S_IMODE(os.stat(dst).st_mode)
            pk.write_json(dst, state)
            os.chmod(dst, mode)       # atomic replace must not relax credential-adjacent state
        except OSError as e:
            print("helm seat: feature cache not repaired for %s (%s); a launched "
                  "seat may omit deferred tools including Monitor" % (cdir, e),
                  file=sys.stderr)
        return
    seed = {"hasCompletedOnboarding": True, "theme": "dark"}
    for ref in _onboarded_refs():
        r = pk.read_json(ref, None)
        if isinstance(r, dict) and r.get("hasCompletedOnboarding"):
            for k in _ONBOARD_KEYS:
                if k in r:
                    seed[k] = r[k]
            # folder-trust is a SEPARATE per-project gate (a second wizard that
            # also stalls a fresh seat): projects[<path>].hasTrustDialogAccepted.
            # Copy just the trust flag for every worktree the ref already trusts,
            # so a seat launched in one of them skips the trust dialog too. Only
            # the trust flags — never the ref's session state/metrics.
            trusted = {}
            for path, pj in (r.get("projects") or {}).items():
                if isinstance(pj, dict) and pj.get("hasTrustDialogAccepted"):
                    trusted[path] = {"hasTrustDialogAccepted": True,
                                     "projectOnboardingSeenCount": 1}
            if trusted:
                seed["projects"] = trusted
            break
    cache = _feature_cache_seed(family, dst) if family else None
    if cache is None:
        _warn_feature_cache(cdir)
    else:
        seed.update(cache)
    # synthesize trust for the intended workdir (+ its git root) — exact-match
    # keys the dialog needs; helm already made the stronger bypass call.
    projects = seed.setdefault("projects", {})
    wd = os.path.realpath(workdir or os.getcwd())
    for p in {wd, _git_toplevel(wd)}:
        if p:
            projects.setdefault(os.path.realpath(p),
                                {"hasTrustDialogAccepted": True,
                                 "projectOnboardingSeenCount": 1})
    try:
        pk.write_json(dst, seed)
    except OSError as e:
        print("helm seat: onboarding not seeded for %s (%s); a launched seat "
              "may stall at the first-run wizard" % (cdir, e), file=sys.stderr)


def _seed_seat_settings(cdir):
    """CC 2.1.216 records bypass-permissions acceptance in settings.json
    (skipDangerousModePermissionPrompt), NOT .claude.json — so a launched
    --dangerously-skip-permissions seat stalls at the bypass warning without it
    (proven 2026-07-21: codex-3). Merge it (+ a theme, + the plan-entry deny)
    into the settings.json that hooks.install_home just wrote, preserving the
    delivery-lane hooks. Best-effort, non-fatal.

    The deny lives HERE and not in hooks.ESTATE_DEFAULTS because hooks estates
    include the owner's own `(default-claude)` home and every credhome he
    resumes into — and taking plan mode away from the human it exists for is
    the opposite of the ruling. This function runs only on a SEAT's config dir,
    from _write_launch_assets, on every `seat add` AND every `seat
    launch/resume` — so the rule is re-asserted on the same cadence launch.sh
    is re-minted, and a seat cannot drift out of it while sitting still.
    Additive: permissions.allow (the beacon permits hooks.install_home just
    merged) and every sibling key survive byte-identical."""
    from . import pk
    p = os.path.join(cdir, "settings.json")
    s = pk.read_json(p, {}) or {}
    changed = False
    for k, v in (("skipDangerousModePermissionPrompt", True), ("theme", "auto")):
        if s.get(k) != v:
            s[k] = v
            changed = True
    perms = s.get("permissions")
    if not isinstance(perms, dict):
        perms = s["permissions"] = {}
        changed = True
    deny = perms.get("deny")
    if not isinstance(deny, list):
        deny = perms["deny"] = []
        changed = True
    if PLAN_ENTRY_TOOL not in deny:
        deny.append(PLAN_ENTRY_TOOL)
        changed = True
    if not changed:
        return
    try:
        pk.write_json(p, s)
    except OSError as e:
        print("helm seat: bypass/theme/plan-entry deny not seeded in %s (%s); a "
              "launched seat may stall at the bypass dialog or at a plan-mode "
              "approval prompt no human is watching" % (p, e), file=sys.stderr)


# The probe agent body: per-agent `model:` frontmatter is the WHOLE mixed-model
# mechanism (premise multimodel-one-cc-proven-per-agent-frontmatter-no-fork) —
# the string in `model:` goes to the wire per-request and the proxy conducts.
_PROBE_AGENT_MD = """---
name: %(name)s
description: helm multi-model probe pinned to %(model)s via frontmatter (the proven per-agent mechanism). Spawn with subagent_type %(name)s when asked to run this probe.
model: %(model)s
---
You are a helm multi-model probe subagent running as model %(model)s.
Reply with exactly the marker text given in your task prompt, then name the
model family you actually are — one line, nothing else.
"""


def probe_agents(family):
    """[(agent_name, model)] for the family's probe models — deterministic
    names (helm-probe-<model-slug>) so re-mints overwrite, never accrete."""
    fam = FAMILIES[family]
    models = fam.get("probe_models") or (fam["model"],)
    return [("helm-probe-" + re.sub(r"[^a-z0-9]+", "-", m.lower()).strip("-"), m)
            for m in models]


def _mint_probe_agents(cdir, family):
    """Mint the family's probe agents into <cdir>/agents/*.md (a
    CLAUDE_CONFIG_DIR-scoped agent set). Returns probe_agents(family)."""
    ad = os.path.join(cdir, "agents")
    os.makedirs(ad, exist_ok=True)
    probes = probe_agents(family)
    for name, model in probes:
        with open(os.path.join(ad, name + ".md"), "w") as f:
            f.write(_PROBE_AGENT_MD % {"name": name, "model": model})
    return probes


def _mint_instance_proxy(family, seat):
    """Give an INSTANCE its own proxy fate: config.yaml + token under
    instances/<seat>/, so `helm seat up <seat>` starts a proxy only this
    instance uses. Idempotent (an existing instance token is kept so a live
    launch line stays valid). The OAuth cred pool stays FAMILY-level — the
    instance config's auth-dir points at the family's auth/, so per-instance
    proxies add NO upstream quota (same account, N local listeners). Only
    proxy (OAuth) families have a pool to point at; proxy-key families bake
    their key into ONE family config and are out of scope here. -> the
    instance proxy-home dir."""
    home_dir = _proxy_home(family, seat)
    os.makedirs(home_dir, mode=0o700, exist_ok=True)
    os.chmod(home_dir, 0o700)
    fam = FAMILIES[family]
    token = _seat_token_per(home_dir)         # instance-scoped, stable
    if fam["mode"] == "proxy":
        auth_dir = os.path.join(seat_dir(family), "auth")   # the SHARED pool
        _write_private(os.path.join(home_dir, "config.yaml"),
                       _config_yaml(_instance_port(family, seat), auth_dir, token))
    return home_dir


def _seat_token_per(d):
    """Read-or-mint a proxy token in an explicit dir (instance-scoped twin of
    the family-level _seat_token)."""
    try:
        with open(os.path.join(d, "token")) as f:
            tok = f.read().strip()
        if tok:
            return tok
    except OSError:
        pass
    import secrets
    tok = secrets.token_hex(32)
    _write_private(os.path.join(d, "token"), tok + "\n")
    return tok


def _write_launch_assets(family, d, room=None, seat=None, workdir=None,
                         room_source=None, multi=False):
    """The seat's isolated CLAUDE_CONFIG_DIR + the executable launch preset —
    identical for every mode, and refreshed by BOTH `add` and `launch` (a
    stale launch.sh minted before HELM_CHAT_NAME existed is why the live
    kimi seat was absent from the roster). The claude dir is born WIRED
    (G-seatlaunch-installs): the delivery lane (deliver + join + stop-guard)
    plus the beacon permit land here at creation through hooks.py's gated
    merge-preserving write — a seat must never be born deaf. Install trouble
    is loud (stderr) but never fatal: the seat still mints and the message
    names the estate-wide repair. `seat` (slice 6) mints an INSTANCE's assets
    (instances/<seat>/{claude,launch.sh}). The instance's PROXY assets
    (config.yaml/token) are minted separately by `_mint_instance_proxy` at
    launch — this function stays proxy-agnostic."""
    seat = seat or family
    ownership = _seat_surface_error(family, seat, d)
    if ownership:
        print("helm seat: " + ownership, file=sys.stderr)
        return _SEAT_SURFACE_REFUSED
    cdir = os.path.join(d, "claude")
    # BEFORE THE FIRST WRITE, not after: makedirs(exist_ok=True) follows an
    # existing symlink, so by the time anything below cdir is written the
    # damage is already in another seat's tree.
    nested = _nested_surface_error(d, cdir)
    if nested:
        print("helm seat: " + nested, file=sys.stderr)
        return _SEAT_SURFACE_REFUSED
    os.makedirs(cdir, exist_ok=True)
    _link_skills(cdir)       # seat agents get the host's /learn, /premise, /afk, …
    _seed_onboarding(cdir, workdir, family)  # onboarding/trust + feature cache
    from . import hooks
    action, detail = hooks.install_home(cdir, specs=hooks.DELIVERY_SPECS)
    if action == "fail":
        print("helm seat: WARNING — %s delivery hooks not installed (%s); "
              "`helm hooks install` closes it" % (seat, detail),
              file=sys.stderr)
    elif action != "ok":
        print("helm seat: %s claude dir wired for fleet delivery (%s: "
              "deliver + join + stop-guard + beacon permit)" % (seat, action),
              file=sys.stderr)
    _seed_seat_settings(cdir)   # skip the bypass-permissions dialog (settings.json)
    if multi:
        _mint_probe_agents(cdir, family)   # per-model frontmatter pins ride here
    _write_launch_sh(os.path.join(d, "launch.sh"),
                     "#!/bin/sh\n# helm seat %s — GENERATED by `helm seat add`; "
                     "regenerate with `helm seat launch %s`.\n"
                     "# EDITS HERE ARE LOST: each of `helm seat launch <seat>` and `helm seat resume <seat>` "
                     "rewrites this file from\n# helm/seat.py (FAMILIES + the "
                     "launch_line builders). A context-window fix was made\n"
                     "# here on 2026-07-30, silently reverted by the next "
                     "resume, and cost a wedged\n# seat and a morning — change "
                     "helm/seat.py, then relaunch the seat.\n"
                     "# child-stamp guard: inherited from a daemon born inside "
                     "a Claude session,\n# these mark the seat a subprocess "
                     "child (persistence silently OFF) — strip.\n"
                     "unset %s\n"
                     "# bearer: exported from the 0600 token file (builtin, no argv) —\n"
                     "# never an env NAME=value arg (the external env binary's argv\n"
                     "# would carry the resolved secret).\n"
                     "%s"
                     "exec %s \"$@\"\n"
                     % (seat, seat, " ".join(CHILD_STAMP_VARS),
                        _token_export(family, seat),
                        launch_line(family, room=room, seat=seat,
                                    room_source=room_source, multi=multi)))


def _write_launch_sh(path, text):
    """launch.sh lands ATOMICALLY (0700 tmp sibling + os.replace): a running
    pane's `sh` reads this script, and an O_TRUNC-in-place rewrite (the
    `_write_private` shape) lets that reader catch a truncated/half file
    mid-re-mint — the slice-6 pool-write lesson, same class. The token never
    leaves the file either way; only the write shape changes."""
    import tempfile
    d = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".launch-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.chmod(tmp, 0o700)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _env_file_value(path, key):
    """The value of the `key=...` line in a .env-style file (`export ` prefix
    and surrounding quotes tolerated); None absent/unreadable. The value is
    secret — callers must never print or log it."""
    try:
        with open(path) as f:
            lines = f.read().splitlines()
    except OSError:
        return None
    for line in lines:
        line = line.strip()
        if line.startswith("export "):
            line = line[7:].lstrip()
        if line.startswith(key + "="):
            val = line.split("=", 1)[1].strip().strip('"').strip("'")
            if val:
                return val
    return None


def _hermes_pool_key(provider):
    """(access_token, base_url, err) for a provider from the hermes CLI's
    credential_pool (HERMES_AUTH). credential_pool[provider] is a LIST of
    bearer entries; this selects the LIVE one — a real bearer (not an
    empty/1-char placeholder), preferring last_status=='ok' then the lowest
    priority — and returns its outbound base_url alongside so the caller wires
    the endpoint the cred was minted for. READ-ONLY on the source; the token
    value is secret and callers must never print or log it."""
    try:
        with open(HERMES_AUTH) as f:
            a = json.load(f)
    except (OSError, ValueError) as exc:
        return None, None, "unreadable %s (%s)" % (HERMES_AUTH, exc)
    pool = (a.get("credential_pool") or {}).get(provider)
    if not isinstance(pool, list) or not pool:
        return None, None, ("no credential_pool.%s entries in %s"
                            % (provider, HERMES_AUTH))
    # a real bearer is >= 20 chars — the junk placeholder entry (a 1-char
    # token) never wins selection.
    live = [e for e in pool if isinstance(e, dict)
            and len(e.get("access_token") or "") >= 20]
    if not live:
        return None, None, ("no live bearer in credential_pool.%s of %s "
                            "(entries present but tokens are empty/placeholder)"
                            % (provider, HERMES_AUTH))
    live.sort(key=lambda e: (
        e.get("last_status") != "ok",
        e.get("priority") if isinstance(e.get("priority"), int) else 1 << 30))
    best = live[0]
    return best.get("access_token"), best.get("base_url"), None


def _opencode_authstore_key(provider):
    """(api_key, err) for a provider from the opencode tool auth store
    (OPENCODE_AUTHSTORE) — a JSON dict of provider -> {"type": "api",
    "key": <bearer>}. Only type=="api" entries carry a static bearer we can
    bake; oauth entries (access/refresh tokens that expire) are skipped here.
    The store carries NO base_url, so the caller keeps the provider's
    pool-table base_url. READ-ONLY on the source; the key value is secret and
    callers must never print or log it."""
    try:
        with open(OPENCODE_AUTHSTORE) as f:
            a = json.load(f)
    except (OSError, ValueError) as exc:
        return None, "unreadable %s (%s)" % (OPENCODE_AUTHSTORE, exc)
    ent = a.get(provider)
    if not isinstance(ent, dict):
        return None, "no %s entry in %s" % (provider, OPENCODE_AUTHSTORE)
    if ent.get("type") != "api":
        return None, ("%s entry in %s is type '%s', not a static api key"
                      % (provider, OPENCODE_AUTHSTORE, ent.get("type")))
    key = ent.get("key")
    # a real bearer is >= 20 chars — an empty/placeholder key never wins.
    if not isinstance(key, str) or len(key) < 20:
        return None, ("%s api entry in %s has no usable key"
                      % (provider, OPENCODE_AUTHSTORE))
    return key, None


def _resolve_homing(explicit_room=None):
    """(room, source) for seat add/launch — seats.resolve_homing is THE one
    precedence (CLI wins, then the inherited launch seam, then the current
    git project; never a private re-derivation). Only a derived choice
    carries the marker into launch assets; explicit choices clear any
    inherited derived provenance."""
    from . import seats
    # safe_cwd, never a bare os.getcwd(): `helm seat add --room X` from a
    # deleted cwd must resolve (eager-getcwd class), not crash pre-resolver.
    room, source = seats.resolve_homing(explicit_room, seats.safe_cwd())
    return room, ("derived" if source == "derived" else None)
