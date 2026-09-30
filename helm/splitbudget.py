"""The seats-split line budget, asked at COMMIT instead of from a red suite.

WHY THIS EXISTS AS A RUNG. tests/test_seats_split_contract.py already enforces
the budget, and its arm is well built -- a real ceiling, a must-hit control
refusing an empty scan, and a message naming the file and the number. What it
cannot do is speak EARLY: it lives inside a whole suite, so its cheapest
possible check -- a directory listing and a line count -- costs a fab round
trip to consult. The seat who pays that is never the one planning an
extraction; it is whoever added one line to a module they were not thinking
about, on a lane about something else, and the red arrives naming a budget
they have not heard of (helm task/2264, measured the hard way).

IT REFUSES ONLY WHAT THIS COMMIT MAKES WORSE, and that asymmetry is the whole
design. A module already over budget is a standing debt somebody owns;
refusing every commit in the repository until they pay it would make this rung
the thing seats route around. So:

    GREW past the budget in this commit   -> REFUSE, naming both numbers
    already over, untouched or shrinking  -> report, never refuse
    within the warning band               -> report the remaining headroom

The shrinking case is deliberate: a commit that takes a module from 1010 to
1004 is progress and must not be blocked for arriving mid-way.

THE CURRENCY IS THE STAGED BLOB, not the working tree, because the staged
blob is what becomes history and the two differ exactly when someone is
part-way through an edit.

THE CONSTANTS LIVE HERE AND THE SUITE IMPORTS THEM BACK, so there is one
budget with two readers rather than a copy that drifts -- the shape
docref_guard established for its citation registry.

THE REFUSAL LEADS WITH THE WAY OUT. A reader that stops after one line must
still learn the fix, so the REFUSED line itself says "split into a sibling
module", and numbered steps plus real past splits to copy follow before the
rule is restated. `way_out` builds that text and the whole-suite backstop
imports it, so the two readers cannot drift apart (task/3524: a seat that read
only the rule trimmed comments against it for hours and never split).

A REFUSAL LEAVES ONE RECORD: a JSON line in <git common dir>/helm/refusals.jsonl
with the guard, the room, its branch and each module's numbers, so a progress
check can see a seat walled by this rung without reading its transcript. The
common dir is shared by every worktree of the repository, so one file holds
every lane's refusals. The record is fail-open: a write that fails is named on
stderr and the refusal is unchanged.

IMPORTS NOTHING FROM helm. The installer snapshots these bytes beside the
pre-commit hook, where no helm package exists to import (see
helm/inflight_gate.py, which states the same law).
"""
import json
import os
import subprocess
import sys
import time

#: An EXTRACTED module over this defeats the split.
FINISH = 1000
#: The facade's own ratchet. It is supposed to DRAIN, never grow back.
CEILING = 1000
#: How close to the budget is worth saying out loud while there is still room.
WARN_BAND = 25

SKIP_ENV = "HELM_SPLIT_BUDGET_SKIP"
_PKG = "helm"

#: Past splits a seat can copy: (the new sibling, the module it moved out of,
#: the commit that did it). `git show <commit>` shows every moved line, the
#: import back into the old module, and the tests that followed the code.
#: tests/test_splitbudget.py refuses a row whose files are gone.
EXAMPLES = (
    ("helm/seats_stop_spiral.py", "helm/seats_stop_signals.py", "63c872c6d47"),
    ("helm/seats_stop_claims.py", "helm/seats_stop_guard.py", "124fb524bc9"),
    ("helm/seats_gc.py", "helm/seats_report.py", "c162d84e4cc"),
)

#: The refusal record: its guard token, its path under the git common dir, and
#: the size at which the live file rolls to `<file>.1` (one generation kept).
GUARD = "split-budget"
RECORD = ("helm", "refusals.jsonl")
RECORD_ROLL_BYTES = 256 * 1024


def is_budgeted(rel):
    """Does this repo-relative path carry the split budget?"""
    d, name = os.path.split(rel.replace(os.sep, "/").lstrip("./"))
    return d == _PKG and (name == "seats.py" or name.startswith("seats_")) \
        and name.endswith(".py")


def _git(root, *args):
    p = subprocess.run(("git",) + args, capture_output=True, cwd=root,
                       timeout=60)
    return p.returncode, p.stdout, p.stderr


def _lines(blob):
    return len(blob.splitlines())


