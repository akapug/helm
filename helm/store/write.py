"""helm store — writers + lifecycle.

The byte-shape-compatible type writers, the _WRITERS dispatch, and the
lifecycle verbs (evidence/supersede/retire/confirm/reject/xrev-clear/demote)
plus pinned_stats. Moved verbatim from the pre-split helm/store.py.
"""
import json
import os
import re
import sys

from .. import pk
from ._common import (
    _slug, _coerce_conf, CERTAIN, BELIEF_CLAMP, derive_class, derive_load_class,
    _is_pinned, _json1, _timestamp_scalar, PRIOR_PREFIX, STATUS_LIVE, STATUS_RETIRED,
    STATUS_DELETE_ELIGIBLE, STATUS_CANDIDATE, STATUS_PROVISIONAL,
    GENERIC_KEYWORDS, _HEURISTIC_GENERIC, _JIT_TYPES,
)
from .load import _default_dir, _find, load_all, reviewable
from .resolve import _df_map, _jit_candidates, _probe_hits, pinned, resolve_prompt


# ---------------------------------------------------------------------------
# writers — byte-shape-compatible on-disk (same key order)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# the write timestamp — ONE parser shared with every recency reader
# ---------------------------------------------------------------------------


def _valid_write_ts(ts):
    """Why `ts` may NOT be written, or None when it has one recency meaning.

    #145: `last_updated` decides which entry wins a cap-4 injection slot. The
    write boundary and the sort key therefore call the SAME parser: accepting a
    shape one consumer interprets differently is an unordered field, not
    validation."""
    return _timestamp_scalar(ts)[1]


def _refuse_bad_ts(ts):
    """The shared refusal line for every ts-taking writer."""
    err = _valid_write_ts(ts)
    return err and ("refusing the write: %s" % err)


# ---------------------------------------------------------------------------
# the gloss — ONE emit and ONE validator, shared by every writer
# ---------------------------------------------------------------------------

def _gloss_lines(e):
    """[] or ["  gloss: <text>"] — EMIT only. Validation happens in _commit.

    One helper for every writer, because four writers each carrying their own
    optional-key loop is how the field ended up accepted by three load schemas
    and emitted by one."""
    gl = re.sub(r"\s+", " ", str(e.get("gloss") or "")).strip()
    return ["  gloss: " + gl] if gl else []


