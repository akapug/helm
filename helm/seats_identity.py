#!/usr/bin/env python3
"""helm seats — identity and addressing: who a seat IS, and who a word is FOR.

Two questions that share every primitive and are constantly confused, so they
live together deliberately. WHO IS THIS (derive_seat, acting_seat, auto_name,
foreign_seat, identity_disagreement) resolves a session id and a cwd into a
seat name, and says so out loud when two sources disagree rather than picking
a winner. WHO IS THIS FOR (mentions, deliverable, seat_scope, _delivery_pause)
decides whether a written word reaches a given seat.

THE THREE FUNCTION-LEVEL IMPORTS AT THE BOTTOM ARE NOT LAZINESS. This module
and the roster form a genuine cycle: identity needs three roster QUERIES
(seat_for_session, runtime_for_session, nonpane_session) at five call sites,
and the roster needs identity for most of what it writes. The cycle was
measured, not assumed — it is the thinnest of the six in the whole file, which
is why it is the one closed with a deferred import rather than by moving code.
Hoisting those three into seats_common was considered and rejected: they call
back into the roster themselves, so they are not closure-clean, and a floor
that is not closed under its own dependencies is what turns four cycles into
eight (see seats_common's docstring — that number is measured too).
"""

import contextlib
import getpass
import hashlib
import os
import re
import subprocess
import sys

from . import chat, home, pk, vcs
from .machine_senders import is_machine
from .seats_address import (_mention_re, mentions, seat_names,  # noqa: F401
                            seat_scope)                        # re-exported
from .seats_common import (MAX_BYTES, _BROADCAST, _canonical_recipient, _clip,
                           live_alias, names_match, _scrub, _seat_key,
                           canonical_keys, dm_lane, own_name,
                           recipient_matches, roster, roster_path, seat_row)

_FAMILIES = ("fable", "opus", "sonnet", "haiku", "kimi", "glm", "gpt",
             "gemini", "deepseek", "qwen", "grok", "mistral", "llama")
def _family():
    """The ambient model family, best-effort: the model env first (seat
    launches export CLAUDE_CODE_SUBAGENT_MODEL), else the harness. Display
    material for the auto-name — never identity, never authorization."""
    model = (os.environ.get("CLAUDE_CODE_SUBAGENT_MODEL")
             or os.environ.get("ANTHROPIC_MODEL") or "").lower()
    for fam in _FAMILIES:
        if fam in model:
            return fam
    if model:
        tok = pk.slug(model).split("-")[0]
        if tok:
            return tok
    if os.environ.get("CODEX_SESSION_ID"):
        return "codex"
    if (os.environ.get("CLAUDE_CODE_SESSION_ID")
            or os.environ.get("CLAUDE_SESSION_ID")
            or os.environ.get("CLAUDECODE")):
        return "claude"
    return "agent"
def auto_name(session, cwd=None):
    """G-stable-names: a MEANINGFUL stable auto-name for an un-named join —
    <project>-<family> ('helm-fable'), deduped with -2/-3… when a DIFFERENT
    session already holds the name. Stable: callers reach here only when the
    roster has no row for this session, and the result is immediately
    roster-bound (join / deliver self-heal), so the same session keeps
    resolving to the same seat. Opaque agent-<sid8> hex (12/15 of the live
    roster before this) is the last-resort floor only."""
    sid = str(session)
    proj = os.path.basename((cwd or "").rstrip(os.sep))
    base = pk.slug("%s-%s" % (proj, _family())) if proj else _family()
    r = roster()
    from .sessions import pane_heir     # a restart in the same Orca pane

    def taken(name):
        row = r.get(name)
        return bool(row) and row.get("session") != sid \
            and sid not in (row.get("sessions") or [])

    cands = [base] + ["%s-%d" % (base, i) for i in range(2, 100)]
    return pane_heir(r, cwd) or next(
        (c for c in cands if not taken(c)), "agent-" + sid[:8])
def derive_seat(session=None, cwd=None):
    """$HELM_CHAT_NAME first, then the session's roster binding, then a
    meaningful stable auto-name for an un-rostered session; a bare agent never
    gets the operator's identity."""
    # DEFERRED, to close the identity<->roster cycle at its thinnest
    # leg. See this module's docstring: the cycle is real and was
    # measured; this is the smaller side of it.
    from .seats import seat_for_session
    name = home.chat_name()   # THE validated seam (home.chat_name): a hostile
    if name:                  # HELM_CHAT_NAME is rejected, never becomes a seat
        return own_name()     # ...and a live rename alias resolves to its row
    if session:
        bound = seat_for_session(session)
        if bound:
            return bound
        return auto_name(session, cwd)
    return chat.whoname()
# The identity resolution states, NAMED. They already existed — as prose in
# these docstrings and as the branch order of derive_seat/acting_seat — and
# every caller re-derived them from the truthiness of a returned string, which
# cannot distinguish them at all. Three defects in one day, 2026-08-14, were
# each one call site failing to tell two of these apart.
DECLARED = "declared"   # this process's own HELM_CHAT_NAME, through the
                        # validated seam. Inheritable by any child, which is
                        # the 2026-08-02 takeover.
ROSTERED = "rostered"   # no declared name; the session->row map answered.
                        # Corruptible, and outranking DECLARED with it caused
                        # the 2026-07-24 cross-seat contamination.
DERIVED = "derived"     # NEITHER answered, so a name was MINTED from session
                        # and cwd. THIS IS THE DANGEROUS ONE and it is the
                        # reason this enum exists: it is a STRANGER IDENTITY
                        # THAT PASSES EVERY CHECK. It is never None, never
                        # empty, and reads exactly like a real seat to any
                        # caller testing `if seat:` — so a verb that acts on
                        # a backlog, a cursor, or an obligation under a
                        # DERIVED name is acting for somebody who does not
                        # exist, and reporting success.


