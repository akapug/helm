#!/usr/bin/env python3
"""helm seat events — the #seats room: one event, two sinks (task/3876).

THE FAILURE THIS PREVENTS. A family walls and comes back an hour later. The
owner's phone hears both, through `notify.owner_push`. The seat that owns
credentials hears neither: proxywatch posted the edge to #helm as a machine
row addressed to nobody, and a machine row is pulled, never pushed
(`machine_senders`). The owner learns of the wall first and has to tell the
steward himself.

THE RULE. A seat event (a credential wall and its unblock, a codex reset, the
default Claude home's switch, a seat that stops answering, a local-serving
outage) is posted ONCE to #seats, and the row @mentions the STEWARD of the
component it concerns. An @mention wakes its seat in any room
(`seats_identity.deliverable`), so the steward's beacon rings the way the
owner's phone does. The event that drives the row is the one that drives the
phone push: there is no second detector.

THE STEWARDS, ONE DECLARED TABLE. `STEWARDS` names, for each component, where
its steward is declared. A seat name is this host's own data and the source
carries none (`localnames`, and the seat-literal rung of `hardcode`), so the
table points at the declaration, never at a name:

  credentials    local-names `cred-steward-seat`
  local-serving  local-names `local-operator-seat` (the seat that runs the
                 local families' hardware, `burnflags.local_operator`)
  build-lanes    the integrator (`seats_integrator.integrator_seat`)
  project-seats  that project's team lead (`teams.read`)
  helm-friction  local-names `friction-steward-seat` (the friction
                 autopilot's rows, helm/frictionpilot.py)
  helm-ticks     local-names `tick-steward-seat` (a tick leg that keeps
                 failing, helm/tickalarm.py)

A component not in the table, or a declaration that names nobody, posts the
row with no mention and says where its steward should be declared.

DEDUP, PER EPISODE. `announce` keeps a small ledger. An event is keyed on its
detector's own identity (proxywatch's transition key, the switch instant),
so a detector that re-offers an event it has not seen acknowledged posts it
once. An OPENING event (a wall, an outage) opens its key's episode, and a
second opening event on an open key posts nothing. A CLOSING event (an
unblock, a recovery, a reset, a stand-down) closes it. Each channel is owed
until it delivers, and an owed channel is retried on the next call, so a
chat node that is down does not re-push the phone, and a failed push does
not re-post the room. A detector with its own latch (codexpace's walls,
beacons' attendance register) posts through `post` and keeps that latch.
"""
import fcntl
import hashlib
import os
import re
import time

from . import home, pk

ROOM = "seats"
WHO = "seat-events"
LEDGER_NAME = "seat-events.json"
V = 1

#: component -> (where its steward is declared, the key there).
STEWARDS = {
    "credentials": ("local-names", "cred-steward-seat"),
    "local-serving": ("local-names", "local-operator-seat"),
    "build-lanes": ("integrator", None),
    "project-seats": ("lead", None),
    "helm-friction": ("local-names", "friction-steward-seat"),
    "helm-ticks": ("local-names", "tick-steward-seat"),
}

OPENS = frozenset(("wall", "outage"))
CLOSES = frozenset(("unblock", "recovered", "reset", "stood-down"))
#: The closing events that ask their steward for nothing: a family came back.
#: Their row says so right after the mention (FYI_LEAD), and the wake
#: classifier (helm/needs_act.py) holds such a row for the steward's next
#: tool boundary instead of waking it. The room and the phone still carry it.
RECOVERIES = frozenset(("unblock", "recovered"))
FYI_LEAD = "FYI, nothing to do: "
_FYI_ROW = re.compile(r"(?:@[A-Za-z0-9._-]+\s+)*" + re.escape(FYI_LEAD))

#: A delivered row, and an episode nothing closed, are kept this long.
KEEP_S = 7 * 86400

_SEAT = home._SEAT_NAME_RE


def ledger_path():
    return os.path.join(home.helm_home(), home.GLOBAL, ".state", LEDGER_NAME)


# ------------------------------------------------------------------ stewards

