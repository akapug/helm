#!/usr/bin/env python3
"""helm teams — each project's team, and the share of a short family it may
spend (task/3156).

THE TWO HALVES HELM ALREADY HAD. A project's LIGHT is permission: whether the
owner wants spending on it at all (`registry.light`). A family's BURN FLAG is
supply: how much the pooled accounts behind a family can carry
(`burnflags`). Supply is measured ONCE PER FAMILY because every project draws
on the same pools. What was missing is ALLOCATION: which seats serve which
project, and what share of a SHORT family each project may spend. Until this
module, four surfaces derived "the team" four different ways (the roster's
cwd basename, the home room, the spawn register, `project_for_cwd`) and each
picked one.

ONE RECORD, BESIDE THE LIGHT, THAT THE OWNER OWNS. The team is authored into
the same registry-authored entry that holds the project's `state`:

    "team": {"v": 6, "by": "owner", "ts": ..., "reason": "...",
             "members": [{"seat": ..., "family": ..., "role": ...}, ...],
             "shares": {"codex": 30, "ds4pro": 100}}

It is a NEVER-MIGRATED field for the light's reason: a projection must never be
able to write a team the router then obeys. `read` takes authorship from the
authored layer through `registry.authority_record` and never from a merged
record, so a `team` block planted in registry.json is not a team.

THE RULE, in one sentence (store premise
`project-shares-ration-a-short-family`). Shares take effect only while a
family is SHORT (ORANGE, and the money axis or an owner declaration set it;
`RATION_AXES`): inside its budget a project works normally (YELLOW),
up to twice its budget it is critical-path-only (ORANGE), and past twice its
budget it starts nothing new on that family (RED). A RED family is red for
every project; GREEN, YELLOW and GREY families are not rationed.

THE OWNER'S OPEN QUESTIONS, BUILT AS DEFAULTS HE CAN FLIP (spec Part A):
  Q1  `burn declare` stays worse-only; shares are the lever. No whole-family
      override in v1.
  Q2  THE DOORS ADVISE (`ADVISE_ONLY`). `dispatch send` and `work claim`
      print the project's effective colour as a NOTE and refuse nothing;
      the advise-then-decide week stands. `route` is a recommendation and
      reads the project's colour at N3 like any flag, so a family RED on
      the project's share is DROPPED from its answer (edge E33).
  Q3  every write records `by`; nothing is refused by author in v1 (kept by
      the design read, D7: the TEAM-CHANGED notice and `by` are the trace).
      The web writes as "owner"; an agent running the verb from chat writes
      as its seat, on the owner's behalf.
  Q4  a seat may sit on several teams (`SHARED_SEATS_ALLOWED`). Its spend is
      SHARED: it counts toward the family total and is attributed to no
      project.
  Q5  a red light releases the project's shares (`RED_LIGHT_RELEASES_SHARE`).
  Q6  one team per project; `derive` places a seat by its home room first, so
      an alias key that no seat is homed to gets no team of its own.

LOCAL LANES ARE SLOTS, SHARED AND CAPACITY-BOUND, a benefit for every
project lead rather than one project's arrangement. A family served from the
operator's own
GPUs (qwen27, qwenlocal, any lane whose host comes from the local endpoints
file) spends no money; what runs short is CONCURRENT LANES. Its supply is a
measured capacity (`helm team capacity <family> <lanes>`, written by the seat
that measured it) and each team's share of it is a slot share: slots = share x
capacity. In use is the owed non-build rows addressed to that family's seats
for that project; inside its slots a project reads YELLOW, over them ORANGE,
and `route` still offers the family's seat, marked QUEUED rather than refused.
Until a capacity is recorded the family says "capacity not measured", never a
number. The same record, the same shares, the same allocation: slots are a
mode of it, not a second mechanism.

WHAT THIS MODULE NEVER DOES: start, stop or re-home a seat, probe a vendor, or
refuse work. It records what the owner decided and folds readings other
passes already wrote. The lead fixes drift with the tools it already has.
"""
import contextlib
import copy
import math
import os
import time

from . import home, pk

# ---------------------------------------------------------------------------
# the vocabulary
# ---------------------------------------------------------------------------

ROLES = ("lead", "builder", "reviewer", "checker")

# WHAT EACH ROLE TAKES, in `route.KINDS` words. `None` is every kind: the lead
# owns the team and may be handed anything.
ROLE_KINDS = {"lead": None,
              "builder": ("build", "delegate", "review"),
              "reviewer": ("review", "verify", "council"),
              "checker": ("verify", "research")}

# The owner's defaults, named so the flip is one line (see the docstring).
ADVISE_ONLY = True
SHARED_SEATS_ALLOWED = True
RED_LIGHT_RELEASES_SHARE = True

EVENTS_NAME = "team-events.jsonl"
NOTICE_TAG = "TEAM-CHANGED"
BUDGET_TAG = "PROJECT-BUDGET"
BUDGET_LATCH_NAME = "project-budget.json"

# THE MODES, one per kind of supply reading a family has.
#   rate      a sustainable token rate is measured (codex: runway x rate /
#             horizon), so a share becomes a budget per hour.
#   relative  burn is measured and supply is not: the project's slice of the
#             family's burn is compared with its share.
#   accounts  the native credential: each seat spends the account its own home
#             holds, so a project's share is the accounts homed to its seats.
#   slots     supply is CONCURRENT LANES, not money: a local family (and
#             any family whose lane capacity was recorded) turns a share
#             into slots = share x capacity, measured against the rows in
#             use. With no capacity recorded, and for a family nothing
#             measures at all, the share is advisory seat count only.
RATE, RELATIVE, ACCOUNTS, SLOTS = "rate", "relative", "accounts", "slots"
MODES = (RATE, RELATIVE, ACCOUNTS, SLOTS)

# THE LANE CAPACITY FILE: {"v": 1, "families": {family: {"lanes", "by", "ts",
# "reason"}}}, one current value per family, written by `set_capacity` (the
# seat that measured the lanes), read by every allocation. State, not
# authored canon: losing it reads "capacity not measured" until it is
# measured again.
CAPACITY_NAME = "slot-capacity.json"
CAPACITY_V = 1
MAX_LANES = 256

# THE ROWS THAT HOLD A LANE: an owed row addressed to a slot family's seat,
# of any kind but new work. The owner's words are "open review rows"; a
# verification, a council or research on a local seat holds a lane the same
# way, and a build row is not a reader's work.
NOT_A_LANE = ("build", "delegate")

# THE AXES WHOSE ORANGE A SHARE RATIONS (design read D1 on task/3156). A
# flag's headline `axis` names the axis that set its colour. Shares exist to
# ration MONEY: the pools behind a family running short before the reset, or
# the owner declaring that they are. An ORANGE that reach set (seats of the
# family not answering) or policy set is not a shortage a project's share can
# spend less of, so every project reads the family's own ORANGE. Rationing it
# made a reach flap move every project's colour, and the watchdog posted a
# PROJECT-BUDGET line on each flap.
RATION_AXES = ("money", "declared")

# THE TWO BREAKPOINTS of the rule. A ratio of exactly 1.0 is inside the
# budget and exactly 2.0 is still ORANGE: "more than twice" is what RED says.
IN_BUDGET = 1.0
TWICE = 2.0

SHARE_MIN, SHARE_MAX = 0, 100

# HOW RECENTLY A SEAT MUST HAVE BEEN SEEN TO BE PROPOSED FOR A TEAM. The roster
# keeps rows for seats nobody has run in weeks; proposing them would seat the
# dead. Three days covers a weekend away and nothing older.
DERIVE_SEEN_S = 3 * 86400

# The room every seat can read and no project owns. A seat homed there has no
# project home, so its project falls back to where it works.
MAIN_ROOM = "main"

# The native credential's family word, and the harness word a roster row or a
# spawn register writes for it. Imported lazily below where a module is needed;
# these two are spelled here because every reader of a team needs them.
NATIVE_FAMILY = "anthropic"


def events_path():
    """The team history: one line per team write and per light change."""
    return os.path.join(home.global_dir(), EVENTS_NAME)


def budget_latch_path():
    return os.path.join(home.global_dir(), ".state", BUDGET_LATCH_NAME)


def capacity_path():
    return os.path.join(home.global_dir(), ".state", CAPACITY_NAME)


def families():
    """The family vocabulary a member may name — the burn flags' own."""
    from . import burnflags
    return burnflags.families()


def kinds_of(role):
    """The route kinds a role takes, as a tuple."""
    from . import route
    got = ROLE_KINDS.get(role)
    return tuple(route.KINDS) if got is None else tuple(got)


def takes(role, kind):
    return kind in kinds_of(role)


# ---------------------------------------------------------------------------
# validation — one law under every door
# ---------------------------------------------------------------------------

def _seat_ok(seat):
    return isinstance(seat, str) and bool(home._SEAT_NAME_RE.match(seat))


def validate(team):
    """(clean team, problem) — the shape every write must have.

    `team` carries `members` and `shares`; the version, author, time and
    reason are stamped by `write` and are not the caller's to set. A member
    whose seat is not running is NOT a problem here: that is drift, and a team
    that names a seat the lead is about to start is the ordinary case."""
    if not isinstance(team, dict):
        return None, "a team is a mapping with members and shares"
    fams = set(families())
    members, seen = [], set()
    for m in team.get("members") or ():
        if not isinstance(m, dict):
            return None, "every member is {seat, family, role}"
        seat, fam, role = m.get("seat"), m.get("family"), m.get("role")
        if not _seat_ok(seat):
            return None, ("%r is not a seat name (a seat name is "
                          "[A-Za-z0-9._-], like codex-2)" % (seat,))
        if seat.casefold() in seen:
            return None, "%s is on the team twice" % seat
        seen.add(seat.casefold())
        if fam not in fams:
            return None, ("%s's family %r is not a family helm measures (%s)"
                          % (seat, fam, ", ".join(sorted(fams))))
        if role not in ROLES:
            return None, ("%s's role %r is not one of %s"
                          % (seat, role, ", ".join(ROLES)))
        members.append({"seat": seat, "family": fam, "role": role})
    raw = team.get("shares") or {}
    if not isinstance(raw, dict):
        return None, "shares are {family: percent}"
    shares = {}
    for fam, pct in raw.items():
        if fam not in fams:
            return None, "a share names %r, which is not a family" % (fam,)
        if isinstance(pct, bool) or not isinstance(pct, int) \
                or not SHARE_MIN <= pct <= SHARE_MAX:
            return None, ("the %s share must be a whole number from %d to %d"
                          % (fam, SHARE_MIN, SHARE_MAX))
        shares[fam] = pct
    return {"members": members, "shares": dict(sorted(shares.items()))}, None


def _authored_shape(said):
    """Is this authored value a team record at all? A malformed one is read
    as NO team (the derived proposal stands) and the reason travels."""
    if not isinstance(said, dict):
        return "the authored team is not a mapping"
    v = said.get("v")
    if isinstance(v, bool) or not isinstance(v, int) or v < 1:
        return "the authored team carries no version"
    _clean, problem = validate(said)
    return problem


# ---------------------------------------------------------------------------
# the read
# ---------------------------------------------------------------------------

