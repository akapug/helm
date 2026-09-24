"""helm.roguescan — the LOCAL rogue-compute watchdog (task/1039).

THE HOLE THIS CLOSES. The fab-suite PreToolUse guard and the PATH shims
(~/.local/bin/python3, cargo) block rogue suites at the SOURCE — but only in
sessions whose config loaded after install, and only for the shapes they
enumerate. Today's load-49 incident (2026-08-11) came through BOTH holes at
once: a days-old session, and a JS/TS project (vitest/webpack/pnpm) no shim
covers. The box layer is the only place that measures what ACTUALLY EXECUTES
rather than what configs say (store: intent-is-not-outcome). ~10 measured
incidents of agent-launched local compute making the owner's daily driver
unusable; his fans were the only detector in between.

WHAT IT DOES, in one pass (riding the silent-drop 90s cadence — no new
daemon): scan /proc for heavy-compute processes of the measured classes whose
identity is NOT a fab wrapper and whose ancestor chain contains neither a fab
wrapper nor the human-blessed escape env; attribute each to a seat by walking
HELM_CHAT_NAME up the parent chain (store: liveness-is-environ-not-cwd),
reporting UNKNOWN honestly when no ancestor carries it; classify Wrangler and
OpenNext Cloudflare commands as network-bound so remote calls and deploys are
never killable; require high CPU across consecutive passes; capture evidence
(cmdline, seat, parent chain, start time) to a state file; alert LOUDLY to
#helm naming the seat and the command; then — per the owner's standing kill
authority (store: standing-authority-sa-and-seat-management; he wants this
STOPPED, not reported) — kill after a short grace, SIGTERM then SIGKILL a
pass later, the whole subtree.

IDENTITY-FIRST CLASSIFICATION is the correctness crux. A local `fab test
--repo . -- python3 -m unittest tests` client is a bash process whose ARGV
contains a forbidden suite spelling verbatim — substring-matching the command
line would kill the very wrapper that offloads the work (the runaway-reaper
argv-false-skip lesson). So the EXE decides what a process IS
(python/node/cargo/go classify; bash/ssh never do), and only then does the
process's OWN argv decide whether it is suite-shaped. The fab CLIENT is
therefore structurally unflaggable, and fab's brief local helpers (snapshot
git, receipt-import python) are covered by the ancestor exemption.

IDENTITY AT THE MOMENT OF ACTION (store: measuring-later-only-shrinks-the-
window): a kill decision made from a snapshot binds (pid, starttime), and the
signal is sent only after re-reading the live starttime — a recycled pid is a
DIFFERENT process and is never killed on a stale finding.

Config (env, read at pass time; defaults are what the owner's history
supports):
  HELM_ROGUE_KILL=1       kill after grace (0 = alert-only)
  HELM_ROGUE_GRACE_S=60   seconds between first alert and SIGTERM
  HELM_ROGUE_MIN_AGE_S=30 candidacy floor: younger processes are never
                          flagged (an ALLOWED single-test-method local run
                          finishes in seconds and never reaches candidacy)
  HELM_ROGUE_MIN_CPU=20   candidacy floor: subtree average cpu%, summed over
                          descendants because pool runners (vitest workers,
                          workerd-per-test-file) burn cpu in CHILDREN while
                          the flagged parent idles
"""
import fcntl
import json
import os
import signal
import sys
import time

from . import home

_STATE = "roguescan.json"
_EVID_DIR = "roguescan"

_USAGE = """usage: helm rogue [--dry-run] [--json] [--kill|--no-kill] [--quiet]
                  [--min-age S] [--min-cpu PCT] [--grace S]
  One pass of the local rogue-compute watchdog: flag heavy-compute processes
  (python/node/cargo/go suite+build+install shapes) running on THIS box
  outside the fab, attribute each to a seat via HELM_CHAT_NAME in the parent
  chain (UNKNOWN when absent — never guessed), capture evidence, alert #helm,
  and kill after a grace window under the owner's standing kill authority.
  --dry-run reports without posting, writing state, or killing. Defaults from
  HELM_ROGUE_KILL/GRACE_S/MIN_AGE_S/MIN_CPU. Rides the silent-drop cadence.
"""


