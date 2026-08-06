"""Spawn registration and runtime recovery for :mod:`helm.seat`."""
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

def _prove_spawned_pane(ad, handle):
    """Prove the exact handle returned by spawn is independently live."""
    detail = "returned handle %s was absent from %s inventory" % (handle, ad.name)
    for _ in range(20):
        try:
            matches = [row for row in ad.list() if row.get("handle") == handle]
        except Exception as e:
            detail = "%s pane inventory failed: %s" % (ad.name, e)
            matches = []
        if len(matches) == 1 and _pane_live(matches[0]):
            return True, None
        if len(matches) > 1:
            return False, "returned handle %s matched %d inventory rows" % (
                handle, len(matches))
        if matches:
            detail = "returned handle %s is not live" % handle
        time.sleep(0.05)
    return False, detail


def _pane_sendable(row):
    """Whether a row can RECEIVE input, which is a weaker claim than _pane_live.

    An orphaned pane is READ-BLIND but SEND-CAPABLE: `orphaned` means the PTY
    has no live RENDERER, so reads return "" — but the PTY itself is still
    attached and writable, so a send lands. Measured 2026-07-26: kimi rescued a
    codex seat stuck at 102% by sending /compact straight to its orphaned
    handle, and it compacted.

    This exists because making `_pane_live` honest about orphaned panes (the
    correct read-side fix) also made every ACTUATION refuse, since
    `_resolve_registered_pane` gates on it and the autocompact injection path
    resolves through there. 30 of 34 panes were orphaned, so the rescue path
    died for exactly the seats most likely to need rescuing. A send must not be
    required to prove a renderer it does not use.

    Identity is NOT weakened by this: callers prove the pane through the spawn
    register and the live session's own ORCA_TERMINAL_HANDLE, neither of which
    needs a readable pane."""
    if not (row.get("writable") is True):
        return False
    status = str(row.get("status") or "").strip().lower()
    return status not in ("disconnected", "closed", "gone", "exited", "dead")


_SEND_ONLY_DETAIL = ("SEND-ONLY (orphaned: writable, but reads return empty, "
                     "so this action cannot be confirmed by reading the pane)")


def _live_session_orca_identity(d, session):
    """The exact live Claude process's remint-stable Orca identity.

    The pid-keyed Claude session record proves session -> process incarnation;
    only the non-secret Orca identity keys are then selected from /proc. Full
    process environments can carry credentials and are never returned/logged.
    """
    from . import sessions
    found = []
    for path in glob.glob(os.path.join(d, "claude", "sessions", "*.json")):
        try:
            with open(path) as f:
                rec = json.load(f)
            if rec.get("sessionId") != session:
                continue
            pid = int(rec.get("pid") or 0)
        except (OSError, ValueError, TypeError):
            continue
        start = rec.get("procStart")
        if pid and start and sessions._pid_is_claude(pid, start):
            found.append(pid)
    if len(found) != 1:
        return None, ("session %s has %d exact live Claude processes; pane "
                      "replacement requires exactly one" % (session, len(found)))
    try:
        with open("/proc/%d/environ" % found[0], "rb") as f:
            env = f.read().split(b"\0")
    except OSError as e:
        return None, "live session environment is unreadable: %s" % e
    wanted = {b"ORCA_PANE_KEY", b"ORCA_WORKTREE_ID"}
    vals = {}
    for item in env:
        key, sep, value = item.partition(b"=")
        if sep and key in wanted:
            vals[key.decode("ascii")] = value.decode("utf-8", "replace")
    pane_key = vals.get("ORCA_PANE_KEY")
    worktree_id = vals.get("ORCA_WORKTREE_ID")
    if not pane_key or not worktree_id:
        return None, "live session has no complete Orca pane/worktree identity"
    return {"pid": found[0], "pane_key": pane_key,
            "worktree_id": worktree_id}, None


def _prove_orca_replacement(d, rec, ad, rows, for_send=False,
                            identity_session=None):
    """Prove one current Orca handle with the caller's required capability.

    `identity_session` is a measured live transcript session for a caller whose
    ordinary pane action deliberately tolerates a stale register session. It may
    locate a reminted handle but never rebinds the durable session; destructive
    callers omit it and remain anchored to the register exactly as before.
    """
    session = identity_session or rec.get("session")
    if not session:
        return None, None, "stale Orca handle has no bound session identity"
    identity, err = _live_session_orca_identity(d, session)
    if err:
        return None, None, err
    recorded_key = rec.get("pane_key")
    recorded_worktree = rec.get("worktree_id")
    if identity_session and identity_session != rec.get("session") \
            and (not recorded_key or not recorded_worktree):
        return None, None, ("spawn has no complete pane/worktree identity to "
                            "bind a mismatched live session")
    if recorded_key and recorded_key != identity["pane_key"]:
        return None, None, "spawn pane key conflicts with the live session"
    if recorded_worktree and recorded_worktree != identity["worktree_id"]:
        return None, None, "spawn worktree identity conflicts with the live session"
    try:
        resolved = ad.resolve_pane(identity["pane_key"])
    except Exception as e:
        return None, None, str(e)
    handle, pty = resolved.get("handle"), resolved.get("pty_id")
    if not handle or not pty:
        return None, None, "Orca pane-key resolution returned no handle/pty identity"
    matches = [row for row in rows if row.get("handle") == handle and
               row.get("pty_id") == pty and
               row.get("worktree_id") == identity["worktree_id"] and
               (_pane_sendable(row) if for_send else
                row.get("writable") is True and _pane_live(row))]
    if len(matches) != 1:
        capability = "sendable" if for_send else "read-live"
        return None, None, ("Orca pane-key resolution matched %d %s inventory "
                            "rows; refusing replacement"
                            % (len(matches), capability))
    fields = {"handle": handle, "pane_key": identity["pane_key"],
              "pty_id": pty, "worktree_id": identity["worktree_id"]}
    return matches[0], fields, None


