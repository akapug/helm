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

THE INSTALLED TEXT IS A THIRD FACT, and neither question above asks it. A unit
that elapses and fires can still run what an older template said: see `drift`
below (task/3405). And a value an installer copies into its unit is written
through `env_assignment`, because systemd does not read an Environment= line
as the KEY=VALUE text it looks like (task/3423).
"""
import collections
import os
import re
import shlex
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
    box has none" and "this box could not be asked". So does an answer that
    is not UTF-8 (a UnicodeDecodeError is a ValueError): a reader that fails
    closed on it must get None, never a traceback.

    UTF-8 WHATEVER THE LOCALE (task/3423). systemctl prints raw UTF-8 under
    any locale (measured under LC_ALL=C), so the locale's encoding misread
    it: Latin-1 turned an e-acute into two characters, a value the peer's
    unit does not have, and ASCII read an answer as a systemctl that could
    not be run. Every other answer read here is ASCII, which UTF-8 reads the
    same.
    """
    try:
        p = subprocess.run(["systemctl", "--user"] + list(argv),
                           capture_output=True, encoding="utf-8",
                           timeout=timeout)
    except (OSError, ValueError, subprocess.SubprocessError):
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


# ------------------------------------------------------------ the installer
#
# ONE UNIT DIRECTORY AND ONE INSTALLER FOR EVERY HELM USER TIMER (task/3307).
# Nineteen modules spelled ~/.config/systemd/user for themselves and seventeen
# ran their own copy of write, `daemon-reload`, `enable --now`, so task/3254's
# timeouts were fixed one copy at a time and task/3306 had no one directory to
# fence. What stays in each installer is what differs between them, because
# that is its contract: the off-switch it asks first, the systemctl it
# resolves, the words it reports, whether an unchanged install is a no-op,
# and how it cleans a failure's output. Seventeen call install_user_timer; chat's
# log-flush installer keeps its own loop, because it prints each failure as it
# happens and still runs the enable after a failed reload.

UNIT_DIR_ENV = "HELM_USER_UNIT_DIR"


def user_unit_dir(home=None):
    """The directory helm writes the user's systemd units into and reads them
    back from: $HELM_USER_UNIT_DIR when set, else ~/.config/systemd/user.
    `home` names another home to read (a census of a fixture estate), whose
    own .config/systemd/user answers whatever the variable says.

    EVERY INSTALLER AND EVERY READER ASKS HERE, so one setting moves them all
    together, and a directory nobody could redirect is not bound into
    nineteen modules any more. The variable outranks HOME, which is why the
    test suite removes an inherited value (tests/__init__.py) and
    tests._tmphome.fake_user_systemd sets it beside the temp HOME it makes."""
    if home:
        return os.path.join(home, ".config", "systemd", "user")
    return os.environ.get(UNIT_DIR_ENV) or os.path.join(
        os.path.expanduser("~"), ".config", "systemd", "user")