def resolve_identity(session=None, cwd=None):
    """(state, name) — acting_seat's answer WITH the provenance that makes it
    safe to act on.

    Same resolution order and same name as `acting_seat`, which stays the
    convenience wrapper for callers that genuinely only need a string (a
    label, a display, a stamp). Any caller whose ACTION differs by provenance
    must use this instead, because the distinction it needs is invisible in
    the string: DECLARED, ROSTERED and DERIVED are all non-empty and all
    look identical to `if seat:`.

    THE RULE OF THUMB: if being wrong about WHO would silently mark work as
    done for the wrong party — parking a backlog, moving a cursor,
    discharging an obligation, claiming a lease — DERIVED must not be treated
    as an identity. If the answer is only rendered or logged, it is fine."""
    own = own_name()
    if own:
        return DECLARED, own
    if session:
        from .seats import seat_for_session
        hit = seat_for_session(session)
        if hit:
            return ROSTERED, hit
    return DERIVED, derive_seat(session, cwd)


# THE ADMISSION DOOR MOVED OUT OF THIS FILE, deliberately. A first attempt put
# it here (`admitting_seat`) and six live bypasses were measured
# around it: the helper was a door in a wall with four other doors, because
# what a caller needed — a seat NAME — stayed obtainable as a bare string from
# `derive_seat`, `acting_seat`, `chat.whoname` and `seat_for_session or derive`.
# It also omitted `identity_disagreement`, which lives twenty lines below it,
# so a declared name disagreeing with the roster was admitted as DECLARED — the
# 2026-08-02 P0 reintroduced by its own fix.
#
# Admission is now `helm.actors.resolve_actor`, which returns an OPAQUE
# CAPABILITY instead of a string. The functions in THIS file keep answering
# "what name is this?" for RENDER and ADDRESSING, which is all they were ever
# safe for; authorization is a type an actuator's signature requires and a
# name cannot satisfy.


def foreign_seat(seat):
    """True when `seat` is provably NOT this process's own seat: this process
    DECLARES a name and that name is a different seat. A process with no
    declared name is never 'foreign' — fail-open, so the owner CLI, an
    operator script, and any seat whose launch seam predates the env var keep
    working exactly as before."""
    own = own_name()
    return bool(own) and str(seat or "").casefold() != str(own).casefold()
def acting_seat(session=None, cwd=None):
    """THE identity law for every delivery/presence path: the ONE seat this
    process is allowed to stamp. Its OWN declared name FIRST, then the
    session->row map, then the auto-name floor.

    Delivery used to ask the ROSTER first (`seat_for_session(session)` BEFORE
    the process's own name — old seats.py:1264/1364/1546), and that inversion
    is the cross-seat contamination the owner hit 2026-07-24: once any row
    remembered this process's session id, that row's seat NAME hijacked this
    process's identity. Every tool boundary then refreshed the WRONG seat's
    presence (a paneless seat reading 🟢 fresh forever) and CONSUMED the wrong
    seat's inbox (the owner's @mention delivered into a stranger's context —
    `pending 0` on a seat that never answered). chat.whoname() always had this
    order right; the delivery path did not. Same law, one owner."""
    # DEFERRED, to close the identity<->roster cycle at its thinnest
    # leg. See this module's docstring: the cycle is real and was
    # measured; this is the smaller side of it.
    from .seats import seat_for_session
    own = own_name()
    if own:
        return own
    if session:
        hit = seat_for_session(session)
        if hit:
            return hit
    return derive_seat(session, cwd)
def identity_disagreement(session=None):
    """(own, bound) when BOTH identity sources answer and DISAGREE, else None:
    this process's DECLARED name (own_name — the env, which any inheriting
    child gets for free) vs the roster's binding for `session`
    (seat_for_session — repairable, corruptible state).

    THE INCIDENT (owner-declared P0, 2026-08-02): a crashed pane was
    restarted and the new process inherited HELM_CHAT_NAME=opus-integrator
    from an orca-ancestor's export. Every resolver then agreed the process
    WAS the integrator: it armed that seat's beacon, drained ~60 of its DMs.
    The mirror failure already happened 2026-07-24 with the OPPOSITE order
    (roster outranked the declared name — see acting_seat). Both orders have
    now failed live, so the law is not an order at all: AGREEMENT of sources
    is the only clean state. When they disagree, acting/delivery/registration
    paths REFUSE (naming both answers), and read-only paths warn loudly
    (_warn_disagreement) — neither source is ever silently preferred.

    THE R1 PREDICATE WAS A PROPER SUBSET AND AN ADVERSARY MEASURED IT OPEN.
    It asked "does MY session disagree with the roster", so it required
    `bound` to be non-empty — and a crash-restarted pane is a NEW PROCESS
    WITH A FRESH SID, whose `seat_for_session` is None. Inherited name plus
    fresh sid produced no dispute and every rung opened: the exact incident,
    replaying against its own guard. The subset was invisible because the
    fixtures (and the acceptance criterion the dispatcher banked) all staged
    a sid ALREADY bound elsewhere — the shape that IS caught, generalized
    from one instance to a class it did not cover.

    THE QUESTION IS NOT WHOSE MY SESSION IS. IT IS WHETHER THE NAME I CLAIM
    IS ALREADY SOMEONE'S. Absence of a binding for my sid is not absence of
    a conflict, so there are two dispute shapes and both refuse:
      TAKEOVER  — my sid is rostered to a DIFFERENT seat (r1's shape).
      CLAIM-JUMP — my sid is rostered NOWHERE, and the name I declare is
                   held by a row bound to a DIFFERENT, LIVE session.
    Claim-jump needs no "...and that session is not mine" clause, and one was
    written then REMOVED when a mutation proved it inert: seats_for_session
    reads the SAME row["session"] field, so a row holding my sid would have
    made `bound` non-empty and returned above. An untestable guard clause is
    a guarantee's form without its substance, so it is gone rather than
    commented. If that field ever stops being the one `bound` consults, this
    reasoning dies with it — the arms below are the tripwire.

    Claim-jump is deliberately narrow: it fires only while the claimed row's
    session is HELD — `session.held_or_unproven`, which fails closed. A row
    with no session, or whose session no live process holds (a spawn mirror,
    a relaunch onto a fresh session), is a seat between panes: a name waiting
    to be claimed, and refusing there locks a live pane out of its own seat.
    """
    # DEFERRED, to close the identity<->roster cycle at its thinnest leg (see
    # this module's docstring); the process census is deferred with it.
    from .seats import nonpane_session, seat_for_session
    from .session import held_or_unproven as _held
    own = own_name()
    if not (own and session):
        return None
    try:
        bound = seat_for_session(session)
    except Exception:      # noqa: BLE001 — an UNREADABLE roster is no
        return None        # dispute: this check rides never-raise paths
                           # (stop_guard rungs, the delivery boundary)
    if bound and str(bound).casefold() != str(own).casefold():
        return own, bound                       # TAKEOVER
    if bound:
        return None                             # my sid, my name — clean
    if not nonpane_session(session) and _held(_live_session_of(own)):
        return own, own                         # CLAIM-JUMP
    return None
