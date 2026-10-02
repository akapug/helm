"""The owner's Orca floor: each fleet seat's tab sits in the pane its state names.

THE FLOOR PLAN. The owner watches his fleet in one Orca workspace split into
panes side by side. Pane 1 holds his lead and the seats whose provider is
walling them, pane 2 the fleet seats that are working, pane 3 the seats served
from his own GPU. A tab belongs in another pane each time its seat walls or
recovers. This module makes that move on the timer that already sweeps every
seat's pane (`helm seat resume --all`), and `helm seat cubicles` shows the
plan and makes it on demand.

WHAT ORCA OFFERS (read from orca 1.4.218's CLI schema and its app bundle):
  * The CLI has no verb that moves a tab between panes. `terminal split`
    makes a NEW terminal; nothing moves, reparents or regroups one.
  * The runtime RPC helm already speaks (`harness.OrcaAdapter.rpc`, the
    authenticated Unix socket, switched off by HELM_ORCA_RPC) carries
    `session.tabs.move`. Its kind "move-to-group" moves one tab into another
    tab group of the SAME workspace, and `terminal.list` with
    `includeVisualLayouts` returns each workspace's groups as a split tree.
    A move keeps the tab's terminal, so its handle, its pane key and its
    title are unchanged.
  * The one per-tab channel besides the title is the tab colour
    (`session.tabs.setTabProps`). While a desktop window is open, that call
    answers updated:true and writes nothing, because the window owns tab
    props. It cannot put a mood on the owner's screen, so this module never
    calls it. The title stays the seat name, byte-exact (helm/orcatitle.py),
    and this module never renames a tab.

WHICH TABS MOVE. Only a tab helm names through a seat's SPAWN REGISTER (the
HELM_SPAWNED rung of `orcaadopt.pane_rows`) whose seat name is a catalog
family or a numbered instance of one. So the owner's lead, a hand-launched
seat and a tab two registers claim stay where he put them, by construction.
Naming reads no /proc, which keeps the sweep's healthy-fleet cost.

WHERE A SEAT GOES. A seat of a locally served family goes to pane 3 and its
liveness is not read. Any other seat reads its liveness, read-only
(`seat.seat_liveness(repair=False)`): WALLED or BLOCKED_ON_QUOTA goes to pane
1, UNKNOWN moves nothing, and every other state goes to pane 2.

THE FLOOR KEEPS ITS SHAPE. Panes are counted in layout order (a split's first
side before its second), and only the first three are cubicles. Orca closes a
pane when its last tab leaves it, which would renumber the floor, so a move
that would empty a pane is held. A missing pane is reported and never made. A
seat in a fourth pane stays: that is the owner's own placement.

ONE READING IS NOT A MOVE. The timer moves a tab only when two passes in a row
want the same pane for it, so a pane tail that flickers does not throw a tab
back and forth. A hand-run `--apply` moves on one reading.

A MOVE IS CHECKED BY ITS EFFECT. Orca answers moved:true once it has told the
window. The layout is read again, and a tab that is not yet in its new pane is
reported as sent, not moved. Orca's drop rule shows the moved tab in its new
pane, as a hand drag does.

FAILURE IS NEVER FATAL: every failure is a row or a line, never an exception
into the sweep.
"""
import importlib
import json as _json
import os
import sys as _sys
import time

from . import home

SWITCH_KEY = "cubicle-mover"
FLOOR_KEY = "cubicle-floor"
ON, OFF, DRY = "on", "off", "dry-run"

WALLED, WORKING, LOCAL = "walled", "working", "local"
#: The cubicle (0-based, in layout order) each placement sits in.
PANE = {WALLED: 0, WORKING: 1, LOCAL: 2}
CUBICLES = 3
#: Liveness states that say the seat's provider is refusing it.
WALLED_STATES = ("WALLED", "BLOCKED_ON_QUOTA")
#: Liveness states that say nothing about the seat: they move nothing.
UNREAD_STATES = ("UNKNOWN",)

#: Passes in a row that must want one pane before the timer moves a tab.
CONFIRM_PASSES = 2
READBACK_TRIES = 4
READBACK_PAUSE_S = 0.5
STATE_NAME = "cubicles.json"

STAYS = "stays"
WOULD = "would move"
WAITS = "waits"
HELD = "held"
MOVED = "moved"
SENT = "sent"
FAILED = "not moved"

_USAGE = "seat cubicles [--json] [--apply|--dry-run]"
HEADER = "%-16s %-8s %-10s %-4s %-4s %s" % ("SEAT", "PLACE", "MOOD", "NOW",
                                            "WANT", "ACTION")


# ------------------------------------------------------------------ switch

def switch():
    """ON, OFF or DRY, from local-names `cubicle-mover`. Unset is ON, and a
    word the switch does not know plans only."""
    from . import localnames
    word = (localnames.value(SWITCH_KEY) or ON).strip().lower()
    return word if word in (ON, OFF, DRY) else DRY


