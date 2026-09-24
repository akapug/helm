#!/usr/bin/env python3
"""The citation registry, and the pre-commit rung that reads it.

WHY THIS EXISTS AS A GUARD AND NOT ONLY AS A TEST. `tests/test_docstring_refs`
already refuses a commit that cites a sha nobody can reach — but it only runs
inside the whole suite, so the feedback loop for a one-character citation slip
is a GATE: ~8 minutes, and at the time of measurement the wait in front of it
was a cap of 2 that every node shared (that cap is PER-HOST as of 2026-08-05;
the argument here rests on the 8 minutes, which did not change). MEASURED over the
receipt ledger 2026-08-01..2026-08-04: 36 gate runs killed by that one check,
229 MINUTES of whole-suite wall-clock. Three seats hit it in one night and TWO
OF THEM hit it AFTER reporting it to the room. That is not careless seats; it
is a correct guard in the wrong POSITION. Same law as never-track: refuse it
BEFORE it becomes history.

THIS RUNG IS DELIBERATELY STRICTER THAN THE SUITE'S, and that is the whole
point rather than an accident. The suite's `_resolves` admits any object a
LOCAL ref points at (`for-each-ref --points-at`), which is honest for "can an
in-repo reader reach it" and is why a citation can pass on the author's box and
die on the fab: a local lane branch is invisible to every other clone. This
rung asks the FAB'S question instead — would a FRESH CLONE reach it — so it
predicts the gate rather than repeating it. It never relaxes the suite's rule
and never edits `_resolves`, so no existing citation newly fails.

IT VALIDATES AUTHORITY BY CATEGORY, not by the shared accident of hexadecimal
shape. A commit sha is a Git object, LEDGER_CITED names one live row or receipt,
PATCH_IDS names content identity recomputed from an exact diff, and SKIP names a
reasoned non-object runtime token. The registry lookup remains one ACCOUNTED
set, but admission never flattens these categories: asking a ledger to vouch for
a PID or cat-file to resolve a patch-id is a category error. Anything no
category or REMOTE ref accounts for is REFUSED BY NAME. The guard never claims
a token is DEAD -- only that this repository cannot account for it, which is
the strongest repo-local claim available at pre-commit speed.

STAGED DIFF ONLY. A whole-tree scan would refuse your commit for a citation
somebody else introduced, which is how a guard earns a blanket SKIP=1 reflex
and stops guarding anything.
"""
import ast
import os
import re
import subprocess
import sys

HEX = re.compile(r"\b[0-9a-f]{7,40}\b")
FULL_HEX = re.compile(r"[0-9a-f]{40}\Z")
_HUNK = re.compile(r"@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@(?: .*)?\Z")


class _UnsupportedDiff(ValueError):
    """The staged diff was readable but outside this scanner's grammar."""


def _sibling(name):
    """An installed snapshot neighbour, or None on a broken installation — the
    dual-mode loader the other rungs use to reach each other's seams."""
    if __package__:
        try:
            return __import__("helm." + name, fromlist=[name])
        except Exception:                                      # noqa: BLE001
            return None
    here = os.path.dirname(os.path.abspath(__file__))
    if not os.path.exists(os.path.join(here, name + ".py")):
        return None
    try:
        mod = __import__(name)
    except Exception:                                          # noqa: BLE001
        return None
    got = os.path.dirname(os.path.abspath(getattr(mod, "__file__", "") or ""))
    return mod if got == here else None


def _parents(repo):
    """This commit's parent committishes, from nevertrack.commit_parents — the
    ONE implementation of that question in the guard family, carrying the
    measurement of what a first-parent base does to a merge. An
    absent neighbour (a broken installation; nevertrack.py is snapshotted
    beside this rung under every profile) falls back to HEAD alone, which is
    this rung's OLD base: it over-blocks a merge, and this rung's own callers
    already turn an unreadable staged diff into a WARNING rather than a
    refusal, so failing closed here would be stricter than the rung is
    anywhere else."""
    nt = _sibling("nevertrack")
    if nt is None:
        return ["HEAD"]
    return nt.commit_parents(repo) or ["HEAD"]


def _added_path(line):
    """The decoded b-side path from one `+++` header, or None if malformed."""
    raw = line[4:]
    if raw == "/dev/null":
        return "?"
    if raw.startswith('"'):
        try:
            raw = os.fsdecode(ast.literal_eval("b" + raw))
        except (SyntaxError, ValueError):
            return None
    return raw[2:] if raw.startswith("b/") else None


