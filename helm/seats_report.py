#!/usr/bin/env python3
"""helm seats — the roster report: what an operator SEES, and the GC beside it.

THIS IS THE ONE SEATS SURFACE A HUMAN READS DIRECTLY — the CLI table, the
/api/chat/roster payload, and the seats panel all render from here. That is
why every seat name leaving this module goes through _seat_label rather than
raw: this is the last place a roster key can turn into display text, and the
launder tripwire exists because it did.

PRESENCE IS A JUDGEMENT WITH THREE ANSWERS, not a boolean. Fresh, quiet, and
unverified are different claims about different evidence, and collapsing
unverified into absent is what makes an operator relaunch a seat that was
merely unproven. The dots are a glance; the row behind them has to survive
being wrong.

THE GC LIVES HERE ON PURPOSE. Deciding a row is reapable needs exactly the
evidence the report already gathered — presence, transcript existence, live
process, host plausibility — and a second gatherer would start at zero on
every edge this one already paid for. _gc_keep_reason states WHY a row
survives rather than returning a bare boolean, because "kept" with no reason
is indistinguishable from "the check did not run".
"""

import glob
import json
import os
import re
import time

from . import chat, home, pk, vcs
from .seats_common import (FRESH_S, MAX_BYTES, PREVIEW_CHARS, QUIET_S,
                           SEAT_BYTES, STATUS_BYTES,
                           UNVERIFIED, _clip, _flocked, _scrub, _seat_key,
                           _seat_label, roster, roster_path)
from .seats_roster import _key_bounded, last_seen, touch_seen
from .seats_stop_signals import _pending_all
from .seats_claims import claims_list

def presence_of(ls):
    if not ls:
        return "absent"
    age = time.time() - ls
    return "fresh" if age < FRESH_S else "quiet" if age < QUIET_S else "absent"
def session_owners(r=None):
    """sid -> sorted seats that remember it. The roster's identity index; a
    healthy roster maps every sid to exactly ONE seat (write_roster enforces
    it going forward — _evict_session)."""
    owners = {}
    for seat, row in (roster() if r is None else r).items():
        if not isinstance(row, dict):
            continue
        for sid in [row.get("session")] + list(row.get("sessions") or []):
            if isinstance(sid, str) and sid:
                owners.setdefault(sid, set()).add(seat)
    return {k: sorted(v) for k, v in owners.items()}
def unverified_seats(r=None):
    """{seat: (sid, [the other claimants])} for every row whose presence is
    NOT ATTRIBUTABLE — a session id it remembers is ALSO remembered by another
    row, so ANY beat on either row could be either process. This is the
    owner-visible half of the 2026-07-24 contamination fix: while two rows
    share an id, every 🟢 painted off `.seen` is a GUESS, and helm must say
    'unverified' rather than guess (the roster showed console-design fresh
    every 2s while another process wore its name). Pure roster arithmetic — no
    I/O past the one roster read the callers already did, so it rides the ~2s
    presence poll. Fail-open: a junk roster reads {}."""
    try:
        r = roster() if r is None else r
        out = {}
        for sid, owners in session_owners(r).items():
            if len(owners) < 2:
                continue
            for s in owners:
                out.setdefault(s, (sid, [o for o in owners if o != s]))
        return out
    except Exception:
        return {}
def presence_with_identity(ls, conflict=None):
    """presence_of, then the HONEST override: a row whose beat cannot be
    attributed to its own process never reads fresh/quiet. An ABSENT row stays
    absent — no beat at all is not an identity question, and unhiding hundreds
    of dead rows would bury the one that matters."""
    p = presence_of(ls)
    return UNVERIFIED if conflict and p != "absent" else p
def identity_warning(conflict):
    """The one owner-legible sentence for an unverified row. `conflict` is
    unverified_seats()'s (sid, others) pair."""
    sid, others = conflict
    return ("presence UNVERIFIED — session %.8s is also on %s, so this row's "
            "beat may be another process's" % (sid, ", ".join(others)))
# the ICQ-style glance: one dot + one line per seat, on every surface (the
# web presence bar, `helm chat seats`, the roster payload) — same truth
PRESENCE_DOTS = {"fresh": "\U0001f7e2",    # 🟢 active at a tool boundary
                 "quiet": "\U0001f7e1",    # 🟡 seated, idle a while
                 UNVERIFIED: "\U0001f7e0",  # 🟠 a beat we cannot attribute
                 "absent": "⚫"}       # ⚫ gone (no recent beat)
