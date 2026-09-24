"""The owner's away posture: one sentinel, written and removed by HIS word.

WHY A VERB AND NOT AN INFERENCE. Away is a fact about a person, and the only
authority on it is that person. This writes a flag when they say so, removes it
when they say so, and does nothing else.

NOTHING HERE INFERS POSTURE. There is no
clock, no activity heuristic, no "quiet for twenty minutes" rule, and no second
sentinel. Away is DECLARED and it is REVOKED, both by a human typing a word.
Declaring the posture is a decision; inferring it is a guess about someone
else's attention. Twenty minutes of silence is a man thinking, and a surface
that fires on it interrupts exactly the person it exists to serve.

WHY THE WRITER OWNS THE PATH AND THE READER IMPORTS IT. The progress chart read
this flag first and so defined the path first, which had it backwards: a
sentinel belongs to whatever WRITES it, or two modules end up agreeing by
coincidence about a filename. Readers import MARKER_NAME/marker_path from here.

A PHONE IS THE DOOR, which is why the verb is one word each way. Away matters
exactly when the person is not at a terminal. `helm away` and `helm back` are
one pair of doors; the web console's away card is the other, and the owner's
own, because he does not type verbs. Both write through `declare` and `lift`
below, so there is one writer whichever door he uses.

WHAT PERSISTS, STATED ONLY AS FAR AS IT IS IMPLEMENTED. The FLAG is the state
and the only state: while the file exists the posture holds. Its readers are
the progress chart (ownerchart), the per-turn inject (ownernotice, once per
change per session), the web card and `helm away status`, and every one of
them reads this file. There is deliberately no second record: two
representations of one boolean is how two surfaces come to disagree about
whether a person is at their desk, and a record nobody reads that can
contradict the marker is a contradiction surface rather than a notice. The
owner's fleet NOTICE (ownernotice) is a different fact — his words, not
whether he is here — and it lives in its own file beside this one.

THE FLAG'S LIFETIME IS THE CHAT STORE'S, AND THIS MODULE DOES NOT KNOW WHAT
THAT IS. It publishes into whatever directory chat resolves, and that resolver
picks a PATH — it never asks the filesystem underneath what kind it is. So
whether a reboot clears the flag is a property of somebody else's mount, not a
promise available to make here, and HELM_HOME or HELM_CHAT_DIR can move it
anyway. This module therefore promises no durability at all in either
direction: the flag exists or it does not, it is never reset from inside, and
whoever owns the store owns what its lifetime means.

THE THREAT MODEL IS NAMED RATHER THAN IMPLIED: the bar
for this marker is a 0700 directory owned by the caller. The parent is checked
for symlink, owner and group/other-writability before publishing, and the
marker is created atomically, and no component of the path above it may be a
symlink, foreign-owned, or writable by others without the sticky bit. It is
NOT hardened against a local attacker who can win a path RACE inside that
directory — closing that needs openat against a directory fd held across the
whole publish. The bar stops short of it on proportionality, stated here so a
later reader can disagree deliberately: this artifact is a posture flag in the
operator's own
private directory, the worst outcome of losing such a race is that a CHART
renders when it should not, and anyone positioned to win it already has write
access to the code that draws the chart. That is a deliberate limitation
rather than an oversight.
"""

import os
import stat
import sys
import tempfile

# ONE SENTINEL. The name carries NO ROOM: chat.marker_path builds the unread
# flag as slug(room) + ".owner-unread" because THAT marker is a fact about a
# room. Away-ness is not — he is away from everything or from nothing.
MARKER_NAME = "owner-away"


class MarkerUnresolvable(RuntimeError):
    """The chat store could not be located, so no flag path can be honest."""


