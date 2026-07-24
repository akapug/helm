"""Physics inspector: what a seat launched with (credhome, cwd) WOULD load.
helm-native (see ATTRIBUTION.md for lineage)
dissolve-into-helm law.

Read-only. Stdlib-only. Secret-free by construction:
  - env vars / HTTP headers -> NAMES only, never values
  - MCP transports -> type + command basename or URL scheme://host, never full
    URLs (paths/queries can embed tokens), never args beyond count
  - permissions -> counts + defaultMode, not rule bodies unless harmless names

Ground truth for the resolution rules was verified against Claude Code 2.1.207 and
codex-cli 0.144.1 on 2026-07-12. Key facts encoded here:

CLAUDE
  settings precedence (low->high): user < project < local < --settings flag < managed
  state file: <home>/.claude.json, EXCEPT the default home ~/.claude whose state
    (when CLAUDE_CONFIG_DIR is unset) is the sibling ~/.claude.json
  MCP sources: managed-mcp.json | user scope (.claude.json mcpServers) |
    local scope (.claude.json projects[cwd].mcpServers) |
    project scope (.mcp.json in cwd AND every ancestor, nearest wins,
    gated by projects[cwd].enabledMcpjsonServers/disabledMcpjsonServers or
    settings enableAllProjectMcpServers) | enabled plugins' .mcp.json |
    launch flags (--mcp-config / --strict-mcp-config, not visible to this module)
  .claude/.mcp.json (inside the .claude dir) is NOT read by claude -> warning.

CODEX
  everything per-cred rides <CODEX_HOME>/config.toml; per-cwd state is only
  projects."<abs cwd>".trust_level. AGENTS.md: <CODEX_HOME>/AGENTS.md global +
  repo AGENTS.md chain (root->cwd, docs-believed). hooks: <CODEX_HOME>/hooks.json
  plus external hooks.json files registered (trusted/enabled) in hooks.state.
"""

from __future__ import annotations

import json
import os
import re

try:
    import tomllib  # py3.11+
except ImportError:  # pragma: no cover
    tomllib = None

__all__ = ["physics_report", "physics_diff"]

MANAGED_DIRS = (
    "/etc/claude-code",                              # Linux
    "/Library/Application Support/ClaudeCode",       # macOS
)

_SECRETISH = re.compile(
    r"(token|secret|key|password|passwd|credential|bearer|auth)", re.I
)


# ---------------------------------------------------------------- helpers

def _expand(p):
    return os.path.abspath(os.path.expanduser(p)) if p else p


def _read_json(path):
    """Best-effort JSON read; returns (data, error_string_or_None)."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f), None
    except FileNotFoundError:
        return None, None
    except Exception as e:  # malformed counts as a finding, not a crash
        return None, "%s: %s" % (path, e.__class__.__name__)


def _redact_url(url):
    """scheme://host only — paths and queries can embed credentials."""
    m = re.match(r"^([a-z][a-z0-9+.-]*://)(?:[^/@]*@)?([^/?#]*)", str(url), re.I)
    if not m:
        return "<url>"
    return m.group(1) + m.group(2)


def _mcp_shape(name, cfg, source):
    """Redacted description of one MCP server definition."""
    cfg = cfg if isinstance(cfg, dict) else {}
    if "url" in cfg:
        transport = cfg.get("type") or cfg.get("transport") or "http"
        detail = _redact_url(cfg["url"])
    else:
        transport = cfg.get("type") or cfg.get("transport") or "stdio"
        detail = os.path.basename(str(cfg.get("command", ""))) or None
    entry = {"name": name, "source": source, "transport": transport}
    if detail:
        entry["detail"] = detail
    nargs = len(cfg.get("args") or [])
    if nargs:
        entry["args"] = nargs
    env_keys = sorted((cfg.get("env") or {}).keys()) if isinstance(cfg.get("env"), dict) else []
    if env_keys:
        entry["envKeys"] = env_keys
    hdrs = cfg.get("headers") or cfg.get("http_headers")
    if isinstance(hdrs, dict) and hdrs:
        entry["headerNames"] = sorted(hdrs.keys())
        if any(_SECRETISH.search(h) for h in hdrs):
            entry["hasSecretHeaders"] = True
    return entry