LEDGER_CITED = {
    "e43ec4309e89": "land-request row id (codex-3 FIX verdict 2026-08-12 on "
                "lane store-keys-stay-discriminating; CLOSED_WITHDRAWN "
                "2026-09-13, its reviewed tip never reached origin/main) — "
                "cited in helm/store/write.py `_dup_folds`, `_attest_verdict` "
                "and `doctor` as the review whose three surviving findings "
                "(partial commit, unverified attestations rewritten, planted "
                "contributors) those cures close. task/2544.",
    "a973a6f73ea9e292": "gate-receipt id (another project's lane "
                "a-node-that-never-reaps-fills-in-an-afternoon, declared "
                "`bash fab/test/run.sh` under protocol exit, rc 1 on a fab node, "
                "2026-09-15) — cited in helm/gate.py `_mint_result` as the "
                "first measured red declared receipt that could not be "
                "diagnosed: its stdout went nowhere and no tail was kept. "
                "task/2527.",
    "304cfecb480082c4": "gate-receipt id (the second measurement of the same "
                "defect on the same lane, rc 1) — cited beside "
                "a973a6f73ea9e292 in helm/gate.py `_mint_result`. task/2527.",
    "a4133aed1dc6": "land-request row id (REVIEWED, CONCUR held, lane "
                "foreign-repo-write-door, recorded tip 9d02cd307178) — cited "
                "in helm/landreq.py on `_lane_family_refs` as one of the two "
                "rows that proved the ref scan was walking the wrong table. "
                "Its recorded tip does not resolve (`git cat-file -e` says "
                "absent), so the stranded ladder reached its lane-family rung "
                "and, walking only refs/heads + refs/remotes, answered WOULD "
                "CLOSE --reason stranded — a claim that the substrate was "
                "DESTROYED. The lane was sitting intact the whole time at "
                "refs/helm-retired/lane/foreign-repo-write-door @ "
                "32f2a88451de, where helm's own worktree GC had COPIED it "
                "before deleting the branch. The row is the evidence that the "
                "one namespace helm invented to PRESERVE a lane was the one "
                "namespace the destruction probe could not see, and that the "
                "fix had to be an unrestricted for-each-ref rather than a "
                "third namespace in a list that goes stale again.\n",
    "e85ee1566272": "land-request row id (REVIEWED, CONCUR held, lane "
                "helm-runtime-identity-rebinds, recorded tip 6c98dca35af5) — "
                "cited in helm/landreq.py on `_lane_family_refs` as the "
                "SECOND row of that pair, and it is cited because one row is "
                "an anecdote. Same shape and same false terminal: tip absent, "
                "lane alive at refs/helm-retired/lane/"
                "helm-runtime-identity-rebinds @ 109c11429d0c. Measured "
                "2026-09-13 through the real verb, both now REFUSE naming the "
                "surviving ref, so the pair is also the pass/fail control for "
                "that fix — a regression re-admits exactly these two.\n",
    "7ac39e0f704e": "dispatch row id (review, codex-3, polarity fix, basis "
                "measured, reviewed tip 0e15b7a2273e) — the SCOPE verdict on "
                "the melded whole-object cure, cited in helm/handoff.py "
                "beside the datetime parse. It answered the exit question NO "
                "and named why: the hand-rolled grammar ACCEPTED zone offsets "
                "and fractional seconds and then discarded them, so a record "
                "written in +05:00 read five hours wrong in the direction "
                "that withdraws a delivery. The row is the evidence that "
                "`fromisoformat` replaced a working-looking regex because "
                "accepted and understood had come apart, not for taste.\n",
    "d4ac3c08ad12": "dispatch row id (review, codex-3, polarity fix, basis "
                "measured, reviewed tip cb3a12c06e2d) — the THIRD round on "
                "the same reader and the one that ended the spiral, cited in "
                "helm/handoff.py beside `_spoken_at`. Its two probes: a "
                "timestamp value carrying its own quotes escaped the faux "
                "JSON the cure built around it, and a date-shaped impossible "
                "value raised out of a helper contracted to answer None. The "
                "row is the evidence that the value is parsed DIRECTLY "
                "because constructing parser input from untrusted data was "
                "measured to be exploitable, not because it reads better.\n",
    "348e1dc4aa81": "dispatch row id (review, codex-3, polarity fix, basis "
                "measured, reviewed tip a173538d9e9e, worse-than-main on "
                "helm/handoff.py) — the SECOND round on the same reader, "
                "cited in helm/handoff.py beside `_spoken_at`. Round one "
                "moved the timestamp read to the parsed top level and left "
                "the record SELECTION a regex over the raw line, so a "
                "top-level `user` row wearing a nested assistant marker was "
                "still read as proof the seat had spoken. The row is the "
                "evidence that the one-parse form exists because a half-cure "
                "was measured to still be wrong, not because the author "
                "preferred it.\n",
    "0e4addc128eb": "dispatch row id (review, codex-3, polarity fix, basis "
                "measured, reviewed tip 2042fb02a849, worse-than-main on "
                "helm/handoff.py) — cited in helm/handoff.py as the "
                "provenance of `_record_stamp`. The finding is that a nested "
                "payload `timestamp` can be read as the record's own, so an "
                "old assistant record answers `spoke_since` True and the "
                "resume leg withdraws a delivery the seat never got. The row "
                "is the evidence that the narrow reader exists for a measured "
                "defect rather than an imagined one, and it names the "
                "reviewer, so a later reader can weigh the cure against the "
                "finding instead of taking the comment's word for it.\n",
    "5d2185eb69f5f335": "gate receipt id (whole-suite FAILED, 54 failures + 1 "
                "error, tree 35533f008f93, 2026-09-09) — cited in "
                "helm/gate.py as the measured instance of the cap refusal "
                "asserting a data loss that had not happened. Its base check "
                "said 35 identities 'were never identified and were SKIPPED' "
                "while its own import line names the chunk holding all of "
                "them. The receipt is the evidence for the sentence being "
                "reworded, so a reader can check the claim rather than take "
                "the comment's word for it.",
    "48eb1ddffc4248ef": "dispatch row id (foldcheck-asks-whether-the-cars-"
                "were-reviewed round two, REVIEW FIX/MEASURED at reviewed "
                "tip 4e029b7b41fa, 2026-09-09) — cited in helm/foldcheck.py "
                "as the PLACEMENT RULING that deleted the REFUSE arm: no "
                "sound fold-time refusal can come from a patch-id miss after "
                "a rewrite, and closure needs composer-produced provenance "
                "verified by replay and tree equality. The comment states a "
                "design boundary somebody will want to reopen; the row is "
                "how they check who drew it and on what evidence.",
    "5bf9eef37584496b": "dispatch row id (foldcheck-asks-whether-the-cars-"
                "were-reviewed, REVIEW FIX/MEASURED at reviewed tip "
                "321e7fde6ffe, 2026-09-10) — codex-3's SECOND probe on the "
                "same rung, cited in helm/foldcheck.py and "
                "tests/test_foldcheck.py. Round one (251e6648583581b4, above) "
                "moved `_NO_REVIEW_YET` above the ref handling; the fix then "
                "left it INSIDE THE PAIRLESS ARM, so an OPEN row carrying a "
                "VERIFIED RETIP derived a real (base, tip) from `_work_pair`, "
                "never reached the guard, and had its unreviewed patch "
                "credited. Their Fab probe returned a false PASS — 'all 1 "
                "folded commit inside a dispatched review range' — while "
                "every focused arm stayed green, because each built a "
                "PAIRLESS row. `retip` is OPEN-only, so the state is "
                "production-reachable. The row is how a later reader checks "
                "why the guard now dominates `_work_pair` instead of sitting "
                "in one of its arms, and that the placement was measured "
                "twice rather than argued.",
    "251e6648583581b4": "dispatch row id (foldcheck-asks-whether-the-cars-"
                "were-reviewed, REVIEW FIX/MEASURED at reviewed tip "
                "3d623fc42a46, 2026-09-09) — cited in helm/foldcheck.py as "
                "the origin of the _NO_REVIEW_YET ordering fix. codex-3's "
                "exact probe: a resolvable CANCELLED or still-OPEN pairless "
                "row entered `ranges` BEFORE the status guard skipped it, so "
                "a pending review credited work nobody had reviewed and an "
                "unreviewed folded commit could PASS. The row is the "
                "evidence for a comment that would otherwise read as the "
                "author's own reasoning, which is the attribution this "
                "registry exists to keep checkable.",
    "48bf36a0075715b6": "gate receipt id (whole-suite FAILED on the four-lane "
                "compose af20ed700ec6, 2026-08-31) — cited in "
                "helm/inject/_entries.py as the measured cause of REVERTING a "
                "per-process capability memo. The memo was right for "
                "production, where every caller is a per-invocation hook, and "
                "wrong for the SUITE, which is one process running 14,419 "
                "tests: it leaked a wired estate from one test into the next. "
                "Three test_capability arms and the memo own must-miss arm "
                "went red. The receipt is the evidence the docstring rests "
                "on, so a reader can check the claim instead of trusting it.",
    "e8336b68699c": "dispatch row id (gc-rescue-defers-to-a-live-author, "
                "REVIEW FIX at reviewed tip aed1bd91b192, "
                "gate:ae37fd2136486b41) — cited in helm/work/_gc.py as the "
                "provenance of three cures at once: the activity guard moved "
                "from gc_enact to `_wip_commit` because it was above ONE of "
                "three callers; `_rescue_deferral`'s two-state answer became "
                "the `_room_activity` TRI-STATE because UNKNOWN was reading as "
                "abandoned; and `clock_skew` acquired its first reader, having "
                "been computed and ignored. A ROW id, never a commit.",
    "9f09ff4b858e": "dispatch row id (lr-close-chain-proof-for-pruned-tips, "
                "FIX at reviewed tip f69687e292f3 with gate "
                "856d56aa5762c002) — the review that measured the three holes "
                "in the chain-proof terminal: chain_path derived pre-lock and "
                "never re-walked by writer OR replay, _chain_authority "
                "reading ANY nonempty gate as authorization, and verdict "
                "indexes discarded so a frontier APPROVE could predate the "
                "FIX debt it claims to have carried. Cited by "
                "dispatches._close_event_error's chain-proof arm and by "
                "landreq._chain_authority as the origin of each rung, so a "
                "reader years later can find the construction rather than "
                "take the comment's word for it. A ROW id, never a commit.",
    "4171ff6cae91": "dispatch row id (absence-invariant-covers-every-path, "
                "REVIEW FIX at reviewed tip fc0eeacaa554, "
                "gate:44e46958ba0dc768) — the row whose finding is cited in "
                "helm/work/_claims.py's except-branch comment: a fail-open "
                "cure stranded EVERY matched row, including terminal history, "
                "so one closed row plus a transient object-store failure "
                "blocked a claim label reuse main permits. Cited as the "
                "provenance of the liveness filter, and as the case that "
                "shows the in-loop UNKNOWN-ON-TERMINAL rule was never applied "
                "to the branch three screens down in the same function. "
                "A ROW id, never a commit.",
    "9641bb987009": "dispatch row id (chatnode-posture-r1, FIX at "
                "reviewed tip 8b716e23605e) — THE SPECIMEN for the owed "
                "phantom: a FIX verdict, a DISCHARGE twelve hours "
                "later, and it rendered as unanswered "
                "debt every day since because `status` names how a row FIRST "
                "closed and the projection's transitions are guarded on "
                "status == 'open'. Cited by obligation.unanswered_fixes as "
                "the row that proves the retirement is already projected "
                "(discharged/discharge_ts/discharge_ref) and merely unread. "
                "A ROW id, never a commit.",
    "0a36e61fd6cd": "land-request row id (helm-runtime-identity-rebinds, "
                "FIX at reviewed tip 6c98dca35af5) — one of the three live "
                "specimens that proved the close ladder had NO DOOR for "
                "reviewed work on trunk by PATCH IDENTITY. Its cure round "
                "was dispatched --new-work, so `superseded` had no chain "
                "link; its reviewed tip is PRESENT on trunk, so `subsumed` "
                "(which the resolved refusal used to name) required the "
                "complement of the measured state. Cited by "
                "rowworld._reached_trunk as the class that witness exists "
                "for. A ROW id, never a commit.",
    "18e895caf600": "land-request row id (burn-down-renderer-arms, FIX at "
                "reviewed tip d9d715acf1a7) — second of the three specimens "
                "for rowworld._reached_trunk. Its work pair EXISTED and both "
                "content witnesses still went silent, because trunk had "
                "edited the same files after the rebase-land: the proof that "
                "the gap was the QUESTION being asked, not a missing base. "
                "A ROW id, never a commit.",
    "158ce57d13fb": "land-request row id (shadowed-test-class-never-ran, FIX "
                "at reviewed tip 23c0a2d896a9) — third specimen, and the one "
                "that shows the second half of the gap: a --new-work review "
                "row is its own chain root, so `_work_pair` correctly "
                "returns (None, None) and the replay witnesses could never "
                "be ASKED about it. rowworld._reached_trunk needs no base. "
                "A ROW id, never a commit.",
    "3e4f26966ba8": "dispatch row id (lane/hardcode-rung-staged-shape) "
                "— the row that proves `helm owed` and `helm dispatch triage` "
                "are NOT in conflict: owed correctly OMITS it because its "
                "author already cured, and triage correctly NAMES it because "
                "the cure was never re-dispatched. Two questions, two right "
                "answers. Cited by web_owed._cured_rows as the specimen that "
                "retired the contradiction premise on task/955 and made the "
                "second bucket a rendering gap rather than a ruling. "
                "A ROW id, never a commit.",
    "ff7024b5": "dispatch row id (xrev of the task-ledger project "
                "axis, lane/task-ledger-project-axis at 14aa03e8fca4) — the "
                "FIX that measured the scoped list's footer calling "
                "fleet-wide counts() under a scoped header: \"2 shown — "
                "open 5\", totalizing the exact rows the scope had just "
                "withheld. Cited by cmd_task's footer comment and the arm "
                "that pins the footer row whole. A ROW id, never a commit.",
    "db87bcd4": "land-request row id (spark-spawn-wires-model) — the red-first fixture for task/777's content-equivalent rung: READY+STALLED, its work carried on trunk by 98582d17 with byte-identical added and removed lines, and Git calling it ABSENT because 3 of 98 context lines moved. Cited by landreq._content_equivalent_carrier as the measurement that motivates the rung. A ROW id, never a commit.",
    "8b0c8110": "land-request row id (a2h-authority-tier-bypass-design, "
                "the FIX — the LIVE SPECIMEN for the degenerate-pair "
                "vacuity found by task/756: its chain carries no build row, "
                "so the work pair fell back to base == tip, the replay was "
                "EMPTY, and an empty replay reproduces whatever HEAD it is "
                "handed. Measured dry-running `close --reason carried` "
                "against THIS repository's trunk, where its work — authored "
                "in another project's repository — has never been. Cited by "
                "rowworld._work_pair. A ROW id, never a commit.",
    "5f394d10": "land-request row id (compose-carry-content-review, r5) — "
                "the FIX verdict naming the three false-carry classes "
                "the content digest was path-blind to (mode-only chmod "
                "across a rename, quoted deletion collisions, divergent "
                "binary blobs) and the missing composed-tree gate binding. "
                "Cited by _diff_header_path/_commit_content_id and by the "
                "composed_tree derivation in _cmd_compose. A ROW id, never "
                "a commit.",
    "441c4491": "land-request row id (supersedes-leaves-parent-open) — --supersedes left the parent looking actionable; cited by the annotation rung in dispatches._append_dispatch and by superseded_parent_sweep",
    "a6d5d95f": "dispatch/chain id (beacons-attendance review, chain_root "
                "a6d5d95fb7f02e4a…) — the REWORK verdict whose two residues "
                "task #199 closed; cited by escalate's revalidation law in "
                "helm/beacons.py (\"concurrent passes deliver same edge "
                "twice\") and by the delivery-edge tests in "
                "tests/test_beacons.py",
    "463ae38a": "review dispatch row id (orphaned-build-row-rung) — the review that attacked the STATED REASON rather than the behaviour and showed the specificity guard is the dash-joined form, not the length floor. Cited by landreq.offchain_landing. A ROW id, never a commit.",
    "652d9794": "land-request row id (parked-dispatch-rebind-orphaned-tip) — the live specimen for task/430: a BUILD row whose work reached trunk under a different lane label, so every landedness check keyed on its own ref answered UNKNOWN and the stall surface billed a named seat for finished work. Its LABEL carries a parked- prefix the branch never had, which is why the verbatim-lane criterion missed it. Cited by landreq.offchain_landing. A ROW id, never a commit.",
    "e2acaae0": "dispatch row id (task-add-leading-flag) — the probes that a valueless repeated flag vanished at rc 0 and that an empty --owner filter printed the whole ledger. Cited by the valueless list in tasks.add and the asked_owner check in list. A ROW id, never a commit.",
    "e37b25f9": "dispatch row id (task-add-leading-flag) — the exact probe that a DUPLICATE valued flag after a title token was joined into the title at rc 0. Cited by the full-argv stray scan in tasks.add and its arms. A ROW id, never a commit.",
    "a75aecaea36a": "dispatch row id (store-evidence-resolves-what-get-"
                    "resolves review) — the FIX verdict whose "
                    "exact-tip repro showed _resolve_write filtering to the "
                    "verb's types BEFORE the _TYPE_ORDER winner choice, so a "
                    "heuristic + always-reference sharing one slug meant "
                    "`get` showed the heuristic while `demote` silently "
                    "wrote the REFERENCE, err=None. Cited by "
                    "store.write._resolve_write's winner-first law and its "
                    "shared-slug arm in tests/test_store.py. A ROW id, never "
                    "a commit.",
    "ee48cea6": "dispatch row id (lr-hard-ttl-clears-the-rebuild, FIX/"
                "MEASURED) — the exact-tip probe that measured the "
                "inherited `_LR_HARD_TTL_S * 30` backstop widening 1h->5h "
                "when the serve-stale display cap moved 120->600: age=7200/"
                "ledger_age=8200 rendered WARM and bypassed cold replay under "
                "a contract promising one genuinely quiet hour. Cited by "
                "_WARM_QUIET_HOUR_S in helm/landreq.py and by its arm in "
                "tests/test_landreq.py. A ROW id, never a commit.",
    "069406da7cf6": "land-request row id (cell-bin-doc-vs-code) — the rebind whose room stayed fenced under the walled OLD recipient; cited by dispatches.rebind_room_fence as the incident that rung exists to end",
    "7d2b5c1ae0f3": "dispatch row id (gate-orphans-need-a-sweep) — the finding that a FAILED receipt was classified as binding, that a substring stood in for a token parse, and that the wording said ANNOUNCED for a value stamped before the post. Cited by gate._minted_token and _announced_advice. A ROW id, never a commit.",
    "02c7cddc6887": "dispatch row id (gate-orphans-need-a-sweep) — two blocking defects on the sweep: the OPERATIONAL publisher discarded the stamp the list surface rendered, and the advice told readers not to re-run REFUSED/UNMINTED/UNKNOWN attempts that may need it. Cited by gate._orphan_line and _announced_advice. A ROW id, never a commit.",
    "45b6d0c46161": "land-request row id (gate-waiter-orphans-with-no-receipt) "
                    "— the FIX round that separated helm's two nouns: a "
                    "RECEIPT is what the gate mints and `helm gate list` "
                    "prints, a VERDICT is a reviewer's polarity in the "
                    "dispatch ledger. Cited by gate._ORPHAN_CONSEQUENCE and "
                    "by the orphan-notice arms in tests/test_gate_fifo.py, "
                    "both of which exist to keep that separation. It is a ROW "
                    "id and never a commit: the row outlives every rebase of "
                    "the lane it reviews, which is exactly why the comments "
                    "cite it instead of a sha.",
    "c7897ed62f4a": "dispatch row id (rule-arrives-with-its-gate, task/1346) "
                    "— the fourth FIX: the cooldown read preferred the "
                    "typed record by ORDER, so a rider's stale score stayed "
                    "authoritative over a later selected escape — repeated "
                    "escapes, wrong explain age. Cited by "
                    "inject/_whisper._cool_rec. A ROW id, never a commit.",
    "0fb12823851d": "dispatch row id (rule-arrives-with-its-gate, task/1346) "
                    "— the third FIX/MEASURED: rendered rider ledger and "
                    "cooldown ids bare (same-slug types collapsed) and a typed "
                    "gates --remove matching a stored bare same-slug gate. "
                    "Cited by inject/_whisper._cool_key, inject/_entries and "
                    "store/write.regate. A ROW id, never a commit.",
    "0810a8fbe4e9": "dispatch row id (rule-arrives-with-its-gate, task/1346) "
                    "— the second FIX/MEASURED, converged in a meld: "
                    "riders rendered for a rule the base budget omitted (a "
                    "gate without its rule, explain sharing the model) and "
                    "typed gate identity flattened to a bare slug on markers. "
                    "Cited by inject/_entries._gate_plan and store/load.typed_id. "
                    "A ROW id, never a commit.",
    "fff5cef99aec": "dispatch row id (rule-arrives-with-its-gate, task/1346) "
                    "— the FIX/MEASURED on c23771519: a<->b gate cycles "
                    "accepted then erased by the plan, same-slug cross-type "
                    "gates validating one entry and injecting another, pinned "
                    "riders displacing later pins, a lexicon re-mint over a "
                    "rejected term inheriting its gloss and retirement, and "
                    "the posture guard living at one door. Cited by the cures "
                    "in store/write.py, store/load.py, store/cli.py, "
                    "inject/_entries.py, tasks.py, dispatches.py and "
                    "posture.py. A ROW id, never a commit.",
    "7c04b8437c00": "dispatch row id (a-fold-that-cannot-witness-is-silent, "
                    "round 3) — the row APPROVED and then corrected "
                    "themselves on within two minutes: the runtime cure was "
                    "sound and _witness_land's docstring still taught the "
                    "model the cure disproves. Cited by that docstring's "
                    "two-catch split. A ROW id, never a commit.",
    "ef0c543800a6": "dispatch row id (a-fold-that-cannot-witness-is-silent, "
                    "round 2) — the FIX that the typed witness state was "
                    "cured on the RETURN path and still flattened on the "
                    "RAISE path, so a crashed receipt READ serialized as the "
                    "NEGATIVE. Cited by landreq._witness_land's two exception "
                    "boundaries and by the read-crash arm. A ROW id, never a "
                    "commit.",
    "141d46c5a6ab": "land-request row id (a-fold-that-cannot-witness-is-silent) "
                    "— the tier re-review that caught an UNREADABLE witness "
                    "state being serialized as `unwitnessed`, under CLI text "
                    "prescribing `helm lr land`, i.e. handing the operator the "
                    "duplicate mint as the remedy. Cited by "
                    "landreq._existing_receipt, _witness_land, the close's "
                    "witness_unknown branch and the arm that used to pin the "
                    "collapse. A ROW id and never a commit: the row is what "
                    "outlives the lane's rebases, which is why the code cites "
                    "it rather than a sha.",
    "af391eab": "land-request row id (spark-spawn-wires-model) — the FIX "
                "verdict: --model wiring was spawn-only while launch.sh "
                "REFRESHES, so the first resume reverted a spark seat to the "
                "family default (sol+320k on a 76k model); cited by the "
                "persist/re-derive comments in helm/seat.py and the arms in "
                "tests/test_seat_spawn.py + tests/test_seat.py",
    # coordination-ledger rows (~/.helm/_global/dispatches.jsonl)
    # The five doorless land-request rows that motivated `--reason resolved`,
    # cited by _close_ladder_resolved and _CLOSE_POLARITY. They are ROW ids,
    # never commits: their whole defining property is that each row's own
    # reviewed tip reached trunk while the ROW could not close, so the row
    # outlives every sha a reader might otherwise reach for.
    "74144aca": "land-request row id (tripwire-reimplementation-confirm) — "
                "the SELF variant: a SUPERSEDE row whose own tip landed and "
                "which nothing supersedes",
    "ef0abe53": "land-request row id (chat-restore-journal-r1) — the TEMPORAL "
                "variant: content-verified post-landing, it could bind only "
                "SUPERSEDE because every receipt predated the dispatch",
    "87cf5f81": "land-request row id (clear-dedup-subsumption-at-the-fold), "
                "doorless with land_state LANDED",
    "28dd2907": "land-request row id (fleet-prints-a-raw-roster-key), "
                "doorless with land_state LANDED",
    "d94af579": "land-request row id (rebind-two-lock-race), doorless with "
                "land_state LANDED",
    "b71f8dab": "land-request row id (stop-guard-renewed-lease-allows-stop) — "
                "cited as the row DELIBERATELY EXCLUDED from resolved's "
                "acceptance set: its tip reaches trunk only by patch "
                "identity, so the ancestry rung refuses it and it goes "
                "through #177 instead",
    "ae7a5d6f": "review dispatch id (vacuous-rung-root-aliasing lane)",
    "6a8f9530": "review dispatch id (dispatch-rebind-wired blockers)",
    "2a899c9317": "review dispatch id (native-seat-runtime-family lane)",
    "a4051c69": "review dispatch id (orcaadopt round 3)",
    "90845108": "review dispatch id (orcaadopt round 7)",
    "1b4039cc": "review dispatch id (orcaadopt wrong-pane repro)",
    "6c351ce6": "dispatch id quoted in the auto-claim lease finding",
    "1ddf37fc": "dispatch row id (#142 round 4) — the ONE live v3 auto row "
                "minted under the PRE-PARENT key schema with no stored "
                "chain_root; cited by the pre-parent candidate rung in "
                "dispatches.send/_append_dispatch and its round-4 fixtures",
    "251d3c1a89bb": "land-request row id (the withdrawn specimen whose own "
                    "page headlined it CHANGES_REQUESTED — cited by _retired_label)",
    "766a5bf761f0": "FIX dispatch id (worktree-reap-cadence #157), "
                    "cited by the peek-reuse and summary-count repros it filed",
    "6d41adc116e3": "land-request row id (cross-tree gate refusal), "
                    "cited by _cross_tree_refusal INSTEAD of the reviewed tip: "
                    "that lane was re-derived rather than rebased, so no branch "
                    "contains its old sha and a fresh clone cannot resolve it — "
                    "the row outlives the rebase, the tip does not",
    "18a49b86cf1f": "review row id (stale-claim-visibility), cited by "
                    "_gc.py for their non-blocking claims_list finding INSTEAD "
                    "of the reviewed tip: that sha is reflog-only here, no "
                    "branch contains it, and a fresh clone reads it as dead",
    "aaf78be36525": "FIX dispatch id (docref authority rung) — the F2 "
                    "review that found this module executing the judged tree; "
                    "cited by _live_ledgers, and cited as a ROW rather than a "
                    "sha because the reviewed tip was orphaned by a rebase",
    "5e5bcfd5": "FIX dispatch id (cross-seat-surface-alias) — the "
                "nested-alias finding cited by seat._nested_surface_error and "
                "its fixture, seats/<family-a>/claude -> seats/<family-b>/claude",
    "ad157755": "review dispatch id (orcaadopt metadata-read finding)",
    "ad2aed0f": "review dispatch id (orcaadopt comm-read finding)",
    "da34a297": "FIX dispatch id (verdicts-join-on-tip-not-chain "
                "round 4) — the fab probe that measured live contest data "
                "retroactively annotating TERMINAL history (`LANDED ... ?1`); "
                "cited by the terminal gates in landreq.contest_report, "
                "ready_rung and the _line render comment. The row carries the "
                "reviewed tip and gate token, which is why the prose cites "
                "the ROW: the lane tip is local-only until the integrator "
                "lands, so a fresh clone reads the sha as dead while the "
                "ledger row outlives the rebase",
    "1267f509": "review-row id (another lane's exemption row)",
    "63cf625c": "dispatch row id (sender-attribution incident)",
    "6ccc7347": "FIX dispatch id (class-a-detector-verification lane), "
                "cited by the dispatch-rebind ACTUATORS declaration in "
                "helm/wiring.py",
    "97d8899a": "FIX dispatch id (contrary-honored-on-every-surface "
                "lane) — the composite honored+stalled row that split the "
                "surfaces again; cited by landreq.honored_display and its JS "
                "twin lrHonored as the incident the shared predicate exists "
                "to end",
    "d0c72ad9": "land-request row id (chat-restore-journal-r1) — a #177 "
                "ladder CONFIRMATION round the contrary classifier stamped "
                "CONTRARY (the hydra); cited by "
                "landreq.confirmation_row as the incident the row-kind "
                "recognition exists to end",
    "62c5a2cc": "land-request row id (chat-restore-journal-r1) — sibling "
                "confirmation round of the same hydra measurement, cited "
                "beside d0c72ad9 in landreq.confirmation_row",
    "68e1a449": "land-request row id (living-pipeline-r1) — third "
                "confirmation round of the hydra measurement, cited in "
                "landreq.confirmation_row",
    "a7235e643f64": "FIX dispatch id (boxes-job-routing review round 1)"
                    " — the 20-finding trust-boundary review whose authority/"
                    "challenge/identity laws gateroute.py cites by row id",
    "6580ad1dc5e0": "FIX dispatch id (boxes-job-routing review round 2)"
                    " — the review that read fab's ACCEPT? at its source and "
                    "proved it an eligibility proxy, never a consent "
                    "capability; cited by gateroute._eligibility_note, "
                    "_parse_markers (the challenge is not the framing) and "
                    "_bundle (per-run private dir)",
    "87a923eb": "land-request row id (stop-guard-renewed-lease-allows-stop) "
                "— fourth confirmation round of the hydra measurement, "
                "cited in landreq.confirmation_row",
    "1710265fd9a7": "FIX dispatch id (confirmation-rows-are-never-"
                    "contrary round 2) — the polarity-not-read finding: a "
                    "FIX-polarity confirmation-shaped row passed the first "
                    "cut of confirmation_row; cited by the shared "
                    "CONFIRMATION_POLARITIES constant it forced",
    "9e237a33": "dispatch row id (sender-attribution incident)",
    "1bc1b2c2": "dispatch row id (aspublic-test-fixtures lane)",
    "5aa24f98": "land-request row id — the predecessor the superseded ladder "
                "refused even with its chain head LANDED (chain-folding lane)",
    "c3b19436": "land-request row id — the unmeasurable parent of the "
                "chain-folding lane's live fixture",
    "f5436c21": "land-request row id — the gated child of that same fixture",
    "be5e82bbe0b5": "land-request row id — the 8-day owner-sign-guard row the "
                "chain-polarity door opens, cited in the _chain_polarity "
                "provenance (12-hex here because the prose quotes the ledger's "
                "own display width, not git's)",
    "b5c7a8df67df": "land-request row id — the chained SUPERSEDE round carrying "
                "the owner's Layer-B ruling, the verdict that door reads",
    "4fdd32fcf664": "land-request row id — at 7.9d the OLDEST row helm has, "
                "unclosable because _landing_proof scored its vanished object "
                "`unknown` while the subsumed door needs `absent`; cited in "
                "the _vanished_proof provenance (12-hex because the prose "
                "quotes the ledger's display width, not git's)",
    "b970911edbe6": "land-request row id — the 7.8d sibling of the above, same "
                "refusal and the same cure, cited in the same docstring",
    # gate receipts (`helm gate run` mints these into the receipt ledger)
    "79b508b87b1ca6c5": "gate receipt id cited in landgate.py provenance",
    "756b936006bf0e47": "the v4 receipt whose SILENT SKIP under a pre-v4 reader "
                        "is why gate.py and gateimport.py teach v4 one commit "
                        "before anything mints it — cited at both sites and in "
                        "test_gate_import's HostBoundVersionTest",
    "80f95d7e": "r5 gate receipt id (dispatch-rebind-wired matrix)",
    "3caeb50c": "gate receipt token carried by f5436c21 (chain-folding lane)",
    # handoff.py cites its own review rounds as "gate <id>" — six
    # receipts, two abbreviated in prose.
    "0b18e133f272a1e3": "review-round gate receipt (handoff-shelf lane)",
    "c8fd53acb0cc435f": "review-round gate receipt (handoff-shelf lane)",
    "dcf603c8b0bef585": "review-round gate receipt (handoff-shelf lane)",
    "dcf603c8": "dcf603c8b0bef585 abbreviated in a three-gate prose list",
    "d56a646e": "review-round gate receipt d56a646e73fa17cd, abbreviated",
    "4ef1aeee": "review-round gate receipt 4ef1aeee612d3612, abbreviated",
    "73b7f2cecb441f8f": "review-round gate receipt (handoff-shelf lane)",
    # helm/rowstate.py + helm/rowworld.py — the rows that MEASURE the defect
    # rowstate exists to cure: the ledger reports a stage no artifact supports.
    # Every one is a land-request ROW id, never a commit; several are precisely
    # the rows whose lane branch was reaped, so no sha survives to cite.
    "a1f5aee91903": "land-request row id (fleet-notes-freshness) — the owner's "
                    "row: read AWAITING_BUILD while trunk commit fa0915b22104 "
                    "named it `chain a1f5aee9`; cited by rowstate's module "
                    "docstring as the measured defect",
    "b2bc5e4a411a": "land-request row id (bigfile-split-seat-py) — the build "
                    "row whose recorded tip is its BASE and IS an ancestor of "
                    "trunk while the lane sat 19 commits ahead; cited by "
                    "rowstate.subject_tip as the base-vs-output trap",
    "b73f16ca721a": "land-request row id (bigfile-split-landreq-py) — "
                    "superseded by a review round whose reviewed tip was "
                    "already on trunk; cited by rowstate._predates_dispatch",
    "7e2219140e90": "land-request row id (kanban-verified-superseded-is-closed)"
                    " — second of the same three; cited by "
                    "rowstate._predates_dispatch",
    "669393cb4d8a": "land-request row id (fleet-prints-empty-scrub-rebuild) — "
                    "third of the same three; cited by "
                    "rowstate._predates_dispatch",
    "ce2194293b0d": "land-request row id (bigfile-split-seat-py-final-review) —"
                    " the gated row whose receipt-bound head is neither on "
                    "trunk nor a branch head; cited by rowworld._commit_trees",
    "9b5029474fb63d13": "the review-round gate receipt on the rowstate "
                        "lane (exact tip f7f4ede7, 13 blockers) — cited by "
                        "rowstate's lifecycle vocabulary as the verdict whose "
                        "blockers this cure round answers",
}

