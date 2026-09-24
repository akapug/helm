"""Session discovery and onboarding prompts for :mod:`helm.seat`."""
import contextlib as _contextlib
import glob
import json
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import time

from . import seats_advice
from .seat_role import SEAT_ROLES


def _named_seat_family(seat_name):
    """'codex' -> codex, 'codex-3' -> codex (slice-6 instances); unknown ->
    (None, reason). THE NAME GRAMMAR ALONE, and no I/O of any kind: this is the
    rung every ordinary seat operand is answered by, and its cost must stay
    exactly what it was."""
    if seat_name in FAMILIES:
        return seat_name, None
    base, _, tail = seat_name.rpartition("-")
    if base in FAMILIES and tail.isdigit():
        return base, None
    return None, ("unknown seat '%s' (families: %s; numbered instances: %s; "
                  "project seats: <project>-<family> for %s, and only once "
                  "`helm seat spawn` has registered one)"
                  % (seat_name, ", ".join(sorted(FAMILIES)),
                     ", ".join("%s-N" % name for name in numbered_families()),
                     ", ".join(project_families())))


def registered_seat_family(seat_name):
    """(family, project) THE SEAT'S OWN SPAWN REGISTER names, else (None, None).

    THE REGISTER IS THE AUTHORITY, NEVER THE DISPLAY NAME. `acme-codex` looks
    like a project-canonical seat and `pi-codex` looks identical, so a splitter
    that read the last segment would hand every consumer a provider family it
    guessed from a string an operator typed. What this reads instead is the
    record the SPAWN PRODUCER wrote: the name only proposes WHICH instance
    directory to open, and the recorded `family` decides — and only when the
    record agrees with the tree it was found in (`rec["family"] == family` and
    `rec["seat"] == seat_name`). A name nothing spawned resolves to nothing,
    which is what keeps an orca-adopted pane on the adoption path.

    Deliberately bounded to `project_families()` (the families a project seat
    may name), so this walks two directories at most and never becomes a scan.

    (None, None) says only that no register RESOLVED the name; whether one
    EXISTS and cannot answer is the third slot of `_register_reading`.
    """
    return _register_reading(seat_name)[:2]


def _register_reading(seat_name):
    """(family, project, defect) — ONE walk of the registers a project-canonical
    name may have, answering both what they resolve and, when that is nothing,
    why.

    NO RECORD AND A BROKEN RECORD ARE DIFFERENT FACTS WITH DIFFERENT REPAIRS.
    With no record the name was never spawned, and "unknown seat" is the truth.
    A record that exists but is missing a key the spawn door writes was written
    by a door that dropped the key: the seat is real, its pane may be live, and
    the repair is to restore the key, never to re-spawn the name. Reporting both
    as "unknown seat" degraded such a seat silently and permanently, because
    the reboot sweep's first rung answers UNKNOWN and never re-stamps it.

    `defect` decides nothing. It is None whenever a register resolved, it
    changes no resolution, and it reaches no caller as a new state: it is only
    the TEXT of the refusal `_seat_family` was already going to return, so every
    consumer branch stays exactly as it was.
    """
    if not seat_name or "-" not in seat_name:
        return None, None, None
    if seat_name.rpartition("-")[2].isdigit():
        return None, None, None    # the numbered grammar already answered
    from .seat_launch_assets import _instance_dir
    from .seat_lifecycle import _spawn_path, _spawn_record
    found = []
    for family in project_families():
        d = _instance_dir(family, seat_name)
        rec = _spawn_record(d)
        if rec is None:
            if os.path.lexists(_spawn_path(d)):
                found.append("%s exists and does not read as a JSON object"
                             % _spawn_path(d))
            continue
        if rec.get("seat") != seat_name:
            continue
        if rec.get("family") == family:
            return family, rec.get("project"), None
        # a record that disagrees or is silent decides nothing — it is named
        if rec.get("family"):
            found.append("%s names family %r but sits in the %s seat tree"
                         % (_spawn_path(d), rec["family"], family))
            continue
        missing = [key for key in ("family", "project") if not rec.get(key)]
        found.append("%s is an INCOMPLETE register, missing %s (it sits in "
                     "the %s seat tree)"
                     % (_spawn_path(d), " and ".join(missing), family))
    if not found:
        return None, None, None
    return None, None, (
        "seat '%s' has a spawn register that does not resolve it: %s. A "
        "register that exists and cannot answer is a broken record, not an "
        "unregistered name — restore the missing fields from the seat's last "
        "complete record; do not re-spawn the name"
        % (seat_name, "; ".join(found)))


