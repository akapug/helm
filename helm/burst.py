#!/usr/bin/env python3
"""helm burst — may THIS seat exceed the one-delegate rule on the credential
its own home holds, right now, measured.

THE FAILURE THIS MODULE PREVENTS, and it was measured. A project was ORANGE,
set with the stated reason "only 1 max cred avail", and the seat rationed itself
to one delegate because that is what ORANGE says. At that same instant the
credential the seat was homed on read: session window 0% used resetting in
3h49m, weekly window 85% used resetting in 13h59m. The owner asked for a burst
of subagents and was right to — the remaining 15% of that week does not roll
over, and thirteen hours is not enough time to spend it one delegate at a time.

THE FLAG WAS NOT WRONG, WHICH IS THE WHOLE POINT. ORANGE is a POOL-axis
statement: most of the fleet's credentials are walled, so an arbitrary NEW
delegate probably cannot get served. That is true and it is useful. It says
nothing about whether a seat ALREADY HOMED on a named credential with measured
headroom and a near reset may spend it. One flag was being asked two different
questions, and the conservative answer to the pool question silently became the
answer to the per-credential one. This module answers only the second.

CAPACITY IS NEVER PERMISSION, AND THIS IS A CAPACITY ANSWER. `registry.admits`
already states the law in the other direction: a project's light outranks any
credential flag. Nothing here admits work a light refused, changes a colour, or
touches the fold. The exemption lifts a COUNT — "one delegate at a time" — and
lifts nothing else.

THE TWO RULES ORANGE WEARS AS ONE COLOUR, kept apart here on purpose:

  CAPACITY — "one delegate at a time". About how much may run. This is the
  rule the exemption may lift, because a measured credential can answer it.

  REACH — "do not start new work on a dark family". About whether a family can
  be reached at all. A burst onto a family whose proxy is in cooldown is wrong
  however much headroom the credential has, so this rule survives every
  exemption. It survives STRUCTURALLY, not by a check here: this module names
  ONE family (the native credential, which has no proxy seat at all — see
  `burnflags.NATIVE_FAMILY`) and is not consulted at any door that decides
  whether a family may be reached. `route` drops a RED or unreachable family at
  its N3 node, before any cap is computed; a record from here cannot travel to
  that node because nothing there imports this module.

THE SURPLUS IS THE MEASUREMENT, and it is one number per window:

    room       = 100 - used_percent                  (points of quota left)
    time_left  = 100 * (reset_at - now) / seconds    (percent of window left)
    surplus    = room - time_left

`surplus` is the points of this window's quota that WILL EXPIRE UNSPENT if the
seat keeps to the window's own average pace. It is exactly the quantity the
owner's sentence names — headroom that "needs burning in a limited timeframe" —
and it has the two properties the rule needs built in:

  * It is POSITIVE ONLY WHEN THE HEADROOM IS PERISHABLE. 15% left with 14 hours
    of a week to go is +6.7: real, and it evaporates. The same 15% left with
    six days to go is -70.7: the pacing policy is right and there is no
    exemption. One number, both conjuncts, no second threshold for "soon".

  * It DIES AT THE RESET. The arithmetic is meaningless one second past the
    instant it was computed against, so the grant carries that instant as its
    expiry. An exemption that outlives its measurement is the rule deleted.

THE TIGHTEST WINDOW DECIDES. The surplus is the MINIMUM across the credential's
account-wide windows, because spending is bounded by the first wall reached: a
weekly window with +6.7 to burn is worth nothing behind a session window at 95%
with four hours to run. A grant that read only the perishable window would
route a burst straight into the nearer wall.

A SCOPED WINDOW IS NOT THE CREDENTIAL'S WINDOW. `7d-fable` read 100% in the
measured episode while the account-wide week read 85%, and the owner was still
right to burst — on other models. A model-scoped sub-limit is a statement about
that model, so it never enters the surplus, and it is carried in the record and
printed so the grant cannot be read as covering the model that is already
walled. Scoped is recognised by the PRODUCER'S shape (`7d-<name>`, the same
test `burnflags._primary` uses), never by a list of names that goes stale.

A STALE READING IS NEVER HEADROOM. This module NEVER PROBES — it reads the
observation log the creds cycle already wrote, the same file and the same
freshness bound `burnflags` uses. A reading past that bound, an account with no
row, a window whose length cannot be parsed, a home whose identity cannot be
read: every one of them yields NO GRANT and says which one it was. The pool
flag keeps governing anything this module cannot measure, which is the default
and the safe direction — the usage telemetry that reported 1% weekly for an
account the owner's own `/usage` showed at 85% was forty days stale, and a
design that treats an unread number as room spends money on a lie.
"""
import os
import time