def _dispute_sentence(own, bound, session):
    """The dispute, described by SHAPE — the two read nothing alike.

    bound != own is a TAKEOVER: my sid lives in someone else's row. bound ==
    own is a CLAIM-JUMP: my sid lives nowhere and the name I declare is held
    by a different live session. That equality IS the discriminator, so no
    caller sniffs a string, and the tuple stays two-wide for every existing
    consumer. The claim-jump wording exists because the takeover sentence was
    factually WRONG for it — it said a session was "rostered to" a name when
    that session is rostered nowhere, which is the misleading-surface class
    this whole lane is about."""
    if str(bound).casefold() != str(own).casefold():
        return ("this process declares %r but session %.8s is rostered to %r. "
                "An inherited HELM_CHAT_NAME is free; the roster can be "
                "corrupt; only agreement is clean. Fix: unset/re-export "
                "HELM_CHAT_NAME, or `helm chat seat disown %s %.8s`"
                % (own, str(session), bound, bound, str(session)))
    return ("this process declares %r and session %.8s is rostered NOWHERE, "
            "but %r is already held by live session %.8s: a claude process "
            "holds it or may, or the census could not prove it closed. A pane "
            "that inherited another seat's HELM_CHAT_NAME arrives in exactly "
            "this shape. Fix: unset/re-export HELM_CHAT_NAME to the seat you "
            "actually are; if you ARE that seat, close the process holding "
            "that session, or `helm chat seat disown %s %.8s` if none does"
            % (own, str(session), own, str(_live_session_of(own) or "?"),
               own, str(_live_session_of(own) or "")))
def _live_session_of(seat):
    """The session id a roster row is CURRENTLY bound to, or None.

    None covers three different clean states on purpose — no such row, a row
    with no session, and an unreadable roster — because every one of them
    means "no live occupant to displace". A present session is a BINDING, not
    proof of life (the row keeps it after the pane dies): `held_or_unproven`
    decides whether it still displaces a fresh join."""
    try:
        rows = roster() or {}
        keys = canonical_keys(seat, rows)
    except Exception:      # noqa: BLE001 — same never-raise contract as above
        return None
    # ANY EQUIVALENT ROW ANSWERS, ambiguous rosters included: the question
    # is "is the name I claim ALREADY SOMEONE'S", so two case-variant rows
    # are two reasons to say yes, and a raw `.get(seat)` abstained silently.
    for key in keys:
        row = rows.get(key)
        if not isinstance(row, dict):
            continue
        live = row.get("session")
        if live:
            return str(live)
    return None
def _warn_disagreement(session, where):
    """The warn-only rung of the disagreement law, for paths that must never
    block (stop-guard would wedge every Stop; whoname/pending are reads).
    Returns the (own, bound) pair so refusal sites can share the one check.
    One loud line per (session, where) per process — every boundary of a
    disputed pane says so, but a 2s poll cannot spam itself useless."""
    dis = identity_disagreement(session)
    if dis:
        own, bound = dis
        _warn_once(
            "identity-disagreement:%s:%s" % (session, where),
            "IDENTITY DISPUTE (%s): %s\n"
            % (where, _dispute_sentence(own, bound, session)))
    return dis
def _assert_own_seat(claimed, on_behalf=None, session=None):
    """(seat, err) — `--seat` on a DELIVERY verb is an ASSERTION of this
    process's own identity, never a selector for someone else's inbox. Exactly
    chat._seat_actor's law (which already guards the SIGNING verbs), extended
    to the verbs that CONSUME: `helm chat deliver|wait --seat <someone-else>`
    used to drain that seat's rows into the caller's terminal and refresh its
    presence — a probe that both lied about the seat and stole its mail.

    THE UNNAMED CASE USED TO FAIL OPEN, and that was the guard aimed at the
    wrong half. It refused `--seat <other>` when this process DECLARED a name
    and returned the claimed seat untouched when it declared none — so an
    unnamed shell, the one that has proven nothing, could arm any seat's
    beacon and drain its cursor, silently and by contract. A pinned arm said
    so out loud ("an UN-named process may still name any seat"); that contract
    is RETIRED, deliberately, by cross-family ruling on task/994.

    ACTING FOR ANOTHER SEAT IS NOW TYPED: pass an
    `actors.OnBehalfCapability` covering `claimed` (the CLI mints one from
    `--on-behalf`). It is not an identity and does not pretend to be — a
    supervisor arming a beacon for a seat it is standing up is precisely
    on-behalf-of and precisely not "I am that seat".

    A casefold-equivalent assertion returns the process's DECLARED spelling.
    Seat identity treats `Kimi` and `kimi` as one address; letting argv casing
    become the actuator key would split that one seat's beacon election.

    `session` IS THE SECOND IDENTITY SOURCE AND EVERY CALLER ALREADY HELD IT —
    `_env_session()` in `_cmd_wait`, the payload's `session_id` in the deliver
    legs — and passed it to the beacon refusal but not to THIS door, which read
    the env alone and refused a seat helm itself had named (task/2439). With it
    the name comes from `actors.self_identity`, the ONE layer, and the ROW's
    spelling is returned for the same reason the DECLARED branch returns `own`;
    with no session, or zero or many rows, today's refusal stands and carries
    the count. A DECLARED name is never displaced and the mismatch is never
    silent (`_warn_disagreement` here); the disputed-identity REFUSAL stays
    with the actuators that own it."""
    own = own_name()
    if own and session:
        # REPORTED, NEVER SILENTLY OVERRIDDEN — one line per key per process.
        _warn_disagreement(session, "seat assertion")
    if claimed and own:
        if str(claimed).casefold() != str(own).casefold():
            # the seat's own live rename alias IS the seat: `--seat <old>`
            # inside the window arms the canonical row, under its spelling
            if str(live_alias(claimed)[0] or "").casefold() == own.casefold():
                return own, None
            return None, ("helm chat: --seat %r cannot receive for another seat: "
                          "this process is %r. Delivery and presence are "
                          "per-PROCESS facts; to inspect another seat read "
                          "`helm chat seats`." % (claimed, own))
        return own, None
    if claimed:
        from . import actors
        if isinstance(on_behalf, actors.OnBehalfCapability) \
                and on_behalf.covers(claimed):
            return claimed, None
        _state, derived, why = actors.self_identity(session)
        if derived and str(claimed).casefold() == str(derived).casefold():
            # A RESOLUTION OWES THE ADMISSION PASS — actors.admit_rostered.
            return actors.admit_rostered(claimed, derived, session)
        _err = actors.grant_on_behalf("beacon", claimed, stated=False)[1]
        if derived:
            why = ("this session is bound to seat %r on the roster, not %r — "
                   "--seat asserts an identity, it never selects one"
                   % (derived, claimed))
        return None, "helm chat: %s (%s)" % (_err, why)
    return claimed, None
