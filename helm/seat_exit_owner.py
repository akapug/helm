"""One owner for exact seat process/pane exit and runtime archival.

A positive terminal proof closes one runtime occurrence. It never deletes or
reassigns the durable Seat, Actor, label, key lineage, or obligations.
"""
import os
import re
import signal
import time

from . import pk

_ARCHIVE_DIR = "spawn.archive"
_SPAWN_V = 1
_TERMINAL_V = 1
_TERMINAL_PANE_STATES = ("disconnected", "closed", "gone", "exited", "dead")
_PANE_CLOSE_POLLS = 30
_PANE_CLOSE_DELAY = 0.1
_TERM_POLL_DELAY = 0.2


def pane_inventory_state(ad, handle):
    """One contradiction-free exact-handle inventory classification."""
    try:
        rows = ad.list()
    except Exception as e:
        return "unknown", "pane census is UNKNOWN: %s" % e
    matches = [item for item in rows if item.get("handle") == handle]
    if len(matches) > 1:
        return "unknown", "pane census has %d rows for exact handle %s" % (
            len(matches), handle)
    if not matches:
        return "absent", "exact handle %s is absent from %s" % (handle, ad.name)
    state = str(matches[0].get("status") or "").strip().lower()
    if state in _TERMINAL_PANE_STATES:
        return "terminal", "registered pane %s reported terminal state %s" % (
            handle, state)
    return "live", "registered pane %s still reports state %s" % (
        handle, state or "unknown")


def pane_terminal_proof(ad, handle, absent_proof=None):
    """Inventory plus exact-process proof for a required terminal result."""
    state, inventory = pane_inventory_state(ad, handle)
    if state in ("unknown", "live"):
        return None, inventory
    if ad.name != "orca":
        return inventory, None
    if absent_proof is None:
        return None, inventory + " but exact-session process census is unavailable"
    reason, unavailable = absent_proof()
    if unavailable:
        return None, unavailable
    if not reason:
        return None, inventory + " but exact-session process termination is unproven"
    return inventory + " and " + reason, None


def stop_pane(ad, handle, absent_proof=None):
    """Close one exact pane and boundedly prove its terminal completion."""
    ad.stop(handle)
    last = "terminal state remained unproven"
    for _ in range(_PANE_CLOSE_POLLS):
        reason, unavailable = pane_terminal_proof(
            ad, handle, absent_proof=absent_proof)
        if reason:
            return reason, None
        last = unavailable or last
        time.sleep(_PANE_CLOSE_DELAY)
    return None, last


def terminate_process(pid, alive, term_polls=15, kill_polls=10):
    """Signal one identity-bound PID; success requires explicit measured False."""
    os.kill(pid, signal.SIGTERM)
    state = True
    for _ in range(term_polls):
        state = alive()
        if state is not True:
            break
        time.sleep(_TERM_POLL_DELAY)
    if state is True:
        os.kill(pid, signal.SIGKILL)
        for _ in range(kill_polls):
            state = alive()
            if state is not True:
                break
            time.sleep(0.1)
    if state is False:
        return "process %s is absent after owned termination" % pid, None
    if state is None:
        return None, "process %s terminal state is UNKNOWN" % pid
    return None, "process %s survived SIGKILL" % pid


def archive_inventory(d):
    """(paths, unavailable); missing is empty, unreadable is UNKNOWN."""
    root = os.path.join(d, _ARCHIVE_DIR)
    try:
        names = os.listdir(root)
    except FileNotFoundError:
        return [], None
    except OSError as e:
        return [], str(e)
    paths = [os.path.join(root, name) for name in names
             if name.endswith(".json")]
    def order(path):
        match = re.fullmatch(r"(spawn\.closed-.+?)(?:-(\d+))?\.json",
                             os.path.basename(path))
        return (match.group(1), int(match.group(2) or 0)) if match else (path, 0)
    return sorted(paths, key=order), None


def archive_paths(d):
    """Every readable archived spawn transition, oldest name first."""
    return archive_inventory(d)[0]


