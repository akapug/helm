"""A seat's LAUNCH RECIPE, and the exact resume it makes possible (task/3695).

THE PROPERTY. A parked seat comes back on the settings it RAN with, never on
whatever the catalog or its home says that day. Two things move a seat's
settings away from its launch while it runs and from today's defaults while
it is parked: a `/model` (or shift+tab) inside the session, and a catalog or
home default that changes after it launched. A resume that re-derives the
launch from current code swaps the model, the window or the permission mode
silently, and a seat that could only be resumed that way is "saved only",
never asleep. docs/VERBS.md (`helm seat resume`) records the measured cases.

THE RECIPE. The settings a seat's claude process runs with, by field:

  model                 the model it runs NOW (a /model moves it after launch)
  config_dir            its CLAUDE_CONFIG_DIR, the credential home (the
                        default home is ~/.claude, run with the var unset)
  cwd                   the directory its session resumes from
  permission            the permission mode it runs NOW (shift+tab moves it)
  effort                the effort level, plus ultracode when it is on
  disallowed_tools      its --disallowedTools words
  append_system_prompt  its --append-system-prompt text
  identity_env          HELM_CHAT_NAME, HELM_CELL_PROFILE, DREGG_PROFILE
  family, window, subagent
                        proxy seats only: the catalog family, the context
                        window its launch taught it, its subagent pin

plus three that are recorded and shown but never block a resume: `harness`,
`harness_version` (helm cannot pick a claude binary) and `account` (the
transcript's paying account id, another kind of id than a home's, so it is
shown and never compared). THE ACCOUNT CHECK compares one kind of id on both
sides, the measured key of an account email read from a home: the one the
home of the process it ran in held when that process was captured, and the
one the home it resumes on holds now (`account_check`). A side that cannot
be read prints `account: UNKNOWN (<why>)` and refuses nothing; only two keys
that differ refuse.

WHERE EACH FIELD IS READ. No new store: every source already exists.
  transcript    the session's own record of how it runs now: the model
                (Claude Code's `model` attachment carries the exact id, 1M
                variant included, and a later /model is honoured), the last
                permission mode, the effort level, ultracode enter/exit, the
                harness version and the paying account id.
  live process  /proc/<pid>/cmdline and an ALLOW-LISTED /proc/<pid>/environ of
                the seat's own claude, while it runs.
  capture       that same argv/environ reading, taken by every
                `seat resume --all --apply` pass (the reboot timer, every five
                minutes) for each LIVE seat and kept in the resume live set's
                `recipes` map, carried forward pass to pass and boot to boot.
                A process's argv and environment never change while it runs,
                so a capture taken at any time in its life is exact when it
                parks or goes down; the fields that DO move are the
                transcript's.
  last launch   a proxy seat's launch.sh, which states its whole launch line.
Mutable fields (model, permission, effort) take the transcript first; the
argv/environ fields take the live process, then the capture, then the last
launch. A capture counts only as this session's LAST launch (THE ONE RULE,
`capture_binds`: same session, same process birth, no later launch, and
for a pruned copy a lineage stamp naming exactly the requested source).

EXACTLY MEANS ONE LAUNCH (`_one_launch`). The transcript, a live process
and a capture are this session's launch: the process held the session, or,
for a cv-pruned copy, held the session it was pruned from (the copy's own
lineage stamp, `source_session`). The last launch is not: `seat remint`,
`seat launch` and `seat add` rewrite launch.sh with no launch after it, so
nothing binds it to a launch. A field
read from launch.sh beside one the transcript states, or from a source
below the first launch source read, is unknown, named with its source, and
the exact resume refuses it as it does any unknown field.

NOTHING SECRET IS READ OR KEPT: EVERY FIELD PASSES AN ALLOW-LIST. helm's own
launch line carries no secret (its token law), but an adopted lead's process
was started by a person or another launcher, and any word of its argv or its
environ can carry one (a --settings env block, an --mcp-config header, the
text of --append-system-prompt, a token passed as --model, a directory named
for its owner). So a value is read from a process, a capture or a launch only
through the allow-list for its field, and a capture is written only through
the same lists:
  argv    ARGV_KINDS: each flag's value is one of its list — a model the
          seat catalog knows, a mode or effort level claude's flag takes, a
          known tool or a deny rule helm mints, a catalog system line, the
          ultracode boolean. No other word is read at all.
  env     ENV_KINDS: only these names, and each VALUE passes its kind's list
          — a seat name by helm's seat-name rule, a home under a root helm
          knows, a catalogued family, a window, a catalogued model. The
          bearer and every other name are dropped at the read.
  record  CAPTURE_KEYS: every key a capture writes, with its list. The
          binary is kept only as the version its versions layout names,
          never as a path, and the writer (`_record`) refuses a key it has
          no list for.
A value outside its list is never persisted, carried, printed or logged: its
FIELD is recorded as withheld BY NAME, so an exact resume refuses naming the
field, as it does a missing one, and `--defaults` names the field, never the
value.
"""
import glob
import json
import os
import re
import shlex
import sys
import time

from . import pk

CLAUDE_FIELDS = ("model", "config_dir", "cwd", "permission", "effort",
                 "disallowed_tools", "append_system_prompt", "identity_env")
PROXY_FIELDS = CLAUDE_FIELDS + ("family", "window", "subagent")
INFO_FIELDS = ("harness", "harness_version", "account")

IDENTITY_KEYS = ("HELM_CHAT_NAME", "HELM_CELL_PROFILE", "DREGG_PROFILE")
WINDOW_KEY = "CLAUDE_CODE_MAX_CONTEXT_TOKENS"
SUBAGENT_KEY = "CLAUDE_CODE_SUBAGENT_MODEL"
CONFIG_KEY = "CLAUDE_CONFIG_DIR"
FAMILY_KEY = "HELM_MODEL_FAMILY"

SKIP_FLAG = "--dangerously-skip-permissions"
MODE_FLAG = "--permission-mode"
BYPASS = "bypassPermissions"
_DENY_FLAGS = ("--disallowedTools", "--disallowed-tools")
_VALUED = {"--model": "model", MODE_FLAG: "permission",
           "--append-system-prompt": "append_system_prompt",
           "--effort": "effort_level", "--settings": "settings"}
_SESSION_VALUED = ("--resume", "-r", "--session-id")
_SESSION_FLAGS = ("--continue", "-c")
_EVAL_SEAM = "${HELM_EVAL_CONFIG_DIR:-"
# CLAUDE CODE'S OWN OPTION NAMES that a resume does not restore: the list a
# dropped option must be on to be NAMED in "not restored: ..." (its value is
# never read). The measured boolean and optional-value options
# (helm/session.py `_MEASURED_BOOL_FLAGS`, `_MEASURED_OPTIONAL_FLAGS`, less
# the session and print flags), the value-taking ones `claude --help` lists,
# and --settings, which drops whenever it carries more than the ultracode key
# the recipe restores. Any other option-shaped word is COUNTED, never named:
# a word on no list is free text.
CLAUDE_FLAGS = frozenset((
    "--add-dir", "--agent", "--agents", "--allowedTools", "--allowed-tools",
    "--append-system-prompt-file", "--betas", "--debug-file",
    "--fallback-model", "--input-format", "--json-schema", "--max-budget-usd",
    "--max-turns", "--mcp-config", "--mcp-debug", "--name", "--output-format",
    "--permission-prompt-tool", "--plugin-dir", "--setting-sources",
    "--settings", "--system-prompt", "--system-prompt-file", "--tools",
    "--allow-dangerously-skip-permissions", "--ax-screen-reader", "--bare",
    "--brief", "--chrome", "--disable-slash-commands",
    "--exclude-dynamic-system-prompt-sections", "--fork-session",
    "--forward-subagent-text", "--ide", "--include-hook-events",
    "--include-partial-messages", "--no-chrome", "--no-session-persistence",
    "--replay-user-messages", "--safe-mode", "--strict-mcp-config",
    "--verbose", "--debug", "-d", "--from-pr", "--prompt-suggestions",
    "--remote-control", "--worktree", "-w"))
MAX_UNLISTED = 1000

# THE ALLOW-LISTS (task/3695; the module docstring says why). Each field's
# list is one function under "allow-lists" below; ARGV_KINDS, ENV_KINDS and
# CAPTURE_KEYS bind them to what a capture writes. The recipe fields a value
# outside its list withholds, by name:
ARGV_FIELDS = ("model", "permission", "effort", "disallowed_tools",
               "append_system_prompt")
ENV_FIELDS = ("config_dir", "identity_env", "window", "subagent", "family")
VOCAB_FIELDS = ARGV_FIELDS + ENV_FIELDS
# recorded-only facts a capture can withhold too (never a reason to refuse)
WITHHOLDABLE = VOCAB_FIELDS + ("harness_version",)
# Claude Code's own model words. A Claude model id is known when the catalog
# routes it (seat_catalog.CC_AGENT_FRONTMATTER_MODELS); any listed model may
# carry the 1M suffix.
CLAUDE_MODEL_ALIASES = ("default", "best", "opus", "sonnet", "haiku", "fable",
                        "opusplan")
ONE_M = "[1m]"
MAX_WINDOW = 10000000
# The SHAPE of a model id. It lists nothing, so it keeps nothing a capture
# writes; it reads only Claude Code's own transcript records (never
# persisted), where the model a session runs must be named even when the
# catalog has retired it.
_MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,99}(\[1m\])?$")
# a harness version: plain N.N.N, never a prerelease or build suffix
_VERSION = re.compile(r"\A\d+\.\d+\.\d+\Z")
# Claude's own model-id grammar: claude-<family>-<version parts>, lowercase,
# digits and dashes, bounded; a retired id the catalog no longer lists is
# still named by it. The family is one Claude names, never free letters.
CLAUDE_FAMILIES = ("opus", "sonnet", "haiku", "fable")
_CLAUDE_ID = re.compile(r"\Aclaude-(?:%s)(?:-\d+)+\Z" % "|".join(CLAUDE_FAMILIES))
MAX_MODEL_ID = 64
_UUID = re.compile(r"\A[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}"
                   r"-[0-9a-f]{12}\Z")
_STAMP = re.compile(r"\A\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z")
# accounts.measured_key's shape: an opaque handle, never an address
_ACCOUNT_KEY = re.compile(r"\Am-[0-9a-f]{16}\Z")
CLAUDE_TOOLS = frozenset((
    "Agent", "Task", "Bash", "BashOutput", "KillShell", "KillBash", "Read",
    "Write", "Edit", "MultiEdit", "Glob", "Grep", "LS", "NotebookEdit",
    "NotebookRead", "WebFetch", "WebSearch", "TodoWrite", "TodoRead",
    "TaskCreate", "TaskGet", "TaskList", "TaskUpdate", "TaskOutput",
    "TaskStop", "ToolSearch", "Skill", "SlashCommand", "EnterPlanMode",
    "ExitPlanMode", "AskUserQuestion", "EnterWorktree", "ExitWorktree",
    "Monitor", "SendMessage", "ListAgents", "Workflow", "LSP",
    "ListMcpResourcesTool", "ReadMcpResourceTool", "ReadMcpResourceDirTool",
    "CronCreate", "CronDelete", "CronList", "ScheduleWakeup",
    "PushNotification", "Artifact", "ArtifactComments", "ArtifactData",
    "DesignSync", "ReportFindings"))

# the transcript is read from its TAIL, widening once when the first window
# held no permission mode or no model its own attachment or a /model names
# (a big session writes ~4 MB an hour)
TAIL_WINDOWS = (8 << 20, 64 << 20)
_TAIL_KEYS = (b'"assistant"', b'"model"', b"permissionMode", b"ultra_effort_",
              b"ownerAccountUuid", b"/model")
_MODEL_COMMAND = "<command-name>/model</command-name>"

LIVE, CAPTURE, LAUNCH, TRANSCRIPT = ("live process", "capture", "last launch",
                                    "transcript")


def default_home():
    """The default credential home: ~/.claude, run with CLAUDE_CONFIG_DIR
    unset (sessions.is_pinnable says why it is never pinned)."""
    return os.path.realpath(os.path.expanduser("~/.claude"))


def _unseamed(value):
    """A CLAUDE_CONFIG_DIR value with the eval seam a proxy launch line
    spells it through, `${HELM_EVAL_CONFIG_DIR:-<dir>}`, taken off."""
    value = (value or "").strip()
    if value.startswith(_EVAL_SEAM) and value.endswith("}"):
        value = value[len(_EVAL_SEAM):-1].replace("\\", "")
    return value


def config_path(value):
    """The credential home a CLAUDE_CONFIG_DIR value names, as a real path;
    an unset or empty value is the default home. A proxy launch line spells
    it through the eval seam, whose default is the seat's own dir."""
    value = _unseamed(value)
    return os.path.realpath(os.path.expanduser(value)) if value \
        else default_home()


def normal_mode(mode):
    """A permission mode as `--permission-mode` spells it: the transcript's
    `default` is the CLI's `manual` (seat_rehome._mode_of reads it the same)."""
    return "manual" if mode == "default" else mode


def permission_words(mode):
    """The argv words that start claude in `mode`."""
    if mode == BYPASS:
        return [SKIP_FLAG]
    return [MODE_FLAG, normal_mode(mode)] if mode else []


def effort_text(level, ultracode):
    return "%s%s" % (level or "default", "+ultracode" if ultracode else "")


# ------------------------------------------------------------ allow-lists

def known_model(value):
    """Is `value` a model the seat catalog knows? A family's catalogued model
    (seat_catalog.family_catalogued_models), a subagent tier's or an
    instance's, a Claude id the catalog routes (CC_AGENT_FRONTMATTER_MODELS)
    or a Claude Code alias (CLAUDE_MODEL_ALIASES), with or without the 1M
    suffix. A LIST, never a shape: a token shaped like a model id is not
    one."""
    if not isinstance(value, str):
        return False
    base = value[:-len(ONE_M)] if value.endswith(ONE_M) else value
    return base in _catalog_models()