def _env_int(name, default):
    v = os.environ.get(name, "")
    return int(v) if v.isdigit() else default


def _kill_on():
    return os.environ.get("HELM_ROGUE_KILL", "1") != "0"


# ---------------------------------------------------------------------------
# process snapshot — one read of /proc, pure functions after
# ---------------------------------------------------------------------------

class Snapshot:
    """A process table: [{pid, ppid, exe, argv, comm, starttime, cpu_s, age}]
    plus per-pid environ access. Tests build one directly (environs=dict);
    live passes get lazy cached /proc/<pid>/environ reads."""

    def __init__(self, procs, environs=None):
        self.procs = {p["pid"]: p for p in procs}
        self._environs = environs
        self._env_cache = {}
        self.kids = {}
        for p in procs:
            self.kids.setdefault(p["ppid"], []).append(p["pid"])

    def environ(self, pid):
        if self._environs is not None:
            return self._environs.get(pid, {})
        if pid not in self._env_cache:
            self._env_cache[pid] = _read_environ(pid)
        return self._env_cache[pid]

    def ancestors(self, pid):
        """Parent chain, leaf-exclusive, cycle-guarded."""
        seen, out = {pid}, []
        p = self.procs.get(pid)
        while p:
            ppid = p["ppid"]
            if ppid in seen or ppid not in self.procs:
                break
            seen.add(ppid)
            out.append(self.procs[ppid])
            p = self.procs[ppid]
        return out

    def descendants(self, pid):
        out, queue = [], list(self.kids.get(pid, ()))
        while queue:
            c = queue.pop()
            out.append(c)
            queue.extend(self.kids.get(c, ()))
        return out

    def subtree_cpu_pct(self, pid):
        """Average cpu% over the flagged process's lifetime, summed across its
        subtree — pool runners burn cpu in children, not the flagged parent."""
        p = self.procs[pid]
        total = p["cpu_s"] + sum(self.procs[c]["cpu_s"]
                                 for c in self.descendants(pid))
        return total / max(p["age"], 1.0) * 100.0


def _read_environ(pid):
    try:
        with open("/proc/%d/environ" % pid, "rb") as f:
            raw = f.read()
    except OSError:
        return {}
    env = {}
    for tok in raw.split(b"\0"):
        if b"=" in tok:
            k, _, v = tok.partition(b"=")
            env[k.decode("utf-8", "replace")] = v.decode("utf-8", "replace")
    return env


def live_starttime(pid):
    """The kernel's per-boot start tick for this pid, or None if gone. THE
    identity re-read before any signal: pid alone is recyclable."""
    try:
        with open("/proc/%d/stat" % pid) as f:
            return int(f.read().rsplit(") ", 1)[1].split()[19])
    except (OSError, IndexError, ValueError):
        return None


def live_snapshot():
    clk = os.sysconf("SC_CLK_TCK") or 100
    with open("/proc/uptime") as f:
        uptime = float(f.read().split()[0])
    uid, procs = os.getuid(), []
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        pid = int(d)
        try:
            if os.stat("/proc/" + d).st_uid != uid:
                continue
            with open("/proc/%s/stat" % d) as f:
                stat = f.read()
            with open("/proc/%s/cmdline" % d, "rb") as f:
                argv = [a.decode("utf-8", "replace")
                        for a in f.read().split(b"\0") if a]
            exe = ""
            try:
                exe = os.readlink("/proc/%s/exe" % d)
            except OSError:
                pass
        except OSError:
            continue      # raced away mid-scan
        head, _, rest = stat.partition("(")
        comm, _, rest = rest.rpartition(") ")
        f = rest.split()
        if len(f) < 21 or not argv:
            continue      # kernel thread / zombie: no cmdline, no candidacy
        start = int(f[19])
        procs.append({
            "pid": pid, "ppid": int(f[1]), "exe": exe, "argv": argv,
            "comm": comm, "starttime": start,
            "cpu_s": (int(f[11]) + int(f[12])) / clk,
            "age": max(uptime - start / clk, 0.0),
        })
    return Snapshot(procs)


# ---------------------------------------------------------------------------
# classification — the EXE decides what a process IS, its OWN argv decides
# whether it is suite/build/install-shaped. Classes derive from MEASURED
# incidents (task rows 1038/1039/1040 + shim history); see EXCLUDED below.
# ---------------------------------------------------------------------------

