#!/usr/bin/env python3
"""ONE per-process temp HELM_HOME, created lazily and removed at exit.

The class this closes (/tmp hitting 100% INODE exhaustion —
1,048,575 of 1,048,576 used with 29G of space still free, so every space-based
check read healthy while every agent's tool calls failed ENOSPC):

    os.environ.setdefault("HELM_HOME", tempfile.mkdtemp(prefix=...))

leaks a directory per module per run, TWICE over:
  1. Python evaluates arguments EAGERLY, so mkdtemp() runs and creates the
     directory even when setdefault discards its value because HELM_HOME is
     already set. 31 test modules did this; the first one wins and the other
     30 each orphan a fresh directory on every single run.
  2. Nothing ever removed them — module-scope has no tearDown.

Use `home()` instead: it only creates when the var is genuinely unset, and it
registers the removal with atexit so a normal process exit takes the directory
with it.
"""
import atexit
import contextlib
import json
import os
import shutil
import subprocess
import tempfile


def home(prefix="helm-test-home-", var="HELM_HOME"):
    """The process's temp home for `var`, created ONCE and cleaned at exit.
    Returns the existing value untouched when the caller (or a parent
    harness) already set one — without creating a directory to throw away."""
    current = os.environ.get(var)
    if current:
        return current
    d = tempfile.mkdtemp(prefix=prefix)
    os.environ[var] = d
    atexit.register(shutil.rmtree, d, ignore_errors=True)
    return d


# ORDER-PROOFING THE FREEZE-AT-IMPORT MODULES (helm.configs computes its
# CWD_ROOTS when first imported): tests/conftest.py plants HELM_CONFIG_ROOTS
# before any import, but ONLY pytest loads conftest — under plain unittest
# discovery the guarantee vanishes, and whichever module first drags in
# helm.configs (helm.hooks does, transitively) freezes the roots on the REAL
# estate. test_configs then plants fixtures nowhere the frozen roots look and
# fails, but only in orders that include such a module — measured 2026-07-28
# when a new test file imported helm.hooks and three configs tests failed
# under unittest while pytest stayed green. Every test module imports THIS
# module before any helm.*, so planting the same guarantee here makes the
# freeze land on a tmp root no matter who imports what first, under either
# runner. conftest still wins when it ran first: home() honours an existing
# value untouched.
home(prefix="helm-test-cfgroots-", var="HELM_CONFIG_ROOTS")

# EXPLICIT ROOM FOR EVERY TEST PROCESS: chat.post's room default now DERIVES
# (env seam, then cwd project) instead of hardcoding "main" — the 2026-07-29
# room-partition class fix. A test process's cwd is the REPO, so a bare
# default-relying post would derive the repo's room and 60 hermetic tests
# asserting #main would fail for a reason unrelated to what they test. Tests
# therefore declare their room EXPLICITLY through the same env seam every
# launched seat uses (launch.sh sets HELM_CHAT_ROOM) — the old behavior, now
# stated instead of accidental. Tests OF the derivation itself mock
# _default_post_room and are untouched by this.
os.environ.setdefault("HELM_CHAT_ROOM", "main")