def _seat_family(seat_name):
    """'codex' -> codex, 'codex-3' -> codex; a REGISTERED project-canonical
    seat -> the family its own register names; unknown -> (None, reason).

    Two rungs, in this order. The name grammar answers first and pays nothing,
    so `codex` / `codex-7` cost exactly what they always did. Only a name the
    grammar REFUSED reaches the register, and there the answer comes from the
    producer's own record rather than from the shape of the name — which is why
    `helm seat where|resume|up|down` and the reboot sweep can all consume a
    project-canonical seat without any of them learning a naming convention.
    A resume must never mint a seat that was never added, and that still holds:
    nothing registered, nothing resolved. A register that EXISTS and still
    resolves nothing is refused too, but with its own reason in place of the
    grammar's "unknown seat" (`_register_reading`)."""
    family, err = _named_seat_family(seat_name)
    if not err:
        return family, None
    family, _project, defect = _register_reading(seat_name)
    return (family, None) if family else (None, defect or err)


def family_for(seat_name, runtime=None, runtime_verified=False):
    """Resolve provider family from verified launch metadata, then seat name.

    Display identity is not capability identity: a pi harness may call its seat
    ``pi-codex`` while the credential wall belongs to family ``codex``. Roster
    metadata is authority-bearing only after the launched process self-wrote it;
    a foreign launch mirror is useful display evidence but cannot steer delivery.
    Name parsing remains the compatibility fallback for ordinary ``family[-N]``
    seats and callers that have no verified runtime row.
    """
    if runtime_verified is True and isinstance(runtime, dict):
        family = runtime.get("family")
        if family in FAMILIES:
            return family, None
    return _seat_family(seat_name)


def _unknown_seat_reason(seat_name, fallback):
    """Suggestion-only identity evidence; never a replacement seat operand."""
    from . import seat_identity
    return seat_identity.alias_refusal(
        seat_name, closed=True, fuzzy=FAMILIES) or fallback


# THE PROJECT-CANONICAL SEAT NAME (task/2440). Owner canon: teams are per TLA,
# each actively worked project gets one claude TLA and one codex TLA named
# <project>-<family>, and a team using helm must never need to know how helm is
# made. Until now a seat could only be spawned as a family-number, so every
# premise, row and brief about a project's seat named a number that says nothing
# about the project — one numbered codex seat served another project for two
# days and only its operator knew.
#
# THE NUMBERED FORM IS RESOLVED FIRST AND UNTOUCHED, with no registry read and no
# I/O at all: `_named_seat_family` is the entire answer for `codex` / `codex-7`,
# so every caller of that grammar keeps its cost and behaviour exactly. Only a
# name that rung REFUSES reaches either the project branch (`spawn_identity`, at
# the spawn door) or the seat's own register (`registered_seat_family`, for every
# consumer downstream of a spawn).
NATIVE_FAMILY = "claude"


def numbered_families():
    """Every family a NUMBERED seat (`codex-3`) may name: mode=proxy, exactly.

    A NUMBERED SEAT IS A PER-INSTANCE PROXY, and that is an OAuth-POOL feature:
    `_instance_gate` refuses mode=proxy-key for any seat that is not the family
    seat itself, so `kimi-2` has never been spawnable and never will be by this
    door. The BARE form is a different question, which every family in the table
    answers — so this is its own list rather than a filter each message is
    trusted to remember. It exists because the refusal for `proj-a-kimi` offered
    `kimi-N` as the recovery, sending an operator to a door that refuses every
    numbered kimi there is; a suggested form the next gate declines is worse than
    no suggestion, because it looks like an answer.
    """
    from .seat_catalog import FAMILIES as table
    return sorted(name for name, fam in table.items()
                  if fam.get("mode") == "proxy")


