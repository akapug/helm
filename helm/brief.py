#!/usr/bin/env python3
"""helm brief — the operator's morning brief, composed from what the estate
already knows. READ-ONLY everywhere and NETWORK-NEVER: catalog rows, the typed
store, the inject ledger, drain receipts, the creds probe cycle's cached
observations, doctor-style owner gates. A brief must cost nothing to ask for —
no probe, no mutation, no LLM.

Sections (headline-first; an empty section is omitted entirely):
  SINCE YOU LEFT  — sessions active in the window, bucketed by project
  WHAT WE BUILT   — the gauge: trunk lands from git (first-parent of the
                    landedness ref, last 48h, grouped by day) + the land-loop
                    board counts. NEVER omitted — tri-state instead: an
                    unreadable repo or an unavailable board is said out loud,
                    because a gauge that goes quiet when the instrument breaks
                    reads as "nothing shipped"
  KNOWLEDGE DELTA — store entries added/updated/retired + drain receipts,
                    plus inject-ledger turn stats (top-firing, silent-rate)
  STORE REVIEW QUEUE — provisional entries (xrev-cleared, FIRING but awaiting
                    the owner's ratify) newest-first + the raw-candidate count
                    (the owner steer: the provisional queue comes to the owner)
  SEATS           — freshest cached quota observation per account
                    (no cache -> "quota: run `helm creds`", never a probe)
  WAITING ON YOU  — owner-gated items the estate already records
"""
import calendar
import glob
import json
import os
import sys
import time

from . import home, pk

# The creds probe cycle's observation log (providers.NativeQuotaProvider
# appends here). Reading it IS the cheapest quota read there is.
USAGE_HISTORY = os.path.join(os.path.expanduser("~"), ".cache", "helm",
                             "native-usage-history.jsonl")

TOP_FIRING = 3   # inject entries named in the ledger line
MAX_PROJECTS = 6  # session buckets shown (the ~40-line render cap)
MAX_SEATS = 6
MAX_WAITING = 6
MAX_REVIEW = 6   # provisional entries listed before the "+N more" fold
BUILT_HOURS = 48.0  # the gauge window — fixed, NOT the brief's --hours: the
                    # owner compares mornings, and a window that moves with the
                    # flag makes two briefs incomparable
MAX_BUILT = 12   # land lines before the "+N more" fold (~15 with day heads)


def _ts_epoch(s):
    """Store/ledger timestamp -> utc epoch: '...T..:..:..Z', tz-less ISO, or
    date-only ('2026-06-01' stated_ts rows are live in the adopted store).
    Fail-open: unparseable -> 0 (never in any window)."""
    s = str(s or "").strip()
    for fmt, n in (("%Y-%m-%dT%H:%M:%S", 19), ("%Y-%m-%d", 10)):
        try:
            return calendar.timegm(time.strptime(s[:n], fmt))
        except ValueError:
            pass
    return 0


# ------------------------------------------------------------- since you left

def _sessions_delta(cutoff):
    """Catalog rows (read-only, single-flight cached) touched in the window,
    bucketed by registry project; synthetic (pruned-copy) rows excluded."""
    from . import sessions
    buckets = {}
    total = 0
    for r in sessions.rows_for():
        if (r.get("mt") or 0) < cutoff:
            continue
        total += 1
        b = buckets.setdefault(r.get("project") or "-", {
            "name": r.get("project") or "-", "n": 0, "latest_mt": 0, "latest_title": ""})
        b["n"] += 1
        if (r.get("mt") or 0) >= b["latest_mt"]:
            b["latest_mt"] = r.get("mt") or 0
            b["latest_title"] = r.get("t") or ""
    projects = sorted(buckets.values(), key=lambda b: (-b["n"], -b["latest_mt"]))
    return {"total": total, "projects": projects}


# --------------------------------------------------------------- what we built

