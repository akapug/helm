#!/usr/bin/env python3
"""helm eval — pin the arms before measuring, and register the rule before running.

THE FLAGSHIP 0.3 DELIVERABLE is the cc-codex vs pi-codex evidence, and §E-5 of
the planning seed states the condition it lives or dies on: "Same model, same
tier, same task suite, same independent reviewer, confound extensions held
constant. Without this pin the numbers are cross-harness noise."

MEASURED 2026-07-29, the arms were NOT pinned and nothing would have said so.
`cc-codex` reaches codex through helm's CLIProxyAPI on the owner's subscription
OAuth; pi had exactly one provider configured, openrouter. Run as specified,
the eval would have compared Claude-Code-over-subscription against
pi-over-OpenRouter and credited the entire difference to the HARNESS —
different provider, different billing, different rate limits, possibly a
different model build. The confound §E-5 forbids, sitting inside the design
that forbids it, and the numbers would have looked exactly like an answer.

So the first thing built here is not a runner. It is the REFUSAL.

A PROBE THAT CANNOT SAY OTHERWISE IS NOT A MEASUREMENT. `arms()` is written so
that its counterfactual is real: point the two arms at different endpoints or
different model ids and it reports NOT PINNED with the specific mismatch. A
green from this check means something because a red is reachable, and a test
plants each mismatch to prove the instrument can still fail.

PRE-REGISTRATION IS PART OF THE INSTRUMENT, not paperwork. §C.5 requires the
flip-threshold to be fixed BEFORE the run, because a decision rule chosen after
seeing the numbers is not a decision rule. `register()` writes it to the evals
shelf with the arms fingerprint it was registered against, so a later run
against DIFFERENT arms cannot quietly inherit an earlier registration.

WHAT THIS MODULE DELIBERATELY DOES NOT DO: run the task suite. §C.2 is explicit
that reliability is "read off existing surfaces, not new harness" — helm
already measures silent drops, delivery, and cursor advance. Building a second
measurement stack beside those would produce a number nobody could reconcile
with the fleet's own.
"""
import json
import os
import re
import sys

from . import home

# The arms, by name. Kept as data because §C.1 insists the pin is EXACT and a
# pin described in prose is a pin nobody can check.
CC_ARM = "cc-codex"
PI_ARM = "pi-codex"

KEY_ENV_NAME = "HELM_PI_PROXY_KEY"

_BASEURL = re.compile(r'baseUrl:\s*"([^"]+)"')
_MODEL_ID = re.compile(r'id:\s*"([^"]+)"')


def _cc_arm(seat="codex"):
    """(facts, err) for the Claude Code arm, read from the seat's own proxy.

    The PORT and MODEL come from the family table, never from the seat's 0600
    config — there is no reason to open a file holding a credential to learn
    which endpoint it fronts.
    """
    from . import seat as seatmod
    family, err = seatmod._seat_family(seat)
    if err:
        return None, err
    fam = seatmod.FAMILIES.get(family) or {}
    port = fam.get("port")
    if not port:
        return None, "family %s declares no proxy port" % family
    from . import pi as pimod
    resolved, resolve_err = pimod.seat_port(seat)
    if resolved is None:
        # `resolved or port` WAS THE SAME ALIAS ONE HOP OUT. This builds the
        # ENDPOINT an arm is measured at, and substituting the family base for an
        # instance whose own endpoint is unresolved points the arm at ANOTHER
        # seat's proxy — answering with another seat's credential and reporting it
        # as this seat's. The family seat itself always resolves (its endpoint IS
        # the base), so this refusal only reaches an instance that really has none.
        return None, (resolve_err or
                      "seat %s has no resolvable proxy endpoint" % seat)
    return {"arm": CC_ARM, "seat": seat, "harness": "claude-code",
            "endpoint": "http://127.0.0.1:%d/v1" % resolved,
            "model": fam.get("upstream_model") or fam.get("model"),
            "alias": fam.get("model")}, None


