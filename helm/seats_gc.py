#!/usr/bin/env python3
"""helm seats — the roster GC: the one verb allowed to DELETE a roster row.

EXTRACTED FROM seats_report BECAUSE OF THE SPLIT BUDGET, and the ratchet is
only what forced the timing. seats_report.py stood at 999 lines against the
1000-line finish line, so the next author to add two lines to it — on a lane
about something else entirely — would have been refused at commit by a rung
naming a budget they had never heard of, and a compose merging two lanes that
are each individually under the line would have handed that same refusal to an
integrator mid-train.

THE SEAM WAS ALREADY THERE, which is why this is an extraction and not a
partition. The old module's docstring argued the GC lived beside the report
because it "needs exactly the evidence the report already gathered". That is
true about the KINDS of evidence and false about the CODE: the two halves
shared not one name. Measured on the parse tree before the move — every
top-level definition, every load of a module-level name — the GC set (REAP_S,
_unlink_seat_state, _transcript_exists, _transcript_hit, _COULD_HOST_SEAT,
_live_process_evidence, _gc_keep_reason, gc_roster) referenced ZERO names from
the report half, and the report half referenced ZERO names from the GC set.
They shared a file and a paragraph of rationale, nothing else. Their only
common ground, `last_seen`, is a seats_roster import both reach for
independently.

AND THEY ARE OPPOSITE KINDS OF THING, which is the argument that would stand
with no budget at all. A REPORT IS A READ: roster_report and presence_report
delete nothing, fail open per row, and exist to be right on a 2-second web
poll. THIS FILE IS A DESTRUCTIVE VERB someone runs: it takes the roster lock,
re-probes under it, and unlinks derived state. The legacy auto-reap that rode
roster_report — cleanup smuggled into a read — is the exact defect the split
of these two responsibilities makes structurally hard to write again.

REFUSAL IS THE DEFAULT here, and every law that makes it so is stated on
_gc_keep_reason, _live_process_evidence and gc_roster below, where the
evidence is handled. Nothing about those laws changed in the move.
"""
import glob
import os
import re
import time

from . import chat, pk
from .seats_common import (_flocked, _seat_key, roster, roster_for_write,
                           roster_path)
from .seats_roster import _key_bounded, last_seen

REAP_S = 3600   # presence window: a beat this recent is live evidence on its
def _unlink_seat_state(seat):
    """Remove keyed state, retaining cursor obligations with a retained DM."""
    from .seats_cursor import _cursor_estate_locks, parse_cursor_path

    key = _seat_key(seat)
    d = chat.chat_dir()
    lane = chat.DM_PREFIX + key
    transcript = chat.room_path(lane)
    # Room before estate matches rotation's order. The roster GC caller already
    # holds the roster lock, so DM append/rename cannot enter while transcript,
    # receipts, and cursor custody are decided as one critical section.
    with chat._room_lock(lane) as locked:
        if not locked:
            return
        with _cursor_estate_locks({key}):
            try:
                os.stat(transcript)
                retained = True
            except FileNotFoundError:
                retained = False
            except OSError:
                retained = True
            if retained:
                try:  # unknown census/prune preserves transcript AND cursors
                    rows, _total, fault = chat.read_checked(lane)
                    if not fault:
                        from .seats_receipts import prune_room_receipts
                        if prune_room_receipts(
                                lane, [row.get("id") for row in rows]):
                            try:
                                os.remove(transcript)
                            except FileNotFoundError:
                                retained = False
                            else:
                                retained = False
                except OSError:
                    pass
            try:
                names = os.listdir(d)
            except OSError:
                return
            for n in names:
                if not _key_bounded(n, key):
                    continue
                path = os.path.join(d, n)
                parsed = parse_cursor_path(path)
                if retained and parsed and parsed["seat_key"] == key \
                        and parsed["room"] == pk.slug(lane):
                    continue
                try:
                    os.remove(path)
                except OSError:
                    pass
def _transcript_exists(sid, roots):
    """Any transcript file naming the session under any harness store:
    claude's <root>/<proj-slug>/<sid>.jsonl, codex's nested
    rollout-<ts>-<sid>.jsonl."""
    s = glob.escape(str(sid))
    for root in roots:
        if not os.path.isdir(root):
            continue
        if glob.glob(os.path.join(root, "*", s + ".jsonl")):
            return True
        if glob.glob(os.path.join(root, "**", "*" + s + "*.jsonl"),
                     recursive=True):
            return True
    return False
