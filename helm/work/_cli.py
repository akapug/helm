"""helm work — the cli cluster: the `helm work` verb dispatcher.
Moved verbatim from the pre-split helm/work.py.
"""
import json
import os
import re
import sys

from .. import seats
from ..seats_common import ttl_flag
from ._common import DEFAULT_TTL, LANE_RE, RECENT_WRITE_SECONDS
from ._lanes import _worktree_records, find_root, unguarded_inventory
from ._gc import (LANE_LANDED, _rowed_lanes, format_gc_summary, gc_enact,
                  gc_scan, lane_overlaps, phantom_scan, prune_phantom_records,
                  was_reclassified, refresh_trunk, list_rows,
                  post_gc_summary, release_command, trunk_sync)
from ._guard import stale_guard_hooks, guard_remedy, guard_remedy_note
from ._claims import (_infer_lane, _positional, claim, release_lane,
                      release_stale_lane)
from . import _guard
from ._guard import install_guard


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

USAGE = """usage: helm work <verb> [--repo PATH] [--seat S]
  claim <lane> [--ttl N] [--lease ID]   check in: lease + private room —
                                        prints path<TAB>branch<TAB>lease<TAB>ttl.
                                        --ttl is seconds, bare or with one
                                        unit suffix: 3600, 3600s, 90m, 4h, 1d
  release [<lane>] --lease ID [--park] [--stale]  check out: dirty refuses (--park
                                        WIP-commits); landed room retires,
                                        unlanded room + branch stay for triage.
                                        Lost the id? `helm work list` reprints
                                        your own.
  peek <committish> [--json]            read-only look: disposable DETACHED
                                        worktree at exactly that commit under
                                        <repo>-wt/peeks/<sha12> — no lease, no
                                        branch, no lock, and it passes the ref
                                        guard with NO env (the reviewer door;
                                        prints path<TAB>sha)
  peek --drop <path-or-committish>      retire a peek room (occupied, pane-
                                        bound, or dirty REFUSES; never --force)
  gc [--apply]                          housekeeping: keep/triage/rescue/remove
                                        (dry-run; only clean LANDED rooms retire)
  list                                  the room board: worktree registry x
                                        claims, plus YOUR OWN lease ids
  install-guard [--apply] [--profile P] composed deterministic git guards (dry/apply):
                                        rail = shared-tree rail + every pre-commit rung;
                                        leak = the legs EVERY repo owes: never-track
                                        staged-set scan (bulk data, needles) at pre-commit
                                        AND pre-merge-commit (a merge commit never reaches
                                        pre-commit) + host-path pre-push. Recorded as
                                        helm.guard.profile.
                                        NO --profile RESOLVES IN THIS ORDER: what the repo
                                        DECLARED, else what its hooks are RUNNING, else the
                                        identity default (leak in a project repo, rail only
                                        in helm's own checkout -- the rail is opt-in for a
                                        repo helm is being USED on). A flagless install that
                                        would retire what the running profile uses REFUSES
                                        and names the --profile that narrows on purpose.
  stash list|apply|pop|drop|show <msg>  address a stash by its MESSAGE — a
                                        stash@{N} argument is REFUSED (a
                                        position shifts when anyone drops an
                                        entry; an ambiguous message refuses
                                        rather than guessing)"""


_DANGLING_NOTE = (
    "    !! DANGLING CONFLICT: %d unmerged path(s) with NO git operation in\n"
    "       progress — the shape a conflicted `git stash apply/pop` leaves.\n"
    "       `git merge --abort` WILL REFUSE (git sees no merge in progress), so\n"
    "       do NOT reach for it. Before anything else, check that every\n"
    "       conflicted file still has its <<<<<<< markers: if any are resolved,\n"
    "       that is SOMEONE'S WORK and clearing it would destroy it.\n"
    "       Then clear ONLY THE CONFLICTED PATHS, one at a time:\n"
    "         git -C %s restore --staged --worktree -- <conflicted-path>\n"
    "       and re-apply the stash in ITS OWN room.\n"
    "       Do NOT use `reset --hard HEAD` — the prohibition is on this line on\n"
    "       purpose, so grepping for the command cannot surface a line that\n"
    "       reads like an instruction. The marker check above covers the\n"
    "       CONFLICTED paths, but a hard reset discards every OTHER uncommitted\n"
    "       change in the tree too, which the check never looked at. The mismatch\n"
    "       is not hypothetical — it nearly destroyed an unrelated modified test\n"
    "       file in a lane whose conflict had nothing to do with it (2026-07-24).\n"
    "       Refer to a stash by MESSAGE, never `stash@{N}`: an index is a\n"
    "       POSITION and shifts under everyone when any stash is dropped.")


def _reuse_notice(payload):
    """[lines] telling the caller this peek room was ALREADY THERE.

    `peek` converges on an existing room for the same sha rather than
    minting a second one, which is the cheap and intended case — two
    reviewers reading one immutable checkout. What made it a defect is that
    the RETURN VALUE was identical either way, so a second caller could not
    tell it had become a GUEST in a tree with no lease, no branch and no lock
    protecting it. The concrete cost: a cherry-pick in a reused room writes
    conflict markers into the files of a worktree another caller has live
    processes in, and the only surface that names the owner is a REFUSED
    `--drop` — a door the guest has no reason to knock on.

    NOT A REFUSAL, AND DELIBERATELY SO. Sharing an immutable checkout is the
    design; being unable to find out is the bug. The caller gets the room and
    the facts, and decides.
    """
    out = ["helm work: this peek room ALREADY EXISTED — you are a GUEST in a "
           "tree with no lease, no branch and no lock. Anything you write "
           "here lands in whatever another caller is reading."]
    if payload.get("occupancy") != "measured":
        # THE THIRD STATE, AND IT IS NOT "NOBODY IS HERE". An unreadable
        # process census returns the same empty list a genuinely idle room
        # does, and a caller told "0 processes" would act on a measurement
        # nobody made.
        out.append("helm work: occupancy could NOT be measured (the process "
                   "census was unreadable) — this says nothing about whether "
                   "someone is working here")
    elif payload.get("occupants"):
        pids = payload["occupants"]
        out.append("helm work: %d process(es) have their cwd here: %s"
                   % (len(pids), ",".join(str(p) for p in pids)))
    else:
        out.append("helm work: no process currently has its cwd here — but a "
                   "peek room outlives the process that cut it, so this is "
                   "not proof the room is yours")
    if payload.get("dirty"):
        # `dirty()` reads an unreadable checkout as dirty by contract, so this
        # line means "uncommitted bytes, or a checkout that would not answer".
        # Both are reasons to look before writing, which is the whole job.
        out.append("helm work: the room is DIRTY — uncommitted or untracked "
                   "bytes are present (an unreadable checkout reads dirty "
                   "too); someone may be mid-work in it")
    return out