def presence_dot(p):
    return PRESENCE_DOTS.get(p, PRESENCE_DOTS["absent"])
def set_status(seat, text, by=None):
    """(ok, message). The seat's explicit one-line status ('what am I on') —
    `helm chat status <line>` / `--clear`. Rides THE roster writer's flock
    (a sibling of write_roster, mutating only the status fields — never a
    second writer path; the homing lane unified writers for a reason).
    Scrubbed + byte-clipped like every roster-borne label. Cross-seat writes
    stay allowed (a coordinator annotating a wedged seat is the point), but
    a writer that isn't the target is RECORDED as status_by — the same
    attribution parity posts have; a self-set carries no by field. The
    presence beat lands on the WRITER (the seat evidently alive is the one
    announcing, not a wedged target being annotated)."""
    if not seat:
        return False, "no seat to set a status on (join first, or --seat S)"
    line = _clip(_scrub(str(text or "")).strip(), STATUS_BYTES) or None
    by = _clip(_scrub(str(by or "")).strip(), 40) or None
    # The roster status line is the OTHER free-text body under `helm chat` —
    # 160 bytes is ample room for a 40-hex tip, it renders on every roster and
    # console surface, and it does NOT pass through chat.post, so the funnel
    # guard cannot see it. Scanned AFTER scrub+clip, i.e. the exact string that
    # will be stored and read: warning about a token the clip is about to cut
    # off would be a warning about text nobody will ever see. Warn only, before
    # the flock — a guard has no business holding the roster's lock.
    if line:
        from . import shaguard
        shaguard.warn(line)
    chat._ensure_dir()
    with _flocked(roster_path() + ".lock"):
        r = roster()
        row = r.get(seat)
        if row is None:
            return False, ("no roster row for %r — sessions join on start "
                           "(helm hooks install wires it); `helm chat status "
                           "--seat <live-seat>` targets an existing one" % seat)
        if line:
            row["status"] = line
            row["status_ts"] = time.time()
            if by and by != seat:
                row["status_by"] = by
            else:
                row.pop("status_by", None)
        else:
            row.pop("status", None)
            row.pop("status_ts", None)
            row.pop("status_by", None)
        r[seat] = row
        pk.write_json(roster_path(), r)
    touch_seen(by or seat)
    lbl = _seat_label(seat)   # raw key drove the write; echoed label laundered
    return True, ("%s ▸ %s" % (lbl, line) if line
                  else "%s status cleared" % lbl)
