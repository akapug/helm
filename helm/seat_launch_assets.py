"""Launch configuration and asset minting for :mod:`helm.seat`."""
import glob
import json
import math
import os
import re
import shlex

from .yaml_scalar import ABSENT, yaml_scalar, yaml_scalar_or_raise
from . import pk
import stat
import sys
import time
import urllib.parse

# THE NAMES THIS MODULE CALLS ARE BOUND BY THIS MODULE.
#
# helm.seat is a facade: importing it copies its whole namespace into every
# impl module (seat._seed_impl_modules), so an impl module can call a name it
# neither defines nor imports. That is the design, and it holds for every
# caller that enters through the facade. It fails for a caller that reaches an
# impl module DIRECTLY — `from .seat_launch_assets import _instance_dir`, the
# door helm.seat_lifecycle_sessions and helm.seat_health both use — because
# nothing on that path runs the seed. The failure mode is the expensive one:
# no ImportError at startup, a NameError raised at CALL time on whichever
# branch reaches the unbound name first, so the module imports clean and dies
# on use. Binding the names here removes the seed from that call path.
#
# THIS DOES NOT OPT THE MODULE OUT OF THE FAN-OUT. The facade seeds every one
# of these names over the bindings below with the same objects, and a later
# `seat.<name> = fake` still rebinds this module's globals through
# _SeatModule.__setattr__ — the rebinding tests in
# tests/test_seat_split_contract.py measure that seam and are unaffected.
from .seat_catalog import (AUTOCOMPACT_PCT_OVERRIDE, CHILD_STAMP_VARS,
                           DREGG_SIGNER_DEFAULT, FAMILIES, HERMES_AUTH,
                           KEYLESS_API_KEY_PLACEHOLDER, OPENCODE_AUTHSTORE)
from .seat_env import child_stamp_unsets
from .seat_paths import (_launch_endpoint, _maintenance_endpoint, _proxy_home,
                         _write_private, seat_dir, seats_root)
from .seat_ports import _read_token, _token_export


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


def _yaml_quote(value):
    return json.dumps(value, ensure_ascii=False)


def _frontmatter_alias_yaml(channel, model, family=None):
    """The oauth-model-alias block routing CC's built-in agent frontmatter ids
    to a model this family serves — empty for a channel the fork cannot alias.

    ONE ROW PER ID, AND THE ROWS NEED NOT NAME THE SAME MODEL. `model` is
    THIS SEAT'S launch model — the family model for an undeclared instance,
    the instance's own where `instance_models` names one (seat_catalog) — and
    it is the default for every id; a family that declares a `subagent_tiers`
    table names a model PER ID instead, which is what lets one pane burst into
    workers on one model and checkers on another with no second pane (the
    owner shape). An id with NO tier following the seat's launch model is what
    keeps a sol seat's checkers on sol: the caller passes the instance's
    model, so nothing here can escalate a pane back to the family default.
    A family with no table emits `model` on every row, byte-for-byte as
    before the table existed.
    Every tier value is a model that family catalogues — refused at import by
    seat_catalog.subagent_tier_error, so nothing here can mint a row the
    upstream would 502.
    """
    from .seat_catalog import (CC_AGENT_FRONTMATTER_MODELS, FAMILIES,
                               OAUTH_ALIAS_CHANNELS, subagent_tier_model)
    if not channel or not model or channel not in OAUTH_ALIAS_CHANNELS:
        return ""
    fam = FAMILIES.get(family) or {}
    # fork: true is LOAD-BEARING. Without it an alias RENAMES the upstream model
    # and the family's own id stops routing — measured on a codex seat's proxy:
    # claude-opus-5 -> 200 while gpt-6-astra -> 502 "unknown provider". With
    # fork the original stays routable and the alias is added beside it.
    # force-mapping is DELIBERATELY ABSENT. It is not required for routing
    # (fork's config.go: the alias routes with or without it); what it does is
    # rewrite the RESPONSE model to the alias, so a codex seat's subagent
    # would report "claude-opus-5" as its served model and every reader of
    # served-model — proxywatch's route proof first — would see a claude id
    # on a codex family. The alias is a routing convenience; the served
    # identity stays the family's real model.
    rows = "".join("    - name: %s\n      alias: %s\n      fork: true\n"
                   % (_yaml_quote(subagent_tier_model(fam, alias) or model),
                      _yaml_quote(alias))
                   for alias in CC_AGENT_FRONTMATTER_MODELS)
    # THE FIRST COMMENT LINE IS BYTE-IDENTICAL TO THE ONE EVERY LIVE CONFIG
    # ALREADY CARRIES, and the tier line is added only by a family that has a
    # table. A config's text IS the desired state (proxy_config_plan compares
    # bytes and `seat doctor --ensure` respawns the proxy on any difference),
    # so re-wording this line for everybody would have restarted gemini's and
    # grok's live sidecars to deliver a comment.
    tiers = "# (per id where the family declares subagent_tiers: one pane, " \
            "two models)\n" if fam.get("subagent_tiers") else ""
    return ("# built-in subagent frontmatter ids -> this family's model (task/1948)\n"
            "%soauth-model-alias:\n  %s:\n%s" % (tiers, channel, rows))


def _config_yaml(port, auth_dir, token, channel=None, model=None, family=None):
    """The proxy config that passed the live eval, verbatim shape. The
    nonstream-keepalive-interval is NOT optional: a long non-streaming pass
    (compaction's ~360k summarize — the longest single request a session
    makes) sits silent while the upstream thinks, the proxy reaps the idle
    socket, and Claude Code gets an empty HTTP 200 ('proxy or gateway
    intercepting') — owner-witnessed on a codex seat's /compact. The
    live family configs carry 15s by hand; the generator must emit it too or
    every re-mint silently strips the fix (as-prevented)."""
    return ('host: "127.0.0.1"\n'
            "port: %d\n"
            "auth-dir: %s\n"
            "api-keys:\n"
            "  - %s\n"
            "debug: %s\n"
            # THE METER IS ON, FOR EVERY SIDECAR (task/2522). With it off the
            # fork discards every usage record at the sink, so no reader can
            # ever say which seat spent a pooled account. The records live in
            # an in-memory queue the proxy prunes by age; 3600 is the fork's
            # ceiling, and the reader (helm/proxy_usage.py) pops on the
            # fifteen-minute proxywatch pass, so nothing ages out between
            # reads. The queue only fills when management routes are enabled,
            # which the spawn does through MANAGEMENT_PASSWORD (seat_proxy)
            # rather than a secret-key here: the proxy bcrypts a plaintext
            # key and writes the hash BACK into this file, and the reconcile
            # would then rewrite it every pass forever.
            "usage-statistics-enabled: true\n"
            "redis-usage-queue-retention-seconds: 3600\n"
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
            "  bootstrap-retries: 2\n") % (
                port, _yaml_quote(auth_dir), _yaml_quote(token), proxy_debug()) \
        + _frontmatter_alias_yaml(channel, model, family)


def _frontmatter_models_yaml(upstream):
    """Provider-block `models` rows routing CC's built-in agent frontmatter
    ids to this provider's upstream model — one row per catalogued id.

    The key-backed sibling of _frontmatter_alias_yaml: oauth-model-alias
    hangs off OAuth channels, so a key-backed provider (moonshot, deepseek)
    aliases inside its own `models` list instead. MEASURED 2026-09-09 on the
    kimi seat: an Explore child sent "claude-opus-5" upstream and Moonshot
    answered 502 "unknown provider for model claude-opus-5" fifteen times
    (task/1952).

    NO fork: the OAuth alias type carries one; the key-backed
    OpenAICompatibilityModel does NOT (CLIProxyAPI
    internal/config/config_types.go:595-631 — Name/Alias/DisplayName/
    ForceMapping only), so a `fork` key here is silently ignored YAML.
    A review measured this on the first cut: the family
    route survives WITHOUT it because the family's own row stays explicit
    above and the resolver rewrites nothing when original == requested and
    force-mapping is false. force-mapping stays absent for the same reason
    as the OAuth side: it rewrites the RESPONSE model, and every reader of
    served-model (proxywatch first) would see a claude id on a non-claude
    family."""
    from .seat_catalog import CC_AGENT_FRONTMATTER_MODELS
    return "".join("      - name: %s\n"
                   "        alias: %s\n"
                   % (_yaml_quote(upstream), _yaml_quote(alias))
                   for alias in CC_AGENT_FRONTMATTER_MODELS)


def _provider_block_yaml(provider, base_url, key_rows, upstream, alias,
                         frontmatter=True):
    """One openai-compatibility provider block, credential entries and all.

    EXTRACTED SO A FAMILY MAY HAVE MORE THAN ONE OF THEM. A per-model family
    writes one block per model, each with its OWN api-key-entries list,
    because CLIProxyAPI cools a CREDENTIAL and not a model — the measurement
    is recorded on the openrouter catalog entry. Every byte here is the byte
    the single-provider template emitted before the split, so the families
    that have one block still write exactly what they wrote.

    `frontmatter` is False for a per-model family's NON-default blocks. CC's
    built-in agent ids must resolve to exactly one provider, or the proxy has
    five credentials in front of `claude-opus-5` and pools them — which is the
    shared-cooldown failure the per-model split exists to end, re-created one
    alias over.
    """
    return ("  - name: %s\n"
            "    base-url: %s\n"
            "    api-key-entries:\n"
            "%s"
            "    models:\n"
            "      - name: %s\n"
            "        alias: %s\n"
            # built-in subagent frontmatter ids -> this provider's model,
            # beside the family's own row (task/1952 — measured 502s on kimi)
            "%s" % (_yaml_quote(provider), _yaml_quote(base_url), key_rows,
                    _yaml_quote(upstream), _yaml_quote(alias),
                    _frontmatter_models_yaml(upstream) if frontmatter else ""))


def _config_yaml_key(port, token, provider, base_url, model, api_key,
                     upstream=None, api_keys=None, frontmatter=True,
                     providers=None):
    """The proxy-key config: same inbound head (the per-seat token claude
    presents), no auth-dir (no OAuth cred), plus the openai-compatibility
    provider block carrying the outbound API key (0600 via _write_private —
    the same trust level as the seat token beside it). `upstream` is the
    provider-side model id when it differs from the claude-side alias
    (ds4pro: alias ds4-pro -> deepseek/deepseek-v4-flash); default: same id
    both sides (kimi).

    `providers` writes MORE THAN ONE BLOCK in one file — the mint door for a
    per-model family, where every model needs its own block and its own
    credential entry from the first byte. Each entry is a mapping carrying
    `provider`, `base_url`, `alias`, `upstream` and `frontmatter`; the
    positional arguments are then the head's, and are ignored for the body.
    Absent, the file is the single block every existing caller gets, spelled
    exactly as before.
    """
    keys = tuple(api_keys or (api_key,))
    key_rows = "".join("      - api-key: %s\n" % _yaml_quote(key)
                       for key in keys)
    body = "".join(
        _provider_block_yaml(row["provider"], row["base_url"], key_rows,
                             row["upstream"], row["alias"],
                             row.get("frontmatter", True))
        for row in providers) if providers else \
        _provider_block_yaml(provider, base_url, key_rows,
                             upstream or model, model, frontmatter)
    return ('host: "127.0.0.1"\n'
            "port: %d\n"
            "api-keys:\n"
            "  - %s\n"
            "debug: %s\n"
            # the same meter as _config_yaml (task/2522): on, ceiling retention,
            # management enabled by the spawn env rather than a key here
            "usage-statistics-enabled: true\n"
            "redis-usage-queue-retention-seconds: 3600\n"
            "remote-management:\n"
            "  allow-remote: false\n"
            '  secret-key: ""\n'
            "  disable-control-panel: true\n"
            "openai-compatibility:\n"
            # ONE OR MANY PROVIDER BLOCKS, already spelled by
            # _provider_block_yaml — the split is the credential-isolation
            # invariant, not a formatting choice (see that function).
            "%s"
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
            % (port, _yaml_quote(token), proxy_debug(), body))


_TOP_KEY = re.compile(r"^([A-Za-z0-9][A-Za-z0-9_-]*):")


def _top_blocks(text):
    """Return (prefix, [(key, bytes-as-text)]) without parsing secret values."""
    lines = text.splitlines(True)
    starts = [(i, _TOP_KEY.match(line).group(1)) for i, line in enumerate(lines)
              if _TOP_KEY.match(line)]
    if not starts:
        raise ValueError("config has no top-level YAML keys")
    prefix = "".join(lines[:starts[0][0]])
    blocks = []
    for n, (start, key) in enumerate(starts):
        end = starts[n + 1][0] if n + 1 < len(starts) else len(lines)
        blocks.append((key, "".join(lines[start:end])))
    return prefix, blocks


def _yaml_scalar_or_raise(value):
    """The plan side of the ONE reader (helm/yaml_scalar.py): a value, or a
    refusal — an unreadable desired state is not a state to plan against."""
    return yaml_scalar_or_raise(value)


def _block_scalar(block, key):
    lines = block.splitlines()
    if not lines:
        raise ValueError("%s is absent or not a scalar" % key)
    first = lines[0].lstrip()
    name, sep, value = first.partition(":")
    parsed, err = yaml_scalar(name)
    # THE READER'S FAILURE CLASS SURVIVES ITS FIRST CALLER. The whole point
    # of the typed answer is that a refusal can say WHY it could not read;
    # discarding `err` here turned "malformed single-quoted scalar" back
    # into "absent", which is the sentence this module already had. Only
    # ABSENT is folded into the absent-or-not-a-scalar refusal, because that
    # is the one err that genuinely means not there.
    if err is not None and err != ABSENT:
        raise ValueError("%s: %s" % (key, err))
    if not sep or parsed != key or not value.strip():
        raise ValueError("%s is absent or not a scalar" % key)
    return _yaml_scalar_or_raise(value)