def helm_tree(case, repo):
    """Say out loud that this fixture repo stands in for a tree that SHIPS HELM,
    and that this process is gating it from OUTSIDE.

    `gate.run` no longer assumes its own command for every repo it is handed: it
    ASKS who declares one — a registered project's own authored `gate`
    declaration, else helm's `SUITE`, and that default applies ONLY to a tree
    that ships helm, because that is the only tree whose command helm may
    assume. A bare temp git repo declares nothing and ships nothing, so without
    this mark every `gate.run(repo=self.repo)` in the estate is refused with
    "declares no gate command" — a TRUE refusal that says nothing about what the
    arm was checking. MEASURED before the mark existed: 16 of 41 arms in
    test_gate.Minting alone.

    TWO HALVES, AND THE SECOND IS NOT A WORKAROUND FOR THE FIRST. The mark is
    the file the gate's is-this-helm predicate reads -- `helm/__init__.py`, THE
    PACKAGE ROOT, which is what `selfrepo.is_helm_source_tree` answers SOURCE on
    and what `import helm` actually binds. It was `bin/helm` until task/2442
    ruled that an entry script LAUNCHES helm and is not helm's source: a project
    shipping a one-line `bin/helm` wrapper is an ADOPTER, and every predicate in
    the tree now says so. A fixture marked the old way reads as an adopter that
    declared nothing and is refused "declares no gate command" -- the same
    true-but-irrelevant refusal this mark exists to keep out of the arms. Once
    the package root is there the repo IS a helm checkout, and
    `_cross_tree_refusal` then correctly observes that this python came from the
    real checkout and not from the fixture. A fixture repo can
    never BE the running binary's tree, so it takes the same recorded escape an
    operator takes to gate a tree from outside it — the override that "always
    says so, on stderr". Measured: with only the first half, 23 of 41 arms in
    test_gate.Minting are refused cross-tree.

    The mark is planted UNCOMMITTED but STAGED on purpose: every caller plants it
    before its own first commit, so it rides that commit and no fixture's head,
    tree or receipt id moves. A fixture whose arm is about an ADOPTER project —
    one that declares its own command — must NOT call this: that arm wants the
    declaration path, and marking the repo would take the default instead.
    """
    d = os.path.join(repo, "helm")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, "__init__.py")
    with open(path, "w") as fh:
        fh.write("# fixture package root: this temp repo stands in for\n"
                 "# a tree that ships helm\n")
    # STAGED, NEVER COMMITTED HERE. A commit would move the fixture's HEAD; a
    # plain untracked file would leave the worktree DIRTY and every receipt this
    # fixture mints would record dirty=True. Staging leaves exactly one state:
    # the fixture's own next commit includes it. A non-git directory (the arms
    # that gate a bare scratch root) just keeps the file.
    subprocess.run(("git", "add", "helm/__init__.py"), cwd=repo,
                   capture_output=True, timeout=30)
    own_env(case, "HELM_CROSS_TREE_GATE", "1")
    return path


def own_env(case, key, value):
    """Set one environment variable for this case, with ONE restoration owner.

    THE ORDER IS THE WHOLE PROBLEM. unittest runs addCleanup callbacks AFTER
    tearDown and in LIFO order, so a helper that registers a restore per CALL
    restores a value it captured at a moment a sibling call had already changed,
    and the LAST cleanup to run wins. Measured before this existed: the shared
    gate fixture calls `helm_tree` twice, the two calls captured None and then
    "1", tearDown restored an INCOMING HELM_CROSS_TREE_GATE=1, and then the
    first call's cleanup popped it — so a process that came in with the
    cross-tree override silently lost it for every later test, which is exactly
    the host-coupled fixture class the key is in ENV_KEYS to prevent.

    ONE OWNER PER (case, key): the value observed at the FIRST ownership is the
    one restored, later calls change nothing about the restoration, and the
    restore is UNCONDITIONAL — a guard that only undoes "what is still ours"
    cannot distinguish the value it set from the one the process arrived with.
    A fixture that snapshots the WHOLE environment stays the final word by
    registering its own restoration through addCleanup BEFORE calling any of
    these helpers, which is what `tests/test_gate.py`'s GateBase and
    `tests/test_gate_focus.py`'s FocusBase both do: earliest registration, last
    to run. A fixture that restores its snapshot in tearDown INSTEAD runs BEFORE
    these cleanups and loses whatever the process arrived with — every value
    alike, `1`, `0` and any other string, since the restore below is
    value-faithful and unconditional by design and cannot know it is second.
    """
    owned = case.__dict__.setdefault("_tmphome_owned_env", {})
    if key not in owned:
        owned[key] = os.environ.get(key)

        def _restore(key=key, prior=owned[key]):
            if prior is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = prior

        case.addCleanup(_restore)
    os.environ[key] = value


def pin_dispatch_home(case, repo):
    """Make `repo` this fixture's OWN project for the dispatch write door.

    THE DOOR REFUSES A REF WHOSE REPOSITORY IS NOT THIS PROJECT'S, and
    home_repo_id resolves from the RUNNING PACKAGE'S location — the real helm
    checkout, never a fixture's temp repo. Without this pin every dispatch a
    test writes is refused as FOREIGN: a TRUE refusal that says nothing about
    what the test was checking, across 86 write sites in nine files.

    A fixture that writes dispatches for a temp repo SHOULD declare that repo
    is its home. That is the honest shape — the alternative was leaving the
    write door fail-open on an unknown authority, which is the escape this
    guard exists to close.

    RETURNS THE REAL RESOLVER, deliberately, so an arm that must exercise it
    can put it back. Pinning with no escape is how a suite ends up asserting
    about a lambda and never about the resolver — the exact gap that let a
    registry fail-open ship past a 10,863-test green.
    """
    from helm import dispatches
    real = dispatches.home_repo_id
    home = dispatches._repo_info(repo)["repo_id"]
    dispatches.home_repo_id = lambda: (home, None)
    case.addCleanup(setattr, dispatches, "home_repo_id", real)
    return real


