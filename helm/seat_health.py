"""Seat status, drift diagnostics, and proxy health for :mod:`helm.seat`."""
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time


def _minted_instances(family):
    """[seat] every instance with its OWN minted proxy (config.yaml under
    instances/<seat>/) — the per-instance-proxy fleet, sorted numerically so
    codex-2 precedes codex-10."""
    root = os.path.join(seat_dir(family), "instances")
    out = []
    for name in (os.listdir(root) if os.path.isdir(root) else []):
        if os.path.exists(os.path.join(root, name, "config.yaml")):
            out.append(name)
    def _key(s):
        m = re.match(r"^%s-(\d+)$" % re.escape(family), s)
        return (0, int(m.group(1))) if m else (1, s)
    return sorted(out, key=_key)


def _minted_seats():
    """(family, seat) for every MINTED proxy — family seats with a config.yaml
    plus each per-instance proxy. The ONE enumeration doctor/--ensure/the CPU
    canary all walk, so no surface can silently see a different fleet."""
    for family in sorted(FAMILIES):
        if not os.path.exists(os.path.join(seat_dir(family), "config.yaml")):
            continue                      # never minted: nothing to supervise
        for s in [family] + _minted_instances(family):
            yield family, s


def _proxy_live_text(family, seat=None):
    """One authenticated process record drives both liveness and drift text.

    THIS SURFACE MUST NOT SAY "down" FOR A LISTENING PROXY. Measured 2026-07-29:
    all three codex proxies read "proxy down" while pid 815805 held :8319 and
    answered in 0.9ms, because their pidfiles are LEGACY BARE-PID (no captured
    birth identity) and the fail-closed ladder correctly refused them. The
    refusal was right; rendering it as absence was not, and "down" routes a
    maintainer to respawn a process whose pidfile — not whose liveness — is the
    problem. `seat doctor --ensure` read the identical signal and said
    "alive but unverifiable, refusing to signal": same input, honest output.
    """
    # `_running_pid_rec` STAYS the primary seam. It is what every other caller
    # and test mocks, and moving the renderer off it silently broke three
    # integration tests whose patches stopped intercepting — the reason lookup
    # is an ADDITION on the negative path, never a replacement for the read.
    rec = _running_pid_rec(family, seat)
    pid = rec["pid"] if rec else None
    port = _instance_port(family, seat)
    why = raw = None
    if not pid:
        _, why, raw = _proxy_pid_verdict(family, seat)
    if pid:
        live = "proxy UP pid %d port %d%s" % (
            pid, port, "" if _port_open(port) else " (port not answering!)")
    elif why in (PROXY_UNVERIFIABLE, PROXY_PID_REUSED):
        # An OPEN PORT is positive evidence a proxy is serving, independent of
        # the pidfile — so it settles liveness even when identity is unknowable.
        # A closed port leaves both unknown, and UNKNOWN is the honest word.
        rawpid = (raw or {}).get("pid")
        # name the CAUSE, not the verdict slug — "(unverifiable)" restates the
        # word it follows and tells the reader nothing they can act on
        cause = ("legacy bare-pid, no birth identity"
                 if why == PROXY_UNVERIFIABLE else "pid reused since launch")
        live = ("proxy UP pid %s port %d — UNVERIFIABLE pidfile (%s): re-mint "
                "the pidfile or check creds, do NOT respawn" % (rawpid, port, cause)
                if _port_open(port) else
                "proxy UNKNOWN pid %s port %d — %s AND port silent"
                % (rawpid, port, cause))
    else:
        live = "proxy down"
    # THE PROVIDER WALL RIDES EVERY PATH. "proxy UP" is a claim about the
    # LOCAL process; a seat whose provider is refusing it is not usable, and
    # the column said UP for three hours while codex was hard-walled. Appended
    # after the drift marker so both can show — they are different failures.
    live += upstream_phrase(family, compact=True)
    status, detail = proxy_drift(family, seat, record=rec)
    if status == PROXY_STALE:
        return live + " ⚠ STALE", detail
    if status == PROXY_UNKNOWN:
        return live + " ⚠ drift UNKNOWN", "UNKNOWN — " + detail
    return live, None