def _blob(root, ref, rel):
    """Line count of `rel` at `ref`, or None when it is not there to read.

    NONE IS NOT ZERO. A path absent from a parent is NEW there, and a path git
    refuses to show is unreadable -- calling either one zero lines would make
    every new module read as maximal growth and refuse its own first commit.
    """
    rc, out, _ = _git(root, "show", "%s:%s" % (ref, rel))
    return _lines(out) if rc == 0 else None


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


def _parents(root):
    """This commit's parent committishes, from nevertrack.commit_parents — the
    ONE implementation of that question in the guard family, carrying the
    measurement of what a first-parent base does to a merge.
    nevertrack.py is snapshotted beside this rung under every guard profile; an
    absent neighbour is a broken installation and falls back to HEAD alone —
    this rung's OLD base, which over-blocks a merge rather than going quiet, and
    ONE LAW'S ABSENCE ISOLATES TO ITSELF in the composed hook.
    """
    nt = _sibling("nevertrack")
    if nt is None:
        return ["HEAD"]        # one law's absence isolates to itself
    return nt.commit_parents(root) or ["HEAD"]


def _staged_budgeted(root):
    """[(rel, staged_lines, [line counts at each parent])] for budgeted staged
    paths. EVERY PARENT, because GREW is a claim about what this commit did: a
    merge that brings a module's growth in from its other side did not grow it
    (the same first-parent base the never-track scanner was refusing merges
    over)."""
    rc, out, err = _git(root, "diff", "--cached", "--name-only", "-z",
                        "--no-ext-diff", "--diff-filter=ACMRT")
    if rc != 0:
        raise RuntimeError("git diff --cached failed: %s"
                           % err.decode("utf-8", "replace").strip())
    rels = [p.decode("utf-8", "surrogateescape")
            for p in out.split(b"\0") if p]
    parents = _parents(root) or ["HEAD"]
    rows = []
    for rel in sorted(rels):
        if not is_budgeted(rel):
            continue
        staged = _blob(root, "", rel)
        if staged is None:
            continue                      # unreadable index entry: say nothing
        rows.append((rel, staged, [_blob(root, ref, rel) for ref in parents]))
    return rows


def _limit(rel):
    return CEILING if rel.endswith("/seats.py") else FINISH


def way_out(rel, limit=None):
    """The fix for `rel` over its budget, as lines a seat can follow with no
    mentor: what to move, where, how to import it back, and past splits to
    copy. ONE TEXT, TWO READERS: this rung prints it and the whole-suite
    backstop imports it."""
    limit = _limit(rel) if limit is None else limit
    d, name = os.path.split(rel)
    stem = name[:-3] if name.endswith(".py") else name
    new = "%s_<topic>" % stem
    fanout = ('Add "%s" to _IMPL_MODULES in helm/seats.py, the facade\'s '
              'patch fan-out.' % new if stem == "seats" else
              'If _IMPL_MODULES in helm/seats.py lists "%s", add "%s" '
              'beside it.' % (stem, new))
    lines = [
        "THE WAY OUT: split %s. Move whole functions into a new sibling "
        "module and import them back:" % rel,
        "  1. Pick whole functions (and the constants only they use) that "
        "share one topic, enough to leave %s well under %d lines." % (
            rel, limit),
        "  2. Move them unchanged into a new file named for that topic: "
        "%s/%s.py. Give it its own imports for what the moved code uses. It "
        "must not import %s at the top (an import cycle), so a helper the "
        "moved code calls moves with it." % (d or ".", new, stem),
        "  3. Import them back near the top of %s, so every caller keeps "
        "working:" % rel,
        "         from .%s import name_a, name_b  # noqa: F401" % new,
        "  4. %s" % fanout,
        "  5. Run: git grep -n '%s' tests  (a test that reads the old file by "
        "path must also read the new one)." % name,
        "  6. git add both files and commit again.",
        "COPY A PAST SPLIT (git show <commit> shows every moved line):",
    ]
    lines += ["    %s  moved out of %s  in %s" % ex for ex in EXAMPLES]
    lines.append("The budget is never raised to fit a commit: it is what keeps "
                 "these files small enough to read.")
    return lines


