"""`helm lr` -- the VERB SURFACE of the land ledger, and the sentences it prints.

WHAT `lr` DOES AT THE COMMAND LINE, NOT WHAT THE LEDGER KNOWS. Every verb
`cli.py` reaches lives here, together with the row renderer, the one-line
formatter and the card the verbs print; the ledger's own questions -- what
landed, what a tip proves, what a chain says -- stayed behind in `landreq`.
Measured at the cut, the remainder reaches into this module through exactly
ONE name, which is what makes this the honest side to move: it is a leaf
CONSUMER of the ledger API.

THE CODE MOVED WHOLE, AND EVERY LEDGER NAME IS SPELLED `landreq.NAME` AT ITS
CALL SITE. An import list would bind each object once at import, so a test
that patches an attribute on the ledger and then drives a verb would reach
the original here and measure nothing -- a whole family of structural guards
stays green while the arms that matter quietly stop testing anything. The
module spelling keeps the lookup at CALL TIME, exactly as a bare global did
inside the ledger. Names this module OWNS are spelled that way too, because
they are published back onto `landreq` and arms patch them there.

THE CYCLE IS BROKEN THE WAY THIS PACKAGE ALREADY BREAKS IT: this module
imports the ledger EAGERLY, and `landreq` imports this one at the END of its
own module body, after every name it needs exists. A module object is in
`sys.modules` from the first line of its execution, so either import order
resolves.
"""
import calendar
import contextlib
import functools
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata

from . import landreq
from . import dispatches, foldcheck, foldcompose, home, pk, projscope, query, vcs
from .seats_common import recipient_matches
from .store import load as store_load
from .work import _lanes


def card(lr):
    """One land loop projected for a CONSOLE row — the shape `board_section`
    and `GET /api/lr` both render. PROJECTION ONLY: every field is copied from
    the row `_lr` already built; nothing here recomputes, joins or judges.

    Four of these fields exist because a panel that omits them cannot help
    reading as healthier than the record is:

    - `polarity` / `polarity_source` — REVIEWED with polarity None is "not
      stall-checked", not a quiet nearly-approved row. A declared held APPROVE
      stays distinct, and the source field names the dispatch store as owner.
    - `attest_state` / `attest_detail` / `attest_source` — the independent
      sidecar axis. A binding mismatch is durable evidence to repair, never a
      reason to rewrite polarity or retroactively re-sign a verdict.
    - `contrary_state` — WHICH physical fact contradicts the verdict (landed
      vs merged-local). Without it a console can say "contrary" and not say
      what happened; `_line` prints exactly this distinction already.
    - `review_sha_full` / `entered_ts` — the 12-char clip is for the row, but
      a reader who wants to check the claim needs the whole sha, and the
      terminal window ("closed in the last 24h") needs a real stamp rather
      than a dwell that keeps counting.
    - `closed_ts` / `closed_ts_unreadable` / `dwell_known` — the "do we
      actually know WHEN" bits. `closed_ts` is the CLOSURE instant and is
      None for a git-observed landing, which carries no stamp; a window that
      dates closure from `entered_ts` instead drops a lane that merged a
      minute ago after an old verdict. `closed_ts_unreadable` keeps the third
      state distinct: the record HAS a closure stamp and it is not a
      timestamp, which is a repair to make, not an absence to shrug at.
      `closed_ts_impossible` keeps the FOURTH: a closure stamp that parses
      perfectly and is dated in the future, which is not an instant this row
      can have closed at.
    - `ledger_refused` — the transitions the record holds that the canonical
      fold REFUSED (an out-of-sequence verdict, a delivery on a row that never
      opened). Empty on every healthy row. It travels because the alternative
      is dropping the event silently, and a timeline quietly missing a verdict
      the ledger contains is the same disagreement in the other direction.
      `dwell_known` is False when the entry stamp could not be read, i.e.
      when `dwell_s` is 0 because nothing was measured rather than because
      the row is new.
    - `gate` / `ungated` — the VERIFICATION axis, read tolerantly with
      `.get`: they do not exist until the gate lane lands, and a row without
      them must render UNVERIFIED. An absent verification field may never
      default to verified, which is why neither has a truthy fallback.
    """
    # ONE RESOLVE PER CARD. `ball_holder` is the two-field half of this
    # same answer; asking it here and asking the standing again below
    # would read the roster twice per row to answer one question, and
    # the two reads could disagree across a roster write.
    holder_role, holder_seat, holder_standing, holder_standing_why = \
        landreq.owed_seat_standing(lr)
    out = {"id": lr["id"], "state": lr["state"], "lane": lr["lane"],
            "branch": lr["branch"], "review_sha": (lr["review_sha"] or "")[:12],
            "review_sha_full": lr["review_sha"] or "",
            # WORK IDENTITY RIDES THE WIRE, because the surface that draws
            # these rows had only the LANE to relate them by and a lane is a
            # renamable label, not identity — this file's own span comment
            # says so at length ("THE COUNT IS OVER WORK IDENTITY, NEVER THE
            # LANE LABEL", task/734). Without `chain_root` the console could
            # only choose between drawing every row separately (what the owner
            # met: "the list you print on the webUI makes them look all
            # separate") and collapsing on the label, which is the defect that
            # comment records — two unrelated chains sharing a name would
            # merge into one. Copied, never recomputed.
            #
            # `supersedes` travels with it for one narrow job: picking WHICH
            # row of a chain is the current one. A round cited by a sibling's
            # `supersedes` is an EARLIER round of the same work, and that is
            # a declared relation in the record rather than a guess from
            # dwell order.
            #
            # Both are read tolerantly downstream: an absent `chain_root`
            # falls back to the row's own id ("a root names itself", the seal
            # `_append_dispatch` writes), which can only ever SPLIT a group,
            # never merge two pieces of work that are not one.
            "chain_root": lr.get("chain_root"),
            "supersedes": lr.get("supersedes"),
            "base_sha": lr.get("base_sha") or "",
            "author": lr["author"], "reviewer": lr["reviewer"],
            "kind": lr.get("kind"), "polarity": lr.get("polarity"),
            "polarity_source": lr.get("polarity_source"),
            "attest_source": lr.get("attest_source"),
            "attest_state": lr.get("attest_state"),
            "attest_detail": lr.get("attest_detail"),
            "dwell_s": lr["dwell_s"], "entered_ts": lr.get("entered_ts"),
            "dwell_known": lr.get("dwell_known", False),
            "closed_ts": lr.get("closed_ts"),
            "closed_ts_unreadable": lr.get("closed_ts_unreadable", False),
            "closed_ts_impossible": lr.get("closed_ts_impossible", False),
            "ledger_refused": lr.get("ledger_refused") or [],
            "stalled": lr["stalled"],
            # TERMINAL RIDES THE WIRE, because the header that reads it is the
            # exact place these two surfaces have already disagreed once. This
            # file's own list header records the incident: the browser
            # partitioned honored rows OUT of its in-flight count while the
            # CLI counted them IN, so "one word still named two numbers", and
            # the cure was ONE PREDICATE — `inflight_rows`, which reads
            # `terminal`.
            #
            # Measured 2026-08-07 while wiring the CLI's warm read (task/444):
            # `inflight_rows(cards)` raised KeyError 'terminal' because this
            # projection dropped it. The alternative — recomputing in-flight
            # on the warm side from `state` — would rebuild that defect
            # exactly, so the missing datum is supplied at the PRODUCER and
            # both transports keep asking the same predicate.
            "terminal": lr["terminal"],
            # THE RETIREMENT RIDES THE WIRE. `/api/lr` serves exactly this
            # shape, and a console that can render a terminal row but cannot
            # say helm gave up on it — on what measurement, by whom — reads
            # as a row that resolved. Copied, never recomputed.
            "retired_admin": lr.get("retired_admin", False),
            "retire_reason": lr.get("retire_reason"),
            "retire_measurement": lr.get("retire_measurement"),
            "retire_seat": lr.get("retire_seat"),
            "retire_ts": lr.get("retire_ts"),
            "observable": lr["observable"],
            "land_state": lr.get("land_state"),
            "landed": lr["landed"], "merged_local": lr["merged_local"],
            "has_upstream": lr.get("has_upstream", False),
            # ALREADY ON TRUNK WITH NO VERDICT RIDES THE WIRE. `lr list`
            # prints it for a pre-verdict row whose pinned tip trunk already
            # contains (`_annotate_trunk_containment`), and the owner's kanban
            # drew the same row as waiting for review because this card
            # dropped the field. Tri-state and copied: None is "not asked".
            "trunk_contains_tip": lr.get("trunk_contains_tip"),
            "trunk_contains_proof": lr.get("trunk_contains_proof"),
            # PROJECT-SCOPE MARKS RIDE THE WIRE (task/974): the escape view
            # renders foreign rows and a row without its label is a row the
            # reader mistakes for their own — the exact confusion the scope
            # exists to end. Read tolerantly; absent means "this board's own".
            "foreign": lr.get("foreign", False),
            "foreign_repo": lr.get("foreign_repo"),
            "foreign_project": lr.get("foreign_project"),
            "origin_unknown": lr.get("origin_unknown", False),
            "project_unresolved": lr.get("project_unresolved", False),
            "contrary": lr.get("contrary", False),
            "contrary_state": lr.get("contrary_state"),
            # THE SEAT STANDING RIDES THE WIRE for the reason the note below
            # states about the discharge stamp and the r1 FIX: this
            # card IS the shape `/api/lr` serves, so a fact the projection
            # computes and the card drops cannot reach the owner's console no
            # matter how right the classifier is. ORPHANED is the one the
            # console needs — a row owed by nobody that still reads as debt.
            "owed_seat_standing": landreq._card_standing(holder_standing,
                                                 holder_standing_why),
            # HOW THE STATE IS KNOWN, and it rides the wire for the same reason
            # the discharge stamp below does: this card is the exact shape
            # `/api/lr` serves, so a field the projection computes and the card
            # drops is invisible to the owner's console no matter how correct
            # the classifier is. The r1 FIX was for precisely
            # this — the field existed and no operator surface read it.
            # "observed" = git saw the landing; "recorded" = a string persisted
            # at close, which git can no longer be consulted about; None = no
            # contrary state at all. Copied, never recomputed — the projection
            # owns the branch precedence and a second derivation here is how the
            # card and `lr list` would come to disagree about one row.
            "contrary_provenance": lr.get("contrary_provenance"),
            # DISPLAY TRUTH RIDES THE WIRE TOO. `_annotate_contrary_discharge`
            # stamps whether an on-trunk APPROVE in this row's chain HONORED
            # the verdict through succession ("a"/"b") or the row IS the
            # resolved door's confirmation round ("c" — the discharge
            # instrument, rendered as a quiet "confirmation"), and `lr list`
            # renders it — but this card is the exact shape `/api/lr` serves,
            # and omitting the stamp made the owner's console count five
            # honored rows as live contrary debt (measured 2026-08-04: 11
            # shown, 6 real). Copied, never recomputed; None means "not
            # stamped" and every renderer stays LOUD on it — fail-closed,
            # like `gate`.
            "contrary_discharge": lr.get("contrary_discharge"),
            # The web stall mark consumes the SAME stamped succession result as
            # the CLI; carrying only its reason made the state unreachable.
            "succession_state": lr.get("succession_state"),
            "succession_unknown_reason": lr.get("succession_unknown_reason"),
            # THE CLASSIFICATION ITSELF, not just its inputs. The browser used
            # to re-derive this from the two fields above through a JS twin of
            # honored_display, and parity tests pinned the twin equal row for
            # row. Measured 2026-08-05 they still agreed — 276/276 over the
            # whole population — so this is DEDUPLICATION, not a bug fix, and
            # it changes no rendered row. Two implementations of one predicate
            # that agree today are a drift waiting to happen, and only one of
            # them feeds the owner's screen. The server already knows; send it.
            "honored": landreq.honored_display(lr),
            "discharged": lr.get("discharged", False),
            "superseding_tip": lr.get("superseding_tip"),
            "withdrawn": lr.get("withdrawn", False),
            "withdraw_contradicted": lr.get("withdraw_contradicted", False),
            "abandoned": lr.get("abandoned", False),
            "abandon_reason": lr.get("abandon_reason"),
            "abandon_ts": lr.get("abandon_ts"),
            "abandon_object_state": lr.get("abandon_object_state"),
            "abandon_proof_mode": lr.get("abandon_proof_mode"),
            "abandon_proof_version": lr.get("abandon_proof_version"),
            "abandon_trunk_mention_state":
            lr.get("abandon_trunk_mention_state"),
            "abandon_trunk_mention_proof_mode":
            lr.get("abandon_trunk_mention_proof_mode"),
            "abandon_trunk_mention_proof_version":
            lr.get("abandon_trunk_mention_proof_version"),
            "abandon_branch_state": lr.get("abandon_branch_state"),
            "abandon_branch_proof_mode": lr.get("abandon_branch_proof_mode"),
            "abandon_branch_proof_version": lr.get("abandon_branch_proof_version"),
            "abandon_worktree_state": lr.get("abandon_worktree_state"),
            "abandon_worktree_proof_mode": lr.get("abandon_worktree_proof_mode"),
            "abandon_worktree_proof_version":
            lr.get("abandon_worktree_proof_version"),
            "abandon_land_state": lr.get("abandon_land_state"),
            "close_reason": lr.get("close_reason"),
            # the VALIDATED closure instant only — the raw close stamp goes
            # through the same absent/unreadable/impossible typing as every
            # other closure stamp, and an unvalidated instant never reaches
            # the wire (the 2099 lesson, once per stamp position)
            "close_ts": lr.get("closed_ts") if lr.get("close_reason")
            else None,
            "close_contradicted": lr.get("close_contradicted", False),
            "close_proof_mode": lr.get("close_proof_mode"),
            # the LIVE step rides the wire too: a console that renders landed
            # rows must be able to say which of them the fleet actually has
            "close_delivery_class": lr.get("close_delivery_class"),
            "close_delivery_restart": lr.get("close_delivery_restart"),
            "close_evidence": lr.get("close_evidence"),
            "confirmation_id": lr.get("confirmation_id"),
            "confirmation_tip": lr.get("confirmation_tip"),
            "confirmation_ref": lr.get("confirmation_ref"),
            "original_author": lr.get("original_author"),
            "confirmation_recipient": lr.get("confirmation_recipient"),
            "original_author_family": lr.get("original_author_family"),
            "confirmation_recipient_family":
            lr.get("confirmation_recipient_family"),
            "original_verdict_anchor": lr.get("original_verdict_anchor"),
            "confirmation_verdict_anchor":
            lr.get("confirmation_verdict_anchor"),
            "original_family_evidence": lr.get("original_family_evidence"),
            "confirmation_family_evidence":
            lr.get("confirmation_family_evidence"),
            "original_family_anchor": lr.get("original_family_anchor"),
            "confirmation_family_anchor":
            lr.get("confirmation_family_anchor"),
            "confirmation_tier_state": lr.get("confirmation_tier_state"),
            "confirmation_gate_requirement":
            lr.get("confirmation_gate_requirement"),
            "confirmation_gate": lr.get("confirmation_gate"),
            "confirmation_approval_anchor":
            lr.get("confirmation_approval_anchor"),
            "original_proof_mode": lr.get("original_proof_mode"),
            "closing_repo_id": lr.get("closing_repo_id"),
            "closing_trunk_ref": lr.get("closing_trunk_ref"),
            "closing_trunk_sha": lr.get("closing_trunk_sha"),
            "landing_review_id": lr.get("landing_review_id"),
            "landing_review_tip": lr.get("landing_review_tip"),
            "landing_review_verdict_anchor":
            lr.get("landing_review_verdict_anchor"),
            "landing_review_tier_state": lr.get("landing_review_tier_state"),
            "landing_review_gate_requirement":
            lr.get("landing_review_gate_requirement"),
            "landing_review_gate": lr.get("landing_review_gate"),
            "landing_review_approval_anchor":
            lr.get("landing_review_approval_anchor"),
            "closed_by_landing": lr.get("closed_by_landing", False),
            "landing_trunk_sha": lr.get("landing_trunk_sha"),
            # TWO ANSWERS, because they answer two different questions:
            # owed_by is enforcement/audit truth and stays untouched; the
            # holder pair is the server-resolved display answer after
            # succession annotation. HOME formats this pair, never discharge.
            "owed_by": lr["owed_by"],
            "owner_gated": bool(lr.get("owner_gated")),
            "hold_ts": lr.get("hold_ts"),
            "hold_reason": lr.get("hold_reason"),
            "holder_role": holder_role,
            "holder_seat": holder_seat,
            # COPIED, NEVER INVENTED. This defaulted to R_NONE, which is the
            # third place on this one card where a receipt state helm does not
            # have was rendered as a receipt helm looked for and did not find —
            # a projection is not entitled to answer a question its input never
            # answered. A row that reaches here without the field sends None,
            # and the renderers say the state was not sent.
            "receipt_state": lr.get("receipt_state"),
            "timeline": lr.get("timeline") or [],
            "gate": lr.get("gate") or "",
            "ungated": lr.get("ungated"),
            "tier": lr.get("tier"), "tier_kind": lr.get("tier_kind")}
    if lr.get("close_reason") == "delivered-report":
        out.update(artifact_ref=lr.get("artifact_ref"),
                   report_ref=lr.get("report_ref"),
                   delivered_report_correction=lr.get(
                       "delivered_report_correction", False),
                   cancel_reason=lr.get("cancel_reason"))
    return out

def _fold_proven(root, tip, report, gate_ref):
    """`helm lr foldcheck` once all five rungs PASSED: the composition-proof
    stage, which `docs/VERBS.md` keeps SEPARATE from the five. -> rc."""
    project_state, project = foldcompose.project_state(root)
    if project_state == "unregistered":
        for line in report:
            print(line)
        return 0                       # no project can have been activated
    if project_state != "registered":
        for line in report[:-1]:
            print(line)
        print("all five PASSED — composition authority is UNKNOWN")
        print("\n????  composition-proof                project registry "
              "could not be read — UNKNOWN is not consent")
        return 1
    proof = foldcompose.composition_proof(root, project, tip, gate_ref=gate_ref)
    if proof is None:
        for line in report:
            print(line)
        return 0                       # absence of a pre-activation stage
    for line in report[:-1]:
        print(line)
    print("all five PASSED — active composition proof follows")
    print("\ncomposition proof:")
    print(foldcompose.render(proof))
    if not all(ok is True for _n, ok, _d in proof):
        return 1
    print("composition proof PASSED — this fold is provable")
    return 0


def _print_landed_leases(root):
    """After a fold, the leases the trunk now carries — the land releases
    none of them, so this names each one and the exact line that does.

    ADVISORY AND AFTER THE VERDICT: it never changes foldcheck's exit code,
    and a read that fails says so in one line rather than taking the fold's
    answer down with it. Silent when there is nothing to say."""
    from . import work
    try:
        release, kept = work.landed_leases(root)
    except Exception as exc:            # noqa: BLE001 — named, never silent
        print("\nleases on landed work: UNKNOWN — the lane rooms could not "
              "be read (%s)" % type(exc).__name__)
        return
    if release:
        print("\nLEASES ON LANDED WORK — a land releases no lease; each "
              "holder runs its line:")
        for r in release:
            print("  %s (%s) — %s: %s" % (
                r["lane"], "yours" if r.get("lease") else r.get("holder"),
                r["landed"]["proof"], work.release_command(r)))
    if kept:
        print("\nKEPT — this trunk carries their work, and releasing now "
              "would lose some:")
        for r, why in kept:
            print("  %s (%s) — %s" % (r["lane"], r.get("holder"), why))


def _card_standing(state, why):
    """The seat-standing pair the wire carries, or None when there is nothing
    to say. Takes the ALREADY-RESOLVED answer rather than the row, so a card
    cannot read the roster a second time and disagree with its own
    `holder_seat`. A card field that is present-and-null and a card field that
    is absent must not mean two different things to the browser, so UNKNOWN
    and the holder-less roles are absent rather than an empty banner."""
    return {"state": state, "why": why} if state in ("ORPHANED",
                                                     "RENAMED") else None

