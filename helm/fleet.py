#!/usr/bin/env python3
"""helm fleet — the ONE composition-truth table.

Born 2026-07-22, the morning the integrator hand-rolled the same /proc census
five times, each slightly differently, and every fleet mistake that day was a
stale-mental-model error: text injected into the wrong pane on a remembered
title, a seat declared running on a request-sent, an identity assembled from
three eras of belief. The design answer is not care — it is that COMPOSITION
QUESTIONS GET ANSWERED BY RUNNING THIS VERB, never from memory, and answers
given to anyone (owner included) quote its output.

ONE TRUTH OWNER: this verb never re-derives what another module already
proves. SID truth is helm/session.py's WHOLE census (_proc_claude_census:
the procStart-bound pid record, fail-closed --resume argv, who attribution,
cwd candidate set, and the final generation recheck) — fleet consumes those
rows verbatim and only composes them with its own display probes. The census
also exports each row's bracketed cwd, canonical trusted config root,
bracket generation (start) and bracketed WHOLE environ, so fleet never
re-derives a config home and never re-opens a proc environ file: seat, deck
and stamp facts come from the same coherent read the census proved, never
from a later moment a reused pid could answer. Consumed WHOLE means the
census is also the ONLY row source: no second /proc comm scan may resurrect
a pid the census rejected (its generation recheck failed — no coherent facts
exist for it), and a census cwd=None is never patched with a later
unbracketed /proc/<pid>/cwd read, because that would compose two process
generations into one row and could join a pane across them. And it is
consumed WITH its completeness channel: a failed /proc enumeration surfaces
as census_failed — the verb prints estate-UNKNOWN and exits 1, never
certifying an empty estate it never read — and a failed who scan marks every
sub-declared/resume row sid-UNKNOWN instead of letting the vanished rung
read as a proven blank. The same discipline holds PER PID below those
estate-wide bits: a mandatory probe that failed AFTER comm proved claude
arrives as a probe_failed UNKNOWN row (rendered, counted, exit 1 — never
dropped as proven absence), and one that failed BEFORE comm could prove or
refute claude arrives as census_partial — the estate total prints as a
floor ("at least N, CENSUS PARTIAL") and the verb exits 1. EVERY estate-wide
failed probe — who scan, daemon scan, terminal list — travels the same way
as census_failed/census_partial: a named completeness bit in the --json
envelope and exit 1, so a machine consumer keying on rc or the estate bits
can never read PASS while that truth went unprobed.

The one display probe that DOES re-read /proc after the census — the
daemon/host ppid walk — is only composed in after a FINAL generation
recheck (_generation_intact: live starttime == the row's bracketed start),
run after all display probes: a pid reused between census and walk must
never combine the old row's sid/home/cwd with the new process's ancestry,
so a failed recheck kills daemon/pane to UNKNOWN.

One row per census (same-uid live claude) process:
  pid, seat (HELM_CHAT_NAME or roster reverse-lookup), sid (census identity
  ladder; a sid proven live in MULTIPLE pids is flagged DOUBLE-OPEN — that is
  what a second holder proves, never that this process is a fork), home
  (session's canonical config root; '?' when untrusted/unproven), daemon
  (ppid-walk to an orca daemon whose incarnation is re-proven at match time),
  pane (orca terminal handle when the join is unambiguous; '?' otherwise —
  never guessed), stamps (child-session trio count), deck (a physics-deck tag
  from HELM_SKILL_DECK — host-authored deck_labels tag site-specific decks,
  else 'helm' when set, '-' when unset).

Every column comes from a live probe; nothing is cached. A FAILED probe is
UNKNOWN, never an absence fact (premise failed-probe-not-absence): HEADLESS is
a PROVEN verdict (a fully-parsed ppid walk that reached init, touching no pid
the daemon scan left unproven); an unparsable hop, exhausted walk, stale or
unprovable daemon evidence, failed daemon scan, unreadable environ/cwd,
untrusted config root, failed roster, failed/misshapen terminal list, failed
who scan, or failed final generation recheck
renders host/columns as '?' and marks the row UNKNOWN. The verb is read-only
and safe to run at any moment.
"""
import json
import os
import re
import socket
import subprocess
import time
import uuid

from . import session
from . import pk


def _deck_label(deck):
    """A short physics-deck tag for a seat's HELM_SKILL_DECK value. EMPTY public
    default — the authored `host.deck_labels` ({substring: tag}) supplies any
    site-specific deck names; first substring match wins, else 'helm' when a deck
    is set, '-' when unset. No site-specific deck name ships in code. This is a
    cosmetic listing column, so an UNREADABLE authored layer degrades quietly to
    the default rather than refusing the fleet listing — unlike the
    data-affecting host-config readers (skillsync/envtidy/capability), which
    refuse, because a wrong fleet label costs nothing a wrong sync/surface does."""
    if not deck:
        return "-"
    from . import registry
    try:
        labels = registry.authored_host().get("deck_labels", {})
    except registry.AuthoredUnreadable:
        labels = {}
    if isinstance(labels, dict):
        for sub, tag in labels.items():
            if sub and sub in deck:
                return str(tag)
    return "helm"

_DAEMON_ENTRY = "daemon-entry.js"
# the runtimes that actually execute the orca daemon script (orca-ide is the
# packaged electron binary; node/electron cover dev shapes)
_ORCA_RUNTIMES = {"orca-ide", "orca", "electron", "node"}
_SID_SRC = {"declared": "record", "resume": "argv", "who": "who"}
# census reasons that mean the record/environ PROBE failed (vs an affirmative
# fact like record-missing/record-stale): an unresolved sid under one of these
# is UNKNOWN, not a proven blank
_FAILED_PROBE_REASONS = {"environ-unreadable", "record-unreadable",
                         "record-replaced", "probe-failed"}


def _rownames(rows, cap=5):
    """Seat labels for a handful of rows, named the way the row printer names
    them. A qualifier that gives only a COUNT cannot be checked by a reader:
    they have to be able to go and look at the processes it excluded."""
    seen = [("UNRENDERABLE" if r.get("seat_unrenderable")
             else (r["seat"] or ("?" if r["unknown"] else "(no seat)")))
            for r in rows]
    return ", ".join(seen[:cap]) + ("" if len(seen) <= cap
                                    else " and %d more" % (len(seen) - cap))


def _throttle(pid, start, root=None, proc=None):
    """(counts, blind, why, known) for one pid — ONE observation, structured.

    `root` AND `proc` are injectable, and it takes both to isolate an arm
    about this seam. A root alone leaves the fixture deriving the PATH and the
    GENERATION from the live host, so it still carries host premises — the
    counter availability stops deciding the answer and the cgroup layout does
    not. A process whose chain carries a counter at every level and one whose
    chain does not are both ordinary, and so are a process in the cgroup root
    and one several levels down; an arm that needs a determinate answer
    supplies the whole world it is asking about.

    A NAMED SEAM, like `_census`, and for the same reason: this reaches the
    real /sys/fs/cgroup, so an arm driving rows() with synthetic pids would
    otherwise consume whatever the HOST is doing — and a fake pid that
    collides with a live one reads that process's real pressure. Tests patch
    this, exactly as they patch the census.

    BOTH ANSWERS COME FROM ONE READ. Asking twice would double the I/O and,
    worse, let the two disagree: the generation can change between two reads,
    so a row could print a line derived from one process while deciding to
    print it from another.

    STRUCTURED, NOT A SENTENCE, because the caller must compare levels across
    rows: a shared ancestor's counter is identical on every process beneath
    it, and repeating it per row buries the one level that is about THIS
    process. A read that RAISES answers (None, None, why, False) rather than
    raising — fleet exists to answer, and a throttle read is not worth losing
    the census over — and never ({}, 0, ...): {} is what a process whose
    counters all read zero reports, so the raise would publish that clean
    tally in `--json`. It names the exception and leaves a breadcrumb.
    """
    try:
        from . import seatceiling
        where = {}
        if root is not None:
            where["root"] = root
        if proc is not None:
            where["proc"] = proc
        obs = seatceiling.observe(pid, start=start, **where)
        # KNOWNNESS IS A BOOLEAN THE CALLER COMPOSES, not a sentence it has
        # to remember to read. `acquisition_complete` is true when the bracket held
        # at BOTH ends and every level asked about answered; this file's own
        # contract two hundred lines down is that every failed probe gates the
        # exit code, and a reason nothing joins cannot do that. It is NOT a
        # claim the chain is coherent — endpoint agreement is not atomicity; read its docstring.
        return (seatceiling.counts(obs),
                len(seatceiling.unobtainable(obs)),
                obs.bracket.why,
                seatceiling.acquisition_complete(obs))
    except Exception as exc:                # noqa: BLE001
        from . import record
        record.swallow("fleet._throttle", exc)
        return (None, None,
                "the throttle read raised %s" % exc.__class__.__name__, False)