_PY_MODULES = ("unittest", "pytest", "py.test", "nose2")
_PY_RUNNERS = ("pytest", "py.test", "nose2")
_NODE_SUITES = ("vitest", "jest", "webpack", "open-next", "opennext")
_NODE_PMS = ("pnpm", "npm", "npx")
_PM_VERBS = ("install", "ci", "run", "rebuild", "exec", "dlx")
_NETWORK_EXEC_TARGETS = ("wrangler", "opennextjs-cloudflare")
_NETWORK_PREFIX = "network-bound "
_SUSTAINED_SAMPLES = 2
_CARGO_VERBS = ("build", "b", "test", "t", "check", "c", "nextest",
                "clippy", "bench")
_GO_VERBS = ("test", "build")

# EXCLUDED deliberately (not measured as incidents on this box): yarn/bun/deno
# (no measured run), make/cmake/gcc/clang/rustc direct compiles (fab's cargo
# shim covers the measured Rust path), pip install, ffmpeg/ML workloads, and
# `next dev`/watch-mode servers (interactive owner usage; only `next build`
# has bitten). workerd is handled TRANSITIVELY: it is spawned per test file by
# a flagged vitest parent and dies with the subtree kill. A language
# server's --serve runaways stay the host runaway-reaper's class, not this rung's.


def _py_shape(argv):
    it = iter(range(1, len(argv)))
    for i in it:
        a = argv[i]
        if a == "-m":
            mod = argv[i + 1] if i + 1 < len(argv) else ""
            return "python -m " + mod if mod in _PY_MODULES else None
        if a.startswith("-m") and len(a) > 2:
            return "python -m " + a[2:] if a[2:] in _PY_MODULES else None
        if a in ("-c", "-") or a.startswith("-c"):
            return None
        if a.startswith("-"):
            continue
        base = a.rsplit("/", 1)[-1]
        if base in _PY_RUNNERS:
            return "python " + base
        test_shaped = ("/tests/" in a or a.startswith("tests/")
                       or base.startswith("test_") and base.endswith(".py")
                       or base.endswith("_test.py"))
        return "python " + base if test_shaped else None
    return None


def _node_base(arg):
    base = arg.rsplit("/", 1)[-1]
    for ext in (".js", ".cjs", ".mjs"):
        if base.endswith(ext):
            return base[:-len(ext)]
    return base


def _network_target(args):
    base = _node_base(args[0])
    if base in _NETWORK_EXEC_TARGETS:
        return base
    if base not in _NODE_PMS or len(args) < 2:
        return None
    verb = _node_base(args[1])
    if base == "npx" and verb in _NETWORK_EXEC_TARGETS:
        return verb
    if verb in ("exec", "dlx") and len(args) > 2:
        target = _node_base(args[2])
        return target if target in _NETWORK_EXEC_TARGETS else None
    return None


def _node_shape(argv):
    args = [a for a in argv[1:] if not a.startswith("-")]
    if not args:
        return None
    network = _network_target(args)
    if network:
        return _NETWORK_PREFIX + network
    base = _node_base(args[0])
    if base in _NODE_SUITES:
        return "node " + base
    if base == "next":
        return "node next build" if "build" in args[1:] else None
    if base in _NODE_PMS:
        verb = args[1] if len(args) > 1 else ""
        return "%s %s" % (base, verb) if verb in _PM_VERBS else None
    return None


def classify(exe, argv):
    """-> class label or None. IDENTITY-FIRST: bash/ssh (e.g. the local fab
    client, whose argv CONTAINS a suite spelling verbatim) never classify."""
    base = (exe or "").rsplit("/", 1)[-1]
    if base.startswith("python"):
        return _py_shape(argv)
    if base in ("node", "nodejs"):
        return _node_shape(argv)
    verb = next((a for a in argv[1:] if not a.startswith("-")), "")
    if base == "cargo":
        return "cargo " + verb if verb in _CARGO_VERBS else None
    if base == "go":
        return "go " + verb if verb in _GO_VERBS else None
    return None