def _line(lr, avail=None):
    marks = []
    # PROJECT PROVENANCE FIRST (task/974): these rows reach a listing only
    # through `--all` / `--all-projects`, and an unlabeled foreign row is the
    # exact confusion the scope exists to end — a reader billing another
    # project's row as their own.
    if lr.get("foreign"):
        marks.append("FOREIGN — project %s owns this row (%s)"
                     % (lr.get("foreign_project") or "?",
                        lr.get("foreign_repo") or "repo unrecorded"))
    elif lr.get("project_unresolved"):
        marks.append("project UNRESOLVED — %s is registered to no project"
                     % (lr.get("foreign_repo") or "its repo"))
    elif lr.get("origin_unknown"):
        marks.append("origin UNKNOWN — no repo recorded on the create event")
    if lr.get("abandoned"):
        marks.append("ABANDONED — LAND STATE UNKNOWN: %s" %
                     (lr.get("abandon_reason") or "reason unavailable"))
    # THE REVIEW IS OVER AND THE STAGE CANNOT SAY SO. A source-clean hold is a
    # structured claim that the reviewer read the delta and found nothing, and
    # could not mint an approve only because one binds a whole-suite token the
    # integrator's land gate produces. The STATE column still reads
    # AWAITING_REVIEW — correctly, the ledger has no verdict — so without this
    # mark the row renders as a slow reviewer with a growing dwell and nothing
    # contradicting it. Suppressing the STALLED word alone would remove the
    # accusation and leave the reader to supply it themselves.
    if lr.get("source_clean_tip"):
        marks.append("SOURCE-CLEAN at %s — the review is COMPLETE; this waits "
                     "on the INTEGRATOR's whole-suite gate, not on its reviewer"
                     % str(lr["source_clean_tip"])[:12])
    # THE WORK IS ALREADY IN HISTORY AND THE ROW IS STILL BILLING SOMEBODY.
    # A row can pin a tip that trunk already contains, with zero commits ahead,
    # while every other field on it renders as ordinary owed work. Nothing else
    # on the board contradicts that, so what stands between such a row and a
    # whole-suite gate spent on a tree already in history is somebody checking
    # a commit count by hand.
    #
    # IT IS NOT A STALL AND IT IS NOT A REBASE DEBT, which is why it gets its
    # own word rather than a quieter `STALLED`. A stall accuses the reviewer of
    # being slow; a distance accuses the author of owing a rebase (task/2677,
    # and an ancestor of trunk is also BEHIND it, usually far, so that integer
    # alone would bill the author for rebasing work that already landed). What
    # is actually missing is a VERDICT: the change reached trunk without one,
    # and the close door is right to refuse to stamp it landed, because git
    # cannot prove a review happened. The row IS the finding.
    if lr.get("trunk_contains_tip"):
        marks.append("ALREADY ON TRUNK — this work is in history and the row "
                     "has NO VERDICT recorded; it is a LEDGER gap (a review "
                     "that never happened), not a slow reviewer and not a "
                     "rebase its author owes")
    # A DISTANCE IS NOT AN AGE, AND THEY ACCUSE DIFFERENT PEOPLE. `STALLED` is
    # how long the row has sat and reads as a slow reviewer. BEHIND is how far
    # its pinned tip is from trunk and says the row cannot land whoever
    # reviews it and however green its gate: the land door is ff-only and
    # foldcheck refuses. A whole-suite gate spent on a stale base does not
    # fail, it CONGRATULATES you, which is why the number has to be on the row
    # rather than discovered by the next person who thinks to check.
    #
    # BOTH WORDS, NEVER ONE INSTEAD OF THE OTHER. The age is still true and
    # still rendered; this adds the second sentence the reader needs to route
    # the row to its author instead of its reviewer. It deliberately does NOT
    # clear `stalled` -- unlike an already-landed row, a behind row still has
    # somebody who can act, and silencing the age would trade one wrong
    # accusation for a missing one.
    # AND ONLY WHEN CONTAINMENT CAME BACK A PROVEN NO. `trunk_contains_tip` is
    # tri-state and None means the question was not answered -- which is a
    # REACHABLE state here, not a theoretical one: the containment walk reaches
    # the typed oracle only after the shared derive budget may already be
    # spent, and past it that oracle answers UNKNOWN.
    #
    # THE CASE THAT MAKES THIS A DEFECT RATHER THAN A ROUNDING IS A REBASED
    # LAND, and the two instruments genuinely disagree about it by design:
    # measured on one real row, ancestry answers NOT_ANCESTOR while patch
    # identity answers PATCH_EQUIVALENT. So a lane whose content IS on trunk
    # under new object ids is NOT_ANCESTOR, is therefore measured for distance,
    # and would render "rebase this" to the author of work that already
    # landed -- the exact wrong accusation this whole leg exists to stop,
    # reintroduced one branch below where it was cured.
    #
    # A DISTANCE IS ONLY MEANINGFUL ONCE "IT ALREADY LANDED" HAS BEEN RULED
    # OUT. Unknown is not ruled out, so it renders nothing: silence is honest
    # where an accusation is not.
    elif lr.get("trunk_contains_tip") is False and (
            lr.get("tip_behind_trunk") or lr.get("base_behind")):
        marks.append("%d BEHIND trunk — this cannot land until its author "
                     "rebases, whoever reviews it and however green its gate"
                     % int(lr.get("tip_behind_trunk")
                           or lr.get("base_behind")))
    # THE SEAT THIS ROW NAMES, asked once per row and not cached. `roster_
    # checked` is one small JSON read; this listing already spends a
    # merge-base three-dot per row in `_moved_tip_marker`, so the read is
    # noise beside what is already here — and a module-level memo of roster
    # state would be a durable cross-call channel serving a stale answer to
    # exactly the surface whose job is to be current.
    # THE NAME COMES BACK FROM THE RESOLVER, never re-derived from the row.
    # On ORPHANED the pair still carries the ORIGINAL string precisely so this
    # mark can print it; reaching into `_OWED_SEAT_FIELD` a second time here
    # would be transcribing a lookup that has already been done, and the two
    # copies drift the moment the role vocabulary gains an entry.
    _role, owed_seat, standing, standing_why = landreq.owed_seat_standing(lr)
    # THE VENDOR WORD ABOUT THE SEAT THIS ROW ALREADY NAMES, taken off the
    # resolution just made rather than re-derived: the owed seat is the one a
    # reader is deciding about, whether they are chasing this row or picking a
    # reviewer for the next one. UNAVAILABLE prints; UNKNOWN does NOT, because
    # an unreadable proxywatch record would mark every row on the board and a
    # mark every row carries is a mark nobody reads — and it must never be
    # confused with the wall, which is the whole point of the two values being
    # distinct. A wall needs no relaunch: the next green proxywatch pass clears
    # it, so the mark says what will fix it and what will not.
    vendor = (avail(owed_seat) if avail else None) or {}
    if landreq._avail_walled(vendor) and vendor.get("text"):
        marks.append("VENDOR %s — @%s's family is not answering usably, so "
                     "routing or chasing this row here lands in a hole. The "
                     "state names WHAT was observed and not WHERE it came "
                     "from, so no cause is asserted. No relaunch cures it: the "
                     "next green proxywatch pass (15m timer) flips it back on "
                     "its own"
                     % (vendor["text"], owed_seat))
    if standing == "ORPHANED":
        marks.append("ORPHANED — %s. The work is NOT reassigned and this row "
                     "is NOT expired; `helm seat reassign %s --to <seat> "
                     "--apply` moves it"
                     % (standing_why or "no roster row answers to its holder",
                        owed_seat))
    elif standing == "RENAMED":
        marks.append("RENAMED — %s" % standing_why)
    if lr.get("closed_by_landing"):
        marks.append("UNDECLARED — CLOSED BY LANDING via %s" %
                     (lr.get("landing_proof_mode") or "proof"))
    if lr.get("close_reason"):
        mark = "CLOSED (%s)" % landreq._close_label(lr)
        if lr["close_reason"] == "superseded":
            mark += " by %s" % (lr.get("superseding_tip") or "-")[:12]
        marks.append(mark)
    # The LIVE step rides beside the terminal, on the compact row too: a
    # process-class land that nothing has re-armed is an OPEN LOOP, and a loop
    # nobody prints is a loop nobody closes.
    phrase = landreq._delivery_phrase(lr)
    if phrase:
        marks.append(phrase)
    if lr.get("close_contradicted"):
        # THE CONTRADICTION AND THE DEBT ARE TWO FACTS, and admitting
        # APPROVE and CONCUR to `withdrawn` pulled them apart. A withdrawn
        # FIX or SUPERSEDE whose work later landed owes the integrator an
        # answer. A withdrawn APPROVE or CONCUR whose work later landed is
        # corrected by the OBSERVED PHYSICAL LANDING alone — the row reaches
        # state LANDED, terminal, owing NOBODY. It is deliberately not put
        # that the verdict authorized the outcome: an APPROVE does authorize,
        # a CONCUR authorizes nothing at all, and only the landing is common
        # to both. One hardcoded sentence billed all four, so half the
        # reopened rows advertised a debt the ledger does not hold.
        #
        # `_owed_by_whom` is the DISPLAY authority and it routes through
        # `ball_holder`, which carries precedence raw `owed_by` does not: a
        # contrary row already honored through succession has no action left
        # for a human, and reads "nobody" while the enforcement field still
        # records integrator debt. Rendering the raw field here would revive
        # that debt on exactly those rows.
        marks.append("close CONTRADICTED — the withdrawn change later "
                     "landed; owed by %s" % landreq._owed_by_whom(lr))
    if lr.get("withdrawn"):
        # The verdict stays visible; WITHDRAWN names the resolution, not a
        # rewrite. A withdrawn row is terminal, so no STALLED mark follows it.
        marks.append("%s — WITHDRAWN, lane abandoned in favour of the verdict"
                     % (lr.get("polarity") or "fix").upper())
    # NOT A BLOCK, AND THE LIST LINE IS WHERE AN AUTHOR SEES IT FIRST. A FIX
    # answering IMPERFECT names no regressing path: it carries the reader's
    # committed cure, so the only move left is the author reading that patch.
    # Without this the row reads CHANGES_REQUESTED and the author goes looking
    # for a defect in a tip the reviewer already found no worse than main.
    if lr.get("exit_answer") == "imperfect" and lr.get("patch_tip"):
        marks.append("AUTHOR AGREEMENT OWED ON THE PATCH %s by %s — IMPERFECT "
                     "is not a block on the reviewed tip; read the patch and "
                     "record an agreeing verdict on it"
                     % (str(lr["patch_tip"])[:12],
                        lr.get("patch_author") or lr.get("reviewer") or "?"))
    if lr.get("owner_gated"):
        # THE ROW SAYS WHOSE TURN IT IS. Excluding it from the stall count is
        # only half the cure: a row that simply stops alarming reads as a row
        # nobody is thinking about, and the owner's queue would go from
        # mislabelled to invisible. So it states the holder and quotes the
        # dependency in the same breath, and it comes BEFORE the stall block
        # because the two are mutually exclusive by construction.
        marks.append("WAITING ON THE OWNER — %s"
                     % (lr.get("hold_reason") or "no reason recorded"))
    # honored-first: the stalled ALARM yields to the honored banner (one
    # predicate, every surface) — the successor already landed, so there is
    # nothing to unstall. `stalled` itself is untouched.
    if lr["stalled"] and not landreq.honored_display(lr):
        # SUCCESSION OUTRANKS THE STALL ALARM, for the same reason honored does
        # one line up: a stall nobody can unstall is noise, not a debt
        # reminder. honored_display only ever answers for CONTRARY rows, so six
        # in-flight rows whose work was cured on a successor and APPROVED by a
        # second reviewer alarmed for up to five and a half days with nothing
        # able to notice. `stalled` itself is untouched — display only.
        state = lr.get("succession_state")
        if state == landreq.SUCCESSION_MOVED:
            pass                       # the chain carried it; nothing to unstall
        elif state == landreq.SUCCESSION_UNKNOWN:
            root = str(lr.get("chain_root") or "")
            why = lr.get("succession_unknown_reason") or (
                "no chain root; this row predates or escaped chain sealing"
                if not root or root == dispatches.CHAIN_UNKNOWN else
                "carrier relation or landing proof is unreadable")
            marks.append("STALLED — succession UNKNOWN (%s; whether a "
                         "successor carried it cannot be read)" % why)
        else:
            marks.append("STALLED")
    if lr.get("contrary"):
        # A CONTRARY BANNER IS A CLAIM THAT A VERDICT WAS DEFIED, and it was
        # shouting on rows whose verdict was HONORED THROUGH SUCCESSION — the
        # successor landed and the door closed the row, which is the process
        # WORKING. `contrary_discharge` names which arm discharged it; a
        # discharged row states the succession instead of the alarm, and an
        # UNVERIFIED pair says so rather than guessing in either direction.
        # The `contrary` FIELD is untouched — display only.
        discharge = lr.get("contrary_discharge")
        fact = landreq.contrary_fact(lr)
        if landreq.honored_display(lr):
            # "c" is its OWN kind, not a variant of honored wording: the row
            # is the resolved door's confirmation round, whose reviewed tip
            # is the landed cure BY DESIGN — the exact words lrMarks prints,
            # or the card and the terminal describe one row differently.
            mark = ("CONFIRMATION: %s by design — the resolution verified "
                    "on trunk; the discharge instrument, never a debt"
                    % fact) if discharge == "c" else \
                   ("SUPERSEDED-CLOSED: %s, and the %s verdict was HONORED "
                    "through succession (%s)" % (
                        fact, (lr.get("polarity") or "unknown").upper(),
                        "continuation" if discharge == "a" else "ladder discharge"))
            marks.append(mark + landreq.contrary_provenance_clause(lr))
            mark = None
        elif discharge == "unverified":
            reason = lr.get("succession_unknown_reason") or \
                "carrier relation or landing proof is unreadable"
            mark = ("CONTRARY? %s despite %s verdict — succession UNVERIFIED "
                    "(%s; no discharge was inferred)"
                    % (fact, (lr.get("polarity") or "unknown").upper(), reason))
        else:
            mark = "CONTRARY: %s despite %s verdict" % (
                fact, (lr.get("polarity") or "unknown").upper())
    if lr.get("contrary") and mark:
        if lr.get("discharged"):
            mark += "; DISCHARGED by %s" % (
                (lr.get("superseding_tip") or "-")[:12])
        if lr.get("consumed_by"):
            mark += "; CONSUMED as confirmation by %s" % (
                str(lr.get("consumed_by"))[:12])
        marks.append(mark + landreq.contrary_provenance_clause(lr))
    if lr["state"] == "REVIEWED" and not lr.get("closed_by_landing") \
            and not lr.get("close_reason"):
        # REVIEWED can be an undeclared historical verdict OR a declared
        # approval held by tier/gate policy. State alone cannot distinguish
        # them; polarity comes from the dispatch store and the hold reason is
        # already present on the projected row.
        source = dispatches._source_label(lr.get("polarity_source"))
        if lr.get("polarity"):
            marks.append("%s from %s — held: %s" % (
                lr["polarity"].upper(), source, landreq._held_reason(lr)))
        else:
            marks.append("polarity UNDECLARED in %s — not stall-checked" % source)
    if lr.get("attest_state"):
        marks.append("attest " + landreq._attest_text(lr))
    if lr.get("closed_ts_impossible"):
        marks.append("closure stamp IMPOSSIBLE — dated in the future")
    if lr.get("ledger_refused"):
        marks.append("dispatch fold REFUSED historical %s event; current fields "
                     "come from accepted dispatch state"
                     % "/".join(lr["ledger_refused"]))
    if lr.get("receipt_state") == landreq.R_UNREADABLE:
        marks.append("land receipt ledger UNREADABLE — not 'no receipt'")
    elif lr.get("receipt_state") in (landreq.R_REJECTED, landreq.R_CONFLICT):
        marks.append("land receipt %s — diagnostic only" % lr["receipt_state"])
    if not lr.get("dwell_known"):
        marks.append("dwell UNKNOWN — no usable instant to measure from or to")
    if lr["state"] in ("READY", "MERGED_LOCAL") and not lr["observable"]:
        marks.append("landing unobservable — "
                     + landreq.observe_reason(lr.get("observe_why")))
    if lr["state"] == "READY" and not lr.get("terminal"):
        # THE NAME BESIDE THE READY-SELF-REVIEW WORD. The word says nobody
        # independent looked; a board reader ordering a compose queue needs to
        # know WHICH seat, because the cure is to send the composed tip to a
        # seat that is not that one. Nothing is printed for an independent row
        # — a mark on every healthy row is a mark readers learn to skip.
        independent, why = landreq.independent_review(lr)
        if independent is not True:
            marks.append(why)
    if lr.get("base_state") == "UNLANDED-STALE":
        # The NUMBER beside the READY-STALE-BASE word: the word says a rung
        # bit, this says how hard — a 148 and a 1333 are different emergencies
        # and the integrator orders the compose queue by exactly this.
        marks.append("base %d commits behind trunk" % lr["base_behind"])
    mark = "  " + "  ".join("(%s)" % m for m in marks) if marks else ""
    # 17 = len("CHANGES_REQUESTED"), the longest current state.
    # An unreadable entry stamp prints "?" rather than the 0m `_age_s` invents
    # for it: the column is a measurement everywhere else on the board.
    # THE STATE WORD A READER SEES, not the one consumers key on. `ready_word`
    # composes READY-<RUNG> when a rung fails and returns every other state
    # untouched; `lr["state"]` itself is deliberately unchanged so STAGE_ORDER,
    # OWED_BY and LAND_STALL_S keep indexing "READY". Display truth only --
    # enforcement stays in the land door.
    # THE WIDE REPORT, AND IT WAS INVISIBLE UNTIL A REVIEW SAID SO — TWICE.
    # `ready_word` narrows the REFUSAL to a lane span <= 2 so a shared base
    # cannot gate a stranger — but I claimed the REPORT stayed wide, and no
    # renderer called `contest_report`, so a refusal on a busy tip was
    # reported to NOBODY. A narrowing that hides what it declines to gate is
    # not a narrowing, it is the silence this rung exists to end. `?N` marks
    # a tip other reviewers have refused on some other chain.
    #
    # BESIDE EVERY NONTERMINAL WORD, NOT ONLY PLAIN READY. Round three: my
    # first render gated the mark on `word == "READY"`, so the report
    # survived only on the healthiest rows: READY-STALE-BASE with a span-4
    # FIX — measured — rendered no `?1`, and CHANGES_REQUESTED hid a SECOND
    # chain's refusal behind the row's own. The obligation to show a refusal
    # does not lapse because another rung also bit; a tipless row answers
    # [], 0 before any read and the memoised index keeps this one dict
    # lookup per row.
    #
    # ROUND FOUR REVERSED MY TERMINAL CLAUSE (dispatch da34a297). I had
    # argued a terminal row keeps the mark — "a refusal beside LANDED is
    # history the reader may still want". A fab probe measured what
    # that mark actually is: the contest index is the CURRENT ledger, so a
    # FIX filed today re-words a row that landed last week — a live
    # annotation retroactively applied to closed history, never history
    # itself (54 terminal rows wore one the night it was measured). The
    # scoping lives in `contest_report` — the one door — so this render
    # inherits it: a terminal row answers [], 0 and its line stays a record.
    word = landreq.ready_word(lr)
    hits, _span, err = landreq.contest_report(lr)
    contest_mark = " ?%d" % len(hits) if hits and not err else ""
    return "  %-12s %-17s %-20s %7s  %s%s%s" % (
        lr["id"][:12], word, (lr["lane"] or "-")[:20],
        landreq._fmt_dwell(lr["dwell_s"]) if lr.get("dwell_known") else "?",
        (lr["review_sha"] or "-")[:12], mark, contest_mark)