def _ancestors(path):
    """path and every ancestor up to filesystem root, nearest first."""
    out = []
    cur = _expand(path)
    while True:
        out.append(cur)
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return out


def _list_names(dirpath, suffix=None):
    """Entry names in a directory (dirs, or files stripped of suffix)."""
    try:
        entries = sorted(os.listdir(dirpath))
    except OSError:
        return []
    out = []
    for e in entries:
        if e.startswith("."):
            continue
        full = os.path.join(dirpath, e)
        if suffix is not None:
            if e.endswith(suffix) and os.path.isfile(full):
                out.append(e[: -len(suffix)] if suffix else e)
        elif os.path.isdir(full):
            out.append(e)
    return out


def _dedupe(seq):
    seen = set()
    return [x for x in seq if not (x in seen or seen.add(x))]


def _hook_summary(hooks_cfg, source):
    """settings.json 'hooks' -> [{event, matchers, count, source}]."""
    out = []
    if not isinstance(hooks_cfg, dict):
        return out
    for event, matchers in sorted(hooks_cfg.items()):
        if not isinstance(matchers, list):
            continue
        n = sum(len(m.get("hooks", [])) for m in matchers if isinstance(m, dict))
        if n:
            out.append({"event": event, "matchers": len(matchers),
                        "count": n, "source": source})
    return out


def _hook_commands(hooks_cfg):
    for matchers in (hooks_cfg or {}).values():
        if not isinstance(matchers, list):
            continue
        for m in matchers:
            for h in (m.get("hooks", []) if isinstance(m, dict) else []):
                cmd = h.get("command") if isinstance(h, dict) else None
                if cmd:
                    yield cmd


# ---------------------------------------------------------------- claude

def _claude_state_file(home):
    """Resolve <home>'s .claude.json, handling the default-home trap:
    ~/.claude run WITHOUT CLAUDE_CONFIG_DIR keeps state at ~/.claude.json
    (a sibling), while an explicit CLAUDE_CONFIG_DIR=~/.claude uses
    ~/.claude/.claude.json. Prefer the richer file; report ambiguity."""
    inside = os.path.join(home, ".claude.json")
    candidates = [inside]
    if os.path.basename(home) == ".claude":
        candidates.append(os.path.join(os.path.dirname(home), ".claude.json"))
    best, best_path, warn = None, None, None
    found = []
    for c in candidates:
        data, err = _read_json(c)
        if err:
            warn = "unreadable state file: " + err
        if isinstance(data, dict):
            found.append((c, data))
    if found:
        # richer = has more projects recorded
        found.sort(key=lambda t: len(t[1].get("projects", {}) or {}), reverse=True)
        best_path, best = found[0]
        if len(found) > 1:
            warn = ("two state files exist (%s); using the richer one — set "
                    "CLAUDE_CONFIG_DIR explicitly at launch to pin identity"
                    % " vs ".join(p for p, _ in found))
    return best or {}, best_path, warn


def _claude_settings_layers(home, cwd):
    """(name, path, data) for each present settings layer, low->high
    precedence. Launch flags (--settings) are a runtime injection seam;
    they cannot be read from disk and are noted in the report instead."""
    layers = []
    for name, path in (
        ("user", os.path.join(home, "settings.json")),
        ("project", os.path.join(cwd, ".claude", "settings.json") if cwd else None),
        ("local", os.path.join(cwd, ".claude", "settings.local.json") if cwd else None),
    ):
        if not path:
            continue
        data, err = _read_json(path)
        if isinstance(data, dict):
            layers.append((name, path, data))
        elif err:
            layers.append((name, path, {"__error__": err}))
    for mdir in MANAGED_DIRS:
        mpath = os.path.join(mdir, "managed-settings.json")
        data, _ = _read_json(mpath)
        if isinstance(data, dict):
            layers.append(("managed", mpath, data))
    return layers


