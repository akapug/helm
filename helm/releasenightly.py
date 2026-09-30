"""The nightly release dry run: can a release still be cut from trunk?

WHY IT EXISTS. A public release is cut by `scripts/release/release.py`, and its
dry run is the proof that a cut would pass: it builds the release commit from
trunk, backs up the public repository and runs every gate a publish runs.
Nothing ran it between releases, so a gate that trunk had broken (a seat line
off the KEEP list, a stale omit entry, a private needle, an installer that no
longer installs) surfaced only on the day of a release. The release's
falsifier is seven consecutive green nightly dry runs.

WHAT ONE NIGHT DOES, on the helm hub where the record lives: fetch trunk fresh,
open a read-only peek room at trunk's tip, and run the release tool's
`--nightly` in that room ON THE BUILD HOST the config names, never on the hub:

    fab <host> --key helm-release-nightly --repo <room> --
        python3 scripts/release/release.py --nightly --trunk <sha>
        --public <url> --private <url>

`--nightly` is the tool's own dry run, the same steps in the same code, with
no push, no tag on a remote and no GitHub release; the tool's docstring says
what differs from a plain dry run and why. The hub resolves the two URLs from
its own remotes (the build host has the room's tree and none of them). The
public repository is handed over in its https form: the dry run only READS it,
which needs no key, and the build hosts hold none. The private URL is never
contacted: it is only named in the stage write the dry run prints. Then one
verdict row:

  GREEN    the tool passed every gate on this trunk, said so, and exited 0.
  RED      the tool refused. The row names the first step that refused
           (read, backup, build, gate/<gate>), or `tool` when the tool started
           and gave no verdict.
  MISSING  the dry run did not run: no host configured, trunk could not be
           fetched, no room (or a reused one left dirty), or the launch never
           reached the host.

THE RECORD (`history_path`, one JSON line per run, append-only) has the gate
canary's shape (helm/gatecanary.py) and is read whole or not at all. `streak`
counts consecutive green NIGHTS back from the newest night that is due, by the
timer's rows: a night holding a RED is red, a night with no GREEN is MISSING,
and either ends the streak, so a timer that stopped firing or a hub that was
off shows up as the night it cost. A manual `run` is recorded and shown; it
neither counts nor breaks. A record that cannot be read whole reads UNKNOWN,
never 0 and never green.

WHERE IT RUNS IS CONFIG, the canary's rule: `--install-timer --host HOST`
writes HELM_RELEASE_NIGHTLY_HOST to `config_env_path()`, which the timer's
service reads and a manual `run` reads too, so no host is a literal in helm.
The unit installer is the canary's pattern (`ensure_timer`), kept separate
until one shared user-timer installer exists.
"""
import datetime
import json
import os
import re
import shlex
import subprocess
import sys
import time

from . import eventledger, home, pk, vcs

STATE_SUBDIR = os.path.join(".state", "release-nightly")
HISTORY_NAME = "verdicts.jsonl"
LOG_SUBDIR = "logs"
HISTORY_VERSION = 1
VERDICT_EVENT = "release-nightly-verdict"
GREEN, RED, MISSING = "GREEN", "RED", "MISSING"
VERDICTS = (GREEN, RED, MISSING)
# What `streak` says about the record as a whole.
KNOWN, UNKNOWN = "KNOWN", "UNKNOWN"
# Who ran it: the timer's nightly, or a person's `run`.
TIMER, MANUAL = "timer", "manual"
SOURCES = (TIMER, MANUAL)
# The env the installed unit sets in its service, so `--timer` runs are
# timer-attributed only when the unit's scheduler ran them, not when a
# person typed `--timer` by hand.
SOURCE_ENV = "HELM_RELEASE_NIGHTLY_SOURCE"
# The falsifier: this many consecutive green nights.
NEEDED = 7
TOOL = "scripts/release/release.py"
PUBLIC_REMOTE, PRIVATE_REMOTE = "aspublic", "origin"
# One warm worktree and one mirror on the build host, shared night to night.
FAB_KEY = "helm-release-nightly"
LAUNCH_TIMEOUT_S = 2 * 3600
STAMP = "%Y-%m-%dT%H:%M:%SZ"
_SHA = re.compile(r"[0-9a-f]{40}\Z")
_NIGHT = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
_GITHUB_SSH = re.compile(r"^(?:git@github\.com:|ssh://git@github\.com/)"
                         r"([^/]+)/([^/]+?)(?:\.git)?/?$")
