#!/usr/bin/env python3
"""helm harness — the metaharness ADAPTER seam.

CANON: helm is metaharness-AGNOSTIC. It never assumes orca (or any other
terminal workspace manager); each metaharness gets a lightweight adapter here,
detect() picks whichever is actually installed, and every helm verb that
drives panes (seat resume, future watchdog legs) speaks only the uniform
interface below. When NO metaharness is installed, pane ops degrade to a
printed paste line and doctor recommends orca as an OPTIONAL companion
(herdr equally supported).

The uniform pane-op interface (every adapter):
    spawn(command, title=None, cwd=None) -> handle   a new pane running command
    list()  -> [{"handle", "title", "status"}, ...]  live panes
    read(handle, limit=3000, timeout=60) -> str      bounded tail text
    send(handle, text, enter=True)                   keystrokes into the pane
    submit(handle, text) -> (state, detail)          text, THEN Enter, VERIFIED
    stop(handle)                                     close the pane

`send` is the TRANSPORT verb and its success means one thing only: the
metaharness accepted the bytes. `submit` is the TURN verb — see the law below
it — and every caller that wants a seat to take a turn must use it.

Every CLI adapter also inherits:
    spawn_failure_absent(error) -> bool   proof its create process never started

OPTIONAL per adapter:
    ensure_home_worktree(seat, repo_root, base="main") -> path

Adapters shell out to each metaharness's OWN public CLI (subprocess + json,
list-argv only — never a shell string, so arbitrary titles/commands are safe
without quoting games):

  orca   `orca terminal create/list/read/send/close --json`
         (flags live-verified against the installed CLI 2026-07-21).
  herdr  `herdr agent start` + `herdr pane list/read/run/send-text/close`
         (API discovered from the installed CLI's own help + `herdr api
         schema` protocol 16, 2026-07-21; replies are JSON envelopes
         {"id", "result": {...}} / {"id", "error": {code, message}}).

TOKEN LAW: nothing routed through this seam may carry a secret. Callers pass
PATHS (a seat's launch.sh), never expanded launch lines — a pane title, spawn
command, or error echo must never contain ANTHROPIC_AUTH_TOKEN et al. Error
text is truncated and comes only from the metaharness CLI's own stderr.
"""
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import time
import uuid

# The one-line optional-companion pitch (doctor + seat resume share it).
RECOMMENDATION = ("no metaharness detected — helm is metaharness-agnostic and "
                  "runs fine without one, but pane ops (`helm seat resume`) "
                  "need one; optional companion: orca (recommended), herdr "
                  "also supported")


class HarnessError(RuntimeError):
    """A metaharness failure plus any proof the request never started."""

    def __init__(self, message, code=None, request_absent=False,
                 timed_out=False):
        super().__init__(message)
        self.code = code
        self.request_absent = request_absent
        # The client gave up waiting. The request may still have landed.
        self.timed_out = timed_out


# The CLI failed BEFORE it could speak its own protocol — a launcher that could
# not start, a missing/half-extracted bundle, a wrapper refusing outright. It is
# a fact about the INSTRUMENT, never about the estate the instrument reports on,
# and the two must not share a value: a caller that renders this as "no panes"
# states a measurement it never took. Keyed on the STRUCTURAL fact (rc != 0 with
# a reply that is not the protocol's JSON), never on a wrapper's wording, which
# helm does not own and cannot keep in sync.
CLI_PROTOCOL_UNREACHED = "cli-protocol-unreached"


def _error_code(body):
    try:
        doc = json.loads(body)
    except (TypeError, ValueError):
        return CLI_PROTOCOL_UNREACHED
    if not isinstance(doc, dict):
        return CLI_PROTOCOL_UNREACHED
    err = doc.get("error")
    return doc.get("code") or (err.get("code") if isinstance(err, dict) else None)


# Every orca DAEMON RPC is bounded by this. A hung daemon must not hang a helm
# verb: the socket gets one short window, then the caller degrades to helm's own
# behaviour. Callers needing a different bound pass `timeout=`; there is
# deliberately no env override, because nothing has needed one.
RPC_TIMEOUT_S = 5


# ---------------------------------------------------------------------------
# SUBMIT — "the metaharness took the bytes" is NOT "the seat took a turn"
# ---------------------------------------------------------------------------
# THE BUG (owner-reported, plural and recurring: "these keep landing and not
# hitting enter"). A seat's own resume directive was TYPED INTO ITS COMPOSER
# AND NEVER SUBMITTED. The seat then sits with its next instruction on screen
# doing nothing — and reads IDLE-AND-HEALTHY to seat_liveness, to proxywatch
# and to the beacons, because all three ask "is this pane alive?" and the pane
# is perfectly alive. It is not a prompt nobody answered; it is a message
# nobody sent.
#
# `send(..., enter=True)` returned success on ORCA ACCEPTED THE BYTES, which is
# a DIFFERENT CLAIM from THE SEAT GOT A TURN, and that gap is the whole defect.
# Three answers are needed, not two, and the third is the one that was missing:
#
#   DELIVERED      the composer no longer holds our text — it went in
#   NOT_DELIVERED  the composer STILL holds our text — it did not
#   UNKNOWN        the pane could not be read — WE DO NOT KNOW
#
# UNKNOWN MUST NEVER COLLAPSE INTO DELIVERED. Measured on this fleet
# 2026-08-05: of 26 live panes, 12 answered `terminal read` with an empty tail
# and every cursor pinned at 0 while their seats were provably working (one had
# posted to chat minutes earlier). A verifier that reads those as "composer is
# empty, therefore it submitted" would mint a false success on nearly half the
# fleet — the exact failure this class already produces, with a receipt.
DELIVERED = "delivered"
NOT_DELIVERED = "not-delivered"
UNKNOWN = "unknown"
# THE SEND ITSELF FAILED AND THE FAILURE DOES NOT SAY WHETHER INPUT ARRIVED.
# `HarnessError.request_absent` is set only when the CLI could not be launched,
# which PROVES the request never left this box. Every other failure — a
# timeout, a non-zero rc, an unparseable reply — can follow input the pane
# already accepted, so collapsing them into NOT_DELIVERED publishes "no key
# reached the pane" about a keystroke that may well have landed.
UNCERTAIN = "uncertain"


#: The one pane state whose ABSENCE OF A COMPOSER is a positive fact rather
#: than a failed read: a vendor limit dialog owns the screen and offers
#: numbered choices, so there is no composer to protect and none to read back.
MODAL_STATE = "BLOCKED_ON_VENDOR_PROMPT"

#: THE ESCAPE TYPES NOTHING IN THIS LAND, AND THIS IS THE LAND DECISION RATHER
#: THAN A TUNABLE. A pane tail is unframed scrollback: it can carry an ENDED
#: option run above a human's half-typed draft, and no scanner over that text
#: can tell an offer that owns current input from one that has already been
#: answered. An ended buy/switch/no run followed by "half a sentence the human
#: was writing" classifies as a live dialog and the chooser returns its digit,
#: so a build that typed it would type into the draft. The tail admits no
#: bounded set of such shapes, which is what says the question is wrong rather
#: than the answers.
#:
#: So the actuator STOPS AT THE DOOR: the dialog is detected, named and
#: reported, and no key is sent. What re-enables it is a TYPED EVENT from the
#: producer — a structured statement that these options are awaiting input now
#: — not a better reading of the same bytes. That is task/2386.
#:
#: The legs below this check are kept REACHABLE rather than deleted because
#: 2386 turns this off and needs them proven: the arms that exercise them lift
#: this flag explicitly and say so.
ESCAPE_TYPES_NOTHING = True


class _PreReadRefusal(str):
    """Local detail plus retry/export policy for a pre-actuation refusal."""

    def __new__(cls, detail, retryable, alert_detail=None):
        refusal = super().__new__(cls, detail)
        refusal.retryable = retryable
        refusal.alert_detail = alert_detail or detail
        # WHY there was no composer, when the pane could be classified at all.
        # "no composer" has two causes that must never share a value: a pane
        # this call could not read, and a pane that HAS no composer because a
        # modal owns the screen. The first is unknown and refuses; the second
        # is a positive identification.
        return refusal


#: How many times ONE act door prepares again after its final capture finds an
#: input moved, a composer that no longer reads as proven, or a capture that
#: overran its deadline, beyond the first preparation. The obligation is kept
#: when this is exhausted; nothing is typed and no Enter is spent.
ADMISSION_RESTARTS = 2

#: THE DECLARED ACT WINDOW, in seconds of MONOTONIC elapsed time, from the
#: start of an act door's final capture to the START of its send. The final
#: capture re-reads every certified dependency of the authorization and then
#: the composer; each read runs on a worker that is abandoned at the deadline,
#: and the composer read's own CLI timeout is the time remaining. A capture
#: that has not returned by the deadline, or a window that closes before the
#: send starts, restarts the round and then refuses: nothing is acted on and no
#: lock is held, because the door takes none. Elapsed and not CPU time,
#: because what exposes an authorization to change is the wall clock a stalled
#: read spends, not the cycles it burns. HELM_ACT_DEADLINE_S overrides.
#:
#: DERIVED FROM A MEASUREMENT, not chosen. The window holds every dependency
#: read (small tmpfs and disk reads, milliseconds) plus ONE composer read, and
#: the composer read is a Node CLI round trip. Measured on an 8-core host at
#: load average 4.8, 40 sequential `orca terminal read`
#: round trips (a handle that resolves to no terminal, so no pane was read):
#: min 0.52s, p50 0.61s, p90 0.71s, max 0.80s. 2.0s is 2.5 times that maximum.
#: A host where the read is slower refuses and says how long the capture ran.
ACT_DEADLINE_S = 2.0

#: THE DECLARED SEND BOUND, in seconds: the client-side timeout of the one CLI
#: subprocess that carries an act's keystroke. It is a different bound from
#: the act window above. The act window is a CAPTURE ADMISSION window that ends
#: when the act callback starts; this timeout starts later, inside that
#: callback, when the subprocess is launched. Neither of them, nor their sum,
#: is a guaranteed deadline from the capture to the send's return or to the
#: keystroke's effect in the terminal: the Python work between the callback's
#: start and the launch, process creation and termination, and scheduling are
#: not bounded by the subprocess timeout. A send that does not return inside
#: its timeout is killed and accounted OBSOLETE-AUTHORIZATION with its landing
#: unknown; verification decides delivery, and a server that received the
#: keystroke before the client was killed still lands it. Every act's receipt
#: publishes the MEASURED capture-to-send-start and capture-to-send-return.
#: HELM_ACT_SEND_S overrides. 5s is SUBMIT_READ_TIMEOUT_S, the bound every
#: other CLI call in a turn already carries, and about six times the measured
#: round trip above.
ACT_SEND_S = 5.0

#: OUTCOMES OF THE ACCOUNTING AFTER AN ACT. An act whose dependencies read
#: back identical to its final capture is CLEAN; one whose dependencies moved,
#: were rewritten, or could not be re-read in time is OBSOLETE_AUTHORIZATION.
#: The keystroke is not undone -- nothing can undo a keystroke -- it is
#: accounted, and a repeat of it for the same attempt is refused.
ACT_CLEAN = "clean"
OBSOLETE_AUTHORIZATION = "obsolete-authorization"


def act_deadline_s():
    value = _env_float("HELM_ACT_DEADLINE_S", ACT_DEADLINE_S)
    return value if value > 0 else ACT_DEADLINE_S


def act_send_s():
    value = _env_float("HELM_ACT_SEND_S", ACT_SEND_S)
    return value if value > 0 else ACT_SEND_S


def _bounded(fn, timeout):
    """(True, fn()) when `fn` returned within `timeout` seconds, else
    (False, None). `fn` runs on a daemon worker that is ABANDONED at the
    deadline: its eventual answer is discarded, and it can hold nothing the
    door needs released, because no act door takes a lock. An exception is
    re-raised in the caller."""
    import threading
    box = {}

    def run():
        try:
            box["value"] = fn()
        except BaseException as exc:         # noqa: BLE001 — re-raised below
            box["error"] = exc
    worker = threading.Thread(target=run, name="helm-act-capture", daemon=True)
    worker.start()
    worker.join(max(0.0, timeout))
    if worker.is_alive():
        return False, None
    if "error" in box:
        raise box["error"]
    return True, box.get("value")


class Grant(object):
    """A PREPARED authorization for one act, and the only shape an act door
    accepts from a caller.

    `ok` and `why` are what the preparation decided; `kind` names a refusal's
    kind for the caller's own records ("paused", "drained", "unknown").

    `capture()` is the FINAL CAPTURE of the authorization: it re-reads every
    dependency the preparation certified and answers (True, "", facts) when
    they stand, (False, what moved, facts) when one moved, (None, why, facts)
    when one could not be read coherently -- and a capture that RAISES is that
    last case, never an exception out of the door. `facts` may carry
    `horizon`, the wall-clock instant past which the act is no longer
    eligible, and `versions`, what the authorization was read as, which the
    accounting after the act compares. `still()` is the same capture without
    the facts. A grant built with no capture cannot be validated, so it
    answers None and authorizes nothing: the door's safety must not depend on
    a caller remembering one.

    `account(receipt)`, when given, PERSISTS the accounting of an act that
    happened -- clean or obsolete-authorization -- and returns "" or why it
    could not. `abandon()`, when given, is told that the door this grant was
    prepared for acted on nothing, so an intent the preparation recorded can
    be withdrawn."""

    def __init__(self, ok, why="", kind="", still=None, account=None,
                 abandon=None):
        self.ok = bool(ok)
        self.why = why or ""
        self.kind = kind or ""
        self._still = still
        self._account = account
        self._abandon = abandon

    def capture(self):
        if self._still is None:
            return None, "this authorization carries no validator", {}
        try:
            got = self._still()
        except Exception as exc:             # noqa: BLE001 — UNKNOWN, refuses
            return None, "the capture raised %s: %s" % (
                exc.__class__.__name__, exc), {}
        facts = got[2] if len(got) > 2 and isinstance(got[2], dict) else {}
        return got[0], got[1], facts

    def still(self):
        return self.capture()[:2]

    def recheck(self, facts):
        """(outcome, what) -- the accounting read after an act: the same
        capture again, compared with the final capture it followed. What is
        compared is what the capture chose to record as `versions`: the
        authorization's own values, and the identities of files that belong to
        this authorization alone, so that a value changed and restored in
        between is still a change the act ran under."""
        valid, why, now = self.capture()
        if valid is not True:
            return OBSOLETE_AUTHORIZATION, (
                why or "the authorization could not be re-read after the act")
        before, after = facts.get("versions"), now.get("versions")
        if before != after:
            moved = sorted(k for k in set(before or {}) | set(after or {})
                           if (before or {}).get(k) != (after or {}).get(k))
            return OBSOLETE_AUTHORIZATION, (
                "rewritten between the final capture and the accounting "
                "read: %s" % (", ".join(moved) or "the dependency set"))
        return ACT_CLEAN, ""

    def account(self, receipt):
        if self._account is None:
            return ""
        return self._account(receipt) or ""

    def abandon(self):
        if self._abandon is not None:
            try:
                self._abandon()
            except Exception:                # noqa: BLE001 — nothing acted
                pass


