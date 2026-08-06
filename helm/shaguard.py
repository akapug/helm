#!/usr/bin/env python3
"""helm shaguard — the PROSE half of the sha check.

THE ASYMMETRY THIS CLOSES. helm's TYPED sha paths are already guarded:
`helm dispatch send --ref` resolves the ref and refuses one it cannot, and
dispatches._ref_sanity then warns about refs that DO resolve but mislead. The
PROSE path had nothing at all. A sha travelling inside a chat message, a DM, a
verdict announcement or a seat's status line was never looked at — and prose is
exactly where a reviewer picks a tip up and binds work to it.

THE DEFECT, measured four times in one session across two model families, with
ZERO of them caught by the agent that made the error:

  1. a real short sha `a009d56` was PADDED into a fabricated 40-hex. It
     surfaced only because it was typed into `dispatch send --ref`, which
     refused it — the TYPED path doing its job.
  2. a seat announced its tip as `1161c6f93798aa1f…`. The real tip was
     `1161c6f7a702…`. Caught by a human re-running cat-file.
  3. the SAME seat, the very next round and immediately after being told,
     announced `8f130075ebf5…` against a real `8f130078e0c4…`.

Every one had the right SHAPE, the right LENGTH and a correct short PREFIX,
then invented the remaining hex. That is invisible to re-reading — the reason
nobody self-catches it — and only git can tell you.

WHAT THIS IS NOT, and the constraint that makes it correct rather than merely
clever: it is NOT a fabrication detector and it must never speak as one. A sha
that does not resolve HERE is not proof of anything. helm files issues and PRs
against other repositories, a lane object may be unfetched, and a 40-hex token
explicitly presented as a patch-id may be CONTENT IDENTITY rather than an object
id. The last class is silenced only when the committed PATCH_IDS registry owns it
or this checkout recomputes it from HEAD's commit/lane diff; the label alone is
never authority. Every remaining warning reports exactly what was measured —
"this checkout cannot resolve it" — and names benign explanations. Writing
"this sha is fake" would reproduce the confident-negative defect class inside
its own guard.

THREE LAWS:

  * WARN BY DEFAULT; REFUSE ONLY WHAT IS PROVEN. The original law here was
    WARN, NEVER BLOCK, and its reasoning still holds for the case it was about:
    a foreign sha this checkout cannot resolve is AMBIGUOUS, and refusing it
    would refuse a true statement. That case still only warns. What changed
    2026-08-04 is that one finding is not ambiguous — a token whose LONG prefix
    resolves to a DIFFERENT local object is a padded short sha by measurement,
    and warning about it let one reach a teammate and corrupt their merge
    probe. `refusals()` carries the split and REFUSE_PREFIX_MIN carries the
    proof threshold; HELM_SHA_GUARD_SKIP=1 covers the one honest case, quoting
    a wrong sha in order to correct it.
  * NO OVERCLAIM (above).
  * 40-HEX ONLY. A short prefix (7-12) is safe to DISPLAY and unsafe only to
    EXTEND — `a009d56` is a fine thing to write, and the failure is what
    happens when someone pads it. Warning on short prefixes would drown the
    signal in every honest commit reference in the fleet. `_SHA40` is the
    enforcement, and tests/test_shaguard.py pins that a 7-12 char prefix stays
    silent.

COST. The regex is the gate: a body with no 40-hex token spawns nothing, which
is nearly every message. A body WITH one costs one repo probe plus one cat-file
per distinct token. Only a line explicitly labeled patch-id pays for the bounded
HEAD/upstream diff recomputation; only an unresolved object pays for the bounded
prefix hunt below.

Git goes through `vcs.backend(target)` — never a raw subprocess
(tests/test_vcs.py's DirectSpawnAuditTest pins the spawns outside that seam,
and the seam already owns timeouts and a missing-binary fallback). The target
is always passed explicitly, per that module's per-TARGET selection law — and
the argument-less spelling of that call appears nowhere in this file, prose
included, because the audit forbidding it is a line scan and a docstring is
lines.
"""
import os
import re
import sys

# The one honest reason to send a padded sha: QUOTING it to correct it.
# Named after the never-track rung's escape so the shape is familiar, and
# deliberately per-send rather than a config: an override that persists is
# an override that is on when nobody meant it.
SKIP_ENV = "HELM_SHA_GUARD_SKIP"

