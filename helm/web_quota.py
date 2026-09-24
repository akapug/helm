"""Quota projections for :mod:`helm.web`."""
import sys
from . import pk
# EXPLICIT, NOT INHERITED — the rule web_core already states one name at a
# time, applied to the whole family. Each name below reached this module
# ONLY through the globals() splice under this block, so it was bound when
# web.py had already been imported and ABSENT on a direct `from helm import
# <this module>`: a NameError at the first call, or a NameError swallowed by
# a fail-open. Measured in web_common, whose code_drift answered "no drift"
# from an unbound `time` — the half-live detector silenced by an import
# order. The binding is identical either way (web.py imports the same module
# object, and the facade fanout rebinds it over this one), so naming it here
# costs nothing and removes the ordering dependency.
import calendar
import glob
import json
import os
import shutil
import time
import traceback
# THE HELPERS TOO, by the same rule and for the same reason (task/2914): the
# world read calls `_provider` and `_cached`, the catalog calls `_transcripts`,
# the doors call `_q1` and read `_qlock`/`_qstate`, and each of them reached
# this module only through the splice. A direct import succeeded and the first
# call raised NameError. Both owners sit BEFORE this module in web_compat's
# import order and neither imports the facade or this module, so there is no
# cycle; the fanout still rebinds every name here to the facade's object, so a
# test that patches `web._provider` still reaches this module.
from .web_cache import _cached, _provider, _qlock, _qstate
from .web_common import _q1, _transcripts

_web =sys.modules.get(__package__ + ".web")
if _web is not None:
    globals().update({name: value for name, value in vars(_web).items()
                      if not (name.startswith("__") and name.endswith("__"))})



def _catalog_rows():
    """The catalog through the ONE acquisition path — transcripts.get_catalog()'s
    single-flight cache (cwd-overrides applied, 5-min TTL). This once ran its own
    second single-flight over catalog.build() directly: a duplicate cache with a
    different TTL that skipped the cwd-override pass, so the burn view could show
    a session under a stale cwd the rest of the UI had already re-homed. One
    cache, one lens, no divergence."""
    return _transcripts().get_catalog()["rows"]



def _claude_home_identity(home_p):
    """oauthAccount email from a home's .claude.json — identity METADATA, never tokens."""
    try:
        with pk.open_regular(os.path.join(home_p, ".claude.json")) as f:
            d = json.load(f)
        return (d.get("oauthAccount") or {}).get("emailAddress")
    except Exception:
        return None



def _norm(s):
    return "".join(ch if ch.isalnum() else "-" for ch in (s or "").lower()).strip("-")



# ── THE MEASURED WORLD ─────────────────────────────────────────────────────
# ONE ACQUISITION, THREE STATES, AND NO FOURTH.
#
# A PER-CASE HANDLER COVERS THE FAILURES ITS AUTHOR IMAGINED AND NO OTHERS,
# which is why there are none below. A catch naming `ProviderError` does not
# cover the `OSError` one raise-site over, and that OSError is real: the
# native provider appends every probe cycle to a history file, so an
# unwritable path raises it out of the enrichment. Uncovered, it answers a
# confirm that has ALREADY LANDED with a 500 and empties the owner's declared
# inventory on the read.
#
# The cure is not one more case. It is one object that
# names every state a caller can be in, and doors that consume THAT instead of
# catching for themselves. The states are about what helm KNOWS, never about
# what went wrong:
#
#   measured          the providers answered and named accounts.
#   nothing-measures  the providers answered and there is nothing here. A real
#                     reading and the honest empty — a machine with no quota
#                     provider measures nothing, and this tab stays FAIL-OPEN
#                     because sessions and resume do not need quota.
#   unreadable        the world could not be read. `cause` is the CLASS NAME
#                     of what stopped it and never its text: that text carries
#                     credential homes and paths, and every string on this
#                     object can reach the owner's page.
#
# "COULD NOT READ" AND "NOTHING THERE" NEVER SHARE A VALUE, at any of the
# depths he can see: the state, the sentence the card prints, and the per-row
# `quota_unreadable` marker. A page that draws the second when it means the
# third is telling him a confident wrong sentence about his own accounts.
#
# AND THE CLOSURE IS THE WHOLE POINT. `_read_the_world` has exactly ONE net
# around it. A `raise` added anywhere inside the acquisition tomorrow — a new
# provider call, a new merge step, a new file read — lands in that net and
# becomes `unreadable`, which every door below already handles. There is
# nowhere for a new failure to invent a behaviour nobody wrote for.


_MEASURED = "measured"
_NOTHING_MEASURES = "nothing-measures"
_UNREADABLE = "unreadable"