_FOREIGN_WARNED = set()
def _warn_once(key, text):
    """One loud stderr line per key per process — loudness without spamming a
    2s poll into uselessness."""
    if key in _FOREIGN_WARNED:
        return
    _FOREIGN_WARNED.add(key)
    try:
        os.write(2, ("[helm chat] " + text).encode("utf-8", "replace"))
    except OSError:
        pass
def _warn_foreign(what, seat):
    """One loud stderr line per (what, seat) per process. A refused cross-seat
    write must be VISIBLE (never a silent no-op — silence is what let this
    class run for days), and must not spam a 2s poll loop into uselessness."""
    key = (what, str(seat))
    if key in _FOREIGN_WARNED:
        return
    _FOREIGN_WARNED.add(key)
    try:
        os.write(2, ("[helm chat] REFUSED a cross-seat %s: this process is %r, "
                     "not %r — presence and delivery are per-PROCESS facts, so "
                     "a seat may only ever stamp its own row\n"
                     % (what, own_name(), seat)).encode("utf-8", "replace"))
    except OSError:
        pass          # a dead/closed stderr must never raise out of a beat
# THE DERIVATION IS A TRI-STATE, AND COLLAPSING IT IS A BUG CLASS.
#
# "there is no project room for this cwd" and "I could not find out" are
# different facts with opposite safe actions: the first licenses clearing a
# seat's stale home, the second licenses nothing at all. Both used to leave
# this module as a bare None, so every caller that acted on the None was
# acting on a measurement it did not have — an uninstallable git, an
# unreadable directory or a five-second timeout rendered as "looked, and this
# place has no project", which is how a transient failure erases a live seat's
# home room.
#
# The three statuses are the whole contract; `derive_home_room` keeps its
# two-state shape for the callers that only ever wanted "a room or nothing".
DERIVE_OK = "ok"            # looked; this cwd has a project room
DERIVE_NONE = "none"        # looked; this cwd has NO project room
DERIVE_UNKNOWN = "unknown"  # could NOT look — asserts nothing about the cwd
_GIT_SELECTION_ENV = (
    "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_NAMESPACE", "GIT_CEILING_DIRECTORIES")
# The durable stamp for a room that was DELIBERATELY emptied by a measured
# DERIVE_NONE, as distinct from a seat that never carried a room at all. An
# absent stamp is "no opinion" and lets a later resolution supply one; this
# says the question was ASKED and the answer was nothing.
ROOM_CLEARED = "cleared"


def _checkout_marker_state(cwd):
    """PRESENT/ABSENT/UNKNOWN for the bounded supported-checkout walk."""
    from . import vcs
    path = vcs.resolved_target(cwd)
    if path is None:
        return vcs.UNKNOWN
    for _edge in range(vcs._MAX_WALK + 1):
        unreadable = False
        for _name, marker in vcs.MARKERS:
            state = vcs.marker_state(os.path.join(path, marker))
            if state == vcs.PRESENT:
                return vcs.PRESENT
            unreadable = unreadable or state == vcs.UNKNOWN
        if unreadable:
            return vcs.UNKNOWN
        parent = os.path.dirname(path)
        if parent == path:
            return vcs.ABSENT
        path = parent
    return vcs.ABSENT