def authored(name, rec, auth):
    """(team, problem) THE OWNER WROTE for `name`, or (None, why-not).

    AUTHORSHIP COMES FROM THE AUTHORED LAYER, NEVER FROM `rec`, for
    `registry.light`'s reason: `rec` is the merged record, and a `team` block
    sitting inline in registry.json reaches the merge looking exactly like one
    the owner wrote."""
    from . import registry
    _key, entry = registry.authority_record(auth, name,
                                            (rec or {}).get("path"))
    said = entry.get("team") if isinstance(entry, dict) else None
    if said is None:
        return None, None
    problem = _authored_shape(said)
    if problem:
        return None, problem
    return copy.deepcopy(said), None


def read(project, reg=None, auth=None, world=None):
    """THE ONE READ of a project's team -> a record, never None.

        {project, authored, v, by, ts, reason, members, shares, problem}

    `authored` is the same provenance law `registry.light` keeps: the authored
    team when there is one, else `derive`'s proposal from today's seats, and
    the flag says which. A proposal binds nothing — until the owner accepts a
    team every agent behaves as it did before teams existed.

    `world` is `placements`' input bundle, so a caller asking about many
    projects reads the roster once."""
    from . import registry
    reg = registry.load(strict=True) if reg is None else reg
    auth = registry._authored_load(strict=True) if auth is None else auth
    rec = (reg.get("projects") or {}).get(project)
    said, problem = authored(project, rec, auth) if rec is not None \
        else (None, "%s is not a project in the registry" % project)
    if said is not None:
        return {"project": project, "authored": True, "v": said["v"],
                "by": said.get("by") or "", "ts": said.get("ts"),
                "reason": said.get("reason") or "",
                "members": said.get("members") or [],
                "shares": dict(said.get("shares") or {}), "problem": None}
    members = derive(project, world=world, projects=reg.get("projects"))
    return {"project": project, "authored": False, "v": 0, "by": "",
            "ts": None, "reason": "", "members": members, "shares": {},
            "problem": problem}


def read_all(reg=None, auth=None, world=None, keys=None):
    """{project: read(project)} for every project that has a team: an authored
    one, or a proposal with at least one member. One registry read, one roster
    read."""
    from . import registry
    reg = registry.load(strict=True) if reg is None else reg
    auth = registry._authored_load(strict=True) if auth is None else auth
    projects = reg.get("projects") or {}
    world = placements(projects=projects) if world is None else world
    out = {}
    for key in sorted(projects if keys is None else keys):
        if key not in projects:
            continue
        got = read(key, reg=reg, auth=auth, world=world)
        if got["authored"] or got["members"]:
            out[key] = got
    return out


def settled_role(seat, reg=None):
    """The role a WHOLE read of the teams positively gives `seat`, else None.

    None is "this read cannot settle it", never "not a lead": the roster did
    not read, the integrator did not resolve (role_for then hands its lead to
    a `<project>-claude` by name), the seat is in no team, or a team it is in
    names no lead or several (a lone-native lead stops leading the moment a
    second native is placed beside it). A caller that withholds anything on
    a role must treat None as the answer it gave before teams existed: a
    proposal binds nothing until the owner accepts a team. `read_all`'s roles
    are the reader, never a second opinion about what a lead is."""
    from . import registry
    reg = registry.load(strict=True) if reg is None else reg
    world = placements(projects=reg.get("projects") or {})
    if world.get("roster_unread") or not world.get("integrator"):
        return None
    roles = []
    for t in read_all(reg=reg, world=world).values():
        members = t.get("members") or ()
        mine = [m.get("role") for m in members if m.get("seat") == seat]
        if not mine:
            continue
        if sum(m.get("role") == "lead" for m in members) != 1:
            return None
        roles += mine
    return "lead" if "lead" in roles else (roles[0] if roles else None)


# ---------------------------------------------------------------------------
# derive — the migration seed
# ---------------------------------------------------------------------------

def _alias(word):
    """The harness word a roster row or register writes -> the flag word."""
    from . import route
    word = str(word or "").lower()
    return route.FROM_ALIASES.get(word, word) or None


def family_of(seat, row=None):
    """The burn-flag family a seat spends, or None — asked of
    `seat_usability.burn_family`, the one door the web board and the
    signing-liveness read ask, so the team card and every other surface that
    colours a seat give one answer and keep one ordering (coordinator ruling
    on task/3156). `row` is the seat's roster row; `seat` is its key."""
    from . import seat_usability
    row = row if isinstance(row, dict) else {}
    return seat_usability.burn_family(dict(row, seat=seat))


class RosterUnread(RuntimeError):
    """The seat roster did not read (`roster_checked` said FAILED)."""


class LedgerUnread(RuntimeError):
    """The dispatch ledger the lanes in use are folded from did not read."""


class CapacityUnread(RuntimeError):
    """The lane capacity file exists and did not read."""


def _roster(strict=False):
    """{seat: row} — the roster through its checked reader, THE ONE READ
    this module makes of it (tests/test_display_launder_tripwire.py pins
    one call site). For a non-strict reader an unreadable roster is empty:
    each seat's family is then read from its name and its spawn register
    alone. A STRICT reader (route and the doors, design read D6) is told
    instead: `roster_checked` reports a failure without raising, and read
    as empty it benched and billed every member on its typed family, on an
    answer that read whole."""
    from .seats_roster import roster_checked
    roster, failed = roster_checked()
    if failed and strict:
        raise RosterUnread("the seat roster did not read")
    return {} if failed else (roster or {})


def live_families(seats, roster=None, strict=False):
    """{seat: family} for every seat in `seats` the family door (`family_of`)
    names. A seat it names nothing for is left out, and keeps the family its
    team typed.

    THE FAMILY DOOR RULES (design read D3 on task/3156): a member's family
    was authored twice — the typed word on the team and what the seat
    actually spends — and nothing reconciled them, so allocation billed a
    seat to one family while route benched it on another. Every reader of a
    team member's family asks this. `roster` is {seat: row}; left out, it is
    read once, and `strict` raises `RosterUnread` when it does not read.
    The rows are looked up by the member's seat name, which `validate`
    admitted; no roster key is iterated or emitted here."""
    roster = _roster(strict) if roster is None else (roster or {})
    out = {}
    for seat in sorted(set(seats or ())):
        row = roster.get(seat)
        fam = _alias(family_of(seat, row if isinstance(row, dict) else None))
        if fam:
            out[seat] = fam
    return out


def family_disagrees(members, families_of=None):
    """The refusal for the first member whose KNOWN family is not the family
    written for it, or None (design read D3). `families_of` is a seam,
    {seats} -> {seat: family}; left out, `live_families`, read STRICTLY: a
    roster that did not read raises `RosterUnread` (round 3, ruling c), never
    an empty roster that waves every typed family through. A seat the family
    door names nothing for (not registered, not started) keeps the typed
    family: a team that names the seat the lead is about to start is the
    ordinary case."""
    known = (families_of or (lambda seats: live_families(seats, strict=True)))(
        [m["seat"] for m in members]) or {}
    for m in members:
        # ONE WORD PER FAMILY ON BOTH SIDES: the alias table route and the
        # burn-flag door share (`claude` is `anthropic`), so a door that
        # answers in the harness's word is not a different family
        spends, typed = _alias(known.get(m["seat"])), _alias(m["family"])
        if spends and spends != typed:
            return ("%s spends %s, not %s: the family door (its verified "
                    "runtime, its name or its spawn register) names what a "
                    "seat spends, and a team names the same family. Add it "
                    "as %s:%s:%s, or leave the family off"
                    % (m["seat"], spends, m["family"], m["seat"], m["role"],
                       spends))
    return None


def _register(seat, registers=None):
    """(family, project) the seat's spawn register names, or (None, None).
    `place` reads only the PROJECT from it; a seat's family is
    `family_of`'s, whatever a register seam says."""
    if registers is not None:
        got = registers.get(seat) if isinstance(registers, dict) \
            else registers(seat)
        return tuple(got) if got else (None, None)
    try:
        from . import seat as seat_mod
        return seat_mod.registered_seat_family(seat)
    except Exception:                       # noqa: BLE001 — unread is None
        return None, None


def _local_families():
    """Families served by the operator's own box: a pool row whose host comes
    from the local endpoints file. Derived from the catalog, never listed."""
    try:
        from . import seat  # noqa: F401 — the facade first (seat_compat)
        from .seat_catalog import FAMILIES as table
    except Exception:                       # noqa: BLE001
        return frozenset()
    out = set()
    for name, fam in table.items():
        for row in ((fam or {}).get("pool_providers") or {}).values():
            if isinstance(row, dict) and row.get("base_url_from"):
                out.add(name)
    return frozenset(out)


def _presence(seat, row, now):
    """live / quiet / absent, off the roster's own presence rule."""
    try:
        from .seats_report import presence_of
        from .seats_roster import last_seen
        said = presence_of(last_seen(seat, row))
    except Exception:                       # noqa: BLE001
        said = None
    return {"fresh": "live", "quiet": "quiet"}.get(said, "absent")


def _home_room(row):
    """The row's home room, or None. A room is printed in drift lines, so
    one that is not a plain token is read as no home room, the same door
    as the seat key (`placements`)."""
    room = str((row or {}).get("home_room") or "").strip()
    return None if not _seat_ok(room) or room == MAIN_ROOM else room


def _path_of(projects, key):
    rec = (projects or {}).get(key)
    return os.path.realpath(rec.get("path")) \
        if isinstance(rec, dict) and rec.get("path") else None


def _inside(inner, outer):
    return bool(inner and outer) and (inner == outer or inner.startswith(
        outer.rstrip(os.sep) + os.sep))


def place(seat, row, projects, registers=None, project_for_cwd=None):
    """(project key, source) for one seat, or (None, why).

    THE ORDER IS THE SPEC'S: the roster home room, then the spawn register's
    project, then the directory the seat works in. A home of `#main`, or no
    home at all, is not a project home.

    AN ALIAS KEY NEVER WINS OVER THE KEY THE SEAT WORKS IN (Q6). One checkout
    can sit under two registry keys — an umbrella directory and the repository
    inside it — and a register written against the umbrella would otherwise
    invent a second team for a key no seat is homed to. When the register's
    project is an ANCESTOR of the project the seat's directory resolves to,
    the deeper key is the one the seat serves."""
    keys = set(projects or ())
    room = _home_room(row)
    if room and room in keys:
        return room, "home room"
    if project_for_cwd is None:
        from .inject._ledger import project_for_cwd
    cwd = (row or {}).get("cwd")
    try:
        here = project_for_cwd(cwd, projects=projects) if cwd else None
    except Exception:                       # noqa: BLE001
        here = None
    _fam, reg_project = _register(seat, registers)
    if reg_project and reg_project in keys:
        if here and here != reg_project and _inside(
                _path_of(projects, here), _path_of(projects, reg_project)):
            return here, "working directory (inside the register's %s)" \
                % reg_project
        return reg_project, "spawn register"
    if here and here in keys:
        return here, "working directory"
    return None, "no home room, register or working directory names a project"


def role_for(seat, family, project, integrator=None):
    """The role `derive` proposes: the integrator leads the project it is
    placed on, and `<project>-claude` leads any other; any other native seat
    builds; a codex seat builds; every proxy seat, local or not, is a shared
    reviewer. `integrator` is the integrator's seat ONLY when it is placed on
    this project.

    A LOCAL SEAT REVIEWS. A local lane is a REVIEWER lane offered to every
    project whose team holds it, and a checker takes no review, so a
    proposal that seated it as one would route none to it. A lead who
    wants a checker still says so in one word on the card."""
    if family == NATIVE_FAMILY:
        if seat == integrator:
            return "lead"
        if seat == "%s-claude" % project and not integrator:
            return "lead"
        return "builder"
    if family == "codex":
        return "builder"
    return "reviewer"


