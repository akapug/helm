#!/usr/bin/env python3
"""helm web — the web surface. CLI-first + web parity: every view here is a
projection of what the CLI already answers (registry / store / whoami /
configs / skills / homes / quota). The ONLY mutations that land from the
browser are the owner-requested skills verbs (toggle = reversible rename,
delete = move to trash — archive-not-delete, nothing is ever destroyed;
census-validated), the homes lifecycle verbs (prepare/verify/archive/
unarchive/migrate — directory moves only, archive-not-delete, live-agent
refusals; logins stay human-only), the session verbs (cwd re-home / prune —
metadata + new-copy only), the configs editor (backup→validate→atomic,
recognized files only) and the chat post (an append to the RAM room — the
owner's side of the groupchat). All of it localhost-only, and every mutation
demands the per-process bearer token (MUTATION_TOKEN) — 403 without.

Laws: localhost-only bind (127.0.0.1, default port 7433), Python stdlib only,
one self-contained assembled UI page served at /. The store and whoami modules
are built in parallel — their endpoints DEGRADE GRACEFULLY to
{"unavailable": true} when the module is missing or misbehaves.
"""
import calendar
import hashlib
import json
import math
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import registry, web_ui_loader


if __name__ == "__main__":
    sys.modules[__package__ + ".web"] = sys.modules[__name__]

_previous_impls = globals().get("_WEB_IMPL_MODULES")
globals().pop("_WEB_FANOUT_NAMES", None)
if _previous_impls is not None:
    import importlib as _importlib
    for _module in _previous_impls:
        _importlib.reload(_module)

from . import web_compat as _web_compat
if _previous_impls is not None:
    _web_compat = _importlib.reload(_web_compat)
globals().update(_web_compat.EXPORTS)
_WEB_IMPL_MODULES = _web_compat.IMPL_MODULES
if _previous_impls is not None:
    del _importlib, _module
del _previous_impls, _web_compat



API = {
    "/api/registry": _api_registry,
    "/api/ready": _api_ready,
    "/api/store": _api_store,
    "/api/store/review": _api_store_review,
    "/api/decisions": _api_decisions,
    "/api/tasks": _api_tasks,
    "/api/whoami": _api_whoami,
    "/api/sessions": _api_sessions,
    "/api/configs": _api_configs,
    "/api/skills": _api_skills,
    "/api/status": _api_quota_status,
    "/api/allocate": _api_allocate,
    "/api/homes": _api_homes,
    "/api/configs/homes": _api_configs_homes,
    "/api/configs/backups": _api_configs_backups,
    "/api/notes": _api_notes,
    "/api/storage-matrix": _api_storage_matrix,
}

QUERY_API = {  # GET endpoints that take query params; fn(qs) -> (obj, status)
    "/api/configs/cascade": _api_configs_cascade,
    "/api/configs/tree": _api_configs_tree,
    "/api/configs/resolve": _api_configs_resolve,
    "/api/configs/file": _api_configs_file,
    "/api/creds": _api_creds,
    "/api/history": _api_history,
    "/api/burn": _api_burn,
    "/api/physics": _api_physics,
    "/api/physics-diff": _api_physics_diff,
    "/api/catalog": _api_catalog,
    "/api/search": _api_search,
    "/api/task/notes": _api_task_notes,
    "/api/session": _api_session,
    "/api/cmd": _api_cmd,
    "/api/chat": _api_chat,
    "/api/chat/roster": _api_chat_roster,
    "/api/todos": _api_todos,
    "/api/lr": _api_lr,
    "/api/roster/git": _api_roster_git,
    "/api/ledger": _api_ledger,
    "/api/ledger/turn": _api_ledger_turn,
    "/api/ledger/native": _api_ledger_native,
    "/api/multiplayer/state": _api_mp_state,
}

POST_API = {  # fn(payload_dict) -> (obj, status); ALL demand the mutation token
    "/api/store/confirm": _api_store_confirm,
    "/api/store/reject": _api_store_reject,
    "/api/decisions/verdict": _api_decisions_verdict,
    "/api/decisions/deliver": _api_decisions_deliver,
    "/api/decisions/comment": _api_decisions_comment,
    "/api/tasks/comment": _api_tasks_comment,
    "/api/skills/toggle": _api_skills_toggle,
    "/api/skills/delete": _api_skills_delete,
    "/api/homes": _api_homes_post,
    "/api/cwd": _api_cwd_post,
    "/api/prune": _api_prune_post,
    "/api/configs/file": _api_configs_file_post,
    "/api/configs/entry": _api_configs_entry_post,
    "/api/configs/restore": _api_configs_restore_post,
    "/api/chat": _api_chat_post,
    "/api/chat/react": _api_chat_react,
    "/api/chat/read": _api_chat_read_post,
    "/api/chat/seat": _api_chat_seat,
    "/api/chat/dm": _api_chat_dm,
    "/api/multiplayer/publish": _api_mp_publish,
    "/api/multiplayer/presence": _api_mp_presence,
}


def _seed_impl_modules():
    namespace = {name: value for name, value in globals().items()
                 if not (name.startswith("__") and name.endswith("__"))}
    for module in _WEB_IMPL_MODULES:
        module.__dict__.update(namespace)
    return frozenset(namespace)


class _WebModule(sys.modules[__name__].__class__):
    def __setattr__(self, name, value):
        super().__setattr__(name, value)
        if name in self._WEB_FANOUT_NAMES:
            for module in self._WEB_IMPL_MODULES:
                setattr(module, name, value)

    def __delattr__(self, name):
        modules = tuple(self._WEB_IMPL_MODULES)
        super().__delattr__(name)
        for module in modules:
            if name in module.__dict__:
                delattr(module, name)


_WEB_FANOUT_NAMES = _seed_impl_modules()
sys.modules[__name__].__class__ = _WebModule


if __name__ == "__main__":
    sys.exit(cmd_web(sys.argv[1:]))
