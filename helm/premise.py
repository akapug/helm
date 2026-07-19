#!/usr/bin/env python3
"""helm premise — attested premise capture per
docs/DECISION-dregg-attested-premises.md (+ the live-proof addendum).

A premise (a confidence-1.0 truth) is written into the typed store
(store.write_prior — the same premise path `helm store add premise` takes)
AND attested on the dregg substrate as ONE self-write turn: the digest of the
canonical statement rides the WHISPER PAYLOAD slots via the proven meld-send
path — NEVER the 8-byte heartbeat tag (addendum law #1). `premise-check`
recomputes the digest from the STORED statement, compares it to the attested
payload, and quotes the FINALITY TIER the node proved (addendum law #2).

CANONICALIZATION (the digest contract — stable by construction):
  1. unicodedata.normalize("NFC", statement)
  2. replace every '"' with "'"  — mirrors store.write_prior's serialization
     (it swaps double quotes for single on write), so the digest recomputed
     from the stored statement always equals the digest taken at capture
  3. collapse every whitespace run to one space  (re.sub(r"\\s+", " ", s))
  4. strip leading/trailing whitespace

DIGEST — ALGORITHM-TAGGED because blake3 tooling is absent on this host
(verified 2026-07-18: no b3sum on PATH, no python `blake3` module):
  payload = "prem:b2b:" + hashlib.blake2b(canonical_utf8, digest_size=32).hexdigest()
73 ASCII bytes — inside the whisper frame's 104-byte budget. The tag makes the
algorithm self-describing and upgradeable: when blake3 tooling lands, a
"prem:b3:<hex>" payload coexists and old attestations stay verifiable.

IDENTITY: an agent-run capture CANNOT be user-signed — the user's cell signs
only when the USER captures. The signing profile is recorded on the entry as
`attest_by`; the default (no HELM_CELL_PROFILE / MELD_AGENT_PROFILE set) is
the TEST profile 'helm-test', so a test-signed attestation is never mistaken
for the user's. The user-cell activation is the owner's first /premise with
their own profile set — by design.

Substrate down / binary missing / no profile: the premise is STORED anyway,
the attestation is queued to <helm-home>/_global/.state/attest-queue.jsonl for
retry, and the capture reports "attestation pending (substrate unavailable)".

BACKFILL (--attest-existing <id> / --attest-sweep): entries captured BEFORE
attestation existed (the adopted corpus included) are attested IN PLACE — the
digest is computed per the same contract from the entry's CURRENT stored
statement, one self-write turn is submitted, and the entry's file is annotated
with the attest_* keys ONLY (never a rewrite, never a global twin of an
adopted entry, never through add — so the supersede-guard cannot trip). The
sweep covers every live certain (confidence-1.0) prior across ALL roots,
sequentially, under ONE minted bearer token: the node rate-limits
/api/cipherclerk/unlock to 5/60s COUNTING SUCCESSES (emberian/dregg#60) and
meld re-unlocks per send when only a passphrase is set, so the sweep unlocks
once and rides MELD_NODE_TOKEN. Per-entry resilience: insufficient balance
auto-refuels via the dev faucet and resends; any other failure resends once
after a pause, then falls to the attest-queue — the sweep never crashes.

NOTE on the attest_* frontmatter: store.write_prior owns the entry's byte
shape, so this module appends the attest keys into the metadata block after
the write; the store's parsers + lifecycle writers carry all six attest_*
keys through rewrites (evidence/retire), so an annotation survives the
entry's lifecycle. The attestation TRUTH lives on the ledger either way —
the file keys are the pointer back to it (turn hash, receipt, chain index).

Import-safe, stdlib-only.
"""
import hashlib
import json
import os
import re
import sys
import time
import unicodedata

from . import cell, home, pk, store

DIGEST_TAG = "prem:b2b:"          # blake2b-256 (see module docstring)
DEFAULT_PROFILE = "helm-test"     # test-signed by default — never the user's cell

# Backfill resilience knobs (the sweep exercises the live node at corpus scale):
RETRY_PAUSE_S = 2      # pause before the one retry + between queued failures
FAUCET_AMOUNT = 10000  # dev-faucet grant ceiling per request (computrons)
FAUCET_WINDOW_S = 61   # the faucet allows 1 grant per cell per 60s — wait it out
ATTEST_COST_HINT = 1442  # one attest turn's computron cost, observed live 2026-07-19

