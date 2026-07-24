"""helm store — writers + lifecycle.

The byte-shape-compatible type writers, the _WRITERS dispatch, and the
lifecycle verbs (evidence/supersede/retire/confirm/reject/xrev-clear/demote)
plus pinned_stats. Moved verbatim from the pre-split helm/store.py.
"""
import json
import os
import re

from .. import home, pk
from ._common import (
    _slug, _coerce_conf, CERTAIN, BELIEF_CLAMP, derive_class, derive_load_class,
    _is_pinned, _json1, PRIOR_PREFIX, STATUS_LIVE, STATUS_RETIRED,
    STATUS_DELETE_ELIGIBLE, STATUS_CANDIDATE, STATUS_PROVISIONAL,
)
from .load import _default_dir, _find, reviewable
from .resolve import pinned


# ---------------------------------------------------------------------------
# writers — byte-shape-compatible on-disk (same key order)
# ---------------------------------------------------------------------------

def write_prior(e, root_dir=None, path=None):
    """Write a prior-*.md (the prior-*.md shape). class/load_class re-derived
    on write so a stale authored value can never desync from confidence."""
    if path is None:
        path = os.path.join(root_dir or _default_dir("prior"),
                            PRIOR_PREFIX + _slug(str(e["id"])) + ".md")
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
    pk.atomic_write(path, "\n".join(body))
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
    dom = (e.get("domain") or "").strip()
    if dom:
        body.append("  domain: " + dom)
    # a candidate carries an explicit status line (excluded from inject until
    # confirmed); a live lexicon keeps its historical byte-shape (no status key)
    status = str(e.get("status") or STATUS_LIVE)
    if status != STATUS_LIVE:
        body.insert(6, "  status: " + status)
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
    pk.atomic_write(path, "\n".join(body))
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
    pk.atomic_write(path, "\n".join(body))
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
    for opt in ("supersedes", "replaced_by", "xrev_by", "xrev_ts"):
        if e.get(opt):
            body.append("  " + opt + ": " + str(e[opt]))
    if e.get("retired_ts"):
        body += ["  retired_ts: " + str(e["retired_ts"]),
                 "  retired_why: " + (e.get("retired_why") or "")]
    body += ["---", "", "REFERENCE" + flag + ": " + summary_raw, ""]
    if e.get("url"):
        body += ["Source: " + e["url"], ""]
    pk.atomic_write(path, "\n".join(body))
    return path


_WRITERS = {"prior": write_prior, "heuristic": write_heuristic,
            "reference": write_reference, "lexicon": write_lexicon}


# ---------------------------------------------------------------------------
# lifecycle — evidence, supersede, retire (the lifecycle laws, root-aware)
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


def retire(eid, ts, why="", project=None):
    """Human-retire: status=retired, file KEPT as the record (never deleted)."""
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


def confirm(eid, ts, new_statement=None, project=None, ctype=None):
    """Owner ratify -> live (the owner/confirm gate that makes inferred capture
    safe to leave on) — works on BOTH a candidate (fires nothing) AND a
    provisional (xrev-cleared, already firing tagged): either way the owner's
    ratification makes it human-canon. v2: every capturable type (autolearn
    widened it from lexicon-only; capture everything, canonize nothing
    automatically). --edit swaps the statement in the same turn. source flips to
    'explicit' — the knowledge is now human-confirmed. A prior carries the
    who/when receipt in its own evidence_log (confidence untouched — confirming
    ratifies the capture, never inflates the belief); the other types' receipt
    is the events journal row. ctype disambiguates a slug shared across
    reviewable types (ambiguity without it is refused — never ratify the wrong
    entry)."""
    e, err = _pick_candidate(eid, project=project, ctype=ctype)
    if err:
        return None, err
    prev = e.get("status")
    if prev not in (STATUS_CANDIDATE, STATUS_PROVISIONAL):
        return None, "'%s' is not a candidate or provisional entry (status=%s)" \
            % (eid, prev)
    edited = bool(str(new_statement or "").strip())
    if edited:
        s = new_statement.strip()
        e["statement"] = s
        alias = _STMT_ALIAS.get(e["type"])
        if alias:
            e[alias] = s
    note = prev + " -> live" + (" (edited)" if edited else "")
    e.update({"status": STATUS_LIVE, "source": "explicit",
              "updated_ts": ts, "last_updated": ts})
    if e["type"] == "prior":
        e["evidence_log"] = list(e.get("evidence_log") or []) + [
            {"ts": ts, "type": "confirmed", "delta": 0, "reason": note, "by": "human"}]
    _WRITERS[e["type"]](e, path=e["path"])
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
    wait silently. HELM_NTFY_TOPIC (a full URL, else a bare topic ->
    https://ntfy.sh/<name>) opts in; UNSET -> no network call at all. stdlib
    urllib POST, 3s per-op timeout (bounds each socket op, not total
    wall-clock), a one-line body + a Title header. ALWAYS fail-open:
    any error (down notifier, DNS, timeout) is journaled as a one-line receipt
    and the graduation still succeeds — a notifier NEVER breaks the verb."""
    topic = (home.env("NTFY_TOPIC") or "").strip()
    if not topic:
        return  # opted out — never touch the network
    url = topic if topic.startswith(("http://", "https://")) \
        else "https://ntfy.sh/" + topic
    msg = "helm: %s %s now provisionally live - review when convenient" % (etype, eid)
    try:
        import urllib.request
        req = urllib.request.Request(
            url, data=msg.encode("utf-8"), method="POST",
            headers={"Title": "helm store review queue"})
        with urllib.request.urlopen(req, timeout=3):
            pass
    except Exception as ex:
        pk.event("store.notify_failed", str(eid),
                 "ntfy graduation push failed: " + str(ex)[:120])


def xrev_clear(eid, ts, by, project=None, ctype=None):
    """The graduation gate: a candidate -> provisional after a cross-family /x
    review clears it (owner canon: xrev is the gate, not the owner). The
    reviewer ATTESTS a cross-family review happened — this verb records the
    who/when, it NEVER runs the review itself. A provisional entry FIRES through
    the resolver like live but renders with a visible [provisional] tag until the
    owner ratifies (confirm) or rejects (reject) it in the web review panel. The
    receipt lands in xrev_by/xrev_ts on the file (all types) + the events
    journal, and a prior also logs it to its own evidence_log. On graduation it
    fires ONE optional push (HELM_NTFY_TOPIC) so the provisional queue reaches
    the owner — fail-open, never blocks the graduation. Refuses a missing
    reviewer, a non-candidate, and cross-type slug ambiguity (same law as
    confirm/reject)."""
    if not str(by or "").strip():
        return None, "xrev-clear requires --by <reviewer> " \
            "(who attests the cross-family review cleared it)"
    e, err = _pick_candidate(eid, project=project, ctype=ctype)
    if err:
        return None, err
    if e.get("status") != STATUS_CANDIDATE:
        return None, "'%s' is not a candidate (status=%s) — xrev-clear graduates " \
            "candidates only" % (eid, e.get("status"))
    by = str(by).strip()
    note = "candidate -> provisional (xrev-cleared by %s)" % by
    e.update({"status": STATUS_PROVISIONAL, "xrev_by": by, "xrev_ts": ts,
              "updated_ts": ts, "last_updated": ts})
    if e["type"] == "prior":
        e["evidence_log"] = list(e.get("evidence_log") or []) + [
            {"ts": ts, "type": "xrev-cleared", "delta": 0, "reason": note, "by": by}]
    _WRITERS[e["type"]](e, path=e["path"])
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
    fits, used = set(), 0
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
