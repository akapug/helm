"""A verdict stamp on task rows (task/3747).

Triage verdicts were living in side files, so every triage re-read rows
another reader had already checked, and nothing on a row said who verified it,
when, or against which trunk. This module carries that fact in the row itself:
a `verified` list of stamps, newest last, capped, each saying who, when,
against which trunk, what the verdict is, the evidence, and the link to the
verdict.

The module is a single vocabulary and a single cap. `VERDICTS` is the ONE
word set; `stamp` refuses anything outside it, so a row cannot carry a verdict
the fleet does not speak. `CAP` bounds the history a row carries: a row that
was verified and re-verified is still read once, and the newest five stamps
are the ones a triage needs to see.

The API is `tasks.verify(token, verdict, trunk, evidence, by=None, link=None,
path=None)` -> (row, err). `verify` reads the row, resolves `trunk` in the
task's project repo (so the row names the FULL sha the reader actually read,
never the raw input), and stamps through the same locked write path a comment
uses. `show_line(row)` renders the newest stamp for `helm task show`; `None`
when no stamp is present."""
import os
import re
import time

from . import registry, vcs

VERDICTS = ("LIVE", "NARROW", "FIXED", "STALE")
CAP = 5
_SHA = re.compile(r"^[0-9a-f]{7,40}$", re.IGNORECASE)


class REFUSE(Exception):
    """A stamp refused before anything was written. The refusal names the
    bad value and the rule it broke, so the caller can teach the operator
    rather than report a blank failure."""


def _clean_value(value):
    """(cleaned, err) — a field that must be a one-line string.

    An empty, whitespace-only, or multi-line value is refused: evidence that
    is nothing says nothing, and evidence that spans lines would be stored as
    a string a line-oriented reader (and `helm task show`) could not render
    as one line. Whitespace at the ends is stripped so a padded argument
    does not land with invisible bytes on either side."""
    if not isinstance(value, str):
        return None, ("a stamp field must be text, not %s" % type(value).__name__)
    value = value.strip()
    if not value:
        return None, "the value is empty — nothing to record"
    if "\n" in value or "\r" in value or "\t" in value:
        return None, "the value must be a single line"
    return value, None


def resolve_trunk(repo, sha):
    """The FULL 40-hex sha of `sha` in `repo`, via `git rev-parse --verify`.

    `sha` must be a full sha or a 7-or-more hex prefix: a tag or branch name
    is refused, because it names whatever it points at later, not the commit
    the reader read. `^{commit}` pins the prefix to that commit.
    The return value is the exact sha to store in `verified.trunk` — the reader
    can re-verify `git log` against it later. A short prefix that is not a
    commit in this repo, or a repo that is not a git root, raises `REFUSE`;
    the caller names the error rather than a second reader discovering an
    unresolvable trunk at triage time."""
    value, err = _clean_value(sha)
    if err:
        raise REFUSE("the trunk sha is empty or not text")
    if not _SHA.match(value):
        raise REFUSE(
            "trunk %r is not a git sha (a full 40-hex sha or a 7-or-more hex "
            "prefix)" % value)
    # THE SPAWN IS THROUGH THE SEAM, NOT A DIRECT SUBPROCESS. `resolve_trunk`
    # asks `git rev-parse --verify <sha>^{commit}` via `vcs.GitVcs().text`,
    # the sanctioned spawn site (helm/vcs.py), with the repository as the
    # `cwd` so the seam's repository-selection and read-view are applied exactly
    # as for every other repository read in this package. Outside a
    # `projscope.scope()` the seam computes directly (no memo, unbudgeted), so a
    # stamp in an unscoped verb still answers the same question a scoped reader
    # would. The seam returns `(rc, out, err)`; `rc` is -1 or a non-zero git
    # rc on refusal, `out` is the stripped sha on success and `err` the git
    # diagnostic on failure. This layer refuses with both named so a second
    # reader never discovers an unresolvable trunk at triage time.
    rc, out, err_out = vcs.backend(repo).text(
        repo, "rev-parse", "--verify", "%s^{commit}" % value)
    if rc != 0:
        raise REFUSE(
            "trunk %r does not name a commit in %s (%s)"
            % (value, os.path.basename(str(repo) or repo),
               (err_out or out or "").strip() or "git refused it"))
    return out.strip()