_USAGE_PREMISE = ("usage: helm premise <id> | <statement> [| keywords [| domain]] "
                  "[--project P] [--no-attest]\n"
                  "       helm premise --retry-queue\n"
                  "       helm premise --attest-existing <id> [--project P]\n"
                  "       helm premise --attest-sweep [--dry] [--limit N]")
_USAGE_CHECK = "usage: helm premise-check <id> [--project P]"


def canonicalize(statement):
    """The exact canonical text the digest covers (see module docstring):
    NFC -> '\"'->\"'\" -> collapse-whitespace -> strip."""
    s = unicodedata.normalize("NFC", statement or "")
    s = s.replace('"', "'")
    return re.sub(r"\s+", " ", s).strip()


def digest_payload(statement):
    """The algorithm-tagged attestation payload for a statement (73 bytes)."""
    h = hashlib.blake2b(canonicalize(statement).encode("utf-8"), digest_size=32)
    return DIGEST_TAG + h.hexdigest()


def _queue_path():
    return os.path.join(home.global_dir(), ".state", "attest-queue.jsonl")


def _enqueue(rec):
    path = _queue_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return path


def _annotate(path, fields):
    """Insert attest_* keys into the entry's metadata block (just before the
    closing frontmatter fence). fields = ordered (key, value) pairs; blank
    values are skipped. Fail-open False on a shape surprise."""
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().split("\n")
    except OSError:
        return False
    fences = [i for i, l in enumerate(lines) if l.strip() == "---"]
    if len(fences) < 2:
        return False
    ins = ["  %s: %s" % (k, v) for k, v in fields if str(v) != ""]
    lines[fences[1]:fences[1]] = ins
    pk.atomic_write(path, "\n".join(lines))
    return True


def _pop_flag(args, flag, takes_value):
    """Remove `flag` (and its value) from args; return the value (or True)."""
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
    """The signing profile for THIS capture (env else the test default)."""
    return cell.profile_name(default=DEFAULT_PROFILE)


def cmd_premise(args):
    """premise <id> | <statement> [| keywords [| domain]] — store + attest.
    premise --retry-queue — replay attestations queued while the substrate
    was down (success annotates the entry + leaves the queue; failures stay).
    premise --attest-existing <id> — backfill-attest one entry already in the
    store (annotation in place, wherever the file lives).
    premise --attest-sweep [--dry] [--limit N] — backfill every live certain
    entry lacking a recorded turn, across all roots."""
    args = list(args)
    if "--retry-queue" in args:
        return _retry_queue()
    project = _pop_flag(args, "--project", True)
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
    parts = [p.strip() for p in " ".join(args).split("|")]
    if len(parts) < 2 or not parts[0] or not parts[1]:
        print(_USAGE_PREMISE, file=sys.stderr)
        return 2
    pid, statement = parts[0], parts[1]
    ts = pk.now_ts()

    # 1. STORE — same target + shape as `helm store add premise` (write_prior,
    # confidence CERTAIN, source human); attestation failure never loses it.
    path = os.path.join(store._default_dir("prior", project),
                        store.PRIOR_PREFIX + pk.slug(pid) + ".md")
    e = store._parse_prior(path) or {}
    e.update({"id": pid, "statement": statement, "confidence": store.CERTAIN,
              "keywords": parts[2] if len(parts) > 2 else e.get("keywords", ""),
              "domain": parts[3] if len(parts) > 3 else e.get("domain", ""),
              "status": store.STATUS_LIVE, "stated_ts": ts, "last_updated": ts,
              "source": "human"})
    store.write_prior(e, path=path)
    print("helm premise: LIVE '%s' [certain 1.00] - %s" % (pid, statement))
    print("  stored: " + path)

    if no_attest:
        print("  attestation skipped (--no-attest)")
        return 0

    # 2. ATTEST — the digest rides the whisper PAYLOAD slots (self-write send).
    payload = digest_payload(statement)
    profile = attest_profile()
    info, err = cell.send_self(payload, profile)
    if err:
        qp = _enqueue({"ts": ts, "id": pid, "project": project,
                       "payload": payload, "profile": profile, "reason": err})
        print("  attestation pending (substrate unavailable) — queued: " + qp)
        print("    reason: " + err)
        return 0
    _annotate(path, [("attest_payload", payload), ("attest_ts", ts),
                     ("attest_by", profile),
                     ("attest_turn", info.get("turn_hash", "")),
                     ("attest_receipt", info.get("receipt_hash", "")),
                     ("attest_chain_index", info.get("chain_index", ""))])
    print("  attested: turn %s (chain_index %s) signed by profile '%s'"
          % (info.get("turn_hash"), info.get("chain_index"), profile))
    print("  payload: " + payload)
    return 0


