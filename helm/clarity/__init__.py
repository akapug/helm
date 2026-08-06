#!/usr/bin/env python3
"""helm.clarity — a controlled language for agent coordination, checkable.

THE INVARIANT (docs/PLAN-CLARITY.md): clarity is a property you can CHECK,
not a property you can request. ASD-STE100 moved the constraint off the
author's spirit and onto vocabulary and grammar, where a machine can verify
conformance — a die, not a review. The measured experiment behind this
(violations per 100 words, two model families): banning words moved Claude
3%; giving it the system halved violations. So helm ships the system AND the
die off one table:

              helm/clarity/rules.py        <- ONE table
                 |                |
      generation |          check |
                 v                v
        helm clarity skill   helm clarity check <file|->
        (write-time system)  (deterministic, exit 1)

TARGET TEXT (the fork the plan resolved): coordination traffic first — chat
posts, dispatch briefs, verdicts, handoffs. Our 3am non-native mechanic is a
cross-family agent on a fresh context at hour twenty reading another family's
verdict. Docs inherit the same table for free.

OUT OF v1, DELIBERATELY (noted, not built): the Stop-hook rung (advisory only
after the linter has run against real traffic for a week — a die aimed at
coordination text can silence the fleet, and that failure is worse than
slop); anything needing POS tagging (see rules.NOT_CHECKABLE); any
model-assisted rule.

THE POSITIVE CONTROL LAW (today's council): a die that has never stamped a
real part is not verified, merely unrefuted. tests/fixtures carries a REAL
historical helm commit message this linter MUST flag, re-checked by the
suite; when the control stops firing the linter is presumed broken.
"""
import json
import os
import sys

from .rules import (RULES, NOT_CHECKABLE, OWNER_ONLY, SENTENCE_MAX,  # noqa: F401
                    check_text, word_count)


def lexicon_terms(project=None):
    """({term: definition}, error) — the CURATED controlled vocabulary.

    Reads store entries of type lexicon from the AUTHORED helm roots only
    (helm-global + project). The adopted root is the raw claude memory —
    ~180 entries carrying other projects' vocabularies —
    and treating that as the controlled vocabulary would make every post
    about a neighbour project a violation. The curated layer is ~17 terms,
    each with one definition, already maintained; that is the dictionary.

    Terms are normalised: the store carries a few terms with a trailing ':'
    (an artifact of their capture), and 'de-meld:' must match 'de-meld'.

    UNAVAILABLE IS NOT EMPTY: a store read failure returns the error out of
    band so the caller can say "the drift rule did not run" — an absent
    lexicon must never render as a clean drift check."""
    try:
        from ..store import load_all
        rows = load_all(project=project, types=("lexicon",))
    except Exception as e:                    # noqa: BLE001 — declared, not hidden
        return {}, "store unreadable (%s: %s)" % (type(e).__name__, e)
    out = {}
    for e in rows:
        if e.get("root") not in ("helm-global", "project"):
            continue
        term = str(e.get("id") or "").strip().rstrip(":").strip().lower()
        if term:
            out[term] = e.get("statement") or ""
    return out, None


def render_report(name, findings, n_words, lexicon_n, lexicon_error=None):
    """The human report: one line per finding, then the honest summary with
    the violations-per-100-words number the experiment scored — the same
    metric, so our output is comparable to the published baselines."""
    lines = []
    for f in findings:
        tag = f["rule"] + (" weak" if f["weak"] else "") \
            + (" advisory" if f["severity"] == "advisory" else "")
        lines.append("%s:%d: [%s] %s" % (name, f["line"], tag, f["message"]))
    errors = [f for f in findings if f["severity"] == "error"]
    advis = len(findings) - len(errors)
    per100 = (len(errors) * 100.0 / n_words) if n_words else 0.0
    lex = ("lexicon unavailable — the drift rule DID NOT RUN (%s)"
           % lexicon_error) if lexicon_error \
        else "lexicon: %d term%s" % (lexicon_n, "s"[:lexicon_n != 1])
    lines.append("helm clarity: %d violation%s, %d advisor%s in %d words — "
                 "%.2f violations per 100 words (%s)"
                 % (len(errors), "s"[:len(errors) != 1], advis,
                    "y" if advis == 1 else "ies", n_words, per100, lex))
    return lines