def _live_seat_orca_identity(seat_name):
    """The Orca identity of the live process that CLAIMS THIS SEAT NAME.

    The reboot fallback for _prove_orca_rebind. Scans /proc for a process whose
    HELM_CHAT_NAME is exactly this seat and returns the Orca keys from that same
    environ. Identity is process-proven: the environ of a live pid is stamped at
    exec and cannot be edited by anything that merely LOOKS like the seat — the
    same standard as the session route, and deliberately NOT titles, cwds, or
    inventory labels, which are copyable presentation.

    REFUSES ON AMBIGUITY. Two processes claiming one seat name is a real
    condition (a double-open, a half-finished relaunch) and picking either would
    bind the register to a coin flip. Fail closed and say how many.
    """
    if not seat_name:
        return None, "no seat name to match"
    want = ("HELM_CHAT_NAME=" + str(seat_name)).encode("utf-8")
    found = []
    for entry in glob.glob("/proc/[0-9]*/environ"):
        try:
            with open(entry, "rb") as f:
                env = f.read().split(b"\0")
        except OSError:
            continue                      # gone or not ours — never fatal
        if want not in env:
            continue
        vals = {}
        for item in env:
            key, sep, value = item.partition(b"=")
            if sep and key in (b"ORCA_PANE_KEY", b"ORCA_WORKTREE_ID"):
                vals[key.decode("ascii")] = value.decode("utf-8", "replace")
        if vals.get("ORCA_PANE_KEY") and vals.get("ORCA_WORKTREE_ID"):
            try:
                pid = int(entry.split("/")[2])
            except (IndexError, ValueError):
                continue
            found.append({"pid": pid, "pane_key": vals["ORCA_PANE_KEY"],
                          "worktree_id": vals["ORCA_WORKTREE_ID"]})
    # One seat may legitimately own several processes (the pane plus its beacon
    # and hooks), all carrying the same pane identity. That is ONE pane, not an
    # ambiguity — collapse on the identity, not the pid count.
    distinct = {(f["pane_key"], f["worktree_id"]): f for f in found}
    if len(distinct) != 1:
        return None, ("%d distinct live panes claim seat '%s'; refusing to "
                      "guess which one owns it" % (len(distinct), seat_name))
    return next(iter(distinct.values())), None


def _prove_orca_rebind(d, rec, ad, rows):
    """Prove a seat's CURRENT pane after a REBOOT, where the old pane is gone.

    WHY THIS IS NOT _prove_orca_replacement. That one repairs a handle change
    WITHIN a pane generation, so it guards `pane_key`/`worktree_id` drift — a
    changed pane key there means something redirected the seat and it refuses.
    A REBOOT changes both LEGITIMATELY: the machine came back, orca minted new
    panes, and every cached key in the register describes a pane that no longer
    exists. Feeding that case to the repair path returns "spawn pane key
    conflicts with the live session" — correct for its own question, and the
    reason a reboot leaves the whole register unusable.

    MEASURED 2026-07-28, the morning after an Ubuntu reboot: `helm seat where`
    reported 6 of 7 seats GONE while ds4pro and gemini were demonstrably alive
    and posting in chat. The seats never died; the REGISTER did. helm keyed a
    durable thing (the seat) on an ephemeral one (the orca handle) with no
    reconstruction path across a boot — the same shape as the chat-node data
    dir that tmpfs wiped and nothing re-inited.

    WHAT STILL ANCHORS IDENTITY, because dropping two guards must not drop
    safety: the SESSION is the durable identity and it is untouched.
    _live_session_orca_identity requires the recorded sessionId to match a
    LIVE claude process (pid + procStart proven), reads the Orca keys from that
    exact process's /proc environ — non-forgeable, unlike a title or a cwd —
    and the inventory match below still demands exactly one connected, writable
    row. pane_key and worktree_id were only ever cached copies of facts that
    the boot invalidated; comparing them across a reboot compares a seat to its
    own dead past."""
    session = rec.get("session")
    identity, err = (None, "seat has no bound session identity to rebind from")
    if session:
        identity, err = _live_session_orca_identity(d, session)
    if err:
        # SESSION-ANCHORED PROOF CANNOT REACH A SEAT THAT CAME BACK ON A NEW
        # SESSION, which is the common reboot case: the pane was respawned
        # rather than resumed, so the register's sessionId names a process that
        # no longer exists ("0 exact live Claude processes"). Measured
        # 2026-07-28: gemini and codex resolved by session, while ds4pro, grok
        # and kimi refused — and ds4pro was provably alive the whole time.
        #
        # Fall back to the seat's OWN NAME as carried by its live process.
        # HELM_CHAT_NAME is stamped into the environ at launch and is exactly
        # as non-forgeable as the session route: it is read from /proc of a
        # live pid, not from a title, a cwd, or an inventory label — the
        # copyable-presentation sources that authorize nothing.
        identity, name_err = _live_seat_orca_identity(rec.get("seat"))
        if identity is None:
            # Report the SESSION error, not the fallback's: the session route is
            # the primary and its message says which seat and how many
            # candidates. Appending the fallback's reason keeps both visible.
            return None, None, "%s; and %s" % (err, name_err)
    try:
        resolved = ad.resolve_pane(identity["pane_key"])
    except Exception as e:
        return None, None, str(e)
    handle, pty = resolved.get("handle"), resolved.get("pty_id")
    if not handle or not pty:
        return None, None, "Orca pane-key resolution returned no handle/pty identity"
    matches = [row for row in rows if row.get("handle") == handle and
               row.get("pty_id") == pty and
               row.get("writable") is True and _pane_live(row)]
    if len(matches) != 1:
        return None, None, ("Orca pane-key resolution matched %d connected, "
                            "writable inventory rows; refusing rebind"
                            % len(matches))
    fields = {"handle": handle, "pane_key": identity["pane_key"],
              "pty_id": pty, "worktree_id": identity["worktree_id"]}
    return matches[0], fields, None


