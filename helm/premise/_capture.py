"""helm premise — the capture / write path.

Store leg + native attestation + in-place annotation + the anchor-retry queue +
backfill + sweep + the `helm premise` command. Depends on _common + _chain
(record_attestation); imports nothing from _verify/_cli.
"""
import json
import os
import re
import sys

from .. import cell, delim, freetext, home, pk, store
from ._chain import record_attestation
from ._common import (CHAIN_V, DEFAULT_PROFILE, _USAGE_PREMISE, _front,
                      _queue_path, _root_label, canonicalize, digest_payload,
                      payload_digest)

_GRAMMAR = "helm premise <id> | <statement> [| keywords [| domain]]"


# ---------------------------------------------------------------------------
# the anchor-retry queue (only the OPTIONAL external anchor ever queues)
# ---------------------------------------------------------------------------

def _enqueue(rec):
    path = _queue_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return path


def _read_queue(qp):
    rows = []
    try:
        with open(qp, encoding="utf-8") as f:
            for l in f:
                if l.strip():
                    try:
                        rows.append(json.loads(l))
                    except ValueError:
                        pass
    except OSError:
        pass
    return rows


def _rewrite_queue(qp, examined, kept):
    """Atomic queue rewrite that PRESERVES rows appended while the caller
    worked (the queue is append-only, so everything past `examined` rides
    along)."""
    rows = kept + _read_queue(qp)[examined:]
    pk.atomic_write(qp, "".join(json.dumps(r, ensure_ascii=False) + "\n"
                                for r in rows))


# ---------------------------------------------------------------------------
# annotation
# ---------------------------------------------------------------------------

def _annotate(path, fields):
    """Insert attest_* keys into the entry's metadata block (just before the
    closing frontmatter fence). fields = ordered (key, value) pairs; blank
    values skipped. Fail-open False on a shape surprise (non-UTF-8 file, or a
    metadata block without two fences). newline='' keeps CRLF byte-identical
    outside the inserted lines."""
    try:
        with open(path, encoding="utf-8", newline="") as f:
            lines = f.read().split("\n")
    except (OSError, ValueError):
        return False
    fences = [i for i, l in enumerate(lines) if l.strip() == "---"]
    if len(fences) < 2:
        return False
    ins = ["  %s: %s" % (k, v) for k, v in fields if str(v) != ""]
    lines[fences[1]:fences[1]] = ins
    pk.atomic_write(path, "\n".join(lines))
    return True


def _attest_fields(digest, ts, profile, info, supersedes_record=""):
    """The ordered attest_* frontmatter for one attestation."""
    fields = [("attest_payload", digest), ("attest_ts", ts),
              ("attest_by", profile), ("attest_record", info["rec_hash"]),
              ("attest_chain_index", info["chain_index"])]
    if supersedes_record:
        fields.append(("attest_supersedes_record", supersedes_record))
    if info.get("anchor_turn"):
        fields.append(("attest_anchor", info["anchor_label"]))
        fields.append(("attest_anchor_turn", info["anchor_turn"]))
    return fields


def _reconcile_enqueue(pid, project, fields):
    """Durably remember an attestation whose in-place annotation FAILED (a file-
    shape surprise), so the native record is NEVER orphaned: the record already
    stands in the chain, and a later `--retry-queue` re-annotates the entry from
    this row. fields = the ordered attest_* (key, value) pairs."""
    _enqueue({"ts": pk.now_ts(), "id": str(pid), "project": project,
              "kind": "annotate",
              "fields": [[k, str(v)] for k, v in fields],
              "reason": "annotation failed (file shape) — native record stands"})


def _annotate_or_reconcile(path, fields, pid, project):
    """Annotate the entry in place; on a shape refusal, queue a durable
    reconciliation row so the native record is not orphaned. -> annotated bool."""
    if _annotate(path, fields):
        return True
    _reconcile_enqueue(pid, project, fields)
    return False