def _git_root_typed(cwd):
    """(status, root) — the canonical checkout root plus whether the answer is
    a MEASUREMENT or an admission of ignorance. See the tri-state note above.

    The classification is deliberately locale-independent: it never reads
    git's translated message text. A nonzero first probe is NONE only when no
    `.git` marker exists from cwd to the filesystem root; when a marker exists,
    git failed to inspect a checkout and the answer is UNKNOWN."""
    if not cwd:
        # No cwd is not a cwd with no project — it is no question asked. The
        # old code fell back to `git -C "."`, which derives from the CALLING
        # process's own tree: `derive_home_room(None)` inside a helm seat
        # returned that SEAT's room and handed it to a caller asking about
        # somebody else's (or nobody's) directory. safe_cwd()'s docstring has
        # promised "treated as un-homed" the whole time.
        return DERIVE_UNKNOWN, None
    if not os.path.isdir(cwd) or not os.access(cwd, os.R_OK | os.X_OK):
        return DERIVE_UNKNOWN, None     # nothing to look at, or may not look
    try:
        import subprocess
        from . import vcs
        env = {k: v for k, v in os.environ.items()
               if k not in _GIT_SELECTION_ENV}
        inside = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--is-inside-work-tree"],
            capture_output=True, text=True, timeout=5, env=env)
        if inside.returncode != 0:
            status = _checkout_marker_state(cwd)
            return (DERIVE_NONE if status == vcs.ABSENT
                    else DERIVE_UNKNOWN), None
        if inside.stdout.strip() != "true":
            return DERIVE_NONE, None
        common = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--git-common-dir"],
            capture_output=True, text=True, timeout=5, env=env)
        if common.returncode != 0:
            return DERIVE_UNKNOWN, None
        raw = common.stdout.strip()
        if not raw:
            return DERIVE_UNKNOWN, None
        path = (raw if os.path.isabs(raw)
                else os.path.abspath(
                    os.path.join(os.path.realpath(cwd), raw))).rstrip(os.sep)
        if os.path.basename(path) == ".git":
            return DERIVE_OK, os.path.dirname(path)
        bare = subprocess.run(
            ["git", "--git-dir", path, "rev-parse", "--is-bare-repository"],
            capture_output=True, text=True, timeout=5, env=env)
        if bare.returncode == 0 and bare.stdout.strip() == "true":
            # Every worktree attached to one bare common-dir shares this root.
            return DERIVE_OK, path
        # A submodule's common dir is <super>/.git/modules/<name>; its own
        # top-level remains the identity root. Odd non-bare git-dir layouts do
        # too; bare repositories without a worktree were rejected above.
        top = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=5, env=env)
        if top.returncode != 0:
            # Same law as the common-dir leg: the work tree is already
            # CONFIRMED and its common dir already read, so this is git
            # failing to finish the answer, never the cwd answering "no
            # project".
            return DERIVE_UNKNOWN, None
        return (DERIVE_OK, top.stdout.strip()) if top.stdout.strip() \
            else (DERIVE_UNKNOWN, None)
    except Exception:
        # git could not be RUN — missing binary, timeout, EPERM. This asserts
        # NOTHING about the cwd, and must never license clearing a room.
        return DERIVE_UNKNOWN, None


def _git_root(cwd):
    """The canonical checkout root, including normal worktrees + submodules.
    Bare repos and odd non-worktree layouts are not project contexts. The
    two-state view of `_git_root_typed` — a caller that must tell "no repo"
    from "could not look" reads the typed one."""
    return _git_root_typed(cwd)[1]
def _fingerprinted_project(label, root):
    """A readable label plus canonical-path fingerprint that survives pk.slug's
    60-char cap. Used for every unregistered root and only those registered names
    whose normalized room label collides with another distinct registered path."""
    import hashlib
    real = os.path.realpath(root).rstrip(os.sep)
    fingerprint = hashlib.blake2b(
        real.encode("utf-8"), digest_size=8).hexdigest()
    base = pk.slug(label)[:60 - len(fingerprint) - 1]
    return "%s-%s" % (base, fingerprint)
def _path_project(root):
    """Stable fallback identity for an unregistered checkout, independent of
    scanner state. Registry adoption remains the deliberate human-naming seam."""
    real = os.path.realpath(root).rstrip(os.sep)
    return _fingerprinted_project(os.path.basename(real), real)
def _project_from_root(root):
    """The canonical HELM project for one already-measured checkout root."""
    try:
        from . import registry
        projects = registry.load().get("projects") or {}
        real = os.path.realpath(root)
        paths = {
            name: os.path.realpath(row.get("path"))
            for name, row in projects.items() if row.get("path")
        }
        for name, path in paths.items():
            if path != real:
                continue
            room = pk.slug(name)
            collision = any(
                other_path != real and pk.slug(other) == room
                for other, other_path in paths.items()
            )
            return _fingerprinted_project(name, real) if collision else name
        return _path_project(real)
    except Exception:
        return _path_project(root) or None


def _git_project_typed(cwd):
    """(status, project) from one git-root measurement, never a re-probe."""
    status, root = _git_root_typed(cwd)
    if status != DERIVE_OK:
        return status, None
    project = _project_from_root(root)
    return (DERIVE_OK, project) if project else (DERIVE_UNKNOWN, None)


def _git_project(cwd):
    """Two-state compatibility projection for callers that never act on None."""
    return _git_project_typed(cwd)[1]


def _room_from_project(project):
    room = pk.slug(project) if project else None
    return None if not room or room == "main" else room


def derive_home_room(cwd):
    """The seat's DEFAULT project room, or None for absent/unknown legacy reads."""
    return _room_from_project(_git_project(cwd))


def derive_home_room_typed(cwd):
    """(status, room), preserving cannot-look through every derivation hop."""
    status, project = _git_project_typed(cwd)
    if status != DERIVE_OK:
        return status, None
    room = _room_from_project(project)
    return (DERIVE_OK, room) if room else (DERIVE_NONE, None)
def safe_cwd():
    """os.getcwd() failing OPEN to None when the process cwd no longer exists
    (a pruned lane worktree is a ROUTINE lifecycle state here, not an error).
    Every homing call site must use this instead of a bare os.getcwd(): an
    eager getcwd in the chat/hook prologue crashed every default chat verb and
    all three delivery hooks for a deleted-cwd session, BEFORE any fail-open
    guard could catch it. resolve_homing/derive_home_room treat None as
    un-homed, so the session keeps working (in #main) instead of dying."""
    try:
        return os.getcwd()
    except OSError:
        return None
def resolve_homing(cli_room=None, cwd=None):
    """THE one home-room precedence — every writer resolves through here and
    write_roster is the one enforcement gate behind it. The bug-class this
    kills: multiple derivations of one truth scattered a live roster's homes
    across 'main' (a defaulted mirror write), '<project>' (a cwd derivation)
    and '<env room>' (the launch seam) for seats of the SAME team. Order:
      1. an explicit CLI/operator room (cli_room)          -> explicit
      2. HELM_CHAT_ROOM (the launch seam) — explicit unless the seam stamped
         HELM_CHAT_ROOM_SOURCE=derived                     -> explicit/derived
      3. the cwd's git-project room (derive_home_room)     -> derived
    Returns (room, source); (None, None) = un-homed. Paired law (enforced in
    write_roster): a derived value may NEVER overwrite an explicit/operator
    one, so a re-join/resume/mirror can never downgrade a deliberate home."""
    if cli_room:
        return cli_room, "explicit"
    env_room, env_source = home.env_pair("CHAT_ROOM", "CHAT_ROOM_SOURCE")
    if env_room:
        return env_room, ("derived" if env_source == "derived" else "explicit")
    room = derive_home_room(cwd)
    return room, ("derived" if room else None)