def marker_path():
    """The one away flag's path, derived from chat's own store accessor.

    DERIVED, NOT LITERAL, because that store is under a namespacing migration
    and a hardcoded path would silently stop matching the day it lands. The
    derivation also inherits helm's isolation law (chat.py:222) — a redirected
    HELM_HOME moves the flag with it.

    IT FAILS CLOSED, AND A FALLBACK PATH HERE WOULD DO THE OPPOSITE. A literal
    default — /dev/shm or any other — looks like robustness and buys this:
    under a redirected HELM_HOME where the chat import fails, it sends the
    WRITE to the LIVE shared path, so the one construct added for safety is the
    one that punches through the isolation claimed two paragraphs above it.
    A resolver that cannot find the store must refuse,
    because writing a posture flag to a DIFFERENT path than the reader reads is
    worse than not writing one at all: it marks a man away somewhere nobody
    looks, and on a shared box it marks him away for everyone.
    """
    try:
        from . import chat
        d = chat.chat_dir()
    except Exception as e:
        raise MarkerUnresolvable("cannot locate the chat store: %s" % e)
    if not isinstance(d, str) or not d.strip():
        raise MarkerUnresolvable("chat store path is empty")
    return os.path.join(d, MARKER_NAME)


def declared_by(marker=None):
    """Who declared the current posture, or None when nothing is declared.

    THE READER THE ATTRIBUTION WAS MISSING. The marker recorded WHO from the
    moment the admission gate was deleted — and nothing consumed it, so the
    visibility that replaced the gate lived only in a file nobody opened.
    Recording a fact and wiring no reader is the same defect as promising a
    reader that does not exist, and this file has now produced both.
    """
    # O_NOFOLLOW, BECAUSE A MARKER THAT IS A SYMLINK IS NOT ATTRIBUTION. The
    # writer publishes with os.link, and is_away answers on lexists — so a
    # SYMLINK sitting at the marker path is authoritative AWAY to both of
    # them. A plain open() then FOLLOWS it and reads whatever it points at, so
    # any file on the box whose first line begins "declared by " becomes the
    # name the chart prints beside the operator's posture. Refusing to follow
    # makes that case answer None, and None already renders as the unattributed
    # "you are marked away" — the posture stays true and the name stays unsaid.
    # A MISSING O_NOFOLLOW IS NOT "NO FLAG", IT IS "NO PROTECTION". The old
    # getattr(os, "O_NOFOLLOW", 0) degraded to 0 on a runtime lacking the
    # constant and the open SUCCEEDED, following the very symlink the comment
    # above explains we must not follow — so the attribution guard was inert
    # exactly where it could not be seen. Refusing to answer is the honest
    # degradation: None already renders as the unattributed "you are marked
    # away", so the posture survives and only the name goes unsaid.
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        return None
    try:
        fd = os.open(marker or marker_path(), os.O_RDONLY | nofollow)
    except (OSError, MarkerUnresolvable):
        return None
    try:
        with os.fdopen(fd, encoding="utf-8") as fh:
            line = fh.readline().strip()
    # UnicodeDecodeError IS NOT AN OSError. A marker holding invalid UTF-8
    # raised straight THROUGH this function, past the idempotent retry, and
    # took the whole chart down with it — a malformed byte silently removing
    # a surface the owner reads. An unreadable name is an unattributed
    # posture, never an absent chart.
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    prefix = "declared by "
    if not line.startswith(prefix):
        return None
    return line[len(prefix):].strip() or None


def is_away(marker=None):
    """True while the flag exists. Reads; never writes, never infers.

    A READER THAT CANNOT RESOLVE THE STORE ANSWERS FALSE, not an exception:
    every clean stop of every seat calls this, and the honest answer to "is he
    away" when the instrument is broken is "do not act as if he is". Refusing
    loudly belongs to the WRITER, where a person is present to read it.
    """
    # LEXISTS, NOT EXISTS, SO THE READER AND THE WRITER AGREE. The writer
    # publishes with os.link, which fails EEXIST on a DANGLING SYMLINK and
    # reports "already away"; os.path.exists FOLLOWS that symlink and answers
    # False. Writer says away, reader says not away, and the chart is the one
    # that disagrees with the person's own command. Divergence is the bug —
    # agreement is the fix, and lexists is the question the writer is really
    # asking: is there a name here already.
    if marker:
        return os.path.lexists(marker)
    try:
        return os.path.lexists(marker_path())
    except MarkerUnresolvable:
        return False