# A prefix match this long is proof of padding, not coincidence: at this
# repo's object count a 10-char collision runs about 1 in 5 billion, while
# 7 chars is about 1 in 17,000 — low, but a real chance of refusing a true
# statement about a foreign sha, which is what the WARN-NEVER-BLOCK law was
# written to prevent.
REFUSE_PREFIX_MIN = 10

# Word-boundary 40-hex, LOWERCASE — the shape git itself prints, and the shape
# every one of the four measured fabrications had. `\b` on both ends means a
# 64-hex digest (chat's own blake2b payload ids are 64) can never match on its
# first 40 characters: position 40 sits between two word characters, so it is
# not a boundary. Uppercase is deliberately out: git object names are lowercase
# hex, so an uppercase run is some other encoding, and matching it would put
# this guard's first false positive in the class it exists to avoid.
_SHA40 = re.compile(r"\b[0-9a-f]{40}\b")
_PATCH_LABEL = re.compile(r"\bpatch[- ]id\b", re.IGNORECASE)

# The bounded prefix hunt for the "did you mean" hint, LONGEST first. The three
# measured fabrications diverged at character 8, 8 and 8 respectively — a
# correct 7-char prefix then invented hex — so 7 is the floor that catches the
# observed class and 12 is a ceiling that keeps the worst case at six extra
# spawns on a token that has ALREADY failed. Not a binary search: prefix
# resolution is not monotone (a shorter prefix can become AMBIGUOUS and fail
# where a longer one resolved), so halving would silently skip real answers.
_PREFIX_LENS = tuple(range(12, 6, -1))

_TIMEOUT = 5          # a chat post must never hang behind a slow object store


def tokens(text):
    """The DISTINCT 40-hex tokens in `text`, in first-seen order.

    Order is preserved rather than set-returned so the warnings a reader sees
    come out in the order they appear in their own message.
    """
    out, seen = [], set()
    for tok in _SHA40.findall(text or ""):
        if tok not in seen:
            seen.add(tok)
            out.append(tok)
    return out


def patch_tokens(text):
    """40-hex tokens explicitly presented as patch IDs, in message order.

    A label is only a request to try content verification, never authority by
    itself. findings() silences one only when the committed PATCH_IDS registry
    owns it or this checkout recomputes it from the current commit/lane diff.
    """
    out, seen = [], set()
    for line in (text or "").splitlines():
        if not _PATCH_LABEL.search(line):
            continue
        for tok in _SHA40.findall(line):
            if tok not in seen:
                seen.add(tok)
                out.append(tok)
    return out


def _cwd():
    """os.getcwd() failing OPEN to None — a pruned lane worktree is a ROUTINE
    lifecycle state here (seats.safe_cwd carries the incident). Spelled locally
    rather than imported: chat imports this module, seats imports chat, and a
    guard has no business creating an import cycle."""
    try:
        return os.getcwd()
    except OSError:
        return None


def _probe(be, root, *args):
    """Stripped stdout on rc 0, else None. Every failure mode — nonzero exit,
    missing git, timeout, a root that no longer exists — folds to None, and
    None always means SILENT here."""
    try:
        rc, out, _err = be.text(root, *args, timeout=_TIMEOUT)
    except Exception:
        return None
    return out if rc == 0 else None


def _stable_patch_id(be, root, base, tip):
    """One content identity from an exact diff, or None on any uncertainty."""
    rc, diff, _err = be.run(root, "diff", "--no-ext-diff", "--binary",
                            base, tip, "--", timeout=_TIMEOUT)
    if rc != 0 or not diff:
        return None
    rc, out, _err = be.text(root, "patch-id", "--stable", stdin=diff,
                            timeout=_TIMEOUT)
    rows = [line.split() for line in out.splitlines() if line.strip()]
    if rc != 0 or len(rows) != 1 or len(rows[0]) != 2:
        return None
    return rows[0][0] if _SHA40.fullmatch(rows[0][0]) else None


