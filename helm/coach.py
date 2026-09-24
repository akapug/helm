#!/usr/bin/env python3
"""helm coach — the capture front door with the 4-step GATE.

One verb for "steer me durably, in plain words" — the chat-path partner to
`helm premise`. The owner speaks a lesson (typos expected) and coach PLACES it
right instead of letting the store accrete duplicates (the 46 prem/prior twins
drain measured are the cost of NOT having this gate). The ancestor's /learn
intake discipline, lifted out of a claude-only command file into a CLI every
harness shares — codex/opencode/bare terminal get the identical verb.

The GATE is MECHANICAL: deterministic shape rules, no model call, so the routing
is the same on every host and every family (the card's own RISKS note — keep the
router deterministic, propose-only shows it before landing):

  1. REFRAME   echo the lesson's intent back — STOP, name what you're capturing.
  2. PLACE     shape rules pick the layer, the SAME spirit as drain.classify
               (shape-only, no LLM):
                 term / "X = Y" / "define X"        -> lexicon
                 certain standing truth             -> premise (attested)
                   (always/never/must, no hedge)
                 hedged (probably/usually/might)    -> prior, confidence scaled
                 fires-unbidden ("when X, do Y")    -> reflex (prompt vs marker pick)
                 procedure (numbered steps/how-to)  -> skill/hook POINTER (not the store)
  3. NO-CRUFT  resolve + fuzzy-search the store INCLUDING retired/tombstoned
               entries; near matches ranked with ready-to-run supersede
               commands. UPGRADE-in-place beats a new near-duplicate.
  4. SIMPLIFY  flag what the new entry could retire.

Propose-only by default: prints the routing, the near matches, and the EXACT
one-paste landing verb. `--apply` lands it through the layer's OWN verb (so all
the store's guards run — coach composes resolve + add, it does not re-implement
them). A low-confidence route (no clear shape) is never guessed at on --apply:
it drops into the drain intake dir as a feedback entry, lossless, for `helm
drain` to route later. Fail-open throughout.

Framing is neutral proof-engineering (reliability/verifiability) — never
adversarial-security vocabulary (house law).
"""
import os
import re
import sys

from . import drain, home, pk, store

# --- routing thresholds ------------------------------------------------------
APPLY_MIN = 0.5    # below this, --apply preserves in drain intake, never guesses
NEAR = 0.18        # Jaccard >= this surfaces as a near match (anti-cruft)
SUBSUME = 0.5      # a live near match this dense could be retired (simplify)

# coach layer -> store type (reflex/skill live outside the typed store)
_LAYER_TYPE = {"lexicon": "lexicon", "premise": "prior", "prior": "prior",
               "heuristic": "heuristic", "reference": "reference",
               "reflex": None, "skill": None}
_LAYERS = tuple(_LAYER_TYPE)
_SUPERSEDE_LAYERS = ("premise", "prior", "heuristic", "reference")

# --- shape vocabularies (deterministic; no model call) -----------------------
_HEDGES = (
    "probably", "i think", "i suspect", "i reckon", "i guess", "my sense",
    "seems", "seem to", "appears to", "usually", "tends to", "tend to",
    "might", "maybe", "perhaps", "i believe", "often", "generally",
    "in general", "likely", "sometimes", "roughly", "afaik", "for the most part",
)
_STRONG_HEDGES = ("might", "maybe", "perhaps", "i guess", "i suspect", "roughly")
_SOFT_HEDGES = ("usually", "often", "generally", "in general", "for the most part",
                "tends to", "tend to")
_CERTAIN = (
    "always", "never", "must ", "must:", "law:", "canon:", "invariant",
    "shall ", "required", "forbidden", "no exceptions", "without exception",
)
_DEFN = re.compile(
    r"^\s*(?P<term>[\w][\w /+.-]{0,39}?)\s*"
    r"(?:=|:=|≡|\bmeans\b|\bstands for\b|\bis short for\b|\bis defined as\b)\s+"
    r"(?P<def>\S.*)$", re.I)
_WHEN = re.compile(r"\b(whenever|every time|each time|when)\b", re.I)
_EVERY_TURN = re.compile(r"\bevery turn\b|\beach turn\b", re.I)
_STEP = re.compile(r"(^|\n)\s*(\d+[.)]\s|step\s+\d+\b)|(\bfirst\b.*\bthen\b)", re.I)
_PROCEDURE = ("procedure", "checklist", "workflow", "step-by-step", "runbook",
              "the steps", "recipe")