def act_note(receipt):
    """The disclosure an act door publishes with an act it ran: the MEASURED
    residual window and how the act was accounted. "" for an act with no
    authorization behind it.

    The keystroke is inside the send call, so the window is published as its
    two measured edges: capture start to send start (admitted inside the act
    window) and capture start to send return, published beside the client
    send timeout it ran under. That timeout is not a deadline on the second
    edge (see ACT_SEND_S). Nothing measures the keystroke itself."""
    if not receipt:
        return ""
    accounted = ("authorization clean" if receipt.get("outcome") == ACT_CLEAN
                 else "act accounted OBSOLETE-AUTHORIZATION (%s)"
                 % (receipt.get("what") or "no detail"))
    if receipt.get("account_error"):
        accounted += "; the accounting record failed: %s" % (
            receipt["account_error"])
    return ("; [%s act window: final capture to send start %.4fs of a %.2fs "
            "deadline, final capture to send return %.4fs under a %.2fs "
            "client send timeout, send start to accounting read %.4fs; %s]"
            % (receipt.get("door"), receipt.get("capture_to_send_start_s", 0),
               receipt.get("deadline_s", 0),
               receipt.get("capture_to_send_return_s", 0),
               receipt.get("send_bound_s", 0),
               receipt.get("send_start_to_reconciliation_s", 0), accounted))


class DoorRefusal(str):
    """The detail of an act door that refused, carrying WHICH door and WHAT
    KIND of refusal, so a caller that records outcomes reads a field rather
    than sniffing prose. `kind` is the grant's own kind, or "unknown" for a
    validation that could not be taken, or "moved" for an exhausted restart
    budget."""

    def __new__(cls, detail, door, kind):
        refusal = super().__new__(cls, detail)
        refusal.door = door
        refusal.kind = kind
        return refusal


# The gap between typing the text and the BARE ENTER that submits it.
#
# MEASURED at the pty 2026-08-05, recording raw bytes in an orca-created pane:
# `orca terminal send --text X --enter` is ALREADY two writes — the text, then
# 0x0d exactly 500ms later — and there is NO bracketed-paste wrapper at any
# size (re-measured with a 2739-byte 20-line payload: one write, then the lone
# CR, +0.500s). So the popular "the --enter is absorbed into the paste" story
# is REFUTED at the transport: orca is not the layer that eats the Enter.
#
# The split below therefore does not exist to beat a transport race. It exists
# so the Enter is an ACT WITH ITS OWN RECEIPT — a second call helm can retry,
# and a seam it can READ ACROSS. Two CLI invocations also widen the real gap to
# whole seconds, which costs nothing and can only help whatever the consuming
# TUI's own ingest window turns out to be.
SUBMIT_SETTLE_S = 0.4
SUBMIT_VERIFY_READS = 3          # bounded repaint window, never unbounded
SUBMIT_VERIFY_INTERVAL_S = 0.8
SUBMIT_READ_LIMIT = 200          # lines; the composer is at the BOTTOM
SUBMIT_READ_TIMEOUT_S = 5        # each CLI read; count bounds alone are not time bounds


def _env_float(name, default):
    try:
        return float(os.environ[name])
    except (KeyError, TypeError, ValueError):
        return default


# A collapsed paste chip ("[Pasted text #1 +23 lines]") is UNSENT INPUT that
# does not render as the text we typed. Reading it as "not our text, therefore
# it submitted" is a false DELIVERED on exactly the big multi-line directives
# this bug bites hardest, so for the question "is our text STILL SITTING THERE"
# it counts as held.
#
# THAT DOES NOT MAKE IT SAFE TO SUBMIT, and conflating the two questions is the
# bug a review blocked (task/1909). "Nothing was sent" and "I know what is in
# there" are different claims: a chip renders identically whether it collapsed
# our directive or a human's paste arrived during the post-type window. So
# `composer_holds` still calls a bare chip held, and `observe_composer` calls it
# OPAQUE and refuses the Enter.
_PASTE_CHIP_OPEN = re.compile(r"^\[Pasted text\b")
_PASTE_CHIP_WHOLE = re.compile(r"^\[Pasted text\b[^\]]*\]")

CHIP_NONE = None
CHIP_INCOMPLETE = "incomplete"
CHIP_COMPLETE = "complete"


def read_paste_chip(body):
    """Parse a leading paste chip ONCE -> (kind, remainder). task/1909.

    ONE GRAMMAR, because two regexes answering slightly different questions is
    what produced the bug twice. `_PASTE_CHIP_OPEN` matches the opening words
    and answers yes/no; `_PASTE_CHIP_WHOLE` matches the token and lets a caller
    measure what FOLLOWS it. Callers that picked the first one and then asked a
    remainder question read " #1 +12 lines]" as an append and called a bare chip
    foreign. Callers that picked it and asked a yes/no question waved a chip
    WITH appended text through as ours. Both are the same defect: the chip was
    parsed at the point of use, differently each time.

    THE INCOMPLETE KIND IS NOT A DETAIL, it is a review's acceptance branch. A
    chip mid-render — "[Pasted text #1 +12 lines" with no closing bracket yet —
    matches the WHOLE-token pattern nowhere, so it fell through to the foreign
    branch and latched a human-edit accusation on a pane that was simply still
    painting. A half-drawn token is evidence of drawing, never of a person.
    """
    if not body or not _PASTE_CHIP_OPEN.match(body):
        return CHIP_NONE, body
    whole = _PASTE_CHIP_WHOLE.match(body)
    if not whole:
        return CHIP_INCOMPLETE, ""
    return CHIP_COMPLETE, body[whole.end():]


_COMPOSER_PLACEHOLDERS = ("press up to edit queued messages",)


def composer_is_placeholder(body):
    return (body or "").lower() in _COMPOSER_PLACEHOLDERS


HOLDS = "HOLDS"
REPAINTING = "REPAINTING"
PLACEHOLDER = "PLACEHOLDER"
OPAQUE = "OPAQUE"
FOREIGN = "FOREIGN"
UNREADABLE = "UNREADABLE"

#: The fold's precedence, and the ORDER IS THE DESIGN. FOREIGN latches above
#: everything (residual 1: a later exact read must not un-see an edit already
#: witnessed). HOLDS comes next, because a composer that settles with our text
#: is the answer the poll exists to wait for — a window of repaints followed by
#: HOLDS is a DELIVERED send, not an unsettled one. PLACEHOLDER then reports a
#: pane that already moved on. UNREADABLE outranks REPAINTING because "I could
#: not look" is a stronger claim than "I looked and it had not settled", and
#: REPAINTING is weakest: it is the undecidable middle and authorizes nothing.
#: I had HOLDS at the BOTTOM in the first draft, which made every late-settling
#: pane read as unsettled and turned a delivered send into a refusal. Two of my
#: own new arms and one pre-existing arm caught it on the first run.
#:
#: OPAQUE sits BELOW HOLDS and ABOVE everything else, and both halves are load
#: bearing. Below HOLDS: a later read whose body EQUALS our first line exactly
#: has no chip in it at all, so the collapsed thing resolved into our own text
#: and submitting is safe — ranking OPAQUE above HOLDS would refuse every send
#: that begins with a paste chip and settles. Above the rest: OPAQUE is
#: POSITIVE evidence that content is present and unidentifiable, which is a
#: stronger claim than "I could not look" or "it had not settled".
_STATE_RANK = {FOREIGN: 5, HOLDS: 4, OPAQUE: 3, PLACEHOLDER: 2,
               UNREADABLE: 1, REPAINTING: 0}


def observe_composer(tail, text):
    """ONE reading of a composer -> ONE state. task/1909.

    THE DEFECT THIS REPLACES was three booleans answering "what is in this
    composer" — `composer_holds`, `composer_exactly_holds` and
    `composer_shows_foreign_text` (now DELETED, task/1909 — it had no callers
    left once this classifier existed) — whose False meant three different
    worlds.
    Two rounds of review found two different bugs in the same seam, and
    the fourth residual ended the incremental approach outright: STABLE
    STRICT-PREFIX EVIDENCE CANNOT PROVE "not because anyone edited it", because
    a human deleting back to a prefix and a pane still painting our own text
    produce a byte-identical body. No predicate over one observation can answer
    that. So this does not answer it — it NAMES it, and hands the caller a
    state it can decide about.

    REPAINTING is that undecidable middle, and making it a state is the whole
    point: it authorizes nothing and accuses nobody. A window that ends still
    REPAINTING is UNSETTLED, never "a human edited it" — which is the exact
    false accusation that started this chain.

    PLACEHOLDER is explicit rather than inferred from absence, because the
    post-type placeholder is real text and reading it as foreign is how an
    idle pane gets blamed.

    FOREIGN requires POSITIVE evidence of somebody else's text, and includes an
    APPEND to ours: `composer_holds` calls want-is-a-prefix-of-body held, and an
    append is exactly the edit an actuator must never wave through.

    OPAQUE IS THE STATE A BARE PASTE CHIP GETS, and it replaces the HOLDS this
    returned in the previous round — a review's blocker, and it was right that
    it was worse than main. I had reasoned "a chip alone is OUR collapsed
    payload, so it is held text"; the parent predicate deliberately answered
    UNKNOWN there, and I converted a refusal into an authorization without
    noticing I had. A CHIP HIDES PAYLOAD IDENTITY. It renders identically
    whether it collapsed our directive or a human's paste dropped in during the
    post-type window, so reading it as HOLDS spends the Enter on text nothing
    ever identified. OPAQUE says exactly what is true — something is there and
    it is not identifiable — and authorizes nothing.

    UNREADABLE is "I could not look", never "there is nothing there" — the two
    must not share a value, or a blind read silently clears real evidence. And
    OPAQUE is a third thing again: "I looked, there IS content, and I cannot
    say whose." Collapsing it into either neighbour loses the distinction that
    makes the refusal explainable.
    """
    line = _prompt_line(tail)
    if line is None:
        return UNREADABLE
    body = composer_body(line)
    if composer_is_placeholder(body):
        return PLACEHOLDER
    if not body:
        return REPAINTING
    want = composer_first_line(text)
    if not want:
        # Nothing to compare against: we cannot call this ours, and we have no
        # standing to call it anyone else's either.
        return UNREADABLE
    if body == want:
        return HOLDS
    # ONE PARSE OF THE CHIP, and its three kinds are three different answers.
    # An INCOMPLETE chip is a token still being drawn: it authorizes nothing and
    # accuses nobody, so it is REPAINTING. A COMPLETE chip ALONE hides whose
    # payload it holds, so it is OPAQUE. A complete chip with anything after it
    # is an APPEND, which is the one reading that positively implicates a
    # person. Deciding these at the point of use, with whichever regex was
    # nearest, is what produced this bug in both directions.
    kind, rest = read_paste_chip(body)
    if kind is CHIP_INCOMPLETE:
        return REPAINTING
    if kind is CHIP_COMPLETE:
        return FOREIGN if rest.strip() else OPAQUE
    if want.startswith(body):
        return REPAINTING       # a prefix: painting, or a deletion — undecidable
    return FOREIGN


def reduce_composer_states(states):
    """A SEQUENCE of observations -> the one state that survives them.

    THE LATCH, and it is a property of the fold rather than a patch on a
    predicate. A review's first residual: foreign evidence seen, then a later
    exact read wins and the Enter is spent anyway, discarding the edit we had
    already witnessed. Ranking makes that unstateable — once FOREIGN is
    observed the sequence is FOREIGN, and no amount of later agreement can
    un-see it. Three independent booleans could not hold this, because none of
    them could see the reads that came before.

    UNREADABLE outranks PLACEHOLDER and REPAINTING for the same reason a failed
    read must not clear evidence: a window of errors is not a window of quiet.
    An empty sequence is UNREADABLE — no observation was made, and that is not
    the same as an observation of nothing.

    AN UNKNOWN STATE RAISES RATHER THAN BEING DROPPED, which is a review's
    "reject unknown reducer states". The first version FILTERED anything it did
    not recognise, so a typo'd or newly-added state would vanish from the fold
    and the result would be computed from the remaining reads — quietly, with
    the right type and a plausible value. That is a fail-OPEN on the exact
    surface whose entire purpose is to withhold an Enter: adding OPAQUE in this
    very commit would have been silently ignored by every caller that had not
    also been updated. A name this reducer does not know is a programming
    error, and it says so.
    """
    seen = list(states or ())
    unknown = [s for s in seen if s not in _STATE_RANK]
    if unknown:
        raise ValueError(
            "reduce_composer_states: unknown composer state(s) %r — the fold "
            "refuses to rank a name it does not know rather than dropping it"
            % (sorted(set(map(str, unknown))),))
    if not seen:
        return UNREADABLE
    return max(seen, key=lambda s: _STATE_RANK[s])


def composer_first_line(text):
    """The first non-empty line of an outgoing payload, whitespace-normalized.

    What a composer RENDERS is this line — Claude wraps rather than truncates,
    so the visible row is this string or a prefix of it. Comparing against the
    whole payload could never match; comparing against nothing at all is how a
    verifier ends up asserting the absence of a complaint.
    """
    for line in (text or "").splitlines():
        if line.strip():
            return " ".join(line.split())
    return ""