def rebind_seat(seat_name, d, ad, apply=False):
    """Re-point one seat's register at the pane its LIVE process occupies now.

    Returns (fields, err). apply=False proves the rebind and reports it without
    writing — the dry-run default every destructive helm verb carries, because
    a register rewrite is not observable after the fact.
    """
    import fcntl
    from . import pk

    def run():
        rec = _spawn_record(d)
        if not rec or rec.get("seat") != seat_name:
            return None, "no spawn register for this seat"
        if rec.get("harness") != "orca":
            return None, "rebind is orca-only (harness: %s)" % rec.get("harness")
        try:
            rows = ad.list()
        except Exception as e:
            return None, str(e)
        _, fields, err = _prove_orca_rebind(d, rec, ad, rows)
        if err:
            return None, err
        if fields.get("handle") == rec.get("handle"):
            return dict(fields, unchanged=True), None
        if not apply:
            return dict(fields, dry_run=True), None
        rec.update(fields)
        try:
            pk.write_json(_spawn_path(d), rec)
        except OSError as e:
            return None, "rebind write failed: %s" % e
        return fields, None

    os.makedirs(d, mode=0o700, exist_ok=True)
    try:
        with open(os.path.join(d, ".spawn.lock"), "a") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            return run()
    except OSError as e:
        return None, "rebind lock failed: %s" % e


def _repair_orca_handle(seat_name, d, ad, old_handle, locked=False,
                        for_send=False, identity_session=None):
    """Re-prove and atomically replace one stale Orca handle in spawn.json."""
    import fcntl
    from . import pk

    def repair():
        rec = _spawn_record(d)
        if not rec or rec.get("seat") != seat_name or \
                rec.get("harness") != "orca":
            return None, "spawn identity changed before pane repair"
        if rec.get("handle") not in (old_handle,):
            return None, "spawn handle changed before pane repair"
        try:
            rows = ad.list()
        except Exception as e:
            return None, str(e)
        replacement, fields, err = _prove_orca_replacement(
            d, rec, ad, rows, for_send=for_send,
            identity_session=identity_session)
        if err:
            return None, err
        rec.update(fields)
        try:
            pk.write_json(_spawn_path(d), rec)
        except OSError as e:
            return None, "spawn handle repair write failed: %s" % e
        return (fields["handle"], replacement), None

    if locked:
        return repair()
    os.makedirs(d, mode=0o700, exist_ok=True)
    try:
        with open(os.path.join(d, ".spawn.lock"), "a") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            return repair()
    except OSError as e:
        return None, "spawn handle repair lock failed: %s" % e


