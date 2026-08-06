#!/usr/bin/env python3
"""Import one remotely-minted gate receipt into the binding ledger — strictly.

WHY THIS VERB EXISTS. A receipt minted on another machine is a fact about a
tree, an interpreter and a suite — none of which stop being true in transit.
What transit CAN do is tamper, truncate, duplicate and orphan, and on
2026-08-03 it did something worse: seven remotely-minted rows reached the
binding ledger through a one-off hand append inside one seat's bind window
(#167). The rows were genuine; the PATH was undocumented, unrepeatable and
indistinguishable from forgery. This verb sanctions that movement by making
it verifiable, idempotent and auditable — and by existing, it makes the hand
append the wrong tool forever.

WHAT IT REFUSES TO BE. Not a second minting path, and not a weakening of the
verdict guard (bind-provenance-dont-weaken-the-guard): `verdict --approve`
still reads only the global ledger, learns nothing about artifact files, and
this module never widens what it accepts. The import VALIDATES, then appends
the row VERBATIM — the receipt's content id is recomputed here and must
match, so an edited status, a pasted base-check or a doctored failure list
stops resolving exactly as it would have on the minting machine.

HOW A ROW QUALIFIES, in refusal order — every gate names itself:
  1. the artifact parses and the target row is unambiguous (one row, or --id);
  2. the receipt version is one this helm KNOWS (imports are strict: an
     unknown future schema is REFUSED, never half-understood);
  3. the recomputed content id equals the stored id (tamper evidence);
  4. the cited head resolves in THIS repo and its tree matches the receipt's
     tree exactly — dirty-run receipts carry the head that was actually
     dispatched, so a receipt about a tree this repo cannot produce refuses;
  5. the id is new, or the stored row is byte-identical (idempotent: same
     import twice is a no-op; same id with DIFFERENT content is a conflict
     and refuses loudly).
Only then does a durable provenance event (artifact path, repo, time, and actor
when the identity law can prove one) append to gate-imports.jsonl BEFORE the
receipt. Two files cannot append atomically, so order decides which orphan a
crash may leave: provenance-only is
a falsifiable attempted import; receipt-only is indistinguishable from the hand
append this verb exists to forbid. No origin host/run is inferred or accepted —
the artifact does not carry those facts. The provenance ledger is read by humans
and audits, never by bind: distinguishable must not mean second-class.
"""
import json
import os
import subprocess
import sys

from . import eventledger, gate, home, pk, seats

IMPORTS = "gate-imports.jsonl"

# Receipt versions whose binding grammar this module fully understands.
# v4 (host-bound receipts) is now among them. The earlier refusal was written
# when `gate` itself minted only v3 and could not recompute an id over the host
# block — accepting it THEN would have validated nothing. That is no longer the
# shape: `_receipt_id` below is gate's OWN function, so the commit that taught
# gate `host-v4` taught this reader in the same breath, and this file ships in
# that same commit for exactly that reason.
#
# READER FIRST, WRITER LATER, and the gap between them was the point. A helm
# that minted v4 before every helm could read it would produce receipts whose id
# recomputes differently here and in `gate.receipts()` — refused loudly at
# import, and SILENTLY SKIPPED at bind.
#
# THAT GAP IS CLOSED: `gate._mint_result` stamps `"v": 4` with a `host` block and
# the ledger carries live v4 receipts beside v1-v3. Every version listed here
# binds, and a receipt's VERSION IS NOT AN AUTHORIZATION AXIS — `landreq
# .gate_requirement` is explicit that authorization is a property of the WRITER,
# "never of the clock and never of a version int". Reading an older receipt as
# second-class is how a verified round gets filed SUPERSEDE instead of APPROVE
# and rots into contrary debt on the land board (measured 2026-08-04: three rows,
# each prescribing a whole-suite re-gate to earn a "v4 APPROVE" that never existed
# — while 182 APPROVEs already stood on v3). Hence the docstring's law above:
# distinguishable must not mean second-class.
KNOWN_VERSIONS = (1, 2, 3, 4)

# Every key EVERY minting writes (gate._mint_result). Strict means STRICT:
# a row missing reader-relied keys, or carrying keys no minting writes, did
# not come from a gate and does not enter the ledger.
REQUIRED_KEYS = frozenset((
    "v", "event", "ts", "repo_id", "head", "tree", "dirty", "interpreter",
    "argv", "suite", "label", "rc", "wall", "status", "ran", "skipped",
    "detail", "elapsed", "failures", "failures_unreadable", "id"))
