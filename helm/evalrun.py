#!/usr/bin/env python3
"""helm eval run/seed — the §C batch's runner rig, built out of the pilot's
five defects instead of ahead of them.

THE 2026-07-29 PILOT PUBLISHED ZERO NUMBERS, BY DESIGN, and that was its whole
value: one real task through two real arms surfaced five ways this rig would
have manufactured false numbers at batch scale. Each fix below is owned here
and cites its finding; the findings themselves live in the §C PILOT messages
(journal chat-2026-07-29.log) and the fix spec the integrator ACKed.

FINDING 1 — an arm reconstructed from READABLE env is a broken arm. The
pilot's first cc firing composed base-url/config-dir/model off launch.sh by
eye and died in 1s ("Not logged in") because the bearer is exported INSIDE
launch.sh from a 0600 token file, deliberately never argv-visible. And a
1-second failure looks exactly like a fast arm to a naive timer. So
`arm_command()` shells the seat's OWN launch.sh with -p — the only
construction that cannot drift from what the fleet actually runs.

FINDING 5 — THE FATAL ONE: a headless arm that inherits the seat's config dir
inherits its SessionStart onboarding, OBEYS it, arms `helm chat wait
--follow`, and never returns. The write-up then reads "Claude Code hangs" — a
harness verdict manufactured entirely by the rig, in the eval whose reason to
exist is that config differences masquerade as harness differences. So
`arm_config()` builds a PER-RUN config dir carrying the seat's files with the
hooks GONE — auth and identity kept (that is what makes the arms comparable),
fleet participation removed (that is what makes the measurement lie) — and
the seat's real config is never touched. The runner also feeds the arm
stdin from /dev/null: the pilot arm sat 3s on "no stdin data received".

FINDING 3 — the pilot's timing instrument printed elapsed = -64s, because one
.start/.end path pair was reused across a failed firing and its re-fire. It
was loud only by luck of sign: minutes later the same bug yields a PLAUSIBLE
positive number. So every run gets its OWN directory named by its run id
(`run_dir()` refuses a reused id outright), the stamps are taken by the
wrapper that owns the process rather than by shell redirection, and
`record_result()` REFUSES any row whose elapsed is <= 0, naming the row —
a broken probe must never file a fast success.

FINDING 2 — the pilot seeded a board-sourced task whose premise had been
fixed EIGHT DAYS earlier (react-digest: REACT_TAG landed 2026-07-21). Both arms correctly refuted it — which means the run measured
premise-checking, not harness reliability, and an arm run was burned finding
out. So `seed()` verifies an atom's premise against CURRENT code first: a
refuted premise writes a stale-and-skipped record WITH the refuting evidence
and never reaches an arm; an atom carrying no checkable premise never seeds
at all.

(FINDING 4 — inter-arm convergence on the refuted premise — was the pilot's
one genuine result, not a defect; it needed no code, it needed the four
fixes above so the next convergence is measured rather than lucky.)

WHAT THIS MODULE DELIBERATELY DOES NOT DO: grade. §C.1's "no arm grades
itself" and the pre-registered §C.5 rule live in evalpin; this file only
drives arms and records honest rows for that machinery to judge.
"""
import json
import os
import re
import shutil
import sys
import time

from . import home

# The seam the seat's launch.sh honors so an arm can ride the REAL launch
# path (FINDING 1) with its OWN config dir (FINDING 5). launch_line() emits
# CLAUDE_CONFIG_DIR="${HELM_EVAL_CONFIG_DIR:-<seat claude>}", so a seat runs
# identically when the var is unset and an arm overrides nothing else.
# `arm_command()` checks the SCRIPT TEXT for this name and refuses a
# launch.sh minted before the seam existed — running it anyway would hand
# the arm the seat's real config dir, fleet hooks included, silently.
CONFIG_OVERRIDE_ENV = "HELM_EVAL_CONFIG_DIR"

# settings files whose `hooks` key makes a process a fleet participant.
SETTINGS_FILES = ("settings.json", "settings.local.json")

_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def runs_root(root=None):
    """Where run dirs live. Under the project home so the artifacts sit on
    the same evals shelf as the §C.5 pre-registration."""
    return root or os.path.join(home.project_dir("helm"), "evals", "runs")