def _pi_arm(seat="codex", path=None):
    """(facts, err) for the pi arm, read from the GENERATED EXTENSION.

    Read from the installed artifact rather than from what helm would generate,
    because the eval's arm is whatever pi actually loads. A generator and an
    installed file that have drifted are exactly the state this check exists to
    catch, and asking the generator would agree with itself every time.
    """
    from . import pi as pimod
    path = path or os.path.join(pimod.DEFAULT_DIR, "helm-%s.ts" % seat)
    if not os.path.exists(path):
        return None, ("no pi extension installed at %s — run `helm pi "
                      "extension --seat %s --apply`; without it pi reaches its "
                      "OWN providers and the arms measure the PROVIDER, not "
                      "the harness" % (path, seat))
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            src = f.read()
    except OSError as e:
        return None, "pi extension unreadable (%s)" % e
    url = _BASEURL.search(src)
    mid = _MODEL_ID.search(src)
    if not (url and mid):
        return None, ("pi extension at %s declares no baseUrl/model id — it "
                      "cannot be compared" % path)
    return {"arm": PI_ARM, "seat": seat, "harness": "pi",
            "endpoint": url.group(1), "model": mid.group(1),
            "source": path}, None


def arms(seat="codex", pi_path=None):
    """(report, err) — are the two arms comparable?

    `report["pinned"]` is False with a populated `mismatches` list whenever the
    arms differ on anything that would let a harness comparison measure
    something else instead. Each mismatch names the FIELD, both values, and
    what the difference would be mistaken for.
    """
    cc, cc_err = _cc_arm(seat)
    pi_facts, pi_err = _pi_arm(seat, pi_path)
    report = {"seat": seat, "arms": {}, "mismatches": [], "pinned": False}
    if cc_err:
        report["mismatches"].append({"field": "cc-arm", "why": cc_err})
    else:
        report["arms"][CC_ARM] = cc
    if pi_err:
        report["mismatches"].append({"field": "pi-arm", "why": pi_err})
    else:
        report["arms"][PI_ARM] = pi_facts
    if cc_err or pi_err:
        return report, None

    if cc["endpoint"] != pi_facts["endpoint"]:
        report["mismatches"].append({
            "field": "endpoint", CC_ARM: cc["endpoint"],
            PI_ARM: pi_facts["endpoint"],
            "why": "different upstreams — a latency or reliability difference "
                   "would be the PROVIDER's, credited to the harness"})
    if cc["model"] != pi_facts["model"]:
        report["mismatches"].append({
            "field": "model", CC_ARM: cc["model"], PI_ARM: pi_facts["model"],
            "why": "different model builds — a quality difference would be the "
                   "MODEL's, credited to the harness"})
    report["pinned"] = not report["mismatches"]
    report["fingerprint"] = fingerprint(report)
    # RUNNABLE is a SECOND question. Pinned says the arms are comparable;
    # runnable says either of them can be driven at all. A run needs both, and
    # reporting only the first is how "PINNED" gets heard as "ready".
    blockers = []
    for name in sorted(report["arms"]):
        ok, why = reachable(report["arms"][name])
        report["arms"][name]["reachable"] = ok
        if not ok:
            blockers.append("%s: %s" % (name, why))
    report["runnable"] = report["pinned"] and not blockers
    report["run_blockers"] = blockers
    return report, None