from . import burnflags

#: THE FAMILY THIS ANSWERS FOR, and the only one. A seat's claude home holds an
#: anthropic credential; the burst it licenses is spent there. Naming a second
#: family here would be the pool/per-credential conflation rebuilt one layer
#: down — a codex seat's window is not this seat's home.
FAMILY = burnflags.NATIVE_FAMILY


def floor_pct(ceiling=None):
    """The smallest surplus worth calling perishable headroom.

    A SURPLUS BELOW THIS IS NOISE, NOT A REASON. Derived from the one ceiling
    knob rather than minted: `burnflags.orange_pct` puts the warning band at
    TWICE the wall's distance from full, and this puts the floor at HALF it, so
    moving `HELM_CODEX_WEEKLY_CEILING_PCT` moves the wall, the band and this
    floor together instead of leaving a third number behind.

    HONEST CALIBRATION, stated where the number is: the measured episode
    carried +6.7 points of surplus and the default floor sits at 5.0, so that
    episode grants with 1.7 points of margin. This is one episode, not a fitted
    threshold; it is here to keep a hairline surplus reading as a licence.
    """
    ceiling = burnflags.ceiling_pct() if ceiling is None else ceiling
    return (100.0 - ceiling) / 2.0


def granted_capacity():
    """How many delegates a grant allows, IMPORTED NOT CHOSEN.

    A credential whose own windows are ahead of their own pace imposes no
    rationing of its own, so the count is the one the behaviour table already
    gives an unrationed family. An integer written here would be a second
    capacity policy beside the owner's own table."""
    return burnflags.BEHAVIOUR[burnflags.GREEN]["capacity"]


#: The states a record can carry. GRANTED is one of seven, deliberately: every
#: other one is a distinct reason no grant exists, and collapsing them would
#: hand a reader "no" with no repair attached.
GRANTED = "granted"
NOT_HOMED = "not-homed"          # no config dir, or its identity is unreadable
UNREAD = "unread"                # no row in the log for this account
STALE = "stale"                  # a row, past the freshness bound
UNREADABLE = "unreadable"        # a row whose status carries no gauges
UNSIZED = "unsized-window"       # a window with a reset nobody can size
NO_SURPLUS = "no-surplus"        # measured, and the headroom is not perishable
STATES = (GRANTED, NOT_HOMED, UNREAD, STALE, UNREADABLE, UNSIZED, NO_SURPLUS)


def homed_account(config_dir=None):
    """(account name, masked label, error) for the credential THIS seat's home
    holds.

    THE NAME IS THE JOIN KEY AND THE LABEL IS WHAT PRINTS. The observation log
    keys rows on the account name `providers` enumerates, which is the home's
    own oauth email; a reader needs only enough to tell two accounts apart, so
    every rendered line carries `accounts.mask_identity` of it instead. The
    unmasked name never leaves this function's caller's join.
    """
    from . import accounts as accounts_mod
    from . import cred
    from . import fixedtext
    home = config_dir or fixedtext.config_dir()
    if not home or not os.path.isdir(home):
        return None, None, "no readable claude config dir (%s)" % (home or "-")
    ident = cred.account_of(home)
    email = ident.get("email")
    if not email:
        return None, None, (ident.get("error")
                            or "the home carries no account identity")
    return email, accounts_mod.mask_identity(email), None