def _blocked_reason(lines):
    """The SKIPPED reason from one gc_enact result, compressed for a summary.

    A planned removal that did not happen has its reason in the enact lines and
    nowhere else; the count alone cannot say whether the estate is clean or
    jammed. Falls back to "unreported" rather than inventing one — a summary
    that guesses why is worse than one that admits it does not know."""
    for ln in reversed(lines or []):
        if "SKIPPED " not in ln:
            continue
        # DO NOT REQUIRE THE CLOSING ") — kept". Measured 2026-08-04 on the
        # first live run of this very feature: an adapter error carried a
        # multi-line JSON body, so the parens never balanced on one line and
        # the summary printed "blocked: unreported" while the reason sat two
        # characters away. A summary that cannot name a reason it HAS is the
        # same defect this function exists to fix, one layer in.
        why = ln.split(" (", 1)[1] if " (" in ln else ln
        why = why.split(" — kept")[0].strip()
        low = why.lower()
        if "pane" in low:
            return "bound pane" if "bound to this room" in low \
                else "pane close failed"
        if "occupied" in low:
            return "occupied"
        if "lease" in low:
            return "lease live"
        if "locked" in low:
            return "locked"
        return why.splitlines()[0][:40].strip() or "unreported"
    return "unreported"


def _show(path):
    """A worktree path is ATTACKER-SHAPED TEXT once it reaches a report.

    The fourth finding on this lane (r4): "raw newline
    paths spoof summary output". A path may legally contain a newline, and
    every line this verb prints is a single-line claim an operator reads as one
    fact — so one embedded newline forges an entire extra "pruned phantom
    record ..." line for a record that was never touched. Same shape as the
    meld-room launder: the value need not be hostile, the FRAME it rides in is
    single-line, and only the emitter can defend it.

    Escaped via repr rather than stripped, because the operator still has to be
    able to identify the complete path. A silently-shortened path is a
    different lie. Printability is the boundary, not a hand-picked control
    list: ESC and the other terminal controls can forge one line without a
    newline."""
    return path if path.isprintable() else repr(path)


def _tree_word(row):
    """The tree's state in ONE word. CONFLICT and DANGLING are called out rather
    than folded into "dirty", because that folding is exactly what hid a dangling
    conflict in the shared checkout five times in one day — a room with edits and
    a room stuck mid-conflict need completely different remedies."""
    if row.get("dangling_conflict"):
        return "DANGLING"
    if row.get("conflicts"):
        return "CONFLICT"
    return "dirty" if row["dirty"] else "clean"



# EVERY FLAG EACH VERB ACTUALLY READS, not every flag its help text mentions.
# Derived by reading each verb's body; the arms drive the ACCEPT side of every
# entry, because a table that is too NARROW refuses a working call and that is
# a worse failure than the drop it replaces.
#
# `list` IS ABSENT ON PURPOSE — it already calls cli.guard_tail, and has since
# before this table existed. That one verb having the contract while six did
# not is the whole finding rather than an oversight to tidy: the guard was in
# this file, three hundred lines from the verb that needed it most. Two guards
# answering one question is how they drift, so list keeps its own, which also
# gives it -h and the wants-a-value check this table does not.
#
# AND THIS IS NOT guard_tail, DELIBERATELY. guard_tail tests membership on the
# whole token, so `--superseded=<reason>` is JUNK to it — and that spelling is
# the documented escape `release` offers for a reason beginning with a dash.
# A shared guard that refuses a documented form is not the same guard.
_SHARED_FLAGS = ("--repo", "--seat")
_VERB_FLAGS = {
    "claim": ("--ttl", "--lease"),
    "release": ("--lease", "--superseded", "--stale", "--park"),
    "peek": ("--drop", "--json"),
    "stash": (),
}
# WHICH OF THOSE TAKE A VALUE. The distinction is not documentation, and
# this file needs it in two places at once: a dash token in a VALUE POSITION
# belongs to the flag before it, and calling it an unknown flag speaks over
# the guard that already owns malformed values and says the better sentence.
# `release --superseded -dash-leading` is the measured case — the verb's own
# refusal names the `--superseded=REASON` escape and a generic unknown-flag
# refusal does not.
# THIS FILE KEEPS NO VALUED TABLE OF ITS OWN. It had one, it was a COPY of
# `_common._VALUE_FLAGS` one entry short — `--drop` is read with a value by
# the peek branch below and was missing — so `_positional` skipped that
# value while this scan called a dash-leading one an unknown flag. Two
# tables answering one question drift in the direction nobody is looking,
# because each is locally correct. The scan no longer needs the answer:
# WHICH FLAGS TAKE A VALUE stopped deciding anything here once the
# value-position skip moved to `_EQUALS_FLAGS` below, and a table kept for
# the arms to compare is a table with no reader. The package answer
# lives in `_common._VALUE_FLAGS`, which `_positional` reads and an
# arm holds this file to — from there, rather than through a local
# alias that would be the same drift wearing an import.
# WHICH OF THOSE HAVE A READER THAT PARSES `--flag=VALUE`, AND IT IS ONE. The
# other four are read by `seats._flag`, which is `args.index(name)` — an
# EXACT token match, so `--ttl=30` is a token it never finds and the flag
# reads as ABSENT. A guard that accepts that spelling hands the caller a
# lease with a default TTL they believe they set, and `release --lease=TOK`
# is a release presenting no lease at all.
#
# SO THE `=` FORM IS PART OF THE GRAMMAR OF ONE FLAG, not of the table. The
# distinction has to live beside the table because the scan below reads both
# and would otherwise normalise a spelling into a flag whose reader cannot
# see it. Whether `seats._flag` should learn the `=` form is a wider
# question than this file — it has callers well outside helm/work — and this
# names the flags whose readers ALREADY do, so an answer either way lands by
# moving names into this tuple rather than by editing the scan.
_EQUALS_FLAGS = ("--superseded",)
# EVERY VERB THAT ALREADY CALLS cli.guard_tail, AND THE TABLE ABOVE COVERS
# NONE OF THEM. A second guard in front of guard_tail is the drift this file
# refuses everywhere else, and it REGRESSED --help on two of them: main
# answered rc 0 with the verb's own usage, and a table ahead of it called
# --help an unknown flag and refused (task/2255).
#
# `list` WAS EXCLUDED BY NAME AND THAT IS THE MISTAKE ITSELF. The property is
# "calls guard_tail", not "is called list" — I checked the one verb I had in
# mind and found one of the three. This list is derived from the call sites,
# and an arm holds it to them so a fourth cannot appear unnoticed.
_GUARD_TAIL_VERBS = ("gc", "list", "install-guard")

# -h AND --help ARE EVERY VERB'S, so they are never "unknown" and they are
# ANSWERED here rather than passed through. Passing them through is not
# harmless: `_positional` filters only tokens that start with TWO dashes and
# LANE_RE accepts a dash, so `helm work claim -h` reaches the lane name and
# MINTS A ROOM CALLED `-h` with a live lease. An arm pins that it does not.
_HELP_FLAGS = ("-h", "--help")


def _known_flags(verb):
    return sorted(set(_VERB_FLAGS.get(verb, ())) | set(_SHARED_FLAGS))