def _read_unit(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return None


def _failure_output(result):
    return (result.stderr or result.stdout or "").strip()[:200]


def install_user_timer(units, timers, systemctl="systemctl",
                       subprocess=subprocess, timeout=30,
                       keep_unchanged=False, reload_unchanged=True,
                       clean=_failure_output,
                       failed="%(cmd)s failed: %(detail)s"):
    """Write `units`, reload the user manager, enable `timers`.
    -> (error, unchanged).

    `units` are (path, text) pairs, written in order through pk.atomic_write;
    every name in `timers` is enabled by ONE `enable --now`, after ONE
    `daemon-reload`, and the first step that fails ends the install. `error`
    is None once the timers are enabled, else the sentence the caller
    reports: "unit write failed: ..." or `failed` filled with the command and
    its detail -- the exception, or `clean(result)` of a nonzero exit.

    IDEMPOTENT ONLY WHERE THE CALLER WAS. Under `keep_unchanged` a unit that
    already holds its text is not rewritten, since a rewrite still moves its
    mtime, and `unchanged` says every unit did; the reload then runs only if
    `reload_unchanged`. Without it every unit is written and `unchanged` is
    False.

    `subprocess` IS THE CALLER'S OWN, never this module's: a test stubs the
    module it is testing (tests/test_gate_sliced_land.py replaces
    gatecanary.subprocess), and an installer that ran another module's copy
    would walk past that stub to a real systemctl (task/3254's review,
    finding 3). A spawn that raises OSError is reported like a timeout,
    because a raised exception is not the (ok, detail) every caller returns.
    """
    from . import pk
    unchanged = keep_unchanged and all(
        _read_unit(path) == text for path, text in units)
    try:
        if not unchanged:
            for path, text in units:
                pk.atomic_write(path, text)
    except OSError as exc:
        return "unit write failed: %s" % exc, unchanged
    steps = [] if unchanged and not reload_unchanged \
        else [[systemctl, "--user", "daemon-reload"]]
    steps.append([systemctl, "--user", "enable", "--now"] + list(timers))
    for cmd in steps:
        try:
            # systemctl's own words, UTF-8 whatever the locale (task/3423),
            # a byte that is not replaced: they are relayed for reading.
            result = subprocess.run(cmd, capture_output=True,
                                    encoding="utf-8", errors="replace",
                                    timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            detail = exc
        else:
            if result.returncode == 0:
                continue
            detail = clean(result)
        return failed % {"cmd": " ".join(cmd), "detail": detail}, unchanged
    return None, unchanged


# ------------------------------------------------- Environment= assignments
#
# AN Environment= LINE IS NOT KEY=VALUE TEXT (task/3423). systemd.exec(5)
# unquotes each line per systemd.syntax(7) "Quoting" into WHITESPACE-SEPARATED
# words, reads C escapes in and out of quotes, and then expands "%" specifiers
# (systemd.unit(5)). Measured on systemd 259 (`systemd --test --user` prints
# what it parsed): `Environment=K=/a b` sets K=/a and ignores "b"; a quote or
# a backslash in an unquoted word STOPS the line at that word, so the words
# before it are set and it and the rest are not (the log's "Invalid syntax,
# ignoring" quotes the whole line all the same); `K=%h` sets the home
# directory. Every installer wrote its values raw before this section, so a
# claude path with a space set the path's first word and a knob with a quote
# set nothing.
#
# env_assignment is the one renderer. A value with no space, quote, backslash,
# percent sign or control character is written K=V exactly as before, so no
# existing install churns. Any other has "%" doubled and the whole assignment
# double-quoted with backslash and double quote escaped, which systemd 259
# reads back exactly. A control character is refused: systemd 259 does set one
# that a quoted C escape spells (measured, "\t" and "\x01"), but this renderer
# writes no escapes, and a raw newline would end the directive and begin
# another.
#
# NOTHING IN helm READS AN Environment= LINE AS systemd DOES. The chat node's
# posture mirror did, twice (shlex, then a copy of the grammar above), and
# each copy granted what systemd did not (a codex gap check, F1-F5): it now
# reads the running peer's own environment (chatnode.running_posture), and
# no unit text at all (task/3432).

_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_ENV_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_ENV_QUOTED = " \"'\\"
# What systemd strips from the ends of a unit file line, a key and a value
# (its WHITESPACE; measured on 259: an NBSP or a form feed ending a line is
# part of the value). Python's str.strip takes both, and more. The census
# strips it from each directive it reads (_directives), and remote_relay from
# each knob it writes, so an install and its census agree.
_SYSTEMD_SPACE = " \t\n\r"


def env_word(text, special=_ENV_QUOTED):
    """`text` as ONE Environment= word: unchanged when it holds none of
    `special` (by default space, quote and backslash), else double-quoted with
    backslash and double quote escaped. A "%" in it stays a specifier, which
    is the caller's to double. None when it holds a control character (see
    this section's header)."""
    if _ENV_CONTROL.search(text):
        return None
    if not any(c in special for c in text):
        return text
    return '"%s"' % text.replace("\\", "\\\\").replace('"', '\\"')


def env_assignment(name, value):
    """The Environment= word that sets `name` to exactly `value`: its "%"
    doubled so systemd expands no specifier, and quoted whenever it holds a
    space, quote, backslash or percent sign, so an UNQUOTED knob line is
    always one whose value is its raw text, whichever installer wrote it.
    None when no word can (a name systemd refuses, or a control character)."""
    if not _ENV_NAME.match(name):
        return None
    return env_word("%s=%s" % (name, value.replace("%", "%%")),
                    _ENV_QUOTED + "%")


# ------------------------------------ a string array as `systemctl show` prints
#
# `systemctl show -p P` PRINTS A STRING-ARRAY PROPERTY (Environment=,
# UnsetEnvironment=) as "P=" and each string through shell_maybe_quote, one
# space between (systemd 259: src/shared/bus-print-properties.c and
# src/basic/escape.c; measured by calling libsystemd-shared-259's own
# shell_maybe_quote, which systemctl links, and on this host's system
# manager). A string holding no whitespace, no control character and none of
# !"$&'()*;<>?[\`| is printed as it is, multi-byte characters (NBSP, U+2028)
# included. Any other is double-quoted, with backslash, double quote, backtick
# and dollar backslash-escaped and each control character C-escaped (\t, or
# \037 in octal), so the property never spans a line. The value it prints is
# systemd's own: after unquoting, escapes and specifiers (`100%%` shows 100%).

_SHOW_WORD = (r'[^\x00-\x20\x7f!"$&\'()*;<>?\[\\`|]+'
              r'|"(?:[^\x00-\x1f\x7f"\\`$]|\\(?:[\\"`$abfnrtv]|0[0-3][0-7]|177))*"')
_SHOW_ARRAY = re.compile(r"(?:%s)(?: (?:%s))*" % (_SHOW_WORD, _SHOW_WORD))
_SHOW_ESCAPE = re.compile(r"\\(0[0-3][0-7]|177|.)")
_SHOW_ESCAPES = {"a": "\a", "b": "\b", "f": "\f", "n": "\n", "r": "\r",
                 "t": "\t", "v": "\v"}


def show_strings(text):
    """The strings of one string-array property, from `text`, what `systemctl
    show` printed after its "=". None when `text` is not that output: a
    reader that cannot tell the strings apart must not guess at them."""
    if not text:
        return []
    if not _SHOW_ARRAY.fullmatch(text):
        return None
    out = []
    for word in re.finditer(_SHOW_WORD, text):
        word = word.group()
        if word[:1] == '"':
            word = _SHOW_ESCAPE.sub(lambda m: chr(int(m.group(1), 8))
                                    if len(m.group(1)) == 3 else
                                    _SHOW_ESCAPES.get(m.group(1), m.group(1)),
                                    word[1:-1])
        out.append(word)
    return out


# ------------------------------------------------------------- unit drift
#
# A TIMER THAT FIRES CAN STILL RUN WHAT AN OLDER TEMPLATE SAID (task/3405).
# helm-release-nightly.service was installed before its template gained
# Environment=HELM_RELEASE_NIGHTLY_SOURCE=timer, so every timer night recorded
# as MANUAL and the release streak could never count -- while the census above
# read the unit healthy, because it elapsed and it fired. Every module below
# renders its units from a template that changes across lands, and until this
# section nothing compared the installed text with the current template.
#
# THE COMPARISON IS AGAINST THE MODULE'S OWN RENDER, not a copy of it: the
# census calls the same function the installer calls (UNIT_TEMPLATES names
# it), with each input that legitimately varies per install replaced by a
# WILDCARD. Those inputs are the helm binary path and the checkout (both follow
# the installing home and checkout), the env-file path (it follows the config
# home), an `--interval` the operator chose, and the environment knobs an
# installer copies into its unit from its own environment (a BLOCK: zero or
# more whole `Environment=` lines). Every other input -- a calendar time, a
# fixed cadence, a catalog-derived calendar, a description -- is rendered with
# the value the code holds now, because a re-install would write that value,
# so a unit holding an older one is drift. The build host of `--host` is not
# an input of the text at all: it lives in the env file the unit names.
#
# THE MATCH IS PER DIRECTIVE, NOT PER BYTE. Comments and blank lines are
# dropped (systemd ignores them, and a comment edit is not a reason to
# re-install), and directives are grouped by section and key, compared in
# order WITHIN a key (ExecStart lines run in order; Environment lines override
# in order) but not across keys, whose order systemd does not read.
#
# A KNOB LINE IS READ IN BOTH FORMS an installer wrote (task/3423): raw, as
# every one did before env_assignment, and quoted, as env_assignment writes a
# value that needs it. A line is clean when env_assignment writes it again for
# the value its installer was given, so an old unit whose values need no
# quoting stays CLEAN, and one whose value systemd split, dropped or expanded
# is DRIFTED, its re-install line carrying that value. Each line is read as
# systemd reads it, a trailing space, tab or carriage return stripped
# (_directives), so an unquoted line is judged by the value systemd sets.
#
# THREE VERDICTS, and each is claimed only on its own evidence:
#   CLEAN    one assignment of the wildcards renders exactly the installed
#            directives, and it is consistent across every line that repeats
#            an input. A correct install is always CLEAN, whatever home,
#            checkout or interval it used, because its own inputs are one.
#   DRIFTED  PROVEN for every value of every input: a directive group the
#            template renders that no installed group matches under ANY
#            wildcard values, or an installed directive no template line can
#            produce. The proof never uses this process's home or checkout.
#   UNKNOWN  it could not be compared: the unit cannot be read, the template
#            could not be rendered, or every line matches but the installer's
#            inputs cannot be recovered because two lines disagree about one
#            (the checkout reads /a in one line and /b in another). Never
#            clean, never drifted.
# A module none of whose units is on disk is ABSENT: not installed here, which
# is somebody's decision and not a finding. A module with only some of its
# units on disk is DRIFTED, because the installer now writes all of them.
#
# READ-ONLY BY CONSTRUCTION. It opens unit files for reading and imports the
# modules that render them; it writes no file, runs no systemctl and never
# re-installs. The row names the command that does.

CLEAN = "clean"          # the installed unit is what its template renders
DRIFTED = "drifted"      # no install inputs render the installed text
ABSENT = "absent"        # none of the module's units is on this box

UnitTemplate = collections.namedtuple(
    "UnitTemplate", "module render wild command blocks flags")
UnitTemplate.__doc__ = """One render function whose units the drift census
compares: `module` under helm/, its `render` returning (service path, service,
timer path, timer), the per-install inputs it takes as `wild`cards, the
`command` that re-installs it, the environment `blocks` it copies from its
installer's environment as (input, variable prefix) pairs, and the `flags`
that carry a recovered input back into the command as (input, option)."""
UnitTemplate.__new__.__defaults__ = ((), ())

_HOME = ("helm", "cwd")

UNIT_TEMPLATES = (
    UnitTemplate("autoland", "timer_units", _HOME,
                 "helm train auto --install-timer"),
    UnitTemplate("autocompact", "_timer_units", _HOME + ("interval",),
                 "helm seat autocompact --install-timer --apply",
                 flags=(("interval", "--interval"),)),
    UnitTemplate("beacons", "timer_units", _HOME,
                 "helm beacons --install-timer"),
    UnitTemplate("chat", "_logflush_timer_units", _HOME + ("interval",),
                 "helm chat log-flush --install-timer --apply",
                 flags=(("interval", "--interval"),)),
    UnitTemplate("checkoutwatch", "timer_units", _HOME,
                 "helm work checkout-watch --install-timer"),
    UnitTemplate("gatecanary", "timer_units", _HOME + ("env",),
                 "helm gate canary --install-timer"),
    UnitTemplate("gateshadow", "timer_units", _HOME,
                 "helm gate shadow --install-timer"),
    UnitTemplate("gc", "timer_units", _HOME, "helm gc --install-timer"),
    UnitTemplate("keepalive", "_timer_units", _HOME,
                 "helm keepalive --ensure-timer"),
    UnitTemplate("offpeak", "timer_units", _HOME,
                 "helm offpeak --install-timer"),
    UnitTemplate("owedpush", "_timer_units", _HOME,
                 "helm owed-push --ensure-timer"),
    UnitTemplate("pressurewatch", "timer_units", _HOME,
                 "helm pressure-watch --install-timer"),
    UnitTemplate("proxy_fork_watch", "timer_units", (),
                 "helm proxy-fork-watch --install-timer"),
    UnitTemplate("proxywatch", "timer_units", _HOME,
                 "helm proxywatch --install-timer"),
    UnitTemplate("proxywatch", "dark_timer_units", _HOME,
                 "helm proxywatch --install-timer"),
    UnitTemplate("releasenightly", "timer_units", _HOME + ("env",),
                 "helm release nightly --install-timer"),
    UnitTemplate("remote_relay", "timer_units", _HOME + ("interval",),
                 "helm remote ensure-timer",
                 blocks=(("knobs", "HELM_REMOTE_"),)),
    UnitTemplate("seat_lifecycle", "rebind_timer_units", _HOME,
                 "helm seat rebind --install-timer"),
    UnitTemplate("stalebot", "_timer_units", _HOME,
                 "helm stale sweep --ensure-timer"),
    UnitTemplate("tasksmirror", "_timer_units", _HOME,
                 "helm task mirror --ensure-timer"),
    UnitTemplate("upstream_watch", "timer_units", (),
                 "helm upstream-watch --install-timer",
                 blocks=(("env", "HELM_UPSTREAM_WATCH_"),)),
)

_TOKEN = re.compile("\x00([A-Za-z_]+)\x00")
_BLOCK_KEY = "Environment"
# No unit helm writes is near this; a file past it is read as unreadable
# rather than fed to the matcher.
_MAX_UNIT_BYTES = 1 << 16
_SHOWN = 4


def wildcard(name):
    """The mark a per-install input `name` renders as when the drift census
    renders a template. NUL cannot occur in a unit file."""
    return "\x00%s\x00" % name


def unit_values(values, inputs=None):
    """`values`, the mapping a unit template is rendered with, with each input
    `inputs` names put in its place. An installer passes no `inputs`; the drift
    census passes wildcards. A name `values` does not hold is ignored, so a
    render only ever changes where it already takes that input."""
    if not inputs:
        return values
    out = dict(values)
    out.update((k, v) for k, v in inputs.items() if k in values)
    return out


def template_units(spec, inputs=None):
    """[(unit path, text)] of `spec`'s render, through the module's own render
    function. `inputs` None renders every declared input as its wildcard (a
    block as one wildcard line); a dict renders those values instead."""
    import importlib
    mod = importlib.import_module("%s.%s" % (__package__, spec.module))
    if inputs is None:
        inputs = dict((name, wildcard(name)) for name in spec.wild)
        inputs.update((name, wildcard(name) + "\n")
                      for name, _prefix in spec.blocks)
    render = getattr(mod, spec.render)
    quad = render(inputs=inputs) if inputs else render()
    return [(quad[0], quad[1]), (quad[2], quad[3])]


def _read_installed(path):
    """-> (text, None) | (None, None) when absent | (None, why) unreadable."""
    try:
        with open(path, "rb") as fh:
            data = fh.read(_MAX_UNIT_BYTES + 1)
    except FileNotFoundError:
        return None, None
    except OSError as exc:
        return None, "unreadable (%s)" % type(exc).__name__
    if len(data) > _MAX_UNIT_BYTES:
        return None, "unreadable (larger than %d bytes)" % _MAX_UNIT_BYTES
    try:
        return data.decode("utf-8"), None
    except UnicodeDecodeError:
        return None, "unreadable (not UTF-8)"


def _directives(text):
    """[(section, key, value)] in file order; comments and blanks dropped.

    EXACTLY WHAT systemd STRIPS, NO MORE AND NO LESS (task/3423). systemd
    strips _SYSTEMD_SPACE from both ends of a line, a key and a value, and
    keeps every other byte (measured, 259: `K=/x/claude\\r` sets /x/claude
    and `K=/a ` sets /a, while an NBSP or a form feed ending a line is part
    of the value). So each directive is what systemd read. Python's
    str.strip also took the NBSP and the form feed, and called CLEAN a unit a
    re-install changes; keeping what systemd strips called DRIFTED a CRLF
    unit a re-install writes the same, and put a "\\r" and a trailing space
    into its re-install line (_env_knob)."""
    section, out = "", []
    for raw in text.split("\n"):
        line = raw.strip(_SYSTEMD_SPACE)
        if not line or line[0] in "#;":
            continue
        if line[0] == "[" and line[-1] == "]":
            section = line
            continue
        key, _sep, value = line.partition("=")
        out.append((section, key.strip(_SYSTEMD_SPACE),
                    value.strip(_SYSTEMD_SPACE)))
    return out


def _template_groups(text, blocks):
    """{(section, key): [element]} for a wildcard render. An element is
    ("line", value) or ("block", input name)."""
    groups = collections.OrderedDict()
    for section, key, value in _directives(text):
        mark = _TOKEN.fullmatch(key)
        if mark and mark.group(1) in blocks and not value:
            groups.setdefault((section, _BLOCK_KEY), []).append(
                ("block", mark.group(1)))
        else:
            groups.setdefault((section, key), []).append(("line", value))
    return groups


def _line_regex(value, names):
    """Regex source for one template value: literal text escaped, each
    wildcard a lazy capture whose group id `names` maps to its input."""
    parts, pos = [], 0
    for m in _TOKEN.finditer(value):
        parts.append(re.escape(value[pos:m.start()]))
        gid = "g%d" % len(names)
        names[gid] = m.group(1)
        parts.append("(?P<%s>.+?)" % gid)
        pos = m.end()
    parts.append(re.escape(value[pos:]))
    return "".join(parts)


def _block_regex(prefix):
    """An installed knob line of `prefix` in either form an installer wrote:
    the word raw, as every one did before task/3423, or double-quoted, as
    env_assignment writes a value systemd would otherwise split or drop.
    Whether a re-install writes the line again is _env_knob's to say."""
    name = re.escape(prefix) + "[A-Za-z0-9_]*="
    return r'(?:%s.*|"%s(?:[^"\\\n]|\\.)*")' % (name, name)


def _env_knob(text):
    """(name, value, current) for one installed knob line `text`: the value
    its installer was given, and whether env_assignment writes `text` again
    for it. A quoted line is env_assignment's, read back through its escapes
    and doubled "%"; an unquoted line is its text as systemd read it, its
    ends stripped as systemd strips them (_directives), which is what every
    installer before task/3423 wrote and what env_assignment writes for a
    value needing no quoting, so an old plain unit is current and one whose
    value systemd split, dropped or expanded is not."""
    if text[:1] == '"':
        name, _sep, value = re.sub(r"\\(.)", r"\1", text[1:-1]).partition("=")
        value = value.replace("%%", "%")
    else:
        name, _sep, value = text.partition("=")
    return name, value, env_assignment(name, value) == text


def _match_group(elements, values, blocks):
    """-> {input: value} and {block: [lines]} when `values` is a render of
    `elements` under some wildcard values, else None."""
    names, source = {}, []
    for kind, what in elements:
        if kind == "block":
            gid = "b%d" % len(names)
            names[gid] = what
            source.append("(?P<%s>(?:%s\n)*)" % (gid, _block_regex(
                blocks[what])))
        else:
            source.append(_line_regex(what, names) + "\n")
    m = re.fullmatch("".join(source), "".join(v + "\n" for v in values))
    if m is None:
        return None
    captured, knobs = {}, {}
    for gid, name in names.items():
        if gid.startswith("b"):
            knobs[name] = [v for v in m.group(gid).split("\n") if v]
        else:
            captured.setdefault(name, set()).add(m.group(gid))
    # A KNOB LINE A RE-INSTALL WRITES DIFFERENTLY IS DRIFT (task/3423): an
    # older installer wrote `K=/a b` raw, systemd set K=/a, and the renderer
    # now quotes it. A plain value reads the same in both forms.
    if not all(_env_knob(v)[2] for lines in knobs.values() for v in lines):
        return None
    return captured, knobs


def _block_lines(elements, values, blocks):
    """{block: [installed lines]} read line by line: each value a block's
    prefix admits and no literal template line makes."""
    lines = [re.compile(_line_regex(v, {}))
             for kind, v in elements if kind == "line"]
    out = {}
    for kind, name in elements:
        if kind == "block":
            shape = re.compile(_block_regex(blocks[name]))
            out[name] = [v for v in values if shape.fullmatch(v)
                         and not any(p.fullmatch(v) for p in lines)]
    return out


def _show(text):
    text = "".join(c if c.isprintable() else "?" for c in text)
    return text if len(text) <= 160 else text[:157] + "..."


def _shown(parts):
    more = len(parts) - _SHOWN
    return "; ".join(parts[:_SHOWN]) + (
        "; and %d more" % more if more > 0 else "")


def _compare(installed, template, blocks):
    """-> (verdict, detail, {input: {values}}, {block: [lines]})."""
    blocks = dict(blocks)
    want = _template_groups(template, blocks)
    have = collections.OrderedDict()
    for section, key, value in _directives(installed):
        have.setdefault((section, key), []).append(value)
    captured, knobs, failed = {}, {}, []
    for group in list(want) + [g for g in have if g not in want]:
        got = _match_group(want.get(group, []), have.get(group, []), blocks)
        if got is None:
            failed.append(group)
            # THE KNOBS STILL COUNT when their group drifted: a re-install
            # line without them would drop the operator's own settings.
            got = ({}, _block_lines(want.get(group, []),
                                    have.get(group, []), blocks))
        for name, values in got[0].items():
            captured.setdefault(name, set()).update(values)
        for name, lines in got[1].items():
            knobs.setdefault(name, []).extend(lines)
    if failed:
        return DRIFTED, _shown(_differences(failed, want, have, blocks,
                                            captured)), captured, knobs
    split = sorted(n for n, v in captured.items() if len(v) > 1)
    if split:
        return UNKNOWN, ("the installer's inputs cannot be recovered: "
                         + "; ".join("%s reads %s" % (n, " and ".join(
                             repr(_show(v)) for v in sorted(captured[n])))
                                     for n in split)), captured, knobs
    return CLEAN, "", captured, knobs


def _differences(failed, want, have, blocks, captured):
    """The lines that prove each failed group: what the template renders and
    no installed line matches, what is installed and no template line makes,
    else the order or count within the key."""
    out = []
    for section, key in failed:
        elements = want.get((section, key), [])
        values = have.get((section, key), [])
        lines = [v for kind, v in elements if kind == "line"]
        literal = [re.compile(_line_regex(v, {})) for v in lines]
        patterns = literal + [re.compile(_block_regex(blocks[v]))
                              for kind, v in elements if kind == "block"]
        before = len(out)
        for value, pattern in zip(lines, patterns):
            if not any(pattern.fullmatch(v) for v in values):
                shown = _TOKEN.sub(lambda m: "".join(captured[m.group(1)])
                                   if len(captured.get(m.group(1), ())) == 1
                                   else "<%s>" % m.group(1), value)
                out.append("missing %s=%s" % (key, _show(shown)))
        for value in values:
            if not any(p.fullmatch(value) for p in patterns):
                out.append("extra %s=%s" % (key, _show(value)))
            elif not any(p.fullmatch(value) for p in literal):
                name, knob, current = _env_knob(value)
                if not current:
                    word = env_assignment(name, knob)
                    out.append("misquoted %s=%s (a re-install %s)" % (
                        key, _show(value), "writes %s=%s" % (key, _show(word))
                        if word else "leaves it out"))
        if len(out) == before:
            out.append("%s lines differ in order or count" % key)
    return out


def _command(spec, captured, knobs):
    """The line that re-installs `spec`'s module with the inputs its units
    were installed with: the knobs as the environment they came from, and
    each flag whose input one value was recovered for."""
    words = []
    for name, _prefix in spec.blocks:
        for line in knobs.get(name, ()):
            var, value, _current = _env_knob(line)
            words.append("%s=%s" % (var, shlex.quote(value)))
    words.append(spec.command)
    for name, option in spec.flags:
        values = captured.get(name, ())
        if len(values) == 1:
            words += [option, shlex.quote(next(iter(values)))]
    return " ".join(words)


def drift(templates=None):
    """-> rows, one per unit file of every module in `templates`
    (UNIT_TEMPLATES by default): {"module", "unit", "path", "verdict",
    "detail", "command"}, the verdict one of CLEAN, DRIFTED, UNKNOWN and
    ABSENT. A template that cannot be rendered is one UNKNOWN row named
    after its render function. READ-ONLY: see this section's header."""
    modules = collections.OrderedDict()
    for spec in templates or UNIT_TEMPLATES:
        modules.setdefault(spec.module, []).append(spec)
    rows = []
    for module, specs in modules.items():
        units = []
        for spec in specs:
            # A render that raises is UNMEASURED: one UNKNOWN row, and the
            # module's other renders are still compared.
            try:
                wild = template_units(spec)
            except Exception as exc:            # noqa: BLE001 — see above
                rows.append({
                    "module": module, "unit": "%s()" % spec.render,
                    "path": None, "verdict": UNKNOWN,
                    "detail": "its template could not be rendered (%s)"
                              % type(exc).__name__,
                    "command": spec.command})
                continue
            for path, text in wild:
                units.append((spec, path, text) + _read_installed(path))
        rows.extend(_module_rows(module, units))
    return rows


def _module_rows(module, units):
    """Rows for one module's rendered units: (spec, path, template,
    installed text or None, why unreadable or None)."""
    here = [u for u in units if u[3] is not None or u[4]]
    captured, knobs, out = {}, {}, []
    for spec, path, template, text, why in units:
        name = os.path.basename(path)
        if not here:
            verdict, detail = ABSENT, ""
        elif why:
            verdict, detail = UNKNOWN, why
        elif text is None:
            verdict, detail = DRIFTED, "not installed, while %s %s" % (
                ", ".join(os.path.basename(u[1]) for u in here),
                "is" if len(here) == 1 else "are")
        else:
            verdict, detail, got, lines = _compare(text, template,
                                                   spec.blocks)
            for key, values in got.items():
                captured.setdefault(key, set()).update(values)
            for key, block in lines.items():
                knobs.setdefault(key, []).extend(block)
        out.append({"module": module, "unit": name, "path": path,
                    "verdict": verdict, "detail": detail, "spec": spec})
    for row in out:
        row["command"] = _command(row.pop("spec"), captured, knobs)
    return out