def _frozen_capture_note(tails):
    """The clause to append when successive reads returned IDENTICAL bytes.

    RETRYING A CAPTURE THAT CANNOT REFRESH IS NOT CORROBORATION. A pane that
    has emitted nothing since it was attached hands back the same frame on
    every read, so N retries produce one observation N times — and the
    refusals below then count them as "N read(s)", which a person reads as N
    independent looks. Measured on a freshly resumed pane (task/1977): the
    screen read returned the same 24 lines with no composer line TWICE,
    minutes apart, and the composer appeared only once a real turn produced
    output. The box existed the whole time; the capture had not repainted.

    So the bytes are the evidence about whether the reads were independent,
    and this says so where the count is claimed. It changes no verdict: the
    refusal stays a refusal, because an unrefreshed capture is exactly the
    UNKNOWN case and authorising an action on it would be the opposite cure.

    Returns "" when the reads genuinely differed, and when there were fewer
    than two to compare — one read makes no claim about independence.
    """
    if len(tails) < 2 or len(set(tails)) > 1:
        return ""
    return ("; the capture did not change across those %d reads, so they are "
            "ONE observation repeated rather than %d independent looks — this "
            "pane may simply have emitted nothing since it was attached"
            % (len(tails), len(tails)))


def _prompt_line(tail):
    """The live composer line in a pane tail, or None when unresolvable.

    A one-line indirection so this module does not carry a lazy import at three
    call sites; the rule itself lives in seat_lifecycle and stays there.
    """
    from .seat_lifecycle import _current_prompt_line
    return _current_prompt_line(tail)


def composer_body(line):
    """A composer PROMPT LINE -> just the text sitting in it ("" when empty).

    ONE owner for this normalization. It lived in two places for an afternoon —
    here and in the fleet probe — which is how a rule about an invisible
    character starts meaning two different things in two files.

    The glyph and the body are separated by U+00A0, not a space (measured live
    2026-08-05: `❯\\xa0Resuming after compaction…`). The bare `.split()` is
    load-bearing and NOT interchangeable with `.split(" ")`: Python's default
    whitespace class INCLUDES U+00A0, so an empty composer (`❯` alone) and a
    held one both normalize correctly, while splitting on a literal space would
    leave the NBSP attached and make every empty composer look like held text.
    An explicit `.replace("\\xa0", " ")` stood here first; a mutant proved it
    dead code, and a comment is the honest replacement for it.
    """
    return " ".join((line or "").strip().lstrip("❯").split())


def composer_exactly_holds(tail, text):
    """Does the LIVE composer equal `text` exactly? True/False/None.

    This is deliberately stricter than `composer_holds`. The submit verifier may
    accept a visible prefix because Claude wraps long composer lines, but a later
    recovery is about to spend a BARE ENTER. A prefix cannot authorize that act:
    a human may have appended to Helm's text after it was injected. When wrapping
    or a collapsed paste chip hides the full value, the answer is UNKNOWN and the
    recovery refuses rather than submitting text it cannot identify completely.

    MIGRATED TO THE SHARED CHIP GRAMMAR (task/1909) with its CONTRADICTION
    SEMANTICS DELIBERATELY INTACT — a review's scope line, "the redesign is
    submit-only: verify/recovery keep contradictions". What changes is that the
    chip is parsed in one place; what does NOT change is that any chip, complete
    or still being drawn, answers UNKNOWN here. A recovery about to spend a bare
    Enter must refuse text it cannot identify completely, and both chip kinds
    are exactly that.
    """
    line = _prompt_line(tail)
    if line is None:
        return None
    body = composer_body(line)
    want = composer_first_line(text)
    if read_paste_chip(body)[0] is not CHIP_NONE:
        return None
    return bool(want) and body == want


def composer_holds(tail, text):
    """Does this pane's LIVE composer still hold `text`? True/False/None.

    None is UNREADABLE — no composer could be located in the tail — and it is
    the value that becomes UNKNOWN. It is returned for a blind pane (empty
    tail), for a pane mid-turn whose composer is not the current frame, and for
    any TUI whose prompt this does not recognise.

    The comparison is against OUR OWN TEXT, not against emptiness, and that is
    what makes it discriminate. A composer can legitimately be non-empty
    without holding our directive — a placeholder ("Press up to edit queued
    messages", measured live), a transient toast, or a human's half-typed
    draft. Only a prefix match against the payload we sent proves OUR message
    is the one still sitting there.

    MIGRATED TO THE SHARED CHIP GRAMMAR (task/1909), and the migration FIXES a
    real case here rather than merely relocating a regex. This asks "is our
    text still sitting there", so a collapsed chip counts as held — but the old
    `_PASTE_CHIP.match` was a PREFIX test, so a chip with a human's text
    appended after it also answered True. That is the same append the submit
    classifier calls FOREIGN, waved through by the predicate whose False means
    "it went in". That round was WRONG in the other direction — see the comment
    at the chip branch below: any chip is presence, and ownership is
    observe_composer's question, not this one's.
    """
    line = _prompt_line(tail)
    if line is None:
        return None                       # UNREADABLE — never "it went in"
    body = composer_body(line)
    if not body:
        return False                      # empty composer — it went in
    # ANY CHIP IS PRESENCE. This predicate answers "is text still SITTING
    # THERE" — the post-Enter question verify_submitted asks, whose False means
    # DELIVERED. The previous round returned False for a chip WITH appended
    # text, reasoning it was "not ours alone"; a probe measured the consequence:
    # a lost Enter followed by a human's append read as our turn DELIVERED —
    # worse than main, whose True withheld it. Ownership (whose text is it?)
    # is observe_composer's question, and FOREIGN lives there; presence must
    # stay True for a chip regardless of what follows it.
    if read_paste_chip(body)[0] is not CHIP_NONE:
        return True                       # a collapsed paste chip IS held text
    want = composer_first_line(text)
    if not want:
        return False                      # a bare Enter can never be "held"
    n = min(len(body), len(want))
    return bool(n) and body[:n] == want[:n]


def _pane_row(t):
    """One metaharness pane -> helm's uniform inventory row.

    Shared by the CLI leg (`orca terminal list --json`) and the RPC leg
    (`terminal.list`) because both emit the SAME terminal object; two copies of
    this mapping is how a field rename starts failing on one path only.

    `preview` (the pane's visible tail) rides along free, but note what it is
    NOT: it is mutable scrollback, so it can only ever be a WEAK hint about who
    a pane belongs to — measured live, only 2 of 25 panes still had their launch
    line in view. Pane->seat identity comes from the process, not from here.
    """
    return {"handle": t.get("handle"), "title": t.get("title") or "",
            "preview": t.get("preview") or "",
            "status": "connected" if t.get("connected") else "disconnected",
            "writable": t.get("writable") is True,
            "pty_id": t.get("ptyId"), "tab_id": t.get("tabId"),
            # PTY incarnation, not a tab-title generation: a renderer restore
            # or title change can leave the PTY intact. Both inventory legs
            # carry the host's value without inferring restart semantics.
            "incarnation_id": t.get("incarnationId"),
            "leaf_id": t.get("leafId"),
            "worktree_id": t.get("worktreeId"),
            "worktree": t.get("worktreePath"),
            # `orphaned` is the field that was DROPPED here and broke every pane
            # read after the orca .46 remint: a pane can be connected=True and
            # writable=True while its PTY has no live renderer (orphaned=True),
            # and reads against it return empty. It is orca's OWN statement of
            # "this pane has no live backend", and it is the only such statement
            # — there is no helm-side heuristic that distinguishes an orphaned
            # PTY from an idle one. Measured live 2026-07-26: 34 panes, 22
            # orphaned-while-connected, every one reading 0 chars behind a live
            # process. Carry it so _pane_live can stop answering True on it.
            "orphaned": t.get("orphaned") is True,
            # THE SECOND DROPPED FIELD, and the discriminator for a pane helm
            # cannot READ (as opposed to one it cannot reach). Measured live
            # 2026-08-05: 26 panes, and `lastOutputAt is None` selected EXACTLY
            # the 12 whose `terminal read` came back with an empty tail and
            # every cursor at 0 — while connected=True, writable=True and
            # orphaned=False said "fine" on all twelve, and at least one was a
            # seat that had posted to chat minutes earlier. It is orca's own
            # statement that it has never observed output from this pane, so a
            # probe that folds those into "clean" reports an all-clear it never
            # measured. Carried so a composer probe can answer CANNOT-TELL.
            "last_output_at": t.get("lastOutputAt")}


def worktree_panes(path, adapter=None):
    """Panes the metaharness has BOUND to `path` -> ([handles], error).

    THE THING /proc CANNOT SEE (2026-07-30, owner-reported, then measured).
    Worktree GC decided a room was empty by scanning /proc for a process whose
    CWD was inside it. But a pane is a METAHARNESS object that OUTLIVES its
    shell: the shell can be hung up, or its cwd moved, while orca still holds a
    terminal bound to that worktree. GC would hang up the shell, delete the
    directory, and leave the operator's sidebar holding a pane whose working
    directory no longer exists — which renders as a bare command prompt and is
    indistinguishable, from the outside, from someone having killed the agent.

    Measured on this host the morning it was found: one live bash sitting at a
    `(deleted)` cwd, and FIVE panes listed by orca with no worktree path at all.
    The owner reported it repeatedly as "someone killed my seats back to a CWD"
    and was told, more than once, that the seats were fine. He was right and the
    instruments were looking in the wrong place.

    RETURNS AN ERROR RATHER THAN AN EMPTY LIST when the metaharness cannot be
    asked, and callers MUST treat that as a refusal. "I could not look" and
    "nothing is there" are the same value only to code that has stopped caring
    which one it got; here they differ by a live agent. There is no metaharness
    at all -> ([], None), which IS an affirmative empty: nothing can be bound to
    a room by a host that does not exist.

    An adapter that does not carry a pane's worktree binding in its rows is
    UNANSWERABLE too, by the same rule — see `reports_pane_worktree`. Reading
    its rows would produce a bare [] that means "this adapter never says", and
    a bare [] here authorizes a delete.
    """
    ad = adapter if adapter is not None else detect()
    if ad is None:
        return [], None
    if not getattr(ad, "reports_pane_worktree", False):
        return None, ("the %s adapter does not report which worktree a pane is "
                      "bound to — set HELM_METAHARNESS=none to sweep anyway"
                      % getattr(ad, "name", "metaharness"))
    try:
        panes = ad.list()
    except (HarnessError, OSError) as exc:
        return None, "metaharness pane list unavailable: %s" % exc
    # REALPATH, not abspath: every other worktree comparison in this module
    # (`adopt_worktree`, `_native_home_worktree`, `_knows_worktree`) resolves
    # symlinks, because the metaharness's path and helm's `<repo>-wt/<lane>`
    # can reach the same directory through different links. A miss here is not
    # a cosmetic mismatch — it is a delete under a live pane.
    want = os.path.realpath(path).rstrip(os.sep)
    bound = []
    for p in panes:
        wt = p.get("worktree")
        if not wt:
            continue                      # unbound pane belongs to no room
        wt = os.path.realpath(wt).rstrip(os.sep)
        if wt == want or wt.startswith(want + os.sep):
            # A pane with no handle still BLOCKS; it just cannot be named.
            bound.append(p.get("handle") or "?")
    return bound, None


def disposable_worktree_pid(pid, proc_root="/proc"):
    """Whether a metaharness owns this PID as a disposable room placeholder.

    Today Orca is the only adapter with such a process: an inert shell-ready
    bash. Keeping the host fingerprint at the adapter seam prevents worktree GC
    from accumulating Orca/Herdr-specific argv and process-state cases. Any
    unreadable field is NOT disposable; future adapters extend this owner.
    """
    pid = str(pid)
    if not pid.isdigit():
        return False
    base = os.path.join(proc_root, pid)
    try:
        with open(os.path.join(base, "cmdline"), "rb") as f:
            argv = [a for a in f.read(65536).split(b"\0") if a]
        with open(os.path.join(base, "stat"), "rb") as f:
            raw = f.read(65536)
        with open(os.path.join(base, "task", pid, "children"), "rb") as f:
            children = f.read(65536).strip()
    except OSError:
        return False
    if not argv or os.path.basename(os.fsdecode(argv[0])) != "bash" or children:
        return False
    rcfile = None
    for i, arg in enumerate(argv):
        if arg == b"--rcfile" and i + 1 < len(argv):
            rcfile = argv[i + 1]
            break
        if arg.startswith(b"--rcfile="):
            rcfile = arg.split(b"=", 1)[1]
            break
    shell_ready = (b".config/orca/shell-ready",
                   b".config/orca/shell-ready/bash/rcfile")
    if rcfile is None or not any(
            rcfile == p or rcfile.endswith(b"/" + p) for p in shell_ready):
        return False
    close = raw.rfind(b") ")
    fields = raw[close + 2:].split() if close >= 0 else []
    if len(fields) < 6:
        return False
    try:
        state, pgrp, session, tpgid = (
            fields[0], int(fields[2]), int(fields[3]), int(fields[5]))
        n = int(pid)
    except (TypeError, ValueError):
        return False
    return state == b"S" and session == n and pgrp == n and tpgid == pgrp