class MeasuredWorld:
    """What the providers say right now, as ONE object every door consumes.

    `accounts` is the provider's own rows (the join key and the subscription
    identities are derived from these); `rows` is the merged projection the
    quota table renders. Both are EMPTY when the world is unreadable, and no
    door may read that emptiness as a measurement — which is what `state` is
    for, and why every door asks the object rather than the list."""

    STATES = (_MEASURED, _NOTHING_MEASURES, _UNREADABLE)

    def __init__(self, state, accounts=(), rows=(), cause=None,
                 enrichment_cause=None):
        assert state in self.STATES, state
        self.state = state
        self.accounts = list(accounts)
        self.rows = list(rows)
        self.cause = cause
        self.enrichment_cause = enrichment_cause

    @classmethod
    def read(cls, accounts, rows, enrichment_cause=None):
        """A world that was READ. An account list that came back empty is
        `nothing-measures` — a reading, not a failure."""
        return cls(_MEASURED if accounts else _NOTHING_MEASURES,
                   accounts, rows, enrichment_cause=enrichment_cause)

    @classmethod
    def nothing_measures(cls):
        return cls(_NOTHING_MEASURES)

    @classmethod
    def unreadable(cls, exc):
        return cls(_UNREADABLE, cause=type(exc).__name__)

    @property
    def readable(self):
        return self.state != _UNREADABLE

    def why(self):
        """The sentence the card turns into "this row's measurement is UNKNOWN,
        not absent", or None when there is nothing to say.

        TWO SHAPES OF UNKNOWN, because the owner acts on them differently: a
        world helm could not read at all, and a world it read whose NUMBERS it
        could not. A reading that answered with nothing wears neither — it is a
        measurement, and it said nothing."""
        if self.state == _UNREADABLE:
            return ("helm could not read the measured quota rows just now (%s)"
                    % self.cause)
        if self.enrichment_cause:
            return ("helm measured these accounts but could not read their "
                    "quota windows just now (%s), so every number on them is "
                    "unknown rather than zero" % self.enrichment_cause)
        return None

    def client_rows(self):
        """The declared inventory as the browser sees it, joined to THIS world.

        `accounts.client_rows` stays the ONE producer of a record the page
        sees; what this adds is the abstention. A declared row claims its
        subscription through `measured_as`, and `accounts.declared_identity`
        resolves that name against the measured rows to recover the login
        behind it — falling back, correctly, to the vendor plus the raw name
        when there is nothing to resolve against. That fallback is what keeps
        a row the owner added by hand keyed, and it must stay.

        BUT A WORLD THAT COULD NOT BE READ IS NOT A WORLD WITH NOTHING IN IT,
        and handing the fallback an empty list is how this lane's original
        defect worked: it answered a DIFFERENT subscription key for the same
        row, so the record split off the row he was editing. With the world
        unread the honest answer is that helm does not know which subscription
        the row is on, so the key is WITHHELD rather than invented, and
        `measured_unavailable` says why.

        A WITHHELD KEY MUST NOT MOVE THE RECORD, and that is the PAGE's half of
        this contract. The page polls the measured rows on its own clock, so it
        can hold KEYED measured rows beside a record whose key was withheld
        here. `quotaRows` joins such a record through the measured row its
        `measured_as` names and uses that row's key; only when the page holds
        no such row does it join by the name itself. Falling back on the
        declared side alone would draw the record as a second row and leave the
        row he is editing with none."""
        from . import accounts as _accounts
        view = _accounts.client_rows(measured=self.accounts)
        if self.readable:
            return view
        return dict(view, accounts=[dict(row, subscription=None,
                                         subscription_unreadable=True)
                                    for row in view["accounts"]])

    def joinable(self):
        """The provider's OWN rows for the accounts the table shows — what
        `accounts.join` must key against, and never `rows`.

        A MERGED ROW DROPS THE `email`, and the email is the login. A codex
        home is named for its directory, so two homes on one login are two
        names that only the email ties together. Joined against `rows`, the
        declared card keyed the second home by its directory name while the
        table keyed it by its login: it offered "describe" for a home already
        on a described row, and the click minted a second record for one bill
        (task/2914). `client_rows` already keys against `accounts`, and so does
        the CLI's join, so this puts the card's two halves on one keying.

        RESTRICTED TO THE NAMES THE TABLE KEPT, because `resolve_measured`
        resolves the describe button's handle against `rows`: an account the
        table does not draw must not be offered a button that cannot land."""
        kept = {r["name"] for r in self.rows}
        return [a for a in self.accounts if a.get("name") in kept]

    def resolve_measured(self, row):
        """(row, refusal) — the measured account the page named by HANDLE,
        resolved back to the join key before the writer ever sees the row.

        THE CARD NAMES A MEASURED ACCOUNT BY ITS HANDLE because it is never
        given the name: the provider's spelling is usually an address and every
        human rendering of this surface masks it (accounts.measured_key). So
        the handle is resolved HERE — an adapter between the page's shape and
        the writer's — and the row that reaches disk still carries the real
        join key, because a masked join key joins nothing.

        TWO REFUSALS, AND THEY ARE DIFFERENT SENTENCES. "helm no longer
        measures that account" is a statement ABOUT THE WORLD, and it is only
        available when the world was read; with the world unread the one true
        sentence is that helm could not look. Both refuse rather than dropping
        the handle: a row written with no `measured_as` reads back as
        "declared, not measured", which is a confident wrong sentence about the
        exact account he just clicked."""
        if "measured_key" not in row:
            return row, None
        from . import accounts as _accounts
        out = {k: v for k, v in row.items() if k != "measured_key"}
        key = row.get("measured_key")
        if not key:
            return out, None
        if not self.readable:
            return row, ("helm cannot read the measured quota rows just now "
                         "(%s), so it cannot tell which account this is. "
                         "Nothing was changed; try again in a moment."
                         % self.cause)
        name = _accounts.resolve_measured_key(key, self.rows)
        if not name:
            return row, ("helm no longer measures the account that button came "
                         "from. Reload the quota tab and describe it again — "
                         "nothing was changed.")
        out["measured_as"] = name
        return out, None