def _render_show(lr):
    state = ("REVIEWED (UNDECLARED) — CLOSED BY LANDING"
             if lr.get("closed_by_landing") else lr["state"])
    if lr.get("close_reason") == "delivered-report":
        state = "BUILD — CLOSED (%s)" % landreq._close_label(lr)
    elif lr.get("landing_review_id") and not lr.get("close_contradicted"):
        state = "BUILD — CLOSED (LANDED via APPROVED REVIEW)"
    elif lr.get("close_reason") and not lr.get("close_contradicted"):
        # Exact per-polarity headline: the verdict-derived state (or the
        # UNDECLARED spelling) plus the terminal — never an approval the
        # ledger does not carry.
        base = "REVIEWED (UNDECLARED)" if lr.get("polarity") is None \
            else lr["state"]
        state = "%s — CLOSED (%s)" % (base, landreq._close_label(lr))
    elif landreq._retired_label(lr):
        # The verdict stays in the headline and the retirement is appended to
        # it — the same law `_line`'s WITHDRAWN mark states: the resolution
        # names how the debt ended, it does not rewrite what the reviewer said.
        state = "%s — RETIRED (%s)" % (lr["state"], landreq._retired_label(lr))
    out = ["LAND REQUEST %s   %s%s" % (
        lr["id"], state,
        # honored-first, same predicate as every other surface: the stalled
        # ALARM yields to the honored banner printed on the contrary line
        "  STALLED" if lr["stalled"] and not landreq.honored_display(lr) else "")]
    # DIRECTLY UNDER THE HEADLINE, because the headline is this binary's
    # reading and this line says where that reading stopped.
    unread = dispatches.unknown_kinds_note(lr)
    if unread:
        out.append("  ledger    %s — this row's state above is incomplete; "
                   "every writer refuses it until the trunk helm reads it"
                   % unread)
    recipient_role = "builder " if lr.get("kind") == "build" else "reviewer"
    out.append("  author    %s  ->  %s %s%s" % (
        lr["author"] or "-", recipient_role, lr["reviewer"] or "-",
        "  [model %s]" % lr["reviewer_model"] if lr.get("reviewer_model")
        else ""))
    out.append("  lane      %s" % (lr["lane"] or "-"))
    # THE CHAIN, where someone is already asking "what work is this".
    # `discharge` now REFUSES on chain mismatch and its refusal names a
    # `--supersedes <id>` to use; a reader sent here by that message must be
    # able to see the chain this row actually belongs to. Legacy rows print
    # LEGACY rather than a dash, because "written before chains existed" and
    # "value missing" are different facts.
    out.append("  chain     %s%s" % (
        lr.get("chain_root") or "LEGACY (pre-chain row; never retro-fitted)",
        "   continues %s" % lr["supersedes"] if lr.get("supersedes") else ""))
    out.append("  branch    %s" % (lr["branch"] or "-"))
    if lr.get("kind") == "build":
        out.append("  base      %s" % (lr.get("base_sha") or "-"))
    if lr.get("close_reason") == "delivered-report":
        out.append("  verdict   none — delivered-report claims no reviewed tip, "
                   "verdict polarity, Git landing, or trunk proof")
    else:
        out.append("  review    %s" % (lr["review_sha"] or "-"))
        out.append("  verdict   %s   reviewed %s" % (
            lr["verdict_ref"] or "(none)", lr["reviewed_tip"] or "-"))
        if lr.get("patch_tip"):
            out.append("  patch     %s by %s — the reviewer's own committed "
                       "cure off the reviewed tip; rebase this lane onto it or "
                       "cherry-pick it, and credit both authors"
                       % (str(lr["patch_tip"])[:12],
                          lr.get("patch_author") or lr.get("reviewer") or "-"))
        # THE EXIT ANSWER DECIDES WHAT IS OWED, and polarity cannot: both of
        # these rows read FIX. An IMPERFECT answer names no regressing path,
        # so the reviewed tip is not what is being objected to — the ONE move
        # left is the author reading the reviewer's patch and agreeing. A row
        # that reads as a block on the original tip sends its author back to
        # re-derive a defect the reviewer already cured.
        if lr.get("exit_answer") == "imperfect" and lr.get("patch_tip"):
            out.append("  exit      IMPERFECT — NOT a block on %s: the "
                       "reviewed tip is worse than main on no path it "
                       "touches. AUTHOR AGREEMENT OWED ON THE PATCH: read %s "
                       "and, if it is complete, record an agreeing verdict on "
                       "it."
                       % (str(lr.get("reviewed_tip") or "-")[:12],
                          str(lr["patch_tip"])[:12]))
        elif lr.get("no_patch_because"):
            out.append("  exit      no cure committed, because: %s"
                       % lr["no_patch_because"])
        out.append("  gate      %s" % (lr.get("gate") or "UNVERIFIED"))
    if lr.get("state") == "READY" and not lr.get("terminal"):
        # WHO READ THIS THAT DID NOT WRITE IT — the whole promise the review
        # procedure makes, said on the detail page the integrator opens before
        # a land. The word on the list line says a rung bit; this says which
        # seat wrote the chain it also approved, which is the part a reader
        # can act on (send the composed tip to a seat that wrote none of it).
        out.append("  outsider  %s" % landreq.independent_review(lr)[1])
        # Base drift, on the row detail the integrator reads before a land.
        # Three-way honest: a number (with the STALE verdict when it crosses
        # the bar), or UNMEASURED — a READY row whose drift helm could not
        # count must say so rather than render like a current one (task/266:
        # the zombies' every OTHER instrument reported health).
        #
        # NOT on a terminal row, even though a closed-as-landed row FREEZES
        # the verdict word READY: staleness is a property of WAITING work,
        # and a terminal row stops re-reading git by design — so it would
        # print "drift UNMEASURED" forever, an eternal shrug on a discharged
        # row (dogfood find: the receipt-reader-learns-v4 close in the live
        # ledger — CLOSED (LANDED), headline READY, shrugging).
        behind = lr.get("base_behind")
        if lr.get("base_state") == "UNLANDED-STALE":
            out.append("  drift     base is %d commits behind trunk — STALE "
                       "(>= %d): the compose leg re-proves this row against "
                       "all of them" % (behind, landreq.STALE_BASE_BEHIND))
        elif isinstance(behind, int):
            out.append("  drift     base is %d commit(s) behind trunk"
                       % behind)
        else:
            out.append("  drift     UNMEASURED — staleness unknown, never "
                       "assumed current")
    if lr.get("ungated") and lr.get("close_reason") != "delivered-report":
        # The whole reason this row is not READY, said where the integrator is
        # already looking for the reason.
        out.append("  NOT READY %s" % lr["ungated"])
    if lr.get("abandoned"):
        out.append("  terminal  ABANDONED — reviewed artifact unresolvable")
        out.append("  land      LAND STATE UNKNOWN — never inferred from object loss")
        out.append("  reason    %s" % (lr.get("abandon_reason") or "-"))
        out.append("  proof     %s v%s (%s)" % (
            lr.get("abandon_proof_mode") or "-",
            lr.get("abandon_proof_version") or "-",
            lr.get("abandon_object_state") or "-"))
        out.append("  interlock %s v%s (%s trunk mention)" % (
            lr.get("abandon_trunk_mention_proof_mode") or "-",
            lr.get("abandon_trunk_mention_proof_version") or "-",
            lr.get("abandon_trunk_mention_state") or "-"))
        out.append("  branch    %s via %s v%s" % (
            lr.get("abandon_branch_state") or "-",
            lr.get("abandon_branch_proof_mode") or "-",
            lr.get("abandon_branch_proof_version") or "-"))
        out.append("  worktree  %s via %s v%s" % (
            lr.get("abandon_worktree_state") or "-",
            lr.get("abandon_worktree_proof_mode") or "-",
            lr.get("abandon_worktree_proof_version") or "-"))
    if lr.get("closed_by_landing"):
        out.append("  polarity  UNDECLARED — immutable; closure records landing, "
                   "not approval")
        out.append("  closure   CLOSED BY LANDING via %s" %
                   (lr.get("landing_proof_mode") or "-"))
        out.append("  repository %s" % (lr.get("landing_repo_id") or "-"))
        out.append("  trunk     %s" % (lr.get("landing_trunk_ref") or "-"))
        out.append("  anchor    %s" % (lr.get("landing_trunk_sha") or "-"))
    if lr.get("close_reason"):
        reason = lr["close_reason"]
        if reason != "delivered-report" and lr.get("polarity") is None \
                and not lr.get("landing_review_id"):
            out.append("  polarity  UNDECLARED — immutable; closure records "
                       "%s, not approval" % reason)
        out.append("  closure   CLOSED (%s)%s%s" % (
            landreq._close_label(lr),
            "  at %s" % lr["close_ts"] if lr.get("close_ts") else "",
            "  — CONTRADICTED: the withdrawn change later landed"
            if lr.get("close_contradicted") else ""))
        if reason == "delivered-report":
            out.append("  artifact  %s" % (lr.get("artifact_ref") or "-"))
            out.append("  report    %s" % (lr.get("report_ref") or "-"))
            out.append("  evidence  %s" % (lr.get("close_evidence") or "-"))
            out.append("  identity  reference syntax validated; artifact/chat existence "
                       "not checked")
            out.append("  land      NOT CLAIMED — this terminal records handoff evidence, "
                       "not Git")
            if lr.get("delivered_report_correction"):
                out.append("  correction explicit annotation of preserved cancel: %s"
                           % (lr.get("cancel_reason") or "-"))
        elif reason == "landed":
            out.append("  live      %s" % landreq._delivery_phrase(lr))
            out.append("  repository %s" % (lr.get("closing_repo_id") or "-"))
            out.append("  trunk     %s" % (lr.get("closing_trunk_ref") or "-"))
            out.append("  anchor    %s  proof %s%s" % (
                lr.get("closing_trunk_sha") or "-",
                lr.get("close_proof_mode") or "-",
                "  translated tip %s" % lr["translated_tip"]
                if lr.get("translated_tip") else ""))
            if lr.get("landing_review_id"):
                out.append("  via       APPROVED REVIEW %s  tip %s" % (
                    lr["landing_review_id"],
                    lr.get("landing_review_tip") or "-"))
                out.append("  authorization tier %s; gate %s (%s); proof %s" % (
                    lr.get("landing_review_tier_state") or "-",
                    lr.get("landing_review_gate") or "none",
                    lr.get("landing_review_gate_requirement") or "-",
                    lr.get("landing_review_approval_anchor") or "-"))
        elif reason == "superseded":
            out.append("  superseded %s via dispatch %s  %s" % (
                lr.get("superseding_tip") or "-",
                lr.get("superseding_id") or "-",
                lr.get("close_evidence") or "-"))
        elif reason == "subsumed":
            out.append("  confirmation %s  tip %s" % (
                lr.get("confirmation_id") or "-",
                lr.get("confirmation_tip") or "-"))
            out.append("  reimplementation %s" % (
                lr.get("confirmation_ref") or "-"))
            out.append("  families  %s/%s -> %s/%s" % (
                lr.get("original_author") or "-",
                lr.get("original_author_family") or "-",
                lr.get("confirmation_recipient") or "-",
                lr.get("confirmation_recipient_family") or "-"))
            out.append("  authorization tier %s; gate %s (%s); proof %s" % (
                lr.get("confirmation_tier_state") or "-",
                lr.get("confirmation_gate") or "none",
                lr.get("confirmation_gate_requirement") or "-",
                lr.get("confirmation_approval_anchor") or "-"))
            out.append("  verdicts  original %s; confirmation %s" % (
                lr.get("original_verdict_anchor") or "-",
                lr.get("confirmation_verdict_anchor") or "-"))
            out.append("  trunk     %s@%s  confirmation %s; original %s" % (
                lr.get("closing_trunk_ref") or "-",
                (lr.get("closing_trunk_sha") or "-")[:12],
                lr.get("close_proof_mode") or "-",
                lr.get("original_proof_mode") or "-"))
        elif reason == "discharged":
            out.append("  discharged by %s  tip %s  tier %s" % (
                lr.get("discharging_id") or "-",
                (lr.get("discharging_tip") or "-")[:12],
                lr.get("discharge_tier") or "-"))
            out.append("  evidence  %s" % (lr.get("close_evidence") or "-"))
        elif reason == "withdrawn":
            out.append("  withdrawn absent at %s@%s  %s" % (
                lr.get("absence_trunk_ref") or "-",
                (lr.get("absence_trunk_sha") or "-")[:12],
                lr.get("close_evidence") or "-"))
            # The hand rides on its OWN line, beside the evidence rather than
            # inside it: the evidence is the closer's sentence and this is the
            # ledger's, and folding a recorded fact into free prose is how a
            # reader loses which half the record vouches for.
            out.append("  withdrawn %s" % landreq.withdrawal_hand(lr))
        elif reason == "stranded":
            out.append("  stranded  substrate destroyed; control %s in %s  %s"
                       % ((lr.get("control_sha") or "-")[:12],
                          lr.get("closing_repo_id") or "-",
                          lr.get("close_evidence") or "-"))
    if lr.get("contrary"):
        target = "%s trunk" % (lr.get("contrary_target") or "local")
        fact = landreq.contrary_fact(lr)
        # THE SAME WORDS `lr list` PRINTS, or the two terminals describe one
        # row differently. A stamped discharge means the verdict was HONORED
        # through succession, so "owed by integrator" would bill a debt the
        # chain already paid; UNVERIFIED stays billed AND says why; an absent
        # stamp stays loud — fail-closed. Display only: `owed_by` untouched.
        discharge = lr.get("contrary_discharge")
        unknown_reason = lr.get("succession_unknown_reason") or \
            "carrier relation or landing proof is unreadable"
        suffix = " — DISCHARGED" if lr.get("discharged") \
            else " — CONFIRMATION round: the reviewed tip is the landed " \
                 "resolution by design (the discharge instrument, never a " \
                 "debt)" \
            if discharge == "c" \
            else " — SUPERSEDED-CLOSED; HONORED through succession (%s)" % (
                "continuation" if discharge == "a" else "ladder discharge") \
            if landreq.honored_display(lr) \
            else " — CLOSED (SUPERSEDED)" \
            if lr.get("close_reason") == "superseded" \
            else " — owed by integrator (succession UNVERIFIED — %s; no " \
                 "discharge was inferred)" % unknown_reason \
            if discharge == "unverified" \
            else " — owed by integrator"
        out.append("  contrary  %s on %s despite %s verdict%s%s" % (
            fact, target, (lr.get("polarity") or "unknown").upper(), suffix,
            landreq.contrary_provenance_clause(lr)))
        if lr.get("discharged"):
            out.append("  discharge %s via dispatch %s  %s" % (
                lr.get("superseding_tip") or "-",
                lr.get("superseding_id") or "-",
                lr.get("discharge_ref") or "-"))
    elif lr.get("withdrawn"):
        out.append("  withdrawn %s — lane abandoned in favour of the verdict  %s" % (
            (lr.get("polarity") or "fix").upper(),
            lr.get("withdraw_ref") or "-"))
    elif lr["state"] in ("MERGED_LOCAL", "LANDED"):
        target = "upstream trunk" if lr["has_upstream"] else "local trunk"
        source = " [verified receipt]" if lr["receipt"] else ""
        out.append("  landed    %s (%s)%s" % (
            "yes" if lr["landed"] else "local only, NOT pushed", target, source))
    elif lr["state"] == "REVIEWED" and not lr.get("closed_by_landing") \
            and not lr.get("close_reason"):
        # UNDECLARED is a claim about the LEDGER, so it may only be printed
        # when the ledger actually lacks a polarity. Printing it unconditionally
        # made this line contradict the row's own `verdict` line two lines up,
        # and a reader who trusted the projection over the record announced in
        # the room that an APPROVE carried no polarity.
        source = dispatches._source_label(lr.get("polarity_source"))
        if lr.get("polarity"):
            out.append("  polarity  %s — current value owned by %s; LR is a "
                       "projection. This row is REVIEWED rather than READY for "
                       "the reason on the NOT READY line, not for want of a "
                       "verdict" % (lr["polarity"].upper(), source))
        else:
            out.append("  polarity  UNDECLARED in %s — LR is a projection; no "
                       "stall is billable in either direction (a verdict is "
                       "immutable, so this row can never gain one)" % source)
    elif lr["state"] == "READY" and not lr["observable"]:
        # TWO CAUSES REACH HERE AND THEY NEED OPPOSITE READER ACTIONS. The old
        # single message blamed a missing repo binding unconditionally, while
        # this same output prints `repository` and `trunk` two lines above —
        # it named a cause its own report refutes, and sent readers hunting a
        # binding that was present and correct (audit 2026-08-05).
        # The common case by far is DELIBERATE: _observe() is called with
        # git=False for a row closed as landed/superseded/stranded/subsumed/
        # delivered-report, so a terminal row stops re-reading git. That makes
        # observable False, which makes landed False, so the state ladder can
        # never reach its `closed and landed` rung and falls through to the
        # VERDICT word — which is why a closed row still reads READY forever.
        # Measured: 121 of 135 raw-READY rows are close_reason='landed', and
        # NONE of them is observable. Nothing here is stale; it is frozen.
        why = lr.get("close_reason")
        if why:
            out.append("  landed    NOT RE-OBSERVED BY DESIGN — closed as "
                       "%s, and a terminally-closed row stops re-reading git "
                       "(proved once, never re-proved). The repo binding "
                       "above is fine. READY here is the VERDICT word frozen "
                       "at closure, NOT a live lifecycle state — this row is "
                       "already terminal; do not try to land it." % why)
        elif lr.get("has_upstream"):
            # ASKED AND UNANSWERABLE IS NOT THE SAME AS NEVER ASKED, and the
            # single message below used to claim the second while the row's own
            # fields proved the first. `has_upstream` is set INSIDE
            # `_git_observe` from a resolved upstream ref, so it can only be
            # true on a row where git ran, the gitdir was usable and trunk
            # resolved — which is precisely the row the old sentence accused of
            # having no repo binding and no trunk.
            #
            # Traced end to end on a live READY row rather than inferred:
            # `_stored_patch_index` returned a CAPPED window (599 entries), the
            # tip's patch-id was not in it, and a capped miss is honestly
            # `unknown` and never `absent` — so `_landing_proof` answered
            # "unknown", `landed_ever` turned that into None, and
            # `_git_observe`'s `if local is None or upstream is None: return
            # blind` dropped observability on the floor with has_upstream
            # already set. Every clause of the old message was false for it.
            #
            # This is the SAME defect the 2026-08-05 audit named — "it named a
            # cause its own report refutes, and sent readers hunting a binding
            # that was present and correct" — cured then for rows carrying a
            # close_reason and left standing for live ones. It cost three wrong
            # diagnoses in a row before anyone read the emitting branch.
            out.append("  landed    NOT PROVEN — helm ASKED git and could not "
                       "settle it. The repo binding above is fine and trunk "
                       "resolved; what came back is an UNKNOWN landing proof, "
                       "which a bounded patch-index scan returns when it "
                       "misses (a capped miss is not an absence). Never "
                       "inferred landed, in either direction.")
        else:
            out.append("  landed    NOT OBSERVED — helm did not ask git for "
                       "this row: no repo binding, no tip, or no trunk ref to "
                       "resolve (never inferred landed)")
    receipt_state = lr.get("receipt_state")
    if receipt_state not in (None, landreq.R_NONE):
        diagnostic = " — diagnostic only; live Git governs" \
            if receipt_state in (landreq.R_REJECTED, landreq.R_CONFLICT, landreq.R_UNREADABLE) else ""
        reason = "  (%s)" % lr["receipt_reason"] \
            if lr.get("receipt_reason") else ""
        label = "%s — the land-receipt ledger could not be READ, which is NOT " \
            "the same as no receipt" % receipt_state \
            if receipt_state == landreq.R_UNREADABLE else receipt_state
        out.append("  receipt   %s%s%s" % (label, diagnostic, reason))
    if lr.get("closed_ts_impossible"):
        out.append("  closed    UNKNOWN — the ledger's closure stamp for this "
                   "row is dated in the FUTURE, so it is not an instant this "
                   "row can have closed at; repair that ledger row")
    if lr.get("attest_state"):
        out.append("  attest    " + landreq._attest_text(lr))
    if lr.get("ledger_refused"):
        # Historical refusal is a separate axis from current polarity. The
        # dispatch fold owns both facts: accepted events supply current state;
        # refused events remain visible as durable repair evidence.
        out.append("  refused   the dispatch fold refused historical %s event; "
                   "it is absent from the state and the timeline below, whose "
                   "current fields come only from accepted dispatch events"
                   % "/".join(lr["ledger_refused"]))
    out.append("  timeline:")
    for step in lr["timeline"]:
        # "(observed)" belongs to the git-observed step ALONE. It used to be
        # printed for every absent stamp, which explained a ledger event with
        # no ts as something git had seen — and an OPEN step is never that.
        out.append("    %-16s %s" % (step["state"], step["ts"] or (
            "(the ledger's stamp here is NOT A TIMESTAMP)"
            if step.get("ts_unreadable")
            else "(the ledger's stamp here is dated in the FUTURE)"
            if step.get("ts_impossible") else "(git-observed, no ledger stamp)"
            if step.get("observed") else "(no stamp on the ledger event)")))
    threshold = lr["stall_threshold_s"]
    tail = ""
    if threshold is not None:
        # the measurement stays true on an honored row — only the ALARM WORD
        # yields, and it says why instead of pretending "ok"
        word = ("past threshold, not billed (CONFIRMATION row — the "
                "discharge instrument)"
                if lr.get("contrary_discharge") == "c"
                else "past threshold, not billed (HONORED through "
                "succession)") \
            if lr["stalled"] and landreq.honored_display(lr) \
            else "STALLED" if lr["stalled"] else "ok"
        tail = "  (threshold %s — %s)" % (landreq._fmt_dwell(threshold), word)
    out.append("  dwell     %s in %s%s" % (
        landreq._fmt_dwell(lr["dwell_s"]) if lr.get("dwell_known")
        else "UNKNOWN (this row's age was never measured: the record carries "
             "no usable instant to measure FROM, or — this row having already "
             "closed — none to measure TO)", lr["state"], tail))
    return "\n".join(out)

def _cmd_legacy_completion_hints(rest):
    """Read-only audit of historical reason-text completion hints."""
    from .cli import guard_tail
    rc = guard_tail("helm lr legacy-completion-hints", rest,
                    flags=("--json",), usage=landreq.USAGE)
    if rc is not None:
        return rc
    rows, unavailable = landreq.legacy_completion_hints()
    if unavailable:
        return landreq._unavailable(unavailable)
    if "--json" in rest:
        print(json.dumps({"why": landreq._LEGACY_COMPLETION_WHY, "rows": rows},
                         ensure_ascii=False, indent=1))
        return 0
    print("helm lr — %d legacy completion hint%s; every row remains "
          "CANCELLED and UNVERIFIED" % (len(rows), "" if len(rows) == 1 else "s"))
    print("  " + landreq._LEGACY_COMPLETION_WHY)
    for row in rows:
        print("  %s  %s  eligible=%s" % (
            row["id"], row["classification"], row["delivered_report_eligible"]))
        print("    cancel_reason: %s" % row["cancel_reason"])
    return 0

def _print_loop_list(rows_, filed_, read_at, note="", withheld=None,
                     all_projects=False):
    """THE ONE RENDERER, fed by either transport.

    The cold replay and the warm projection both arrive here, because a
    second formatter is how `lr list` and the board came to disagree
    about the word "in flight" the first time -- the incident this
    function's own comments record just below. `note` states the
    transport and the projection's age, appended INSIDE the header so a
    pasted line still carries where it came from. `withheld` is the
    project-scope disclosure record (`withheld_split`) and prints on its
    own line under the header — the board may withhold other projects'
    rows only while saying so (task/974)."""
    retired = sum(1 for lr in rows_ if lr["terminal"])
    # ONE PREDICATE FOR "IN FLIGHT" ACROSS BOTH FRONT-ENDS, which is the
    # whole point of this lane and which its first cut did not finish:
    # the browser partitions honored rows OUT of its in-flight
    # count (00-core.js lrHonored, `live.length + " in flight"`) while
    # this header counted them IN, so one word still named two numbers —
    # the exact defect one surface down. The direction is not a taste
    # call: the owner ruled it (2026-08-04, quoted at 00-core.js:1089)
    # "superseded, if verified, should just be like another type of
    # closed", so honored is CLOSED and the CLI follows the ruling.
    # The docstring above already promised "minus honored" and was, until
    # this line, describing a subtraction nobody performed.
    #
    # THE ROWS ARE STILL PRINTED. Only the accounting moves — same as the
    # board, where an honored row keeps its SUPERSEDED-CLOSED mark and
    # moves to the closed strip. So the header states all three numbers
    # rather than quietly printing more rows than it counts.
    honored_n = len(rows_) - retired - len(landreq.inflight_rows(rows_))
    # ADMINISTRATIVE RETIREMENT GETS ITS OWN NUMBER, and it is a SUBSET of the
    # terminal count beside it rather than a rival to it. Both readings are
    # needed and they answer different questions: `retired` says how many rows
    # are done with, `admin_n` says how many of those helm gave up on rather
    # than resolved. Folding the second into the first would make a board that
    # cleared 30 rows by measuring them unreachable look exactly like a board
    # that landed 30 — which is the one confusion this whole terminal exists
    # to prevent.
    admin_n = sum(1 for lr in rows_ if lr.get("retired_admin"))
    # THE HEADLINE SAYS WHEN ITS OWN NUMBER IS AN UPPER BOUND. Counted over
    # the IN-FLIGHT subset, not over every row, so it answers the question the
    # number beside it asks: of the loops this board calls live, how many were
    # never actually measured against trunk. It sits in `counted` with the
    # others so it renders only when non-zero -- a fully derived board says
    # nothing extra, which is the point.
    #
    # AND THE WORD CARRIES THAT SCOPE, because `filed_line` prints a SECOND
    # underived number on the same rendered line -- the all-time filed
    # population -- and one noun may not name two predicates on one surface.
    # This is the third term of that shape in this header, after `open`
    # against `in flight` (task/324) and `honored` against the browser's
    # count. Unqualified, one cold read of the live board rendered
    # "529 UNDERIVED" and "1309 underived" about forty words apart.
    underived_n = sum(1 for lr in landreq.inflight_rows(rows_)
                      if lr.get("observe_why") == landreq.OBSERVE_UNDERIVED)
    counted = [(retired, "RETIRED — owed by nobody"),
               (admin_n,
                "of them ADMINISTRATIVELY RETIRED — proof chain measured "
                "unreachable, unbillable, no claim about the work"),
               (honored_n,
                "honored — closed through succession, not work"),
               (underived_n,
                "IN-FLIGHT UNDERIVED — landedness never determined, so this "
                "count is an UPPER BOUND that re-reading lowers")]
    said = ["%d %s" % (n, word) for n, word in counted if n]
    # The populated header carries the instant too: every row below it
    # prints a RELATIVE dwell, and a relative age with no origin
    # re-anchors to whenever the listing is read next.
    print("helm lr — %d land loop%s%s  (projection; %s; "
          "verdict polarity: %s; read %s%s)"
          % (len(rows_), "" if len(rows_) == 1 else "s",
             "  (%s; %d in flight)"
             % ("; ".join(said), len(landreq.inflight_rows(rows_)))
             if said else " in flight",
             landreq.filed_line(filed_),
             dispatches._source_label(dispatches.POLARITY_SOURCE),
             read_at, note))
    # THE DISCLOSURE IS PART OF THE BOARD, NOT AN OPTION ON IT: every
    # transport that reaches this renderer carries a withheld reading (the
    # warm path declines bodies without one), so a scoped listing can never
    # print without saying what left it and how to see it.
    print(landreq.withheld_line(withheld, all_projects=all_projects))
    avail = landreq._availability_lookup()
    for lr in rows_:
        print(landreq._line(lr, avail=avail))
    if any(lr["stalled"] for lr in rows_):
        print("STALLED = past its per-stage threshold (helm lr stalls)")