def _unknown_flags(verb, rest):
    """Dash tokens this verb does not read, or () for a verb it does not own.

    AN UNKNOWN FLAG WAS ACCEPTED AS DATA AND THE ARTIFACT IS A TREE. `helm
    work claim <lane> --base <sha>` returned 0 and printed an ordinary lease
    line while the room was cut from trunk: the caller believed the lane was
    stacked on a reviewed tip, the work compiled against code that was not
    there, and the first gate was the discovery. The same CLI already rules
    the other way three verbs away — `helm task list` refuses an unrecognised
    filter because "an unrecognised filter would print the WHOLE ledger while
    you believed it was narrowed" — and that argument is strictly stronger
    here, since a wrong view is recovered by looking again and a wrong base is
    recovered only by rebasing commits that should never have been written.

    THE `=` FORM IS ONE FLAG'S GRAMMAR, not an alias to be normalised away
    for the whole table. `release --superseded=<reason>` is the documented
    escape for a value that begins with a dash and its reader parses that
    token, so the name is taken from the left of the first `=`. Every other
    valued flag is read by an exact token match, so the same spelling arrives
    as a token nothing looks for: normalising it here would report the flag
    as present to this scan and absent to its reader, and the value would be
    dropped while the call returned 0. `_EQUALS_FLAGS` is the difference, and
    it names readers rather than preferences.

    A BARE `--` IS IGNORED AND WHAT FOLLOWS IT IS NOT. No verb here takes a
    child argv, so `--` names no flag and is not worth refusing, while a token
    after it belongs to nobody and would be dropped exactly like one before
    it. Stopping the scan there would put the silence back behind two dashes.

    AN UNKNOWN VERB YIELDS NOTHING, so the dispatcher keeps printing USAGE for
    it rather than complaining about flags on a verb that does not exist.
    """
    if verb not in _VERB_FLAGS:
        return []
    known = set(_known_flags(verb)) | set(_HELP_FLAGS)
    out = []
    # INDEXED, BECAUSE `prev` MUST ADVANCE PAST TOKENS THIS LOOP SKIPS. A
    # `prev = token` at the bottom is dead under every `continue` above it,
    # which would leave a value position unrecognised exactly when the token
    # before it was itself skipped.
    tokens = list(rest or ())
    for i, token in enumerate(tokens):
        prev = tokens[i - 1] if i else None
        # SINGLE-DASH TOKENS ARE SCANNED TOO, and that is not tidiness. A lane
        # name may contain a dash, so LANE_RE accepts `-h`, and `_positional`
        # only filters tokens beginning with TWO — so a one-dash token walks
        # past both and becomes the lane. Every flag these verbs read is
        # double-dash, so nothing legitimate is caught by widening this.
        if not token.startswith("-") or token in ("-", "--"):
            continue
        # A KNOWN NAME IS NOT ENOUGH — THE SPELLING HAS TO REACH A READER.
        # `--superseded=REASON` is the documented escape for a value that
        # begins with a dash, so its name is taken from the left of the first
        # `=`; every other valued flag is read by an exact token match, so
        # that same spelling arrives as a token nothing looks for and the
        # value is dropped while the call returns 0. Accepting it here is the
        # silence this function exists to end, wearing a known flag's name.
        name, eq, _value = token.partition("=")
        if name in known and (not eq or name in _EQUALS_FLAGS):
            continue
        # A DASH TOKEN IN A VALUE POSITION IS A MALFORMED VALUE, NOT AN
        # UNKNOWN FLAG, AND IT IS SOMEBODY ELSE'S REFUSAL. `--superseded
        # -dash-leading` is the verb's own documented failure: its guard
        # refuses it and names the `--superseded=REASON` escape, which this
        # refusal cannot do because it does not know which flag was starved.
        # Answering first replaced a specific sentence with a generic one and
        # reddened the trunk arm that pins it.
        #
        # IT IS POSITION, NOT SPELLING, AND ONLY AFTER A *VALUED* FLAG. The
        # token after a valued flag is ITS value whatever it looks like; a
        # token after a BOOLEAN flag or a POSITIONAL is in nobody's value
        # position and is still scanned. Measured on this table: `claim
        # <lane> -x`, `claim -x`, `release <lane> --park -x` and `release
        # <lane> --stale -x` all still report, and only the run after
        # --repo/--seat/--ttl/--lease/--superseded is skipped. So the silence
        # this function exists to end — a one-dash token walking past
        # `_positional` and LANE_RE to become a lane name with a live lease —
        # stays ended for every shape except the one another guard answers
        # better.
        # THE SKIP IS EARNED BY A BETTER REFUSAL, NOT BY TAKING A VALUE.
        # `seats._flag` is `args.index(name)` plus a check that the next token
        # does not begin with a dash — deliberately, so `--seat --apply`
        # cannot mint a seat called `--apply` — so for EVERY flag it reads, a
        # dash-leading value is read as ABSENT. Skipping the token after all
        # of them therefore protected calls that cannot work: `claim <lane>
        # --ttl -1` was accepted here and the TTL silently defaulted, and
        # `peek <sha> --drop -x` was accepted here, lost its value at the
        # reader, and then PEEKED the subject instead of dropping it.
        #
        # SO THE SET IS THE FLAGS WHOSE VERB CAN SAY MORE THAN THIS SCAN CAN.
        # `release --superseded -dash` is answered by the verb's own guard,
        # which names the `--superseded=REASON` escape; this refusal cannot,
        # because it does not know which flag was starved. That better
        # sentence exists exactly where an inline escape exists — the escape
        # is what it points the caller at — so `_EQUALS_FLAGS` is the same
        # set for a reason rather than by coincidence, and a flag added there
        # brings its refusal with it. A flag with no escape has nothing better
        # to be said about it than this scan already says.
        #
        # AND A FLAG THAT CARRIED ITS VALUE INLINE OPENS NO VALUE POSITION
        # EITHER: the token after `--superseded=reason` belongs to nobody, so
        # the membership test is EXACT and `--superseded=reason --base <sha>`
        # reports the `--base` it would otherwise swallow.
        if prev is not None and prev in _EQUALS_FLAGS:
            continue
        out.append(token)
    return out