_PATH = re.compile(r"(~?/[\w./~-]+|\b[\w-]+\.(?:md|flag|json|lock|txt))")
_MARKERISH = re.compile(r"\b(file|marker|flag|exists|present)\b", re.I)


def _has(low, words):
    return next((w for w in words if w in low), None)


def _mint_id(text, hint=""):
    """A filename-safe id from the DISTINCTIVE words of the lesson — reuses
    drain's keyword derivation so a coached id keys the same way a drained one
    does; first 4 distinctive tokens, else the first few words slugged."""
    kws = [k for k in drain._keywords_from(hint, text or "").split(",") if k][:4]
    slug = "-".join(kws) if kws else pk.slug(" ".join((text or "").split()[:5]))
    return pk.slug(slug) or "coached-note"


def _hedge_conf(low):
    if _has(low, _STRONG_HEDGES):
        return 0.5
    if _has(low, _SOFT_HEDGES):
        return 0.7
    return 0.6


# --- the router --------------------------------------------------------------

def _route_lexicon(stmt, low):
    body = stmt
    if low.startswith("term:"):
        body = stmt.split(":", 1)[1].strip()
    elif low.startswith("define "):
        rest = stmt.split(None, 1)[1] if " " in stmt else ""
        mm = re.match(r"(?P<term>[\w /+.-]{1,40}?)\s+(?:as|:|=|means)\s+(?P<def>.+)",
                      rest, re.I)
        if mm and mm.group("term").strip() and mm.group("def").strip():
            return {"layer": "lexicon", "confidence": 0.85, "why": "explicit define",
                    "id": pk.slug(mm.group("term")),
                    "extra": {"term": mm.group("term").strip(),
                              "definition": mm.group("def").strip()}}
        return None
    m = _DEFN.match(body)
    if not m:
        return None
    term, definition = m.group("term").strip(), m.group("def").strip()
    if not term or not definition or len(term.split()) > 4:
        return None
    return {"layer": "lexicon", "confidence": 0.8,
            "why": "definition marker (=/means/defined as)",
            "id": pk.slug(term),
            "extra": {"term": term, "definition": definition}}


def _reflex_extra(trigger, steer):
    """The prompt-vs-dynamic pick: a concrete marker PATH -> marker-file (fires
    while the path exists); otherwise a prompt regex over the trigger's
    distinctive words. Conservative — an ambiguous 'file' with no path stays a
    prompt reflex, never a dead marker."""
    pm = _PATH.search(trigger)
    if pm:
        return {"signal": "marker-file", "pattern": "", "marker": pm.group(0),
                "steer": steer, "trigger": trigger}
    kws = [k for k in drain._keywords_from("", trigger).split(",") if k][:4]
    pattern = r"\b(" + "|".join(re.escape(k) for k in kws) + r")\b" if kws else ""
    return {"signal": "prompt", "pattern": pattern, "marker": "",
            "steer": steer, "trigger": trigger}


def _route_reflex(stmt, low):
    if _EVERY_TURN.search(low):
        return {"layer": "reflex", "confidence": 0.75, "why": "every-turn steer",
                "id": _mint_id(stmt),
                "extra": {"signal": "every-turn", "pattern": "", "marker": "",
                          "steer": stmt, "trigger": ""}}
    w = _WHEN.search(stmt)
    if not w:
        return None
    after = stmt[w.end():].strip(" ,")
    seg = re.split(r",|\bthen\b", after, maxsplit=1, flags=re.I)
    trigger = seg[0].strip()
    steer = (seg[1].strip() if len(seg) > 1 else after).strip()
    if not steer or not trigger:
        return None
    extra = _reflex_extra(trigger, steer)
    strong = extra["signal"] == "marker-file" or bool(extra["pattern"])
    return {"layer": "reflex", "confidence": 0.8 if strong else 0.55,
            "why": "fires-unbidden trigger->steer (%s signal)" % extra["signal"],
            "id": _mint_id(trigger), "extra": extra}


def _route_skill(stmt, low):
    if low.startswith("how to ") or _STEP.search(stmt) or _has(low, _PROCEDURE):
        return {"layer": "skill", "confidence": 0.7,
                "why": "procedure/steps -> a skill or hook, not the typed store",
                "id": _mint_id(stmt), "extra": {}}
    return None