def _seat_row(family):
    d = seat_dir(family)
    fam = FAMILIES.get(family) or {}
    # One authority for "what cred does this seat have" (cred_state), so an
    # unreadable cred can never render here as a verdict about the cred.
    # PRECEDENCE PRESERVED from the pre-fix reader: a pooled cred file speaks
    # even for a proxy-key family (mode "proxy-key" bakes its key into
    # config.yaml, but a pooled json in the auth-dir is still what the proxy
    # hot-reloads, and reporting the baked key over it would hide a real cred).
    auth = os.path.join(d, "auth")
    state, detail, email = cred_state(auth)
    # The proxy-key gate is the PRE-FIX PREDICATE VERBATIM ("if creds:"), and it
    # has to be: for mode "proxy-key" the key is baked into config.yaml, so NO
    # pooled cred file is the normal, healthy state — whether the auth-dir is
    # empty or was never created at all. Gating on CRED_ABSENT alone regressed
    # this: the live ds4pro and kimi seats have no auth-dir, which is UNKNOWN
    # (correctly, for a family that reads creds from there), and they rendered a
    # do-NOT-respawn warning in place of "api-key cred". Caught by dogfooding
    # `helm seat doctor`, NOT by the unit test, whose fixture created the dir.
    if fam.get("mode") == "proxy-key" \
            and not glob.glob(os.path.join(auth, "*.json")):
        cred = "api-key cred (baked into config.yaml)"
    else:
        cred = "%s — %s" % (email, detail) if email else detail
    live, detail = _proxy_live_text(family)
    details = [(family, detail)] if detail else []
    row = "%-8s %-38s %s" % (family, live, cred)
    # per-instance proxies: each minted instance reports its OWN proxy fate
    for inst in _minted_instances(family):
        ilive, idetail = _proxy_live_text(family, inst)
        row += "\n  %-6s %-38s" % (inst, ilive)
        if idetail:
            details.append((inst, idetail))
    if family == "codex":   # slice 6: live-instance / pooled-capacity suffix
        try:
            from . import codexhomes, seats as _seats
            now = time.time()
            live_n = 0
            for s, r in _seats.roster().items():
                if s == "codex" or s.startswith("codex-"):
                    ls = _seats.last_seen(s, r)
                    if ls and now - ls < _seats.QUIET_S:
                        live_n += 1
            row += "  [instances: %d live / cap %d]" % (
                live_n, codexhomes.capacity()["total"])
        except Exception:
            pass
    if details:
        row += "\n" + "\n".join("  ⚠ %s: %s" % item for item in details)
    return row


def _status(args):
    root = seats_root()
    fams = sorted(f for f in (os.listdir(root) if os.path.isdir(root) else [])
                  if os.path.isdir(os.path.join(root, f)))
    if not fams:
        print("helm seat: no seats yet — `helm seat add codex`")
        return 0
    for f in fams:
        print(_seat_row(f))
    return 0


# ---------------------------------------------------------------------------
# config drift — the generator's promise vs the file a proxy is actually running
# ---------------------------------------------------------------------------
# WHY A CENSUS AND NOT A REGENERATION. A live config carries per-family truth a
# regeneration would have to re-derive (port, model, upstream alias, the 0600
# token), so this compares only the INVARIANTS both generators emit identically
# for every family, and leaves everything else alone.
#
# MEASURED 2026-07-29: ds4pro's config carried NO nonstream-keepalive-interval
# while kimi's — written in the same second, by the same generator path —
# carried 15. Neither had been re-minted; both had been hand-patched, and
# ds4pro was skipped. `_config_yaml`'s own docstring names the consequence: a
# long non-streaming request (a compaction's ~360k summarize is the longest one
# a session makes) sits silent while the upstream thinks, the proxy reaps the
# idle socket, and Claude Code receives an EMPTY HTTP 200. From the outside
# that is a seat that simply went quiet — which is what the owner had been
# reporting about ds4pro for a week, as unreliability.
#
# The docstring even predicted the shape: "the live family configs carry 15s by
# hand; the generator must emit it too or every re-mint silently strips the
# fix". The generator was fixed. Nothing ever checked the files that were never
# re-minted, so a hand-patch that missed one family stayed missed. A config
# nobody re-mints is a config nobody re-checks — this is the check.