def _report_anchor(info, pid, project, ts, rec_hash=None):
    """Print the OPTIONAL anchor's honest status; queue it for retry if it did
    not land (the native record is already the proof)."""
    if info.get("anchor_turn"):
        print("  external anchor: " + info["anchor_label"])
        return
    _enqueue({"ts": ts, "id": str(pid), "project": project,
              "rec_hash": rec_hash or info["rec_hash"], "kind": "anchor",
              "reason": info.get("anchor_reason") or "no node"})
    print("  external anchor: none yet — native record stands; queued for retry")
    print("    reason: " + (info.get("anchor_reason") or "no dregg node reachable"))


# ---------------------------------------------------------------------------
# argument helpers
# ---------------------------------------------------------------------------

def _pop_flag(args, flag, takes_value):
    if flag not in args:
        return None
    i = args.index(flag)
    if not takes_value:
        del args[i]
        return True
    if i + 1 >= len(args):
        return None
    v = args[i + 1]
    del args[i:i + 2]
    return v


def attest_profile():
    """The recording label for THIS capture (env else the test default). A
    LABEL only — helm's native attestation is not a cell signature."""
    return cell.profile_name(default=DEFAULT_PROFILE)


# ---------------------------------------------------------------------------
# capture
# ---------------------------------------------------------------------------

def _derived_keywords(statement):
    """Conservative symptom probes for the optional-keyword premise surface.

    Omitted keywords remain compatible by deriving bounded single-word probes;
    an explicitly empty third field stays empty and is refused by the shared
    mint guard. The guard still owns lint, duplicate detection, and stems.
    """
    out = []
    for word in re.findall(r"[a-z0-9][a-z0-9'-]*", statement.lower()):
        if word not in store.GENERIC_KEYWORDS and word not in out:
            out.append(word)
    return ", ".join(out[:8])


def _capture_store(parts, project, ts, force=False, corpus=None, exclusions=(),
                   mint_notes=None, mint_events=None):
    """The guarded store leg every semantic premise capture path shares.

    Re-minting over a NON-live prior life starts FRESH (stale tombstone/attest
    metadata never rides into the new lifecycle). A LIVE attested entry whose
    canonical statement would CHANGE is refused toward --supersede (an in-place
    edit orphans the attestation). The pure mint guard runs before the serializer;
    deferred duplicate receipts are returned through the optional sinks and are
    otherwise recorded only after the writer succeeds. -> (path, entry, None) or
    (None, None, refusal).
    """
    pid, statement = parts[0], parts[1]
    path = os.path.join(store._default_dir("prior", project),
                        store.PRIOR_PREFIX + pk.slug(pid) + ".md")
    e = store._parse_prior(path) or {}
    if e.get("status") == store.STATUS_LIVE and _front(e, "attest_record") \
            and canonicalize(e.get("statement")) != canonicalize(statement):
        return None, None, (
            "'%s' is LIVE and attested — an in-place edit orphans the "
            "attestation (the chain law). Evolve it instead:\n"
            "  helm premise --supersede %s <new-id> | %s%s"
            % (pid, pid, statement, (" --project " + project) if project else ""))
    keywords = parts[2] if len(parts) > 2 else \
        (e.get("keywords", "") if e else _derived_keywords(statement))
    current = dict(e, path=path, type="prior") if e else None
    omitted = tuple(exclusions) + ((current,) if current else ())
    keywords, refusal, notes, events = store.guard_entry_keywords(
        "prior", pid, keywords, project=project, force=force, corpus=corpus,
        exclusions=omitted)
    if refusal:
        return None, None, refusal
    if e and e.get("status") != store.STATUS_LIVE:
        # THE SHARED CONTRACT plus this path's own attest/evidence receipts.
        # This used to be a second hard-coded copy, and `gloss` was added to
        # the other one — so a re-minted premise kept a line it no longer said.
        from ..store._common import STALE_ON_REMINT
        for stale in STALE_ON_REMINT + (
                      "evidence_log", "confidence_history", "attest_payload",
                      "attest_ts", "attest_by", "attest_record",
                      "attest_chain_index", "attest_supersedes_record",
                      "attest_anchor", "attest_anchor_turn",
                      "attest_turn", "attest_receipt", "attest_supersedes_turn"):
            e.pop(stale, None)
    # A STAGED REVISION IS WRITTEN AGAINST A SPECIFIC STATEMENT, so an
    # in-place edit of that statement INVALIDATES it (T1 residual on
    # store-revise). This path legally rewrites a LIVE statement whenever the
    # entry is unattested — the guard above only refuses the attested case —
    # and it carried `pending_revision` straight through, leaving a correction
    # staged against text that no longer exists. `confirm` would then install
    # a sharpening of a sentence nobody can read any more.
    #
    # EXACTLY THE GLOSS RULE, one field over: a gloss is DERIVED FROM the
    # statement and is dropped rather than guessed at when the statement
    # changes. A staged revision is a proposed REPLACEMENT for the statement,
    # which is a stronger dependency, so it cannot survive what the gloss does
    # not. Dropped only when the text actually changes — a re-mint of the same
    # sentence keeps the stage, because nothing it was written against moved.
    if canonicalize(e.get("statement")) != canonicalize(statement):
        e.pop("pending_revision", None)
    e.update({"id": pid, "statement": statement, "confidence": store.CERTAIN,
              "keywords": keywords,
              "domain": parts[3] if len(parts) > 3 else e.get("domain", ""),
              "status": store.STATUS_LIVE, "stated_ts": ts, "last_updated": ts,
              "source": "human"})
    store.write_prior(e, path=path)
    if mint_notes is not None:
        mint_notes.extend(notes)
    if mint_events is not None:
        mint_events.extend(events)
    else:
        store.record_mint_events(events)
    return path, e, None


