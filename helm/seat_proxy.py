"""Proxy process lifecycle and smoke gates for :mod:`helm.seat`."""
import os
import shutil
import signal
import subprocess
import sys
import time


def _require_seat(family):
    if FAMILIES.get(family) is None:
        print("helm seat: unknown family '%s' (have: %s)"
              % (family, ", ".join(sorted(FAMILIES))), file=sys.stderr)
        return None
    if not os.path.exists(os.path.join(seat_dir(family), "config.yaml")):
        print("helm seat: no %s seat yet — `helm seat add %s`" % (family, family),
              file=sys.stderr)
        return None
    return FAMILIES[family]


def _up(family, quiet=False, seat=None):
    fam = _require_seat(family)
    if fam is None:
        return 1
    seat = seat or family
    ownership = _seat_surface_error(family, seat)
    if ownership:
        print("helm seat: " + ownership, file=sys.stderr)
        return 1
    # an instance proxy must have its own minted config before it can come up
    cfgd = _proxy_home(family, seat)
    if seat != family and not os.path.exists(os.path.join(cfgd, "config.yaml")):
        print("helm seat: no per-instance proxy for %s yet — `helm seat launch "
              "%s -i %s` mints it" % (seat, family, seat.rsplit("-", 1)[-1]),
              file=sys.stderr)
        return 1
    port = _instance_port(family, seat)
    # Serialize check→spawn→record under the per-home lock: without it two
    # concurrent _up calls both read an empty pidfile and double-start (the
    # atomic-ownership finding). The whole critical section runs under the
    # flock so the empty-check and the record-write are one atomic step.
    with _proxy_lock(family, seat):
        pid = _running_pid(family, seat)
        if pid:
            print("helm seat: %s proxy already running (pid %d, port %d) — "
                  "`helm seat down %s` first" % (seat, pid, port, seat),
                  file=sys.stderr)
            return 1
        b = _proxy_bin()
        if not b:
            print("helm seat: cli-proxy-api binary not found (HELM_PROXY_BIN, "
                  "%s, PATH all empty) — run `helm seat doctor`"
                  % PROXY_BIN_DEFAULT, file=sys.stderr)
            return 1
        # A pre-bound target is a collision ONLY once helm knows whose bind it
        # is. Spawning blind against it would either fight for the bind or,
        # worse, let _port_open read the STRANGER as readiness (the codex MED:
        # _up returned rc=0 + wrote proxy.pid against a foreign listener while
        # its own child lost the bind). But an open port is EQUALLY the
        # signature of this seat's OWN proxy running unrecorded — the state a
        # stale/dead pidfile leaves behind — and the old text asserted the
        # foreign case off a bool that cannot tell them apart. Resolve the
        # owner from /proc, then decide: adopt ours, refuse a NAMED stranger,
        # refuse an unidentified listener as UNKNOWN. Family bases/comments
        # prove no RUNTIME ownership; only the socket's holder does.
        if _port_open(port):
            return _adopt_or_refuse_port(seat, port, cfgd)
        config_path = os.path.join(cfgd, "config.yaml")
        try:
            launch_inputs = _proxy_launch_inputs(config_path, b)
        except OSError as exc:
            print("helm seat: cannot capture proxy launch inputs: %s" % exc,
                  file=sys.stderr)
            return 1
        log = open(os.path.join(cfgd, "proxy.log"), "ab")
        try:
            p = subprocess.Popen([b, "-config", config_path],
                                 stdout=log, stderr=log, start_new_session=True)
        except OSError as exc:
            print("helm seat: proxy failed to launch: %s" % exc, file=sys.stderr)
            return 1
        finally:
            log.close()
        # record pid + BIRTH identity so `_down`/`_running_pid` signal only THIS
        # incarnation — a reused pid is never proxied-on or killed (the bare
        # reusable-PID finding). Capture right after spawn; /proc can lag a tick,
        # so retry briefly. If identity is STILL unverifiable the proxy would be
        # unmanageable (fail-closed `_down` would refuse to ever signal it) — kill
        # the orphan and fail loudly rather than leave a proxy we cannot stop.
        ident = None
        for _ in range(10):
            ident = _pid_identity(p.pid)
            if ident or p.poll() is not None:
                break
            time.sleep(0.1)
        if not ident:
            p.kill()
            print("helm seat: proxy pid %d birth identity unverifiable — killed "
                  "the orphan rather than leave an unstoppable proxy (no /proc?)"
                  % p.pid, file=sys.stderr)
            return 1
        _write_private(
            os.path.join(cfgd, "proxy.pid"),
            "%d %s %s\n" % (p.pid, ident, _encode_launch_inputs(launch_inputs)))
        # readiness = OUR child alive AND the port open. Poll the child first:
        # a dead child with the port held by a late foreign listener must NOT
        # read as success (the other half of the codex MED). We already refused
        # a pre-bound port above, so an open port with a live child is ours.
        for _ in range(30):  # up to ~6s for the port to open
            if p.poll() is not None or _port_open(port):
                break
            time.sleep(0.2)
        if p.poll() is not None:
            os.remove(os.path.join(cfgd, "proxy.pid"))
            print("helm seat: proxy exited rc %s — tail %s"
                  % (p.returncode, os.path.join(cfgd, "proxy.log")), file=sys.stderr)
            return 1
        if not _port_open(port):
            # child alive but never bound (lost the race to a late listener, or
            # wedged): not a proxy we can reach — kill it, don't leave an
            # unmanageable record.
            p.kill()
            os.remove(os.path.join(cfgd, "proxy.pid"))
            print("helm seat: proxy pid %d alive but port %d never opened — "
                  "killed the orphan; tail %s"
                  % (p.pid, port, os.path.join(cfgd, "proxy.log")), file=sys.stderr)
            return 1
    if not quiet:
        print("helm seat: %s proxy up — 127.0.0.1:%d (pid %d)"
              % (seat, port, p.pid))
    return 0