# Load-bearing CONTENT identities. Each key is the exact 40-hex value from
# `git patch-id --stable`; each value is (base tree, tip tree, reason), with
# full object ids so staged data cannot smuggle git options into the snapshot
# evaluator. Admission recomputes the key from `git diff base tip` — cat-file
# can never vouch for a patch-id because it is not a Git object.
PATCH_IDS = {
    # MUST-HIT content identity 1f7bacc86ebe5849147f11e33494cf62da89da45.
    # It is prose here deliberately: the installed old snapshot exempts SELF,
    # so the category's bootstrap commit stays guardable without a blanket skip.
    "1f7bacc86ebe5849147f11e33494cf62da89da45": (
        "0d2d5933c516ee8d5a2ef754ff20bbba51452a6d",
        "ba5e74063ea085267769870ccb30033f06e095f2",
        "landed_state content identity — git patch-id --stable prints this for "
        "both lane tip 5905f65 and landed commit ba5e740",
    ),
}

# Non-git tokens NO ledger can vouch for, each with the reason it is exempt.
# Keyed by the exact token as it appears in prose. This list once held 40
# entries; the 29 that were ledger rows now live in LEDGER_CITED above, where
# the audit can refuse them. What remains is what has no authority anywhere:
# harness-store identifiers and shaguard's evidence. An entry that stops
# matching any scanned token is a stale exemption and fails the suite, so the
# list cannot accrete.
SKIP = {
    "7f454c46": "NOT A SHA AT ALL — the four ELF MAGIC BYTES, cited in "
                "helm/hooks.py's `_elf_launch` as the literal header a file "
                "must start with. It matches the rung's 7-40 hex pattern by "
                "pure coincidence of being hex, and no registry keyed on "
                "PROVENANCE fits it: it is not a commit, not a ledger row and "
                "not a patch id. Registering it anywhere else would teach the "
                "guard that a byte constant is an artifact reference. The "
                "citation is load-bearing prose — the reviewer's exact probe "
                "was `7f454c46` plus 60 NULs, which helm certified as "
                "launch-ok while execve answered ENOEXEC — so it stays in the "
                "comment and is exempted here as the CONSTANT it is.",
    # THE GUARD CORRECTED ME AGAIN, the same way it corrected the author of
    # the note below. I filed this in LEDGER_CITED and the registry rung
    # refused: LEDGER_CITED is a claim that a LEDGER ROW exists, and this is
    # a COMMIT — one in another project's repository, which this repo
    # deliberately cannot resolve. Reaching for the nearest registry that
    # unblocks you is how a guard gets taught to lie.
    # SKIP, NOT LEDGER_CITED, and the guard is what corrected me. I first
    # registered these three as ledger citations to get past the
    # unaccountable-token rung, and the registry rung refused: an entry in
    # LEDGER_CITED is a CLAIM THAT A LEDGER ROW EXISTS, and these are not
    # rows, they are prose examples. Reaching for the nearest registry that
    # unblocks you is how a guard gets taught to lie.
    #
    # None of them is new. All three have been in helm/seats.py on trunk for
    # a long time; the seats.py split MOVED the docstrings carrying them into
    # helm/seats_roster.py, and this rung scans ADDED lines — so relocation is
    # what made pre-existing text newly visible. Every big-file split will hit
    # this, and the answer is a category judgement, not a skip flag.
    "32285620": "truncated claude session id used as an example in "
                "seats_roster's identity docstring — written with a trailing "
                "ellipsis, which is the giveaway",
    "2789564": "a PID, cited in seats_roster.seat_for_session's docstring as "
               "the process whose row a later claimant pushed out",
    "1785568214": "a MELD EPOCH (meld e:1785568214) — a chat-room "
                  "coordination id, never a commit",
    # harness/runtime identifiers — never git objects, never helm ledger rows.
    "f0ad7476": "claude session id (console-design row-eviction incident)",
    "56a628d4": "claude session id (compacted-pane incident)",
    "8d2e1ff0": "claude history sid (history-as-address)",
    "58e6f94a": "claude session id (the sibling sid from the same passage)",
    "69d709b0": "dregg cell id (faucet grant incident)",
    "a2f8ba2a": "request id inside a verbatim-quoted proxy log line",
    # shaguard.py exhibits — the module documents sha-fabrication incidents,
    # so two of these are deliberately fake and the real ones are pre-rebase
    # history. They must stay verbatim: they are evidence, not citations.
    "a009d56": "shaguard exhibit: real short sha later padded into a fake",
    "1161c6f93798aa1f": "shaguard exhibit: the FABRICATED padded announcement",
    "1161c6f7a702": "shaguard exhibit: the real tip (pre-rebase history)",
    "8f130075ebf5": "shaguard exhibit: fabricated middle-hex announcement",
    "8f130078e0c4": "shaguard exhibit: the real tip (pre-rebase history)",
    # vcs.landed_state exhibits — the owner's measurement that
    # ancestry answers the wrong question. The pair must stay verbatim for the
    # same reason as the shaguard rows: they are the evidence, not a citation.
    # 5905f65's UNREACHABILITY IS THE FINDING, and it resolves today only
    # because its lane branch still exists — which the reaper that docstring
    # describes is built to retire. Without this row a future suite would read
    # the fix WORKING as a dead citation. (ba5e740, the landed twin, is on
    # trunk permanently and needs no exemption.)
    "5905f65": "landed_state exhibit: the lane tip whose patch landed as "
               "ba5e740 under a different sha",
}