def _declarer():
    """(name, provenance) for whoever is declaring. Never refuses.

    THE PROVENANCE IS NOT DECORATION. seats_identity.resolve_identity returns
    (state, name) rather than a bare string for a stated reason: DECLARED,
    ROSTERED and DERIVED are all non-empty and all look identical to
    `if seat:`. Its own rule of thumb covers this case exactly — if being
    wrong about WHO would silently mark work as done for the wrong party,
    DERIVED must not be treated as an identity. A posture flag IS a claim
    about a person, so a derived name recorded as though it were declared
    attributes a declaration nobody made. Reading the env var directly
    collapsed all three states into one string and lost that distinction.

    THIS REPLACED AN OWNER GATE, AND THE REASON IS THAT THE GATE COULD NOT
    DISCRIMINATE. It allowed any process declaring no seat identity, on the
    reasoning that a plain shell is a person — but roster and session derived
    agents can be unnamed too, or can simply unset the variable. So the gate
    refused the honest agents and passed the ones worth stopping, which is a
    DECORATIVE gate: it implied a guarantee it could not keep, and code that
    implies a guarantee is worse than code that admits it has none.

    IT ALSO HAD A FAILURE MODE THAT HURT THE PERSON IT PROTECTED. Gating on an
    identity lookup means an identity seam that breaks can REFUSE `helm back`
    — stranding someone in away posture because an instrument failed, with a
    diagnostic telling them to create the flag they are trying to remove.
    Fail-closed is right for MUTATING someone else's state and wrong for
    LETTING THEM OUT, and one gate cannot be both.

    SO POSTURE IS ATTRIBUTED RATHER THAN GATED. Every declaration records WHO
    made it, in the marker and in the announcement. A machine declaring a
    person away is now VISIBLE instead of blocked — which is the honest
    guarantee, since visibility is the one this can actually keep. The blast
    radius it replaces is small by construction: the flag makes a chart render
    on agent stops, it does not page anyone, and any reader can lift it.
    """
    try:
        from .seats_identity import (resolve_identity, identity_disagreement,
                                     DECLARED, ROSTERED)
        from .home import session_id
        # THE SESSION IS PASSED, OR ROSTERED CAN NEVER ANSWER. resolve_identity
        # tries the declared name, then the session->row map, then derivation —
        # so calling it with no session skips the middle rung entirely and
        # sends a seat that IS rostered straight to DERIVED, which this module
        # then refuses as an identity. The discriminator seats_join records is
        # that a process which IS the seat has a session; `helm away` running
        # in a pane is exactly that process.
        session = session_id()
        # DISAGREEMENT IS ASKED THROUGH THE PREDICATE THAT ALREADY OWNS THE
        # QUESTION, and the reason is not tidiness. I first wrote this check by
        # hand as "is my sid rostered to a different seat", which is a PROPER
        # SUBSET of what identity_disagreement asks — it needs a binding to
        # exist, so a process whose sid is rostered NOWHERE, declaring a name
        # some other LIVE session holds, disagrees with nothing and passes.
        # That subset is the exact shape identity_disagreement records as
        # having been measured open by an adversary: an inherited name plus a
        # fresh sid. A posture flag is a claim about a person, so the name it
        # records must clear both dispute shapes, and only that predicate
        # knows both.
        if identity_disagreement(session):
            return "an unnamed process", "disputed"
        state, name = resolve_identity(session)
    except Exception:
        return "an unnamed process", "unreadable"
    # A DERIVED NAME IS NOT AN IDENTITY HERE, and resolve_identity says so
    # itself: if being wrong about WHO would silently mark work as done for
    # the wrong party, DERIVED must not be treated as one. A posture flag is
    # a claim about a person, so a guessed name recorded as the declarer
    # attributes a declaration nobody made. Recording it with a "(derived)"
    # tag beside it is not a fix: a reader scanning the chart sees the NAME,
    # and a tag next to it does not un-say it.
    if name and state in (DECLARED, ROSTERED):
        return name, str(state).lower()
    return "an unnamed process", "underived"


