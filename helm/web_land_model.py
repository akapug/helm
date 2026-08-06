"""Land-pipeline source model for :mod:`helm.web`."""
import re
import sys

_web = sys.modules.get(__package__ + ".web")
if _web is not None:
    globals().update({name: value for name, value in vars(_web).items()
                      if not (name.startswith("__") and name.endswith("__"))})

# How long /api/lr blocks a COLD reader before answering "warming" — under the
# card's 12s deadline with room, so a build that beats it still returns rows.
_LR_COLD_WAIT_S = 8


_LR_FLOOR_S = 30            # the floor: at most one projection per 30s

# The truth cap for serve-stale-while-revalidating: a body may be served
# STALE only while younger than this and only with a rebuild in flight.
# 4x the floor gives a slow projection two full build-lengths of headroom
# before the surface stops serving its past and blocks for the truth.
_LR_HARD_TTL_S = 120

_LR_CLOSED_WINDOW_S = 86400  # "closed in the last 24h"

_LR_CLOSED_CAP = 12          # the phone shows a footer, not a history

_LR_LANDS_CAP = 6            # dashboard rows: above the fold on a phone, not a history

_LR_LANDS_WINDOW_S = 86400   # the "and N more today" window, same 24h as the footer


# THE LANDING GRAMMAR the integrator already writes on every fold:
# `fold: <lane> at <reviewed tip> (dispatch ..., <reviewer> APPROVE gate:...)`.
#
# NON-GREEDY, and the hex constraint is what makes that safe. A lane label may
# contain spaces — measured on this repo, 23 of 145 folds do ("idle-dispatch
# names live pane state at <tip>") — so the label cannot be \S+. The anchor
# is therefore the first ` at ` FOLLOWED BY a 7-40 character object name at a
# word boundary; an ` at ` inside the prose backtracks past because no object
# name follows it.
#
# MEASURED over every fold commit on origin/main: 145 subjects, 145
# parsed, and all 145 parsed tips resolve as real commits (`git cat-file -e`).
# The greedy variant was measured beside it and disagreed on zero of 145; the
# non-greedy one is kept because it cannot be dragged forward by a hex-looking
# token later in the subject.
_LR_FOLD = re.compile(r"^fold:\s+(?P<lane>.+?)\s+at\s+(?P<tip>[0-9a-f]{7,40})\b")



def _lr_ledger_paths():
    """Every ledger the land projection reads. The gate receipts ledger is the
    VERIFICATION half and does not exist until that lane lands, so it is probed
    tolerantly — the fingerprint picks it up the day it appears, and until then
    its absence is simply one more term."""
    from . import dispatches, landreq
    paths = [dispatches.ledger_path(), landreq.receipts_path()]
    try:
        from . import gate
        receipts_path = getattr(gate, "receipts_path", None)
        if callable(receipts_path):
            paths.append(receipts_path())
    except Exception:
        pass
    return paths



def _lr_newest_mtime():
    """Newest mtime across those ledgers, or None when none of them exist —
    the card's "newest ledger write Ns ago" term. This is a DISPLAY fact, and
    deliberately not a cache key; see _lr_build."""
    newest = None
    for path in _lr_ledger_paths():
        try:
            st = os.stat(path)
        except OSError:
            continue
        newest = st.st_mtime if newest is None else max(newest, st.st_mtime)
    return newest



def _lr_epoch(ts):
    """Epoch for a ledger stamp, or None. dispatches._age_s answers 0 for an
    unparseable stamp — i.e. "just now" — so a row whose stamp cannot be read
    would otherwise be the freshest thing in the closed-today list."""
    try:
        return calendar.timegm(time.strptime(str(ts), "%Y-%m-%dT%H:%M:%SZ"))
    except (TypeError, ValueError):
        return None



def _lr_window(ts, now):
    """"in" | "out" | "unusable" for one closure instant against the 24h window.

    THE WINDOW HAS TWO SIDES AND THE TEST ONLY HAD ONE. `now - ts <= 86400` is
    satisfied by every instant in the FUTURE as well — the subtraction goes
    negative and sails under the ceiling — so a row whose record says it closed
    in 2099 was counted, confidently, as "closed in the last 24h" and printed
    among the newest. A closure that has not happened yet is not recent; it is
    a broken clock or a broken record, and either way the instant is UNUSABLE.
    Unusable is answered as its own state so the caller COUNTS the row as
    closed-at-an-unknown-time rather than dropping it, which is the other half
    of this footer's history.

    ONE owner for both readings the caller has (the closure stamp and the
    entered_ts lower bound), because a guard applied to one of two identical
    comparisons is a guard the next reader walks around."""
    if ts is None:
        return "unusable"
    from . import landreq
    age = now - ts
    if age < -landreq.FUTURE_SKEW_S:
        return "unusable"
    return "in" if age <= _LR_CLOSED_WINDOW_S else "out"



