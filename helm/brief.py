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
The OFFICE WEATHER line (task/3902) heads it all: the settled sunny, cloudy
or stormy line the weather pass last recorded, read from its state file.

THE MORNING REPORT (`--report`, task/3537) is the part of the owner's report
that was written by hand: one plain line per LAND, its words from trunk's
first-parent subjects (`plain_words`), its number from the LAND counter's
land log (`autoland.land_log`), its gate from the receipt that passed its
tree; every open owner ask, none folded; and a CHECKLIST it verifies, which
prints a MISSING line for every LAND n..m no line carries, for trunk that no
LAND number records, and for an open ask the report does not name.
`--check FILE` holds a report written by hand to the same checklist.
ORDER lines (task/3821) name a story whose lead has no lane while a later
sub-task went first, and owner-asked P0/P1 work with no lane for over 2h,
and each seat that owes seen-working checks on landed tasks
(helm/observed.py): "ORDER @seat owes N seen-working checks: <ids>". The
owing seat itself hears the same line through its stop whisper
(observed.stop_candidate), once per owed set.
"""
import glob
import json
import os
import re
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


def _weather():
    """THE OFFICE WEATHER heads the brief (task/3902): the floor's settled
    word and its line, every change of it, though the owner's phone hears
    only a storm and its all-clear. Read from the weather
    pass's own state file, the one small read this section allows: the brief
    never measures the floor itself."""
    from . import officeweather
    try:
        return officeweather.surface()
    except Exception as exc:                 # noqa: BLE001 — said, not lost
        return {"word": None, "shown": "office weather: UNKNOWN (%s)"
                % officeweather._why(exc)}


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
            "waiting": _waiting(),
            "weather": _weather()}


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
    if b.get("weather"):
        lines.append(b["weather"]["shown"])
    top = len(lines)
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
        lines.insert(top, "quiet — nothing new in the window.")
    return "\n".join(lines)


# ------------------------------------------------------------ morning report

#: How many of trunk's first-parent commits the report reads, newest first. A
#: bound, not a window: a recorded land past it reads MISSING, never absent.
REPORT_SCAN = 2000
#: A LAND's line carries each merge's first clause of plain words, up to
#: MERGE_WORDS characters, for its first LAND_MERGES merges.
MERGE_WORDS = 160
LAND_MERGES = 8
#: The widest "LAND a-b" a hand-written report may name as one range.
RANGE_CAP = 500

# A train merge's subject, as `helm train`, auto-land and the integrator
# write it: "<train>: merge lane <lane>[ at [its retip ]<sha>][ (<detail>)]
# [: <words>]".
_MERGE_SUBJECT = re.compile(
    r"\A(?P<train>[A-Za-z0-9][A-Za-z0-9._-]*): merge lane "
    r"(?P<lane>[^\s:()]+)(?: at (?:its retip )?[0-9a-f]{7,40})?(?P<rest>.*)\Z")
# A clause that says who built, held, patched or read a lane, its row, or
# auto-land's priority-and-door label, never what it changed: where a
# subject's plain words stop. The clause auto-land opens with a task's title
# ("task/N: <title>") is that title's words, whatever verdict it names.
_DOOR_LABEL = r"P(?:[0-9]+|\?), (?:not a door|a DOOR\b|doors UNKNOWN)"
_TITLED = r"(?:task/[^\s:,;]+|no task): "
_PROVENANCE = re.compile(
    r"\A(?:(?:built|author|held|reviewer|re-read|patch|row|round)\b|"
    + _DOOR_LABEL + r"|(?!" + _TITLED + r").*?\b(?:SOURCE-CLEAN|APPROVE)\b)")
# Auto-land's detail for a car whose task has no title: labels, no words.
_LABEL_ONLY = re.compile(r"\A(?:task/\S+|no task), " + _DOOR_LABEL)
# "LAND 389", "LANDs 319-323", "LAND 376–377": the lands a report names.
_LAND_NAMED = re.compile(
    r"\bLANDs?\s+(\d{1,6})(?:\s*[-–]\s*(\d{1,6}))?(?!\d)")
# A markdown table whose first column is the LAND number ("| Land | ...").
_TABLE_HEAD = re.compile(r"\A\s*\|\s*land\s*\|", re.I)
_TABLE_ROW = re.compile(r"\A\s*\|\s*(\d{1,6})\s*\|")


def _closing(text, start):
    """The index of the parenthesis that closes the one at `start`, or
    None when it never closes."""
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return i
    return None


def _clauses(text, short=False):
    """`text`'s clauses up to the first that is provenance, rejoined; only
    the first of them when `short`."""
    kept = []
    for clause in (text or "").split(";"):
        clause = clause.strip()
        if _PROVENANCE.search(clause):
            break
        if clause:
            kept.append(clause)
    return "; ".join(kept[:1] if short else kept)


def _without_note(text):
    """`text` without a trailing parenthesis that is a reader's note."""
    text = text.rstrip()
    if not text.endswith(")"):
        return text
    depth = 0
    for i in range(len(text) - 1, -1, -1):
        depth += {")": 1, "(": -1}.get(text[i], 0)
        if depth == 0:
            return text[:i] if _PROVENANCE.search(text[i:]) else text
    return text