def _ancestors(d):
    """`d` and every directory above it, leaf first, ending at the root."""
    out, seen = [], set()
    cur = os.path.abspath(d)
    while cur not in seen:
        seen.add(cur)
        out.append(cur)
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return out


def _group_has_others(gid):
    """True / False / None — None when membership CANNOT BE READ.

    THIS USED TO FAIL OPEN AND THE FAIL-OPEN WAS THE DEFECT. The old docstring
    argued that an unreadable group is not a finding, because the private-group
    scheme makes group-write usually mean you alone and refusing ordinary
    machines is its own bug. The reasoning was right and the ENCODING was
    wrong: it returned False for "proven alone" AND for "could not ask", so a
    caller could not tell a measured negative from no measurement. Measured by
    cross-family review: on an interpreter without grp/pwd the guard is
    entirely inert, and the exact 0775 path that REFUSES under CPython ALLOWS
    under GraalPy. A guard that evaporates on one runtime is not a lenient
    guard, it is an absent one.

    So the third state is explicit and the CALLER decides what an unknown is
    worth. Nothing here refuses; nothing here pretends to know either.

    gr_mem IS ONLY THE SUPPLEMENTARY LIST. Users whose PRIMARY gid is this
    group never appear in it, so an empty gr_mem was read as "nobody else"
    for a group that may hold every user on the box. The primary-gid scan is
    the other half of the same question, and a scan that cannot complete
    answers None rather than falling through to False.
    """
    try:
        import grp
        import pwd
        me = pwd.getpwuid(os.geteuid()).pw_name
        entry = grp.getgrgid(gid)
    except Exception:      # noqa: BLE001 — no group service, so NO EVIDENCE
        return None
    if [m for m in (entry.gr_mem or []) if m != me]:
        return True
    try:
        for user in pwd.getpwall():
            if user.pw_gid == gid and user.pw_name != me:
                return True
    except Exception:      # noqa: BLE001 — a partial scan is not a negative
        return None
    return False