def _route_premise(stmt, low):
    if _has(low, _HEDGES):
        return None
    hit = _has(low, _CERTAIN)
    if not hit:
        return None
    return {"layer": "premise", "confidence": 0.85,
            "why": "certain standing truth (signal '%s', no hedge)" % hit.strip(),
            "id": _mint_id(stmt), "extra": {}}


def _route_prior(stmt, low):
    hedge = _has(low, _HEDGES)
    return {"layer": "prior",
            "confidence": 0.75 if hedge else 0.45,
            "why": ("hedged belief (signal '%s')" % hedge) if hedge
            else "default belief (no strong shape signal)",
            "id": _mint_id(stmt),
            "extra": {"belief_conf": _hedge_conf(low) if hedge else 0.6}}


def _forced(layer, stmt):
    """--as <layer>: honor the human's override, reusing the shape parse for its
    field split when it fits, else a sane fallback."""
    low = stmt.lower()
    if layer == "lexicon":
        r = _route_lexicon(stmt, low)
        if r:
            r["confidence"] = 0.9
            r["why"] = "forced --as lexicon"
            return r
        parts = stmt.split(None, 1)
        return {"layer": "lexicon", "confidence": 0.6, "why": "forced --as lexicon",
                "id": pk.slug(parts[0]) if parts else "term",
                "extra": {"term": parts[0] if parts else stmt,
                          "definition": parts[1] if len(parts) > 1 else stmt}}
    if layer == "reflex":
        r = _route_reflex(stmt, low)
        if r:
            r["confidence"] = 0.9
            r["why"] = "forced --as reflex"
            return r
        eid = _mint_id(stmt)
        return {"layer": "reflex", "confidence": 0.6, "why": "forced --as reflex",
                "id": eid,
                "extra": {"signal": "prompt", "marker": "", "steer": stmt,
                          "trigger": stmt,
                          "pattern": r"\b" + re.escape(eid.replace("-", " ")) + r"\b"}}
    extra = {"belief_conf": 0.6} if layer == "prior" else {}
    return {"layer": layer, "confidence": 0.9, "why": "forced --as " + layer,
            "id": _mint_id(stmt), "extra": extra}


def route(text, as_layer=None, id_override=None):
    """Deterministic shape router -> {layer,id,statement,confidence,why,extra}.
    `confidence` is the ROUTER's certainty in the layer choice (drives propose
    vs intake), distinct from a prior's stored belief confidence (extra)."""
    stmt = re.sub(r"\s+", " ", (text or "").strip())
    low = stmt.lower()
    if as_layer:
        r = _forced(as_layer, stmt)
    elif not stmt:
        r = {"layer": "prior", "confidence": 0.0, "why": "empty", "id": "",
             "extra": {"belief_conf": 0.6}}
    else:
        r = (_route_lexicon(stmt, low) or _route_reflex(stmt, low)
             or _route_skill(stmt, low) or _route_premise(stmt, low)
             or _route_prior(stmt, low))
    r["statement"] = stmt
    r.setdefault("extra", {})
    r["id"] = id_override or r.get("id") or _mint_id(stmt, r["extra"].get("term", ""))
    return r


# --- NO-CRUFT: resolve + fuzzy search (incl. retired/tombstoned) -------------

def _tokens(s):
    return set(re.split(r"[^a-z0-9]+", (s or "").lower())) - {""}


def _jaccard(a, b):
    return len(a & b) / len(a | b) if a and b else 0.0


def near_matches(statement, layer, project=None, cap=4):
    """Same-topic store entries, LIVE + retired/tombstoned/candidate (the
    anti-cruft point: a lesson captured-then-retired must still surface so it is
    UPGRADED, not re-minted). Jaccard token overlap; same-layer-type ranked
    ahead of cross-type; the entries that already JIT-fire for this text are
    folded in even below the Jaccard floor. -> [(entry, overlap)] highest first."""
    want = _tokens(statement)
    if not want:
        return []
    etype = _LAYER_TYPE.get(layer)
    scored, seen = [], set()
    for e in store.load_all(project=project, include_retired=True):
        ov = _jaccard(want, _tokens(e.get("statement") or e.get("definition") or ""))
        key = (e["type"], str(e["id"]))
        if ov >= NEAR and key not in seen:
            seen.add(key)
            scored.append((1 if e["type"] == etype else 0, ov, e))
    for e in store.resolve_prompt(statement, project=project):   # what already fires
        key = (e["type"], str(e["id"]))
        if key not in seen:
            seen.add(key)
            scored.append((1 if e["type"] == etype else 0,
                           _jaccard(want, _tokens(e.get("statement"))), e))
    scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
    return [(e, ov) for _s, ov, e in scored[:cap]]