def record(root, refusals, now=None):
    """Append ONE JSON line for this refusal to <common dir>/helm/refusals.jsonl.

    None when the line was written, else why it was not. NEVER RAISES: the
    refusal is decided before this runs and must not depend on it. One
    `os.write` of one line through O_APPEND, so rooms appending at once do
    not interleave inside a line."""
    try:
        rc, common, err = _git(root, "rev-parse", "--path-format=absolute",
                               "--git-common-dir")
        if rc != 0 or not common.strip():
            return "no git common dir (%s)" % err.decode(
                "utf-8", "replace").strip()
        rc, top, _err = _git(root, "rev-parse", "--show-toplevel")
        brc, branch, _err = _git(root, "symbolic-ref", "-q", "--short", "HEAD")
        row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
               "guard": GUARD,
               "worktree": os.fsdecode(top.rstrip(b"\n")) if rc == 0 else None,
               "branch": (os.fsdecode(branch.rstrip(b"\n")) or None)
                         if brc == 0 else None,
               "modules": [{"path": rel, "lines": staged, "was": head,
                            "budget": limit}
                           for rel, staged, limit, head in refusals]}
        path = os.path.join(os.fsdecode(common.rstrip(b"\n")), *RECORD)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if os.path.isfile(path) and os.path.getsize(path) > RECORD_ROLL_BYTES:
            os.replace(path, path + ".1")
        line = (json.dumps(row, sort_keys=True) + "\n").encode("utf-8")
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            wrote = os.write(fd, line)
        finally:
            os.close(fd)
        return None if wrote == len(line) else "short write (%d of %d bytes)" \
            % (wrote, len(line))
    except Exception as exc:                                  # noqa: BLE001
        return "%s: %s" % (type(exc).__name__, exc)


def scan(root):
    """(refusals, notes) for what this commit does to the budget."""
    refusals, notes = [], []
    for rel, staged, befores in _staged_budgeted(root):
        limit = _limit(rel)
        # GREW means bigger than EVERY parent had it: a count any parent
        # already carried is that parent's growth, not this commit's, and an
        # unborn/absent path in all of them is new.
        grew = all(before is None or staged > before for before in befores)
        head = max([b for b in befores if b is not None], default=None)
        if staged > limit and grew:
            refusals.append((rel, staged, limit, head))
        elif staged > limit:
            notes.append("%s is %d lines, over the %d budget, but this commit "
                         "does not grow it (%s) — not refused here; the debt "
                         "is somebody's to pay"
                         % (rel, staged, limit,
                            "was %d" % head if head is not None else "new"))
        elif limit - staged <= WARN_BAND:
            notes.append("%s is %d lines, %d from the %d budget"
                         % (rel, staged, limit - staged, limit))
    return refusals, notes


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--staged" not in argv:
        print("usage: splitbudget.py --staged [--repo PATH]", file=sys.stderr)
        return 2
    root = "."
    if "--repo" in argv:
        root = argv[argv.index("--repo") + 1]
    try:
        refusals, notes = scan(root)
    except Exception as exc:                                  # noqa: BLE001
        # NOT FOR A PATHNAME THAT WILL NOT DECODE: this rung asks
        # nevertrack.commit_parents for the parent set, that helper carries
        # `rev-parse --git-path` as BYTES, and so a repo under a non-UTF-8
        # ancestor reaches the budget decision instead of landing here as
        # UNMEASURED and returning success with the inherited-debt vs
        # new-growth distinction dropped. An arm varies only that ancestor.
        #
        # A RUNG THAT CANNOT MEASURE SAYS SO AND LETS THE COMMIT THROUGH. It
        # is an early warning for a check the suite still makes; refusing on
        # an unreadable index would wall every commit on this rung's own bad
        # day, and the backstop has not moved.
        print("[helm split-budget] UNMEASURED (%s: %s) — the whole-suite arm "
              "still enforces this" % (type(exc).__name__, exc), file=sys.stderr)
        return 0
    for note in notes:
        print("[helm split-budget] note: %s" % note, file=sys.stderr)
    if not refusals:
        return 0
    print("[helm split-budget] REFUSED: this commit takes %d module(s) past "
          "the seats-split line budget. The fix is to split: move whole "
          "functions into a new sibling module and import them back (steps "
          "below)." % len(refusals), file=sys.stderr)
    for rel, staged, limit, head in refusals:
        print("    %s  %s -> %d  (budget %d)"
              % (rel, "new" if head is None else head, staged, limit),
              file=sys.stderr)
    for line in way_out(refusals[0][0], refusals[0][2]):
        print("  " + line, file=sys.stderr)
    if len(refusals) > 1:
        print("  Do the same for every module listed above.", file=sys.stderr)
    print("  Do not trim old comments to fit: the next commit meets the same "
          "wall. Only when this commit's own growth is a comment may that text "
          "move to the commit message instead. Owner override for one commit: "
          "%s=1" % SKIP_ENV, file=sys.stderr)
    why = record(root, refusals)
    if why:
        print("[helm split-budget] note: this refusal was not recorded (%s); "
              "the refusal stands." % why, file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
