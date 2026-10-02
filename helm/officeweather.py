#!/usr/bin/env python3
"""helm office weather — the whole floor in one word (task/3902, office floor
slice 4).

WHY IT EXISTS. The owner asked to know, at a glance and on his phone, whether
the office needs him. Every seat already carries a measured mood (`seatmood`,
slice 1) and the fleet already keeps its own alarms: the burn flags, the stall
watcher, the chat node's boot diagnosis and the land train's state. Nothing
folded them into the one answer he wants: do I have to come over?

THE THREE WORDS:
  sunny   every live seat is flowing or idle and no fleet alarm is up.
  cloudy  seat friction (even a stuck seat without a live steward), ordinary
          model walls, the land switch paused, chat node booting, or an
          unreadable source (UNKNOWN IS NEVER SUNNY).
  stormy  an owner decision, every measured model family walled, an open
          land-path P0 cause, or a fleet-blocking outage: pressure-watch, the
          chat node, the land train STOPPED or its timer dead, or FLEET-DOWN.

FLEET-DOWN HAS ONE DEFINITION, `beacon_phone.qualify` (the retired fleet
pager's qualifier, which the weather now calls): the steward seat unreachable,
or a strict majority (at least two) of the eligible seats unreachable, over a
complete census that matches the roster's attendance register. ONE
OBSERVATION, ONE OWNER: the census judged is the one `helm beacons --post`
ATTENDED and recorded beside the register; the weather takes no census of its
own. An incomplete census is never fleet-down, and a recorded census older
than two beacons passes (`beacon_phone.STALE_S`), or one that lists fewer
seats than the roll, never storms: the weather says so instead. The episode
keeps the cohort it froze when it opened, and a storm the phone heard clears
only when a fresh census shows every seat of that cohort covered again (or
resting: the owner paused it). THE BOUND: a heard episode that has held
FLEET_HOLD_S with no fresh clear (a seat that never comes back, a census
that stays stale) EXPIRES: one page names the seats still not covered and
says the weather stops holding the storm, no all-clear follows, and the same
uncovered seats never reopen it (`_reopens`), so the phone is free for the
next storm. The weather is the ONLY writer of the fleet-down page: beacons
and beacon_phone never push.
(Other modules still reach the owner's phone for their own events:
decisions, pressure-watch, the proxies.)

A STUCK SEAT IS HANDLED when its steward is another seat on the floor that
is not itself stuck, blocked or walled. The steward comes from task/3876's
declared table (`seatevents.steward`): the project's lead for a project seat,
else the integrator (build lanes); when that is the seat itself, the friction
steward takes over. A stuck, grinding or walled seat without an able steward
stays cloudy: seat friction is not a land-path P0 page.

THE LINE is `<glyph> <word>: <the first two reasons>[; +N more]`, one
printable line of at most LINE_MAX characters, laundered like every other
seat-sourced text. The same line heads `helm brief` and the console
(`/api/ready` carries it to the nav strip), and is what the phone gets on a
storm (an all-clear carries it after "all clear: ").

THE PUSH (`tick`, run by the idle-dispatch timer's pass every five minutes,
and by `helm office weather --push`). The owner's paging rule: "A P0 cause
turns the office weather stormy, and nothing else pages him." So the phone
hears STORMS, and nothing else:
  * the first reading in a helm home is a BASELINE and pushes nothing, so a
    fresh home (an install, a test) never rings the phone;
  * a new word must hold on passes HOLD_S apart before it SETTLES: a flap that
    comes and goes between passes never reaches the phone;
  * a settled edge INTO stormy pushes at once, and the matching ALL-CLEAR
    (the floor settled out of a storm the phone heard) pushes once, after
    GAP_S has passed since the storm's push. An unreadable source cannot
    establish an all-clear: its sources must read clean for HOLD_S first.
    An all-clear is dropped if the storm returns first, and waits while the
    floor is turning stormy again;
  * every other change of the word (sunny to cloudy and back, or out of a
    storm the phone never heard) is SHOWN on the surfaces, never pushed;
  * a heard fleet-down storm that expires (FLEET_HOLD_S) pushes its ONE
    expiry page before anything else; then the phone holds no storm, the
    word settles as the floor reads, and a storm still on pages next pass;
  * a push that fails stays owed and is retried on the next pass;
  * at the cutover the retired pager's last down latch is read once per helm
    home and judged by the same definition: a page it sent whose outage has
    cleared owes one all-clear, a page it owed whose outage is still on is
    sent once, and a page it owed whose outage has cleared is dropped. Once
    the record holding it is written the latch file is renamed `.migrated`,
    so a lost weather record cannot adopt it twice.
The state is one file under the helm home (`state_path`), written under a
lock: a pass that cannot take it does nothing, so two passes never push one
change twice. The fleet record lives in it too (`fleet`, `_fleet_record`):
the open episode, when it opened and was heard, and the last expired one.

ONE PHONE SEAM. Every push goes through `_phone()`, which answers
`notify` (`notify.owner_push`, the one phone path) or None. SWITCH_ENV=off
makes it None and makes the idle tick's pass (`ride`) read and record
nothing: the owner's kill switch for the weather, and the switch the test
suite plants (tests/__init__.py), so no test, and no child a test runs, can
ring his phone. A caller that wants delivery without the switch hands
`tick` a phone of its own.

READ ONLY EXCEPT THE TICK'S PUSH PASS. `weather` and `surface` write
nothing, and `surface` is one small file read, so the brief (which never
probes) and the console can afford it on every read.
"""
import fcntl
import json
import os
import sys
import time

