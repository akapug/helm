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

IT VALIDATES THE WHOLE OBJECT AND REFUSES BY NAME, never enumerating token
TYPES. The registry already holds a commit sha, a land-request row id, a
dispatch id, a gate receipt id, a lane tip whose patch landed under a different
sha, and a git PATCH-ID that is not a commit at all and resolves nowhere by
design. Enumerating those types is how the next case lands outside the set --
it is how these entries accumulated. So: a staged token must be ACCOUNTED FOR
(registered here, or reachable from a REMOTE ref) and anything this cannot
classify it REFUSES BY NAME. It never claims a token is DEAD -- only that
nothing in this repository accounts for it, which is the strongest claim
repo-local state supports at pre-commit speed.

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


LEDGER_CITED = {
    "441c4491": "land-request row id (supersedes-leaves-parent-open) — --supersedes left the parent looking actionable; cited by the annotation rung in dispatches._append_dispatch and by superseded_parent_sweep",
    "a6d5d95f": "dispatch/chain id (beacons-attendance review, chain_root "
                "a6d5d95fb7f02e4a…) — the REWORK verdict whose two residues "
                "task #199 closed; cited by escalate's revalidation law in "
                "helm/beacons.py (\"concurrent passes deliver same edge "
                "twice\") and by the delivery-edge tests in "
                "tests/test_beacons.py",
    "069406da7cf6": "land-request row id (cell-bin-doc-vs-code) — the rebind whose room stayed fenced under the walled OLD recipient; cited by dispatches.rebind_room_fence as the incident that rung exists to end",
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
                "variant: ds4pro content-verified it and could bind only "
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
    "ae7a5d6f": "codex review dispatch id (vacuous-rung-root-aliasing lane)",
    "6a8f9530": "codex review dispatch id (dispatch-rebind-wired blockers)",
    "2a899c9317": "codex review dispatch id (native-seat-runtime-family lane)",
    "a4051c69": "codex review dispatch id (orcaadopt round 3)",
    "90845108": "codex review dispatch id (orcaadopt round 7)",
    "1b4039cc": "codex review dispatch id (orcaadopt wrong-pane repro)",
    "6c351ce6": "dispatch id quoted in the auto-claim lease finding",
    "1ddf37fc": "dispatch row id (#142 round 4) — the ONE live v3 auto row "
                "minted under the PRE-PARENT key schema with no stored "
                "chain_root; cited by the pre-parent candidate rung in "
                "dispatches.send/_append_dispatch and its round-4 fixtures",
    "251d3c1a89bb": "land-request row id (the withdrawn specimen whose own "
                    "page headlined it CHANGES_REQUESTED — cited by _retired_label)",
    "766a5bf761f0": "codex-2 FIX dispatch id (worktree-reap-cadence #157), "
                    "cited by the peek-reuse and summary-count repros it filed",
    "6d41adc116e3": "codex-2 land-request row id (cross-tree gate refusal), "
                    "cited by _cross_tree_refusal INSTEAD of the reviewed tip: "
                    "that lane was re-derived rather than rebased, so no branch "
                    "contains its old sha and a fresh clone cannot resolve it — "
                    "the row outlives the rebase, the tip does not",
    "18a49b86cf1f": "codex-2 review row id (stale-claim-visibility), cited by "
                    "_gc.py for their non-blocking claims_list finding INSTEAD "
                    "of the reviewed tip: that sha is reflog-only here, no "
                    "branch contains it, and a fresh clone reads it as dead",
    "aaf78be36525": "codex FIX dispatch id (docref authority rung) — the F2 "
                    "review that found this module executing the judged tree; "
                    "cited by _live_ledgers, and cited as a ROW rather than a "
                    "sha because the reviewed tip was orphaned by a rebase",
    "5e5bcfd5": "codex-2 FIX dispatch id (cross-seat-surface-alias) — the "
                "nested-alias finding cited by seat._nested_surface_error and "
                "its fixture, seats/ds4pro/claude -> seats/codex/claude",
    "ad157755": "codex review dispatch id (orcaadopt metadata-read finding)",
    "ad2aed0f": "codex review dispatch id (orcaadopt comm-read finding)",
    "1267f509": "review-row id (another lane's exemption row)",
    "63cf625c": "dispatch row id (sender-attribution incident)",
    "6ccc7347": "codex FIX dispatch id (class-a-detector-verification lane), "
                "cited by the dispatch-rebind ACTUATORS declaration in "
                "helm/wiring.py",
    "97d8899a": "codex-2 FIX dispatch id (contrary-honored-on-every-surface "
                "lane) — the composite honored+stalled row that split the "
                "surfaces again; cited by landreq.honored_display and its JS "
                "twin lrHonored as the incident the shared predicate exists "
                "to end",
    "d0c72ad9": "land-request row id (chat-restore-journal-r1) — a #177 "
                "ladder CONFIRMATION round the contrary classifier stamped "
                "CONTRARY (the hydra, measured 2026-08-05); cited by "
                "landreq.confirmation_row as the incident the row-kind "
                "recognition exists to end",
    "62c5a2cc": "land-request row id (chat-restore-journal-r1) — sibling "
                "confirmation round of the same hydra measurement, cited "
                "beside d0c72ad9 in landreq.confirmation_row",
    "68e1a449": "land-request row id (living-pipeline-r1) — third "
                "confirmation round of the hydra measurement, cited in "
                "landreq.confirmation_row",
    "a7235e643f64": "codex FIX dispatch id (boxes-job-routing review round 1)"
                    " — the 20-finding trust-boundary review whose authority/"
                    "challenge/identity laws gateroute.py cites by row id",
    "6580ad1dc5e0": "codex FIX dispatch id (boxes-job-routing review round 2)"
                    " — the review that read fab's ACCEPT? at its source and "
                    "proved it an eligibility proxy, never a consent "
                    "capability; cited by gateroute._eligibility_note, "
                    "_parse_markers (the challenge is not the framing) and "
                    "_bundle (per-run private dir)",
    "87a923eb": "land-request row id (stop-guard-renewed-lease-allows-stop) "
                "— fourth confirmation round of the hydra measurement, "
                "cited in landreq.confirmation_row",
    "1710265fd9a7": "codex-2 FIX dispatch id (confirmation-rows-are-never-"
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
    "79b508b87b1ca6c5": "kimi's gate receipt id cited in landgate.py provenance",
    "756b936006bf0e47": "the v4 receipt whose SILENT SKIP under a pre-v4 reader "
                        "is why gate.py and gateimport.py teach v4 one commit "
                        "before anything mints it — cited at both sites and in "
                        "test_gate_import's HostBoundVersionTest",
    "80f95d7e": "codex r5 gate receipt id (dispatch-rebind-wired matrix)",
    "3caeb50c": "gate receipt token carried by f5436c21 (chain-folding lane)",
    # handoff.py cites its own review rounds as "codex, gate <id>" — six
    # receipts, two abbreviated in prose.
    "0b18e133f272a1e3": "codex review-round gate receipt (handoff-shelf lane)",
    "c8fd53acb0cc435f": "codex review-round gate receipt (handoff-shelf lane)",
    "dcf603c8b0bef585": "codex review-round gate receipt (handoff-shelf lane)",
    "dcf603c8": "dcf603c8b0bef585 abbreviated in a three-gate prose list",
    "d56a646e": "codex review-round gate receipt d56a646e73fa17cd, abbreviated",
    "4ef1aeee": "codex review-round gate receipt 4ef1aeee612d3612, abbreviated",
    "73b7f2cecb441f8f": "codex review-round gate receipt (handoff-shelf lane)",
}

