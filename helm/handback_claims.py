#!/usr/bin/env python3
"""helm handback_claims — the hand-back claim check (task/3540).

A builder hands work back with a review row (`dispatch send ... --kind
review`), and its brief carries claims: "fab: Ran 52 tests in 1.102s OK",
"gate:<id> whole-suite OK Ran 26377", "LOG: host:~/fab/logs/<job>.log",
"tip <sha>". Until this check they were prose, and a reader could not tell a
truthful hand-back from the LAUNDERED-GATE case: a green receipt minted on
ANOTHER lane's tree, cited as this lane's proof.

EVERY CLAIM BINDS TO A GATE RECEIPT FOR THE ROW'S OWN TREE, or reads UNBOUND
with its reason. The row's own tree is the tree of the tip the row carries: a
receipt binds when its head IS that tip or its tree IS that tree (a fab
snapshot or a gate-then-commit run wears another head over identical bytes),
it records the count the brief claims, and the land path's own row checks
admit it: `gate.row_refusal` (clean before and after, the worktree held
still, OK only over exit 0, the frozen serial argv for a whole suite), then
status OK, an interpreter helm chose (a `helm gate run -- <cmd>` receipt
records whatever its command printed, so an echoed "Ran N" proves nothing)
and a named host, which is the order `gate.bind` asks them in. A whole-suite
claim ("whole suite" or "full suite", in its clause or on a heading line just
above) binds only a whole-suite KIND (`gate.stored_whole_suite`), and every
CHECKED reason names the kind it bound. A receipt from any other tree is
refused and named, with the head and room it ran in.

THE RULE FOR TESTIMONY, stated because it is the case a reader most wants
to read green: a claim that names no receipt is CHECKED only when a receipt
on this tree binds it anyway. A bare "Ran N" line binds to an OK receipt
minted on this tree that records exactly N, and names it; with none it is
UNBOUND. A claim whose own text says FAILED (in its clause, or on the line
that follows a Ran line with no OK of its own) is never CHECKED, whatever
receipt that tree holds. A LOG path is never CHECKED: no receipt records a
log, and a log this helm may not even be able to read is testimony, not
proof. A tip claim binds to the row's own tip.

A READING, NEVER A DOOR. The lines print on `helm dispatch triage <id>` and
`helm lr show <id>`; nothing here refuses a send, mints a verdict or spends a
receipt, and `gate.bind` stays the only binding a verdict spends. That is why
the row checks are `gate.row_refusal` and the clauses beside it rather than
`gate.bind` itself: bind is the spending door (repository authority, the
consuming checkout re-stamped fresh), and a reading asks only whether the row
is admissible. The count comparison is exact, the rule the focused bind
holds its id count to.

Import-safe, stdlib-only.
"""
import re

from . import gate

CHECKED = "CHECKED"
UNBOUND = "UNBOUND"

_GATE = re.compile(r"gate:([0-9a-f]{4,32})(?![0-9a-f])")
# "Ran 1,234 tests" claims 1234: a comma inside the digits is grouping.
_RAN = re.compile(r"\bRan (\d+(?:,\d+)*)\b")
_LOG = re.compile(r"\bLOG:\s*(\S+)")
# `--patch-tip <sha>` names a reviewer's cure, not the hand-back's tip.
_TIP = re.compile(r"(?<![\w-])tip\b[\s:=]+([0-9a-fA-F]{7,40})(?![0-9a-fA-F])",
                  re.I)
_WHOLE = re.compile(r"(?:whole|full)[- ]suite", re.I)
_FAILED = re.compile(r"\bFAILED\b")
_OK = re.compile(r"\bOK\b")
_CLAUSE = re.compile(r"[\n;]")