def cmd_premise(args):
    """premise <id> | <statement> [| keywords [| domain]] [--force-new] —
    store + attest. premise --supersede <old-id> <new-id> | <statement>
    [--force-new] — evolve the chain.
    premise --retry-queue — re-attempt OPTIONAL dregg anchors queued while no
    node was reachable (the native record already stands).
    premise --attest-existing <id> — backfill a native record for one entry.
    premise --attest-sweep [--dry] [--limit N] — backfill every live certain."""
    args = list(args)
    if "--retry-queue" in args:
        return _retry_queue()
    project = _pop_flag(args, "--project", True)
    sup_of = _pop_flag(args, "--supersede", True)
    if "--supersede" in args:  # flag present but valueless
        print(_USAGE_PREMISE, file=sys.stderr)
        return 2
    if "--attest-sweep" in args:
        _pop_flag(args, "--attest-sweep", False)
        dry = bool(_pop_flag(args, "--dry", False))
        limit = _pop_flag(args, "--limit", True)
        if "--limit" in args or (limit is not None and not str(limit).isdigit()):
            print(_USAGE_PREMISE, file=sys.stderr)
            return 2
        return _attest_sweep(dry=dry, limit=int(limit) if limit is not None else None)
    if "--attest-existing" in args:
        pid = _pop_flag(args, "--attest-existing", True)
        if not pid:
            print(_USAGE_PREMISE, file=sys.stderr)
            return 2
        return _attest_one(pid, project)
    no_attest = _pop_flag(args, "--no-attest", False)
    force_new = bool(_pop_flag(args, "--force-new", False))
    # arity 4 — and this verb SIGNS parts[1] into the attestation chain, so a
    # delimiter cascade here does not merely store a fragment, it attests one
    # at confidence 1.00. The refusal is load-bearing.
    spec, _crc = freetext.tail("helm premise", "add", args,
                               "the pipe-delimited premise")
    if _crc is not None:
        return _crc
    parts, refused = delim.split(spec or "", 4, _GRAMMAR)
    if refused:
        print("helm premise: " + refused, file=sys.stderr)
        return 2
    if len(parts) < 2 or not parts[0] or not parts[1]:
        print(_USAGE_PREMISE, file=sys.stderr)
        return 2
    if sup_of:
        if no_attest:
            print("helm premise: --supersede IS the attested lifecycle — for a "
                  "store-only tombstone use `helm store supersede`", file=sys.stderr)
            return 2
        return _supersede(sup_of, parts, project, force=force_new)
    pid, statement = parts[0], parts[1]
    ts = pk.now_ts()

    # 0. The SAME statement-overlap warn `store add` gives, through the SAME
    # shared wording (store.near_dup_warning) — a warn, never a block, printed
    # before the guarded store leg exactly as add prints it before its guards.
    # The --supersede leg deliberately does NOT warn: it IS the cure this warn
    # teaches, and its predecessor is being tombstoned in the same breath.
    dup, ov = store._near_dup("prior", pid, statement, project=project)
    if dup:
        warn = store.near_dup_warning(dup, ov, ts, pid, project=project)
        print("helm premise: " + warn[0])
        for line in warn[1:]:
            print(line)

    # 1. STORE — the pure guard runs before every semantic side effect; the
    # serializer lands before any deferred duplicate-override receipt.
    mint_notes, mint_events = [], []
    path, e, refuse = _capture_store(
        parts, project, ts, force=force_new,
        mint_notes=mint_notes, mint_events=mint_events)
    if refuse:
        print("helm premise: " + refuse, file=sys.stderr)
        return 1
    store.record_mint_events(mint_events)
    print("helm premise: LIVE '%s' [certain 1.00] - %s" % (pid, statement))
    print("  stored: " + path)
    for note in mint_notes:
        print(note)
    # THE one retest nudge every live capture surface ends its store leg with
    # (store._common.RETEST — the same constant `store add` prints): a capture
    # is done at FIRES, never at stored:.
    print(store.RETEST)

    if no_attest:
        print("  attestation skipped (--no-attest)")
        return 0

    # 2. ATTEST — the native chain record is the primary proof.
    digest = digest_payload(statement)
    if _front(e, "attest_record") and \
            payload_digest(_front(e, "attest_payload")) == payload_digest(digest):
        print("  already attested — record %s (verify: helm premise-check %s)"
              % (_front(e, "attest_record")[:16], pid))
        return 0
    profile = attest_profile()
    info = record_attestation("create", pid, digest, root=_root_label(e, project),
                              project=project, ts=ts, attest_by=profile)
    annotated = _annotate_or_reconcile(path, _attest_fields(digest, ts, profile, info),
                                       pid, project)
    print("  attested (native): record %s at chain_index %s — recorded by '%s'"
          % (info["rec_hash"][:16], info["chain_index"], profile))
    print("  payload: " + digest)
    if not annotated:
        print("  WARNING: file shape refused the attest_* annotation — the native "
              "record %s STANDS in the chain; queued for reconciliation "
              "(helm premise --retry-queue)" % info["rec_hash"][:16])
    _report_anchor(info, pid, project, ts)
    return 0


