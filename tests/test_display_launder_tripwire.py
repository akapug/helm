#!/usr/bin/env python3
"""Cross-MODULE display-launder tripwire — END the ESC/bidi class for good.

This test enumerates OUTPUT SINKS across MODULES, not fields of one report.
That distinction is the whole point. The r1..r5 rounds each hand-patched the
seats.py surfaces they already knew about, and the class kept reopening
because a roster KEY — seats.roster(), a roster dict key, or a pane's
HELM_CHAT_NAME — reaches sinks (stdout/stderr, JSON response bodies, HTML
renders) in OTHER modules (todos.py, codexhomes.py, web.py, hooks.py) that no
seats.py-scoped guard (tests/test_presence.py) could ever see. A hostile
HELM_CHAT_NAME is UNVALIDATED at the join seam, so the raw key may still drive
internal matching — only the EMITTED value must be laundered.

Two tripwires, deliberately BOTH — one proves today, one guards tomorrow:

  A. SOURCE-DRIVEN grep tripwire (test_every_roster_consumer_is_allowlisted):
     greps EVERY helm/*.py caller of seats.roster() and asserts each caller
     module is in an allowlist carrying a REASON (laundered-before-emit, or
     internal-matching-only with no sink). A NEW module that consumes the
     roster FAILS this test until someone registers it with a justification —
     that is the forcing function that stops the 6th surface from being born
     in a module nobody thought to guard.

  B. RUNTIME sweep (test_no_roster_sink_leaks_the_planted_payload): plants a
     hostile HELM_CHAT_NAME (lane\x1b[2J‮pwn, plus a codex-… twin so the
     codex-only readout is exercised) on the roster and DRIVES every
     roster-consuming verb/endpoint across all four modules, asserting NO
     ESC/bidi reaches any stdout/stderr or JSON body. Proves the surfaces are
     clean at HEAD; the grep tripwire keeps them that way.
"""
import ast
import contextlib
import io
import os
import re
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import chat, codexhomes, home, hooks, human, meld, pk, seats, todos, web  # noqa: E402,E501

# the two payload markers every sink must strip: a screen-clear CSI (Cc) and a
# right-to-left override (Cf, reorders the whole rendered line).
ESC, BIDI = "\x1b", "‮"

HELM_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(HELM_DIR, "helm")


# ── A. the source-driven grep tripwire ──────────────────────────────────────
#
# The allowlist IS the law: every module that CALLS seats.roster() must appear
# here with a reason that is EITHER "launders every emitted key" OR
# "internal-matching-only, no sink". A module absent from this map fails the
# test — so a new roster consumer cannot ship in an unguarded module without a
# human writing down HOW it stays inert. Keyed by module basename; the reason
# is the reviewer's contract, verified for real by the runtime sweep below.
_ROSTER_CONSUMERS = {
    "seats_cli.py": (
        "INTERNAL-MATCHING-ONLY: the CLI dispatcher reads the roster to "
        "resolve a verb's target seat — a lookup of a name the operator "
        "already typed. Anything it PRINTS goes through the report module's "
        "_seat_label; this module renders no roster key itself. Relocated "
        "verbatim from seats.py by the split."),
    "seats_work_offer.py": (
        "INTERNAL-MATCHING-ONLY: the offer rung reads the roster once to ask "
        "which seats are live enough to be offered work — a membership "
        "question whose answer becomes a claim on a lane, never display "
        "text. What the whisper emits is a lane name and a verb. Relocated "
        "verbatim from seats.py by the split."),
    "seats_report.py": (
        "LAUNDERED, and this is THE emission site — the CLI roster table, "
        "the /api/chat/roster payload and the seats panel all render from "
        "here, so it is the last place a roster KEY can become display "
        "text. Every seat name leaving this module goes through _seat_label; "
        "_pub_row and _ROW_CAPS bound what else escapes. The GC reads the "
        "same rows to decide reapability and emits only a reason string. "
        "Relocated verbatim from seats.py by the split — this entry is the "
        "reviewer contract for the surface, not a formality."),
    "seats_ack.py": (
        "INTERNAL-MATCHING-ONLY: the ack ladder reads the roster to decide "
        "whether a row's recipient IS a seat and which lanes it should scan "
        "— membership questions about names the caller already holds. What "
        "leaves is a per-(recipient, row) state and a count. Relocated "
        "verbatim from seats.py by the split."),
    "seats_stop_signals.py": (
        "INTERNAL-MATCHING-ONLY: the stop ladder reads the roster once, to "
        "ask whether THIS seat's own row is present and armed before it "
        "decides whether an idle stop is allowed. It is a membership "
        "question about a name the process already holds; nothing keyed by "
        "roster leaves. Relocated verbatim from seats.py by the split."),
    "seats_delivery.py": (
        "INTERNAL-MATCHING-ONLY: the delivery path reads the roster to decide "
        "WHO A ROW REACHES — resolve_recipient, recipient_capability, "
        "_deliver_unpaused — and the keys it reads are compared, never "
        "emitted. What leaves is the row text and a cursor position; the one "
        "place a seat name is rendered for a human is _seat_label, which "
        "launders. Relocated verbatim from seats.py by the split."),
    "seats_roster.py": (
        "THE ROSTER'S OWN MODULE, relocated by the seats.py split: this is "
        "where roster() is READ to write a row, beat presence, evict a dead "
        "session, and run the identity-admin verbs (disown/rename/rehome/"
        "mute). Not one line changed in the move. It is the writer as well "
        "as the reader, so its keys never leave as display text — the CLI "
        "table that DOES emit them is roster_report, which stays in the "
        "report section and carries its own entry."),
    "seats_identity.py": (
        "SAME CODE, NEW FILE: this is seats.py's OWN identity/addressing "
        "roster access, relocated by the seats.py split and not one line "
        "changed. It reads the roster to answer who a session IS "
        "(auto_name, _live_session_of) and who a word is FOR (seat_scope, "
        "_delivery_pause) — the same two questions the module has always "
        "answered, with the same laundering discipline seats.py already "
        "carried. Registered rather than exempted because a split must not "
        "be a way to leave an allowlist: if these reads were safe under the "
        "old filename they are safe under the new one, and if they were not, "
        "the split is exactly when someone should have to say so."),
    "cell.py": (
        "INTERNAL-MATCHING-ONLY: `derived_seat` reads the roster to answer ONE "
        "boolean about a name it already holds — is this row a FLEET ACTOR "
        "(home_room set) rather than an observed session? The name comes from "
        "`meld._self_seat`, never from a roster key; the read is `.get(name)`, "
        "a membership lookup whose miss returns \"\"; and the only value that "
        "leaves is that same already-held name. No roster key is ever bound, "
        "so none can reach a sink. It emits nothing itself — its caller uses "
        "the answer to decide whether to SIGN or refuse."),
    "chat.py": (
        "INTERNAL-MATCHING-ONLY: restore_journal's cursor sweep reads roster "
        "KEYS to mint end-of-file cursors for cursor-less seats (the "
        "restore-storm guard). No key reaches a sink — the CLI report prints "
        "room names and a minted COUNT. The cursor files are keyed by "
        "seats._seat_key exactly as delivery's own writes are."),
    "dispatches.py": (
        "INTERNAL-MATCHING-ONLY: two sites, both via roster_checked(). "
        "_open_for's sweep passes the roster to _mine_or_unprovable as a "
        "MEMBERSHIP predicate to decide which rows are visible; no key is "
        "returned or printed. The recipient-runtime read does `roster.get("
        "recipient)` and any name in its refusals is the CALLER'S recipient "
        "argument, never a roster key."),
    "fleet.py": (
        "LAUNDERED via seats._seat_label in _seat_for — the ONE boundary "
        "where a censused pid gains a seat name, covering BOTH sources "
        "(HELM_CHAT_NAME out of a process environ, and the roster key matched "
        "by sid). rows() prints that into %-18s, the first and widest column "
        "of the census. It was RAW until the AST scan landed: fleet.py held zero "
        "scrub/label calls, and the regex tripwire could not see it because "
        "fleet obtains the roster through roster_checked()."),
    "orcaadopt.py": (
        "TWO SITES, DIFFERENT REASONS. (a) INTERNAL-MATCHING-ONLY: "
        "`roster.get(seat)` reads the row's session and sessions SIDS for pane "
        "adoption; the seat key is the caller's own argument, used to index. "
        "(b) LAUNDERED-BEFORE-EMIT since the session join: `roster_current_sids()` "
        "reads roster KEYS, and the session join hands the winning key out as "
        "a pane row's `seat` — which `seat._panes` prints in its first and "
        "widest column. That key is now scrubbed at the sink through "
        "`seats._seat_label`, the same launder every roster-borne display "
        "string clears. The old reason for this module — 'no roster key is "
        "emitted' — stopped being true the moment the listing learned the "
        "session join, and this pin is what said so."),
    "proxywatch.py": (
        "THREE INTERNAL-MATCHING-ONLY SITES. health() looks up launch-owned "
        "runtime metadata by the watched seat name it already holds. "
        "_roster_session() resolves that caller-owned name and uses matching "
        "keys only to select one exact session. _roster_identity_for_session() "
        "selects one key for an already-held session and hands it back only as "
        "the next internal lookup argument. No roster key reaches a report, "
        "proof, or refusal; health emits its pre-existing watched-seat value."),
    "resumeturn.py": (
        "INTERNAL-MATCHING-ONLY: uses the roster for `seat_name in roster` "
        "MEMBERSHIP to decide whether a seat is known. The name in its "
        "operator message is the seat_name PARAMETER it was called with, not "
        "a key read out of the roster."),
    "todos.py": (
        "LAUNDERED: fleet()/_row run the seat KEY + project through _lbl "
        "(_scrub + _clip) before the fleet table AND the /api/todos JSON; "
        "the todo TEXT is scrubbed in digest()."),
    "codexhomes.py": (
        "LAUNDERED: _print_capacity emits the live codex seats through "
        "seats._seat_label."),
    "beacons.py": (
        "FOUR READERS, AND EXACTLY ONE OF THEM ITERATES. `roll()` walks "
        "`sorted(rows.items())` across the whole roster to decide which seats "
        "the census may speak about — the only site here that binds a roster "
        "KEY — and every name it admits reaches the report through "
        "beacons.label -> seats._seat_label. This module carries a SECOND "
        "unvalidated source no other consumer has — a seat name read "
        "from ANOTHER PROCESS's `--seat` argv or environ, which passed no "
        "join seam at all — and that one is laundered where it ENTERS the "
        "row (classify), not left to whoever prints it. The attendance "
        "register adds THREE more sites — attend, escalate and _ack_alerts — "
        "that read the roster to write it, or to revalidate a delivery batch "
        "against it, under a lock. Those three are the WEAKEST kind of "
        "consumer and deliberately so: NONE of them iterates the roster. Each "
        "walks a list it was HANDED — the census rows, the transition rows — "
        "and touches the register only as `r.get(seat)`, a membership test "
        "whose miss is a SKIP, never a mint (escalate's revalidation reads "
        "one attendance dict per handed row and emits nothing at all). No "
        "roster key is bound to a name in any of the three, so none can reach "
        "a sink; every name they emit is the census's, already laundered "
        "upstream. THE RECOUNT THAT WROTE THIS PARAGRAPH: the previous "
        "version named two register sites (omitting escalate) and then said "
        "all four readers use `r.get(seat)` — false of roll(), the one that "
        "iterates. BeaconsRosterReaderShapeTest below now holds the "
        "structural half of this claim, so a rationale can no longer describe "
        "a shape the code does not have."),
    "web:_api_chat": (
        "LAUNDERED: the roster @mention list routes through _seat_label and "
        "the sidebar rows through _room_seats/_rooms_summary."),
    "web:_rooms_summary": (
        "LAUNDERED: every room sidebar row is built through _room_seats; the "
        "semantic function key survives the web.py physical split."),
    "web:_dm_seat_map": (
        "INTERNAL-MATCHING + LAUNDERED-at-emit: builds {state-file key: seat} "
        "so DM lanes resolve to their recipient. The only roster KEY "
        "that leaves is the sidebar `seat` label, laundered via "
        "seats._seat_label at its ONE emit site in _rooms_summary — exercised "
        "by the runtime sweep's planted hostile-seat DM lane. The POST path "
        "consumes the raw name solely as seats.dm's addressee, which "
        "re-validates it as an exact token."),
    "hooks.py": (
        "LAUNDERED: surface_uncovered launders BOTH the printed pane-name "
        "column AND the interpolated reason via seats._seat_label."),
    "seat.py": (
        "INTERNAL-MATCHING-ONLY: repr(%r)-echoes the OPERATOR-SUPPLIED launch "
        "arg — it never emits a stored roster KEY to a raw sink."),
    "seat_health.py": (
        "INTERNAL-MATCHING-ONLY: counts live codex instances via last_seen; "
        "it emits only the numeric count, never a stored roster KEY."),
    "seat_identity.py": (
        "INTERNAL-MATCHING-ONLY: reads roster KEYS into the canonicals set "
        "for exact-match/classification; an emitted refusal string carries "
        "the OPERATOR-typed token and a canonical name that is itself "
        "operator-facing addressing (the suggestion IS the point), never a "
        "stored display field."),
}