def cmd_lr(args):
    """The `helm lr` verbs, with ONE MEMO WINDOW over the ones that only read.

    THE LISTING OPENED ITS SCOPE IN THE MIDDLE OF ITSELF. `project_raw` enters
    `projscope.scope()` around its row loop and nothing else did, so the ledger
    fold it projects — `dispatches.snapshot_and_events`, which runs before that
    `with` — and the per-row render below it, which reaches a SECOND fold
    through `_line` -> `independent_review` -> `chain_contributor_index` ->
    `_ledger_fold`, each bought their own answers to questions the projection
    had already paid for. MEASURED through the CLI against a frozen copy of the
    live record: two folds per listing, NEITHER inside a scope, 4113 reads
    through the git seam over 1789 DISTINCT questions — 255 spawns of one
    `rev-parse` of the trunk ref, 255 of one `rev-parse
    --is-shallow-repository`, 171 of ONE `ls-tree -r -z` of trunk whose every
    path was re-parsed into a fresh dict all 171 times.

    IT IS A COHERENCE FIX BEFORE IT IS A CHEAPER ONE, the same argument
    `project_raw`'s docstring makes one layer down. A scope means two identical
    questions ninety seconds apart get ONE answer, and for a projection whose
    state fold, rows, disclosure and availability line are meant to be four
    views of ONE instant that is the point and not the cost: asked in three
    unshared windows they can be told about three different trunks, and the
    header then counts rows against a trunk the rows below it were never
    compared with.

    THE SCOPE GOES ON A NAMED SET OF VERBS AND NOT ON THIS FUNCTION. Every
    branch below arrives through one dispatch and several of them WRITE.
    `projscope`'s module docstring makes "outside a scope nothing is cached"
    the property the land door, the close ladders and the send advisory are
    safe by, so a memo stretched over the whole dispatch would let a land be
    authorised by a question asked before it started. A write verb's own read
    is a different thing and is unchanged: `_cmd_land` projects, and
    `project_raw` has always opened a scope around that projection. The
    property here is that nothing ABOVE a write verb opens one for it.
    """
    verbs = list(args or [])
    if verbs and verbs[0] in landreq._SCOPED_READ_VERBS:
        with projscope.scope():
            return landreq._cmd_lr(args)
    return landreq._cmd_lr(args)

def _cmd_lr(args):
    args = list(args or [])
    if not args or args[0] in ("-h", "--help"):
        print(landreq.USAGE, file=sys.stderr)
        return 2
    verb, rest = args[0], args[1:]
    from .cli import guard_tail, suggest
    if verb == "refs":
        rc = guard_tail("helm lr refs", rest, flags=("--json",),
                        valued=("--repo",), usage=landreq.USAGE)
        if rc is not None:
            return rc
        i = rest.index("--repo") if "--repo" in rest else -1
        report, err = landreq.dangling_refs(repo=(rest[i + 1] if i >= 0 else None))
        if err:
            print("helm lr refs: " + err, file=sys.stderr)
            return 1
        if "--json" in rest:
            print(json.dumps(report, ensure_ascii=False, indent=1))
            return 1 if report["dangling"] else 0
        if report.get("note"):
            print("helm lr refs: " + report["note"], file=sys.stderr)
            return 1
        n = len(report["dangling"])
        print("helm lr refs — %d commit id%s recorded across the land-request "
              "and dispatch ledgers, %d unresolvable in %s" % (
                  report["checked"], "s"[:report["checked"] != 1], n,
                  report["repo"]))
        for d in report["dangling"][:20]:
            print("  DANGLING %-9s %-34s %-18s %s" % (
                d["source"], d["id"], d["field"], d["sha"]))
        if n > 20:
            print("  (+%d more — --json for the full set)" % (n - 20))
        if n:
            print("  a recorded proof that cannot be resolved can never be "
                  "re-verified; after a history rewrite, migrate the ledgers "
                  "through git filter-repo's commit-map rather than leaving "
                  "these", file=sys.stderr)
        elif report["unknown"]:
            print("  %d UNKNOWN (git could not be asked) — not cleared"
                  % report["unknown"], file=sys.stderr)
        return 1 if n else 0
    if verb == "migrate":
        rc = guard_tail("helm lr migrate", rest, flags=("--apply", "--json"),
                        valued=("--commit-map", "--repo"), usage=landreq.USAGE)
        if rc is not None:
            return rc
        if "--commit-map" not in rest:
            print("helm lr migrate: --commit-map PATH is required (git "
                  "filter-repo writes .git/filter-repo/commit-map)",
                  file=sys.stderr)
            return 2
        cm = rest[rest.index("--commit-map") + 1]
        rp = rest[rest.index("--repo") + 1] if "--repo" in rest else None
        report, err = landreq.migrate_refs(cm, repo=rp, apply="--apply" in rest)
        if err:
            print("helm lr migrate: " + err, file=sys.stderr)
            return 1
        if "--json" in rest:
            print(json.dumps(report, ensure_ascii=False, indent=1))
            return 0
        n = len(report["translations"])
        print("helm lr migrate%s — %d commit id%s translatable, %d left alone "
              "(absent from the map), over %d mapped rewrite%s" % (
                  "" if report["applied"] else " [dry-run (--apply to record)]",
                  n, "s"[:n != 1], report["unmapped"], report["mapped"],
                  "s"[:report["mapped"] != 1]))
        for t in report["translations"][:15]:
            print("  %-9s %-34s %-16s %s -> %s" % (
                t["source"], t["id"], t["field"], t["old"][:12], t["new"][:12]))
        if n > 15:
            print("  (+%d more)" % (n - 15))
        if report.get("chained"):
            # Say it out loud. The number that exposed the bug was a plausible
            # zero, so a run that only works because earlier passes were
            # composed should never look like a run that matched directly.
            print("  %d of those were COMPOSED through an earlier rewrite this "
                  "map does not know about — the recorded translation stays "
                  "one hop: original -> newest" % report["chained"])
        if report.get("sidecar"):
            print("  recorded %d translation(s) -> %s" % (
                report["written"], report["sidecar"]))
            print("  the attested tips are UNCHANGED by design — a migration "
                  "that could move what a verdict attested could forge one; "
                  "resolution goes through content identity, this only "
                  "explains. Verify with: helm lr refs")
        return 0
    if verb == "legacy-completion-hints":
        return landreq._cmd_legacy_completion_hints(rest)
    if verb == "list":
        rc = guard_tail("helm lr list", rest,
                        flags=("--all", "--json", "--cold", "--all-projects"),
                        usage=landreq.USAGE)
        if rc is not None:
            return rc
        all_projects = "--all-projects" in rest
        # loops() inlined so the header can also say how many rows the ledger
        # HOLDS — same single project_raw read, no second walk. The web card's
        # filed strip (/api/lr) states the same population; a CLI header that
        # could not would let the two surfaces disagree about one record.
        # ONE NUMERIC INSTANT, bound BEFORE the projection and threaded into
        # it. Binding only the STAMP was half the cure: `project_raw()` with
        # no argument samples the clock ITSELF, so the header could name T0
        # while the dwell and stall classification inside the projection used
        # T1. A review found that the stamp-count tests cannot see it — they
        # assert how many stamps print, not which clock classified the rows.
        # project_raw's own docstring warns about the two-instants bug; its
        # caller was reintroducing it one level up.
        # THE WARM READ, and the flags it must NOT serve — SCOPE ONLY.
        #
        # `--all` asks for a DIFFERENT ROW SET — `_loop_rows(include_landed=
        # True)` — and the warm projection is built with the default. Serving
        # it warm would answer a narrower question confidently, which is the
        # capped-partial failure: a bound that skips work must never render as
        # a checked answer. `--all-projects` asks for a DIFFERENT ROW SET too,
        # same law: the warm projection is built with the default scope and
        # serving it here would answer the wide question with the narrow body.
        #
        # `--json` used to force cold here as well, and that was the recorded
        # defect (task/1821, owner-directed): the guard conflated SHAPE (json
        # vs card) with SCOPE (which rows). Each clause was defensible alone —
        # serving a script the card projection would be worse than being slow
        # — but the emergent sum was that the machine-readable path, the one
        # agents reach for by correct instinct, was the one excluded from the
        # accelerator: human path 307ms, agent path 90s over the SAME rows
        # (measured 2026-08-28). The warm body now carries the raw row
        # product built from its own snapshot (`rows`, web_land._lr_project),
        # so shape no longer costs the replay; only SCOPE decides warm-vs-
        # cold, and a body that predates the field falls through below.
        #
        # Measured 2026-08-07 on the live instance: warm 3 ms, cold 18,621 ms.
        # That is the whole reason this path exists, and it is an ACCELERATOR
        # — every refusal below falls through to the identical cold replay.
        want_json = "--json" in rest
        # ...AND THE WARM BODY IS BUILT FOR THE RUNNING `helm web`'s SCOPE,
        # which is anchored to the helm package (web_land_model._lr_repo). So a
        # caller standing in ANOTHER project's checkout is asking a question
        # this body does not answer: serving it would render helm's rows under
        # that project's header, which is the same "narrow body, wide question"
        # failure `--all-projects` falls through for, mirrored (task/2437).
        cwd_repo, cwd_project, _cwd_why = dispatches.cwd_scope()
        home_repo, _home_why = dispatches.home_repo_id()
        home_scoped = bool(home_repo) and \
            dispatches._real(cwd_repo) == dispatches._real(home_repo)
        warm_ok = "--cold" not in rest and "--all" not in rest \
            and not all_projects and home_scoped
        if not home_scoped and "--cold" not in rest:
            print("helm lr: cold replay — this directory is project %s and the "
                  "running `helm web` renders %s" % (
                      cwd_project or cwd_repo or "UNRESOLVED",
                      landreq._project_of(str(home_repo)) or home_repo or "UNKNOWN"),
                  file=sys.stderr)
        if warm_ok:
            warm_body, warm_why = landreq.warm_lr_body()
            if warm_body is not None and "withheld" not in warm_body:
                # A BODY WITHOUT A WITHHELD READING PREDATES PROJECT SCOPING
                # (task/974): its lists were built unscoped, so rendering it
                # under a header that promises scope would show another
                # project's rows as this board's own — the exact defect the
                # scope exists to end. Vintage mismatch, same answer as the
                # render failure below: name it, take the cold path.
                print("helm lr: cold replay — the warm projection carries no "
                      "project-scope reading; the running `helm web` predates "
                      "project scoping", file=sys.stderr)
                warm_body = None
            if want_json and warm_body is not None \
                    and not isinstance(warm_body.get("rows"), list):
                # SAME VINTAGE LAW FOR THE RAW ROWS (task/1821): a body
                # without the field predates the --json warm path — or its
                # builder refused the chain and REMOVED it, which must read
                # identically here. Absence falls through to the cold replay;
                # only a list may answer, because an invented empty would be
                # the confident narrower answer this whole block forbids.
                print("helm lr: cold replay — the warm projection carries no "
                      "raw rows; the running `helm web` predates the --json "
                      "warm path", file=sys.stderr)
                warm_body = None
            if warm_body is None and warm_why:
                # SAY WHY THE FAST PATH DECLINED, on stderr so it never enters
                # the listing a reader pastes. `warm_lr_body`'s own docstring
                # promises "the reason is for the operator, never swallowed"
                # and my first cut assigned it to `_warm_why` and dropped it —
                # so the operator waited two and a half minutes with no idea
                # the accelerator had even been tried. An accelerator that
                # silently does nothing is indistinguishable from a broken one.
                print("helm lr: cold replay — %s" % warm_why, file=sys.stderr)
            if warm_body is not None and want_json:
                # THE MACHINE CONTRACT IS UNCHANGED: raw rows on stdout,
                # disclosure on stderr — the same two prints, in the same
                # order, as the cold replay below. JSON has no header line to
                # carry the transport, so the freshness note rides stderr
                # instead, where it cannot enter the parsed stream and cannot
                # be silent either (the age is STATED, always — a warm answer
                # that hid when it was computed would be a faster way to be
                # wrong, and --cold forces the replay).
                print(landreq.withheld_line(warm_body.get("withheld"),
                                    all_projects=False), file=sys.stderr)
                print("helm lr: WARM projection, computed %ds ago; --cold "
                      "replays" % warm_body.get("read_age_s", -1),
                      file=sys.stderr)
                print(json.dumps(warm_body["rows"], ensure_ascii=False,
                                 indent=1))
                return 0
            if warm_body is not None:
                # RENDER TO A BUFFER FIRST, because the warm payload comes
                # from a DIFFERENT, LONG-LIVED PROCESS whose vintage we do not
                # control. Measured the hard way: the gate went red because a
                # `helm web` started before this lane served cards without the
                # `terminal` key, the header's `inflight_rows` raised KeyError
                # mid-print, and the accelerator had become exactly the new
                # failure mode this row forbids.
                #
                # A KEY-LIST CHECK AT THE DOOR WOULD DRIFT out of sync with
                # the renderer the first time the renderer read one more
                # field. Proving it RENDERS cannot drift, because the proof IS
                # the renderer — and nothing reaches the terminal until the
                # whole listing is known to format, so a half-printed board is
                # not a state this can produce.
                import contextlib
                import io
                buf = io.StringIO()
                try:
                    with contextlib.redirect_stdout(buf):
                        landreq._print_loop_list(
                            warm_body["loops"], warm_body.get("filed"),
                            dispatches._read_stamp(warm_body.get("read_ts")),
                            # THE AGE IS STATED, ALWAYS. A warm answer that
                            # hid when it was computed would be a faster way
                            # to be wrong; staleness displayed beats staleness
                            # hidden, and --cold forces the replay.
                            " — WARM projection, computed %ds ago; --cold "
                            "replays" % warm_body.get("read_age_s", -1),
                            withheld=warm_body.get("withheld"))
                except Exception as e:
                    # A payload this build cannot render is an OLDER helm web,
                    # not a reason to fail: name the vintage mismatch and take
                    # the cold path, which is what the caller would have had.
                    print("helm lr: cold replay — the warm projection did not "
                          "render (%s: %s); the running `helm web` is probably "
                          "older than this build" % (type(e).__name__, e),
                          file=sys.stderr)
                else:
                    sys.stdout.write(buf.getvalue())
                    return 0
        read_now = time.time()
        # ONE scope resolution feeds the marking AND the header, so the rows
        # were classified against exactly the identity the disclosure names.
        # FROM THE DIRECTORY, NOT FROM WHERE THE PACKAGE LIVES (task/2437).
        scope_ = landreq._cli_scope()
        lrs_, raw_, unavailable = landreq.project_raw(read_now, scope=scope_)
        if unavailable:
            return landreq._unavailable(unavailable)
        # ONE MEMO, STILL TWO BUDGET WINDOWS — A SCOPE IS AN OPERATION AND
        # NEVER A CLOCK, and the scope `cmd_lr` opens over this verb would
        # quietly have made it one. The derive budget rides `projscope.memo`
        # under a single key and `arm_derive_budget` is first-seeder-wins, so
        # one cache across the whole listing shares the DEADLINE too: the row
        # loop seeds the four-second default the moment it asks, and every
        # walk after the projection then reads a bound that expired a minute
        # before it began. MEASURED board against board on a frozen record:
        # the census fell from 203 OFF-FRONTIER and 5 closable to 202 and 4,
        # and the row it lost turned up in UNCLASSIFIED — the retirable
        # population shrinking and the work-owed count growing, which is the
        # flip `filed_split`'s own docstring records from the last memo fix
        # that charged that walk a budget it had never had.
        #
        # SO THE WINDOW RE-OPENS WHERE THE SECOND SCOPE OPENS IT. Without the
        # verb's scope everything below here stands outside any scope, which by
        # `_derive_expired`'s own contract is unbounded, until `filed_split`
        # arms the board's number for its own walk. The evict and the arm
        # reproduce exactly that under the wider memo: the git answers stay
        # shared, which is the whole cure, and the derive spend keeps two
        # windows rather than one. `filed_split`'s own arm is then the ignored second seeder it is
        # documented to be, which is how the web read already treats it.
        #
        # WHAT THIS DELIBERATELY DOES NOT DECIDE: whether a foreground cold
        # replay the operator waits two minutes for should give the ROW LOOP
        # more than the four seconds sized for an inject turn. Seeding the
        # board's number at the top of the listing would do that, and it would
        # derive rows this board prints as UNDERIVED — a change to what the
        # owner reads, which is his call and not a side effect of a memo fix.
        projscope.forget(landreq._DERIVE_DEADLINE_KEY)
        landreq.arm_derive_budget(landreq.BOARD_DERIVE_BUDGET_S)
        try:
            rows_ = landreq._loop_rows(lrs_, raw_, include_landed="--all" in rest,
                               all_projects=all_projects)
        except landreq._ChainUntrustworthy as e:
            return landreq._unavailable(str(e))
        withheld_ = landreq.withheld_split(lrs_, scope_)
        if "--json" in rest:
            # The machine contract stays raw rows — a wrapper would change
            # the shape scripts parse. The disclosure goes to stderr, where
            # it cannot enter the parsed stream and cannot be silent either.
            print(landreq.withheld_line(withheld_, all_projects=all_projects),
                  file=sys.stderr)
            print(json.dumps(rows_, ensure_ascii=False, indent=1))
            return 0
        filed_ = landreq.filed_split(lrs_, raw_)
        # The stamp is DERIVED from the same number the projection used, so
        # the instant a reader sees is the instant that classified the rows.
        read_at = dispatches._read_stamp(read_now)
        if not rows_:
            # STAMPED BECAUSE IT IS AN ABSENCE CLAIM. "no land loops in
            # flight" quoted forward reads as a standing fact about the board,
            # and that is the reading an integrator acts on by standing down —
            # hours after it stopped being true. The WHOLE filed strip rides
            # along, same shape as the card's (one owner, filed_line): an
            # empty BOARD over a populated LEDGER is exactly where "nothing
            # here" needs the population beside it, and a pasted header must
            # read identically to the surface it gets compared against.
            print("helm lr: no land loops in flight — %s  (read %s)"
                  % (landreq.filed_line(filed_), read_at))
            # AN EMPTY BOARD IS WHERE THE DISCLOSURE MATTERS MOST: "nothing
            # in flight" over rows the scope withheld would be exactly the
            # confident narrower board task/974 forbids.
            print(landreq.withheld_line(withheld_, all_projects=all_projects))
            return 0
        # "in flight" IS a claim, and under `--all` it was a false one: the
        # header counted every row the listing carries, retirements included,
        # so the live board read `718 land loops in flight` over 653 rows that
        # owe nobody anything (measured 2026-08-03). The default listing
        # filters terminals out, so `retired` is exactly 0 there and that line
        # is unchanged; only the header that has retirements to account for
        # says so. The polarity provenance rides alongside — both facts are
        # about the same header and dropping either to resolve a rebase would
        # trade one operator's truth for another's.
        landreq._print_loop_list(rows_, filed_, read_at, withheld=withheld_,
                         all_projects=all_projects)
        return 0
    if verb == "foldcheck":
        # The five fold checks as ONE refusing rung. Every fold ran these by
        # hand and the fifth — did it reach ORIGIN — is the one that gets
        # skipped when tired, which is how two lands were announced that
        # origin did not have. Exit 1 on REFUSE *or* UNKNOWN: not-measured is
        # not consent, so a caller that gates on rc gets the same stop for
        # "no" and for "I could not tell", which are both reasons not to
        # announce a land.
        fc_usage = ("usage: helm lr foldcheck <tip> [--gate gate:TOKEN] "
                    "[--repo PATH] [--remote R] [--branch B] [--no-fetch]")
        # CLOSED-SET TAIL, and the reason it is not the hand-rolled scan it
        # was: reading options by `opts.index(name) + 1` raised IndexError on
        # a trailing `--gate` and silently IGNORED `--bogus`. A rung that
        # refuses on measurement has to refuse on its own arguments first —
        # an ignored flag means the caller asked for something this verb did
        # not do, and then read the answer as though it had.
        tip = rest[0] if rest and not rest[0].startswith("-") else None
        rc = guard_tail("helm lr foldcheck", rest[1:] if tip else rest,
                        flags=("--no-fetch",),
                        valued=("--gate", "--repo", "--remote", "--branch"),
                        usage=fc_usage)
        if rc is not None:
            return rc
        if not tip:
            print(fc_usage, file=sys.stderr)
            return 2
        opts = rest[1:]

        def _opt(name, default=None):
            return opts[opts.index(name) + 1] if name in opts else default

        repo = _opt("--repo", ".")
        rungs = foldcheck.check(
            repo, tip, gate_ref=_opt("--gate"),
            remote=_opt("--remote", "origin"),
            branch=_opt("--branch", "main"),
            fetch="--no-fetch" not in opts)
        report = foldcheck.report(rungs)
        if not foldcheck.ok(rungs):
            for line in report:
                print(line)
            return 1
        root = _lanes.find_root(repo)
        try:
            return _fold_proven(root, tip, report, _opt("--gate"))
        finally:
            _print_landed_leases(root)
    if verb == "stalls":
        rc = guard_tail("helm lr stalls", rest, flags=("--json",), usage=landreq.USAGE)
        if rc is not None:
            return rc
        # ONE READ INSTANT for every section this verb prints, so the stalled
        # rows, the unmeasurable rows and the tail cannot be read as three
        # observations taken at three different times — and bound AT the read
        # rather than after it. It used to be taken below the --json early
        # return, i.e. after the whole projection, so the published instant
        # named the RENDER; on a large board that projection is the slow part,
        # and the stamp drifted by however long it took.
        # Same cure as `lr list`: ONE numeric instant feeds BOTH the stamp and
        # the projection. Binding the stamp first fixed the drift between the
        # stamp and the render, but left project_raw() sampling its own clock,
        # so the published instant and the stall classification still came
        # from different reads.
        read_now = time.time()
        read_at = dispatches._read_stamp(read_now)
        # Same one-resolution rule as `lr list`, and the same cwd default:
        # the scope that classified the rows is the scope the disclosure names.
        scope_ = landreq._cli_scope()
        lrs, raw, unavailable = landreq.project_raw(read_now, scope=scope_)
        if unavailable:
            return landreq._unavailable(unavailable)
        try:
            rows_ = landreq._stalled_rows(lrs, raw)
            unmeasurable_rows = landreq._unmeasurable_rows(lrs, raw)
        except landreq._ChainUntrustworthy as e:
            return landreq._unavailable(str(e))
        withheld_ = landreq.withheld_split(lrs, scope_)
        closed_rows = sorted(
            (lr for lr in lrs.values() if lr.get("closed_by_landing")),
            key=lambda lr: str(lr.get("landing_ts") or ""))
        if "--json" in rest:
            unmeasurable_json = [{"row": lr, "reason": why}
                                 for lr, why in unmeasurable_rows]
            print(landreq.withheld_line(withheld_), file=sys.stderr)
            print(json.dumps({"stalled": rows_, "unmeasurable": unmeasurable_json,
                              "closed_by_landing": closed_rows},
                             ensure_ascii=False, indent=1))
            return 0
        # The disclosure prints on BOTH branches: "no stalled loops" over a
        # withheld foreign stall would be a confident narrower claim.
        print(landreq.withheld_line(withheld_))
        # ONE persisted proxywatch read for both listings below, for the same
        # reason the store read below is one: a per-row read on a stall board
        # is a file open per row.
        stall_avail = landreq._availability_lookup()
        if not rows_:
            print("helm lr: no stalled land loops — every loop is inside its "
                  "threshold  (read %s)" % read_at)
        else:
            print("helm lr — %d STALLED land loop%s (workflow gaps):  (read %s)"
                  % (len(rows_), "" if len(rows_) == 1 else "s", read_at))
            # ONE store read for the whole listing (ref_branch lives on the
            # dispatch row, not the projection), one repo dir from the row's
            # own recorded repository. The marker is silent whenever it cannot
            # measure, and says so nowhere — see _moved_tip_marker.
            store_rows = dispatches.rows()
            for lr in rows_:
                gitdir = lr.get("repo_id")
                repo_dir = gitdir[:-5] if isinstance(gitdir, str) \
                    and gitdir.endswith("/.git") else gitdir
                store_row = store_rows.get(lr["id"])
                marker = landreq._moved_tip_marker(lr, store_row, repo_dir)
                # THE SECOND MARKER ANSWERS A DIFFERENT QUESTION FROM THE
                # FIRST. Moved-tip asks whether the review is still about this
                # work; landed asks whether the work is still owed at all. A
                # row can be both, and a reader billed for a job already on
                # trunk needs to be told so before either.
                # THE LANDED TAIL REPLACES THE OWED-BY CLAUSE, never sits
                # beside it: a row whose work is on trunk is owed by nobody,
                # and printing a debtor next to the correction leaves the
                # routing debt standing.
                tail = landreq._landed_marker(store_row, lr["state"])
                print(landreq._line(lr, avail=stall_avail) + "  (>= %s in %s, %s)%s" % (
                    landreq._fmt_dwell(lr["stall_threshold_s"]), lr["state"],
                    tail or ("owed by %s" % landreq._owed_by_whom(lr)), marker))
        if unmeasurable_rows:
            print("helm lr — %d loop%s NOT stall-checked:" % (
                len(unmeasurable_rows), "" if len(unmeasurable_rows) == 1 else "s"))
            for lr, why in unmeasurable_rows:
                print(landreq._line(lr, avail=stall_avail) + "  (%s)" % why)
            # For REVIEWED: no remedy — a verdict is immutable. For OPEN unbilled:
            # the fix is re-sending or cancelling.
            if any("UNDECLARED" in why for _lr, why in unmeasurable_rows):
                print("  these carry UNDECLARED verdicts: the ledger records that "
                      "a review happened, not whether it approved. A verdict is "
                      "immutable; new verdicts send --approve|--fix|--supersede.")
        if closed_rows:
            print("helm lr — %d REVIEWED (UNDECLARED) row%s CLOSED BY LANDING; "
                  "excluded from stall accounting (helm lr list --all)" % (
                      len(closed_rows), "" if len(closed_rows) == 1 else "s"))
        return 0
    if verb == "show":
        if not rest:
            print(landreq.USAGE, file=sys.stderr)
            return 2
        rid = rest[0]
        rc = guard_tail("helm lr show", rest[1:], flags=("--json",), usage=landreq.USAGE)
        if rc is not None:
            return rc
        lr, err = landreq.get(rid)
        if err:
            print("helm lr: " + err, file=sys.stderr)
            return 1
        if "--json" in rest[1:]:
            print(json.dumps(lr, ensure_ascii=False, indent=1))
            return 0
        print(landreq._render_show(lr))
        return 0
    if verb == "land":
        return landreq._cmd_land(rest)
    if verb == "compose":
        return landreq._cmd_compose(rest)
    if verb == "close":
        return landreq._cmd_close(rest)
    if verb == "annotate-delivered-report":
        return landreq._cmd_annotate_delivered_report(rest)
    if verb == "discharge":
        return landreq._cmd_discharge(rest)
    if verb == "withdraw":
        return landreq._cmd_withdraw(rest)
    if verb == "abandon":
        return landreq._cmd_abandon(rest)
    if verb == "close-landed":
        return landreq._cmd_close_landed(rest)
    if verb == "expired":
        return landreq._cmd_expired(rest)
    if verb == "retire":
        return landreq._cmd_retire(rest)
    print("helm lr: unknown subverb '%s'%s (%s)" % (
        verb, suggest(verb, ("list", "show", "stalls", "foldcheck",
                             "legacy-completion-hints", "land", "compose",
                             "close",
                             "annotate-delivered-report", "discharge",
                             "withdraw", "abandon", "close-landed",
                             "expired", "retire")),
        landreq.USAGE),
        file=sys.stderr)
    return 2

