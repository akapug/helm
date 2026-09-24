#!/usr/bin/env python3
"""Pre-commit rung: REFUSE a staged test that introduces a REAL seat identity.

`tests/` is public-bound. A real seat identity in a fixture is not a secret and
not a live exposure — it is PUBLICATION DEBT, payable at export, and it
compounds every time a new test copies the local convention. The house already
has the cure in wide use: seat-a, seat-b, seat-c, seat-under-test.

WHY A RUNG AND NOT A SWEEP. A sweep is a one-time payment that decays the
moment the next test is written. That is also why "zero constructed-value doors
in the tree today" was REJECTED as a defence for leaving one open: the
planned export sweep is precisely the thing that would manufacture such a
door at scale, and a prevention rung may not carry a hole its own successor
work will fill.

WHAT IT REFUSES IN A STAGED tests/ FILE — one small, TOTAL guarantee, and
the operator list is EXHAUSTIVE rather than illustrative:
  · a string literal whose VALUE is a real seat identity, compared
    case-insensitively, on an added line;
  · the same value produced by CONCATENATION of such literals (`a + b`), which
    `_folded` decides in the source and nowhere else.

THAT LIST IS THE WHOLE OF IT. An earlier draft of this docstring promised
refusal for "an expression whose operands are ALL constant", which is WIDER
THAN THE CODE BENEATH IT: `_folded` recognises Constant and BinOp(Add), so
`"zz-synthetic-%s" % "seat"` and a constant f-string are both decided in the
source, both produce an armed identity, and both pass. Promising a grammar
and implementing two of its operators is the same failure as the
source-can-produce claim this rung already retired, one level down — so the
promise is narrowed to what is pinned rather than the pin widened to a
grammar no arm covers. Refusing %-formatting and f-strings is its own row
with its own per-operator mutation arms.

WHAT IT DOES NOT PROVE, STATED PLAINLY BECAUSE A GUARD THAT OVERSTATES ITS
REACH IS WORSE THAN A NARROW ONE: this rung DOES NOT PROVE ARBITRARY RUNTIME
CONSTRUCTION. A value assembled through a local alias, a positional call this
module does not model, a nonconstant template, or any other partial provenance
can carry an identity past it. Two earlier designs tried to decide "could this
source produce an identity" in general; each grew a bespoke dataflow engine
that was wrong in a NEW way, and the honest ends of that road are an
incomplete analyzer that lies or fail-closed UNKNOWNs on ordinary names.

The delimited-token shape (`"seat-one,seat-two".split(",")`, which yields
seat-name values while never being one) and the identity-bearing sinks are
still MEASURED and still REPORTED — they are real signal and they found real
debt — but they are ADVISORY and cannot gate a commit. Broader prevention
belongs at a constrained test-identity API or an export boundary, not in a
pre-commit analyzer guessing at what a program might compute.

WHAT IT DOES NOT REFUSE, and this residue is LEGITIMATE rather than debt: a
seat name inside ordinary prose. The earlier version claimed it "does not touch
prose" — that was FALSE whenever the prose quoted the name, because a byte
matcher cannot tell a quoted name in a docstring from a fixture value.
Parsing gives the literal's VALUE instead of its bytes, so prose is now
distinguishable by construction. Some prose legitimately NAMES seats as its
subject rather than citing them: a test that documents case-folding between
a committer string and a roster key needs BOTH spellings, and they ARE the
thing under test. That residue uses the reasoned escape and is expected, not
owed.

IT FAILS CLOSED ON A BROKEN MEASUREMENT, AND ONLY ON THAT. An unreadable staged
entry, a diff git will not produce, an authority whose bytes will not decode —
each is a REFUSAL naming what could not be read, because an unreadable
measurement is not evidence of a clean tree. The first version SKIPPED on an
unreadable diff while its own docstring argued the opposite for the roster.

A BROKEN INSTALL IS A DIFFERENT ANIMAL and gets the opposite answer. If a
sibling snapshot is missing this rung WARNS LOUDLY and stands down, exactly as
every sibling does for its own. The v3 rung law says each rung's absence
isolates to itself, and it is right: an estate-integrity fault must not block
every commit in every room, and deleting one scanner must not turn into another
scanner's refusal. Conflating the two cost two sibling rungs' tests — measured,
not theorised.

IT BORROWS THE HARDENED SEAMS RATHER THAN REPAIRING A PRIVATE PARSER, which is
how seven bypasses came to exist at once in the first implementation:
  · nevertrack._payload      — staged blob + added bytes in ONE lookup, with
                               the binary fallback (added-side IS the blob).
  · conflict_marker.staged_entries / _added_ranges — NUL-safe literal paths,
                               `-M`, `:(literal)` pathspecs, and --no-textconv.
                               A textconv filter can display `seat-a` while the
                               staged blob carries the real identity.
Those live BESIDE this file in the snapshot dir, so importing them needs no
helm package — `import helm` from a snapshot FAILS and would make this rung
inert everywhere it is installed (inflight_gate paid for that law).
"""
import ast
import fcntl
import io
import os
import re
import subprocess
import sys
import tokenize

_CONVENTION = ("seat-a", "seat-b", "seat-c", "seat-under-test")
# The escape must be a REAL COMMENT token, never a substring: text that merely
# CONTAINS the token inside ordinary string data disarmed the old check.
_ESCAPE = re.compile(r"#\s*noqa:\s*SEAT_NAME\s+(?:—|-)\s*(\S.*)$")
_DELIM = re.compile(r"[,;:\s|]+")


def _sibling(name):
    """A snapshot neighbour, or None.

    NO PACKAGE FALLBACK WHEN SCRIPT-RUN. Falling back to `helm.<name>` meant a
    broken estate — one whose snapshot is missing but whose PYTHONPATH or
    editable checkout exposes helm — silently measured with MUTABLE SOURCE
    instead of issuing the v3 missing-snapshot warning. The whole point of a
    snapshot is that the tree being committed cannot edit its own judge.
    """
    if __package__:               # imported as helm.seatname_guard, in-tree
        try:
            return __import__("helm." + name, fromlist=[name])
        except Exception:                                     # noqa: BLE001
            return None
    # SCRIPT-RUN: the sibling must live in THIS FILE'S OWN DIRECTORY. A bare
    # __import__ searches all of sys.path, so an inherited PYTHONPATH or an
    # editable checkout satisfied it with MUTABLE SOURCE when the installed
    # snapshot was missing — the estate looked guarded while the tree being
    # committed supplied its own judge. Same law as refusing `import helm`,
    # one level finer: proximity is the check, not importability.
    here = os.path.dirname(os.path.abspath(__file__))
    if not os.path.exists(os.path.join(here, name + ".py")):
        return None
    try:
        mod = __import__(name)
    except Exception:                                         # noqa: BLE001
        return None
    got = os.path.dirname(os.path.abspath(getattr(mod, "__file__", "") or ""))
    return mod if got == here else None