# What a launch log carries: the tool's banner, fab's line once the job is on
# the host, and the tool's closing line.
STARTED = re.compile(r"^helm\s+release: NIGHTLY\b", re.M)
LAUNCHED = re.compile(r"^fab: id=\S+ +LOG: ", re.M)
RESULT = re.compile(r"^NIGHTLY\s+(GREEN|RED)\b(.*)$", re.M)
REFUSED = re.compile(r"^REFUSED\s+(.*)$", re.M)


def state_dir(global_dir=None):
    return os.path.join(global_dir or home.global_dir(), STATE_SUBDIR)


def history_path(global_dir=None):
    return os.path.join(state_dir(global_dir), HISTORY_NAME)


def night_of(epoch):
    """The night a run started on: its local calendar date on this host."""
    return time.strftime("%Y-%m-%d", time.localtime(epoch))


def _timer_source(opts):
    """`--timer` counts as the timer's night only when its scheduler set the
    SOURCE_ENV marker; a hand-typed `--timer` with no marker is manual."""
    return (TIMER if opts.get("timer")
            and os.environ.get(SOURCE_ENV) == TIMER else MANUAL)


def _date(night):
    try:
        return datetime.date.fromisoformat(night)
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------ record

def history_row(verdict, started, finished, source, step=None, reason="",
                trunk=None, candidate=None, host=None, log=None, code=None):
    """The one line a run leaves in the record."""
    return {"v": HISTORY_VERSION, "event": VERDICT_EVENT,
            "id": os.urandom(16).hex(),
            "at": time.strftime(STAMP, time.gmtime(finished)),
            "started": time.strftime(STAMP, time.gmtime(started)),
            "night": night_of(started), "source": source,
            "verdict": verdict, "step": step,
            "reason": str(reason or "")[:500], "trunk": trunk,
            "candidate": candidate, "host": host,
            "wall_s": max(0, int(round(finished - started))),
            "log": log, "exit": code}


def append_verdict(row, global_dir=None):
    """Append one run's row. -> True when it is durable.

    Never raises: a row the record could not keep is a night the streak does
    not count, which is the safe direction."""
    try:
        return eventledger.append(history_path(global_dir), row)
    except Exception:           # noqa: BLE001 -- see the docstring
        return False


def _well_formed(row):
    if not isinstance(row, dict) or row.get("v") != HISTORY_VERSION \
            or row.get("event") != VERDICT_EVENT \
            or row.get("verdict") not in VERDICTS \
            or row.get("source") not in SOURCES \
            or not isinstance(row.get("at"), str):
        return False
    night = row.get("night")
    if not isinstance(night, str) or not _NIGHT.match(night) \
            or _date(night) is None:
        return False
    if row["verdict"] == RED:
        return isinstance(row.get("step"), str) and bool(row["step"])
    if row["verdict"] == GREEN:
        return all(isinstance(row.get(k), str) and bool(_SHA.match(row[k]))
                   for k in ("trunk", "candidate"))
    return True


# ------------------------------------------------------------------ streak

def _due_night(now):
    """The newest night whose run is due by `now`: tonight once the timer's
    time plus DUE_GRACE_H has passed, else last night."""
    lt = time.localtime(now)
    h, m, s = (int(x) for x in TIMER_AT.split(":"))
    due = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, h, m, s, 0, 0, -1))
    today = datetime.date(lt.tm_year, lt.tm_mon, lt.tm_mday)
    if now >= due + DUE_GRACE_H * 3600:
        return today
    return today - datetime.timedelta(days=1)


def _night_verdict(rows):
    """(verdict, the row that decided it or None) of one night's timer rows:
    any RED makes the night red, else any GREEN makes it green, else it is
    MISSING (rows that record the dry run did not run, or no row at all)."""
    for verdict in (RED, GREEN, MISSING):
        hits = [row for row in rows if row["verdict"] == verdict]
        if hits:
            return verdict, hits[-1]
    return MISSING, None