def _current_patch_ids(be, root):
    """Patch identities for HEAD's commit and aggregate lane diff.

    The chat happy path reports patch-id before/after a rebase from the lane
    worktree itself. HEAD^..HEAD covers one-commit evidence; merge-base against
    the configured upstream or origin/HEAD covers a multi-commit lane. Every
    failed probe drops one candidate, never turns a label into authority.
    """
    tip = _probe(be, root, "rev-parse", "--verify", "HEAD")
    if not tip:
        return set()
    pairs = []
    parent = _probe(be, root, "rev-parse", "--verify", "HEAD^")
    if parent:
        pairs.append((parent, tip))
    for ref in ("@{upstream}", "origin/HEAD"):
        upstream = _probe(be, root, "rev-parse", "--verify", ref)
        if not upstream:
            continue
        base = _probe(be, root, "merge-base", tip, upstream)
        if base and base != tip:
            pairs.append((base, tip))
    return {pid for base, head in dict.fromkeys(pairs)
            for pid in [_stable_patch_id(be, root, base, head)] if pid}


def findings(text, root=None):
    """[(token, prefix_or_None, real_sha_or_None)] for every 40-hex token in
    `text` that this checkout cannot resolve. [] when everything resolves, when
    there is nothing to check, or when the repo cannot answer at all.

    THE REPO PROBE IS LOAD-BEARING. Without it, running from a directory that
    is not a git checkout makes `cat-file -e` fail for EVERY token, and the
    guard would warn confidently about perfectly real shas — a confident
    negative manufactured by its own blind spot. If git cannot speak about this
    directory, this function has measured nothing and says nothing.
    """
    toks = tokens(text)
    if not toks:
        return []                      # the fast path: no spawn, no import cost
    root = root or _cwd()
    if not root:
        return []
    try:
        from . import vcs
        be = vcs.backend(root)
    except Exception:
        return []
    if _probe(be, root, "rev-parse", "--git-dir") is None:
        return []                      # not a checkout we can read: unknowable
    from . import docref_guard
    labeled = set(patch_tokens(text))
    verified_patches = (set(docref_guard.PATCH_IDS)
                        | (_current_patch_ids(be, root) if labeled else set()))
    out = []
    for tok in toks:
        if tok in verified_patches and (tok in labeled
                                        or tok in docref_guard.PATCH_IDS):
            continue                  # content identity, not a Git object
        # `cat-file -e <tok>` asks about the OBJECT, not the commit: a message
        # may legitimately name a tree or a blob, and `^{commit}` would report
        # a real blob sha as missing. rc 0 prints nothing, so the test is
        # `is not None` — an empty string is the success answer here.
        if _probe(be, root, "cat-file", "-e", tok) is not None:
            continue
        out.append((tok,) + _nearest(be, root, tok))
    return out


def _nearest(be, root, tok):
    """(prefix, full_sha) for the LONGEST bounded prefix of `tok` this checkout
    does resolve, else (None, None).

    This is the single most useful thing the warning can carry: the three
    measured fabrications all kept a correct 7-char prefix, so the real object
    the author meant is one short lookup away — and printing it lets them
    compare the two strings instead of re-reading the one they already wrote.
    """
    for n in _PREFIX_LENS:
        # --verify --quiet: an AMBIGUOUS prefix exits nonzero and prints
        # nothing, which is the honest answer (git cannot say which object) and
        # folds to the same silence as a missing one. `^{object}` pins the
        # question to the object store rather than the ref namespace.
        full = _probe(be, root, "rev-parse", "--verify", "--quiet",
                      tok[:n] + "^{object}")
        if full:
            return tok[:n], full
    return None, None