def _block_list(block, key, indent=2, item_key=None):
    values = []
    lines = block.splitlines()
    if lines:
        name, sep, inline = lines[0].partition(":")
        if sep and name.strip() == key and inline.strip():
            try:
                flow = json.loads(inline.strip())
            except ValueError:
                raise ValueError("%s has malformed inline values" % key)
            if not isinstance(flow, list) or not flow \
                    or not all(isinstance(value, str) for value in flow):
                raise ValueError("%s inline values are not strings" % key)
            return tuple(flow)
    prefix = " " * indent + "- "
    keyed = (item_key + ":") if item_key else None
    for line in block.splitlines():
        if not line.startswith(prefix):
            continue
        value = line[len(prefix):].strip()
        if keyed:
            if not value.startswith(keyed):
                continue
            value = value[len(keyed):]
        values.append(_yaml_scalar_or_raise(value))
    if not values:
        raise ValueError("%s has no values" % key)
    return tuple(values)


def _indented_sections(block, indent, pattern):
    lines = block.splitlines(True)
    hits = []
    rx = re.compile(pattern)
    for i, line in enumerate(lines):
        match = rx.match(line)
        if match:
            hits.append((i, _yaml_scalar_or_raise(match.group(1))))
    prefix_end = hits[0][0] if hits else len(lines)
    prefix = "".join(lines[:prefix_end])
    sections = []
    for n, (start, name) in enumerate(hits):
        end = hits[n + 1][0] if n + 1 < len(hits) else len(lines)
        sections.append((name, "".join(lines[start:end])))
    return prefix, sections


def _provider_sections(block):
    """Split provider list items without depending on mapping-key order."""
    lines = block.splitlines(True)
    hits = [i for i, line in enumerate(lines) if line.startswith("  - ")]
    prefix_end = hits[0] if hits else len(lines)
    sections = []
    for n, start in enumerate(hits):
        end = hits[n + 1] if n + 1 < len(hits) else len(lines)
        body = "".join(lines[start:end])
        # AN ITEM THIS GRAMMAR CANNOT READ IS CARRIED OPAQUELY, NOT REFUSED
        # ON EVERYONE ELSE'S BEHALF. Enumeration is what every other provider
        # in the file depends on, so refusing here takes the whole family's
        # plan down for a block it may have no custody over -- the same
        # "cannot compute a desired state at all" failure this module exists
        # to end. The REFUSAL SURVIVES where it belongs: `_provider_info` and
        # `_provider_disabled` read the same fields and still raise, so an
        # unreadable block is never selected and never written into, and
        # `_eligible_providers` re-raises when nothing at all was eligible.
        #
        # A None name is not a claim that the block is foreign. It is the
        # absence of an answer, and it is safe here ONLY because every branch
        # it reaches is a non-write: it is skipped for selection and its bytes
        # pass through. A future branch that WRITES must split unreadable from
        # not-ours rather than inheriting this equivalence.
        name = None
        try:
            _prefix, fields = _provider_fields(body)
        except UnsupportedProviderSyntax:
            fields = ()
            opaque = True
        else:
            opaque = False
            for key, field in fields:
                if key == "name":
                    name = _block_scalar(field, key)
                    break
        if name is None and not opaque:
            raise ValueError("openai-compatibility provider has no name")
        sections.append((name, body))
    return "".join(lines[:prefix_end]), sections


def _provider_mapping(item):
    """Normalize one list-item mapping so every key has four-space indent."""
    lines = item.splitlines(True)
    if not lines or not lines[0].startswith("  - "):
        raise ValueError("openai-compatibility provider is not a list item")
    return "    " + lines[0][4:] + "".join(lines[1:])


# THE ADMITTED OWN-KEY GRAMMAR, in ONE place: plain identifiers and quoted
# scalars. Two readers share it -- the validating split and the name-only read
# that reports an unreadable item -- and a second spelling would let them
# disagree about which lines are keys.
_PROVIDER_KEY = r"""^    ([A-Za-z0-9_-]+|"(?:[^"\\]|\\.)*"|'(?:[^']|'')*')[ \t]*:"""


class UnsupportedProviderSyntax(ValueError):
    """This provider block's OWN-DEPTH syntax is outside the admitted grammar.

    A ValueError subclass so every existing handler keeps catching it, and a
    NAMED one so a caller that must treat this refusal differently branches on
    the TYPE. Branching on the message text would be a guard a reworded
    sentence silently opens -- the same reason `_resolve` returns a reason
    constant rather than a sentence.
    """


def _provider_fields(item):
    """Share one admitted own-key grammar between selection and merging.

    Plain identifier keys and quoted scalar keys are understood; unsupported
    own-depth syntax (including YAML merge keys) refuses instead of hiding a
    disabled flag. Nested model/header keys are outside this mapping.
    """
    mapping = _provider_mapping(item)
    pattern = _PROVIDER_KEY
    rx = re.compile(pattern)
    for line in mapping.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        if indent < 4 or indent == 4 and not rx.match(line):
            raise UnsupportedProviderSyntax(
                "unsupported provider mapping key or indentation")
    return _indented_sections(mapping, 4, pattern)


def _provider_info(item, family_model):
    _prefix, sections = _provider_fields(item)
    by_key = {}
    for key, body in sections:
        if key in by_key:
            raise ValueError("openai-compatibility provider repeats %s" % key)
        by_key[key] = body
    provider = _block_scalar(by_key.get("name", ""), "    name".strip()) \
        if "name" in by_key else None
    base_url = _block_scalar(by_key.get("base-url", ""), "    base-url".strip()) \
        if "base-url" in by_key else None
    api_keys = _block_list(by_key.get("api-key-entries", ""),
                           "api-key-entries", indent=6, item_key="api-key")
    models = []
    model_block = by_key.get("models", "")
    _model_prefix, rows = _indented_sections(
        model_block, 6, r'^      - name:\s*(.+?)\s*$')
    for name, body in rows:
        row = {"name": name}
        seen = {"name"}
        for line in body.splitlines()[1:]:
            if not line.startswith("        "):
                continue
            key, sep, value = line.strip().partition(":")
            if not sep:
                continue
            if key in seen:
                row.setdefault("extra", []).append("%s (repeated)" % key)
            seen.add(key)
            if key in ("alias", "fork", "force-mapping"):
                row[key] = _yaml_scalar_or_raise(value)
            else:
                row.setdefault("extra", []).append(key)
        models.append(row)
    native = [row for row in models if row.get("alias") == family_model]
    if not provider or not base_url:
        raise ValueError("provider lacks exact name or endpoint")
    return {"provider": provider, "base_url": base_url,
            "api_keys": tuple(api_keys),
            "upstream": native[0].get("name") if len(native) == 1 else None,
            "native_rows": len(native), "models": tuple(models)}


def _provider_disabled(item):
    """Read only the provider's own flag; absent is enabled, unreadable is not.

    Use the same field boundaries as the merger and the shared scalar reader
    so quoted/spaced keys and tab-separated comments keep their YAML meaning.
    Comments inside a quoted value remain part of that value, not a boolean.
    """
    if not item:
        return False
    _prefix, sections = _provider_fields(item)
    flags = [body for key, body in sections if key == "disabled"]
    if not flags:
        return False
    if len(flags) != 1:
        return True
    raw = flags[0].splitlines()[0].split(":", 1)[1].strip()
    value, err = yaml_scalar(raw)
    if err is not None:
        return True
    # Quoting makes a YAML string, even when decoding its escapes spells false.
    return raw.startswith(('"', "'")) or value not in ("false", "False", "FALSE")


def _private_literal_host(host):
    """Is this hostname a LITERAL private or loopback IP address?

    A NAME IS NOT AN ADDRESS and is deliberately refused: `nas.local` resolves
    wherever DNS says today, so admitting names would make the answer depend
    on a resolver rather than on the text in the config. A literal is a fact
    about the string.
    """
    import ipaddress
    try:
        addr = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return addr.is_private or addr.is_loopback


def _safe_endpoint(value, keyless=False):
    """May helm point a seat at this endpoint?

    HTTPS ALWAYS, and the userinfo/fragment refusals are about the URL itself
    carrying credentials or a client-side tail no server ever sees.

    PLAINTEXT FOR EXACTLY ONE SHAPE: a `keyless` family whose host is a
    LITERAL private or loopback address. The reason the https floor exists is
    that helm bakes a bearer into the seat's config and that bearer then
    travels to this endpoint — a keyless family has no bearer to expose, and
    the request body is already leaving this box over the same LAN either way.
    Widening it any further than that would be trading the credential
    guarantee for convenience: a keyless family on a PUBLIC http host would
    still be sending the owner's prompts across the internet in the clear.
    """
    parsed = urllib.parse.urlsplit(value)
    if not parsed.hostname or parsed.username is not None \
            or parsed.password is not None or parsed.fragment:
        return False
    if parsed.scheme == "https":
        return True
    return bool(keyless) and parsed.scheme == "http" \
        and _private_literal_host(parsed.hostname)


def _credential_shaped(value):
    """Does this string look like a REAL bearer rather than a placeholder?

    A keyless family's key entry is a declared constant, so anything else in
    that slot is drift; the question this answers is the narrower and more
    serious one -- whether the drift is a CREDENTIAL somebody pasted onto a
    family that sends no Authorization header at all. The answer decides the
    wording of a reason, never whether it is reported, so a miss costs a
    vaguer sentence and never a silent pass.

    THE SHAPE JUDGMENT IS THE TREE'S, NOT A SECOND ONE. `accounts.secret_reason`
    is helm's credential detector and it is TUNED AGAINST LIVE PROVIDER ROWS --
    key-shape, JWT and bearer clauses, each with a measured reason for its
    floor, and the same law this function keeps: name the field and the shape,
    never the value. A private copy here would be a second detector to keep in
    step with the first, and the one that drifts is always the copy.

    ONE CLAUSE IS ADDED AND IT IS ABOUT THE FIELD, NOT THE STRING. That
    detector guards PROSE the owner writes about his accounts, so it
    deliberately lets a short token like `sk-abc123` stand rather than refuse a
    sentence that mentions one. This field is a credential SLOT: nothing in it
    is prose, so a vendor bearer prefix is enough on its own, and a truncated
    or half-pasted key is exactly the case worth catching.

    THE VALUE NEVER TRAVELS. The caller gets a boolean and the string stays
    here, because a reason naming a leaked credential that then QUOTES it has
    published it into every log the doctor writes.
    """
    from . import accounts
    text = str(value or "")
    if text == KEYLESS_API_KEY_PLACEHOLDER:
        return False
    if text.lower().startswith(("sk-", "sk_", "pat-", "pat_", "ghp_", "xoxb-",
                                "bearer ")):
        return True
    return bool(accounts.secret_reason("api-key entry", text))


def _keyless_custody_drift(family, info, route):
    """Why a keyless family's provider block is not in the state helm declares,
    else None — the reason the doctor names and the generator then rewrites.

    BOTH FIELDS, BECAUSE BOTH WERE PRESERVED. The endpoint and the key entry
    were each read out of the config and written straight back, so a config
    naming a different box on the LAN and a config carrying a pasted bearer
    both reconciled clean. The endpoint is reported as a value because it is
    not a secret and the operator needs to see which host was there; the key
    is reported as a SHAPE and a count, and the value itself never leaves this
    function.
    """
    reasons = []
    want_url = str(route.get("base_url") or "")
    if info["base_url"].rstrip("/") != want_url.rstrip("/"):
        reasons.append("endpoint is %s, not the catalogued %s"
                       % (info["base_url"], want_url))
    keys = tuple(info["api_keys"])
    if keys != (KEYLESS_API_KEY_PLACEHOLDER,):
        planted = [k for k in keys if _credential_shaped(k)]
        if planted:
            reasons.append(
                "%d of %d key entries carry a CREDENTIAL-SHAPED value on a "
                "family that sends no Authorization header — rotate it at its "
                "vendor and treat this config as a disclosure"
                % (len(planted), len(keys)))
        else:
            reasons.append("%d key entries, not the one declared placeholder"
                           % len(keys))
    if not reasons:
        return None
    return "%s keyless custody: %s" % (family, "; ".join(reasons))