def _account_row(account, history=None):
    """The freshest observation for `account`, or None.

    `burnflags.usage_history` already folds the log to one row per account and
    resolves its path per call; this filters that fold rather than reading the
    file a second time, so a fixture home isolates both readers at once.
    """
    for row in (burnflags.usage_history() if history is None else history):
        if row.get("account") == account:
            return row
    return None


def _windows(row, now):
    """(account-wide window records, scoped window records, unsized flag).

    A GAUGE WITH NO RESET IS NOT A WINDOW. The overage gauge carries a
    utilization and no instant; it says nothing about what expires when, so it
    contributes no surplus and is not counted as an unsized window either.
    """
    wide, scoped, unsized = [], [], False
    for gauge in row.get("gauges") or ():
        label = gauge.get("label")
        util = gauge.get("utilization")
        reset = gauge.get("reset")
        if util is None:
            continue
        used = round(float(util) * 100, 1)
        if "-" in str(label or ""):
            # SCOPED BY THE PRODUCER'S OWN CONVENTION, not by a list of names.
            # `providers` builds every model-scoped window as "7d-<name>" and
            # `burnflags._primary` already picks the account-wide gauge with
            # exactly this test. Naming `7d-fable` alone here would have been a
            # list that goes stale the first time the vendor adds a second
            # scoped limit — and worse, `_label_seconds` cannot size those
            # labels, so an unlisted one would have landed in the unsized arm
            # below and silently blocked EVERY grant on this host.
            scoped.append({"label": label, "used_percent": used,
                           "reset_at": reset})
            continue
        if not reset:
            continue
        seconds = burnflags._label_seconds(label)
        if not seconds:
            # A WINDOW WITH A DEADLINE AND NO LENGTH CANNOT BE PACED, and an
            # unpaceable window is not a safe one. It blocks the grant rather
            # than being dropped from the minimum, because dropping it would
            # let an unread wall read as no wall at all.
            unsized = True
            continue
        left = max(0.0, min(1.0, (float(reset) - now) / float(seconds)))
        room = 100.0 - used
        wide.append({"label": label, "used_percent": used,
                     "reset_at": reset, "seconds": seconds,
                     "room_pct": round(room, 1),
                     "time_left_pct": round(left * 100, 1),
                     "surplus_pct": round(room - left * 100, 1)})
    return wide, scoped, unsized


def _record(state, account=None, label=None, **extra):
    rec = {"state": state, "granted": state == GRANTED, "family": FAMILY,
           "credential": label, "account": account,
           "surplus_pct": None, "floor_pct": round(floor_pct(), 1),
           "binding": None, "windows": [], "scoped": [],
           "capacity": None, "expires_at": None, "expires_kind": None,
           "window_reset_at": None,
           "measured_at": None, "reading_age_s": None,
           "max_age_s": burnflags.max_age_s(), "why": ""}
    rec.update(extra)
    return rec


def exemption(now=None, config_dir=None, history=None):
    """May this seat exceed the one-delegate rule on its own credential?

    Returns a record whose `granted` is the answer and whose every other field
    is the evidence it rests on. NEVER RAISES ACROSS THIS EDGE and never
    probes: the callers are doors that start work, and a door that stopped on
    an unreadable log would let a broken reader wall the fleet — the same law
    `registry.admits` keeps one layer up.
    """
    now = time.time() if now is None else now
    try:
        return _exemption(now, config_dir, history)
    except Exception as exc:                       # noqa: BLE001 — see above
        return _record(UNREAD, why="the burst reading itself failed (%s: %s) "
                                   "— no exemption, and the pool flag governs"
                                   % (exc.__class__.__name__, exc))