def _commit(path, body):
    """Write an entry ONLY after proving the row it will LOAD AS renders inside
    the budget. THE ARTIFACT IS THE ORACLE — never a model of it.

    THREE ROUNDS OF REVIEW DIED ON THE MODEL. The first validator rendered the
    writer's raw input (no type, no derived class) and measured the generic
    branch; the second normalized a probe and a caller-supplied `type` still
    overrode write_prior's authority; each round codex found the next site that
    modelled the render slightly differently — the prior branch, the reference
    branch, premise/_capture's private stale list, the lexicon update path.

    A per-case handler cannot be completed by adding cases, and helm's own
    premise names the cure: WHOLE-OBJECT VALIDATION PLUS AN HONEST REFUSAL. So
    this parses the EXACT BODY about to be written, with the REAL loader
    (_parse_entry, filename-prefix dispatch — the same call the store uses), and
    renders the result with the REAL renderer. No probe, no normalization, no
    per-type branch. A field the writer DERIVES is covered automatically; so is
    a writer nobody has thought of; so is the next type someone adds.

    MEASURED IN UTF-8 BYTES, not code points: len() counts characters, so 100
    emoji is 114 chars and 414 bytes and passed a 400 check (codex round 3).
    The budget is a byte budget, so this is the unit everywhere.

    Refuses rather than truncating: a gloss the injector would cut mid-clause
    recreates the severed-sentence failure the gloss exists to prevent."""
    from .. import inject
    from .load import _parse_entry
    text = "\n".join(body)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # A UNIQUE TEMP PER TRANSACTION, in the same directory so _parse_entry's
    # filename-prefix dispatch still sees the right type.
    #
    # The first cut used a DETERMINISTIC `path + ".commit-check.tmp"` with no
    # lock, and codex reproduced the contamination: writer A pauses inside
    # _parse_entry; writer B replaces the shared temp with a valid small body;
    # A parses B's row, validates THAT, and then atomic-writes its OWN 900-byte
    # gloss. The final entry carries a gloss the oracle never saw. A validator
    # that can be handed someone else's artifact is not measuring the artifact —
    # which is the entire point of this function, defeated by a filename.
    #
    # mkstemp gives an O_EXCL unique name, so the file parsed is IMMUTABLY the
    # one written here.
    import tempfile
    fd, tmp = tempfile.mkstemp(prefix=os.path.basename(path) + ".",
                               suffix=".commit-check.tmp",
                               dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        row = _parse_entry(tmp, os.path.basename(path))
    finally:
        # CLEANUP FAILURE IS NOT SILENT. codex's second repro: a rejected
        # oversized write left a 1373-byte temp body on disk while the real
        # entry was absent — a stray file that _entry_files may later enumerate
        # as an entry. Report it; never let it pass as a clean transaction.
        try:
            os.remove(tmp)
        except OSError as exc:               # noqa: BLE001
            print("helm store: could not remove commit-check temp %s (%s) — "
                  "remove it by hand; it may be enumerated as an entry"
                  % (tmp, exc), file=sys.stderr)
    if row:
        for field in ("last_updated", "updated_ts", "stated_ts"):
            stamp = str(row.get(field) or "").strip()
            if not stamp:
                continue
            bad_ts = _refuse_bad_ts(stamp)
            if bad_ts:
                raise ValueError("%s (%s)" % (bad_ts, field))
    if row and str(row.get("gloss") or "").strip():
        n = len(inject._entry_line_full(row).encode("utf-8"))
        if n > inject.LINE_CAP:
            raise ValueError(
                "gloss too long: the rendered line is %d UTF-8 bytes, limit %d "
                "— shorten the gloss by %d. A gloss that the injector truncates "
                "recreates the severed-sentence failure it exists to prevent."
                % (n, inject.LINE_CAP, n - inject.LINE_CAP))
    pk.atomic_write(path, text)


def write_prior(e, root_dir=None, path=None):
    """Write a prior-*.md (the prior-*.md shape). class/load_class re-derived
    on write so a stale authored value can never desync from confidence."""
    if path is None:
        path = os.path.join(root_dir or _default_dir("prior"),
                            PRIOR_PREFIX + _slug(str(e["id"])) + ".md")
    raw_kind = str(e.get("policy_kind") or "").strip()
    raw_reason = str(e.get("policy_reason") or "").strip()
    raw_members = e.get("policy_members") or []
    policy_present = bool(raw_kind or raw_reason or raw_members)
    if policy_present:
        if not raw_kind or not raw_reason or not isinstance(raw_members, (list, tuple)) \
                or not raw_members:
            raise ValueError("policy metadata requires kind, member list, and reason")
        try:
            explicitly_certain = float(e.get("confidence")) == CERTAIN
        except (TypeError, ValueError):
            explicitly_certain = False
        source = str(e.get("source") or "").strip()
        source_lower = source.casefold()
        if not explicitly_certain or not source or "inferred" in source_lower \
                or source_lower.startswith("agent"):
            raise ValueError("policy metadata requires explicit human certainty")
        values = [raw_kind, raw_reason] + [str(m) for m in raw_members]
        if any(any(ord(c) < 32 or ord(c) == 127 for c in value)
               for value in values):
            raise ValueError("policy metadata cannot contain control characters")
        if any("||" in str(m) for m in raw_members):
            raise ValueError("one policy member cannot contain the || delimiter")
    conf = _coerce_conf(e.get("confidence"))
    if conf < CERTAIN:
        # a BELIEF clamps to 0.99 BEFORE the %.2f serialization: 0.995..0.999
        # would round to "confidence: 1.00" beside "class: prior" and the next
        # read would coerce it into a certain premise — an agent-suppliable
        # value crossing the human-only certainty rail.
        conf = min(conf, BELIEF_CLAMP[1])
    klass = derive_class(conf)
    pin = _is_pinned(e)
    lc = derive_load_class(e, conf, pin)
    st = re.sub(r"\s+", " ", (e.get("statement") or "").replace('"', "'"))
    status = e.get("status") or "live"
    flag = "" if status == "live" else " [" + status + "]"
    label = "premise" if klass == "certain" else "prior"
    body = [
        "---",
        "name: " + PRIOR_PREFIX + _slug(str(e["id"])),
        'description: "' + label + flag + ': ' + (str(e["id"]) + " - " + st)[:170] + '"',
        "metadata:",
        "  node_type: memory",
        "  type: prior",
        "  id: " + str(e["id"]),
        "  statement: " + st,
        "  confidence: " + ("%.2f" % conf),
        "  class: " + klass,
        "  load_class: " + lc,
        "  status: " + status,
        "  domain: " + (e.get("domain") or ""),
        "  keywords: " + (e.get("keywords") or ""),
        "  stated_ts: " + str(e.get("stated_ts") or ""),
        "  last_updated: " + str(e.get("last_updated") or e.get("stated_ts") or ""),
        "  source: " + (e.get("source") or "human"),
        "  evidence_log: " + _json1(e.get("evidence_log")),
        "  confidence_history: " + _json1(e.get("confidence_history")),
    ]
    # CASEFOLDED, because a policy kind is an IDENTIFIER and not prose. @kimi
    # reproduced the bypass through this very writer: `Approval-Tier` passes
    # every validation, lands in the store, and is then INVISIBLE to a reader
    # asking for `approval-tier` — so the land gate sees no policy, and no
    # policy PERMITS. The operator believes the tier is enforced while the gate
    # reads an empty store.
    policy_kind = re.sub(r"\s+", " ",
                         str(e.get("policy_kind") or "")).strip().casefold()
    policy_reason = re.sub(r"\s+", " ", str(e.get("policy_reason") or "")).strip()
    members = e.get("policy_members") or []
    if isinstance(members, str):
        members = members.split("||")
    members = [re.sub(r"\s+", " ", str(m)).strip() for m in members]
    members = [m for m in members if m]
    if policy_kind:
        body.append("  policy_kind: " + policy_kind)
    if members:
        body.append("  policy_members: " + " || ".join(members))
    if policy_reason:
        body.append("  policy_reason: " + policy_reason)
    # THE LINE THAT FIRES, when the full statement is too long for the pinned
    # budget. OPTIONAL and emitted only when present: an unconditional
    # "gloss: " would rewrite the byte shape of every one of the ~880 entries
    # that will never have one, which the round-trip test correctly refused.
    body += _gloss_lines(e)
    if pin:
        body.append("  pin: true")
    elif str(e.get("pin") or "").strip().lower() in ("false", "0", "no"):
        # an explicit un-pin (demote) must survive the rewrite — on read it
        # beats the PINNED_SLUGS tuple, which is what keeps the flip stuck
        body.append("  pin: false")
    # attestation annotations survive rewrites — the native chain is the truth,
    # but a store rewrite (evidence/retire) must never orphan the pointer keys
    for opt in ("attest_payload", "attest_ts", "attest_by", "attest_record",
                "attest_chain_index", "attest_supersedes_record",
                "attest_anchor", "attest_anchor_turn",
                "attest_turn", "attest_receipt", "attest_supersedes_turn"):
        if e.get(opt) not in (None, ""):
            body.append("  " + opt + ": " + str(e[opt]))
    for opt in ("supersedes", "replaced_by", "source_prior", "xrev_by", "xrev_ts"):
        if e.get(opt):
            body.append("  " + opt + ": " + str(e[opt]))
    if e.get("retired_ts"):
        body += ["  retired_ts: " + str(e["retired_ts"]),
                 "  retired_why: " + (e.get("retired_why") or "")]
    body += ["---", "",
             label.upper() + flag + ": " + (e.get("statement") or ""),
             "",
             "**Why a " + label + ":** human-stated standing "
             + ("truth" if klass == "certain" else "belief")
             + "; agent flags, never silently overrides; confidence updates on logged evidence.",
             ""]
    _commit(path, body)
    return path


def _lexicon_path(term, scope, root_dir=None):
    """The term_path law: global -> lex-<term>.md; narrower authored scope
    prefixes lex-<scope-slug>--<term>.md so a project define never clobbers
    global."""
    name = "lex-" + _slug(term) + ".md" if scope == "global" \
        else "lex-" + _slug(scope) + "--" + _slug(term) + ".md"
    return os.path.join(root_dir or _default_dir("lexicon"), name)


def write_lexicon(e, root_dir=None, path=None):
    """Write a lex-*.md (the lex-*.md shape)."""
    term = e.get("term") or str(e.get("id") or "")
    scope = e.get("term_scope") or "global"
    if path is None:
        path = _lexicon_path(term, scope, root_dir)
    d = (e.get("definition") or e.get("statement") or "").replace('"', "'")
    body = [
        "---",
        "name: lex-" + _slug(term),
        'description: "' + ("lexicon: " + term + " = " + d)[:200] + '"',
        "metadata:",
        "  node_type: memory",
        "  type: lexicon",
        "  term: " + term,
        "  scope: " + scope,
        "  kind: " + (e.get("kind") or "phrase"),
        "  source: " + (e.get("source") or "define"),
        "  updated_ts: " + str(e.get("updated_ts") or ""),
        "  hits: " + str(e.get("hits") or "0"),
        "  definition: " + re.sub(r"\s+", " ", e.get("definition") or e.get("statement") or "").strip(),
    ]
    kw = re.sub(r"\s+", " ", e.get("keywords") or "").strip()
    if kw:
        body.append("  keywords: " + kw)
    # canon synonym-map fields (Lane 1) — serialized only when present so an
    # alias-free lexicon keeps its historical byte-shape. Each has a
    # _LEX_DEFAULTS default, so the parser round-trips them (no dropped-field
    # loss); alias_triggers is what the resolver folds into the probe set.
    for fld in ("aliases", "alias_triggers", "canonical"):
        val = re.sub(r"\s+", " ", str(e.get(fld) or "")).strip()
        if val:
            body.append("  " + fld + ": " + val)
    dom = (e.get("domain") or "").strip()
    if dom:
        body.append("  domain: " + dom)
    # a candidate carries an explicit status line (excluded from inject until
    # confirmed); a live lexicon keeps its historical byte-shape (no status key)
    status = str(e.get("status") or STATUS_LIVE)
    if status != STATUS_LIVE:
        body.insert(6, "  status: " + status)
    body += _gloss_lines(e)
    for opt in ("xrev_by", "xrev_ts"):  # the provisional-graduation receipt
        if e.get(opt):
            body.append("  " + opt + ": " + str(e[opt]))
    if e.get("retired_ts"):  # a rejected candidate keeps its receipt in-file
        body += ["  retired_ts: " + str(e["retired_ts"]),
                 "  retired_why: " + (e.get("retired_why") or "")]
    ex = e.get("examples") or []
    if ex:
        body.append("  examples: " + " || ".join(ex))
    body += ["---", "", term + (" (" + e["kind"] + ")" if e.get("kind") else "")
             + ": " + (e.get("definition") or e.get("statement") or ""), ""]
    _commit(path, body)
    return path


def write_heuristic(e, root_dir=None, path=None):
    """Write a heuristic-*.md (the heuristic-*.md shape). confidence is
    always 1 (a move, not a belief) so it is NOT a field; load_class always jit."""
    if path is None:
        path = os.path.join(root_dir or _default_dir("heuristic"),
                            "heuristic-" + _slug(str(e["id"])) + ".md")
    move_raw = (e.get("move") or e.get("statement") or "").strip()
    move = re.sub(r"\s+", " ", move_raw.replace('"', "'"))
    trig = (e.get("trigger") or e.get("keywords") or "").strip()
    status = e.get("status") or "live"
    flag = "" if status == "live" else " [" + status + "]"
    body = [
        "---",
        "name: heuristic-" + _slug(str(e["id"])),
        'description: "heuristic' + flag + ' (cross-domain MOVE, confidence=1): '
        + (str(e["id"]) + " - " + move)[:170] + '"',
        "metadata:",
        "  node_type: memory",
        "  type: heuristic",
        "  id: " + str(e["id"]),
        "  move: " + move,
        "  trigger: " + trig,
        "  domain: " + (e.get("domain") or ""),
        "  load_class: jit",
        "  status: " + status,
        "  stated_ts: " + str(e.get("stated_ts") or ""),
        "  last_updated: " + str(e.get("last_updated") or e.get("stated_ts") or ""),
        "  source: " + (e.get("source") or "human"),
    ]
    body += _gloss_lines(e)
    for opt in ("supersedes", "replaced_by", "xrev_by", "xrev_ts"):
        if e.get(opt):
            body.append("  " + opt + ": " + str(e[opt]))
    if e.get("retired_ts"):
        body += ["  retired_ts: " + str(e["retired_ts"]),
                 "  retired_why: " + (e.get("retired_why") or "")]
    body += ["---", "",
             "HEURISTIC" + flag + " (a cross-domain MOVE you APPLY, not a belief you hold): "
             + move_raw,
             "",
             "**Trigger pattern** (when it fires): " + (trig or "(none authored)"),
             "",
             "**Why a heuristic, not a premise:** this is a STRATEGY you reach for across domains "
             "(confidence=1 by construction), distinct from a premise/prior (a belief that gates). "
             "A reflex is this move compiled to fire every turn. It surfaces JIT when its trigger "
             "pattern appears in a turn - never added to the always-on digest.",
             ""]
    _commit(path, body)
    return path


def write_reference(e, root_dir=None, path=None):
    """Write a ref-*.md — the NEW class for harvested external reference
    material, same house frontmatter shape as its siblings."""
    if path is None:
        path = os.path.join(root_dir or _default_dir("reference"),
                            "ref-" + _slug(str(e["id"])) + ".md")
    summary_raw = (e.get("statement") or e.get("summary") or "").strip()
    summary = re.sub(r"\s+", " ", summary_raw.replace('"', "'"))
    status = e.get("status") or "live"
    flag = "" if status == "live" else " [" + status + "]"
    lc = str(e.get("load_class") or "").strip().lower()
    if lc not in ("always", "jit", "dormant"):
        lc = "jit"
    body = [
        "---",
        "name: ref-" + _slug(str(e["id"])),
        'description: "reference' + flag + ': ' + (str(e["id"]) + " - " + summary)[:170] + '"',
        "metadata:",
        "  node_type: memory",
        "  type: reference",
        "  id: " + str(e["id"]),
        "  summary: " + summary,
        "  url: " + (e.get("url") or ""),
        "  keywords: " + (e.get("keywords") or ""),
        "  domain: " + (e.get("domain") or ""),
        "  load_class: " + lc,
        "  status: " + status,
        "  stated_ts: " + str(e.get("stated_ts") or ""),
        "  last_updated: " + str(e.get("last_updated") or e.get("stated_ts") or ""),
        "  source: " + (e.get("source") or "harvest"),
    ]
    body += _gloss_lines(e)
    for opt in ("supersedes", "replaced_by", "xrev_by", "xrev_ts"):
        if e.get(opt):
            body.append("  " + opt + ": " + str(e[opt]))
    if e.get("retired_ts"):
        body += ["  retired_ts: " + str(e["retired_ts"]),
                 "  retired_why: " + (e.get("retired_why") or "")]
    body += ["---", "", "REFERENCE" + flag + ": " + summary_raw, ""]
    if e.get("url"):
        body += ["Source: " + e["url"], ""]
    _commit(path, body)
    return path


_WRITERS = {"prior": write_prior, "heuristic": write_heuristic,
            "reference": write_reference, "lexicon": write_lexicon}


# ---------------------------------------------------------------------------
# lifecycle — evidence, supersede, retire (the store laws, root-aware)
# ---------------------------------------------------------------------------

def apply_evidence(pid, ts, delta, reason, by="agent", kind=None, project=None):
    """Move a prior's confidence by `delta`, appending the receipt to
    evidence_log + a snapshot to confidence_history. A belief clamps to
    [0.05, 0.99] (never auto-1.0). A certain-prior (human truth) is NOT
    auto-demoted by agent evidence — the contradiction is LOGGED (the drift
    report reads exactly that) and surfaced, confidence stays pinned. An
    un-reasoned move is refused. Writes back IN PLACE on whichever root holds
    the entry — including adopted (the adoption contract)."""
    e = _find(pid, project=project, types=("prior",))
    if not e:
        return None, "not found"
    bad_ts = _refuse_bad_ts(ts)
    if bad_ts:
        return None, bad_ts
    if not str(reason or "").strip():
        return None, "evidence move requires a reason (un-logged moves are forbidden)"
    try:
        d = float(delta)
    except (TypeError, ValueError):
        return None, "delta not numeric"
    old = e["confidence"]
    new = old + d
    surfaced_msg = None
    if e["class"] == "certain":
        new = min(CERTAIN, max(BELIEF_CLAMP[0], new)) if by == "human" else old
        if by != "human":
            surfaced_msg = ("certain-prior: agent evidence is LOGGED + surfaced as "
                            "drift (contradicted), confidence not auto-applied")
    else:
        new = max(BELIEF_CLAMP[0], min(BELIEF_CLAMP[1], new))
    e["evidence_log"] = list(e.get("evidence_log") or []) + [
        {"ts": ts, "type": (kind or ("support" if d >= 0 else "contradict")),
         "delta": round(d, 4), "reason": reason, "by": by}]
    e["confidence_history"] = list(e.get("confidence_history") or []) + [
        {"ts": ts, "value": round(new, 4), "reason": reason}]
    e["confidence"] = new
    e["last_updated"] = ts
    write_prior(e, path=e["path"])
    pk.event("store.evidence", str(e["id"]),
             "%+.2f -> %.2f by %s: %s" % (d, new, by, reason))
    return e, surfaced_msg


def mark_superseded(old_id, new_id, ts, reason="", project=None):
    """Tombstone OLD as superseded BY NEW: old.status=delete_eligible +
    old.replaced_by=new, backpointer new.supersedes=old. A superseded entry
    STOPS injecting (load skips non-live) but the FILE STAYS — this function
    NEVER deletes (the presence-gated sweep is the physical delete). Idempotent;
    refuses self-supersede and a missing replacement."""
    bad = _refuse_bad_ts(ts)
    if bad:
        return None, bad
    old_id = (old_id or "").strip()
    new_id = (new_id or "").strip()
    if not old_id or not new_id:
        return None, "supersede requires both <old-id> and <new-id>"
    if _slug(old_id) == _slug(new_id):
        return None, "an entry cannot supersede itself"
    writable = ("prior", "heuristic", "reference")
    old = _find(old_id, project=project, types=writable)
    if not old:
        return None, "old entry '" + old_id + "' not found"
    new = _find(new_id, project=project, types=writable)
    if not new:
        return None, "new entry '" + new_id + "' not found (the replacement must exist first)"
    old.update({"status": STATUS_DELETE_ELIGIBLE, "replaced_by": str(new["id"]),
                "last_updated": ts})
    if reason:
        old["retired_why"] = reason
        old["retired_ts"] = old.get("retired_ts") or ts
    _WRITERS[old["type"]](old, path=old["path"])
    if _slug(str(new.get("supersedes") or "")) != _slug(str(old["id"])):
        new["supersedes"] = str(old["id"])
        new["last_updated"] = ts
        _WRITERS[new["type"]](new, path=new["path"])
    pk.event("store.supersede", str(old["id"]),
             "-> " + str(new["id"]) + ((" — " + reason) if reason else ""))
    return old, None


_KEYWORD_TYPES = ("prior", "heuristic", "reference", "lexicon")


def _kw_list(raw):
    """A keyword CSV -> an ordered, de-duplicated list. Order is preserved
    because it is the author's, and duplicates are dropped case-insensitively:
    the resolver lowercases, so `Pane` and `pane` are ONE probe wearing two
    spellings, and keeping both would understate nothing and clutter the row."""
    out, seen = [], set()
    for part in str(raw or "").split(","):
        k = re.sub(r"\s+", " ", part).strip()
        if k and k.lower() not in seen:
            seen.add(k.lower())
            out.append(k)
    return out


# ---------------------------------------------------------------------------
# add-time findability guards — the WRITE-side half of the resolve law
# ---------------------------------------------------------------------------
#
# MEASURED BASELINE (SA audit 2026-08-03, controls clean): 19% of live _global
# entries findable by symptom phrasing; 26% of the 483-entry population
# structurally near-unfindable from the keywords field ALONE — 9% EMPTY, 12%
# comma-missing word-salad (one giant probe only a VERBATIM repeat of the
# whole phrase can match), 4% a lone single-word probe. resolve indexes ONLY
# id + keywords, word-boundary (resolve._probes/_probe_hits); every measured
# WIN was a 1-3 word probe and every near-miss a fused phrase. Findability is
# therefore decided at WRITE time, and this seam is where it is decided.
# Existing entries are NOT retro-linted (that is a separate migration row);
# only NEW adds pay the toll. The `keywords`/lifecycle rewrite verbs are also
# NOT gated here — retag has its own no-empty law, and a retire/supersede of
# a legacy salad entry must never be vetoed by the salad it is retiring.

_SALAD_MIN_WORDS = 4    # a comma-less field of >= this many words = the salad
_STEM_MIN_WORDS = 3     # keyword cells this long get 1-2-word stems auto-added
_PROBE_CAP = 24         # keyword cells after decomposition — bounds index weight
_DF_GENERIC = 3         # a SOLE probe carried by >= this many live entries discriminates nothing
_DUP_OVERLAP_PROBES = 2  # shared probes that make an existing entry a strong sibling
_DUP_TOP_CELLS = 2      # keyword cells an existing entry must top-rank to be a strong sibling
_DUP_MIN_WORDS = 5      # WORD MASS of that shared evidence — see _dup_word_mass

# The decomposition stopword set. Deliberately TINY and closed-class — this
# filters WITHIN an author's phrase when forming stems, where a broad list
# (GENERIC_KEYWORDS) would deform bigrams by deleting topical-but-common words.
_STEM_STOPWORDS = frozenset((
    "the", "a", "is", "of", "to", "for", "and", "or", "not", "my", "it"))

# The one teaching sentence every lint refusal ends with — the measured cure.
_KW_CURE = ("comma-separated SYMPTOM phrases, 1-3 words each work best — "
            "measured: verbatim-match index")


def _keyword_lint(eid, cells, df, generic):
    """Why this keywords field writes a near-unfindable entry, or None.

    The three measured degenerate shapes, refused in the order cheapest to
    detect. All three share one property: no symptom phrasing a colleague
    would actually type can reach the entry, so the write LOOKS successful
    and the knowledge is silently lost to retrieval."""
    if not cells:
        return ("'%s' has NO keywords — resolve indexes ONLY id + keywords "
                "(word-boundary), and a kebab-slug id matches ~never, so an "
                "empty field files the entry where no symptom phrasing can "
                "reach it (measured: 9%% of the live store). Give it %s"
                % (eid, _KW_CURE))
    if len(cells) != 1:
        return None
    words = cells[0].split()
    if len(words) >= _SALAD_MIN_WORDS:
        return ("'%s' keywords are ONE comma-less cell of %d words — that "
                "indexes as ONE giant probe only a VERBATIM repeat of the "
                "whole phrase can match (the missing-comma salad; measured: "
                "12%% of the store, every near-miss a fused phrase). Use %s"
                % (eid, len(words), _KW_CURE))
    if len(words) == 1:
        w = words[0].lower()
        if w in generic:
            return ("'%s' keywords are the single generic word '%s' — a "
                    "generic-only probe can never satisfy the specificity "
                    "guard, so the entry can never fire. Give it %s"
                    % (eid, w, _KW_CURE))
        if df.get(w, 0) >= _DF_GENERIC:
            return ("'%s' keywords are the single word '%s', already carried "
                    "by %d live entries — as the ONLY probe it discriminates "
                    "nothing (df-weighting splits it %d ways). Give it %s"
                    % (eid, w, df[w], df[w], _KW_CURE))
    return None


def stem_probes(cells, generic=GENERIC_KEYWORDS):
    """The 1-2-word content stems of every >= _STEM_MIN_WORDS-word cell —
    the ADDITIONS only, deduped against the originals, author order kept.

    A fused phrase like `seat freezes on plan prompt` is a legal probe that
    matches ~never (verbatim-index law), and every measured near-miss was
    exactly this shape. Its 1-2-word sub-probes are what real turns contain,
    so they are AUTO-ADDED and the original is KEPT (it still matches the
    verbatim repeat, and it documents the author's intent).

    Two per-shape filters, both principled rather than per-case:
      - 1-word stems skip the type's GENERIC set — a generic unigram can
        never be the specific hit the resolver requires and only dilutes the
        df of every entry that carries it.
      - 2-word stems skip pairs whose BOTH halves are generic ('might be'):
        the pair is absent from the per-WORD generic set, so it would count
        as a SPECIFIC probe and wallpaper — a specificity-guard bypass minted
        by the very guard meant to improve retrieval."""
    have = {c.lower() for c in cells}
    out = []

    def take(p):
        if p not in have:
            have.add(p)
            out.append(p)

    for cell in cells:
        if len(cell.split()) < _STEM_MIN_WORDS:
            continue
        words = [w for w in cell.lower().split() if w not in _STEM_STOPWORDS]
        for w in words:
            if w not in generic:
                take(w)
        for i in range(len(words) - 1):
            if words[i] in generic and words[i + 1] in generic:
                continue
            take(words[i] + " " + words[i + 1])
    return out


def _dup_word_mass(shared):
    """Total WORD COUNT of the shared evidence — the kinship measure both
    duplicate legs weigh, replacing a bare tally of how MANY probes matched.

    THE BUG THIS CLOSES (#271, measured on the live 1330-entry candidate set).
    Counting probes made two entries siblings for sharing two bare English
    words. The live refusal that surfaced it paired a premise about agent
    dependence with a reference doc on a generational garbage collector, on
    `remember` + `process`; re-running every live entry through its own guard,
    80% of the store REFUSES ITSELF today, and 88% of those refusals are
    carried entirely by single-word probes. A guard that vetoes four of five
    legitimate writes is not protecting retrieval, it is a wall — and it was
    measurably DEGRADING retrieval, because the author's cure is to delete the
    colliding keyword, which in the surfacing case meant dropping the single
    highest-value symptom word and losing two of four retest phrasings.

    WHY WORD MASS AND NOT THE THREE OBVIOUS ALTERNATIVES. All three were
    measured against the pairs that share >= 2 MULTI-WORD probes (authored
    phrase overlap = real kinship), at the threshold that keeps every one:
      - DF/IDF weighting (1/df of the shared probes): 18% of the noise
        survives. It is not merely weak, it is aimed wrong: df counts entries
        in THIS store, not English commonness, so `remember` at df=2 scores
        near the maximum while `worktree` at df=32 scores near the floor. It
        is the same inversion GENERIC_KEYWORDS documents for `should` —
        rare-yet-meaningless is exactly the population df misreads.
      - an overlap RATIO instead of the raw count: 100% of the noise survives.
        Noise pairs have a HIGHER median ratio (0.22) than genuine ones (0.21);
        a short entry sharing two stray words scores above a long pair sharing
        seven phrases. Converting the count to a ratio makes this worse.
      - simply RAISING the count floor to 3: 40% of the noise survives.
    Word mass leaves 6%. _DUP_MIN_WORDS is not tuned to that figure, it is
    the LARGEST floor that still keeps every genuine pair — the sweep runs
    100%/40%, 100%/17%, 100%/6%, 97%/3%, 74%/1% at 3,4,5,6,7 words kept
    genuine over kept noise, so 5 is the knee and 6 is where real pairs
    start being released. The reason it works is the index it defends: resolve
    matches comma-separated SYMPTOM PHRASES verbatim, so kinship is two entries
    answering the same SYMPTOM — and a symptom is a phrase, not a word. Sharing
    `process` is vocabulary; sharing `sha does not resolve` is the same symptom.
    Every genuine pair in the live store carries >= 5 shared words; a
    single-word-only collision must now show five separately shared words
    (subsystem crowding) rather than two (coincidence)."""
    return sum(len(str(s).split()) for s in shared)


def _dup_sibling(eid, cells, project, entries):
    """The strongest existing entry the new keywords ALREADY resolve to, as
    (entry, why) — or (None, None). REUSES resolve's own machinery
    (_probe_hits + resolve_prompt over the same candidate set): the duplicate
    question IS the retrieval question, and a second implementation would
    drift from the index it predicts.

    Two legs, matching the two ways a live pair was measured splitting
    retrieval weight: (a) >= _DUP_OVERLAP_PROBES of one entry's probes hit
    the new keywords text (with the house specificity law — generic-only
    overlap is noise, not kinship); (b) one entry top-ranks
    >= _DUP_TOP_CELLS of the new cells individually (claimed to catch the
    same concept winning through DIFFERENT single probes — see the subsumption
    note below, that claim does not hold).

    BOTH legs now also require _DUP_MIN_WORDS of shared WORD MASS
    (_dup_word_mass carries the measurement). The count floors are KEPT
    beneath it, which makes this change strictly NARROWING: every pair that
    passed the guard before still passes, so no author can be newly refused
    by it. Leg (b) had no specificity law of any kind — leg (a) at least
    rejected generic-only overlap — so a bare unigram cell winning a top slot
    scored a full point toward kinship. That is the leg the live
    premise-vs-garbage-collector refusal came out of.

    Leg (b) weighs the mass of the SHARED PROBES that won the cells, not of
    the cells themselves, and that distinction is load-bearing rather than
    cosmetic. The cells are the author's own text, so two overlapping
    phrasings of one idea — `has to remember` and `to remember`, both live in
    the surfacing entry — are two cells won by ONE shared word, and charging
    their five author-written words as evidence re-creates the same bug one
    layer up: it re-refused the very pair this change exists to release. The
    union of matched probes is the actual evidence.

    WHAT THAT REVEALS ABOUT LEG (b), stated plainly because the leg's own
    justification above is wrong. Leg (b) claimed to catch "the same concept
    winning through DIFFERENT single probes, invisible to leg (a)". It is not
    invisible: leg (a) matches against the cells JOINED, and a probe that
    matches any cell matches the join, so leg (b)'s matched set is always a
    SUBSET of leg (a)'s and two different probes always give leg (a) its two
    hits. The only case leg (b) ever held alone is ONE shared probe winning
    two cells — which is the false-positive generator itself, and is how a
    lone `remember` accused a premise of duplicating a garbage-collector doc.
    Measured after this change: leg (b) contributes ZERO refusals leg (a)
    does not already make, over a 70-entry live sample. It is kept, not
    deleted, because its one surviving non-subsumed case is real (a single
    >= _DUP_MIN_WORDS-word phrase top-ranking two separate cells) and because
    it asserts something stronger than overlap — that the sibling actually
    WINS resolve's top slot — which keeps guarding if leg (a) is ever
    loosened.

    The `why` NAMES the shared evidence on both legs. Leg (b) previously said
    only "top-ranked for 2 of the 12 keyword cells"; the author then had to go
    read the other entry to discover which two, and the two turned out to
    settle the question instantly."""
    me = _slug(str(eid))
    text = ", ".join(cells).lower()
    best = None
    for e in _jit_candidates(entries):
        if e.get("type") == "capability" or _slug(str(e["id"])) == me:
            continue
        hits, specific, matched = _probe_hits(e, text)
        mass = _dup_word_mass(matched)
        if specific and hits >= _DUP_OVERLAP_PROBES and mass >= _DUP_MIN_WORDS \
                and (best is None or (mass, hits) > (best[0], best[1])):
            best = (mass, hits, e, matched)
    if best:
        return best[2], "%d shared probes, %d words (%s)" % (
            best[1], best[0], ", ".join(best[3]))
    tops = {}
    for cell in cells:
        for top in resolve_prompt(cell, project=project, cap=1, entries=entries):
            if _slug(str(top["id"])) != me:
                rec = tops.setdefault(_slug(str(top["id"])), [[], set(), top])
                rec[0].append(cell)
                rec[1].update(_probe_hits(top, cell.lower())[2])
    ranked = [rec for rec in tops.values()
              if len(rec[0]) >= _DUP_TOP_CELLS
              and _dup_word_mass(rec[1]) >= _DUP_MIN_WORDS]
    if ranked:
        won, shared, e = max(
            ranked, key=lambda rec: (_dup_word_mass(rec[1]), len(rec[0])))
        return e, ("top-ranked for %d of the %d keyword cells (%s) on %s"
                   % (len(won), len(cells), ", ".join(won),
                      ", ".join(sorted(shared))))
    return None, None


def _mint_identity(e):
    """The exact corpus row identity used by entry-mint exclusions."""
    return (str(e.get("type") or ""), _slug(str(e.get("id") or "")),
            str(e.get("path") or ""))


class _MintGuardResult:
    """The pure guard's four values, including deferred receipt data."""

    __slots__ = ("keywords", "refusal", "notes", "events")

    def __init__(self, keywords, refusal, notes, events):
        self.keywords = keywords
        self.refusal = refusal
        self.notes = notes
        self.events = events

    def __iter__(self):
        return iter((self.keywords, self.refusal, self.notes, self.events))

    def __len__(self):
        return 4

    def __getitem__(self, index):
        return (self.keywords, self.refusal, self.notes, self.events)[index]


def guard_entry_keywords(etype, eid, kw, project=None, force=False,
                         corpus=None, exclusions=()):
    """The pure entry-mint findability gate.

    Returns ``(kw_out, refusal, notes, events)``; ``events`` are receipt DATA
    for the caller to record only after its writer succeeds. The guard itself
    never mutates the store or journal. That transaction boundary matters when
    a duplicate override passes the probe but the serializer later refuses.

    ``corpus`` optionally supplies the caller's already-read store view.
    ``exclusions`` removes exact entry mappings from that view (normally the
    candidate being activated). Omitting ``corpus`` preserves add's one-read
    behavior by loading the same live/provisional JIT slice resolve uses.

    refusal set  -> the mint must NOT proceed (message teaches the cure).
    kw_out       -> the keywords to store: the author's EXACT string, with
                    stem probes APPENDED when long cells earned them —
                    byte-identical to the input when nothing was added, so
                    the guard never rewrites what it merely inspected.
    notes        -> receipt lines for the caller to print after success.
    events       -> deferred ``pk.event`` argument tuples.

    Guard order is load-bearing: lint first (a salad must never reach
    decomposition — stemming it would LAUNDER the refusable shape into a
    passable one); then the duplicate probe on the AUTHOR's cells (stems are
    common words, and counting them would over-accuse distinct entries of
    kinship); stems last, only for a mint that has earned the write.

    LEXICON is exempt from the EMPTY case only (its term is a probe by
    construction, so an empty field is still findable) and from the
    duplicate probe entirely (redefinition is its one update lane, and the
    alias-fold — not supersession — is its consolidation law; same reasoning
    as its _GUARD_TYPE exemption). Provided lexicon keywords are still
    linted and stemmed like everyone else's."""
    cells = _kw_list(kw)
    generic = _HEURISTIC_GENERIC if etype == "heuristic" else GENERIC_KEYWORDS
    entries = list(corpus) if corpus is not None else load_all(
        project=project, include_dormant=False, types=_JIT_TYPES)
    omitted = {_mint_identity(e) for e in exclusions}
    if omitted:
        entries = [e for e in entries if _mint_identity(e) not in omitted]
    df = _df_map(_jit_candidates(entries))
    if not (etype == "lexicon" and not cells):
        bad = _keyword_lint(eid, cells, df, generic)
        if bad:
            return _MintGuardResult(kw, bad, [], [])
    notes, events = [], []
    if cells and etype != "lexicon":
        sib, why = _dup_sibling(eid, cells, project, entries)
        if sib is not None:
            force = force or os.environ.get("HELM_STORE_FORCE_NEW", "") \
                .strip().lower() in ("1", "true", "yes")
            if not force:
                pflag = (" --project " + project) if project else ""
                return _MintGuardResult(kw, (
                    "'%s' already resolves to LIVE '%s' [%s] — %s. A "
                    "duplicate pair SPLITS retrieval weight (each shared "
                    "probe's 1/df halves, BOTH entries rank lower — a live "
                    "pair was measured doing exactly this), so update beats "
                    "add:\n  helm store keywords %s --add <your-new-probes>%s"
                    "\n  (or `helm store supersede` if the statement itself "
                    "evolved). Deliberately distinct? re-run with --force-new "
                    "(the override is recorded)."
                    % (eid, sib["id"], sib["type"], why, sib["id"], pflag)), [], [])
            summary = "forced past the duplicate probe — strong sibling '%s': %s" \
                % (sib["id"], why)
            events.append(("store.dup_override", str(eid), summary))
            notes.append("  DUP OVERRIDE recorded: strong sibling '%s' — %s"
                         % (sib["id"], why))
    added = stem_probes(cells, generic)[:max(0, _PROBE_CAP - len(cells))]
    if added:
        kw = str(kw or "").strip() + ", " + ", ".join(added)
        notes.append("  +%d stem probes auto-added from >=%d-word phrases "
                     "(originals kept): %s"
                     % (len(added), _STEM_MIN_WORDS, ", ".join(added)))
    return _MintGuardResult(kw, None, notes, events)


def guard_add_keywords(etype, eid, kw, project=None, force=False,
                       corpus=None, exclusions=()):
    """Legacy add-guard adapter preserving the three-value unpack contract."""
    result = guard_entry_keywords(
        etype, eid, kw, project=project, force=force,
        corpus=corpus, exclusions=exclusions)
    return result.keywords, result.refusal, result.notes


def record_mint_events(events):
    """Commit pure guard event data after the caller's serializer succeeds."""
    for verb, target, summary in events:
        pk.event(verb, target, summary)


def retag(eid, ts, add=None, remove=None, replace=None, project=None,
          ctype=None):
    """Widen or narrow an entry's RETRIEVAL KEYWORDS in place. -> (e, err)

    THE GAP THIS CLOSES (#188, measured 2026-08-04 on two seats in one night).
    `/learn` mandates a resolve-widen-retest loop and ends at "a capture is not
    done at `stored:` — it is done at FIRES". The store had no verb for the
    widen step: `add` refuses a live id and `confirm --edit` rewrites only the
    statement. So both seats following the skill CORRECTLY hit a wall and took
    the two available exits, and both are wrong in opposite directions — one
    hand-edited the frontmatter (silent: no receipt, the mutation trail this
    store keeps is bypassed), the other minted `-r2` ids and superseded twice
    (loud but FALSE: it records a supersession that did not happen and splits
    the DF weight of shared keywords across two files, so BOTH rank lower than
    either alone). A rule whose last step has no verb trains people to route
    around their own accountability trail.

    WHY IT SETS `trigger` TOO, AND THIS IS NOT BELT-AND-BRACES. Heuristics
    serialize their probes as `trigger`, and `write_heuristic` reads
    `e.get("trigger") or e.get("keywords")` — TRIGGER FIRST. A verb that set
    only `keywords` would write the OLD trigger back and report success, and
    the author would resolve-test, still miss, and conclude the store cannot
    be widened. Silent-no-op is the exact failure mode this verb exists to end,
    so it must not ship as one. `load` already normalises both directions.

    AN EMPTY RESULT IS REFUSED. An entry with no probes can never be resolved
    by anything — it is retired without a retirement receipt, and it looks live
    on every listing. `retire` is the verb for that, and it says so.
    """
    if replace is not None and (add or remove):
        return None, "--set replaces the whole list; do not combine it with " \
                     "--add/--remove"
    if ctype:
        ctype = "prior" if ctype == "premise" else ctype
        if ctype not in _KEYWORD_TYPES:
            return None, "unknown --type '%s' (one of: %s)" \
                % (ctype, ", ".join(_KEYWORD_TYPES))
    e = _find(eid, project=project, types=(ctype,) if ctype else _KEYWORD_TYPES)
    if not e:
        return None, "'" + str(eid) + "' not found"
    before = _kw_list(e.get("keywords"))
    if replace is not None:
        after = _kw_list(replace)
    else:
        drop = {k.lower() for k in _kw_list(remove)}
        after = [k for k in before if k.lower() not in drop]
        have = {k.lower() for k in after}
        for k in _kw_list(add):
            if k.lower() not in have:
                have.add(k.lower())
                after.append(k)
    if not after:
        return None, "that would leave '%s' with NO keywords — nothing could " \
                     "ever resolve it, and an entry no probe reaches is " \
                     "retired without a receipt; use `store retire` to retire " \
                     "it on the record" % e["id"]
    if after == before:
        return e, None                          # idempotent: no write, no event
    joined = ",".join(after)
    # BOTH KEYS, ALWAYS — see the docstring. Non-heuristic writers never
    # serialize `trigger`, so this is inert for them and load-bearing for one.
    e.update({"keywords": joined, "trigger": joined, "last_updated": ts})
    _WRITERS[e["type"]](e, path=e["path"])
    pk.event("store.retag", str(e["id"]),
             "%d -> %d keywords" % (len(before), len(after)))
    return e, None


def retire(eid, ts, why="", project=None):
    """Human-retire: status=retired, file KEPT as the record (never deleted)."""
    bad = _refuse_bad_ts(ts)
    if bad:
        return None, bad
    e = _find(eid, project=project, types=("prior", "heuristic", "reference"))
    if not e:
        return None, "'" + str(eid) + "' not found"
    e.update({"status": STATUS_RETIRED, "retired_ts": ts, "retired_why": why,
              "last_updated": ts})
    _WRITERS[e["type"]](e, path=e["path"])
    pk.event("store.retire", str(e["id"]), why or "retired")
    return e, None


# per-type alias key the writers actually serialize beside "statement"
_STMT_ALIAS = {"lexicon": "definition", "heuristic": "move", "reference": "summary"}


def _entry_keywords(e):
    """The retrieval field an entry's writer owns (heuristic = trigger)."""
    return e.get("trigger") if e.get("type") == "heuristic" else e.get("keywords")


def _set_entry_keywords(e, keywords):
    """Apply guarded probes in the representation each writer serializes."""
    e["keywords"] = keywords
    if e.get("type") == "heuristic":
        e["trigger"] = keywords


# the types a candidate can be born as (premise refused — the human-only rail)
_CANDIDATE_TYPES = ("prior", "lexicon", "heuristic", "reference")


def _pick_candidate(eid, project=None, ctype=None):
    """Resolve a confirm/reject/xrev-clear id against the REVIEWABLE set first
    (candidate + provisional) — bare _find is typed-first (prior > heuristic >
    reference > lexicon), and with entries mintable in all four types a slug
    shared across types would silently ratify/retire the WRONG entry. >1
    same-slug reviewable without a type qualifier is REFUSED with the exact
    disambiguation; zero reviewable hits falls back to _find so the not-found /
    not-a-candidate errors keep their precision."""
    if ctype:
        ctype = "prior" if ctype == "premise" else ctype
        if ctype not in _CANDIDATE_TYPES:
            return None, "unknown --type '%s' (one of: %s)" \
                % (ctype, ", ".join(_CANDIDATE_TYPES))
    types = (ctype,) if ctype else None
    want = _slug(str(eid or ""))
    hits = [e for e in reviewable(project=project, types=types)
            if _slug(str(e["id"])) == want]
    if len(hits) > 1:
        return None, "'%s' is ambiguous — %d candidates share the id (%s); " \
            "re-run with --type <type>" \
            % (eid, len(hits), ", ".join(sorted(e["type"] for e in hits)))
    if hits:
        return hits[0], None
    e = _find(eid, project=project, types=types)
    if not e:
        return None, "'" + str(eid) + "' not found"
    return e, None


def confirm(eid, ts, new_statement=None, project=None, ctype=None,
            force=False, guard_notes=None):
    """Owner ratify -> live (the owner/confirm gate that makes inferred capture
    safe to leave on) — works on BOTH a candidate (fires nothing) AND a
    provisional (xrev-cleared, already firing tagged): either way the owner's
    ratification makes it human-canon. A candidate crosses the entry-mint guard
    against the CURRENT corpus before activation; a provisional already crossed
    it at xrev-clear, so provisional -> live stays a lifecycle-only rewrite.
    ``force`` and ``guard_notes`` are appended parameters to preserve every
    historical positional call. --edit swaps the statement in the same turn.
    source flips to 'explicit' — the knowledge is now human-confirmed. A prior
    carries the who/when receipt in its own evidence_log (confidence untouched —
    confirming ratifies the capture, never inflates the belief); the other types'
    receipt is the events journal row. ctype disambiguates a slug shared across
    reviewable types (ambiguity without it is refused — never ratify the wrong
    entry)."""
    bad = _refuse_bad_ts(ts)
    if bad:
        return None, bad
    e, err = _pick_candidate(eid, project=project, ctype=ctype)
    if err:
        return None, err
    prev = e.get("status")
    if prev not in (STATUS_CANDIDATE, STATUS_PROVISIONAL):
        return None, "'%s' is not a candidate or provisional entry (status=%s)" \
            % (eid, prev)
    notes, events = [], []
    if prev == STATUS_CANDIDATE:
        keywords, err, notes, events = guard_entry_keywords(
            e["type"], e["id"], _entry_keywords(e), project=project,
            force=force, exclusions=(e,))
        if err:
            return None, err
    edited = bool(str(new_statement or "").strip())
    if edited:
        s = new_statement.strip()
        # A GLOSS IS DERIVED FROM THE STATEMENT, so an edit invalidates it.
        # Keeping it here would fire a line the entry no longer says — worse
        # than the truncation the gloss exists to prevent, because a severed
        # sentence is VISIBLY incomplete while a stale gloss is confidently
        # wrong. Dropped rather than guessed at: the author re-writes it,
        # knowing the new statement. (codex review 2026-07-30, finding 2.)
        if e.get("gloss") and str(e.get("statement") or "") != s:
            e["gloss"] = ""
        e["statement"] = s
        alias = _STMT_ALIAS.get(e["type"])
        if alias:
            e[alias] = s
    if prev == STATUS_CANDIDATE:
        _set_entry_keywords(e, keywords)
    note = prev + " -> live" + (" (edited)" if edited else "")
    e.update({"status": STATUS_LIVE, "source": "explicit",
              "updated_ts": ts, "last_updated": ts})
    if e["type"] == "prior":
        e["evidence_log"] = list(e.get("evidence_log") or []) + [
            {"ts": ts, "type": "confirmed", "delta": 0, "reason": note, "by": "human"}]
    _WRITERS[e["type"]](e, path=e["path"])
    record_mint_events(events)
    if guard_notes is not None:
        guard_notes.extend(notes)
    pk.event("store.confirm", str(e["id"]), note)
    return e, None


def reject(eid, ts, why="", project=None, ctype=None):
    """The wrong-inference exit: a candidate OR a provisional -> retired IN PLACE
    (the record law: the file STAYS, never deleted — a rejected inference is
    itself knowledge). Works on both non-ratified states (the owner may reject a
    provisional that xrev cleared but is wrong). Refuses live entries (retire is
    the live-entry verb) and cross-type slug ambiguity without a ctype qualifier
    (same law as confirm); drain --expire-candidates remains the age leg for the
    never-reviewed."""
    e, err = _pick_candidate(eid, project=project, ctype=ctype)
    if err:
        return None, err
    prev = e.get("status")
    if prev not in (STATUS_CANDIDATE, STATUS_PROVISIONAL):
        return None, "'%s' is not a candidate or provisional entry (status=%s) " \
            "— retire handles live entries" % (eid, prev)
    e.update({"status": STATUS_RETIRED, "retired_ts": ts,
              "retired_why": why or "rejected", "updated_ts": ts, "last_updated": ts})
    _WRITERS[e["type"]](e, path=e["path"])
    pk.event("store.reject", str(e["id"]), (prev + " rejected") + ((" — " + why) if why else ""))
    return e, None


def _notify_graduation(etype, eid):
    """Fire ONE optional push when a candidate graduates to provisional — the
    owner steer: the provisional queue must ROUTINELY reach the owner, never
    wait silently. The channel is `notify.owner_push` — the ONE phone path off
    this box (HELM_NTFY_TOPIC), shared with proxywatch's family edges and
    beacons' reachability alarm rather than re-implemented here. UNSET is a
    deliberate opt-out and makes no network call; any error is journaled as a
    one-line receipt and the graduation still succeeds — a notifier NEVER
    breaks the verb."""
    from .. import notify
    notify.owner_push(
        "helm: %s %s now provisionally live - review when convenient"
        % (etype, eid),
        title="[helm store] review queue",
        receipt=("store.notify_failed", str(eid)))


def xrev_clear(eid, ts, by, project=None, ctype=None, force=False,
               guard_notes=None):
    """The graduation gate: a candidate -> provisional after a cross-family /x
    review clears it (owner canon: xrev is the gate, not the owner). The
    candidate crosses the entry-mint guard against the CURRENT corpus before it
    can begin firing. ``force`` and ``guard_notes`` are appended parameters so
    every historical positional call keeps its meaning. The reviewer ATTESTS a
    cross-family review happened — this verb records the who/when, it NEVER runs
    the review itself. A provisional entry FIRES through the resolver like live
    but renders with a visible [provisional] tag until the owner ratifies
    (confirm) or rejects (reject) it in the web review panel. The receipt lands
    in xrev_by/xrev_ts on the file (all types) + the events journal, and a prior
    also logs it to its own evidence_log. On graduation it fires ONE optional
    push (HELM_NTFY_TOPIC) so the provisional queue reaches the owner — fail-open,
    never blocks the graduation. Refuses a missing reviewer, a non-candidate,
    and cross-type slug ambiguity (same law as confirm/reject)."""
    if not str(by or "").strip():
        return None, "xrev-clear requires --by <reviewer> " \
            "(who attests the cross-family review cleared it)"
    bad = _refuse_bad_ts(ts)
    if bad:
        return None, bad
    e, err = _pick_candidate(eid, project=project, ctype=ctype)
    if err:
        return None, err
    if e.get("status") != STATUS_CANDIDATE:
        return None, "'%s' is not a candidate (status=%s) — xrev-clear graduates " \
            "candidates only" % (eid, e.get("status"))
    keywords, err, notes, events = guard_entry_keywords(
        e["type"], e["id"], _entry_keywords(e), project=project,
        force=force, exclusions=(e,))
    if err:
        return None, err
    _set_entry_keywords(e, keywords)
    by = str(by).strip()
    note = "candidate -> provisional (xrev-cleared by %s)" % by
    e.update({"status": STATUS_PROVISIONAL, "xrev_by": by, "xrev_ts": ts,
              "updated_ts": ts, "last_updated": ts})
    if e["type"] == "prior":
        e["evidence_log"] = list(e.get("evidence_log") or []) + [
            {"ts": ts, "type": "xrev-cleared", "delta": 0, "reason": note, "by": by}]
    _WRITERS[e["type"]](e, path=e["path"])
    record_mint_events(events)
    if guard_notes is not None:
        guard_notes.extend(notes)
    pk.event("store.xrev_clear", str(e["id"]), note)
    _notify_graduation(e["type"], str(e["id"]))
    return e, None


def demote(eid, ts, reason, by="human", project=None, undo=False):
    """The pinned lane's growth path: flip an always-entry OUT of the lane
    (always -> jit) — NEVER silent, never a delete (the memGC demote law: the
    statement, its history and its file all stay; only the injection tier
    moves). A prior's 'demoted' evidence receipt carries the exact prior state
    (was: {load_class, pin}); undo=True replays that receipt's was-state back —
    restoration is one provenanced flip — and appends 'undemoted'. References
    flip their authored load_class the same way; their receipt is the events
    journal row (refs carry no evidence_log)."""
    if not str(reason or "").strip():
        return None, "a reason is required (silent tier flips are forbidden)"
    e = _find(eid, project=project, types=("prior", "reference"))
    if not e:
        return None, "'" + str(eid) + "' not found"
    if undo:
        if e.get("load_class") == "always":
            return None, "'%s' is already in the always lane" % e["id"]
        was = next((r.get("was") for r in reversed(e.get("evidence_log") or [])
                    if isinstance(r, dict) and r.get("type") == "demoted"
                    and isinstance(r.get("was"), dict)), None)
        if e["type"] == "prior" and was is None:
            return None, "'%s' has no demote receipt to undo" % e["id"]
        was = was or {"load_class": "always", "pin": False}
        e["load_class"] = was.get("load_class") or "always"
        e["pin"] = "true" if was.get("pin") else ""
        kind = "undemoted"
    else:
        if e.get("load_class") != "always":
            return None, "'%s' is not in the always lane (load_class=%s)" \
                % (e["id"], e.get("load_class"))
        was = {"load_class": "always", "pin": bool(e.get("pinned"))}
        e["load_class"] = "jit"
        # an explicit un-pin: beats both the pin flag and the PINNED_SLUGS tuple
        e["pin"] = "false" if was["pin"] else ""
        kind = "demoted"
    if e["type"] == "prior":
        e["evidence_log"] = list(e.get("evidence_log") or []) + [
            {"ts": ts, "type": kind, "delta": 0, "reason": reason, "by": by,
             "was": was}]
    e["last_updated"] = ts
    _WRITERS[e["type"]](e, path=e["path"])
    pk.event("store.undemote" if undo else "store.demote", str(e["id"]),
             ("-> %s — %s" % (e["load_class"], reason)) if undo
             else "always -> jit — " + reason)
    return e, None


def pinned_stats(project=None):
    """The pinned lane's MEASURED reality, READ-ONLY: the deterministic budget
    walk exactly as inject renders it now (who fits under PINNED_BUDGET with
    inject's own line shape), plus each always-entry's made-the-budget count
    over the fire-ledger window (current + rotated generation; a row counts
    when it carries a fired.pinned lane). Nothing here writes — the ledger is
    inject's, borrowed through its one path/line/budget surface."""
    from .. import inject
    always = pinned(project=project)
    # SEED THE WALK WITH THE WHO DIGEST, because gather does. This started at
    # used=0 and was therefore a MODEL of the walk rather than the walk: the
    # real one (inject/_whisper.py) emits the WHO digest FIRST and only then
    # walks the pinned entries, so the budget an entry actually competes for is
    # PINNED_BUDGET minus the digest. MEASURED 2026-07-30: who = 350 bytes
    # (its whole WHO_CAP), leaving 850 of 1200 — the model reported 3 entries
    # fitting where the live walk fits 2. An OPTIMISTIC starvation predicate is
    # the same failure as no predicate at all for the entry it silently clears.
    #
    # Read through the same helper gather uses, never a re-derivation: a second
    # spelling of the digest would drift from the first and put this right back
    # to modelling. `inject._who_lines` is the package re-export gather itself
    # calls — NOT `from ..inject import _whisper`, which silently yields the
    # FUNCTION of that name (inject/__init__.py re-exports a `_whisper` symbol
    # that shadows the submodule) and raises AttributeError on the call. The
    # fail-open below then swallowed it and returned the optimistic answer with
    # no sign anything was wrong: the first cut of this fix reported 3 entries
    # fitting where the live walk fits 2, which is the same silent-clear the
    # whole lane exists to end.
    try:
        who_bytes = sum(len(l) for l in inject._who_lines())
        if who_bytes > inject.PINNED_BUDGET:
            who_bytes = 0        # gather drops the digest whole rather than truncate
    except Exception:            # noqa: BLE001 — stats never dies on the digest
        who_bytes = 0
    fits, used = set(), who_bytes
    for e in always:
        line = inject._entry_line(e)
        if used + len(line) > inject.PINNED_BUDGET:
            break  # gather's greedy walk: the first overflow ends the lane
        fits.add(str(e["id"]))
        used += len(line)
    made = {str(e["id"]): 0 for e in always}
    path = inject._ledger_path()
    lines = []
    for p in (path + ".1", path):
        try:
            with open(p, encoding="utf-8") as f:
                lines += f.read().splitlines()
        except OSError:
            continue
    rows, first, last = 0, "", ""
    for ln in lines:
        try:
            r = json.loads(ln)
        except ValueError:
            continue
        ids = (r.get("fired") or {}).get("pinned") if isinstance(r, dict) else None
        if not isinstance(ids, list):
            continue
        rows += 1
        ts = str(r.get("ts") or "")
        first, last = first or ts, ts or last
        for i in ids:
            if str(i) in made:
                made[str(i)] += 1
    return {"always": always, "fits": fits, "used": used,
            "budget": inject.PINNED_BUDGET, "rows": rows,
            "first": first, "last": last, "made": made}