def _print_retire_sweep(report):
    """The sweep's operator surface — the REFUSALS as loudly as the plan."""
    print("helm lr retire --sweep%s%s — %d billing row%s on the board, %d "
          "older than %gd considered; %d would clear, %d refused, %d skipped "
          "as live-TLA work"
          % ("" if not report["dry_run"] else " [dry-run — nothing written]",
             " [--all-projects]" if report.get("all_projects") else "",
             report["billing"], "s"[:report["billing"] != 1],
             report["considered"], report["older_than_days"],
             len(report["plan"]), len(report["refused"]),
             len(report["skipped_live_tla"])))
    if report["by_class"]:
        print("  billing by class: " + ", ".join(
            "%s %d" % (k, report["by_class"][k])
            for k in sorted(report["by_class"])))
    for entry in report["plan"]:
        print("  RETIRE   %s %-17s %-24s %6s  --reason %s"
              % (entry["id"][:12], entry["state"] or "-",
                 (entry["lane"] or "-")[:24], landreq._fmt_dwell(entry["dwell_s"]),
                 entry["reason"]))
        print("           measured: %s" % entry["measurement"])
    for entry in report["retired"]:
        print("  RETIRED  %s --reason %s at %s"
              % (entry["id"][:12], entry["reason"], entry.get("retire_ts")))
    for entry in report["refused"]:
        print("  REFUSED  %s %-17s %-24s %6s"
              % (entry["id"][:12], entry["state"] or "-",
                 (entry["lane"] or "-")[:24], landreq._fmt_dwell(entry["dwell_s"])))
        for ref in entry["refusals"]:
            print("           %-24s %s" % (ref["reason"], ref["refusal"]))
    for entry in report["skipped_live_tla"]:
        print("  SKIPPED  %s owed by @%s (%s) — a live TLA holds it"
              % (entry["id"][:12], entry["seat"], entry["role"]))
    for entry in report["errors"]:
        print("  ERROR    %s %s" % (entry["id"][:12], entry["err"]))
    if report["dry_run"]:
        print("  nothing was written — re-run without --dry-run to record "
              "the %d retirement%s above"
              % (len(report["plan"]), "s"[:len(report["plan"]) != 1]))

def _print_off_frontier(report):
    """The residue census — the REFUSALS and the UNCLASSIFIED as loudly as
    the plan, because this verb's whole claim is that it never retires a row
    it could not measure."""
    print("helm lr retire --off-frontier%s%s — %d open row%s on the live "
          "board; %d ON the frontier, %d OFF it, %d UNCLASSIFIED"
          % ("" if not report["dry_run"] else " [census — nothing written]",
             " [--all-projects]" if report.get("all_projects") else "",
             report["considered"], "s"[:report["considered"] != 1],
             report["on_frontier"], report["off_frontier"],
             report["unclassified"]))
    # THE SPLIT THE HEADER PRINTS, PRINTED HERE TOO AND FROM THE SAME WALK.
    # "Placed" is what the classification proved; "closable" is what the close
    # ladder will take, and one word for both is what told the owner 316 rows
    # were clearable while the authorizing witness refused 182 of them.
    if report["off_frontier"]:
        print("  of the %d OFF-FRONTIER: %d CLOSABLE NOW, %d placed but NOT "
              "closable — the close ladder's own witness refuses them, "
              "row by row below"
              % (report["off_frontier"], report.get("closable", 0),
                 report.get("not_closable", 0)))
    if report["by_reason"]:
        print("  off-frontier by reason: " + ", ".join(
            "%s %d" % (k, report["by_reason"][k])
            for k in sorted(report["by_reason"])))
    if report["plan"]:
        routed = {}
        for entry in report["plan"]:
            routed[entry["close_reason"]] = routed.get(entry["close_reason"],
                                                       0) + 1
        print("  routed to: " + ", ".join("--reason %s %d" % (k, routed[k])
                                          for k in sorted(routed)))
    # THE REFUSAL PRINTS ONCE. Under `--apply` these same rows come back in
    # the REFUSED block below, where they are the run's OUTCOME; on the dry
    # census that block is empty and the refusal belongs beside the row.
    refusals = {entry["id"]: entry.get("refusal")
                for entry in (report.get("not_closable_rows") or [])
                if report["dry_run"]}
    for entry in report["plan"]:
        # THE VERB SAYS WHICH OF THE TWO THIS ROW IS. `RETIRE` is a row the
        # door will take; `NO-DOOR` is a row the classification placed and the
        # ladder refuses, printed at the same volume and with the ladder's own
        # sentence, because it is the larger half of this population and the
        # half nobody had ever been shown.
        print("  %s %s %-17s %-28s  %s/%s --reason %s"
              % ("RETIRE  " if entry.get("closable") else "NO-DOOR ",
                 entry["id"][:12], entry["state"] or "-",
                 (entry["lane"] or "-")[:28], entry["reason"],
                 entry.get("polarity") or "undeclared",
                 entry["close_reason"]))
        print("           measured: %s" % entry["evidence"])
        if entry["id"] in refusals:
            print("           the %s door refuses: %s"
                  % (entry["close_reason"], refusals[entry["id"]]))
    for entry in report["closed"]:
        print("  CLOSED   %s --reason %s at %s"
              % (entry["id"][:12], entry["close_reason"],
                 entry.get("closed_ts")))
    for entry in report["refused"]:
        print("  REFUSED  %s --reason %s: %s"
              % (entry["id"][:12], entry["close_reason"], entry["refusal"]))
    if not report["dry_run"]:
        # THE TWO OUTCOMES, COUNTED, because a run over a thousand rows
        # scrolls past and the operator needs the shape before the detail:
        # what closed under which word, and how many the ladders refused.
        closed = {}
        for entry in report["closed"]:
            closed[entry["close_reason"]] = closed.get(
                entry["close_reason"], 0) + 1
        print("  closed %d (%s) · refused %d"
              % (len(report["closed"]),
                 ", ".join("%s %d" % (k, closed[k]) for k in sorted(closed))
                 or "none",
                 len(report["refused"])))
    if report["unclassified"]:
        print("  %d UNCLASSIFIED row%s — the lane is gone but the tip could "
              "not be placed (repository unreadable, tip pruned by git gc per "
              "task/2383, or a ref outside the lane family still reaches it). "
              "--apply NEVER touches these."
              % (report["unclassified"], "s"[:report["unclassified"] != 1]))
        for entry in report["unclassified_rows"]:
            print("  UNCLASS  %s %-17s %-28s  at the %s rung"
                  % (entry["id"][:12], entry["state"] or "-",
                     (entry["lane"] or "-")[:28], entry.get("rung") or "-"))
            print("           %s" % entry["evidence"])
    if report["dry_run"]:
        # THE COUNT IS THE CLOSABLE ONE, NOT THE PLAN'S LENGTH. `--apply` acts
        # on the rows whose door answered yes on this same walk; promising the
        # whole plan here is the overclaim this split exists to end, one line
        # from the split itself.
        closable = report.get("closable", 0)
        print("  nothing was written — re-run with --apply to close the %d "
              "row%s above through the existing reason registry"
              % (closable, "s"[:closable != 1]))

def _cmd_expired(rest):
    """`helm lr expired` — the census, and the batch that spends it.

    DRY BY DEFAULT AND THE CENSUS IS THE DEFAULT OUTPUT, because the whole
    point of this verb is to be readable before it is trusted. `--apply` is
    what closes rows, exactly as `retire --sweep` and `seat reassign` split
    the preview from the act, and for the same reason: an operator who cannot
    see WHICH rows would close cannot tell a safe run from a surprising one,
    and this door's population is the part of the board nobody has looked at
    in weeks.

    THE CENSUS AND THE CLOSE RUN THE SAME PREDICATE BY CALL. `expired_verdict`
    answers for both, so the preview cannot promise a population the act then
    disagrees with. Its three states survive to the output: ADMIT, REFUSE and
    UNMEASURED are three buckets here, never two, because "could not tell"
    must be countable separately from "measured and no".
    """
    from .cli import guard_tail
    tail = list(rest)
    rc = guard_tail("helm lr expired", tail, flags=("--json", "--apply"),
                    valued=(), usage=landreq.USAGE)
    if rc is not None:
        return rc
    # TWO FLAGS. A first cut carried --census, --repo, --trunk and --evidence
    # and the owner named it: overcomplicated AX. The census is what the verb
    # IS, so it needs no flag; the repository and trunk are the row's own and
    # every sibling close derives them rather than asking; and the evidence is
    # the predicate's own line, because a door whose admission is measured
    # should not then ask an operator to describe the measurement.
    as_json = "--json" in tail
    apply_it = "--apply" in tail
    repo, trunk, evidence = None, None, None
    # THE ROW SET `lr list` RENDERS, THROUGH THE CALLS THE CARD ITSELF MAKES —
    # owner canon webui-reads-what-agents-read. Two wrong accessors came
    # first and each failed in its own direction, which is why this names the
    # pair rather than any one of them. `project()` is the WHOLE LEDGER: the
    # census reported 11 admissible out of 2119 rows instead of out of the 35
    # an operator sees, and the two elevens were DIFFERENT ELEVENS, same count
    # and four shared ids. `loops()` is the right row set with the wrong
    # BINDING: no `scope`, so git observation is not owned, `land_state`
    # answers UNKNOWN for every row, and the census reported 0 admissible and
    # 32 unmeasured — the safe direction, and completely uninformative.
    # `project_raw(scope=board_scope())` then `_loop_rows` is the pair
    # web_land._lr_project uses, so this census and that card cannot disagree
    # about which rows exist or what their land state is.
    # WARM FIRST, AND THE SOURCE IS DISCLOSED — the same accelerator `lr list`
    # uses, for the reason owner canon gives: this census must not report a
    # board its CLI twin does not have. It is not only speed. A COLD
    # projection taken from an agent seat does not OWN git observation for
    # most rows, so `land_state` answers UNKNOWN and the census reports 31
    # unmeasured and 1 admissible where the board shows 23 ABSENT. Both
    # readings are honest about what each process could see, and the one an
    # operator is looking at is the warm one.
    # ...AND THE WARM BODY IS ONLY THIS PROJECT'S ANSWER (task/2437 round two,
    # finding 5). `lr list` learned the rule one verb over: the warm projection
    # is built for the running `helm web`'s scope, which is anchored to the helm
    # PACKAGE, so a caller standing in another project's checkout is asking a
    # question that body does not answer. This census took the warm rows
    # unconditionally while its COLD path went through `_cli_scope` — so the same
    # command reported a population from helm's board in one process and from the
    # caller's project in another, and with `--apply` it is a population that
    # gets CLOSED. Worse, the two halves disagreed in the same run: the warm ROWS
    # came from helm and the trunk observation below comes from the caller's cwd
    # git lane, so helm's rows were measured against another project's trunk.
    # Same guard, same sentence, and it FALLS THROUGH to the identical cold
    # replay rather than refusing.
    cwd_repo, cwd_project, _cwd_why = dispatches.cwd_scope()
    home_repo, _home_why = dispatches.home_repo_id()
    home_scoped = bool(home_repo) and \
        dispatches._real(cwd_repo) == dispatches._real(home_repo)
    if home_scoped:
        body, warm_why = landreq.warm_lr_body()
    else:
        body = None
        warm_why = ("this directory is project %s and the running `helm web` "
                    "renders %s, so its warm rows are another project's "
                    "population" % (
                        cwd_project or cwd_repo or "UNRESOLVED",
                        landreq._project_of(str(home_repo)) or home_repo or "UNKNOWN"))
        print("helm lr expired: cold replay — " + warm_why, file=sys.stderr)
    lrs, source = None, None
    if body is not None and isinstance(body.get("rows"), list):
        lrs, source = body["rows"], "warm"
    if lrs is None:
        projected, raw_rows, unavailable = landreq.project_raw(None,
                                                       scope=landreq._cli_scope())
        if unavailable:
            print("helm lr expired: the land-request board is unavailable "
                  "(%s) — a census cannot report zero rows when it read none"
                  % unavailable, file=sys.stderr)
            return 2
        lrs, source = landreq._loop_rows(projected, raw_rows), "cold"
    gitdir, err = landreq._close_repo({"repo_id": None}, repo or os.getcwd())
    if err:
        print("helm lr expired: %s" % err, file=sys.stderr)
        return 2
    pinned = landreq._git(gitdir, "rev-parse", trunk or "origin/main")
    if pinned is None or getattr(pinned, "returncode", 1) != 0:
        print("helm lr expired: the trunk ref %s could not be resolved in %s"
              % (trunk or "origin/main", gitdir), file=sys.stderr)
        return 2
    census = landreq.expired_census(lrs, gitdir, (pinned.stdout or "").strip())
    if not apply_it:
        if as_json:
            print(json.dumps(dict(census, projection=source),
                             indent=1, sort_keys=True))
        else:
            print("helm lr expired: CENSUS ONLY — nothing closed. "
                  "%d admissible, %d refused, %d UNMEASURED. [%s projection%s]"
                  % (len(census["admit"]), len(census["refuse"]),
                     len(census["unmeasured"]), source,
                     "" if source == "warm"
                     else " — %s; a cold read from a seat that does not own "
                          "git observation reports UNKNOWN where the board "
                          "reports ABSENT" % (warm_why or "no warm body")))
            for e in census["admit"]:
                print("  ADMIT      %s  %-32s %s" % (
                    str(e["id"])[:12], (e["lane"] or "-")[:32], e["kind"]))
            for e in census["unmeasured"]:
                print("  UNMEASURED %s  %s" % (str(e["id"])[:12], e["why"]))
            print("  Re-run with --apply to close the admissible rows, each "
                  "recording its own measured line; every other row stays "
                  "open.")
        # AN UNMEASURED ROW IS A NON-ZERO CENSUS. The count is the reason to
        # look, and rc 0 is the only part of this a script reads.
        return 1 if census["unmeasured"] else 0
    closed, refused = [], []
    # THE CENSUS'S UNMEASURED ROWS ARE CARRIED, AND APPLY CAN ADD TO THEM. A
    # row admitted by the census can answer UNMEASURED at the write — the lane
    # refs were readable during the walk and are not now — and appending that
    # to `refused` reports a MEASURED refusal for a row nothing measured, while
    # the unmeasured count sits at its earlier value looking complete. The
    # ladder returns its disposition for exactly this, so the bucket an
    # operator reads is the bucket the predicate chose.
    unmeasured = list(census["unmeasured"])
    for e in census["admit"]:
        lr = next((r for r in lrs if r.get("id") == e["id"]), None)
        if lr is None:
            # THE ROW LEFT THE PROJECTION, which is a read problem and not a
            # judgement: nothing measured this row and said no.
            unmeasured.append({"id": e["id"], "lane": e["lane"],
                               "why": "row left the projection between "
                                      "census and close"})
            continue
        # EVIDENCE None: the ladder records the predicate's own line, which
        # is per-row by construction — the integrator asked for an evidence
        # line per row and a single operator sentence could not have been one.
        state, row, cerr = landreq._close_expired_one(lr, None, repo or os.getcwd(),
                                              trunk, False)
        if row is None and state == landreq.EXPIRE_UNMEASURED:
            unmeasured.append({"id": e["id"], "lane": e["lane"], "why": cerr})
            continue
        (closed if row else refused).append(
            {"id": e["id"], "lane": e["lane"], "err": cerr} if cerr
            else {"id": e["id"], "lane": e["lane"]})
    out = {"closed": closed, "refused": refused,
           "unmeasured": unmeasured, "projection": source}
    if as_json:
        print(json.dumps(out, indent=1, sort_keys=True))
    else:
        for c in closed:
            print("helm lr expired: closed %s (%s)" % (str(c["id"])[:12],
                                                       c.get("lane") or "-"))
        for r in refused:
            print("helm lr expired: REFUSED %s — %s" % (str(r["id"])[:12],
                                                        r.get("err")))
        for u in unmeasured:
            print("helm lr expired: UNMEASURED %s — %s"
                  % (str(u["id"])[:12], u.get("why")))
    # AN UNMEASURED ROW IS STILL A NON-ZERO RESULT after --apply, for the same
    # reason it is one in the census: rc 0 is the only part a script reads, and
    # "some rows could not be adjudicated" must not render as a clean sweep.
    return 1 if refused or unmeasured else 0

def _cmd_retire(rest):
    """`helm lr retire` — the administrative terminal, one row or a sweep.

    THE VERB RE-RUNS EVERY MEASUREMENT ITSELF and never trusts the caller's
    claim: `--reason` selects WHICH question is asked, not what the answer
    is. A reason whose measurement finds the proof chain reachable refuses,
    and says which instrument found it reachable.

    VALIDATION FAILS SAFELY, NOT LOUDLY. The acting seat is read through
    home.chat_name, which REJECTS a control-char / bidi HELM_CHAT_NAME at the
    source rather than letting it author a ledger row. That rejection is a
    raise, and a raise crossing this boundary would hand the operator a
    traceback instead of a refusal — so it is rendered here, on BOTH the
    single-row and --sweep paths, as the bounded one-line refusal every other
    door in this verb answers with.
    """
    try:
        return landreq._cmd_retire_inner(rest)
    except home.SeatNameError as exc:
        print("helm lr retire: %s" % exc, file=sys.stderr)
        return 2

