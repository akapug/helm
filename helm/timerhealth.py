#!/usr/bin/env python3
"""ENABLED AND RUNNING ARE TWO FACTS SHARING ONE WORD IN THE SURFACE EVERYONE
CHECKS, and the word says yes for a timer that stopped.

A TIMER CAN STOP BEING SCHEDULED WITHOUT FAILING. The service reports success,
restarts stay at zero, and the unit simply stops firing -- while
`systemctl --user is-enabled` keeps answering "enabled", which is the field an
operator reaches for first. In `list-timers` such a unit renders as a row with
`-` where a live one shows a time, which reads as a formatting artifact rather
than an alarm. Two fleet watchdogs, one of them the stranded-obligation
detector, sat in exactly that state for nineteen hours and nothing noticed.

PER-TIMER RUNGS ARE THE DEFECT, NOT THE COVERAGE. A bespoke check per unit --
one for chat-logflush, one for tasks-mirror, one for stale-bot, each asking its
own question its own way -- leaves every timer added afterwards with no
coverage at all, and nobody notices, because the absence of a check looks
exactly like a check that passes. This asks ONE question of EVERY user
timer instead, so a new timer is covered by existing.

THE TWO FINDINGS ARE DIFFERENT FAILURES AND ARE KEPT APART:
  TRAP           the unit file says enabled and the unit is NOT active. This is
                 the nineteen-hour shape and the only state that actively lies.
  NEVER-FIRES    the unit IS active and has no next elapse at all. Armed,
                 scheduled for never; healthy to every check that stops at
                 is-active.
A timer that is disabled AND inactive is OFF, which is a decision somebody made
and not a finding -- reporting it would bury the two above in noise.

THE TIMER OWNS ITS RUNNING STATE. Timer SubState distinguishes waiting,
running and elapsed directly. The paired service cannot substitute: a target
may have been started independently and remain active while its timer is
elapsed. The service is consulted only after the timer says elapsed, where its
Result separates completed one-shot work from failed work.

TWO QUESTIONS STAY SEPARATE. First: did the timer or its triggered service
fail? Second, only for a healthy elapsed timer: does its own schedule still
promise an occurrence? Calendar truth is delegated to systemd-analyze, the
parser that owns calendar syntax; hand-classifying commas, ranges and
wildcards cannot distinguish a bounded set with occurrences left from one
whose final date passed.

WHAT THE SERVICE READ COSTS, AND WHY IT IS ASKED IN BULK. One `show` per unit
turns a 50-timer box into 81 subprocesses, each carrying its own timeout, so a
rung meant to take a second can hold the whole doctor for minutes. Both reads
are BULK: one `show` describing every timer, then ONE more describing only the
services of elapsed timers whose result can still change the answer.

AND THE PAIRED SERVICE IS NAMED BY THE TIMER, NOT GUESSED FROM ITS FILENAME.
A timer may point `Unit=` at any service; probing <name>.service assumes the
default and reports NEVER-FIRES about a unit it never looked at.
"""
import os
import subprocess

HEALTHY = "healthy"
TRAP = "trap"            # enabled, not active: the state that lies
NEVER = "never-fires"    # active, no next elapse, service not running
RUNNING = "running"      # timer SubState says its trigger is running
WAITING = "waiting"      # event-only timer is armed without a clock elapse
OFF = "off"              # explicitly disabled/masked and inactive
SPENT = "spent"          # timer SubState says its schedule is exhausted
FAILED = "failed"        # the timer or its triggered service failed
UNKNOWN = "unknown"      # could not be read; never guessed at

_NO_ELAPSE = ("", "infinity", "n/a")

# UnitFileState IS AN ENUMERATION AND WE ONLY CLASSIFY WHAT WE RECOGNISE.
# `enabled-runtime` is enabled — a runtime symlink is still somebody saying
# yes — and reading it as OFF hides the exact intent this rung exists to
# check. Anything outside both lists is a word systemd added since, so it
# yields UNKNOWN rather than a guess dressed as a decision.
_ENABLED_FILE_STATES = ("enabled", "enabled-runtime")
_OFF_FILE_STATES = ("disabled", "masked", "masked-runtime")

_NEVER_TRIGGERED = ("", "n/a", "0")
_EVENT_TRIGGERS = ("OnClockChange", "OnTimezoneChange")