def _supersede(old_id, parts, project, force=False):
    """--supersede <old-id> <new-id> | <statement>: capture NEW, tombstone OLD
    (store lifecycle — lands with no node), and append ONE native supersede
    record linking supersedes_record to OLD's rec_hash. The selected predecessor
    and an exact same-id retry destination are excluded from the pure mint
    corpus; every other sibling can still refuse the revision. A never-attested
    OLD is stated honestly: the chain
    starts at the new premise."""
    old = store._find(old_id, project=project, types=("prior",))
    if not old:
        print("helm premise: '%s' not found (helm store list)" % old_id,
              file=sys.stderr)
        return 1
    if old.get("replaced_by"):
        print("helm premise: '%s' already superseded by '%s' — the chain "
              "extends from its tip, never forks"
              % (old["id"], old["replaced_by"]), file=sys.stderr)
        return 1
    if pk.slug(parts[0]) == pk.slug(str(old["id"])):
        print("helm premise: a premise cannot supersede itself — the revision "
              "needs a new id", file=sys.stderr)
        return 1
    ts = pk.now_ts()
    old_record = _front(old, "attest_record")   # capture before the tombstone
    corpus = store.load_all(project=project, include_dormant=False,
                            types=store._JIT_TYPES)
    mint_notes, mint_events = [], []
    path, _e, refuse = _capture_store(
        parts, project, ts, force=force, corpus=corpus, exclusions=(old,),
        mint_notes=mint_notes, mint_events=mint_events)
    if refuse:
        print("helm premise: " + refuse, file=sys.stderr)
        return 1
    store.record_mint_events(mint_events)
    for note in mint_notes:
        print(note)
    _old, err = store.mark_superseded(str(old["id"]), parts[0], ts,
                                      reason="premise --supersede", project=project)
    if err:
        print("helm premise: " + err, file=sys.stderr)
        return 1
    print("helm premise: LIVE '%s' [certain 1.00] - %s" % (parts[0], parts[1]))
    print("  supersedes '%s' — tombstoned (delete_eligible, file kept)"
          % old["id"])
    # the successor is a NEW live capture — the same retest law applies to it
    print(store.RETEST)
    if not old_record:
        print("  note: '%s' was never attested — the chain starts here" % old["id"])
    digest = digest_payload(parts[1])
    profile = attest_profile()
    # A never-attested predecessor has no record to link: the NATIVE chain
    # genuinely starts here, so the record commits op="create" (matching
    # _record_expect's inference from the absent supersedes_record) — the
    # STORE-level supersession link still lives in `supersedes` frontmatter.
    info = record_attestation("supersede" if old_record else "create",
                              parts[0], digest,
                              root=_root_label(_e, project), project=project,
                              ts=ts, attest_by=profile, supersedes=str(old["id"]),
                              supersedes_record=old_record)
    annotated = _annotate_or_reconcile(
        path, _attest_fields(digest, ts, profile, info,
                             supersedes_record=old_record), parts[0], project)
    print("  attested (native): record %s at chain_index %s — recorded by '%s'"
          % (info["rec_hash"][:16], info["chain_index"], profile))
    print("  payload: " + digest)
    if not annotated:
        print("  WARNING: file shape refused the attest_* annotation — the native "
              "record %s STANDS in the chain; queued for reconciliation "
              "(helm premise --retry-queue)" % info["rec_hash"][:16])
    _report_anchor(info, parts[0], project, ts)
    if old_record:
        print("  chain: -> prior record %s (attest_supersedes_record)"
              % old_record[:16])
    return 0