# TWO JOBS, TWO NAMES — they used to share one, which is how the bug hid.
#
# `_OWNER_NAME` is a PIN: a fixed answer that overrides derivation entirely.
# Nothing in production sets it; five test files do, to hold the owner handle
# still while they exercise a surface. That seam is preserved exactly.
#
# THE MEMO IS GONE, AND IT WAS MINE. I added `_OWNER_NAME_CACHE` in this lane
# keyed on (authored path, cwd), and a review then found FOUR narrowings of
# that key in sequence: git's documented config FILES missed command-scope
# injection; the GIT_CONFIG_ namespace missed GIT_DIR; the GIT prefix missed
# that `owner_name` caches its FINAL answer, whose fallback is
# getpass.getuser() reading LOGNAME/USER/LNAME/USERNAME, and whose git leg can
# be re-pointed by PATH. Each time I called the narrowing DERIVED and each
# time the sentence claimed "every input the answer depends on".
#
# MEASURED, which is what settles it: the memo saves 1.77 ms on a function
# with FOUR call sites and no loop. It bought microseconds on a cold path and
# cost four review rounds over a key that cannot be enumerated correctly,
# because the answer's inputs are the union of a registry file, git's whole
# config resolution, PATH, and four getpass variables. A cache whose key
# cannot be stated is not a cache, it is a wrong-answer generator with a
# hit rate. Derive every time.
#
# `_OWNER_NAME` remains a PIN: a fixed answer that overrides derivation
# entirely. Nothing in production sets it; five test files do, to hold the
# owner handle still while they exercise a surface. That seam is untouched.
_OWNER_NAME = None
_HANDLE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


# The git-config selectors that change what `git config --get user.name`
# returns for a FIXED cwd: the global config (GIT_CONFIG_GLOBAL, else
# XDG_CONFIG_HOME/git/config, else HOME/.gitconfig) and the system config
# (GIT_CONFIG_SYSTEM, GIT_CONFIG_NOSYSTEM). Derived from what git documents
# it reads, not from what a cache key felt like it needed.
# OMITTED is not None — see _git_owner_handle. A module-level singleton so
# the distinction survives every call path, including keyword callers.
_CWD_UNSET = object()


def _git_owner_handle(cwd=_CWD_UNSET):
    """`git config --get user.name`'s first token, lowercased — a chat HANDLE
    derived from the identity the human already chose for git (the cargo/npm
    convention: read git's user.name, never ship an author). None when git is
    absent, unset, or the token is not handle-shaped. Routed through the vcs
    SEAM (capture — stripped-stdout-or-None, exit deliberately unconsulted),
    never a direct git spawn: the spawn census pins those. The backend target
    is the CWD, deliberately: identity is a property of where the human works
    (a repo-local user.name override in the cwd's repo is still their chosen
    name there), so cwd-derivation is the stated intent, not a silent default.

    THE CWD IS HANDED IN, never re-read here. `owner_name` already captures it
    through `safe_cwd`; this function used to call a bare `os.getcwd()` OUTSIDE
    its try, so a deleted lane worktree — the routine lifecycle state
    `safe_cwd` exists for — raised FileNotFoundError straight out of
    `owner_name` instead of degrading to the login fallback the docstring
    promises. No directory to ask means NO ANSWER FROM GIT (None), not a
    crash: the caller's own fallback ladder is the degrade path.

    OMITTED AND None ARE DIFFERENT ANSWERS, and collapsing them made the
    paragraph above FALSE in the one case it was written for. `cwd=None` used
    to mean "no argument given, sample it yourself", so a caller that had
    ALREADY looked and found no cwd — `safe_cwd()` returning None, the exact
    deleted-worktree state this seam exists for — handed in that None and got
    a SECOND read. Two samples of a moving value inside one derivation, and
    the second could see a directory the first did not: a probe
    [None, /repo-b] derived a name from repo-b that the caller never observed.

    The sentinel separates them. Omitted means SAMPLE; None means I LOOKED
    AND THERE IS NONE. That is the same defect as `is_agent(None, None)` one
    subsystem over — a failed read and a real answer wearing one value."""
    from . import vcs
    if cwd is _CWD_UNSET:
        cwd = safe_cwd()
    if cwd is None:
        return None                      # unlinked cwd: nothing to ask git in
    try:
        got = vcs.backend(cwd).capture(cwd, "config", "--get", "user.name",
                                       timeout=5)
    except Exception:
        return None                      # a degraded/non-git backend: derive on
    tok = ((got or "").split() or [""])[0].lower()
    return tok if _HANDLE_RE.match(tok) else None


def owner_name():
    """The owner's display handle — DERIVED from the environment, never shipped
    in code (the git/cargo way): the host's authored `owner_name`
    (registry-authored `host` block) -> git config user.name's first token
    lowercased -> the unix login. An UNREADABLE authored
    layer degrades LOUDLY to derivation rather than refusing: BOTH sides of the
    owner seam — the posting default (web/human) and the recognition set
    (owner_names) — resolve through THIS function, so a degrade stays coherent
    across the seam (the owner still posts under a name the rails recognize)."""
    if _OWNER_NAME:
        return _OWNER_NAME                     # explicit pin wins outright
    # ONE READ OF THE CWD, captured here and handed to the derivation. Reading
    # it twice is how the deleted-cwd branch below became a lie: the second
    # read raised out of the whole function instead of degrading to the login
    # fallback. `_git_owner_handle` distinguishes an OMITTED cwd (sample it)
    # from a captured None (I looked; there is none), so passing the capture
    # is the whole contract.
    #
    # The memo this comment used to also describe is GONE — its key could not
    # be stated — and the `home` import that served it went with it.
    cwd = safe_cwd()
    from . import registry
    name = None
    try:
        v = registry.authored_host().get("owner_name")
        if isinstance(v, str) and v.strip():
            name = v.strip().lower()
    except registry.AuthoredUnreadable as e:
        print("helm chat seats: authored layer unreadable (%s) — owner name falls "
              "back to git/login derivation (config recoverable from its "
              ".corrupt backup)" % e, file=sys.stderr)
    name = name or _git_owner_handle(cwd)
    if not name:
        try:
            name = getpass.getuser().lower()
        except Exception:
            name = "owner"
    return name