@contextlib.contextmanager
def dispatch_home(repo):
    """Write as though this process were `repo`'s OWN helm, for one block.

    THE CROSS-REPO FIXTURES ARE NOT WORKING AROUND THE DOOR — they are
    modelling the only way such a row can honestly exist. A row bound to
    repository B is written by B's helm, in B's ledger; a test that needs one
    present in order to prove the CONSUMER distinguishes repositories has to
    mint it the way the world mints it. Disarming the door instead would make
    those arms assert about a ledger state production cannot produce.

    Restores the previous resolver on exit, including on exception, so a
    fixture that raises mid-block cannot leave the door pointed at the twin.
    """
    from helm import dispatches
    real = dispatches.home_repo_id
    home = dispatches._repo_info(repo)["repo_id"]
    dispatches.home_repo_id = lambda: (home, None)
    try:
        yield real
    finally:
        dispatches.home_repo_id = real


@contextlib.contextmanager
def declaring(argv=(), default="seat-a"):
    """DECLARE an identity for one CLI call — the fixture half of the identity
    layer's contract.

    A LEASE CLAIM IS ACTOR-ATTRIBUTED: it durably assigns responsibility, gates
    routing and release, and can strand another worker, so `helm.actors`
    requires an AdmittedActor and refuses a name MINTED from session+cwd. A
    hermetic fixture runs with no HELM_CHAT_NAME and no roster row, which IS
    the DERIVED tier — so a fixture that claims a lease has to say who it is,
    the same thing a real seat does at launch. Seeding the identity is the
    honest fix; softening the door so an unnamed process may own a lease was
    considered and refused (cross-family ruling, task/994).

    `--seat S` IN ARGV IS AN ASSERTION, NEVER A SELECTOR, so the declared name
    is taken FROM it: a fixture that claims as one seat and then as another is
    modelling two seats, and it now models them the way the world does — one
    declared identity per process, per call. An argv with no --seat gets
    `default`, so a bare `claim <lane>` still runs as somebody.

    Restores the prior value (including its absence) on exit, so a fixture that
    raises mid-call cannot leak a declared name into the next test.
    """
    args = list(argv)
    prior = os.environ.get("HELM_CHAT_NAME")
    seat = None
    if "--seat" in args:
        i = args.index("--seat")
        if i + 1 < len(args):
            seat = args[i + 1]
    # NO --seat MEANS DO NOT TOUCH THE AMBIENT IDENTITY. A fixture whose setUp
    # already declares one is modelling a seat, and overriding it here made
    # `helm work list` render another seat's rows as not-mine — the "(yours)"
    # column vanished and two arms went red for a reason that had nothing to do
    # with what they test. `default` is the floor for a fixture that declares
    # nothing at all, not a replacement for one that does.
    if seat and prior and seat.casefold() == prior.casefold():
        # THE DECLARED SPELLING WINS OVER ARGV'S. `--seat kimi` against a
        # process declaring `Kimi` is a SATISFIED assertion, and the seat it
        # acts as is the declared one — seat identity is casefolded, so
        # letting argv casing become the actuator key would split that seat's
        # beacon election. Overriding here erased exactly the distinction
        # test_seats.WaitTest.test_case_variant_seat_assertion_arms_the_
        # declared_identity exists to pin.
        seat = prior
    seat = seat or prior or default
    # AND CORROBORATE IT, because a declared name alone is not an identity.
    # The name is a value any process can export; what makes it evidence is a
    # session the ROSTER resolves back to the same seat, and an ACT door now
    # requires that. A fixture that declares a name and stops has modelled the
    # export and not the join.
    #
    # THIS IS THE SAME MOVE AS THE ONE ABOVE, ONE LEVEL DEEPER, and the ruling
    # in the docstring already settled the direction: seeding the identity is
    # the honest fix, softening the door was considered and refused. So the
    # fixture does what a real seat does at launch -- it declares a name AND
    # joins, rather than the door learning to accept half of it.
    #
    # THE SESSION IS MINTED FROM THE SEAT so it is stable across calls within a
    # fixture: two `declaring` blocks for one seat are the same seat, and two
    # seats never collide. The row is MERGED rather than written, because
    # fixtures roster other seats and clobbering them would break arms that are
    # about somebody else.
    sid = _own_session(seat) or session_for(seat)
    prior_sid = os.environ.get("CLAUDE_CODE_SESSION_ID")
    os.environ["HELM_CHAT_NAME"] = seat
    os.environ["CLAUDE_CODE_SESSION_ID"] = sid
    undo = corroborate(seat, sid)
    try:
        yield seat
    finally:
        if prior is None:
            os.environ.pop("HELM_CHAT_NAME", None)
        else:
            os.environ["HELM_CHAT_NAME"] = prior
        if prior_sid is None:
            os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
        else:
            os.environ["CLAUDE_CODE_SESSION_ID"] = prior_sid
        # THE ROSTER ROW GOES BACK WITH THE ENV, or this block leaves a JOIN
        # behind that no arm asked for -- see `corroborate`.
        undo()