ALLOWED_KEYS = REQUIRED_KEYS | frozenset((
    "head_after", "tree_after", "dirty_after", "base_check"))

# Keys a SPECIFIC version both requires and permits. `host` is not optional
# decoration at v4: it is bound into the receipt id, so a v4 row without one
# recomputes to a different id, and a v1-3 row carrying one describes a minting
# that never happened. Required-at-4 and foreign-below-4 are the same fact
# stated from the two sides a reader can be wrong from.
VERSION_KEYS = {4: frozenset(("host",))}


def imports_path():
    return os.path.join(home.global_dir(), IMPORTS)


def _canonical(row):
    """One byte shape per content, for the idempotency comparison."""
    return json.dumps(row, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def _load_rows(path):
    """-> (rows, err). Every line must parse; a torn artifact refuses whole.

    An artifact file is small (one run's ledger) and it is EVIDENCE — a file
    where line 3 is garbage does not get to contribute lines 1 and 2."""
    rows = []
    try:
        with open(path, encoding="utf-8") as f:
            for n, line in enumerate(f, 1):
                if not line.strip():
                    continue
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    return None, "line %d of %s is not JSON" % (n, path)
    except OSError as exc:
        return None, "cannot read %s: %s" % (path, exc)
    if not rows:
        return None, "%s holds no receipt rows" % path
    return rows, None


def _pick(rows, want_id):
    if want_id:
        hits = [r for r in rows if isinstance(r, dict)
                and r.get("id") == want_id]
        if not hits:
            return None, "no row with id %s in the artifact" % want_id
        if len(hits) > 1:
            return None, ("id %s appears %d times in the artifact — a file "
                          "that repeats an id is not evidence"
                          % (want_id, len(hits)))
        return hits[0], None
    if len(rows) > 1:
        return None, ("the artifact holds %d rows — name one with "
                      "--id <receipt-id>" % len(rows))
    return rows[0], None


def _schema_err(row):
    if not isinstance(row, dict):
        return "receipt row is not an object"
    if row.get("event") != "gate":
        return "event is %r, not 'gate'" % (row.get("event"),)
    if row.get("v") not in KNOWN_VERSIONS:
        return ("receipt version %r is not one this helm understands "
                "(knows %s) — REFUSED rather than half-validated; update "
                "helm before importing it" % (row.get("v"),
                                              list(KNOWN_VERSIONS)))
    versioned = VERSION_KEYS.get(row.get("v"), frozenset())
    missing = sorted((REQUIRED_KEYS | versioned) - set(row))
    if missing:
        return "receipt is missing minted keys: %s" % ", ".join(missing)
    foreign = sorted(set(row) - (ALLOWED_KEYS | versioned))
    if foreign:
        return "receipt carries keys no gate mints: %s" % ", ".join(foreign)
    return None


def _tree_of(repo, head):
    p = subprocess.run(["git", "-C", repo, "rev-parse", head + "^{tree}"],
                       capture_output=True, text=True, timeout=30)
    if p.returncode != 0:
        return None
    return p.stdout.strip()


def _existing(row_id):
    """The stored row with this id, or None. Reads the ledger the same way
    every other consumer does: whole file, last word wins is not a thing —
    ids are content handles, so first hit is the only hit that matters."""
    try:
        with open(gate.receipts_path(), encoding="utf-8") as f:
            for line in f:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict) and row.get("id") == row_id:
                    return row
    except OSError:
        pass
    return None