def _catalog_models():
    from . import seat  # noqa: F401 — the facade, before an impl module
    from .seat_catalog import (CC_AGENT_FRONTMATTER_MODELS, FAMILIES,
                               family_catalogued_models)
    out = set(CLAUDE_MODEL_ALIASES) | set(CC_AGENT_FRONTMATTER_MODELS)
    for fam in FAMILIES.values():
        out.update(family_catalogued_models(fam))
        for table in ("subagent_tiers", "instance_models"):
            out.update(m for m in (fam.get(table) or {}).values()
                       if isinstance(m, str))
    return out


def is_mode(value):
    """A mode claude's --permission-mode takes (seat_rehome.PERMISSION_MODES)."""
    from .seat_rehome import PERMISSION_MODES
    return isinstance(value, str) and value in PERMISSION_MODES


def is_effort(value):
    """A level claude's --effort takes (remote_session.EFFORTS)."""
    from .remote_session import EFFORTS
    return isinstance(value, str) and value in EFFORTS


def is_tool(value):
    """A tool Claude Code ships (CLAUDE_TOOLS), or a deny rule helm's catalog
    mints for any family (seat_catalog.denied_tools), a retired one too."""
    from . import seat  # noqa: F401 — the facade, before an impl module
    from .seat_catalog import FAMILIES, RETIRED_SPAWN_DENIES, denied_tools
    if not isinstance(value, str):
        return False
    return value in CLAUDE_TOOLS or value in RETIRED_SPAWN_DENIES \
        or any(value in denied_tools(family) for family in FAMILIES)


def is_catalog_line(value):
    """A family's catalog `system_line`: helm's own text, and no other."""
    from . import seat  # noqa: F401 — the facade, before an impl module
    from .seat_catalog import FAMILIES
    return isinstance(value, str) and any(
        fam.get("system_line") == value for fam in FAMILIES.values())


def is_ultracode_settings(value):
    """The one --settings value a capture writes: {"ultracode": <bool>}."""
    try:
        body = json.loads(value)
    except (TypeError, ValueError):
        return False
    return isinstance(body, dict) and list(body) == ["ultracode"] \
        and isinstance(body["ultracode"], bool)


def takes_no_value(_value):
    """The skip flag takes no value, so no word after it is ever kept."""
    return False


def is_seat_name(value):
    """A name by helm's seat-name rule (home._SEAT_NAME_RE)."""
    from . import home
    return isinstance(value, str) and bool(home._SEAT_NAME_RE.fullmatch(value))


def is_home_path(value):
    """Does a CLAUDE_CONFIG_DIR value name a home helm KNOWS? Exactly the
    default ~/.claude (or unset), exactly helm's own home, a seat home helm
    mints (`_is_seat_home`), or a named credhome that exists on disk — a
    directory directly under ~/.claude-homes, the registry `helm cred list`
    reads (cred.is_credhome). NEVER BY PREFIX: a child name under a root is
    free text, and a path is never kept for lying under one."""
    if not isinstance(value, str):
        return False
    raw = os.path.expanduser(_unseamed(value))
    if not raw:
        return True
    if not os.path.isabs(raw):
        return False
    from . import home
    from .cred import is_credhome
    path = config_path(value)
    return path in (default_home(), os.path.realpath(home.helm_home())) \
        or (os.path.isdir(path) and is_credhome(path)) or _is_seat_home(path)


def _is_seat_home(path):
    """A seat home helm mints: <seats>/<family>/claude for a catalogued
    family, or <seats>/<family>/instances/<seat>/claude for an instance whose
    seat directory exists (helm made it) and whose name passes the seat-name
    rule (seat_launch_assets._instance_dir)."""
    from . import seat  # noqa: F401 — the facade, before an impl module
    from .seat_paths import seats_root
    root = os.path.realpath(seats_root())
    parts = os.path.relpath(path, root).split(os.sep)
    if not is_family(parts[0]) or parts[-1] != "claude":
        return False
    return len(parts) == 2 or (
        len(parts) == 4 and parts[1] == "instances" and is_seat_name(parts[2])
        and os.path.isdir(os.path.join(root, *parts[:3])))


def is_family(value):
    """A family the seat catalog declares."""
    from . import seat  # noqa: F401 — the facade, before an impl module
    from .seat_catalog import FAMILIES
    return isinstance(value, str) and value in FAMILIES


def is_window(value):
    """A context window: a whole number of tokens, 1 to MAX_WINDOW."""
    return isinstance(value, str) and value.isascii() and value.isdigit() \
        and 0 < int(value) <= MAX_WINDOW


def echoable_model(value):
    """A model a recipe may keep and name: one the catalog or an alias knows
    (`known_model`), or one in Claude's own id grammar (_CLAUDE_ID), so a
    retired Claude id is still named. Anything else is 'a model the catalog
    does not know', withheld by field."""
    if known_model(value):
        return True
    base = value[:-len(ONE_M)] if isinstance(value, str) \
        and value.endswith(ONE_M) else value
    return isinstance(base, str) and len(value) <= MAX_MODEL_ID \
        and bool(_CLAUDE_ID.match(base))


def names_wide(model, reply):
    """Does `model` (a launch's or a /model argument's) name the 1M variant
    of the id `reply` names, which a reply always shows without the suffix?
    Exactly that id with it, or its family's alias with it (`opus[1m]` for
    a claude-opus-… reply)."""
    if not (isinstance(model, str) and model.endswith(ONE_M)
            and isinstance(reply, str)):
        return False
    base = model[:-len(ONE_M)]
    return base == reply or (base in CLAUDE_FAMILIES
                             and bool(_CLAUDE_ID.match(reply))
                             and reply.startswith("claude-%s-" % base))


def _model_shaped(value):
    """Is `value` shaped as one model id? For the transcript alone (see
    _MODEL_ID); a capture keeps only `known_model`."""
    return isinstance(value, str) and bool(_MODEL_ID.match(value))


# Each argv flag a capture can write, with the list its value must pass.
ARGV_KINDS = {"--model": known_model, MODE_FLAG: is_mode, "--effort": is_effort,
              "--disallowedTools": is_tool, "--disallowed-tools": is_tool,
              "--append-system-prompt": is_catalog_line,
              "--settings": is_ultracode_settings, SKIP_FLAG: takes_no_value}
# Each environ name a capture keeps: the recipe field it states, and the list
# its value must pass. No other name is read, the bearer first.
ENV_KINDS = {"HELM_CHAT_NAME": ("identity_env", is_seat_name),
             "HELM_CELL_PROFILE": ("identity_env", is_seat_name),
             "DREGG_PROFILE": ("identity_env", is_seat_name),
             CONFIG_KEY: ("config_dir", is_home_path),
             FAMILY_KEY: ("family", is_family),
             WINDOW_KEY: ("window", is_window),
             SUBAGENT_KEY: ("subagent", known_model)}
CAPTURE_ENV = tuple(ENV_KINDS)
# Every key a capture record writes, with the list its value must pass: each
# answers the value it keeps, or None (not kept). `_record` writes through
# this table and nothing else, so a key with no list here is never written.
CAPTURE_KEYS = {
    "version": lambda v: v if isinstance(v, str) and _VERSION.match(v)
    else None,
    # read back through ARGV_KINDS: the words it keeps re-read as themselves
    "argv": lambda words: _recipe_words(argv_facts(words)),
    "withheld": lambda names: [f for f in WITHHOLDABLE if f in names]
    if isinstance(names, (list, tuple)) else [],
    "env": lambda env: {k: v for k, v in env.items()
                        if k in ENV_KINDS and ENV_KINDS[k][1](v)}
    if isinstance(env, dict) else {},
    "pid": lambda pid: pid if type(pid) is int and pid > 0 else None,
    # a birth stamp: /proc/<pid>/stat field 22, digits (an int is spelled)
    "start": lambda stamp: str(stamp) if isinstance(stamp, (int, str))
    and not isinstance(stamp, bool) and str(stamp).isascii()
    and str(stamp).isdigit() else None,
    "sessions": lambda ids: [s for s in ids
                             if isinstance(s, str) and _UUID.match(s)]
    if isinstance(ids, (list, tuple)) else [],
    "captured": lambda ts: ts if isinstance(ts, str) and _STAMP.match(ts)
    else None,
    # the options the resume does not restore (`dropped_flags`): only names
    # on claude's own list, never a value, and a count of the rest
    "dropped": lambda names: [n for i, n in enumerate(names)
                              if isinstance(n, str) and n in CLAUDE_FLAGS
                              and n not in names[:i]] or None
    if isinstance(names, (list, tuple)) else None,
    "unlisted_options": lambda n: n if type(n) is int
    and 0 < n <= MAX_UNLISTED
    else None,
    # the account its home held when it was captured, as the measured key of
    # the account email (`account_key`), never the address itself
    "account": lambda k: k if isinstance(k, str) and _ACCOUNT_KEY.match(k)
    else None,
}
# Every field of the ASSEMBLED recipe, and every recorded fact, with the list
# its value must pass WHATEVER ITS SOURCE: read() applies these once, where it
# returns (`_vet`). The transcript's facts rank above a checked argv, and a
# synthetic transcript can carry anything. cwd alone has none: it is the
# directory the session resumes from, needed verbatim (claude resume is
# cwd-scoped), and it comes from the session's own row, never a process.
RECIPE_CHECKS = {
    "model": echoable_model,
    "config_dir": is_home_path,
    "permission": is_mode,
    "effort": lambda e: isinstance(e, dict)
    and (e.get("level") is None or is_effort(e["level"]))
    and isinstance(e.get("ultracode"), bool),
    "disallowed_tools": lambda tools: isinstance(tools, list)
    and all(is_tool(t) for t in tools),
    "append_system_prompt": lambda v: v is None or is_catalog_line(v),
    "identity_env": lambda env: isinstance(env, dict)
    and set(env) <= set(IDENTITY_KEYS)
    and all(v is None or is_seat_name(v) for v in env.values()),
    "family": lambda v: v is None or is_family(v),
    "window": lambda v: v is None or (type(v) is int and 0 < v <= MAX_WINDOW),
    "subagent": lambda v: v is None or known_model(v),
    "harness": lambda v: v == "claude",
    "harness_version": lambda v: isinstance(v, str) and bool(_VERSION.match(v)),
    "account": lambda v: isinstance(v, str) and bool(_UUID.match(v)),
}


# ----------------------------------------------------------------- readers

def _hold(facts, field):
    """Record `field` as withheld in argv or env facts, its value dropped."""
    key = "effort_level" if field == "effort" else field
    facts[key] = [] if key == "disallowed_tools" else None
    if field not in facts["withheld"]:
        facts["withheld"].append(field)


def _hold_named(a, e, withheld):
    """Hold, in argv facts `a` and env facts `e`, each field a record's own
    `withheld` list names (any other shape names nothing)."""
    for field in withheld if isinstance(withheld, (list, tuple)) else ():
        if field in ARGV_FIELDS:
            _hold(a, field)
        elif field in ENV_FIELDS:
            _hold(e, field)


def argv_facts(words):
    """What a claude argv (the words after the binary) says, as recipe
    fields. A flag's ABSENCE is a known value for the fields only a flag can
    set (no --disallowedTools is no disallowed tools); for the ones a
    settings file can also set (model, permission, effort) absence is None,
    which the caller reads as "this source does not say".

    ONLY ALLOW-LISTED VALUES ARE RETURNED (ARGV_KINDS). A value outside its
    flag's list is dropped here, at the one reader every source goes through
    (the live process, a capture, a launch.sh, the line helm would mint), and
    its field is named in `withheld`: so no caller can keep, print or log it,
    and none can read the flag as absent. Once withheld, a field stays
    withheld whatever a later occurrence of its flag says."""
    out = {"model": None, "permission": None, "effort_level": None,
           "ultracode": None, "disallowed_tools": [],
           "append_system_prompt": None, "withheld": []}
    words = [w for w in words if isinstance(w, str)] \
        if isinstance(words, (list, tuple)) else []
    i = 0
    while i < len(words):
        word = words[i]
        name, eq, inline = (word.partition("=") if word.startswith("--")
                            else (word, "", ""))
        i += 1
        if name in _DENY_FLAGS:
            values = [inline] if eq else []
            while not eq and i < len(words) and not words[i].startswith("-"):
                values.append(words[i])
                i += 1
            values = [v for v in values if v]
            if all(is_tool(v) for v in values):
                out["disallowed_tools"] += values
            else:
                _hold(out, "disallowed_tools")
        elif name == SKIP_FLAG:
            out["permission"] = BYPASS
        elif name in _VALUED:
            if eq:
                value = inline
            elif i < len(words):
                value, i = words[i], i + 1
            else:
                value = None
            key = _VALUED[name]
            if key == "settings":
                try:
                    body = json.loads(value or "")
                except ValueError:
                    body = None
                if isinstance(body, dict) and "ultracode" in body:
                    out["ultracode"] = bool(body["ultracode"])
                continue
            value = normal_mode(value) if key == "permission" else value
            if value is None or ARGV_KINDS[name](value):
                out[key] = value
            else:
                _hold(out, "effort" if key == "effort_level" else key)
    for field in list(out["withheld"]):
        _hold(out, field)
    return out


def env_facts(env):
    """What an environment says, as recipe fields. Only ENV_KINDS names are
    read, and each value must pass its kind's list: one that does not is
    dropped and its FIELD named in `withheld` (one bad identity name
    withholds the whole identity)."""
    env = env if isinstance(env, dict) else {}
    held = []
    for name, (field, ok) in ENV_KINDS.items():
        if name in env and not ok(env[name]) and field not in held:
            held.append(field)
    good = {name: env[name] for name, (field, _ok) in ENV_KINDS.items()
            if name in env and field not in held}
    window = good.get(WINDOW_KEY)
    out = {"config_dir": config_path(good.get(CONFIG_KEY)),
           "identity_env": {k: good.get(k) for k in IDENTITY_KEYS},
           "window": int(window) if window else None,
           "subagent": good.get(SUBAGENT_KEY) or None,
           "family": good.get(FAMILY_KEY) or None, "withheld": []}
    for field in held:
        _hold(out, field)
    return out