def run_dir(run_id, root=None):
    """Mint THIS run's own directory -> (path, err). The name IS the run id.

    Refuses a reused id rather than reusing the directory: the pilot's -64s
    elapsed (FINDING 3) was exactly one path pair shared by a failed firing
    and its re-fire, run-1's stale .end read against run-2's .start. A rig
    that recycles paths across attempts will eventually cross-read them.
    """
    if not run_id or not _SAFE_NAME.match(run_id):
        return None, ("run id %r cannot name a directory — one token of "
                      "[A-Za-z0-9._-], because the run dir NAME is how a "
                      "human ties artifacts back to the run" % (run_id,))
    path = os.path.join(runs_root(root), run_id)
    try:
        os.makedirs(path, exist_ok=False)
    except FileExistsError:
        return None, ("run dir %s already exists — REFUSING to reuse it. A "
                      "reused run dir is how the pilot read run-1's stale "
                      ".end against run-2's .start and printed elapsed=-64s "
                      "(FINDING 3); mint a new run id instead" % path)
    except OSError as e:
        return None, "could not create %s (%s)" % (path, e)
    return path, None


def _strip_hooks(body):
    """(hookless copy, event names stripped) — the whole `hooks` object goes:
    an eval arm is not a fleet participant, and every event class in a seat's
    settings (SessionStart join/resume, Stop guard, delivery) exists to wire
    a process INTO the fleet."""
    events = sorted(body.get("hooks") or ()) if isinstance(body, dict) else []
    out = {k: v for k, v in body.items() if k != "hooks"} \
        if isinstance(body, dict) else body
    return out, events


def arm_config(seat_claude, rdir):
    """The arm's per-run CLAUDE_CONFIG_DIR -> (path, err).

    A COPY of the seat's config FILES with the hooks gone, built inside the
    run dir, so the arm keeps the seat's identity/settings (what makes the
    arms comparable) and loses the fleet integrations (what made the pilot's
    cc arm sit forever on a beacon its own harness told it to arm —
    FINDING 5). The seat's real config dir is READ ONLY here, never written.

    State DIRS (projects/, sessions/, history…) deliberately do not ride
    along: they are fleet context, and fleet context inside a measurement
    arm is the contamination this function exists to remove.
    """
    if not os.path.isdir(seat_claude):
        return None, ("seat claude dir %s missing — the arm must be built "
                      "from a REAL seat's config, not invented (FINDING 1)"
                      % seat_claude)
    cfg = os.path.join(rdir, "arm-config")
    try:
        os.makedirs(cfg)
    except OSError as e:
        return None, "could not create %s (%s)" % (cfg, e)
    stripped = {}
    for name in sorted(os.listdir(seat_claude)):
        src = os.path.join(seat_claude, name)
        if not os.path.isfile(src):
            continue                      # state dirs stay behind, see above
        if name in SETTINGS_FILES:
            try:
                with open(src, encoding="utf-8") as f:
                    body = json.load(f)
            except (OSError, ValueError) as e:
                # An arm with UNKNOWN hooks is FINDING 5 waiting to recur —
                # refuse rather than copy what could not be read.
                return None, ("seat %s unreadable (%s) — cannot prove the "
                              "arm config is hook-free, refusing to build it"
                              % (src, e))
            body, events = _strip_hooks(body)
            if events:
                stripped[name] = events
            with open(os.path.join(cfg, name), "w", encoding="utf-8") as f:
                f.write(json.dumps(body, indent=2, sort_keys=True) + "\n")
        else:
            try:
                shutil.copy2(src, os.path.join(cfg, name))
            except OSError as e:
                return None, "could not copy %s (%s)" % (src, e)
    # The strip leaves EVIDENCE, not an absence: a later reader can see what
    # was removed from which file without diffing the seat by hand.
    with open(os.path.join(rdir, "hooks-stripped.json"), "w",
              encoding="utf-8") as f:
        f.write(json.dumps({"source": seat_claude, "stripped": stripped},
                           indent=2, sort_keys=True) + "\n")
    return cfg, None


