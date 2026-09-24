"""The USABILITY JOIN behind `helm seat list` — can this seat take work NOW?

THE OWNER'S QUESTION, 2026-08-11: "why did we build the roster if we weren't
going to actually use it?" He was right, and the answer was measured the
moment he asked. `helm seat list` renders PROXY facts — proxy UP, pid, port,
cred validity, refresh-token rolling, instance caps — and it renders them
well. It renders NOTHING about whether the seat behind that proxy can take a
turn. On the day he asked:

    codex     "proxy UP pid 80247 port 8317 ... 239h06m left"   — 42 HOURS dark
    codex-4   "proxy UP pid 1101927 port 8321"                  — its PANE WAS GONE

Both lines true. Both read by everyone as "this seat is fine".

THREE SURFACES MEASURED THAT SEAT AND NONE OF THEM JOINED:

  (1) the roster / `seat list`  proxy + cred infrastructure
  (2) proxywatch                turn liveness, semantic age, starved/hung,
                                the provider wall
  (3) the dispatch/lr ledger    what work the seat is already holding

A seat can read PERFECTLY HEALTHY on (1) while being 42 hours dark on (2) and
holding a twelve-day-old row on (3). This module is the join, and the ROSTER
is where it lands — the owner's own ruling: extend the surface every seat and
the integrator already read, do not build a fourth one nobody opens.

RE-IMPLEMENTS NOTHING. Every fact here comes from the authority that already
owns it, called ONCE for the whole fleet:

    proxywatch.health(include_upstream=False, include_probe=False)
        the local rungs — the pane census (one /proc walk for the whole host),
        the semantic transcript age, and the turn_state ladder. `seats=None`
        deliberately: health walks its OWN `_watched_seats()` census, the one
        the grok incident widened, so this join cannot be one family short.

    proxywatch.upstream_snapshot()
        the CACHED per-family wall verdict. A DISPLAY MUST NOT PROBE — that
        law is already written in upstream_snapshot's own docstring, and it is
        why `include_probe=False` above exists: health's probe rung opens an
        HTTP request per seat, which is exactly the per-seat storm a verb this
        hot must never pay.

    dispatches.open_recipients()
        one fold of ONE ledger. Land requests are not a second store: an lr IS
        a dispatch row (kind="review"; a build dispatch is kind="build"), so
        this single reader covers both ledgers the owner named. The count is
        therefore rows ADDRESSED to the seat — see HOLDING below for the one
        thing it deliberately does not claim.

    seats.roster()
        the seat register's runtime/tier state — runtime_verified.

FAIL-LOUD, PER FIELD. helm's law, and the reason this join is worth having at
all: the surface it replaces was not wrong, it was CONFIDENT. Every unreadable
input renders UNKNOWN for its own field and never a healthy-looking default. A
seat whose proxywatch state cannot be read is UNKNOWN, not "ok". A ledger that
will not read makes the count UNKNOWN, not 0 — a zero would say "this seat is
free", which is the same false confidence one layer down.

AND UNKNOWN IS PRINTED HERE, NOT WITHHELD — a DELIBERATE departure from the
compact rule immediately next door in `seat.upstream_phrase`, which withholds
UNKNOWN from the list column on the attention-budget law (a badge every row
carries is a badge every reader skims). The asymmetry holds because these are
different objects. That badge is an ADDITION to a line that already says
something; this line's ENTIRE CONTENT is the verdict, so an UNKNOWN here is
the answer to the question asked, not noise beside one. A blank verdict column
would read as "usable", which is the founding defect restated.

VERDICT PRECEDENCE, and the order is the argument:

  1. a MEASURED refusal (pane GONE, provider wall, starved/hung turn) wins
     over an unreadable sibling field — the answer is already known, and
     downgrading it to UNKNOWN because some OTHER field would not read hides
     a fact helm actually holds.
  2. otherwise ANY unreadable input that could have changed the answer makes
     the verdict UNKNOWN, carrying both what was measured and which input was
     not. Never a confident USABLE from an unchecked signal.
  3. otherwise a measured impairment (a stale semantic age, a benign-but-not
     -turning state, an unverified runtime) is DEGRADED, naming it.
  4. otherwise USABLE.

HOLDING is "open ledger rows ADDRESSED to this seat", and the label is exact
on purpose. `dispatches.owed()` already answers supersession, so a row a
sibling carries is not counted twice. What it does NOT claim is the author
leg: an lr in CHANGES_REQUESTED is owed by its SENDER, and resolving that
needs `landreq.loops()`, whose per-row git observation is a subprocess storm
measured at >120s on a cold worktree — categorically too expensive for a verb
run this often. So the field says what it measured and no more.

THE RETURN IS A CONTRACT, AND THE RENDER IS BUILT ON TOP OF IT — owner catch
mid-build, 2026-08-11: "another p0 impl, another built-not-wired class, no?"
He is right, and it is the same defect one layer up. A roster that RENDERS
usability is still an instrument somebody has to READ, which is exactly how
five degraded seats sat unnoticed this morning while proxywatch, seat-where
and silent-drop had all measured them correctly. So the primary artifact is
this typed answer, and `line()` is a consumer of it like any other:

    join(...) -> {seat: row}, each row carrying

        verdict        USABLE | DEGRADED | UNUSABLE | UNKNOWN
        reason         "" for USABLE, else the named cause
        can_take_work  True  — USABLE, and DEGRADED (it can, with a caveat)
                       False — UNUSABLE, a MEASURED refusal
                       None  — UNKNOWN, helm could not tell
        holding        int, or None when the ledger would not read
        measured_at    epoch seconds of the pass that produced this row
        + the raw fields every verdict was derived from (turn_state,
          turn_evidence, semantic_age_s, pane, upstream, upstream_since,
          upstream_dark, runtime_verified, registered, reachable, unknown)

    seat_verdict(seat, ...) -> (verdict, reason, row) for ONE seat, scoped so
    a router pays for the seat it is asking about and not the whole fleet.

`can_take_work is False` is the routing predicate, and it is deliberately the
only value a guard may refuse on. None (UNKNOWN) must be announced and must
not block: a guard that refuses on ABSENCE downgrades every case whose
evidence merely aged out, and on a box where proxywatch has never run it would
refuse the entire fleet's work. Refuse on the measured contradiction; SAY the
unknown.
"""
import time

USABLE = "USABLE"
DEGRADED = "DEGRADED"
UNUSABLE = "UNUSABLE"
UNKNOWN = "UNKNOWN"

# The ledger fold a routing caller deliberately skips. A sentinel, not None:
# None already means "read and unreadable", and collapsing the two would let a
# skipped read render as a failed one.
_NOT_ASKED = object()

# The staleness bar is proxywatch's, not a second opinion: HANG_S is what the
# turn ladder itself calls stale, and a roster that drew its own line would let
# two surfaces disagree about the same seat — which is the whole defect here.
def _stale_s():
    from . import proxywatch
    return proxywatch.HANG_S


# turn_state values that mean the seat cannot complete a turn right now.
# `unonboarded` belongs here and not with the impaired: it is a MEASURED
# refusal — the seat has completed no turn since its latest launch failed to
# submit its brief, so it cannot take one now, and the repair is a guarded
# pane read rather than time.
_TURN_UNUSABLE = ("off", "starved", "hung", "unonboarded")
# measured, benign, and still not "a turn completed inside the window".
_TURN_IMPAIRED = ("thinking", "compact-needed", "fresh", "idle")
# turn_state values that ARE an unreadable input rather than a measured
# condition. They belong to rung 2 (UNKNOWN), never to the impairment rung:
# the seat may be entirely healthy and helm cannot say so. Without this, an
# unenumerated state falls through every rung and a seat whose register helm
# CANNOT READ renders USABLE with no reason — a false green on the one line
# the owner scans. Scoped to the state this rung was added for; the older
# `hung-unknown` keeps whatever path it already had.
_TURN_UNKNOWN = ("onboarding-unreadable",)


