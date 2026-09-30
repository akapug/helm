"""WHICH HOST A LAND GATE RUNS ON: the one expected to FINISH first.

THE MEASURED DEFECT. Over 09-22..09-25 a land gate took a median 18.3 min on
the fast build host and 52.7 min on the slow one, and the owner's criterion is
that a land gate answers in under five minutes. Fab's own placement
(`fab-gate-job`'s history picker, task/3066) already prefers the host whose
recent gates of this repository and scope ran fastest, but it asks only which
host is fastest AND ADMITS the gate now: with the fast host at its whole-suite
cap it falls through to the slow one, however soon the fast one frees. So a
train waited 50 minutes on the slow host for a gate the fast host would have
answered in 17 plus the few minutes left on the gate it was running.

THE RULE. Each land gate goes to the host with the lowest EXPECTED FINISH:

    finish(host) = median(host) + remaining(the gate running there, if any)

where median(host) is the median execution time of that host's newest
HISTORY_RECENT green land gates of the same mode (serial or sliced), and
remaining is that median less the running gate's elapsed time, floored at
zero. A slower host is used only when it would finish sooner. When the best
host is busy the door does not stack a second gate there — one gate per host,
because a whole suite (and a sliced one at `--slots fit --cores 16`) saturates
the host it runs on, and stacked gates cost 13.7 h in one week of contention —
it WAITS: the launch is refused with the host, its expected finish and the
relaunch line.

WHERE THE MEDIANS COME FROM. The window's own job logs
(`_global/.state/gate-window/logs/<job>.log`): every land gate this door
dispatched ends its log with Fab's terminal gate-job event, which names the
node Fab placed it on (the same host atom routing pins), the execution seconds
Fab measured, the exit code, the scope (serial or sliced) and the receipt the
run minted. A receipt in the gate ledger names the machine by its kernel node
name, which is not the atom Fab routes by, so the ledger alone cannot say which
routing host a receipt came from; the log's event can.

WHAT IS CONFIG, AND WHAT IS NOT. No host is a literal here. The candidates are
HELM_GATE_HOSTS (an ordered list, the config order), else every host the logs
name, fastest first. FAB_EXCLUDE_HOSTS, the knob Fab's own placement honours,
removes hosts from routing too. While no candidate has a measured gate of
the request's mode (the first sliced land gates), each host's green gates of
ANY mode rank it instead: they still say which host is faster and by how much,
which is what waiting versus going elsewhere turns on. With no measured gate
at all the CONFIG ORDER decides: the first free host, else a wait on the
first. A host with no measured gate is never chosen over waiting on a measured
one, because nothing says it would finish sooner. And with no candidate known
at all — no config, no history — Fab places the gate as before, with every
busy host excluded.

HOW THE CHOICE REACHES FAB. `fab gate measure` takes no host, but every host
it admits passes `fab pick-build`, which refuses a host named in
FAB_EXCLUDE_HOSTS. So the door pins a host by excluding every other known
host on the measure and the submit it runs. A host Fab knows and helm does not
is not excluded by that; the door therefore reads the host Fab ANSWERED and
refuses to submit onto a host already running a gate, whatever routed it there.
"""
import json
import math
import os
import re

HOSTS_ENV = "HELM_GATE_HOSTS"
EXCLUDE_ENV = "FAB_EXCLUDE_HOSTS"
# THE KNOB ON WHAT ONE GATE AT A TIME IS KEYED BY (R1 of the landing
# refactor's refutation, accepted by the integrator). `host`, the DEFAULT, keys
# the one-gate rule by host alone: two rooms on one trunk head (a train and the
# same train less the car `helm train blame` ejected) run at once on two
# different hosts, never two on one host. `trunk` is the escape that keeps the
# landing-window rule: one whole suite per project trunk head, and one gate per
# host besides.
KEY_ENV = "HELM_GATE_WINDOW_KEY"
KEY_TRUNK = "trunk"
KEY_HOST = "host"

SERIAL = "serial"
SLICED = "sliced"
# Every green land gate of a host, whatever its mode: the relative speed of
# the hosts while no host has a measured gate of the request's own mode.
ANY = "any"
HISTORY_RECENT = 10
HISTORY_MIN_SAMPLES = 2
# The newest job logs read per decision. A log is one event line or two, so
# this bounds a launch's read at well under a megabyte.
HISTORY_SCAN = 512
_LOG_BYTES = 1 << 20
_ATOM = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,191}\Z")