def plain_words(subject, short=False):
    """What a trunk subject says the land changed, in plain words: a train
    merge's detail and words without the mechanics that merged it (`<train>:
    merge lane <lane> at <sha>`) or the clauses that say who built, held or
    read it; its lane first when the rest is only auto-land's labels, and
    alone when that is all it says. Any other subject is its own plain
    words. `short` keeps the first clause of the detail and of the words,
    which is what a LAND's line carries."""
    subject = " ".join(str(subject or "").split())
    m = _MERGE_SUBJECT.match(subject)
    if not m:
        return (_clauses(subject, short) or subject) if short else subject
    head, rest = "", m.group("rest")
    if rest.startswith(" ("):
        end = _closing(rest, 1)
        head, rest = (rest[2:], "") if end is None \
            else (rest[2:end], rest[end + 1:])
    words = _clauses(_without_note(rest[1:] if rest.startswith(":")
                                   else rest), short)
    head, lane = _clauses(head, short), "lane %s" % m.group("lane")
    if head and not words and _LABEL_ONLY.match(head):
        return "%s: %s" % (lane, head)
    return ("%s: %s" % (head, words) if head and words else head or words
            or lane)


def _gate_words(row, commit, receipts):
    """The gate that passed a land: the one the land log recorded, else the
    newest whole-suite OK receipt on the land's head or its exact tree."""
    if row.get("gate"):
        return "gate:%s%s" % (row["gate"], "" if row.get("ran") is None
                              else " Ran %s" % row["ran"])
    rows, why = receipts()
    if why:
        return "gate UNKNOWN (the receipts do not read: %s)" % why
    hits = [r for r in rows if r.get("suite") is True
            and r.get("status") == "OK" and not r.get("dirty")
            and (r.get("head") == commit[0] or r.get("tree") == commit[1])]
    if not hits:
        return "no whole-suite receipt on its tree here"
    best = max(hits, key=lambda r: str(r.get("ts") or ""))
    return "gate:%s Ran %s" % (best.get("id"), best.get("ran"))


def _receipts_once():
    """A reader of the gate receipts that reads them at most once."""
    got = []

    def read():
        if not got:
            from . import gate
            try:
                rows, why, _skipped = gate.receipts()
            except Exception as exc:  # noqa: BLE001 — a line, never a crash
                rows, why = [], "%s: %s" % (type(exc).__name__, exc)
            got.append((rows, why))
        return got[0]
    return read