class _CLIAdapter:
    """Shared subprocess+JSON plumbing. Subclasses set name/bin and translate
    the uniform interface into their CLI's argv + reply shapes."""
    name = None   # adapter id ("orca"/"herdr")
    bin = None    # the CLI binary name looked up on PATH
    # Does this adapter's `list()` row carry `worktree` — the directory the
    # pane is bound to? DEFAULT FALSE, and the default is the whole point:
    # `worktree_panes` is the guard that keeps GC from deleting a room out from
    # under a live pane, and an adapter that never fills the field would hand
    # it an empty list that reads as "nothing is bound here". Unproven means
    # UNANSWERABLE, which refuses; flip this to True in the same commit that
    # makes the adapter's row builder carry the field.
    reports_pane_worktree = False

    def __init__(self, path=None):
        self.path = path or self.bin

    def spawn_failure_absent(self, error):
        """Proof a failed CLI create never started, inherited by real adapters."""
        return isinstance(error, HarnessError) and error.request_absent

    def _run(self, args, timeout=60, env=None):
        """`env` OVERLAYS the ambient environment for this one call (never
        replaces it — the CLI needs HOME/PATH/its own socket vars). It is how
        the shared-tree ref-guard is handed the single bit distinguishing a
        sanctioned worktree creator from a forbidden one, scoped to the call
        rather than leaked into the process (the work/_claims.py pattern)."""
        cmd = [self.path] + list(args)
        label = "%s %s" % (self.name, " ".join(args[:2]))
        try:
            p = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=timeout,
                               env=dict(os.environ, **env) if env else None)
        except OSError as e:
            raise HarnessError("%s: %s" % (label, e), request_absent=True)
        except subprocess.TimeoutExpired as e:
            raise HarnessError("%s: %s" % (label, e), timed_out=True)
        if p.returncode != 0:
            body = (p.stderr or p.stdout or "").strip()
            raise HarnessError("%s: rc %d — %s" % (
                label, p.returncode, body[:300]), code=_error_code(body))
        try:
            d = json.loads(p.stdout)
        except ValueError:
            raise HarnessError("%s: unparseable JSON reply (%r)"
                               % (label, (p.stdout or "").strip()[:120]))
        if not isinstance(d, dict):
            raise HarnessError("%s: non-object JSON reply" % label)
        err = d.get("error")
        if err or d.get("ok") is False:   # herdr error envelope / orca ok:false
            msg = (err or {}).get("message") if isinstance(err, dict) else None
            code = (err or {}).get("code") if isinstance(err, dict) else d.get("code")
            raise HarnessError("%s: %s" % (label, msg or json.dumps(d)[:200]),
                               code=code)
        result = d.get("result")
        return result if isinstance(result, dict) else {}

    # -- the TURN verb, shared by every adapter -------------------------------

    def submit(self, handle, text, settle=None, reads=None, interval=None,
               on_typed=None, admit=None):
        """Give a pane a TURN, and prove it -> (state, detail).

        state is one of DELIVERED / NOT_DELIVERED / UNKNOWN and the third is
        never folded into the first. Defined ONCE here rather than per adapter,
        so orca, herdr and every adapter added later inherit the same contract
        and every caller inherits it through them.

        FIVE ACTS, and the split is the point:
          0. READ first; refuse a dirty or unreadable composer.
          1. TYPE the text with NO Enter.
          2. SETTLE.
          3. A BARE ENTER, as its own call with its own receipt.
          4. READ THE PANE BACK and require that it ADVANCED.

        Step 4 is what `send` never did. `send` succeeded when the metaharness
        accepted the bytes, which is true of every one of the panes the owner
        found sitting with their next instruction typed and unsent. A proven
        NOT_DELIVERED result is only observation one; retry lives outside this
        method behind durable provenance plus a later persistence observation.
        Identity without persistence never earns a recovery Enter.

        FAILURE DIRECTIONS ARE DELIBERATE. A typing failure is NOT_DELIVERED —
        nothing was typed, nothing can be sitting there. An Enter failure falls
        THROUGH to verification rather than guessing, because the text is in
        the composer either way and only the pane can say what happened to it.
        An unreadable pane is UNKNOWN even when the send legs both returned ok.

        THE PRE-READ IS AN ACTUATION GATE, and it was found on a live pane
        rather than reasoned about. A composer can ALREADY hold a human's
        half-finished draft. Typing and Enter in that state would submit the
        draft even if verification later admitted UNKNOWN, so dirty OR unreadable
        composers now refuse before either keystroke. Verification cannot make an
        unsafe act safe after the fact.
        """
        # An EXPLICIT argument outranks the env override, which outranks the
        # default. The other order silently ignores what the caller asked for.
        if settle is None:
            settle = _env_float("HELM_SUBMIT_SETTLE_S", SUBMIT_SETTLE_S)
        reads = SUBMIT_VERIFY_READS if reads is None else reads
        interval = (SUBMIT_VERIFY_INTERVAL_S if interval is None else interval)

        def clean():
            dirty, refusal = self.composer_was_dirty(
                handle, reads=reads, interval=interval)
            return (True, None) if dirty is False else \
                (False, (UNKNOWN, refusal))

        # THE PLACEMENT DOOR: prepare, a fresh CLEAN proof, a non-blocking
        # validation of the preparation, then the first keystroke. A caller
        # with no authorization to prepare gets the proof alone.
        def type_it(timeout=None):
            try:
                self.send(handle, text, enter=False,
                          **self._send_bound(timeout))
            except HarnessError as e:
                return e
            return None

        placed, got = self._through_door(
            "placement", admit, clean, self._final_clean(handle), type_it)
        if not placed:
            if isinstance(got, DoorRefusal):
                return NOT_DELIVERED, DoorRefusal(
                    "pane %s: nothing typed -- %s" % (handle, got),
                    got.door, got.kind)
            return got
        typed_err, placement = got
        if typed_err is not None:
            return NOT_DELIVERED, "pane %s refused the text: %s%s" % (
                handle, typed_err, act_note(placement))
        if on_typed is not None:
            # The callback records provenance, not delivery. It runs only after
            # a successful type into a composer this call proved CLEAN.
            # Bookkeeping failure must not strand the text by preventing the
            # Enter leg.
            try:
                on_typed(handle, text)
            except Exception:
                pass
        if settle > 0:
            time.sleep(settle)

        def holds():
            state, detail = self._held_window(handle, text, reads, interval)
            return (True, None) if state is HOLDS else (False, (UNKNOWN, detail))

        # THE ENTER DOOR, the same shape: prepare, a fresh HOLDS proof taken
        # through the bounded window, a non-blocking validation, then the bare
        # Enter. Every refusal here leaves Helm's text placed and unsubmitted,
        # which is exactly NOT_DELIVERED; a refused PROOF keeps its own graded
        # reason, because "could not look" and "saw someone's text" are
        # different claims about a person.
        admitted, got = self._through_door(
            "enter", admit, holds, self._final_holds(handle, text),
            lambda timeout=None: self._press_enter(handle, timeout))
        if not admitted:
            if isinstance(got, DoorRefusal):
                return NOT_DELIVERED, DoorRefusal(
                    "pane %s: Enter withheld at the act -- %s -- placed by "
                    "Helm, not submitted" % (handle, got), got.door, got.kind)
            return got
        enter_err, entered = got
        state, detail = self._verify_after_enter(
            handle, text, enter_err, reads=reads, interval=interval)
        return state, "%s%s" % (detail, act_note(placement) + act_note(entered))

    def _final_clean(self, handle):
        """The placement door's LAST observation: one bounded read whose
        composer must still be CLEAN.

        Each final helper answers True (the proven state), False (a composer
        READ and seen to hold something else: it moved) or None (UNKNOWN). A
        read that raises is UNKNOWN, and so is a read that RETURNS a frame in
        which no composer can be identified: nothing was seen to move, so the
        door refuses at once instead of restarting as if it had."""
        def final(timeout):
            try:
                tail = self.read(handle, limit=SUBMIT_READ_LIMIT,
                                 timeout=timeout)
            except (HarnessError, OSError) as e:
                return None, ("pane %s could not be read at the final "
                              "capture: %s" % (handle, e))
            line = _prompt_line(tail)
            if line is None:
                return None, ("pane %s showed no live composer at the final "
                              "capture, so it could not be read" % handle)
            body = composer_body(line)
            if body and not composer_is_placeholder(body):
                return False, ("pane %s composer was no longer clean at the "
                               "final capture" % handle)
            return True, ""
        return final

    def _final_holds(self, handle, text):
        """The Enter door's LAST observation: one bounded read whose composer
        must still hold exactly what Helm typed."""
        def final(timeout):
            try:
                tail = self.read(handle, limit=SUBMIT_READ_LIMIT,
                                 timeout=timeout)
            except (HarnessError, OSError) as e:
                return None, ("pane %s could not be read at the final "
                              "capture: %s" % (handle, e))
            state = observe_composer(tail, text)
            if state is HOLDS:
                return True, ""
            if state is UNREADABLE:
                return None, ("pane %s composer read UNREADABLE at the final "
                              "capture" % handle)
            return False, ("pane %s composer read %s, not HOLDS, at the final "
                           "capture" % (handle, state))
        return final

    def _through_door(self, door, admit, prove, final=None, act=None):
        """ONE ACT DOOR -> (True, (act result, receipt)) when the act ran,
        else (False, refusal).

        THE ORDER IS THE CONTRACT, and every door that types or presses a key
        takes this one:

          1. `admit(door)` PREPARES. It may block: a backlog census under its
             locks, a pause read, identity.
          2. `prove()` takes the graded composer proof, the classifier whose
             refusal names what it saw.
          3. THE FINAL CAPTURE, inside ONE declared window (`act_deadline_s`,
             monotonic, from the capture's first read to the start of the
             send): the grant's `capture()` re-reads every certified
             dependency, and LAST, `final(timeout)` reads the composer once
             more and requires the proven state. A composer edit made during
             the dependency reads is therefore seen by the composer read after
             them.
          4. A CPU-only check that the attempt's horizon, captured in step 3,
             has not passed; then THE ACT, whose CLI subprocess takes
             `act_send_s` as its client timeout. The window in step 3 admits
             the act; it does not bound it. The subprocess timeout starts
             inside the act callback, after the window closed, so the two
             together are not a hard deadline from the capture to the send's
             return or to the keystroke's effect. A send killed at its timeout
             is accounted obsolete-authorization with its landing unknown.
          5. THE ACCOUNTING. The same dependencies are re-read once and
             compared with the final capture. An authorization that changed
             between the capture and the keystroke is not something any lock
             here can forbid -- the keystroke is an external event -- so it is
             ACCOUNTED: the grant records the act as obsolete-authorization,
             and its caller refuses a repeat. The measured window (capture to
             send start, capture to send return, send start to the accounting
             read) rides the receipt, per act, never a constant.

        A capture that finds an input moved, a composer that no longer reads
        as proven, or a window that overran its deadline restarts the whole
        preparation and proof, at most ADMISSION_RESTARTS times, and then
        refuses naming what moved or stalled and how long the capture ran. A
        dependency or a composer that could not be read refuses at once: it is
        UNKNOWN, and UNKNOWN authorizes nothing. Every round that acts on
        nothing tells its grant so (`abandon`). `refusal` is either the
        proof's own (state, detail) or a DoorRefusal.
        A caller with no `admit` has no authorization to capture or account:
        it gets the proof, then the act, and a receipt of None."""
        act = act or (lambda timeout=None: None)
        send_s = act_send_s()
        if admit is None:
            proven, refusal = prove()
            if not proven:
                return False, refusal
            return True, (act(send_s), None)
        moved = []
        for _round in range(1 + max(0, ADMISSION_RESTARTS)):
            grant = admit(door)
            if not grant.ok:
                return False, DoorRefusal(
                    grant.why or "the reason to act no longer stands",
                    door, grant.kind or "unknown")
            outcome = self._one_round(door, grant, prove, final, act, send_s)
            if outcome[0] == "act":
                return True, outcome[1]
            # NOTHING WAS ACTED ON IN THIS ROUND, whatever comes next.
            grant.abandon()
            if outcome[0] == "refuse":
                return False, outcome[1]
            moved.append(outcome[1])
        return False, DoorRefusal(
            "%s -- it moved or stalled at the final capture on each of %d "
            "preparations, so the obligation stands and nothing was acted on"
            % (moved[-1], len(moved)), door, "moved")

    def _one_round(self, door, grant, prove, final, act, send_s):
        """One prepared round of `_through_door` -> ("act", (result,
        receipt)), ("refuse", refusal) or ("moved", what moved or stalled).
        A moved or stalled round says how long the capture ran and how that
        split between the dependencies and the composer, because a refusal
        that publishes no measurement leaves a host where every act refuses
        with nothing to calibrate against."""
        proven, refusal = prove()
        if not proven:
            return "refuse", refusal
        deadline = act_deadline_s()
        start, cpu = time.monotonic(), time.process_time()

        def ran(what, composer_start=None):
            now = time.monotonic()
            deps = (composer_start if composer_start is not None else now) \
                - start
            return ("%s (the capture ran %.3fs: dependencies %.3fs, composer "
                    "%.3fs)" % (what, now - start, deps, now - start - deps))
        done, got = _bounded(grant.capture, deadline)
        if not done:
            return "moved", ran("the final capture of the authorization did "
                                "not return inside its %.2fs deadline"
                                % deadline)
        valid, why, facts = got
        if valid is None:
            return "refuse", DoorRefusal(
                "the authorization could not be captured after the composer "
                "proof (%s)" % (why or "no detail"), door, "unknown")
        if valid is not True:
            return "moved", ran(why or "an input of the authorization")
        composer_start = time.monotonic()
        if final is not None:
            left = deadline - (composer_start - start)
            done, seen = (False, None) if left <= 0 else _bounded(
                lambda: final(min(SUBMIT_READ_TIMEOUT_S, left)), left)
            if not done:
                return "moved", ran("the final composer capture did not "
                                    "return inside the %.2fs deadline"
                                    % deadline, composer_start)
            if seen[0] is None:
                # AN UNREADABLE COMPOSER IS UNKNOWN, and UNKNOWN refuses at
                # once: it is not a composer that moved.
                return "refuse", DoorRefusal(
                    seen[1] or "the composer could not be read at the final "
                    "capture", door, "unknown")
            if seen[0] is not True:
                return "moved", ran(seen[1] or "the composer moved",
                                    composer_start)
        elapsed = time.monotonic() - start
        if elapsed > deadline:
            return "moved", ran("the final capture ran past its %.2fs "
                                "deadline" % deadline, composer_start)
        horizon = facts.get("horizon")
        if horizon is not None and time.time() >= horizon:
            return "refuse", DoorRefusal(
                "the attempt passed its act horizon between the final capture "
                "and the keystroke, so nothing was acted on", door, "stale")
        send_start = time.monotonic()
        result = act(send_s)
        returned = time.monotonic()
        receipt = {"door": door, "deadline_s": deadline, "send_bound_s": send_s,
                   "capture_dependencies_s": composer_start - start,
                   "capture_composer_s": send_start - composer_start,
                   "capture_to_send_start_s": send_start - start,
                   "capture_to_send_return_s": returned - start,
                   "capture_cpu_s": time.process_time() - cpu}
        receipt.update(self._reconcile(grant, facts, send_start, deadline))
        if getattr(result, "timed_out", False):
            # THE SEND WAS KILLED AT ITS BOUND. Whether the keystroke landed
            # is unknown, so this act cannot be shown to have run under the
            # authorization it was captured with.
            receipt["outcome"] = OBSOLETE_AUTHORIZATION
            receipt["what"] = "; ".join(w for w in (
                "the send did not return inside its %.2fs bound, so whether "
                "the keystroke landed is unknown" % send_s,
                receipt.get("what")) if w)
        receipt["account_error"] = grant.account(receipt)
        self.__dict__.setdefault("act_receipts", []).append(receipt)
        return "act", (result, receipt)

    @staticmethod
    def _reconcile(grant, facts, send_start, deadline):
        """The accounting read after an act, under the same deadline: a read
        that does not return in time cannot show the authorization held, so it
        is obsolete-authorization too."""
        try:
            done, got = _bounded(lambda: grant.recheck(facts), deadline)
        except Exception as exc:             # noqa: BLE001 — a failed read
            done, got = True, (OBSOLETE_AUTHORIZATION, (   # proves nothing
                "the accounting read raised %s" % exc.__class__.__name__))
        read = time.monotonic() - send_start
        if not done:
            got = (OBSOLETE_AUTHORIZATION, "the accounting read did not return "
                   "inside its %.2fs deadline" % deadline)
        return {"outcome": got[0], "what": got[1],
                "send_start_to_reconciliation_s": read}

    def _send_bound(self, timeout):
        """The keyword that bounds an act's send by `timeout`: {"timeout": t}
        when this adapter's send takes one -- both shipped adapters do -- and
        {} for a double whose send does not."""
        if timeout is None:
            return {}
        try:
            import inspect
            takes = "timeout" in inspect.signature(self.send).parameters
        except (TypeError, ValueError):
            takes = False
        return {"timeout": timeout} if takes else {}

    def _press_enter(self, handle, timeout=None):
        """One bare Enter -> the HarnessError it raised, or None."""
        try:
            self.send(handle, "", enter=True, **self._send_bound(timeout))
        except HarnessError as e:
            return e
        return None

    def _held_window(self, handle, text, reads, interval):
        """(HOLDS, None) or (the settled state, its graded refusal) across ONE
        bounded read window after typing."""
        # SETTLE IS A FLOOR, NOT AN ANSWER. A long payload renders slower than
        # any fixed settle, so the question "has the composer finished showing
        # what we typed" is asked across a bounded window of reads, the same
        # window the pre-read uses. A seat whose Enter is refused on its own
        # repaint never runs the brief that arms its beacon.
        #
        # THE WINDOW COLLECTS STATES AND FOLDS THEM. The fold latches FOREIGN,
        # so a later exact read cannot un-see positive evidence of someone
        # else's text, and a later unreadable read cannot downgrade it to
        # "could not read". Strict-prefix evidence cannot tell a repaint from a
        # deletion, so that middle is REPAINTING, which authorizes nothing and
        # accuses nobody.
        tail, last_error, observed, tails = None, None, [], []
        for n in range(1, max(1, reads) + 1):
            if n > 1 and interval > 0:
                time.sleep(interval)
            try:
                tail = self.read(
                    handle, limit=SUBMIT_READ_LIMIT,
                    timeout=SUBMIT_READ_TIMEOUT_S)
            except (HarnessError, OSError) as e:
                last_error = str(e)
                observed.append(UNREADABLE)
                continue
            tails.append(tail)
            state = observe_composer(tail, text)
            observed.append(state)
            # ONLY HOLDS ENDS THE POLL EARLY. FOREIGN latches in the fold, so
            # no later read can change the verdict, but the window is a
            # property of the REFUSAL: a person reading it can trust that a
            # slow frame was given its full chance.
            if state is HOLDS:
                break
        settled = reduce_composer_states(observed)
        if settled == HOLDS:
            return HOLDS, None
        saw_foreign = settled == FOREIGN
        # THE REFUSAL IS TOTAL AND ONLY ATTRIBUTION IS GRADED: a POSITIVELY
        # foreign composer is the one reading a person can be blamed for; a
        # collapsed chip, a pane that could not be read, and a composer that
        # never showed our text are each "could not observe", never an edit.
        if saw_foreign:
            return settled, ("pane %s composer holds text that is not what Helm "
                             "typed, still after %d read(s) — refusing Enter "
                             "because a human may have edited the composer"
                             % (handle, len(observed)))
        if settled == OPAQUE:
            return settled, ("pane %s composer shows a collapsed paste chip and "
                             "nothing that identifies it, after %d read(s) — "
                             "refusing Enter because a chip renders the same "
                             "whether it collapsed Helm's text or a human's "
                             "paste, NOT because anyone edited it"
                             % (handle, len(observed)))
        if tail is None:
            return settled, ("pane %s became unreadable before Enter: %s — "
                             "refusing the keystroke"
                             % (handle, last_error or "no read succeeded"))
        # THE NOTE GOES ON THE NEGATIVE ONLY, and the asymmetry is the point:
        # a frozen capture that SHOWS foreign text or a paste chip is still a
        # real observation of one — a positive needs a single look — while
        # "never showed" is a claim about everything the reads did not
        # contain, and repeated identical frames are not more of that.
        return settled, ("pane %s never showed an identifiable composer holding "
                         "Helm's text across %d read(s)%s — refusing Enter "
                         "because the composer could not be read, NOT because "
                         "anyone edited it"
                         % (handle, len(observed),
                            _frozen_capture_note(tails)))

    def choose_in_modal(self, handle, intent, on_typed=None, settle=None):
        """(state, proof) for selecting the option that MEANS `intent`.

        THIS IS A DIFFERENT OPERATION FROM `submit`, AND THAT SEPARATION IS THE
        SAFETY RATHER THAN AN ORGANISING PREFERENCE. `submit` types text a
        caller chose earlier; its pre-read exists to protect a human's draft and
        it refuses a pane with no composer. A modal has no composer, so an
        earlier build made `submit` branch into modal handling whenever the pane
        classified as a dialog — which meant EVERY caller's text became modal
        input the moment a dialog was up, including the boot and re-arm prose
        `seat` sends, and a digit chosen when the pane was ASSESSED was spent
        later against options that may have been reordered since.

        So the choice is its own door and it takes an INTENT, never a digit:
          - the pane is re-read HERE, and the option is derived from THAT tail
            by the shipped chooser, so what is pressed is decided at the
            keystroke and cannot be a stale index into a changed dialog;
          - a tail that is no longer a recognised modal REFUSES — there is no
            fall-through to composer submission, so a dialog that closed
            between assessment and delivery costs nothing and types nothing;
          - a modal whose current options no longer offer `intent` REFUSES,
            because authority proves WHICH PANE, never what an option means;
          - NO ENTER IS EVER SPENT: a numbered dialog acts on the keystroke.

        CLEARANCE IS POSITIVE. The re-read must classify to a recognised
        NON-modal state. An empty tail is what `read` returns for a failed read,
        and an unrecognised one is a pane nobody can interpret; calling either
        of those "delivered" would spell a keystroke that may never have
        registered as a proven escape.
        """
        from . import panetail
        from .seat_lifecycle import vendor_escape_choice
        if settle is None:
            settle = _env_float("HELM_SUBMIT_SETTLE_S", SUBMIT_SETTLE_S)
        try:
            tail = self.read(handle)
        except HarnessError as e:
            return UNKNOWN, ("pane %s could not be read before choosing, so "
                             "nothing was typed: %s" % (handle, e))
        # ADMISSION ASKS THE DIALOG'S OWN EVENTS, NOT A SCALAR STATE WORD.
        # `_classify_pane_tail` reduces the whole tail to one word, and the
        # word it answers for an ENDED option run sitting above a human's
        # half-typed draft is the SAME word it answers for a dialog that is
        # genuinely capturing keys. A gate that cannot separate those two
        # cases cannot gate a keystroke — which is the defect this lane
        # exists for, and the reason the scalar is gone from this door rather
        # than tightened. The facts below separate them by construction: the
        # occupied composer below the run IS the evidence the scalar threw
        # away, and here it is a positioned event that outranks the run.
        parsed = panetail.parse(tail)
        standing = panetail.modal_standing(parsed)
        if standing.state == panetail.ENDED or standing.occurrence is None:
            return NOT_DELIVERED, (
                "pane %s is not showing a dialog that owns input at delivery "
                "time (%s), so no choice was made and nothing was typed"
                % (handle, standing.why))
        run = standing.occurrence.detail
        if run.qualification != panetail.QUALIFIED:
            # SUPPRESSION IS NOT A REASON TO REACH PAST IT. A newer numbered
            # run that is not a dialog means no choice is on offer; the older
            # qualifying list below it is scrollback, and answering that is
            # typing a digit at whatever now owns the screen.
            return NOT_DELIVERED, (
                "the newest numbered run on pane %s is not a dialog, so no "
                "choice is on offer and nothing was typed — helm does not "
                "fall through to an older list (%s)" % (handle, standing.why))
        wall = panetail.wall_standing(parsed)
        if (wall.state == panetail.IN_FORCE
                and wall.occurrence.pos.line > run.end.line):
            # POSITION IS NOT CHRONOLOGY AND IT IS NOT OWNERSHIP. Two
            # placements the tail DOES settle, and both of them admit: a wall
            # strictly above the first option row is the banner that RAISED
            # this dialog — "you've reached your usage limit" over "1. Yes /
            # 2. No" is the single most common shape the escape exists for, and
            # refusing it would refuse the feature — and a wall on a line the
            # run occupies is the reason spelled inside the label being
            # offered, which cannot have arrived after the label carrying it.
            #
            # BELOW THE LAST NUMBERED ROW SETTLES NOTHING, and this branch
            # claims nothing. A run ends at its last NUMBERED row because a
            # line that is not an option is what closes it, so the row beneath
            # a run is a wrapped continuation of the final label — "3. No, keep
            # my current model (reason:" over "   usage balance exhausted)" —
            # as readily as it is a newer screen. Those two render identically
            # and no pane tail separates them. So the door states the
            # measurement, names the chronology UNKNOWN, and takes the decision
            # this build takes for any dialog whose ownership it cannot vouch
            # for: nothing is typed.
            #
            # THE QUOTED STRING IS THE PANE'S, NOT THE RECOGNIZER'S. A `Wall`
            # carries both: `spelling` is the pattern out of the table that
            # matched and `line` is the pane line the match was found in. Only
            # the second is something an operator can act on — there is no
            # matched-slice field, and the line as read is the narrowest
            # observation this record holds. Printing the pattern under these
            # words would hand a human regex syntax as the screen and drop the
            # vendor's own wording, which is the only part that tells them
            # which limit they hit.
            observed = (wall.occurrence.detail.line or "").strip()
            return NOT_DELIVERED, (
                "pane %s carries quota-wall text (%r) on line %d with the "
                "choices on lines %d to %d, and a pane tail cannot say whether "
                "that wall is a newer screen or the last option's own wrapped "
                "label: which of them holds the keys is UNKNOWN, so nothing "
                "was typed" % (handle, observed[:200],
                               wall.occurrence.pos.line, run.start.line,
                               run.end.line))
        # The SAME run the standing facts were judged on decides the digit —
        # one tail, one parse, one ordered list. Passing it is what stops the
        # chooser re-scanning and answering about the older menu this door
        # just refused.
        digit, kind, seen = vendor_escape_choice(tail, options=run.options)
        if kind != intent or not digit:
            return NOT_DELIVERED, (
                "the dialog on pane %s does not currently offer %s (its safe "
                "option reads %s), so nothing was typed" % (handle, intent,
                                                            kind or "nothing"))
        if ESCAPE_TYPES_NOTHING:
            # DETECTED, NAMED, NOT PRESSED. The refusal carries what was seen
            # so the seat is VISIBLE to an operator — which is the outcome the
            # lane exists for — while the keystroke waits on a producer event
            # that can say these options own current input.
            return NOT_DELIVERED, (
                "pane %s is showing a vendor dialog whose safe option reads %r "
                "(option %s), and helm types NOTHING into a dialog in this "
                "build: a pane tail cannot distinguish an offer awaiting input "
                "from one already answered above a human's draft. The seat is "
                "parked and visible; re-enabling the keystroke is task/2386"
                % (handle, seen, digit))
        try:
            self.send(handle, digit, enter=False)
        except HarnessError as e:
            # PROVEN ABSENCE AND UNCERTAINTY ARE DIFFERENT FACTS. Only a CLI
            # that could not be launched proves the request never left; a
            # timeout or a malformed reply can follow input the pane accepted,
            # and reporting that as "not delivered" tells a caller no key was
            # pressed when one may have been.
            if getattr(e, "request_absent", False):
                return NOT_DELIVERED, ("pane %s never received the choice %r — "
                                       "the request did not leave this box: %s"
                                       % (handle, digit, e))
            return UNCERTAIN, ("the choice %r was sent to pane %s and the send "
                               "failed in a way that cannot say whether the "
                               "keystroke arrived: %s" % (digit, handle, e))
        if on_typed is not None:
            # Provenance, not delivery — and a bookkeeping failure must never
            # strand a keystroke that has already left.
            try:
                on_typed(handle, digit)
            except Exception:
                pass
        if settle > 0:
            time.sleep(settle)
        try:
            after = self.read(handle)
        except HarnessError as e:
            return UNKNOWN, ("choice %r was sent to pane %s and the pane could "
                             "not be re-read (%s), so whether the dialog "
                             "cleared is unknown" % (digit, handle, e))
        if not (after or "").strip():
            return UNKNOWN, ("choice %r was sent to pane %s and the pane read "
                             "back EMPTY, which is what a failed read returns — "
                             "not evidence the dialog cleared" % (digit, handle))
        # CLEARANCE IS A QUESTION ABOUT THE DIALOG, NOT ABOUT WHICH STATE WON
        # THE SCREEN. Reading a scalar classification and calling every
        # non-MODAL answer "delivered" certifies a clearance from the wrong
        # fact: a pane that is walled AND still showing a dialog classifies as
        # something else entirely — measured, a wall above a Yes/No run reads
        # BLOCKED_ON_HUMAN — and the dialog is still up. So the dialog is asked
        # about ITSELF, and only positive evidence that it ENDED clears.
        standing = panetail.modal_standing(panetail.parse(after))
        if standing.state == panetail.ENDED:
            return DELIVERED, ("choice %r (%s) sent to pane %s and the dialog "
                               "has ended: %s" % (digit, seen, handle,
                                                  standing.why))
        return UNKNOWN, ("choice %r was sent to pane %s and the dialog has NOT "
                         "been shown to have ended (%s), so whether it cleared "
                         "is unknown — a pane can be walled, or showing a "
                         "replacement dialog, while the original still owns "
                         "input" % (digit, handle, standing.why))

    def composer_was_dirty(self, handle, reads=None, interval=None):
        """(True/False/None, detail) after one bounded pre-actuation poll.

        A pane repaint and an unreadable pane used to share one one-shot None.
        Polling the same bounded window as submission verification lets a late
        composer answer, while the terminal refusal preserves which of three
        facts was measured: every read failed, readable chrome had no live
        composer, or the composer holds exact pre-existing text.
        """
        from .seat_lifecycle import _classify_pane_tail
        reads = SUBMIT_VERIFY_READS if reads is None else reads
        interval = SUBMIT_VERIFY_INTERVAL_S if interval is None else interval
        last_error, last_tail, tails = None, None, []
        for n in range(1, max(1, reads) + 1):
            if n > 1 and interval > 0:
                time.sleep(interval)
            try:
                tail = self.read(
                    handle, limit=SUBMIT_READ_LIMIT,
                    timeout=SUBMIT_READ_TIMEOUT_S)
            except (HarnessError, OSError) as exc:
                last_error = str(exc)
                continue
            last_tail = tail
            # ONLY THE READS THAT SUCCEEDED. A failed read produced no bytes
            # to compare, so it can neither prove nor disprove a repaint.
            tails.append(tail)
            line = _prompt_line(tail)
            if line is None:
                continue
            body = composer_body(line)
            if body and not composer_is_placeholder(body):
                detail = ("pane %s composer holds %r — refusing to type or "
                          "spend Enter because it may be a human draft"
                          % (handle, body))
                safe = ("pane %s composer holds a pre-existing human draft "
                        "(%d characters, redacted) — refusing to type or spend "
                        "Enter" % (handle, len(body)))
                return True, _PreReadRefusal(detail, False, safe)
            return False, None
        if last_tail is not None:
            # THE CLASSIFICATION BELONGS TO THE LAST FRAME THAT COULD BE READ,
            # WHICH IS NOT ALWAYS THE LAST FRAME. `last_tail` survives later
            # failed reads, so a pane that was readable and then went dark
            # produced a confident "classified pane state: X" about a frame the
            # pane has since stopped confirming. The state is still the best
            # evidence available and is kept; what was missing is that an
            # operator could not tell it from a classification of the pane as
            # it stands now.
            state = _classify_pane_tail(last_tail)[0]
            stale = ("; LATER READ(S) FAILED (%s), so this state is the last "
                     "frame that could be read and not necessarily the pane "
                     "as it stands" % last_error) if last_error else ""
            detail = ("pane %s has no live composer located after %d read(s); "
                      "classified pane state: %s%s%s — refusing to type or "
                      "spend Enter" % (handle, max(1, reads),
                                       state or "UNKNOWN", stale,
                                       _frozen_capture_note(tails)))
            return None, _PreReadRefusal(detail, True)
        detail = ("pane %s read failed during composer pre-read after %d "
                  "read(s): %s — refusing to type or spend Enter"
                  % (handle, max(1, reads),
                     last_error or "unknown read error"))
        return None, _PreReadRefusal(detail, True)

    def _verify_after_enter(self, handle, text, enter_err, reads=None,
                            interval=None):
        """The pane's measured tri-state after an Enter already spent."""
        state, detail = self.verify_submitted(
            handle, text, reads=reads, interval=interval, dirty=False)
        if enter_err:
            detail = "%s (the Enter call itself errored: %s)" % (
                detail, enter_err)
        return state, detail

    def verify_submitted(self, handle, text, reads=None, interval=None,
                         dirty=False):
        """Did `text` leave the composer? -> (state, detail).

        Polls a BOUNDED repaint window. The first read that proves the composer
        is clear of our text wins; a composer that still holds it after every
        read is NOT_DELIVERED; a pane that never yielded a readable composer is
        UNKNOWN. Separate from `submit` because the same proof is what a probe
        and a retry both need.

        `dirty` is what the composer looked like BEFORE we typed. True or
        UNREADABLE (None) both raise the bar to an EMPTY composer, because with
        foreign text in front of ours "does not start with our text" stops
        meaning anything. Default False keeps the plain case cheap.
        """
        reads = SUBMIT_VERIFY_READS if reads is None else reads
        interval = (SUBMIT_VERIFY_INTERVAL_S if interval is None else interval)
        last = "pane %s never yielded a readable composer" % handle
        proven_held = False
        for n in range(1, max(1, reads) + 1):
            if n > 1 and interval > 0:
                time.sleep(interval)
            try:
                tail = self.read(
                    handle, limit=SUBMIT_READ_LIMIT,
                    timeout=SUBMIT_READ_TIMEOUT_S)
            except (HarnessError, OSError) as e:
                last = "pane %s re-read failed: %s" % (handle, e)
                continue
            held = composer_holds(tail, text)
            if held is None:
                last = ("pane %s returned no readable composer (read %d)"
                        % (handle, n))
                continue
            if not held:
                if dirty is False:
                    return DELIVERED, ("pane %s advanced — the composer no "
                                       "longer holds the text (read %d)"
                                       % (handle, n))
                # The composer was ALREADY dirty (or unreadable) before we
                # typed, so our text is not the first line and "does not start
                # with our text" proves nothing. Only EMPTY proves it here.
                line = _prompt_line(tail)
                if line is not None and not composer_body(line):
                    return DELIVERED, ("pane %s advanced to an EMPTY composer "
                                       "(read %d; it held foreign text before "
                                       "the send, so nothing weaker counts)"
                                       % (handle, n))
                last = ("pane %s held foreign text before the send and its "
                        "composer is still not empty — helm cannot tell whose "
                        "text is in there or whether the turn landed (read %d)"
                        % (handle, n))
                continue
            proven_held = True
            last = ("pane %s STILL holds the text in its composer after %d "
                    "read(s) — typed, never submitted" % (handle, n))
        # A composer that was READ and still holds our text is a PROVEN
        # non-delivery; anything else is an admission that we could not tell,
        # and the two must not share a return value.
        return (NOT_DELIVERED if proven_held else UNKNOWN), last

    def retry_held_submission(self, handle, text, attempts=2, backoff=1.0,
                              reads=None, interval=None, on_attempt=None,
                              on_observation=None, before_attempt=None,
                              prepare_attempt=None, admit=None):
        """Submit a PROVEN Helm-owned held composer using bare Enter only.

        Every attempt starts with a fresh exact read. This is the safety boundary:
        unreadable, wrapped, collapsed, cleared, or modified text spends ZERO
        keystrokes. The text is never retyped. Successful Enter calls are checked
        with the same bounded pane-advance proof as the initial submit.

        on_observation receives each exact-read tri-state; the recovery owner
        alone changes durable persistence. After each exact match,
        prepare_attempt finishes blocking audit/callback work BEFORE the final
        before_attempt admission check. A non-None refusal returns UNKNOWN
        without spending that attempt. on_attempt then accounts for entry into
        the send path: admission callers must keep it nonblocking (no I/O or
        outer callbacks). All hooks are optional for existing callers. A
        refused retry cannot turn an attempted submission into a successful delivery.

        `admit` IS THE ACT DOOR FOR EACH RECOVERY ENTER, and when it is given
        it replaces `before_attempt`: after the blocking preparation, the door
        prepares the caller's authorization, takes a FRESH exact read as its
        proof, validates the authorization without blocking, and only then is
        the Enter spent -- per attempt, because attempts are separated by a
        backoff that is long enough for the reason to lapse.
        """
        last = "pane %s recovery was not attempted" % handle

        def exact_read():
            try:
                tail = self.read(
                    handle, limit=SUBMIT_READ_LIMIT,
                    timeout=SUBMIT_READ_TIMEOUT_S)
            except (HarnessError, OSError) as e:
                return False, (UNKNOWN, ("pane %s recovery read failed before "
                                         "Enter: %s" % (handle, e)))
            exact = composer_exactly_holds(tail, text)
            if on_observation is not None:
                on_observation(exact)
            if exact is None:
                return False, (UNKNOWN, ("pane %s recovery refused — the full "
                                         "current composer could not be "
                                         "identified exactly" % handle))
            if not exact:
                return False, (UNKNOWN, ("pane %s recovery refused — current "
                                         "composer does not exactly equal "
                                         "Helm's recorded text" % handle))
            return True, None

        for n in range(1, max(1, attempts) + 1):
            if n > 1 and backoff > 0:
                time.sleep(backoff * (2 ** (n - 2)))
            held, refusal = exact_read()
            if not held:
                return refusal
            if prepare_attempt is not None:
                prepare_attempt(n)

            def enter(timeout=None, n=n):
                # Entry into the send path is accounted on the far side of
                # the final capture, immediately before the keystroke.
                if on_attempt is not None:
                    on_attempt(n)
                return self._press_enter(handle, timeout)
            if admit is not None:
                admitted, got = self._through_door(
                    "recovery-attempt", admit, exact_read,
                    self._final_exact(handle, text), enter)
                if not admitted:
                    if isinstance(got, DoorRefusal):
                        return UNKNOWN, got
                    return got
                enter_err, receipt = got
            else:
                if before_attempt is not None:
                    refusal = before_attempt()
                    if refusal is not None:
                        return UNKNOWN, refusal
                enter_err, receipt = enter(act_send_s()), None
            state, proof = self._verify_after_enter(
                handle, text, enter_err, reads=reads, interval=interval)
            proof = "%s%s" % (proof, act_note(receipt))
            last = "bare Enter recovery %d: %s" % (n, proof)
            if state != NOT_DELIVERED:
                return state, last
        return NOT_DELIVERED, last

    def _final_exact(self, handle, text):
        """The recovery door's LAST observation: one bounded read whose full
        composer must still equal Helm's recorded text exactly."""
        def final(timeout):
            try:
                tail = self.read(handle, limit=SUBMIT_READ_LIMIT,
                                 timeout=timeout)
            except (HarnessError, OSError) as e:
                return None, ("pane %s could not be read at the final "
                              "capture: %s" % (handle, e))
            exact = composer_exactly_holds(tail, text)
            if exact is None:
                return None, ("pane %s composer could not be identified "
                              "exactly at the final capture" % handle)
            if exact is not True:
                return False, ("pane %s composer no longer exactly held "
                               "Helm's text at the final capture" % handle)
            return True, ""
        return final

    def _field(self, obj, dotted):
        """Walk result.a.b; a miss is a loud HarnessError naming the path —
        a metaharness upgrade that moves a field must never fail silent."""
        cur = obj
        for part in dotted.split("."):
            cur = cur.get(part) if isinstance(cur, dict) else None
        if cur is None:
            raise HarnessError("%s: reply missing result.%s" % (self.name, dotted))
        return cur