def _down(family, seat=None):
    fam = _require_seat(family)
    if fam is None:
        return 1
    seat = seat or family
    ownership = _seat_surface_error(family, seat)
    if ownership:
        print("helm seat: " + ownership, file=sys.stderr)
        return 1
    pidfile = os.path.join(_proxy_home(family, seat), "proxy.pid")
    # Serialize verify→signal→unlink under the per-home lock (the atomic-
    # ownership finding): without it a concurrent _up/replacement can write a
    # FRESH pidfile after the old proxy exits, and an unconditional os.remove
    # then deletes the NEW record — leaving the new proxy alive but
    # unmanageable and eligible for a duplicate start.
    with _proxy_lock(family, seat):
        # ONE authenticated read, threaded through signal+unlink (the atomic-
        # ownership advisory): the verify and the kill act on the SAME owned
        # snapshot, so a transient re-read failure or malformed replacement
        # mid-sequence can never split authentication from action (the
        # None-subscript crash class fixed by "seat: streaming-leg
        # survival in proxy config") — the record is already in hand.
        owned = _running_pid_rec(family, seat)
        if not owned:
            # Distinguish a merely-dead proxy from a REUSED/unauthenticated pid:
            # a live process holding our recorded pid that we cannot prove is
            # ours is NOT our proxy — never signal it. Reap the stale file.
            rec = _proxy_pid_record(family, seat)
            if rec and _pid_alive(rec["pid"]):
                os.remove(pidfile)
                print("helm seat: %s proxy pidfile stale — pid %d now belongs to "
                      "an unrelated process (reused); NOT signalled, record "
                      "reaped. NOTE: this includes LEGACY bare-pid records from "
                      "pre-per-instance proxies (every proxy running at land). "
                      "The old proxy may STILL hold the port — to load the new "
                      "config, kill it by hand: `kill %d` (verify with "
                      "`ss -ltnp | grep :%d`), then `helm seat up %s`."
                      % (seat, rec["pid"], rec["pid"],
                         _instance_port(family, seat), seat))
                return 0
            if os.path.exists(pidfile):
                os.remove(pidfile)  # stale
            print("helm seat: %s proxy not running" % seat)
            return 0
        pid, expected = owned["pid"], owned["identity"]
        # TOCTOU guard: re-verify the birth identity IMMEDIATELY before each
        # signal. `owned` authenticated at entry, but the proxy could die and
        # its pid be recycled in the gap before a kill; a recycled pid has a
        # different starttime, so the recheck refuses to signal it. (pidfd would
        # close the window outright; /proc starttime narrows it to the
        # check→kill instant, which is the portable floor here.)

        def _still_ours():
            return _pid_alive(pid) and _pid_identity(pid) == expected

        if _still_ours():
            os.kill(pid, signal.SIGTERM)
        for _ in range(15):
            if not _pid_alive(pid):
                break
            time.sleep(0.2)
        if _still_ours():
            os.kill(pid, signal.SIGKILL)
        # Unlink ONLY the exact record this operation killed: re-read under the
        # lock and remove just if the pidfile still names THIS pid+birth. A
        # concurrent replacement's fresh record (different pid or birth) is
        # left intact.
        cur = _proxy_pid_record(family, seat)
        if cur and cur["pid"] == pid and cur["identity"] == expected \
                and os.path.exists(pidfile):
            os.remove(pidfile)
    print("helm seat: %s proxy stopped (pid %d)" % (seat, pid))
    return 0