def _replay_annotate(rec, e):
    """A 'annotate' reconciliation row: the native record already stands but its
    in-place attest_* annotation once failed. Re-attempt it. -> ('done'|'skip'|
    'keep'). Never re-anchors, never touches the native chain."""
    if _front(e, "attest_record"):
        return "skip"    # already reconciled out of band
    fields = [(k, v) for k, v in (rec.get("fields") or [])]
    if fields and _annotate(e["path"], fields):
        print("helm premise: reconciled '%s' — attest_* annotation landed"
              % rec.get("id"))
        return "done"
    rec["reason"] = "annotation still failing — file shape refuses it"
    return "keep"


def _replay_anchor(rec, e):
    """An 'anchor' reconciliation row. If a prior pass already anchored (turn
    stored on the row) only the annotation is left — re-annotate, never re-anchor.
    Otherwise submit the anchor; on a SUCCESSFUL anchor whose annotation refuses,
    keep the row WITH the turn so the anchor is never lost and never re-sent.
    -> ('done'|'skip'|'keep')."""
    if _front(e, "attest_anchor_turn"):
        return "skip"
    turn = rec.get("anchor_turn")
    if not turn:
        rh = rec.get("rec_hash") or _front(e, "attest_record")
        turn, err = cell.anchor_submit(rh, memo="helm-attest:v%d:%s"
                                       % (CHAIN_V, rec.get("id")))
        if err:
            rec["reason"] = err
            return "keep"
    if _annotate(e["path"], [("attest_anchor", cell.anchor_label(turn)),
                             ("attest_anchor_turn", turn)]):
        print("helm premise: anchored '%s' — turn %s" % (rec.get("id"), turn[:16]))
        return "done"
    # Anchor LANDED but annotation refused: never drop a successful anchor —
    # retain the turn on the row for a lock-free re-annotation next pass.
    rec["anchor_turn"] = turn
    rec["reason"] = "anchored — annotation deferred (file shape refuses it)"
    return "keep"