def _eligible_providers(block, family, fam):
    """EVERY provider this family may actually be routed through, never one.

    SELECTION WAS THE WRONG FRAME. A multi-provider alias has NO singular route
    until an authenticated canary names the credential the proxy actually
    picked -- proxywatch._proxy_config_route says exactly that -- because the
    proxy selects across credentials by availability and priority, not by the
    position of a block in a file. An earlier cut of this function chose the
    FIRST catalogued provider and justified it with an operator's comment
    claiming file order wins routing. That comment is evidence about its author
    and not about the program, and building on it put the schema's alias rows
    on one provider while leaving every other routable provider stale.

    SO THE DESIRED STATE COVERS THE WHOLE ELIGIBLE SET: a provider that can be
    selected must be able to serve what it is selected for. Eligibility is
    catalogued for this family and model, a safe endpoint, and not disabled --
    the proxy skips disabled credentials outright, so writing aliases into a
    disabled block satisfies nothing while the enabled fallback stays stale.

    Refusal survives, narrowed: an EMPTY eligible set is still an unreadable
    desired state and still raises, because the watchdog must say UNKNOWN
    rather than invent a provider.
    """
    from .seat_catalog import family_block_alias, family_default_provider, \
        proxy_routes, unconfigured_endpoint
    _prefix, items = _provider_sections(block)
    candidates = []
    unreadable = None
    routes = tuple(proxy_routes(family))
    default_block = family_default_provider(fam)
    for index, (_name, item) in enumerate(items):
        # THE ALIAS IS PER BLOCK, NOT PER FAMILY, and that is the whole
        # difference between a pool family and a per-model one. ds4pro's
        # blocks all serve `ds4-pro` and are told apart by ENDPOINT; a
        # per-model family's blocks share one endpoint and are told apart by
        # ALIAS, so reading `fam["model"]` for every block would select
        # exactly the default one and call the other four foreign — which
        # reads as "openai-compatibility maps to 0 catalogued routes" the
        # moment the default block is the one that is stale.
        expected_alias = family_block_alias(fam, _name)
        try:
            info = _provider_info(item, expected_alias)
        except UnsupportedProviderSyntax as exc:
            # KEPT, NOT RAISED YET. An unreadable block beside a readable one
            # is skipped and reported; an unreadable block and NOTHING else is
            # an unreadable desired state, and then this is the honest reason
            # rather than a count of zero that says nothing about why.
            unreadable = unreadable or exc
            continue
        except ValueError:
            # A retained provider is outside this family's custody. Preserve it
            # byte-for-byte; only the selected provider is interpreted.
            continue
        matches = [route for route in routes
                   if route.get("alias") == expected_alias
                   and route.get("provider") == info["provider"]]
        # A DISABLED PROVIDER IS NOT A ROUTE. CPA skips a disabled credential
        # at selection (selector.go blockReasonDisabled), so writing the
        # schema's alias rows into a disabled block satisfies nothing while
        # every enabled sibling stays stale -- and the plan would then report
        # no drift, because the rows it looked for are technically present.
        if _provider_disabled(item):
            continue
        keyless = bool(fam.get("keyless"))
        exact = [route for route in matches
                 if route.get("base_url") == info["base_url"].rstrip("/")]
        if len(exact) == 1:
            route = exact[0]
        elif keyless:
            # A KEYLESS FAMILY'S ENDPOINT IS THE CATALOG'S, and the pool
            # fallback below is the wrong door for it. That fallback exists
            # because a KEYED pool credential legitimately supplies an endpoint
            # the static table does not carry -- the credential store is the
            # authority there. A keyless family has no credential store: its
            # only endpoint is the one this catalog declares. Sent through the
            # pool branch, ANY private HTTP host in the config satisfied
            # `native_rows == 1` plus an upstream match, was selected on its own
            # word, and was then re-emitted verbatim by the generator -- so a
            # config pointed at a different box on the LAN reconciled clean
            # forever, and `seat doctor --ensure` rewrote it back to itself
            # every three minutes.
            if len(matches) != 1:
                continue
            route = matches[0]
        else:
            # A pool credential can supply an endpoint outside the static table.
            # The stored native row must still bind it to one exact provider /
            # upstream pair; otherwise the endpoint is merely self-declared.
            pool = [route for route in matches if fam.get("pool_providers")
                    and info["native_rows"] == 1
                    and info["upstream"] == route.get("upstream_model")]
            if len(pool) != 1:
                continue
            route = pool[0]
        selected = dict(info)
        selected["upstream"] = route["upstream_model"]
        if keyless:
            # THE DESIRED STATE IS THE CATALOG'S, NOT THE FILE'S, and until
            # this the two were the same object: `proxy_config_plan` emits
            # `info["base_url"]` and `info["api_keys"]` straight back, so
            # whatever the config held WAS the desired state and no drift on
            # either could exist to report. Overriding both here is what gives
            # the generator something to rewrite TO and the reader something to
            # name. `custody_drift` rides on the provider because that is the
            # record `_alias_reason` already receives.
            selected["base_url"] = route["base_url"]
            selected["api_keys"] = (KEYLESS_API_KEY_PLACEHOLDER,)
            selected["custody_drift"] = _keyless_custody_drift(
                family, info, route)
        # THE SAFETY FLOOR IS ON THE ENDPOINT THAT WILL BE WRITTEN, which for a
        # keyless family is no longer the one that was read. Asked of the
        # config's value it would refuse a block this pass is about to CORRECT
        # -- a planted public-http endpoint would leave the eligible set empty
        # and raise "0 catalogued routes" instead of being rewritten to the
        # catalog's. For every other family the two values are the same string,
        # so the set this loop admits is unchanged.
        if not _safe_endpoint(selected["base_url"], keyless=keyless):
            continue
        # THE ALIAS AND THE FRONTMATTER FLAG TRAVEL WITH THE PROVIDER, because
        # the generator writes ONE block per pass and must not look either one
        # up again from the family — a per-model family answers differently
        # per block, and a lookup by family would give every block the default
        # block's answer.
        selected["alias"] = expected_alias
        selected["frontmatter"] = default_block is None \
            or info["provider"] == default_block
        # THE INDEX IS THE IDENTITY. A name identifies a KIND of provider, and
        # a config may carry two blocks sharing one name with different
        # endpoints and different credentials; it may also carry a retained
        # block with the same name that this family has no custody over. So
        # every eligible provider travels with the position it was read from,
        # and the merge binds to that position rather than looking the name up
        # again and taking whatever comes first.
        candidates.append((selected, item, index))
    if not candidates:
        if unreadable is not None:
            raise unreadable
        # AN UNCONFIGURED ENDPOINT IS NAMED, because it is the one cause the
        # operator cures by editing a file rather than the config.
        raise ValueError(unconfigured_endpoint(family)
                         or "openai-compatibility maps to 0 catalogued routes")
    return candidates


def _oauth_rows(block, channel):
    _prefix, sections = _indented_sections(
        block, 2, r'^  ([A-Za-z0-9_-]+):\s*$')
    matches = [body for name, body in sections if name == channel]
    if len(matches) != 1:
        return ()
    rows = []
    current = None
    seen = set()
    for line in matches[0].splitlines()[1:]:
        if line.startswith("    - name:"):
            current = {"name": _yaml_scalar_or_raise(line.split(":", 1)[1])}
            seen = {"name"}
            rows.append(current)
        elif current is not None and line.startswith("      "):
            key, sep, value = line.strip().partition(":")
            if not sep:
                continue
            if key in seen:
                current.setdefault("extra", []).append("%s (repeated)" % key)
            seen.add(key)
            if key in ("alias", "fork", "force-mapping"):
                current[key] = _yaml_scalar_or_raise(value)
            else:
                current.setdefault("extra", []).append(key)
    return tuple(rows)


def _alias_reason(mode, block, family, fam, provider=None, model=None):
    from .seat_catalog import CC_AGENT_FRONTMATTER_MODELS, subagent_tier_model
    expected = tuple(CC_AGENT_FRONTMATTER_MODELS)
    if mode == "proxy-key":
        # CUSTODY BEFORE ALIASES, because a block whose endpoint or key entry
        # is not helm's can carry a perfectly canonical alias table and the
        # alias reading would then answer None — a watchdog saying nothing is
        # wrong about a seat pointed at a stranger's box. This reason exists
        # only for a keyless family (see `_keyless_custody_drift`), and for
        # every other family it is absent and the reading below is unchanged.
        if provider.get("custody_drift"):
            return provider["custody_drift"]
        rows = provider["models"]
        upstream = provider["upstream"]
        # THE BLOCK'S OWN ALIAS ROW IS PART OF THE DESIRED STATE, and on a
        # per-model family's NON-default block it is the ONLY part: those
        # blocks carry no frontmatter ids by design, so a reading that checked
        # only the frontmatter set would call four of five blocks canonical
        # without looking at anything they actually serve.
        own = provider.get("alias") or fam["model"]
        if not provider.get("frontmatter", True):
            expected = ()
        expected = (own,) + expected
        for alias in expected:
            matches = [row for row in rows if row.get("alias") == alias]
            if len(matches) != 1 or matches[0].get("name") != upstream:
                return "%s alias %s does not map exactly once to %s" % (
                    family, alias, upstream)
            if matches[0].get("extra"):
                return "%s alias %s has repeated or unsupported fields" % (
                    family, alias)
            if "fork" in matches[0] or "force-mapping" in matches[0]:
                return "%s alias %s carries unsupported identity rewriting" % (
                    family, alias)
        return None
    channel = fam.get("auth_type")
    rows = _oauth_rows(block or "", channel)
    for alias in expected:
        # THE DESIRED NAME IS PER ID, exactly as the generator emits it: a
        # family declaring subagent_tiers wants its tier model on that row, and
        # comparing every row against fam["model"] would report a correct
        # tiered config as stale forever — `seat doctor --ensure` regenerates
        # on `changed`, so the two readings must come from one rule. `model` is
        # THIS INSTANCE's launch model for the same reason: caller and
        # generator must read one rule, or a declared sol instance would be
        # reported stale on every sweep and respawned to the same bytes.
        want = subagent_tier_model(fam, alias) or model or fam["model"]
        matches = [row for row in rows if row.get("alias") == alias]
        if len(matches) != 1 or matches[0].get("name") != want \
                or matches[0].get("fork") != "true" \
                or matches[0].get("extra") \
                or "force-mapping" in matches[0]:
            return "%s OAuth alias %s is missing or stale" % (family, alias)
    return None


def _model_alias(body):
    for line in body.splitlines()[1:]:
        if line.startswith("        alias:"):
            return _yaml_scalar_or_raise(line.split(":", 1)[1])
    return None


def _merge_model_row(old, canonical):
    """Replace routing identity while retaining capability metadata."""
    old_lines = old.splitlines(True)
    prefix, sections = _indented_sections(
        "".join(old_lines[1:]), 8, r'^        ([A-Za-z0-9_-]+):')
    extras = [body for key, body in sections
              if key not in ("alias", "fork", "force-mapping")]
    return canonical + prefix + "".join(extras)


def _merge_model_rows(old, canonical, family_model):
    from .seat_catalog import CC_AGENT_FRONTMATTER_MODELS
    owned = {family_model, *CC_AGENT_FRONTMATTER_MODELS}
    prefix, rows = _indented_sections(old, 6, r'^      - name:\s*(.+?)\s*$')
    _canonical_prefix, canonical_rows = _indented_sections(
        canonical, 6, r'^      - name:\s*(.+?)\s*$')
    by_alias = {}
    for _name, body in rows:
        by_alias.setdefault(_model_alias(body), []).append(body)
    merged = []
    for _name, body in canonical_rows:
        prior = by_alias.get(_model_alias(body), ())
        merged.append(_merge_model_row(prior[0], body)
                      if len(prior) == 1 else body)
    extras = [body for _name, body in rows if _model_alias(body) not in owned]
    return prefix + "".join(merged) + "".join(extras)


def _merge_oauth_rows(old, canonical):
    from .seat_catalog import CC_AGENT_FRONTMATTER_MODELS
    owned = set(CC_AGENT_FRONTMATTER_MODELS)
    prefix, rows = _indented_sections(old, 4, r'^    - name:\s*(.+?)\s*$')
    _canonical_prefix, canonical_rows = _indented_sections(
        canonical, 4, r'^    - name:\s*(.+?)\s*$')

    def alias(body):
        for line in body.splitlines()[1:]:
            if line.startswith("      alias:"):
                return _yaml_scalar_or_raise(line.split(":", 1)[1])
        return None

    extras = [body for _name, body in rows if alias(body) not in owned]
    return prefix + "".join(body for _name, body in canonical_rows) + "".join(extras)


def _merge_provider_item(old, canonical, family_model):
    old_prefix, old_sections = _provider_fields(old)
    _new_prefix, new_sections = _provider_fields(canonical)
    generated = dict(new_sections)
    order = [key for key, _body in new_sections]
    if "name" not in generated:
        raise ValueError("generated provider lacks name")
    out = ["  - " + generated["name"][4:]]
    emitted = {"name"}
    for key, body in old_sections:
        if key == "name":
            continue
        if key not in generated:
            out.append(body)
            continue
        if key in emitted:
            continue
        replacement = generated[key]
        if key == "api-key-entries":
            # THE FILE'S CREDENTIAL WINS, BECAUSE HELM DID NOT AUTHOR IT. A
            # regeneration reads the bearer out of the config it is rewriting,
            # so emitting the generated list would at best write the same bytes
            # back and at worst replace a rotated credential with the one this
            # pass happened to read first.
            #
            # THE ONE EXCEPTION IS THE VALUE HELM DECLARES. A keyless family's
            # entry is a constant in the catalog, not a credential read off
            # disk, so there is nothing to protect and the retention was
            # instead PRESERVING drift: a pasted bearer on a family that sends
            # no Authorization header survived every reconcile, and a doctor
            # naming it as a disclosure could never rewrite it away. Read from
            # the generated text rather than from a flag, because the
            # placeholder in that text IS the statement that helm owns this
            # field.
            declared = _block_list(replacement, "api-key-entries", indent=6,
                                   item_key="api-key")
            if tuple(declared) != (KEYLESS_API_KEY_PLACEHOLDER,):
                replacement = body
        elif key == "models":
            replacement = _merge_model_rows(body, replacement, family_model)
        out.append(replacement)
        emitted.add(key)
    for key in order:
        if key not in emitted:
            out.append(generated[key])
    return old_prefix + "".join(out)