def spawn_families():
    """Every family a BARE seat may name: the proxy table exactly.

    BARE, not numbered — `numbered_families` is the narrower list, and the two
    are separate because only one of them is the whole table.

    `claude` is deliberately NOT here. It has no proxy, no port and no
    translated cred, and there is no such thing as a bare `claude` or a
    `claude-2` seat — native claude is only ever spawned as `<project>-claude`
    (see `project_families`). Including it here is what makes the help and the
    refusals advertise two forms the resolver then declines: an ADVERTISED
    COMBINATION THE GATE REFUSES IS A LIE THE OPERATOR PAYS FOR, so each list
    names exactly the forms its own door admits.
    """
    from .seat_catalog import FAMILIES as table
    return sorted(table)


def project_families():
    """Every family the PROJECT-CANONICAL `<project>-<family>` form may name.

    Exactly the families whose spawn door admits a non-family instance:
      * native `claude`, which has no proxy at all and rides `helm launch`;
      * every mode=proxy family, because a per-instance proxy is an OAuth-pool
        feature — `_instance_gate` refuses mode=proxy-key for any seat that is
        not the family seat itself, so advertising `<project>-kimi` would
        advertise a name the very next rung declines.

    That set IS the owner canon it was derived from independently: one claude
    seat and one codex seat per actively worked project.
    """
    from .seat_catalog import FAMILIES as table
    return sorted({NATIVE_FAMILY} | {name for name, fam in table.items()
                                     if fam.get("mode") == "proxy"})


def _registry_path():
    from . import home as home_mod
    return home_mod.registry_path()


def _registered_projects():
    """(sorted names, None) or (None, why) — the project registry population.

    Strict, because an unreadable authority root that read as "no projects"
    would refuse every project name there is and blame the operator's spelling.
    """
    from . import registry
    try:
        projects = registry.load(strict=True).get("projects") or {}
    except Exception as e:
        return None, "%s: %s" % (type(e).__name__, e)
    return sorted(projects), None


def spawn_identity(seat_name):
    """(family, project, err) for `seat spawn`'s operand — the name door.

    Two accepted forms, in this order:
      * `<family>` / `<family>-N` — unchanged, and never reads the registry;
      * `<project>-<family>` — the project-canonical TLA, where <project> is
        registered and <family> is one of `project_families()`. THE FAMILY IS
        THE LAST SEGMENT, so a project whose own name carries a family word
        cannot shadow one.

    EVERY ADVERTISED COMBINATION IS ONE SOME DOOR BELOW ACTUALLY ADMITS, and
    each refusal names the form it is about rather than a flat family list:
    bare/numbered `claude` is refused as a form (native claude has no proxy to
    number), and `<project>-<proxy-key-family>` is refused as a form too, since
    `_instance_gate` would decline it one rung later. Whichever list a message
    prints, the reader can act on it.

    `err` is the final text to print. The unknown-tail case keeps the
    alias/fuzzy decoration every closed-world seat operand already gets AND
    still names the two authorities, because a fuzzy hint that REPLACED them
    would answer a question the operator did not ask.
    """
    family, fam_err = _named_seat_family(seat_name)
    if not fam_err:
        return family, None, None
    base, _, tail = seat_name.rpartition("-")
    known = project_families()
    numbered = numbered_families()
    bare = spawn_families()
    # EVERY LIST NAMES THE FORMS ITS OWN DOOR ADMITS, and the three doors differ:
    # any family may be spawned bare, only an OAuth-pool family may be numbered,
    # and only a family with a non-family instance may carry a project.
    forms = ("a seat is <family> (%s), <family>-N for the per-instance-proxy "
             "families (%s), or the project-canonical <project>-<family> (%s)"
             % (", ".join(bare), ", ".join(numbered), ", ".join(known)))
    if seat_name == NATIVE_FAMILY or (base == NATIVE_FAMILY and tail.isdigit()):
        # THE ONE COMBINATION THE OLD LIST ADVERTISED AND NO DOOR ACCEPTED.
        # `claude` is a family for the PROJECT form only: it has no proxy to
        # number and a bare native seat is the fleet-wide default identity,
        # not a seat helm mints. Refuse it here, in the same breath as the
        # grammar, rather than in an "unknown seat" line naming claude as a
        # family the operator may use.
        return None, None, (
            "native claude has no bare or numbered seat form — `%s` names no "
            "proxy to instance and no project; it is spawned as "
            "<project>-claude (%s)" % (seat_name, forms))
    if not base or tail not in known:
        text = ("unknown seat '%s' — %s; projects: the registry at %s"
                % (seat_name, forms, _registry_path()))
        if base and tail in bare:
            # A REAL FAMILY AT THE WRONG DOOR is its own answer: kimi/gemini
            # are spawnable, just never as a project instance, and saying
            # "unknown family" about a name in the table sends the operator
            # to check their spelling instead of their form.
            #
            # AND THE RECOVERY IT OFFERS IS ONE THIS FAMILY'S OWN GATE ADMITS.
            # `kimi-N` was offered here to every family, including the
            # proxy-key ones whose numbered form `_instance_gate` refuses
            # outright — the operator follows the suggestion and meets a second
            # refusal, which reads as helm contradicting itself.
            text = ("seat '%s' names family '%s', which has no per-instance "
                    "proxy — a project-canonical seat is one of %s; spawn `%s`%s "
                    "instead (projects: the registry at %s)"
                    % (seat_name, tail, ", ".join(known), tail,
                       " or `%s-N`" % tail if tail in numbered else "",
                       _registry_path()))
        hint = _unknown_seat_reason(seat_name, "")
        return None, None, (hint + " — " + text) if hint else text
    projects, err = _registered_projects()
    if projects is None:
        return None, None, (
            "seat '%s' names project '%s' and the registry at %s cannot be "
            "read (%s) — a seat is never bound to a project helm cannot see "
            "(families: %s)"
            % (seat_name, base, _registry_path(), err, ", ".join(known)))
    if base not in projects:
        return None, None, (
            "unknown project '%s' in seat name '%s' — the registry at %s holds "
            "%s; register the checkout (`helm sync`) or spawn a numbered seat "
            "(families: %s)"
            % (base, seat_name, _registry_path(),
               ", ".join(projects) or "no projects", ", ".join(known)))
    return tail, base, None