_CONFIG_INVARIANTS = (
    ("nonstream-keepalive-interval", "15",
     "a long non-streaming pass (a compaction summarize) gets an EMPTY HTTP "
     "200 when the proxy reaps the idle socket — the seat goes silent"),
)


def _config_values(path):
    """{key: value} for the config's TOP-LEVEL scalars, comments stripped.

    Deliberately not a YAML parse: helm ships stdlib-only, the invariants are
    all top-level scalars, and a config that carries a token must never be
    round-tripped through a writer that could reformat it.
    """
    out = {}
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for ln in f:
                if not ln[:1].strip() or ln.lstrip().startswith("#"):
                    continue          # indented => nested; '#' => comment
                key, sep, rest = ln.partition(":")
                if sep and key.strip() and " " not in key.strip():
                    out[key.strip()] = rest.split("#")[0].strip().strip('"')
    except OSError:
        return None
    return out


def config_drift(path):
    """[(key, want, got, why)] — generator invariants this config does not
    carry. `got` is None when the key is absent entirely, which is the shape
    that bit ds4pro. An unreadable config reports no drift rather than a false
    one: doctor must not turn a permissions problem into a config alarm."""
    vals = _config_values(path)
    if vals is None:
        return []
    return [(k, want, vals.get(k), why)
            for k, want, why in _CONFIG_INVARIANTS if vals.get(k) != want]


def _config_drift_lines():
    """One line per live proxy config that has drifted from the generator."""
    out = []
    for family, seat in _minted_seats():
        path = os.path.join(_proxy_home(family, seat), "config.yaml")
        if not os.path.exists(path):
            continue
        for key, want, got, why in config_drift(path):
            out.append("config drift: %-9s %s is %s, want %s — %s"
                       % (seat, key, "ABSENT" if got is None else repr(got),
                          want, why))
    return out


def _doctor(args):
    b = _proxy_bin()
    if b:
        try:
            v = subprocess.run([b, "--version"], capture_output=True, text=True,
                               timeout=5).stdout.strip().splitlines()
            print("proxy binary: %s (%s)" % (b, v[0] if v else "version unknown"))
        except (OSError, subprocess.TimeoutExpired):
            print("proxy binary: %s (present, --version failed)" % b)
    else:
        print("proxy binary: MISSING — install CLIProxyAPI to %s, e.g.\n"
              "  gh release download v7.2.88 --repo router-for-me/CLIProxyAPI "
              "--pattern 'CLIProxyAPI_*_linux_amd64.tar.gz'" % PROXY_BIN_DEFAULT)
    c = shutil.which("claude")
    print("claude binary: %s" % (c or "MISSING from PATH"))
    cstate, src, cdetail = codex_cred_state()
    print("codex cred: %s (newest valid, access token until %s)"
          % (src, _rfc3339(_cred_exp(src))) if src else "codex cred: " + cdetail)
    _status([])
    # proxy-CPU canary per live proxy — the struggling-backend leading
    # indicator (a DOWN proxy is the status rows'/--ensure's story, not ours)
    for family, seat in _minted_seats():
        pid = _running_pid(family, seat)
        if pid:
            cstate, pct, window, note = _cpu_canary(family, seat, pid)
            print("cpu canary: %-9s %-9s %s"
                  % (seat, cstate.upper(), _canary_text(cstate, pct, window, note)))
    drift = _config_drift_lines()
    for ln in drift:
        print(ln)
    try:      # proxy-seat context% + autocompact latch — read-only visibility
        from . import autocompact
        for ln in autocompact.report_lines():
            print(ln)
    except Exception as e:
        print("autocompact: report unavailable (%s)" % e)
    # EXIT STATUS IS A VERDICT TOO, and it was the last place the old fold
    # survived: gating on `not err` failed a healthy fully-pooled fleet (see
    # codex_cred_state). Only a REAL negative fails. CRED_UNKNOWN does not:
    # it is printed loudly with do-NOT guidance, exactly as `helm doctor` files
    # every unreadable case as WARN and reserves exit 1 for a proven FAIL —
    # an exit 1 here would tell a machine "this fleet's creds are broken" on
    # the strength of a probe that could not see them.
    return 0 if b and c and not drift \
        and cstate not in (CRED_EXPIRED, CRED_ABSENT) else 1