def authority_target():
    """(absolute path, why_invalid, explicitly_targeted), resolved atomically.

    The preferred spelling wins when it names a path; an empty HELM value is
    unset and falls through to the legacy spelling. This differs deliberately
    from generic :func:`home.env`: empty disables several transports, while an
    empty authority path must never erase a non-empty isolation target.

    NOT through $HELM_HOME — the suite sandboxes that, and these names
    describe the fleet, not a helm estate (nevertrack's needles precedent).

    A RELATIVE OVERRIDE IS NOT A PATH, IT IS A QUESTION, and it is REFUSED
    rather than resolved. ``os.path.realpath`` answers it against whatever
    directory the process happens to stand in, and the two processes that
    matter here never stand in the same one: the roster writer resolved it
    against its cwd while the commit hook resolved it against the repository
    it was invoked from, so they updated and read DIFFERENT FILES while both
    believed they were pointed at one authority. Picking either cwd would be
    inventing an answer the operator did not give. The default is absolute by
    construction, so only an explicit override can be ambiguous.
    """
    variable = "HELM_SEAT_NAMES"
    declared = os.environ.get(variable)
    if not declared:
        variable = "MELD_SEAT_NAMES"
        declared = os.environ.get(variable)
    if not declared:
        path = os.path.join(os.path.expanduser("~"), ".helm", "_global",
                            "seat-names.txt")
        return path, None, False
    expanded = os.path.expanduser(declared)
    if not os.path.isabs(expanded):
        return None, ("%s is relative (%s) — it would name a different file "
                      "from every working directory, so the writer and the "
                      "commit hook could not agree on one authority. Set an "
                      "absolute path." % (variable, declared)), True
    return os.path.normpath(expanded), None, True


def authority_path():
    """Stable two-field view of :func:`authority_target`."""
    path, invalid, _targeted = authority_target()
    return path, invalid


# A FAMILY KEY IS NOT A SEAT IDENTITY, SO IT IS HELD BY CONSTRUCTION. The
# harness names a seat after its model or provider family when it knows
# nothing more specific: a claude session joining from no project becomes
# `claude`, and a family seat's first instance takes the family's name. The
# roster, and the authority projected from it, therefore carries these
# words — while in tests they are overwhelmingly provider and family VALUES
# (a `"provider"` field, a homes root key, a catalog key), and arming them
# refuses honest tests for a name no reader would mistake for a seat. The
# authority's own `!` curation already held four of them back for exactly
# that reason; this makes the rule total instead of a list someone has to
# remember to extend. A name that merely CONTAINS a family (`<project>-claude`,
# `claude-2`) is still armed, and a file that names a key both ways is still
# a CONFLICT. Kept a superset of seat_catalog.FAMILIES, homes.ROOTS and
# seats_identity._FAMILIES by tests/test_seatname_guard.py; this module is a
# hook snapshot and cannot import them.
FAMILY_KEYS = frozenset((
    "claude", "codex", "deepseek", "dots3", "ds4flash", "ds4pro", "fable",
    "gemini", "glm", "gpt", "gptoss", "grok", "haiku", "kimi", "llama",
    "mistral", "openrouter", "opus", "opus46", "qwen", "qwen27", "sonnet"))

ARMED = "ARMED"
HELD = "HELD"
CONFLICT = "CONFLICT"
ABSENT = "ABSENT"


class Authority:
    """What the authority file says about one identity. THE single owner.

    TWO QUESTIONS, NEVER ONE, and collapsing them is why this is a type
    rather than a pair of sets. The SCANNER asks ``refuses(name)`` — may this
    identity appear in public-bound source? The INSTALLER and the refresh ask
    ``covers(name)`` — is it accounted for, so it is not re-reported as drift
    forever? A held-back identity is COVERED and does NOT refuse. One
    "is it known" boolean answers both, which silently promotes every
    deliberate exclusion into a publication gate.

    THE KEY IS CASEFOLDED ONCE, HERE. Seat identity folds case-only variants
    together everywhere else in helm, so an authority holding
    ``zz-synthetic-seat`` is speaking about ``ZZ-SYNTHETIC-SEAT`` too;
    comparing raw spellings let the shouted form walk past an armed name.

    ONE IDENTITY SPELLED BOTH WAYS IS CONFLICT, NOT A MERGE. A file naming
    both ``n`` and ``!n`` states two incompatible intentions about one
    identity, and neither of them is reliably the author's — silently
    picking a winner invents consent that was never given. CONFLICT both
    REFUSES and COUNTS AS COVERED: the fail-closed pair, so an ambiguous
    authority tightens the rung instead of quietly disarming it.
    """

    def __init__(self, states):
        self._states = dict(states)

    def state(self, name):
        """ARMED, HELD, CONFLICT, or ABSENT for anything unnamed."""
        return self._states.get(str(name).casefold(), ABSENT)

    def refuses(self, name):
        """May this identity NOT appear in public-bound source?"""
        return self.state(name) in (ARMED, CONFLICT)

    def covers(self, name):
        """Is this identity accounted for — armed, held, or in conflict?"""
        return self.state(name) != ABSENT

    def __contains__(self, name):
        """``name in authority`` IS the refusing question — the one the
        scanner asks. Binding it here means every membership test in the
        scanner inherits casefolding and the conflict rule instead of each
        site re-deciding what "in the list" means, which is how the shouted
        spelling got past in the first place."""
        return self.refuses(name)

    def names(self, *states):
        """Canonical keys in the given states (ARMED when none are named)."""
        want = frozenset(states) or frozenset((ARMED,))
        return frozenset(k for k, v in self._states.items() if v in want)

    # ITERATION AND LENGTH AGREE WITH MEMBERSHIP, deliberately. All three
    # describe the REFUSING set, so a reader who learns what `in` means here
    # cannot be surprised by what a loop or a count covers. `names(HELD)`
    # remains for the caller that genuinely wants the other states.
    def __iter__(self):
        return iter(self.names(ARMED, CONFLICT))

    def __len__(self):
        return len(self.names(ARMED, CONFLICT))


def parse_authority(text):
    """The file's text as an :class:`Authority`. ONE parser, because two
    readers of one file drift.

    ``!name`` is HELD BACK DELIBERATELY: not armed, and not reported as drift
    by the installer's staleness check. Machine-readable rather than prose, so
    a deliberate exclusion is never re-reported as a gap forever. A
    FAMILY_KEYS entry the file arms is read as held too (see FAMILY_KEYS);
    one it names both ways stays a CONFLICT.
    """
    states = {}
    for line in text.splitlines():
        token = line.strip()
        if not token or token.startswith("#"):
            continue
        held = token.startswith("!")
        key = (token[1:].strip() if held else token).casefold()
        if not key:
            continue
        want = HELD if held else ARMED
        prior = states.get(key)
        # A repeated spelling is agreement and stays as it is; the two
        # spellings disagreeing is the conflict, and it is sticky — a third
        # line cannot talk the file back out of having contradicted itself.
        states[key] = want if prior is None else (
            prior if prior == want else CONFLICT)
    for key in FAMILY_KEYS:
        if states.get(key) == ARMED:
            states[key] = HELD
    return Authority(states)