def render_skill(lexicon=None):
    """The write-time system — the consumer the experiment measured as the
    real win. Rendered FROM the table so it cannot drift from the check."""
    out = ["# clarity — the helm write-time system",
           "",
           "The same table `helm clarity check` enforces. Forked from "
           "ste-writing-skill.md (github.com/woosal1337/blog, ep01) and "
           "ASD-STE100; two rules are helm's own (lexicon-drift, provenance).",
           "Modes: strict = instructions, verdicts, runbooks (%d-word cap); "
           "descriptive = design posts, docs (%d); owner = anything the OWNER "
           "reads (%d, plus the register-gloss rules)."
           % (SENTENCE_MAX["strict"], SENTENCE_MAX["descriptive"],
              SENTENCE_MAX["owner"]),
           "",
           "## Rules"]
    for r in RULES:
        label = r["id"] + (", weak" if r["weak"] else "") \
            + (", advisory" if r["severity"] == "advisory" else "") \
            + (", owner mode only" if r["id"] in OWNER_ONLY else "")
        out.append("- [%s] %s (%s)" % (label, r["guidance"], r["source"]))
    out += ["", "## Not checkable here (stdlib-only: no POS tagger)"]
    out += ["- %s" % x for x in NOT_CHECKABLE]
    out.append("These need human judgment; the die does not pretend to them.")
    if lexicon:
        out += ["", "## The lexicon (one name for one thing)"]
        out += ["- %s: %s" % (t, (d or "").split("\n")[0][:120])
                for t, d in sorted(lexicon.items())]
    return out


_USAGE = ("clarity check [<file>|-] [--strict|--owner] [--project P] [--no-store] "
          "[--json] | rules [--json] | skill [--project P] [--no-store] — "
          "the ASD-STE100-descended clarity die (one rule table, "
          "deterministic, exit 1 on violation)")
_USAGE_CHECK = ("clarity check [<file>|-] [--strict|--owner] [--project P] "
                "[--no-store] [--json]  (no file: reads stdin; --strict = "
                "instruction mode, %d-word sentences; --owner = text the OWNER "
                "reads, same cap plus the register-gloss rules)"
                % SENTENCE_MAX["strict"])


def _split_args(args, valued):
    """(flags_and_valued, positionals) — guard_tail judges the first list."""
    tail, pos, i = [], [], 0
    while i < len(args):
        a = args[i]
        if a in valued:
            tail.extend(args[i:i + 2])
            i += 2
            continue
        (tail if a.startswith("--") or a in ("-h",) else pos).append(a)
        i += 1
    return tail, pos


def _read_source(pos):
    """(name, text, err) from the one positional, '-', or stdin."""
    if len(pos) > 1:
        return None, None, "one input at most (got %d)" % len(pos)
    if pos and pos[0] != "-":
        try:
            with open(pos[0], encoding="utf-8", errors="replace") as f:
                return pos[0], f.read(), None
        except OSError as e:
            return None, None, "cannot read %s: %s" % (pos[0], e)
    if not pos and sys.stdin.isatty():
        return None, None, "no input: name a file, or pipe the text " \
            "(usage: helm %s)" % _USAGE_CHECK
    return "<stdin>", sys.stdin.read(), None


def _cmd_check(args):
    from ..cli import guard_tail
    tail, pos = _split_args(args, valued=("--project",))
    rc = guard_tail("helm clarity check", tail,
                    flags=("--strict", "--owner", "--json", "--no-store"),
                    valued=("--project",), usage=_USAGE_CHECK)
    if rc is not None:
        return rc
    name, text, err = _read_source(pos)
    if err:
        print("helm clarity check: " + err, file=sys.stderr)
        return 2
    project = args[args.index("--project") + 1] if "--project" in args else None
    lexicon, lex_err = ({}, None) if "--no-store" in args \
        else lexicon_terms(project)
    if "--strict" in args and "--owner" in args:
        print("helm clarity check: --strict and --owner set different caps "
              "and different rules; choose one", file=sys.stderr)
        return 2
    mode = "owner" if "--owner" in args else \
        "strict" if "--strict" in args else "descriptive"
    findings = check_text(text, mode=mode, lexicon=lexicon)
    n_words = word_count(text)
    errors = sum(1 for f in findings if f["severity"] == "error")
    if "--json" in args:
        print(json.dumps({
            "file": name, "mode": mode, "words": n_words,
            "violations": errors, "advisories": len(findings) - errors,
            "per_100_words": round(errors * 100.0 / n_words, 2) if n_words else 0.0,
            "lexicon_terms": len(lexicon), "lexicon_error": lex_err,
            "findings": findings}))
    else:
        for line in render_report(name, findings, n_words, len(lexicon), lex_err):
            print(line)
    return 1 if errors else 0


def _cmd_rules(args):
    from ..cli import guard_tail
    rc = guard_tail("helm clarity rules", args, flags=("--json",),
                    usage="clarity rules [--json] — the one table, inspectable")
    if rc is not None:
        return rc
    rows = [{k: r[k] for k in ("id", "severity", "source", "weak", "guidance")}
            for r in RULES]
    if "--json" in args:
        print(json.dumps({"rules": rows, "not_checkable": list(NOT_CHECKABLE)}))
        return 0
    w = max(len(r["id"]) for r in rows)
    for r in rows:
        sev = r["severity"] + ("/weak" if r["weak"] else "") \
            + ("/owner" if r["id"] in OWNER_ONLY else "")
        print("%s  %-14s %-8s %s" % (r["id"].ljust(w), sev, r["source"],
                                     r["guidance"]))
    print("not checkable (stdlib-only, no POS): " + "; ".join(NOT_CHECKABLE))
    return 0