_ASK = object()


def placements(roster=None, projects=None, registers=None, now=None,
               integrator=_ASK, project_for_cwd=None):
    """{seats: {seat: {project, source, family, presence, home_room}},
    integrator, at, roster} for every seat the roster saw within
    `DERIVE_SEEN_S` — the world `derive` and `drift` read, built once.

    Every reader is a seam: `roster` ({seat: row}), `registers` ({seat:
    (family, project)} or a callable; its project places a seat, its family
    decides nothing), `integrator` (a seat name, or None for none; left out,
    the roster is asked) and `project_for_cwd`. A seat's family is always
    `family_of`, the door every other surface asks."""
    now = time.time() if now is None else now
    if projects is None:
        from . import registry
        projects = (registry.load(strict=True).get("projects") or {})
    roster_unread = None
    if roster is None:
        try:
            roster = _roster(strict=True)
        except RosterUnread as exc:
            # READ ON, AND SAID (round 3, ruling c): the card still draws its
            # teams, and names the roster as a reader that did not read
            roster, roster_unread = {}, exc.__class__.__name__
    if integrator is _ASK:
        try:
            from .seats_integrator import integrator_seat
            integrator, _why = integrator_seat(snapshot=roster)
        except Exception:                   # noqa: BLE001
            integrator = None
    seats = {}
    for seat in sorted(roster or {}):
        # A ROSTER KEY IS UNVALIDATED at the join seam (a hostile
        # HELM_CHAT_NAME can carry ESC or bidi), and every seat placed here
        # can leave this module: a proposed member, a drift line, the
        # add-seat picker. So a key that is not a seat name is never placed.
        # `validate` refuses it as a member anyway; the raw roster still
        # answers membership (tests/test_display_launder_tripwire.py).
        if not _seat_ok(seat):
            continue
        row = roster[seat] if isinstance(roster[seat], dict) else {}
        seen = row.get("last_seen")
        if not isinstance(seen, (int, float)) or now - seen > DERIVE_SEEN_S:
            continue
        key, source = place(seat, row, projects, registers=registers,
                            project_for_cwd=project_for_cwd)
        seats[seat] = {"project": key, "source": source,
                       "family": family_of(seat, row),
                       "presence": _presence(seat, row, now),
                       "home_room": _home_room(row)}
    return {"seats": seats, "integrator": integrator, "at": now,
            "roster": roster or {}, "roster_unread": roster_unread}


def derive(project, world=None, projects=None, **kw):
    """The members `derive` proposes for `project`, from `placements`. A seat
    whose family helm cannot name is left out: a member must have a family.
    So is one whose family helm does not MEASURE (not in the burn-flag
    vocabulary, like a family the catalog has just gained): `write` refuses
    such a member, and a proposal the owner cannot accept is no proposal.
    `drift` names the seat and why instead."""
    world = placements(projects=projects, **kw) if world is None else world
    vocab = frozenset(families())
    seats = world.get("seats") or {}
    lead = world.get("integrator")
    here = lead if (seats.get(lead) or {}).get("project") == project else None
    out = []
    for seat, p in sorted(seats.items()):
        if p["project"] != project or p["family"] not in vocab:
            continue
        out.append({"seat": seat, "family": p["family"],
                    "role": role_for(seat, p["family"], project,
                                     integrator=here)})
    # THE ONE NATIVE SEAT LEADS where neither rule named a lead. A seat named
    # for an alias key (`<alias>-claude` working on the key it is homed to)
    # or numbered (`<project>-claude-2`) matches no `<project>-claude`, and a
    # proposal with no lead is drift the owner did not make. Two or more
    # native seats and no rule: nobody is guessed, and drift says "no lead".
    natives = [m for m in out if m["family"] == NATIVE_FAMILY]
    if len(natives) == 1 and not any(m["role"] == "lead" for m in out):
        natives[0]["role"] = "lead"
    return out


# ---------------------------------------------------------------------------
# the write
# ---------------------------------------------------------------------------

def diff(was, now):
    """The change between two teams, as the lines a person reads."""
    was = was or {}
    out = []
    old = {m["seat"]: m for m in was.get("members") or ()}
    new = {m["seat"]: m for m in now.get("members") or ()}
    for seat in sorted(set(old) | set(new)):
        a, b = old.get(seat), new.get(seat)
        if a and not b:
            out.append("- %s" % seat)
        elif b and not a:
            out.append("+ %s (%s, %s)" % (seat, b["role"], b["family"]))
        elif a["role"] != b["role"] or a["family"] != b["family"]:
            out.append("%s role %s → %s" % (seat, a["role"], b["role"])
                       if a["role"] != b["role"] else
                       "%s family %s → %s" % (seat, a["family"], b["family"]))
    so, sn = was.get("shares") or {}, now.get("shares") or {}
    for fam in sorted(set(so) | set(sn)):
        if so.get(fam) != sn.get(fam):
            out.append("%s share %s → %s" % (fam, pct(so.get(fam)),
                                              pct(sn.get(fam))))
    return out


def pct(share):
    """30% / unset — a share as a person reads it. AN UNSET SHARE IS NOT 0%
    (design read D2): 0% is the owner's word and rations the project to
    nothing, unset rations nothing."""
    return "unset" if share is None else "%d%%" % share


def notice(prev, new):
    """The TEAM-CHANGED room body for one write, modelled on
    `burnflags.watch_notice`: the tag, the project, the versions, who and why,
    then one line per change."""
    project = new.get("project") or "?"
    lines = diff(prev, new)
    head = "%s %s v%d → v%d by %s: \"%s\"" % (
        NOTICE_TAG, project, (prev or {}).get("v") or 0, new.get("v") or 0,
        new.get("by") or "an unnamed terminal", new.get("reason") or "")
    body = [head] + ["  " + line for line in lines]
    if not lines:
        body.append("  (no member or share changed)")
    body.append("  the lead reads `helm team %s` for what to start or move; "
                "routing reads the team from now on" % project)
    return "\n".join(body)


def _append_event(row):
    """One history line. The event ledger's grammar requires an `id`: a team
    write is unique by (project, version) under the compare-and-set, and a
    light change carries a random suffix."""
    import uuid
    from . import eventledger
    ident = ("team:%s:v%d" % (row["project"], row["v"])
             if row.get("kind") == "team" else
             "%s:%s:%s" % (row.get("kind"),
                           row.get("project") or row.get("family"),
                           uuid.uuid4().hex[:12]))
    return eventledger.append(events_path(), dict(row, id=ident))


def record_light(name, was, now, by, apply):
    """One history line for a light change — `registry._state` calls this, so
    the card shows the light and the team in one history. Best effort: a
    history that could not be written never un-writes the light."""
    if not apply:
        return None
    def colour(v):
        return (v or {}).get("colour") if isinstance(v, dict) else None
    try:
        return _append_event({
            "kind": "light", "project": name, "v": None,
            "by": (now or {}).get("by") or by or "",
            "ts": (now or {}).get("ts") or int(time.time()),
            "reason": (now or {}).get("reason") or "",
            "diff": ["light %s → %s" % (colour(was) or "(the scan's)",
                                         colour(now) or "(the scan's)")]})
    except Exception:                       # noqa: BLE001
        return False


def write(project, team, expected_v, by="", reason="", apply=False, now=None,
          post=None, families_of=None):
    """Author `project`'s team -> (row, problem, code).

    COMPARE-AND-SET, the friction dial's model (`friction.set_dial`).
    `expected_v` is the version the caller was LOOKING AT — 0 for a proposed
    team — and a write against any other version writes nothing and answers
    code `stale`. The check and the write share the registry's write lock, so
    two saves cannot both be judged against the same version.

    `code` is "invalid" for a team, reason or project the door refuses,
    "stale" for a version that moved and "unread" when the roster the family
    door checks members against did not read; none writes anything. A member
    whose
    known family is not the one written is invalid, naming the family it
    spends (`family_disagrees`; `families_of` is its seam). A dry run
    (`apply=False`) validates and diffs and writes nothing at all.

    On an applied write the history gains one line and the project's room one
    TEAM-CHANGED notice, keyed on (project, version) so a retry cannot post it
    twice. Neither failing un-writes the team: `event` and `posted` say so."""
    from . import registry
    now = int(time.time()) if now is None else int(now)
    reason = str(reason or "").strip()
    if not reason:
        return None, ("a team change needs a reason (it rides the room "
                      "notice and the history)"), "invalid"
    if isinstance(expected_v, bool) or not isinstance(expected_v, int) \
            or expected_v < 0:
        return None, "the version you edited is required (--expect V)", \
            "invalid"
    clean, problem = validate(team)
    if problem:
        return None, problem, "invalid"
    try:
        problem = family_disagrees(clean["members"], families_of)
    except RosterUnread as exc:
        # REFUSED, NOT WAVED THROUGH (round 3, ruling c): with no roster the
        # family door cannot check a single member
        return None, ("the seat roster did not read (%s), so the family "
                      "door cannot check which family each member spends; "
                      "nothing was written" % exc.__class__.__name__), \
            "unread"
    if problem:
        return None, problem, "invalid"
    with registry._write_lock() if apply else contextlib.nullcontext():
        reg = registry.load(strict=True)
        auth = registry._authored_load(strict=True)
        rec = (reg.get("projects") or {}).get(project)
        if rec is None:
            return None, "unknown project '%s'" % project, "invalid"
        path = rec.get("path")
        if not path or not os.path.isabs(path):
            return None, "project path is not an absolute identity", "invalid"
        was, bad = authored(project, rec, auth)
        current = was["v"] if was else 0
        if expected_v != current:
            return None, ("the team is v%d now and this change was made "
                          "against v%d, so nothing was saved; reload and "
                          "make it again" % (current, expected_v)), "stale"
        base = was or {"v": 0, "members": derive(
            project, projects=reg.get("projects")), "shares": {}}
        new = dict(clean, v=current + 1, by=by or "", ts=now, reason=reason)
        lines = diff(base, new)
        slot = registry._authority_slot(auth, project, path)
        entry = auth.setdefault("projects", {}).setdefault(slot, {})
        entry.setdefault("path", path)
        entry["team"] = new
        event = None
        if apply:
            pk.write_json(home.authored_path(), auth)
            event = _append_event({"kind": "team", "project": project,
                                   "v": new["v"], "by": new["by"], "ts": now,
                                   "reason": reason, "diff": lines})
    body = notice(base, dict(new, project=project))
    posted = None
    if apply:
        posted = _post(body, project, new, post)
    return {"project": project, "was": was, "team": new, "v": new["v"],
            "diff": lines, "notice": body, "event": event, "posted": posted,
            "replaced_malformed": bad}, None, None


def _post(body, project, new, post=None):
    """Post the notice to the project's room once -> True/False."""
    try:
        if post is not None:
            post(body, project)
            return True
        from . import chat
        chat.post(body, room=project, who="teams", sign=False,
                  event_id="team-changed:%s:v%d" % (project, new["v"]))
        return True
    except Exception:                       # noqa: BLE001 — the team stands
        return False