# Non-git tokens NO ledger can vouch for, each with the reason it is exempt.
# Keyed by the exact token as it appears in prose. This list once held 40
# entries; the 29 that were ledger rows now live in LEDGER_CITED above, where
# the audit can refuse them. What remains is what has no authority anywhere:
# harness-store identifiers and shaguard's evidence. An entry that stops
# matching any scanned token is a stale exemption and fails the suite, so the
# list cannot accrete.
SKIP = {
    # harness/runtime identifiers — never git objects, never helm ledger rows.
    "f0ad7476": "claude session id (console-design row-eviction incident)",
    "56a628d4": "claude session id (compacted-pane incident)",
    "8d2e1ff0": "claude history sid (kimi history-as-address)",
    "58e6f94a": "claude session id (kimi's current sid, same passage)",
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
    # vcs.landed_state exhibits — the owner's 2026-08-03 measurement that
    # ancestry answers the wrong question. The pair must stay verbatim for the
    # same reason as the shaguard rows: they are the evidence, not a citation.
    # 5905f65's UNREACHABILITY IS THE FINDING, and it resolves today only
    # because its lane branch still exists — which the reaper that docstring
    # describes is built to retire. Without this row a future suite would read
    # the fix WORKING as a dead citation. (ba5e740, the landed twin, is on
    # trunk permanently and needs no exemption.)
    "5905f65": "landed_state exhibit: the lane tip whose patch landed as "
               "ba5e740 under a different sha",
    "1f7bacc86ebe5849": "a git PATCH-ID, not a commit — the identity `git "
                        "patch-id --stable` prints for BOTH 5905f65 and ba5e740",
}