def _census():
    """({pid: row}, census_failed, who_failed, census_partial) straight from
    session._proc_claude_census() — the ONE sid truth owner (record/argv/who/
    cwd rungs, generation recheck, canonical config root, bracketed environ).
    Fleet never re-derives any of it. census_failed means the /proc
    enumeration itself failed: the empty table is a FAILED PROBE, not a
    proven-empty estate. who_failed means the who rung was never probed.
    census_partial means a mandatory per-pid probe failed before the pid's
    comm could prove or refute claude: the row count is a FLOOR ('at least
    N'), never a certified estate total."""
    c = session._proc_claude_census()
    return ({r["pid"]: r for r in c["rows"]}, c["listing_failed"],
            c["who_failed"], c["census_partial"])


def _generation_intact(pid, start):
    """The census generation still owns the pid NOW. Run AFTER the row's
    display probes: the census row was proven coherent at return time, but
    the host walk re-reads /proc later — if the pid was reused in between,
    those reads describe a DIFFERENT process and must not be composed onto
    the census facts. start=None never verifies (an unbracketed row cannot
    be re-proven)."""
    return start is not None and session._proc_start(pid) == start


def _cmdline_probe(pid):
    """('ok', argv) | ('gone', None) | ('failed', None)."""
    try:
        with open("/proc/%d/cmdline" % pid, "rb") as f:
            argv = f.read().decode("utf-8", "replace").split("\0")
        return "ok", argv
    except OSError as e:
        return ("gone" if session._gone(e) else "failed"), None


def _cmdline_argv(pid):
    """NUL-split argv, or None outside the daemon census status channel."""
    status, argv = _cmdline_probe(pid)
    return argv if status == "ok" else None


def _is_daemon_argv(argv):
    """The ACTUAL orca daemon argv shape, nothing looser: a known orca/JS
    runtime (argv[0] basename) whose SCRIPT argument — the first non-flag
    element after argv[0] — is daemon-entry.js, or the script executed
    directly. `cat /tmp/daemon-entry.js`, `python worker.py daemon-entry.js`,
    substring lookalikes, and flag-embedded paths are NOT daemons."""
    argv0 = os.path.basename(argv[0]) if argv and argv[0] else ""
    if argv0 == _DAEMON_ENTRY:
        return True
    if argv0 not in _ORCA_RUNTIMES:
        return False
    for a in argv[1:]:
        if not a or a.startswith("-"):
            continue
        return os.path.basename(a) == _DAEMON_ENTRY
    return False


def _daemon_pids():
    """({pid: starttime}, unproven_pids, scan_failed). Every daemon pid is
    BRACKETED with its starttime (session._proc_start, the canonical field-22
    reader) so a later membership hit can re-prove the same incarnation
    instead of trusting a bare number across PID reuse. scan_failed=True
    means the /proc listing itself failed — daemon truth is then UNKNOWN for
    every row. UNPROVEN pids are PARTIAL probe failures — an unreadable
    cmdline (cannot be ruled a daemon OR ruled out) or a daemon-shaped argv
    whose starttime read failed (a daemon that cannot be incarnation-proven).
    They must never silently vanish behind scan_failed=False: a ppid walk
    that reaches one answers UNKNOWN, never walks through it to init and
    claims a proven HEADLESS."""
    out, unproven = {}, set()
    try:
        names = os.listdir("/proc")
    except OSError:
        return {}, set(), True
    for name in names:
        if not name.isdigit():
            continue
        pid = int(name)
        status, argv = _cmdline_probe(pid)
        if status == "gone":
            continue
        if status == "failed":
            unproven.add(pid)
            continue
        if not _is_daemon_argv(argv):
            continue
        start = session._proc_start(pid)
        if start:
            out[pid] = start
        else:
            unproven.add(pid)
    return out, unproven, False


def _daemon_generations(daemons):
    """({generation: [pid, ...]}, unknown_count) — the PROTOCOL generation each
    live daemon is serving, re-proven against the incarnation it was scanned as.

    An orca upgrade starts a NEW daemon and does NOT reap the old one, so more
    than one generation alive at once is not cosmetic: the current UI cannot
    render sessions owned by another generation, and the agents inside them go
    INVISIBLE while still running. Measured 2026-07-27 on this fleet — four
    generations (v23/v24/v26/v28) spanning a week, 13 daemon startups against 5
    shutdowns, and the owner reasonably concluded an upgrade had killed his
    fleet when nothing had been killed at all.

    The count alone was already printed and read past, by two agents, on the
    morning it mattered. A number with no NORMAL beside it is a fact, not a
    finding, which is why the caller renders a verdict rather than a total.

    Fails CLOSED in both directions a probe can fail: a pid whose starttime no
    longer matches is a REUSED pid and is not the daemon we scanned, and an
    unreadable or unparseable cmdline is UNKNOWN. Neither collapses into a
    clean single-generation answer — "I could not look" must never render as
    "there is only one".
    """
    gens, unknown = {}, 0
    for pid, start in sorted(daemons.items()):
        if session._proc_start(pid) != start:
            unknown += 1
            continue
        status, argv = _cmdline_probe(pid)
        if status != "ok" or not argv:
            unknown += 1
            continue
        hit = re.search(r"daemon-(v\d+)\.sock", " ".join(argv))
        if not hit:
            unknown += 1
            continue
        gens.setdefault(hit.group(1), []).append(pid)
    return gens, unknown


def _generation_verdict(gens, unknown):
    """The one line that says what the daemon count MEANS, or '' when clean."""
    if unknown:
        return (" — %d daemon(s) UNPROVEN: generation unreadable, so a stale "
                "generation cannot be ruled out" % unknown)
    if len(gens) <= 1:
        return ""
    live = sorted(gens)
    return (" — %d PROTOCOL GENERATIONS ALIVE (%s): an orca upgrade starts a new "
            "daemon and does not reap the old one, so agents owned by a stale "
            "generation are INVISIBLE to the current UI, not dead. Roll-call "
            "them (helm chat) before concluding anything about liveness."
            % (len(live), ", ".join(live)))


def _stat_link(pid):
    """(starttime, ppid) from ONE validated stat read, or None.

    Both fields must travel together: a ppid without its process generation is
    not an ancestry identity and can splice a reused intermediate pid into an
    unrelated daemon chain.
    """
    try:
        with open("/proc/%d/stat" % pid, "rb") as f:
            raw = f.read()
    except OSError:
        return None
    _comm, sep, tail = raw.rpartition(b")")
    if not sep:
        return None
    fields = tail.split()
    # tail: state ppid ... starttime(field 22 => tail index 19)
    if (len(fields) <= 19 or not fields[1].isdigit()
            or not fields[19].isdigit()):
        return None
    return fields[19].decode("ascii"), int(fields[1])