# ---------------------------------------------------------------------------
# per-seat HOME worktree — the isolation seam, LAYER 1 of the coordination
# substrate: every seat works in a checkout of its own
# ---------------------------------------------------------------------------
# THE BUG this cures: a spawned seat's cwd DEFAULTED to wherever the operator
# stood — the shared main checkout — so nine fleet agents edited, built and
# stashed the SAME tree. Dirty main blocks every land, seats collide, and
# `git stash` on a shared checkout is not swarm-safe (Ember's law). A seat now
# gets a long-lived private checkout keyed by its own name.
#
# ONE method, THREE metaharness cases, IDENTICAL contract:
#     ensure_home_worktree(seat, repo_root, base="main") -> absolute path
# create-or-REUSE (idempotent — a second spawn of the same seat returns the
# same path), and deliberately NEVER `worktree lock`ed: `helm work claim` locks
# a LANE room because there the lock IS the task lease, but a seat HOME is
# long-lived and a lock would make every prune/remove/gc refuse forever.
#
# The seam above the three implementations is identical, which is the whole
# point: helm is metaharness-AGNOSTIC. herdr honors helm's deterministic path
# natively (`--path` + adopt); orca's worktree CLI cannot (create-only,
# `--name`, no adopt), so orca rides the helm-native floor plus a fail-open
# visibility pass.