def _retry_queue():
    """Drain the reconciliation queue. Two kinds ride it, both fail-open and both
    off the capture path: 'anchor' rows re-attempt the OPTIONAL dregg checkpoint;
    'annotate' rows re-attempt an in-place attest_* annotation that once refused
    (the native record already stands). A successful anchor is NEVER dropped on an
    annotation failure; rows whose entry vanished, or that still refuse, are kept."""
    qp = _queue_path()
    rows = _read_queue(qp)
    if not rows:
        print("helm premise: attest queue empty.")
        return 0
    kept = []
    done = dropped = reconciled = 0
    for rec in rows:
        e = store._find(rec.get("id", ""), types=("prior",),
                        project=rec.get("project"))
        if not e:
            rec["reason"] = "entry no longer in the store"
            kept.append(rec)
            continue
        if rec.get("kind") == "annotate":
            outcome = _replay_annotate(rec, e)
            if outcome == "done":
                reconciled += 1
            elif outcome == "skip":
                dropped += 1
            else:
                kept.append(rec)
            continue
        outcome = _replay_anchor(rec, e)
        if outcome == "done":
            done += 1
        elif outcome == "skip":
            dropped += 1
        else:
            kept.append(rec)
    _rewrite_queue(qp, len(rows), kept)
    tail = (", %d already-anchored row%s dropped" % (dropped, "s"[:dropped != 1])) \
        if dropped else ""
    if reconciled:
        tail += ", %d annotation%s reconciled" % (reconciled, "s"[:reconciled != 1])
    print("helm premise: anchor replay — %d anchored, %d still pending%s."
          % (done, len(kept), tail))
    # SAY WHY THEY ARE PENDING. `_replay_anchor` already records the exact
    # failure on each kept row (`rec["reason"]`), and this summary used to throw
    # every one of them away — so an operator saw "0 anchored, 85 still pending"
    # and could not tell a down node from a wrong auth from a bug. Measured
    # 2026-07-26: all 85 were one cause, and the per-row error already named it
    # precisely ("the node is UP and REFUSING this turn — an auth problem, not a
    # reachability one"). The diagnosis existed and the surface discarded it.
    #
    # Grouped and capped: identical causes collapse to one line with a count, so
    # 85 rows failing the same way report once rather than 85 times.
    seen = {}
    for rec in kept:
        why = str(rec.get("reason") or "").strip().splitlines()[0:1]
        if why:
            seen[why[0]] = seen.get(why[0], 0) + 1
    for why, n in sorted(seen.items(), key=lambda kv: -kv[1])[:3]:
        print("  %d pending: %s" % (n, why[:200]))
    if len(seen) > 3:
        print("  (+%d further distinct cause(s) — `helm premise --retry-queue "
              "--json` for all)" % (len(seen) - 3))
    return 0 if not kept else 1


# ---------------------------------------------------------------------------
# backfill — append a native record for entries ALREADY in the store
# ---------------------------------------------------------------------------

def attest_existing(e, profile=None, project=None, anchor=True):
    """Append a native record for an entry already in the store (adopted
    included) + annotate in place. The digest is computed from the CURRENT
    stored statement; the byte diff is exactly the attest_* lines. Returns
    (info, None) — the native record always lands; info['annotated'] False = a
    file whose shape refused the annotation (the record still stands in the
    chain), info['anchor_reason'] set = the OPTIONAL anchor did not land."""
    profile = profile or attest_profile()
    digest = digest_payload(e.get("statement") or "")
    info = record_attestation("create", str(e["id"]), digest,
                              root=e.get("root") or "global", project=project,
                              ts=pk.now_ts(), attest_by=profile, anchor=anchor)
    info["annotated"] = _annotate_or_reconcile(
        e["path"], _attest_fields(digest, info["ts"], profile, info),
        str(e["id"]), project)
    return info, None


def _attest_one(pid, project):
    """--attest-existing <id>: backfill a native record for one existing entry."""
    e = store._find(pid, project=project, types=("prior",))
    if not e:
        print("helm premise: '%s' not found (helm store list)" % pid,
              file=sys.stderr)
        return 1
    if e.get("status") != store.STATUS_LIVE:
        print("helm premise: '%s' is %s — only LIVE certain truths attest"
              % (pid, e.get("status")), file=sys.stderr)
        return 1
    if e.get("class") != "certain":
        print("helm premise: '%s' holds confidence %.2f — the attestable set "
              "is exactly the confidence-1.0 truths (beliefs never attest)"
              % (pid, e["confidence"]), file=sys.stderr)
        return 1
    if _front(e, "attest_record"):
        print("helm premise: '%s' already attested — record %s (verify: helm "
              "premise-check %s)" % (pid, _front(e, "attest_record")[:16], pid))
        return 0
    profile = attest_profile()
    info, _err = attest_existing(e, profile=profile, project=project)
    print("helm premise: attested existing '%s' [%s] — record %s (chain_index "
          "%s) recorded by '%s'" % (e["id"], e["root"], info["rec_hash"][:16],
                                    info["chain_index"], profile))
    print("  payload: " + digest_payload(e.get("statement") or ""))
    if not info.get("annotated"):
        print("  WARNING: file shape refused the annotation — the record still "
              "stands in the native chain (record %s)" % info["rec_hash"][:16])
    if info.get("anchor_turn"):
        print("  external anchor: " + info["anchor_label"])
    return 0