def reachable(arm):
    """(ok, why) — can this arm's endpoint actually be TALKED TO right now?

    PINNED AND RUNNABLE ARE DIFFERENT CLAIMS, and conflating them is the
    over-claim this function exists to stop. `arms()` compares DECLARED
    CONFIGURATION: both sides naming one endpoint and one model. That is
    necessary and it is not sufficient — a pi arm whose provider points at the
    right proxy but which cannot authenticate against it is configured
    identically and cannot make a single request.

    Measured 2026-07-29, on this integrator's own commit three hours old: the
    generated extension references $HELM_PI_PROXY_KEY and NOTHING IN HELM SETS
    IT. `helm eval arms` reported PINNED, I told the room PINNED, and a
    reasonable reader hears "the eval can run". It could not.

    Deliberately a TCP-level probe and an env-PRESENCE check: no request is
    made and no token is read, so this never needs the credential it is
    checking for the existence of.
    """
    import socket
    from urllib.parse import urlparse
    u = urlparse(arm.get("endpoint") or "")
    host, port = u.hostname, u.port or (443 if u.scheme == "https" else 80)
    if not host:
        return False, "no parseable endpoint"
    try:
        with socket.create_connection((host, port), timeout=2):
            pass
    except OSError as e:
        return False, "endpoint %s:%d refused the connection (%s)" % (host, port, e)
    if arm.get("harness") == "pi" and not os.environ.get(KEY_ENV_NAME):
        return False, ("the endpoint answers but %s is UNSET — launch pi via "
                       "`helm pi run <seat>`, which execs it with the key in "
                       "the child environment; the arm is configured, not "
                       "runnable" % KEY_ENV_NAME)
    return True, "endpoint answers%s" % (
        "; %s is set" % KEY_ENV_NAME if arm.get("harness") == "pi" else "")


def fingerprint(report):
    """A short digest of WHAT WAS PINNED, so a pre-registration cannot be
    inherited by a later run against different arms."""
    import hashlib
    parts = []
    for name in sorted(report.get("arms") or {}):
        a = report["arms"][name]
        parts.append("%s|%s|%s" % (name, a.get("endpoint"), a.get("model")))
    return hashlib.blake2b("\n".join(parts).encode("utf-8"),
                           digest_size=8).hexdigest()


# The rule §C.5 requires to be fixed BEFORE the run. Stated here as data so it
# is diffable, and so "no post-hoc goalpost moving" is a property of the
# repository rather than of everyone's memory.
DECISION_RULE = {
    "adopt_pi_iff": [
        "silent_drop_rate(pi-codex) <= silent_drop_rate(cc-codex)",
        "end_to_end_latency(pi-codex) within +/-15% of cc-codex",
        "quality_gate_clear_rate(pi-codex) >= cc-codex",
    ],
    "otherwise": "pi stays a SUPPORTED but NOT PREFERRED harness",
    "primary_metric": "silent-drop rate — silence is not progress for a fleet",
    "grading": "an independent family seat rules each diff CLEAR/BLOCK; no arm "
               "grades itself",
    "order": "ABBA counter-balanced to wash out warmup and quota drift",
}


def register(report, out=None):
    """Write the pre-registration -> (path, err). Refuses on unpinned arms:
    registering a rule against arms that are not comparable records a decision
    procedure for an experiment that cannot be run."""
    if not report.get("pinned"):
        return None, ("arms are NOT pinned — registering a decision rule "
                      "against incomparable arms would pre-register a "
                      "measurement of the wrong thing")
    path = out or os.path.join(home.project_dir("helm"), "evals",
                               "cc-codex-vs-pi-codex-PREREGISTERED.json")
    body = {"registered_for_fingerprint": report["fingerprint"],
            "arms": report["arms"], "rule": DECISION_RULE}
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps(body, indent=2, sort_keys=True) + "\n")
    except OSError as e:
        return None, "could not write %s (%s)" % (path, e)
    return path, None


