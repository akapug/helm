#!/usr/bin/env python3
"""helm cell — the OPTIONAL dregg-node leg.

helm rides dregg (the substrate) + cv (recall) ONLY. It never depends on the
meld binary/project — meld is a sibling that also rides dregg. Premise
attestation is helm-NATIVE (a stdlib append-only hash chain; see premise.py);
this module is only the OPTIONAL external checkpoint + a few node-liveness
reads, all over stdlib urllib. No 'meld' binary is needed for attestation.

Env law (env2 new-with-old-fallback): HELM_* is canonical; the legacy MELD_*
name is accepted READ-ONLY as a migration fallback (via home.env):

  HELM_NODE_URL        (MELD_NODE_URL)        node, default http://127.0.0.1:8899
  HELM_NODE_TOKEN      (MELD_NODE_TOKEN)      optional bearer for a gated node
  HELM_NODE_PASSPHRASE (MELD_NODE_PASSPHRASE) reserved (node unlock, operator side)

OPTIONAL a2a transport: a small explicit-opt-in escape hatch (`helm cell
<verb>` + chat's signed rows) can still drive a co-located cell binary when
HELM_CELL_BIN (legacy MELD_CELL_BIN) points at one, or when the operator's
`signer.env` names one — the file is a way of being TOLD, and being told is the
whole content of the rule. A real env var still WINS; the file fills the gap.
It is NEVER auto-resolved (no PATH probe, no sibling-build guess) and NEVER
used for attestation; with neither set it degrades cleanly. helm is fully
functional without it.

Import-safe, stdlib-only.
"""
import json
import os
import re
import stat
import subprocess
import sys

from . import home

DEFAULT_NODE_URL = "http://127.0.0.1:8899"
# dregg charges a per-turn computron fee and REFUSES a turn whose declared fee
# budget is below its real cost — so an ECONOMIC turn must declare >= that cost.
# Historically that was true of EVERY turn (EmitEvent cost ~100; dregg's
# DEFAULT_ANCHOR_FEE is 1000), so anchors declared a non-zero fee. dregg's
# Stage B "coordination-turn class" (leash, not ledger) now WAIVES the admission
# charge for EmitEvent-only turns with no balance_change on an opting-in node:
# such a turn ADMITS and COMMITS at fee = 0, so it never drains a cell and never
# needs a faucet grant (the throttle that degraded signed transport to
# [unsigned]). helm therefore declares fee = 0 for the coordination class
# (`is_coordination_actions`) and keeps DEFAULT_ANCHOR_FEE only for turns that
# carry economic effects. Both bounds are env-overridable.
DEFAULT_ANCHOR_FEE = 1000
# The declared fee for a COORDINATION turn (EmitEvent-only, no balance_change).
# 0 = ride the dregg coordination-exempt class free. Override with
# HELM_NODE_COORD_FEE for a node that has NOT opted into the exempt class (there
# the anchor simply fails open — the native record is still the proof).
DEFAULT_COORD_FEE = 0
# The OPTIONAL anchor rides the capture path best-effort: keep the bound SMALL so
# a slow/hung node never noticeably delays capture (the native record is the proof).
DEFAULT_ANCHOR_TIMEOUT = 2


def node_url():
    return (home.env("NODE_URL") or DEFAULT_NODE_URL).rstrip("/")


def anchor_fee():
    """The per-anchor computron fee budget the submit stamps. dregg charges the
    EmitEvent a real cost and rejects an underfunded turn, so this is >= dregg's
    DEFAULT_ANCHOR_FEE. Override with HELM_NODE_ANCHOR_FEE (legacy MELD_*)."""
    v = home.env("NODE_ANCHOR_FEE")
    try:
        return int(v) if v not in (None, "") else DEFAULT_ANCHOR_FEE
    except (TypeError, ValueError):
        return DEFAULT_ANCHOR_FEE


def coord_fee():
    """The declared fee for a COORDINATION turn (EmitEvent-only, no
    balance_change) — 0 by default so the turn rides dregg's Stage B
    coordination-exempt class free (admits + commits without a faucet grant, so
    signed transport never throttles to [unsigned]). Override with
    HELM_NODE_COORD_FEE for a node that has not opted into the exempt class."""
    v = home.env("NODE_COORD_FEE")
    try:
        return int(v) if v not in (None, "") else DEFAULT_COORD_FEE
    except (TypeError, ValueError):
        return DEFAULT_COORD_FEE


def is_coordination_actions(actions):
    """True iff `actions` is the COORDINATION class dregg's `Turn::is_coordination`
    admits fee-free: non-empty, and EVERY action declares NO `balance_change` and
    carries ONLY `emit_event` effects. Any economic effect (transfer/burn/
    note_spend/create_cell/set_field/…) or any balance_change disqualifies the
    whole turn, so an economic turn is NEVER zeroed — exactly the node-side
    disqualification, mirrored client-side so the client-declared `fee` matches
    what the admission gate will charge."""
    if not actions:
        return False
    for a in actions:
        if not isinstance(a, dict):
            return False
        if a.get("balance_change") is not None:
            return False
        effects = a.get("effects") or []
        if not effects:
            return False
        if not all(isinstance(e, dict) and e.get("kind") == "emit_event"
                   for e in effects):
            return False
    return True


def turn_fee(actions):
    """The per-turn fee helm DECLARES for `actions`: 0 for the coordination
    class (`is_coordination_actions` → dregg waives the admission charge), else
    the economic `anchor_fee()`. This is the client half of the leash — the
    declared fee is what the node's admission gate meters against."""
    return coord_fee() if is_coordination_actions(actions) else anchor_fee()


def anchor_timeout():
    """The bounded best-effort anchor timeout on the capture path (small by
    design). Override with HELM_NODE_ANCHOR_TIMEOUT (legacy MELD_*)."""
    v = home.env("NODE_ANCHOR_TIMEOUT")
    try:
        return int(v) if v not in (None, "") else DEFAULT_ANCHOR_TIMEOUT
    except (TypeError, ValueError):
        return DEFAULT_ANCHOR_TIMEOUT


def profile_name(default="helm-agent"):
    """The effective identity/recording label (HELM_CELL_PROFILE >
    MELD_AGENT_PROFILE > `default`). This is a provenance LABEL only — helm's
    native attestation is not a cell signature, so this never proves a signer.
    Attestation callers pass a safer default (see premise.attest_profile)."""
    return os.environ.get("HELM_CELL_PROFILE") \
        or os.environ.get("MELD_AGENT_PROFILE") or default


# ---------------------------------------------------------------------------
# node HTTP — stdlib urllib, fail-open (down/refused/garbled -> None)
# ---------------------------------------------------------------------------

def get_json(url, timeout=4, diag=None):
    """One node GET -> parsed JSON, fail-open None. An HTTP error status is
    CLOSED before dropping — an abandoned HTTPError holds its socket and
    detonates under -W error::ResourceWarning.

    `diag`, when a dict is passed, is filled the way post_json fills it:
    `status` (an int for an HTTP error, None when nothing answered) and `body`
    (a short excerpt). A route that answers 404 WITH a JSON verdict and a route
    that does not exist at all both return None here; only the body tells them
    apart, so a caller that must not confuse the two asks for it."""
    import urllib.error
    import urllib.request
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        if diag is not None:
            try:
                body = e.read()[:4000].decode("utf-8", "replace").strip()
            except Exception:
                body = ""
            diag.update(status=e.code, body=body)
        e.close()
        return None
    except Exception:
        if diag is not None:
            diag.update(status=None, body="")
        return None


def post_json(url, payload, timeout=8, headers=None, diag=None):
    """One node POST (JSON in, JSON out), same fail-open None law as get_json.
    `headers` layers over the default Content-Type (e.g. an Authorization
    bearer for a gated node).

    `diag`, when a dict is passed, is filled with WHY None came back: `status`
    (int, for an HTTP error), `body` (a short excerpt) and `reason` (a sentence).
    The fail-open contract is unchanged and no existing caller has to adapt.

    THIS THREW AWAY THE ONE FACT THE CALLER NEEDED. `except HTTPError: return
    None` collapsed a 401, a 404, a 500 and a refused connection into the same
    value, which is why anchor() could only report "node unreachable/locked" —
    a disjunction it had no way to resolve. Live 2026-07-25: every helm premise
    attestation had been failing its external anchor with that message while the
    node was demonstrably UP (GET /api/receipts -> 200 on the same host and
    port). The real answer was HTTP 401 on POST /turn/submit, which is a
    different problem with a different fix, and the message actively pointed
    away from it — an operator reading "unreachable" checks whether the node is
    running, and the node was running.

    Third instance of this shape in one evening: the watchdog counting its own
    input, `helm chat node up` reporting "the API never answered" while dregg had
    printed the exact remedy, and now this. The pattern is always the same — an
    error path that discards the discriminator and then reports the ambiguity as
    if it were the finding.
    """
    import urllib.error
    import urllib.request
    hdr = {"Content-Type": "application/json"}
    hdr.update(headers or {})
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                 headers=hdr)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        if diag is not None:
            try:
                body = e.read()[:300].decode("utf-8", "replace").strip()
            except Exception:
                body = ""
            diag.update(status=e.code, body=body,
                        reason="HTTP %s from %s%s" % (
                            e.code, url, (": " + body) if body else ""))
        e.close()
        return None
    except Exception as exc:
        if diag is not None:
            diag.update(status=None, body="",
                        reason="%s: %s" % (exc.__class__.__name__, exc))
        return None


