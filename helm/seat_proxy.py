"""Proxy process lifecycle and smoke gates for :mod:`helm.seat`."""
import os
import shutil
import signal
import select
import stat
import subprocess
import sys
import time

from .seat_paths import (_commit_private, _discard_private,
                         _is_project_instance, _mgmt_secret, _stage_private)


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


def _owned_process_state(owned):
    """Classify one record without treating a recycled pid as the old process."""
    pid, expected = owned["pid"], owned["identity"]
    if not _pid_alive(pid):
        return "gone"
    actual = _pid_identity(pid)
    if actual is None:
        return "unknown"
    return "ours" if actual == expected else "reused"


def _unlink_owned_record(family, seat, owned):
    pidfile = os.path.join(_proxy_home(family, seat), "proxy.pid")
    current = _proxy_pid_record(family, seat)
    if current and current["pid"] == owned["pid"] \
            and current["identity"] == owned["identity"] \
            and os.path.exists(pidfile):
        os.remove(pidfile)


def _pidfd_exited(fd):
    poller = select.poll()
    poller.register(fd, select.POLLIN)
    return bool(poller.poll(0))


def _stop_owned_proxy(family, seat, owned, force=False):
    """Stop exactly ``owned`` through a lifetime-bound pidfd."""
    pid = owned["pid"]
    if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
        return False, "pidfd signalling unavailable; NOT signalled"
    try:
        fd = os.pidfd_open(pid)
    except ProcessLookupError:
        _unlink_owned_record(family, seat, owned)
        return True, None
    except OSError as exc:
        return False, "pid %d pidfd open failed: %s; NOT signalled" % (pid, exc)
    try:
        state = _owned_process_state(owned)
        if state == "unknown":
            return False, "pid %d birth identity became unreadable; NOT signalled" % pid
        if state == "reused":
            return False, "pid %d birth identity changed before signal; NOT signalled" % pid
        if state == "gone" or _pidfd_exited(fd):
            _unlink_owned_record(family, seat, owned)
            return True, None
        try:
            signal.pidfd_send_signal(fd, signal.SIGTERM)
        except ProcessLookupError:
            _unlink_owned_record(family, seat, owned)
            return True, None
        except OSError as exc:
            return False, "pid %d SIGTERM failed: %s" % (pid, exc)
        for _ in range(25):
            if _pidfd_exited(fd):
                _unlink_owned_record(family, seat, owned)
                return True, None
            time.sleep(0.2)
        if not force:
            return False, "pid %d survived SIGTERM; old listener retained" % pid
        try:
            signal.pidfd_send_signal(fd, signal.SIGKILL)
        except ProcessLookupError:
            _unlink_owned_record(family, seat, owned)
            return True, None
        except OSError as exc:
            return False, "pid %d SIGKILL failed: %s" % (pid, exc)
        for _ in range(25):
            if _pidfd_exited(fd):
                _unlink_owned_record(family, seat, owned)
                return True, None
            time.sleep(0.2)
        return False, "pid %d survived SIGKILL; ownership record retained" % pid
    finally:
        os.close(fd)


def _proxy_binary_probe(path):
    try:
        mode = os.stat(path).st_mode
        if not stat.S_ISREG(mode) or not os.access(path, os.X_OK):
            return False, "not an executable regular file", ""
        probe = subprocess.run([path, "-h"], stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, text=True, timeout=5,
                               check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc), ""
    if probe.returncode != 0:
        return False, "-h exited %d" % probe.returncode, probe.stdout
    if not any(line.lstrip().startswith("-config ")
               for line in probe.stdout.splitlines()):
        return False, "-h did not advertise the required -config flag", probe.stdout
    return True, None, probe.stdout


def _proxy_binary_ready(path):
    ready, why, unused_output = _proxy_binary_probe(path)
    return ready, why