def read_authority(path, honor_stale=True):
    """(:class:`Authority`, why_unreadable) — TOTAL: this never raises.

    `honor_stale=False` reports only whether the BYTES parsed, ignoring the
    stale marker. THE REFRESH MUST USE IT: the marker is precisely what a
    successful refresh clears, so a refresh that treated it as unreadable
    could never heal — the marker would be a one-way trap and the first I/O
    blip would disarm the rung permanently. Caught by its own arm.

    ABSENT and MALFORMED are different answers. Absent is a proven-empty
    authority and the rung says so out loud; malformed is an UNREADABLE
    MEASUREMENT — invalid bytes could be hiding a name — so the rung refuses
    rather than guarding a list it could not fully read. The old version
    caught only OSError, so undecodable bytes escaped as UnicodeDecodeError
    and reached the INSTALLER as a traceback after it had already written
    four hooks and nine snapshots.
    """
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except FileNotFoundError:
        return Authority({}), None                     # absent: proven empty
    except OSError as exc:
        # ONLY FileNotFoundError IS PROVEN ABSENCE. A chmod-000 authority, an
        # I/O error, a directory where a file belongs — those are UNREADABLE,
        # and collapsing them into "absent" turned a locked authority into a
        # silent NO-OP that passed every commit. This is the same tri-state
        # this module argues for the BYTES, applied one level shallower than I
        # first wrote it: at the OPEN, not just at the decode.
        return Authority({}), (
            "the authority at %s could not be read (%s)" % (path, exc))
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        return Authority({}), (
            "the authority at %s is not valid UTF-8 (%s)" % (path, exc))
    authority = parse_authority(text)
    if not honor_stale:
        return authority, None
    try:
        with open(stale_marker(path), "rb") as f:
            raw_marker = f.read()
    except OSError:
        raw_marker = None
    if raw_marker is not None:
        # THE MARKER IS A HINT, ITS PRESENCE IS THE SIGNAL. Reading it as
        # strict UTF-8 let malformed bytes raise UnicodeDecodeError out of a
        # function documented as TOTAL — on the commit path AND on the
        # post-install diagnostic. Decode leniently; a marker that exists at
        # all means the projection could not be written.
        why = raw_marker.decode("utf-8", "replace").strip() or "reason unreadable"
        return authority, (
            "the authority at %s is marked STALE — %s" % (path, why))
    return authority, None


def load_seat_names():
    """(:class:`Authority`, path, why_unreadable).

    Seat identity is casefolded throughout helm (a seat key folds case-only
    variants together deliberately), so case-sensitive membership let
    `ZZ-SYNTHETIC-SEAT` past an authority holding `zz-synthetic-seat`. The
    fold is the Authority's canonical key, so every comparison downstream
    inherits it rather than each caller remembering to fold — and a caller
    that forgets can no longer produce a wrong answer, because there is no
    unfolded spelling left to compare against.
    """
    path, invalid = authority_path()
    if invalid:
        # AN UNRESOLVABLE AUTHORITY IS AN UNREADABLE ONE, and this rung fails
        # closed on unreadable: it cannot prove the commit is clean against a
        # list it could not name, so it refuses rather than passing everything.
        return Authority({}), None, invalid
    authority, why = read_authority(path)
    return authority, path, why


def refresh_authority(live, path=None):
    """Keep the rung's external authority CURRENT. THREE ANSWERS, not two:

      (name, ...)  the names ADDED — the authority is current.
      ()           nothing was added, AND any refusal that was owed is now
                   DURABLE: a stale marker exists, so the rung refuses every
                   tests/ commit until it is repaired.
      None         UNGUARDED. Armed nothing AND could not record the refusal,
                   because the marker lives beside the authority and THE
                   MARKER SHARES A FAULT DOMAIN WITH WHAT IT REPORTS — an
                   unwritable directory defeats os.open(<path>.lock) and the
                   <path>.stale write identically. Nothing downstream will
                   refuse on the caller's behalf in this state.

    COLLAPSING THE LAST TWO IS THE BUG THIS EXISTS TO PREVENT: a silently
    discarded marker read as a successful refusal, and an admitting caller
    published an identity the rung would never challenge.

    WHY THE ROSTER OWNS THIS. The rung is a snapshot and cannot import helm, so
    it reads a FILE — and an install-time drift note cannot keep a file fresh.
    Measured: install with one roster, then add a seat, and every staleness
    check stays green because they all compare HOOK AND SCANNER
    BYTES; the commit path reads only the stale authority, so an exact fixture
    for the new seat commits rc=0. That silently recreates the very premise
    this rung was built for. So the only thing that can change membership —
    the roster write — updates the projection in the same breath.

    ADDITIVE AND NON-DESTRUCTIVE, deliberately:
      · it never REMOVES a name. A departed seat's identity is still
        publication debt, and over-guarding is safe where under-guarding is
        the bug.
      · it never re-arms a name held back behind '!'. Those exclusions are
        curated (a name that is also a provider value would refuse honest
        tests), and a roster write must not silently undo that judgement.

    TOTAL, AND THE "never breaks a roster write" HALF IS NOW QUALIFIED. This
    still never raises. But the None answer is specifically consumed to stop
    an ADMITTING roster write — a join or a rename introducing a casefold-new
    identity — because publishing a seat nothing can refuse is the exact
    outcome this rung exists to prevent. Every other write, including every
    presence beat and metadata refresh on an already-armed seat, persists
    unchanged: they introduce no unguarded name, and breaking them would buy
    an outage for nothing.
    """
    if path is None:
        path, invalid = authority_path()
        if invalid:
            # THE ABSPATH THIS REPLACES WAS THE BUG, not the cure. Resolving a
            # relative override against THIS process's cwd made the writer
            # agree with itself and with nobody else — the reader resolved the
            # same string somewhere different. Refusing to project is the
            # honest outcome; the installer's notes report why.
            return ()
    else:
        path = os.path.abspath(path)
    # ONE LOCKED READ / MERGE / WRITE, on a lock owned by the AUTHORITY PATH.
    # The roster's own lock is per-estate, so two estates could each read,
    # each add one seat, and the last writer erased the other's — and an
    # operator adding `!hold` between the read and the write had it re-armed.
    # The authority is machine-global, so its serialization must be too.
    lock_fd = None
    try:
        # THE PARENT MUST EXIST BEFORE THE LOCK. A fresh estate has no
        # _global/ yet, so opening <path>.lock raised, the refresh returned
        # empty, and it left NEITHER an authority NOR a stale marker — the one
        # state that is silent in both directions. Create it first; a failure
        # here is a real failure and still marks stale below.
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        lock_fd = os.open(path + ".lock", os.O_CREAT | os.O_RDWR, 0o644)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
    except OSError as exc:
        if lock_fd is not None:
            os.close(lock_fd)
        # FAIL CLOSED, AND THE COMMENT ABOVE ALREADY PROMISED THIS WHILE THE
        # CODE DID NOT DO IT. Returning bare left the OLD authority readable
        # and APPARENTLY FRESH: an admitting path then published a new
        # identity to the roster, no marker made the rung refuse, and every
        # later commit carrying that identity passed. A projection that could
        # not RUN is exactly what the stale marker exists to record — the same
        # class as marking an obligation delivered before attempting delivery,
        # which is why the marker goes here and not only after a failed write.
        marked = _mark_stale(path, "authority lock unavailable (%s)" % exc)
        # None IS "UNGUARDED", and it is a THIRD answer rather than a louder
        # second one. () means "nothing was added, and any refusal owed is now
        # DURABLE" — the rung refuses until repaired. None means the arm failed
        # AND the refusal could not be recorded, because THE MARKER SHARES THE
        # FAILED FAULT DOMAIN: an unwritable authority directory defeats
        # os.open(<auth>.lock) and the <auth>.stale write identically. In that
        # state nothing downstream refuses on our behalf, so an ADMITTING
        # caller must decline to publish an identity it cannot guard.
        # Collapsing the two is exactly how a silently-discarded marker read
        # as a successful refusal.
        return () if marked else None
    try:
        return _refresh_locked(path, live)
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)