def _log_world_fault(what):
    """The traceback of the exception being handled, to the helm web log.

    THE PAGE GETS THE CLASS NAME AND THE LOG GETS THE REST. Both nets on the
    world read turn a raise into a state the page can render, and that is
    right; but they logged nothing, so a NameError in the read showed only as
    a chip whose tooltip said "NameError" and a programming error could not be
    told apart from an outage (task/2914). The log is the one `helm web`
    already uses for an unexpected failure: its stderr, where `web_server`
    prints a handler's traceback and where its 500 tells him to look. It is
    local to his machine and never reaches the page, so the text rides whole.
    Once per build, not per request: the failure is cached like a reading.

    IT CANNOT RAISE, because it runs INSIDE the one net: a log write that
    raised (no stderr, a closed pipe) would carry the fault straight past the
    closure the net exists for."""
    try:
        sys.stderr.write("[helm web] quota: %s\n" % what)
        traceback.print_exc(file=sys.stderr)
        sys.stderr.flush()
    except Exception:  # noqa: BLE001 — see above: the net must stay closed
        pass


def _read_the_world():
    """THE ONE READING. Everything the quota tab knows about the providers is
    acquired here, once, and both projections of it are built together.

    TWO CACHES WITH THE SAME TTL ARE NOT ONE SNAPSHOT, which is the reason
    this is one function and one entry. Each entry ages from its own first
    build, so two reads behind two keys have independent epochs and can
    straddle a change — and the change is an ORDINARY operation here. A codex
    row is named for its credential HOME and its login is read out of the
    token inside it, so re-logging that home into another account keeps the
    name and moves the identity, which is what rotating a spent credential
    does. Straddle that and the page renders one subscription while the save
    keys the record to another.

    IT RAISES FREELY. Its caller holds the one net; nothing in here has to
    decide what a failure means."""
    import glob
    from .providers import NoQuotaProvider, POOL_MEMBER_UNKNOWN
    from . import accounts as _accounts
    HOME = os.path.expanduser("~")
    prov = _provider()
    # QUESTION ONE: WHO IS THERE — and the ONE branch entitled to answer
    # empty is the one that says there is nothing here to measure WITH. No
    # quota provider on this machine measures nothing, and the tab stays
    # fail-open over that, because sessions and resume do not need quota.
    #
    # IT IS NARROW ON PURPOSE, TWICE OVER. The catch is around the CALL and
    # not around the body, so a raise site added to the acquisition cannot
    # reach this answer; and it names `NoQuotaProvider` rather than its parent,
    # because a quota CLI that IS installed and answered non-JSON is a world
    # helm could not READ. Catching the parent here drew a broken binary as
    # "no accounts on this machine" — unreadable-as-empty at the one door that
    # decides whether there is a world at all.
    try:
        acquired = prov.accounts()
    except NoQuotaProvider:
        return MeasuredWorld.nothing_measures()
    # QUESTION TWO: HOW MUCH IS LEFT — and its failure is not an answer to the
    # first. These two calls read the credential STATES and the quota WINDOWS
    # of an account list that has ALREADY been acquired, and on this fleet they
    # fail for ordinary reasons: an expired credential (a ProviderError), or
    # the provider's own history append hitting an unwritable path (an
    # OSError out of the provider's own history append).
    #
    # SHARING THE ACQUISITION'S CATCH THREW THAT LIST AWAY, and CATCHING ONLY
    # ProviderError let the OSError out of the cache entirely and into the two
    # doors. So the catch is by POSITION, not by type: whatever stops the
    # enrichment, the identities already read survive it, and the class name of
    # what stopped it is carried.
    enrichment_cause = None
    try:
        states = {s.get("account"): s for s in prov.cred_state()}
        windows = {w.get("account"): w for w in prov.windows()}
    except Exception as exc:  # noqa: BLE001 — see above: by position, not type
        # THE CLASS NAME ONLY. This value reaches his page, and an exception's
        # own text on this path can carry a credential home or a path.
        states, windows = {}, {}
        enrichment_cause = type(exc).__name__
        _log_world_fault("the quota windows could not be read")
    merged = []
    for a in acquired:
        if a.get("provider") not in ("anthropic", "codex"):
            continue
        s = states.get(a["name"], {})
        w = windows.get(a["name"], {})
        home_p = a.get("home") or ""
        real = os.path.realpath(home_p) if home_p else ""
        home_name = os.path.basename(home_p) if home_p else None
        identity = _claude_home_identity(real) if a["provider"] == "anthropic" and real else None
        name_lies = bool(identity) and _norm(identity) not in (_norm(home_name), _norm(os.path.basename(real)))
        merged.append({
            # WHICH SUBSCRIPTION THIS HOME IS ONE OF. Opaque: the login it
            # is derived from is an address and this rides to the browser.
            # It is what lets the table put two homes on one row instead of
            # two rows, which is the owner's ruling for this surface.
            "subscription": _accounts.subscription_key(
                _accounts.measured_identity(a)),
            # THE HANDLE THE WRITE DOOR RESOLVES. A page describing an
            # account it has never been told the name of has to post
            # something, and `resolve_measured` turns this back into the
            # provider's own spelling on the way to disk.
            "measured_key": _accounts.measured_key(a["name"]),
            "name": a["name"], "provider": a["provider"], "home": home_p,
            "home_name": home_name, "identity": identity, "name_lies": name_lies,
            "active": a.get("active", False), "tier": s.get("tier") or a.get("tier"),
            "headroom": s.get("headroom_pct"), "state": s.get("cred_state", "unknown"),
            "status": s.get("status"), "resets_at_ms": s.get("resets_at_ms"),
            "windows_left": w.get("windows_left"), "windows_per_week": w.get("windows_per_week"),
            "windows_verdict": w.get("verdict"),
            # ONE WORD, TWO CAUSES (task/2981). `state` reads "unknown" both
            # for a row with no reading and for a codex Team member whose
            # workspace is pooled while no pool file is proven to be it. The
            # second is MEASURED, and its cure is to pool that member's own
            # credential, not to log in. So it rides its own flag, and the
            # page gives it its own label and cause.
            "member_unproven": s.get("status") == POOL_MEMBER_UNKNOWN,
        })
        if enrichment_cause:
            # CARRIED ON THE ROW, because the table filters a row with no
            # data out of the quota view and into the homes list — right
            # for a home with no auth and no reading, and wrong for an
            # account helm measures whose numbers it could not read this
            # minute. Dropped there, the page draws "nothing there" for
            # "could not read" one surface further out.
            merged[-1]["quota_unreadable"] = True
    seen = {}
    for h in glob.glob(f"{HOME}/.claude-homes/*/"):
        real = os.path.realpath(h)
        ident = _claude_home_identity(real)
        if ident and os.path.exists(os.path.join(real, ".credentials.json")):
            seen.setdefault(ident, set()).add(real)
    dups = {i: sorted(os.path.basename(p) for p in ps) for i, ps in seen.items() if len(ps) > 1}
    for c in merged:
        if c["provider"] == "anthropic" and c["name"] in dups:
            c["duplicate_homes"] = dups[c["name"]]
    return MeasuredWorld.read(acquired, merged, enrichment_cause)