def _git(repo, *args):
    return subprocess.run(("git", "-C", repo) + args,
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


def staged_tokens(repo):
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
    blanket-skip reflex and stops guarding anything. Measured 2026-08-04: a
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
    p = _git(repo, "diff", "--cached", "--unified=0", "--no-color")
    if p.returncode != 0:
        return None
    out, path = [], "?"
    for line in p.stdout.splitlines():
        if line.startswith("+++ b/"):
            path = line[6:]
        elif line.startswith("+") and not line.startswith("+++"):
            # Everything outside POLICED is exempt because the registry can
            # only vouch for helm/ prose — measured twice on 2026-08-04, from
            # both ends: a tests/ gate token had NO legal cure (register ->
            # staleness audit red; don't -> commit blocked), and a fixture's
            # orca-handle hex (tests/fixtures/codex3-100pct-tail.txt) is
            # runtime identity, never a claim about history. Scope == the
            # tree the registry audits, or the rung eats its own exemptions.
            if (path == SELF or not path.startswith(POLICED)
                    or _web_ui_source(path)):
                continue
            for tok in HEX.findall(line):
                out.append((path, tok))
    return out


def remote_reachable(repo, tok):
    """True iff a FRESH CLONE could reach it — a REMOTE ref contains it.

    `git branch -r --contains` and NOT `-a`: `-a` counts local branches, so it
    vouches for objects only this machine has. That distinction cost two gate
    rounds on 2026-08-04 — a reviewed tip whose `cat-file -e` succeeded and
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


def committing_registry(repo):
    """Registry keys as the tree BEING COMMITTED spells them; () if unreadable.

    unaccounted() resolves LEDGER_CITED from the module that is RUNNING, and
    the running module is the SNAPSHOT under .git/hooks/.helm-scanners/ —
    shared by every worktree, deliberately, so no lane's edit can rewrite
    every other room's guard.

    THE CONSEQUENCE NOBODY HAD MEASURED: registering a citation is the one
    edit a LANE legitimately has to make, and its entry is invisible to the
    rung judging its own commit. `helm work install-guard --apply`, which this
    rung's own refusal names as the second step, re-takes the snapshot from
    the SHARED CHECKOUT — so the documented cure cannot reach a lane either.
    The only move left is HELM_DOCREF_SKIP=1, which is exactly how a guard
    earns a blanket-skip reflex and stops guarding anything.

    That is the SELF exemption's shape one level up. SELF lets a lane WRITE
    the entry; nothing let the running rung READ it. Measured 2026-08-05: a
    lane registered a dispatch id, staged it, and this rung refused the very
    commit that registered it — the third documented instance of this module
    blocking its own cure, and the first that survived the fix for the
    previous two.

    LOGIC STAYS SNAPSHOT-OWNED; ONLY DATA COMES FROM THE TREE. Every decision
    is still the snapshot's code — this widens the set of ACCOUNTED tokens and
    can never narrow it, so a lane can unblock its own citation and cannot
    make the rung refuse anything it would otherwise pass.

    AND THE KEYS ARE READ WITH ast, NEVER import OR exec. A guard that
    executed the tree it is judging would hand any lane a shell inside the
    rung — a far worse defect than the one this repairs. literal_eval on the
    assignment node reads data and cannot call anything.

    An unreadable or unparseable index copy yields () and SAYS SO: the rung
    then judges by the snapshot alone, which is the pre-existing behaviour, so
    a malformed tree loses the widening rather than the guard."""
    keys = _registry_at(repo, ":")
    if keys is None:
        print("[helm docref] note: staged %s does not parse — judging by the "
              "installed snapshot alone" % SELF, file=sys.stderr)
        return ()
    return keys


def _registry_at(repo, ref):
    """Registry keys at a git ref; () if absent, None if it DOES NOT PARSE.

    The two are different failures and the caller must be able to say which:
    an absent ref is a tree with nothing to offer, an unparseable one is a
    tree that tried to say something and failed. Collapsing them cost the
    note that names the reason — caught by this module's own test one edit
    after the refactor that introduced it."""
    p = _git(repo, "show", ref + SELF)
    if p.returncode != 0:
        return ()
    keys = []
    try:
        tree = ast.parse(p.stdout)
    except SyntaxError:
        return None
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not ({"LEDGER_CITED", "SKIP"}
                & {t.id for t in node.targets if isinstance(t, ast.Name)}):
            continue
        try:
            keys.extend(ast.literal_eval(node.value))
        except (ValueError, TypeError):
            continue
    return tuple(k for k in keys if isinstance(k, str))


def added_registry_keys(repo):
    """Keys this commit ADDS to the registry — the only ones needing authority.

    Existing entries were vouched for when they were added and are not
    re-litigated on every unrelated commit: auditing all ~200 on each commit
    would be slow, and one entry going stale for its own reasons would block
    work that has nothing to do with it."""
    now, before = _registry_at(repo, ":"), _registry_at(repo, "HEAD:")
    # An unparseable tree ADDS NOTHING KNOWABLE, so it adds nothing to audit.
    return tuple(sorted(set(now or ()) - set(before or ())))


def citation_vouched(tok, rows, receipts):
    """True iff one exact id, or one prefix across BOTH ledger namespaces,
    names the citation. Per-ledger uniqueness is insufficient: a prefix that
    names one dispatch and one receipt still names two things.

    LIVES HERE, BESIDE THE REGISTRY IT POLICES, for the same reason the
    registry itself moved here — the pre-commit rung cannot import from
    tests/, and a second copy of this predicate would drift from the first
    invisibly. tests/test_docstring_refs.py imports it from this module."""
    t = tok.strip().lower()
    exact = sum(t in snap for snap in (rows, receipts))
    if exact:
        return exact == 1
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
    whole point of this function's existence (@codex F2 on dispatch
    aaf78be36525). THE CITATION IS A DISPATCH ID, NOT A SHA, and that is the
    lesson rather than a formatting choice: this docstring first cited the
    reviewed TIP, a rebase orphaned it, and the suite's own dead-citation arm
    caught it on the very lane that exists to police citations. A lane commit
    is rebasable by construction, so citing one only re-arms the trap; a
    ledger row id is stable under every rebase this lane will ever take. My
    first
    cure shelled out to <repo>/tests/test_docstring_refs.py — the judged tree's
    own script, and not even its STAGED copy. codex's repro replaced only the
    unstaged working-tree file with one that wrote a marker: guard rc=0, marker
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
    """-> (unvouched, why_undecidable). Authority for NEW registry keys.

    TAKES KEYS AS DATA. The evaluator is this module — snapshot-owned — and
    the judged tree contributes nothing but the staged key strings.

    IT FAILS CLOSED, and I had this backwards. I argued fail-open because
    refusing on an unreadable ledger would "block every commit on any machine
    without the live home" — but this rung only fires when a commit ADDS a
    registry key, so fail-closed blocks registry-changing commits and nothing
    else. That is a handful, and it is exactly what this module already
    promises: a SKIP entry fails OPEN (it silences a token forever, and went
    stale twice in one hour), while a LEDGER_CITED entry fails CLOSED AT
    AUTHORING TIME. Fail-open would have made the new rung contradict the
    sentence the registry was designed around. Caught by @codex; my error was
    measuring the cost against ALL commits instead of the ones this can reach.

    The escape hatch stays HELM_DOCREF_SKIP=1, which is loud, deliberate, and
    already how every other arm of this rung is bypassed."""
    if not keys:
        return (), None
    rows, receipts, why = _live_ledgers() if ledgers is None else ledgers
    if why:
        return tuple(keys), why
    return tuple(k for k in keys
                 if not citation_vouched(k, rows, receipts)), None


def unaccounted(repo, tokens):
    """[(path, token)] this repository cannot account for.

    The accounted set is the snapshot's registry UNION the committing tree's —
    see committing_registry for why the union, and why it can only widen."""
    accounted = set(LEDGER_CITED) | set(SKIP) | set(committing_registry(repo))
    return [(path, tok) for path, tok in tokens
            if tok not in accounted and not remote_reachable(repo, tok)]


def main(argv):
    repo = os.environ.get("HELM_DOCREF_REPO") or os.getcwd()
    if "--staged" not in argv:
        print("usage: docref_guard.py --staged", file=sys.stderr)
        return 2
    tokens = staged_tokens(repo)
    if tokens is None:
        # UNREADABLE INDEX IS NOT A CLEAN COMMIT, but it is also not this
        # rung's business to block on: warn and let the gate be the backstop.
        print("[helm docref] WARNING: could not read the staged diff — "
              "citation check SKIPPED, the gate remains the backstop",
              file=sys.stderr)
        return 0
    # AUTHORITY FOR NEW REGISTRY KEYS, BEFORE the accountability check that
    # those very keys would answer. Registering a token is what MAKES it
    # accounted, so if the registration itself is unvouched the accountability
    # pass below is circular — it would report success because of the line
    # this rung is here to judge.
    added = added_registry_keys(repo)
    unvouched, why = vouch_added_keys(added)
    if unvouched:
        print("[helm docref] REFUSED: %d new registry key(s) no live ledger "
              "vouches for:" % len(unvouched), file=sys.stderr)
        for tok in unvouched:
            print("    %s" % tok, file=sys.stderr)
        if why:
            print("  THE LEDGERS COULD NOT BE READ (%s), and this arm fails "
                  "CLOSED — it fires only on a commit that ADDS a registry "
                  "key, so it blocks those and nothing else. A LEDGER_CITED "
                  "entry is meant to fail closed at authoring time; passing "
                  "an unverifiable one would be the opposite." % why,
                  file=sys.stderr)
        print("  A registry entry is a claim that some ledger row, receipt or "
              "patch-id EXISTS. Registering a token you cannot point at "
              "silences it forever, including the day it becomes a genuinely "
              "dead reference. Check the id, or cite the commit instead. "
              "Skip once: HELM_DOCREF_SKIP=1", file=sys.stderr)
        return 1

    bad = unaccounted(repo, tokens)
    if not bad:
        return 0
    print("[helm docref] REFUSED: %d cited token(s) this repo cannot account "
          "for — a fresh clone (and the fab) reads them as dead:"
          % len(bad), file=sys.stderr)
    for path, tok in bad:
        print("    %s  %s" % (tok, path), file=sys.stderr)
    # THE CURE MUST NAME ITS OWN SECOND STEP, and until 2026-08-05 it named a
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
    # Measured 2026-08-04: I registered a row id, the suite went green, and
    # this rung refused the very commit that fixed it. That is the same
    # blocks-its-own-cure shape as the registry self-exemption, in a second
    # form, and a FIX line that omits the reinstall is what made it one.
    print("  FIX: cite a commit reachable from a REMOTE ref (a local branch is "
          "invisible to every other clone), or register it in LEDGER_CITED / "
          "SKIP in helm/docref_guard.py when it is a ledger row, receipt or "
          "patch-id rather than a commit — AND STAGE THAT EDIT, which is the "
          "whole second step: the registry is read from the tree being "
          "committed, so an unstaged entry is invisible to this rung. "
          "Skip once: HELM_DOCREF_SKIP=1", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