def _split_seat(seat_name):
    """'codex' -> ('codex', 'codex'), 'codex-3' -> ('codex', 'codex-3'): the
    family (which FAMILIES entry / cred pool) beside the full seat identity
    (whose proxy/config/session). Falls back to (seat_name, seat_name) so a
    bare family name is instance 1."""
    fam, _ = _seat_family(seat_name)
    fam = fam or seat_name
    return fam, seat_name


_SESSION_JSONL = re.compile(r"^[0-9a-fA-F-]{36}\.jsonl$")


# Only a SMALL file can be a crash stub worth reading; a large transcript is a
# real session by construction and must never be read in full to find out (the
# fleet has 131MB+ transcripts, and this runs on the resume path).
_STUB_MAX_BYTES = 256 * 1024


def _has_real_turn(path, size):
    """True when the transcript contains at least one assistant turn.

    The discriminator between a real session and a reboot stub. Deliberately
    NOT a size threshold on its own: a legitimately fresh session is also
    small, and ranking by size would make a seat unable to resume work it
    started five minutes ago."""
    if size > _STUB_MAX_BYTES:
        return True
    try:
        with open(path, "rb") as f:
            raw = f.read(_STUB_MAX_BYTES)
    except OSError:
        return False
    return b'"assistant"' in raw


_PRUNE_HEAD_BYTES = 4096


def _prune_source(path):
    """The session id this transcript is a PRUNED COPY of, or None.

    Measured 2026-08-04 across the night's six rescues: cv prune --revive
    writes its target in one of two shapes, both visible in the file's head —
    claude-shaped records whose "session_id" (snake) keeps the SOURCE session
    while "sessionId" (camel) carries the copy's own id, or tool-slot records
    ({"slot": ...}) that carry no session identity at all. A normal transcript
    never carries a session_id that differs from its own sessionId, so a
    foreign session_id IS the lineage stamp, never an accident of quoting.
    """
    try:
        with open(path, "rb") as f:
            head = f.read(_PRUNE_HEAD_BYTES)
    except OSError:
        return None
    first = head.split(b"\n", 1)[0]
    if b'"slot"' in first:
        return True        # slot-shaped prune copy; lineage unrecorded
    src = re.search(rb'"session_id"\s*:\s*"([0-9a-f-]{36})"', head)
    own = re.search(rb'"sessionId"\s*:\s*"([0-9a-f-]{36})"', head)
    if src and own and src.group(1) != own.group(1):
        return src.group(1).decode("ascii")
    return None


