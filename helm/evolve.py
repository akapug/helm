#!/usr/bin/env python3
"""The self-evolution engine — one cycle of notice -> name -> propose.

The loop that keeps helm improving itself: observe the stores AND the live
behavior record, notice what drifted or accumulated, and PROPOSE the next
actions. The anti-rulesurf gate is constitutional: evolve NEVER edits
knowledge — every proposal is an explicit verb the operator (or a supervising
agent) runs deliberately. A system that edits its own beliefs unprompted
isn't learning, it's drifting.

Boundary, stated honestly: the sync observer (registry.sync) DOES write the
registry file and scaffold missing project home dirs — additive bookkeeping,
idempotent, never touching entries/beliefs. The drift observer runs with
snapshot=False so a pending drift report is never consumed. Everything else
is read-only — the behavior observers in particular never write the store,
the ledger, or the reflexes.

`helm evolve [--project P]` composes the observers:
  sync     is the registry current?
  drain    what raw intake is routable to typed homes?
  drift    what beliefs moved or broke? (+ paste-ready per-belief commands)
  fire     what does the inject fire-ledger say about the store's behavior?
             wallpaper    a jit entry firing in most non-silent turns
             dead-weight  live jit entries that NEVER fired (batched)
             silence      ~100% silent turns over a populated store
  reflex   which steers re-fire right after firing (steer not landing)?
  whoami   is the know-your-user leg still empty?

Behavior observers read the fire-ledger (inject's measurement spine, plus its
.1 rotation) tolerating absence and garbage — no ledger, no claims. The
ledger is read estate-wide (fire behavior is not scope-sliced); the store and
reflex sets are resolved under the cycle's scope, and every minted command
carries --project when scoped (the store-guard scope lesson). Behavior
proposals are ranked by evidence strength and capped per cycle; the cycle
output names its data window (over N turns since T).

Empty stores + steady beliefs + healthy home + quiet ledger -> one quiet line.
"""
import json

from . import drain, drift, pk, registry

WALLPAPER_RATE = 0.5   # jit entry firing in > this fraction of non-silent turns
WALLPAPER_MIN = 20     # non-silent turns before a fire-rate means anything
DEADWEIGHT_MIN = 200   # total turns before never-fired means anything
SILENT_RATE = 0.95     # ~100% silent
SILENT_MIN = 20        # total turns before a silent-rate means anything
HABIT_GAP = 3          # a reflex re-fire within this many turns = steer ignored
HABIT_MIN = 3          # ignored-steer episodes before proposing
BEHAVIOR_CAP = 10      # behavior proposals per cycle, ranked by evidence strength


def _pflag(project):
    return (" --project " + project) if project else ""


def _ledger_rows():
    """Fire-ledger rows oldest-first across the .1 rotation. Absence and
    garbage (torn lines, non-JSON, non-dict rows) are tolerated silently — a
    behavior observer must never crash or block the cycle."""
    from . import inject
    path = inject._ledger_path()
    rows = []
    for p in (path + ".1", path):
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                for line in f:
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(r, dict):
                        rows.append(r)
        except OSError:
            continue
    return rows


def _fired(row, lane):
    f = row.get("fired")
    return [pk.slug(str(i)) for i in (f.get(lane) or [])] if isinstance(f, dict) else []


def _fire_props(rows, project=None):
    """The fire-ledger observers -> [(strength, area, what, verb)]."""
    if not rows:
        return []
    from . import store
    out = []
    ts = pk.now_ts()
    pf = _pflag(project)
    live = {pk.slug(str(e["id"])): e for e in store.load_all(project=project)}
    non_silent = [r for r in rows if isinstance(r.get("fired"), dict)]
    ns, turns = len(non_silent), len(rows)

    # wallpaper — a jit entry firing in most non-silent turns is noise, not salience
    if ns >= WALLPAPER_MIN:
        fires = {}
        for r in non_silent:
            for s in set(_fired(r, "jit")):
                fires[s] = fires.get(s, 0) + 1
        for s, k in sorted(fires.items()):
            e = live.get(s)
            rate = k / ns
            if not e or rate <= WALLPAPER_RATE:
                continue
            pct = round(100 * rate)
            if e["type"] == "prior" and e.get("class") != "certain":
                verb = "helm store evidence %s %s -0.2 wallpaper: fired %d%% of non-silent turns%s" \
                    % (ts, e["id"], pct, pf)
            else:
                verb = "helm store get %s%s" % (e["id"], pf)  # keywords live in the file
            out.append((1.0 + rate, "fire",
                        "'%s' fired in %d/%d non-silent turns (%d%%) — wallpaper: "
                        "tighten keywords or demote" % (e["id"], k, ns, pct), verb))

    # dead-weight — live jit entries that never fired, batched (no per-entry spam)
    if turns >= DEADWEIGHT_MIN:
        ever = set()
        for r in non_silent:
            for lane in ("pinned", "jit"):
                ever.update(_fired(r, lane))
        dead = sorted(str(e["id"]) for e in store._jit_candidates(list(live.values()))
                      if pk.slug(str(e["id"])) not in ever)
        if dead:
            names = ", ".join(dead[:6]) + (" (+%d more)" % (len(dead) - 6) if len(dead) > 6 else "")
            out.append((0.9 + 0.001 * len(dead), "fire",
                        "%d live jit entr%s never fired over %d turns: %s — dormancy review"
                        % (len(dead), "ies" if len(dead) != 1 else "y", turns, names),
                        "helm store evidence %s <id> -0.3 dead-weight: zero fires in %d turns%s"
                        % (ts, turns, pf)))

    # silence — a populated store injecting nothing points at the hook estate
    if turns >= SILENT_MIN and live and (turns - ns) / turns >= SILENT_RATE:
        out.append((3.0, "fire",
                    "%d%% of %d turns silent with %d live entries — injection may be dark"
                    % (round(100 * (turns - ns) / turns), turns, len(live)),
                    "helm hooks status"))
    return out