from . import home, pk

WORDS = ("sunny", "cloudy", "stormy")
_RANK = {w: i for i, w in enumerate(WORDS)}
GLYPH = {"sunny": "\u2600", "cloudy": "\u2601", "stormy": "\u26c8"}
#: Moods that need nobody.
CALM = frozenset(("flowing", "idle"))
#: A steward in one of these moods cannot handle another seat.
UNABLE = frozenset(("stuck", "blocked-on-owner", "walled"))
#: A new word settles once it holds on passes this far apart; the all-clear
#: after a storm waits this long after the storm's push.
HOLD_S, GAP_S = 240, 30 * 60
#: A heard fleet-down storm that has held this long with no fresh clear stops
#: holding the phone: one seat that never returns must not mute later storms.
FLEET_HOLD_S = 2 * 3600
HOLD_ENV, GAP_ENV = "HELM_OFFICE_WEATHER_HOLD_S", "HELM_OFFICE_WEATHER_GAP_S"
#: `off` (or 0, no) stops the idle tick's pass and makes the phone seam inert.
SWITCH_ENV = "HELM_OFFICE_WEATHER"
_OFF = ("0", "off", "no")
#: The surfaces call the last pass stale past this (the tick runs every 5m).
STALE_S = 20 * 60
#: pressure-watch reads the fleet every minute; a last read older than this
#: says nothing about now.
STALL_STALE_S = 10 * 60
LINE_MAX, SHOWN = 200, 2
TITLE = "helm office weather"
RECEIPT = "office-weather"
NOT_MEASURED = ("office weather: not measured yet (the idle-dispatch tick "
                "reads the floor every five minutes)")
#: The chat node's boot diagnosis states, in the owner's words.
_NODE = {"down": "down", "hung": "hung", "refused": "refused to start",
         "unknown": "no answer"}


# ------------------------------------------------------------------ helpers

def _key(seat):
    return str(seat or "").casefold()


def _text(value, cap=200):
    """One printable line: controls, format and separator characters dropped,
    runs of space folded, clipped."""
    return " ".join(pk.launder(str(value if value is not None else ""),
                               keep="").split())[:cap]


def _why(exc):
    return "%s: %s" % (type(exc).__name__, _text(exc, 120))


class _Unread(tuple):
    """A cloudy finding whose source cannot establish that a storm cleared."""

    def __new__(cls, why):
        return super().__new__(cls, ("cloudy", why))


def _num(value):
    return value if isinstance(value, (int, float)) \
        and not isinstance(value, bool) else None


def _clock(epoch):
    return time.strftime("%H:%MZ", time.gmtime(epoch))


def _instant(value):
    """A recorded instant the weather can compute with and print, or None: a
    number that is NaN, infinite or beyond the platform's clock is no instant
    (kept, it would raise in a reason every pass or never reach the bound)."""
    v = _num(value)
    try:
        return v if v is not None and v == v and _clock(v) else None
    except (OverflowError, ValueError, OSError):
        return None


def _clip(text, cap):
    return text if len(text) <= cap else text[:cap - 1].rstrip() + "…"


def _knob(name, default):
    raw = (os.environ.get(name) or "").strip()
    return int(raw) if raw.isdigit() and int(raw) > 0 else default


def hold_s():
    return _knob(HOLD_ENV, HOLD_S)


def gap_s():
    return _knob(GAP_ENV, GAP_S)


def switched_off():
    return (os.environ.get(SWITCH_ENV) or "").strip().lower() in _OFF


def _phone():
    """THE ONE PHONE SEAM: the module that reaches the owner's phone, or None
    while SWITCH_ENV is off. Nothing else here imports notify."""
    if switched_off():
        return None
    from . import notify
    return notify


# ------------------------------------------------------------------ readers
#
# Each fleet reader answers [(word, why)] from the module that owns the
# question; none keeps a record of its own. `weather(reads=...)` replaces any
# of them, which is how the tests run without this machine's fleet.

def _floor(now):
    from . import seatmood
    return seatmood.floor(now=now)


def _stewards(moods):
    """{seat key: (steward, why)} for each mood that needs one, from
    task/3876's declared table."""
    try:
        from . import seatevents
    except ImportError:
        why = ("no steward table on this checkout (helm/seatevents.py, "
               "task/3876)")
        return {_key(m["seat"]): (None, why) for m in moods}
    from . import burnflags
    local = set(burnflags.local_families())
    places = seatevents.project_of([m["seat"] for m in moods])
    out = {}
    for m in moods:
        seat = m["seat"]
        if m["state"] == "walled":
            fam = ((m.get("signals") or {}).get("wall") or {}).get("family")
            out[_key(seat)] = seatevents.steward(
                "local-serving" if fam in local else "credentials")
            continue
        lead = seatevents.steward("project-seats", places[seat]) \
            if places.get(seat) else (None, None)
        if m["state"] == "stuck" and _key(lead[0]) == _key(seat):
            out[_key(seat)] = seatevents.steward("helm-friction")
            continue
        build = lead if lead[0] and _key(lead[0]) != _key(seat) \
            else seatevents.steward("build-lanes")
        out[_key(seat)] = seatevents.steward("helm-friction") \
            if m["state"] == "stuck" and _key(build[0]) == _key(seat) \
            else build
    return out