def _lr_repo():
    """(gitdir, trunk_ref) for the repository this dashboard reports on, or
    (None, None) when there is no repository to report on.

    ONE OWNER FOR "WHICH REPOSITORY". dispatches._repo_info is the same
    derivation that stamps `repo_id` onto every dispatch row at dispatch time,
    so the LANDED card and the ledger cannot come to different conclusions
    about which tree they are describing. Re-deriving it here from a second
    rule is how two surfaces start disagreeing about one record.

    Which trunk ANSWERS follows landreq's own precedence unchanged: the
    upstream ref when an origin is configured, the local one otherwise."""
    from . import dispatches, landreq
    info = dispatches._repo_info()
    gitdir = (info or {}).get("repo_id")
    if not gitdir:
        return None, None
    local, upstream = landreq._trunk_refs(gitdir, {})
    return gitdir, (upstream if landreq._origin_configured(gitdir) else local)



def _lr_witnessed(tip, tips):
    """True / False / None — is a signed land receipt recorded for this tip?

    A BADGE, NEVER A GATE. The receipt's job is the signed content-identity
    attestation, which is an INTEGRITY question; whether the work landed is a
    question trunk already answered by carrying the fold. So a missing receipt
    changes this field and nothing else — it can never remove a row.

    `tips` is None when the receipt index could not be READ, which is UNKNOWN
    and says so rather than accusing every land of being unwitnessed. Fold
    subjects carry an abbreviated object name, so the match is by prefix, and
    an AMBIGUOUS prefix answers None too: two candidate receipts cannot be
    resolved to one attestation, and picking either would be a guess."""
    if tips is None:
        return None
    hits = [t for t in tips if t.startswith(tip)]
    return True if len(hits) == 1 else (False if not hits else None)