def recipe_argv(words):
    """The recipe's own words of a claude argv, and nothing else: the words
    `argv_facts` reads, in the closed vocabulary, spelled back so they read
    as the same recipe, with --settings cut to its ultracode key. Every other
    word — a session flag, a positional prompt, an --mcp-config, a --settings
    env block, free text in --append-system-prompt or --disallowedTools — is
    dropped, because a capture is kept on disk and carried forward. What
    free text a capture dropped is kept by FIELD NAME, beside the words
    (`capture_record`)."""
    return _recipe_words(argv_facts(words))


def _recipe_words(a):
    out = (["--disallowedTools"] + a["disallowed_tools"]
           if a["disallowed_tools"] else [])
    out += permission_words(a["permission"])
    for flag, key in (("--model", "model"), ("--effort", "effort_level"),
                      ("--append-system-prompt", "append_system_prompt")):
        if a[key] is not None:
            out += [flag, a[key]]
    if a["ultracode"] is not None:
        out += ["--settings", json.dumps({"ultracode": a["ultracode"]})]
    return out


def strip_session(words):
    """An argv minus the flags that name a SESSION: those belong to one
    conversation, and the resume supplies its own."""
    out, words, i = [], list(words or ()), 0
    while i < len(words):
        word = words[i]
        if word in _SESSION_VALUED:
            i += 2
            continue
        if word in _SESSION_FLAGS or word.split("=", 1)[0] in _SESSION_VALUED:
            i += 1
            continue
        out.append(word)
        i += 1
    return out


def dropped_flags(words):
    """(names, unlisted): the options of a claude argv that a resume does
    not restore. `names` are those on claude's own list (CLAUDE_FLAGS), each
    once, in argv order; `unlisted` counts every other option-shaped word.
    NO VALUE IS READ: the value word of an option the recipe knows is
    skipped, a word after the `--` terminator is prose, and the recipe's own
    flags and the session flags (the resume names its own session) are not
    dropped. --settings is dropped when its value is anything but the
    ultracode key alone."""
    names, unlisted = [], 0
    words = [w for w in words if isinstance(w, str)] \
        if isinstance(words, (list, tuple)) else []
    i = 0
    while i < len(words):
        word = words[i]
        i += 1
        if word == "--":
            break
        if not word.startswith("-") or word == "-":
            continue
        name, eq, value = (word.partition("=") if word.startswith("--")
                           else (word, "", ""))
        if name in _DENY_FLAGS:
            while not eq and i < len(words) and not words[i].startswith("-"):
                i += 1
            continue
        if name in _VALUED or name in _SESSION_VALUED:
            if not eq and i < len(words):
                value, i = words[i], i + 1
            if name == "--settings" and not is_ultracode_settings(value) \
                    and name not in names:
                names.append(name)
            continue
        if name == SKIP_FLAG or name in _SESSION_FLAGS:
            continue
        if name in CLAUDE_FLAGS:
            if name not in names:
                names.append(name)
        else:
            unlisted += 1
    return names, unlisted


def not_restored(r):
    """The line naming each option the launch it ran carried that the
    resume does not pass, by NAME (never a value), or None."""
    names, unlisted = r.dropped
    more = ("%d%s option%s outside claude's own list"
            % (unlisted, " more" if names else "",
               "" if unlisted == 1 else "s")) if unlisted else None
    parts = list(names) + (["and " + more] if names and more else
                           [more] if more else [])
    return "not restored: " + ", ".join(parts) if parts else None


def launch_capture(text):
    """{"argv", "env"} of the `claude` command a launch text runs, or None.
    Read shell-aware with the nested re-split `seat_remint.launch_fields`
    documents: the launch owner hands the whole line to its supervisor as
    ONE quoted argument. `argv` ends before the script's own "$@"."""
    try:
        tokens = shlex.split(text, comments=True)
    except ValueError:
        return None
    runs = [tokens]
    for token in tokens:
        if " claude " in token:
            try:
                runs.append(shlex.split(token))
            except ValueError:
                pass
    for run in reversed(runs):
        starts = [i for i, t in enumerate(run) if t == "claude"]
        if not starts:
            continue
        c = starts[-1]
        env_at = max([i for i in range(c) if run[i] == "env"], default=-1)
        env = {}
        for word in run[env_at + 1:c]:
            name, eq, value = word.partition("=")
            if eq and name in CAPTURE_ENV:
                env[name] = value
        argv = [w for w in run[c + 1:] if w not in ("$@", '"$@"')]
        names, unlisted = dropped_flags(argv)
        return {"argv": strip_session(argv), "env": env, "dropped": names,
                "unlisted_options": unlisted}
    return None


def read_launch(path):
    """launch_capture of a launch.sh on disk, or None."""
    try:
        with open(path) as f:
            return launch_capture(f.read())
    except (OSError, ValueError):
        return None


def _record(**raw):
    """A capture record, key by key through CAPTURE_KEYS: a key with no
    allow-list raises KeyError, so it is never written, and a value its list
    refuses (None back) is not kept."""
    out = {}
    for key, value in raw.items():
        kept = CAPTURE_KEYS[key](value)
        if kept is not None:
            out[key] = kept
    return out


def capture_record(argv, env, binary=None, withheld=(), **facts):
    """A capture as it is kept on disk, built only through the allow-lists:
    the argv words and env values that pass their field's list, `withheld`
    naming each field one did not pass (and each field `withheld` already
    names), the version a binary's versions layout states (never its path),
    and the record's own `facts` (pid, start, sessions, captured, and a
    carried record's version)."""
    a, e = argv_facts(argv), env_facts(env)
    _hold_named(a, e, withheld)
    env = env if isinstance(env, dict) else {}
    # the options it drops, read off the whole argv; a carried record's
    # argv is already cut to the recipe's words, so its own list stands
    names, unlisted = dropped_flags(argv)
    facts.setdefault("dropped", names)
    facts.setdefault("unlisted_options", unlisted)
    # a version is STATED by a carried record's `version` or a binary in the
    # versions layout; one stated outside N.N.N is withheld by name
    stated = "version" in facts or _versions_layout(binary)
    facts.setdefault("version", _binary_version(binary))
    held = [f for f in ("harness_version",)
            if (stated and not CAPTURE_KEYS["version"](facts["version"]))
            or (isinstance(withheld, (list, tuple)) and f in withheld)]
    return _record(argv=_recipe_words(a),
                   withheld=a["withheld"] + e["withheld"] + held,
                   env={k: v for k, v in env.items()
                        if k in ENV_KINDS and ENV_KINDS[k][0] not in e["withheld"]},
                   **facts)


def capture_process(proc, **facts):
    """The capture record (`capture_record`) of a live claude process, with
    `facts` beside it, or None when either read fails (an unread process is
    never an empty one). The account its home holds is read now, while it
    runs, and kept as a key (`account_key`): the side of the resume's
    account check that no later read can supply."""
    from . import beacons
    argv = beacons.proc_argv(int(proc))
    env = beacons.proc_env(int(proc))
    if not argv or env is None:
        return None
    home = env.get(CONFIG_KEY)
    if "account" not in facts and is_home_path(home or ""):
        facts["account"] = account_key(config_path(home))[0]
    return capture_record(argv[1:], env, argv[0], **facts)


def account_key(home):
    """(key, why): the measured key (accounts.measured_key) of the account
    email a credential home holds, read by cred.account_of, or (None, why
    it could not be read). The default home keeps its account one level up,
    in ~/.claude.json."""
    from . import accounts
    from .cred import account
    got = account.account_of(os.path.dirname(home) if home == default_home()
                             else home)
    if got.get("ok") and got.get("email"):
        return accounts.measured_key(got["email"]), None
    return None, got.get("error") or "no account could be read"


def _versions_layout(binary):
    """Is `binary` a path in claude's versions layout (…/versions/<name>)?"""
    parent, base = os.path.split(str(binary or ""))
    return bool(base) and os.path.basename(parent) == "versions"


def _binary_version(binary):
    """The plain N.N.N version a claude binary path names
    (…/claude/versions/2.1.280), or None."""
    base = os.path.basename(str(binary or ""))
    return base if _versions_layout(binary) and _VERSION.match(base) else None


def transcript_facts(path):
    """What a session's own transcript says about how it runs NOW, from its
    tail. Keys present only when the transcript states them: model (with
    `model_source`), permission, effort_level, ultracode, harness_version,
    account, and `replies` (it holds at least one main-chain reply)."""
    try:
        size = os.path.getsize(path)
    except (OSError, TypeError):
        return {}
    facts = {}
    for window in TAIL_WINDOWS:
        facts = _tail_facts(path, size, window)
        # A REPLY NAMES THE MODEL WITHOUT ITS 1M SUFFIX, so a model read from
        # a reply alone is not settled while an attachment may lie further
        # back (it is written at launch, at a /model and after a
        # compaction, and a large session outgrows the first window between
        # them): keep widening until the attachment, a /model or the
        # widest window answers.
        settled = "model_unknown" in facts or \
            facts.get("model_source") not in (None, "last reply")
        if (settled and "permission" in facts) or window >= size:
            break
    return facts


def _tail_facts(path, size, window):
    try:
        with open(path, "rb") as f:
            start = max(0, size - window)
            f.seek(start)
            lines = f.read().split(b"\n")
    except OSError:
        return {}
    if start:
        lines = lines[1:]                  # the first line is cut mid-record
    facts, att, cmd, reply = {}, None, None, None
    for i, raw in enumerate(lines):
        if not any(key in raw for key in _TAIL_KEYS):
            continue
        try:
            e = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(e, dict) or e.get("isSidechain"):
            continue
        kind = e.get("type")
        if isinstance(e.get("permissionMode"), str):
            facts["permission"] = normal_mode(e["permissionMode"])
        if isinstance(e.get("version"), str):
            facts["harness_version"] = e["version"]
        if kind == "assistant":
            msg = e.get("message")
            model = msg.get("model") if isinstance(msg, dict) else None
            if _model_shaped(model):       # never a `<synthetic>` row
                reply = (i, model)
                if isinstance(e.get("effort"), str):
                    facts["effort_level"] = e["effort"]
        elif kind == "attachment":
            a = e.get("attachment") if isinstance(e.get("attachment"),
                                                  dict) else {}
            if a.get("type") == "model":
                ident = a.get("identity") if isinstance(a.get("identity"),
                                                        dict) else {}
                if _model_shaped(ident.get("modelId")):
                    att = (i, ident["modelId"])
            elif a.get("type") in ("ultra_effort_enter", "ultra_effort_exit"):
                facts["ultracode"] = a["type"] == "ultra_effort_enter"
        elif kind == "bridge-session" \
                and isinstance(e.get("ownerAccountUuid"), str):
            facts["account"] = e["ownerAccountUuid"]
        elif kind == "user":
            msg = e.get("message")
            content = msg.get("content") if isinstance(msg, dict) else None
            if isinstance(content, str) and _MODEL_COMMAND in content:
                args = content.partition("<command-args>")[2]
                cmd = (i, args.partition("<")[0].strip())
    # THE MODEL: Claude Code's own `model` attachment names the exact id it
    # runs (the 1M variant included, which a reply's model field drops) and
    # is written again after every compaction. A /model after the last one
    # moved it: then the first reply after the command says where to, or the
    # command's own argument does — typed by a person, so it names a model
    # only when the catalog, an alias or Claude's id grammar knows it, and
    # anything else names nothing.
    #
    # A REPLY DROPS THE 1M SUFFIX: after `/model <id>[1m]` (or the alias
    # form) the command's argument carries it, and after a menu pick (an
    # empty argument) nothing does, so on a Claude id the model is unknown.
    if att and not (cmd and cmd[0] > att[0]):
        facts["model"], facts["model_source"] = att[1], "model attachment"
    elif cmd and reply and reply[0] > cmd[0]:
        wide = names_wide(cmd[1], reply[1])
        if wide or not _CLAUDE_ID.match(reply[1]) or _CLAUDE_ID.match(cmd[1]):
            facts["model"] = reply[1] + ONE_M if wide else reply[1]
            facts["model_source"] = "reply after /model" + (
                "; its 1M suffix: the /model argument" if wide else "")
        else:
            # a menu pick, or a typed alias (`/model default`, `/model
            # opus`), may select the 1M model, and the reply shows none
            facts["model_unknown"] = (
                "%s after its last model record may select the 1M model, and "
                "the reply after it names a Claude id without the suffix" % (
                    "a /model menu pick" if not cmd[1]
                    else "`/model %s`" % cmd[1]
                    if cmd[1] in CLAUDE_MODEL_ALIASES
                    else "a /model argument that is not a Claude id"))
    elif cmd and echoable_model(cmd[1]):
        facts["model"], facts["model_source"] = cmd[1], "/model argument"
    elif reply and not cmd:
        facts["model"], facts["model_source"] = reply[1], "last reply"
    facts["replies"] = reply is not None
    return facts


# ------------------------------------------------------- capture and store

def _live_set_paths():
    """The resume live-set records, newest first."""
    from . import seat_resume_all
    found = []
    for path in glob.glob(os.path.join(glob.escape(
            seat_resume_all.live_set_dir()), "*.json")):
        try:
            found.append((os.path.getmtime(path), path))
        except OSError:
            continue
    return [path for _mtime, path in sorted(found, reverse=True)]


def _recipes_of(path):
    doc = pk.read_json(path, None)
    recipes = doc.get("recipes") if isinstance(doc, dict) else None
    return recipes if isinstance(recipes, dict) else None