def steward(component, project=None):
    """(seat, None) for the component's declared steward, else (None, where
    it should be declared). Resolved when the row is sent, never cached."""
    where, key = STEWARDS.get(component, (None, None))
    if where is None:
        return None, ("%r is not a component in seatevents.STEWARDS; the "
                      "owner names its steward there" % component)
    try:
        if where == "local-names":
            from . import localnames
            seat = localnames.value(key)
            why = "local-names %r is unset: set it to the seat that owns " \
                  "%s" % (key, component)
        elif where == "integrator":
            from . import seats_integrator
            seat, why = seats_integrator.integrator_seat()
        else:
            seat, why = _lead(project)
    except Exception as exc:                 # noqa: BLE001 — a row still posts
        seat, why = None, "its steward could not be read (%s)" % exc
    if seat and not _SEAT.match(str(seat)):
        seat, why = None, "the declared steward %r is not a seat name" % seat
    return (seat, None) if seat else (None, why)


def _lead(project):
    if not project:
        return None, "no project serves this seat, so no project lead owns it"
    from . import teams
    leads = [m.get("seat") for m in teams.read(project).get("members") or ()
             if m.get("role") == "lead"]
    if len(leads) == 1:
        return leads[0], None
    return None, ("project %s's team names %s, so nobody leads its seats"
                  % (project, "no lead" if not leads
                     else "%d leads" % len(leads)))


def address(subjects):
    """[(component, project)] -> (the '@seat ' lead-in, the tail naming each
    subject nobody owns). One mention per steward, in first-seen order."""
    seen, lead, tail = set(), [], []
    for component, project in subjects:
        seat, why = steward(component, project)
        if seat and seat not in seen:
            seen.add(seat)
            lead.append("@" + seat)
        elif not seat and why not in tail:
            tail.append(why)
    return ((" ".join(lead) + " ") if lead else "",
            "".join("\n(no steward woken: %s)" % why for why in tail))


def row(component, body, project=None, kind=None):
    """The #seats text for one event: its steward's mention, FYI_LEAD when
    the event is a recovery, and the body."""
    lead, tail = address([(component, project)])
    return lead + (FYI_LEAD if kind in RECOVERIES else "") + body + tail


def is_recovery(text):
    """Does this #seats text lead, after its mentions, with FYI_LEAD?"""
    return isinstance(text, str) and bool(_FYI_ROW.match(text))


def post(component, body, project=None):
    """Post one event row to #seats now. Raises when the post fails, so a
    caller with its own latch keeps the event owed."""
    from . import chat
    return chat.post(row(component, body, project), who=WHO, room=ROOM)


def project_of(seats):
    """{seat: the registered project it serves, or None}, from the one world
    the teams read builds (`teams.placements`: home room, spawn register,
    working directory)."""
    try:
        from . import teams
        placed = teams.placements()["seats"]
    except Exception:                        # noqa: BLE001 — unowned, not lost
        placed = {}
    return {seat: (placed.get(seat) or {}).get("project") for seat in seats}


# ------------------------------------------------------------------ events

def event(component, key, kind, ident, body, more=None, project=None,
          push=False, title="helm", head=""):
    """One seat event. `key` names its episode (a family, an account), `ident`
    is the detector's own identity for this edge, `body` is the sentence
    both sinks carry, and `more` is evidence only the room gets. `push` sends
    it to the owner's phone too, as `head` + the batch's bodies."""
    return {"component": component, "key": str(key), "kind": kind,
            "ident": str(ident), "body": body, "more": more,
            "project": project, "push": bool(push), "title": title,
            "head": head}


def _claim(ledger, ev, now):
    """Record one event -> True when it is new and owed."""
    rid = "%s|%s|%s" % (ev["key"], ev["kind"], ev["ident"])
    if rid in ledger["rows"]:
        return False
    opened = ledger["open"]
    if ev["kind"] in OPENS and ev["key"] in opened:
        return False                          # a repeat inside the episode
    if ev["kind"] in CLOSES:
        opened.pop(ev["key"], None)
    elif ev["kind"] in OPENS:
        opened[ev["key"]] = {"rid": rid, "at": now}
    ledger["rows"][rid] = dict(ev, at=now, chat=False,
                               pushed=not ev["push"])
    return True