def _resolve_registered_pane(seat_name, d=None, adapter=None, repair=True,
                             locked=False, for_send=False,
                             identity_session=None):
    """Resolve the one pane authorized by this seat's spawn register.

    `for_send=True` resolves for ACTUATION rather than observation: an orphaned
    pane is accepted when it is still writable, because a send needs a PTY and
    not a renderer (see `_pane_sendable`). Identity is unchanged — the register
    still authorizes the pane — only the liveness bar differs, and it differs
    because reading and writing genuinely need different things.

    The register is the durable identity owner shared by every pane actuator
    (autocompact injection, resume, and duplicate-seat reap). Titles and pane
    content are mutable/copyable presentation and never authorize a write or
    stop. Orca handle remints are recovered through an exact live session's PID
    -> ORCA_PANE_KEY -> runtime resolvePane chain. By default that is the durable
    registered session; `identity_session` lets a non-destructive caller prove a
    newer measured transcript session without rebinding the register. The handle
    alone is atomically repaired. Returns (adapter, handle, detail);
    adapter/handle are both non-None only after identity is proven.
    """
    from . import harness
    family, err = _seat_family(seat_name)
    if err:
        return None, None, err
    d = d or _instance_dir(family, seat_name)
    rec = _spawn_record(d)
    if not rec:
        return None, None, ("no authoritative spawn handle for %r; run `helm "
                            "seat spawn`/`resume` to register it" % seat_name)
    if rec.get("seat") != seat_name:
        return None, None, "spawn record identity mismatch for %r" % seat_name
    recorded_harness = rec.get("harness")
    if recorded_harness == "headless":
        return None, None, ("seat is registered headless (pid %s); no pane "
                            "input channel exists" % rec.get("pid"))
    handle = rec.get("handle")
    if not recorded_harness or not handle:
        return None, None, "spawn record for %r has no pane identity" % seat_name

    ad = adapter if adapter is not None and \
        adapter.name == recorded_harness else None
    if ad is None:
        detected = harness.detect()
        ad = detected if detected is not None and \
            detected.name == recorded_harness else None
    if ad is None:
        cls = harness.ADAPTERS.get(recorded_harness)
        path = shutil.which(cls.bin) if cls else None
        if not path:
            return None, None, "recorded %s adapter is unavailable" \
                % recorded_harness
        ad = cls(path)
    try:
        rows = ad.list()
    except harness.HarnessError as e:
        return None, None, str(e)
    row = next((row for row in rows if row.get("handle") == handle), None)
    if row is not None and _pane_live(row):
        return ad, handle, "registered pane %s via %s" % (handle, ad.name)
    # ACTUATION accepts what OBSERVATION must not: an orphaned pane is
    # read-blind and send-capable, so a caller that only needs to WRITE is not
    # made to prove a renderer it will never use. Said explicitly in the detail
    # so an operator reading a log knows this send went to a pane nobody can
    # read back — the send is authorized, the confirmation is not available.
    if for_send and row is not None and _pane_sendable(row):
        return ad, handle, ("registered pane %s via %s — %s"
                            % (handle, ad.name, _SEND_ONLY_DETAIL))
    # Name the field that DECIDED the rejection, never the field that is fine.
    # For an orphaned pane `status` is "connected" — printing it tells the
    # operator the pane is connected (true and useless) and hides that it is
    # orphaned (the whole finding), which is the exact false diagnosis this
    # lane exists to fix. orca's `orphaned` is the decisive field; say so.
    if row is not None and row.get("orphaned"):
        why = "orphaned (PTY has no live renderer)"
    else:
        why = (row or {}).get("status") or "not live"
    stale = "registered handle %s is %s on %s" % (handle, why, ad.name)
    if ad.name == "orca" and hasattr(ad, "resolve_pane"):
        if not repair:
            replacement, fields, replacement_err = _prove_orca_replacement(
                d, rec, ad, rows, for_send=for_send,
                identity_session=identity_session)
            if fields:
                current = fields["handle"]
                capability = (" — " + _SEND_ONLY_DETAIL
                              if for_send and replacement.get("orphaned") else "")
                return ad, current, ("%s; identity-proven replacement pane %s%s "
                                     "(register unchanged in dry-run)"
                                     % (stale, current, capability))
        else:
            repaired, replacement_err = _repair_orca_handle(
                seat_name, d, ad, handle, locked=locked, for_send=for_send,
                identity_session=identity_session)
            if repaired:
                current, replacement = repaired
                capability = (" — " + _SEND_ONLY_DETAIL
                              if for_send and replacement.get("orphaned") else "")
                return ad, current, ("%s; repaired spawn handle to %s via exact "
                                     "session pane-key identity%s"
                                     % (stale, current, capability))
        if replacement_err:
            stale += "; replacement unavailable: " + replacement_err
    return ad, None, stale


