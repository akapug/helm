"""helm inject — the ledger cluster: hook-JSON parsing, cwd->project scoping,
the fire-ledger append/read primitives, the per-session cooldown seen-state,
and the lane-split cohort report (lane_report/--lane-report).

Moved verbatim from the pre-split helm/inject.py. _cohort lives HERE (its
only consumer is lane_report) so the cluster graph stays one-way.
"""
import json
import os
import time

from .. import home, pk
from ._common import COOLDOWN_TURNS, LEDGER_MAX, SEEN_TTL, WHO_ID
from ._entries import _entry_line, _who_lines, load_entries


def parse_hook_json(raw):
    """Claude Code UserPromptSubmit hook JSON -> (prompt, cwd, session). Unknown
    keys are tolerated (the hook payload grows); missing keys read as empty.
    Malformed/non-object input -> (None, None, None): the caller must FAIL OPEN
    (inject nothing, rc 0) — a garbled payload never blocks a turn."""
    try:
        d = json.loads(raw)
    except Exception:
        return None, None, None
    if not isinstance(d, dict):
        return None, None, None
    return (str(d.get("prompt") or ""),
            str(d.get("cwd") or "") or None,
            str(d.get("session_id") or "") or None)


def project_for_cwd(cwd):
    """cwd -> registry project name by LONGEST-prefix match over project paths
    (drain's longest-first law: the deepest registered path that contains cwd
    wins). None = no project claims it — the caller stays global-only.
    Read-only registry access; fail-open (any trouble -> None)."""
    if not cwd:
        return None
    from .. import registry
    try:
        want = os.path.abspath(os.path.expanduser(str(cwd)))
        best = None
        for key, rec in (registry.load().get("projects") or {}).items():
            # the FULL cwd lens, not just the canonical path — the registry
            # records sibling-worktree and alternate cwds in cv_scope
            # (codex-seat review: a hook fired in a worktree fell back to
            # global-only, silently dropping project premises + reflexes)
            prefixes = (rec.get("cv_scope") or {}).get("cwd_prefixes") \
                or [rec.get("path")]
            for pre in prefixes:
                pre = str(pre or "").rstrip("/")
                if pre and (want == pre or want.startswith(pre + "/")) \
                        and len(pre) > len(best[0] if best else ""):
                    best = (pre, str(rec.get("name") or key))
        return best[1] if best else None
    except Exception:
        return None


def _ledger_path():
    return os.path.join(home.global_dir(), ".state", "inject-ledger.jsonl")