SEAT_HOME_DIRNAME = "seats"


def seat_worktree_path(repo_root, seat):
    """`<repo>-wt/seats/<seat>` — the deterministic per-seat HOME. The pure
    function IS the registry key (the lane_path law). It reuses the ONE sibling
    container helm already uses for lane rooms (`<repo>-wt/`, already
    automap-folded), one level down under `seats/` so a seat HOME can never
    collide with a lane room that happens to share a seat's name."""
    return os.path.join(repo_root.rstrip(os.sep) + "-wt", SEAT_HOME_DIRNAME,
                        seat)


def seat_branch(seat):
    """The seat's own long-lived branch — `seat/<seat>`, a namespace disjoint
    from `lane/<lane>` so lane gc/merge logic never mistakes a seat HOME for a
    task room."""
    return "seat/" + seat


def find_repo_root(path=None):
    """The MAIN checkout root for `path` (linked worktrees fold to it via
    --git-common-dir), or None outside a checkout. A REUSE of work's resolver,
    never a second derivation. `path=None`/'' answers None rather than reaching
    for os.getcwd() — a deleted cwd must not raise here."""
    if not path:
        return None
    from .work._lanes import find_root
    return find_root(path)


def _check_seat_name(seat):
    """A seat name becomes a path segment AND a branch name; anything that
    could escape `<repo>-wt/seats/` or be read as a git flag is refused
    loudly rather than silently landing a worktree somewhere else."""
    if not seat or not isinstance(seat, str) or seat != seat.strip() \
            or seat.startswith("-") or seat in (".", "..") \
            or os.sep in seat or (os.altsep and os.altsep in seat):
        raise HarnessError("unsafe seat name %r for a home worktree" % (seat,))