def _newest_seat_session(instance_dir, prefer_source=None):
    """(session_id, cwd) of the seat's newest claude session, from its OWN
    isolated CLAUDE_CONFIG_DIR (<instance>/claude/projects/<slug>/<uuid>.jsonl);
    (None, None) when the seat never ran. The id feeds `--resume <id>`, the
    sniffed cwd re-homes the pane where the session actually worked. Only
    uuid-named files count — a sidecar must fall through to --continue, never
    resume the wrong transcript. (Every helm seat runs the `claude` binary —
    codex seats are claude-over-proxy — so claude's resume flags are universal
    here; a raw `codex resume` pane is not a helm seat.)

    prefer_source is the seat's RECORDED session (spawn.json). Ranking:
    a pruned copy OF the recorded session outranks everything — measured
    2026-08-04, when prune+resume landed 3 of 5 seats back on the WALLED
    original because pure content+mtime ranking cannot see that a copy whose
    content is older-but-bounded is the rescue, not a stranger. The recorded
    session itself outranks a stale prune of some dead session (a codex
    seat's 05:22 prune targeted a session that died at 23:36 while the
    recorded one ran until morning), which outranks ordinary work, and only
    then do real turns and mtime decide."""
    cands = []
    for p in glob.glob(os.path.join(instance_dir, "claude", "projects",
                                    "*", "*.jsonl")):
        if not _SESSION_JSONL.match(os.path.basename(p)):
            continue
        try:
            st = os.stat(p)
        except OSError:
            continue
        sid = os.path.basename(p)[:-len(".jsonl")]
        lineage = _prune_source(p)
        if prefer_source and lineage == prefer_source:
            rank = 3                      # the rescue copy of the live session
        elif prefer_source and sid == prefer_source:
            rank = 2                      # the recorded session itself
        elif lineage:
            rank = 1                      # a prune of some dead session
        else:
            rank = 0                      # an ordinary session
        cands.append((rank, _has_real_turn(p, st.st_size), st.st_mtime, p))
    if not cands:
        return None, None
    # A SESSION WITH REAL TURNS OUTRANKS A STUB, and only then does mtime
    # decide. Pure-mtime ranking is correct until a REBOOT, which touches every
    # transcript at once and makes "newest" meaningless — measured 2026-07-29
    # after a reboot: `helm seat resume` would have
    # resumed kimi into a 28K crash stub over its real 26MB session, and 4 of 5
    # seats mismatched. Resuming the wrong transcript does not fail loudly; the
    # seat comes back as a stranger with its work invisible.
    p = max(cands)[3]
    from . import harnesses
    return os.path.basename(p)[:-len(".jsonl")], harnesses._sniff_cwd(p)


def _seat_session_path_by_id(instance_dir, sid):
    """One exact real transcript path in this seat's own config home."""
    if not sid or not _SESSION_JSONL.match(str(sid) + ".jsonl"):
        return None
    paths = glob.glob(os.path.join(instance_dir, "claude", "projects", "*",
                                   str(sid) + ".jsonl"))
    unique = {}
    for p in paths:
        try:
            st = os.stat(p)
        except OSError:
            continue
        if not _has_real_turn(p, st.st_size):
            continue
        unique.setdefault(os.path.realpath(p), p)
    return next(iter(unique.values())) if len(unique) == 1 else None


def _seat_session_by_id(instance_dir, sid):
    """(sid, cwd) for one exact session in this seat's own config home.

    Recovery must resume the COPY cv just minted, never whichever transcript
    happens to win a newest-mtime race. Multiple project-slug symlinks to the
    same file are one identity; distinct files carrying one UUID are ambiguous
    and refuse rather than selecting by directory order.
    """
    p = _seat_session_path_by_id(instance_dir, sid)
    if not p:
        return None, None
    from . import harnesses
    return str(sid), harnesses._sniff_cwd(p)


