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
  WAITING ON YOU  — estate-health gates and distinct fleet-filed owner asks,
                    including the board-recorded age of each ask
"""
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


def usage_history_path():
    """WHERE THIS PROCESS'S usage log lives — the cache root it was told about,
    not the one the module was imported next to.

    The constant above is resolved ONCE, at import, from the real home, so a
    caller under a test fixture reads THIS MACHINE'S live observation log
    however carefully it set its environment: the watchdog's flag fold did
    exactly that. `HELM_CACHE_DIR` is the tree's own override for that root
    (`registry.cache_root`), and it is honoured here rather than spelled a
    third time."""
    root = home.env("CACHE_DIR")
    if not root:
        return USAGE_HISTORY
    return os.path.join(root, "native-usage-history.jsonl")

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
    """Store/ledger timestamp -> UTC epoch; unparseable -> zero.

    Refusal belongs to pk.parse_ts_epoch. This caller explicitly turns refusal
    into zero because zero is safely outside every brief window; age renderers
    must keep None so malformed evidence never becomes a fabricated old age.
    """
    return pk.parse_ts_epoch(s) or 0


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

#: What the brief may spend deriving landing proofs it does not keep.
#:
#: Sized against what this caller actually consumes rather than against what
#: the projection can produce: the brief reduces the board to two counts, and
#: those counts are identical from one second to unbounded. The number that
#: matters is the hook's, not the board's — `landreq.BOARD_DERIVE_BUDGET_S` is
#: 300s for a reader whose job is to finish, and charging that to a 10s hook is
#: what made the day's first turn lose its whisper.
BRIEF_DERIVE_BUDGET_S = 1.0


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
    from . import landreq, projscope, vcs
    repo = repo or os.getcwd()
    v = vcs.backend(repo)
    ref = v.trunk_ref(repo)  # origin/<base> when a remote publishes one
    # LOCAL FALLBACK IS NOT LANDEDNESS. trunk_ref degrades to the local
    # branch when no remote exists, and a local merge is exactly the claim
    # this gauge must never launder into a "land" (a finding of
    # the landed-means-origin-main class). Ask git, never parse the name.
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
        # THE BRIEF KEEPS THREE VALUES AND PAID FOR A FULL LANDING
        # PROJECTION TO GET THEM. Below, this function reduces the board to
        # len(loops)/len(stalled) plus `unavailable`; every landing proof the
        # projection derives beyond that is discarded on the next line.
        #
        # WHAT THIS SCOPE ADDS, AND WHAT IT DOES NOT. `project_raw` opens a
        # projscope of its own, so the derive deadline is memoised and the
        # module default already refuses partway through a cold projection —
        # this scope does NOT make an inert guard live. What it adds is a
        # SHORTER deadline, seeded before any projection work, and a scope
        # lifetime spanning the whole of `_built`.
        #
        # SO IT BUYS SPEED BY DERIVING LESS, AND THAT IS A TRADE, NOT A FREE
        # WIN. A row the reader ran out of budget for reads UNKNOWN rather than
        # landed-or-not, which can move a row between the open and stalled
        # sets. The counts below are therefore reported WITH the number of rows
        # that could not be derived, because a count computed from a partial
        # projection and presented as exact is the failure this bound would
        # otherwise introduce.
        #
        # SOFT, never `projscope.scope(deadline=...)`: a hard deadline turns a
        # spent budget into an exception out of `project_raw` instead of rows
        # that read UNDERIVED. See `landreq.arm_derive_budget`.
        with projscope.scope():
            landreq.arm_derive_budget(BRIEF_DERIVE_BUDGET_S)
            b = landreq.board_section()
    except Exception as exc:  # noqa: BLE001 — the brief never dies on the board
        b = {"unavailable": "board projection raised: %s" % exc,
             "loops": [], "stalled": [], "undecided": None}
    # HOW MANY ROWS THE READER COULD NOT DERIVE, TAKEN FROM THE BOARD RATHER
    # THAN RECOUNTED HERE. `land_state` is UNKNOWN exactly when a row's landing
    # was not established — a spent derive budget is one cause and the one this
    # bound creates — and the count is deliberately the WIDER one (an unreadable
    # repo or a row with no verdict reads UNKNOWN too), because over-disclosing
    # uncertainty is the safe direction.
    #
    # IT CANNOT BE DERIVED FROM `loops` AND `stalled`, WHICH IS WHY IT IS NOT.
    # Those are two predicates over one population: they overlap, so a sum
    # across them bills a row twice, and they each withhold rows (foreign,
    # terminal, relieved), so an undecided row can be in neither while still
    # being a row this brief failed to decide. `board_section` counts the
    # projected population itself, keyed by id.
    #
    # None, not 0. A refused projection decided nothing, which is a different
    # fact from having decided everything, and the renderer below reads them
    # apart.
    undecided = b.get("undecided")
    return {"repo": repo, "ref": ref, "published": published,
            "window_h": BUILT_HOURS,
            "git_error": git_error, "days": days, "total": total,
            "board": {"loops": len(b["loops"]), "stalled": len(b["stalled"]),
                      "undecided": undecided,
                      "budget_s": BRIEF_DERIVE_BUDGET_S,
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
    try:
        projects = registry.load().get("projects") or {}
    except (OSError, ValueError):
        projects = {}
    for name in sorted(projects):
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
    current rows are v2, while optional reads retain historical v1 support."""
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
        # predicate cannot see the case it exists for. MEASURED 2026-07-30 on
        # the live store: 5 always-entries, budget 1200 fully consumed by the
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
    count. The owner steer 2026-07-23: this queue must ROUTINELY reach the owner,
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
    log. NEVER probes: no cache -> no rows (render says run `helm creds`).

    A gaugeless row does not replace the last gauged one: a transient probe
    error keeps the last reading. A MEMBER-UNKNOWN row does (task/2981).
    Before the member join a Team member's row was gauged from a SIBLING's
    pool file; after it, that member reads pool-member-unknown and carries no
    gauges, so this kept printing the sibling's number. So a member-unknown
    row retires every gauged row older than it, and the seat reads UNKNOWN
    until a newer gauged row, the member's own, arrives."""
    from .providers import NativeQuotaProvider, POOL_MEMBER_UNKNOWN
    best, unproven = {}, {}
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
            if not a:
                continue
            # key by (provider, account) — one email can seat BOTH an
            # anthropic and a codex identity; account-only keying let the
            # most-recent probe silently replace the other provider's seat
            k = (r.get("provider") or "?", a)
            pick = best if r.get("gauges") else \
                unproven if r.get("status") == POOL_MEMBER_UNKNOWN else None
            if pick is not None and (k not in pick or str(
                    r.get("probed_at") or "") > str(pick[k].get("probed_at") or "")):
                pick[k] = r
    for k, r in unproven.items():
        if k not in best or str(r.get("probed_at") or "") >= str(
                best[k].get("probed_at") or ""):
            best[k] = r
    out = []
    for (_prov, a), r in best.items():
        if not r.get("gauges"):
            out.append({"account": a, "provider": r.get("provider") or "?",
                        "headroom_pct": None,
                        "window": "UNKNOWN: member unproven",
                        "status": r.get("status") or "",
                        "probed_at": r.get("probed_at") or ""})
            continue
        g = NativeQuotaProvider._primary(r["gauges"])
        # An unread binding gauge (utilization None) has UNKNOWN headroom,
        # never 100% and never 0% — the probe's own reading (task/2935).
        out.append({"account": a, "provider": r.get("provider") or "?",
                    "headroom_pct": NativeQuotaProvider._headroom(g),
                    "window": (g or {}).get("label") or "?",
                    "status": r.get("status") or "",
                    "probed_at": r.get("probed_at") or ""})
    out.sort(key=lambda s: (s["provider"], -(s["headroom_pct"] or 0)))
    return out