def _git(repo, *args, input_text=None):
    return subprocess.run(("git", "-C", repo) + args, input=input_text,
                          capture_output=True, text=True)


SELF = "helm/docref_guard.py"

# The tree this rung polices, kept EQUAL to the tree the registry can vouch
# for — `tests/test_docstring_refs` scans helm/ prose only, by its own stated
# contract, so a token anywhere else has no legal exemption.
POLICED = "helm/"


def _web_ui_source(path):
    """True for browser source fragments outside the prose registry's scope."""
    return (path == "helm/web_ui.html"
            or (path.startswith("helm/web_ui/") and path.endswith(".part")))


def staged_tokens(repo, parents=None):
    """[(path, token)] for hex tokens this commit ADDS under `POLICED`. Added
    lines only: a citation you did not write is not yours to fix, and a rung
    that says otherwise gets disabled wholesale.

    SCOPED TO THE TREE THE REGISTRY CAN VOUCH FOR, and that equality is the
    point. This rung read EVERY staged file while the registry only holds
    tokens cited under helm/ — so a hex token added in tests/ was refused with
    NO LEGAL CURE: register it and `test_no_exemption_is_stale_or_double_booked`
    calls the entry stale (its scan is helm/-only, stated in that module's own
    contract); leave it unregistered and this rung blocks the commit. The only
    remaining move is HELM_DOCREF_SKIP=1, which is exactly how a guard earns a
    blanket-skip reflex and stops guarding anything. Measured: a
    fixture gate token in tests/ refused a commit, was registered in SKIP, and
    took the whole-suite gate RED on the staleness check one run later.

    THE REGISTRY FILE IS EXEMPT FROM ITSELF, and this is not a convenience --
    without it the guard BLOCKS ITS OWN CURE. Adding a LEDGER_CITED or SKIP
    entry adds a line whose text IS the token, so the scanner reads the act of
    REGISTERING a citation as MAKING an unaccounted one, and refuses the exact
    commit that fixes the refusal. The first person to hit that has no move
    except disabling the rung wholesale. Caught by running the scanner against
    its own staged diff -- it refused this file on three tokens, two of them
    registry keys and one an example inside this docstring -- which no amount
    of reading the code would have shown.

    A token here is a REGISTRATION or an example OF one, never a claim that
    some commit exists; the suite's `--audit` entrypoint is what polices the
    registry's own contents against the live ledgers."""
    parents = _parents(repo) if parents is None else parents
    if len(parents) > 1:
        # ADDED MEANS ABSENT FROM EVERY PARENT. HEAD is one side
        # of a merge, so a citation the OTHER side wrote read as this commit's
        # and this rung refused a merge over a token its trunk had already
        # registered. Per-parent parses, intersected on (path, token): a
        # citation every parent lacks is this commit's. One parse that cannot
        # be read makes the whole answer None, exactly as one unreadable diff
        # does for a single parent — the caller's WARNING path, never a
        # narrower set.
        sets, first = [], None
        for ref in parents:
            got = _tokens_against(repo, (ref,))
            if got is None:
                return None
            first = got if first is None else first
            sets.append(set(got))
        keep = set.intersection(*sets) if sets else set()
        return [row for row in first if row in keep]
    return _tokens_against(repo, ())