def _transcript_hit(sids, roots=None):
    """First remembered session with a transcript on this host, else None.
    Root discovery is NOT ours: session's persistence census
    (session._persisting_sids) is the ONE truth owner — the catalog roots
    (~/.claude + ~/.claude-homes, ~/.codex + ~/.codex-homes) PLUS every helm
    seat home (~/.helm/_global/seats/**/claude/projects). The previous
    hand-rolled root list here omitted helm's own seat stores, so an
    inactive-but-fully-persisted proxy seat probed as junk (a live
    reproduction: its own transcript root missing from the list). An
    explicit roots list (tests / a foreign store) is globbed directly. An
    INCOMPLETE census raises — probe trouble must keep the row, never pass
    as proven-absent."""
    if roots is not None:
        return next((s for s in sids if _transcript_exists(s, roots)), None)
    from . import session
    census = session._persisting_sids()
    hit = next((s for s in sids if s in census), None)
    if hit is None and not getattr(census, "complete", True):
        raise RuntimeError("persistence census incomplete")
    return hit
# Processes that could plausibly carry an agent's environment: a shell, an
# interpreter, a claude/helm/family binary, a node runtime, a metaharness.
# Anything else whose environ we cannot read (systemd --user, dbus, portals)
# carries no evidence about a seat and must not veto the whole scan.
_COULD_HOST_SEAT = re.compile(
    rb"(claude|helm|codex|kimi|gemini|grok|ds4pro|bash|/sh|zsh|python|node|"
    rb"orca|herdr|tmux)", re.I)
def _live_process_evidence(seat, sids, proc_dir="/proc"):
    """Keep-reason when a live process of THIS uid references the seat — its
    cmdline/environ naming a remembered session id, or its environ carrying
    HELM_CHAT_NAME=<seat> (a joined pane whose row remembers no session is
    still a live seat, not junk). FAIL-CLOSED: an unlistable table, or ANY
    same-uid process whose cmdline/environ cannot be read, returns a
    keep-reason — an unfinished scan never testifies to absence. Scope is
    same-uid on purpose: a foreign-uid process cannot host this user's
    harness, and its environ is unreadable by kernel design — counting that
    as trouble would fail-close every gc on any real host into a no-op. A
    process that EXITED mid-scan (ENOENT/ESRCH) is proven not-live and skips
    — that is evidence of absence, not probe trouble."""
    sid_needles = [str(s).encode("utf-8") for s in sids if s]
    seat_needles = [("%s=%s" % (var, seat)).encode("utf-8") + b"\0"
                    for var in ("HELM_CHAT_NAME", "MELD_CHAT_NAME")]
    try:
        me = os.getuid()
        pids = [n for n in os.listdir(proc_dir) if n.isdigit()]
    except OSError as e:
        return "process table unlistable (%s) — fail closed" % e
    for pid in pids:
        pdir = os.path.join(proc_dir, pid)
        try:
            if os.stat(pdir).st_uid != me:
                continue
        except OSError:
            continue                    # exited between listdir and stat
        # A ZOMBIE IS PROVEN NOT-LIVE, and it must be classified BEFORE the
        # readability test below. A defunct process executes nothing and holds
        # no environment; its cmdline reads empty and its environ is
        # unreadable, which is byte-for-byte what "cannot examine" looks
        # like. Read as blindness it vetoes the whole scan, and a single
        # defunct sandbox helper on the host is then enough to make every
        # dead seat permanently uncollectable. Reaped state is evidence of
        # absence, exactly like ENOENT.
        try:
            with open(os.path.join(pdir, "stat"), "rb") as f:
                st = f.read(512)
            # PARSE, DO NOT SEARCH. `comm` sits inside this line and is
            # PROCESS-CONTROLLED, so a substring test for ") Z " is a test a
            # process can satisfy about itself: kimi demonstrated it live with
            # prctl(PR_SET_NAME, "agent) Z live") — 13 bytes, inside the
            # 15-byte cap, no newline or NUL needed — making a LIVE RUNNING
            # process read as a zombie, be skipped as proven-not-live, and let
            # its seat be falsely pruned. A process could name itself into
            # being ignored by the reaper.
            #
            # comm may itself contain ')', so the state field is the character
            # two past the LAST ')' — rfind, never split-on-first.
            rpar = st.rfind(b")")
            if rpar != -1 and st[rpar + 2:rpar + 3] == b"Z":
                continue
        except OSError:
            pass                        # unreadable stat: fall through, judge below
        blob = b""
        blind = False
        for leaf in ("cmdline", "environ"):
            try:
                with open(os.path.join(pdir, leaf), "rb") as f:
                    blob += f.read(1 << 20)
            except (FileNotFoundError, ProcessLookupError):
                continue                # exited mid-scan: proven not-live
            except OSError:
                blind = True            # cannot examine THIS pid — see below
        if blind:
            # BLINDNESS IS PER-PID, AND THAT IS THE WHOLE DESIGN. Treating
            # any unreadable process as a scan-wide veto reduces this guard
            # to a constant: `systemd --user` runs as us on every host with
            # an environ we can never read, so the answer would be "fail
            # closed" on every call — and this branch is only reached for a
            # seat with NO presence beat and NO transcript, i.e. exactly the
            # dead rows the gc exists to collect. A guard that can only ever
            # say KEEP is not a guard.
            #
            # So the blind spot is narrowed by what we CAN read. If the
            # cmdline came through and shows a process that could not be
            # hosting an agent (systemd, dbus, a portal), this pid carries no
            # evidence either way and is skipped. If the cmdline is unreadable
            # too, or names a plausible agent host, the blind spot is REAL and
            # the whole scan still fails closed — unproven is not absent.
            if blob and not _COULD_HOST_SEAT.search(blob):
                continue
            return ("process %s unreadable (permission) and it could be an "
                    "agent host — fail closed" % pid)
        if any(n in blob for n in sid_needles):
            return "a live process references a remembered session"
        if any(n in blob for n in seat_needles):
            return "a live process carries HELM_CHAT_NAME=%s" % seat
    return None