def _exemption(now, config_dir, history):
    from . import pk
    account, label, err = homed_account(config_dir)
    if not account:
        return _record(NOT_HOMED, why="this seat is not homed on a readable "
                                      "credential (%s) — the pool flag "
                                      "governs, which is what it is for" % err)
    row = _account_row(account, history=history)
    if row is None:
        return _record(UNREAD, account=account, label=label,
                       why="nothing in the usage log has observed %s — an "
                           "absent reading is not headroom" % label)
    probed = pk.parse_ts_epoch(row.get("probed_at"))
    age = None if not probed else now - probed
    bound = burnflags.max_age_s()
    if age is None or age > bound:
        how = ("carries no readable instant" if age is None
               else "is %ds old, past the %ds freshness bound" % (int(age),
                                                                  bound))
        return _record(STALE, account=account, label=label, measured_at=probed,
                       reading_age_s=None if age is None else int(age),
                       why="the only reading of %s %s — a stale reading is "
                           "never headroom" % (label, how))
    status = str(row.get("status") or "")
    if not status.startswith(burnflags.READ_STATUSES):
        # THE PROBE ALREADY WROTE THE REPAIR, so the refusal carries it whole
        # rather than the first word. Measured on a real host, where the
        # status read `reauth-needed (helm's token expired Nd ago; orca
        # refreshes its own store, not this one)` — the parenthesis is the
        # difference between "this account is spent" and "helm's COPY of the
        # token is dead, and one keepalive pass fixes it". It goes through
        # `redact_why` because a producer's free text reaches a terminal here.
        #
        # AND THE SENTENCE IS ABOUT THE READING, NEVER ABOUT THE CREDENTIAL.
        # Saying helm "cannot see this credential" is false wherever this
        # branch is reachable: the home is on disk, `helm cred` reports it, and
        # the seat reading this line may be RUNNING on it — the account here
        # was serving a live seat while its newest probe said reauth-needed.
        # The two readings send a reader to opposite repairs. "helm cannot see
        # it" points at re-authenticating the home, which mutates state the
        # whole fleet shares; "the newest observation carries no windows"
        # points at the cheap non-destructive refresh the status text already
        # names. A seat took the first reading off this line and told the owner
        # his working credential needed a re-login.
        return _record(UNREADABLE, account=account, label=label,
                       measured_at=probed, reading_age_s=int(age),
                       why="%s answered %s, which carries no windows — so "
                           "helm holds no MEASUREMENT to pace this burn "
                           "against. That is a fact about the newest reading "
                           "and not about the credential, which may be serving "
                           "this seat right now; the repair is whatever the "
                           "status above names"
                           % (label, burnflags.redact_why(status) or "nothing"))
    wide, scoped, unsized = _windows(row, now)
    base = dict(account=account, label=label, measured_at=probed,
                reading_age_s=int(age), windows=wide, scoped=scoped)
    if unsized:
        return _record(UNSIZED, why="%s carries a window with a reset and no "
                                    "readable length — an unpaceable window is "
                                    "not a measured one" % label, **base)
    if not wide:
        return _record(UNREAD, why="%s was read and carries no account-wide "
                                   "window — there is nothing to pace against"
                                   % label, **base)
    binding = min(wide, key=lambda w: w["surplus_pct"])
    surplus = binding["surplus_pct"]
    soonest = min(w["reset_at"] for w in wide)
    floor = floor_pct()
    if surplus < floor:
        return _record(NO_SURPLUS, surplus_pct=surplus,
                       binding=binding["label"],
                       why="%s's tightest window (%s) will expire %+.1f points "
                           "unspent at its own average pace, under the %.1f "
                           "floor — nothing here is perishable, so the pool "
                           "flag governs"
                           % (label, binding["label"], surplus, floor),
                       **base)
    # BOTH BOUNDS, AND THE NEARER ONE WINS. The arithmetic dies at the reset it
    # was computed against; the evidence dies when the reading goes stale. An
    # exemption that outlived either would be a standing licence wearing a
    # measurement's clothes.
    stale_at = probed + bound
    expires_at, kind = ((soonest, "window-reset") if soonest <= stale_at
                        else (stale_at, "reading-stale"))
    # IN PRACTICE THE READING EXPIRES FIRST, and that is the point rather than
    # a defect. The freshness bound is forty minutes and a window reset is
    # hours away, so a grant is a SHORT-LIVED answer that has to be re-measured
    # — which is exactly what stops it from becoming a standing licence. Both
    # instants are carried so a reader can tell "go and look again" from "the
    # headroom is gone".
    return _record(GRANTED, surplus_pct=surplus, binding=binding["label"],
                   capacity=granted_capacity(), expires_at=expires_at,
                   expires_kind=kind, window_reset_at=soonest,
                   why=_grant_why(label, binding, surplus, scoped), **base)


