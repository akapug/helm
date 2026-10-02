#!/usr/bin/env python3
"""helm friction autopilot: a guard that keeps refusing gets ONE task row,
filed, counted, raised and re-checked by helm itself (task/3899).

THE FAILURE THIS ENDS. The friction ledger (helm/friction.py) counts every
guard refusal, and nothing acts on the count unless a seat chooses to read
it. A wrong guard then costs every seat that meets it, and the cost reaches
a row only when a supervisor reads the friction by hand. This pass reads it
on the idle-dispatch tick, with no seat in the loop.

THE CAUSE KEY (`cause_key`). A ledger row carries a guard token and a reason
token. The guard is lower-cased but otherwise kept distinct (guard1 and guard2
are different guards). The reason is normalized: the row's own seat and
session become "seat", a date or a time "time", a path "path", a hex run that
holds a digit "sha", and any other digit run "n". One cause keeps one key,
whoever met it and whenever.

WHAT COUNTS. The ledger records a refusal and never what the seat did next,
so it cannot tell a refusal the seat routed around from one that stopped a
mistake. Every refusal is counted, and the row says so. The bar is the
narrowing: a cause crosses only on REPEAT (HITS_BAR refusals inside
WINDOW_S) or SPREAD (SEATS_BAR seats), so a mistake a guard stopped once
stays under it. `retried` counts the refusals that met a seat again on the
same cause inside RETRY_S, the one routed-around sign the ledger carries. A
steward who closes the row with a reason that starts "guard-correct" rules
the refusals stopped mistakes: the cause is retired and is not filed again.

PRIORITY. P1, or P0 when the cause blocks lands (its reason is a land-path
hook, LAND_PATH, or the stopped auto-land train's reason names its guard) or
reaches SEATS_BAR seats. A new row is born at its priority. Raising a row
already filed is a rank change, and tasks.update records who ranked it. A
pass that an admitted seat runs raises the row as that seat. The timer's pass
has no seat, so it raises as helm itself: `actors.grant_system_rank(SYSTEM,
<the rows this state maps>)` mints a capability that tasks.update admits for
one change only, a row in that map whose source is POSTER, from P1 to P0, and
`ranked_by` reads "system:frictionpilot". A raise writes one comment and one
#seats post either way. A row the capability refuses (a row a seat filed and
this pass adopted, or one a seat ranked away from P1) is not raised: the
raise is commented as owed, once, and its #seats post names the command.

ONE ROW PER CAUSE. The state file (STATE_NAME, written under its lock, the
shape of seatevents' ledger) maps each cause key to its row, and the row
carries the ref "friction-cause:<key>", so a lost state file finds the open
row again and files no second one. New refusals add one counted comment to
that row at most once per BUMP_EVERY_S. Every comment this pass writes
starts with MARK, and the task sweep reads it as a machine note, never as
motion on the row (taskhygiene._machine_note).

THE CURE IS MEASURED. When a cause's row closes, the pass records the hit
rate over the SETTLE_S before the pass saw the close. SETTLE_S later it
reads the rate after. If the rate has not fallen, the cause gets its row
back. The ledger never reopens a closed row (tasks.update: "file a row that
cites this one"), so the cause gets a new row that cites the closed one and
carries both rates, and the closed row gets a comment naming the new row.
A rate that fell is CURED, and a later crossing files a new row.

NOTHING PAGES THE OWNER. The one voice is #seats, through seatevents, with
the steward of the "helm-friction" component mentioned (local-names
`friction-steward-seat`). This module hands seatevents a phone leg that
sends nothing. `p0_causes()` is the read the office weather takes.

BOUNDED, AND NEVER RAISES INTO ITS HOST. A pass makes at most MAX_WRITES
task writes and leaves the rest for the next pass. `ride()` is the
idle-dispatch tick's entry: at most one pass per PASS_EVERY_S, and every
failure is a line. A dry run prints what the pass would do and writes
nothing.
"""
import fcntl
import json
import os
import re
import sys
import time

from . import home, pk, tickalarm