# COUNT-PIN per module: module-granularity alone let a NEW roster() call site
# slip into an ALREADY-allowlisted module unreviewed (r6 was module-only). Pin
# the exact number of call sites per module so a new one — even in an
# allowlisted module — trips the wire until a human re-counts AND confirms the
# new site launders. Regenerate deliberately with _roster_call_sites() below.
_ROSTER_CALL_COUNTS = {
    "beacons.py": 4,       # ONE ITERATING reader + THREE membership-only.
                           # roll()'s present-seat filter WALKS the roster —
                           # `for name, row in sorted(rows.items())` — and is
                           # the only site here that binds a roster KEY; every
                           # name it admits is emitted via label(). The other
                           # three are attend() and _ack_alerts(), the
                           # attendance register's two read-modify-write
                           # sites, plus escalate()'s delivery revalidation
                           # (the delivery-edge cure and its opposite-edge
                           # amendment). Those three never iterate: each walks
                           # a list it was HANDED (the census rows / the
                           # transition rows) and uses `r.get(seat)` as a pure
                           # MEMBERSHIP test — the revalidation reads one
                           # attendance dict per handed row and emits nothing
                           # — so a roster key is never bound, never emitted,
                           # and a seat absent from the register is skipped
                           # rather than minted. Every name that leaves them —
                           # out["written"], the transition tuples — is the
                           # CENSUS's name, laundered where the census laundered
    "cell.py": 1,          # derived_seat's fleet-actor check: ONE `.get(name)`
                           # membership lookup on a name it already holds; the
                           # miss returns "" and no key is ever bound
    "chat.py": 1,          # restore_journal's cursor sweep — reads KEYS into
                           # the end-of-file mint loop, INTERNAL-MATCHING-ONLY
                           # (the CLI prints a minted COUNT, never a key)
    "codexhomes.py": 1,
    "dispatches.py": 2,   # BOTH via roster_checked(), invisible to the old
                          # regex: _open_for's visibility filter and the
                          # recipient-runtime read. INTERNAL-MATCHING-ONLY.
    "fleet.py": 1,        # via roster_checked(). LAUNDERED at _seat_for —
                          # it printed the raw key into the census's first
                          # column until the AST scan saw it.
    "hooks.py": 2,        # +1 (was 1): the second is roster_checked(), which
                          # the regex `\broster\(\)` could not match at all.
    "orcaadopt.py": 2,    # +1 (was 1): roster_current_sids(), the ADDRESSING
                          # half of the roster read once per pane listing. The
                          # original site stays INTERNAL-MATCHING-ONLY; the new
                          # one EMITS (see its reason above) and is laundered
                          # at the sink in seat._panes.
    "proxywatch.py": 3,  # health() plus the session->identity and identity->
                          # session proof joins. All are INTERNAL-MATCHING-ONLY;
                          # no selected roster key reaches a report or proof.
    "resumeturn.py": 1,   # via roster_checked(); `seat_name in roster`
                          # MEMBERSHIP only. INTERNAL-MATCHING-ONLY.
    "seat.py": 1,
    "seat_health.py": 1,
    "seat_identity.py": 1,  # _canonical_sources: roster KEYS -> the
                            # canonicals membership set; INTERNAL-MATCHING-
                            # ONLY (refusals emit operator-facing names)
    # SPLIT, NOT DRIFT — and the arithmetic is the proof. The seats.py split
    # moved four identity/addressing call sites into seats_identity.py:
    # seats.py 28 -> 24, seats_identity.py 0 -> 4, TOTAL 52 -> 52. Not one
    # site was added, removed, or edited, so no site needs re-reviewing for
    # laundering; the conservation of the total is what says so. A pin
    # updated without that check is a rubber stamp.
    "seats_identity.py": 4,
    # SPLIT, NOT DRIFT (second wave): seven sites moved to seats_roster.py.
    # seats.py 24 -> 17, seats_roster 0 -> 7, TOTAL 52 -> 52 — conserved,
    # so nothing needs re-reviewing for laundering.
    "seats_roster.py": 7,
    # third wave: three sites to seats_delivery. 17 -> 14, 0 -> 3, 52 -> 52.
    "seats_delivery.py": 3,
    # fourth wave: one site to seats_stop_signals. 14 -> 13, 0 -> 1, 52 -> 52.
    "seats_stop_signals.py": 1,
    # fifth wave: four sites to seats_ack. 13 -> 9, 0 -> 4, 52 -> 52.
    "seats_ack.py": 4,
    # sixth wave: seven sites to seats_report — the rendering half. seats.py
    # 9 -> 2, seats_report 0 -> 7, TOTAL 52 -> 52.
    "seats_report.py": 7,
    # seventh wave: one site to seats_work_offer. 2 -> 1, 0 -> 1, 52 -> 52.
    "seats_work_offer.py": 1,
    # seats.py IS GONE FROM THIS PIN, and its absence is the finish
    # line: the facade reads the roster ZERO times now. An allowlist
    # entry for a module that no longer consumes would rot into a
    # vouch for a call site that does not exist.
    "seats_cli.py": 1,
                           # rotation's paused-cursor hold. Both are INTERNAL-
                           # MATCHING-ONLY: they index/iterate keys solely to
                           # choose family + cursor state; no raw key reaches a sink.
                           # +1 (was 25): roster_checked() at 2431, which
                           # the regex could not match. The earlier +1 (24
                           # -> 25) was _live_session_of, the claim-jump
                           # occupancy read. INTERNAL-MATCHING-ONLY — it
                           # returns a SESSION ID, never a name, and the one
                           # sentence that renders it (_dispute_sentence) is a
                           # refusal shown to the disputing process about its
                           # OWN claimed seat, so no third party's identity is
                           # emitted. Counted deliberately: the r2 predicate
                           # could not ask "is this name occupied" without it.
                           # main's 19 (+3 ack-ladder, +1 _is_seat) + work-offer's
                           # _live_seats() poaching-filter read (internal-only)
                           # + honest-presence's identity index: 3 reads
                           #   (session_owners / unverified_seats emit SIDS+seat
                           #    keys, laundered downstream by _pub_row's per-field
                           #    caps; disown_session is INTERNAL-MATCHING-ONLY and
                           #    its one emitted label goes through _seat_label)
                           # + owes_beacon()'s registration test: INTERNAL-MATCHING-
                           #   ONLY (seat in roster() -> bool, key never reaches a
                           #   sink); the name its caller EMITS is chat_name()'s
                           #   validated return, not a roster key.
    "todos.py": 1,
    "web:_api_chat": 1,
    "web:_dm_seat_map": 1,
    "web:_rooms_summary": 1,
}

# EVERY accessor that hands a caller the roster mapping. `roster()` is the
# fail-OPEN reader; `roster_checked()` returns the SAME mapping plus a probe
# flag. Counting only `roster()` made four live consumers — dispatches.py,
# fleet.py, orcaadopt.py, resumeturn.py — structurally INVISIBLE to a guard
# whose whole claim is that it is total across the tree. That blindness is not
# bookkeeping: fleet.py obtains the roster through `roster_checked()` and, at
# the time of writing, prints the resulting KEY raw to the operator's terminal
# and raw into `fleet --json` (a P0 finding). The tripwire built to make that class
# un-reopenable could not see the module where it reopened.
_ROSTER_GETTERS = ("roster", "roster_checked")


class RosterAccessEscape(Exception):
    """A roster accessor was BOUND OR RESOLVED rather than called outright.

    The counter can follow `x.roster()`. It cannot follow `fn = seats.roster`
    (called later under any name), `getattr(seats, "roster")()`, or
    `from .seats import roster as _r`. Each of those is a real roster consumer
    that the count-pin would score as ZERO — a silent hole precisely where the
    guard is supposed to be total, and indistinguishable from a module that
    genuinely stopped consuming the roster. So the counter FAILS CLOSED: an
    escape raises instead of quietly counting nothing."""


def _called_name(func):
    """The bare attribute/name being called, or None for a computed callee."""
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _package_sources(pkg):
    """[(abspath, module-key)] for every .py under pkg, RECURSIVELY.

    The predecessor ran `os.listdir(PKG)`, so it saw only the top level while
    helm/ carries seven real subpackages (work, inject, cred, store, premise,
    clarity, configs). A roster consumer in any of them was invisible. No
    subpackage consumes the roster today — this closes a LATENT hole, and the
    hermetic fixtures below are what prove the closure, since the live tree
    cannot.

    The module key stays the BASENAME for a top-level file, so every existing
    pin in _ROSTER_CALL_COUNTS keeps its meaning; a nested file keys on its
    path relative to pkg (e.g. "work/claim.py")."""
    out = []
    for root, dirs, files in os.walk(pkg):
        dirs[:] = sorted(d for d in dirs if d != "__pycache__")
        for fn in sorted(files):
            if not fn.endswith(".py"):
                continue
            path = os.path.join(root, fn)
            out.append((path, os.path.relpath(path, pkg).replace(os.sep, "/")))
    return sorted(out, key=lambda pair: pair[1])


_WEB_SPLIT_MODULES = frozenset({
    "web.py", "web_cache.py", "web_chat.py", "web_chat_rooms.py",
    "web_common.py", "web_configs.py", "web_core.py", "web_land.py",
    "web_land_model.py", "web_ledger.py", "web_multiplayer.py",
    "web_quota.py", "web_roster.py", "web_server.py", "web_sessions.py",
    "web_sse.py",
})


def _semantic_source_owner(module, tree, lineno):
    """Stable web owner key across a physical module split."""
    if module not in _WEB_SPLIT_MODULES:
        return module
    for node in tree.body:
        if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef))
                and node.lineno <= lineno <= node.end_lineno):
            return "web:" + node.name
    return module


def _refuse_escapes(module, tree):
    """Raise unless every roster accessor in this module is called outright."""
    nodes = list(ast.walk(tree))
    called = {id(node.func) for node in nodes if isinstance(node, ast.Call)}
    bare = {
        alias.name
        for node in nodes if isinstance(node, ast.ImportFrom)
        for alias in node.names
        if alias.name in _ROSTER_GETTERS and not alias.asname
    }
    for node in nodes:
        if (isinstance(node, ast.Name) and node.id in bare
                and id(node) not in called):
            raise RosterAccessEscape(
                "%s:%d binds `%s` without calling it — the count-pin cannot "
                "see where it is invoked. Call the accessor at its use site."
                % (module, node.lineno, node.id))
        if (isinstance(node, ast.Attribute) and node.attr in _ROSTER_GETTERS
                and id(node) not in called):
            raise RosterAccessEscape(
                "%s:%d binds `.%s` without calling it — the count-pin cannot "
                "see where it is invoked. Call the accessor at its use site."
                % (module, node.lineno, node.attr))
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "getattr" and len(node.args) >= 2
                and isinstance(node.args[1], ast.Constant)
                and node.args[1].value in _ROSTER_GETTERS):
            raise RosterAccessEscape(
                "%s:%d resolves `%s` through getattr — a dynamic callable the "
                "count-pin cannot see. Call the accessor directly."
                % (module, node.lineno, node.args[1].value))
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                if alias.name in _ROSTER_GETTERS and alias.asname:
                    raise RosterAccessEscape(
                        "%s:%d imports `%s` under the alias `%s` — calls to it "
                        "carry a name the count-pin cannot see. Import the "
                        "module and call `seats.%s()`."
                        % (module, node.lineno, alias.name, alias.asname,
                           alias.name))