def session_for(seat):
    """The session id a fixture mints for `seat` — one spelling, one seat.

    STABLE ACROSS CALLS AND ACROSS HELPERS, deliberately: `declaring`, `declare`
    and a hand-built child env must agree, or a fixture that names itself twice
    would be two seats and the roster would carry both bindings.
    """
    return "sess-declaring-%s" % seat.casefold()


_SESSION_VARS = ("CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID",
                 "CODEX_SESSION_ID")


def _own_session(seat):
    """The session already in this env IF the roster says it is `seat`'s.

    A FIXTURE THAT SET ITS OWN SESSION MEANT IT, and minting a second one over
    the top is not a stronger identity — it is a DIFFERENT one. Measured: the
    lease-recovery fixture exports its own SESSION, binds a lease to it, then
    runs the printed release command inside `declaring`; a minted session
    replaced the granting one and the release was refused with "release needs
    the granting session" — a true refusal about a session the fixture never
    presented.

    AND THE ROSTER HAS TO AGREE BEFORE WE ADOPT IT, which is the half that
    keeps this safe under the whole suite. Any session leaking in from the
    process that RUNS the tests would otherwise be bound to whichever seat
    declared next, and one sid resolving to several seats is the inconsistency
    `seat_for_session` announces loudly. Unowned or foreign -> mint instead.
    """
    for var in _SESSION_VARS:
        sid = os.environ.get(var)
        if not sid:
            continue
        try:
            from helm import seats as _seats
            from helm.seats_common import recipient_matches as _same
            bound = _seats.seat_for_session(sid)
            # PRODUCTION'S RELATION, NOT PYTHON'S. The roster keeps ONE
            # canonical key per casefold-equivalent name and
            # `identity_disagreement` compares declared against bound with
            # casefold, so a case-variant declaration is the SAME seat. A raw
            # `==` here rejects the session this very row grants, mints a
            # replacement, and the release door then refuses with "release
            # needs the granting session" — the exact failure this helper was
            # written to end, reappearing on the spelling axis.
            return sid if bound and _same(bound, seat) else None
        except Exception:                       # noqa: BLE001 — never raise
            return None
    return None