# ---------------------------------------------------------------------------
# exemption + attribution — both walk the parent chain
# ---------------------------------------------------------------------------

def _is_fab_wrapper(p):
    base0 = (p["argv"][0] if p["argv"] else "").rsplit("/", 1)[-1]
    if base0 == "fab" or base0.startswith("fab-"):
        return True
    if (p.get("comm") or "").startswith("fab"):
        return True
    if base0 in ("bash", "sh", "dash", "zsh") and len(p["argv"]) > 1:
        base1 = p["argv"][1].rsplit("/", 1)[-1]
        return base1 == "fab" or base1.startswith("fab-")
    return False


def exemption(snap, pid):
    """Reason this candidate is EXEMPT, or None. Self + ancestors: a fab
    wrapper anywhere above (fab's own brief local helpers), or the human-only
    escape env the PATH shims honor (FAB_ALLOW_LOCAL_SUITE/BUILD=1)."""
    for p in [snap.procs[pid]] + snap.ancestors(pid):
        if _is_fab_wrapper(p):
            return "under-fab (pid %d %s)" % (p["pid"],
                                              " ".join(p["argv"][:2]))
        env = snap.environ(p["pid"])
        if env.get("FAB_ALLOW_LOCAL_SUITE") == "1" \
                or env.get("FAB_ALLOW_LOCAL_BUILD") == "1":
            return "human-escape env on pid %d" % p["pid"]
    return None


def attribute(snap, pid):
    """Owning seat via HELM_CHAT_NAME in the process's OWN environ, then up
    the parent chain (subagent shells often lack it). UNKNOWN when no
    ancestor carries it — never guessed (liveness-is-environ-not-cwd)."""
    for p in [snap.procs[pid]] + snap.ancestors(pid):
        seat = snap.environ(p["pid"]).get("HELM_CHAT_NAME")
        if seat:
            return seat
    return "UNKNOWN"


# ---------------------------------------------------------------------------
# the scan
# ---------------------------------------------------------------------------

def scan(snap, min_age=None, min_cpu=None):
    """-> {"flagged": [...], "exempt": [...]} findings. Topmost-classified
    wins (killing `pnpm run test` AND its vitest child is one action, on the
    ancestor); the candidacy floor (age + subtree cpu) keeps allowed brief
    local runs — a single test method, a quick owner command — out of reach."""
    if min_age is None:
        min_age = _env_int("HELM_ROGUE_MIN_AGE_S", 30)
    if min_cpu is None:
        min_cpu = _env_int("HELM_ROGUE_MIN_CPU", 20)
    labels = {}
    for pid, p in snap.procs.items():
        lab = classify(p["exe"], p["argv"])
        if lab:
            labels[pid] = lab
    flagged, exempt = [], []
    for pid, lab in sorted(labels.items()):
        if any(a["pid"] in labels for a in snap.ancestors(pid)):
            continue                      # a classified ancestor carries it
        p = snap.procs[pid]
        cpu = snap.subtree_cpu_pct(pid)
        if p["age"] < min_age or cpu < min_cpu:
            continue                      # below the candidacy floor
        network = lab.startswith(_NETWORK_PREFIX) or any(
            labels.get(c, "").startswith(_NETWORK_PREFIX)
            for c in snap.descendants(pid)
        )
        if network and not lab.startswith(_NETWORK_PREFIX):
            lab = next(labels[c] for c in snap.descendants(pid)
                       if labels.get(c, "").startswith(_NETWORK_PREFIX))
        f = {"pid": pid, "starttime": p["starttime"], "label": lab,
             "argv": p["argv"], "age_s": int(p["age"]), "cpu_pct": int(cpu),
             "seat": attribute(snap, pid),
             "chain": [{"pid": a["pid"], "argv": a["argv"][:4]}
                       for a in snap.ancestors(pid)]}
        why = "network-bound" if network else exemption(snap, pid)
        if why:
            f["exempt"] = why
            exempt.append(f)
        else:
            flagged.append(f)
    return {"flagged": flagged, "exempt": exempt}


# ---------------------------------------------------------------------------
# evidence, alerts, and the grace->kill state machine
# ---------------------------------------------------------------------------

def _state_dir():
    return os.path.join(home.helm_home(), home.GLOBAL, ".state")