# ---------------------------------------------------------------------------
# smoke — the 4-leg acceptance gate
# ---------------------------------------------------------------------------

def _seat_env(family, config_dir, multi=False):
    """The proxied-seat subprocess env: scrubbed base (so a stray inherited
    ANTHROPIC_API_KEY can never ride along), then the seat's own proxy, chat,
    and dregg-signing identity. Mirrors launch_line so smoke cannot certify a
    materially different process shape — including the child-stamp strip.
    `multi` mirrors launch_line's --multi: NO CLAUDE_CODE_SUBAGENT_MODEL (it
    would blunt-pin every subagent over the per-agent frontmatter — the proven
    mixed-model mechanism); the scrubbed base also guarantees no inherited pin
    leaks back in."""
    fam = FAMILIES[family]
    env = scrub_env(os.environ)
    for v in CHILD_STAMP_VARS:
        env.pop(v, None)
    env.pop("CLAUDE_CODE_SUBAGENT_MODEL", None)
    env.update({
        "ANTHROPIC_BASE_URL": "http://127.0.0.1:%d" % _instance_port(family),
        "ANTHROPIC_AUTH_TOKEN": _read_token(family) or "",
        "CLAUDE_CONFIG_DIR": config_dir,
        "HELM_CHAT_NAME": family,
        "HELM_CELL_BIN": DREGG_SIGNER_DEFAULT,
        "HELM_CELL_PROFILE": family,
        "DREGG_PROFILE": family,
        # coord_fee() default is 0 — exempt on devnet. Read the knob directly
        # (home.env) to avoid a cell→chat→seats→cell import cycle in _seat_env.
        "DREGG_COORDINATION_EXEMPT": "1" if home.env("NODE_COORD_FEE") in (None, "", "0") else "0",
    })
    if not multi:
        env["CLAUDE_CODE_SUBAGENT_MODEL"] = fam["model"]
    return env


