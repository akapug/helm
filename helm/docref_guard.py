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
shape. A commit sha is a Git object (e.g. 99fd8406, this repository's root
commit — the one citation every clone can resolve), LEDGER_CITED names one live row or receipt,
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

# Load-bearing CONTENT identities. Each key is the exact 40-hex value from
# `git patch-id --stable`; each value is (base tree, tip tree, reason), with
# full object ids so staged data cannot smuggle git options into the snapshot
# evaluator. Admission recomputes the key from `git diff base tip` — cat-file
# can never vouch for a patch-id because it is not a Git object.
PATCH_IDS = {
    # MUST-HIT content identity 51c087e2d51143dbf08015a8f34598f31a146879.
    # It is prose here deliberately: the installed old snapshot exempts SELF,
    # so the category's exemplar stays guardable without a blanket skip. The
    # endpoints are this repository's first commit pair, so every clone can
    # recompute the identity — the property the whole category rests on.
    "51c087e2d51143dbf08015a8f34598f31a146879": (
        "99fd84067ebe033723ad940ef75136634ffe14f9",
        "cc84a09cda1036d9e74d598a076d31bbf631d594",
        "content identity of the repository's root..second-commit diff — the "
        "category's in-repo exemplar, recomputable from any clone",
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
    "1785568214": "a MELD EPOCH (codex meld e:1785568214) — a chat-room "
                  "coordination id, never a commit",
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
    # 5905f65's UNREACHABILITY IS THE FINDING — the exhibit is a lane tip
    # whose patch landed under a DIFFERENT sha, so ancestry can never see it.
    # Without this row a future suite would read the fix WORKING as a dead
    # citation.
    "5905f65": "landed_state exhibit: the lane tip whose patch landed under "
               "a different sha",
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
                # PURE-DECIMAL IS A NUMBER, NOT A CITATION — the suite's scan
                # (`checked = {... if not t.isdigit()}`) already says so, and
                # a rung stricter than the suite on a NUMERIC LITERAL is not
                # stricter, it is wrong: `1000000` in a comment about a size
                # cap names no object in any ledger. Same rule, both layers.
                if tok.isdigit():
                    continue
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


REGISTRY_NAMES = ("LEDGER_CITED", "SKIP", "PATCH_IDS")


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
    before = _registries_at(repo, "HEAD:")
    if now is None or before is None:
        return None
    return {name: {k: v for k, v in now[name].items()
                   if k not in before[name] or before[name][k] != v}
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
    tokens = staged_tokens(repo)
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
          "invisible to every other clone), or register it in LEDGER_CITED, "
          "PATCH_IDS, or SKIP in helm/docref_guard.py according to its actual "
          "authority — AND STAGE THAT EDIT, which is the "
          "whole second step: the registry is read from the tree being "
          "committed, so an unstaged entry is invisible to this rung. "
          "Skip once: HELM_DOCREF_SKIP=1", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