POSTER = "system:frictionpilot"
#: The subsystem name the timer's raise is minted for (actors.
#: SYSTEM_RANK_SUBSYSTEMS); its rows' source is POSTER.
SYSTEM = "frictionpilot"
COMPONENT = "helm-friction"
STEWARD_KEY = "friction-steward-seat"
MARK = "[helm/friction-autopilot:"
REF = "friction-cause:"
STATE_NAME = "friction-autopilot.json"
V = 1

#: The bar: HITS_BAR refusals or SEATS_BAR seats inside WINDOW_S.
WINDOW_S = 24 * 3600
HITS_BAR = 10
SEATS_BAR = 3
#: A seat refused again on one cause inside this is a seat that retried.
RETRY_S = 30 * 60
#: At most one counted comment per row inside this.
BUMP_EVERY_S = 24 * 3600
#: The window before and after a close that the cure is measured over.
SETTLE_S = 2 * 86400
#: The tick runs a pass at most this often; a hand-run pass always runs.
PASS_EVERY_S = 15 * 60
#: Task writes (a filing, a comment, a raise) in one pass.
MAX_WRITES = 5
#: Reason tokens whose refusal stops a lane on its way to a land.
LAND_PATH = ("pre-push", "pre-merge-commit")
GUARD_CORRECT = "guard-correct"
POSTURE_NA = ("a machine-filed friction cause: one of helm's own guards, "
              "no dependency seam")

_TIME = re.compile(r"\d{4}-\d\d-\d\d(?:t\d\d:\d\d(?::\d\d)?(?:\.\d+)?z?)?"
                   r"|\d\d:\d\d(?::\d\d)?")
_PATH = re.compile(r"[^\s|]*/[^\s|]*")
_SHA = re.compile(r"(?<![0-9a-z])(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])"
                  r"[0-9a-f]{7,64}(?![0-9a-z])")
_NUM = re.compile(r"\d+")
_STAMP = "%Y-%m-%dT%H:%M:%SZ"


def state_path():
    return os.path.join(home.helm_home(), home.GLOBAL, ".state", STATE_NAME)


def _iso(epoch):
    return time.strftime(_STAMP, time.gmtime(epoch))


# ------------------------------------------------------------------ the key

def _norm(text, seat=None, session=None):
    """One token, normalized. The row's own seat and session fold only out
    of a REASON: a guard's name is the component's identifier, and a seat
    that happened to share a word with it must not split its cause."""
    text = str(text or "").strip().lower()
    for who in (seat, session):
        who = str(who or "").strip().lower()
        if len(who) >= 3:
            text = re.sub(r"(?<![a-z0-9])%s(?![a-z0-9])" % re.escape(who),
                          "seat", text)
    text = _PATH.sub("path", _TIME.sub("time", text))
    return _NUM.sub("n", _SHA.sub("sha", text))


def cause_key(guard, reason=None, seat=None, session=None):
    """The stable key of one refusal's cause: '<guard>|<reason>'. The guard
    stays distinct; the reason folds volatile details (see the module docstring)."""
    return "%s|%s" % (str(guard or "").strip().lower() or "-",
                      _norm(reason, seat, session) or "-")


def is_receipt(c):
    """Is task comment `c` one this pass wrote?"""
    return isinstance(c, dict) and \
        str(c.get("text") or "").startswith(MARK)


# ------------------------------------------------------------ the reading

def census(rows, now):
    """{key: cause} from friction ledger rows. A cause holds the guard and
    reason it was first seen with and every hit as (epoch, seat), oldest
    first. A row with no guard or no time is not a hit."""
    from . import friction
    out = {}
    for r in rows:
        guard, at = friction._token(r.get("guard")), friction._epoch(r.get("ts"))
        if not guard or at is None or at > now:
            continue
        seat = friction._token(r.get("seat"))
        key = cause_key(guard, r.get("reason"), seat, r.get("session"))
        cause = out.setdefault(key, {"key": key, "guard": guard, "hits": [],
                                     "reason": friction._token(r.get("reason"))})
        cause["hits"].append((at, seat))
    for cause in out.values():
        cause["hits"].sort(key=lambda h: h[0])
    return out


