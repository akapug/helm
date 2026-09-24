#!/usr/bin/env python3
"""helm cred — WHO a credential home actually holds, and the safe /login.

THE INCIDENT this exists for: a session hits its limit, the human runs
`/login` inside it, and Claude Code writes the NEW account into the config dir
that session is pinned to (a live session's CLAUDE_CONFIG_DIR is immutable —
we cannot redirect that write). The dir keeps its old NAME. From that moment
every helm verb that keys on the dir NAME attributes work, tokens and
transcripts to an account that is no longer there. The evicted account's
credentials are simply gone.

We do not fight the write. We make it NON-DESTRUCTIVE, TRUTHFUL, REVERSIBLE:

  * TRUTHFUL — identity comes from CONTENT, never from the directory name.
    `account_of()` reads <dir>/.claude.json's oauthAccount block (email, uuid,
    org — identity METADATA; the tokens live in .credentials.json and are
    never read here) and caches it by mtime. `helm cred list` shows
    DIR NAME | ACTUAL ACCOUNT | verdict, so drift is visible, not inferred.
  * NON-DESTRUCTIVE — `helm cred backup` snapshots a home's .credentials.json
    bytes plus its oauthAccount block into ~/.cred-backups/<folded-email>/<ts>/
    at 0600 (dirs 0700), skipping when an identical snapshot already exists.
    `helm cred switch-guard` is that backup run deliberately BEFORE a /login
    (`--install` now retires old per-turn credential hooks in discovered
    credential and minted seat homes; it does not install a schedule).
  * REVERSIBLE — `helm cred heal` puts the correctly-named account back into a
    drifted home from a safely selected snapshot. DRY-RUN BY DEFAULT, it backs the
    current occupant up first (so the undo is itself undoable), and it REFUSES
    while any live process holds that config dir — a live session is never
    evicted, never raced. `helm doctor --ensure` owns unattended backup-before-
    heal upkeep when explicitly invoked. No per-turn credential hooks are
    declared; existing settings require explicit retirement.
  * FRESH AGAINST ORCA — an AGREE home can still carry a token Orca rotated
    away weeks ago. `freshness()` measures the home against Orca's managed copy
    of the same account (orca.py), `helm cred list` shows it, and
    `launch_sync()` is the one Orca -> home sync every claude launch onto a
    credhome runs before exec (`helm cred sync-orca` is the same act by hand).

LAWS (violating these is how accounts get bricked):
  * SECRETS NEVER SURFACE. Credential bytes are read, copied and compared —
    never printed, logged, or put in an error string. Only a 12-hex sha256
    prefix (a content fingerprint, same idiom as homes._token_family) is ever
    written into metadata.
  * OWNER-ONLY FROM CREATION. Every snapshot file is opened 0o600 before a byte
    lands; every directory is 0o700. Never chmod-after-write.
  * FAIL CLOSED. An unreadable/absent .claude.json yields NO account claim —
    the row reports the reason and the mutation refuses. helm never guesses an
    identity from a directory name.
  * EVERY MUTATING PATH BACKS UP FIRST (heal, and keepalive's rotation).
"""
import fcntl
import glob
import hashlib
import json
import os
import signal
import stat
import sys
import tempfile
import time

from .. import homes

# Re-export the whole former single-module surface — every `from helm import
# cred; cred.X` caller (and every mock.patch.object(cred, ...) seam) keeps
# working unchanged. Private names are re-exported deliberately: tests and
# sibling modules reach them as package attributes.
from ._common import (
    ACCOUNT_JSON, AUTH_JSON, DEFAULT_BACKUP_ROOT, GUARD_SPECS, KEEP,
    LINEAGE_FILE, LINEAGE_KEEP, PROC_ROOT, RETIRED_GUARD_SPECS, TS_FMT,
    _BACKUP_GUARDS, _CACHE,
    _HEAL_GUARDS, _atomic_private, _display_path, _email_or_none, _fsync_dir,
    _json_bytes, _read_json, _read_regular, _secure_dir, _stage_private,
    _stat_key, _str_or_none, _unlink, _write_private, backup_root,
    cache_clear)
from .account import _read_account, account_of, oauth_block, verdict_for
from .snapshots import (
    _COMMIT_SIGNALS, _block_commit_signals, _capture_home, _census_pair,
    _claim_snapshot_dir, _drop, _family_of_blob, _foreign_family, _identical,
    _lineage_accounts, _lineage_homes, _lineage_load, _lineage_path,
    _lineage_record, _pre_image, _prune, _rollback_files,
    _snapshot_families_on_disk, _snapshot_expiry, _snapshot_family,
    _snapshot_files, _snapshots_in, _unblock_commit_signals, account_dir,
    backup, backup_all, folded_dir, restore, rows, snapshots,
    snapshots_for_home_name)
from .heal import (
    _CLAUDE_FAMILY_COMMS, _comm_claude_family, _family_elsewhere, _held_note,
    _login_cmd, _pair_misbound, _proc_start, _proc_uid,
    _protected_not_holder, _unprovable_note, heal, heal_plan, holders_of)
from .orca import (
    DISAGREE, FRESH, NO_COPY, OWN_CHAIN, STALE, SYNC_ENV, UNKNOWN, UNPROVEN,
    _claude_holders, _cure, _measure, _token_facts, freshness, is_credhome,
    launch_sync, orca_accounts_root, orca_copy, spawn_note, sync, sync_disabled)
from .cli import (
    _print_backup, _print_heal, _print_list, _print_switch_guard,
    _public_paths, _resolve_home, _retired_guard_command, _retire_guard_settings,
    _take_flag, cmd_cred, doctor_rows, retire_guard_home, retire_guards)

# The `snapshots` and `heal` submodule attributes are shadowed by the
# same-named functions above (matching the old module surface); drop the two
# remaining submodule names so the public surface stays byte-identical.
del account, cli, orca