def _stat_ppid(pid):
    link = _stat_link(pid)
    return link[1] if link else None


def _daemon_for(pid, start, daemons, unproven):
    """('daemon', pid) | ('headless', None) | ('unknown', None).

    Every hop is captured as (pid, starttime, ppid) and re-read before a
    verdict. Rechecking only the Claude pid misses an intermediate parent that
    exits and is numerically reused between hops; its replacement can otherwise
    splice an unrelated Orca daemon into the chain.
    """
    chain, cur, expected = [], pid, start
    for _ in range(12):
        link = _stat_link(cur)
        if link is None:
            return "unknown", None
        live_start, ppid = link
        if expected is not None and live_start != expected:
            return "unknown", None
        chain.append((cur, live_start, ppid))

        def intact():
            return all(_stat_link(p) == (s, parent)
                       for p, s, parent in chain)

        if ppid <= 1:
            return ("headless", None) if intact() else ("unknown", None)
        if ppid in daemons:
            argv = _cmdline_argv(ppid)
            if (argv is not None and _is_daemon_argv(argv)
                    and session._proc_start(ppid) == daemons[ppid]
                    and intact()):
                return "daemon", ppid
            return "unknown", None
        if ppid in unproven:
            return "unknown", None
        cur, expected = ppid, None
    return "unknown", None


def _daemon_user_data(pid):
    """The Orca user-data root named by this daemon's --socket argv."""
    argv = _cmdline_argv(pid)
    if not argv or "--socket" not in argv:
        return None
    try:
        socket = argv[argv.index("--socket") + 1]
    except IndexError:
        return None
    return os.path.dirname(os.path.dirname(os.path.realpath(socket)))


def _orca_cli(user_data):
    """Known CLI for one Orca user-data root; custom roots fail closed."""
    root = os.path.realpath(user_data or "")
    if root == os.path.realpath(os.path.expanduser("~/.config/orca")):
        return "orca"
    if root == os.path.realpath(os.path.expanduser("~/.config/orca-dev")):
        return "orca-dev"
    return None


def _orca_json(cli, *args):
    try:
        p = subprocess.run([cli] + list(args) + ["--json"],
                           capture_output=True, text=True, timeout=10)
        if p.returncode != 0:
            return None
        data = json.loads(p.stdout)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    return data if isinstance(data, dict) and data.get("ok") is True else None


def _orca_runtime_call(user_data, runtime_id, method, params, timeout=5):
    """Read-only authenticated Orca runtime RPC, or None on any uncertainty."""
    try:
        with pk.open_regular(os.path.join(user_data, "orca-runtime.json")) as f:
            meta = json.load(f)
        if meta.get("runtimeId") != runtime_id:
            return None
        transports = meta.get("transports")
        if not isinstance(transports, list):
            transports = [meta.get("transport")]
        transport = next((t for t in transports if isinstance(t, dict)
                          and t.get("kind") == "unix" and t.get("endpoint")),
                         None)
        token = meta.get("authToken")
        if transport is None or not token or not hasattr(socket, "AF_UNIX"):
            return None
        request_id = str(uuid.uuid4())
        request = {"id": request_id, "authToken": token,
                   "method": method, "params": params}
        # ONE WALL-CLOCK DEADLINE, sibling of helm/harness.py's. settimeout()
        # bounds a SINGLE operation, so connect, sendall and each recv formerly
        # drew a FULL budget apiece; the reply loop also `continue`s on every
        # _keepalive, so a trickling daemon reset the clock each pass. Expiry
        # RETURNS None rather than raising, because this function's whole
        # contract is "None on any uncertainty" and a deadline expiry is
        # exactly that — the harness sibling raises because ITS contract says
        # so. Same defect, two honest shapes.
        deadline = time.monotonic() + timeout

        def _left():
            return deadline - time.monotonic()

        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            if _left() <= 0:
                return None
            conn.settimeout(_left())
            conn.connect(transport["endpoint"])
            if _left() <= 0:
                return None
            conn.settimeout(_left())
            conn.sendall((json.dumps(request, separators=(",", ":")) +
                          "\n").encode("utf-8"))
            buf = b""
            while len(buf) <= 1024 * 1024:
                if _left() <= 0:
                    return None
                conn.settimeout(_left())
                chunk = conn.recv(65536)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    if _left() <= 0:        # buffered frames bounded too: a
                        return None         # peer can pack many keepalives
                    raw, buf = buf.split(b"\n", 1)   # into ONE recv
                    if not raw.strip():
                        continue
                    reply = json.loads(raw.decode("utf-8"))
                    if reply.get("_keepalive"):
                        continue
                    if (reply.get("id") != request_id
                            or (reply.get("_meta") or {}).get("runtimeId")
                            != runtime_id or reply.get("ok") is not True):
                        return None
                    result = reply.get("result")
                    return result if isinstance(result, dict) else None
    except (OSError, ValueError, TypeError, socket.timeout):
        return None
    return None


def _orca_terminals(daemon, daemon_start, env):
    """(terminals, failed), bound to the process's daemon and Orca runtime.

    The bracketed process environment names its user-data root. The daemon argv
    must name the same root; ``orca status`` must name the app process that owns
    this exact daemon generation; and terminal-list's runtimeId must equal the
    status runtimeId. A bare cwd match crosses runtimes and is never identity.
    """
    user_data = (env or {}).get("ORCA_USER_DATA_PATH")
    cli = _orca_cli(user_data)
    if (not cli or not user_data
            or _daemon_user_data(daemon) != os.path.realpath(user_data)):
        return [], True
    status = _orca_json(cli, "status")
    result = status.get("result") if status else None
    app = result.get("app") if isinstance(result, dict) else None
    runtime = result.get("runtime") if isinstance(result, dict) else None
    app_pid = app.get("pid") if isinstance(app, dict) else None
    runtime_id = runtime.get("runtimeId") if isinstance(runtime, dict) else None
    if (not isinstance(app_pid, int) or not runtime_id
            or _stat_ppid(daemon) != app_pid
            or session._proc_start(daemon) != daemon_start):
        return [], True
    data = _orca_json(cli, "terminal", "list")
    meta = data.get("_meta") if data else None
    result = data.get("result") if data else None
    terms = result.get("terminals") if isinstance(result, dict) else None
    total = result.get("totalCount") if isinstance(result, dict) else None
    if (not isinstance(meta, dict) or meta.get("runtimeId") != runtime_id
            or not isinstance(terms, list)
            or not all(isinstance(t, dict) for t in terms)
            or result.get("truncated") is not False
            or not isinstance(total, int) or total != len(terms)
            or session._proc_start(daemon) != daemon_start):
        return [], True
    return [dict(t, _orca_cli=cli, _runtime_id=runtime_id)
            for t in terms], False


def _pane_for(env, terminals):
    """(handle, proven) through Orca's live pane-key resolver.

    Spawn-time handle/cwd/title are never authorization. The bracketed process
    contributes ORCA_PANE_KEY; the daemon-bound runtime remints it to a live
    handle+pty, and the complete terminal inventory must contain exactly that
    connected+writable handle+pty+worktree tuple.
    """
    env = env or {}
    key, user_data = env.get("ORCA_PANE_KEY"), env.get("ORCA_USER_DATA_PATH")
    healthy = [t for t in terminals if t.get("handle")
               and t.get("connected") is True and t.get("writable") is True]
    runtime_ids = {t.get("_runtime_id") for t in terminals
                   if t.get("_runtime_id")}
    if not key or not user_data or len(runtime_ids) != 1:
        return None, False
    runtime_id = next(iter(runtime_ids))
    result = _orca_runtime_call(
        user_data, runtime_id, "terminal.resolvePane", {"paneKey": key})
    resolved = result.get("terminal") if isinstance(result, dict) else None
    if not isinstance(resolved, dict):
        return None, False
    handle, pty = resolved.get("handle"), resolved.get("ptyId")
    if not handle or not pty:
        return None, False
    inherited = env.get("ORCA_TERMINAL_HANDLE")
    if inherited and inherited != handle and any(
            t.get("handle") == inherited for t in healthy):
        return None, False  # live conflicting handle, not an ordinary remint
    worktree = env.get("ORCA_WORKTREE_ID")
    hits = [t for t in healthy if t.get("handle") == handle
            and t.get("ptyId") == pty
            and (not worktree or t.get("worktreeId") == worktree)]
    return (handle, True) if len(hits) == 1 else (None, False)