def _merge_mapping_block(old, canonical):
    """Replace owned mapping keys while retaining unknown sibling fields."""
    old_lines = old.splitlines(True)
    new_lines = canonical.splitlines(True)
    old_prefix, old_sections = _indented_sections(
        "".join(old_lines[1:]), 2, r'^  ([A-Za-z0-9_-]+):')
    _new_prefix, new_sections = _indented_sections(
        "".join(new_lines[1:]), 2, r'^  ([A-Za-z0-9_-]+):')
    generated = dict(new_sections)
    order = [key for key, _body in new_sections]
    out = [new_lines[0], old_prefix]
    emitted = set()
    for key, body in old_sections:
        if key not in generated:
            out.append(body)
        elif key not in emitted:
            out.append(generated[key])
            emitted.add(key)
    for key in order:
        if key not in emitted:
            out.append(generated[key])
    return "".join(out)


def _replace_nested(block, canonical, selected, pattern, family_model=None,
                    preserve_oauth_rows=False, selected_index=None):
    """Merge the canonical body into ONE section and pass every other through.

    A NAME IS NOT AN IDENTITY WHEN NAMES REPEAT, and this file's sections
    repeat by design: two provider blocks may share a name while holding
    different endpoints and different credentials, and a same-named block may
    be outside this family's custody entirely. Choosing the section by name
    and taking the first hit was measured doing both harms at once -- an
    excluded block was rewritten while the eligible block later in the file
    stayed stale, and with two eligible blocks the second pass overwrote the
    first, leaving one endpoint carrying the other's aliases and its own
    credentials.

    So a caller that knows WHICH section it means passes selected_index, and
    the merge binds to that position; the name is then only a consistency
    check on the generated side. Every other section -- same name, different
    name, in custody or not -- passes through byte-for-byte, because this
    function's job is to correct one block's drift and never to have an
    opinion about the rest of the file.

    selected_index=None keeps the by-name behaviour for the callers whose
    sections are a mapping and therefore cannot repeat.
    """
    split = _provider_sections if family_model else \
        lambda text: _indented_sections(text, 2, pattern)
    old_prefix, old_sections = split(block)
    _new_prefix, new_sections = split(canonical)
    replacement = [body for name, body in new_sections if name == selected]
    if len(replacement) != 1:
        raise ValueError("generated config lacks selected %s section" % selected)

    def _merged(body):
        merged = replacement[0]
        if family_model:
            merged = _merge_provider_item(body, merged, family_model)
        elif preserve_oauth_rows:
            merged = _merge_oauth_rows(body, merged)
        return merged

    if selected_index is not None:
        # THE INDEX IS READ FROM THE SAME SPLIT THE CALLER READ, and a merge
        # replaces a section in place, so positions are stable across the
        # per-provider passes. If that ever stops being true the plan is
        # writing into a block it did not measure, which is exactly the class
        # of harm this argument exists to end -- so it refuses rather than
        # falling back to a name.
        if not 0 <= selected_index < len(old_sections):
            raise ValueError(
                "provider section %d is outside the %d sections read"
                % (selected_index, len(old_sections)))
        out = [old_prefix]
        for index, (_name, body) in enumerate(old_sections):
            out.append(_merged(body) if index == selected_index else body)
        return "".join(out)

    out = [old_prefix]
    replaced = False
    for name, body in old_sections:
        if name != selected:
            out.append(body)
            continue
        if replaced:
            # A SECOND SECTION WITH THE SAME NAME IS CUSTODY, NOT A DUPLICATE
            # TO DISCARD: every later one passes through byte-for-byte, because
            # silently losing a credential is worse than any drift this
            # function exists to correct.
            out.append(body)
            continue
        out.append(_merged(body))
        replaced = True
    if not replaced:
        out.append(replacement[0])
    return "".join(out)


def _merge_generated(old, generated, mode, provider=None, channel=None,
                     family_model=None, section_index=None):
    prefix, blocks = _top_blocks(old)
    _generated_prefix, generated_blocks = _top_blocks(generated)
    canonical = dict(generated_blocks)
    order = [key for key, _body in generated_blocks]
    owned = set(order)
    out = [prefix]
    emitted = set()
    for key, body in blocks:
        if key not in owned:
            out.append(body)
            continue
        if key in emitted:
            continue
        replacement = canonical[key]
        if key == "api-keys":
            # Existing configs may authorize more than one inbound bearer. The
            # generator needs one value to build a complete candidate, but this
            # block is credential custody rather than policy: retain every row
            # and its exact spelling instead of silently deleting all but the
            # first during an unrelated alias migration.
            replacement = body
        elif key in ("remote-management", "streaming"):
            replacement = _merge_mapping_block(body, replacement)
        elif key == "openai-compatibility" and provider:
            replacement = _replace_nested(
                body, replacement, provider,
                r'^  - name:\s*(.+?)\s*$', family_model=family_model,
                selected_index=section_index)
        elif key == "oauth-model-alias" and channel:
            replacement = _replace_nested(
                body, replacement, channel,
                r'^  ([A-Za-z0-9_-]+):\s*$', preserve_oauth_rows=True)
        out.append(replacement)
        emitted.add(key)
    for key in order:
        if key not in emitted:
            out.append(canonical[key])
    return "".join(out)


def _readable_name(item):
    """This item's `name` scalar if it can be read WITHOUT the full grammar.

    A block may be unreadable at own depth and still carry a plain `name:`
    line, because the section splitter simply does not match what it cannot
    parse. Returns None when there is no readable name -- which is an absent
    answer, never a claim about whose the block is.
    """
    try:
        _prefix, sections = _indented_sections(
            _provider_mapping(item), 4, _PROVIDER_KEY)
        for key, field in sections:
            if key == "name":
                return _block_scalar(field, key)
    except (ValueError, IndexError):
        pass
    return None


def opaque_provider_sections(block):
    """Every provider item this grammar could not read, by POSITION and name.

    REPORTED RATHER THAN REFUSED, and reported by INDEX because that is the
    identity the merge binds to -- a block whose own keys are unreadable may
    have no readable name either, so a name alone is an unreliable handle for
    exactly the rows that need one. The index is ZERO-BASED because it is the
    same number `section_index` uses; the human ordinal travels beside it so a
    reader and the code cannot end up meaning different blocks by "#1".

    NEVER THE RAW LINE. An earlier cut carried the item's first non-empty line
    so an operator could find it by eye, and a provider whose first own key is
    `api-key:` then printed its CREDENTIAL into a doctor line and every log
    that captures one -- measured, not theorised. Only the decoded `name`
    scalar is published, and its absence is said in words.

    This is what keeps the opaque branch from being silent: a mistake in the
    family's OWN block no longer takes the plan down, so something else has to
    make it visible, or the plan reports no drift about a route it quietly
    stopped considering.
    """
    _prefix, items = _provider_sections(block)
    out = []
    for index, (name, body) in enumerate(items):
        if name is not None:
            continue
        out.append({"index": index, "ordinal": index + 1,
                    "name": _readable_name(body)})
    return out


def _mapping_fields(item):
    """(name, {own key: body}) for one provider item, else None.

    NARROWER THAN `_provider_info` ON PURPOSE, and the narrowing is the fix for
    a measured hole. `_provider_info` VALIDATES: it raises when a block has no
    exact name, no endpoint, or — the case that bit — an `api-key-entries` list
    with no values. Both readers below were built on it and both swallowed that
    raise, so a provider block with NO CREDENTIAL ENTRY became invisible to the
    very guard written to refuse it, and invisible to the refused-model scan
    beside it. A reader whose question is "what does this file MAP" must not
    refuse on a field it does not need to answer that.

    None means the block's own grammar is unreadable, which is a different
    answer again and is reported by `opaque_provider_sections`.
    """
    try:
        _prefix, sections = _provider_fields(item)
    except (ValueError, IndexError):
        return None
    by_key = {}
    for key, body in sections:
        by_key.setdefault(key, body)
    name = None
    if "name" in by_key:
        try:
            name = _block_scalar(by_key["name"], "name")
        except (ValueError, IndexError):
            name = None
    return name, by_key


def config_model_rows(block):
    """Every (provider-block name, upstream model id) the config actually maps.

    READ FROM THE FILE, NOT FROM THE CATALOG, because the whole reason this
    reader exists is that the two can disagree. The generator preserves a
    provider block it has no custody over BYTE-FOR-BYTE (that is deliberate:
    silently losing a credential is worse than any drift), so a hand-added
    route to a refused model survives every regeneration and every doctor
    sweep without a word. Only a reading of the bytes can see it.

    A block whose own grammar is unreadable contributes nothing and does not
    raise: it may belong to somebody else, and `opaque_provider_sections` is
    what reports it.
    """
    out = []
    try:
        _prefix, items = _provider_sections(block)
    except (ValueError, IndexError):
        return ()
    for name, item in items:
        fields = _mapping_fields(item)
        if fields is None:
            continue
        readable, by_key = fields
        _model_prefix, rows = _indented_sections(
            by_key.get("models", ""), 6, r'^      - name:\s*(.+?)\s*$')
        for model, _body in rows:
            if model:
                out.append((name or readable, model))
    return tuple(out)


def disqualified_route_reason(block, fam):
    """Why this config maps a model the family REFUSES, else None.

    THE DISQUALIFICATION LIST IS ONLY A LIST UNTIL SOMETHING READS IT AGAINST
    THE FILE. Four ids were refused for reasons no pricing field and no
    latency table carries — two train on submitted data, one routes to a
    RANDOM free model per call (which makes the reviewing family unknowable
    and silently voids the cross-family guarantee a review lane exists to
    provide), one cannot call a tool. A reader who re-adds one off a benchmark
    ranking would get a config that starts, answers, and quietly does the
    thing the list was written to prevent.

    The reason is carried verbatim from the catalog, because the refusal has
    to teach or the next reader deletes the row instead of the route.
    """
    refused = fam.get("disqualified_models") or {}
    if not refused:
        return None
    for provider, model in config_model_rows(block):
        if model in refused:
            return ("provider block %r maps %s, which this family refuses: %s"
                    % (provider, model, refused[model]))
    return None


def credential_isolation_reason(block, fam):
    """Why this config has lost the per-model credential split, else None.

    THE INVARIANT, MEASURED on a live seat: CLIProxyAPI cools a CREDENTIAL, not a
    model. Two models sharing one provider block share that block's
    api-key-entries, so a 429 on ONE of them benches the entry and the OTHER
    starts refusing locally with "no available credential, 1 cooling down" —
    while the same model answers upstream in 0.3s on a direct curl. One
    rate-limited free preview takes the whole family dark, and the local
    refusal is byte-indistinguishable from a vendor wall.

    So the property is not "the generator emits N blocks" (a generator can be
    edited) but "no provider block serves two of this family's models, and
    every block that serves one has a credential entry of its own". Stated
    that way it is checkable against any config, including one a hand wrote.

    Only families that DECLARE `model_providers` are checked: everyone else
    has one model and the question does not arise.
    """
    rows = fam.get("model_providers")
    if not isinstance(rows, dict) or not rows:
        return None
    mapped = {row.get("upstream_model") for row in rows.values()
              if isinstance(row, dict)}
    by_provider = {}
    for provider, model in config_model_rows(block):
        if model in mapped:
            by_provider.setdefault(provider, set()).add(model)
    for provider, models in sorted(by_provider.items()):
        if len(models) > 1:
            return ("provider block %r serves %d of this family's models (%s) "
                    "— they then SHARE that block's api-key-entries, and "
                    "CLIProxyAPI cools a CREDENTIAL rather than a model, so a "
                    "429 on any one of them benches the credential and every "
                    "other model in the block starts refusing locally with "
                    "'no available credential'. One block per model, each "
                    "with its own key entry."
                    % (provider, len(models), ", ".join(sorted(models))))
    try:
        _prefix, items = _provider_sections(block)
    except (ValueError, IndexError):
        return None
    for name, item in items:
        if name not in by_provider:
            continue
        fields = _mapping_fields(item)
        if fields is None:
            continue
        _readable, by_key = fields
        # NOT `_provider_info`, which REFUSES a block whose api-key-entries
        # list is absent or empty — the exact block this arm exists to name.
        # Asking the validating reader made the guard silent about its own
        # subject, which is the worst shape a guard can have.
        _entry_prefix, entries = _indented_sections(
            by_key.get("api-key-entries", ""), 6,
            r'^      - api-key:\s*(.+?)\s*$')
        if not entries:
            return ("provider block %r serves %s with NO api-key-entries of "
                    "its own — a block with no credential entry cannot be "
                    "isolated from its neighbours' cooldowns"
                    % (name, ", ".join(sorted(by_provider[name]))))
    return None


#: "nobody has said whether to probe" — told apart from an explicit None,
#: which MEANS "the listing could not be read". Two different facts: one asks
#: this module to go and look, the other is the answer that it looked and
#: could not see. A default of None would have made a test that passes None
#: silently trigger a live fetch.
_UNPROBED = object()

#: How long the money guard waits for the vendor's public model listing. Short
#: on purpose: an unreachable listing is NOT a refusal (see `zero_cost_refusal`),
#: so the only thing a long timeout buys is a seat that hangs on start.
VENDOR_LISTING_TIMEOUT = 8