def _merged_setting(layers, key):
    """Highest-precedence value for a scalar settings key."""
    val, src = None, None
    for name, _path, data in layers:  # layers are low->high
        if isinstance(data, dict) and key in data:
            val, src = data[key], name
    return val, src


def _claude_plugins(home, layers):
    """Effective plugin set: enabledPlugins merged across layers (later
    layer wins per key), joined with the home's install records."""
    merged = {}
    for _name, _path, data in layers:
        ep = data.get("enabledPlugins") if isinstance(data, dict) else None
        if isinstance(ep, dict):
            merged.update(ep)
    installed, _ = _read_json(os.path.join(home, "plugins", "installed_plugins.json"))
    install_map = {}
    if isinstance(installed, dict):
        for pname, recs in (installed.get("plugins") or {}).items():
            if isinstance(recs, list) and recs:
                install_map[pname] = recs[0].get("installPath")
    enabled = sorted(k for k, v in merged.items() if v)
    warnings = []
    plugin_dirs = []
    for p in enabled:
        path = install_map.get(p)
        if path and os.path.isdir(path):
            plugin_dirs.append((p, path))
        else:
            warnings.append("plugin enabled but not installed in this home: " + p)
    return {
        "enabled": enabled,
        "installed": len(install_map),
        "disabled": sum(1 for v in merged.values() if not v),
    }, plugin_dirs, warnings