def captured(seat, session):
    """(record, path) — the newest capture of `seat` whose process held
    `session`, or (None, None). A capture of another session is another
    process's launch, so it never speaks for this one."""
    if not session:
        return None, None
    for path in _live_set_paths():
        rec = (_recipes_of(path) or {}).get(seat)
        # read through the same lists it was written through: a record an
        # earlier build kept whole is never read, or printed, whole
        rec = _recut(rec) if isinstance(rec, dict) else None
        if rec and session in rec.get("sessions", ()):
            return rec, path
    return None, None


def source_session(transcript, session):
    """The session a cv-pruned copy was pruned from, read from the copy's own
    lineage stamp (seat_lifecycle_sessions.prune_lineage), or None. A pruned
    copy is the same conversation under a new id, so the capture of the
    process that held its source binds it — but only when the stamp's own
    half names THIS copy, `session`, and its source half names exactly the
    source the resume asked for (read()'s `source`): the context-wall
    recovery and a rescue resume of the copy come back exactly as the
    source ran. A copy whose lineage is unrecorded, or whose stamp names
    another copy or another source, binds nothing."""
    from . import seat  # noqa: F401 — the facade, before an impl module
    from .seat_lifecycle_sessions import prune_lineage
    stamp = prune_lineage(transcript) if transcript else None
    if not isinstance(stamp, tuple) or stamp[1] != session:
        return None
    return stamp[0] if stamp[0] != session else None


def carried(fresh):
    """The recipes map a live-set record writes: the newest readable one's,
    carried forward, with this pass's `fresh` captures over it. A seat's entry
    outlives its process that way, which is the point: it is read when the
    seat is gone. The same process captured again keeps every session it has
    held (a /clear moves the session, never the argv). SAME means the pid AND
    its birth stamp: a stamp that could not be read never matches, so a
    reused pid is never credited with the sessions of the process before
    it. A carried record is CUT AGAIN as a fresh capture is
    (`capture_record`): one an earlier build kept whole, free text and all,
    is never written again."""
    prior = newest_recipes()
    out = dict(prior)
    for seat, rec in fresh.items():
        out[seat] = _joined(rec, prior.get(seat))
    return out


def _joined(rec, was):
    """`rec` holding every session `was` held too, when the two are captures
    of the SAME process (its pid and its birth stamp); else `rec` alone."""
    was = was if isinstance(was, dict) else {}
    if rec.get("start") is None or \
            (was.get("pid"), was.get("start")) != (rec.get("pid"),
                                                   rec.get("start")):
        return rec
    return dict(rec, sessions=list(dict.fromkeys(
        list(was.get("sessions") or ()) + list(rec.get("sessions") or ()))))


def newest_recipes():
    """The recipes map of the newest live-set record that carries one, each
    record cut again through the lists (`_recut`), or {}."""
    for path in _live_set_paths():
        recipes = _recipes_of(path)
        if recipes is not None:
            return {seat: _recut(rec) for seat, rec in recipes.items()
                    if is_seat_name(seat) and isinstance(rec, dict)}
    return {}


def keep_newest(mine, theirs):
    """A pass's recipes map `mine` written over `theirs`, the map on disk
    when it writes: per seat the capture taken LATER wins, and a seat only
    one side holds is kept. Two sweep passes can run at once (the timer and
    a hand pass), and one that read the map before the other wrote it must
    never write its older capture over the newer one. Two captures of the
    same process keep every session either saw (`_joined`)."""
    out = dict(theirs)
    for seat, rec in mine.items():
        was = out.get(seat)
        took = pk.parse_ts_epoch((rec or {}).get("captured"))
        had = pk.parse_ts_epoch((was or {}).get("captured"))
        if was is None or had is None or (took is not None and took >= had):
            out[seat] = _joined(rec, was)
        else:
            out[seat] = _joined(was, rec)
    return out


def _recut(rec):
    """A carried record cut again through the same lists, key by key; a key
    with no list (a pre-cure record's whole `binary` path among them) is not
    carried."""
    return capture_record(rec.get("argv"), rec.get("env"), rec.get("binary"),
                          rec.get("withheld"),
                          **{k: rec[k] for k in CAPTURE_KEYS
                             if k in rec and k not in ("argv", "env",
                                                       "withheld")})


def _one_interactive(pids):
    """The ONE interactive claude among `pids`, or None. A `claude -p` helper
    inherits its seat's name and is not the seat; two interactive processes
    are ambiguous and capture nothing."""
    from . import beacons
    pids = [p for p in pids or ()
            if not {"-p", "--print"} & set(beacons.proc_argv(int(p)) or ())]
    return pids[0] if len(pids) == 1 else None


def live_pid(seat):
    """The seat's one interactive claude process, read through
    `orcaadopt.resolve` (the `seat where` reading), or None."""
    from . import orcaadopt
    try:
        info = orcaadopt.resolve(seat)
    except Exception:            # noqa: BLE001 — a reading is never worth a crash
        return None
    return _one_interactive((info or {}).get("pids"))


def sweep_pid(seat_name, pids=None, session=None):
    """A LIVE row's process for the sweep's capture, from what the sweep's
    own proof already read — an adopted row's resolved `pids`, else a
    spawned seat's register pin to `session` (`session_pid`: a process
    pinned to another session is not this row's) — and never a /proc walk
    of its own: the sweep is a timer payload, and a healthy fleet costs it
    none."""
    if pids is not None:
        return _one_interactive(pids)
    from . import seat
    family, err = seat._seat_family(seat_name)
    if err or family not in seat.FAMILIES:
        return None
    rec = seat._spawn_record(seat._instance_dir(family, seat_name)) or {}
    return session_pid(rec if rec.get("seat") == seat_name else {}, session)


def capture_seats(rows):
    """{seat: capture} for the (seat, session, pids) the sweep found LIVE.
    `start` is the process's birth stamp: an adopted row's resolved pid
    carries it, a spawned seat's register pin is a bare int, so it is read."""
    from . import orcaadopt
    out = {}
    for seat, session, pids in rows:
        pid = sweep_pid(seat, pids, session) if is_seat_name(seat) else None
        if pid is None:
            continue
        cap = capture_process(
            pid, pid=int(pid),
            start=getattr(pid, "start", None) or orcaadopt.proc_start(pid),
            sessions=[session] if session else [], captured=pk.now_ts())
        if cap:
            out[seat] = cap
    return out


def session_pid(record, session):
    """The live process that holds `session`: the one the seat's spawn
    register pins to it, still running with the same birth stamp, and only
    when the register's session IS `session`. A process holding another
    session is another launch, so it never speaks for this one (THE ONE
    RULE, `capture_binds`)."""
    if not session or (record or {}).get("session") != session:
        return None
    return pinned_pid(record)


def pinned_pid(record):
    """The pid a proxy seat's spawn register pins to its session, when that
    exact process (pid AND birth stamp) still runs; else None. A resume asks
    `session_pid`, which also requires the session."""
    from . import orcaadopt
    pid = (record or {}).get("session_pid")
    ident = (record or {}).get("session_pid_identity")
    if not isinstance(pid, int) or not ident:
        return None
    start = orcaadopt.proc_start(pid)
    return pid if start and ident == "proc:%s" % start else None


# ------------------------------------------------- what binds a resume

# THE ONE RULE (task/3695): a capture binds a resume ONLY when it provably
# describes THIS session's LAST launch —
#   same session    the session is one its process held;
#   same process    it names that process's birth (pid AND start stamp), and
#                   where the seat's spawn register pins a process to the
#                   session it is that process;
#   no later launch no launch of the seat of ANY kind (an exact resume, a
#                   `--defaults` one, a spawn, a move to another home) is
#                   recorded after it was taken: the spawn register's
#                   stamp, or THE LAUNCH RECORD below;
#   the lineage     for a cv-pruned copy, the copy's own stamp names exactly
#                   the source the resume asked for.
# Anything short of that reads as UNCAPTURED. A launch helm keeps no record
# of cannot be seen here: that is the rule's bound, not an exemption.

def register_launch(register, session):
    """((pid, start) the seat's spawn register pins to `session`, or None;
    the epoch of the last launch the register records, or None)."""
    register = register if isinstance(register, dict) else {}
    ident = str(register.get("session_pid_identity") or "")
    pinned = ((register.get("session_pid"), ident[len("proc:"):])
              if register.get("session") == session and session
              and ident.startswith("proc:") else None)
    return pinned, pk.parse_ts_epoch(register.get("ts"))


# THE LAUNCH RECORD: one row for each process start of a session, written by
# helm's SessionStart hook (`seat resume-turn --hook-json`, the hook every
# helm home runs in every project) for the sources that start a process:
# startup, resume (--resume, --continue, an in-session /resume, Orca's own
# relaunch, a hand `claude --resume`) and fork. compact keeps the session in
# the same process and writes none; clear moves that same process onto a new
# session id, and writes a row only when it carries the process's capture (a
# capture-less clear row would unbind the sweep's capture of that process).
# Each row {session, seat, home, model, source,
# via, ts, at, capture}: `capture` is the capture of the process that
# started, read through the CLAUDE_PID Claude Code gives its hooks, so a row
# exists only for a launch that really started and IS its freshest capture.
# Every resume path reads it (`recorded_launch`): the newest row for a
# session decides what binds, and the sweep's capture is the fallback for a
# session no row names (one started before the record existed).
LAUNCHES = "seat-launches.jsonl"
LATER = "a later launch of the seat followed it"
LAUNCH_SOURCES = ("startup", "resume", "fork", "clear")
# THE RECORD FAILS CLOSED AND WAITS BOUNDEDLY: a row it cannot write leaves
# a marker for the session, read as an unread record (nothing binds its next
# resume), and a lock another writer holds is waited on this long, never
# forever, so a wedged holder cannot hang a session start
UNRECORDED = "seat-launches.unrecorded"
LAUNCH_LOCK_WAIT_S = 1.0


def launches_path():
    from . import home as helm_home
    return os.path.join(helm_home.global_dir(), LAUNCHES)


def unrecorded_path(session):
    """The marker a start of `session` the record could not take leaves."""
    from . import home as helm_home
    return os.path.join(helm_home.global_dir(), UNRECORDED, session)


def record_session_start(payload, environ=None):
    """The SessionStart hook's write: the launch row for the process start
    `payload` (Claude Code's hook input) reports, read from the hook's own
    `environ`. None when it wrote one, or the source starts no process;
    else why no row was written, for the hook to say."""
    env = os.environ if environ is None else environ
    source = payload.get("source") if isinstance(payload, dict) else None
    session = payload.get("session_id") if source in LAUNCH_SOURCES else None
    if not (isinstance(session, str) and _UUID.match(session)) \
            or payload.get("agent_id") or _print_mode(env):
        return None
    ts = pk.now_ts()
    cap = _started_process(env, session, ts)
    if source == "clear" and cap is None:
        return None
    home = config_path(env.get(CONFIG_KEY))
    seat = env.get("HELM_CHAT_NAME")
    return _append_launch({
        "v": 1, "event": "seat-launch", "id": os.urandom(16).hex(), "ts": ts,
        "at": time.time(), "session": session,
        "seat": seat if is_seat_name(seat) else None,
        "home": home if is_home_path(home) else None,
        "model": argv_facts((cap or {}).get("argv"))["model"],
        "source": source, "via": "SessionStart %s" % source, "capture": cap})


def _print_mode(env):
    """Is the process a SessionStart hook runs under a `claude -p` helper?
    One run in a seat's cwd (`-p --continue`, `-p --resume <sid>`) starts
    the seat's session in a process that is not the seat, so it writes no
    row: the rule `_one_interactive` applies to the sweep."""
    from . import beacons
    raw = str(env.get("CLAUDE_PID") or "")
    argv = beacons.proc_argv(int(raw)) if raw.isdigit() else None
    return bool({"-p", "--print"} & set(argv or ()))


def _started_process(env, session, ts):
    """The capture of the claude process a SessionStart hook runs under: the
    CLAUDE_PID Claude Code gives its hooks, taken only when that process's
    own session (CLAUDE_CODE_SESSION_ID) is `session`; None when either does
    not read."""
    from . import orcaadopt
    raw = str(env.get("CLAUDE_PID") or "")
    if not raw.isdigit() or env.get("CLAUDE_CODE_SESSION_ID") != session:
        return None
    start = orcaadopt.proc_start(int(raw))
    return capture_process(int(raw), pid=int(raw), start=start,
                           sessions=[session], captured=ts) if start else None


def _append_launch(row):
    """Append one launch row, waiting on its lock at most
    LAUNCH_LOCK_WAIT_S. None, or why it was not written; then the session's
    marker is left (`unrecorded_path`), and when even that cannot be written
    the reason says the next resume may bind a stale capture."""
    from . import eventledger
    try:
        with eventledger.locked(launches_path(),
                                timeout=LAUNCH_LOCK_WAIT_S) as held:
            ok, why = eventledger.append_unlocked_checked(
                launches_path(), row) if held else (
                    False, "its lock was not taken within %gs"
                    % LAUNCH_LOCK_WAIT_S)
    except Exception as e:      # noqa: BLE001 — a session start never fails on its record
        ok, why = False, "%s: %s" % (type(e).__name__, e)
    if ok:
        return None
    marker = unrecorded_path(row["session"])
    try:
        os.makedirs(os.path.dirname(marker), exist_ok=True)
        pk.atomic_write(marker, row["ts"] + "\n")
        left = "marker %s left, so no capture binds its next resume" % marker
    except OSError as e:
        left = ("its marker %s could not be written either (%s), so the next "
                "resume of %s may bind a stale capture" % (
                    marker, e, row["session"]))
    return ("the launch record %s did not take the start of session %s (%s); "
            "%s" % (launches_path(), row["session"], why, left))