def _fmt_age(seconds):
    """"42h13m" — HOURS, never days, and that is the point.

    The bar this age is read against is 45 MINUTES (proxywatch.HANG_S). A
    reader comparing "1d18h" to a 45-minute bar has to do arithmetic in their
    head before the number means anything, and the owner's own report of this
    incident was "42 hours dark" — the unit he actually thinks in. The three
    other `_fmt_age`s in this tree (stalebot, seats_report, storage_matrix)
    all roll to days, and importing one of them costs 0.16s of transitive
    ledger imports on a verb that currently runs in 0.12s total.
    """
    seconds = max(0, int(seconds or 0))
    return "%dh%02dm" % (seconds // 3600, (seconds % 3600) // 60)


# ---------------------------------------------------------------------------
# the four readers — each one wrapped so a failure becomes a REASON, never a
# default. None of them measures anything itself.
# ---------------------------------------------------------------------------

def _read_health(health=None, names=None):
    """({seat: row}, error) — proxywatch's local rungs.

    ONE pass, no upstream canary (that spends provider tokens) and NO PROBE
    (that is an HTTP request per seat). `names=None` hands the enumeration to
    health's own `_watched_seats()`, which is already the widened census — the
    ROSTER wants that, because a seat the watch does not cover is itself news.
    A ROUTER asking about one recipient passes that one name and pays for one
    seat: `dispatches` already reads health this way (seats=[recipient]).
    """
    from . import proxywatch
    fn = health or proxywatch.health
    try:
        kwargs = {"include_upstream": False, "include_probe": False}
        if names is not None:
            kwargs["seats"] = list(names)
        rep = fn(**kwargs)
    except Exception as e:                  # noqa: BLE001 — a display never raises
        return None, ("proxywatch health pass failed (%s: %s)"
                      % (e.__class__.__name__, e))
    rows = (rep or {}).get("seats")
    if not isinstance(rows, list):
        return None, "proxywatch health returned no seat rows"
    return {r.get("seat"): r for r in rows if isinstance(r, dict)}, None


def _read_upstream(snapshot=None):
    """({family: record}, error) — the CACHED wall verdict. Never a network
    call; the snapshot reader owns missing / corrupt / STALE and hands its own
    reason back, which this module prints rather than reinterprets."""
    from . import proxywatch
    fn = snapshot or proxywatch.upstream_snapshot
    try:
        up, err = fn()
    except Exception as e:                  # noqa: BLE001
        return None, ("proxywatch upstream snapshot failed (%s: %s)"
                      % (e.__class__.__name__, e))
    if err:
        return None, err
    if not isinstance(up, dict):
        return None, "proxywatch upstream snapshot is not a family map"
    return up, None


def _read_holding(open_recipients=None):
    """({recipient: open-row count}, error) — one fold of the ONE ledger that
    holds both dispatches and land requests. None is 'helm could not read the
    obligations', never an empty board."""
    from . import dispatches
    fn = open_recipients or dispatches.open_recipients
    try:
        counts, unavailable = fn()
    except Exception as e:                  # noqa: BLE001
        return None, ("the dispatch/lr ledger fold failed (%s: %s)"
                      % (e.__class__.__name__, e))
    if unavailable or not isinstance(counts, dict):
        return None, (unavailable or "the dispatch/lr ledger did not read")
    return counts, None


def _read_roster(register=None):
    """({seat: register row}, error) — the seat register's runtime/tier.

    THE ACCESSOR IS CALLED AT ITS USE SITE, never bound to a local. The
    roster count-pin (tests/test_display_launder_tripwire.py) walks the AST
    for `seats.roster()` CALLS, so `fn = roster or seats.roster` hid this
    module's read from the guard that exists to review every roster consumer —
    it refused the lane, correctly, on exactly that line.

    AND THE TEST SEAM IS `register`, NOT `roster`, FOR THE SECOND HALF OF THE
    SAME LAW. Named `roster`, the injected callable's own call site READS as a
    roster accessor to that AST scan — the pin counted 2 where this module has
    exactly 1 real register read, and it was right to: a local shadowing the
    accessor's name is precisely the ambiguity the guard exists to notice. One
    name, one meaning.
    """
    from . import seats
    try:
        reg = register() if register is not None else seats.roster()
    except Exception as e:                  # noqa: BLE001
        return None, ("the seat register did not read (%s: %s)"
                      % (e.__class__.__name__, e))
    if not isinstance(reg, dict):
        return None, "the seat register is not readable as a roster"
    return reg, None


def _beacon_stale_s():
    """How old an attendance verdict may still gate a routing decision.

    DERIVED FROM THE CENSUS CADENCE RATHER THAN DECLARED BESIDE IT, because a
    bound written as a number drifts the moment the timer interval changes and
    nothing reports the drift. Twelve passes is long enough that an ordinary
    late or skipped census never invents an UNKNOWN, and short enough that a
    STOPPED census stops gating within the hour.

    THE STALENESS RULE IS THE POINT, NOT A PRECAUTION. This register is
    written by a systemd timer, and a timer can be uninstalled, stopped or
    never wired at all — a fleet where that has happened carries no attendance
    on any row. A verdict that kept gating after its writer stopped would
    freeze a seat at DEAF long after it re-armed, which is the same defect one
    layer up: a measurement that keeps steering after it expires."""
    try:
        from . import beacons
        return max(int(beacons.INTERVAL_S) * 12, 600)
    except Exception:                       # noqa: BLE001
        return 3600


def _read_reachable(entry, now):
    """(reachable, why) — can helm WAKE this seat, per the attendance register.

    True   a census PROVED a live wake path.
    False  a census PROVED there is none. This is beacons' DEAF, and DEAF is
           defined there as a PROVEN ABSENCE of any wake path — the same
           measured contradiction this module refuses on everywhere else.
    None   nothing proved either way, which is the honest answer for a seat
           with no register row, an UNPROVEN verdict, or a reading too old.

    REACHABILITY IS NOT HEALTH, AND THAT IS WHY THIS FIELD HAD TO EXIST. A
    seat can answer USABLE on every other rung — pane live, upstream healthy,
    turn ok, runtime verified — while holding work it can never be woken to
    read. Under the native-wake-only law a seat with no armed beacon is a
    mailbox nobody opens, and no amount of health says so.

    UNPROVEN AND VACANT BOTH ANSWER None, FOR DIFFERENT REASONS. UNPROVEN is
    the census saying its instruments could not tell, and refusing on that
    would brick routing wherever the census is degraded. VACANT is a PROVEN
    LIVE wake path with a proven empty house — the wake works, so the row is
    delivered and waits for whoever next occupies the seat, which is the
    ordinary between-panes case rather than a hole."""
    att = (entry or {}).get("attendance") if isinstance(entry, dict) else None
    if not isinstance(att, dict):
        return None, ("no attendance verdict on this seat's roster row — "
                      "`helm beacons --post` writes it")
    state = att.get("state")
    at = att.get("at")
    if state not in ("covered", "DEAF"):
        return None, ("beacon census answered %s: %s"
                      % (state, att.get("why") or "no reason recorded"))
    bound = _beacon_stale_s()
    try:
        age = None if at is None else float(now) - float(at)
    except (TypeError, ValueError):
        age = None
    if age is None:
        return None, "the attendance verdict carries no measurement time"
    if age > bound:
        return None, ("the attendance verdict is %s old, past the %s census "
                      "bound — too stale to gate on"
                      % (_fmt_age(age), _fmt_age(bound)))
    if state == "DEAF":
        return False, (att.get("why")
                       or "no live beacon: helm cannot wake this seat")
    return True, None


def _recipient(name, canonical=None):
    """(ledger recipient, error) for one seat name — the SAME resolution
    proxywatch's own fuse applies before counting a seat's open rows, so the
    two surfaces cannot disagree about whose rows these are."""
    from . import seats
    fn = canonical or seats._canonical_recipient
    try:
        who, err = fn(name)
    except Exception as e:                  # noqa: BLE001
        return None, ("seat name %r does not resolve to a ledger recipient "
                      "(%s: %s)" % (name, e.__class__.__name__, e))
    if err or not who:
        return None, (err or "seat name %r has no canonical recipient" % name)
    return str(who), None


# ---------------------------------------------------------------------------
# the join
# ---------------------------------------------------------------------------

def _seat_row(name, hrow, herr, up, uperr, holding, holderr, reg, regerr,
              canonical=None, panes=None, panes_blind=None, now=None):
    """One seat's joined row. `unknown` maps FIELD -> why it could not read;
    an empty `unknown` is the only thing that lets a verdict be USABLE."""
    row = {"seat": name, "family": None, "turn_state": None,
           "turn_evidence": None, "semantic_age_s": None, "pane": None,
           "upstream": None, "upstream_since": None, "upstream_dark": None,
           "holding": None, "runtime_verified": None, "registered": None,
           "reachable": None, "reachable_why": None,
           "runtime_unreadable": None, "scope": "proxy",
           "holding_scope": "measured", "unknown": {}}

    # --- (2) proxywatch: turn state, semantic age, pane liveness -----------
    if herr:
        for field in ("turn", "last", "pane"):
            row["unknown"][field] = herr
    elif hrow is None or hrow.get("error"):
        # PANE-ONLY SCOPE, AND IT IS A MEASURED STRUCTURAL FACT, NOT BLINDNESS.
        #
        # Caught by wiring the router before shipping it: proxywatch watches
        # MINTED PROXY seats, so every claude-native seat, the integrator included,
        # came back turn/last/pane UNREADABLE — and the dispatch gate would
        # then have stamped "usability UNKNOWN" on the MAJORITY of dispatches
        # helm sends. That is crying wolf, and a warning every row carries is a
        # warning nobody reads.
        #
        # A native claude seat is not UNMEASURED, it is OUT OF PROXY SCOPE:
        # there is no proxy, so there is no provider wall and no per-instance
        # transcript to date. What helm CAN still answer for it is the question
        # that caught a codex seat — does a live process hold this seat name —
        # and that census covers every claude on the host by HELM_CHAT_NAME,
        # seat class irrelevant. So the pane verdict is real here and the two
        # fields that genuinely do not apply render n/a rather than UNKNOWN.
        #
        # THE GAP THIS LEAVES, NAMED RATHER THAN HIDDEN: a native seat with a
        # live pane can still be hung, and nothing here would say so. Closing
        # it needs `seat.seat_liveness`'s pane-tail read — one subprocess,
        # affordable on the single-seat router path and NOT on a roster scan.
        # That is the next lane's work, not a silent assumption of health.
        row["scope"] = "pane-only"
        row["scope_why"] = str((hrow or {}).get("error")
                               or "proxywatch's minted-seat census does not "
                                  "cover %r — no proxy, so no turn age and no "
                                  "provider wall exist for it" % name)
        if panes_blind:
            row["unknown"]["pane"] = panes_blind
        else:
            row["pane"] = name in (panes or ())
    else:
        row["family"] = hrow.get("family")
        row["turn_state"] = hrow.get("turn_state")
        row["turn_evidence"] = hrow.get("turn_evidence")
        row["semantic_age_s"] = hrow.get("transcript_age_s")
        row["pane"] = hrow.get("pane_live")
        if row["turn_state"] is None:
            row["unknown"]["turn"] = "proxywatch recorded no turn verdict"
        if row["semantic_age_s"] is None:
            row["unknown"]["last"] = ("no COMPLETED semantic transcript entry "
                                      "could be dated for this seat")
        if row["pane"] is None:
            # THREE-VALUED, and None is the census saying it could not look at
            # the whole host. It must never collapse into GONE, which is a
            # positive claim, nor into live, which is the founding defect.
            row["unknown"]["pane"] = (hrow.get("census_blind")
                                      or "the live-pane census could not look")

    # --- (2) proxywatch: the provider wall, from the CACHE ------------------
    if row["scope"] == "pane-only":
        pass                        # no proxy, so there is no wall to look up
    elif uperr:
        row["unknown"]["upstream"] = uperr
    elif row["family"] is None:
        row["unknown"]["upstream"] = ("this seat's family is unresolved, so no "
                                      "upstream verdict can be looked up")
    else:
        from . import proxywatch
        raw = (up or {}).get(row["family"])
        if not isinstance(raw, dict) or not raw.get("state"):
            row["unknown"]["upstream"] = ("proxywatch has recorded no verdict "
                                          "for family %s" % row["family"])
        else:
            rec, rec_err = proxywatch.upstream_record(
                {"upstream": up}, row["family"])
            if rec_err:
                row["unknown"]["upstream"] = rec_err
            else:
                row["upstream"] = rec["state"]
                row["upstream_since"] = rec.get("since")
                row["upstream_dark"] = rec.get("dark") is True

    # --- (3) the dispatch/lr ledger ----------------------------------------
    # NOT-ASKED IS NOT UNKNOWN, and the difference is the whole reason the
    # routing caller can afford this join. `holding` is a DISPLAY fact — no
    # rung of the verdict reads it — so a guard on the dispatch write path
    # passes need_holding=False and never folds the ledger. It then renders
    # `holding=-` (not 0, which would claim the seat is free, and not UNKNOWN,
    # which would claim helm looked and failed). Measured: without this the
    # gate added a second ledger snapshot to every `dispatch add`, which
    # tests/test_dispatch_chain.py pins against by call count.
    if holding is _NOT_ASKED:
        row["holding_scope"] = "not-asked"
    elif holderr:
        row["unknown"]["holding"] = holderr
    else:
        who, err = _recipient(name, canonical=canonical)
        if err:
            row["unknown"]["holding"] = err
        else:
            try:
                row["holding"] = int(holding.get(who, 0))
            except (TypeError, ValueError):
                row["unknown"]["holding"] = (
                    "the ledger fold returned a non-numeric count for %r" % who)

    # --- (1) the seat register: runtime / tier ------------------------------
    # DELIBERATELY NOT IN `unknown`, and this is the one asymmetry in the
    # module. `unknown` is the GATE — the fields whose absence means helm
    # cannot say whether the seat can take a turn. The runtime label answers a
    # different question: not "will it answer" but "do you know WHAT answers".
    # An unverified runtime is real (it means the label was seeded by a foreign
    # process, or never stamped) and it PRINTS, but 3 of 8 live seats carry it
    # while working fine, and degrading all three would teach every reader to
    # skim the verdict column — which is how the one row that says UNUSABLE
    # arrives to an audience that stopped reading. NO REGISTER ROW AT ALL is
    # the exception and does gate: a seat helm cannot address work to cannot
    # take work, whatever its proxy says.
    if regerr:
        row["runtime_unreadable"] = regerr
    else:
        entry = reg.get(name)
        row["registered"] = isinstance(entry, dict)
        row["runtime_verified"] = (entry or {}).get("runtime_verified") \
            if row["registered"] else None
        # SAME READ, NO NEW COST: the attendance register lives ON the roster
        # row this branch already holds, so reachability joins the verdict
        # without a second read, a probe, or a process census.
        row["reachable"], row["reachable_why"] = _read_reachable(entry, now)
    return row


def _read_panes(live_seats=None):
    """(set of seat names holding a live process, HOST-WIDE blind reason or
    None, {seat: that seat's own blind reason}).

    proxywatch's `_live_seats` — ONE /proc walk for the whole host, and
    the only reader here that answers for a NATIVE claude seat as well as a
    proxied one, because it keys on HELM_CHAT_NAME rather than on a proxy.
    Three-valued at the census level: a blind reason is a string, and it must
    never collapse into "nobody holds this seat", which is a positive claim.

    THE THIRD ELEMENT IS THE BLAST-RADIUS BOUND, and `join` renders a row per
    seat, so it is the shape this caller needs: a refusal raised while keying
    ONE roster seat must reach that seat's row alone. Measured before the split
    (task/2739): one renamed seat put its own pid and refusal sentence into the
    `helm seat list` line of nine other seats and turned 29 measured Falses
    into UNKNOWN. A HOST-WIDE reason — an unidentified pid, a census that
    raised — still reaches every row, because that pid could be any seat's.

    ONE PRODUCER, ONE SHAPE. `proxywatch._live_seats` is the only host walk and
    it answers the triple, so an injected double answers the triple too. A
    tolerated shorter answer was tried and removed: it is a branch no producer
    reaches, and a seam that silently accepts a narrower double is how a stub
    stops covering the door it was written for.
    """
    from . import proxywatch
    fn = live_seats or proxywatch._live_seats
    try:
        names, blind, per_seat = fn()
    except Exception as e:                  # noqa: BLE001
        return set(), ("the live-pane census could not be taken (%s: %s)"
                       % (e.__class__.__name__, e)), {}
    return set(names or ()), (blind or None), dict(per_seat or {})


def join(seats=None, health=None, upstream=None, open_recipients=None,
         register=None, canonical=None, health_seats=None, now=None,
         live_seats=None, need_holding=True):
    """{seat name: typed row} — FOUR reads total, whatever the fleet size.

    See the module docstring for the row contract. The four readers run ONCE
    each here and every seat is derived from those same four results, so N
    seats never cost N ledger folds or N process censuses.

    `seats` widens the result to names the CALLER renders that proxywatch does
    not watch (they come back UNKNOWN, which is the honest answer and also
    surfaces a census disagreement between the two surfaces). `health_seats`
    NARROWS what proxywatch measures, for a router asking about one recipient.
    `need_holding=False` skips the ledger fold entirely for a caller that only
    wants the verdict — no rung of the verdict reads the count.
    """
    now = time.time() if now is None else now
    hrows, herr = _read_health(health, names=health_seats)
    up, uperr = _read_upstream(upstream)
    holding, holderr = (_read_holding(open_recipients) if need_holding
                        else (_NOT_ASKED, None))
    reg, regerr = _read_roster(register)
    panes, panes_blind, panes_blind_by_seat = _read_panes(live_seats)
    names = sorted(set(seats or ()) | set((hrows or {}).keys()))
    out = {}
    for n in names:
        # ONE SEAT'S REFUSAL REACHES ONE SEAT'S ROW. `panes_blind` is host-wide
        # doubt (an unidentified pid could be anybody's); the keyed entry is
        # doubt about THIS seat only. See `_read_panes` for the measurement.
        row = _seat_row(n, (hrows or {}).get(n), herr, up, uperr, holding,
                        holderr, reg, regerr, canonical=canonical,
                        panes=panes,
                        panes_blind=panes_blind or panes_blind_by_seat.get(n),
                        now=now)
        # THE VERDICT RIDES THE ROW. A caller that has to remember to call a
        # second function to find out what the row MEANS is a caller that will
        # eventually not, and every such caller would then have its own idea
        # of what "degraded" routes to.
        row["verdict"], row["reason"] = verdict(row)
        row["can_take_work"] = {USABLE: True, DEGRADED: True,
                                UNUSABLE: False}.get(row["verdict"])
        row["measured_at"] = now
        out[n] = row
    return out


# A DARK VERDICT NAMES WHAT IT MEASURED. IT ASSERTS AN ORIGIN ONLY WHERE THE
# STATE TOKEN ITSELF ENCODES ONE.
#
# ROUND ONE of this cure split the states into three buckets and still assigned
# causes to two of them. Review showed that was the same defect one level down:
#
#   * EMPTY200 / MALFORMED200 prove only that HELM'S OWN CANARY ENDPOINT
#     answered HTTP 200 and OUR validation failed. They do NOT prove the
#     PROVIDER answered — the canary talks to cli-proxy-api, not to the
#     vendor — and nothing in the state proves a reseed or a cred swap would
#     be inert. My round-one wording asserted both.
#   * "the provider is refusing this seat's family" is FALSE for
#     AUTH-UNAVAILABLE (the credential is unavailable to US), for a client-side
#     TIMEOUT-500, for a RATE-LIMITED whose origin is not recorded, for the
#     FAMILY-MIXED aggregate and for a latched UNKNOWN — and merely
#     UNSUPPORTED for generic 4xx/5xx/overload.
#
# So the rule is: NEUTRAL MEASURED-STATE WORDING FOR EVERY
# UNTYPED-ORIGIN STATE, and local wording only where the token encodes the
# origin. Two tokens do: PROXY-COOLDOWN and PROXY-LOCAL-403 are ours by
# construction (proxywatch reads the proxy's local-origin mark before the body).
#
# The cost of the old sentence was not cosmetic. One family read UNUSABLE for five
# days under "the provider is refusing this seat's family" (task/1903) while
# the canary was refusing a well-formed carrier reply on shape — a verdict that
# sends every reader at the provider, the account or the family catalog, where
# no reseed and no cred swap can help.
_DARK_OURS = frozenset(("PROXY-COOLDOWN", "PROXY-LOCAL-403"))
_DARK_OUR_VALIDATION = frozenset(("EMPTY200", "MALFORMED200"))


def _dark_reason(state, since):
    """One dark state -> a sentence that claims no more than the state proves."""
    when = since or "?"
    # AND THE WORD "upstream" IS ABSENT HERE, not merely explained away. A
    # lead of "upstream PROXY-COOLDOWN" followed by a tail of "HELM'S OWN
    # PROXY is in cooldown" is a claim of origin beside its own denial: a
    # reader skimming for whether a provider is down reads the LEAD, and no
    # tail unsays a claim the lead already made. The word belongs only to the
    # untyped branch below, where it reports that no origin was recorded.
    if state == "PROXY-LOCAL-403":
        # No cooldown was measured, so the mirror sentence below would invent
        # one; the repair of a refusal our proxy minted is ours.
        return ("%s since %s — HELM'S OWN PROXY refused this itself before "
                "any request left the box. That names WHERE this refusal "
                "happened and measures NOTHING about the provider; the repair "
                "is ours: read the proxy's stated reason, fix it, restart and "
                "probe" % (state, when))
    if state in _DARK_OURS:
        return ("%s since %s — HELM'S OWN PROXY refused before any request "
                "left the box. That names WHERE this refusal happened and "
                "measures NOTHING about the provider: a local cooldown is "
                "often the MIRROR of an upstream wall, because the proxy cools "
                "a credential precisely when a provider refuses it"
                % (state, when))
    if state in _DARK_OUR_VALIDATION:
        return ("%s since %s — helm's canary endpoint answered HTTP "
                "200 and HELM'S OWN validation rejected the reply. This names "
                "the measurement, not an origin: it does not establish who "
                "answered, and the state cannot say whether any repair would "
                "change it" % (state, when))
    # EVERY OTHER STATE HAS AN UNTYPED ORIGIN. Naming one would be inventing
    # it, so the sentence reports the observation and stops.
    return ("upstream %s since %s — this seat's family is not answering "
            "usably. The recorded state does not encode WHERE the failure "
            "originated, so no cause is asserted here" % (state, when))


# ---------------------------------------------------------------------------
# THE ONE AVAILABILITY PREDICATE — "is this seat's vendor answering right now?"
# ---------------------------------------------------------------------------
# THE OWNER'S RULING: "I basically just want them to be available whenever
# their credits work. and shouldnt they show as unavailable in the roster if
# their credits dont work automatically."
#
# The state he was reading: a FAMILY-DARK line from `helm proxywatch` next to a
# roster calling the same seat `absent` and a fleet row whose only UNKNOWN was
# about something else — three surfaces, three answers, one seat, one minute.
# `helm fleet`, `helm chat seats` and `helm lr list` carried NO family word at
# all, while the web roster derived one privately with its own composition.
#
# WHY THIS IS A SECOND FUNCTION AND NOT A RUNG OF `verdict` ABOVE. The verdict
# ladder answers "can this seat take work", and it deliberately puts the PANE
# rung ahead of the wall rung — so grok, whose pane is also gone, reads "pane
# GONE" and the wall it is stacked on top of never reaches the sentence a
# reader quotes. That precedence is right for ROUTING and wrong for the
# owner's question, which is about the VENDOR and nothing else. So this
# predicate answers the vendor question alone, from the same cached record, and
# the ladder keeps its order. Nothing here re-derives a wall: `dark` is the bit
# proxywatch composed at write time and every caller passes it through.
#
# THREE VALUES THAT NEVER SHARE ONE, and the DISCRIMINATOR IS THE DARK LATCH,
# never the state token:
#
#   UNAVAILABLE  rec["dark"] is True. A wall was MEASURED and nothing has
#                measured recovery. This holds even when the token itself
#                reads "UNKNOWN" — proxywatch latches exactly that shape when
#                a walled family drops out of the census, and reading the
#                token there would INVENT RECOVERY, which is the one thing
#                `delivery_pause`'s own docstring forbids. The reason word then
#                falls back to the recorded last dark cause.
#   AVAILABLE    a validated record whose state is HEALTHY. The only value
#                that may be composed from a positive reading.
#   UNKNOWN      helm could not tell: the snapshot is missing, unreadable or
#                STALE, the family has no readable record, or the record says
#                UNKNOWN with no dark latch. It carries WHY, and a guard must
#                never refuse on it (see the module docstring).
#
# AND A FOURTH THAT IS NOT AN AVAILABILITY ANSWER AT ALL. A seat with no proxy
# family — every native claude seat, which is the MAJORITY of this fleet — has
# no vendor to be walled by. AVAILABLE there would be a confident claim from a
# signal nobody read, and UNKNOWN there would put a badge on most rows in the
# estate, which is how a badge stops being read. So it answers NO_VENDOR and
# every surface below renders NOTHING for it — the same silence web_roster's
# `_annotate_upstream` already chose for the same seats, for the same reason.
#
# COMING BACK IS AUTOMATIC AND NEEDS NO RELAUNCH, and that was MEASURED rather
# than assumed: the seat process holds ANTHROPIC_BASE_URL pointed at its own
# long-lived loopback sidecar on a deterministic port, and the token baked into
# its env is helm's OWN bearer for that sidecar, not the vendor credential. So
# a vendor credit that expires and returns never invalidates anything the seat
# holds. proxywatch recomputes every family from scratch each pass (a systemd
# user timer, OnUnitActiveSec=900s), so the worst case from vendor-green to
# every surface below reading AVAILABLE is ONE cycle — under 15 minutes, with
# no human step and no relaunch. The one thing that DOES still want a relaunch
# is a GONE PANE, which is a different rung of a different question, and the
# surfaces below say so rather than prescribing a respawn for a wall.
AVAILABLE = "AVAILABLE"
UNAVAILABLE = "UNAVAILABLE"
# NOT a state a reader scans for — the answer that this question does not
# apply. Spelled with a hyphen so no substring search for either real verdict
# word can ever match it.
NO_VENDOR = "NO-VENDOR"


def _availability(state, family, reason=None, since=None, why=None):
    rec = {"state": state, "family": family, "reason": reason,
           "since": since, "why": why, "origin": None, "detail": None}
    rec["text"] = availability_text(rec)
    if state == UNAVAILABLE:
        # WHOSE FAILURE THIS IS, ASKED OF THE MODULE THAT OWNS THE STATE NAMES,
        # and the long form written BY that classification rather than beside
        # it. A PROXY-COOLDOWN is HELM'S OWN proxy refusing before any
        # request leaves the box, so any sentence reading "their provider is
        # refusing" sends every reader at the provider, the account and the
        # family catalog, where nothing they can do will help. That is the cost
        # `_dark_reason` was written to stop paying (task/1903), so this reuses
        # it instead of growing a second opinion: ONE sheet for what a dark
        # state claims, and `text` stays the short neutral form asserting
        # nothing.
        from . import proxywatch
        rec["origin"] = proxywatch.dark_origin(reason)
        rec["detail"] = _dark_reason(reason, since)
    return rec


def availability_text(rec):
    """The ONE rendering, so no surface spells this fact its own way.

      UNAVAILABLE kimi AUTH-401 since 2026-09-13T00:55:03Z
      AVAILABLE codex
      UNKNOWN grok — proxywatch state is 71m old, bar 40m ...

    NO_VENDOR renders the EMPTY STRING: a surface printing this text renders
    nothing at all for a seat the question does not apply to."""
    state = (rec or {}).get("state")
    family = (rec or {}).get("family") or "?"
    if state == UNAVAILABLE:
        return "%s %s %s since %s" % (UNAVAILABLE, family,
                                      (rec or {}).get("reason") or UNKNOWN,
                                      (rec or {}).get("since") or "?")
    if state == AVAILABLE:
        return "%s %s" % (AVAILABLE, family)
    if state == UNKNOWN:
        return "%s %s — %s" % (UNKNOWN, family,
                               (rec or {}).get("why") or "no reason recorded")
    return ""


def _availability_stale_s():
    from . import proxywatch
    return proxywatch.UPSTREAM_CACHE_FRESH_S


# The two family tokens that carry no usable cause word of their own: the
# all-dark-but-mixed aggregate, and the latch proxywatch writes when a walled
# family leaves the census. For these the SEAT's own record is asked for a
# sharper word — never for the dark bit, which stays the family's.
_AVAIL_VAGUE = ("FAMILY-MIXED", UNKNOWN)


def _availability_reason(up, family, seat, rec):
    """The cause WORD for one UNAVAILABLE seat. The family token unless that
    token names no cause, in which case the seat's own record and then the
    recorded last dark cause are asked, in that order."""
    from . import proxywatch
    state = rec.get("state")
    if state not in _AVAIL_VAGUE:
        return state
    srec, serr = proxywatch.upstream_seat_record({"upstream": up}, family, seat)
    if not serr and isinstance(srec, dict) \
            and srec.get("state") not in _AVAIL_VAGUE:
        return srec.get("state")
    last = rec.get("last_dark_state")
    return last if isinstance(last, str) and last else state


# ── THE ONE ACQUISITION ─────────────────────────────────────────────────────
# A RENDER THAT ASKS TWICE PUBLISHES TWO INSTANTS. One console roster row is
# composed by TWO producers: seats_report stamps the five availability cells,
# and web_roster joins the raw upstream fields onto the same row. Each read the
# persisted record for itself, so a vendor recovering between those two reads
# published AVAILABLE beside an UNAVAILABLE mark and a walled flag from the
# earlier instant — this lane's founding defect wearing the cure's clothes, and
# `availability_walled` is a routing input, not only a glyph. So the record is
# acquired ONCE per render by whoever owns that render, and handed down: every
# consumer of one render reads the SAME bytes or none.
#
# THE BAR TRAVELS WITH THE BYTES. Staleness is a judgement about this record,
# and a consumer holding its own number while reading someone else's bytes is
# the same two-opinions defect one field over — so `stale_s` rides IN the
# record rather than in each call.
#
# THE BEST READABLE SNAPSHOT, not the primary alone: proxywatch's delivery
# reader falls back to its byte-equivalent last-good copy, so deleting or
# corrupting the primary during a dark episode cannot read as recovery.
def availability_snapshot(stale_s=None):
    """{upstream, age, error, stale_s} — ONE persisted read for a whole render.

    Three answers, never two: a readable family map, or an `error` naming why
    there is none. An unreadable or absent record is NOT "everyone is fine" —
    that is the could-not-look-recorded-as-a-fact class, and availability is
    exactly the surface where silence reads as health.

    `upstream` is always a DICT on the no-error path, empty included: a None
    there would send `availability` to its own fallback read and quietly mint
    the second acquisition this function exists to remove.
    """
    import os
    from . import proxywatch
    try:
        st, err = proxywatch._read_delivery_state()
        if err:
            return {"upstream": {}, "age": None, "error": err,
                    "stale_s": stale_s}
        age = None
        try:
            age = max(0, int(time.time()
                             - os.path.getmtime(proxywatch._state_path())))
        except OSError:
            pass                   # the map is still good; only its age is not
        up, shape_err = proxywatch.upstream_records(st)
        return {"upstream": (up if isinstance(up, dict) else {}), "age": age,
                "error": shape_err, "stale_s": stale_s}
    except Exception as e:         # noqa: BLE001 — a display never raises
        return {"upstream": {}, "age": None, "stale_s": stale_s,
                "error": ("proxywatch state unreadable — %s"
                          % e.__class__.__name__)}


def _snapshot_seams(snapshot, stale_s):
    """(upstream, error, age, stale_s) out of ONE acquired snapshot. A caller's
    explicit `stale_s` still wins, so a surface may keep its own bar while
    reading the render's bytes."""
    snapshot = snapshot or {}
    return (snapshot.get("upstream"), snapshot.get("error"),
            snapshot.get("age"),
            snapshot.get("stale_s") if stale_s is None else stale_s)


def roster_runtimes():
    """{seat: (runtime, verified)} off ONE roster read, or {}.

    THE FAMILY A SEAT'S NAME IMPLIES IS NOT AUTHORITY. A pi harness calls its
    seat `pi-codex` while the credential wall belongs to family `codex`, and
    only the launch metadata the launched process self-wrote says so. A surface
    that hands `availability_map` names alone therefore DISCARDS the authority
    and falls back to name parsing, which answers NO_VENDOR for that seat — the
    roster shows a dark family and the fleet table shows silence about the same
    wall. Surfaces with no roster read of their own ask here.
    """
    try:
        from . import seats_common
        rows = seats_common.roster()
        return {n: (r.get("runtime"), r.get("runtime_verified") is True)
                for n, r in (rows or {}).items() if isinstance(r, dict)}
    except Exception:                       # noqa: BLE001 — a display never
        return {}                           # dies for a family word


def availability(seat, upstream=None, upstream_error=None, age=None,
                 stale_s=None, runtime=None, runtime_verified=False,
                 family=None, snapshot=None):
    """{state, family, reason, since, why, text} for ONE seat — the predicate.

    `upstream` lets a caller that has ALREADY read the persisted record hand it
    down (the web console reads it once per roster cache rebuild and must not
    read it again per row); pass `upstream_error` and `age` from the same read
    so staleness is judged against the caller's own bar. Supplied nothing, this
    reads `proxywatch.upstream_snapshot()`, which owns missing / corrupt /
    stale and hands back its own reason — printed here, never reinterpreted.

    `snapshot` is the render's ONE acquisition (see availability_snapshot) and
    supplies the map, its error, its age and its staleness bar together; it is
    the form every multi-surface render uses, and the loose seams remain for a
    caller holding only some of those facts.

    NEVER A PROBE, at any call depth. Every reader is the persisted record.
    """
    from . import proxywatch
    if snapshot is not None:
        upstream, upstream_error, age, stale_s = _snapshot_seams(
            snapshot, stale_s)
    try:
        from . import seat as seatmod
        fam, ferr = (family, None) if family else seatmod.family_for(
            str(seat or ""), runtime, runtime_verified is True)
    except Exception as e:                  # noqa: BLE001 — a display never raises
        return _availability(UNKNOWN, None,
                             why=("this seat's family could not be resolved "
                                  "(%s: %s)" % (e.__class__.__name__, e)))
    if ferr or not fam:
        # NOT UNKNOWN. No proxy backs this seat, so no vendor wall can exist
        # for it — see the NO_VENDOR note above.
        return _availability(NO_VENDOR, None)
    if upstream_error:
        return _availability(UNKNOWN, fam, why=str(upstream_error))
    if upstream is None:
        upstream, err = proxywatch.upstream_snapshot()
        if err:
            return _availability(UNKNOWN, fam, why=err)
    elif not isinstance(upstream, dict):
        return _availability(UNKNOWN, fam,
                             why="the persisted upstream record is not a "
                                 "family map")
    else:
        bar = _availability_stale_s() if stale_s is None else int(stale_s)
        if age is not None and age > bar:
            return _availability(
                UNKNOWN, fam,
                why=("proxywatch last wrote %dm ago (bar %dm) — this is the "
                     "last thing it saw, not the state now"
                     % (int(age) // 60, int(bar) // 60)))
    rec, rec_err = proxywatch.upstream_record({"upstream": upstream}, fam)
    if rec_err:
        return _availability(UNKNOWN, fam, why=rec_err)
    # A MALFORMED LATCH IS NOT A MEASURED WALL. proxywatch normalises a
    # non-boolean `dark` CONSERVATIVELY — it keeps delivery paused and reports
    # `dark_invalid` — so `dark is True` is also true for a record whose ONLY
    # dark evidence is the field helm could not read ({"state": "UNKNOWN",
    # "dark": "false"} normalises to dark=True). UNAVAILABLE off that accuses a
    # vendor of refusing on an unreadable byte, and it is the value that steers
    # routing. The NAMED state is the surviving evidence: a named dark verdict
    # is still a measurement and still reads UNAVAILABLE, while anything else
    # reads UNKNOWN and quotes the malformed field. THE HOLD IS UNTOUCHED —
    # `beacon_paused` reads proxywatch's own latch and keeps pausing delivery;
    # what this refuses to do is publish the hold as a measurement. The dark
    # vocabulary stays proxywatch's (one sheet), asked rather than copied.
    if rec.get("dark_invalid") and not proxywatch._named_upstream_dark(
            rec.get("state")):
        return _availability(
            UNKNOWN, fam,
            why=("proxywatch recorded %s for family %s and a dark latch that "
                 "is not boolean — delivery stays held on that record, and an "
                 "unreadable field is not a measured wall"
                 % (rec.get("state") or UNKNOWN, fam)))
    if rec.get("dark") is True:
        return _availability(
            UNAVAILABLE, fam,
            reason=_availability_reason(upstream, fam, str(seat), rec),
            since=rec.get("since"))
    if rec.get("state") == "HEALTHY":
        return _availability(AVAILABLE, fam, since=rec.get("since"))
    return _availability(UNKNOWN, fam,
                         why=("proxywatch recorded %s for family %s and no "
                              "dark latch — nothing has measured this vendor "
                              "either way"
                              % (rec.get("state") or UNKNOWN, fam)))


def availability_map(names, upstream=None, upstream_error=None, age=None,
                     stale_s=None, runtimes=None, snapshot=None):
    """{seat: availability record} — ONE persisted read for the whole fleet.

    The reason every surface below can afford this on a row loop: N seats cost
    one small file read, never N. `snapshot` is the render's one acquisition.

    `runtimes` is {seat: (runtime, verified)}. SUPPLIED NOTHING, THE ROSTER IS
    ASKED — a caller that holds no launch metadata is not a caller for whom
    name parsing is correct, it is a caller that has not looked (see
    roster_runtimes). Pass {} to mean "parse the names", which is what a
    fixture with no roster wants.
    """
    if snapshot is not None:
        upstream, upstream_error, age, stale_s = _snapshot_seams(
            snapshot, stale_s)
    if runtimes is None:
        runtimes = roster_runtimes()
    if upstream is None and not upstream_error:
        from . import proxywatch
        upstream, err = proxywatch.upstream_snapshot()
        if err:
            upstream, upstream_error = None, err
    out = {}
    for n in (names or ()):
        rt, ver = (runtimes or {}).get(n) or (None, False)
        try:
            out[n] = availability(n, upstream=upstream,
                                  upstream_error=upstream_error, age=age,
                                  stale_s=stale_s, runtime=rt,
                                  runtime_verified=ver)
        except Exception as e:              # noqa: BLE001 — one row never takes
            out[n] = _availability(         # the table down
                UNKNOWN, None,
                why=("the availability join failed for this seat (%s: %s)"
                     % (e.__class__.__name__, e)))
    return out



# ── THE CONSUMER-SIDE HELPERS, HERE AND NOT IN EACH SURFACE ─────────────────
# Every one of these started life in a consumer, and each copy carried the same
# two-line comparison against the vocabulary above. That is a second copy of a
# vocabulary its owner already holds, and it drifts the first time the
# vocabulary gains an entry — so the WORD never leaves this module and the
# consumers ask for a rendering or a boolean instead. It is also what keeps the
# `helm/seats_*` split budget draining rather than filling: the roster surface
# carries the CALL, not the rules.
def availability_walled(rec):
    """Is this record the MEASURED wall — the one value that may steer routing?

    UNKNOWN is deliberately False here: it is helm saying it could not read the
    record, and a guard refusing on that would brick every box where proxywatch
    has never run (the module docstring's refuse-on-contradiction law)."""
    return (rec or {}).get("state") == UNAVAILABLE


def availability_mark(rec):
    """The row mark for one record: a dark glyph for the measured wall, a `?` for
    an unreadable record, and NOTHING for AVAILABLE or a seat with no vendor.

    AVAILABLE PRINTS NOTHING ON A SCAN LINE, and that is the attention budget:
    a mark every healthy row carries is a mark nobody reads, which is how the
    one row that matters arrives to an audience that stopped looking.

    NO LEADING WHITESPACE, AND THE CALLER SUPPLIES THE SEPARATOR. This string
    is PUBLISHED on roster rows, and the roster's publish boundary strips every
    string it launders — so a mark that carried its own indent arrived with the
    indent gone and butted straight onto the column before it. A rendering must
    not depend on whitespace surviving a launder it does not control."""
    rec = rec or {}
    state, text = rec.get("state"), rec.get("text")
    if not text:
        return ""
    if state == UNAVAILABLE:
        return "⚫ %s" % text
    return "? %s" % text if state == UNKNOWN else ""


def availability_cells(rec):
    """The published keys for one row, or {} when the question does not apply.

    A seat with no proxy family publishes NOTHING — the same silence
    web_roster's `_annotate_upstream` chose, because a badge every native-claude
    row carries is a badge nobody reads.

    THE VERDICT AND THE RENDERED MARK ARE PUBLISHED, NOT LEFT TO EACH READER.
    `availability` is the word, but a consumer that has to COMPARE it holds a
    copy of the vocabulary above and drifts the first time that vocabulary
    gains an entry — so the boolean a filter needs and the string a scan line
    needs are computed HERE, once, by the module that owns both. It is the same
    reason the roster already publishes `dot` beside `presence`."""
    rec = rec or {}
    if not rec.get("text"):
        return {}
    return {"availability": rec.get("state"),
            "availability_text": rec.get("text"),
            "availability_family": rec.get("family"),
            "availability_walled": availability_walled(rec),
            "availability_mark": availability_mark(rec)}


def availability_for_roster(roster_rows, snapshot=None):
    """{seat: record} for a whole roster read.

    The verified launch metadata rides along because a display name may differ
    from the family that owns the wall (a `pi-codex` seat is billed to `codex`),
    and only the roster row can say so.

    `snapshot` is the render's ONE acquisition, handed down by the surface that
    composes several producers onto one row.

    A READ THAT RAISES PROPAGATES. This returned {} for it, and {} is a roster
    of seats with no vendor: every row's word vanished. The caller's fail-open
    (seats_report._avail) makes it UNKNOWN on every row and leaves a breadcrumb.
    """
    return availability_map(
        sorted(roster_rows), snapshot=snapshot,
        runtimes={name: (row.get("runtime"),
                         row.get("runtime_verified") is True)
                  for name, row in roster_rows.items()
                  if isinstance(row, dict)})

def seat_verdict(seat, **seams):
    """(verdict, reason, row) for ONE seat — the ROUTING question.

    Scoped TWICE: proxywatch measures only this seat, and the ledger is not
    folded at all (need_holding defaults False here, and the caller may pass
    True when it wants the count) — so a guard on the dispatch write path pays
    for the one recipient it asked about and adds NO ledger read. `row["can_take_work"] is False` is the only value a guard may
    refuse on (see the module docstring on refusing measured contradictions
    rather than absences).
    """
    seams.setdefault("need_holding", False)
    rows = join(seats=[seat], health_seats=[seat], **seams)
    row = rows.get(seat)
    return (row or {}).get("verdict", UNKNOWN), \
        (row or {}).get("reason", "no joined row exists for this seat"), row


def verdict(row):
    """(verdict, reason) — the ladder, plus the one NON-GATING note.

    An unreadable seat register cannot change the answer to "can this seat
    take work" (see `_seat_row`), but the operator still has to be told why
    the runtime column says UNKNOWN — a field that reads UNKNOWN with no
    stated cause is the same silence one column over. So it rides the reason
    and never the verdict word."""
    state, why = _verdict_core(row)
    note = (row or {}).get("runtime_unreadable")
    if note:
        why = (why + "; " if why else "") + "note: " + str(note)
    return state, why


def _verdict_core(row):
    """(verdict, reason) — the ladder. See the module docstring for why the
    precedence is measured-refusal, then UNKNOWN, then measured impairment."""
    if row is None:
        return UNKNOWN, "no joined row exists for this seat"
    unknown = row.get("unknown") or {}
    age = row.get("semantic_age_s")
    turn = row.get("turn_state")
    # THE LADDER OWNS STALENESS. THIS FUNCTION MUST NOT RE-DERIVE IT.
    #
    # Measured on the live fleet the first time this rendered: codex came out
    # "DEGRADED turn=ok last=0h56m - no completed turn in 0h56m" — a line that
    # contradicts itself in eight characters. `turn_state` subtracts the HOST
    # SUSPEND GAP from the age before comparing it to HANG_S (a suspend adds
    # the same delta to every seat at once and would invent a fleet-wide
    # hang), while the row carries the RAW WALL age. Comparing that raw age to
    # HANG_S here was a SECOND, DIFFERENT staleness rule wearing the first
    # one's name.
    #
    # `ok` is the ladder saying a turn completed inside the window; `off` is
    # the ladder declining to ask because nothing is running. Every other
    # state IS the stale branch, by construction of the ladder itself.
    stale = turn is not None and turn not in ("ok", "off")
    measured = []
    if stale and age is not None:
        measured.append("no completed turn in %s" % _fmt_age(age))

    # 1 — a MEASURED refusal outranks an unreadable sibling field.
    if row.get("pane") is False:
        # THE REPAIR MUST MATCH THE SEAT CLASS. `helm seat spawn` mints a
        # PROXY seat; prescribing it for a native claude pane would hand the
        # operator an instrument that cannot fix their case — the bug class
        # where a confident recommendation is worse than none.
        repair = "`helm seat resume %s` relaunches its pane" \
            if row.get("scope") == "pane-only" else \
            "`helm seat spawn %s` respawns it"
        # THE WALL STACKED UNDER A GONE PANE RIDES THIS SENTENCE. The pane rung
        # keeps its precedence — it is the right ROUTING answer and reordering
        # it would change what this guard refuses — but the reason a reader
        # quotes must not stop at the pane when the vendor is ALSO dark. A
        # seat in that shape rendered "pane GONE — ... `helm seat spawn <seat>`
        # respawns it" while its own upstream column named a wall, so the one
        # instrument the line prescribed would respawn a pane into a dark
        # vendor and the operator would learn nothing. The wall is named now,
        # and the prescription says what it will and will not fix.
        wall = ([("this seat is ALSO %s — a respawn brings the pane back and "
                  "does NOT clear it: it clears on its own when the next "
                  "proxywatch pass measures the family green"
                  % availability_text({"state": UNAVAILABLE,
                                       "family": row.get("family"),
                                       "reason": row.get("upstream"),
                                       "since": row.get("upstream_since")}))]
                if row.get("upstream_dark") else [])
        return UNUSABLE, "; ".join(
            ["pane GONE — no live process holds this seat (%s)"
             % (repair % row.get("seat"))] + wall + measured)
    if row.get("reachable") is False:
        # AFTER THE PANE RUNG ON PURPOSE. A seat whose pane is GONE is also
        # unreachable, and `helm seat spawn` re-arms the beacon as part of
        # bringing the pane back — so that rung's prescription already fixes
        # this one, and naming the beacon there would hand the operator the
        # narrower repair. What is left here is the case the pane rung cannot
        # see and no other rung can either: a seat that is LIVE, healthy and
        # turning, with no way for helm to wake it.
        #
        # AND ITS PANE IS LIVE, WHICH THE SENTENCE MUST SAY (task/2948). Read
        # alone, "UNUSABLE ... no live beacon" told the owner a seat whose
        # vendor answers and whose process runs could not be used, when one
        # keystroke into that live pane wakes it — measured on
        # ds4pro, gemini and openrouter: DEAF by the census, pane live, and
        # their proxies' canaries answering 200 in the same minute. The
        # verdict stays UNUSABLE, because it is true of HELM'S OWN wake path
        # and it is what the dispatch door routes on; what changes is that the
        # line names the outside wake a reader can use now.
        live = ("; its pane is LIVE, so a keystroke wakes it: with a row "
                "waiting for it, `helm seat resume-turn --nudge --seat %s` "
                "types the wake into that pane, as `orca terminal send` would"
                % row.get("seat")) \
            if row.get("pane") is True else ""
        return UNUSABLE, "; ".join(
            ["%s (`helm chat wait --seat %s --follow` re-arms it)%s"
             % (row.get("reachable_why") or "helm cannot wake this seat",
                row.get("seat"), live)] + measured)
    if row.get("upstream_dark"):
        return UNUSABLE, "; ".join(
            [_dark_reason(row.get("upstream"), row.get("upstream_since"))]
            + measured)
    if turn in _TURN_UNUSABLE:
        return UNUSABLE, "; ".join(
            ["turn=%s: %s" % (turn, row.get("turn_evidence")
                              or "proxywatch recorded no evidence")] + measured)

    # 2 — anything unreadable that could have changed the answer.
    if turn in _TURN_UNKNOWN:
        return UNKNOWN, "; ".join(
            ["turn=%s: %s" % (turn, row.get("turn_evidence")
                              or "proxywatch recorded no evidence")] + measured)
    if unknown:
        return UNKNOWN, "; ".join(measured + [
            "%s UNREADABLE (%s)" % (field, why)
            for field, why in sorted(unknown.items())])

    # 3 — measured, turning or turnable, but impaired.
    if turn in _TURN_IMPAIRED:
        measured.append("turn=%s: %s" % (turn, row.get("turn_evidence")
                                         or "proxywatch recorded no evidence"))
    if row.get("registered") is False:
        measured.append("no seat-register row — this seat has never joined, "
                        "so helm cannot address work to it")
    if measured:
        return DEGRADED, "; ".join(measured)
    return USABLE, ""


def line(seat, rows, indent="  "):
    """The rendered row — one line per seat, answering the owner's question.

      codex    UNUSABLE  turn=starved last=42h13m pane=live upstream=HEALTHY
               holding=13 runtime=verified - <reason>

    Every field prints. A field that could not be read prints UNKNOWN rather
    than being omitted, because an omission on a scan line reads as health.
    """
    row = (rows or {}).get(seat)
    # THE RENDER IS A CONSUMER, NOT THE AUTHORITY. It reads the verdict the
    # join already stamped rather than deriving a second one — two derivations
    # is how the display and the router come to disagree about one seat, which
    # is the class this whole module exists to close. `verdict(row)` is the
    # fallback for a row the join never produced (an empty map: what `_status`
    # falls back to when the join itself raises), and it answers UNKNOWN.
    state = (row or {}).get("verdict") or verdict(row)[0]
    why = (row or {}).get("reason") if row else verdict(row)[1]
    row = row or {}
    age = row.get("semantic_age_s")
    holding = row.get("holding")
    # n/a is a MEASURED STRUCTURAL FACT — this seat has no proxy, so it has no
    # turn age and no provider wall to report. It is not UNKNOWN (helm looked
    # and could not read) and it is emphatically not blank, which would read as
    # health. The legend defines it.
    na = row.get("scope") == "pane-only"
    return "%s%-8s %-8s turn=%s last=%s pane=%s upstream=%s holding=%s " \
           "runtime=%s%s" % (
               indent, seat, state,
               "n/a" if na else (row.get("turn_state") or UNKNOWN),
               "n/a" if na else
               (_fmt_age(age) if age is not None else UNKNOWN),
               {True: "live", False: "GONE"}.get(row.get("pane"), UNKNOWN),
               "n/a" if na else (row.get("upstream") or UNKNOWN),
               "-" if row.get("holding_scope") == "not-asked"
               else UNKNOWN if holding is None else holding,
               _runtime_text(row),
               (" - " + why) if why else "")


def _runtime_text(row):
    """verified | UNVERIFIED | UNREGISTERED | UNKNOWN — never blank, and never
    a claim helm did not measure.

    The label prints on every row precisely because it does NOT gate the
    verdict: a reader who wants to know what answers behind the proxy has to
    see it without the verdict shouting about it.

    UNVERIFIED IS A MEASUREMENT, NOT A FALLBACK, and this function shipped the
    other way for an hour. The first cut ended `... else "UNVERIFIED"`, so the
    join-raised fallback row — where NOTHING was read — rendered
    `turn=UNKNOWN last=UNKNOWN pane=UNKNOWN upstream=UNKNOWN holding=UNKNOWN
    runtime=UNVERIFIED`: five honest unknowns and one confident claim, in the
    module whose entire subject is that exact failure. UNVERIFIED now requires
    a register row that was READ and is not verified; anything else is UNKNOWN.
    """
    if row.get("runtime_unreadable"):
        return UNKNOWN
    if row.get("registered") is False:
        return "UNREGISTERED"
    if row.get("runtime_verified") is True:
        return "verified"
    return "UNVERIFIED" if row.get("registered") is True else UNKNOWN


LEGEND = ("  usability: last=age of the last COMPLETED semantic turn (stale "
          "bar %dm) · holding=OWED open rows addressed to the seat, one "
          "ledger for dispatches and land requests, supersession folded · "
          "runtime is shown, never gating · n/a=no proxy backs this seat, so "
          "that field cannot exist · a proxy row carries TWO model fields, "
          "declared then minted: declared=the catalog DEFAULT for that seat "
          "(seat_catalog instance_models, its family's model where it "
          "declares none), which need not be its family's default and is what "
          "the next mint derives from; minted=the model that seat's own "
          "launch.sh on disk runs, so an explicit `seat launch --model` shows "
          "the two disagreeing · minted is what the NEXT spawn of that pane "
          "starts with, NOT a reading of the pane running now: launch.sh is "
          "re-minted on every add, launch and resume, so a pane started "
          "before the last mint can be running another model · there is no "
          "launched column because no shipped record names the model a "
          "running pane attested — the roster runtime row a pane self-writes "
          "carries its harness, family and backend and no model — so helm "
          "says nothing about it rather than guessing from the disk · "
          "minted=unminted means that seat has NO launch.sh at all (never "
          "launched, or orca-adopted), which is a measured absence and not a "
          "failed read; minted=UNKNOWN means helm could not answer from that "
          "path — the script IS there and names no model, or the read itself "
          "failed and the error class is printed beside it (a dangling "
          "symlink, an unreadable parent directory) · "
          "UNKNOWN=helm could not read that input, "
          "never a default")


def legend():
    return LEGEND % (_stale_s() // 60)