# --- the one-paste landing verb ----------------------------------------------

def landing_verb(r, project=None):
    pflag = (" --project " + project) if project else ""
    layer, eid, stmt, x = r["layer"], r["id"], r["statement"], r.get("extra", {})
    if layer == "lexicon":
        return 'helm store add lexicon "%s | %s"%s' % (
            x.get("term") or eid, x.get("definition") or stmt, pflag)
    if layer == "premise":
        return 'helm premise "%s | %s"%s' % (eid, stmt, pflag)
    if layer == "prior":
        return 'helm store add prior "%s | %s | %.2f"%s' % (
            eid, stmt, x.get("belief_conf", 0.6), pflag)
    if layer in ("heuristic", "reference"):
        return 'helm store add %s "%s | %s"%s' % (layer, eid, stmt, pflag)
    if layer == "reflex":
        sig, tail = x.get("signal", "prompt"), ""
        if sig == "prompt" and x.get("pattern"):
            tail = " --pattern '%s'" % x["pattern"]
        elif sig == "marker-file":
            tail = " --marker '%s'" % (x.get("marker") or "<path-to-marker>")
        return 'helm reflex add "%s | %s" --signal %s%s%s' % (
            eid, x.get("steer") or stmt, sig, tail, pflag)
    return ("# procedure -> author a skill (~/.claude/commands/%s.md) or a helm "
            "hook; the typed store is for facts, not runbooks" % eid)


def plan(text, project=None, as_layer=None, id_override=None):
    """The full proposal: route + near matches + landing verb + simplify set.
    Pure derivation, no writes — the propose surface and the test seam."""
    r = route(text, as_layer=as_layer, id_override=id_override)
    r["near"] = near_matches(r["statement"], r["layer"], project=project)
    r["landing"] = landing_verb(r, project)
    etype = _LAYER_TYPE.get(r["layer"])
    r["subsumes"] = [(e, ov) for e, ov in r["near"]
                     if ov >= SUBSUME and e.get("status") == store.STATUS_LIVE
                     and e["type"] == etype]
    return r


# --- --apply: land through the layer's own verb, or preserve losslessly ------

def _store_add(t, a, b, project, extra=None):
    payload = a + " | " + b + "".join(" | " + e for e in (extra or []))
    args = ["add", t, payload]
    if project:
        args += ["--project", project]
    return store.cmd_store(args)


def _symptom_kw(statement):
    """A keywords CSV for a coached lesson's store landing: the statement's
    non-generic content words, first-seen order, capped at 8.

    The store's add-time findability lint refuses an EMPTY keywords field
    (measured: 9% of the store was empty-keyword near-unfindable, and this
    landing leg was one of the faucets — it authored NO keywords at all).
    Coach composes the store's guards rather than re-implementing them (its
    own charter), so it must hand `add` what the gate demands: derived with
    the store's own generic-word law, single words only — the gate's stem
    decomposition owns sub-phrase work."""
    out = []
    for w in re.findall(r"[a-z0-9][a-z0-9'-]*", statement.lower()):
        if w not in store.GENERIC_KEYWORDS and w not in out:
            out.append(w)
    return ", ".join(out[:8])


def to_intake(r):
    """Lossless fallback: a low-confidence (or skill) route drops into the drain
    intake dir as a feedback entry — drain routes it to a typed home later.
    Nothing captured is ever lost, even when the shape is ambiguous."""
    d = store.adopted_dir()
    os.makedirs(d, exist_ok=True)
    base = "feedback-" + (pk.slug(r["id"]) or "coached")
    path = os.path.join(d, base + ".md")
    n = 2
    while os.path.exists(path):
        path = os.path.join(d, "%s-%d.md" % (base, n))
        n += 1
    ts = pk.now_ts()
    st = re.sub(r"\s+", " ", r["statement"].replace('"', "'"))[:200]
    body = "\n".join([
        "---",
        "name: " + os.path.basename(path)[:-3],
        'description: "feedback: ' + st + '"',
        "metadata:",
        "  node_type: memory",
        "  type: feedback",
        "  source: coach",
        "  stated_ts: " + ts,
        "  coach_route: " + r["layer"],
        "---", "", r["statement"], ""])
    pk.atomic_write(path, body)
    pk.event("coach.intake", os.path.basename(path), r["statement"][:120])
    return path