def recorded_launch(*sids):
    """The newest launch recorded of any of `sids`: {"at": its epoch,
    "home": its home when the record names one, "via": what recorded it,
    "unread": why a record did not read, "row": the newest launch-record row
    naming one of them (its `capture` the process that started)}, each None
    when unknown. The two records these launches were kept in before it —
    the resume script the adopted resume mints (its mtime) and the `seat
    rehome` ledger — still count, for an entry newer than the newest row (or
    any entry, when no row names them): such an entry is a launch that may
    have started, so it can only UNBIND (a spawn that raised demotes a
    capture to uncaptured, never binds one), and the row's home is then no
    longer the last launch's. A record that does not read may hold a later
    launch, so nothing binds: `unread` names it."""
    from . import eventledger, seat_rehome, sessions
    wanted = [sid for sid in sids if sid]
    out = {"at": None, "home": None, "via": None, "unread": None, "row": None}
    if not wanted:
        return out
    rows, unread = eventledger.checked_events(launches_path(), strict=True)
    if unread:
        return dict(out, unread="the launch record %s did not read (%s)"
                    % (launches_path(), unread))
    rows = [row for row in rows if isinstance(row, dict)]
    mine = [row for row in rows
            if row.get("session") in wanted and _row_at(row) is not None]
    newest = max([_row_at(row) for row in mine], default=None)
    for sid in wanted:
        # A START THE RECORD COULD NOT TAKE, after its newest row: the
        # record may miss the session's last launch
        try:
            missed = os.path.getmtime(unrecorded_path(sid))
        except OSError:
            continue
        if newest is None or missed > newest:
            return dict(out, unread="a start of session %s was not recorded "
                        "(%s)" % (sid, unrecorded_path(sid)))
    row = max(mine, key=_row_at) if mine else None
    if row is not None:
        home = row.get("home")
        out.update(at=_row_at(row), row=row,
                   home=home if is_home_path(home) else None,
                   via="the launch record: %s %s" % (
                       row.get("via") or "a launch", row.get("ts")))
    more, unread = seat_rehome.events()
    if unread:
        return dict(out, unread="the seat rehome ledger %s did not read (%s)"
                    % (seat_rehome.ledger_path(), unread))
    # a rehome row is written after it PROVES the relaunched process, so
    # one naming the very process the newest row captured is that launch
    started = _process_of((row or {}).get("capture"))
    found = [(pk.parse_ts_epoch(entry.get("ts")),
              "the seat rehome ledger %s" % entry.get("ts")) for entry in more
             if isinstance(entry, dict) and entry.get("session") in wanted
             and not (started and _process_of(entry) == started)]
    for sid in wanted:
        try:
            found.append((os.path.getmtime(os.path.join(
                sessions.RESUME_DIR, "%s.sh" % sid)),
                "the adopted resume's script"))
        except (OSError, TypeError):
            continue
    found = [f for f in found if f[0] is not None
             and (out["at"] is None or f[0] > out["at"])]
    if found:
        at, via = max(found, key=lambda f: f[0])
        out.update(at=at, via=via, home=None)
    return out


def _process_of(rec):
    """(pid, start) a capture or a rehome row names (`pid`/`start`, or a
    rehome row's `process` pair), or None."""
    if not isinstance(rec, dict):
        return None
    pair = rec.get("process") if isinstance(rec.get("process"), list) \
        else [rec.get("pid"), rec.get("start")]
    if len(pair) != 2 or not isinstance(pair[0], int) or pair[1] is None:
        return None
    return pair[0], str(pair[1])


def _row_at(row):
    """A launch row's epoch: its `at`, else its `ts`."""
    at = row.get("at")
    if isinstance(at, (int, float)) and not isinstance(at, bool):
        return at
    return pk.parse_ts_epoch(row.get("ts"))


def _last_capture(launched, seat, session, pinned, last):
    """(the capture found, why it binds nothing or None) for `session`'s
    LAST launch (THE ONE RULE): when a launch-record row names the session,
    the newest row's capture, the process that started, unless the sweep
    captured a process AFTER that row — a later launch the record missed (a
    start whose hook wrote nothing: killed at its timeout, or a home without
    helm's hook), which then binds against the row's time, and which for
    the same process is its newer reading; else the sweep's capture, bound
    against the spawn register's stamp (`last`) and the older records.
    (None, None) when neither holds one."""
    row = launched["row"]
    if row is not None:
        swept, _path = captured(seat, session)
        taken = pk.parse_ts_epoch((swept or {}).get("captured"))
        # an older record's entry after the row is a later launch too
        after = launched["at"] if launched["at"] > _row_at(row) else None
        if taken is not None and taken > _row_at(row):
            cap, why = swept, capture_binds(swept, session, pinned,
                                            launched["at"])
        else:
            cap = _recut(row["capture"]) if isinstance(row.get("capture"),
                                                        dict) else None
            if cap is None:
                return None, ("its last launch (%s) recorded no capture of "
                              "its process" % launched["via"])
            why = capture_binds(cap, session, pinned, after)
        if why == LATER and after is not None:
            why = "%s (%s)" % (why, launched["via"])
        return cap, why
    cap, _path = captured(seat, session)
    if cap is None:
        return None, None
    later = launched["at"] is not None and (last is None
                                            or launched["at"] > last)
    why = capture_binds(cap, session, pinned, launched["at"] if later
                        else last)
    if why == LATER and later and launched["via"]:
        why = "%s (%s)" % (why, launched["via"])
    return cap, why


def capture_binds(rec, session, pinned=None, last=None):
    """None when capture `rec` binds a resume of `session` (THE ONE RULE
    above), else why it does not: `pinned` is the (pid, start) the seat's
    register pins to the session, `last` the epoch of its last recorded
    launch."""
    if not isinstance(rec, dict) or session not in (rec.get("sessions") or ()):
        return "no process it names held this session"
    if not (rec.get("pid") and rec.get("start")):
        return "it records no process birth"
    if pinned and (pinned[0], str(pinned[1])) != (rec["pid"],
                                                  str(rec["start"])):
        return "the seat's register pins another process to this session"
    taken = pk.parse_ts_epoch(rec.get("captured"))
    if taken is None:
        return "it records no capture time"
    if last is not None and last > taken:
        return LATER
    return None


# ------------------------------------------------------------- the recipe

class Recipe(object):
    """The recipe as read: `fields` (known values), `sources` (where each was
    read), `missing` (required fields no source knows), `withheld` (the
    fields a source set outside their allow-list, by name, with that
    source), `notes`."""

    def __init__(self, kind, seat, session):
        self.kind, self.seat, self.session = kind, seat, session
        self.fields, self.sources, self.notes = {}, {}, []
        self.info, self.withheld, self.info_sources = {}, {}, {}
        # the options the launch it is read from carried and a resume does
        # not pass: (names on claude's own list, count of the rest)
        self.dropped = ([], 0)
        # the account key the launch it is read from recorded, and where
        self.ran_account = (None, None)
        # the newest launch helm recorded of the session (`recorded_launch`)
        self.launched = {"at": None, "home": None, "via": None,
                         "unread": None, "row": None}
        # fields read from a source that is not the one launch the rest of
        # the recipe comes from, by name, with that source (`_one_launch`)
        self.unbound = {}
        # fields a source says MOVED to a value it cannot name: by name,
        # (that source, why); no lower source may fill one
        self.unknown = {}

    def put(self, name, value, source):
        if name not in self.fields and name not in self.withheld \
                and name not in self.unknown:
            self.fields[name], self.sources[name] = value, source

    def put_info(self, name, value, source):
        """A recorded-only fact, first source wins, as `put`."""
        if name not in self.info and name not in self.withheld:
            self.info[name], self.info_sources[name] = value, source

    def withhold(self, name, source, why=None):
        """`source` set `name` to a value outside its allow-list: the
        field is unknown, by name, and no later source may fill it (a lower
        source's value is not what this one ran). `why` says so in other
        words; the value is never in them."""
        if name not in self.fields and name not in self.info \
                and name not in self.withheld and name not in self.unknown:
            self.withheld[name] = source
            self.notes.append("%s is not kept: the %s %s, which is never "
                              "stored or shown" % (
                                  name, source, why or "sets it to a value "
                                  "outside its allow-list"))

    def unbind(self, name):
        """`name` was read from a source that is not this session's launch:
        it is unknown, and its source is kept to be named."""
        self.unbound[name] = self.sources.pop(name)
        del self.fields[name]

    @property
    def required(self):
        return PROXY_FIELDS if self.kind == "proxy" else CLAUDE_FIELDS

    @property
    def missing(self):
        return [f for f in self.required if f not in self.fields]

    def as_json(self):
        return {"seat": self.seat, "kind": self.kind, "session": self.session,
                "fields": self.fields, "sources": self.sources,
                "info": self.info, "missing": self.missing,
                "withheld": self.withheld, "unbound": self.unbound,
                "unknown": {n: why for n, (_src, why) in self.unknown.items()},
                "notes": self.notes,
                "not_restored": {"options": list(self.dropped[0]),
                                 "unlisted": self.dropped[1]},
                "account_key": self.ran_account[0]}


def read(kind, seat, session, transcript=None, cwd=None, launch=None,
         pid=None, role=None, credhome=None, cwd_source=TRANSCRIPT,
         register=None, source=None):
    """The recipe `seat` ran `session` with. `kind` is "proxy" or "claude";
    `launch` is a proxy seat's launch.sh path; `pid` its live process, when
    one runs; `role` a proxy seat's recorded role; `credhome` the home the
    session-creds index names for an adopted session; `cwd` the directory the
    resume lands in, and `cwd_source` where the resume path took it from.
    A cwd that no longer exists is unknown, named with its source.
    `register` (a proxy seat's spawn register) and the launch record
    (`recorded_launch`) are the launches a capture must not predate
    (`capture_binds`). `source` is the session the resume asked for when
    `session` is a cv-pruned copy of it: the copy binds that session's
    capture only when its own lineage stamp names exactly `source`."""
    r = Recipe(kind, seat, session)
    tx = transcript_facts(transcript) if transcript else {}
    layers = []
    if pid is not None:
        cap = capture_process(pid)
        if cap:
            layers.append(("%s pid %d" % (LIVE, int(pid)), cap))
        else:
            r.notes.append("the live process pid %d could not be read"
                           % int(pid))
    pinned, last = register_launch(register, session)
    r.launched = recorded_launch(session)
    cap, why = _last_capture(r.launched, seat, session, pinned, last)
    lineage = None
    if cap is None and why is None and source:
        stamp = source_session(transcript, session)
        if stamp == source:
            # a pruned copy no launch or capture names is its source's
            # launch: that one's LAST launch binds it
            lineage = source
            r.launched = recorded_launch(source, session)
            cap, why = _last_capture(r.launched, seat, source,
                                     register_launch(register, source)[0],
                                     last)
        elif stamp:
            r.notes.append("its lineage stamp names another session than "
                           "%s…, the source the resume asked for, so no "
                           "capture binds it" % source[:8])
    if cap and not why and r.launched["unread"]:
        # UNREAD IS NOT LATER: the record may hold a launch after the
        # capture, so nothing binds, and the note says which did not read
        why = "%s, so no launch after it can be ruled out" % (
            r.launched["unread"])
    if why:
        r.notes.append("the %s %s (pid %s) binds nothing: %s" % (
            CAPTURE, cap.get("captured"), cap.get("pid"), why) if cap
            else "no capture binds it: %s" % why)
        cap = None
    if cap:
        layers.append(("%s %s (pid %s)%s" % (
            CAPTURE, cap.get("captured"), cap.get("pid"),
            ", of its source session %s…" % lineage[:8] if lineage else ""),
            cap))
    if launch:
        was = read_launch(launch)
        if was:
            layers.append(("%s (launch.sh)" % LAUNCH, was))
    parsed, dropped = [], {}
    for src, layer in layers:
        a, e = argv_facts(layer.get("argv")), env_facts(layer.get("env"))
        _hold_named(a, e, layer.get("withheld"))   # what its capture withheld
        parsed.append((src, a, e))
        dropped[src] = (CAPTURE_KEYS["dropped"](layer.get("dropped")) or [],
                        CAPTURE_KEYS["unlisted_options"](
                            layer.get("unlisted_options")) or 0,
                        CAPTURE_KEYS["account"](layer.get("account")))
    if kind == "proxy":
        # A PROXY LAUNCH NAMES ITS MODEL, always: helm mints every one with
        # `claude --model`, so a script or process that names none is not a
        # launch helm can read a recipe from (the same line
        # `seat_remint.launch_fields` draws), and it states nothing here.
        stubs = [src for src, a, _e in parsed
                 if not a["model"] and "model" not in a["withheld"]]
        parsed = [(src, a, e) for src, a, e in parsed if src not in stubs]
        r.notes += ["%s names no `claude --model`, so it states no recipe"
                    % src for src in stubs]
    if cwd and os.path.isdir(cwd):
        r.put("cwd", cwd, cwd_source)
    elif cwd:
        r.notes.append("its working directory %s [%s] no longer exists"
                       % (cwd, cwd_source))
    if tx.get("model_unknown"):
        # the transcript says the model MOVED to one it cannot name: no
        # launch source, which states the model it STARTED on, may fill it
        r.unknown["model"] = (TRANSCRIPT, tx["model_unknown"])
        r.notes.append("the model is unknown: %s" % tx["model_unknown"])
    if "model" in tx:
        model, msrc = tx["model"], "%s (%s)" % (TRANSCRIPT, tx["model_source"])
        # A REPLY DROPS THE 1M SUFFIX, so a model read from the last reply
        # alone (a /clear's session, or an attachment past the widest tail)
        # is the 1M variant the launch it is read with names, when that
        # launch names exactly it: a /model would have left an attachment or
        # its own record, which outrank a reply
        if tx["model_source"] == "last reply":
            wide = [src for src, a, _e in parsed
                    if names_wide(a["model"], model)]
            if wide:
                model, msrc = model + ONE_M, "%s; its 1M suffix: %s" % (
                    msrc, wide[0])
        r.put("model", model, msrc)
    if "permission" in tx:
        r.put("permission", tx["permission"], TRANSCRIPT)
    for src, a, e in parsed:
        for name in a["withheld"] + e["withheld"]:
            # `_effort` weighs an argv's effort beside the transcript's
            if name != "effort" and name in r.required:
                r.withhold(name, src)
        if a["model"]:
            r.put("model", a["model"], src)
        if a["permission"]:
            r.put("permission", a["permission"], src)
        r.put("disallowed_tools", a["disallowed_tools"], src)
        r.put("append_system_prompt", a["append_system_prompt"], src)
        r.put("config_dir", e["config_dir"], src)
        r.put("identity_env", e["identity_env"], src)
        if kind == "proxy":
            for name in ("family", "window", "subagent"):
                r.put(name, e[name], src)
    _effort(r, tx, parsed, role)
    if parsed:
        # the launch the recipe is read from: the first source that states one
        names, unlisted, key = dropped[parsed[0][0]]
        r.dropped = (names, unlisted)
        r.ran_account = (key, parsed[0][0])
    _one_launch(r, [src for src, _a, _e in parsed])
    if kind == "claude" and "config_dir" not in r.fields \
            and r.launched["home"]:
        # the home the session's LAST recorded launch ran it on: newer than
        # any capture that binds (else that capture would state it), and
        # than the session-creds index, which keeps the first home it saw
        r.put("config_dir", r.launched["home"], r.launched["via"])
    if kind == "claude" and credhome and "config_dir" not in r.fields:
        r.put("config_dir", os.path.realpath(credhome), "session-creds index")
    elif kind == "claude" and credhome and \
            os.path.realpath(credhome) != r.fields["config_dir"]:
        r.notes.append("the session-creds index names %s for this session, "
                       "the %s %s" % (
                           credhome if is_home_path(credhome) else
                           "a home helm's registry does not know",
                           r.sources["config_dir"], r.fields["config_dir"]))
    r.put_info("harness", "claude", "helm")
    if tx.get("harness_version"):
        r.put_info("harness_version", tx["harness_version"], TRANSCRIPT)
    for src, layer in layers:
        version = CAPTURE_KEYS["version"](layer.get("version")) \
            or _binary_version(layer.get("binary"))
        if version:
            r.put_info("harness_version", version, src)
        elif "harness_version" in (layer.get("withheld") or ()) \
                or _versions_layout(layer.get("binary")):
            r.withhold("harness_version", src)
    if tx.get("account"):
        r.put_info("account", tx["account"], TRANSCRIPT)
    return _vet(r)


