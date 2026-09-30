#!/usr/bin/env python3
"""helm eval board — the model scorecard (task/3448).

The owner: "maybe helm needs our own internal version on a page of the webui
that ranks the models we use as well, with a mix of quant and qualitative
factors. i think doing so officially if in a lightweight way would help us do
things like certify apprenticeships over and decide how to allocate lanes
across projects", and "we could even have it use outside model benchmarks to
initially organize it, and then let the rankings upgrade themselves over time
with our 'direct experience' factors".

SO EACH MODEL'S SCORE IS A PRIOR UPDATED BY OUR RECORD. The prior is one
public benchmark number per exact model version (`PRIORS`, data, each with its
source and date), measured under its MAKER's harness — never ours, so it is a
starting place and never evidence of how the model does here (premise
judge-a-model-under-its-makers-measured-conditions). Our record is how often a
lane the model authored LANDED WITHOUT A REVIEWER'S PATCH. The blend is a beta
update in which the prior is worth `PRIOR_LANES` lanes: it orders a new model
sensibly and fades as measured lanes accrue. Prior, record, n and a band are
shown side by side, because a large gap between the maker's number and ours is
a finding about our harness, scaffolding or briefs.

WHAT IT READS, READ-ONLY: the dispatch ledger (every build, review request,
verdict, findings note and close), and helm's trunk merge subjects ("merge lane
<name>") for lanes that landed without a ledger close. A lane is one dispatch
chain; its author is the seat a build was sent to, or failing a build, the
seat that asked for the first review. A seat's model is the one its own signed
verdicts recorded at the time, else the seat catalog's (INFERRED), and a seat
nothing names is never guessed into one: its lanes and reviews are one
footnote under the table (`unnamed`), never a ranked model.

WHAT IT CANNOT SAY IS None, AND READS UNKNOWN — never a zero. Request speed,
DEAF time and tokens per lane are not joined here yet (`UNMEASURED` says why);
the local seats' mentor measured the first two by hand in its grade cards.

THE RUNGS ARE DATA (`RUNGS`, `SEAT_RUNGS`, seeded from the mentor's grade
cards). The board shows each seat's rung and the evidence the
next one needs. It never promotes: only the owner admits.
"""
import calendar
import json
import math
import os
import re
import sys
import threading
import time

WINDOWS = {"7d": 7 * 86400, "30d": 30 * 86400}
DEFAULT_WINDOW = "30d"

# the prior is worth this many lanes; below FLOOR decided lanes a score is
# "prior only" — or, with no public prior either, NOT SCORED: no number is
# shown without evidence behind it (task/3448, the owner's finding: a model
# we knew nothing about read the fleet's pooled rate, 89 70–100)
PRIOR_LANES = 6
FLOOR = 3
NOT_SCORED = "not scored"
STATES = ("measured", "prior only", NOT_SCORED)

# how a chain ends: landed, or ended without landing (decided either way)
LANDED = ("landed", "source-clean-landed")
ENDED = ("withdrawn", "stranded", "expired")