def _gc_keep_reason(seat, row, roots, proc_dir, now):
    """The ONE keep-evidence probe — the dry-run scan AND the locked apply
    both run THIS, so no deletion path can ever act on less evidence than
    the report showed. Returns the keep reason, or None (prunable junk).
    Any raise is probe trouble: the caller keeps the row (fail closed)."""
    sids = [x for x in [row.get("session")]
            + list(row.get("sessions") or []) if x]
    ls = last_seen(seat, row)
    if ls and now - ls < REAP_S:
        return "presence beat %dm ago" % max(0, int((now - ls) / 60))
    hit = _transcript_hit(sids, roots)
    if hit:
        return "transcript exists for session %.12s" % hit
    return _live_process_evidence(seat, sids, proc_dir)
def gc_roster(apply=False, roots=None, proc_dir="/proc", now=None):
    """The roster's ONE cleanup owner (`helm chat seat gc`) — a verb someone
    RUNS, never automatic, and the only code allowed to delete a roster row.
    (The legacy auto-reap that rode roster_report deleted any stale row on
    presence ALONE — an inactive-but-fully-persisted seat lost its row to a
    3-second web poll, bypassing every transcript/process guard and the
    dry-run gate. Retired, not fenced: a report is a read.) Targets JUNK
    rows (the /tmp throwaway class). REFUSAL IS THE DEFAULT — a row is kept
    on ANY live evidence (_gc_keep_reason, the one probe):
      * a presence beat within REAP_S (.seen mtime / roster last_seen),
      * a transcript for ANY remembered session, anywhere the session
        census covers (incl. helm's own seat homes),
      * a live same-uid process naming ANY remembered session id or
        carrying the seat's HELM_CHAT_NAME,
      * probe trouble of any kind (fail-closed).
    Returns (rows, pruned): rows = [{seat, verdict: keep|prune, why}] for the
    whole roster; dry-run (apply=False) prunes NOTHING. apply=True deletes a
    scan-flagged row only after the FULL evidence probe re-runs fresh under
    the roster lock (TOCTOU: a transcript flushing or a presence beat
    landing between scan and apply must win), then unlinks its derived seat
    state (_unlink_seat_state — cursors, .seen, latches, the RAM DM lane)
    UNDER THE SAME LOCK: row delete + state unlink are one atomic critical
    section, so a rejoin can only land before (and be re-probed as keep
    evidence) or after (and keep its fresh state) — never in between."""
    now = time.time() if now is None else now
    rows = []
    for s, row in sorted(roster().items()):
        sids = [x for x in [row.get("session")]
                + list(row.get("sessions") or []) if x]
        try:
            why = _gc_keep_reason(s, row, roots, proc_dir, now)
        except Exception as e:                # fail-closed, loudly
            why = "keep-evidence probe failed (%s)" % e
        rows.append({
            "seat": s, "verdict": "keep" if why else "prune",
            "why": why or (
                "no transcript for %d remembered session%s, no live "
                "process, no fresh presence"
                % (len(sids), "s"[:len(sids) != 1]) if sids else
                "no remembered sessions, no live process, no fresh presence")})
    pruned = []
    if apply:
        victims = {r["seat"] for r in rows if r["verdict"] == "prune"}
        if victims:
            with _flocked(roster_path() + ".lock"):
                r = roster_for_write()
                for s in list(r):
                    if s not in victims:
                        continue
                    try:      # the SAME full probe, fresh, under the lock
                        keep = _gc_keep_reason(s, r[s], roots, proc_dir,
                                               time.time())
                    except Exception:         # fail closed under the lock too
                        keep = "probe trouble"
                    if keep:
                        continue              # evidence landed since the scan
                    del r[s]
                    pruned.append(s)
                if pruned:
                    pk.write_json(roster_path(), r)
                # unlink INSIDE the same lock: row delete + state
                # unlink are ONE critical section. Unlinking after release
                # left a gap where a SessionStart rejoin recreated the row
                # plus fresh .seen/cursors/DM lane — and this old invocation
                # then destroyed the NEW seat's state (live seat reading as
                # absent with its queued DMs gone). _unlink_seat_state takes
                # no locks of its own (plain os.remove), so no inversion.
                for s in pruned:
                    _unlink_seat_state(s)
    return rows, pruned