def _built(repo=None):
    """The gauge: what actually reached trunk in the last BUILT_HOURS, read
    straight from git — every first-parent commit of the landedness ref IS one
    land (a merge or a direct trunk commit), so the subjects are the fleet's
    own outcome lines — plus the land-loop counts `landreq.board_section`
    already projects (reused, never re-derived). Still NETWORK-NEVER: the
    remote-tracking ref is a local snapshot and `git log` never fetches.

    TRI-STATE ON BOTH LEGS, and the section is never omitted: a repo git
    cannot read and a board the projection refuses are each SAID, because this
    gauge going silent is indistinguishable from "nothing shipped" — the one
    lie it exists to prevent. An empty window is its own honest state."""
    from . import landreq, vcs
    repo = repo or os.getcwd()
    v = vcs.backend(repo)
    ref = v.trunk_ref(repo)  # origin/<base> when a remote publishes one
    # LOCAL FALLBACK IS NOT LANDEDNESS. trunk_ref degrades to the local
    # branch when no remote exists, and a local merge is exactly the claim
    # this gauge must never launder into a "land" (a cross-family review
    # finding — the landed-means-origin-main class). Ask git, never parse the
    # name.
    rp_rc, _, _ = v.text(repo, "rev-parse", "--verify", "--quiet",
                         "refs/remotes/" + ref)
    published = rp_rc == 0
    since = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                          time.gmtime(time.time() - BUILT_HOURS * 3600))
    rc, out, err = v.text(repo, "log", ref, "--first-parent",
                          "--since=" + since, "--date=format:%Y-%m-%d",
                          "--pretty=%h%x09%cd%x09%s")
    days, total, git_error = [], 0, None
    if rc != 0:
        git_error = ((err or "git log failed").splitlines() or ["?"])[0][:120]
    else:
        for line in out.splitlines():
            sha, _, rest = line.partition("\t")
            day, _, subject = rest.partition("\t")
            if not (sha and day):
                continue
            if not days or days[-1]["day"] != day:
                days.append({"day": day, "lands": []})
            days[-1]["lands"].append({"sha": sha, "subject": subject})
            total += 1
    try:
        b = landreq.board_section()
    except Exception as exc:  # noqa: BLE001 — the brief never dies on the board
        b = {"unavailable": "board projection raised: %s" % exc,
             "loops": [], "stalled": []}
    return {"repo": repo, "ref": ref, "published": published,
            "window_h": BUILT_HOURS,
            "git_error": git_error, "days": days, "total": total,
            "board": {"loops": len(b["loops"]), "stalled": len(b["stalled"]),
                      "unavailable": b["unavailable"]}}


# ------------------------------------------------------------ knowledge delta

def _store_entries():
    """Estate-wide store view, ALL statuses: the global root set plus each
    registry project's own root, deduped by path (a project entry shadowing a
    global one still counts once)."""
    from . import registry, store
    by_path = {}
    for root, scope, d in store.roots():
        by_path.update({e["path"]: e for e in store._load_root(root, scope, d).values()})
    for name in sorted(registry.load().get("projects") or {}):
        d = home.project_dir(name)
        by_path.update({e["path"]: e
                        for e in store._load_root("project", "project:" + name, d).values()})
    return list(by_path.values())


def _knowledge_delta(cutoff):
    """Typed-store movement in the window: retired outranks added outranks
    updated per entry (one entry, one bucket), plus drain-receipt action counts
    (the receipts ARE the drained ledger — nothing re-derived)."""
    from . import store
    added, updated, retired = [], [], []
    for e in _store_entries():
        if e.get("type") == "episodic":
            continue  # bulk memory: no authored timestamps, pure noise here
        rid = str(e.get("id") or e.get("term") or "")
        rt = _ts_epoch(e.get("retired_ts"))
        st = _ts_epoch(e.get("stated_ts"))
        lu = _ts_epoch(e.get("last_updated") or e.get("updated_ts"))
        if rt >= cutoff and rt and e.get("status") != store.STATUS_LIVE:
            retired.append(rid)
        elif st and st >= cutoff:
            added.append(rid)
        elif lu and lu >= cutoff:
            updated.append(rid)
    drained = 0
    for rp in sorted(glob.glob(os.path.join(store.adopted_dir(),
                                            "archive", "drain-*", "RECEIPT.json"))):
        r = pk.read_json(rp) or {}
        if _ts_epoch(r.get("ts")) >= cutoff:
            drained += int(r.get("applied") or 0)
    return {"added": sorted(added), "updated": sorted(updated),
            "retired": sorted(retired), "drained": drained}


