#!/usr/bin/env python3
"""helm tick alarm — a tick leg that keeps failing is heard (task/4189).

THE FAILURE THIS PREVENTS. The dark-seat mover raised AttributeError on every
idle-dispatch pass for three days: 944 journal lines of "dark-seat mover:
stopped (AttributeError ...)". Each leg catches its own
exception so the tick goes on, which is right; but the catch was the end of
the story. Nobody reads a timer's journal, and `helm doctor` reported the
mover's on/off switch ("automatic"), not whether it had worked once in three
days. A leg that never raises into its tick and never tells anyone is a leg
that can stay broken forever.

THE RULE. Every tick leg reports each pass here: `record(leg, error)`, with
`error` None for a pass that worked. One small state file keeps, per leg, the
last result (ok or not, the error text, when) and the count of failures in a
row. Then:

  3 failures in a row   ONE #seats row naming the leg, the error, the count,
                        and the leg's steward @mentioned (seatevents)
  1h after that row,    ONE owner push through notify.owner_push
  still failing
  a pass that works     re-arms both, so the next failing episode is heard
                        again; nothing is sent on recovery

An owed row or push (the room refused, the phone failed) is retried on the
next failing pass, never twice once delivered. `helm doctor`
(`check_tick_legs`) reads these results: a leg at the alarm count is a
doctor FAILURE, a leg that failed its last pass a WARN.

THE ALARM NEVER RAISES AND NEVER SPAMS. A leg calls this from inside its own
catch, so an alarm that raised would turn a contained failure into a crashed
tick. Every path is inside one try; a state file that cannot be read starts
fresh, one that cannot be written sends nothing and says so in the lines it
returns. The room row carries a stable event id, so a pass that dies between
the post and the state write does not post it twice.

THE SWITCH. HELM_TICK_ALARM=off (or 0, no) records nothing and sends
nothing, and `helm doctor` says the alarm is off. It is the production
switch; the test suite plants it (tests/__init__.py) so no arm's failing leg
counts toward another arm's alarm.

A WORKING PASS IS ONE FILE READ. A leg already recorded as working, less
than REFRESH_S ago, is not written again: the dark-family recheck runs every
minute and promises one file read while nothing is dark.

WIRED LEGS (`LEGS`): the idle-dispatch tick's own pass and the three legs
that ride it (darkmove, frictionpilot, officeweather) record from their own
catch; the timer entries of proxywatch (both units), gc, the auto-land train
and beacons are recorded at `helm`'s one door from one table
(`TIMER_ENTRIES`), so no verb carries its own wrapper.
"""
import fcntl
import os
import time

# NO HELM IMPORT AT MODULE SCOPE: `helm`'s one door (cli._main) imports this
# for every verb, hooks included, so it costs only the stdlib until a timer
# entry or a leg actually records.

NAME = "tick-legs.json"
V = 1
ALARM_AFTER = 3
PUSH_AFTER_S = 3600
ERROR_MAX = 300
REFRESH_S = 600
TITLE = "helm tick failing"
SWITCH_ENV = "HELM_TICK_ALARM"
_OFF = ("off", "0", "no", "false")
#: The seatevents component a leg's steward is declared under, unless LEGS
#: names another.
STEWARD = "helm-ticks"
#: The #seats room's machine sender (seatevents.WHO), named here so the
#: delivery-truth walk resolves this module's label.
WHO = "seat-events"

#: leg -> (what it is, the seatevents component that owns it).
LEGS = {
    "idle-dispatch": ("the idle-dispatch pass (`helm seat idle-dispatch`)",
                      STEWARD),
    "darkmove": ("the dark-seat mover (idle-dispatch tick)", STEWARD),
    "frictionpilot": ("the friction autopilot (idle-dispatch tick)",
                      "helm-friction"),
    "officeweather": ("the office weather (idle-dispatch tick)", STEWARD),
    "proxywatch": ("`helm proxywatch --post` (proxywatch timer)",
                   "credentials"),
    "proxywatch-dark": ("`helm proxywatch --dark-only --post` (dark-family "
                        "recheck timer)", "credentials"),
    "gc": ("`helm gc --apply` (gc timer)", STEWARD),
    "autoland": ("`helm train auto --apply` (auto-land timer)",
                 "build-lanes"),
    "beacons": ("`helm beacons --post` (beacons timer)", STEWARD),
}