def _world(refresh=False):
    """THE ONE ACQUISITION AND ITS ONE NET, cached as ONE entry.

    ANYTHING THAT RAISES INSIDE THE READ BECOMES `unreadable` HERE. That is
    the closure this whole shape exists for: there is exactly one `except` on
    this path, so a raise site added to the acquisition tomorrow produces a
    state every door below already consumes.

    AND THE FAILURE IS CACHED LIKE THE READING IT IS. The alternative is the
    GET and the POST of one click answering from two different worlds — the
    exact drift one entry exists to remove — because a failure nobody stored
    is re-attempted per door. "Measure now" (`refresh`) drops it, which is the
    control he already has on the page."""
    if refresh:
        with _qlock:
            # "measure now" means read the world again — dropping only the
            # merged rows would re-render the same identities with fresher
            # numbers and call that a measurement.
            _qstate.pop("creds", None)

    def build():
        try:
            return _read_the_world()
        except Exception as exc:  # noqa: BLE001 — THE one net; see above
            _log_world_fault("the measured world could not be read")
            return MeasuredWorld.unreadable(exc)
    return _cached("creds", 60, build)


def get_creds(refresh=False):
    """The merged quota rows the table renders — empty when the world could
    not be read, which is why no door decides anything from this list alone."""
    return _world(refresh).rows