def _env(environ):
    return os.environ if environ is None else environ


def window_key(environ=None):
    """KEY_TRUNK when HELM_GATE_WINDOW_KEY says `trunk`, else KEY_HOST: the
    default is one gate per host, and `trunk` is the named escape."""
    value = (_env(environ).get(KEY_ENV) or "").strip().lower()
    return KEY_TRUNK if value == KEY_TRUNK else KEY_HOST


def _atoms(raw):
    """(hosts in order, ignored tokens) of a space- or comma-separated list."""
    hosts, ignored = [], []
    for token in (raw or "").replace(",", " ").split():
        if not _ATOM.match(token):
            ignored.append(token)
        elif token not in hosts:
            hosts.append(token)
    return hosts, ignored


def configured(environ=None):
    """(HELM_GATE_HOSTS in its order, the tokens that are not host atoms)."""
    return _atoms(_env(environ).get(HOSTS_ENV))


def excluded(environ=None):
    """The hosts FAB_EXCLUDE_HOSTS names."""
    return set(_atoms(_env(environ).get(EXCLUDE_ENV))[0])


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _seconds(value):
    if type(value) not in (int, float):
        return None
    value = float(value)
    return value if math.isfinite(value) and value >= 0 else None


def _terminal(path):
    """The last gate-job event one job log holds, or None."""
    try:
        with open(path, "rb") as fh:
            blob = fh.read(_LOG_BYTES + 1)
    except OSError:
        return None
    if len(blob) > _LOG_BYTES:
        return None
    event = None
    for line in blob.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("event") == "gate-job":
            event = row
    return event


def sample(event, modes):
    """(host, mode, seconds) of one terminal event, or None.

    Only a GREEN land gate counts — COMPLETED with exit 0, as Fab's own
    history ranking counts — so a suite that died in its first minute is not
    read as a fast host. `modes` maps each land scope's canonical JSON to its
    mode."""
    if not isinstance(event, dict):
        return None
    snap = event.get("snapshot")
    ident = event.get("identity")
    host = event.get("node")
    if not isinstance(snap, dict) or not isinstance(ident, dict) \
            or not isinstance(host, str) or not _ATOM.match(host):
        return None
    if snap.get("state") != "COMPLETED" or type(snap.get("exit")) is not int \
            or snap["exit"] != 0:
        return None
    seconds = _seconds(snap.get("execution_elapsed_s"))
    mode = modes.get(_canonical(ident.get("scope")))
    if seconds is None or mode is None:
        return None
    return host, mode, seconds


def land_modes():
    """{canonical scope JSON: mode} for the two scopes a land gate runs."""
    from . import fabgate
    return {_canonical(fabgate.whole_scope()): SERIAL,
            _canonical(fabgate.slice_scope()): SLICED}


def history(logs, scan=HISTORY_SCAN):
    """{(host, mode): [seconds, newest first]} from the window's job logs,
    each gate counted under its own mode and under ANY."""
    try:
        names = [n for n in os.listdir(logs)
                 if n.startswith("gate-") and n.endswith(".log")]
    except OSError:
        return {}
    dated = []
    for name in names:
        path = os.path.join(logs, name)
        try:
            dated.append((os.path.getmtime(path), path))
        except OSError:
            continue
    dated.sort(reverse=True)
    modes, out = land_modes(), {}
    for _when, path in dated[:scan]:
        found = sample(_terminal(path), modes)
        if found:
            out.setdefault(found[:2], []).append(found[2])
            out.setdefault((found[0], ANY), []).append(found[2])
    return out


def median(values):
    ordered = sorted(values)
    mid = len(ordered) // 2
    return ordered[mid] if len(ordered) % 2 \
        else (ordered[mid - 1] + ordered[mid]) / 2


def estimate(samples, host, mode):
    """(median seconds, samples used) of the host's newest gates of this
    mode, or None below HISTORY_MIN_SAMPLES."""
    recent = samples.get((host, mode), [])[:HISTORY_RECENT]
    return (median(recent), len(recent)) \
        if len(recent) >= HISTORY_MIN_SAMPLES else None


def discovered(samples):
    """Every host the logs name, fastest first by its median over its newest
    green land gates of any mode, then by name."""
    per = {host: median(values[:HISTORY_RECENT])
           for (host, mode), values in samples.items() if mode == ANY}
    return sorted(per, key=lambda h: (per[h], h))


def _minutes(seconds):
    return "%.1f min" % (seconds / 60.0)