def _claude_report(home, cwd):
    home = _expand(home)
    cwd = _expand(cwd) if cwd else None
    warnings = []
    if not os.path.isdir(home):
        warnings.append("home does not exist: " + home)

    state, state_path, warn = _claude_state_file(home)
    if warn:
        warnings.append(warn)
    layers = _claude_settings_layers(home, cwd)
    for name, path, data in layers:
        if "__error__" in data:
            warnings.append("malformed settings (%s layer): %s" % (name, path))
    layers = [(n, p, d) for n, p, d in layers if "__error__" not in d]

    proj = (state.get("projects") or {}).get(cwd, {}) if cwd else {}
    enable_all, _ = _merged_setting(layers, "enableAllProjectMcpServers")

    # ---- MCP: managed / user / local / project(.mcp.json walk-up) / plugins
    mcp = []
    for mdir in MANAGED_DIRS:
        mdata, _ = _read_json(os.path.join(mdir, "managed-mcp.json"))
        if isinstance(mdata, dict):
            for n, cfg in (mdata.get("mcpServers") or mdata).items():
                if isinstance(cfg, dict):
                    mcp.append(_mcp_shape(n, cfg, "managed"))
    for n, cfg in (state.get("mcpServers") or {}).items():
        mcp.append(_mcp_shape(n, cfg, "home-global"))          # user scope
    for n, cfg in (proj.get("mcpServers") or {}).items():
        mcp.append(_mcp_shape(n, cfg, "home-project-approval"))  # local scope

    enabled_names = set(proj.get("enabledMcpjsonServers") or [])
    disabled_names = set(proj.get("disabledMcpjsonServers") or [])
    seen_project = {}  # name -> nearest (winning) .mcp.json path; farther copies are shadowed
    if cwd:
        for d in _ancestors(cwd):  # nearest first; nearest wins on collision
            mdata, err = _read_json(os.path.join(d, ".mcp.json"))
            if err:
                warnings.append("malformed .mcp.json: " + err)
            if not isinstance(mdata, dict):
                continue
            for n, cfg in (mdata.get("mcpServers") or {}).items():
                if not isinstance(cfg, dict):
                    continue
                entry = _mcp_shape(n, cfg, "project-mcpjson")
                entry["file"] = os.path.join(d, ".mcp.json")
                if n in seen_project:
                    # a nearer .mcp.json already defined this name — this ancestor's
                    # copy is SHADOWED (kept, marked, so the UI can show the override).
                    entry["shadowed"] = True
                    entry["shadowed_by"] = seen_project[n]
                    mcp.append(entry)
                    continue
                seen_project[n] = os.path.join(d, ".mcp.json")
                if n in disabled_names:
                    entry["approval"] = "rejected"
                elif n in enabled_names or enable_all:
                    entry["approval"] = "approved" + (
                        " (enableAllProjectMcpServers)" if enable_all and n not in enabled_names else ""
                    )
                else:
                    entry["approval"] = "pending-approval-prompt"
                    warnings.append(
                        "project MCP '%s' not yet approved for this (home, cwd) — "
                        "interactive prompt at launch" % n)
                mcp.append(entry)
        stray = os.path.join(cwd, ".claude", ".mcp.json")
        if os.path.isfile(stray):
            warnings.append(
                ".claude/.mcp.json exists but claude only reads <dir>/.mcp.json — "
                "dead config unless passed via --mcp-config: " + stray)

    plugin_summary, plugin_dirs, pwarn = _claude_plugins(home, layers)
    warnings.extend(pwarn)
    for pname, pdir in plugin_dirs:
        mdata, _ = _read_json(os.path.join(pdir, ".mcp.json"))
        if isinstance(mdata, dict):
            for n, cfg in (mdata.get("mcpServers") or mdata).items():
                if isinstance(cfg, dict):
                    mcp.append(_mcp_shape(n, cfg, "plugin:" + pname))

    # ---- hooks: every settings layer + plugin hooks.json
    hooks = []
    for name, path, data in layers:
        hooks.extend(_hook_summary(data.get("hooks"), "%s (%s)" % (name, path)))
        for cmd in _hook_commands(data.get("hooks")):
            exe = cmd.split()[0] if cmd.strip() and not cmd.lstrip().startswith("if") else None
            if exe and os.path.isabs(exe) and not os.path.exists(exe):
                warnings.append("hook references missing script: " + exe)
            first_path = re.search(r"(/[^\s'\"]+)", cmd)
            if first_path:
                p = first_path.group(1)
                if p.startswith(os.path.expanduser("~/.claude/")) and \
                        not home.rstrip("/").endswith("/.claude"):
                    warnings.append(
                        "hook command lives in the MAIN home (~/.claude), not this "
                        "home — cross-home coupling: " + p)
                    break  # one warning is enough
    for pname, pdir in plugin_dirs:
        hdata, _ = _read_json(os.path.join(pdir, "hooks", "hooks.json"))
        if isinstance(hdata, dict):
            hooks.extend(_hook_summary(hdata.get("hooks", hdata), "plugin:" + pname))

    # ---- skills / commands / agents: home + project + plugins
    def _content(kind, suffix=None):
        names = {}
        home_names = _list_names(os.path.join(home, kind), suffix)
        if home_names:
            names["home"] = home_names
        if cwd:
            proj_names = _list_names(os.path.join(cwd, ".claude", kind), suffix)
            if proj_names:
                names["project"] = proj_names
        for pname, pdir in plugin_dirs:
            pl = _list_names(os.path.join(pdir, kind), suffix)
            if pl:
                names.setdefault("plugins", {})[pname] = len(pl)
        total = len(names.get("home", [])) + len(names.get("project", [])) + \
            sum(names.get("plugins", {}).values())
        return {"count": total, **names}

    # ---- settings highlights (NAMES only for env)
    model, model_src = _merged_setting(layers, "model")
    env_names = {}
    for name, _path, data in layers:
        if isinstance(data.get("env"), dict) and data["env"]:
            env_names[name] = sorted(data["env"].keys())
    perms = {}
    for name, _path, data in layers:
        p = data.get("permissions")
        if isinstance(p, dict):
            perms[name] = {k: (len(v) if isinstance(v, list) else v)
                           for k, v in p.items()}
    status_line, _ = _merged_setting(layers, "statusLine")
    effort, _ = _merged_setting(layers, "effortLevel")

    # ---- trust prediction
    trust = proj.get("hasTrustDialogAccepted")
    if cwd and not proj:
        warnings.append("cwd never opened under this home — first-launch trust "
                        "dialog (and .mcp.json approvals) will fire")
    elif cwd and trust is not True:
        warnings.append("trust dialog not accepted for this (home, cwd) — "
                        "interactive prompt likely on launch")

    # ---- memory chain
    memory = {
        "homeClaudeMd": os.path.isfile(os.path.join(home, "CLAUDE.md")),
    }
    if cwd:
        chain = [os.path.join(d, "CLAUDE.md") for d in _ancestors(cwd)
                 if os.path.isfile(os.path.join(d, "CLAUDE.md"))]
        memory["projectClaudeMdChain"] = chain
        memory["dotClaudeClaudeMd"] = os.path.isfile(
            os.path.join(cwd, ".claude", "CLAUDE.md"))
        memory["claudeLocalMd"] = os.path.isfile(
            os.path.join(cwd, "CLAUDE.local.md"))
        memory["rules"] = len(_list_names(
            os.path.join(cwd, ".claude", "rules"), ".md"))

    return {
        "harness": "claude",
        "home": home,
        "cwd": cwd,
        "stateFile": state_path,
        "settingsLayers": [{"layer": n, "path": p} for n, p, _ in layers],
        "mcpServers": mcp,
        "hooks": hooks,
        "plugins": plugin_summary,
        "skills": _content("skills"),
        "commands": _content("commands", ".md"),
        "agents": _content("agents", ".md"),
        "memory": memory,
        "trust": {"cwdKnown": bool(proj), "hasTrustDialogAccepted": trust},
        "settingsHighlights": {
            "model": model, "modelSource": model_src,
            "envKeys": env_names,
            "permissions": perms,
            "effortLevel": effort,
            "statusLine": bool(status_line),
            "enableAllProjectMcpServers": bool(enable_all),
        },
        "launchSeams": [
            "--settings (outranks user/project/local; only managed beats it)",
            "--mcp-config adds servers; --strict-mcp-config makes them exclusive",
            "--setting-sources can drop whole layers",
            "CLAUDE_CONFIG_DIR must be set explicitly to pin the state file",
        ],
        "warnings": _dedupe(warnings),
    }