def _load(target, now):
    got = pk.read_json(target, default={})
    got = got if isinstance(got, dict) and got.get("v") == V else {}
    ledger = {"v": V, "open": dict(got.get("open") or {}),
              "rows": dict(got.get("rows") or {})}
    for table in (ledger["open"], ledger["rows"]):
        for rid in list(table):
            rec = table[rid]
            if not isinstance(rec, dict) or \
                    (rec.get("at") or 0) < now - KEEP_S:
                table.pop(rid)
    return ledger


def _deliver(ledger, post_fn, push_fn):
    posted = []
    # CLAIM ORDER: the sort is stable and keyed on the claim instant only,
    # so a wall and its unblock claimed in one call post in that order.
    owed = sorted(ledger["rows"].items(), key=lambda kv: kv[1].get("at") or 0)
    for rid, rec in owed:
        if rec.get("chat"):
            continue
        text = row(rec.get("component"), rec["body"] + (
            " — " + rec["more"] if rec.get("more") else ""),
            rec.get("project"), rec.get("kind"))
        try:
            post_fn(text, rid)
        except Exception:                    # noqa: BLE001 — owed, retried
            continue
        rec["chat"] = True
        posted.append(text)
    groups = {}
    for _rid, rec in owed:
        if not rec.get("pushed"):
            groups.setdefault((rec.get("title") or "helm",
                               rec.get("head") or ""), []).append(rec)
    for (title, head), recs in sorted(groups.items()):
        body = head + "; ".join(rec["body"] for rec in recs)
        try:
            done = push_fn(body, title)
        except Exception:                    # noqa: BLE001 — owed, retried
            done = False
        for rec in recs:
            rec["pushed"] = bool(done)
    return posted


def opened(path=None, now=None):
    """{episode key: when it opened} for every episode still open (a wall or
    an outage no closing event has closed), read without the lock. The
    dark-seat mover dates a darkness it measured from it (task/3881)."""
    now = time.time() if now is None else now
    got = pk.read_json(path or ledger_path(), default={})
    rows = got.get("open") if isinstance(got, dict) \
        and got.get("v") == V else None
    return {str(key): rec["at"] for key, rec in (rows or {}).items()
            if isinstance(rec, dict) and isinstance(rec.get("at"), (int, float))
            and rec["at"] >= now - KEEP_S}


def _chat_post(text, rid):
    """The room leg. The chat door's event id makes a retry of an owed row
    return the row already written, so a process that dies between the post
    and the ledger write never posts it twice."""
    from . import chat
    chat.post(text, who=WHO, room=ROOM, sign=False, event_id=(
        "seatevents:" + hashlib.sha256(rid.encode("utf-8")).hexdigest()[:40]))


def _owner_push(body, title):
    from . import notify
    return notify.owner_push(body, title=title,
                             receipt=("seatevents.notify_failed", title))


def announce(events, now=None, post=None, push=None, path=None):
    """Claim each new event, then deliver every channel still owed ->
    {"posted": [row texts sent now], "pushed": no phone push is owed,
     "claimed": the supplied events reached the durable ledger}.
    `post` is called as post(text, rid), `push` as push(body, title).

    One critical section: the claim, the delivery and the acknowledgement run
    under the ledger's lock, so two passes never deliver one event twice. The
    claim is written before anything is sent. Never raises; a ledger that
    cannot be written sends nothing and reports the push as owed."""
    now = time.time() if now is None else now
    target = path or ledger_path()
    events = [ev for ev in events or () if isinstance(ev, dict)]
    if not events and not os.path.exists(target):
        return {"posted": [], "pushed": True, "claimed": True}
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target + ".lock", "a+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                ledger = _load(target, now)
                for ev in events:
                    _claim(ledger, ev, now)
                pk.write_json(target, ledger)
                posted = _deliver(ledger, post or _chat_post,
                                  push or _owner_push)
                pk.write_json(target, ledger)
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    except Exception:                        # noqa: BLE001 — never the pass
        return {"posted": [], "pushed": False, "claimed": False}
    return {"posted": posted,
            "pushed": all(rec.get("pushed") for rec in ledger["rows"].values()),
            "claimed": True}