def arm_command(seat, task_path, cfg_dir):
    """The arm's exact launch -> ({argv, env, launch_sh}, err).

    THE SEAT'S OWN launch.sh WITH -p, NOTHING ELSE. The pilot's hand-built
    arm died in one second ("Not logged in") because the bearer is exported
    inside launch.sh from a 0600 token file and is invisible to environment
    inspection (FINDING 1) — any rig that re-derives the env measures its
    own reconstruction, and the drift is silent until it is a number.

    The ONLY variable the rig adds is CONFIG_OVERRIDE_ENV, the seam
    launch.sh itself expands into CLAUDE_CONFIG_DIR — pointed at the
    hook-stripped per-run copy. A launch.sh minted before that seam existed
    is REFUSED by text inspection: run as-is it would hand the arm the
    seat's real config dir, fleet hooks included (FINDING 5), and nothing
    downstream could tell.
    """
    from . import seat as seatmod
    family, err = seatmod._seat_family(seat)
    if err:
        return None, err
    launch_sh = os.path.join(seatmod._instance_dir(family, seat), "launch.sh")
    if not os.path.exists(launch_sh):
        return None, ("seat %s has no launch.sh at %s — `helm seat launch "
                      "%s` mints it. An arm assembled from a reconstructed "
                      "env instead is FINDING 1" % (seat, launch_sh, seat))
    try:
        with open(launch_sh, encoding="utf-8", errors="replace") as f:
            script = f.read()
    except OSError as e:
        return None, "launch.sh unreadable (%s)" % e
    if CONFIG_OVERRIDE_ENV not in script:
        return None, ("%s predates the %s seam — re-mint it (`helm seat "
                      "launch %s`). Run as-is the arm would inherit the "
                      "seat's REAL config dir, fleet hooks included, and "
                      "sit on a beacon exactly like the pilot's cc arm "
                      "(FINDING 5)" % (launch_sh, CONFIG_OVERRIDE_ENV, seat))
    try:
        with open(task_path, encoding="utf-8") as f:
            prompt = f.read()
    except OSError as e:
        return None, "task %s unreadable (%s)" % (task_path, e)
    env = dict(os.environ)
    env[CONFIG_OVERRIDE_ENV] = cfg_dir
    return {"argv": [launch_sh, "-p", prompt], "env": env,
            "launch_sh": launch_sh}, None


def record_result(rdir, row):
    """Append the row to <run>/results.jsonl -> (row+elapsed, err).

    REFUSES, loudly and by name, any row whose elapsed is not a positive
    number — missing stamp, crossed stamps, clock skew, all of it. The
    pilot's instrument printed -64s and was honest only by luck of sign
    (FINDING 3): started a few minutes later, the same stale-stamp bug
    yields a plausible positive latency and the eval publishes a
    measurement that never happened. A refused row is never written.
    """
    name = "%s/%s" % (row.get("run") or os.path.basename(rdir),
                      row.get("arm") or "?")
    started, ended = row.get("started"), row.get("ended")
    for stamp, val in (("started", started), ("ended", ended)):
        if not isinstance(val, (int, float)) or isinstance(val, bool):
            return None, ("REFUSED result row %s: %s stamp missing (%r) — a "
                          "row without both stamps has no elapsed, and "
                          "defaulting one is how a crashed timer becomes a "
                          "fast success (FINDING 3)" % (name, stamp, val))
    elapsed = ended - started
    if elapsed <= 0:
        return None, ("REFUSED result row %s: elapsed %.3fs <= 0 — stale or "
                      "crossed stamps (the pilot's reused .start/.end pair "
                      "read -64s; FINDING 3). This row must never be "
                      "recorded: shifted a few minutes, the same defect "
                      "reads as a plausible fast success" % (name, elapsed))
    row = dict(row, elapsed=elapsed)
    try:
        with open(os.path.join(rdir, "results.jsonl"), "a",
                  encoding="utf-8") as f:
            f.write(json.dumps(row, sort_keys=True) + "\n")
    except OSError as e:
        return None, "could not record row %s (%s)" % (name, e)
    return row, None