def _retry_queue():
    """Replay pending attestations. Each success annotates the stored entry
    and drops the row; failures (and rows whose entry vanished) are kept."""
    qp = _queue_path()
    try:
        with open(qp, encoding="utf-8") as f:
            rows = [json.loads(l) for l in f if l.strip()]
    except OSError:
        rows = []
    if not rows:
        print("helm premise: attest queue empty.")
        return 0
    kept = []
    done = 0
    for rec in rows:
        e = store._find(rec.get("id", ""), types=("prior",),
                        project=rec.get("project"))
        if not e:
            rec["reason"] = "entry no longer in the store"
            kept.append(rec)
            continue
        info, err = cell.send_self(rec["payload"], rec.get("profile") or attest_profile())
        if err:
            rec["reason"] = err
            kept.append(rec)
            continue
        _annotate(e["path"], [("attest_payload", rec["payload"]),
                              ("attest_ts", pk.now_ts()),
                              ("attest_by", rec.get("profile") or attest_profile()),
                              ("attest_turn", info.get("turn_hash", "")),
                              ("attest_receipt", info.get("receipt_hash", "")),
                              ("attest_chain_index", info.get("chain_index", ""))])
        print("helm premise: attested '%s' from queue — turn %s"
              % (rec["id"], (info.get("turn_hash") or "")[:16]))
        done += 1
    pk.atomic_write(qp, "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in kept))
    print("helm premise: queue replay — %d attested, %d still pending." % (done, len(kept)))
    return 0 if not kept else 1


# ---------------------------------------------------------------------------
# backfill — attest entries ALREADY in the store (see the module docstring)
# ---------------------------------------------------------------------------

def _post_json(url, payload, timeout=10):
    """One JSON POST -> (parsed body, None) or (None, reason). An empty or
    garbled body (the unlock limiter's empty-body 429) is a reason, never a
    crash; a caught HTTPError is closed (the ResourceWarning gate)."""
    import urllib.error
    import urllib.request
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        exc.close()
        return None, "HTTP %s" % exc.code
    except Exception as exc:
        return None, str(exc)
    try:
        return json.loads(raw), None
    except ValueError:
        return None, "unparseable response body: %r" % raw[:80]


def refuel(profile):
    """Dev-faucet refuel: POST /api/faucet for FAUCET_AMOUNT computrons to the
    profile's OWN cell — recipient + public key read programmatically (cell
    cache + ~/.dregg/profiles/<name>.json), never typed in. (True, None) or
    (False, reason). The faucet answers a refusal as HTTP 200 with
    success:false + error (per-cell limiter: 1 grant/60s) — an unchecked body
    read as success starved a whole sweep once; the body's own verdict is the
    verdict. One grant covers ~6 attest turns (ATTEST_COST_HINT)."""
    hexid, err = cell.own_cell(profile)
    if err:
        return False, err
    prof = pk.read_json(os.path.join(cell.profiles_dir(), profile + ".json"),
                        {}) or {}
    pub = str(prof.get("public_key_hex") or "")
    if not pub:
        return False, "profile '%s' carries no public_key_hex" % profile
    body, perr = _post_json(cell.node_url() + "/api/faucet",
                            {"recipient": hexid, "amount": FAUCET_AMOUNT,
                             "public_key": pub})
    if perr:
        return False, "faucet refused: " + perr
    if not (body or {}).get("success"):
        return False, "faucet refused: " + str((body or {}).get("error")
                                               or "no success in response")
    return True, None


def mint_node_token(wait_on_limit=False, pause=None):
    """POST /api/cipherclerk/unlock ONCE -> (bearer token, None) or (None,
    reason). The node allows 5 unlocks/60s and counts successes
    (emberian/dregg#60) — the limiter's refusal is an empty-body 429; with
    wait_on_limit a single limiter-window wait buys one re-mint."""
    pause = time.sleep if pause is None else pause
    phrase = home.env("NODE_PASSPHRASE")
    if not phrase:
        return None, "no HELM_NODE_PASSPHRASE/MELD_NODE_PASSPHRASE to mint from"
    url = cell.node_url() + "/api/cipherclerk/unlock"
    body, err = _post_json(url, {"passphrase": phrase})
    if not (body and body.get("bearer_token")) and wait_on_limit:
        pause(60)  # the unlock limiter window
        body, err = _post_json(url, {"passphrase": phrase})
    if body and body.get("success") and body.get("bearer_token"):
        return body["bearer_token"], None
    return None, ("unlock mint failed: "
                  + (err or (body or {}).get("error") or "no bearer_token"))


def _install_token(tok):
    """Ride the minted bearer for every subsequent meld send in THIS process:
    the passphrase leaves the env so meld prefers the token over a per-send
    unlock (cell.build_env maps MELD_NODE_TOKEN through untouched)."""
    os.environ["MELD_NODE_TOKEN"] = tok
    for k in ("HELM_NODE_PASSPHRASE", "MELD_NODE_PASSPHRASE"):
        os.environ.pop(k, None)


def _enter_token_mode(pause=None):
    """The sweep's auth posture: a token already in env rides as-is; else ONE
    bearer is minted from the passphrase and installed. (mode-line, None) or
    (None, reason) — reason means per-send passphrase unlocks remain, which
    the 5/60s limiter will throttle at corpus scale."""
    if home.env("NODE_TOKEN"):
        return "bearer token (from env)", None
    tok, err = mint_node_token(wait_on_limit=True, pause=pause)
    if not tok:
        return None, err
    _install_token(tok)
    return "bearer token (minted once from the passphrase)", None


def attest_existing(e, profile=None, pause=None):
    """Attest an entry ALREADY in the store, wherever its file lives (adopted
    included): digest per the capture contract from the CURRENT stored
    statement, one self-write turn, then annotation ONLY — the byte diff is
    exactly the attest_* lines; statement/keywords/confidence are never
    touched, no twin is minted, add's supersede-guard never runs (this path
    never goes through add). Resilience: an insufficient-balance refusal
    refuels via the dev faucet and resends — waiting out the faucet's
    1-grant/cell/60s window once when the grant itself is rate-limited (the
    faucet cadence IS the sweep's sustainable pace, ~6 turns/grant); any
    other failure resends once after RETRY_PAUSE_S. Returns (info, None) or
    (None, reason); info["annotated"] False = the turn landed but the file's
    shape refused the annotation (the receipt still lives on the ledger)."""
    pause = time.sleep if pause is None else pause
    payload = digest_payload(e.get("statement") or "")
    profile = profile or attest_profile()
    refueled = retried = False
    info, err = cell.send_self(payload, profile)
    while err:
        if not refueled and "insufficient balance" in err:
            refueled = True
            ok, ferr = refuel(profile)
            if not ok and "rate limited" in (ferr or ""):
                pause(FAUCET_WINDOW_S)  # the per-cell grant window
                ok, ferr = refuel(profile)
            if ok:
                info, err = cell.send_self(payload, profile)
                continue
        if retried:
            return None, err
        retried = True
        pause(RETRY_PAUSE_S)
        info, err = cell.send_self(payload, profile)
    info["annotated"] = _annotate(e["path"], [
        ("attest_payload", payload), ("attest_ts", pk.now_ts()),
        ("attest_by", profile),
        ("attest_turn", info.get("turn_hash", "")),
        ("attest_receipt", info.get("receipt_hash", "")),
        ("attest_chain_index", info.get("chain_index", ""))])
    return info, None


def _attest_one(pid, project):
    """--attest-existing <id>: backfill-attest one existing entry in place."""
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
    if e.get("attest_turn"):
        print("helm premise: '%s' already attested — turn %s (verify: helm "
              "premise-check %s)" % (pid, e["attest_turn"], pid))
        return 0
    profile = attest_profile()
    info, err = attest_existing(e, profile=profile)
    if err:
        qp = _enqueue({"ts": pk.now_ts(), "id": str(e["id"]), "project": project,
                       "payload": digest_payload(e.get("statement") or ""),
                       "profile": profile, "reason": err})
        print("  attestation pending (substrate unavailable) — queued: " + qp)
        print("    reason: " + err)
        return 1
    print("helm premise: attested existing '%s' [%s] — turn %s (chain_index %s)"
          " signed by profile '%s'" % (e["id"], e["root"], info.get("turn_hash"),
                                       info.get("chain_index"), profile))
    print("  payload: " + digest_payload(e.get("statement") or ""))
    if not info.get("annotated"):
        print("  WARNING: file shape refused the annotation — the receipt "
              "lives on the ledger (turn above)")
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
    """Every LIVE certain (confidence-1.0) prior across ALL physical roots —
    the attestable set (exactly the operator's stated truths, ATTESTATION.md).
    The global view covers adopted + helm-global; each project home under the
    helm root adds its project-scoped entries. (entry, project) pairs, deduped
    by real path (a symlinked project home never yields a double)."""
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
    """Drop attest-queue rows whose entry ALREADY carries an attest_turn (it
    was attested directly after the row was queued — replaying the row would
    double-annotate). Missing-entry rows stay, as ever. Returns rows dropped."""
    qp = _queue_path()
    try:
        with open(qp, encoding="utf-8") as f:
            rows = [json.loads(l) for l in f if l.strip()]
    except OSError:
        return 0
    kept = [r for r in rows
            if not ((store._find(r.get("id", ""), types=("prior",),
                                 project=r.get("project")) or {}).get("attest_turn"))]
    if len(kept) != len(rows):
        pk.atomic_write(qp, "".join(json.dumps(r, ensure_ascii=False) + "\n"
                                    for r in kept))
    return len(rows) - len(kept)


def _attest_sweep(dry=False, limit=None):
    """--attest-sweep: backfill-attest every live certain entry lacking a
    recorded turn, across all roots, via the SAME per-entry path as
    --attest-existing. Never crashes: a persistent per-entry failure falls to
    the attest-queue; the whole pass runs under one minted bearer; one
    mid-sweep re-mint covers a bearer expiring (TTL unknown). --dry reports
    the certain-set count + estimated computrons and sends nothing."""
    allc = certain_set()
    todo = [(e, p) for e, p in allc if not e.get("attest_turn")]
    profile = attest_profile()
    print("helm premise sweep: %d live certain entries — %d attested, %d to attest"
          % (len(allc), len(allc) - len(todo), len(todo)))
    if limit is not None:
        todo = todo[:limit]
        print("  --limit: at most %d this pass" % limit)
    if dry:
        print("  dry run — estimated ~%d computrons (~%d/turn observed live); "
              "signing profile '%s'; nothing sent"
              % (len(todo) * ATTEST_COST_HINT, ATTEST_COST_HINT, profile))
        return 0
    if not todo:
        pruned = _prune_queue()
        if pruned:
            print("  pruned %d stale queue row%s (entries already attested)"
                  % (pruned, "s"[:pruned != 1]))
        print("helm premise sweep: nothing to attest.")
        return 0
    mode, merr = _enter_token_mode()
    print("  auth: " + (mode if mode
                        else "per-send passphrase unlocks (%s) — the 5/60s "
                             "unlock limiter may throttle" % merr))
    reminted = False
    done = queued = 0
    for e, proj in todo:
        info, err = attest_existing(e, profile=profile)
        if err and mode and not reminted and "insufficient balance" not in err:
            # one mid-sweep re-mint covers an expired bearer (a balance
            # refusal is not an auth failure — re-minting buys nothing)
            reminted = True
            tok, _terr = mint_node_token(wait_on_limit=True)
            if tok:
                _install_token(tok)
                info, err = attest_existing(e, profile=profile)
        if err:
            _enqueue({"ts": pk.now_ts(), "id": str(e["id"]), "project": proj,
                      "payload": digest_payload(e.get("statement") or ""),
                      "profile": profile, "reason": err})
            queued += 1
            print("  QUEUED '%s' — %s"
                  % (e["id"], err.strip().splitlines()[-1][:110]))
            # pace the failure path: a fast queue-storm (2 sends/entry) once
            # tripped the node's 60-submits/60s limiter and 429'd the rest
            time.sleep(RETRY_PAUSE_S)
            continue
        done += 1
        note = "" if info.get("annotated") else \
            " [ANNOTATION FAILED — receipt on ledger only]"
        print("  attested '%s' [%s] — turn %s (chain %s)%s"
              % (e["id"], e["root"], (info.get("turn_hash") or "")[:16],
                 info.get("chain_index"), note))
    pruned = _prune_queue()
    tail = (", %d stale queue row%s pruned" % (pruned, "s"[:pruned != 1])) \
        if pruned else ""
    print("helm premise sweep: %d attested, %d queued for retry%s."
          % (done, queued, tail))
    return 0 if not queued else 1


# ---------------------------------------------------------------------------
# premise-check
# ---------------------------------------------------------------------------

_CHECK_DEFAULTS = {"attest_payload": "", "attest_ts": "", "attest_by": "",
                   "attest_turn": "", "attest_receipt": ""}


def _fetch_turn_status(turn_hash):
    """GET /api/turn/{hash}/status. Observed live response shape:
    {"attested_height": N, "consensus_final": bool, "receipt_hash": hex,
     "receipt_present": bool, "turn_hash": hex}. None when unreachable."""
    return cell.get_json(cell.node_url() + "/api/turn/" + turn_hash + "/status")


def finality_tier(st):
    """Map the node's turn-status response to the finality tier it PROVES —
    quoted verbatim in the check output (addendum law #2). Honest mapping of
    the observed fields: consensus_final at an attested height is the
    after-next-height tier; a receipt alone is only ingress-immediate."""
    if not st:
        return "unverified (node unreachable)"
    if st.get("consensus_final") and st.get("attested_height") is not None:
        return ("attested-after-next-height (consensus_final at attested_height %s)"
                % st["attested_height"])
    if st.get("receipt_present"):
        return ("ingress-immediate (receipted on the node; not yet "
                "consensus-final at an attested height)")
    return "unverified (turn not found on the node)"


def cmd_premise_check(args):
    """premise-check <id> — recompute the digest from the stored statement,
    compare to the attested payload, and quote the verified finality tier."""
    args = list(args)
    project = _pop_flag(args, "--project", True)
    pid = " ".join(a for a in args if not a.startswith("--")).strip()
    if not pid:
        print(_USAGE_CHECK, file=sys.stderr)
        return 2
    e = store._find(pid, project=project, types=("prior",))
    if not e:
        print("helm premise-check: '%s' not found (helm store list)" % pid,
              file=sys.stderr)
        return 1
    print("helm premise-check: " + str(e["id"]))
    print("  statement: " + (e.get("statement") or ""))
    meta = pk.parse_simple_frontmatter(e["path"], _CHECK_DEFAULTS) or {}
    if not meta.get("attest_payload"):
        print("  no attestation recorded — captured with --no-attest or while "
              "the substrate was down (a pending record may sit in "
              + _queue_path() + ")")
        return 0
    recomputed = digest_payload(e.get("statement") or "")
    match = recomputed == meta["attest_payload"]
    if match:
        print("  digest: MATCH " + recomputed)
    else:
        print("  digest: MISMATCH — the stored statement no longer hashes to "
              "the attested payload")
        print("    attested:   " + meta["attest_payload"])
        print("    recomputed: " + recomputed)
    print("  attested by profile '%s' at %s"
          % (meta.get("attest_by") or "?", meta.get("attest_ts") or "?"))
    turn = meta.get("attest_turn")
    if turn:
        print("  turn: " + turn)
        print("  finality tier: " + finality_tier(_fetch_turn_status(turn)))
    else:
        print("  finality tier: unverified (no turn hash recorded)")
    return 0 if match else 1