def _report_lands(repo, cutoff, first=None):
    """The LANDED section and its two checklist items, read from trunk's
    first-parent line, the land log and the gate receipts.

    -> {repo, ref, published, error, log_error, first, last, lands: [{n,
    sha, words, gate}], missing: [MISSING line], tail: [MISSING line]}.
    `first` is the first LAND the report names; None takes the oldest land
    the log records inside the window, or the number just past the nearest
    lower land trunk carries when that is earlier. A LAND's merges are trunk's
    first-parent commits after the nearest lower land trunk carries, up to
    and including its own head, so a number the log never recorded reads
    MISSING and its merges read under the next number it did."""
    from . import autoland, vcs
    from .work import _lanes
    out = {"repo": repo, "ref": None, "published": True, "error": None,
           "log_error": None, "first": first, "last": None, "lands": [],
           "missing": [], "tail": []}
    root = _lanes.find_root(repo)
    if not root:
        out["error"] = "%s is not inside a git repository" % repo
        return out
    v = vcs.backend(root)
    ref = out["ref"] = v.trunk_ref(root)
    out["published"] = v.text(root, "rev-parse", "--verify", "--quiet",
                              "refs/remotes/" + ref)[0] == 0
    rc, text, err = v.text(root, "log", ref, "--first-parent", "-n",
                           str(REPORT_SCAN), "--format=%H%x09%T%x09%ct%x09%s")
    if rc != 0:
        out["error"] = ((err or "git log failed").splitlines() or ["?"])[0]
        return out
    commits = [tuple(line.split("\t", 3)) for line in text.splitlines()
               if line.count("\t") >= 3]
    recorded, out["log_error"] = autoland.land_log(root)
    if not recorded:
        out["tail"].append(
            "MISSING LAND numbers: no land-log line or LAND counter records "
            "a land of this repository; `helm train auto seed <n> <sha>` "
            "records the last land")
        return out
    at, place = {c[0]: i for i, c in enumerate(commits)}, {}
    for n, row in recorded.items():
        sha = str(row["sha"])
        place[n] = at.get(sha) if len(sha) == 40 else next(
            (i for i, c in enumerate(commits) if c[0].startswith(sha)), None)
    last = out["last"] = max(recorded)
    if first is None:
        inside = [n for n, i in place.items()
                  if i is not None and int(commits[i][2]) >= cutoff]
        first = min(inside) if inside else None
        # the range opens just past the nearest lower land trunk carries: a
        # land no line records at the window's edge (a hand land nobody
        # seeded) reads MISSING below, never a range that starts past it
        below = [n for n, i in place.items()
                 if i is not None and first is not None and n < first]
        first = out["first"] = max(below) + 1 if below else first
    receipts = _receipts_once()
    unrecorded = []
    for k in range(first, last + 1) if first is not None else ():
        row, idx = recorded.get(k), place.get(k)
        if row is None:
            unrecorded.append(k)
            continue
        if idx is None:
            out["missing"].append((k, (
                "MISSING LAND %d: its recorded head %s is not on %s's "
                "first-parent line (the newest %d commits); `helm train auto "
                "seed %d <its head>` corrects it"
                % (k, str(row["sha"])[:12], ref, REPORT_SCAN, k))))
            continue
        # its merges stop at the nearest lower land from `first - 1` on that
        # trunk carries: a gap inside the range reads MISSING above, and one
        # below it would be a land no line names, so the head stands alone
        older = [place[j] for j in range(first - 1, k)
                 if place.get(j) is not None and place[j] > idx]
        span = range(idx, min(older)) if older else [idx]
        words = []
        for i in reversed(list(span)):
            said = _clip(pk.launder(plain_words(commits[i][3], short=True)),
                         MERGE_WORDS)
            if said not in words:
                words.append(said)
        more = len(words) - LAND_MERGES
        tail = " (+%d more merge%s)" % (more, "s"[:more != 1]) \
            if more > 0 else ""
        if not older:
            tail += (" (merges before its head not shown: LAND %d is not "
                     "recorded on this trunk)" % (k - 1))
        out["lands"].append({"n": k, "sha": commits[idx][0],
                             "words": "; ".join(words[:LAND_MERGES]) + tail,
                             "gate": _gate_words(row, commits[idx],
                                                 receipts)})
    for a, b in _runs(unrecorded):
        out["missing"].append((a, (
            "MISSING LAND %d: no land-log line records its head, so its "
            "merges read under the next LAND that is recorded; `helm train "
            "auto seed %d <its head>` records it" % (a, a)) if a == b else (
            "MISSING LANDs %d-%d: no land-log line records their heads, so "
            "their merges read under the next LAND that is recorded; `helm "
            "train auto seed <n> <its head>` records each" % (a, b))))
    out["missing"] = [text for _k, text in sorted(out["missing"])]
    top = place.get(last)
    if top is None and (first is None or first > last):
        out["tail"].append(
            "MISSING LAND %d: its recorded head %s is not on %s's "
            "first-parent line, so what trunk carries past it is UNKNOWN; "
            "`helm train auto seed %d <its head>` corrects it"
            % (last, str(recorded[last]["sha"])[:12], ref, last))
    elif top:
        newest = commits[0]
        out["tail"].append(
            "MISSING LAND %d: trunk carries %d merge%s past LAND %d that no "
            "LAND number records (newest %s: %s); `helm train auto seed %d "
            "%s` records it when they are one land"
            % (last + 1, top, "s"[:top != 1], last, newest[0][:12],
               _clip(pk.launder(plain_words(newest[3], short=True)),
                     MERGE_WORDS),
               last + 1, newest[0][:12]))
    return out