def _reap_stale(seat_name, d, ad, allow_live=False, locked=False):
    """Reap only the process/pane authorized by the spawn register.

    The same register resolver used by autocompact proves pane identity here;
    copied launch text and mutable titles never authorize destruction. Any
    additional same-title pane blocks replacement instead of being guessed at.
    """
    notes, errors = [], []
    rec = _spawn_record(d)
    if rec and rec.get("seat") != seat_name:
        return notes, ["spawn record identity mismatch for %r; refusing reap"
                       % seat_name]

    pid = (rec or {}).get("pid")
    if (rec or {}).get("harness") == "headless" and pid:
        live = _recorded_pid_alive(rec)
        if live is None:
            errors.append("stale headless %s pid %s is live but its process "
                          "identity is unverifiable; refusing to kill it"
                          % (seat_name, pid))
        elif live and not allow_live:
            errors.append("registered headless %s pid %s is LIVE; `seat spawn` "
                          "refuses implicit replacement — pass --replace"
                          % (seat_name, pid))
        elif live:
            try:
                os.kill(pid, signal.SIGTERM)
                for _ in range(15):
                    if _recorded_pid_alive(rec) is not True:
                        break
                    time.sleep(0.2)
                if _recorded_pid_alive(rec) is True:
                    os.kill(pid, signal.SIGKILL)
                    for _ in range(10):
                        if _recorded_pid_alive(rec) is not True:
                            break
                        time.sleep(0.1)
                if _recorded_pid_alive(rec) is True:
                    errors.append("stale headless %s pid %s survived SIGKILL"
                                  % (seat_name, pid))
                else:
                    notes.append("reaped stale headless %s (pid %d)"
                                 % (seat_name, pid))
            except OSError as e:
                errors.append("stale headless %s pid %s NOT reaped (%s)"
                              % (seat_name, pid, e))
        return notes, errors

    pane_ad, handle, detail = _resolve_registered_pane(
        seat_name, d=d, adapter=ad, locked=locked)
    adapters = []
    if pane_ad is not None:
        adapters.append(pane_ad)
    if ad is not None and all(a.name != ad.name for a in adapters):
        adapters.append(ad)
    title_only = []
    for adapter in adapters:
        try:
            rows = adapter.list()
        except Exception as e:
            errors.append("%s pane scan failed (%s)" % (adapter.name, e))
            continue
        title_only.extend(row.get("handle") for row in rows
                          if row.get("handle") and row.get("handle") != handle
                          and row.get("title") == seat_name)
    if title_only:
        errors.append("unregistered pane(s) %s have mutable title %r; refusing "
                      "identity-by-title reap" % (", ".join(title_only),
                                                   seat_name))
        return notes, errors
    if rec and rec.get("harness") not in (None, "headless") and pane_ad is None:
        errors.append("recorded pane cannot be checked or reaped: " + detail)
        return notes, errors
    if handle is not None and not allow_live:
        errors.append("registered pane %s for %s is LIVE; `seat spawn` refuses "
                      "implicit replacement — pass --replace"
                      % (handle, seat_name))
        return notes, errors
    if handle is not None:
        try:
            pane_ad.stop(handle)
            notes.append("reaped stale %s pane %s" % (seat_name, handle))
        except Exception as e:
            errors.append("stale %s pane %s NOT reaped (%s)"
                          % (seat_name, handle, e))
    return notes, errors


def _headless_spawn(launch_sh, onboarding, cwd, log_path):
    """The standalone path: launch.sh detached (start_new_session=True = its
    own setsid session — survives this CLI and any parent pane), stdin from
    /dev/null, stdout+stderr appended to spawn.log (the nohup shape). The
    onboarding is launch.sh's POSITIONAL ARG: the script execs
    `claude … "$@"`, so the prompt lands as the seat's first turn at boot —
    the launch-time delivery, since headless has no pane to inject into."""
    with open(log_path, "ab") as log, open(os.devnull, "rb") as devnull:
        p = subprocess.Popen([launch_sh, onboarding], cwd=cwd, stdin=devnull,
                             stdout=log, stderr=log, start_new_session=True)
    return p.pid


def _register_spawn(seat_name, d, rec):
    """Write the authoritative spawn.json, then its best-effort roster mirror.
    False means the spawn must be torn back down: an unregistered headless
    process cannot be found safely for the next duplicate-name reap."""
    from . import pk
    try:
        pk.write_json(_spawn_path(d), rec)
    except OSError as e:
        print("helm seat: spawn register write failed (%s): %s"
              % (_spawn_path(d), e), file=sys.stderr)
        return False
    try:
        from . import seats as _seats
        # Provenance rides into the mirror: a derived room stays derived (it
        # may NEVER overwrite an explicit/operator home — write_roster's law);
        # no room writes NO home (join derives the real one at SessionStart).
        family, family_err = _seat_family(seat_name)
        _seats.write_roster(
            seat_name, cwd=rec.get("worktree"), home_room=rec.get("room"),
            home_room_source=None if not rec.get("room")
            else "derived" if rec.get("room_source") == "derived"
            else "explicit",
            runtime={"agent_harness": "claude", "family": family,
                     "backend": "proxy"} if not family_err else None)
    except Exception as e:
        print("helm seat: chat-roster mirror skipped (%s) — spawn.json is "
              "still authoritative for `helm seat where`" % e, file=sys.stderr)
    return True


def _backfill_spawn_session(seat_name, d, ad):
    """Recover a SessionStart that raced the initial spawn register write."""
    from . import pk
    rec = _spawn_record(d)
    if not rec or rec.get("seat") != seat_name or rec.get("harness") != "orca":
        return False
    sessions = [rec.get("session")] if rec.get("session") else []
    if not sessions:
        for path in glob.glob(os.path.join(d, "claude", "sessions", "*.json")):
            try:
                with open(path) as f:
                    sid = json.load(f).get("sessionId")
            except (OSError, ValueError, AttributeError):
                continue
            if sid and sid not in sessions:
                sessions.append(sid)
    try:
        rows = ad.list()
    except Exception:
        return False
    candidates = []
    for sid in sessions:
        probe = dict(rec, session=sid)
        _, fields, err = _prove_orca_replacement(d, probe, ad, rows)
        if not err and fields and fields["handle"] == rec.get("handle"):
            candidates.append((sid, fields))
    if len(candidates) != 1:
        return False
    sid, fields = candidates[0]
    rec["session"] = sid
    rec.update(fields)
    try:
        pk.write_json(_spawn_path(d), rec)
    except OSError:
        return False
    return True