def history(project, path=None):
    """(rows for `project`, newest last, unavailable reason)."""
    from . import eventledger
    rows, err = eventledger.checked_events(path or events_path())
    return [r for r in rows if isinstance(r, dict)
            and r.get("project") == project], err


# ---------------------------------------------------------------------------
# allocation — shares into budgets, budgets into colours
# ---------------------------------------------------------------------------

def short(flag):
    """Is this family SHORT in the sense a share rations? ORANGE, and the
    axis that set the ORANGE is money or an owner declaration
    (`RATION_AXES`). PURE."""
    from . import burnflags as bf
    flag = flag if isinstance(flag, dict) else {}
    return flag.get("colour") == bf.ORANGE and flag.get("axis") in RATION_AXES


def effective_colour(family_colour, ratio):
    """The colour a project reads for one family. PURE.

    RED stays RED for everyone. GREY, GREEN and YELLOW are unchanged: shares
    ration only a short family. ORANGE becomes YELLOW inside the budget
    (ratio <= 1.0), stays ORANGE up to twice it (<= 2.0), and is RED past
    twice it. A ratio nobody measured leaves the family's colour standing."""
    from . import burnflags as bf
    if family_colour == bf.RED:
        return bf.RED
    if family_colour != bf.ORANGE or ratio is None:
        return family_colour
    if ratio <= IN_BUDGET:
        return bf.YELLOW
    if ratio <= TWICE:
        return bf.ORANGE
    return bf.RED


def sustainable_per_h(pace):
    """runway_h x tokens_per_hour / horizon_h, or None when any is missing.

    That is the fleet's SUPPLY over the horizon (runway is supply over rate,
    so the rate cancels): the rate at which the pool lasts until the reset."""
    pace = pace or {}
    runway, rate, horizon = (pace.get("runway_h"), pace.get("tokens_per_hour"),
                             pace.get("horizon_h"))
    if not all(isinstance(x, (int, float)) and not isinstance(x, bool)
               for x in (runway, rate, horizon)) or horizon <= 0:
        return None
    return runway * rate / horizon


def mode_of(family, pace, burn, slots=None):
    """Which reading a family's share is measured against. A SLOT family —
    local, or with a lane capacity recorded — is measured in lanes even when
    the proxy also counts its tokens: its lanes run short, never its money."""
    if family == NATIVE_FAMILY:
        return ACCOUNTS
    if sustainable_per_h((pace or {}).get(family)) is not None:
        return RATE
    if family in ((slots or {}).get("families") or ()):
        return SLOTS
    fam = ((burn or {}).get("families") or {}).get(family) or {}
    if isinstance(fam.get("per_hour"), (int, float)):
        return RELATIVE
    return SLOTS


def _ratio(burn, budget):
    if burn is None or budget is None:
        return None
    if budget <= 0:
        return 0.0 if burn <= 0 else math.inf
    return burn / budget


# ---------------------------------------------------------------------------
# slots — a local family's concurrent lanes, shared by every team using it
# ---------------------------------------------------------------------------

def slot_families(capacity=None, local=None):
    """The families whose share is a count of LANES: every local family, and
    any other whose lane capacity somebody recorded."""
    local = _local_families() if local is None else local
    return frozenset(local) | frozenset(capacity or ())