def _cmd_skill(args):
    from ..cli import guard_tail
    tail, pos = _split_args(args, valued=("--project",))
    rc = guard_tail("helm clarity skill", tail + pos,
                    flags=("--no-store",), valued=("--project",),
                    usage="clarity skill [--project P] [--no-store] — the "
                          "write-time system, rendered from the same table")
    if rc is not None:
        return rc
    project = args[args.index("--project") + 1] if "--project" in args else None
    lexicon, lex_err = ({}, None) if "--no-store" in args \
        else lexicon_terms(project)
    for line in render_skill(lexicon):
        print(line)
    if lex_err:
        print("(lexicon unavailable — rendered without terms: %s)" % lex_err,
              file=sys.stderr)
    return 0


_SUBS = {"check": _cmd_check, "rules": _cmd_rules, "skill": _cmd_skill}


def cmd_clarity(args):
    """clarity check|rules|skill — the clarity die over coordination text."""
    if not args or args[0] in ("-h", "--help"):
        print("helm " + _USAGE)
        return 0 if args else 2
    fn = _SUBS.get(args[0])
    if fn is None:
        from ..cli import suggest
        print("helm clarity: unknown subverb '%s'%s (helm %s)"
              % (args[0], suggest(args[0], _SUBS), _USAGE), file=sys.stderr)
        return 2
    return fn(args[1:])


# ---------------------------------------------------------------------------
# THE OWNER-BOUND ADVISORY — the L3 adapter tap (helmese draft-2, step 2)
# ---------------------------------------------------------------------------
#
# Four verbs write text the OWNER reads, and each one's own registered help
# says so: `note set` ("one owner-facing headline"), `asks add` ("told the
# OWNER"), `board landed` ("the owner console's lane truth"), `chat post`
# ("web panel = the owner's surface"). Those four call this. Nothing else
# should — an advisory on agent-to-agent text would nag the fleet for speaking
# the register it was given.
#
# LAWS, borrowed wholesale from toolwhisper because they are what keep a nudge
# from becoming noise:
#   * ADVISORY, NEVER BLOCKING. It cannot change an exit code and it cannot
#     refuse a write. The clarity plan put the Stop-hook rung out of v1 for
#     this reason: a die aimed at coordination text can silence the fleet.
#   * FAIL-OPEN, ALWAYS. Any trouble returns [] and the caller proceeds. An
#     advisory that can break `helm note set` is worse than no advisory.
#   * SILENCEABLE PER SURFACE, one env var with one polarity:
#     HELM_CLARITY_ADVISE_OFF=all | note,board  (named surfaces are silenced).
#   * BUDGETED. At most three findings reach the eye, then a count. An
#     advisory that prints twelve lines is a wall the reader learns to skip.
ADVISE_OFF_ENV = "HELM_CLARITY_ADVISE_OFF"
ADVISE_MAX = 3


def _advise_silenced(surface):
    raw = (os.environ.get(ADVISE_OFF_ENV) or "").strip().lower()
    if not raw:
        return False
    off = {p.strip() for p in raw.split(",") if p.strip()}
    return "all" in off or str(surface).lower() in off


def advise(text, surface, stream=None):
    """Owner-mode findings for owner-bound `text`, printed as advice.

    Returns the lines emitted, so a caller can test it without capturing a
    stream. Never raises: every failure path returns []."""
    try:
        # A non-string is a caller bug, never owner-bound text. str()ing one
        # can MANUFACTURE register symbols from a repr — "<object object at
        # 0x...>" is how the ASCII-operator false positive was found.
        if not isinstance(text, str) or not text.strip() \
                or _advise_silenced(surface):
            return []
        findings = check_text(text, mode="owner", lexicon={})
        if not findings:
            return []
        shown = findings[:ADVISE_MAX]
        lines = ["[helm clarity/%s] %s%s" % (surface, f["message"],
                 "" if f["severity"] == "error" else " (advisory)")
                 for f in shown]
        if len(findings) > len(shown):
            lines.append("[helm clarity/%s] %d more — `helm clarity check "
                         "--owner` on the full text (silence: %s=%s)"
                         % (surface, len(findings) - len(shown),
                            ADVISE_OFF_ENV, surface))
        for ln in lines:
            print(ln, file=stream or sys.stderr)
        return lines
    except Exception:                    # noqa: BLE001 — fail-open by law
        return []