def _reflex_props(rows, project=None):
    """Habituation: a reflex re-firing within HABIT_GAP turns of firing means
    the steer didn't land. The ledger records FIRES, not heeds — proximity
    re-fire is the only observable proxy, and the proposal says so."""
    from . import reflex
    live = {pk.slug(str(e["id"])): e for e in reflex.load_all(project)}
    last, episodes = {}, {}
    for i, r in enumerate(rows):
        for s in set(_fired(r, "reflex")):
            if s in last and i - last[s] <= HABIT_GAP:
                episodes[s] = episodes.get(s, 0) + 1
            last[s] = i
    out = []
    for s, n in sorted(episodes.items()):
        e = live.get(s)
        if not e or n < HABIT_MIN:
            continue
        out.append((1.0 + 0.05 * n, "reflex",
                    "reflex '%s' re-fired within %d turns of steering %dx — steer not "
                    "landing (the ledger logs fires, not heeds); reword or retire"
                    % (e["id"], HABIT_GAP, n),
                    "helm reflex retire %s" % e["id"]))
    return out


def _drift_props(found, project=None):
    """Per-finding paste-ready commands off the structured drift feed — one
    proposal per belief (strongest finding wins), rises stay quiet."""
    ts = pk.now_ts()
    pf = _pflag(project)
    out, seen = [], set()
    for f in found:
        if f["id"] in seen:
            continue
        if f["kind"] == "contradicted":
            out.append((2.0 + 0.01 * f["n"], "drift",
                        "premise '%s' holds 1.0 against %d agent contradiction%s — "
                        "supersede or retire" % (f["id"], f["n"], "s"[:f["n"] != 1]),
                        "helm store supersede %s %s <new-id> %s%s"
                        % (ts, f["id"], f["latest"][:60] or "contradicted", pf)))
        elif f["kind"] == "tier" and f["dir"] == "fell":
            tier = drift.ACT_AT if f["tier"] == "auto-act" else drift.DORMANT_BELOW
            out.append((0.8, "drift",
                        "belief '%s' fell below %s (%.2f -> %.2f) — re-confirm or let it decay"
                        % (f["id"], f["tier"], f["was"], f["now"]),
                        "helm store evidence %s %s +%.2f re-confirmed (%s fall review)%s"
                        % (ts, f["id"], tier - f["now"] + 0.05, f["tier"], pf)))
        elif f["kind"] == "decayed":
            out.append((0.6, "drift",
                        "'%s' dormant at %.2f — retire or re-confirm" % (f["id"], f["conf"]),
                        "helm store retire %s %s decayed dormant%s" % (ts, f["id"], pf)))
        else:
            continue  # tier rise: good news rides the summary line, no command
        seen.add(f["id"])
    return out


def cycle(project=None):
    """-> (proposals, window). proposals = [(area, what, verb-or-None)];
    window = {turns, non_silent, since} when the ledger has rows, else None."""
    out = []

    reg, report = registry.sync()
    if report["new"]:
        out.append(("registry", "%d new project%s discovered: %s"
                    % (len(report["new"]), "s"[:len(report["new"]) != 1],
                       ", ".join(report["new"])), None))

    plan = drain.classify()
    routable = [a for a in plan if a["op"] in ("retype", "route-project")]
    dups = [a for a in plan if a["op"] == "sweep-dup"]
    if routable:
        out.append(("drain", "%d raw entr%s routable to typed homes"
                    % (len(routable), "ies" if len(routable) != 1 else "y"),
                    "helm drain --apply"))
    if dups:
        out.append(("drain", "%d prem/prior twin duplicates sweepable (operator-gated)"
                    % len(dups), "helm drain --apply --sweep-dups"))

    found, _ = drift.findings(project=project, snapshot=False)
    if found:
        out.append(("drift", "%d belief%s drifting" % (len(found), "s"[:len(found) != 1]),
                    "helm drift" + _pflag(project)))

    rows = _ledger_rows()
    behavior = _drift_props(found, project) + _fire_props(rows, project) \
        + _reflex_props(rows, project)
    behavior.sort(key=lambda p: (-p[0], p[2]))
    out.extend(p[1:] for p in behavior[:BEHAVIOR_CAP])

    from . import whoami
    p = whoami.load_profile()
    if not (p["technical_level"] or p["guidance"] or whoami.load_notes()):
        out.append(("who", "the know-your-user leg is empty — the five-minute interview "
                    "makes every agent warmer", "helm interview"))

    window = {"turns": len(rows),
              "non_silent": sum(1 for r in rows if isinstance(r.get("fired"), dict)),
              "since": str(rows[0].get("ts") or "?")} if rows else None
    return out, window


def proposals(project=None):
    return cycle(project)[0]


def cmd_evolve(args):
    """evolve [--project P] — one observe/propose cycle. Proposes; edits no
    knowledge (the sync observer's registry write + scaffold is the one
    documented bookkeeping side effect — see the module docstring)."""
    project = None
    if "--project" in args:
        project = args[args.index("--project") + 1]
    props, window = cycle(project=project)
    tail = "  (over %d turns since %s; %d non-silent)" \
        % (window["turns"], window["since"], window["non_silent"]) if window else ""
    if not props:
        print("helm evolve: steady — nothing to propose." + tail)
        return 0
    print("helm evolve — %d proposal%s (no knowledge edited)%s:"
          % (len(props), "s"[:len(props) != 1], tail))
    for area, what, verb in props:
        print("  [%s] %s%s" % (area, what, ("  ->  " + verb) if verb else ""))
    return 0