def vendor_model_listing(fam, timeout=VENDOR_LISTING_TIMEOUT):
    """The vendor's own public model listing as {id: record}, or None.

    NO CREDENTIAL IS SENT. OpenRouter serves /models unauthenticated, which is
    what makes this probe safe to run on every start: it cannot spend, cannot
    leak the funded key, and cannot be mistaken by the vendor for usage. The
    same property is why the context-window pins in the catalog could be
    probe-backed at all.

    None on ANY failure, and the caller must treat None as "not measured"
    rather than "nothing wrong" — the split is in `zero_cost_refusal`, which
    refuses on a measured price and never on a missing one.
    """
    import urllib.error
    import urllib.request
    base = str(fam.get("base_url") or "").rstrip("/")
    if not base or not _safe_endpoint(base,
                                      keyless=bool(fam.get("keyless"))):
        return None
    try:
        with urllib.request.urlopen(base + "/models", timeout=timeout) as r:
            blob = json.loads(r.read().decode("utf-8", "replace"))
    except (OSError, ValueError, urllib.error.URLError):
        return None
    rows = blob.get("data") if isinstance(blob, dict) else None
    if not isinstance(rows, list):
        return None
    return {row["id"]: row for row in rows
            if isinstance(row, dict) and isinstance(row.get("id"), str)}


def family_start_refusal(family, fam, config_text, listing=_UNPROBED):
    """Why this family must not be started, plus what to announce — as
    ``(refusal_or_None, notes)``.

    THE PREFLIGHT FOR A FAMILY WHOSE FAILURE MODE IS MONEY, and it is three
    readings of three different things because they fail in three ways:
      * the CONFIG's own model rows, against the family's refused list — the
        generator preserves a block it does not own, so a hand-added route to
        a trains-on-submitted-data model survives every regeneration;
      * the CONFIG's own provider blocks, against the credential-isolation
        invariant — a collapse back to one shared key entry is invisible until
        one model's 429 takes the family dark;
      * the VENDOR's live listing, against the declared price receipts — a
        preview that stopped being free bills the owner's funded account, and
        nothing local can know that.

    `listing` defaults to a sentinel meaning "fetch it"; pass an explicit dict
    or None to decide it yourself, which is how a test drives every arm of this
    without a network.
    """
    from .seat_catalog import zero_cost_refusal
    if not fam.get("model_providers"):
        return None, ()
    _prefix, blocks = _top_blocks(config_text)
    block = "".join(body for key, body in blocks
                    if key == "openai-compatibility")
    for reason in (disqualified_route_reason(block, fam),
                   credential_isolation_reason(block, fam)):
        if reason:
            return "%s: %s" % (family, reason), ()
    if listing is _UNPROBED:
        listing = vendor_model_listing(fam)
    return zero_cost_refusal(family, fam, listing)


def proxy_config_plan(path, family, seat=None):
    """Plan one Helm-profile regeneration while retaining non-owned blocks.

    A MAINTENANCE DOOR BY CONSTRUCTION: it opens `path`, so the instance whose
    config it is planning already exists. That is why the endpoint comes from
    `_maintenance_endpoint` (the reader's accessor over the ledger and this
    instance's own config.yaml) and never from `_launch_endpoint`, the
    NEW-ADMISSION door. Asking admission here made every consumer that had
    correctly resolved an existing high-number endpoint fail one line later:
    `seat up` recovered 8500 through `_existing_instance_port` and then could not
    regenerate the config, so the seat could not be restarted or reconciled at
    all, and `_config_drift_lines` reported the seat as unreadable.
    """
    from .seat_catalog import instance_launch_model
    fam = FAMILIES[family]
    mode = fam.get("mode")
    if mode not in ("proxy", "proxy-key", "proxy-oauth"):
        raise ValueError("%s is not a supported proxy schema" % family)
    with open(path, encoding="utf-8") as f:
        old = f.read()
    _prefix, blocks = _top_blocks(old)
    by_key = {key: body for key, body in blocks}
    if len(by_key) != len(blocks):
        raise ValueError("config repeats a top-level key")
    token = _block_list(by_key.get("api-keys", ""), "api-keys")[0]
    port = _maintenance_endpoint(family, seat)   # never None into `port: %d`
    provider = None
    if mode == "proxy-key":
        # EVERY ELIGIBLE PROVIDER IS PART OF THE DESIRED STATE, because the
        # proxy routes across credentials by availability rather than by a
        # block's position, so a provider that can be selected must be able to
        # serve what it is selected for. One merge per eligible provider, each
        # targeting its own name; every other block is identical across the
        # passes, so they compose and the result does not depend on order.
        eligible = _eligible_providers(
            by_key.get("openai-compatibility", ""), family, fam)
        merged = old
        for info, _item, index in eligible:
            generated = _config_yaml_key(
                port, token, info["provider"], info["base_url"],
                info.get("alias") or fam["model"],
                info["api_keys"][0], info["upstream"],
                api_keys=info["api_keys"],
                frontmatter=info.get("frontmatter", True))
            merged = _merge_generated(
                merged, generated, mode, provider=info["provider"],
                channel=fam.get("auth_type"),
                family_model=info.get("alias") or fam["model"],
                section_index=index)
        # THE DIAGNOSTIC COVERS EVERY ELIGIBLE ROUTE, NOT THE FIRST ONE.
        # Reading drift off eligible[0] alone answered "is the first route
        # canonical" and reported that as the config's health: a canonical
        # provider followed by a stale one returned alias_drift None beside
        # changed True, which is a watchdog saying nothing is wrong about the
        # same pass that just rewrote something. The first non-None reason is
        # returned because one stale route is enough to make the answer no,
        # and naming which one is what makes it actionable.
        alias_reason = None
        for info, _item, _index in eligible:
            alias_reason = _alias_reason(mode, by_key["openai-compatibility"],
                                         family, fam, provider=info)
            if alias_reason is not None:
                break
        return {"old": old, "text": merged, "changed": old != merged,
                "alias_drift": alias_reason,
                "opaque": opaque_provider_sections(
                    by_key.get("openai-compatibility", ""))}
    else:
        auth_dir = _block_scalar(by_key.get("auth-dir", ""), "auth-dir")
        # THE DESIRED STATE IS THIS INSTANCE'S, not the family's: `seat doctor
        # --ensure` regenerates every instance config from here on a */3 cron,
        # so an instance launch model that this function did not read would be
        # reverted within the minute (measured of hand edits) — which is
        # exactly why the declaration lives in the catalog and not on disk.
        model = instance_launch_model(fam, seat)
        generated = _config_yaml(port, auth_dir, token,
                                 channel=fam.get("auth_type"),
                                 model=model, family=family)
        alias_reason = _alias_reason(
            mode, by_key.get("oauth-model-alias"), family, fam, model=model)
    merged = _merge_generated(
        old, generated, mode, provider=provider and provider["provider"],
        channel=fam.get("auth_type"), family_model=fam["model"])
    # THE KEY IS PRESENT ON EVERY PLAN, never only the mode that can populate
    # it. A field that appears conditionally makes `plan["opaque"]` a KeyError
    # on the other modes, so a caller learns the difference by crashing.
    return {"old": old, "text": merged, "changed": old != merged,
            "alias_drift": alias_reason, "opaque": []}


def regenerate_proxy_config(path, family, seat=None):
    """Atomically apply the current generator policy; return (changed, error)."""
    try:
        plan = proxy_config_plan(path, family, seat)
        if plan["changed"]:
            _write_private(path, plan["text"])
        return plan["changed"], None
    except (IndexError, KeyError, OSError, TypeError, ValueError) as exc:
        return False, str(exc)


def proxy_alias_drift(path, family, seat=None):
    """None when exact schema-owned aliases are current; otherwise a reason."""
    try:
        return proxy_config_plan(path, family, seat)["alias_drift"]
    except (IndexError, KeyError, OSError, TypeError, ValueError) as exc:
        return "alias policy unreadable: %s" % exc


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
    (a review on 5e5bcfd5): `seats/ds4pro` can be a REAL directory whose CHILD
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


def _launch_identity(seat):
    """(name, error) for the identity a stale seat asset must launch as.

    A seat's instance directory and home worktree are durable INFRASTRUCTURE,
    not its current identity. ``rename_seat`` records every historical seat key
    on the renamed roster row; that lineage is the proof that lets a generated
    ``codex-2`` launch asset relaunch as ``gt-codex`` without trusting either
    the stale storage label OR ambient HELM_CHAT_NAME. The latter belongs to the
    operator process in ordinary cross-seat launch/resume, not to the target.
    Helm already owns the target selector (the seat argument), so the durable
    roster lineage resolves that target directly; ambiguous lineage refuses.

    ONE RESOLVER, AND THIS WALK IS NOT IT ANY MORE. `seats_lineage.seat_lineage`
    is the same computation, a module of its own beside the one that WRITES `seat_keys`
    because the board needs it too — and a second walker would have drifted
    from this one precisely on the malformed and ambiguous rows, where both
    have to fail closed. The launcher reads two of its three states the same
    way: a name nothing answers to and a name that IS a live seat both launch
    as themselves, so only UNKNOWN is an error here.

    ONE BEHAVIOUR CHANGES AND IT IS DECLARED RATHER THAN INHERITED: the shared
    resolver asks whether a LIVE ROW answers to the name BEFORE it walks any
    lineage, and this walk did not. `rename_seat` leaves the OLD key on the
    successor's row forever, while `write_roster` recycles a released name
    through `reclaim_seat_key` on admission — so once a later seat joins under
    a recycled name, the old walk resolved that LIVE seat to its predecessor's
    successor and the asset relaunched as somebody else. Lineage is what a
    name means when nothing answers to it; it may not outrank a seat that is
    sitting there.
    """
    from .seats_lineage import (SEAT_LINEAGE_UNKNOWN, SEAT_RENAMED,
                                seat_lineage)

    name, state, why = seat_lineage(seat)
    if state == SEAT_LINEAGE_UNKNOWN:
        return None, why
    return (name if state == SEAT_RENAMED else seat), None