def streak(global_dir=None, now=None):
    """What the record says about the falsifier. -> {"state", "count",
    "needed", "last_red", "stopped", "last", "last_manual", "why", "path"}.

    `count` is the consecutive green nights ending at the newest night that
    is due (tonight, once a run is recorded tonight or its time plus the
    grace has passed), by the timer's rows only. `stopped` is the night that
    ends it: {"night", "verdict", "step", "reason"}.

    READ WHOLE OR NOT AT ALL. A record that cannot be read, or holds one line
    that is not a verdict, is UNKNOWN with `count` None: the missing line may
    be the red night."""
    path = history_path(global_dir)
    out = {"state": UNKNOWN, "count": None, "needed": NEEDED,
           "last_red": None, "stopped": None, "last": None,
           "last_manual": None, "why": None, "path": path}
    rows, unavailable = eventledger.checked_events(path, strict=True)
    bad = next((i for i, row in enumerate(rows) if not _well_formed(row)), None)
    if unavailable or bad is not None:
        out["why"] = unavailable or ("entry %d is not a nightly verdict"
                                     % (bad + 1))
        return out
    now = time.time() if now is None else now
    timer = [row for row in rows if row["source"] == TIMER]
    nights = {}
    for row in timer:
        nights.setdefault(row["night"], []).append(row)
    day = _due_night(now)
    tonight = _date(night_of(now))
    if tonight.isoformat() in nights:
        day = tonight
    count = 0
    while True:
        verdict, row = _night_verdict(nights.get(day.isoformat(), []))
        if verdict != GREEN:
            out["stopped"] = {
                "night": day.isoformat(), "verdict": verdict,
                "step": (row or {}).get("step"),
                "reason": (row or {}).get("reason")
                or "no nightly run was recorded that night"}
            break
        count += 1
        day -= datetime.timedelta(days=1)
    reds = [row for row in timer if row["verdict"] == RED]
    out.update(state=KNOWN, count=count, last_red=reds[-1] if reds else None,
               last=timer[-1] if timer else None,
               last_manual=next((row for row in reversed(rows)
                                 if row["source"] == MANUAL), None))
    return out


def streak_line(held):
    """`N of 7 consecutive green nightlies; last red: <night> <step>`, or
    the UNKNOWN that stands in for it."""
    if held["state"] != KNOWN:
        return ("UNKNOWN: the nightly record %s cannot be read whole (%s), so "
                "it counts nothing until it is repaired"
                % (held["path"], held["why"]))
    n, red = held["count"], held["last_red"]
    return "%d of %d consecutive green nightlies%s; last red: %s" % (
        min(n, NEEDED), NEEDED, " (a streak of %d)" % n if n > NEEDED else "",
        "%s %s" % (red["night"], red["step"]) if red else "none")


def _run_line(label, row):
    return "  %s: %s %s%s, trunk %s on %s, %d s; log %s" % (
        label, row["at"], row["verdict"],
        " at " + row["step"] if row.get("step") else "",
        (row.get("trunk") or "unknown")[:12], row.get("host") or "no host",
        row.get("wall_s") or 0, row.get("log") or "none")


def status_lines(held):
    """The status verb's lines: the streak, what stopped it, the newest runs,
    and whether this host can run the next night at all."""
    lines = ["helm release nightly: " + streak_line(held)]
    stop = held["stopped"]
    if stop:
        lines.append("  the streak stops at %s: %s%s — %s" % (
            stop["night"], stop["verdict"],
            " at " + stop["step"] if stop["step"] else "", stop["reason"]))
    for key, label in (("last", "last nightly"),
                       ("last_manual", "last manual run")):
        if held[key]:
            lines.append(_run_line(label, held[key]))
    lines.append("  timer: %s" % (
        "%s installed, nightly at %s" % (TIMER_NAME, TIMER_AT)
        if timer_installed() else
        "NOT INSTALLED on this host, so every night reads MISSING; `helm "
        "release nightly --install-timer --host HOST` installs it"))
    host = configured_host()
    lines.append("  host: %s" % (
        "%s (%s)" % (host, config_env_path()) if host else
        "NOT CONFIGURED, so a run is MISSING; `--install-timer --host HOST` "
        "writes %s" % config_env_path()))
    return lines


# -------------------------------------------------------------------- run

HOST_ENV = "HELM_RELEASE_NIGHTLY_HOST"
_HOST_ATOM = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,62}\Z")