def _sessionstart_pane_fields(rec):
    """Prove this hook process belongs to the registered pane/process."""
    kind = rec.get("harness")
    if kind == "headless":
        if os.getppid() != rec.get("pid") or _recorded_pid_alive(rec) is not True:
            return None, "SessionStart is not a child of the registered headless process"
        return {}, None
    if kind != "orca":
        return None, "SessionStart binding is unsupported for harness %r" % kind
    pane_key = os.environ.get("ORCA_PANE_KEY")
    worktree_id = os.environ.get("ORCA_WORKTREE_ID")
    if not pane_key or not worktree_id:
        return None, "SessionStart has no complete Orca pane/worktree identity"
    if rec.get("pane_key") and rec["pane_key"] != pane_key:
        return None, "SessionStart pane key conflicts with the spawn register"
    if rec.get("worktree_id") and rec["worktree_id"] != worktree_id:
        return None, "SessionStart worktree identity conflicts with the spawn register"
    from . import harness
    path = shutil.which(harness.OrcaAdapter.bin)
    if not path:
        return None, "Orca adapter unavailable during SessionStart binding"
    ad = harness.OrcaAdapter(path)
    try:
        resolved = ad.resolve_pane(pane_key)
        rows = ad.list()
    except harness.HarnessError as e:
        return None, str(e)
    handle, pty = resolved.get("handle"), resolved.get("pty_id")
    matches = [row for row in rows if row.get("handle") == handle and
               row.get("pty_id") == pty and
               row.get("worktree_id") == worktree_id and
               row.get("writable") is True and _pane_live(row)]
    if len(matches) != 1:
        return None, ("SessionStart pane identity matched %d connected, writable "
                      "inventory rows" % len(matches))
    if not rec.get("pane_key") and rec.get("handle") != handle:
        return None, "initial SessionStart pane does not match the spawned handle"
    return {"handle": handle, "pane_key": pane_key, "pty_id": pty,
            "worktree_id": worktree_id}, None


def _bind_spawn_session(seat_name, session, source=None):
    """Bind SessionStart's live session id to this seat's spawn register.

    Spawn cannot know the new Claude session before the process starts. The
    SessionStart hook is the first authoritative owner of that identity; it
    updates only an exact-seat register under the same per-seat lifecycle lock.
    """
    if not session:
        return False
    family, err = _seat_family(seat_name)
    if err or _seat_surface_error(family, seat_name):
        return False
    import fcntl
    from . import pk
    d = _instance_dir(family, seat_name)
    try:
        os.makedirs(d, mode=0o700, exist_ok=True)
        with open(os.path.join(d, ".spawn.lock"), "a") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            rec = _spawn_record(d)
            if not rec:
                return None                 # direct/manual seat, nothing to bind
            if rec.get("seat") != seat_name:
                return False
            fields, identity_err = _sessionstart_pane_fields(rec)
            if identity_err:
                return False
            prior = rec.get("session")
            if prior and prior != session and source != "clear":
                return False
            rec["session"] = session
            rec.update(fields)
            pk.write_json(_spawn_path(d), rec)
            return True
    except OSError:
        return False


def _spawn_plan(seat_name, d, launch_sh, room, cwd, onboard, ad,
                replace=False):
    """--print/--dry-run: the exact per-harness calls, nothing spawned,
    reaped, or re-minted."""
    print("helm seat spawn %s — plan (--print: nothing spawned, reaped, or "
          "re-minted):" % seat_name)
    rec = _spawn_record(d)
    would = []
    if (rec or {}).get("harness") == "headless" and rec.get("pid"):
        live = _recorded_pid_alive(rec)
        if live and replace:
            would.append("kill registered headless pid %d (explicit --replace)"
                         % rec["pid"])
        elif live:
            would.append("REFUSE live headless pid %d: pass --replace"
                         % rec["pid"])
        elif live is None:
            would.append("REFUSE live headless pid %d: process identity "
                         "unverifiable" % rec["pid"])
    if (rec or {}).get("harness") != "headless":
        pane_ad, handle, detail = _resolve_registered_pane(
            seat_name, d=d, adapter=ad, repair=False)
        adapters = []
        if pane_ad is not None:
            adapters.append(pane_ad)
        if ad is not None and all(a.name != ad.name for a in adapters):
            adapters.append(ad)
        title_only = []
        for adapter in adapters:
            try:
                title_only.extend(row.get("handle") for row in adapter.list()
                                  if row.get("handle") != handle
                                  and row.get("title") == seat_name)
            except Exception as e:
                would.append("(pane scan failed: %s)" % e)
        if title_only:
            would.append("REFUSE mutable-title-only pane match for %r (%s)"
                         % (seat_name, ", ".join(title_only)))
        elif handle is not None and replace:
            would.append("%s stop registered pane %s (explicit --replace)"
                         % (pane_ad.name, handle))
        elif handle is not None:
            would.append("REFUSE live registered pane %s: pass --replace"
                         % handle)
        elif rec and pane_ad is None:
            would.append("REFUSE " + detail)
    print("  reap:  " + ("; ".join(would) or "none (no stale same-name seat)"))
    # The home worktree is RESOLVED, never provisioned, by a plan — a dry run
    # has no side effects. It reads create-or-reuse because that is what the
    # real spawn does at this step.
    print("  cwd:   %s (%s)"
          % (cwd, "already checked out — reused" if cwd and os.path.isdir(cwd)
             else "per-seat home worktree, created at spawn"))
    print("  mint:  refresh %s (child-stamp stripped => persistence ON, "
          "--dangerously canonical, skills linked, hooks wired)" % launch_sh)
    q = shlex.quote(launch_sh)
    if ad is None:
        print("  harness: headless (no metaharness detected — the standalone "
              "default)")
        print("  spawn: detached setsid: %s '<onboarding>'  "
              "(stdin /dev/null, log %s)" % (q, os.path.join(d, "spawn.log")))
        print("  onboard: delivered AT LAUNCH as the claude first-prompt "
              "positional arg")
    else:
        print("  harness: " + ad.name)
        print("  spawn: %s.spawn(command=%s, title=%s, cwd=%s) -> <handle>"
              % (ad.name, q, seat_name, cwd))
        print("  onboard: %s.send(<handle>, <onboarding>, enter=True)"
              % ad.name)
    print("  register: %s {harness, %s, worktree=%s, room=%s}"
          % (_spawn_path(d), "pid" if ad is None else "handle", cwd,
             room or "(derived at join)"))
    print("  onboarding first-prompt:\n    " + onboard)
    return 0