def _cmd_retire_inner(rest):
    from .cli import guard_tail, suggest
    sweep = "--sweep" in rest
    frontier = "--off-frontier" in rest
    if not (sweep or frontier) and (not rest or str(rest[0]).startswith("-")):
        print("helm lr retire: needs a land-request id, --sweep, or "
              "--off-frontier (%s)" % landreq.USAGE, file=sys.stderr)
        return 2
    rid, tail = (None, list(rest)) if (sweep or frontier) \
        else (rest[0], list(rest[1:]))
    rc = guard_tail("helm lr retire", tail,
                    flags=("--json", "--dry-run", "--sweep", "--all-projects",
                           "--off-frontier", "--apply"),
                    valued=("--reason", "--note", "--seat", "--older-than"),
                    usage=landreq.USAGE)
    if rc is not None:
        return rc

    def val(flag):
        return tail[tail.index(flag) + 1] if flag in tail else None

    as_json, dry = "--json" in tail, "--dry-run" in tail
    note, seat = val("--note"), val("--seat")
    if frontier:
        # TWO MODES, NEVER BOTH. `--sweep` classifies by REACHABILITY OF THE
        # PARTIES and writes an administrative retirement; `--off-frontier`
        # classifies by the LANE AND THE TIP and routes each row to the close
        # reason its own measurement supports. They select different
        # populations and write different terminals, so a caller asking for
        # both is asking a question with no answer.
        if sweep:
            print("helm lr retire: --off-frontier and --sweep are two "
                  "different classifications — run one (%s)" % landreq.USAGE,
                  file=sys.stderr)
            return 2
        for flag in ("--reason", "--note", "--seat", "--older-than"):
            if val(flag) is not None:
                print("helm lr retire --off-frontier: %s belongs to another "
                      "mode — this one measures every row itself and closes "
                      "through the existing reason registry (%s)"
                      % (flag, landreq.USAGE), file=sys.stderr)
                return 2
        if dry:
            print("helm lr retire --off-frontier: the census IS the default "
                  "and nothing is written without --apply, so --dry-run names "
                  "a mode that is already on (%s)" % landreq.USAGE, file=sys.stderr)
            return 2
        report, err = landreq.off_frontier_census(
            apply_it="--apply" in tail,
            all_projects="--all-projects" in tail)
        if err:
            print("helm lr retire: " + err, file=sys.stderr)
            return 1
        if as_json:
            print(json.dumps(report, ensure_ascii=False, indent=1))
        else:
            landreq._print_off_frontier(report)
        return 0
    if sweep:
        if val("--reason") is not None:
            print("helm lr retire --sweep: --reason belongs to a single-row "
                  "retire; the sweep CLASSIFIES each row itself (%s)" % landreq.USAGE,
                  file=sys.stderr)
            return 2
        raw_days = val("--older-than")
        days = landreq.RETIRE_SWEEP_DEFAULT_DAYS
        if raw_days is not None:
            days, err = landreq._retire_days(raw_days)
            if err:
                print("helm lr retire: " + err, file=sys.stderr)
                return 2
        report, err = landreq.retire_sweep(older_than_days=days, dry_run=dry,
                                   seat=seat, note=note,
                                   all_projects="--all-projects" in tail)
        if err:
            print("helm lr retire: " + err, file=sys.stderr)
            return 1
        if as_json:
            print(json.dumps(report, ensure_ascii=False, indent=1))
        else:
            landreq._print_retire_sweep(report)
        return 0
    reason = val("--reason")
    if reason is None:
        print("helm lr retire: --reason is required — one of %s (%s)"
              % ("|".join(landreq.RETIRE_REASONS), landreq.USAGE), file=sys.stderr)
        return 2
    if reason not in landreq.RETIRE_REASONS:
        print("helm lr retire: unknown --reason '%s'%s (%s)"
              % (reason, suggest(reason, landreq.RETIRE_REASONS), landreq.USAGE),
              file=sys.stderr)
        return 2
    if val("--older-than") is not None:
        print("helm lr retire: --older-than belongs to --sweep (%s)" % landreq.USAGE,
              file=sys.stderr)
        return 2
    if "--all-projects" in tail:
        print("helm lr retire: --all-projects belongs to --sweep — a single "
              "row is named by its id, and its scope is its own (%s)" % landreq.USAGE,
              file=sys.stderr)
        return 2
    out, err = landreq.retire(rid, reason, note=note, seat=seat, dry_run=dry)
    if err:
        print("helm lr retire: " + err, file=sys.stderr)
        return 1
    if as_json:
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return 0
    if dry:
        print("helm lr retire [dry-run — nothing written] %s --reason %s"
              % (out["id"][:12], out["reason"]))
        print("  measured: %s" % out["measurement"])
        return 0
    print("helm lr retire: %s RETIRED --reason %s at %s (by @%s)"
          % (out["id"][:12], out["retire_reason"], out.get("retire_ts"),
             out.get("retire_seat")))
    print("  measured: %s" % out.get("retire_measurement"))
    print("  this claims NOTHING about the work — only that helm measured "
          "the row's proof chain unreachable. It bills nothing from here.")
    return 0

def _cmd_abandon(rest):
    """Terminal judgement for a reviewed commit Git explicitly reports missing."""
    pos, opts, i = [], {}, 0
    while i < len(rest):
        arg = rest[i]
        if arg == "--json":
            if arg in opts:
                print(landreq.USAGE, file=sys.stderr)
                return 2
            opts[arg] = True
            i += 1
            continue
        if arg in ("--reason", "--repo"):
            if arg in opts or i + 1 >= len(rest) \
                    or str(rest[i + 1]).startswith("--"):
                print(landreq.USAGE, file=sys.stderr)
                return 2
            opts[arg] = rest[i + 1]
            i += 2
            continue
        if str(arg).startswith("--"):
            print(landreq.USAGE, file=sys.stderr)
            return 2
        pos.append(arg)
        i += 1
    if len(pos) != 1 or "--reason" not in opts:
        print(landreq.USAGE, file=sys.stderr)
        return 2
    lr, err = landreq.abandon(pos[0], opts["--reason"], repo=opts.get("--repo"))
    if err:
        if opts.get("--json"):
            print(json.dumps({"abandoned": False, "reason": err},
                             ensure_ascii=False, indent=1))
        else:
            print("helm lr abandon: " + err, file=sys.stderr)
        return 1
    if opts.get("--json"):
        print(json.dumps(lr, ensure_ascii=False, indent=1))
        return 0
    print("helm lr: %s ABANDONED — LAND STATE UNKNOWN; WRITES OFF REVIEWED "
          "WORK: %s" % (lr["id"][:12], lr.get("abandon_reason") or "-"))
    return 0

def _cmd_annotate_delivered_report(rest):
    """Explicit correction path for historical cancelled BUILD rows."""
    from .cli import guard_tail
    if not rest or str(rest[0]).startswith("-"):
        print(landreq.USAGE, file=sys.stderr)
        return 2
    rid, tail = rest[0], rest[1:]
    rc = guard_tail(
        "helm lr annotate-delivered-report", tail, flags=("--json",),
        valued=("--artifact-ref", "--report-ref", "--evidence"), usage=landreq.USAGE)
    if rc is not None:
        return rc

    def val(flag):
        return tail[tail.index(flag) + 1] if flag in tail else None

    if any(val(flag) is None for flag in (
            "--artifact-ref", "--report-ref", "--evidence")):
        print("helm lr annotate-delivered-report: --artifact-ref REF, "
              "--report-ref CHAT_REF, and --evidence LINE are required (%s)"
              % landreq.USAGE, file=sys.stderr)
        return 2
    out, err = landreq.annotate_delivered_report(
        rid, val("--artifact-ref"), val("--report-ref"), val("--evidence"))
    if err:
        if "--json" in tail:
            print(json.dumps({"annotated": False, "reason": err},
                             ensure_ascii=False, indent=1))
        else:
            print("helm lr annotate-delivered-report: " + err, file=sys.stderr)
        return 1
    if "--json" in tail:
        print(json.dumps(out, ensure_ascii=False, indent=1))
    else:
        print("helm lr: %s historical cancellation annotated CLOSED "
              "(DELIVERED_REPORT)" % out["id"][:12])
    return 0

def _cmd_close(rest):
    """helm lr close <id> --reason R — the one terminal verb. Exit contract:
    0 recorded / idempotent, 1 refusal, 2 usage (missing/unknown --reason,
    flag-reason incoherence, superseded without --tip)."""
    from .cli import guard_tail, suggest
    if not rest or str(rest[0]).startswith("-"):
        print(landreq.USAGE, file=sys.stderr)
        return 2
    rid, tail = rest[0], rest[1:]
    rc = guard_tail("helm lr close", tail, flags=("--json", "--dry-run",
                                                  "--live"),
                    valued=("--reason", "--evidence", "--artifact-ref",
                            "--report-ref", "--tip", "--repo", "--trunk",
                            "--needs-restart", "--attest", "--compose-manifest",
                            "--compose-gate"), usage=landreq.USAGE)
    if rc is not None:
        return rc

    def val(flag):
        return tail[tail.index(flag) + 1] if flag in tail else None

    reason, as_json, dry = val("--reason"), "--json" in tail, "--dry-run" in tail
    if reason is None:
        print("helm lr close: --reason is required (%s)" % landreq.USAGE,
              file=sys.stderr)
        return 2
    if reason not in landreq.CLOSE_CLI_REASONS:
        print("helm lr close: unknown --reason '%s'%s (%s)"
              % (reason, suggest(reason, landreq.CLOSE_CLI_REASONS), landreq.USAGE),
              file=sys.stderr)
        return 2
    if val("--tip") is not None and reason != "superseded":
        print("helm lr close: --tip belongs to --reason superseded (%s)"
              % landreq.USAGE, file=sys.stderr)
        return 2
    if reason == "superseded" and val("--tip") is None:
        print("helm lr close: --reason superseded requires --tip FULL_SHA "
              "(%s)" % landreq.USAGE, file=sys.stderr)
        return 2
    # `carried` JOINS THIS SET RATHER THAN GROWING A SECOND FLAG (task/756).
    # Its whole question is "does THIS repository's trunk carry the work",
    # and --repo/--trunk are already how an operator names a repository and a
    # trunk that are not the cwd's. That is also the ONLY honest form the
    # cross-repository case takes: the proof is computed IN the named repo,
    # against ITS trunk, from objects that must resolve THERE — never
    # stretched across two repositories.
    # DERIVED FROM THE SET IT POLICES, never restated beside it. The tuple
    # already admitted `chain-proof` and the sentence still named four reasons,
    # so the door and the message disagreed about the door — an operator who
    # believed the message would never try the flag that works.
    if (val("--repo") is not None or val("--trunk") is not None) \
            and reason not in landreq.REPO_TRUNK_REASONS:
        print("helm lr close: --repo/--trunk belong to --reason %s (%s)"
              % ("/".join(landreq.REPO_TRUNK_REASONS), landreq.USAGE), file=sys.stderr)
        return 2
    if val("--attest") is not None and reason != "chain-proof":
        print("helm lr close: --attest belongs to --reason chain-proof — it "
              "admits an UNMEASURABLE approval tier on the record, and no "
              "other terminal has a tier to be unmeasurable about (%s)"
              % landreq.USAGE, file=sys.stderr)
        return 2
    live, restart = "--live" in tail, val("--needs-restart")
    if (live or restart is not None) and reason != "landed":
        print("helm lr close: --live/--needs-restart belong to --reason "
              "landed — no other terminal claims a change reached the "
              "running fleet (%s)" % landreq.USAGE, file=sys.stderr)
        return 2
    refs = (val("--artifact-ref"), val("--report-ref"))
    if reason == "delivered-report" \
            and (not all(refs) or val("--evidence") is None):
        print("helm lr close: --reason delivered-report requires --artifact-ref "
              "REF, --report-ref CHAT_REF, and --evidence LINE (%s)" % landreq.USAGE,
              file=sys.stderr)
        return 2
    if reason != "delivered-report" and any(ref is not None for ref in refs):
        print("helm lr close: --artifact-ref/--report-ref belong to --reason "
              "delivered-report (%s)" % landreq.USAGE, file=sys.stderr)
        return 2
    compose_options = {}
    if val("--compose-manifest") is not None or val("--compose-gate") is not None:
        if reason != "landed" or not val("--compose-manifest") or not val("--compose-gate"):
            print("helm lr close: --compose-manifest and --compose-gate belong "
                  "together to --reason landed", file=sys.stderr)
            return 2
        compose_options = {"compose_manifest": val("--compose-manifest"),
                           "compose_gate": val("--compose-gate")}
    out, err = landreq.close(rid, reason, evidence=val("--evidence"),
                     tip=val("--tip"), repo=val("--repo"),
                     trunk=val("--trunk"), dry_run=dry,
                     live=live, needs_restart=restart,
                     artifact_ref=val("--artifact-ref"),
                     report_ref=val("--report-ref"),
                     attest=val("--attest"),
                     attester=landreq._acting_seat(), **compose_options)
    if err:
        # D10c: a dry-run refusal prints the refusing rung's EXACT message
        # with the mirrored exit — and appends nothing, same as the live path.
        if as_json:
            print(json.dumps({"closed": False, "dry_run": dry, "reason": err},
                             ensure_ascii=False, indent=1))
        else:
            print("helm lr close%s: %s" % (" [dry-run]" if dry else "", err),
                  file=sys.stderr)
        return 1
    # `close()` owns the normalized result, and this boundary checks the whole
    # object before either renderer spends it. Caller intent is not proof that
    # nothing appended; a malformed or contradictory success refuses.
    contract = None
    if not isinstance(out, dict) or not out.get("id"):
        contract = "close returned an invalid success result"
    elif dry and (out.get("dry_run") is not True
                  or out.get("reason") != reason):
        contract = "close returned a result that does not bind this dry run"
    elif not dry and out.get("dry_run"):
        contract = "close returned a dry-run result for a live close"
    if contract:
        if as_json:
            print(json.dumps({"closed": False, "dry_run": dry,
                              "reason": contract},
                             ensure_ascii=False, indent=1))
        else:
            print("helm lr close: " + contract, file=sys.stderr)
        return 1
    if as_json:
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return 0
    # Dry-run state belongs to the caller and the normalized result contract,
    # not to any individual proof ladder.
    if dry:
        proof = out.get("proof_mode") or out.get("close_proof_mode")
        detail = "%s%s" % (
            " (proof %s)" % proof if proof else "",
            " (%s)" % landreq._delivery_text(
                out.get("close_delivery_class"),
                out.get("close_delivery_restart"))
            if out.get("close_delivery_class") else "")
        if out.get("would_close_siblings"):
            print("helm lr close [dry-run]: this land also closes %d "
                  "row(s) — peers bound to the same tip, and chain "
                  "predecessors inside the landed head: %s"
                  % (len(out["would_close_siblings"]),
                     ", ".join(i[:12] for i in out["would_close_siblings"])))
        if out.get("would_leave_open_siblings"):
            print("helm lr close [dry-run]: %d row(s) the sweep reaches "
                  "STAY OPEN — a FIX/SUPERSEDE peer is debt, not a "
                  "duplicate, and a held rung without a source-clean hold "
                  "owes a verdict: %s"
                  % (len(out["would_leave_open_siblings"]),
                     ", ".join(i[:12]
                               for i in out["would_leave_open_siblings"])))
        if out.get("idempotent"):
            print("helm lr close [dry-run]: %s ALREADY closed — --reason %s%s; "
                  "no append needed" % (out["id"][:12], reason, detail))
        else:
            print("helm lr close [dry-run]: %s WOULD close — --reason %s%s; "
                  "nothing appended" % (out["id"][:12], reason, detail))
        return 0
    if reason == "out-of-scope":
        print("helm lr: %s closed out-of-scope — cancelled: %s"
              % (out["id"][:12], out.get("cancel_reason") or ""))
        return 0
    # THE LABEL COMES FROM THE CALLER'S REASON, and the result may only
    # SHARPEN it. Reading it from `out` alone is the same inversion the
    # dry-run marker fix removed one branch above: a ladder that returns a
    # summary without close_reason then prints "CLOSED ()" — a terminal report
    # with the terminal missing. _close_label wins when it has something more
    # specific to say (LANDED_REWRITTEN); otherwise the caller's word stands.
    label = landreq._close_label(out) or reason.upper()
    print("helm lr: %s CLOSED (%s)%s" % (
        str(out.get("id") or rid)[:12], label,
        "  %s" % landreq._delivery_phrase(out)
        if out.get("close_delivery_class") else ""))
    # THE FAN-OUT IS PART OF THE ACTION, SO IT IS PART OF THE REPORT. A land
    # that silently closes three rows is exactly as unreadable as one that
    # silently closes none — the operator has to be able to say which rows
    # left the board and why.
    # THE AUTHORS, ON THE LINE THAT SAYS THE WORK LANDED. Printed whenever the
    # chain records more than one, because one name is the ordinary case and a
    # line saying "AUTHORS x" on every close is noise that trains the reader to
    # skip the line that matters.
    credited = landreq.credit_line(out.get("credited_authors"))
    if credited:
        print(credited)
    for peer in out.get("closed_siblings") or ():
        print("  ALSO CLOSED %s (%s) — %s, lane %s"
              % (str(peer.get("id"))[:12], peer.get("reason"),
                 peer.get("relation") or "same reviewed tip",
                 peer.get("lane") or "-"))
    for peer in out.get("sibling_refusals") or ():
        print("  STILL OPEN %s — %s"
              % (str(peer.get("id"))[:12], peer.get("why")))
    if out.get("witness_unknown"):
        # THE OPPOSITE PRESCRIPTION FROM THE ONE BELOW, and that is the whole
        # point of the split. Below, no receipt exists and minting is the
        # cure. Here, whether one exists is UNKNOWN, so minting is the one
        # move that can make the record strictly worse — a second signed row
        # beside an unreadable or conflicted one.
        print("  WITNESS UNKNOWN — helm could not establish whether this land "
              "is already witnessed: %s\n"
              "  This is NOT a report that the land is unwitnessed. A receipt "
              "may well exist; the record could not be read to say.\n"
              "  DO NOT run `helm lr land` to 'fix' this — minting beside an "
              "unreadable or conflicted record is how one land ends up with "
              "two signed rows. Read %s first and repair the record; there is "
              "deliberately no verb that mints past this state."
              % (out["witness_unknown"], landreq.RECEIPTS))
    elif out.get("unwitnessed"):
        # STDOUT, BESIDE THE CLOSE IT BELONGS TO — not stderr. The close
        # SUCCEEDED; an unwitnessed receipt is a status of that success, not
        # a diagnostic from a failed command, and putting it on stderr made
        # eight tests that assert a clean stderr fail for the right reason.
        # The `--json` arm above needs no change: the field rides `out`.
        print("  NOT WITNESSED — no land receipt was written: %s\n"
              "  The land is real; receipting is fail-open on purpose. What "
              "it costs is the AUDIT TRAIL: no signed attestation binds this "
              "land's content identity.\n"
              "  Mint it once the cause is cleared: helm lr land %s"
              % (out["unwitnessed"], out["id"][:12]))
    return 0

def _cmd_close_landed(rest):
    """Proof-gated terminal closure — deprecated alias of lr close."""
    print("helm lr close-landed is deprecated: use helm lr close --reason "
          "landed", file=sys.stderr)
    pos, opts, i = [], {}, 0
    while i < len(rest):
        arg = rest[i]
        if arg in ("--json", "--live"):
            if arg in opts:
                print(landreq.USAGE, file=sys.stderr)
                return 2
            opts[arg] = True
            i += 1
            continue
        if arg in ("--repo", "--trunk", "--needs-restart"):
            if arg in opts or i + 1 >= len(rest) \
                    or str(rest[i + 1]).startswith("--"):
                print(landreq.USAGE, file=sys.stderr)
                return 2
            opts[arg] = rest[i + 1]
            i += 2
            continue
        if str(arg).startswith("--"):
            print(landreq.USAGE, file=sys.stderr)
            return 2
        pos.append(arg)
        i += 1
    if len(pos) != 1 or "--trunk" not in opts:
        print(landreq.USAGE, file=sys.stderr)
        return 2
    lr, err = landreq.close_landed(pos[0], repo=opts.get("--repo"),
                           trunk=opts["--trunk"],
                           live=bool(opts.get("--live")),
                           needs_restart=opts.get("--needs-restart"))
    if err:
        if opts.get("--json"):
            print(json.dumps({"closed": False, "reason": err},
                             ensure_ascii=False, indent=1))
        else:
            print("helm lr close-landed: " + err, file=sys.stderr)
        return 1
    if opts.get("--json"):
        print(json.dumps(lr, ensure_ascii=False, indent=1))
        return 0
    print("helm lr: %s %s — CLOSED (%s) via %s at %s@%s" % (
        lr["id"][:12],
        "REVIEWED (UNDECLARED)" if lr.get("polarity") is None
        else lr["state"],
        landreq._close_label(lr) if lr.get("close_reason") else "BY LANDING",
        lr.get("close_proof_mode") or lr.get("landing_proof_mode"),
        lr.get("closing_trunk_ref") or lr.get("landing_trunk_ref"),
        (lr.get("closing_trunk_sha") or lr.get("landing_trunk_sha")
         or "")[:12]))
    return 0

def _cmd_discharge(rest):
    """Proof-gated contrary reconciliation — deprecated alias of lr close."""
    print("helm lr discharge is deprecated: use helm lr close --reason "
          "superseded", file=sys.stderr)
    flags = [arg for arg in rest if str(arg).startswith("--")]
    pos = [arg for arg in rest if not str(arg).startswith("--")]
    if any(flag != "--json" for flag in flags) or len(flags) > 1 or len(pos) < 3:
        print(landreq.USAGE, file=sys.stderr)
        return 2
    lr, err = landreq.discharge(pos[0], pos[1], " ".join(pos[2:]))
    if err:
        if "--json" in flags:
            print(json.dumps({"discharged": False, "reason": err},
                             ensure_ascii=False, indent=1))
        else:
            print("helm lr discharge: " + err, file=sys.stderr)
        return 1
    if "--json" in flags:
        print(json.dumps(lr, ensure_ascii=False, indent=1))
        return 0
    print("helm lr: discharged contrary %s via approved tip %s" % (
        lr["id"][:12], (lr.get("superseding_tip") or "")[:12]))
    return 0