def apply(r, project=None, supersede_old=None):
    """Land the routed entry. -> (path-or-rc, outcome). A route below APPLY_MIN,
    or a skill pointer (nothing to store), is preserved in the drain intake.

    Landing and lifecycle results are truthful: a failed landing never advances
    supersession, premise evolution uses its native attested route, and an
    unsupported lifecycle refuses before minting anything.
    """
    layer, eid, x = r["layer"], r["id"], r.get("extra", {})
    if supersede_old and (layer not in _SUPERSEDE_LAYERS
                          or r["confidence"] < APPLY_MIN):
        print("helm coach: --supersede is unsupported for a %s route that cannot "
              "land semantically — use that layer's native evolution path" % layer,
              file=sys.stderr)
        return 1, "refused"
    if layer == "skill" or r["confidence"] < APPLY_MIN:
        return to_intake(r), "intake"
    if layer == "premise":
        from . import premise
        payload = eid + " | " + r["statement"] + " | " + _symptom_kw(r["statement"])
        argv = (["--supersede", supersede_old, payload] if supersede_old else [payload])
        if project:
            argv += ["--project", project]
        rc = premise.cmd_premise(argv)
        return rc, ("superseded" if not rc and supersede_old else
                    "new" if not rc else "refused")
    if layer == "reflex":
        from . import reflex
        argv = ["add", eid + " | " + (x.get("steer") or r["statement"]),
                "--signal", x.get("signal", "prompt")]
        if x.get("signal") == "prompt" and x.get("pattern"):
            argv += ["--pattern", x["pattern"]]
        if x.get("signal") == "marker-file" and x.get("marker"):
            argv += ["--marker", x["marker"]]
        if project:
            argv += ["--project", project]
        rc = reflex.cmd_reflex(argv)
    elif layer == "lexicon":
        rc = _store_add("lexicon", x.get("term") or eid,
                        x.get("definition") or r["statement"], project)
    elif layer == "prior":
        rc = _store_add("prior", eid, r["statement"], project,
                        extra=["%.2f" % x.get("belief_conf", 0.6),
                               _symptom_kw(r["statement"])])
    else:
        # heuristic: slot 3 is the trigger CSV; reference: slot 3 is url
        # (left empty), slot 4 the keywords CSV
        kw = _symptom_kw(r["statement"])
        rc = _store_add(layer, eid, r["statement"], project,
                        extra=[kw] if layer == "heuristic" else ["", kw])
    if rc:
        return rc, "refused"
    if not supersede_old:
        return rc, "new"
    sargs = ["supersede", pk.now_ts(), supersede_old, eid,
             "coached: upgraded in place"]
    if project:
        sargs += ["--project", project]
    src = store.cmd_store(sargs)
    return (src, "superseded") if not src else (src, "supersede-refused")


# --- CLI ---------------------------------------------------------------------

_USAGE = ("usage: helm coach <lesson...> [--apply] [--as L] [--id ID] "
          "[--project P] [--supersede OLD] [--json]\n"
          "  the capture front door: reframe -> place -> search-first -> "
          "simplify. Propose-only unless --apply.\n"
          "  L = lexicon|premise|prior|heuristic|reference|reflex|skill; "
          "lesson may also pipe on stdin.")


def _pct(ov):
    return "%d%%" % round(ov * 100)