def floor():
    """The Orca workspace path whose panes are the cubicles, or None."""
    from . import localnames
    raw = localnames.value(FLOOR_KEY)
    return os.path.realpath(os.path.expanduser(raw)) if raw else None


def switch_state():
    """(ok, the sentence `helm doctor` prints)."""
    from . import localnames
    word, where = switch(), floor()
    if word == OFF:
        return False, ("cubicle mover: OFF (local-names %r is \"off\") — seat "
                       "tabs stay where they are" % SWITCH_KEY)
    if not where:
        return True, ("cubicle mover: no floor — local-names %r names the "
                      "Orca workspace whose first three panes are the "
                      "cubicles (1 walled, 2 working, 3 local); unset, no "
                      "tab moves" % FLOOR_KEY)
    if word == DRY:
        raw = (localnames.value(SWITCH_KEY) or "").strip().lower()
        said = ("is \"dry-run\"" if raw == DRY
                else "is %r, a word it does not know" % raw)
        return False, ("cubicle mover: DRY RUN (local-names %r %s) — the seat "
                       "resume tick plans moves on %s and makes none"
                       % (SWITCH_KEY, said, where))
    return True, ("cubicle mover: on — the seat resume tick moves each fleet "
                  "seat's tab to its pane on %s (1 walled, 2 working, 3 "
                  "local); local-names %r \"off\" stops it, \"dry-run\" plans "
                  "only" % (where, SWITCH_KEY))


# ------------------------------------------------------------------- floor

def _handles(node):
    """Every terminal handle in one tab's pane tree."""
    if not isinstance(node, dict):
        return []
    if node.get("handle"):
        return [node["handle"]]
    return _handles(node.get("first")) + _handles(node.get("second"))


def panes(root):
    """[(group_id, [(tab_id, [handle])])] — a workspace's tab groups in layout
    order, a split's first side before its second at every depth."""
    if not isinstance(root, dict):
        return []
    if root.get("type") == "group":
        return [(root.get("groupId"),
                 [(t["tabId"], _handles(t.get("panes")))
                  for t in root.get("tabs") or ()
                  if isinstance(t, dict) and t.get("tabId")])]
    return panes(root.get("first")) + panes(root.get("second"))


def read_floor(ad, where):
    """(worktree_id, [pane], why) — the floor workspace's panes, read over
    the runtime RPC. `why` is one line when it cannot be read."""
    if getattr(ad, "rpc", None) is None:
        return None, [], ("this metaharness has no runtime RPC, so the floor's "
                          "panes cannot be read")
    result, err = ad.rpc("terminal.list", {"includeVisualLayouts": True})
    if err:
        return None, [], err
    hits = [l for l in (result or {}).get("visualLayouts") or ()
            if isinstance(l, dict) and l.get("worktreePath")
            and os.path.realpath(l["worktreePath"]) == where]
    if not hits:
        return None, [], ("no Orca workspace at %s has a terminal tab open"
                          % where)
    if len(hits) > 1:
        return None, [], ("%d Orca workspaces sit at %s, so the floor is "
                          "ambiguous" % (len(hits), where))
    return hits[0].get("worktreeId"), panes(hits[0].get("root")), None


def registered():
    """{handle: seat} from every spawn register (`orcaadopt.helm_spawned`,
    the HELM_SPAWNED rung of `pane_rows`), labelled by identity as pane_rows
    labels it. A handle two registers claim names nobody."""
    from . import orcaadopt
    claims = {}
    for name, rec in orcaadopt.helm_spawned().items():
        handle = (rec or {}).get("handle")
        if handle:
            claims.setdefault(handle, set()).add(rec.get("identity") or name)
    return {h: next(iter(s)) for h, s in claims.items() if len(s) == 1}


def tab_seat(handles, named):
    """The one seat a tab's terminals name, else None."""
    seats = {named[h] for h in handles if h in named}
    return seats.pop() if len(seats) == 1 else None


# --------------------------------------------------------------- placement

def placement(seat_name):
    """(WALLED | WORKING | LOCAL | None, why). None moves nothing."""
    from . import burnflags, seat
    family, err = seat._named_seat_family(seat_name)
    if err:
        return None, ("not a catalog seat: the lead and hand-launched seats "
                      "stay where the owner put them")
    if family in burnflags.local_families():
        return LOCAL, "family %s is served locally" % family
    try:
        row = seat.seat_liveness(seat_name, repair=False) or {}
    except Exception as e:                  # noqa: BLE001 — a reason, never a crash
        return None, "liveness unread (%s)" % _one_line(e)
    state = row.get("state") or "UNKNOWN"
    if state in UNREAD_STATES:
        return None, "liveness %s: an unread state moves nothing" % state
    if state in WALLED_STATES:
        return WALLED, state + (" — %s" % row["blocked_on"]
                                if row.get("blocked_on") else "")
    return WORKING, state