def _clip(text, width):
    return text if len(text) <= width else text[:width - 1].rstrip() + "…"


def _runs(numbers):
    """The consecutive runs in `numbers`, as (first, last) pairs."""
    runs = []
    for n in sorted(numbers):
        if runs and n == runs[-1][1] + 1:
            runs[-1][1] = n
        else:
            runs.append([n, n])
    return [tuple(r) for r in runs]


#: How long an owner-asked P0/P1 task may have no lane before the ORDER line
#: names it.
ORDER_UNROUTED_S = 2 * 3600


def _check_lines(rows):
    """One ORDER line per seat that owes seen-working checks on landed
    tasks among `rows` (helm/observed.py), in seat order; [] when none
    are owed."""
    from . import observed
    return ["ORDER @%s %s" % (seat, observed.line(len(ids), ids))
            for seat, ids in sorted(observed.owed(rows).items())]


def _went(tid, because, verb=""):
    """How one skip reads on an ORDER line: its reason quoted, or that it
    went ahead without one, or that nothing was recorded."""
    from . import tasks
    head = "%s %s" % (tid, verb) if verb else tid
    if because == tasks.NO_REASON:
        return "%s without a reason" % head
    if because:
        return '%s%s "%s"' % (head, ":" if verb else "", because)
    return "%s (no reason recorded)" % head