def home_drift(repo_root, seat, base="main"):
    """(behind, ahead) commits between a seat's HOME branch and trunk, or None
    when git cannot say. Pure MEASUREMENT — it moves nothing.

    Worktrees share one object store, so this is answerable from the shared
    checkout without entering (or disturbing) the seat's tree at all."""
    from .work._lanes import _git
    branch = seat_branch(seat)
    rc, out, _err = _git(repo_root, "rev-list", "--left-right", "--count",
                         "%s...%s" % (base, branch))
    if rc != 0 or not out:
        return None            # a pruned base, an unborn branch, a broken repo
    parts = out.split()
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        return None            # unparseable is UNKNOWN, never zero
    return int(parts[0]), int(parts[1])


def report_home_drift(repo_root, seat, base="main"):
    """Say it out loud when a REUSED seat home is behind trunk, and do NOTHING
    else. Returns the drift for callers that want it.

    THE BUG THIS CLOSES: a seat home is provisioned
    once, at whatever trunk happened to be, and the reuse leg returned it with no
    further word. The first repair told every author to rebase before working.
    Three different seats then obeyed, gated, and reported — while another land
    moved trunk between their check and report. A true author-side check cannot
    stay true; only the integrator's ff-only landing decision is authoritative.

    WHY IT ONLY REPORTS: auto-rebasing is the one thing this must not do. A
    rebase can conflict, and a tree helm does not own is not helm's to resolve —
    the same law that makes `git stash` unsafe on a shared checkout. Surface the
    drift, name the remedy, let the owner run it. Fail-open in both directions:
    if git cannot answer, nothing is claimed and nothing is printed, because a
    measurement we could not take is not a measurement of zero."""
    drift = home_drift(repo_root, seat, base)
    if not drift or not drift[0]:
        return drift
    behind, ahead = drift
    print("helm seat: NOTE %s's home worktree is %d commit%s BEHIND %s%s — "
          "DO NOT rebase speculatively as the author. Work from the recorded "
          "base; the reviewer binds that exact tip, then the integrator chooses "
          "landing order, rebases the chain, and runs the final exact-tree gate "
          "once. An author-side freshness check can become stale before it is "
          "reported."
          % (seat, behind, "" if behind == 1 else "s", base,
             " (and %d ahead)" % ahead if ahead else ""), file=sys.stderr)
    return drift


def _native_home_worktree(repo_root, seat, base="main"):
    """The canonical FLOOR — helm-native `git worktree add`, no metaharness
    involved. Same shape as work/_claims.py's room provisioning, MINUS the
    `worktree lock` (see the no-lock law above) and minus the lease: a seat
    HOME is not a task lease."""
    from .work._gc import _base, _has_branch
    from .work._lanes import _git, worktrees
    _check_seat_name(seat)
    path = seat_worktree_path(repo_root, seat)
    branch = seat_branch(seat)
    registered = {os.path.realpath(w["path"]) for w in worktrees(repo_root)}
    if os.path.realpath(path) in registered:
        if os.path.isdir(path):
            report_home_drift(repo_root, seat, base)
            return path                 # REUSE — the idempotent leg
        # The admin record outlived the directory (a reseed/rm -rf). Prune
        # only drops records whose checkout is already gone, so this cannot
        # touch a live sibling room.
        _git(repo_root, "worktree", "prune")
    elif os.path.isdir(path):
        raise HarnessError(
            "%s already exists but is not a registered worktree of %s — "
            "inspect it by hand rather than have helm adopt unknown bytes"
            % (path, repo_root))
    if _git(repo_root, "rev-parse", "--verify", "-q", base)[0] != 0:
        base = _base(repo_root)    # main, else the shared checkout's HEAD
    args = (["worktree", "add", path, branch] if _has_branch(repo_root, branch)
            else ["worktree", "add", "-b", branch, path, base])
    # HELM_WORK_CLAIM: the shared-tree ref-guard cannot tell `worktree add -b`
    # apart from a forbidden `checkout -b` — both run with cwd AND toplevel in
    # the shared checkout. So the sanctioned creator announces itself, scoped
    # to this ONE call rather than the process env (work/_claims.py:36).
    rc, _out, err = _git(repo_root, *args, env={"HELM_WORK_CLAIM": "1"})
    if rc != 0:
        raise HarnessError("git worktree add failed for seat %s: %s"
                           % (seat, err or "unknown git error"))
    return path


def ensure_home_worktree(seat, repo_root, base="main"):
    """The FLOOR implementation (headless / HELM_METAHARNESS=none / any adapter
    that does not implement its own): create-or-reuse the seat's private
    checkout with helm-native git and return its absolute path. Every adapter's
    method answers the SAME deterministic path for the same inputs."""
    return _native_home_worktree(repo_root, seat, base=base)


class OrcaAdapter(_CLIAdapter):
    """orca's terminal CLI plus its read-only pane-remint runtime method.

    Public pane verbs use the CLI. `resolve_pane` calls the same local,
    authenticated `terminal.resolvePane` RPC Orca's own orchestration CLI uses
    when a long-lived shell's handle has gone stale. The auth token is read from
    Orca's private runtime metadata and sent only over its Unix socket; it never
    enters argv, logs, errors, or Helm state.
    """
    name = "orca"
    bin = "orca"
    # `_pane_row` maps orca's `worktreePath` onto every row this adapter emits,
    # from both the CLI and the RPC leg, so the pane-binding question is
    # answerable here. Measured live 2026-07-30: a real lane room answered with
    # its pane handle, a non-room answered [].
    reports_pane_worktree = True

    @staticmethod
    def _user_data_path():
        explicit = os.environ.get("ORCA_USER_DATA_PATH")
        if explicit:
            return explicit
        if sys.platform == "darwin":
            return os.path.expanduser("~/Library/Application Support/orca")
        if os.name == "nt":
            return os.path.join(os.environ.get("APPDATA") or "", "orca")
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
        return os.path.join(base, "orca")

    def _runtime_call(self, method, params, timeout=RPC_TIMEOUT_S):
        # KILL-SWITCH, checked before the socket is even looked up. The RPC legs
        # reach a REAL daemon holding the owner's live panes and project list,
        # and `_user_data_path()` falls back to $HOME/.config/orca — which the
        # test suite does NOT redirect (tests/_tmphome.py moves HELM_HOME only).
        # So any code path that adopts or lists must be switchable off from the
        # environment, or a unit test silently talks to the live workspace.
        if (os.environ.get("HELM_ORCA_RPC") or "").strip().lower() in \
                ("off", "0", "none", "false"):
            raise HarnessError("orca runtime RPC disabled (HELM_ORCA_RPC)")
        path = os.path.join(self._user_data_path(), "orca-runtime.json")
        # ABSOLUTE, AND HERE RATHER THAN AT THE TOP: test_harness's deadline
        # arms exec this module's source outside the package, where a relative
        # import has no parent, and they drive this very method.
        from helm import pk
        try:
            with pk.open_regular(path) as f:
                meta = json.load(f)
        except (OSError, ValueError) as e:
            raise HarnessError("orca runtime metadata unavailable: %s" % e)
        transports = meta.get("transports")
        if not isinstance(transports, list):
            transports = [meta.get("transport")]
        transport = next((t for t in transports if isinstance(t, dict) and
                          t.get("kind") == "unix" and t.get("endpoint")), None)
        token = meta.get("authToken")
        if transport is None or not token or not hasattr(socket, "AF_UNIX"):
            raise HarnessError("orca runtime has no usable local Unix transport")
        request_id = str(uuid.uuid4())
        request = {"id": request_id, "authToken": token,
                   "method": method, "params": params}
        try:
            # ONE WALL-CLOCK DEADLINE GOVERNS EVERY BLOCKING STEP.
            # settimeout() bounds a SINGLE operation, so connect, sendall and
            # each recv formerly drew a FULL budget apiece — 3x the declared
            # timeout before the loop even started, which is how a 5s RPC
            # ceiling blew a 10s hook. And the reply loop `continue`s on every
            # _keepalive, so a daemon trickling keepalives reset the effective
            # clock each pass: every operation inside budget, total wall time
            # unbounded. MEASURED by a hookprobe on
            # its first live run — inject caught at 9.02s asleep in
            # wchan=unix_stream_data_wait, blocked reading this socket.
            deadline = time.monotonic() + timeout

            def _left(where):
                """Seconds remaining, or the NAMED expiry. Every exit from the
                deadline carries the same error text and the step that hit it,
                so a stall is never mistakable for an empty answer."""
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise HarnessError(
                        "orca runtime rpc %s: deadline exceeded after %.1fs "
                        "at %s (daemon held the connection open)"
                        % (method, timeout, where))
                return remaining

            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
                try:
                    conn.settimeout(_left("connect"))
                    conn.connect(transport["endpoint"])
                    conn.settimeout(_left("send"))
                    conn.sendall((json.dumps(request, separators=(",", ":")) +
                                  "\n").encode("utf-8"))
                except socket.timeout:
                    raise HarnessError(
                        "orca runtime rpc %s: deadline exceeded after %.1fs "
                        "at connect/send (daemon held the connection open)"
                        % (method, timeout))
                buf = b""
                while len(buf) <= 1024 * 1024:
                    conn.settimeout(_left("recv"))
                    try:
                        chunk = conn.recv(65536)
                    except socket.timeout:
                        # The socket budget WAS the remaining budget, so a
                        # timeout here IS the deadline by construction — no
                        # epsilon race on whether _left() sees <= 0.
                        # settimeout was set to REMAINING, so a timeout here
                        # means the deadline elapsed — _left raises the NAMED
                        # expiry. Without this the socket's generic "timed out"
                        # escapes to the outer handler and the caller cannot
                        # tell a daemon stall from any other socket failure,
                        # which is finding (3) left half-done: naming the error
                        # at the RAISE SITE is not the same as naming it at
                        # every EXIT.
                        raise HarnessError(
                            "orca runtime rpc %s: deadline exceeded after "
                            "%.1fs at recv (daemon held the connection open)"
                            % (method, timeout))

                    if not chunk:
                        break
                    buf += chunk
                    while b"\n" in buf:
                        _left("frame")      # buffered frames are bounded too:
                        # a peer can pack many keepalives into ONE recv, so an
                        # unbounded inner loop reintroduces the same stall
                        # without ever blocking on the socket again.
                        raw, buf = buf.split(b"\n", 1)
                        if not raw.strip():
                            continue
                        reply = json.loads(raw.decode("utf-8"))
                        if reply.get("_keepalive"):
                            continue
                        if reply.get("id") != request_id:
                            raise HarnessError(
                                "orca runtime rpc %s: response id mismatch"
                                % method)
                        runtime_id = (reply.get("_meta") or {}).get("runtimeId")
                        if runtime_id and meta.get("runtimeId") and \
                                runtime_id != meta["runtimeId"]:
                            raise HarnessError(
                                "orca runtime changed during rpc %s" % method)
                        if reply.get("ok") is not True:
                            err = reply.get("error") or {}
                            msg = err.get("message") if isinstance(err, dict) else None
                            raise HarnessError(
                                "orca runtime rpc %s: %s"
                                % (method, msg or "error reply without a message"))
                        return reply.get("result") or {}
        except HarnessError:
            raise
        # Every error line NAMES THE METHOD — this client outgrew its
        # pane-resolution birth (accounts.list rides it too), and a hardcoded
        # "pane resolution failed" label sent a live accounts.list timeout
        # diagnosis chasing panes (measured 2026-08-04). The failure class
        # stays in %s (e.g. "timed out", ECONNREFUSED).
        except (OSError, ValueError, TypeError) as e:
            raise HarnessError("orca runtime rpc %s failed: %s" % (method, e))
        raise HarnessError("orca runtime rpc %s returned no reply" % method)

    def resolve_pane(self, pane_key):
        terminal = (self._runtime_call(
            "terminal.resolvePane", {"paneKey": pane_key}).get("terminal") or {})
        return {"handle": terminal.get("handle"),
                "pty_id": terminal.get("ptyId"),
                "tab_id": terminal.get("tabId"),
                "leaf_id": terminal.get("leafId")}

    # -- the FAIL-OPEN rung over the same socket -------------------------------
    # Every adopt/see leg reads orca opportunistically: orca absent, daemon
    # down, socket stale, or a garbage reply must degrade to helm's own
    # behaviour and NEVER crash a helm verb. `_runtime_call` raises; this
    # returns (result, error_text) so a caller can render an honest one-liner
    # without a try/except at every site. It is the only rung the adoption
    # seam uses.

    def rpc(self, method, params, timeout=RPC_TIMEOUT_S):
        """(result, None) on success; (None, "one honest line") on ANY failure.

        Deliberately swallows every exception class the socket path can raise
        — a metaharness is an OPTIONAL companion, so its absence is a normal
        state, not an error the caller must handle."""
        try:
            return self._runtime_call(method, params, timeout=timeout), None
        except HarnessError as e:
            return None, str(e)
        except Exception as e:                    # noqa: BLE001 — fail-open law
            return None, "orca rpc %s: %s" % (method, e)

    def panes(self):
        """([row], error) — every pane orca currently holds, over the RPC
        rather than the CLI. Same row shape as `list()` (one normalizer, see
        `_pane_row`), so a caller can take either source. The RPC carries the
        same fields the CLI does plus `lastOutputAt`, and costs no subprocess.

        FAIL-OPEN: an unavailable orca answers ([], reason) — never an
        exception, and never a bare [] that a caller could mistake for
        "orca is running and has no panes"."""
        result, err = self.rpc("terminal.list", {})
        if err:
            return [], err
        return [_pane_row(t) for t in (result.get("terminals") or [])], None

    def adopt_worktree(self, repo_root, path):
        """Make a helm-CREATED checkout visible on orca's board — (ok, detail).

        Orca's own `orca worktree` CLI has no adopt verb and no --path, so this
        is RPC-only (`repo.update`). The authoritative param shape, read from
        the installed app bundle rather than guessed, is
            {"repo": "<selector>", "updates": {...}}
        with the selector one of `id:`/`name:`/`path:` — NOT a flat `repoId`,
        which answers `invalid_argument: Missing repo selector`.

        Two updates do the work together: `externalWorktreeVisibility: "show"`
        turns on external-worktree discovery for the repo, and
        `importedExternalWorktreePaths` names this checkout explicitly. The
        imported list is READ-MODIFY-WRITTEN, never overwritten — a blind write
        would silently un-adopt every worktree adopted before us.

        FAIL-OPEN in every leg: visibility is a nicety, the checkout is the
        contract. The one case that is reported rather than attempted is a repo
        orca holds as a FOLDER context (see below) — silently reclassifying a
        project in the owner's sidebar is not helm's call to make."""
        repos, err = self.rpc("repo.list", {})
        if err:
            return False, err
        want = os.path.realpath(repo_root)
        row = next((r for r in (repos.get("repos") or [])
                    if isinstance(r, dict) and r.get("path")
                    and os.path.realpath(r["path"]) == want), None)
        if row is None:
            return False, "orca does not have %s registered as a project" % repo_root
        if row.get("kind") != "git":
            # MEASURED 2026-07-24, and it is the reason a helm worktree cannot
            # simply be flipped visible: orca holds helm as kind="folder", and a
            # folder context has no externalWorktreeVisibility field at all —
            # orca never enumerates git worktrees for it (git reported 102
            # worktrees for helm; orca's worktree.list knew 1). The schema DOES
            # accept updates.kind="git", but reclassifying a project rearranges
            # the owner's orca sidebar, so helm reports it and stops.
            return False, ("orca holds %s as a %r context, not a git repo — "
                           "external-worktree discovery never runs for it, so "
                           "adoption needs the project reclassified to git "
                           "first (owner-visible sidebar change; helm will not "
                           "do it silently)" % (repo_root, row.get("kind")))
        imported = [p for p in (row.get("importedExternalWorktreePaths") or [])
                    if isinstance(p, str)]
        if not any(os.path.realpath(p) == os.path.realpath(path)
                   for p in imported):
            imported = imported + [path]
        _, err = self.rpc("repo.update", {
            "repo": "id:" + str(row.get("id")),
            "updates": {"externalWorktreeVisibility": "show",
                        "importedExternalWorktreePaths": imported}})
        if err:
            return False, err
        return True, "adopted into orca (visibility show, %d imported path%s)" \
            % (len(imported), "" if len(imported) == 1 else "s")

    def ensure_home_worktree(self, seat, repo_root, base="main"):
        """Orca's `worktree` CLI is create-ONLY and path-BLIND — `create
        --name` (orca picks the directory), no `--path`, no adopt verb
        (re-verified against the installed CLI 2026-07-24). Letting orca create
        the seat HOME would scatter homes into orca-chosen directories and lose
        helm's deterministic `<repo>-wt/seats/<seat>`, so the checkout is
        helm-native and orca visibility is a best-effort, FAIL-OPEN pass on top.

        Adoption now rides the DAEMON RPC (`adopt_worktree` -> `repo.update`),
        which is the only surface that can adopt at all. It replaces an earlier
        `orca worktree set --worktree path:<p>` attempt that could never work:
        `set` can only select a worktree orca ALREADY knows, and an unadopted
        helm checkout is exactly the case where it does not, so it answered
        selector_not_found every time.

        WHY THE EARLIER READING OF THAT FAILURE WAS WRONG (measured 2026-07-24):
        it was taken as "orca does not auto-discover foreign worktrees". The
        real rule is per-repo. Comparing `git worktree list` against orca's
        `worktree.list` across all 22 registered projects: web-app
        (visibility "show") -> orca knew 10 of 10; example-project ("hide") -> 0 of 10;
        api-service ("hide") -> 2 of 20. Orca discovers external worktrees fine, and
        `externalWorktreeVisibility` is what gates it. helm is the harder case
        for a different reason (a folder context — see `adopt_worktree`).

        Either way the CONTRACT is unchanged: the seat gets and runs in its own
        deterministic checkout, and orca board presence is a fail-open nicety
        layered on top."""
        path = _native_home_worktree(repo_root, seat, base=base)
        self.adopt_worktree(repo_root, path)   # (ok, detail) — nicety, ignored
        return path                            # here; `seat adopt` reports it

    def spawn(self, command, title=None, cwd=None):
        # NOTE (measured 2026-07-24, kept as a signpost rather than as code):
        # `orca terminal create` exposes only --worktree/--title/--command/
        # --focus, while the daemon's TerminalCreateParams also accepts an `env`
        # record — the same CLI-is-the-thin-surface asymmetry as worktree adopt.
        # helm does NOT need it: a pane's env identity rides the minted resume
        # script (sessions.mint_resume_script(env=...)), which works identically
        # on all three metaharness cases instead of only on orca.
        # `--worktree path:X` is an Orca-registry selector, not a filesystem
        # cwd. Normal seat homes are adopted before spawn, but a selector can
        # still miss for an explicit cwd, a resumed lane, fail-open adoption, or
        # discovery lag. Keep the visible pane and let the command carry cwd in
        # that one narrow case; every other Orca failure remains loud.
        def create(with_selector):
            args = ["terminal", "create"]
            if cwd and with_selector:
                args += ["--worktree", "path:" + cwd]
            if title:
                args += ["--title", title]
            cmd = command
            if cwd and not with_selector:
                cmd = "cd %s && exec sh -lc %s" % (
                    shlex.quote(cwd), shlex.quote(command))
            args += ["--command", cmd, "--json"]
            return self._field(self._run(args), "terminal.handle")

        if not cwd:
            return create(False)
        try:
            return create(True)
        except HarnessError as exc:
            if exc.code != "selector_not_found":
                raise
            return create(False)

    def list(self):
        r = self._run(["terminal", "list", "--json"])
        return [_pane_row(t) for t in (r.get("terminals") or [])]

    def read(self, handle, limit=3000, timeout=60):
        r = self._run(["terminal", "read", "--terminal", handle,
                       "--limit", str(limit), "--json"], timeout=timeout)
        terminal = r.get("terminal") or {}
        tail = list(terminal.get("tail") or [])
        # Orca exposes the current input separately from screen rows. Claude
        # Code 2.1.266 can render that draft on the box rule with no `❯`, so the
        # structured field is the stronger owner and is canonicalized onto the
        # uniform text seam every composer consumer already reads. Multiline
        # drafts stay opaque: first-line equality cannot authorize bare Enter
        # when a human may have changed a later line.
        if isinstance(terminal.get("draft"), str):
            draft = terminal["draft"]
            body = "[Pasted text from structured draft]" \
                if "\n" in draft else draft
            tail.append("❯\xa0" + body if body else "❯")
        return "\n".join(tail)

    def send(self, handle, text, enter=True, timeout=60):
        args = ["terminal", "send", "--terminal", handle, "--text", text]
        if enter:
            args.append("--enter")
        args.append("--json")
        self._run(args, timeout=timeout)

    def rename(self, handle, title=None):
        """Set a pane's tab title -> the title orca STORED, or None on reset.

        ORCA-ONLY ON PURPOSE, and declared by its absence: herdr exposes no
        equivalent, and a base-class stub that raised would make "this
        metaharness cannot label a tab" indistinguishable from "the rename
        failed". Callers ask `getattr(adapter, "rename", None)` and degrade.

        `title=None` omits `--title`, which is orca's documented reset to the
        AUTO-generated title — the one way to hand a pane back to its host.

        RETURNS THE ECHO, never a bare True. The reply carries
        `result.rename.title`, which is what orca actually stored; a caller
        that wants the effect rather than the absence of a complaint compares
        against it, and a reply that lost the field is a LOUD HarnessError
        (`_field`'s law) rather than a silent success. A RESET has no
        requested title to echo, so it answers None and asserts nothing.
        """
        args = ["terminal", "rename", "--terminal", handle]
        if title:
            args += ["--title", title]
        args.append("--json")
        reply = self._run(args)
        return self._field(reply, "rename.title") if title else None

    def stop(self, handle):
        return self._run(["terminal", "close", "--terminal", handle, "--json"])


