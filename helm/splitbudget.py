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

IMPORTS NOTHING FROM helm. The installer snapshots these bytes beside the
pre-commit hook, where no helm package exists to import (see
helm/inflight_gate.py, which states the same law).
"""
import os
import subprocess
import sys

#: An EXTRACTED module over this defeats the split.
FINISH = 1000
#: The facade's own ratchet. It is supposed to DRAIN, never grow back.
CEILING = 1000
#: How close to the budget is worth saying out loud while there is still room.
WARN_BAND = 25

SKIP_ENV = "HELM_SPLIT_BUDGET_SKIP"
_PKG = "helm"


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
          "the seats-split line budget:" % len(refusals), file=sys.stderr)
    for rel, staged, limit, head in refusals:
        print("    %s  %s -> %d  (budget %d)"
              % (rel, "new" if head is None else head, staged, limit),
              file=sys.stderr)
    print("  The budget is what DRAINS the facade, so it is not raised to fit "
          "a commit. Move the rationale to the task row or the commit message, "
          "or extract. One-commit skip: %s=1" % SKIP_ENV, file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