def _seat_home_cwd(seat_name, provision=True, base=None):
    """The seat's DEFAULT working directory: its OWN home worktree
    (`<repo>-wt/seats/<seat>`), never the shared main checkout.

    THE BUG this closes (design: prd/COORDINATION-SUBSTRATE-DESIGN.md LAYER 1):
    the default was safe_cwd() — wherever the operator happened to stand, in
    practice the MAIN checkout — so every spawned seat edited, built and
    stashed the same tree. Dirty main blocks every land, seats collide, and
    `git stash` on a shared checkout is not swarm-safe. A default is a decision;
    this one is now owned. An explicit `--cwd` still wins.

    provision=False (a `--print` plan, or the pre-lock parse pass) resolves the
    path WITHOUT creating anything: a dry run must have no side effects, and the
    real provisioning belongs INSIDE the per-seat spawn lock so two concurrent
    spawns cannot race one `git worktree add`.

    Fails OPEN to `base` with a loud stderr note — a git/metaharness hiccup
    must degrade a spawn to the old shared-tree behaviour, never abort it.

    `base` is the reference directory: what to resolve the repo from and what to
    degrade to. It defaults to safe_cwd() (the SPAWN case — the operator's cwd is
    the only reference a brand-new seat has), and RESUME passes the seat's own
    last working directory instead, because degrading a resume to wherever the
    INTEGRATOR happens to be standing would move someone else's pane into my
    tree — a fallback must land where the caller's failure is survivable, not
    wherever the invoking process happens to sit."""
    from . import harness, seats
    here = base or seats.safe_cwd()
    root = harness.find_repo_root(here)
    if not root or not seat_name:
        return here      # outside a checkout there is nothing to isolate from
    if not provision:
        return harness.seat_worktree_path(root, seat_name)
    ad = harness.detect()
    ensure = getattr(ad, "ensure_home_worktree", None) if ad else None
    try:
        return ensure(seat_name, root) if ensure \
            else harness.ensure_home_worktree(seat_name, root)
    except harness.HarnessError as e:
        print("helm seat: WARN — %s home worktree unavailable (%s); falling "
              "back to the SHARED checkout %s (dirty-main collisions are back "
              "until it is fixed)" % (seat_name, e, here), file=sys.stderr)
        return here