# ---------------------------------------------------------------- codex

def _codex_report(home, cwd):
    home = _expand(home)
    cwd = _expand(cwd) if cwd else None
    warnings = []
    cfg_path = os.path.join(home, "config.toml")
    cfg = {}
    if tomllib is None:
        warnings.append("python < 3.11: tomllib unavailable, config.toml not parsed")
    else:
        try:
            with open(cfg_path, "rb") as f:
                cfg = tomllib.load(f)
        except FileNotFoundError:
            warnings.append("no config.toml in " + home)
        except Exception as e:
            warnings.append("malformed config.toml: %s" % e.__class__.__name__)

    mcp = [_mcp_shape(n, c, "home-config-toml")
           for n, c in (cfg.get("mcp_servers") or {}).items()]

    # trust: exact cwd, else nearest trusted ancestor (informational)
    projects = cfg.get("projects") or {}
    trust_level, trust_via = None, None
    if cwd:
        for d in _ancestors(cwd):
            ent = projects.get(d)
            if isinstance(ent, dict) and "trust_level" in ent:
                trust_level, trust_via = ent["trust_level"], d
                break
        if trust_level is None:
            warnings.append("cwd not in projects table — codex will ask for "
                            "trust/approval posture on first run here")
        elif trust_via != cwd:
            warnings.append("trust inherited from ancestor entry %s (exact cwd "
                            "has no projects entry)" % trust_via)

    # hooks: home hooks.json + hooks.state trust registry
    hooks = []
    hdata, herr = _read_json(os.path.join(home, "hooks.json"))
    if herr:
        warnings.append("malformed hooks.json: " + herr)
    if isinstance(hdata, dict):
        events = hdata.get("hooks", hdata)
        if isinstance(events, dict):
            for ev, defs in sorted(events.items()):
                if isinstance(defs, list) and defs:
                    hooks.append({"event": ev, "count": len(defs),
                                  "source": os.path.join(home, "hooks.json")})
    hstate = (cfg.get("hooks") or {}).get("state") or {}
    ext_files, enabled_n, disabled_n = set(), 0, 0
    for key, ent in hstate.items():
        fpath = key.rsplit(":", 3)[0] if ":" in key else key
        ext_files.add(fpath)
        if isinstance(ent, dict) and ent.get("enabled"):
            enabled_n += 1
        else:
            disabled_n += 1
        if fpath.startswith("/") and not os.path.exists(fpath):
            warnings.append("hooks.state references missing hooks file: " + fpath)
    if hstate:
        hooks.append({"event": "(hooks.state registry)",
                      "count": len(hstate),
                      "enabled": enabled_n, "disabled": disabled_n,
                      "files": sorted(ext_files),
                      "source": cfg_path})

    skills_cfg = (cfg.get("skills") or {}).get("config") or []
    skill_names = []
    missing_skills = 0
    for ent in skills_cfg:
        if not isinstance(ent, dict):
            continue
        path = str(ent.get("path", ""))
        if ent.get("enabled"):
            skill_names.append(os.path.basename(os.path.dirname(path)) or path)
        if path and not os.path.exists(path):
            missing_skills += 1
    if missing_skills:
        warnings.append("%d skills.config paths missing on disk" % missing_skills)

    plugins_tbl = cfg.get("plugins") or {}
    plugins = {
        "enabled": sorted(k for k, v in plugins_tbl.items()
                          if isinstance(v, dict) and v.get("enabled")),
        "disabled": sum(1 for v in plugins_tbl.values()
                        if isinstance(v, dict) and not v.get("enabled")),
    }

    agents_chain = {"homeAgentsMd": os.path.isfile(os.path.join(home, "AGENTS.md"))}
    if cwd:
        agents_chain["projectAgentsMdChain"] = [
            os.path.join(d, "AGENTS.md") for d in _ancestors(cwd)
            if os.path.isfile(os.path.join(d, "AGENTS.md"))]

    if not os.path.isfile(os.path.join(home, "auth.json")):
        warnings.append("no auth.json — this home has no login; codex will "
                        "demand auth at launch")

    profiles = sorted((cfg.get("profiles") or {}).keys())

    return {
        "harness": "codex",
        "home": home,
        "cwd": cwd,
        "configFile": cfg_path if cfg else None,
        "mcpServers": mcp,
        "hooks": hooks,
        "plugins": plugins,
        "skills": {"count": len(skill_names), "enabled": skill_names,
                   "configured": len(skills_cfg)},
        "commands": {},   # codex has no commands dir analogue
        "agents": agents_chain,
        "trust": {"cwdKnown": bool(cwd and trust_via == cwd),
                  "trust_level": trust_level, "via": trust_via},
        "settingsHighlights": {
            "model": cfg.get("model"),
            "modelReasoningEffort": cfg.get("model_reasoning_effort"),
            "approvalPolicy": cfg.get("approval_policy"),
            "sandboxMode": cfg.get("sandbox_mode"),
            "profiles": profiles,
            "features": {k: v for k, v in (cfg.get("features") or {}).items()},
            "authPresent": os.path.isfile(os.path.join(home, "auth.json")),
        },
        "launchSeams": [
            "CODEX_HOME pins the whole physics (config.toml is the only config)",
            "--profile selects a named profile block",
            "-c/--config key=value overrides any config.toml key at launch",
        ],
        "warnings": _dedupe(warnings),
    }