def declare(case, seat, session=None):
    """Name `seat` for the rest of `case`, WITH something behind the name.

    THE setUp/mid-test HALF of what `declaring` does for one call. A bare
    `os.environ["HELM_CHAT_NAME"] = seat` models the EXPORT and not the JOIN:
    the name is a value any process can set, and an ACT door now requires a
    session the ROSTER resolves back to the same seat. Forty arms across nine
    modules declared that way and went red at act doors that were never their
    subject — which is why this is a helper and not forty edits: an arm written
    tomorrow reaches for the same one line.

    RESTORES THE PRIOR VALUES, INCLUDING THEIR ABSENCE, through addCleanup, so
    an arm that raises cannot leak a declared name into the next one. Returns
    the session id for arms that assert on it.

    AN ARM WHOSE SUBJECT IS THE UNCORROBORATED TIER MUST NOT CALL THIS. It
    wants the export without the join, and seeding the roster here would make
    it pass for the opposite reason — the same trap `pin_suite_guard` names.
    """
    sid = session or _own_session(seat) or session_for(seat)
    prior = os.environ.get("HELM_CHAT_NAME")
    prior_sid = os.environ.get("CLAUDE_CODE_SESSION_ID")

    def _restore():
        # ONLY UNDO WHAT IS STILL OURS. unittest runs addCleanup AFTER
        # tearDown, and most of these fixtures restore the whole environment in
        # their own tearDown -- so an unconditional restore here would run
        # SECOND and put back a value the fixture had already replaced, or pop
        # one it had legitimately returned. Comparing first makes the two
        # orders indistinguishable.
        if os.environ.get("HELM_CHAT_NAME") == seat:
            if prior is None:
                os.environ.pop("HELM_CHAT_NAME", None)
            else:
                os.environ["HELM_CHAT_NAME"] = prior
        if os.environ.get("CLAUDE_CODE_SESSION_ID") == sid:
            if prior_sid is None:
                os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
            else:
                os.environ["CLAUDE_CODE_SESSION_ID"] = prior_sid

    os.environ["HELM_CHAT_NAME"] = seat
    os.environ["CLAUDE_CODE_SESSION_ID"] = sid
    # CLEANUPS RUN LAST-REGISTERED-FIRST, and the roster undo is registered
    # AFTER the env restore so it runs BEFORE it -- the same order `declaring`
    # unwinds in, so a fixture cannot tell the two helpers apart.
    case.addCleanup(_restore)
    case.addCleanup(corroborate(seat, sid))
    return sid


def corroborate(seat, sid):
    """Bind `sid` to `seat` in the roster, merging rather than replacing.

    THE ROSTER IS ON DISK UNDER HELM_HOME, WHICH A CHILD INHERITS, so a fixture
    that launches a subprocess corroborates from the PARENT and the child's own
    exported session resolves. That is why this is separate from `declare`: a
    child env is the caller's to build (its session may live in CODEX_SESSION_ID
    rather than the claude var, and overwriting that would change which family
    the child looks like), while the binding it needs is written here.

    RETURNS AN UNDO, AND CALLING IT IS NOT OPTIONAL FOR A SCOPED HELPER. A
    ROSTER ROW IS NOT INERT: `listedness`, the absent-seat census and the stale
    -claim renderers all read presence off this file, so a row minted for one
    CLI call and left behind changes what the NEXT assertion sees. Five arms in
    test_stale_claim measure exactly that — "ME holds it and never joined" — and
    seeding without undoing made the fixture join on their behalf and answered
    `listed` where the arm's whole subject was `unlisted`.

    THE UNDO IS ALSO WHAT MAKES THE FIXTURE HONEST ABOUT THE WORLD, not merely
    tidy. Under the corroboration law an unrostered seat cannot TAKE a claim at
    all, so a claim held by an unlisted holder is only reachable the way the
    world reaches it: a rostered seat claims, and its row is pruned or disowned
    afterwards. Claim-inside-the-block, restore-on-exit IS that sequence.

    NEVER RAISES. A fixture whose home is not writable, or which has no roster
    directory yet, is not a fixture this helper may fail: the arms that care
    about the roster assert on it themselves, and the arms that do not must not
    acquire a new way to error in setUp. A failed seeding returns an undo that
    does nothing, so a caller never has to ask whether it worked.
    """
    def _nothing():
        pass

    try:
        from helm import seats as _seats, pk as _pk
        from helm.seats_common import recipient_matches as _same
        path = _seats.roster_path()
        rows = _pk.read_json(path, {}) or {}
        # ONE CANONICAL KEY PER IDENTITY, RESOLVED BEFORE THE ROW IS LOADED --
        # the law `write_roster` enforces, which a fixture writing the same
        # file has to uphold or it manufactures a state production REFUSES.
        # Looking the row up by exact spelling misses an existing case-variant
        # key, inserts a SECOND row for one identity, and the next real
        # `write_roster` raises "the roster holds 2 case-variant rows".
        canon = [k for k in rows if str(k).casefold() == str(seat).casefold()]
        if len(canon) > 1:
            # AMBIGUOUS IS DECLINED, NEVER GUESSED. Production refuses this
            # roster and tells the operator to repair it; a fixture picking one
            # would seed a binding onto whichever row it happened to sort to.
            return _nothing
        seat = canon[0] if canon else seat       # keep the EXISTING spelling
        row = dict(rows.get(seat) or {})
        if row.get("session") == sid or sid in (row.get("sessions") or ()):
            return _nothing                     # already bound, nothing to do
        bound = _seats.seat_for_session(sid)
        if bound and not _same(bound, seat):
            # ONE SESSION, ONE SEAT. Adding a second binding does not make this
            # seat corroborated -- it makes the sid AMBIGUOUS, which
            # `seat_for_session` reports as a repairable inconsistency rather
            # than resolving. A fixture must not manufacture that.
            return _nothing
        had = seat in rows
        before = dict(rows[seat]) if had else None
        # PRESENCE AND VALUE ARE TWO FACTS. A row can carry `session: null`
        # -- the key PRESENT, holding no sid -- and reading the value alone
        # cannot tell that from a row with no session key at all. Restoring
        # by value REMOVES an originally present null, which is a different
        # row from the one we found.
        had_session = had and "session" in before
        row["session"] = sid
        rows[seat] = row
        os.makedirs(os.path.dirname(path), exist_ok=True)
        _pk.atomic_write(path, json.dumps(rows))
    except Exception:                           # noqa: BLE001 — see docstring
        return _nothing

    def _undo():
        # UNDO THE BINDING, NEVER THE ROW. The block between seed and undo is
        # the CLI call itself, and it legitimately writes -- including to THIS
        # seat's own row: a `join` inside the block creates the real row, with
        # its own session, cursor and home. Restoring the row wholesale
        # DELETED that join, and the arm then read UNKNOWN membership for a
        # seat it had just joined (test_chat_restore_journal's join_seat, which
        # asserts the row precisely so this cannot fail silently).
        #
        # So the only thing put back is the ONE KEY this helper set, and only
        # while it is still the value this helper wrote: if the block replaced
        # the session, that is the block's answer and it stands.
        try:
            now = _pk.read_json(path, {}) or {}
            row = now.get(seat)
            if not isinstance(row, dict) or row.get("session") != sid:
                return                          # the block owns it now
            row = dict(row)
            if before is None and set(row) <= {"session"}:
                now.pop(seat, None)             # nothing but our own seeding
            else:
                if not had_session:
                    row.pop("session", None)
                else:
                    row["session"] = (before or {}).get("session")
                now[seat] = row
            _pk.atomic_write(path, json.dumps(now))
        except Exception:                       # noqa: BLE001 — see docstring
            pass

    return _undo