def _roster_call_sites(pkg=PKG):
    """[(module, lineno, text)] for every site under pkg that OBTAINS the roster.

    AST, NOT A LINE REGEX. The predecessor grepped `\\broster\\(\\)` over raw
    lines, so a DOCSTRING or COMMENT that merely MENTIONED roster() counted as
    a call site. That is not theoretical: it cost a live lane a full gate
    cycle — seats.py read 27 sites against a pin of
    25 and TWO of the three "new" lines were prose. The documented remedy
    ("verify each site launders, then update _ROSTER_CALL_COUNTS") would have
    written two call sites THAT DO NOT EXIST into the list this file calls the
    law, permanently weakening a real guard by two. A counter that punishes any
    module for DOCUMENTING its own roster use punishes exactly the code most
    worth commenting.

    Matching a Call node instead of a string is exact and needs no exclusion
    list: `def roster(...)` is a FunctionDef, and write_roster/gc_roster/
    roster_report/roster_path are simply different names. It is also stricter
    in the right direction — a call split across lines is now SEEN, where the
    empty-parens regex could not see it.

    THE AST/REGEX DIVERGENCE IS PINNED, NOT ASSERTED IN PROSE. The measurement
    this docstring used to cite ("AST and regex agree on all nine allowlisted
    modules") was true and proved nothing: agreement on the live tree means a
    revert to the regex leaves the suite identically green. The predecessor is
    kept executable as `_regex_call_sites` below and the hermetic fixtures in
    ASTvsRegexDiscriminationTest assert the two DISAGREE, so reverting this
    function goes RED.

    `pkg` is a parameter for exactly that reason — the divergence, the
    recursion, the alias refusal and the parse refusal are all provable only on
    a tree built to contain them, never on helm/ as it happens to stand today.

    A module that fails to parse RAISES. Skipping it would let an unparseable
    module hide a roster consumer from the allowlist — a hole exactly where the
    guard is supposed to be total."""
    sites = []
    for path, module in _package_sources(pkg):
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        tree = ast.parse(text, filename=path)
        lines = text.splitlines()
        _refuse_escapes(module, tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if _called_name(node.func) in _ROSTER_GETTERS:
                owner = _semantic_source_owner(module, tree, node.lineno)
                sites.append(
                    (owner, node.lineno, lines[node.lineno - 1].strip()))
    return sites


# The PREDECESSOR counter, kept EXECUTABLE and unused by the guard. It exists
# only so the tests below can assert the new counter disagrees with it on a
# tree built to make them disagree. Delete it and the discrimination tests stop
# being able to tell an AST counter from a grep.
_ROSTER_CALL_RE = re.compile(r"\broster\(\)")


def _regex_call_sites(pkg=PKG):
    """[(module, lineno, text)] the way the line regex saw it — the CONTROL arm."""
    sites = []
    for fn in sorted(os.listdir(pkg)):
        if not fn.endswith(".py"):
            continue
        with open(os.path.join(pkg, fn), encoding="utf-8") as fh:
            for i, line in enumerate(fh, 1):
                if line.lstrip().startswith("def roster("):
                    continue                     # the definition, not a call
                if _ROSTER_CALL_RE.search(line):
                    sites.append((fn, i, line.strip()))
    return sites


class ASTvsRegexDiscriminationTest(unittest.TestCase):
    def setUp(self):
        self.pkg = tempfile.mkdtemp(prefix="helm-roster-ast-")

    def tearDown(self):
        shutil.rmtree(self.pkg, ignore_errors=True)

    def plant(self, relative, source):
        path = os.path.join(self.pkg, relative)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(source)
        return path

    def test_AST_counts_calls_while_regex_counts_prose(self):
        self.plant("surface.py", '''"""roster() in prose is not a call."""
# roster() in a comment is not a call either.
from helm import seats
value = seats.roster_checked()
''')
        ast_sites = _roster_call_sites(self.pkg)
        regex_sites = _regex_call_sites(self.pkg)
        self.assertEqual([(m, n) for m, n, _ in ast_sites],
                         [("surface.py", 4)])
        self.assertEqual(len(regex_sites), 2)
        self.assertNotEqual(ast_sites, regex_sites,
                            "a line-regex revert must go RED on this fixture")

    def test_AST_walks_subpackages_the_regex_never_enters(self):  # noqa: VACUOUS_ASSERTION — the exact nested AST site is the positive control on the same fixture whose top-level regex result must be empty
        self.plant("nested/consumer.py", "from helm import seats\nseats.roster()\n")
        self.assertEqual(
            [(m, n) for m, n, _ in _roster_call_sites(self.pkg)],
            [("nested/consumer.py", 2)])
        self.assertEqual(_regex_call_sites(self.pkg), [])

    def test_accessor_bindings_and_dynamic_resolution_fail_closed(self):  # noqa: VACUOUS_ASSERTION — each planted escape must raise RosterAccessEscape; four distinct syntax arms prove the refusal is exercised
        cases = {
            "attribute.py": "from helm import seats\nfn = seats.roster\nfn()\n",
            "bare.py": "from helm.seats import roster\nfn = roster\nfn()\n",
            "alias.py": "from helm.seats import roster as fn\nfn()\n",
            "getattr.py": "from helm import seats\ngetattr(seats, 'roster')()\n",
        }
        for name, source in cases.items():
            with self.subTest(name=name):
                shutil.rmtree(self.pkg, ignore_errors=True)
                os.makedirs(self.pkg)
                self.plant(name, source)
                with self.assertRaises(RosterAccessEscape):
                    _roster_call_sites(self.pkg)

    def test_an_unparseable_module_refuses_the_whole_scan(self):  # noqa: VACUOUS_ASSERTION — the planted invalid source must raise SyntaxError instead of disappearing from the total scan
        self.plant("broken.py", "this is not: python\n")
        with self.assertRaises(SyntaxError):
            _roster_call_sites(self.pkg)

    def test_web_semantic_owners_survive_physical_motion(self):
        source = ('from helm import seats\n'
                  'def _api_chat():\n'
                  '    seats.roster()\n'
                  '    row = {}\n'
                  '    return row.get("from")\n')
        path = self.plant("web.py", source)
        self.assertEqual([(m, n) for m, n, _ in _roster_call_sites(self.pkg)],
                         [("web:_api_chat", 3)])
        self.assertEqual([(m, n) for m, n, _ in
                          _from_field_read_sites(self.pkg)],
                         [("web:_api_chat", 5)])

        os.rename(path, os.path.join(self.pkg, "web_chat.py"))
        self.assertEqual([(m, n) for m, n, _ in _roster_call_sites(self.pkg)],
                         [("web:_api_chat", 3)])
        self.assertEqual([(m, n) for m, n, _ in
                          _from_field_read_sites(self.pkg)],
                         [("web:_api_chat", 5)])


class RosterConsumerAllowlistTest(unittest.TestCase):
    def test_every_roster_consumer_is_allowlisted(self):
        """Enumerates SINKS across MODULES: every helm/*.py caller of
        seats.roster() must be registered in _ROSTER_CONSUMERS with a reason.
        A new module that reads the roster and reaches a sink cannot pass
        until a human writes down how it launders — this is the tripwire that
        makes the class un-reopenable across the tree, not just in seats.py."""
        sites = _roster_call_sites()
        self.assertTrue(sites, "found no roster() call sites — regex rotted")
        offenders = sorted({m for m, _, _ in sites} - set(_ROSTER_CONSUMERS))
        self.assertFalse(
            offenders,
            "un-allowlisted roster consumer(s) %r — a roster KEY can reach a "
            "sink from a module no display-launder guard covers. Add each to "
            "_ROSTER_CONSUMERS with a reason (LAUNDERED via _pub_row/"
            "_seat_label, or INTERNAL-MATCHING-ONLY) AND, if it emits, wire it "
            "into the runtime sweep below.\n  sites: %s"
            % (offenders, [s for s in sites if s[0] in offenders]))

    def test_roster_call_site_counts_are_pinned(self):
        """COUNT-PIN, not just module-membership: a NEW roster() call site in an
        ALREADY-allowlisted module (the exact gap r6 left — module-granular
        guards can't see a fresh call site in a module already trusted) trips
        this until a human re-counts and confirms the new site launders."""
        from collections import Counter
        counts = Counter(m for m, _, _ in _roster_call_sites())
        actual = dict(counts)
        self.assertEqual(
            actual, _ROSTER_CALL_COUNTS,
            "roster() call-site counts drifted from the pin. A new call site "
            "(even in an allowlisted module) can reach a sink unreviewed — "
            "verify each site launders its emitted key, then update "
            "_ROSTER_CALL_COUNTS.\n  actual: %s\n  pinned: %s"
            % (actual, _ROSTER_CALL_COUNTS))

    def test_allowlist_has_no_stale_entries(self):
        """The allowlist cannot rot: every allowlisted module must still call
        roster(). A module that stopped consuming the roster (or was renamed)
        must be pruned so the reasons stay trustworthy."""
        live = {m for m, _, _ in _roster_call_sites()}
        stale = sorted(set(_ROSTER_CONSUMERS) - live)
        self.assertFalse(stale, "allowlist entries no longer call roster(): %r"
                         % stale)


# ── A2. the SHAPE pin: a rationale must not describe code that isn't there ──
#
# The allowlist above is prose a human wrote, and prose rots in the one way
# nothing else here can catch: beacons.py's reason claimed all four of its
# roster readers "touch the register only as `r.get(seat)`" while `roll()`
# has always walked `sorted(rows.items())` — the ITERATING reader, the one
# that binds a roster key, described as the weakest kind. It survived because
# membership and counts are checked and SHAPE was not. This pins the
# structural half of that paragraph: which readers iterate the mapping and
# which only probe it. Scoped to beacons.py deliberately — it is the
# rationale that was measured false; widening it to modules nobody has
# re-read would assert claims this lane cannot stand behind.

_ITERATING_ATTRS = ("items", "keys", "values")

# roll() walks the roster to build the census roll; the register's three sites
# only ever probe it for a seat the CENSUS already named.
_BEACONS_READER_SHAPES = {
    "roll": "ITERATES",
    "attend": "MEMBERSHIP",
    "escalate": "MEMBERSHIP",
    "_ack_alerts": "MEMBERSHIP",
}


def _reader_shapes(path):
    """{function: (shape, {attrs used on the mapping})} for every top-level
    function in `path` that calls a roster accessor.

    FAILS CLOSED, like the escape check above: a roster call whose result is
    not bound to a plain local name is a shape this analysis cannot read, and
    it raises rather than scoring the function as membership-only."""
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    out = {}
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        sub = list(ast.walk(node))
        calls = [n for n in sub if isinstance(n, ast.Call)
                 and _called_name(n.func) in _ROSTER_GETTERS]
        if not calls:
            continue
        bound = set()
        for call in calls:
            names = {a.targets[0].id for a in sub
                     if isinstance(a, ast.Assign) and a.value is call
                     and len(a.targets) == 1
                     and isinstance(a.targets[0], ast.Name)}
            if not names:
                raise RosterAccessEscape(
                    "%s:%d calls a roster accessor without binding the "
                    "mapping to a local name — the shape pin cannot tell an "
                    "iterating reader from a membership probe."
                    % (os.path.basename(path), call.lineno))
            bound |= names
        attrs, iterates = set(), False
        for a in sub:
            if (isinstance(a, ast.Attribute) and isinstance(a.value, ast.Name)
                    and a.value.id in bound):
                attrs.add(a.attr)
            iter_of = getattr(a, "iter", None)
            if (isinstance(a, (ast.For, ast.comprehension))
                    and isinstance(iter_of, ast.Name) and iter_of.id in bound):
                iterates = True
        if attrs & set(_ITERATING_ATTRS):
            iterates = True
        out[node.name] = ("ITERATES" if iterates else "MEMBERSHIP", attrs)
    return out


class BeaconsRosterReaderShapeTest(unittest.TestCase):
    def test_the_beacons_rationale_names_the_right_readers(self):  # noqa: VACUOUS_ASSERTION — _BEACONS_READER_SHAPES is a four-entry literal, so an empty or rotted scan FAILS this assertEqual rather than passing it; the count-pin cross-check below is the second unconditional control
        """MUST-HIT first: the four readers the allowlist paragraph and the
        count-pin both describe must actually be the four the module has, so
        a rename or a new site fails here instead of leaving the prose
        quietly describing a module that moved."""
        shapes = _reader_shapes(os.path.join(PKG, "beacons.py"))
        self.assertEqual(sorted(shapes), sorted(_BEACONS_READER_SHAPES),
                         "beacons.py's roster readers are not the ones the "
                         "allowlist rationale names — re-read the module, "
                         "then update BOTH _ROSTER_CONSUMERS['beacons.py'] "
                         "and _BEACONS_READER_SHAPES")
        self.assertEqual(len(shapes), _ROSTER_CALL_COUNTS["beacons.py"],
                         "the shape pin and the count pin disagree about how "
                         "many roster readers beacons.py has")

    def test_exactly_one_beacons_reader_ITERATES_the_roster(self):  # noqa: VACUOUS_ASSERTION — the assertEqual is against a non-empty literal naming roll() as ITERATES, so a scan that found nothing (or lost roll) fails here; the per-reader attrs=={'get'} loop is guarded by that same equality
        """The claim that rotted, now measured. `roll()` iterates and binds a
        roster KEY (which is why its names are laundered through label());
        attend, escalate and _ack_alerts only probe a mapping for a seat the
        census already named, which is what makes `r.get(seat)`-miss-is-a-SKIP
        the whole of their safety story."""
        shapes = _reader_shapes(os.path.join(PKG, "beacons.py"))
        actual = {fn: shape for fn, (shape, _attrs) in shapes.items()}
        self.assertEqual(actual, _BEACONS_READER_SHAPES,
                         "a beacons.py roster reader changed shape. An "
                         "ITERATING reader binds roster KEYS and must launder "
                         "every one it emits; a MEMBERSHIP reader may not "
                         "start iterating behind a rationale that says it "
                         "does not.\n  actual: %s\n  pinned: %s"
                         % (actual, _BEACONS_READER_SHAPES))
        for fn, (shape, attrs) in shapes.items():
            if shape != "MEMBERSHIP":
                continue
            self.assertEqual(
                attrs, {"get"},
                "%s reaches the roster mapping through %r — the rationale "
                "says these three touch it ONLY as `r.get(seat)`, a probe "
                "whose miss is a SKIP" % (fn, sorted(attrs)))


# ── B. the runtime sweep across every roster-consuming SINK ──────────────────
def _walk_strings(v):
    """Every string reachable in a value — dict values, list items, nested."""
    if isinstance(v, str):
        yield v
    elif isinstance(v, dict):
        for x in v.values():
            yield from _walk_strings(x)
    elif isinstance(v, (list, tuple)):
        for x in v:
            yield from _walk_strings(x)


ENV_KEYS = ("HELM_HOME", "MELD_HOME", "HELM_CHAT_DIR", "MELD_CHAT_DIR",
            "HELM_CHAT_NODE_URL", "MELD_CHAT_NODE_URL",
            "HELM_CELL_BIN", "MELD_CELL_BIN",
            "HELM_CHAT_NAME", "MELD_CHAT_NAME")


class RosterSinkSweepTest(unittest.TestCase):
    """Plant a hostile HELM_CHAT_NAME and drive EVERY roster-consuming verb and
    endpoint across todos.py, codexhomes.py, web.py, and hooks.py. Hermetic:
    tmp HELM_HOME + HELM_CHAT_DIR; transport off; the real ~/.helm is never
    touched."""

    SEAT = "lane" + ESC + "[2J" + BIDI + "pwn"
    CODEX_SEAT = "codex-" + ESC + "[2J" + BIDI + "pwn"
    # a running pane whose HELM_CHAT_NAME is hostile and NOT on the roster —
    # exercises hooks' "never joined" reason interpolation.
    PANE = "pane" + ESC + "[2J" + BIDI + "x"

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="helm-test-tripwire-")
        cls.env_prior = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["HELM_HOME"] = os.path.join(cls.tmp, "helm")
        os.environ["HELM_CHAT_DIR"] = os.path.join(cls.tmp, "chat")
        os.environ["HELM_CHAT_NODE_URL"] = ""   # transport off — hermetic
        cls._plant()

    @classmethod
    def tearDownClass(cls):
        for k, v in cls.env_prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @classmethod
    def _plant(cls):
        from helm import pk
        for seat, sid in ((cls.SEAT, "z" * 32), (cls.CODEX_SEAT, "y" * 32)):
            seats.write_roster(seat, session=sid, cwd=cls.tmp)
            with seats._flocked(seats.roster_path() + ".lock"):
                r = seats.roster()
                r[seat]["project"] = "proj" + ESC + "[31m" + BIDI + "X"
                r[seat]["status"] = "busy" + ESC + "[2J" + BIDI + "wiping"
                r[seat]["status_ts"] = time.time()      # fresh: the status tier wins
                r[seat]["runtime"] = {
                    "agent_harness": "pi" + ESC + "[2J" + BIDI + "h",
                    "family": "codex" + ESC + "[31m" + BIDI + "f",
                    "backend": "proxy" + ESC + "]0;t\x07" + BIDI + "b"}
                pk.write_json(seats.roster_path(), r)
            os.makedirs(os.path.dirname(todos.state_path(sid)), exist_ok=True)
            pk.write_json(todos.state_path(sid), {
                "v": 1, "ts": time.time(),
                "items": [{"id": "1",
                           "text": "evil" + ESC + "[31m" + BIDI + "task",
                           "status": "in_progress"}]})
        # a DM LANE addressed to the hostile seat: /api/chat's rooms array now
        # carries a dm row whose `seat` label resolves to the hostile roster
        # key — the emit web:_dm_seat_map feeds must launder or the sweep
        # leaks. Written via chat.post because seats.dm itself
        # refuses a non-token addressee — the lane file is the substrate.
        from helm import chat as _chat
        _chat.post("dm to the hostile seat", _chat.dm_room(cls.SEAT),
                   who="planter")

    def _assert_inert(self, label, text):
        self.assertNotIn(ESC, text, "%s leaked ESC" % label)
        self.assertNotIn(BIDI, text, "%s leaked bidi" % label)

    @staticmethod
    def _cap(fn):
        o, e = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(o), contextlib.redirect_stderr(e):
            rv = fn()
        return o.getvalue() + e.getvalue(), rv

    def _assert_json_inert(self, label, obj):
        import json
        for s in _walk_strings(obj):
            self._assert_inert("%s (walk %r)" % (label, s), s)
        # the actual wire: the server serializes ensure_ascii=False, so a raw
        # bidi codepoint would ride the response bytes uncescaped.
        self._assert_inert("%s (serialized)" % label,
                           json.dumps(obj, ensure_ascii=False))

    # -- todos.py: the fleet table AND /api/todos JSON --------------------------
    def test_todos_fleet_table_is_inert(self):
        text, _ = self._cap(lambda: todos.cmd_todos(["--all"]))
        self._assert_inert("helm todos --all", text)
        # the seat label must have PRINTED (laundered), not vanished
        self.assertIn("lane", text)
        self.assertIn("pwn", text)

    def test_todos_json_is_inert(self):
        text, _ = self._cap(lambda: todos.cmd_todos(["--all", "--json"]))
        self._assert_inert("helm todos --all --json", text)

    def test_todos_fleet_structure_is_inert(self):
        self._assert_json_inert("todos.fleet()", todos.fleet(items=True))

    # -- codexhomes.py: `helm codex capacity` ----------------------------------
    def test_codex_capacity_readout_is_inert(self):
        text, _ = self._cap(codexhomes._print_capacity)
        self._assert_inert("helm codex capacity", text)
        self.assertIn("codex-", text)            # the laundered label survives

    # -- web.py: the three roster-consuming JSON endpoints ----------------------
    def test_api_chat_is_inert(self):
        body, _ = web._api_chat({})
        self._assert_json_inert("/api/chat", body)

    def test_api_todos_is_inert(self):
        body, _ = web._api_todos({})
        self._assert_json_inert("/api/todos", body)

    def test_api_chat_roster_is_inert(self):
        body, _ = web._api_chat_roster({})
        self._assert_json_inert("/api/chat/roster", body)

    # -- hooks.py: surface_uncovered (printed name + interpolated reason) -------
    def test_hooks_surface_uncovered_is_inert(self):
        pane = {"pid": 4242, "seat": self.PANE, "family": "",
                "config_dir": None, "signer_bin": None, "signer_profile": None}
        orig = hooks.running_panes
        hooks.running_panes = lambda proc=None: [pane]
        try:
            text, _ = self._cap(hooks.surface_uncovered)
        finally:
            hooks.running_panes = orig
        self._assert_inert("helm hooks surface_uncovered", text)
        self.assertIn("pane", text)              # the laundered name survives

    # -- seat.py: `seat panes`, the pane listing's widest column ---------------
    def test_seat_panes_is_inert(self):
        """THE SINK THE SESSION JOIN OPENED. `pane_rows` labels a pane with a
        roster KEY once the session join runs, and `_panes` prints that name
        in its first and widest column — so a hostile HELM_CHAT_NAME reaches
        the operator's terminal through a path that did not exist before
        the session join landed. The sweep drove every other module's sinks and never this
        one, so the launder would have shipped with nothing proving it."""
        from helm import orcaadopt, seat
        row = {"handle": "h1", "provenance": orcaadopt.ORCA_ADOPTED,
               "seat": self.PANE, "status": "connected", "worktree": "/w"}
        procs, rows = orcaadopt.claude_processes, orcaadopt.pane_rows
        orcaadopt.claude_processes = lambda: ([], [])
        orcaadopt.pane_rows = lambda **kw: ([row], None)
        try:
            text, _ = self._cap(lambda: seat._panes([]))
        finally:
            orcaadopt.claude_processes, orcaadopt.pane_rows = procs, rows
        self._assert_inert("helm seat panes", text)
        self.assertIn("pane", text)              # the laundered name survives
        self.assertIn("h1", text)                # and the row really rendered

    # -- proof the sweep BITES: without the launder the payload would leak -----
    def test_planted_payload_is_actually_hostile(self):
        """Guards the guard: if the plant ever stopped carrying ESC/bidi, every
        assertion above would pass vacuously. Prove the raw key is hostile."""
        raw = seats.roster()[self.SEAT]
        self.assertIn(ESC, self.SEAT)
        self.assertIn(BIDI, self.SEAT)
        self.assertIn(ESC, raw["project"])       # the stored key stays raw…
        # …and _seat_label is what makes the EMITTED copy inert.
        self._assert_inert("_seat_label", seats._seat_label(self.SEAT))