class HerdrAdapter(_CLIAdapter):
    """herdr's socket-API CLI (JSON by default — no --json flag; shapes from
    the installed CLI's help + `herdr api schema`, protocol 16, 2026-07-21):
    agent start NAME [--cwd P] --no-focus -- argv…  -> result.agent.pane_id
    pane list -> result.panes[] (pane_id/label/agent_status)
    pane read H --source recent --lines N -> result.read.text
    pane run H CMD (text+Enter) / pane send-text H TEXT (literal, no Enter)
    pane close H. Handles are pane ids — pane verbs need them and agent
    verbs accept them ("legacy pane ids")."""
    name = "herdr"
    bin = "herdr"

    def ensure_home_worktree(self, seat, repo_root, base="main"):
        """herdr owns the richest worktree surface of the three, and it is the
        proof helm's seam is not orca-shaped: `worktree create` takes an
        explicit `--path` (so helm's deterministic location WINS) and
        `worktree open --path` ADOPTS an existing checkout.

        Verified against the installed CLI's own help, 2026-07-24:
          worktree list   [--workspace ID | --cwd PATH] [--json]
          worktree create [--cwd PATH] [--branch N] [--base REF] [--path P]
                          [--label T] [--focus|--no-focus] [--json]
          worktree open   [--cwd PATH] (--path P | --branch N) [--label T]
                          [--no-focus] [--json]

        Reuse is checked through herdr's OWN list (which reads the repo's git
        registry), so a home provisioned by any leg is recognised by every
        other. HELM_WORK_CLAIM rides the CLI env for the create; herdr does the
        git work in its DAEMON, whose environment we cannot reach, so a
        ref-guard refusal (or any other create failure) falls back to the
        helm-native floor and then ADOPTS the result via `worktree open`. The
        deterministic path holds on either leg."""
        _check_seat_name(seat)
        path = seat_worktree_path(repo_root, seat)
        label = "helm seat " + seat
        if self._knows_worktree(repo_root, path) and os.path.isdir(path):
            return path                        # REUSE — the idempotent leg
        try:
            self._run(["worktree", "create", "--cwd", repo_root,
                       "--branch", seat_branch(seat), "--base", base,
                       "--path", path, "--label", label, "--no-focus",
                       "--json"], env={"HELM_WORK_CLAIM": "1"})
            return path
        except HarnessError:
            _native_home_worktree(repo_root, seat, base=base)
        try:
            self._run(["worktree", "open", "--cwd", repo_root,
                       "--path", path, "--label", label, "--no-focus",
                       "--json"])
        except HarnessError:
            pass        # visibility is a nicety; the checkout is the contract
        return path

    def _knows_worktree(self, repo_root, path):
        """Does herdr's registry already carry a checkout at `path`? An
        unavailable herdr answers False — the caller then attempts a create,
        whose own failure falls through to the helm-native floor."""
        try:
            rows = self._run(["worktree", "list", "--cwd", repo_root,
                              "--json"]).get("worktrees") or []
        except HarnessError:
            return False
        real = os.path.realpath(path)
        return any(isinstance(w, dict) and w.get("path")
                   and os.path.realpath(w["path"]) == real for w in rows)

    def spawn(self, command, title=None, cwd=None):
        name = title or ("helm-%d" % int(time.time()))
        args = ["agent", "start", name]
        if cwd:
            args += ["--cwd", cwd]
        # agent start wants an argv, not a command string: wrap in sh -lc so
        # the uniform command-string contract holds.
        args += ["--no-focus", "--", "sh", "-lc", command]
        agent = self._field(self._run(args), "agent")
        handle = agent.get("pane_id") or agent.get("terminal_id")
        if not handle:
            raise HarnessError("herdr: agent_started reply carries no pane_id")
        return handle

    def list(self):
        # NO `worktree` KEY, which is why `reports_pane_worktree` stays False
        # for herdr: `pane list` rows are mapped to pane_id/label/agent_status
        # and nothing here says which directory a pane sits in. The flag is a
        # statement about THIS row builder, not a claim that herdr's CLI cannot
        # answer — the daemon was down when it was last probed (2026-07-30), so
        # it is recorded as unproven. Add the field, flip the flag, in one
        # commit; until then `helm work gc` refuses under herdr and says so,
        # which is the safe direction.
        r = self._run(["pane", "list"])
        return [{"handle": p.get("pane_id"),
                 "title": p.get("label") or "",
                 "status": p.get("agent_status") or "unknown"}
                for p in (r.get("panes") or [])]

    def read(self, handle, limit=3000, timeout=60):
        r = self._run(["pane", "read", handle, "--source", "recent",
                       "--lines", str(limit)], timeout=timeout)
        return (r.get("read") or {}).get("text") or ""

    def send(self, handle, text, enter=True, timeout=60):
        # pane run = text + Enter; pane send-text = literal keystrokes.
        self._run(["pane", "run", handle, text] if enter
                  else ["pane", "send-text", handle, text], timeout=timeout)

    def stop(self, handle):
        self._run(["pane", "close", handle])


ADAPTERS = {"orca": OrcaAdapter, "herdr": HerdrAdapter}


def detect(env=None, which=None):
    """The best available adapter instance, or None when no metaharness is
    installed. Order: HELM_METAHARNESS=orca|herdr|none is the explicit
    override (none = pane ops off even with both installed); inside a herdr
    session (HERDR_ENV set) herdr wins — panes spawn where the operator
    already lives; otherwise orca first (the recommended companion), herdr
    next. env/which are test seams only."""
    env = os.environ if env is None else env
    which = which or shutil.which
    override = (env.get("HELM_METAHARNESS") or "").strip().lower()
    if override in ("none", "off"):
        return None
    if override in ADAPTERS:
        path = which(ADAPTERS[override].bin)
        return ADAPTERS[override](path) if path else None
    order = ("herdr", "orca") if env.get("HERDR_ENV") else ("orca", "herdr")
    for name in order:
        path = which(ADAPTERS[name].bin)
        if path:
            return ADAPTERS[name](path)
    return None