def _refresh_locked(path, live):
    """The critical section: every read AND the write happen under the lock."""
    authority, malformed = read_authority(path, honor_stale=False)
    if malformed:
        return ()
    # THE REFRESH ASKS `covers`, NEVER `refuses`, and the Authority keeps them
    # apart so this site cannot pick the wrong one by accident. A deliberately
    # held `!Seat-A` does NOT refuse, but it IS covered — it is accounted for
    # and must not be re-appended. Asking the refusing question here appended
    # the name bare and silently RE-ARMED the operator's exclusion, which is
    # the one outcome this file's docstring promises cannot happen. Case folds
    # on both sides because the canonical key is folded, so `!Seat-A` matches
    # a live `seat-a` without this site remembering to fold anything.
    fresh = sorted(n for n in live
                   if isinstance(n, str) and n.strip()
                   and not authority.covers(n))
    if not fresh:
        # CONVERGED IS A SUCCESS, NOT A NO-EVENT. Returning here before
        # clearing the marker made it a one-way trap in the commonest case of
        # all: mark stale once, and if the roster then gains nothing the
        # marker never lifts and every tests/ commit refuses forever. The
        # marker records "the projection could not be written"; an authority
        # that is already current is the projection being correct.
        _clear_stale(path)
        return ()
    try:
        parent = os.path.dirname(path)
        if parent:                    # dirname('auth.txt') is '' and makedirs('') raises
            os.makedirs(parent, exist_ok=True)
        existing = ""
        try:
            with open(path, "rb") as f:
                existing = f.read().decode("utf-8")
        except OSError:
            existing = ("# helm seat-name guard authority — maintained by the "
                        "roster write.\n# '!name' holds a name back "
                        "deliberately; that judgement is never undone here.\n")
        if existing and not existing.endswith("\n"):
            existing += "\n"
        tmp = path + ".tmp-%d" % os.getpid()
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(existing + "\n".join(fresh) + "\n")
        os.replace(tmp, path)                      # atomic for every reader
    except OSError as exc:
        # DURABLE, NOT SWALLOWED. A best-effort push that fails silently
        # recreates the stale authority this exists to prevent — the roster
        # write must not fail, but the failure may not vanish either
        # The marker is the second freshness owner: the rung
        # refuses while it exists and the installer reports it.
        # SAME TRI-STATE AS THE LOCK PATH. A write that failed on an
        # unwritable directory cannot record its own refusal there either, so
        # None ("UNGUARDED") travels up and the admitting caller declines to
        # publish. () still means the refusal is durable.
        marked = _mark_stale(path, "a roster write could not refresh it (%s)"
                             % exc)
        return () if marked else None
    _clear_stale(path)
    return tuple(fresh)


def stale_marker(path):
    return path + ".stale"


def _mark_stale(path, why):
    """True when the refusal became DURABLE, False when it could not.

    THE MARKER SHARES A FAULT DOMAIN WITH WHAT IT REPORTS, which is why the
    answer must be returned rather than swallowed. The marker lives beside the
    authority, so the very fault it records — an unwritable directory — is
    also the fault that stops it being written: os.open(<auth>.lock) fails
    EACCES, this then fails EACCES on <auth>.stale, and a silent `pass` left
    the old authority reading FRESH while an admitting caller published an
    identity nothing would refuse. Returning the outcome is what lets that
    caller decline to publish an identity it cannot guard.
    """
    try:
        with open(stale_marker(path), "w", encoding="utf-8") as f:
            f.write(why + "\n")
        return True
    except OSError:
        return False


def _clear_stale(path):
    try:
        os.remove(stale_marker(path))
    except OSError:
        pass


def committing_root():
    """The worktree this commit is FOR, asked of GIT and nothing else.

    THERE IS NO ENV OVERRIDE, deliberately. The first version honoured
    $HELM_SEATNAME_REPO, and an inherited value naming a DIFFERENT VALID repo
    was measured to make the rung scan a clean tree and pass — fail-closed
    on an invalid path proves nothing about a wrong-but-valid one, which is the
    likelier accident. A guard that can be re-pointed by ambient environment is
    a guard with an undocumented off switch. Tests drive it by cwd, or call
    offenders(root, names) directly.
    """
    done = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                          capture_output=True, timeout=60)
    if done.returncode != 0:
        return None, "not inside a git work tree"
    return done.stdout.decode("utf-8", "replace").strip(), None


def escape_lines(source):
    """Line numbers carrying a REASONED escape, from real COMMENT tokens.

    vacuous_assertion._comments is the same shape but binds ITS token, so this
    is a deliberate small duplicate rather than a wrong reuse. The reason is
    REQUIRED: a bare noqa is an exception the next reader cannot evaluate.

    AN ESCAPE COVERS ITS WHOLE LOGICAL LINE, not the physical line it sits on.
    A physical line ending in a backslash CANNOT carry a trailing comment —
    that is a syntax error — so on a continued statement the remedy this
    guard prescribes is unwritable at the very place it points, and the only
    markable line belongs to the same statement rather than the same physical
    line. Measured on task/2098: three refused commits and two re-cuts, with
    the author annotating correctly each time and the guard declining to see
    it. A remedy that cannot be written is not a remedy.

    Python already draws this boundary: tokenize emits NEWLINE at the end of
    a LOGICAL line and NL inside a continuation, so the grouping here is the
    language's own and not a heuristic about backslashes.
    """
    out, code, reasoned = set(), None, False
    try:
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type == tokenize.NEWLINE:
                if reasoned and code is not None:
                    out.update(range(code, tok.end[0] + 1))
                code, reasoned = None, False
                continue
            if tok.type in (tokenize.NL, tokenize.INDENT, tokenize.DEDENT):
                continue
            if tok.type == tokenize.COMMENT:
                if _ESCAPE.match(tok.string):
                    # ITS OWN LINE ALWAYS, and the STATEMENT only when one has
                    # already begun on this logical line. That condition is
                    # what keeps a standalone remark from escaping the code
                    # under it: a comment-only line reaches here with no code
                    # token yet, so it never marks a statement as reasoned.
                    out.add(tok.start[0])
                    if code is not None:
                        reasoned = True
                continue
            if code is None:
                code = tok.start[0]
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return None                       # unreadable -> caller fails closed
    return out