def owner_names():
    """Display names the owner rails post under. HELM_CHAT_OWNER_NAMES csv
    overrides; default = owner_name() (the derived handle the owner surfaces
    post under — SAME resolver as the posting side, the seam invariant) + the
    unix login."""
    raw = home.env("CHAT_OWNER_NAMES")
    if raw is not None:
        override = {n.strip().lower() for n in raw.split(",") if n.strip()}
        # AN EXPORTED-BUT-EMPTY OVERRIDE IS A MISCONFIGURATION, NEVER A
        # STATEMENT THAT THE OWNER HAS NO NAMES. `raw is not None` alone took
        # this branch for "" and for " , , ", returning an EMPTY SET — and an
        # empty set is not inert downstream: _owner_signal builds its mention
        # regex as `... if names else None`, so every @-mention of the owner
        # stopped counting and his mention badge read 0 with nothing logged.
        # Measured 2026-09-09: unset -> mentions 2; HELM_CHAT_OWNER_NAMES=""
        # -> mentions 0 on the same two rows (owner_unread stayed correct at
        # 2, which is why the failure is quiet). An override that names nobody
        # falls back to the derived default, exactly as if it were unset; a
        # deliberate override still wins.
        if override:
            return override
    names = {owner_name()}
    try:
        names.add(getpass.getuser().lower())
    except Exception:
        pass
    return names
def _delivery_pause(seat, session=None):
    """The current proxywatch-owned credential wall for one delivery process.

    This is read on EVERY delivery attempt, not frozen into the beacon process:
    an already-armed waiter pauses on the next measured dark record and resumes
    on the next measured HEALTHY record without a restart. Runtime family comes
    only from the exact session's verified launch stamp; co-named sessions never
    inherit whichever provider label joined last.
    """
    # DEFERRED, to close the identity<->roster cycle at its thinnest
    # leg. See this module's docstring: the cycle is real and was
    # measured; this is the smaller side of it.
    from .seats import runtime_for_session
    try:
        from . import proxywatch
        runtime, verified = runtime_for_session(
            (seat_row(seat)[0] or {}), session)
        pause, _err = proxywatch.delivery_pause(
            seat, runtime=runtime, runtime_verified=verified)
        return pause
    except Exception as exc:
        # A proxy observer/lock failure cannot invent recovery. Conventional
        # proxy family names fail closed; direct/unknown names have no wall.
        try:
            from . import seat as seatmod
            family, _err = seatmod._seat_family(str(seat or ""))
        except Exception:
            family = None
        return ({"family": family, "state": "UNKNOWN", "stale": True,
                 "observer_error": exc.__class__.__name__} if family else None)
@contextlib.contextmanager
def _delivery_guard(seat, session=None):
    """Pause ordered with its mutation; DELIVERY_BUSY when the lock is busy."""
    from . import proxywatch
    with proxywatch.delivery_state_guard(wait=proxywatch.delivery_wait()) as held:
        yield _delivery_pause(seat, session) if held else proxywatch.DELIVERY_BUSY