# ------------------------------------------------------------- waiting on you

def _waiting():
    """Estate-health owner gates — mechanical read-only scans only.

    These are doctor-style conditions inferred from local estate state. They
    stay distinct from _owner_asks(), whose rows were explicitly filed by the
    fleet on the integration board.
    """
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
    try:
        projects = registry.load().get("projects") or {}
    except (OSError, ValueError):
        projects = {}
    for name, rec in sorted(projects.items()):
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
    items += _chat_durability()
    return items


def _owner_asks(now):
    """Canonical owner-ask rows, including scheduler-owned age judgement.

    The brief and Web scheduler must not independently decide which board rows
    are owner debt, whether a malformed queue is empty, or how a timestamp maps
    to a known age. Keep this private door for callers/tests that predate the
    scheduler module; delegate the full judgement rather than only the file read.
    """
    from . import scheduler
    raw, error = scheduler.read_owner_asks()
    model = scheduler.project([], active_ids=[], owner_asks=raw,
                              owner_asks_unavailable=error, now=now,
                              projection_age_s=0)
    return model["owner_asks"], model["owner_asks_unavailable"]


def _chat_durability():
    """WAITING ON YOU, because the morning brief is where this belonged.

    Measured 2026-08-11: the chat flush cursor sat EIGHT HOURS behind a
    THREE-MINUTE timer, every row of the fleet's morning coordination existed
    in RAM only, and the owner found it by asking why memory was not on disk.
    The fact was on the filesystem the whole time; no surface he reads carried
    it. `helm doctor` carries it now, but doctor is a verb somebody has to
    decide to run — the brief is the one he already reads.

    Read-only and cheap, as this section requires: two file reads, no probe.

    SILENT WHEN NO FLUSH HAS EVER RUN, deliberately. That is an install-time
    fact about a host with nothing to lose yet, not something the owner is
    holding up — `helm doctor` says it, where setup questions belong. A brief
    that opens every fresh estate with a durability warning is how this line
    would earn a skim.
    """
    from . import chat
    h = chat.flush_health()
    if h["disabled"]:
        return ["chat log-flush DISABLED — the rooms are tmpfs, so a reboot "
                "loses every row written since it was turned off "
                "(HELM_CHAT_LOG)"]
    if h["age_s"] is None:
        return []
    if h["age_s"] > chat.FLUSH_STALE_WARN_INTERVALS * h["interval_s"]:
        return ["chat journal %s behind its %ds timer — every row since then "
                "is in RAM only and dies with the machine; `helm chat "
                "log-flush`" % (chat._dur(h["age_s"]), h["interval_s"])]
    if h["quarantined"]:
        return ["chat log-flush refused %d room%s (%s) — those rows stay "
                "undurable until the divergence is repaired"
                % (len(h["quarantined"]), "s"[:len(h["quarantined"]) != 1],
                   ", ".join(h["quarantined"][:3]))]
    return []