def import_receipt(artifact, repo, want_id=None, actor=None):
    """-> (row, verdict, err). verdict in ('imported', 'duplicate', None)."""
    rows, err = _load_rows(artifact)
    if err:
        return None, None, err
    row, err = _pick(rows, want_id)
    if err:
        return None, None, err
    err = _schema_err(row)
    if err:
        return None, None, err
    try:
        recomputed = gate._receipt_id(row)
    except ValueError as exc:
        return None, None, "receipt id is not computable: %s" % exc
    if recomputed != row.get("id"):
        return None, None, ("content id mismatch: stored %s, recomputed %s — "
                            "the row was edited after minting and no longer "
                            "resolves" % (row.get("id"), recomputed))
    head, tree = row.get("head"), row.get("tree")
    local_tree = _tree_of(repo, str(head))
    if local_tree is None:
        return None, None, ("cited head %s does not resolve in %s — this "
                            "repo cannot produce the tree the receipt is "
                            "about" % (head, repo))
    if local_tree != tree:
        # No dirty exception: a dirty-run receipt's tree matches no commit's
        # tree by construction, and bind refuses dirty receipts anyway — a row
        # this gate blocks is a row that had no binding value to lose.
        return None, None, ("tree mismatch: %s^{tree} is %s here, receipt "
                            "says %s" % (head, local_tree, tree))
    stored = _existing(row["id"])
    if stored is not None:
        if _canonical(stored) == _canonical(row):
            return row, "duplicate", None
        return None, None, ("id %s already in the ledger with DIFFERENT "
                            "content — refusing the conflict; neither row "
                            "is trusted to overwrite the other" % row["id"])
    # Origin is READ, never typed and never parsed-into-existence: origin_repo
    # comes from the receipt's own repo_id — no inference possible. origin_run
    # stays ABSENT by the same rumour rule that keeps host out (the landed cut
    # was right and my first fix here proved it wrong-way: a scratch artifact's
    # tmp dirname read back as a "run"). The run id is
    # recoverable from the verbatim artifact path without minting a semantic claim.
    provenance = {"event": "gate-import", "ts": pk.now_ts(),
                  "receipt": row["id"], "artifact": os.path.abspath(artifact),
                  "origin_repo": row.get("repo_id"), "repo": repo}
    actor = actor or _self_actor()
    if actor:
        provenance["actor"] = actor
    if not eventledger.append(imports_path(), provenance):
        return None, None, "provenance append failed (lock or io); receipt untouched"
    if not eventledger.append(gate.receipts_path(), row):
        # The only survivable half-state: an audit row that says an import was
        # attempted but did not complete. It cannot bind and is harmlessly
        # falsifiable; the opposite orphan is indistinguishable from hand append.
        return None, None, ("provenance appended but receipt append FAILED — "
                            "receipt remains unbound; retry the import")
    return row, "imported", None


def _self_actor():
    """The SEAT, resolved by the identity law (seats.acting_seat: declared
    name, then the session->row map, then the auto-name floor) — never the OS
    user as the value. The first live imports recorded actor=<user>@<host> for
    every seat on the box because own_name() reads only the env and importer
    shells declare none: the git-metadata-never-proves-who-wrote-it shape in
    a brand-new field. A provenance field that cannot distinguish actors is a
    hostname with extra steps (helm-claude, 2026-08-04). An unanswered law
    leaves actor ABSENT rather than replacing identity with process location."""
    try:
        return seats.acting_seat() or None
    except Exception:
        return None


IMPORT_USAGE = "gate import <artifact.jsonl> [--repo PATH] [--id RECEIPT-ID]"


def cmd_import(rest):
    rest = list(rest or ())
    def opt(name):
        if name in rest:
            i = rest.index(name)
            if i + 1 >= len(rest):
                print("gate import: %s needs a value" % name, file=sys.stderr)
                return None, True
            v = rest[i + 1]
            del rest[i:i + 2]
            return v, False
        return None, False
    repo, bad_p = opt("--repo")
    want, bad_i = opt("--id")
    if bad_p or bad_i:
        return 2
    artifact = rest.pop(0) if rest and not rest[0].startswith("-") else None
    from .cli import guard_tail
    rc = guard_tail("helm gate import", rest, usage=IMPORT_USAGE)
    if rc is not None:
        return rc
    if artifact is None:
        print("usage: helm " + IMPORT_USAGE, file=sys.stderr)
        return 2
    repo = os.path.abspath(repo or os.getcwd())
    row, verdict, err = import_receipt(artifact, repo, want_id=want)
    if err and verdict is None:
        print("gate import: REFUSED — %s" % err, file=sys.stderr)
        return 2
    if verdict == "duplicate":
        print("gate import: %s already in the ledger, content identical — "
              "no-op" % row["id"])
        return 0
    line = ("gate import: %s appended — provenance in %s"
            % (row["id"], imports_path()))
    print(line)
    if err:
        print("gate import: WARNING — %s" % err, file=sys.stderr)
        return 1
    return 0
