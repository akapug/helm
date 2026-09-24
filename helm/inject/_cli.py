"""helm inject — the cli cluster: cmd_inject (the flags-only membership
reader behind `helm inject`).

Moved verbatim from the pre-split helm/inject.py. gather/render are read
through the package namespace (_inject.X) — the test contract monkeypatches
them on helm.inject.
"""
import json
import sys

from .. import inject as _inject
from .. import moments
from ._ledger import (_lane_report, _ledger_append, parse_hook_json,
                      project_for_cwd)
from ._compare import _compare_report
from ._whisper import _explain


def cmd_inject(args):
    """inject [--project P] [--json] [--explain] [--hook-json] [--lane-report]
    [--compare-report] [--moment-report [--days N]] [--replay TURNS ...] —
    prompt text on stdin -> context lines. --hook-json reads the harness
    hook's FULL JSON on stdin instead (prompt/cwd/session_id) and derives
    --project from the cwd via the registry; malformed hook JSON injects
    nothing, rc 0 (fail-open). --explain prints what WOULD fire and why, sans
    ledger row. --lane-report renders the lane-split cohort table (read-only).
    --compare-report renders the local-vs-comparison divergence verdict
    (read-only). --moment-report renders the per-route missed-moment verdicts
    and the hook's latency and timeout bars (read-only; rc 1 on any RED).
    --replay runs the router over a recorded turn file (helm.injectreplay)."""
    # flags-only membership reader with an inline-text positional (quick
    # tests): guard_tail would junk the inline text, so a custom guard refuses
    # any UNKNOWN --flag (rc 2) while passing inline non-dash text + the
    # hook-json path — `inject --bogus` used to inject the real context and
    # exit 0 with the bogus flag pretending it existed.
    _known = ("--project", "--json", "--explain", "--hook-json",
              "--lane-report", "--compare-report", "--moment-report", "--days",
              "--replay", "--gold", "--labels", "--limit", "--recorded",
              "--out")
    bad = [a for a in args if a.startswith("-")
           and a not in _known and a not in ("-h", "--help")]
    if bad:
        from ..cli import suggest
        print("helm inject: unknown arg '%s'%s" % (bad[0], suggest(bad[0], _known)),
              file=sys.stderr)
        return 2
    if "-h" in args or "--help" in args:
        print("inject [--project P] [--json] [--explain] [--hook-json] "
              "[--lane-report] [--compare-report] "
              "[--moment-report [--days N]] "
              "[--replay TURNS.jsonl [--recorded] [--gold GOLD --labels LABELS] "
              "[--limit N] [--out FILE]]")
        return 0
    project = None
    if "--project" in args:
        project = args[args.index("--project") + 1]
    if "--lane-report" in args:
        return _lane_report(project=project)
    if "--compare-report" in args:
        return _compare_report(project=project)
    if "--moment-report" in args:
        return _moment_report(args)
    if "--replay" in args:
        from .. import injectreplay
        return injectreplay.cmd(args)
    session = scope_via = cwd = hook = None
    if "--hook-json" in args:
        # THE HOOK'S OWN DEADLINE, from process start, under the wrapper's
        # budget (moments.DEADLINE_S): a turn that runs out of time leaves a
        # `timed_out` ledger row instead of dying silently at the TERM.
        moments.arm_deadline()
        try:
            raw = "" if sys.stdin.isatty() else sys.stdin.read()
            text, cwd, session = parse_hook_json(raw)
            if text is None:
                moments.disarm_deadline()
                return 0  # garbled hook payload: inject nothing, never block
            try:
                hook = json.loads(raw)
            except ValueError:
                hook = None
            if project is None:
                project = project_for_cwd(cwd)
                scope_via = cwd if project else None
        except moments.Deadline:
            return 0
    else:
        # scan args for the inline-text positional FIRST; only read stdin when
        # no inline text was given — reading sys.stdin.read() unconditionally
        # under captured (non-tty) stdin raises OSError in test harnesses.
        inline = next((a for a in args
                       if not a.startswith("-") and a != project), None)
        text = inline if inline is not None else (
            "" if sys.stdin.isatty() else sys.stdin.read())
    if "--explain" in args:
        moments.disarm_deadline()
        if scope_via:
            print("[scope: %s via %s]" % (project, scope_via))
        return _explain(text, project=project, session=session)
    try:
        sections = _inject.gather(text, project=project, session=session,
                                  cwd=cwd, hook=hook)
    except moments.Deadline:
        # The deadline landed OUTSIDE construction (the turn-boundary
        # counters, or admission itself): ledger the loss on a fresh attempt
        # when gather had not, then inject nothing.
        moments.disarm_deadline()
        from ._whisper import CURRENT, timed_out_row
        if not CURRENT.get("ledgered"):
            try:
                _ledger_append(timed_out_row(text, project, session, cwd, hook))
            except Exception:
                pass
        return 0
    moments.disarm_deadline()
    if "--json" in args:
        print(json.dumps(sections, ensure_ascii=False))
        return 0
    out = _inject.render(sections)
    if out:
        print(out)
    return 0


def _moment_report(args):
    """--moment-report [--days N] [--json]: the missed-moment instrument
    (moments.report). rc 1 when any route or hook bar reads RED, so a script
    can gate on it; the report itself is read-only."""
    days = 7.0
    if "--days" in args:
        try:
            days = float(args[args.index("--days") + 1])
        except (IndexError, ValueError):
            print("helm inject: --days needs a number", file=sys.stderr)
            return 2
    rep = moments.report(days=days)
    if "--json" in args:
        print(json.dumps(rep, ensure_ascii=False, indent=1, default=str))
    else:
        for line in moments.render_report(rep):
            print(line)
    return 1 if rep["red"] else 0