def _folded(node):
    """The literal string a node deterministically yields, or None.

    Adjacent literals are folded by the parser; `+` of literals is not, and
    that concatenation was a measured bypass.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if not (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add)):
        return None
    # ITERATIVE, NOT RECURSIVE. A pre-existing 1000-literal concatenation is
    # a left-leaning BinOp chain 1000 deep, and recursing it blew the stack —
    # a RecursionError out of a pre-commit rung, on a file nobody was even
    # changing. Flatten the chain instead.
    parts, stack = [], [node]
    while stack:
        cur = stack.pop()
        if isinstance(cur, ast.Constant) and isinstance(cur.value, str):
            parts.append(cur.value)
        elif isinstance(cur, ast.BinOp) and isinstance(cur.op, ast.Add):
            stack.append(cur.right)
            stack.append(cur.left)
        else:
            return None
    out, seen = [], []
    for piece in parts:
        out.append(piece)
    return "".join(out) if out else None


def _split_yield(node):
    """The pieces a literal `.split(<const>)` deterministically yields.

    INSPECT THE OPERATION, DO NOT GUESS THE SEPARATOR. The first version
    enumerated likely delimiters, and `"seat-A/seat-B".split("/")` walked past
    because a slash was not in the list. Any separator enumeration recreates
    that hole for the next character nobody thought of; the split CALL states
    its own separator, so read it.
    """
    if not isinstance(node, ast.Call):
        return None
    fn = node.func
    if not isinstance(fn, ast.Attribute) or fn.attr not in ("split", "rsplit"):
        return None
    subject = _folded(fn.value)
    if subject is None:
        return None
    # MAXSPLIT IS PART OF THE OPERATION, and ignoring it INVENTED a collection
    # that the code never produces: `"a,b".split(",", 0)` yields ['a,b'], one
    # element, yet computing subject.split(sep) gave two real seats and the
    # rung refused an honest line. Read every argument the call actually
    # passes, including the keyword spellings.
    kw = {k.arg: k.value for k in node.keywords if k.arg}
    sep_node = node.args[0] if node.args else kw.get("sep")
    max_node = (node.args[1] if len(node.args) > 1 else kw.get("maxsplit"))
    maxsplit = -1
    if max_node is not None:
        if not (isinstance(max_node, ast.Constant)
                and isinstance(max_node.value, int)):
            return None                     # not deterministic; do not guess
        maxsplit = max_node.value
    rsplit = fn.attr == "rsplit"
    # AN EXPLICIT `None` IS A SEPARATOR CHOICE, not an unresolvable node, and
    # collapsing the two made `"a b".split(None)` — whitespace splitting,
    # spelled out — walk straight past. Absent argument and literal None mean
    # the same thing to str.split; everything else must resolve or be declined.
    explicit_none = (isinstance(sep_node, ast.Constant)
                     and sep_node.value is None)
    if sep_node is None or explicit_none:
        return subject.rsplit(None, maxsplit) if rsplit else \
            subject.split(None, maxsplit)
    sep = _folded(sep_node)
    if sep is None or sep == "":
        return None
    return subject.rsplit(sep, maxsplit) if rsplit else subject.split(sep, maxsplit)


def _all_names(pieces, names):
    parts = [p for p in pieces if p != ""]
    return len(parts) >= 2 and all(p.casefold() in names for p in parts)


def classify(value, names):
    """'value' when this literal IS an identity, compared CASEFOLDED."""
    return "value" if value.casefold() in names else None


# WHAT THIS RUNG REFUSES, AND WHAT IT MERELY REPORTS.
#
# THE GUARANTEE IS SMALL AND TOTAL: no armed identity may appear, case
# -insensitively, as an exact string scalar on an added line — or as a
# CONCATENATION of such literals, which is the ONLY composed form `_folded`
# decides. That is a LEXICAL DISCLOSURE claim over an EXHAUSTIVE operator
# list, and within that list it is fully decidable.
#
# %-FORMATTING AND CONSTANT F-STRINGS ARE NOT IN IT, deliberately and
# stated: both are decided in the source, both can produce an armed identity,
# and neither is folded here. Naming them as a gap is honest; naming them as
# covered — which an earlier draft of this comment did, by promising "all
# constant operands" — is the wider-promise-than-code failure that this whole
# rung exists to have stopped making.
#
# THIS RUNG DOES NOT PROVE ARBITRARY RUNTIME CONSTRUCTION, and nothing here
# should be read as claiming it does. A value assembled through a local alias,
# a positional call this list does not model, a nonconstant template, or any
# other partial provenance can carry an identity past it. Two earlier designs
# tried to decide "could this source produce an identity" in general; each
# grew a bespoke dataflow engine that was wrong in a new way, and the honest
# end of that road is either an incomplete analyzer that lies or fail-closed
# UNKNOWNs on ordinary names. A NARROW PROMISE KEPT BEATS A BROAD ONE IMPLIED.
#
# So the constructed-value and sink heuristics still RUN and still print —
# they are genuinely useful signal and they found real debt — but they are
# ADVISORY and cannot gate a publication. If broader prevention is wanted, it
# belongs at a constrained test-identity API or an export boundary, not in a
# pre-commit analyzer guessing at what a program might compute.
_REFUSING_KINDS = frozenset(("value",))


# IDENTITY-BEARING SINKS. A literal reaching one of these IS a fixture value
# for a seat, whatever expression carries it. Scoping the literal-part rule to
# these is what keeps it from firing on ordinary prose: `MSG = "seat-red" + "
# pane died"` is not at a sink and stays untouched.
_SINKS = frozenset((
    "seat", "seats", "recipient", "recipients", "to", "sender", "from_seat",
    "owner", "holder", "author", "reviewer", "seat_name", "who", "chat_name",
    "actor", "assignee", "HELM_CHAT_NAME"))


def sink_expressions(tree):
    """[(sink_name, expression_node)] — every expression handed to an identity
    boundary: a keyword argument or a dict entry keyed by a sink name."""
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            out += [(k.arg, k.value) for k in node.keywords if k.arg in _SINKS]
        elif isinstance(node, ast.Dict):
            for key, val in zip(node.keys, node.values):
                if isinstance(key, ast.Constant) and key.value in _SINKS:
                    out.append((key.value, val))
    return out


def literal_parts(node):
    """Every string literal inside an expression, including f-string parts.

    PUBLICATION DEBT LIVES IN LITERALS, because the source is what gets
    published. An expression with no string literal — seats=list(seats),
    seat=launch.stable_seat() — resolves at RUNTIME and can never introduce a
    hardcoded identity, whatever it evaluates to. That is why this rule needs
    no evaluator and carries no false-positive budget: it asks what the SOURCE
    contains, not what the program will compute.
    """
    out = []
    for n in ast.walk(node):
        if isinstance(n, ast.JoinedStr):
            # AN F-STRING IS A TEMPLATE, and ast SPLITS it into Constant parts
            # plus FormattedValue holes — so its literal parts individually
            # carry no placeholder and the template rule cannot see the shape.
            # Reassemble it with an explicit placeholder so `f"seat-{n}"` is
            # judged the same as `"seat-%d" % n`. Metamorphic equivalence:
            # same product, same verdict, different spelling.
            out.append("".join(
                part.value if isinstance(part, ast.Constant)
                and isinstance(part.value, str) else "{}"
                for part in n.values))
        elif isinstance(n, ast.Constant) and isinstance(n.value, str):
            out.append(n.value)
    return out


_PLACEHOLDER = re.compile(r"%[-#0-9. +]*[sdrfgxo]|\{[^{}]*\}")


def template_reaches(text, names):
    """A name a FORMAT TEMPLATE could produce, or None.

    THE ASSEMBLED-IDENTITY CASE: `seat="zz-seat-%d" % n` contains no literal
    that IS an identity, yet yields one whenever the argument supplies the
    matching tail. Treat each placeholder as a wildcard and ask whether any
    real identity matches the shape — `family-%d` reaches nothing and passes,
    while a template whose fixed parts bracket a real name refuses.

    This is why the literal rule is about what the source can PRODUCE rather
    than what it spells: the parts are innocent and the product is not.
    """
    if not _PLACEHOLDER.search(text):
        return None
    pattern = "".join("[^\\s]*" if _PLACEHOLDER.fullmatch(part) else re.escape(part)
                      for part in _PLACEHOLDER.split(text)
                      if part is not None)
    # re.split drops the separators, so rebuild with explicit wildcards.
    pattern = _PLACEHOLDER.sub("\x00", text)
    pattern = "".join("[^\\s]*" if ch == "\x00" else re.escape(ch)
                      for ch in pattern)
    try:
        rx = re.compile("^" + pattern + "$")
    except re.error:
        return None
    for name in names:
        if rx.match(name):
            return name
    return None


def sink_findings(tree, names, escaped, ranges):
    """[(lineno, value, kind)] for literals reaching an identity boundary.

    Runs ONLY at sinks, which is what makes leaf-level inspection safe: at a
    sink a literal carrying an identity IS the fixture value, so there is no
    prose to protect. Away from a sink the maximal-expression rule still owns
    the judgement and prose keeps passing.
    """
    hits, seen = [], set()
    for _sink, expr in sink_expressions(tree):
        first, last = _span(expr)
        if first is None or not _overlaps(first, last, ranges):
            continue
        if any(l in escaped for l in range(first, (last or first) + 1)):
            continue
        for text in literal_parts(expr):
            kind = classify(text, names)
            pieces = [t for t in _DELIM.split(text.strip()) if t]
            if kind is None and _all_names(pieces, names):
                kind, text = "constructed", ",".join(pieces)
            if kind is None:
                reached = template_reaches(text, names)
                if reached:
                    kind, text = "constructed", reached
            if kind and (first, text) not in seen:
                seen.add((first, text))
                hits.append((first, text, kind))
    return hits


def _span(node):
    """(first, last) source lines a node occupies.

    end_lineno is load-bearing: adjacent literals fold onto the FIRST one's
    line, so a newly added second literal hides behind an unchanged first if
    only node.lineno is tested against the added ranges.
    """
    return getattr(node, "lineno", None), getattr(node, "end_lineno", None)


def scan_python(source, names, ranges):
    """[(lineno, value, kind)] for ADDED lines only. Raises on unparseable."""
    tree = ast.parse(source)
    escaped = escape_lines(source)
    if escaped is None:
        raise SyntaxError("comment tokens unreadable")
    # CLASSIFY MAXIMAL EXPRESSIONS ONCE. Walking every node classified the
    # PARENT and its CHILDREN, so `MSG = "seat-red" + " pane died"` refused on
    # the child Constant even though the deterministic whole expression is
    # prose, and a two-seat concatenation reported three findings for one
    # line. What a line DOES is the whole expression, not its fragments.
    candidates, covered = [], set()
    for node in ast.walk(tree):
        pieces = _split_yield(node)
        value = None if pieces is not None else _folded(node)
        if pieces is None and value is None:
            continue
        candidates.append((node, pieces, value))
        for child in ast.walk(node):
            if child is not node:
                covered.add(id(child))
    hits, seen = [], set()
    for node, pieces, value in candidates:
        if id(node) in covered:
            continue                       # a fragment of a larger expression
        first, last = _span(node)
        if first is None or not _overlaps(first, last, ranges):
            continue
        if any(l in escaped for l in range(first, (last or first) + 1)):
            continue
        if pieces is not None:
            if _all_names(pieces, names) and (first, tuple(pieces)) not in seen:
                seen.add((first, tuple(pieces)))
                hits.append((first, ",".join(pieces), "constructed"))
        elif classify(value, names) and (first, value) not in seen:
            seen.add((first, value))
            hits.append((first, value, "value"))
    for hit in sink_findings(tree, names, escaped, ranges):
        if (hit[0], hit[1]) not in seen:
            seen.add((hit[0], hit[1]))
            hits.append(hit)
    return hits


def _overlaps(first, last, ranges):
    """ranges None means EVERY line is this commit's (new path, binary, or a
    venue change); otherwise any overlap with the node's SPAN counts."""
    if ranges is None:
        return True
    last = last or first
    return any(lo <= last and first <= hi for lo, hi in ranges)


