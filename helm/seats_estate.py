"""Estate resolution: WHICH estate this process speaks for.

Split out of :mod:`helm.seats_roster` because it is a distinct
question with its own vocabulary, and because carrying it there put
that module over its line budget — the budget is the split working,
not an obstacle to it.

THE QUESTION IS ABOUT A FILE. A process may keep the machine-global
seat authority current exactly when the roster it writes IS the
production roster. Everything else — which variable was set, which
root is redirected, which spelling won — is evidence about that
question, and the predicate this replaced kept reconstructing the
answer from the evidence instead of asking the question.
"""
import os

from . import chat, home


HOST = "HOST"
ISOLATED = "ISOLATED"
TARGETED = "TARGETED"
UNKNOWN = "UNKNOWN"


class Estate:
    """Which estate this process speaks for — resolved ONCE and STATED.

    THE QUESTION IS ABOUT A FILE, NOT ABOUT THE ENVIRONMENT. A process may
    keep the machine-global seat authority current exactly when the roster it
    writes IS the production roster. Everything else — which variable was
    set, which root is redirected, which spelling won — is evidence about
    that question, and the predicate this replaces tried to reconstruct the
    answer from the evidence instead of asking the question. Every cured case
    taught the next one: comparing the resolved bus against a LITERAL
    /dev/shm path made every tmpfs-less host silently stop projecting; an
    explicit production chat dir beside an isolated home wrote the live
    roster and skipped projection anyway; and reading HELM_HOME and MELD_HOME
    directly walked past home.env's own precedence between the two spellings.
    Comparing the two ROSTER PATHS answers all three at once, because the
    roster path is the thing every one of them was a proxy for.

    projection is HOST (this process writes the production roster, so the
    authority is its to keep current), TARGETED (an explicit authority file
    was named — sandbox or not, the operator chose it), ISOLATED (a redirected
    estate, which must never touch the operator's authority: a suite once
    persisted synthetic keys into the real seat-names.txt this way), or
    UNKNOWN with a reason, which projects NOTHING — a guess here writes fleet
    identity into a file nobody asked for.
    """

    def __init__(self, roster_path, authority_path, projection, why=None,
                 surface_origin=None):
        self.roster_path = roster_path
        self.authority_path = authority_path
        self.projection = projection
        self.why = why
        self.surface_origin = surface_origin

    def projects(self):
        """May this process write the seat-name authority?"""
        return self.projection in (HOST, TARGETED)


def estate():
    """Resolve the estate. Total: it reports UNKNOWN rather than raising."""
    from . import seatname_guard
    surface, origin = home.surface_origin("CHAT_DIR", "helm-chat",
                                          chat.DEFAULT_DIR)
    # NOT NAMED `roster`: that is the accessor's name, and the launder census
    # reads a bare binding of it as an accessor escaping its call site. A
    # local shadow costs a real guard its measurement.
    mine = os.path.join(surface, ".roster.json")
    authority, invalid, targeted = seatname_guard.authority_target()
    if invalid:
        return Estate(mine, None, UNKNOWN, invalid, origin)
    if targeted:
        # EXPLICITLY TARGETED, sandbox or not: the operator named the file, so
        # there is no ownership question left to infer. Path and provenance
        # come from one resolution so an empty preferred spelling cannot make
        # this classifier and the writer choose different authority files.
        return Estate(mine, authority, TARGETED, None, origin)
    production = os.path.join(home.default_surface(chat.DEFAULT_DIR),
                              ".roster.json")
    if os.path.realpath(mine) == os.path.realpath(production):
        return Estate(mine, authority, HOST, None, origin)
    return Estate(mine, authority, ISOLATED, None, origin)


def _owns_the_global_authority():
    """May this process write the machine-global authority? A named BOOLEAN
    VIEW of :func:`estate`, never a second derivation of it — one owner
    answering one question, with a short name for the callers that only want
    the yes/no."""
    return estate().projects()


def _project_seat_authority(r):
    """True when it is SAFE TO PUBLISH this roster; False when it is not.

    IT USED TO RETURN NOTHING, and "never fails a roster write" was read as
    "the caller need not care". It must care in exactly one case. Projection
    keeps the rung's authority current, so an ADMITTING write that could
    neither arm the identity NOR record a durable refusal publishes a seat
    nothing will ever refuse — and a later public-bound commit carrying that
    identity passes. The marker cannot cover that case by itself because IT
    SHARES A FAULT DOMAIN WITH THE THING IT REPORTS: an unwritable authority
    directory defeats the lock and the marker identically.

    So False means UNGUARDED — armed nothing, recorded nothing — and only the
    doors that ADMIT an identity need honour it. Everything else (a sandbox
    estate, a converged authority, a durable refusal that WAS written) is
    True, because in each of those the rung will do the right thing on its
    own.
    """
    e = estate()
    if not e.projects():
        return True          # nothing here to guard; publishing is unaffected
    try:
        from . import seatname_guard
        return seatname_guard.refresh_authority(
            r, path=e.authority_path) is not None
    except (OSError, ValueError) as exc:                      # declared class
        try:
            from . import seatname_guard as _sg
            _path, _invalid = _sg.authority_path()
            # NOTHING TO MARK IF NOTHING RESOLVES. A stale marker lives BESIDE
            # the authority, so an unresolvable override has no place to put
            # one — and writing it beside a cwd-relative guess would leave the
            # marker somewhere no reader will ever look. The rung already
            # fails closed on an unresolvable path, which is the louder signal.
            if _path is None:
                return True
            return _sg._mark_stale(_path,
                                   "roster projection failed (%s)" % exc)
        except Exception:                                     # noqa: BLE001
            return False