# ── THE PRIOR: public benchmark numbers per exact model version ─────────────
# Keyed by the model id the fleet records (a verdict's resolved model, or the
# seat catalog's alias). The FIRST score is the one the blend starts from; the
# rest are shown beside it. Benchmarks differ between models, so priors from
# different benchmarks are not comparable with each other: they only order a
# model until our own record takes over. Each score carries its own date.
PRIORS = {
    "qwen27": {
        "exact": "Qwen3.8-27B (served int4)",
        "scores": [
            {"bench": "Terminal-Bench 2.1", "value": 73.0, "date": "2026-09",
             "harness": "maker (Qwen's model card)",
             "source": "https://huggingface.co/Qwen/Qwen3.8-27B"},
            {"bench": "SWE-bench Verified", "value": 86.0, "date": "2026-09",
             "harness": "third party (no maker figure)",
             "source": "https://kingy.ai/blog/qwen3-8-27b-specs-benchmarks-local-hardware/"},
        ],
        "departs": "we serve int4 weights with our own thinking and context "
                   "settings; the card measured full weights in the Claude Code "
                   "harness with thinking on",
    },
    "qwenlocal": {
        "exact": "Qwen3.6-35B-A3B",
        "scores": [
            {"bench": "SWE-bench Verified", "value": 73.4, "date": "2026-04-15",
             "harness": "maker (Qwen's release post)",
             "source": "https://qwen.ai/blog?id=qwen3.6-35b-a3b"},
            {"bench": "Terminal-Bench 2.0", "value": 51.5, "date": "2026-04-15",
             "harness": "maker (Qwen's release post)",
             "source": "https://qwen.ai/blog?id=qwen3.6-35b-a3b"},
        ],
        "departs": "we serve quantized weights through llama.cpp",
    },
    "bonsai": {
        "exact": "Ternary Bonsai 2 27B",
        "scores": [
            {"bench": "SWE-bench Verified", "value": 60.8, "date": "2026-09",
             "harness": "maker's evaluation of the ternary build, as reported",
             "source": "https://www.orcarouter.ai/blog/ternary-bonsai-2-27b"},
        ],
        "departs": "our own SYCL build of the ternary weights",
    },
    "kimi-k3": {
        "exact": "Kimi K3",
        "scores": [
            {"bench": "Terminal-Bench 2.1", "value": 88.3, "date": "2026-09",
             "harness": "maker (Moonshot, its KimiCode harness)",
             "source": "https://huggingface.co/moonshotai/Kimi-K3"},
            {"bench": "Terminal-Bench 2.1", "value": 85.0, "date": "2026-09",
             "harness": "third party (Artificial Analysis harness)",
             "source": "https://artificialanalysis.ai/evaluations/terminalbench-2-1"},
        ],
    },
    "deepseek-v4-pro": {
        "exact": "DeepSeek V4 Pro 0813",
        "scores": [
            {"bench": "Terminal-Bench 2.1", "value": 87.9, "date": "2026-08",
             "harness": "maker (DeepSeek's own harness)",
             "source": "https://www.mindstudio.ai/blog/deepseek-v4-pro-0813-benchmark-review"},
            {"bench": "SWE-bench Verified", "value": 80.6, "date": "2026-09",
             "harness": "maker (V4-Pro-Max)",
             "source": "https://www.morphllm.com/deepseek-v4"},
        ],
    },
    "deepseek-v4-flash": {
        "exact": "DeepSeek V4 Flash 0731",
        "scores": [
            {"bench": "Terminal-Bench 2.1", "value": 82.7, "date": "2026-08",
             "harness": "maker (DeepSeek's own harness)",
             "source": "https://www.mindstudio.ai/blog/deepseek-v4-pro-0813-benchmark-review"},
        ],
    },
    "gpt-6-astra": {
        "exact": "GPT-6 Astra",
        "scores": [
            {"bench": "Terminal-Bench 4.0", "value": 57.9, "date": "2026-09",
             "harness": "maker (OpenAI's launch post)",
             "source": "https://openai.com/index/gpt-6-astra/"},
        ],
    },
    "gpt-5.6-sol": {
        "exact": "GPT-5.6 Sol",
        "scores": [
            {"bench": "Terminal-Bench 4.0", "value": 37.3, "date": "2026-09",
             "harness": "maker (OpenAI's GPT-6 Astra launch post)",
             "source": "https://openai.com/index/gpt-6-astra/"},
            {"bench": "SWE-bench Pro", "value": 64.6, "date": "2026-09",
             "harness": "third-party leaderboard",
             "source": "https://www.morphllm.com/swe-bench-pro"},
        ],
    },
    "gemini-3.8-flash-high": {
        "exact": "Gemini 3.8 Flash",
        "scores": [
            {"bench": "Terminal-Bench 2.1", "value": 89.4, "date": "2026-09",
             "harness": "maker's reported number (others report 90.8)",
             "source": "https://emergent.sh/learn/gemini-3-8-flash-benchmarks"},
        ],
    },
    "claude-opus-4-6-thinking": {
        "exact": "Claude Opus 4.6",
        "scores": [
            {"bench": "SWE-bench Verified", "value": 80.8, "date": "2026-02-05",
             "harness": "maker (Anthropic's launch post)",
             "source": "https://www.anthropic.com/news/claude-opus-4-6"},
            {"bench": "Terminal-Bench 2.0", "value": 65.4, "date": "2026-02-05",
             "harness": "maker (Anthropic's launch post)",
             "source": "https://www.anthropic.com/news/claude-opus-4-6"},
        ],
        "departs": "we reach it through a proxy seat, not Anthropic's harness",
    },
    "gpt-oss-120b-medium": {
        "exact": "gpt-oss-120b",
        "scores": [
            {"bench": "SWE-bench Verified", "value": 62.4, "date": "2025-08-05",
             "harness": "maker (OpenAI's model card, high reasoning)",
             "source": "https://arxiv.org/abs/2508.10925"},
        ],
        "departs": "we run it at medium reasoning; the card's figure is at high",
    },
}
# the same model under another id the fleet records
PRIOR_ALIASES = {"deepseek/deepseek-v4-flash": "deepseek-v4-flash",
                 "ds4-pro": "deepseek-v4-pro"}

PRIOR_NOTE = ("each prior is one public benchmark measured under its maker's "
              "harness, not ours; priors from different benchmarks are not "
              "comparable with each other, and only order a model until our "
              "own lanes take over")

# ── THE APPRENTICESHIP RUNGS ────────────────────────────────────────────────
RUNGS = ("input", "non-door reviewer", "door reader on stated invariants",
         "admitted")
WHO_ADMITS = ("the owner: only he admits a seat to its next rung; this board "
              "shows the evidence and never promotes")