def _order_lines(repo, now):
    """The owner's ORDER lines (task/3821): the biggest lever first, as a
    fact he can check. [] when nothing is inverted or unrouted.

    One line per open story whose lead (the first open sub-task in the
    story's order, `tasks.story_order`) has no lane while a later sub-task
    has one, with the reason that later task went first when one was
    recorded (`--because`, on its lane or its chain's first row). One line
    for owner-asked P0/P1 tasks filed over ORDER_UNROUTED_S ago with no
    lane, naming each task that went ahead of one of them and its reason.
    Each list names its first `tasks.LEVER_NAMED` and counts the rest. A lane is a lane record in `repo` or an open dispatch chain for
    the task or a task below it (`taskkey.live_tasks`), or that task being
    `in_progress` (`helm task claim`). Rows are those of
    `repo`'s project when it derives to one. A read that fails is one ORDER
    UNKNOWN line, never silence."""
    from . import dispatches, taskkey, tasks
    from .inject._ledger import project_for_cwd
    from .work import _lanes
    root = _lanes.find_root(repo) or repo
    known, why = tasks.snapshot()
    why = why and "the task ledger could not be read (%s)" % why
    current, live, levers = {}, frozenset(), {}
    if not why:
        current, why = dispatches.snapshot()
        why = why and "the dispatch ledger could not be read (%s)" % why
    if not why:
        live, why = taskkey.live_tasks(root, current=current)
    if not why:
        levers, why = taskkey.lever_records(root)
    if why:
        return ["ORDER UNKNOWN: %s" % why]
    went = {rec["task"]: rec["because"] for rec in levers.values()}
    over = {rec["task"]: set(rec["skipped"]) for rec in levers.values()}
    for row in current.values():
        if row.get("task") and row.get("lever_because"):
            went.setdefault(row["task"], row["lever_because"])
            over.setdefault(row["task"], set(row.get("lever_skipped") or ()))
    facts = tasks.story_facts(known)

    def lane(tid):
        return any(r.get("id") in live or r.get("status") == "in_progress"
                   for r in [known.get(tid) or {}]
                   + list((facts.get(tid) or {}).get("below", ())))

    try:
        project = project_for_cwd(root)
    except Exception:                         # noqa: BLE001 — scope unknown
        project = None
    rows = tasks.board_order(
        r for r in known.values() if isinstance(r, dict)
        and r.get("status") in tasks.OPEN_STATUSES
        and (project is None or tasks.project_of_row(r) == project))
    kids = {}
    for r in rows:
        kids.setdefault(r.get("continues"), []).append(r)
    lines = []
    for story in rows:
        order = tasks.story_order(story, kids.get(story.get("id"), ()))
        ahead = [r["id"] for r in order[1:] if lane(r["id"])]
        if not ahead or lane(order[0]["id"]):
            continue
        lead = order[0]
        filed = tasks.filed_epoch(lead)
        lines.append("ORDER %s (%s): lead %s has no lane, filed %s · %s" % (
            _clip(story.get("title") or "", 48), story["id"], lead["id"],
            _age_s(int(now - filed)) if filed else "at an unknown time",
            tasks.named(_went(t, went.get(t), "went first") for t in ahead)))
    waiting = [r for r in rows if tasks.origin_of(r) == "owner"
               and r.get("priority") in tasks.LEVER_RANKS
               and (tasks.filed_epoch(r) or now) <= now - ORDER_UNROUTED_S
               and not lane(r["id"])]
    if waiting:
        ids = {r["id"] for r in waiting}
        ahead = sorted((t for t in went if went[t] and over.get(t, ()) & ids),
                       key=lambda t: tasks.sort_key({"id": t}))
        lines.append("ORDER owner-asked with no lane for over %dh: %s%s" % (
            ORDER_UNROUTED_S // 3600, tasks.named(
                "%s (%s, filed %s)" % (r["id"], r["priority"], _age_s(
                    int(now - tasks.filed_epoch(r)))) for r in waiting),
            " · went ahead: " + tasks.named(_went(t, went[t])
                                            for t in ahead) if ahead else ""))
    return lines + _check_lines(rows)