# ── C. the SOURCE seam: every HELM_CHAT_NAME env read routes the accessor ────
#
# The r1..r6 rounds laundered SINKS; this closes the SOURCE. HELM_CHAT_NAME is
# validated once, in home.chat_name (home.py) — a legit name is [A-Za-z0-9._-],
# a control/bidi name is REJECTED at the seam. This grep enforces that NO other
# module reads the var raw: a new `os.environ["HELM_CHAT_NAME"]` /
# getenv / home.env("CHAT_NAME") anywhere but the accessor FAILS here, so a new
# sink can never be fed an unvalidated name again.
_RAW_ENV_READ = re.compile(
    r"""(?:os\.)?(?:environ(?:\.get)?\s*[\[(]|getenv\s*\()\s*"""
    r"""['"](?:HELM|MELD)_CHAT_NAME""")
_HOME_ENV_READ = re.compile(r"""\benv\(\s*['"]CHAT_NAME['"]""")
# ONLY this module may read the var — it is the accessor's home.
_ACCESSOR_MODULE = "home.py"


def _chat_name_read_sites():
    """[(module, lineno, text)] for every helm/*.py line that reads the
    HELM_CHAT_NAME env var — directly (os.environ/getenv) or via home.env's
    'CHAT_NAME' indirection. Source-driven: a new reader shows up here with no
    edit to this test, then fails unless it is the accessor module."""
    sites = []
    for fn in sorted(os.listdir(PKG)):
        if not fn.endswith(".py"):
            continue
        with open(os.path.join(PKG, fn), encoding="utf-8") as fh:
            for i, line in enumerate(fh, 1):
                if _RAW_ENV_READ.search(line) or _HOME_ENV_READ.search(line):
                    sites.append((fn, i, line.strip()))
    return sites


class SeatNameSourceSeamTest(unittest.TestCase):
    def test_only_the_accessor_reads_helm_chat_name(self):
        """Every HELM_CHAT_NAME ingestion routes home.chat_name — the ONE
        validating seam. A raw read in any other module (a new sink fed an
        unvalidated name) fails until it is routed through the accessor."""
        sites = _chat_name_read_sites()
        self.assertTrue(sites, "found no HELM_CHAT_NAME read — regex rotted")
        offenders = sorted({(m, ln, t) for m, ln, t in sites
                            if m != _ACCESSOR_MODULE})
        self.assertFalse(
            offenders,
            "HELM_CHAT_NAME is read RAW outside the accessor (%s) — route it "
            "through home.chat_name so the name is validated at the source.\n"
            "  offenders: %s" % (_ACCESSOR_MODULE, offenders))

    def test_the_accessor_actually_reads_it(self):
        """The seam cannot rot to a no-op: home.py must still read the var, so
        the allowlist-of-one names a real reader, not a stale entry."""
        live = {m for m, _, _ in _chat_name_read_sites()}
        self.assertIn(_ACCESSOR_MODULE, live,
                      "the accessor module no longer reads HELM_CHAT_NAME — the "
                      "seam moved; update _ACCESSOR_MODULE")