def _launch_proxy_process(binary, config_path, cfgd, port):
    try:
        launch_inputs = _proxy_launch_inputs(config_path, binary)
    except OSError as exc:
        return None, "cannot capture proxy launch inputs: %s" % exc
    try:
        log = open(os.path.join(cfgd, "proxy.log"), "ab")
    except OSError as exc:
        return None, "proxy log cannot open: %s" % exc
    # THE METER'S DOOR OPENS HERE (task/2522). The fork registers its
    # management routes, and with them the usage queue the reader pops, only
    # when a management secret exists at start; MANAGEMENT_PASSWORD is the
    # one spelling that never lands in config.yaml (a secret-key there is
    # bcrypted and written back, which the reconcile would fight every pass).
    # It rides the ENVIRONMENT, never argv: /proc/<pid>/cmdline is
    # world-readable, environ is not. The listener binds loopback, so the
    # remote-management override the env spelling implies reaches nobody.
    env = dict(os.environ)
    try:
        env["MANAGEMENT_PASSWORD"] = _mgmt_secret(cfgd)
    except OSError as exc:
        log.close()
        return None, "management secret cannot be minted: %s" % exc
    try:
        process = subprocess.Popen(
            [binary, "-config", config_path], stdout=log, stderr=log,
            start_new_session=True, env=env)
    except OSError as exc:
        return None, "proxy failed to launch: %s" % exc
    finally:
        log.close()
    ident = None
    for _ in range(10):
        ident = _pid_identity(process.pid)
        if ident or process.poll() is not None:
            break
        time.sleep(0.1)
    if not ident:
        process.kill()
        return None, ("proxy pid %d birth identity unverifiable — killed the "
                      "orphan rather than leave an unstoppable proxy (no /proc?)"
                      % process.pid)
    pidfile = os.path.join(cfgd, "proxy.pid")
    try:
        _write_private(
            pidfile, "%d %s %s\n" %
            (process.pid, ident, _encode_launch_inputs(launch_inputs)))
    except OSError as exc:
        process.kill()
        return None, "proxy pid record failed (%s); killed unowned child" % exc
    for _ in range(30):
        if process.poll() is not None or _port_open(port):
            break
        time.sleep(0.2)
    if process.poll() is not None:
        os.remove(pidfile)
        return None, "proxy exited rc %s — tail %s" % (
            process.returncode, os.path.join(cfgd, "proxy.log"))
    if not _port_open(port):
        process.kill()
        os.remove(pidfile)
        return None, ("proxy pid %d alive but port %d never opened — killed the "
                      "orphan; tail %s" %
                      (process.pid, port, os.path.join(cfgd, "proxy.log")))
    return process, None