def _harden_ancestors(d):
    """None when no directory ABOVE `d` can be swapped, else why not.

    THE LEAF CHECK WAS THE WHOLE CHECK, AND A PATHNAME IS NOT ITS LAST
    COMPONENT. Hardening only the final directory asks whether the flag lands
    somewhere you own while leaving every component that RESOLVES to it
    unexamined — so a symlink or a foreign-owned directory anywhere above it
    re-points the entire path, and the leaf you then inspect (or create) is the
    one inside the attacker's tree. It passes the owner test because you made
    it. This is not the path RACE the module declines to close: it is a static
    property of the pathname, readable once, before anything is written.

    THE RULE PER ANCESTOR IS THE ORDINARY SAFE-PATH ONE, WITH ONE CORRECTION
    THE LIVE RUN FORCED. A component may not be a symlink and must belong to
    you or to root. On mode, the first cut refused any group- or
    other-writable directory without the sticky bit — and that refused the
    ORDINARY CASE: under umask 002 a user's own home tree is created 0775, so
    the guard turned `helm away` into a verb that refuses on a normal machine.
    A guard that refuses everything is not a strict guard, it is a broken one.

    So the question is asked properly: can somebody ELSE replace this
    component. World-writable without the sticky bit always means yes.
    Group-writable means yes only when the GROUP HAS OTHER MEMBERS — under the
    private-group scheme every mainstream distribution ships, your primary
    group is you alone, and 0775 there is the same trust boundary as 0700. The
    sticky bit exempts both, which is what makes a shared root like /tmp safe
    to traverse: anyone may create there, only the owner may replace.

    A GROUP THAT CANNOT BE LOOKED UP IS NOT EVIDENCE OF A THREAT. The
    membership read fails open, because refusing on ABSENCE would downgrade
    every legitimate path on a box with a directory service the process cannot
    reach — the same failure the mode rule above just made in miniature.
    """
    for a in _ancestors(d)[1:]:
        try:
            st = os.lstat(a)
        except OSError:
            continue          # a component that is not there yet cannot be
                              # swapped for one that is; makedirs owns it
        if stat.S_ISLNK(st.st_mode):
            return ("refusing to write posture into %s — %s on the way to it "
                    "is a SYMLINK, and a link can re-point the whole path"
                    % (d, a))
        if st.st_uid not in (os.geteuid(), 0):
            return ("refusing to write posture into %s — %s on the way to it "
                    "belongs to uid %d, who can replace everything below it"
                    % (d, a, st.st_uid))
        if st.st_mode & stat.S_ISVTX:
            continue
        if st.st_mode & stat.S_IWOTH:
            return ("refusing to write posture into %s — %s on the way to it "
                    "is WORLD-writable (%04o) without the sticky bit, so "
                    "anyone can replace what is below it"
                    % (d, a, st.st_mode & 0o777))
        if st.st_mode & stat.S_IWGRP:
            others = _group_has_others(st.st_gid)
            if others is None:
                # UNKNOWN REFUSES. This is the WRITER, where a person is
                # present to read the refusal and widen the permission — the
                # reader half (is_away) still answers False on a broken
                # instrument, deliberately. An unreadable group on a
                # group-writable ancestor is exactly the case where "probably
                # just me" is a guess about who can replace the file below.
                return ("refusing to write posture into %s — %s on the way to "
                        "it is writable (%04o) by group %d and this runtime "
                        "cannot read that group's membership, so whether "
                        "anyone else can replace what is below it is UNKNOWN. "
                        "Tighten the mode (chmod g-w %s) or run where grp/pwd "
                        "resolve." % (d, a, st.st_mode & 0o777, st.st_gid, a))
            if others:
                return ("refusing to write posture into %s — %s on the way to "
                        "it is writable (%04o) by group %d, which has members "
                        "other than you, and any of them can replace what is "
                        "below it" % (d, a, st.st_mode & 0o777, st.st_gid))
    return None


def _harden_parent(d):
    """None when the directory is safe to publish posture into, else why not.

    LSTAT, NOT STAT, AND MODE AS WELL AS OWNER. A uid check alone follows a
    SYMLINK — so a link owned by you pointing at somebody else's directory
    passes it — and it says nothing about a directory the world can write.
    Both are the same failure in the end: a claim about a person, recorded
    somewhere they do not control.
    """
    if not d:
        return None
    # THE ANCESTORS ARE CHECKED BEFORE THE DIRECTORY IS CREATED, not after.
    # makedirs on an unsafe path is itself the write this refuses.
    err = _harden_ancestors(d)
    if err:
        return err
    try:
        if not os.path.isdir(d):
            os.makedirs(d, mode=0o700, exist_ok=True)
        st = os.lstat(d)
    except OSError as e:
        return "cannot inspect %s: %s" % (d, e)
    if stat.S_ISLNK(st.st_mode):
        return ("refusing to write posture into %s — it is a SYMLINK, and a "
                "link you own can point at a directory you do not" % d)
    if st.st_uid != os.geteuid():
        return ("refusing to write posture into %s — it exists under another "
                "uid (%d), and a flag about a person does not go in a "
                "directory they do not own" % (d, st.st_uid))
    # EXACTLY 0700, BECAUSE THAT IS WHAT THE CONTRACT SAYS. This checked only
    # the WRITE bits, so 0755 passed while the documented bar was 0700 — a
    # guard that admits more than its own contract states is worse than a
    # missing one, because the contract is what a later reader trusts.
    if st.st_mode & 0o777 != 0o700:
        return ("refusing to write posture into %s — the bar is a private "
                "0700 directory and this one is %04o"
                % (d, st.st_mode & 0o777))
    return None