def _tokens_against(repo, base=()):
    """staged_tokens' parse against ONE base (empty = HEAD, git's default)."""
    p = _git(repo, "-c", "diff.noprefix=false", "diff", "--cached",
             "--unified=0", "--no-color", "--no-ext-diff", *base)
    if p.returncode != 0:
        return None
    out, path, in_hunk = [], "?", False
    for line in p.stdout.splitlines():
        if line.startswith("diff --git "):
            path, in_hunk = "?", False
            continue
        if _HUNK.fullmatch(line):
            in_hunk = True
            continue
        if line.startswith("@@"):
            raise _UnsupportedDiff("unsupported hunk header")
        if not in_hunk:
            if line.startswith("+++ "):
                path = _added_path(line)
                if path is None:
                    raise _UnsupportedDiff("unreadable b-side path")
            continue
        if not line.startswith("+"):
            continue
        # Inside a hunk the first byte is the authority. An ADDED line whose
        # own text starts `++` renders as `+++...`, byte-identical to the file
        # header outside a hunk; a prefix-only `not startswith("+++")` filter
        # dropped it and let a staged citation pass this guard unseen.
        # Everything outside POLICED is exempt because the registry can only
        # vouch for helm/ prose — measured twice, from both ends:
        # a tests/ gate token had NO legal cure (register -> staleness audit
        # red; don't -> commit blocked), and a fixture's orca-handle hex
        # (tests/fixtures/codex3-100pct-tail.txt) is runtime identity, never a
        # claim about history. Scope == the tree the registry audits, or the
        # rung eats its own exemptions.
        if (path == SELF or not path.startswith(POLICED)
                or _web_ui_source(path)):
            continue
        for tok in HEX.findall(line):
            # PURE-DECIMAL IS A NUMBER, NOT A CITATION — the suite's scan
            # (`checked = {... if not t.isdigit()}`) already says so, and a
            # rung stricter than the suite on a NUMERIC LITERAL is not
            # stricter, it is wrong: `1000000` in a comment about a size cap
            # names no object in any ledger. Same rule, both layers.
            if tok.isdigit():
                continue
            out.append((path, tok))
    return out


