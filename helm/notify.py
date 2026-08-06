#!/usr/bin/env python3
"""helm notify — THE one path off this box to the owner's own phone.

WHY THIS MODULE EXISTS AT ALL, and it is not tidiness. Every alarm helm has
ever raised is delivered INTO the fleet: a chat room read by seats. On
2026-08-03 at 03:44 the orca PTY daemon died and took all seven panes with it,
and for four hours nothing said so — because every reader of every alarm
surface was one of the seven dead seats, and the owner was asleep. The rule
that outage wrote:

    AN ALARM ABOUT THE FLEET BEING UNREACHABLE MUST NOT DEPEND ON A FLEET
    MEMBER BEING REACHABLE.

The escape hatch already existed and was already VERIFIED — HELM_NTFY_TOPIC,
the owner's private push topic, receipt confirmed on his phone 2026-07-23. It
had two callers (proxywatch's family dark/recovered edges, the store's
graduation push) and each had written its OWN copy of the same eight lines.
This module is those eight lines, once, so a third caller composes instead of
minting a rival: reachability alarms now ride the SAME channel the owner
already reads, and there is exactly one place where the endpoint, the timeout,
and the fail-open law live.

CONFIG IS A KEY NAME, NEVER A VALUE. `HELM_NTFY_TOPIC` (legacy `MELD_` accepted
by `home.env`) holds a full URL, else a bare topic resolved against
https://ntfy.sh/. It is a capability — anyone holding it can push to the
owner's phone — so nothing here returns it to a caller, prints it, or puts it
in an error string: `configured()` answers the only question a caller may ask.
UNSET is a deliberate OPT-OUT, not a failure, and it makes NO network call.

FAIL-OPEN, ALWAYS. A notifier must never break the verb it reports on. Every
error (DNS, timeout, a down notifier) is one journal line and a False return —
False means "not delivered", so an outbox caller keeps the edge armed for its
next pass (at-least-once). A caller that wants at-most-once simply ignores it.
"""
from . import home

TIMEOUT_S = 3      # per-socket-op, not total wall clock
DEFAULT_TITLE = "helm"


def _endpoint():
    """The resolved push URL, or None when the owner has opted out.

    PRIVATE BY DESIGN: the topic is a push capability for the owner's phone.
    Callers get `configured()`; nothing hands them the value to print into a
    log, an error string, or a chat room."""
    topic = (home.env("NTFY_TOPIC") or "").strip()
    if not topic:
        return None
    return topic if topic.startswith(("http://", "https://")) \
        else "https://ntfy.sh/" + topic


def configured():
    """Is there a phone at the end of this channel? A caller raising an alarm
    that ONLY the fleet can read should say so on its own surface when this is
    False — a silently opted-out push is exactly the half-working alarm that
    reads as delivered and wakes nobody."""
    return _endpoint() is not None


def owner_push(body, title=DEFAULT_TITLE, receipt=None):
    """Push ONE message to the owner's phone -> delivered.

    True  — delivered, or deliberately opted out (no topic: nothing to retry).
    False — the POST failed; the caller's outbox must keep this edge armed.

    `receipt` is an optional (verb, target) pair journaled via pk.event on
    failure, so each caller keeps its own receipt vocabulary and a miss is
    never silent. Batch before you call: one push per pass, not one per row —
    a phone that buzzes once per seat during a seven-seat outage is a phone
    that gets silenced before the next one."""
    url = _endpoint()
    if url is None:
        return True                          # deliberately opted out
    try:
        import urllib.request
        req = urllib.request.Request(
            url, data=str(body).encode("utf-8"), method="POST",
            headers={"Title": str(title or DEFAULT_TITLE)})
        with urllib.request.urlopen(req, timeout=TIMEOUT_S):
            pass
        return True
    except Exception as ex:                  # noqa: BLE001 — never blocks a verb
        if receipt:
            try:
                from . import pk
                pk.event(receipt[0], receipt[1],
                         "owner push failed: " + str(ex)[:120])
            except Exception:                # noqa: BLE001 — best-effort receipt
                pass
        return False