def _resume_cwd(seat_name, sess_cwd):
    """Where a RESUMED pane lands. Follows the seat's own last working directory
    EXCEPT when that directory is a place no seat should be working.

    THE HOLE THIS CLOSES: slice 0 gave `spawn` a per-seat home worktree, but
    resume kept `cwd=sess_cwd or safe_cwd()` — the cwd SNIFFED from the seat's
    newest session. Every fleet seat here is a long-lived RESUME, not a fresh
    spawn, so a spawn-only default isolated almost nothing: resuming a seat that
    had been working in the shared checkout put it straight back into the shared
    checkout, which is the exact collision the home worktree exists to prevent.

    THE RULE IS NOT "resume always goes home", and getting that wrong would cost
    real continuity: a seat that was working in a LANE worktree must come back to
    it — that is its task, mid-flight. What is refused is a DEGRADED location:
    the shared main checkout (every seat's collision surface) and a throwaway
    temp dir. That is deliberately the same DOWNGRADE REFUSAL landed one layer up
    for roster rows, reusing the same `_is_temp_cwd` predicate rather than a
    second opinion about what "throwaway" means: never accept a degraded location
    when a real one is available.

    Fail-open in the direction that keeps a resume working: anything unreadable,
    outside a checkout, or already a real non-shared directory is kept as-is, and
    a home worktree that cannot be provisioned degrades to `sess_cwd` (the seat's
    OWN last tree), never to the invoking process's cwd."""
    from . import harness, seats
    if not sess_cwd or not seat_name:
        return sess_cwd or seats.safe_cwd()
    # ORDER MATTERS, and the first version of this got it wrong: the temp check
    # has to come BEFORE the repo lookup. A throwaway cwd is not inside any
    # checkout, so an early "outside a checkout, nothing to isolate" return sent
    # the seat straight back to /tmp — the exact bug being fixed (a live row read
    # cwd=/tmp). Caught by this function's own test, which is the only reason the
    # ordering is stated here rather than rediscovered later.
    if seats._is_temp_cwd(sess_cwd):
        # A temp dir names no repository, so the seat side carries no reference
        # to resolve a home from; the invoking operator's cwd is the only one
        # available. That is NOT the same as landing the pane in the invoker's
        # tree: the result is still the deterministic <repo>-wt/seats/<seat>.
        here = seats.safe_cwd()
        if not harness.find_repo_root(here):
            return sess_cwd      # no repo anywhere in reach — nothing better
        return _report_rehome(seat_name, sess_cwd,
                              _seat_home_cwd(seat_name, base=here),
                              "a throwaway temp dir")
    root = harness.find_repo_root(sess_cwd)
    if not root:
        return sess_cwd          # outside a checkout there is nothing to isolate
    if os.path.realpath(sess_cwd) != os.path.realpath(root):
        return sess_cwd          # a real lane room — continuity wins
    return _report_rehome(seat_name, sess_cwd,
                          _seat_home_cwd(seat_name, base=sess_cwd),
                          "the SHARED checkout every seat collides in")


def _stale_resume_cwd(path):
    """Why `path` must not receive a resumed pane, or None when it is usable.

    Two stale shapes, both measured putting seats in trees the operator could
    not find (row #155): a recorded cwd that NO LONGER EXISTS, and one that
    still exists but sits inside a REMOVED git worktree — its `.git` FILE
    names a gitdir that is gone (the worktree was pruned, or the checkout it
    hung off moved), so git verbs there fail while the directory itself looks
    fine. File reads only, no subprocess: this runs on every resume.

    Fail-open everywhere else (the `_resume_cwd` law): no path, an unreadable
    `.git`, a directory outside any checkout — none of that is EVIDENCE of
    staleness, and refusing on a guess would block real resumes."""
    if not path:
        return None
    if not os.path.isdir(path):
        return "it no longer exists"
    d = os.path.realpath(path)
    while True:
        g = os.path.join(d, ".git")
        if os.path.isdir(g):
            return None              # a real checkout root
        if os.path.isfile(g):
            try:
                with open(g, encoding="utf-8") as f:
                    raw = f.read()
            except OSError:
                return None          # unreadable is not evidence
            target = raw.partition("gitdir:")[2].strip().splitlines()
            target = target[0].strip() if target else ""
            if not target:
                return None
            if not os.path.isabs(target):
                target = os.path.join(d, target)
            if os.path.exists(target):
                return None
            return ("it sits in a REMOVED worktree (%s names gitdir %s, "
                    "which is gone)" % (g, target))
        parent = os.path.dirname(d)
        if parent == d:
            return None              # outside any checkout — nothing stale
        d = parent


def _report_rehome(seat_name, was, now, why):
    """Say it out loud when a resume moves a pane. A relocation the operator
    cannot see is indistinguishable from a bug, and this one changes which tree
    someone else's agent wakes up in."""
    if os.path.realpath(now) != os.path.realpath(was):
        print("  cwd: %s is %s — resuming %s into its own home worktree %s"
              % (was, why, seat_name, now))
    return now


def _spawn_args(rest, seat_name=None, provision=True):
    """Parse spawn's small option surface without letting a missing value raise
    IndexError or an unknown flag silently change the launch. The default cwd is
    the seat's OWN home worktree (`_seat_home_cwd` — the dirty-main collision
    cure), NOT safe_cwd()/the shared checkout; an explicit `--cwd` still wins.
    Resolution stays fail-open and tolerates None downstream (harness
    inherits). `provision` is False for the pre-lock and dry-run passes."""
    room, cwd, dry_run, replace = None, None, False, False
    i = 0
    while i < len(rest):
        arg = rest[i]
        if arg in ("--print", "--dry-run"):
            dry_run = True
            i += 1
            continue
        if arg == "--replace":
            replace = True
            i += 1
            continue
        if arg not in ("--room", "--cwd"):
            return None, "unknown option %s" % arg
        if i + 1 >= len(rest) or rest[i + 1].startswith("--"):
            return None, "%s wants a value" % arg
        value = rest[i + 1]
        if arg == "--room":
            room = value
        else:
            cwd = os.path.abspath(os.path.expanduser(value))
        i += 2
    if cwd is None:
        cwd = _seat_home_cwd(seat_name, provision=provision and not dry_run)
    return (room, cwd, dry_run, replace), None