def remote_reachable(repo, tok):
    """True iff a FRESH CLONE could reach it — a REMOTE ref contains it.

    `git branch -r --contains` and NOT `-a`: `-a` counts local branches, so it
    vouches for objects only this machine has. That distinction cost two gate
    rounds — a reviewed tip whose `cat-file -e` succeeded and
    whose `-a` returned ONE local lane branch, while `-r` returned zero and the
    fab read it dead.

    THE SHA IS DELIBERATELY NOT QUOTED HERE. It was, and the suite's prose scan
    caught it on the fab: an 8-hex token in this docstring is a CITATION like
    any other, and that one is by definition unreachable from any remote — the
    exact property being described. A docstring about not citing unreachable
    shas cited one. Naming the shape is what carries the lesson; the literal
    only adds a token nobody else's clone can resolve."""
    if _git(repo, "cat-file", "-e", tok + "^{commit}").returncode != 0:
        return False
    return bool(_git(repo, "branch", "-r", "--contains", tok).stdout.strip())


REGISTRY_NAMES = ("LEDGER_CITED", "SKIP", "PATCH_IDS")
#: Sentinel for "this parent had no such key" — a registry value
#: could be anything, so absence needs its own token.
_MISSING = object()


def committing_registry(repo):
    """Registry keys as the tree BEING COMMITTED spells them; () if unreadable.

    unaccounted() resolves registries from the module that is RUNNING, and the
    running module is the SNAPSHOT under .git/hooks/.helm-scanners/ — shared by
    every worktree, deliberately, so no lane's edit can rewrite every other
    room's guard.

    LOGIC STAYS SNAPSHOT-OWNED; ONLY DATA COMES FROM THE TREE. The staged file
    is read with ast.literal_eval, NEVER import or exec. A guard that executed
    the tree it judges would hand any lane a shell inside the rung.

    A direct library call with an unreadable or unparseable index copy yields
    () and SAYS SO. The authorizing main() path parses the same staged registry
    first through added_registry_entries() and REFUSES before reaching this
    fallback, so malformed authority fails closed rather than losing only the
    widening."""
    registries = _registries_at(repo, ":")
    if registries is None:
        print("[helm docref] note: staged %s does not parse — judging by the "
              "installed snapshot alone" % SELF, file=sys.stderr)
        return ()
    return tuple(k for name in REGISTRY_NAMES for k in registries[name])