def mood(seat_name):
    """The seat's mood word from helm/seatmood.py, or None while that module
    is absent or cannot say. Read for display only: placement never uses it."""
    try:
        # THROUGH sys.modules, not a package attribute, so the import seam is
        # the one a test (or a reload) controls.
        got = importlib.import_module(__package__ + ".seatmood").mood(seat_name)
    except Exception:                       # noqa: BLE001 — a blank column
        return None
    if isinstance(got, dict):
        got = got.get("mood")
    return got.strip() if isinstance(got, str) and got.strip() else None


class Row(object):
    """One named tab on the floor. `now` and `want` are 0-based panes."""

    __slots__ = ("seat", "tab", "now", "want", "place", "mood", "action",
                 "reason", "group")

    def __init__(self, seat, tab, now, place=None, mood=None):
        self.seat, self.tab, self.now = seat, tab, now
        self.place, self.mood = place, mood
        self.want = PANE.get(place)
        self.action, self.reason, self.group = STAYS, "", None

    def as_dict(self):
        def n(i):
            return None if i is None else i + 1
        return {"seat": self.seat, "tab": self.tab, "place": self.place,
                "mood": self.mood, "now": n(self.now), "want": n(self.want),
                "action": self.action, "reason": self.reason}

    def line(self):
        from .seats_common import _scrub, _seat_label

        def n(i):
            return "-" if i is None else str(i + 1)
        return "%-16s %-8s %-10s %-4s %-4s %s%s" % (
            _seat_label(self.seat), self.place or "-",
            _scrub(self.mood or "-")[:10], n(self.now), n(self.want),
            self.action, " — " + self.reason if self.reason else "")


def plan(floor_panes, named, place=placement, read_mood=mood):
    """[Row] for every named tab, with its action. Pure but for `place` and
    `read_mood`, which tests replace."""
    groups = [g for g, _tabs in floor_panes]
    left = [len(tabs) for _g, tabs in floor_panes]
    rows = []
    for i, (_g, tabs) in enumerate(floor_panes):
        for tab_id, handles in tabs:
            who = tab_seat(handles, named)
            if not who:
                continue
            if i >= CUBICLES:
                row = Row(who, tab_id, i, mood=read_mood(who))
                row.reason = ("pane %d is not a cubicle; the owner's placement "
                              "stands" % (i + 1))
                rows.append(row)
                continue
            where, why = place(who)
            row = Row(who, tab_id, i, where, read_mood(who))
            row.reason = why
            if row.want is None or row.want == i:
                rows.append(row)
                continue
            if row.want >= len(groups):
                row.action = HELD
                row.reason = ("%s; the floor has %d pane(s), pane %d is "
                              "missing and the mover never makes one"
                              % (why, len(groups), row.want + 1))
            elif left[i] <= 1:
                row.action = HELD
                row.reason = ("%s; it is the last tab in pane %d, and moving "
                              "it would close that pane" % (why, i + 1))
            else:
                row.action, row.group = WOULD, groups[row.want]
                left[i] -= 1
                left[row.want] += 1
            rows.append(row)
    return rows


def survey(ad, where):
    """(rows, why, worktree_id) for the floor at `where`."""
    wid, floor_panes, why = read_floor(ad, where)
    if why:
        return [], why, None
    return plan(floor_panes, registered()), None, wid


# ------------------------------------------------------------------- moves

def state_path():
    return os.path.join(home.global_dir(), ".state", STATE_NAME)


def confirm(rows, now=None):
    """Hold each WOULD row as WAITS until CONFIRM_PASSES passes in a row want
    the same pane for its seat, and record the waits. A record that does not
    read starts every wait over; one that cannot be written is said."""
    from . import pk
    path = state_path()
    try:
        old = pk.read_json(path, {}) or {}
    except Exception:                       # noqa: BLE001 — start the waits over
        old = {}
    waiting = old.get("waiting") if isinstance(old, dict) else None
    waiting = waiting if isinstance(waiting, dict) else {}
    kept = {}
    for row in rows:
        if row.action != WOULD:
            continue
        seen = waiting.get(row.seat)
        seen = seen if isinstance(seen, dict) else {}
        passes = (seen.get("passes") or 0) + 1 \
            if seen.get("pane") == row.want else 1
        if passes < CONFIRM_PASSES:
            row.action = WAITS
            row.reason = "%s; pass %d of %d wanting pane %d" % (
                row.reason, passes, CONFIRM_PASSES, row.want + 1)
            kept[row.seat] = {"pane": row.want, "passes": passes,
                              "since": seen.get("since") or now}
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        pk.write_json(path, {"v": 1, "waiting": kept})
    except Exception as e:                  # noqa: BLE001 — the pass goes on
        return "the waiting record was not written (%s)" % _one_line(e)
    return None