def _names(guard, text):
    """Does the stop text name this guard as a word? An EMPTY guard names
    nothing: `cause_key` spells a missing guard "-", and "-" as a word would
    match any bare " - " in the stop text."""
    guard = str(guard or "").strip()
    return bool(guard and guard != "-" and text and re.search(
        r"(?<![A-Za-z0-9_-])%s(?![A-Za-z0-9_-])" % re.escape(guard), text,
        re.I))


def judge(cause, now, stop_text=""):
    """The cause's reading over the last WINDOW_S: hits, seats, retried,
    whether it crosses the bar, its priority and why."""
    cause = cause or {}
    hits = [(at, s) for at, s in cause.get("hits", ()) if now - WINDOW_S < at <= now]
    seats = sorted({s for _at, s in hits if s})
    last, retried = {}, 0
    for at, s in hits:
        if s and s in last and at - last[s] <= RETRY_S:
            retried += 1
        if s:
            last[s] = at
    reason = (cause.get("reason") or "").lower()
    why = []
    if reason in LAND_PATH:
        why.append("it refuses %s, which a lane needs on its way to a land"
                   % reason)
    if _names(cause.get("guard"), stop_text):
        why.append("the stopped auto-land train names it")
    if len(seats) >= SEATS_BAR:
        why.append("%d seats met it" % len(seats))
    return {"hits": len(hits), "seats": seats, "retried": retried,
            "crossing": len(hits) >= HITS_BAR or len(seats) >= SEATS_BAR,
            "priority": "P0" if why else "P1", "why": "; ".join(why)}


def rate(cause, lo, hi):
    """Refusals a day of `cause` inside [lo, hi)."""
    n = sum(1 for at, _s in (cause or {}).get("hits", ()) if lo <= at < hi)
    return round(n * 86400.0 / max(hi - lo, 1.0), 2)


def land_stop_text():
    """The stopped auto-land train's reason, or "" (none stopped, unread)."""
    try:
        from . import autoland, work
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        st, _why = autoland.active(work.find_root(here) or here)
        if isinstance(st, dict) and st.get("state") == autoland.STOPPED:
            return str((st.get("stopped") or {}).get("why") or "")
    except Exception:                        # noqa: BLE001 — unread is ""
        pass
    return ""


# ------------------------------------------------------------- the state

def _load(path):
    got = pk.read_json(path, default={})
    got = got if isinstance(got, dict) and got.get("v") == V else {}
    causes = got.get("causes") if isinstance(got.get("causes"), dict) else {}
    last = got.get("last_pass")
    return {"v": V,
            "last_pass": last if isinstance(last, (int, float)) else None,
            "causes": {k: dict(v) for k, v in causes.items()
                       if isinstance(v, dict)},
            "owed_events": [e for e in got.get("owed_events") or ()
                            if isinstance(e, dict)]}


def p0_causes(path=None):
    """[{key, row, count, seats}] for each open cause whose rule reads P0
    (filed P0, or raised or owed a raise to P0). NEVER RAISES: [] when the
    state cannot be read."""
    try:
        got = _load(path or state_path())["causes"]
    except Exception:                        # noqa: BLE001 — a reader's []
        return []
    return [{"key": key, "row": e.get("row"), "count": e.get("hits", 0),
             "seats": e.get("seats", 0)}
            for key, e in sorted(got.items())
            if e.get("status") == "open" and e.get("row")
            and "P0" in (e.get("priority"), e.get("owed_raise"))]


# --------------------------------------------------------------- the pass

def _no_push(_body, _title):
    """The phone leg handed to seatevents: it sends nothing, so an owed push
    of another detector's event stays owed for that detector."""
    return False