def _registries_at(repo, ref):
    """Registry mappings at a git ref; empty mappings if absent, None if bad.

    CATEGORY IS AUTHORITY. Returning one flat key list caused SKIP runtime ids
    to inherit LEDGER_CITED's citation_vouched test. Values stay attached here
    so SKIP reasons and PATCH_IDS diff witnesses can be admitted by the
    snapshot evaluator without executing the judged tree."""
    p = _git(repo, "show", ref + SELF)
    out = {name: {} for name in REGISTRY_NAMES}
    if p.returncode != 0:
        return out
    try:
        tree = ast.parse(p.stdout)
    except SyntaxError:
        return None
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        names = ({t.id for t in node.targets if isinstance(t, ast.Name)}
                 & set(REGISTRY_NAMES))
        if not names:
            continue
        try:
            value = ast.literal_eval(node.value)
        except (ValueError, TypeError):
            return None
        if not isinstance(value, dict):
            return None
        for name in names:
            out[name] = value
    return out


def added_registry_entries(repo):
    """New or changed registry entries; None if either tree is malformed.

    Unchanged entries were already admitted and are not re-litigated on every
    commit. Moving a key between registries or changing its reason/witness IS a
    new authority claim and is re-admitted by the destination category."""
    now = _registries_at(repo, ":")
    if now is None:
        return None
    # NEW AUTHORITY IS AUTHORITY NO PARENT ALREADY DECLARED. Against HEAD alone
    # a merge re-litigated every registry entry its other side had added, and
    # the entries it cannot re-prove (a lane branch's sha that never reached a
    # remote) refused the merge.
    befores = []
    for ref in _parents(repo) or ["HEAD"]:
        before = _registries_at(repo, ref + ":")
        if before is None:
            return None
        befores.append(before)
    return {name: {k: v for k, v in now[name].items()
                   if all(before[name].get(k, _MISSING) != v
                          for before in befores)}
            for name in REGISTRY_NAMES}


def citation_vouched(tok, rows, receipts):
    """True iff the token prefixes exactly one id across BOTH namespaces.

    Exact is not a shortcut: one exact row plus one longer id sharing that
    prefix still names two artifacts. Per-ledger uniqueness is insufficient.

    LIVES HERE, BESIDE THE REGISTRY IT POLICES, for the same reason the
    registry itself moved here — the pre-commit rung cannot import from
    tests/, and a second copy of this predicate would drift from the first
    invisibly. tests/test_docstring_refs.py imports it from this module."""
    t = tok.strip().lower()
    hits = sum(rid.startswith(t) for snap in (rows, receipts) for rid in snap)
    return hits == 1


def _trusted_root():
    """The tree this rung's own LOGIC came from — never the tree it judges.

    A snapshot lives at <shared>/.git/hooks/.helm-scanners/docref_guard.py, so
    four levels up is the shared checkout install-guard took it from. Running
    from source, __file__ is <repo>/helm/docref_guard.py and two levels up is
    that repo. Either way the answer is the tree that OWNS this code."""
    here = os.path.dirname(os.path.abspath(__file__))
    if os.path.basename(here) == ".helm-scanners":
        return os.path.dirname(os.path.dirname(os.path.dirname(here)))
    return os.path.dirname(here)


def _live_ledgers():
    """(rows, receipts, why_undecidable) from the TRUSTED tree's helm package.

    NOTHING FROM THE JUDGED WORKTREE IS IMPORTED OR EXECUTED, and that is the
    whole point of this function's existence (F2 on dispatch
    aaf78be36525). THE CITATION IS A DISPATCH ID, NOT A SHA, and that is the
    lesson rather than a formatting choice: this docstring first cited the
    reviewed TIP, a rebase orphaned it, and the suite's own dead-citation arm
    caught it on the very lane that exists to police citations. A lane commit
    is rebasable by construction, so citing one only re-arms the trap; a
    ledger row id is stable under every rebase this lane will ever take. The
    first cure shelled out to <repo>/tests/test_docstring_refs.py — the judged
    tree's own script, and not even its STAGED copy. The repro replaced only
    the unstaged working-tree file with one that wrote a marker: guard rc=0,
    marker
    written, script staged=0. Any lane got a shell inside the snapshot rung and
    could silently vouch its own key. That is precisely the no-exec boundary
    committing_registry was written to hold, breached one function later by the
    author of the boundary.

    An EMPTY ledger is undecidable, never a pass: a planted or sandboxed home
    reads as zero rows, and zero rows must never be mistaken for verification."""
    root = _trusted_root()
    if root not in sys.path:
        sys.path.insert(0, root)
    try:
        from helm import dispatches, gate
    except Exception as e:
        return {}, {}, "helm package unavailable (%s)" % e.__class__.__name__
    try:
        snap, unavailable = dispatches.snapshot()
    except Exception as e:
        return {}, {}, "coordination ledger unreadable (%s)" % e.__class__.__name__
    if unavailable:
        return {}, {}, "coordination ledger unreachable: %s" % unavailable
    if not snap:
        return {}, {}, ("coordination ledger read EMPTY — a planted or "
                        "sandboxed home is not the live ledger")
    try:
        receipts, gate_err, _skipped = gate.receipts()
    except Exception as e:
        return {}, {}, "gate ledger unreadable (%s)" % e.__class__.__name__
    if gate_err:
        return {}, {}, "gate receipt ledger unreachable: %s" % gate_err
    if not receipts:
        return {}, {}, ("gate receipt ledger read EMPTY — a planted or "
                        "sandboxed home is not the live ledger")
    return snap, {str(r["id"]): r for r in receipts}, None


def vouch_added_keys(keys, ledgers=None):
    """-> (unvouched, why_undecidable) for NEW LEDGER_CITED keys only.

    TAKES KEYS AS DATA. The evaluator is this module — snapshot-owned — and
    the judged tree contributes nothing but staged key strings. This strict
    citation_vouched behavior is deliberately NOT shared by SKIP or PATCH_IDS:
    those categories have their own object-absence and diff-recomputation
    admission tests.

    LEDGER_CITED FAILS CLOSED. This arm fires only when a commit adds ledger
    authority, so an unreadable ledger blocks those commits and nothing else.
    Passing an unverifiable row would permanently silence a dead citation."""
    if not keys:
        return (), None
    rows, receipts, why = _live_ledgers() if ledgers is None else ledgers
    if why:
        return tuple(keys), why
    return tuple(k for k in keys
                 if not citation_vouched(k, rows, receipts)), None