def config_env_path():
    base = os.environ.get("XDG_CONFIG_HOME") or \
        os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "helm", "release-nightly.env")


def configured_host():
    """The build host the dry run runs on: HELM_RELEASE_NIGHTLY_HOST, else
    the one `config_env_path()` names (the service reads that file into its
    environment; a manual `run` reads it here), else None. A value that is
    not a plain host name is None."""
    host = os.environ.get(HOST_ENV, "").strip()
    if not host:
        try:
            with open(config_env_path(), encoding="utf-8") as fh:
                for line in fh:
                    name, sep, value = line.strip().partition("=")
                    if sep and name == HOST_ENV:
                        host = value.strip()
        except OSError:
            return None
    return host if _HOST_ATOM.match(host) else None


# `fab nodes --json` probes every node live before it answers: 33.9 s on the
# hub that runs the nightly timer (measured), and longer while a
# node is down. A timeout under that would make every night a MISSING night.
FAB_NODES_TIMEOUT = 120


def fab_hosts():
    """The build hosts `fab` itself lists (`fab nodes --json`), so a host that
    the fleet does not know of can never be the one the nightly names: no
    hand-written list, never a pin that reserves a node. -> (set or None,
    why). `None` means fab could not answer: not found, timed out, non-zero
    exit, or an answer that is not a JSON object. An empty node list is an
    answer, an empty set, and every host is then refused as not listed."""
    try:
        r = subprocess.run(
            ["fab", "nodes", "--json"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=FAB_NODES_TIMEOUT)
    except FileNotFoundError:
        return None, ("fab is not on PATH; the host is unvalidated (no "
                      "answer from fab `nodes --json`)")
    except subprocess.TimeoutExpired:
        return None, ("fab `nodes --json` timed out (no answer within %d s)"
                      % FAB_NODES_TIMEOUT)
    if r.returncode != 0:
        tail = (r.stderr or r.stdout or b"").decode("utf-8", "replace")
        return None, ("fab `nodes --json` failed (exit %s: %s)" % (
            r.returncode, tail.strip()[:300]))
    try:
        body = json.loads(r.stdout.decode("utf-8", "replace"))
        hosts = [n["host"] for n in (body.get("nodes") or [])
                 if isinstance(n, dict) and n.get("host")]
        return set(hosts), ""
    except (json.JSONDecodeError, UnicodeDecodeError,
            AttributeError, KeyError, TypeError) as exc:
        return None, "fab `nodes --json` answered but was unparseable: %s" % exc


def write_host(host):
    """Record the build host the dry run runs on. -> (ok, detail)

    The host must survive BOTH checks: the plain-shape check, and
    `fab_hosts()` — the fleet's own list — so a name that looks like a host
    but is a fab verb, or a host fab does not know, refuses before it is
    ever written where a nightly would pick it up."""
    if not _HOST_ATOM.match(str(host or "")):
        return False, "--host must be a plain host name, got %r" % host
    hosts, why = fab_hosts()
    if hosts is None:
        return False, "cannot confirm the host: %s" % why
    if host not in hosts:
        return False, (
            "host %r is not in the build fleet `fab nodes --json` lists; the "
            "nightly would launch against a name fab does not own. The fleet: "
            "%s" % (host, ", ".join(sorted(hosts))[:120]
            if len(hosts) else "(none)"))
    path = config_env_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        pk.atomic_write(path, "%s=%s\n" % (HOST_ENV, host))
    except OSError as exc:
        return False, "cannot write %s: %s" % (path, exc)
    return True, "the nightly release dry run runs on %s (%s)" % (host, path)


def launcher(host):
    """The argv prefix that runs one command on `host` against a room:
    `<prefix> --repo ROOM -- <command>`, pinned to that host."""
    return ["fab", host, "--key", FAB_KEY]


def tool_argv(sha, public, private):
    """The release tool's nightly, as the build host runs it in the room."""
    return ["python3", TOOL, "--nightly", "--trunk", sha,
            "--public", public, "--private", private]


def remotes(root):
    """(public URL, private URL, why) of `root`'s release remotes, the way
    the tool is handed them: a GitHub SSH URL of the public repository in its
    https form, read-only and keyless (see the module docstring)."""
    backend = vcs.backend(root)
    urls = []
    for name in (PUBLIC_REMOTE, PRIVATE_REMOTE):
        rc, url, _err = backend.text(root, "remote", "get-url", name)
        url = (url or "").strip() if rc == 0 else ""
        if not url:
            return None, None, "the checkout %s has no remote %s" % (root, name)
        urls.append(url)
    m = _GITHUB_SSH.match(urls[0])
    public = "https://github.com/%s/%s.git" % m.groups() if m else urls[0]
    return public, urls[1], None


def run(repo=None, global_dir=None, runner=None, source=MANUAL, host=None):
    """One night: fetch trunk fresh, open a peek room at its tip, run the
    release tool's --nightly there on the build host, record the verdict.
    -> the row, plus "recorded": whether the record kept it.

    `runner(argv, log, env)` returns the launch's exit code; tests plant it."""
    from . import work
    from .work import _gc
    started = time.time()
    root = work.find_root(os.path.realpath(repo or os.getcwd())) \
        or os.path.realpath(repo or os.getcwd())
    host = host or configured_host()
    log = os.path.join(state_dir(global_dir), LOG_SUBDIR, "%s-%s.log" % (
        time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(started)), source))

    def done(verdict, reason, step=None, sha=None, candidate=None, code=None):
        row = history_row(verdict, started, time.time(), source, step=step,
                          reason=reason, trunk=sha, candidate=candidate,
                          host=host, code=code,
                          log=log if os.path.exists(log) else None)
        return dict(row, recorded=append_verdict(row, global_dir))

    if not host:
        return done(MISSING, "no build host is configured: `helm release "
                             "nightly --install-timer --host HOST` writes %s"
                    % config_env_path())
    # Fail closed: the host the nightly would launch against must be a node the
    # fleet itself knows of (`fab nodes --json`). A name that looks like a
    # host but is a fab verb, or one fab does not know, is a MISSING night and
    # is never handed to the launcher.
    hosts, why = fab_hosts()
    if hosts is None:
        return done(MISSING, "the build host %s cannot be confirmed: %s; the "
                             "dry run refuses to launch until it can be named "
                             "by `fab nodes --json`" % (host, why))
    if host not in hosts:
        return done(MISSING, "the build host %s is not in the fleet `fab "
                             "nodes --json` lists; the nightly would launch "
                             "against a name fab does not own and would run "
                             "the wrong command. The fleet: %s" % (
                                 host, ", ".join(sorted(hosts))[:120]
                                 if len(hosts) else "(none)"))
    ok, why = _gc.refresh_trunk(root)
    backend = vcs.backend(root)
    ref = backend.trunk_ref(root)
    sha = (backend.head_sha(root, ref=ref) or "").strip() if ok else ""
    if not sha:
        return done(MISSING, "trunk %s could not be fetched fresh and "
                             "resolved: %s" % (ref, why))
    public, private, why = remotes(root)
    if why:
        return done(MISSING, why, sha=sha)
    try:
        os.makedirs(os.path.dirname(log), exist_ok=True)
    except OSError as exc:
        return done(MISSING, "cannot create the log directory %s: %s"
                    % (os.path.dirname(log), exc), sha=sha)
    rc, peeked = work.peek(root, sha)
    if rc != 0:
        return done(MISSING, peeked.get("error") or "peek refused", sha=sha)
    room = peeked["path"]
    # fab ships the room's WORKING TREE, uncommitted and untracked files too.
    # A reused room somebody left dirty would run a release tool that is not
    # trunk's, so that night is not the dry run of trunk. The room is not
    # this run's to drop: it is left as found.
    if peeked.get("dirty"):
        return done(MISSING, "the peek room %s at trunk %s is a reused room "
                             "with uncommitted changes, and fab ships the "
                             "working tree; it was left as found"
                    % (room, sha[:12]), sha=sha)
    argv = launcher(host) + ["--repo", room, "--"] + tool_argv(
        sha, public, private)
    try:
        code = (runner or _launch)(argv, log, dict(os.environ))
    finally:
        work.peek_drop(root, room)
    verdict, step, candidate, reason = judge(log, code, sha, host)
    return done(verdict, reason, step=step, sha=sha, candidate=candidate,
                code=code)