def move(ad, worktree_id, rows):
    """Send each WOULD row's move. A refusal is a FAILED row with Orca's
    reason; an accepted one is SENT until `settle` sees it."""
    for row in rows:
        if row.action != WOULD:
            continue
        _result, err = ad.rpc("session.tabs.move", {
            "worktree": "id:%s" % worktree_id, "tabId": row.tab,
            "targetGroupId": row.group, "kind": "move-to-group"})
        if err:
            row.action, row.reason = FAILED, _one_line(err)
        else:
            row.action = SENT


def settle(ad, where, rows):
    """Re-read the floor until every SENT tab sits in its new pane, a few
    times. A tab seen there is MOVED; one never seen stays SENT."""
    sent = [r for r in rows if r.action == SENT]
    for _ in range(READBACK_TRIES):
        if not sent:
            return
        time.sleep(READBACK_PAUSE_S)
        _wid, now_panes, why = read_floor(ad, where)
        if why:
            continue
        at = {t: g for g, tabs in now_panes for t, _h in tabs}
        for row in sent:
            if at.get(row.tab) == row.group:
                row.action = MOVED
        sent = [r for r in sent if r.action == SENT]
    for row in sent:
        row.reason = ("Orca took the move, and the layout has not shown it "
                      "yet; the next pass reads it again")


def summary(rows, live):
    counts = {}
    for row in rows:
        counts[row.action] = counts.get(row.action, 0) + 1
    order = (MOVED, SENT, FAILED, WOULD, WAITS, HELD, STAYS)
    said = ", ".join("%d %s" % (counts[k], k) for k in order if counts.get(k))
    return "%s%s" % (said or "no fleet seat's tab is on the floor",
                     "" if live else " — DRY RUN, no tab moved")


def tick(ad, apply=False):
    """[line] for the seat resume sweep. Silent with no floor or with the
    switch OFF. Moves only when the sweep writes (`apply`) and the switch is
    ON, after two agreeing passes. Never raises."""
    try:
        word, where = switch(), floor()
        if word == OFF or not where:
            return []
        live = bool(apply) and word == ON
        rows, why, wid = survey(ad, where)
        if why:
            return ["cubicle mover: %s" % why]
        lines = []
        if live:
            unwritten = confirm(rows, now=int(time.time()))
            if unwritten:
                lines.append("cubicle mover: %s" % unwritten)
            move(ad, wid, rows)
            settle(ad, where, rows)
        lines += ["cubicle mover: " + r.line() for r in rows
                  if r.action != STAYS]
        return lines + ["cubicle mover: %s" % summary(rows, live)]
    except Exception as e:                  # noqa: BLE001 — never fail the sweep
        return ["cubicle mover: the pass failed (%s)" % _one_line(e)]


def _one_line(err):
    from .orcatitle import one_line
    return one_line(err)


# --------------------------------------------------------------------- CLI

def cmd_cubicles(rest):
    """seat cubicles [--json] [--apply|--dry-run] — the floor plan, and the
    moves on --apply. A dry run by default; --dry-run wins over --apply. A
    hand-run --apply moves on one reading and ignores the switch, which
    governs only the timer."""
    from .cli import guard_tail
    rc = guard_tail("helm seat cubicles", rest,
                    flags=("--json", "--apply", "--dry-run"), usage=_USAGE)
    if rc is not None:
        return rc
    apply = "--apply" in rest and "--dry-run" not in rest
    where = floor()
    if not where:
        print("helm seat cubicles: no floor — set local-names %r to the Orca "
              "workspace path whose first three panes are the cubicles"
              % FLOOR_KEY, file=_sys.stderr)
        return 1
    from . import harness
    ad = harness.detect()
    if ad is None or getattr(ad, "name", None) != "orca":
        print("helm seat cubicles: no orca metaharness detected — the floor "
              "is an Orca workspace", file=_sys.stderr)
        return 1
    rows, why, wid = survey(ad, where)
    if why:
        print("helm seat cubicles: %s" % why, file=_sys.stderr)
        return 1
    if apply:
        move(ad, wid, rows)
        settle(ad, where, rows)
    failed = any(r.action == FAILED for r in rows)
    if "--json" in rest:
        print(_json.dumps({"floor": where, "switch": switch(), "apply": apply,
                           "rows": [r.as_dict() for r in rows]},
                          indent=2, sort_keys=True))
        return 1 if failed else 0
    print(HEADER)
    for row in rows:
        print(row.line())
    print("helm seat cubicles: %s (floor %s; the timer's switch is %s)"
          % (summary(rows, apply), where, switch()))
    return 1 if failed else 0