def _roster():
    """(roster, failed) without seats.roster's coordination fail-open."""
    try:
        from . import seats
        return seats.roster_checked()
    except Exception:
        return {}, True


def _mark_unknown(row):
    """Mark a row UNKNOWN *and* record WHY, in one call.

    `unknown` has two kinds of cause — a refused identity and a failed probe —
    and the footer counts them independently. Every mutation below the row
    builder is a probe outcome, never an identity refusal, so both bits move
    together or the row lands in NEITHER bucket and simply stops being
    reported.

    THIS EXISTS AS A FUNCTION BECAUSE PATCHING THE SITES DID NOT HOLD. The
    cause bit was added at the builder and maintained at ONE of four
    post-builder mutations; I fixed the one a failing test pointed at and said
    out loud that I did not assume it was the only one. A review then found the
    other three — terminal-inventory failure, unproven pane, duplicate pane —
    each leaving rows invisible to both footers and a JSON body contradicting
    its own two-cause model. A rule that must be remembered at every call site
    is a rule that will be missed at the next one, so the pair is now
    unsplittable by construction.

    PROBE-ONLY IS DELIBERATE, NOT MERELY CURRENT — DO NOT ADD A BYPASS. This
    helper serves the POST-BUILDER mutations, and every one of them is a probe
    outcome; the other cause, `seat_unrenderable`, is set at the row BUILDER
    and never travels through here. An earlier cut of this docstring argued
    the opposite and grew a `probe=False` escape "so a closed class has a
    door". That escape emitted `unknown` with NEITHER cause bit — rc=1 and no
    owner-facing sentence, which is the exact NEITHER-BUCKET class this helper
    exists to close. It had no call site and could not have acquired a correct
    one: a non-probe post-builder cause has no bit, no producer, and no name
    to write down, so the door led nowhere nameable. (A cross-family read
    found it; a test of mine had pinned the uncategorized row as CORRECT.)

    A genuine second cause arrives as its own bit plus its own footer
    sentence, added here with the producer that emits it — never as a flag
    that suppresses the only cause this helper can state. Mislabelling is
    recoverable because it is counted; an uncategorized row is invisible.
    """
    row["unknown"] = True
    row["probe_unknown"] = True
    return True


def _display_seat(seat, seat_src):
    """(label, unrenderable) for the fleet table AND the --json body.

    When a seat identity is present in env or roster (seat_src in ('env',
    'env-alias', 'roster'))
    but scrubbing stripped all unprintable or hostile characters leaving an empty label,
    label is '?' and unrenderable is True so consumers and rendering logic don't
    conflate empty-scrubbed seats with absent ones ('(no seat)').

    A PARTIALLY scrubbed name reaches here too, and that is the case a review
    found: the FULLY-scrubbed name was handled and the
    partial one silently ALIASED A LEGITIMATE SEAT. `_seat_for` now refuses to
    hand a noncanonical identity down, so those arrive empty and take the arm
    above — see its docstring for the reproduction.
    """
    if seat_src in ("env", "env-alias", "roster") and not seat:
        return "?", True
    return seat, False