def _fmt_left(sec):
    sec = max(0, int(sec or 0))
    if sec >= 3600:
        return "%dh%02dm" % (sec // 3600, sec % 3600 // 60)
    return "%dm" % (sec // 60) if sec >= 60 else "<1m"
_WORKTREE_RES = re.compile(r"^worktree:([^:]+):(.+)$")
STATUS_FRESH_S = 4 * 3600   # how long an explicit status outranks LIVE truth:
STATUS_SKEW_S = 300   # clock-skew allowance on status_ts: a ts slightly in
def _status_age(row):
    """Seconds since the explicit status was set, or None (no status, or a
    planted row without a sane status_ts — missing, non-numeric, or dated
    beyond STATUS_SKEW_S into the future)."""
    ts = row.get("status_ts")
    if row.get("status") and isinstance(ts, (int, float)):
        d = time.time() - ts
        if d >= -STATUS_SKEW_S:
            return max(0, int(d))
    return None
def _status_by(row):
    """The recorded cross-seat writer for '(by X)', scrubbed reader-side."""
    b = row.get("status_by")
    return (_clip(_scrub(str(b)).strip(), 40) or None) if b else None
def _fmt_age(sec):
    sec = max(0, int(sec or 0))
    if sec >= 86400:
        return "%dd" % (sec // 86400)
    if sec >= 3600:
        return "%dh" % (sec // 3600)
    return "%dm" % (sec // 60) if sec >= 60 else "<1m"
def status_line(row, claim=None):
    """(line, source) — the ONE status line every surface shows, composed
    from what already exists. Precedence: a FRESH explicit status (the seat
    said so, within STATUS_FRESH_S) > a live claim (the lease says what it
    holds) > a stale explicit status > the home room (where it lives).
    source ∈ status|claim|home names the winning tier. Reader-side law:
    WHICHEVER tier wins, the line leaves here scrubbed (Cc/Cf incl. bidi,
    Zl/Zp) + clipped — a planted claim resource or roster field must not
    reshape a terminal or reorder the seats table. Never raises: a junk row
    reads '?' (one corrupt row must not blank the whole fleet bar)."""
    try:
        s = str(row.get("status") or "").strip()
        age = _status_age(row)
        fresh = age is not None and age <= STATUS_FRESH_S
        if s and (fresh or not claim):
            line, source = s, "status"
        elif claim:
            left = _fmt_left(claim.get("remaining"))
            m = _WORKTREE_RES.match(str(claim.get("resource") or ""))
            # A DEAD HOLDER IS NOT "WORKING". This is the line that reaches the
            # widest audience — bare `helm chat status`, the roster/presence
            # JSON, the web doing-cells and dashboard tooltips — and it was
            # reporting a dead seat's lock as work in progress, which is the
            # exact condition this lane exists to end. @codex-2 found it on
            # review after I fixed the two NARROWER surfaces and declared the
            # class closed; the verb `stale` is on the claim, so every renderer
            # that reads a claim has to read it.
            state = claim.get("liveness")
            verb = ("STALE hold on" if state == "stale"
                    else "UNVERIFIED hold on" if state == "unknown"
                    else "working")
            line, source = (("%s lane/%s (%s), %s left"
                             % (verb, m.group(2), m.group(1), left)) if m else
                            "%s %s, %s left"
                            % ("STALE hold on" if state == "stale"
                               else "UNVERIFIED hold on" if state == "unknown"
                               else "holds", claim.get("resource"), left)), "claim"
        elif row.get("home_room"):
            line, source = "in #%s" % row["home_room"], "home"
        else:
            line, source = ("in %s" % row["project"]
                            if row.get("project") else ""), "home"
        return _clip(_scrub(str(line)).strip(), STATUS_BYTES), source
    except Exception:
        return "?", "home"
def _claims_by_holder(cl=None):
    """holder -> its longest-lived live claim (the most work-shaped one)."""
    by = {}
    for c in (claims_list() if cl is None else cl):
        h = c.get("holder")
        if h and (h not in by
                  or (c.get("remaining") or 0) > (by[h].get("remaining") or 0)):
            by[h] = c
    return by
def _is_ephemeral_sa(name, home_room, cwd):
    """An EPHEMERAL review-subagent: auto-named agent-<hex>, no home room, a
    /tmp cwd — a transient fan-out SA (a review/scan SA), not a conversational
    seat. The ONE criterion every surface that hides them shares (the web
    'message a seat' picker AND the fleet-presence 'online' list). Fail-safe:
    any surprise reads False, so a real seat is never mis-hidden."""
    try:
        n = str(name or "")
        rest = n[6:] if n.startswith("agent-") else ""
        auto = bool(rest) and all(c in "0123456789abcdef" for c in rest)
        return auto and not home_room and "/tmp" in str(cwd or "")
    except Exception:
        return False
def presence_report():
    """The fleet-wide glance bar: one LIGHT row per roster seat — presence
    dot + the one status line — with zero cursor scans (roster_report walks
    pending; this must stay cheap enough to ride every ~2s web poll). Each row
    carries `ephemeral` so the 'online' list can drop done review-SAs.
    [{seat, presence, dot, last_seen, status, line, source, ephemeral}],
    fresh first."""
    try:
        by = _claims_by_holder()
    except Exception:
        by = {}
    rank = {"fresh": 0, UNVERIFIED: 1, "quiet": 2, "absent": 3}
    out = []
    r = roster()
    unver = unverified_seats(r)
    for seat, row in sorted(r.items()):
        try:
            ls = last_seen(seat, row)
            conflict = unver.get(seat)
            p = presence_with_identity(ls, conflict)
            line, source = status_line(row, by.get(seat))
            # the fleet bar is a roster-consuming SURFACE too: every string it
            # ships (seat KEY, status, status_by, line, source) rides the SAME
            # publish boundary as roster_report — presence_report escaping this
            # choke point is exactly how the 5th ESC surface was born (r4). One
            # owner, not a scrub scattered per surface.
            out.append(_pub_row({
                        "seat": seat, "runtime": row.get("runtime"),
                        "presence": p, "dot": presence_dot(p),
                        "last_seen": ls, "status": row.get("status"),
                        "status_age": _status_age(row),
                        "status_by": _status_by(row),
                        "line": line, "source": source,
                        "unverified": (conflict[1] if conflict else None),
                        "unverified_session": (conflict[0] if conflict
                                               else None),
                        "warn": (identity_warning(conflict)
                                 if conflict else None),
                        "ephemeral": _is_ephemeral_sa(
                            seat, row.get("home_room"), row.get("cwd"))}))
        except Exception:   # per-row fail-open: one junk roster row renders
            out.append(_pub_row({    # '?', it never blanks the whole fleet bar
                "seat": seat, "runtime": None, "presence": "absent",
                "dot": presence_dot("absent"), "last_seen": None,
                "status": None, "status_age": None, "status_by": None,
                "line": "?", "source": "home",
                "unverified": None, "unverified_session": None,
                "warn": None}))
    out.sort(key=lambda s: (rank.get(s["presence"], 3), s["seat"]))
    return out
REAP_S = 3600   # presence window: a beat this recent is live evidence on its
def _unlink_seat_state(seat):
    """Remove every state file keyed on the seat (cursors + locks +
    per-session variants, .seen, stop latches) — the orphan tail a pruned
    row would otherwise leave in the room dir forever. Fail-open per file."""
    key = _seat_key(seat)
    d = chat.chat_dir()
    try:  # the private DM lane goes with the seat (RAM etiquette)
        os.remove(chat.room_path(chat.DM_PREFIX + key))
    except OSError:
        pass
    try:
        names = os.listdir(d)
    except OSError:
        return
    for n in names:
        if _key_bounded(n, key):
            try:
                os.remove(os.path.join(d, n))
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
    inactive-but-fully-persisted proxy seat probed as junk (codex-2's live
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
        # A ZOMBIE IS PROVEN NOT-LIVE. A defunct process executes nothing and
        # holds no environment; its cmdline reads empty and its environ is
        # unreadable, which looks identical to "cannot examine" and used to
        # veto the scan. Measured 2026-07-29: a defunct zypak-sandbox was the
        # blind spot keeping codex-2 uncollectable once systemd stopped being
        # one. Reaped state is evidence of absence, exactly like ENOENT.
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
            # A SINGLE UNREADABLE PROCESS USED TO ABORT THE WHOLE SCAN, and
            # `systemd --user` runs as us on every host with an environ we can
            # never read. So this returned "fail closed" every time it was
            # reached — and it is only reached for a seat with NO presence beat
            # and NO transcript, i.e. exactly the dead seats gc exists to
            # collect. Measured 2026-07-29: codex-2 was KEPT with reason
            # "process 3915 environ unreadable (PermissionError)", and pid 3915
            # is systemd. A guard that can only ever say KEEP is not a guard.
            #
            # Blindness is now per-PID and narrowed by what we CAN read. If the
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
                r = roster()
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
                # unlink INSIDE the same lock (codex-2): row delete + state
                # unlink are ONE critical section. Unlinking after release
                # left a gap where a SessionStart rejoin recreated the row
                # plus fresh .seen/cursors/DM lane — and this old invocation
                # then destroyed the NEW seat's state (live seat reading as
                # absent with its queued DMs gone). _unlink_seat_state takes
                # no locks of its own (plain os.remove), so no inversion.
                for s in pruned:
                    _unlink_seat_state(s)
    return rows, pruned
# per-field byte caps for a roster row's DISPLAY strings; unlisted string
# fields ride the default. seat + session are here too so NO roster-borne
# string — the seat KEY included — reaches an operator terminal unlaundered.
_ROW_CAPS = {"seat": SEAT_BYTES, "session": MAX_BYTES, "project": 80,
             "cwd": 160, "home_room": 40, "home_room_source": 40,
             "status": STATUS_BYTES, "status_by": 40, "line": STATUS_BYTES,
             "source": 40, "preview": PREVIEW_CHARS, "active": STATUS_BYTES,
             "agent_harness": 64, "family": 64, "backend": 64,
             # honest-presence fields: `unverified` is a LIST of seat KEYS (so
             # it takes the seat cap, per element) and `warn` interpolates one
             # of those keys into a sentence — both must clear the same launder
             # bar as every other roster-borne string
             "unverified": SEAT_BYTES, "unverified_session": MAX_BYTES,
             "warn": STATUS_BYTES}
def _pub_row(d):
    """The ONE publish boundary for a roster row: scrub+clip EVERY string
    field (Cc/Cf incl. bidi, Zl/Zp) at its per-field cap, so no roster-borne
    string — seat KEY, project, cwd, status, status_by, line, source,
    preview, and any FUTURE string field — can reshape an operator terminal
    or reorder the fleet table. RECURSES into nested dicts and lists so a
    nested cell (the todo mirror's `active` text, or any future nested
    surface) is laundered by the same enumeration — not a hand-maintained
    special case that the next nested field would silently escape. Non-string
    values (last_seen, pending, dot, status_age) and falsy strings pass
    through. The stored roster keeps its raw keys (rename/claim match the dict
    itself); only this report copy is laundered — one owner, not a scrub
    scattered across every print site."""
    for k, v in list(d.items()):
        if isinstance(v, str) and v:
            d[k] = _clip(_scrub(v).strip(), _ROW_CAPS.get(k, 80))
        elif isinstance(v, dict):
            _pub_row(v)
        elif isinstance(v, list):
            d[k] = [_pub_row(x) if isinstance(x, dict)
                    else _clip(_scrub(x).strip(), _ROW_CAPS.get(k, 80))
                    if isinstance(x, str) and x else x
                    for x in v]
    return d
def runtime_label(row):
    """Compact family + harness/backend label; empty for legacy rows."""
    runtime = row.get("runtime") if isinstance(row, dict) else None
    if not isinstance(runtime, dict):
        return ""
    family = str(runtime.get("family") or "")
    harness = str(runtime.get("agent_harness") or "")
    backend = str(runtime.get("backend") or "")
    route = "/".join(x for x in (harness, backend) if x)
    return " · ".join(x for x in (family, route) if x)
def roster_report(room="main"):
    """{"seats": [...], "claims": [...]} — fail-open by caller. Pending is
    computed from each seat's cursor WITHOUT moving it. A report is a READ:
    it deletes NOTHING. (The legacy auto-reap that rode this verb was a
    second cleanup owner, dropping stale rows on presence alone — a
    persisted seat vanished on a poll while gc's evidence probe would have
    kept it. Cleanup has ONE owner now: gc_roster, a verb someone runs.
    Absent rows merely hide behind --all in the surfaces.)"""
    seats = []
    try:
        cl = claims_list()
    except Exception:
        cl = []
    by_holder = _claims_by_holder(cl)
    r = roster()
    unver = unverified_seats(r)
    for seat, row in sorted(r.items()):
        try:
            # pending is the MULTI-ROOM truth (the owner's panel must show a
            # helm-dogfood mention, not just main), read off the row's newest
            # session cursor (hook joins are session-keyed) with the
            # seat-level fallback — cursors never move here.
            hits = _pending_all(
                room, seat, session=row.get("session"), scan_lane="report")
            pending, preview = len(hits), None
            if hits:
                preview = _scrub(hits[-1][1].get("text") or "")[:PREVIEW_CHARS]
            ls = last_seen(seat, row)
            # the seat's CURRENT task, pulled (never pushed) off the todo
            # mirror — what turns "who is here" into "who is working on what".
            try:
                from . import todos as _todos
                todo = _todos.seat_digest(row)
            except Exception:
                todo = None              # fail-open: a roster read never 500s
            line, source = status_line(row, by_holder.get(seat))
            conflict = unver.get(seat)
            p = presence_with_identity(ls, conflict)
            seats.append(_pub_row({
                          "seat": seat, "session": row.get("session"),
                          "runtime": row.get("runtime"),
                          "runtime_verified": row.get("runtime_verified") is True,
                          "project": row.get("project"),
                          "cwd": row.get("cwd"),
                          "home_room": row.get("home_room"),
                          "home_room_source": row.get("home_room_source"),
                          "last_seen": ls, "presence": p,
                          "dot": presence_dot(p), "status": row.get("status"),
                          "status_age": _status_age(row),
                          "status_by": _status_by(row),
                          "line": line, "source": source,
                          "unverified": (conflict[1] if conflict else None),
                          "unverified_session": (conflict[0] if conflict
                                                 else None),
                          "warn": (identity_warning(conflict)
                                   if conflict else None),
                          "pending": pending, "preview": preview,
                          "todo": todo}))
        except Exception:   # per-row fail-open (the same law as the bar): a
            seats.append(_pub_row({  # junk row reads '?', never kills the report
                "seat": seat, "session": None, "runtime": None,
                "runtime_verified": False,
                "project": None, "cwd": None,
                "home_room": None, "home_room_source": None,
                "last_seen": None, "presence": "absent",
                "dot": presence_dot("absent"), "status": None,
                "status_age": None, "status_by": None,
                "line": "?", "source": "home",
                "unverified": None, "unverified_session": None, "warn": None,
                "pending": 0, "preview": None, "todo": None}))
    return {"room": room, "seats": seats, "claims": cl}