def _lr_recent_lands():
    """RECENT LANDS — DERIVED FROM TRUNK ON EVERY READ.

    THE INVERSION THIS REPLACES (owner: "why are the 'landed'
    items all so old and why aren't new ones showing up"). This card used to
    read `land-receipts.jsonl` — an append-only DURABILITY LOG whose only
    writer is the manual verb `helm lr land`. A log is written BEHIND the
    truth; it is never the thing a surface reads to LEARN the truth. So the
    card could only show a land that some agent had additionally remembered to
    witness, and the verb fails open, so forgetting was silent. Measured the
    morning this changed: trunk carried 55 folds since the newest receipt, and
    the card showed six rows, the newest sixteen hours old. That is the
    `log-as-bus` bug class, and every downstream symptom was a consequence of
    it — including the tempting cure of making agents better at remembering.

    SO THE ROWS COME FROM WHAT ACTUALLY LANDED. `git log <trunk> --grep ^fold:`
    is the integrator's own record of a landing, written by the merge itself,
    with no verb, no memory and no agent in the path. If it is on trunk, it is
    on this card. The receipt becomes a per-row badge (`witnessed`) instead of
    the gate on visibility, which is the job it can actually do.

    COST, MEASURED on this repo: the log is 4ms and the six proof
    ladders are 30ms, ~35ms in total. The alternative source — the ledger's own
    LANDED rows, which ride in this same body — needs `project_raw`, measured
    at 90.0s over 1078 rows on the same read. The console has flapped UNKNOWN
    on a 12s deadline before, so a card sourced from the projection would have
    traded a stale card for an absent one.

    Answers {"rows", "window_total", "window_s", "unavailable"} with its OWN
    unavailable. That independence is now total: this leg reads git, the
    pipeline reads the dispatch ledger, and the badge reads the receipt index —
    three failures that do not fold into each other. In particular a receipt
    index that cannot be read leaves every row present and every badge UNKNOWN.
    """
    from . import landreq
    out = {"rows": [], "window_total": None, "window_s": _LR_LANDS_WINDOW_S,
           "unavailable": None}
    try:
        gitdir, trunk = _lr_repo()
    except Exception as e:
        return dict(out, unavailable="the repository could not be identified "
                    "inside helm web (%s) — recent lands UNKNOWN"
                    % type(e).__name__)
    if not gitdir:
        return dict(out, unavailable="helm web is not running inside a git "
                    "repository, so there is no trunk to read lands from — "
                    "recent lands UNKNOWN")
    if not trunk:
        return dict(out, unavailable="this repository has no resolvable trunk "
                    "ref (no main/master, local or upstream) — recent lands "
                    "UNKNOWN")
    try:
        p = landreq._git(gitdir, "log", trunk, "--grep=^fold:",
                         "-n", str(_LR_LANDS_CAP), "--pretty=%H%x1f%ct%x1f%s")
        # The window total is a SECOND walk on purpose. A cap is a display
        # budget and may never edit a count (the same law the closed footer
        # learned), so "showing 6" and "76 landed today" are measured
        # separately rather than one being inferred from the other.
        q = landreq._git(gitdir, "log", trunk, "--grep=^fold:", "--pretty=%H",
                         "--since=%d seconds ago" % _LR_LANDS_WINDOW_S)
    except Exception as e:
        return dict(out, unavailable="reading lands off %s failed inside helm "
                    "web (%s) — recent lands UNKNOWN" % (trunk, type(e).__name__))
    if p is None or p.returncode != 0:
        return dict(out, unavailable="git could not read %s, so what has "
                    "landed is UNKNOWN — not zero lands" % trunk)
    # THE BADGE READ IS ALLOWED TO FAIL ALONE. A failure marker's keys are
    # module-private objects, so the rows are taken only past `rfail`; None
    # then travels to every badge as UNKNOWN.
    rrows, rfail = landreq._receipt_rows()
    tips = None if rfail else {str(r.get("id") or "") for r in rrows
                               if isinstance(r, dict)
                               and r.get("topic") == "helm.land"}
    rows = []
    for line in p.stdout.splitlines():
        parts = line.split("\x1f", 2)
        if len(parts) != 3:
            continue
        fold_sha, ct, subject = parts
        m = _LR_FOLD.match(subject)
        if not m:
            continue
        tip = m.group("tip")
        proof = landreq._landing_proof(gitdir, tip, trunk)
        rows.append({
            "lane": m.group("lane"), "reviewed_tip": tip,
            # THE COMMIT THAT CARRIES THE LAND. Not `trunk_sha`: that field
            # meant "the local trunk a receipt WROTE DOWN at land time", which
            # is provenance about a writer. This is the trunk commit itself,
            # and it is why the row exists at all.
            "fold_sha": fold_sha, "trunk_sha": None,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                time.gmtime(int(ct))) if ct.isdigit() else None,
            "trunk_ref": trunk,
            # THE SAME LADDER, ASKING THE SAME QUESTION IT ALWAYS DID: is the
            # REVIEWED CONTENT on trunk now? That is a separate fact from "did
            # this land", which the fold already settles, and it stays worth
            # rendering — a fold whose reviewed tip is absent is a real finding.
            "on_trunk": True if proof in ("ancestor", "patch-equivalent")
            else (False if proof == "absent" else None),
            "how": proof if proof in ("ancestor", "patch-equivalent") else None,
            "witnessed": _lr_witnessed(tip, tips),
            # The receipt-rescue trio has no meaning for a git-derived row:
            # there is no receipt-stored patch-id here to correlate WITH. Sent
            # as None rather than omitted so the renderer's arms stay total.
            "correlates_to": None, "correlation_why": None, "rescue_why": None,
            # ONE FOLD IS ONE LAND. The rounds of a chain do not each mint a
            # fold — only the landing does — so the round-collapsing the
            # receipt-fed card needed is structurally unnecessary here, and
            # asking for it would be re-deriving a grouping the source already
            # performed. No chain_root means the renderer keys each row on its
            # own position and can never merge two lands into one.
            "chain_root": None,
        })
    if q is not None and q.returncode == 0:
        out["window_total"] = len(q.stdout.split())
    return dict(out, rows=rows)



def _lr_native_chain():
    """The local attest-chain summary for the dashboard's RECORD row: how many
    records, whether the whole chain VERIFIES, and the head index. A summary of
    /api/ledger/native's chain leg rather than a second poll of it — the
    dashboard reads /api/lr anyway, and verify_chain() over ~100 records is
    hashing, not git. Failure is a NAMED unavailable, never {count: 0}: an
    unreadable chain and an empty one are different facts."""
    from . import premise
    try:
        recs = premise.chain_records()
        verified, detail = premise.verify_chain() if recs else (True, "")
        head = recs[-1] if recs else {}
        return {"count": len(recs), "verified": bool(verified),
                "detail": "" if verified else str(detail),
                "head_index": head.get("chain_index"), "unavailable": None}
    except Exception as e:
        return {"count": None, "verified": None, "detail": "",
                "head_index": None,
                "unavailable": "the local chain read failed (%s) — chain "
                               "UNKNOWN" % type(e).__name__}
del _web