def _grant_why(label, binding, surplus, scoped):
    text = ("this seat is homed on %s, whose own %s window will expire %.1f "
            "points unspent at its average pace (%.1f%% of the window left, "
            "%.1f%% of the quota left) — perishable headroom the pool flag "
            "cannot see" % (label, binding["label"], surplus,
                            binding["time_left_pct"], binding["room_pct"]))
    walled = [w for w in scoped if w["used_percent"] >= 100.0]
    if walled:
        text += ("; NOT covered: %s"
                 % ", ".join("%s at %.1f%%" % (w["label"], w["used_percent"])
                             for w in walled))
    return text


def note(now=None, config_dir=None, history=None, record=None):
    """The one sentence a door prints under an ORANGE light, or None.

    ONLY A GRANT SPEAKS HERE. A door that printed every refusal of an
    exemption nobody asked for would bury the light's own reason under
    arithmetic about a credential that is behaving normally — and the light's
    reason is the thing the owner wrote for the reader.
    """
    rec = exemption(now=now, config_dir=config_dir,
                    history=history) if record is None else record
    if not rec.get("granted"):
        return None
    at = time.time() if now is None else now
    return ("BURST EXEMPTION — %s. Until %s (%s; the window itself resets %s), "
            "the one-delegate clause is lifted to %d for THIS seat on THIS "
            "credential; past that instant, MEASURE AGAIN rather than assume. "
            "It lifts the COUNT and nothing else: the light still decides "
            "whether work may start, a family whose proxy is dark is still "
            "dark, and the lanes opened must be work that is already built up "
            "— never speculative. Any delegate NOT on this credential is still "
            "governed by the flag."
            % (rec["why"], burnflags._when(rec["expires_at"], at),
               rec["expires_kind"],
               burnflags._when(rec["window_reset_at"], at), rec["capacity"]))


def render(rec, now=None):
    """The `helm burn burst` lines."""
    now = time.time() if now is None else now
    head = "helm burn burst — %s" % ("GRANTED" if rec["granted"]
                                     else "NO EXEMPTION (%s)" % rec["state"])
    if rec.get("credential"):
        head += "   credential %s" % rec["credential"]
    if rec.get("reading_age_s") is not None:
        head += "   reading %ds old (bound %ds)" % (rec["reading_age_s"],
                                                    rec["max_age_s"])
    out = [head, "  why   " + rec["why"]]
    if rec["granted"]:
        out.append("  cap   %d delegates until %s (%s); window resets %s"
                   % (rec["capacity"], burnflags._when(rec["expires_at"], now),
                      rec["expires_kind"],
                      burnflags._when(rec["window_reset_at"], now)))
    for w in rec["windows"]:
        out.append("  win   %-8s used %5.1f%%   window left %5.1f%%   "
                   "surplus %+6.1f   resets %s"
                   % (w["label"], w["used_percent"], w["time_left_pct"],
                      w["surplus_pct"], burnflags._when(w["reset_at"], now)))
    for w in rec["scoped"]:
        out.append("  scope %-8s used %5.1f%%   NOT part of the surplus — a "
                   "model-scoped limit is not the credential's window"
                   % (w["label"], w["used_percent"]))
    out.append("  floor a surplus under %+.1f is not perishable headroom"
               % rec["floor_pct"])
    return out