def _elapsed(row, now):
    """Seconds the row's gate has run; 0 for a launch time nobody can read,
    so its whole median is still ahead of it (gatewindow._row_ts's reading)."""
    ts = row.get("ts")
    if type(ts) not in (int, float):
        return 0.0
    try:
        launched = float(ts)
    except OverflowError:
        return 0.0
    return max(0.0, now - launched) if math.isfinite(launched) else 0.0


def plan(mode, live, logs, environ=None, now=0.0, unread_hosts=(),
         failed=()):
    """The route for one land gate of `mode` given the `live` window rows.

    -> {"host": the host to pin, or None to let Fab place;
        "wait": the live row to wait on, or None;
        "exclude": FAB_EXCLUDE_HOSTS for the measure and the submit;
        "why": one line; "lines": the table the door prints}

    `unread_hosts` (whose node could not be read) and `failed` hosts (Fab
    could not place a gate there this launch) are neither waited on nor
    routed to: a host nobody can reach is down as far as a land gate goes."""
    samples = history(logs)
    order, ignored = configured(environ)
    shut = excluded(environ)
    down = set(unread_hosts) | set(failed)
    busy = {}
    for row in live:
        if row.get("host"):
            busy.setdefault(row["host"], []).append(row)
    lines = ["  ignored in %s: %s (not a host name)" % (HOSTS_ENV, t)
             for t in ignored]
    known = order or discovered(samples)
    if not known:
        exclude = sorted(set(busy) | shut | down)
        return {"host": None, "wait": None, "exclude": exclude,
                "lines": lines,
                "why": "no host is configured (%s) or measured, so Fab places "
                       "it%s" % (HOSTS_ENV, ", excluding busy %s"
                                 % " ".join(sorted(busy)) if busy else "")}
    known = known + [h for h in sorted(busy) if h not in known]
    cands = [h for h in known if h not in shut and h not in down]
    est = {h: estimate(samples, h, mode) for h in cands}
    basis = mode
    if not any(est.values()):
        # NO HOST HAS A GATE OF THIS MODE YET (the first sliced lands): the
        # hosts' gates of any mode still say which is faster and by how much,
        # which is what a choice between waiting and going elsewhere turns on.
        est = {h: estimate(samples, h, ANY) for h in cands}
        basis = "%s (no %s gate measured yet)" % (ANY, mode)

    def remaining(host):
        left = 0.0
        for row in busy.get(host, ()):
            own = estimate(samples, host, row.get("mode") or SERIAL) \
                or est.get(host)
            if own:
                left = max(left, own[0] - _elapsed(row, now))
        return left

    finish = {h: est[h][0] + remaining(h) for h in cands if est[h]}
    for host in known:
        if host in shut:
            lines.append("  %-12s excluded (%s)" % (host, EXCLUDE_ENV))
        elif host in down:
            lines.append("  %-12s not routed to: %s" % (
                host, "Fab could not place a gate there" if host in failed
                else "its node could not be read"))
        elif est[host] is None:
            lines.append("  %-12s no %s gate measured here (%s)" % (
                host, mode, "busy" if host in busy else "free"))
        else:
            lines.append("  %-12s median %s over %d %s gate(s), %s -> "
                         "finishes in ~%s" % (
                             host, _minutes(est[host][0]), est[host][1], basis,
                             "busy, ~%s left" % _minutes(remaining(host))
                             if host in busy else "free",
                             _minutes(finish[host])))
    if not cands:
        return {"host": None, "wait": None, "lines": lines,
                "exclude": sorted(set(busy) | shut | down),
                "why": "every known host is excluded or down, so Fab places "
                       "it"}
    if finish:
        best = min(finish, key=lambda h: (finish[h], cands.index(h)))
        why = "expected to finish first (~%s)" % _minutes(finish[best])
    else:
        free = [h for h in cands if h not in busy]
        best = free[0] if free else cands[0]
        why = "no %s gate is measured on any host, so the config order " \
            "decides%s" % (mode, "" if free else ", and every host is busy")
    pinned = sorted((set(known) | shut | down) - {best})
    if best in busy:
        return {"host": None, "wait": busy[best][0], "exclude": pinned,
                "lines": lines, "why": "%s is busy and %s%s" % (
                    best, "still " if finish else "", why)}
    return {"host": best, "wait": None, "exclude": pinned, "lines": lines,
            "why": why}


def occupant(host, live):
    """The live row already running on `host`, or None."""
    return next((row for row in live if host and row.get("host") == host),
                None)