def pin_suite_guard(case, tmp):
    """Pin the ONE required external guard helm does not ship, per fixture.

    `hooks.SPECS` carries `suite-guard`, whose executable helm does not ship
    (`fab-suite-pretooluse`), and a seat mint REFUSES a contract it cannot
    write in full — an unguarded seat must not be born. Left unpinned, that
    resolution comes off the HOST's PATH: present on a machine that has the
    binary installed, absent in CI, so the SAME fixture would mint on one and
    refuse on the other, and the behaviour under test would depend on which box
    ran the suite. Seven fixtures need this; it lives here once so the estate
    cannot grow two spellings of the same fact.

    TWO CLASSES OF ARM, and which one you are writing decides whether to call
    this at all:

      COMPLETE RUNNABLE CONTRACT — the arm's subject is a seat that mints,
      launches or actually EXECUTES launch.sh. It takes this one hermetic,
      harness-owned, absolute guard fixture. An arm that also runs launch.sh
      under a scrubbed env must carry HELM_SUITE_GUARD into that env too, or
      the generated preflight refuses and the arm measures the preflight
      instead of its own subject.

      MINT-ONLY SHORTENED — the arm's subject IS the unresolved path. It must
      NOT pin a resolvable guard; it deliberately leaves the guard unresolvable
      and asserts the warning. Pinning there tests the wrong state. Such an arm
      makes the pin DEAD rather than merely unsetting it, because an unset pin
      resolves off the host's PATH and would otherwise pass for opposite
      reasons on a box that has the binary and one that does not.

    Restores the prior value (including its absence) through addCleanup, so a
    fixture that raises mid-test cannot leak a pin into the next one. Returns
    the path for arms that assert on it.
    """
    prior = os.environ.get("HELM_SUITE_GUARD")

    def _restore():
        if prior is None:
            os.environ.pop("HELM_SUITE_GUARD", None)
        else:
            os.environ["HELM_SUITE_GUARD"] = prior

    case.addCleanup(_restore)
    p = os.path.join(tmp, "fab-suite-pretooluse")
    with open(p, "w") as f:
        f.write("#!/bin/sh\nexit 0\n")
    os.chmod(p, 0o755)
    os.environ["HELM_SUITE_GUARD"] = p
    return p