# ── D. the chat.py runtime sweep: hostile name REJECTED at the seam ──────────
#
# r6 found chat.py's from-field (reaching `helm chat read`/`rooms`/--follow +
# /api/chat raw) — the surface the roster()-scoped tripwire is structurally
# blind to. The SOURCE fix closes it: a seat can NEVER post under a hostile
# HELM_CHAT_NAME because home.chat_name rejects it before whoname/derive_seat
# return. This class proves (1) the reject fires across every name reader, (2)
# a legit name (codex-2/opus-integrator/ds4pro) still joins+posts+reads clean,
# and (3) a name planted OUTSIDE the seam (a foreign jsonl row) is still
# display-laundered on read AND /api/chat — belt (source) and suspenders (sink).
class ChatHostileNameSweepTest(unittest.TestCase):
    HOSTILE = "lane" + ESC + "[2J" + BIDI + "pwn"
    LEGIT = ("codex-2", "opus-integrator", "ds4pro")

    _ENV = ("HELM_HOME", "MELD_HOME", "HELM_CHAT_DIR", "MELD_CHAT_DIR",
            "HELM_CHAT_NODE_URL", "MELD_CHAT_NODE_URL",
            "HELM_CHAT_NAME", "MELD_CHAT_NAME", "HELM_CHAT_ROOM",
            "MELD_CHAT_ROOM", "HELM_CELL_BIN", "MELD_CELL_BIN")

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-chatseam-")
        self.prior = {k: os.environ.get(k) for k in self._ENV}
        for k in self._ENV:
            os.environ.pop(k, None)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "chat")
        os.environ["HELM_CHAT_NODE_URL"] = ""   # transport off — hermetic
        self.cwd_prior = os.getcwd()
        os.chdir(self.tmp)                       # default room stays 'main'

    def tearDown(self):
        os.chdir(self.cwd_prior)
        for k, v in self.prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _assert_inert(self, label, text):
        self.assertNotIn(ESC, text, "%s leaked ESC" % label)
        self.assertNotIn(BIDI, text, "%s leaked bidi" % label)

    # (1) the reject fires at the source, across EVERY name reader ------------
    def test_hostile_helm_chat_name_is_rejected_at_the_seam(self):
        os.environ["HELM_CHAT_NAME"] = self.HOSTILE
        for label, fn in (("home.chat_name", home.chat_name),
                          ("chat.whoname", chat.whoname),
                          ("seats.derive_seat", seats.derive_seat),
                          ("human.operator_name", human.operator_name)):
            with self.assertRaises(home.SeatNameError, msg=label):
                fn()

    def test_hostile_name_cannot_post_and_the_error_is_safe(self):
        os.environ["HELM_CHAT_NAME"] = self.HOSTILE
        with self.assertRaises(home.SeatNameError) as cm:
            chat.post("payload")
        # the room never received the hostile row — the post was refused
        _rows, total = chat.read("main")
        self.assertEqual(total, 0, "a hostile name entered the room")
        # the error names the offender SAFELY — no raw ESC/bidi in the message
        self._assert_inert("SeatNameError message", str(cm.exception))
        self.assertIn("\\x1b", str(cm.exception))   # hex-escaped, not raw

    # (2) a LEGIT name still joins + posts + reads cleanly, pinned ------------
    def test_legit_names_join_post_and_read_clean(self):
        for name in self.LEGIT:
            os.environ["HELM_CHAT_NAME"] = name
            self.assertEqual(home.chat_name(), name)
            self.assertEqual(chat.whoname(), name)
            row = chat.post("hi from %s" % name, room=name)
            self.assertEqual(row["from"], name)
            rows, total = chat.read(name)
            self.assertEqual(total, 1)
            self.assertEqual(rows[0]["from"], name)
            self._assert_inert(name, chat._fmt(rows[0]))
            self.assertIn(name, chat._fmt(rows[0]))

    # (3) a name planted OUTSIDE the seam is still laundered on the sinks -----
    def test_planted_hostile_from_field_is_laundered_on_read_and_api(self):
        # bypass the seam: write a raw jsonl row with a hostile `from`. The text
        # carries an @owner mention + a turn id so the owner_mention_* AND ledger
        # sinks (the reviewer's blind spots) are exercised, not only `lines`.
        os.makedirs(chat.chat_dir(), mode=0o700, exist_ok=True)
        import json
        with open(chat.room_path("main"), "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": "2026-07-22T00:00:00", "from": self.HOSTILE,
                                "text": "@owner look here", "turn": "t1"},
                               ensure_ascii=False) + "\n")
        rows, total = chat.read("main")
        self.assertEqual(total, 1)
        # CLI/journal render sink: laundered, but the visible name survives
        line = chat._fmt(rows[0])
        self._assert_inert("chat._fmt", line)
        self.assertIn("lane", line)
        self.assertIn("pwn", line)
        import json as _json
        # every browser-polled JSON sink that carries a from-field: /api/chat
        # (incl. owner_mention_last/preview) + the ledger endpoints.
        for label, body in (("/api/chat", web._api_chat({})[0]),
                            ("/api/ledger/native", web._api_ledger_native({})[0]),
                            ("/api/ledger", web._api_ledger({})[0])):
            for s in _walk_strings(body):
                self._assert_inert(label + " (walk)", s)
            self._assert_inert(label + " (serialized)",
                               _json.dumps(body, ensure_ascii=False))
        # the laundered name still rode the wire (not vanished)
        self.assertIn("lane", _json.dumps(web._api_chat({})[0], ensure_ascii=False))

    def test_seat_arg_rejects_hostile_name_second_ingestion(self):
        # the --seat CLI arg is the SECOND seat-name ingestion beside the env
        # seam; home.validate_seat_arg rejects a hostile name so `helm launch
        # --seat <hostile>` can never export it as HELM_CHAT_NAME or key a roster
        # row (the source-grep tripwire covers env reads only).
        from helm import home
        with self.assertRaises(home.SeatNameError) as cm:
            home.validate_seat_arg(self.HOSTILE)
        self._assert_inert("SeatNameError(--seat)", str(cm.exception))
        self.assertEqual(home.validate_seat_arg("codex-2"), "codex-2")
        self.assertIsNone(home.validate_seat_arg(""))