def launch_line(family, model=None, room=None, seat=None, room_source=None,
                multi=False, identity=None):
    """The exact seat launch command. env -u ANTHROPIC_API_KEY is part of the
    line: an inherited key must never ride into a proxied seat either. The
    child-stamp trio (CHILD_STAMP_VARS) is unset right beside it: a spawning
    daemon born inside a Claude session stamps its panes CLAUDE_CODE_CHILD_
    SESSION=1 (+ its own SID/bridge id), and CC then silently disables the
    seat's transcript persistence — the seat must start top-level.
    HELM_CHAT_NAME=<seat> is the STABLE seat identity: the SessionStart join
    hook (seats.py derive_seat) keys the roster on it, so the seat joins as
    'codex'/'codex-2'/'kimi'/… instead of an ephemeral agent-<sid8> — and
    fleet posts that @-mention it then deliver to it. HELM_CELL_PROFILE
    + DREGG_PROFILE bind both helm's signing call and the dregg SDK fallback to
    that SAME seat identity; HELM_CELL_BIN selects the dregg-native client
    signer. A seat therefore never inherits the owner's ambient profile.
    `seat` (slice 6 — N-per-credhome) defaults to the family name and selects
    the config dir/port (instances/<seat>); `identity` defaults to that storage
    label but may carry a roster-proven durable rename into the three identity
    vars. HELM_SEAT_STORAGE carries the former explicitly into SessionStart so
    an arbitrary canonical name can still find its storage-keyed spawn register.
    Instances share ONLY the family OAuth cred pool (same account — no quota
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
    from .seat_catalog import (denied_tools, feedback_env_words,
                               instance_launch_model, taught_window,
                               workflow_cap_env_words)
    from . import seats_identity
    fam = FAMILIES[family]
    seat = seat or family
    # THE SEAT IS RESOLVED FIRST because the launch model is now a property of
    # the INSTANCE: `instance_models` names one per instance and falls back to
    # the family model, so an undeclared seat (every other family, every codex
    # instance with no row) mints the same line as before. An explicit `model`
    # — the operator's `--model`, or the persisted choice seat.py re-derives —
    # still outranks the declaration for this pane.
    model = model or instance_launch_model(fam, seat)
    identity = identity or seat
    port = _launch_endpoint(family, seat)   # never None into `127.0.0.1:%d`
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
    # launch (both measured live under /bin/sh). "seat paths
    # are helm-minted, never attacker-shaped" was the assumption the argv-guard
    # family exists to refuse — HELM_HOME is operator-set, and this line runs
    # on every re-mint, not only eval.
    cfgdir = '"${HELM_EVAL_CONFIG_DIR:-%s}"' % _dq_escape(
        os.path.join(_instance_dir(family, seat), "claude"))
    homing = (" HELM_CHAT_ROOM=%s" % shlex.quote(room)) if room else ""
    if room and room_source:
        homing += " HELM_CHAT_ROOM_SOURCE=%s" % shlex.quote(room_source)
    elif not room and room_source == seats_identity.ROOM_CLEARED:
        # ABSENCE AND A CLEARED VALUE MUST NOT SHARE A REPRESENTATION. Without
        # this line a room that was DELIBERATELY emptied (the cwd was measured
        # and has no project) mints a launch.sh byte-identical to one for a
        # seat that never had a room — and the two are read back differently
        # on purpose: a never-homed seat may take a room from whatever
        # resolution runs next, while a cleared one has already answered that
        # question. Collapsing them is how a cleared seat picked the room of
        # whichever process happened to run `helm seat resume`. The stamp
        # rides ALONE: HELM_CHAT_ROOM stays unset (the `env -u` above), so the
        # child still derives its own home from its own cwd.
        homing += " HELM_CHAT_ROOM_SOURCE=%s" % seats_identity.ROOM_CLEARED
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
    # A family `context_budget` then narrows that window (kimi 1M -> 380k,
    # task/2944), through the one helper the autocompact gauge reads too.
    _ctx = taught_window(fam, fam.get("model_context", {}).get(model)
                         or fam.get("max_context"))
    if _ctx:
        ctxenv += " CLAUDE_CODE_MAX_CONTEXT_TOKENS=%d" % _ctx
        ctxenv += " CLAUDE_CODE_AUTO_COMPACT_WINDOW=%d" % _ctx
    # THE OUTPUT CAP, for a family whose models spend before they answer.
    # MEASURED on openrouter's nex-n2.5-pro through the proxy: at
    # max_tokens 1200 it returned NOTHING — 1200 completion tokens consumed,
    # zero text blocks, the whole budget eaten by reasoning — and at 4000 it
    # returned correct code using 2241. A seat under a low cap gets silent
    # empty responses that look exactly like a broken family, so the number is
    # a safety knob and it is DECLARED (seat_catalog `max_output_tokens`),
    # which is also why every other family's launch line is byte-identical:
    # no other entry declares one. CLAUDE_CODE_MAX_OUTPUT_TOKENS is present in
    # the shipped binary (2.1.273, `strings -a`) — the same evidence grade the
    # MAX_CONTEXT_TOKENS pair above stands on.
    if fam.get("max_output_tokens"):
        ctxenv += " CLAUDE_CODE_MAX_OUTPUT_TOKENS=%d" % fam["max_output_tokens"]
    # A proxy seat's workflow cap (task/2559, seat_catalog WORKFLOW_AGENT_CAP)
    # rides as ONE env word after the window knobs: the Workflow tool reads
    # the variable from the process environment, so the cap binds every
    # workflow this pane runs, whoever starts it. Empty on a native seat.
    ctxenv += workflow_cap_env_words(family)
    # Feedback about helm never leaves helm (task/2328, seat_catalog
    # FEEDBACK_ENV): the pair rides AFTER ctxenv so the signing env and the
    # window knobs above stay byte-identical, and BEFORE ` claude` so it is
    # environment, never an argument the variadic --disallowedTools could eat.
    ctxenv += feedback_env_words()
    # A FAMILY-DECLARED SYSTEM LINE, for a model whose defect is in what it
    # WRITES rather than in what it costs (seat_catalog `system_line`).
    # THE ARGUMENT IS THE LEVER HELM HOLDS. A seat's request body is Claude
    # Code's, so a body parameter — the thing qwen27's entry wanted for its
    # thinking budget and could not reach through the proxy's
    # openai-compatibility block — is unavailable here. --append-system-prompt
    # is an argv word on the seat's own launch line, so it reaches every turn
    # of every session that line starts, including a --resume.
    # IT RIDES LAST, AFTER `--model <m>`, and that position is the safe one:
    # --disallowedTools is variadic and swallows a trailing positional (the
    # live hazard the smoke legs note below), so the pair may not sit first,
    # and --model takes exactly one value, so appending after it completes one
    # option and opens another. launch.sh ends with "$@" — a resume's bare
    # positional prompt still arrives after a COMPLETE option pair.
    # Empty for every family that declares none, so every other launch line in
    # this tree stays byte-identical.
    sysline = fam.get("system_line")
    sysline = (" --append-system-prompt %s" % shlex.quote(sysline)) if sysline else ""
    # --multi: no pin (frontmatter routes per-subagent); default: today's line.
    pin = "" if multi else " CLAUDE_CODE_SUBAGENT_MODEL=%s" % model
    # NO-keys-in-argv (a second read): the bearer is NEVER a NAME=value arg
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
    # Every denied token is shell-quoted: a permission RULE such as
    # Agent(fork) (task/2287) carries parentheses, which /bin/sh reads as a
    # subshell in an unquoted word. quote() leaves a bare tool name as is.
    return ("env -u ANTHROPIC_API_KEY %s -u HELM_CHAT_ROOM"
            " -u MELD_CHAT_ROOM -u HELM_CHAT_ROOM_SOURCE"
            " -u MELD_CHAT_ROOM_SOURCE -u HELM_EVAL_CONFIG_DIR"
            " ANTHROPIC_BASE_URL=http://127.0.0.1:%d"
            "%s"
            " CLAUDE_CONFIG_DIR=%s"
            " HELM_CHAT_NAME=%s"
            " HELM_SEAT_STORAGE=%s%s"
            " HELM_AGENT_HARNESS=claude"
            " HELM_MODEL_FAMILY=%s"
            " HELM_MODEL_BACKEND=proxy"
            " HELM_CELL_BIN=%s"
            " HELM_CELL_PROFILE=%s"
            " DREGG_PROFILE=%s%s"
            " claude --disallowedTools %s"
            " --dangerously-skip-permissions --model %s%s"
            % (child_stamp_unsets(),
               port,
               pin, cfgdir, shlex.quote(identity), shlex.quote(seat), homing,
               shlex.quote(family), shlex.quote(DREGG_SIGNER_DEFAULT),
               shlex.quote(identity),
               shlex.quote(identity), ctxenv,
               " ".join(shlex.quote(t) for t in denied_tools(family)),
               model, sysline))


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
    without this a seat agent can't /learn, /premise, /afk, etc. The link is
    skillsync.link_canonical's, the ONE birth-time primitive every config dir
    shares (seat mint here, `helm homes prepare`, `helm launch --home`):
    canonical-first, a stale or indirect link normalized at mint, a REAL
    skills dir never clobbered (that estate repair is `helm skills sync`'s
    deliberate job), and the minting host's own skills mirrored only when no
    canonical exists on this host. Best-effort: a miss is loud (stderr) but
    never fatal — the seat still mints, exactly like the delivery-hook
    install."""
    from . import skillsync
    res = skillsync.link_canonical(cdir, relink=True, host_fallback=True)
    action, detail = res.action, res.detail
    # a canonical read failure is said on its own, whatever the destination
    # did; a fallback that delivered because of it is said beside it — a
    # fallback that succeeds quietly is a degraded source nobody hears about
    # until every seat is running on the host's private set
    for line in (skillsync.failure_line(res), skillsync.degraded_line(res)):
        if line:
            print("helm seat: %s: %s" % (cdir, line), file=sys.stderr)
    if action == "real":
        print("helm seat: %s/skills is a REAL dir — left untouched; "
              "`helm skills sync --apply` folds it into the canonical "
              "source" % cdir, file=sys.stderr)
    elif action == "error":
        print("helm seat: skills not linked into %s (%s); a seat agent won't "
              "see /learn until fixed" % (cdir, detail), file=sys.stderr)
    elif action == "unavailable":
        print("helm seat: skills hub unavailable (%s) — %s has NO skills "
              "until it is restored" % (detail, cdir), file=sys.stderr)


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
    a cache-less codex instance omitted Monitor while a cache-backed A/B launch exposed
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


def _deny_record(value):
    """(list, None) for a list of strings, (None, None) for an absent value,
    (None, why) for anything else — the one reading of a deny record under
    the `helm` settings key, shared by the seeder and `seat retire-deny`, so
    a value that is not a list of strings is never read as permission."""
    if value is None:
        return None, None
    if isinstance(value, list) and all(isinstance(t, str) for t in value):
        return list(value), None
    return None, "not a list of strings (%s)" % type(value).__name__