def _homing_from_launch(path):
    """The seat's room + provenance recovered from launch.sh. Shell-aware
    tokenization preserves quoted values and ignores the generated comments;
    resume must not silently turn a derived default into an explicit home.

    THREE OUTCOMES, NOT TWO. (room, source) with no room is `cleared` when the
    script carries that stamp — the seat's cwd was measured and has no project
    — and (None, None) only when the script expresses no opinion at all. An
    unreadable script is (None, None) as well, which is honest: it is exactly
    the state of knowing nothing. Callers that home a room-less seat must read
    the source before falling back to any ambient resolution, or a deliberate
    clear becomes whatever room the invoking process happened to sit in."""
    try:
        with open(path) as f:
            tokens = shlex.split(f.read(), comments=True)
        nested = []
        direct = ("HELM_CHAT_ROOM=", "HELM_CHAT_ROOM_SOURCE=")
        for token in tokens:
            if "HELM_CHAT_ROOM" in token and not token.startswith(direct):
                nested.extend(shlex.split(token, comments=True))
        tokens.extend(nested)
    except (OSError, ValueError):
        return None, None
    values = {}
    for token in tokens:
        for name in ("HELM_CHAT_ROOM", "HELM_CHAT_ROOM_SOURCE"):
            prefix = name + "="
            if token.startswith(prefix):
                values[name] = token[len(prefix):]
    from .seats_identity import ROOM_CLEARED
    source = values.get("HELM_CHAT_ROOM_SOURCE")
    room = values.get("HELM_CHAT_ROOM")
    if not room:
        return None, (ROOM_CLEARED if source == ROOM_CLEARED else None)
    return room, source if source == "derived" else None


def _room_from_launch(path):
    """Back-compatible room-only view used by the multi-resume seam."""
    return _homing_from_launch(path)[0]


def _multi_from_launch(path):
    """Recover the seat's mixed-model shape from launch.sh. --multi's durable
    marker is the ABSENCE of the blunt CLAUDE_CODE_SUBAGENT_MODEL pin; every
    single-model launch assigns it. Missing/unreadable assets default safely to
    the normal pinned shape."""
    try:
        with open(path) as f:
            return "CLAUDE_CODE_SUBAGENT_MODEL=" not in f.read()
    except OSError:
        return False


def _ensure_autocompact_timer():
    """Best-effort lifecycle wiring: a running proxy seat needs its prevention
    cadence. Failure is loud but never blocks the requested seat operation.
    An install that HELM_AUTOCOMPACT_TIMER turned off (ok is None) is not a
    failure and not an armed timer: it is one NOTE line that names the
    variable, and the seat operation continues."""
    from . import autocompact
    ok, detail = autocompact.ensure_timer()
    if ok is None:
        print("helm seat: NOTE — autocompact timer not armed: " + detail,
              file=sys.stderr)
    elif not ok:
        print("helm seat: WARN — autocompact timer not armed: " + detail,
              file=sys.stderr)
    return ok




# ---------------------------------------------------------------------------
# spawn / where — the harness-agnostic SELF-ONBOARDING seat spawn
# ---------------------------------------------------------------------------
# THE GAP this closes: a hand-spawned seat is a BARE idle pane — no beacon,
# no work, not addressable (feature without RSH = dead scaffolding). One verb,
# THREE spawn paths dispatched by harness.detect():
#   headless  no metaharness (the standalone DEFAULT — helm is the substrate,
#             orca/herdr are optional front-ends): the minted launch.sh runs
#             DETACHED (start_new_session=True IS setsid; nohup-equivalent io
#             to spawn.log), and the onboarding rides as launch.sh's
#             positional arg — launch.sh execs `claude … "$@"`, so the prompt
#             is the seat's FIRST TURN, self-run at boot. No pane to inject
#             into ⇒ deliver at launch time.
#   orca      adapter.spawn (terminal create, handle captured) + adapter.send
#             --enter of the onboarding first-prompt into the pane.
#   herdr     the same two seam calls (agent start + pane run) via the adapter.
# Common to all: mint hygiene via _write_launch_assets (child-stamp stripped ⇒
# persistence forced ON, --dangerously canonical, skills linked), DUP-NAME
# REAP first (a prior bare same-name seat is killed/closed — the exact live
# bug), and a ROSTER REGISTER (spawn.json + chat-roster mirror) so any agent
# can `helm seat where <name>` and reap. TOKEN LAW holds: the adapter seam
# carries the launch.sh PATH + the secret-free onboarding text, never the
# expanded launch line.
# ISOLATION LAW (LAYER 1 of the coordination substrate): a seat spawned
# without an explicit --cwd lands in its OWN home worktree
# (`<repo>-wt/seats/<seat>`, provisioned create-or-reuse through the
# metaharness seam), NEVER the shared main checkout. Trust-seeding follows the
# cwd (_write_launch_assets(workdir=cwd)) and homing folds back to the project
# root via --git-common-dir, so isolation costs no room scatter.