def _seat_for(sid, env, roster, roster_err):
    """(seat, source) for a censused pid — LAUNDERED at this one boundary.

    BOTH NAME SOURCES ARE ATTACKER-SHAPED AND NEITHER WAS SCRUBBED — but only
    ONE of them is reachable, and this docstring used to say otherwise. It
    claimed "the seat key is unvalidated at the join seam", I believed it, and
    I repeated it in a verdict before checking. MEASURED: `home.chat_name()`
    RAISES SeatNameError on a noncanonical HELM_CHAT_NAME, so nothing hostile
    reaches the ROSTER through join. What IS reachable is the other half —
    this function reads HELM_CHAT_NAME straight out of a foreign process's
    environ, bypassing that seam entirely. One module reading raw where every
    other consumer reads validated is the whole defect, and it is a sharper
    statement than "neither is validated". This function is where either
    becomes the
    row's `seat`, and `rows()` prints that into `%-18s` — the first, widest,
    most prominent column of the census the owner reads to decide what to
    kill, resume or trust. An ESC or bidi run there reshapes the operator's
    terminal at exactly the moment they are making a fleet decision.

    `helm/fleet.py` carried ZERO calls to _seat_label/_pub_row/_scrub before
    this. The display-launder tripwire could not see the gap because it
    matched the regex `\\broster\\(\\)` and fleet obtains the roster through
    `roster_checked()` — invisible to that pattern, so the module never
    reached the allowlist and its emission was never questioned.

    _seat_label is scrub+clip, NOT anonymisation: "a legit seat … is
    unchanged", so `codex-3` still prints `codex-3` and the census reads
    exactly as before. Only a hostile name changes, which is the point.

    LAUNDERING IS NOT ENOUGH FOR AN IDENTITY, AND THAT IS THIS FUNCTION'S
    SECOND LESSON (a reviewer's FIX on this lane's reviewed tip; the receipt id
    is in that verdict and in this commit's message, not inlined here — it
    addresses a RUN, and a reader who greps it in source finds no commit).
    Scrubbing made a hostile
    name SAFE TO PRINT; it did not make it TRUE. A name that scrubs to nothing
    was already handled — `_display_seat` renders UNRENDERABLE. A name that
    scrubs to SOMETHING was not, and it aliased whatever it landed on:

        _seat_label('alpha')            -> 'alpha'
        _seat_label('alpha\\u202e')      -> 'alpha'    <- RLO stripped
        _seat_label('alpha\\u200b')      -> 'alpha'    <- ZWSP stripped, and
                                                         nobody named this one
        collision: the last two are INDISTINGUISHABLE from the real seat,
        with seat_unrenderable=False and unknown=False, in the first and
        widest column of the census the owner reads to decide what to kill.

    So the rung is IDENTITY, not printability, and helm already owns that
    predicate: `seats._SEAT_TOKEN` is what an @mention may say. A raw name
    that does not fullmatch it is not a seat name, whatever it scrubs to, and
    is refused here rather than laundered into one. Refusing returns an EMPTY
    label so it takes `_display_seat`'s existing UNRENDERABLE arm — one
    "present but unprintable-as-identity" concept, not a second flag beside
    it. VERIFIED IN BOTH DIRECTIONS over the live population before landing:
    all 22 roster seats and all 7 live HELM_CHAT_NAME values are canonical
    (nothing legitimate changes), and every attack shape above is refused.
    """
    from . import seats
    # PRESENCE, NOT TRUTHINESS. `if name:` read an EXPLICIT empty identity —
    # `HELM_CHAT_NAME=`, which `session._full_environ` faithfully preserves as
    # "" — as ABSENT, so it fell through to the roster branch and the process
    # was handed whatever seat owned that session. Measured on the previous
    # tip: _seat_for('sid', {'HELM_CHAT_NAME': ''}, {'real': …}) returned
    # ('real', 'roster') with unrenderable=False — the alias this whole
    # function exists to stop, through the one door the rung did not cover
    # (exact-tip repro). An empty declaration is a PRESENT
    # noncanonical identity: it says "I am nobody", which is not the same as
    # saying nothing, and only the second may fall through.
    env = env or {}
    if "HELM_CHAT_NAME" in env:
        name = env["HELM_CHAT_NAME"]
        if not isinstance(name, str) or not seats._SEAT_TOKEN.fullmatch(name):
            return None, "env"          # present, but not an identity
        # A DECLARATION IS A LAUNCH-TIME RECORD, AND THIS IS THE WIDEST COLUMN
        # THE OWNER READS. `/proc/<pid>/environ` is written at exec and the
        # kernel never rewrites it, so a seat RENAMED while live keeps
        # declaring its old spelling for as long as it runs — and this column
        # printed that spelling as the seat's name, which is how task/2739 was
        # first read as a stale process needing a relaunch. Nothing about the
        # process was stale; the reading was.
        #
        # THE ROSTER IS ALREADY IN HAND AND ALREADY KNOWS. `helm chat seat rename`
        # records the old name on the new row and `seats_common.live_alias` is
        # the one resolver for it — the same seam `actors._resolve` uses to
        # admit a process whose own environ spells it the old way. Resolving
        # here is strictly NARROWER than printing the raw declaration: the
        # answer becomes a roster KEY, re-checked against `_SEAT_TOKEN` because
        # `write_roster` carries no token check of its own (the roster arm
        # below states that measurement).
        #
        # live_alias's own law keeps this from aliasing anything: an exact
        # roster key is never an alias, so a name that still names its own row
        # is unchanged, and a re-admitted old name resolves to itself. Only a
        # name that is NOT a key and IS a live alias moves, which is the rename
        # and nothing else. An UNREADABLE roster cannot resolve one and the
        # declaration stands as before — the row's `seat_src` says which seam
        # answered, so a reader can tell the two apart.
        # AND THE ALIAS IS BOUND TO THE SESSION IT WAS PROVEN ON, because the
        # paragraph above is TRUE OF THE ROSTER AND NOT OF THE PROCESS. A
        # reviewer's reuse chain is the counterexample and it is worth stating
        # exactly: rename A to B, admit a FRESH A, then rename that A to C.
        # `live_alias("A")` now answers C, correctly — A really is C's prior
        # name. But the ORIGINAL A process is still running, still declaring
        # HELM_CHAT_NAME=A at exec, and it belongs to B. Resolving its pid to C
        # LABELS AN OLD GENERATION WITH A NEWER SEAT'S NAME, in the widest
        # column of the census the owner reads to decide what to kill.
        #
        # The declaration alone cannot separate them — both processes spell
        # themselves A and environ is frozen, which is this whole lane's
        # premise turned against the cure. What separates them is the SESSION:
        # the alias answers a question about a NAME, and only the session says
        # WHICH PROCESS that name's row is currently about. So an alias moves a
        # pid only when this pid's session is one the target row actually
        # claims; otherwise the raw declaration stands as `env`, which is what
        # main does and is honest about what was measured.
        # SO THE SESSION PICKS THE ROW AND THE ALIAS ONLY CONFIRMS IT, which is
        # the opposite of the obvious order and the measurement is why. Asking
        # `live_alias(name, roster)` first and corroborating afterwards looks
        # equivalent and is not: MEASURED, with two rows claiming the same
        # prior name, that call answers WHICHEVER COMES FIRST IN ITERATION
        # ORDER — seat-b on this roster, and the reviewer who found the defect
        # saw seat-c on theirs. Neither is wrong; there is no "the" alias of an
        # ambiguous name, so a corroboration bolted on afterwards would reject
        # the LEGITIMATE renamed process whenever the arbitrary pick landed on
        # the other claimant. Resolving the session FIRST has no such tie:
        # `hits` is the roster's own answer to "whose process is this", and a
        # single-row `live_alias` then applies its whole law — an expired
        # window still refuses, an exact key is still never an alias — to the
        # one row the session proved.
        # TWO CALLS, AND THE FIRST ONE IS THE LAW. The whole-roster call is
        # what refuses: an exact roster key is never an alias, and an expired
        # window is not an identity. Narrowing to one row FIRST would defeat
        # exactly that — a single-row view cannot see that the declared name is
        # somebody else's LIVE key, so a re-admitted seat-a would be aliased
        # away. An arm on this file caught that and it was right to.
        # The second call only DISAMBIGUATES. Once the roster as a whole agrees
        # the name is a live alias, the session says which claimant this pid
        # belongs to, and the single-row call re-applies the same law to that
        # one row rather than trusting iteration order.
        if not roster_err and sid:
            from . import seats_common
            anywhere, _until = seats_common.live_alias(name, roster)
            if anywhere is not None:
                owners = [(seat, row) for seat, row in roster.items()
                          if row.get("session") == sid
                          or sid in (row.get("sessions") or [])]
                if len(owners) == 1:
                    seat, row = owners[0]
                    key, _u = seats_common.live_alias(name, {seat: row})
                    if key is not None and seats._SEAT_TOKEN.fullmatch(str(key)):
                        return seats._seat_label(str(key)), "env-alias"
        return seats._seat_label(name), "env"
    if roster_err:
        return None, "roster-error"
    if not sid:
        return None, None
    hits = [seat for seat, row in roster.items()
            if row.get("session") == sid
            or sid in (row.get("sessions") or [])]
    if len(hits) == 1:
        # DEFENCE IN DEPTH, AND SAYING SO IS THE POINT — this arm is NOT the
        # twin of the env one, and an earlier draft of this comment claimed it
        # was. MEASURED: `home.chat_name()` is the validated seam and RAISES
        # SeatNameError on a noncanonical HELM_CHAT_NAME ("refusing to join or
        # post under it"), so a hostile name cannot reach the roster through
        # join at all. `write_roster` itself carries no token check across any
        # of its four call sites, so this rung guards a direct roster write
        # rather than a live join path. Kept because the cost is one call and
        # the roster is a file, not because a reachable writer is known.
        if not seats._SEAT_TOKEN.fullmatch(hits[0]):
            return None, "roster"
        return seats._seat_label(hits[0]), "roster"
    return (None, "roster-ambiguous") if hits else (None, None)


