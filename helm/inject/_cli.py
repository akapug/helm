"""helm inject — the cli cluster: cmd_inject (the flags-only membership
reader behind `helm inject`).

Moved verbatim from the pre-split helm/inject.py. gather/render are read
through the package namespace (_inject.X) — the test contract monkeypatches
them on helm.inject.
"""
import json
import sys

from .. import inject as _inject
from ._ledger import _lane_report, parse_hook_json, project_for_cwd
from ._compare import _compare_report
from ._whisper import _explain


def cmd_inject(args):
    """inject [--project P] [--json] [--explain] [--hook-json] [--lane-report]
    [--compare-report] — prompt text on stdin -> context lines. --hook-json reads
    the harness hook's FULL JSON on stdin instead (prompt/cwd/session_id) and
    derives --project from the cwd via the registry; malformed hook JSON injects
    nothing, rc 0 (fail-open). --explain prints what WOULD fire and why, sans
    ledger row. --lane-report renders the lane-split cohort table (read-only).
    --compare-report renders the local-vs-comparison divergence verdict (read-only)."""
    # flags-only membership reader with an inline-text positional (quick
    # tests): guard_tail would junk the inline text, so a custom guard refuses
    # any UNKNOWN --flag (rc 2) while passing inline non-dash text + the
    # hook-json path — `inject --bogus` used to inject the real context and
    # exit 0 with the bogus flag pretending it existed.
    _known = ("--project", "--json", "--explain", "--hook-json",
              "--lane-report", "--compare-report")
    bad = [a for a in args if a.startswith("-")
           and a not in _known and a not in ("-h", "--help")]
    if bad:
        from ..cli import suggest
        print("helm inject: unknown arg '%s'%s" % (bad[0], suggest(bad[0], _known)),
              file=sys.stderr)
        return 2
    if "-h" in args or "--help" in args:
        print("inject [--project P] [--json] [--explain] [--hook-json] "
              "[--lane-report] [--compare-report]")
        return 0
    project = None
    if "--project" in args:
        project = args[args.index("--project") + 1]
    if "--lane-report" in args:
        return _lane_report(project=project)
    if "--compare-report" in args:
        return _compare_report(project=project)
    session = scope_via = cwd = None
    if "--hook-json" in args:
        text, cwd, session = parse_hook_json(
            "" if sys.stdin.isatty() else sys.stdin.read())
        if text is None:
            return 0  # garbled hook payload: inject nothing, never block
        if project is None:
            project = project_for_cwd(cwd)
            scope_via = cwd if project else None
    else:
        # scan args for the inline-text positional FIRST; only read stdin when
        # no inline text was given — reading sys.stdin.read() unconditionally
        # under captured (non-tty) stdin raises OSError in test harnesses.
        inline = next((a for a in args
                       if not a.startswith("-") and a != project), None)
        text = inline if inline is not None else (
            "" if sys.stdin.isatty() else sys.stdin.read())
    if "--explain" in args:
        if scope_via:
            print("[scope: %s via %s]" % (project, scope_via))
        return _explain(text, project=project, session=session)
    sections = _inject.gather(text, project=project, session=session, cwd=cwd)
    if "--json" in args:
        print(json.dumps(sections, ensure_ascii=False))
        return 0
    out = _inject.render(sections)
    if out:
        print(out)
    return 0