def verify_premise(atom, repo_root):
    """Is the atom's premise LIVE against the code as it is NOW?
    -> ({live, evidence}, err)

    The pilot's react-digest atom described a bug fixed eight days earlier;
    both arms refuted it, so the run measured premise-checking instead of
    harness reliability and burned an arm run doing it (FINDING 2). An atom
    therefore carries a machine-checkable premise:

        {"path": "helm/chat.py", "absent": "REACT_TAG"}   # live while absent
        {"path": "helm/x.py",  "present": "shared_tag"}   # live while present

    `absent` is the react-digest shape — the premise holds only while the
    fix's marker is NOT in the tree; its refuting evidence is the file:line
    that proves the fix landed. Exactly one of present/absent, always with a
    path: an atom whose premise CANNOT be checked returns err, because
    "unverifiable" seeded anyway is precisely the pilot's defect.
    """
    aid = atom.get("id") or "?"
    spec = atom.get("premise")
    if not isinstance(spec, dict):
        return None, ("atom %s carries no premise spec — an atom that cannot "
                      "be premise-checked must not seed; the pilot burned an "
                      "arm run on a premise fixed 8 days earlier (FINDING 2)"
                      % aid)
    path, present, absent = spec.get("path"), spec.get("present"), \
        spec.get("absent")
    if not path or bool(present) == bool(absent):
        return None, ("atom %s premise must name `path` and exactly one of "
                      "`present`/`absent`" % aid)
    target = os.path.join(repo_root, path)
    if not os.path.isfile(target):
        return {"live": False,
                "evidence": "%s does not exist under %s — the premise names "
                            "code that is not there" % (path, repo_root)}, None
    try:
        pattern = re.compile(present or absent)
    except re.error as e:
        return None, "atom %s premise pattern does not compile (%s)" % (aid, e)
    hit = None
    with open(target, encoding="utf-8", errors="replace") as f:
        for n, line in enumerate(f, 1):
            if pattern.search(line):
                hit = "%s:%d: %s" % (path, n, line.strip())
                break
    if absent:
        if hit:
            return {"live": False,
                    "evidence": "%s — the premise holds only while %r is "
                                "absent, and there it is" % (hit, absent)}, None
        return {"live": True,
                "evidence": "no match for %r anywhere in %s — the premise "
                            "still holds" % (absent, path)}, None
    if hit:
        return {"live": True, "evidence": hit}, None
    return {"live": False,
            "evidence": "no match for %r anywhere in %s — the code the "
                        "premise describes is gone" % (present, path)}, None


def seed(atom, repo_root, rdir):
    """Premise-gate the atom, then write its task into the run dir
    -> (report, err).

    A REFUTED PREMISE NEVER REACHES AN ARM. It writes <id>.stale.json —
    verdict stale-and-skipped WITH the refuting file:line — and the report
    says so, because a skipped atom recorded nowhere is indistinguishable
    from an atom nobody ever seeded (FINDING 2's queue rotted for eight
    days precisely because nothing re-read it against the code).
    """
    verdict, err = verify_premise(atom, repo_root)
    if err:
        return None, err
    aid = atom.get("id") or "atom"
    if not _SAFE_NAME.match(aid):
        return None, ("atom id %r cannot name run-dir artifacts — one token "
                      "of [A-Za-z0-9._-]" % (aid,))
    if not verdict["live"]:
        record = {"atom": aid, "verdict": "stale-and-skipped",
                  "premise": atom.get("premise"),
                  "evidence": verdict["evidence"],
                  "repo": repo_root, "ts": int(time.time())}
        path = os.path.join(rdir, "%s.stale.json" % aid)
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps(record, indent=2, sort_keys=True) + "\n")
        return {"atom": aid, "seeded": False, "stale": True,
                "evidence": verdict["evidence"], "record": path}, None
    task = atom.get("task") or ""
    if not task.strip():
        return None, "atom %s has a live premise and no task text" % aid
    path = os.path.join(rdir, "%s-TASK.md" % aid)
    with open(path, "w", encoding="utf-8") as f:
        f.write(task)
    return {"atom": aid, "seeded": True, "stale": False,
            "evidence": verdict["evidence"], "task_path": path}, None


def run_arm(seat, task_path, rdir, arm=None, timeout=None):
    """Drive ONE arm through the seat's real launch path -> (row, err).

    The wrapper that owns the process owns both stamps (FINDING 3 — never
    shell redirection), stdin comes from /dev/null (the pilot arm sat 3s
    waiting on it, FINDING 5), stdout+stderr land in the run dir, and the
    row goes through record_result's elapsed refusal like every other row.
    A timeout is recorded as a row (timed_out, rc None) — an arm that hit
    the wall is a DATAPOINT; only the rig's own refusals are errors.
    """
    import subprocess
    from . import seat as seatmod
    arm = arm or "cc-%s" % seat
    if not _SAFE_NAME.match(arm):
        return None, "arm name %r cannot name run-dir artifacts" % (arm,)
    family, err = seatmod._seat_family(seat)
    if err:
        return None, err
    seat_claude = os.path.join(seatmod._instance_dir(family, seat), "claude")
    cfg, err = arm_config(seat_claude, rdir)
    if err:
        return None, err
    cmd, err = arm_command(seat, task_path, cfg)
    if err:
        return None, err
    out_path = os.path.join(rdir, "%s.out" % arm)
    timed_out = False
    started = time.time()          # the wrapper owns the stamps (FINDING 3)
    with open(out_path, "wb") as out:
        try:
            rc = subprocess.run(cmd["argv"], env=cmd["env"],
                                stdin=subprocess.DEVNULL,   # FINDING 5
                                stdout=out, stderr=subprocess.STDOUT,
                                timeout=timeout).returncode
        except subprocess.TimeoutExpired:
            timed_out, rc = True, None
        except OSError as e:
            return None, "arm %s failed to launch (%s)" % (arm, e)
    ended = time.time()
    return record_result(rdir, {
        "run": os.path.basename(rdir), "arm": arm, "seat": seat, "rc": rc,
        "started": started, "ended": ended, "timed_out": timed_out,
        "out": out_path, "task": task_path, "config": cfg,
        "launch_sh": cmd["launch_sh"]})


