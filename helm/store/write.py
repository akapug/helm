"""helm store — writers + lifecycle.

The byte-shape-compatible type writers, the _WRITERS dispatch, and the
lifecycle verbs (evidence/supersede/retire/confirm/reject/xrev-clear/demote)
plus pinned_stats. Moved verbatim from the pre-split helm/store.py.
"""
import json
import os
import re
import sys

from .. import pk, promptcensus
from ._common import (
    _slug, _coerce_conf, CERTAIN, BELIEF_CLAMP, derive_class, derive_load_class,
    _is_pinned, _json1, _pending_lines, _timestamp_scalar, PRIOR_PREFIX, STATUS_LIVE, STATUS_RETIRED,
    STATUS_DELETE_ELIGIBLE, STATUS_CANDIDATE, STATUS_PROVISIONAL,
    INJECTABLE_STATUSES, GENERIC_KEYWORDS, _HEURISTIC_GENERIC, _JIT_TYPES,
    PINNED_SLUGS,
)
from .load import (FLEET, _default_dir, _find, load_all, reviewable, gate_ids,
                   gate_candidates, gate_key, resolve_gate, split_gate,
                   find_typed, typed_id)
from .resolve import (route_cell, legacy_route_cells, _df_map, _jit_candidates, _probe_hits,
                      pinned, resolve_prompt)
from .index import DUP_OVERLAP, _tokens


# ---------------------------------------------------------------------------
# writers — byte-shape-compatible with the predecessor's writers (same key order)
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


def _gates_lines(e):
    """[] or ["  gates: a,b"] — the rule's preconditions (task/1346), emitted
    only when present so every gate-less entry keeps its byte shape. One
    helper for every writer, for the same reason as _gloss_lines."""
    gl = ",".join(g.strip() for g in str(e.get("gates") or "").split(",")
                  if g.strip())
    return ["  gates: " + gl] if gl else []


def _evidence_lines(e):
    """The evidence-receipt line for the NON-PRIOR writers, and ONLY when there
    IS one (#951). write_prior serializes evidence_log unconditionally (its
    historical byte shape); for heuristic/reference/lexicon an unconditional
    line would rewrite every existing file, which the round-trip test correctly
    refuses — the same conditional-emission law as _pending_lines/_gloss_lines.
    The receipt lives IN the artifact because the events journal is a lossy
    rotating trail by charter (pk.event: "receipts, never truth"), so a
    journal-only receipt would be a success whose only trace can silently
    age out."""
    return (["  evidence_log: " + _json1(e["evidence_log"])]
            if e.get("evidence_log") else [])