# A live proxy still binding its port at startup must never read as WEDGED —
# the fable adversarial MED: two back-to-back 0.5s connect probes with no grace
# let a just-launched proxy be SIGTERMed, and cron firing inside the boot window
# churns kill->respawn->kill. Age source = pidfile mtime: _up writes the pidfile
# atomically at spawn, so mtime ~= launch time and stays readable even when
# /proc is restricted. Env-tunable for slow hosts / tests.
_ENSURE_STARTUP_GRACE_S = float(os.environ.get("HELM_ENSURE_STARTUP_GRACE", "15"))


def _proxy_age_s(family, seat):
    """Seconds since this proxy's pidfile was written (~= launch time), or None
    when there is no pidfile to age. mtime is the portable birth proxy: it does
    not depend on /proc readability and _up stamps it at spawn."""
    try:
        return time.time() - os.path.getmtime(
            os.path.join(_proxy_home(family, seat), "proxy.pid"))
    except OSError:
        return None


# ---------------------------------------------------------------------------
# proxy-CPU canary — the leading indicator BEFORE a proxy goes silent
# ---------------------------------------------------------------------------
# Owner evidence (htop, 2026-07-23): cli-proxy-api pids at 152% and 90.6% CPU
# while healthy siblings idle at ~0% — a proxy pegged at SUSTAINED high CPU is
# a struggling/looping backend for that seat's model, and the precursor of the
# silent death doctor --ensure heals after the fact. The canary reads
# /proc/<pid>/stat utime+stime as a WINDOW, never a point: %CPU over the span
# since the stored prior sample (tmpfs, a cron cadence apart) when one exists,
# else a short in-process double-read — a single high reading never classifies.

_CPU_SAMPLE_MAX_AGE_S = 900   # a stored sample older than this is history,
                              # not a window — fall back to a fresh double-read


def _env_float(name, default):
    try:
        return float(home.env(name, default))
    except ValueError:
        return float(default)


def _proc_cpu_sample(pid):
    """One /proc reading for a pid: {jiffies, age_s, clk, ts} or None when
    /proc cannot be read (gone pid, no /proc, permission) — the caller must
    surface UNKNOWN, never OK (no false-absence). jiffies is cumulative
    utime+stime; age_s is process age, because startup/model-load bursts are
    normal and must not read as thrash."""
    try:
        with open("/proc/%d/stat" % int(pid)) as f:
            tail = f.read().rsplit(")", 1)[1].split()
        with open("/proc/uptime") as f:
            uptime = float(f.read().split()[0])
        clk = os.sysconf("SC_CLK_TCK") or 100
        return {"jiffies": int(tail[11]) + int(tail[12]),
                "age_s": max(0.0, uptime - int(tail[19]) / clk),
                "clk": clk, "ts": time.time()}
    except (OSError, ValueError, IndexError, TypeError):
        return None


def _cpu_sample_path(seat):
    """Where a seat's prior jiffies reading lives BETWEEN doctor runs — RAM
    (tmpfs) when the host has it: the sample is disposable derived state, not
    seat fate, and must not touch the proxy home. HELM_PROXY_CPU_DIR pins it
    (tests); losing it merely degrades to the double-read path."""
    base = home.env("PROXY_CPU_DIR")
    if not base:
        base = os.path.join("/dev/shm", "helm-cpu-canary-%d" % os.getuid()) \
            if os.path.isdir("/dev/shm") \
            else os.path.join(seats_root(), ".cpu-canary")
    os.makedirs(base, mode=0o700, exist_ok=True)
    return os.path.join(base, "%s.json" % seat)