def _lanes(value):
    """A recorded lane count, or None: an integer from 1 to MAX_LANES."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 1 <= value <= MAX_LANES else None


def read_capacity(path=None):
    """({family: {lanes, by, ts, reason}}, problem). A missing file is no
    capacity and no problem; an unreadable one is no capacity AND a problem,
    so a reader says why instead of printing a number nobody measured."""
    target = path or capacity_path()
    if not os.path.exists(target):
        return {}, None
    try:
        doc = pk.read_json(target, strict=True)
    except (OSError, ValueError) as exc:
        return {}, "the lane capacity file did not read (%s)" % exc
    fams = (doc or {}).get("families") if isinstance(doc, dict) else None
    if not isinstance(fams, dict):
        return {}, "the lane capacity file holds no families"
    out = {}
    for fam, rec in fams.items():
        lanes = _lanes((rec or {}).get("lanes")) if isinstance(rec, dict) \
            else None
        if lanes is None:
            continue
        out[str(fam)] = {"lanes": lanes, "by": rec.get("by") or "",
                         "ts": rec.get("ts"),
                         "reason": rec.get("reason") or ""}
    return out, None


@contextlib.contextmanager
def _capacity_lock(target):
    import fcntl
    os.makedirs(os.path.dirname(target), exist_ok=True)
    fd = os.open(target + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def set_capacity(family, lanes, by, reason, apply=False, now=None,
                 path=None):
    """(row, problem) — record how many concurrent lanes `family` serves, or
    clear it (`lanes` None). A DRY RUN until `apply`, like every write here.

    The capacity is a MEASUREMENT, so it carries who measured it, when and
    how (`reason`); the card and the verb print all three beside the number.
    The native family is refused (its share is accounts), and so is a word
    the catalog does not know."""
    from . import seat  # noqa: F401 — the facade first (seat_compat)
    from .seat_catalog import FAMILIES as table
    now = time.time() if now is None else now
    if family == NATIVE_FAMILY:
        return None, ("%s spends each seat's own account; it has no lane "
                      "capacity" % family)
    if family not in table:
        return None, "unknown family '%s'" % family
    if lanes is not None and _lanes(lanes) is None:
        return None, "lanes must be a whole number from 1 to %d" % MAX_LANES
    if not str(reason or "").strip():
        return None, "a reason is required: how the lanes were measured"
    target = path or capacity_path()
    with _capacity_lock(target):
        have, problem = read_capacity(target)
        if problem and os.path.exists(target):
            return None, problem + "; nothing was written over it"
        was = (have.get(family) or {}).get("lanes")
        row = {"family": family, "was": was, "lanes": lanes, "by": by or "",
               "ts": now, "reason": str(reason).strip(), "applied": apply}
        if not apply:
            return row, None
        if lanes is None:
            have.pop(family, None)
        else:
            have[family] = {"lanes": lanes, "by": row["by"], "ts": now,
                            "reason": row["reason"]}
        pk.write_json(target, {"v": CAPACITY_V, "families": have})
    try:
        _append_event({"kind": "capacity", "project": None, "family": family,
                       "was": was, "lanes": lanes, "by": row["by"],
                       "ts": now, "reason": row["reason"]})
    except Exception:                       # noqa: BLE001 — the value stands
        row["history_unwritten"] = True
    return row, None


def slot_use(families, projects=None, snap=None, families_of=None):
    """({family: {by_project, unplaced, total}}, problem) — the rows holding
    each slot family's lanes right now, by the project each row's repository
    belongs to.

    ONE FOLD OF THE ONE LEDGER: `dispatches.owed`, the same rows proxywatch
    counts a seat's holding from, so a lane is in use on this card exactly
    when the seat is holding work on the seats board. A row whose repository
    no project owns is counted `unplaced`: it holds a lane all the same.
    `families_of` resolves a recipient seat to its family (a seam)."""
    from . import dispatches
    from .inject._ledger import project_for_cwd
    families = frozenset(families or ())
    if not families:
        return {}, None
    if snap is None:
        snap, unavailable = dispatches.snapshot()
        if unavailable:
            return None, ("the dispatch ledger did not read (%s)"
                          % unavailable)
    resolve = families_of or (lambda seat: family_of(seat))
    fam_of, where = {}, {}
    out = {fam: {"by_project": {}, "unplaced": 0, "total": 0}
           for fam in sorted(families)}
    for row in dispatches.owed(snap):
        if row.get("kind") in NOT_A_LANE:
            continue
        seat = row.get("recipient")
        if seat not in fam_of:
            fam_of[seat] = resolve(seat)
        fam = fam_of[seat]
        if fam not in out:
            continue
        root = row.get("repo_root")
        if root not in where:
            try:
                where[root] = project_for_cwd(root, projects=projects) \
                    if root else None
            except Exception:               # noqa: BLE001 — unplaced
                where[root] = None
        use, key = out[fam], where[root]
        use["total"] += 1
        if key:
            use["by_project"][key] = use["by_project"].get(key, 0) + 1
        else:
            use["unplaced"] += 1
    return out, None


def _uses_slots(teams, fams):
    for team in (teams or {}).values():
        named = {m.get("family") for m in team.get("members") or ()} \
            | set(team.get("shares") or ())
        if named & fams:
            return True
    return False


def slot_reading(teams=None, projects=None, fold=True, snap=None,
                 families_of=None, capacity_file=None, strict=False):
    """{families, capacity, capacity_problem, in_use, why} — the slots half
    of the readings.

    THE LEDGER IS FOLDED ONLY WHEN IT CAN MATTER: `fold` is on and some team
    in `teams` (None: the caller cannot say, so fold) names a slot family. A
    fleet with no local family on any team pays nothing for this reading.

    A READING THAT FAILED IS NEVER "NOTHING THERE" (round 3, ruling c): a
    capacity file that exists and does not read, or a ledger fold that did
    not read, RAISES to a `strict` reader (`CapacityUnread`, `LedgerUnread`,
    or what the fold raised), so route answers PARTIAL and a door says
    FAILED; a non-strict reader gets `failed`, {capacity|lanes: class}, and
    says so on the card."""
    capacity, problem = read_capacity(capacity_file)
    if problem and strict:
        raise CapacityUnread(problem)
    fams = slot_families(capacity)
    out = {"families": sorted(fams), "capacity": capacity,
           "capacity_problem": problem, "in_use": None, "why": None,
           "failed": {"capacity": CapacityUnread.__name__} if problem
           else {}}
    if not fold:
        out["why"] = "not read on this pass"
    elif teams is not None and not _uses_slots(teams, fams):
        out["why"] = "no team uses a slot family"
    else:
        try:
            out["in_use"], out["why"] = slot_use(fams, projects=projects,
                                                 snap=snap,
                                                 families_of=families_of)
        except Exception as exc:            # noqa: BLE001 — named, FAILED
            if strict:
                raise
            out["in_use"], out["why"] = None, (
                "the lanes in use did not read (%s: %s)"
                % (exc.__class__.__name__, exc))
            out["failed"]["lanes"] = exc.__class__.__name__
        else:
            if out["in_use"] is None and out["why"]:
                if strict:
                    raise LedgerUnread(out["why"])
                out["failed"]["lanes"] = LedgerUnread.__name__
    return out


def _slot_row(key, fam, share, total, slots, colour):
    """The slots half of one allocation row: capacity, this project's slots,
    the lanes it and the fleet hold, the colour and whether new work queues."""
    from . import burnflags as bf
    slots = slots or {}
    cap = ((slots.get("capacity") or {}).get(fam) or {}).get("lanes")
    use = (slots.get("in_use") or {}).get(fam)
    in_use = None if use is None \
        else int((use.get("by_project") or {}).get(key, 0))
    fleet = None if use is None else int(use.get("total") or 0)
    mine = None if cap is None or share is None \
        else share / max(100.0, float(total)) * cap
    ratio = _ratio(in_use, mine)
    if colour == bf.RED or mine is None or in_use is None:
        eff = colour
    else:
        eff = bf.YELLOW if in_use <= mine else bf.ORANGE
    return {"capacity": cap, "slots": mine, "in_use": in_use,
            "in_use_fleet": fleet, "ratio": ratio, "colour": eff,
            # ONE MORE ROW WOULD GO PAST THE SLOTS: the door and the router
            # say QUEUED — admitted, waiting for a lane, never refused.
            "queued": mine is not None and in_use is not None
            and colour != bf.RED and in_use + 1 > mine + 1e-9,
            "capacity_measured": cap is not None}


def allocation(flags, pace, burn, teams, lights=None, slots=None,
               live=None, failed=None):
    """{project: {family: row}} — each project's share of each family it uses,
    turned into a budget and a colour. PURE: readings in, dict out.

    ONLY A FAMILY SHORT ON MONEY IS RATIONED (`short`): an ORANGE that
    reach or policy set leaves every project on the family's own colour.

    `flags` is {family: flag} (`burnflags.cached_flags`), `pace` is {family:
    `codexpace.fold_input`}, `burn` is `codexpace.seat_burn`'s reading (per
    seat, per family, per hour), `slots` is `slot_reading`'s (lane capacity
    and lanes in use) and `teams` is {project: team record}. `lights` is
    {project: light} and only an AUTHORED red releases a share. `live` is
    {seat: family} (`live_families`): A MEMBER IS BILLED TO THE FAMILY IT
    SPENDS where the family door names one, and to its typed family only
    where it names none (design read D3). `failed` is `readings`'s: a row
    whose burn is unmeasured BECAUSE its reader raised carries
    `burn_failed` (the class), and its line says FAILED (design read D6).

    Every row: {mode, share, budget_per_h, burn_per_h, ratio, colour,
    family_colour, say, shared, burn_measured}, and a SLOTS row adds
    {capacity, slots, in_use, in_use_fleet, queued, capacity_measured}. A
    `share` is None for a family the team spends with NO share: that row is
    UNRATIONED (no budget, no ratio, the family's own colour) and claims no
    part of the family's total; `drift` tells the lead. A
    SHARED seat — one on more than one team — counts toward the family's
    total and is attributed to no project, so its spend never lands on one
    team's bill; a lane in use is billed by the ROW's project instead, since
    a row, unlike a seat, belongs to exactly one."""
    from . import burnflags as bf
    flags, teams, lights = flags or {}, teams or {}, lights or {}
    pace, live = pace or {}, live or {}

    def fam_of(m):
        return live.get(m["seat"]) or m["family"]
    fam_burn = (burn or {}).get("families") or {}
    on = {}
    for key, team in teams.items():
        for m in team.get("members") or ():
            on.setdefault(m["seat"], set()).add(key)
    shared = {seat for seat, keys in on.items() if len(keys) > 1}

    def share_of(key, fam):
        """The project's share of `fam`, or None when the team sets none.
        AN UNSET SHARE IS UNRATIONED, NEVER 0% (design read D2): read as 0%
        it put every team project past twice a zero budget, so RED, on any
        family short on money. An authored red light still releases the
        share to 0, which is the owner's word."""
        lit = lights.get(key) or {}
        if RED_LIGHT_RELEASES_SHARE and lit.get("authored") \
                and lit.get("colour") == "red":
            return 0
        got = (teams[key].get("shares") or {}).get(fam)
        return None if got is None else int(got)

    out = {}
    for key, team in sorted(teams.items()):
        fams = {fam_of(m) for m in team.get("members") or ()} \
            | set(team.get("shares") or ())
        rows = {}
        for fam in sorted(fams):
            flag = flags.get(fam) or {}
            colour = flag.get("colour") or bf.GREY
            rations = short(flag)
            mode = mode_of(fam, pace, burn, slots)
            share = share_of(key, fam)
            total = sum(x for x in (share_of(k, fam) for k in teams)
                        if x is not None)
            seats = (fam_burn.get(fam) or {}).get("seats") or {}
            measured = isinstance((fam_burn.get(fam) or {}).get("per_hour"),
                                  (int, float))
            mine = [m["seat"] for m in team.get("members") or ()
                    if fam_of(m) == fam]
            burn_h = (sum(float(seats.get(s) or 0.0) for s in mine
                          if s not in shared) if measured else None)
            budget = ratio = None
            if share is None:
                pass                    # unrationed: the family colour stands
            elif mode == RATE:
                budget = share / max(100.0, float(total)) \
                    * sustainable_per_h(pace.get(fam))
                ratio = _ratio(burn_h, budget)
            elif mode == RELATIVE:
                fam_total = float(fam_burn[fam]["per_hour"])
                part = (burn_h / fam_total) if burn_h is not None \
                    and fam_total > 0 else (0.0 if burn_h is not None else None)
                ratio = _ratio(part, share / max(100.0, float(total)))
            eff = effective_colour(colour, ratio) \
                if mode in (RATE, RELATIVE) and rations else colour
            lane = _slot_row(key, fam, share, total, slots, colour) \
                if mode == SLOTS and fam in ((slots or {}).get("families")
                                             or ()) else None
            if lane:
                ratio, eff = lane["ratio"], lane["colour"]
            rows[fam] = {"mode": mode, "share": share, "shares_total": total,
                         "over_promised": total > 100,
                         "budget_per_h": budget, "burn_per_h": burn_h,
                         "ratio": ratio, "colour": eff, "family_colour": colour,
                         "rationed": rations
                         and mode in (RATE, RELATIVE) and ratio is not None,
                         "say": bf.BEHAVIOUR.get(eff, bf.BEHAVIOUR[bf.GREY])
                         ["say"],
                         "shared": sorted(s for s in mine if s in shared),
                         "burn_measured": measured,
                         "burn_failed": None if measured
                         else (failed or {}).get("burn")}
            if lane:
                rows[fam].update(lane)
        out[key] = rows
    return out


def tokens(n):
    """13.9M / 640k / 0 — the one way this module prints a token rate."""
    if n is None:
        return "?"
    # HALF UP ON THE PRINTED DIGIT, the way a person rounds: 10.95M reads
    # 11.0M, not the 10.9M that binary floating point and banker's rounding
    # would each print for it.
    if n >= 1e9:
        return "%.1fB" % (math.floor(n / 1e8 + 0.5) / 10)
    if n >= 1e6:
        return "%.1fM" % (math.floor(n / 1e5 + 0.5) / 10)
    if n >= 1e3:
        return "%dk" % math.floor(n / 1e3 + 0.5)
    return "%d" % math.floor(n + 0.5)


def burning(row):
    """32.9M/h / FAILED (ValueError) / unmeasured — a row's burn as its line
    prints it. A burn whose reader RAISED is FAILED, never unmeasured
    (design read D6)."""
    if row.get("burn_measured"):
        return tokens(row.get("burn_per_h")) + "/h"
    if row.get("burn_failed"):
        return "FAILED (%s)" % row["burn_failed"]
    return "unmeasured"


def ratio_text(r):
    if r is None:
        return "?"
    if r == math.inf:
        return "no budget"
    return ("%.2f×" % r) if r < 1 else ("%.1f×" % r)


def line(project, family, row):
    """The one line route and the doors print for (project, family):

        share codex 30% → 13.9M/h budget, burning 32.9M/h (2.4×) → RED for helm

    A family the team spends with no share says so, never 0%.
    """
    if row.get("share") is None and row["mode"] in (RATE, RELATIVE):
        return ("no share for %s on this team, so it is unrationed here, "
                "burning %s → %s for %s (the family's own colour)"
                % (family, burning(row), row["colour"], project))
    if row.get("share") is None and "capacity_measured" in row:
        return ("no share of %s's lanes on this team, so nothing queues "
                "here, %s in use → %s for %s" % (family,
                                                 _count(row["in_use"]),
                                                 row["colour"], project))
    if row["mode"] == RATE:
        return ("share %s %d%% → %s/h budget, burning %s (%s) → %s for %s"
                % (family, row["share"], tokens(row["budget_per_h"]),
                   burning(row), ratio_text(row["ratio"]), row["colour"],
                   project))
    if row["mode"] == RELATIVE:
        return ("share %s %d%% of the family's burn, burning %s (%s) → %s "
                "for %s" % (family, row["share"], burning(row),
                            ratio_text(row["ratio"]), row["colour"], project))
    if row["mode"] == ACCOUNTS:
        return ("%s: each seat spends its own home's account; no percent "
                "share → %s for %s" % (family, row["colour"], project))
    if row.get("capacity_measured"):
        return ("slots %s %d%% of %d lanes → %s slots, %s in use (fleet %s of "
                "%d) → %s for %s%s" % (
                    family, row["share"], row["capacity"],
                    slots_text(row["slots"]), _count(row["in_use"]),
                    _count(row["in_use_fleet"]), row["capacity"],
                    row["colour"], project,
                    "; QUEUED — new work waits for a lane, it is not refused"
                    if row.get("queued") else ""))
    if "capacity_measured" in row:
        return ("slots %s %d%%: capacity not measured, %s in use here → %s "
                "for %s" % (family, row["share"], _count(row["in_use"]),
                            row["colour"], project))
    return ("%s: not measured; a share here is advisory seat count → %s for %s"
            % (family, row["colour"], project))


def slots_text(n):
    """1.2 / 3 / ? — a project's slots, to one decimal when fractional."""
    if n is None:
        return "?"
    return ("%d" % n) if abs(n - round(n)) < 1e-9 else ("%.1f" % n)


def _count(n):
    return "?" if n is None else "%d" % n


# ---------------------------------------------------------------------------
# drift — what the lead does next
# ---------------------------------------------------------------------------

def drift(project, team, world, flags=None):
    """[{kind, seat, text[, say]}] — where the running seats and the team
    disagree.

    `text` is the lead's line and names the verb it runs. Where it does,
    `say` is the same line in the OWNER'S words for the console's Team tab:
    what the lead will do, never a command, because he does not use a
    terminal (console walk 3, #7). A row whose text names no verb has no
    `say`; the tab shows its text.

    Five kinds, each with the lead's own repair:
      wanted            a member that is not running: `helm seat spawn`
      unlisted          a seat working here that is not on the team
      homed-elsewhere   a member homed in another project's room
      family-red        a member whose family is RED for everyone
      no-share          a family the team spends with no share: it is
                        unrationed here (`helm team set --share`)
      family-differs    a member written on a family it does not spend
                        (the family door rules; remove it and add it again)
    plus the team's own shape: no lead, or two."""
    from . import burnflags as bf
    flags = flags or {}
    seats = (world or {}).get("seats") or {}
    roster = (world or {}).get("roster") or {}
    out = []
    members = team.get("members") or []
    leads = [m["seat"] for m in members if m["role"] == "lead"]
    if not leads:
        out.append({"kind": "no-lead", "seat": None,
                    "text": "no lead: somebody must own this team and fix its "
                            "drift"})
    elif len(leads) > 1:
        out.append({"kind": "two-leads", "seat": None,
                    "text": "%d leads (%s): pick one"
                            % (len(leads), ", ".join(leads))})
    names = {m["seat"] for m in members}
    spent = {}
    for m in members:
        seat, fam = m["seat"], m["family"]
        placed = seats.get(seat)
        # THE FAMILY DOOR RULES (design read D3): `placements` asked it for
        # every running seat, and a member written on another family is
        # billed and benched on the one it spends.
        known = (placed or {}).get("family")
        spent[seat] = known or fam
        if known and known != fam:
            out.append({"kind": "family-differs", "seat": seat,
                        "text": "%s is on the team as %s and spends %s, so "
                                "allocation and route read %s: remove it and "
                                "add it again (`helm team set %s --expect V "
                                "--remove %s --add %s:%s:%s --reason ...`)"
                                % (seat, fam, known, known, project, seat,
                                   seat, m["role"], known),
                        "say": "%s is on the team as %s and spends %s, so "
                               "allocation and route read %s: remove it and "
                               "add it again on %s" % (seat, fam, known,
                                                       known, known)})
        if seat not in roster or not placed \
                or placed.get("presence") == "absent":
            word = "codex" if fam == "codex" else \
                "claude" if fam == NATIVE_FAMILY else fam
            out.append({"kind": "wanted", "seat": seat,
                        "text": "%s is not running: the lead starts or "
                                "resumes it (`helm seat spawn %s` for a new "
                                "%s seat on this project)"
                                % (seat, "%s-%s" % (project, word), word),
                        "say": "%s is not running: the lead starts or "
                               "resumes it (a new %s seat on this project "
                               "is named %s)"
                               % (seat, word, "%s-%s" % (project, word))})
        elif placed.get("home_room") and placed["home_room"] != project:
            out.append({"kind": "homed-elsewhere", "seat": seat,
                        "text": "%s is homed in #%s and also serves this "
                                "team; its spend shows as shared (`helm chat "
                                "seat rehome %s %s` moves its home)"
                                % (seat, placed["home_room"], seat, project),
                        "say": "%s is homed in #%s and also serves this "
                               "team; its spend shows as shared until its "
                               "home moves to #%s"
                               % (seat, placed["home_room"], project)})
        if (flags.get(fam) or {}).get("colour") == bf.RED:
            out.append({"kind": "family-red", "seat": seat,
                        "text": "%s's family %s is RED for everyone: start "
                                "nothing new on it" % (seat, fam)})
    # AN UNSET SHARE IS UNRATIONED (design read D2), and the lead is told:
    # one line per family the team spends with no share. The native family
    # spends each seat's own account and takes no percent.
    shares = team.get("shares") or {}
    for fam in sorted(set(spent.values()) - set(shares) - {NATIVE_FAMILY}):
        out.append({"kind": "no-share", "seat": None, "family": fam,
                    "text": "%s has no share on this team, so %s is "
                            "unrationed here: while it is short this "
                            "project reads the family's own colour (`helm "
                            "team set %s --expect V --share %s=PCT "
                            "--reason ...` sets one)"
                            % (fam, project, project, fam),
                    "say": "%s has no share on this team, so %s is "
                           "unrationed here: while it is short this project "
                           "reads the family's own colour until a share is "
                           "set" % (fam, project)})
    vocab = frozenset(families())
    for seat, placed in sorted(seats.items()):
        if placed.get("project") != project or seat in names \
                or placed.get("presence") == "absent":
            continue
        fam = placed.get("family")
        row = {"kind": "unlisted", "seat": seat}
        if fam and fam not in vocab:
            row["text"] = ("%s works here (%s), and its family %s is not one "
                           "helm measures yet, so it cannot join a team "
                           "until it is" % (seat, placed.get("source"), fam))
        else:
            row["text"] = ("%s works here (%s) and is not on the team: add "
                           "it, or rehome it (`helm chat seat rehome %s "
                           "<room>`)" % (seat, placed.get("source"), seat))
            row["say"] = ("%s works here (%s) and is not on the team: add "
                          "it, or move its home to another room"
                          % (seat, placed.get("source")))
        out.append(row)
    return out


# ---------------------------------------------------------------------------
# the readings, and one project's whole view
# ---------------------------------------------------------------------------

def _failed(failed, name, exc):
    """Name a reader that RAISED in the caller's `failed` map, by class."""
    if isinstance(failed, dict):
        failed[name] = exc.__class__.__name__


def failed_text(failed):
    """burn (ValueError), pace (KeyError) — the readers that raised."""
    return ", ".join("%s (%s)" % kv for kv in sorted((failed or {}).items()))


def readings(now=None, live=False, teams=None, projects=None, strict=False,
             failed=None):
    """(flags, pace, burn, slots) — the four readings `allocation` folds, from
    the files the posting pass already wrote and the dispatch ledger. NEVER
    PROBES. `live` lets a verb a person ran read the per-seat burn from the
    ledger itself when the pass has not written its snapshot; a routing
    answer never does. `teams` lets the slots reading skip the ledger fold
    when no team names a slot family (`slot_reading`).

    `strict` RAISES what a reader raised instead of reading it as empty. A
    door asks for it (`door_note`), because there an empty reading is an
    answer ("nothing is rationed") that a failed read cannot give. An ABSENT
    or stale snapshot is not a failure either way: it is nothing measured,
    and each reader already answers it without raising.

    `failed`, a dict, is where a NON-strict caller learns which readers
    raised ({flags|pace|burn|slots: exception class}; design read D6): the
    board and the verb read on past a failed reader, and say FAILED for it,
    never "unmeasured"."""
    from . import burnflags, codexpace
    now = time.time() if now is None else now
    try:
        flags, _age = burnflags.cached_flags(now=now)
    except Exception as exc:                # noqa: BLE001 — named, FAILED
        if strict:
            raise
        _failed(failed, "flags", exc)
        flags = {}
    pace = {}
    try:
        got = codexpace.cached_fold_input(now=now)
        if got:
            pace[codexpace.FAMILY] = got
    except Exception as exc:                # noqa: BLE001 — named, FAILED
        if strict:
            raise
        _failed(failed, "pace", exc)
    burn = None
    try:
        burn = codexpace.cached_seat_burn(now=now)
        if burn is None and live:
            burn = codexpace.live_seat_burn(now=now)
    except Exception as exc:                # noqa: BLE001 — named, FAILED
        if strict:
            raise
        _failed(failed, "burn", exc)
        burn = None
    try:
        slots = slot_reading(teams=teams, projects=projects, strict=strict)
        if isinstance(failed, dict):
            failed.update(slots.get("failed") or {})
    except Exception as exc:                # noqa: BLE001 — named, FAILED
        if strict:
            raise
        _failed(failed, "slots", exc)
        slots = {"families": [], "capacity": {}, "capacity_problem": None,
                 "in_use": None, "why": "the slots did not read (%s: %s)"
                 % (exc.__class__.__name__, exc)}
    return flags or {}, pace, burn, slots


def view(keys=None, now=None, live=False, reg=None, auth=None, world=None,
         inputs=None, failed=None):
    """{project: {team, allocation, drift, light, binding}} for every project
    with a team (or the named `keys`) — the join the verb, the doors and the
    board all draw.

    ONLY AN AUTHORED TEAM BINDS. The allocation every consumer acts on is
    computed over the authored teams alone, so a proposal changes nobody's
    budget until the owner accepts it. A PROPOSED team's own rows are a
    PREVIEW — the authored teams plus that one — marked `binding: False`, so
    the card can show what accepting it would do without it doing anything.

    `failed` is `readings`'s (design read D6): the readers that raised, each
    record carries it, and a row whose burn reader raised says FAILED."""
    from . import registry
    now = time.time() if now is None else now
    reg = registry.load(strict=True) if reg is None else reg
    auth = registry._authored_load(strict=True) if auth is None else auth
    projects = reg.get("projects") or {}
    world = placements(projects=projects, now=now) if world is None else world
    lights = registry.lights(reg)
    all_teams = read_all(reg=reg, auth=auth, world=world)
    failed = {} if failed is None else failed
    if world.get("roster_unread"):
        failed["roster"] = world["roster_unread"]
    flags, pace, burn, slots = readings(
        now=now, live=live, teams=all_teams, projects=projects,
        failed=failed) if inputs is None else inputs
    bound = {k: t for k, t in all_teams.items() if t["authored"]}
    live = live_families({m["seat"] for t in all_teams.values()
                          for m in t["members"]}, roster=world.get("roster"))
    binding = allocation(flags, pace, burn, bound, lights=lights, slots=slots,
                         live=live, failed=failed)
    out = {}
    for key in sorted(all_teams if keys is None else keys):
        team = all_teams.get(key) or read(key, reg=reg, auth=auth,
                                          world=world)
        if not team["authored"] and not team["shares"]:
            # A PROPOSAL CARRIES TODAY'S MEASURED SPLIT, the same shares
            # `seed` would author, so accepting it on the card is accepting
            # what the verb would have written.
            team = dict(team, shares=measured_shares(key, team["members"],
                                                     burn, pace, slots=slots))
        if team["authored"]:
            alloc = binding.get(key) or {}
        else:
            alloc = allocation(flags, pace, burn, dict(bound, **{key: team}),
                               lights=lights, slots=slots, live=live,
                               failed=failed).get(key) or {}
        out[key] = {"team": team, "binding": team["authored"],
                    "allocation": alloc,
                    "drift": drift(key, team, world, flags=flags),
                    "light": lights.get(key), "failed": dict(failed),
                    "live": {m["seat"]: live[m["seat"]]
                             for m in team["members"] if m["seat"] in live}}
    return out


def project_row(project, family, now=None, inputs=None, reg=None, auth=None,
                strict=False, live=None):
    """The allocation row `route` and the doors read for (project, family),
    or None when the project has no AUTHORED team or no share applies. Its
    members are billed to the family they spend (`live`, {seat: family};
    left out, `live_families` over this project's members, one roster read).
    `strict` is `readings`'s: a door asks for a reader's failure to be
    raised, not read as empty."""
    from . import registry
    if not project:
        return None
    reg = registry.load(strict=True) if reg is None else reg
    auth = registry._authored_load(strict=True) if auth is None else auth
    bound = {}
    for key, rec in (reg.get("projects") or {}).items():
        said, _bad = authored(key, rec, auth)
        if said is not None:
            bound[key] = said
    if project not in bound:
        return None
    flags, pace, burn, slots = readings(
        now=now, teams=bound, projects=reg.get("projects"), strict=strict) \
        if inputs is None else inputs
    if live is None:
        live = live_families((m["seat"] for m in
                              bound[project].get("members") or ()),
                             strict=strict)
    rows = allocation(flags, pace, burn, bound, lights=registry.lights(reg),
                      slots=slots, live=live).get(project) or {}
    return rows.get(family) if family else rows


# ---------------------------------------------------------------------------
# editing and seeding — the verb's halves
# ---------------------------------------------------------------------------

def edit(team, add=(), remove=(), roles=(), shares=(), families_of=None):
    """(new team, problem) — `team` with the verb's edits applied, in order:
    removals, additions, role changes, then shares. `families_of` resolves a
    seat added without a family (`SEAT:ROLE`)."""
    members = [dict(m) for m in team.get("members") or ()]
    out_shares = dict(team.get("shares") or {})
    names = lambda: {m["seat"].casefold(): m for m in members}
    for seat in remove:
        if seat.casefold() not in names():
            return None, "%s is not on the team" % seat
        members = [m for m in members if m["seat"].casefold() != seat.casefold()]
    for spec in add:
        parts = spec.split(":")
        if len(parts) not in (2, 3) or not all(parts):
            return None, "--add takes SEAT:ROLE or SEAT:ROLE:FAMILY, not %r" \
                % spec
        seat, role = parts[0], parts[1]
        fam = parts[2] if len(parts) == 3 else \
            (families_of(seat) if families_of else None)
        if not fam:
            return None, ("helm cannot tell which family %s spends; name it "
                          "(--add %s:%s:FAMILY)" % (seat, seat, role))
        if seat.casefold() in names():
            return None, "%s is already on the team (use --role)" % seat
        members.append({"seat": seat, "family": _alias(fam), "role": role})
    for spec in roles:
        seat, _sep, role = spec.partition(":")
        m = names().get(seat.casefold())
        if not m or not role:
            return None, ("--role takes SEAT:ROLE for a seat on the team, not "
                          "%r" % spec)
        m["role"] = role
    for spec in shares:
        fam, _sep, pct = spec.partition("=")
        if not fam or not pct.isdigit():
            return None, "--share takes FAMILY=PERCENT, not %r" % spec
        out_shares[_alias(fam)] = int(pct)
    return validate({"members": members, "shares": out_shares})


def measured_shares(project, members, burn, pace=None, families_only=None,
                    slots=None):
    """{family: percent} — the project's slice of each family's measured burn,
    for a seed. Rate and relative families carry a percent of the burn; a
    SLOT family carries the project's slice of the lanes the fleet's projects
    hold right now. A family with nothing measured gets none: the lead sets
    it on the card."""
    fam_burn = (burn or {}).get("families") or {}
    use = (slots or {}).get("in_use") or {}
    out = {}
    for fam in sorted({m["family"] for m in members}):
        if families_only and fam not in families_only:
            continue
        mode = mode_of(fam, pace or {}, burn, slots)
        if mode == SLOTS:
            held = (use.get(fam) or {}).get("by_project") or {}
            placed = sum(held.values())
            if placed > 0:
                out[fam] = int(math.floor(100.0 * held.get(project, 0)
                                          / placed + 0.5))
            continue
        if mode not in (RATE, RELATIVE):
            continue
        total = (fam_burn.get(fam) or {}).get("per_hour")
        if not isinstance(total, (int, float)) or total <= 0:
            continue
        seats = (fam_burn.get(fam) or {}).get("seats") or {}
        mine = sum(float(seats.get(m["seat"]) or 0.0) for m in members
                   if m["family"] == fam)
        out[fam] = int(math.floor(100.0 * mine / total + 0.5))
    return out


def burn_unmeasured(burn):
    """Why the per-seat burn reading measures nothing, or None when it
    measures something: no reading, a ledger that did not read, or no family
    burned in its window."""
    if not isinstance(burn, dict):
        return "no per-seat burn reading"
    if burn.get("why"):
        return str(burn["why"])
    if not any(isinstance((f or {}).get("per_hour"), (int, float))
               for f in (burn.get("families") or {}).values()):
        return "no family burned in the reading's window"
    return None


def seed(now=None, reg=None, auth=None, world=None, inputs=None, live=True):
    """([{project, team, why}], problem) — a proposal for every project with
    seats on it today and no authored team, shares from today's measured
    split. Nothing is written here; the verb writes each one through `write`
    at v0.

    REFUSED WHEN BURN IS NOT MEASURED (design read D2): the shares a seed
    proposes ARE the measured split, so with nothing measured it would
    author every team with no share. `problem` says why and nothing is
    proposed."""
    from . import registry
    now = time.time() if now is None else now
    reg = registry.load(strict=True) if reg is None else reg
    auth = registry._authored_load(strict=True) if auth is None else auth
    world = placements(projects=reg.get("projects"), now=now) \
        if world is None else world
    all_teams = read_all(reg=reg, auth=auth, world=world)
    _flags, pace, burn, slots = readings(
        now=now, live=live, teams=all_teams, projects=reg.get("projects")) \
        if inputs is None else inputs
    unmeasured = burn_unmeasured(burn)
    if unmeasured:
        return [], ("burn is not measured (%s), so a seed would author every "
                    "team with no share; nothing was proposed. `helm "
                    "proxywatch --post` writes the per-seat burn, or author "
                    "a team with its shares by hand (`helm team set`)"
                    % unmeasured)
    out = []
    for key, team in sorted(all_teams.items()):
        if team["authored"] or not team["members"]:
            continue
        shares = measured_shares(key, team["members"], burn, pace,
                                 slots=slots)
        out.append({"project": key,
                    "team": {"members": team["members"], "shares": shares},
                    "why": None})
    return out, None


# ---------------------------------------------------------------------------
# what route reads
# ---------------------------------------------------------------------------

def authored_only(project, reg=None, auth=None):
    """The AUTHORED team for `project` as `read` shapes it, or None. Reads no
    roster: a routing answer asks this on every call and a proposal binds
    nothing, so there is nothing to derive."""
    from . import registry
    if not project:
        return None
    reg = registry.load(strict=True) if reg is None else reg
    rec = (reg.get("projects") or {}).get(project)
    if rec is None:
        return None
    auth = registry._authored_load(strict=True) if auth is None else auth
    said, _bad = authored(project, rec, auth)
    if said is None:
        return None
    return {"project": project, "authored": True, "v": said["v"],
            "by": said.get("by") or "", "ts": said.get("ts"),
            "reason": said.get("reason") or "",
            "members": said.get("members") or [],
            "shares": dict(said.get("shares") or {}), "problem": None}


def bench(team, kind, live=None):
    """({seat: bench row}, [(seat, role)] filtered out) — the members whose
    role takes `kind`, in the shape `route`'s bench join reads. A member's
    family rides the row so a seat the usability join cannot name (a native
    seat, a seat not started yet) still lands on its family's bench — THE
    FAMILY IT SPENDS where the family door names one (`live`, {seat:
    family}; design read D3), with the typed one beside it as
    `authored_family`."""
    live = live or {}
    out, filtered = {}, []
    for m in (team or {}).get("members") or ():
        if takes(m["role"], kind):
            out[m["seat"]] = {"family": live.get(m["seat"]) or m["family"],
                              "authored_family": m["family"],
                              "role": m["role"],
                              "home_room": (team or {}).get("project"),
                              "team": True}
        else:
            filtered.append((m["seat"], m["role"]))
    return out, filtered


# ---------------------------------------------------------------------------
# the doors and the watchdog — advice and one room line per crossing
# ---------------------------------------------------------------------------

def door_note(path, family=None, now=None, inputs=None, filed=False,
              kind=None):
    """The NOTE a door prints for work starting in `path`'s project, or None.

    It names the project's OWN colour on each family its share rations, and
    each slot family where one more row would QUEUE behind the project's
    lanes (only `family` when the door knows which one the work will spend).
    `filed` says the door has ALREADY appended the row it admits, of `kind`:
    the dispatch door builds its note after the append, so that row is one
    of the lanes in use and it queues only when the lanes in use, itself
    included, pass the slots — and a build or delegate row holds no lane, so
    it never queues (`NOT_A_LANE`). A door that filed nothing (the claim
    door) asks whether ONE MORE row would queue. ADVICE ONLY,
    the owner's default for v1 (`ADVISE_ONLY`): no caller refuses on it, and
    a door must never stop on an unread input.

    A READ THAT FAILS SAYS SO (trunk's fail-loud rule, ruled for this door on
    task/3156). Silence here means "inside its share, or nothing is short",
    so a failed read that returned None would give exactly the answer it
    could not know. Any failure, the formatting included, is one FAILED line
    naming the exception's class; it never raises, because the claim door
    calls this with nothing around it."""
    try:
        from . import registry
        from .inject._ledger import project_for_cwd
        reg = registry.load(strict=True)
        project = project_for_cwd(path, projects=reg.get("projects") or {})
        if not project:
            return None
        rows = project_row(project, None, now=now, inputs=inputs, reg=reg,
                           strict=True)
        if not rows:
            return None
        picked = {family: rows.get(family)} if family else rows
        picked = {fam: _as_filed(row, kind) if filed else row
                  for fam, row in picked.items() if row}
        said = ["%s — %s" % (line(project, fam, row), row["say"])
                for fam, row in sorted(picked.items())
                if row.get("rationed") or row.get("queued")]
    except Exception as exc:                # noqa: BLE001 — said, not raised
        return failed_note(exc)
    if not said:
        return None
    return ("project share (advice only; the owner decides whether it ever "
            "refuses): " + "; ".join(said))


def _as_filed(row, kind):
    """An allocation row as a door that already FILED a `kind` row reads it.
    `queued` there is one more row than the ledger holds; the filed row is
    already in the ledger when it holds a lane, and holds none as a build."""
    if not row.get("queued"):
        return row
    if kind in NOT_A_LANE:
        return dict(row, queued=False)
    return dict(row, queued=row["in_use"] > row["slots"] + 1e-9)


def failed_note(exc):
    """The one sentence for a project-share read that raised, at any door."""
    return ("project share FAILED (%s): the project's share of a short "
            "family could not be read, so this door cannot say whether the "
            "work is inside it (advice only; nothing is refused)"
            % exc.__class__.__name__)


def budget_dwell_s():
    """How long a project's colour on a short family must HOLD before its
    PROJECT-BUDGET line posts (design read D4): half one watchdog interval,
    IMPORTED from the installed timer's period (`proxywatch.INTERVAL_S`),
    never chosen here. A colour still read at the next scheduled pass posts;
    one that flips back inside a pass never does."""
    from . import proxywatch
    return proxywatch.INTERVAL_S // 2


def budget_crossings(alloc, prior, now, dwell):
    """([(project, family, was, colour, since, body)], new state) — LATCHED
    ON THE COLOUR, NEVER ON THE NUMBERS, AND HELD FOR THE DWELL. Burn moves
    every pass by construction; what a project needs to hear once is that
    its colour on a short family CROSSED and STAYED crossed, or that the
    family stopped being rationed for it.

    `alloc` is `allocation` over the authored teams. `prior` is the last
    state, {colours: {project: {family: colour}}, pending: {project:
    {family: {colour, since}}}}: `colours` is what was last POSTED (a family
    not rationed has no entry), `pending` a colour first read at `since` and
    not posted yet. A colour that differs from the posted one is pending
    until it has held for `dwell` seconds, then due; one that returns to the
    posted colour first is dropped, and says nothing (design read D4: a flap
    posted a line on every pass). PURE."""
    prior = prior if isinstance(prior, dict) else {}

    def keyed(name):
        got = prior.get(name)
        return {p: dict(rows) for p, rows in (got or {}).items()
                if isinstance(rows, dict)} if isinstance(got, dict) else {}
    posted, pending = keyed("colours"), keyed("pending")
    alloc = alloc or {}
    # A PROJECT WHOSE AUTHORED TEAM IS GONE HAS NO LATCH LEFT TO KEEP.
    posted = {p: rows for p, rows in posted.items() if p in alloc}
    pending = {p: rows for p, rows in pending.items() if p in alloc}
    due = []
    for project, rows in sorted(alloc.items()):
        was_rows = posted.get(project) or {}
        now_rows = {fam: row["colour"] for fam, row in rows.items()
                    if row.get("rationed")}
        waiting = pending.setdefault(project, {})
        for fam in sorted(set(was_rows) | set(now_rows) | set(waiting)):
            was, colour = was_rows.get(fam), now_rows.get(fam)
            if was == colour:
                waiting.pop(fam, None)
                continue
            held = waiting.get(fam)
            if not isinstance(held, dict) or held.get("colour") != colour \
                    or not isinstance(held.get("since"), (int, float)):
                waiting[fam] = {"colour": colour, "since": now}
                continue
            if now - held["since"] < dwell:
                continue
            if colour:
                body = ("%s %s %s: %s → %s — %s. %s. Advice only: the lead "
                        "routes this family's new work elsewhere or waits "
                        "(`helm route`, `helm team %s`)"
                        % (BUDGET_TAG, project, fam, was or "unrationed",
                           colour, line(project, fam, rows[fam]),
                           rows[fam]["say"], project))
            else:
                row = rows.get(fam) or {}
                body = ("%s %s %s: %s → no longer rationed (the family reads "
                        "%s for everyone)" % (
                            BUDGET_TAG, project, fam, was,
                            row.get("family_colour") or "unmeasured"))
            due.append((project, fam, was, colour, held["since"], body))
        if not waiting:
            pending.pop(project, None)
    return due, {"colours": {p: r for p, r in posted.items() if r},
                 "pending": pending}


def _latch_write(target, now, state):
    """None, or why the latch could not be written."""
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        pk.write_json(target, dict(state, ts=now))
        return None
    except Exception as exc:                # noqa: BLE001 — reported
        return ("the PROJECT-BUDGET latch could not be written (%s: %s)"
                % (exc.__class__.__name__, exc))


def _post_budget(body, project, leads, event_id, post=None):
    """One PROJECT-BUDGET line to the project's LEAD (design read D4): a DM
    to each lead the authored team names, or, for a team with no lead, one
    AMBIENT row in the project's room, on the record and waking nobody.
    `event_id` keys it on the crossing, so a retry is the same row."""
    if post is not None:
        for lead in leads:
            post(body, project, dm=lead)
        if not leads:
            post(body, project)
        return
    from . import chat
    for lead in leads:
        chat.post(body, room=project, dm=lead, who="proxywatch", sign=False,
                  event_id=event_id)
    if not leads:
        chat.post(body, room=project, who="proxywatch", sign=False,
                  event_id=event_id, ambient=True)


def budget_watch_pass(flags, pace, burn, now=None, post=None, reg=None,
                      auth=None, path=None):
    """The watchdog's per-project pass -> ([bodies delivered], problem).

    Reads the authored teams, folds this pass's readings, and posts one
    PROJECT-BUDGET line per crossing that has held for the dwell
    (`budget_dwell_s`), to the project's LEAD (`_post_budget`). A pass with
    no flags latches nothing: an unread fold is not a crossing.

    THE LATCH IS WRITTEN BEFORE ANYTHING POSTS (design read D4). Without it
    every pass reads the same crossing as new, so a latch write that failed
    in silence would post the same line on every pass. A latch that cannot
    be written posts nothing and says so in `problem`, which the proxywatch
    pass prints. After the posts it is written again with what
    was DELIVERED: a failed post stays owed (at least once) and is reported,
    and a delivered one never repeats; each crossing is keyed on when it was
    first read, so a retry of one the latch missed is the same chat row."""
    from . import registry
    if not flags:
        return [], None
    now = time.time() if now is None else now
    reg = registry.load(strict=True) if reg is None else reg
    auth = registry._authored_load(strict=True) if auth is None else auth
    bound = {}
    for key, rec in (reg.get("projects") or {}).items():
        said, _bad = authored(key, rec, auth)
        if said is not None:
            bound[key] = said
    # THE SLOT FAMILIES WITHOUT THE LEDGER FOLD: a local family is measured
    # in lanes, never rationed on its tokens, so it must read as SLOTS here
    # exactly as it does at route; its lanes in use are the router's
    # business, and a crossing is latched on a colour, not on a queue.
    try:
        live = live_families({m["seat"] for t in bound.values()
                              for m in t.get("members") or ()}, strict=True)
    except RosterUnread as exc:
        # UNREADABLE, AND NOTHING POSTED (round 3, ruling c): with no roster
        # every member would be billed to its typed family, so no colour
        # this pass reads is the project's; the latch is left as it was
        return [], ("UNREADABLE — the seat roster did not read (%s), so no "
                    "member's family is known; no PROJECT-BUDGET line posted "
                    "and the latch is untouched" % exc.__class__.__name__)
    alloc = allocation(flags, pace, burn, bound,
                       lights=registry.lights(reg),
                       slots=slot_reading(fold=False), live=live)
    target = path or budget_latch_path()
    prior = pk.read_json(target, default={}) or {}
    due, state = budget_crossings(alloc, prior, now, budget_dwell_s())
    was = {k: prior.get(k) or {} for k in ("colours", "pending")} \
        if isinstance(prior, dict) else {}
    if not due and state == was:
        return [], None
    problem = _latch_write(target, now, state)
    if problem:
        return [], problem + (
            ", so no PROJECT-BUDGET line posted this pass: without the latch "
            "every pass would post the same line again")
    leads = {key: sorted(m["seat"] for m in team.get("members") or ()
                         if m.get("role") == "lead")
             for key, team in bound.items()}
    delivered, failed = [], []
    for project, fam, _was, colour, since, body in due:
        try:
            _post_budget(body, project, leads.get(project) or [],
                         "project-budget:%s:%s:%s:%d"
                         % (project, fam, colour or "clear", since), post)
        except Exception as exc:            # noqa: BLE001 — owed next pass
            failed.append(exc.__class__.__name__)
            continue
        delivered.append(body)
        rows = state["colours"].setdefault(project, {})
        if colour:
            rows[fam] = colour
        else:
            rows.pop(fam, None)
        if not rows:
            state["colours"].pop(project, None)
        (state["pending"].get(project) or {}).pop(fam, None)
        if not state["pending"].get(project):
            state["pending"].pop(project, None)
    said = []
    if delivered:
        again = _latch_write(target, now, state)
        if again:
            said.append(again + " after the lines posted; each is keyed on "
                        "its crossing, so a retry is the same row")
    if failed:
        said.append("%d PROJECT-BUDGET line(s) did not post (%s); owed at "
                    "the next pass" % (len(failed),
                                       ", ".join(sorted(set(failed)))))
    return delivered, "; ".join(said) or None


# ---------------------------------------------------------------------------
# the board's teams leg
# ---------------------------------------------------------------------------

HISTORY_ROWS = 8


def _wire(row):
    """An allocation row safe for JSON: an infinite ratio (a share of 0 with
    burn on it) becomes None with `no_budget`, because JSON has no Infinity."""
    out = dict(row)
    if isinstance(out.get("ratio"), float) and math.isinf(out["ratio"]):
        out["ratio"], out["no_budget"] = None, True
    return out


def board_model(now=None):
    """The board's teams leg -> {projects, seats, pace, burn, slots, say,
    roles, tier}.

    Every project with a team, joined with what its card draws: the members
    and their state, the shares, each family's allocation (binding, or a
    PREVIEW for a proposal), the lead's drift list and the recent history.
    Beside them, what the card needs to re-fold a DRAFT in the browser the
    same way `allocation` folds a saved team: the fleet's seats (the add-seat
    picker), each rate family's supply, the per-seat burn, each slot
    family's lanes, and the owner's own sentence for each colour (`burnflags.BEHAVIOUR`, read, never copied).
    No identity: seats are seat names, and the burn file carries no account."""
    from . import burnflags, eventledger, registry, route
    now = time.time() if now is None else now
    vocab = frozenset(families())
    reg = registry.load(strict=True)
    auth = registry._authored_load(strict=True)
    world = placements(projects=reg.get("projects"), now=now)
    # THE LANES IN USE ARE FOLDED WHETHER OR NOT A TEAM NAMES A SLOT FAMILY:
    # the family sheet shows who holds a local lane before any share exists.
    failed = {}
    inputs = readings(now=now, projects=reg.get("projects"), failed=failed)
    got = view(now=now, reg=reg, auth=auth, world=world, inputs=inputs,
               failed=failed)
    rows, _err = eventledger.checked_events(events_path())
    hist = {}
    for r in rows:
        if isinstance(r, dict) and r.get("project") in got:
            hist.setdefault(r["project"], []).append(
                {k: r.get(k) for k in ("kind", "v", "by", "ts", "reason",
                                       "diff")})
    on = {}
    for key, rec in got.items():
        for m in rec["team"]["members"]:
            on.setdefault(m["seat"], []).append(key)
    seats = world.get("seats") or {}
    roster = world.get("roster") or {}
    projects = {}
    for key, rec in got.items():
        team = rec["team"]

        def state(seat):
            placed = seats.get(seat)
            if seat not in roster or not placed:
                return "wanted"
            return placed.get("presence") or "absent"
        projects[key] = {
            "v": team["v"], "authored": team["authored"],
            "binding": rec["binding"], "by": team["by"], "ts": team["ts"],
            "reason": team["reason"], "problem": team.get("problem"),
            # `spends` is the family door's answer (design read D3): the
            # card folds a draft on it, as `allocation` does
            "members": [dict(m, state=state(m["seat"]),
                             shared=[k for k in on.get(m["seat"], ())
                                     if k != key],
                             spends=rec["live"].get(m["seat"]) or m["family"])
                        for m in team["members"]],
            "shares": team["shares"],
            "allocation": {fam: _wire(row)
                           for fam, row in rec["allocation"].items()},
            "drift": rec["drift"],
            "history": hist.get(key, [])[-HISTORY_ROWS:]}
    _flags, pace, burn, slots = inputs
    return {
        "projects": projects,
        # THE ADD-SEAT PICKER offers only a seat a team can hold: one whose
        # family helm measures (`write` refuses any other)
        "seats": [{"seat": seat, "family": p.get("family"),
                   "presence": p.get("presence"), "project": p.get("project")}
                  for seat, p in sorted(seats.items())
                  if p.get("family") in vocab],
        "pace": {fam: {"sustainable_per_h": sustainable_per_h(p),
                       "tokens_per_hour": p.get("tokens_per_hour"),
                       "runway_h": p.get("runway_h"),
                       "horizon_h": p.get("horizon_h"),
                       "horizon_at": p.get("horizon_at")}
                 for fam, p in (pace or {}).items()},
        "burn": (burn or {}).get("families") if burn else None,
        "burn_window_s": (burn or {}).get("window_s"),
        # THE LANES: which families are measured in slots, each one's
        # recorded capacity (with who measured it, when and how) and the rows
        # holding its lanes by project — what the card needs to fold a slot
        # share the way `allocation` does.
        "slots": {k: slots.get(k) for k in ("families", "capacity",
                                             "capacity_problem", "in_use",
                                             "why")},
        "say": {colour: rec["say"]
                for colour, rec in burnflags.BEHAVIOUR.items()},
        "roles": {role: list(kinds_of(role)) for role in ROLES},
        # THE AXES WHOSE ORANGE A SHARE RATIONS, so the card folds a draft
        # the way `allocation` folds a saved team (read, never copied)
        "ration_axes": list(RATION_AXES),
        # THE READERS THAT RAISED, {reader: class} (design read D6): the card
        # says FAILED for them, never "unmeasured"
        "failed": dict(failed),
        # THE OWNER'S APPROVAL TIER (route E3), so the card's route preview
        # drops a family route drops at N1: a review or a verification CLOSES
        # a row, and only these families may. Read from route, never copied.
        "tier": {"kinds": list(route.TIER_KINDS),
                 "families": list(route.APPROVAL_TIER)}}