def _cmd_withdraw(rest):
    """Proof-gated do-not-land reconciliation — deprecated alias of lr close."""
    print("helm lr withdraw is deprecated: use helm lr close --reason "
          "withdrawn", file=sys.stderr)
    flags = [arg for arg in rest if str(arg).startswith("--")]
    pos = [arg for arg in rest if not str(arg).startswith("--")]
    if any(flag != "--json" for flag in flags) or len(flags) > 1 or len(pos) < 2:
        print(landreq.USAGE, file=sys.stderr)
        return 2
    lr, err = landreq.withdraw(pos[0], " ".join(pos[1:]))
    if err:
        if "--json" in flags:
            print(json.dumps({"withdrawn": False, "reason": err},
                             ensure_ascii=False, indent=1))
        else:
            print("helm lr withdraw: " + err, file=sys.stderr)
        return 1
    if "--json" in flags:
        print(json.dumps(lr, ensure_ascii=False, indent=1))
        return 0
    print("helm lr: withdrew %s — the verdict stands, the lane is abandoned" %
          lr["id"][:12])
    return 0

def _cmd_compose(rest):
    """helm lr compose <id> [<id>...] — stand N APPROVED lanes on ONE composed
    tip, measuring per member that the approved CONTENT is what composed
    (the merge-queue owner directive, lane merge-queue-adopt-the-lazy-hybrid).

    THE EVIDENCE SPLIT IT MECHANIZES: an APPROVE binds CONTENT (patch-id), a
    gate binds a TREE. Compose re-measures each member's patch-id across the
    cherry-pick and REFUSES on conflict or drift, NAMING the member — "the
    batch is bad" is not an actionable verdict. It runs NO suite: the one
    gate the caller then runs on the composed tip is the only per-tree
    evidence there is. File-disjointness predicts a clean compose and
    licenses nothing about evidence.

    The room is a detached worktree under `<repo>-wt/compose/` — the peek
    container's shape, but WRITABLE and left standing on success, because its
    HEAD is what keeps the composed tip reachable until the integrator gates
    and lands it (no branch is minted: the shared-checkout guard reserves
    branch creation for the integrator). --dry-run composes, measures,
    reports, and removes the room. On a later red gate, localization is by
    PREFIX: re-run compose with the first K members — the longest green
    prefix lands, the first red member is evicted to its own re-gate."""
    pos, opts, i = [], {}, 0
    while i < len(rest):
        arg = rest[i]
        if arg in ("--dry-run", "--json", "--stop-on-first", "--bounded-concur"):
            if arg in opts:
                print(landreq.USAGE, file=sys.stderr)
                return 2
            opts[arg] = True
            i += 1
            continue
        if arg in ("--trunk", "--repo"):
            if arg in opts or i + 1 >= len(rest) \
                    or str(rest[i + 1]).startswith("--"):
                print(landreq.USAGE, file=sys.stderr)
                return 2
            opts[arg] = rest[i + 1]
            i += 2
            continue
        if str(arg).startswith("--"):
            print(landreq.USAGE, file=sys.stderr)
            return 2
        pos.append(arg)
        i += 1
    if not pos:
        print(landreq.USAGE, file=sys.stderr)
        return 2

    def refuse_batch(msg, members=None, room_kept=None):
        """The all-excluded / failed-batch report. A FIX on the reviewed tip:
        four early returns printed prose to stderr and returned BEFORE the
        --json branch, so a scripted caller got empty stdout and a
        JSONDecodeError — the silent-empty disease cured for the human and
        preserved for the machine. rc stays 1 (a failed batch is a failed
        batch); the account is parseable in BOTH modes."""
        if "--json" in opts:
            print(json.dumps({"composed_tip": None, "refused": msg,
                              "members": members or [],
                              "excluded": excluded,
                              "room": room_kept, "dry_run": "--dry-run" in opts},
                             ensure_ascii=False, indent=1))
        else:
            print("helm lr compose: " + msg, file=sys.stderr)
            for x in excluded:
                print("  EXCLUDED %s (%s): %s" % (x["id"][:12], x["lane"],
                                                  x["reason"]),
                      file=sys.stderr)
        return 1
    # ONE projection for every id: get() pays the whole-ledger hydration per
    # call (measured 2026-08-05, 164s; cured the same night by the lr-show-hydrates-the-whole-ledger lane), so an N-member
    # compose must never multiply it.
    raw_rows = None
    if "--bounded-concur" in opts:
        from . import compose_contract
        err = compose_contract.writer_error()
        if err:
            print("helm lr compose: " + err, file=sys.stderr)
            return 1
        lrs, raw_rows, unavailable = landreq.project_raw()
    else:
        lrs, unavailable = landreq.project()
    if unavailable:
        print("helm lr: " + unavailable, file=sys.stderr)
        return 1
    picks = []
    excluded = []
    for rid in pos:
        lr, err = dispatches._resolve_row(lrs, rid, noun="land request",
                                          list_hint="helm lr list", allow_retired=True)
        if err:
            excluded.append({"id": rid, "lane": "?", "unresolvable": True,
                             "reason": err})
            continue
        # THE PROJECTION'S OWN WORD, never raw polarity: a gate-capable
        # writer's untokened approve projects REVIEWED, not READY, and raw
        # polarity=approve would compose it anyway (a FIX, the
        # compose-verb-slice-1-review chain). READY is the door's authorization word; the reader
        # rungs (ready_word) are printed as caution, not re-judged here —
        # the land door still guards the land.
        bounded = None
        if "--bounded-concur" in opts and lr.get("polarity") == "concur":
            if landreq._retired_by(lr) or str(lr.get("state")) != "REVIEWED":
                berr = "bounded CONCUR requires a live REVIEWED row"
            else:
                bounded, berr = compose_contract.admission(
                    (raw_rows or {}).get(lr["id"], {}), raw_rows, lr.get("repo_id"))
            if berr:
                excluded.append({"id": lr["id"], "lane": lr.get("lane") or "?",
                                 "reason": berr, "state": lr.get("state")})
                continue
        if str(lr.get("state")) != "READY" and bounded is None:
            # SAY WHAT THIS PROJECTION ITSELF READ, and say WHICH KIND of
            # reading it was. Measured twice 2026-08-11 (task/1067): an approve
            # the DM had already called ready refused here as REVIEWED and the
            # identical command minutes later admitted it — a transient
            # approval-tier resolution projects REVIEWED with `tier`/`ungated`
            # on the row, and this refusal printed only the state word, so the
            # caller paid a second ~8-minute compose to learn it was nothing.
            #
            # THE FIRST CURE FOR THAT WAS ITSELF THE BUG. It said "heals on
            # re-read" for EVERY held approve, and on the live roster that
            # sentence was true of none of them: six seats carry a malformed
            # proxywatch record, five store no proof at all because their
            # upstream is dark, and an OUTSIDE reading is a measurement that
            # never heals by definition. Vague cost one compose; confident
            # and wrong costs a night. So the disclosure branches on the axis
            # the row already carries, and each branch ends with the cure for
            # THAT world and no other.
            #
            # A row refused with no tier hold at all keeps the plain sentence:
            # it is a genuine polarity/lifecycle refusal and the tier is not
            # its subject.
            why = ("state is %s — compose takes only READY rows (an "
                   "authorizing approve, per the projection)"
                   % (lr.get("state") or "UNKNOWN"))
            if lr.get("polarity") == "approve" and lr.get("ungated"):
                tier, kind = lr.get("tier"), lr.get("tier_kind")
                if tier in ("outside", "unknown"):
                    # SAY IT ONCE. `ungated` IS the authoritative sentence for
                    # this axis and _approval_refusal already ended it with
                    # THIS kind's cure, so that `lr show` and the land door
                    # say what compose says. Appending the cure again here
                    # printed the whole paragraph twice — the first draft of
                    # this branch did exactly that, and a refusal nobody can
                    # read to the end discriminates nothing.
                    tail = ""
                else:
                    # tier ok/none: the hold is on the APPROVAL's own
                    # verification (no gate_caps stamp, no minted receipt), not
                    # on who reviewed — and `ungated` says so without the tier
                    # framing, so this is the one branch with something to add.
                    tail = (" The tier read fine (tier=%s), so the hold is on "
                            "this APPROVAL's own verification and not on the "
                            "reviewer; that does not clear on a re-read "
                            "either — the reason above names what to run or "
                            "repair." % tier)
                # NO RE-MEASURE PROMISE IN THE TRAILER. It used to say `lr
                # show` "re-measures before any re-review is routed" on EVERY
                # branch, including the ones whose own sentence had just
                # explained that re-measuring reaches nothing.
                why += ("; this projection's own reading: tier=%s%s — %s.%s "
                        "`helm lr show %s` prints this row's reading"
                        % (tier, "/" + kind if kind else "", lr["ungated"],
                           tail, lr["id"][:12]))
            excluded.append({"id": lr["id"], "lane": lr.get("lane") or "?",
                             "reason": why, "state": lr.get("state"),
                             "polarity": lr.get("polarity"),
                             "tier": lr.get("tier"),
                             "tier_kind": lr.get("tier_kind"),
                             "ungated": lr.get("ungated")})
            continue
        word = landreq.ready_word(lr) if bounded is None else "READY"
        if word != "READY":
            print("helm lr compose: caution — %s is %s; composing anyway, "
                  "the land door verifies the gate leg"
                  % (lr["id"][:12], word), file=sys.stderr)
        tip = lr.get("reviewed_tip") or lr.get("review_sha")
        if not tip:
            excluded.append({"id": lr["id"], "lane": lr.get("lane") or "?",
                             "reason": "has no reviewed tip to compose"})
            continue
        if bounded is not None and any(p["tip"] == tip for p in picks):
            excluded.append({"id": lr["id"], "lane": lr.get("lane") or "?",
                             "reason": "same tip already selected; close peers independently after composition"})
            continue
        picks.append({"lr": lr, "tip": tip, "bounded": bounded})
    bindings = set(p["lr"].get("repo_id") for p in picks
                   if p["lr"].get("repo_id"))
    if len(bindings) > 1:
        print("helm lr compose: members bind DIFFERENT repos (%s) — one "
              "composition, one repository" % ", ".join(sorted(bindings)),
              file=sys.stderr)
        return 1
    if not picks:
        return refuse_batch("no member is composable — every row refused "
                            "at admission")
    bound = bindings.pop() if bindings else None
    root = opts.get("--repo") or (
        bound[:-5] if isinstance(bound, str) and bound.endswith("/.git")
        else bound)
    if not root or not os.path.isdir(root):
        print("helm lr compose: no usable repository (row binding %r; "
              "--repo overrides)" % (bound,), file=sys.stderr)
        return 1
    be = vcs.backend(root)
    scoped_batch = any(p["bounded"] is not None for p in picks)
    # --repo is a Git working path, not a registry identity. Worktrees and
    # subdirectories belong to the same canonical project as their main root.
    project_root = _lanes.find_root(root)
    project_state, compose_project = foldcompose.project_state(project_root)
    if project_state == "unknown":
        return refuse_batch("canonical compose project registry is UNKNOWN")
    if scoped_batch and (project_state != "registered" or
            foldcompose.activation_state(compose_project)[0] != foldcompose.ACTIVATION_ACTIVE):
        return refuse_batch("bounded compose requires a registered project with readable active composition proof")
    ref = opts.get("--trunk")
    if not ref:
        for cand in landreq.UPSTREAM_TRUNK + landreq.LOCAL_TRUNK:
            rc, out, _e = be.text(root, *landreq._REF_ARGV,
                                  cand)
            if rc == 0 and out:
                ref = cand
                break
    if not ref:
        print("helm lr compose: trunk unresolvable in %s (--trunk names one)"
              % root, file=sys.stderr)
        return 1
    rc, out, _e = be.text(root, "rev-parse", ref)
    if rc != 0 or not out:
        print("helm lr compose: trunk ref %s unreadable" % ref,
              file=sys.stderr)
        return 1
    trunk_sha = out
    for p in list(picks):
        def drop(reason):
            excluded.append({"id": p["lr"]["id"],
                             "lane": p["lr"].get("lane") or "?",
                             "reason": reason})
            picks.remove(p)
        rc, _o, _e = be.text(root, "cat-file", "-e", p["tip"] + "^{commit}")
        if rc != 0:
            drop("reviewed tip %s is not readable here" % p["tip"][:12])
            continue
        rc, out, _e = be.text(root, "merge-base", trunk_sha, p["tip"])
        if rc != 0 or not out:
            drop("tip %s shares no history with %s" % (p["tip"][:12], ref))
            continue
        if out == p["tip"]:
            drop("tip %s is already contained in %s — close it, do not "
                 "compose it" % (p["tip"][:12], ref))
            continue
        p["mb"] = out
        if p["bounded"] is not None:
            gitdir, berr = landreq._close_repo(p["lr"], root)
            bounded, err = compose_contract.admission(
                (raw_rows or {}).get(p["lr"]["id"], {}), raw_rows, gitdir) \
                if not berr else (None, berr)
            if err or bounded != p["bounded"] \
                    or p["mb"] != bounded["contract"]["base"]:
                drop(err or "compose range differs from the immutable bounded range")
                continue
        rc, out, _e = be.text(root, "rev-list", p["mb"] + ".." + p["tip"])
        if rc != 0 or not out:
            drop("approved range is unreadable — nothing to carry")
            continue
        p["range_shas"] = out.split()
        # The RICH owner, not the adapter: Compose's newline veto needs the
        # third axis, and consuming the 2-tuple adapter here would bypass it.
        p["approved_ids"] = [landreq._commit_content_identity(root, s)
                             for s in p["range_shas"]]
        if not all(p["approved_ids"]):
            drop("a commit in the approved range has an unmeasurable "
                 "content id — nothing to carry")
            continue
        p["approved"] = landreq._range_patch_id(root, p["mb"], p["tip"])
    if not picks:
        return refuse_batch("no member is composable — every row refused "
                            "at pre-flight")
    # A REBASED land leaves a row whose tip is NO trunk ancestor while its
    # CONTENT is on trunk — ancestry stays silent and the cherry-pick then
    # mis-frames ledger residue as a live conflict (measured on the very
    # first live run: gateroute, landed rebased minutes earlier). Patch
    # identity is the discriminator; the rung acts only on a POSITIVE hit —
    # an unreadable or INCOMPLETE index proves nothing and falls through to
    # pick, whose conflict refusal still backstops.
    gd = bound if isinstance(bound, str) and bound.endswith("/.git") \
        else root.rstrip(os.sep) + os.sep + ".git"
    index, why = landreq._stored_patch_index(gd, trunk_sha)

    def range_commit_pids(p):
        """{sha: patch-id} for the member's own range, streamed in two
        spawns — the SCREEN must speak the index's per-commit shape: a
        multi-commit lane rebased in lands as N trunk commits with N
        per-commit ids, and the aggregate id matches none of them
        (FIX: the screen missed exactly the class it was built for)."""
        rc, out, _e = be.text(root, "rev-list", p["mb"] + ".." + p["tip"])
        if rc != 0 or not out:
            return None
        shas = out.split()
        rc, raw, _e = be.run(root, "-c", "diff.noprefix=false", "log", "-p",
                             p["mb"] + ".." + p["tip"])
        if rc != 0 or not raw:
            return None
        rc, ids, _e = be.text(root, "patch-id", "--stable", stdin=raw)
        if rc != 0 or not ids:
            return None
        got = {}
        for line in ids.splitlines():
            parts = line.split()
            if len(parts) == 2:
                got[parts[1]] = parts[0]
        return got if len(got) == len(shas) else None
    # A skipped screen must stay VISIBLE: unreadable-or-incomplete means an older
    # rebased land is NOT ruled out, and the conflict refusal below would
    # then mis-frame residue with full confidence — so that refusal carries
    # the screen's status instead of inheriting its silence.
    # THE STATUS NAMES THE ACTUAL CAUSE. It said "capped at N commits" for
    # every reason the window could fall short, so a screen skipped because a
    # commit could not be hashed reported a bound that was never reached.
    screen = "unreadable" if index is None else (
        landreq._INDEX_SCREEN_WHY.get(why, "incomplete") if why else "")
    for p in list(picks):
        pids = range_commit_pids(p) if index is not None else None
        if pids is None and index is not None and not screen:
            screen = "member range unreadable"
        if pids and len(set(pids.values())) < len(pids):
            # two range commits with one patch-id (revert/reapply, duplicated
            # cherry-pick): the trunk index maps each id to ONE sha, so both
            # copies would "hit" the same commit and ALREADY ON would
            # overclaim — the screen declines and says why (FIX r2)
            pids = None
            if not screen:
                screen = "duplicate patch-ids in member range"
        landed = [(index or {}).get(pid) for pid in (pids or {}).values()]
        if pids and all(landed):
            excluded.append({"id": p["lr"]["id"],
                             "lane": p["lr"].get("lane") or "?",
                             "reason": "content is ALREADY ON %s (%d commit%s "
                                       "by patch-identity; its tip %s was "
                                       "rebased in) — close it, do not "
                                       "compose it"
                                       % (ref, len(landed),
                                          "" if len(landed) == 1 else "s",
                                          p["tip"][:12])})
            picks.remove(p)
            continue
        if pids and any(landed):
            hits = sum(1 for x in landed if x)
            if why:
                excluded.append({"id": p["lr"]["id"],
                                 "lane": p["lr"].get("lane") or "?",
                                 "reason": "%d of %d commits are provably on "
                                           "%s by patch-identity and the rest "
                                           "are UNPROVABLE (screen %s) — an "
                                           "older land is not ruled out; "
                                           "resolve before composing"
                                           % (hits, len(landed), ref,
                                              landreq._INDEX_SCREEN_WHY.get(
                                                  why, "incomplete"))})
                picks.remove(p)
                continue
            excluded.append({"id": p["lr"]["id"],
                             "lane": p["lr"].get("lane") or "?",
                             "reason": "PARTIALLY on %s — %d of %d commits "
                                       "match trunk by patch-identity; that "
                                       "state must keep its branch — resolve "
                                       "the split before composing"
                                       % (ref, hits, len(landed))})
            picks.remove(p)
            continue
    if not picks:
        return refuse_batch("no member is composable — every row was "
                            "excluded")
    box = (project_root or root).rstrip(os.sep) + "-wt" + os.sep + "compose"
    room = os.path.join(box, "+".join(p["lr"]["id"][:4] for p in picks))
    if os.path.exists(room):
        print("helm lr compose: room %s already exists — land it or remove "
              "it first (its HEAD may be the only anchor of a prior "
              "composition)" % room, file=sys.stderr)
        return 1
    os.makedirs(box, exist_ok=True)
    # The shared-checkout ref guard refuses worktree/HEAD creation from
    # anything but the sanctioned creator (measured live: `worktree add`
    # REFUSED, "ref updates aborted by hook"). Compose IS integration
    # staging, so it hands the guard that bit the way vcs.run documents —
    # scoped to this one call, never leaked into the process.
    sanction = {"HELM_WORK_INTEGRATOR": "1"}
    rc, _o, err = be.text(root, "worktree", "add", "--detach", room,
                          trunk_sha, env=sanction)
    if rc != 0:
        print("helm lr compose: cannot mint the compose room: %s"
              % (err or "worktree add failed"), file=sys.stderr)
        return 1

    def scrap(msg):
        # --abort only when a pick is actually IN PROGRESS: scrap also fires
        # on post-pick failures (drift, unreadable HEAD) where --abort has
        # nothing to abort and its rc says nothing about the room — the
        # unconditional form stamped 'abort failed / room may hold' onto
        # refusals whose room was already clean (FIX r3, reproduced
        # on the drift refusal's own output)
        rc, _o, _e = be.text(room, *landreq._REF_ARGV, "CHERRY_PICK_HEAD")
        abort_err = None
        if rc == 0:
            rc, _o, err = be.text(room, "cherry-pick", "--abort")
            if rc != 0:
                abort_err = ((err or "no diagnostic").splitlines()[0]
                             if err else "no diagnostic")
        rc, _o, err = be.text(root, "worktree", "remove", "--force", room,
                              env=sanction)
        # The message derives from the FINAL state, never an intermediate
        # one: an abort failure followed by a successful forced removal
        # leaves NO room, and saying 'may hold' over an absent room is the
        # r3 defect one path over (FIX r4).
        if rc != 0:
            msg += (" — %sthe room resisted removal (%s); %s may hold a "
                    "conflicted half-pick, remove it by hand"
                    % ("cherry-pick --abort failed (%s) AND " % abort_err
                       if abort_err else "AND ",
                       (err or "no diagnostic").splitlines()[0]
                       if err else "no diagnostic", room))
        elif abort_err:
            msg += (" — cherry-pick --abort failed (%s) but the forced "
                    "removal succeeded; no room remains" % abort_err)
        # scrap carries the cleanup-halt now, so it owes the same
        # parseable account as every other refusal — a scripted integrator
        # watching --json must see the room-integrity stop, not empty
        # stdout on the one failure that most needs parsing.
        if "--json" in opts:
            print(json.dumps({"composed_tip": None, "refused": msg,
                              "members": members, "excluded": excluded,
                              # Derive from the SAME rc the message
                              # branched on — a resisted removal tells the
                              # human "the room may hold" and must not tell
                              # the machine there is no room.
                              "room": room if rc != 0 else None,
                              "dry_run": "--dry-run" in opts},
                             ensure_ascii=False, indent=1))
        else:
            print("helm lr compose: " + msg, file=sys.stderr)
        return 1

    members, head = [], trunk_sha
    canonical_cars = []
    stop_first = "--stop-on-first" in opts

    def evict(p, reason):
        """Exclude one member mid-compose and roll the room back to the last
        good head, so LATER MEMBERS STILL GET A VERDICT (task/165, owner:
        'forloop over open lanes'). The default is best-effort at any N — a
        batch that reports on 2 of 8 reads as a batch of 2 at 2am, which is
        the silent-exclusion failure this verb exists to end. Fail-fast is
        the opt-in (--stop-on-first), never the default.

        EVERY cleanup step's rc is checked (a FIX on the reviewed tip): the first
        cut checked --abort and discarded reset/clean — so a failed reset
        left member A's residue in the room, member B's pick then failed
        against THAT, and B was evicted naming B's OWN files as the
        conflict. A cleanup failure stops the batch with the room state
        UNKNOWN rather than letting later verdicts measure a dirty room."""
        note = ""
        rc, _o, _e = be.text(room, *landreq._REF_ARGV, "CHERRY_PICK_HEAD")
        if rc == 0:
            rc, _o, err = be.text(room, "cherry-pick", "--abort")
            if rc != 0:
                note = (" [cleanup: cherry-pick --abort failed (%s); "
                        "reset --hard continued over it]"
                        % ((err or "no diagnostic").splitlines()[0]
                           if err else "no diagnostic"))
        rc, _o, err = be.text(room, "reset", "--hard", "-q", head)
        if rc != 0:
            return ("reset --hard to the last good head FAILED (%s) — the "
                    "room's state is UNKNOWN and later verdicts would "
                    "measure residue, so the batch stops here"
                    % ((err or "no diagnostic").splitlines()[0]
                       if err else "no diagnostic"))
        rc, _o, err = be.text(room, "clean", "-fdq")
        if rc != 0:
            return ("git clean FAILED (%s) — the room's state is UNKNOWN "
                    "and later verdicts would measure residue, so the batch "
                    "stops here"
                    % ((err or "no diagnostic").splitlines()[0]
                       if err else "no diagnostic"))
        excluded.append({"id": p["lr"]["id"],
                         "lane": p["lr"].get("lane") or "?",
                         "reason": reason + note})
        return None

    for p in list(picks):
        lane = p["lr"].get("lane") or "?"
        rc, _o, err = be.text(room, "cherry-pick", p["mb"] + ".." + p["tip"],
                              timeout=120)
        if rc != 0:
            rc2, out, _e = be.text(room, "diff", "--name-only",
                                   "--diff-filter=U")
            doubt = " (patch-identity screen %s — an older rebased " \
                    "land is not ruled out)" % screen if screen else ""
            if rc2 == 0 and out:
                why = ("conflict in %s at %s — evict or reorder it%s"
                       % (", ".join(out.split()), head[:12], doubt))
            else:
                # No unmerged paths: the pick stopped for a NON-conflict
                # reason (an empty pick — content already present — or an
                # interrupted one). Naming it a conflict would assert a
                # cause the state refutes; UNKNOWN stays UNKNOWN (a FIX).
                why = ("pick stopped at %s WITHOUT conflicts — an empty pick "
                       "(content already present?) or an interrupted one; "
                       "state UNKNOWN%s: %s"
                       % (head[:12], doubt,
                          (err or "no git diagnostic").splitlines()[0]))
            fatal = evict(p, why)
            if fatal:
                return scrap(fatal)
            if stop_first:
                return scrap("member %s (%s) failed and --stop-on-first was "
                             "given: %s"
                             % (p["lr"]["id"][:12], lane,
                                excluded[-1]["reason"]))
            continue
        rc, out, _e = be.text(room, "rev-parse", "HEAD")
        if rc != 0 or not out:
            fatal = evict(p, "composed HEAD unreadable after its pick")
            if fatal:
                return scrap(fatal)
            if stop_first:
                return scrap("composed HEAD unreadable after %s%s"
                             % (lane, excluded[-1]["reason"].replace(
                                 "composed HEAD unreadable after its pick",
                                 "")))
            continue
        after = out
        # THE CARRY CHECK IS PER-COMMIT AND CONTEXT-FREE (the batch-2
        # moved-target eviction): patch-id hashes context lines, so the
        # aggregate — and even a per-commit patch-id — drifts whenever an
        # earlier member moved the same region, and the eviction accused
        # an innocent lane with a claim the instrument could not support.
        # An APPROVE binds what each commit DOES; the +/- lines are that.
        rc, out, _e = be.text(room, "rev-list", head + ".." + after)
        if rc != 0 or not out:
            fatal = evict(p, "composed range unreadable after its pick")
            if fatal:
                return scrap(fatal)
            continue
        new_shas = out.split()
        if len(new_shas) != len(p["range_shas"]):
            why = ("the pick produced %d commit%s from an approved range of "
                   "%d — a squash, split, or empty-drop the reviewer never "
                   "saw" % (len(new_shas), "" if len(new_shas) == 1 else "s",
                            len(p["range_shas"])))
            fatal = evict(p, why)
            if fatal:
                return scrap(fatal)
            continue
        drift = None
        for orig_sha, new_sha, orig_id in zip(p["range_shas"], new_shas,
                                              p["approved_ids"]):
            # The RICH owner on BOTH sides: the newline veto needs the third
            # axis, and a mixed 2/3 shape must REFUSE, never authorize.
            new_id = landreq._commit_content_identity(room, new_sha)
            if not new_id:
                drift = "%s: composed content unmeasurable" % new_sha[:12]
                break
            if len(new_id) != 3 or len(orig_id) != 3:
                drift = ("%s: content identity is not the 3-axis owner shape "
                         "— refusing to compare across a mixed/tuple boundary"
                         % new_sha[:12])
                break
            # (patch OR digest) AND newline. patch-id carries every
            # discrimination git makes; the digest rescues the context shift
            # patch-id flinches at; and the newline fingerprint is an AND
            # veto because the first two are an OR — a payload-newline change
            # whose patch-id agrees would otherwise carry even as the digest
            # disagrees. BOTH content axes differing is real drift; a
            # fingerprint mismatch is drift on its own.
            if new_id[0] != orig_id[0] and new_id[1] != orig_id[1]:
                drift = ("%s -> %s: content drift — the APPROVE binds this "
                         "commit's changes (payload, paths, modes, binary "
                         "blobs) and they are not what composed"
                         % (orig_sha[:12], new_sha[:12]))
                break
            if new_id[2] != orig_id[2]:
                drift = ("%s -> %s: newline drift — the APPROVE binds whether "
                         "this commit's changed lines end in a newline, and "
                         "the composed commit differs (content axes agree, "
                         "the newline fingerprint does not)"
                         % (orig_sha[:12], new_sha[:12]))
                break
        if drift:
            fatal = evict(p, drift + "; re-review the delta rather than "
                            "composing it")
            if fatal:
                return scrap(fatal)
            if stop_first:
                return scrap("%s (%s): %s"
                             % (p["lr"]["id"][:12], lane,
                                excluded[-1]["reason"]))
            continue
        # Both existing carry lists are newest-first. Canonical replay needs
        # the actual parent-first source/result mapping, not just member tips.
        canonical_cars.extend({"row": p["lr"]["id"], "lane": lane,
                               "source_commit": source, "result_commit": result}
                              for source, result in reversed(list(zip(p["range_shas"], new_shas))))
        members.append({"id": p["lr"]["id"], "lane": lane,
                        "approved_tip": p["tip"],
                        "patch_id": p["approved"],
                        "carries": True})
        if p["bounded"] is not None:
            members[-1]["compose_land"] = {"source_base": p["mb"],
                                            "base": head, "tip": after}
        head = after
    dry = "--dry-run" in opts
    # THE TREE THE GATE MUST BIND (row 5f394d10: "exact head tree ..
    # no binding gate"): a gate receipt records a TREE, and until compose
    # names the composed head's tree there is nothing tying that receipt to
    # THIS composition — a green gate on any tree reads as the batch's.
    # Derived in the room while its HEAD still anchors the tip, dry or not.
    # THE WHOLE DIAGNOSTIC, one line: taking splitlines()[0] threw away the
    # cause lines a multi-line git error carries, so the operator got the
    # banner and lost the reason. Joined, never truncated.
    def gitdiag(err):
        parts = [l.strip() for l in (err or "").splitlines() if l.strip()]
        return " | ".join(parts) if parts else "no diagnostic"

    # THE SIDECAR NEVER GOES STALE: it exists exactly when a composition
    # STANDS. Every refusal path — kept room or removed — drops any sidecar
    # a PRIOR composition left beside this room's path, because a manifest
    # describing a batch that no longer exists reads as that batch to every
    # later tool.
    def write_sidecar(payload):
        try:
            with open(room + ".manifest.json", "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=1)
            return None
        except OSError as exc:
            return str(exc)

    def drop_sidecar():
        """None, or the fault that left a stale sidecar STANDING. Absence is
        the goal state and not an error; every other removal fault must
        surface, because removal-failed and nothing-to-remove sharing one
        silence is exactly how a stale manifest outlives its refusal."""
        try:
            os.unlink(room + ".manifest.json")
            return None
        except FileNotFoundError:
            return None
        except OSError as exc:
            return str(exc)

    # A KEPT-ANCHOR CLAIM IS MEASURED, never assumed: the sentence "it
    # anchors tip X" re-reads the room's HEAD and says what the reading
    # found — on the exact failure path where reads are failing, an
    # unmeasured claim of anchorage is the likeliest lie.
    def anchor_claim():
        rc_a, out_a, _e = be.text(room, "rev-parse", "HEAD")
        if rc_a == 0 and out_a == head:
            return "it anchors tip %s" % head[:12]
        return ("its anchor could not be re-verified — expected tip %s"
                % head[:12])

    tree = None
    if members:
        rc, out, err = be.text(room, "rev-parse", "HEAD^{tree}")
        tree = out if rc == 0 and out else None
        if not tree:
            msg = ("the composed HEAD's tree is unreadable (%s) — no gate "
                   "can bind this composition" % gitdiag(err))
            stale = drop_sidecar()
            if stale:
                msg += (" — a stale sidecar manifest SURVIVES beside the "
                        "room (%s); remove %s.manifest.json by hand"
                        % (stale, room))
            if dry:
                rc, _o, err = be.text(root, "worktree", "remove", "--force",
                                      room, env=sanction)
                kept = rc != 0
                if kept:
                    msg += (" — DRY-RUN could not remove its room (%s); %s "
                            "is still registered, remove it by hand"
                            % (gitdiag(err), room))
                else:
                    msg += " — the dry-run room was removed; nothing stands"
            else:
                kept = True
                msg += (" — the room is KEPT at %s (%s); resolve the read, "
                        "then remove and recompose before gating"
                        % (room, anchor_claim()))
            if "--json" in opts:
                print(json.dumps({"composed_tip": head if kept else None,
                                  "composed_tree": None, "refused": msg,
                                  "trunk": {"ref": ref, "sha": trunk_sha},
                                  "room": room if kept else None,
                                  "members": members, "excluded": excluded,
                                  "dry_run": dry},
                                 ensure_ascii=False, indent=1))
            else:
                # the text account matches the JSON one: a scripted reader
                # gets members/excluded either way, never only in one mode.
                print("helm lr compose: " + msg, file=sys.stderr)
                for m in members:
                    print("  %s  %s  tip %s  patch-id %s  CARRIES"
                          % (m["id"][:12], m["lane"],
                             m["approved_tip"][:12], m["patch_id"][:12]),
                          file=sys.stderr)
                for x in excluded:
                    print("  EXCLUDED %s (%s): %s"
                          % (x["id"][:12], x["lane"], x["reason"]),
                          file=sys.stderr)
            return 1
    manifest = {"composed_tip": head, "composed_tree": tree,
                "trunk": {"ref": ref, "sha": trunk_sha},
                "room": None if dry else room, "members": members,
                "excluded": excluded, "dry_run": dry}
    bounded_ids = [member["id"] for member in members if "compose_land" in member]
    canonical = {"result_tip": head, "base": trunk_sha, "room": room,
                 "cars": canonical_cars, "composed_ts": int(time.time())}
    if bounded_ids:
        manifest.update(canonical, compose_land_version=1,
                        bounded_rows=bounded_ids, room=None if dry else room,
                        fold_authority="pending whole composed-tree suite gate")
        canonical = dict(manifest)
    def refuse_provenance(why):
        stale = drop_sidecar()
        if stale:
            why += " — stale sidecar removal failed: " + stale
        return scrap(why)

    if members and project_state == "registered":
        replay = foldcompose.replay_chain(room, canonical_cars, trunk_sha, head)
        if not replay or any(ok is not True for _name, ok, _why in replay):
            return refuse_provenance("canonical car replay refused; composition is not evidence")
        if bounded_ids:
            derived, err = foldcompose.bounded_members(
                room, dict(canonical, dry_run=False), raw_rows)
            if err or set(derived or {}) != set(bounded_ids):
                return refuse_provenance(err or "bounded car census changed")
    if not members:
        rc, _o, _e = be.text(root, "worktree", "remove", "--force", room,
                             env=sanction)
        return refuse_batch("NO member composed — the batch stands empty; "
                            "every exclusion listed")
    if dry:
        rc, _o, err = be.text(root, "worktree", "remove", "--force", room,
                              env=sanction)
        if rc != 0:
            # a dry run that left state broke its own contract — say so and
            # fail, never return 0 over a registered room (a FIX), and
            # in BOTH output modes: a bare stderr line under --json left a
            # scripted caller with empty stdout and a parse error.
            return refuse_batch("DRY-RUN could not remove its room (%s) — "
                                "%s is still registered; remove it by hand"
                                % (gitdiag(err), room),
                                members=members, room_kept=room)
        stale = drop_sidecar()
        if stale:
            return refuse_batch("the dry-run room was removed but a stale "
                                "sidecar manifest SURVIVES (%s); remove "
                                "%s.manifest.json by hand" % (stale, room),
                                members=members, room_kept=None)
    if not dry and project_state == "registered":
        # The canonical producer owns all actual source/result addresses.
        # Bounded output is a presentation of THIS record; no second sidecar
        # is persisted. A gate is intentionally not required before composing.
        if not foldcompose.write_manifest(compose_project, canonical):
            return refuse_provenance("canonical project-home manifest could not be written")
    if not dry and bounded_ids:
        stale = drop_sidecar()
        if stale:
            return refuse_batch("canonical manifest stands but stale sidecar removal failed: " + stale,
                                members=members, room_kept=room)
    if not dry and not bounded_ids:
        # The manifest lives BESIDE the room, never inside it (task/228,
        # measured on the first live batch): an untracked compose-manifest
        # in the room makes the room dirty at gate time, and fab snapshots
        # untracked files — the receipt would bind a tree NO COMMIT HAS,
        # and the fold then refuses the batch for what reads like
        # corruption, after the slot was spent.
        exc = write_sidecar(manifest)
        if exc:
            return refuse_batch("composition stands at %s but its manifest "
                                "could not be written (%s) — the room is "
                                "KEPT (%s); rerun or write the manifest by "
                                "hand" % (room, exc, anchor_claim()),
                                members=members, room_kept=room)
    if "--json" in opts:
        print(json.dumps(manifest, ensure_ascii=False, indent=1))
        return 0
    stood = "DRY — would stand" if dry else "standing"
    print("helm lr compose: %d member%s %s at %s (onto %s @ %s)%s"
          % (len(members), "" if len(members) == 1 else "s", stood, head[:12],
             ref, trunk_sha[:12],
             (", %d excluded" % len(excluded)) if excluded else ""))
    for m in members:
        print("  %s  %s  tip %s  patch-id %s  CARRIES"
              % (m["id"][:12], m["lane"], m["approved_tip"][:12],
                 m["patch_id"][:12]))
    for x in excluded:
        print("  EXCLUDED %s (%s): %s" % (x["id"][:12], x["lane"],
                                          x["reason"]))
    if bounded_ids:
        print("  fold authority PENDING — whole composed-tree suite gate required")
        if not dry:
            print("  canonical manifest %s" % foldcompose.manifest_path(compose_project, head))
            print("  verify with helm lr foldcheck %s --repo %s --gate gate:<RECEIPT_ID>"
                  % (head, room))
    if not dry:
        print("  room %s — its HEAD anchors the composed tip; keep it until "
              "the land" % room)
        print("  the gate must bind tree %s — a receipt naming any other "
              "tree is not this composition's evidence"
              % ((tree or "UNREADABLE")[:12]))
        print("  next: gate the composed tip; on green, merge it and run "
              "`helm lr land <id>` per member (the receipt RECORDS the "
              "carried patch-id — diagnostic and fail-open; Git stays the "
              "lifecycle authority); on red, localize by prefix: compose "
              "the first K members, longest green prefix lands, first red "
              "member evicts to its own re-gate")
        # SLICE 3 (task/165): the post-land leg is per-member handwork no
        # longer — the manifest already names who composed, so the verb
        # prints the exact commands. The merge and the closes still belong
        # to the integrator; this is the list, not the act.
        print("  on green, per member, in order:")
        for m in members:
            print("    helm lr land %s   # %s (patch-id %s)"
                  % (m["id"][:12], m["lane"], m["patch_id"][:12]))
        for x in excluded:
            if x.get("unresolvable"):
                print("    # excluded: %s (unresolvable row token) — %s"
                      % (x["id"][:12], x["reason"][:80]))
            else:
                print("    # excluded: helm lr show %s   # %s — %s"
                      % (x["id"][:12], x["lane"], x["reason"][:80]))
    return 0

def _cmd_land(rest):
    """helm lr land <id> — emit a signed helm.land candidate at integration and
    write through its local diagnostic receipt. FAIL-OPEN: a missing signer/node
    never fails the land; Git remains the lifecycle authority.

    THE VERB SPLIT (D5): `lr land` is the AT-INTEGRATION receipt verb — run at
    the moment the merge exists locally, pre-push, where its fail-open witness
    and fail-CLOSED deletion guard both mean something. `lr close --reason
    landed` is the separate POST-OBSERVATION terminal, recorded once the land
    is provable on the named trunk. Neither aliases, deprecates, or absorbs
    the other: aliasing land onto close would flip exit 0->1 in this verb's
    primary window (a local merge not yet pushed reads absent under the
    upstream trio), and the deletion guard is hollow post-merge."""
    from .cli import guard_tail
    if not rest:
        print(landreq.USAGE, file=sys.stderr)
        return 2
    rid = rest[0]
    # --ack-deletions MUST be in this tuple: the deletion refusal at the
    # bottom of this function instructs the operator to pass it, and for as
    # long as it was missing here guard_tail returned rc 2 on the very flag
    # the refusal demanded — every deletion-bearing merge was unwitnessable
    # from the CLI, refusal (1) without the flag and usage-error (2) with it.
    rc = guard_tail("helm lr land", rest[1:],
                    flags=("--json", "--ack-deletions"), usage=landreq.USAGE)
    if rc is not None:
        return rc
    lr, err = landreq.get(rid)
    if err:
        print("helm lr: " + err, file=sys.stderr)
        return 1
    gitdir = lr.get("repo_id")
    reviewed_tip = lr.get("reviewed_tip") or lr.get("review_sha")
    as_json = "--json" in rest[1:]
    if not gitdir or not reviewed_tip:
        msg = "%s has no repo binding / reviewed tip to record a receipt for" \
            % lr["id"]
        if as_json:
            print(json.dumps({"recorded": False, "receipt": None,
                              "reason": msg}, ensure_ascii=False, indent=1))
            return 0
        print("helm lr land: " + msg, file=sys.stderr)
        return 1
    ack_dels = "--ack-deletions" in rest[1:]
    trunk_ref = landreq._resolve_ref(gitdir, landreq.LOCAL_TRUNK)
    # Deletion guard: a merge that deletes TRACKED files from a remote is
    # irreversible. The quality gate asks "is this merge SAFE"; this asks
    # "am I AUTHORIZED to delete." UNKNOWN fails SAFE: if the deletion set
    # cannot be computed, REFUSE — an unscannable deletion is not a known-
    # safe one. The flag is tedious on purpose: it should be deliberate,
    # not the default next flag after --json.
    del_hits, del_err = landreq._tracked_deletions(gitdir, trunk_ref, reviewed_tip)
    if del_err:
        print("helm lr land: " + del_err, file=sys.stderr)
        return 1
    if del_hits and not ack_dels:
        print(landreq._deletion_refusal(del_hits), file=sys.stderr)
        return 1
    up_ref = landreq._resolve_ref(gitdir, landreq.UPSTREAM_TRUNK)
    # The actual trunk for the historical event identity, sampled at integration.
    # Upstream fields remain diagnostic snapshots outside the payload and replay
    # never consults them for lifecycle state.
    trunk_sha = landreq._trunk_sha(gitdir, trunk_ref)
    rec, why = landreq.record_land(
        lr["lane"], lr["branch"], reviewed_tip,
        landreq._patch_id(gitdir, reviewed_tip, trunk_ref), trunk_sha, repo_id=gitdir,
        has_upstream=bool(up_ref),
        upstream=landreq._ancestry(gitdir, trunk_sha, up_ref) == landreq.ANCESTOR)
    if as_json:
        print(json.dumps({"recorded": bool(rec), "receipt": rec, "reason": why},
                         ensure_ascii=False, indent=1))
        return 0
    # BOTH LINES NAME THE NON-ACTION, because this verb's NAME is an
    # imperative and its job is not. `helm lr land` emits a RECEIPT; the merge
    # is the integrator's own git work, and nothing here moves a ref.
    #
    # The old wording made that invisible in both directions. Success read
    # "recorded ... land candidate", failure read "land not blocked" — and an
    # integrator scanning output sees the word `land` next to an exit 0 and
    # reasonably concludes the lane landed. Measured 2026-07-30: I ran this
    # against an approved row, got exit 0 and one benign line, and only caught
    # it because I checked origin/main afterwards and found it unmoved,
    # ancestry NO, row still landed=False. The ledger DOES refuse to agree
    # (it derives landedness from live Git, so the row stays READY) — but
    # nothing TELLS the caller, so the gap between "this printed fine" and
    # "nothing happened" is exactly one habit wide.
    if rec:
        print("helm lr: receipt recorded for %s (chain %s, turn %s) — NO MERGE "
              "PERFORMED; run the merge yourself, this verb only witnesses it"
              % (lr["id"][:12], rec["chain"], (rec["turn"] or "")[:12]))
        return 0
    # "fail-open" is kept VERBATIM: it is a real documented property (a missing
    # signer must never block a land) and dropping it to make room for the
    # non-action would trade one true thing for another.
    print("helm lr: NO MERGE PERFORMED and no receipt written (%s) — fail-open, "
          "the receipt is optional and its absence blocks nothing. This verb "
          "never merges; it witnesses a land you perform in git, and the row "
          "stays unlanded until that merge reaches trunk." % why)
    return 0


# ---------------------------------------------------------------------------
# PUBLISH BACK ONTO THE LEDGER
# ---------------------------------------------------------------------------
def _publish():
    """Bind every name this module owns back onto `landreq`.

    ONE SOURCE OF TRUTH. `landreq._OWNER_NAMES` is both the tuple the
    retired-name rung reads and the tuple this loop walks, so the declaration
    and the runtime binding cannot drift -- there is no second list to forget.
    """
    for module, names in landreq._OWNER_NAMES:
        if module != __name__.rsplit(".", 1)[-1]:
            continue
        for _name in names:
            setattr(landreq, _name, globals()[_name])


_publish()