def healthy_tmp(path):
    """A fixture box's tmp mount, well under every scratch threshold, in the
    shape `scratch.usage` answers. Stated once, because the child pin below
    spells the same reading as source."""
    return {"path": path, "mount": path, "fstype": "fixture",
            "nr_inodes": None, "bytes_pct": 1, "inodes_pct": 1}


def pin_admission(case, proc=None, ledger=None, tmp=None):
    """Run this case's gate admission against a FIXTURE box, never this one.

    `gate.run` admits a whole-suite, suite-shaped or majority-focused run
    through `gate._admit_suite`, and that door reads THE HOST on purpose: the
    real /proc for its census, pressure and panes, and the node-wide ledger
    under the RAM root for pending owners. `HELM_PROC` does not redirect it
    (task/1740: fixtures still set it believing it did). So an arm that is not
    about admission went red whenever the node running it was full — MEASURED:
    25 test_gate arms refused by the build host's live whole-suite cap while two whole
    suites ran there — and every run it did admit wrote the live ledger.

    This keeps the REAL admission code and hands it a fixture box: `proc`
    (default an empty process table: no suites, no pressure, no panes) and a
    fixture `ledger`, which `_admission_release` reaches too. The box's TMP is
    pinned the same way: the real `_scratch_preflight` reads `tmp`, a fixture
    reader of the mount (default a healthy one), never this node's mount, whose
    fill decided the run just as the cap did. An arm that IS about admission
    plants its own processes under `proc`, passes its own `tmp`, or patches
    the door itself; an inner patch wins while it is active. Returns
    (proc, ledger).
    """
    from unittest import mock
    from helm import gate
    root = tempfile.mkdtemp(prefix="helm-test-admission-")
    case.addCleanup(shutil.rmtree, root, True)
    if proc is None:
        proc = os.path.join(root, "proc")
        os.makedirs(proc)
    ledger = ledger or os.path.join(root, "gate-admissions.json")
    tmp = tmp or healthy_tmp
    real, real_preflight = gate._admit_suite, gate._scratch_preflight

    def admit(*args, **kwargs):
        kwargs.setdefault("proc_dir", proc)
        kwargs.setdefault("admissions_path", ledger)
        return real(*args, **kwargs)

    def preflight(*args, **kwargs):
        kwargs.setdefault("usage", tmp)
        return real_preflight(*args, **kwargs)

    for name, value in (("_admissions_path", lambda *a, **k: ledger),
                        ("_admit_suite", admit),
                        ("_scratch_preflight", preflight)):
        patch = mock.patch.object(gate, name, value)
        patch.start()
        case.addCleanup(patch.stop)
    return proc, ledger


def pin_admission_code(proc, ledger):
    """`pin_admission` for a CHILD python an arm spawns with `-c`, as source to
    prepend: the same fixture box, so parent and child share one ledger. A
    child never imports this package, so the suite's tripwire cannot see it,
    and this is the only thing that keeps its admission off the host."""
    return ("from helm import gate as _pin_gate\n"
            "_pin_real = _pin_gate._admit_suite\n"
            "def _pin_admit(*args, **kwargs):\n"
            "    kwargs.setdefault('proc_dir', %r)\n"
            "    kwargs.setdefault('admissions_path', %r)\n"
            "    return _pin_real(*args, **kwargs)\n"
            "_pin_gate._admit_suite = _pin_admit\n"
            "_pin_gate._admissions_path = lambda *args, **kwargs: %r\n"
            "_pin_preflight = _pin_gate._scratch_preflight\n"
            "def _pin_tmp(path):\n"
            "    return %r | {'path': path, 'mount': path}\n"
            "def _pin_pre(*args, **kwargs):\n"
            "    kwargs.setdefault('usage', _pin_tmp)\n"
            "    return _pin_preflight(*args, **kwargs)\n"
            "_pin_gate._scratch_preflight = _pin_pre\n"
            % (proc, ledger, ledger, healthy_tmp("")))