def judge(log, code, sha, host):
    """(verdict, step, candidate, reason) of one launch, from its log and
    exit code. GREEN needs BOTH the tool's GREEN line naming this trunk and a
    clean exit. A tool that started (its banner, or fab's line once the job
    is on the host) and gave no verdict is RED at `tool`; a launch that never
    started it is MISSING."""
    try:
        with open(log, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError as exc:
        return MISSING, None, None, "the launch log %s cannot be read: %s" % (
            log, exc)
    found = RESULT.findall(text)
    if found:
        word, rest = found[-1]
        fields = dict(f.split("=", 1) for f in rest.split() if "=" in f)
        if word == RED:
            step = fields.get("step") or "tool"
            refused = REFUSED.findall(text)
            return RED, step, None, "the release tool refused at %s (exit %s)%s" % (
                step, code, ": " + refused[-1][:300] if refused else "")
        candidate = fields.get("candidate", "")
        wrong = ["exit %s" % code] * (code != 0) + [
            "it names trunk %s, not %s" % (fields.get("trunk"), sha)] * (
                fields.get("trunk") != sha) + [
            "no candidate"] * (not _SHA.match(candidate))
        if not wrong:
            return GREEN, None, candidate, (
                "the release tool passed every gate on trunk %s" % sha[:12])
        return RED, "tool", None, "the release tool said GREEN, but %s" % (
            "; ".join(wrong))
    if STARTED.search(text) or LAUNCHED.search(text):
        return RED, "tool", None, (
            "the release tool started on %s and gave no verdict (exit %s); "
            "log %s" % (host, code, log))
    tail = [line for line in text.splitlines() if line.strip()]
    return MISSING, None, None, "the dry run did not start on %s (launch exit "\
        "%s): %s" % (host, code, tail[-1][:200] if tail else "no output")


def _launch(argv, log, env):
    with open(log, "a", encoding="utf-8") as fh:
        fh.write("\n$ %s\n" % " ".join(shlex.quote(a) for a in argv))
        fh.flush()
        try:
            return subprocess.run(argv, stdout=fh, stderr=subprocess.STDOUT,
                                  stdin=subprocess.DEVNULL, env=env,
                                  timeout=LAUNCH_TIMEOUT_S).returncode
        except (OSError, subprocess.TimeoutExpired) as exc:
            fh.write("nightly launch failed: %s\n" % exc)
            return None


# ------------------------------------------------------------------ timer

TIMER_NAME = "helm-release-nightly.timer"
SERVICE_NAME = "helm-release-nightly.service"
# 02:30 local: an hour before the gate canary (03:30), whose serial whole
# suite takes the same build host for most of an hour, and done long before
# it starts. A night is due once this plus the grace has passed.
TIMER_AT = "02:30:00"
DUE_GRACE_H = 3

_SERVICE = """[Unit]
Description=helm release nightly — can a release still be cut from trunk?

[Service]
WorkingDirectory=%(cwd)s
Type=oneshot
# The build host the dry run runs on is config (HELM_RELEASE_NIGHTLY_HOST),
# never a literal here: `helm release nightly --install-timer --host HOST`
# writes it.
EnvironmentFile=-%(env)s
# Trunk fetched fresh, the release tool's --nightly on the build host, and
# one verdict row in _global/.state/release-nightly/verdicts.jsonl.
ExecStart=%(helm)s release nightly run --repo %(cwd)s --timer
# The run reads this as its source: this service is the timer, not a person.
Environment=HELM_RELEASE_NIGHTLY_SOURCE=timer
Nice=15
"""

_TIMER = """[Unit]
Description=nightly helm release dry run (trunk, on the configured build host)

[Timer]
OnCalendar=*-*-* %(at)s
Persistent=true

[Install]
WantedBy=timers.target
"""

# THE SWITCH for a process that must not change this host's scheduler. Off
# means no unit file is written and no systemctl runs.
TIMER_ENV = "HELM_RELEASE_NIGHTLY_TIMER"
TIMER_OFF_VALUES = ("0", "off", "no", "false")


def timer_units(at=TIMER_AT, inputs=None):
    """(service_path, service_text, timer_path, timer_text), the working
    directory derived from this checkout's shared root, never a literal.
    `inputs` replaces per-install values (timerhealth.unit_values)."""
    from . import work
    helm_bin = os.path.join(os.path.expanduser("~"), ".local", "bin", "helm")
    from . import timerhealth
    udir = timerhealth.user_unit_dir()
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cwd = work.find_root(here) or here
    return (os.path.join(udir, SERVICE_NAME),
            _SERVICE % timerhealth.unit_values(
                {"helm": helm_bin, "cwd": cwd, "env": config_env_path()},
                inputs),
            os.path.join(udir, TIMER_NAME),
            _TIMER % timerhealth.unit_values({"at": at}, inputs))


def timer_switched_off():
    value = os.environ.get(TIMER_ENV)
    if value is not None and value.strip().lower() in TIMER_OFF_VALUES:
        return value
    return None


def timer_installed():
    """Is the timer's unit file on this host?"""
    return os.path.exists(timer_units()[2])


def ensure_timer():
    """(ok, detail): install and enable the nightly cadence, idempotently.
    `ok` is True (enabled), False (failed) or None (switched off by
    TIMER_ENV: nothing written, nothing run). The canary's installer, the
    same steps (helm/gatecanary.py `ensure_timer`)."""
    import shutil
    from . import timerhealth
    off = timer_switched_off()
    if off is not None:
        return None, ("install skipped by %s=%s: no unit file written, no "
                      "systemctl run" % (TIMER_ENV, off))
    systemctl = shutil.which("systemctl")
    if not systemctl:
        return False, ("systemctl unavailable; the other scheduler must set "
                       "HELM_RELEASE_NIGHTLY_SOURCE=timer in the command's "
                       "environment before running `helm release nightly "
                       "run --timer` nightly, or the run records its source "
                       "as manual")
    spath, service, tpath, timer = timer_units()
    error, unchanged = timerhealth.install_user_timer(
        ((spath, service), (tpath, timer)), (TIMER_NAME,), systemctl,
        subprocess, timeout=60, keep_unchanged=True, reload_unchanged=False)
    if error:
        return False, error
    return True, "%s nightly at %s (%s)" % (
        "already installed, unchanged," if unchanged else "installed",
        TIMER_AT, tpath)


# -------------------------------------------------------------------- cli

USAGE = ("usage: helm release nightly [status] | run [--repo PATH] [--timer] "
         "| --install-timer [--host HOST]")


def cmd_release(rest):
    """helm release nightly: the nightly release dry run and its streak."""
    rest = list(rest or ())
    if not rest or rest[0] != "nightly":
        print(USAGE, file=sys.stderr)
        return 2
    return _cmd_nightly(rest[1:])


def _cmd_nightly(rest):
    if rest[:1] == ["--install-timer"] and (
            len(rest) == 1 or (len(rest) == 3 and rest[1] == "--host")):
        if len(rest) == 3:
            ok, detail = write_host(rest[2])
            print("helm release nightly: %s" % detail,
                  file=sys.stdout if ok else sys.stderr)
            if not ok:
                return 2
        ok, detail = ensure_timer()
        print("helm release nightly: %s" % detail,
              file=sys.stderr if ok is False else sys.stdout)
        return 1 if ok is False else 0
    sub, args = (rest[0], rest[1:]) if rest else ("status", [])
    if sub == "status" and not args:
        held = streak()
        for line in status_lines(held):
            print(line)
        if held["state"] != KNOWN:
            return 3
        return 0 if held["count"] >= NEEDED else 1
    if sub == "run":
        opts, bad = {}, None
        while args and bad is None:
            if args[0] == "--timer":
                opts["timer"], args = True, args[1:]
            elif args[0] == "--repo" and len(args) > 1:
                opts["repo"], args = args[1], args[2:]
            else:
                bad = args[0]
        if bad is not None:
            print(USAGE, file=sys.stderr)
            return 2
        row = run(repo=opts.get("repo"), source=_timer_source(opts))
        print("helm release nightly: %s%s — %s (trunk %s on %s, %d s; log %s)"
              % (row["verdict"], " at " + row["step"] if row["step"] else "",
                 row["reason"], (row["trunk"] or "unknown")[:12],
                 row["host"] or "no host", row["wall_s"], row["log"] or "none"))
        if not row["recorded"]:
            print("helm release nightly: this run could not be recorded in "
                  "%s, so its night reads MISSING" % history_path(),
                  file=sys.stderr)
            return 3
        return {GREEN: 0, RED: 1}.get(row["verdict"], 3)
    print(USAGE, file=sys.stderr)
    return 2