def _stall(now):
    """The stall watcher's open episode (pressurewatch, outside agents.slice).
    A host with no watcher says nothing; a watcher that stopped reading is
    named."""
    from . import pressurewatch
    path = pressurewatch.state_path()
    if not os.path.exists(path):
        return []
    state = pressurewatch._load(path)
    last = state.get("last") if isinstance(state.get("last"), dict) else {}
    at = _num(last.get("at"))
    if at is None or now - at > STALL_STALE_S:
        return [_Unread("pressure-watch has not read the fleet %s" % (
            "since " + _clock(at) if at is not None else "yet"))]
    ep = pressurewatch._episode(state)
    if ep:
        return [("stormy", "the fleet is stalled (pressure-watch, since %s)"
                 % _clock(ep["since"]))]
    return []


def _chat(now):
    """The chat node: off by config says nothing, a node still booting is
    cloudy, any other silence is stormy (`chatnode.boot_diagnosis`)."""
    from . import chat, chatnode
    url = chat.node_url()
    if not url or chat.node_head(url) is not None:
        return []
    state = chatnode.boot_diagnosis(url).get("state")
    if state in ("initializing", "preparing"):
        return [("cloudy", "the chat node is booting (%s)" % state)]
    return [("stormy", "the chat node is not answering (%s)"
             % _NODE.get(state, _text(state, 24) or "no answer"))]


def _land_root():
    """The shared checkout the land train lands (a lane worktree folds back
    to it, as auto-land's own timer does)."""
    from .work import find_root
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return find_root(here) or here


def _land(now):
    """The land train: paused is cloudy; STOPPED, or its timer trapped,
    failed or never firing, is stormy. A host with no auto-land timer has no
    timer to be dead."""
    from . import autoland, timerhealth
    root, out = _land_root(), []
    control, why = autoland.read_control(root)
    if why:
        out.append(_Unread("the auto-land switch could not be read"))
    elif control.get("paused"):
        out.append(("cloudy", "auto-land is paused by %s" % _text(
            control["paused"].get("by") or "someone", 64)))
    st, why = autoland.active(root)
    if why:
        out.append(_Unread("the land train could not be read"))
    elif st and st.get("state") == autoland.STOPPED:
        out.append(("stormy", "the land train %s is STOPPED: %s" % (
            _text(st.get("name") or st.get("train"), 64),
            _text((st.get("stopped") or {}).get("why")
                  or "no reason recorded", 120))))
    if not autoland.timer_installed():
        return out
    rows, err = timerhealth.census()
    if err:
        out.append(_Unread("the land timer's health is unmeasured"))
    dead = [r[1] for r in rows if r[0] == autoland.TIMER_NAME
            and r[1] in (timerhealth.TRAP, timerhealth.FAILED,
                         timerhealth.NEVER)]
    if dead:
        out.append(("stormy", "the land train is dead: %s is %s"
                    % (autoland.TIMER_NAME, dead[0])))
    return out


def _burn(now):
    """The burn flags: unmeasured is cloudy, every measured family RED is
    stormy. One walled family shows as its seats' walls on the floor."""
    from . import burnflags
    snap, _age = burnflags.cached_snapshot(now=now)
    if not snap:
        return [_Unread("the burn flags are not measured (no fresh "
                        "snapshot)")]
    fams = {f: fl.get("colour") for f, fl in
            (snap.get("families") or {}).items() if isinstance(fl, dict)}
    measured = sorted(f for f, c in fams.items() if c != burnflags.GREY)
    if measured and all(fams[f] == burnflags.RED for f in measured):
        return [("stormy", "every measured model family is walled (%s)"
                 % ", ".join(measured))]
    return []


def _land_path(key):
    """A cause's `key` is a land-path P0 iff it is (a) a land-hook refusal, or
    (b) the stopped auto-land train's reason names its guard — by provenance,
    never by priority or how many seats share it. `key` is "<guard>|<reason>";
    a land-hook refusal is a reason in frictionpilot's LAND_PATH; a stopped
    train names its guard in `stopped.why`. Anything else (a plain P0, or a
    cause that cannot be read) is not land-path."""
    from . import frictionpilot
    guard, _sep, reason = str(key or "").partition("|")
    guard = guard.strip()
    reason = reason.strip().lower()
    if not reason:
        return False
    if reason in frictionpilot.LAND_PATH:
        return True
    stop_text = frictionpilot.land_stop_text()
    return bool(stop_text) and frictionpilot._names(guard, stop_text)


def _causes(now):
    """The P0 causes: one stormy finding per LAND-PATH P0 cause, nothing else.

    A cause is land-path by its provenance only (`_land_path`): a land-hook
    refusal, or the stopped auto-land train's reason names its guard. A P0
    spread across five seats, or filed P0 for no land-path reason, is NOT a
    finding here — it stays on the console and #seats, and never pages the
    owner. Never raises: an unreadable cause store is [] (the fleet sees no
    P0, never sunny by absence of one)."""
    from . import frictionpilot
    try:
        causes = frictionpilot.p0_causes()
    except Exception:
        return []
    return [("stormy", "the land-path P0 cause %s is open"
             % _text(key, 120))
            for key in (c.get("key") for c in causes)
            if isinstance(key, str) and _land_path(key)]