_USAGE = """usage: helm eval seed --atom FILE --repo DIR --run-id ID [--root DIR] [--json]
       helm eval run --seat S --atom FILE --repo DIR --run-id ID
                     [--arm NAME] [--root DIR] [--timeout SECS] [--json]

  The §C batch runner, shaped by the 2026-07-29 pilot's five defects.

  seed   Premise-check the atom (FILE is JSON: {id, task, premise:{path,
         present|absent}}) against --repo, then write its task into a fresh
         run dir. A refuted premise writes <id>.stale.json with the refuting
         file:line and NEVER seeds — exit 1 so a batch script sees it.
  run    seed, then drive the seat's OWN launch.sh with -p under a per-run
         hook-stripped config copy, stdin from /dev/null, wrapper-owned
         stamps, and an elapsed<=0 refusal on the recorded row. The arm's
         exit code is data in the row; exit 1 here means the RIG refused.

  Run dirs are never reused — a reused id is refused outright (the pilot's
  -64s elapsed came from one recycled .start/.end pair). Mint a new id.
"""


def _opt(rest, name, default=None):
    return rest[rest.index(name) + 1] if name in rest \
        and rest.index(name) + 1 < len(rest) else default


def cmd(verb, rest):
    """seed|run — routed here by evalpin.cmd_eval, which owns `helm eval`."""
    from .cli import guard_tail
    valued = ("--atom", "--repo", "--run-id", "--root") + \
        (("--seat", "--arm", "--timeout") if verb == "run" else ())
    rc = guard_tail("helm eval " + verb, rest, flags=("--json",),
                    valued=valued, usage=_USAGE)
    if rc is not None:
        return rc
    atom_path, repo = _opt(rest, "--atom"), _opt(rest, "--repo")
    run_id, root = _opt(rest, "--run-id"), _opt(rest, "--root")
    if not (atom_path and repo and run_id):
        print("helm eval %s: --atom, --repo and --run-id are all required"
              % verb, file=sys.stderr)
        return 2
    try:
        with open(atom_path, encoding="utf-8") as f:
            atom = json.load(f)
    except (OSError, ValueError) as e:
        print("helm eval %s: atom %s unreadable (%s)" % (verb, atom_path, e),
              file=sys.stderr)
        return 1
    rdir, err = run_dir(run_id, root)
    if err:
        print("helm eval %s: %s" % (verb, err), file=sys.stderr)
        return 1
    report, err = seed(atom, repo, rdir)
    if err:
        print("helm eval %s: %s" % (verb, err), file=sys.stderr)
        return 1
    if report["stale"]:
        if "--json" in rest:
            print(json.dumps(report, indent=2, sort_keys=True))
        else:
            print("helm eval %s: atom %s STALE-AND-SKIPPED — %s\n  record %s"
                  % (verb, report["atom"], report["evidence"],
                     report["record"]))
        return 1
    if verb == "seed":
        if "--json" in rest:
            print(json.dumps(report, indent=2, sort_keys=True))
        else:
            print("helm eval seed: atom %s LIVE (%s) — task at %s"
                  % (report["atom"], report["evidence"],
                     report["task_path"]))
        return 0
    seat_name = _opt(rest, "--seat")
    if not seat_name:
        print("helm eval run: --seat is required", file=sys.stderr)
        return 2
    timeout = _opt(rest, "--timeout")
    try:
        timeout = float(timeout) if timeout else None
    except ValueError:
        print("helm eval run: --timeout wants seconds, got %r" % timeout,
              file=sys.stderr)
        return 2
    row, err = run_arm(seat_name, report["task_path"], rdir,
                       arm=_opt(rest, "--arm"), timeout=timeout)
    if err:
        print("helm eval run: %s" % err, file=sys.stderr)
        return 1
    if "--json" in rest:
        print(json.dumps(row, indent=2, sort_keys=True))
    else:
        print("helm eval run: %s rc=%s elapsed=%.1fs%s\n  out %s"
              % (row["arm"], row["rc"], row["elapsed"],
                 " TIMED OUT" if row["timed_out"] else "", row["out"]))
    return 0
