"""Frozen injection telemetry wire contracts.

V2 is immutable: exact UTF-8 byte accounting plus explicit provider-home env
only. V3 preserves those bytes and adds sample-time config-home provenance from
a bracketed actual agent process. Changing either contract requires V4.
"""
import os

from . import runtime_config


V2 = 2
V3 = 3
EXACT_VERSIONS = (V3, V2)
V2_LANES = ("whisper", "pinned", "jit", "reflex")
SESSION_SOURCE = "hook-json"
CWD_SOURCE = "hook-json"
HARNESS_ENV_SOURCE = "HELM_AGENT_HARNESS"
PI_HARNESS_SOURCE = "PI_CODING_AGENT"
CLAUDE_HARNESS_SOURCE = "claude-runtime"
CONTEXT_SOURCES = {
    "session": (SESSION_SOURCE,),
    "cwd": (CWD_SOURCE,),
}
HARNESS_SOURCES = {
    "claude": (HARNESS_ENV_SOURCE, CLAUDE_HARNESS_SOURCE),
    "codex": (HARNESS_ENV_SOURCE,),
    "pi": (HARNESS_ENV_SOURCE, PI_HARNESS_SOURCE),
}
# Frozen v2 vocabulary. V3 provider/source ownership lives in runtime_config.
CONFIG_HOME_ENV = {
    "claude": "CLAUDE_CONFIG_DIR",
    "codex": "CODEX_HOME",
    "pi": "PI_CODING_AGENT_DIR",
}


def _valid_exact(row, version):
    if not isinstance(row, dict) or row.get("v") != version:
        return False
    sample = row.get("sample")
    if not isinstance(sample, dict) or sample.get("encoding") != "utf-8":
        return False
    rendered, lanes = sample.get("rendered_bytes"), sample.get("lane_bytes")
    return type(rendered) is int and rendered >= 0 and isinstance(lanes, dict) \
        and set(lanes) == set(V2_LANES) \
        and all(type(lanes[lane]) is int and lanes[lane] >= 0
                for lane in V2_LANES) \
        and rendered >= sum(lanes.values())


def valid_v2(row):
    """True only for the frozen complete and arithmetically possible v2 shape."""
    return _valid_exact(row, V2)


def valid_v3(row):
    """True for exact-byte v3; failed provenance remains an unassigned sample."""
    return _valid_exact(row, V3)


def _valid_v3_runtime(row):
    runtime = row.get("runtime") if isinstance(row, dict) else None
    return isinstance(runtime, dict) \
        and type(runtime.get("pid")) is int and runtime["pid"] > 0 \
        and isinstance(runtime.get("proc_start"), str) \
        and runtime["proc_start"].isdigit()


def valid_exact(row):
    return valid_v2(row) or valid_v3(row)


def context_value(row, key):
    """One provenance-validated exact-sample context field, or None."""
    if not valid_exact(row):
        return None
    context, sources = row.get("context"), row.get("context_sources")
    if not isinstance(context, dict) or not isinstance(sources, dict) \
            or row.get("v") == V3 and not _valid_v3_runtime(row):
        return None
    value, source = context.get(key), sources.get(key)
    if key == "harness":
        value = str(value or "").strip().lower()
        allowed = ((runtime_config.HARNESS_SOURCE,) if row.get("v") == V3 else
                   HARNESS_SOURCES.get(value, ()))
        if source not in allowed:
            return None
        return value
    allowed = (runtime_config.CONTEXT_SOURCES if row.get("v") == V3 else
               CONTEXT_SOURCES).get(key, ())
    if source not in allowed:
        return None
    if key == "session" and value != row.get("session"):
        return None
    if key == "cwd" and (not value or not os.path.isabs(str(value))):
        return None
    return value


def config_home(row):
    """One version-appropriate harness-specific runtime config home, or None."""
    if not valid_exact(row):
        return None
    context, sources = row.get("context"), row.get("context_sources")
    if not isinstance(context, dict) or not isinstance(sources, dict) \
            or row.get("v") == V3 and not _valid_v3_runtime(row):
        return None
    harness = context_value(row, "harness")
    allowed = ((CONFIG_HOME_ENV.get(harness),) if row.get("v") == V2 else
               runtime_config.config_sources(harness))
    if sources.get("config_home") not in allowed:
        return None
    home = context.get("config_home")
    return str(home) if home and os.path.isabs(str(home)) else None


def config_context(row):
    """Validated cohort identity for one exact sample, or None when unassigned."""
    harness = context_value(row, "harness")
    cwd = context_value(row, "cwd")
    home = config_home(row)
    if not harness or not cwd or not home:
        return None
    return {"harness": str(harness), "cwd": str(cwd), "config_home": home}