# Structured data under tests/ is public-bound too, and a quoted scalar there
# IS a value. PROSE FORMATS ARE NOT HELD, and that is stated rather than
# guessed at: a byte matcher cannot tell `The "seat-A" pane died` in a
# markdown note from a fixture, which is exactly why the Python path parses.
# Promising coverage the implementation cannot honour is worse than a narrow
# promise kept, so the boundary is named here and pinned by an arm.
# MEASURED, NOT IMAGINED. An inventory of this repo's non-Python tests/ assets
# found .js 4 files, .txt 2, .json 1, .md 1 — and every real-seat occurrence in
# ALL of them is prose, narration or a captured transcript, not one a fixture
# value. So .json is the only non-Python format scanned: it has an authoritative
# stdlib parser and its scalars ARE values. .js carries the most names but has
# no honest parser, so it is out of scope and belongs to the export sweep
# rather than to a regex pretending to be one. An earlier draft of this line
# listed .yaml/.toml/.ini/.cfg — four formats that do not exist in this tree.
_STRUCTURED = (".json",)


def _quoted_lines(text, value):
    """Every 1-based line where `value` appears as a QUOTED scalar, or [] when
    it cannot be located.

    EMPTY MEANS UNLOCATABLE, NOT ABSENT — the caller must treat it as an
    unknown position rather than as line 1. json.loads discards positions, so
    the parser tells us WHICH values are identities and this tells us WHERE
    they are written; a value the raw text spells differently (an escape, a
    surrogate pair) is genuinely unlocatable and says so.
    """
    pattern = r"""(['"])%s\1""" % re.escape(value)
    return [text.count("\n", 0, m.start()) + 1
            for m in re.finditer(pattern, text)]


def scan_structured(text, names):
    """Quoted scalars in a structured fixture, WITH REAL LINE NUMBERS.

    Line 0 is UNKNOWN POSITION, and the staged-range filter must not exclude
    it. Every hit here used to be reported at line 0 and then filtered against
    a hardcoded line 1, so a whole file's findings stood or fell on whether
    its FIRST line happened to be in the diff: adding an identity on line 3
    passed, and touching line 1 blamed unchanged values elsewhere. The values
    come from the parser — that is what keeps prose distinguishable — and the
    positions come from locating those exact values in the raw text.
    """
    hits = []
    try:
        import json
        doc = json.loads(text)
    except Exception:                                         # noqa: BLE001
        doc = None
    if doc is not None:
        # COUNTED, NOT DEDUPED, and that is the whole of the fix below.
        counts, stack = {}, [doc]
        while stack:
            cur = stack.pop()
            if isinstance(cur, str):
                if cur in names:
                    counts[cur] = counts.get(cur, 0) + 1
            elif isinstance(cur, dict):
                stack.extend(list(cur.keys()) + list(cur.values()))
            elif isinstance(cur, list):
                stack.extend(cur)
        for value, parsed in counts.items():
            lines = _quoted_lines(text, value)
            # EVERY OCCURRENCE, deliberately: two lines carrying one identity
            # are two pieces of publication debt, and reporting one of them
            # would let the other survive the fix.
            for line in lines:
                hits.append((line, value, "value"))
            # EVERY PARSED OCCURRENCE OWES A SPAN, and a shortfall is UNKNOWN.
            #
            # _quoted_lines searches the RAW spelling, so an occurrence written
            # with an escape ("...-2") decodes to this value and matches
            # NOTHING. The previous shape deduped values and fell back to
            # line 0 only when NO line matched — so an ADDED escaped scalar
            # whose value also appears on an OLD unchanged line located that
            # old line, the added-range filter dropped it, and the new
            # identity passed. Absence-of-any is not absence-of-this-one, and
            # only the weaker test was being run.
            for _ in range(max(0, parsed - len(lines))):
                hits.append((0, value, "value"))
        return hits
    for match in re.finditer(r"""(['"])([^'"]{0,200}?)\1""", text):
        if match.group(2) in names:
            hits.append((text.count("\n", 0, match.start()) + 1,
                         match.group(2), "value"))
    return hits