SPAWN_SEND_DELAY_S = 5   # pane-boot grace before the onboarding keystrokes
                         # (HELM_SPAWN_SEND_DELAY overrides; tests set 0)


def onboarding_prompt(seat_name, room=None, role="worker"):
    """The seat's self-onboarding FIRST PROMPT — identical across all three
    spawn paths (only the delivery differs). One line, no newlines (it rides
    `terminal send`/`pane run` as a single keystroke burst) and no secrets
    (it crosses the adapter seam). Content law: arm the beacon FIRST (the only
    idle wake), read the home room, announce, read the rows ADDRESSED TO
    @<seat>, and wait for rows. A fresh seat is a reader of its own inbox and
    nothing else: it takes no row that names another seat, because the seat
    named on a row may be reading it at that moment. room=None is
    NOT 'main': the home derives at SessionStart join (seats.resolve_homing)
    — the prompt says so instead of inventing a room the roster never wrote."""
    if role not in SEAT_ROLES:
        raise ValueError("unknown seat role %r" % role)
    r = room or "derived at join — `helm chat seats` shows it"
    flag = "" if not room or room == "main" else " --room %s" % shlex.quote(room)
    posture = (
        "Owner posts addressed to you are live too. You are an explicit FLEET "
        "LEAD: when your assigned rows are empty, inspect `helm task list` and "
        "unowned OPEN dispatches for eligible project work; claim or route a "
        "bounded lane instead of parking. Use subagents for independent work "
        "and workflows when ultracode is enabled. After handing work off, post "
        "progress and STOP instead of holding the parent turn open; delegate "
        "completion re-invokes you. The stop guard retains a held lease only on "
        "positive live/recent delegation evidence; UNKNOWN still blocks. Do not "
        "duplicate delegated work. Do not park while eligible work queues."
        if role == "lead" else
        "Owner posts addressed to you are live too. Do the work, reply in the "
        "room, and when idle again stay parked on the beacon.")
    return ("You are helm fleet seat '%(s)s' (role %(role)s, home room %(r)s). "
            "You are pre-authorized to use Agent subagents; never ask the owner "
            "for permission to delegate. Self-onboard "
            "now, in order: (1) ARM YOUR INBOX BEACON before anything else — "
            "%(arm)s. %(expiry)s It wakes you on @%(s)s mentions, replies to "
            "your rows, DMs and @all — NOT on ambient home-room chatter "
            "(read the room when you wake; add --ambient only if your room "
            "is quiet). Monitor NOT in your tool surface? It is "
            "DEFERRED, not absent — load it with ToolSearch(query: "
            "\"select:Monitor\"), then arm it. Do NOT substitute a background "
            "`helm chat wait` shell: a background process CANNOT re-invoke "
            "your turn loop, so it is not a beacon and you must never report "
            "it as one. Nothing external can wake an idle seat, so the beacon "
            "is mandatory — say so plainly if you could not arm it. "
            "(2) CATCH UP: "
            "`helm chat read%(f)s` — read the room before acting, but the "
            "room is CONTEXT, never your work list. (3) "
            "ANNOUNCE: `helm chat post%(f)s \"%(s)s online — beacon armed, "
            "reading rows addressed to @%(s)s\"`. (4) READ THE LEDGER FOLD "
            "FOR ROWS ADDRESSED TO YOU, then WAIT FOR ROWS — never history, "
            "and never another seat's work: `helm dispatch list --mine "
            "--open`. That spelling is the one to use because it keeps ONLY "
            "the rows that name you and REFUSES outright when your identity "
            "cannot be resolved, where the unfiltered listing shows you the "
            "whole project and leaves the filtering to your judgement. A row "
            "addressed to a DIFFERENT seat belongs to that seat however long "
            "it has sat and however quiet its room is: you do not move it, "
            "close it, or answer it, and a fresh seat that does takes work "
            "out from under somebody who is mid-read. An empty list means you "
            "are idle BY DESIGN — say so and park on the beacon. A row's "
            "CURRENT STATUS decides "
            "— cancelled, superseded and verdicted rows are DEAD however "
            "recent or open the chat about them reads, so check the row "
            "before acting; two recovered seats in one hour rebuilt "
            "already-superseded work straight out of history. And if the "
            "fold itself cannot be read, your obligations are UNKNOWN, "
            "never empty — report the unreadable ledger and do not infer "
            "no-work from a failed read. %(posture)s (5) END YOUR "
            "TURNS: at "
            "every bounded milestone post progress and STOP — the beacon "
            "re-wakes you; that is what it is for. A turn held open blocks "
            "queued messages and /compact, inflates context toward the "
            "100%% cliff, and is indistinguishable from a hang. A routine "
            "event = process, post, END."
            % {"arm": seats_advice.beacon_monitor(seat_name),
               "expiry": seats_advice.BEACON_EXPIRY,
               "s": seat_name, "role": role, "r": r, "f": flag,
               "posture": posture})