def _cpu_canary(family, seat, pid):
    """Tri-state CPU verdict for a LIVE verified proxy pid: (state, pct,
    window_s, note), state "ok" | "thrashing" | "unknown". SUSTAINED beats
    spike: the %CPU window is the span since the stored prior reading when one
    exists for this pid (cron cadence = the real sustain), else a short
    double-read (HELM_PROXY_CPU_CANARY_WINDOW_S, default 1s). Thrash =
    >= HELM_PROXY_CPU_CANARY_PCT (default 80) over the window, UNLESS the
    process is younger than HELM_PROXY_CPU_CANARY_GRACE_S (default 60) —
    startup bursts are normal. Unreadable /proc is UNKNOWN, not OK."""
    now = _proc_cpu_sample(pid)
    if now is None:
        return ("unknown", None, None, "unreadable /proc/%s/stat" % pid)
    path = _cpu_sample_path(seat)
    prior = None
    try:
        with open(path) as f:
            rec = json.load(f)
        if rec.get("pid") == pid and \
                1.0 <= now["ts"] - rec.get("ts", 0) <= _CPU_SAMPLE_MAX_AGE_S:
            prior = rec
    except (OSError, ValueError):
        prior = None                     # no/corrupt store: double-read below
    if prior is None:
        # first sight of this pid (or a stale/foreign sample): a short
        # double-read gives a real window — a single reading never classifies.
        time.sleep(min(max(_env_float("PROXY_CPU_CANARY_WINDOW_S", "1.0"),
                           0.1), 10.0))
        second = _proc_cpu_sample(pid)
        if second is None:
            return ("unknown", None, None, "pid %s vanished mid-sample" % pid)
        prior, now = now, second
    try:
        with open(path, "w") as f:
            json.dump({"pid": pid, "jiffies": now["jiffies"],
                       "ts": now["ts"]}, f)
    except OSError:
        pass          # losing the store degrades to double-read, never crashes
    window = now["ts"] - prior["ts"]
    if window <= 0:
        return ("unknown", None, None, "non-positive sample window (clock skew)")
    pct = max(0.0, now["jiffies"] - prior["jiffies"]) / now["clk"] / window * 100
    threshold = _env_float("PROXY_CPU_CANARY_PCT", "80")
    if pct < threshold:
        return ("ok", pct, window, "")
    grace = _env_float("PROXY_CPU_CANARY_GRACE_S", "60")
    if now["age_s"] < grace:
        return ("ok", pct, window, "startup burst — %.0fs old, grace %.0fs"
                % (now["age_s"], grace))
    return ("thrashing", pct, window, ">=%.0f%% threshold" % threshold)


def _canary_text(cstate, pct, window, note):
    """One human line for a canary verdict — shared by doctor and --ensure so
    the two surfaces can never describe the same proxy differently."""
    if cstate == "thrashing":
        return ("cpu %.0f%% sustained %.0fs (%s) — backend struggling"
                % (pct, window, note))
    if cstate == "unknown":
        return "cpu UNKNOWN (%s)" % note
    return "cpu %.0f%% over %.0fs%s" % (pct, window,
                                        " (%s)" % note if note else "")


def _ensure_row(family, seat):
    """One proxy's supervise-verdict: (label, state, detail). state is
    "healthy" | "respawned" | "unknown". The reconciler's whole job is to make
    every row provably one of the first two; a row it cannot prove is UNKNOWN,
    never a silent down/up (the fleet-truth fail-closed law)."""
    label = family if seat == family else seat
    port = _instance_port(family, seat)
    rec = _proxy_pid_record(family, seat)
    live = _running_pid(family, seat)
    # The discriminant is the LIVENESS of the recorded pid, not record-presence:
    #  - dead recorded pid   -> a STALE pidfile of a crashed proxy (the silent-
    #    starvation case the watchdog exists to heal). Fall through to respawn;
    #    _up's empty-check reads _running_pid (None for a corpse) and overwrites.
    #  - ALIVE recorded pid but _running_pid None -> identity verification FAILED
    #    on a live process: a REUSED pid now owned by a stranger (never signal)
    #    or a legacy bare-pid proxy (running but unverifiable). Both UNKNOWN —
    #    refuse to signal and refuse to respawn over a live foreign listener.
    if rec and not live and _pid_alive(rec["pid"]):
        return (label, "unknown",
                "pidfile pid %d alive but unverifiable (reused or legacy "
                "bare-pid) — refusing to signal or respawn over it" % rec["pid"])
    if live and _port_open(port):
        return (label, "healthy", "pid %d port %d" % (live, port))
    # down (no live pid / stale record) or wedged (live pid, port not answering).
    if live and not _port_open(port):
        # STARTUP GRACE: a YOUNG non-answering proxy is STARTING, not wedged —
        # never SIGTERM it. Surface as unknown (still binding) and leave it for
        # the next cron cycle; only a proxy old enough to have bound AND still
        # failing the probe is truly wedged.
        age = _proxy_age_s(family, seat)
        if age is not None and age < _ENSURE_STARTUP_GRACE_S:
            return (label, "unknown",
                    "pid %d launched %.0fs ago, port %d not answering yet — "
                    "STARTING (grace %.0fs), not wedged; left for next cycle"
                    % (live, age, port, _ENSURE_STARTUP_GRACE_S))
        # wedged: a live verified process past its grace and still not serving.
        # Signal it away, then respawn.
        _down(family, seat)
    rc = _up(family, quiet=True, seat=seat)
    if rc != 0:
        # concurrent-_up loser race (fable LOW): a seat launching in the same
        # instant wins the flock, our _up reads 'already running' (rc 1) — that
        # is not a failure, the row is now HEALTHY under the winner. Re-probe
        # before crying UNKNOWN.
        pid = _running_pid(family, seat)
        if pid and _port_open(port):
            return (label, "healthy",
                    "pid %d port %d (a concurrent starter won the race)" %
                    (pid, port))
        return (label, "unknown", "respawn failed (rc %d); see proxy.log" % rc)
    pid = _running_pid(family, seat)
    if pid and _port_open(port):
        return (label, "respawned", "pid %d port %d" % (pid, port))
    return (label, "unknown", "post-respawn probe could not prove healthy")