def _write_evidence(f, now, acted=False):
    d = os.path.join(_state_dir(), _EVID_DIR)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, "rogue-%d-%d.json" % (f["pid"], f["starttime"]))
    from . import pk
    previous = pk.read_json(path, {}) or {}
    rec = dict(f,
               captured_at=previous.get("captured_at", now),
               host=previous.get("host", os.uname().nodename))
    if acted:
        rec["action_at"] = now
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(rec, fh, indent=1)
    os.replace(tmp, path)
    return path


def _alert(f, grace, kill_on, evidence):
    act = ("KILLING in %ds if high CPU persists (standing kill authority — "
           "this box is agents-only; suites/builds go through fab)" % grace
           if kill_on else "alert-only mode (HELM_ROGUE_KILL=0)")
    # THE INTEGRATOR IS RESOLVED FROM THE ROSTER, NEVER SPELLED: a seat name
    # written here keeps addressing that name after no seat carries it, and
    # the alert then reaches nobody while it reads as sent.
    from .seats_integrator import integrator_addressed
    return integrator_addressed(
        "@%s ROGUE-COMPUTE on %s: %s running LOCALLY "
        "outside the fab — pid %d, age %ds, ~%d%%cpu incl. children: "
        "`%s`. %s. evidence %s [rogue-watchdog]"
        % (f["seat"], os.uname().nodename, f["label"], f["pid"],
           f["age_s"], f["cpu_pct"], " ".join(f["argv"])[:300], act,
           evidence))


def _network_alert(f, evidence):
    return ("@%s NETWORK-BOUND on %s: `%s` may call remote services or deploy; "
            "the local-compute watchdog will alert once and never kill it. "
            "evidence %s [rogue-watchdog]"
            % (f["seat"], os.uname().nodename, " ".join(f["argv"])[:300],
               evidence))


def _post(text, quiet):
    if quiet:
        return
    try:
        from . import chat
        chat.post(text, who="rogue-watchdog", room="helm")
    except Exception as e:      # a down chat node never blocks the kill leg
        print("helm rogue: chat post failed: %s" % e, file=sys.stderr)


def _kill_subtree(snap, pid, sig, kill_fn):
    for c in snap.descendants(pid):
        try:
            kill_fn(c, sig)
        except OSError:
            pass
    try:
        kill_fn(pid, sig)
    except OSError:
        pass