def _under_tests(path):
    """The boundary the docstring ADVERTISES: anything under a tests/ dir.

    The old rung coded `tests/**.py` while promising `tests/`, so a staged
    tests/fixture.json walked past a guard that claimed to hold it.
    """
    # NO BACKSLASH TRANSLATION. On POSIX a backslash is an ordinary filename
    # character, so folding it to a separator classified the literal file
    # `outside\\tests\\x.py` as living under tests/. Git paths are POSIX.
    parts = path.split("/")
    return "tests" in parts[:-1]


def index_tree(root):
    """The staged tree object id, or None. ONE observation of the index.

    The scan reaches git several times — entries, blobs, ranges — and each is
    its own subprocess seeing its own index. A concurrent `git add` during
    pre-commit was reproduced: the commit carried the later bytes while the
    guard had scanned the earlier clean blob. Capturing this before and after
    turns that race into a REFUSAL instead of a silent pass.

    WHAT IT CANNOT SEE, stated because a guard owes its limit: an index that is
    changed and CHANGED BACK inside the scan window returns the SAME tree id at
    both endpoints, so an A->B->A cycle passes. Measured, not assumed.

    THE FIX IS NOT AVAILABLE WITHOUT GIVING UP THE HARDENED SEAMS. Deriving
    entries, blobs and ranges from ONE captured generation would close it, but
    conflict_marker.staged_entries, conflict_marker._added_ranges and
    nevertrack._payload all take a repo root and read the LIVE index — none
    accepts a tree — so a captured generation means hand-rolling those three
    readers, and they are exactly where seven measured bypasses were closed
    (textconv, NUL-safe paths, -M renames, the binary fallback). Re-buying
    those edges from zero to close a narrower window is the worse trade.

    A CHEAPER DETECTOR WAS TRIED AND REJECTED ON EVIDENCE: the .git/index FILE
    changes on every write, so pairing the tree id with its stat would see the
    cycle. It also moves when a concurrent `git status` merely REFRESHES the
    index with nothing staged — measured — so it false-refuses on the
    commonest concurrent action during a pre-commit hook. A guard that fires
    on `git status` is a guard that gets switched off.

    So the window is KNOWN, NARROW and DOCUMENTED rather than silently
    present: it needs a write, a counter-write and a restore, all inside one
    scan, on the same index.
    """
    done = subprocess.run(["git", "-C", root, "write-tree"],
                          capture_output=True, timeout=60)
    if done.returncode != 0:
        return None
    return done.stdout.decode("ascii", "replace").strip()


def _entered_the_boundary(status, old, rel):
    """True when this entry MOVED into tests/ from outside it.

    Renames are why the FIX bar named them: a blob that was private yesterday
    is public today with no line of it changed.
    """
    code = status.decode("ascii", "replace") if isinstance(status, bytes) else status
    if not code or code[0] not in ("R", "C"):
        return False
    if old is None:
        return False
    was = old.decode("utf-8", "replace") if isinstance(old, bytes) else old
    return _under_tests(rel) and not _under_tests(was)


def offenders(root, names):
    """(found, refusal_reason, missing_seam).

    THREE outcomes, not two: a finding, an unreadable MEASUREMENT (refuse), and
    a broken INSTALL (warn loudly, stand down — the v3 rung law).
    """
    cm, nt = _sibling("conflict_marker"), _sibling("nevertrack")
    if cm is None or nt is None:
        # A BROKEN INSTALL IS NOT A BROKEN MEASUREMENT, and conflating them is
        # how this rung broke two sibling rungs' tests. Fail-CLOSED is the law
        # for staged state the commit CARRIES. A missing snapshot is an
        # ESTATE-INTEGRITY problem, and the v3 rung law is explicit that each
        # rung's absence isolates to itself — otherwise deleting one scanner
        # blocks every commit in every room, and one rung's absence becomes
        # another rung's refusal. So this warns LOUDLY and stands down, exactly
        # as every sibling does for its own missing snapshot.
        missing = ", ".join(n for n, m in (("conflict_marker", cm),
                                           ("nevertrack", nt)) if m is None)
        return None, None, missing
    snapshot = index_tree(root)
    if snapshot is None:
        return None, "the staged index could not be captured", None
    try:
        entries = cm.staged_entries(root)
    except Exception as exc:                                  # noqa: BLE001
        return None, "the staged set could not be read (%s)" % exc, None
    found = []
    for status, old, path in entries:
        # os.fsdecode round-trips through surrogateescape; decode("replace")
        # is LOSSY, so a legal tests/bad-\xff.py became a different path and
        # every commit refused it as unreadable.
        rel = os.fsdecode(path) if isinstance(path, bytes) else path
        if not _under_tests(rel):
            continue
        try:
            # EACH SEAM IN ITS OWN CURRENCY: conflict_marker keeps paths as
            # literal BYTES (that is the NUL-safety), nevertrack's blob reader
            # takes the decoded name. Handing either the other's type is a
            # TypeError, and under fail-closed a TypeError becomes a refusal
            # that looks exactly like a real finding.
            payload = nt._payload(root, rel)
        except Exception as exc:                              # noqa: BLE001
            return None, "staged blob unreadable for %s (%s)" % (rel, exc), None
        if payload is None:
            return None, ("staged entry %s is not a readable blob — an "
                          "uninspectable path may not read as a clean one"
                          % rel), None
        # The added-side is deliberately UNUSED: it is obtained without
        # --no-textconv, so a display filter could render `seat-a` over a real
        # identity. Only the BLOB is honest, and conflict_marker's ranges do
        # the attribution. Named rather than silently discarded so the next
        # reader does not "restore" it. (The extra diff _payload computes for
        # it is a measured cost the meld may trade for _staged_blob; that
        # swaps a borrowed hardened seam and is not a tidy-up.)
        full, _added_side_unused = payload
        try:
            ranges = cm._added_ranges(root, status, old, path)
        except Exception as exc:                              # noqa: BLE001
            return None, "added-line ranges unreadable for %s (%s)" % (rel, exc), None
        if _entered_the_boundary(status, old, rel):
            # A PURE RENAME ADDS NO TEXT AND STILL PUBLISHES. `git mv
            # private/x.py tests/test_x.py` moves an unchanged blob across the
            # public boundary: the added ranges are correctly empty, and every
            # line is nonetheless newly inside tests/ (measured rc=0).
            # Venue change means the WHOLE blob is new HERE. An
            # inside->inside rename is untouched — it published already.
            ranges = None
        # THE STAGED BLOB IS THE ONLY HONEST TEXT. nevertrack's added-side is
        # obtained WITHOUT --no-textconv, so a textconv filter could render
        # `seat-a` while the blob carried a real identity; only
        # conflict_marker's RANGES are hardened. So: scan the blob, attribute
        # by the hardened ranges.
        if rel.endswith(".py"):
            try:
                hits = scan_python(full.decode("utf-8"), names, ranges)
            except (SyntaxError, ValueError, UnicodeDecodeError) as exc:
                # FAIL CLOSED. Falling back to a weaker scan silently lost the
                # constructed-value checks on exactly the input least likely
                # to be innocent.
                return None, ("staged Python at %s could not be parsed (%s) — "
                              "a program this rung cannot read may not pass it"
                              % (rel, exc)), None
        elif rel.lower().endswith(_STRUCTURED):
            try:
                hits = scan_structured(full.decode("utf-8"), names)
            except UnicodeDecodeError as exc:
                return None, ("staged JSON at %s is not decodable (%s)"
                              % (rel, exc)), None
            # PER-HIT, AT ITS OWN LINE. This filter passed a hardcoded 1, so
            # the predicate never looked at the hit at all: every finding in a
            # file stood or fell on whether its FIRST line was in the diff.
            # Adding an identity on line 3 of an existing fixture returned
            # rc=0, and touching line 1 blamed values the commit never moved.
            # Line 0 is UNKNOWN POSITION and is KEPT: a hit this rung could
            # not locate is not a hit it may quietly drop.
            hits = [h for h in hits
                    if h[0] == 0 or _overlaps(h[0], None, ranges)]
        else:
            # OUT OF SCOPE BY MEASUREMENT, not by omission — see the module
            # contract. Every non-Python occurrence in this tree today is
            # prose, narration or a captured transcript, and no honest stdlib
            # parser exists for the formats that carry them.
            hits = []
        for line, value, kind in hits:
            found.append((rel, line, value, kind))
    if index_tree(root) != snapshot:
        return None, ("the staged index CHANGED while this rung was reading it "
                      "— the commit would carry bytes the scan never saw"), None
    return found, None, None