def _seed_seat_settings(cdir, family=None):
    """CC 2.1.216 records bypass-permissions acceptance in settings.json
    (skipDangerousModePermissionPrompt), NOT .claude.json — so a launched
    --dangerously-skip-permissions seat stalls at the bypass warning without it
    (proven on a live codex seat). Merge it (+ a theme, + the plan-entry deny)
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
    from .seat_catalog import FEEDBACK_DRAFTS_SETTING
    p = os.path.join(cdir, "settings.json")
    s = pk.read_json(p, {}) or {}
    changed = False
    # feedbackDrafts=off (task/2328) rides here for the deny set's reason:
    # this file is what a resume or an orca relaunch inherits, and it is the
    # one switch that removes the SendFeedback tool by itself (seat_catalog
    # FEEDBACK_DRAFTS_SETTING). Authoritative for its key, like the other two.
    for k, v in (("skipDangerousModePermissionPrompt", True), ("theme", "auto"),
                 FEEDBACK_DRAFTS_SETTING):
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
    # THE SCHEMA-UNSAFE SET RIDES THE SAME DENY (task/1941): on a proxy family
    # the Artifact tool's \p{} pattern 400s the whole tool list at OpenAI, and
    # this surface is what a resume or an orca relaunch inherits, so denying it
    # only on the launch line would come back on the first restart. The spawn
    # deny (task/2287 Skill + Agent(fork)) rides here for the same reason, and
    # reaches a minted subagent definition through _frontmatter_deny_yaml.
    from .seat_catalog import (OPERATOR_RECORD_DENIES, SEED_RECORD_DENIES,
                               SEED_RECORD_KEY, denied_tools, retired_denies,
                               workflow_cap_env)
    # THE TWO RECORDS under the key helm owns (seat_catalog
    # RETIRED_SPAWN_DENIES): what helm wrote here, and what the operator asks
    # helm to keep. Either is None when absent or not a list.
    rec = s.get(SEED_RECORD_KEY)
    rec = rec if isinstance(rec, dict) else {}
    # ONE VALIDATOR for both keys, here and in the retire-deny verb: a
    # record is a list of strings or it is nothing. The seeder REWRITES a
    # malformed helm record (it is helm's own) and does not honour a
    # malformed operator one (it names nothing), so neither can remove.
    record, _why = _deny_record(rec.get(SEED_RECORD_DENIES))
    operator, _why = _deny_record(rec.get(OPERATOR_RECORD_DENIES))
    operator = operator or []
    # denied_tools(None) is plan entry alone, so the native door reads the
    # same one door as every family: this module binds no deny name of its
    # own, and a name resolved only through the facade's shared globals is
    # unbound when the implementation is called directly.
    seeded = list(record or [])
    for tool in denied_tools(family):
        if tool not in deny:
            deny.append(tool)
            changed = True   # only an actual append dirties the file
            if tool not in seeded:
                seeded.append(tool)
    # THE OPERATOR'S RECORD IS ALWAYS RE-APPLIED and never edited: an entry
    # it names is in the deny list after every refresh, whatever else moves.
    for tool in operator:
        if tool not in deny:
            deny.append(tool)
            changed = True
    # A RETIRED NAME LEAVES BY ONE OF THREE LEGS, and only a record decides:
    # the operator recorded it (kept, re-applied above); helm recorded it
    # (retired, record updated); nobody recorded it (LEFT, and said so —
    # `helm seat retire-deny` is the deliberate door). Neither the list's
    # shape nor the file's location is read as proof of who wrote it.
    for tool in retired_denies(family) if family else ():
        if tool not in deny or tool in operator:
            continue
        if tool in seeded:
            deny[:] = [t for t in deny if t != tool]
            seeded.remove(tool)
            changed = True
        else:
            print("helm seat: %s keeps the unrecorded deny %r in %s — no %s.%s "
                  "record names it, so a refresh never removes it; `helm seat "
                  "retire-deny %s` lists it and `--apply` retires it "
                  "deliberately, or record it under %s.%s to keep it"
                  % (family, tool, p, SEED_RECORD_KEY, SEED_RECORD_DENIES, tool,
                     SEED_RECORD_KEY, OPERATOR_RECORD_DENIES), file=sys.stderr)
    # THE RECORD NEVER NAMES AN ENTRY THE LIST DOES NOT CARRY: a name the
    # operator removed by hand, or a retirement removed, leaves the record.
    seeded = [t for t in seeded if t in deny]
    if record != seeded:
        if not isinstance(s.get(SEED_RECORD_KEY), dict):
            s[SEED_RECORD_KEY] = rec
        s[SEED_RECORD_KEY][SEED_RECORD_DENIES] = seeded
        changed = True
    # THE WORKFLOW CAP RIDES THE SETTINGS `env` MAP (task/2559) for the deny
    # set's reason: the launch line carries the same variable, and this file
    # is what a resume or an orca relaunch inherits. CC's settings schema
    # declares `env` as "Environment variables to set for Claude Code
    # sessions" (read in the 2.1.272 bundle); the launch-line word is the
    # surface the Workflow tool was READ consuming, this one is the surface
    # that survives a relaunch. Authoritative for its key, additive to the
    # map: an operator's other env entries survive byte-identical. A native
    # seat gets no entry — workflow_cap_env is empty off the proxy modes.
    for k, v in (workflow_cap_env(family) if family else ()):
        env = s.get("env")
        if not isinstance(env, dict):
            env = s["env"] = {}
            changed = True
        if env.get(k) != v:
            env[k] = v
            changed = True
    if not changed:
        return
    try:
        pk.write_json(p, s)
    except OSError as e:
        print("helm seat: bypass/theme/feedback-drafts/plan-entry deny not seeded "
              "in %s (%s); a launched seat may stall at the bypass dialog or at "
              "a plan-mode approval prompt no human is watching, and may draft "
              "helm feedback into Claude Code's own queue" % (p, e), file=sys.stderr)


RETIRE_DENY_USAGE = (
    "seat retire-deny <tool> [--seat S|--all] [--apply]  list (dry run) every "
    "proxy-seat settings.json carrying a deny of <tool> that neither "
    "helm.seeded_denies nor helm.operator_denies records, and what --apply would "
    "remove; --apply removes exactly that entry from exactly those files. A "
    "refresh never removes an unrecorded deny. Applying this fleet-wide is an "
    "OWNER decision; nothing in deploy or doctor runs it. Refuses a tool still "
    "in the current deny set.")


def _proxy_seat_candidates():
    """Every proxy-seat settings.json helm would have minted: (label, family,
    path) for the family dir and each instance dir, WHETHER OR NOT the file
    exists — the strict reader decides absent from unreadable, never a stat
    prefilter. An instances directory helm cannot list is an ERROR, never an
    empty family."""
    from .seat_catalog import PROXY_MODES
    rows = []
    for family, fam in FAMILIES.items():
        if fam.get("mode") not in PROXY_MODES:
            continue
        root = os.path.join(seat_dir(family), "instances")
        try:
            names = sorted(os.listdir(root))
        except FileNotFoundError:
            names = []
        except OSError as e:
            return None, "cannot list %s (%s)" % (root, e)
        for label in [family] + names:
            rows.append((label, family,
                         os.path.join(_instance_dir(family, label), "claude",
                                      "settings.json")))
    return rows, None


_NO_SETTINGS_FILE = object()   # "no such file", told apart from a file of `null`


def _json_kind(value):
    """The JSON name of a parsed value's shape, for a MALFORMED row: `null`
    for None (which is a value here, never absence), else `a <type>`."""
    return "null" if value is None else "a %s" % type(value).__name__


def _classify_retire_candidate(label, family, path, tool):
    """(state, detail, settings) for one candidate, with NO write and no read
    past a containment refusal. States: REFUSED (the path is not this seat's
    own — a linked instance dir or a linked child, the seeder's own guards),
    UNREADABLE (the strict reader failed: dangling link, FIFO, a directory
    helm cannot enter — named with its errno, never absent), ABSENT (no file,
    a seat never minted — told apart from a file of `null` by the reader's
    missing sentinel, never by the parsed value), MALFORMED (the root or the
    present `permissions` container is not an object, or a deny list or a
    `helm` record is not the shape it must be — never read as permission,
    and never read as absent), KEPT (the operator
    recorded it), HELM-OWNED (helm recorded it; the refresh retires it),
    WOULD-REMOVE (unrecorded), NONE (the file carries no such deny)."""
    from . import pk
    from .seat_catalog import (OPERATOR_RECORD_DENIES, SEED_RECORD_DENIES,
                               SEED_RECORD_KEY)
    d = _instance_dir(family, label)
    why = _seat_surface_error(family, label) or _nested_surface_error(d, path)
    if why:
        return "REFUSED", why, None
    try:
        settings = pk.read_json(path, _NO_SETTINGS_FILE, strict=True)
    except Exception as e:                        # noqa: BLE001 — every failure is a row
        err = getattr(e, "errno", None)
        return "UNREADABLE", ("%s%s" % (e, (" [errno %d]" % err) if err else "")), None
    if settings is _NO_SETTINGS_FILE:
        return "ABSENT", "", None
    if not isinstance(settings, dict):
        return "MALFORMED", "settings.json root is %s, not an object" % _json_kind(settings), None
    # A PRESENT CONTAINER THAT IS NOT AN OBJECT IS MALFORMED, never an empty
    # one: `"permissions": "Workflow"` read as no-deny would drop this file
    # from the blocked set and let the fleet write proceed past it.
    perms = settings.get("permissions")
    if "permissions" in settings and not isinstance(perms, dict):
        return "MALFORMED", "permissions is %s, not an object" % _json_kind(perms), None
    deny = perms.get("deny") if perms else None
    if deny is not None and not (isinstance(deny, list)
                                 and all(isinstance(t, str) for t in deny)):
        return "MALFORMED", "permissions.deny is not a list of strings", None
    rec = settings.get(SEED_RECORD_KEY)
    if rec is not None and not isinstance(rec, dict):
        return "MALFORMED", "%s is not an object" % SEED_RECORD_KEY, None
    rec = rec or {}
    operator, why = _deny_record(rec.get(OPERATOR_RECORD_DENIES))
    if why:
        return "MALFORMED", "%s.%s is %s" % (SEED_RECORD_KEY, OPERATOR_RECORD_DENIES, why), None
    seeded, why = _deny_record(rec.get(SEED_RECORD_DENIES))
    if why:
        return "MALFORMED", "%s.%s is %s" % (SEED_RECORD_KEY, SEED_RECORD_DENIES, why), None
    if tool not in (deny or []):
        return "NONE", "", settings
    if tool in (operator or []):
        return "KEPT", ("deny %r is recorded under %s.%s"
                        % (tool, SEED_RECORD_KEY, OPERATOR_RECORD_DENIES)), settings
    if tool in (seeded or []):
        return "HELM-OWNED", ("deny %r is recorded under %s.%s; the next refresh "
                              "retires it" % (tool, SEED_RECORD_KEY, SEED_RECORD_DENIES)), settings
    return "WOULD-REMOVE", "deny %r (unrecorded)" % tool, settings


def cmd_retire_deny(rest):
    """`helm seat retire-deny <tool> [--seat S|--all] [--apply]` — the
    DELIBERATE door for a deny entry no record owns (seat_catalog
    RETIRED_SPAWN_DENIES): a refresh leaves such an entry alone and names
    this verb. Dry-run by default and the dry run IS the list. Three phases,
    and nothing is written before the third: every candidate is CONTAINED
    (the seeder's own identity and nesting guards, before any read),
    CLASSIFIED through the strict reader and the one record validator, and
    only then, under --apply and only when no candidate is REFUSED,
    UNREADABLE or MALFORMED, the WOULD-REMOVE entries are removed — exactly
    the named entry, from exactly those files, appending nothing to
    helm.seeded_denies, never touching an entry helm.operator_denies names,
    never a non-proxy seat. A tool the current table still denies is refused
    (a refresh would re-seed it). Exit 1 whenever a candidate could not be
    judged, so a fleet run never reports clean over a file it did not read."""
    from . import pk
    from .cli import guard_tail
    from .seat_catalog import PROXY_MODES, SPAWN_DENIED_TOOLS, denied_tools
    rest = list(rest)
    tool = rest.pop(0) if rest and not rest[0].startswith("-") else None
    rc = guard_tail("helm seat retire-deny", rest, flags=("--apply", "--all"),
                    valued=("--seat",), usage=RETIRE_DENY_USAGE)
    if rc is not None:
        return rc
    if not tool:
        print("helm seat retire-deny: which tool? (%s)" % RETIRE_DENY_USAGE,
              file=sys.stderr)
        return 2
    if "--seat" in rest and "--all" in rest:
        print("helm seat retire-deny: --seat and --all are two answers to one "
              "question; pass one", file=sys.stderr)
        return 2
    if tool in SPAWN_DENIED_TOOLS or any(tool in denied_tools(f) for f in FAMILIES):
        print("helm seat retire-deny: %r is in the current deny set, so a "
              "refresh would seed it straight back; retire it from "
              "seat_catalog first" % tool, file=sys.stderr)
        return 2
    apply = "--apply" in rest
    if "--seat" in rest:
        from .seat_lifecycle_sessions import _seat_family
        name = rest[rest.index("--seat") + 1]
        family, err = _seat_family(name)
        if err:
            print("helm seat retire-deny: %s" % err, file=sys.stderr)
            return 2
        if FAMILIES[family].get("mode") not in PROXY_MODES:
            print("helm seat retire-deny: %s is not a proxy-family seat; nothing "
                  "here touches a native seat" % name, file=sys.stderr)
            return 2
        rows = [(name, family, os.path.join(_instance_dir(family, name),
                                            "claude", "settings.json"))]
    else:
        rows, err = _proxy_seat_candidates()
        if err:
            print("helm seat retire-deny: " + err, file=sys.stderr)
            return 1
    # PHASE ONE AND TWO: every candidate judged, nothing written
    judged = [(label, path) + _classify_retire_candidate(label, family, path, tool)
              for label, family, path in rows]
    blocked = [row for row in judged if row[2] in ("REFUSED", "UNREADABLE", "MALFORMED")]
    listed = 0
    for label, path, state, detail, _settings in judged:
        if state in ("ABSENT", "NONE"):
            continue
        listed += 1
        if state == "WOULD-REMOVE":
            detail += "; --apply removes it" if not apply else ""
        print("  %-12s %s  %s  %s" % (state, label, path, detail))
    # PHASE THREE: the writes, only under --apply and only over a fleet
    # every member of which was judged
    if apply and not blocked:
        for label, path, state, _detail, settings in judged:
            if state != "WOULD-REMOVE":
                continue
            settings["permissions"]["deny"] = [t for t in settings["permissions"]["deny"]
                                               if t != tool]
            pk.write_json(path, settings)
            print("  %-12s %s  %s  deny %r" % ("REMOVED", label, path, tool))
    if blocked:
        print("helm seat retire-deny: %d candidate(s) could not be judged "
              "(REFUSED/UNREADABLE/MALFORMED above); nothing written%s"
              % (len(blocked), "" if not apply else " — narrow with --seat or repair them first"))
        return 1
    if not listed:
        print("helm seat retire-deny: no proxy-seat settings.json carries a deny "
              "%r" % tool)
    elif not apply:
        print("helm seat retire-deny: dry run — nothing written; --apply removes "
              "the WOULD-REMOVE entries above, and applying that fleet-wide is "
              "an owner decision")
    return 0


def _seed_seat_rules(cdir):
    """Seed the one standing rule line into the seat's <cdir>/CLAUDE.md — CC's
    User memory file for that config dir, loaded into every session there
    (task/2328, seat_catalog.FEEDBACK_RULE). ADDITIVE, BYTE-PRESERVING: a file
    an operator has written keeps every byte (read and written in binary, so
    CRLF line ends and trailing blank lines survive) and gains the sentence
    once, after one newline only when the file does not already end in one; a
    file that already carries it is not touched, so a relaunch never dirties
    it. IN PLACE, NEVER THROUGH A LINK: _write_launch_assets
    guards the directory, not this leaf, and a symlinked CLAUDE.md would have
    carried the write into another seat or the owner's own home. Three
    guards, each covering exactly one step: the lstat refuses a leaf that is
    a link at that instant, without touching it; the READ opens with
    O_NOFOLLOW, so a link swapped in after the lstat is refused (ELOOP) rather
    than read through; the WRITE opens with O_NOFOLLOW, so the same swap can
    never redirect the bytes. Every refusal — including a link that vanishes
    between the lstat and the open — is one stderr line and a return; nothing
    here raises into _write_launch_assets, so launch.sh is still minted. Same
    cadence as _seed_seat_settings (every add, launch and resume)."""
    import stat
    from .seat_catalog import FEEDBACK_RULE
    from . import openflags
    p = os.path.join(cdir, "CLAUDE.md")
    try:
        st = os.lstat(p)
    except FileNotFoundError:
        st = None
    except OSError as e:
        print("helm seat: rules not seeded in %s (%s)" % (p, e), file=sys.stderr)
        return
    if st is not None and stat.S_ISLNK(st.st_mode):
        # No readlink here: the link can be gone or replaced by now, and a
        # second syscall outside the handlers would raise through the mint.
        print("helm seat: rules NOT seeded in %s — the leaf is a symlink; a "
              "seat's rules file is written in place, never through a link "
              "into another home. Replace the link with a real file to seed it."
              % p, file=sys.stderr)
        return
    rule = FEEDBACK_RULE.encode("utf-8")
    body = None
    if st is not None:
        try:
            fd = os.open(p, openflags.flags(os.O_RDONLY, "O_NOFOLLOW", cloexec=True))
            with os.fdopen(fd, "rb") as f:
                body = f.read()
        except OSError as e:
            print("helm seat: rules not seeded in %s (%s)" % (p, e), file=sys.stderr)
            return
        if rule in body:
            return
    if body is None:
        out = (b"# helm seat rules \xe2\x80\x94 seeded by `helm seat add`/`launch`/`resume` "
               b"(helm/seat_launch_assets.py). Lines you add here are kept.\n")
    else:
        out = body if (not body or body.endswith(b"\n")) else body + b"\n"
    out += rule + b"\n"
    # O_NOFOLLOW is REQUIRED on both opens, never folded to zero
    # (helm/openflags): on a platform without it the open refuses with ENOTSUP
    # and the seat is told its rules were not seeded, instead of a write that
    # silently lost the in-place guarantee the lstat above promised.
    try:
        fd = os.open(p, openflags.flags(os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                                        "O_NOFOLLOW", cloexec=True), 0o644)
        with os.fdopen(fd, "wb") as f:
            f.write(out)
    except OSError as e:
        print("helm seat: rules not seeded in %s (%s); a launched seat may "
              "route helm feedback to Claude Code's own queue" % (p, e),
              file=sys.stderr)


# The probe agent body: per-agent `model:` frontmatter is the WHOLE mixed-model
# mechanism (premise multimodel-one-cc-proven-per-agent-frontmatter-no-fork) —
# the string in `model:` goes to the wire per-request and the proxy conducts.
_PROBE_AGENT_MD = """---
name: %(name)s
description: helm multi-model probe pinned to %(model)s via frontmatter (the proven per-agent mechanism). Spawn with subagent_type %(name)s when asked to run this probe.
model: %(model)s
%(deny)s---
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


def _frontmatter_deny_yaml(family):
    """The agent-definition frontmatter line that carries this family's deny
    set down to a subagent the seat mints — "" for a family with no deny.

    THE PER-SUBAGENT SURFACE EXISTS, and it is `disallowedTools` (READ in the
    2.1.270 bundle: the agent-definition schema declares
    `disallowedTools: Array of tool names to explicitly disallow for this
    agent`, and the loader carries it into the dispatched agent's resolved
    tool pool beside the spawn's own). So a helm-minted tier is denied at its
    own definition, not only at the seat — which is the difference between a
    CHILD reader that cannot reach the fan-out doors and one that merely
    inherits a session rule (task/2491).

    `tools:` is DELIBERATELY ABSENT here: the same schema says disallowedTools
    is "Ignored if `tools` is set", so emitting an allowlist beside it would
    silently drop the deny. The list is denied_tools(family) itself — the one
    door, never a second table — and flow-style JSON so the `Agent(fork)`
    rule's parentheses arrive quoted."""
    from .seat_catalog import denied_tools
    tools = denied_tools(family) if family else ()
    if not tools:
        return ""
    return "disallowedTools: [%s]\n" % ", ".join(_yaml_quote(t) for t in tools)


def _mint_probe_agents(cdir, family):
    """Mint the family's probe agents into <cdir>/agents/*.md (a
    CLAUDE_CONFIG_DIR-scoped agent set). Returns probe_agents(family)."""
    ad = os.path.join(cdir, "agents")
    os.makedirs(ad, exist_ok=True)
    probes = probe_agents(family)
    deny = _frontmatter_deny_yaml(family)
    for name, model in probes:
        with open(os.path.join(ad, name + ".md"), "w") as f:
            f.write(_PROBE_AGENT_MD % {"name": name, "model": model,
                                       "deny": deny})
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
    from .seat_catalog import instance_launch_model
    home_dir = _proxy_home(family, seat)
    os.makedirs(home_dir, mode=0o700, exist_ok=True)
    os.chmod(home_dir, 0o700)
    fam = FAMILIES[family]
    token = _seat_token_per(home_dir)         # instance-scoped, stable
    if fam["mode"] == "proxy":
        auth_dir = os.path.join(seat_dir(family), "auth")   # the SHARED pool
        _write_private(os.path.join(home_dir, "config.yaml"),
                       _config_yaml(_launch_endpoint(family, seat), auth_dir, token,
                                    channel=fam.get("auth_type"),
                                    model=instance_launch_model(fam, seat),
                                    family=family))
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


def _guard_preflight(cdir):
    """Shell that CALLS the canonical runtime helper before exec — it does not
    reimplement the rule.

    THE PREVIOUS VERSION WAS A SECOND RESOLVER and it diverged from the real
    one in four measured ways, each a session that started while
    `hooks.external_status` said refuse: a relative pin, an executable
    DIRECTORY, a relative PATH entry, and — the one no static shell check could
    ever catch — a shortened mint followed by installing a valid guard, where
    resolution reads ok while the hook is still ABSENT from settings.json. It
    was stale as well as wrong: generated text cannot know about an external
    spec added after it was written.

    So the asset carries a CALL. `helm hooks preflight` resolves through
    canonical `external_status` over the CURRENT `SEAT_SPECS`, refreshes the
    config, and verifies each required hook is live in the file the session
    will load. The path baked here is data (this seat's own config dir); the
    RULE is fetched at run time, which is the whole distinction.

    FAIL-CLOSED: if helm cannot run at all the command fails and the session is
    refused. Every hook spec in this estate fails OPEN because it must never
    hold a turn hostage; this one decides whether a session begins, and an
    unverifiable answer is a refusal.
    """
    from . import home as _home, hooks
    # HELM_HOME IS PINNED, AND IT IS DATA EXACTLY LIKE THE CONFIG DIR. The
    # config dir alone was not enough: `helm hooks preflight` re-derives which
    # helm home it is talking about, and configs' recognized-config gate
    # accepts a seat dir by its LOCATION UNDER THAT HOME. So a seat minted
    # under a custom HELM_HOME produced a script that passed only when the
    # invoking shell happened to export the same value — rc0 with HELM_HOME
    # matching, rc1 from an ordinary shell where it is unset or points
    # elsewhere. That is a durable launch regression, not an environment
    # requirement: which home this seat belongs to was known at mint time and
    # never changes, so the script states it instead of hoping to inherit it.
    #
    # A FIXTURE THAT NEEDS AN EXTRA VARIABLE TO PASS IS EVIDENCE ABOUT THE
    # CODE. test_credhoming_parity had to inject HELM_HOME for its exec arms,
    # and that injection was masking exactly this bug rather than accommodating
    # a fact of life; it is removed with this cure.
    return ('\n# helm guard preflight — GENERATED; the RULE lives in\n'
            '# `helm hooks preflight` (helm/hooks.py), never in this file.\n'
            '# HELM_HOME and the config dir are DATA about this seat, fixed at\n'
            '# mint; the rule is fetched from the runtime at every launch.\n'
            'if ! HELM_HOME=%s %s hooks preflight --config-dir %s; then\n'
            '  echo "helm seat: REFUSED - the guard preflight did not pass; '
            'no session was started." >&2\n'
            '  exit 1\n'
            'fi\n' % (shlex.quote(_home.helm_home()),
                      shlex.quote(hooks.helm_bin()), shlex.quote(cdir)))


def _launch_owner(command, cdir=None):
    """One owner decides direct headless exec versus attached supervision."""
    from . import hooks, seat_launch_owner
    root = os.path.dirname(os.path.dirname(hooks.helm_bin()))
    owner = "python3 %s" % shlex.quote(os.path.join(
        root, "helm", os.path.basename(seat_launch_owner.__file__)))
    child = shlex.quote("exec %s \"$@\"" % command)
    return "%s\nexec %s -- /bin/sh -c %s helm-seat-harness \"$@\"\n" % (
        _guard_preflight(cdir) if cdir else "", owner, child)


def _launch_surface_refusal(family, seat, d, fatal_shortened=True):
    """(refusal, short): the sentence `_write_launch_assets` refuses with, or
    None, and the unresolved guard list it measured — READ-ONLY.

    EVERY ANSWER HERE IS KNOWABLE BEFORE ANYTHING IS CREATED, which is why it
    is one function with two callers and not a ladder inside the writer. The
    writer asks it before its first write. `seat spawn` asks it BEFORE THE
    REAP as well: a replace that stops the live pane, archives its register
    and only then learns the new surface cannot be written has destroyed a
    working seat in order to refuse — and the refusal was available, unchanged,
    one step earlier.
    """
    ownership = _seat_surface_error(family, seat, d)
    if ownership:
        return ownership, ()
    # BEFORE THE FIRST WRITE, not after: makedirs(exist_ok=True) follows an
    # existing symlink, so by the time anything below cdir is written the
    # damage is already in another seat's tree.
    nested = _nested_surface_error(d, os.path.join(d, "claude"))
    if nested:
        return nested, ()
    # THE GUARD CHECK BELONGS HERE, BEFORE THE FIRST WRITE, for the same reason
    # the two checks above do. Refusing AFTER hooks.install_home has already
    # written settings.json leaves a half-minted seat on disk under a message
    # saying it was not minted — the refusal has to be one the filesystem
    # agrees with, not just the console. A shortened contract is knowable
    # before anything is created, so nothing is.
    from . import hooks as _hooks
    short = _hooks.unresolved_externals(_hooks.SEAT_SPECS)
    if short and fatal_shortened:
        # A DOOR THAT STARTS OR ATTACHES A SESSION (seat launch / spawn /
        # resume). The security capability IS exercised here, so a contract we
        # cannot write in full refuses before anything runs.
        return ("REFUSED — %s claude dir: SHORTENED contract — %d "
                "required guard(s) NOT installable here (%s); no session was "
                "started. Install the guard or pin it, then retry"
                % (seat, len(short),
                   ", ".join(x["name"] for x, _w, _m in short))), short
    return None, short


def _write_launch_assets(family, d, room=None, seat=None, workdir=None,
                         room_source=None, multi=False, model=None,
                         fatal_shortened=True, identity=None):
    """The seat's isolated CLAUDE_CONFIG_DIR + the executable launch preset —
    identical for every mode, and refreshed by BOTH `add` and `launch` (a
    stale launch.sh minted before HELM_CHAT_NAME existed is why the live
    kimi seat was absent from the roster). The claude dir is born WIRED
    (G-seatlaunch-installs): the complete SEAT_SPECS hook contract plus the
    beacon permit land here at creation through hooks.py's gated merge-
    preserving write — a seat must never be born deaf or continuity-blind.
    Install TROUBLE (a failed write) is loud but never fatal: the seat still
    mints and the message names the estate-wide repair. A SHORTENED write is
    different in kind and IS fatal — the contract could not be delivered because
    a required guard resolves to nothing on this host, so the mint returns
    `_SEAT_SURFACE_REFUSED` and no seat is born. A seat that exists without its
    guards is the outcome this refusal exists to prevent; retrying after a
    failed write can succeed, whereas a shortened write will reproduce exactly
    until the machine changes. `seat` (slice 6) mints an
    INSTANCE's assets (instances/<seat>/{claude,launch.sh}). The instance's
    PROXY assets
    (config.yaml/token) are minted separately by `_mint_instance_proxy` at
    launch — this function stays proxy-agnostic."""
    seat = seat or family
    if identity is None:
        identity, identity_error = (seat, None) if not fatal_shortened \
            else _launch_identity(seat)
        if identity_error:
            print("helm seat: REFUSED — launch identity for %s is unresolved: %s; "
                  "no launch asset was written and no session was started"
                  % (seat, identity_error), file=sys.stderr)
            return _SEAT_SURFACE_REFUSED
    refusal, short = _launch_surface_refusal(family, seat, d, fatal_shortened)
    if refusal:
        print("helm seat: " + refusal, file=sys.stderr)
        return _SEAT_SURFACE_REFUSED
    cdir = os.path.join(d, "claude")
    if short:
        # MINT-ONLY (seat add / provision): LOUD, NAMED, and rc 0. The
        # command's objective — durable seat and config creation — SUCCEEDED,
        # and the security capability is not exercised until launch, where a
        # shortened contract MUST refuse before join and exec. The last clause
        # is what makes nonfatal honest rather than silent: a warning that does
        # not say what happens next is the announce-without-acting shape this
        # lane spent three rounds killing. It is also literally true rather
        # than advisory — the minted launch.sh carries `_guard_preflight`, so
        # the artifact itself refuses to start a session until this resolves.
        print("helm seat: WARNING — %s claude dir: SHORTENED contract — %d "
              "required guard(s) NOT installed (%s); this is NOT the full hook "
              "contract. The seat and its config WERE created. LAUNCH WILL "
              "REFUSE until the guard is installed or pinned."
              % (seat, len(short),
                 ", ".join(x["name"] for x, _w, _m in short)), file=sys.stderr)
    os.makedirs(cdir, exist_ok=True)
    _link_skills(cdir)       # seat agents get the host's /learn, /premise, /afk, …
    _seed_onboarding(cdir, workdir, family)  # onboarding/trust + feature cache
    from . import hooks
    res = hooks.install_home(cdir, specs=hooks.SEAT_SPECS)
    action, detail = res
    if action == "fail":
        print("helm seat: WARNING — %s hook contract not installed (%s); "
              "`helm hooks install` closes it" % (seat, detail),
              file=sys.stderr)
    elif action != "ok" and not short:
        # `and not short` — a nonfatal mint already said SHORTENED above, and
        # falling through to this line would have it claim the full contract in
        # the very next breath. Never two sentences about one write.
        print("helm seat: %s claude dir wired for full hook contract (%s: "
              "inject + delivery + handoff + resume + beacon permit)"
              % (seat, action), file=sys.stderr)
    _seed_seat_settings(cdir, family)   # bypass dialog + the family deny set (settings.json)
    _seed_seat_rules(cdir)              # feedback goes to helm rows, never CC /feedback (CLAUDE.md)
    if multi:
        _mint_probe_agents(cdir, family)   # per-model frontmatter pins ride here
    _write_launch_sh(os.path.join(d, "launch.sh"),
                     "#!/bin/sh\n# helm seat %s — GENERATED by `helm seat add`; "
                     "regenerate with `helm seat launch %s`.\n"
                     "# EDITS HERE ARE LOST: each of `helm seat launch <seat>` and `helm seat resume <seat>` "
                     "rewrites this file from\n# helm/seat_launch_assets.py (through the "
                     "helm/seat.py facade). A context-window fix was made\n"
                     "# here on 2026-07-30, silently reverted by the next "
                     "resume, and cost a wedged\n# seat and a morning — change "
                     "helm/seat_launch_assets.py, then relaunch the seat.\n"
                     "# child-stamp guard: inherited from a daemon born inside "
                     "a Claude session,\n# these mark the seat a subprocess "
                     "child (persistence silently OFF) — strip.\n"
                     "unset %s\n"
                     "# bearer: exported from the 0600 token file (builtin, no argv) —\n"
                     "# never an env NAME=value arg (the external env binary's argv\n"
                     "# would carry the resolved secret).\n"
                     "%s"
                     "%s"
                     % (seat, seat, " ".join(CHILD_STAMP_VARS),
                        _token_export(family, seat),
                        _launch_owner(launch_line(
                            family, room=room, seat=seat, identity=identity,
                            room_source=room_source, multi=multi,
                            model=model), cdir=cdir)))


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
        with pk.open_regular(HERMES_AUTH) as f:
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
        with pk.open_regular(OPENCODE_AUTHSTORE) as f:
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