def _vet(r):
    """The ASSEMBLED recipe, checked once where read() returns it, whatever
    source each value came from (RECIPE_CHECKS): a value outside its field's
    list is removed and the field withheld by name, so a refusal names the
    field and nothing prints the value."""
    for store in (r.fields, r.info):
        for name in [n for n in store
                     if n in RECIPE_CHECKS and not RECIPE_CHECKS[n](store[n])]:
            del store[name]
            src = r.sources.pop(name, None) or r.info_sources.pop(name, "?")
            r.withhold(name, src, why=(
                "names a model the catalog does not know" if name == "model"
                else None))
    return r


def _one_launch(r, labels):
    """EXACTLY MEANS ONE LAUNCH. Keep only the fields that come from the one
    launch this session ran, and unbind the rest (`Recipe.unbind`): unknown,
    each with its source named, so the exact resume refuses them as it does
    an unknown field.

    The transcript is the session's own record, and so are the cwd, the
    session-creds index and the register's role. A live process or a
    capture is a process that held this session, so the transcript's
    fields and its fields are one launch's. launch.sh is bound to NO launch:
    `seat remint`, `seat launch` and `seat add` rewrite it with no launch
    after it, so beside a field the transcript states it is another
    launch's recipe (the transcript's model with a re-minted window), and
    alone it is the only launch there is. `labels` are the launch sources
    read() used, first first; a field from any but the first is another
    launch's too."""
    used = {}
    for name, src in r.sources.items():
        for label in labels:
            if label in src:
                used.setdefault(label, []).append(name)
    if not used:
        return
    top = next(label for label in labels if label in used)
    off = [n for label in labels if label != top for n in used.get(label, ())]
    session = [n for n, src in r.sources.items()
               if n != "cwd" and src.startswith(TRANSCRIPT)]
    alone = top.startswith(LAUNCH) and session
    if alone:
        off += used[top]
    off = [n for n in r.required if n in off]
    if not off:
        return
    for name in off:
        r.unbind(name)
    r.notes.append(
        "not one launch's recipe: %s %s; the rest is %s" % (
            ", ".join("%s [%s]" % (n, r.unbound[n]) for n in off),
            "are read from launch.sh, which a re-mint rewrites with no launch "
            "after it, so nothing binds it to the launch this session ran"
            if alone else "are read from another launch than %s" % top,
            ", ".join("%s [%s]" % (n, r.sources[n]) for n in r.required
                      if n in r.fields)))


def _effort(r, tx, parsed, role):
    """effort = level + ultracode. The level: the transcript's, else an
    argv's --effort; a proxy launch passing none is itself known (the seat's
    own settings decide, and helm writes those). Ultracode: the transcript's
    enter/exit, else an argv's --settings, else a proxy seat's launch is off
    whatever its role (no role carries ultracode on the pane command), else
    a transcript with replies and no enter is off. An argv whose --effort is
    free text withholds the level unless the transcript states one."""
    level, lsrc = tx.get("effort_level"), TRANSCRIPT
    if level is None:
        for src, a, _e in parsed:
            if "effort" in a["withheld"]:
                r.withhold("effort", src)
                return
            if a["effort_level"]:
                lsrc, level = src, a["effort_level"]
                break
        else:
            if not (r.kind == "proxy" and parsed):
                return
            lsrc = parsed[0][0] + ", no --effort"
    ultra, usrc = tx.get("ultracode"), TRANSCRIPT
    if ultra is None:
        found = [(src, a["ultracode"]) for src, a, _e in parsed
                 if a["ultracode"] is not None]
        if found:
            usrc, ultra = found[0]
        elif r.kind == "proxy" and role:
            usrc, ultra = ("spawn register role %s, which carries no "
                           "ultracode" % role), False
        elif tx.get("replies"):
            ultra = False
        else:
            return
    r.put("effort", {"level": level, "ultracode": bool(ultra)},
          lsrc if lsrc == usrc else "%s; ultracode: %s" % (lsrc, usrc))


def ultracode_line(f):
    """The line an exact resume prints for a recipe that ran with ultracode,
    or None. No resume restores ultracode; any recorded effort level is
    handled separately."""
    if f["effort"].get("ultracode"):
        return "ultracode: not restored (recorded effort handled separately)"
    return None


def show(name, value):
    """One field's value as a person reads it."""
    if name == "effort" and isinstance(value, dict):
        return effort_text(value.get("level"), value.get("ultracode"))
    if name == "identity_env" and isinstance(value, dict):
        return " ".join("%s=%s" % (k, value[k] if value.get(k) is not None
                                   else "(unset)") for k in IDENTITY_KEYS
                        if k in value)
    if name == "disallowed_tools":
        return " ".join(value) if value else "(none)"
    if name == "config_dir" and value == default_home():
        return "%s (the default home)" % value
    if value is None:
        return "(none)"
    return str(value)


def changes(recorded, target, fields):
    """[(field, recorded text, target text)] for each field that differs,
    UNKNOWN standing in for a field the recipe does not know."""
    out = []
    for name in fields:
        want = target.get(name)
        if name not in recorded.fields:
            out.append((name, "UNKNOWN", show(name, want)))
        elif show(name, recorded.fields[name]) != show(name, want):
            out.append((name, show(name, recorded.fields[name]),
                        show(name, want)))
    return out


def change_lines(recorded, target, fields):
    return ["%-20s %s -> %s" % (name, was, now)
            for name, was, now in changes(recorded, target, fields)]


def defaults_diff(recorded, today, fields):
    """The line EVERY exact resume prints, proxy seat and adopted lead
    alike: what today's defaults would have changed, field by field, so the
    operator sees what the recipe kept. None when they change nothing."""
    moved = changes(recorded, today, fields)
    return ("(today's defaults would change: %s)" % "; ".join(
        "%s %s -> %s" % m for m in moved)) if moved else None


class Plan(object):
    """What a resume will do: `lines` to print, `refusal` (None to go on),
    and the path's own knobs (`overrides`/`extra` for a proxy seat,
    `home`/`env`/`extra` for an adopted one)."""

    def __init__(self, lines=(), refusal=None, **knobs):
        self.lines, self.refusal = list(lines), refusal
        self.overrides = knobs.get("overrides")
        self.extra = tuple(knobs.get("extra") or ())
        self.home = knobs.get("home")
        self.env = knobs.get("env")
        self.recipe = knobs.get("recipe")
        # an adopted proxy seat's own launch script and the model it passes
        self.launch = knobs.get("launch")


# THE FIELDS A DEFAULTS RESUME READS FROM THE SEAT'S OWN PAST RUN, by kind:
# the session's cwd, and an adopted lead's credential home from the
# session-creds index. Every other field it sets from today's catalog, home
# or seat name. An UNATTENDED caller (the reboot sweep, the context-wall
# recovery: no operator is there to choose --defaults) resumes a seat whose
# recipe lacks only fields outside this list on today's defaults, which is
# what a resume always did for them, rather than leave the seat down or make
# an automatic recovery manual; with one of these unknown it refuses, as the
# operator's resume does.
PAST_FIELDS = {"claude": ("config_dir", "cwd"), "proxy": ("cwd",)}


def _takes_defaults(r, unattended):
    """Does an unattended caller resume `r` on today's defaults? Only when
    every field it lacks is one a defaults resume never takes from the
    seat's past run (PAST_FIELDS). `unattended` names the caller, or is
    falsy for the operator's resume, which refuses instead."""
    return bool(unattended and r.missing
                and not set(r.missing) & set(PAST_FIELDS[r.kind]))


def _defaults_lines(header, r, today, fields):
    """A defaults resume's lines: `header`, each field that moves, and the
    options it does not restore."""
    moved = change_lines(r, today, fields)
    return ([header + (":" if moved else " (nothing changes)")]
            + ["  " + line for line in moved]
            + [line for line in (not_restored(r),) if line])


def _unattended_header(r, unattended):
    """The one loud line an unattended caller prints when it resumes a seat
    on today's defaults: the caller, and the fields the recipe lacks, by
    name."""
    return ("WARNING: %s resumes it on TODAY'S defaults, not the recipe it "
            "ran with: %s unknown, none of them a field a defaults resume "
            "takes from its past run (the operator's resume refuses it)"
            % (unattended if isinstance(unattended, str)
               else "an unattended resume", ", ".join(r.missing)))


def _leave_down(unattended, why):
    """The one loud line an unattended caller refuses with: it leaves the
    seat down, naming itself and why."""
    return "%s leaves it down: %s" % (
        unattended if isinstance(unattended, str) else "an unattended resume",
        why)


def _model_change(r, launches):
    """Why an unattended caller's defaults fallback would change the model
    the recipe KNOWS the seat ran, or None: `launches` is the model today's
    defaults launch, None when they name none (claude then picks, so that
    is a change as well). An unattended resume never changes the model a
    seat runs; the operator's --defaults prints the change and is the
    operator's choice. Names the field and its source, never a model."""
    ran = r.fields.get("model")
    if ran and ran != launches:
        # a defaults launch that names NO model runs whatever claude picks,
        # which cannot be shown to be the one it ran: that is a change too
        return ("the model it ran [%s] is not %s, and an unattended resume "
                "never changes the model a seat runs; its recipe does not "
                "state %s" % (r.sources["model"],
                              "the one today's defaults launch" if launches
                              else "one today's defaults name (they name "
                              "none)", ", ".join(r.missing)))
    return None


def _home_move(r, home):
    """Why an unattended caller's defaults fallback, which runs on `home`
    (the session-creds index's), would move the seat off the home its recipe
    knows it ran on, or None. An unattended resume never moves the home a
    seat is paid by, unless both homes are measured to hold one account;
    the operator's --defaults prints the move and is the operator's
    choice."""
    ran = r.fields.get("config_dir")
    if not ran or not home or os.path.realpath(ran) == os.path.realpath(home):
        return None
    if r.ran_account[0] and account_key(home)[0] == r.ran_account[0]:
        return None
    return ("it last ran on home %s [%s], not %s, the home a defaults resume "
            "takes; an unattended resume never moves the home a seat is paid "
            "by" % (ran, r.sources["config_dir"], home))


def _incomplete(r, _seat):
    """The refusal of a recipe with a field no source knows: it names each
    field, and each source it did read, never a value, and offers the
    `--defaults` flag, never a command to type."""
    return ("its launch recipe is incomplete — saved only: %s unknown%s. "
            "Nothing was stopped or re-minted; --defaults resumes it on "
            "today's defaults and prints each field that changes first"
            % (", ".join(r.missing), "".join("; " + n for n in r.notes)))


# ------------------------------------------------------------ proxy seats

def proxy_line_fields(family, seat_name, room=None, room_source=None,
                      multi=False, identity=None, model=None, recipe=None):
    """The recipe fields the launch line helm would mint states — today's
    (recipe None) or the one a recipe overrides — read back through the same
    parser that reads launch.sh, so the check is on the line itself."""
    from . import seat
    line = seat.launch_line(family, model, room, seat_name,
                            room_source=room_source, multi=multi,
                            identity=identity, recipe=recipe)
    cap = launch_capture(line) or {"argv": [], "env": {}}
    a, e = argv_facts(cap["argv"]), env_facts(cap["env"])
    return {"model": a["model"], "permission": a["permission"],
            "effort": {"level": a["effort_level"], "ultracode": False},
            "disallowed_tools": a["disallowed_tools"],
            "append_system_prompt": a["append_system_prompt"],
            "config_dir": e["config_dir"], "identity_env": e["identity_env"],
            "family": e["family"], "window": e["window"],
            "subagent": e["subagent"]}


