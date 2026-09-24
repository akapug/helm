"""Provider config-home identity from one process environment.

The environment belongs to the process being attributed. Callers prove that
process separately; this module owns the v3 provider and source vocabulary.
"""
import os


SPECS = {
    "claude": ("CLAUDE_CONFIG_DIR", ".claude"),
    "codex": ("CODEX_HOME", ".codex"),
    "pi": ("PI_CODING_AGENT_DIR", os.path.join(".pi", "agent")),
}
DEFAULT_SOURCE = "agent-HOME-default"
SESSION_SOURCE = "agent-session-runtime"
CWD_SOURCE = "hook-event-cwd"
HARNESS_SOURCE = "agent-process-runtime"
CONTEXT_SOURCES = {
    "session": (SESSION_SOURCE,),
    "cwd": (CWD_SOURCE,),
}


def env_key(harness):
    spec = SPECS.get(str(harness or "").strip().lower())
    return spec[0] if spec else None


def config_sources(harness):
    key = env_key(harness)
    return (key, DEFAULT_SOURCE) if key else ()


def resolve(harness, env, canonical=True):
    """(home, source) from one readable target-process environment.

    A present provider key is explicit authority only when it is a nonempty
    absolute path. Its presence with an empty/malformed value is an explicit
    refusal, never permission to fall through to HOME. Only an ABSENT provider
    key plus the target process's own absolute HOME proves the default. The
    inspector's environment is never consulted.
    """
    spec = SPECS.get(str(harness or "").strip().lower())
    if not spec or not isinstance(env, dict):
        return None, None
    key, suffix = spec
    if key in env:
        raw, source = env[key], key
    else:
        raw, source = env.get("HOME"), DEFAULT_SOURCE
        if isinstance(raw, str) and raw:
            raw = os.path.join(raw, suffix)
    if not isinstance(raw, str) or not raw or "\0" in raw \
            or not os.path.isabs(raw):
        return None, None
    return (os.path.realpath(raw) if canonical else raw), source