def _mint_hint(family, seat):
    """The verb that ACTUALLY mints this instance's assets.

    THE RECOVERY A REFUSAL OFFERS MUST BE ONE SOME DOOR ADMITS. `seat launch
    <family> -i N` mints a NUMBERED instance, and the last segment of a
    project-canonical name is the family rather than a number, so that phrasing
    told the operator to run `launch codex -i codex` for `proj-a-codex` — a
    second refusal, which reads as helm contradicting itself. A project instance
    is minted by its own spawn.
    """
    if _is_project_instance(family, seat):
        return "helm seat spawn %s" % seat
    return "helm seat launch %s -i %s" % (family, seat.rsplit("-", 1)[-1])


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
        print("helm seat: no per-instance proxy for %s yet — `%s` mints it"
              % (seat, _mint_hint(family, seat)), file=sys.stderr)
        return 1
    # THE READER'S QUESTION, not admission's: this instance's config.yaml was
    # proven to exist two lines up, so the port it NAMES is the endpoint a proxy
    # would be started on. `_instance_port` answers admission and returns None
    # for a project instance whose ledger entry is gone, and every `port %d`
    # below would then format a None and take `seat up` down with a TypeError
    # about string formatting instead of a sentence about this seat.
    port = _existing_instance_port(family, seat)
    if port is None:
        print("helm seat: %s has a proxy config but helm cannot resolve its "
              "endpoint — neither the allocation ledger nor %s names a port; "
              "re-mint it with `%s`"
              % (seat, os.path.join(cfgd, "config.yaml"),
                 _mint_hint(family, seat)), file=sys.stderr)
        return 1
    # Serialize check→spawn→record under the per-home lock: without it two
    # concurrent _up calls both read an empty pidfile and double-start (the
    # atomic-ownership finding). The whole critical section runs under the
    # flock so the empty-check and the record-write are one atomic step.
    with _proxy_lock(family, seat):
        config_path = os.path.join(cfgd, "config.yaml")
        owned = _running_pid_rec(family, seat)
        # THE MONEY AND TERMS PREFLIGHT RUNS BEFORE THE PLAN, and before any
        # byte is staged or any listener stopped. A family whose key is the
        # owner's FUNDED account must not be started on a model that has
        # started charging, on a model whose vendor trains on the submitted
        # prompt, or on a config that has collapsed its per-model credential
        # split — and "must not be started" has to mean the running listener
        # and the existing bytes are both left exactly as they were, which is
        # only true if nothing has happened yet. A no-op on every family that
        # declares no per-model table.
        from .seat_launch_assets import family_start_refusal
        try:
            with open(config_path, encoding="utf-8") as f:
                preflight_text = f.read()
        except OSError as exc:
            print("helm seat: refusing %s proxy start — its config could not "
                  "be read (%s)" % (seat, exc), file=sys.stderr)
            return 1
        try:
            gate, notes = family_start_refusal(family, fam, preflight_text)
        except (IndexError, KeyError, TypeError, ValueError) as exc:
            print("helm seat: refusing %s proxy start — the zero-cost and "
                  "data-terms preflight could not read this config (%s); "
                  "existing bytes and listener were left intact"
                  % (seat, exc), file=sys.stderr)
            return 1
        for note in notes:
            print("helm seat: %s" % note, file=sys.stderr)
        if gate:
            print("helm seat: refusing %s proxy start — %s" % (seat, gate),
                  file=sys.stderr)
            return 1
        try:
            plan = proxy_config_plan(config_path, family, seat)
        except (IndexError, KeyError, OSError, TypeError, ValueError) as exc:
            print("helm seat: refusing %s proxy start — config regeneration "
                  "failed (%s); existing bytes and listener were left intact"
                  % (seat, exc), file=sys.stderr)
            return 1
        drift, detail = proxy_drift(family, seat, record=owned) \
            if owned else (PROXY_CURRENT, None)
        if owned and not plan["changed"] and drift != PROXY_STALE:
            print("helm seat: %s proxy already running (pid %d, port %d)"
                  % (seat, owned["pid"], port), file=sys.stderr)
            return 1
        # An unowned bound port is resolved before staging or publishing config.
        # Adoption needs no replacement binary: the exact listener already runs.
        if not owned and _port_open(port):
            return _adopt_or_refuse_port(seat, port, cfgd)
        # Establish replacement capability BEFORE mutating config or stopping a
        # working listener. A missing/non-executable binary leaves both live.
        b = _proxy_bin()
        ready, why = _proxy_binary_ready(b) if b else (False, "not found")
        if not ready:
            print("helm seat: cli-proxy-api binary unavailable (%s; checked "
                  "HELM_PROXY_BIN, %s, PATH) — run `helm seat doctor`"
                  % (why, PROXY_BIN_DEFAULT), file=sys.stderr)
            return 1
        staged = None
        try:
            if plan["changed"]:
                staged = _stage_private(config_path, plan["text"])
            if owned:
                stopped, why = _stop_owned_proxy(family, seat, owned)
                if not stopped:
                    print("helm seat: refusing %s proxy replacement — %s; "
                          "config and listener were left intact"
                          % (seat, why), file=sys.stderr)
                    return 1
            if _port_open(port):
                return _adopt_or_refuse_port(seat, port, cfgd)
            if staged:
                _commit_private(staged, config_path)
                staged = None
        except OSError as exc:
            restore = ""
            if owned:
                try:
                    _write_private(config_path, plan["old"])
                    old_binary = ((owned.get("launch") or {}).get("binary") or {}).get(
                        "source") or b
                    prior, prior_error = _launch_proxy_process(
                        old_binary, config_path, cfgd, port)
                    restore = "; previous listener restored" if prior else \
                        "; previous listener restore failed (%s)" % prior_error
                except OSError as restore_exc:
                    restore = "; previous listener restore failed (%s)" % restore_exc
            print("helm seat: refusing %s proxy replacement — staged config "
                  "publication failed (%s)%s" % (seat, exc, restore),
                  file=sys.stderr)
            return 1
        finally:
            if staged:
                _discard_private(staged)
        if owned and detail:
            print("helm seat: %s proxy restart: %s" % (seat, detail),
                  file=sys.stderr)
        p, launch_error = _launch_proxy_process(b, config_path, cfgd, port)
        if p is None:
            restored = None
            if owned:
                try:
                    if plan["changed"]:
                        _write_private(config_path, plan["old"])
                    old_binary = ((owned.get("launch") or {}).get("binary") or {}).get(
                        "source") or b
                    restored, restore_error = _launch_proxy_process(
                        old_binary, config_path, cfgd, port)
                except OSError as exc:
                    restore_error = str(exc)
                if restored is None:
                    launch_error += "; previous listener restore failed (%s)" % \
                        restore_error
                else:
                    launch_error += "; previous config and listener restored"
            print("helm seat: %s" % launch_error, file=sys.stderr)
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
        # None-subscript crash class fixed at a6b8d4a, "seat: streaming-leg
        # survival in proxy config") — the record is already in hand.
        owned = _running_pid_rec(family, seat)
        if not owned:
            # Distinguish a merely-dead proxy from a REUSED/unauthenticated pid:
            # a live process holding our recorded pid that we cannot prove is
            # ours is NOT our proxy — never signal it. Reap the stale file.
            rec = _proxy_pid_record(family, seat)
            if rec and _pid_alive(rec["pid"]):
                os.remove(pidfile)
                # THE READER'S ACCESSOR, not admission's, and this is a
                # MAINTENANCE path: the seat exists, it has a pidfile, and the
                # sentence's whole value is the `ss` command an operator runs
                # next. `_instance_port` answers None for a project seat whose
                # ledger entry is gone and for a numbered suffix reaching the
                # block, and `%d` of that None raised a TypeError about string
                # formatting from inside the one branch that had already done
                # its work — the record was reaped and the operator got a
                # traceback instead of the port to check.
                endpoint = _existing_instance_port(family, seat)
                print("helm seat: %s proxy pidfile stale — pid %d now belongs to "
                      "an unrelated process (reused); NOT signalled, record "
                      "reaped. NOTE: this includes LEGACY bare-pid records from "
                      "pre-per-instance proxies (every proxy running at land). "
                      "The old proxy may STILL hold the port — to load the new "
                      "config, kill it by hand: `kill %d` (%s), then "
                      "`helm seat up %s`."
                      % (seat, rec["pid"], rec["pid"],
                         "verify with `ss -ltnp | grep :%d`" % endpoint
                         if endpoint is not None else
                         "helm cannot resolve this instance's endpoint, so "
                         "find the socket with `ss -ltnp | grep %d`" % rec["pid"],
                         seat))
                return 0
            if os.path.exists(pidfile):
                os.remove(pidfile)  # stale
            print("helm seat: %s proxy not running" % seat)
            return 0
        pid = owned["pid"]
        stopped, why = _stop_owned_proxy(family, seat, owned, force=True)
        if not stopped:
            print("helm seat: refusing to claim %s stopped — %s"
                  % (seat, why), file=sys.stderr)
            return 1
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