def rows():
    census, census_failed, who_failed, census_partial = _census()
    # the daemon scan runs AFTER the census bracket: a daemon that started
    # between the two scans — whose freshly-spawned claude IS censused — is
    # then in the set, so the ppid walk can never pass through the missing
    # pid to init and read a false proven-HEADLESS for a live-pane process
    daemons, unproven, daemons_failed = _daemon_pids()
    daemons_partial = bool(unproven)
    roster, roster_err = _roster()
    stamp_keys = ("CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_SESSION_ID",
                  "CLAUDE_CODE_BRIDGE_SESSION_ID")
    live = session.live_sids(list(census.values()))
    tilde = lambda p: p.replace(os.path.expanduser("~"), "~")  # noqa: E731
    out, row_envs = [], {}
    for pid in sorted(census):  # the census is the ONLY row source
        sr = census[pid]
        # the census's BRACKETED environ, verbatim — never a live re-read
        # that a pid reused since the census could answer
        env = sr["environ"]
        env_unknown = env is None  # failed probe, NOT an empty environment
        sid = sr["session"]
        sid_src = _SID_SRC.get(sr["identity"])
        candidates = list(sr["possible_sessions"])
        # a census row can itself be a failed probe: unresolved because the
        # record/environ READ failed — or because the who rung was never
        # probed at all — not because evidence proved a blank
        # a mandatory probe that failed AFTER comm proved claude: the pid is
        # known, every fact on it is unprovable — the row is UNKNOWN and the
        # verb's exit status must say so, never a silent drop
        probe_failed = bool(sr.get("probe_failed")
                            or sr.get("who_probe_failed")
                            or sr.get("who_context_mismatch"))
        sid_unknown = sid is None and (
            sr.get("declared_reason") in _FAILED_PROBE_REASONS
            or sr.get("who_probe_failed") or sr.get("who_context_mismatch")
            or (who_failed and not sr.get("child")))
        double_open = bool(sid) and len(live.get(sid) or ()) > 1
        seat, seat_src = _seat_for(sid, env, roster, roster_err)
        seat, seat_unrenderable = _display_seat(seat, seat_src)
        deck = (env or {}).get("HELM_SKILL_DECK", "")
        # the census's BRACKETED cwd, verbatim: cwd=None is a failed probe
        # and stays UNKNOWN — a later /proc read would be a different
        # process-generation moment and must never be composed in
        cwd = sr["cwd"]
        row_envs[pid] = env
        if daemons_failed:
            daemon_state, daemon = "unknown", None
        else:
            daemon_state, daemon = _daemon_for(
                pid, sr["start"], daemons, unproven)
        root = sr["root"]
        t_counts, t_blind, t_why, t_known = _throttle(pid, sr["start"])
        out.append({
            "pid": pid,
            "seat": seat, "seat_src": seat_src,
            "seat_unrenderable": seat_unrenderable,
            "sid": sid, "sid_src": sid_src,
            "candidates": candidates,
            "double_open": double_open,
            "probe_failed": probe_failed,
            # the row-level bit carries EVERY failed probe the row rests on
            # (home/config root and seat included) so JSON consumers never
            # receive a false known-row bit the footer contradicts
            "unknown": (probe_failed or env_unknown or sid_unknown
                        or cwd is None or root is None or seat_unrenderable
                        or seat_src in ("roster-error", "roster-ambiguous")
                        or daemon_state == "unknown" or not t_known),
            # THE SAME DISJUNCTION MINUS THE SEAT REFUSAL, because `unknown`
            # has two KINDS of cause and they CO-OCCUR. The footer used to
            # partition on `seat_unrenderable`, which silently assumed they
            # were exclusive: a row with a hostile name AND a failed cwd probe
            # reported only the refusal and hid the probe failure (shown by
            # an exact-tip repro). This is computed HERE, where the causes are
            # already in hand, so no reader has to re-derive a disjunction and
            # drift from it — which is how the footer got it wrong in the
            # first place.
            # A FAILED THROTTLE ACQUISITION IS A FAILED PROBE, so it joins
            # BOTH disjunctions and keeps their distinction: it belongs here
            # as well as in `unknown` because it is a probe that did not
            # answer, where a seat refusal is not. The two causes co-occur.
            "probe_unknown": bool(probe_failed or env_unknown or sid_unknown
                                  or cwd is None or root is None
                                  or seat_src in ("roster-error",
                                                  "roster-ambiguous")
                                  or daemon_state == "unknown"
                                  or not t_known),
            "home": tilde(root) if root else "?",
            "cwd": tilde(cwd) if cwd else "?",
            "daemon": daemon, "daemon_state": daemon_state,
            # THE KERNEL'S OWN THROTTLE TALLY, beside the census row it
            # describes. `start` is the bracketed generation, so a pid reused
            # since the bracket refuses to attribute rather than reporting a
            # stranger's pressure under this seat's name.
            "throttle_counts": t_counts,
            "throttle_blind": t_blind,
            "throttle_why": t_why,
            "throttle_known": t_known,
            "pane": None,
            "stamps": None if env_unknown else
                      sum(1 for k in stamp_keys if k in env),
            "deck": "?" if env_unknown else _deck_label(deck),
        })
    hosted = [r for r in out if r["daemon_state"] == "daemon"]
    terms_failed, inventories = False, {}
    for r in hosted:
        daemon = r["daemon"]
        env = row_envs[r["pid"]]
        key = (daemon, (env or {}).get("ORCA_USER_DATA_PATH"))
        if key not in inventories:
            inventories[key] = _orca_terminals(
                daemon, daemons[daemon], env)
        terminals, failed = inventories[key]
        if failed:
            terms_failed = _mark_unknown(r)
            continue
        r["pane"], proven = _pane_for(env, terminals)
        if not proven:
            _mark_unknown(r)
    pane_counts = {}
    for r in hosted:
        if r["pane"]:
            pane_counts[r["pane"]] = pane_counts.get(r["pane"], 0) + 1
    for r in hosted:
        if r["pane"] and pane_counts[r["pane"]] > 1:
            r["pane"] = None
            _mark_unknown(r)   # one live pane cannot own two process rows
    # FINAL generation recheck, AFTER every display probe: the host walk
    # re-read /proc later than the census bracket. If the pid's generation
    # changed in between, those fresh reads describe a DIFFERENT process
    # (PID reuse) — their conclusions (daemon, proven HEADLESS, pane) must
    # die UNKNOWN, never be composed onto the census facts. Env facts need
    # no recheck: they come from the census's own bracket.
    for r in out:
        if _generation_intact(r["pid"], census[r["pid"]]["start"]):
            continue
        r["daemon"], r["daemon_state"], r["pane"] = None, "unknown", None
        # THE THROTTLE READ DIES WITH THEM. It was taken against the pid this
        # recheck just proved is a different process, so its counters describe
        # someone else — keeping them while clearing the daemon and pane facts
        # beside them published one process's throttling under another's row.
        # None, never {}: {} is a process whose counters all read zero.
        r["throttle_counts"], r["throttle_blind"] = None, None
        r["throttle_known"] = False
        r["throttle_why"] = ("this pid changed generation after the throttle "
                             "read, so those counters are another process's")
        # a failed generation recheck IS a failed probe — the recheck is the
        # probe. Same helper as every other post-builder mutation.
        _mark_unknown(r)
    _join_vendor(out)
    _join_resolved_model(out)   # after the vendor join: it reads vendor_family
    return out, sorted(daemons), {
        "census_failed": census_failed, "census_partial": census_partial,
        "who_failed": who_failed, "daemons_failed": daemons_failed,
        "daemons_partial": daemons_partial, "terms_failed": terms_failed,
        # A THROTTLE READ THAT DID NOT COMPLETE IS A FAILED PROBE, and the
        # comment on the rc line below says every failed probe gates the exit
        # code. A throttle failure that reaches only the printed sentence
        # lets `--json` carry an unavailable throttle line beside rc 0, which
        # launders a failed read into a successful shell verdict.
        "throttle_partial": any(not r.get("throttle_known") for r in out)}



# ── THE VENDOR COLUMN — one predicate, read once for the whole table ─────────
# THE OWNER'S RULING: "shouldnt they show as unavailable in the roster if their
# credits dont work automatically." `helm proxywatch` emits FAMILY-DARK lines
# beside this table and this verb printed no family word at all — `git grep
# upstream|proxywatch|dark` over this module returned zero. The only UNKNOWN a
# walled row carried was the SID/PANE probe column, which most rows carry
# including every healthy seat, so a reader taking it for the vendor answer
# attaches a true measurement to the wrong claim. Both facts print now, each
# saying what it measured.
#
# NOT A PROBE AND NOT A COMPLETENESS BIT. Every other column here is a LIVE
# probe and a failed one gates this verb's exit code. The vendor column is the
# opposite kind of fact: a cached record proxywatch's timer wrote, read ONCE
# for the whole table (`availability_map`), and an availability UNKNOWN is this
# verb reporting that record's state — never a failed census probe. So it stays
# out of `unknown`/`probe_unknown` and out of the estate flags: folding it in
# would make a stale proxywatch timer read as a broken /proc walk, and would
# flip rc for 55 rows that probed perfectly.
def _join_vendor(table):
    """Stamp each row's vendor availability, in place. Never raises.

    The census carries no launch metadata, and the family a seat's NAME implies
    is not authority — a `pi-codex` row is billed to `codex`. `availability_map`
    asks the roster for the verified runtime when a caller hands it none, so
    this column cannot read silence about a wall the roster is naming."""
    try:
        from . import seat_usability
        names = sorted({r.get("seat") for r in table
                        if r.get("seat") and not r.get("seat_unrenderable")})
        avail = seat_usability.availability_map(names)
    except Exception as e:                  # noqa: BLE001 — a column never
        for r in table:                     # takes the estate table down
            r["vendor"] = None
            r["vendor_text"] = ""
            r["vendor_detail"] = None
            r["vendor_error"] = ("the vendor availability join failed (%s: %s)"
                                 % (e.__class__.__name__, e))
        return
    for r in table:
        rec = avail.get(r.get("seat"))
        r["vendor"] = (rec or {}).get("state")
        r["vendor_text"] = (rec or {}).get("text") or ""
        r["vendor_family"] = (rec or {}).get("family")
        # the long, ORIGIN-AWARE sentence — the row prints the short neutral
        # form and the footer prints this one, so a scan line stays a scan line
        r["vendor_detail"] = (rec or {}).get("detail")
        r["vendor_origin"] = (rec or {}).get("origin")