_CARDS = "the local seats' mentor's grade cards (journal: local-seat-grade-cards)"
# Each seat's rung and the evidence its next one needs, QUOTED from its grade
# card ("Rung: ... (proposed)" and its "To move to ..." sentence); `step` is
# where that rung sits on RUNGS, so a narrower rung ("non-door reviewer on
# briefed tables") keeps its own words and still has a next rung.
SEAT_RUNGS = {
    "qwen27": {
        "rung": "non-door reviewer", "step": "non-door reviewer",
        "by": "its mentor, proposed", "date": "2026-09-27",
        "next": "5 door reads briefed with the stated-invariant table plus the "
                "standing rows, each scored against the approval-tier read of "
                "the same tip, with every stated row judged right and no "
                "stated-row miss",
        "evidence": "5/5 on stated invariants, 0/6 unstated; the findings pass "
                    "said \"no findings\" on 25 of 28 tips the approval tier "
                    "returned FIX on, and agreed on 17 of 18 clean tips",
        "source": _CARDS},
    "qwenlocal": {
        "rung": "input", "step": "input",
        "by": "its mentor, proposed", "date": "2026-09-27",
        "next": "3 non-door reads against an answer key with every stated row "
                "judged right, no false findings, no rebuilding in place of "
                "reading, and each claimed fab log quoted",
        "evidence": "15 lanes, 13 needed a reviewer patch; as a door reader, "
                    "SOURCE-CLEAN on a tip where the approval tier found F1-F6",
        "source": _CARDS},
    "bonsai": {
        "rung": "non-door reviewer on briefed tables", "step": "non-door reviewer",
        "by": "its mentor, proposed", "date": "2026-09-27",
        "next": "5 briefed reads (non-door first) with EVERY asked row answered "
                "(no skipped row), no stated-row miss against the key, and "
                "numbers quoted from receipts, not memory",
        "evidence": "briefed with one evaluative row, 4/4 and 0 false; stated "
                    "6/6 plus 2 real extras, but skipped the one explicitly "
                    "asked standing row that held the known finding",
        "source": _CARDS},
}

# ── WHAT THIS BOARD DOES NOT MEASURE YET, and why ───────────────────────────
UNMEASURED = {
    "request_speed": "not joined yet: the per-seat proxy logs name no lane and "
                     "rotate; the mentor measured them by hand in the grade cards",
    "deaf_time": "not joined yet: the beacon census rides the chat log at a "
                 "5-minute grain and is an upper bound",
    "token_cost": "the ledger attributes no tokens to a lane, so cost per land "
                  "is counted in reviewer reads and patches, not tokens",
}

TIER_FALLBACK = ("anthropic", "codex", "ds4pro", "kimi", "grok")

# the model word for a seat nothing names (`_Who`): NOT A MODEL. Its lanes and
# reviews are one footnote under the table, never a ranked row (task/3448)
UNNAMED = "unknown"


def ledger_path():
    """The dispatch ledger's own path (`dispatches.ledger_path`)."""
    from . import dispatches
    return dispatches.ledger_path()


def _t(value):
    """An ISO `...Z` stamp -> epoch seconds, or None."""
    try:
        return calendar.timegm(time.strptime(str(value), "%Y-%m-%dT%H:%M:%SZ"))
    except (TypeError, ValueError):
        return None