def stamp(row, verdict, trunk, evidence, by, link=None, now=None):
    """Append one verdict stamp to `row` -> the row, newest last, capped to CAP.

    `trunk` is the FULL sha already resolved by the caller (`resolve_trunk`);
    `stamp` records it as given. `verified_by` is the caller-stated seat or
    OwnerDoor name, or `None` for a stamp with no author recorded.
    `verified_at` is `now` (epoch seconds) when given, else the wall clock;
    `now` is a seam for tests, not a lie about the write.

    The new stamp is appended to `row["verified"]` (created when absent), and
    the list is truncated to the newest `CAP` entries, oldest last. The
    row is returned (the caller is expected to pass the same row it read,
    so a later read under the lock still sees the newest stamp).

    Refuses (raising `REFUSE`, before writing): a verdict outside `VERDICTS`,
    or an empty or multi-line `evidence`. `trunk` and `by` are validated as
    one-line strings; `link`, when given, is likewise validated."""
    if verdict not in VERDICTS:
        raise REFUSE(
            "verdict %r is outside the vocabulary %s"
            % (verdict, " | ".join(VERDICTS)))
    evidence, eerr = _clean_value(evidence)
    if eerr:
        raise REFUSE("evidence: %s" % eerr)
    trunk, trr = _clean_value(str(trunk))
    if trr:
        raise REFUSE("trunk: %s" % trr)
    if link is not None:
        link, lr = _clean_value(link)
        if lr:
            raise REFUSE("link: %s" % lr)
    if now is None:
        now = time.time()

    # EVIDENCE AND LINK ARE BOUNDED: a one-line stamp of any size would push
    # the row toward the event cap, and a row that outgrows it then fails
    # close/update/comment on itself. Refuse BEFORE the row changes (REFUSE,
    # no write) naming the limit and the length. The caps live in tasks (the
    # module that owns row sizes); it is imported here only to read a constant,
    # so the task <==> taskverify edge is a read of an already-loaded module,
    # never a call into it.
    from . import tasks
    if len(evidence) > tasks.COMMENT_TEXT_MAX:
        raise REFUSE(
            "evidence %d chars exceeds the %d-char limit"
            % (len(evidence), tasks.COMMENT_TEXT_MAX))
    if link is not None and len(link) > 512:
        raise REFUSE("link %d chars exceeds the 512-char limit" % len(link))

    verified = list(row.get("verified") or ())
    verified.append({
        "verified_at": float(now),
        "verified_by": str(by) if by is not None else None,
        "trunk": trunk,
        "verdict": verdict,
        "evidence": evidence,
        "link": link,
    })
    row["verified"] = verified[-CAP:]
    return row


def show_line(row):
    """One line for the newest stamp, `None` when there is no stamp.

    The exact shape a reader needs to know who verified, when, against which
    trunk, what the verdict is, the evidence, and the link:
    `verified <verdict> by <by> at <YYYY-MM-DD HH:MMZ>, trunk <sha>: <evidence>`
    plus ` (<link>)` when a link is present. The newest stamp wins (newest
    last, so `verified[-1]`). The timestamp is UTC with no seconds, matching
    the other row timestamps."""
    verified = row.get("verified")
    if not verified:
        return None
    s = verified[-1]
    # A malformed stamp (missing or non-numeric `verified_at`) renders "at ?"
    # rather than crashing: show_line is the render door, so a corrupted row
    # must not take down `helm task show`.
    vtime = s.get("verified_at")
    try:
        when = time.strftime("%Y-%m-%d %H:%MZ", time.gmtime(float(vtime)))
    except (TypeError, ValueError, OverflowError):
        when = "?"
    by = s.get("verified_by") or "unrecorded"
    line = "verified %s by %s at %s, trunk %s: %s" % (
        s.get("verdict") or "?", by, when,
        s.get("trunk") or "?", s.get("evidence") or "")
    if s.get("link"):
        line += " (%s)" % s["link"]
    return line


def project_repo_path(name):
    """(path, why): the git repo path registered for project `name`, or None
    and the reason the registry could not answer.

    The one place a project name is mapped to the repo it resolves against.
    The reason keeps the four failures apart (no project on the row, a
    registry that cannot be read, a project not registered, a record with no
    path), so a refusal sends the operator to the real cause. The read is
    STRICT, as for every caller that decides: a tolerant read turns a corrupt
    registry into "not registered", which sends the operator to the wrong fix."""
    if not name:
        return None, "the row names no project"
    try:
        projects = (registry.load(strict=True) or {}).get("projects") or {}
    except Exception as exc:
        return None, "the project registry cannot be read (%s)" % exc
    # LOOKED UP BY NAME, NOT BY KEY: `registered_projects` (tasks) maps each
    # record to `rec.get("name") or key`, and THAT is what a row's project
    # field carries. The old `projects.get(name)` read the key, so a record
    # whose name differs from its key was unreachable — a row scoped to the
    # NAME could not find its repo. Match on the same value the rest of the
    # ledger resolves, key as fallback so a key-only record still resolves.
    rec = None
    for key, value in projects.items():
        if isinstance(value, dict) and (value.get("name") or key) == name:
            rec = value
            break
    if rec is None:
        return None, "project %r is not registered" % name
    path = rec.get("path")
    if not isinstance(path, str) or not path:
        return None, "project %r is registered without a repo path" % name
    return path, None