def skip_rejections(repo, entries):
    """{token: reason} for NEW SKIP entries that cannot silence prose.

    SKIP is not ledger authority, but it is still a permanent suppression. A
    key must explain itself and must NOT resolve as any Git object in the
    committing repository; if it does, it is a citation and belongs in
    LEDGER_CITED (or needs a reachable commit) instead."""
    bad = {}
    for tok, reason in entries.items():
        if not isinstance(tok, str) or not HEX.fullmatch(tok):
            bad[str(tok)] = "key must be 7-40 lowercase hex"
        elif not isinstance(reason, str) or not reason.strip():
            bad[tok] = "entry must carry a non-empty reason"
        else:
            probe = _git(repo, "cat-file", "--batch-check",
                         input_text=tok + "\n")
            fields = probe.stdout.strip().split()
            if probe.returncode != 0 or fields != [tok, "missing"]:
                bad[tok] = (("resolves as a Git object; it is a citation, not "
                             "a runtime identifier")
                            if probe.returncode == 0 and len(fields) >= 3
                            else "Git object absence could not be proven")
    return bad


def stable_patch_id(repo, base, tip):
    """(40-hex patch-id, error) recomputed from one exact tree-to-tree diff.

    base and tip are full object ids, not revision expressions. They are staged
    DATA supplied by the judged tree, so constraining them before git sees them
    preserves the snapshot-owned/no-exec boundary and prevents option injection.
    """
    if not (isinstance(base, str) and FULL_HEX.fullmatch(base)
            and isinstance(tip, str) and FULL_HEX.fullmatch(tip)):
        return None, "diff endpoints must be full 40-hex object ids"
    diff = _git(repo, "diff", "--no-ext-diff", "--binary", base, tip, "--")
    if diff.returncode != 0:
        return None, "diff could not be read"
    if not diff.stdout:
        return None, "diff is empty and has no content identity"
    got = _git(repo, "patch-id", "--stable", input_text=diff.stdout)
    if got.returncode != 0:
        return None, "git patch-id --stable failed"
    rows = [line.split() for line in got.stdout.splitlines() if line.strip()]
    if len(rows) != 1 or len(rows[0]) != 2 or not FULL_HEX.fullmatch(rows[0][0]):
        return None, "git patch-id --stable returned no single identity"
    return rows[0][0], None


def patch_id_rejections(repo, entries):
    """{token: reason} for PATCH_IDS not verified by their recorded diff."""
    bad = {}
    for tok, witness in entries.items():
        if not isinstance(tok, str) or not FULL_HEX.fullmatch(tok):
            bad[str(tok)] = "patch-id key must be exactly 40 lowercase hex"
            continue
        if not (isinstance(witness, (tuple, list)) and len(witness) == 3):
            bad[tok] = "entry must be (base, tip, non-empty reason)"
            continue
        base, tip, reason = witness
        if not isinstance(reason, str) or not reason.strip():
            bad[tok] = "entry must carry a non-empty reason"
            continue
        got, why = stable_patch_id(repo, base, tip)
        if why:
            bad[tok] = why
        elif got != tok:
            bad[tok] = "recomputed patch-id is %s" % got
    return bad


def unaccounted(repo, tokens):
    """[(path, token)] this repository cannot account for.

    The accounted set is the snapshot's registry UNION the committing tree's —
    see committing_registry for why the union, and why it can only widen."""
    accounted = (set(LEDGER_CITED) | set(SKIP) | set(PATCH_IDS)
                 | set(committing_registry(repo)))
    return [(path, tok) for path, tok in tokens
            if tok not in accounted and not remote_reachable(repo, tok)]


def main(argv):
    repo = os.environ.get("HELM_DOCREF_REPO") or os.getcwd()
    if "--staged" not in argv:
        print("usage: docref_guard.py --staged", file=sys.stderr)
        return 2
    try:
        tokens = staged_tokens(repo)
    except _UnsupportedDiff as exc:
        # A readable diff outside the parser's grammar is UNKNOWN, never a
        # clean staged set. Treating it like git's read failure switched this
        # guard off for the ENTIRE commit — the opposite direction from the
        # staged-registry parser three lines below.
        print("[helm docref] REFUSED: staged diff uses unsupported syntax "
              "(%s) — citation scan is UNKNOWN" % exc, file=sys.stderr)
        return 1
    if tokens is None:
        # UNREADABLE INDEX IS NOT A CLEAN COMMIT, but it is also not this
        # rung's business to block on: warn and let the gate be the backstop.
        print("[helm docref] WARNING: could not read the staged diff — "
              "citation check SKIPPED, the gate remains the backstop",
              file=sys.stderr)
        return 0
    # CATEGORY-SPECIFIC AUTHORITY, before those entries can answer the
    # accountability check below. Flattening these mappings was the defect:
    # citation_vouched is strict and correct for LEDGER_CITED, a category error
    # for SKIP, and incapable of proving content identity for PATCH_IDS.
    added = added_registry_entries(repo)
    if added is None:
        print("[helm docref] REFUSED: staged registry data does not parse — "
              "authority is UNKNOWN", file=sys.stderr)
        return 1
    unvouched, ledger_why = vouch_added_keys(
        tuple(sorted(added["LEDGER_CITED"])))
    rejected = {
        "LEDGER_CITED": {tok: ("no live ledger vouches for it"
                               + ((" (%s)" % ledger_why) if ledger_why else ""))
                         for tok in unvouched},
        "SKIP": skip_rejections(repo, added["SKIP"]),
        "PATCH_IDS": patch_id_rejections(repo, added["PATCH_IDS"]),
    }
    if any(rejected.values()):
        print("[helm docref] REFUSED: new registry authority was not proven:",
              file=sys.stderr)
        for name in REGISTRY_NAMES:
            for tok, reason in sorted(rejected[name].items()):
                print("    %s  %s: %s" % (tok, name, reason), file=sys.stderr)
        if ledger_why and rejected["LEDGER_CITED"]:
            print("  LEDGER_CITED fails CLOSED when the ledgers cannot be read; "
                  "this arm fires only on commits that add ledger authority.",
                  file=sys.stderr)
        print("  SKIP requires a non-object runtime token plus a reason. "
              "PATCH_IDS requires a full stable patch-id recomputed from its "
              "recorded diff. LEDGER_CITED requires one unambiguous live row "
              "or receipt. Skip once: HELM_DOCREF_SKIP=1", file=sys.stderr)
        return 1

    bad = unaccounted(repo, tokens)
    if not bad:
        return 0
    print("[helm docref] REFUSED: %d cited token(s) this repo cannot account "
          "for — a fresh clone (and the fab) reads them as dead:"
          % len(bad), file=sys.stderr)
    for path, tok in bad:
        print("    %s  %s" % (tok, path), file=sys.stderr)
    # THE CURE MUST NAME ITS OWN SECOND STEP, and it once named a
    # step that COULD NOT WORK FROM A LANE: `helm work install-guard --apply`
    # re-takes the snapshot from the SHARED CHECKOUT, so a lane's registry
    # entry stayed unreachable and the only move left was the blanket skip.
    # committing_registry now reads the registry from the COMMITTING TREE, so
    # the second step is simply STAGING the entry. Kept as a comment because
    # the history is the reason the message is worded this way.
    # This rung runs from a SNAPSHOT in
    # .git/hooks/.helm-scanners/ — deliberately, so a lane's unstaged edit
    # cannot change every worktree's guard — which means editing the registry
    # in the SOURCE tree leaves the running rung unchanged and STILL REFUSING.
    # Measured: a row id registered, the suite green, and
    # this rung refused the very commit that fixed it. That is the same
    # blocks-its-own-cure shape as the registry self-exemption, in a second
    # form, and a FIX line that omits the reinstall is what made it one.
    print("  FIX: cite a commit reachable from a REMOTE ref (a local branch is "
          "invisible to every other clone), or register it in LEDGER_CITED, "
          "PATCH_IDS, or SKIP in helm/docref_guard.py according to its actual "
          "authority — AND STAGE THAT EDIT, which is the "
          "whole second step: the registry is read from the tree being "
          "committed, so an unstaged entry is invisible to this rung. "
          "Skip once: HELM_DOCREF_SKIP=1", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