def _join_resolved_model(table):
    """Stamp each row's MEASURED route, in place. Never raises.

    KEYED ON THE ROW'S OWN SESSION ID, not on the seat's newest join. This
    census walks live PROCESSES and has already resolved a session for each
    one; a seat that rejoined under a second session would otherwise lend its
    newest route to an older process still running beside it. A row with no
    resolvable seat, no session, or no stamped proof carries None and prints
    nothing, because a census that cannot read a route must not invent one
    from the seat name -- which is the whole defect this column exists for.
    """
    try:
        from . import proxywatch, seats
        rows = seats.roster()
    except Exception as e:                  # noqa: BLE001 — a column never
        for r in table:                     # takes the estate table down
            r["resolved"] = None
            r["resolved_text"] = ""
            r["resolved_error"] = ("the measured-route join failed (%s: %s)"
                                   % (e.__class__.__name__, e))
        return
    for r in table:
        r["resolved"] = None
        r["resolved_text"] = ""
        r["resolved_error"] = None
        seat = r.get("seat")
        if not seat or r.get("seat_unrenderable") or not r.get("sid"):
            continue
        try:
            resolved = seats.resolved_route(rows.get(seat), r["sid"])
            if resolved:
                r["resolved"] = resolved
                r["resolved_text"] = proxywatch.resolved_model_phrase(
                    resolved, r.get("vendor_family"))
        except Exception as e:              # noqa: BLE001 — per row, like above
            r["resolved_error"] = ("%s: %s" % (e.__class__.__name__, e))


def _vendor_footer(table):
    """The footer lines the vendor column owes — UNAVAILABLE families named
    once with their cause, and the recovery sentence beside them so nobody
    relaunches a seat to cure a wall."""
    from . import seat_usability
    out = []
    walled, unreadable = {}, {}
    for r in table:
        if r.get("vendor") == seat_usability.UNAVAILABLE:
            walled[r.get("vendor_family")] = r.get("vendor_detail") \
                or r.get("vendor_text")
        elif r.get("vendor") == seat_usability.UNKNOWN:
            unreadable[r.get("vendor_family")] = r.get("vendor_text")
    if walled:
        # THE PER-FAMILY SENTENCE IS THE PREDICATE'S `detail`, WHICH IS ORIGIN-
        # AWARE, and this footer asserts nothing of its own about a provider.
        # A PROXY-COOLDOWN is helm's OWN proxy refusing before any request
        # leaves the box, so a footer that blamed "their provider" would send
        # every reader at the provider, the account and the family catalog,
        # where no reseed and no cred swap can help — the wrong-origin claim
        # that `_dark_reason` exists to prevent (task/1903). The only thing this
        # line adds is the RECOVERY fact, true of every dark state whoever
        # produced it.
        out.append("  ⚫ %d vendor famil%s UNAVAILABLE — these seats are alive "
                   "and their family is not answering usably: %s. A relaunch "
                   "does NOT cure any of these and is not wanted: proxywatch "
                   "recomputes every family from scratch each pass (15m "
                   "timer), so a row flips back to AVAILABLE on the first "
                   "green probe with no human step."
                   % (len(walled), "y" if len(walled) == 1 else "ies",
                      " · ".join(walled[f] for f in sorted(walled, key=str))))
    if unreadable:
        out.append("  ? %d vendor famil%s UNKNOWN — helm could not read the "
                   "persisted verdict, which is NEVER the same answer as "
                   "UNAVAILABLE: %s"
                   % (len(unreadable), "y" if len(unreadable) == 1 else "ies",
                      "; ".join(unreadable[f]
                                for f in sorted(unreadable, key=str))))
    return out