def parse(text):
    """The claims in a brief, in order. -> [{kind, ...}].

    A clause is a line or a `;`-separated part of one. A Ran count in a
    clause with exactly ONE gate token is that token's claim; anywhere else
    it is a claim of its own. A clause says a whole suite when it names one
    or when the clause above is a heading that does ("Whole suite:"), and it
    says FAILED when its own text does or, for a Ran clause with no OK of its
    own, when the next non-empty clause opens FAILED (unittest's own shape)."""
    clauses = _CLAUSE.split(str(text or ""))
    claims = []
    heading = False
    for at, clause in enumerate(clauses):
        tokens = _GATE.findall(clause)
        counts = [int(n.replace(",", "")) for n in _RAN.findall(clause)]
        whole = heading or bool(_WHOLE.search(clause))
        failed = bool(_FAILED.search(clause))
        if counts and not failed and not _OK.search(clause):
            after = next((c for c in clauses[at + 1:] if c.strip()), "")
            failed = after.lstrip().startswith("FAILED")
        for token in tokens:
            claims.append({"kind": "gate", "receipt": token, "whole": whole,
                           "failed": failed,
                           "ran": counts[0] if len(tokens) == 1 and counts
                           else None})
        if len(tokens) != 1:
            claims.extend({"kind": "ran", "ran": n, "whole": whole,
                           "failed": failed} for n in counts)
        claims.extend({"kind": "log", "log": path}
                      for path in _LOG.findall(clause))
        claims.extend({"kind": "tip", "tip": sha.lower()}
                      for sha in _TIP.findall(clause))
        if clause.strip():
            heading = bool(_WHOLE.search(clause)) and not tokens \
                and not counts and clause.rstrip().endswith(":")
    return claims


def check(text, tip, repo=None):
    """Each claim in `text`, bound against the tree of `tip` in `repo`.
    -> [claim + {state, why}]. Total: an unreadable receipt ledger or
    repository makes a claim UNBOUND, never raises."""
    tip = str(tip or "").strip().lower()
    tree = gate._tree_at(repo, tip) if repo and tip else ""
    out = []
    for claim in parse(text):
        state, why = _bind(claim, tip, tree, repo)
        out.append(dict(claim, state=state, why=why))
    return out


def label(claim):
    """How a line names the claim it judges."""
    kind = claim["kind"]
    if kind == "gate":
        return "gate:" + claim["receipt"]
    if kind == "ran":
        return "Ran %d" % claim["ran"]
    if kind == "log":
        return "LOG " + claim["log"]
    return "tip " + claim["tip"][:12]


def lines(row):
    """The lines a reader of a REVIEW row sees about its brief's claims, or
    [] for any other row and for a brief that claims nothing. A build brief is
    the sender's instruction, not a hand-back: its numbers are not claims
    about the row's tree."""
    if not isinstance(row, dict) or row.get("kind") != "review":
        return []
    # A READING MUST NEVER BREAK THE VERB THAT ASKED: a failure is one line
    # saying so, and the rest of the row prints as it did.
    try:
        from . import dispatches
        brief, _absent, _problem = dispatches.brief_of(row)
        claims = check(brief, row.get("tip"),
                       repo=row.get("repo_root") or row.get("repo_id"))
    except Exception as exc:                          # noqa: BLE001
        return ["claims: the hand-back's claims could not be checked (%s)"
                % type(exc).__name__]
    return ["claim %s %s — %s" % (c["state"], label(c), c["why"])
            for c in claims]


def _on_tree(receipt, tip, tree):
    head = str(receipt.get("head") or "").lower()
    return bool(tip) and head == tip \
        or bool(tree) and str(receipt.get("tree") or "") == tree


def _elsewhere(receipt, tip, tree):
    """The laundered-gate refusal: where the receipt WAS minted."""
    room = gate.receipt_room(receipt)
    return ("receipt %s was minted on %s (tree %s%s), not this row's tip %s "
            "(tree %s) — a receipt from another tree is not this lane's proof"
            % (receipt["id"], str(receipt.get("head") or "?")[:12],
               str(receipt.get("tree") or "?")[:12],
               ", room " + room if room else "", tip[:12] or "?",
               tree[:12] or "UNREADABLE"))


def kind(receipt):
    """The receipt's KIND, as a CHECKED reason names it, and whether that
    kind is a whole suite. -> (name, whole)."""
    v = receipt.get("v")
    if not gate._ident_of(receipt).get("name"):
        return "custom command v%s" % v, False
    if v == gate.SLICE_VERSION:
        return "sliced v%d" % v, gate.stored_whole_suite(receipt)
    if v == gate.FOCUSED_VERSION or receipt.get("suite") is not True:
        return "focused v%s" % v, False
    if gate.stored_whole_suite(receipt):
        return "serial v%s" % v, True
    if receipt.get("suite_command") is not None:
        # `row_refusal` established the declaration's origin before this is
        # asked: the project's own declared command, whole.
        return "declared-command v%s" % v, True
    return "suite-flagged v%s (not the serial discovery argv)" % v, False