def window_pin_conflict(cdir, family, window, reseeds=True):
    """Why the window an exact resume stamps would NOT reach the process, or
    None. The seat's settings.json `env` OUTRANKS the launch stamp
    (envtidy.seat_stamp), so a window knob (seat_catalog.WINDOW_VARS) pinned
    there at another value is the window the seat runs. With `reseeds`, the
    resume re-seeds a lite family's pins from the recipe's window
    (seat_catalog.pin_action: helm's own pin follows it, an absent one is
    written), so only a pin the re-seed KEEPS conflicts: an operator's lower
    pin, or any pin on a family whose profile seeds none. A settings file
    that does not read is a conflict too: nobody can say which window
    Claude Code takes. Names the knob, never a number."""
    from . import seat  # noqa: F401 — the facade, before an impl module
    from .seat_catalog import (SEED_RECORD_ENV, SEED_RECORD_KEY, WINDOW_VARS,
                               launch_profile, pin_action)
    if not window:
        return None
    path = os.path.join(cdir, "settings.json")
    try:
        doc = pk.read_json(path, {}, strict=True)
    except (OSError, ValueError):
        return ("its settings.json does not read, so which window Claude "
                "Code takes cannot be known")
    env = doc.get("env") if isinstance(doc, dict) else None
    if not isinstance(env, dict):
        return None
    record = (doc.get(SEED_RECORD_KEY) or {}) if isinstance(
        doc.get(SEED_RECORD_KEY), dict) else {}
    helms = record.get(SEED_RECORD_ENV) if isinstance(
        record.get(SEED_RECORD_ENV), dict) else {}
    seeded = reseeds and launch_profile(family).get("pin_window")
    for name in WINDOW_VARS:
        if name not in env or str(env[name]) == str(window):
            continue
        if seeded and pin_action(env[name], str(window),
                                 helms.get(name)) == "write":
            continue
        return ("its settings.json pins %s to another window than the one it "
                "ran with, and that pin outranks the launch line" % name)
    return None


def window_warning(fam, fields):
    """Why a recorded window is WIDER than the one the catalog teaches for
    the recorded model, or None. The seat keeps the window it ran with (that
    is the exact resume); the operator is told, because a window past the
    model's real input ceiling is the one compaction cannot recover from."""
    from . import seat  # noqa: F401 — the facade, before an impl module
    from .seat_catalog import launch_window
    window, model = fields.get("window"), fields.get("model")
    taught = launch_window(fam, model) if model else None
    if window and taught and window > taught:
        return ("its window %d is larger than the %d the catalog teaches for "
                "%s; it resumes with the window it ran with, and `--defaults` "
                "takes the catalog's" % (window, taught, model))
    return None


def proxy_plan(family, seat_name, d, sid, cwd, prior, role, room,
               room_source, multi, identity, prior_model, defaults=False,
               cwd_source=TRANSCRIPT, unattended=None, source=None):
    """The resume plan for a helm-spawned proxy seat: EXACT by default (the
    launch line re-minted to carry the recipe's model, window, subagent pin,
    denied tools and system prompt, `--effort` on the pane command), today's
    defaults only under `defaults`, which prints every field that moves.
    `cwd_source` names where the resume took `cwd` from (the transcript,
    `--cwd`, the spawn register's worktree, or the seat home it was moved
    to), so a refusal names it truly. `unattended` names an unattended
    caller (the reboot sweep, the context-wall recovery), which resumes on
    today's defaults a recipe whose unknown fields a defaults resume never
    took from the seat's past run (`_takes_defaults`). `source` is the
    session `sid` is a pruned copy of, when the resume asked for one."""
    from . import seat
    from .seat_catalog import family_catalogued_models
    launch = os.path.join(d, "launch.sh")
    transcript = seat._seat_session_path_by_id(d, sid) if sid else None
    # the process the register pins to the session, or, for a pruned copy
    # whose lineage stamp names exactly `source`, the one pinned to its
    # source (the walled process a context-wall recovery replaces)
    pid = session_pid(prior, sid)
    if pid is None and source and source_session(transcript, sid) == source:
        pid = session_pid(prior, source)
    r = read("proxy", seat_name, sid, transcript=transcript, cwd=cwd,
             launch=launch, pid=pid, role=role,
             cwd_source=cwd_source, register=prior, source=source)
    try:
        today = proxy_line_fields(family, seat_name, room, room_source,
                                  multi, identity, prior_model)
    except (KeyError, TypeError, ValueError) as exc:
        return Plan(refusal="the launch line cannot be rendered: %s" % exc)
    today["cwd"] = cwd
    if defaults:
        return Plan(_defaults_lines("--defaults: resuming on TODAY'S "
                                    "defaults, not the recipe it ran with",
                                    r, today, PROXY_FIELDS), recipe=r)
    if _takes_defaults(r, unattended):
        change = _model_change(r, today.get("model"))
        if change:
            return Plan(refusal=_leave_down(unattended, change), recipe=r)
        return Plan(_defaults_lines(_unattended_header(r, unattended), r,
                                    today, PROXY_FIELDS), recipe=r)
    if r.missing:
        return Plan(refusal=_incomplete(r, seat_name), recipe=r)
    f = r.fields
    fam = seat.FAMILIES[family]
    if f["model"] not in family_catalogued_models(fam):
        return Plan(refusal="the model it ran [%s] is one the %s family no "
                            "longer catalogues, so it cannot resume exactly; "
                            "--defaults resumes it on today's defaults" % (
                                r.sources["model"], family), recipe=r)
    overrides = {k: f[k] for k in ("model", "subagent", "window",
                                   "disallowed_tools", "append_system_prompt")}
    try:
        exact = proxy_line_fields(family, seat_name, room, room_source, multi,
                                  identity, prior_model, recipe=overrides)
    except (KeyError, TypeError, ValueError) as exc:
        return Plan(refusal="the exact launch line cannot be rendered: %s"
                            % exc, recipe=r)
    exact["cwd"] = cwd
    # The level rides --effort (below); ultracode rides nothing any more,
    # and an exact resume neither restores it nor refuses over it: it says
    # so (ultracode_line). So effort is never a field the line cannot carry.
    exact["effort"] = dict(f["effort"], ultracode=False)
    # THE IDENTITY FOLLOWS THE ROSTER. A proxy seat's name and signing
    # profile are derived from its roster identity at every launch (task/3049),
    # and a durable rename is the operator changing it on purpose: the resume
    # carries the renamed identity, and says so, rather than refusing it.
    renamed = changes(r, exact, ("identity_env",))
    stuck = changes(r, exact, [n for n in PROXY_FIELDS
                               if n not in ("identity_env", "effort")])
    conflict = window_pin_conflict(os.path.join(d, "claude"), family,
                                   f["window"])
    if conflict:
        return Plan(refusal="the window it ran with cannot reach the process: "
                            "%s; --defaults resumes it on today's defaults"
                            % conflict, recipe=r)
    if stuck:
        return Plan(refusal="the launch line cannot carry the %s it ran with; "
                            "--defaults resumes it on today's defaults" % (
                                ", ".join(name for name, _was, _can in stuck)),
                    recipe=r)
    lines = ["resuming EXACTLY as it ran: model %s, window %s, %s, effort %s "
             "(`seat recipe %s` shows every field and its source)"
             % (f["model"], f["window"] or "(none)", f["permission"],
                show("effort", f["effort"]), seat_name)]
    wide = window_warning(fam, f)
    if wide:
        lines.append("WARNING: " + wide)
    lines += ["identity follows the roster: %s -> %s" % (was, now)
              for _name, was, now in renamed]
    lines += [line for line in (not_restored(r), ultracode_line(f)) if line]
    diff = defaults_diff(r, today, PROXY_FIELDS)
    if diff:
        lines.append(diff)
    level = f["effort"]["level"]
    return Plan(lines, overrides=overrides,
                extra=("--effort", level) if level else (), recipe=r)


# ---------------------------------------------------------- adopted leads

def _home_setting(home, key):
    """A top-level key of a credential home's settings.json (model,
    effortLevel), or None."""
    doc = pk.read_json(os.path.join(home or default_home(), "settings.json"),
                       None)
    value = doc.get(key) if isinstance(doc, dict) else None
    return value if isinstance(value, str) and value else None


def adopted_defaults(seat, home, cwd):
    """What today's adopted-seat resume launches with, field by field: no
    --model, no permission flag, no effort flag, only HELM_CHAT_NAME set."""
    from .seat_rehome import _settings_mode
    where = home or default_home()
    model, level = _home_setting(where, "model"), _home_setting(where,
                                                                "effortLevel")
    if model and not echoable_model(model):
        model = "a model the catalog does not know"
    if level and not is_effort(level):
        level = "a value outside its allow-list"
    return {"model": "(home default: %s)" % (model or "none set"),
            "permission": "(home default: %s)" % (_settings_mode(where)
                                                  or "none set"),
            "effort": "(home default: %s)" % (level or "none set"),
            "config_dir": os.path.realpath(where), "cwd": cwd,
            "disallowed_tools": [], "append_system_prompt": None,
            "identity_env": {"HELM_CHAT_NAME": seat,
                             "HELM_CELL_PROFILE": "(inherited)",
                             "DREGG_PROFILE": "(inherited)"}}


def account_check(r, home=None):
    """(line, refusal): a resume's account check, on EVERY path (exact,
    --defaults, an unattended caller's fallback): no resume may change the
    account a seat is paid by. BOTH SIDES ARE ONE KIND OF ID, the measured
    key of an account email that cred.account_of reads from a home: the
    account the home of the process it ran in held when that process was
    captured (or read live), and the one `home`, the home it resumes on
    (the recipe's by default), holds now. The transcript's bridge-session id
    is another kind of id and decides nothing here. A side that cannot be
    read is `account: UNKNOWN (<why>)`, printed, never a refusal; two keys
    that differ refuse, whatever the path, except for a seat that RAN on the
    default home (the launch its account was read from states it) and
    resumes there: that home's payer is whichever account Orca holds, and
    Orca switching it is the fleet's designed wall handling, so there the
    switch is printed and the resume goes on."""
    was, src = r.ran_account
    home = home or r.fields.get("config_dir")
    if not was:
        return ("account: UNKNOWN (no capture of the process it ran in "
                "recorded the account its home held)"), None
    now, why = account_key(home)
    if not now:
        return "account: UNKNOWN (the home it resumes on, %s: %s)" % (
            home, why), None
    ran = r.fields.get("config_dir") if r.sources.get("config_dir") == src \
        else None
    if now != was and os.path.realpath(home) == default_home() \
            and ran and os.path.realpath(ran) == default_home():
        return ("account: %s now; it ran on %s (%s), and the default home is "
                "paid by whichever account Orca holds" % (now, was, src)), None
    if now != was:
        return None, ("the account its home held when it ran (%s) is not the "
                      "one %s holds now; resuming there would swap the "
                      "account it is paid by, so no resume takes it there, "
                      "--defaults included" % (src, home))
    return "account: %s, the one its home held when it ran (%s)" % (
        now, src), None


def adopted_words(f):
    """The claude flags that restore a recipe (before `--resume`)."""
    words = ["--model", f["model"]] + permission_words(f["permission"])
    if f["effort"].get("level"):
        words += ["--effort", f["effort"]["level"]]
    if f["disallowed_tools"]:
        words += ["--disallowedTools"] + list(f["disallowed_tools"])
    if f["append_system_prompt"]:
        words += ["--append-system-prompt", f["append_system_prompt"]]
    return words


def _lead_env():
    """The env a LEAD launch adds, off the very helpers `helm launch`
    reads (launch.build_env): the lead window pair at
    seat_catalog.launch_window's lead number, and the HELM_SEAT_ROLE marker
    from seat_role._launch_env."""
    from . import seat  # noqa: F401 — the facade, before an impl module
    from .seat_catalog import WINDOW_VARS, launch_window
    from .seat_role import _launch_env
    win = str(launch_window({}, role="lead"))
    return dict(_launch_env("lead", {}), **{v: win for v in WINDOW_VARS})


def _is_lead(seat):
    """True when `seat`'s recorded role is lead. recorded_role reads worker
    on any error of its own; an error reaching here reads worker too, so a
    broken declaration never promotes a seat."""
    try:
        from . import seat as _facade  # noqa: F401 — the facade, before an
        # impl module; named apart so it never rebinds the `seat` parameter
        from .seat_role import recorded_role
        return recorded_role(seat) == "lead"
    except Exception:                          # noqa: BLE001 — never promote
        return False


def lead_line(seat, lean=True):
    """The plan or readout line naming an adopted LEAD's posture, or None
    for any other seat. Read-only: it names the lean profile and writes
    nothing; `lean` False says the profile could not be written."""
    if not _is_lead(seat):
        return None
    return ("lead posture (its recorded role): HELM_SEAT_ROLE=lead, window "
            "%s, %s, as a lead launch carries them"
            % (_lead_env()["CLAUDE_CODE_AUTO_COMPACT_WINDOW"],
               "the lead-lean --settings profile" if lean else
               "NO lead-lean profile (it could not be written)"))


def _lead_posture(seat, env, extra):
    """(env, extra, line) for an adopted claude seat's relaunch: a LEAD
    (seat_role.recorded_role) gets what `helm launch --seat S --role lead`
    gives it — the window pair and marker in its env and the lead-lean
    --settings words before --resume (launch._lead_lean_args, which writes
    the profile as a launch does). Any other seat gets `env` and `extra`
    back untouched and no line, so its plan is byte-identical."""
    if not _is_lead(seat):
        return env, extra, None
    from .launch import _lead_lean_args
    lean = _lead_lean_args(seat, "lead")
    return (dict(env, **_lead_env()), tuple(extra) + tuple(lean),
            lead_line(seat, lean=bool(lean)))