def _project_names():
    root = home.helm_home()
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return []
    return [n for n in names if n != home.GLOBAL and not n.startswith(".")
            and os.path.isdir(os.path.join(root, n))]


def certain_set():
    """Every LIVE certain (confidence-1.0) prior across ALL physical roots — the
    attestable set. (entry, project) pairs, deduped by real path."""
    seen = set()
    out = []
    for proj in [None] + _project_names():
        for e in store.load_all(project=proj, types=("prior",)):
            if proj and e.get("root") != "project":
                continue
            if e.get("class") != "certain":
                continue
            rp = os.path.realpath(e["path"])
            if rp not in seen:
                seen.add(rp)
                out.append((e, proj))
    return out


def _prune_queue():
    """Drop anchor-queue rows whose entry ALREADY carries an anchor turn.
    Missing-entry rows stay. Returns rows dropped."""
    qp = _queue_path()
    rows = _read_queue(qp)
    kept = [r for r in rows
            if r.get("kind") == "annotate"     # reconciliation rows never prune here
            or not _front(store._find(r.get("id", ""), types=("prior",),
                                      project=r.get("project")) or {},
                          "attest_anchor_turn")]
    if len(kept) != len(rows):
        _rewrite_queue(qp, len(rows), kept)
    return len(rows) - len(kept)


def _attest_sweep(dry=False, limit=None):
    """--attest-sweep: append a native record for every live certain entry that
    lacks one, across all roots. Offline + free — no computrons, no faucet, no
    bearer. A best-effort dregg anchor is attempted per entry (fail-open)."""
    allc = certain_set()
    todo = [(e, p) for e, p in allc if not _front(e, "attest_record")]
    profile = attest_profile()
    print("helm premise --attest-sweep: %d live certain entries — %d attested, %d to attest"
          % (len(allc), len(allc) - len(todo), len(todo)))
    if limit is not None:
        todo = todo[:limit]
        print("  --limit: at most %d this pass" % limit)
    if dry:
        print("  dry run — %d native records to append (offline, no cost); "
              "recording label '%s'; nothing written" % (len(todo), profile))
        return 0
    if not todo:
        pruned = _prune_queue()
        if pruned:
            print("  pruned %d stale queue row%s (entries already anchored)"
                  % (pruned, "s"[:pruned != 1]))
        print("helm premise --attest-sweep: nothing to attest.")
        return 0
    done = pending = 0
    for e, proj in todo:
        info, _err = attest_existing(e, profile=profile, project=proj)
        note = "" if info.get("annotated") else \
            " [ANNOTATION FAILED — record in the native chain]"
        anchor = "" if info.get("anchor_turn") else " (anchor pending)"
        if not info.get("anchor_turn"):
            _enqueue({"ts": pk.now_ts(), "id": str(e["id"]), "project": proj,
                      "rec_hash": info["rec_hash"], "kind": "anchor",
                      "reason": info.get("anchor_reason") or "no node"})
            pending += 1
        print("  attested '%s' [%s] — record %s (chain %s)%s%s"
              % (e["id"], e["root"], info["rec_hash"][:16], info["chain_index"],
                 note, anchor))
        done += 1
    pruned = _prune_queue()
    tail = (", %d stale queue row%s pruned" % (pruned, "s"[:pruned != 1])) \
        if pruned else ""
    print("helm premise --attest-sweep: %d attested (native), %d anchor%s pending%s."
          % (done, pending, "s"[:pending != 1], tail))
    return 0