# ── E. the chat-ROW from-field sinks: deliver / stop_guard / meld.recv ───────
#
# Rounds A–D covered roster() consumers, the four web endpoints, and the
# HELM_CHAT_NAME env seam. But three terminal-facing sinks read a from-field
# off a CHAT ROW — not from roster() and not from the env — so BOTH the
# roster-grep tripwire (A) and the env-seam grep (C) are structurally BLIND to
# them, and the round-D chat sweep only drives chat._fmt / the JSON endpoints:
#
#   1. seats.deliver()   — the tool-boundary nudge "[helm chat → seat] <FROM>:
#                          <text>" (the most-rendered agent-facing line, every
#                          PostToolUse + beacon).
#   2. seats.stop_guard() — the undelivered-message block line.
#   3. meld.recv()       — the YIELD/HOLD/DONE/ABORT/READY line to the peer.
#
# A hostile from-field reaches these via a PLANTED/FOREIGN jsonl row (written
# outside the validating join seam — a pre-fix row, a foreign node's row). The
# TEXT legitimately carries unicode and is left as-is (the class rule); only
# the identity field is laundered through chat._dsan. THIS SWEEP now covers the
# chat-ROW from-field sinks, not just roster()/env readers.
#
# r10 broadened the sweep to the FULL meld identity surface + verify: the
# convener/peer from-field also PROMOTES into posted message TEXT (join's READY,
# invite's @peer, say's DONE/ABORT mention) which chat._fmt renders raw
# fleet-wide — the identity-into-text bypass — plus meld.status' peer column and
# chat.verify's MISMATCH stderr print. Every meld identity EMIT and verify's
# emitted dict now launder through chat._dsan; the RAW convener/peer lives only
# in state for recv's matching. The Section-F grep tripwire below keeps it that
# way: a NEW raw from-field emit in ANY module trips the source-driven grep.
class ChatRowFromFieldSinkSweepTest(unittest.TestCase):
    HOSTILE = "lane" + ESC + "[2J" + BIDI + "pwn"

    _ENV = ("HELM_HOME", "MELD_HOME", "HELM_CHAT_DIR", "MELD_CHAT_DIR",
            "HELM_CHAT_NODE_URL", "MELD_CHAT_NODE_URL",
            "HELM_CHAT_NAME", "MELD_CHAT_NAME", "HELM_CHAT_ROOM",
            "MELD_CHAT_ROOM", "HELM_CELL_BIN", "MELD_CELL_BIN",
            "HELM_SCRATCH_GC", "HELM_CACHE_DIR")

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-rowsink-")
        self.prior = {k: os.environ.get(k) for k in self._ENV}
        for k in self._ENV:
            os.environ.pop(k, None)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "chat")
        os.environ["HELM_CHAT_NODE_URL"] = ""    # transport off — hermetic
        # this class drives seats.stop_guard, whose silent-mechanical lane runs
        # the scratch reaper — a real DELETE under /tmp/claude-*. A test never
        # mutates a harness store (test_scratch.py pins this).
        os.environ["HELM_SCRATCH_GC"] = "0"
        os.environ["HELM_CACHE_DIR"] = os.path.join(self.tmp, "cache")
        self.cwd_prior = os.getcwd()
        os.chdir(self.tmp)                        # default room stays 'main'
        os.makedirs(chat.chat_dir(), mode=0o700, exist_ok=True)

    def tearDown(self):
        os.chdir(self.cwd_prior)
        for k, v in self.prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _assert_inert(self, label, text):
        self.assertNotIn(ESC, text, "%s leaked ESC" % label)
        self.assertNotIn(BIDI, text, "%s leaked bidi" % label)

    def _plant(self, room, rid, text):
        """Append ONE raw jsonl row with a hostile from-field — bypassing the
        join seam, as a foreign/pre-fix node row does."""
        import json
        with open(chat.room_path(room), "a", encoding="utf-8") as f:
            f.write(json.dumps(
                {"ts": "2026-07-22T00:00:00", "id": rid, "from": self.HOSTILE,
                 "text": text}, ensure_ascii=False) + "\n")

    def _plant_obj(self, room, obj):
        """Append ONE raw jsonl row of an ARBITRARY shape — the meld/verify
        sinks need custom from + text + protocol markers (READY/ABORT/DONE,
        an epoch fence, a signed-MISMATCH payload)."""
        import json
        row = {"ts": "2026-07-22T00:00:00"}
        row.update(obj)
        with open(chat.room_path(room), "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    @staticmethod
    def _cap(fn):
        o, e = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(o), contextlib.redirect_stderr(e):
            rv = fn()
        return o.getvalue() + e.getvalue(), rv

    # -- 1. seats.deliver(): the tool-boundary nudge ---------------------------
    def test_deliver_boundary_nudge_is_inert(self):
        self._plant("main", "aa" * 6, "@recvr ping here")
        collected = []
        seats.deliver(session="s" * 32, room="main", seat="recvr",
                      emit=collected.append, backfill=True)
        line = "\n".join(collected)
        self._assert_inert("seats.deliver", line)
        # the from-field actually EMITTED (laundered, not vanished): the sweep
        # would pass vacuously if deliver never rendered the row's from.
        self.assertIn("lane", line)
        self.assertIn("pwn", line)

    # -- 2. seats.stop_guard(): the undelivered-message block ------------------
    def test_stop_guard_inbox_block_is_inert(self):
        sid = "t" * 32
        seats.write_roster("stopr", session=sid, cwd=self.tmp)
        seats.deliver(session=sid, room="main", seat="stopr",
                      emit=lambda s: None)         # establish the EOF cursor
        self._plant("main", "bb" * 6, "@stopr ping here")
        blocks, warns = seats.stop_guard(session=sid, room="main", seat="stopr")
        text = "\n".join(blocks + warns)
        self._assert_inert("seats.stop_guard", text)
        self.assertIn("undelivered", text)         # the block fired…
        self.assertIn("lane", text)                # …and rendered the from
        self.assertIn("pwn", text)

    # -- 3. meld.recv(): the peer-facing chunk line ---------------------------
    def test_meld_recv_chunk_line_is_inert(self):
        room = "meld-sink-test"
        meld._write_state(room, "recvr2", {
            "room": room, "epoch": 123, "role": "joiner", "self": "recvr2",
            "peer": self.HOSTILE, "idx": 0, "exchanges": 0, "cap": 8,
            "status": "active", "created": pk.now_ts()})
        self._plant(room, "cc" * 6, "my chunk [YIELD]")
        code, lines = meld.recv(room, timeout=0.2, seat="recvr2")
        text = "\n".join(lines)
        self._assert_inert("meld.recv", text)
        self.assertEqual(code, 0)                  # a real chunk was returned…
        self.assertIn("lane", text)                # …with the from laundered
        self.assertIn("pwn", text)

    # -- 3b. meld.recv(): the OTHER three marker lines (r9 laundered all four,
    #        the r9 sweep drove only YIELD — READY/ABORT/DONE were revert-blind).
    def test_meld_recv_ready_abort_done_lines_are_inert(self):
        # READY (convener/invited): the joiner's control row, hostile from.
        # peer pins to the SAME hostile name — recv's pinned-pair law drops
        # any non-peer row before it can render, so the sink only fires for
        # the meld's own (hostile-named, planted-outside-the-seam) peer.
        r1 = "meld-recv-ready"
        meld._write_state(r1, "conv2", {
            "room": r1, "epoch": 123, "role": "convener", "self": "conv2",
            "peer": self.HOSTILE, "idx": 0, "exchanges": 0, "cap": 8,
            "status": "invited", "created": pk.now_ts()})
        self._plant_obj(r1, {"from": self.HOSTILE,
                             "text": "[MELD e:123] READY:123 (joined)"})
        code, lines = meld.recv(r1, timeout=0.2, seat="conv2")
        t = "\n".join(lines)
        self._assert_inert("meld.recv READY", t)
        self.assertEqual(code, 0)
        self.assertIn("lane", t)
        self.assertIn("pwn", t)
        # ABORT (active joiner): fail-loud line renders the peer's from.
        r2 = "meld-recv-abort"
        meld._write_state(r2, "recvr3", {
            "room": r2, "epoch": 9, "role": "joiner", "self": "recvr3",
            "peer": self.HOSTILE, "idx": 0, "exchanges": 0, "cap": 8,
            "status": "active", "created": pk.now_ts()})
        self._plant_obj(r2, {"from": self.HOSTILE, "text": "boom [ABORT]"})
        code, lines = meld.recv(r2, timeout=0.2, seat="recvr3")
        t = "\n".join(lines)
        self._assert_inert("meld.recv ABORT", t)
        self.assertEqual(code, meld.EXIT_ABORT)
        self.assertIn("lane", t)
        # DONE (active joiner): peer-left line renders the from.
        r3 = "meld-recv-done"
        meld._write_state(r3, "recvr4", {
            "room": r3, "epoch": 9, "role": "joiner", "self": "recvr4",
            "peer": self.HOSTILE, "idx": 0, "exchanges": 0, "cap": 8,
            "status": "active", "created": pk.now_ts()})
        self._plant_obj(r3, {"from": self.HOSTILE, "text": "bye [DONE]"})
        code, lines = meld.recv(r3, timeout=0.2, seat="recvr4")
        t = "\n".join(lines)
        self._assert_inert("meld.recv DONE", t)
        self.assertEqual(code, 0)
        self.assertIn("lane", t)

    # -- 4. meld.join(): the MELD-JOINED display AND the promoted READY TEXT ----
    #    convener is a SEED ROW's from — HIGH: it entered the POSTED text (@%s
    #    [MELD…] READY), which chat._fmt then renders raw fleet-wide.
    def test_meld_join_display_and_promoted_text_are_inert(self):
        room = "meld-join-test"
        self._plant_obj(room, {"from": self.HOSTILE,
                               "text": "[MELD e:123] PROBLEM: x [HOLD]"})
        lines = meld.join(room, seat="joiner")
        disp = "\n".join(lines)
        self._assert_inert("meld.join display", disp)
        self.assertIn("lane", disp)
        self.assertIn("pwn", disp)
        rows, _ = chat.read(room)
        ready = [r for r in rows if "READY" in (r.get("text") or "")][0]
        # the convener was laundered BEFORE entering the posted text — the
        # identity-into-text bypass is closed at the source of the promotion.
        self._assert_inert("meld.join promoted READY text", ready["text"])
        self.assertIn("lane", ready["text"])
        self._assert_inert("meld.join READY _fmt", chat._fmt(ready))

    # -- 5. meld.invite(): a hostile peer ARG is now REFUSED at the validated
    #    seam (home.validate_seat_arg — the same law that closed launch
    #    --seat), one layer ABOVE the display launder: nothing is posted, no
    #    room is born, and the refusal itself renders inert (_safe_name).
    def test_meld_invite_hostile_peer_is_refused_inert(self):
        before = set(chat.list_rooms())
        with self.assertRaises(SystemExit) as cm:
            meld.invite(self.HOSTILE, "topic here", seat="conv")
        msg = str(cm.exception)
        self._assert_inert("meld.invite refusal", msg)
        self.assertIn("pwn", msg)                  # named, laundered
        self.assertEqual(before, set(chat.list_rooms()))  # nothing posted

    # -- 5b. inject._council_reach(): the reach whisper's peer emit ------------
    def test_council_reach_whisper_is_inert(self):
        from helm import inject
        os.environ["HELM_CHAT_NAME"] = "reachr"    # _ENV-listed, restored
        for i in range(3):
            chat.post("q%d" % i, room="main", who="reachr")
            self._plant("main", ("%02d" % i) * 6, "a%d" % i)
        got = inject._council_reach(None, None)
        self.assertIsNotNone(got)
        line, _wid = got
        self._assert_inert("inject council-reach whisper", line)
        self.assertIn("lane", line)                # emitted laundered,
        self.assertIn("pwn", line)                 # not vanished

    # -- 6. meld.say(): the DONE/ABORT @peer mention posted into TEXT ----------
    def test_meld_say_done_mention_is_inert(self):
        room = "meld-say-test"
        meld._write_state(room, "sayer", {
            "room": room, "epoch": 55, "role": "convener", "self": "sayer",
            "peer": self.HOSTILE, "idx": 0, "exchanges": 0, "cap": 8,
            "status": "active", "created": pk.now_ts()})
        lines = meld.say(room, "DONE", "closing state", seat="sayer")
        self._assert_inert("meld.say return", "\n".join(lines))
        rows, _ = chat.read(room)
        done = [r for r in rows if "[DONE]" in (r.get("text") or "")][0]
        self._assert_inert("meld.say posted DONE text", done["text"])
        self.assertIn("lane", done["text"])
        self.assertIn("pwn", done["text"])

    # -- 7. meld.status(): the peer=%s display column --------------------------
    def test_meld_status_display_is_inert(self):
        room = "meld-status-test"
        meld._write_state(room, "statr", {
            "room": room, "epoch": 7, "role": "convener", "self": "statr",
            "peer": self.HOSTILE, "idx": 0, "exchanges": 0, "cap": 8,
            "status": "active", "created": pk.now_ts()})
        text = "\n".join(meld.status(seat="statr"))
        self._assert_inert("meld.status", text)
        self.assertIn("lane", text)
        self.assertIn("pwn", text)

    # -- 8. chat.verify(): the emitted dict AND the CLI MISMATCH stderr print ---
    def test_chat_verify_mismatch_row_is_inert(self):
        room = "verify-test"
        # a SIGNED row whose stored payload no longer recomputes = MISMATCH, the
        # one verify state that reaches the stderr print (r["from"], raw pre-fix).
        self._plant_obj(room, {"from": self.HOSTILE, "text": "x",
                               "chain": "sig-abc", "payload": "WRONG-PAYLOAD"})
        rep = chat.verify(room)
        for r in rep:                              # every emitted from laundered
            self._assert_inert("verify() dict from", r["from"])
        bad = [r for r in rep if r["state"] == "MISMATCH"]
        self.assertTrue(bad, "the planted signed row must verify MISMATCH")
        self.assertIn("lane", bad[0]["from"])      # laundered, not vanished
        self.assertIn("pwn", bad[0]["from"])
        # the actual CLI stderr sink: the print reads r["from"] off the dict.
        text, _ = self._cap(lambda: chat.cmd_chat(["verify", "--room", room]))
        self._assert_inert("helm chat verify (stderr)", text)
        self.assertIn("lane", text)

    # -- 9. chat.log_flush(): the journal a `cat` renders (identity columns) ----
    def test_log_flush_journal_is_inert(self):
        room = "main"
        self._plant_obj(room, {"from": self.HOSTILE, "id": "ee" * 6,
                               "text": "@x hi there"})
        n = chat.log_flush(rooms=[room])
        self.assertGreaterEqual(n, 1)
        path = os.path.join(chat.journal_dir(),
                            "chat-%s.log" % time.strftime("%Y-%m-%d"))
        with open(path, encoding="utf-8") as f:
            body = f.read()
        # the from column is _dsan-laundered; the message TEXT stays full-fidelity
        self._assert_inert("chat log-flush journal", body)
        self.assertIn("lane", body)                # laundered name survives
        self.assertIn("pwn", body)
        self.assertIn("hi there", body)            # the text rode through intact

    # -- 10. nested transport profile/reason projections -----------------------
    def test_nested_transport_identity_and_reason_sinks_are_inert(self):
        transport = {"state": "DEGRADED", "profile": self.HOSTILE,
                     "code": "send_failed",
                     "reason": "node " + self.HOSTILE + " refused",
                     "first_failure": "then", "last_failure": "now",
                     "last_age_s": 1, "failure_count": 1,
                     "remediation": "retry"}
        row = {"ts": "2026-07-23T00:00:00Z", "from": "agent",
               "text": "fallback", "transport": transport}
        public = chat.public_rows([row])[0]["transport"]
        self._assert_inert("public transport.profile", public["profile"])
        self._assert_inert("public transport.reason", public["reason"])
        for label, text in (
                ("chat._fmt transport", chat._fmt(row)),
                ("chat._fmt_body transport", chat._fmt_body(row)),
                ("transport summary", chat.transport_failure_summary(
                    dict(transport, mode="degraded")))):
            self._assert_inert(label, text)
        model = human.model_new()
        model["status"] = dict(transport, mode="degraded", head=None)
        self._assert_inert("human status transport",
                           human.status_line(model, 300))

    # -- proof the sweep BITES: the planted from-field is genuinely hostile ----
    def test_planted_from_field_is_actually_hostile(self):
        """Guards the guard: if the plant stopped carrying ESC/bidi the three
        assertions above would pass vacuously. The stored from stays RAW; only
        the EMITTED copy (chat._dsan) is inert."""
        self.assertIn(ESC, self.HOSTILE)
        self.assertIn(BIDI, self.HOSTILE)
        self._plant("main", "dd" * 6, "@x hi")
        rows, _total = chat.read("main")
        self.assertIn(ESC, rows[0]["from"])        # stored key stays raw…
        self._assert_inert("chat._dsan", chat._dsan(self.HOSTILE))  # …emit inert


# ── F. the SOURCE-DRIVEN grep tripwire for chat-ROW from-field emits ─────────
#
# THE FROM-FIELD ANALOG OF THE roster() TRIPWIRE (Section A,
# RosterConsumerAllowlistTest). Section A greps every seats.roster() caller and
# forces each into an allowlist with a reason; a new consumer in an unguarded
# module fails by construction. This does the IDENTICAL thing for the chat-row
# IDENTITY fields — the second unvalidated identity source that kept the ESC/
# bidi class reopening (r7..r10): a foreign/planted row's from/tfrom/rfrom/dm,
# and its meld relocations convener (join) + peer (meld state). Unlike
# HELM_CHAT_NAME (rejected at the home.chat_name seam, Section C), a foreign row
# CANNOT be rejected — so its only defense is PER-SINK laundering (chat._dsan),
# and each round found one more sink. This grep ENDS that: it enumerates EVERY
# helm/*.py site that reads a chat/meld row's identity field, and every module
# reaching a sink must be allowlisted with a reason — LAUNDERED (the emitted
# copy routes chat._dsan / public_rows / _fmt), INTERNAL-MATCHING-ONLY (the read
# never reaches a sink — react/reply/deliverable keys), or NOT-A-CHAT-ROW. A NEW
# raw from-field emit in ANY module then FAILS this grep by construction: it is
# a new read site (count-pin trips) or lands in an unlisted module (allowlist
# trips) — sink #14 can never ship unlaundered. The reason is the reviewer's
# contract; the Section-E runtime sweep verifies the emits are actually inert.

# IDENTITY-field dict accessors ( .get/.pop/.setdefault("X") / ["X"] ) —
# from/tfrom/rfrom/dm are the chat-row NAME columns (chat._ID_FIELDS); peer is
# the meld state relocation of a convener from-field (say/status emit it). The
# keys are ALWAYS quoted, so this is code-only by nature (no prose match),
# exactly like _ROSTER_CALL. get/pop/setdefault all READ the value into a sink
# (r10 adversarial: a raw .pop("from") emit would otherwise evade the sweep);
# %-dict ("%(from)s") and itemgetter remain a documented LOW residual — exotic
# enough that no current sink uses them and a future one is caught at review.
_FROM_FIELD_READ = re.compile(
    r"""(?:\.(?:get|pop|setdefault)\(\s*|\[\s*)['"](?:from|tfrom|rfrom|dm|peer)['"]""")
# the meld `convener` LOCAL, assigned from a seed row's `from` — matched as a
# bare identifier in CODE only (docstrings + comments + the "convener" role
# string literal are stripped before the match, see _from_field_read_sites).
_CONVENER_LOCAL = re.compile(r"\bconvener\b")
_STR_LITERAL = re.compile(r"""(['"]).*?\1""")


def _code_visible(line, st):
    """The part of `line` OUTSIDE any triple-quoted docstring, tracking the
    open/close across lines via `st` — so a docstring that merely MENTIONS
    'convener' (meld.join's) is never counted as a read site."""
    out, i, n = [], 0, len(line)
    while i < n:
        if st["in"]:
            idx = line.find(st["q"], i)
            if idx == -1:
                i = n
            else:
                i = idx + 3
                st["in"] = False
                st["q"] = None
        else:
            cands = [x for x in (line.find('"""', i), line.find("'''", i))
                     if x != -1]
            if not cands:
                out.append(line[i:])
                i = n
            else:
                nxt = min(cands)
                out.append(line[i:nxt])
                st["q"] = line[nxt:nxt + 3]
                st["in"] = True
                i = nxt + 3
    return "".join(out)


def _strip_comment(code):
    """Drop the trailing comment QUOTE-AWARE — a naive split("#") truncates at
    a '#' INSIDE a string literal, hiding any accessor after it (a planted
    `print("#issue %s" % row.get("from"))` evaded the r10 tripwire that way).
    Worst-case mis-tracking keeps comment text and over-counts — which fails
    the count-pin LOUDLY, never silently hides a read site."""
    q = None
    for i, ch in enumerate(code):
        if q:
            if ch == q and (i == 0 or code[i - 1] != "\\"):
                q = None
        elif ch in "'\"":
            q = ch
        elif ch == "#":
            return code[:i]
    return code


def _from_field_files(pkg=PKG):
    """helm/*.py plus one level of package submodules ('inject/_whisper.py') —
    a module decomposed into a PACKAGE must never blind this sweep: its read
    sites keep being scanned, keyed by the package-relative path."""
    out = [fn for fn in sorted(os.listdir(pkg)) if fn.endswith(".py")]
    for d in sorted(os.listdir(pkg)):
        sub = os.path.join(pkg, d)
        if os.path.isdir(sub) and os.path.exists(os.path.join(sub, "__init__.py")):
            out += [os.path.join(d, fn) for fn in sorted(os.listdir(sub))
                    if fn.endswith(".py")]
    return out


def _from_field_read_sites(pkg=PKG):
    """[(owner, lineno, text)] for every helm chat-identity read.

    Web sites key on their enclosing semantic function so moving that function
    between web.py and a web_* implementation does not erase or duplicate the
    allowlist authority. Other modules retain their historical module key."""
    sites = []
    for fn in _from_field_files(pkg):
        path = os.path.join(pkg, fn)
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        tree = ast.parse(text, filename=path)
        st = {"in": False, "q": None}
        for i, line in enumerate(text.splitlines(keepends=True), 1):
            code = _strip_comment(_code_visible(line, st))
            hit = bool(_FROM_FIELD_READ.search(code))
            if not hit and _CONVENER_LOCAL.search(_STR_LITERAL.sub("", code)):
                hit = True                  # bare convener local, no literal
            if hit:
                sites.append((_semantic_source_owner(fn, tree, i),
                              i, line.strip()))
    return sites


# The allowlist IS the law (mirrors _ROSTER_CONSUMERS): every module that reads
# a chat-row identity field must appear here with a reason. Keyed by basename.
_FROM_FIELD_CONSUMERS = {
    "seats_cli.py": (
        "LAUNDERED: the catchup and ack verbs read a chat row's from/tfrom "
        "to decide which rows are ADDRESSED to the acting seat. Identity "
        "that reaches the operator goes through _seat_label; the rest are "
        "comparisons. Moved verbatim from seats.py by the split."),
    "seats_stop_guard.py": (
        "INTERNAL-MATCHING-ONLY: the idle gate reads a row's from-field once "
        "to decide whether an addressed obligation is outstanding before a "
        "seat may stop. A comparison against the seat's own identity; what "
        "the guard emits is a refusal sentence, never the row's author."),
    "seats_ack.py": (
        "LAUNDERED / INTERNAL-MATCHING-ONLY: this is the module whose whole "
        "subject is a chat row's identity fields — it reads from/tfrom to "
        "locate a row and to decide who owes an ack. The identity that "
        "reaches a human surface goes through _seat_label; the rest are "
        "comparisons. Moved verbatim from seats.py by the split."),
    "seats_stop_signals.py": (
        "INTERNAL-MATCHING-ONLY: the pending/ask probes read a chat row's "
        "from/tfrom to decide whether a row is ADDRESSED TO this seat and "
        "therefore owed an answer before it may idle. A comparison against "
        "the seat's own identity, never a render. Moved by the split."),
    "seats_delivery.py": (
        "LAUNDERED: the delivery path reads a chat row's from/tfrom to decide "
        "whether the row is FOR this seat — a comparison against the "
        "recipient, not a render. Moved verbatim from seats.py by the split."),
    "seats_identity.py": (
        "SAME CODE, NEW FILE: seats.py's own chat-row identity reads, moved "
        "by the seats.py split with not one line changed. These are the "
        "addressing half of the module — mentions, deliverable, "
        "_same_reaction_target, _reaction_wake_body — deciding whether a "
        "written word reaches a given seat, which is the question a from/"
        "tfrom/rfrom field exists to answer. Whatever laundering discipline "
        "these reads carried under the old filename they carry under the "
        "new one; registered rather than exempted because a split must not "
        "become a way to leave an allowlist."),
    "chat.py": (
        "LAUNDERED (publish owner): every identity column that reaches a sink "
        "routes chat._dsan — _fmt (CLI/journal render), _fmt_body (log-flush "
        "journal), verify() (the emitted dict, one-owner for the MISMATCH "
        "stderr print), public_rows (the JSON wire). The remaining reads are "
        "INTERNAL-MATCHING-ONLY: react-digest/react-state keys, reply/quote "
        "resolution, rkey, _touch_poster_presence, the dm-room derivation, "
        "and keyed-post identity derivation — _append hashes from with the "
        "canonical room and event key, exposing only an opaque row id — none "
        "emit a raw name. restore_journal's already-restored marker detection "
        "and dedupe key compare from/text against journal records and emit only "
        "room names and counts. Verified by ChatHostileNameSweep + "
        "ChatRowFromFieldSinkSweep."),
    "council.py": (
        "LAUNDERED+INTERNAL: the council registry STORES raw member/convener "
        "seat names (they are the actor-gate KEYS — signal() matches the "
        "ambient seat against reg[members], so the key must stay raw, exactly "
        "like meld's state[peers]). Every EMIT laundates via chat._dsan: the "
        "non-member refusal's member list, status_lines' member roster and "
        "aborted_by. The reveal/abort/tally CALLERS in seats._cmd_council "
        "launder each signer as they print. This module has no other sink — "
        "it returns data; seats.py owns the printing."),
    "meld.py": (
        "LAUNDERED: every meld identity EMIT routes chat._dsan — invite (@peers "
        "posted text + MELD-INVITED display, d_peers), join (READY posted text + "
        "MELD-JOINED display, d_convener), recv (READY/ABORT/DONE lines + the "
        "pending-countersign list, chat._dsan(frm/member)), say (DONE/ABORT @peer "
        "mention), status (peer column). standup's 2+ pinned SET keeps the RAW "
        "members ONLY in state[peers]/[done_peers] for recv's frm MATCHING (frm "
        "is accept-filtered before it can enter done_peers) — never emitted raw. "
        "Verified by ChatRowFromFieldSinkSweep's join/invite/say/status/recv-all-"
        "markers tests + TestStandupMultiParty."),
    # seats.py IS DELIBERATELY ABSENT. Trunk still carried a rich
    # entry here describing deliver()'s boundary nudge, stop_guard's
    # undelivered block and the ack/pending CLI lines. Those emit sites
    # are real and still guarded — they now live in seats_delivery,
    # seats_stop_guard and seats_ack, which carry their own entries
    # above. The facade emits nothing, so an entry for it would vouch
    # for call sites that are not there.
    "web:_owner_signal": (
        "LAUNDERED+INTERNAL: owner-mention preview and matching reads launder "
        "every emitted identity through chat._dsan."),
    "web:_room_seats": (
        "LAUNDERED: room-summary identities route through the seat-label seam."),
    "web:_chat_gen": (
        "INTERNAL-MATCHING-ONLY: rotation fingerprints hash from/tfrom into "
        "sha1 and never display either identity."),
    "web:_api_chat_react": (
        "INTERNAL-MATCHING-ONLY: the reaction target identity selects a row; "
        "the endpoint emits only the laundered public projection."),
    "web:_turn_about": (
        "LAUNDERED+INTERNAL: ledger turn identities are matched internally and "
        "emitted only through chat._dsan."),
    "web:_native_chat_pulse": (
        "LAUNDERED+INTERNAL: native-ledger identities are reduced to the "
        "laundered pulse projection."),
    "homes.py": (
        "NOT-A-CHAT-ROW: meta.get('from') is a provider-migration SOURCE PATH "
        "(the home's origin dir), never a chat/meld identity — it reaches no "
        "chat sink. Listed so a future `.get(\"from\")` here is re-justified."),
    "inject/_whisper.py": (
        "LAUNDERED+INTERNAL: _council_reach's from reads (the ping-pong "
        "suffix walk, the owner check, the mid-meld state peer match) are "
        "INTERNAL-MATCHING-ONLY; the ONE emit — the council-reach whisper "
        "line — launders the peer via chat._dsan before it rides the reflex "
        "lane. Verified by ChatRowFromFieldSinkSweep's council-reach test."),
}

# COUNT-PIN per module (mirrors _ROSTER_CALL_COUNTS): module-membership alone
# lets a NEW read site slip into an already-allowlisted module unreviewed. Pin
# the exact count so a new identity read — even in an allowlisted module —
# trips until a human re-counts AND confirms the new site launders (or is
# internal). Regenerate deliberately from _from_field_read_sites().
_FROM_FIELD_READ_COUNTS = {
    "chat.py": 34,         # +1: _fmt's ack-marker render (laundered via _dsan)
                           # +1: _restore_pending's dedupe key (MATCHING-ONLY —
                           # rebuilds "%s: %s" % (from, text) to match journal
                           # records by exact equality; the CLI emits only room
                           # names and counts, never a row identity).
                           # +1: _append's keyed-post identity input (HASH-ONLY —
                           # canonical room + author + event key become an opaque
                           # sha256 row id; no raw identity reaches a sink). The
                           # r3 tmpfs sentinel retired the marker-row read.
    "council.py": 2,       # convene's `convener` param + its registry write —
                           # the actor-gate KEY is stored raw (like meld's
                           # state[peers]); every emit launders via _dsan
    "homes.py": 1,
    "inject/_whisper.py": 5,  # _council_reach: suffix walk x3 + mid-meld peer
                              # match; +1 _is_review_streak's per-speaker read —
                              # MATCHING-ONLY (it counts which SIDES used review
                              # vocabulary and emits no name; the whisper line
                              # itself launders the peer via chat._dsan)
    "meld.py": 16,    # +4: lifecycle replay validates peer, matches it against
                      # the raw peer set, builds the legacy peer list, and copies
                      # peer/peers/done_peers/spoke_peers into the durable journal
                      # projection. The first three are MATCHING-ONLY; the fourth
                      # is a durable KEY record governed by meld.py's own
                      # "launder the EMIT, keep the KEY raw" law. (was 12)
                      # +1: _floor_line's yielder/still-to-hear reads (the
                      # honest-floor rung) — MATCHING-ONLY set filtering; all three
                      # emitted identities route chat._dsan. (was 11)
                      # +1 net: standup 2+ generalized the pinned PAIR to a
                      # pinned SET — recv resolves the accept-set (st.get("peers")
                      # + the st["peer"] backward-compat fallback) and DONE tracks
                      # st.get("done_peers"). All MATCHING-ONLY; every EMIT still
                      # routes chat._dsan (frm, pending members). (was 10)
    # SPLIT, NOT DRIFT — the arithmetic is the proof. Eight identity/
    # addressing read sites moved to seats_identity.py: seats.py 30 -> 22,
    # seats_identity.py 0 -> 8, TOTAL 96 -> 96. Nothing added, removed or
    # edited, so no site needs re-reviewing for laundering; the conservation
    # of the total is what says so.
    "seats_identity.py": 8,
    # third wave: one site to seats_delivery. 22 -> 21, 0 -> 1, 96 -> 96.
    "seats_delivery.py": 1,
    # fourth wave: two sites to seats_stop_signals. 21 -> 19, 0 -> 2, 96 -> 96.
    "seats_stop_signals.py": 2,
    # fifth wave: ten sites to seats_ack — it is the ack ladder, so the
    # from-field IS its subject. 19 -> 9, 0 -> 10, 96 -> 96.
    "seats_ack.py": 10,
    # seats.py IS GONE FROM THIS PIN and its absence is the finish line:
    # the facade reads a chat row's identity fields ZERO times now. Its
    # nine sites went to seats_cli (8) and seats_stop_guard (1); TOTAL
    # 96 -> 96, conserved. A pin entry for a module that no longer reads
    # would vouch for call sites that do not exist.
    "seats_cli.py": 8,
    "seats_stop_guard.py": 1,
                           # for canonical matching, and the aggregate wake
                           # renders reactor/target through chat._dsan. (was 26)
                           # +6: catchup — the sender tally for the
                           # addressed-park TRACE (each name emits through
                           # chat._dsan inside the posted text), the report
                           # rows' from field (emitted only via the CLI
                           # branch, which _dsan's every name), and the
                           # _addressed anti-drift reads. (was 18)
                           # +11: the ack/consume-ladder reads (matching +
                           # laundered emits); +1: consume_state's dm-vs-room
                           # branch reads .get("dm") for control flow only
                           # +1: _spiral_gate's `peer` — a dispatch-LEDGER
                           # recipient, VALIDATED at the seam against
                           # dispatches._TOKEN before it is quoted into a
                           # copy-pasteable meld command (the beacon block's
                           # pattern; a non-matching name yields no finding).
                           # (was 24)
                           # +1: _melded_with's meld-record `peer` (spiral
                           # suppression) — MATCHING-ONLY: the value enters a
                           # membership test against the caller's own peer and
                           # is never emitted. The one thing that function DOES
                           # emit — the meld's `room` — is laundered AT THE READ
                           # (_clip(_scrub(...), SEAT_BYTES)) rather than at the
                           # emit, because _spiral_gate interpolates it into
                           # displayed stop-guard text and a per-caller launder
                           # would leave the next caller exposed. (was 25)
    "web:_owner_signal": 3,
    "web:_room_seats": 1,
    "web:_chat_gen": 2,
    "web:_api_chat_react": 1,
    "web:_turn_about": 1,
    "web:_native_chat_pulse": 1,
}


class ChatRowFromFieldConsumerAllowlistTest(unittest.TestCase):
    """The from-field analog of RosterConsumerAllowlistTest (Section A). Same
    three teeth: every consuming module allowlisted with a reason, the per-
    module read-count pinned, and no stale allowlist entry."""

    def test_every_from_field_consumer_is_allowlisted(self):
        sites = _from_field_read_sites()
        self.assertTrue(sites, "found no from-field read sites — regex rotted")
        offenders = sorted({m for m, _, _ in sites} - set(_FROM_FIELD_CONSUMERS))
        self.assertFalse(
            offenders,
            "un-allowlisted chat-row identity consumer(s) %r — a from/tfrom/"
            "rfrom/dm/convener/peer can reach a sink from a module no display-"
            "launder guard covers (the exact way sinks #12..#14 were born). Add "
            "each to _FROM_FIELD_CONSUMERS with a reason (LAUNDERED via "
            "chat._dsan/public_rows/_fmt, INTERNAL-MATCHING-ONLY, or "
            "NOT-A-CHAT-ROW) AND, if it emits, wire it into the Section-E "
            "runtime sweep.\n  sites: %s"
            % (offenders, [s for s in sites if s[0] in offenders]))

    def test_from_field_read_counts_are_pinned(self):
        from collections import Counter
        actual = dict(Counter(m for m, _, _ in _from_field_read_sites()))
        self.assertEqual(
            actual, _FROM_FIELD_READ_COUNTS,
            "chat-row identity read-site counts drifted from the pin. A new read "
            "site (even in an allowlisted module) can reach a sink unreviewed — "
            "verify each new site launders its emitted identity (or is internal/"
            "not-a-chat-row), then update _FROM_FIELD_READ_COUNTS.\n"
            "  actual: %s\n  pinned: %s" % (actual, _FROM_FIELD_READ_COUNTS))

    def test_allowlist_has_no_stale_entries(self):
        live = {m for m, _, _ in _from_field_read_sites()}
        stale = sorted(set(_FROM_FIELD_CONSUMERS) - live)
        self.assertFalse(stale, "from-field allowlist entries no longer read an "
                         "identity field: %r" % stale)


# ── G. source-driven nested transport profile/reason sink tripwire ────────────
# A transport projection may expose either nested identity (`profile`) OR network
# diagnostic (`reason`) independently. Enumerate every such key reader across
# helm/*.py: a new renderer/status/JSON adapter becomes a new site and fails
# until its laundering boundary is explicitly justified.


def _transport_projection_functions(source):
    functions = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        keys = set()
        for child in ast.walk(node):
            if isinstance(child, ast.Call) \
                    and isinstance(child.func, ast.Attribute) \
                    and child.func.attr in ("get", "pop", "setdefault") \
                    and child.args and isinstance(child.args[0], ast.Constant):
                keys.add(child.args[0].value)
            elif isinstance(child, ast.Subscript) \
                    and isinstance(child.slice, ast.Constant):
                keys.add(child.slice.value)
        projection = {"profile", "reason"} & keys
        transport = "transport" in node.name or any(
            isinstance(child, ast.Constant) and child.value == "transport"
            or isinstance(child, ast.Name) and "transport" in child.id
            or isinstance(child, ast.Attribute) and "transport" in child.attr
            for child in ast.walk(node))
        # `reason` is generic across unrelated domains. The old both-key pair is
        # unambiguous; a single-key projection must carry transport context.
        if projection and (len(projection) == 2 or transport):
            functions.add(node.name)
    return functions


def _transport_projection_sites():
    sites = set()
    for fn in sorted(os.listdir(PKG)):
        if not fn.endswith(".py"):
            continue
        with open(os.path.join(PKG, fn), encoding="utf-8") as f:
            sites.update((fn, name)
                         for name in _transport_projection_functions(f.read()))
    return sites


_TRANSPORT_PROJECTION_CONSUMERS = {
    ("chat.py", "cmd_chat"): "restore-journal lifecycle UNKNOWN reasons route through _safe_reason before terminal output",
    ("chat.py", "_public_transport"): "publish owner launders profile via _dsan and reason via _safe_reason",
    ("chat.py", "_failure_public"): "incident status owner launders profile and reason before every status/JSON consumer",
    ("chat.py", "_stamp_sign_failure"): "reason-only row projection routes its transport copy through _public_transport",
    ("chat.py", "_transport_tag"): "CLI, follow, and journal row renderer launders legacy/pre-fix nested fields at the sink",
    ("chat.py", "transport_failure_summary"): "CLI/node/doctor summary launders both nested fields at the sink",
    ("chat.py", "transport_status"): "status owner consumes already-public incidents and routes signer reason through _transient_failure",
    ("doctor.py", "check_chat_node"): "doctor routes direct signer reason through chat._safe_reason and transport summaries through their public owner",
    ("human.py", "status_line"): "TUI renderer launders both nested fields at the sink",
    ("chatnode.py", "_status"): "signer freshness/core rungs launder through chat._safe_reason at the sink — the staleness reason embeds a git COMMIT SUBJECT and a repo path, the core reason embeds names printed by the signer binary, and none of the three is helm-authored text",
    ("gateroute.py", "route"): "job-routing errors embed REMOTE-produced text (ssh stderr, the far gate's --json reason, gate.err tails, marker fields) — every such string routes gateroute._launder (chat._safe_reason) at this sink before terminal or JSON emission; the receipt body itself is validated and rendered by the gate-import/evidence machinery, never raw",
}


class TransportProjectionConsumerAllowlistTest(unittest.TestCase):
    def test_reason_only_and_profile_only_sinks_trip(self):
        functions = _transport_projection_functions("""
def reason_only(row):
    return row["transport"].get("reason")

def profile_only(row):
    return row["transport"]["profile"]

def unrelated(row):
    return row.get("code")
""")
        self.assertEqual(functions, {"reason_only", "profile_only"})

        live = _transport_projection_sites()
        for boundary in _TRANSPORT_PROJECTION_CONSUMERS:
            self.assertIn(boundary, live,
                          "central laundering boundary stopped being detected")

    def test_every_nested_transport_projection_is_allowlisted(self):
        self.assertEqual(
            _transport_projection_sites(),
            set(_TRANSPORT_PROJECTION_CONSUMERS),
            "nested transport profile/reason consumer drifted: every new public "
            "projection must pass chat._dsan/_safe_reason before rendering or "
            "JSON emission and be registered with its laundering reason")


class FleetSeatLaunderTest(unittest.TestCase):
    """helm/fleet.py PRINTED A RAW ROSTER KEY AND NOTHING SAW IT FOR AS LONG AS
    THE TRIPWIRE WAS A REGEX.

    fleet obtains the roster through `roster_checked()`, and the old scan
    matched the literal pattern `\\broster\\(\\)` — which cannot match
    `seats.roster_checked()` at all. So fleet never reached _ROSTER_CONSUMERS,
    its emission was never questioned, and `grep -c '_seat_label|_pub_row|
    _scrub' helm/fleet.py` returned 0.

    What it emits is the worst case available: `rows()` prints the seat into
    `%-18s`, the first and widest column of the census the owner reads to
    decide what to kill, resume or trust. BOTH name sources are attacker-
    SHAPED, but only one is attacker-REACHABLE, and this sentence used to say
    otherwise. MEASURED: `home.chat_name()` RAISES SeatNameError on a
    noncanonical HELM_CHAT_NAME — pinned by
    `test_hostile_helm_chat_name_is_rejected_at_the_seam` below, across four
    name readers — so the JOIN SEAM is precisely where the roster key IS
    validated. `write_roster` carries no token check of its own, and its two
    non-join callers are helm's own spawn paths writing a name helm chose, so
    a hostile roster key needs a direct file write rather than a join.
    HELM_CHAT_NAME is the reachable half: `fleet._seat_for` reads it straight
    out of a FOREIGN process environ, bypassing that seam entirely.

    _seat_label is scrub+clip, NOT anonymisation, so this test's first duty is
    proving the census still reads normally."""

    def _roster(self, hostile):
        return {"codex-3": {"session": "sid-a"}, hostile: {"session": "sid-b"}}

    def test_a_legitimate_seat_name_is_unchanged(self):
        """THE CONTROL, and it comes first: if laundering altered ordinary
        names the census would become unreadable and the cure worse than the
        bug."""
        from helm import fleet
        seat, src = fleet._seat_for("sid-a", {}, self._roster("x"), False)
        self.assertEqual(seat, "codex-3")
        self.assertEqual(src, "roster")

    def test_a_hostile_roster_key_cannot_reshape_the_census(self):
        """UPDATED, and the update is a TIGHTENING, not a relaxation.

        This asserted that a hostile key LAUNDERS — that the scrubbed name
        still contains its recognisable part and merely loses ESC/bidi. That
        was the contract until a cross-family review showed laundering ALIASES: a
        partially-scrubbed name is indistinguishable from the real seat it
        scrubs down to, with seat_unrenderable=False. So the identity rung now
        REFUSES a noncanonical name instead of printing a cleaned version of
        it, and the old positive control (`assertIn('codex-3', seat)`) asserts
        exactly the aliasing the cure exists to end.

        The PROPERTY this tripwire defends is unchanged and asserted harder:
        nothing hostile reaches the census's first column — now because
        nothing reaches it at all."""
        from helm import fleet
        hostile = "codex-3\x1b[2J‮work"
        seat, src = fleet._seat_for("sid-b", {}, self._roster(hostile), False)
        self.assertEqual(src, "roster", "the row must still be ATTRIBUTED to "
                                        "the roster — refusing the name is "
                                        "not refusing the row")
        self.assertIsNone(seat, "a noncanonical key must not render as a seat")
        label, unrenderable = fleet._display_seat(seat, src)
        self.assertTrue(unrenderable)
        for bad in ("\x1b", "‮", "codex-3"):
            self.assertNotIn(bad, label,
                             "%r reached the census's first column" % bad)

    def test_the_env_name_is_laundered_too_not_only_the_roster_key(self):
        """HELM_CHAT_NAME is read from a process environment, so it is at
        least as attacker-shaped as the roster key — and it takes an EARLIER
        return, so a fix that only covered the roster branch would leave the
        commoner path raw."""
        from helm import fleet
        env = {"HELM_CHAT_NAME": "codex-3\x1b[2J‮work"}
        seat, src = fleet._seat_for(None, env, {}, False)
        self.assertEqual(src, "env")
        # SAME TIGHTENING as the roster arm above: refused, not laundered,
        # because a cleaned name that still reads `codex-3` IS the alias.
        self.assertIsNone(seat)
        label, unrenderable = fleet._display_seat(seat, src)
        self.assertTrue(unrenderable)
        for bad in ("\x1b", "‮", "codex-3"):
            self.assertNotIn(bad, label)
        # …and the legit env name is still untouched — the control that keeps
        # this a tightening rather than a blanket refusal.
        plain, _ = fleet._seat_for(None, {"HELM_CHAT_NAME": "codex-3"}, {}, False)
        self.assertEqual(plain, "codex-3")


if __name__ == "__main__":
    unittest.main()
