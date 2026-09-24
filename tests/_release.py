"""What a release export of this repository leaves out, and how a test says so.

A public release is published as one commit: this tree minus a few internal
documents, with none of the development history. A test that reads one of
those documents, or reads the repository's own history, cannot hold in that
tree. It skips there, and the skip names why; wherever the document or the
history is present, it runs in full. Nothing here excuses a path or a history
the development repository is supposed to have: a document that is not listed
below still fails loudly when it is missing, and the history checks run
unchanged whenever the history they cite is here.
"""
import os
import subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Documents the release omits and that a test reads. Keep this to the paths a
# test opens: a path listed here and absent is a skip, never a failure.
RELEASE_OMITS = frozenset((
    "docs/CANON_CONTROLLED_LANGUAGE_LANES.md",
    "docs/ORCA_SEAM_AUDIT.md",
    "docs/REBOOT_CHECKLIST_1.4.149.md",
))


def omitted(rel):
    """The skip reason when `rel` is a document the release omits and this
    tree does not have it; None when the test can read it (it is present) or
    must not be excused (it is not listed)."""
    if rel in RELEASE_OMITS and not os.path.exists(os.path.join(ROOT, rel)):
        return ("%s is not in this tree: the release export omits it "
                "(tests/_release.py)" % rel)
    return None


def _git(*args):
    return subprocess.run(("git", "-C", ROOT) + args, capture_output=True,
                          text=True, timeout=60)


def commit_count(*rev_list_args):
    """Commits reachable from HEAD (narrowed by `rev_list_args`, for example
    "--no-merges", "--max-count=20"), or None when git cannot say."""
    p = _git("rev-list", "--count", *rev_list_args, "HEAD")
    out = p.stdout.strip()
    return int(out) if p.returncode == 0 and out.isdigit() else None


def history_gap(witness):
    """None when this repository holds `witness`, a commit of the development
    history; else the reason history-bound checks cannot judge this checkout:
    a release export (its history starts at the release) or a shallow clone.
    Only the object's ABSENCE counts. A witness that is present but no longer
    reachable is the development repository's own defect and gets no excuse."""
    if _git("cat-file", "-e", witness + "^{commit}").returncode == 0:
        return None
    shallow = _git("rev-parse", "--is-shallow-repository").stdout.strip()
    return ("commit %s is not in this repository (%s commits reachable from "
            "HEAD, shallow=%s): this checkout does not carry the development "
            "history, as in a release export or a shallow clone "
            "(tests/_release.py)" % (witness, commit_count(), shallow or "?"))