# --------------------------------------------------------------- compose/render

def compose(hours=12.0, repo=None):
    """The raw brief dict (`--json` prints exactly this). `repo` is the
    checkout the WHAT WE BUILT gauge reads (default: the cwd the brief was
    asked from — the repo the operator is standing in)."""
    now = time.time()
    cutoff = now - hours * 3600
    asks, asks_err = _owner_asks(now)
    return {"generated_at": pk.now_ts(), "hours": hours,
            "sessions": _sessions_delta(cutoff),
            "built": _built(repo),
            "knowledge": _knowledge_delta(cutoff),
            "inject": _inject_stats(cutoff),
            "review": _review_queue(),
            "seats": _seats(),
            "owner_asks": asks,
            "owner_asks_unavailable": asks_err,
            "waiting": _waiting()}


def _ago(iso):
    e = _ts_epoch(iso)
    if not e:
        return "?"
    s = time.time() - e
    if s < 0:
        return "?"
    if s < 3600:
        return "%dm ago" % (s // 60)
    if s < 86400:
        return "%dh ago" % (s // 3600)
    return "%dd ago" % (s // 86400)


def _age_s(seconds):
    if not isinstance(seconds, int) or seconds < 0:
        return "age unknown"
    if seconds < 3600:
        return "%dm ago" % (seconds // 60)
    if seconds < 86400:
        return "%dh ago" % (seconds // 3600)
    return "%dd ago" % (seconds // 86400)


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
    # A COUNT FROM A PARTIAL PROJECTION SAYS SO — AND IT SAYS SO LOUDEST AT
    # ZERO. The brief bounds the derive budget to fit a hook, so rows can go
    # undecided, and an exact number rendered over them is the lie the bound
    # introduced. Attaching this only to the non-empty branch dropped it on
    # "no open land loops", which is the reading where an undecided row
    # changes the ANSWER rather than the magnitude: a reader told there is
    # nothing in flight stops looking, and rows this brief could not decide
    # are exactly the ones that might be in flight.
    if bd.get("undecided"):
        board += " (%d undecided within a %.0fs derive budget, so this " \
                 "may move)" % (bd["undecided"], bd.get("budget_s") or 0)
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
            # claim instead of rendering local merges as lands (the
            # landed-means-origin-main class).
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
    estate, asks = b["waiting"], b.get("owner_asks") or []
    asks_err = b.get("owner_asks_unavailable")
    if estate or asks or asks_err:
        lines += ["", "WAITING ON YOU"]
        if asks_err:
            lines.append("  FLEET-FILED OWNER ASKS — UNKNOWN: " + str(asks_err)[:90])
        if estate:
            lines.append("  ESTATE HEALTH — %d" % len(estate))
            lines += ["    - " + it for it in estate[:MAX_WAITING]]
            more = len(estate) - MAX_WAITING
            if more > 0:
                lines.append("    (+%d more estate gate%s)" % (
                    more, "s"[:more != 1]))
        if asks:
            lines.append("  FLEET-FILED OWNER ASKS — %d" % len(asks))
            for rec in asks[:MAX_WAITING]:
                age = _age_s(rec.get("age_s")) if rec.get("age_known") \
                    else "age unknown"
                body = (rec.get("plain_title") or rec.get("title") or "owner input")
                if rec.get("detail"):
                    body += " — " + rec["detail"]
                if len(body) > 108:
                    body = body[:107].rstrip() + "…"
                lines.append("    - %s · %s" % (age, body))
            more = len(asks) - MAX_WAITING
            if more > 0:
                lines.append("    (+%d more fleet-filed ask%s)" % (
                    more, "s"[:more != 1]))
    if not (s["total"] or segs or live_inject or rq_active or estate or asks
            or asks_err or bl["total"]):
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