def _commit(path, body):
    """Write an entry ONLY after proving the row it will LOAD AS renders inside
    the budget. THE ARTIFACT IS THE ORACLE — never a model of it.

    THREE ROUNDS OF REVIEW DIED ON THE MODEL. The first validator rendered the
    writer's raw input (no type, no derived class) and measured the generic
    branch; the second normalized a probe and a caller-supplied `type` still
    overrode write_prior's authority; each round found the next site that
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
    emoji is 114 chars and 414 bytes and passed a 400 check (r3).
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
    # lock, and a probe reproduced the contamination: writer A pauses inside
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
        # CLEANUP FAILURE IS NOT SILENT. The second repro: a rejected
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


# The attestation annotations a prior carries (premise/_capture annotates;
# write_prior carries them through every rewrite; the doctor reads them from
# the ARTIFACT to decide whether a rewrite is allowed at all). attest_record
# is the native hash-chain record (the primary proof), attest_anchor* the
# optional dregg anchor, attest_turn/attest_receipt/attest_supersedes_turn the
# legacy pre-native pointers (read-only).
_ATTEST_KEYS = ("attest_payload", "attest_ts", "attest_by", "attest_record",
                "attest_chain_index", "attest_supersedes_record",
                "attest_anchor", "attest_anchor_turn",
                "attest_turn", "attest_receipt", "attest_supersedes_turn")


def write_prior(e, root_dir=None, path=None):
    """Write a prior-*.md (the predecessor priors._write shape). class/load_class re-derived
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
        *_pending_lines(e),
        "  domain: " + (e.get("domain") or ""),
        "  keywords: " + (e.get("keywords") or ""),
        "  stated_ts: " + str(e.get("stated_ts") or ""),
        "  last_updated: " + str(e.get("last_updated") or e.get("stated_ts") or ""),
        "  source: " + (e.get("source") or "human"),
        "  evidence_log: " + _json1(e.get("evidence_log")),
        "  confidence_history: " + _json1(e.get("confidence_history")),
    ]
    # CASEFOLDED, because a policy kind is an IDENTIFIER and not prose. A probe
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
    body += _gates_lines(e)
    if pin:
        body.append("  pin: true")
    elif str(e.get("pin") or "").strip().lower() in ("false", "0", "no"):
        # an explicit un-pin (demote) must survive the rewrite — on read it
        # beats the PINNED_SLUGS tuple, which is what keeps the flip stuck
        body.append("  pin: false")
    # attestation annotations survive rewrites — the native chain is the truth,
    # but a store rewrite (evidence/retire) must never orphan the pointer keys
    for opt in _ATTEST_KEYS:
        if e.get(opt) not in (None, ""):
            body.append("  " + opt + ": " + str(e[opt]))
    for opt in ("project", "supersedes", "replaced_by", "source_prior",
                "xrev_by", "xrev_ts"):
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
    """Write a lex-*.md (the predecessor lexicon._write_term shape)."""
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
    body += _gates_lines(e)
    # "project" = the project this entry is ABOUT (task/2435); emitted only
    # when recorded, so an entry without one keeps its historical bytes.
    for opt in ("project", "xrev_by", "xrev_ts"):  # + the provisional-graduation receipt
        if e.get(opt):
            body.append("  " + opt + ": " + str(e[opt]))
    # CONDITIONAL, matching this writer's shape rather than the other three:
    # a live lexicon keeps its historical byte-shape, so an always-emitted key
    # would rewrite every existing file. Absent means no revision staged.
    # (My pattern for the other writers anchored on their unconditional
    # `status:` line, which this one does not have — 3 of 4 patched and this
    # one silently skipped. An enumerator that misses one writer makes
    # `revise` a no-op for exactly one type.)
    body += _pending_lines(e)
    body += _evidence_lines(e)
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
    """Write a heuristic-*.md (the predecessor heuristics_store._write shape). confidence is
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
        *_pending_lines(e),
        "  stated_ts: " + str(e.get("stated_ts") or ""),
        "  last_updated: " + str(e.get("last_updated") or e.get("stated_ts") or ""),
        "  source: " + (e.get("source") or "human"),
    ]
    body += _gloss_lines(e)
    body += _gates_lines(e)
    body += _evidence_lines(e)
    for opt in ("project", "supersedes", "replaced_by", "xrev_by", "xrev_ts"):
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
        *_pending_lines(e),
        "  stated_ts: " + str(e.get("stated_ts") or ""),
        "  last_updated: " + str(e.get("last_updated") or e.get("stated_ts") or ""),
        "  source: " + (e.get("source") or "harvest"),
    ]
    body += _gloss_lines(e)
    body += _gates_lines(e)
    body += _evidence_lines(e)
    for opt in ("project", "supersedes", "replaced_by", "xrev_by", "xrev_ts"):
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
# lifecycle — evidence, supersede, retire (the predecessor's laws, root-aware)
# ---------------------------------------------------------------------------

def _resolve_write(eid, verb, types, project=None):
    """The ONE admission door for id-resolving write verbs (#951). -> (e, err).

    THE DEFECT THIS CLOSES, measured 2026-08-11: same cwd, same id, two doors,
    two answers — `get` resolved a helm-global heuristic and `evidence`
    answered "not found" about it, because each write verb carried its own
    typed _find and a miss OUTSIDE the verb's type set was indistinguishable
    from a miss everywhere. "not found" about an id the read door resolves is
    a lie, and it landed on exactly the entries with the widest reach.

    RESOLVE UNTYPED ONCE, THEN ADMIT OR REFUSE THE WINNER (verdict
    on a75aecaea36a, at this lane's own first tip): the first cut filtered to
    the verb's types BEFORE the _TYPE_ORDER winner choice, so a heuristic and
    a reference sharing one slug meant `get` showed the heuristic while
    `demote` (prior/reference) silently wrote the REFERENCE — the same
    two-doors divergence this door exists to kill, except now it WRITES to a
    record the read door does not show. The winner is chosen exactly as `get`
    chooses it (untyped _find: all roots, project shadows helm-global shadows
    adopted, typed-first precedence over the FULL candidate set); a winner
    outside the verb's set is refused BY NAME, and the door never falls
    through to a lower-precedence candidate of an admitted type — that
    fallthrough IS the divergence.

    Three answers, distinguished: the winner's type is INSIDE the verb's set
    -> (entry, None) — the same record `get` shows. Outside it -> the
    truthful refusal naming the type, never "not found". The id resolves
    NOWHERE -> (None, None), and the caller keeps its own historical
    not-found phrasing.

    READS THE TYPED SPELLING TOO (`reference:dup-b`, find_typed — the form
    every remediation command the store prints carries, typed_id): a bare
    slug resolves the untyped WINNER, and a prior outranks a reference, so
    a printed `supersede a b` about a measured REFERENCE pair tombstoned the
    unrelated PRIOR sharing the slug. A typed operand resolves exactly that
    type; a typed operand outside the verb's set is refused by name."""
    e = find_typed(eid, project=project)
    if not e:
        return None, None
    if e["type"] not in types:
        return None, ("'%s' resolves as type %s — %s applies to %s"
                      % (eid, e["type"], verb, "/".join(types)))
    return e, None


# Every type with a round-tripping writer takes evidence; the non-prior three
# are certain BY CONSTRUCTION (parse pins confidence 1.0, their writers
# serialize no confidence field), so for them the verb is the certain-prior
# law generalized: the receipt LANDS, the confidence does not move.
_EVIDENCE_TYPES = ("prior", "heuristic", "reference", "lexicon")


def apply_evidence(pid, ts, delta, reason, by="agent", kind=None, project=None):
    """Move a prior's confidence by `delta`, appending the receipt to
    evidence_log + a snapshot to confidence_history. A belief clamps to
    [0.05, 0.99] (never auto-1.0). A certain-prior (human truth) is NOT
    auto-demoted by agent evidence — the contradiction is LOGGED (the drift
    report reads exactly that) and surfaced, confidence stays pinned. An
    un-reasoned move is refused. Writes back IN PLACE on whichever root holds
    the entry — including adopted (the adoption contract).

    Resolves every type `get` resolves (#951, via _resolve_write): a
    heuristic/reference/lexicon is certain by construction — its parse pins
    confidence at 1.0 and its writer serializes no confidence field — so the
    certain-prior law generalizes: the receipt lands in the entry's own
    evidence_log (durable, in the artifact — the events journal is a lossy
    trail by charter) and is surfaced, while confidence never moves. Before
    this the verb answered "not found" about every one of them, so the global
    canon entries with the widest reach were exactly the ones no seat could
    attach a correction receipt to."""
    e, err = _resolve_write(pid, "evidence", _EVIDENCE_TYPES, project=project)
    if err:
        return None, err
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
    if e["type"] != "prior":
        new = old
        surfaced_msg = ("certain-by-construction (%s): evidence LOGGED in the "
                        "entry + surfaced, confidence stays %.2f — correct with "
                        "revise/supersede/retire" % (e["type"], old))
    elif e["class"] == "certain":
        new = min(CERTAIN, max(BELIEF_CLAMP[0], new)) if by == "human" else old
        if by != "human":
            surfaced_msg = ("certain-prior: agent evidence is LOGGED + surfaced as "
                            "drift (contradicted), confidence not auto-applied")
    else:
        new = max(BELIEF_CLAMP[0], min(BELIEF_CLAMP[1], new))
    e["evidence_log"] = list(e.get("evidence_log") or []) + [
        {"ts": ts, "type": (kind or ("support" if d >= 0 else "contradict")),
         "delta": round(d, 4), "reason": reason, "by": by}]
    if e["type"] == "prior":
        # history + the confidence move are the PRIOR half only: the other
        # writers serialize neither field, and an in-memory append a writer
        # drops is the writer-emits-what-the-reader-drops loss with extra steps
        e["confidence_history"] = list(e.get("confidence_history") or []) + [
            {"ts": ts, "value": round(new, 4), "reason": reason}]
        e["confidence"] = new
    # the freshness field each writer actually serializes (lexicon: updated_ts)
    e["updated_ts" if e["type"] == "lexicon" else "last_updated"] = ts
    _WRITERS[e["type"]](e, path=e["path"])
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
    # lexicon stays OUT on purpose: write_lexicon serializes no supersedes/
    # replaced_by, so admitting it would silently drop the tombstone fields —
    # but the refusal now names that truth instead of claiming "not found"
    writable = ("prior", "heuristic", "reference")
    old, err = _resolve_write(old_id, "supersede", writable, project=project)
    if err:
        return None, err
    if not old:
        return None, "old entry '" + old_id + "' not found"
    new, err = _resolve_write(new_id, "supersede", writable, project=project)
    if err:
        return None, err
    if not new:
        return None, "new entry '" + new_id + "' not found (the replacement must exist first)"
    # THE SELF-SUPERSEDE FENCE COMPARES RESOLVED IDENTITY — the loaded
    # record's own (type, slug), the key the loader files it under — after
    # BOTH operands resolved and before either write. Comparing the raw
    # operands' slugs did two wrong things: `a` and `prior:a` (or `prior:a`
    # and `premise:a`) spell one artifact and passed, so the old copy wrote a
    # tombstone and the separately loaded new copy wrote live status back
    # with supersedes:a — the CLI reported TOMBSTONED over a live self-link;
    # and the slug's 60-character cap, applied to a TYPED address, truncated
    # two distinct legal 60-character ids sharing a prefix into one refusal.
    if (old["type"], _slug(str(old["id"]))) == (new["type"], _slug(str(new["id"]))):
        return None, "an entry cannot supersede itself"
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
_PROBE_CAP = 24         # GENERATED stems a mint may append — bounds index weight; never an authored limit (doctor keeps every authored cell)
_DF_GENERIC = 3         # a SOLE probe carried by >= this many live entries discriminates nothing
_DUP_OVERLAP_PROBES = 2  # shared probes that make an existing entry a strong sibling
_DUP_TOP_CELLS = 2      # keyword cells an existing entry must top-rank to be a strong sibling
_DUP_MIN_WORDS = 5      # WORD MASS of that shared evidence — see _dup_word_mass

# The decomposition stopword set. Deliberately TINY and closed-class — this
# filters WITHIN an author's phrase when forming stems, where a broad list
# (GENERIC_KEYWORDS) would deform bigrams by deleting topical-but-common words.
_STEM_STOPWORDS = frozenset((
    "the", "a", "is", "of", "to", "for", "and", "or", "not", "my", "it"))

# The smallest statement-df that can count as "corpus-common" — the floor keeps
# a near-empty store from calling every word common (n//100 is 0 there).
_STEM_COMMON_FLOOR = 4

# THE PROMPT SIDE OF "COMMON" (task/2978): a word the prompt census
# (helm.promptcensus) sees in at least this share of recent turns is not minted
# as a stem. The statement corpus measures the store's own prose; the probes
# fire on prompts, and the two disagree exactly where it costs (`summary`: 1
# verified statement, 16.5% of turns). THE VALUE IS A MEASURED KNEE, the same
# method as _DUP_MIN_WORDS: removing every GENERATED stem that fails the bar
# from the live store (stem_unfit) and replaying the E2 gold through the real
# resolver and census, 0.40 is the lowest bar that keeps every
# consensus-relevant pair admitted on both splits, and 0.35 drops one: `land`
# reaches 35.6% of turns counting its inflections, and it is the corroborating
# stem of one-whole-gate-lands-the-whole-stacked-train, whose other matched
# stem (`lanes`) the lone-word law then holds. 0.40 through 0.60 read the same
# on the gold. A LONE common word is the resolver's job (resolve._LONE_COMMON);
# this bar only stops minting words so common that even as corroboration they
# mostly add noise.
_STEM_PROMPT_COMMON = 0.40


def _weak_shape(w):
    """A word no stem may be minted from ALONE, whatever the corpora say: two
    characters or fewer, or no letter at all (task/2978). MEASURED on the live
    store: 31 entries carried a numeric probe and 120 a probe of <= 2
    characters, and the generated stem `1` fired
    silent-death-must-never-render-as-in-progress on 18 turns. A number or a
    two-letter token is a fragment of the author's phrase, never its symptom."""
    return len(w) <= 2 or not re.search(r"[a-z]", w)


class _CorpusProfile:
    """One statement-corpus measurement: the stop-list AND the numbers that
    produced it, so a refusal can SHOW its arithmetic instead of asserting it.

    n      DISTINCT VERIFIED statements measured (a near-duplicate cluster
           among them counts once)
    cut    the >= statement-df threshold
    df     word -> statement-df  common frozenset(w for w in df if df >= cut)
    folded [(verified duplicate, the earlier verified entry it folded into)]
    attested        rows whose seal verified (before folding)
    unsealed        rows that cast no vote (no seal, or a seal that did not
                    verify)
    floor_measured  attested > 0 — with no verified carrier the floor is
                    UNMEASURABLE and `common` is empty by construction
    dups   [(duplicate entry, the earlier entry it duplicates)] over EVERY
           row, sealed or not — the doctor's retrieval-hygiene report, which
           never feeds a vote"""

    __slots__ = ("n", "cut", "df", "common", "folded", "attested", "unsealed",
                 "floor_measured", "dups")

    def __init__(self, n, cut, df, common, folded=(), attested=0, unsealed=0,
                 dups=()):
        self.n, self.cut, self.df, self.common = n, cut, df, common
        self.folded, self.dups = list(folded), list(dups)
        self.attested, self.unsealed = attested, unsealed
        self.floor_measured = attested > 0


def _dup_folds(entries):
    """cluster index per row — a row whose statement is a NEAR-DUPLICATE
    (token-set Jaccard >= DUP_OVERLAP, the add door's own supersede-not-
    duplicate law) of an EARLIER row joins that row's cluster.

    THE POISONING THIS CLOSES (row e43ec4309e89): corpus_profile
    counted every admitted statement as an independent vote, so FOUR rows
    pasted from one sentence with a counter reached the floor (cut 4) and made
    the rare stem they carried "common" — the generated stem of a genuine
    capture was then refused on the say-so of one contributor repeated. The
    store already says what a repeated statement is: `add` warns at this very
    overlap that the row should have been a supersede, not an accumulation.
    The corpus measure now counts what the duplicate law counts — one
    contributor — and the doctor names the copies with the supersede cure.

    Four DISTINCT statements carrying a word still common it at the floor:
    that is the pinned floor law (the arms that hold `_STEM_COMMON_FLOOR` by
    value), not a defect, and this fold does not touch it.

    PREFIX-FILTERED so a full-corpus pass stays cheap on every stemmable add:
    two token sets with Jaccard >= t share at least one token among the
    (|A| - ceil(t*|A|) + 1) rarest of each under ONE global order, so only
    rows sharing a rare prefix token are compared exactly. Integer ceiling —
    a float 0.8*n lands above an exact multiple and would shorten the prefix
    by one, which is a missed pair, not a rounding nit."""
    toks = [_tokens(e.get("statement")) for e in entries]
    tf = {}
    for t in toks:
        for w in t:
            tf[w] = tf.get(w, 0) + 1
    pct = int(round(DUP_OVERLAP * 100))
    seen = {}                      # prefix token -> rows that carried it
    rep = list(range(len(entries)))
    for i, t in enumerate(toks):
        if not t:
            continue
        need = -(-(pct * len(t)) // 100)          # ceil(DUP_OVERLAP * |t|)
        prefix = sorted(t, key=lambda w: (tf[w], w))[:len(t) - need + 1]
        cands = set()
        for w in prefix:
            cands.update(seen.get(w, ()))
        for j in sorted(cands):
            u = toks[j]
            if len(t & u) / len(t | u) >= DUP_OVERLAP:
                rep[i] = rep[j]
                break
        for w in prefix:
            seen.setdefault(w, []).append(i)
    return rep


def _claims_seal(e):
    """Whether a loaded row CLAIMS a native attestation (payload or record in
    its frontmatter) — the regime switch for corpus_profile, read from the
    dict the loader already filled, never a file reread."""
    return bool(str(e.get("attest_payload") or e.get("attest_record") or "").strip())


def _statement_words(e):
    """The word set the floor votes with: the statement's lowercased tokens."""
    return set(re.findall(r"[a-z0-9][a-z0-9'-]*",
                          str(e.get("statement") or "").lower()))


def _floor_profile(word_sets, folded=(), attested=None, unsealed=0, dups=()):
    """THE FLOOR ARITHMETIC over the word sets of DISTINCT VERIFIED
    statements — one set per contributor, eligibility and folding already
    decided by corpus_profile. Pure, so the moving threshold (n // 100) and
    the floor can be measured exactly at any n without seeding files. With
    no contributor the floor is unmeasurable: `common` is empty and nothing
    is demoted, whatever the words."""
    word_sets = list(word_sets)
    df = {}
    for ws in word_sets:
        for w in ws:
            df[w] = df.get(w, 0) + 1
    n = len(word_sets)
    cut = max(_STEM_COMMON_FLOOR, n // 100)
    common = frozenset(w for w, c in df.items() if c >= cut) if n else frozenset()
    return _CorpusProfile(n, cut, df, common, folded,
                          len(word_sets) if attested is None else attested,
                          unsealed, dups)


def corpus_profile(entries, project=None):
    """corpus_common's measurement, undiscarded — see corpus_common.

    THE CONTRACT: the stem-common floor counts DISTINCT VERIFIED STATEMENTS
    — a seal that verifies (_attest_verdict, the premise-check contract) is
    the provenance, an unsealed row casts no vote, and four verified
    statements sharing a stem make it common by design, because verified
    rows ARE the store's authority; that narrower reading is the accepted
    one.

    IN THAT ORDER, per row: tokens, then seal eligibility, then folding of
    near-duplicates AMONG THE VERIFIED ROWS ONLY. An unsealed row
    contributes zero tokens to any stem's vote and cannot inherit a sealed
    sibling's eligibility by resembling it — four verified statements each
    shadowed by an unsealed copy that appends one word give that word zero
    votes, not four. Folding closes the pasted shape and provenance closes
    the planted one; the floor itself is pinned. A store with NO verifying
    carrier cannot measure its floor: nothing is demoted to common,
    `floor_measured` is False and the doctor names it. The chain is read
    once for the whole pass. `dups` is the near-duplicate report over every
    row — retrieval hygiene, never a vote."""
    from ..premise._chain import chain_records
    entries = list(entries)
    recs = None
    verified = []
    for i, e in enumerate(entries):
        if not _claims_seal(e):
            continue
        if recs is None:
            recs = chain_records()
        if _attest_verdict(e, project, recs)[0] == "verified":
            verified.append(i)
    vrows = [entries[i] for i in verified]
    vrep = _dup_folds(vrows)
    clusters = {}
    for k, i in enumerate(verified):
        clusters.setdefault(vrep[k], set()).update(_statement_words(entries[i]))
    folded = [(vrows[k], vrows[vrep[k]]) for k in range(len(vrows)) if vrep[k] != k]
    arep = _dup_folds(entries)
    dups = [(entries[i], entries[arep[i]]) for i in range(len(entries)) if arep[i] != i]
    return _floor_profile(clusters.values(), folded, len(verified),
                          len(entries) - len(verified), dups)


def corpus_common(entries, project=None):
    """The words the store's own statements use EVERYWHERE — the corpus-built
    stop-list that keeps an auto-stem DISCRIMINATING (task/1077 defect 1).

    THE INCIDENT, measured twice in one hour on 2026-08-11: the /learn
    discipline says prefer phrases, the guard auto-stems >=3-word phrases, and
    the stems it minted — bare `built`, `works`, `wired`, `back`, `came`, then
    `id`, `content`, `matches`, `valid` minutes later — made half-arc-delivery
    fire on "lunch break, back in twenty minutes". Following the rule produced
    the violation, and both captures needed manual pruning.

    WHY PROBE-DF CANNOT BE THE GATE, measured on the live 1430-candidate store
    before choosing this one: every incident spammer was RARE in the probe
    vocabulary (df: built=2, came=1, id=0, max 3), so a probe-df threshold
    would have admitted all nine — and their rarity is the damage, because a
    matched probe scores 1/df, so a common-English word that few entries carry
    fires with near-maximal weight and DISPLACES a real rule out of the cap-4
    window. It is the GENERIC_KEYWORDS `should` inversion again:
    rare-yet-meaningless is exactly the population probe-df misreads.

    THE STATEMENT CORPUS IS THE MEASURE THAT SEPARATES THEM. The store's own
    statements are written in the fleet's working language — the same language
    prompts arrive in — so a word carried by many STATEMENTS is a word ordinary
    turns will keep containing. Measured on the same store: the nine spammers
    sit at statement-df 14..72 (top decile), while the genuine symptom stems a
    capture needs (`stash` 8, `freezes` 2, `pycache` 5, `tombstone` 3) sit
    under 11. The threshold max(4, n//100) lands at 14 on the live corpus:
    all nine spammers refused, the symptom vocabulary kept.

    Self-updating on purpose: computed from the candidate set the guard is
    already holding, never persisted, so it scales with the store and needs no
    curation — a hand-kept list would be GENERIC_KEYWORDS' maintenance burden
    grown without bound.

    THE VERDICT IS THEREFORE TIME-DEPENDENT, AND DELIBERATELY NOT PINNED
    (the choice a reviewer asked to be stated where its next reader stands).
    The same phrase can stem today and be refused next month, and — the
    direction that actually costs retrieval — a stem admitted when its word was
    rare BECOMES a spammer as siblings arrive carrying it. A snapshot would buy
    reproducibility and lose both halves of the point: it freezes the measure at
    one day's corpus (that IS the curated list again, just machine-written and
    unowned), and it cannot see the second hazard AT ALL, because that drift
    happens in the store long after the write the snapshot was taken for.
    So the drift is accepted and made VISIBLE, on both legs:
      - WRITE: every refusal is reported with the arithmetic that produced it —
        the word's statement-df, the corpus size, the threshold — plus the fact
        that the number moves (guard_entry_keywords' notes, printed by add).
        `built (statement-df 73 of 1433 statements, threshold >=14)` is a
        verdict the author can reproduce and argue with; "refused" is not.
      - AFTER: `helm store doctor` re-measures TODAY's corpus against the stems
        already stored and reports the ones that have since become common
        (the `stem_drift` class), with the `keywords --remove` cure. Measured
        read-only with the shipped instrument against the live store on
        2026-08-11: 217 of 1441 live keyword-typed rows carry such a stem
        (`came`, `back`, `changed`, `worktree`, `gate`) — the drift is not
        hypothetical, and no pin taken at their mint could have named one."""
    return corpus_profile(entries, project=project).common


def _refused_stem_note(cell, prof, generic, census=None, prompt_common=()):
    """A refused stem WITH the arithmetic that refused it: a unigram carries
    its own statement-df, a bigram carries BOTH halves' (a pair falls only when
    EVERY half is common-or-generic, so one number could not explain it), and a
    half that fell to the curated generic set says so — its df is beside the
    point and printing a bare number there would misname the reason. A word
    the PROMPT census refused (task/2978) carries its share of turns instead,
    because that is the number that refused it."""

    def prompt(w):
        return "prompt %.1f%% of %d turns, bar >=%d%%" % (
            100.0 * census.frac(w), census.turns, 100 * _STEM_PROMPT_COMMON)

    def half(w):
        if w in generic:
            return "%s generic" % w
        if w in prompt_common:
            return "%s %s" % (w, prompt(w))
        return "%s df %d" % (w, prof.df.get(w, 0))

    words = cell.split()
    if len(words) == 1:
        if cell in prompt_common:
            return "%s (%s)" % (cell, prompt(cell))
        return "%s (statement-df %d of %d)" % (cell, prof.df.get(cell, 0),
                                               prof.n)
    return "%s (halves: %s)" % (cell, ", ".join(half(w) for w in words))


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


def stem_probes(cells, generic=GENERIC_KEYWORDS, common=frozenset(),
                shaped=True):
    """The 1-2-word content stems of every >= _STEM_MIN_WORDS-word cell —
    the ADDITIONS only, deduped against the originals, author order kept.

    A fused phrase like `seat freezes on plan prompt` is a legal probe that
    matches ~never (verbatim-index law), and every measured near-miss was
    exactly this shape. Its 1-2-word sub-probes are what real turns contain,
    so they are AUTO-ADDED and the original is KEPT (it still matches the
    verbatim repeat, and it documents the author's intent).

    Three per-shape filters, all principled rather than per-case:
      - 1-word stems skip the type's GENERIC set — a generic unigram can
        never be the specific hit the resolver requires and only dilutes the
        df of every entry that carries it.
      - 1-word stems ALSO skip `common` — the corpus-common set the caller
        measured from the store's own statements (corpus_common). This is the
        task/1077 cure: `built`/`works`/`back` are not in any curated generic
        list and never will be, because the population is open-ended and
        store-relative; the store's own language is the measurement. A refused
        unigram costs ~nothing (the phrase and its bigrams remain, and an
        author who WANTS the word can still write it as a cell — the guard
        never edits authored cells) while an admitted one fires on ordinary
        chatter with 1/df ~ 1 and DISPLACES a real rule from the cap-4 window.
      - 2-word stems skip pairs whose BOTH halves are generic-or-common
        ('might be', 'came back'): such a pair is absent from the per-WORD
        sets, so it would count as a SPECIFIC probe and wallpaper — a
        specificity-guard bypass minted by the very guard meant to improve
        retrieval. A pair with one rare half ('seat freezes') is kept: the
        rare half is what makes it a symptom and the common half is what makes
        it the phrase a turn actually contains.
      - with `shaped` (the default, task/2978), a 1-word stem is never
        _weak_shape (numeric, or two characters or fewer), and such a word
        counts as a weak half of a pair. `shaped=False` is the decomposition
        as it stood before that law, kept for ONE reader: generated_stems,
        which must recognise the stems earlier mints appended, `1` and `ok`
        among them, in order to remove them."""
    have = {c.lower() for c in cells}
    out = []

    def take(p):
        if p not in have:
            have.add(p)
            out.append(p)

    def weak(w):
        return w in generic or w in common or (shaped and _weak_shape(w))

    for cell in cells:
        if len(cell.split()) < _STEM_MIN_WORDS:
            continue
        words = [w for w in cell.lower().split() if w not in _STEM_STOPWORDS]
        for w in words:
            if not weak(w):
                take(w)
        for i in range(len(words) - 1):
            if weak(words[i]) and weak(words[i + 1]):
                continue
            take(words[i] + " " + words[i + 1])
    return out


def generated_stems(e):
    """The entry's GENERATED keyword cells, read the way write.py records
    them — as the cells an author never typed.

    There is no field that marks a stem: guard_entry_keywords returns the
    author's EXACT string with its stems APPENDED after ", " (the only place
    stems are ever written), in decomposition order. So the generated cells
    are the TRAILING RUN of the stored cells that the entry's own
    >= _STEM_MIN_WORDS phrases decompose into, and that run must read in the
    decomposition's order; the first cell from the end that is not such a
    stem is the author's and ends the run. A stem-shaped cell BEFORE an
    authored cell is the author's by construction (a retag --add writes after
    the stems, so nothing the guard minted can sit in front of it).

    `shaped=False` recognises stems minted before the shape law. What this
    cannot tell apart, and says so: an author who typed, at the very END of
    the field, exactly the stems the guard would have minted, in its order.
    A second mint appending a later run out of the first run's order reads as
    authored, which is the safe direction: authored cells are never edited."""
    generic = _HEURISTIC_GENERIC if e.get("type") == "heuristic" \
        else GENERIC_KEYWORDS
    cells = _kw_list(_entry_keywords(e))
    longs = [c for c in cells if len(c.split()) >= _STEM_MIN_WORDS]
    if not longs:
        return []
    order = [s.lower() for s in stem_probes(longs, generic, shaped=False)]
    mintable = set(order)
    k = len(cells)
    while k > 0 and cells[k - 1].lower() in mintable:
        k -= 1
    tail = [c.lower() for c in cells[k:]]
    walk = iter(order)
    if not all(any(c == o for o in walk) for c in tail):
        return []
    return cells[k:]


def stem_unfit_cells(e, prompt_common=frozenset()):
    """The entry's GENERATED stems today's mint gate would not mint on the
    task/2978 legs: _weak_shape unigrams, words in `prompt_common` (the prompt
    census's common set), and pairs whose halves are both weak. The statement
    leg is stem_drift's, a report; these are the doctor's to remove.

    The difference between two runs of the decomposition over the entry's own
    long phrases (the stem_drift_cells method): what the legacy decomposition
    mints minus what today's shaped, prompt-gated one keeps, restricted to the
    cells generated_stems says the guard wrote."""
    generated = generated_stems(e)
    if not generated:
        return []
    generic = _HEURISTIC_GENERIC if e.get("type") == "heuristic" \
        else GENERIC_KEYWORDS
    longs = [c for c in _kw_list(_entry_keywords(e))
             if len(c.split()) >= _STEM_MIN_WORDS]
    kept = {s.lower() for s in stem_probes(longs, generic, prompt_common)}
    return [c for c in generated if c.lower() not in kept]


def prompt_profile(census=None):
    """(census, common set) for the mint gate's prompt leg. The common set is
    empty while the census is unmeasured: nothing is refused on a census that
    has not seen enough turns to mean anything."""
    census = promptcensus.load() if census is None else census
    return census, census.common(_STEM_PROMPT_COMMON)


def stem_drift_cells(e, common):
    """The entry's OWN stored keyword cells that today's corpus would refuse —
    the drift instrument behind `helm store doctor`'s stem_drift class.

    Defined as the DIFFERENCE BETWEEN TWO RUNS of the very decomposition the
    mint gate uses (ungated minus gated), restricted to the entry's own >=
    _STEM_MIN_WORDS phrases. Deriving it any other way would be a second
    implementation of `what the gate would do`, free to drift from the gate it
    is supposed to re-check; this one cannot. The long cells are passed alone
    so the dedup set does not swallow the stems that are already stored — those
    stored stems are exactly what this is looking for."""
    generic = _HEURISTIC_GENERIC if e.get("type") == "heuristic" \
        else GENERIC_KEYWORDS
    cells = _kw_list(e.get("keywords"))
    longs = [c for c in cells if len(c.split()) >= _STEM_MIN_WORDS]
    if not longs:
        return []
    mintable = {s.lower() for s in stem_probes(longs, generic)}
    kept = {s.lower() for s in stem_probes(longs, generic, common)}
    return [c for c in cells
            if c.lower() in mintable and c.lower() not in kept]


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


def _dup_sibling(eid, cells, project, entries, census=None):
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
    census = promptcensus.load() if census is None else census
    for cell in cells:
        for top in resolve_prompt(cell, project=project, cap=1, entries=entries,
                                  census=census):
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


# ---------------------------------------------------------------------------
# an id is one token (task/2980)
# ---------------------------------------------------------------------------

# AN ID IS THE NAME EVERY POINTER, GATE, SUPERSEDE LINK AND `helm store get`
# SPELLS, so it is one kebab token. MEASURED on the live store: 8 fired
# entries had ids with spaces and fired 97 times in 48 h; the worst,
# `beacon-also-freezes-seat-config beacon-also-freezes-seat-config`, spent its
# JIT line on the id twice and delivered one word of rule, "EXTENDS". A
# lexicon's id is its TERM, and a term is the phrase a prompt must contain
# ("stripe test mode"), so a lexicon keeps its spaces.
_ID_TYPES = ("prior", "heuristic", "reference")
# a verb the capture grammar leaked into the id slot: `helm premise add <id> |
# ...` files the id "add <id>", because `premise` has no `add` subcommand
_LEAKED_VERBS = ("add",)
_KEBAB = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_ID_SHAPED = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+){2,}")    # three words or more


def spaced_id(etype, eid):
    """True for an id-type entry whose id carries whitespace."""
    return etype in _ID_TYPES and any(ch.isspace() for ch in str(eid or ""))


def canonical_id(eid):
    """The one kebab id a whitespace-carrying id MEANS, or None when no
    mechanical reading is safe. Four shapes, each measured on the live store,
    in this order:

      the id typed twice   `x x`                                   -> x
      a leaked verb        `add operator-absence-is-not-a-gate`    -> the id
      a statement filed    `hardcode-is-eventual-failure CANON (...)` -> its
        after its id       leading kebab token of three words or more
      a phrase of plain    `count the distance before you spend a gate`
        lowercase words                                            -> kebab

    Anything else (capitals, punctuation, a two-word leading token such as
    `as-public repo STORY law ...`) is None: which words name the rule is a
    reader's judgment, and a guessed id is a new name nobody chose."""
    words = str(eid or "").split()
    if not words:
        return None
    if len(set(words)) == 1 and _KEBAB.fullmatch(words[0]):
        return words[0]
    if len(words) == 2 and words[0] in _LEAKED_VERBS \
            and _KEBAB.fullmatch(words[1]):
        return words[1]
    if _ID_SHAPED.fullmatch(words[0]):
        return words[0]
    if all(_KEBAB.fullmatch(w) for w in words):
        return "-".join(words)
    return None


def _spaced_id_refusal(etype, eid):
    """The mint door's refusal for an id with whitespace, naming the id to use."""
    want = canonical_id(eid) or _slug(str(eid))
    return ("'%s' is not an id: an id is ONE kebab token, the name every "
            "pointer, gate and supersede link spells, and whitespace in it "
            "makes `helm store get` and every printed pointer miss. Mint it as "
            "'%s' (or another kebab id). If you ran `helm premise add <id> | "
            "...`, drop the `add`: the premise verb takes the id directly."
            % (eid, want))


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
    linted and stemmed like everyone else's.

    THE ID IS CHECKED FIRST (task/2980): this is the one guard every mint
    door calls (add, premise capture, confirm, xrev-clear, drain), and an id
    with whitespace is refused here before any keyword is looked at
    (spaced_id; a lexicon term keeps its spaces)."""
    if spaced_id(etype, eid):
        return _MintGuardResult(kw, _spaced_id_refusal(etype, eid), [], [])
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
    # ONE census read for the whole mint: the duplicate probe resolves each
    # cell under the lone-word law, and the stem gate reads the prompt leg
    census = promptcensus.load()
    if cells and etype != "lexicon":
        sib, why = _dup_sibling(eid, cells, project, entries, census)
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
    # The corpus-common gate is computed ONLY when some cell can stem — it is
    # a full statement-corpus pass, and most writes carry short cells. The
    # prompt census (task/2978) is read under the same condition: it is the
    # second half of the same question, "is this word common?".
    stems = any(len(c.split()) >= _STEM_MIN_WORDS for c in cells)
    prof = corpus_profile(_jit_candidates(entries), project=project) \
        if stems else _CorpusProfile(0, 0, {}, frozenset())
    census, pcommon = prompt_profile(census) if stems \
        else (census, frozenset())
    room = max(0, _PROBE_CAP - len(cells))
    added = stem_probes(cells, generic, prof.common | pcommon)[:room]
    if added:
        kw = str(kw or "").strip() + ", " + ", ".join(added)
        notes.append("  +%d stem probes auto-added from >=%d-word phrases "
                     "(originals kept): %s"
                     % (len(added), _STEM_MIN_WORDS, ", ".join(added)))
    # THE REFUSAL IS SPOKEN, NOT SILENT (the corpus-drift leg — corpus_common).
    # Re-running the SAME decomposition with the gate OFF is what names the
    # refused stems without re-deriving the word split: the difference between
    # the two runs IS the gate's effect, so this note can never drift from it.
    # The ungated run keeps the shape law: a `1` is refused by its shape, not
    # by any corpus, and is never reported as "too common".
    ungated = stem_probes(cells, generic)[:room] \
        if (prof.n or census.measured) else []
    refused = [c for c in ungated if c not in added]
    if refused:
        notes.append(
            "  %d stem probe%s REFUSED as too common to discriminate: %s — "
            "measured NOW against %d distinct verified statements, threshold "
            ">=%d (max(%d, n//100)), and against the prompt census (%d recent "
            "turns, bar >=%d%% of them). Both measures MOVE: this is a "
            "corpus-relative verdict, nothing is pinned. `helm store doctor` "
            "re-checks stored stems against today's corpora."
            % (len(refused), "" if len(refused) == 1 else "s",
               ", ".join(_refused_stem_note(c, prof, generic, census, pcommon)
                         for c in refused),
               prof.n, prof.cut, _STEM_COMMON_FLOOR, census.turns,
               100 * _STEM_PROMPT_COMMON))
        if prof.folded:
            # the arithmetic above counts CONTRIBUTORS: a repeated statement
            # is one vote however many times it was pasted (_dup_folds)
            notes.append(
                "  (%d near-duplicate statement%s counted once with the row "
                "%s repeat — a repeated statement is one contributor, not a "
                "vote; `helm store doctor` names the copies)"
                % (len(prof.folded), "" if len(prof.folded) == 1 else "s",
                   "it" if len(prof.folded) == 1 else "they"))
        if prof.unsealed:
            notes.append(
                "  (the floor counts DISTINCT VERIFIED statements — a seal "
                "that verifies is the provenance; %d unsealed statement%s "
                "cast no vote)"
                % (prof.unsealed, "" if prof.unsealed == 1 else "s"))
    elif prof.unsealed and not prof.floor_measured:
        # the profile ran (a cell could stem) and NOTHING could be refused:
        # say why, or an unmeasured floor reads as a clean corpus
        notes.append(
            "  (stem floor unmeasured: 0 attested carriers — no stem is "
            "refused as common until a sealed row verifies; %d unsealed "
            "statement%s cast no vote)"
            % (prof.unsealed, "" if prof.unsealed == 1 else "s"))
    if stems and not census.measured:
        # the same honesty for the prompt leg: an empty refusal on a census
        # that has not seen enough turns is "unmeasured", never "rare"
        notes.append(
            "  (prompt census unmeasured: %d of %d turns recorded — no stem "
            "is refused as prompt-common until it has seen %d)"
            % (census.turns, promptcensus.MIN_TURNS, promptcensus.MIN_TURNS))
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
    `/learn` mandates a resolve-sharpen-retest loop and ends at "a capture is not
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
    if ctype:
        # An EXPLICIT --type is the house disambiguator (the _pick_candidate
        # law): the author NAMES the record, so the typed find is the intent
        # and the winner-first law below is for bare ids only. A single-type
        # set cannot fall through to a lower-precedence admitted type — the
        # divergence a probe measured needs two admitted types with an
        # excluded one ranked between them — so selection here stays sound.
        e = _find(eid, project=project, types=(ctype,))
        if not e:
            seen = _find(eid, project=project)
            if seen:
                return None, ("'%s' resolves as type %s — no %s entry "
                              "carries that id" % (eid, seen["type"], ctype))
            return None, "'" + str(eid) + "' not found"
    else:
        e, err = _resolve_write(eid, "keywords", _KEYWORD_TYPES,
                                project=project)
        if err:
            return None, err
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


def _gate_cycle(rule, gate, live):
    """The path gate -> ... -> rule through the gates graph, as ids, or []
    when none. Bounded by the live set (each entry visited once), so a cycle
    already on disk cannot hang the walk."""
    target = gate_key(rule)
    seen, stack = set(), [(gate, [str(gate["id"])])]
    while stack:
        node, path = stack.pop()
        key = gate_key(node)
        if key == target:
            return [str(rule["id"])] + path
        if key in seen:
            continue
        seen.add(key)
        for gid in gate_ids(node):
            nxt = resolve_gate(gid, live)
            if nxt is not None:
                stack.append((nxt, path + [str(nxt["id"])]))
    return []


def regate(eid, ts, add=None, remove=None, replace=None, project=None,
           ctype=None):
    """Link or unlink an entry's GATES — its preconditions — in place.
    -> (e, err). The sibling of retag, one verb shape (task/1346).

    A gate is another entry this rule presupposes: when the rule fires, the
    gate's gloss rides in the same whisper (inject._entries._gate_plan), and
    the two share probe vocabulary (load.link_gates / resolve._probes). The
    specimen: fork-is-PR-staging fired as a conclusion while its two gates —
    upstream-only-what-is-proven-and-only-where-welcome and
    orca-accommodations-live-in-helm-never-fork-or-upstream — sat in entries
    keyed on other words.

    EVERY NAMED GATE MUST RESOLVE, at write time. A gate id that points at
    nothing is a rule that believes it arrives with its gate and arrives
    alone — the silent shape this mechanism exists to end — so it is refused
    here rather than rendered as MISSING forever. Self-gating is refused for
    the same reason (it says nothing). Idempotent: an unchanged list writes
    nothing and emits no event."""
    bad = _refuse_bad_ts(ts)
    if bad:
        return None, bad
    if replace is not None and (add or remove):
        return None, "--set replaces the whole list; do not combine it with " \
                     "--add/--remove"
    # THE TYPED OPERAND — `gates lexicon:seam --remove prior:x` is what the
    # injector's markers print, so the verb reads it (find_typed).
    tid_t, tid_slug = split_gate(eid)
    if tid_t and not ctype:
        ctype, eid = tid_t, tid_slug
    if ctype:
        ctype = "prior" if ctype == "premise" else ctype
        if ctype not in _KEYWORD_TYPES:
            return None, "unknown --type '%s' (one of: %s)" \
                % (ctype, ", ".join(_KEYWORD_TYPES))
        e = _find(eid, project=project, types=(ctype,))
        if not e:
            return None, "'" + str(eid) + "' not found"
    else:
        e, err = _resolve_write(eid, "gates", _KEYWORD_TYPES, project=project)
        if err:
            return None, err
        if not e:
            return None, "'" + str(eid) + "' not found"
    before = _kw_list(e.get("gates"))
    # RESOLVED AGAINST THE INJECTABLE SET THROUGH THE ONE RESOLVER the
    # injector uses (load.resolve_gate), so what validates here is exactly
    # what rides there. A bare slug two types share is AMBIGUOUS and refused
    # — `type:id` names one (a bare slug validated the prior and
    # injected the lexicon).
    live = [x for x in load_all(project=project)
            if x.get("status") in INJECTABLE_STATUSES]
    if replace is not None:
        after = _kw_list(replace)
    else:
        # A REMOVAL MATCHES BY RESOLVED IDENTITY, the --add rule in reverse
        # (dispatch 0fb12823851d: a typed operand matched a stored
        # bare same-slug gate and could delete the other type). A typed
        # operand matches only a stored gate whose resolved (type, slug)
        # equals it; a stored BARE spelling resolves to its one injectable
        # type, and when its slug has two the remove refuses AMBIGUOUS,
        # naming both typed spellings. An operand or a stored gate that
        # resolves to nothing matches on its exact spelling (a link to a
        # retired entry must still be removable).
        def identity(spelling):
            hits = gate_candidates(spelling, live)
            kinds = sorted({x["type"] for x in hits})
            if split_gate(spelling)[0] is None and len(kinds) > 1:
                return None, kinds
            return (gate_key(hits[0]) if hits else split_gate(spelling)), []

        def ambiguous(spelling, kinds):
            return ("gate '%s' is AMBIGUOUS — %s all carry that id; name one "
                    "as type:id (%s)" % (spelling, ", ".join(kinds),
                                         " or ".join("%s:%s" % (k, split_gate(spelling)[1])
                                                     for k in kinds)))
        drop = []
        for g in _kw_list(remove):
            key, kinds = identity(g)
            if key is None:
                return None, ambiguous(g, kinds)
            drop.append(key)
        after = []
        for stored in before:
            key, kinds = identity(stored)
            if key is None and split_gate(stored)[1] in {d[1] for d in drop}:
                return None, "stored " + ambiguous(stored, kinds)
            if key not in drop:
                after.append(stored)
        have = {g.lower() for g in after}
        for g in _kw_list(add):
            if g.lower() not in have:
                have.add(g.lower())
                after.append(g)
    for g in after:
        want_t, slug = split_gate(g)
        if slug == _slug(str(e["id"])) and want_t in (None, e["type"]):
            return None, "'%s' cannot gate itself" % e["id"]
        hits = gate_candidates(g, live)
        if not hits:
            return None, ("gate '%s' resolves to no injectable store entry — a "
                          "rule whose gate points at nothing arrives alone, "
                          "which is the exact failure a gate exists to end; add "
                          "the gate entry first (a retired or candidate entry "
                          "could never ride)" % g)
        if want_t is None and len({x["type"] for x in hits}) > 1:
            return None, ("gate '%s' is AMBIGUOUS — %s all carry that id; name "
                          "one as type:id (%s)"
                          % (g, ", ".join(sorted({x["type"] for x in hits})),
                             " or ".join("%s:%s" % (x["type"], slug)
                                         for x in hits)))
        # A CYCLE IS REFUSED HERE, because the plan a cycle produces is two
        # rules each deferring to the other and neither rendering — measured
        # as _gate_plan returning [] for a<->b. Walk the gates
        # graph from the new gate; reaching this entry is a cycle (the self
        # gate above is its 1-cycle). The injector still fails OPEN on a
        # cycle already on disk (inject._entries._gate_plan).
        cyc = _gate_cycle(e, hits[0], live)
        if cyc:
            return None, ("gate '%s' would close a CYCLE: %s — a rule and its "
                          "gate cannot presuppose each other; remove one side "
                          "(helm store gates <id> --remove <gate>)"
                          % (g, " -> ".join(cyc)))
    if after == before:
        return e, None                          # idempotent: no write, no event
    e.update({"gates": ",".join(after), "last_updated": ts})
    _WRITERS[e["type"]](e, path=e["path"])
    pk.event("store.regate", str(e["id"]),
             "%d -> %d gates" % (len(before), len(after)))
    return e, None


def regloss(eid, ts, text=None, clear=False, project=None, ctype=None):
    """Set or clear an entry's GLOSS — the short line that FIRES — in place.
    -> (e, err). The sibling of retag/regate, one verb shape.

    THE BUILT-BUT-UNWIRED LEVER. The gloss had a writer (_gloss_lines), an
    oracle (_commit's LINE_CAP refusal), an injector reader (a gloss fires
    whole) and docs — and no verb set it, so the owner's only way to make a
    canon entry fit the lane was to rewrite the canon shorter. Measured on
    the live store 2026-08-22: the specimen's three entries all fire at 400
    truncated bytes, and the two-gates premise dropped off the JIT ceiling
    for lack of one.

    WRITES THROUGH THE SAME _commit PATH AS EVERY WRITER, so an oversized
    gloss is REFUSED with the exact overage and the file is untouched —
    never a second validator that could drift from the one the writers
    use. The DERIVED law is untouched: confirm --edit and a re-mint over a
    retired id still scrub a gloss the statement no longer matches; this
    verb only writes what the author hands it, against the statement as it
    stands. Idempotent: an unchanged gloss writes nothing, emits no event."""
    bad = _refuse_bad_ts(ts)
    if bad:
        return None, bad
    if clear and str(text or "").strip():
        return None, "--clear empties the gloss; do not combine it with --set"
    tid_t, tid_slug = split_gate(eid)
    if tid_t and not ctype:
        ctype, eid = tid_t, tid_slug
    if ctype:
        ctype = "prior" if ctype == "premise" else ctype
        if ctype not in _KEYWORD_TYPES:
            return None, "unknown --type '%s' (one of: %s)" \
                % (ctype, ", ".join(_KEYWORD_TYPES))
        e = _find(eid, project=project, types=(ctype,))
        if not e:
            return None, "'" + str(eid) + "' not found"
    else:
        e, err = _resolve_write(eid, "gloss", _KEYWORD_TYPES, project=project)
        if err:
            return None, err
        if not e:
            return None, "'" + str(eid) + "' not found"
    before = re.sub(r"\s+", " ", str(e.get("gloss") or "")).strip()
    after = "" if clear else re.sub(r"\s+", " ", str(text or "")).strip()
    if not clear and not after:
        return None, "--set needs the gloss text; --clear empties it"
    if after == before:
        return e, None                          # idempotent: no write, no event
    e.update({"gloss": after, "last_updated": ts})
    try:
        _WRITERS[e["type"]](e, path=e["path"])
    except ValueError as exc:                   # the oracle's refusal, verbatim
        return None, str(exc)
    pk.event("store.regloss", str(e["id"]),
             "gloss cleared" if clear else "gloss %d -> %d bytes"
             % (len(before.encode("utf-8")), len(after.encode("utf-8"))))
    return e, None


def rescope(eid, ts, owner, project=None, ctype=None):
    """RECORD THE PROJECT AN ENTRY IS ABOUT — a registry project name, or
    store.FLEET for owner policy that applies everywhere, or "" to clear the
    record. -> (e, err). The sibling of retag/regate/regloss, one verb shape.

    This is the migration door for task/2435, and it exists because the
    alternative was hand-editing files under ~/.helm: an entry's project is
    canon about canon, so it goes through the SAME _commit path as every other
    writer, gets an event receipt, and cannot be spelled two ways.

    A CLEARED record RETURNS THE ENTRY TO ITS ROOT'S DEFAULT, which is a
    DERIVATION and not a constant: a row under a project root derives that
    project, the adopted root derives FLEET, and a helm-global row derives FLEET
    unless its statement names a helm artifact. Clearing is therefore not a way
    to hide a row, and the CLI reports what the derivation actually yields
    rather than promising one of the three. Only a recorded name confines an
    entry to one project, which is what makes this the door."""
    bad = _refuse_bad_ts(ts)
    if bad:
        return None, bad
    # THE OPERAND IS VALIDATED, NEVER NORMALIZED, and it is validated through
    # the registry's OWN contract rather than a second spelling of it. A name is
    # RECORDED here and compared EXACTLY by every reader (load.entry_scope,
    # inject's ledger), so rewriting it is worse than refusing it: a rewritten
    # name silently belongs to no project while this door reports success. The
    # registry admits internal spaces, so stripping whitespace turned the valid
    # identity "client site" into "clientsite" and scoped the entry to nothing.
    # The refusal lands HERE, before any resolve or write, so an invalid name
    # touches no bytes and mints no receipt.
    from .. import registry
    want = str(owner or "").strip()
    if want and registry.name_is_malformed(want):
        return None, ("a project is a registry name or '%s', never '%s'"
                      % (FLEET, owner))
    # AND THEN THE SECOND CONTRACT, because registry validity is not proof of
    # representability. The registry's name contract forbids only an empty name,
    # a dot name and a path separator, so it ADMITS a name carrying a newline or
    # a quotation mark; this store records a project as ONE FIELD VALUE ON ONE
    # LINE of flat frontmatter. A line-breaking name ended the field early and
    # handed the remainder to the reader as a FORGED FIELD, and a quoted name
    # came back stripped — and the door reported success on both, recording a
    # name no reader could match. The frontmatter grammar is UNCHANGED and there
    # is no new encoding to roll out to readers: a name it cannot carry is
    # refused, and the refusal names BOTH contracts so the operator can see that
    # the name is a legal project and still not a recordable one.
    if want and not pk.field_value_is_representable(want):
        return None, ("the registry's name contract admits %s, but the store's "
                      "flat frontmatter cannot carry it: a recorded project is "
                      "one field value on one line, read back with surrounding "
                      "quotes stripped, and this name does not survive that. "
                      "Nothing was recorded." % repr(want))
    tid_t, tid_slug = split_gate(eid)
    if tid_t and not ctype:
        ctype, eid = tid_t, tid_slug
    if ctype:
        ctype = "prior" if ctype == "premise" else ctype
        if ctype not in _KEYWORD_TYPES:
            return None, "unknown --type '%s' (one of: %s)" \
                % (ctype, ", ".join(_KEYWORD_TYPES))
        e = _find(eid, project=project, types=(ctype,))
        if not e:
            return None, "'" + str(eid) + "' not found"
    else:
        e, err = _resolve_write(eid, "rescope", _KEYWORD_TYPES, project=project)
        if err:
            return None, err
        if not e:
            return None, "'" + str(eid) + "' not found"
    before = str(e.get("project") or "").strip()
    if want == before:
        return e, None                          # idempotent: no write, no event
    e.update({"project": want, "last_updated": ts})
    try:
        _WRITERS[e["type"]](e, path=e["path"])
    except ValueError as exc:                   # the oracle's refusal, verbatim
        return None, str(exc)
    pk.event("store.rescope", str(e["id"]),
             "project %s -> %s" % (before or "(none)", want or "(none)"))
    return e, None


# ---------------------------------------------------------------------------
# doctor — the retrieval-field repair lane (task/1077 defects 2/3/4/5)
# ---------------------------------------------------------------------------

# A keyword cell that is PROSE, not a probe: sentence punctuation, shell
# metacharacters, or a ` + ` conjunction — the shapes measured in the live
# cascade rows ("jj-agnostic + auditable-with-auto-cleanup.",
# "grep -q <session-prefix> && echo HOLDER; done.").
_DOCTOR_PROSE = re.compile(r'[.;&|{}<>"()]| \+ ')
# domain fields carrying this many commas are keyword CSVs, never taxonomy —
# measured 2026-08-11: exactly 3 rows across 1700 entries, all three of them
# delimiter-cascade victims, zero legitimate domains.
_DOCTOR_DOMAIN_CSV = 3
# AUTHORED probes past which the doctor warns (heuristic
# store-keywords-few-short-rare; trigger design R3): keys are
# candidate generators, 3-6 short symptom probes. An entry keyed wider is two
# entries or a route, and every common key it adds buys its recall with every
# other agent's precision (a lone single-word match: 61% of fires, 7.8%
# relevant, MEASURED E2).
_AUTHORED_PROBES_MAX = 6


def authored_probes(e):
    """The keyword cells an AUTHOR wrote: the stored cells less the guard's
    appended stems (generated_stems) and less the route cells, which are
    declarations, not probes."""
    gen = {c.lower() for c in generated_stems(e)}
    return [c for c in _kw_list(_entry_keywords(e))
            if c.lower() not in gen and route_cell(c) is None]


def _doctor_classify(entries, project=None):
    """The four defect classes, measured fresh from a load_all() list ->
    {statement_ids, cascade, space_csv, zero_keys, flagged}.

    THE CLASSES (all measured live 2026-08-11, task/1077):
    - space_ids (task/2980): a prior, heuristic or reference whose id carries
      whitespace — `add <id>`, the id typed twice, a statement filed after
      its id. An id is one kebab token (spaced_id). FIXABLE where it is safe:
      `doctor` marks each row with `rekey` (canonical_id's kebab id) or
      `rekey_held` (why not), and --fix re-keys the row to a new file under
      that id and leaves a tombstone at the old one that points to it, with
      every supersede link and gate that named it re-pointed (_doctor_rekeys).
      Held: an attested row (its seal binds the id), a pinned one, an id with
      no mechanical reading, a target already taken.
    - statement_ids: a LEXICON whose term carries whitespace. REPORT ONLY — a
      term is the phrase a prompt must contain, so it keeps its spaces; the
      _find prefix fallback is what makes a statement-shaped one addressable.
    - legacy_routes (trigger design lane 1): route cells still spelled
      `notice:<kind>`, which resolve reads as `route:arrival.<kind>`. REPORT
      ONLY, with the one-command re-key; the rows are COPIES carrying the
      cells, so no transient key can reach a writer.
    - wide_keys: more than _AUTHORED_PROBES_MAX authored probes
      (authored_probes). REPORT ONLY — which keys to keep is the author's
      call; the warning names the count.
    - cascade: the delimiter cascade the add grammar warns about (statement
      tail -> keywords slot, keywords CSV -> domain slot, domain off the end;
      an unescaped pipe in the statement). 3 rows. FIXABLE: the CSV in domain
      IS the author's keywords — move it home, drop the prose fragments from
      keywords (receipted in the entry's own evidence_log WITH the exact
      before-bytes of both fields, never silently), clear domain. Every
      authored cell is kept — _PROBE_CAP bounds generated stems only (F2). The statement is NOT reconstructed: it is attested on two
      of the three rows and the payload hash binds its bytes.
    - space_csv: a comma-less keyword field of >= _SALAD_MIN_WORDS words whose
      tokens are all probe-shaped — the legacy space-separated CSV that parses
      as ONE giant probe no turn will ever repeat verbatim (69 live rows,
      silently dead). FIXABLE: split on whitespace, drop per-type generic
      tokens, comma-join — the author typed a list, the commas restore it.
    - zero_keys: live prior/heuristic/reference with NO keywords (42 rows —
      resolve indexes only id + keywords, so these never fire). REPORT ONLY:
      keying is symptom-vocabulary judgment, not mechanics; the report carries
      the exact retag command. The MINT fail-open behind them is already
      closed for every CLI door (guard_entry_keywords refuses an empty field
      at add/confirm/xrev-clear/drain); these rows predate the guard.
    - flagged: comma-less multi-word keywords carrying prose shapes — a human
      call, never auto-split (splitting a sentence mints exactly the spam
      unigrams the stem gate refuses).
    - stem_drift: THE CORPUS-DRIFT RE-CHECK (corpus_common's second leg). The
      stop-list is measured from the store's own statements, so it MOVES: a
      stem admitted when its word was rare becomes a spammer once siblings
      arrive carrying it, and no snapshot taken at that entry's mint could ever
      name it. This class re-runs TODAY's decomposition over each entry's own
      phrases and reports the stored cells the gate would now refuse. Measured
      read-only against the live store 2026-08-11: 217 rows. REPORT ONLY —
      removing a key is retrieval judgment (the word may be the one the author
      wants), and the cure printed is `keywords <id> --type <type> --remove
      <cell>` — typed, because a same-id row of another type would otherwise
      win the bare resolve and lose ITS authored cell (F1). The
      transient `stem_drift` key set on a reported entry never reaches disk:
      these rows are never written (the writers take explicit field lists, and
      the two FIXABLE classes `continue` before this check).
    - dup_statements: rows whose statement is a NEAR-DUPLICATE of an earlier
      row (token-set Jaccard >= DUP_OVERLAP — the same overlap `add` warns
      at). The corpus measure behind the stem gate counts such a cluster as
      ONE contributor (_dup_folds), so pasting one sentence four times can
      no longer vote a rare stem "common"; this class names the copies with
      the `supersede` cure the add warning already prescribes. REPORT ONLY.
      The transient `dup_of` key is the kept sibling's (type, id).
    - stem_unfit: GENERATED stems today's mint gate would not mint on its
      task/2978 legs (stem_unfit_cells): a numeric or <= 2-character unigram,
      a word the prompt census measures as common, a pair of two weak
      halves. FIXABLE: --fix removes exactly those cells, receipted with the
      field's before-bytes. It edits only what generated_stems reads as the
      guard's own appended tail, so no authored cell is ever touched. The
      transient `stem_unfit` key never reaches disk (the repair writes a
      copy). A cell that is also statement-drifted is listed here only, with
      the cure that applies. With the census unmeasured only the shape leg
      runs, and `census_unmeasured` says so.
    - attest_unverified: a FIXABLE row (cascade / space_csv / stem_unfit) that carries an
      attestation the doctor could NOT verify — filled by `doctor` (it needs
      the project lens), listed here so the two classes share one table.
      Such a row is left BYTE-IDENTICAL by --fix and stays listed in its
      defect class; see _attest_verdict. The transient `attest_why` key
      carries premise-check's reason."""
    prof = corpus_profile(_jit_candidates(entries), project=project)
    common = prof.common
    census, pcommon = prompt_profile()
    out = {"space_ids": [], "statement_ids": [], "cascade": [],
           "space_csv": [], "zero_keys": [], "flagged": [], "stem_drift": [],
           "stem_unfit": [], "wide_keys": [], "legacy_routes": [],
           "dup_statements": [], "attest_unverified": [],
           # not a defect class: the one record that says the stem floor could
           # not be measured (no verified carrier), so an empty stem_drift
           # reads as "nothing measured", never as "nothing drifted". Shaped
           # like every other report row (id + type) so a reader that walks
           # the report's classes as entries keeps working.
           "floor_unmeasured": [] if prof.floor_measured else
           [{"id": "stem-floor", "type": "corpus",
             "attested": prof.attested, "unsealed": prof.unsealed}],
           # the prompt leg's twin of floor_unmeasured: stem_unfit ran on
           # the shape leg alone because the census has too few turns
           "census_unmeasured": [] if census.measured else
           [{"id": "prompt-census", "type": "corpus",
             "turns": census.turns, "min_turns": promptcensus.MIN_TURNS}]}
    for dup, kept in prof.dups:
        dup["dup_of"] = (kept["type"], str(kept["id"]))
        out["dup_statements"].append(dup)
    for e in entries:
        if e.get("type") not in _KEYWORD_TYPES:
            continue
        legacy = legacy_route_cells(e)
        if legacy:
            out["legacy_routes"].append(dict(e, legacy_routes=legacy))
        if spaced_id(e["type"], e["id"]):
            out["space_ids"].append(e)
        elif any(ch.isspace() for ch in str(e["id"])):
            out["statement_ids"].append(e)
        kw = str(e.get("keywords") or "").strip()
        dom = str(e.get("domain") or "").strip()
        cells = _kw_list(kw)
        first = cells[0] if cells else ""
        if dom.count(",") >= _DOCTOR_DOMAIN_CSV \
                and (not cells or _DOCTOR_PROSE.search(first)
                     or len(first.split()) > _SALAD_MIN_WORDS):
            out["cascade"].append(e)
            continue
        if kw and "," not in kw and len(kw.split()) >= _SALAD_MIN_WORDS:
            if _DOCTOR_PROSE.search(kw):
                out["flagged"].append(e)
            else:
                out["space_csv"].append(e)
            continue
        if not kw and e["type"] != "lexicon":
            out["zero_keys"].append(e)
            continue
        wide = len(authored_probes(e))
        if wide > _AUTHORED_PROBES_MAX:
            e["wide_keys"] = wide           # transient, never written
            out["wide_keys"].append(e)
        unfit = stem_unfit_cells(e, pcommon)
        if unfit:
            e["stem_unfit"] = unfit
            out["stem_unfit"].append(e)
        low = {c.lower() for c in unfit}
        drifted = [c for c in stem_drift_cells(e, common)
                   if c.lower() not in low]
        if drifted:
            e["stem_drift"] = drifted
            out["stem_drift"].append(e)
    return out


def _doctor_receipt(e, ts, reason):
    """A durable in-artifact receipt for a doctor rewrite. The events journal
    is a lossy rotating trail by charter; a repair that DROPS author text must
    keep the dropped bytes somewhere a reader can find them forever.

    `was` carries the EXACT before-bytes of both retrieval fields the repair
    rewrites (the demote receipt's own key for a prior state). A reason that
    only COUNTS what moved is not a record anyone can restore from — task/1077
    revival (F2) measured a 25-token authored field whose 25th token was gone
    with nothing but "-> 24 cells" left behind. Taken
    BEFORE the caller mutates the fields, by construction of the call order
    in `doctor`; a receipt appended after the rewrite would record the
    repaired bytes as the originals."""
    e["evidence_log"] = list(e.get("evidence_log") or []) + [
        {"ts": ts, "type": "doctor", "delta": 0, "reason": reason,
         "by": "store-doctor",
         "was": {"keywords": str(e.get("keywords") or ""),
                 "domain": str(e.get("domain") or "")}}]


def _attest_verdict(e, project=None, recs=None):
    """-> (state, why) for the attestation an entry's ARTIFACT carries:
      unattested  no attest_* key at all — the row claims no seal
      verified    exactly what `helm premise-check` exits 0 on: the stored
                  statement still hashes to the attested payload AND the
                  native record recomputes AND binds to this premise
      unverified  everything else, with premise-check's own reason (digest
                  MISMATCH, a record the chain never minted or that commits
                  another claim, a payload with no native record, a legacy
                  turn pointer with no native proof)

    Read from the FILE, not the loaded dict, and checked through the SAME
    verifier premise-check uses (_record_expect + verify_record) — the
    doctor decides whether to rewrite a sealed row, so it must hold the
    seal's own instrument, not a model of it. THE DEFECT THIS CLOSES (row
    e43ec4309e89): load parses attest_* without authenticating
    and write_prior carries them through, so --fix rewrote a row whose seal
    was already broken — a fresh last_updated and a doctor receipt over a
    digest that no longer matched, which reads downstream as a maintained,
    attested row. A rewrite the doctor did not verify is a laundering.

    UNREADABLE IS NOT ABSENCE. The reread is STRICT: an artifact this pass
    cannot read (or decode) answers unverified with the OS reason, never
    "unattested" — a lenient reader's None collapses to {} here, which
    makes a broken-seal prior the loader has already parsed read as sealed
    by nothing exactly when the verifier's own read fails, and --fix then
    rewrites it. THE VERIFIER IS CONTAINED: a malformed native-record line (the chain
    parser admits any JSON value, and a `null` before the sought record
    makes verify_record raise) answers unverified naming the exception, so a
    read-only doctor still returns its inventory instead of escaping.
    `recs` is the chain the caller already read (corpus_profile verifies a
    whole store on one read)."""
    from ..premise._common import digest_payload, payload_digest
    from ..premise._verify import _record_expect, verify_record
    if not e.get("path"):
        return "unverified", "no artifact path to read the seal from"
    try:
        meta = _attest_meta(e["path"])
    except Exception as exc:  # noqa: BLE001 — any read/decode failure is named
        return "unverified", ("artifact unreadable for verification — %s: %s"
                              % (type(exc).__name__, exc))
    claims = {k: str(meta.get(k) or "").strip() for k in _ATTEST_KEYS}
    if not any(claims.values()):
        return "unattested", ""
    payload, rec = claims["attest_payload"], claims["attest_record"]
    if not payload and not rec:
        return "unverified", ("no native record — a legacy turn pointer alone "
                              "is not verification")
    if payload_digest(payload) != payload_digest(
            digest_payload(e.get("statement") or "")):
        return "unverified", ("digest MISMATCH — the stored statement no "
                              "longer hashes to the attested payload")
    if not rec:
        return "unverified", ("no native record hash recorded — a digest "
                              "match alone is not verification")
    try:
        ok, detail = verify_record(rec, _record_expect(e, project), recs=recs)
    except Exception as exc:  # noqa: BLE001 — a malformed chain is a finding, not an escape
        return "unverified", ("native chain UNREADABLE — the verifier raised "
                              "%s: %s" % (type(exc).__name__, exc))
    return ("verified", detail) if ok else ("unverified",
                                           "native chain BROKEN — " + detail)


def _attest_meta(path):
    """The attest_* frontmatter of ONE artifact, read STRICTLY (a failure
    raises; see _attest_verdict). Its own seam so a test can fail exactly
    this reread while the writers' reads keep working."""
    return pk.parse_simple_frontmatter(path, dict.fromkeys(_ATTEST_KEYS, ""),
                                       strict=True) or {}


def _doctor_repairs(report, ts):
    """The repairs --fix would make -> [(entry, repaired COPY, class, reason)].
    Pure: the copy is what the writer serializes; the entry on the report is
    untouched until the whole batch has landed (see doctor)."""
    plan = []
    for e in report["cascade"]:
        cells = _kw_list(e.get("keywords"))
        prose = [c for c in cells if _DOCTOR_PROSE.search(c)
                 or len(c.split()) > _SALAD_MIN_WORDS]
        keep = [c for c in cells if c not in prose]
        moved = _kw_list(e.get("domain"))
        merged = moved + [c for c in keep
                          if c.lower() not in {m.lower() for m in moved}]
        if not merged:
            continue                       # nothing to move home — leave it
        reason = ("field-shift repair: keywords <- domain CSV (%d cells); "
                  "dropped prose fragment(s) from keywords: %s"
                  % (len(moved), " / ".join(prose) if prose else "(none)"))
        row = dict(e)
        _doctor_receipt(row, ts, reason)
        # EVERY AUTHORED CELL IS KEPT. _PROBE_CAP is the mint gate's budget
        # for GENERATED stems (guard_entry_keywords caps only what it appends,
        # `room`); it was never an authored limit, and the field has no byte
        # budget (_commit measures the gloss line only). The first cut sliced
        # `merged[:_PROBE_CAP]` here, so a cascade row with 24 domain cells
        # plus one surviving keyword lost the survivor with no record
        # (task/1077 revival, F2) — a repair that drops author bytes the
        # receipt does not carry is a second defect wearing a cure's name.
        _set_entry_keywords(row, ",".join(merged))
        row.update({"domain": "", "last_updated": ts})
        plan.append((e, row, "cascade", reason))
    for e in report["space_csv"]:
        generic = _HEURISTIC_GENERIC if e["type"] == "heuristic" \
            else GENERIC_KEYWORDS
        toks, seen = [], set()
        for t in str(e.get("keywords")).split():
            if t.lower() not in generic and t.lower() not in seen:
                seen.add(t.lower())
                toks.append(t)
        if not toks:
            continue                       # all-generic: retag-empty law, skip
        reason = ("space-separated keyword CSV comma-ized: 1 fused probe -> "
                  "%d cells" % len(toks))
        row = dict(e)
        _doctor_receipt(row, ts, reason)
        # all of them — the cap law is stated at the cascade repair above; the
        # reason's count and the stored count are now the same number
        _set_entry_keywords(row, ",".join(toks))
        row["last_updated"] = ts
        plan.append((e, row, "space_csv", reason))
    for e in report["stem_unfit"]:
        drop = {c.lower() for c in e["stem_unfit"]}
        keep = [c for c in _kw_list(_entry_keywords(e))
                if c.lower() not in drop]
        reason = ("generated stems the mint gate would not mint removed "
                  "(numeric, <= 2 characters, or prompt-common): %s"
                  % ", ".join(e["stem_unfit"]))
        row = dict(e)
        row.pop("stem_unfit", None)
        _doctor_receipt(row, ts, reason)
        # THE AUTHOR'S CELLS ARE KEPT IN THEIR ORDER, and joined the way the
        # guard joins what it appends (", "): generated_stems removed only
        # the cells it read as the guard's own tail
        _set_entry_keywords(row, ", ".join(keep))
        row["last_updated"] = ts
        plan.append((e, row, "stem_unfit", reason))
    return plan


def _names(r, e):
    """The fields of row `r` that name entry `e`: its supersede links (bare
    ids, compared as slugs) and its gates (bare or typed spellings)."""
    key = _slug(str(e["id"]))
    hit = [f for f in ("supersedes", "replaced_by")
           if r.get(f) and _slug(str(r[f])) == key]
    for g in gate_ids(r):
        t, slug = split_gate(g)
        if slug == key and t in (None, e["type"]):
            hit.append("gates")
            break
    return hit


def _repoint(row, moves):
    """Re-point `row`'s supersede links and gates through `moves`
    ({old slug: (type, new id)}) in place -> the fields it changed."""
    changed = []
    for f in ("supersedes", "replaced_by"):
        v = str(row.get(f) or "")
        if v and _slug(v) in moves:
            row[f] = moves[_slug(v)][1]
            changed.append(f)
    cells = []
    for g in gate_ids(row):
        t, slug = split_gate(g)
        if slug in moves and t in (None, moves[slug][0]):
            g = ("%s:%s" % (t, moves[slug][1])) if t else moves[slug][1]
            if "gates" not in changed:
                changed.append("gates")
        cells.append(g)
    if "gates" in changed:
        row["gates"] = ",".join(cells)
    return changed


def _rekey_verdicts(report, rows, project=None):
    """Mark every report["space_ids"] row with `rekey` (the kebab id --fix
    moves it to) or `rekey_held` (why it stays). Transient keys, never
    written. `rows` is the WHOLE store, every status and type, because a
    target must be free of every row and a link to the row can sit in a
    tombstone.

    A RE-KEY IS HELD, and says why, when it cannot be made safely:
      attested       the seal's native record binds the premise id, so a new
                     id reads BROKEN to premise-check; `helm premise
                     --supersede` is the door that re-seals under a new id
      no reading     canonical_id found none (capitals, punctuation)
      pinned         a founding pin is keyed on the slug and would drop
      taken          another row already answers to the target id
      unfiled        the file name does not follow the id, so there is no
                     canonical path to derive

    A target whose slug IS the row's own key (`count the distance before
    you spend a gate`) is not taken by the row itself: its key and file stay,
    and only the id field is respelled (_doctor_rekeys).
      a link's seal  a row that names it carries a seal the doctor cannot
                     verify, and re-pointing that link is a rewrite of it
      shared target  two rows mean the same id: which one keeps it is a
                     reader's call"""
    taken = {}
    for r in rows:
        taken.setdefault(_slug(str(r["id"])), []).append(r)
    want = {}
    for e in report["space_ids"]:
        target, why = canonical_id(e["id"]), None
        others = [r for r in taken.get(_slug(target or ""), ())
                  if r.get("path") != e.get("path")]
        if _attest_verdict(e, project)[0] != "unattested":
            why = ("attested: its seal binds the id, so a re-key would read "
                   "BROKEN to premise-check; move it with `helm premise "
                   "--supersede %s %s | <statement>`"
                   % (typed_id(e), target or "<new-id>"))
        elif target is None:
            why = ("no mechanical kebab id (capitals, punctuation or a "
                   "two-word lead): add the rule under a kebab id, then "
                   "`helm store supersede` this row to it")
        elif _slug(str(e["id"])) in PINNED_SLUGS:
            why = "a founding pin is keyed on this slug"
        elif others:
            why = ("'%s' is taken by %s [%s]; merge them by hand with `helm "
                   "store supersede`" % (target, typed_id(others[0]),
                                         others[0].get("status")))
        else:
            base = os.path.basename(str(e.get("path") or ""))
            tail = _slug(str(e["id"])) + ".md"
            if not base.endswith(tail):
                why = "its file name does not follow its id (%s)" % base
            else:
                for r in rows:
                    if _names(r, e) and _attest_verdict(r, project)[0] \
                            == "unverified":
                        why = ("%s names it and carries a seal the doctor "
                               "cannot verify" % typed_id(r))
                        break
        if why:
            e["rekey_held"] = why
        else:
            e["rekey"] = target
            want.setdefault(_slug(target), []).append(e)
    for same in want.values():
        if len(same) > 1:
            for e in same:
                e.pop("rekey")
                e["rekey_held"] = ("%d rows mean the id '%s'; which keeps it "
                                   "is a reader's call" % (len(same),
                                                           canonical_id(e["id"])))


def _rekey_receipt(row, ts, reason, was):
    """The re-key's in-artifact receipt: the doctor receipt's shape, with the
    exact before-values of the identity fields it moved."""
    row["evidence_log"] = list(row.get("evidence_log") or []) + [
        {"ts": ts, "type": "doctor", "delta": 0, "reason": reason,
         "by": "store-doctor", "was": was}]


def _doctor_rekeys(report, plan, rows, ts):
    """Fold the re-keys into the --fix plan -> the plan, ONE item per path.

    Per re-keyed row, two writes: the entry under its kebab id at the
    canonical path beside the old file (its other repairs this run folded
    in), and a TOMBSTONE at the old path — the supersede law's shape,
    `delete_eligible` with `replaced_by` the new id, so `helm store get
    "<old id>"` still answers and names where the rule went. The new row
    `supersedes` the old id unless it already supersedes something, whose
    `replaced_by` is re-pointed instead. Every other row whose supersede link
    or gate named a re-keyed row is re-pointed at the new id, in the SAME
    batch, so the rename and its references land or refuse together.

    When the kebab id slugs to the row's OWN key (a phrase of plain words),
    the key, the file and every link already resolve: the row is rewritten in
    place with the id respelled, and there is no tombstone to leave.

    One item per path, because a stage is keyed by basename: a second item
    for the same file would overwrite the first stage and its rename would
    find nothing (the batch would land half of itself)."""
    moving = [e for e in report["space_ids"] if e.get("rekey")]
    if not moving:
        return plan
    moves = {_slug(str(e["id"])): (e["type"], e["rekey"]) for e in moving}
    items = {}
    for e, row, klass, reason in plan:
        items[row["path"]] = [e, row, klass, reason]
    tail = []
    for e in moving:
        old = items.pop(e["path"], None)
        new = dict(old[1] if old else e)
        for k in ("rekey", "rekey_held", "wide_keys", "stem_unfit"):
            new.pop(k, None)
        oid, nid = str(e["id"]), e["rekey"]
        base = os.path.basename(e["path"])
        prefix = base[:len(base) - len(_slug(oid) + ".md")]
        new.update({"id": nid, "last_updated": ts,
                    "path": os.path.join(os.path.dirname(e["path"]),
                                         prefix + _slug(nid) + ".md")})
        _repoint(new, moves)
        in_place = new["path"] == e["path"]
        if not (in_place or new.get("supersedes")):
            new["supersedes"] = oid
        reason = "re-keyed from '%s': an id is one kebab token" % oid
        _rekey_receipt(new, ts, reason, {"id": oid, "path": e["path"]})
        if in_place:
            tail.append((e, new, "space_ids",
                         reason + (("; " + old[3]) if old else "")))
            continue
        tomb = dict(e)
        for k in ("rekey", "rekey_held", "wide_keys", "stem_unfit"):
            tomb.pop(k, None)
        why = "re-keyed to '%s' by store doctor: an id is one kebab token" % nid
        tomb.update({"status": STATUS_DELETE_ELIGIBLE, "replaced_by": nid,
                     "retired_ts": tomb.get("retired_ts") or ts,
                     "retired_why": why, "last_updated": ts})
        _rekey_receipt(tomb, ts, why, {"status": str(e.get("status") or "")})
        # a copy, so landing the new row does not rename the report's entry
        tail.append((dict(e), new, "space_ids",
                     reason + (("; " + old[3]) if old else "")))
        tail.append((e, tomb, "space_ids", why))
    gone = {e["path"] for e in moving}
    for r in rows:
        if r["path"] in gone or not any(_names(r, e) for e in moving):
            continue
        item = items.get(r["path"])
        row = item[1] if item else dict(r)
        was = {f: str(row.get(f) or "") for f in ("supersedes", "replaced_by",
                                                   "gates")}
        changed = _repoint(row, moves)
        why = "re-pointed %s at the re-keyed id" % ", ".join(changed)
        _rekey_receipt(row, ts, why, {f: was[f] for f in changed})
        row["last_updated"] = ts
        if item:
            item[3] += "; " + why
        else:
            items[r["path"]] = [r, row, "space_ids", why]
    return [tuple(v) for v in items.values()] + tail


def _doctor_stage_dir(path):
    """Where a repair of `path` is staged: a per-run subdirectory of the
    entry's OWN directory. Same filesystem, so the final os.replace is
    atomic; SAME basename, so _commit's filename-prefix dispatch validates
    the identical row it will load as; a subdirectory, so _entry_files
    (regular .md files only) can never enumerate a stage as an entry."""
    return os.path.join(os.path.dirname(path), ".doctor-stage.%d" % os.getpid())


def doctor(project=None, fix=False, ts=None):
    """Audit (and with fix=True repair) the mechanically-repairable retrieval
    defects -> (report, actions). Read-only by default.

    --fix VALIDATES WHOLE, THEN LANDS. Every repair is serialized by the
    entry's own writer to a stage file first (so _commit's whole-object
    validation — the gloss budget, the timestamp grammar — runs on every
    row), and only when EVERY writer accepted are the stage files renamed
    over the entries. A writer's refusal mid-batch removes every stage and
    raises ValueError naming the row and its reason; the store is then
    byte-for-byte what it was — unless a stage could not be removed, in
    which case the same message names the residue path. THE DEFECT THIS
    CLOSES (row e43ec4309e89): the loop wrote each row as it went, so a
    valid cascade repair followed by a space_csv row whose existing gloss
    overran LINE_CAP left the first rewritten, the second untouched, and the
    CLI — which prints actions only after this returns — reported nothing
    at all.

    THE RENAME LEG IS THE FILESYSTEM'S BOUNDARY, NOT THE WRITER'S, and the
    public guarantee says so: one os.replace per entry, so an OS failure
    between renames lands a prefix of the batch. When that happens the
    landed rows get their receipts and events (they ARE fully validated
    entries), the un-landed stages are removed where possible, and the
    ValueError names three sets — the rows that landed, the rows that did
    not, and any stage residue left on disk — so the operator reads the
    exact state instead of a promise.

    AN ATTESTED ROW IS REWRITTEN ONLY WHEN ITS SEAL VERIFIES (_attest_verdict
    — the same digest + native-record check premise-check exits 0 on). A
    fixable row whose seal does not verify is left byte-identical, stays in
    its defect class, and is named under report["attest_unverified"] with
    the reason — in both read-only and --fix modes, so the reader learns why
    a listed row was not repaired before, not after, running --fix. The
    verified row's repair never touches the statement, so its seal is as
    valid after the rewrite as the check found it before.

    Every landed fix stamps last_updated and leaves an evidence receipt."""
    ts = ts or pk.now_ts()
    rows = load_all(project=project, include_retired=True)
    entries = [e for e in rows
               if e.get("status") in (STATUS_LIVE, STATUS_PROVISIONAL)]
    report = _doctor_classify(entries, project=project)
    held = set()
    for e in report["cascade"] + report["space_csv"] + report["stem_unfit"]:
        state, why = _attest_verdict(e, project)
        if state == "unverified":
            e["attest_why"] = why
            report["attest_unverified"].append(e)
            held.add(id(e))
    _rekey_verdicts(report, rows, project)
    actions = []
    if not fix:
        return report, actions
    plan = [p for p in _doctor_repairs(report, ts) if id(p[0]) not in held]
    plan = _doctor_rekeys(report, plan, rows, ts)
    staged, dirs = [], []
    try:
        for e, row, klass, reason in plan:
            sdir = _doctor_stage_dir(row["path"])
            if sdir not in dirs:
                dirs.append(sdir)
            stage = os.path.join(sdir, os.path.basename(row["path"]))
            _WRITERS[row["type"]](row, path=stage)
            staged.append((stage, e, row, klass, reason))
    except Exception as exc:  # noqa: BLE001 — ANY writer refusal lands nothing
        residue = _doctor_unstage([s for s, *_ in staged], dirs)
        raise ValueError(
            "doctor --fix wrote NOTHING — '%s' [%s] was refused by its "
            "writer: %s. Every repair is staged before any lands, so a "
            "refusal mid-run leaves the store exactly as it was; repair or "
            "retire that row and re-run (%d other repair%s withheld).%s"
            % (str(e["id"]), klass, exc, len(plan) - 1,
               "" if len(plan) == 2 else "s", residue)) from exc
    landed = []
    for k, (stage, e, row, klass, reason) in enumerate(staged):
        try:
            os.replace(stage, row["path"])
        except OSError as exc:
            # THE FILESYSTEM BOUNDARY: a prefix of the batch is on disk.
            # Receipt what landed, clear what can be cleared, and say
            # exactly which is which.
            for _s, le, lrow, lklass, lreason in landed:
                le.update(lrow)
                pk.event("store.doctor", str(le["id"])[:80], lreason)
                actions.append((str(le["id"]), lklass, lreason))
            residue = _doctor_unstage([s for s, *_ in staged[k:]], dirs)
            raise ValueError(
                "doctor --fix landed %d of %d repairs and the filesystem "
                "refused the next rename (%s: %s). LANDED (validated, "
                "receipted): %s. NOT landed (originals untouched): %s.%s"
                % (k, len(staged), row["path"], exc,
                   ", ".join(str(x[1]["id"]) for x in landed) or "(none)",
                   ", ".join(str(x[1]["id"]) for x in staged[k:]),
                   residue)) from exc
        landed.append((stage, e, row, klass, reason))
    residue = _doctor_unstage([], dirs)
    if residue:
        print("helm store doctor:" + residue, file=sys.stderr)
    for stage, e, row, klass, reason in landed:
        e.update(row)
        pk.event("store.doctor", str(e["id"])[:80], reason)
        actions.append((str(e["id"]), klass, reason))
    return report, actions


def _doctor_unstage(stages, dirs):
    """Remove stage files then their per-run directories -> "" when every
    one went, else one clause naming every path still on disk and why. A
    cleanup that swallowed its own failure left a stage file the message
    called 'exactly as it was'; the residue is part of the answer."""
    left = []
    for stage in stages:
        try:
            os.remove(stage)
        except OSError as exc:
            left.append("%s (%s)" % (stage, exc))
    for sdir in dirs:
        try:
            os.rmdir(sdir)
        except OSError as exc:
            if not isinstance(exc, FileNotFoundError):
                left.append("%s (%s)" % (sdir, exc))
    return (" RESIDUE left on disk, remove by hand: " + "; ".join(left)) \
        if left else ""


def retire(eid, ts, why="", project=None):
    """Human-retire: status=retired, file KEPT as the record (never deleted)."""
    bad = _refuse_bad_ts(ts)
    if bad:
        return None, bad
    e, err = _resolve_write(eid, "retire", ("prior", "heuristic", "reference"),
                            project=project)
    if err:
        return None, err
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
            force=False, guard_notes=None, by=""):
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
    # A LIVE ENTRY WITH A STAGED REVISION IS CONFIRMABLE — that pair IS the
    # missing refinement path. Without this clause the store can stage a
    # correction and never land it, which is worse than not staging one.
    # The edit machinery below is reused verbatim rather than copied: the
    # gloss-invalidation rule in particular must not exist twice.
    pending = e.get("pending_revision") if prev == STATUS_LIVE else None
    if pending and not str(new_statement or "").strip():
        new_statement = pending.get("statement")
    # WHO READ THIS BEFORE IT BECAME CANON. A correction to a LIVE entry is
    # the riskiest write this store performs — the entry is already firing
    # fleet-wide — and until now one actor could stage it and ratify it with
    # nothing recording that nobody else looked. The path of least resistance
    # went there, repeatedly, including by agents who had just read the row
    # describing the gap.
    #
    # THIS DOES NOT REFUSE, and that is deliberate rather than lenient. The
    # revise verb keeps the entry SERVING while a correction is staged,
    # precisely so canon never goes dark while it is being fixed; a refusal
    # here would wedge the one actor awake to fix something dangerous, which
    # is the same failure wearing the opposite sign. So instead: an ATTESTED
    # revision and a SELF-CONFIRMED one become two different durable
    # observables, and the caller is told which one it just made.
    revision_receipt = None
    if pending:
        staged_by = str(pending.get("by") or "").strip()
        read_by = str(pending.get("xrev_by") or "").strip()
        # THREE STATES, BECAUSE TWO CANNOT SAY THIS. An attestation is a
        # claim that a SECOND reader saw the revision, and that claim needs
        # BOTH sides named: with an UNIDENTIFIED stager, a named reader
        # cannot be shown to be anyone other than the stager, so recording
        # it as attested asserts something the record does not support.
        #
        # THE DOOR BELOW STILL ACCEPTS IT, deliberately. A seat that cannot
        # name itself is exactly the lone actor this design refuses to wedge,
        # and refusing its attestation would push it to the silent path. So
        # the third state is a RECORD, not a refusal: unverifiable says the
        # reading happened and that nothing here can prove it was a second
        # pair of eyes.
        #
        # This is _same_actor's own rule one layer up — an empty side is not
        # a match, and a non-match over an unknown must not be read as
        # evidence of somebody else.
        if not read_by:
            state = "self"
        elif _same_actor(read_by, staged_by) or _same_actor(read_by, by):
            state = "self"
        elif not staged_by:
            state = "unverifiable"
        else:
            state = "attested"
        revision_receipt = {
            "ts": ts, "staged_by": staged_by or None,
            "staged_ts": pending.get("ts"),
            "confirmed_by": str(by or "").strip() or None,
            "read_by": read_by or None,
            "state": state,
            "attested": state == "attested",
        }
    if prev not in (STATUS_CANDIDATE, STATUS_PROVISIONAL) and not pending:
        return None, ("'%s' is not a candidate or provisional entry "
                      "(status=%s), and has no staged revision — use `revise "
                      "<id> <corrected statement>` to stage one" % (eid, prev))
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
        # knowing the new statement. (Finding 2.)
        if e.get("gloss") and str(e.get("statement") or "") != s:
            e["gloss"] = ""
        e["statement"] = s
        alias = _STMT_ALIAS.get(e["type"])
        if alias:
            e[alias] = s
    if prev == STATUS_CANDIDATE:
        _set_entry_keywords(e, keywords)
    if pending:
        # THE REVISION IS CONSUMED, never left behind: a staged revision that
        # survives its own confirm would re-apply on the next one and read as
        # a correction nobody made.
        e.pop("pending_revision", None)
        # THE ATTESTATION RIDES THE EVENT, for the same reason the actor does:
        # the entry writers serialize an explicit field list, so a key set
        # here would be computed and dropped on write — and a record that
        # exists only between two statements is worse than none, because the
        # next reader finds the assignment and believes it persisted. The
        # events journal is durable for every type, and a prior's
        # evidence_log carries it too.
        note = ("live -> live (revised; %s)"
                % _revision_attestation(revision_receipt))
    else:
        note = prev + " -> live" + (" (edited)" if edited else "")
    # WHO RATIFIED IS RECORDED, NOT ASSUMED. `by` was the literal "human" in
    # the receipt below and the CLI printed "(owner-ratified)" for every
    # caller, while confirm() has no actor check of any kind — so an agent-run
    # confirm writes the owner's authority into the ledger for an edit he has
    # never seen. An unattributable confirm says UNIDENTIFIED rather than
    # borrowing a name, so a reader can tell the two apart.
    actor = str(by or "").strip() or "UNIDENTIFIED"
    # THE ACTOR IS NOT SET ON THE ENTRY, DELIBERATELY. Each `_WRITERS` writer
    # serializes an explicit field list, so a `confirmed_by` key here would be
    # computed and then dropped on write — a value that exists only between
    # two statements is worse than none, because the next reader finds the
    # assignment and believes the record exists. The two surfaces that DO
    # persist carry it instead: a prior's evidence_log below, and the events
    # journal for every type.
    e.update({"status": STATUS_LIVE, "source": "explicit",
              "updated_ts": ts, "last_updated": ts})
    if e["type"] == "prior":
        e["evidence_log"] = list(e.get("evidence_log") or []) + [
            {"ts": ts, "type": "confirmed", "delta": 0, "reason": note,
             "by": actor}]
    _WRITERS[e["type"]](e, path=e["path"])
    record_mint_events(events)
    if guard_notes is not None:
        guard_notes.extend(notes)
        if revision_receipt is not None:
            # THE CALLER IS TOLD WHAT IT JUST MADE. A self-confirmed
            # correction to live canon is a legitimate act and a different
            # one from a reviewed correction; an actor who cannot see which
            # they performed cannot choose the other.
            guard_notes.append(
                "helm store: revision %s"
                % _revision_attestation(revision_receipt))
            if revision_receipt.get("state") != "attested":
                guard_notes.append(
                    "helm store: this correction to LIVE canon carries no "
                    "independent read. It stands — a staged revision must "
                    "never wait for a reader who may not exist — and the "
                    "events journal records that nobody else saw it. To "
                    "record one next time: `helm store xrev-clear <id> --by "
                    "<reader>` BEFORE confirm.")
    # THE MUTATION JOURNAL CARRIES THE ACTOR TOO, because `helm store events`
    # is the trail somebody reads when asking who changed canon — a receipt
    # that names the change but not the changer answers the easier question.
    # THE STRUCTURED FIELD TOO, not only the summary string: recording the
    # actor in prose while leaving `actor=` on its old resolver wires the
    # value PARTWAY, and the human-readable and machine-readable halves then
    # disagree while a reader who checks the summary concludes it is done.
    pk.event("store.confirm", str(e["id"]), note + " [by " + actor + "]",
             actor=actor)
    return e, None


def revise(eid, ts, new_statement, project=None, ctype=None, by=""):
    """Stage a correction to a LIVE entry -> (entry, error). THE MISSING VERB.

    WHY IT DID NOT EXIST AND WHAT THAT COST: the lifecycle was
    evidence/supersede/retire/confirm/reject/xrev-clear/demote, and NONE of
    them edits a live statement. `evidence` moves CONFIDENCE, which is a lie
    when the core claim is right and only a clause is wrong; `supersede`
    TOMBSTONES the id and mints a new one, splitting retrieval weight across
    two ids for a statement that is mostly correct. So the store could not do
    the thing the owner asked for — "most of my requests just need composition
    or refinement of existing primitives" — and every sharpening became a new
    id.

    MEASURED COST, 2026-08-06: the premise seat-liveness-is-environ-not-cwd
    sits at certainty 1.00 and is RIGHT that identity comes from environ and
    never from cwd — while the probe it recommends, `pgrep -x claude`, matches
    comm exactly and is BLIND to panes that exec .../claude/versions/<semver>
    (measured: two such panes live on this host, comm 2.1.220 and 2.1.223).
    A safety premise telling seats to use a blind enumerator, and no verb in
    the store could correct the clause without either lying about confidence
    or fragmenting the entry.

    IT STAYS LIVE AND KEEPS SERVING THE OLD STATEMENT. That is the whole
    design constraint and it is why this is not "flip it back to provisional":
    a candidate is EXCLUDED FROM INJECT, so demoting a live entry to stage a
    correction would take the canon DARK for the window in which it is being
    corrected — worst for exactly the safety entries most worth correcting.
    The revision rides ALONGSIDE, and `confirm` swaps it in.

    RATIFICATION IS PRESERVED BECAUSE THE ID IS. A revision is not a new
    belief; it is the same belief said correctly. Confidence is untouched here
    for the same reason confirm does not inflate it — ratifying a wording is
    not evidence about the world."""
    bad = _refuse_bad_ts(ts)
    if bad:
        return None, bad
    stmt = str(new_statement or "").strip()
    if not stmt:
        return None, ("a revision needs the corrected statement — an empty "
                      "revise would stage a promise to fix it later, which is "
                      "the thing an unfixable entry already is")
    e, err = _pick_candidate(eid, project=project, ctype=ctype)
    if err:
        return None, err
    status = e.get("status") or STATUS_LIVE
    if status != STATUS_LIVE:
        return None, ("'%s' is %s, not live — confirm carries the edit for a "
                      "candidate or provisional entry; revise is the live-entry "
                      "door" % (eid, status))
    if str(e.get("statement") or "").strip() == stmt:
        return None, ("the revision is identical to the live statement — "
                      "nothing to correct")
    e["pending_revision"] = {"statement": stmt, "ts": ts,
                             "by": str(by or "") or None}
    e.update({"updated_ts": ts, "last_updated": ts})
    _WRITERS[e["type"]](e, path=e["path"])
    pk.event("store.revise", str(e["id"]), "revision staged; live unchanged")
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


def _revision_attestation(receipt):
    """One clause saying WHO READ a staged revision before it became canon.

    SELF-CONFIRMED IS NOT AN ERROR AND MUST NOT READ LIKE ONE. A seat alone
    at night correcting a dangerous entry SHOULD confirm it; the thing that
    must not happen is that the record of a reviewed correction and an
    unreviewed one look identical. So this always produces a clause, and the
    two clauses are different sentences rather than a present-or-absent
    field — an absent field reads as an older record, not as an unread one.
    """
    if not receipt:
        return "no staged revision"
    # THE DISPLAY PLACEHOLDER IS KEPT OUT OF THE IDENTITY DECISION, and that
    # separation is the whole of this function's correctness. Substituting
    # "an UNIDENTIFIED actor" into the variables the comparison below reads
    # makes two UNNAMED actors compare EQUAL — the placeholder is a single
    # literal — so a revision where NEITHER party could be named rendered as
    # "SELF-CONFIRMED: nobody else read it", a positive identity claim about
    # two actors nobody could identify. `_same_actor` is correct on its own
    # terms and its docstring states the rule that would have caught this;
    # it never saw the empties, because they were laundered into a display
    # string two lines before it was asked.
    raw_staged = receipt.get("staged_by") or ""
    raw_who = receipt.get("confirmed_by") or ""
    staged = raw_staged or "an UNIDENTIFIED actor"
    who = raw_who or "an UNIDENTIFIED actor"
    if receipt.get("state") == "attested":
        return ("staged by %s, READ BY %s, confirmed by %s"
                % (staged, receipt.get("read_by"), who))
    if receipt.get("state") == "unverifiable":
        # THE READING HAPPENED AND CANNOT BE SHOWN TO BE A SECOND ONE.
        return ("READ BY %s and confirmed by %s — UNVERIFIABLE: the actor "
                "who staged it could not be named, so nothing here shows "
                "%s was a second reader"
                % (receipt.get("read_by"), who, receipt.get("read_by")))
    if receipt.get("read_by"):
        # An attestation that names the stager or the confirmer is not a
        # second reader, and saying so is the whole point of recording it.
        return ("staged by %s, confirmed by %s — SELF-CONFIRMED: the only "
                "attestation on file names an actor already on this revision"
                % (staged, who))
    # A CONFIRM BY ANOTHER HAND IS NOT SELF-CONFIRMATION, AND IT IS THE MORE
    # DANGEROUS CASE. The staging slot holds ONE revision, so a second
    # `revise` between another actor's revise and their confirm REPLACES what
    # that confirm will land — and the confirmer ratifies canon they never
    # read. MEASURED on this store: two seats staged four seconds apart and
    # the confirm landed the second text under the first confirmer's name.
    #
    # SAYING "SELF-CONFIRMED: nobody else read it" THERE IS EXACTLY BACKWARDS.
    # It reports the ONE-actor case while naming TWO, so the line that should
    # have raised an alarm read as the ordinary lone-seat note — and the data
    # contradicting it was already inside the sentence. `_same_actor` below is
    # the comparison this branch needed and did not make; case and surrounding
    # space are not identity, which is why the two names are compared through
    # it rather than with `!=`.
    # AN UNNAMED ACTOR IS A THIRD ANSWER, NOT EITHER OF THE OTHER TWO. With a
    # side missing, "one actor" and "two actors" are both claims the record
    # cannot support, and BOTH of the sentences below assert one of them: the
    # self-confirmed line says nobody else read it, the another-hand line says
    # somebody did. This is the state the `unverifiable` branch above already
    # names for its own case, said here for the ordinary one.
    if not (raw_staged and raw_who):
        return ("staged by %s, confirmed by %s — UNATTRIBUTABLE: an actor on "
                "this revision could not be named, so nothing on file shows "
                "whether a second hand read it. Treat it as unread until the "
                "actor is established" % (staged, who))
    if not _same_actor(raw_staged, raw_who):
        return ("staged by %s, confirmed by %s — CONFIRMED BY ANOTHER HAND: "
                "the staging slot holds one revision, so this landed whatever "
                "was staged LAST and %s may never have read it; re-read the "
                "live entry and correct it if the text is not the one you "
                "meant" % (staged, who, who))
    return ("staged by %s, confirmed by %s — SELF-CONFIRMED: nobody else "
            "read it" % (staged, who))


def _same_actor(a, b):
    """True when two recorded actor strings name the same actor.

    CASE AND SURROUNDING SPACE ARE NOT IDENTITY. Both sides here are whatever
    a process could resolve about itself, written at different moments by
    different verbs, so a comparison that treats `Helm-Claude-2 ` and
    `helm-claude-2` as two readers would accept a self-attestation on a
    spelling difference. An EMPTY side is not a match: an unidentifiable
    actor is UNKNOWN, and unknown must never mean 'someone else'.
    """
    a, b = str(a or "").strip().lower(), str(b or "").strip().lower()
    return bool(a) and a == b


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
    by = str(by).strip()
    # A REVISION TO A LIVE ENTRY IS THE RISKIER OPERATION AND HAD NO
    # ATTESTATION DOOR AT ALL. The graduation ladder guards the CAPTURE lane —
    # a candidate fires nothing until a reader clears it — while a correction
    # to canon that is already firing fleet-wide could be staged and ratified
    # by one actor with nothing recording that nobody else read it. Same verb,
    # same meaning: this records WHO ATTESTS, and never runs the review.
    pending = e.get("pending_revision") if e.get("status") == STATUS_LIVE else None
    if pending:
        staged_by = str(pending.get("by") or "").strip()
        if staged_by and _same_actor(staged_by, by):
            return None, ("%s staged this revision, so attesting it as %s is "
                          "the author reading their own work — an attestation "
                          "is a claim that a SECOND reader saw it. Name the "
                          "reader, or confirm it and let the ledger record "
                          "that nobody else did." % (staged_by, by))
        pending["xrev_by"] = by
        pending["xrev_ts"] = ts
        e.update({"updated_ts": ts, "last_updated": ts})
        _WRITERS[e["type"]](e, path=e["path"])
        note = "staged revision attested by %s (live entry, unchanged)" % by
        pk.event("store.xrev_clear", str(e["id"]), note)
        return e, None
    if e.get("status") != STATUS_CANDIDATE:
        return None, "'%s' is not a candidate (status=%s) and carries no " \
            "staged revision — xrev-clear graduates a candidate or attests a " \
            "live entry's staged revision" % (eid, e.get("status"))
    keywords, err, notes, events = guard_entry_keywords(
        e["type"], e["id"], _entry_keywords(e), project=project,
        force=force, exclusions=(e,))
    if err:
        return None, err
    _set_entry_keywords(e, keywords)
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
    # prior/reference is PRINCIPLED, not the evidence defect: only these two
    # types can hold the always lane (heuristic/lexicon parse-force jit), so
    # the door stays narrow — but the untyped WINNER decides admission: a
    # heuristic winning the slug refuses BY NAME rather than silently
    # demoting a same-slug reference `get` does not show (this was
    # the one routed verb where an excluded type ranks BETWEEN admitted ones)
    e, err = _resolve_write(eid, "demote", ("prior", "reference"),
                            project=project)
    if err:
        return None, err
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
    walk exactly as inject renders it now — CONSUMED from inject's one
    admission record, never re-derived here — plus each always-entry's
    made-the-budget count
    over the fire-ledger window (current + rotated generation; a row counts
    when it carries a fired.pinned lane). Nothing here writes — the ledger is
    inject's, borrowed through its one path/line/budget surface."""
    from .. import inject
    # ONE SNAPSHOT SERVES BOTH READS. `always` and the walk's corpus were two
    # independent load_all() parses, so a store written between them yielded a
    # lane measured against entries that were never jointly live — the counts
    # stayed plausible and the mix was invisible. pinned() takes `entries`
    # precisely so a caller can share its snapshot; this is that caller.
    # ONE COMPOSITION, taken from inject — never re-paired here. gather and
    # this surface must walk the same corpus (store entries PLUS the live
    # capability self-index) AND the same always-set filtered from it. This
    # used to call store.load_all(), which omits capabilities entirely, so a
    # pinned rule whose GATE named a capability rendered its rider to the seat
    # and read as a MISSING gate here. Composing the pair locally — even
    # correctly — rebuilds that seam for the next reader to get wrong, so the
    # pairing lives in exactly one place and both callers take it.
    always, entries = inject.pinned_lane(project=project)
    # CALL THE WALK, DO NOT MODEL IT. This surface has now been wrong THREE
    # times, each time by re-deriving an answer inject already computed, and
    # each time it kept returning a plausible number while it was wrong:
    #
    #   used=0            a MODEL of the walk. Reported 3 entries fitting where
    #                     the live walk fit 2 (2026-07-30). An OPTIMISTIC
    #                     starvation predicate is the same as no predicate for
    #                     the entry it silently clears.
    #   seeded-with-WHO   correct until the 2026-08-28 ruling took WHO OUT of
    #                     PINNED_BUDGET (rules walk first, WHO after, against
    #                     WHO_CAP alone). Then it over-charged the other way.
    #   line-text match   asked `_entry_line(e) in admitted["lines"]`, a THIRD
    #                     derivation of what the plan already carries — and it
    #                     filed every GATE-EARNED entry as starved, because a
    #                     delivered gate-earned entry renders through
    #                     `_gate_line`, not `_entry_line`.
    #
    # The lesson each round taught and the next round re-learned: a model of a
    # walk is wrong the moment the walk changes, and it is wrong SILENTLY.
    # There is ONE admission computation (inject.pinned_admission) and this
    # CONSUMES it — the record, its identities, and its byte total.
    try:
        admitted = inject.pinned_admission(always, entries)
        # IDENTITY FROM THE PLAN, NEVER FROM RENDERED TEXT. This matched
        # `_entry_line(e) in admitted["lines"]`, which is a THIRD derivation of
        # the answer the plan already carries, and it was wrong in a way no
        # count would show: a GATE-EARNED entry is delivered — it keeps the
        # slot it earned — but renders through `_gate_line`, not `_entry_line`,
        # so text-matching filed it as starved while the seat was reading it.
        # A SECOND collision exists in the renderer, and its bound is
        # narrower than "unreachable here". `_entry_line_full`'s CAPABILITY
        # branch returns "CAP <body>" with NO id, so two capabilities sharing a
        # body render byte-identical. They can never be ALWAYS-entries —
        # capability.py:290 sets load_class 'jit' by construction and pinned()
        # takes 'always' — so they are not in the set this walk admits. They
        # ARE in its CORPUS, and so can be reached as gate RIDERS, which is why
        # the corpus parity above is load-bearing rather than tidiness.
        # NOT ARMED for the fits map, because fits counts admitted entries and
        # no capability is ever one.
        # The gate-earned miss above is the reachable defect, and it is armed.
        # Identity comes from the plan item, which already IS
        # (kind, entry, ...) — ask it, and no change to line SHAPE can
        # silently re-break the mapping.
        fits = set(str(it[1]["id"]) for it in admitted["plan"]
                   if it[6] and it[1] is not None
                   and it[0] in ("entry", "gate-earned"))
        used = admitted["used"]
    except Exception:            # noqa: BLE001 — stats never dies on the lane
        fits, used = set(), 0
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