def cmd_work(args):
    """work claim|release|gc|list|install-guard — worktree lifecycle on the
    claims lane: private room per lane, the shared checkout stays the
    integrator's, abandoned dirty work is rescued, never discarded."""
    args = list(args or [])
    if not args:
        print(USAGE, file=sys.stderr)
        return 2
    verb, rest = args[0], args[1:]
    # -h/--help IS ANSWERED BEFORE ANYTHING ELSE for the verbs this file
    # guards. The guard_tail verbs answer it themselves and must not be
    # reached from here.
    if verb in _VERB_FLAGS and any(t in _HELP_FLAGS for t in rest):
        print(USAGE)
        return 0
    # A CLAIM'S TTL IS JUDGED BEFORE THE UNKNOWN-FLAG SCAN, by its own
    # reader. That reader knows which flag was starved and what it takes, so
    # `--ttl -1` gets that sentence instead of the scan's "unknown flag -1";
    # a malformed length is refused before an identity or a room is touched.
    if verb == "claim":
        ttl, ttl_err = ttl_flag(rest, DEFAULT_TTL)
        if ttl_err:
            print("helm work claim: " + ttl_err, file=sys.stderr)
            return 2
    bad = _unknown_flags(verb, rest)
    if bad:
        # TWO REFUSALS, BECAUSE THE CALLER'S MISTAKE IS TWO DIFFERENT
        # MISTAKES. A name this verb does not read is a typo or a flag from
        # another verb; a name it DOES read, spelled with an `=` its reader
        # cannot parse, is a value about to be dropped by a token that looks
        # entirely correct. Telling the second caller their flag is "unknown"
        # while the refusal lists that very flag among the readable ones
        # sends them to look for a typo that is not there.
        name = bad[0].partition("=")[0]
        if name in set(_known_flags(verb)) | set(_HELP_FLAGS):
            print("helm work %s: %s — REFUSED rather than dropped: %s is read "
                  "by an EXACT token match, so this spelling is a token "
                  "nothing looks for and the value would be dropped while the "
                  "call returned 0. Write it as `%s VALUE`. The `=` spelling "
                  "is read by %s alone, which is why it is the escape offered "
                  "for a value that begins with a dash.\n%s"
                  % (verb, bad[0], name, name,
                     ", ".join(_EQUALS_FLAGS), USAGE), file=sys.stderr)
            return 2
        # THE `=` ESCAPE IS NAMED ONLY BY A VERB THAT OFFERS ONE. Advertising
        # it to a caller whose verb reads no such flag hands them a spelling
        # that this same guard refuses one line up, which is worse than
        # saying nothing: it reads as permission.
        escape = [f for f in _known_flags(verb) if f in _EQUALS_FLAGS]
        print("helm work %s: unknown flag %s — REFUSED rather than dropped: "
              "this verb reads only %s, and every other dash token was "
              "accepted-looking and inert, so a caller's belief and the "
              "WORKTREE could diverge in silence.%s\n%s"
              % (verb, bad[0], ", ".join(_known_flags(verb)),
                 (" A value that legitimately begins with a dash needs the "
                  "`%s=VALUE` spelling." % escape[0]) if escape else "",
                 USAGE),
              file=sys.stderr)
        return 2
    repo = seats._flag(rest, "--repo")
    root = find_root(repo) if repo else find_root()
    if not root:
        print("helm work: not inside a git repo (--repo PATH names one)",
              file=sys.stderr)
        return 2
    session = seats._env_session()

    def acting(act):
        """(name, rc) — the actor for ONE lease verb, resolved AT THE VERB.

        DELIBERATELY NOT AT THE TOP OF THIS FUNCTION. A first pass put the
        admission here-but-earlier, before the verb dispatch, and that REFUSED
        `helm work list|peek|gc|stash|install-guard` for any operator with no
        HELM_CHAT_NAME — read-only verbs that take no identity, denied for
        lacking one. An identity guard that outgrows the acts it guards is a
        shipped regression, so admission is called by the two verbs that
        mutate a lease and by nothing else.

        --seat is an ASSERTION against the resolved identity, never a selector:
        this verb never reached chat._seat_actor's law, so a bare flag could
        name any seat and the DERIVED floor could mint one."""
        from helm import actors
        actor, err = actors.resolve_actor(session, root,
                                          asserted=seats._flag(rest, "--seat"),
                                          act=act)
        if err:
            print("helm work: " + err, file=sys.stderr)
            return None, 2
        return actor.canonical_name, 0

    if verb == "claim":
        pos = _positional(rest)
        if not pos or not LANE_RE.match(pos[0]):
            print("usage: helm work claim <lane>  (lane = [A-Za-z0-9._-]{1,64};"
                  " keep the printed lease id — it is the room key)",
                  file=sys.stderr)
            return 2
        # `<root>-wt/seats/` is the per-seat HOME container; a lane by that name
        # would try to check a room out ON TOP of it and fail obscurely. The
        # peek container is reserved for the same reason.
        from ..harness import SEAT_HOME_DIRNAME
        from ._common import PEEK_DIRNAME
        if pos[0] == SEAT_HOME_DIRNAME:
            print("helm work: '%s' is reserved — it is the per-seat home "
                  "worktree container (<repo>-wt/%s/<seat>); pick another lane "
                  "name" % (SEAT_HOME_DIRNAME, SEAT_HOME_DIRNAME),
                  file=sys.stderr)
            return 2
        if pos[0] == PEEK_DIRNAME:
            print("helm work: '%s' is reserved — it is the read-only peek "
                  "container (<repo>-wt/%s/<sha12>); pick another lane name"
                  % (PEEK_DIRNAME, PEEK_DIRNAME), file=sys.stderr)
            return 2
        seat, arc = acting("claim a lane lease")
        if arc:
            return arc
        # THE PROJECT'S LIGHT, BEFORE ANYTHING IS CHECKED OUT. A new lane IS
        # new work starting here, so this is the door the owner's colour has to
        # hold at. A RENEWAL is never refused: a lease is a lock on a room, not
        # work, and refusing one would strand a room mid-edit without stopping
        # a single keystroke — it gets the note and nothing else.
        from .. import registry
        lit_ok, lit_refusal, lit_note = registry.admits(
            root, new_work=not seats._flag(rest, "--lease"))
        if not lit_ok and not seats._flag(rest, "--lease"):
            print("helm work: REFUSED — " + lit_refusal, file=sys.stderr)
            return 1
        if lit_note or lit_refusal:
            print("helm work: NOTE — " + (lit_note or lit_refusal), file=sys.stderr)
        rc, line = claim(root, pos[0], seat,
                         ttl=ttl,
                         lease=seats._flag(rest, "--lease"), session=session)
        print(line, file=sys.stdout if rc == 0 else sys.stderr)
        if rc == 0:
            # WHO ELSE IS LIVE, AND IN WHAT. A lease answers "is anyone in this
            # ROOM", never "is anyone already fixing this DEFECT" — and the
            # board's answer to the second question (lane_overlaps) lives in
            # `gc`, so it reaches whoever runs housekeeping AFTER the duplicate
            # is written. 2026-08-04: three seats built one fix under three
            # different lane labels, all in helm/cli.py.
            #
            # It DISCLOSES rather than compares, because the lane just claimed
            # has no commits and nothing can compare an empty diff. The claimer
            # knows what they are about to touch; this is the half they cannot
            # get. Fail-open and last: a git hiccup must never cost a claim
            # that already succeeded.
            try:
                from ._gc import moved_lane_targets
                # DID TRUNK MOVE THE FILE OUT FROM UNDER A LIVE LANE. Sibling
                # of the disclosure below, one axis over: that one says who
                # else is in this file NOW, this one says the file you are in
                # is no longer where trunk keeps that code. Printed FIRST
                # because it is rarer and strictly more urgent — an unnoticed
                # moved target lands an edit on a facade and the real caller
                # keeps calling a name the same commit deleted.
                for lane, gone in moved_lane_targets(root)[:6]:
                    shown = ", ".join(gone[:4])
                    if len(gone) > 4:
                        shown += " (+%d more)" % (len(gone) - 4)
                    # THE WRAPPER MUST NOT ASSERT THE EVENT — the entry
                    # already names it, and this sentence used to contradict
                    # it. It read "trunk NO LONGER HAS it" directly after
                    # printing "(trunk -3685, yours 1)", which shows trunk DOES
                    # have it and merely shrank it: a reader sent hunting for a
                    # deleted file that is still there. The producer learned to
                    # tell deleted from drained and the surface collapsed them
                    # back into one false claim, which is the seam class in a
                    # single sentence. Now the wrapper states only what is true
                    # of BOTH events and the entry carries which one.
                    print("helm work: lane %s — trunk changed code this lane "
                          "edits: %s; rebase before trusting a gate on it"
                          % (lane, shown), file=sys.stderr)
            except Exception:
                pass
            # A SECOND try, NOT a shared one. A reviewer measured the shared
            # version: with the moved-target call raising, the live-lane
            # disclosure below DISAPPEARED ENTIRELY and the claim still
            # returned 0 — a new guard silently deleting an older one, which
            # is worse than the gap it was added to close. Each disclosure
            # fails open ALONE.
            try:
                from ._gc import held_lane_files
                # NEVER OMIT AN IDENTITY, ONLY ABBREVIATE EVIDENCE.
                #
                # Per-class caps were the wrong shape and a probe reproduced
                # why: 5 UNKNOWN / 8 bare / 5 file-bearing kept 3/4/4 rows and
                # a generic count, so the fourth UNKNOWN lane and the fifth
                # file-bearing lane's files were simply gone. Worse, the
                # overflow line told the reader to run `helm work list` to
                # recover them — and that surface renders NEITHER the UNKNOWN
                # class NOR filenames. A pointer to a surface that cannot
                # answer is not a disclosure, it is a second silence with a
                # sentence in front of it.
                #
                # So the cap moves off the LANE and onto the FILE LIST. Every
                # live lane is named, always — the name is the whole signal a
                # claimer needs to recognise their own subject under someone
                # else's label, and it costs one line. Only the file evidence
                # is abbreviated, and that abbreviation is counted in place,
                # where the reader can see which lane it belongs to.
                FILES_SHOWN = 4
                for lane, files in held_lane_files(root, exclude=pos[0]):
                    if files is None:
                        # UNKNOWN IS ITS OWN SENTENCE. Rendering it as
                        # "nothing authored yet" would state as fact the one
                        # thing the read failed to establish.
                        print("helm work: live lane %s — authorship UNKNOWN, "
                              "its history could not be read; treat it as "
                              "possibly overlapping yours" % lane,
                              file=sys.stderr)
                    elif not files:
                        print("helm work: live lane %s CLAIMED, nothing "
                              "authored yet — read the NAME: if it is your "
                              "subject under another label, ask before you "
                              "build" % lane, file=sys.stderr)
                    else:
                        shown = ", ".join(files[:FILES_SHOWN])
                        if len(files) > FILES_SHOWN:
                            shown += " (+%d more file(s))" % (
                                len(files) - FILES_SHOWN)
                        print("helm work: live lane %s is in %s"
                              % (lane, shown), file=sys.stderr)
            except Exception:
                pass
            # AND WHETHER THE RAIL AROUND THIS NEW ROOM IS ACTUALLY ARMED.
            # stale_guard_hooks has exactly ONE production caller — `gc`,
            # which prints it above a 45-room listing. So a landed-but-inert
            # guard is discoverable only by someone running a housekeeping
            # verb and reading past the rooms, and MEASURED 2026-08-04 it sat
            # unnoticed long enough that the shared checkout was missing the
            # worktree-birth traversal refusal and the pre-commit in-flight
            # gate outright, across all 45 rooms (worktrees share one hook
            # dir) while nine seats committed through them.
            #
            # CLAIM IS THE MOMENT, not a convenient one: the drifted rules
            # guard worktree BIRTH and COMMITS, and claim is where a worktree
            # is born and a room's commits begin. It cannot live in the
            # pre-commit hook itself, because the stale thing IS that hook —
            # nothing can be trusted to report its own absence.
            #
            # EVERY non-fresh state, unlike the landed-close rung which
            # refuses on STALE alone. The asymmetry is deliberate and is about
            # what being wrong COSTS: that rung blocks a land, so it must be
            # narrow enough that a repo which never opted into helm's rail is
            # never held up by a stranger's opinion of its hooks. This one
            # prints a line to stderr and blocks nothing, so it can afford to
            # say "not armed" for missing and unreadable too — which at CLAIM
            # time is the more useful reading anyway, because the room being
            # born right now gets no birth guard in any of those states.
            try:
                drift = stale_guard_hooks(root)
                if drift:
                    print("helm work: GUARD RAIL NOT ARMED for this room — "
                          "%s. These hooks are SHARED by every worktree, so "
                          "this is the whole checkout, not just your lane. "
                          "Arm them: `%s`%s"
                          % ("; ".join("%s is %s" % (n, s.lower())
                                       for s, n, _w in drift),
                             guard_remedy(root), guard_remedy_note(root)),
                          file=sys.stderr)
            except Exception:
                pass
        return rc
    if verb == "release":
        pos = _positional(rest)
        lane = pos[0] if pos else _infer_lane(root)
        if not lane:
            print("usage: helm work release <lane> --lease ID [--park] "
                  "[--superseded REASON] | --stale (lane infers only from "
                  "inside its room; `helm work list` reprints your own lease "
                  "id; --stale is the dead-holder escape — liveness proof, no "
                  "lease required; --superseded retires the ROOM for work that "
                  "will never land and KEEPS the branch)",
                  file=sys.stderr)
            return 2
        # THE FLAG WAS TYPED IS A DIFFERENT FACT FROM THE FLAG HAS A VALUE,
        # and every guard below needs the first one. `seats._flag` answers
        # None both when the flag is absent and when it is present with no
        # usable value (last token, or followed by another flag) — so keying
        # the guards on the parsed value made a valueless `--superseded`
        # vanish: the door fell back to an ORDINARY release, retired nothing,
        # and said nothing about the evidence it had just dropped. That is the
        # one outcome a door whose whole authority IS the evidence cannot
        # have. Worse, it took the `--stale` refusal with it, since a guard
        # reading `gone is not None` cannot fire on the shape it exists for.
        typed = ("--superseded" in rest
                 or any(t.startswith("--superseded=") for t in rest))
        if typed and "--stale" in rest:
            # TWO DIFFERENT AUTHORITIES, AND NEITHER SUBSUMES THE OTHER.
            # --stale is a liveness proof about a DEAD HOLDER and deliberately
            # touches neither room nor branch; --superseded is a live caller's
            # evidence about the WORK. Accepting both would silently drop the
            # one the caller typed second, so it refuses and says which.
            print("helm work: --stale and --superseded are different requests "
                  "— --stale releases a dead holder's claim and leaves the "
                  "room, --superseded retires the room on stated evidence. "
                  "Run the stale release first, then re-run with "
                  "--superseded.", file=sys.stderr)
            return 2
        gone = seats._flag(rest, "--superseded")
        if gone is None:
            # THE `=` FORM IS AUTHORITATIVE AND ITS VALUE IS ALWAYS LITERAL —
            # `helm task`'s rule (tasks.py `_take`), adopted here for the same
            # reason: a reason that legitimately begins with a dash is
            # unsayable in the space form, and a guard whose forbidden thing
            # is unsayable gets deleted by the first person who needs it.
            for tok in rest:
                if tok.startswith("--superseded="):
                    gone = tok[len("--superseded="):]
                    break
        if typed and gone is None:
            print("helm work: --superseded needs a value that is not another "
                  "flag — nothing was released. If the reason really starts "
                  "with a dash, write it as --superseded=REASON.",
                  file=sys.stderr)
            return 2
        if "--stale" in rest:
            # STALE RELEASE TAKES NO IDENTITY, on purpose. The authority is a
            # LIVENESS PROOF about the dead holder, never the caller's own
            # name, so an env-less operator recovering a wedged fleet must not
            # be refused for lacking one. `--seat` still only labels the
            # request; helm.actors.StaleReleaseProof carries the real
            # authority and mints only from a verdict of exactly "stale".
            rc, lines = release_stale_lane(
                root, lane, seats._flag(rest, "--seat") or "", session=session)
        else:
            # THE LEASE TOKEN IS THE AUTHORITY (see helm/seats_cli.py's
            # release leg): `--seat` addresses the holder row, and requiring an
            # admitted actor on top would refuse a holder following the exact
            # command its own guard printed.
            seat = seats._flag(rest, "--seat") \
                or seats.acting_seat(session, root)
            rc, lines = release_lane(root, lane, seat,
                                     lease=seats._flag(rest, "--lease"),
                                     session=session, park="--park" in rest,
                                     superseded=gone)
        for ln in lines:
            print(ln, file=sys.stdout if rc == 0 else sys.stderr)
        return rc
    if verb == "peek":
        # A positional-taking verb, so no guard_tail (the claim/release
        # precedent). --drop and <committish> are two different requests and
        # carrying both is ambiguous, so it refuses rather than picking.
        from ._peek import peek, peek_drop
        drop = seats._flag(rest, "--drop")
        pos = _positional(rest)
        if drop is not None and pos:
            print("usage: helm work peek <committish> [--json] | "
                  "helm work peek --drop <path-or-committish>  (not both)",
                  file=sys.stderr)
            return 2
        if drop is not None:
            rc, lines = peek_drop(root, drop)
            for ln in lines:
                print(ln, file=sys.stdout if rc == 0 else sys.stderr)
            return rc
        if len(pos) != 1:
            print("usage: helm work peek <committish> [--json]  (a read-only "
                  "detached room at exactly that commit; --drop retires it)",
                  file=sys.stderr)
            return 2
        rc, payload = peek(root, pos[0])
        if rc != 0:
            print(json.dumps(payload) if "--json" in rest else payload["error"],
                  file=sys.stderr)
            return rc
        # THE DISCLOSURE GOES TO STDERR, AND THAT IS NOT TIMIDITY. The
        # documented stdout contract of this verb is `path<TAB>sha`; every
        # caller that parses it would break on a second line. stderr reaches
        # the human without touching the machine contract, which is exactly
        # the split this verb already uses for its refusals.
        if payload.get("reused") and "--json" not in rest:
            for ln in _reuse_notice(payload):
                print(ln, file=sys.stderr)
        print(json.dumps(payload) if "--json" in rest
              else "%s\t%s" % (payload["path"], payload["sha"]))
        return 0
    if verb == "gc":
        # `work gc --bogus --apply` must refuse before scan/enact, not run
        # the rescue-commit sweep under an arg that does not exist.
        from ..cli import guard_tail
        rc = guard_tail("helm work gc", rest, flags=("--apply",),
                        valued=("--repo", "--seat"),
                        usage="work gc [--apply] [--repo PATH]")
        if rc is not None:
            return rc
        # FETCH BEFORE THE SCAN, NOT AFTER IT. gc_scan decides landedness
        # against a remote-tracking ref, and --apply deletes branches on that
        # decision — so the refresh has to happen before the proof is computed,
        # not before the deletion. A failed fetch REFUSES the whole apply: an
        # unrefreshed proof must not authorize a deletion.
        enforcing = "--apply" in rest
        if enforcing:
            ok, why = refresh_trunk(root)
            print("helm work gc: %s" % why)
            if not ok:
                print("helm work gc: REFUSING to apply — the trunk ref could "
                      "not be refreshed, so 'landed' is UNPROVEN and this verb "
                      "deletes branches on that proof. Re-run when the remote "
                      "is reachable, or use the dry run.", file=sys.stderr)
                return 1
        # THE SHARED CHECKOUT ITSELF RIDES THE CADENCE (2026-08-03: the
        # fleet measured trunk on a checkout that had silently FORKED from
        # origin/main for ~80 minutes). refresh_trunk just made the
        # remote-tracking ref honest; this converges the local base onto it
        # — ff only, every unconvergeable state named, a fork posted loud.
        sync_lines, sync_post_error = trunk_sync(root, enforcing)
        for line in sync_lines:
            print("helm work gc: %s" % line)
        if sync_post_error:
            print("helm work gc: %s" % sync_post_error, file=sys.stderr)
        # A LANDED GUARD THAT IS NOT INSTALLED IS INERT, and until now
        # nothing looked. Measured 2026-08-04: this checkout ran hook v3
        # while trunk generated v4, so v4's fixes had never been armed here.
        # Report only — installing rewrites an executable in someone's .git
        # and must never be a side effect of a read pass.
        # THE SAME REMEDY AS CLAIM'S, and it carries the profile for the same
        # reason: a flagless refresh is what the reader pastes.
        _remedy = guard_remedy(root)
        _remedy_note = guard_remedy_note(root)
        for _state, _name, _why in stale_guard_hooks(root):
            print("helm work gc: GUARD-%s %s — %s; run `%s`%s"
                  % (_state, _name, _why, _remedy, _remedy_note))
        rows = gc_scan(root)

        # REGISTRY RESIDUE RIDES THE SAME PASS. A record whose directory is
        # GONE has no room, so the room scan cannot see it — but git keeps the
        # record indefinitely, `worktree add` refuses its path, and a
        # metaharness that mirrors the registry (Orca's sidebar) renders it as
        # a ghost room forever. The dry run names each one; --apply prunes
        # RECORDS ONLY — never files, never branches, locked records immune.
        def phantom_pass():
            phantoms, excluded, scan_error = phantom_scan(root)
            if scan_error:
                print("helm work gc: worktree registry unavailable: %s" %
                      scan_error, file=sys.stderr)
                return 1, None, None
            if not phantoms:
                return 0, 0, 0
            if not enforcing:
                for p in phantoms:
                    print("  PHANTOM %s — registered but the directory is "
                          "gone (registry residue renders as a ghost room in "
                          "a metaharness sidebar); --apply prunes the record"
                          % _show(p))
                return 0, 0, len(phantoms)
            pruned, error, unknown = prune_phantom_records(
                root, phantoms, excluded=excluded)
            for p in pruned:
                print("  phantom record no longer registered " + _show(p))
            if error:
                suffix = "" if unknown else " — phantom records kept"
                print("helm work gc: %s%s" % (error, suffix), file=sys.stderr)
                return ((1, None, None) if unknown else
                        (1, len(pruned), len(phantoms) - len(pruned)))
            return 0, len(pruned), 0

        # STALE PEEKS RIDE THE SAME PASS (#157, the owner's fifth ask for
        # the same recurrence): peeks are minted per review/gate and nothing
        # retired them, so a metaharness sidebar accreted a ghost project per
        # peek forever. TTL detection lives in _peek.stale_peeks; REMOVAL
        # stays peek_drop's alone — every live-room refusal (cwd occupant,
        # bound pane, dirty tree) fires there and a refusal here is a KEPT
        # room, printed, never an error.
        def peek_pass():
            from ._peek import PEEK_TTL_S, peek_drop, stale_peeks
            dropped = kept = 0
            for path, idle in stale_peeks(root):
                name = os.path.basename(path)
                if not enforcing:
                    print("  PEEK-STALE %s — idle %dh%02dm past its TTL; "
                          "--apply drops it (occupied/pane-bound rooms "
                          "refuse and are kept)"
                          % (name, idle // 3600, idle % 3600 // 60))
                    kept += 1
                    continue
                rc_d, lines = peek_drop(root, path, stale_ttl_s=PEEK_TTL_S)
                for ln in lines:
                    print("        " + ln)
                if rc_d == 0:
                    dropped += 1
                else:
                    kept += 1
            return dropped, kept

        if not rows:
            print("helm work gc: no lane rooms under %s-wt/" % root)
            # PEEKS ARE THEIR OWN POPULATION in the summary, both branches:
            # this branch DISCARDED peek_pass's counts, so a pass that
            # dropped a peek reported removed=0 (the REWORK on
            # dispatch 766a5bf761f0, first repro).
            peek_dropped, peek_kept = peek_pass()
            phantom_rc, phantom_removed, phantom_kept = phantom_pass()
            if enforcing and phantom_removed is not None:
                line = format_gc_summary(
                    root, peek_dropped + phantom_removed,
                    peek_kept + phantom_kept, phantom_kept)
                print(line)
                error = post_gc_summary(line)
                if error:
                    print(error, file=sys.stderr)
                    return 1
            return phantom_rc
        print("helm work gc — %d room%s under %s-wt/ (%s)" % (
            len(rows), "s"[:len(rows) != 1], root,
            "APPLYING" if enforcing else "dry-run; --apply enforces — only "
            "clean LANDED rooms retire; dirty/unlanded work stays for triage, "
            "never discarded"))
        w = max(len(r["lane"]) for r in rows)
        removed = 0
        # PLANNED-VS-DONE, because "removed=0" is literally true and reads as
        # "nothing needed removing". Measured 2026-08-04: a run PLANNED two
        # removals, completed ZERO (both blocked on a bound pane), and said
        # `removed=0 kept=40` — a summary that cannot distinguish a clean estate
        # from a jammed one, and real time was burned believing the first.
        planned, blocked_why, reclassified = 0, [], []
        for r in rows:
            print("  %-7s %-*s  %s" % (r["verdict"].upper(), w, r["lane"], r["why"]))
            if enforcing:
                planned += r["verdict"] == "remove"
                out = gc_enact(root, r)
                for ln in out:
                    print("        " + ln)
                gone = not os.path.exists(r["path"])
                removed += gone
                if r["verdict"] == "remove" and not gone:
                    blocked_why.append(_blocked_reason(out))
                    # BOTH TALLIES, NOT A MOVE BETWEEN THEM. The room stays
                    # blocked (a planned removal did not happen) AND becomes
                    # triage (gc_enact re-measured landedness and refused).
                    # Counting it only as blocked is what let the summary
                    # print a triage number excluding a room the detail
                    # directly above printed a full TRIAGE line for.
                    if was_reclassified(out):
                        reclassified.append(r)
        # `removed` stays a LANE quantity: folding peek drops into it made
        # `len(rows) - removed` subtract peeks from the LANE kept count —
        # two dropped peeks beside one kept lane printed kept=-1 (the
        # REWORK, dispatch 766a5bf761f0, second repro). Peeks join the summary as
        # their own population instead.
        peek_dropped, peek_kept = peek_pass()
        phantom_rc, phantom_removed, phantom_kept = phantom_pass()
        if enforcing and phantom_removed is not None:
            # ONE DERIVATION, AT ENACT-TIME TRUTH, FEEDING BOTH NUMBERS.
            # It was two derivations of the same population, which is how they
            # drift; and both read the SCAN verdicts, which is how they came
            # to disagree with the detail printed directly above. A room
            # gc_enact reclassified is triage NOW even though the scan said
            # remove. (A cross-family read, finding 2 + cleanup.)
            eligible = [r for r in rows
                        if r["verdict"] in ("triage", "rescue")] + reclassified
            triage = len(eligible) + phantom_kept
            # COUNTED OVER THE SAME POPULATION THE TRIAGE NUMBER IS TAKEN FROM
            # — the triage/rescue rows — and never over every room, or the
            # clause would answer a question the number beside it is not
            # asking. phantom_kept is deliberately excluded on both sides of
            # this predicate: a phantom has no lane branch to look up, so
            # counting it as unrowed would report a measured absence for a
            # room that was never eligible to carry a row.
            # AN EMPTY POPULATION IS KNOWABLY ZERO, SO IT NEVER ASKS THE
            # LEDGER. With no triage/rescue rows there is nothing that COULD
            # be unrowed, and that stays true whether the ledger is pristine,
            # missing or shredded — so an unreadable ledger printed
            # `triage=0 (unrowed=UNKNOWN)`, which claims helm cannot tell when
            # helm can. UNKNOWN belongs only where the answer genuinely
            # DEPENDS on the unreadable source; reporting it anywhere else
            # spends the operator's doubt on a question already settled.
            # Omitting beats printing 0 because there is no population to
            # report on, which is the same reason the no-lane-rows branch
            # passes no clause at all. (a cross-family read, finding 3)
            if not eligible:
                unrowed = None
            else:
                rowed, rowed_err = _rowed_lanes()
                unrowed = ("UNKNOWN" if rowed_err else
                           sum(r["lane"] not in rowed for r in eligible))
            line = format_gc_summary(
                root, removed + peek_dropped + phantom_removed,
                len(rows) - removed + peek_kept + phantom_kept, triage,
                planned=planned, blocked=blocked_why, unrowed=unrowed)
            print(line)
            error = post_gc_summary(line)
            if error:
                print(error, file=sys.stderr)
                return 1
        return phantom_rc
    if verb == "list":
        from ..cli import guard_tail
        rc = guard_tail("helm work list", rest, valued=("--repo", "--seat"),
                        usage="work list [--repo PATH]")
        if rc is not None:
            return rc
        from ._peek import peek_rows
        registered, registry_error = _worktree_records(root)
        rows = list_rows(root, registered=registered)
        peeks = peek_rows(root, registered=registered)
        loose, discovery_errors = unguarded_inventory(
            root, registered=registered, registry_error=registry_error)
        if not rows and not peeks and not loose and not discovery_errors:
            print("helm work: no lane rooms — `helm work claim <lane>` opens "
                  "one at %s-wt/<lane>" % root)
            return 0
        if rows:
            # Separate stale rows so the fleet sees dead holders without scanning
            stale = [r for r in rows if r.get("stale")]
            live_rows = [r for r in rows if not r.get("stale")]
            if live_rows:
                print("GUARDED lane rooms — claims and leases apply:")
                w = max(len(r["lane"]) for r in rows)
                # THE DURATION SAYS WHICH DIRECTION IT RUNS. It rendered as
                # a bare "<seat> 13378s", which reads equally well as "held
                # for" and as "left", and the field behind it is literally
                # named `remaining`. Two seats misread it in the same hour on
                # 2026-08-05 — one nearly raised a false alarm on their own
                # lane — which makes it a surface defect rather than
                # carelessness. Line 554 of this same file already writes
                # "%ds ago" for the other direction, so the convention
                # existed and this column simply missed it.
                holds = [("%s %ds left" % (r["holder"], r["remaining"])
                          if r["holder"] else "-") for r in live_rows]
                # width DERIVED from the data, like the lane column above:
                # padding a longer seat name into a fixed 24 silently ragged
                # the rows that most needed reading.
                hw = max([len(h) for h in holds] + [len("holder")])
                for r, hold in zip(live_rows, holds):
                    # the release token, for THIS seat's own rows only — the board
                    # that lists the obligation now also hands back what closing
                    # it requires (a holder who lost the token to compaction used
                    # to have no route but reading .claims.json by hand)
                    mine = ("  lease=%s (yours)" % r["lease"]) if r.get("lease") else ""
                    print("  GUARDED %-*s  %-*s  %-9s  +%s/-%s%s  path=%s%s" % (
                        w, r["lane"], hw, hold, _tree_word(r),
                        r["ahead"], r["behind"], "  locked" if r["locked"] else "",
                        ascii(r["path"]), mine))
                    if r.get("dangling_conflict"):
                        print(_DANGLING_NOTE % (r["conflicts"], ascii(r["path"])))
                    # A LANDED LANE IS NOT WORK IN FLIGHT, and `+0/-N` alone
                    # leaves the reader to work that out. The verdict is the one
                    # the web board reads (`lanes_landed`); the release line
                    # is the lease's only way out, since a land retires none.
                    landed = r.get("landed") or {}
                    if landed.get("state") == LANE_LANDED:
                        print("      LANDED — %s; the lease is still held%s: %s"
                              % (landed.get("proof"),
                                 " and the room is DIRTY (commit or --park "
                                 "first)" if r.get("dirty") else "",
                                 release_command(r)))
            if stale:
                print("STALE claims — holder is provably dead; release with:")
                for r in stale:
                    print("  helm work release %s --stale  (was %s)"
                          % (r["lane"], r["holder"]))
            # THE DISCOVERY PROBLEM, third instance of the day's one cure
            # (#289): a lease expires, the room and its committed delta do
            # not — and nothing LISTED a holderless room with work in it, so
            # the risk is not loss (a branch survives forever) but a seat
            # SILENTLY REDOING it. Two seats claimed the same gc-triage fix
            # under different names on 2026-08-05; the lease dedupes a NAME,
            # never the WORK. Empty-section law: silent unless real.
            def _delta(r):
                try:
                    return int(r.get("ahead") or 0)
                except (TypeError, ValueError):
                    return 0
            holderless = [r for r in live_rows
                          if not r["holder"] and _delta(r) > 0]
            if holderless:
                print("HOLDERLESS WITH DELTA — nobody holds these and their "
                      "branches carry commits; check for your subject here "
                      "BEFORE claiming a new lane:")
                for r in holderless:
                    print("  %-*s  +%s/-%s  resume: helm work claim %s" % (
                        w, r["lane"], r["ahead"], r["behind"], r["lane"]))
            # THE QUESTION THE LEASE COLUMN CANNOT ANSWER. Every row above can
            # be a valid, uncontested lease and two of them can still be aimed
            # at one defect — which happened three times on 2026-07-30, caught
            # each time by a human reading chat and never by this board. Empty
            # section stays silent (the empty-section law); it only speaks when
            # two open lanes have actually touched the same file.
            try:
                overlaps = lane_overlaps(root, registered=registered)
            except Exception:      # noqa: BLE001 — the board never dies on it
                overlaps = []
            if overlaps:
                print("OVERLAPPING LANES — a lease guards a ROOM, not a DEFECT; "
                      "these touch the same files:")
                for a, b, shared in overlaps:
                    print("  %s x %s — %s%s" % (
                        a, b, ", ".join(shared[:4]),
                        " +%d more" % (len(shared) - 4) if len(shared) > 4 else ""))
                print("  file overlap is a PROXY for 'same defect', not proof — "
                      "check with the other holder before building")
        if peeks:
            # its OWN section, not extra GUARDED rows: a peek has no lane,
            # holder, or lease, and printing it in that table would invite
            # exactly the claim/release verbs that must never aim here
            print("PEEK rooms — read-only, disposable "
                  "(`helm work peek --drop <path>` retires):")
            for r in peeks:
                print("  PEEK %s  %s  path=%s%s" % (
                    r["name"],
                    "DETACHED" if not r["branch"]
                    else "!! on branch %s (no longer a clean peek)" % r["branch"],
                    ascii(r["path"]), "  locked" if r["locked"] else ""))
        for error in discovery_errors:
            print("WARNING: UNGUARDED discovery UNKNOWN — %s; retain and inspect "
                  "manually." % ascii(error))
        if loose:
            print("WARNING: %d UNGUARDED Agent/Workflow room%s — visibility and "
                  "advisory evidence only. No claim/lease protects these rooms, "
                  "and this output never authorizes cleanup:" %
                  (len(loose), "s"[:len(loose) != 1]))
            for r in loose:
                if r["occupants"]:
                    state = "OCCUPIED"
                    evidence = "cwd pid(s) " + ",".join(r["occupants"])
                elif r["harness"] == "live":
                    state, evidence = "LIVE-HARNESS", r["harness_note"]
                elif r["hard_unknown"]:
                    state, evidence = "UNKNOWN", r["unknown"]
                elif r["wrote_ago"] is not None \
                        and r["wrote_ago"] <= RECENT_WRITE_SECONDS:
                    state = "RECENT-WRITE"
                    evidence = "%ds ago%s; advisory, not ownership proof" % (
                        r["wrote_ago"], " (future mtime/clock skew)"
                        if r["clock_skew"] else "")
                elif r["dirty"]:
                    state = "DIRTY"
                    evidence = "uncommitted work; last timestamp %s" % (
                        "%dm ago" % (r["wrote_ago"] // 60)
                        if r["wrote_ago"] is not None else "unknown")
                elif r["unknown"]:
                    state, evidence = "UNKNOWN", r["unknown"]
                else:
                    state = "CLEAN"
                    evidence = ("no cwd or live harness evidence; this is not "
                                "proof that no writer will resume")
                print("  UNGUARDED %-16s id=%-30s tree=%-8s lock=%-8s "
                      "branch=%s path=%s evidence=%s" % (
                          state, r["id"], "DIRTY" if r["dirty"] else "CLEAN",
                          "LOCKED" if r["locked"] else "UNLOCKED",
                          ascii(r["branch"] or "(detached)"), ascii(r["path"]),
                          ascii(evidence)))
        return 0
    if verb == "install-guard":
        from ..cli import guard_tail
        grc = guard_tail("helm work install-guard", rest, flags=("--apply",),
                         valued=("--repo", "--seat", "--profile"),
                         usage="work install-guard [--apply] [--repo PATH] "
                               "[--profile rail|leak]")
        if grc is not None:
            return grc
        profile = seats._flag(rest, "--profile")
        if profile is not None and profile not in _guard.GUARD_PROFILES:
            print("helm work install-guard: --profile must be one of %s"
                  % ", ".join(sorted(_guard.GUARD_PROFILES)), file=sys.stderr)
            return 2
        rc, lines = install_guard(root, apply="--apply" in rest,
                                  profile=profile)
        for ln in lines:
            print(ln, file=sys.stdout if rc == 0 else sys.stderr)
        return rc
    if verb == "stash":
        from ._stash import act, list_lines
        sub = rest[0] if rest and not rest[0].startswith("--") else "list"
        if sub == "list":
            print("\n".join(list_lines(root)))
            return 0
        needle = " ".join(x for x in rest[1:] if not x.startswith("--"))
        lines, err = act(root, sub, needle)
        if err:
            print("helm work stash: " + err, file=sys.stderr)
            return 2
        print("\n".join(x for x in lines if x))
        return 0
    print(USAGE, file=sys.stderr)
    return 2