def _append_jsonl(path, row, max_bytes):
    """ONE appended JSON line — the measurement-spine primitive (fire-ledger +
    compare-ledger). HARD LAWS: O(1) (one stat + one append, never a read), IDS
    never prompt text, one-generation rotation at max_bytes (-> .1), and
    FAIL-OPEN — a ledger that cannot be written must never block or slow the hook."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        try:
            if os.path.getsize(path) > max_bytes:
                os.replace(path, path + ".1")
        except OSError:
            pass  # no ledger yet
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _ledger_append(row):
    """The per-inject fire-ledger row — one line to inject-ledger.jsonl."""
    _append_jsonl(_ledger_path(), row, LEDGER_MAX)


# ---------------------------------------------------------------------------
# session cooldown — the habituation guard extended to the JIT lane
# ---------------------------------------------------------------------------

def _seen_dir():
    return os.path.join(home.global_dir(), ".state", "inject-seen")


def _seen_path(session):
    return os.path.join(_seen_dir(), pk.slug(str(session)) + ".json")


def _seen_load(session):
    """Per-session suppression state {turn: N, fired: {id: [turn, score]}}.
    Fail-open: an absent/torn/alien-shaped file reads as a FRESH session (no
    cooldown) — seen-state trouble must never block or crash the hook."""
    d = pk.read_json(_seen_path(session))
    fresh = {"turn": 0, "fired": {}}
    if not isinstance(d, dict) or not isinstance(d.get("turn"), int) \
            or d["turn"] < 0 or not isinstance(d.get("fired"), dict):
        return fresh
    fired = {str(i): r for i, r in d["fired"].items()
             if isinstance(r, list) and len(r) == 2
             and isinstance(r[0], int) and isinstance(r[1], (int, float))}
    return {"turn": d["turn"], "fired": fired}


def _seen_save(session, state):
    """Atomic write of one session's seen-state; records past the cooldown
    window are dropped (the file stays tiny) and stale SIBLING session files
    (mtime beyond SEEN_TTL) are pruned opportunistically. Entirely fail-open."""
    try:
        turn = state["turn"]
        state = {"v": 1, "ts": pk.now_ts(), "turn": turn,
                 "fired": {i: r for i, r in state["fired"].items()
                           if turn - r[0] <= COOLDOWN_TURNS}}
        pk.write_json(_seen_path(session), state)
        now = time.time()
        d = _seen_dir()
        for n in os.listdir(d):
            p = os.path.join(d, n)
            try:
                if n.endswith(".json") and now - os.path.getmtime(p) > SEEN_TTL:
                    os.remove(p)
            except OSError:
                pass
    except Exception:
        pass


# ---------------------------------------------------------------------------
# cohort analyzer — the read-only fire-ledger cohort report (--lane-report)
# ---------------------------------------------------------------------------

def _cohort(e):
    """The facts-vs-judgment cohort razor:
    'facts' = knowledge that makes a capable model fluent — lexicon terms,
    certain priors (premises / decisions-of-record), references; 'judgment' =
    steering that could anchor it — heuristic moves, sub-certain belief
    priors. None = not cohortable (reflex machinery, unknown ids)."""
    t = e.get("type")
    if t in ("lexicon", "reference", "capability"):
        # a wired capability is substrate TRUTH the agent should be fluent in
        return "facts"
    if t == "prior":
        return "facts" if e.get("class") == "certain" else "judgment"
    if t == "heuristic":
        return "judgment"
    return None


def _read_jsonl(path):
    """Every row of a rotated jsonl (path + its .1 generation) oldest-first.
    Read-only; torn lines skipped; any file trouble reads as fewer rows."""
    rows = []
    for p in (path + ".1", path):
        try:
            with open(p, encoding="utf-8") as f:
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


def _ledger_rows():
    """Every fire-ledger row oldest-first, rotated generation (.1) included."""
    return _read_jsonl(_ledger_path())


def lane_report(project=None):
    """The lane-split eval's accumulating instrument, READ-ONLY: every fired
    id in the ledger classified via _cohort against the CURRENT store (the
    operator digest who:operator = facts/profile), with per-cohort fires,
    distinct ids, byte estimate (today's rendering x fires — per-entry bytes
    are not ledgered), session spread, cooldown suppression, and the
    silent-rate first-half vs second-half trend. DELIVERY ONLY: the ledger
    logs fires, not heeds — anchoring is NOT measurable here; the
    outcome-marker protocol is measured out-of-band."""
    rows = _ledger_rows()
    try:
        by_id = {str(e["id"]): e for e in load_entries(project)}
    except Exception:
        by_id = {}
    who_bytes = sum(len(l) for l in _who_lines())

    def cohort(i):
        if i == WHO_ID:
            return "facts"  # the operator profile
        e = by_id.get(i)
        return (e and _cohort(e)) or "other"

    c = {k: {"fires": 0, "ids": set(), "bytes": 0, "sessions": set(),
             "suppressed": 0} for k in ("facts", "judgment", "other")}
    fired_rows = silent = 0
    sessions = set()
    halves = [[0, 0], [0, 0]]  # [silent, rows] per ledger half
    for n, r in enumerate(rows):
        half = halves[n * 2 // len(rows)]
        half[1] += 1
        s = r.get("session")
        if s:
            sessions.add(s)
        if r.get("silent"):
            silent += 1
            half[0] += 1
        else:
            fired_rows += 1
        for ids in (r.get("fired") or {}).values():
            for i in map(str, ids):
                d = c[cohort(i)]
                d["fires"] += 1
                d["ids"].add(i)
                e = by_id.get(i)
                d["bytes"] += len(_entry_line(e)) if e else \
                    (who_bytes if i == WHO_ID else 0)
                if s:
                    d["sessions"].add(s)
        for i in map(str, r.get("suppressed") or ()):
            c[cohort(i)]["suppressed"] += 1
    return {"rows": len(rows), "fired_rows": fired_rows, "silent": silent,
            "sessions": len(sessions), "halves": halves,
            "cohorts": {k: {"fires": d["fires"], "ids": len(d["ids"]),
                            "bytes": d["bytes"],
                            "sessions": len(d["sessions"]),
                            "suppressed": d["suppressed"]}
                        for k, d in c.items()}}


def _pct(num, den):
    return 100.0 * num / den if den else 0.0


def _lane_report(project=None):
    """--lane-report: lane_report() rendered as the cohort table. No ledger
    row, no state mutation — an analyzer must never count as a turn."""
    r = lane_report(project)
    if not r["rows"]:
        print("lane-report: no ledger rows yet — the instrument is unfired.")
        return 0
    print("lane-split cohorts — %d rows (%d fired, %d silent), %d sessions" % (
        r["rows"], r["fired_rows"], r["silent"], r["sessions"]))
    total_f = sum(d["fires"] for d in r["cohorts"].values())
    total_b = sum(d["bytes"] for d in r["cohorts"].values())
    fmt = "%-9s %6s %6s %5s %8s %6s %5s %5s %6s"
    print(fmt % ("cohort", "fires", "share", "ids", "bytes~", "share",
                 "sess", "supp", "supp%"))
    for k in ("facts", "judgment", "other"):
        d = r["cohorts"][k]
        print("%-9s %6d %5.1f%% %5d %8d %5.1f%% %5d %5d %5.1f%%" % (
            k, d["fires"], _pct(d["fires"], total_f), d["ids"], d["bytes"],
            _pct(d["bytes"], total_b), d["sessions"], d["suppressed"],
            _pct(d["suppressed"], d["fires"] + d["suppressed"])))
    h1, h2 = r["halves"]
    print("silent rate: %.1f%% overall | first half %.1f%% -> second half %.1f%%" % (
        _pct(r["silent"], r["rows"]), _pct(h1[0], h1[1]), _pct(h2[0], h2[1])))
    print("bytes~ = today's rendering x fires (per-entry bytes are not ledgered).")
    print("DELIVERY ONLY: fires are not heeds — the anchoring verdict needs the")
    print("outcome markers, measured out-of-band.")
    return 0