def morning_report(hours=12.0, repo=None, first=None):
    """(text, rc): the morning report and its checklist; rc is 1 when any
    checklist line is MISSING or UNKNOWN, else 0. The ORDER lines
    (`_order_lines`) sit between LANDED and WAITING ON YOU."""
    now = time.time()
    repo = repo or os.getcwd()
    got = _report_lands(repo, now - hours * 3600, first)
    asks, asks_err = _owner_asks(now)
    where = "%s%s" % (got["ref"] or repo, "" if got["published"] else
                      " (a LOCAL ref: local history is not proof of "
                      "publication)")
    ranged = got["first"] is not None and got["last"] is not None \
        and got["first"] <= got["last"]
    span = ("LANDs from %d to %d" % (got["first"], got["last"]) if ranged
            else "no LAND")
    lines = ["helm morning report — %s — %s on %s (last %gh)"
             % (pk.now_ts(), span, where, hours), "", "LANDED"]
    if got["error"]:
        lines.append("  trunk unreadable: %s" % got["error"])
    for land in got["lands"]:
        lines.append("  LAND %d: %s — %s · %s" % (
            land["n"], land["words"], land["gate"], land["sha"][:11]))
    if not got["lands"] and not got["error"]:
        lines.append("  none in the last %gh%s" % (
            hours, "; the last recorded is LAND %d" % got["last"]
            if got["last"] is not None else ""))
    order = _order_lines(repo, now)
    lines += [""] + order if order else []
    lines += ["", "WAITING ON YOU — %s" % (
        "UNKNOWN: " + str(asks_err) if asks_err else
        "%d open owner ask%s" % (len(asks), "s"[:len(asks) != 1]))]
    for rec in asks:
        age = _age_s(rec.get("age_s")) if rec.get("age_known") \
            else "age unknown"
        lines.append("  - %s · %s%s" % (
            age, rec.get("plain_title") or rec.get("title") or "owner input",
            " — " + _clip(rec["detail"], 160) if rec.get("detail") else ""))
    bad = 0
    lines += ["", "CHECKLIST"]
    if got["error"] or got["log_error"]:
        bad += 1
        lines.append("  every LAND present: UNKNOWN (%s)" % (
            got["error"] or "the land log does not read: %s"
            % got["log_error"]))
    if not got["error"]:
        items = (("every LAND from %d to %d present" % (got["first"],
                                                        got["last"])
                  if ranged else "every LAND present (none in the window)",
                  got["missing"]),
                 ("no trunk merge past the last LAND without a number",
                  got["tail"]))
        for item, missing in items:
            bad += bool(missing)
            lines.append("  %s: %s" % (item, "MISSING %d" % len(missing)
                                       if missing else "OK"))
            lines += ["    " + m for m in missing]
    bad += bool(asks_err)
    lines.append("  every open owner ask present: %s" % (
        "UNKNOWN (%s)" % asks_err if asks_err else "OK (%d)" % len(asks)))
    return "\n".join(lines), 1 if bad else 0


def lands_named(text):
    """The LAND numbers a report names: "LAND n", "LANDs a-b" (a hyphen or
    an en dash, at most RANGE_CAP wide) and the first cell of a table whose
    header's first cell is "Land". A line that says MISSING names no land:
    a checklist's word for a land is not the land."""
    named, table = set(), False
    for line in (text or "").splitlines():
        if line.strip().lstrip("-*•> ").startswith("MISSING"):
            continue
        if _TABLE_HEAD.match(line):
            table = True
            continue
        table = table and line.lstrip().startswith("|")
        row = _TABLE_ROW.match(line) if table else None
        if row:
            named.add(int(row.group(1)))
        for m in _LAND_NAMED.finditer(line):
            a = int(m.group(1))
            b = int(m.group(2)) if m.group(2) else a
            named.update(range(a, b + 1 if a <= b <= a + RANGE_CAP
                               else a + 1))
    return named