def deliverable(m, seat, room="main", scope=None, ambient=True, beacon=False):
    """Does this row reach `seat` at a tool boundary, given the ROOM it sits
    in? The beacon-scope law (premise beacon-scope-mentions-plus-home-room-
    owner-posts-not-all), top to bottom:
      * reaction additions: the reacted-to row's author only (`tfrom`, signed
        into the reaction), at mention tier in any room. Self-reactions and
        reaction-removal tombstones wake nobody.
      * AMBIENT rows and the seat's own posts: never. An ambient row
        ({ambient}: the todo mirror's status line) is machine state a teammate
        PULLS — it renders everywhere and wakes nobody, including in a home
        room, where the rule below would otherwise hand every plain row to
        every seat on the team.
      * a {dm} row: the EXACT-token recipient only (casefold — never a
        substring, never a slug fold), whatever lane it sits in.
      * an @seat mention: ANY room, ALWAYS — checked before mute, because a
        direct address is never noise.
      * on the idle beacon only, a muted room (the seat's roster "mute" list):
        nothing further. Tool-boundary delivery is cheap context inside an
        already-running turn and never consults the wake mute.
      * the seat's HOME room (roster home_room): every remaining row from a
        person or a seat, the owner's included — full-surface for its own
        team — but ONLY under ambient=True (the boundary/pending default).
        A SUBSYSTEM's plain row (machine_senders) is pulled, never pushed.
        ambient=False is the idle BEACON's default (wait --follow): it drops
        this whole tier, so a plain home-room row wakes nobody while
        everything above (and the @all rule below) still does.
      * {home, main}: @all broadcasts only. Owner-rail posts (origin web/tui)
        do NOT wake here (owner steer 2026-07-21: mentions + home-room are
        enough — an owner post reaches a seat via an @mention or its own home
        room, never as a plain main broadcast). NOT fleet-wide: a side room's
        @all drafts nobody homed elsewhere.
      * anything else (foreign-room chatter, incl. non-mention owner posts
        outside home): never (noise law).
    scope=None computes seat_scope here — hot paths pass it precomputed."""
    if m.get("restored"):
        # A journal-restored row is HISTORY, never a wake: it carries
        # restored=1 from _restored_row, and every delivery surface keys on
        # row id — so a reconstruction (new id by the hash, no original id in
        # the journal) is indistinguishable from an unseen row and would mint
        # a fresh addressed-row obligation, pierce mute as a mention, and
        # wake an armed beacon. Measured twice 2026-08-03: an owner directive
        # from Jul 21 woke the integrator a step from acting on a 12-day-old
        # instruction, and two seats spent a live exchange designing inside a
        # 35-hour-old replayed meld. It stays fully READABLE as history —
        # this is a delivery-path drop, never a visibility one.
        return False
    if m.get("ambient") or m.get("ack"):
        return False        # machine state the sender pulls, never a wake
    frm = str(m.get("from") or "")
    sc = scope if scope is not None else seat_scope(seat)
    names = seat_names(seat, sc)   # the seat AND its live rename aliases
    if m.get("react"):
        # A reaction is a direct address of the TARGET row's author. `tfrom` is
        # signed into the reaction digest, so this does not trust rendered text
        # or re-resolve a mutable room ordinal. The toggle-off tombstone is
        # readable history, not a new reaction, and wakes nobody.
        return not m.get("un") and not names_match(frm, names) \
            and names_match(m.get("tfrom"), names)
    text = m.get("text")
    if not text:
        return False
    if not isinstance(text, str):
        text = ""           # a malformed non-string text (foreign/corrupt jsonl
                            # row) carries no @mention or broadcast token and
                            # must never make a regex .search() raise mid-scan
                            # (that would strand the whole backlog behind it).
                            # It still delivers where dm/rfrom/home-room address
                            # it; deliver() then skips it LOUDLY if its own
                            # render fails. Valid rows (str text) are unaffected.
    if names_match(frm, names):
        # Own-post suppression casefolds like EVERY seat-identity match here
        # (roster keys, mentions, dm, rfrom): after a case-only rename
        # (kimi -> Kimi) the seat's pre-rename rows still carry the old
        # casing, and an exact-case check would let the seat wake on its own
        # reply — the precise identity transition the rfrom rule below
        # protects (an xrev of e2d0af4 "chat: close the three
        # reply-wake FIX findings from codex xrev of 88629b1", 2026-07-21).
        return False
    if m.get("dm"):
        # exact-token recipient (casefold only), OR the row sits in the
        # seat's OWN lane — the lane is the routing truth, so a rename's
        # carried-over history (rows naming the old token) still delivers
        return (names_match(m["dm"], names)
                or (bool(seat) and room == dm_lane(seat)))
    if any(_mention_re(n).search(text) for n in names):
        return True
    rf = str(m.get("rfrom") or "")
    if rf and names_match(rf, names):
        # A REPLY to this seat's row is a direct address of its author — the
        # same tier as an @mention, any room, before mute. This inverts the
        # original "threading is invisible to the beacon" law deliberately:
        # the owner's stated WHY for replies was "I'm tired of typing agent
        # names to mention" (2026-07-22) — replying INSTEAD OF mentioning is
        # the feature, so a reply that wakes nobody delivers the mechanism
        # while dropping its purpose. rfrom is the parent's recorded author,
        # stamped at post time; only the parent's author wakes, so a reply
        # stays quieter than the mention it replaces ever was. Casefold, like
        # every seat-identity match here (roster keys, mentions, dm): a
        # case-only rename must not silently drop direct reply delivery.
        return True
    if beacon and room in sc["mute"]:
        return False
    home_r = sc.get("home")
    if ambient and home_r and room == home_r and not is_machine(frm):
        # The home-room full-surface tier is AMBIENT-scope only. The boundary
        # nudge and the pending gate keep it (a BUSY seat reads its team
        # channel for free between tool calls); the idle beacon drops it by
        # DEFAULT — an ambient wake burns a full turn per row (premise
        # mute-busy-home-room-trust-mentions, owner-confirmed 2026-07-22:
        # a seat in a busy home room trusts the mention culture) and the
        # default must encode that canon, not contradict it (owner directive
        # 2026-07-29: "i dont think they shold wake on every post in #helm
        # chat"). ambient=False falls THROUGH to the {home, main} @all check
        # below, so a home-room @all broadcast still wakes. So does a
        # SUBSYSTEM's plain row (machine_senders): it is pulled, not pushed.
        return True
    if room != "main" and room != home_r:
        return False
    # @all broadcasts still wake in {home, main}. Owner-rail posts NO LONGER
    # auto-wake (owner steer 2026-07-21: mentions + home-room are enough — an
    # owner post reaches a seat only via an @mention or its own home room, never
    # as a plain main-room broadcast). OWNER_RAILS/owner_names stay for owner
    # IDENTITY (forgery defense) elsewhere; owner-posts are simply not a wake
    # class. bug-class superseded: beacon-owner-post-wake-is-noise.
    return bool(_BROADCAST.search(text))
def _same_reaction_target(a, b):
    """Same canonical target as chat's signed reaction + `_react_state`:
    `(tts, tfrom)`. Reactions do not carry a room ordinal, rendered text, or a
    target row id, so the beacon composes with their existing durable identity
    rather than inventing a second grouping key."""
    return a.get("tts") == b.get("tts") \
        and recipient_matches(a.get("tfrom"), b.get("tfrom"))
def _reaction_wake_body(rows, room="main"):
    """One bounded, honest delivery body for a contiguous reaction burst.

    The target comes FIRST, so clipping can never erase which row woke the seat.
    Include as many reactor+emoji pairs as fit, then name the omitted count and
    the exact pull surface — never silently truncate identities behind an
    ellipsis or point a foreign-room wake at main."""
    target = rows[0]
    prefix = "%s@%s ← " % (
        chat._dsan(target.get("tfrom") or "?"),
        _scrub(str(target.get("tts") or "?")))
    acts = ["%s %s %s" % (
        chat._dsan(r.get("from") or "?"),
        "un-reacted" if r.get("un") else "reacted",
        _scrub(str(r.get("react") or "?"))) for r in rows]
    pull = ("helm chat read --dm" if room.startswith(chat.DM_PREFIX)
            else "helm chat read" if room == "main"
            else "helm chat read --room %s" % _scrub(str(room)))
    shown = []
    for i, act in enumerate(acts):
        rest = len(acts) - i - 1
        suffix = " (+%d more reactions — %s)" % (rest, pull) if rest else ""
        candidate = prefix + "; ".join(shown + [act]) + suffix
        if len(candidate.encode("utf-8")) > MAX_BYTES:
            break
        shown.append(act)
    rest = len(acts) - len(shown)
    suffix = " (+%d more reactions — %s)" % (rest, pull) if rest else ""
    return _clip(prefix + "; ".join(shown) + suffix)