def warnings(text, root=None):
    """The stderr lines for `text` — [] when there is nothing to say.

    Every sentence here is bounded by what was actually measured. "does not
    name an object in this checkout" is the measurement; the second line is the
    honest list of reasons that is NOT damning; the prefix line is offered as a
    conditional ("if you meant") because a coincidental prefix collision is
    possible and this guard does not get to decide what the author meant.
    """
    out = []
    for tok, prefix, real in findings(text, root):
        lines = [
            "helm: WARNING — a 40-hex sha in this message does not name an "
            "object",
            "  in this checkout:  %s" % tok,
            "  This is NOT proof the sha is wrong. It may name a commit in an "
            "upstream",
            "  repo, in another fork, or in a lane whose objects were never "
            "fetched here.",
            "  All that was measured is that THIS checkout cannot resolve it.",
        ]
        if prefix:
            # The two shas go on ADJACENT lines, aligned. The whole failure is
            # that a wrong sha is invisible to re-reading — so the useful act
            # is not describing it, it is putting the string the author typed
            # directly above the string git has.
            lines += [
                "  Its %d-char prefix %s DOES resolve here, to a DIFFERENT "
                "object:" % (len(prefix), prefix),
                "      you wrote:  %s" % tok,
                "      resolves:   %s" % real,
                "  If you meant that object, the rest of the token is not its "
                "hex —",
                "  a padded short sha looks exactly like this.",
            ]
        # THE REFUSAL LANGUAGE APPEARS ONLY WHERE THE REFUSAL DOES. A short
        # prefix match still only warns, so printing "REFUSED" beside it would
        # make the message contradict the behaviour — and would put a verdict
        # on the one finding this guard cannot support. Caught by
        # NoOverclaimTest and the CLI funnel test, both of which were right.
        if prefix and len(prefix) >= REFUSE_PREFIX_MIN:
            lines.append(
                "  REFUSED — a %d-char prefix resolving to a different object "
                "is measured, not ambiguous." % len(prefix))
            lines.append(
                "  Resolve it instead: TIP=$(git rev-parse %s) and interpolate "
                "$TIP." % prefix)
            lines.append(
                "  Quoting this token deliberately (to correct it) is the one "
                "honest case: %s=1." % SKIP_ENV)
        else:
            lines.append("  Sent anyway — this is a warning, never a block.")
        out.append("\n".join(lines))
    return out


def refusals(text, root=None):
    """The findings that must BLOCK rather than annotate — padded short shas.

    THE SPLIT IS THE WHOLE POINT, and it is drawn where the evidence changes
    kind. A 40-hex token this checkout cannot resolve is AMBIGUOUS: it may name
    a commit upstream, in another fork, or in a lane never fetched here, and
    blocking on it would refuse true statements. But a token whose 12-char
    prefix resolves to a DIFFERENT object is not ambiguous — the author had the
    real thing in hand and the tail is not its hex. That is a padded short sha,
    and the guard has already measured it.

    Warning was not enough. Measured across one night, 2026-08-04: four padded
    shas from one seat. The three aimed at `dispatch verdict` were REFUSED by
    that verb and cost nothing. The one aimed at `helm chat` was warned about
    and SENT, reached a teammate holding a build row, and made their merge
    probe report a CONFLICT computed against a rev that does not exist — a
    fabricated sha does not fail loudly at the reader, it produces a confident
    wrong answer inside someone else's instrument. Same guard, same evidence,
    opposite outcome, decided only by which verb was called.

    THE LENGTH FLOOR IS WHAT KEEPS THE OLD LAW INTACT. Refusing on ANY prefix
    match would put this module's own false-refusal fear back on the table: a
    legitimate foreign sha whose first 7 characters happen to match some local
    object would be blocked, and at this repo's object count that is roughly 1
    in 17,000 foreign shas — rare, but a real refusal of a true statement. At
    ten characters it is about 1 in 5 billion, which is not a trade-off any
    more. So a LONG prefix match refuses and a short one still only warns.

    The cost of that floor, stated rather than buried: the three fabrications
    in this module's own header diverged at character 8, so THEY would still
    only warn. This catches the class it can prove and leaves the rest exactly
    as loud as it was.
    """
    return [f for f in findings(text, root)
            if f[1] and len(f[1]) >= REFUSE_PREFIX_MIN]


def refuse(text, root=None, stream=None):
    """-> True when the caller MUST NOT send. Prints why. Never raises.

    Fail-OPEN on its own trouble, like every guard in this module: a check that
    cannot run must not become a check that blocks everything. The env escape
    is read here rather than in the caller so every caller inherits it.
    """
    try:
        if os.environ.get(SKIP_ENV):
            return False
        bad = refusals(text, root)
        if not bad:
            return False
        for line in warnings(text, root):
            print(line, file=stream or sys.stderr)
        return True
    except Exception:
        return False


def warn(text, root=None, stream=None):
    """Print the warnings for `text` to stderr; return how many were printed.

    Wrapped whole in a try: a guard that can raise is a guard that can break
    the send it was only ever supposed to annotate.
    """
    try:
        said = warnings(text, root)
        for line in said:
            print(line, file=stream or sys.stderr)
        return len(said)
    except Exception:
        return 0