def adopted_plan(seat, row, home, defaults=False, unattended=None):
    """The resume plan for an orca-adopted claude seat: EXACT by default —
    its model, permission mode, effort, denied tools, added instructions and
    identity environment on the credential home it ran on — and the old
    bare `claude --resume` only under `defaults`, which prints every field
    that moves, or for an `unattended` caller (it names it) for a recipe whose
    unknown fields a defaults resume never took from its past run."""
    cwd = row.get("cwd")
    if row.get("h") != "claude":
        if defaults:
            return Plan(["--defaults: resuming as before (the %s harness "
                         "records no launch recipe helm reads)"
                         % row.get("h")], home=home,
                        env={"HELM_CHAT_NAME": seat})
        return Plan(refusal="its session runs on the %s harness, whose launch "
                            "recipe helm does not read, so it cannot resume "
                            "exactly; --defaults resumes it as before"
                            % row.get("h"))
    r = read("claude", seat, row.get("i"), transcript=row.get("p"), cwd=cwd,
             credhome=home)
    today = adopted_defaults(seat, home, cwd)
    fallback = not defaults and _takes_defaults(r, unattended)
    if defaults or fallback:
        # the defaults resume pins the session-creds index's home
        account, swap = account_check(r, home)
        if swap:
            return Plan(refusal=_leave_down(unattended, swap) if fallback
                        else swap, recipe=r)
        change = fallback and (_model_change(r, _home_setting(home, "model"))
                               or _home_move(r, home))
        if change:
            return Plan(refusal=_leave_down(unattended, change), recipe=r)
        env, extra, lead = _lead_posture(seat, {"HELM_CHAT_NAME": seat}, ())
        return Plan(_defaults_lines(
            "--defaults: resuming on TODAY'S defaults, not the recipe it ran "
            "with" if defaults else _unattended_header(r, unattended), r,
            today, CLAUDE_FIELDS) + [account] + ([lead] if lead else []),
            home=home, env=env, extra=extra, recipe=r)
    if r.missing:
        return Plan(refusal=_incomplete(r, seat), recipe=r)
    account, swap = account_check(r)
    if swap:
        return Plan(refusal=swap, recipe=r)
    f = r.fields
    env = dict(f["identity_env"], HELM_CHAT_NAME=seat)
    if f["config_dir"] == default_home():
        env[CONFIG_KEY] = None           # the default home runs with it unset
    lines = ["resuming EXACTLY as it ran: model %s, %s, effort %s, home %s "
             "(`seat recipe %s` shows every field and its source)"
             % (f["model"], f["permission"], show("effort", f["effort"]),
                show("config_dir", f["config_dir"]), seat)]
    lines += [line for line in (not_restored(r), ultracode_line(f), account)
              if line]
    diff = defaults_diff(r, today, CLAUDE_FIELDS)
    if diff:
        lines.append(diff)
    env, extra, lead = _lead_posture(seat, env, adopted_words(f))
    if lead:
        lines.append(lead)
    return Plan(lines, home=f["config_dir"], env=env, extra=extra, recipe=r)


# ------------------------------------------------------ adopted proxy seats

# The fields a proxy seat's own launch.sh states, which a resume THROUGH it
# cannot change: only the model (--model) and the effort level (--effort)
# ride its command line.
_LAUNCH_FIXED = ("config_dir", "permission", "disallowed_tools",
                 "append_system_prompt", "identity_env", "family", "window",
                 "subagent")


def _launch_today(launch_sh, cwd):
    """The fields a resume through `launch_sh` as it stands launches with,
    read through the same allow-listed readers as any recipe source."""
    cap = read_launch(launch_sh) or {"argv": [], "env": {}}
    a, e = argv_facts(cap["argv"]), env_facts(cap["env"])
    return {"model": a["model"], "permission": a["permission"],
            "effort": {"level": a["effort_level"],
                       "ultracode": bool(a["ultracode"])},
            "disallowed_tools": a["disallowed_tools"],
            "append_system_prompt": a["append_system_prompt"],
            "config_dir": e["config_dir"], "identity_env": e["identity_env"],
            "family": e["family"], "window": e["window"],
            "subagent": e["subagent"], "cwd": cwd}


def adopted_proxy_plan(seat, row, launch, defaults=False, unattended=None):
    """The resume plan for an orca-adopted seat whose session lives in a
    proxy seat's tree (`orcaadopt.proxy_launch`): THE SAME PLANNER as every
    other resume, read the same way (the transcript, a capture that binds,
    the launch script), refusing and falling back by the same rules. The
    resume runs THROUGH the seat's own launch.sh, which states every field
    but the model and the effort, so EXACT means the recipe's other fields
    are the ones launch.sh states now, the model it ran rides --model and
    its effort --effort/--settings; `defaults` (and an unattended caller's
    fallback) is launch.sh with the model it records, as before."""
    launch_sh = launch["launch_sh"]
    sid = row.get("i")
    r = read("proxy", seat, sid, transcript=row.get("p"), cwd=row.get("cwd"),
             launch=launch_sh)
    today = _launch_today(launch_sh, row.get("cwd"))
    header = None
    if defaults:
        header = ("--defaults: resuming on TODAY'S defaults, not the recipe "
                  "it ran with")
    elif _takes_defaults(r, unattended):
        change = _model_change(r, today["model"])
        if change:
            return Plan(refusal=_leave_down(unattended, change), recipe=r)
        header = _unattended_header(r, unattended)
    if header:
        return Plan(_defaults_lines(header, r, today, PROXY_FIELDS),
                    launch=dict(launch), recipe=r)
    if r.missing:
        return Plan(refusal=_incomplete(r, seat), recipe=r)
    f = r.fields
    stuck = [name for name, _was, _now in changes(r, today, _LAUNCH_FIXED)]
    if stuck:
        return Plan(refusal="its launch script cannot carry the %s it ran "
                            "with; --defaults resumes it on today's defaults"
                            % ", ".join(stuck), recipe=r)
    conflict = window_pin_conflict(
        os.path.join(os.path.dirname(launch_sh), "claude"),
        f.get("family") or launch.get("family"), f["window"], reseeds=False)
    if conflict:
        return Plan(refusal="the window it ran with cannot reach the process: "
                            "%s; --defaults resumes it on today's defaults"
                            % conflict, recipe=r)
    level = f["effort"].get("level")
    extra = ["--effort", level] if level else []
    lines = ["resuming EXACTLY as it ran, through its own launch script: "
             "model %s, window %s, %s, effort %s"
             % (f["model"], f["window"] or "(none)", f["permission"],
                show("effort", f["effort"]))]
    lines += [line for line in (not_restored(r), ultracode_line(f)) if line]
    diff = defaults_diff(r, today, PROXY_FIELDS)
    if diff:
        lines.append(diff)
    return Plan(lines, launch=dict(launch, model=f["model"]), extra=extra,
                recipe=r)


# ------------------------------------------------------------- the verb

def verdict(r):
    """What `seat recipe` says a parked seat would do: "resumes exactly" or
    "saved only: <the fields no source knows>"."""
    return ("resumes exactly" if not r.missing
            else "saved only: %s unknown" % ", ".join(r.missing))


def _proxy_view(seat_name, family):
    from . import seat
    d = seat._instance_dir(family, seat_name)
    launch = os.path.join(d, "launch.sh")
    if not os.path.exists(launch):
        return None, "no launch.sh — nothing minted to resume"
    prior = seat._spawn_record(d) or {}
    prior = prior if prior.get("seat") == seat_name else {}
    sid, sess_cwd = seat._newest_seat_session(d, prefer_source=prior.get(
        "session"))
    role = prior.get("role") or "worker"
    room, room_source = seat._homing_from_launch(launch)
    multi = seat._multi_from_launch(launch)
    from .seat_launch_assets import _launch_identity
    identity, _err = _launch_identity(seat_name)
    pid = session_pid(prior, sid)
    # the newest session may be a pruned copy of the one the register holds
    # (the rescue rank in `_newest_seat_session`): that one is its source
    held = prior.get("session")
    transcript = seat._seat_session_path_by_id(d, sid) if sid else None
    if pid is None and held and held != sid \
            and source_session(transcript, sid) == held:
        pid = session_pid(prior, held)
    r = read("proxy", seat_name, sid, transcript=transcript,
             cwd=sess_cwd, launch=launch, pid=pid, role=role, register=prior,
             source=held if held and held != sid else None)
    try:
        today = proxy_line_fields(family, seat_name, room, room_source, multi,
                                  identity or seat_name,
                                  seat._persisted_model(d, seat_name))
    except (KeyError, TypeError, ValueError) as exc:
        today = {}
        r.notes.append("today's launch line cannot be rendered: %s" % exc)
    today["cwd"] = r.fields.get("cwd")
    wide = window_warning(seat.FAMILIES[family], r.fields)
    if wide:
        r.notes.append("WARNING: " + wide)
    return (r, today, pid, PROXY_FIELDS), None


def _adopted_view(seat_name):
    from . import orcaadopt, sessions
    row, why = orcaadopt.newest_session_row(seat_name)
    if row is None:
        return None, why
    # latch=False: a READ-ONLY verb never appends to the session-creds index
    home = sessions.credhome_for(row["i"], latch=False) \
        if row.get("h") == "claude" else None
    # the seat's process speaks for this session only when its own session
    # record says it holds it (THE ONE RULE, `capture_binds`)
    pid = live_pid(seat_name)
    if pid is not None and sessions.live_sids().get(row["i"]) != int(pid):
        pid = None
    r = read("claude", seat_name, row["i"], transcript=row.get("p"),
             cwd=row.get("cwd"), pid=pid, credhome=home)
    return (r, adopted_defaults(seat_name, home, row.get("cwd")), pid,
            CLAUDE_FIELDS), None


def view(seat_name):
    """((recipe, today's fields, live pid, fields), None) or (None, why)."""
    from . import seat
    family, err = seat._seat_family(seat_name)
    if err:
        from . import orcaadopt
        if orcaadopt.resolve(seat_name) is None:
            return None, seat._unknown_seat_reason(seat_name, err)
        return _adopted_view(seat_name)
    if family not in seat.FAMILIES:
        return None, ("%s is a %s seat — it has no launch recipe to replay "
                      "(`helm seat spawn %s --replace` relaunches it)"
                      % (seat_name, family, seat_name))
    return _proxy_view(seat_name, family)


def render(seat_name, got):
    r, today, pid, fields = got
    out = ["%s — %s seat, session %s…, %s" % (
        seat_name, "proxy" if r.kind == "proxy" else "orca-adopted claude",
        str(r.session or "?")[:8],
        "running (pid %d)" % int(pid) if pid is not None else "not running")]
    for name in fields:
        if name in r.fields:
            out.append("  %-20s %s   [%s]" % (name, show(name, r.fields[name]),
                                             r.sources[name]))
        elif name in r.withheld:
            out.append("  %-20s UNKNOWN   [%s: outside its allow-list, "
                       "never kept or shown]" % (name, r.withheld[name]))
        elif name in r.unbound:
            out.append("  %-20s UNKNOWN   [%s: not this session's launch]"
                       % (name, r.unbound[name]))
        elif name in r.unknown:
            out.append("  %-20s UNKNOWN   [%s: %s]" % (
                name, r.unknown[name][0], r.unknown[name][1]))
        else:
            out.append("  %-20s UNKNOWN" % name)
    for name in INFO_FIELDS:
        if name in r.info:
            value = r.info[name]
            out.append("  %-20s %s   [recorded, never restored]" % (
                name, value[:8] + "…" if name == "account" else value))
    for note in r.notes:
        out.append("  note: " + note)
    lead = lead_line(seat_name) if r.kind == "claude" else None
    if lead:
        out.append("  " + lead + "; a resume applies it")
    dropped = not_restored(r)
    if dropped:
        out.append("  " + dropped)
    out.append("  resume: " + verdict(r))
    moved = changes(r, today, fields)
    out.append("  `seat resume %s --defaults` (today's defaults) would change:"
               " %s" % (seat_name, "; ".join("%s %s -> %s" % m for m in moved)
                        if moved else "nothing"))
    return out


USAGE = "seat recipe <seat> [--json]"


def cmd_recipe(rest):
    """seat recipe <seat> [--json] — READ-ONLY: the launch recipe a seat runs
    (or ran) with, each field's source, and whether a resume restores it
    exactly ("resumes exactly") or not ("saved only: <unknown fields>"),
    beside what `--defaults` would change. Exit 1 when the recipe is
    incomplete (a resume would refuse), 2 for an unknown seat."""
    from .cli import guard_tail
    rc = guard_tail("helm seat recipe", [a for a in rest if a.startswith("-")],
                    flags=("--json",), usage=USAGE)
    if rc is not None:
        return rc
    names = [a for a in rest if not a.startswith("-")]
    if len(names) != 1:
        print("usage: helm " + USAGE, file=sys.stderr)
        return 2
    got, why = view(names[0])
    if got is None:
        print("helm seat recipe: " + why, file=sys.stderr)
        return 2
    r, today, pid, fields = got
    if "--json" in rest:
        doc = r.as_json()
        doc.update(verdict=verdict(r), running=pid is not None,
                   defaults_would_change=[
                       {"field": n, "from": a, "to": b}
                       for n, a, b in changes(r, today, fields)])
        lead = lead_line(names[0]) if r.kind == "claude" else None
        if lead:
            doc["lead_posture"] = lead
        print(json.dumps(doc, indent=1, sort_keys=True, default=str))
    else:
        print("\n".join(render(names[0], got)))
    return 1 if r.missing else 0
