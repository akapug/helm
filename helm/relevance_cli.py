"""`helm relevance` — the long-tail re-rank's verbs (helm/relevance.py).

  helm relevance [status]         mode, settings file, scorer endpoint and its
                                  health, credential presence (never the value),
                                  the remeasure receipt
  helm relevance report [--hours N] [--json]
                                  the shadow read: per tier, in-time / late /
                                  failed and the fallback rate, scoring p50/p95,
                                  classifier keeps beside keyword deliveries,
                                  and the local head's agreement with the
                                  evaluator where the evaluator taught
  helm relevance show --session S the session's per-turn score cache — the
                                  later read (turn_scores), no model called
  helm relevance warm [--project P]
                                  embed every current store line once, so the
                                  service never embeds a row inside a turn
  helm relevance remeasure ...    the gate on local-by-choice (see its --help)
  helm relevance serve ...        run the scorer service (see its --help)
  helm relevance score-turn       the detached worker the hook starts; reads
                                  one job on stdin
"""
import json
import sys
import time

from . import pk, relevance

USAGE = ("usage: helm relevance [status | report [--hours N] [--json] | "
         "show --session S | warm [--project P] | remeasure ... | serve ... | "
         "score-turn]")


def _status():
    cfg = relevance.settings()
    print("helm relevance: mode %s%s" % (cfg["mode"], (" — " + cfg["why"]) if cfg["why"] else ""))
    print("  settings   %s" % cfg["path"])
    print("  wait bound %.2f s, %d candidates, teach %s, local by choice: %s"
          % (cfg["wait_s"], cfg["candidates"], "on" if cfg["teach"] else "off",
             ", ".join(cfg["local_by_choice"]) or "none"))
    url, why = relevance.endpoint()
    if not url:
        print("  scorer     UNCONFIGURED — %s" % why)
    else:
        try:
            h = relevance.local_health(cfg)
            print("  scorer     %s — %s, head %s, threshold %s, %s cached rows"
                  % (url, h.get("status"), h.get("model"), h.get("threshold"), h.get("rows")))
        except Exception as exc:            # noqa: BLE001 — a status line, not a failure
            print("  scorer     %s — UNREACHABLE (%s: %s)" % (url, exc.__class__.__name__, exc))
    try:
        relevance.jev_key(cfg)
        cred = "present"
    except Exception as exc:                # noqa: BLE001 — never the value, only the state
        cred = "absent (%s)" % exc.__class__.__name__
    print("  evaluator  %s, threshold %s, credential %s"
          % (relevance.JEV_MODEL, cfg["jev"]["threshold"], cred))
    rec = pk.read_json(relevance.receipt_path(), None)
    if isinstance(rec, dict):
        print("  remeasure  head %s — %s (%s)" % (rec.get("head"),
              "PASS" if rec.get("pass") else "NOT PASSED", rec.get("ts")))
    else:
        print("  remeasure  no receipt: no project runs local-only by choice")
    print("  (residency is per project: `helm projects residency`)")
    return 0


def _report(args):
    hours = None
    if "--hours" in args:
        try:
            hours = float(args[args.index("--hours") + 1])
        except (IndexError, ValueError):
            print(USAGE, file=sys.stderr)
            return 2
    r = relevance.report(since_epoch=time.time() - hours * 3600 if hours else None)
    r["counters"] = relevance.counters()
    if "--json" in args:
        print(json.dumps(r, indent=1))
        return 0
    print("helm relevance report%s: %d turns submitted, %d scored, %d unspawned, %d lost"
          % (" (last %g h)" % hours if hours else "", r["submitted"], r["scored"],
             r["unspawned"], r["lost"]))
    print("  hook   added %s ms p50, %s ms p95 at prompt submit (%d turns); "
          "%d live turns fell back at the hook"
          % (r["hook_ms"]["p50"], r["hook_ms"]["p95"], r["hook_ms"]["n"], r["live_fallback"]))
    for name, t in r["tiers"].items():
        print("  %-6s %d turns: %d in time, %d late, %d failed — fallback %s; "
              "p50 %s ms, p95 %s ms; sources %s"
              % (name, t["turns"], t["in_time"], t["late"], t["failed"],
                 t["fallback_rate"], t["p50_ms"], t["p95_ms"], t["sources"]))
        print("         classifier keeps %s of candidates; of %d keyword-delivered "
              "lines it keeps %d" % (t["keep_share"], t["keyword_lines"],
                                     t["keyword_lines_kept"]))
    tch = r["teach"]
    print("  teach  %d paired scores, %d evaluator keeps; local AUC vs evaluator %s; "
          "decision agreement %s" % (tch["pairs"], tch["evaluator_keeps"],
                                     tch["auc_local_vs_evaluator"], tch["decision_agreement"]))
    for name, c in sorted((r["counters"].get("tiers") or {}).items()):
        print("  counter %s: %s" % (name, ", ".join("%s %s" % (k, c.get(k, 0))
                                                   for k in relevance.OUTCOMES)))
    return 0


def _show(args):
    if "--session" not in args or args.index("--session") + 1 >= len(args):
        print(USAGE, file=sys.stderr)
        return 2
    session = args[args.index("--session") + 1]
    obj = pk.read_json(relevance.cache_path(session), None)
    if not isinstance(obj, dict):
        print("helm relevance show: no score cache for session %s" % session)
        return 0
    print(json.dumps(obj, indent=1, ensure_ascii=False))
    got = relevance.turn_scores(session)
    print("current turn %s: %s" % (obj.get("current"),
                                   "scored" if got else "not scored (pending or failed)"))
    return 0


def _warm(args):
    from .inject import _entries
    project = args[args.index("--project") + 1] if "--project" in args \
        and args.index("--project") + 1 < len(args) else None
    rows = relevance.candidate_rows(_entries.load_entries(project))
    url, why = relevance.endpoint()
    if not url:
        print("helm relevance warm: %s" % why, file=sys.stderr)
        return 1
    done = 0
    for i in range(0, len(rows), 32):
        batch = [{"text": r["note"]} for r in rows[i:i + 32]]
        try:
            resp = relevance._post(url + "/embed", {"rows": batch}, 120)
        except Exception as exc:            # noqa: BLE001 — reported, rc 1
            print("helm relevance warm: %s: %s" % (exc.__class__.__name__, exc), file=sys.stderr)
            return 1
        done += int(resp.get("embedded") or 0)
    print("helm relevance warm: %d store lines, %d newly embedded" % (len(rows), done))
    return 0


def cmd_relevance(args):
    args = list(args or ())
    verb = args[0] if args else "status"
    rest = args[1:]
    if verb in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    if verb == "status":
        return _status()
    if verb == "report":
        return _report(rest)
    if verb == "show":
        return _show(rest)
    if verb == "warm":
        return _warm(rest)
    if verb == "score-turn":
        return relevance.run_worker()
    if verb == "remeasure":
        from . import relevance_remeasure
        return relevance_remeasure.cmd(rest)
    if verb == "serve":
        from . import relevanced
        return relevanced.serve(rest)
    print("helm relevance: unknown subverb %r\n%s" % (verb, USAGE), file=sys.stderr)
    return 2