def _spawn_defect(rec, seat_name=None):
    if not isinstance(rec, dict):
        return "spawn record is not an object"
    if rec.get("v") != _SPAWN_V:
        return "spawn record has unsupported version %r" % rec.get("v")
    if not rec.get("seat") or (seat_name is not None and rec.get("seat") != seat_name):
        return "spawn record has no matching seat identity"
    harness = rec.get("harness")
    if harness == "headless":
        pid = rec.get("pid")
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            return "headless spawn record has no positive pid"
        if not rec.get("pid_identity"):
            return "headless spawn record has no process birth identity"
        if rec.get("handle") or rec.get("session"):
            return "headless spawn record mixes pane/session identity"
    else:
        from . import harness as harness_owner
        if harness not in harness_owner.ADAPTERS:
            return "pane spawn record has unknown harness %r" % harness
        if not rec.get("handle"):
            return "pane spawn record has no exact handle"
        if not rec.get("session"):
            return "pane spawn record has no exact session"
        if rec.get("pid") or rec.get("pid_identity"):
            return "pane spawn record mixes headless process identity"
    return None


def latest_archived_exit(d, seat_name=None):
    """(record, unavailable) for one identity-bound terminal projection."""
    paths, unavailable = archive_inventory(d)
    if unavailable or not paths:
        return None, unavailable
    rec = pk.read_json(paths[-1], None)
    defect = _spawn_defect(rec)
    if defect:
        return None, "latest spawn archive is invalid: " + defect
    terminal = rec.get("terminal")
    if not isinstance(terminal, dict) or terminal.get("event") not in (
            "pane-closed", "process-closed"):
        return None, "latest spawn archive is unreadable or has no terminal event"
    if terminal.get("v") != _TERMINAL_V or not terminal.get("ts"):
        return None, "latest spawn archive has an invalid terminal witness"
    expected = ("process-closed" if rec.get("harness") == "headless"
                else "pane-closed")
    if terminal.get("event") != expected:
        return None, "latest spawn archive event contradicts its harness"
    for field in ("seat", "session", "harness", "handle", "pid",
                  "pid_identity", "room"):
        if rec.get(field) != terminal.get(field):
            return None, ("latest spawn archive has mismatched %s identity"
                          % field)
    record_seat = rec.get("seat")
    if not record_seat:
        return None, "latest spawn archive has no seat identity"
    if seat_name is not None and record_seat != seat_name:
        return None, "latest spawn archive belongs to seat %s, not %s" % (
            record_seat, seat_name)
    return rec, None


def has_archived_exit(d):
    paths, unavailable = archive_inventory(d)
    return bool(paths) or unavailable is not None


def _archive_path(d, stamp):
    root = os.path.join(d, _ARCHIVE_DIR)
    os.makedirs(root, mode=0o700, exist_ok=True)
    word = re.sub(r"[^A-Za-z0-9]+", "", str(stamp)) or "unknown"
    base = os.path.join(root, "spawn.closed-%s.json" % word)
    path, n = base, 1
    while os.path.exists(path):
        path = base[:-5] + "-%d.json" % n
        n += 1
    return path



def archive_spawn(seat_name, d, rec, reason):
    """Archive the exact active spawn record with one terminal transition."""
    path = os.path.join(d, "spawn.json")
    current = pk.read_json(path, None)
    if not isinstance(current, dict):
        return None, "active spawn record disappeared before exit archival"
    if current != rec or current.get("seat") != seat_name:
        return None, "spawn identity changed before exit archival"
    defect = _spawn_defect(current, seat_name)
    if defect:
        return None, defect
    stamp = pk.now_ts()
    event = {
        "v": _TERMINAL_V,
        "event": ("process-closed" if rec.get("harness") == "headless"
                  else "pane-closed"),
        "ts": stamp,
        "reason": str(reason),
        "seat": seat_name,
        "session": rec.get("session"),
        "room": rec.get("room"),
        "harness": rec.get("harness"),
        "handle": rec.get("handle"),
        "pid": rec.get("pid"),
        "pid_identity": rec.get("pid_identity"),
    }
    closed = dict(rec)
    closed["terminal"] = event
    try:
        pk.write_json(path, closed)
        archived = _archive_path(d, stamp)
        os.replace(path, archived)
    except OSError as e:
        return None, "spawn exit archival failed: %s" % e
    return {"archive": archived, "event": event}, None


def close_spawn(seat_name, d, rec, reason):
    """Archive one positively closed runtime occurrence; decide no identity policy."""
    closed, err = archive_spawn(seat_name, d, rec, reason)
    if err:
        return [], [err]
    event = closed["event"]
    return ["archived %s transition for %s at %s"
            % (event["event"], seat_name,
               os.path.basename(closed["archive"]))], []