def cmd_fleet(args):
    """fleet [--json] — every live claude process, composition truth."""
    # flags-only membership reader — guard the tail before the census scan so
    # `fleet --bogus` refuses rc 2 instead of silently printing the estate and
    # exiting 0 (an unknown flag must never ride through as if it existed)
    from .cli import guard_tail
    rc = guard_tail("helm fleet", args, flags=("--json",), usage="fleet [--json]")
    if rc is not None:
        return rc
    table, daemons, flags = rows()
    probe_failed = [r for r in table if r["probe_failed"]]
    unknowns = [r for r in table if r["unknown"]]
    # EVERY row-level or estate-wide failed probe gates the exit code. UNKNOWN
    # in JSON with rc=0 launders a failed read into a successful shell verdict.
    rc = 1 if unknowns or any(flags.values()) else 0
    if "--json" in args:
        print(json.dumps({"rows": table, "daemons": daemons, **flags},
                         indent=2))
        return rc
    census_failed, census_partial = (flags["census_failed"],
                                     flags["census_partial"])
    if census_failed:
        print("helm fleet — CENSUS FAILED: /proc could not be enumerated. "
              "The estate is UNKNOWN, not empty — zero rows is a failed "
              "probe, never proven absence.")
        return 1
    process_count = (("at least %d" % len(table)) if census_partial
                     else str(len(table)))
    daemon_count = (("at least %d" % len(daemons))
                    if flags["daemons_partial"] else str(len(daemons)))
    partial_bits = [x for x, bit in (
        ("CENSUS PARTIAL", census_partial),
        ("DAEMON CENSUS PARTIAL", flags["daemons_partial"])) if bit]
    partial = " — " + ", ".join(partial_bits) if partial_bits else ""
    gens, gens_unknown = _daemon_generations(
        {p: session._proc_start(p) for p in daemons})
    print("helm fleet — %s live claude process(es), %s orca daemon(s)%s%s"
          % (process_count, daemon_count, partial,
             _generation_verdict(gens, gens_unknown)))
    # LEVELS EVERY ROW SHARES ARE REPORTED ONCE, NOT PER ROW. A cgroup above
    # the per-process leaves has ONE counter that every descendant inherits,
    # so printing it on all 60 rows repeats a single fact sixty times and
    # buries the level that is actually about one process. Derived from the
    # rows rather than assumed from a path shape, because nothing here may
    # parse a cgroup name to decide what it is.
    # THE DENOMINATOR IS STATED, NOT RE-DERIVED. A process observed with every
    # counter at zero IS observed and does NOT share a level it never
    # reported, and a row whose observation did not complete cannot vouch for
    # any level at all — so neither may sit silently inside a sentence that
    # says "all observed". The footer names both numbers instead.
    observed = [r for r in table if r.get("throttle_known")]
    counted = [r for r in observed if r.get("throttle_counts")]
    # WHO THE FOOTER SPEAKS FOR, carried with the paths it factored. A shared
    # path is shared among THESE rows, and subtracting it from a row that was
    # never in the computation deletes that row's OWN differing value on the
    # authority of a fact that was never about it. Keyed on the census row's
    # pid rather than the seat label, because labels need not be unique.
    shared_members = set()
    shared_throttle = set()
    if len(counted) > 1:
        shared_throttle = set.intersection(
            *(set(r["throttle_counts"]) for r in counted))
        shared_throttle = {c for c in shared_throttle
                           if len({r["throttle_counts"][c]
                                   for r in counted}) == 1}
        if shared_throttle:
            shared_members = {r["pid"] for r in counted}
    for r in table:
        sid8 = (r["sid"] or "?")[:8]
        if r["sid_src"] in ("argv", "who"):
            tag = " (%s)" % r["sid_src"]
        elif r["sid_src"] == "record":
            tag = ""
        elif r["candidates"]:
            tag = " (%d cwd-candidate(s))" % len(r["candidates"])
        else:
            tag = " (UNRESOLVED)"
        if r["double_open"]:
            tag += " DOUBLE-OPEN"
        host = ("daemon %d pane=%s" % (r["daemon"], r["pane"] or "?")
                if r["daemon_state"] == "daemon" else
                "HEADLESS/no-pane" if r["daemon_state"] == "headless"
                else "host=?")
        seat = "UNRENDERABLE" if r.get("seat_unrenderable") else (r["seat"] or ("?" if r["unknown"] else "(no seat)"))
        stamps = "?" if r["stamps"] is None else str(r["stamps"])
        print("  pid %-8d %-18s sid=%s%s%s"
              % (r["pid"], seat, sid8, tag,
                 " UNKNOWN" if r["unknown"] else ""))
        print("       %-13s stamps=%s deck=%-5s home=%s cwd=%s"
              % (host, stamps, r["deck"], r["home"], r["cwd"]))
        # THE VENDOR WORD ON THE ROW ITSELF, not only in a footer. The footer
        # FAMILY-DARK lines were already the fact and the owner still could not
        # act on them, because the ROW he was looking at said nothing and a
        # reader matches rows to footers by hand. A seat the question does not
        # apply to (no proxy family) prints nothing — see seat_usability's
        # NO_VENDOR note.
        if r.get("vendor_text"):
            print("       vendor=%s" % r["vendor_text"])
        elif r.get("vendor_error"):
            print("       vendor=UNKNOWN — %s" % r["vendor_error"])
        # WHICH MODEL THIS PROCESS ACTUALLY ANSWERS ON. Composition truth is
        # the point of this verb, and a seat's route is composition: the same
        # alias on a fallen-back pool is a different model behind an identical
        # name. Silent where no proof exists, like the vendor column.
        if r.get("resolved_text"):
            print("       model=%s" % r["resolved_text"])
        elif r.get("resolved_error"):
            print("       model=UNKNOWN — %s" % r["resolved_error"])
        # PRINTED ONLY WHEN THERE IS SOMETHING TO SEE, and never as an alarm:
        # the line carries its own UNKNOWN-now qualifier, and a seat whose
        # counters all read zero says nothing here rather than a reassuring
        # zero that would read as a clean bill.
        own = {c: n for c, n in (r.get("throttle_counts") or {}).items()
               if not (r["pid"] in shared_members and c in shared_throttle)}
        if own or r.get("throttle_blind") or r.get("throttle_why"):
            from . import seatceiling
            print("       throttle: %s"
                  % seatceiling.summary_of(own, r.get("throttle_blind") or 0,
                                           r.get("throttle_why") or ""))
    try:
        for extra in _vendor_footer(table):
            print(extra)
    except Exception as e:      # noqa: BLE001 — the estate table never dies
        print("  ? the vendor availability footer could not be composed "
              "(%s: %s) — read `helm proxywatch` for the family verdicts"
              % (e.__class__.__name__, e))                    # for a footer
    if shared_throttle:
        # REPORTED ONCE BECAUSE IT IS ONE FACT. Dropping it from the rows was
        # not permission to drop it from the output: a cgroup every process
        # shares has been throttled, and that is an estate-level observation
        # rather than a per-seat one. Same qualifier as every row.
        from . import seatceiling
        ex = counted[0]["throttle_counts"]
        print("  throttle, shared by all %d of %d observed process(es) that "
              "reported any counter: %s"
              % (len(counted), len(observed),
                 seatceiling.summary_of({c: ex[c] for c in shared_throttle})))
        # AND WHO IS NOT IN THAT 'ALL'. Two numbers say a subset was taken;
        # they do not say WHICH processes it left out, and a reader cannot
        # check a population they cannot name. A silent process is observed
        # and reported nothing; an unobserved one is outside the population
        # altogether, and those are different exclusions.
        silent = [r for r in observed if not r.get("throttle_counts")]
        unobserved = [r for r in table if not r.get("throttle_known")]
        for group, why in ((silent, "reported no counter at all, so they are "
                                    "not part of that 'all'"),
                           (unobserved, "could not be observed, so they are "
                                        "outside the population entirely")):
            if group:
                print("    %d of them %s: %s"
                      % (len(group), why, _rownames(group)))
    ghosts = [r for r in table if r["daemon_state"] == "headless"]
    if ghosts:
        print("  ⚠ %d process(es) have NO orca pane — the owner cannot see or "
              "type at them" % len(ghosts))
    # TWO REASONS A ROW IS UNKNOWN, AND THE OPERATOR'S NEXT MOVE DIFFERS.
    # A failed probe says DOUBT THE INSTRUMENT; a refused identity says a
    # process presented a name that is not a seat name, so doubt the PROCESS.
    # Before the identity rung existed every UNKNOWN really was a probe
    # failure and one sentence was honest; now the same sentence would send a
    # reader hunting a broken probe while a noncanonical name sat in the row
    # it was printed for. The tool knows the difference — it must say it.
    # INDEPENDENT, NEVER PARTITIONED — a row can be both, and the first cut
    # of this split used `not seat_unrenderable` for the probe side, which
    # dropped every such row from the probe count and hid real evidence.
    unknowns = [r for r in table if r["unknown"]]
    refused = [r for r in unknowns if r.get("seat_unrenderable")]
    probes = [r for r in unknowns if r.get("probe_unknown")]
    if probes:
        print("  ? %d row(s) carry UNKNOWN columns — failed probes, not "
              "absence; verify by hand before acting" % len(probes))
    if refused:
        print("  ? %d row(s) show UNRENDERABLE — a name was present and is "
              "NOT a canonical seat token, so it is refused rather than "
              "printed; it can neither reshape this table nor impersonate a "
              "seat. Identify the process by pid, never by that name."
              % len(refused))
    estate = [name for name, bit in (
        ("who scan", flags["who_failed"]),
        ("daemon scan", flags["daemons_failed"]),
        ("daemon census", flags["daemons_partial"]),
        ("terminal list", flags["terms_failed"])) if bit]
    if estate:
        print("  ! estate-wide probe(s) FAILED: %s — the affected columns "
              "are UNKNOWN, never proven blanks" % ", ".join(estate))
    if probe_failed:
        print("  ! pid(s) %s PROVED claude but a mandatory census probe "
              "FAILED — facts unprovable (UNKNOWN), never proven absence"
              % ", ".join(str(r["pid"]) for r in probe_failed))
    if census_partial:
        print("  ! CENSUS PARTIAL: a same-uid pid failed a mandatory probe "
              "before its comm could prove or refute claude — the total "
              "above is a floor, not a certified estate")
    doubles = sorted({r["sid"] for r in table if r["double_open"]})
    if doubles:
        print("  ‼ %d sid(s) are live in MULTIPLE pids (DOUBLE-OPEN) — "
              "resolve before resuming or injecting" % len(doubles))
    return rc