#: THE THREE ANSWERS A READER CAN GIVE. is_away collapses UNKNOWN into False
#: on purpose, for the stop seam; `state` keeps it, for every surface that
#: SHOWS posture to a person or a seat. An unreadable flag drawn as "present"
#: is a confident wrong answer about where a man is.
AWAY, PRESENT, UNKNOWN = "away", "present", "unknown"


def state(marker=None):
    """(AWAY | PRESENT | UNKNOWN, why) — the flag, read without collapsing.

    LSTAT, NOT LEXISTS. lexists answers False for a path it could not examine
    (a directory it may not search, a component that is a file), which is
    is_away's documented degrade and exactly the answer a shown surface must
    not give. Only a measured FileNotFoundError is "present"; every other
    failure is UNKNOWN and says why. Reads; never writes, never infers."""
    try:
        path = marker or marker_path()
    except MarkerUnresolvable as e:
        return UNKNOWN, str(e)
    try:
        os.lstat(path)
    except FileNotFoundError:
        return PRESENT, None
    except OSError as e:
        return UNKNOWN, "cannot read the away flag at %s: %s" % (
            path, e.strerror or e.__class__.__name__)
    return AWAY, None


def _owner_door_declarer(door):
    """(name, door) for a write that came through one of the owner's doors,
    or None when `door` is not an OwnerDoor.

    THE DOOR IS A CAPABILITY, NEVER A STRING (ownerasks.OwnerDoor): only the
    web's owner handlers mint one, and a census arm fails any other minting
    site. A caller that passes the word "web" gets no door, and so no owner
    attribution."""
    from .ownerasks import OwnerDoor
    if not isinstance(door, OwnerDoor):
        return None
    from .seats import owner_name
    return owner_name(), door.door


def declare(door=None):
    """Publish the away flag. -> (outcome, detail)

      ("away", None)          the flag was published by this call
      ("already", standing)   a flag was already there; `standing` names its
                              declarer (or None), and nothing was rewritten
      ("refused", why)        nothing was written, and `why` is the sentence

    THE ONE WRITER. `helm away` and the web console's "I'm away" both land
    here, so the two surfaces cannot come to disagree about where the flag is
    or what it says. `door` is an ownerasks.OwnerDoor when the owner acted
    through one of his own doors: the marker then records his name and the
    door. With no door the calling process is ATTRIBUTED (see _declarer), as
    it always was. Anything else is refused: a string is not a door."""
    if door is None:
        who, prov = _declarer()
    else:
        owner = _owner_door_declarer(door)
        if owner is None:
            return "refused", ("refusing to record %r as the declarer: only "
                               "the owner's own web door carries his name, "
                               "and a string is not a door" % (door,))
        who, prov = owner
    try:
        path = marker_path()
    except MarkerUnresolvable as e:
        return "refused", "cannot set away posture: %s" % e
    d = os.path.dirname(path)
    err = _harden_parent(d)
    if err:
        return "refused", err
    # PUBLISHED COMPLETE OR NOT AT ALL, IN ONE OPERATION. Creating the file
    # first and writing it second leaves two hazards no patch can close: a
    # failed write has to be CLEANED UP, and that cleanup can delete a marker
    # somebody else has since published. Writing a temp file and LINKING it
    # into place removes both — the marker never exists in a partial state,
    # and a failure leaves only the temp, which is nobody's posture.
    #
    # LINK RATHER THAN RENAME, and the difference is the whole idempotency
    # story: rename OVERWRITES, silently replacing a standing declarer with a
    # new one on a no-op `away`. link is the atomic CREATE-ONLY primitive —
    # it raises FileExistsError instead, which is exactly the "already away"
    # answer, so idempotency and atomicity come from the same syscall.
    # A UNIQUE TEMP, NOT A PID-DERIVED ONE. `<path>.<pid>.tmp` collides two
    # ways: a crashed run leaves a stale temp that the next same-pid run trips
    # over, and pids are REUSED, so the collision is not even rare on a busy
    # box. mkstemp picks a name nothing else holds and creates it 0600 in one
    # step, in the same directory so the link stays on one filesystem.
    try:
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".away-", suffix=".tmp")
    except OSError as e:
        return "refused", "could not stage the away flag in %s: %s" % (d, e)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("declared by %s (%s)\n" % (who, prov))
        try:
            os.link(tmp, path)
        except FileExistsError:
            return "already", declared_by(path)
    except OSError as e:
        return "refused", "could not set the away flag at %s: %s" % (path, e)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    return "away", None