# ---------------------------------------------------------------- API

def physics_report(home_path, cwd, harness):
    """What a seat launched with (home_path, cwd) WOULD load.

    harness: "claude" | "codex". cwd may be None for a cwd-independent view.
    Returns a JSON-safe dict; never raises on missing/malformed files
    (they become entries in result["warnings"]). Never includes secret
    values: env/header names only, URL scheme+host only.
    """
    if harness == "claude":
        return _claude_report(home_path, cwd)
    if harness == "codex":
        return _codex_report(home_path, cwd)
    raise ValueError("unknown harness: %r (want 'claude' or 'codex')" % (harness,))


def _flatten(obj, prefix=""):
    """Flatten a report to comparable leaf paths -> shape-safe values."""
    out = {}
    if isinstance(obj, dict):
        for k, v in sorted(obj.items()):
            out.update(_flatten(v, "%s.%s" % (prefix, k) if prefix else str(k)))
    elif isinstance(obj, list):
        if not obj:
            pass  # [] == absent section: never a diff leaf (empty-vs-empty noise)
        elif all(isinstance(x, (str, int, float, bool, type(None))) for x in obj):
            out[prefix] = obj
        else:
            for i, v in enumerate(obj):
                key = v.get("name") or v.get("event") or str(i) if isinstance(v, dict) else str(i)
                out.update(_flatten(v, "%s[%s]" % (prefix, key)))
    else:
        out[prefix] = obj
    return out