def _stamp(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def _median(values):
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    mid = len(vals) // 2
    return vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2


def _lane_name(lane):
    s = str(lane or "")
    return s[len("lane/"):] if s.startswith("lane/") else s


def read_ledger(path):
    """(rows, skipped, why): every parseable line, READ-ONLY. A malformed line
    is skipped and counted; an unreadable file is rows None and a reason."""
    rows, skipped = [], 0
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    skipped += 1
                    continue
                if isinstance(row, dict):
                    rows.append(row)
                else:
                    skipped += 1
    except OSError as exc:
        return None, 0, "the dispatch ledger could not be read (%s)" % type(exc).__name__
    return rows, skipped, None


_MERGE = re.compile(r"\bmerge lane (\S+)")


def trunk_lanes(since, repo=None, ref="main"):
    """({lane name: [(merged tip, merge time)]}, why): each of helm's own
    trunk merges since `since` whose subject says "merge lane <name>", first
    parent only, with the tip it merged (its second parent; None for a merge
    commit with one parent). READ-ONLY."""
    repo = repo or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        from . import vcs
        rc, out, err = vcs.backend(repo).text(repo, "log", "--first-parent", ref,
                                             "--since=@%d" % int(since),
                                             "--format=%ct%x09%P%x09%s")
    except Exception as exc:                # noqa: BLE001 — named, never an empty map
        return None, "git log failed (%s)" % type(exc).__name__
    if rc != 0:
        return None, "git log %s failed (rc %s)" % (ref, rc)
    merges = {}
    for line in (out or "").splitlines():
        when, parents, subject = (line.split("\t", 2) + ["", ""])[:3]
        m = _MERGE.search(subject)
        if not m or not when.isdigit():
            continue
        tips = parents.split()
        merges.setdefault(_lane_name(m.group(1)).rstrip(":,;"), []).append(
            (tips[1] if len(tips) > 1 else None, int(when)))
    return merges, None


# ── WHO RAN WHAT ─────────────────────────────────────────────────────────────

def _family_word(fam):
    return "anthropic" if fam == "claude" else fam


def _catalog():
    try:
        from . import seat as seatmod
        return seatmod
    except Exception:                       # noqa: BLE001 — the catalog is optional here
        return None


class _Who:
    """Seat -> (family, model, basis) at a time: the verdicts the seat signed
    first, the seat catalog second (INFERRED), nothing third."""

    def __init__(self, rows, by_id):
        self.recorded = {}
        for r in rows:
            if r.get("event") != "verdict":
                continue
            seat = (by_id.get(r.get("id")) or {}).get("recipient")
            ev = r.get("verdict_author_runtime_evidence")
            res = ev.get("resolved") if isinstance(ev, dict) else None
            if not seat or not isinstance(res, dict) or not res.get("family"):
                continue
            fam = _family_word(res.get("family"))
            model = res.get("upstream_model" if res.get("backend") == "proxy" else "model")
            self.recorded.setdefault(seat, []).append(
                (_t(r.get("ts")) or 0, fam, model or ("claude" if fam == "anthropic" else fam)))
        for v in self.recorded.values():
            v.sort()
        self._cat = {}

    def _catalog_of(self, seat):
        if seat not in self._cat:
            fam = None
            mod = _catalog()
            if mod is not None:
                try:
                    fam, err = mod._seat_family(seat)
                    fam = None if err else fam
                except Exception:           # noqa: BLE001 — an unknown seat is unknown
                    fam = None
            if not fam and "claude" in str(seat):
                fam = "anthropic"
            model = None
            if fam == "anthropic":
                model = "claude"
            elif fam and mod is not None:
                model = (getattr(mod, "FAMILIES", {}).get(fam) or {}).get("model") or fam
            self._cat[seat] = (fam or "unknown", model or UNNAMED,
                               "catalog" if fam else "unknown")
        return self._cat[seat]

    def at(self, seat, when):
        rec = self.recorded.get(seat)
        if rec:
            before = [x for x in rec if x[0] <= when]
            _ts, fam, model = before[-1] if before else rec[0]
            return fam, model, "recorded"
        return self._catalog_of(seat)

    def family(self, seat, when=None):
        return self.at(seat, when if when is not None else float("inf"))[0]


# ── THE LANES ────────────────────────────────────────────────────────────────

def lanes(rows, since, trunk=None, who=None):
    """Every dispatch chain that STARTED since `since`, as a lane:
    {lane, author, start, end, rounds, fixes, patched, landed, decided,
    handback_s, reads {family: n}, patches {family: n}}. `trunk` is
    `trunk_lanes`' map of merges, or None.

    A LAND IS CREDITED ONCE, to the chain that made it (`_credit_lands`),
    never to every chain that shares a landed lane NAME."""
    disp = [r for r in rows if r.get("event") == "dispatch" and r.get("id")]
    by_id = {r["id"]: r for r in disp}
    who = who or _Who(rows, by_id)
    events = {}
    for r in rows:
        if r.get("event") != "dispatch":
            events.setdefault(r.get("id"), []).append(r)
    chains = {}
    for r in disp:
        chains.setdefault(r.get("chain_root") or r["id"], []).append(r)
    out = []
    for ds in chains.values():
        ds.sort(key=lambda r: _t(r.get("ts")) or 0)
        start = _t(ds[0].get("ts"))
        if start is None or start < since:
            continue
        builds = [d for d in ds if d.get("kind") == "build"]
        author = (builds[0].get("recipient") if builds else ds[0].get("sender")) or "unknown"
        asks = [d for d in ds if d.get("kind") != "build" and d.get("sender") == author]
        evs = [e for d in ds for e in events.get(d["id"], [])]
        verdicts = [e for e in evs if e.get("event") == "verdict"]
        closes = [e.get("close_reason") for e in evs if e.get("event") == "close"]
        name = _lane_name(ds[0].get("lane"))
        landed_at = [e.get("reviewed_tip") or (by_id.get(e.get("id")) or {}).get("tip")
                     for e in evs if e.get("event") == "close" and e.get("close_reason") in LANDED]
        landed = bool(landed_at)
        ended = not landed and any(c in ENDED for c in closes)
        reads, patches = {}, {}
        for v in verdicts:
            reader = (by_id.get(v.get("id")) or {}).get("recipient")
            when = _t(v.get("ts")) or start
            if reader and reader != author:
                fam = who.family(reader, when)
                reads[fam] = reads.get(fam, 0) + 1
            pa = v.get("patch_author")
            if v.get("patch_tip") and pa and pa != author:
                fam = who.family(pa, when)
                patches[fam] = patches.get(fam, 0) + 1
        handback = None
        if builds:
            b0 = _t(builds[0].get("ts"))
            back = [_t(d.get("ts")) for d in asks if (_t(d.get("ts")) or 0) >= b0]
            handback = back[0] - b0 if back else None
        end = max([_t(d.get("ts")) or start for d in ds]
                  + [_t(e.get("ts")) or start for e in evs])
        out.append({
            "lane": name, "author": author, "start": start, "end": end,
            "rounds": len(asks),
            "fixes": sum(1 for v in verdicts if v.get("polarity") == "fix"),
            "patched": bool(patches), "landed": landed, "decided": landed or ended,
            "handback_s": handback, "reads": reads, "patches": patches,
            "_built": bool(builds), "_closed_at": landed_at[-1] if landed_at else None,
            "_tips": {x for x in [d.get("tip") for d in ds]
                      + [e.get(k) for e in evs for k in ("reviewed_tip", "patch_tip", "tip")] if x}})
    return [{k: v for k, v in lane.items() if not k.startswith("_")}
            for lane in _credit_lands(out, trunk or {})]


def _credit_lands(out, trunk):
    """One land per landed tip, credited to ONE chain of the lane's name:

      - a chain whose close says LANDED lands at the tip it closed on; two
        chains closed LANDED on the same tip are one land, kept by the chain
        with the build (else the earlier), and the other chain's reads,
        patches and FIX verdicts count toward it — it is not a lane of its own;
      - a trunk merge lands the chain that carries the tip it merged;
      - a merge of a tip no chain carries (a rebased tip) lands the latest
        chain of that name that started before it, and only while no chain of
        that name has landed otherwise (INFERRED: it may be a second land whose
        tip moved, which this then leaves uncounted rather than counted twice).
    """
    by_name = {}
    for lane in out:
        by_name.setdefault(lane["lane"], []).append(lane)
    gone = set()
    for name, group in by_name.items():
        owners = {}
        for lane in sorted(group, key=lambda l: (not l["_built"], l["start"])):
            tip = lane["_closed_at"]
            if tip is None:
                continue
            if tip not in owners:
                owners[tip] = lane
                continue
            keep = owners[tip]
            _add(keep["reads"], lane["reads"])
            _add(keep["patches"], lane["patches"])
            keep["fixes"] += lane["fixes"]
            keep["patched"] = keep["patched"] or lane["patched"]
            keep["end"] = max(keep["end"], lane["end"])
            gone.add(id(lane))
        live = [l for l in group if id(l) not in gone]
        for tip, when in sorted(trunk.get(name) or (), key=lambda x: x[1]):
            if tip in owners:
                continue
            carriers = [l for l in live if tip and tip in l["_tips"]]
            if carriers:
                pick = min(carriers, key=lambda l: (not l["_built"], l["start"]))
            elif any(l["landed"] for l in live):
                continue
            else:
                before = [l for l in live if l["start"] <= when]
                if not before:
                    continue
                pick = max(before, key=lambda l: l["start"])
            pick["landed"] = pick["decided"] = True
            owners[tip] = pick
    return [lane for lane in out if id(lane) not in gone]


def reader_record(rows, since, who, tier):
    """({(seat, model): {pairs, agree, misses, false_alarms}}, {(seat, model):
    reads}) — each read a seat made (a verdict, or a complete findings note)
    set against the FIRST later read of the same tip by another seat on the
    approval tier. A clean read the tier then read FIX on is a miss; a defect
    it then approved is a false alarm. The second map counts every read, so a
    model that only reviews is still a model we use."""
    by_id = {r["id"]: r for r in rows if r.get("event") == "dispatch" and r.get("id")}
    reads = {}
    for r in rows:
        ev = r.get("event")
        if ev == "verdict":
            seat = (by_id.get(r.get("id")) or {}).get("recipient")
            pol = r.get("polarity")
            word = "defect" if pol == "fix" else "clean" if pol in ("approve", "concur") else None
        elif ev == "findings-note":
            seat = r.get("reader")
            word = None if r.get("outcome") != "complete" \
                else ("clean" if str(r.get("kept")) == "0" else "defect")
        else:
            continue
        when, tip = _t(r.get("ts")), r.get("reviewed_tip")
        if seat and tip and word and when is not None and when >= since:
            reads.setdefault(tip, []).append((when, seat, word))
    done = {}
    for rs in reads.values():
        for when, seat, _word in rs:
            key = (seat, who.at(seat, when)[1])
            done[key] = done.get(key, 0) + 1
    out = {}
    for rs in reads.values():
        rs.sort()
        # ONE PAIR PER SEAT PER TIP: a seat that read the tip again before the
        # tier did is judged on its last word, so pairs count distinct tips
        judged = {}
        for i, (when, seat, word) in enumerate(rs):
            later = next((x for x in rs[i + 1:] if x[1] != seat
                          and who.family(x[1], x[0]) in tier), None)
            if later is not None:
                judged[seat] = (when, word, later)
        for seat, (when, word, later) in judged.items():
            key = (seat, who.at(seat, when)[1])
            rec = out.setdefault(key, {"pairs": 0, "agree": 0, "misses": 0, "false_alarms": 0})
            rec["pairs"] += 1
            if word == later[2]:
                rec["agree"] += 1
            elif word == "clean":
                rec["misses"] += 1
            else:
                rec["false_alarms"] += 1
    return out, done


# ── THE BLEND ────────────────────────────────────────────────────────────────

def blend(p0, k, n, m=PRIOR_LANES):
    """A beta update: the prior p0 is worth m lanes, then k clean of n decided.
    -> {posterior, band: [lo, hi]} on 0..1, the band ~90% (normal approximation
    to the beta), clipped to 0..1."""
    a, b = k + p0 * m, (n - k) + (1 - p0) * m
    mean = a / (a + b)
    sd = (a * b / ((a + b) ** 2 * (a + b + 1))) ** 0.5
    return {"posterior": mean,
            "band": [max(0.0, mean - 1.645 * sd), min(1.0, mean + 1.645 * sd)]}


def _prior(model):
    entry = PRIORS.get(PRIOR_ALIASES.get(model, model))
    if not entry:
        return None
    head = entry["scores"][0]
    return dict(head, exact=entry["exact"], departs=entry.get("departs"),
                others=entry["scores"][1:])


def _add(total, part):
    for k, v in part.items():
        total[k] = total.get(k, 0) + v


def _round(x, nd=2):
    return None if x is None else round(x, nd)


def _whole(x):
    """x to a whole number, half up — THE ONE ROUNDING of every number the
    board shows, so the text and the page print it as it is and round
    nothing again (a half read 62 in one and 63 in the other)."""
    return None if x is None else int(math.floor(x + 0.5))


def _pct(x):
    """A 0..1 rate as a whole percent, rounded once (`_whole`)."""
    return None if x is None else _whole(x * 100)


def _ratio(k, n):
    """k per n at two decimals, or None with no n. A real count never reads
    0: a ratio two decimals would round to 0 keeps two significant digits
    (1 of 201 is 0.005)."""
    if not n:
        return None
    r = round(k / n, 2)
    return float("%.2g" % (k / n)) if k and not r else r


def board(rows, trunk, now=None, window_s=WINDOWS[DEFAULT_WINDOW], trunk_why=None,
          skipped=0, path=None):
    """The whole scorecard as a dict (see the module doc). `trunk` is
    `trunk_lanes`' map of merges, or None when it could not be read."""
    now = time.time() if now is None else now
    since = now - window_s
    by_id = {r["id"]: r for r in rows if r.get("event") == "dispatch" and r.get("id")}
    who = _Who(rows, by_id)
    try:
        from . import route
        tier = tuple(route.APPROVAL_TIER)
    except Exception:                       # noqa: BLE001 — the named tier stands in
        tier = TIER_FALLBACK
    ls = lanes(rows, since, trunk, who)
    reads, done = reader_record(rows, since, who, tier)
    per_model, per_seat = {}, {}
    unnamed = {"lanes": 0, "reviews": 0, "seats": set()}

    def slot(table, key):
        return table.setdefault(key, {"lanes": [], "reader": None, "reads": 0, "seats": set()})

    def tables(model, seat):
        # A MODEL THE LEDGER CANNOT NAME IS NOT A ROW: the seat keeps its own
        # entry, and the lane or review is counted once under the table
        if model == UNNAMED:
            unnamed["seats"].add(seat)
            return ((per_seat, seat),)
        return ((per_model, model), (per_seat, seat))

    for lane in ls:
        fam, model, basis = who.at(lane["author"], lane["end"])
        unnamed["lanes"] += model == UNNAMED
        for table, key in tables(model, lane["author"]):
            s = slot(table, key)
            s["lanes"].append(lane)
            s["seats"].add(lane["author"])
            s.setdefault("family", fam)
            s.setdefault("basis", basis)
            s.setdefault("model", model)
    for (seat, model), n in done.items():
        rec = reads.get((seat, model))
        unnamed["reviews"] += n if model == UNNAMED else 0
        for table, key in tables(model, seat):
            s = slot(table, key)
            s["seats"].add(seat)
            s["reads"] += n
            if rec:
                s["reader"] = s["reader"] or {"pairs": 0, "agree": 0, "misses": 0, "false_alarms": 0}
                _add(s["reader"], rec)
            fam, _m, basis = who.at(seat, now)
            s.setdefault("family", fam)
            s.setdefault("basis", basis)
            s.setdefault("model", model)
    # A SEAT NAMES THE MODEL IT RUNS NOW, its latest recorded one; each lane
    # and read stays credited to the model that ran it at the time
    for seat, s in per_seat.items():
        s["family"], s["model"], s["basis"] = who.at(seat, now)
    pooled_n = sum(1 for l in ls if l["decided"])
    pooled_k = sum(1 for l in ls if l["landed"] and not l["patched"])
    pooled = pooled_k / pooled_n if pooled_n else 0.5

    def measured(s):
        mine = s["lanes"]
        landed = [l for l in mine if l["landed"]]
        hb = _median(l["handback_s"] for l in mine)
        return {
            "lanes": len(mine),
            "decided": sum(1 for l in mine if l["decided"]),
            "landed": len(landed),
            "clean": sum(1 for l in landed if not l["patched"]),
            "patched": sum(1 for l in landed if l["patched"]) if landed else None,
            "rounds_median": _median(l["rounds"] for l in landed),
            # over DECIDED lanes: one in flight has not had all its reads
            "fix_per_lane_median": _median(l["fixes"] for l in mine if l["rounds"] and l["decided"]),
            "handback_median_min": _whole(None if hb is None else hb / 60.0),
            "reviews_done": s["reads"],
            "reader": s["reader"]}

    def cost(s):
        # COUNTS, and a ratio over them: a family's share of one read in 200
        # lands is a real read, never a rounded 0
        landed = [l for l in s["lanes"] if l["landed"]]
        if not landed:
            return {"lands": 0, "reads": None, "patches": None,
                    "reads_per_land": None, "patches_per_land": None, "by_family": None,
                    "patches_by_family": None, "basis": UNMEASURED["token_cost"]}
        reads_f, patch_f = {}, {}
        for l in landed:
            _add(reads_f, l["reads"])
            _add(patch_f, l["patches"])
        n, k, p = len(landed), sum(reads_f.values()), sum(patch_f.values())
        return {"lands": n, "reads": k, "patches": p,
                "reads_per_land": _ratio(k, n), "patches_per_land": _ratio(p, n),
                "by_family": dict(sorted(reads_f.items())),
                "patches_by_family": dict(sorted(patch_f.items())),
                "basis": "INFERRED: " + UNMEASURED["token_cost"]
                         + "; each is counted against the family that did it"}

    models, raw = [], {}
    for model, s in per_model.items():
        m = measured(s)
        prior = _prior(model)
        mine = m["clean"] / m["decided"] if m["decided"] else None
        enough = m["decided"] >= FLOOR
        state = "measured" if enough else "prior only" if prior else NOT_SCORED
        # NO NUMBER WITHOUT EVIDENCE: with neither a public prior nor FLOOR
        # decided lanes of ours there is nothing to blend, so no score, band
        # or rank — the measured cells stay
        p0 = prior["value"] / 100.0 if prior else pooled
        b = None if state == NOT_SCORED else blend(p0, m["clean"], m["decided"])
        raw[model] = b["posterior"] if b else 0
        models.append({
            "model": model, "family": s.get("family"), "basis": s.get("basis"),
            "seats": sorted(s["seats"]), "prior": prior, "measured": m, "cost": cost(s),
            "score": {
                "prior": _pct(p0) if b else None,
                "prior_basis": "benchmark" if prior else
                               "the fleet's pooled rate (no public prior)" if b else
                               "none: no public prior and under %d decided lanes of ours" % FLOOR,
                "ours": _pct(mine),
                "posterior": _pct(b["posterior"]) if b else None,
                "band": [_pct(b["band"][0]), _pct(b["band"][1])] if b else None,
                "n": m["decided"], "state": state,
                # AGAINST A MAKER'S NUMBER ONLY: the pooled rate is where a
                # model with no benchmark starts, never a benchmark
                "gap": _pct(mine - p0) if prior and enough and mine is not None else None}})
    # MEASURED FIRST, then prior only: a model with one lane would otherwise
    # rank on a prior it has not earned above models with hundreds. Then the
    # models not scored, unranked, the most lanes first
    models.sort(key=lambda x: (STATES.index(x["score"]["state"]), -raw[x["model"]],
                               -x["score"]["n"], -x["measured"]["lanes"], x["model"]))
    for i, x in enumerate(models, 1):
        x["rank"] = None if x["score"]["state"] == NOT_SCORED else i
    seats = []
    for seat, s in sorted(per_seat.items()):
        entry = {"seat": seat, "model": s.get("model"), "family": s.get("family"),
                 "basis": s.get("basis"), "measured": measured(s),
                 "rung": SEAT_RUNGS.get(seat, {}).get("rung")}
        seats.append(entry)
    seat_of = {x["seat"]: x for x in seats}
    rung_rows = []
    for seat, r in SEAT_RUNGS.items():
        at = RUNGS.index(r["step"])
        rung_rows.append(dict(r, seat=seat, next_rung=RUNGS[at + 1],
                              model=(seat_of.get(seat) or {}).get("model"),
                              reader=((seat_of.get(seat) or {}).get("measured") or {}).get("reader")))
    label = next((k for k, v in WINDOWS.items() if v == window_s), "%dd" % (window_s // 86400))
    return {
        "window": label, "since": _stamp(since), "generated_at": _stamp(now),
        "sources": {"ledger": {"path": path, "rows": len(rows), "skipped": skipped},
                    "trunk": {"ref": "main", "why": trunk_why,
                              "lanes": None if trunk is None else sum(len(v) for v in trunk.values())}},
        "lanes": len(ls), "decided": pooled_n, "prior_lanes": PRIOR_LANES, "floor": FLOOR,
        "prior_note": PRIOR_NOTE,
        "models": models, "seats": seats,
        "unnamed": dict(unnamed, seats=sorted(unnamed["seats"])),
        "rungs": {"ladder": list(RUNGS), "who_admits": WHO_ADMITS, "seats": rung_rows},
        "unknown": dict(UNMEASURED)}


# ── THE ONE READ BOTH DOORS USE ──────────────────────────────────────────────
_CACHE = {}
_CACHE_LOCK = threading.Lock()
CACHE_S = 60


def board_now(window=DEFAULT_WINDOW):
    """The scorecard over the live ledger and trunk: the CLI's read and the
    page's (/api/models), memoised for CACHE_S so a page open does not re-read
    the ledger per request."""
    now = time.time()
    with _CACHE_LOCK:
        hit = _CACHE.get(window)
        if hit and now - hit[0] < CACHE_S:
            return hit[1]
    window_s = WINDOWS[window]
    path = ledger_path()
    rows, skipped, why = read_ledger(path)
    if rows is None:
        return {"unavailable": why, "window": window}
    trunk, trunk_why = trunk_lanes(now - window_s)
    got = board(rows, trunk, now=now, window_s=window_s, trunk_why=trunk_why,
                skipped=skipped, path=path)
    with _CACHE_LOCK:
        _CACHE[window] = (now, got)
    return got


# ── TEXT ─────────────────────────────────────────────────────────────────────

def _u(value, fmt="%s"):
    return "UNKNOWN" if value is None else fmt % value


def _dur(m):
    """Whole minutes as the page prints them (`modelsDur`): no rounding."""
    if m is None:
        return "UNKNOWN"
    return "%dm" % m if m < 120 else "%dh%02dm" % divmod(m, 60)


def unnamed_line(u):
    """The one footnote for lanes and reviews whose model the ledger cannot
    name ("1 lane carries no model on the ledger (seat seat-c)"), or None."""
    parts = ["%d %s%s" % (n, word, "" if n == 1 else "s")
             for n, word in ((u["lanes"], "lane"), (u["reviews"], "review")) if n]
    if not parts:
        return None
    one = len(parts) == 1 and u["lanes"] + u["reviews"] == 1
    return "%s %s no model on the ledger (%s %s)" % (
        " and ".join(parts), "carries" if one else "carry",
        "seat" if len(u["seats"]) == 1 else "seats", ", ".join(u["seats"]))


def board_lines(b):
    if b.get("unavailable"):
        return ["helm eval board: UNKNOWN — " + b["unavailable"]]
    src = b["sources"]
    trunk = src["trunk"]
    out = ["helm eval board — the model scorecard, window %s (since %s)" % (b["window"], b["since"]),
           "  %d lanes from the dispatch ledger (%d rows%s), %d decided; %s" % (
               b["lanes"], src["ledger"]["rows"],
               ", %d unreadable skipped" % src["ledger"]["skipped"] if src["ledger"]["skipped"] else "",
               b["decided"],
               "%d lanes merged on trunk" % trunk["lanes"] if trunk["lanes"] is not None
               else "trunk UNKNOWN: " + str(trunk["why"])),
           "  score = lanes landed without a reviewer's patch, blended with a public "
           "benchmark PRIOR worth %d lanes; under %d lanes it is prior only, and with no "
           "public prior either it is not scored" % (b["prior_lanes"], b["floor"]),
           "  " + b["prior_note"], ""]
    head = ("rank", "model", "score", "band", "decided", "prior", "ours", "landed", "clean",
            "rounds", "fix/lane", "reviews/land", "patches/land", "hand-back", "tips agree/miss")
    rows = []
    for m in b["models"]:
        s, x, c, p = m["score"], m["measured"], m["cost"], m["prior"]
        r = x["reader"]
        rows.append((
            "-" if m["rank"] is None else str(m["rank"]), m["model"],
            NOT_SCORED if s["posterior"] is None else str(s["posterior"]),
            "%d-%d" % tuple(s["band"]) if s["band"] else "",
            str(s["n"]) + (" prior only" if s["state"] == "prior only" else ""),
            ("%g %s" % (p["value"], p["bench"])) if p else
            "no prior (%d pooled)" % s["prior"] if s["prior"] is not None else "no prior",
            _u(s["ours"]), str(x["landed"]), str(x["clean"]),
            _u(x["rounds_median"], "%g"), _u(x["fix_per_lane_median"], "%g"),
            _u(c["reads_per_land"], "%g"), _u(c["patches_per_land"], "%g"),
            _dur(x["handback_median_min"]),
            "%d/%d of %d" % (r["agree"], r["misses"], r["pairs"]) if r else "UNKNOWN"))
    widths = [max(len(h), *(len(row[i]) for row in rows)) if rows else len(h)
              for i, h in enumerate(head)]
    fmt = "  ".join("%%-%ds" % w for w in widths)
    out.append(fmt % head)
    out.extend(fmt % row for row in rows)
    foot = unnamed_line(b.get("unnamed") or {"lanes": 0, "reviews": 0, "seats": []})
    if foot:
        out.append("  " + foot)
    gaps = [m for m in b["models"] if m["score"]["gap"] is not None and abs(m["score"]["gap"]) >= 15]
    if gaps:
        out.append("")
        out.append("gaps between the maker's number and ours (a finding about our setup until a rerun says otherwise):")
        out.extend("  %s: prior %d, ours %d (%+d)" % (m["model"], m["score"]["prior"],
                                                      m["score"]["ours"], m["score"]["gap"])
                   for m in gaps)
    out.append("")
    out.append("apprenticeship rungs (%s):" % b["rungs"]["who_admits"])
    for r in b["rungs"]["seats"]:
        rd = r.get("reader")
        out.append("  %s — %s (%s %s); next: %s" % (r["seat"], r["rung"], r["by"], r["date"], r["next_rung"]))
        out.append("      needs: " + r["next"])
        out.append("      so far: " + r["evidence"] + ("; on the ledger: %d of %d tips agreed with the tier, %d missed"
                                                        % (rd["agree"], rd["pairs"], rd["misses"]) if rd
                                                        else "; on the ledger: no read the tier re-read (UNKNOWN)"))
    out.append("")
    out.append("not measured (UNKNOWN):")
    out.extend("  %s: %s" % (k.replace("_", " "), v) for k, v in sorted(b["unknown"].items()))
    return out


_USAGE = """usage: helm eval board [--json] [--window 7d|30d]

  The model scorecard (task/3448), READ-ONLY over the dispatch ledger and
  helm's trunk. Per model (and per seat): lanes, lands, rounds and FIX
  verdicts per lane, reviewer patches, time to hand back, reader agreement
  with the later approval-tier read, reviews and patches per land — each with
  its n, and UNKNOWN where the ledger cannot say. The score blends a public
  benchmark PRIOR (the maker's harness, not ours) with our record; a model
  with neither a prior nor 3 decided lanes of ours is not scored. The
  apprenticeship rungs are shown as evidence; only the owner admits.
  The web console's Fleet › models page reads the same board (/api/models).
"""


def cmd(args):
    args = list(args or [])
    if any(a in ("-h", "--help", "help") for a in args):
        print(_USAGE)
        return 0
    from .cli import guard_tail
    rc = guard_tail("helm eval board", args, flags=("--json",), valued=("--window",),
                    usage=_USAGE)
    if rc is not None:
        return rc
    window = args[args.index("--window") + 1] if "--window" in args \
        and args.index("--window") + 1 < len(args) else DEFAULT_WINDOW
    if window not in WINDOWS:
        print("helm eval board: --window is one of %s" % ", ".join(sorted(WINDOWS)),
              file=sys.stderr)
        return 2
    b = board_now(window)
    if "--json" in args:
        print(json.dumps(b, indent=2, sort_keys=True))
    else:
        for line in board_lines(b):
            print(line)
    return 1 if b.get("unavailable") else 0