def lift():
    """Remove the away flag. -> (outcome, detail)

      ("back", None)       this call removed it
      ("already", None)    there was no flag (somebody already lifted it)
      ("refused", why)     it is there and could not be removed

    LIFTING IS ONE SYSCALL AND CANNOT BE REFUSED BY IDENTITY. Anyone may let
    him out, from any surface: the outcome of the unlink IS the answer, and
    FileNotFoundError means it was already lifted, which is success."""
    try:
        path = marker_path()
    except MarkerUnresolvable as e:
        return "refused", "cannot clear away posture: %s" % e
    try:
        os.remove(path)
    except FileNotFoundError:
        return "already", None
    except OSError as e:
        return "refused", "could not clear the away flag at %s: %s" % (path, e)
    return "back", None


def cmd_away(args):
    """away [off|status]|back — DECLARE the operator away, lift it, or read
    it. -> rc

    THE MARKER IS THE ONLY POSTURE TRUTH. Nothing else is written on a
    transition — no notice row, no second record. A second representation of
    one boolean is how two surfaces come to disagree about whether someone is
    at their desk, and an unread one that can contradict the marker under
    interleaving is a CONTRADICTION SURFACE rather than a notice. Having only
    one also means there is no transition to serialise: the link either
    creates the marker or reports the standing declarer, and that is the
    entire state machine. The web console writes through the same `declare`
    and `lift`, so a change made there reads here and the other way round.

    `status` READS: the flag, and the owner's standing fleet notice beside it
    (ownernotice), in the words the web card uses. It writes nothing.
    """
    # STRICT ARITY. `away off junk` used to MUTATE — args[0] matched and the
    # tail was ignored, so a fat-fingered command silently lifted the posture.
    if len(args) > 1 or (args and args[0] not in ("off", "back", "--off",
                                                  "status")):
        print("usage: helm away [off|status]   (or: helm back)",
              file=sys.stderr)
        return 2
    if args and args[0] == "status":
        from . import ownernotice
        for line in ownernotice.status_lines(ownernotice.snapshot()):
            print(line)
        return 0
    if args:
        outcome, detail = lift()
        if outcome == "refused":
            print(detail, file=sys.stderr)
            return 1
        print("welcome back — away flag cleared" if outcome == "back"
              else "already back — no away flag was set")
        _standing_notice()
        return 0
    outcome, detail = declare()
    if outcome == "refused":
        print(detail, file=sys.stderr)
        return 1
    if outcome == "already":
        print("already away — %s" % (detail or "declared"))
        return 0
    # SHORT, BECAUSE THE READER IS ON A PHONE: what happened, how to undo it.
    print("away — the progress chart will show on agent stops while you are out")
    print("say `helm back` when you return")
    return 0


def _standing_notice():
    """One line after a lift when the owner's fleet notice is still standing:
    lifting the flag does not clear his notice, and a person who says he is
    back must not be left thinking the fleet stopped reading his words."""
    try:
        from . import ownernotice
        n = ownernotice.read_notice()
    except Exception:           # noqa: BLE001 — a lift never fails on a read
        return
    if n.get("state") == ownernotice.SET:
        print("your fleet notice is still showing to agents: %s — clear it "
              "from the web console" % ownernotice.quote(n.get("text")))


def cmd_back(args):
    """back — lift the owner's away posture. -> rc

    A SEPARATE VERB RATHER THAN A FLAG, and that is a UX call not a taxonomy
    one: the door is a phone, and `helm back` is four fewer
    keystrokes than `helm away off` at the moment he least wants to type.
    """
    if args:
        print("usage: helm back", file=sys.stderr)
        return 2
    return cmd_away(["off"])