# ---------------------------------------------------------------------------
# OPTIONAL external anchor — checkpoint a native record onto a dregg node
# ---------------------------------------------------------------------------
#
# This is the SECOND tier of attestation, never the first. The native hash
# chain (premise.py) is the primary, offline proof. IF a dregg node is
# configured + reachable, helm MAY post the record's hash to the node's thin
# /turn/submit ingress as an EXTERNAL ANCHOR. The node signs the turn with its
# OWN operator cell (confused-deputy hardening, dregg F-P1-3: the request
# `agent` is advisory only) — so the honest claim is "dregg node <url> anchored
# this digest at turn <hash>", NEVER "the user's cell signed". Best-effort +
# FAIL-OPEN: node absent/locked/refusing => no anchor, the native record still
# stands.

ANCHOR_TOPIC = "helm.attest"
ANCHOR_ENDPOINT = "/turn/submit"


def anchor_submit(rec_hash, memo=None, timeout=8):
    """POST `rec_hash` (a 64-hex native record/chain-head hash) to the dregg
    node's thin ingress as an external anchor. (turn_hash, None) on a node-
    accepted anchor; (None, reason) fail-open otherwise. Never raises, never a
    signer claim."""
    word = (rec_hash or "").strip().lower()
    data_words = [word] if len(word) == 64 and all(
        c in "0123456789abcdef" for c in word) else []
    actions = [{
        "method": "attest",
        "effects": [{"kind": "emit_event", "topic": ANCHOR_TOPIC,
                     "data": data_words}],
    }]
    body = {
        "agent": "00" * 32,          # advisory only — the node signs as itself
        # COORDINATION class: an attest turn is EmitEvent-only with no
        # balance_change, so `turn_fee` declares 0 and it rides dregg's
        # coordination-exempt admission free (no cell drain, no faucet grant, no
        # [unsigned] throttle). An economic turn would declare `anchor_fee()`.
        "nonce": 0, "fee": turn_fee(actions),
        "memo": memo or ("helm-attest:" + (rec_hash or "")),
        "actions": actions,
    }
    headers = {}
    tok = home.env("NODE_TOKEN")
    if tok:
        headers["Authorization"] = "Bearer " + tok
    diag = {}
    resp = post_json(node_url() + ANCHOR_ENDPOINT, body, timeout=timeout,
                     headers=headers, diag=diag)
    if resp is None:
        # SAY WHICH. "unreachable/locked" is a disjunction, and an operator
        # reading "unreachable" checks whether the node is running — which, for
        # the 401 this actually was, is the one check that comes back fine.
        why = diag.get("reason") or "no response and no error detail"
        if diag.get("status") in (401, 403):
            why += ("\n  the node is UP and REFUSING this turn — an auth "
                    "problem, not a reachability one. helm sends a bearer from "
                    "HELM_NODE_TOKEN; a node gated behind cipherclerk needs that "
                    "token, while the signature-based client-sign path "
                    "authenticates differently and can succeed while this fails.")
        return None, "anchor rejected by %s — %s" % (node_url(), why)
    if not isinstance(resp, dict):   # a valid JSON list/string is NOT acceptance
        return None, "node returned a non-object anchor response — fail open"
    if resp.get("accepted") and resp.get("turn_hash"):
        return resp["turn_hash"], None
    return None, "node did not accept the anchor: %s" \
        % (resp.get("error") or "no turn_hash")


def anchor_label(turn_hash):
    """The HONEST one-line description of an external anchor — node-anchored,
    never cell-signed."""
    return "dregg node %s anchored digest at turn %s" % (node_url(), turn_hash)


def verify_anchor(turn_hash, timeout=4):
    """Best-effort read-back: does the dregg node still show a turn with this
    hash? (observed, detail). Tries /api/turn/<hash>/proof then the starbridge
    receipt filter.

    HONEST SCOPE (helm A1): this only OBSERVES that a turn with the stored hash
    EXISTS on the node — it does NOT prove that turn's EmitEvent carries the
    entry's native record hash. attest_anchor_turn is mutable frontmatter and is
    not part of the native record, so an unrelated real turn could be substituted
    and still 'observe' here. Until dregg exposes payload disclosure helm can
    consume, this is 'turn observed', NEVER independent re-verification of the
    external commitment. Reports only what the node shows — never a signer
    identity helm cannot prove."""
    url = node_url()
    proof = get_json("%s/api/turn/%s/proof" % (url, turn_hash), timeout=timeout)
    if isinstance(proof, dict) and proof.get("turn_hash"):
        return True, "turn present on %s (payload binding unavailable)" % url
    rec = get_json("%s/api/starbridge/receipts?turn_hash=%s" % (url, turn_hash),
                   timeout=timeout)
    if isinstance(rec, list) and rec:
        return True, "receipt present on %s (payload binding unavailable)" % url
    if proof is None and rec is None:
        return False, "node unreachable at " + url
    return False, "turn not found on " + url


# ---------------------------------------------------------------------------
# OPTIONAL a2a transport (explicit opt-in, NEVER attestation, always degrades)
# ---------------------------------------------------------------------------

# HELM_* -> every name a configured signer bin reads: the dregg-native
# dregg-client-sign (DREGG_*) and the legacy meld-style cell bin (MELD_*).
# One pair per target so build_env stays a plain loop; a HELM var with two
# targets simply appears twice.
ENV_MAP = (
    ("HELM_NODE_URL", "MELD_NODE_URL"),
    ("HELM_NODE_URL", "DREGG_NODE_URL"),
    ("HELM_CELL_PROFILE", "MELD_AGENT_PROFILE"),
    ("HELM_CELL_PROFILE", "DREGG_PROFILE"),
    ("HELM_NODE_TOKEN", "MELD_NODE_TOKEN"),
    ("HELM_NODE_TOKEN", "DREGG_API_TOKEN"),
    ("HELM_NODE_PASSPHRASE", "MELD_NODE_PASSPHRASE"),
    ("HELM_NODE_PASSPHRASE", "DREGG_NODE_PASSPHRASE"),
)

# THE VERBS THE SIGNER ACTUALLY DISPATCHES, and only those. dregg-client-sign
# answers `join` and `send` (and, from the rebased build on, `transfer`, which
# helm drives itself through run_bin rather than as a passthrough); every other
# word is `unknown verb`. `accept`, `recv`, `heartbeat` and `roster` were the
# retired meld-style cell binary's verbs, and passing them through handed the
# caller a signer error in place of helm's own "unknown verb" line. The
# `~/.dregg/roster.toml` reader went with them: no dregg build reads that file
# (neither the fee-loop build nor the rebased one), so `helm cell status` was
# printing a count of a file nothing consults.
PASS_VERBS = ("join", "send")

_USAGE = """usage: helm cell <verb> [args]
  status        node liveness (dregg HTTP)
  join|send     OPTIONAL a2a transport — only when HELM_CELL_BIN points at a
                signer binary; degrades otherwise (attestation never needs it)"""