def _fleet_record(value):
    """The weather's fleet record, normalized -> {episode, opened, heard,
    owed, gone, expired}: the open episode (`beacon_phone.episode`), when it
    opened and when the phone first held its storm, the seats it still owes
    as of the last fresh census (and which of them left the roster), and the
    last episode the bound EXPIRED (`_expire`). A malformed part is dropped,
    never trusted: a malformed episode is no episode."""
    from . import beacon_phone
    v = value if isinstance(value, dict) else {}
    try:
        ep = beacon_phone.episode(v.get("episode") or beacon_phone.CLEAR)
    except ValueError:
        ep = dict(beacon_phone.CLEAR)
    exp = v.get("expired")
    if not (isinstance(exp, dict) and _instant(exp.get("at")) is not None
            and (_num(exp.get("opened")) is None
                 or _instant(exp["opened"]) is not None)
            and _names(exp.get("owed")) is not None
            and (exp.get("gone") is None or _names(exp["gone"]) is not None)
            and (exp.get("page") is None or isinstance(exp["page"], str))):
        exp = None
    down = ep["phase"] == "down"
    return {"episode": ep,
            "opened": _instant(v.get("opened")) if down else None,
            "heard": _instant(v.get("heard")) if down else None,
            "owed": (_names(v.get("owed")) or []) if down else [],
            "gone": (_names(v.get("gone")) or []) if down else [],
            "expired": exp}


def _names(value):
    return list(value) if isinstance(value, list) and all(
        isinstance(n, str) for n in value) else None


def _reopens(expired, down):
    """THE BOUND'S REOPEN RULE: after an episode the bound expired, does a
    fleet-down verdict whose unreachable seats are `down` open a new one?
    Only for a DIFFERENT outage, a seat down that the expired episode did not
    still owe; the same uncovered seats reading down pass after pass never
    re-page. With no expired episode, always. (A fresh clear drops the
    expired episode, which is the other way a new one opens.)"""
    if not expired:
        return True
    owed = {n.casefold() for n in expired.get("owed") or ()}
    return not {n.casefold() for n in down} <= owed


def _owed_expiry(rec):
    """The expired episode to carry past a reset of the record (a fresh
    clear, or a different outage opening), when its ONE page has not landed
    yet: the page stays owed until `_deliver_expiry` lands it. It owes no
    seat any more (the reset already happened), so it never holds a new
    episode off (`_reopens`)."""
    exp = rec["expired"]
    return dict(exp, owed=[], gone=[]) if exp and exp.get("page") else None


def _since_why(expired):
    from . import beacon_phone
    at = expired["at"]
    opened = _num(expired.get("opened"))
    return ("the fleet has been down since %s; the weather stopped holding "
            "it at %s: %s still not covered" % (
                _clock(at if opened is None else opened), _clock(at),
                beacon_phone.named(expired["owed"], expired.get("gone") or ())
                or "no seat named"))