#: The timer entries `helm`'s one door (cli._main) records, first match wins:
#: (verb, the leading words, the flags all present, leg, a failed exit code).
#: Exit 1 of proxywatch and beacons is a watch that RAN and found faults, the
#: watchdog working; exit 2 is the watchdog itself failing. gc always exits 0
#: and auto-land's 1 is a refusal it posts itself, so only a raise counts.
TIMER_ENTRIES = (
    ("proxywatch", (), ("--dark-only", "--post"), "proxywatch-dark", 2),
    ("proxywatch", (), ("--post",), "proxywatch", 2),
    ("gc", (), ("--apply",), "gc", None),
    ("train", ("auto",), ("--apply",), "autoland", None),
    ("beacons", (), ("--post",), "beacons", 2),
)
#: A tail carrying one of these is not the timer's pass.
NOT_A_PASS = ("--install-timer", "-h", "--help", "vendor-reset")


def timer_leg(verb, rest):
    """(leg, failed) for a timer entry's argv, else None."""
    rest = list(rest or ())
    if any(a in NOT_A_PASS for a in rest):
        return None
    for v, lead, flags, leg, bad in TIMER_ENTRIES:
        if v == verb and rest[:len(lead)] == list(lead) \
                and all(f in rest for f in flags):
            return leg, (None if bad is None else (lambda rc, b=bad: rc == b))
    return None


def switched_off():
    return (os.environ.get(SWITCH_ENV) or "").strip().lower() in _OFF


def state_path():
    from . import home
    return os.path.join(home.helm_home(), home.GLOBAL, ".state", NAME)


def _stamp(ts):
    return time.strftime("%Y-%m-%dT%H:%MZ", time.gmtime(ts))


def _what(leg):
    return LEGS.get(leg, ("an unlisted tick leg", STEWARD))


def _load(target):
    from . import pk
    got = pk.read_json(target, default={})
    legs = got.get("legs") if isinstance(got, dict) and got.get("v") == V \
        else None
    return {str(k): v for k, v in (legs or {}).items() if isinstance(v, dict)}


def results(path=None):
    """{leg: its last result}, read without the lock. Never raises."""
    try:
        return _load(path or state_path())
    except Exception:                        # noqa: BLE001 — unread is {}
        return {}


def _post(text, eid):
    from . import chat, seatevents
    chat.post(text, who=WHO, room=seatevents.ROOM, sign=False,
              event_id=eid)


def _push(body, title):
    from . import notify
    return notify.owner_push(body, title=title,
                             receipt=("tickalarm.notify_failed", title))