def _signer_env_file():
    """The OPTIONAL operator-set deployment env for the signer subprocess:
    HELM_CELL_ENV_FILE else <helm-home>/signer.env. Absent => no-op ({})."""
    p = home.env("CELL_ENV_FILE") \
        or os.path.join(home.helm_home(), "signer.env")
    try:
        with open(p, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return {}
    out = {}
    for ln in lines:
        ln = ln.strip()
        if not ln or ln.startswith("#") or "=" not in ln:
            continue
        k, v = ln.split("=", 1)
        k = k.strip()
        if k:
            out[k] = v.strip()
    return out


PROFILE_ENV = ("HELM_CELL_PROFILE", "DREGG_PROFILE", "MELD_AGENT_PROFILE")


def signer_profile():
    """(profile, source) — the AMBIENT/configured signing profile, or (None,
    None). Env only; this deliberately does NOT resolve identity.

    Split out from the identity question on purpose: what the ENVIRONMENT says
    and who this process ACTUALLY IS are different facts, and conflating them
    is how a machine-global shell export came to speak for individual seats.
    `signing_identity` is the one that decides; this only reports the claim.

    AN ABSENT PROFILE IS ALSO NOT A NEUTRAL STATE. `build_env` mapped HELM_*
    onto DREGG_* only when the HELM_* var was set, so an unset profile left
    DREGG_PROFILE MISSING from the child env — and dregg's `active_name()`
    then falls through to a machine-global ACTIVE file
    (dregg/sdk/src/profiles.rs). helm chose nothing and the downstream default
    filled the vacuum. Naming the profile explicitly closes that second hole
    even where the first one is not what bit us."""
    for name in PROFILE_ENV:
        v = (os.environ.get(name) or "").strip()
        if v:
            return v, name
    return None, None


def derived_seat():
    """This process's OWN seat name, or "" when it cannot prove which seat it
    is. Never guesses.

    THE SESSION ID IS THE AUTHORITY, NOT THE ENV VAR. `HELM_CHAT_NAME` is a
    hint any ambient shell can set or clear; the session id is minted per
    session and looked up in the roster, so it survives an environment that
    lies. Measured 2026-08-04 in a fresh process with HELM_CHAT_NAME UNSET and
    an ambient profile naming the owner: the sid path still resolved this seat
    correctly. `meld._self_seat` already composes both in that order and is
    reused rather than re-implemented — two identity resolvers diverge, and the
    divergence is silent.

    AND A NAME IS NOT YET A SIGNING IDENTITY — the row must be a FLEET ACTOR.
    The roster holds two populations and they are already distinguishable
    without inventing a marker: every fleet actor carries `home_room` (minted
    by the join/adopt path when the session ACTS), while the owner's ad-hoc
    sessions are OBSERVED rows with a sid and no home_room. Measured
    2026-08-04: 12 actors carry it, 10 rows do not, and the second set is
    exactly the owner's own ad-hoc sessions plus never-joined rows.

    That distinction is load-bearing, not cosmetic. Without it, the owner's own
    session would resolve to its own session name, disagree with his ambient
    and go UNSIGNED — breaking the one case that was always correct in order to
    fix the ones that were not. With it, a home_room-less row is simply not a
    signing identity, no conflict can fire, and his rows keep signing as
    himself. It is also self-consistent going forward: the moment a session
    joins a room it becomes an actor, which is precisely the moment
    misattribution becomes possible.

    "" HAS TWO MEANINGS HERE, and only `seat_reading` keeps them apart: "this
    process is not a seat" and "the roster could not say". A signing decision
    must never read the second as the first — see `signing_identity`."""
    seat, unreadable = seat_reading()
    return "" if unreadable else seat


def seat_reading():
    """(seat, unreadable) — this process's seat as far as the roster can
    prove it, and why it could not, from ONE roster read. Never raises.

      ("X", "")      the roster proves X is a fleet actor (see `derived_seat`)
      ("", "")       provably no seat: nothing named, or an observed session
      ("X", reason)  X is the name this process gives itself, and whether X
                     is a fleet actor could not be read
      ("", reason)   the process could not name itself at all

    AN UNREADABLE ROSTER IS NOT AN EMPTY ONE. `seats.roster()` answers {} for
    a corrupt, unreadable or wrong-shaped file, and reading through it turned
    every such window into permission: no row, so no actor, so no conflict, so
    the ambient profile stood — for EVERY seat that inherited the owner's
    export. `seats.roster_checked()` is the strict reader: a MISSING file is
    still a proven empty roster (a fresh box has no seats), everything else it
    cannot read is a failure, and a failure is reported, never filed as "no
    seat".

    THE NAME MAY BE A ROSTER KEY, AND IT IS VALIDATED BEFORE IT LEAVES.
    `_self_seat` resolves a live rename alias to the row it names (task/3049),
    so the name is no longer always the process's own validated
    HELM_CHAT_NAME. It goes back through the same identifier rule
    (`home.validate_seat_arg`) before it can become a signer or a refusal's
    text; a key that fails it is a name this process could not resolve."""
    try:
        from . import seats
        from .meld import _self_seat
        name = (_self_seat() or "").strip()
        home.validate_seat_arg(name)
    except Exception as exc:
        return "", "this process could not resolve its own seat (%s: %s)" % (
            exc.__class__.__name__, exc)
    if not name:
        return "", ""
    try:
        rows, failed = seats.roster_checked()
    except Exception as exc:
        return name, "the roster read raised %s: %s" % (
            exc.__class__.__name__, exc)
    if failed or not isinstance(rows, dict):
        return name, "the roster is unreadable, malformed or not a mapping"
    row = rows.get(name)
    if not isinstance(row, dict) or not row.get("home_room"):
        return "", ""                 # observed session, not a fleet actor
    return name, ""


def _owner_set():
    """The owner's names, casefolded, or an empty set when they cannot be
    read (see `is_owner_cell`). One `owner_names()` call — it can spawn `git
    config` (~17 ms measured), so a decision reads it once, never per name."""
    try:
        from .seats_identity import owner_names
        return {str(n).strip().casefold() for n in owner_names() or ()}
    except Exception:                    # noqa: BLE001 — unread is not-owner
        return set()


def is_owner_cell(name, owners=None):
    """True when `name` is one of the OWNER's names (`seats.owner_names`,
    casefolded) — the recognition set the owner rails and the web card
    already use. Never raises: an owner set that cannot be read answers False,
    which makes the signing gate REFUSE (the safe direction), never swap.

    NOT A BOX CONSTANT. With no authored `owner_name`, `owner_name()` derives
    from the CWD's git user.name, then the unix login, so on such a box the
    same process can be recognised in one repo and not in another. Every
    wrong answer here is a refusal, never an owner signature: a name wrongly
    NOT recognised keeps today's loud refusal, and a seat wrongly recognised
    as the owner is exactly what the swap refuses to land on."""
    cf = str(name or "").strip().casefold()
    if not cf:
        return False
    return cf in (_owner_set() if owners is None else owners)


def _admitted_as(seat, admitted=None, admit=True):
    """(True, None) when the identity layer admits THIS process as `seat`,
    else (False, why). Never raises.

    `admitted` is an `actors.AdmittedActor` the caller's own door already
    minted for this process (the CLI post door does, through `_seat_actor`),
    so the common path runs no second admission. A raw string is not one and
    is ignored: a name cannot stand in for the capability. Without it, the
    law is asked through `actors.admitted_name`, which runs every refusal the
    admission pass runs and WRITES NOTHING. `admit=False` is a reader that
    discards the refusal anyway (a fleet status panel); it consults nothing."""
    name = None
    try:
        from . import actors
        if isinstance(admitted, actors.AdmittedActor):
            name = admitted.canonical_name
        elif not admit:
            return False, "this reader does not consult the identity layer"
        else:
            name, err, _reason = actors.admitted_name(
                home.session_id(), act="sign as seat %s" % seat)
            if not name:
                return False, err or "the identity layer admitted no seat"
    except Exception as exc:             # noqa: BLE001 — never-raise gate
        return False, "the identity layer raised %s: %s" % (
            exc.__class__.__name__, exc)
    if str(name).casefold() != str(seat).casefold():
        return False, ("the identity layer admits this process as %r, not %r"
                       % (name, seat))
    return True, None


_AMBIENT = object()


def _session_dispute(named):
    """(bound, None) when this process's session is bound to a FLEET ACTOR
    other than the name it gives itself; (None, why) when the roster cannot
    say; (None, None) otherwise. Never raises.

    ASKED ONLY WHERE THE OWNER'S PROFILE WOULD OTHERWISE STAND (rule 6 with
    an owner-name ambient and a session to look up). `seat_reading` names a
    seat by the name this process gives itself; a HELM_CHAT_NAME that no row
    holds — a rename alias past its window, a name inherited from another
    pane — therefore read as "no seat", and the owner's inherited profile
    signed the row while the SESSION sat bound to a fleet seat (task/3049,
    the expired-alias arm). The session is the stronger fact: an owner
    profile never stands for a process the roster binds to a fleet seat."""
    try:
        from . import seats, seats_roster
        sid = home.session_id()
        rows, failed = seats.roster_checked()
    except Exception as exc:             # noqa: BLE001 — never-raise gate
        return None, "the roster read raised %s: %s" % (
            exc.__class__.__name__, exc)
    if failed or not isinstance(rows, dict):
        return None, ("the roster could not be read to rule out that this "
                      "process's session belongs to a fleet seat")
    index, _holders = seats_roster.roster_indexes(rows)
    for bound in seats_roster.seats_for_session_in(index, sid):
        row = rows.get(bound)
        if isinstance(row, dict) and row.get("home_room") \
                and str(bound).casefold() != str(named or "").casefold():
            from .seats_common import _seat_label
            return _seat_label(bound), None
    return None, None


def _own_alias(profile, seat):
    """True when `profile` is `seat`'s OWN old name inside a live rename
    window — the same actor under its previous label, whose key it already
    holds. Never raises; an unreadable roster answers False (refuse).

    WHY THIS IS AGREEMENT, NOT A CONFLICT. `seat_reading` resolves a live
    alias to the renamed row (task/3049), so a seat launched as `old` (profile
    `old`) and renamed to `new` reads as seat `new` under profile `old`. Before
    that resolution it read as "no seat" and signed as `old`; calling the same
    process a CONFLICT now would turn a working seat DEGRADED for the whole
    window. Past the window the old name is a stranger again and the conflict
    returns, which is the rename law everywhere else (`seats_common.own_name`).
    The caller asks this only for a profile that is NOT an owner name, so a
    seat once renamed away from an owner name can never sign as the owner."""
    try:
        from .seats_common import live_alias
        key, _until = live_alias(profile)
    except Exception:                    # noqa: BLE001 — never-raise gate
        return False
    return bool(key) and str(key).casefold() == str(seat).casefold()


def signing_identity(explicit=None, *, reading=None, admitted=None,
                     admit=True, ambient=_AMBIENT):
    """(profile, refusal) — who this process may sign as, or why it may not.
    Exactly one is non-None. `reading` is a `seat_reading()` result the caller
    already took, so one decision costs one roster read; never build one.
    `admitted` and `admit` only matter in the owner-export branch below (see
    `_admitted_as`); that branch alone reads more. `ambient` is the profile
    the environment names, `signer_profile()` unless a caller holds the exact
    value its subprocess will read (`cmd_cell`'s mapped DREGG_PROFILE).

    THE LAW ALREADY EXISTED AND WAS ENFORCED IN THE WRONG PLACE. `launch.py`
    sets HELM_CELL_PROFILE and DREGG_PROFILE to the seat and says why: "a child
    speaking as `seat` must sign as that same seat, not its launcher". That is
    a SPAWN-TIME gate, so any seat arriving by another door — adopted, hand
    started, restarted outside the launcher — bypasses it entirely and then
    inherits whatever the ambient environment says. On the owner's box an ambient
    shell rc exports HIS profile machine-wide, so 6 of 10 live seats were
    signing as the OWNER. Owner-observed. This moves the same law to SIGNING time, which is
    the boundary that actually matters: never trust that the caller checked.

    A SEAT UNDER THE OWNER'S INHERITED PROFILE SIGNS AS ITSELF (task/3049).
    Refusing is not the only honest move, because "a seat cannot sign as
    itself without ITS OWN key material" is a false binary: the signing leg's
    `join --profile <seat>` CREATES the seat's own key when it is missing and
    reuses it after (dregg-client-sign `resolve_clerk(create=true)`),
    fee-free. So the choice is never "borrow or refuse" — the seat can sign
    with its own key. And the owner's profile on a seat is INHERITED BY
    CONSTRUCTION: every helm launch door sets the profile to the seat itself,
    none ever sets an owner name, so a seat carrying one got it from the shell
    (the owner's rc exports it on purpose, for premises he states). Refusing
    that shape left 29 of 300 measured room rows UNSIGNED, every one under the
    owner's profile.

    PRECEDENCE:
      1. `explicit` — a caller deliberately speaking for someone (`--seat kimi`
         signing as kimi). STATED INTENT, never an inherited accident, so it
         wins outright and is not a conflict.
      2. a proven seat identity that AGREES with the ambient profile — the
         normal, correctly-launched case.
      3. a proven seat under the OWNER's profile, that the identity layer
         ADMITS as that same seat — signs as the SEAT. The swap never lands on
         an owner name (`is_owner_cell(seat)` refuses), so it cannot create an
         owner signature; it only turns a refusal into the seat's own.
      4. a proven seat whose profile is its OWN old name inside a live rename
         window (`_own_alias`) — the same actor's key, so it agrees.
      5. any other DISAGREEMENT between a proven seat identity and the ambient
         profile — REFUSED, naming both values. A profile naming ANOTHER SEAT
         stays loud on purpose: that is the pane-contagion shape, a pane that
         inherited another seat's exports.
      6. no proven seat identity — the ambient profile stands. This is the
         population with no seat context, and collapsing it into a refusal
         would strand it unsigned for no benefit.

    AN UNREADABLE IDENTITY IS NOT PERMISSION. When the process names a seat
    and the roster cannot say whether that seat is a fleet actor, rule 6 would
    let the ambient profile stand, and on this box that profile is the owner's.
    So an ambient profile that is not the named seat is REFUSED for that
    window. An ambient profile that IS the named seat needs no roster to be
    safe, and still signs. With no ambient profile the unproven name is never
    used as a signer, so the no-profile answer stands."""
    if ambient is _AMBIENT:
        ambient, _src = signer_profile()
    if explicit:
        return explicit, None
    named, unreadable = reading if reading is not None else seat_reading()
    if unreadable and ambient and ambient != named:
        why = ("this process names seat %r and whether it is a fleet actor "
               "could not be read (%s)" % (named, unreadable)
               if named else unreadable)
        return None, ("identity unreadable: %s, so the environment's profile "
                      "%r cannot be proven to be its own. Signing as %r could "
                      "attribute this row to someone who did not write it, so "
                      "it is left UNSIGNED. Repair the roster (`helm doctor`) "
                      "or pass an explicit profile if you mean to speak for %r."
                      % (why, ambient, ambient, ambient))
    seat = "" if unreadable else named
    if seat and ambient and ambient != seat:
        # THE REASON THE OWNER'S PROFILE WAS NOT SET ASIDE LEADS: a row's
        # stamped reason is capped with its MIDDLE elided (chat._safe_reason),
        # so a clause placed between the diagnosis and the remedy never
        # reaches the reader who needs it.
        head = "the environment names profile %r" % ambient
        owners = _owner_set()
        if is_owner_cell(ambient, owners):
            if not is_owner_cell(seat, owners):
                ok, why = _admitted_as(seat, admitted, admit)
                if ok:
                    return seat, None
                head = ("the environment names the owner's profile %r, which "
                        "is set aside for a seat only when the identity "
                        "layer admits this process as that seat — it did "
                        "not (%s)" % (ambient, why))
        elif _own_alias(ambient, seat):
            return ambient, None
        return None, ("identity conflict: this process resolves to seat %r but "
                      "%s. Signing as %r would attribute this row to someone "
                      "who did not write it, so it is left UNSIGNED. Relaunch "
                      "through `helm launch` (which sets both vars to the "
                      "seat) or pass an explicit profile if you mean to speak "
                      "for %r." % (seat, head, ambient, ambient))
    if not seat and not unreadable and ambient and home.session_id() \
            and is_owner_cell(ambient):
        bound, why = _session_dispute(named)
        if bound or why:
            return None, ("identity conflict: the environment names the "
                          "owner's profile %r, but %s. Signing as %r would "
                          "attribute this row to someone who did not write "
                          "it, so it is left UNSIGNED. Re-export "
                          "HELM_CHAT_NAME to the seat this is, relaunch "
                          "through `helm launch`, or pass an explicit profile "
                          "if you mean to speak for %r."
                          % (ambient, why or (
                              "this process's session is bound to fleet seat "
                              "%r while it names itself %r — a stale or "
                              "expired HELM_CHAT_NAME" % (bound, named)),
                             ambient, ambient))
    return (ambient or seat or None,
            None if (ambient or seat) else "no profile and no derivable seat")


def build_env():
    """Subprocess env for the OPTIONAL a2a transport: a copy of os.environ with
    each set HELM_* mapped onto its signer-facing names (legacy MELD_* and
    dregg-native DREGG_*; HELM wins; absent HELM leaves a directly-set
    MELD_*/DREGG_* untouched — the env2 pattern).

    An operator's `signer.env` (see `_signer_env_file`) fills GAPS only — real
    os.environ / HELM_* mappings WIN — so a deployment configures the signer's
    runtime posture ONCE (e.g. a marshal-only devnet's DREGG_ALLOW_UNAUDITED_PQ,
    read fresh on every signed turn, so an already-running seat picks it up with
    no relaunch) without editing per-seat launch env. Absent file => unchanged;
    an env that explicitly sets the same key overrides the file (production
    leaving the file absent stays audited)."""
    env = dict(_signer_env_file())
    env.update(os.environ)
    for h, m in ENV_MAP:
        v = os.environ.get(h)
        if v is not None:
            env[m] = v
    # THE PROFILE IS STATED EXPLICITLY OR NOT AT ALL. Leaving it absent hands
    # the choice to the dregg client's default, which is the OWNER's profile —
    # see `signer_profile`. When a seat identity is derivable but no profile
    # env is set, name it on BOTH signer-facing vars so neither the dregg-native
    # nor the legacy bin can fall back. When nothing is derivable the vars stay
    # unset and `run_bin` refuses rather than letting the signer pick.
    # Only fill a GAP: an operator's signer.env may legitimately name the
    # profile, and a DERIVED seat identity must never clobber an EXPLICIT
    # configuration — that is the same fills-gaps-only contract the file
    # already has with os.environ, applied one layer further in.
    if not (env.get("DREGG_PROFILE") or "").strip():
        profile, _refusal = signing_identity()
        if profile:
            env["DREGG_PROFILE"] = profile
            if not (env.get("MELD_AGENT_PROFILE") or "").strip():
                env["MELD_AGENT_PROFILE"] = profile
    return env


def _signer_env_writable_by_others(path=None):
    """True when signer.env — or the directory holding it — is group- or
    world-writable, i.e. when a seat OTHER than this one could choose the
    binary this one is about to exec.

    A blocking finding, measured on this box: signer.env is -rw-rw-r--
    inside a drwxrwxr-x ~/.helm, and `bin_path` execs whatever path it names
    for EVERY seat. That is a CROSS-SEAT REDIRECT THE ENV ROUTE CANNOT
    PRODUCE: one seat's environ is private to that process, but a shared file
    is a channel from any same-uid seat into every other seat's exec. `_usable`
    does not help — it happily passes a planted binary, because being a real
    executable is exactly what an attacker's payload is.

    THE DIRECTORY COUNTS AS MUCH AS THE FILE. A 0600 signer.env inside a
    group-writable directory can be REPLACED wholesale — unlink plus create —
    so checking the file alone would be a guard that reads the wrong object.
    This is OpenSSH's StrictModes rule and it is right for the same reason.

    AN ABSENT FILE ANSWERS FALSE and short-circuits: there is no file route to
    abuse, bin_path returns None on its own a line later, and calling that
    "writable by others" would be a guard inventing a threat. The directory's
    mode is therefore only ever consulted for a file that EXISTS — which is
    exactly the case where a loose directory lets someone replace it — and an
    unstattable directory under an existing file answers True, because a mode
    we could not check must never authorize an exec.

    The env var route is untouched — a seat that sets HELM_CELL_BIN is
    choosing its own signer and no shared file is involved, so a hostile
    signer.env cannot disarm a seat that never consulted one."""
    p = path or (home.env("CELL_ENV_FILE")
                 or os.path.join(home.helm_home(), "signer.env"))
    for target in (p, os.path.dirname(p) or "."):
        try:
            mode = os.stat(target).st_mode
        except OSError:
            if target == p:
                return False        # absent file: nothing to trust, nothing to fear
            return True             # unstattable directory: cannot verify, refuse
        if mode & (stat.S_IWGRP | stat.S_IWOTH):
            return True
    return False


def bin_path():
    """The OPTIONAL a2a cell binary, EXPLICIT only: HELM_CELL_BIN (legacy
    MELD_CELL_BIN), else the operator's `signer.env`. Never auto-resolved — no
    PATH probe, no sibling-build guess — so helm never silently execs a binary.
    None => the transport degrades.

    THE FILE IS A WAY OF BEING TOLD, WHICH IS THE LAW'S ACTUAL CONTENT. The
    rule is that helm never execs a signer it was not TOLD about; an operator
    writing an absolute path into signer.env has told it, as deliberately as an
    export does. `build_env` has always read this file, with exactly these
    semantics — gaps only, real env WINS — so a deployment can configure the
    signer's posture ONCE and "an already-running seat picks it up with no
    relaunch". This key was the one it skipped, and it is the key that decides
    whether signing happens at all.

    THE GAP THAT MADE THIS NECESSARY, measured 2026-08-01 over 30 room rows:
    proxy-family seats carry HELM_CELL_BIN in their process env and signed
    12/12; the two claude-direct seats must prefix it onto EVERY command,
    because their shell state does not persist between tool calls, and signed
    7 of 16. The board row said "RELAUNCH is the sole path" — it is not, but
    the alternative was per-command ritual that both seats forgot about half
    the time. A capability that degrades silently unless a human-shaped habit
    holds every single time is not wired; it is available.

    PRECEDENCE IS UNCHANGED AND DELIBERATE: a real env var still WINS, so a
    seat that sets HELM_CELL_BIN keeps whatever it set, and an unset key falls
    to the file. Absent file => None => the transport degrades exactly as
    before, and `_usable()` still decides whether what we were told is real."""
    told = home.env("CELL_BIN")
    if told:
        return told
    # ONE read, both spellings — an operator writes the file by hand and the
    # legacy MELD_ name is still accepted everywhere else helm reads config.
    # STRICT MODES FIRST: a file ANY same-uid seat can rewrite must not choose
    # the binary EVERY seat execs (see _signer_env_writable_by_others).
    if _signer_env_writable_by_others():
        return None
    fromfile = _signer_env_file()
    return fromfile.get("HELM_CELL_BIN") or fromfile.get("MELD_CELL_BIN") or None


def _resolve(b):
    """The configured signer as an executable path, or None.

    A BARE NAME RESOLVES THROUGH PATH. seat.py's module doc has always taught
    `HELM_CELL_BIN=dregg-client-sign` (the form every adopter copies), while
    the launch env exports the absolute default — and the first cut of
    `_usable` took the value as a literal path, so an operator who followed
    the doc exactly got usable:False at runtime (measured on the original
    row: doc says bare name, code exec's the value without PATH resolution).
    The doc is the better form: a name the PATH already knows survives a
    prefix change, and `helm seat`'s own proxy binary resolves the same way
    (`_proxy_bin`: default, else PATH). Absolute and relative paths are taken
    as-is; only a slash-free name goes to shutil.which, which is the
    resolution the exec would have done — declared HERE, where the verdict
    can still be refused, not left to the exec's silent failure.

    THE TOLD-ABOUT LAW IS UNTOUCHED. This resolves a value the operator
    already SET; it never probes PATH for a signer nobody configured.
    """
    if not b:
        return None
    if "/" in b:
        return b
    import shutil
    return shutil.which(b)


def _usable(b):
    """A configured signer is real ONLY when it is a regular file AND
    executable — a directory or a non-executable file (mode 0600, etc.) is NOT
    a usable binary. os.path.exists() would say yes and let status claim
    'signed' + let the signing leg burn an unlock lap; isfile + X_OK is the
    minimum honest meaning of 'signer ready' (day-review #1)."""
    p = _resolve(b)
    return bool(p and os.path.isfile(p) and os.access(p, os.X_OK))


SIGNER_CRATE = "dregg-sdk-net"   # the crate the signer binary is built from

# WHAT A DEPLOYED BINARY MUST NOT LAG: its own crate plus the path dependencies
# that decide what it does. Read from the Cargo manifests at the rebased dregg
# tip: dregg-sdk-net depends on sdk and turn directly, sdk (and node, and
# persist) on dregg-lean-ffi, and dregg-lean-ffi's build.rs compiles
# ../metatheory, the Lean source of the verified cores both binaries link.
# node depends on turn, persist and dregg-lean-ffi directly.
#
# NOT THE WHOLE CLOSURE, ON PURPOSE. The transitive path closure is 42 crates
# for the signer and 67 for the node, and `--all` below reads every ref, so a
# check over all of it reads stale after almost any commit anywhere and is
# ignored within a day. These are the crates whose change is a change in
# signing, verification, execution or storage.
SOURCE_PATHS = {
    "signer": (SIGNER_CRATE, "sdk", "turn", "dregg-lean-ffi", "metatheory"),
    "node": ("node", "persist", "turn", "dregg-lean-ffi", "metatheory"),
}
_CORE_RE = re.compile(r"verified ML-DSA cores:\s*(.+)")


def dregg_repo():
    """Where the signer's SOURCE lives, or None when unconfigured. Config-driven,
    never a hardcoded identity (a path literal inside portable logic is a bug):
    HELM_DREGG_REPO env, else the host's authored `dregg_repo` (registry-authored
    `host` block). No site-specific path ships in code. On an UNREADABLE authored
    layer it warns LOUDLY and returns None (signer source unknown -> posts ride
    unsigned, but never silently) rather than crashing the signer callers."""
    v = home.env("DREGG_REPO")
    if not v:
        from . import registry
        try:
            v = registry.authored_host().get("dregg_repo")
        except registry.AuthoredUnreadable as e:
            print("helm cell: authored layer unreadable (%s) — signer source "
                  "unknown; posts ride unsigned until fixed (config recoverable "
                  "from its .corrupt backup)" % e, file=sys.stderr)
            v = None
    return os.path.expanduser(v) if v else None


def _git(repo, args, timeout=10):
    """(rc, stdout) from git in `repo`; rc None when git cannot run at all."""
    try:
        p = subprocess.run(["git", "-C", repo] + args, capture_output=True,
                           text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None, ""
    return p.returncode, p.stdout.strip()


def source_head(paths, repo=None):
    """Newest commit touching any of `paths` ON ANY REF.

    `--all` is the load-bearing flag, not a flourish. The fix that repairs a
    binary routinely lives on a LANE that was never merged — so a check against
    main would have reported everything current while the cure sat one branch
    away, which is exactly where the signer's funding-bounds fix once sat.
    """
    repo = repo or dregg_repo()
    if not repo:
        return None  # no source configured -> no head; callers report it
    rc, out = _git(repo, ["log", "-1", "--format=%ct%x09%h%x09%s", "--all",
                          "--"] + list(paths))
    if rc != 0 or not out:
        return None
    ts, _, rest = out.partition("\t")
    commit, _, subject = rest.partition("\t")
    try:
        return {"ts": int(ts), "commit": commit, "subject": subject}
    except ValueError:
        return None


def _staleness(what, binary, repo, remedy):
    """{state: current|stale|unknown, ...} for one deployed binary against the
    newest commit on SOURCE_PATHS[what]. UNKNOWN IS NOT OK: no binary, no
    repo or an unreadable git is "unknown", never "current"."""
    paths = SOURCE_PATHS[what]
    head = source_head(paths, repo)
    if not head:
        return {"state": "unknown",
                "reason": "%s source unreadable (no %s in a repo at %s)"
                          % (what, ", ".join(paths), repo or dregg_repo()
                             or "(unconfigured — set HELM_DREGG_REPO or host.dregg_repo)")}
    try:
        built = os.path.getmtime(binary)
    except OSError as exc:
        return {"state": "unknown", "reason": "%s mtime unreadable (%s)"
                % (what, exc.strerror or exc.__class__.__name__)}
    lag = head["ts"] - built
    row = {"built_at": int(built), "source_ts": head["ts"],
           "commit": head["commit"], "subject": head["subject"],
           "lag_s": int(lag), "binary": binary, "paths": list(paths)}
    if lag <= 0:
        row["state"] = "current"
        row["reason"] = "%s is newer than its source" % what
        return row
    row["state"] = "stale"
    row["reason"] = (
        "the DEPLOYED %s predates its own source by %s — it was built "
        "before %s (%s). Whatever that commit fixes is NOT in the running "
        "binary. %s" % (what, _ago(lag), head["commit"], head["subject"],
                        remedy))
    return row


def signer_staleness(repo=None):
    """Does the DEPLOYED signer predate its own SOURCE?

    THE 52-MINUTE CHECK. On 2026-07-29 the owner asked why dregg signing "is
    broken again every time I look". It was not breaking repeatedly: the fix
    (an exempt join must zero BOTH funding bounds — without it the first join
    still asks the faucet for a funded grant and dies on `rate limited: 1
    request per cell per minute`) was committed at 17:44 and the deployed
    binary was built at 16:52. Fifty-two minutes too old, for twenty hours,
    and every helm surface said "signer ready" the whole time.

    Nothing here needs a provenance file or a build-system change: a binary's
    mtime against its source's newest commit is enough, and it is the cheapest
    honest answer available. The source is SOURCE_PATHS["signer"], the crate
    AND the dependencies that decide what it signs and verifies — a fix in
    turn/ or the Lean cores changes the signer as surely as one in its crate.

    UNKNOWN IS NOT OK. No binary, no repo, or an unreadable git all return
    state "unknown" — never "current". An unproven thing is not a safe thing,
    and a staleness check that fails open would re-create the exact silence it
    exists to break.
    """
    b = bin_path()
    if not _usable(b):
        return {"state": "unknown", "reason": "no usable signer configured"}
    return _staleness("signer", _resolve(b), repo,
                      "Rebuild the signer binary (never on the agent hub).")


def node_staleness(repo=None):
    """Does the DEPLOYED chat node binary predate its own SOURCE?

    The same check as signer_staleness, for the other binary on the seam. It
    did not exist, and the node sat thousands of commits behind its source
    with no helm surface saying so. The binary is the one `helm chat node up`
    would run (chatnode.bin_resolution: HELM_CHAT_NODE_BIN, else the binary
    the last `up` recorded, else — only with nothing recorded —
    dregg-cave-node on PATH, else ~/.local/bin/dregg-cave-node). A record
    that cannot be honoured is unknown with its own reason, never a staleness
    reading of whichever binary the PATH offers."""
    from . import chatnode
    res = chatnode.bin_resolution()
    b = res["path"] and chatnode.install_path(res["path"])
    if not b or not os.path.isfile(b):
        return {"state": "unknown", "reason": res["reason"] or
                "no chat node binary at %s" % b}
    return _staleness("node", b, repo,
                      "Rebuild and reinstall the node binary (never on the "
                      "agent hub).")


def _ago(seconds):
    s = int(max(0, seconds))
    if s < 5400:
        return "%dm" % round(s / 60.0)
    if s < 172800:
        return "%.1fh" % (s / 3600.0)
    return "%.1fd" % (s / 86400.0)


def signer_cores():
    """CAPABILITY, not presence: can the configured signer actually SIGN?

    The binary announces this itself, unprompted, on stderr of EVERY run —
    `verified ML-DSA cores: sign ExportAbsent, verify ExportAbsent` — and helm
    had no code that read it, so `bin_status()` reported "signer ready" about a
    binary that was telling us in plain text it had no signing cores. The probe
    costs nothing measurable (the line is printed before any work: 0.00s wall,
    ~9MB RSS) because it needs no verb and no network.

    ExportAbsent is NOT automatically a fault — a deliberate marshal-only
    devnet posture reports exactly that and signs via the unaudited fips204
    fallback. So this REPORTS the cores and refuses to editorialise; the caller
    decides whether the deployment it is looking at expects them present.
    """
    b = bin_path()
    if not _usable(b):
        return {"state": "unknown", "reason": "no usable signer configured"}
    try:
        p = subprocess.run([_resolve(b)], capture_output=True, text=True,
                           timeout=20, env=build_env())
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"state": "unknown",
                "reason": "signer would not run (%s)"
                          % (getattr(exc, "strerror", None) or
                             exc.__class__.__name__)}
    m = _CORE_RE.search((p.stderr or "") + "\n" + (p.stdout or ""))
    if not m:
        return {"state": "unknown",
                "reason": "signer printed no ML-DSA core line"}
    cores = {}
    for part in m.group(1).split(","):
        name, _, val = part.strip().partition(" ")
        if name and val:
            cores[name.strip()] = val.strip()
    absent = sorted(k for k, v in cores.items() if v != "Installed")
    return {"state": "marshal-only" if absent else "verified",
            "cores": cores, "absent": absent,
            "reason": ("Lean-verified cores present"
                       if not absent else
                       "running the unaudited fallback; absent: %s"
                       % ", ".join(absent))}


def bin_status():
    """One signer-configuration truth for send + every status projection.
    The configured path is deliberately not returned: diagnostics identify the
    failure class without publishing a possibly hostile path string."""
    b = bin_path()
    if not b:
        return {"configured": False, "usable": False, "state": "unset",
                "reason": "HELM_CELL_BIN is unset"}
    p = _resolve(b)
    if not p or not os.path.exists(p):
        # The reason names the FORM looked up, never the VALUE: this string
        # renders on status surfaces and a hostile path would ride it (the
        # existing missing-signer test carries an ESC sequence for exactly
        # that law).
        return {"configured": True, "usable": False, "state": "missing",
                "reason": ("HELM_CELL_BIN is set but the signer path does "
                           "not exist" + ("" if "/" in b
                                          else " (a bare name, looked up on "
                                          "PATH)"))}
    if not os.path.isfile(p):
        return {"configured": True, "usable": False, "state": "not_file",
                "reason": "HELM_CELL_BIN is set but the signer path is not a regular file"}
    if not _usable(b):
        return {"configured": True, "usable": False,
                "state": "not_executable",
                "reason": "HELM_CELL_BIN is set but the signer file is not executable"}
    return {"configured": True, "usable": True, "state": "ready",
            "reason": "signer ready"}


#: THE SIGNER'S OWN WORDS, and they are an EXTERNAL producer's grammar so they
#: are matched EXACTLY rather than normalised. dregg-pq's install result is
#: printed with Rust's Debug, so these are that enum's variant spellings:
#: `Installed` and `AlreadyInstalled` mean the Lean-verified core is live,
#: `ExportAbsent` means the linked archive exported no such core and dregg-pq
#: fell back to its unaudited default implementation.
_CORE_PRESENT = ("Installed", "AlreadyInstalled")
_CORE_ABSENT = ("ExportAbsent",)
#: `[client-sign] verified ML-DSA cores: sign X, verify Y`, on STDERR, printed
#: before the binary looks at argv — so any invocation answers, and the values
#: are the RESULT of the real install attempt rather than a claim about it.
#: THE WHOLE TOKEN OR NOTHING. `[A-Za-z]+` stops at the underscore, so a future
#: `Installed_v2` matched as `Installed` and reported READY — a variant this
#: helm has never seen, read as the one outcome that withholds every warning.
#: The class spans everything Rust's Debug can put in a bare variant name, and
#: the boundary is asserted so a longer token fails membership instead of
#: silently shortening into a known one.
_CORES_RE = re.compile(
    r"verified ML-DSA cores:\s*sign\s+([A-Za-z0-9_]+)\s*,"
    r"\s*verify\s+([A-Za-z0-9_]+)\b")


def verified_cores(timeout=20):
    """Does the configured signer actually carry the Lean-verified ML-DSA cores?

    -> {"state": ready|degraded|unknown|unusable, "sign": ..., "verify": ...,
        "reason": ...}

    WHY HELM ASKS AT ALL. A dregg build whose libdregg_lean.a exports no
    verified cores does not fail: dregg-pq answers security-critical
    operations with its unaudited fallback crates instead, the build is green,
    and the only trace is one stderr line at signer startup. helm is the
    consumer that execs this binary, so helm is where the question has to be
    asked — nothing upstream of here will refuse on our behalf.

    EVERY FAILURE PATH ANSWERS `unknown`, NEVER `ready`. Not being able to ask
    is not evidence that the answer is good, and this value gates whether an
    operator is told their transport is verified.
    """
    status = bin_status()
    if not status["usable"] or not bin_path():
        return {"state": "unusable", "sign": None, "verify": None,
                "reason": status["reason"]}
    # `--help` is the cheapest invocation that still runs the real install
    # attempt: the line is printed before argv is examined.
    #
    # THIS PROBE MUST NEVER RAISE INTO ITS CALLER. It is read by `helm doctor`,
    # whose entire job is to survive a broken world and describe it — a
    # diagnostic that crashes on the misconfiguration it exists to report is
    # worse than one that never ran. Anything unexpected is UNKNOWN, which is
    # the answer that withholds the verified claim.
    try:
        rc, _out, err = run_bin(["--help"], timeout=timeout)
    except Exception as exc:                      # noqa: BLE001
        return {"state": "unknown", "sign": None, "verify": None,
                "reason": ("the signer could not be probed (%s)"
                           % exc.__class__.__name__)}
    if rc is None:
        return {"state": "unknown", "sign": None, "verify": None,
                "reason": err or "the signer could not be run"}
    m = _CORES_RE.search(err or "")
    if not m:
        # A signer that does not answer is not a signer that answered well.
        return {"state": "unknown", "sign": None, "verify": None,
                "reason": ("the signer printed no verified-core line; it may "
                           "predate the line or not be dregg-client-sign")}
    sign, verify = m.group(1), m.group(2)
    known = _CORE_PRESENT + _CORE_ABSENT
    if sign not in known or verify not in known:
        # THE VALUE IS NOT REPEATED BACK. This whole module names the FORM and
        # never the VALUE on a diagnostic surface, because the string comes
        # from a binary an operator was told about rather than one helm built.
        return {"state": "unknown", "sign": None, "verify": None,
                "reason": ("the signer's verified-core line used words this "
                           "helm does not recognise")}
    if sign in _CORE_PRESENT and verify in _CORE_PRESENT:
        return {"state": "ready", "sign": sign, "verify": verify,
                "reason": "both Lean-verified ML-DSA cores are installed"}
    absent = [n for n, v in (("sign", sign), ("verify", verify))
              if v in _CORE_ABSENT]
    return {"state": "degraded", "sign": sign, "verify": verify,
            "reason": ("the signer linked NO Lean-verified %s core, so "
                       "dregg-pq answers with its unaudited fallback"
                       % " and ".join(absent))}


def unaudited_declared():
    """Did the OPERATOR declare the unaudited-PQ posture? -> (declared, where).

    THE DIFFERENCE BETWEEN A DECISION AND AN ACCIDENT, and it is the whole
    value of asking. dregg refuses to serve unverified unless an operator opens
    the hatch, and helm's contract is to MIRROR an existing declaration and
    never mint one. So a signer running the unaudited fallback WITH a
    declaration is a posture somebody chose, with a written reversal; the same
    signer WITHOUT one is the case nobody chose, and only that is an alarm.

    `build_env` is the authority because it is what the signer subprocess
    actually receives — signer.env filling gaps, real env winning — so this
    asks the same composed question a signed turn asks.
    """
    try:
        # THE ENVIRONMENT IS ASKED FIRST AND ANSWERS ON ITS OWN. A real env var
        # is readable whatever the file is doing, so a declaration made there
        # never depends on the file's read status.
        live = os.environ.get("DREGG_ALLOW_UNAUDITED_PQ")
        if live is not None:
            return _truthy(live), ("the environment" if _truthy(live) else None)
        # ABSENT AND UNREADABLE ARE DIFFERENT ANSWERS, and conflating them is
        # what made a failed read render as an alarm. `_signer_env_file`
        # fails OPEN to {} by design — correct for its callers, fatal here,
        # because "no declaration" would then be indistinguishable from "the
        # declaration could not be read". signer.env's own text is explicit
        # that an ABSENT file means audited-required, so absent really is
        # undeclared; unreadable is neither.
        mapping, err = _signer_env_read()
        if err:
            return None, None
        return (_truthy(mapping.get("DREGG_ALLOW_UNAUDITED_PQ")),
                _signer_env_path()
                if _truthy(mapping.get("DREGG_ALLOW_UNAUDITED_PQ")) else None)
    except Exception:                             # noqa: BLE001
        return None, None


def _truthy(v):
    return str(v).strip().lower() not in ("", "0", "false", "none")


def _signer_env_read():
    """(mapping, err) — the same file `_signer_env_file` reads, with its READ
    STATUS preserved. ABSENT is (empty, None); unreadable is ({}, reason)."""
    p = _signer_env_path()
    try:
        with open(p, encoding="utf-8") as f:
            raw = f.read()
    except FileNotFoundError:
        return {}, None
    except OSError as exc:
        return {}, exc.__class__.__name__
    out = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip()
    return out, None


def _signer_env_path():
    return home.env("CELL_ENV_FILE") \
        or os.path.join(home.helm_home(), "signer.env")


def signer_reach(path):
    """How many LIVE seat processes name THIS signer in their own environment.

    -> (naming, seats, err), both counts of DISTINCT SEAT NAMES rather than of
    processes: a seat's children inherit its environment, so counting
    processes overstates the answer several-fold.

    `naming` names `path` DIRECTLY via HELM_CELL_BIN. A seat that reaches the
    same signer through the operator's signer.env is NOT counted, so `naming`
    is a FLOOR on the reach and never the whole of it — which is why every
    render of this number states what it counted in the same sentence.
    """
    try:
        want = os.path.realpath(path)
        seats, naming, opaque = set(), set(), set()
        unreadable = 0
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            env, err = _environ_read(int(entry))
            if err:
                # A FAILED READ IS NOT AN EMPTY ENVIRONMENT. roguescan's reader
                # fails open to {}, which here would silently turn unreadable
                # processes into "no seat there" and let the census report a
                # confident 0 of 0.
                unreadable += 1
                continue
            name = env.get("HELM_CHAT_NAME")
            if not name:
                continue
            seats.add(name)
            theirs = env.get("HELM_CELL_BIN")
            if not theirs:
                continue
            # ANOTHER SEAT'S BARE NAME IS NOT OURS TO RESOLVE. `_resolve`
            # would answer with THIS process's PATH and cwd, so a seat whose
            # bare `dregg-client-sign` resolves to /b/signer in its own
            # context would be counted against our /a/signer. Only an
            # ABSOLUTE path can be compared without borrowing our context;
            # everything else is recorded as opaque and stated as a limit.
            if not os.path.isabs(theirs):
                opaque.add(name)
                continue
            if os.path.realpath(theirs) == want:
                naming.add(name)
        return {"naming": len(naming), "seats": len(seats),
                "opaque": len(opaque), "unreadable": unreadable, "err": None}
    except Exception as exc:                      # noqa: BLE001
        # A COUNT THAT CANNOT BE TAKEN MUST NOT BECOME A ZERO. Zero reads as
        # "nothing is affected", which is the opposite of what an unreadable
        # census means.
        return {"naming": None, "seats": None, "opaque": None,
                "unreadable": None, "err": exc.__class__.__name__}


def _environ_read(pid):
    """(env, err) — a process environment WITH its read status. Deliberately
    not `roguescan._read_environ`, which fails open to {}: correct for a
    scanner asking "does this look rogue", wrong for a census that must not
    report a confident zero over processes it could not read."""
    try:
        with open("/proc/%d/environ" % pid, "rb") as f:
            raw = f.read()
    except FileNotFoundError:
        return {}, None                            # exited mid-scan, not a gap
    except PermissionError:
        # A DENIED READ DOES NOT NAME AN OWNER. EPERM here can mean another
        # user's process, and it can equally mean a SAME-UID process that is
        # non-dumpable or fenced by ptrace policy — so treating every denial
        # as "not ours" silently drops processes that could be helm seats,
        # which is the false-zero this census exists to avoid, wearing a
        # narrower costume.
        #
        # OWNERSHIP IS ESTABLISHED POSITIVELY OR NOT AT ALL. The /proc entry's
        # own owner is readable when its environ is not, so a FOREIGN uid is a
        # fact and excludes the process; our uid, or an unreadable owner, is a
        # real gap and is counted as one.
        try:
            owner = os.stat("/proc/%d" % pid).st_uid
        except OSError:
            return {}, "PermissionError"          # owner unknown => a gap
        if owner != os.getuid():
            return {}, None                        # foreign, positively
        return {}, "PermissionError"               # ours and unreadable
    except OSError as exc:
        return {}, exc.__class__.__name__
    env = {}
    for tok in raw.split(b"\0"):
        if b"=" in tok:
            k, _, v = tok.partition(b"=")
            env[k.decode("utf-8", "replace")] = v.decode("utf-8", "replace")
    return env, None


def bin_ready():
    """True only when the configured signer is a real executable file."""
    return bin_status()["usable"]


class BinTimeout(str):
    """A launched subprocess exceeded its budget; its outcome is unknown.

    Keep the three-value run_bin contract and string diagnostics compatible,
    but let callers distinguish this from a binary that never launched.
    """


def profiles_dir():
    """The dregg SDK's profile store, by the SDK's own rule
    (`dregg_sdk::profiles::profiles_dir`): `$DREGG_HOME/profiles` when
    DREGG_HOME is set at all, else `$HOME/.dregg/profiles`, with `.` standing
    in for an unset HOME. The rule is identical in the fee-loop build and the
    rebased one.

    helm read DREGG_PROFILES_DIR here, a variable no dregg build honours. A
    value set there pointed helm at one directory while the signer it launched
    read its keys from another, so "which local identity owns this cell"
    could answer from profiles the signer never uses."""
    if "DREGG_HOME" in os.environ:
        return os.path.join(os.environ["DREGG_HOME"], "profiles")
    return os.path.join(os.environ.get("HOME", "."), ".dregg", "profiles")


def profile_public_keys():
    """{public_key_hex: profile name} for every readable SDK profile.

    THE PUBLIC HALF, AND ONLY THE PUBLIC HALF. A profile file holds `seed_hex`
    beside `public_key_hex`; nothing here returns, prints or stores the seed,
    and what the caller receives is a mapping that cannot carry one.

    This is the instrument that answers WHICH LOCAL IDENTITY OWNS A FUNDED CELL
    without asking any key to prove it. The node reports each cell's public key
    on `/api/cell/{id}`, so a cell whose key appears here is a cell helm holds
    the key for — which is the difference between "helm has no credential" and
    "helm has the credential and the signer has no verb that spends it"."""
    out = {}
    d = profiles_dir()
    try:
        names = sorted(os.listdir(d))
    except OSError:
        return out
    for n in names:
        if not n.endswith(".json"):
            continue
        try:
            with open(os.path.join(d, n), encoding="utf-8") as f:
                rec = json.load(f)
        except (OSError, ValueError):
            continue
        pkh = rec.get("public_key_hex") if isinstance(rec, dict) else None
        if isinstance(pkh, str) and pkh:
            out[pkh.lower()] = str(rec.get("name") or n[:-5])
    return out


def signer_supports(verb):
    """Does the CONFIGURED signer expose `verb`? True, False, or None.

    MEASURED FROM THE BINARY'S OWN USAGE TEXT, which it prints (and exits 0)
    when run with no arguments — never inferred from a version string or a
    build date, both of which have already lied about this signer once.

    None means helm could not ask: a signer that is absent, unusable or silent
    is UNKNOWN, and a caller must not read that as "the verb is missing"."""
    rc, out, err = run_bin([], timeout=20)
    if rc is None:
        return None
    text = ((out or "") + "\n" + (err or "")).strip()
    if not text:
        return None
    for line in text.splitlines():
        head = line.strip().split(" ", 1)[0].split("[", 1)[0]
        if head == verb:
            return True
    return False


def run_bin(args, timeout=90, env_extra=None):
    """Run the OPTIONAL cell binary captured. (rc, stdout, stderr); rc None +
    reason on launch failure or timeout (a BinTimeout, never proof of no send).
    env_extra lays over the mapped env."""
    b = bin_path()
    status = bin_status()
    if not status["usable"]:
        return None, "", "a2a transport unavailable — " + status["reason"]
    env = build_env()
    env.update(env_extra or {})
    try:
        p = subprocess.run([b] + args, env=env, capture_output=True,
                           text=True, timeout=timeout)
    except OSError as exc:
        detail = exc.strerror or exc.__class__.__name__
        return None, "", "configured signer could not execute (%s)" % detail
    except subprocess.TimeoutExpired:
        # Partial output may contain credentials or payloads. Preserve only
        # the known boundary, not arbitrary stdout/stderr from the child.
        return None, "", BinTimeout(
            "cell %s timed out after %ss; subprocess launched, outcome unknown"
            % (args[0], timeout))
    return p.returncode, p.stdout, p.stderr


def _last_json(out):
    """The a2a binary's stdout contract: one JSON object line (progress goes to
    stderr). Parse the last JSON-looking stdout line, fail-open."""
    for line in reversed((out or "").strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except ValueError:
                return None
    return None


def _status(args):
    url = node_url()
    receipts = get_json(url + "/api/receipts")
    live = receipts is not None
    if isinstance(receipts, list) and receipts:
        h = receipts[0]
        print("helm cell: node LIVE at %s — chain head %s (finality %s)"
              % (url, h.get("chain_index"), h.get("finality")))
    elif live:
        print("helm cell: node LIVE at %s — no receipts yet" % url)
    else:
        print("helm cell: node UNREACHABLE at %s (attestation is native + "
              "offline; the node is only an optional anchor)" % url)
    b = bin_path()
    signer = bin_status()
    if signer["usable"]:
        print("helm cell: optional a2a binary " + b)
    elif signer["configured"]:
        print("helm cell: optional a2a signer UNAVAILABLE — %s" % signer["reason"])
    else:
        print("helm cell: optional a2a transport OFF (no HELM_CELL_BIN) — "
              "attestation does not need it")
    return 0 if live and (not signer["configured"] or signer["usable"]) else 1


def cmd_cell(args):
    """cell <status|join|send> — node liveness + the OPTIONAL a2a transport
    (degrades with no HELM_CELL_BIN)."""
    args = list(args)
    if not args:
        print(_USAGE, file=sys.stderr)
        return 2
    verb, rest = args[0], args[1:]
    if verb == "status":
        return _status(rest)
    if verb not in PASS_VERBS:
        print("helm cell: unknown verb '%s'" % verb, file=sys.stderr)
        print(_USAGE, file=sys.stderr)
        return 2
    b = bin_path()
    signer = bin_status()
    if not signer["usable"]:
        print("helm cell: a2a transport unavailable — %s" % signer["reason"],
              file=sys.stderr)
        return 1
    env = build_env()
    # THE PASSTHROUGH ASKS THE SAME GATE AS A POST, when the signer would sign
    # as the OWNER on the environment's say-so alone. `build_env` maps
    # HELM_CELL_PROFILE onto DREGG_PROFILE, and dregg-client-sign reads that
    # var whenever argv carries no `--profile` — so a bare `helm cell send`
    # from a seat that inherited the owner's export signed as the OWNER, with
    # no refusal anywhere (task/3049). Now a seat signs as itself, a process
    # the gate refuses is refused here too, and a process that is not a seat
    # (the owner's own terminal) is unchanged. `--profile <name>` in argv is
    # stated intent and the escape hatch; the signer reads it over the env.
    if "--profile" not in rest:
        mapped = (env.get("DREGG_PROFILE") or "").strip()
        if is_owner_cell(mapped):
            profile, refusal = signing_identity(ambient=mapped)
            if refusal:
                print("helm cell: refusing to %s as the owner's profile %r "
                      "on the environment's say-so — %s (pass --profile "
                      "<name> to speak for someone deliberately)"
                      % (verb, mapped, refusal), file=sys.stderr)
                return 1
            env["DREGG_PROFILE"] = env["MELD_AGENT_PROFILE"] = profile
    try:
        return subprocess.call([b, verb] + rest, env=env)
    except OSError as exc:
        print("helm cell: a2a transport unavailable — %s" % exc, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