_TIMER_PROPS = ("Id", "Names", "Unit", "ActiveState", "SubState",
                "UnitFileState", "NextElapseUSecRealtime",
                "NextElapseUSecMonotonic", "TimersCalendar",
                "TimersMonotonic", "LastTriggerUSec", "OnClockChange",
                "OnTimezoneChange")
_SERVICE_PROPS = ("Id", "Names", "LoadState", "ActiveState", "SubState",
                  "Result")


def _systemctl(argv, timeout=10):
    """-> (rc, stdout). rc is None when the call could not be made at all.

    NONE AND NONZERO ARE DIFFERENT FACTS. A missing systemd, a missing binary
    and a refused call all mean nothing was measured, and folding them in with
    a command that RAN and answered would let "no timers here" mean both "this
    box has none" and "this box could not be asked".
    """
    try:
        p = subprocess.run(["systemctl", "--user"] + list(argv),
                           capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None, ""
    return p.returncode, p.stdout or ""


def _show_many(units, run, props):
    """-> {requested unit: fields}, preserving valid blocks on partial failure.

    `show` names a canonical Id even when the request used an alias. Names binds
    that returned block back to the requested population. A mixed request may
    also print valid blocks and exit nonzero for one unreadable unit; those
    blocks remain evidence and only the missing unit becomes UNKNOWN.
    """
    if not units:
        return {}
    argv = ["show"] + list(units)
    for prop in props:
        argv += ["-p", prop]
    rc, out = run(argv)
    if rc is None:
        return None
    blocks = []
    block = {}
    for line in out.split("\n") + [""]:
        line = line.strip()
        if not line:
            if block.get("Id"):
                blocks.append(block)
            block = {}
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        # `show` PRINTS ONE LINE PER SCHEDULE ENTRY, so a timer with two
        # bases arrives as two TimersMonotonic lines; overwriting keeps
        # only the last and can lose the recurring one.
        block[key] = block[key] + "\n" + value if key in block else value
    if rc != 0 and not blocks:
        return None
    requested = set(units)
    described = {}
    for block in blocks:
        names = set(block.get("Names", "").split())
        names.add(block["Id"])
        for name in requested.intersection(names):
            described[name] = block
    return described


def _no_elapse(fields):
    """Is this timer scheduled for nothing, by either clock?"""
    return all(fields.get(k, "").strip().lower() in _NO_ELAPSE
               for k in ("NextElapseUSecRealtime", "NextElapseUSecMonotonic"))


def _entries(fields):
    """Every schedule line the timer declares, calendar and monotonic alike.

    `show` prints ONE LINE PER ENTRY, so a timer with two bases arrives as two
    lines under one key and both must survive to be asked about.
    """
    out = []
    for key in ("TimersCalendar", "TimersMonotonic"):
        for line in fields.get(key, "").split("\n"):
            if line.strip():
                out.append(line.strip())
    return out


def _calendar_expression(entry):
    _head, sep, tail = entry.partition("OnCalendar=")
    return tail.split(";")[0].strip(" }").strip() if sep else ""


def _calendar_future(expression):
    """Ask systemd's calendar parser whether an occurrence remains."""
    try:
        p = subprocess.run(
            ["systemd-analyze", "calendar", "--iterations=1", expression],
            capture_output=True, text=True, timeout=10,
            env=dict(os.environ, LC_ALL="C"))
    except (OSError, subprocess.SubprocessError):
        return None
    if p.returncode != 0:
        return None
    for line in p.stdout.splitlines():
        label, sep, value = line.partition(":")
        if sep and label.strip().lower() == "next elapse":
            return value.strip().lower() != "never"
    return None


def _promises_an_elapse(fields, calendar):
    """Does the timer's own schedule still promise another trigger?"""
    unknown = False
    for entry in fields.get("TimersMonotonic", "").split("\n"):
        entry = entry.strip()
        if not entry:
            continue
        _head, _, tail = entry.partition("next_elapse=")
        if tail and tail.strip(" }").strip().lower() not in _NO_ELAPSE:
            return True
        if "OnUnitActiveUSec" in entry or "OnUnitInactiveUSec" in entry:
            return True
    for entry in fields.get("TimersCalendar", "").split("\n"):
        entry = entry.strip()
        if not entry:
            continue
        _head, _, tail = entry.partition("next_elapse=")
        if tail and tail.strip(" }").strip().lower() not in _NO_ELAPSE:
            return True
        expression = _calendar_expression(entry)
        future = calendar(expression) if expression else None
        if future:
            return True
        unknown = unknown or future is None
    return None if unknown else False


def _event_only(fields):
    return not _entries(fields) and any(
        fields.get(key, "").strip().lower() == "yes"
        for key in _EVENT_TRIGGERS)


def _has_fired(fields):
    return fields.get("LastTriggerUSec", "").strip().lower() \
        not in _NEVER_TRIGGERED


def _paired_name(unit):
    return unit[:-len(".timer")] + ".service"


def _service_failed(fields):
    if not fields:
        return False
    result = fields.get("Result", "").strip().lower()
    return fields.get("ActiveState") == "failed" \
        or result not in ("", "success")


def _verdict(fields, service=None, calendar=None):
    """Classify only facts positively established by systemd's own states."""
    if fields is None:
        return UNKNOWN
    active = fields.get("ActiveState", "")
    if not active:
        return UNKNOWN
    if active == "failed" or fields.get("SubState") == "failed":
        return FAILED
    if active != "active":
        enabled = fields.get("UnitFileState", "")
        if enabled in _ENABLED_FILE_STATES:
            return TRAP
        if enabled in _OFF_FILE_STATES:
            return OFF
        return UNKNOWN
    state = fields.get("SubState", "")
    if state == "running":
        return RUNNING
    if state == "waiting":
        if not _no_elapse(fields):
            return HEALTHY
        if _event_only(fields):
            return WAITING
        promise = _promises_an_elapse(fields, calendar or _calendar_future)
        return NEVER if promise else UNKNOWN
    if state != "elapsed" or service is None:
        return UNKNOWN
    if service.get("LoadState") != "loaded":
        return UNKNOWN
    if _service_failed(service):
        return FAILED
    if service.get("ActiveState") != "inactive":
        return UNKNOWN
    promise = _promises_an_elapse(fields, calendar or _calendar_future)
    if promise is None:
        return UNKNOWN
    if promise or not _has_fired(fields):
        return NEVER
    return SPENT if service.get("Result") == "success" else UNKNOWN


def census(run=None, calendar=None):
    """-> (rows, err). rows are (unit, verdict, active, file_state).

    `err` is a sentence when the census could NOT be taken, and rows is then
    empty -- an empty list with no err means "asked, and every timer is fine",
    which is a different claim and must be able to be made separately.

    TWO BULK READS, NEVER N+1: one `show` for every timer, then one more for
    the services of the only units a service state can still reclassify.
    """
    run = run or _systemctl
    rc, out = run(["list-unit-files", "--type=timer", "--no-legend"])
    if rc is None:
        return [], "systemctl could not be run — timer health is UNMEASURED"
    if rc != 0:
        return [], "systemctl refused the unit-file listing (rc %s) — " \
                   "timer health is UNMEASURED" % rc
    units = []
    for line in out.split("\n"):
        name = line.split()[0] if line.split() else ""
        if name.endswith(".timer"):
            units.append(name)
    if not units:
        return [], None
    # An uninstantiated template is a unit-file definition, not a runtime unit;
    # asking systemctl to show it returns rc1 and must not poison valid peers.
    query = [unit for unit in units if "@." not in unit]
    described = _show_many(query, run, _TIMER_PROPS)
    if described is None:
        return [], "systemctl refused to describe the timers — " \
                   "timer health is UNMEASURED"
    # Only an elapsed timer needs its target result to distinguish completed
    # one-shot work from failed work or a schedule that still promises a run.
    targets = {}
    for unit in units:
        fields = described.get(unit)
        if fields and fields.get("ActiveState") == "active" \
                and fields.get("SubState") == "elapsed":
            targets[unit] = fields.get("Unit") or _paired_name(unit)
    services = _show_many(sorted(set(targets.values())), run, _SERVICE_PROPS)
    rows = []
    for unit in units:
        fields = described.get(unit)
        service = None
        if unit in targets and services is not None:
            service = services.get(targets[unit])
        rows.append((unit, _verdict(fields, service, calendar),
                     (fields or {}).get("ActiveState", ""),
                     (fields or {}).get("UnitFileState", "")))
    return rows, None


def findings(rows):
    """The bad states, in the order an operator should read them."""
    return ([r for r in rows if r[1] == TRAP]
            + [r for r in rows if r[1] == FAILED]
            + [r for r in rows if r[1] == NEVER])