def _row(leg, rec):
    from . import seatevents
    what, component = _what(leg)
    return seatevents.row(component, (
        "tick leg `%s` (%s) has failed %d passes in a row since %s: %s — "
        "it alarms once per episode; `helm doctor` shows it until a pass "
        "works, and the owner's phone hears it if it still fails %dh after "
        "this row" % (leg, what, rec["failures"], _stamp(rec["since"]),
                      rec["error"], PUSH_AFTER_S // 3600)))


def _apply(rec, leg, error, now):
    """Fold one pass into the leg's record (in place)."""
    if error is None:
        rec.clear()
        rec.update(ok=True, error=None, ts=now, failures=0)
        return
    if rec.get("ok", True) or not rec.get("since"):
        rec.update(since=now, alarmed=None, pushed=None, failures=0)
    rec.update(ok=False, error=" ".join(str(error).split())[:ERROR_MAX],
               ts=now, failures=int(rec.get("failures") or 0) + 1)


def _deliver(rec, leg, now, post, push):
    """Send what this failing episode owes -> lines saying what was sent."""
    out = []
    if rec.get("ok", True) or rec["failures"] < ALARM_AFTER:
        return out
    if not rec.get("alarmed"):
        try:
            post(_row(leg, rec), "tickalarm:%s:%d" % (leg, rec["since"]))
            rec["alarmed"] = now
            out.append("tick alarm: %s posted to #seats (%d failures)"
                       % (leg, rec["failures"]))
        except Exception as exc:             # noqa: BLE001 — owed, retried
            return out + ["tick alarm: %s row owed (%s)" % (leg, exc)]
    if not rec.get("pushed") and now - rec["alarmed"] >= PUSH_AFTER_S:
        try:
            done = push("helm tick leg %s still failing %d passes since %s: "
                        "%s" % (leg, rec["failures"], _stamp(rec["since"]),
                                rec["error"]), TITLE)
        except Exception:                    # noqa: BLE001 — owed, retried
            done = False
        if done:
            rec["pushed"] = now
            out.append("tick alarm: %s pushed to the owner" % leg)
    return out


def record(leg, error=None, now=None, path=None, post=None, push=None):
    """Record one pass of `leg` (`error` None: it worked) and send what its
    failing episode owes -> lines naming anything sent or not recorded.
    NEVER RAISES: it is called from inside a leg's own catch."""
    if switched_off():
        return []
    try:
        now = time.time() if now is None else now
        target = path or state_path()
        if error is None:
            last = results(target).get(str(leg)) or {}
            if last.get("ok") and 0 <= now - float(last.get("ts") or 0) \
                    < REFRESH_S:
                return []
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target + ".lock", "a+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                try:
                    legs = _load(target)
                except Exception:            # noqa: BLE001 — start fresh
                    legs = {}
                rec = legs.setdefault(str(leg), {})
                _apply(rec, leg, error, now)
                from . import pk
                # THE CLAIM IS WRITTEN FIRST: a send never precedes the
                # record of the failure it reports.
                pk.write_json(target, {"v": V, "legs": legs})
                out = _deliver(rec, leg, now, post or _post, push or _push)
                if out:
                    pk.write_json(target, {"v": V, "legs": legs})
                return out
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    except Exception as exc:                 # noqa: BLE001 — never the leg
        return ["tick alarm: %s pass not recorded (%s: %s)"
                % (leg, exc.__class__.__name__, exc)]


def error_of(exc):
    return "%s: %s" % (exc.__class__.__name__, exc)


def watch(leg, fn, failed=None):
    """Run a timer entry `fn()` -> what it returns, recording the pass: an
    exception is a failure (recorded, then raised as before), and so is a
    result `failed(rc)` calls one (its text, if it returns a string, else
    "exit <rc>"). KeyboardInterrupt is not a failure."""
    try:
        rc = fn()
    except Exception as exc:
        record(leg, error_of(exc))
        raise
    why = failed(rc) if failed else None
    record(leg, (why if isinstance(why, str) else "exit %s" % rc)
           if why else None)
    return rc


def doctor_rows(path=None, now=None):
    """[(level, text)] for `helm doctor`: FAIL at the alarm count, WARN for a
    leg whose last pass failed, one OK line for the rest."""
    if switched_off():
        return [("WARN", "tick alarm: off (%s=off) — no tick leg records its "
                         "passes, so none that keeps failing is heard"
                 % SWITCH_ENV)]
    now = time.time() if now is None else now
    legs = results(path)
    rows, fine = [], []
    for leg, rec in sorted(legs.items()):
        if rec.get("ok", True):
            fine.append(leg)
            continue
        n = int(rec.get("failures") or 0)
        level = "FAIL" if n >= ALARM_AFTER else "WARN"
        rows.append((level, "tick leg %s (%s): FAILING, %d pass%s in a row "
                     "since %s, last %dm ago: %s" % (
                         leg, _what(leg)[0], n, "es"[:2 * (n != 1)],
                         _stamp(rec.get("since") or rec.get("ts") or 0),
                         max(0, now - float(rec.get("ts") or now)) // 60,
                         rec.get("error"))))
    if fine or not rows:
        rows.append(("OK", "tick legs: %s" % (
            "%d worked on their last pass (%s)" % (len(fine), ", ".join(fine))
            if fine else "no tick leg has recorded a pass yet")))
    return rows