def rearm_prompt(seat_name, role="worker"):
    """The resumed seat's first prompt — the WAKE-PATH half of a restart.

    A seat's inbox beacon is a PER-SESSION Monitor process: it survives a
    compaction (/compact compacts the conversation, not the process — verified
    live, notifications arriving DURING the window) but NOT a process restart.
    The SessionStart join line directs the arm, but a resumed session gets no
    TURN to act on it — the directive sits in context while the seat sits at
    its composer, alive and DEAF (measured 2026-08-03: six of seven restored
    seats, a fleet-wide brief reaching nobody for hours). This prompt IS that
    turn: injecting it starts the turn the re-arm happens on. Same content law
    as onboarding_prompt: one line (a single keystroke burst), no secrets,
    beacon FIRST, then catch up — chat is durable, so rows sent during the
    restart are waiting, not lost."""
    if role not in SEAT_ROLES:
        raise ValueError("unknown seat role %r" % role)
    posture = (
        " After the in-flight obligation, resume fleet-lead posture: inspect "
        "`helm task list` and unowned OPEN dispatches, claim or route eligible "
        "work, and use subagents/workflows for independent lanes. After handing "
        "work off, post progress and STOP instead of holding the parent turn "
        "open; delegate completion re-invokes you. The stop guard retains a "
        "held lease only on positive live/recent delegation evidence; UNKNOWN "
        "still blocks. Do not park while eligible work queues."
        if role == "lead" else "")
    return ("You were just RELAUNCHED (helm seat resume, role %(role)s). You are "
            "pre-authorized to use Agent subagents; never ask the owner for "
            "permission to delegate. Your inbox beacon "
            "is a per-session Monitor process and did NOT survive the "
            "restart: nothing can wake you until you re-arm it. FIRST "
            "ACTION, before anything else: %(arm)s. %(expiry)s If Monitor is "
            "not in your tool surface it is DEFERRED, not absent — load it "
            "with ToolSearch(query: \"select:Monitor\"), then arm it. Do "
            "NOT substitute a background `helm chat wait` shell — a "
            "background process cannot re-invoke your turn loop, so it is "
            "not a beacon. THEN catch up: `helm chat read` — chat is "
            "durable, so rows addressed to you during the restart are "
            "waiting — and continue your in-flight work; your live "
            "obligations are the OPEN dispatch rows naming @%(s)s (`helm "
            "dispatch list --open`).%(posture)s If you could not arm the "
            "beacon, say so plainly instead of going idle."
            % {"arm": seats_advice.beacon_monitor(seat_name),
               "expiry": seats_advice.BEACON_EXPIRY,
               "s": seat_name, "role": role, "posture": posture})
