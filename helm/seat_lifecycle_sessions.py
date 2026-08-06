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


def _seat_family(seat_name):
    """'codex' -> codex, 'codex-3' -> codex (slice-6 instances); unknown ->
    (None, reason). A resume must never mint a seat that was never added."""
    if seat_name in FAMILIES:
        return seat_name, None
    base, _, tail = seat_name.rpartition("-")
    if base in FAMILIES and tail.isdigit():
        return base, None
    return None, ("unknown seat '%s' (families: %s; instances: <family>-N)"
                  % (seat_name, ", ".join(sorted(FAMILIES))))


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
    session itself outranks a stale prune of some dead session (codex-2's
    05:22 prune targeted a session that died at 23:36 while the recorded one
    ran until morning), which outranks ordinary work, and only then do real
    turns and mtime decide."""
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
    # by helm-claude-2 after the 10:26 reboot: `helm seat resume` would have
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
    resume must not silently turn a derived default into an explicit home."""
    try:
        with open(path) as f:
            tokens = shlex.split(f.read(), comments=True)
    except (OSError, ValueError):
        return None, None
    values = {}
    for token in tokens:
        for name in ("HELM_CHAT_ROOM", "HELM_CHAT_ROOM_SOURCE"):
            prefix = name + "="
            if token.startswith(prefix):
                values[name] = token[len(prefix):]
    source = values.get("HELM_CHAT_ROOM_SOURCE")
    return values.get("HELM_CHAT_ROOM"), \
        source if source == "derived" else None


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
    cadence. Failure is loud but never blocks the requested seat operation."""
    from . import autocompact
    ok, detail = autocompact.ensure_timer()
    if not ok:
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
# ISOLATION LAW (LAYER 1, prd/COORDINATION-SUBSTRATE-DESIGN.md): a seat spawned
# without an explicit --cwd lands in its OWN home worktree
# (`<repo>-wt/seats/<seat>`, provisioned create-or-reuse through the
# metaharness seam), NEVER the shared main checkout. Trust-seeding follows the
# cwd (_write_launch_assets(workdir=cwd)) and homing folds back to the project
# root via --git-common-dir, so isolation costs no room scatter.

SPAWN_SEND_DELAY_S = 5   # pane-boot grace before the onboarding keystrokes
                         # (HELM_SPAWN_SEND_DELAY overrides; tests set 0)


def onboarding_prompt(seat_name, room=None):
    """The seat's self-onboarding FIRST PROMPT — identical across all three
    spawn paths (only the delivery differs). One line, no newlines (it rides
    `terminal send`/`pane run` as a single keystroke burst) and no secrets
    (it crosses the adapter seam). Content law: arm the beacon FIRST (the only
    idle wake), read the home room, announce, take @<seat> work. room=None is
    NOT 'main': the home derives at SessionStart join (seats.resolve_homing)
    — the prompt says so instead of inventing a room the roster never wrote."""
    r = room or "derived at join — `helm chat seats` shows it"
    flag = "" if not room or room == "main" else " --room %s" % shlex.quote(room)
    return ("You are helm fleet seat '%(s)s' (home room %(r)s). Self-onboard "
            "now, in order: (1) ARM YOUR INBOX BEACON before anything else — "
            "Monitor(command: \"helm chat wait --seat %(s)s --follow\", "
            "persistent: true). It wakes you on @%(s)s mentions, replies to "
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
            "taking @%(s)s work\"`. (4) TAKE WORK FROM THE LEDGER FOLD, not "
            "from history: `helm dispatch list --open` rows naming @%(s)s "
            "are your ONLY live obligations. A row's CURRENT STATUS decides "
            "— cancelled, superseded and verdicted rows are DEAD however "
            "recent or open the chat about them reads, so check the row "
            "before claiming; two recovered seats in one hour rebuilt "
            "already-superseded work straight out of history. And if the "
            "fold itself cannot be read, your obligations are UNKNOWN, "
            "never empty — report the unreadable ledger and do not infer "
            "no-work from a failed read. Owner posts "
            "addressed to you are live too. Do the work, reply in the room, "
            "and when idle again stay parked on the beacon. (5) END YOUR "
            "TURNS: at "
            "every bounded milestone post progress and STOP — the beacon "
            "re-wakes you; that is what it is for. A turn held open blocks "
            "queued messages and /compact, inflates context toward the "
            "100%% cliff, and is indistinguishable from a hang. A routine "
            "event = process, post, END."
            % {"s": seat_name, "r": r, "f": flag})


def rearm_prompt(seat_name):
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
    return ("You were just RELAUNCHED (helm seat resume). Your inbox beacon "
            "is a per-session Monitor process and did NOT survive the "
            "restart: nothing can wake you until you re-arm it. FIRST "
            "ACTION, before anything else: Monitor(command: \"helm chat "
            "wait --seat %(s)s --follow\", persistent: true). If Monitor is "
            "not in your tool surface it is DEFERRED, not absent — load it "
            "with ToolSearch(query: \"select:Monitor\"), then arm it. Do "
            "NOT substitute a background `helm chat wait` shell — a "
            "background process cannot re-invoke your turn loop, so it is "
            "not a beacon. THEN catch up: `helm chat read` — chat is "
            "durable, so rows addressed to you during the restart are "
            "waiting — and continue your in-flight work; your live "
            "obligations are the OPEN dispatch rows naming @%(s)s (`helm "
            "dispatch list --open`). If you could not arm the beacon, say "
            "so plainly instead of going idle." % {"s": seat_name})