def check(snap=None, now=None, dry=False, quiet=False, kill=None, grace=None,
          min_age=None, min_cpu=None, kill_fn=os.kill, start_of=None):
    """One pass: scan -> evidence+alert on first sight -> SIGTERM after the
    grace window -> SIGKILL a pass later if still alive. State is latched
    per (pid, starttime) so one episode alerts once, and every signal is
    preceded by a LIVE starttime re-read (start_of) so a recycled pid is
    never killed on a stale finding. dry=True is genuinely read-only."""
    if snap is None:
        snap = live_snapshot()
    if now is None:
        now = time.time()
    if kill is None:
        kill = _kill_on()
    if grace is None:
        grace = _env_int("HELM_ROGUE_GRACE_S", 60)
    if start_of is None:
        start_of = live_starttime
    res = scan(snap, min_age=min_age, min_cpu=min_cpu)
    own_chain = {os.getpid()}
    own_chain.update(a["pid"] for a in snap.ancestors(os.getpid()))
    p = os.path.join(_state_dir(), _STATE)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    from . import pk
    with open(p + ".lock", "a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        st = pk.read_json(p, {}) or {}
        for f in res["exempt"]:
            if f.get("exempt") != "network-bound":
                continue
            key = "%d:%d" % (f["pid"], f["starttime"])
            f["action"] = "network-bound"
            entry = st.get(key)
            if not entry or not entry.get("network_bound"):
                if not dry:
                    ev = _write_evidence(f, now)
                    _post(_network_alert(f, ev), quiet)
                    st[key] = {"network_bound": True, "seat": f["seat"],
                               "label": f["label"]}
        for f in res["flagged"]:
            key = "%d:%d" % (f["pid"], f["starttime"])
            entry = st.get(key)
            if entry is None or "samples" not in entry:
                f["action"] = "cpu-sampling"
                if not dry:
                    st[key] = {"sampled_at": now, "samples": 1,
                               "seat": f["seat"], "label": f["label"]}
                continue
            entry["samples"] += 1
            entry["sampled_at"] = now
            if "alerted_at" not in entry:
                f["action"] = "cpu-sampling"
                if entry["samples"] >= _SUSTAINED_SAMPLES:
                    f["action"] = "alerted"
                    if not dry:
                        ev = _write_evidence(f, now)
                        _post(_alert(f, grace, kill, ev), quiet)
                        entry["alerted_at"] = now
                if not dry:
                    st[key] = entry
                continue
            if f["pid"] in own_chain:
                f["action"] = "self-chain-kill-skipped"
                continue
            if not kill:
                f["action"] = "alert-only"
                continue
            if now - entry["alerted_at"] < grace:
                f["action"] = "grace-wait"
                continue
            if start_of(f["pid"]) != f["starttime"]:
                f["action"] = "identity-changed"   # recycled pid: new process
                st.pop(key, None)
                continue
            sig = signal.SIGKILL if entry.get("termed_at") else signal.SIGTERM
            f["action"] = "sigkilled" if entry.get("termed_at") else "killed"
            if not dry:
                _kill_subtree(snap, f["pid"], sig, kill_fn)
                entry["termed_at"] = entry.get("termed_at") or now
                st[key] = entry
                _write_evidence(f, now, acted=True)
                _post("@%s ROGUE-COMPUTE %s pid %d (%s) after %ds grace: "
                      "`%s` [rogue-watchdog]"
                      % (f["seat"], f["action"].upper(), f["pid"], f["label"],
                         grace, " ".join(f["argv"])[:200]), quiet)
        if not dry:
            live = {"%d:%d" % (x["pid"], x["starttime"])
                    for x in res["flagged"] + res["exempt"]
                    if x.get("exempt") in (None, "network-bound")}
            for k in [k for k in st if k not in live]:
                entry = st[k]
                pid, start = (int(x) for x in k.split(":"))
                if entry.get("network_bound") and start_of(pid) == start:
                    continue  # safe-process alert latch lasts for its identity
                st.pop(k)  # compute below the floor resets sustained evidence
            pk.write_json(p, st)
    return res


def cadence_pass(dry=False):
    """The timer leg — called from the silent-drop cadence (`helm seat
    silent-drop`, OnUnitActiveSec=90s). One line of summary; never raises."""
    try:
        res = check(dry=dry)
    except Exception as e:
        print("helm rogue: pass failed: %s" % e, file=sys.stderr)
        return
    for f in res["flagged"]:
        print("rogue: %s seat=%s pid=%d %s%s"
              % (f["label"], f["seat"], f["pid"], f["action"],
                 " (dry)" if dry else ""))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def cmd_rogue(argv=None):
    args = list(argv if argv is not None else sys.argv[1:])
    if "-h" in args or "--help" in args:
        print(_USAGE)
        return 0

    def opt(name, default):
        if name in args:
            i = args.index(name)
            return int(args[i + 1]) if i + 1 < len(args) else default
        return default

    dry = "--dry-run" in args
    kill = True if "--kill" in args else (False if "--no-kill" in args
                                          else None)
    res = check(dry=dry, quiet="--quiet" in args, kill=kill,
                grace=opt("--grace", None),
                min_age=opt("--min-age", None), min_cpu=opt("--min-cpu", None))
    if "--json" in args:
        print(json.dumps(res))
        return 0
    for f in res["flagged"]:
        print("FLAGGED %-14s seat=%-16s pid=%-7d age=%ds cpu=%d%% %s -> %s"
              % (f["label"], f["seat"], f["pid"], f["age_s"], f["cpu_pct"],
                 " ".join(f["argv"])[:120], f["action"]))
    for f in res["exempt"]:
        print("exempt  %-14s pid=%-7d %s (%s)"
              % (f["label"], f["pid"], " ".join(f["argv"])[:120], f["exempt"]))
    if not res["flagged"] and not res["exempt"]:
        print("nothing flagged, nothing exempt — no classified compute "
              "above the floor on this box right now")
    return 0


if __name__ == "__main__":
    raise SystemExit(cmd_rogue())