def main(argv):
    if "--staged" not in argv:
        print("usage: seatname_guard.py --staged", file=sys.stderr)
        return 2
    if os.environ.get("HELM_SEATNAME_SKIP") == "1":
        print("[helm seat-name] SKIPPED — HELM_SEATNAME_SKIP=1", file=sys.stderr)
        return 0
    root, why = committing_root()
    if root is None:
        print("[helm seat-name] REFUSED: %s" % why, file=sys.stderr)
        return 1
    names, path, why = load_seat_names()
    if why:
        print("[helm seat-name] REFUSED: %s.\n  This rung FAILS CLOSED on an "
              "authority it cannot fully read — undecodable bytes could be "
              "hiding a name it is supposed to catch. Repair the file, or skip "
              "this one commit with HELM_SEATNAME_SKIP=1." % why,
              file=sys.stderr)
        return 1
    if not names:
        print("[helm seat-name] NO-OP: no seat names configured at %s — this "
              "rung checked nothing. Populate it (one name per line) to arm "
              "the guard." % path, file=sys.stderr)
        return 0
    found, refusal, missing_seam = offenders(root, names)
    if missing_seam:
        # LOUD, and rc=0. Same shape and same wording as every sibling rung's
        # own missing-snapshot notice: this is an install fault, not a finding
        # about the commit, and the v3 law keeps one rung's absence from
        # becoming another rung's refusal.
        print("[helm seat-name] WARNING: hardened scanner seam(s) missing: %s —"
              % missing_seam, file=sys.stderr)
        print("[helm seat-name] public-bound seat-identity scan SKIPPED; "
              "reinstall: helm work install-guard --apply", file=sys.stderr)
        return 0
    if refusal:
        print("[helm seat-name] REFUSED: %s.\n  This rung FAILS CLOSED: an "
              "unreadable measurement is not evidence of a clean tree. Fix the "
              "cause, or skip this one commit with HELM_SEATNAME_SKIP=1."
              % refusal, file=sys.stderr)
        return 1
    refusing = [f for f in found if f[3] in _REFUSING_KINDS]
    advisory = [f for f in found if f[3] not in _REFUSING_KINDS]
    if advisory:
        # REPORTED, NEVER A GATE — see _REFUSING_KINDS. These are the
        # constructed-value and sink heuristics: real signal, worth a human's
        # eye, and NOT something this rung can prove. Blocking a commit on a
        # guess about what a program might compute is how a guard earns a
        # reputation that gets it switched off.
        print("[helm seat-name] ADVISORY (not blocking): %d staged test "
              "line(s) may ASSEMBLE a seat identity at run time. This rung "
              "cannot prove it either way — read them:" % len(advisory),
              file=sys.stderr)
        for rel, line, value, kind in advisory[:12]:
            where = "%s:%d" % (rel, line) if line else rel
            note = (" (constructed — yields seat values)"
                    if kind == "constructed" else " (%s)" % kind)
            print("    %-44s %r%s" % (where, value, note), file=sys.stderr)
        if len(advisory) > 12:
            print("    ... and %d more" % (len(advisory) - 12), file=sys.stderr)
    if not refusing:
        return 0
    print("[helm seat-name] REFUSED: %d staged test line(s) carry a REAL seat "
          "identity AS A LITERAL. tests/ is public-bound, and a real identity "
          "there is publication debt that compounds with every copy:"
          % len(refusing), file=sys.stderr)
    for rel, line, value, _kind in refusing[:12]:
        where = "%s:%d" % (rel, line) if line else rel
        print("    %-44s %r" % (where, value), file=sys.stderr)
    if len(refusing) > 12:
        print("    ... and %d more" % (len(refusing) - 12), file=sys.stderr)
    print("  FIX: use the house convention — %s. Build fixtures from those "
          "DIRECTLY; never assemble one by splitting or concatenating real "
          "names, which yields the same value past a weaker check.\n"
          # THE REMEDY NAMES THE BOUNDARY THE CODE ACTUALLY HAS. `escape_lines`
          # groups by tokenize's LOGICAL line, so this may not say "the same
          # line": a physical line ending in a backslash cannot carry a
          # comment at all, and telling an author to mark one is prescribing
          # a syntax error. Nor may it say "the same statement" — a compound
          # statement spans several logical lines and NEWLINE resets the
          # escape at the header, so a marker in the body does not reach it.
          "  Prose that NAMES a seat as its subject is legitimate and needs no "
          "rewrite — mark it anywhere on the same LOGICAL line:\n"
          "      # noqa: SEAT_NAME — <reason>\n"
          "  A line ending in a backslash cannot carry a comment, so on a "
          "continued statement put the marker on that statement's last "
          "comment-capable line — the header's own final line, NOT its "
          "indented body, which is a logical line of its own.\n"
          "  roster: %s | skip this commit: HELM_SEATNAME_SKIP=1"
          % (", ".join(_CONVENTION), path), file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
