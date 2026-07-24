#!/usr/bin/env python3
"""configs — the config-management model (decomposed package; public API preserved).
helm-native (see ATTRIBUTION.md for lineage)
dissolve-into-helm law.

One place to SEE and safely EDIT every local claude/codex config — MCPs, hooks,
skills, rules, memory (CLAUDE.md/AGENTS.md), settings — across all homes and cwds,
with the cascade (root → /dev → project).

Two halves:
  * READ / RESOLVE — reuses physics.py (the ground-truthed cascade resolver) for
    "what a seat loads", and enumerates which config files exist across a cwd tree.
  * EDIT — a safety-first editor: every write is backup → validate → atomic rename,
    restricted to RECOGNIZED config files under allowlisted roots, never a
    plugin/managed file. A bad write must never brick an agent's launch.

Stdlib only. Never reads or writes credential/token contents (config files only).

This module was a single configs.py; it was split into cluster modules
(_common/_classify/_resolve/_io/_cli) with the public import surface preserved
byte-for-byte here. Callers keep using `from helm import configs; configs.X`.
"""
import sys as _sys

from . import _common, _classify, _resolve, _io, _cli

# top-level module imports the original configs.py exposed as public names
from ._common import (
    ctypes, errno, glob, hashlib, json, os, secrets, stat, time, tomllib, re,
    physics,
)
# module-level constants (mutable roots the callers/tests rebind)
from ._common import (
    HOME, BACKUP_DIR, CWD_ROOTS, HOME_ROOTS, MANAGED_DIRS,
    _MAX_CONFIG_BYTES, _RENAME_EXCHANGE,
)
# classify cluster
from ._classify import (
    classify_path, _ext_type, _is_recognized_config, _is_seat_home,
)
# resolve cluster
from ._resolve import (
    tree, homes_configs, resolve, _annotate_mcp_shadows,
    _find_config_dirs, _project_files_at,
)
# read / edit / backups cluster
from ._io import (
    read_file, write_file, entry_op, list_backups, restore,
    _validate, _renameat2,
)
# cli cluster
from ._cli import cmd_configs

# ── monkeypatch fanout ────────────────────────────────────────────────────────
# The predecessor configs.py was ONE module, so tests + callers that rebind a
# module-level name (configs.HOME_ROOTS = …, configs.BACKUP_DIR = …) or
# mock.patch.object(configs, "_renameat2", …) changed the single namespace every
# function read from. After the split those names are read inside sibling cluster
# modules, so a rebind on the package must fan out to each cluster module that
# also binds the name — restoring the monolith's patch semantics exactly.
_FANOUT_MODULES = (_common, _classify, _resolve, _io, _cli)


class _ConfigsModule(_sys.modules[__name__].__class__):
    def __setattr__(self, name, value):
        super().__setattr__(name, value)
        for _m in _FANOUT_MODULES:
            if name in _m.__dict__:
                setattr(_m, name, value)


_sys.modules[__name__].__class__ = _ConfigsModule