def get_history(hours=168):
    def build():
        from .providers import ProviderError
        try:
            rows = _provider().history(hours)
        except ProviderError:
            return []
        # downsample to ~240 buckets per account — charts don't need minute-level points
        bucket_s = max(60, (hours * 3600) // 240)
        latest = {}
        for r in rows:  # newest sample per (account, bucket), independent of provider order
            try:
                ts = time.mktime(time.strptime(r["probed_at"][:19], "%Y-%m-%dT%H:%M:%S"))
            except (ValueError, KeyError):
                continue
            k = (r.get("account"), int(ts // bucket_s))
            if k not in latest or r["probed_at"] > latest[k]["probed_at"]:
                latest[k] = r
        return sorted(latest.values(), key=lambda r: r["probed_at"])  # oldest-first, guaranteed
    return _cached(f"history:{hours}", 120, build)



def _probe_epoch(s):
    """True epoch seconds for a provider probed_at (RFC3339 UTC '...Z'; a naive
    local string still parses). Burn buckets join against session mtimes (real
    epochs), so a tz-shifted parse would attribute burn to the wrong hour."""
    import calendar
    try:
        t = time.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")
    except (TypeError, ValueError):
        return None
    return calendar.timegm(t) if "Z" in s[19:] or "+00:00" in s[19:] else time.mktime(t)



def get_burn(hours=48):
    """The join only this surface can make: this burn spike ↔ that session. v1 is
    TEMPORAL — per account, the per-hour drop in remaining% of the binding '5h'
    gauge (floor 0: resets are not burn), joined to the sessions last-active in
    each hour. A session listed under a bucket was ACTIVE then, not proven to be
    the burner."""
    hours = max(1, min(168, hours))

    def build():
        hist = get_history(hours)
        if not hist:
            return {"buckets": [],
                    "note": "no quota provider — burn attribution needs burn history"}
        now = time.time()
        t_lo = now - hours * 3600
        # remaining% of the '5h' gauge per account, oldest-first (get_history guarantees order)
        samples = {}
        for r in hist:
            g = next((g for g in r.get("gauges", [])
                      if g.get("label") == "5h" and g.get("utilization") is not None), None)
            if g is None:
                continue
            ts = _probe_epoch(r.get("probed_at") or "")
            if ts is None:
                continue
            samples.setdefault(r.get("account"), []).append(
                (ts, (1 - min(1, g["utilization"])) * 100))
        burned = {}  # bucket epoch sec -> {account: pct burned}
        for acct, pts in samples.items():
            pts.sort()
            for (_, r0), (t1, r1) in zip(pts, pts[1:]):
                drop = r0 - r1
                if drop <= 0 or t1 < t_lo:  # floor 0: a rising gauge is a reset, not burn
                    continue
                b = int(t1 // 3600) * 3600
                acc = burned.setdefault(b, {})
                acc[acct] = acc.get(acct, 0) + drop
        by_bucket = {}
        for r in _catalog_rows():
            mt = r.get("mt") or 0
            if mt < t_lo:
                continue
            by_bucket.setdefault(int(mt // 3600) * 3600, []).append(r)
        buckets = []
        for b in sorted(set(burned) | set(by_bucket)):
            sess = sorted(by_bucket.get(b, []), key=lambda r: r["z"], reverse=True)[:8]
            buckets.append({
                "t": b * 1000,
                "byAccount": {a: round(p, 1) for a, p in sorted(burned.get(b, {}).items())
                              if p >= 0.05},
                "sessions": [{"i": s["i"], "t": s["t"][:60], "c": s["c"], "h": s["h"]}
                             for s in sess],
            })
        return {"buckets": buckets,
                "note": "temporal join v1: burn = per-hour drop of each account's binding 5h "
                        "gauge; sessions = last-active that hour, capped at the 8 largest "
                        "per bucket — coincidence in time, not proven causation"}
    return _cached(f"burn:{hours}", 120, build)



def _alloc_models():
    from . import localnames
    return (os.environ.get("HELM_ALLOC_MODELS")
            or localnames.legacy_env("ALLOC_MODELS") or "fable,opus,gpt-5.5").split(",")



def get_allocations():
    def build():
        from .providers import ProviderError
        out = {}
        for m in _alloc_models():
            m = m.strip()
            if not m:
                continue
            try:
                ranked = _provider().allocate(m)
            except ProviderError:
                ranked = []
            pick = next((r for r in ranked if r.get("eligible")), None)
            out[m] = {"pick": pick, "ranked": ranked[:5]}
        return out
    return _cached("alloc", 120, build)



def get_flags():
    """The burn-flag snapshot for the quota tab — READ-ONLY, NO NEW PROBE.

    The watchdog pass is the writer; this reads the file it already wrote,
    through the same 120-second single-flight cache the other quota readers
    use. A stale or absent snapshot comes back as `measured: false` rather
    than as an old colour, because a colour nobody refreshed is the one thing
    this tab must not render as current."""
    def build():
        from . import burnflags
        snap, age = burnflags.cached_snapshot()
        if not snap:
            return {"measured": False, "bound_s": burnflags.max_age_s(),
                    "families": {}, "overall": None,
                    "why": "no fresh burn-flag snapshot — the writer is the "
                           "proxywatch pass"}
        # `measured_at` is the snapshot's own instant, so a reader holding
        # this body past the cache window can still say how old it is.
        return {"measured": True, "age_s": round(age),
                "measured_at": snap.get("ts"),
                "bound_s": burnflags.max_age_s(),
                "families": snap.get("families") or {},
                "overall": snap.get("overall"),
                "line": burnflags.line(snap)}
    return _cached("flags", 120, build)



def _api_flags():
    try:
        return get_flags()
    except Exception:
        return {"measured": False, "families": {}, "overall": None,
                "why": "the burn-flag read raised — UNMEASURED, not clean"}



def _api_creds(qs):
    """The quota table's rows — or the fact that they could not be read.

    NO `try` HERE ANY MORE, and that is the change rather than an omission:
    the acquisition cannot raise, so this door has nothing to catch and cannot
    disagree with any other door about what happened. The page reads a
    non-list as "no rows", which is right — an unreadable world has no rows —
    and `why` is the class name, never the exception's text."""
    world = _world(refresh=_q1(qs, "refresh") == "1")
    if world.readable:
        return world.rows, 200
    return {"unavailable": True, "why": world.why()}, 200



# ── WHAT THE CHART IS ALLOWED TO COST A PHONE ────────────────────────────────
# MEASURED on the owner's own probe history: /api/history?hours=168 shipped
# 766,109 bytes over 1,993 rows, and the quota tab re-fetches it on a 120s
# interval. Server time was never the problem; bytes on a cellular link were.
#
# THESE ARE THE FIELDS THE SHIPPED PAGE READS, and the list is exhaustive
# rather than a sample: every reader of the history list is `HIST` in
# helm/web_ui, and all five of them (currentProbes, gaugeLabels, seriesFor,
# latestGauges, quotaRenderAge) between them touch `account`, `probed_at`,
# `provider` and, inside a gauge, `label`, `utilization` and `reset`. Nothing
# else on the row is read by anything — see HistoryWireReadersTest, which
# proves it by RUNNING those five functions out of the shipped page over the
# trimmed payload and the untrimmed one and comparing their answers.
HISTORY_WIRE_KEYS = ("account", "probed_at", "provider")
HISTORY_GAUGE_WIRE_KEYS = ("label", "utilization", "reset")


def _history_on_the_wire(rows):
    """The history list as the chart needs it, and no wider.

    THE TRIM IS AT EMIT AND NOWHERE EARLIER. `get_history` keeps the whole
    provider row because get_burn joins against the same list and because the
    cache is shared; only this door, the one a browser pulls, narrows it. A
    trim pushed one function inwards would silently narrow burn attribution
    too, and nothing on that surface would say so.

    THIS IS A DROP, NOT A TRUNCATION, and the distinction is the contract: no
    row here carries less of a field than it has — a dropped key has no reader
    at all, so there is no "rest of it" for a reader to be misled about and no
    `_more` flag that could honestly be either value. Every row that went in
    comes out, with its identity (`account`, `probed_at`) intact, so a caller
    counting this list or reading its age gets the answer it always did."""
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        trimmed = {k: row[k] for k in HISTORY_WIRE_KEYS if k in row}
        gauges = row.get("gauges")
        if isinstance(gauges, list):
            trimmed["gauges"] = [
                {k: g[k] for k in HISTORY_GAUGE_WIRE_KEYS if k in g}
                for g in gauges if isinstance(g, dict)]
        out.append(trimmed)
    return out


def _api_history(qs):
    try:
        return _history_on_the_wire(
            get_history(hours=int(_q1(qs, "hours", "168")))), 200
    except ValueError:
        return {"error": "hours wants an integer"}, 400
    except Exception:
        return {"unavailable": True}, 200



def _api_burn(qs):
    try:
        return get_burn(hours=int(_q1(qs, "hours", "48"))), 200
    except ValueError:
        return {"error": "hours wants an integer"}, 400
    except Exception:
        return {"unavailable": True}, 200



def _api_allocate():
    try:
        return get_allocations()
    except Exception:
        return {"unavailable": True}



def _api_quota_status():
    """Same shape as the predecessor's /api/status: is a provider present, how many accounts,
    does cv exist, how big is the cached catalog."""
    import shutil
    from . import localnames
    try:
        qcli = type(_provider()).__name__.replace("QuotaProvider", "").lower()
    except Exception:
        qcli = "none"
    world = _world()
    creds_n = len(world.rows)
    with _qlock:
        cat = _qstate.get("catalog")
    return {
        "provider": {"configured": qcli,
                     "present": qcli == "native" or bool(shutil.which(
                         os.environ.get("HELM_QUOTA_CLI")
                         or localnames.legacy_env("QUOTA_CLI") or "")),
                     "accounts": creds_n,
                     "degraded": creds_n == 0,
                     # A PROVIDER THAT COULD NOT BE READ IS NOT A PROVIDER
                     # WITH NO ACCOUNTS: both count zero, and only one of them
                     # means the numbers on this tab are unknown.
                     "unreadable": world.why() if not world.readable else None,
                     "fallback": "(default) account resume works without a provider"},
        "cv": bool(shutil.which("cv")),
        "catalogRows": len(cat[1]) if cat else None,
        "mutations": "POST JSON + Authorization: Bearer (per-process token, "
                     "templated into the UI)",
    }
del _web


# ── the OWNER-DECLARED account inventory (helm/accounts.py backend) ──
# accounts.py owns every behaviour: the schema, the secret refusal, the
# optimistic-concurrency revision and the join. These two handlers only adapt
# query/payload shapes, and they pass the writer's refusals through VERBATIM —
# a page that rewrites a refusal into its own words teaches the owner a
# vocabulary the CLI does not share (_api_inject_act's law).

def _measured_accounts():
    """The provider's own rows for the account doors — THE SAME acquisition the
    quota rows were rendered from, not a second read of the same age.

    Those rows cost ~170ms to gather (a stat and a JSON read per credential
    home) and both account doors need them, so caching is worth having on its
    own; but the reason this is not its own entry is correctness, not cost. The
    subscription keys are derived from these rows, and a second entry with an
    equal TTL is a second EPOCH — it can be built either side of a re-login and
    hand the page two keyings of one account.

    THIS IS THE FACADE-REGISTERED DOOR and it answers a LIST, so it cannot say
    which of the three states produced an empty one. Inside this module the
    handlers take `_world()` itself for exactly that reason."""
    return _world().accounts


def _api_accounts():
    """What the owner DECLARED, joined to what helm MEASURES.

    THE TWO SIDES ARE TWO QUESTIONS AND THIS DOOR KEEPS THEM APART.
    `unreadable` is the sentence the card renders INSTEAD of his rows, and it
    is about HIS INVENTORY. A measured-side raise reaching it takes rows he
    typed off his screen and tells him helm does not know how many accounts he
    has — from a failure that said nothing about his file at all.

    So the world is acquired FIRST, where it cannot raise, and the `except`
    below can only ever be about the declared read. The measured side reports
    through `measured_unavailable`, which decorates the rows instead of
    replacing them.

    Three join states and they stay three: `matched` decorates a measured row,
    `declared_only` is the normal case for an account no provider watches, and
    `measured_only` is a row nobody has described yet."""
    world = _world()
    try:
        from . import accounts
        # THE ONE PRODUCER OF A RECORD THE BROWSER SEES, shared with the save
        # door below. It also answers which of these are the same bill, where
        # the complaint was made: he read this card and said "some of them were
        # dupes or unknown to me", and every fact needed to tell him which was
        # already reachable from here.
        view = world.client_rows()
        linked = accounts.join(view["accounts"], world.joinable())
        # THE PAGE NEVER RECEIVES AN UNDESCRIBED ACCOUNT'S WHOLE NAME. Those
        # names are the provider's own and on this host are mostly addresses,
        # and the card renders them — so the masking has to happen where the
        # data is minted rather than in a renderer that can forget. What the
        # page gets instead is the masked spelling to print and the opaque
        # handle to post back; `name` stays in `join` for the CLI's `--json`,
        # which is the machine surface the join reads.
        linked = dict(linked, measured_only=[
            {k: v for k, v in m.items() if k != "name"}
            for m in linked["measured_only"]])
        return dict(view, join=linked,
                    measured_unavailable=world.why(),
                    cap=accounts.MAX_ACCOUNTS, owner_only=accounts.OWNER_ONLY)
    except Exception as exc:
        # THE CLASS NAME ONLY, on this side too. `accounts.read`'s own failures
        # name the inventory file, which is a path on his disk, and this string
        # renders on his page.
        return {"accounts": [], "bad": [], "totals": None,
                "unreadable": "the declared inventory could not be read (%s)"
                              % type(exc).__name__}


def _status(code):
    """The writer's code as an HTTP status. `conflict`, `gone` and `stale` all
    say the inventory changed under this tab — the first two before the write,
    the third between the write and the read that should have confirmed it —
    and the card reloads on each; everything else is a refusal of what he
    typed, which is a 400."""
    return 409 if code in ("conflict", "gone", "stale") else 400


def _api_accounts_post(payload):
    """save | remove — the owner's own edits, through accounts.py's one door."""
    from . import accounts
    # THE SAME WORLD BOTH HALVES OF THIS REQUEST SEE — the handle resolution
    # before the write and the re-read after it. Asked twice, they can be
    # asked either side of a TTL lapse, which is how the page came to render
    # one subscription and save another.
    world = _world()
    act = payload.get("action")
    revision = payload.get("revision")
    # PROVENANCE IS HELM'S, NEVER THE FORM'S. `seeded_from` records where a row
    # came from — the seeder writes it and nothing else may. The page never
    # carries the field, so this refuses only a payload somebody built by hand,
    # and it refuses rather than dropping it silently: a value accepted and
    # discarded is the other way to be wrong about who owns a field.
    reserved = "seeded_from"
    if act == "save":
        row = payload.get("account")
        row = row if isinstance(row, dict) else {}
        if reserved in row:
            return {"error": "helm records where a row came from itself — "
                             "'%s' is not editable here." % reserved,
                    "code": "refused"}, 400
        row, gone = world.resolve_measured(row)
        if gone:
            return {"error": gone, "code": "refused"}, 400
        # WHICH ROW THE FORM OPENED, beside the revision and for the same
        # reason: both say what the editor was looking at when he started. An
        # add carries neither.
        out, err, code = accounts.save(row, expected_revision=revision,
                                       original_id=payload.get("original_id"))
        if err:
            return {"error": err, "code": code}, _status(code)
        # THE SAME PRODUCER THE GET USES, never `out` on its own. `save`
        # answers the stored projection, which is one row and cannot carry the
        # fields derived against the measured world — and the page GROUPS on
        # one of those. Answered from `save` alone, a record created by typing
        # in a cell came back unkeyed, split off as a second row for an account
        # that already had one, and left the row he was typing in blank while
        # the next cell created a third.
        # THE WRITE HAS LANDED BY HERE, AND NOTHING BELOW MAY UNSAY IT. An
        # unguarded re-read raises through this handler — an OSError out of
        # the provider will do it — and the page is then answered with a 500
        # carrying the exception text, paints an X and skips its reload. He
        # ticked the box, helm wrote it, and helm told him it had not taken.
        #
        # The measured half can no longer reach here: the world was acquired
        # at the top of this handler and cannot raise. What remains is the
        # declared read, and its failure is `stale` — the code that already
        # means "your change went through and the screen has stopped
        # describing your inventory", which declSave, declConfirm and the fill
        # path all read as landed and reload on.
        try:
            view = world.client_rows()
        except Exception as exc:
            return {"error": "your change was saved, but helm could not read "
                             "the accounts list back just now (%s), so the "
                             "screen is no longer describing your inventory. "
                             "Reloading." % type(exc).__name__,
                    "code": "stale"}, 409
        saved = next((r for r in view["accounts"] if r["id"] == out["id"]), None)
        if saved is None:
            # A MISS HERE IS A CONTRADICTION, NOT A CASE TO DEFAULT AROUND. The
            # write returned, so the record is on disk; a re-read that cannot
            # see it means the file moved underneath — another writer removed
            # it, or it stopped parsing and `read` fail-opened to an empty
            # inventory, which is right for a page that must still render and
            # wrong as an answer to a write.
            #
            # DEFAULTING TO `out` HERE WOULD HAND BACK THE EXACT SHAPE THE
            # LINES ABOVE EXIST TO ELIMINATE: unkeyed, so the page splits the
            # record off the row he typed it into and the next cell creates a
            # third — a degrade that re-opens the bug it guards, on the one
            # path nothing exercises. He is TOLD instead: the change landed,
            # and the screen is no longer describing his inventory.
            return {"error": "your change was saved, but the accounts list "
                             "changed underneath it and helm can no longer "
                             "see the row. Reloading so the screen matches "
                             "what is really there.",
                    "code": "stale", "revision": view["revision"]}, 409
        return {"ok": True, "account": saved,
                "revision": view["revision"]}, 200
    if act == "remove":
        ok, err, code = accounts.remove(payload.get("id") or "",
                                        expected_revision=revision)
        if err:
            return {"error": err, "code": code}, _status(code)
        # THE REMOVE HAS LANDED BY HERE, the same as the save above, and the
        # same law binds: nothing below may unsay it. This read exists only to
        # hand the page its next revision. Raising through the handler answers
        # a 500 for a removal that is already on disk, and his next move is to
        # remove a row that is no longer there. `stale` is the code the page
        # reads as landed-and-reload.
        try:
            after = accounts.read()["revision"]
        except Exception as exc:
            return {"error": "the account was removed, but helm could not read "
                             "the accounts list back just now (%s), so the "
                             "screen is no longer describing your inventory. "
                             "Reloading." % type(exc).__name__,
                    "code": "stale"}, 409
        return {"ok": ok, "revision": after}, 200
    return {"error": "unknown action %r (save | remove)" % (act,),
            "code": "refused"}, 400