def check_report(text, repo=None, first=None):
    """(lines, rc): `text`, a report written by hand, held to the morning
    report's checklist. Every LAND from `first` (else the first it names)
    to the last it names, or the land log's last when that is later, must
    be named, and every open owner ask's title must appear in it; each one
    that is not prints MISSING, and an owner queue that does not read is
    UNKNOWN. rc is 1 on any MISSING or UNKNOWN."""
    from . import autoland
    from .work import _lanes
    repo = repo or os.getcwd()
    named = lands_named(text)
    lines, bad, log_last = [], 0, None
    root = _lanes.find_root(repo)
    if root:
        recorded, why = autoland.land_log(root)
        log_last = max(recorded) if recorded else None
        if why:
            bad += 1
            lines.append("UNKNOWN land log: it reads in part (%s), so its "
                         "last LAND may be later" % why)
    else:
        lines.append("the land log is not read: %s is not inside a git "
                     "repository" % repo)
    lo = first if first is not None else min(named) if named else None
    hi = max([n for n in (max(named) if named else None, log_last)
              if n is not None] or [None])
    if lo is None or hi is None:
        bad += 1
        lines.append("UNKNOWN LANDs: the report names none; `--from N` "
                     "names the first it should")
    else:
        lines.insert(0, "every LAND from %d to %d%s" % (
            lo, hi, " (the land log's last)" if log_last == hi
            and (not named or hi > max(named)) else ""))
        for a, b in _runs(k for k in range(lo, hi + 1) if k not in named):
            bad += 1
            lines.append("MISSING LAND %d" % a if a == b
                         else "MISSING LANDs %d-%d" % (a, b))
    asks, asks_err = _owner_asks(time.time())
    flat = " ".join(text.lower().split())
    if asks_err:
        bad += 1
        lines.append("UNKNOWN owner asks: %s" % asks_err)
    for rec in asks:
        title = rec.get("plain_title") or rec.get("title") or ""
        if " ".join(title.lower().split()) not in flat:
            bad += 1
            lines.append("MISSING owner ask: %s" % title)
    lines.append("CHECKLIST: %s" % ("%d not OK" % bad if bad else "OK"))
    return lines, 1 if bad else 0


USAGE = ("helm brief [--hours N] [--json] | --report [--hours N] [--from N] "
         "[--repo PATH] | --check FILE [--from N] [--repo PATH]")


def cmd_brief(args):
    """brief [--hours N] [--json] — the operator's morning brief: session
    activity, trunk lands (the WHAT WE BUILT gauge, read from the repo the
    brief is asked from), knowledge delta, cached seat reality, owner gates.
    Read-only, never probes the network. `--report` prints the morning report
    and its checklist, `--check FILE` holds a report written by hand to that
    checklist; both exit 1 on a MISSING or UNKNOWN line."""
    # flags-only membership reader — guard the tail before compose():
    # `brief --bogus` silently rendered the brief and exited 0.
    from .cli import guard_tail
    rc = guard_tail("helm brief", args, flags=("--json", "--report"),
                    valued=("--hours", "--check", "--from", "--repo"),
                    usage=USAGE)
    if rc is not None:
        return rc
    opts = {a: args[i + 1] for i, a in enumerate(args)
            if a in ("--hours", "--check", "--from", "--repo")}
    # one mode; --from and --repo only beside --report or --check, and a
    # hand-written report has no window
    modes = [a for a in ("--json", "--report", "--check") if a in args]
    scoped = "--from" in opts or "--repo" in opts
    if len(modes) > 1 or scoped and modes in ([], ["--json"]) \
            or "--check" in opts and "--hours" in opts:
        print("usage: " + USAGE, file=sys.stderr)
        return 2
    hours, first = 12.0, None
    try:
        if "--hours" in opts:
            hours = float(opts["--hours"])
        if "--from" in opts:
            first = int(opts["--from"])
    except ValueError:
        print("usage: " + USAGE, file=sys.stderr)
        return 2
    if "--report" in args:
        text, rc = morning_report(hours=hours, repo=opts.get("--repo"),
                                  first=first)
        print(text)
        return rc
    if "--check" in opts:
        try:
            with open(opts["--check"], encoding="utf-8",
                      errors="replace") as fh:
                text = fh.read()
        except OSError as exc:
            print("helm brief: %s does not read (%s)" % (opts["--check"],
                                                        exc), file=sys.stderr)
            return 1
        lines, rc = check_report(text, repo=opts.get("--repo"), first=first)
        print("helm brief --check %s" % opts["--check"])
        print("\n".join(lines))
        return rc
    b = compose(hours=hours)
    if "--json" in args:
        print(json.dumps(b, indent=2, ensure_ascii=False))
        return 0
    print(render(b))
    return 0