def _refusal(receipt, tip, repo):
    """Why the land path would not admit this row, or None: `gate.row_refusal`
    and then bind's row clauses, in bind's order. Read-only, and total."""
    rid = receipt.get("id")
    try:
        refusal = gate.row_refusal(receipt, about=tip[:12],
                                   consuming_repo=repo or None)
    except Exception as exc:                          # noqa: BLE001
        return "receipt %s could not be judged (%s)" % (rid,
                                                         type(exc).__name__)
    if refusal:
        return refusal
    if receipt.get("status") != "OK":
        return "receipt %s is %s, not OK" % (rid, receipt.get("status"))
    if not gate._ident_of(receipt).get("name"):
        return ("receipt %s ran a custom command, so helm never chose its "
                "interpreter and its count is whatever the command printed"
                % rid)
    if not gate._host_of(receipt).get("node"):
        return "receipt %s does not name the HOST it ran on" % rid
    return None


def _bind(claim, tip, tree, repo=None):
    what = claim["kind"]
    if what == "tip":
        if tip and tip.startswith(claim["tip"]):
            return CHECKED, "the row's own tip"
        return UNBOUND, ("the brief names tip %s; the row carries %s"
                         % (claim["tip"][:12], tip[:12] or "no tip"))
    if what == "log":
        return UNBOUND, ("testimony: a log path is not a receipt, and no "
                         "gate receipt records it, so nothing binds it")
    if claim.get("failed"):
        return UNBOUND, ("the brief itself reports FAILED, so no receipt "
                         "vouches for it")
    if what == "ran":
        rows, unavailable, _skipped = gate.receipts()
        if unavailable:
            return UNBOUND, "receipt ledger unavailable: %s" % unavailable
        judged = [(r, _refusal(r, tip, repo))
                  for r in rows if _on_tree(r, tip, tree)]
        counted = [(r, why) for r, why in judged
                   if r.get("ran") == claim["ran"]]
        admitted = [r for r, why in counted if not why]
        hit = [r for r in admitted if kind(r)[1] or not claim["whole"]]
        if hit:
            return CHECKED, ("receipt %s (%s) records OK Ran %d on this tree"
                             % (hit[-1]["id"], kind(hit[-1])[0],
                                claim["ran"]))
        if admitted:
            return UNBOUND, ("the brief claims a whole suite; receipt %s "
                             "recording Ran %d on this tree is %s"
                             % (admitted[-1]["id"], claim["ran"],
                                kind(admitted[-1])[0]))
        if counted:
            return UNBOUND, ("testimony: it names no receipt, and the "
                             "receipt on this tree recording Ran %d is not "
                             "admitted: %s" % (claim["ran"], counted[-1][1]))
        others = sorted({r.get("ran") for r, why in judged
                         if type(r.get("ran")) is int and not why})
        return UNBOUND, ("testimony: it names no receipt, and no OK receipt "
                         "minted on this tree records Ran %d%s"
                         % (claim["ran"], " (this tree's receipts record Ran "
                            + ", ".join(str(n) for n in others) + ")"
                            if others else ""))
    receipt, err = gate.by_id(claim["receipt"])
    if err:
        return UNBOUND, err
    if not _on_tree(receipt, tip, tree):
        return UNBOUND, _elsewhere(receipt, tip, tree)
    refusal = _refusal(receipt, tip, repo)
    if refusal:
        return UNBOUND, refusal
    name, whole = kind(receipt)
    if claim["whole"] and not whole:
        return UNBOUND, ("receipt %s is %s; the brief claims a whole suite"
                         % (receipt["id"], name))
    ran = receipt.get("ran")
    if claim["ran"] is not None and claim["ran"] != ran:
        return UNBOUND, ("the brief claims Ran %d; receipt %s records Ran %s"
                         % (claim["ran"], receipt["id"], ran))
    return CHECKED, "receipt %s (%s): OK Ran %s on this tree" % (
        receipt["id"], name, ran)