_DIFF_SECTIONS = ("mcpServers", "hooks", "plugins", "skills", "commands",
                  "agents", "settingsHighlights", "memory", "trust")
_VOLATILE = re.compile(r"\.(cwd|home|stateFile|configFile|warnings|launchSeams"
                       r"|settingsLayers)\b|^(cwd|home|stateFile|configFile"
                       r"|warnings|launchSeams|settingsLayers)")


def physics_diff(home_a, home_b, harness, cwd=None):
    """What differs between two homes' physics for the same harness.

    cwd defaults to None so the diff isolates the CRED axis (per-cwd layers
    identical by construction); pass a cwd to compare full seat physics.
    Values are already shape-redacted by physics_report. Returns
    {added, removed, changed} keyed by section.path."""
    ra = physics_report(home_a, cwd, harness)
    rb = physics_report(home_b, cwd, harness)
    fa = {k: v for k, v in _flatten(
        {s: ra.get(s) for s in _DIFF_SECTIONS}).items() if not _VOLATILE.match(k)}
    fb = {k: v for k, v in _flatten(
        {s: rb.get(s) for s in _DIFF_SECTIONS}).items() if not _VOLATILE.match(k)}
    added = {k: fb[k] for k in fb if k not in fa}
    removed = {k: fa[k] for k in fa if k not in fb}
    changed = {k: {"a": fa[k], "b": fb[k]}
               for k in fa if k in fb and fa[k] != fb[k]}
    return {
        "harness": harness,
        "a": ra["home"], "b": rb["home"], "cwd": cwd,
        "added_in_b": added,
        "removed_in_b": removed,
        "changed": changed,
        "identical": not (added or removed or changed),
    }


if __name__ == "__main__":  # manual smoke: python3 physics.py <home> <cwd> <harness>
    import sys
    if len(sys.argv) == 4:
        print(json.dumps(physics_report(sys.argv[1], sys.argv[2] or None,
                                        sys.argv[3]), indent=2))
    elif len(sys.argv) == 5 and sys.argv[1] == "diff":
        print(json.dumps(physics_diff(sys.argv[2], sys.argv[3], sys.argv[4]),
                         indent=2))
    else:
        print("usage: physics.py <home> <cwd> <claude|codex>\n"
              "       physics.py diff <home_a> <home_b> <claude|codex>")