def _smoke_multi_leg(family, fam, smoke_dir, mark):
    """The mixed-model fan-out leg (the proven run-1 pattern): TWO subagents
    pinned to DIFFERENT models via per-agent frontmatter, one claude-code
    process, no CLAUDE_CODE_SUBAGENT_MODEL. Verified against a CONDUCTOR LOG —
    per-request model names on the wire, not the subagents' word: the smoke
    claude rides through an ephemeral helm modelrouter fronting this seat's
    proxy, and the leg passes only when the router's log shows BOTH probe
    models leaving the process. (The stock proxy's gin log carries no model
    names at debug:false; the router's conductor log is the helm-owned
    equivalent of the debug log run-1 read.)"""
    probes = _mint_probe_agents(smoke_dir, family)
    if len(probes) < 2:
        print("  %-8s SKIP — %s has one probe model; the mixed fan-out needs "
              "two (FAMILIES probe_models)" % ("multi", family))
        return True
    from . import modelrouter
    log_path = os.path.join(smoke_dir, "router.log")
    if os.path.exists(log_path):
        os.remove(log_path)      # stale wire evidence must never certify a run
    srv = modelrouter.start_inprocess(default_family=family, log_path=log_path)
    (a_name, a_model), (b_name, b_model) = probes[0], probes[1]
    try:
        env = _seat_env(family, smoke_dir, multi=True)
        env["ANTHROPIC_BASE_URL"] = "http://127.0.0.1:%d" % srv.server_address[1]
        prompt = ("Use the Task tool to spawn exactly two subagents in "
                  "parallel: one with subagent_type %s whose entire task is to "
                  "reply with exactly helm-seat-multi-a-%s, and one with "
                  "subagent_type %s whose entire task is to reply with exactly "
                  "helm-seat-multi-b-%s. Report both replies verbatim."
                  % (a_name, mark, b_name, mark))
        try:
            p = subprocess.run(["claude", "-p", prompt, "--model", fam["model"],
                                "--allowedTools", "Task"],
                               env=env, capture_output=True, text=True,
                               timeout=600)
            reply, note = (p.stdout or "").strip(), ""
        except subprocess.TimeoutExpired:
            p, reply, note = None, "", " (timeout 600s)"
    finally:
        srv.shutdown()
        srv.server_close()
    wired = modelrouter.logged_models(log_path)
    markers = ("helm-seat-multi-a-%s" % mark in reply
               and "helm-seat-multi-b-%s" % mark in reply)
    routed = {a_model, b_model} <= wired
    passed = p is not None and p.returncode == 0 and markers and routed
    if not passed and not note:
        note = " (rc %s%s%s)" % (
            p.returncode if p else "-",
            "" if markers else "; marker missing",
            "" if routed else "; conductor log saw %s, wanted %s+%s"
            % (sorted(wired) or "nothing", a_model, b_model))
    print("  %-8s %s%s — %s" % ("multi", "PASS" if passed else "FAIL", note,
                                (reply or "(no reply)").replace("\n", " ")[:200]))
    return passed


def _smoke(family, multi=False):
    fam = _require_seat(family)
    if fam is None:
        return 1
    ownership = _seat_surface_error(family, family)
    if ownership:
        print("helm seat: " + ownership, file=sys.stderr)
        return 1
    if not shutil.which("claude"):
        print("helm seat: `claude` not on PATH — cannot smoke", file=sys.stderr)
        return 1
    if not _running_pid(family):
        if _up(family, quiet=True) != 0:
            return 1
        print("  (proxy was down — auto-started)")
    model = fam["model"]
    smoke_dir = os.path.join(seat_dir(family), "smoke-claude")
    shutil.rmtree(smoke_dir, ignore_errors=True)
    os.makedirs(smoke_dir)
    mark = "%d" % (time.time() % 100000)
    legs = (
        ("prompt", "Say which model family you are in one sentence.", None, None),
        ("tool", "Run exactly this command with the Bash tool: "
                 "echo helm-seat-tool-proof-%s — then report the command "
                 "output verbatim." % mark,
         "Bash(echo:*)", "helm-seat-tool-proof-%s" % mark),
        ("subagent", "Use the Task tool to spawn one subagent whose entire "
                     "task is to reply with exactly: helm-seat-subagent-ok-%s "
                     "plus its model family. Report the subagent's reply "
                     "verbatim." % mark,
         "Task", "helm-seat-subagent-ok-%s" % mark),
    )
    ok = True
    for name, prompt, allowed, marker in legs:
        # prompt rides directly after -p: --allowedTools is variadic and
        # swallows a trailing positional (live-found 2026-07-18)
        cmd = ["claude", "-p", prompt, "--model", model]
        if allowed:
            cmd += ["--allowedTools", allowed]
        try:
            p = subprocess.run(cmd, env=_seat_env(family, smoke_dir),
                               capture_output=True, text=True, timeout=300)
            reply = (p.stdout or "").strip()
            passed = p.returncode == 0 and reply and (marker is None or marker in reply)
            note = "" if passed else " (rc %s%s)" % (
                p.returncode, "; marker missing" if marker and reply else "")
        except subprocess.TimeoutExpired:
            reply, passed, note = "", False, " (timeout 300s)"
        ok = ok and passed
        print("  %-8s %s%s — %s" % (name, "PASS" if passed else "FAIL", note,
                                    (reply or "(no reply)").replace("\n", " ")[:200]))
    if multi:
        ok = _smoke_multi_leg(family, fam, smoke_dir, mark) and ok
    print("  %-8s SKIP — whisper: not yet wired" % "whisper")
    print("helm seat: %s smoke %s (model %s)" % (family, "PASS" if ok else "FAIL", model))
    return 0 if ok else 1