def _print_propose(r, project):
    print('helm coach: reframed -> "%s"' % r["statement"])
    print("  place:    %-9s [id: %s]  - %s" % (r["layer"], r["id"], r["why"]))
    near = r["near"]
    if not near:
        print("  no-cruft: clean - nothing related in the store (incl. retired)")
    else:
        dupes = sum(1 for _e, ov in near if ov >= NEAR)
        print("  no-cruft: %d related entr%s (resolve + fuzzy, incl. "
              "retired/tombstoned; %d near-dup):"
              % (len(near), "y" if len(near) == 1 else "ies", dupes))
        ts = pk.now_ts()
        etype = _LAYER_TYPE.get(r["layer"])
        pflag = (" --project " + project) if project else ""
        for e, ov in near:
            mark = "" if e["status"] == store.STATUS_LIVE else " " + e["status"]
            print("      - %s [%s %.2f%s] %s overlap"
                  % (e["id"], e["type"], e.get("confidence", 1.0), mark, _pct(ov)))
            if ov >= NEAR and e["status"] == store.STATUS_LIVE \
                    and e["type"] == etype and str(e["id"]) != r["id"]:
                print("        upgrade in place:  helm store supersede %s %s %s "
                      "<reason>%s" % (ts, e["id"], r["id"], pflag))
    if r["subsumes"]:
        print("  simplify: this could retire: "
              + ", ".join(str(e["id"]) for e, _ in r["subsumes"]))
    print("  land it:  " + r["landing"])
    print("  (propose-only - re-run with --apply to land, or paste the verb above)")


def cmd_coach(args):
    """coach <lesson...> [--apply] [--as L] [--id ID] [--project P]
    [--supersede OLD] [--json] — the capture front door with the 4-step GATE."""
    args = list(args)
    apply_it = _pop(args, "--apply")
    as_json = _pop(args, "--json")
    project = _pop_val(args, "--project")
    as_layer = _pop_val(args, "--as")
    id_override = _pop_val(args, "--id")
    supersede_old = _pop_val(args, "--supersede")
    # the lesson is free text, so guard_tail cannot own this tail — but a
    # remaining '--' token after the pops is an unknown FLAG, not lesson
    # prose, and used to be silently dropped: `coach lesson --bogus --apply`
    # landed the lesson with --bogus eaten. Junk refuses before help, before
    # plan(); single-dash tokens stay lesson text ("use -j"). A clean tail
    # carrying -h/--help prints usage and stops.
    junk = [a for a in args if a.startswith("--") and a != "--help"]
    if junk:
        from .cli import suggest
        print("helm coach: unknown arg '%s'%s (%s)"
              % (junk[0], suggest(junk[0], ("--apply", "--json", "--as",
                                            "--id", "--project",
                                            "--supersede", "--help")),
                 _USAGE), file=sys.stderr)
        return 2
    if any(a in ("-h", "--help") for a in args):
        print(_USAGE)
        return 0
    if as_layer and as_layer not in _LAYERS:
        print("helm coach: unknown --as layer '%s' (%s)"
              % (as_layer, "|".join(_LAYERS)), file=sys.stderr)
        return 2
    text = " ".join(a for a in args if not a.startswith("--"))
    if not text and not sys.stdin.isatty():
        text = sys.stdin.read()
    if not text.strip():
        print(_USAGE, file=sys.stderr)
        return 2

    r = plan(text, project=project, as_layer=as_layer, id_override=id_override)

    if as_json:
        import json
        print(json.dumps({
            "layer": r["layer"], "id": r["id"], "statement": r["statement"],
            "confidence": r["confidence"], "why": r["why"], "landing": r["landing"],
            "near": [{"id": str(e["id"]), "type": e["type"], "status": e["status"],
                      "overlap": round(ov, 3)} for e, ov in r["near"]],
            "subsumes": [str(e["id"]) for e, _ in r["subsumes"]],
        }, ensure_ascii=False, indent=2))
        return 0

    if not apply_it:
        _print_propose(r, project)
        return 0

    rc, outcome = apply(r, project=project, supersede_old=supersede_old)
    if outcome == "intake":
        print("helm coach: low-confidence route (%s) - preserved in the drain "
              "intake, lossless" % r["why"])
        print("  intake: " + rc)
        print("  route later: helm drain")
    print("coached -> %s (%s) | %s" % (r["layer"], r["id"], outcome))
    return rc if isinstance(rc, int) else 0


def _pop(args, flag):
    if flag in args:
        args.remove(flag)
        return True
    return False


def _pop_val(args, flag):
    if flag not in args:
        return None
    i = args.index(flag)
    val = args[i + 1] if i + 1 < len(args) else None
    del args[i:(i + 2) if val is not None else (i + 1)]
    return val


if __name__ == "__main__":
    sys.exit(cmd_coach(sys.argv[1:]))
