#!/usr/bin/env python3
"""helm store — the ONE typed personal-knowledge store resolver.

The unification of the four near-clone legacy resolvers (priors.py + lexicon.py +
heuristics_store.py + the episodic memory reader) behind one loader, one JIT
resolver, one lifecycle. The store spans several PHYSICAL ROOTS but reads as
one logical store; every entry records where it lives:

  adopted      ~/.claude/projects/<home-slug>/memory — the LIVE store the
               user's agents write today. helm adopts it IN PLACE (same files,
               one more resolver); HELM_ADOPTED_DIR overrides it for tests.
  helm-global  ~/.helm/_global/{premises,heuristics,lexicon,references,priors}
  project      ~/.helm/<name>/{premises,heuristics,lexicon,references}

Scope precedence on a same-type same-slug collision: project > helm-global >
adopted (narrowest wins — the shadowing law).

This is a PACKAGE: store.py was decomposed into one-way clusters
(_common <- load <- resolve <- write <- index <- cli) with ZERO public-surface
change — this __init__ re-exports every name the old module exposed, so every
`from helm import store; store.X` caller keeps working unchanged. Entry TYPES,
writer byte-shapes, and lifecycle laws are documented at each cluster module.

Import-safe, side-effect-free, stdlib-only.
"""
# The top-level imports the pre-split module exposed as public attributes.
import calendar
import json
import os
import re
import sys

from .. import home, pk

# --- re-exports: every top-level name the pre-split store.py defined ---------
from ._common import (
    _slug,
    CERTAIN, DORMANT_BELOW, ACT_AT, BELIEF_CLAMP,
    STATUS_LIVE, STATUS_RETIRED, STATUS_DELETE_ELIGIBLE, STATUS_CANDIDATE,
    STATUS_PROVISIONAL, INJECTABLE_STATUSES, PINNED_SLUGS,
    LEGACY_PREFIX, PRIOR_PREFIX, RETEST, GENERIC_KEYWORDS, _HEURISTIC_GENERIC,
    _MIN_HEURISTIC_TOKEN, TYPE_SUBDIR, _SCAN_SUBDIRS, _TYPE_ORDER, _JIT_TYPES,
    _coerce_conf, derive_class, derive_load_class, _is_pinned, _decode_lists,
    _json1, _scope_rank, _recency,
)
from .load import (
    adopted_dir, _ADOPTED_PROJECT_CACHE, _project_adopted_dirs, roots,
    _default_dir, _PRIOR_DEFAULTS, _LEX_DEFAULTS, _HEUR_DEFAULTS, _REF_DEFAULTS,
    _EPISODIC_DEFAULTS, _parse_prior, _parse_lexicon, _parse_heuristic,
    _parse_reference, _parse_episodic, _parse_entry, _entry_files, _load_root,
    load_all, entries, candidates, reviewable, counts, _find,
    load_certain_policy, policy_declared,
)
from .resolve import (
    _probes, _jit_candidates, _df_map, _probe_hits, resolve_prompt, pinned,
)
from .write import (
    write_prior, _lexicon_path, write_lexicon, write_heuristic, write_reference,
    _WRITERS, apply_evidence, mark_superseded, retire, _STMT_ALIAS,
    _CANDIDATE_TYPES, _pick_candidate, confirm, reject, _notify_graduation,
    xrev_clear, demote, pinned_stats, retag, _kw_list, _KEYWORD_TYPES,
    guard_entry_keywords, guard_add_keywords, record_mint_events,
)
from .index import (
    INDEX_BUDGET_LINES, _memory_index_path, _index_link_backing, index_cap,
    cmd_index, DUP_OVERLAP, _tokens, _near_dup, near_dup_warning, _fmt,
)
from .cli import _GUARD_TYPE, _STALE_ON_REMINT, _USAGE, cmd_store

# The cluster submodules bind as public package attributes on import; drop them
# so the package's public surface is byte-identical to the pre-split module
# (the re-exported names above are what callers use).
del load, resolve, write, index, cli