class _Pass(object):
    def __init__(self, causes, now, apply, admit, stop_text):
        self.causes, self.now, self.apply = causes, now, apply
        self.admit, self.stop_text = admit, stop_text
        self.writes, self.events, self.actor, self.snap = 0, [], False, None
        self.mapped = {}
        self.rep = {"dry_run": not apply, "unreadable": None, "owed": 0,
                    "problems": [], "causes": [], "met": 0}
        for k in ("filed", "adopted", "bumped", "raised", "raise_owed",
                  "closed", "reopened", "cured", "retired"):
            self.rep[k] = []

    # ---- reads
    def judged(self, key):
        return judge(self.causes.get(key), self.now, self._stop())

    def _stop(self):
        if self.stop_text is None:
            self.stop_text = land_stop_text()
        return self.stop_text

    def rows(self):
        if self.snap is None:
            from . import tasks
            got, why = tasks.snapshot(strict=True)
            if why:
                raise _Unread("the task ledger would not read (%s)" % why)
            self.snap = got
        return self.snap

    # ---- writes
    def room(self, n=1):
        if self.writes + n > MAX_WRITES:
            self.rep["owed"] += 1
            return False
        self.writes += n
        return True

    def add(self, title, note, refs, priority):
        if not self.apply:
            return {"id": "(new)"}, None
        from . import tasks
        return tasks.add(title, None, note=note, refs=refs, source=POSTER,
                         origin="agent", priority=priority,
                         project=tasks.OWN_PROJECT, force_new=True,
                         posture_na=POSTURE_NA)

    def comment(self, tid, kind, text):
        if not self.apply:
            return None
        from . import tasks
        _row, err = tasks.comment(tid, "%s %s] %s" % (MARK, kind, text),
                                  by=None)
        return err

    def event(self, key, kind, ident, body):
        from . import seatevents
        self.events.append(seatevents.event(COMPONENT, key, kind, ident, body))

    def actor_for_rank(self):
        """The seat that runs this pass when it is admitted; else helm's own
        system rank capability over the rows this state maps (the timer's
        pass has no seat); None when neither mints."""
        if self.actor is False:
            got = self.admit() if self.admit else (None, "no seat")
            self.actor = got[0] if got and not got[1] else None
        if self.actor is None:
            from . import actors
            cap, _err = actors.grant_system_rank(
                SYSTEM, [e.get("row") for e in self.mapped.values()
                         if e.get("status") == "open"])
            self.actor = cap
        return self.actor

    # ---- the steps
    def tend(self, key, e):
        """A mapped cause: its row closed, cured, reopened, raised, bumped."""
        row = self.rows().get(e.get("row"))
        if not isinstance(row, dict):
            self.rep["problems"].append(
                "%s: its row %s is not in the task ledger; the next crossing "
                "files a new one" % (key, e.get("row")))
            e.update(row=None, status="cured")
            return
        if e.get("status") == "open" and row.get("status") == "closed":
            reason = str(row.get("closed_reason") or "").strip().lower()
            if reason.startswith(GUARD_CORRECT):
                e.update(status="retired", retired_at=self.now)
                self.rep["retired"].append({"key": key, "row": e["row"]})
                return
            before = rate(self.causes.get(key), self.now - SETTLE_S, self.now)
            e.update(status="closed", closed_at=self.now, rate_before=before)
            self.rep["closed"].append({"key": key, "row": e["row"],
                                       "rate_before": before})
            return
        if e.get("status") == "closed":
            return self.measure(key, e)
        if e.get("status") == "open":
            j = self.judged(key)
            e.update(hits=j["hits"], seats=len(j["seats"]))
            if j["priority"] == "P0" and row.get("priority") != "P0" \
                    and e.get("priority") != "P0" and j["hits"]:
                self.raise_p0(key, e, j, row)
            self.bump(key, e, j)

    def measure(self, key, e):
        closed_at = e.get("closed_at") or self.now
        if self.now < closed_at + SETTLE_S:
            return
        before = e.get("rate_before") or 0.0
        after = rate(self.causes.get(key), closed_at, closed_at + SETTLE_S)
        if after > 0 and after >= before:
            return self.reopen(key, e, before, after)
        e.update(status="cured", cured_at=self.now, rate_after=after,
                 history=list(e.get("history") or ()) + [e["row"]])
        self.rep["cured"].append({"key": key, "row": e["row"],
                                  "rate_before": before, "rate_after": after})

    def raise_p0(self, key, e, j, row):
        tid, actor = e["row"], self.actor_for_rank()
        refused = _refusal(actor, row)
        if refused is None:
            return self.apply_raise(key, e, j, actor, row)
        if e.get("owed_raise") == "P0" or not self.room():
            return
        err = self.comment(tid, "raise owed", (
            "This cause now reads P0 (%s): %d refusals by %d seat(s) in the "
            "last 24h. helm's own raise is refused (%s), so a seat applies "
            "it: `helm task update %s --priority P0`."
            % (j["why"], j["hits"], len(j["seats"]), refused, tid)))
        if err:
            self.rep["problems"].append("%s: %s" % (key, err))
            return
        e["owed_raise"] = "P0"
        self.rep["raise_owed"].append({"key": key, "row": tid, "why": j["why"]})
        self.event(key, "raise-owed", tid + ":P0", (
            "helm friction: %s now reads P0 (%s). Raise it: `helm task update "
            "%s --priority P0`." % (tid, j["why"], tid)))

    def apply_raise(self, key, e, j, actor, row):
        """The raise as `actor`: the rank, one comment, one #seats post."""
        tid, by = e["row"], _ranker(actor)
        was = row.get("priority") or "unranked"
        if not self.room(2):
            return
        if self.apply:
            from . import tasks
            _row, err = tasks.update(tid, rank_actor=actor, priority="P0")
            if err:
                self.rep["problems"].append("%s: raise of %s refused: %s"
                                            % (key, tid, err))
                return
        err = self.comment(tid, "raised", (
            "Raised from %s to P0 by %s: this cause now reads P0 (%s), %d "
            "refusals by %d seat(s) in the last 24h."
            % (was, by, j["why"], j["hits"], len(j["seats"]))))
        if err:
            self.rep["problems"].append("%s: %s" % (key, err))
        e.update(priority="P0", owed_raise=None)
        self.rep["raised"].append({"key": key, "row": tid, "why": j["why"],
                                   "by": by})
        self.event(key, "raised", tid + ":P0", "helm friction: %s raised "
                   "to P0 by %s: %s." % (tid, by, j["why"]))

    def bump(self, key, e, j):
        since = e.get("bumped_at") or e.get("filed_at") or 0
        new = sum(1 for at, _s in (self.causes.get(key) or {}).get("hits", ())
                  if since < at <= self.now)
        if not new or self.now - since < BUMP_EVERY_S or not self.room():
            return
        err = self.comment(e["row"], "count", (
            "%d more refusals since %s: %d in the last 24h by %d seat(s), %d "
            "of them a seat meeting it again within %d min."
            % (new, _iso(since), j["hits"], len(j["seats"]), j["retried"],
               RETRY_S // 60)))
        if err:
            self.rep["problems"].append("%s: %s" % (key, err))
            return
        e["bumped_at"] = self.now
        self.rep["bumped"].append({"key": key, "row": e["row"], "new": new})

    def file(self, key, e, j, refs_open):
        if key in refs_open:
            tid = refs_open[key]
            self.rep["adopted"].append({"key": key, "row": tid})
            return self.state_entry(key, e, tid, j)
        if not self.room():
            return None
        cause = self.causes[key]
        history = list((e or {}).get("history") or ())
        row, err = self.add(_title(cause), _note(cause, j, self.now),
                            [REF + key] + history, j["priority"])
        if err:
            self.rep["problems"].append("%s: not filed: %s" % (key, err))
            return None
        e = self.state_entry(key, e, row["id"], j)
        self.rep["filed"].append({"key": key, "row": row["id"],
                                  "priority": j["priority"], "hits": j["hits"],
                                  "seats": len(j["seats"]),
                                  "why": "(%s)" % j["why"] if j["why"] else ""})
        self.event(key, "filed", row["id"], _body(cause, j, row["id"]))
        return e

    def state_entry(self, key, e, tid, j):
        e = e if e is not None else {}
        e.update(row=tid, priority=j["priority"], status="open",
                 filed_at=self.now, bumped_at=self.now, owed_raise=None,
                 hits=j["hits"], seats=len(j["seats"]))
        return e

    def reopen(self, key, e, before, after):
        old = e["row"]
        if not self.room(2):
            return
        cause = self.causes[key]
        j = dict(self.judged(key), priority=e.get("priority") or "P1")
        history = list(e.get("history") or ()) + [old]
        rates = ("%.1f refusals a day in the %d h before %s closed, %.1f in "
                 "the %d h after" % (before, SETTLE_S // 3600, old, after,
                                     SETTLE_S // 3600))
        row, err = self.add(_title(cause), "The fix did not lower the hit "
                            "rate: %s. %s" % (rates, _note(cause, j, self.now)),
                            [REF + key] + history, j["priority"])
        if err:
            self.rep["problems"].append("%s: not refiled: %s" % (key, err))
            return
        self.comment(old, "cure not measured", "The hit rate did not fall "
                     "after this row closed (%s), so the cause has a new "
                     "row: %s." % (rates, row["id"]))
        e.update(history=history, closed_at=None, rate_before=None)
        self.state_entry(key, e, row["id"], j)
        self.rep["reopened"].append({"key": key, "row": row["id"], "was": old,
                                     "rate_before": before,
                                     "rate_after": after})
        self.event(key, "reopened", row["id"], (
            "helm friction: %s keeps refusing after %s closed (%s). New row "
            "%s." % (cause["guard"], old, rates, row["id"])))


class _Unread(Exception):
    pass


def _ranker(actor):
    """The name a raise is recorded under: `system:<subsystem>` for helm's
    own capability, the seat's canonical name for an admitted seat."""
    from . import actors
    return actor.ranker if isinstance(actor, actors.SystemRankCapability) \
        else actor.canonical_name


def _refusal(actor, row):
    """None when `actor` may raise `row` from P1 to P0, else why not. A seat's
    admission ranks any row; helm's own capability answers for itself.
    tasks.update asks both again against the row under its lock."""
    from . import actors
    if actor is None:
        return "no seat runs this pass and no system rank was minted"
    if isinstance(actor, actors.SystemRankCapability):
        return actor.refusal(row, {"priority": "P0"})
    return None


def _title(cause):
    return "helm friction: %s keeps refusing (%s)" % (
        cause["guard"], cause.get("reason") or "no reason recorded")


def _note(cause, j, now):
    return (
        "Filed by helm's friction autopilot (helm friction autopilot): cause "
        "%s crossed its bar at %s with %d refusals by %d seat(s) in the last "
        "24h, %d of them a seat meeting it again within %d min.%s The "
        "friction ledger records a refusal and never what the seat did next, "
        "so it cannot tell a refusal a seat routed around from one that "
        "stopped a mistake: every refusal is counted. Fix the guard, or make "
        "its refusal name the way out. When this row closes the autopilot "
        "records the hit rate, and if the rate has not fallen %d h later the "
        "cause gets a new row citing this one. If these refusals stop real "
        "mistakes, close this row with a reason that starts '%s:' and the "
        "cause is retired. The refusals: `helm friction --seat`."
        % (cause["key"], _iso(now), j["hits"], len(j["seats"]), j["retried"],
           RETRY_S // 60, (" P0: %s." % j["why"]) if j["why"] else "",
           SETTLE_S // 3600, GUARD_CORRECT))


def _body(cause, j, tid):
    return ("helm friction: %s keeps refusing (%s): %d refusals by "
            "%d seat(s) in 24h. Filed %s %s%s." % (
                cause["guard"], cause.get("reason") or "no reason", j["hits"],
                len(j["seats"]), j["priority"], tid,
                " (%s)" % j["why"] if j["why"] else ""))


def _open_refs(rows):
    """{cause key: open row id} for every open row carrying a cause ref."""
    from . import tasks
    out = {}
    for r in sorted(rows.values(), key=tasks.sort_key):
        if isinstance(r, dict) and r.get("status") in tasks.OPEN_STATUSES:
            for ref in r.get("refs") or ():
                if isinstance(ref, str) and ref.startswith(REF):
                    out.setdefault(ref[len(REF):], r.get("id"))
    return out


def _step(p, state):
    mapped = p.mapped = state["causes"]
    for key, e in sorted(mapped.items()):
        if e.get("status") in ("open", "closed") and e.get("row"):
            p.tend(key, e)
    due = []
    for key in p.causes:
        e = mapped.get(key)
        if e is not None and e.get("status") in ("open", "closed", "retired"):
            continue
        j = p.judged(key)
        if j["crossing"]:
            due.append((j["priority"] != "P0", -j["hits"], key, j))
    if due:
        refs_open = _open_refs(p.rows())
        for _p0, _n, key, j in sorted(due):
            got = p.file(key, mapped.get(key), j, refs_open)
            if got is not None:
                mapped[key] = got
    met = [(p.judged(k), k) for k in sorted(p.causes)]
    met = sorted((m for m in met if m[0]["hits"]), key=lambda m: -m[0]["hits"])
    p.rep["met"] = len(met)
    for j, key in met[:10]:
        p.rep["causes"].append({"key": key, "hits": j["hits"],
                                "seats": len(j["seats"]),
                                "retried": j["retried"],
                                "crossing": j["crossing"],
                                "priority": j["priority"],
                                "row": (mapped.get(key) or {}).get("row")})


def _guarded(p, state):
    """`_step`, with a failure part-way made a problem line: what it already
    wrote is in `state` and is saved, so the next pass neither files it twice
    nor loses the post it owes."""
    try:
        _step(p, state)
    except _Unread as exc:
        p.rep["problems"].append(str(exc))
    except Exception as exc:                 # noqa: BLE001 — see docstring
        p.rep["problems"].append("the pass stopped part-way (%s: %s)"
                                 % (exc.__class__.__name__, exc))


def run(apply=True, post=True, now=None, admit=None, stop_text=None,
        chat_post=None, path=None):
    """One pass -> the report. `apply` False is the dry run: nothing is
    written, filed, commented or posted. `admit` returns (actor, err) for a
    rank change and is None on the timer. `stop_text` overrides the auto-land
    read; `chat_post` the #seats post leg (seatevents.announce's `post`)."""
    from . import friction
    now = time.time() if now is None else now
    rows, unreadable = friction.read()
    target = path or state_path()
    if unreadable:
        p = _Pass({}, now, apply, admit, stop_text)
        p.rep["unreadable"] = unreadable
        return p.rep
    p = _Pass(census(rows, now), now, apply, admit, stop_text)
    if not apply:
        _guarded(p, _load(target))
        return p.rep
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target + ".lock", "a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            state = _load(target)
            _guarded(p, state)
            state["last_pass"] = now
            state["owed_events"] = state["owed_events"] + p.events
            pk.write_json(target, state)
            if post:
                from . import seatevents
                delivered = seatevents.announce(state["owed_events"], now=now,
                                                post=chat_post, push=_no_push)
                if delivered["claimed"] and state["owed_events"]:
                    state["owed_events"] = []
                    pk.write_json(target, state)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    return p.rep


#: (report list, the word for a pass that did it, the word for a dry run,
#: the rest of the line)
_WORDS = (
    ("filed", "FILED", "WOULD FILE", "%(priority)s %(row)s for %(key)s: "
     "%(hits)d refusals, %(seats)d seat(s) %(why)s"),
    ("adopted", "ADOPTED", "WOULD ADOPT", "open row %(row)s for %(key)s"),
    ("bumped", "COUNTED", "WOULD COUNT", "%(new)d more refusals on %(row)s"),
    ("raised", "RAISED", "WOULD RAISE", "%(row)s to P0 as %(by)s: %(why)s"),
    ("raise_owed", "OWES A RAISE", "WOULD OWE A RAISE", "%(row)s to P0: "
     "%(why)s"),
    ("closed", "SAW CLOSED", "WOULD SEE CLOSED", "%(row)s: %(rate_before)s "
     "refusals a day before"),
    ("reopened", "REFILED", "WOULD REFILE", "%(key)s as %(row)s: %(was)s "
     "closed and the rate went %(rate_before)s -> %(rate_after)s a day"),
    ("cured", "CURED", "WOULD CURE", "%(key)s: %(rate_before)s -> "
     "%(rate_after)s refusals a day after %(row)s closed"),
    ("retired", "RETIRED", "WOULD RETIRE", "%(key)s: %(row)s closed "
     "guard-correct"),
)


def lines(rep, verbose=False):
    """The report as lines. Not verbose: only what was done and what went
    wrong, so a quiet tick prints nothing."""
    if rep.get("unreadable"):
        return ["friction autopilot: the friction ledger is UNREADABLE (%s); "
                "nothing was judged or written" % rep["unreadable"]]
    out = []
    if verbose:
        out.append("helm friction autopilot — %d cause(s) met in the last 24h, "
                   "%d over the bar (%d refusals or %d seats)%s"
                   % (rep["met"], len([c for c in rep["causes"]
                                       if c["crossing"]]),
                      HITS_BAR, SEATS_BAR,
                      "; DRY RUN, nothing written" if rep["dry_run"] else ""))
        for c in rep["causes"]:
            out.append("  %s %4d hits %2d seat(s) %3d retried  %s%s"
                       % (c["priority"] if c["crossing"] else "--", c["hits"],
                          c["seats"], c["retried"], c["key"],
                          "  " + c["row"] if c["row"] else ""))
    for name, done, would, fmt in _WORDS:
        for item in rep[name]:
            out.append("friction autopilot: %s %s" % (
                would if rep["dry_run"] else done, (fmt % item).strip()))
    if rep.get("owed"):
        out.append("friction autopilot: %d write(s) left for the next pass "
                   "(at most %d a pass)" % (rep["owed"], MAX_WRITES))
    for problem in rep["problems"]:
        out.append("friction autopilot: PROBLEM %s" % problem)
    return out


def ride(apply=True, now=None, path=None):
    """The idle-dispatch tick's entry -> lines. A pass that writes runs at
    most once per PASS_EVERY_S; a dry pass always runs. NEVER RAISES."""
    try:
        now = time.time() if now is None else now
        last = _load(path or state_path())["last_pass"]
        if apply and last is not None and 0 <= now - last < PASS_EVERY_S:
            return []
        out = lines(run(apply=apply, now=now, path=path))
    except Exception as exc:                 # noqa: BLE001 — never the tick
        return ["friction autopilot: FAILED (%s: %s)"
                % (exc.__class__.__name__, exc)] + tickalarm.record(
                    "frictionpilot", tickalarm.error_of(exc))
    tickalarm.record("frictionpilot")
    return out


USAGE = """usage: helm friction autopilot [--dry-run] [--json]
  One pass of the friction autopilot. Each guard refusal in the friction
  ledger gets a cause key (its guard and its reason, with seats, shas, paths,
  numbers and times normalized away). A cause that reaches %d refusals or %d
  seats in 24h gets ONE task row, unowned, in the helm project: P1, or P0
  when it reaches %d seats or blocks lands. Later refusals add one counted
  comment a day to that row. A raise to P0 is applied by this pass, as the
  seat that runs it or, on the timer, as helm itself (ranked_by
  system:frictionpilot, only for a row this pass filed, only P1 to P0), with
  one comment and one #seats post; a row helm may not raise is owed. When
  the row closes, the hit rate is measured: if it has not fallen %d h later
  the cause gets a new row citing the closed one; a close whose reason starts
  'guard-correct:' retires the cause. Each new row, raise and refile posts
  once to #seats, mentioning local-names `friction-steward-seat`. Nothing
  pages the owner. --dry-run prints what the pass would do and writes
  nothing. The idle-dispatch tick runs a pass at most every %d min.""" % (
    HITS_BAR, SEATS_BAR, SEATS_BAR, SETTLE_S // 3600, PASS_EVERY_S // 60)


def cmd(argv):
    from .cli import guard_tail
    argv = list(argv)
    if {"-h", "--help"} & set(argv) and \
            set(argv) <= {"-h", "--help", "--dry-run", "--json"}:
        print(USAGE)
        return 0
    rc = guard_tail("helm friction autopilot", argv,
                    flags=("--dry-run", "--json"), usage=USAGE.splitlines()[0])
    if rc is not None:
        return rc

    def admit():
        from . import tasks
        return tasks._admit("raise a friction cause's row to P0")
    try:
        rep = run(apply="--dry-run" not in argv, admit=admit)
    except Exception as exc:                 # noqa: BLE001 — a sentence
        print("helm friction autopilot: FAILED (%s: %s)"
              % (exc.__class__.__name__, exc), file=sys.stderr)
        return 1
    if "--json" in argv:
        print(json.dumps(rep, sort_keys=True))
    else:
        print("\n".join(lines(rep, verbose=True)))
    return 1 if rep["unreadable"] or rep["problems"] else 0