def report_lines(report):
    out = ["eval arms — %s vs %s (seat %s)" % (CC_ARM, PI_ARM, report["seat"])]
    for name in sorted(report.get("arms") or {}):
        a = report["arms"][name]
        out.append("  %-9s %-34s %s" % (name, a.get("endpoint"), a.get("model")))
    if report.get("pinned"):
        out.append("  PINNED — both arms traverse one endpoint on one model, "
                   "so a difference is the HARNESS (fingerprint %s)"
                   % report.get("fingerprint"))
        if report.get("runnable"):
            out.append("  RUNNABLE — both endpoints answer and pi can "
                       "authenticate")
        else:
            out.append("  NOT RUNNABLE — pinned is not the same as drivable:")
            for b in report.get("run_blockers") or []:
                out.append("    %s" % b)
    else:
        out.append("  NOT PINNED — this comparison would measure something "
                   "other than the harness:")
        for m in report["mismatches"]:
            if "why" in m and len(m) == 2:
                out.append("    %-9s %s" % (m["field"], m["why"]))
            else:
                out.append("    %-9s %s=%s vs %s=%s"
                           % (m["field"], CC_ARM, m.get(CC_ARM),
                              PI_ARM, m.get(PI_ARM)))
                out.append("              %s" % m["why"])
    return out


_USAGE = """usage: helm eval arms [--seat S] [--json]
       helm eval register [--seat S] [--out PATH]
       helm eval seed --atom FILE --repo DIR --run-id ID [--root DIR] [--json]
       helm eval run --seat S --atom FILE --repo DIR --run-id ID
                     [--arm NAME] [--root DIR] [--timeout SECS] [--json]

  The cc-codex vs pi-codex eval's guard rails, which exist BEFORE its runner
  on purpose — and the runner, which exists AFTER its pilot's defects.

  arms      Are the two arms comparable — same endpoint, same model? Without
            that pin a harness comparison measures the PROVIDER and the
            numbers still look like an answer. Exit 1 when NOT pinned.
  register  Write the §C.5 decision rule BEFORE any run, stamped with the arms
            fingerprint it was registered against, so a later run against
            different arms cannot inherit it. Refuses on unpinned arms.
  seed      Premise-check a task atom against current code; a refuted premise
            is recorded stale-and-skipped with evidence, never seeded.
  run       seed + drive the seat's OWN launch.sh (-p) under a per-run
            hook-stripped config, in a per-run dir, with an elapsed<=0
            refusal on the row (see helm/evalrun.py for the five findings).
"""


def cmd_eval(args):
    """eval arms|register|seed|run — pin the arms, register the rule, run
    the rig. seed/run live in evalrun (the pilot-shaped runner); arms and
    register stay here — the guard rails a run must clear first."""
    args = list(args or [])
    verb = args[0] if args else ""
    if not verb or verb in ("-h", "--help", "help"):
        print(_USAGE)
        return 0 if verb else 2
    if verb in ("seed", "run"):
        from . import evalrun
        return evalrun.cmd(verb, args[1:])
    if verb not in ("arms", "register"):
        print("helm eval: unknown verb '%s'" % verb, file=sys.stderr)
        print(_USAGE, file=sys.stderr)
        return 2
    rest = args[1:]
    from .cli import guard_tail
    rc = guard_tail("helm eval " + verb, rest, flags=("--json",),
                    valued=("--seat", "--out"), usage=_USAGE)
    if rc is not None:
        return rc

    def opt(name, default=None):
        return rest[rest.index(name) + 1] if name in rest \
            and rest.index(name) + 1 < len(rest) else default

    report, err = arms(opt("--seat", "codex"))
    if err:
        print("helm eval: " + err, file=sys.stderr)
        return 1
    if verb == "arms":
        if "--json" in rest:
            print(json.dumps(report, indent=2, sort_keys=True))
        else:
            for ln in report_lines(report):
                print(ln)
        # EXIT ON RUNNABLE, not merely on pinned. The exit code is what a
        # script reads, and the question a script is asking is "can the eval
        # run" — answering that with the narrower pin would hand a green to a
        # caller about to drive an arm that cannot authenticate.
        return 0 if report.get("runnable") else 1
    path, err = register(report, opt("--out"))
    if err:
        print("helm eval: " + err, file=sys.stderr)
        for ln in report_lines(report):
            print("  " + ln, file=sys.stderr)
        return 1
    print("helm eval: pre-registered %s" % path)
    print("  fingerprint %s — a run against different arms will not match it"
          % report["fingerprint"])
    return 0