def _inject_stats(cutoff):
    """Inject-ledger stats over the window (main + one rotated generation).
    None when no ledger exists (an older estate — the section just stays out);
    rows are v:1 shape but every field read is optional (fail-open per line)."""
    from . import inject
    path = inject._ledger_path()
    if not (os.path.exists(path) or os.path.exists(path + ".1")):
        return None
    turns = silent = 0
    fired = {}
    for p in (path + ".1", path):
        try:
            fh = open(p, encoding="utf-8", errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(row, dict) or _ts_epoch(row.get("ts")) < cutoff:
                    continue
                turns += 1
                if row.get("silent"):
                    silent += 1
                    continue
                for ids in (row.get("fired") or {}).values():
                    for i in ids or ():
                        fired[str(i)] = fired.get(str(i), 0) + 1
    top = sorted(fired.items(), key=lambda kv: (-kv[1], kv[0]))[:TOP_FIRING]
    starved, always_n = [], 0
    cannot, never = [], []
    try:
        # pinned starvation, surfaced where the owner actually looks.
        #
        # STARVED IS A PRESENT-TENSE FACT, NOT A HISTORICAL ZERO. This asked
        # `made[id] == 0` — never fired in the whole ledger window — and that
        # predicate cannot see the case it exists for. MEASURED on a live
        # store: 5 always-entries, budget 1200 fully consumed by the
        # first 3 (each capped to exactly LINE_CAP=400, so 3 x 400 == 1200 and
        # a 4th can NEVER fit). Two entries could not fire on any turn, three
        # more had fired 118-121 times in 8345 rows (1.4%) — and `starved` was
        # EMPTY, because none had a lifetime zero. The surface built to catch
        # starvation reported healthy while 2 of 5 owner rules were structurally
        # dead, including the two about how to sequence work.
        #
        # A RATE THRESHOLD would be the obvious fix and is the wrong one: an
        # entry pinned an hour ago legitimately has a near-zero count, so any
        # rate predicate either flags every new entry or needs an age carve-out
        # that re-creates the same blind spot at a different boundary.
        #
        # `fits` is already the deterministic answer. pinned_stats runs inject's
        # own greedy budget walk, so an always-entry ABSENT FROM `fits` cannot
        # fire on the NEXT turn — no window, no rate, no new-entry false
        # positive. That is exactly the question the owner is asking when they
        # pin something: will this reach me? Lifetime-zero stays in the report
        # as a weaker second signal for entries that fit today but never landed.
        # The two cases are kept APART because the owner's action differs: an
        # entry that cannot fit needs something ahead of it shortened, while
        # one that fits but has never landed is usually just newly pinned.
        # Folding them into one number would tell the owner a rule is broken
        # when it is merely new, which is how a real signal gets ignored.
        from . import store
        s = store.pinned_stats()
        if s["rows"] or s["always"]:
            always_n = len(s["always"])
            cannot = sorted(str(e["id"]) for e in s["always"]
                            if str(e["id"]) not in s["fits"])
            never = sorted(i for i, c in s["made"].items()
                           if c == 0 and i in s["fits"]) if s["rows"] else []
            starved = cannot + [i for i in never if i not in cannot]
    except Exception:
        pass  # fail-open: the brief never dies on a stats walk
    return {"turns": turns, "silent": silent,
            "silent_rate": round(silent / turns, 2) if turns else 0.0,
            "top": top, "starved": starved, "always_n": always_n,
            "cannot_fire": cannot, "never_fired": never}


# --------------------------------------------------------- store review queue

def _review_queue():
    """The store's owner-review backlog, estate-wide (the same entry set as the
    knowledge delta — never a project lens): provisional entries (xrev-cleared,
    FIRING but awaiting the owner's ratify) newest-first, plus the raw-candidate
    count. By owner directive: this queue must ROUTINELY reach the owner,
    so it rides the brief he already reads."""
    from . import store
    prov, cand = [], 0
    for e in _store_entries():
        st = e.get("status")
        if st == store.STATUS_PROVISIONAL:
            prov.append(e)
        elif st == store.STATUS_CANDIDATE:
            cand += 1
    prov.sort(key=store._recency, reverse=True)  # freshest graduation first
    return {"provisional": prov, "candidate": cand}


# --------------------------------------------------------------- seat reality

def _seats():
    """Freshest cached observation per account from the probe cycle's history
    log. NEVER probes: no cache -> no rows (render says run `helm creds`)."""
    best = {}
    try:
        fh = open(USAGE_HISTORY, encoding="utf-8", errors="replace")
    except OSError:
        return []
    with fh:
        for line in fh:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            a = r.get("account") if isinstance(r, dict) else None
            if not a or not r.get("gauges"):
                continue
            # key by (provider, account) — one email can seat BOTH an
            # anthropic and a codex identity; account-only keying let the
            # most-recent probe silently replace the other provider's seat
            k = (r.get("provider") or "?", a)
            if k not in best or str(r.get("probed_at") or "") > str(best[k].get("probed_at") or ""):
                best[k] = r
    from .providers import NativeQuotaProvider
    out = []
    for (_prov, a), r in best.items():
        g = NativeQuotaProvider._primary(r["gauges"])
        out.append({"account": a, "provider": r.get("provider") or "?",
                    "headroom_pct": round(100 - g["utilization"] * 100, 1) if g else None,
                    "window": (g or {}).get("label") or "?",
                    "status": r.get("status") or "",
                    "probed_at": r.get("probed_at") or ""})
    out.sort(key=lambda s: (s["provider"], -(s["headroom_pct"] or 0)))
    return out


# ------------------------------------------------------------- waiting on you

def _waiting():
    """Owner-gated items the estate already records — mechanical read-only
    scans only, mirroring what doctor surfaces, no LLM, no probe."""
    from . import registry, store, whoami
    from .premise import _queue_path
    items = []
    p = whoami.load_profile()
    if p["interview_status"] != "done":
        items.append("know-your-user interview %s — `helm interview`"
                     % (p["interview_status"] or "not started"))
    try:
        with open(_queue_path(), encoding="utf-8") as f:
            n = sum(1 for l in f if l.strip())
    except OSError:
        n = 0
    if n:
        items.append("%d attestation%s queued — `helm premise --retry-queue`"
                     % (n, "s"[:n != 1]))
    stale = []
    for name, rec in sorted((registry.load().get("projects") or {}).items()):
        if rec.get("retired"):
            continue
        pd = home.project_dir(name)
        mem = rec.get("memory_dir")
        if (os.path.islink(pd) and not os.path.exists(pd)) \
                or (rec.get("path") and not os.path.exists(rec["path"])) \
                or (mem and not os.path.isdir(mem)):
            stale.append(name)
    if stale:
        items.append("%d project pointer%s stale (%s) — `helm doctor`" % (
            len(stale), "s"[:len(stale) != 1],
            ", ".join(stale[:3]) + (", …" if len(stale) > 3 else "")))
    try:
        files = [f for f in os.listdir(store.adopted_dir()) if f.endswith(".md")]
    except OSError:
        files = []
    dups = {f[5:] for f in files if f.startswith("prem-")} \
        & {f[6:] for f in files if f.startswith("prior-")}
    if dups:
        items.append("%d prem/prior duplicate pair%s — `helm drain --sweep-dups` "
                     "(owner-gated)" % (len(dups), "s"[:len(dups) != 1]))
    return items


# --------------------------------------------------------------- compose/render

def compose(hours=12.0, repo=None):
    """The raw brief dict (`--json` prints exactly this). `repo` is the
    checkout the WHAT WE BUILT gauge reads (default: the cwd the brief was
    asked from — the repo the operator is standing in)."""
    cutoff = time.time() - hours * 3600
    return {"generated_at": pk.now_ts(), "hours": hours,
            "sessions": _sessions_delta(cutoff),
            "built": _built(repo),
            "knowledge": _knowledge_delta(cutoff),
            "inject": _inject_stats(cutoff),
            "review": _review_queue(),
            "seats": _seats(),
            "waiting": _waiting()}


def _ago(iso):
    e = _ts_epoch(iso)
    if not e:
        return "?"
    s = max(0, time.time() - e)
    if s < 3600:
        return "%dm ago" % (s // 60)
    if s < 86400:
        return "%dh ago" % (s // 3600)
    return "%dd ago" % (s // 86400)


def render(b):
    """Tight, headline-first text (~40 lines max): every section headed and
    skippable, empty sections omitted, a no-data estate says so in one line."""
    lines = ["helm brief — %s (last %gh)" % (b["generated_at"], b["hours"])]
    s = b["sessions"]
    if s["total"]:
        lines += ["", "SINCE YOU LEFT — %d session%s, %d project%s" % (
            s["total"], "s"[:s["total"] != 1],
            len(s["projects"]), "s"[:len(s["projects"]) != 1])]
        for p in s["projects"][:MAX_PROJECTS]:
            lines.append("  %-20s %3d  %s" % (p["name"][:20], p["n"],
                                              (p["latest_title"] or "")[:46]))
        more = len(s["projects"]) - MAX_PROJECTS
        if more > 0:
            lines.append("  (+%d more project%s)" % (more, "s"[:more != 1]))
    bl, bd = b["built"], b["built"]["board"]
    if bd["unavailable"]:
        board = "board unavailable — " + str(bd["unavailable"])[:60]
    elif bd["loops"]:
        board = "%d open land loop%s, %d stalled" % (
            bd["loops"], "s"[:bd["loops"] != 1], bd["stalled"])
    else:
        board = "no open land loops"
    if bl["git_error"]:
        lines += ["", "WHAT WE BUILT — trunk unreadable · " + board,
                  "  git unreadable at %s: %s" % (bl["repo"], bl["git_error"])]
    elif not bl["total"]:
        lines += ["", "WHAT WE BUILT — nothing landed on %s in the last %gh · %s"
                  % (bl["ref"], bl["window_h"], board)]
    else:
        if bl.get("published", True):
            head = "WHAT WE BUILT — %d land%s on %s (last %gh) · %s" % (
                bl["total"], "s"[:bl["total"] != 1], bl["ref"],
                bl["window_h"], board)
        else:
            # A local-only ref proves COMMITS, never publication — name the
            # claim instead of rendering local merges as lands (a cross-family
            # review finding, the landed-means-origin-main class).
            head = ("WHAT WE BUILT — %d LOCAL commit%s on %s (last %gh; no "
                    "remote-tracking ref — local history is not proof of "
                    "publication) · %s" % (
                        bl["total"], "s"[:bl["total"] != 1], bl["ref"],
                        bl["window_h"], board))
        lines += ["", head]
        shown = 0
        for d in bl["days"]:
            if shown >= MAX_BUILT:
                break
            lines.append("  " + d["day"])
            for land in d["lands"]:
                if shown >= MAX_BUILT:
                    break
                lines.append("    %s  %s" % (land["sha"], land["subject"][:66]))
                shown += 1
        more = bl["total"] - shown
        if more > 0:
            lines.append("  (+%d more land%s)" % (more, "s"[:more != 1]))
    k, inj = b["knowledge"], b["inject"]
    segs = [fmt % len(k[key]) for fmt, key in
            (("+%d added", "added"), ("%d updated", "updated"), ("%d retired", "retired"))
            if k[key]]
    if k["drained"]:
        segs.append("%d drained" % k["drained"])
    live_inject = bool(inj and inj["turns"])
    if segs or live_inject:
        lines += ["", "KNOWLEDGE DELTA — " + (" · ".join(segs) or "store unchanged")]
        for rid in k["added"][:3]:
            lines.append("  + " + rid)
        if live_inject:
            top = ", ".join("%s ×%d" % (i, n) for i, n in inj["top"])
            lines.append("  inject: %d turn%s · %d%% silent%s" % (
                inj["turns"], "s"[:inj["turns"] != 1],
                round(inj["silent_rate"] * 100), " · top " + top if top else ""))
            if inj.get("starved"):
                ids = ", ".join(inj["starved"][:4])
                more = len(inj["starved"]) - 4
                # CANNOT-FIRE leads: it is the actionable half and the half
                # that was invisible. "never fired" is appended only as a
                # count, because a newly-pinned entry lands there innocently
                # and giving it equal billing is how the real signal gets
                # trained away.
                head = ("%d of %d CANNOT FIRE (budget spent before the walk "
                        "reaches them)" % (len(inj["cannot_fire"]), inj["always_n"])
                        if inj["cannot_fire"] else
                        "%d of %d never fired" % (len(inj["starved"]), inj["always_n"]))
                tail = (", %d fit but never fired" % len(inj["never_fired"])
                        if inj["cannot_fire"] and inj["never_fired"] else "")
                lines.append("  pinned starvation: %s%s%s — "
                             "`helm store pinned --stats` (shorten one ahead of "
                             "them, or demote)" % (
                                 head, tail,
                                 ": " + ids + (" +%d" % more if more > 0 else "")))
    rq = b["review"]
    rq_active = bool(rq["provisional"] or rq["candidate"])
    if rq_active:
        head = ("STORE REVIEW QUEUE — %d provisional (firing, awaiting your ratify)"
                % len(rq["provisional"]))
        if rq["candidate"]:
            head += " · %d candidate" % rq["candidate"]
        lines += ["", head]
        for e in rq["provisional"][:MAX_REVIEW]:
            lines.append("  [%s] %s - %s" % (e["type"], str(e["id"]),
                                             (e.get("statement") or "")[:80]))
        more = len(rq["provisional"]) - MAX_REVIEW
        if more > 0:
            lines.append("  (+%d more provisional)" % more)
    lines += ["", "SEATS"]
    if b["seats"]:
        for r in b["seats"][:MAX_SEATS]:
            hp = "-" if r["headroom_pct"] is None else "%d%%" % round(r["headroom_pct"])
            lines.append("  %-9s %-30s %4s headroom (%s)  probed %s" % (
                r["provider"], r["account"][:30], hp, r["window"], _ago(r["probed_at"])))
    else:
        lines.append("  quota: run `helm creds`")
    if b["waiting"]:
        lines += ["", "WAITING ON YOU"]
        lines += ["  - " + it for it in b["waiting"][:MAX_WAITING]]
    if not (s["total"] or segs or live_inject or rq_active or b["waiting"]
            or bl["total"]):
        lines.insert(1, "quiet — nothing new in the window.")
    return "\n".join(lines)


def cmd_brief(args):
    """brief [--hours N] [--json] — the operator's morning brief: session
    activity, trunk lands (the WHAT WE BUILT gauge, read from the repo the
    brief is asked from), knowledge delta, cached seat reality, owner gates.
    Read-only, never probes the network."""
    # flags-only membership reader — guard the tail before compose():
    # `brief --bogus` silently rendered the brief and exited 0.
    from .cli import guard_tail
    rc = guard_tail("helm brief", args, flags=("--json",),
                    valued=("--hours",),
                    usage="brief [--hours N] [--json]")
    if rc is not None:
        return rc
    hours = 12.0
    if "--hours" in args:
        try:
            hours = float(args[args.index("--hours") + 1])
        except (IndexError, ValueError):
            print("usage: helm brief [--hours N] [--json]", file=sys.stderr)
            return 2
    b = compose(hours=hours)
    if "--json" in args:
        print(json.dumps(b, indent=2, ensure_ascii=False))
        return 0
    print(render(b))
    return 0