def _ensure(args):
    """doctor --ensure: supervise every minted family+instance proxy. Reuse the
    landed ownership primitives — never a second spawn path. A healthy row
    also runs the proxy-CPU canary: a pegged proxy is a struggling backend
    BEFORE it goes silent (the leading indicator; the respawn is the trailing
    one). rc 0 all proven ok; rc 1 WARN — a THRASHING or cpu-UNKNOWN canary on
    an otherwise-live proxy; rc 2 when any liveness row is UNKNOWN (a row the
    watchdog could not prove), so a cron line can page on 2 alone. --json
    emits the same rows for a board/console."""
    as_json = "--json" in args
    unknown = thrash = cpu_unknown = 0
    rows = []
    for family, seat in _minted_seats():
        label, state, detail = _ensure_row(family, seat)
        cpu = None
        if state == "unknown":
            unknown += 1
        elif state == "healthy":
            # canary only on a proven-live row: DOWN just respawned (its own
            # tri-state arm), and a fresh respawn is inside its startup burst
            # by definition. A pid that vanished between the row's probe and
            # ours is UNKNOWN, never OK (no false-absence).
            pid = _running_pid(family, seat)
            cpu = _cpu_canary(family, seat, pid) if pid else \
                ("unknown", None, None, "pid vanished between probes")
        shown = state
        if cpu is not None:
            cstate = cpu[0]
            if cstate == "thrashing":
                thrash += 1
                shown = "thrashing"       # the tri-state's middle arm, surfaced
            elif cstate == "unknown":
                cpu_unknown += 1
            detail += " — " + _canary_text(*cpu)
        if as_json:
            rows.append({"seat": label, "family": family, "state": state,
                         "shown": shown, "detail": detail,
                         "cpu": None if cpu is None else
                         {"state": cpu[0], "pct": cpu[1],
                          "window_s": cpu[2], "note": cpu[3]}})
        else:
            print("%-10s %-9s %s" % (label, shown.upper(), detail))
    rc = 2 if unknown else (1 if thrash or cpu_unknown else 0)
    if as_json:
        print(json.dumps({"rows": rows, "unknown": unknown,
                          "thrashing": thrash, "cpu_unknown": cpu_unknown,
                          "rc": rc}, indent=2, sort_keys=True))
    if unknown:
        print("helm seat doctor --ensure: %d UNKNOWN row(s) — a proxy the "
              "watchdog could not prove healthy; investigate" % unknown,
              file=sys.stderr)
    elif thrash or cpu_unknown:
        print("helm seat doctor --ensure: WARN — %d THRASHING / %d cpu-UNKNOWN "
              "row(s); a pegged proxy is a struggling backend (the leading "
              "indicator before silent death)" % (thrash, cpu_unknown),
              file=sys.stderr)
    return rc