def _expire(rec, got, now):
    """End a heard episode that has held FLEET_HOLD_S with no fresh clear ->
    (the record to keep, its finding). The episode ends as EXPIRED: when,
    since when, the seats it still owed, and the ONE page that says so
    (`_deliver` sends it once, and no all-clear: nothing recovered)."""
    from . import beacon_phone
    ep = rec["episode"]
    owed, gone = rec["owed"], rec["gone"]
    if not owed:
        owed = list(dict.fromkeys(ep["cohort"] + ([ep["steward"]]
                                                  if ep["steward"] else [])))
    opened = rec["opened"] if rec["opened"] is not None else rec["heard"]
    held = "" if got["phase"] == "down" else " (%s)" % _text(got["why"], 120)
    page = ("the fleet has been down %d h: %s still not covered%s; the "
            "weather stops holding this storm so other storms can page" % (
                (now - opened) // 3600, beacon_phone.named(owed, gone), held))
    expired = {"at": now, "opened": opened, "owed": owed, "gone": gone,
               "page": page}
    return (dict(_fleet_record(None), expired=expired),
            [("cloudy", _since_why(expired))])


def _held(rec, got, now):
    """The fleet record after this pass's verdict -> (record, findings).

      * a fresh "clear" ends any episode (an all-clear is the weather's, by
        the word) and drops an expired one, except an expiry page that has
        not landed yet (`_owed_expiry`);
      * "down" with no open episode opens one, unless the bound expired an
        episode and this is the same outage (`_reopens`): that stays the
        cloudy reason the expired episode left;
      * an episode the phone heard that has held FLEET_HOLD_S with no fresh
        clear EXPIRES (`_expire`), whatever holds it: seats that never come
        back, a census gone stale or one that cannot speak;
      * otherwise the episode is kept as the qualifier answered it."""
    latch, phase = rec["episode"], got["phase"]
    if phase == "clear":
        return dict(_fleet_record(None), expired=_owed_expiry(rec)), []
    if phase == "down" and latch["phase"] == "clear":
        if not _reopens(rec["expired"], got.get("down") or ()):
            return rec, [("cloudy", _since_why(rec["expired"]))]
        rec = dict(_fleet_record(None), episode=got["latch"], opened=now,
                   owed=list(got.get("down") or ()),
                   expired=_owed_expiry(rec))
    else:
        rec = dict(rec, episode=got.get("latch", latch))
        if phase == "down" and "owed" in got:
            rec.update(owed=list(got["owed"]), gone=list(got["gone"]))
    ep = rec["episode"]
    if ep["phase"] == "down" and ep["delivered"]:
        if rec["heard"] is None:
            rec["heard"] = now               # the bound runs from now
        if now - rec["heard"] >= FLEET_HOLD_S:
            return _expire(rec, got, now)
    if phase == "down":
        # An open episode whose fleet is up again (it waits on its cohort)
        # is not "the fleet is down": say what it waits on.
        return rec, [("stormy", "the fleet is down: %s; helm cannot wake it"
                      % got["why"]) if got["looks_down"] else
                     ("stormy", "the fleet was down and %s" % got["why"])]
    if ep["phase"] == "down" and ep["delivered"]:
        return rec, [("stormy", "the fleet was down and is not shown up "
                      "again (%s); helm cannot wake it" % got["why"])]
    if got.get("stale") or got.get("short") or got["looks_down"] \
            or ep["phase"] == "down":
        return rec, [_Unread("fleet-down is not established: %s"
                             % got["why"])]
    return rec, []


def _fleet(now, record=None, verdict=None):
    """Fleet-down, by THE ONE DEFINITION (`beacon_phone.qualify`): the steward
    seat unreachable, or a strict majority (at least two) of the eligible
    seats unreachable, over a COMPLETE census that matches the roster's
    attendance register. The census is the one `helm beacons --post` attended
    and recorded (`beacon_phone.fleet`), never one taken here: a second
    census at another instant disagrees with the register on every seat
    attendance held or that flapped, and each disagreement would reset the
    storm's debounce. RESTING seats and census rows for seats off the roll
    have no vote, so they cannot hold a storm off.

    `record` is the weather's fleet record (`_fleet_record`: the open
    episode, its frozen cohort and whether the phone heard it, and the last
    expired one); the read-only surfaces take it from the weather's file.
    An episode the phone heard stays stormy until every seat of its cohort is
    covered again, or until FLEET_HOLD_S has passed with no fresh clear: then
    it EXPIRES (`_held`), pages once that the weather stops holding it, and
    the same uncovered seats never reopen it. `verdict`, when given, receives
    the qualifier's whole answer and the record to keep (`record`), which is
    how `tick` keeps the episode and adopts the cutover latch.

    A recorded census that is stale, missing or unreadable is a cloudy
    unread finding that says so ("the beacons census is N min old"), and
    never storms; so is one that lists fewer seats than the roll ("the
    beacons census lists N of M seats"). Any other incomplete one is NOT
    fleet-down: it is a cloudy unread finding when it is what stands between
    the floor and a storm (its rows alone qualify) or an all-clear (an
    episode is open), and nothing otherwise. ONLY A FRESH "clear" ENDS A
    STORM THE PHONE HEARD (or the bound): while that episode is open, a
    census that cannot establish anything keeps it stormy (and says why), so
    a stale or partial reading never settles the word out of a storm the
    owner was paged for."""
    from . import beacon_phone
    rec = _fleet_record(_load().get("fleet") if record is None else record)
    try:
        got = beacon_phone.fleet(now, rec["episode"])
    except Exception as exc:                 # noqa: BLE001 — named, never sunny
        got = {"phase": "unknown", "latch": rec["episode"],
               "looks_down": False, "stale": True,
               "why": "the roster could not be read (%s)" % _why(exc)}
    keep, found = _held(rec, got, now)
    if verdict is not None:
        verdict.update(got, record=keep)
    return found


_READERS = {"floor": _floor, "stewards": _stewards, "stall": _stall,
            "chat": _chat, "land": _land, "burn": _burn,
            "causes": _causes, "fleet": _fleet}
#: The fleet's sources, read in this order. Each answers findings from the
#: module that owns its question (`_causes` the land-path P0 causes, `_fleet`
#: fleet-down by beacon_phone's one definition); a stormy finding pages
#: through `_deliver`, and a reader that raises reads cloudy, never sunny.
_FLEET = (("stall", "the stall watcher"), ("chat", "the chat node"),
          ("land", "the land train"), ("burn", "the burn flags"),
          ("causes", "the friction autopilot"),
          ("fleet", "the fleet reachability census"))


# ------------------------------------------------------------------ the word

def _handler(m, live, named):
    """(steward, None) when a live, able steward has `m`, else (None, why)."""
    who, why = named
    if not who:
        return None, why or "no steward is declared"
    who = _text(who, 64)
    if _key(who) == _key(m["seat"]):
        return None, "it is its own steward"
    s = live.get(_key(who))
    if s is None:
        return None, "its steward %s is not live" % who
    if s["state"] in UNABLE:
        return None, "its steward %s is %s too" % (who, s["state"])
    return who, None


def _floor_findings(moods, stewards):
    live = {_key(m["seat"]): m for m in moods}
    needy = [m for m in moods
             if m["state"] not in CALM and m["state"] != "blocked-on-owner"]
    unread_stewards = None
    try:
        table = stewards(needy) if needy else {}
    except Exception as exc:                 # noqa: BLE001 — named per seat
        unread_stewards = "the steward table could not be read (%s)" % _why(exc)
        table = {_key(m["seat"]): (None, unread_stewards) for m in needy}
    out = [_Unread(unread_stewards)] if unread_stewards else []
    for m in moods:
        seat, state = _text(m["seat"], 64), m["state"]
        unread = (m.get("signals") or {}).get("unread") or ()
        if unread:
            out.append(_Unread("%s's mood sources could not be read (%s)" % (
                seat, _text(unread[0], 120))))
        if state in CALM:
            continue
        if state == "blocked-on-owner":
            out.append(("stormy", "%s waits on you: %s"
                        % (seat, _text(m.get("reason")))))
            continue
        who, why = _handler(m, live, table.get(_key(m["seat"]))
                            or (None, "no steward was named"))
        if who:
            out.append(("cloudy", "%s is %s, %s has it" % (seat, state, who)))
        elif state == "stuck":
            out.append(("cloudy", "%s is stuck and no steward has it (%s): %s"
                        % (seat, why, _text(m.get("reason")))))
        else:
            out.append(("cloudy", "%s is %s (%s)" % (seat, state, why)))
    return out


def _line(word, found, moods):
    head = "%s %s: " % (GLYPH[word], word)
    if not found:
        n = len(moods)
        return head + ("%d live seat%s flowing or idle" % (n, "s"[:n != 1])
                       if n else "no live seat on the floor")
    more = len(found) - SHOWN
    tail = "; +%d more" % more if more > 0 else ""
    body = "; ".join(y for _w, y in found[:SHOWN])
    return head + _clip(body, LINE_MAX - len(head) - len(tail)) + tail


def weather(now=None, reads=None):
    """(word, line, reasons) for the floor now; reasons are {word, why},
    stormy first. READ ONLY. `reads` replaces any of `_READERS`."""
    now = time.time() if now is None else now
    r = dict(_READERS, **(reads or {}))
    found, moods = [], []
    try:
        moods = list(r["floor"](now))
    except Exception as exc:                 # noqa: BLE001 — never sunny
        found.append(_Unread("the floor could not be read (%s)"
                             % _why(exc)))
    found += _floor_findings(moods, r["stewards"])
    for name, label in _FLEET:
        try:
            found += list(r[name](now) or ())
        except Exception as exc:             # noqa: BLE001 — never sunny
            found.append(_Unread("%s could not be read (%s)"
                                 % (label, _why(exc))))
    # every reason is laundered once, here: seat names, mood reasons and the
    # steward table's own words all reach a phone and a terminal
    found = sorted((_Unread(_text(f[1], 400)) if isinstance(f, _Unread)
                    else (f[0], _text(f[1], 400)) for f in found),
                   key=lambda f: -_RANK[f[0]])
    word = found[0][0] if found else "sunny"
    return word, _line(word, found, moods), [
        dict(word=f[0], why=f[1],
             **({"unread": True} if isinstance(f, _Unread) else {}))
        for f in found]


# ------------------------------------------------------------------ the pass

def state_path():
    return os.path.join(home.global_dir(), ".state", "office-weather.json")


def _load():
    got = pk.read_json(state_path(), None)
    return got if isinstance(got, dict) else {}


def _settled(st):
    s = st.get("settled")
    return s if isinstance(s, dict) and s.get("word") in WORDS \
        and _num(s.get("since")) is not None else None


def _step(st, word, line, now, unread=False):
    """Move the settled word and the candidate -> (state, what happened)."""
    settled = _settled(st)
    cand = st.get("candidate") if isinstance(st.get("candidate"), dict) \
        and _instant(st["candidate"].get("since")) is not None else None
    pushed = st.get("pushed") if isinstance(st.get("pushed"), dict) else None
    known = _instant(st.get("known_since"))
    out = {"word": word, "line": line, "at": now, "settled": settled,
           "candidate": None, "pushed": pushed, "owed": bool(st.get("owed")),
           "known_since": None if unread else
           (known if known is not None else now)}
    if settled is None:
        out.update(settled={"word": word, "line": line, "since": now},
                   owed=False)
        return out, "baseline"
    if word == settled["word"]:
        settled["line"] = line
        return out, "same"
    if not cand or cand.get("word") != word:
        out["candidate"] = {"word": word, "since": now}
        return out, "turning"
    if now - cand["since"] < hold_s():
        out["candidate"] = cand
        return out, "turning"
    out.update(settled={"word": word, "line": line, "since": cand["since"]},
               owed=True)
    return out, "settled"


def _deliver(st, what, now, phone):
    """Tell the phone of a settled storm or its all-clear -> what it was
    told, in one phrase. Any other change is shown and never pushed."""
    settled, pushed, cand = st["settled"], st["pushed"], st["candidate"]
    if what == "baseline":
        return "baseline: the first reading pushes nothing"
    expired = (st.get("fleet") or {}).get("expired") or {}
    if expired.get("page"):
        return _deliver_expiry(st, expired, now, phone)
    if not st["owed"]:
        return "unchanged" if what == "same" else \
            "holding: %s must hold %ds before it settles" % (
                cand["word"], hold_s())
    storm, heard = settled["word"] == "stormy", (pushed or {}).get("word")
    if storm == (heard == "stormy"):
        st["owed"] = False
        return "nothing: the phone already holds the storm" if storm \
            else "shown, not pushed: the phone hears only a storm and its " \
                 "all-clear"
    last = _instant((pushed or {}).get("at"))
    if not storm and (cand or {}).get("word") == "stormy":
        return "held: the all-clear waits while the floor is turning stormy"
    if not storm and (st["known_since"] is None or
                      now - st["known_since"] < hold_s()):
        return "held: the all-clear waits for a readable floor to hold"
    if not storm and last is not None and now - last < gap_s():
        return "held: the all-clear waits until %s, and is dropped if the " \
               "storm returns" % _clock(last + gap_s())
    phone = _phone() if phone is None else phone
    if phone is None:
        st["owed"] = False
        return "off: %s=off, so the phone is not told" % SWITCH_ENV
    if not phone.configured():
        return "owed: no phone transport is configured; the next pass retries it"
    body = settled["line"] if storm else "all clear: " + settled["line"]
    delivered = phone.owner_push(body, title=TITLE,
                                 receipt=(RECEIPT, settled["word"]))
    if not delivered or not phone.configured():
        return "owed: the push did not land; the next pass retries it"
    st.update(pushed={"word": settled["word"], "line": body, "at": now},
              owed=False)
    return "pushed" if storm else "pushed the all-clear"


def _deliver_expiry(st, expired, now, phone):
    """The bound's ONE page: the weather stops holding a heard fleet storm.
    Once it lands (or the switch is off) the phone holds no storm, and the
    floor's word settles as it reads now, so a storm still on (another
    cause) pages next pass and a calm floor sends no all-clear. A push that
    fails stays owed, and nothing else is told until it lands."""
    phone = _phone() if phone is None else phone
    if phone is not None:
        if not phone.configured():
            return ("owed: no phone transport is configured; the next pass "
                    "retries the fleet expiry")
        if not phone.owner_push(expired["page"], title=TITLE,
                                receipt=(RECEIPT, "fleet-expired")) \
                or not phone.configured():
            return "owed: the fleet expiry did not land; the next pass retries it"
    page, expired["page"] = expired["page"], None
    st.update(pushed={"word": "expired", "line": page, "at": now},
              settled={"word": st["word"], "line": st["line"], "since": now},
              candidate=None, owed=st["word"] == "stormy")
    return "pushed the fleet expiry: the weather stops holding its storm" \
        if phone is not None else \
        "off: %s=off, so the phone is not told the fleet expiry" % SWITCH_ENV


def _beacon_latch():
    """The retired fleet pager's last episode, read once per helm home at the
    cutover -> a down latch, or None (clear, absent or unreadable)."""
    from . import beacon_phone
    try:
        latch = beacon_phone._latch(beacon_phone.latch_path())
    except Exception:                        # noqa: BLE001 — nothing to adopt
        return None
    return latch if latch["phase"] == "down" else None


def _retire_beacon_latch():
    """Rename the retired pager's latch to `.migrated` (atomic) once the
    weather's record holding it is written, so a lost or reset weather
    record can never adopt it a second time and send a second all-clear.
    A rename that fails leaves the record's `migrated_beacon` as the guard."""
    from . import beacon_phone
    src = beacon_phone.latch_path()
    try:
        os.replace(src, src + ".migrated")
    except OSError:                          # absent, or the record guards it
        pass


def _adopt_beacon_latch(st, latch, verdict, what, now, line):
    """One-time: carry the retired pager's down latch into the weather's own
    record -> what `_deliver` should treat this pass as.

    The fleet reader judged this pass WITH the latch's episode (its frozen
    cohort and whether the phone heard it), by the same one definition the
    pager used, so whether the outage has cleared is already decided:
      * still down: the storm is the weather's own, settled now. A page the
        phone already heard is not repeated; an owed page is sent now, once.
      * cleared: a page the phone heard owes exactly one all-clear (`pushed`
        names the storm, so `_deliver` sends it once the floor holds and
        GAP_S has passed); a page it never heard is dropped with no page.
      * undecided (the recorded census was incomplete, stale or unread): a
        heard page keeps the floor stormy, since only a fresh clear ends it,
        so it settles as the weather's storm and is never re-sent; a page it
        never heard waits. The episode stays the weather's, and the next
        fresh complete census decides.
    """
    phase = verdict.get("phase")
    if latch["delivered"]:
        st["pushed"] = {"word": "stormy", "at": now,
                        "line": "helm fleet: steward or majority unreachable"}
        st["owed"] = True
    if phase == "down":
        st["settled"] = {"word": "stormy", "since": now, "line": line}
        st["candidate"] = None
        st["owed"] = True
    return "adopted" if st["owed"] else what


def tick(now=None, push=True, reads=None, phone=None):
    """One pass -> {word, line, reasons, settled, phone}. With push it
    records the reading and tells the phone of a settled storm or its
    all-clear (the timer's pass); without it nothing is written and nothing
    is pushed. `phone` replaces the seam (`_phone`) for this pass.

    The push pass reads the floor under the weather's lock, so the fleet
    reader judges against the episode this record holds (and, at the
    cutover, the retired pager's latch, read once per helm home)."""
    now = time.time() if now is None else now
    if not push:
        word, line, reasons = weather(now, reads)
        return {"word": word, "line": line, "reasons": reasons,
                "settled": _settled(_load()),
                "phone": "dry run: nothing written, nothing pushed"}
    from . import beacon_phone
    path = state_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".lock", "a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            word, line, reasons = weather(now, reads)
            return {"word": word, "line": line, "reasons": reasons,
                    "settled": None,
                    "phone": "skipped: another weather pass holds the lock"}
        old = _load()
        cutover = not old.get("migrated_beacon")
        latch = _beacon_latch() if cutover else None
        held = _fleet_record(old.get("fleet"))
        if latch is not None:
            held = dict(held, episode=latch, opened=now,
                        heard=now if latch["delivered"] else None)
        verdict, r = {}, dict(reads or {})
        r.setdefault("fleet", lambda t: _fleet(t, held, verdict))
        word, line, reasons = weather(now, r)
        st, what = _step(old, word, line, now,
                         unread=any(x.get("unread") for x in reasons))
        st["migrated_beacon"] = True
        kept = verdict.get("record", held)
        if kept["episode"]["phase"] == "down" or kept["expired"]:
            st["fleet"] = kept
        if latch is not None:
            what = _adopt_beacon_latch(st, latch, verdict, what, now, line)
        said = _deliver(st, what, now, phone)
        fleet = st.get("fleet") or {}
        ep = fleet.get("episode") or {}
        # While an expiry page is owed, the phone's last storm is the episode
        # that expired, never this one: this one is heard once it pages.
        if ep.get("phase") == "down" and (st.get("pushed") or {}).get(
                "word") == "stormy" and not (fleet.get("expired") or {}).get(
                "page"):
            ep["delivered"] = True           # the phone holds this storm
            if st["fleet"]["heard"] is None:
                st["fleet"]["heard"] = now   # FLEET_HOLD_S runs from here
        pk.write_json(path, st)
        if cutover:
            _retire_beacon_latch()
    return {"word": word, "line": line, "reasons": reasons,
            "settled": st["settled"], "phone": said}


def ride(push=True):
    """The idle-dispatch tick's pass -> lines to print. Never raises: the
    tick it rides goes on whatever the weather does. SWITCH_ENV off: no
    read, no record, no push."""
    if switched_off():
        return ["office weather: off (%s=off): the floor is not read and the "
                "phone is not told" % SWITCH_ENV]
    from . import tickalarm
    try:
        got = tick(push=push)
    except Exception as exc:                 # noqa: BLE001 — the tick goes on
        return ["office weather: FAILED (%s)" % _why(exc)] + tickalarm.record(
            "officeweather", tickalarm.error_of(exc))
    tickalarm.record("officeweather")
    return ["office weather: %s [phone: %s]" % (got["line"], got["phone"])]


# ------------------------------------------------------------------ surfaces

def surface(now=None):
    """What the brief and the console show: the settled line from the last
    pass, with its age. One small file read; never a probe."""
    now = time.time() if now is None else now
    st = _load()
    settled, at = _settled(st), _instant(st.get("at"))
    if settled is None or at is None:
        return {"word": None, "line": NOT_MEASURED, "shown": NOT_MEASURED,
                "at": None, "stale": True, "turning": None}
    cand = st.get("candidate") if isinstance(st.get("candidate"), dict) \
        else {}
    turning = cand.get("word") if cand.get("word") in WORDS else None
    stale = now - at > STALE_S
    line = _text(settled.get("line"), LINE_MAX)
    shown = line + (" (turning %s)" % turning if turning else "") + (
        " (as of %s; not read since)" % _clock(at) if stale else "")
    return {"word": settled["word"], "line": line, "shown": shown,
            "at": pk.epoch_ts(at), "stale": stale, "turning": turning}


# ------------------------------------------------------------------ CLI

USAGE = """usage: helm office weather [--json] [--push]
  The whole floor in one word. sunny: every live seat is flowing or idle and
  no fleet alarm is up. cloudy: seat friction (including a stuck seat without
  a live steward), a single model wall, auto-land paused, the chat node booting
  or a source helm could not read. stormy: the owner waits on a decision,
  every measured model family is walled, a land-path P0 cause is open, the
  fleet stalled, the chat node not answering, the land train STOPPED or its
  timer dead, or the fleet down: the steward seat or a majority of the seats
  unreachable on a complete census. Bare, it reads the floor now and prints
  the word, its line and every reason; nothing is written. --push is the
  timer's pass (the idle-dispatch tick runs it every five minutes): it
  records the reading and pushes to the owner's phone only a settled edge
  INTO stormy and, %dm after it, the matching all-clear. Every other change
  is shown here, in helm brief and on the console, and never pushed. A
  change must hold %ds to settle, and the first reading is a baseline. A
  fleet-down storm the phone heard holds at most %dh with no fresh clear:
  then one page names the seats still not covered, no all-clear follows,
  and the same seats still down never reopen it. %s=off stops the tick's
  pass and every push. `helm office` alone is `helm office weather`.""" % (
    GAP_S // 60, HOLD_S, FLEET_HOLD_S // 3600, SWITCH_ENV)


def cmd_office(args):
    """helm office weather [--json] [--push] — the whole floor in one word."""
    argv = list(args)
    if argv[:1] == ["weather"]:
        argv = argv[1:]
    elif argv and not argv[0].startswith("-"):
        print("helm office: unknown subverb %r (the one subverb is weather)\n"
              "%s" % (_text(argv[0], 40), USAGE), file=sys.stderr)
        return 2
    from .cli import guard_tail
    rc = guard_tail("helm office weather", argv, flags=("--json", "--push"),
                    usage=USAGE)
    if rc is not None:
        return rc
    got = tick(push="--push" in argv)
    if "--json" in argv:
        print(json.dumps(got, sort_keys=True, ensure_ascii=False))
        return 0
    print(got["line"])
    for r in got["reasons"]:
        print("  %-6s %s" % (r["word"], r["why"]))
    s = got["settled"]
    print("  settled: %s" % ("%s since %s" % (s["word"], _clock(s["since"]))
                             if s else "nothing yet"))
    print("  phone: %s" % got["phone"])
    return 0
